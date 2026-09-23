# Announcements — 2026-09-21 (published)

Published by Nereus on 2026-09-22T01:44:55Z after the $19.99 Stripe price and production site
deployment were verified. Announcement message: `1551771256288845887`; pre-publication audit
message: `1551771252690133035`. The live rules message `1549685816404746273` was patched with its
`venice_guard:verify` component preserved, and the live pricing message `1549501172875010240` was
patched as Triton before this announcement was sent.

Copy for Nereus `/announce`. Nereus is the announcements bot: an Admin runs the command in
`#announcements` (`1549859272341332111`), gets an **ephemeral preview**, and only that Admin can
press **Publish**. Nereus writes the audit request first and refuses to post if the audit fails.

Its limits, enforced in `nereus_bot.py::clean_copy`:

- title **3–120** characters, body **10–1800** characters;
- **no links anywhere** in the text (`https://`, `www.`, `discord.gg/` are all rejected) — the
  fixed **Venice website** button under the embed is the only call to action;
- no role or `@everyone` pings.

Both bodies below are inside those limits and contain no links.

---

## ⚠ Publish order — do not announce a price the Worker does not charge

`/announce` is the last step, not the first:

1. **Stripe**: create a **$19.99/month recurring price**. Stripe prices are immutable, so this is a
   NEW price object, not an edit — it produces a new `price_...` id (one for test, one for live).
2. **Repo**: put those ids in `website/wrangler.jsonc` (`vars.STRIPE_PRICE_ID` and
   `env.production.vars.STRIPE_PRICE_ID`) and in the two pins in `website/tests/verify_site.py`.
3. **Deploy the site** (`npm run deploy:production` in `website/`) so the page and the checkout
   agree. The live site today still says **$25/month** and runs the old starfield — it is two
   revisions behind this repo.
4. **Patch the rules message** (`welcome_terms_patch.json`) as **Venice Guard**, with the
   `components` block unchanged, or the Verify button is lost.
5. **Then** publish announcement **A**.

**Existing subscribers are a decision, not a default.** A new Stripe price does not move anyone:
current subscriptions keep billing at the price they were created on. If you want existing members
on $19.99 you have to update each subscription in Stripe. Announcement A deliberately says nothing
about existing subscribers — if you do migrate them, add one line saying so.

---

## A. Price + beta scope  (kind: `Update`)

**title**

```
Venice is now $19.99 a month
```

**body**

```
Venice membership is now $19.99 a month.

What that includes:
• The free 7-day trial — one per Discord account and one per PC, no card needed.
• No activation fee, and you can cancel yourself any time from the billing portal.
• Access is linked to your Discord account, so there is no licence key to lose. It unlocks one PC at a time, and the first 3 HWID resets are free.

Venice is in beta. A PS5 with a capture card is the supported setup; Remote Play-only and Xbox are experimental and may not work on your rig yet.

Two rule updates went up in the rules channel today: access stays personal — that now covers the app itself, so no redistributing the installer or its files — and beta problems belong in a ticket with your log attached, because public channels cannot action a bug report.

Start the trial or subscribe with the website button below.
```

---

## B. Rules only  (kind: `Update`) — optional, if you patch the rules before the price is live

**title**

```
Server rules updated
```

**body**

```
Two additions to the rules today:

• Keep access personal. On top of accounts and one-time codes, this now covers the app itself: do not redistribute the installer or its files.
• Report beta problems in a ticket. Venice is in beta, and bugs, crashes and timing problems need your log attached — public channels cannot action them.

Everything else is unchanged; the rules channel has the full text.
```

---

## What changed in the repo today (for the record)

- `welcome_terms_patch.json` — price `$20 → $19.99` (welcome + terms), beta scope stated in both,
  and the two rule additions above. Same message id, same `components`, still posted as Venice Guard.
- `pricing.json`, `purchase_command.json`, `launch_announcement.json`, `orion_bot.py` — every
  customer-facing `$20` is now `$19.99`. (`pricing.json._meta.replaces_note` still mentions the old
  `$25` Triton embed on purpose: it records what the post replaces.)
- `orion_worker.js` header comment now states the live plan (was a stale `$25`, 3-day trial).
- Website: `$19.99` everywhere including the meta/OG descriptions, a rebuilt Get Venice card, and a
  starfield that blinks and flickers. `python tests/verify_site.py public/index.html` → `VERIFY_OK`.
