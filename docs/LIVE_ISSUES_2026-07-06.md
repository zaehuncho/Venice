# Orion — Live-batch issue log (2026-07-06, capture-card path)

Live batch with the 95% stack correctly engaged (`Timing mode: autonomous=1 shadow=0`, RawInput route, `ORION_MEASURED_LEAD/REG_FUSION/LEAD_VISION_GATE=1`, measured-latency oracle enabled). Result: **0 greens**, glitchy meter detection, and a **stuttering/delayed capture feed**. Below are the observed issues, each grounded in `logs/orion_native.log` evidence, plus the likely dependency chain. Hand to external AIs / agents for concrete diagnosis + fixes.

## VISUAL TARGET — flawless vs. failure (the bar to clear)
- **FLAWLESS** (reference `C:\Users\Administrator\Videos\RED_PARK_NEON.mp4`): the magenta detection box sits **tight and stable on the meter through the entire shot**; telemetry is **sane** — `FILL 0–100%`, state `RISING`/`FULL`, `RISE` = *real velocity* (e.g. 3.1, −0.2), `HOLD` = *real seconds* (e.g. 0.13s, 2.40s); and when no shot is active it cleanly shows **"(no meter)"** — no box, no false lock on décor.
- **FAILURE** (`C:\Users\Administrator\Videos\Screen Recordings\Screen Recording 2026-07-06 045234.mp4` + this live batch): the box **drifts off / disappears during the shot**; `RISE` reads **0.0** (dead velocity); `HOLD` reads **nonsensical (293s)**; between shots it serves a **frozen echo / stale "idle_overlay"** instead of "(no meter)"; and it **false-locks on red court décor** (2K wall logo, jerseys). **Live detection must always look like RED_PARK_NEON, never like the glitchy recording.**

## The likely ROOT-CAUSE CHAIN (fix top-down)
**Capture instability → stale/glitchy detection → the latency oracle can't measure → timing fires on the wrong clock → 0 greens.** The capture feed is the upstream suspect; downstream fixes can't work on corrupted frames.

---

## A — CAPTURE / DISPLAY PIPELINE (new, likely the root)
- **A1 — Capture-card feed stutters + degrades live.** `health: tier=capture_card uniqfps=60` then later **`uniqfps=37`**; `WARNING: Capture degraded: 10 consecutive bad/black frames (black content / window hidden or GPU surface lost)`; `capture_card_backend: Capture-card capture stopped` mid-session. User observes stutter + perceptible delay in the Live Capture panel. Elgato HD60 X, MJPG 1080p60 expected.
- **A2 — Is the displayed feed the raw capture, or a re-rendered/delayed copy?** `ORION_QML_RENDER=1` — the preview is drawn through QML; display latency may be *additional* to the detection feed. User specifically asks whether the capture-card capture is what's shown.
- **A3 — Black-frame guard fired ("window hidden or GPU surface lost").** Is the PS5 HDMI output actually dropping (HDCP / game pause / resolution renegotiation), or is the capture pipeline losing its GPU surface / the MJPG device wedging (see `capture_card_backend.is_healthy` stall logic)? `core_black_run` / `core_static_run` counters exist — need them read out during a stutter.
- **A4 — `cv_fps=0` / `export_fps=0` / `detect_ms=0.0` on the native health line** while `uniqfps=60`. Likely a metric-wiring artifact (CV runs in the sidecar, not reported on the native health line) — but must be confirmed it isn't a real detector stall. Questions: what actual frame rate + frame-age does the *detector* see, and does it drop during the A1 stutter?

## B — METER DETECTION (still glitchy live)
- **B1 — Serves stale echoes instead of fresh reads.** `Detection presence: idle_overlay (fill 71.5% conf 0.91 ...)` and `Detection presence: rejected (fill 0.0% conf 0.00 ...)` interleaved — the detector is flickering between a stale/idle overlay and outright rejection rather than tracking the live rising meter.
- **B2 — The offline detection fixes (hold-through-release, served-fill sub-pixel, the 2 bug fixes in commit 14fb2916) don't visibly cure the *live* glitch.** Either the live capture corruption (A) defeats them, or there's a live-only failure mode the offline replay didn't reproduce. Needs live framedump analysis (a real-shot dump exists this session: `logs/diagnostics/framedump/session_20260706_162326`).
- **B3 — Detection quality is gated by capture quality.** Stale/black/dropped frames from A → the detector has nothing fresh to serve → idle_overlay/rejected. Fixing A is likely a prerequisite for B.

## C — TIMING / 0 GREENS
- **C1 — The measured-latency oracle is ENABLED but never produced/fed a number.** Releases fire on `learnedOffset ~8–15ms` + a `learned 219ms meter-appear clock`, NOT the measured ~83ms. Hypothesis: the frozen-meter oracle needs a clean post-release meter read to invert; glitchy detection (B) denies it that, so `measured_latency_ms` never converges. The single biggest 95% lever therefore never engaged. **Confirm: is `measured_latency_ms` in the sidecar payload ever > 0 this session?**
- **C2 — Feedforward preempts the measured-lead/vision stack for FAST meters.** `seq=15 Standstill: code=feedforward_target ("fast meter; view-latency-immune")` — the per-type feedforward clock overrides the autonomous global-lead path for the most common shot type, so the measured lead + registration never apply there. (This is the known "feedforward preempts vision" issue; unify as prior→posterior.)
- **C3 — Blind fire on no detection.** `seq=18: Release issued fill 0.0% target 99.7% ... presence=rejected conf=0.00` — the engine fired with zero valid fill/detection. The P0.1 loud-unsubmitted guard covers *disconnected*, not *detection-rejected* blind fires — a rejected-detection release should likely also be suppressed or flagged.
- **C4 — LATE-66 / EARLY-90 grader artifacts persist** in `Shot outcome` verdicts (`errorMs=66.0`, `errorMs=-90.0`). These are the known unreliable meter self-grader; confirm the vision-gate is actually preventing them from moving the autonomous lead (learning.json drifted to `learned_latency_ms=-11.4` this session — is that the grader leaking past the gate?).
- **C5 — 0 greens (user ground truth).** With C1–C3, expected: the stack that should hit the tip isn't the one pulling the trigger, and it's timing off corrupted fill.

## D — Cross-cutting questions for the diagnosers
1. Is the capture-card degradation (A1/A3) a hardware/HDCP/USB-bandwidth issue, a device-wedge (MJPG buffer), or a software pipeline/GPU-surface issue? What log/telemetry pins it?
2. Does fixing capture stability restore fresh detection (B) — i.e., is A the root of B?
3. With fresh detection, does the measured-latency oracle (C1) converge and start feeding the lead? Verify the oracle's frozen-meter read requirements.
4. How to make the measured-lead/registration stack apply to fast meters too (C2) — prior→posterior blend instead of feedforward preempt?
5. Should a `presence=rejected` release be suppressed (C3)?

## Artifacts for analysis
- `logs/orion_native.log` (this session ~21:23–21:29 UTC / 16:23–16:29 local)
- `logs/diagnostics/framedump/session_20260706_162326` (raw frames, real shots)
- `logs/diagnostics/detframes_*.csv`, the oracle-join tool `tools/timing/oracle_join.py`, `tools/timing/live_batch_report.py`, `tools/diagnostics/meter_stability_report.py`
- Backlogs already written: `docs/TIP_TIMING_BACKLOG.md`, `docs/DETECTION_BACKLOG.md`, `docs/CHIAKI_STREAM_BACKLOG.md`

---
## CORRECTED DIAGNOSIS (capture deep-dive — supersedes the "capture is the root" hypothesis)
Standalone Elgato probe = healthy **60 fps, sub-ms jitter**. NOT a device/HDCP/USB problem. Real roots:
- **RC-1 (stutter/delay) = SIDECAR RESTART LOOP.** 4 teardown/respawns in ~6 min (one crash, code=62097), each a **15–21 s freeze**; capture was 60 fps otherwise. Trigger: dual-mode env (`ORION_CAPTURE_CARD=1` AND `ORION_FRAME_PIPE=1` together) + native gating session health on the Chiaki *stream window* in cc-mode. Fix: cc-mode without frame-pipe; don't gate health/reconnect on the Chiaki window in cc-mode; keep the sidecar alive across chiaki relaunches; fix the 62097 crash. (`SidecarWatchdog.h`, `RemotePlaySession.cpp:1462-1499/1660-1750`, `remote_play_orchestrator.py:686-722`)
- **RC-2 (glitch) = detector CPU-bound ~110 ms/frame → ~8 fps** vs 60 fps feed (classical CV masks are CPU on the standard OpenCV wheel; GPU locator "ready" never logged). Coarse subsampling → stale/`RISE 0.0`/rejected. Fix: GPU the CV (OpenCV-CUDA) / lighten / ROI-only / confirm locator load → `cv_fps→~60`. **The direct lever for RED_PARK_NEON-flawless detection.**
- **RC-3 (0 greens / oracle) = `release_marker` never pushed native→python** → the frozen-meter latency oracle never gets a release timestamp → never measures → timing on old clocks. Also capture-card frames carry `pts=0` → frame-age under-counts device latency. Fix: native pushes `release_marker` per release; stamp capture frames with a device-referenced timestamp.
- **RC-4 = LATE-66 grader leaks past the vision-gate** (nudges learned lead). And the `(window hidden / GPU surface lost)` warning is MISAPPLIED boilerplate on the capture-card tier; the 60→37 dip was real black frames (PS5 HDMI blip), not a pipeline fault.
- Preview vs detection: SAME frames, two rates — preview runs full 60 fps except during the RC-1 freezes; the detector just subsamples. Not a divergent/laggy display pipeline.
