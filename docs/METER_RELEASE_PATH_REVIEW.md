# Meter release-path review (pre-batch, 2026-06-20)

Read-only trace of `AutomationEngine.cpp` release decision under the shipped settings
(`autonomous_vision=False`, `freeze_calibration=True`, tempo off). No behavior changed.

## Verdict: the release path is in excellent shape
No logic bugs found. The code is mature, defensively guarded, and **fully instrumented** — every
release path and every waiting frame emits a distinct `code=`, so the live batch will be completely
diagnosable. All prior live-batch learnings are baked in as guards.

## Confirmed-correct
- **Path selection** (in order): commit-wait → feedforward clock (fast types) → meter-gate/abort →
  green-window → trajectory-tip → ETA → reactive → timeout/ceiling/hard-cap. Each path is gated so a
  good earlier path isn't preempted by a safety.
- **`predictCrossingMs`**: exp-weighted LS linear fit + quadratic (adopted only if it explains the
  decelerating top materially better). Clean; matches the Python predictor.
- **`effectiveLatency`** stack: `controllerChainMs + remotePlayPipelineMs + |networkOffset| +
  earlyLateOffsetMs + perTypeOffset + clamp(frameAge)`. No double-counting; per-type offset is the
  frozen dialed term under the shipped config.
- **Safeties**: `timeout_fallback` / max-hold ceiling / hard cap all exclude the armed fast clock
  (`fastTypeClockOwns`) and are guarded by `recoverableOrRising` + `nonGotoMinReleaseFillPct` so they
  can't blind-dump a recoverable/low-fill meter (the 49%-Left-Fade-dump class is fixed).
- **Phantom-at-target guard**: reactive requires `minFreshFillPct <= target - reactiveRiseMargin`
  (the meter genuinely rose this shot) → `phantom_target` code when suppressed. Go-To excluded from
  reactive + max-hold (forced-late worse than none).
- **Telemetry**: waiting frames overwrite the side-effect release code with honest waiting codes
  (`stale_sample`, `confidence_low`, `out_of_reach`, `phantom_target`, `eta_not_ready`) — no false
  "a path fired" on a holding frame.

## Watch items for the batch (leading hypotheses, not bugs)
These are where *timing accuracy* (an empirical property of the live feed/latency) could land off —
each with the signature to confirm/refute:
1. **Frozen per-type clocks mis-dialed.** Fast types fire on `shotTypeMeterToReleaseMs` as
   `feedforward_target`. If a type lands consistently early/late at that code, the seed offset is off.
   → **Fix without rebuild:** the one global `early_late_offset_ms` (late→raise, early→lower).
2. **`effectiveLatency` global bias.** If ALL types lean the same direction after convergence, the
   pipeline/controller latency estimate is off → same `early_late_offset_ms` nudge.
3. **Feed starvation.** If releases come via `timeout_fallback` / `out_of_reach` / `stale_sample`
   instead of `feedforward_target` / `green_confirmed` / `predictive_target`, the detector is
   under-feeding (frozen/sparse feed), not a timing bug — check the detector fps + frame-age line.
4. **Go-To aborts.** Go-To times vision-only; if its meter is sparse it aborts (no blind shot). A run
   of `stale_detection_abort` / `await_meter` = the Go-To meter isn't being detected, not mistiming.

## Pre-batch success signature
Every type lands at the tip via **`feedforward_target` / `green_confirmed` / `predictive_target`**,
NOT `timeout_fallback` / `phantom_target` / `out_of_reach`. A standing early/late bias after that is a
single `early_late_offset_ms` adjustment — never a per-type slider. Pair each release `code=` with its
post-release verdict (`evaluatePostReleaseMeter`) and, if available, the OBS TIMING-HUD frame.
