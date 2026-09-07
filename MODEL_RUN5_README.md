# model_run5.joblib — SolarChargeML excess-solar predictor (amp/dollar-optimized)

Supersedes `model_run4.joblib` (see [MODEL_RUN4_README.md](MODEL_RUN4_README.md) for that
model). Same prediction target, same 30 features, same architecture family — the difference is
the **loss function**, chosen after `model_run4`'s own live shadow data revealed that watts MAE
was the wrong thing to optimize for this problem. Trained in the
[WeatherML](https://github.com/dacarson/WeatherML) repo's `workspace/SolarChargeML/` — see
`SOLARCHARGE_EXPERIMENT_LOG.md` there (Runs 5a-5c) for the full investigation this summarizes.

**Currently shadow-mode only** — `model_shadow_logger.py` logs what this model would
predict/set alongside the real controller's own values. It does not control charging.

## Why this model exists: watts MAE was the wrong target

`model_run4` beat the heuristic by 9-17% on watts MAE over ~17 days of live shadow data — but
`solar_charge_controller.py`'s `determine_target_amperage()` rounds continuous watts into
~240W-wide integer-amp buckets, so most of that improvement fell inside a bucket and never
changed the actual amp decision. Measured against a perfect-hindsight "ideal amp," the real edge
was only +3 percentage points of exact-match and ~13% relative reduction in mean amp error —
and in dollars (PG&E NEM 3.0 marginal rates), only about **$4.60/month**, almost entirely in the
`off_peak` TOU period.

**From `model_run5` onward, the metric that matters is amp-decision accuracy and simulated
dollar cost — not watts MAE**, which is now reported only for continuity/context.

## What changed from model_run4

Everything is identical (same 30 features in the same order, same
`HistGradientBoostingRegressor` family, same daytime-only/delta-target setup) **except the
loss function**: `loss="quantile", quantile=0.3` instead of the default `squared_error`.

**Why quantile, and why 0.3**: under NEM 3.0, over-predicting excess (charging too much) pulls
the shortfall from the grid at the full import rate; under-predicting only forgoes the much
smaller export credit — asymmetric costs that squared-error loss doesn't account for.
`quantile=0.3` biases predictions below the median. A second, initially surprising mechanism
turned out to matter even more: `determine_target_amperage()` always rounds **up**, so for any
true excess in `(-500W, 1800W)` — 40.6% of off-peak rows in the backtest — the policy floors to
a flat 8A (1920W) regardless of how close to zero the real excess is, forcing needless grid
import by the existing policy's own design. A downward-biased forecast partially corrects for
that rounding inefficiency, on top of the rate-asymmetry effect.

A quantile sweep from 0.10 to 0.50 found `q=0.3` is the only point that wins decisively on
dollars while **not** increasing stop/start cycling above the heuristic's own rate — lower
quantiles kept looking better on the dollar simulation alone (monotonically, no local optimum)
but did so partly by refusing to charge so often that oscillation exceeded the heuristic and
amp-accuracy degraded; `q=0.3` was the cutoff before that started happening. See the experiment
log for the full guardrail analysis.

## Offline backtest results (2026-09-06, 65,271-row backtest, 2026-07-17 → 2026-09-07)

| | Watts MAE | $/month vs. heuristic | Off-peak stop/start (flips/day) |
|---|---|---|---|
| heuristic (current) | 387.0 W | — | 4.46 |
| model_run4 | 362.3 W | +1.90 | 3.15 |
| **model_run5 (this model)** | 383.4 W | **+6.33** | 4.36 |
| perfect hindsight ("ideal", same policy) | 0.0 W | +3.96 | 6.51 |

Note `model_run5` has a *worse* watts MAE than `model_run4` — the headline finding of this
investigation: once amp-bucket rounding and asymmetric rates are in the loop, watts MAE and
dollar impact aren't just imperfectly correlated, they can point in opposite directions. The
"ideal" row is not the dollar-optimal achievable outcome, just the best the *existing*
round-up-always policy can do with perfect information — `model_run5` beats it precisely by not
being perfectly accurate in the direction the policy is structurally wasteful.

**Offline only, same as model_run4's own history.** This needs live validation across 3+
independent windows (per this project's own established convention — `model_run4`'s own $4.60/mo
live figure was smaller than its own offline "ideal" ceiling would suggest, for reasons discussed
in the experiment log) before drawing further conclusions.

## Using the model — `model_shadow_logger.py`

No changes needed beyond pointing at the new file — same feature list/order, same joblib bundle
shape (`{"model", "features"}`). `model_shadow_logger.py`'s `--model-path` now defaults to
`model_run5.joblib`.

## Loading the model

```python
import joblib
bundle = joblib.load("model_run5.joblib")
model, features = bundle["model"], bundle["features"]
# X must have exactly these columns, in this order:
prediction_delta = model.predict(X[features])
```
