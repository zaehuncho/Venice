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

## Brand facts (use everywhere)
- **Name:** Orion **Tagline:** *Precision Shot-Timing*
- **Accent color:** `#2563EB` (royal blue) → Discord embed color int **`2450411`**
- **Background tone:** near-black `#0A0D12`
- **Icon:** `assets/orion_logo.png` (use as server icon + embed thumbnails)
- **Banner:** `assets/orion_banner.png` (server banner, invite splash, top of #welcome)
- **What Orion is (1-liner):** an AI-vision assistant that reads the on-screen shot meter and times your release to the green — for NBA 2K.

## You must fill these placeholders (search for `{{ }}`)
- `{{STORE_URL}}` — your SellHub store link (e.g. `https://orion.sellhub.cx`)
- `{{PRICE_DAY}}` `{{PRICE_WEEK}}` `{{PRICE_MONTH}}` `{{PRICE_LIFETIME}}` — set the real prices in SellHub; mirror them in the pricing embed
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
