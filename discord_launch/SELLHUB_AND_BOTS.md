# Orion — SellHub Store + Bots

> **SUPERSEDED.** This is SellHub-era copy. The store is Gumroad and, since the owner's
> 2026-09-15 rule, there is ONE plan: a free 3-day trial then **$25/month recurring**, plus a
> one-time activation fee. No Day/Week/Lifetime tiers. See `BOT_DEPLOY_GUIDE.md` for the
> two-product Gumroad checkout. Kept for the server/store build steps only.

Three bots: **SellHub** (store + `/purchase` + auto-roles), **Wick** (security + verification), **a ticket bot** (support). Set them up in that priority. Keep all bot roles **above** `💎 Customer`/`⭐ Lifetime` so auto-role assignment works.

---

## A. SellHub store

### 1. Account + store
1. Create the store at https://sellhub.cx → name it **Orion**, set the logo to `orion_logo.png`, accent to `#2563EB`.
2. Set the public store URL → that's your `{{STORE_URL}}` (e.g. `https://orion.sellhub.cx`).

### 2. Products — create 4 (one per tier)
| Product | Price | Description (short) |
|---|---|---|
| **Orion — Day** | `{{PRICE_DAY}}` | 24-hour license. Full features. |
| **Orion — Week** | `{{PRICE_WEEK}}` | 7-day license. Full features. |
| **Orion — Month** | `{{PRICE_MONTH}}` | 30-day license. Best value. |
| **Orion — Lifetime** | `{{PRICE_LIFETIME}}` | Permanent license + all future updates. |

Long description (reuse on each, swap the duration line):
> **Orion — Precision Shot-Timing for NBA 2K.** AI-vision reads the on-screen shot meter and times your release to the green across standstills, fades, go-to and tempo shots. Includes auto-calibration, a clean overlay, low-latency release, updates and support. **License duration: {DURATION}.** Single-user, non-transferable. All sales final (digital goods).

Set each product's image to `orion_logo.png` (or a per-tier card if you make them).

### 3. Delivery (license keys) — webhook + bot DM (the secure way)
**Do NOT paste a key list and do NOT use SellHub's serial delivery.** Keys are minted server-side by the Lambda on the signed purchase webhook and **DM'd by the Orion bot** (see `SELLHUB_LICENSE_HOOKUP_PROMPT.md`). In SellHub, set each product's delivery to a **static message**:
> ✅ Payment received! Your Orion license key is being delivered to your Discord DMs — make sure you've joined the server and allow DMs from members. No key within 2 minutes? Open a ticket.

No live keys ever sit in SellHub (nothing to leak), each key is bound to the order + Discord buyer, and refunds/chargebacks auto-revoke. For manual/one-off keys (giveaways), the owner uses `POST /api/provision` with the admin secret.
> Point the SellHub webhook at the **direct API Gateway origin** (`https://v348t5hg3i.execute-api.us-east-1.amazonaws.com/api/sellhub/webhook`), NOT `api.zaeorion.com` — Cloudflare Bot Fight Mode would block the webhook POST. The HMAC signature secures it regardless.

### 4. Payments
Enable in SellHub → Payments: **Crypto** (BTC/ETH/LTC/USDT — common for this space), **Card/Stripe**, **PayPal**, **Cash App** as available. Turn on as many as your processors allow.

### 5. Coupons (optional)
Make a `LAUNCH` % coupon for opening week and a staff/test 100%-off coupon for the end-to-end test.

---

## B. SellHub Discord bot (`/purchase`)
1. **Invite** the SellHub bot from your store's Discord-integration page → authorize to the Orion server.
2. **Link** the store to the server in SellHub → Integrations → Discord; set the **purchase channel** = `#purchase`.
3. **Product embeds:** have the bot post each product as a buy-card in `#pricing` or `#purchase` (buttons → checkout). Set the embed color to `#2563EB` if available.
4. **Auto-roles:** map **any purchase → `💎 Customer`**; map **Lifetime product → also `⭐ Lifetime`**. (SellHub → Discord → Roles on purchase.)
5. **Sales webhook:** point purchase notifications to `#sales-log` (a Discord webhook URL from that channel).
6. **Buyer flow:** member runs `/purchase` (or clicks Buy) → checkout → on success the bot **DMs the key + download**, posts confirmation, and **adds the role**. Confirm DMs-from-server are allowed or it posts in-channel.

---

## C. Wick (security)  — https://wickbot.com
1. **Invite** Wick (Administrator, placed just under Owner/Admin).
2. **Verification gate:** Wick → Verification → enable; on join, members get no community access until they pass → Wick grants `✅ Verified`. Put the prompt in a `#verify` channel visible only to unverified members. Mode: button/captcha (raid-resistant).
3. **Anti-nuke / anti-raid:** enable Wick's nuke protection (limits mass channel/role deletes + mass ban/kick — even from a compromised admin) and raid mode (auto-quarantine join-spikes).
4. **Automod / anti-spam:** block invite links, mass-mentions, known scam/phishing domains, repeated/zalgo text; auto-timeout repeat offenders.
5. **Logs:** Wick logs → `#mod-log`.
> Wick can also do tickets — if you'd rather not add a 3rd bot, use Wick tickets and skip section D.

---

## D. Ticket bot (support)  — Tickets / Ticket Tool / Carl-bot, or Wick tickets
1. **Invite** the ticket bot (Manage Channels + Manage Roles).
2. **Panel:** create a panel in `#create-ticket` using the *Support Panel* embed from `EMBEDS.md`, with category buttons: **🛠 Support · 🛒 Purchase Help · ⚠ Report**.
3. **Behavior:** opening a ticket makes a private channel visible to the opener + `🔧 Staff`; **transcripts → `#ticket-log`** on close.
4. **Support role:** `🔧 Staff` is pinged on new tickets.

---

## E. Bot role order + permissions (important)
Top-to-bottom role list:
```
👑 Owner
🛡️ Admin
Wick           (Administrator)
🔧 Staff
SellHub bot    (Manage Roles, Send Messages, Embed Links, Use Slash Cmds)
Ticket bot     (Manage Channels, Manage Roles, Send Messages)
⭐ Lifetime
💎 Customer
✅ Verified
🔇 Muted
@everyone
```
- A bot can only assign roles **below** itself → SellHub/ticket bots must sit **above** `⭐ Lifetime`/`💎 Customer`/`✅ Verified`.
- Give each bot the **minimum** perms listed; don't blanket-Administrator anything except Wick.
- After setup: run the 100%-off coupon through `/purchase` to confirm **key delivery + `💎 Customer` auto-add + `#sales-log` entry**, then disable the coupon.
