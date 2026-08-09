# Orion — Support FAQ

> Customer-facing FAQ copy for the killer failure modes. Use in `#faq`, `#support-info`, and as the source text for the `/setup` and `/faq <topic>` bot commands. Each section is written to be posted standalone.

---

## 🎥 "My capture card isn't detected"

> **Orion can't see your capture card? Work down this list — it's almost always #1–#3.**
>
> **1. Close everything else that uses the card.** OBS, Discord video, the card's own viewer app, browser tabs with camera access — capture cards allow **one** app at a time. Close them all, then restart Orion.
>
> **2. Check the physical chain.** Card plugged into a **USB 3.0+ port** (blue, or the fastest port you have — avoid unpowered hubs)? HDMI from the **console's output** into the card's **IN** port? Console actually powered on and outputting a picture?
>
> **3. HDCP is the silent killer.** The PS5 encrypts HDMI output by default and your card will show **black or nothing**. On the PS5: **Settings → System → HDMI → disable "Enable HDCP."** This is the single most common "not detected" cause.
>
> **4. Driver check.** Open Windows **Device Manager** — does the card show up (usually under "Cameras" or "Sound, video and game controllers") without a warning icon? If not, install the manufacturer's driver and replug.
>
> **5. Still nothing?** Some cards only expose certain resolutions — set the console output to **1080p** and try again.
>
> If it still won't show, open a ticket with: **card model, Windows version, console, and a screenshot of Device Manager**. Known-good cards are listed in the requirements — if you haven't bought a card yet, check that list first.

---

## 🎮 "Remote Play won't pair / the stream is laggy"

> **Pairing problems:**
> 1. On the PS5: **Settings → System → Remote Play → ON**, and **Settings → System → Power Saving → Features Available in Rest Mode → "Stay Connected to the Internet" + "Enable Turning On PS5 from Network"** both ON.
> 2. Get the pairing PIN from **Settings → System → Remote Play → Link Device** — it expires quickly, so have Chiaki open and ready.
> 3. PC and PS5 must be on the **same network** for first pairing. Enter the console's IP exactly as shown under Settings → Network → Connection Status.
> 4. Pairing loops or times out? Reboot the PS5 fully (not rest mode) and generate a fresh PIN.
>
> **Lag / stutter / rubber-banding:**
> - **Wire everything.** Ethernet to the PS5 and Ethernet to the PC is the setup Orion is built for. Wi-Fi on either end adds jitter that no software can time through. If you must use Wi-Fi: 5 GHz / Wi-Fi 6, same room as the router, nothing else streaming.
> - Drop the Remote Play stream quality one notch — a stable 720p stream times better than a stuttering 1080p one.
> - Close downloads, cloud sync, and anyone else's Netflix on the network.
>
> **Be straight with yourself here:** Orion reads your stream and is exactly as fast as that stream. A clean wired link = clean greens. A congested Wi-Fi link = late reads, and no setting can fix physics. This is also why we say it **before** you buy.

---

## 🟩 "No greens / the overlay isn't reading the meter"

> **Orion runs but shots come out early, late, or the overlay never locks onto the meter? Check these in order:**
>
> **1. Is the stream actually visible in Orion?** Open the Live Capture panel — you should see your gameplay. If it's black, that's the real problem: reconnect (Disconnect → Connect Chiaki), make sure Chiaki shows the game and not its setup screen, and keep the Orion window visible (don't minimize it).
>
> **2. Hold, don't tap.** Orion releases for you — **hold** Square (tempo), right-stick-down (stick tempo), or right-stick-up (go-to) and let go of nothing. If you release manually, you're fighting the tool.
>
> **3. Controller in the right place?** It must be plugged into the **PC**. If your pad is paired to the PS5 directly, Orion can see the meter but can't act on it.
>
> **4. Meter visible in-game?** Shot meter ON in 2K's settings, and use a meter style/size Orion has calibrated against (defaults work best). A hidden or exotic meter gives the vision system nothing to read.
>
> **5. Let calibration finish.** The first 10–15 shots in practice teach Orion your jumper. Judging it off shot #2 in a Rec game is judging it before it's calibrated.
>
> **6. Stream quality dips = read quality dips.** If lag spikes coincide with the misses, it's the network — see the Remote Play section above.
>
> Still off after all six? Open a ticket with a **short clip or screenshot of the overlay during a shot** — that one image usually tells us the fix immediately.

---

## 💻 "I got a new PC" (moving your license / HWID reset)

> Your license is **single-user and locked to one PC** — that's what keeps keys from being shared and your purchase from being resold. Moving to a new machine is self-service:
>
> 1. In the Discord server, run **`/hwid_reset`**.
> 2. The bot unbinds your key from the old machine.
> 3. On the new PC: install Orion, paste your **same key**, activate. Done.
>
> **Limits (anti-sharing, not anti-you):**
> - Once per **24 hours**.
> - Up to **3 resets per 30 days**.
>
> Rebuilt Windows, swapped major hardware, or RMA'd the machine? Same command — a reinstall on the same box sometimes reads as a "new" machine and one reset fixes it.
>
> Legitimately need more than the limit (multiple hardware failures, stolen laptop)? Open a ticket with a short explanation — staff can reset it manually. What we **don't** do is resets that let two people share one key; that pattern gets the key revoked.

---

## 🔑 "I paid but never got my key"

> Nine times out of ten this is the **Discord ID field** at checkout — it needs your numeric User ID, not your username.
>
> 1. Make sure you've **joined this server** and your **DMs are open** (Privacy Settings → Direct Messages ON), then check your DMs again — delivery can take a minute.
> 2. Still nothing? Open a ticket in **#create-ticket** with the **email you used at checkout** — we'll verify the purchase and hand you your key directly. You will not be left paying for nothing.
>
> *(Buying in future? Instructions for copying your numeric Discord ID are in #how-to-buy — Settings → Advanced → Developer Mode ON, then right-click your name → Copy User ID.)*

---

## Quick answers

- **Is there a free trial?** Yes — `/claim_trial` in the server gets you 72 hours (3 full days), full features, no card. One per Discord account and per PC.
- **Does every tier have all features?** Yes. Tiers differ only in duration.
- **Can I use it on two PCs at once?** No — single-user, one machine. Use `/hwid_reset` to move it.
- **When does my license expire?** Run `/status` with your key in the server — instant, private answer. (Your key is in your original delivery DM.)
- **What does Orion actually do to my game?** Nothing. It watches your screen and times a controller input — it never touches game files, game memory, or your console. See the "What Orion is" post in #faq.
