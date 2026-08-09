# Orion Code Notes

Organized engineering notes extracted from the heavy in-source commentary
(AutomationEngine, OrionAppController, remote_play_orchestrator, controller_remap,
meter_detector). Source files keep only short load-bearing constraints (telemetry
formats, sign conventions, safety gates); the narrative "why" lives here.

---

## 1. Timing architecture (who owns the clock)

- The **native C++ AutomationEngine** is authoritative for timing + ViGEm output in
  the live `OrionNative.exe`. The Python sidecar is launched with
  `virtual_controller=false` — detection/telemetry only. The Python `RemapEngine`
  is authoritative only in standalone Python mode.
- The engine runs a 250 Hz tick. Detection/decision stay on the tick; **only the
  fire moves**: `processHolding` commits a release deadline within a ≤6 ms horizon
  (`scheduleFire`) and `OrionPreciseFireThread` (cv-wait + spin, TIME_CRITICAL)
  submits the exact output at the deadline under `submitMutex_`. The engine
  confirms via `confirmScheduledFire` before the next `process()`; if no fire
  thread consumed it within an 8 ms grace, the in-tick fallback completes the
  release. Telemetry: the `Scheduled fire:` line (seq-paired) reports
  scheduled-vs-actual delta, wifi mode, jitter, held offset, clock-vision divergence.
- **Feedforward clocks**: fast meters (~400 ms Arrow2) cannot be timed reactively —
  observe→decide latency lands the release on the meter's recede. Each shot type
  owns a deterministic clock (anchor `meter_appear` or `hold_start`,
  `shotTypeFeedforwardMs` / `shotTypeMeterToReleaseMs`), seeded per type, learned
  from graded outcomes, persisted in learning.json.
- **Target**: the contest-invariant dead TOP (`bestEndPct_`). Every meter always
  has a green window at the tip; fill to the top and time the peak ⇒ greens
  regardless of contest. A contested fade's window is a ~2–3 % sliver vs
  standstill's ~10 %, so precision (not target choice) is the limiter.

## 2. Calibration (autonomous ACQUIRE → LOCK)

- Per-type phases: ACQUIRE (3 banner-confirmed greens ⇒ LOCK) → LOCK (±4 ms
  micro-trim only; 3 misses ⇒ UNLOCK). Phase + clock persist in learning.json.
- **Ground truth** = the on-screen TIMING banner (`banner_calibration=true`
  default). The post-release meter self-grade false-LATEs at the dead top (an
  EXCELLENT and a LATE both peak ~100 then recede), so the banner VETOES it. If
  the banner stays silent for `bannerUnclearFallbackShots` (3) graded shots, the
  engine auto-falls back to the peak-gated meter self-grade
  (`evaluatePostReleaseMeter`): settled-below-green + peak-reached-green ⇒ LATE
  (`meterRecedeLatePct`), grades only after the meter SETTLES (static bbox run +
  fill-stable tail) so a moving fade is never graded mid-flight.
- Auto-recalibration triggers (silent drop to ACQUIRE, clocks kept as warm
  starts): meter style/color change, stream start, divergence-guard trip
  (`calDivergenceGuardShots` — same-signed residuals without greening ⇒ the clock
  ran away).
- The old global `early_late_offset_ms` knob is retired: folded once into the
  per-type learned offsets at load, then zeroed in both files in the same pass.

## 3. Network sync (drift-proof, wifi-robust)

- Python `rtt_sync_engine.py` (Kalman + EMA) feeds `predicted_offset_ms`
  (forward-looking: predicted_half + jitter margin + tick advance + court bias +
  decode comp) and a live decode comp from the frame-age EMA
  (`observe_frame_age_ms`, replaces the fixed 7.5 ms). Ping interval tightens
  400→150 ms for 2.5 s around meter activity (`notify_meter_active`).
- Native side (`networkAutomationOffset` → `effectiveLatency`): **sample-and-hold
  per shot** — the offset is frozen at hold start; a mid-hold spike can never move
  a committed shot. On top: a delta clamp vs the LOCK-time RTT baseline
  (`shotTypeRttBaselineMs`, ±25 ms wired / ±12 ms wifi) so a locked clock follows
  network drift without relearning.
- **Auto wifi mode**: jitter EMA ≥ 4 ms (`wifiJitterThresholdMs`) ⇒ tighter
  vision-freshness gating (×0.6), tighter delta clamp. No user toggle.
- `align_release()` in controller_remap.py is **live code** on the phase-locked
  standalone path (an earlier plan mislabeled it dead).

## 4. Vision / meter detection

- Arrow2/Purple meter: vertical bar, magenta fill rises to a GREEN chevron at the
  top (~93–100 %), FAST (~0.18 %/ms ⇒ green ≈ 30–40 ms in time), then recedes.
  The fill reading is ACCURATE (the old "52 % false-lock" was a real post-shot
  deflate of a LATE shot, not a detector bug).
- Hue/sat purity gate in `_color_detect` rejects the pink FLOATIE (masked-median
  Hue>159 or Sat<165; the meter is Hue≈150/Sat≈203). Arrow2 Purple is
  UI-authoritative — ignore stamina bar/floatie/body.
- `_StabilityValidator` uses a tracking latch: once acquired it FOLLOWS bbox
  motion (width-scaled jump gate) instead of hard-resetting — a moving/filling
  meter must keep feeding the engine (the old reset caused feed starvation ⇒
  timeout_fallback).
- The meter bbox must stay wired sidecar→native (`RemotePlaySession` x/y/w/h):
  the meter-settle grader needs it to detect the static run.
- TemporalSampler: linear + quadratic crossing predictor; when deceleration is
  detected the tip ETA uses the fitted vertex (stops over-lead on decelerating
  meters). Per-type velocity priors (`shotTypeVelocityPriorPctMs`, learning.json)
  sanity-band live estimates to [×0.5, ×1.6] — rejects detector blips (the wifi
  case). When clock and vision disagree beyond a band, the banner-calibrated
  clock wins; the divergence is logged (`clockVisionDivMs`) for the learner.

## 5. Input path

- Physical DualSense is read via **native Raw Input HID** (never XInput), on a
  dedicated input thread (`OrionRawInputWorker`: message-only HWND,
  RIDEV_INPUTSINK). The GUI-thread WM_INPUT path starved to ~30 msg/s under
  decoder-frame load vs the pad's ~250/s — the historical "slow-motion stick" lag.
- **Trigger model**: the user holds Square / RS-up; the bot owns timing + release
  + submit only. Engine output continuity contract (test-guarded): X held on
  EVERY tick of an owned hold; exactly one release edge per shot; Go-To pins RS
  every hold tick and X never pulses.
- Output goes to ViGEm every tick (fallback) and, with `ORION_INPUT_HOOK=1`, to
  the patched chiaki's pre-encryption pipe (`OrionInputClient` →
  `orioninputbridge` in the fork) with CONTINUOUS ownership — the patched client
  does not pump SDL, the pipe is the sole live input. The bridge coalesces to
  latest-wins and injects via `chiaki_session_set_controller_state`.
- WinMM joystick polling is gated (only when no RawInput/physical-XInput live,
  2 s device-change cooldown, ~1/s throttle): unconditional per-tick polling
  caused the recurring c0000374 heap crash during device churn. HidHide cloaking
  is unsupported (hides the pad from Orion too ⇒ WinMM fallback ⇒ crash).
- Defense Mode: a single arm gate at the top of `AutomationEngine::process()` —
  disarmed = verbatim pass-through; a mid-flight shot is reset once. The gate is
  the same mechanism safe mode uses (`syncEngineArmed`).

## 6. Stream / capture

- The custom fork (OrionStream.exe, branch `orion` of chiaki-ng) exports each
  decoded frame over the `OrionFrameExport` named pipe (latest-wins single slot,
  ~30 fps cap, ABOVE_NORMAL priority) and runs the input bridge TIME_CRITICAL.
  Embedded mode (either Orion pipe env var set) forces Vulkan + zero-copy + half
  audio buffer, immune to stale registry values.
- GDI window capture is the fallback; it is embed-bound (~1550×697 max) — the
  Main window is fixed 1860×1000 and the Remote Play page goes full-bleed while
  streaming so the embed reaches ≥1280×720. Minimizing Orion blacks GL capture.
- Frame-age climbing was fixed by the purple-stain decoder rewind; capture tiers
  handle a black GDI grab under a Vulkan swapchain.

## 7. Telemetry & log contract

- Log lines are machine-parseable `key=value`, no spaces inside values
  (`telemetryToken`/`holdStateToken`); released shots pair by `seq=`. NEVER edit
  an existing parsed line — add a new one (tools: correlate_releases.py,
  release_action_timeline.py, extract_shot_banners.py).
- Timezones: orion_native.log is UTC (Z); recording filenames are local
  (America/Chicago, UTC-5 in DST) ⇒ video_start_utc = local + 5 h.

## 8. Update / security

- `/api/update` Ed25519 manifest contract: docs/UPDATER_CLIENT.md. The canonical
  signing string is a compact JSON of exactly 8 fields in fixed order; `channel`
  is deliberately NOT signed (ring routing is server-side). Gate policy is
  `evaluateUpdateGate()` (pure, tested).
- Updater hard rules: HTTPS only, signature must verify against the pinned
  public key, sha256 must match, downgrades need `allow_rollback`, zip entries
  must stay inside the install dir, rollback on any apply failure.
- Diagnostics exports pass `redactDiagnosticsText` (license keys → support
  suffix, tokens/JWTs/machine ids/emails stripped); crash dumps ship as metadata
  only (raw memory cannot be text-redacted).

## 9. Watchdogs / safe mode

- Any watchdog trip: neutral-submit + engine disarm FIRST, then ONE recovery
  (sidecar restart); a second trip in a 5-minute window ⇒ safe mode (automation
  parked until the user exits). GUI-freeze watchdog runs on its own thread and
  acts directly (same `submitMutex_` discipline as the fire thread).
- Removed (do not resurrect): Quit/Lag-All/Lag-Opponents DoS features; the
  on-screen HUD closed-loop reader (`hud_feedback.py`) — the banner reader that
  replaced it is calibration-only and never drives live timing.
