# Orion Native

Remote Play-first computer-vision timing application for Windows.

The active tree is now Orion-focused. Legacy Nexus/PyQt/server/reverse-analysis
source is not part of this repository. The historical migration record is kept at:

`ORION_CLEANUP_MANIFEST.json`

## Active Runtime

- `native_orion/` - Qt6/C++ Orion launcher, QML UI, native vision, automation, controller, Remote Play, and security cores.
- `native_orion/backend/` - Python sidecars started by Orion for frame/telemetry support.
- `remote_play_orchestrator.py` - sidecar orchestration used by `native_orion/backend/autogreen_sidecar.py`.
- `controller_remap.py`, `virtual_controller.py` - controller state/remap compatibility layer used by the sidecar and tests.
- `chiaki_backend.py`, `remote_play_client.py`, `remote_play_cv.py` - Chiaki/window capture and CV support.
- `meter_detector.py`, `rtt_sync_engine.py` - active sidecar meter detection and RTT sync support.
- `meter_styles/` - meter profile data.
- `vendor/` - Chiaki/WinDivert/vendor material still used by Orion tooling.
- `tools/diagnostics/` - local diagnostic scripts, including saved-recording meter analysis.

Removed legacy/reference modules are not required for Orion startup or Remote
Play sidecar operation. Historical destinations are recorded in the cleanup
manifest.

## Preserved Local State

The following are kept because Orion still uses them locally:

- `settings.json`
- `settings.json.sig`
- `license_cache.enc`
- `.vault/`
- `codesigning/`
- `native_orion/build/Release/OrionNative.exe`

Generated logs, diagnostics, caches, build outputs, release staging, and archives
are ignored by Git and may be retained locally while launch evidence is still
needed. They are not release-package inputs; archive or remove them only after
the corresponding test evidence and rollback window are no longer required.

## Verify Orion

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1
```

That checks:

- native Orion Release build and tests
- controller remap tests
- Remote Play orchestrator capture tests
- active sidecar Python modules compile

For a strict pre-ship security/package gate:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -StrictSecurity
```

That also creates `release/orion-package`, generates `release_manifest.json`,
adds production `security_policy.json`, and fails if symbols, test binaries,
local settings, vault files, logs, DBs, caches, or other non-ship artifacts leak
into the package.

Release security details are documented in `docs/RELEASE_SECURITY.md`.

## Launch Orion

```powershell
$orionExe = (Resolve-Path .\native_orion\build\Release\OrionNative.exe).Path
Start-Process $orionExe
```

The tracked launch instructions intentionally do not enable local authentication
bypasses. Development-only credentials and launch overrides belong in ignored
`*.local.ps1` scripts or environment configuration, never in source-controlled
documentation or a release package.
