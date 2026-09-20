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

## Packing command

Use `tools\security\pack_lethe_release.py` to apply the packer and then verify
the packed package. The script copies `release\orion-package` to
`release\orion-package-packed`, runs the packer per target, regenerates
`release_manifest.json`, runs the package audit, launches Owner/Staff startup
integrity checks, and proves missing-manifest tamper refusal.

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

## Release blockers

- Any admin/staff secret embedded in the packed package.
- Packed Owner/Staff tools start when `release_manifest.json` is missing.
- Packed package audit fails.
- Packed update path accepts unsigned or SHA-mismatched artifacts.
- A production packing target is a DLL before loader-safe deferred
  initialization is implemented and verified.
- Packed staff build can perform an owner/admin-only action server-side.
- The packer causes persistent AV false positives that block normal install/use.
