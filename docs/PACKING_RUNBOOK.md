# Orion Packing Runbook

Packing is a last-layer hardening step. It is not the authorization model and it
must not introduce secrets into the client. Owner/Staff permission is still
server-enforced by staff tokens, roles, machine binding, and audit checks.

## Preconditions

1. Run the strict gate first:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -Python C:\Python314\python.exe -StrictSecurity
```

2. Confirm the live backend owner/staff contract is deployed:

```powershell
C:\Python314\python.exe tools\admin\check_backend_contract.py --base-url https://api.zaeorion.com
```

3. Keep `native_orion\build_prod\Release` as the unpacked internal debug build.
   Do not overwrite it with packed binaries; crash triage and support need the
   unpacked symbols/build path.

## Pipeline modes (2026-09-20 hardening)

`tools\security\pack_lethe_release.py` has three modes:

* **`--verify-only` (READ-ONLY).** Validates an EXISTING packed package's release
  manifest + Ed25519 signature against the PINNED production key, runs the
  forbidden/secret scans, enforces the **customer acceptance** test below, and
  proves the customer launch + every tamper-refusal class. It NEVER copies, packs,
  regenerates, or signs. Use it to re-check a package without producing one.

  **Integrity is not acceptance (2026-09-20 round 2).** A green
  `verify_release_integrity.verify()` only proves that every *manifested* file
  hashes correctly under a signature from the pinned key; it records an
  UNMANIFESTED file as a mere warning. `--verify-only` therefore additionally
  requires, and exits non-zero on any of:
  1. **zero unmanifested files** (AGENTS.md: an unmanifested runtime file is a
     release blocker — an attacker-supplied DLL would otherwise ride along);
  2. `audience == "customer"` (never publish an internal-audience package);
  3. **no `OrionOwner.exe`/`OrionStaff.exe`** on disk *or* in the manifest;
  4. **full `security_policy.json` semantics** — present, correct schema, and every
     value failing closed (`require_release_manifest`, the three
     `lock_automation_on_*`, `allow_local_dev_bypass: false`). A correctly SIGNED
     but semantically unsafe policy is still refused;
  5. **profile-aware executable admission** — every `*.exe` must be on the release
     allowlist *at its exact package-relative path*. `OrionNative.packed.exe` is
     admitted **only** with `--server-shard`.
* **assembly (default).** Copies the input into a private, run-specific staging
  root, packs the first-party EXEs (Lethe with **`--process-hardening`** — restricted
  default DLL search directories), runs the INTERNAL Owner/Staff smokes, then
  STRIPS Owner/Staff, regenerates + Ed25519-signs the final **customer** manifest,
  audits, verifies, runs the CUSTOMER (Native+Updater) startup + tamper smokes,
  builds the zip + signed update manifest, and only then **atomically promotes**
  the complete artifact set. Any failure leaves the previously published
  artifacts (package dir + zip + update manifest + report) untouched.
* **`--server-shard`.** Assembles the three-exe activation chain and stays
  fail-closed until every shard essential is present AND manifest-covered (see
  "Server-shard packaging" below).

Notes:
* Re-signing (key rotation) without re-packing: use assembly mode with `--no-pack`.
* **Production provenance is not bypassable (round 2).** A production run (the default —
  it is signed with the trusted production key) **requires** `--expected-packer-commit
  <clean lethe commit>` whenever the packer runs, and **refuses** `--allow-dirty-packer`
  outright. Both relaxations live behind `--non-production-build`, which in turn
  **refuses the production signing key** and stamps `"production": false` into the
  packing report. A dirty or unpinned packer tree therefore cannot produce
  production-signed, publishable output.
* **Publication is crash-safe (round 2).** The promotion of the complete artifact set
  (package dir + zip + update manifest + report) runs under a durable, fsync'd journal
  at `release\.orion-pack-publish.journal.json`. A handled failure rolls back
  in-process; a **kill / power loss between moves** leaves the journal, and the next
  packer run replays it — forward to the complete new set while the staged artifacts
  survive, back to the complete old set otherwise. A reader therefore never observes a
  mixed old/new set. `--verify-only` REFUSES to run while a journal is present.
* **The startup/tamper smokes are an offline integrity PROXY**, not an executed launch:
  they replicate `SecurityManager::verifyReleaseIntegrity()`'s decision without running
  a single packed binary, so they cannot detect a PE-load failure, a missing Qt plugin,
  packer incompatibility, or real DLL-search behaviour. They are adequate only while the
  clean-VM launch (broker -> bootstrap -> packed app -> updater, renamed dir, unrelated
  CWD, scrubbed env, every tamper class re-run against the real processes) stays a
  MANDATORY blocking rig test.
* Running the packer's tests on this machine needs `TEMP`/`TMP` redirected to a short,
  writable path (`$env:TEMP = "C:\Users\aaron\AppData\Local\Temp\opk"`): the default
  pytest temp root is ACL-denied here, and a long path trips `WinError 206`.
* Secrets are **never** passed on the packer command line: Lethe reads shard
  secrets from the environment (`LETHE_SHARD_ADMIN_SECRET`, `LETHE_SHARD_EDGE_AUTH`,
  `LETHE_SHARD_ADMIN_TOTP`, `LETHE_SHARD_PIN_PEM`). The wrapper refuses a template
  carrying credential flags and records only a REDACTED command in the report.

> Encrypted key (2026-09-21): the PEM may stay encrypted on disk. Export
> `ORION_UPDATE_SIGNING_KEY_PEM=<path>` and `ORION_UPDATE_SIGNING_KEY_PASSPHRASE=<passphrase>`
> in the shell that runs `-StrictSecurity` / the packager (never on the command line); an
> interactive run prompts instead. No decrypted copy is ever written.

## Packing command

The script copies `release\orion-package` to a private staging root, runs the
packer per target, regenerates `release_manifest.json`, runs the package audit,
launches the internal Owner/Staff startup integrity checks, proves the customer
startup + tamper refusals, and atomically promotes `release\orion-package-packed`.

The packer command template must contain `{input}` and `{output}`. Do not quote
the placeholders; the wrapper inserts quoted paths.

```powershell
C:\Python314\python.exe tools\security\pack_lethe_release.py `
  --packer-command "C:\Path\To\PackerConsole.exe {input} {output} -profile C:\Path\To\orion-owner-staff-profile.cfg" `
  --require-live-backend
```

For a verification dry run without a packer installed:

```powershell
C:\Python314\python.exe tools\security\pack_lethe_release.py --verify-only
```

## Default pack targets and DLL safety boundary

The production wrapper packs first-party executable binaries only:

- `OrionOwner.exe`
- `OrionStaff.exe`
- `OrionNative.exe`
- `OrionUpdater.exe`

All DLL release targets are currently blocked, including Orion's first-party
core DLLs. The internal Lethe DLL round-trip harness remains valid lab
coverage, but it does not authorize packed DLLs for production: protected DLL
imports are still resolved during `DllMain`, under the Windows loader lock.
Keep every DLL unpacked until loader-safe deferred initialization is implemented
and separately release-approved. An explicit `.dll`/`.DLL` in `--targets`
causes the wrapper to fail before copying or modifying the release package.

## Qt Quick style prune (2026-09-21)

`windeployqt` copies **every** Qt Quick Controls style (Basic, Fusion, Imagine, Material,
Universal, Windows, FluentWinUI3) because the style is chosen at runtime, not by an import it
can scan. Each packaged app pins exactly one: OrionNative/Owner/Staff call
`QQuickStyle::setStyle("Basic")` (`native_orion/src/main.cpp`), OrionStream pins
`Style=Material` in its bundled `qtquickcontrols2.conf`. The unused styles were 2,272 of the
2,940 packaged files (FluentWinUI3 alone is ~840 PNG/QML files per copy, and the package
carries two Qt runtimes). `copy_runtime()` now ends with `prune_unused_quick_styles()`:

* launcher tree keeps `qml/QtQuick/Controls/{Basic,impl}`; stream tree keeps
  `{Basic,Material,impl}` (Basic is Qt's fallback for any control a style leaves out);
* the paired `Qt6QuickControls2<Style>.dll` / `...StyleImpl.dll` leave with their style;
* `qml/QtQuick/NativeStyle` leaves unless the Windows style is pinned (its only consumer);
* a pinned style that is missing REFUSES the package (the app would start with no controls);
* a tree with no Qt deployment is left alone (test fixtures).

Allowlist: `QT_QUICK_STYLE_ROOTS` in `tools/release_filter_policy.py` (ONE policy read by the
packager's prune AND by `security_audit.py`, which refuses any leftover as HIGH
`PACKAGE_QT_STYLE_UNPRUNED`). Fail-closed three ways: a root the source build deploys Qt for
must be staged; a pinned style must be complete (qmldir, QML plugin, StyleImpl DLL, shared impl
plugin, core DLLs); the post-prune self-check must find nothing. `+<Style>` Dialogs selector
folders of unselectable styles are pruned too. If an app ever changes
its pinned style, change the allowlist in the same commit. Tests:
`tests/test_packager_quick_style_prune.py`. Runtime proof per release: OrionNative AND
OrionStream must open from the pruned package (a missing style renders an empty window,
not a crash, so a launch smoke that only checks the PID is not enough - open Remote Play).
## Required post-pack evidence

The wrapper must finish with:

- `[orion-security] OK`
- Owner/Staff `--check-startup-security` return success
- missing `release_manifest.json` causes `OrionStaff.exe` to return non-zero
- live backend contract check passes when `--require-live-backend` is used
- `release\orion-packing-report.json` records the exact packer template and
  per-target size changes

After that, run one real smoke:

1. Owner login with owner secret.
2. Create or reissue a staff enrollment key.
3. Staff enroll/login from the target machine.
4. Staff performs an allowed lookup.
5. Staff attempts a disallowed action and the backend rejects it.
6. Disable the staff account and confirm the old token stops working.

## Server-shard packaging (`--server-shard`)

The server-shard release ships the three-exe activation chain:

- `OrionActivate.exe` — the UNPACKED activation broker. The owner is **cert-free**:
  the broker is NOT Authenticode-signed. The **Ed25519-signed release manifest** is its
  package-INTEGRITY record — the broker's round-3 self-check verifies its own file
  against the pinned manifest key, and the packer proves the broker is covered by that
  signed customer manifest — but it is **NOT a sufficient sole bootstrap trust anchor** — a replacement broker can simply omit the self-check, and Windows loads the broker's app-local imports before its code runs (red team 2026-09-20; see the OPEN blocker in `docs/PACKING_RUNBOOK.md`).
- `OrionNative.exe` — the verified `LetheShardBootstrap.exe`, staged under this name.
- `OrionNative.packed.exe` — the Lethe-packed inner payload.

The wrapper Lethe-packs the app to `OrionNative.packed.exe` (shard flags), stages the
bootstrap as `OrionNative.exe`, stages the broker as `OrionActivate.exe`, regenerates
+ signs the **customer** manifest, then asserts every shard essential is present AND
manifest-covered (`OrionActivate.exe`, `OrionNative.exe`, `OrionNative.packed.exe`,
`SecurityCore.dll`, `Qt6Core.dll`, `Qt6Network.dll`, `libcrypto-3-x64.dll`, the Qt
plugins the broker needs, and any VC runtime deps present). The `--server-shard` gate
**VERIFIES** these and fails closed if any is missing — it is no longer an
unconditional block.

Installer: `installer\orion.iss` auto-detects the broker in the package
(`OrionActivate.exe`) and points the launch target, Start-menu/desktop shortcuts, and
the `orion://` protocol registration at the broker; `build_installer.ps1` additionally
requires `OrionNative.packed.exe` and the signed `release_manifest.sig`.

RIG-only steps (not run by packaging): build `LetheShardBootstrap.exe` + the packed
app on a **pinned clean Lethe commit**, run the real pack with the `VeniceSigning`
key, StrictSecurity, and a clean-VM launch of the full broker→bootstrap→packed chain.

## Installer gate

`installer\build_installer.ps1` will not compile an installer from an unverified
package: before ISCC it runs `pack_lethe_release.py --verify-only` (adding
`--server-shard` when `OrionActivate.exe` is present) against the pinned key, and a
non-zero exit aborts the build. On a shard package it additionally requires the
broker's own import set — `SecurityCore.dll`, `Qt6Core.dll`, `Qt6Network.dll`,
`libcrypto-3-x64.dll` — and the compiled script read-back asserts that broker
registration is FATAL. `-AllowUnsigned` skips the gate for local dev builds only.

In `orion.iss`, `RegisterActivationBroker` now **raises** on a non-zero
`OrionActivate.exe --register`: on a shard build the customer's only activation path
is `orion://activate?code=...`, so a failed registration must fail the install rather
than report success with a dead protocol handler.

## OPEN blocker — the broker's trust anchor is circular (red team, 2026-09-20)

The Ed25519-signed release manifest is a sound package-INTEGRITY record. It is **not**
a sufficient sole BOOTSTRAP trust anchor for an unsigned `OrionActivate.exe` that
verifies itself:

* manifest coverage proves what the signer intended; it cannot force a **replacement**
  broker to perform the self-check at all;
* Windows resolves the broker's app-local imports (`SecurityCore.dll` ->
  `Qt6Network.dll`, `Qt6Core.dll`) **before** broker code runs, so a tampered
  app-local DLL executes first (`DllMain`).

Partial mitigation in place: the installer targets `{autopf}\Venice` with
`PrivilegesRequired=admin`, so a standard user cannot overwrite the chain. Closing it
requires a verifier OUTSIDE the verified object — an OS-enforced signed launcher /
WDAC code-integrity policy, or a minimal trusted bootstrap that verifies the broker
AND its imports before loading them — plus eliminating pre-verification app-local
imports where possible. Verification: tamper the broker and each preloaded first-party
DLL, launch via both the shortcut and `orion://`, and prove refusal happens before any
tampered entry point or `DllMain` runs.

## Release blockers

- Any admin/staff secret embedded in the packed package.
- Packed Owner/Staff tools start when `release_manifest.json` is missing.
- Packed package audit fails (incl. any lab/`*Checks.exe`/stray `*.packed.exe`).
- Packed update path accepts unsigned or SHA-mismatched artifacts.
- A production packing target is a DLL before loader-safe deferred
  initialization is implemented and verified.
- Packed staff build can perform an owner/admin-only action server-side.
- The packer command line carries a credential, or the report records the raw
  packer template.
- A server-shard package is emitted with an incomplete/uncovered activation chain.
- The Lethe packer tree is dirty/unversioned when a release is packed, or a
  production run is not pinned to an exact clean packer commit.
- `--verify-only` accepts a package with unmanifested runtime files, an
  internal-audience manifest, Owner/Staff, an unsafe `security_policy.json`, or an
  unadmitted executable.
- An installer is built from a package that did not pass the pinned-key verifier.
- The packed/unsigned broker chain is trusted on self-verification alone (see the
  OPEN blocker above) without an OS-enforced verifier.
- The packer causes persistent AV false positives that block normal install/use.
