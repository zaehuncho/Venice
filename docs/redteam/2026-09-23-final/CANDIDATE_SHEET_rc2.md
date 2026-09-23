# Frozen candidate sheet: rc2 (2026-09-23)

rc2 is the patched unit after the final internal red team blocked rc1 (`RED_TEAM_REPORT.md`). The patches are in `patches/P-A..P-F`. There are no credentials or keys in this sheet.

## Source and fork

| Repo | Commit | Tree state |
|---|---|---|
| `C:\Users\aaron\Desktop\NexusVision` | `08cfa5a` (branch `fix/timing-input-and-remoteplay-blockers`, pushed to the private origin) | clean except `learning.json` (the owner's runtime timing data, deliberately uncommitted) |
| `C:\Users\aaron\Desktop\chiaki-ng-src` | `67eec725` (branch `orion`, local), unchanged since rc1 | fork unchanged. `OrionStream.exe` is the same `1f659a46` |

## Candidate (frozen copy; do not modify)

- **Frozen package:** `C:\Users\aaron\VeniceRC\rc2-20260923\orion-package`. It holds 577 files: 575 manifested, plus the manifest and its signature. `DEPLOY_LOG` is now excluded.
- **Installer:** `C:\Users\aaron\VeniceRC\rc2-20260923\VeniceSetup-1.0.0.exe`.
  - It is **not Authenticode-signed**. That is the owner's decision for the beta.
  - It uses the one-screen Venice look (`installer/ui/venice_ui.iss`).
  - The install path is fixed to Program Files.
- **Update:** `orion-package-1.0.0.zip` and `update_manifest.json`. The signed `artifact_url` is `https://venice-releases.s3.amazonaws.com/releases/orion-package-1.0.0.zip`, so the empty-URL finding (CL3-F1-002 / RT-MED-11) is closed in bytes.
- **Built from:**
  - `native_orion/build_prod_review/Release` (`ORION_PRODUCTION=ON`, hardening ON);
  - the sidecar at `build/sidecar/autogreen_sidecar.dist`, rebuilt after the capture patch ("compiled sidecar matches current sources"; DML smoke median 16.1 ms);
  - the fork at `build-orion-optimized-ffmpeg7`.
- **Signing key ID:** `orion-ed25519-v1`.
- **Not Lethe-packed.**

| Item | SHA-256 |
|---|---|
| `VeniceSetup-1.0.0.exe` | `95d3b1b1d28ac0f9d44de4ac405d896122b296833f57303f296a93e6f66c1ea8` |
| `orion-package-1.0.0.zip` | `8b5afbbbea871a7a4e93d96df6b9ee392b5a5eaae2d6f2d6cd83de98dfab0361` |
| `update_manifest.json` | `2bd6dc07411af266640002bfdab80d72565e33cbec71c94c2e47f1a2e416c6c5` |
| `orion-package/release_manifest.json` | `993b7182ed9b6265c5d674c786e2140cd22fe5619ad6806b7437c74b9ef03a29` |
| `orion-package/release_manifest.sig` | `4a67b471d1841f9c35951aba8be6eac507ce1d82b96cfcf4dd69a9b2984bb77f` |
| `orion-package/security_policy.json` | `1ffa1ef7ba357d806481dbaf1b6b408aa4ccd82c7f0c5da04a0f5c32356a5188` |
| `orion-package/OrionNative.exe` | `f7f6197b37e80ada65ed62ab37e3fe57f05c71d29cc481e7ad945366f9f620aa` |
| `orion-package/OrionUpdater.exe` | `63a3def1cf0dc805a85109af0dda65019b4f79ab436b645790b8120cfe3686e8` |
| `orion-package/OrionSidecar.exe` | `405ae51462fb1acebb89d675495c803da967aaa55ad54745b4f720d7e591e524` |
| `orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | `1f659a46570facbb12f505bda8b61aaf65e9a522c06c6dbea470e5e48c180897` |
| `orion-package/packet_bridge/VeniceNetSvc.exe` | `ba1b3d0b893884a82ea780dbbca897fafea7e44da1e2defd387c47bebf2fbf66` |

| Native DLL | SHA-256 |
|---|---|
| `OrionCommon.dll` | `dd65ef592e4cf85c9ec197bf03de12f55ca2badaa0ef72e44cfbba3ef7cfdfba` |
| `AutomationCore.dll` | `395bb2c22fe61454e522e9c1bae020f95345f9d7fde24b270d5c9c8199e27549` |
| `VisionCore.dll` | `e043ca4ba1d956ec8ac664268d7e30f6caac42b8732690c7f1cab58331c84990` |
| `RemotePlayCore.dll` | `519b7721173a1a0eb8716325498560fffc8b616165729b4b75285442d4d3ecb8` |
| `SecurityCore.dll` | `7577cdf757310bad48ba08d4a75d62c527b3458c4983d65389e6f2cbaededc73` |
| `UpdaterCore.dll` | `c568fd5705c936a097dd6a946f9cf1b1ea09d2a8acfb41ab803ee253b6f99fdb` |
| `VeniceNet.dll` | `e61238832d0853dbb3aca796ddbea3fd332ac596ffe2c5c0c482eacf9d939b87` |

## Gate record

| Gate | State |
|---|---|
| Native ctest (dev, same source) | 35/35 |
| Python broad suite (offline guard) | green after five tests were updated to the new stricter behaviour: uninstall, packer, updater fixture version, bot trial |
| Backend | 435 passed |
| Worker | 50/50 |
| Package integrity + signature | PASS, 575 files |
| `security_audit.py --package-only` | OK |
| Installer build gate (exact package) | OK |
| Standard verify on rc2 | **not run** |
| StrictSecurity on rc2 | **not run** (owner) |
| VM canary (Codex P-B five steps) | **not run** (owner) |
| Packaged-unit play test | **not run** (owner) |

## Owner decisions applied

- Unsigned beta installer, with SmartScreen steps and the published SHA-256.
- The trial starts on the website.
- Xbox is shown as experimental.
- Remote Play is fully supported.
- Updates are hosted in the `venice-releases` S3 bucket.
- Calibration applies and saves its value on an Auto install.

## Known residuals (accepted for beta or owner-gated)

- **A6-003:** three-path publication is failure-atomic, not literally atomic.
- **Capture:** the device is still opened by index; the before/after enumeration narrows the window to the open call itself.
- **Signatures:** the settings signature is still a keyless SHA-256 (tamper evidence only).
- **Admin:** the owner-bearer is single-factor while TOTP is not enrolled.
- **Not implemented:** the fleet meter signal and a detection-only switch. See `docs/support/PATCH_DAY_RUNBOOK.md`.

**Deploy prerequisites:**
- `orion-nonces` TTL on `ttl`.
- Lambda role permissions: `DeleteItem` on `orion-nonces`, and SSM `Get`/`Put`/`Delete` on `/orion/owner_totp_pending_secret`.
- `ORION_ARTIFACT_ALLOWLIST=venice-releases.s3.amazonaws.com/releases`.

**Reviewer note:** use `C:\Users\aaron\Desktop\NexusVision\.venv\Scripts\python.exe`. `C:\Python314` does not exist.
