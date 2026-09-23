# Venice launch embeds (prepared 2026-09-19 — NOT posted)

Ready-to-post Discord message payloads for the launch. **Nothing here has been posted or edited
into the live server.** The copy matches the paired-account flow (no licence keys) and every
"test mode" line is gone.

Re-mapped 2026-09-19 to the owner's compact layout. He deleted the seven channels the tidy created
("i delete those on purpose trying to keep it compact and tidy without having TOO many channels")
and `#reviews`, and renamed `#customer-lounge` to `#paid-chat`. **Do not create channels for these
posts** — every file below targets a channel that exists today.

Each file is `{"_meta": {...}, "message": {...}}`. Strip `_meta` and send `message` as the JSON body:

- new post: `POST /channels/{channel_id}/messages`
- replacing an existing bot message: `PATCH /channels/{channel_id}/messages/{message_id}`
  — **a message can only be edited by the bot that posted it** (see `post_as`).

## Final mapping (verified against the live guild, 2026-09-19)

| File | Channel | Channel id | Message it replaces | Posted by |
|---|---|---|---|---|
| `pricing.json` | `#pricing` (🛒 STORE) | `1549499753274810388` | `1549501172875010240` ✅ resolves (Triton, says "test mode") | Triton |
| `setup_guide.json` | `#setup-guide` (⭐ CUSTOMER) | `1549499802738499705` | `1549501209650536460` ✅ resolves (Triton, says "key delivered by DM") | Triton |
| `downloads.json` | `#downloads` (⭐ CUSTOMER) | `1549499799861067876` | none — channel is empty | Triton |
| `launch_announcement.json` | `#announcements` (💬 COMMUNITY) | `1549859272341332111` | none — channel is empty | Triton, or Nereus via `/announce` (link-free text in `_meta`) |
| `welcome_terms_patch.json` | `#📋-rules` (📌 START HERE) | `1485337632832753736` | `1549685816404746273` ✅ resolves (Venice Guard) | **Venice Guard only** — keep the components block or the Verify button dies |
| `purchase_command.json` | — ephemeral `/purchase` reply | — | none (ephemeral) | Triton, after editing `orion_bot.py::purchase()` (~line 667) and redeploying |

All three message ids were re-fetched on 2026-09-19 and return 200 with the expected author.

**Merged and deleted:**

- `how_to_buy.json` → folded into `pricing.json` as the **How to buy** field (sign in with Discord →
  stay in the server → trial or subscribe → one-time code at `zaeorion.com/connect` → `/status`).
- `faq.json` → folded into `setup_guide.json`: the three troubleshooting answers (capture card,
  Remote Play, no greens), the HWID facts (3 free `/hwid_reset`, then one day off the subscription)
  and the "open a ticket in #create-ticket, never post a one-time code publicly" rule. The
  long-form answers stay in the `/setup` and `/faq` commands, which are unchanged.

**Dead references removed:** `#reviews` (`1549499759159410711`), `#support-info`, `#hwid-reset`,
`#changelog`, `#priority-support`, `#2k-discussion`, `#off-topic`, `#bot-commands`, `#faq`,
`#how-to-buy` and `#vouch-format` do not exist. No file mentions them; every `<#id>` in every file
was checked against the live channel list and resolves.

Sizes (Discord allows 6000 chars per message and 1024 per field): pricing 1259, setup-guide 2452,
downloads 852, announcement 975, welcome 1961, purchase 891 — all well inside, no field over 1024.

## Posting notes

- The read-only pass is live: in `#pricing`, `#setup-guide`, `#downloads` and `#announcements`,
  Customer/Trial/@everyone can no longer post, while **Triton, Nereus, Staff, Admin and Owner
  keep Send** — so all of these still post normally.
- `downloads.json` still carries `INSTALLER_URL` / `INSTALLER_SHA256` placeholders. Fill them with
  the real URL and hash of the exact installer being published, and delete the `blocked_by` note,
  before posting. The beta installer is unsigned by owner decision (2026-09-23) and may be posted.
- Post the announcement only once live Stripe checkout is verified and the installer is in
  `#downloads`; otherwise it sends buyers at a checkout that provisions nothing.
- Never run `discord_launch/register_commands.py`: it bulk-overwrites the guild command tree and
  would delete `/status`, `/setup` and `/faq`. Triton re-syncs its own tree on restart.

## Copy facts these files are built on

- Free 7-day trial, no card, one per Discord account and one per PC. It starts on the website home
  page (owner decision 2026-09-23), not in Discord.
- Then $25/month. Cancel any time in the billing portal
  `https://billing.stripe.com/p/login/5kQ7sL0Ec4Ya5MfcFsgQE00`; access runs to the end of the period
  already paid for.
- To buy you must be signed in with Discord on `zaeorion.com` **and** be a member of this server.
- No licence keys. Access is linked to the Discord account; the app is unlocked with a one-time code
  from `https://zaeorion.com/connect`, locked to one PC, with 3 free PC resets.
- PS5 through a 60 Hz capture card or PS5 Remote Play: both fully supported (owner decision
  2026-09-23). Xbox ships as EXPERIMENTAL (untested), shown with that label, not hidden.
- The installer is unsigned for the beta: SmartScreen "More info" -> "Run anyway", then check the
  published SHA-256 with `Get-FileHash`.
- Footer on every embed: `Venice • Official`. No stats, reviews, guarantees or "test mode".
