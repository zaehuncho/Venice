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

**STOP — confirm the Worker name first (do not skip).** `wrangler.toml` says `name = "orion-license-bot"`, and as of 2026-08-04 **no Worker by that name exists** on the account. The two that do are `orion-discord-bot` and `license-redeem-proxy`, and the docs disagree about which one Discord actually calls. Deploying as-is publishes a *third* Worker: nothing errors, and the commands keep running the old code.

1. Dev Portal → your app → **General Information → Interactions Endpoint URL**. Read the hostname.
2. Match it to a Worker: `wrangler deployments list` / the Cloudflare dashboard (Workers & Pages).
3. Set `name` in `wrangler.toml` to **that** Worker. Only then deploy.

```
npm i -g wrangler && wrangler login
# in a folder with orion_worker.js + wrangler.toml:
wrangler secret put DISCORD_PUBLIC_KEY     # paste the app Public Key (hex)
wrangler secret put DISCORD_APP_ID         # the Application ID
wrangler secret put ORION_BOT_SECRET       # the SAME value as SSM /orion/bot_service_secret
wrangler secret put ORION_EDGE_AUTH        # the SAME value as SSM /orion/edge_auth_secret
wrangler deploy
```
`ORION_EDGE_AUTH` is **not optional**: the HIGH-3/HIGH-4 hardening made `require_edge_auth()` unconditional for every route, `/api/bot/*` included — without it every command 403s before it reaches the bot-secret gate.

`[vars]` in `wrangler.toml` (non-secret): `ORION_API_BASE`, **`STORE_URL`** (the website `/purchase` links to — set it or `/purchase` drops the button and says so), `STAFF_ROLE_IDS` (comma-separated; client-side gate for `/lookup` of other users only).

`GUMROAD_BASE` survives only as a fallback for `STORE_URL` so a Worker deployed before this change keeps a working button. `GUMROAD_HWID_RESET_SLUG` is **gone**: under the owner's 2026-09-15 rule a customer gets 3 free HWID resets and then pays 1 day off the subscription, so `/hwid_reset` links no store at all.

Note the URL, or add a custom route on `zaeorion.com`. Secrets stay in Worker secrets — never in `wrangler.toml`.

## 6. Discord Interactions URL
Dev Portal → General Information → **Interactions Endpoint URL** = the Worker URL → Save. Discord sends a signed PING; with the real Ed25519 verify it saves with a green check. *(Fails = wrong DISCORD_PUBLIC_KEY secret, or an old `compatibility_date`.)*

## 7. Register the commands (owner, re-run on every command change)
The old `/api/discord/register-commands` Lambda route **does not exist** on the deployed bundle (`LAMBDA_RECONCILIATION.md`). Commands are pushed straight to Discord from `discord_commands.json`:
```
export DISCORD_BOT_TOKEN=...   DISCORD_APP_ID=...   ORION_GUILD_ID=...
python register_commands.py --dry-run     # prints what would be sent
python register_commands.py               # bulk PUT, guild-scoped = instant
```
→ `purchase`, `claim_trial`, `hwid_reset`, `deliver`, `keygen`, `lookup`. Bulk-overwrite: anything not in the JSON is removed from the guild. Do **not** run this while the gateway bot (`orion_bot.py`) is the deployed front-end — it syncs its own tree on boot and the two fight.

**Then grant the staff role access**: `/deliver` and `/keygen` ship with `default_member_permissions: "0"`, which hides them from everyone but Discord administrators. Server Settings → Integrations → *(the app)* → **Command Permissions** → allow the Staff role. That is *visibility only* — authorization is the license server's `orion-staff` check on `actor_discord_id` (contract §1/§4).

The launcher killswitch is NOT a Discord command, and `/api/bot/killswitch` is status-only (contract §5) — flip global kill from `OrionOwner.exe` / `orion-admin`.

## 8. SellHub webhook
Dash → Webhooks → `https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/sellhub/webhook`; events **paid/completed + refunded/chargeback**; signing secret → SSM `/orion/sellhub_webhook_secret`; product delivery = the "check your DMs" static message.

## 9. Test
- `/claim_trial` → DMs the Discord connection link + Trial role; re-run → "already claimed"; a 2nd trial on the SAME PC (alt) → "device already used a trial".
- `/hwid_reset` → unbinds; immediate re-run → "try again in 24h".
- `/deliver user:@x plan:month` → DMs a month key + Customer role.
- Buy with a 100%-off coupon → webhook → DM key + Customer role + `#sales-log`.

## Why this is secure (vs. the draft)
Real Ed25519 (no `return true` stub) → no forged interactions. The launcher killswitch is owner-console-only, not a bot command → no Discord-reachable service-disable switch. Bot calls use `X-Orion-Bot-Secret` (not the admin secret). Secrets in Worker-secrets/SSM only. Deferred replies → no 3-second timeout. Trial abuse-locked per-account + per-HWID server-side.
