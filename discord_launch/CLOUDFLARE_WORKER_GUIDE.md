# Orion License Bot — Cloudflare Worker deploy

Serverless Discord bot on your Cloudflare zone. The Worker (`orion_worker.js`) verifies Discord's signature and **proxies** each command to the Lambda's `/api/bot/*` — the Lambda mints keys, DMs them, and assigns roles. No always-on host, no KV, no license logic in the Worker. The launcher **killswitch is NOT reachable from Discord** — it's owner-console-only (`OrionOwner.exe` / `orion-admin` CLI, `/api/admin/kill|unkill|status`).

> This **supersedes** the gateway bot (`orion_bot.py`) — deploy one, not both. Tradeoff vs. the gateway bot: no green "online" dot (HTTP interactions). All commands/DMs/roles still work.

## 1. Bot app (owner — MFA)
Dev Portal → New Application **Orion License Bot** → Bot → Reset Token (copy). From **General Information** copy the **Application ID** and the **Public Key**. OAuth2 → URL Generator → scopes **`bot` + `applications.commands`** → perms **Manage Roles, Send Messages, Embed Links** → add to the server. Drag the bot role **above** Customer/Lifetime/Trial. No privileged intents.

## 2. Roles + IDs (Developer Mode on)
Create Customer/Lifetime/Trial/Verified/Muted (per `SERVER_BLUEPRINT.md`); copy each Role ID + the Server ID.

## 3. SSM (Lambda side — SecureString)
`/orion/bot_service_secret` (random, e.g. `openssl rand -hex 32`), `/orion/discord_bot_token`, `/orion/discord_guild_id`, `/orion/discord_customer_role_id`, `/orion/discord_lifetime_role_id`, `/orion/discord_trial_role_id` *(optional)*, `/orion/sellhub_webhook_secret`.

## 4. Deploy the Lambda
Deploy `backend/lambda_function.py` (has `/api/bot/*`, the SellHub webhook, `register-commands`). Sanity: `GET https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/version`.

## 5. Deploy the Worker
```
npm i -g wrangler && wrangler login
# in a folder with orion_worker.js + wrangler.toml:
wrangler secret put DISCORD_PUBLIC_KEY     # paste the app Public Key (hex)
wrangler secret put DISCORD_APP_ID         # the Application ID
wrangler secret put ORION_BOT_SECRET       # the SAME value as SSM /orion/bot_service_secret
wrangler deploy
```
Note the URL (`https://orion-license-bot.<account>.workers.dev`, or add a custom route on `zaeorion.com`). Secrets stay in Worker secrets — never in `wrangler.toml`.

## 6. Discord Interactions URL
Dev Portal → General Information → **Interactions Endpoint URL** = the Worker URL → Save. Discord sends a signed PING; with the real Ed25519 verify it saves with a green check. *(Fails = wrong DISCORD_PUBLIC_KEY secret, or an old `compatibility_date`.)*

## 7. Register the 3 commands (owner, once)
```
curl -X POST "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/discord/register-commands" \
     -H "X-Orion-Admin-Secret: <ADMIN_SECRET>"
```
→ registers `claim_trial`, `hwid_reset`, `deliver` (guild = instant). The launcher killswitch is NOT a Discord command — see `OrionOwner.exe` / `orion-admin killswitch`.

## 8. SellHub webhook
Dash → Webhooks → `https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/sellhub/webhook`; events **paid/completed + refunded/chargeback**; signing secret → SSM `/orion/sellhub_webhook_secret`; product delivery = the "check your DMs" static message.

## 9. Test
- `/claim_trial` → DMs a key + Trial role; re-run → "already claimed"; a 2nd trial on the SAME PC (alt) → "device already used a trial".
- `/hwid_reset` → unbinds; immediate re-run → "try again in 24h".
- `/deliver user:@x plan:month` → DMs a month key + Customer role.
- Buy with a 100%-off coupon → webhook → DM key + Customer role + `#sales-log`.

## Why this is secure (vs. the draft)
Real Ed25519 (no `return true` stub) → no forged interactions. The launcher killswitch is owner-console-only, not a bot command → no Discord-reachable service-disable switch. Bot calls use `X-Orion-Bot-Secret` (not the admin secret). Secrets in Worker-secrets/SSM only. Deferred replies → no 3-second timeout. Trial abuse-locked per-account + per-HWID server-side.
