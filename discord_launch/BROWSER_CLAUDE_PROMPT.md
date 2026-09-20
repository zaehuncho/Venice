# (This file's body is the exact text to paste into browser-Claude.)

> **SUPERSEDED.** This is SellHub-era copy. The store is Gumroad and, since the owner's
> 2026-09-15 rule, there is ONE plan: a free 3-day trial then **$25/month recurring**, plus a
> one-time activation fee. No Day/Week/Lifetime tiers. See `BOT_DEPLOY_GUIDE.md` for the
> two-product Gumroad checkout. Kept for the server/store build steps only.

You are an operator building a complete, professional **Discord server + SellHub store** for a software product called **Orion**. Work top-to-bottom using the Discord web app (discord.com/app) and SellHub (sellhub.cx). Make it clean, branded, secure, and ready to sell. Whenever a step needs the owner (uploading an image, logging into SellHub, paying, providing a price), pause and ask them for it. Confirm the PLACEHOLDERS below before you start.

## ABOUT ORION (use for all copy)
Orion is an AI-vision assistant that reads the on-screen NBA 2K shot meter and times the player's shot release to the green — across standstills, fades, go-to, and tempo shots. Tagline: **"Precision Shot-Timing."** Sold as a license in four durations: Day / Week / Month / Lifetime.

## BRAND
- Name: **Orion** · Tagline: **Precision Shot-Timing**
- Accent color: **#2563EB** (royal blue). **Discord embed color integer = `2450411` — set this on EVERY embed.**
- Vibe: dark, premium, clean tech.

## ASSETS (the owner handles image uploads; you use URLs)
- Server icon + banner: ask the owner to upload `orion_logo.png` (icon) and `orion_banner.png` (banner) in **Server Settings → Overview**. (Banner needs Boost L1+; if unboosted, skip it.)
- For embed images, ask the owner to upload those two PNGs to any channel once and give you:
  - `LOGO_URL` = ____  (use as embed **thumbnail**)
  - `BANNER_URL` = ____ (use as the big embed **image** where noted)
  If unavailable, post embeds without images; the owner adds them later.

## PLACEHOLDERS — confirm with the owner first
`PRICE_DAY`, `PRICE_WEEK`, `PRICE_MONTH`, `PRICE_LIFETIME` (USD) · `STORE_URL` (e.g. https://orion.sellhub.cx) · is the "Orion" server already created or do you create it fresh?

---

## STEP A — Server settings
1. Server name **Orion**; owner uploads icon (`orion_logo.png`) + banner (`orion_banner.png`).
2. **Server Settings → Safety Setup:** Verification Level = **Medium**, enable "Require 2FA for moderation," DM scan = All members.
3. **Default `@everyone` role — turn OFF:** Administrator, Manage Server/Roles/Channels/Webhooks, Kick, Ban, Mention @everyone/@here, Manage Messages, Manage Nicknames, Create Invite. Leave View Channels / Read History / Add Reactions ON (channels narrow it).

## STEP B — Roles (create in THIS order, top = highest; hoist = show separately)
1. 👑 **Owner** — color #2563EB — Administrator — hoist (owner only)
2. 🛡️ **Admin** — #3B82F6 — Manage Server/Roles/Channels, Kick, Ban, Manage Messages — hoist
3. 🔧 **Staff** — #60A5FA — Manage Messages, Kick, Timeout — hoist
4. 🤖 **Bots** — #1E293B — (per-bot; placeholder for bot roles)
5. ⭐ **Lifetime** — #FACC15 — hoist
6. 💎 **Customer** — #22C55E — hoist  (SellHub auto-assigns on purchase)
7. ✅ **Verified** — #94A3B8  (Wick grants after verification)
8. 🔇 **Muted** — #475569 — (deny Send Messages/Reactions/Speak everywhere)
> The SellHub + ticket bot roles must sit **above** Lifetime/Customer/Verified or auto-roles fail.

## STEP C — Categories, channels & permissions
Create these categories + channels. "read-only" = only Staff/bots send. Apply the overwrites noted.

**📋 INFORMATION** (everyone View, read-only): `#welcome` `#announcements` `#rules` `#terms-of-service` `#faq` `#status` `#how-to-buy`
**🛒 STORE** (everyone View): `#pricing` (read-only) · `#purchase` (everyone may use slash/buttons) · `#reviews` (read-only) · `#vouch-format` (read-only)
**🎫 SUPPORT** (everyone View): `#create-ticket` (buttons only) · `#support-info` (read-only)
**💬 COMMUNITY** (✅Verified only — `@everyone` View OFF, Verified View+Send ON): `#general` `#2k-discussion` `#clips` (media-only) `#off-topic` `#bot-commands`
**⭐ CUSTOMER** (💎Customer only — `@everyone` View OFF, Customer View+Send ON): `#customer-lounge` `#downloads` `#setup-guide` `#changelog` `#priority-support`
**🔒 STAFF** (🔧Staff only — `@everyone` View OFF): `#staff-chat` `#mod-log` `#ticket-log` `#sales-log`
Also create `#verify` (visible to unverified only — Wick uses it).

## STEP D — Post the embeds
Use https://discohook.org: set its webhook to the target channel, paste each embed's title/description/fields, **set color to `2450411`**, add thumbnail = `LOGO_URL` and image = `BANNER_URL` where noted, then Send. (Link buttons: use Discohook's components or Carl-bot.) Replace `<#CHANNEL>` mentions with the real channel links after channels exist.

**`#welcome`** (image: BANNER_URL, thumbnail: LOGO_URL)
Title: `Welcome to Orion`
Description: `**Precision Shot-Timing for NBA 2K.**\nOrion is an AI-vision assistant that reads the on-screen shot meter and times your release to the green — across standstills, fades, go-to and tempo shots.\n\n🛒 Pricing → #pricing\n📜 Rules → #rules  ·  Terms → #terms-of-service\n❓ FAQ → #faq  ·  🎫 Support → #create-ticket\n\nComplete verification to unlock the community.`
Footer: `Orion • Precision Shot-Timing`

**`#rules`** — Title: `📋 Server Rules`
Description: `**1. Be respectful.** No harassment, hate, or slurs.\n**2. No spam or self-promo.** No advertising or unsolicited DMs.\n**3. No leaking or reselling.** Sharing/cracking/reselling Orion or keys = instant ban + voided license.\n**4. Use the right channels.** Support in tickets, buys in the store.\n**5. No chargebacks/fraud.** Chargebacks void your license and ban you.\n**6. English in public channels.**\n**7. Follow Discord's ToS & Guidelines.**\n**8. Staff have final say.**`

**`#terms-of-service`** — Title: `© Orion. All Rights Reserved.`
Description: `Orion, hereafter referred to as "The Software", is provided "as is", without warranty or support of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and non-infringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software.`
Field "Usage and Accessibility": `The Software and its related scripts are designed for accessibility, research, and educational objectives. It is intended to assist users — including those with impairments or disabilities — in developing timing, reaction, and game-sense in a fun environment. All interactions should be responsible, ethical, and adhere to relevant laws, regulations, and platform guidelines.`
Field "License": `Purchase grants a single-user, non-transferable license for the duration of the purchased tier. Licenses may not be shared, resold, or redistributed. Keys found shared or sold are revoked without refund. Chargebacks or payment fraud void the license permanently.`
Field "Refunds": `Due to the digital, instantly-delivered nature of The Software, all sales are final. If you cannot get it running, open a ticket — we'll help before any refund is considered.`
Footer: `© Orion`

**`#how-to-buy`** — Title: `🛒 How to Buy Orion`
Description: `**1.** Go to #pricing and pick a tier (Day / Week / Month / Lifetime).\n**2.** Click Purchase (or run /purchase in #purchase).\n**3.** Pay via secure checkout (crypto, card, PayPal & more).\n**4.** Your license key + download arrive instantly, and the 💎 Customer role is added automatically.\n**5.** Open #downloads + #setup-guide and you're ready.\n\nTrouble? Open a ticket in #create-ticket.`
Footer: `Instant delivery • Secure checkout via SellHub`

**`#pricing`** (image: BANNER_URL) — Title: `Orion — Pricing`
Description: `Same full feature set on every tier — pick your duration. Secure instant delivery.`
Inline fields: `🟦 Day` = `PRICE_DAY — 24-hour access` · `🟦 Week` = `PRICE_WEEK — 7 days` · `🟦 Month` = `PRICE_MONTH — 30 days, best value` · `⭐ Lifetime` = `PRICE_LIFETIME — forever + all updates`
Field "Every tier includes": `✓ AI-vision shot-meter timing (standstill • fades • go-to • tempo)\n✓ Auto-calibration • clean overlay • low-latency release\n✓ Regular updates • customer support`
Button: `🛒 Buy Now → STORE_URL`
Footer: `Prices in USD • All sales final (digital goods)`

**`#faq`** — Title: `❓ Frequently Asked Questions` — Fields:
- `What is Orion?` = `An AI-vision assistant that reads the NBA 2K shot meter and times your release to the green.`
- `What do I need?` = `A PC plus your PlayStation set up for Remote Play. Full requirements are in the customer setup guide.`
- `How is it delivered?` = `Instantly after purchase — key + download in your DMs and the purchase channel; the 💎 Customer role is auto-added.`
- `Refund policy?` = `Digital goods = all sales final. Open a ticket first and we'll get you running.`
- `Free trial?` = `The Day tier is the cheapest way to try everything.`

**`#status`** — Title: `🟢 Orion Status` — Inline fields: `Service` = `🟢 Operational` · `2K Build` = `✅ Supported` · `Version` = `(latest)`. Description: `Updates & maintenance are posted in #announcements.`

**`#create-ticket`** — Title: `🎫 Open a Ticket`
Description: `Need help? Pick a category and our team will assist.\n• 🛠 Support — setup, errors, "won't run"\n• 🛒 Purchase Help — payment or delivery\n• ⚠ Report — bugs or rule-breakers\n\nInclude your order ID, OS, and a clear description.` (The ticket bot adds the buttons in STEP E.)

**`#vouch-format`** — Title: `⭐ How to Vouch`
Description: `Loved Orion? Post in #reviews using:\n\`\`\`\n+rep | Tier: (Day/Week/Month/Lifetime) | ⭐⭐⭐⭐⭐ | (your experience)\n\`\`\`\nClips of your greens welcome 🟩`

## STEP E — Bots (set up in this order; keep bot roles above customer roles)
**1) Wick (security)** — wickbot.com → invite (Administrator). Enable: **Verification** (on join → no access until passed → grant `✅ Verified`; prompt in `#verify`, button/captcha mode); **Anti-nuke** (limit mass channel/role deletes + mass ban/kick); **Anti-raid** (auto-quarantine join spikes); **Automod** (block invites, mass-mentions, scam domains, repeated text). Logs → `#mod-log`.
**2) SellHub bot (/purchase)** — from the SellHub store's Discord integration page → invite. Link store↔server; set purchase channel = `#purchase`; post product buy-cards in `#pricing`/`#purchase`; **auto-role: any purchase → 💎 Customer; Lifetime product → also ⭐ Lifetime**; sales webhook → `#sales-log`.
**3) Ticket bot** (Tickets / Ticket Tool / Carl-bot, or use Wick tickets) — invite (Manage Channels + Roles); build a panel in `#create-ticket` with buttons **🛠 Support · 🛒 Purchase Help · ⚠ Report**; tickets are private to the opener + `🔧 Staff`; transcripts → `#ticket-log`.

## STEP F — SellHub store (sellhub.cx)
1. Store named **Orion**, logo = `orion_logo.png`, accent `#2563EB`. Note the public URL as `STORE_URL`.
2. Create 4 products: **Orion — Day / Week / Month / Lifetime** at `PRICE_DAY/WEEK/MONTH/LIFETIME`. Description (swap the duration): `Orion — Precision Shot-Timing for NBA 2K. AI-vision times your release to the green across standstills, fades, go-to and tempo. Includes auto-calibration, a clean overlay, updates and support. License duration: {DURATION}. Single-user, non-transferable. All sales final.`
3. **Delivery:** ask the owner for the license keys (from their Orion key system) and set each product's delivery to **Serials/Keys**, paste the list (keys carry the tier's duration). SellHub hands out one per sale.
4. **Payments:** enable Crypto, Card/Stripe, PayPal, Cash App as available.
5. Make a 100%-off **TEST** coupon and a `LAUNCH` % coupon.

## STEP G — Test & finish
Run a `/purchase` with the TEST coupon end-to-end. Confirm: key delivered (DM/channel) + `💎 Customer` auto-added + entry in `#sales-log`. Then disable the TEST coupon. Report what's done and anything the owner still needs to provide (prices, keys, images, boosts).
