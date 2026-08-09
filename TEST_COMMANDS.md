# TEST_COMMANDS.md — Canonical Verification & Test Commands

All commands run from the repo root: `C:\Users\Administrator\Desktop\NexusVision`.
Default Python is `C:\Python314\python.exe` (has `cv2` + `pytest`). Do **not** depend
on the `cosmic_env` conda environment.

---

## Authoritative gate (run after every loop round)

Standard verification — Python compile + sidecar pytest + native build + native ctest:
```
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1
```

Strict verification — adds the hardened-runtime package gate. Run this **only if** a
security / package / release file changed *and* `TASK.md` authorized it
(`ALLOW_SECURITY_FILES: yes`):
```
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -StrictSecurity
```

`verify_orion.ps1` accepts `-Python <path>` if you need a specific interpreter, e.g.:
```
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python C:\Python314\python.exe
```

---

## Fast subset (agents: run the slice relevant to your change)

Single regression file (the T1 active task target):
```
python -m pytest tests\test_stability_tracking.py -q
```

The sidecar pytest set that `verify_orion.ps1` currently runs:
```
python -m pytest tests\test_controller_remap.py tests\test_orchestrator_capture.py ^
  tests\test_meter_detector_motion.py tests\test_meter_detector_video.py ^
  tests\test_chiaki_decoder_geometry.py tests\test_orchestrator_feed_gate.py ^
  tests\test_roi_relock.py -q
```

Whole Python test directory:
```
python -m pytest tests -q
```

Python compile check (matches the verify script's first step):
```
python -m py_compile chiaki_backend.py controller_remap.py meter_detector.py ^
  remote_play_client.py remote_play_cv.py remote_play_orchestrator.py ^
  rtt_sync_engine.py virtual_controller.py tools\diagnostics\analyze_orion_recording.py
```

---

## Native (C++) build + tests

Build:
```
cmake --build native_orion\build --config Release --target OrionNative OrionNativeTests
```
Test:
```
ctest --test-dir native_orion\build -C Release --output-on-failure
```
> The native steps need Qt6 + OpenCV on `PATH`; `verify_orion.ps1` sets that up
> automatically. For tooling-only tasks (like T1) the Python subset is sufficient to
> prove the change, but the loop still runs the full standard gate.

---

## Test inventory (`tests/`)

| File | Covers |
|------|--------|
| `test_controller_remap.py` | Shot automation state machine |
| `test_meter_detector_motion.py` | Meter detection under motion |
| `test_meter_detector_video.py` | Meter detection on recorded video |
| `test_chiaki_decoder_geometry.py` | Decoder/capture geometry |
| `test_orchestrator_capture.py` | Orchestrator capture integration |
| `test_orchestrator_feed_gate.py` | Orchestrator feed gate |
| `test_roi_relock.py` | ROI re-acquire across court positions |
| `test_stability_tracking.py` | `_StabilityValidator` tracking latch (**not yet in verify script — see T1**) |

---

## Interpreting results

- **All green** → Codex may set `DECISION: approved` if the diff is also clean.
- **A plain assertion failure** → counts as a test failure; two failures across rounds
  halts the loop.
- **Native build step fails** (`[orion] Native build`) → treated as an *unclear build
  break*; the loop halts immediately.
- **Progress needs live PS5/Remote Play gameplay** → set
  `LIVE_VALIDATION_REQUIRED: yes` in `STATUS.md`; the loop halts for the human.
