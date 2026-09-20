# Orion Release Security Model

This project cannot make native code impossible to reverse engineer. The ship
goal is to raise the cost of tampering, keep customer packages free of developer
artifacts, and fail closed when runtime files are modified.

## Enforced Locally

- Gameplay-critical logic is in native DLL boundaries:
  - `AutomationCore.dll`
  - `VisionCore.dll`
  - `RemotePlayCore.dll`
  - `SecurityCore.dll`
- Release builds use MSVC hardening flags:
  - Control Flow Guard
  - CET compatibility
  - ASLR and NX compatibility
  - stack checks and SDL checks
  - LTCG and dead-code folding for Release packages
- `SecurityCore` verifies `release_manifest.json` hashes before automation.
- `security_policy.json` requires release-manifest verification in packaged builds.
- Local entitlement cache is protected with Windows DPAPI and bound to:
  - machine ID
  - Windows user
  - build SHA-256
- Debugger/analysis observations lock automation in release policy instead of
  crashing, deleting files, or fighting user tools.
- Settings are machine-bound by `settings.json.sig`.

## Package Rules

The customer package is generated with:

```powershell
python tools\package_orion_release.py --strict
```

The strict package excludes:

- `.pdb`, `.lib`, `.exp`, `.ilk`, `.iobj`, `.ipdb`
- `OrionNativeTests.exe`
- debug DLLs such as OpenCV/Qt `*d.dll`
- `.vault`, `master.key`, local license caches, settings signatures
- logs, DBs, Python bytecode, caches, and local runtime artifacts

Run the full strict gate with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1 -StrictSecurity
```

`-StrictSecurity` now also runs:

```powershell
python tools\security_audit.py --package-only
```

The audit verifies that the generated runtime package still fails closed:

- required native runtime files are present
- `release_manifest.json` hashes match every packaged runtime file
- `security_policy.json` requires manifest verification and forbids local dev
  bypass
- no local settings, license caches, vault material, logs, dumps, symbols,
  import libraries, test binaries, debug Qt/OpenCV DLLs, private keys, or
  concrete local dev keys are packaged

For a source-tree secret check without rebuilding the package, run:

```powershell
python tools\security_audit.py --source-only
```

## Server-Side Work Still Required

The cleaned Orion tree no longer contains the FastAPI licensing server. Before
public launch, the server should enforce:

- signed entitlement responses
- per-IP, per-device, and per-license rate limits
- nonce replay rejection
- request signatures on activation/session/update/audit routes
- signed update manifests and artifact hashes
- audit events for invalid signatures, replay attempts, device mismatch,
  tamper reports, and debugger-observed reports

## Production build switch (`-DORION_PRODUCTION=ON`)

A shipped package MUST be configured with `-DORION_PRODUCTION=ON`. This defines
`ORION_PRODUCTION_BUILD` and compiles out **every** developer escape hatch — they
do not exist in the binary, so a planted file or environment variable cannot
re-enable them:

| Escape hatch (dev) | Production behaviour |
| --- | --- |
| `NVDEV-` local dev license / `localDevAllowed()` | always `false` |
| `ORION_SKIP_UPDATE_GATE` + CMakeCache dev-build detection | `devBuild_` stays `false`; gate is not skippable |
| `ORION_DEBUG` debug UI tab | always `false` |
| `ORION_FREEZE_CAL` env | ignored (settings flag only) |
| `ORION_FORCE_VIRTUAL_NEUTRAL` harness | compiled out |
| `require_release_manifest` policy file | forced `true` (integrity always required) |

`tools/package_orion_release.py` reads `CMakeCache.txt` and **refuses to package**
a build not configured with `ORION_PRODUCTION=ON` (override only with
`--allow-dev-build` for a local, never-shipped test package). The diagnostics
bundle records `production_build=1` so support can confirm a customer is on a
hardened binary.

### Dev vs production build trees

Production verification uses a **separate** build tree so the day-to-day binary
never becomes a production one — a production build forces
`releaseManifestRequired=true` and fails closed for automation without a valid
`release_manifest.json`, which would break local dev/live testing:

- `native_orion/build` — dev build (`ORION_PRODUCTION=OFF`); used for daily work,
  live testing, and the standard `verify_orion.ps1` gate.
- `native_orion/build_prod` — production build (`ORION_PRODUCTION=ON`); built,
  tested, and packaged only by `verify_orion.ps1 -StrictSecurity`.

`-StrictSecurity` configures + builds + ctests `build_prod`, deploys the Qt
runtime into it with `windeployqt`, then packages it via
`package_orion_release.py --strict --build-dir native_orion/build_prod/Release`
and runs the package security audit. The dev tree is left untouched.

## Phase 8 — obfuscation & packing plan (after live sign-off)

Aggressive obfuscation/packing is deferred until gameplay + controller
reliability are signed off live, because it makes crash dumps, hotplug bugs, CV
timing bugs, and support substantially harder. Ship-time checklist:

1. **Confirm the production switch**: build `-DORION_PRODUCTION=ON`, package with
   the strict gate, verify `tools/security_audit.py --package-only` passes and the
   diagnostics bundle reads `production_build=1`.
2. **Obfuscate the gameplay-critical DLLs only** (`AutomationCore`, `VisionCore`,
   `RemotePlayCore`, `SecurityCore`) — control-flow flattening + string
   encryption + symbol stripping. Leave `OrionNative.exe` / Qt UI lightly
   processed so crash dumps stay triageable.
3. **Pack the shipping executables** (VMProtect/Themida-class or an in-house
   packer) with `tools/security/pack_lethe_release.py` AFTER the strict package
   is green. The wrapper copies the strict package, runs the external packer,
   regenerates `release_manifest.json`, runs `tools/security_audit.py
   --package-only`, runs Owner/Staff startup-integrity checks, and proves
   missing-manifest tamper refusal. The exact operator command and evidence are
   documented in `docs/PACKING_RUNBOOK.md`.
4. **Anti-tamper at load**: `SecurityCore` already verifies `release_manifest.json`
   and locks (not crashes) on debugger/analysis observation. Add a periodic
   re-hash of the loaded DLL images, and bind the entitlement cache check to the
   packed build SHA so a swapped/unpacked DLL fails entitlement.
5. **Server replay/tamper rejection** (separate backend session): signed
   entitlement + update responses, nonce replay rejection, request signatures —
   see the server checklist above and `docs/SERVER_HANDOFF.md`.
6. **Re-test the full live gate after packing** — packers can perturb timing; the
   sub-tick scheduler and CV path must be re-validated on the packed binary
   before release.

## Owner/Staff Tool Hardening

Owner/staff tools are more sensitive than the gameplay launcher because they can
create licenses, reset machines, disable users, and view audit state. Production
approval requires all of the following:

- **No client-side authority**: the owner secret, staff tokens, Discord bot
  token, Sellhub webhook secret, and update private key must never be embedded
  in an EXE, QML resource, Python file, package, log, or screenshot. The client
  only holds a short-lived staff bearer token or a prompted owner secret.
- **Server-enforced role checks**: staff UI controls are advisory only. The
  backend must enforce owner/admin/support roles for every destructive action.
- **Machine-bound staff enrollment**: owner creates a staff record by Discord ID
  and display name, receives one one-time enrollment key, and the backend stores
  only a salted hash. First enrollment binds the staff account to that machine.
- **Audit trail required**: license generation, revoke/unrevoke, HWID reset,
  deactivation, staff disable/enable, role changes, enrollment reissue, and kill
  switches must write suffix-only audit rows with actor, action, target, reason,
  timestamp, and success/failure.
- **Packaging parity**: any `OrionOwner.exe` / `OrionStaff.exe` ship through the
  same `-DORION_PRODUCTION=ON` build, release manifest, package audit, symbol
  stripping, and update-signature path as `OrionNative.exe`.
- **Fail closed on tamper**: a missing/edited `release_manifest.json`,
  `security_policy.json`, updater, runtime DLL, or public update key must block
  privileged staff/owner operations, not silently downgrade to a less secure
  path.
- **Authenticated tamper reporting**: after Owner/Staff authentication, a new
  security lock triggers one best-effort report to `/api/admin/tamper-report` or
  `/api/staff/tamper-report`. The report is audit-only; the tool remains locked
  and the backend still enforces owner/staff auth, role, and machine binding.
- **Startup integrity check**: production `OrionOwner.exe` and `OrionStaff.exe`
  support `--check-startup-security`, which verifies `release_manifest.json`
  before QML loads and exits non-zero on missing/modified manifest-covered files.
  `verify_orion.ps1 -StrictSecurity` runs this against the valid package and a
  copied package with `release_manifest.json` removed.
- **Binary string scan**: the release audit extracts printable ASCII/UTF-16
  strings from first-party Orion EXEs/DLLs and scans them for concrete dev keys,
  private keys, Discord bot tokens, AWS keys, Cloudflare-token-shaped leaks, and
  Sellhub/webhook secrets. Third-party Qt/OpenSSL DLLs are excluded from this
  binary string pass to avoid blocking on vendor parser templates.

Packing/obfuscation is useful only after those controls exist. It can slow down
reverse engineering, but it cannot protect a secret that is embedded in the
client and it cannot replace backend authorization.
