# Orion — Messaging & Positioning

> The canonical positioning doc. Everything customer-facing — product pages, embeds, ads, ticket replies, announcement posts — pulls its language from here. The two jobs of this doc: (1) lock the honest computer-vision differentiator, (2) keep us from ever using language that trips payment processors or platforms.

---

## The one-liner (lead line — already live, keep it)

> **Orion is an AI-vision assistant that reads the on-screen shot meter and times your release to the green — consistently, across standstills, fades, go-to and tempo shots.**

Short form (page titles, bios, footers):

> **Precision Shot-Timing for NBA 2K.**

---

## The core differentiator (the load-bearing truth)

Orion is **pure computer vision plus a controller press**. Say it plainly, everywhere:

> **Orion sees your screen and times a controller press — nothing more.**
> It never reads or modifies game memory, never touches game files, and never runs anything on your console. It watches the same pixels you watch and presses the same button you'd press — just with better timing. Nothing Orion does is something a human couldn't do; it's just more consistent at it.

**Why this matters (customer-facing framing):**

> Most timing tools work by hooking into the game itself. Orion doesn't — it can't even see the game's insides, only the video stream on your PC. Your console runs a completely stock game, untouched, exactly as Sony and 2K shipped it.

**Supporting proof points (use as bullet points under the differentiator):**

- ✓ Reads the **video stream on your PC** — the same picture your eyes see
- ✓ Outputs a **standard controller input** through a virtual gamepad
- ✗ No game-file modification — the game install is never touched
- ✗ No memory reading or injection — Orion doesn't even run on the console
- ✗ Nothing installed on your PlayStation — it stays 100% stock

---

## Why Orion, vs other timing tools (honest differentiator)

Other shot-timing tools exist. Don't trash them by name — win on what's demonstrably true about Orion. The edge is **method + honesty**, and both are checkable:

> **1. It reads the meter, it doesn't touch the game.** Orion times off the *video* — the same picture you see. Nothing hooks the game, reads memory, or runs on your console. That's the whole architecture, and it's why the ban-fear answer is a fact, not a promise.
>
> **2. It calibrates to *your* build.** Orion doesn't fire on a fixed timer — its calibration loop learns your jumper over the first 10–15 shots and re-tunes when 2K changes the meter. Generic "release at X%" timing drifts the moment your build or the game patch does; a learned window doesn't.
>
> **3. It tells you the truth before you pay.** A free 72-hour trial on *your* setup, honest requirements (wired Ethernet, HDCP off, supported capture card) stated pre-purchase, and "improves consistency" instead of "never miss." We'd rather lose a sale to an honest requirements page than eat a refund.
>
> **4. It's a real product, not a drop.** Signed, verified updates. Machine-bound licenses. A preflight wizard that catches a bad setup before your first possession. Support that answers in hours and whose first job is getting you running.

**Rule:** lead with *how Orion works* and *what we promise*, never with a claim about a competitor's internals we can't verify. Our honesty is the differentiator — the moment we start slinging, we sound like everyone else.

---

## The accessibility & training framing (keep — and keep it true)

This framing is already in the ToS and it's genuine — Orion's calibration loop literally learns and reinforces a user's timing:

> Orion is built as an **assistive and training tool**: it helps players — including those with motor or reaction-time impairments — hit timing windows that pure reflexes make inaccessible, and its calibration and overlay feedback help any player *learn* their jumper's rhythm. You watch the overlay track the meter, you see where green lives, and your own timing improves alongside it.

Usable phrases:
- "an assistant, not a replacement — it learns your jumper, you learn your timing"
- "levels the reflex playing field"
- "training wheels you can actually see through" (casual channels only)

---

## Honest expectations (say this BEFORE purchase, not after)

Under-promise on purpose. Every line below is a refund that never gets filed:

> - Orion **improves consistency** — it does not promise a green on every shot. Stream quality, latency spikes, and in-game factors still exist. It times your release better than reflexes can, every possession; it doesn't rewrite the odds to 100%.
> - **The first 10–15 shots are calibration**, not the finished product. Orion learns your build's jumper before it locks in — judge it after it's dialed, not on shot #2.
> - Orion is **only as fast as your Remote Play feed**. Wired Ethernet is strongly recommended; congested Wi-Fi means late reads, and no software fixes physics.
> - **It needs a real setup**: a supported capture card, HDCP off on the PS5, your controller on the PC. These are stated before checkout on purpose — a requirement you read beforehand is never a surprise afterward.
> - Check the **requirements** before buying — or better, take the **free 72-hour (3-day) trial** and see it on your own setup first. That's exactly what it's for.

---

## ❌ The NEVER-say list

These words never appear in any Orion channel, page, ad, embed, bot reply, or ticket response — from staff or in official copy. They are red flags to payment processors, hosting platforms, and Discord itself, and they misdescribe what Orion is:

| Never say | Say instead |
|---|---|
| ~~undetectable / undetected~~ | **"Compatibility: current 2K build supported"** |
| ~~cheat / cheating~~ | assistant, timing tool, trainer |
| ~~hack / hacked client~~ | AI-vision software, overlay |
| ~~aimbot / bot~~ (as product descriptor) | shot-timing assistant |
| ~~auto-win / never miss / 100% greens~~ | "improves consistency", "times your release to the green" |
| ~~bypass / evade / anti-ban~~ | (nothing — don't discuss detection at all; state the architecture facts: no injection, no memory access, console untouched) |
| ~~injection-free~~ *(implies injection is the norm we dodge)* | "pure computer vision — no game files touched" |

**Rule of thumb:** describe what Orion **is** (vision + a controller press), never what it **avoids**. The moment copy starts arguing about detection, it sounds like the thing it isn't.

### Specific required rename (`#status` channel)

The `#status` channel line "uptime / 2K version compatibility / **undetected status**" is renamed. The status embed fields are:

> - **Service** | 🟢 Operational
> - **Compatibility** | ✅ Current 2K build supported
> - **Latest Version** | `{{VERSION}}`

No "undetected" field exists anywhere. If a customer asks "is it undetected?" in public channels, the scripted staff answer is:

> "Orion doesn't touch the game or your console at all — it's computer vision on your PC that times a controller press, nothing a human couldn't do. There's nothing running on the game's side. Compatibility with the current 2K build is tracked in #status."

---

## Legitimacy signals (weave into store copy)

These read as "real product, real company" to both customers and processors:

- **Single-user, machine-bound licenses** — keys are personal, enforced automatically; shared or resold keys are revoked. (Anti-piracy = we protect paying customers.)
- **Signed releases** — every update is cryptographically verified by the launcher before it runs.
- **Free trial before any charge** — we'd rather you test it at $0 than refund it at $25.
- **Help-before-refund support** — tickets answered in hours, and the goal is always "get you running."
- **Honest requirements page** — we tell you the wired-Ethernet truth *before* checkout.

---

## Voice & tone

- **Confident, specific, zero hype.** "Times your release to the green" beats "DOMINATE THE REC."
- **Basketball-native.** Greens, whites, jumper, Rec, Park, go-to — speak the game's language.
- **Honest about limits.** Every limitation stated pre-purchase is a chargeback that never happens.
- Emoji: sparing and functional (🟩 for greens, 🛒 🎫 for navigation), matching the existing embed style. Brand color `#2563EB` everywhere.
- Footer signature: **Orion • Precision Shot-Timing**
