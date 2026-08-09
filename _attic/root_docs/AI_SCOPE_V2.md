# NexusVision / OrionNative — AI Scope Prompt V2 (Post-Fix Issues)

## Context

Previous fix pass (V1) addressed SHM logging, freshness gates, dual engine, green window, watchdog. After building and launching with `ORION_AUTO_UNLOCK_LOCAL=1`, the app runs but has critical remaining issues confirmed by `logs/orion_native.log`.

## Confirmed Runtime Issues from Latest Log

1. **Capture card never opens**: `CaptureCard backend: no live device (index 0, DSHOW/MSFM)` repeating every ~2s. Sidecar exits after ~90s of retries. No frames ever produced.
2. **Zero SHM frames received**: No `frame_shm` or `SHM:` log entries. The `SharedMemoryFrameReader` never opens or reads.
3. **All releases on stale memory**: `detsummary: fresh=0 staleMem=N` on every shot. `firstFreshMs=-1`. The bot never gets a genuine fresh detection — it fires blind on meter memory echoes.
4. **Black preview box**: No frames reach QML because the capture card never opens. The `clearRemotePreviewFrame()` blank (dark color `6,9,14`) is all the user sees.

---

## ISSUE 1: Capture Card Not Opening (CRITICAL — Black Screen Root Cause)

**Symptom**: `no live device (index 0, DSHOW/MSMF)` every 2s. OpenCV `VideoCapture(0)` fails on both DSHOW and MSMF backends.

**Root cause hypotheses**:
- The `wmic` orphan kill we added may be killing the sidecar's OWN python process before it opens the device (the `commandline like '%autogreen_sidecar%'` pattern matches the sidecar itself).
- Or: a previous orphan still holds the Elgato handle. The `taskkill` by window title runs first, then `wmic` runs — but `wmic` may kill the NEW sidecar process too if it's already spawned.
- Or: the Elgato HD60 X device index changed (multiple video devices listed: `Elgato HD60 X|Logitech Webcam C925e|OBS Virtual Camera`). Index 0 may not be the Elgato.

**Files**: `RemotePlaySession.cpp:1697-1710` (orphan kill), `remote_play_orchestrator.py` (capture card open), `capture_card_backend.py` (device open logic)

**What to do**:
1. The `wmic` orphan kill MUST NOT match the current sidecar process. Either: (a) run it BEFORE spawning the new sidecar (it currently does — verify timing), or (b) exclude the current PID, or (c) remove it entirely if the `taskkill` by window title is sufficient.
2. Add device name-based selection: parse `ORION_VIDEO_DEVICE_NAMES` and find the Elgato by name, not by index 0.
3. Add a pre-open device probe: log available devices + which index maps to "Elgato HD60 X" before attempting `VideoCapture(index)`.
4. Add retry with device re-enumeration: if index 0 fails, try all indices 0-5.

---

## ISSUE 2: SHM Frame Transport Not Working (CRITICAL)

**Symptom**: Zero `frame_shm` events received by C++. No `SHM:` log entries. The `SharedMemoryFrameReader::open()` is never called or always fails.

**Root cause**: Since the capture card never opens, the sidecar never produces frames, so `frame_shm` events are never emitted. This is a **downstream symptom of Issue 1**. However, even if frames were produced, the SHM path may have issues:

**What to do**:
1. Fix Issue 1 first — SHM can't be tested without frames.
2. After fix, verify `frame_shm: first event received — handler reached` appears in log.
3. Verify `SHM: Shared memory frame reader opened successfully` appears.
4. If SHM open fails, check: Python `mmap.mmap(-1, size, tagname="OrionPreviewFrame_v1")` vs C++ `OpenFileMappingW(L"OrionPreviewFrame_v1")` — Windows may prepend `Global\` or `Local\` namespace.
5. Add periodic SHM frame count logging (every 300 frames / 5s) to confirm continuous reception.

**Files**: `SharedMemoryFrameReader.cpp` (full file), `RemotePlaySession.cpp:2050-2075` (frame_shm handler), `shm_frame_bridge.py` (full file)

---

## ISSUE 3: Meter Detection Disappears When Player Jumps (HIGH)

**Symptom**: During gameplay, when the player jumps for a shot, the meter briefly disappears from the screen (camera pan / player movement shifts the meter position). The detector loses lock and serves `meter_memory` echoes. The bot fires on stale data (`fresh=0 staleMem=N`).

**Root cause**: The meter ROI tracker (`meter_detector.py`) uses a position-jump gate (`position_jump_max_px`) that may reject the meter when it shifts due to camera pan during a jump shot. The warm re-acquire window (`_WARM_REACQ_S = 1.2s`) should allow re-lock, but the detector may not find the meter at the new position fast enough.

**What to do**:
1. Audit the position-jump gate: `meter_detector.py:1263-1266` (`_acq_jump_gate_px`). During a jump shot the meter can shift 20-50px vertically. Verify the gate allows this.
2. Check the warm re-acquire path: `meter_detector.py:1225-1229, 1348-1354, 1447+`. The warm window is 1.2s — verify it covers the jump animation (~0.5-1s).
3. Check the track-hold path: `meter_detector.py:4710-4726`. The `_bt_hold_s` track-hold should bridge the meter during the jump. Verify it's enabled and the hold window is long enough.
4. Check the template tracker coast: `simple_meter_reader.py:2715` (`_TPL_COAST = 8`). 8 frames at 30fps = 267ms. A jump shot camera pan may last longer. Consider increasing to 12-15.
5. Add logging: when the meter is lost during a shot (armed state), emit a `log` event with the reason (jump gate, coast exhausted, confidence drop).

**Files**: `meter_detector.py:1200-1460` (tracker), `meter_detector.py:4700-4730` (track-hold), `simple_meter_reader.py:2710-2760` (template coast)

---

## ISSUE 4: Bot Not Timing to the Tip (HIGH)

**Symptom**: Releases fire at wrong fill percentages — not at the meter tip (100%) or green window. The `detsummary` shows `fresh=0` on every shot, meaning the bot is firing on stale memory echoes with no real-time meter data.

**Root cause**: Since the capture card never opens (Issue 1), there are no frames, no detections, and no fresh data. The bot falls back to the feedforward clock (deterministic hold time), which is uncalibrated. Even when frames DO flow, the freshness gate (`frameAgeMs_ <= 250.0 && pixelAgeMs_ <= 200.0`) may reject too many samples if the capture card produces duplicate frames.

**What to do**:
1. Fix Issue 1 (capture card) — without frames, timing is impossible.
2. After fix, verify `fresh > 0` in `detsummary` logs.
3. Audit the feedforward clock fallback: `AutomationEngine.cpp:2400-2410`. When no fresh detections arrive, the bot fires on a deterministic timer. Verify this timer is at least approximately correct for standstill shots.
4. Check the target calculation: `AutomationEngine.cpp:2415-2419`. When `greenTracker_.confirmed()` is false, target is `fullMeterTargetPct = 100.0`. Verify the bot actually aims for 100% and the release fires at the right time.
5. Check the effective latency calculation: `AutomationEngine.cpp:2650-2657`. If `frame_age_ms` is high (stale data), the latency compensation may over-fire early.

**Files**: `AutomationEngine.cpp:2400-2500` (target + fire decision), `AutomationEngine.cpp:2650-2700` (fused fire), `RemotePlaySession.cpp:2259-2268` (freshness gate)

---

## ISSUE 5: Live Capture Preview Shows Black Box (HIGH)

**Symptom**: The preview panel shows a dark blank box (`QColor(6,9,14)`) instead of the Elgato HDMI feed.

**Root cause**: The capture card never opens (Issue 1), so no frames are ever produced. The `clearRemotePreviewFrame()` blank is all the user sees. This is a downstream symptom.

**What to do**:
1. Fix Issue 1 — frames will flow and the preview will show the feed.
2. After fix, verify `handleRemoteFrame` is called and `frameProvider_->setFrame(frame)` updates QML.
3. Check QML `Image` source: `RemotePlayPage.qml:105` uses `source: "image://remote/live/" + orion.frameSerial`. Verify the image provider returns frames.
4. Verify `frameChanged()` signal fires and QML re-requests the image.

**Files**: `OrionAppController.cpp:5836-5877` (frame handling), `RemotePlayPage.qml:100-130` (QML image)

---

## ISSUE 6: Sidecar Process Lifecycle — wmic Kill May Target Self (HIGH)

**Symptom**: The `wmic` orphan kill we added in the fix pass may kill the sidecar's own Python process.

**Current code** (`RemotePlaySession.cpp:1707-1710`):
```
QProcess::execute("wmic.exe", {"process", "where",
    "name='python.exe' and commandline like '%autogreen_sidecar%'",
    "call", "terminate"});
```

This runs BEFORE the new sidecar spawns (in `startSidecar()`), so it should only catch orphans from previous runs. But if a previous sidecar is still shutting down when this fires, it kills that too (which is intended). The risk is if the timing is wrong and the new sidecar is already spawned.

**What to do**:
1. Verify the `wmic` call runs BEFORE `proc->start()` — check the code order in `startSidecar()`.
2. If it runs after, move it before or remove it.
3. Consider removing the `wmic` call entirely — the `taskkill` by window title may be sufficient, and `wmic` is slow (~1-2s blocking on the GUI thread).

**Files**: `RemotePlaySession.cpp:1697-1710`

---

## ISSUE 7: Freshness Gate Too Tight — 250ms May Reject Valid Frames (MEDIUM)

**Symptom**: After tightening `frameAgeMs_` from 350 to 250 in the fix pass, the freshness gate may reject valid frames from the capture card if it produces frames at a variable rate.

**Root cause**: The Elgato HD60 X at 1080p60 produces frames at ~16.7ms intervals. But if the USB bus is busy or the capture pipeline has jitter, `frame_age_ms` can spike to 200-300ms momentarily. The 250ms gate would reject these as stale.

**What to do**:
1. Monitor `frame_age_ms` distribution during normal operation — check telemetry logs.
2. If spikes above 250ms are common, revert to 300ms or use an adaptive threshold (rolling p95 + margin).
3. The `pixel_age_ms` gate at 200ms is the real frozen-echo detector — the `frame_age` gate is secondary. Consider 300ms for `frame_age` and keeping 200ms for `pixel_age`.

**Files**: `RemotePlaySession.cpp:2268`

---

## ISSUE 8: Python Release Path Disabled — Verify C++ Engine Actually Fires (MEDIUM)

**Symptom**: We set `release_enabled=False` in the Python `RemapConfig`. But if the C++ `AutomationEngine` isn't actually firing releases (due to no fresh data), the bot does nothing.

**What to do**:
1. After fixing Issue 1, verify the C++ engine fires: look for `Release:` or `Fired:` log entries.
2. Check `AutomationEngine.cpp` fire path: the `processHolding` function must reach the release decision. Verify `shot_.holdState` transitions to HOLDING and the fire condition triggers.
3. If the C++ engine never fires, check if `automation_.setArmed(true)` is called (it's gated by `automationSecurityAllowed()` which checks `safeModeActive_` and lease gate).
4. Verify the `fusedDiagnostic` signal is connected to `appendLog` so engine diagnostics appear in `orion_native.log`.

**Files**: `OrionAppController.cpp:2947` (arming), `AutomationEngine.cpp:2400-2500` (fire decision), `OrionAppController.cpp:5557-5578` (security gate)

---

## ISSUE 9: Meter Detection — Camera Pan During Jump Shot Loses Lock (MEDIUM)

**Symptom**: When the player jumps for a fade/jump shot, the camera pans and the meter shifts position on screen. The detector's ROI lock breaks and it takes too long to re-acquire.

**Detail**: The meter detector has several re-acquisition mechanisms:
- **Warm re-acquire** (`_WARM_REACQ_S = 1.2s`): allows re-lock without re-proving the rise.
- **Track-hold** (`_bt_hold_s`): bridges gaps using predicted pose.
- **Template coast** (`_TPL_COAST = 8` frames): velocity-coasts through contour misses.

But during a jump shot the camera can pan 100+px in <500ms, moving the meter completely out of the tracked ROI. The warm re-acquire window may be too short, and the track-hold prediction may not account for camera pan velocity.

**What to do**:
1. Increase `_TPL_COAST` from 8 to 12 (400ms at 30fps) to bridge longer camera pans.
2. Increase `_WARM_REACQ_S` from 1.2 to 2.0 seconds to cover the full jump animation + landing.
3. Add camera-pan-aware re-acquisition: detect large bbox shifts and expand the search window.
4. Log when the meter is lost during an armed shot: emit `{"event":"log","msg":"meter lost during shot: reason=X","level":"warn"}`.

**Files**: `meter_detector.py:113` (`_WARM_REACQ_S`), `simple_meter_reader.py:2715` (`_TPL_COAST`), `meter_detector.py:4700-4730` (track-hold)

---

## ISSUE 10: Telemetry Not Flowing — No Detection Events (MEDIUM)

**Symptom**: The log shows no `telemetry` or `detection` events being processed. The `detsummary` entries are from June (old session), not from the current July 24 session.

**Root cause**: Since the capture card never opens, the sidecar never produces frames, so no telemetry/detection events are emitted. This is downstream of Issue 1.

**What to do**:
1. Fix Issue 1 first.
2. After fix, verify telemetry events appear: `frame_age_ms`, `pixel_age_ms`, `meter_present`, `fill_pct`, `confidence`.
3. Check `RemotePlaySession.cpp:2200-2260` (telemetry handler) processes the events.
4. Verify `OrionAppController` receives detection results and updates `lastRealMeterSeenMs_`.

---

## Priority Order

| Priority | Issue | Description |
|----------|-------|-------------|
| **CRITICAL** | 1 | Capture card not opening — black screen root cause |
| **CRITICAL** | 2 | SHM frames not received (downstream of 1) |
| **HIGH** | 6 | wmic kill may target self — verify or remove |
| **HIGH** | 3 | Meter detection disappears on jump |
| **HIGH** | 4 | Bot not timing to tip (downstream of 1) |
| **HIGH** | 5 | Live capture preview black box (downstream of 1) |
| **MEDIUM** | 8 | Verify C++ engine fires after Python disabled |
| **MEDIUM** | 7 | Freshness gate 250ms may be too tight |
| **MEDIUM** | 9 | Camera pan during jump loses meter lock |
| **MEDIUM** | 10 | Telemetry not flowing (downstream of 1) |

## Key Insight

**Issues 2, 4, 5, and 10 are all downstream symptoms of Issue 1 (capture card not opening).** Fix Issue 1 first, then re-test. The `wmic` orphan kill (Issue 6) is the most likely cause of Issue 1 — it may be killing the sidecar process or interfering with device open.

## Instructions

1. **Start with Issue 6** — verify the `wmic` kill timing and safety. If it runs after the sidecar spawns, remove it or move it before.
2. **Then Issue 1** — fix capture card device selection (name-based, not index 0) and add retry logic.
3. **Then test** — launch and verify frames flow, SHM works, preview shows, detections arrive.
4. **Then Issues 3, 4, 9** — meter detection and timing need real frames to debug.
5. **Then Issues 7, 8** — tune freshness and verify C++ engine fires.
6. **Read all referenced files before making changes.**
7. **Provide concrete code changes, not suggestions.**
8. **Test after each fix** — launch the app and check logs.
