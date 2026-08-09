# CURRENT canonical browser-Claude prompt — Orion store GO-LIVE (2026-07-03, rev 2)
# Architecture: Cloudflare WORKER `license-redeem-proxy` (orion_worker.js, HTTP interactions) is the
# DEPLOYED Discord command path. The gateway bot orion_bot.py is the ABANDONED fallback — ignore it.
# Supersedes rev 1 (which wrongly assumed the gateway bot) and the June-28 SELLHUB_LICENSE_HOOKUP_PROMPT.md.
# Body below = paste into browser-Claude.

You are taking the Orion store LIVE: a paid SellHub order -> the AWS Lambda mints a license key ->
the Cloudflare Worker + Discord bot DM the key to the buyer and grant the 💎 Customer role; a refund
revokes it. You have the owner's AWS, Cloudflare, Discord (Developer Portal + the Orion server, owner),
and SellHub dashboards. You have NO file/terminal access — every value you need is below.

KNOWN FACTS (use verbatim):
- Discord Application ID: 1514712514695270477
- Discord Guild (server) ID: 1483836452776316970
- Discord app PUBLIC KEY (hex): 27df132ae94ed499042cac43ac916cf6d17e1c65d984ddb3e093397ea3dfbd8a
- Direct API origin (M2M — bypasses Cloudflare bot-fight): https://v348t5hg3i.execute-api.us-east-1.amazonaws.com
- The Discord command layer is the Cloudflare Worker named `license-redeem-proxy` (orion_worker.js).
- Slash commands: /claim_trial, /hwid_reset, /deliver (admin). The launcher killswitch is
  NOT a Discord command — it's owner-console-only (OrionOwner.exe / orion-admin CLI).

RULES OF ENGAGEMENT: additive only. NEVER rotate/delete an existing secret, modify Lambda CODE, flip
the killswitch to ON, or make a REAL paid purchase. Creating SSM params, setting Worker vars, ordering
Discord roles, adding IAM ARNs, configuring SellHub, and a 100%-OFF TEST coupon are allowed. Anything
that needs Lambda source changes or is ambiguous -> FLAG for the owner, don't guess.

── STEP 1 · Discord server prep ───────────────────────────────────────────────
- Server Settings -> Roles: ensure a 💎 Customer role and a ⭐ Lifetime role EXIST (create if missing;
  if only a "Paid" role exists, either rename it to 💎 Customer or make a new one). Drag the bot's own
  role (the Orion app's managed role) ABOVE both — a bot cannot grant a role above itself.
- Developer Mode ON (Settings -> Advanced). Copy CUSTOMER_ROLE_ID (right-click 💎 Customer) and
  LIFETIME_ROLE_ID (⭐ Lifetime). GUILD_ID is 1483836452776316970 (confirm it matches Copy Server ID).

── STEP 2 · AWS SSM (Systems Manager -> Parameter Store) ──────────────────────
- /orion/bot_service_secret : if it does NOT exist, CREATE it (SecureString) with a fresh random
  32+ char value. RECORD this value — the Worker's ORION_BOT_SECRET (Step 4) must be set to the SAME
  value. If it already exists, read it (decrypted) and reuse it in Step 4.
- /orion/discord_guild_id : confirm = 1483836452776316970.
- /orion/discord_customer_role_id : set to the REAL CUSTOMER_ROLE_ID from Step 1 (a prior session left
  a placeholder = the old Paid role — overwrite it with the real Customer role id).
- /orion/discord_lifetime_role_id : set to the REAL LIFETIME_ROLE_ID from Step 1 (prior placeholder = 0).

── STEP 3 · AWS IAM (webhook role can read the new params) ─────────────────────
- Role `orion-sellhub-webhook-role`, inline policy `orion-webhook-least-privilege`: add these 3 ARNs to
  the ssm:GetParameter statement (it currently lists only sellhub secrets + bot token + admin_secret):
    arn:aws:ssm:us-east-1:987622176566:parameter/orion/discord_guild_id
    arn:aws:ssm:us-east-1:987622176566:parameter/orion/discord_customer_role_id
    arn:aws:ssm:us-east-1:987622176566:parameter/orion/discord_lifetime_role_id

── STEP 4 · Cloudflare Worker `license-redeem-proxy` config ───────────────────
Workers & Pages -> license-redeem-proxy -> Settings -> Variables (+ Triggers for the URL). Set:
- Secret DISCORD_PUBLIC_KEY = 27df132ae94ed499042cac43ac916cf6d17e1c65d984ddb3e093397ea3dfbd8a
    (the DISCORD APP public key above — NOT any SSM value; the SSM /orion/ed25519_public_key is a
     DIFFERENT key for a different purpose and must NOT be used here.)
- Secret DISCORD_APP_ID  = 1514712514695270477
- Secret ORION_BOT_SECRET = the exact value of SSM /orion/bot_service_secret from Step 2.
- Var    ORION_API_BASE   = https://v348t5hg3i.execute-api.us-east-1.amazonaws.com
Then record the Worker's public URL (Triggers tab — a *.workers.dev URL or a custom route).
⚠️ The Cloudflare Workers dashboard has failed to load in prior sessions. If it still won't load:
try a different browser/network first; if truly blocked, DO NOT proceed blindly — FLAG the exact
values above plus the wrangler commands the owner must run:
    wrangler secret put DISCORD_PUBLIC_KEY   (paste 27df13…)
    wrangler secret put DISCORD_APP_ID       (paste 1514712514695270477)
    wrangler secret put ORION_BOT_SECRET     (paste the /orion/bot_service_secret value)
    # ORION_API_BASE goes in wrangler.toml [vars]; owner redeploys with `wrangler deploy`
  and STOP at Step 5 (it depends on the Worker URL).

── STEP 5 · Discord Interactions Endpoint URL ─────────────────────────────────
Dev Portal (app 1514712514695270477) -> General Information -> Interactions Endpoint URL = the Worker
URL from Step 4 -> Save. Discord sends a validation PING; it must save with a GREEN check. A red/failed
save = wrong DISCORD_PUBLIC_KEY in the Worker (Step 4) or the Worker isn't deployed. (This step is
correct for the WORKER model — do NOT leave it blank.)

── STEP 6 · Register the slash commands ───────────────────────────────────────
The commands must be registered as GUILD commands or they won't appear. You cannot run a terminal, so:
- If the deployed Lambda exposes POST /api/discord/register-commands, the OWNER runs (once, privately):
    curl -X POST "https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/discord/register-commands" \
         -H "X-Orion-Admin-Secret: <the /orion/admin_secret value>"
  Expect {"ok": true, ...}. FLAG this for the owner with the command filled in.
- If that route 404s, FLAG that commands must be registered via the Discord API
  (PUT /applications/1514712514695270477/guilds/1483836452776316970/commands) — owner action.

── STEP 7 · SellHub ───────────────────────────────────────────────────────────
- Webhooks: set BOTH the order:completed AND order:refunded (or dispute) endpoints to the DIRECT origin
    https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/sellhub/webhook
  ⚠️ NOT api.zaeorion.com — a server-to-server POST through Cloudflare is bot-filtered/edge-auth-gated
  and will 403. A prior session wrongly set these to the Cloudflare URL — CORRECT them.
- Confirm the signing secrets still match SSM (should already): completed = /orion/sellhub_webhook_secret,
  refunded = /orion/sellhub_webhook_secret_refunded.
- DISCORD OAUTH AT CHECKOUT (BLOCKER): connect a Discord account/integration to the SellHub store so each
  order carries the buyer's Discord user ID. Without it the webhook has no one to DM -> no delivery.
  Confirm a test order's payload actually contains the discord id.
- STOCK: the products show 0 stock. Orion MINTS keys server-side (SellHub stock is not the delivery
  mechanism), so 0 stock is only a problem IF SellHub BLOCKS the purchase at checkout on 0 stock. Verify
  whether a 100%-off test order can COMPLETE with 0 stock; if it blocks, set the products to unlimited/
  large stock (they're mint-on-webhook, so the number is cosmetic) and report.
- Note the exact SIGNATURE HEADER name SellHub sends and the payload field names for order id / product /
  discord id / status from a real test delivery -> report them (the Lambda must match).

── STEP 8 · End-to-end TEST (no real money) ───────────────────────────────────
- 100%-OFF test coupon. From a TEST Discord account in the server with "DMs from server members" on,
  first try /claim_trial in the server (fastest bot-path check), then buy "Orion — 1-Day" via SellHub.
- PASS = the bot DMs the "🔑 Your Orion License" embed AND 💎 Customer role added AND CloudWatch
  (orion-sellhub-webhook) logs `AUDIT sellhub_issue_ok`.
- If a command errors: check the Worker's live logs (Cloudflare -> the Worker -> Logs) AND CloudWatch.
  A 404 from the Lambda = the /api/bot/* routes are NOT deployed -> FLAG (owner/code fix). 401 at the
  Worker = DISCORD_PUBLIC_KEY wrong. `sellhub_sig_fail` = secret/header mismatch. `ignored event=…` =
  paid-event wording unrecognized (report the exact event/status). Then DISABLE the test coupon.
- Refund the test order -> CloudWatch `AUDIT sellhub_revoke_ok`, role removed, key now "revoked".

── DELIVER ────────────────────────────────────────────────────────────────────
Report: (A) roles created + bot-role position, (B) SSM params found/created/updated, (C) IAM ARNs added,
(D) Worker vars set (or the wrangler hand-off if the dashboard was down) + the Worker URL, (E) Interactions
URL save result, (F) command-registration status, (G) SellHub webhook URLs + Discord-OAuth + stock +
the observed header/field names, (H) end-to-end + refund test results with CloudWatch lines, and a
FLAGGED list of anything needing the owner's source/terminal (esp. any /api/bot/* 404 = the deploy gap).
