# Orion — Embeds

Every embed below. **All use color `2450411` (`#2563EB`).** Thumbnail = `orion_logo.png` unless noted; the big `image` = `orion_banner.png` only where stated.

**How to post:** open https://discohook.org → set the webhook to the target channel → paste the title/description/fields/color → send. (Or use a bot's embed command.) Link-buttons need a bot/webhook that supports components — the SellHub & ticket bots provide their own buttons; for plain link buttons use Discohook's "Buttons" or a bot like Carl-bot.

---

### `#welcome` — Welcome
**Image:** `orion_banner.png`  ·  **Thumbnail:** `orion_logo.png`
**Title:** Welcome to Orion
**Description:**
> **Precision Shot-Timing for NBA 2K.**
> Orion is an AI-vision assistant that reads the on-screen shot meter and times your release to the green — consistently, across standstills, fades, go-to and tempo shots.
>
> 🛒 **Buy / Pricing** → <#PRICING_CHANNEL_ID>
> 📜 **Rules** → <#RULES_CHANNEL_ID>  ·  **Terms** → <#TOS_CHANNEL_ID>
> ❓ **FAQ** → <#FAQ_CHANNEL_ID>  ·  🎫 **Support** → <#CREATE_TICKET_CHANNEL_ID>
>
> React below or complete verification to unlock the community.
**Footer:** Orion • Precision Shot-Timing

---

### `#rules` — Rules
**Title:** 📋 Server Rules
**Description:**
> **1. Be respectful.** No harassment, hate, slurs, or discrimination.
> **2. No spam or self-promo.** No advertising other servers/products or unsolicited DMs.
> **3. No leaking or reselling.** Sharing, cracking, or reselling Orion or your account access = instant ban + voided license.
> **4. Use the right channels.** Keep support in tickets, buys in the store.
> **5. No chargebacks/fraud.** Chargebacks void your license and ban you permanently.
> **6. English in public channels** so staff can moderate.
> **7. Follow Discord's [ToS](https://discord.com/terms) & [Guidelines](https://discord.com/guidelines).**
> **8. Staff have final say.** Loophole-hunting is still a violation.
**Footer:** By participating you agree to these rules.

---

### `#terms-of-service` — Terms of Service  *(matches your reference style)*
**Title:** © Orion. All Rights Reserved.
**Description:**
> Orion, hereafter referred to as "The Software", is provided "as is", without warranty or support of any kind, express or implied, including but not limited to the warranties of merchantability, fitness for a particular purpose and non-infringement. In no event shall the authors or copyright holders be liable for any claim, damages or other liability, whether in an action of contract, tort or otherwise, arising from, out of or in connection with the software or the use or other dealings in the software.
**Field — Usage and Accessibility:**
> The Software and its related scripts are designed for accessibility, research, and educational objectives. It is intended to assist users — including those with impairments or disabilities — in developing timing, reaction, and game-sense in a fun environment. All interactions with The Software should be responsible, ethical, and adhere to relevant laws, regulations, and platform guidelines.
**Field — License:**
> Purchase grants a single-user, non-transferable license for the duration of the paid subscription period. Licenses may not be shared, resold, or redistributed. Access found shared or sold is revoked without refund. Chargebacks or payment fraud void the license permanently.
**Field — Refunds:**
> Due to the digital, instantly-delivered nature of The Software, **all sales are final**. If you cannot get it running, open a ticket — we'll help before any refund is considered.
**Footer:** Last updated: {{DATE}} • © Orion

---

### `#how-to-buy` — How to Buy
**Title:** 🛒 How to Buy Orion
**Description:**
> **1.** Head to **<#PRICING_CHANNEL_ID>** — there is one plan, **$25 / month**.
> **2.** Click **Subscribe** (or run `/purchase` in <#PURCHASE_CHANNEL_ID>) and sign in with Discord on zaeorion.com.
> **3.** Pay by card at the Stripe checkout. Stripe emails your receipt.
> **4.** Access is linked to your **Discord account** instantly — the bot DMs your confirmation and the **💎 Customer** role is added automatically. There is no license key.
> **5.** Open **<#DOWNLOADS_CHANNEL_ID>** + **<#SETUP_GUIDE_CHANNEL_ID>**, then unlock the app with a one-time code from https://zaeorion.com/connect and you're ready.
>
> Trouble? Open a ticket in **<#CREATE_TICKET_CHANNEL_ID>**.
**Footer:** Instant access • Secure checkout via Stripe

---

### `#pricing` — Pricing  *(one embed — owner rule 2026-09-15: ONE plan, no tiers)*
**Image:** `orion_banner.png`
**Title:** Venice — Pricing
**Description:** One plan, every feature. Start free, then keep it for $25 a month.
**Fields:**
- 🎁 **Free trial** | **3 days**, no card. Start it on zaeorion.com or run `/claim_trial`, then unlock the app with a one-time code from https://zaeorion.com/connect. Your Discord ID identifies the account but is not a password. One trial per account and per PC.
- 💳 **Subscription** | **$25 / month**, flat — no activation or setup fee. Cancel any time in the Stripe billing portal; access runs to the end of the period you paid for.
**Field — Included:**
> ✓ AI-vision shot-meter timing (standstill • fades • go-to • tempo)
> ✓ Auto-calibration to your setup • clean on-screen overlay
> ✓ Low-latency release • regular updates • customer support
> ✓ 3 free PC moves — after that each `/hwid_reset` takes 1 day off your subscription
**Buttons:** `🛒 Subscribe → {{STORE_URL}}`
**Footer:** Prices in USD • Recurring monthly • All sales final (digital goods)

> There is **no Lifetime, Day or Week tier** any more. Access already sold keeps working until it
> expires. Do not re-add a tier table here without the owner saying so — the bot's `/purchase` shows
> no prices at all, and this embed is the only place a customer sees one.

---

### `#faq` — FAQ
**Title:** ❓ Frequently Asked Questions
**Fields:**
- **What is Orion?** | An AI-vision assistant that reads the NBA 2K shot meter and times your release to the green.
- **What do I need?** | A PC and your PlayStation set up for Remote Play. Full requirements + setup are in the customer guide.
- **How is it delivered?** | Instantly after purchase — access is linked to your Discord account (no license key); the bot DMs your confirmation and the 💎 Customer role is auto-added. Unlock the app with a one-time code from https://zaeorion.com/connect.
- **Can I switch PCs?** | Venice runs on one PC at a time. You get 3 free resets with `/hwid_reset`; after that each reset takes 1 day off your subscription.
- **Refund policy?** | Digital goods = all sales final. Open a ticket first; we'll get you running.
- **Is there a free trial?** | Yes — **3 days free, no card**. Start it on zaeorion.com or run `/claim_trial`.
**Footer:** More questions? Open a ticket.

---

### `#status` — Status
**Title:** 🟢 Orion Status
**Fields (inline):**
- **Service** | 🟢 Operational
- **2K Build** | ✅ Supported
- **Latest Version** | `{{VERSION}}`
**Description:** Updates and any maintenance windows are posted in <#ANNOUNCEMENTS_CHANNEL_ID>. Last checked: {{DATE}}.
**Footer:** Orion • status

---

### `#create-ticket` — Support Panel  *(the ticket bot renders the buttons; this is the intro embed)*
**Title:** 🎫 Open a Ticket
**Description:**
> Need help? Pick a category below and our team will assist.
> • **🛠 Support** — setup, errors, "won't run"
> • **🛒 Purchase Help** — payment or delivery issues
> • **⚠ Report** — bugs or rule-breakers
>
> Please include your **order ID**, **OS**, and a clear description.
**Footer:** Average response: within a few hours.

---

### `#vouch-format` — Vouch Format
**Title:** ⭐ How to Vouch
**Description:**
> Loved Orion? Drop a vouch in <#REVIEWS_CHANNEL_ID> using:
> ```
> +rep | Plan: (Trial/Monthly) | ⭐⭐⭐⭐⭐ | (your experience)
> ```
> Screenshots/clips of your greens are very welcome 🟩
**Footer:** Genuine vouches only — fakes get removed.

---

### `#announcements` — Announcement TEMPLATE  *(reuse per release)*
**Image:** `orion_banner.png`
**Title:** 🚀 Orion {{VERSION}} — {{HEADLINE}}
**Description:**
> {{WHATS_NEW}}
> **Customers:** grab the update in <#DOWNLOADS_CHANNEL_ID>. Changelog → <#CHANGELOG_CHANNEL_ID>.
**Footer:** Orion • {{DATE}}

---

## Example Discohook JSON (Welcome) — paste into discohook.org's JSON editor
```json
{
  "embeds": [{
    "title": "Welcome to Orion",
    "description": "**Precision Shot-Timing for NBA 2K.**\nOrion is an AI-vision assistant that reads the on-screen shot meter and times your release to the green — across standstills, fades, go-to and tempo shots.\n\n🛒 **Pricing** · 📜 **Rules** · ❓ **FAQ** · 🎫 **Support**",
    "color": 2450411,
    "image": { "url": "attachment://orion_banner.png" },
    "thumbnail": { "url": "attachment://orion_logo.png" },
    "footer": { "text": "Orion • Precision Shot-Timing" }
  }]
}
```
> Replace `attachment://…` with the image URLs after you upload the assets to a channel (right-click the uploaded image → Copy Link), or upload them directly in Discohook.
