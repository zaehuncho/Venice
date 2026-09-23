# Venice support macros

Copy-paste replies for the tickets we expect at launch. Written 2026-09-23 (RT-HIGH-04 / CL3-F8-010,
P-E patch). Every quoted in-app line is the real string after the P-E patch; if a ticket quotes
something different, the customer is on an older build (ask for the version in **Profile**).

Rules for every reply:

- Never ask for, or accept, a one-time code, password, card number or the full Discord token in a
  public channel. Tickets only.
- There is **no licence key**. Access lives on the customer's Discord account. Don't say "key".
- The app's buttons are **Connect**, **Disconnect**, **Repair settings**, **Exit safe mode**.
  Don't say "Chiaki", "sidecar", "lease" or "Orion" to customers.
- Supported setups (owner decision 2026-09-23): PS5 with a **60 Hz capture card** or **PS5 Remote
  Play**, both fully supported. **Xbox is experimental and untested**: help with setup, but make
  no timing promise. Shot meter style: **Arrow2 (White)**.
- The free trial starts on the **zaeorion.com home page** (not in Discord).
- The beta installer is **not code-signed** (owner decision 2026-09-23).
- Escalate to the owner: anything that looks like a pattern (2+ customers, same symptom, same
  hour), a payment dispute, or a suspected account share.

---

## Install and sign-in

### M-INSTALL-1: "Windows protected your PC" / SmartScreen
> The beta installer isn't code-signed yet, so Windows shows "Windows protected your PC". Click
> **More info**, then **Run anyway**. Setup asks for administrator rights because it installs a
> controller driver.
>
> To check the file first, run this in PowerShell and compare it with the SHA-256 posted in
> #downloads:
> `Get-FileHash $HOME\Downloads\VeniceSetup-*.exe`
> If it doesn't match, don't run it. Delete it and download again from #downloads only.

### M-INSTALL-2: how to unlock / "what's my key?"
> There's no key. Open Venice, then go to https://zaeorion.com/connect, sign in with the **same
> Discord account** you use in this server, and paste the one-time code into Venice. Your Discord
> ID alone isn't a code.

### M-TRIAL-1: where do I start the trial?
> Start it on the https://zaeorion.com home page: sign in with this Discord account and press
> **Start free 7-day trial**. It's one per Discord account and one per PC, no card needed. Then get
> your one-time code at https://zaeorion.com/connect.

### M-AUTH-1: "That one-time code is invalid, expired, or already used."
> Codes are single-use and expire quickly. Open https://zaeorion.com/connect again for a fresh code
> and paste it straight into Venice.

### M-AUTH-2: "This licence is linked to a different PC."
> Your access is locked to one PC. Run `/hwid_reset` here, then connect the new PC with a fresh
> one-time code. The first 3 resets are free; after that each reset takes one day off your
> subscription. If you didn't move PCs, tell us here and don't reset.

### M-AUTH-3: "Your PC clock is off." or a secure-connection error
> Venice needs your PC's clock to be right. Open Windows **Settings > Time & language > Date &
> time**, turn on **Set time automatically**, press **Sync now**, then restart Venice. If the date
> itself is wrong (for example years off after a battery change), fix that first.

### M-AUTH-4: "Couldn't reach Venice's servers."
> That one really is the connection. Check the PC is online, pause any VPN, and try again in a
> minute. If you're on a school or work network, it may block us.

---

## Shots and detection

### M-GREENS-1: no greens / shots early or late
> 1. The Live page must show your gameplay. If it's black, press **Disconnect**, then **Connect**.
> 2. In 2K, turn the shot meter **on**, style **Arrow2 (White)**.
> 3. **Hold** the shot button and let Venice release it.
> 4. The controller must be plugged into the **PC**, not the PS5.
> 5. Take 10 to 15 practice shots before judging it; Venice learns your setup.
> Still off? Send a short clip of the Live page during one shot.

### M-GREENS-2: "NOT TIMING" banner (Venice can't see the meter)
> Venice has stopped timing because it can't see the shot meter, so it's not guessing. Check the
> meter is on and set to **Arrow2 (White)**, and that the game (not a menu) is on the Live page.
> Take a couple of shots with the meter showing and it turns back on by itself. If this started
> right after a 2K update, see #announcements; we're on it.

### M-GREENS-3: "Shot not taken — Shot Lead N is too high for your jumpshot…"
> Lower Shot Lead to the number the message gives, or press **Reset** on Tip Timing. Then take a
> few practice shots.

### M-FEED-1: "Feed paused: Venice is minimized, so shots are paused. They resume when you bring the Venice window back."
> Expected. Windows stops drawing a minimized window, so Venice pauses shots. Keep the Venice
> window open (it can sit behind other windows), not minimized.

---

## Capture card

### M-CAPTURE-1: "Capture: Your capture card (…) is busy or has no picture."
> Only one app can use a capture card at a time. Close OBS, Discord video, browser tabs with camera
> access and the card's own viewer, then press **Connect** again. Also check the PS5 is awake and
> HDCP is off (**PS5 Settings > System > HDMI > Enable HDCP: off**).

### M-CAPTURE-2: "Capture: Your capture card is set to 30 Hz. Shot timing is only tested at 60 Hz, so this is preview only and the bot will not shoot."
> Open **Stream Setup**, set **Refresh rate** to **60 Hz (recommended)**, and reconnect. At
> 30 Hz Venice shows the picture but doesn't time shots. Set the card itself to 1080p60 or 720p60
> on a USB 3.0 port.

### M-CAPTURE-3: "Capture: Your capture device is dropping frames" / "sending N fps" / "repeating frames"
> Plug the card straight into a USB 3.0 port on the PC (no hub), close heavy apps, and set it to
> 1080p60 or 720p60. Venice won't shoot until the feed is steady; that's on purpose.

### M-CAPTURE-4: safe mode with "The capture card stopped sending video"
> The capture card stopped sending video a few times in a row, so Venice paused shots. Check the
> USB and HDMI cables and close anything else using the card. Venice usually turns shots back on
> by itself once the stream has been steady for 30 seconds; otherwise click **SAFE MODE** at the
> top, then **Exit safe mode**.

---

## Remote Play

### M-RP-1: "Another device is using Remote Play on this PS5 (code RP-10). Close Remote Play there, then press Connect."
> The PS5 allows one Remote Play session at a time. Close PS Remote Play on your phone, tablet or
> other PC (or sign out of it there), then press **Connect** in Venice.

### M-RP-2: pairing fails or the stream lags
> On the PS5: **Settings > System > Remote Play: on**, and in **Power Saving > Features Available
> in Rest Mode** turn on "Stay Connected to the Internet" and "Enable Turning On PS5 from Network".
> Pair on the same network with a fresh PIN. For lag, use **wired Ethernet** on both the PS5 and
> the PC; Wi-Fi jitter can't be timed through. Dropping stream quality one notch helps too.

### M-RP-3: coded Remote Play messages
> - "Venice couldn't reconnect your controller (code RP-07). Press Connect." Press **Connect**.
> - "Venice couldn't link your controller to the PS5 (code RP-08). Disconnect and connect again."
>   Press **Disconnect**, then **Connect**.
> - "Venice's detection engine didn't start (code RP-06)." Restart Venice; if it repeats,
>   reinstall from #downloads.
> - "Part of Venice is missing from this install (code RP-05)." Reinstall from #downloads.
> - "Venice hit an unexpected error (code RP-09)." Disconnect and connect again; if it repeats,
>   restart Venice and send us **Export diagnostics** (the SAFE MODE dialog, or ask staff).

### M-INPUT-1: "Controller input isn't reaching your PS5"
> Keep the controller plugged into the PC over USB. Venice is reconnecting on its own; if the
> message stays for more than a few seconds, press **Connect**.

---

## Settings, safe mode, sleep

### M-SETTINGS-1: "Venice's settings didn't save cleanly (code ST-01), so shots are off."
> This happens when the PC shut down or crashed in the middle of saving settings. On the Live page
> press **Repair settings**. Venice goes back to its default settings; your shot timing history is
> kept. After that, check Shot Lead and your meter style, then press **Connect**.
> If it says "Repair settings didn't finish (code ST-02)", restart Venice and press **Repair
> settings** again; if it repeats, open a ticket.

### M-SETTINGS-2: "Venice restored your last saved settings after an interrupted save."
> Venice caught a save that was cut off (usually a forced shutdown) and put back your last good
> settings. Nothing to do. If a setting you just changed is back to its old value, set it again.

### M-SAFE-1: SAFE MODE pill
> Safe mode means something kept failing, so Venice paused shots to be safe. Click **SAFE MODE** at
> the top to see the reason. Venice usually turns shots back on by itself once the stream has been
> steady for 30 seconds, or press **Exit safe mode**. If it keeps coming back, press **Export
> diagnostics** in that dialog and send us the zip from your Desktop (venice_diagnostics_….zip) in
> a ticket.

### M-SLEEP-1: after sleep / resume
> Venice pauses shots while the PC sleeps. After you wake the PC it says "Venice resumed from
> sleep. If the picture or your controller doesn't come back, press Disconnect, then Connect."
> If shots show **SHOTS PAUSED** for a few seconds, that's Venice re-checking your subscription.

### M-LEASE-1: "Reconnecting to Venice servers — shots paused until your subscription is confirmed. Usually a few seconds."
> Venice re-checks your subscription in the background; shots pause until it answers. If it lasts
> more than a minute, check the PC's internet connection.

---

## Updates and service status

### M-UPDATE-1: update didn't finish / "Update required"
> Restart Venice to let it update. If the update doesn't finish, download the latest installer
> from #downloads and install it over the top; your settings and timing history are kept.

### M-PATCH-1: after a 2K update, before we know
> 2K just updated and we're checking Venice against it now. If Venice shows **NOT TIMING** or shots
> feel off, shoot manually for now. No need to reinstall or change settings. Updates go in
> #announcements.

### M-PATCH-2: "Venice is paused by the service right now. Nothing is wrong with your PC or internet."
> That's us: we paused Venice for everyone while we fix an issue. Nothing is wrong with your PC or
> internet, so there's nothing to change. Watch #announcements; when it's back, press **Unlock**
> again (or just restart Venice).

---

## Account and billing

### M-HWID-1: moving to a new PC
> Run `/hwid_reset` here. The first 3 resets are free; after that each reset takes one day off
> your subscription (the bot asks you to confirm first). Then install Venice on the new PC and
> connect with a fresh one-time code from https://zaeorion.com/connect.

### M-BILLING-1: cancel / refund
> You can cancel any time in the billing portal:
> https://billing.stripe.com/p/login/5kQ7sL0Ec4Ya5MfcFsgQE00. You keep access until the end of the
> period you've paid for. For a duplicate charge or a problem we couldn't fix, include the email on
> your Stripe receipt in this ticket and we'll review it.

### M-XBOX-1: Xbox
> Xbox is **experimental and untested** in the beta. You can try it (pick **Xbox** under Console in
> Stream Setup and tick the acknowledgement), but no Xbox console has been measured yet, so we
> can't promise timing or tune it for you yet. PS5 with a capture card or Remote Play is fully
> supported.
