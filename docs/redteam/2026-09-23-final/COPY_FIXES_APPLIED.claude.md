# Copy fixes applied (Claude, 2026-09-23)

Source: `GEMINI_VERIFIED.claude.md`, REAL rows only. I checked each row against the current tree before editing it. All of them were still true.

This was light work only. I built no native code, launched nothing, killed no process, made no commits and deployed nothing. I did not touch `simple_meter_reader.py`, `meter_locator_cv.py`, `ShotLeadCard.qml`, `RhythmCard.qml`, or the tempo/shotLead tests that the main session owns. Line numbers are from the working tree after these edits.

## Items

### 1. AuthGate default line (NEW-A1)
- `native_orion/src/OrionAppController.h:2483`: "Enter a license key to unlock Venice." → "Paste the one-time code from zaeorion.com/connect, then press Unlock."
- I also changed these AuthGate strings, because the error card shows them and they said "license":
  - `OrionAppController.cpp:2269`: "Discord connection did not return a valid license. Connect again." → "Discord sign-in did not return a valid code. Get a fresh one-time code from zaeorion.com/connect and try again."
  - `:2285`: "License verified. Opening Venice." → "Verified. Opening Venice."
  - `:5581`: "Checking license..." → "Checking your code…"
  - `AuthGate.qml:282`: "Verifying license…" → "Verifying…"

### 2. AuthGate hint selection (NEW-A2)
- `native_orion/qml/pages/AuthGate.qml:24-63`: the hint logic is now `function hintFor(message)`, placed between the `// HINT-FN-BEGIN` / `// HINT-FN-END` markers. `errorHint: hintFor(orion.authMessage)`.
- Why the rules match text: the server error code never reaches QML. `authMessage` holds either mapped copy (`licenseErrorUserText`), the server's prose, or the bare code, so every rule matches the sentence and the raw code.
- The rules are ordered, and the first match wins:
  1. No hint when the message is already the whole answer: Discord-ID paste, clock off, paused/frozen, "contact support".
  2. Update required / `version_blocked` → "Restart Venice to update, or reinstall from #downloads."
  3. `\bblocked\b` / `blacklisted` → "Open a ticket in the Venice Discord."
  4. fingerprint / `machine_id` → "Restart Venice; if it repeats, open a ticket."
  5. Rate limit → the existing rate-limit hint.
  6. different PC / `device_mismatch` / hwid → the /hwid_reset hint.
  7. `subscription_required` / `discord_signin` → the Discord sign-in hint.
  8. Sign-in link / one-time code / `invalid_key` / key format / replay → the fresh-code hint.
  9. expire / `trial_used` / revoked → the subscription-ended hint.
  10. Network (timed out, connection refused/closed/reset, host not found, TLS/SSL/certificate, `entitlement_unavailable`, `internal_error`, `service_disabled`) → "Couldn't reach Venice's servers. Check your internet connection and try again in a moment." The old version said "license server".
- Removed: the loose `/connect/` match, which sent the Discord-ID paste and "Connect Discord again" to the network hint. Also removed the old `/machine|device|another|bound|hwid/` and `/discord_signin|required/` patterns.
- Test: `tests/test_copy_fixes_20260923.py::test_authgate_hint_picks_the_right_next_step_for_every_message` runs the extracted function under node against 30 real messages. These cover every mis-hint in the review, the server's prose, the bare codes and the transport errors.

### 3. Website after purchase, and the other-browser payer (CW-4, CW-1)
- `website/src/worker.js:540-546`: when `purchaseComplete`, the page renders only `<a class="button primary" href="/connect">Get your one-time code</a>`. The Subscribe button and the "Starting the 7-day trial?" block are gone. The page without the purchase flag is unchanged.
- `website/src/worker.js:624-626`: `ENTITLEMENT_MESSAGE` now ends with "…Claim the trial in Discord, then try again. Just paid? Activation can take up to a minute — refresh this page."
- Tests: `website/tests/worker.test.mjs:964` updates the `ENTITLEMENT_TEXT` pin to the new message. I added two tests: `CW-4: the account page after checkout offers only Get your one-time code` (with a negative control for the page without the flag) and `CW-1: no paid cookie + not yet provisioned tells a just-paid customer to refresh`.

### 4. First-run tour meter step (CW-5)
- `native_orion/qml/components/FirstRunTour.qml:82`: "Pick the Style and Color that match the in-game meter. …" → "Arrow2 (White) is preset here — set your in-game shot meter to Arrow2 (White). Detection adapts on its own while you play, and Venice keeps refining its timing on your setup over your first shots."

### 5. Raw engine errors on the Live page (NEW-A5, EA-25, NEW-A18)
- New `ui_notifications::customerRemoteStatus(raw)` in `native_orion/src/UiNotificationPolicy.h:166-235`. It maps raw `RemotePlaySession` statuses to coded, plain copy in the RP-01..04 style:
  - RP-05 "Part of Venice is missing from this install (code RP-05). Reinstall Venice from the latest download." Covers `Production package is incomplete…`.
  - RP-06 "Venice's detection engine didn't start (code RP-06). Restart Venice; if it happens again, reinstall from the latest download." Covers the sidecar launch failure. When the engine crashes or dies instead, the RP-06 copy is "…stopped (code RP-06). If the picture doesn't come back, disconnect and connect again."
  - RP-07 "Venice couldn't reconnect your controller (code RP-07). Press Connect." Covers every "Chiaki input recovery …" status and "Recovered input child …".
  - RP-08 "Venice couldn't link your controller to the PS5 (code RP-08). Disconnect and connect again." Covers "Stream start did not confirm…" and "input session was not proven ready".
  - RP-09 "Venice hit an unexpected error (code RP-09). Disconnect and connect again; if it repeats, restart Venice." This is the catch-all for any other status that mentions the sidecar, Chiaki, autogreen, "input child" or `ORION_`.
  - Progress lines get plain copy too: "Starting…", "Connecting your controller…", "Starting video detection…", "Restarting video detection…", "Reconnecting your controller…", "Connected — meter detection active", "Stopped", "PS5 connection test passed". "Console IP is required before starting Chiaki" → "Enter your PS5's IP address in Setup, then press Connect."
  - Plain statuses pass through unchanged, including the existing RP-0x copy.
- `OrionAppController.cpp:2611-2614`: `remoteStatus_` now holds the mapped copy. That feeds the Live Capture subtitle, the input-dead Notice detail and the "Remote Play: …" Activity line (`:2699`).
- `OrionAppController.cpp:2700-2703`: when the mapping changed the text, the raw status is logged as "Remote Play engine detail: …". Rule 0 below keeps that line out of the customer feed. The `orion_user.log` line (`:2711`) also uses the mapped copy.
- Input-dead detail (`OrionAppController.cpp:7886-7897`):
  - "Reconnect blocked: %1" → the mapped `remoteStatus_`.
  - "Automatic reconnect did not restore the session — press Connect. (%1)" → "Venice couldn't reconnect your controller (code RP-07). Press Connect."
  - "Press Connect to restore the session. (%1)" → "Press Connect to restore the session."
- `RemotePlaySession.cpp:4151-4155`: the input-recovery failure no longer puts the raw sidecar `msg` into the session status. It uses the fixed "Chiaki input recovery failed.", which maps to RP-07. The raw `msg` stays in the setupMessage log line directly below it.

### 6. Overlay button name (EA-23)
- `OrionAppController.cpp:7880-7882`: "This is the HDMI preview only — no session is connected. Press Enable Bot + Controller to connect." → "This is the HDMI preview only — Venice isn't connected yet. Press Connect."

### 7. "license key" / "licence key" (CW-8, NEW-A3)
- `native_orion/qml/pages/LegalGate.qml:124`: "…or share your license key." → "…or share your account access or one-time codes."
- `native_orion/qml/components/Sidebar.qml:719`: "Each licence key includes N free PC resets…" → "Your account includes N free PC resets…"
- `native_orion/qml/components/Sidebar.qml:641-651`, the Key row: **hidden in customer builds** (`visible: orion.debugUiEnabled === true && …`). Here is why. Sign-in is keyless. The pair code is exchanged for `result.canonicalLicenseKey` (`OrionAppController.cpp:2255-2280`), which Venice stores and uses for heartbeats, so the customer never types or needs it. `authenticate()` still accepts a raw canonical key (`validateLicenseKeyFormat`), so showing it with a Copy button just invites sharing. Dev builds keep the row. The `label: "Key"` text, `orion.licenseKeyMasked` and `orion.copyLicenseKey()` stay in the source, because `test_venice_ui_contract.py` and `test_non_live_production_surface_contract.py` pin them.

### 8. SmartScreen (CW-7, NEW-A11): **owner flag**
- Signing status: `installer/Output/VeniceSetup-1.0.0.exe` returns `Get-AuthenticodeSignature` = **NotSigned**. The code supports signing: `orion.iss:128-131` sets `SignTool`/`SignedUninstaller` when `SignToolName` is defined, and `build_installer.ps1:179-188` takes `-SignToolCommand`. Without that option the script refuses to build unless given `-AllowUnsigned`, and warns "Never ship this artifact." I found no signing certificate in use. As things stand, **the shipping installer is unsigned**.
- `discord_launch/launch_embeds/downloads.json` field 1 (line 21): "Windows will show the publisher on the signed installer — if it does not, stop and open a ticket." → the SmartScreen steps plus a download check. It now says the installer isn't code-signed yet, Windows may show "Windows protected your PC", click **More info** then **Run anyway**, Setup asks for administrator rights because it installs a controller driver, and **Check your download:** run `Get-FileHash $HOME\Downloads\VeniceSetup-*.exe` and match the SHA-256 above, or don't run it and open a ticket. The field is 488 chars, under Discord's 1024 limit.
- `downloads.json` `_meta.signing_status` (new) records this, and flags that `_meta.blocked_by` still says "Do not post a link to an unsigned build". **Owner decision:** either ship unsigned with this copy and delete the blocker note, or buy and plug in a certificate. If you sign it, swap the SmartScreen sentence for the publisher name.
- `installer/INSTALL_NOTICE.txt`, rewritten:
  - It keeps "isn't code-signed yet" and the More info → Run anyway steps.
  - It adds the admin-rights line, the `Get-FileHash` check against the #downloads SHA-256, and "only download from #downloads".
  - It drops the promise "A code-signed version is coming in the first update… you'll never see this warning again".
  - This file is not wired into the installer (nothing references it). SmartScreen fires before any wizard page anyway, so the #downloads embed is the channel that matters.

### 9. Activity feed internal lines (NEW-A6, EA-J7)
- `UiNotificationPolicy.h:140-164`: new **rule 0** `isInternalOnlyLine`, checked in `shouldEnterActivityRing` **before** the allow-list (`:~390`).
  - Markers: `lease-gated fire`, `fire lease`, `sidecar`, `orion_`, `engine detail:`, `shot automation aborted:`.
  - A bare `lease` is deliberately not a marker, because it would hide every "Release …" shot line.
  - These lines still go to `orion_native.log` and the Debug ring, and `tools/diagnostics/session_report.py` still sees "restarting detection sidecar".
- With rule 0 in place, customers no longer see:
  - "Lease-gated fire ENABLED (ORION_LEASE_GATED_FIRE)…"
  - "Fire lease seeded by activation…"
  - "Stream warm-up hiccup (sidecar exited…)"
  - "Watchdog: restarting detection sidecar…"
  - the three "SAFE MODE: sidecar…" lines
  - "Input-only recovery started…"
  - every `ORION_*` env line
  - raw setupMessage sidecar lines
- Customer lines I rewrote or added so the customer isn't left in silence:
  - `OrionAppController.cpp:4966`, the watchdog trip reason. This text also appears in the SAFE MODE dialog. "Detection sidecar exited mid-stream" → "Video detection stopped mid-stream".
  - `:4925-4928`: new "Safe mode: video detection keeps stopping. Click SAFE MODE at the top, then Exit safe mode, or restart Venice." It is written next to the crash-loop line.
  - `:10837`: "…(applies on the next sidecar launch)" → "…(applies the next time you connect)".
  - `:9359`: calibration refused. The ORION_ line stays engineering-only, and a new customer line reads "Calibration isn't available right now — restart Venice." (EA-31)

### 10. Verified rows 11-21
- **Row 12 (CW-9/EA-27/NEW-A7): Shot Lead conflict, controller half.**
  - `OrionAppController.cpp:3992-4002`: the Activity line "Shot not taken — Shot Lead N ms is more than Tip Timing M ms can schedule (the release deadline…)" → "Shot not taken — Shot Lead N is too high for your jumpshot, so Venice can't release in time. Lower it to M or less (or press Reset on Tip Timing)." N and M are now on the card's 1-100 scale, via `ui_notifications::shotLeadSliderValue`, which mirrors `ShotLeadCard.qml` `valueFromMs` 150..400.
  - "Shot not taken (%1)." → `ui_notifications::customerShotNotTakenText(reason)` (`UiNotificationPolicy.h:247-282`). It gives a plain category ("couldn't release in time", "the controller link wasn't ready", "lost track of the shot", "automation was paused", "still measuring your setup", "Shot canceled.") and falls back to "Shot not taken." The raw "Shot automation aborted: <enum>" line is now rule 0, so it stays in the disk log only (`tools/timing/live_batch_report.py` parses it).
- **Row 12 and row 13, ShotLeadCard half: NOT applied.** The main session owns `ShotLeadCard.qml`. Please apply these:
  - `ShotLeadCard.qml:67-72` (conflictText):
    - old: `"Shot Lead " + root.shownValue + " is more than Tip Timing can schedule — " + "live tip shots cannot fire and will abort. Lower it to " + root.valueFromMs(orion.shotLeadMaxUsableMs) + " or less, or raise/reset Tip Timing." + (orion.shotLeadConflictMisses > 0 ? " " + orion.shotLeadConflictMisses + " shot(s) already aborted this session." : "")`
    - new: `"Shot Lead " + root.shownValue + " is too high for your jumpshot — Venice can't release in time, so shots are skipped. Lower it to " + root.valueFromMs(orion.shotLeadMaxUsableMs) + " or less (or press Reset on Tip Timing)." + (orion.shotLeadConflictMisses > 0 ? " " + orion.shotLeadConflictMisses + " shot(s) skipped this session." : "")`
  - `ShotLeadCard.qml:243-245` (auto-trim caption):
    - old: `"Auto-trim from game feedback: " + (sign) + Math.abs(Math.round(orion.bannerLeadTrimMs)) + " ms"`
    - new: `"Auto-adjusting from game feedback: " + (orion.bannerLeadTrimMs > 0 ? "+" : "−") + Math.max(1, Math.round(Math.abs(orion.bannerLeadTrimMs) * 99 / (root.msHi - root.msLo)))`
  - `ShotLeadCard.qml:264-269` (auto-seed caption):
    - old: `"Auto: measured latency X ms + Y ms margin = Z ms"` / `"Auto: calibrating… using Z ms until your latency is measured"`
    - new: `"Auto: set to " + root.valueFromMs(orion.leadAutoSeedMs) + " from your measured setup"` / `"Auto: calibrating… using " + root.valueFromMs(orion.leadAutoSeedMs) + " until your setup is measured"`
- **Row 13 (CW-6/EA-30): calibration, controller half.**
  - `OrionAppController.cpp:9331-9335`: "Locked at %1 ms after %2 shots. Saved — …" → "Locked at Shot Lead %1 after %2 shots. Saved — …".
  - `:9342-9343`: "Shot %1 — lead %2 ms, adjusting by %3 ms. Keep going until it locks." → "Shot %1 — Shot Lead %2 so far. Keep going until it locks."
- **Row 14 (CW-13/EA-21/EA-22):**
  - `OrionAppController.cpp:7865`: "CONNECTING — BUTTONS NOT REACHING THE CONSOLE YET" → "Connecting — buttons aren't reaching your PS5 yet".
  - `:7869`: "CONTROLLER INPUT IS NOT REACHING THE CONSOLE" → "Controller input isn't reaching your PS5".
  - `:7874-7876`: "The direct input link dropped — recovering it now. Presses are not landing…" → "Connection to the PS5 dropped — reconnecting now. Presses won't reach the game until it's back."
  - The QML headline still uses `font.letterSpacing: 1.0`, which is fine for sentence case.
- **Row 15 (CW reboot):**
  - `installer/orion.iss:412-415`: new `VCRedistRestartNeeded` flag. It is set on a VC++ 3010 at `:434`.
  - `:691-703`: new `RestartPending` and `NeedRestart()`. When a driver or runtime restart is pending, Inno's Finished page offers "Restart now".
  - `:194-196`: the "Launch Venice" postinstall entry gets `Check: not RestartPending`.
  - The installer was **not compiled** (no ISCC run). The Check/NeedRestart syntax is standard Inno, but it has not been compiled.
- **Row 16 (NEW-A8):** `MeterConfigPanel.qml:205-210`, the detector health line: `visible: orion.debugUiEnabled === true`. It is dev-only now.
- **Row 17 (CW-11/EA-18):** `UpdateGatePage.qml:309`: "Signed updates · Ed25519 verified" → "Signed, verified updates".
- **Row 18 (CW-10/EA-26):**
  - `LicenseHeartbeatPolicy.h:117`: "…shots paused until your licence is confirmed." → "Reconnecting to Venice servers — shots paused until your subscription is confirmed. Usually a few seconds." I kept "shots paused", because `LicenseHeartbeatPolicyTests` pins it.
  - `:278`: "Licence confirmed: shots re-enabled." → "Subscription confirmed: shots re-enabled." It now reaches the ring through default-keep instead of the "licence" allow-word. `LicenseHeartbeatPolicyTests::retryLinesAreEngineeringOnly` still asserts that it enters the ring.
- **Row 19 (NEW-A12):** `discord_launch/launch_embeds/setup_guide.json:45`: "Shot meter **on** in 2K's settings." → "Shot meter **on**, style **Arrow2** (White), in 2K's settings."
- **Rows 20-21 (CW-14/EA-33, NEW-A9): Xbox.**
  - `StreamSetupForm.qml:108-113`: the console combo shows `["PS5"]` in customer builds. Xbox appears only in dev builds, or when the install already has Xbox selected, so the combo never shows a value missing from its list; switching to PS5 removes it. The Xbox section, acknowledgement gate and session refusal are unchanged.
  - `:227-229`: "WGC video → shared meter reader → virtual Xbox controller…" → "Venice watches the Xbox app window and sends input through a virtual controller. Keep the window visible at a fixed size. Disconnecting Venice leaves the Xbox app open."
  - `:231`: "Verify the Xbox app receives only Venice's virtual controller… Configure HidHide separately if needed." → "Make sure the Xbox app sees only Venice's virtual controller, not your physical pad as well." I did **not** use the brief's "Unplug any second controller", because the physical pad is Venice's input.
- **Row 11** is item 9 above. **Row 22** (the disagreement banner) was removed by the main session.

### 11. Lead env overrides in production (EA-31 side note, security)
- `native_orion/src/AutomationEngine.cpp:2628-2654`: the `ORION_LEAD_FLOOR_MS` and `ORION_LEAD_BIAS_MS` reads are now inside `#ifndef ORION_PRODUCTION_BUILD … #endif`, the same pattern as the `ORION_ONSET_FF_*` gating at `:1611-1642`.
  - `bool leadOverrideFromEnv = false;` and `config_.leadOverrideFromEnv = leadOverrideFromEnv;` stay outside the guard, so production always gets `false`.
  - As a result, a production build ignores both env vars, and the calibration-refused branch cannot fire there.
- No production-path native test exists, because test targets build without `ORION_PRODUCTION_BUILD`. The existing ONSET_FF gating has none either. I added a source contract instead: `test_lead_floor_and_bias_env_overrides_are_compiled_out_of_production`. It checks that both env reads sit between the guard and its `#endif`, and that each variable is read exactly once. The dev test `AutomationEngineTests::devLeadEnvOverrideStillBeatsTheUserShotLead` (`:39265`) is still valid, because tests build without `ORION_PRODUCTION_BUILD`.

## Deferred
- **HDCP black-frame detection (verified row 10):** this is feature work, deferred until after testing, as instructed.
- **REAL rows I did not touch, because they were outside the 11-item brief:**
  - 23: installer ACT-01 and internal Detail in the exception text
  - 24: supported-setup scope, owner decision
  - 25: DM privacy copy
  - 26 and 27: `LicenseClient.cpp:266` "This device is blocked." and `:287` "This **licence** is linked to a different PC…". The hint picker now handles both.
  - 29: "Invalid key format." / "TLS verification failed." / "Pinned server certificate did not match." still appear verbatim in the error card, although the hint is now correct.
  - 30: "rate limited locally"
  - 31: the update-required suffix
  - 33: `orion_bot.py` "license"/"keys"
  - 35: "URL:Orion Protocol"
  - 36: the admin-rights line, which is now in the downloads embed
- **ENTITLEMENT_MESSAGE:** it still says "Claim the trial in Discord", but the account page says the trial starts on the website home page. One of the two is stale. **Owner decision.**

## Tests changed or added
- New: `tests/test_copy_fixes_20260923.py`, 13 tests. It pins every new string, the removal of every old one, the node-executed hint picker, the embed/notice agreement, rule 0 ordering, the NeedRestart wiring, the Xbox gate, and the production env gate.
- `website/tests/worker.test.mjs`: `ENTITLEMENT_TEXT` updated, plus 2 new tests (CW-4 and CW-1).
- `native_orion/tests/ActivityFeedPolicyTests.cpp`:
  - `humanEventsAlwaysReachTheCustomerFeed`: "Watchdog: restarting detection sidecar." is replaced by the new customer lines ("Watchdog trip: Video detection stopped mid-stream", the "Safe mode: video detection keeps stopping…" line and a mapped RP-07 "Remote Play:" line). The old line is now asserted **hidden**.
  - New `internalOnlyLinesNeverReachTheCustomerFeed`: 11 internal lines are hidden, and "Release …" and the capture-rate line are kept.
  - New `remoteStatusMapsEngineErrorsToCodedCustomerCopy`: 13 raw statuses map to codes with no jargon, and plain statuses pass through.
  - New `shotNotTakenNeverPrintsTheRawReasonEnum`: 11 reasons plus the slider mapping at 150/275/400 and at the clamps.
- `test_venice_ui_contract.py` was not edited. The main session is editing it, and nothing in it pinned a string I changed.

## Focused test results
- `.venv\Scripts\python.exe -m pytest tests/test_copy_fixes_20260923.py -q -p no:cacheprovider --basetemp C:/Users/aaron/AppData/Local/Temp/copyfix-pytest` → **13 passed**.
- The same command over the 22 files that read the edited sources: `test_async_log_sink_contract, test_capture_fps_env, test_capture_qualification_cl2_p3, test_controller_route_lifecycle_contract, test_copy_fixes_20260923, test_gui_cadence_contract, test_installer_uninstall_path, test_installer_venice_service, test_latency_calibration_ui_contract, test_non_live_production_surface_contract, test_packer_hardening, test_passive_court_flow_contract, test_preview_cadence_policy, test_preview_qml_contract, test_route_transition_source_contract, test_server_shard_release_gate, test_ship_defaults, test_shipped_reader_defaults, test_venice_live_page_cleanup_contract, test_venice_profile, test_venice_ui_contract, test_xbox_path_hazards` → **328 passed**.
- `test_remote_play_frame_pipe, test_latency_corroboration, test_manual_shot_tally, test_ownership_proof_detector_box, test_pipeline_clock_trace, test_readiness_incident, test_release_oracle, test_square_path_press_census, test_latency_estimator` → **207 passed, 1 xfailed, 2 xpassed**. The xfail/xpass results were already there and are unrelated.
- `node --test tests/worker.test.mjs`, run from `website/` → **47 passed, 0 failed**.
- JSON validity: `downloads.json` and `setup_guide.json` both parse.

## Native tests Claude must build and run after the owner's session
- **ActivityFeedPolicyTests**: the edited and new cases above (`UiNotificationPolicy.h` changed).
- **LicenseHeartbeatPolicyTests**: `leaseNoticeTextIsPlain` and `retryLinesAreEngineeringOnly` (the notice and restored strings changed).
- **AutomationEngineTests**: at least `devLeadEnvOverrideStillBeatsTheUserShotLead` and the onset-FF env block (`AutomationEngine.cpp` changed; dev build, so it should still pass).
- **PreviewPresentationBufferTests**: it loads QML with a mock `orion`. Sidebar (the Key row is now bound to `debugUiEnabled`), MeterConfigPanel (health line `visible`), StreamSetupForm (the combo model reads `debugUiEnabled` / `remotePlayConsole`), and the licence flyout probes (`licenseFlyoutCopyKey` is now invisible when the mock has `debugUiEnabled=false`).
- **MeterDelaySettingsPropertyTests**: the property existence checks (unchanged properties; recompile only).
- **The full native build**: `OrionAppController.cpp/.h`, `RemotePlaySession.cpp`, `AutomationEngine.cpp`, `UiNotificationPolicy.h` (new `<cmath>` include) and `LicenseHeartbeatPolicy.h` all changed.
- **A production-configured build**, to confirm the `#ifndef ORION_PRODUCTION_BUILD` block compiles out cleanly with no unused-variable warning as error. `leadOverrideFromEnv` is still assigned after the block.
- **The installer**: compile once with ISCC (`NeedRestart`, `RestartPending`, `Check: not RestartPending`).
- **A QML smoke launch after the build**: AuthGate (`hintFor` is a JS function in an Item), Sidebar, StreamSetupForm, MeterConfigPanel, FirstRunTour and UpdateGatePage. `qmllint` could not run on this PC (exit 127).

## Needs an owner decision
1. **Unsigned installer (item 8).** Ship unsigned with the new SmartScreen + SHA-256 copy and delete `_meta.blocked_by` in downloads.json, or buy and plug in a signing certificate first.
2. **Xbox**: currently hidden for customers except on installs that already selected it. Say if you want it fully visible with the acknowledgement gate instead.
3. **ENTITLEMENT_MESSAGE:** "Claim the trial in Discord" versus the account page's "start it on the Venice home page". Which is right?
4. **Supported-setup scope (verified row 24)**: the installer says capture card only, while the site and #downloads say capture card or Remote Play.
