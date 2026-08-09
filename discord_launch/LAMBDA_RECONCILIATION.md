# Deployed-Lambda reconciliation checklist (2026-07-03)

The DEPLOYED backend is `orion-activate` **v0.4.0** (+ separate `orion-sellhub-webhook`). The repo's
`backend/lambda_function.py` was **v0.1.0** and had DRIFTED. The deployed bundles were downloaded
(`orion-activate.zip` 8936 B, `orion-sellhub-webhook.zip` 4350 B) and unzipped into
`discord_launch/deployed_bundle/`. **RESOLUTION: `backend/lambda_function.py` has been replaced with
the verbatim deployed-bundle content** (adopt-the-deployed-bundle-as-baseline) so the repo now matches
production. One security fix was applied on top (see Q1 below). All five questions below are closed.

## Q1 — Do `/api/bot/*` routes exist in the deployed API Gateway? ✅ RESOLVED — YES
Confirmed by reading the full deployed `orion-activate/lambda_function.py`: `handle_bot_trial`,
`handle_bot_hwid_reset`, `handle_bot_killswitch`, `handle_bot_deliver`, and `handle_bot_provision` all
exist as explicit routes in `lambda_handler()`'s router, each gated by `require_bot(event)`. Both
Discord front-ends (`orion_worker.js` and `orion_bot.py`) proxy to paths that match exactly.

Side finding while adopting the bundle: `issue_staff_token()` had a fail-OPEN pattern — if the SSM
lookup for the staff-token signing secret raised, it silently fell back to a hardcoded
`"fallback-staff-secret-change-me"` string to sign staff auth tokens. Fixed to fail CLOSED (the
exception now propagates and the request fails) in the copied `backend/lambda_function.py`.

## Q2 — Does SSM have `/orion/bot_service_secret`? ✅ RESOLVED (code-confirmed; console spot-check still cheap insurance)
The deployed `lambda_function.py` declares `BOT_SECRET_SSM = "/orion/bot_service_secret"` and reads it
in `require_bot()`; the deployed `orion-sellhub-webhook` (`lambda_v6.py`) independently calls
`get_secret("/orion/bot_service_secret")` for its own outbound call to `/api/bot/provision`. Both sides
agree on the same param name, which is strong evidence it exists and is correctly provisioned — the
original session-1 audit's "confirmed 10 params" list was apparently incomplete, not the source of
truth. A one-time AWS Console → Systems Manager → Parameter Store check is still worth doing before
launch, but this is no longer a blocker.

## Q3 — Webhook edge-auth exemption ✅ RESOLVED — separate infra, no edge-auth involved
The deployed `orion-activate/lambda_function.py` has **no `/api/sellhub/webhook` route at all**. The
SellHub webhook is handled entirely by the separate `orion-sellhub-webhook` Lambda (`lambda_v6.py`),
which does its own independent HMAC verification (`x-sellhub-signature` + `x-sellhub-timestamp` replay
window) and never calls `require_edge_auth`. The repo's old self-contained design (webhook logic living
inside `lambda_function.py`) does not reflect production — this is a fully separate Lambda + API
Gateway route with its own trust boundary. No edge-auth question applies to it.

## Q4 — TIER/EXPIRY BUG ✅ RESOLVED — NOT PRESENT IN PRODUCTION (false alarm, no revenue bug)
The `tier_from_product()` substring-match bug described here only ever existed in the repo's old,
undeployed `backend/lambda_function.py` — it is not in the deployed path at all:
- `lambda_v6.py`'s `PRODUCT_MAP` maps each SellHub product **UUID** directly to an explicit integer
  `days` (1/3/7/14/30/120/None-for-lifetime). No product-name string parsing happens anywhere.
- `lambda_v6.py` always sends that explicit numeric `days` in its POST body to `/api/bot/provision`.
- `handle_bot_provision` / `handle_bot_deliver` compute `days = int(body.get("days") or
  TIER_DAYS.get(plan, 30))` — since `days` is always present and correct from the webhook, the
  `TIER_DAYS` fallback never triggers for a real purchase.
- For lifetime purchases (`days: None`), expiry is `0 if plan == "lifetime" else now + days*86400` —
  the special-cased lifetime branch means the `days` fallback doesn't matter there either.

**No fix was needed. This was a concern about dead code that was never deployed.**

## Q5 — Webhook field/header names ✅ RESOLVED (confirmed from live deployed source)
`lambda_v6.py` reads `x-sellhub-signature` (literal header, no fallback list) and
`x-sellhub-timestamp` for replay protection. Body: `event_id`, optional `order` wrapper
(`body.get("order", body)`), then `order.id`, `order.status`, `order.line_items[].product_id` (or
`productId`), and `order.custom_fields` (dict OR list form, multiple discord-id key aliases handled by
`get_discord_user_id()`). This is the literal live contract — no guessing required.

## Q6 — Trial-abuse guard ✅ ADDED on top of the adopted baseline
`handle_activate` now blocks a trial license from binding to a machine that has already redeemed a
different trial: when `item.get("plan") == "trial"` and the incoming `machine_id` differs from the
license's bound `machine_id`, it conditionally writes a `"TRIALMACHINE#" + machine_id` marker row; a
`ConditionalCheckFailedException` (marker already exists) logs `activate_blocked_trial_used` and returns
`err("trial_used", 403)`. This was not present in the deployed v0.4.0 bundle — it's a new guard, added
per the user's "patch it now, then reconcile tests" decision, and is part of the diff to review below.

## Test-suite reconciliation ✅ DONE — 50/50 passing
The previously-flagged conftest/test drift (function-accessor pattern, wrong routes, fictional helpers)
is fully resolved:
- `conftest.py` — SSM params, table defs (incl. `orion-staff`, `orion-staff-audit`, `orion-tokens` w/
  `token_hash-index` GSI), and `put_license`/`put_staff` helpers all reconciled to the real
  `licenses_table()`/`staff_table()`-style call accessors.
- `test_activate.py`, `test_trial.py`, `test_validate.py`, `test_staff_rbac.py`, `test_admin_bot_auth.py`
  — all rewritten against the actual handler behavior read directly from `lambda_function.py` (exact
  route paths, exact error strings per handler — `handle_activate` uses underscore-joined errors,
  `handle_validate` uses space-separated errors, no auto-bind on `/api/license/check`, etc.).
- `test_edge_auth.py` — untouched, already correct.
- Two dead test files (`test_nonce_ts.py`, `test_webhook.py`) testing routes that don't exist were
  deleted.

**Root-cause bug found during this reconciliation, not introduced by it**: `conftest.py`'s
`make_event()` only ever set `requestContext.http.path`, but `lambda_handler()` actually reads the
route from `event.get("rawPath", "") or event.get("path", "")`. Every single test request was
therefore resolving to `path=""` and falling through to a 404/unmatched-route response, regardless of
which endpoint or assertions the test itself specified — the entire suite was silently testing "does
this hit an unmatched route" rather than real handler behavior. Fixed by also setting `event["rawPath"]`
in `make_event()`. This one-line fix alone took the suite from every test failing (48 failed / 2 passed,
and the 2 that "passed" were only accidentally consistent with a 404) to **50 passed, 0 failed**.

- Gaps that exist in the deployed bundle itself (not introduced by this reconciliation), flagged for
  awareness rather than silently fixed: no OPTIONS/CORS preflight handling, no
  `/api/discord/register-commands` endpoint (present in the old repo file, absent from the deployed
  bundle), `handle_admin_staff`'s POST path is a no-op, `handle_admin_staff_audit` is missing a
  `details` field.
- The merged `backend/lambda_function.py` diff (adopted baseline + fail-closed `issue_staff_token` +
  trial-abuse guard) still needs to be shown to the user before any live redeploy, per their chosen
  process — this is the last remaining step.

## SellHub product IDs / prices (for mapping reference)
Lifetime $99.99 · 120-Day $49.99 · 30-Day $24.99 · 14-Day $14.99 · 7-Day $9.99 · 3-Day $5.99 ·
1-Day $2.99 · Beta Access $0.00.

## 2026-07-17 — Red-Team V2 security fixes (UNCOMMITTED, review pending)
All six 07-15 server fixes + six new findings from `docs/REDTEAM_SERVER_V2.md` are now
implemented in `backend/lambda_function.py`, `discord_launch/gumroad_webhook/lambda_function.py`,
and `discord_launch/deployed_bundle/orion-sellhub-webhook/lambda_v6.py`, with moto tests under
`tests/backend/`. **Operator actions (new SSM params, new DynamoDB tables, lease keypair,
offline update-signing, edge-auth fail-closed) are enumerated in `docs/SERVER_DEPLOY_CHECKLIST.md`
— read it BEFORE redeploy or the money path will 403 (edge auth now fails closed).**
