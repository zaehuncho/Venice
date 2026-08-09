# Orion Admin Tools

Orion ships two hardened Qt admin tools beside the launcher:

- `OrionOwner.exe` - owner-only console for staff registration, staff disable,
  staff machine reset, enrollment reissue, license lookup, HWID reset, and
  license deactivation.
- `OrionStaff.exe` - staff console for Discord-ID-bound enrollment/login and
  role-limited license operations.

Both tools use the same production build, updater handoff, release manifest, and
security policy path as `OrionNative.exe`. They do not embed owner/admin secrets,
Discord bot tokens, Sellhub secrets, update private keys, or license authority.
The owner secret is prompted and kept in memory only. Staff sessions are issued
by the backend as short-lived machine-bound bearer tokens.

The shipped release package must contain the EXEs, but not loose admin QML
source. `verify_orion.ps1 -StrictSecurity` builds `OrionOwner.exe` and
`OrionStaff.exe` in the production tree, packages them, and runs the release
security audit.

Both EXEs also expose a non-interactive startup integrity check used by the
strict gate:

```powershell
.\OrionOwner.exe --check-startup-security
.\OrionStaff.exe --check-startup-security
```

In production builds this returns `0` only if `release_manifest.json` verifies
the package. A missing or modified manifest-covered file returns non-zero before
the QML UI loads. `verify_orion.ps1 -StrictSecurity` runs this against the real
package and against a copied package with `release_manifest.json` removed.

Owner/Staff tools do not consume gameplay `settings.json`, so the admin runtime
does not block on the launcher's gameplay settings-signature check when running
from a packaged admin install. Release-manifest integrity, debugger/analysis
locks, server authentication, role checks, and machine-bound staff tokens still
fail closed.

## CLI (`tools/admin/orion_admin.py`)

A local, stdlib-only command-line tool for owner/staff operations against the
Orion license backend. It is intentionally kept out of the customer gameplay UI.

## Auth & Config

Credentials resolve from, in order:

1. Environment: `ORION_ADMIN_SECRET` for owner, `ORION_ADMIN_TOKEN` for staff,
   plus optional `ORION_ADMIN_DISCORD_ID`, `ORION_ADMIN_ROLE`,
   `ORION_API_BASE`.
2. Ignored local file `~/.orion/admin_config.json`, written `0600` by login or
   enrollment commands. It lives outside the repo and is never committed.

```bash
# Owner auth. Prompts silently and never echoes the secret.
python tools/admin/orion_admin.py login --mode owner

# Staff first-time enrollment. The owner creates this one-time key.
python tools/admin/orion_admin.py staff-enroll --discord 123456789012345678 --enrollment-key ORION-STAFF-...

# Staff session refresh on the same machine.
python tools/admin/orion_admin.py staff-login --discord 123456789012345678
```

`orion_admin.py config` prints a redacted config: secrets are shown only as
`set` or `unset`.

## Roles

Roles are server-enforced. CLI checks are advisory only.

| Role | Capabilities |
| --- | --- |
| `owner` | Everything, including staff create, disable, role changes, and enrollment reissue. |
| `admin` | License create, revoke, unrevoke, reset, deactivate, extend, plan change, lookup, audit. |
| `support` | License lookup, limited HWID reset, and deactivate. |

Staff identity is bound by Discord ID and machine ID. The backend rejects
disabled staff, wrong-machine tokens, expired sessions, and role-ineligible
operations.

## Commands

```bash
# Backend / identity
orion-admin version
orion-admin whoami

# License lifecycle
orion-admin license create --email user@example.com --plan pro --days 365 [--discord ID] [--key ORION-...]
orion-admin license lookup --key ORION-XXXX-XXXX-XXXX
orion-admin license lookup --email user@example.com
orion-admin license lookup --key ORION-XXXX-XXXX-XXXX --reveal
orion-admin license revoke      --key ORION-... --reason "chargeback"
orion-admin license unrevoke    --key ORION-...
orion-admin license reset-hwid  --key ORION-... --reason "user reinstalled"
orion-admin license deactivate  --key ORION-... --reason "transfer"
orion-admin license extend      --key ORION-... --days 90
orion-admin license set-plan    --key ORION-... --plan pro

# Staff management (owner)
orion-admin staff list
orion-admin staff create --discord ID --name "Display Name" --role admin
orion-admin staff disable --discord ID --reason "left team"
orion-admin staff reset-machine --discord ID --reason "new PC"
orion-admin staff reissue-enrollment --discord ID --reason "lost enrollment key"

# Audit
orion-admin audit --limit 50
```

## Safety Rules

- Destructive actions require a non-empty `--reason` and an interactive
  confirmation. `--yes` skips the prompt for scripts, but the reason is still
  mandatory.
- Full license keys are shown only by `license create` once, or by explicit
  `--reveal`. Everywhere else uses suffix-only masking.
- Staff enrollment keys are shown once by `staff create` or
  `staff reissue-enrollment`; only salted hashes are stored server-side.
- HTTPS-only transport. Non-HTTPS endpoints are refused.
- Admin secrets and staff tokens are never printed or logged.

## Endpoint Contract

Owner endpoints:

- `POST /api/provision` - create a license with owner secret.
- `GET /api/admin/license?key=` - look up a license.
- `POST /api/admin/license` - `revoke`, `unrevoke`, `reset_machine`,
  `deactivate`, `extend_expiry`, and `change_plan`.
- `GET /api/admin/search?email=` / `?discord=` - suffix-only license search.
- `GET /api/admin/whoami` - owner identity check.
- `GET/POST /api/admin/staff` - list/create/enable/disable/update-role/reset
  staff machine/reissue enrollment.
- `GET /api/admin/staff/audit?limit=` - staff/admin audit stream.
- `POST /api/admin/tamper-report` - owner-authenticated security-lock/tamper
  audit report from the privileged tool. This never unlocks the client.
- `GET /api/version`.

Staff endpoints:

- `POST /api/staff/enroll` - Discord ID + one-time key + machine ID returns a
  short-lived staff token.
- `POST /api/staff/login` - Discord ID + same machine ID returns a fresh staff
  token.
- `GET /api/staff/whoami` - staff identity check.
- `GET/POST /api/staff/license` - role-limited license lookup and mutations.
- `POST /api/staff/tamper-report` - staff-token-authenticated
  security-lock/tamper audit report from the privileged tool. Disabled or
  wrong-machine staff tokens still fail closed.

Audit rows are rendered with suffix-only targets plus actor, action, target
type, and reason.

## Tests

`tests/test_orion_admin.py` runs fully offline with a fake transport: key
masking, expiry math, config redaction, auth-header construction, HTTPS refusal,
reason/confirmation gates, key-once creation, masked lookups, and staff command
flows.

`tests/test_backend_staff_auth.py` runs fully offline with fake tables: staff
token verification, disabled-staff rejection, one-time enrollment-key handling,
route exposure, role matrix, and structured JSON failures.

`tools/admin/check_backend_contract.py` is the live, non-secret deployment smoke:

```powershell
python tools\admin\check_backend_contract.py --base-url https://api.zaeorion.com
```

It must pass before Owner/Staff tools are considered production-ready. Expected
live behavior is not "200 everywhere"; unauthenticated staff/admin/tamper probes
should fail closed with JSON auth or missing-field errors. A `404` means the
deployed API Gateway/Lambda revision is missing the staff route contract.
