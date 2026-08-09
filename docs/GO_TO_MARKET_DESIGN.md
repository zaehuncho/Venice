# Orion — Go-To-Market Design (grounded, 2026-07-09)

Design only — every item maps to existing code. Rides on infra that already exists (Gumroad → webhook → licensing Lambda → Discord bot); the work is onboarding polish + a few additive bot commands + Gumroad SKU/copy config, NOT a backend rebuild.

## 🐞 REAL BUG found (fix first — direct revenue loss)
**Silent `422 missing discord_user_id` → buyer pays and gets NOTHING → refund/chargeback.** Gumroad checkout has a required custom field literally labeled "Discord ID" (`gumroad_webhook/lambda_function.py:80-88`); if blank/mis-entered, provisioning 422-fails (line 253) and the mint never happens. Also `dm_status:"failed"` is recorded (`:263-269`) but **alerts no one**.
- **Fix:** (a) email-fallback mint keyed to the Gumroad `email` (already captured, `:186`) + a `/redeem <email>` bot command; (b) pipe `dm_status:"failed"` to a staff channel → proactive ping instead of customer dispute; (c) "how to copy your Discord numeric ID" (Developer Mode → Copy User ID) on the product page + `#how-to-buy`.

## 1. Pricing (consolidate + add subscription)
Current ladder (`LAMBDA_RECONCILIATION.md:99-101`, 8 SKUs, hardcoded `PRODUCT_MAP`): Lifetime $99.99 / 120d $49.99 / 30d $24.99 / 14d $14.99 / 7d $9.99 / 3d $5.99 / 1d $2.99 / Beta $0.
- **Retire 1-Day ($2.99) + 3-Day ($5.99)** — refund-magnet tail with the highest cold-start failure rate; a $2.99 buyer can generate a $15 support interaction. Fold their intent into the free trial.
- **Shipped ladder (locked, see `docs/gtm/pricing.md`):** Free Trial **72h** ($0, `handle_bot_trial`) · Weekly $9.99 · **Monthly Subscription $24.99/mo** (recurring Gumroad membership → same `/api/bot/provision`; backend already does `expiry=now+days*86400`, lifetime `expiry=0`) · Lifetime **$199.99** (all products + priority + early access). The standalone one-time Monthly is retired so nothing steps on the sub; the $199.99 anchor makes $24.99/mo the smart everyday pick.
- Trial abuse already double-locked: per-Discord (`TRIAL#discord_id`) + per-machine (`TRIALMACHINE#machine_id`, `handle_activate:272-282`). The trial is the #1 refund-prevention lever (green in trial → no chargeback on paid).

## 2. Onboarding (client-side first-run is the gap, not the backend)
Flow as-built: Gumroad → webhook verify (seller_id + Gumroad licenses/verify) → `/api/bot/provision` mint (`gen_license_key`, KEY_CHARS excludes O/I/0/1) → Discord DM spoiler embed → `#downloads` installer (signed Ed25519 updates, `/api/update`) → launcher `/api/activate` (machine-bind + 30d token) → `/api/license/check` 15-min heartbeat (kill-switch point).
- **A · First-run PREFLIGHT WIZARD** (highest support-cost lever) — validate capture device (enumerate DirectShow/MediaFoundation) + Remote Play window + a **calibration test shot** (show the detected meter box) BEFORE activation is spent. Fail loud+early, not a silent black overlay mid-game. → Day-4 launcher feature.
- **B · Zero-typing activation** — one-click "copy key" + a deep link `orion://activate?key=...` that pre-fills the launcher.
- **C · Machine-move self-service already built** — `/hwid_reset` → `handle_bot_hwid_reset` (24h cooldown, max 3/30d). Surface in `#faq`.

## 3. Support automation (bot is thin; add thin proxies to existing endpoints)
`orion_bot.py` today: only `/claim_trial`, `/hwid_reset`, `/deliver`. Backend already exposes `/api/staff/license` (`handle_staff_license:902-936`, safe status/plan/expiry/activations) that NO bot command calls.
- **`/status`** — customer self license-lookup (proxy `/api/staff/license` or a new `/api/bot/status`). Deflects "did my purchase work / when does it expire."
- **`/setup`** — interactive button troubleshooter for the 2 killer failures: capture-card-not-detected · Remote-Play pairing/lag · no-green/overlay. (Promote the static `EMBEDS.md` FAQ to reachable-at-failure-moment.)
- **`/faq <topic>`** · **diagnostic intake form** on ticket-open (order ID/OS/card/wired-or-wifi/screenshot).
- Net: ~70% ticket deflection with thin proxies → bounds the per-user support cost that justifies the pricing floor.

## 4. Messaging (honest CV differentiator; don't trip processors/platforms)
**Load-bearing truth:** pure CV + Remote-Play controller input — never touches/reads/modifies game memory or the console. "Sees your screen and times a controller press — nothing a human couldn't do." This is the real defense on ToS + payment-processor grounds vs memory-hack tools.
- Lead line already on-message: "AI-vision assistant that reads the shot meter and times your release" (`EMBEDS.md:14-16`). Keep. Accessibility/training framing already drafted (`EMBEDS.md:44-45`) — keep + make true (calibration/learn-your-timing reinforces "assistant").
- **NEVER** say undetectable/cheat/hack/aimbot/auto-win. **Rename** the `#status` "undetected status" line (`SERVER_BLUEPRINT.md:29`) → "Compatibility / 2K build supported" — "undetected" is a red flag to processors.
- Anti-piracy = legitimacy signal: single-user machine-bound license (backend enforces via bind + kill-switch + refund→revoke).

## 5. Top refund/chargeback causes → prevention
1. **Cold-start "doesn't work in 5 min"** (dominant) → preflight wizard (§2A) + `/setup` + trial absorbs the first bad impression at $0.
2. **Wrong/HDCP-blocked capture card** → publish a known-good card list PRE-purchase (Requirements) + `/setup` card flow.
3. **Wi-Fi/Remote-Play latency** → "wired Ethernet strongly recommended" as a PRE-purchase requirement; honest "only as fast as your Remote Play feed."
4. **"Paid, got no key"** → the Discord-ID bug fix (§bug).
5. **Expectation mismatch ("not 100% / got banned")** → honest messaging ("improves consistency" not "guaranteed greens"); CV/no-injection truthfully addresses ban fear; "help before refund" funnel (already drafted `EMBEDS.md:48-49`).
**Backstops already built:** refund Ping → auto-revoke (`gumroad_webhook:220-243`) → `/api/license/check` fails closed within one 15-min heartbeat.

## Priority sequencing
1. First-run **preflight wizard** (biggest cold-start-refund lever) → Day-4 launcher.
2. **`/setup` + `/status`** bot commands (thin proxies) → Chrome-assisted/return bot work.
3. **Fix the Discord-ID delivery fragility** (email fallback + failed-DM alert) → server/return.
4. **Consolidate pricing + add subscription** → Gumroad config (return).
5. **Messaging cleanup** (rename "undetected", lock the CV differentiator).
