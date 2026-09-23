# Updater end-to-end harnesses (2026-09-21)

Run the REAL OrionUpdater.exe headless against signed manifests and grade its own log.
Both stop before any download on purpose (the artifact step needs HTTPS); what they prove is
the trust root, signature, downgrade and HTTPS gates on the compiled binary.

- `updater_e2e_dev.py`  - dev build (native_orion/build/Release): signed manifest verifies via a
  pubkeys file from install_dir and from ORION_UPDATE_PUBKEYS; tampered / unknown-key / no-file /
  downgrade all refuse. 9 checks.
- `updater_e2e_prod.py` - PRODUCTION build (native_orion/build_prod_review/Release, configure with
  -DORION_PRODUCTION=ON): `--build-profile` attests profile=production + embedded key ids,
  `--manifest-file` refused, a foreign `--install-dir` refused and audit-logged, and (when a manifest
  is published) rogue keys from cli/env/install_dir are logged as ignored while the embedded key
  verifies. Writes/removes update_pubkeys.json + orion_updater.log inside that Release dir only.

Env: ORION_QT_BIN (default C:/Users/aaron/Qt/6.8.0/msvc2022_64/bin), ORION_PROD_UPDATER_DIR.
Needs the repo .venv (cryptography). Exit code 0 = all checks passed.
