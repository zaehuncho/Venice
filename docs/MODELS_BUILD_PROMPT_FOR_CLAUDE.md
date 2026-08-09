# Orion — Build the 4 Timing Models to Their Fullest Extent (handoff brief)

You are Claude Code working in `C:\Users\Administrator\Desktop\NexusVision` (NexusVision/Orion).
This is a cold start — everything you need is below. Read it fully before touching code.

## What Orion is (60-second orientation)

Orion is an NBA 2K shot-timing assistant for PS5 Remote Play. A custom **chiaki-ng fork** streams
the game and exports each decoded frame over a named pipe. A **Python sidecar**
(`remote_play_orchestrator.py` + `native_orion/backend/autogreen_sidecar.py`, pinned to
`C:\Python314`) runs computer vision on the captured frame and emits telemetry over stdout. The
**native C++ engine** (`native_orion/src/AutomationEngine.cpp` + `OrionAppController.cpp`,
compiled into `RemotePlayCore.dll` / `OrionNative.exe`) is **authoritative for timing**: it owns the
release clock and submits the controller release via ViGEm and a pre-encryption input hook.

The user holds Square (or RS-up for Go-To); the bot owns only **when to release** so the on-screen
**shot meter** lands on its green tip. CV detects the meter (`meter_detector.py`, classical HSV/contour
+ a green-chevron fingerprint). The meter at live capture is **RED** (hue ~0/179) with a green tip,
thin (~1.3 px at 720p).

**Authoritative timing knobs live in `learning.json`** (per-type feedforward ms, latency ms, velocity
priors, contest nudges). Models feed/replace these signals — they do not bypass the native engine.

## The job

Take the four timing models from scaffolding/parked state to **production: finished, wired into the
live path, offline-validated against a frozen baseline, and behind an env/settings flag for safe live
A/B.** "Fullest extent" = each model is genuinely improving timing on held-out data AND is integrated so
the live engine can consume it — not a notebook artifact.

**Hard dependency / sequencing reality:** all four are gated on (a) trustworthy meter detection and (b)
clean labeled gameplay data. Detection is being fixed in a parallel effort — assume the committed
`meter_detector.py` on the current branch is the detection baseline; if your offline harness shows the
detector false-locking on your training clips, STOP and report it rather than training on garbage. Build
the data pipeline + model + integration + validation harness for each; where live data is thin, make the
model degrade safely to the current `learning.json` heuristic (never worse than today).

---

## Model #2 — Temporal release predictor (lookahead, latency-immune)

**What it is.** A sequence model over the meter/pose rise trajectory that predicts the release moment a
few frames *ahead*, so the open-loop fire is immune to observe→decide→actuate latency (reacting frame-by
-frame can't catch a ~400 ms meter — see the late-shot history).

**Current state (DON'T start from scratch).** Already scaffolded:
`tools/diagnostics/temporal_release_predict.py` (v1) and `temporal_release_predict_v2.py` (v2,
`ReleaseCNNv2` = 1D-CNN, BatchNorm, classification "is this the release frame?" over a 30-frame window of
wrist-Y features), trained weights `models/release_predictor.pt` / `release_predictor_v2.pt`. It is **NOT
wired into the live engine** (grep native: no `release_predictor` consumer).

**Build to fullest extent:**
- Re-train v2 (or a v3) on the full clip set, but switch/augment features from pose-only to **meter-fill
  trajectory** (fill% + velocity + green-window proximity) since the meter is the authoritative timing
  signal — pose is the no-meter fallback. Keep a pose-feature variant for no-meter mode.
- Predict **frames-to-tip** (regression, log-compressed horizon) AND emit a calibrated confidence; report
  per-type MAE in ms on held-out clips.
- **Integration target:** the sidecar runs inference and sends a `predTipMs`/`predConf` telemetry token;
  the native engine consumes it as the feedforward clock when confidence is high, else falls back to
  `shot_type_feedforward_ms` in `learning.json`. Gate behind `ORION_TEMPORAL_RELEASE` (default off).
- **Data:** the `CLIPS` list already in `temporal_release_predict_v2.py` + the user's `Videos\*.mp4`
  (e.g. `2026-06-25 21-49-04.mp4`). Label release frames from the meter green-window crossing.
- **Validation gate:** held-out per-type tip-MAE beats the current `learning.json` feedforward residuals;
  zero predictions that fire >X% below target fill (reachability guard).

## Model #3 — Self-measured latency estimator

**What it is.** Auto-measures Orion's true end-to-end latency (observe → decide → ViGEm/hook → on-screen
effect) so the feedforward clock self-calibrates instead of relying on a hand-tuned offset.

**Current state.** Partial: `learning.json` has `learned_latency_ms` (currently 100) and per-type
`shot_type_latency_ms`; the native post-release grader already nudges these from the settled meter. There
is no standalone *estimator* that isolates latency from lead error.

**Build to fullest extent:**
- Build an estimator that, per shot, decomposes the release-vs-green error into **(lead error)** and
  **(latency)** using the timestamped release event vs the meter's measured on-screen response delay
  (the "Release submit:" telemetry line carries `releaseSeq`; correlate with the next meter-fill
  inflection). Output a per-type latency distribution + a robust central estimate.
- Make it **online**: an exponentially-weighted update that the engine reads, with outlier rejection so a
  single bad grade can't swing it (the late-shot saga shows single grades are noisy).
- **Integration target:** writes `learned_latency_ms` + `shot_type_latency_ms` in `learning.json`
  (Python round-trip only — see hygiene rules); the native engine already consumes these. Add an offline
  replay tool that re-derives latency from a recorded batch so it's auditable.
- **Validation gate:** on a recorded batch, the estimator's latency explains the residual early/late bias
  (after removing it, per-type error centers near zero); compare to the current fixed 100 ms.

## Model #4 — Sub-pixel meter reading

**What it is.** Fill/tip estimation below whole-pixel resolution. The meter is ~1.3 px wide at 720p, so
integer-row fill snaps coarsely; sub-pixel reading makes fill rise smoothly 0→100 into the green tip and
sharpens the release trigger.

**Current state.** None — `meter_detector._measure_track` reads fill at integer rows today. This is the
only fully greenfield model.

**Build to fullest extent:**
- In `meter_detector.py`, add **sub-pixel fill estimation**: interpolate the fill-front row from the
  intensity/saturation gradient along the bar's vertical profile (centroid of the transition band), and
  do the same for the green-window top. Optionally a tiny learned 1-D refiner, but a principled
  gradient/centroid estimator likely suffices and is cheaper — prefer it.
- Produce a continuous `fill_pct` (float) + `tip_row` (float) with reduced frame-to-frame jitter; keep it
  monotonic into the tip via the existing green-memory / `_track_full_h`.
- **Integration target:** `DetectResult.fill_pct` already flows sidecar→native; just make it sub-pixel and
  confirm the engine's thresholds still behave. Gate behind `ORION_SUBPIXEL_FILL` (default off) so you can
  A/B against the integer reader.
- **Data:** any clip with a clean rising meter; validate against the meter's known geometry (fill should
  be linear in time during the rise).
- **Validation gate:** lower fill jitter (px/frame variance) and a smoother 0→100 curve than the integer
  reader on the same frames, with no loss of detection rate.

## Model #5 — Context awareness

**What it is.** Reads game context (shot type, contest level, defender proximity, dribble/shot phase) and
selects the right per-type clock/window instead of treating every shot identically.

**Current state.** Partial: `models/user_player_classifier.pt` (player lock, `train_user_classifier.py`),
`models/shot_start_classifier.pt` (shot-start), the `orion_player_detect_v9` / `orion_pose2k_*` detectors,
and `learning.json` already buckets by `shot_type_*` and contest `per_level` nudges (LIGHT…SMOTHERED).

**Build to fullest extent:**
- Build a **context classifier** that, at shot-start, emits: shot type (Standstill / L-R Fade / Post Fade
  / Go-To / No-Dip / tempo variants — match the existing `learning.json` keys exactly) and a **contest
  level** (map to the existing `per_level` buckets). Reuse pose + player-detect features; don't retrain
  what already works — wrap/ensemble.
- **Integration target:** the sidecar emits a `shotContext` token (type + contest); the native engine uses
  it to pick `shot_type_feedforward_ms` / `shot_type_latency_ms` / `per_level.nudge_pct`. Gate behind
  `ORION_CONTEXT_MODEL` (default off). Must degrade to the current heuristic classifier on low confidence.
- **Validation gate:** classification accuracy vs hand-labeled shot type/contest on held-out clips; and
  that routing through the predicted bucket doesn't regress timing vs the current static routing.

---

## Constraints & house rules (NON-NEGOTIABLE)

- **Python = `C:\Python314\python.exe`** (has cv2, torch, ultralytics, pytest). `cosmic_env` is dev-only;
  Orion must not depend on it.
- **NEVER edit `settings.json` with PowerShell** (PS5.1 writes a UTF-8 BOM → silent Python/Qt parse
  failure; it's Ed25519-signed). Python round-trip only, app closed. `learning.json` is plain JSON and
  safe to round-trip in Python.
- **Never blind `git add -A`.** Keep vault/settings/codesigning/license/keys/secrets out of git. Commit
  ONLY when the user asks. Don't push.
- **Telemetry log lines must be machine-parseable** `key=value`, no spaces inside values; add a NEW token,
  never reshape a line an existing parser reads.
- **Native engine stays authoritative for timing + ViGEm.** Models feed signals via telemetry tokens /
  `learning.json`; they do not release the trigger themselves and do not touch the encrypted Remote Play
  UDP.
- Every model ships **behind an env flag, default OFF**, with a safe fallback to today's behavior, so the
  user can live A/B without risk of regressing the ship build.
- Temp files → the scratchpad, not the repo. New training/diagnostic scripts go in `tools/diagnostics/`
  or `tools/training/` (where the existing ones live).

## Build / test / data

- **Sidecar/CV tests:** `C:\Python314\python.exe -m pytest tools -q` (and the model-specific scripts).
- **Native build:** `cmake --build native_orion/build --config Release --target OrionNative`
  (touch changed files first; ~7 s incremental). Tests: `OrionNativeTests.exe -o file.txt,txt` from repo
  root with `C:/Qt/6.8.0/msvc2022_64/bin` and the OpenCV bin dir on PATH (140 tests, keep them green).
- **Data:** `C:\Users\Administrator\Videos\*.mp4` (e.g. `2026-06-25 21-49-04.mp4`); the labeled
  `datasets/` folder; the `CLIPS` list inside `temporal_release_predict_v2.py`.
- **Existing weights:** `models/*.pt` (pose, player-detect v9, release_predictor v1/v2, shot_start,
  user_player). Reuse/extend; back up before overwriting (the repo already keeps `*_backup.pt`).

## Definition of done (report this back)

For EACH model: (1) what you built/changed, (2) the offline metric on held-out data vs the current
`learning.json` heuristic baseline (numbers, not vibes), (3) the integration point (telemetry token /
`learning.json` field) and its env flag, (4) the fallback path when the model is off or low-confidence,
(5) tests added (pytest/ctest) and their status. **Do not claim a live win** — offline-validate, then hand
the user a single combined live A/B build (all four behind their flags) per the user's directive:
"build it all into one live test."

If detection is not trustworthy on your training clips, or the data is too thin to validate a model, say
so explicitly and scope that model to "scaffold + safe-fallback wired, awaiting data" rather than
overclaiming.
