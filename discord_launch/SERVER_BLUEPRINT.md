# Orion — Server Blueprint

Clean, secure, store-focused layout. Build roles first (top-down), then channels, then apply permissions.

## Roles (create top → bottom; drag bots above customer roles)
| Order | Role | Color | Hoist? | Key permissions |
|---|---|---|---|---|
| 1 | 👑 **Owner** | `#2563EB` | yes | Administrator (you only) |
| 2 | 🛡️ **Admin** | `#3B82F6` | yes | Manage server/roles/channels, kick/ban, manage messages |
| 3 | 🔧 **Staff** | `#60A5FA` | yes | Manage messages, kick, timeout, view staff + ticket channels |
| 4 | 🤖 **Bots** | `#1E293B` | no | (per-bot; keep ABOVE customer roles so auto-role works) |
| 5 | ⭐ **Lifetime** | `#FACC15` | yes | Customer access + a flex color |
| 6 | 💎 **Customer** | `#22C55E` | yes | Access to the Customer category (auto-assigned by SellHub) |
| 7 | ✅ **Verified** | `#94A3B8` | no | Access to Community (granted by Wick verification) |
| 8 | 🔇 **Muted** | `#475569` | no | Send Messages DENIED everywhere (moderation) |
| — | **@everyone** | default | — | Locked down (see §Permissions) |

> SellHub assigns **Customer** on any purchase; optionally also **Lifetime** specifically on the Lifetime product. Wick assigns **Verified** after the verification gate.

## Channels
Read-only = only staff/bots can send. ✅Verified / 💎Customer mean "that role + above".

### 📋 INFORMATION  *(everyone can view, read-only)*
- `#welcome` — banner + welcome embed
- `#announcements` — releases/news (read-only)
- `#rules` — rules embed
- `#terms-of-service` — ToS embed
- `#faq` — FAQ embed
- `#status` — uptime / 2K version compatibility / undetected status
- `#how-to-buy` — how-to-buy embed → links to store

### 🛒 STORE  *(everyone can view)*
- `#pricing` — ONE pricing embed (free 3-day trial + $25/month) with a Subscribe button → `{{STORE_URL}}`. Owner rule 2026-09-15: no tiers, no Lifetime.
- `#purchase` — the SellHub `/purchase` bot lives here
- `#reviews` — vouches (read-only; customers post via a vouch command or staff repost)
- `#vouch-format` — how to leave a vouch

### 🎫 SUPPORT  *(everyone can view)*
- `#create-ticket` — ticket bot panel (Support / Purchase help / Report)
- `#support-info` — hours, what to include, response-time embed

### 💬 COMMUNITY  *(✅Verified+)*
- `#general`
- `#2k-discussion`
- `#clips` *(media-only)*
- `#off-topic`
- `#bot-commands`

### ⭐ CUSTOMER  *(💎Customer+ only)*
- `#customer-lounge`
- `#downloads` — the actual Orion download/installer + license-key delivery instructions
- `#setup-guide` — install + first-run + settings walkthrough
- `#changelog` — version notes
- `#priority-support` — customer-only help

### 🔒 STAFF  *(🔧Staff+ only)*
- `#staff-chat`
- `#mod-log` (Wick logs)
- `#ticket-log`
- `#sales-log` (SellHub purchase webhook)

## Permissions model
**`@everyone` (server default — lock these OFF):**
`Administrator`, `Manage Server/Roles/Channels/Webhooks`, `Kick/Ban`, `Mention @everyone/@here`, `Manage Messages`, `Create Invite` (staff-only), `Manage Nicknames` — all **denied**. Leave `View Channel`, `Read History`, `Add Reactions` on (per-channel overwrites narrow it).

**Per-category overwrites:**
- INFORMATION / STORE / SUPPORT: `@everyone` → View ✅, Send ❌ (bots/staff send).  `#purchase`/`#create-ticket` → users may use app/slash commands & buttons.
- COMMUNITY: `@everyone` View ❌ · `✅Verified` View ✅ Send ✅.
- CUSTOMER: `@everyone` View ❌ · `💎Customer` View ✅ Send ✅.
- STAFF: `@everyone` View ❌ · `🔧Staff` View ✅ Send ✅.
- `🔇Muted`: Send ❌ + Add Reactions ❌ + Speak ❌ everywhere.

## Security (Wick) — see SELLHUB_AND_BOTS.md §Wick for steps
- **Verification gate:** new members land with no roles → must pass Wick verification → get `✅Verified`. A `#verify` channel (visible to unverified only) holds the prompt.
- **Anti-raid / anti-nuke:** Wick raid mode + nuke protection (limits on mass-ban/kick/channel-delete, even by compromised admins).
- **Anti-spam / automod:** block invite links, mass-mentions, scam/phishing domains, repeated text; auto-timeout offenders; quarantine on raid.
- **Hardening:** enable 2FA-for-moderation (Server Settings → Safety Setup), set verification level to **Medium/High**, disable `@everyone` for non-staff, keep bot roles above customer roles, audit who has Administrator (Owner only).
