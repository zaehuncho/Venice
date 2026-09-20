# Admin Panel V2 — build handoff (2026-09-14, overnight)

Contract: `docs/ADMIN_PANEL_V2_CONTRACT.md` (owner-ratified picks 2,3,4,7,8,9,10 + the HWID rule:
3 free resets per key, then a paid credit or "deduct N days"). Four workstreams built in parallel
against it (Opus 5 agents), nothing deployed, nothing committed. Survey that preceded it: the live
Lambda `orion-activate` was byte-identical to `backend/lambda_function.py`; the shipped owner/staff
tools and CLI were coded against routes the Lambda never had (license actions 404, staff POST a
no-op, roles never read, audit without actors).

## What exists now (all local, uncommitted)

| area | files | tests |
|---|---|---|
| Backend Lambda | `backend/lambda_function.py` | `tests/backend` 220/220; `tests/test_backend_staff_auth.py` 17/17 |
| Owner/staff tools | `native_orion/src/AdminToolController.*`, `native_orion/qml/admin/*` (19 QML files registered in CMake), `docs/ADMIN_TOOL.md` | OrionOwner.exe + OrionStaff.exe build, launch with 0 QML warnings |
| CLI | `tools/admin/orion_admin.py` | `tests/test_orion_admin.py` 72/72 |
| Discord + Gumroad | `discord_launch/orion_worker.js`, `orion_bot.py`, `gumroad_webhook/lambda_function.py`, `discord_commands.json`, `register_commands.py`, READMEs | `tests/discord` 60/60 |
| Launcher | `native_orion/src/LicenseClient.*`, `OrionAppController.*` (heartbeat codes, MOTD), `qml/pages/RemotePlayPage.qml` (MOTD banner) | OrionNativeTests 807/807 |

Backend route table, capability per route, new DynamoDB attributes and the three optional GSIs are in
the backend agent's report (reproduced in §"Deploy"). Roles: owner / admin / support, server-enforced
by `require_capability`; every mutation needs a `reason` and writes one `orion-audit` row with
`actor_type/actor_id/role/action/target/reason/result/ip`; owner alerts DM the owner for the
configured events.

Real bugs found on the way: (1) Gumroad refunds never revoked (`verify_license_key` incremented uses
on every ping, so the refund ping hit the replay guard) — fixed in the webhook; (2) revokes set only
`status` while `/api/activate` reads the `revoked` boolean — every revoke path now writes both;
(3) the shipped tools would have 404'd on every license action.

## Decisions the owner still owes
1. `version_blocked` on a heartbeat ENDS a running session (safe reading of the contract). If it
   should only bite at next launch, it is a one-line change in the launcher.
2. Keys transit the Cloudflare Worker for `/deliver` and `/keygen` (ephemeral replies; the Worker has
   no bot token to DM). If unacceptable, run those two commands from the gateway bot only.
3. Which Cloudflare Worker is live (`orion-discord-bot` vs `license-redeem-proxy`) — read it off
   Discord's Interactions Endpoint URL before any `wrangler deploy`; `wrangler.toml` still names the
   non-existent `orion-license-bot` on purpose.
4. Purge the Helios auth-bypass patch scripts tracked in the repo root before the repo or a package
   leaves the machine; keep `codesigning/venice_update_signing.pem` off the dev box.

## Deploy (in this order; the owner says "deploy" — nothing is pushed by default)
1. Backend: package `backend/lambda_function.py` → `aws lambda update-function-code
   --function-name orion-activate` (the local `venice-automation` user has the permission). Additive
   for the launcher; BREAKING for Discord (`/deliver` needs `actor_discord_id`; bot killswitch writes
   removed) — so deploy the bot/worker in the same window.
2. IAM on the Lambda role: `dynamodb:Query` on the three GSI ARNs below; `ssm:PutParameter` on
   `/orion/owner_totp_secret` and `/orion/admin_secret`.
3. GSIs (optional for correctness — every consumer falls back to a scan and reports `lookup_mode`):
   `orion-licenses` `discord_user_id-index`, `orion-audit` `day-ts-index` (day HASH, ts RANGE),
   `orion-staff` `discord_user_id-index` (exact `aws dynamodb update-table` commands in the backend
   agent report / `docs/ADMIN_TOOL.md`).
4. Config seed via the owner tool or CLI `server-config set`: `alerts.owner_discord_user_id` (alerts
   no-op until set), `reset_policy_defaults.price_url` (the Gumroad HWID-reset product), optionally
   `min_client_version`, `motd`.
5. Owner TOTP: `POST /api/admin/config {action: totp_enroll}` → scan the otpauth URI → `totp_confirm`
   → `owner_totp_required` on. Default OFF until then. `rotate_admin_secret` prints the new secret once.
6. Discord: `wrangler secret put DISCORD_PUBLIC_KEY / DISCORD_APP_ID / ORION_BOT_SECRET /
   ORION_EDGE_AUTH`; Worker vars `ORION_API_BASE, GUMROAD_BASE, GUMROAD_HWID_RESET_SLUG,
   STAFF_ROLE_IDS`; gateway bot env `ORION_EDGE_AUTH, STAFF_ROLE_IDS, HWID_RESET_BUY_URL,
   ORION_STAFF_ID, ORION_STAFF_MACHINE_ID`; webhook Lambda env `GUMROAD_HWID_RESET_PRODUCT,
   OWNER_DISCORD_USER_ID` (or SSM `/orion/owner_discord_user_id`); `python
   discord_launch/register_commands.py`; then Server Settings → Integrations → Command Permissions →
   grant the Staff role `/deliver` + `/keygen` (they ship hidden).
7. Ship the tools: `verify_orion.ps1 -StrictSecurity` builds/packages OrionOwner/OrionStaff.

## Follow-ups (not blockers)
- The Gumroad webhook still writes refund/chargeback revokes straight to DynamoDB and DMs the owner
  itself; the backend now has `POST /api/bot/chargeback` (audited + alerted) — point the webhook at it.
- `metrics.trials.converted_30d` is a heuristic; add a `converted_from_trial` flag on the mint path.
- A refunded HWID-reset credit is not clawed back (logged `hwid_credit_not_clawed_back`).
- Trial is 3 days (`TRIAL_DAYS`); the launch recommendation was 7 days or usage-capped, HWID-bound.
- Red-team the whole surface before the trial link goes out ([[launch-red-team-pending]]).

## Morning plan (owner, 09-14 ~03:10 local)
1. Owner tests the launcher build (meter-only, shape gate, gate-top, vectorized locator) and the new
   admin stack locally (OrionOwner/OrionStaff against the CURRENT live Lambda will still 404 on the
   new routes until the Lambda is deployed — expect that; the kill switch/whoami/staff list work).
2. Owner answers the four decisions above, then "deploy" (order in §Deploy) and "commit".
3. FADES — what can be built: nothing new; two measured levers. (a) flip `ownership_proof_two_frame`
   (validated, OFF) at an app close — buys one frame on the late-onset fades; (b) a fade-heavy
   framedump session with the per-shot offset sweep (`-Framedump -AllowTimingOverrides` +
   `ORION_DEV_FIRE_OFFSET_SWEEP=list:-8,-4,0,4`), graded by TYPE with panel_grade.py → set
   `tip_phase_type_trim` per type from the data (fades' meters run 8-10 ms faster; live trims -4/-6
   cover half). Onset latency itself (meter not drawn until ~650-700 ms after a fade press) is the
   game's; not reachable.
4. CONTEST — do not take the L yet. (a) free: put Shot Lead at the graded zero-offset point (272,
   owner is at 281) — a late lean only costs on a NARROW window, which is exactly the contested case;
   (b) the same sweep session graded by COVERAGE x offset answers whether contested shots green more
   at an earlier offset; if yes → build a press-time defender-proximity detector (one pose inference
   at the press, the trained yolo26 pose model exists, ONNX; the sidecar venv lacks torch) that
   applies a contested-only early bias; if no → the window top is invariant and precision (~10.7 ms
   sigma, at its floor) is the only lever = the L is real and measured.
5. Disk: archive/delete the analyzed dumps (145457 9.5 GB, 222756 + 224102 5.8 GB, 124114 1 GB;
   keep 201355 = grader validation set) so the sweep session can be dumped and graded.

## Disk cleanup done (owner "go ahead", 09-14 ~03:30 local)
- Deleted the frames of the four analyzed dumps (145457, 222756, 224102, 124114); their
  `frames.csv` / `panel_grade.csv` / review sheets are under `logs\diagnostics\framedump_archive\`.
- Moved the grader validation set `session_20260912_201355` (12,002 files, 14 GB) to
  `D:\NexusVision\framedump\` (robocopy /MOVE, exit 1 = copied OK). Grade it with the full path:
  `panel_grade.py D:\NexusVision\framedump\session_20260912_201355`.
- `run_orion.local.ps1 -Framedump` now writes new sessions under `D:\NexusVision\framedump\`
  whenever that folder exists (412 GB free), else the old C: path. C: free 11 -> 36 GB.

## 13:51-13:57 local sweep batch: 74 shots, UNGRADED — my D: move killed the dump
- The batch itself ran (74 releases, sweep cycling -8/-4/0/+4, aborts: 2 ownership_proof_incomplete
  + 1 live_tip_deadline_missed) and detframes.csv (60 fps, 19,320 rows) was written, but the
  framedump wrote ONE frame to D:\NexusVision\framedump\session_20260914_135124 and stopped:
  the writer's single-write stall guard (`_framedump_max_write_s` = 250 ms, knob
  ORION_FRAMEDUMP_MAX_WRITE_MS 50..2000) permanently disables the dump on one slow write, and D:
  is disk 0 = HGST HTS541010A9E680, a 5400 rpm HDD (C: is the Samsung 980 PRO SSD). No banner
  frames -> no verdicts -> the offset/type/coverage grading cannot be done for this batch.
- FIX: launcher default is back on C: (36 GB free, a capped session <= 14 GB); another root only via
  ORION_FRAMEDUMP_ROOT, and only on a disk proven to take a 720p PNG in < 250 ms cold (or raise
  ORION_FRAMEDUMP_MAX_WRITE_MS). The 201355 validation set stays on D: (reads are fine).
- The sweep batch must be re-run (20 min) for the fade-trim / contest questions.
