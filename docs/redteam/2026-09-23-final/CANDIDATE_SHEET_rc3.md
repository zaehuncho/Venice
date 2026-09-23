# Frozen candidate sheet: rc3 (2026-09-23)

rc3 is rc2 plus the owner's play-test fixes (commit `4dc80b1`):
- **Meter Delay is shelved.** The installed rc2 engaged a 250 ms delay from the historical default `meter_delay_enabled=true`, which caused early shots and "lost track of the shot".
- **Smoother installer glow.** Frames are preloaded, and the progress page no longer animates.

Everything else is as in `CANDIDATE_SHEET_rc2.md`: the same fork, sidecar, updater and signing key, and the same `artifact_url` (`https://venice-releases.s3.amazonaws.com/releases/orion-package-1.0.0.zip`).

- **Source:** NexusVision `4dc80b1` (pushed); fork `67eec725`.
- **Frozen copy:** `C:\Users\aaron\VeniceRC\rc3-20260923` (577 files: 575 manifested plus the manifest and signature). Integrity check PASS; `security_audit` OK; installer gate OK.

| Item | SHA-256 |
|---|---|
| `VeniceSetup-1.0.0.exe` | `836c487bb3307c86b788cd2b3f69e579757ccf98df8a5e9a934eef68082549cb` |
| `orion-package-1.0.0.zip` | `6229e67e2bebdf2b9cbfdbadb5a23badc0aa988bed6506d0f795ae75f9044874` |
| `update_manifest.json` | `12ebe88b0d738ebdb701889d9f1d3f4156a746f0b76e876b0f368c2b95b66719` |
| `orion-package/release_manifest.json` | `eb2bd8ed1c679d9f23a548867be914215786ac84576b1bd766adb93aa9b55dac` |
| `orion-package/OrionNative.exe` | `84d77b50f8f8706c47bee32896b6aef0c6302c49640d4f8952fbd4592aea25de` |
| `orion-package/OrionUpdater.exe` | `63a3def1cf0dc805a85109af0dda65019b4f79ab436b645790b8120cfe3686e8` |
| `orion-package/OrionSidecar.exe` | `405ae51462fb1acebb89d675495c803da967aaa55ad54745b4f720d7e591e524` |
| `orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | `1f659a46570facbb12f505bda8b61aaf65e9a522c06c6dbea470e5e48c180897` |

**Tests:**
- Native ctest 35/35 (dev).
- Focused meter-delay and UI Python tests 59/59.

**Not yet run:** StrictSecurity on rc3, the VM canary and a packaged play test (owner).
