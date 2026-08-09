# Orion — Pricing

> Customer-facing copy for the pricing ladder. Use in `#pricing`, the Gumroad product pages, and the store site. Replaces the old 8-SKU ladder — the **1-Day ($2.99)**, **3-Day ($5.99)**, and standalone one-time Monthly tiers are retired; short-term intent is served by the **Free Trial** and **Weekly**, and the monthly option is the subscription.

---

## The ladder

| Tier | Price | Duration | Effective cost | Positioning |
|---|---|---|---|---|
| 🆓 **Free Trial** | $0 | 72 hours (3 days) | free | Try everything before you pay |
| 🟦 **Weekly Pass** | $9.99 | 7 days | ~$1.43/day | Cheap, no-commitment on-ramp |
| 💠 **Monthly Subscription** ⭐ | $24.99/mo | Recurring | ~$0.83/day | The regular's tier — best value, cancel anytime |
| 👑 **Lifetime All-Access** | $199.99 | Forever | $0 after ~8 months | Every product, forever, priority — pay once |

**Every tier includes the full feature set.** You're choosing a duration, not a feature list — *except* Lifetime, which adds all-product access + priority (below).

> **Weekly vs. Monthly, in one line:** four Weekly Passes cost ~$40; a month of the Subscription is $24.99. Past your second week, the sub is the cheaper way to keep playing — and it never lapses on you mid-grind.

---

## Tier copy (per-tier positioning)

### 🆓 Free Trial — $0 · 72 hours
> **Try Orion free for 3 full days — full features, no card.**
> Run `/claim_trial` in the Discord and get a 72-hour license instantly. That's enough time to set up your capture card, pair Remote Play, calibrate, and actually watch your greens land — then show your friends. One trial per Discord account and per PC.

*Positioning: the front door AND the growth engine. 3 days (not 24h) because setup takes time — a user who only gets an hour of play before the clock runs out never sees the value. A generous trial drives word-of-mouth; abuse is double-locked (per-Discord + per-machine).*

### 🟦 Weekly Pass — $9.99 · 7 days
> **A full week of green windows — no subscription.**
> Perfect for a Rec weekend, a Park grind, a tournament, or stretching a trial you loved into real sessions. Full feature set, instant delivery, nothing to cancel.

*Positioning: the low-commitment paid entry and the pressure valve for anyone who balks at the sub. A week is long enough to get real value (unlike the retired 1-day), so refund risk is low.*

### 💠 Monthly Subscription — $24.99/mo · recurring ⭐ BEST VALUE
> **The regular's price: $24.99/mo, cancel anytime.**
> Thirty days of access that renews automatically so your license never lapses mid-grind — about $0.83 a day to green like a pro. Cancel from your Gumroad receipt in two clicks; access runs to the end of the paid period.

*Positioning: the default recommendation and the recurring-revenue engine. The "cancel anytime, two clicks" line is load-bearing — say it everywhere the sub is mentioned. At ~$25/mo it's clearly the value pick over $9.99/week (~$40 of weeklies), which pulls regulars into recurring.*

### 👑 Lifetime All-Access — $199.99 · forever
> **Pay once. Every product, forever, first in line.**
> A lifetime license to **every Orion product** — the capture-card reader, the no-capture-card (Remote Play stream) mode, and every future mode we ship — plus **priority support** (front of the queue), **all future updates**, and **early/beta access** to new features. Pay once, never think about your license again.
>
> **What the $199.99 actually buys.** Orion isn't a macro or a script — it's a real computer-vision pipeline that reads the shot meter off a live video feed and fires a controller press inside the green window, frame by frame. That means a meter reader tuned per 2K build, a calibration loop that learns *your* jumper, a low-latency virtual-controller release, and a forked Remote Play stack — and every one of those gets re-tuned each time 2K changes the meter or the game. Lifetime means you fund that roadmap once and ride every future build, every new mode, and every re-tune for good.

*Positioning: the anchor + the whale tier. The high price makes the $24.99 sub look like the smart everyday choice, and "every product + priority + early access + the whole future roadmap" makes this a genuine premium bundle, not just a long-duration pass. Break-even vs the sub is ~8 months — but the pitch is "in once, done, first in line, every future build included."*

---

## "Every tier includes" block (reuse verbatim)

> ✓ AI-vision shot-meter timing (standstill • fades • go-to • tempo)
> ✓ Auto-calibration to your setup • clean on-screen overlay
> ✓ Low-latency release • regular updates • customer support
>
> **Lifetime adds:** every current & future product • priority support • early/beta access

---

## Ladder logic (internal notes — not customer-facing)

- **Distinct job per tier — no overlap.** $0 → $9.99 → $24.99 → $199.99, each with a clear reason to exist: trial = try, weekly = cheap short-term, sub = the value/recurring tier, lifetime = all-in whale. The old weekly/monthly closeness ($15/$25) is gone — the standalone one-time Monthly is retired so nothing steps on the sub.
- **Why the sub-$10 tail is retired:** the 1-Day/3-Day tiers had the highest cold-start failure rate and were refund magnets — a $2.99 buyer can generate a $15 support interaction. The trial absorbs "just let me try it" at $0.
- **Trial = 72h on purpose (growth + refund prevention).** 24h barely covers capture-card + Remote-Play setup; 3 days lets users past setup to the actual value, driving word-of-mouth at launch. A buyer who saw greens in the trial does not charge back the paid tier. Tighten to 24-48h later only if churn-after-trial gets high. Abuse is double-locked server-side (per-Discord + per-machine).
- **Priced higher on purpose.** This is a real competitive edge + a high-support, ban-risk-adjacent product — cheap pricing just attracts refund-hunters and support tickets that lose money. Fewer, more serious customers = more margin + less support load. Anchors are launch numbers; tune on real conversion/refund data (drop the sub to $19.99 if it proves too steep for the demographic).
- **Never discount Lifetime below $199.99** without repricing the sub — the anchor makes the sub look smart, and "all products + priority + early access" is the justification for the jump from the old $99.99.
