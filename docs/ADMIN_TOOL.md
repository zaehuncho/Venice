# Orion Admin Tools

Three client surfaces operate the licence backend, all speaking the same
contract — `docs/ADMIN_PANEL_V2_CONTRACT.md` (§2-§6 endpoints, §7 clients):

- **`OrionOwner.exe`** — the owner console: Dashboard, Licenses, Staff, Audit, Config.
- **`OrionStaff.exe`** — the staff console: Licenses (role-limited), My audit, My access.
- **`tools/admin/orion_admin.py`** — the scriptable twin of both.

Neither EXE embeds an owner secret, bot token, update key or licence authority.
The owner secret is typed at login and held in memory only; staff sessions are
machine-bound bearer tokens issued by the backend.

Sources: `native_orion/src/AdminToolController.{h,cpp}` (one controller for both
tools, `ORION_OWNER_TOOL` / `ORION_STAFF_TOOL` picks the mode),
`native_orion/src/admin_tool_main.cpp`, `native_orion/qml/admin/**`.

## Roles and capabilities

Server-enforced by `require_capability`. The clients only *display* the matrix —
`AdminToolController::can()` and the CLI's `ROLE_CAPABILITIES` hide what a role
cannot do so the UI is honest; they never stand in for the server's gate.

| capability | owner | admin | support |
|---|---|---|---|
| license.lookup | ✓ | ✓ | ✓ |
| license.create | ✓ | ✓ (≤ caps.keys_per_day) | ✗ |
| license.reset_machine | ✓ | ✓ (≤ caps.resets_per_day) | ✓ |
| license.reset_machine **force** | ✓ | ✗ | ✗ |
| license.extend | ✓ | ✓ (≤ caps.extend_max_days) | ✗ |
| license.revoke / unrevoke / freeze / unfreeze | ✓ | ✓ | ✗ |
| license.set_plan / transfer | ✓ | ✓ | ✗ |
| license.set_reset_policy, blacklist | ✓ | ✗ | ✗ |
| staff.* , config.* , metrics, audit.read_all | ✓ | ✗ | ✗ |
| audit.read_own | ✓ | ✓ | ✓ |

An owner has two ways in: the break-glass admin secret, or a staff row with
`role=owner` (a machine-bound token like any other). Both reach `/api/admin/*`;
the tools call that state **owner routes**, and fall back to `/api/staff/*`
otherwise.

**Every mutation requires a reason (≤200 characters).** In the panels the action
button stays disabled until the reason field is valid; in the CLI `--reason` is
mandatory and destructive commands additionally prompt for confirmation.

## OrionOwner.exe

Login: admin secret + TOTP code (§5 — the code is required once
`owner_totp_required` is on, and is cached for the session).

| screen | what it does | endpoint |
|---|---|---|
| Dashboard | licence/trial/activation/reset/staff/version counters, online now, fraud flags, service state | `GET /api/admin/metrics`, `GET /api/admin/config` |
| Licenses | lookup by key / email / Discord ID / machine ID; create (plan, days, count ≤25, Discord, email, note); revoke, unrevoke, freeze, unfreeze, extend, set plan, transfer; reset machine with the policy view (free left, used, paid credits, locked, reset history) and an owner **force** switch; per-key reset policy; blacklist / unblacklist | `POST /api/admin/license {action, reason, ...}` |
| Staff | create (role + caps) → the one-time enroll key with a copy button; roster with role, enabled state, caps and usage today; disable/enable, set role, set caps, reset machine, reissue enrollment | `GET/POST /api/admin/staff` |
| Audit | since / until / actor / action / target filters, cursor paging, per-row expander with `details{}` | `GET /api/admin/audit` |
| Config | kill switch, version gate, MOTD, reset-policy defaults, fraud thresholds, alert routing, owner TOTP enrol/confirm, admin-secret rotation, IP allowlist | `GET/POST /api/admin/config` |

## OrionStaff.exe

Login: **enrol once** on this machine with the staff id + one-time enroll key,
then sign in with the staff id alone. Every staff request carries
`Authorization: Bearer …`, `X-Machine-Id` (the Lambda's `require_staff` refuses a
bound token without it) and `X-Orion-Discord-Id`.

- **Licenses** — the same screen, with anything the role cannot do hidden.
- **My audit** — `GET /api/staff/audit` (own rows, cursor-paged).
- **My access** — staff id, role, machine, caps and usage today, and a read-out
  of the capability matrix for the current role.

## One-time secrets

Four things are shown exactly once and never again; each gets a copy button and
a Dismiss that wipes it from the controller:

| secret | where | note |
|---|---|---|
| staff enroll key | Staff → create / reissue enrollment | only a salted per-staff hash is stored; lost ⇒ reissue |
| new licence keys | Licenses → create | the panel shows full keys only at creation |
| TOTP otpauth URI + secret | Config → Enrol TOTP | add to the authenticator, then confirm a code |
| rotated admin secret | Config → Rotate admin secret | the session switches to it immediately; the old secret is dead |

### Owner TOTP procedure (§5)

1. Config → **Enrol TOTP** (reason required) → `POST /api/admin/config {action: totp_enroll}`
   returns the otpauth URI once. Add it to the authenticator.
2. Enter the current 6-digit code → **Confirm code** → `{action: totp_confirm, code}`.
   Confirming turns `owner_totp_required` on.
3. From then on the login pane's TOTP field is mandatory; the controller sends
   `X-Orion-Admin-TOTP` with every owner-secret request and caches the code for
   the session. Server codes `totp_required` / `invalid_totp` re-arm the prompt.

### Secret rotation

Config → **Rotate admin secret** (reason + confirmation) →
`{action: rotate_admin_secret}` writes a new secret to SSM and returns it once.
The panel keeps the session alive by switching to the new secret in memory —
store it before closing the window.

## CLI — `tools/admin/orion_admin.py`

Stdlib only, HTTPS only, no secrets on disk beyond the ignored
`~/.orion/admin_config.json` (written `0600`). Credentials resolve from the
environment first: `ORION_ADMIN_SECRET`, `ORION_ADMIN_TOTP`, `ORION_ADMIN_TOKEN`,
`ORION_ADMIN_STAFF_ID`, `ORION_ADMIN_DISCORD_ID`, `ORION_ADMIN_ROLE`,
`ORION_API_BASE`. The TOTP code is never written to disk — interactive runs
prompt for it, scripts pass `ORION_ADMIN_TOTP`.

```bash
# identity / session
orion-admin version
orion-admin whoami                       # role, staff id, caps, usage today
orion-admin caps                         # the matrix above (display only)
orion-admin config                       # redacted local config
orion-admin login --mode owner           # prompts for the secret, never echoes
orion-admin login --mode staff --staff-id staff_abc
orion-admin logout
orion-admin staff-enroll --staff-id staff_abc --enroll-key ENROLL-...
orion-admin staff-login  --staff-id staff_abc

# licences  (POST /api/admin/license | /api/staff/license {action, reason, ...})
orion-admin license lookup --key ORION-XXXX-XXXX-XXXX [--reveal]
orion-admin license lookup --email u@e.com | --discord ID | --machine MACHINE
orion-admin license create --plan pro --days 90 --count 5 [--discord ID] [--email ..] [--note ..] --reason "launch batch"
orion-admin license revoke|unrevoke|freeze|unfreeze --key ORION-... --reason "..."
orion-admin license reset-machine --key ORION-... [--force] --reason "..."
orion-admin license extend   --key ORION-... --days 30 --reason "..."
orion-admin license set-plan --key ORION-... --plan pro --reason "..."
orion-admin license transfer --key ORION-... --discord ID|--email .. --reason "..."
orion-admin license set-reset-policy --key ORION-... [--free-resets N] [--penalty-days N] [--locked true|false] --reason "..."
orion-admin license blacklist|unblacklist --machine ID|--discord ID --reason "..."

# staff (owner)
orion-admin staff list
orion-admin staff create --discord ID --role admin [--keys-per-day N] [--resets-per-day N] [--extend-max-days N] --reason "..."
orion-admin staff disable|enable|reset-machine|reissue-enrollment --staff-id staff_abc --reason "..."
orion-admin staff set-role --staff-id staff_abc --role support --reason "..."
orion-admin staff set-caps --staff-id staff_abc --resets-per-day 3 --reason "..."

# audit (owner: everything; staff: own rows)
orion-admin audit [--since UNIX] [--until UNIX] [--actor ..] [--action ..] [--target ..] [--cursor ..] [--limit ≤200]

# owner config (§5)
orion-admin server-config show
orion-admin server-config set --min-client-version 1.5.0 --blocked-versions "1.3.0,1.3.1" \
                              --motd-text "Maintenance 02:00 UTC" --motd-level maint --motd-until 1800000000 \
                              --free-resets 3 --penalty-days 3 --cooldown-s 86400 --self-service true \
                              --machines-30d 3 --resets-30d 4 \
                              --alert-owner ID --alert-events "staff.create,license.revoke" \
                              --owner-totp-required true --ip-allowlist "203.0.113.0/24" \
                              --reason "hardening pass"
orion-admin server-config totp-enroll  --reason "..."      # URI shown once
orion-admin server-config totp-confirm --code 123456 --reason "..."
orion-admin server-config rotate-secret --reason "..."     # new secret shown once

# global kill switch (config.global_kill; the bot route is read-only)
orion-admin killswitch status|engage|release --reason "..."

# owner dashboard numbers
orion-admin metrics
```

`--yes` skips the confirmation prompt for scripted use; `--reason` is still
mandatory. `--base-url` overrides the endpoint (HTTPS only).

## Safety rules

- Every mutation carries a reason (≤200 chars) and lands in the audit row.
- Destructive actions (revoke, freeze, reset-machine, transfer, blacklist, staff
  disable / reset-machine / reissue, killswitch engage, secret rotation) also
  require a typed confirmation unless `--yes`.
- Full licence keys appear only at creation or with an explicit `--reveal`;
  everywhere else they are masked to the last four characters.
- Admin secrets, TOTP codes and staff tokens are never printed or logged.
- Non-HTTPS endpoints are refused by the transport; the Qt tools additionally
  pin the server certificate and abort on a mismatch.

## Packaging and startup integrity

Both EXEs build from `native_orion/CMakeLists.txt`
(`orion_add_admin_tool(OrionOwner ORION_OWNER_TOOL OrionOwner)` / `OrionStaff`);
the admin QML lives in `ORION_ADMIN_QML_FILES` and is compiled into the
executable's QML module, so no loose admin QML ships.

```powershell
cmake --build native_orion\build --config Release --target OrionOwner OrionStaff
.\OrionOwner.exe --check-startup-security
.\OrionStaff.exe --check-startup-security
```

In production builds the startup check returns `0` only if
`release_manifest.json` verifies the package; a missing or modified
manifest-covered file returns non-zero before the QML loads.
`verify_orion.ps1 -StrictSecurity` runs this against the real package and against
a copy with the manifest removed.

The admin tools do not consume the gameplay `settings.json`, so the runtime does
not block on the launcher's settings-signature check. Release-manifest
integrity, debugger/analysis locks, server authentication, role checks and
machine-bound staff tokens still fail closed, and a security lock refuses
privileged auth outright (reported to `/api/{admin,staff}/tamper-report`, which
never unlocks the client).

## Error codes surfaced to the operator

`totp_required` / `invalid_totp` (re-prompts for a code), `ip_not_allowed`,
`rate_limited`, `version_blocked`, `token_expired` / `machine_mismatch` /
`staff_disabled` / `invalid_token` (the staff session is dropped and login is
required again), plus the licence states `frozen` and `blacklisted`.

## Tests

- `tests/test_orion_admin.py` — fully offline (fake transport). Asserts each
  subcommand's method, path and body against the contract, plus reason and
  confirmation gating, key masking, auth headers (owner secret + TOTP; staff
  bearer + `X-Machine-Id`), route selection (owner vs staff), enrol/login body
  shape, audit paging, config patch construction, and error surfacing.

  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests\test_orion_admin.py -q
  ```

- `tests/backend` — the server-side capability matrix, reset-policy state
  machine, audit rows, version gate, MOTD, TOTP, IP allowlist, blacklist, fraud
  flags and metrics shape.
- `tools/admin/check_backend_contract.py` — live, non-secret deployment smoke.
  Unauthenticated probes must fail closed with JSON auth errors; a `404` means
  the deployed Lambda revision predates the route.
