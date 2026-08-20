# model_run4.joblib — SolarChargeML excess-solar predictor

Trained model for predicting excess solar power 5 minutes ahead, as a candidate replacement for
`solar_charge_controller.py`'s `predicted_excess` step (the average+slope heuristic in
`get_solar_power_status()`/`main()`). **Currently shadow-mode only** — `model_shadow_logger.py`
logs what this model would predict/set alongside the real controller's own values, for live
comparison. It does not control charging.

Trained in the [WeatherML](https://github.com/dacarson/WeatherML) repo, not this one — see
`workspace/SolarChargeML/` there for the full data pipeline and experiment history
(`SOLARCHARGE_PLAN.md`, `SOLARCHARGE_EXPERIMENT_LOG.md`). This file documents the model itself,
scoped to what's needed to use/maintain it here.

## What it predicts

`excess_delta_w = excess_future_w − excess_now_w`, where `excess_*_w = pv_p − (site_load_p −
charging_power_watts)` — production minus non-EV house load, 5 minutes ahead vs. now. At
inference: `predicted_excess = excess_now_w + model.predict(features)`. Predicting the delta
rather than the raw value was a deliberate choice — see the experiment log's Run 1/2 entries for
why (persistence is a strong baseline at this horizon; predicting the raw value makes the model
re-derive persistence before any of the actually-useful correction shows up in the loss).

**Trained only on daytime data** (`pv_p >= 500W`) — the only regime `solar_charge_controller.py`
ever consults `predicted_excess` in (its `production < 500W` branch bypasses it entirely).

## Architecture

`sklearn.ensemble.HistGradientBoostingRegressor` (gradient-boosted trees), chosen for RPi
deployment: pure sklearn dependency, `joblib`-picklable, no ONNX/TFLite export needed, CPU
inference well within a 5-minute (or, per `model_shadow_logger.py`, 60-second) cycle.

`max_iter=300` (ran the full 300, didn't trigger early stopping), `learning_rate=0.05`,
`max_depth=8`, `validation_fraction=0.1`/`early_stopping=True`/`n_iter_no_change=15` for internal
monitoring, `random_state=42`.

## Data resolution

**30-second bins** — the key change from three earlier failed attempts at 1-minute resolution
(see experiment log Runs 1-3). `pv_p`/`site_load_p` come from `sunpower_power`, sampled ~60x/min
natively, so a 30s bin still has ~15-30 raw samples — enough for meaningful `STDDEV`/range.
`wf/obs_st` (WeatherFlow) reports only once/minute natively — no sub-minute signal is available
from that source at any bin width.

## Features (30, exact order stored in the joblib bundle — see "Loading" below)

| Group | Features |
|---|---|
| State | `pv_p`, `net_p`, `site_load_p`, `baseline_house_load_w`, `excess_now_w` |
| Sub-minute volatility | `pv_p_std`, `pv_p_range`, `site_load_p_std`, `site_load_p_range` |
| Weather (WeatherFlow) | `solar_radiation`, `illuminance`, `uv`, `wind_avg`, `wind_gust`, `wind_lull`, `wind_direction`, `relative_humidity`, `station_pressure`, `temperature`, `rain_accumulated` |
| Trend slopes | `pv_p_slope_2min`, `pv_p_slope_10min`, `pv_p_slope_30min`, `solar_radiation_slope_10min`, `excess_now_slope_2min`, `excess_now_slope_10min` |
| Cyclic time | `time_of_day_sin`, `time_of_day_cos`, `day_of_year_sin`, `day_of_year_cos` |

`baseline_house_load_w = site_load_p − charging_power_watts` (EV charging load subtracted out of
the total site meter reading — `charging_power_watts` comes from `solar_charge_control`, itself
sometimes an amperage-based *estimate* rather than a direct meter reading, see
`get_current_charging_watts()` in `solar_charge_controller.py`). `excess_now_w = pv_p −
baseline_house_load_w`.

**`time_of_day` is intentionally minute-granularity** (`hour + minute/60.0`, no seconds
component) even on this 30s grid — inherited unchanged from the training pipeline's original
1-minute-grid definition. Two consecutive 30s bins in the same minute get an identical value.
Don't "fix" this without retraining — the model learned against this exact quantization.

## Offline backtest results (2026-08-20, WeatherML `SOLARCHARGE_EXPERIMENT_LOG.md` Run 4)

Backtested against `solar_charge_control.excess_solar_watts` — the real controller's own
historically logged predictions — on 65,305 daytime rows (2026-07-01 → 2026-08-20, 99.9% of that
validation window):

| | MAE |
|---|---|
| Naive persistence (assume no change in 5 min) | 408.6 W |
| **Heuristic** (`solar_charge_controller.py`'s current logic) | 384.9 W |
| **This model** | **363.4 W** (−5.6% vs. heuristic, −11.1% vs. persistence) |

For context, typical daytime excess magnitude is ~600-3000W (25th-75th percentile) — a real but
modest edge, not a transformative one. **Offline only.** Live validation via
`model_shadow_logger.py` is the current, in-progress next step before any deployment decision.

## Using the model — `model_shadow_logger.py`

Loads `model_run4.joblib`, reproduces the feature engineering above from live InfluxDB data (30s
`GROUP BY time()` bins over a trailing ~35-minute window), and logs `solar_charge_shadow` to
InfluxDB every `--check-interval` seconds (default 60) with both the model's and the real
controller's predicted excess watts / target amperage side by side. Read-only against InfluxDB;
never calls the ChargePoint API; cannot affect real charging. See its module docstring and
`etc/systemd/system/model_shadow_logger.service` / `etc/default/model_shadow_logger` for the
same install pattern as the other two services (`README.md`'s Systemd section).

**Known simplification**: the derived `model_target_amperage` field applies the heuristic's
"stricter minimum-start threshold" rule unconditionally (the real controller only applies it when
*not already charging* — `current_charging_watts == 0` — a distinction the shadow logger can't
make without session state). This only affects that one derived field, not the primary
`model_excess_watts` vs. `heuristic_excess_watts` comparison, which is the metric that matters for
validating the model itself.

**Feature engineering here must be kept in sync with WeatherML's
`workspace/SolarChargeML/train_run4.py`/`export_and_join.py`** if either changes (there's no
shared import between the two repos — this is a maintained duplication, not a real dependency).

## Loading the model

```python
import joblib
bundle = joblib.load("model_run4.joblib")
model, features = bundle["model"], bundle["features"]
# X must have exactly these columns, in this order:
prediction_delta = model.predict(X[features])
```
