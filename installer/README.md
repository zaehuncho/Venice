# Orion installer

The Orion release ships as a **signed installer**, not a standalone exe — the product loads
kernel-mode drivers (ViGEmBus, HidHide, WinDivert) that cannot be baked into an exe. See
`docs/IP_PROTECTION_PLAN.md` Phase E for the full rationale. This replaces the legacy
`Orion Setup.bat`.

## What it does
1. Runs the **ViGEmBus** and **HidHide** driver installers as silent, elevated steps (skipped if the
   driver service is already present).
2. Lays down the **signed package** from `tools/package_orion_release.py` into `Program Files\Venice`:
   `OrionNative.exe` + the 6 Orion DLLs + Qt runtime + `OrionStream.exe` (the fork) +
   `OrionSidecar.exe` (the Nuitka-compiled sidecar) + its dependency dir + the shipped model subset +
   assets + the **`packet_bridge\` bundle** (wave 3: the C++ `VeniceNetSvc.exe` +
   `WinDivert64.dll`/`.sys` — the Nuitka `NexusVisionSvc.exe` Python bundle no longer ships, and an
   `[InstallDelete]` entry purges it from upgraded installs). The packager's crown-jewel `.py`
   name-gate guarantees no readable reader source is present.
3. Registers the **inbound meter-delay packet bridge** (`RegisterPacketBridgeService`, run from
   `[Code]` at `ssPostInstall` with sc-exit-code checking, the same way the VC++ redist is checked):
   `VeniceNetSvc` is created **demand-start** as LocalSystem with `--arm-meter-delay` in its
   `binPath` **plus** `ORION_METER_DELAY_ARMED=1` written to the per-service `Environment` registry
   value — in SCM service mode the C++ service reads its arm switch from the environment, not from
   the binPath flag (`venicenet_service/main.cpp` `runService`). Any legacy `NexusVisionSvc`
   registration from an older install is stopped + deleted **first** (both bind exclusive TCP 47291).
   The DACL is `sdset` so Interactive Users can START/STOP it (the unelevated app sc-starts it on
   demand; commanding it is still bearer-token gated). **WinDivert itself needs no separate vendor
   installer** — its `WinDivert64.sys` kernel driver is registered/loaded on demand by
   `WinDivert64.dll` the first time the elevated service opens a handle. The uninstaller stops and
   deletes both service names from `[Code]` (`CurUninstallStepChanged`), waiting out the
   asynchronous stop.
4. Creates Start-menu (and optional desktop) shortcuts, a proper uninstaller, and a fixed install dir
   so the `OrionUpdater` auto-update path is stable.

## Build
```
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 [-AllowUnsigned]
```
(Inno Setup 6+.) Produces `installer\Output\VeniceSetup-<version>.exe` (plus
`Output\orion.preprocessed.iss`, the translated script the build verifies the `VeniceNetSvc`
registration strings against). The script reads the
version from `release\orion-package\release_manifest.json` (packager output; ultimately
`native_orion/CMakeLists.txt` PROJECT_VERSION), SHA-256-verifies the driver redists, checks the
Microsoft Authenticode signature on `vc_redist.x64.exe`, and refuses an unsigned package unless
`-AllowUnsigned` (dev only). A raw `iscc.exe installer\orion.iss` now fails on purpose: the
script injects `/DMyAppVersion` and the compile-time gates reject the `0.0.0-dev` placeholder
and a `SourcePackageDir` without `release_manifest.json`.

## Driver redistributables (in `installer\redist\`, gitignored — re-download per machine)
Bundled from the official nefarius releases (pinned; validate before a public release). Both are
vendor-signed `.exe` bundles that accept `/quiet /norestart` — `orion.iss` is wired to these exact
filenames:

| Driver | Version | File | SHA-256 |
|---|---|---|---|
| ViGEmBus | 1.22.0 | `ViGEmBus_1.22.0_x64_x86_arm64.exe` | `89220a7865076b342892f98865f3499fb7c4cfd673159e89d352c360fd014c6a` |
| HidHide  | 1.5.230 | `HidHide_1.5.230_x64.exe` | `f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6` |

Re-download on a fresh checkout (they're gitignored — never committed):
```
curl -sL -o installer/redist/ViGEmBus_1.22.0_x64_x86_arm64.exe https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/ViGEmBus_1.22.0_x64_x86_arm64.exe
curl -sL -o installer/redist/HidHide_1.5.230_x64.exe            https://github.com/nefarius/HidHide/releases/download/v1.5.230.0/HidHide_1.5.230_x64.exe
curl -sL -o installer/redist/vc_redist.x64.exe                  https://aka.ms/vs/17/release/vc_redist.x64.exe
```
`build_installer.ps1` verifies the two driver SHA-256 pins and the Microsoft Authenticode
signature on `vc_redist.x64.exe` before compiling.

**`vc_redist.x64.exe` is REQUIRED, not optional**: the Orion binaries are built `/MD` (dynamic
CRT) and `windeployqt` runs without `--compiler-runtime`, so the package carries no
`msvcp140.dll`/`vcruntime140.dll` — on a clean Windows machine `OrionNative.exe` fails at load
with a missing-DLL dialog before any Orion code runs. The installer runs the redist silently,
skipped when the runtime is already present (registry check).

## What YOU must supply before building a real release
- **A signed package**: run the release pipeline (`scripts\verify_orion.ps1 -StrictSecurity`, which
  requires the Ed25519 release key via `--signing-key`/`ORION_UPDATE_SIGNING_KEY_PEM`) so
  `release\orion-package\` contains signed binaries + `release_manifest.sig`. `build_installer.ps1`
  refuses an unsigned package without `-AllowUnsigned`.
- **The version**: nothing — it is read from the package's `release_manifest.json` automatically.
  To bump it, change `PROJECT_VERSION` in `native_orion/CMakeLists.txt` and re-package.
- **EV code-signing** for the installer exe itself: pass the full signtool command, e.g.
  `-SignToolCommand 'signtool.exe sign /fd SHA256 /a /tr http://timestamp.digicert.com /td SHA256 $f'`
  (the cert/HSM is yours to supply). An EV-signed installer earns SmartScreen reputation; an
  unsigned one trips Defender. See `docs/CODE_SIGNING.md`.
- **The three redists** in `installer\redist\` (download commands above).

## Order in the full release flow
1. Nuitka-compile the sidecar → `OrionSidecar.exe` (recipe in `docs/IP_PROTECTION_PLAN.md`; verified
   feasible — 196 MB numpy+cv2 bundle, no torch).
2. Native `startSidecar()` change to spawn the compiled exe + set `ORION_SIMPLE_READER=1` in the
   shipped sidecar env (Phase A/C).
3. `tools/package_orion_release.py` assembles + signs the package (bundles `OrionSidecar.exe`; the
   crown-jewel `.py` name-gate rejects any leaked reader source).
4. `iscc installer\orion.iss` builds the installer; sign it EV.
5. Final packaging red-team checklist (`docs/IP_PROTECTION_PLAN.md`).
