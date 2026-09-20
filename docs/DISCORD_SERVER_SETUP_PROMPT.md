# Prompt for Astra — set up the Venice Discord server (revised 2026-09-15)

You are setting up the official Discord server for **Venice**, an NBA 2K27 shot-timing tool sold
through Gumroad. Pricing model: a free **3-day trial**, then **$25/month** (recurring subscription)
plus a small **one-time activation fee** on the first purchase. There is **no lifetime plan**. Build
it clean, secure and store-focused, in this order: roles → categories and channels → permissions →
bots and command permissions → embeds → security gate. Hand back the IDs listed at the end.

## 1. Roles (create top to bottom; bot roles above customer roles)

| Order | Role | Colour | Hoist | Permissions |
|---|---|---|---|---|
| 1 | 👑 Owner | #2563EB | yes | Administrator (the owner only) |
| 2 | 🛡️ Admin | #3B82F6 | yes | Manage Server / Roles / Channels, Kick, Ban, Manage Messages |
| 3 | 🔧 Staff | #60A5FA | yes | Manage Messages, Kick, Timeout, view staff + ticket channels |
| 4 | 🤖 Bots | #1E293B | no | per bot; must sit ABOVE Customer and Trial so auto-roles work |
| 5 | 💎 Customer | #22C55E | yes | access to the Customer category (active subscription) |
| 6 | 🧪 Trial | #A78BFA | no | access to the Customer category for the 3-day trial |
| 7 | ✅ Verified | #94A3B8 | no | access to Community (granted by the verification gate) |
| 8 | 🔇 Muted | #475569 | no | Send Messages, Add Reactions, Speak denied everywhere |
| — | @everyone | default | — | locked down (see §3) |

The Venice bot assigns Customer and Trial automatically; Verified comes from the verification bot.
Only the owner has Administrator. No bot has Administrator.

## 2. Categories and channels

Read-only = only staff and bots post. "Verified+" = that role and every role above it.

**📋 INFORMATION** (everyone can view, read-only)
`#verify` (verification bot panel; visible to unverified members only), `#welcome` (ONE compact
message: welcome + rules + terms + status, copy in §5), `#announcements`, `#faq`, `#how-to-buy`.

**🛒 STORE** (everyone can view)
`#pricing` (one embed: $25/month + one-time activation fee, one buy button to the Gumroad store),
`#purchase` (the Venice bot's `/purchase` and `/claim_trial` live here; members may use slash
commands and buttons only), `#reviews` (read-only vouches), `#vouch-format`.

**🎫 SUPPORT** (everyone can view)
`#create-ticket` (ticket bot panel: Support / Purchase help / Report), `#support-info` (the
Venice Support embed in §5), `#hwid-reset` (the PC / HWID Resets embed in §5).

**💬 COMMUNITY** (Verified+)
`#general`, `#2k-discussion`, `#clips` (media only), `#off-topic`, `#bot-commands`.

**⭐ CUSTOMER** (Customer and Trial only)
`#customer-lounge`, `#downloads` (installer link + how the key arrives by DM), `#setup-guide`
(install, first run, capture card or Remote Play, Shot Lead tuning against the game's TIMING
banner), `#changelog`, `#priority-support`.

**🔒 STAFF** (Staff+ only)
`#staff-chat` (the Venice bot posts admin alerts here; keep this exact name), `#mod-log`,
`#ticket-log`, `#sales-log` (Gumroad purchase webhook), `#audit-log` (bot staff actions).

## 3. Permissions model

- `@everyone`: deny Administrator, Manage Server/Roles/Channels/Webhooks, Kick, Ban, Mention
  @everyone/@here, Manage Messages, Create Invite (staff only), Manage Nicknames. Keep View Channel,
  Read History, Add Reactions on; narrow per category below.
- INFORMATION / STORE / SUPPORT: `@everyone` View ✅ Send ❌. In `#purchase` and `#create-ticket`
  members may use application commands and buttons.
- COMMUNITY: `@everyone` View ❌; Verified View ✅ Send ✅.
- CUSTOMER: `@everyone` View ❌; Customer and Trial View ✅ Send ✅.
- STAFF: `@everyone` View ❌; Staff View ✅ Send ✅.
- Muted: Send ❌, Add Reactions ❌, Speak ❌ in every category.

## 4. Bots and command permissions

- **Venice bot** (the license bot). Invite with Manage Roles, Manage Channels, Send Messages, Embed
  Links, Use Application Commands; give it the Bots role placed above Customer and Trial. Commands:
  `/purchase` (replies with ONE button straight to the Gumroad store page, nothing else),
  `/claim_trial` (3-day trial, one per Discord account and one per PC, key by DM), `/hwid_reset`,
  `/status` (own license), `/lookup` (own license; staff can look up others), `/setup`, `/faq`, and
  the staff-only `/deliver` (mint a license for a user, audited) and `/keygen` (admin+, mint unassigned
  keys, audited). `/deliver` and `/keygen` ship hidden: Server Settings → Integrations → the Venice
  bot → Command Permissions → allow `/deliver` for Staff and Admin, `/keygen` for Admin only, and
  restrict both to `#staff-chat`. Keys never appear in a public channel; every reply carrying a key is
  ephemeral or a DM.
- **Ticket bot** (Ticket Tool or equivalent): panel in `#create-ticket`, transcripts to
  `#ticket-log`, Staff as the support role.
- **Verification / security bot** (Wick or equivalent): verification gate granting Verified,
  anti-raid, anti-nuke, anti-spam, invite and phishing link blocking, auto-timeout, logs to `#mod-log`.
- **Gumroad webhook** posts sales to `#sales-log` (staff only).

## 5. Embed copy (post as the bot or via Discohook; footer on each: "Venice • Official")

**`#welcome` — one compact message**

> **Welcome to Venice**
> Venice is an NBA 2K27 shot-timing tool built for clear setup and consistent timing.
> 1. Verify in #verify.
> 2. Buy in #pricing or run `/claim_trial` in #purchase.
> 3. Check DMs for your key.
> 4. Download from #downloads.
>
> **Rules** — One account per person. No key sharing or reselling. No scam or invite links. Respect
> staff and members. Keep clips and discussion in their channels.
>
> **Terms** — Venice is a digital subscription: $25 per month plus a one-time activation fee, cancel
> any time. A key is bound to one PC. Every key gets 3 free PC resets; after that each reset deducts
> one day from your subscription. No refunds once a key has been activated. Chargebacks end the
> subscription and the account.
>
> **Status** — Supported: NBA 2K27, PS5 via capture card (recommended, 60 Hz) or Remote Play.
> Current build and service status are posted in #announcements.

**`#pricing`**

> **Venice — $25 / month**
> Recurring subscription, cancel any time. A small one-time activation fee applies to your first
> purchase. Free 3-day trial with `/claim_trial` in #purchase (one per person, one per PC).
> [Buy on Gumroad]

**`#support-info` — Venice Support**

> Open a private ticket in #create-ticket. Include your platform, setup method, Venice version,
> NBA 2K27 version, and the exact error or behavior. Never post a licence key publicly. Staff reply
> as availability permits; purchase-delivery and access issues receive priority.

**`#hwid-reset` — PC / HWID Resets**

> Every key gets 3 free PC resets through `/hwid_reset`. After that, each reset deducts one day from
> your subscription. Staff-initiated resets do not consume the customer's count.

Also: `#how-to-buy` (three lines: buy on Gumroad → key by DM within a minute → `/status` to check),
`#faq` (works on PS5 Remote Play and capture cards; capture card at 60 Hz recommended; if the key DM
does not arrive run `/status`, then open a ticket), `#vouch-format`, an announcement template.

## 6. Security hardening

Server Settings → Safety Setup: require 2FA for moderation, verification level High, explicit
content filter on for everyone, disable `@everyone` for non-staff. New members land with no roles and
see only `#verify` (visible to unverified only) plus INFORMATION until they pass verification. Audit
who has Administrator: the owner only.

## 7. Hand back

- Guild ID
- Role IDs: Customer, Trial, Staff, Admin, Verified, Muted
- The exact names of `#staff-chat`, `#sales-log`, `#audit-log`, `#mod-log`, `#ticket-log`
- Confirmation that the Venice bot's role sits above Customer and Trial
- Confirmation of the `/deliver` and `/keygen` command permissions
- The Gumroad store link used by `#pricing` and `/purchase`
