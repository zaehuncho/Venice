# Frozen candidate sheet: rc4 (2026-09-23)

rc4 is rc3 plus two things:
- **Ship parity** (`6607eca`). The owner's validated dev timing is now built in: aim 271 frozen, anchor-20, per-type trim, two-frame ownership, tip-phase solo, no curve stretch, plus a v3 settings migration.
- **Installer fixes** (`1dc3300`): a flat hero canvas, and the hero frames first in the archive, which fixes the slow start.

Everything else is as in rc2/rc3: the same fork, the same key, and the same `artifact_url` (`https://venice-releases.s3.amazonaws.com/releases/orion-package-1.0.0.zip`).

- **Source:** NexusVision `6607eca` (pushed); fork `67eec725`.
- **Frozen copy:** `C:\Users\aaron\VeniceRC\rc4-20260923`. It holds 577 files: 575 manifested plus the manifest and its signature.
- **Checks:** integrity PASS, `security_audit` OK, installer gate OK.
- **Meter finder:** the CV contour locator (pure CV). The packaged ONNX (n3_pill, `6af3199c`) is not loaded for Arrow2; it ships only because the production start gate requires it (post-launch: remove it).

| Item | SHA-256 |
|---|---|
| `VeniceSetup-1.0.0.exe` | `45e0bbd80f459e6a318344ab539f952725bf4161cc6d6962da96972c1f63c356` |
| `orion-package-1.0.0.zip` | `979461fe0bf36ec5d87fc466bdcd970ccb1ec6c09ef4ef90130b31ed90fe575c` |
| `update_manifest.json` | `cfc4d124fd43da36e4ea635770555baca5785bb0a133821991fbf695d244cc61` |
| `orion-package/release_manifest.json` | `a16b8f99f4dc40263af0c2d6a583b12c102c329ddbbd157629ed4c2f3fe94a78` |
| `orion-package/OrionNative.exe` | `308fdc628e23cdbd8d33c85284a8e1f2a5c80305f5be0033e12d0acfc69aefa7` |
| `orion-package/OrionUpdater.exe` | `513cf5bdb582b2f2f3693a0eb7b5ef7d5f4727a051b1fce4641432875afa6733` |
| `orion-package/OrionSidecar.exe` | `405ae51462fb1acebb89d675495c803da967aaa55ad54745b4f720d7e591e524` |
| `orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | `1f659a46570facbb12f505bda8b61aaf65e9a522c06c6dbea470e5e48c180897` |

## Tests and play data
- **Native ctest:** 35/35.
- **Python broad suite:** green.
- **A/B on 2026-09-23** (dev code identical to rc4 at B): B at 9:10 PM gave 65.5% EXC and 10% early (n=29). A (morning code) gave 80% EXC and 0% early (n=15). The release point was identical (fill about 37%), so the 5 PM earlies were the connection, not the code.

## Not yet run
- StrictSecurity on rc4.
- The VM canary.
- An installed rc4 play test.
- The external red team.
