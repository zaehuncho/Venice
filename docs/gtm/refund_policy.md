# Orion — Refund Policy & Prevention

> Two parts: (1) the customer-facing "help before refund" policy blurb for `#terms-of-service`, the product page, and ticket macros; (2) the top-5 refund causes with the prevention play for each — internal, for staff and copy placement.

---

## Part 1 — Customer-facing policy blurb

### Long form (ToS / product page)

> ### 🔄 Refunds — read this first
>
> Orion is a digital product delivered instantly, so **all sales are final** — with one big caveat in your favor:
>
> **If you can't get Orion running, we will get you running.** Open a ticket in `#create-ticket` before anything else. The overwhelming majority of "it doesn't work" cases are a 5-minute fix — an HDCP setting, a controller plugged into the wrong device, a Wi-Fi link that needs a cable. Our support team's first job is making your purchase work, and tickets are answered within a few hours.
>
> If we genuinely can't get Orion working on a setup that meets the published requirements, we'll make it right — that's a conversation we're happy to have inside a ticket.
>
> **Two things protect everyone here:**
> - **Try before you buy.** The free 72-hour (3-day) trial (`/claim_trial`) is full-featured. If you're unsure about your capture setup or network, test at $0 — that's exactly what the trial is for.
> - **Chargebacks skip the line in the wrong direction.** Filing a chargeback instead of opening a ticket instantly and permanently revokes your license and access. If something's wrong, talk to us first — we're faster than your bank.

### Short form (checkout / FAQ field)

> **Refund policy?** Digital goods — all sales are final. But open a ticket before you write anything off: nearly every "won't work" is a quick fix, and we don't consider a case closed until you're running. Not sure it'll work on your setup? That's what the free 72-hour (3-day) trial is for.

### Ticket macro (staff paste for refund requests)

> Hey! Before we talk refunds — let's spend 10 minutes getting you running, because in most cases we can. Can you send:
> 1. Your **order email**
> 2. Your setup: **capture card model or Remote Play**, **wired or Wi-Fi**
> 3. **Where it stops** — a screenshot or short clip of what you see
>
> If we walk the setup steps together and it genuinely can't work on your rig, we'll sort you out fairly. Deal?

---

## Part 2 — Top-5 refund causes → prevention (internal)

### 1. Cold-start: "it didn't work in the first 5 minutes" *(dominant cause)*
The buyer installs, sees a black screen or a missed shot, and refunds before ever reaching a green.
- **Prevention:** the first-run **preflight wizard** (validates capture device + Remote Play window + a calibration test shot *before* the first game) — fail loud and early, never a silent black overlay mid-game. Plus the `/setup` bot troubleshooter at the moment of failure, plus the **free trial absorbing the first impression at $0**.
- **Copy placement:** onboarding "First-Run Setup" and "Your First Green" sections set the expectation that the first 10–15 shots are calibration.

### 2. Wrong or HDCP-blocked capture card
Buyer's card is incompatible, single-app-locked, or HDCP-blackscreened — looks like "product is broken."
- **Prevention:** publish the **known-good card list in the pre-purchase requirements**, put the HDCP toggle (PS5 Settings → System → HDMI → disable HDCP) at the top of the FAQ's capture section, and make the `/setup` card flow walk it step by step.
- **Copy placement:** faq.md §"capture card isn't detected", requirements block in onboarding.md Step 3.

### 3. Wi-Fi / Remote Play latency
Congested Wi-Fi → late reads → whites → "this thing doesn't work."
- **Prevention:** **"wired Ethernet strongly recommended" as a PRE-purchase requirement**, stated bluntly: "Orion is only as fast as your Remote Play feed." An expectation set before checkout is a refund that never gets filed.
- **Copy placement:** onboarding requirements, faq.md §Remote Play, messaging.md "Honest expectations."

### 4. "I paid and got nothing" (Discord-ID delivery failure)
Blank or mis-entered Discord ID at checkout → provisioning fails silently → buyer has money gone and no key → chargeback with the buyer 100% in the right.
- **Prevention:** the **"copy your numeric Discord User ID" instructions above the checkout button** (Developer Mode → Copy User ID), the email-fallback `/redeem` path, and failed-DM alerts pinging staff so *we* reach out before the buyer disputes.
- **Copy placement:** onboarding.md Step 0 (the full instructions), faq.md §"I paid but never got my key."

### 5. Expectation mismatch: "not 100% greens" / ban fear
Buyer expected a guaranteed green every shot, or panics about account safety and refunds preemptively.
- **Prevention:** honest messaging everywhere — "**improves consistency**," never "never miss"; the CV/no-injection architecture explained plainly ("watches your screen, presses a button — the game and console are never touched") which truthfully addresses the fear; and the help-before-refund funnel catching the wobble before it becomes a dispute.
- **Copy placement:** messaging.md "Honest expectations" + differentiator, pricing.md feature block ("times your release," not "guarantees greens").

### Backstop (already enforced, keep in staff docs)
A processed refund or chargeback **auto-revokes the license**; the client's periodic license check fails closed within minutes. Nobody refunds and keeps the product — which is also why we can afford to be generous *inside* the ticket funnel.
