# Orion Master Implementation Directive — Meter Lock, Tip Timing & RTT Sync

> **Instruction for Implementer (Claude / Primary Implementer):**
> You are implementing critical, production-grade upgrades to Orion across three key pillars: **(1) 100% Meter Detection Locking**, **(2) Tip Timing Precision & Feedback Calibration**, and **(3) RTT Stream Sync & Offline Test Fidelity**.
>
> Ground every change strictly in the file paths specified. All code changes MUST be flag-gated (default-OFF or fully backwards-compatible), 100% offline-provable via unit tests and regression gates, and engineered so they work live on hardware without regression.

---

## Technical Baseline & Ground Rules

1. **Repo Locations:** Main repo `C:\Users\aaron\Desktop\NexusVision`; Chiaki fork `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`).
2. **Flag Safety Standard:** New features must be flag-gated (e.g. via `_flag('ORION_READER_SHOT_ROI_LOCK', '0')` or `AppConfig` settings), default-OFF, and byte-identical when off.
3. **Guaranteed Release Invariant:** The hard release cap in `AutomationEngine.cpp` ($\le 1000\text{ ms}$) is sacred; no vision, network, or RTT logic may block a held shot from releasing.
4. **Offline Verification Requirement:** Every pillar must be verified 100% offline before live testing using:
   - `python tools/regression/run_gates.py` (51/51 gates PASS)
   - `pytest tests/`
   - `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python C:\Python314\python.exe -StrictSecurity`

---

## Pillar 1: Flawless Meter Lock (Zero In-Shot Blinks)

### Target File: `simple_meter_reader.py`

#### Change 1.1: Implement Hybrid Fixed-ROI Shot Lock (`ORION_READER_SHOT_ROI_LOCK`)
* **Problem:** Live H.264 stream compression (Elgato HD60X) causes strict red ($R \ge 220$) to briefly desaturate for 1-2 frames during meter rise. Per-frame contour re-acquisition emits `detected=False` or drops to `meter_memory`, causing a 16-event overlay blink and starving the engine's vision health ($\ge 3$ accepts / 150ms).
* **Fix:**
  1. Add flag `self._shot_roi_lock = _flag('ORION_READER_SHOT_ROI_LOCK', '0')`.
  2. Maintain state: `self._locked_roi = None` (bounding box `(x, y, w, h)`), `self._lock_frames = 0`.
  3. When armed (`self._armed_state is True`) and `consecutive_frames >= 2`, snapshot and lock the bounding box `self._locked_roi = bbox`.
  4. While `_shot_roi_lock` is active and `self._locked_roi` is set during an armed episode, **skip full-band per-frame contour re-acquisition**. Evaluate red/neon-green pixel distribution directly within `self._locked_roi`.
  5. Reset `self._locked_roi = None` when the shot-gate disarms or after $500\text{ ms}$ max duration.
* **Offline Proof:** Replay framedump `session_20260722_135817` through `simple_meter_reader.py`. Verify `within_shot_ghost_frames == 0` and `within_shot_detect_rate >= 0.98`.

#### Change 1.2: Robust Meter Height Denominator Gate (`ORION_READER_TRACK_H_ROBUST_GATE`)
* **Location:** `simple_meter_reader.py:2373` (`_track_h_hist` seed evaluation) and `simple_meter_reader.py:2924`.
* **Fix:**
  1. Replace bare `max(hist[-8:])` refusal check with median-capped robust ceiling:
     ```python
     robust_max = min(max(hist[-8:]), 1.15 * float(np.median(hist[-8:])))
     seed = full_cap and cand >= 0.90 * robust_max
     ```
  2. Do NOT clear `self._low_cand_hist` on temporary lock-drops at line 2924.
* **Offline Proof:** Run `python tools/regression/run_gates.py`. Verify zero `mid_rise_glitches` and 51/51 gates PASS.

---

## Pillar 2: Tip Timing Precision & Feedback Oracle Calibration

### Target File: `native_orion/src/AutomationEngine.cpp`

#### Change 2.1: Dynamic Latency Compensation for Fades
* **Location:** `AutomationEngine.cpp:2838-2839`.
* **Problem:** Fades are currently excluded from `effectiveLatency` network compensation (`!isFade ? effectiveLatency : ...`), receiving $0.0\text{ ms}$ network offset, while Standstills receive ~6.5ms.
* **Fix:** Update `ffOffset` calculation to include `effectiveLatency` for Fades:
  ```cpp
  const double ffOffset = globalApplies
      ? (isFade ? (effectiveLatency + shotTypeOffsetMs(shot_.bucketKey)) : effectiveLatency)
      : shotTypeOffsetMs(shot_.bucketKey);
  ```
* **Offline Proof:** Run native test suite (`OrionNativeTests.exe`). Verify `fadeUsesPerTypeClockWhileStandstillUsesGlobal` passes.

#### Change 2.2: Safeguard `useMeasuredLead` Offset Zeroing Trap
* **Location:** `AutomationEngine.cpp:2338-2358`.
* **Fix:** Ensure `heldOffsetMs` only zeroes out `networkOffsetMs` if `useMeasuredLead` is true **AND** `shot_.measuredLatencyValid` is explicitly true with non-zero sample history:
  ```cpp
  const bool validMeasuredLead = useMeasuredLead && shot_.measuredLatencyValid && shot_.measuredLatencySamples > 5;
  const double heldOffsetMs = validMeasuredLead ? 0.0 : std::abs(shot_.networkOffsetMs);
  ```

### Target File: `shot_feedback_reader.py`

#### Change 2.3: Feedback Word Reader ROI & Residual Mapping
* **Location:** `shot_feedback_reader.py:63` (`DEFAULT_FEEDBACK_BAND`) and `shot_feedback_reader.py:96-108` (`verdict_from_label`).
* **Fix:**
  1. Pin 1080p feedback crop region: `DEFAULT_FEEDBACK_BAND = (0.33, 0.16, 0.34, 0.10)`.
  2. Implement template matching in `crop_band` against feedback labels ("EXCELLENT", "SLIGHTLY EARLY", "SLIGHTLY LATE").
  3. Ensure `ShotFeedbackVerdict` outputs exact signed residuals ($18.0\text{ ms}$ per level) to `AutomationEngine::setShotVerdict`.
* **Offline Proof:** Run `pytest tests/test_shot_feedback_reader.py` (13/13 PASS).

---

## Pillar 3: RTT Sync & Stream Telemetry Integration

### Target File: Chiaki Fork `chiaki-ng-src/gui/src/streamconnection.c`

#### Change 3.1: Export Video Stream `q.rtt` Telemetry
* **Location:** `streamconnection.c:695-704`.
* **Fix:** Instead of logging and discarding `q.rtt` at line 704, write the current smoothed RTT float value into a shared memory block `OrionStreamStats` or emit via local IPC UDP packet on port 47292.

### Target File: `native_orion/src/OrionAppController.cpp`

#### Change 3.2: Wire Stream RTT to Native Telemetry
* **Fix:** Read the incoming stream RTT payload in `OrionAppController.cpp`, update `telemetry_.rttMs`, and pass it to `RttEstimator::updateRtt()`.

### Target File: `tools/regression/run_gates.py`

#### Change 3.3: Add Synthetic Noise Injection to Regression Suite
* **Fix:** Add `--synthetic-noise` flag to `run_gates.py`. When passed, apply JPEG re-encoding ($Q=75$) and 5% random frame drop simulation during framedump evaluation. Ensure baseline detection passes both clean and noisy gates offline.

---

## Step-by-Step Verification Protocol

Execute the following commands in sequence to guarantee 100% offline correctness before live testing:

```powershell
# 1. Run Python Unit Tests
.venv\Scripts\pytest.exe tests/

# 2. Run Offline Detection Regression Suite
.venv\Scripts\python.exe tools\regression\run_gates.py

# 3. Build & Run Native C++ Timing Test Suite
cmake --build native_orion/build --target OrionNativeTests --config Release
native_orion\build\Release\OrionNativeTests.exe

# 4. Run Strict Security & Release Invariant Verification
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python C:\Python314\python.exe -StrictSecurity
```

**Expected Result:** All tests pass, 51/51 detection gates pass, StrictSecurity passes zero errors.
