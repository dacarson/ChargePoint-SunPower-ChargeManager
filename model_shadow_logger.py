"""
Shadow-mode live validator for the SolarChargeML model (currently model_run5.joblib; see
MODEL_RUN5_README.md — model_run4.joblib was the default through 2026-09-06, superseded after
its own live shadow data showed watts-MAE gains weren't translating into amp/dollar gains).

Runs alongside solar_charge_controller.py, read-only: queries InfluxDB for the same live
state the real controller sees, computes what the model would predict/set, and logs it next
to the real controller's own logged prediction/amperage for direct comparison in Grafana. It
never calls the ChargePoint API and never sets an amperage — no ability to affect real
charging behavior.

Feature engineering here must stay in sync with WeatherML's
workspace/SolarChargeML/feature_engineering.py (shared by all Run 5+ training scripts; Run 4
used its own now-historical copy in train_run4.py) and
workspace/SolarChargeML/export_and_join.py (which builds the training data). The feature list
and joblib bundle shape are unchanged from Run 4, so no engineering changes were needed for
this model swap — see MODEL_RUN5_README.md for the full methodology and feature list.
"""
import sys
import time
import logging
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import joblib
from influxdb import InfluxDBClient

from solar_charge_controller import (
    get_tou_period, get_tou_excess_threshold, determine_target_amperage, setup_logging,
)

BIN_SECONDS = 30  # must match train_run4.py's grid
LOOKBACK_MINUTES = 35  # >= the longest slope window (30min) + buffer for reindex edge effects
DAYTIME_PV_W = 500.0  # matches solar_charge_controller.py's production < 500W branch cutoff

# Empirically observed 2026-08-19 from historical target_amperage/current_amperage in
# pvs6.solar_charge_control (SELECT DISTINCT): every integer 8-40A, no gaps. Update if the
# charger's configured minimum/maximum ever changes.
MIN_AMPERAGE = 8
MAX_AMPERAGE = 40
ALLOWED_AMPS = list(range(MIN_AMPERAGE, MAX_AMPERAGE + 1))
VOLTAGE = 240
MINIMUM_WATTS_REQUIRED = (MIN_AMPERAGE - 0.5) * VOLTAGE


def steps(minutes):
    return int(minutes * 60 / BIN_SECONDS)


def floor_to_bin(ts, bin_seconds=BIN_SECONDS):
    """Floor a timestamp to the nearest bin_seconds boundary, epoch-aligned — InfluxDB's
    GROUP BY time() bins are always epoch-aligned regardless of query WHERE bounds, so the
    pandas reindex grid must match that alignment exactly or every row silently comes back NaN.
    Also conveniently excludes the still-forming (incomplete) current bin from the query."""
    epoch = ts.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % bin_seconds), tz=timezone.utc)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Shadow-mode live validator for the SolarChargeML model."
    )
    parser.add_argument("--influxdb-host", default="localhost")
    parser.add_argument("--influxdb-port", type=int, default=8086)
    parser.add_argument("--influxdb-user", default=None)
    parser.add_argument("--influxdb-pass", default=None)
    parser.add_argument("--influxdb-db", default="pvs6",
                         help="Database with sunpower_power / solar_charge_control (default: pvs6)")
    parser.add_argument("--weather-db", default="weather",
                         help="Database with wf/obs_st (default: weather)")
    parser.add_argument("--model-path", default="model_run5.joblib",
                         help="Path to the joblib model bundle, relative to this script's directory "
                              "unless absolute (default: model_run5.joblib; model_run4.joblib was "
                              "the default through 2026-09-06 — see MODEL_RUN5_README.md for why "
                              "it was superseded, and MODEL_RUN4_README.md for the prior model)")
    parser.add_argument("--check-interval", type=int, default=60,
                         help="Seconds between shadow predictions (default: 60)")
    parser.add_argument("--peak-excess-multiplier", type=float, default=2.0,
                         help="Must match the value solar_charge_controller.py is actually "
                              "deployed with, for a fair comparison (default: 2.0)")
    parser.add_argument("--offpeak-grid-tolerance", type=float, default=500.0,
                         help="Must match the value solar_charge_controller.py is actually "
                              "deployed with, for a fair comparison (default: 500.0)")
    parser.add_argument("--log-file", default="model_shadow_logger.log")
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


def fetch_binned(client, db, measurement, field_aggs, start, end, bin_width=f"{BIN_SECONDS}s"):
    """Single-shot (no chunking — LOOKBACK_MINUTES is small) version of
    export_and_join.py's fetch_binned_stats: BIN-width aggregates, MEAN keeps the bare field
    name, STDDEV/MIN/MAX get _std/_min/_max suffixes."""
    suffixes = {"MEAN": "", "STDDEV": "_std", "MIN": "_min", "MAX": "_max"}
    select_parts, out_cols = [], []
    for field, aggs in field_aggs.items():
        for agg in aggs:
            col = f"{field}{suffixes[agg.upper()]}"
            select_parts.append(f'{agg.upper()}("{field}") AS "{col}"')
            out_cols.append(col)
    select_clause = ", ".join(select_parts)
    start_str = start.strftime('%Y-%m-%dT%H:%M:%SZ')
    end_str = end.strftime('%Y-%m-%dT%H:%M:%SZ')
    query = (
        f'SELECT {select_clause} FROM "{measurement}" '
        f"WHERE time >= '{start_str}' AND time < '{end_str}' "
        f"GROUP BY time({bin_width}) fill(null)"
    )
    result = client.query(query, database=db)
    points = list(result.get_points())
    if not points:
        return pd.DataFrame(columns=out_cols, index=pd.DatetimeIndex([], name="time", tz="UTC"))
    df = pd.DataFrame(points)
    df["time"] = pd.to_datetime(df["time"])
    df = df.drop_duplicates(subset="time").set_index("time").sort_index()
    return df[out_cols]


def build_feature_frame(client, pvs6_db, weather_db, now):
    """Reproduce export_and_join.py + train_run4.py's feature engineering over a short trailing
    window, returning the fully-joined, feature-engineered DataFrame (one row per 30s bin)."""
    now = floor_to_bin(now)
    start = now - timedelta(minutes=LOOKBACK_MINUTES)

    pv = fetch_binned(
        client, pvs6_db, "sunpower_power",
        {"pv_p": ["MEAN", "STDDEV", "MIN", "MAX"], "net_p": ["MEAN"],
         "site_load_p": ["MEAN", "STDDEV", "MIN", "MAX"]},
        start, now,
    )
    pv *= 1000.0  # kW -> W

    ctrl = fetch_binned(
        client, pvs6_db, "solar_charge_control",
        {"excess_solar_watts": ["MEAN"], "charging_power_watts": ["MEAN"],
         "target_amperage": ["MEAN"], "current_amperage": ["MEAN"]},
        start, now,
    )

    wx = fetch_binned(
        client, weather_db, "wf/obs_st",
        {"solar_radiation": ["MEAN"], "illuminance": ["MEAN"], "uv": ["MEAN"],
         "wind_avg": ["MEAN"], "wind_gust": ["MEAN"], "wind_lull": ["MEAN"],
         "wind_direction": ["MEAN"], "relative_humidity": ["MEAN"],
         "station_pressure": ["MEAN"], "temperature": ["MEAN"], "rain_accumulated": ["MEAN"]},
        start, now,
    )

    full_index = pd.date_range(start, now, freq=f"{BIN_SECONDS}s", tz="UTC", inclusive="left")
    pv = pv.reindex(full_index)
    # Step-function control-loop log, forward-filled — see export_and_join.py's Run 4 note.
    ctrl = ctrl.reindex(full_index).ffill(limit=20)
    # wf/obs_st reports ~once/minute; forward-fill the alternating empty 30s bin only.
    wx = wx.reindex(full_index).ffill(limit=1)

    df = pv.join(ctrl, how="left").join(wx, how="left")
    df.index.name = "time"

    df["baseline_house_load_w"] = df["site_load_p"] - df["charging_power_watts"].fillna(0.0)
    df["excess_now_w"] = df["pv_p"] - df["baseline_house_load_w"]

    df["pv_p_range"] = df["pv_p_max"] - df["pv_p_min"]
    df["site_load_p_range"] = df["site_load_p_max"] - df["site_load_p_min"]

    df["pv_p_slope_2min"] = (df["pv_p"] - df["pv_p"].shift(steps(2))) / 2.0
    df["pv_p_slope_10min"] = (df["pv_p"] - df["pv_p"].shift(steps(10))) / 10.0
    df["pv_p_slope_30min"] = (df["pv_p"] - df["pv_p"].shift(steps(30))) / 30.0
    df["solar_radiation_slope_10min"] = (
        df["solar_radiation"] - df["solar_radiation"].shift(steps(10))
    ) / 10.0
    df["excess_now_slope_2min"] = (df["excess_now_w"] - df["excess_now_w"].shift(steps(2))) / 2.0
    df["excess_now_slope_10min"] = (
        df["excess_now_w"] - df["excess_now_w"].shift(steps(10))
    ) / 10.0

    df["day_of_year"] = df.index.dayofyear
    # NOTE: intentionally minute-granularity (no seconds), even on this 30s grid — matches
    # export_and_join.py exactly, which model_run4 was trained on. Two consecutive 30s bins in
    # the same minute get an identical time_of_day; "fixing" this to be more precise would feed
    # the model an input distribution it never saw in training.
    df["time_of_day"] = df.index.hour + df.index.minute / 60.0
    df["time_of_day_sin"] = np.sin(2 * np.pi * df["time_of_day"] / 24.0)
    df["time_of_day_cos"] = np.cos(2 * np.pi * df["time_of_day"] / 24.0)
    df["day_of_year_sin"] = np.sin(2 * np.pi * df["day_of_year"] / 365.25)
    df["day_of_year_cos"] = np.cos(2 * np.pi * df["day_of_year"] / 365.25)

    return df


def compute_model_target_amps(model_excess_w, tou_threshold):
    """Mirrors solar_charge_controller.py's elif predicted_excess >= tou_threshold branch.
    Known simplification: the real script also checks `current_charging_watts == 0` before
    applying the stricter minimum-start threshold (only new starts face the higher bar; an
    already-charging session doesn't). This shadow logger has no session state, so it always
    applies the stricter rule — see MODEL_RUN4_README.md. Only affects this derived amperage
    field, not the primary model_excess_watts comparison."""
    if model_excess_w < tou_threshold:
        return 0
    target_amps = determine_target_amperage(model_excess_w, ALLOWED_AMPS)
    if target_amps > 0 and model_excess_w < MINIMUM_WATTS_REQUIRED:
        return 0
    return target_amps


def log_shadow_metrics(influx_client, db, latest, model_excess_w, model_target_amps, tou_period):
    fields = {
        "model_excess_watts": float(model_excess_w),
        "model_target_amperage": int(model_target_amps),
        "pv_p": float(latest["pv_p"]),
        "excess_now_watts": float(latest["excess_now_w"]),
        "tou_period": tou_period,
    }
    for label, col in [
        ("heuristic_excess_watts", "excess_solar_watts"),
        ("heuristic_target_amperage", "target_amperage"),
        ("heuristic_current_amperage", "current_amperage"),
    ]:
        val = latest.get(col)
        if val is not None and not pd.isna(val):
            fields[label] = int(round(val)) if "amperage" in label else float(val)

    json_body = [{"measurement": "solar_charge_shadow", "fields": fields}]
    try:
        influx_client.write_points(json_body, database=db)
    except Exception as e:
        logging.warning(f"Failed to write shadow metrics to InfluxDB: {e}")


def main():
    args = parse_args()
    setup_logging(args.log_file, args.quiet)

    model_path = Path(args.model_path)
    if not model_path.is_absolute():
        model_path = Path(__file__).parent / model_path
    bundle = joblib.load(model_path)
    model = bundle["model"]
    features = bundle["features"]
    logging.info(f"Loaded model from {model_path} ({len(features)} features).")

    influx_client = InfluxDBClient(
        host=args.influxdb_host, port=args.influxdb_port,
        username=args.influxdb_user, password=args.influxdb_pass,
        database=args.influxdb_db,
    )

    logging.info(
        f"Starting shadow logger: check-interval={args.check_interval}s, "
        f"pvs6-db={args.influxdb_db}, weather-db={args.weather_db}"
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            df = build_feature_frame(influx_client, args.influxdb_db, args.weather_db, now)
            latest = df.iloc[-1]

            if pd.isna(latest.get("pv_p")) or latest["pv_p"] < DAYTIME_PV_W:
                logging.info(
                    f"Low/no production (pv_p={latest.get('pv_p')}); model doesn't apply "
                    f"here (matches solar_charge_controller.py's production<500W branch and "
                    f"the model's daytime-only training). Skipping."
                )
                time.sleep(args.check_interval)
                continue

            missing = [f for f in features if pd.isna(latest.get(f))]
            if missing:
                logging.warning(f"Missing/NaN features, skipping this tick: {missing}")
                time.sleep(args.check_interval)
                continue

            X = latest[features].to_frame().T.astype(float)
            pred_delta = model.predict(X)[0]
            model_excess_w = latest["excess_now_w"] + pred_delta

            tou_period = get_tou_period()
            tou_threshold = get_tou_excess_threshold(MINIMUM_WATTS_REQUIRED, tou_period)
            if tou_period == "peak":
                tou_threshold = MINIMUM_WATTS_REQUIRED * args.peak_excess_multiplier
            elif tou_period == "off_peak":
                tou_threshold = -1.0 * args.offpeak_grid_tolerance

            model_target_amps = compute_model_target_amps(model_excess_w, tou_threshold)

            logging.info(
                f"[{tou_period}] pv_p={latest['pv_p']:.0f}W excess_now={latest['excess_now_w']:.0f}W "
                f"model_excess={model_excess_w:.0f}W model_amps={model_target_amps}A | "
                f"heuristic_excess={latest.get('excess_solar_watts')} "
                f"heuristic_target_amps={latest.get('target_amperage')} "
                f"heuristic_current_amps={latest.get('current_amperage')}"
            )

            log_shadow_metrics(
                influx_client, args.influxdb_db, latest, model_excess_w, model_target_amps,
                tou_period,
            )

        except Exception as e:
            logging.error(f"Error in shadow logger loop: {e}", exc_info=True)

        time.sleep(args.check_interval)


if __name__ == "__main__":
    main()
