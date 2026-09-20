# Website trial, Discord auto-join and purchase gate (2026-09-19)

Status: **built and tested, NOT deployed.** Nothing here has been pushed to Cloudflare, AWS or Discord.

Owner decisions this implements:

- $25/month flat, no activation fee, Stripe Checkout on zaeorion.com.
- The free 3-day trial can be claimed on the website (no card). `/claim_trial` in Discord still works.
- **Nobody can buy or start a trial unless they are signed in with Discord on the site AND are in the Venice server.** Every trial or subscription is confirmed by bot DM, and Discord only lets the bot DM users who share a server with it.
- Sign-in adds the user to the server automatically (`guilds.join`), so for most people the gate costs nothing.
- Stripe automatic tax is off at launch. Checkout is card-only. The Stripe API version is pinned.

Files changed:

| File | Change |
|---|---|
| `website/src/worker.js` | `POST /api/trial`, live membership gate on trial and checkout, login auto-join, new config fields, `Stripe-Version` pin, card-only Checkout, no `automatic_tax`, renewal `status === "paid"` fix |
| `website/wrangler.jsonc` | `DISCORD_INVITE_URL` var in both envs. No other key changed. |
| `website/tests/worker.test.mjs` | 15 new tests, 4 existing tests updated for the new contract |
| `backend/lambda_function.py` | `POST /api/bot/guild-join`, `POST /api/bot/guild-member`, `code` on every `/api/bot/trial` reply, per-account trial throttle, DM copy lines, `BILLING_PORTAL_URL` |
| `tests/backend/test_website_guild.py` | New: 49 tests |

---

## 1. Contract

### 1.1 `GET /api/checkout/config` (new fields)

```jsonc
{
  "provider": "stripe",            // unchanged
  "configured": true,              // unchanged
  "publishableKey": "pk_live_…",   // unchanged
  "authenticated": true,           // unchanged
  "username": "Isaiah",            // unchanged
  "trial": true,                   // NEW: the website trial route is configured
  "inGuild": true,                 // NEW: LIVE membership check (signed-in users only)
  "membershipUnknown": false,      // NEW: true when the live check itself failed
  "joinUrl": "https://discord.gg/yTuekgdgEN" // NEW: from DISCORD_INVITE_URL ("" if unset or not a discord.gg/discord.com HTTPS URL)
}
```

- `trial` is true when Discord sign-in is configured and the Worker can reach the backend (`ORION_API_BASE`, `ORION_BOT_SECRET`, `ORION_EDGE_AUTH`). It does not depend on Stripe.
- Signed out: `inGuild:false, membershipUnknown:false` and **no backend call**.
- Signed in: one backend call (`/api/bot/guild-member`, 5 s timeout, no retries). Member → `inGuild:true`. Not a member → `inGuild:false`. Check failed → `inGuild:false, membershipUnknown:true` (fail-soft, because config only drives the UI; the purchase routes re-check and fail closed).
- The response is `Cache-Control: no-store`.

### 1.2 `POST /api/trial` (new)

Request: same-origin `fetch('/api/trial', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })`. The body is never read. **The Discord ID comes only from the signed session cookie**; any `discord_id` in the body or query string is ignored.

Checks, in order:

| # | Check | Failure response |
|---|---|---|
| 1 | Trial route configured | 503 `{ok:false, code:"unavailable", message}` |
| 2 | `Origin` is `https://zaeorion.com`, `https://www.zaeorion.com`, or the request's own origin (local `wrangler dev`, preview URLs) | 403 `{ok:false, code:"forbidden_origin", error:"forbidden_origin", message}` |
| 3 | `Content-Type` is `application/json` (parameters like `; charset=utf-8` allowed) | 415 `{ok:false, code:"unsupported_media_type", …}` |
| 4 | Valid signed session | 401 `{ok:false, code:"auth_required", error:"Sign in with Discord to start your free trial.", message, authUrl:"/auth/discord"}` |
| 5 | Live server membership (`purpose:"trial"`) | Not a member: 403 `{ok:false, code:"join_required", error:"join_required", message, joinUrl}`. Check failed: 503 `{ok:false, code:"membership_unavailable", error:"membership_unavailable", message}` |
| 6 | Backend `POST /api/bot/trial {discord_id, actor_discord_id}` (both from the session) | mapped below |

Other methods on `/api/trial` → 405 with `Allow: POST`.

Backend result → site response. Every body is `{ ok, code, message }`. The site copy is fixed in the Worker; the backend's Discord-worded `message` is never shown.

| Backend reply | HTTP | `code` | `message` |
|---|---|---|---|
| 200 `ok:true` | 200 | `issued` | Your 3-day trial is active. Check your Discord DMs to connect Venice. |
| 200 `code:"already_claimed"` | 409 | `already_claimed` | You've already used your free trial. Subscribe to keep going. |
| 200 `code:"dm_failed"` (DM bounced, claim rolled back) | 422 | `dm_failed` | We couldn't DM you. In the Venice server, open Privacy Settings and allow Direct Messages, then try again. |
| 429 | 429 | `rate_limited` | Too many attempts. Wait a few minutes and try again. |
| 403 `blacklisted`, backend auth failure, 5xx, network error, anything unexpected | 503 | `unavailable` | We couldn't start your trial right now. Please try again later, or open a ticket in the Venice Discord. |

A blacklisted account gets the same neutral error as an outage. The Worker log line records the difference.

For a Worker deployed ahead of the backend, the Worker also recognises the pre-`code` backend messages ("already claimed", "couldn't DM you"). The deploy order below still puts the backend first.

Log line (one per request, no full Discord ID):
`{"event":"site_trial","result":"issued","backendStatus":200,"backendCode":"issued","discord":"...5678"}`

### 1.3 `POST /api/checkout/session` (changed)

After the existing 401 session check and **before any Stripe call**:

- Not a member → 403 `{ error:"join_required", message, joinUrl }`
- Membership check failed → 503 `{ error:"membership_unavailable", message:"We couldn't confirm your Discord membership. Please try again in a minute." }`. It fails closed so we never take money we can't deliver.

Messages:
- `join_required`: "Join the Venice Discord server first — Venice confirms your access and sends your setup steps there by DM."
- `membership_unavailable`: "We couldn't confirm your Discord membership. Please try again in a minute."

The coordinator's first draft of the join message said "your key is delivered there by DM". I reworded it because no key is DM'd: access is linked to the Discord account and the app is unlocked with a one-time code from `/connect`.

Checkout Session body changes (nothing else changed):
- `payment_method_types[0]=card` is **added**. Cards also cover Apple Pay and Google Pay. Delayed-settlement methods complete Checkout as `unpaid`, and the initial `invoice.paid` (`subscription_create`) is deliberately ignored, so those buyers would be charged and never provisioned.
- `automatic_tax[enabled]` is **removed**. Stripe Tax is not activated on the live account, so every live session creation would fail with it set. **Before re-enabling it, activate Stripe Tax in the live dashboard.** A test asserts the key is absent.

Every Stripe API call now sends `Stripe-Version: 2026-08-26.dahlia`, the live webhook endpoint's version. This way subscriptions and checkout sessions fetched by the Worker have the same shape as the events it verifies. **If the webhook endpoint's API version is upgraded, bump `STRIPE_API_VERSION` in the same release.**

Renewal fix (live audit B3): `invoice.paid` renewals now require `object.status === "paid"` instead of `object.paid === true`. Stripe removed `paid` from invoices in 2025-03-31.basil, so on dahlia every renewal would have thrown, and customers would have lost access at day 30 after paying. The `billing_reason === "subscription_cycle"` and `amount_paid > 0` checks are unchanged.

### 1.4 Discord sign-in (changed)

- `GET /auth/discord` now requests `scope=identify guilds.join`. The consent screen will add "Join servers for you".
- `GET /auth/discord/callback`: after the identity is verified, the Worker calls the backend **once** (best effort, 5 s timeout): `POST /api/bot/guild-join {discord_id, access_token}`.
  - A failure of any kind (backend error, timeout, token granted without `guilds.join`, backend not configured) is logged and **the login continues**.
  - The access token is used only for this call. It is not put in the session cookie, the response or any log line (tested, including when an error message contains the token).
- New session payload fields (in the signed, HttpOnly cookie, so page JS can't read them):
  - `guildJoin`: `"joined"`, `"already_member"` or `"failed"`
  - `inGuild`: `guildJoin !== "failed"`
  - The front end should use `GET /api/checkout/config`, which is live.

Log line: `{"event":"guild_join","result":"joined","status":200,"backendCode":"","discord":"...5678"}`

### 1.5 Backend routes (new: `backend/lambda_function.py`)

Both new routes accept **only the website Worker's secret** (`/orion/worker_bot_secret`, constant `WORKER_BOT_CONSUMER`). Any other valid bot secret gets 403 plus an audit row (`result:"forbidden_consumer"`). Edge auth still applies first. Bodies are capped at 4 KiB (413) and must be a JSON object (400 `invalid_json`). `discord_id` must be 16-22 ASCII digits (400 `invalid_discord_id`).

**`POST /api/bot/guild-join`** `{ discord_id, access_token }`
- `access_token`: a string of 16-512 characters from `[A-Za-z0-9-._~+/=]`, otherwise 400 `invalid_access_token`.
- Rate limit: 5 per account per 10 minutes → 429 `{error:"rate_limited", result:"failed"}`.
- Blacklisted account → 403 `{error:"not_allowed", result:"failed"}`, and Discord is not called.
- Calls Discord `PUT /guilds/{guild_id}/members/{discord_id}` with the bot token and `{"access_token": …}`. The guild ID and bot token come from SSM (`/orion/discord_guild_id`, `/orion/discord_bot_token`), as in `_discord_add_role`.
  - 201 → 200 `{ok:true, result:"joined"}`
  - 204 → 200 `{ok:true, result:"already_member"}`
  - Anything else → 502 `{ok:false, error:"guild_join_failed", result:"failed", discord_status, discord_code}`
- Every attempt writes an `audit` row (`action:"discord.guild_join"`, `target`=Discord ID, `details:{discord_status, discord_code}`). The token is never audited or printed. Discord's error body is read only for its numeric `code` and is never logged.

**`POST /api/bot/guild-member`** `{ discord_id, purpose? }` (`purpose` ∈ `config` | `checkout` | `trial`; default `config`)
- Rate limit: 30 per account per minute → 429.
- Calls Discord `GET /guilds/{guild_id}/members/{discord_id}`. A single-member fetch does not need the privileged Server Members intent.
  - 200 → `{ok:true, member:true}`
  - 404 with Discord code 10007 (Unknown Member), 10013 (Unknown User) or no code → `{ok:true, member:false}`
  - Anything else → 502 `{ok:false, error:"membership_unavailable", discord_status, discord_code}`. This includes 404 with 10004 (Unknown Guild), which means the guild ID is misconfigured and must not show up as "join the server".
- Denials and failures are audited (`action:"discord.membership_check"`, `result:"not_member"|"unavailable"`) **only for `checkout` / `trial`**. Config polls just print a line.

**`POST /api/bot/trial`** (existing, additive changes)
- Every outcome now carries a machine `code`: `issued`, `already_claimed`, `dm_failed`, `blacklisted`, `rate_limited`. The Discord bot and the interactions Worker still read `ok` + `message` and are unaffected.
- **New throttle: 5 claims per Discord account per 10 minutes** (429 `rate_limited`). This also applies to `/claim_trial` in Discord. Reason: the site makes the route scriptable, and with DMs closed each attempt costs two Discord API calls that 403. Discord bans a bot's IP after 10,000 invalid requests in 10 minutes.
- Trial logic is otherwise untouched (one per account, one per PC, blacklist, DM with rollback, trial role, no key returned).

DM copy (one field each):
- Trial DM: new field "When it ends": "The trial ends automatically, with no card and no charge. Subscribe any time at https://zaeorion.com"
- Paid DMs (new subscription and renewal): new field "Manage or cancel" → `BILLING_PORTAL_URL` (`https://billing.stripe.com/p/login/5kQ7sL0Ec4Ya5MfcFsgQE00`, the public Stripe customer portal).

---

## 2. UX states for the front end

`app.js` fetches `/api/checkout/config` once per page load, which fits the 30/min per-account limit. After the user clicks the join link, re-fetch config ("I've joined, check again").

| State (from config) | Show |
|---|---|
| `authenticated:false` | "Sign in with Discord" → `/auth/discord?return_to=/%23pricing`. Hide the buy and trial buttons. |
| signed in, `inGuild:false`, `membershipUnknown:false` | **"Join the Venice Discord to continue"** → `joinUrl` (new tab), **instead of** the buy and trial buttons, plus a "check again" control. |
| signed in, `membershipUnknown:true` | Keep the buttons but add a note: "We couldn't confirm your Discord membership. If you haven't joined yet, join here first" → `joinUrl`. The server re-checks at purchase time and answers 503 `membership_unavailable` if the outage persists. |
| signed in, `inGuild:true` | "Subscribe · $25/month" (existing embedded Checkout) and, when `trial:true`, "Start free 3-day trial". |

Trial button result handling (switch on `code`; display `message`):

| `code` | UI |
|---|---|
| `issued` | Success state + message. Point to `/connect` once the DM arrives. |
| `already_claimed` | Message + subscribe CTA |
| `dm_failed` | Message + "Try again" button |
| `join_required` | Message + `joinUrl` link, then re-check |
| `membership_unavailable`, `rate_limited`, `unavailable` | Message + retry later |
| `auth_required` | Navigate to `authUrl` |
| `forbidden_origin`, `unsupported_media_type` | Should never happen from the site (developer error) |

Checkout: for `join_required` and `membership_unavailable`, `error` is a **code** and `message` is the prose. The older 401/502/503 checkout errors still put prose in `error`. **`public/app.js` currently shows `payload.error`** (line ~165: `throw new Error(payload.error || …)`), so it would print the raw code `join_required`. It should use `payload.message || payload.error`, and on `join_required` show `payload.joinUrl`. (That file belongs to the redesign agent; I did not edit it.)

---

## 3. Deploy order

**Backend Lambda FIRST, then the Worker.** A Worker running ahead of the backend gets 404 from `/api/bot/guild-member`. Membership then reads as "unknown", and **checkout and the trial fail closed with 503 for everyone**. The login auto-join would also fail silently. The backend changes are purely additive, so the backend can safely go first.

1. **Backend** (`orion-activate`): build the zip from the final `backend/lambda_function.py` (the packaging agent owns the zip). No new SSM parameters, tables or IAM permissions: it reuses `/orion/discord_bot_token`, `/orion/discord_guild_id`, `/orion/worker_bot_secret`, `orion-ratelimit`, `orion-audit` and `orion-licenses`.
2. **API Gateway** (`v348t5hg3i`): confirm the new paths reach the Lambda. If the HTTP API uses a catch-all (`$default` / `ANY /{proxy+}`), nothing to do. If it lists explicit routes, add `POST /api/bot/guild-join` and `POST /api/bot/guild-member` → the `orion-activate` integration. Read-only check: `aws apigatewayv2 get-routes --api-id v348t5hg3i`. *Not verified here.*
3. **Backend smoke test** (owner, with the Worker secret + edge secret): `POST /api/bot/guild-member {"discord_id":"<your id>"}` → `{"ok":true,"member":true}`. A 502 with `discord_code:10004` means the guild ID is wrong. A 403 `forbidden` means the wrong secret.
4. **Worker**: `npx wrangler deploy --env production` from `website/`. `DISCORD_INVITE_URL` ships as a plain var. No new secrets.
5. **Stripe dashboard**:
   - The webhook endpoint's API version must be `2026-08-26.dahlia` to match `STRIPE_API_VERSION`.
   - Stripe Tax stays off (automatic tax removed). Re-enable in code only after activating Stripe Tax in the live dashboard.
   - **The Venice price must not be purchasable from any other Stripe surface**: no Payment Links, pricing tables, or dashboard-created subscriptions or invoices on that price. A purchase without the site's Discord metadata can't be provisioned. The existing webhook already refuses to provision without a verified `discord_user_id` on the subscription, so such a buyer would be charged and get nothing.
6. **Post-deploy check**: sign in on zaeorion.com with a test account that is NOT in the server. Expect the consent screen to list "Join servers for you", and after login you should be in the server. Then `/api/checkout/config` shows `inGuild:true`. Leave the server; config should show `inGuild:false`, and the buy button should be replaced by the join link.

Rollback: `wrangler rollback` for the Worker. The backend changes are additive (the old Worker never calls the new routes), so the new Lambda can stay. Rolling back the Lambda while the new Worker is live would close checkout (see above).

## 4. Discord Developer Portal checks

- **Redirect URI**: `https://zaeorion.com/auth/discord/callback` is already registered and unchanged.
- **`guilds.join` scope**: needs no extra approval or verification to add users to a server where the app's own bot is a member. **The bot and the OAuth2 client must be the same application** (`DISCORD_CLIENT_ID` 1549481446782017668): Discord only accepts an access token granted to the bot's own application. Confirm `/orion/discord_bot_token` belongs to that application.
- **Bot permission**: the bot needs **Create Instant Invite** in the Venice server. Without it every join returns 403 (code 50013) → `guildJoin:"failed"`, and users have to join through `joinUrl`.
- **Server Members intent**: not needed. Both routes use single-member REST calls.
- Users banned from the server can't be added, and they read as not-a-member, so they can't buy. That is intended.
- If the server uses Membership Screening or Rules Screening, auto-joined users are `pending` until they accept. The single-member GET still returns 200, so they count as members. **Not verified: whether the bot can DM a still-pending member.**
- Users already in 100 servers (200 with Nitro) get code 30001 → join fails → they must leave a server or join manually.

## 5. Tests

Run on 2026-09-19 (Windows, Node 24.18.1, `.venv` Python 3.12, `--basetemp=D:/NexusVision/pytest_tmp/trial`):

| Suite | Result |
|---|---|
| `npm run check` in `website/` (verify_site.py on the redesign agent's current `public/` + `node --test tests/worker.test.mjs`) | VERIFY_OK, **37/37** (was 22; +15 new) |
| `tests/backend` (full) | **320 passed** (baseline 271; +49 new) |
| `tests/backend/test_website_guild.py` (new, included in the full count above) | **49/49**, stable over 3 consecutive runs |
| `tests/discord` (full) | **127 passed, 1 failed**, the same pre-existing failure as baseline (see section 6) |

A mutation check on a scratch copy of `worker.js` confirmed the new tests catch all 17 seeded regressions:
- skipping the membership gate (trial, checkout)
- using a body-supplied ID
- logging the token, or putting it in the cookie
- `identify`-only scope
- a join failure that breaks login
- removing the Origin check, or the Content-Type check
- membership outage failing open
- config never reporting unknown
- passing through the backend copy
- the legacy `paid` check
- no `Stripe-Version`
- no card-only
- `automatic_tax` back
- full Discord ID in logs

`tests/discord/test_discord_user_agent.py` pins how many times the backend sets a Discord User-Agent. I bumped it from 4 to 5 because the new guild-member helper also sends the required `DiscordBot (…)` UA.

## 6. Not verified / open items

- None of this was exercised against live Discord, Stripe or AWS: the 201/204 join semantics, the Create Instant Invite requirement, the same-application requirement, dahlia acceptance of `payment_method_types` with `ui_mode: "embedded_page"`, and the API Gateway routing.
- The trial throttle also applies to `/claim_trial` in Discord (5 attempts / 10 min / account).
- Sign-in now waits for one extra backend round trip (capped at 5 s), and every signed-in config fetch costs one backend call + one Discord GET.
- `public/app.js` still reads `payload.error` for checkout failures (section 2), which is for the redesign agent.
- The `/discord` account page (inside `worker.js`) still tells users to run `/claim_trial` in Discord. It works, but it doesn't mention the website trial. I left it alone because an existing test pins it and nobody asked for the change.
- Pre-existing and unrelated: `tests/discord/test_venice_guard_refresh.py::test_status_prefers_discord_id_and_fail_closed_legacy_lookup` fails before and after this change. It checks the bot's `/status` command signature.
