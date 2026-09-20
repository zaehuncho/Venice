# Orion — Discord + SellHub Launch Package

Everything needed to stand up a clean, professional **Orion** Discord server + **SellHub** store, ready for browser-Claude (or you) to build out by clicking through the Discord/SellHub UIs.

## Files
| File | What it is |
|---|---|
| `assets/orion_logo.png` | Server icon (1024×1024, royal-blue "O" + Orion's-belt reticles). Discord crops to a circle. |
| `assets/orion_banner.png` | Server banner / invite splash (1920×1080). |
| `make_assets.py` | Regenerates both assets — edit colors/text/tagline here. |
| `SERVER_BLUEPRINT.md` | Roles, categories, channels, permissions, security (Wick) — the structure to build. |
| `EMBEDS.md` | Every embed's exact content (welcome, rules, ToS, pricing, FAQ, …), brand-colored, copy-paste ready. |
| `SELLHUB_AND_BOTS.md` | SellHub store + products, the `/purchase` bot, Wick security, the ticket bot. |

## Deploy checklist — Admin Panel V2 (do these in order)
The license/admin surface (`orion_worker.js`, `orion_bot.py`, `gumroad_webhook/`) implements
`docs/ADMIN_PANEL_V2_CONTRACT.md`. Full instructions: `CLOUDFLARE_WORKER_GUIDE.md` (Worker),
`BOT_DEPLOY_GUIDE.md` (gateway bot). The short version:

1. **Confirm the Worker name from Discord's Interactions Endpoint URL before you deploy.**
   `wrangler.toml`'s `name = "orion-license-bot"` does **not** exist on the account; the real
   Workers are `orion-discord-bot` and `license-redeem-proxy`, and the docs disagree about which
   one Discord calls. Read Dev Portal → General Information → **Interactions Endpoint URL**, match
   it with `wrangler deployments list` / the dashboard, set `name` to that Worker, *then* deploy.
   Deploying as-is silently publishes a third Worker and the commands keep running the old code.
2. Backend first — the `/api/bot/*` contract changes are BREAKING (contract §8 rollout).
3. Worker secrets: `wrangler secret put DISCORD_PUBLIC_KEY | DISCORD_APP_ID | ORION_BOT_SECRET |
   ORION_EDGE_AUTH`. Worker `[vars]`: `ORION_API_BASE`, **`STORE_URL`** (the website `/purchase`
   links to), `STAFF_ROLE_IDS`. `GUMROAD_BASE` is now only a fallback for `STORE_URL`;
   `GUMROAD_HWID_RESET_SLUG` is **removed** — customers cannot buy a reset any more.
4. Gateway bot env (if you run it instead of the Worker): `DISCORD_BOT_TOKEN`, `ORION_BOT_SECRET`,
   `ORION_EDGE_AUTH`, `ORION_API_BASE`, `ORION_GUILD_ID`, role ids, plus `STAFF_ROLE_IDS`,
   `ORION_STAFF_ID`, `ORION_STAFF_MACHINE_ID`. `HWID_RESET_BUY_URL` is **removed**;
   `LIFETIME_ROLE_ID` is legacy (staff comps only).
5. Gumroad webhook Lambda env: `GUMROAD_ACTIVATION_PRODUCT` (default `orion-activation`),
   `OWNER_DISCORD_USER_ID` (or SSM `/orion/owner_discord_user_id`), optional `ORION_API_BASE`.
   `GUMROAD_HWID_RESET_PRODUCT` is legacy — keep it set only while an in-flight reset sale
   could still land, then set it to `""`.
6. `python register_commands.py`, then Server Settings → Integrations → Command Permissions →
   give the Staff role access to `/deliver` and `/keygen` (they are hidden by default).

## Brand facts (use everywhere)
- **Name:** Orion **Tagline:** *Precision Shot-Timing*
- **Accent color:** `#2563EB` (royal blue) → Discord embed color int **`2450411`**
- **Background tone:** near-black `#0A0D12`
- **Icon:** `assets/orion_logo.png` (use as server icon + embed thumbnails)
- **Banner:** `assets/orion_banner.png` (server banner, invite splash, top of #welcome)
- **What Orion is (1-liner):** an AI-vision assistant that reads the on-screen shot meter and times your release to the green — for NBA 2K.

## Pricing (owner rule 2026-09-15)
One product line, and the price is written down in exactly ONE place — the website.

| | |
|---|---|
| Free trial | 3 days, `/claim_trial`, one per Discord account **and** per PC |
| Subscription | **$25 / month, recurring** (Gumroad *membership* `orion-monthly`) |
| Activation fee | one-time, cheap (Gumroad one-off `orion-activation`) — mints **no key** |
| Lifetime / Day / Week | **not sold.** Still accepted on existing keys and for staff comps. |

The bot never prints a price: `/purchase` is one embed and one button to `STORE_URL`. HWID resets
are **3 free per key, then 1 day off the subscription** — there is no reset to buy.

## You must fill these placeholders (search for `{{ }}`)
- `{{STORE_URL}}` — your website / checkout link (also set as the Worker's `STORE_URL` var)
- `{{SUPPORT_EMAIL}}` — support contact (optional)
- `{{INVITE_URL}}` — the server's permanent invite (after creation)

## Integration order (hand this to browser-Claude)
1. **Server basics** — name "Orion", upload `orion_logo.png` (icon) + `orion_banner.png` (banner). Set base `@everyone` perms locked down (see blueprint §Permissions).
2. **Roles** — create in the exact order/colors in `SERVER_BLUEPRINT.md` §Roles (hierarchy matters; bots must sit ABOVE customer roles).
3. **Channels** — create the categories + channels from §Channels, applying the per-channel permission overwrites.
4. **Embeds** — post each embed from `EMBEDS.md` into its channel (use https://discohook.org or a bot embed command; color `2450411`).
5. **Bots** — invite + configure Wick, the SellHub bot, and the ticket bot per `SELLHUB_AND_BOTS.md` (verification gate, automod, ticket panel).
6. **SellHub** — create the 4 products (Day/Week/Month/Lifetime), set delivery + payments, connect the bot, set auto-role → `Customer`, sales webhook → `#sales-log`.
7. **Test** — run a `/purchase` end-to-end with a test/coupon, confirm key delivery + the `Customer` role auto-assigns + the sale logs.

> Note: nothing here connects to the Orion codebase or signs anything — it's a self-contained branding/ops package. The license keys SellHub delivers are generated/managed in your existing Orion key system; SellHub just hands them out on purchase (see `SELLHUB_AND_BOTS.md` §Delivery).
