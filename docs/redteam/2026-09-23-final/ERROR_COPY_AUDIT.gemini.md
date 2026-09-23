# Customer-Facing Error & Warning Copy Audit (Gemini)
**Target:** Venice (Engineering Codename: Orion) — User-Facing UI & Notifications  
**Date:** 2026-09-23 (Final Launch Review)  
**Files Audited:**
- `native_orion/qml/**` (All gates, pages, and components)
- `native_orion/src/UiNotificationPolicy.h` (User Activity ring filter & redaction)
- `native_orion/src/LicenseClient.cpp` (License error code mapping)
- `native_orion/src/OrionAppController.h` / `.cpp` (Controller state & error generation)
- `native_orion/src/LicenseHeartbeatPolicy.h` (Lease notice copy)
- `website/src/worker.js` & `discord_launch/orion_bot.py` (Auth & pair copy)

---

## 1. Master Error & Warning Message Catalog

| Surface / Location | Exact On-Screen Text | Plain-Language? | Tells Customer What To Do? | Matches Support Guidance? | Internal Jargon Leaked? | Evaluation & Recommendation |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **Auth / License**<br>`LicenseClient.cpp:262` | `"Your subscription is paused — contact support."` | Yes | Partially | Yes | None | **Acceptable.** Clear, but could provide a direct clickable link to Discord support. |
| **Auth / License**<br>`LicenseClient.cpp:265` | `"This device is blocked."` | Yes | No | Yes | None | **Needs Action:** Blunt refusal. Does not tell user why or how to appeal. Add: *"Contact support in Discord if you believe this is an error."* |
| **Auth / License**<br>`LicenseClient.cpp:268` | `"An active Venice subscription tied to your Discord account is required. Run /purchase or open a ticket."` | Yes | Yes | Diverges | None | **Divergence:** Tells user to run `/purchase` in Discord, but web subscriptions are bought on `zaeorion.com`. Suggest: *"Renew on zaeorion.com or open a ticket in Discord."* |
| **Auth / License**<br>`LicenseClient.cpp:271` | `"Update required — this version is no longer allowed (minimum version %1)."` | Yes | Partially | Yes | None | **Acceptable.** Tell user: *"Restart Venice to update automatically, or re-download from Discord."* |
| **Auth / License**<br>`LicenseClient.cpp:281` | `"Your PC clock is off. Turn on 'Set time automatically' in Windows Date & Time settings, then restart Venice."` | Yes | **Yes (Excellent)** | **Exact Match** | None | **Exemplary.** Plain language, specifies the exact Windows setting to change, and provides recovery action. |
| **Auth / License**<br>`LicenseClient.cpp:286` | `"This licence is linked to a different PC. Use /hwid_reset in Discord or open a ticket."` | Yes | Yes | **Exact Match** | None | **Good.** Directs user to the free `/hwid_reset` command in Discord. |
| **Auth / Rate Limit**<br>`LicenseClient.cpp:349` | `"Activation is rate limited locally. Wait a moment and retry."` | Mostly | Yes | Yes | "locally" | **Minor Jargon:** "locally" is unnecessary engineering context. Simplify: *"Too many attempts. Wait a few seconds and click Unlock again."* |
| **Auth Gate**<br>`AuthGate.qml:28` | `"This account is linked to another PC. Run /hwid_reset in Discord, then connect this PC again. If that was not you, open a ticket."` | Yes | Yes | **Exact Match** | None | **Good.** Consistent with `LicenseClient.cpp`. |
| **Auth Gate**<br>`AuthGate.qml:30` | `"Your trial or subscription has ended. Run /status in Discord or renew on the Venice website."` | Yes | Yes | **Exact Match** | None | **Good.** Offers both Discord `/status` and web renewal. |
| **Auth Gate**<br>`AuthGate.qml:32` | `"Sign in with the Discord account that owns your trial or subscription, then use a fresh one-time code."` | Yes | Yes | **Exact Match** | None | **Good.** Clarifies Discord account identity mismatch. |
| **Auth Gate**<br>`AuthGate.qml:34` | `"That one-time code is invalid, expired, or already used. Open Connect Discord again for a fresh code."` | Yes | Yes | **Exact Match** | None | **Good.** Explains single-use and expiration property. |
| **Auth Gate**<br>`AuthGate.qml:36` | `"Couldn't reach the license server. Check your internet connection and try again in a moment."` | Yes | Yes | Yes | None | **Good.** Handles network timeouts and TLS failures. |
| **Auth Gate**<br>`AuthGate.qml:38` | `"Too many attempts in a row — wait a moment, then try the connection once."` | Yes | Yes | Yes | None | **Good.** Client-side anti-hammering text. |
| **Web Pairing**<br>`worker.js:572` | `"Your Discord account needs an active trial or subscription. Claim the trial in Discord, then try again."` | Yes | **Misleading** | **Breaks Post-Checkout** | None | **CRITICAL DEFECT:** Shown to paying customers during the Stripe webhook queue window. Must be changed to a retry/activating state. |
| **Web Pairing**<br>`worker.js:573` | `"Venice's servers didn't respond just now. Nothing is wrong with your account — wait a minute and try again. If it keeps happening, open a ticket in the Venice Discord."` | Yes | Yes | Yes | None | **Good.** Reassures customer that their account is safe during backend blips. |
| **Update Gate**<br>`UpdateGatePage.qml:213` | `"The required Venice update helper is missing, so this update can't be applied. Reinstall Venice from the latest package."` | Yes | Yes | Yes | "update helper" | **Acceptable.** Plain-language fallback when `OrionUpdater.exe` is absent. |
| **Update Gate**<br>`UpdateGatePage.qml:309` | `"Signed updates · Ed25519 verified"` | **No** | No | No | **"Ed25519"** | **JARGON LEAK:** Cryptographic algorithm name has zero customer utility. Replace with: *"Signed secure updates"*. |
| **Legal Gate**<br>`LegalGate.qml:124` | `"• For personal use only. Do not resell, redistribute, or share your license key."` | Yes | Yes | **Contradicts Keyless Flow** | **"license key"** | **CONTRADICTION:** Venice uses Discord account pairing; there is no license key. Replace with: *"share your account access"*. |
| **Live Stream**<br>`RemotePlayPage.qml:426` | `"No stream — Press Connect to start"` | Yes | Yes | Yes | None | **Good.** Clean idle placeholder. |
| **Input Delivery**<br>`OrionAppController.cpp:7852` | `"CONTROLLER INPUT IS NOT REACHING THE CONSOLE"` | Harsh | No | Yes | ALL CAPS | **Alarms User:** All-caps warning causes panic. Change to title case: *"Controller disconnected from console"*. |
| **Input Delivery**<br>`OrionAppController.cpp:7856` | `"The direct input link dropped — recovering it now. Presses are not landing in the game until it reconnects."` | Mostly | Explains State | Yes | "direct input link" | **Minor Jargon:** "direct input link" -> *"Controller connection dropped — reconnecting automatically."* |
| **Input Delivery**<br>`OrionAppController.cpp:7862` | `"This is the HDMI preview only — no session is connected. Press Enable Bot + Controller to connect."` | Yes | Yes | Yes | None | **Good.** Prevents player from trying to play on a passive HDMI preview. |
| **Input Delivery**<br>`OrionAppController.cpp:7866` | `"Reconnecting automatically — attempt %1 of %2."` | Yes | Informational | Yes | None | **Good.** Clear progress indicator. |
| **Input Delivery**<br>`OrionAppController.cpp:7873` | `"Automatic reconnect did not restore the session — press Connect. (%1)"` | Yes | Yes | Yes | `(%1)` raw code | **Needs Polish:** `(%1)` exposes raw internal status strings (e.g. `stream_promoted_failed`). Map to plain English. |
| **Fire Lease**<br>`RemotePlayPage.qml:672` | Tag: `"SHOTS PAUSED"` / Text: `"Reconnecting to Venice servers — shots paused until your licence is confirmed."` | Yes | Partially | Yes | "licence confirmed" | **Good:** Solves the 09-21 silent-disarm defect. Explains why Square isn't releasing during Wi-Fi drops. |
| **Shot Lead Card**<br>`ShotLeadCard.qml:70` | `"Shot Lead 85 is more than Tip Timing can schedule — live tip shots cannot fire and will abort. Lower it to 72 or less, or raise/reset Tip Timing."` | **No** | Yes | No | **"Tip Timing", "schedule", "live tip shots", "abort"** | **SEVERE JARGON LEAK:** 4 engineering terms in two sentences. Replace with: *"Shot Lead is set too high for your current setup. Lower it to %1 to prevent shot cancellation."* |
| **Shot Lead Card**<br>`ShotLeadCard.qml:96` | `"Shot Lead 60 disagrees with this rig's validated measurement of 45 (from 24 shots). Venice still fires with your value; the measurement is advisory."` | Mostly | Explains State | No | "validated measurement", "advisory" | **Verbose:** Replace with: *"Venice measured your setup at %1. You can keep your setting (%2) or click Reset to use the measured value."* |
| **Shot Lead Card**<br>`ShotLeadCard.qml:227` | `"Shots landing EARLY → lower it · LATE → raise it"` | Yes | **Dangerous** | **CONTRADICTS TIMING_EXPECTATIONS** | None | **HIGH REFUND RISK:** Prompts users to chase 2K server latency spikes, ruining learned timing. Change to: *"Tune only if shots are consistently off over 10+ shots"*. |
| **Shot Lead Card**<br>`OrionAppController.cpp:9321` | `"Shot 3 — lead 285 ms, adjusting by 12.5 ms. Keep going until it locks."` | Confusing | Yes | Yes | "ms" vs 1..100 | **Scale Mismatch:** Calibration communicates in milliseconds while user knob is 1..100. Normalize to slider units or display both. |
| **Shot Lead Card**<br>`OrionAppController.cpp:9334` | `"Lead calibration refused: an environment override owns the lead (ORION_LEAD_FLOOR_MS / ORION_LEAD_BIAS_MS). Clear it and relaunch…"` | **No** | For Devs | No | **"ORION_LEAD_FLOOR_MS", "ORION_LEAD_BIAS_MS"** | **ENGINEERING LEAK:** Dev environment variables exposed in user activity log. Hide in production builds. |
| **Tip Timing Card**<br>`TipTimingCard.qml:100` | `"Venice measured your jumpshot at 310 ms (14 shots). Click Unlock to let Venice track it, or Keep to stay on your number."` | Yes | Yes | Yes | None | **Good.** Actionable choices (`Unlock` vs `Keep`). |
| **Setup Form**<br>`StreamSetupForm.qml:160` | `"Xbox Remote Play — EXPERIMENTAL. No Xbox console has been measured yet, so shot timing on Xbox is untested and not supported."` | Yes | Warnings | Yes | None | **Brutally Honest.** Good legal protection, but raises the question of why Xbox is visible at all in a PS5 beta. |
| **Setup Form**<br>`StreamSetupForm.qml:316` | `"Pick your capture card (not a webcam). Set it to 1080p60 or 720p60 on a USB 3.0 port, turn HDCP off, and close other capture apps before you connect. If the feed is too slow or busy, the Activity feed says why."` | Yes | **Yes (Excellent)** | **Exact Match** | None | **Exemplary.** Solves the top 4 capture-card setup failures in a single concise callout. |

---

## 2. Leakage of Internal Engineering Jargon

The following terms were identified across the UI, activity feed, and user logs:

### 1. "Orion" (Internal Project Codename)
- **Found in:**
  - `OrionAppController.cpp:9334`: `"ORION_LEAD_FLOOR_MS"`
  - `LegalGate.qml`: Internal object naming `OrionNative`
  - URL schemes: `orion://activate?key=`
  - Log folder path: `%LOCALAPPDATA%\NexusVision\Orion Native\`
- **Remediation:** In production builds, ensure all public-facing text, dialog titles, and protocol messages use **Venice**.

### 2. "Tip Timing", "Live Tip Shots", "Scheduling", "Subtick"
- **Found in:**
  - `ShotLeadCard.qml:70-75`: `"more than Tip Timing can schedule — live tip shots cannot fire and will abort"`
- **Why it hurts:** Customers do not understand what "Tip Timing" is relative to "Shot Lead". Telling them a shot cannot be "scheduled" sounds like a calendar or background job error.
- **Remediation:** Rephrase around "shot cancellation" and "lead limit".

### 3. "Ed25519" (Cryptographic Algorithm)
- **Found in:**
  - `UpdateGatePage.qml:309`: `"Signed updates · Ed25519 verified"`
- **Why it hurts:** Cryptographic implementation detail that adds cognitive load with zero user benefit.
- **Remediation:** Change to `"Signed secure updates"`.

### 4. "Direct Input Link", "Pipe"
- **Found in:**
  - `OrionAppController.cpp:7856`: `"The direct input link dropped — recovering it now."`
- **Remediation:** Replace with `"Controller connection dropped"`.

### 5. "Sidecar"
- **Found in:**
  - `RemotePlayPage.qml:55`: In-code comments and historical log strings.
- **Status:** Successfully filtered out of the customer Activity ring by `UiNotificationPolicy.h:284` (`line.startsWith("Sidecar:") -> return false`). Verified working.

---

## 3. Consistency with Launch Embeds & Support Macros

| Support Macro / Embed Standard | Current UI Implementation | Verdict |
| :--- | :--- | :---: |
| **HDCP Black Screen:** `"Settings → System → HDMI → Enable HDCP (off)"` | Exact match in `StreamSetupForm.qml:316` and `FirstRunTour.qml:71`. | **PASS** |
| **Pair Code Expiration:** `"Codes expire in 5 minutes; fetch fresh code at zaeorion.com/connect"` | Exact match in `AuthGate.qml:34` and `connectPage` in `worker.js:552`. | **PASS** |
| **PC Clock Sync:** `"Turn on 'Set time automatically' in Windows Date & Time settings"` | Exact match in `LicenseClient.cpp:282` and `LicenseHeartbeatPolicy.h:118`. | **PASS** |
| **HWID Reset:** `"Use /hwid_reset in Discord"` | Exact match in `LicenseClient.cpp:287` and `AuthGate.qml:28`. | **PASS** |
| **Online Late Streaks:** `"Don't move Shot Lead after one or two lates — it is server jitter"` | **FAILED in `ShotLeadCard.qml:227`!** The UI card instructs: `"LATE → raise it"`, directly violating the macro. | **FAIL** |
| **Keyless Architecture:** `"There is no licence key; account links via Discord"` | **FAILED in `LegalGate.qml:124`!** Legal agreement says `"do not share your license key"`. | **FAIL** |
| **Meter Style Support:** `"Arrow2 only; Pill is withdrawn"` | **FAILED in `FirstRunTour.qml:80`!** Tour tells user to *"Pick the Style"*, but Style dropdown is locked. | **FAIL** |

---
*Audit complete. Written to `docs/redteam/2026-09-23-final/ERROR_COPY_AUDIT.gemini.md`.*
