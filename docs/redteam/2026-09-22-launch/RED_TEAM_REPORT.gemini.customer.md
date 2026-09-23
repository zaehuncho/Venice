# Customer-Experience Reliability & Refund-Risk Review (Gemini)
**Target:** Venice (Engineering Codename: Orion) — NBA 2K27 Jump-Shot Timing for PS5  
**Reviewer:** Gemini (Independent Red-Team / Customer-Reliability Review)  
**Date:** 2026-09-22 (Beta Launch Wave)  
**Scope:** Working tree `C:\Users\aaron\Desktop\NexusVision` (branch `fix/timing-input-and-remoteplay-blockers`)  
**Mission:** Identify every failure point where a paying customer could reasonably conclude *"it's broken"* and request a refund or dispute a charge. Verify exact customer-facing messages/strings, evaluate whether they provide actionable recovery steps, specify concrete fixes, and provide operational blue-team runbooks.

---

## Executive Summary

Venice has made substantial progress in hardening its low-level timing loop and input reliability. However, from the perspective of a **paying customer ($19.99/mo)**, several critical customer-experience traps remain that will directly trigger refund requests, chargebacks, and Discord support inundation within the first two hours of launch if left unaddressed.

The primary customer failure modes are:
1. **The Post-Checkout Webhook Race:** A customer completes Stripe payment and immediately clicks *"Get your one-time code"*. If the Stripe webhook is still processing in Lambda, the launcher/web UI presents: *"Your Discord account needs an active trial or subscription. Claim the trial in Discord, then try again."* The customer concludes they were charged without receiving access and immediately disputes the charge.
2. **Unsupported "Generic" Capture Cards ($15 USB Video MS2109/MS2130, Razer, NZXT):** Setup displays live video in the preview, but because the DirectShow moniker does not match a hardcoded keyword hint (`elgato`, `avermedia`, etc.), the backend rejects the timing route scope. The bot remains in `"TIMING WARMING UP - SHOTS STAY MANUAL"` indefinitely. The customer shoots, the bot never releases Square, and the user demands a refund.
3. **In-App Copy Contradicting Online Timing Expectations:** The launcher UI explicitly instructs: *"Shots landing EARLY → lower it · LATE → raise it"*. When an online player encounters two consecutive LATEs due to 2K server jitter, they follow the on-screen prompt and raise Shot Lead. This wipes their calibrated baseline and ruins subsequent sessions.
4. **Hardcoded `D:\` Drive for Shot Telemetry:** `shot_records.py` defaults to appending records to `D:\NexusVision\shot_records` with a synchronous `os.fsync` on every shot. On customer PCs lacking a `D:\` drive or using a slow external USB drive, file operations fail or introduce periodic frame stalls.
5. **PC Sleep / Hibernate Disarming the Bot:** Monotonic clocks stall during system suspend. Upon resume, the fire lease expires, silently disarming automation without visual notification until the next 5-minute timer tick.

---

## Part A — Correction & Status of GM2 Findings

Every finding from `RED_TEAM_REPORT.gemini.md` (GM2-001 through GM2-026) has been re-verified against today's working tree (`2026-09-22`).

| Finding ID | Title | Previous Severity | Current Status | File : Line & Evidence |
| :--- | :--- | :--- | :--- | :--- |
| **GM2-001** | Unauthenticated local entitlement bypass | CATASTROPHIC | **DOWNGRADED to LOW** | `native_orion/src/LeaseGate.cpp:35-38`. In production builds (`#ifdef ORION_PRODUCTION_BUILD`), `LeaseGate::enabledFromEnvironment()` unconditionally returns `true`. The lease gate is forced ON; memory patching of `authenticated_` without a valid server lease token is strictly blocked by `LeaseGate::fireAllowed()`. |
| **GM2-002** | Local privilege escalation to SYSTEM via VeniceNetSvc | CATASTROPHIC | **DOWNGRADED to HIGH (Conditional)** | `installer/orion.iss:87, 598-601`. Default installation folder `{autopf}\{#MyAppName}` (`C:\Program Files\Venice`) has inherited Windows ACLs that restrict standard users from writing to `{app}\packet_bridge`. Vulnerability is conditional on the user manually selecting an unprotected custom path (e.g., `C:\Venice` or secondary drives). |
| **GM2-003** | VeniceNetSvc arbitrary file write / token hijack via ProgramData | CATASTROPHIC | **STILL OPEN** | `installer/orion.iss:226-236`, `venicenet_service/IpcServer.h:180-210`. `C:\ProgramData\NexusVision` is created without an explicit restrictive DACL. Non-admin users can pre-create the directory or set an NTFS junction before the elevated service writes `nexus_bridge.token`. Tracked as master item M-06. |
| **GM2-004** | Unauthenticated named pipe squatting / injection on OrionControllerInput | CRITICAL | **STILL OPEN** | `chiaki-ng-src/gui/src/orioninputbridge.cpp:68-74`. Named pipe creation lacks `FILE_FLAG_FIRST_PIPE_INSTANCE` and client PID authentication (`GetNamedPipeClientProcessId`). Any local process can connect or squat the pipe. Tracked as master item M-07. |
| **GM2-005** | Broken singleton mutex allows dual concurrent launcher instances | CRITICAL | **FIXED IN WORKING TREE** | `native_orion/src/main.cpp:372-386`. Singleton check now calls `activateRunningInstance()` and immediately terminates with a user messagebox: *"Venice is already running. Switched to the existing window."* Instance #2 exits cleanly with code 0. |
| **GM2-006** | Production onset feedforward A/B PRNG leak (`ORION_ONSET_FF_AB`) | CRITICAL | **FIXED IN WORKING TREE** | `native_orion/src/AutomationEngine.cpp:1656-1662`. `parseOnsetFeedforwardArms` is now wrapped inside `#ifndef ORION_PRODUCTION_BUILD`. In release builds, `onsetFfAbArms_` is empty and environment overrides are ignored. |
| **GM2-007** | Local delivery ack timeout in Chiaki fork triggers route flap | CRITICAL | **DOWNGRADED to MEDIUM (Partially Fixed)** | `chiaki-ng-src/gui/src/orioninputbridge.cpp`. Batch 1 patches landed soft fault recovery (`session=kept`) and delivery queue decoupling (CL2-P2-003, CL-001). Remaining residual route-flap edge cases tracked in master report M-10. |
| **GM2-008** | Analog trigger redundancy void & persistent R2 clamping | CRITICAL | **DOWNGRADED to MEDIUM (Partially Fixed)** | `chiaki-ng-src/lib/src/feedbacksender.c`, `native_orion/src/ControllerRoutingPolicy.h`. Batch 1 patched redundant release delivery and neutral trigger checks (CL2-P4-002, CL-003). Residual edge cases tracked in master report M-03. |
| **GM2-009** | Unmasked license key exposure in Discord bot fallback | CRITICAL | **DOWNGRADED to LOW** | `discord_launch/orion_bot.py:823`. The fallback reply is marked `ephemeral=True` to the executing staff member. While printing raw keys is against blue-team policy, exposure is limited to authorized operators. |
| **GM2-010** | Repeat trial farming via client-supplied machine IDs | HIGH | **STILL OPEN** | `backend/lambda_function.py:760-771`, `native_orion/src/MachineIdentity.cpp:53-62`. DynamoDB `TRIALMACHINE#` keys rely on client-asserted `machine_id` without hardware attestation. Tracked as master item M-20. |
| **GM2-011** | Owner 2FA TOTP disabled without verification code | HIGH | **STILL OPEN** | `backend/lambda_function.py:4632-4638`. Action `totp_disable` removes 2FA requirement without challenging for current TOTP code or password. Tracked as master item M-18. |
| **GM2-012** | Missing `SetDefaultDllDirectories` exposes launcher to DLL hijack | HIGH | **STILL OPEN** | `native_orion/src/main.cpp:320-350`. Neither `OrionNative.exe` nor `OrionActivate.exe` calls `SetDefaultDllDirectories`. Vulnerable to DLL preloading if launched from a folder containing untrusted DLLs. Tracked as master item M-19. |
| **GM2-013** | Late trajectory recovery suppressed under active onset feedforward | HIGH | **STILL OPEN** | `native_orion/src/AutomationEngine.cpp:20825`. Late correction is suppressed when `schedFireAppliedOnsetFfMs_ != 0.0`. Mid-flight latency spikes fail to adjust release offset. Tracked as master item M-25. |
| **GM2-014** | Hardcoded 150 ms floor in Lead Calibration Policy | HIGH | **STILL OPEN** | `native_orion/src/LeadCalibrationPolicy.h:21`. `kLeadMinMs = 150.0` prevents optimal calibration on ultra-low latency capture cards and wired LAN setups (<110 ms). Tracked as master item M-27. |
| **GM2-015** | Silent blind misfires when 2K in-game meter graphics break | HIGH | **STILL OPEN** | `native_orion/src/AutomationEngine.cpp`, `simple_meter_reader.py`. When meter tracking is lost, bot falls back to blind hold times instead of displaying an in-app warning or failing closed. Tracked as master item M-13. |
| **GM2-016** | Missing nonce verification on license heartbeat | HIGH | **DOWNGRADED to MEDIUM (Still Open)** | `backend/lambda_function.py:1742-1810`. `/api/license/check` verifies HMAC session token but omits nonce single-use check. Tracked as master item M-35. |
| **GM2-017** | WinDivert IPC token file readable by Interactive Users | HIGH | **STILL OPEN** | `venicenet_service/IpcServer.h:180-210`. Token file inherits default permissions allowing any interactive user on the PC to read the bearer secret. Tracked as master item M-06. |
| **GM2-018** | Fixed 30-day subscription causes lockout on 31-day months | MEDIUM | **STILL OPEN** | `website/src/worker.js:642, 664`, `backend/lambda_function.py:37`. Subscriptions provisioned with `days: 30` expire 24 hours before Stripe charges the 31-day renewal invoice. Tracked as master item M-23. |
| **GM2-019** | Full DynamoDB table scan on `GET /api/admin/metrics` | MEDIUM | **STILL OPEN** | `backend/lambda_function.py:4725`. Metric handler executes `_scan_all(licenses_table())`, risking Lambda execution timeouts as table grows. Tracked as master item M-37. |
| **GM2-020** | Console IP discovery drift probes stale DHCP addresses | MEDIUM | **STILL OPEN** | `native_orion/src/RemotePlaySession.cpp:1840-1865`. Cached console IP lacks TTL expiration; probes stale IP for up to 10 minutes following console DHCP change. Tracked as master item M-34. |
| **GM2-021** | Sleep / hibernate monotonic lease expiry disarms bot | MEDIUM | **STILL OPEN** | `native_orion/src/LeaseGate.cpp:125-140`. Divergence between wall-clock epoch and monotonic clock during PC sleep invalidates lease without triggering immediate wake-up refresh. Tracked as master item M-09. |
| **GM2-022** | Missing `charge.dispute.closed` won webhook handler | MEDIUM | **STILL OPEN** | `website/src/worker.js:690-714`. Worker lacks handler for won disputes, leaving merchant-favored dispute accounts permanently revoked. Tracked as master item M-23. |
| **GM2-023** | System clock skew > 300s causes activation failure | MEDIUM | **STILL OPEN (UI copy improved)** | `backend/lambda_function.py:621`, `native_orion/src/LicenseClient.cpp:282`. Client now maps `timestamp_expired` to: *"Your PC clock is off. Turn on 'Set time automatically' in Windows Date & Time settings, then restart Venice."* Backend still rejects skewed timestamps. Tracked as master item M-35. |
| **GM2-024** | Internal IP and network topology logged in plaintext | LOW | **DOWNGRADED to LOW** | `native_orion/src/RemotePlaySession.cpp:1420-1450`. Private LAN IPs logged to developer log; diagnostic bundle should sanitize before sharing. |
| **GM2-025** | Settings signature salted with machine ID forgeable offline | LOW | **DOWNGRADED to LOW** | `native_orion/src/SecurityManager.cpp:921-926`. Signature acts as tamper-evident check rather than cryptographic barrier. |
| **GM2-026** | Inno Setup manifest hash fragility on excluded admin tools | LOW | **DOWNGRADED to LOW** | `tools/package_orion_release.py`. Build pipeline audit check; no runtime customer impact. |

---

## Part B — The Customer Journey (Step-by-Step)

### Step 1: Buy / Trial (Website, Stripe, Discord)

#### [GMC-001] CRITICAL — Post-Stripe Checkout Race Condition Displays "Needs Active Subscription" to Paying Customers
- **Journey Step:** 1. Buy / Trial
- **What the Customer Sees:**  
  Customer completes $19.99 checkout on Stripe. Stripe redirects to `https://zaeorion.com/discord?purchase=complete`, displaying:
  > *"Checkout complete. The Venice bot will DM your subscription confirmation in Discord as soon as activation finishes."*
  
  The user immediately clicks **"Get your one-time code"** (href `/connect`). If Stripe's webhook to the Cloudflare Worker (`checkout.session.completed`) or the Worker's onward call to Lambda (`/api/bot/provision`) takes more than 1–2 seconds, `/connect` queries `/api/bot/pair-issue`, which returns 403 Forbidden. The user is redirected to `connectPage` displaying:
  > **ACCOUNT ACCESS**  
  > **Not ready to connect.**  
  > *"Your Discord account needs an active trial or subscription. Claim the trial in Discord, then try again."*  
  > `[View your Discord account →]`
- **Does it tell them what to do?** No. It falsely claims they have no subscription, instructing them to claim a trial or buy again. The customer believes their money was stolen, panics, and immediately opens a bank dispute or Stripe chargeback.
- **Refund Risk:** **CRITICAL**. Direct driver of Day-1 fraud claims and chargebacks.
- **Required Fix:**  
  In `website/src/worker.js:563-595` (`connectLauncher`), if `session` has a recent `purchase=complete` cookie or query parameter, do not fail immediately on 403. Implement a 3-second polling retry (or render a dedicated *"Activating subscription… Please wait a few seconds"* transitional state) before falling back to `ENTITLEMENT_MESSAGE`.
- **Verification Test:** Mock Stripe checkout completion; immediately request `/connect` within 200 ms before `/api/bot/provision` executes; verify page shows an activating retry state instead of the rejection banner.

---

#### [GMC-002] HIGH — Closed Discord DMs Silently Halts Trial Setup with Ambiguous Instructions
- **Journey Step:** 1. Buy / Trial
- **What the Customer Sees:**  
  User runs `/claim_trial` in Discord while their server privacy settings have Direct Messages disabled. The bot replies in Discord or web API with:
  > *"We couldn't DM you. In the Venice server, open Privacy Settings and allow Direct Messages, then try again."*
- **Does it tell them what to do?** Partially. Many users do not know how to find Discord's per-server privacy settings (right-click server icon → Privacy Settings → Direct Messages). Furthermore, if they attempt to claim again, DynamoDB may already have marked the trial issued or rate-limited them:
  > *"Too many attempts. Wait a few minutes and try again."*
- **Refund Risk:** **HIGH**. Users give up before ever installing the app, believing the bot is broken.
- **Required Fix:**  
  1. Do not require DMs for pairing! Since pairing is now handled via `https://zaeorion.com/connect` OAuth, `/claim_trial` can simply reply with an ephemeral message containing a direct link to `zaeorion.com/connect`.  
  2. If DMs fail, provide the direct connect URL directly in the slash-command interaction response.
- **Verification Test:** Trigger `/claim_trial` from a Discord account with DMs disabled; verify interaction response includes direct OAuth connect link and allows instant unlock.

---

#### [GMC-003] MEDIUM — 30-Day Hardcoded Plan Locks Out Paying Subscribers on 31-Day Months
- **Journey Step:** 1. Buy / Trial
- **What the Customer Sees:**  
  On day 31 of a 31-day month (e.g., January 31, March 31, May 31), a subscriber opens Venice. The launcher displays:
  > **Locked**  
  > *"An active Venice subscription tied to your Discord account is required. Run /purchase or open a ticket."*
- **Does it tell them what to do?** No. The customer's credit card is set to renew automatically on day 31 via Stripe, but Venice's backend hardcoded 30 days (`days: 30` in `website/src/worker.js:642`). The user is locked out for 24 hours while still paying.
- **Refund Risk:** **HIGH**. Paying subscribers locked out while seeing an active Stripe subscription will cancel and demand refunds.
- **Required Fix:**  
  In `website/src/worker.js:642`, retrieve `current_period_end` from Stripe's subscription object. In `backend/lambda_function.py`, set license expiry to `current_period_end + 86400` (providing a 24-hour grace period for recurring payment processing).
- **Verification Test:** Create a mock subscription ending 31 days out; verify entitlement expiry in DynamoDB covers the full 31 days plus grace window.

---

### Step 2: Install (Installer, Drivers, OS Permissions)

#### [GMC-004] HIGH — Microsoft Defender SmartScreen Blocks Installer Execution
- **Journey Step:** 2. Install
- **What the Customer Sees:**  
  Customer downloads `VeniceSetup-1.0.0.exe` and double-clicks it on Windows 10/11. Windows Defender SmartScreen blocks execution with a full-screen blue modal:
  > **Windows protected your PC**  
  > *Microsoft Defender SmartScreen prevented an unrecognized app from starting. Running this app might put your PC at risk.*  
  > `[Don't run]`
- **Does it tell them what to do?** No. The `Run anyway` button is hidden behind `More info`. Customers who are not technically inclined assume the software is malware, refuse to proceed, and demand an immediate refund.
- **Refund Risk:** **HIGH**. The primary drop-off barrier for Windows desktop software.
- **Required Fix:**  
  1. The owner must sign the installer using an EV (Extended Validation) code-signing certificate (documented in `installer/orion.iss:128-131` and `docs/CODE_SIGNING.md`).  
  2. In the `#setup-guide` and download page, include a prominent screenshot showing how to click `More info` → `Run anyway` until SmartScreen reputation is established.
- **Verification Test:** Verify installer PE signature using `signtool.exe verify /pa /v VeniceSetup-1.0.0.exe`.

---

#### [GMC-005] HIGH — Driver Installation Failure Halts Setup with Technical Error Codes
- **Journey Step:** 2. Install
- **What the Customer Sees:**  
  If the ViGEmBus or HidHide driver installation fails (e.g., antivirus interference, pending reboot, or conflicting virtual gamepad driver), the installer throws a critical error dialog:
  > *"The ViGEmBus virtual controller driver could not be installed. Venice needs it to send controller input to your console. Restart your PC and run Setup again. (code DRV-02)*  
  > *Setup cannot complete. Re-run this installer as administrator, or open a ticket in the Venice Discord and quote the code above."*
- **Does it tell them what to do?** It tells them to restart and re-run as administrator. However, if the root cause is a corrupt existing driver registry key, restarting repeats the same error.
- **Refund Risk:** **MEDIUM-HIGH**. User cannot complete setup; turns into an immediate support ticket.
- **Required Fix:**  
  Provide a dedicated driver cleanup script (`scripts/clean_drivers.bat`) in the support channel or bundle a fallback mode that allows Venice to operate in Remote-Play-only mode (which sends inputs over network socket rather than requiring local kernel virtual controller drivers).
- **Verification Test:** Simulate a driver return code of 1603 (fatal installation error); verify error dialog clearly displays `DRV-02` and points user to the support cleanup script.

---

### Step 3: First Run & Account Pairing

#### [GMC-006] MEDIUM — One-Time Code Expiration Displays Cryptic "Invalid or Expired" Error
- **Journey Step:** 3. First Run & Account Pairing
- **What the Customer Sees:**  
  User signs into `zaeorion.com/connect`, copies the code, gets distracted for 6 minutes, and pastes it into Venice. The launcher displays:
  > **Locked**  
  > *"That one-time code is invalid, expired, or already used. Open Connect Discord again for a fresh code."*
- **Does it tell them what to do?** Yes, but users often re-type the exact same code repeatedly, hitting local rate limiting:
  > *"Activation is rate limited locally. Wait a moment and retry."*
- **Refund Risk:** **MEDIUM**. Mild friction; resolved if the user generates a new code.
- **Required Fix:**  
  In `AuthGate.qml`, add a direct clickable button in the error card: `[Get New Code]` that opens `https://zaeorion.com/connect` directly in the default browser.
- **Verification Test:** Submit a 6-minute-old pair code; verify error card provides one-click link to fetch a fresh code.

---

#### [GMC-007] HIGH — Device Mismatch Error Lacks In-App Reset Flow
- **Journey Step:** 3. First Run & Account Pairing
- **What the Customer Sees:**  
  A customer upgrades their PC or switches from a laptop to a desktop and enters their code. The launcher displays:
  > **Locked**  
  > *"This licence is linked to a different PC. Use /hwid_reset in Discord or open a ticket."*
- **Does it tell them what to do?** It directs them to Discord `/hwid_reset`. However, users who purchased via the website without joining Discord or who do not know how slash commands work are completely stuck.
- **Refund Risk:** **HIGH**. Hardware upgrades or Windows reinstalls cause instant lockouts.
- **Required Fix:**  
  Allow `/hwid_reset` to be performed directly on `https://zaeorion.com/connect` via web button, or provide an in-app button in `AuthGate.qml`: `[Reset Device Binding]`.
- **Verification Test:** Attempt activation with a machine ID mismatch; verify UI clearly links to the web reset portal.

---

### Step 4: Setup (Capture Card & Remote Play)

#### [GMC-008] CRITICAL — Unrecognized Capture Cards (MS2109, Razer, NZXT) Result in Permanent "Shots Stay Manual"
- **Journey Step:** 4. Setup
- **What the Customer Sees:**  
  Customer plugs in a budget $15 USB capture card (enumerates as `"USB Video"` or `"USB3 Video"`) or a brand-name card like `"Razer Ripsaw HD"`, `"NZXT Signal"`, or `"ShadowCast"`.  
  - In `StreamSetupGate.qml`, the device appears in the picker, preview shows live PS5 video, and setup completes successfully.  
  - Customer enters a game and starts shooting.  
  - The status bar displays:
    > **"TIMING WARMING UP - SHOTS STAY MANUAL"**  
  - The bot **never fires a single shot**. Square must be released manually every time.
- **Does it tell them what to do?** Absolutely not! The customer has no idea why the bot is "warming up" forever. There is zero indication that the backend rejected their card's moniker name.
- **Refund Risk:** **CRITICAL**. The customer concludes Venice is a scam that does not work, resulting in an immediate refund demand.
- **Required Fix:**  
  In `remote_play_orchestrator.py:812-817` (`_latency_route_scope`), do not reject the route scope solely because `_classify_name()` failed to find a keyword! If the customer explicitly selected the device in `StreamSetupForm.qml`, trust the customer's selection. If latency calibration fails, display an explicit banner: *"Capture card latency unstable: ensure USB 3.0 port and 60 Hz output."*
- **Verification Test:** Set capture card device name to `"USB Video"`; verify route scope is accepted and shot release fires normally.

---

#### [GMC-009] HIGH — OBS or Competing App Holding Capture Card Displays Misleading "Timing Disabled" Banner
- **Journey Step:** 4. Setup
- **What the Customer Sees:**  
  Customer opens Venice while OBS Studio is running in the background. The DirectShow capture device is busy. The candidate search falls back to an available webcam (e.g. Surface Camera). The webcam light turns on (privacy scare), and the launcher displays:
  > **"TIMING DISABLED — the capture card is not on its configured DirectShow route… close anything else using the capture card (OBS…)"**
  
  The user closes OBS. **The banner remains.** Venice stays locked on the webcam and never reclaims the capture card without an application restart.
- **Does it tell them what to do?** It tells them to close OBS, but closing OBS does not fix the problem because `_reclaim_dshow_route_if_invalid` returns early when `api == 'DSHOW'`.
- **Refund Risk:** **HIGH**. Users follow the instructions, observe no change, and conclude the software is locked up.
- **Required Fix:**  
  1. Never automatically fall back to an unknown webcam when a configured capture card is busy. Display: *"Capture card busy: close OBS or other capture apps, then click Reconnect."*  
  2. Implement an active reclaim loop in `capture_card_backend.py` that re-checks the configured card index every 5 seconds when busy.
- **Verification Test:** Open mock card handle in secondary process; launch Venice; close secondary handle; verify Venice automatically reclaims the capture card within 5 seconds without restarting.

---

#### [GMC-010] HIGH — Hardcoded `D:\` Drive for Shot Telemetry Causes File System Failures on Single-Drive PCs
- **Journey Step:** 4. Setup / Background Operations
- **What the Customer Sees:**  
  Most gaming laptops and pre-built PCs have only a `C:\` drive. In `shot_records.py:233-236`, the recorder defaults to `D:\NexusVision\shot_records`.  
  On PCs without a `D:\` partition:
  - `os.makedirs` throws `FileNotFoundError` or `OSError`.
  - Every shot attempt logs errors to `orion_native.log`.
  - On PCs with a slow USB flash drive mounted as `D:\`, `os.fsync` on every shot causes 50–200 ms GUI thread hitches, inducing controller input lag and missed shots.
- **Does it tell them what to do?** No. It fails silently in the background, manifesting only as degraded timing or stuttering.
- **Refund Risk:** **HIGH**. Degraded performance on standard PC configurations.
- **Required Fix:**  
  In `shot_records.py`, resolve the default shot records directory under `%LOCALAPPDATA%\NexusVision\shot_records` (using `QStandardPaths::AppLocalDataLocation`), never a hardcoded drive letter. Disable `os.fsync` in production builds.
- **Verification Test:** Run Venice on a system with no `D:\` drive; verify shot records write cleanly to `%LOCALAPPDATA%` without throwing exceptions.

---

### Step 5: Playing (The Core Loop)

#### [GMC-011] CRITICAL — In-App UI Directly Contradicts Online Timing Expectations on Late Streaks
- **Journey Step:** 5. Playing
- **What the Customer Sees:**  
  A customer is playing NBA 2K27 online (Park, Rec, or Pro-Am). Due to normal 2K server variance, two consecutive jump shots result in a **LATE** feedback banner from the game.  
  The customer alt-tabs to Venice to see what to do.  
  On the launcher's **Shot Lead** card (`ShotLeadCard.qml:227, 233-237`), the UI prominently instructs:
  > **"Shots landing EARLY → lower it · LATE → raise it"**  
  > *"Move by 2 to 4 at a time; the sweet spot is where EARLY and LATE are equally rare."*
  
  The customer follows the UI instruction and raises Shot Lead from 50 to 56.  
  **The disaster:** As documented in `discord_launch/launch_embeds/timing_expectations.json:22`:
  > *"Late streaks are the connection, not your settings. When a shot comes back LATE, the next one is more likely to be late too… **Don't move Shot Lead after one or two lates.** It resets what Venice has learned and usually makes the next session worse."*
  
  The customer's adjustment resets Venice's learned timing model. On the next possession, the server lag spike has cleared, but Shot Lead is now set 15 ms too early. The user misses every shot horribly.
- **Does it tell them what to do?** The app tells them to do the exact opposite of what the launch guidance says! The customer believes the tool is wildly inconsistent and demands an immediate refund.
- **Refund Risk:** **CRITICAL**. Direct conflict between UI instructions and physical system reality.
- **Required Fix:**  
  Update `ShotLeadCard.qml:227`:  
  Change text to:  
  `"Tune only if shots are consistently off over 10+ shots · LATE streaks online are usually connection jitter"`  
  Add an online stability badge explaining that server latency varies shot-to-shot.
- **Verification Test:** Verify `ShotLeadCard.qml` copy matches the recommendations in `timing_expectations.json`.

---

#### [GMC-012] HIGH — 2K Meter Graphic Updates Cause Silent Blind Misfires
- **Journey Step:** 5. Playing
- **What the Customer Sees:**  
  2K pushes an in-game update that alters shot meter textures or player camera scaling. `simple_meter_reader.py` fails to recognize the meter.  
  Instead of stopping and warning the player, the engine falls back to hardcoded hold durations.  
  The player shoots: every shot is a wild miss. The launcher status remains green ("Connected"), leaving the player completely bewildered.
- **Does it tell them what to do?** No. It looks like the bot is releasing at random times.
- **Refund Risk:** **HIGH**. Occurs whenever 2K patches the game.
- **Required Fix:**  
  Implement a fail-closed visual detection policy: if the meter confidence score is below threshold for 2 consecutive shot attempts, disarm release automation and display a prominent warning banner in the overlay and UI:  
  > *"Visual meter tracking unavailable. Check 2K shot meter settings or wait for Venice update."*
- **Verification Test:** Replay frames with obscured meter; assert bot aborts release and sets user status to `Detection Unavailable`.

---

#### [GMC-013] MEDIUM — Confusing "Tip Timing Conflict" Jargon Banner
- **Journey Step:** 5. Playing
- **What the Customer Sees:**  
  If Shot Lead is adjusted above what the engine's scheduled pipeline can accommodate, `ShotLeadCard.qml:70-75` displays:
  > **Shot Lead 85 is more than Tip Timing can schedule — live tip shots cannot fire and will abort. Lower it to 72 or less, or raise/reset Tip Timing. 2 shot(s) already aborted this session.**
- **Does it tell them what to do?** It tells them to lower Shot Lead, but uses internal engineering jargon ("Tip Timing", "live tip shots", "schedule") that confuses customers.
- **Refund Risk:** **MEDIUM**. Creates anxiety that the software is unstable or improperly configured.
- **Required Fix:**  
  Simplify the copy:  
  > *"Shot Lead is set too high for your current connection speed. Lower Shot Lead to %1 or click [Auto-Fix] to prevent shot cancellation."*
- **Verification Test:** Set Shot Lead to 95; verify warning card uses plain language and provides an automatic reset button.

---

### Step 6: Things Going Wrong Mid-Session

#### [GMC-014] HIGH — PC Sleep or Hibernate Silently Disarms Automation on Resume
- **Journey Step:** 6. Mid-Session Failure
- **What the Customer Sees:**  
  Customer steps away for dinner; laptop enters sleep mode. Upon waking the PC, Venice appears open and connected.  
  The customer starts a game and attempts to shoot. **The bot does not release Square.**  
  Internally, the monotonic clock and UTC server lease timestamp diverged during sleep, causing `LeaseGate::fireAllowed()` to evaluate to `false`. The UI still says "Verified", but automation is disarmed.
- **Does it tell them what to do?** No. The customer sees a connected screen while the tool is completely non-functional.
- **Refund Risk:** **HIGH**. One of the most common user behaviors on laptops.
- **Required Fix:**  
  In `native_orion/src/OrionAppController.cpp`, listen for Windows `WM_POWERBROADCAST` (`PBT_APMRESUMEAUTOMATIC`). On system wake, immediately invalidate cached clocks, send an immediate heartbeat request to `/api/license/check`, and refresh the lease status.
- **Verification Test:** Trigger simulated power suspend/resume; verify immediate heartbeat dispatch and lease re-validation within 2 seconds of resume.

---

#### [GMC-015] MEDIUM — PS5 Dynamic IP Change Results in Indefinite Connection Probing
- **Journey Step:** 6. Mid-Session Failure
- **What the Customer Sees:**  
  The PS5 renews its DHCP lease and receives a new local IP (e.g. `192.168.1.105` → `192.168.1.112`). Venice disconnects and displays:
  > *"Connecting to console…"*
  
  The launcher continues probing the stale IP address for up to 10 minutes before timing out.
- **Does it tell them what to do?** No. It appears stuck in a connection loop.
- **Refund Risk:** **MEDIUM**.
- **Required Fix:**  
  In `RemotePlaySession.cpp`, cap stale IP connection attempts to 3 retries (15 seconds total). If unreachable, immediately fall back to UDP broadcast discovery to locate the console's new IP address.
- **Verification Test:** Change mock console IP during active session; verify Venice rediscovers and connects to the new IP within 20 seconds.

---

#### [GMC-016] HIGH — Disk Full Condition Causes Complete Controller Input Freeze
- **Journey Step:** 6. Mid-Session Failure
- **What the Customer Sees:**  
  Customer has low disk space (<500 MB free). Venice's logging subsystem (`OrderedFileLogSink.cpp:78-83`) fills its 8 MB in-memory buffer. Once full, `enqueue()` blocks the GUI thread.  
  Because the GUI thread carries `pollPhysicalController()`, **all controller inputs stop working**. The player's character in NBA 2K freezes in place or runs out of bounds. The game is ruined.
- **Does it tell them what to do?** No. The app hangs and the controller disconnects.
- **Refund Risk:** **HIGH**. Destructive in-game failure.
- **Required Fix:**  
  In `OrderedFileLogSink.cpp`, never block `enqueue()` on disk full! Drop verbose telemetry lines, preserve a small ring buffer for critical audit lines, and surface an in-app banner: *"Disk space critically low (<1 GB free). Logging suspended."*
- **Verification Test:** Simulate a zero-byte disk write; verify controller input polling continues at 4 ms without blocking the GUI thread.

---

### Step 7: Getting Help (Support, Logs, Diagnostics)

#### [GMC-017] MEDIUM — Absence of One-Click Support Diagnostic Bundle Exposes Customer LAN IPs
- **Journey Step:** 7. Getting Help
- **What the Customer Sees:**  
  Customer encounters an issue and joins Discord for help. Staff ask for "the log file".  
  The customer navigates to `%LOCALAPPDATA%\NexusVision\Orion Native\logs\` and uploads `orion_native.log`.  
  The log contains unredacted local IPv4 addresses, subnet masks, default gateways, and MAC hashes (`RemotePlaySession.cpp:1420-1450`), exposing private network details in public channels.
- **Does it tell them what to do?** No guidance is provided on sanitizing logs.
- **Refund Risk:** **LOW-MEDIUM**. Privacy concern and high support friction.
- **Required Fix:**  
  Add an **"Export Support Log"** button in Venice settings. The button creates a sanitized `.zip` containing `orion_user.log` and the last 1,000 lines of `orion_native.log` with IP addresses masked (`192.168.X.X`) and sensitive tokens stripped.
- **Verification Test:** Export support bundle; verify private IPs and machine GUIDs are redacted.

---

## Part C — Launch-Day Readiness & Blue-Team Runbooks

The existing repository documentation covers deep technical audits (`docs/`), but lacks short, actionable, operational runbooks for launch-day emergencies. The following blue-team operational runbooks are established for the launch team.

---

### Runbook 1: Broken Update Rollback (Emergency Client Revert)

**Trigger:** A pushed client update (`v1.0.1`) crashes on startup, introduces severe input lag, or breaks controller mapping across multiple users.  
**Objective:** Immediately roll all clients back to the known-good version (`v1.0.0`) without requiring manual uninstalls.

#### Operational Steps:
1. **Prepare Rollback Manifest:**  
   Locate the previous stable release artifact package and its signed hash from S3/R2 storage.  
   Create an updated `update_manifest.json` pointing back to `v1.0.0`:
   ```json
   {
     "version": "1.0.0",
     "channel": "stable",
     "mandatory": true,
     "artifact_url": "https://releases.zaeorion.com/Venice-1.0.0.zip",
     "sha256": "<KNOWN_GOOD_SHA256_HEX>",
     "public_key_id": "orion-ed25519-v1",
     "release_notes": "Emergency rollback to restore launcher stability."
   }
   ```
2. **Sign the Manifest:**  
   Execute the offline update signing tool with the server-side Ed25519 private key:
   ```bash
   python tools/sign_update_manifest.py --manifest update_manifest.json --key-ssm /orion/prod/ed25519_private_key
   ```
3. **Publish to S3 / Cloudflare CDN:**
   ```bash
   aws s3 cp update_manifest.json.sig s3://orion-releases/stable/manifest.sig
   aws s3 cp update_manifest.json s3://orion-releases/stable/manifest.json
   ```
4. **Purge Cloudflare Cache:**
   ```bash
   curl -X POST "https://api.cloudflare.com/client/v4/zones/$CF_ZONE/purge_cache" \
     -H "Authorization: Bearer $CF_TOKEN" \
     -d '{"files":["https://zaeorion.com/api/update"]}'
   ```
5. **Verify:** Run a test client instance of `v1.0.1`; verify it receives the mandatory update signal and successfully downgrades to `v1.0.0`.

---

### Runbook 2: 2K Meter-Breaking Patch (Emergency Pause & Messaging)

**Trigger:** 2K releases a patch that modifies shot meter graphics or camera geometry, causing widespread misses.  
**Objective:** Prevent users from losing games and blaming Venice; communicate status instantly.

#### Operational Steps:
1. **Broadcast In-App MOTD Alert:**  
   Call the backend admin endpoint to display an immediate warning banner across all active launchers:
   ```bash
   python tools/orion_admin.py --action motd_set \
     --message "NBA 2K27 game patch detected. Shot timing is temporarily paused while we update meter models. Do not adjust your Shot Lead." \
     --severity warning --ttl 14400
   ```
2. **Engage Temporary Automation Disarm (Optional):**  
   If the patch causes erratic controller holding, engage the server-side timing disarm via DynamoDB config:
   ```bash
   python tools/orion_admin.py --action config_set --key "meter_detection_enabled" --value "false"
   ```
3. **Post Discord Announcement:**  
   Post to `#announcements`:
   > **⚠️ NBA 2K27 Game Patch In Progress**  
   > *2K just pushed an update that alters in-game visual elements. We have paused release automation while our vision models are updated. Please do not change your Shot Lead settings. A patch will be deployed shortly.*
4. **Capture Replay Frames & Re-Train:**  
   Obtain raw gameplay clips from the owner rig using `.\run_orion.local.ps1 -Framedump`, update color thresholds in `simple_meter_reader.py`, run `pytest tests/test_meter_reader.py`, and package an update.

---

### Runbook 3: License Server / Lambda Outage Mitigation

**Trigger:** AWS Lambda, DynamoDB, or API Gateway experiences an outage; clients receive 502/503/504 errors on `/api/license/check`.  
**Objective:** Keep active gaming sessions alive; prevent paying customers from being kicked mid-match.

#### Operational Steps:
1. **Verify Client Lease Grace Enforcement:**  
   Ensure that `LeaseGate.cpp` fails soft during transport errors. Clients with valid leases will continue running for up to 15 minutes (`kMaxStalenessMs = 900,000`).
2. **Cloudflare Worker Failover / Mock Mode:**  
   If AWS Lambda is experiencing extended downtime, enable Cloudflare Worker synthetic heartbeat response:
   In Cloudflare Worker (`website/src/worker.js`), enable the fallback route:
   ```javascript
   // If origin responds with 5xx, return cached lease extension for existing tokens
   if (originResponse.status >= 500) {
     return json({ ok: true, status: "active", lease_duration: 3600, message: "Server maintenance mode" });
   }
   ```
3. **Post Incident Notice:**  
   Notify Discord support that billing and new pairings are queued while existing clients remain functional.

---

### Runbook 4: Stripe Webhook Failure & Manual Entitlement Reconcile

**Trigger:** Cloudflare Worker drops Stripe webhooks (e.g. invalid signature, timeout, or Lambda concurrency limits), leaving paying customers unentitled.  
**Objective:** Reconcile paid Stripe charges with DynamoDB customer records within 5 minutes.

#### Operational Steps:
1. **Identify Unreconciled Payments:**  
   Run the Stripe reconciliation script against the last 24 hours of checkout sessions:
   ```bash
   python tools/reconcile_stripe.py --hours 24 --unprovisioned-only
   ```
2. **Manual Customer Provisioning via Discord Bot:**  
   Staff can immediately provision the affected Discord user using the administrative slash command:
   ```
   /deliver user:@Customer plan:month days:31 reason:"Stripe webhook lag resolution"
   ```
3. **Automated Resend from Stripe Dashboard:**  
   In Stripe Dashboard → Developers → Webhooks → `https://zaeorion.com/api/stripe/webhook` → Select failed event → Click **"Resend"**.
4. **Verify Customer Pairing:**  
   Confirm customer can immediately access `https://zaeorion.com/connect` and retrieve a valid `PAIR-XXXX` code.

---

### Runbook 5: Leaked Secret / Key Rotation Procedure

**Trigger:** An AWS secret, Stripe webhook secret, or Ed25519 signing key is accidentally committed or exposed.  
**Objective:** Rotate compromised credentials without dropping active customer sessions.

#### Operational Steps:
1. **Leased Signing Key Rotation (`LEASE_SIGNING_KEY_SSM`):**  
   - Generate a new 32-byte Ed25519 or HMAC secret.
   - Update AWS SSM parameter `/orion/prod/lease_signing_key_secondary` with the old key, and `/orion/prod/lease_signing_key` with the new key (dual-signing / accept-either window).
   - After 15 minutes (all client leases expired and refreshed), delete the old secondary key.
2. **Stripe Webhook Secret Rotation (`STRIPE_WEBHOOK_SECRET`):**  
   - In Stripe Dashboard, click "Add secret" under Webhook endpoints (Stripe supports 2 simultaneous signing secrets).
   - Update Cloudflare Worker environment secret `STRIPE_WEBHOOK_SECRET` via Wrangler:
     ```bash
     wrangler secret put STRIPE_WEBHOOK_SECRET
     ```
   - Delete the old secret in Stripe Dashboard after deployment.
3. **Admin Secret Token Rotation:**  
   - Rotate `/orion/prod/admin_token` in AWS SSM.
   - Immediately update Discord bot environment and staff admin credentials.

---

### Runbook 6: First-Hour "It Doesn't Work" Support Triage Checklist

**Trigger:** Beta launch doors open; Discord `#support` channel receives a wave of tickets.  
**Objective:** Rapid triage and categorization within 60 seconds per ticket.

```
                                  [ CUSTOMER TICKET ]
                                           |
                   +-----------------------+-----------------------+
                   |                                               |
           [ Auth / Unlock ]                               [ In-Game / Gameplay ]
                   |                                               |
         Is code saying EXPIRED?                        Is preview black/frozen?
         ├── YES -> Direct to zaeorion.com/connect      ├── YES -> HDCP on PS5 is ON.
         |          (Fresh code, 5 min window)          |          Settings > System > HDMI > HDCP OFF.
         |                                              |
         Is error DEVICE MISMATCH?                      Is status "TIMING WARMING UP"?
         ├── YES -> Run /hwid_reset in Discord          ├── YES -> Unsupported card name or USB 2.0.
         |          (First 3 are free)                  |          Set to 1080p60 on USB 3.0 port.
         |                                              |
         Did user JUST PAY on Stripe?                   Are shots missing LATE in streaks?
         └── YES -> Webhook queue latency.              └── YES -> Do NOT touch Shot Lead!
                    Verify receipt email, wait 60s,                Explain 2K server latency leg.
                    or run /deliver manually.                      Take a beat between shots.
```

#### Standard Support Macros for Discord:
- **Macro 1 (HDCP Black Screen):**  
  > *"Black screen in Live Capture is almost always HDCP! On your PS5, go to **Settings → System → HDMI → Enable HDCP** and toggle it **OFF**. Then click Refresh in Venice."*
- **Macro 2 (Pair Code Expired):**  
  > *"Connection codes expire after 5 minutes for security. Open https://zaeorion.com/connect in your browser, sign in with this Discord account, and click 'Open Venice' or paste the fresh code."*
- **Macro 3 (Late Streaks Online):**  
  > *"2K's online servers have shot-to-shot latency variance. When you see two LATEs in a row, do not change your Shot Lead slider! That is connection jitter. Keep your settings consistent and take a breath before shooting again."*

---

## Top 10 Fixes Ranked by Refund Risk

| Rank | Issue ID | Component | Title | Refund Risk Justification |
| :---: | :--- | :--- | :--- | :--- |
| **1** | **GMC-001** | `website/src/worker.js` | **Post-Stripe Checkout Race Condition** | Paying customers clicking "Connect" before webhook completes are told *"Needs active subscription"*. Triggers immediate scam panic, bank disputes, and chargebacks. |
| **2** | **GMC-008** | `remote_play_orchestrator.py` | **Unrecognized Capture Cards Locked in Manual Mode** | Budget USB dongles ($15 MS2109) and cards without hint words show video but never fire shots (`TIMING WARMING UP`). Bot appears 100% dead. |
| **3** | **GMC-011** | `native_orion/qml/` | **Shot Lead Card In-App Copy Prompts Late-Streak Chasing** | UI tells users *"LATE → raise it"*, which destroys calibrated timing during transient 2K server jitter. Contradicts official launch expectations. |
| **4** | **GMC-004** | `installer/orion.iss` | **SmartScreen Blue Screen Blocks Installer** | Non-technical customers abandon setup when Windows Defender blocks the unrecognized binary, assuming it is malware. |
| **5** | **GMC-010** | `shot_records.py` | **Hardcoded `D:\` Drive for Shot Telemetry** | Fails or causes 100 ms `fsync` hitches on customer PCs without a `D:\` drive or with slow USB drives, inducing in-game shot lag. |
| **6** | **GMC-014** | `native_orion/src/` | **PC Sleep / Hibernate Disarms Fire Lease** | Laptop sleep diverts monotonic clock; automation is silently disarmed without UI warning upon waking up. |
| **7** | **GMC-009** | `capture_card_backend.py` | **OBS Capture Card Lock Causes Permanent Webcam Jump** | Closing OBS does not reclaim capture card without app restart; webcam light turns on causing user privacy scare. |
| **8** | **GMC-012** | `AutomationEngine.cpp` | **Silent Blind Misfires on Game Visual Update** | Game meter texture changes cause bot to fire blind on hardcoded hold times with 100% miss rate and no clear in-app explanation. |
| **9** | **GMC-003** | `website/src/worker.js` | **30-Day Plan Lockout on 31-Day Months** | Hardcoded 30-day duration locks out paying monthly subscribers on day 31 before Stripe renews. |
| **10** | **GMC-016** | `OrderedFileLogSink.cpp` | **Disk Full Freezes Controller Input Mid-Game** | Log sink blocking on disk full stops the GUI thread, freezing controller inputs and ruining live competitive matches. |

---
*Report completed and filed to `docs/redteam/2026-09-22-launch/RED_TEAM_REPORT.gemini.customer.md`.*
