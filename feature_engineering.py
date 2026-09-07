"""
Shared feature engineering for SolarChargeML training/eval scripts from Run 5 onward.

Identical logic to train_run4.py's inline load_and_engineer() (Runs 1-4 are historical
records and intentionally left untouched with their own inline copies) — factored out here
so Run 5+ scripts (train_run5{a,b,c}.py, evaluate_candidates.py) share one definition instead
of drifting copies.

This file is vendored verbatim into the chargepoint-sunpower-chargemanager repo (copied, not
imported cross-repo — the two repos are otherwise independent) so model_shadow_logger.py's
live inference uses the exact same add_derived_features() as training, instead of a
hand-maintained reimplementation. If you change anything in add_derived_features() or FEATURES,
re-copy this file into that repo's checkout. add_delta_target()/load_and_engineer() are
training-only (they need future ground truth, which live inference doesn't have) and are not
used by the vendored copy.
"""
import numpy as np
import pandas as pd

BIN_SECONDS = 30  # must match export_and_join.py's BIN

STATE_FEATURES = ["pv_p", "net_p", "site_load_p", "baseline_house_load_w", "excess_now_w"]
VOLATILITY_FEATURES = ["pv_p_std", "pv_p_range", "site_load_p_std", "site_load_p_range"]
WEATHER_FEATURES = [
    "solar_radiation", "illuminance", "uv", "wind_avg", "wind_gust", "wind_lull",
    "wind_direction", "relative_humidity", "station_pressure", "temperature", "rain_accumulated",
]
SLOPE_FEATURES = [
    "pv_p_slope_2min", "pv_p_slope_10min", "pv_p_slope_30min",
    "solar_radiation_slope_10min", "excess_now_slope_2min", "excess_now_slope_10min",
]
TIME_FEATURES = ["time_of_day_sin", "time_of_day_cos", "day_of_year_sin", "day_of_year_cos"]
FEATURES = STATE_FEATURES + VOLATILITY_FEATURES + WEATHER_FEATURES + SLOPE_FEATURES + TIME_FEATURES

RAW_TARGET = "excess_future_w"
DELTA_TARGET = "excess_delta_w"
DAYTIME_PV_W = 500.0  # matches solar_charge_controller.py's production < 500W branch cutoff


def steps(minutes, bin_seconds=BIN_SECONDS):
    return int(minutes * 60 / bin_seconds)


def add_derived_features(df, bin_seconds=BIN_SECONDS):
    """Add the engineered columns in VOLATILITY_FEATURES/SLOPE_FEATURES/TIME_FEATURES to a
    dataframe that already has a regular bin_seconds-spaced DatetimeIndex and the base columns
    (pv_p/pv_p_min/pv_p_max, site_load_p/_min/_max, solar_radiation, excess_now_w, day_of_year,
    time_of_day). Shared by training (via load_and_engineer, from a CSV) and live inference
    (model_shadow_logger.py's build_feature_frame, from InfluxDB) — this is the function that
    must stay identical between the two repos; see this file's module docstring."""
    df = df.copy()
    df["pv_p_range"] = df["pv_p_max"] - df["pv_p_min"]
    df["site_load_p_range"] = df["site_load_p_max"] - df["site_load_p_min"]

    df["pv_p_slope_2min"] = (df["pv_p"] - df["pv_p"].shift(steps(2, bin_seconds))) / 2.0
    df["pv_p_slope_10min"] = (df["pv_p"] - df["pv_p"].shift(steps(10, bin_seconds))) / 10.0
    df["pv_p_slope_30min"] = (df["pv_p"] - df["pv_p"].shift(steps(30, bin_seconds))) / 30.0
    df["solar_radiation_slope_10min"] = (
        df["solar_radiation"] - df["solar_radiation"].shift(steps(10, bin_seconds))
    ) / 10.0
    df["excess_now_slope_2min"] = (
        df["excess_now_w"] - df["excess_now_w"].shift(steps(2, bin_seconds))
    ) / 2.0
    df["excess_now_slope_10min"] = (
        df["excess_now_w"] - df["excess_now_w"].shift(steps(10, bin_seconds))
    ) / 10.0

    df["time_of_day_sin"] = np.sin(2 * np.pi * df["time_of_day"] / 24.0)
    df["time_of_day_cos"] = np.cos(2 * np.pi * df["time_of_day"] / 24.0)
    df["day_of_year_sin"] = np.sin(2 * np.pi * df["day_of_year"] / 365.25)
    df["day_of_year_cos"] = np.cos(2 * np.pi * df["day_of_year"] / 365.25)
    return df


def add_delta_target(df):
    """Training-only: adds DELTA_TARGET from RAW_TARGET (future ground truth). Not used by
    live inference, which doesn't have future values — not part of the vendored subset."""
    df = df.copy()
    df[DELTA_TARGET] = df[RAW_TARGET] - df["excess_now_w"]
    return df


def load_and_engineer(path, bin_seconds=BIN_SECONDS, daytime_only=True):
    """Load an export_and_join.py CSV, reindex onto a regular BIN-width grid (so shift-based
    lag features are time-correct across the export's small gaps), and add derived features +
    the training target. Set daytime_only=False to keep nighttime rows too (e.g. for inspecting
    raw coverage) — training/eval always wants the default True."""
    df = pd.read_csv(path, parse_dates=["time"]).set_index("time").sort_index()
    full_idx = pd.date_range(df.index.min(), df.index.max(), freq=f"{bin_seconds}s", tz="UTC")
    df = df.reindex(full_idx)

    df = add_derived_features(df, bin_seconds)
    df = add_delta_target(df)

    before = len(df)
    df = df.dropna(subset=FEATURES + [RAW_TARGET, DELTA_TARGET])
    if daytime_only:
        df = df[df["pv_p"] >= DAYTIME_PV_W]
    print(f"  {path}: {len(df)} / {before} rows after reindex + lag-feature dropna"
          f"{' + daytime filter (pv_p >= ' + str(DAYTIME_PV_W) + 'W)' if daytime_only else ''}.")
    return df
