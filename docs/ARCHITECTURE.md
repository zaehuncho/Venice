# Orion Architecture — how the bot works end-to-end

*Audience: a developer picking up the codebase. Grounded in the code as of 2026-07-09 on
`feat/detection-template-anchor-qml-render`. Every claim cites the file that implements it.*

## One-paragraph summary

Orion is a **pure computer-vision shot-timing assistant for NBA 2K on PS5**. The PS5's HDMI
output is read by a USB capture card (or, card-less, by the decoded Remote Play stream); a
shot-gated CV reader (`simple_meter_reader.py`) extracts the shot meter's fill % + velocity at
~0.6 ms/frame; a native timing engine (`native_orion/src/AutomationEngine.cpp`) predicts when the
fill will cross the green make-window and schedules the release; the release is injected as an
ordinary controller input through a patched Chiaki (Remote Play) fork back to the PS5. Nothing
reads or writes game memory, and nothing runs on the console — the input surface is identical to
a human pressing the button over Remote Play.

## Process topology

```
┌────────────────────────────  PC  ────────────────────────────────┐
│                                                                  │
│  Orion launcher (C++/Qt, native_orion/src/main.cpp)              │
│    ├─ RemotePlaySession.cpp ── spawns + configures ──┐           │
│    ├─ AutomationEngine.cpp  (timing / release)       │           │
│    ├─ VirtualController.cpp (ViGEm XUSB mirror)      │           │
│    └─ OrionInputClient.cpp ──── \\.\pipe\orion_input ┼──┐        │
│                                                      │  │        │
│  autogreen sidecar (Python, long-running)  ◄─────────┘  │        │
│    native_orion/backend/autogreen_sidecar.py             │        │
│    └─ remote_play_orchestrator.py                        │        │
│         ├─ frame source (one of):                        │        │
│         │    capture_card_backend.py   (HDMI card)       │        │
│         │    chiaki_backend.py         (decoder pipe) ◄──┼──┐     │
│         │    wgc_backend.py            (window capture)  │  │     │
│         ├─ simple_meter_reader.py / compressed_meter_reader.py    │
│         └─ latency_estimator.py (frozen-meter oracle)    │  │     │
│                                                          │  │     │
│  OrionStream.exe (patched chiaki-ng fork,                │  │     │
│    vendor/chiaki-orion) ◄── input pipe ──────────────────┘  │     │
│    └── frame pipe \\.\pipe\orion_frames ────────────────────┘     │
└──────────────────────────────┬───────────────────────────────────┘
                               │ Remote Play protocol (network)
                          ┌────▼────┐        HDMI
                          │   PS5   │ ─────────────► capture card ─► USB
                          └─────────┘
```

- The **native launcher** owns UX, licensing (`LicenseClient.cpp`, `SecurityManager.cpp`), the
  controller pipeline, and the timing engine.
- The **sidecar** (`native_orion/backend/autogreen_sidecar.py`) is a long-running Python process
  that owns capture + detection. It speaks **JSONL over stdio**: telemetry/events on stdout, live
  config + commands on stdin (see "Sidecar IPC" below).
- **OrionStream.exe** is the patched chiaki-ng fork (source under `vendor/chiaki-orion`, low-latency
  layer committed in fork commit `1ea25990`). It exposes two named-pipe bridges that stock Chiaki
  lacks: a **pre-encryption input pipe** and a **decoded-frame export pipe**.

## Video path A — HDMI capture card (primary)

`capture_card_backend.py` reads the card directly with `cv2.VideoCapture` (DSHOW first, then
MSMF) on a background thread, into the shared latest-wins `FrameRingBuffer` contract used by all
frame backends (`capture_card_backend.py:1-21`). True 1080p60, no window-capture/occlusion
problems. Selected with `ORION_CAPTURE_CARD=1` (+ optional `ORION_CAPTURE_CARD_INDEX`) in
`remote_play_orchestrator.py:1112-1124`.

Key mechanics:

- **Webcam protection / device gating** — the launcher passes the DirectShow FriendlyName list via
  `ORION_VIDEO_DEVICE_NAMES`; the backend never opens webcam-looking devices and caches the
  resolved card index (`capture_card_backend.py:37-53`).
- **PTS cadence-lock** — card frames carry pts=0, so read-return wall-clock stamps jitter with
  USB/decode/scheduling variance. `CadenceLock` (`capture_card_backend.py:55-90`) is a
  phase/frequency PLL that snaps each stamp to the source's fixed 60 fps grid — the shipped
  14-18× reduction in capture-timestamp jitter (commit `a8c31af2`). Toggled by
  `ORION_CAPTURE_PTS_LOCK`; `ORION_CAPTURE_USE_HW_PTS` probes hardware PTS.
- **Device latency** — a documented ~35 ms Elgato HD60 X estimate
  (`HD60X_DEVICE_LATENCY_ESTIMATE_MS`, `capture_card_backend.py:66-70`) is NOT applied by default;
  `ORION_CAPTURE_DEVICE_LATENCY_MS` enables the mean correction after live confirmation.
- **Capture format** — `ORION_CAPTURE_MJPG=0` requests uncompressed (YUY2/NV12) negotiation to
  skip the JPEG decode; a live test item (`docs/ROADMAP_TO_JULY15.md`, 07-15 item 7a).
- HDCP note: the card must strip HDCP or frames read black; a black feed is rejected by the
  orchestrator's black-frame guard (`capture_card_backend.py:17-21`).

## Video path B — compressed / no-card (Chiaki decoder pipe)

Without a card, detection reads the **decoded Remote Play frames** directly from the fork:
`ORION_FRAME_PIPE` makes `remote_play_client.py` launch the patched build with
`CHIAKI_ORION_FRAME_PIPE=\\.\pipe\orion_frames` and lift the frame-export cap to 60 fps
(`remote_play_client.py:310-325`); `chiaki_backend.py` attaches as the reader
(`frame_source='decoder'`). This stream is H.264 4:2:0 at consumer bitrates — chroma (the red
fill and thin green tip) is what heavy encoding destroys, so this path gets its own reader (next
section). A third fallback, GDI/WGC window capture (`wgc_backend.py`, `ORION_WGC`), exists for
diagnostics.

## CV meter reader (shot-gated) — `simple_meter_reader.py`

The production reader. It replaced a ~5,700-LOC serving chain after a head-to-head proved it
matches on accuracy (peak fill within 0.3 pp, rise within 0.5 pp) at ~1% of the code, ~0.6 ms per
frame, **no ML model**, and zero décor false-locks (`simple_meter_reader.py:1-39`, evidence in
`logs/diagnostics/simple_vs_chain/FINAL_*.txt`). Its design is grounded on two shipping 2K
community tools (2k_Vision's BGR-inRange + confidence-decay lock with its exact parameters, and
Starzen's matchTemplate fast-path + shot gating).

Three stages (vs the chain's ~8):

1. **SHOT-GATE** — is there a tall thin red column in the plausible player band? This is the
   entire false-positive guard. Live, the orchestrator can also arm the gate from the bot's own
   hold state + the v9 pose model via `set_shot_state()` — arming only *relaxes* the early-rise
   acquisition gate (`h_acq_armed`, `ar_min_armed` in `ReaderParams`), it never suppresses a real
   column.
2. **LOCALISE** — grayscale NCC `matchTemplate` fast-path around the last box when locked
   (sub-ms), with a velocity motion-follow so fast-sliding fade meters stay tracked; full
   colour→contour→size/aspect scan only on (re)acquire; a confidence-decay lock (init 1.0 /
   min 0.95 / rate 0.01, exact Arrow2 params) coasts brief misses — with a slower armed decay
   (`conf_rate_armed`) that survives a full arm/body occlusion crossing.
3. **READ** — the fill track is anchored by the **green make-window tip** as the cap;
   fill % = red extent above the red floor / fillable track height. Reported
   position-independent, with a wall-time `velocity_pct_s` every frame for the timing stack.

Contract: `detect()` returns a `DetectResult` field-identical to the old
`meter_detector.MeterDetector.detect()` so the orchestrator flag-selects it
(`ORION_SIMPLE_READER`, `remote_play_orchestrator.py:531-539`) with nothing downstream changing.

All tunables live in `ReaderParams` (`simple_meter_reader.py:103-150`); defaults ARE the shipped
capture-card tuning, asserted byte-identical by a collapse regression. Robustness toggles
(scale-adapt, occlusion hold, guaranteed tip, colour tolerance) are env-gated:
`ORION_READER_SCALE_ADAPT`, `ORION_READER_OCCLUSION`, `ORION_READER_TIP_ENFORCE`,
`ORION_READER_ARMED_HOLD/COAST`, `ORION_READER_ROBUST`, etc.
(`simple_meter_reader.py:978-1063`).

**Green-zone window + self-grader** (also in the reader): the neon make-band (`#1CFE1C`, HSV band
`_NEON_LO/_NEON_HI`, `simple_meter_reader.py:57-62`) is read on frames where the rising red has
not yet covered it; its lower edge IS the per-shot make-window start.
`ORION_GREEN_ZONE_WINDOW` overrides the emitted green window with the colour-derived
`[g_lo, 100]`; `ORION_GREEN_SELF_GRADE` grades each shot EARLY/GREEN/OVER at shot end, OCR-free
(`simple_meter_reader.py:1706-1775`). Port of the offline-validated
`tools/diagnostics/green_zone_grader.py`, which independently reproduces the bimodal engine-log
green window. Every call site is flag-guarded — off is byte-identical.

**Known scope limit**: the reader is tuned for the 2K26 Arrow meter, red fill, at 1920×1080
(`simple_meter_reader.py:926-952` geometry); meter colour auto-detect is not yet wired — the
sidecar config pins Red (`RemotePlaySession.cpp:1546-1554`, explicit TODO). Style/resolution
coverage is an open roadmap item (`docs/ROADMAP_TO_JULY15.md`, "Detection coverage").

## Compressed reader — `compressed_meter_reader.py`

`CompressedMeterReader(SimpleMeterReader)` adds the **luma-domain** machinery the compressed
stream needs (`compressed_meter_reader.py:1-24`):

- **K2 luma fill read** — when the chroma fill profile is too weak, the fill boundary is read
  from the interior Y-column profile with a robust 4-param logistic fit
  (`luma_meter.fit_fill_boundary`), anchored on the bright tip sliver (the luma twin of the
  green-cap anchor), with velocity-aware edge de-lag and a temporal tip-lock.
- **Stale gate** — encoder skip-macroblocks repeat meter pixels on a "new" frame; a pixel-frozen
  ROI while the filter believes the fill is moving is treated as **missing data** (hold, no decay,
  no duplicate velocity sample), with a `STALE_RUN_MAX` deadlock valve so a genuine capture freeze
  still falls through to the honest drop cascade (`compressed_meter_reader.py:44-53`).
- **Quality layer** — `stream_quality.py`'s `QualityEstimator` maps stream quality to
  `ReaderParams.for_quality(q)` (every tunable a monotone function of q that evaluates to the
  shipped constants at q=1) and gates the luma branches via Schmitt bands.
- **Unarmed luma acquire** — only when q sits in the bottom Schmitt bands AND chroma has been
  dead ~0.5 s (`UNARMED_ACQ_Q_MAX`, `UNARMED_CHROMA_DEAD_MIN`), so pristine sources never enter it.
- **Optional CNN confirmer** — `meter_reader_y_infer.py` (`ORION_METER_READER_Y`), the
  synthetically-trained MeterReaderNetY student, wired as a verify-only confirmer.

**Chroma-first collapse rule** (the load-bearing invariant): every chroma gate runs first,
unchanged — on a pristine source the luma branches are dead code and the class is byte-identical
to `SimpleMeterReader`, asserted by `tests/test_compressed_meter_reader.py`. Selected with
`ORION_COMPRESSED_READER=1` (`remote_play_orchestrator.py:541-551`); `ORION_LUMA_TRACKING`
forces/overrides the luma gate (`compressed_meter_reader.py:84-104`). Design + ladder-eval
results: `docs/COMPRESSED_PATH_DESIGN.md`.

## Sidecar IPC (Python ⇄ native launcher)

- **Launch**: `RemotePlaySession::buildSidecarConfig()` serialises the whole session config to a
  compact JSON blob passed as `--config-json` (`RemotePlaySession.cpp:1504-1591`): console IP,
  chiaki path/window title, resolution/fps preset, meter colour + style, green window
  (`green_window_start_pct` defaults 93.0 / end 100.0, `RemotePlaySession.cpp:1557-1558`),
  latency compensation, input mode, shot-type offsets, no-meter fallback config.
- **stdout (JSONL, sidecar → native)**: telemetry (fill %, velocity, meter_present, frame_age_ms,
  green window, measured latency, self-grade records), `started`, log lines, base64 preview
  frames. Writes are lock-serialised so the ~130 KB preview lines can't interleave mid-line
  (`autogreen_sidecar.py:40-54`).
- **stdin (JSONL, native → sidecar)**: live config pushes (`update_remap`), manual goto-shot,
  probe/release markers for the latency oracle (`_handle_probe_marker`,
  `_handle_release_marker`), and `start_stream` — the **warm-preview → stream promotion**: the
  preview sidecar (capture card + detector already live) brings Chiaki + the input hook up
  in place, saving ~5-6 s per connect (`autogreen_sidecar.py:83-107`,
  `RemotePlaySession.cpp:1593-1619`).
- **Stall override**: when the age since the last *unique* frame crosses the watchdog
  (`ORION_STALL_WATCHDOG_MS`), the sidecar synthesises meter-absent telemetry so the native fresh
  gate can never keep serving a frozen fill (`_apply_stall_override`,
  `autogreen_sidecar.py:61-80`).

## Timing engine — `native_orion/src/AutomationEngine.cpp`

The engine consumes fill/velocity telemetry (on the unified capture-epoch clock, Phase 0 commit
`43abdd25`) and owns hold/release. Structure of a shot:

1. **Shot begin** — the user holds the shoot input; `classifyShotType()`
   (`AutomationEngine.cpp:1660`) labels it (Standstill, Left/Right/Post/Back/Front Fade, Go-To…)
   from stick/button state, and `beginShot()` opens per-shot state keyed by
   `timingKey(shotType, mode)`.
2. **Meter-appear anchor** — the per-shot clock anchors to the first *genuine* fresh detection
   (`sawFreshMeterThisShot`), never a stale/memory echo — anchoring to echoes forced per-type
   clocks to absorb the variable fade wind-up and scattered every fade
   (`AutomationEngine.cpp:2160-2178`).
3. **Per-type clocks** — release timing is per shot-type bucket: seeded offsets
   (`shotTypeOffsets`) plus live-learned per-type values (`shotTypeLearnedOffsetMs`,
   `shotTypeFeedforwardMs`, `shotTypeAppearToTipMs`, `shotTypeLatencyMs`, persisted in
   `learning.json`). Fades and Go-To are exempt from the standstill-dominated global clock and
   run their own clocks (`AutomationEngine.cpp:2103-2111`; commits `65ccb5fc`, `fc8c60fd`).
   `[ORION_GOTO_FIRE]` removed every `!isGoto` fence: Go-To flows through the same reshape and
   always fires (`AutomationEngine.cpp:2200-2202`).
4. **Prediction** — `TemporalSampler` keeps a deduped, outlier-rejected sample deque plus a
   2-state (position/velocity) Kalman filter (`AutomationEngine.cpp:28-121`) and answers
   `predictCrossingMs(target)`. Optional far-horizon handoff to the sidecar's registration
   time-to-tip (`ORION_REG_FUSION`, `AutomationEngine.cpp:2311-2319`) and a full fused t_tip
   posterior with re-schedulable deadlines (`ORION_FUSED_FIRE`, `AutomationEngine.cpp:2419+`).
5. **Lead (latency compensation)** — `effectiveLatency` (`AutomationEngine.cpp:2221-2254`) stacks
   network offset + user early/late trim + frame-age staleness (clamped), on one of three bases:
   - `ORION_MEASURED_LEAD` (the big lever): the sidecar's **live-measured** release-path latency
     replaces all modeled constants; engagement is gated on a converged oracle (≥6 labels) AND a
     one-shot clock re-baseline (`AutomationEngine.cpp:2204-2214`, `:3647`).
   - autonomous tip-vision (production default, launcher forces `ORION_AUTONOMOUS_VISION`): one
     self-measured global `learnedLatencyMs` + a small per-type residual.
   - per-type fallback: modeled `controllerChainMs + remotePlayPipelineMs` + per-type offset.
6. **Target** — tip-vision aims the meter TIP (fill=100), green telemetry-only; fades aim the
   confirmed green window's ENTRY edge (`fade_green_entry`); `ORION_PLATEAU_AIM` (H6) supersedes
   both when green is confirmed: aim the apex and land mid-plateau at t(fill=100)+½·capHold
   (`AutomationEngine.cpp:2256-2301`, `:2348`).
7. **Fire** — a **sub-tick scheduler** arms a precise deadline that a controller-owned
   TIME_CRITICAL fire thread executes off the frame/tick grid, confirming the actual fire time
   back (target sub-ms delta); a grace fallback fires in-tick if the thread misses
   (`AutomationEngine.h:1252-1257`, `:1422`, `AutomationEngine.cpp:2114-2151`). This is the
   "actuation → ~0" half of the frame-quantization-jitter fix.
8. **Grade + learn** — the post-release grader dials the learned clocks (grade_v2, commit
   `16f60d86`); guarded so no self-grade drives `learnFromOutcome` improperly
   (`AutomationEngine.cpp:1083`). `ORION_BANDIT_LEAD` adds warmup/calibration-only ±ms lead
   exploration graded by the green-zone self-grade records routed from the sidecar
   (`AutomationEngine.cpp:774`, `:1170`, `:2215-2220`).

**Ceiling stack (H-flags, default OFF, byte-identical until flipped)** — declared in
`AppConfig.h:202-205`, implemented in `AutomationEngine.cpp`:
`ORION_PLATEAU_AIM` (H6 plateau aim) · `ORION_TEMPLATE_ARRIVAL` (H4 template-matched
green-arrival prior, `TemplateArrivalEstimator` at `AutomationEngine.cpp:546`) ·
`ORION_PRESS_T0` (H5 press-anchored template select — the engine natively sees the user's
shoot-press) · `ORION_BANDIT_LEAD` · plus `ORION_MEASURED_LEAD` / `ORION_FUSED_FIRE` from
Phase 1. Rationale + evidence: `docs/ROADMAP_TO_JULY15.md` ("Pushing past 95%" / SYNTHESIS).

## Latency oracle — `latency_estimator.py`

The **frozen-meter oracle v2** (`latency_estimator.py:1-50`): after a release the meter freezes at
F_stop; because our view is delayed, the observed fill reaches F_stop L ms later — inverting the
observed rise yields a supervised per-shot release-path latency label on the same capture-epoch
clock. v2 fixes: rise-vs-deflate disambiguation (late releases routed away from labels), the
plateau-censor margin widened to ~3σ (collapses a +28..38 ms bias to ~+1 ms, σ~8 ms), and an
L_fixed conjugate-normal posterior decomposed from per-shot RTT + expected tick wait — portable
across Wi-Fi wobble. Phase 2 adds tick-staggered warmup **pump-fake probes** (`ORION_PROBE`,
commit `268dbf38`) that pin the absolute console input-tick phase. These labels are what
`ORION_MEASURED_LEAD` consumes.

## Controller input path (inject)

- The physical DualSense is read by Orion and mirrored into a ViGEm XUSB pad
  (`virtual_controller.py` / `VirtualController.cpp`); Chiaki's own SDL HID access to the physical
  pad is blocked (`SDL_GAMECONTROLLER_IGNORE_DEVICES`, `remote_play_client.py:291-300`) so the bot
  is the single input authority. Remap/tempo logic lives in `controller_remap.py`.
- **Pre-encryption input hook** (the low-latency production path): with `ORION_INPUT_HOOK` set,
  the patched fork opens `\\.\pipe\orion_input` (`CHIAKI_ORION_INPUT_PIPE`,
  `remote_play_client.py:301-309`) and `OrionInputClient.cpp` writes the bot's computed
  `ControllerState` directly into Chiaki's input path — past ViGEm/SDL, using Chiaki's own
  crypto/sequencing (never raw packet crafting; `docs/ROADMAP_TO_JULY15.md`, "Considered &
  rejected"). Button mapping XInput→PS at `OrionInputClient.cpp:12-60`; pipe writes are bounded
  at 3 ms so a wedged reader can't stall the TIME_CRITICAL fire thread
  (`OrionInputClient.cpp:69-72`), with ViGEm carrying the tick as fallback.
- Mechanically: the user **holds** the shoot button; the engine relays the hold and **clears the
  button bit at the computed release instant** (`output.buttons &= ~XINPUT_GAMEPAD_X`,
  `AutomationEngine.cpp:2131`; Go-To also neutralises the stick). A side benefit: the engine sees
  the user's shoot-press natively on its own clock — the free early-t0 that H5 builds on.

## Flag reference (the ones that matter)

| Flag | Where | Effect |
|---|---|---|
| `ORION_SIMPLE_READER` | orchestrator | select the production simple reader |
| `ORION_COMPRESSED_READER` | orchestrator | select `CompressedMeterReader` (decoder/Chiaki path) |
| `ORION_LUMA_TRACKING` | compressed reader | force luma branches on/off (unset = quality-managed) |
| `ORION_METER_READER_Y` | compressed reader | CNN Y-crop verify confirmer |
| `ORION_CAPTURE_CARD` / `_INDEX` | orchestrator | HDMI card frame source |
| `ORION_CAPTURE_MJPG` | capture backend | 0 = negotiate uncompressed capture |
| `ORION_CAPTURE_PTS_LOCK` / `_USE_HW_PTS` / `_DEVICE_LATENCY_MS` | capture backend | PTS cadence-lock / hw PTS probe / mean latency correction |
| `ORION_FRAME_PIPE` / `ORION_INPUT_HOOK` | remote_play_client | fork's decoded-frame export / pre-encryption input pipe |
| `ORION_WGC` | orchestrator | window-capture fallback source |
| `ORION_AUTONOMOUS_VISION` | engine | tip-vision production default (launcher forces on) |
| `ORION_MEASURED_LEAD` | engine | oracle-measured latency as the lead base (gated, re-baselined) |
| `ORION_FUSED_FIRE` | engine | fused t_tip posterior fire authority |
| `ORION_PLATEAU_AIM` / `ORION_TEMPLATE_ARRIVAL` / `ORION_PRESS_T0` / `ORION_BANDIT_LEAD` | engine | H6 / H4 / H5 / bandit ceiling stack (default off) |
| `ORION_GOTO_FIRE` | engine | Go-To fires like every other type |
| `ORION_GREEN_ZONE_WINDOW` / `ORION_GREEN_SELF_GRADE` | reader | colour-derived green window / OCR-free per-shot grade |
| `ORION_PROBE` | sidecar/estimator | warmup pump-fake tick-phase probes |
| `ORION_STALL_WATCHDOG_MS` | sidecar | frozen-capture override threshold |
| `ORION_FRAMEDUMP` / `ORION_DETDIAG` / `ORION_DETCSV` | orchestrator | recording/diagnostics for the offline gates |
| `ORION_PRODUCTION_BUILD` | SecurityManager | production security gating (see `docs/RELEASE_SECURITY.md`) |

## Offline validation (no console needed)

Every detection/timing bar is locked by the **regression gate**:
`python tools/regression/run_gates.py` replays recorded live framedumps through the real
production reader and asserts metric floors, then folds in the native QtTest timing suite —
46/46 detection gates + 194 native tests passing on committed HEAD (`docs/REGRESSION_GATES.md`).
Framedumps live under `logs/diagnostics/framedump/`; `ORION_FRAMEDUMP=1` records new corpus
during live sessions.

## Related docs

- `docs/ROADMAP_TO_JULY15.md` — day-by-day plan, ceiling analysis, risk register
- `docs/COMPRESSED_PATH_DESIGN.md` — compressed-reader design + ladder eval
- `docs/GO_TO_MARKET_DESIGN.md` — pricing/onboarding/support design
- `docs/REGRESSION_GATES.md` — the offline quality lock
- `docs/RELEASE_SECURITY.md`, `docs/PACKING_RUNBOOK.md` — packaging + integrity
- `docs/STATE_OF_THE_BOT.md` — consolidated status (companion to this file)
