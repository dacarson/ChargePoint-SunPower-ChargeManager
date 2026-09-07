"""
Offline analysis of model_shadow_logger.py's solar_charge_shadow live data.

Both model_excess_watts and heuristic_excess_watts logged at time T are forecasts of excess power
~5 minutes ahead (control_interval in solar_charge_controller.py) — neither is "truth" at the
time it's logged. To score them we pair each forecast with the realized excess_now_watts at
T+horizon, found via merge_asof against the same measurement (nearest match within a tolerance,
since ticks aren't perfectly regular — see model_shadow_logger.py's check-interval/skip logic).

Also checks whether a watts-level forecast win survives determine_target_amperage()'s
round-UP-to-nearest-allowed-amp bucketing (solar_charge_controller.py), by comparing both
heuristic's and model's actual logged amp choice against an "ideal" amp derived from the realized
(perfect-hindsight) future excess, using the same threshold/rounding rules.
"""
import argparse

import numpy as np
import pandas as pd
from influxdb import InfluxDBClient

MIN_AMPERAGE = 8
MAX_AMPERAGE = 40
VOLTAGE = 240
ALLOWED_AMPS = list(range(MIN_AMPERAGE, MAX_AMPERAGE + 1))
MINIMUM_WATTS_REQUIRED = (MIN_AMPERAGE - 0.5) * VOLTAGE

# Marginal $/kWh rates for the dollar-savings estimate — PG&E Solar Billing Plan (NEM 3.0) only,
# derived from ~/pge/estimate_bill.py's March/July 2026 rate tables (update if that script's RATES
# section changes). CleanPowerSF generation charges are deliberately excluded: they're billed on
# *net* imports per TOU bucket over the whole billing period, floored at 0 — a marginal per-tick
# kWh can't be validly priced against that without knowing whether the bucket already nets
# import- or export-positive for the period, so this estimate is PG&E-only and likely somewhat
# understates total real savings.
#
# import_rate = (PGE energy-delivered charge + PCIA + non-bypassable charges) marked up by PG&E's
# franchise-fee (0.2%) and SF Prop C (1.0%) surcharges, which apply to the PG&E subtotal.
# export_credit = (export delivered credit + export bonus credit) marked up the same way. PG&E's
# export credit is a single seasonal rate, not TOU-bucket-dependent (see estimate_bill.py).
_PGE_SURCHARGE_MULT = 1 + 0.002 + 0.010  # franchise fee + SF Prop C
IMPORT_RATE_PER_KWH = {  # $/kWh, PG&E energy delivered + PCIA + NBC only
    "Summer": {"peak": 0.3402, "part_peak": 0.2767, "off_peak": 0.2649},
    "Winter": {"peak": 0.2700, "part_peak": 0.2678, "off_peak": 0.2673},
}
EXPORT_CREDIT_PER_KWH = {"Summer": 0.0531, "Winter": 0.0239}  # $/kWh, flat across TOU buckets
_SUMMER_MONTHS = {6, 7, 8, 9}
MAX_TICK_HOURS = 5.0 / 60.0  # cap a tick's assumed duration at control_interval (5 min)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Compare SolarChargeML model_run4 vs the heuristic controller using live "
                    "solar_charge_shadow data from model_shadow_logger.py."
    )
    parser.add_argument("--influxdb-host", default="localhost")
    parser.add_argument("--influxdb-port", type=int, default=8086)
    parser.add_argument("--influxdb-db", default="pvs6")
    parser.add_argument("--horizon-minutes", type=float, default=5.0,
                         help="Forecast horizon both model_excess_watts and heuristic_excess_watts "
                              "predict ahead to (must match solar_charge_controller.py's "
                              "control_interval as actually deployed; default: 5)")
    parser.add_argument("--tolerance-seconds", type=float, default=90.0,
                         help="Max gap allowed when matching a forecast to its +horizon ground "
                              "truth row (default: 90)")
    return parser.parse_args()


def get_tou_excess_threshold(base_minimum_watts, tou_period):
    """Mirrors solar_charge_controller.py's function of the same name."""
    if tou_period == "peak":
        return base_minimum_watts * 2.0
    if tou_period == "off_peak":
        return -500.0
    return base_minimum_watts


def determine_target_amperage(avg_excess_solar_watts, allowed_amps=ALLOWED_AMPS, voltage=VOLTAGE):
    """Mirrors solar_charge_controller.py's function of the same name: rounds UP to the nearest
    allowed amp."""
    if avg_excess_solar_watts <= 0:
        return 0
    ideal_amps = max(avg_excess_solar_watts / voltage, min(allowed_amps) - 0.5)
    possible = [a for a in allowed_amps if a >= ideal_amps]
    return min(possible) if possible else max(allowed_amps)


def compute_ideal_target_amps(excess_w, tou_period):
    """The amp a perfect (hindsight) forecast would have chosen — mirrors
    model_shadow_logger.py's compute_model_target_amps, applied to realized future excess instead
    of a prediction."""
    threshold = get_tou_excess_threshold(MINIMUM_WATTS_REQUIRED, tou_period)
    if excess_w < threshold:
        return 0
    amps = determine_target_amperage(excess_w)
    if amps > 0 and excess_w < MINIMUM_WATTS_REQUIRED:
        return 0
    return amps


def mae(s):
    return s.abs().mean()


def rmse(s):
    return np.sqrt((s ** 2).mean())


def report_forecast(sub, label):
    n = len(sub)
    if n == 0:
        print(f"{label}: no rows")
        return
    heur_mae, model_mae = mae(sub["heuristic_error"]), mae(sub["model_error"])
    print(f"{label} (n={n})")
    print(f"  Naive persistence   MAE={mae(sub['persistence_error']):8.1f}W  RMSE={rmse(sub['persistence_error']):8.1f}W")
    print(f"  Heuristic (current) MAE={heur_mae:8.1f}W  RMSE={rmse(sub['heuristic_error']):8.1f}W")
    print(f"  Model (shadow)      MAE={model_mae:8.1f}W  RMSE={rmse(sub['model_error']):8.1f}W")
    if heur_mae:
        print(f"  Model vs heuristic: {(model_mae - heur_mae) / heur_mae * 100:+.1f}%")
    print()


def report_amp(sub, label):
    n = len(sub)
    if n == 0:
        return
    heur_exact = (sub["heuristic_target_amperage"] == sub["ideal_target_amps"]).mean()
    model_exact = (sub["model_target_amperage"] == sub["ideal_target_amps"]).mean()
    print(f"{label} (n={n})")
    print(f"  Heuristic: exact-match {heur_exact * 100:5.1f}%  mean|err|={sub['heuristic_amp_err'].abs().mean():.2f}A")
    print(f"  Model:     exact-match {model_exact * 100:5.1f}%  mean|err|={sub['model_amp_err'].abs().mean():.2f}A")
    print()


def tick_cost(amps, actual_excess_w, dt_hours, import_rate, export_credit):
    """$ cost of charging at `amps` for `dt_hours`, given the actual (realized) excess solar
    power available. Power drawn beyond available excess comes from the grid at import_rate;
    power drawn from what would otherwise have been excess (exported) solar costs the forgone
    export_credit instead — NOT free, but far cheaper than import under NEM 3.0."""
    watts = amps * VOLTAGE
    if watts <= 0:
        return 0.0
    solar_available = max(actual_excess_w, 0.0)
    grid_kwh = max(0.0, watts - solar_available) / 1000.0 * dt_hours
    solar_kwh = min(watts, solar_available) / 1000.0 * dt_hours
    return grid_kwh * import_rate + solar_kwh * export_credit


def report_dollar_savings(merged):
    df = merged.sort_values("time").reset_index(drop=True)
    df["dt_hours"] = (
        df["time"].shift(-1) - df["time"]
    ).dt.total_seconds().div(3600.0).clip(upper=MAX_TICK_HOURS).fillna(0.0)
    df["season"] = np.where(df["time"].dt.month.isin(_SUMMER_MONTHS), "Summer", "Winter")

    def rates(row):
        return IMPORT_RATE_PER_KWH[row["season"]][row["tou_period"]], EXPORT_CREDIT_PER_KWH[row["season"]]

    heur_cost = model_cost = 0.0
    by_period = {}
    for _, row in df.iterrows():
        import_rate, export_credit = rates(row)
        h = tick_cost(row["heuristic_target_amperage"], row["actual_future_excess_w"], row["dt_hours"], import_rate, export_credit)
        m = tick_cost(row["model_target_amperage"], row["actual_future_excess_w"], row["dt_hours"], import_rate, export_credit)
        heur_cost += h
        model_cost += m
        p = by_period.setdefault(row["tou_period"], [0.0, 0.0])
        p[0] += h
        p[1] += m

    days = (df["time"].max() - df["time"].min()).total_seconds() / 86400.0
    print("--- Estimated $ impact (PG&E-only marginal rates; see comment at top of this file "
          "for what's excluded/approximated) ---")
    print(f"Period: {days:.1f} days ({df['time'].min().date()} -> {df['time'].max().date()})")
    print(f"  Heuristic estimated charging cost: ${heur_cost:8.2f}")
    print(f"  Model     estimated charging cost: ${model_cost:8.2f}")
    savings = heur_cost - model_cost
    print(f"  Model savings: ${savings:+.2f} total (${savings / days:+.3f}/day, "
          f"${savings / days * 30:+.2f}/mo projected)\n")
    for period, (h, m) in sorted(by_period.items()):
        print(f"  TOU={period:<10} heuristic=${h:7.2f}  model=${m:7.2f}  savings=${h - m:+.2f}")
    print()


def main():
    args = parse_args()
    horizon = pd.Timedelta(minutes=args.horizon_minutes)
    tolerance = pd.Timedelta(seconds=args.tolerance_seconds)

    client = InfluxDBClient(host=args.influxdb_host, port=args.influxdb_port, database=args.influxdb_db, timeout=30)
    result = client.query('SELECT * FROM "solar_charge_shadow" ORDER BY time ASC')
    df = pd.DataFrame(list(result.get_points()))
    if df.empty:
        print("No solar_charge_shadow data found.")
        return
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)

    print(f"Total rows: {len(df)}")
    print(f"Time range: {df['time'].min()} -> {df['time'].max()}")
    print(f"TOU period counts:\n{df['tou_period'].value_counts()}\n")

    # Ground truth lookup: nearest row's excess_now_watts to (T + horizon), within tolerance.
    truth = df[["time", "excess_now_watts"]].rename(columns={"excess_now_watts": "actual_future_excess_w"})
    df["lookup_time"] = df["time"] + horizon

    merged = pd.merge_asof(
        df.sort_values("lookup_time"),
        truth.sort_values("time"),
        left_on="lookup_time",
        right_on="time",
        tolerance=tolerance,
        direction="nearest",
        suffixes=("", "_truth"),
    )
    merged = merged.dropna(subset=["actual_future_excess_w"]).sort_values("time").reset_index(drop=True)

    print(f"Rows with a matched +{args.horizon_minutes:g}min ground truth (within {tolerance}): "
          f"{len(merged)} ({len(merged) / len(df) * 100:.1f}% of total)\n")

    merged["model_error"] = merged["model_excess_watts"] - merged["actual_future_excess_w"]
    merged["heuristic_error"] = merged["heuristic_excess_watts"] - merged["actual_future_excess_w"]
    merged["persistence_error"] = merged["excess_now_watts"] - merged["actual_future_excess_w"]

    report_forecast(merged, "OVERALL")
    for period in sorted(merged["tou_period"].dropna().unique()):
        report_forecast(merged[merged["tou_period"] == period], f"TOU={period}")

    # Target amperage agreement — informational only. The shadow logger always applies the
    # heuristic's stricter minimum-start threshold (it has no session state to know whether
    # charging is already active), so this is not the metric that validates the model itself.
    amp_match = (merged["model_target_amperage"] == merged["heuristic_target_amperage"]).mean()
    print(f"model_target_amperage == heuristic_target_amperage: {amp_match * 100:.1f}% of matched rows")
    print(f"Mean |model_target_amperage - heuristic_target_amperage|: "
          f"{(merged['model_target_amperage'] - merged['heuristic_target_amperage']).abs().mean():.2f}A\n")

    # Does the watts-forecast win survive amp rounding, or get lost in the ~240W-wide bucket?
    merged["ideal_target_amps"] = merged.apply(
        lambda r: compute_ideal_target_amps(r["actual_future_excess_w"], r["tou_period"]), axis=1
    )
    merged["model_amp_err"] = merged["model_target_amperage"] - merged["ideal_target_amps"]
    merged["heuristic_amp_err"] = merged["heuristic_target_amperage"] - merged["ideal_target_amps"]

    print("--- Amp choice vs. the 'perfect forecast' ideal amperage ---")
    report_amp(merged, "OVERALL")
    for period in sorted(merged["tou_period"].dropna().unique()):
        report_amp(merged[merged["tou_period"] == period], f"TOU={period}")

    report_dollar_savings(merged)


if __name__ == "__main__":
    main()
