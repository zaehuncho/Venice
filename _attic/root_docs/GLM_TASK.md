# GLM_TASK.md — No-meter (SKELE) pose shot-timing track

> Separate track from `TASK.md` (the Claude+Codex meter loop). GLM owns the pose
> model + this file. Branch off commit `08e4546` on
> `feat/detection-template-anchor-qml-render`.

## What just landed (commit 08e4546) — read before doing anything

- **TIME-BASE FIX (the key unblock).** `pose_timing.py` `update()` now takes an optional
  `frame_time`; `tools/diagnostics/eval_skele_pipeline.py` passes `seq/fps`. The offline
  eval is now **deterministic** and runs in true video-time. Previously it processed at
  ~11fps while treating 60fps frames as 11fps-spaced, distorting every velocity and making
  results non-reproducible — and actively *degrading* detection (correct smoothing alone
  cut false pushes sharply). The **live path is unchanged** (no `frame_time` → `perf_counter`).
- **RE-BASELINE FIRST.** The old numbers (V1 65 pushes / 291ms STD, V2 41 / 8–50ms) were on
  the broken time-base and are **invalid**. Re-run the eval for true baselines before judging
  anything.
- **Two shot gates exist, both DEFAULT-OFF, env-toggleable.** The eval is deterministic now,
  so A/B by setting the env var on the same window:
  - `ORION_POSE_REQUIRE_GATHER=1` → require a hip/knee crouch (gather) before a push.
  - `ORION_POSE_APPEARANCE_VETO=1` → jersey/appearance veto on the player lock.
- **Findings (do NOT redo):**
  - Appearance veto = **INERT** — redundant with `pose_timing.py`'s temporal-jump guard;
    brittle mid-animation (84–90% over-veto). Clean A/B: V1 45→46, V2 29→30 pushes.
  - Gather-gate = **PARTIAL** — matched A/B cut raw pushes 75→55 but cost a real shot
    (5/6→4/6) and barely moved STD. Both kept env-gated for you to tune.
- **Bug fixes in this commit:** degenerate edge-crop → `cv2.resize` crash (would crash live);
  `_compute_appearance_hist` conf-index `IndexError`. Tests: `tests/test_pose_timing_appearance.py`
  (5, green).

## Where the real problem is

Push-detection **precision** (the STD wall) + the large false-push rate — **not** player-lock
(lock-on-shot-frames was already 96%). STD is outlier-dominated at n=6, so *more shots* matter
as much as better detection.

## Your tasks

1. **Retrain `models/orion_player_detect_v9.pt` on SHOT-ANIMATION frames** so v9 stops dropping
   out mid-shot (that fallback is what feeds the false pushes). **Keep class indices identical:
   `0=Player, 1=Stamina, 2=Basketball`** — `pose_timing._detect_player_v9` depends on them. Don't
   edit `pose_timing.py`'s live logic without a test.
2. **Find 2–3 more meter-dense clips** via `tools/diagnostics/find_shot_windows.py` over
   `C:\Users\Administrator\Videos`, reported as `VIDEO COUNT START handedness`, like:
   - V1: `"NBA 2K26_20260324195410.mp4" 3000 3760 Right`
   - V2: `"NBA 2K26_20260618064057.mp4" 2500 1000 Right`

## Run / guardrails

```
C:\Python314\python.exe tools/diagnostics/eval_skele_pipeline.py VIDEO COUNT START HANDED
```

- Sidecar Python = `C:\Python314` (cv2 + ultralytics + torch).
- Never commit `settings.json` / `learning.json` / keys / `EXPLOITS*`. See `AGENT_RULES.md`.
- The Arrow2 Purple meter is a **VERTICAL** bar — don't touch detector geometry.
- Work on a branch off `08e4546`.
