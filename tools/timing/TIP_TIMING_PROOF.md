# Autonomous tip-timing — offline proof (2026-07-05)

**Objective (user directive):** the bot must be completely autonomous — *no seeds, no hardcoded timing, no
green window*. It should see the meter, track the rising fill, predict when it reaches the **tip** (top of the
bar, ~100% fill), fire early by a **self-measured** latency, and hit the tip. This validates that architecture
on real data **before** any engine change.

## What was built
- `tools/timing/tip_predictor_eval.py` — the proof harness (reuses `fill_forecaster.py` shot extraction +
  `fill_kalman.py`). Three pure-vision predictors, all reading ONLY the live fill curve:
  - **KALMAN** — `FillKalman.ms_to_target(tip)`: analytical const-accel solve for ms-to-tip.
  - **FORECASTER** — `models/fill_forecaster` 1D-CNN frames-to-tip (retrained on the GPU over the full corpus).
  - **FUSION** — Kalman near the tip (const-accel most accurate there), forecaster earlier.
- `tools/timing/tip_plots.py` — `tip_timing_proof.png` (4-panel visual).
- Retrained the forecaster on the RTX 3060 (CUDA) over **179 real shots** from 11 live sessions
  (`detframes*.csv`); baseline preserved at `models/fill_forecaster/fill_forecaster_baseline_jun30.pt`.

## Data
179 real shots, 2,488 samples, peak-fill median 100%, rise-duration median 383 ms. Ground-truth tip = the
per-shot peak-fill frame, read straight from each recording's future frames (self-supervised, no labels).

## Results (all on real shots)

**(A) Open-loop prediction is accurate near the tip.** Forecaster/Fusion IQR ≈ 25–33 ms at 1–2 frames
(17–33 ms) before the tip; degrades to ~40–58 ms IQR by 5 frames (83 ms) out — the meter's variable
deceleration is genuinely harder to predict further ahead.

**(B) Achievable make-rate is LATENCY-LIMITED** (at the correctly-learned lead; FUSION):

| end-to-end latency | perfect-tip (±20 ms) | good (±30 ms) | oracle (±20 ms) |
|---|---|---|---|
| 50 ms | **55%** | 55% | ~100% |
| 70 ms | 39% | 54% | ~100% |
| 90 ms | 23% | 45% | ~100% |
| 110 ms | 18% | 31% | ~100% |

The **oracle is ~100%** — a perfect fire frame always exists in the data, so this is a prediction+latency
problem, not a data problem. Halving latency roughly doubles the perfect-tip rate.

**(C) Self-correction works with no hardcoded value.** A damped online loop, starting `lead_est = 0`, learns
the firing lead from shot outcomes on a real 68-shot session: it climbs 0 → ~60 ms (70 ms latency) / ~89 ms
(90 ms latency) and settles there (= true latency + predictor bias, folded into one learned number). From a
zero start it takes ~15 shots; **seeded by a boot latency probe it would converge in 2–3.**

## Honest conclusions (what this means for the rewire)
1. **The architecture is correct and proven**: pure-vision tip prediction (no green, no per-type seeds) +
   a self-tuning lead that learns one number. Promote **FUSION** as the release driver.
2. **Accuracy is bounded by end-to-end latency and frame-rate, not by cleverness in the timing math.** The
   biggest levers, in order:
   - **Cut end-to-end latency** — the pre-encryption input hook (`ORION_INPUT_HOOK`) and capture path. 50 ms
     vs 110 ms is 55% vs 18% perfect-tip.
   - **120 fps detection** — landing errors cluster at ±16.7 ms (one 60 fps frame); 120 fps halves that
     quantization directly. (Ties P2 timing to P3 capture.)
   - **Sub-pixel fill reader** (`ORION_METER_READER`, model not yet trained) — cleaner fill → cleaner
     velocity → tighter prediction.
3. **These offline numbers are a CONSERVATIVE FLOOR** — row-quantized fill at 60 fps with real detection gaps.
   The live low-latency path (input hook + 120 fps + sub-pixel) should exceed them.
4. **"Hit the tip every time" is not achievable at 90–110 ms latency** with any predictor — this is the honest
   headline. It becomes realistic only by driving latency down. The make window also scales with the in-game
   shot rating, so "good (±30 ms)" releases convert at a high rate for a well-rated build.

## Reproduce
```
C:\Python314\python.exe tools/timing/tip_predictor_eval.py         # numbers
C:\Python314\python.exe tools/timing/tip_plots.py                  # tip_timing_proof.png
C:\Python314\python.exe tools/diagnostics/fill_forecaster.py train # retrain forecaster (GPU)
```
