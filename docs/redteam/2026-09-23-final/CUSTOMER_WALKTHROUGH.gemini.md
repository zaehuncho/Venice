# Customer-Experience Walkthrough & First-Green Paper Test (Gemini)
**Target:** Venice (NBA 2K27 Shot-Timing Tool on PS5)  
**Date:** 2026-09-23 (Final Launch Review)  
**Perspective:** Brand-new paying customer ($19.99/mo beta subscriber / 7-day trial user)  
**Scope:** Step-by-step journey from website purchase to first green jumpshot in NBA 2K27.  
**Constraint:** Strict read-only on functional code; verified against working tree sources.

---

## 1. Journey Step 1: Website Purchase & Account Pairing

### Flow
1. User visits `https://zaeorion.com/` (`website/public/index.html`).
2. Clicks **"Start free 7-day trial"** or **"Subscribe · $19.99/month"** (`/#pricing`).
3. If clicking Subscribe, redirects through `/buy` to Stripe Checkout.
4. After entering credit card details on Stripe, Stripe redirects back to:  
   `https://zaeorion.com/discord?purchase=complete` (rendered by `website/src/worker.js:522-537`).
5. User is shown the Discord Connected account panel and clicks **"Get your one-time code"** (href `/connect`).
6. `/connect` invokes `connectLauncher()` (`website/src/worker.js:563-595`) which calls the backend `/api/bot/pair-issue`.

### Question 1: Would a customer know what to do next?
- **Yes, until checkout completion.** The homepage CTA is clear.
- **However, upon return from Stripe**, the user is placed on `/discord?purchase=complete`. The page displays:
  > *"Discord connected."*  
  > *"Checkout complete. The Venice bot will DM your subscription confirmation in Discord as soon as activation finishes."*
- Below this notice, two buttons appear side-by-side:
  - `[Subscribe · $19.99/month]` (Primary button style!)
  - `[Get your one-time code]` (Secondary button style)
- A customer who just paid $19.99 sees the primary button asking them to subscribe *again*. This creates immediate friction and doubt: *"Did my payment fail? Why is it asking me to pay again?"*

### Question 2: What could confuse them?
- **Primary vs. Secondary Button Hierarchy:** Having `Subscribe` styled as `button primary` on a page confirming checkout completion is backwards. The primary action must be `Get your one-time code`.
- **Discord ID Misconception:** The copy warns:
  > *"This is the Discord profile linked to this browser. Venice uses this verified account for checkout and launcher access; your Discord ID by itself is not a sign-in code."*  
  While truthful, users often copy their numeric Discord ID (e.g. `1549499802738499705`) and try to paste it into the launcher.

### Question 3: What would make them think "it's broken"?
- **The Post-Checkout Webhook Race Condition (`worker.js:572-587`):**
  If the customer clicks *"Get your one-time code"* within 1–2 seconds of returning from Stripe, Stripe's asynchronous webhook (`checkout.session.completed`) may still be executing in AWS Lambda (`/api/bot/provision`).  
  When `connectLauncher()` calls `/api/bot/pair-issue`, the backend reports the user has no active entitlement (403 Forbidden).  
  The user is immediately presented with:
  > **ACCOUNT ACCESS**  
  > **Not ready to connect.**  
  > *"Your Discord account needs an active trial or subscription. Claim the trial in Discord, then try again."*  
  > `[View your Discord account →]`
- **Customer Reaction:** *"I just gave you my credit card and paid $19.99, and your website says I have no subscription and need to claim a trial! This is a scam!"* Customer immediately files a Stripe dispute or chargeback.

---

## 2. Journey Step 2: Discord `/connect` & Setup Guide

### Flow
1. Customer joins the official Venice Discord server.
2. Checks `#setup-guide` (`discord_launch/launch_embeds/setup_guide.json`) or runs `/connect` in bot commands (`discord_launch/orion_bot.py:734-754`).
3. Reads embed instructions on linking their account.

### Question 1: Would a customer know what to do next?
- `#setup-guide` field 1 (`setup_guide.json:19-21`) instructs:
  > **1 • Install and unlock**  
  > *"Download the signed installer from <#1549499799861067876> and launch Venice. Open zaeorion.com/connect, sign in with the same Discord account you use here, and enter the one-time code in the launcher. There is no licence key, and your Discord ID alone is not a code."*
- This is clear, actionable, and explicitly debunks the "licence key" and "Discord ID" pitfalls.

### Question 2: What could confuse them?
- **Slash-command DMs:** If a user runs `/claim_trial` in Discord, `orion_bot.py` attempts to DM the trial confirmation. If their Discord server privacy settings disallow DMs from server members, the bot replies with:
  > *"We couldn't DM you. In the Venice server, open Privacy Settings and allow Direct Messages, then try again."*
- Many users do not know how to find Discord server-specific privacy settings (right-click server icon → Privacy Settings → Direct Messages).

### Question 3: What would make them think "it's broken"?
- **Discord Bot Token / Rate Limiting:**
  If the bot interaction fails or is slow, users run `/claim_trial` repeatedly and hit:
  > *"Too many attempts. Wait a few minutes and try again."*
  They assume the bot is offline or broken.

---

## 3. Journey Step 3: The Installer (`installer/orion.iss` & `INSTALL_NOTICE.txt`)

### Flow
1. User downloads `VeniceSetup-1.0.0.exe` from the releases channel.
2. Launches installer on Windows 10/11.
3. Steps through Inno Setup wizard:
   - Welcome Page (`WelcomeLabel1="Welcome to Venice"`)
   - Directory Selection (`DefaultDirName={autopf}\Venice`)
   - Driver Installation (`ViGEmBus`, `HidHide`, `VC++ Redistributable`, `VeniceNetSvc`)
   - Finish Page (`FinishedHeadingLabel="Venice is installed"`).

### Question 1: Would a customer know what to do next?
- The welcome page (`orion.iss:139-140`) gives clear pre-flight instructions:
  > *"Welcome to Venice"*  
  > *"Setup will install Venice — jump-shot timing for NBA 2K27 on PS5 — on this PC.*  
  > *Before you continue, close any capture software (OBS, your capture card's own viewer) so Venice can use the card, and keep your controller plugged into this PC over USB.*  
  > *Venice is in beta: PS5 with a capture card is the supported setup."*
- The instructions tell the user exactly what hardware setup is required before clicking Next.

### Question 2: What could confuse them?
- **UAC Elevation Prompt:** Setup requires `PrivilegesRequired=admin` (`orion.iss:92`). Standard users see a Windows UAC credential prompt. If they run on a managed or family PC without admin rights, installation aborts without explanation.
- **Pending Reboot Notice (`orion.iss:676-679`):**
  If ViGEmBus driver installs cleanly but returns exit code `3010` (restart required), setup finishes and displays:
  > *"A controller driver was installed and Windows needs a restart to finish. Please restart your PC before opening Venice."*
  Users frequently ignore this dialog, launch Venice immediately, and hit controller detection faults.

### Question 3: What would make them think "it's broken"?
- **Microsoft Defender SmartScreen Blue Screen:**
  Because the beta installer is signed with a newly generated or self-hosted certificate without accumulated SmartScreen reputation, Windows Defender displays:
  > **Windows protected your PC**  
  > *Microsoft Defender SmartScreen prevented an unrecognized app from starting. Running this app might put your PC at risk.*  
  > `[Don't run]`
- **Customer Reaction:** *"This software is a virus/trojan."* Non-technical gamers do not know to click `More info` → `Run anyway`. (`INSTALL_NOTICE.txt` addresses this, but users rarely read separate text files inside zip archives).
- **Driver Error Codes (`DRV-01`, `DRV-02`, `VNSVC-01`):**
  If driver installation is blocked by third-party antivirus, Inno raises a modal dialog:
  > *"The ViGEmBus virtual controller driver could not be installed. Venice needs it to send controller input to your console. Restart your PC and run Setup again. (code DRV-02)"*
  Customer cannot complete installation and files an immediate ticket.

---

## 4. Journey Step 4: First Launch, Pairing, Capture Card & Remote Play

### Flow
1. Launcher starts (`OrionNative.exe`).
2. Checks for updates (`UpdateGatePage.qml`).
3. Enters **Legal Gate** (`LegalGate.qml`): User accepts terms.
4. Enters **Auth Gate** (`AuthGate.qml`): User pastes one-time code from `zaeorion.com/connect`.
5. Enters **Stream Setup Gate** (`StreamSetupGate.qml`): User selects Console (PS5) and Video Source (Capture Card or Remote Play).
6. Lands on **Live Page** (`RemotePlayPage.qml`): First-Run Tour (`FirstRunTour.qml`) pops up over the interface.

### Question 1: Would a customer know what to do next?
- **UpdateGate:** Automatically proceeds ("Checking for updates…" → "Continuing on the current version…").
- **LegalGate:** Straightforward: "I Agree" button is highlighted in primary blue.
- **AuthGate:** Clear: Input field reads `"One-time code from Discord sign-in"` with a `[Paste]` button and a prominent `[Connect Discord]` button opening `zaeorion.com/connect`.
- **StreamSetupGate:** Direct: User chooses PS5 and Video Source, then clicks `[Continue]`.
- **FirstRunTour:** 9-step guided walkthrough highlighting each control.

### Question 2: What could confuse them?
- **Contradictory Jargon in Legal Gate (`LegalGate.qml:124`):**
  > *"• For personal use only. Do not resell, redistribute, or share your license key."*  
  The user just read in `#setup-guide` and `AuthGate`: *"There is no licence key"*. Seeing "share your license key" makes them wonder: *"Wait, where is my license key? Did I miss an email?"*
- **Ed25519 Cryptography Jargon in Update Gate (`UpdateGatePage.qml:309`):**
  > *"Signed updates · Ed25519 verified"*  
  Unnecessary engineering detail shown to a console gaming audience.
- **FirstRunTour Step 6 ("Match your meter") (`FirstRunTour.qml:80`):**
  > *"Pick the Style and Color that match the in-game meter."*  
  The tour instructs them to pick the **Style**, but `MeterConfigPanel.qml:88-93` has `meterStyleCombo` locked to `Arrow2` only, with `enabled: false` and `opacity: 0.55` (Pill was withdrawn). The customer tries to click the Style dropdown, finds it completely unresponsive, and thinks the UI is bugged.

### Question 3: What would make them think "it's broken"?
- **Non-Hinted Capture Card ($15 MS2109, Razer, NZXT) (`remote_play_orchestrator.py:812-817`):**
  Customer uses a popular budget USB HDMI capture card (named `"USB Video"`, `"USB3 Video"`, `"Razer Ripsaw"`, or `"NZXT Signal"`).
  - In `StreamSetupGate.qml`, the card appears in the dropdown, the live PS5 feed is visible in preview, and they click Continue.
  - On `RemotePlayPage.qml`, the status bar reads:  
    > **`TIMING WARMING UP - SHOTS STAY MANUAL`**
  - Because `_latency_route_scope` rejects any device name not containing keyword hints (`elgato`, `avermedia`, etc.), **timing authority is permanently withheld**.
  - The customer shoots in NBA 2K: Venice **never releases the shot**. Square must be released manually on every attempt.
  - The customer concludes: *"Venice does nothing. It doesn't work."*
- **Black Screen / HDCP (`RemotePlayPage.qml:418-430`):**
  If HDCP is enabled on PS5, preview shows "No stream" or black frame. There is no on-preview notification that HDCP is blocking video; the user must dig into `#setup-guide` or `#faq`.
- **Clock Skew Lockout (`LicenseClient.cpp:282`):**
  If the customer's Windows clock is off by >300 seconds, activation fails with:
  > *"Your PC clock is off. Turn on 'Set time automatically' in Windows Date & Time settings, then restart Venice."*  
  Clear instruction, but requires user to know how to open Windows Date & Time settings.

---

## 5. Journey Step 5: Guided Lead Calibration & First Game

### Flow
1. On `RemotePlayPage.qml`, user scrolls down the right-hand panel to the **Shot Lead** card (`ShotLeadCard.qml`).
2. Reads the **Guided Calibration** panel (`ShotLeadCard.qml:298-410`).
3. Clicks **"Calibrate my lead"** (`objectName: "shotLeadCalibrateButton"`).
4. Enters 2K27 Shoot-Around, takes shots, reads NBA 2K27's feedback banner (`EARLY`, `GOOD`, `LATE`), and taps the corresponding button in Venice.
5. After 10–15 shots, calibration locks.
6. User enters an online Park / Rec game.

### Question 1: Would a customer know what to do next?
- **Yes.** The guided calibration panel is one of the best-designed features in the app:
  > *"Not set yet — 10 to 15 open shots find this rig's own number."*  
  > *"Take a shot, then tap what the game's TIMING banner said. Skip any shot where you did not see the banner."*  
  Buttons `[EARLY]`, `[GOOD]`, `[LATE]`, and `[Skip — I missed the banner]` make input straightforward.

### Question 2: What could confuse them?
- **Millisecond Units vs. 1..100 Slider Scale:**
  The Shot Lead slider at the top of the card is strictly scaled **1 to 100** (`from: 1, to: 100`).
  However, once calibration starts, `leadCalibrationHint` (`OrionAppController.cpp:9321`) announces:
  > *"Shot 3 — lead 285 ms, adjusting by 12.5 ms. Keep going until it locks."*  
  And upon completion (`:9313`):
  > *"Locked at 280 ms after 11 shots. Saved — you should not need to touch this again unless you change capture hardware."*
  The customer looks up at their slider, sees number `53`, and reads the text saying `280 ms`. They wonder: *"Is my lead 53 or 280? Did it save properly?"*
- **Opposing Conventions Between Shot Lead and Tip Timing:**
  Beside Shot Lead sits **Tip Timing** (`TipTimingCard.qml`).
  - Shot Lead: higher number releases *earlier*.
  - Tip Timing: higher number releases *later*.
  Customers who touch both knobs get hopelessly confused about which direction to adjust.

### Question 3: What would make them think "it's broken"?
- **The Online Late-Streak Trap (`ShotLeadCard.qml:227` vs `timing_expectations.json:22`):**
  Customer calibrates successfully in Shoot-Around (greens 10/10 shots).  
  Enters an online game. Due to 2K server network jitter, two shots in a row come back **LATE**.  
  Customer glances at Venice. The Shot Lead card explicitly says:
  > **`Shots landing EARLY → lower it · LATE → raise it`**  
  Customer follows the instruction and raises Shot Lead by 6 points.  
  **Result:** Learned calibration is blown out. On the next shot, server lag clears, but Venice releases 15 ms too early. The shot is a brick. Customer misses 5 shots in a row, rages, and concludes: *"Venice worked in practice but is broken online. Refund."*
- **Meter Detection Lost on Deep Shots:**
  From 30+ feet out, 2K shrinks the green window. In-game camera zooms out, making the meter smaller. If `simple_meter_reader.py` loses pixel confidence, the bot falls back to blind hold timing, causing erratic releases without a clear on-screen explanation.

---

## 6. Top 15 Customer-Experience Fixes (Ranked by Refund Risk)

| Rank | File : Line | Current Customer Text | Suggested Customer Text | Why it matters (Refund Risk) |
| :---: | :--- | :--- | :--- | :--- |
| **1** | `website/src/worker.js:572` | `"Your Discord account needs an active trial or subscription. Claim the trial in Discord, then try again."` | `"Activating your subscription… Please wait a few seconds, then refresh. (Stripe is finishing your setup)"` | **CRITICAL:** Paying customers who click Connect right after checkout are told they have no subscription. Direct chargeback trigger. |
| **2** | `remote_play_orchestrator.py:812-817` | `_latency_route_scope` rejects non-hint cards with `configured_index_not_a_capture_card` (leaves bot in `"TIMING WARMING UP"`) | Accept any DirectShow device explicitly configured by user in Setup; do not block timing solely on name heuristic. | **CRITICAL:** Budget USB dongles ($15 MS2109) and cards without hint words show video but never shoot. Bot appears dead. |
| **3** | `native_orion/qml/components/ShotLeadCard.qml:227` | `"Shots landing EARLY → lower it · LATE → raise it"` | `"Tune only if shots are consistently off over 10+ shots · LATE streaks online are usually server jitter"` | **CRITICAL:** Direct contradiction with `timing_expectations.json`. Prompts users to chase server lag and ruin their calibration. |
| **4** | `website/src/worker.js:531` | Two buttons: `button primary` (`Subscribe · $19.99/mo`) and `button secondary` (`Get your one-time code`) | On `purchase=complete`, make `Get your one-time code` the prominent primary button; hide or mute the Subscribe CTA. | **HIGH:** Customers who just paid are presented with a button asking them to pay again, causing payment failure panic. |
| **5** | `native_orion/qml/components/FirstRunTour.qml:80` | `"Pick the Style and Color that match the in-game meter."` | `"Venice currently supports the Arrow2 meter style in any color. Make sure Arrow2 is equipped in 2K."` | **HIGH:** Tour instructs user to pick a Style, but the Style dropdown is locked and disabled (`Arrow2` only), appearing broken. |
| **6** | `native_orion/src/OrionAppController.cpp:9321` | `"Shot %1 — lead %2 ms, adjusting by %3 ms. Keep going until it locks."` | `"Shot %1 — lead %2 (calibrating). Keep taking open shots until it locks."` (use 1..100 scale units) | **HIGH:** Calibration speaks in raw milliseconds (`280 ms`), while the customer slider is scaled `1 to 100`. |
| **7** | `installer/orion.iss:140` | Welcome copy warns about OBS and USB, but does not mention Windows SmartScreen. | Add prominent note: `"If Windows SmartScreen appears: click 'More info' then 'Run anyway'."` | **HIGH:** Non-technical gamers encounter Defender SmartScreen and abandon setup believing it is malware. |
| **8** | `native_orion/qml/pages/LegalGate.qml:124` | `"• For personal use only. Do not resell, redistribute, or share your license key."` | `"• For personal use only. Do not resell, redistribute, or share your account access."` | **MEDIUM:** Mentions a "license key" when Venice uses keyless Discord account pairing; confuses users about missing keys. |
| **9** | `native_orion/qml/components/ShotLeadCard.qml:70-75` | `"Shot Lead 85 is more than Tip Timing can schedule — live tip shots cannot fire and will abort. Lower it to 72 or less…"` | `"Shot Lead is set too high for your current setup. Lower it to %1 to prevent shot cancellation."` | **MEDIUM:** Internal engineering jargon ("Tip Timing", "live tip shots", "schedule") causes confusion and concern. |
| **10** | `native_orion/qml/pages/RemotePlayPage.qml:672` | `tag: "SHOTS PAUSED"` / `text: orion.leaseNotice` (`"Reconnecting to Venice servers — shots paused until your licence is confirmed."`) | `"Reconnecting to Venice servers… (taking open shots manually until confirmed)"` | **MEDIUM:** Alarming banner appears during brief Wi-Fi hiccups; plain language reduces support panic. |
| **11** | `native_orion/qml/pages/UpdateGatePage.qml:309` | `"Signed updates · Ed25519 verified"` | `"Signed secure updates"` | **LOW-MEDIUM:** Cryptographic implementation detail ("Ed25519") has no customer value and clutters footer. |
| **12** | `native_orion/src/LicenseClient.cpp:269` | `"An active Venice subscription tied to your Discord account is required. Run /purchase or open a ticket."` | `"Your Venice subscription has expired or was not found. Renew at zaeorion.com or open a ticket in Discord."` | **MEDIUM:** Tells user to run `/purchase` in Discord, but web subscriptions are managed on `zaeorion.com`. |
| **13** | `native_orion/src/OrionAppController.cpp:7852` | `"CONTROLLER INPUT IS NOT REACHING THE CONSOLE"` | `"Controller disconnected from PS5 — check USB cable or press Connect"` | **MEDIUM:** All-caps technical alert creates panic; simpler operational instruction eases troubleshooting. |
| **14** | `native_orion/qml/components/StreamSetupForm.qml:160` | `"Xbox Remote Play — EXPERIMENTAL. No Xbox console has been measured yet…"` | In PS5-focused beta, hide Xbox or place behind an advanced toggle to prevent users buying for Xbox and refunding. | **MEDIUM:** Marketing claims PS5 support, but UI offers Xbox with disclaimers, prompting accidental Xbox purchases. |
| **15** | `native_orion/qml/pages/AuthGate.qml:34` | `"That one-time code is invalid, expired, or already used. Open Connect Discord again for a fresh code."` | Add clickable button in error card: `[Get Fresh Code]` that opens `zaeorion.com/connect` directly in browser. | **LOW-MEDIUM:** User gets stuck re-typing the expired code and hits local rate limiting. |

---
*Walkthrough analysis complete. Written to `docs/redteam/2026-09-23-final/CUSTOMER_WALKTHROUGH.gemini.md`.*
