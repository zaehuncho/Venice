# Orion Native

Native Qt6/C++ rewrite workstream for the Orion launcher.

This tree is intentionally separate from the current Python launcher so the
native control panel, CV pipeline, automation state machine, and security
integration can mature without breaking the existing release path.

## Build

From a Visual Studio developer PowerShell:

```powershell
cd C:\Users\Administrator\Desktop\NexusVision\native_orion
& C:\Qt\6.8.0\msvc2022_64\bin\qt-cmake.bat -S . -B build `
  -DOpenCV_DIR=C:\Users\Administrator\Downloads\opencv\build\x64\vc16\lib
cmake --build build --config Release
ctest --test-dir build -C Release --output-on-failure
```

The app can configure without OpenCV for UI/security work, but release builds
for gameplay should be configured with OpenCV available.

## Current Scope

- Qt6 Widgets production-style shell with auth gate, dashboard, Remote Play,
  detection, automation, and security pages.
- C++ settings/learning readers compatible with `settings.json` and
  `learning.json`.
- C++ CV detector interface with OpenCV-backed HSV/contour green-window logic.
- C++ automation state machine for hold-to-release square/stick workflows.
- Dynamic ViGEm client loader for the virtual controller path.
- License client scaffold using Qt Network with TLS verification.

Native Remote Play protocol support is represented by `RemotePlaySession`.
It owns lifecycle and frame dispatch boundaries; a real frame receiver can be
plugged in behind that interface without changing the UI or automation engine.
