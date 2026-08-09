# Orion Gateway Bot — Deploy Guide

## What runs where
- **Lambda** (`backend/lambda_function.py`, already coded): SellHub webhook delivery (mint→DM→role), the bot endpoints `/api/bot/*`, and the **launcher killswitch** gate on `/api/activate` (killswitch state is set only via `/api/admin/kill|unkill`, owner-secret-gated — never from Discord). The owner deploys this.
- **Gateway bot** (`discord_launch/orion_bot.py`): runs 24/7 on a host; owns `/claim_trial` `/hwid_reset` `/deliver` + the green "online" presence. Thin layer — all license logic lives in the Lambda. The killswitch is owner-console-only (`OrionOwner.exe` / `orion-admin` CLI) — not a bot command.

## 1. Create the bot (owner — MFA)
- Dev Portal → New Application **Orion License Bot** → Bot → Reset Token → copy.
- **No privileged intents needed** (leave them off).
- OAuth2 → URL Generator → scopes **`bot` AND `applications.commands`** *(both — `applications.commands` is required for slash commands; the earlier invite was missing it)* → bot perms **Manage Roles, Send Messages, Embed Links** → open the URL → add to the server.
- Server Settings → Roles → drag the bot role **above** Customer / Lifetime / Trial.

## 2. Roles (create any missing)
Customer `#22C55E` (hoist) · Lifetime `#FACC15` (hoist) · Trial `#6366F1` · Verified `#94A3B8` · Muted `#475569` (deny Send/React). Developer Mode on → right-click each → Copy Role ID.

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
LIFETIME_ROLE_ID  = Lifetime role id
TRIAL_ROLE_ID     = Trial role id
ORION_LOGO_URL    = your logo CDN url (optional, embed thumbnail)
ORION_ADMIN_LOG   = staff-chat   (optional; channel for admin alerts)
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
- `/claim_trial` → DMs a trial key + Trial role; run again → "already claimed".
- Activate the trial on a PC; a 2nd trial (alt account) on the **same PC** → "device already used a trial".
- Buy with a 100%-off coupon → webhook → DM key + Customer role + `#sales-log`.
- `/hwid_reset` → unbinds; immediate re-run → "try again in 24h"; 4th in 30 days → "open a ticket".
- `/deliver @user month` → DMs a month key + Customer role.
- Killswitch: `orion-admin killswitch status|engage|release` (or `OrionOwner.exe`) — NOT a Discord command. `engage` → the launcher's activation returns "temporarily disabled" → app won't start and running sessions lock within ~5 min via the heartbeat; `release` → restored.

## Endpoints summary (all on the direct origin)
`/api/sellhub/webhook` (SellHub HMAC) · `/api/bot/trial` `/api/bot/hwid-reset` `/api/bot/deliver` (bot secret) · `/api/admin/kill` `/api/admin/unkill` `/api/admin/status` (owner secret — killswitch) · `/api/activate` (killswitch-gated). The `/api/bot/killswitch` route still exists in the Lambda for backward compatibility but no Discord command triggers it anymore (the gateway bot's startup selfcheck still probes it read-only). The `/api/discord/interactions` + `/api/discord/register-commands` routes also exist but are **unused** in the gateway setup — don't set a Discord "Interactions Endpoint URL", or it'll fight the gateway bot.
