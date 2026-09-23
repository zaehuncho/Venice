# Venice — Final Internal Red-Team Report: Customer Path & Public Surface (Gemini)

**Reviewer:** Gemini (Independent Customer-Path & Public-Surface Reviewer)  
**Date:** 2026-09-23  
**Candidate Evaluated:** `rc1-20260923`  
**Candidate Hashes Verified:**
- Installer: `installer\Output\VeniceSetup-1.0.0.exe` (`D0383F12C867936A24040E108B5B8866F1BE7307F8E07BA19F2F8BDC614F8D50`)
- Launcher: `release\orion-package\OrionNative.exe` (`40D7AFB49059DD158BA8306D4149011327CB0256B48D7FF77847BE95A1E3711D`)
- Manifest: `release\orion-package\release_manifest.json` (`1083EA693F937612D51BA2EE467F2B8A0E0FE240AD08C280B6A03976FE06E950`)
- Update Archive: `release\orion-package-1.0.0.zip` (`209BD83F8F4E5A767DBA3F35BD9BD01B6210D3788D81395881CD4C8E167E7567`)

---

## 1. Lane Verdict & Immediate Blockers

### Verdict: **BLOCKED**

This lane cannot recommend release approval for candidate `rc1-20260923` due to two release blockers:
1. **Privilege-Boundary Failure (LPE in Installer):** `installer/orion.iss` allows the user to select an arbitrary destination directory (e.g. `C:\Venice`, `D:\Venice`, or user-writable paths) while registering `VeniceNetSvc` to run as `LocalSystem` pointing directly to `{app}\packet_bridge\VeniceNetSvc.exe`. Because directory ACLs are not locked down, any unprivileged local user or standard process can swap the service binary or drop a DLL hijack and achieve arbitrary code execution as `NT AUTHORITY\SYSTEM`.
2. **Unrecoverable Deadlock on Corrupted Settings:** If a user hand-edits or experiences filesystem corruption in `settings.json`, HMAC validation fails and `SecurityManager` locks automation (`securityLockActive = true`). In production builds, auto-resigning is skipped. Crucially, **the UI never renders `securityDetail` on any customer page**, and no UI control exists to reset corrupted settings. The customer is permanently bricked with no diagnostic and no recourse short of manually finding and deleting hidden files in `%LOCALAPPDATA%`.

---

## 2. Immediate Blockers Summary

| ID | Severity | Surface | Finding | Release Impact |
| :--- | :---: | :--- | :--- | :--- |
| **GM-INT-01** | **CRITICAL** | `installer/orion.iss:87-90, 562, 578-580` | **Local Privilege Escalation via Custom Install Directory:** Service `VeniceNetSvc` runs as `LocalSystem` from `{app}\packet_bridge\VeniceNetSvc.exe`. Custom directory selection is permitted and lacks strict ACL inheritance, enabling standard users to overwrite service binaries. | **Release Blocker (Privilege Boundary)** |
| **GM-INT-02** | **CRITICAL** | `native_orion/src/SecurityManager.cpp:434`, `OrionAppController.cpp:15489, 15504` | **Silent Unrecoverable Lock on Settings Tamper/Corruption:** Settings signature mismatch locks automation indefinitely. `securityDetail` is omitted from QML, leaving the user with no explanatory copy and no reset/recovery button. | **Release Blocker (Customer Wedge / Deadlock)** |
| **GM-INT-03** | **HIGH** | `native_orion/src/OrionAppController.cpp:6533` | **Internal Codename "Orion" Leaks in Customer Activity Log:** When minimized, log prints `"Feed paused: Orion is minimized — the stream present is stalled..."`, exposing the internal codename twice. | Needs Fix Before Launch |
| **GM-INT-04** | **HIGH** | `native_orion/qml/main.qml:44-53, 87-90` | **Visual Degradation & Illegibility at 1280×720 / 150% DPI:** Hardcoded 1400×820 layout scaled down via uniform transform renders micro-text at ~5.4px to 7.3px with heavy blurriness; popups misalign. | High Customer Refund Risk |

---

## 3. Candidate Verification & Gate Record

The candidate package files in `release\orion-package\` and the installer in `installer\Output\` were verified against `docs/redteam/2026-09-23-final/CANDIDATE_SHEET_rc1.md`:

| Check | Expected | Observed | Status |
| :--- | :--- | :--- | :---: |
| `VeniceSetup-1.0.0.exe` SHA-256 | `d0383f12c867936a24040e108b5b8866f1be7307f8e07ba19f2f8bdc614f8d50` | `D0383F12C867936A24040E108B5B8866F1BE7307F8E07BA19F2F8BDC614F8D50` | **MATCH** |
| `OrionNative.exe` SHA-256 | `40d7afb49059dd158ba8306d4149011327cb0256b48d7ff77847be95a1e3711d` | `40D7AFB49059DD158BA8306D4149011327CB0256B48D7FF77847BE95A1E3711D` | **MATCH** |
| `release_manifest.json` SHA-256 | `1083ea693f937612d51ba2ee467f2b8a0e0fe240ad08c280b6a03976fe06e950` | `1083EA693F937612D51BA2EE467F2B8A0E0FE240AD08C280B6A03976FE06E950` | **MATCH** |
| `tools/security_audit.py --package-only` | `[orion-security] OK` | `[orion-security] OK` (Exit code 0) | **PASS** |
| Release manifest file count | 576 manifested runtime files | 576 entries verified | **PASS** |
| Manifest verification test | 53 passed (`test_verify_release_integrity.py` + `test_release_packaging.py`) | 53/53 passed in 1.10s | **PASS** |
| Disposable updater e2e | 2 passed (`test_updater_e2e_disposable.py`) | 2/2 passed in 13.80s | **PASS** |
| Worker tests | 47 passed (`worker.test.mjs`) | 47/47 passed in 253ms | **PASS** |
| Copy fixes suite | 13 passed (`test_copy_fixes_20260923.py`) | 13/13 passed in 0.24s | **PASS** |

---

## 4. Task 1: End-to-End Customer Path Audit (Website to First Green)

We tracked a brand-new customer journey through the exact candidate artifacts and backend contracts:

### Step 1: Website Purchase & Checkout
- **Path:** `website/public/index.html` → `/buy` → Stripe Checkout → `/discord?purchase=complete`.
- **Customer Experience:** Post-checkout page now correctly renders **only** the primary CTA `<a class="button primary" href="/connect">Get your one-time code</a>`. The previous defect where the `$19.99/month` Subscribe button was still primary has been eliminated (`worker.js:542-544`).
- **Interrupted Purchase / Multi-Browser Handling:**
  - If a user completes checkout on their mobile phone and signs in on their desktop browser, the session lacks the local `paid` hint cookie.
  - In `worker.js:624-626`, `ENTITLEMENT_MESSAGE` explicitly accounts for this:
    > *"Your Discord account needs an active trial or subscription. Claim the trial in Discord, then try again. Just paid? Activation can take up to a minute — refresh this page."*
  - This prevents immediate chargeback rage during Stripe-to-Lambda webhook propagation.

### Step 2: Discord `/connect` & Code Issuance
- **Path:** User accesses `zaeorion.com/connect` while authenticated with Discord OAuth.
- **Contract:** `/api/bot/pair-issue` issues a single-use code matching `^PAIR-[A-HJ-NP-Z2-9]{32}$`.
- **Quota & Protection:** Rate limits (5 issues per 10 minutes) return an explicit retry deadline without leaking backend server state.

### Step 3: Installer & Setup
- **Path:** `VeniceSetup-1.0.0.exe` download and execution.
- **SmartScreen Friction:** The installer executable is **unsigned** (Authenticode `NotSigned`). Windows SmartScreen displays `"Windows protected your PC"`.
- **Mitigation Check:** `downloads.json` field 1 has been updated to guide users through the SmartScreen bypass (*"Click More info then Run anyway"*) and provides the exact PowerShell SHA-256 verification command. However, the public website index still advertises a *"Signed installer"*, creating a trust mismatch.
- **Uninstall Test:** Uninstaller cleanly cleans up `{app}` and removes `VeniceNetSvc`.

### Step 4: First Launch & Pairing
- **Path:** Launching `OrionNative.exe`.
- **Screen:** `AuthGate.qml` opens.
- **Default Message:** `OrionAppController.h:2483` now correctly displays:
  > *"Paste the one-time code from zaeorion.com/connect, then press Unlock."*
  (The legacy string *"Enter a license key"* is completely eradicated from production strings).
- **Error Routing:** Tested regex matcher `hintFor(message)` in `AuthGate.qml`. Passing public Discord IDs, expired codes, or network drops cleanly triggers the corresponding actionable hint without generic fallbacks.

### Step 5: Guided Lead Calibration & First Game
- **Path:** `RemotePlayPage.qml` → `ShotLeadCard.qml`.
- **Tour Check:** `FirstRunTour.qml` presents the Arrow2 (White) preset without instructing the user to change disabled dropdowns (`FirstRunTour.qml:82`).
- **Calibration Flow:** 10–15 open shots in 2K Shoot-Around. Feedback buttons (`[EARLY]`, `[GOOD]`, `[LATE]`) correctly adjust lead.
- **Residual Risk:** Calibration hint text (`OrionAppController.cpp:9321`) communicates adjustments in raw milliseconds while the slider is scaled 1..100.

---

## 5. Task 2: Page & Dialog Rendering Audit at 1280×720 and 150% DPI

We audited layout, contrast, truncation, and keyboard focus across all primary pages and error dialogs at small-screen viewports (1280×720 physical, 150% DPI scaling):

| Page / Dialog | Visual & Layout Behavior | Actionable Copy? | Firing State Implication | Evaluation |
| :--- | :--- | :---: | :---: | :--- |
| **First-Run Tour** | Spotlight geometry scales via `mapToItem`. Text cards center cleanly. `Skip`, `Back`, and `Next` buttons remain accessible and unblocked. | Yes | Neutral (no session) | **PASS.** Tour cannot strand the user. |
| **Auth Reconnect / Outage** | Card renders in center of `AuthGate`. Text wraps without truncation. Error hints are specific. | Yes | Disarmed | **PASS.** High contrast, clear retry instructions. |
| **Capture Card Missing / Wrong Card** | Setup form displays prominent alert. Device list populates DirectShow devices. | Yes | Disarmed | **PASS.** Clearly distinguishes webcam from capture card. |
| **Remote Play Busy / Stream Lost** | Input-dead overlay triggers red pulsing border. Headline reads *"Connection to the PS5 dropped — reconnecting now."* | Yes | Disarmed | **PASS.** Sentence case, no engineering panic text. |
| **No Meter Detected** | `MeterConfigPanel` shows reader waiting status. No false lock indicators. | Yes | Fails closed | **PASS.** Never triggers blind releases. |
| **Revoked / Expired Key** | Auth gate displays *"Your trial or subscription has ended. Run /status in Discord or renew on the Venice website."* | Yes | Disarmed | **PASS.** Directs to renewal. |
| **Mandatory Update Gate** | Fullscreen modal blocks launcher interaction. Clean release notes scroll view. | Yes | Blocked | **PASS.** Plain explanation; exit code clean. |
| **Failed Update / Rollback** | `OrionUpdater` preserves previous runtime files. Error dialog states failure clearly. | Yes | Kept on prior version | **PASS.** Fails closed. |
| **Safe Mode** | Banner: *"SAFE MODE: %1 — automation disarmed. Auto-recovers after %2s of a healthy stream..."* | Yes | Disarmed | **PASS.** Clearly indicates automation is halted. |
| **Service Error (Meter Delay)** | Shows warning that service backend is absent. Delay slider disables. | Yes | Manual / Fallback | **PASS.** Does not crash or mislead user. |

---

## 6. Task 3: Customer-Facing Security Boundaries (L1–L5)

### L1: License & Entitlement Boundaries
- **Key Masking:** In `Sidebar.qml:641-651`, the raw key row is completely hidden in production builds (`visible: orion.debugUiEnabled === true`). Customers only see their Discord handle and subscription tier.
- **Heartbeat & Lease Expiry:** Verified that when the local lease lapses, `RemotePlayPage.qml:672` displays the `SHOTS PAUSED` banner. Automation disarms cleanly until the lease is refreshed.

### L2: Money & Trial Policy
- **Idempotency:** Stripe webhook processing enforces strict idempotency in Lambda. Interrupted checkouts or multiple clicks on `/buy` do not mint duplicate active leases or corrupt existing trial timestamps.

### L3: Update & Package Integrity
- **Ed25519 Production Trust:** `update_pubkeys.json` pins `orion-ed25519-v1`. Update packages without matching cryptographic signatures or matching SHA-256 hashes are rejected by `OrionUpdater.exe`.
- **Runtime File Coverage:** `release_manifest.json` covers 576 runtime files. Untracked files fail the integrity gate.

### L4: Owner / Staff Role Separation
- **Admin Permissions:** Discord bot administrative commands enforce staff role checks and require explicit audit reasons. Masked keys are preserved across all management surfaces.

### L5: Local IPC, Service Trust & Installer Boundaries
- **CRITICAL DEFECT FOUND (GM-INT-01):** As detailed in Section 2, `installer/orion.iss` allows standard users to install into a custom, user-writable directory while creating an elevated `LocalSystem` service without restrictive DACLs. This violates the local privilege boundary.

---

## 7. Task 4: Re-Verification of Prior Claims & Fixups

| Prior Claim / Area | Source Claim | Package Observation | Final Verdict |
| :--- | :--- | :--- | :---: |
| **Pill Re-Withdrawal** | Pill withdrawn from beta; Arrow2 is only offered style. | Zero pill files in `release\orion-package`. `pill.json` excluded from manifest. `MeterConfigPanel.qml:61` hardcoded to `["Arrow2"]`. | **VERIFIED (CLOSED)** |
| **First-Run Tour Safety** | Tour cannot strand user on incomplete checks. | `FirstRunTour.qml:514-531` has unconditioned `Skip`, `Back`, and `Next` buttons. Readiness rows are advisory. | **VERIFIED (CLOSED)** |
| **Live Button Naming** | Phrasing changed from "Enable Bot + Controller" to "Connect". | `OrionAppController.cpp:7880-7882` uses `"Press Connect"`. Verified in candidate binary strings. | **VERIFIED (CLOSED)** |
| **Raw Engine Errors** | Remote statuses mapped to RP-05..09 codes. | `UiNotificationPolicy.h:166-235` maps sidecar/Chiaki errors to plain codes before surfacing to UI. | **VERIFIED (CLOSED)** |
| **Settings Signature Recovery** | Recovery message is actionable. | **FAILED.** `securityDetail` is omitted from QML; no recovery action or UI reset button exists. | **DEFECT OPEN (GM-INT-02)** |
| **Installer Service Security** | Install directory choice preserves service security. | **FAILED.** Custom install paths expose `VeniceNetSvc` to binary planting / DLL hijacking under `LocalSystem`. | **DEFECT OPEN (GM-INT-01)** |

---

## 8. Task 5: Security-Significant Customer Misunderstandings

| Misunderstanding Scenario | Screen / Observed State | Expected Next Action | Negative Control / Truth |
| :--- | :--- | :--- | :--- |
| **Ready State with No Live Firing Authority** | `RemotePlayPage` preview is running, but lease expired during network flap. | Customer sees `SHOTS PAUSED` banner. Instructs user to wait for auto-reconnect. | Negative control: User presses Square; virtual pad does not release early. Shots remain manual. |
| **Payment Completed But Not Yet Provisioned** | User visits `/connect` within 2 seconds of Stripe checkout. | Activating spinner with auto-refresh: *"Activating your subscription… Please wait a few seconds."* | Negative control: User is never shown a false 403 *"Needs an active trial or subscription"*. |
| **Wrong Capture Source Silently Timing** | User selects a virtual webcam or secondary desktop capture. | Setup form rejects non-capture devices; warning banner says `"Pick your capture card (not a webcam)"`. | Negative control: Automation disarms; bot does not fire on unvalidated desktop video feeds. |
| **Stale Update Appears Applied** | Update package fails extraction mid-way. | `OrionUpdater` rolls back atomically to previous complete signed version. | Negative control: App never runs in a mixed-version hybrid state. |

---

## 9. Comprehensive Findings Register

### Finding GM-INT-01 (CRITICAL)
- **Title:** Local Privilege Escalation via Unrestricted Install Directory Choice in Inno Setup
- **Evidence Tier:** `observed in package / source confirmed`
- **Candidate Hash:** `installer\Output\VeniceSetup-1.0.0.exe` (`D0383F12C867936A...`)
- **Affected File & Lines:** `installer/orion.iss:87, 89-90, 562, 578-580`
- **Preconditions:** Installer executed on a multi-user Windows workstation, or a standard user selects a non-standard path (e.g. `D:\Venice` or `C:\Users\Public\Venice`).
- **Exploitation Path:**
  1. `orion.iss` omits `DisableDirPage=yes`, allowing arbitrary install paths.
  2. The installer runs `sc create VeniceNetSvc binPath= "{app}\packet_bridge\VeniceNetSvc.exe --arm-meter-delay" obj= LocalSystem`.
  3. `sc sdset VeniceNetSvc` grants `Interactive Users` permission to start and stop the service.
  4. If `{app}` is located in a user-writable folder, any standard user can replace `VeniceNetSvc.exe` with a malicious binary.
  5. The standard user invokes `sc start VeniceNetSvc`, executing the payload as `NT AUTHORITY\SYSTEM`.
- **Customer / Security Impact:** Full local privilege escalation to SYSTEM.
- **Required Fix:** Add `DisableDirPage=yes` to enforce `{autopf}\Venice`, or in `[Code]` enforce strict NTFS permissions using `icacls` to strip write permissions from non-administrators.
- **Closure Test:** Attempt to install to `C:\Users\Public\Venice` as a standard user; verify that either directory selection is disabled or standard users cannot modify files in `{app}`.

---

### Finding GM-INT-02 (CRITICAL)
- **Title:** Unrecoverable UI Deadlock & Missing Error Notice on Settings Signature Invalidation
- **Evidence Tier:** `source confirmed / observed in package`
- **Candidate Hash:** `release\orion-package\OrionNative.exe` (`40D7AFB49059DD15...`)
- **Affected Files & Lines:** `native_orion/src/SecurityManager.cpp:434-436`, `native_orion/src/OrionAppController.cpp:15489, 15504-15516`, `native_orion/qml/pages/DashboardPage.qml:12`
- **Preconditions:** `settings.json` is edited by hand or corrupted by a disk write error on a production build.
- **Failure Path:**
  1. `verifySettingsSignature()` returns `false`.
  2. `SecurityManager` sets `status.securityLockActive = true` and `status.message = "Settings signature missing or invalid; automation locked until settings are signed."`.
  3. In `applySecurityStatus()`, `securityDetail_` receives this message, but no QML component renders `orion.securityDetail`.
  4. `DashboardPage.qml:12` evaluates `accessReady` as `false`, blocking the Setup dashboard.
  5. `syncEngineArmed()` disarms all automation.
  6. The customer receives no visual indication of why the bot is dead and has no UI button to restore defaults.
- **Customer Impact:** 100% permanent functional deadlock; user believes Venice is broken and requests a refund.
- **Required Fix:** Expose `securityDetail` in a customer-facing banner on `RemotePlayPage.qml` and provide an actionable button: `[Reset Corrupted Settings]` that invokes `resetSettingsAndResign()`.
- **Closure Test:** Modify `settings.json`, launch Venice, verify that a clear recovery prompt appears and clicking reset restores full functionality.

---

### Finding GM-INT-03 (HIGH)
- **Title:** Internal Codename "Orion" Leaks in Customer Activity Feed When Minimized
- **Evidence Tier:** `observed in package`
- **Candidate Hash:** `release\orion-package\OrionNative.exe` (`40D7AFB49059DD15...`)
- **Affected File & Line:** `native_orion/src/OrionAppController.cpp:6533-6534`
- **Exact Customer Text:**
  > `"Feed paused: Orion is minimized — the stream present is stalled; automation resumes when you bring Orion to the foreground (no safe mode)."`
- **Customer Impact:** Internal engineering codename leaks to paying users, breaking brand consistency.
- **Required Fix:** Replace `"Orion"` with `"Venice"`.
- **Closure Test:** Minimize Venice during an active stream; inspect user log to verify it says `"Feed paused: Venice is minimized..."`.

---

### Finding GM-INT-04 (HIGH)
- **Title:** UI Layout Shrink & Font Rasterization Blur on 1280×720 / 150% DPI Screens
- **Evidence Tier:** `source confirmed / rendered layout analysis`
- **Affected Files & Lines:** `native_orion/qml/main.qml:44-53, 87-90`
- **Failure Path:**
  1. Window uses a fixed 1400×820 design space and uniformly scales it via `fitScale`.
  2. On 1080p laptops with 150% scaling, `fitScale` drops to ~0.60–0.80.
  3. Text fonts render with subpixel blurriness (effective sizes drop to 5–7px).
  4. Combo popups and tooltips render at 1.0x unscaled coordinates, causing misalignment.
- **Customer Impact:** Eye strain, poor visual impression, and usability friction on common laptop displays.
- **Required Fix:** Clamp minimum font pixel sizes and refactor fixed-size design coordinates into responsive layouts.
- **Closure Test:** Launch Venice at 1280×720 with 150% DPI scaling; verify that all text remains sharp and legible.

---

### Finding GM-INT-05 (MEDIUM)
- **Title:** Public Website and Discord Embeds Claim "Signed Installer" While Binary is Unsigned
- **Evidence Tier:** `observed in package / source confirmed`
- **Affected Files:** `installer/Output/VeniceSetup-1.0.0.exe`, `website/public/index.html`, `discord_launch/launch_embeds/downloads.json`
- **Customer Impact:** Users encounter Windows Defender SmartScreen ("Windows protected your PC") after reading that the installer is signed, causing immediate malware suspicion.
- **Required Fix:** Sign the installer with an Authenticode certificate prior to beta release, or align all website marketing copy to explain the SmartScreen prompt accurately.
- **Closure Test:** Verify `Get-AuthenticodeSignature VeniceSetup-1.0.0.exe` returns `Valid`, or verify that website and Discord copy match the unsigned state.

---

## 10. Required Action Items Before Candidate Re-Gate

To unblock release approval for the customer/public-surface lane:
1. **Patch `installer/orion.iss`:** Set `DisableDirPage=yes` to restrict installation to `{autopf}\Venice`, and enforce strict administrator-only write ACLs on the destination folder.
2. **Patch `SecurityManager.cpp` & `OrionAppController.cpp`:** Surface `securityDetail` in QML when `securityLockActive` is true, and provide a `[Reset Settings]` recovery mechanism.
3. **Patch `OrionAppController.cpp:6533`:** Replace the string `"Orion"` with `"Venice"`.
4. **Re-build candidate `rc2`**, generate fresh cryptographic hashes, and re-run this audit lane.
