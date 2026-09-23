# Frozen candidate sheet: rc1 (2026-09-23)

Give this same sheet to all four reviewers (Codex Security, Codex Reliability, Gemini, Claude). It contains no credentials or keys.

## Source and fork

| Repo | Commit | Tree state |
|---|---|---|
| `C:\Users\aaron\Desktop\NexusVision` | `59129e5` (branch `fix/timing-input-and-remoteplay-blockers`, pushed to the private origin) | clean except `learning.json` (owner's runtime timing data, deliberately uncommitted) |
| `C:\Users\aaron\Desktop\chiaki-ng-src` | `67eec725` (branch `orion`, local commit only) | all product source committed; untracked scratch scripts, logs and build dirs remain (not product source) |

## Candidate (frozen copy, do not modify)

- Frozen package tree: `C:\Users\aaron\VeniceRC\rc1-20260923\orion-package` (578 files: 576 manifested plus the manifest and its signature)
- Installer: `C:\Users\aaron\VeniceRC\rc1-20260923\VeniceSetup-1.0.0.exe` (Inno Setup; **not Authenticode-signed**)
- Update ZIP and manifest: in the same folder
- Built from: `native_orion/build_prod_review/Release` (`ORION_PRODUCTION=ON`, `ORION_PRODUCTION_HARDENING=ON`); sidecar `build/sidecar/autogreen_sidecar.dist` (the manifest verifier reports "compiled sidecar matches current sources"); fork `build-orion-optimized-ffmpeg7`
- Release manifest signing key ID: `orion-ed25519-v1` (the production key; manifest signature verified at package time, 576 files)
- **Not Lethe-packed yet.** Lethe packing is a later "pack and protect" step; this candidate is the unpacked production package.

| Item | SHA-256 |
|---|---|
| `VeniceSetup-1.0.0.exe` | `d0383f12c867936a24040e108b5b8866f1be7307f8e07ba19f2f8bdc614f8d50` |
| `orion-package-1.0.0.zip` | `209bd83f8f4e5a767dba3f35bd9bd01b6210d3788d81395881cd4c8e167e7567` |
| `update_manifest.json` | `2652b3d64991df8b357f70ded3e9c2ca22753f440098c5a7da7457cb91deef5f` |
| `orion-package/release_manifest.json` | `1083ea693f937612d51ba2ee467f2b8a0e0fe240ad08c280b6a03976fe06e950` |
| `orion-package/release_manifest.sig` | `63acba1c2d676e60fd42fd04bd14ec5af6888122f7ea4c9960b70bfeb53e524d` |
| `orion-package/security_policy.json` | `1ffa1ef7ba357d806481dbaf1b6b408aa4ccd82c7f0c5da04a0f5c32356a5188` |
| `orion-package/OrionNative.exe` | `40d7afb49059dd158ba8306d4149011327cb0256b48d7ff77847be95a1e3711d` |
| `orion-package/OrionUpdater.exe` | `838cace7ff739d37f377f2b27701560aed4a0cf334c7cd1d0b958c036f1e6e1b` |
| `orion-package/OrionSidecar.exe` | `6e3bf77f58374e22f8683eef31ee482c320c4a54afd312819dade94a5197451e` |
| `orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | `1f659a46570facbb12f505bda8b61aaf65e9a522c06c6dbea470e5e48c180897` |

Native DLL closure (package root):

| DLL | SHA-256 |
|---|---|
| `OrionCommon.dll` | `a1191e59deefe1ebbb835f84c840f6f89780e37b22fbf02876e1b3b0f439bfa6` |
| `AutomationCore.dll` | `d34c62e18801a3c51c78e9ec706f6f7c08609f9715ca7bd01fb59237c42bca51` |
| `VisionCore.dll` | `8e20c47500b792cf3107cb9b1b6f5f1d09e97b9f450bd585a43ea13da05c01c8` |
| `RemotePlayCore.dll` | `c507a658625dffe27ebe8f48d59024e4442c8b49f995b7ffa0a6094cf438731f` |
| `SecurityCore.dll` | `22a56359efbfe6d7c1b730b240cea755d840ebf39bd315d6c3f90fc3eb985ed8` |
| `UpdaterCore.dll` | `0e1da6ac80b0463b6e58df8740feb9a52cf8611f5c59276cdf735b6472bc4b2b` |
| `VeniceNet.dll` | `e61238832d0853dbb3aca796ddbea3fd332ac596ffe2c5c0c482eacf9d939b87` |

## Baseline and rollback

- Previous package: none approved. The 09-21/09-22 packages were dev or stale units and are **not** a known-good baseline.
- Clean VM snapshot ID: **owner to supply**
- Disposable profile and update lab: `tests/test_updater_e2e_disposable.py` fixtures; owner VM for the real canary.

## Test scope

- Mocks and fixtures: backend (375 tests, offline guard), Worker (`website/tests/worker.test.mjs`, 47), fork real-pipe harness (`orion-bridge-pipe-test`), dummy input sink fixtures.
- Hardware rig: the owner's PS5, HD60 X capture card and DualSense (owner-run only).
- No live third-party targets. A separate non-production account or lease and a standard-user VM: **owner to confirm**.

## Gate record

| Gate | State |
|---|---|
| `docs/audit/2026-09-23/FIXUP_REPORT.md` | present |
| Native ctest (dev tree, same source) | 32/32 passed |
| Broad Python (`tests`, not slow, offline guard) | 4,308 passed, 0 failed |
| Backend | 375 passed (Codex fixup run) |
| Worker | 47/47 |
| Fork ctest | 4/4 (`build-codex-a3-20260923`, same source as the deployed fork) |
| Package integrity + signature | PASS, 576 files |
| `tools/security_audit.py --package-only` | OK |
| Production binaries contain no `ORION_LEAD_FLOOR_MS` / `ORION_LEAD_BIAS_MS` / `ORION_ONSET_FF` strings | verified |
| Standard verify on this candidate | **not run** |
| StrictSecurity on this candidate | **not run** (owner) |
| VM install/update/rollback canary | **not run** (owner) |
| Owner play test | `session_20260923_120043`: 25 graded, EXC 80 / E 0 / L 20, 0 disconnects, on the **dev** launcher `D2A9920F` with the same fork `1f659a46`. **Not** on this production package. |

## Known and accepted for beta (owner to confirm)

- A6-003: three-path publication is failure-atomic, not literally atomic.
- A4-001: capture identity is inventory-gated; the opened handle is not attested across a same-second hot-plug.
- Unsigned installer (SmartScreen); copy explains it and publishes the SHA-256.
- Reported and not reproduced in Venice's input stream: Square appearing held in-game (the pipe shows every release sent plus 5 re-sends). A controller stick hovering PC windows (no mapper app running).

## Reviewer note

Verify commands in the prompt pack say `C:\Python314\python.exe`, which does not exist on this PC. Use `C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe`.
