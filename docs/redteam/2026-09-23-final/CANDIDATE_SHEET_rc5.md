# Frozen candidate sheet: rc5 (2026-09-24)

rc5 is rc4 plus remembered sign-in:
- **Client** (`a8b054b`): the canonical key is stored DPAPI-protected and submitted automatically at launch. The launcher gains a Sign out option.
- **Backend** (`9485796`): a bound machine resumes without a PAIR code.

The backend has been live since 2026-09-24 05:21Z, after the missing `orion-config` and `orion-ratelimit` tables were created. `ORION_ARTIFACT_ALLOWLIST=venice-releases.s3.amazonaws.com/releases` is set on the Lambda.

- **Source:** NexusVision `9485796` (native unchanged since `a8b054b`); fork `67eec725`.
- **Package:** packaged and signed by Codex at 2026-09-24 05:48Z. The frozen copy is identical to `release/orion-package` (`diff -rq` clean).
- **Installer:** built at 06:01Z from the frozen package. It is **unsigned by owner decision** (`-AllowUnsigned`).
- **Frozen copy:** `C:\Users\aaron\VeniceRC\rc5-20260924`. It holds 577 files.
- **Checks:**
  - integrity: 575/575 manifested files;
  - package audit: PASS;
  - installer gate: OK (service registration verified);
  - native ctest: 36/36;
  - the sidecar matches its source.
- **Meter finder:** the CV contour locator. The ONNX file ships unused, as in rc4.
- **artifact_url:** `https://venice-releases.s3.amazonaws.com/releases/orion-package-1.0.0.zip`
- **Manifest:** `public_key_id` is `orion-ed25519-v1`; not mandatory; `allow_rollback` is false.

| Item | SHA-256 |
|---|---|
| `VeniceSetup-1.0.0.exe` | `5f1e946da59b9875418cbfbfc94cbae85bae3d6455d6039a15b0cec39ae97a76` |
| `orion-package-1.0.0.zip` | `6e3c10e8e173210b17a32a817dc6eb5ceb224af0da52b1ba1892e1e89381d6f2` |
| `update_manifest.json` | `6f30b6ba1f6127f4970283fe39186fe11e020ac003886c01c529bc6f5d3982fe` |
| `orion-package/release_manifest.json` | `9468c450069690709026d86d4421690356c22da1b02fa026d61c374f15a035e7` |
| `orion-package/OrionNative.exe` | `112205caeef2430385163f8f882808eddb14e1ae671e1835c8a7fdebcd95e5e6` |
| `orion-package/OrionUpdater.exe` | `5ae2cf9b15f64c08af81cbd84243f74ca061f8ed71db79006e4f4a19e44d0882` |
| `orion-package/OrionSidecar.exe` | `405ae51462fb1acebb89d675495c803da967aaa55ad54745b4f720d7e591e524` |
| `orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | `1f659a46570facbb12f505bda8b61aaf65e9a522c06c6dbea470e5e48c180897` |

## Not yet run
- StrictSecurity on rc5 (the owner runs it).
- The VM canary (Codex P-B, 5 steps).
- An installed rc5 play test, including remembered sign-in across a relaunch.
- The external red team (`docs/redteam/2026-09-24-external/`). Use this sheet's installer hash as `CANDIDATE_SHA256`.
