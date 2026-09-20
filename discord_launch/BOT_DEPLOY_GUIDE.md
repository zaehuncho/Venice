# Orion Gateway Bot — Deploy Guide

## What you sell (owner rule 2026-09-15)

One product line. The price is written down in exactly ONE place — the website — so changing it
never needs a bot or Worker deploy.

| | |
|---|---|
| **Free trial** | 3 days, `/claim_trial`. One per Discord account **and** one per PC. |
| **Subscription** | **$25 / month, recurring.** |
| **Activation fee** | one-time, cheap, charged on the first purchase only. |
| ~~Lifetime / Day / Week~~ | **Not sold.** Still accepted on keys already issued and for staff comps (`/keygen plan:lifetime`), advertised nowhere. |

`/purchase` is one ephemeral embed and one button pointing at `STORE_URL`. No tiers, no prices.

### The two-product Gumroad checkout

Gumroad cannot charge a one-time fee and open a subscription in the same product, so the owner
creates **two** products. Both need **key generation ON** (the webhook verifies the Gumroad licence
key) and both need the required custom checkout field labelled exactly **`Discord ID`**.

| Product | Type | Permalink (slug) | Ping does |
|---|---|---|---|
| **Venice — Monthly** | **Membership**, $25 / month, 30-day billing period | `orion-monthly` | First charge **mints** the key and DMs it. Every later charge **renews** it: `renew: true` → `/api/bot/provision` adds 30 days to the SAME key and DMs "subscription renewed" (no key is re-sent). |
| **Venice — Activation** | One-time product, the cheap activation fee | `orion-activation` | **Mints nothing.** Recorded in `orion-gumroad-orders` as `plan=activation_fee` and DMs the buyer that the subscription is a separate checkout. |

Both pings go to the SAME Ping URL (`/api/gumroad/webhook/<token>`) — the Lambda routes on the
permalink. Point Gumroad's Ping at the direct execute-api origin.

Set on the webhook Lambda:

```
GUMROAD_ACTIVATION_PRODUCT = orion-activation     # default; "" disables the path
GUMROAD_HWID_RESET_PRODUCT = ""                   # LEGACY — see "HWID resets" below
OWNER_DISCORD_USER_ID      = your Discord user id (or SSM /orion/owner_discord_user_id)
```

> **Do not rename the slugs afterwards.** `product_permalink` is the join key the ping matches on;
> renaming it breaks delivery silently. The legacy slugs (`orion-30day`, `orion-7day`,
> `orion-lifetime`, …) stay in `PRODUCT_MAP` on purpose so refund and dispute pings on keys already
> sold still resolve to a plan — they are not offered anywhere.

### HWID resets

Three free resets per key through `/hwid_reset`. After that each reset **deducts one day from the
subscription**, and the bot asks the customer to confirm first — nothing is deducted until they
press the button. Trial keys get the same 3 free resets but have no subscription to take a day
from, so the 4th is refused with an explanation. Staff resets never consume the customer's count.

There is **no reset to buy**: no `GUMROAD_HWID_RESET_SLUG`, no `HWID_RESET_BUY_URL`, and no reply
links a store. `/api/bot/hwid-credit` and `hwid_paid_credits` survive so staff can hand out a
goodwill reset; a credit is spent before any day is deducted.

## What runs where
- **Lambda** (`backend/lambda_function.py`, already coded): SellHub webhook delivery (mint→DM→role), the bot endpoints `/api/bot/*`, and the **launcher killswitch** gate on `/api/activate` (killswitch state is set only via `/api/admin/kill|unkill`, owner-secret-gated — never from Discord). The owner deploys this.
- **Gateway bot** (`discord_launch/orion_bot.py`): runs 24/7 on a host; owns `/claim_trial` `/hwid_reset` `/status` `/setup` `/faq` and the staff commands `/deliver` `/keygen` `/lookup`, plus the green "online" presence. Thin layer — all license logic lives in the Lambda. The killswitch is owner-console-only (`OrionOwner.exe` / `orion-admin` CLI) — not a bot command, and `/api/bot/killswitch` is status-only (contract §5).
  - Staff commands are **not** gated on the Discord ADMINISTRATOR bit any more (contract §4 BREAKING): the bot forwards `actor_discord_id` and the server resolves it against `orion-staff`. `/deliver` and `/keygen` are registered with no default permissions, so after the first sync grant the Staff role access in Server Settings → Integrations → Command Permissions (visibility only).
  - `/hwid_reset` implements the contract §3 ladder: 3 free → staff goodwill credit → *confirmed* "deduct 1 day" (a button that re-calls with `mode: "deduct"` — nothing is deducted until it is pressed). A trial that runs out is refused (`trial_no_deduct`), never charged.

## 1. Create the bot (owner — MFA)
- Dev Portal → New Application **Orion License Bot** → Bot → Reset Token → copy.
- **No privileged intents needed** (leave them off).
- OAuth2 → URL Generator → scopes **`bot` AND `applications.commands`** *(both — `applications.commands` is required for slash commands; the earlier invite was missing it)* → bot perms **Manage Roles, Send Messages, Embed Links** → open the URL → add to the server.
- Server Settings → Roles → drag the bot role **above** Customer / Lifetime / Trial.

## 2. Roles (create any missing)
Customer `#22C55E` (hoist) · Lifetime `#FACC15` (hoist — legacy, staff comps only) · Trial `#6366F1` · Verified `#94A3B8` · Muted `#475569` (deny Send/React). Developer Mode on → right-click each → Copy Role ID.

## 3. Secrets — generate one bot-service secret, then put it in BOTH places
Generate: `openssl rand -hex 32` (or any 64-char random hex).

**AWS SSM (SecureString) — used by the LAMBDA:**
| Param | Value |
|---|---|
| `/orion/sellhub_webhook_secret` | from SellHub (step 6) |
| `/orion/discord_bot_token` | the bot token |
| `/orion/discord_guild_id` | your server id |
| `/orion/discord_customer_role_id` | Customer role id |
| `/orion/discord_lifetime_role_id` | Lifetime role id |
| `/orion/bot_service_secret` | the generated secret |

**Bot host ENV VARS — used by the GATEWAY BOT:**
```
DISCORD_BOT_TOKEN = the bot token
ORION_BOT_SECRET  = the SAME generated secret as /orion/bot_service_secret
ORION_API_BASE    = https://v348t5hg3i.execute-api.us-east-1.amazonaws.com
ORION_GUILD_ID    = your server id
CUSTOMER_ROLE_ID  = Customer role id
LIFETIME_ROLE_ID  = LEGACY/optional — only applied when staff deliver a lifetime comp.
                    Leave unset if you never comp; it is a no-op at 0.
TRIAL_ROLE_ID     = Trial role id
ORION_LOGO_URL    = your logo CDN url (optional, embed thumbnail)
ORION_ADMIN_LOG   = staff-chat   (optional; channel for admin alerts)

# REQUIRED since the HIGH-3/HIGH-4 hardening — require_edge_auth() is unconditional
# on EVERY route, /api/bot/* included. Without it every command 403s.
ORION_EDGE_AUTH   = the SAME value as SSM /orion/edge_auth_secret

# Admin Panel V2 (docs/ADMIN_PANEL_V2_CONTRACT.md)
STAFF_ROLE_IDS      = comma-separated Discord role ids; CLIENT-SIDE gate for /lookup of
                      OTHER users only. /deliver and /keygen are gated by the SERVER
                      (orion-staff, role admin+) — never by this list.
# REMOVED 2026-09-15: HWID_RESET_BUY_URL. Customers cannot buy a reset; /hwid_reset
# links no store. Delete it from the host env.
ORION_STAFF_ID      = optional; the staff identity the bot logs in as for KEY lookups
ORION_STAFF_MACHINE_ID = optional; machine_id used at that enrollment (default orion-bot-host)
```
> Use the **direct execute-api** URL for `ORION_API_BASE` (and the SellHub webhook) — Cloudflare Bot Fight Mode on `api.zaeorion.com` blocks server-to-server calls.

## 4. Deploy the Lambda
Deploy the updated `backend/lambda_function.py`. Sanity: `GET https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/version` → ok.

## 5. Host the bot (24/7) — pick one
Needs `pip install -U "discord.py>=2.3" aiohttp`, then `python orion_bot.py`.
- **Railway** (easiest): New Project → deploy from a GitHub repo (push `orion_bot.py` + a `requirements.txt` with `discord.py>=2.3` and `aiohttp`) → add the env vars → deploy.
- **Render/Heroku:** a *worker* process running `python orion_bot.py`.
- **VPS:** run it under `systemd`/`pm2`/`screen` so it stays up.
On boot it syncs the slash commands and shows **online**.

## 6. SellHub webhook
Dash → Webhooks → URL = `https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/sellhub/webhook`. Events: order **paid/completed** + **refunded/chargeback**. Copy the signing secret → SSM `/orion/sellhub_webhook_secret`. Product delivery = the static "check your DMs" message.

## 7. Test
- `/purchase` → ONE embed, ONE button, and it opens `STORE_URL`. No prices in the message.
- `/claim_trial` → DMs the Discord connection link + Trial role; the private database key is not delivered. Run again → "already claimed".
- Activate the trial on a PC; a 2nd trial (alt account) on the **same PC** → "device already used a trial".
- Buy the **activation fee** with a 100%-off coupon → DM says the subscription is a separate
  checkout, and **no key is issued**.
- Buy the **membership** with a 100%-off coupon → webhook → DM key + Customer role + `#sales-log`.
- Force a recurring charge (Gumroad test ping with `is_recurring_charge=true`) → the SAME key gains
  30 days and the DM says "subscription renewed". A second key appearing here is the bug to catch.
- `/hwid_reset` → unbinds and says "N free resets left"; immediate re-run → "try again in 24h";
  the 4th asks to confirm **1 day** off the subscription and only deducts after the button.
- `/hwid_reset` on an exhausted **trial** → refusal, no deduction, machine stays bound.
- `/deliver @user month` → DMs a month key + Customer role.
- Killswitch: `orion-admin killswitch status|engage|release` (or `OrionOwner.exe`) — NOT a Discord command. `engage` → the launcher's activation returns "temporarily disabled" → app won't start and running sessions lock within ~5 min via the heartbeat; `release` → restored.

## Endpoints summary (all on the direct origin)
`/api/sellhub/webhook` (SellHub HMAC) · `/api/bot/trial` `/api/bot/hwid-reset` `/api/bot/deliver` (bot secret) · `/api/admin/kill` `/api/admin/unkill` `/api/admin/status` (owner secret — killswitch) · `/api/activate` (killswitch-gated). The `/api/bot/killswitch` route still exists in the Lambda for backward compatibility but no Discord command triggers it anymore (the gateway bot's startup selfcheck still probes it read-only). The `/api/discord/interactions` + `/api/discord/register-commands` routes also exist but are **unused** in the gateway setup — don't set a Discord "Interactions Endpoint URL", or it'll fight the gateway bot.
