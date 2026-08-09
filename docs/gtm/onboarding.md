# Orion — Onboarding Flow Copy

> Customer-facing copy for the full purchase → license → download → install → activate → first-shot journey. Use across the Gumroad product page, the purchase-confirmation DM, `#how-to-buy`, `#downloads`, and `#setup-guide`. The Discord-ID section is **required on the product page** — it prevents the "paid, got nothing" failure.

---

## Step 0 — Before you buy: copy your Discord User ID

*(Place this ABOVE the checkout button on the product page and in `#how-to-buy`. The checkout has a required "Discord ID" field — if it's blank or wrong, your key can't be delivered to you automatically.)*

> ### 📋 Grab your Discord User ID first (takes 20 seconds)
>
> Your license key is delivered straight to your Discord DMs, so checkout asks for your **Discord User ID** — that's a **long number** (17–19 digits, like `204789633539112960`), **not** your username.
>
> **How to copy it:**
> 1. Open Discord → **Settings** (⚙ gear, bottom-left) → **Advanced**.
> 2. Turn on **Developer Mode**.
> 3. Close Settings. **Right-click your own name or avatar** (in any chat, or your profile in the bottom-left) → **Copy User ID**.
> 4. Paste that number into the **Discord ID** field at checkout.
>
> ✅ Correct: `204789633539112960` (a number)
> ❌ Wrong: `@coolshooter`, `coolshooter#0`, your display name
>
> **On phone?** Settings → scroll to **Advanced** (under App Settings) → Developer Mode ON → tap your profile → tap the **⋯** menu → **Copy User ID**.
>
> Also make sure you've **joined our Discord server** and your DMs are open (Server → Privacy Settings → "Direct Messages" ON) — the bot can't DM someone who isn't in the server.

*(Fallback line, once `/redeem` ships:)*
> Typo'd it anyway? Don't worry — run `/redeem your@email.com` in the server with the email you used at checkout, or open a ticket in `#create-ticket`. Nobody pays and walks away empty-handed.

---

## Step 1 — Purchase

> ### 🛒 Buy Orion
> 1. Pick your tier in **#pricing** — Free Trial, Weekly Pass, Monthly Subscription, or Lifetime.
> 2. Click **Purchase** — secure checkout, instant delivery.
> 3. Fill in the **Discord ID** field with the number you copied in Step 0.
> 4. Complete payment.
>
> Within a minute you'll get a **DM from the Orion bot** with your license key (hidden behind a spoiler tag — click to reveal) and the **💎 Customer** role is added automatically, unlocking `#downloads` and `#setup-guide`.
>
> **No DM after 5 minutes?** Check that you're in the server and your DMs are open, then open a ticket in `#create-ticket` with your **order email** — we'll deliver your key manually.

---

## Step 2 — Your license key (delivery DM copy)

*(This is the bot DM the buyer receives.)*

> **🎉 Welcome to Orion!**
>
> Your license key: ||`XXXX-XXXX-XXXX-XXXX`||  *(click to reveal — keep it private)*
>
> **Next steps:**
> 1. 📥 Download the installer from **#downloads**.
> 2. 🛠 Follow **#setup-guide** — 10 minutes, one-time.
> 3. 🔑 Paste your key into the launcher when asked.
>
> Your key is **single-user and locked to your PC** on first activation. Don't share it — shared keys are auto-revoked.
> Moving to a new PC later? Use `/hwid_reset` in the server (see #faq).
>
> Stuck at any step → **#create-ticket**. We'd rather fix it than refund it.

---

## Step 3 — Download & install (`#downloads` copy)

> ### 📥 Download Orion
> Grab the latest installer below. Every release is **cryptographically signed** — the launcher verifies each update before it runs, so you always get the authentic build.
>
> **Requirements (check these BEFORE installing):**
> - Windows 10/11 PC
> - PS5 with **Remote Play** enabled (Settings → System → Remote Play), or a supported capture card
> - Your controller plugged into the **PC** (not the console)
> - **Wired Ethernet strongly recommended** for the console and PC — Orion's timing is only as good as your stream. Stable 5 GHz / Wi-Fi 6 is the minimum.
>
> Install steps:
> 1. Run the installer.
> 2. Launch Orion and paste your license key.
> 3. Continue to first-run setup (**#setup-guide**).

---

## Step 4 — Activate

> ### 🔑 Activation
> Paste your key into the launcher and hit **Activate**. That's it — the key binds to this PC and you're licensed.
>
> - Activation needs an internet connection (it checks in with our license server).
> - Your license quietly re-verifies in the background while you play — no action needed from you.
> - **"Key already activated on another machine"?** That's the machine lock doing its job. If it's your own new PC, run `/hwid_reset` in the Discord (self-service, once per 24h). If you never activated it, open a ticket immediately — your key may have leaked.

---

## Step 5 — First-run setup (`#setup-guide` copy)

> ### 🛠 First-Run Setup (one-time, ~10 minutes)
>
> **1. Install ViGEmBus** — the driver Orion uses to create its virtual controller. The launcher links you straight to it.
>
> **2. Connect Remote Play** — Orion uses Chiaki for the PS5 stream:
> - Point Orion at your Chiaki install (or let it place one).
> - Pair your PS5 once: on the console, Settings → System → Remote Play → **Link Device** shows a PIN; enter it in Chiaki.
> - Set your console's IP in Orion (find it under PS5 Settings → Network → Connection Status).
> - Press **Connect Chiaki** — the stream embeds into Orion's Live Capture panel.
>
> **3. Plug your controller into the PC.** Orion reads your real pad and drives the game through its virtual one. If the controller is paired to the PS5 directly, Orion can't time your shots.
>
> **4. Verify the picture.** You should see your PS5 stream live inside Orion. Black screen? See #faq → "Live Capture is black."

---

## Step 6 — First-run preflight (let Orion check itself before you play)

> ### ✅ Preflight — 60 seconds, once
> Before your first game, Orion runs a **Preflight Wizard** so you find any problem *now* — at a menu, not mid-Rec with money on the line. It checks three things and tells you exactly what's wrong if one fails:
>
> 1. **Capture / stream** — confirms your capture device or Remote Play feed is live and Orion is receiving frames (no black screen).
> 2. **Controller path** — confirms your real pad is on the **PC** and Orion's virtual controller is reaching the game.
> 3. **Calibration test shot** — you take **one shot** at a menu or in practice; Orion shows you the **detected meter box** on the overlay so you can see it's locked onto the right thing.
>
> Green across all three? You're cleared to play. A red check tells you the fix (or links the right FAQ) before you've wasted a single possession. **Don't skip preflight on a new setup** — it's the difference between "it just worked" and a frustrated first session.

---

## Step 7 — Your first shot

> ### 🟩 Your First Green
> 1. Load into **MyCOURT or Freestyle** (practice first — no pressure, no defenders).
> 2. In-game, Orion works on **held** inputs:
>    - **Hold Square** for a tempo shot — *don't release manually*.
>    - **Hold right stick down** for stick tempo shots.
>    - **Hold right stick up** for go-to shots.
> 3. Orion reads the meter off the stream and releases for you at the green window. You'll see the overlay track the meter as it fills.
> 4. **The first 10–15 shots are calibration — expect that.** Orion is learning *your* build's jumper timing; the first few may run early or late on purpose while it dials in. Take them in practice, not a Rec game, and don't judge Orion off shot #2. By shot ~15 the timing tightens and stays there.
>
> **Still seeing whites or earlies after calibration?** Don't grind through it — check the FAQ's "no green / overlay not reading" section or open a ticket. A 2-minute settings fix beats a frustrated session.
>
> Landed your first green streak? Drop it in **#clips** 🟩

---

## Flow summary (one-liner for the product page)

> **Buy → key lands in your Discord DMs → download → 10-minute setup → 60-second preflight check → hold the button, Orion releases at green.** Stuck anywhere? Ticket us — average response within a few hours.
