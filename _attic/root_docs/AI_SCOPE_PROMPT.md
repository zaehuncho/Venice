# NexusVision / OrionNative — Full Scope & Fix Prompt

You are handed a Windows desktop application (C++/Qt + Python sidecar) that provides Remote Play preview with capture card support (Elgato HD60 X), real-time meter detection, and shot automation for a basketball game. The native C++ app (`OrionNative.exe`) launches a Python sidecar (`autogreen_sidecar.py`) which handles capture card frame acquisition, orchestrates Remote Play via Chiaki, runs meter detection, and sends frames + telemetry back to the native app for display and automation.

This document covers **every known issue** across the full stack: shared memory frame transport, capture card preview management, meter detection logic, bot automation, release timing, self-learning, and watchdog reliability. For each, we describe the current state, what's been tried, what remains, and what concrete code changes are needed. **Read every referenced file before proposing fixes.** Ask clarifying questions if anything is ambiguous.

---

## Architecture

```
OrionNative.exe (C++/Qt)
  ├── RemotePlaySession.cpp — manages sidecar process, stdout/stderr parsing, frame_shm handler, telemetry
  ├── OrionAppController.cpp — main app controller, frame provider, UI state, watchdog, disconnect/reconnect
  ├── SharedMemoryFrameReader.cpp — reads raw BGR frames from Windows shared memory
  ├── AutomationEngine.cpp — C++ automation: release timing, green window, learning loops, fused fire
  ├── FrameDecoder.cpp — async JPEG decoder (fallback path)
  └── QML frontend

Python Sidecar (autogreen_sidecar.py)
  ├── remote_play_orchestrator.py — Chiaki launch, capture card, frame loop, stall watchdog
  ├── shm_frame_bridge.py — ShmFrameWriter: writes raw BGR to Windows shared memory via mmap
  ├── meter_detector.py — meter detection: ROI lock, green window scan, meter memory, Kalman tracking
  ├── simple_meter_reader.py — fill reader: stalebreak, coast steal, sub-pixel reading
  ├── controller_remap.py — Python shot automation: HoldState machine, predictive release, self-learning
  └── autogreen_sidecar.py — main entry, preview encoder thread, stdout JSONL events, telemetry heartbeat
```

### Frame Transport

1. **SHM fast path** (preferred): Python writes raw BGR pixels to Windows file-mapping `"OrionPreviewFrame_v1"` (2,764,864 bytes = 64-byte header + 1280×720×3 BGR), emits lightweight `{"event":"frame_shm","frame_number":N}` JSONL to stdout. C++ reads pixels directly via `SharedMemoryFrameReader` using `OpenFileMappingW` + `MapViewOfFile` + mutex sync.

2. **JPEG fallback path**: Python encodes frame as JPEG, base64-encodes, emits `{"event":"frame","jpeg_b64":"...","frame_number":N}`. C++ decodes via `FrameDecoder` worker thread. Must remain functional if SHM fails.

### Telemetry & Detection IPC

The sidecar emits JSONL events on stdout (serialized via `STDOUT_EMIT_LOCK`):
- `{"event":"started",...}` — sidecar ready
- `{"event":"telemetry","fps":N,"frame_age_ms":X,"pixel_age_ms":Y,"meter_present":bool,"raw_fed":bool,"shot":{...},"green":{...},"fusion":{...},...}` — per-frame detection results
- `{"event":"frame_shm","frame_number":N}` — SHM frame notification
- `{"event":"preview_stats","shm_ok":N,"shm_fail":N,...}` — periodic stats
- `{"event":"log","msg":"...","level":"..."}` — sidecar log messages

### Environment

- **OS**: Windows 11
- **Python**: 64-bit
- **Qt**: 6.x (Qt Quick / QML frontend)
- **Build**: CMake + MSVC (Release config)
- **Capture Card**: Elgato HD60 X (1920x1080 input, resized to 1280x720 for SHM)
- **Shared Memory**: `"OrionPreviewFrame_v1"` (2,764,864 bytes), mutex `"OrionPreviewMutex"` (100ms writer timeout, 50ms reader timeout)
- **Log file**: `logs/orion_native.log` (written via `OrionAppController::appendLog()` → `pendingLogDiskLines_`)

---

## Key File Paths

**Shared Memory & Frame Transport:**
- `shm_frame_bridge.py` — Python SHM writer (mmap-based, 245 lines)
- `native_orion\src\SharedMemoryFrameReader.cpp` — C++ SHM reader (177 lines)
- `native_orion\src\SharedMemoryFrameReader.h` — C++ SHM reader header (50 lines)
- `native_orion\src\FrameDecoder.cpp` — Async JPEG decoder (fallback)

**Sidecar & Process Management:**
- `native_orion\src\RemotePlaySession.cpp` — Sidecar process, stdout parsing, frame_shm handler, telemetry (2454 lines)
- `native_orion\src\RemotePlaySession.h` — RemotePlaySession header
- `native_orion\src\OrionAppController.cpp` — Main app controller, frame provider, UI state, watchdog (7252 lines)
- `native_orion\src\SidecarWatchdog.h` — Restart delay constants
- `native_orion\backend\autogreen_sidecar.py` — Python sidecar main (1128 lines)
- `remote_play_orchestrator.py` — Remote Play orchestrator (2909 lines)

**Meter Detection:**
- `meter_detector.py` — Meter detection (5106 lines — ROI lock, green window, meter memory, Kalman tracking)
- `simple_meter_reader.py` — Simple meter reader (3423 lines — stalebreak, coast steal, fill reading)

**Bot Automation & Release Timing:**
- `controller_remap.py` — Python controller remap / shot automation (1376 lines — HoldState, predictive release, self-learning)
- `native_orion\src\AutomationEngine.cpp` — C++ automation engine (5486 lines — release timing, green window, learning loops, fused fire)
- `native_orion\src\AutomationEngine.h` — C++ automation engine config (1801 lines — all timing parameters, freshness windows, memory trust)

**Logs:**
- `logs\orion_native.log` — Native app log file

---

## SECTION A: Shared Memory Frame Transport

### A1: C++ SharedMemoryFrameReader Not Confirmed Reading SHM Frames

**Status:** SHM writes are working (Python confirmed `shm_ok=11000+ shm_fail=0` at ~55fps). C++ read path unconfirmed.

The Python `ShmFrameWriter` writes frames via `mmap.mmap(-1, size, tagname="OrionPreviewFrame_v1")`. The C++ `SharedMemoryFrameReader` opens via `OpenFileMappingW(L"OrionPreviewFrame_v1")`. These should match on Windows, but it has not been confirmed that the C++ reader successfully opens and reads frames from the mmap-created section.

The `frame_shm` handler's first-event log was changed from `qWarning` to `emit setupMessage(...)` so it appears in `orion_native.log`, but the rebuilt binary has not been tested yet. The `qCInfo(lcShmReader)` / `qCWarning(lcShmReader)` calls in `SharedMemoryFrameReader.cpp` use Qt logging categories which may not be enabled.

**What to do:**
1. Verify `SharedMemoryFrameReader::open()` succeeds — `OpenFileMappingW` must find the mapping created by Python's `mmap.mmap(-1, ..., tagname=...)`. Check if Python's mmap prepends `Global\` or `Local\` namespace prefixes (it shouldn't for `mmap(-1, ...)`).
2. Verify `readFrame()` returns valid QImages — check torn-read detection (writeCount before/after), magic/version validation, and `QImage::Format_BGR888` creation. If mmap's memory layout differs from C++ expectations, reads fail silently.
3. Enable Qt logging categories for `orion.shmReader` OR convert `qCInfo`/`qCWarning` to `emit setupMessage(...)` so they appear in `orion_native.log`.
4. Confirm `frameReady` signal fires for SHM frames, reaching `OrionAppController::handleRemoteFrame()` and updating QML.
5. Add periodic SHM frame count logging (every ~300 frames / 5s) to confirm continuous reception.

**Key files:** `SharedMemoryFrameReader.cpp` (full file), `RemotePlaySession.cpp:2050-2075` (frame_shm handler), `shm_frame_bridge.py` (full file), `OrionAppController.cpp:5835` (handleRemoteFrame)

### A2: frame_shm Event Reception Not Confirmed

**Status:** Under investigation.

The sidecar emits `frame_shm` events via `_emit()` to stdout. `preview_stats` events (also via `_emit()`) ARE received. But `frame_shm` reception hasn't been confirmed in logs (explained by the `qWarning` logging issue above, now fixed but untested).

**What to do:**
1. Test with the `setupMessage` fix — check if `"frame_shm: first event received — handler reached"` appears in `orion_native.log`.
2. If still missing — check for stdout pipe buffering. `PYTHONUNBUFFERED=1` is set and `_emit()` calls `sys.stdout.flush()`, but verify the pipe isn't backing up.
3. Consider event volume — at 55fps, 55 `frame_shm` events/sec plus periodic `preview_stats`. If `onSidecarStdout` is slow, events could back up.

**Key files:** `RemotePlaySession.cpp:1792` (readyReadStandardOutput), `RemotePlaySession.cpp:1975-2075` (handleSidecarMessage), `autogreen_sidecar.py:623-626` (frame_shm emit)

### A3: BGR/RGB Format Verification

**Status:** Potential issue — needs check.

`SharedMemoryFrameReader::readFrame()` creates `QImage::Format_BGR888` from raw BGR data (matches OpenCV's BGR output). The QML `Image` component may expect RGB. If the `RemoteFrameProvider` or QML rendering path converts BGR→RGB, there may be an unnecessary conversion or color swap.

**What to do:**
1. Verify `RemoteFrameProvider::setFrame()` handles `Format_BGR888` correctly.
2. Check if `handleRemoteFrame()` does any format conversion at `OrionAppController.cpp:5835`.
3. Qt should handle `Format_BGR888` natively in rendering, but the provider may need `Format_RGB32` or `Format_RGBA8888` for QML compatibility.

**Key files:** `SharedMemoryFrameReader.cpp:119`, `OrionAppController.cpp:5827-5835`

### A4: SHM Reader Does Not Reopen After Sidecar Restart

**Status:** Potential issue — needs retry logic.

When the sidecar restarts, `shmReader_.close()` is called in `stopSidecar()`. On the next `frame_shm` event, the handler calls `if (!shmReader_.isOpen()) shmReader_.open()`. But if the new sidecar's `ShmFrameWriter` hasn't created the file mapping yet, `OpenFileMappingW` fails silently — no logging, no retry until the next event.

**What to do:**
1. Add `emit setupMessage(...)` when `shmReader_.open()` fails so it appears in the log.
2. The existing per-event `isOpen()` check provides retry, but add a 1-2ms delay or backoff to avoid hammering `OpenFileMappingW` at 55fps.

**Key files:** `RemotePlaySession.cpp:2066-2069`, `SharedMemoryFrameReader.cpp:41-73`

### A5: SHM Write Performance

**Status:** Working but potentially suboptimal.

The write path uses `self._mmap.seek(0)` + multiple `self._mmap.write()` calls (header, padding, frame bytes, trailing zeros). This involves `frame.tobytes()` (~2.7MB copy) + `mmap.write()` (another copy).

**What to do:**
1. Use `memoryview(self._mmap)[HEADER_SIZE:HEADER_SIZE+body_size] = frame` to avoid the intermediate `tobytes()` copy.
2. Skip trailing zero fill when `body_size == self._max_body` (common case at 1280×720).
3. Zero the buffer once at startup; only zero the used portion when frame size shrinks.

**Key files:** `shm_frame_bridge.py:201-219`

### A6: Unused ctypes Bindings Cleanup

**Status:** Cleanup needed.

After switching to `mmap`, several ctypes Win32 API bindings are unused: `_CreateFileMappingW`, `_OpenFileMappingW`, `_MapViewOfFile`, `_UnmapViewOfFile`, `PAGE_READWRITE`, `FILE_MAP_ALL_ACCESS`, `ERROR_ALREADY_EXISTS`. Keep `_CreateMutexW`, `_WaitForSingleObject`, `_ReleaseMutex`, `_CloseHandle` (still used for mutex sync).

**Key files:** `shm_frame_bridge.py:44-83`

### A7: Diagnostic Logging Cleanup

**Status:** Needed.

Extensive one-shot `logger.error()` diagnostics were added during debugging in `shm_frame_bridge.py`, `autogreen_sidecar.py`, and `remote_play_orchestrator.py`. Downgrade or remove the one-shot diagnostics (first frame shape, post-resize dimensions, video_cb first call). Keep real error paths (write failed, mutex wait failed, body_size > max_body).

**Key files:** `shm_frame_bridge.py:166-169,183-186`, `autogreen_sidecar.py:512-517`, `remote_play_orchestrator.py:1928-1930`

---

## SECTION B: Capture Card Preview Management

### B1: Stale Preview Frame on App Launch

**Status:** Fix applied — needs test.

When the app launches and opens the live capture preview, a stale frame from a previous session may be visible until the new sidecar opens the capture card. A blank frame clear (`QColor(6,9,14)`) was added in `OrionAppController::startCapturePreview()` (~line 3979) before launching the sidecar.

**What to do:**
1. Test: verify the preview panel shows a dark blank briefly before the capture card feed appears.
2. Also clear on sidecar exit — `RemotePlaySession.cpp:1824-1829` sets `previewMode_ = false` but doesn't clear the frame provider. Add a blank frame clear there.

**Key files:** `OrionAppController.cpp:3957-3990`, `RemotePlaySession.cpp:1824-1829`, `OrionAppController.cpp:4100-4109`

### B2: Post-Disconnect Capture Preview Resume Delay

**Status:** Fix applied — needs test.

After disconnecting from Remote Play, the capture card preview resume delay was reduced from 3000ms to 1500ms (hardcoded in `finishRemotePlayTeardown()`). The rationale: `stopSidecar()` already waits up to 3000ms for graceful shutdown (releasing the DirectShow capture card handle), so by the time teardown returns, the handle is already freed.

**What to do:**
1. Test: disconnect from Remote Play, verify capture preview resumes within ~1.5s without "no live device" errors.
2. If errors appear, the 1500ms may be too short (DirectShow handle release can take 2500-4000ms). Consider adaptive retry: start at 1500ms, retry after additional 1500ms if "no live device".
3. Use the `kSidecarRestartDelayMsCaptureCard` constant instead of hardcoding 1500 — update the constant itself if the forensic data supports it.

**Key files:** `OrionAppController.cpp:4176-4181`, `SidecarWatchdog.h:45`, `RemotePlaySession.cpp:1882-1925`

### B3: Orphaned Python Sidecar Processes

**Status:** Partially fixed.

Orphaned `python.exe` processes with the capture card open ("ActiveMovie Window" window title) prevent new sidecar instances from opening the capture card, causing "no live device (index 0)" errors.

**Fixes applied:**
1. `startSidecar()` runs `taskkill /FI "WINDOWTITLE eq ActiveMovie Window" /IM python.exe /F /T` before launching.
2. `killChiakiProcesses()` uses `sidecarPid()` to specifically kill the sidecar by PID.
3. `PYTHONUNBUFFERED=1` set for the sidecar process.

**What to do:**
1. The taskkill by window title may not catch all orphans. Consider `wmic process where "name='python.exe' and commandline like '%autogreen_sidecar%'"` for more robust detection.
2. Register a Windows Job Object that kills child processes when the parent dies (handles app crash cleanup).
3. The `taskkill` in `startSidecar()` is synchronous (`QProcess::execute()`) and blocks the GUI thread. Make it asynchronous or move to a worker thread.

**Key files:** `RemotePlaySession.cpp:1619-1656`, `OrionAppController.cpp` (killChiakiProcesses), `RemotePlaySession.h:225` (sidecarPid)

### B4: stopSidecar() Graceful Shutdown Timeout

**Status:** Working — can be optimized.

`stopSidecar()` waits up to 3000ms via `proc->waitForFinished(3000)`. If the sidecar doesn't exit, it's force-killed. The 3000ms was chosen because `orch.stop()` calls `terminate_chiaki_processes()` which loops up to 6s. Too-short timeout abruptly drops the PS5 TCP connection ("LAN cable disconnected").

**What to do:**
1. Consider reducing to 2000ms if graceful shutdown typically completes in ~1-2s.
2. Monitor for "LAN cable disconnected" errors after any reduction.
3. `waitForFinished(3000)` already returns early on success, so no wasted time if sidecar exits quickly.

**Key files:** `RemotePlaySession.cpp:1893-1911`

---

## SECTION C: Meter Detection — Freshness, Frozen Echo, Stall

### C1: Frozen Echo Detection Bypassing Freshness Gate

**Status:** Partially fixed — needs verification.

A "frozen echo" occurs when the capture card free-runs on a duplicate frame (HDMI signal frozen but capture device keeps delivering same pixels). `frame_age_ms` stays low (~0) because duplicate frames refresh the capture clock, but `pixel_age_ms` (time since last UNIQUE frame) climbs without bound.

The freshness gate in `RemotePlaySession.cpp:2248-2257` now checks BOTH:
- `frameAgeMs_ <= 350.0` (capture clock)
- `pixelAgeMs_ <= 200.0` (unique-pixel clock)

The old gate ORed `unique_frame_fps > 0` with `confidence >= 0.12` fallback, which let a frozen echo pass freshness at `uniqueFps=0` (exactly backwards).

**What to do:**
1. Verify the pixel_age gate fires during a real freeze — confirm `sidecarFresh` goes false, `detected` goes false, and the engine's HOLD clock resets.
2. Check the 200ms pixel_age threshold — at 60fps, healthy feed has pixel_age ~16-33ms. 200ms allows ~12 duplicate frames. If the capture card regularly produces 4+ duplicate frames, this may be too tight.
3. Check the 350ms frame_age threshold — consider tightening to 250ms to match the stall watchdog.
4. Verify the orchestrator stall watchdog (`remote_play_orchestrator.py:2118-2139`) synthesizes `meter_present=false` after 250ms of no unique frames, and the native side receives it.
5. The main frame feed stall watchdog (`OrionAppController.cpp:3100`) uses `frameAge` (refreshed by duplicates) — a frozen echo would NOT trigger it. Consider adding `pixelAge` to the main stall detection, not just the input hook recovery path.

**Key files:** `RemotePlaySession.cpp:2248-2274`, `autogreen_sidecar.py:80-95`, `remote_play_orchestrator.py:2110-2139`, `OrionAppController.cpp:6442-6480`

### C2: Stall Watchdog — pixel_age Threshold Tuning

**Status:** Working — may need adjustment.

The stall watchdog fires at 250ms (`ORION_STALL_WATCHDOG_MS`) of no unique frames. The sidecar's `_apply_stall_override()` also forces `meter_present=False` when `pixel_age_ms >= stall_ms`.

**What to do:**
1. Monitor false trips — check logs for "stall watchdog: no unique frame" during normal operation. If they appear during normal gameplay, increase the threshold.
2. Coordinate with the native 12s frame feed stall — ensure the native watchdog doesn't fire while the sidecar is restarting (the `lastWatchdogRestartMs_` 30s cooldown should prevent this, but verify).
3. Consider adaptive threshold based on rolling `unique_frame_fps`.
4. For older sidecars that don't send `pixel_age_ms` (defaults to 0), the pixel_age term is inert — the gate degrades to frame_age only, which is vulnerable to frozen echoes.

**Key files:** `remote_play_orchestrator.py:2118-2139`, `autogreen_sidecar.py:80-95`, `OrionAppController.cpp:3100-3113`

### C3: Meter Memory Echo — Stale Fill Served as Fresh

**Status:** Partially fixed — needs verification.

When `meter_detector.py` loses the meter (ROI not found, low confidence, unstable bbox), it serves a "meter_memory" echo — a held copy with decayed confidence, `rejection_reason="meter_memory"`, `raw_fed=False`.

The C++ `AutomationEngine.cpp:1422-1458` distinguishes:
- **genuineFresh**: clean raw detection (`detected && rejectionReason != "stale_or_memory"`)
- **memoryTrusted**: echo promoted to fresh-equivalent under strict conditions (within 45ms of last genuine accept, fill < 85%, `memoryTrustEnabled` flag — defaults `false`)

Only genuine fresh accepts feed the velocity sampler, green tracker, and rise-band trackers.

**What to do:**
1. Verify `raw_fed` flag propagation — sidecar sets `raw_fed=False` for meter_memory echoes. Confirm C++ reads this and sets `rejectionReason="stale_or_memory"` correctly.
2. If `memoryTrustEnabled` is enabled, verify the 45ms age bound and 85% fill cap. A too-generous bound could let a stale echo drive a release.
3. Check the MeterBoxKalman echo — `meter_detector.py:5050-5084` uses a Kalman filter to predict meter position during a miss. Verify the predicted position doesn't cause false re-detection at a wrong location.
4. Verify the TIP-GUARD: `memoryTrustMaxFillPct = 85.0` prevents trusting an echo near the green window. A held 100% echo would trigger a false release.
5. Check the stalebreak/coast steal — `simple_meter_reader.py:2710-2760` re-seats the ROI lock when a coast lasts >= 8 frames. Verify this doesn't cause false re-detections of static decor.

**Key files:** `AutomationEngine.cpp:1422-1458`, `meter_detector.py:5047-5084`, `AutomationEngine.h:98-113`, `simple_meter_reader.py:2710-2760`, `RemotePlaySession.cpp:2280-2290`

---

## SECTION D: Dual Detection Engine — Python vs C++ Synchronization

**Status:** Architecture risk — needs audit.

The system has TWO detection/automation engines that can run simultaneously:

1. **Python sidecar** (`controller_remap.py`): `ControllerRemap` class with its own `ShotContext`, `HoldState` state machine (IDLE/WARMUP/ARMED/HOLDING/GREEN_WINDOW/RELEASING/COOLDOWN), `_check_predictive_release()`, `_green_tracker`, `_sampler`, and `_feedback_offset_ms` self-learning loop. Runs in the sidecar process, directly modifies controller input before it reaches the console.

2. **C++ native** (`AutomationEngine.cpp`): `AutomationEngine` class with its own `ShotState`, `GreenWindowTracker`, `TemporalSampler`, `TemplateArrivalEstimator`, and `learnFromOutcome()` self-learning loop. Runs in the native app, emits controller state via `OrionInput`.

Both receive the same meter detection data and both can trigger releases.

**Potential issues:**
1. **Double-release** — If both engines fire for the same shot, controller state may be corrupted (double button-up, stick flick conflicts).
2. **Conflicting learning** — Python adjusts `_feedback_offset_ms` based on peak fill vs target. C++ adjusts `shotTypeLearnedOffsetMs` based on post-release meter evaluation. These can diverge, each pulling timing in different directions.
3. **State desynchronization** — Python's `HoldState` and C++'s `ShotState` can get out of sync if events are lost or delayed in the stdout pipe.
4. **Which engine is authoritative?** — C++ appears primary (receives telemetry, emits controller state), but Python also processes input. Need to clarify which is active.

**What to do:**
1. Determine which engine is active in production — check for env vars or config flags that gate the Python release path.
2. If both active, document the interaction — how do they avoid double-release? Is one the "input hook" and the other "automation"?
3. Audit the learning loops — if both learn simultaneously, they may fight. Disable one engine's learning when the other is authoritative.
4. Check for race conditions — both may write to the same virtual controller.

**Key files:** `controller_remap.py:1038-1130` (Python release), `AutomationEngine.cpp:2230-2315` (C++ release), `AutomationEngine.cpp:4689-4965` (C++ learning), `controller_remap.py:1217-1260` (Python learning), `AutomationEngine.h:90-130` (C++ config), `controller_remap.py:167-246` (Python ShotContext)

---

## SECTION E: Green Window Detection

**Status:** Tuning needed.

The `GreenWindowTracker` in `AutomationEngine.cpp:502-595` confirms a green window after 1 stable frame (3 fresh green readings within 3.0% tolerance). The `adaptiveTargetPct()` aims for the window's TOP edge (dead-top targeting) minus `tipMarginPct` (currently 0.0 = dead top).

**Potential issues:**
1. **Fast meters** — Arrow2 Purple green window is only ~30-40ms (a couple of frames). The 1-stable-frame confirmation may still be too slow if the green window passes between frames. Consider sub-frame interpolation or predictive green entry.
2. **Contested windows** — When the green window is a sliver (< 3% width), the tracker may never confirm it. Fallback is `fullMeterTargetPct = 100.0` (aim for the top). This works but may be suboptimal for fades.
3. **Width-based targeting** — The old `visibleGreenWidthPct = 8.0` split between center and tip targeting. The new `adaptiveTargetPct` always aims dead-top. Verify this doesn't cause systematic early releases on wide green windows.
4. **Green window memory** — `meter_detector.py:2992-2997` tracks green window offset and full height across frames/shots. If meter style or camera zoom changes, this memory may be stale. The `_track_green_left` counter invalidates it, but the timeout may be too long.

**What to do:**
1. Audit the confirmation threshold — is 1 stable frame (3 readings) enough for fast meters? Consider 0 (confirm on first valid green reading) for very fast meter styles.
2. Check the dead-top targeting — verify `tipMarginPct = 0.0` is correct for all shot types. Fades may benefit from 2-3% margin for late-safety.
3. Test with narrow green windows (< 5% width) — verify the tracker confirms and the bot times the release correctly.

**Key files:** `AutomationEngine.cpp:502-595`, `AutomationEngine.h:270-284`, `meter_detector.py:2992-2997`, `controller_remap.py:1078-1105`

---

## SECTION F: Release Timing & Self-Learning

### F1: Per-Shot-Type Clock Learning

**Status:** Working but complex — needs audit.

The system has multiple release timing paths:

1. **Vision-timed** (primary): ETA to green window from the detector's kinematic fusion, latency-compensated by `effectiveLatency` (pipeline lead + frame age + per-type offset + learned latency).
2. **Feedforward clock** (fallback): Per-shot-type deterministic clock (`shotTypeFeedforwardMs` or `shotTypeMeterToReleaseMs`), anchored to either hold-start or meter-appear time.
3. **Fused fire** (experimental): Posterior-based release combining vision ETA with the feedforward clock via Bayesian blend.
4. **Timeout** (last resort): `tempo_fallback_timeout_ms` fires if no green window is detected in time.

The learning loop (`learnFromOutcome`) has:
- **Per-type ACQUIRE→LOCK** controller: Fast convergence (annealed gain) for first few shots, then micro-trim when locked.
- **Global latency correction**: Type-agnostic bias correction.
- **Fade-specific clock learning**: Fades are carved out of the global clock and learn on their own per-type clocks.
- **Divergence guard**: Freezes integration when the grader rails at a clamp with the same sign.
- **Artifact detection (RC-4c)**: Detects the dead-top meter's two-mode fixed-value histogram (alternating LATE+66/EARLY-90) and freezes.

**Potential issues:**
1. **Double-count risk** — `learnFromOutcome` explicitly avoids nudging both offset AND clock for one outcome (TIMING FINDING 3). But the Python `controller_remap.py` has its own `_feedback_offset_ms` learning. If both learn from the same shot, the total correction doubles.
2. **Grader reliability** — The dead-top meter self-grade is unreliable. The RC-4 fix freezes the global lead against this, but only when `authoritativeVerdict_` is false. Verify the oracle (HUD feedback text) is reliable and available.
3. **Tempo mode parity** — `D3` fix ensures tempo buckets fall back to normal-mode clocks when unlearned. But the first tempo outcome trims from button parity, which may cause a timing jump on the first tempo shot.
4. **Feedforward anchor A/B** — `feedforward_anchor` can be "hold_start" or "meter_appear". The meter-appear anchor is less variable but relies on a clean low-latency feed. If the feed is jittery (capture card hiccups), it may be unreliable.
5. **Fused fire commit lock** — The fused fire mode allows re-scheduling until `fusedCommitLockMs` before the deadline. After that, the decision is locked. Verify this doesn't cause last-second jitter.

**What to do:**
1. Audit which learning loop is active — if C++ `AutomationEngine` is authoritative, the Python `_feedback_offset_ms` loop should be disabled to avoid double-counting.
2. Verify the grader oracle — check that `authoritativeVerdict_` (HUD feedback text) is reliable and correctly gated. The dead-top self-grade must NOT move the lead.
3. Test the fade clock learning — verify it converges and doesn't oscillate.
4. Check the `offsetCapMs` clamp — if a type needs more lead than the cap allows, it will be structurally late. Verify the cap is wide enough.

**Key files:** `AutomationEngine.cpp:4689-4965` (learnFromOutcome), `AutomationEngine.cpp:2230-2315` (release decision), `AutomationEngine.h:90-310` (timing config), `controller_remap.py:1038-1130` (Python release), `controller_remap.py:1217-1260` (Python learning)

### F2: Self-Learning Grader — Dead-Top Meter Artifact

**Status:** Fixed with RC-4 — needs monitoring.

The self-learning grader evaluates post-release meter position to compute a timing error. The dead-top meter (meter at 100% after release) produces an unreliable self-grade because it can't distinguish green from late at the top.

The RC-4 fix:
- **RC-4a**: The `calibrationFrozen` gate now sits ABOVE the global-lead mutation (freeze actually freezes).
- **RC-4b**: Only an `authoritativeVerdict_` (reliable HUD feedback text oracle) may move the lead. The dead-top self-grade is diagnostic-only.
- **RC-4c**: Artifact detection via two-mode fixed-value histogram (alternating LATE+66/EARLY-90) freezes regardless of sign.

**What to do:**
1. Monitor for LATE-66 leaks — check logs for any `learnedLatencyMs` movement from self-grade (not oracle) verdicts.
2. Verify the artifact detector — `isArtifactOutcomeSignature()` at `AutomationEngine.cpp:4655-4687`. Test with real session data to confirm it catches the artifact and doesn't false-positive on a genuinely diverging stream.
3. Check the divergence streak guard — `calDivergenceGuardShots` triggers silent recalibration. Verify the threshold is not too sensitive or too lenient.
4. Verify the fused fire freeze — under `fusedFireEnabled`, `learnFromOutcome` returns early. Confirm the fused path has its own learning mechanism.

**Key files:** `AutomationEngine.cpp:4655-4687`, `AutomationEngine.cpp:4689-4816` (autonomous vision learning), `AutomationEngine.cpp:4817-4965` (per-type ACQUIRE→LOCK)

### F3: Fused Fire Mode — Posterior-Based Release

**Status:** Experimental — needs validation.

The fused fire mode (`fusedFireEnabled` config flag) blends the vision ETA with the feedforward clock via a Bayesian posterior. The release deadline is `mu_tip - aim - MEASURED lead` instead of the legacy `ETA - effectiveLatency`.

**What to do:**
1. Determine if fused fire is enabled in production — check the config flag's default and recent sessions.
2. Verify the measured latency bootstrap — `measuredLatencyMs` starts at 0 with n==0. The fused fire should fall back to the legacy path until the measurement is confident.
3. A/B test fused vs legacy — compare release timing accuracy (early/late/green distribution) using session logs.
4. Check grading v2 — `gradeV2Enabled` routes the trim directly to `shotTypeLearnedOffsetMs` inside `evaluatePostReleaseMeterV2`. If grading v2 is not implemented, the fused path has no learning. Verify it exists and is functional.

**Key files:** `AutomationEngine.cpp:2254-2267` (fused fire re-scheduling), `AutomationEngine.cpp:4689-4698` (fused fire learning freeze), `AutomationEngine.h:298-310` (feedforward config), `RemotePlaySession.cpp:2301-2310` (measured latency IPC)

---

## SECTION G: Watchdog Reliability

### G1: Sidecar Restart Watchdog — False Trips

**Status:** Partially fixed — needs monitoring.

The native app has multiple watchdogs that can restart the sidecar:

1. **Frame feed stall** (`OrionAppController.cpp:3100-3113`): Restarts after 12s of no frames. Gated by `lastWatchdogRestartMs_` (30s cooldown).
2. **Input hook down + video stalled** (`OrionAppController.cpp:6438-6480`): Restarts after `kHookDownRecoverSecs_` seconds of input hook down + stalled video (either arrival stalled OR pixels frozen).
3. **Frozen echo detection** (`OrionAppController.cpp:6442-6454`): Uses `pixelAge > kHookDownPixelFrozenMs_` to detect frozen pixels.

**What to do:**
1. Monitor restart frequency — check logs for "Watchdog: restarting detection sidecar". If more than once per session (excluding genuine freezes), the watchdog is false-tripping.
2. Verify the grace period after restart — `frameStallSinceMs_ = 0` is set during a grace period. Verify it's long enough for the capture card to start producing frames (3-5 seconds for Elgato HD60 X).
3. Check cascading restarts — the 30s cooldown prevents immediate re-trips, but if the sidecar takes > 30s to stabilize, a second restart may fire before the first one stabilizes.
4. Check safe mode entry — how many restarts trigger safe mode? Is the threshold configurable?
5. The churn gate (`OrionAppController.cpp:6429-6437`) prevents restarting when input hook is down but video is healthy (ViGEm/XUSB fallback carries input). Monitor if the ViGEm fallback is reliable.

**Key files:** `OrionAppController.cpp:3100-3113`, `OrionAppController.cpp:6420-6480`, `SidecarWatchdog.h`

### G2: Frame Feed Stall Detection — 12s Threshold

**Status:** May be too long for capture card mode.

The frame feed stall watchdog triggers a sidecar restart after 12 seconds of no new frames. This was designed for Remote Play mode where a 12s stall indicates a dead stream.

**What to do:**
1. Consider mode-specific thresholds — use a shorter stall threshold (5-6s) for capture card mode, keep 12s for Remote Play mode.
2. Add user-facing feedback — show a message in the UI ("Capture signal lost — restarting...") when the watchdog fires.
3. The stall detection uses `frameAge` (refreshed by duplicate frames) — a frozen echo would NOT trigger this watchdog because frame_age stays low. The `pixelAge` check is only in the input hook recovery path. Consider adding `pixelAge` to the main stall detection.
4. The 30s cooldown after a restart means the user waits 30s + 12s = 42s before the next restart attempt if the first one didn't fix the issue. Consider a shorter cooldown for capture card mode.

**Key files:** `OrionAppController.cpp:3090-3113`, `OrionAppController.cpp:6442-6454`

---

## Summary of Priorities

| Priority | Issue | Status |
|----------|-------|--------|
| **HIGH** | A1: Verify C++ SharedMemoryFrameReader reads SHM frames | SHM writes working, C++ read unconfirmed |
| **HIGH** | A2: Confirm frame_shm events received by C++ | Under investigation |
| **HIGH** | C1: Frozen echo detection bypassing freshness gate | Partially fixed, needs verification |
| **HIGH** | D: Dual detection engine (Python + C++) synchronization | Architecture risk, needs audit |
| **HIGH** | C3: Meter memory echo — stale fill served as fresh | Partially fixed, needs verification |
| **MEDIUM** | B1: Stale preview frame on launch | Fix applied, needs test |
| **MEDIUM** | B2: Post-disconnect resume delay | Reduced 3000→1500ms, needs test |
| **MEDIUM** | A3: BGR/RGB format verification | Potential issue, needs check |
| **MEDIUM** | A4: SHM reader reopen on sidecar restart | Potential issue, needs retry logic |
| **MEDIUM** | E: Green window detection — narrow/contested windows | Tuning needed |
| **MEDIUM** | F1: Release timing — per-shot-type clock learning | Working but complex, needs audit |
| **MEDIUM** | C2: Stall watchdog — pixel_age threshold tuning | Working, may need adjustment |
| **MEDIUM** | G1: Sidecar restart watchdog — false trips | Partially fixed, needs monitoring |
| **MEDIUM** | G2: Frame feed stall detection — 12s threshold | May be too long for capture card |
| **LOW** | B3: Orphaned sidecar cleanup | Partially fixed, needs Job Object |
| **LOW** | A5: SHM write performance optimization | Working, can be optimized |
| **LOW** | A6: Unused ctypes bindings cleanup | Cleanup needed |
| **LOW** | A7: Diagnostic logging cleanup | Cleanup needed |
| **LOW** | B4: stopSidecar timeout | Working, can be optimized |
| **LOW** | F2: Self-learning grader — dead-top meter artifact | Fixed with RC-4, needs monitoring |
| **LOW** | F3: Fused fire mode — posterior-based release | Experimental, needs validation |

---

## Instructions for the AI

1. **Read all key files listed above** to understand the full context before proposing any changes.
2. **For each issue**, determine if the fix is correct, incomplete, or needs a different approach.
3. **Provide concrete code changes** (not just suggestions) for each issue.
4. **Prioritize HIGH issues first** — verifying that the C++ side can actually read SHM frames is the most critical remaining step.
5. **Consider edge cases**: sidecar restarts, capture card disconnect/reconnect, app crashes, race conditions between writer and reader, frozen HDMI signals.
6. **Maintain backward compatibility** — the JPEG fallback path must still work if SHM fails. Older sidecars that don't send `pixel_age_ms` must not break.
7. **Do not indiscriminately kill all python.exe processes** — use targeted detection (window title, command line pattern, or PID).
8. **Do not introduce regressions or new freezes** — test changes against the existing working paths.
9. **Test verification**: provide specific log patterns to grep for to confirm each fix works.
10. **Ask clarifying questions** if anything is ambiguous — better to ask than to guess wrong on a complex system.
