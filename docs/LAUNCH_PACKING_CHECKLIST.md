# Launch Packing Checklist — Venice / Orion 1.0.0

The exact, ordered commands the owner runs to turn the verified source tree into a
**signed, shippable installer**. Everything up to (and including) the strict release gate has
already been run and passes; the owner's remaining work is the pack + installer build.

> **Scope note (read first).** This document is the mechanical pack/build recipe. It assumes
> the *release gate* (`scripts\verify_orion.ps1 -StrictSecurity`) has passed. As of
> 2026-09-19 it passes end-to-end (`[orion] OK`, `STRICT_EXIT=0`); see
> `D:\NexusVision\pack_prep\strict.log`.

---

## 0. Provisional-build warning (BLOCKING — 2026-09-19)

A separate agent is finalizing **server-side lease/manifest signing and may change embedded
public-key constants** in the client (`native_orion/src/LeaseGate.cpp` `leaseVerifyPublicKey`,
and possibly the pinned manifest/update key). Per `docs/LAUNCH_LIVE_AUDIT_2026-09-19.md` (B2/B5):

* Production builds force the **lease gate ON** (`LeaseGate::enabledFromEnvironment` returns
  true under `ORION_PRODUCTION_BUILD`). If the license Lambda cannot sign leases, a production
  client **stops firing ~15 min after unlock**.
* SSM `/orion/manifest_signing_key` reportedly does not match the client's pinned release key.

**Consequence for packing:** the production build and the signed package produced by the
2026-09-19 gate run are **PROVISIONAL**. The pinned keys are baked into the compiled binaries
*and* into `release_manifest.sig` / `update_manifest.json`. If any key constant changes, the
**production build, the package, and both signatures must all be regenerated** (re-run Step 1).

Do **not** run the final pack/installer for public release until:
1. The key constants in `LeaseGate.cpp` (and any pinned manifest/update key) are final and
   committed by the signing agent — **do not edit these yourself**.
2. Step 1 (the strict gate) has been re-run on those final sources.
3. The **lease-refresh smoke** in Step 6 passes (build keeps firing >20 min after unlock).

---

## Prerequisites (verified present on the build box, 2026-09-19)

| Requirement | Status | Path / value |
|---|---|---|
| Inno Setup 6 `ISCC.exe` | present | `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` |
| `signtool.exe` (Win SDK) | present (NOT on PATH) | `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe` |
| Lethe CLI | present | `C:\Users\aaron\Desktop\Lethe\lethe.py` (auto-detected; override with `LETHE_CLI`) |
| Ed25519 signing key (PEM) | present, matches pinned pubkey | `C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem` (EFS, ONLY copy) |
| `installer\redist\ViGEmBus_1.22.0_x64_x86_arm64.exe` | SHA-256 OK | pinned in `build_installer.ps1` |
| `installer\redist\HidHide_1.5.230_x64.exe` | SHA-256 OK | pinned in `build_installer.ps1` |
| `installer\redist\vc_redist.x64.exe` | Authenticode Valid (Microsoft) | required (package is `/MD`, no app-local CRT) |
| Version (single source) | `1.0.0` consistent | CMake `PROJECT_VERSION` → `venice.rc.in`, `release_manifest.json`, installer `AppVersion`, `update_manifest.json` |
| Sidecar bundle | fresh, verified | `python tools\sidecar_bundle_manifest.py --verify` → exit 0 |
| Build interpreter | Python 3.12 with Nuitka + `onnxruntime-directml==1.22.0` | `C:\Users\aaron\AppData\Local\Programs\Python\Python312\python.exe` |

### Environment gotchas that MUST be set for every gate/pack run
* **Redirect TEMP away from the poisoned pytest dir.** The default pytest base
  `C:\Users\aaron\AppData\Local\Temp\pytest-of-aaron` returns `Access is denied` on
  enumeration (WinError 5), which makes the gate's pytest step fail with ~276 setup **errors**
  that are *not* real test failures. Before any gate/pack/test run:
  ```powershell
  $env:TEMP = "D:\NexusVision\pack_prep\strict_tmp"; $env:TMP = $env:TEMP
  ```
* **Provide the signing key by env var** (path only, never the material):
  ```powershell
  $env:ORION_UPDATE_SIGNING_KEY_PEM = "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"
  ```
* C: has < 8 GB free. Keep gate/test scratch on D: (`$env:ORION_VERIFY_LOG_ROOT`, `TEMP`).

---

## Step 1 — Release gate (produces the signed package)  [ALREADY PASSING]

This builds the dev + production native trees, rebuilds the compiled sidecar, runs all tests
and audits, and emits the **signed customer package**. Re-run it whenever sources or key
constants change.

```powershell
$env:ORION_UPDATE_SIGNING_KEY_PEM = "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"
$env:ORION_VERIFY_LOG_ROOT = "D:\NexusVision\pack_prep\verify_logs"
$env:TEMP = "D:\NexusVision\pack_prep\strict_tmp"; $env:TMP = $env:TEMP
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 `
  -Python "C:\Users\aaron\AppData\Local\Programs\Python\Python312\python.exe" -StrictSecurity
```

**Success prints:** `[orion] OK` and `STRICT_EXIT=0`. Standard native `100% tests passed out
of 22`; production native `100% tests passed out of 22` (incl. `OrionShotVerdictTallyTests`).

**Outputs:**
* `release\orion-package\` — signed **customer** package (`release_manifest.json` +
  `release_manifest.sig`; `version=1.0.0`; ~2911 files). Omits `OrionOwner.exe`/`OrionStaff.exe`.
* `release\orion-package-1.0.0.zip` — the auto-update artifact.
* `release\update_manifest.json` — re-signed; `sha256` = the zip's hash; `artifact_url` **empty**.

**If it fails:**
* `~276 errors ... PermissionError ... pytest-of-aaron` → you forgot the `TEMP` redirect above.
* `production packaging requires --signing-key or ORION_UPDATE_SIGNING_KEY_PEM` → set the env var.
* `None of these interpreters has Nuitka plus pinned DirectML onnxruntime` → the build Python's
  `onnxruntime` is broken; reinstall the pin:
  `python -m pip install --force-reinstall --no-deps onnxruntime-directml==1.22.0`.

---

## Step 2 — Build the required Lethe server-shard package

> **ASSEMBLING + VERIFYING, fail-closed (2026-09-20 packer hardening).** The
> activation broker exists (round 3). `--server-shard` now ASSEMBLES the three-exe
> chain — Lethe-pack the app to `OrionNative.packed.exe`, stage the verified
> `LetheShardBootstrap.exe` as `OrionNative.exe`, stage the UNPACKED broker as
> `OrionActivate.exe` — signs the customer manifest, and the gate VERIFIES every
> shard essential is present AND manifest-covered, failing closed on any gap. The
> owner is **cert-free**: the broker is not Authenticode-signed; the Ed25519-signed
> release manifest that covers `OrionActivate.exe` is a package-INTEGRITY record and
> **NOT a sufficient sole bootstrap trust anchor** — a replacement broker can simply omit the self-check, and Windows loads the broker's app-local imports before its code runs (red team 2026-09-20; see the OPEN blocker in `docs/PACKING_RUNBOOK.md`). See
> `docs/SERVER_SHARD_2026-09-19.md` and `docs/ACTIVATION_BROKER_2026-09-19.md`.
>
> **RIG prerequisites** (do these before the shard pack): build
> `LetheShardBootstrap.exe` and the packed app on a **pinned clean Lethe commit**
> (the wrapper records packer provenance and REFUSES a dirty tree — pin with
> `--expected-packer-commit`), build the round-3 `OrionActivate.exe`, and provide
> the `VeniceSigning` Ed25519 key. Point the wrapper at these with
> `--bootstrap-exe`, `--broker-exe` (or the `LETHE_SHARD_BOOTSTRAP` /
> `ORION_ACTIVATE_BROKER` env vars). Shard/edge/TOTP secrets are supplied via the
> environment only (`LETHE_SHARD_ADMIN_SECRET`, `LETHE_SHARD_EDGE_AUTH`,
> `LETHE_SHARD_ADMIN_TOTP`, `LETHE_SHARD_PIN_PEM`) — never on the command line.

The release command is shard-enabled so omission cannot silently ship the
unprotected executable. On a machine missing any chain input, the expected result
is the explicit fail-closed refusal and **no output package mutation**.

### Option B — Lethe-pack first (extra tamper-resistance)
The pack wrapper's own smokes launch `OrionOwner.exe`/`OrionStaff.exe`, so it must run against
the **full internal package** (which contains them), *not* the customer package Step 1 leaves
behind. Regenerate the full package, then pack it:

```powershell
# 2b-i. Regenerate the FULL package (includes Owner/Staff) at release\orion-package
"C:\Users\aaron\AppData\Local\Programs\Python\Python312\python.exe" tools\package_orion_release.py `
  --strict --no-customer --skip-archive --build-dir native_orion\build_prod_codex\Release

# 2b-ii. Pack it with the mandatory server-shard release gate.
"C:\Users\aaron\AppData\Local\Programs\Python\Python312\python.exe" tools\security\pack_lethe_release.py `
  --server-shard `
  --signing-key "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"
```

On a complete rig (chain inputs present + `VeniceSigning` key), success prints the
internal admin OK, `[orion-pack] customer startup integrity OK`, `[orion-pack] tamper
refusal OK: ...` for each class, then `[orion-pack] OK`, and atomically promotes
`release\orion-package-packed\` (packed inner `OrionNative.packed.exe`, bootstrap staged
as `OrionNative.exe`, unpacked `OrionActivate.exe`, signed **customer** manifest with
Owner/Staff stripped) plus `release\orion-package-packed-<v>.zip`,
`release\update_manifest.json`, and the REDACTED `release\orion-packing-report.json`.

Shard pack command (rig):

```powershell
"C:\...\python.exe" tools\security\pack_lethe_release.py `
  --server-shard `
  --bootstrap-exe C:\Users\aaron\Desktop\Lethe\bootstrap\build\Release\LetheShardBootstrap.exe `
  --broker-exe native_orion\build_prod_codex\Release\OrionActivate.exe `
  --expected-packer-commit <pinned-clean-lethe-commit> `
  --signing-key "C:\Users\aaron\Desktop\VeniceSigning\venice_update_signing.pem"
```

`--expected-packer-commit` is **mandatory** for a production (production-key) pack, and
`--allow-dirty-packer` is refused there; both relaxations require
`--non-production-build`, which refuses the production key and stamps
`"production": false` into the report. Re-check a finished package read-only with:

```powershell
"C:\...\python.exe" tools\security\pack_lethe_release.py --verify-only `
  --input release\orion-package-packed --server-shard
```

which enforces the full customer acceptance test (zero unmanifested files,
audience=customer, no Owner/Staff, fail-closed `security_policy.json`, profile-aware
executable admission) on top of the signature/hash check. The installer build runs the
same gate before ISCC and refuses to compile from an unverified package.

**Gotchas / mismatches to know:**
* Default pack targets are `OrionOwner,OrionStaff,OrionNative,OrionUpdater` — present only in
  the *full* package (2b-i). Running the wrapper against the customer package fails with
  `packer target is missing from package: OrionOwner.exe`.
* `OrionSidecar.exe` is a Nuitka standalone and is **not** a pack target — it ships unpacked.
* DLL packing is refused by policy (loader-lock). Do not add `.dll` to `--targets`.
* The installer's `[Files]` `Excludes: OrionOwner.exe,OrionStaff.exe`, so building from the
  packed *full* package still ships a customer-only install (packed Native/Updater + unpacked
  Sidecar); Owner/Staff never reach the customer.

**If it fails:** `--packer-command is required ... Lethe CLI not found` → set `LETHE_CLI` to
`C:\Users\aaron\Desktop\Lethe\lethe.py`. `--signing-key does not match the embedded ... key`
→ wrong PEM. `production release packing is EXE-only` → you passed a non-`.exe` target.

---

## Step 3 — Build the installer

The default and only production input is `release\orion-package-packed`. The
installer script now defaults to that path; the explicit argument below makes
the release evidence unambiguous.

```powershell
$Sign = 'signtool.exe sign /fd SHA256 /a /tr http://timestamp.digicert.com /td SHA256 $f'
powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 `
  -PackageDir release\orion-package-packed `
  -SignToolCommand $Sign
```

* `signtool.exe` is not on PATH; either add `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64`
  to PATH for the session or pass the full path in `$Sign`. The EV cert/HSM is the owner's to supply.
* For a local **unsigned** test build only (never ship it), drop `-SignToolCommand` and add
  `-AllowUnsigned` (the latter only if the package itself is unsigned — the gate's package is signed,
  so `-AllowUnsigned` is not needed here).

**Success prints:** `[orion-installer] version 1.0.0`, `[orion-installer] service registration
verified in ...orion.preprocessed.iss`, `[orion-installer] OK`, plus the artifact path and SHA-256.

**Output:** `installer\Output\VeniceSetup-1.0.0.exe` (+ `installer\Output\orion.preprocessed.iss`,
the read-back the build verifies the `VeniceNetSvc` registration strings against).

**If it fails:**
* `MyAppVersion is 0.0.0-dev` / `SourcePackageDir does not contain release_manifest.json` → you
  pointed `-PackageDir` at a non-package folder; use the Step-1/Step-2 output dir.
* `release_manifest.sig missing ... UNSIGNED dev package` → the package isn't signed; re-run Step 1
  with the signing key (or, test-only, add `-AllowUnsigned`).
* `SHA-256 mismatch for <redist>` / `vc_redist ... failed Authenticode` → re-download that redist
  per `installer\README.md`.
* `iscc.exe not found` → install Inno Setup 6+, or pass `-Iscc "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"`.

---

## Step 4 — Publish the update channel (owner-only, post-build)

`release\update_manifest.json` is re-signed by Step 1 with the correct `sha256`, but its
`artifact_url` is **empty**. Before auto-update can serve this build:
1. Upload `release\orion-package-1.0.0.zip` to the update host.
2. Set `artifact_url` in `update_manifest.json` to that URL (this file is served by the backend
   / update worker — coordinate with whoever owns the update endpoint; do not hand-edit the
   signature). The `sha256` and `signature` already match the zip.

---

## Step 5 — Smoke-test the installed build on a clean path

1. On a clean VM/machine (or a fresh user profile), run `VeniceSetup-1.0.0.exe` as admin.
2. Confirm: ViGEmBus + HidHide install silently (skipped if already present), VC++ redist runs,
   `VeniceNetSvc` is registered **demand-start** with `ORION_METER_DELAY_ARMED=1`, Start-menu
   shortcut created, app launches from `C:\Program Files\Venice\OrionNative.exe`.
3. Confirm no dev artefacts landed: no `logs\`, `settings.json`, `learning.json`, `codesigning\`,
   `*.pdb`, or `packet_bridge\NexusVisionSvc.exe` under the install dir. (The strict gate's
   `scan_forbidden` + security audit already enforce this on the package; this is the on-machine
   double-check.)
4. Uninstall: confirm `VeniceNetSvc` (and any legacy `NexusVisionSvc`) is removed, WinDivert is
   stopped, and user data is kept unless you choose "Remove Venice data?".

---

## Step 6 — Lease-refresh smoke (BLOCKING for public release — 2026-09-19)

Because production forces the lease gate ON, the only end-to-end proof that server-side lease
signing works is duration:

1. Install and unlock the production build (Step 5) against the **live** license backend.
2. Let it run **> 20 minutes** of continuous operation after unlock without re-unlocking.
3. **Pass = it keeps firing past the ~15-minute lease horizon** (the lease refreshed). If it
   silently stops firing around 15 min, server-side lease signing is still broken — do not ship;
   return to the signing agent.

This must be run on the **final** build made after the lease/manifest/update key constants are
finalized (see Step 0).

---

## Pending owner-only items (summary)

- [ ] **Do not ship the provisional build.** Re-run Step 1 after the lease/manifest/update key
      constants in `LeaseGate.cpp` (and any pinned manifest/update key) are final (Step 0).
- [ ] **Version choice:** currently `1.0.0` (CMake `PROJECT_VERSION`). To bump, edit
      `native_orion/CMakeLists.txt` `project(OrionNative VERSION x.y.z)` and re-run Step 1.
- [ ] **EV code-signing cert/HSM** for the installer exe (`-SignToolCommand`). Unsigned installers
      trip SmartScreen/Defender.
- [ ] **Off-box backup of the signing key.** `venice_update_signing.pem` is the ONLY copy
      (EFS-encrypted, owner account only). Losing it means no future signed update can ship.
      (Also recoverable from AWS SSM `/orion/ed25519_private_key` via `scripts\fetch_signing_key.ps1`.)
- [ ] **Update host + `artifact_url`** (Step 4).
- [ ] **Lease-refresh smoke** (Step 6) on the final build.
- [ ] Signed outer activation broker implemented; shard acceptance (a)-(e) recorded PASS.

---

## Test-suite state at handoff (2026-09-19, informational)

The release gate (`scripts\verify_orion.ps1 -StrictSecurity`) passed end-to-end on a consistent
snapshot: standard native **22/22**, production native **22/22** (incl. `OrionShotVerdictTallyTests`),
Python gate subset **1492 passed**, packaging + audits + owner/staff smokes all green,
`STRICT_EXIT=0`.

A concurrent agent was editing `native_orion` C++/QML/test sources *during* this pass
(`AutomationEngine.cpp`, `AutomationEngineTests.cpp`, `PreviewPresentationBufferTests.cpp`,
`RhythmCard.qml`, `DashboardPage.qml`, `FirstRunTour.qml`, etc. — all uncommitted). Re-running
tests afterward is therefore a moving target:

* **Dev `ctest -C Release` (`native_orion\build`): 22/22 PASS** once the two targets the
  concurrent edits invalidated are rebuilt (`cmake --build native_orion\build --config Release
  --target OrionNativeTests OrionPreviewPresentationTests`). Before the rebuild, stale binaries
  reported 2 failures — a binary-vs-source drift, not a code defect.
* **Full Python suite (`pytest tests --ignore=tests/discord`): 3235 passed, 3 failed.**
  - `test_idle_scan_cost.py::...strided_one` — a perf/timing test; passes 16/16 in isolation
    (it flaked only because ctest was saturating the CPU concurrently).
  - `test_non_live_production_surface_contract.py::test_setup_keeps_backend_contracts_and_has_no_customer_performance_label`
    (line 53: `assert 'title: "Profiles"' in dashboard`) and
    `::test_sidebar_and_tour_only_name_current_customer_surfaces` (line 172) — **deterministic**
    drift from the in-flight UI refactor: `native_orion/qml/pages/DashboardPage.qml` and
    `FirstRunTour.qml` were edited to remove the "Profiles" surface / change the tour text, but
    the contract test was not fully reconciled. These are source-only assertions (no rebuild
    fixes them) and are **owned by the UI work, not by packing**. They do not affect the built
    binaries or the signed package.

**Action for the owner:** whoever owns the UI refactor must reconcile the QML with
`tests/test_non_live_production_surface_contract.py` and land the `AutomationEngine`/QML edits,
then the final production build (Step 1) must be re-run so the shipped binaries match committed
sources (this is also required by the Step 0 provisional-build rule). The sidecar bundle,
version wiring, redists, and installer plumbing are all confirmed correct and unaffected.

## Documentation drift found while walking the runbooks (2026-09-19)

These do not block the build (the scripts auto-resolve or override), but the prose is stale:

* `docs/PACKING_RUNBOOK.md` lines 12/18/37/45 reference `C:\Python314\python.exe`, which does
  **not** exist on this box (only Python 3.12 / 3.11). `verify_orion.ps1` auto-resolves the
  interpreter, so this is cosmetic — use the Python 3.12 path above.
* `docs/PACKING_RUNBOOK.md` line 21 says keep `native_orion\build_prod\Release` as the internal
  debug build, but the production tree the gate actually uses is `native_orion\build_prod_codex`.
* `docs/PACKING_RUNBOOK.md` lines 37-40 show a placeholder packer command
  (`PackerConsole.exe ... -profile ...`); the wrapper now defaults to the Lethe CLI, so the
  real command is just `pack_lethe_release.py --signing-key <pem>` (Step 2b-ii).
* Neither runbook documents the `TEMP` redirect required to dodge the poisoned
  `pytest-of-aaron` dir, nor the customer-vs-full package interaction between
  `pack_lethe_release.py` (needs Owner/Staff) and the gate's final `--customer` output.
