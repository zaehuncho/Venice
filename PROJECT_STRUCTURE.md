# NexusVision/Orion Project Structure

## Root Directory

```
NexusVision/
├── native_orion/          ← C++ Qt6 application (OrionNative.exe + DLLs)
├── vendor/                ← Third-party dependencies (chiaki, ViGEm, etc.)
├── tests/                 ← Python test suite
├── scripts/               ← Build automation scripts
├── tools/                 ← Utilities (diagnostics, admin, security, packaging)
├── docs/                  ← Documentation (code signing, release, setup)
├── meter_styles/          ← JSON configs for meter detection
├── assets/                ← Icons and resources
├── codesigning/           ← Release signing configuration
├── logs/                  ← Runtime logs
├── .vault/                ← DPAPI-encrypted secrets (master.key, entitlement cache)
├── .windsurf/             ← IDE workspace metadata
└── __pycache__/           ← Python bytecode cache
```

## Core Python Modules (Root)

| File | Purpose |
|------|---------|
| `controller_remap.py` | Shot automation state machine (Idle→Warmup→Armed→Holding→Releasing) |
| `meter_detector.py` | OpenCV meter detection (HSV-based, fill %, green window) |
| `remote_play_orchestrator.py` | Orchestrates CV + controller + RTT sync |
| `remote_play_cv.py` | CV pipeline glue (green window analyzer, ghost engine, adaptive ROI) |
| `remote_play_client.py` | Chiaki launcher/manager |
| `rtt_sync_engine.py` | RTT measurement + Kalman filter for latency compensation |
| `virtual_controller.py` | ViGEmBus virtual controller wrapper |
| `chiaki_backend.py` | Frame/audio data structures |

## Configuration & Data Files

| File | Purpose |
|------|---------|
| `settings.json` | User settings (DPAPI-encrypted) |
| `settings.json.sig` | Settings integrity signature |
| `learning.json` | Auto-learned per-shot-type offsets |
| `license_cache.enc` | Local entitlement cache (DPAPI-encrypted) |
| `version.json` | App version metadata |
| `court_profiles.json` | Court/profile configurations |
| `orion_bw_centroids.json` | Calibration data |
| `requirements.txt` | Python dependencies |

## Native Orion (C++)

```
native_orion/
├── src/                   ← C++ source files
│   ├── OrionAppController.cpp/h    ← Main controller, wires everything
│   ├── AutomationEngine.cpp/h      ← Shot timing state machine
│   ├── MeterDetector.cpp/h         ← C++ meter detection (OpenCV)
│   ├── VirtualController.cpp/h     ← ViGEm controller I/O
│   ├── SecurityManager.cpp/h       ← VM/debugger detection, manifest validation
│   ├── NetworkBridge.cpp/h         ← Packet bridge IPC
│   ├── RemotePlaySession.cpp/h     ← Python sidecar subprocess manager
│   ├── LicenseClient.cpp/h         ← HTTPS license activation
│   ├── AppConfig.cpp/h             ← Configuration management
│   └── main.cpp                    ← Entry point
├── qml/                   ← Qt6 QML UI (pages, components)
├── backend/               ← Python sidecar scripts
│   ├── autogreen_sidecar.py        ← Long-running sidecar (JSONL IPC)
│   └── ps5_remoteplay_helper.py    ← PS5 pairing/registration helper
├── build/                 ← CMake build output
│   └── Release/           ← OrionNative.exe + DLLs
├── tests/                 ← C++ unit tests
└── CMakeLists.txt         ← CMake build configuration
```

## Build Output (native_orion/build/Release/)

| File | Purpose |
|------|---------|
| `OrionNative.exe` | Main executable (Qt6 GUI) |
| `AutomationCore.dll` | (Planned) Automation engine DLL |
| `VisionCore.dll` | (Planned) Vision/detection DLL |
| `RemotePlayCore.dll` | (Planned) Remote play session DLL |
| `SecurityCore.dll` | (Planned) Security/licensing DLL |
| `OrionCommon.dll` | Shared utilities DLL |
| `ViGEmClient.dll` | Virtual controller driver |
| `Qt6*.dll` | Qt6 runtime libraries |
| `opencv*.dll` | OpenCV runtime libraries |

## Tests

```
tests/
├── test_controller_remap.py        ← Automation state machine tests
├── test_meter_detector_motion.py   ← Meter detection tests
└── test_orchestrator_capture.py    ← Orchestrator integration tests
```

## Scripts

```
scripts/
└── verify_orion.ps1                ← Release verification (build + tests + security)
```

## Tools

```
tools/
├── package_orion_release.py        ← Release packaging
├── verify_remote_play_backend.py   ← Backend verification
├── install_remote_play_backend.ps1 ← Backend installer
├── chiaki/                         ← Chiaki-specific utilities
└── diagnostics/                    ← Diagnostic tools
    ├── rawinput_probe.py           ← RawInput device diagnostics
    └── analyze_orion_recording.py  ← Recording analyzer
```

## Documentation

```
docs/
├── CODE_SIGNING.md                 ← Code signing guide
├── ORION_REMOTE_PLAY_SETUP.md      ← Remote play setup instructions
└── RELEASE_SECURITY.md             ← Release security checklist
```

## Vendor Dependencies

```
vendor/
├── chiaki-orion/                   ← Modified Chiaki PS5 remote play client
├── vigem/                          ← ViGEmBus virtual controller driver
└── (other third-party libraries)
```

## Logs

```
logs/
├── orion_native.log                ← Runtime log (C++ + Python)
├── archive/                        ← Archived logs (auto-rotated when >1MB)
└── diagnostics/                    ← Diagnostic logs
```

## Vault (Encrypted Secrets)

```
.vault/
├── master.key                      ← DPAPI-protected master key
├── secrets.vault                   ← Encrypted secrets
└── orion_entitlement.cache         ← Cached entitlement
```

---

## Build Commands

```powershell
# Native build
cmake --build native_orion\build --config Release --target OrionNative OrionNativeTests

# Native tests
ctest --test-dir native_orion\build -C Release --output-on-failure

# Python tests
pytest tests -q

# Strict security verification
scripts\verify_orion.ps1 -StrictSecurity
```

## Launch Commands

```powershell
# Native launcher (C++ Qt6)
native_orion\build\Release\OrionNative.exe

# Python sidecar (standalone testing)
.venv311\Scripts\python.exe native_orion\backend\autogreen_sidecar.py --root . --config-json "{...}"
```

---

**Total Files:** ~1900  
**Core Python Modules:** 8  
**C++ Source Files:** ~24  
**QML UI Files:** ~30  
**Test Files:** 3  
**Documentation:** 3  
**Build Artifacts:** OrionNative.exe + 5 DLLs + Qt6/OpenCV runtimes
