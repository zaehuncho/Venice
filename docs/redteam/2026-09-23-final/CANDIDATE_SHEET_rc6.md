# Frozen candidate sheet: rc6 (2026-09-24)

rc6 is rc5 plus the fixes from the external red team's on-disk round (triage: `docs/redteam/2026-09-24-external/EXTERNAL_TRIAGE.md`):
- **Packet bridge retired (owner):** VeniceNetSvc, the WinDivert driver pair and the service registration no longer ship. The installer retires any bridge service an earlier build registered, on install and on uninstall.
- **Analysis-tool lock:** matches exact executable names. Windows Magnifier and AIDA64 no longer falsely lock automation.
- **Binaries:** production binaries record only the PDB file name, and the developer-tree fallbacks are compiled out.
- **Backend (not yet deployed):** `/api/license/check` echoes only the key suffix.

- **Source:** NexusVision `136b668`, plus the Pascal comment fix in the commit after it; fork `67eec725`.
- **Package:** packaged and signed by the owner on 2026-09-24. It holds 571 manifested files and passed the integrity check. The installer build refuses a bridge and confirmed no service registration.
- **Frozen copy:** `C:\Users\aaron\VeniceRC\rc6-20260924` (573 files, identical to `release/orion-package`).
- **Installer:** unsigned by owner decision (`-AllowUnsigned`).
- **Tests:**
  - backend: 443;
  - native: 36/36 in the dev build and 36/36 in the production build (with Qt on PATH);
  - packaging and installer: 267.
- **Sidecar:** the hash differs from rc5 because Codex's StrictSecurity run rebuilt it this morning; the packager confirmed it is source-bound. OrionStream is unchanged.

| Item | SHA-256 |
|---|---|
| `VeniceSetup-1.0.0.exe` | `c787ea2e5fe754d0d8a92161b1b18a8dcc482a3d791b777e2057147fc1ef2010` |
| `orion-package-1.0.0.zip` | `1c1955dbf18e4c1e7ba757017f66247138850193bd2938a30d2ce533e3145aed` |
| `update_manifest.json` | `3029ae009f8d0dd8638fb8af551f6adb7f32f06f4ec5dbd91442cb17b23b801a` |
| `orion-package/release_manifest.json` | `a0ab526117f071d8c9d0432e995e2a6ee9ef48e7286b6a3d8d447ac59e26cdc1` |
| `orion-package/OrionNative.exe` | `a6f9138207be156bebbbeafc887acdc9b9effaff2aeb7d277fc17e3f173d9847` |
| `orion-package/OrionUpdater.exe` | `b02176bf1766c9da9fddf90156dbbe86b0168451eec3769afd38067864ba179c` |
| `orion-package/OrionSidecar.exe` | `c116d4d75ba40dd9f13a9bb73777aa71b98dc3b2bf2b8657d679d235282582dd` |
| `orion-package/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | `1f659a46570facbb12f505bda8b61aaf65e9a522c06c6dbea470e5e48c180897` |

## Not yet run
- StrictSecurity on rc6.
- The final sandbox red team (all four testers, full scope); prompts are in `C:\RedTeam\vm-reports\<tester>\PROMPT_FINAL.md`.
- An installed rc6 play test. It also checks that upgrading removes the old service on the owner's PC.
