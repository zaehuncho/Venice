# P-E patch: customer states, copy and patch-day docs (2026-09-23)

Scope: RT-MED-09, RT-MED-10, RT-LOW-01, RT-LOW-02, RT-LOW-05 (+ CL3-F8-001/003/004/005/006/007/008/011/012 lows), RT-HIGH-04 (docs only), RT-LOW-07 (dead branch, same function).
Owner decisions applied (relayed 2026-09-23): Remote Play **fully supported** alongside capture card; Xbox ships **EXPERIMENTAL** (shown, labelled, not hidden); beta installer ships **UNSIGNED** and may be posted; the free trial **starts on the website home page**, not in Discord; 30 Hz capture is **preview only** (P-C).

Nothing was built, launched, deployed, committed or posted. No `register_commands.py`. Owner `settings.json` untouched. Admin QML / AdminToolController not touched.

## Native targets Claude must build and run

- Full `OrionNative` (dev + production): `SecurityManager.{h,cpp}`, `OrionAppController.{h,cpp}`, `UiNotificationPolicy.h`, new `GuiFreezeWatchdogPolicy.h`.
- **New** `OrionSettingsSignatureTransactionTests` (tests/SettingsSignatureTransactionTests.cpp, 9 cases; QSKIPs under `ORION_PRODUCTION_BUILD`).
- **New** `OrionGuiFreezeWatchdogPolicyTests` (tests/GuiFreezeWatchdogPolicyTests.cpp, 7 cases).
- `OrionActivityFeedPolicyTests` (new case `peCustomerStateLinesAndTheirEngineeringTwins`).
- `OrionPreviewPresentationTests` (loads QML with a mock `orion`: RemotePlayPage banners use `=== true`, safe for a mock without the new properties), `OrionNativeTests`, `OrionMachineIdParityTests` (SecurityCore changed).
- Both new targets were added to `native_orion/CMakeLists.txt` and to both `cmake --build` target lists in `scripts/verify_orion.ps1` (otherwise ctest would report them missing). The ctest total goes up by 2.
- QML smoke after the build: AuthGate (new `displayMessage`/`isServicePaused` functions), RemotePlayPage (two new banners), StreamSetupForm, TopStatusBar, DebugPage.

## 1. RT-MED-09: atomic settings save + sign, scoped repair, no laundering

Files: `native_orion/src/SecurityManager.{h,cpp}`, `OrionAppController.{h,cpp}`, `UiNotificationPolicy.h`, `qml/pages/RemotePlayPage.qml`, `qml/pages/DebugPage.qml`, `tools/release_filter_policy.py`. AppConfig was **not** touched.

- **Transaction** (SecurityManager): `beginSettingsWrite()` snapshots a *verified* pair to `settings.json.prev` + `.prev.sig`, then writes the journal `settings.json.txn` last. `AppConfig::save()` (already QSaveFile-atomic) writes settings. `commitSettingsWrite()` signs, records the `settings.signed-once` marker, removes the journal. `recoverInterruptedSettingsWrite()` (startup, before `config_.load()`) restores the old pair only when the snapshot verifies and matches the journal; it never signs. Result after a crash at any point: exact old pair or exact new pair.
- **Gate**: `saveConfigSilently` / `persistConfig` call `beginSignedSettingsSave()` first. `Refused` (pair already invalid outside the one-time bootstrap) → the save is not written, one customer line + an engineering line. Previously any slider move while locked re-signed the in-memory config that had been **loaded from the tampered file** (a second laundering path, not in the report).
- **One-time bootstrap**: allowed only while there is no signature **and** no `settings.signed-once` marker. In production an unsigned `settings.json` already on disk is copied to `settings.rejected.json` and replaced with defaults, never signed as-is. The old unconditional `if (!hasSettingsSignature())` sign-whatever-is-loaded branch is gone.
- **Found while fixing**: `AppConfig::load()` persists a settings_version migration with `save()` and never re-signs, so every update that bumps `kSettingsVersion` would lock production customers. Startup now runs `load()` inside the transaction when the pre-load pair verified, and re-signs the migrated file.
- **RT-LOW-07**: the unreachable `[ORION_FIRST_RUN_BOOTSTRAP]` branch in `SecurityManager::evaluate()` is removed. The dev-only auto-resign is unchanged. In dev builds the transaction is a no-op (legacy re-sign, no snapshot/journal/marker files in the source tree).
- **Repair**: `Q_INVOKABLE repairSettings()` + `Q_PROPERTY settingsRepairAvailable` (true only when the settings signature is the lock reason). It acts only while the signature is invalid and not connected; it keeps the rejected file aside, saves **factory defaults** (`AppConfig defaults(rootDir_)`) and signs them. learning.json (timing history) is untouched. Live-page banner **SHOTS OFF** with a **Repair settings** button (objectNames `settingsRepairBanner`, `settingsRepairButton`); it outranks the lease banner and the MOTD and yields to dead input.
- **DebugPage "Sign Settings"**: `visible: orion.debugUiEnabled === true`; `signSettings()` logs and does nothing under `ORION_PRODUCTION_BUILD`.
- **Copy** (old → new):
  - Activity: "Remote Play blocked by security lock: Settings signature missing or invalid" → "Venice's settings didn't save cleanly (code ST-01), so shots are off. Press Repair settings on the Live page to go back to default settings." (raw reason → "Security engine detail: …", hidden by rule 0).
  - Same branch with a lapsed lease → "Remote Play: reconnecting to Venice servers — shots are paused until your subscription is confirmed. Usually a few seconds."; any other lock → "Remote Play: Venice's safety check didn't pass, so Connect is blocked (code ST-03). Restart Venice; if it repeats, reinstall from #downloads."
  - New: "Venice restored your last saved settings after an interrupted save." / "Settings repaired: Venice is back on its default settings. Your shot timing history was kept. Check Shot Lead and your meter style, then press Connect." / "Repair settings didn't finish (code ST-02). …"
- `release_filter_policy.py`: the five transaction file names are denylisted.
- Residual (not fixed, needs a design decision): the signature is `SHA-256("orion-settings-v1|" + machineId + file)`, keyless. A local user who reads the source can compute it; the marker is user-writable too. This patch fixes crash-safety and removes the in-app laundering paths; real tamper-evidence needs a DPAPI-keyed HMAC (would invalidate every existing signature → needs a migration).

Tests: `tests/test_customer_states_pe_20260923.py` (source contracts), `SettingsSignatureTransactionTests` (crash between writes → old pair; crash after sig → new pair; crash before write; tamper → Refused, not re-signed; deleted sig after first sign → no bootstrap; fresh profile bootstraps once; forged journal → nothing signed; dev legacy). The native tests are written but not built.

## 2. RT-MED-10: sleep/resume and the service pause

Files: new `native_orion/src/GuiFreezeWatchdogPolicy.h`, `OrionAppController.{h,cpp}`, `qml/pages/AuthGate.qml`.

- Heartbeat and suppression stamps use `std::chrono::steady_clock` (`gui_freeze::monotonicMs()`), not `QDateTime::currentMSecsSinceEpoch()`.
- `gui_freeze::evaluate()` returns **Suspended** (re-seed, never trip) when `WM_POWERBROADCAST PBT_APMSUSPEND` set `systemSuspended_`, or when the watchdog's **own** 500 ms loop gapped > 3 s (the whole process was paused; a real GUI freeze leaves that thread on time). A stuck suspend flag clears after 60 s of on-time polls.
- `PBT_APMSUSPEND`: disarm, `disarmPreciseFire()`, `neutralizeOwnedInput()` synchronously. Resume: re-seed the heartbeat, clear the flag, `syncEngineArmed()`, request the licence heartbeat (existing), and if connected log "Venice resumed from sleep. If the picture or your controller doesn't come back, press Disconnect, then Connect."
- Safe-mode reason "UI thread froze for over 6 seconds" → "Venice's window stopped responding for a few seconds".
- Service pause (kill switch): activation (`service_disabled: <reason>` in the code) and heartbeat kill now set authMessage "Venice is paused by the service right now. Nothing is wrong with your PC or internet." AuthGate `displayMessage()` maps the raw code the same way; `hintFor()` gives "Check #announcements in the Venice Discord for when it's back, then press Unlock again." `service_disabled` was removed from the network rule.
- Tests: `GuiFreezeWatchdogPolicyTests` (1 h sleep gap = Suspended; announced suspend parks; a real 6.5 s GUI stall with the loop on time still trips; a backward step neither trips nor blinds it; teardown suppression), node-run AuthGate cases in the new pytest file.

## 3. RT-LOW-01: "Orion" in customer text

- "Feed paused: Orion is minimized — the stream present is stalled; automation resumes when you bring Orion to the foreground (no safe mode)." → "Feed paused: Venice is minimized, so shots are paused. They resume when you bring the Venice window back."
- Diagnostics bundle: README "Orion diagnostics bundle." → "Venice diagnostics bundle."; Desktop zip `orion_diagnostics_*.zip` → `venice_diagnostics_*.zip`.
- Grep of customer QML literals is clean (DebugPage is dev-only). Test: `test_no_orion_codename_in_customer_strings` + native ring case.
- **Not my files, still customer-visible (for their owners):** `RemotePlaySession.cpp:1319` "Orion Stream client was not opened because its build identity could not be verified." (reaches the feed via setupMessage); `main.cpp:188` "URL:Orion Protocol" (Windows default-apps list); `tools/package_orion_release.py:~873` writes `README.md` "# Orion Native Runtime … Orion's custom low-latency Remote Play client" into the install folder. Do **not** rename `setApplicationName("Orion Native")`: it is the data-dir path.

## 4. RT-LOW-02: DEPLOY_LOG.txt and non-runtime material

- `tools/release_filter_policy.py`: `_DEPLOY_JOURNAL_RE` denies `deploy_log*` / `build_log*` `.txt|.md` in any casing (rc1 shipped `chiaki-ng-orion/chiaki-ng-Win/DEPLOY_LOG.txt` with build hashes, backup names and finding IDs). `THIRD_PARTY_NOTICES.txt` and `LICENSE.txt` still ship.
- Test: `tests/test_release_filter_hygiene.py::test_deploy_logs_and_build_journals_never_ship` (failed first, then passed) and `::test_settings_transaction_files_never_ship`.
- Remaining RT-LOW-02 items (developer comments/build paths in binaries, the dead interpreter helper, the 69 production env knobs) are outside this file set.

## 5. RT-LOW-05 + F8 lows: one set of facts in every channel

| Fact | App (QML/C++) | Website | Embeds | Bot |
|---|---|---|---|---|
| Remote Play + capture card fully supported | StreamSetupForm unchanged ("Remote Play carries video and the controller with no capture card required.") | index.html beta line, pricing list, "Runs on" | setup_guide, launch_announcement (+ Nereus body), pricing, welcome/terms | /faq remote_play, quick_answers |
| Xbox EXPERIMENTAL, shown | Console combo `["PS5", "Xbox"]` for everyone; PS5 hint "Xbox is listed as EXPERIMENTAL (untested)."; Xbox hint/ack/help rewritten to "untested, no timing accuracy claim" (removes the "not supported" vs "own Shot Lead" contradiction) | same three spots | all "Xbox is not supported yet" → "Xbox is experimental and untested." | quick_answers |
| Installer unsigned (beta) | n/a | n/a | downloads `_meta.signing_status`/`blocked_by` rewritten (posting allowed, real URL+hash still required); setup_guide "signed installer" → "installer"; README copy facts | n/a |
| Trial starts on website | n/a | "Free trial" row, trial-note fallback, refunds.html; worker `ENTITLEMENT_MESSAGE` → "Start the free trial on the Venice home page, then try again."; worker trial-unavailable 503 no longer says /claim_trial (`worker.test.mjs` pin updated) | all `/claim_trial` mentions → website home page | `/claim_trial` now replies with the website steps (command stays registered; no backend call); `/status` "no membership" line; quick_answers |
| 30 Hz preview only | Refresh rate `"30 Hz (preview only)"`, hint "Shot timing needs 60 Hz. At 30 Hz Venice shows the picture but does not time shots." (objectName `captureFpsCombo` unchanged) | n/a | already "60 Hz capture card" | n/a |

Bot copy (CL3-F8-011): `/hwid_reset` "Activate on your new PC with the same key." → "On your new PC, open https://zaeorion.com/connect with this same Discord account and use a fresh one-time code."; "on this key"/"trial key"/"This key"/"your license" → account/trial/access; /faq no_greens "reconnect (Disconnect → Connect Chiaki), make sure Chiaki shows the game" → "press **Disconnect**, then **Connect**, make sure the game … is showing"; "use a meter style/size Venice has calibrated against (defaults work best)" → "style **Arrow2** (White)"; new_pc "license … keys from being shared … gets the key revoked" → access/accounts; remote_play "have Chiaki open" → "Venice's Setup page open", plus the RP-10 paragraph. Needs a Triton redeploy on the owner's word.

Other F8 lows:
- CL3-F8-001: transport trip reason → "The capture card stopped sending video"; numbers → "Watchdog engine detail: capture transport failed (…)". Controller-route trips → "The controller link kept dropping" / "The controller link and the capture card both stopped".
- CL3-F8-003: Activity "SAFE MODE: %1 — automation disarmed. Auto-recovers after %2s of a healthy stream (max %3/session); or exit safe mode manually." → "SAFE MODE: %1. Shots are paused. Venice usually turns them back on by itself once the stream has been steady for %2 seconds, or click SAFE MODE at the top, then Exit safe mode." (prefix kept for `session_report.py`); dialog "Shot automation was disarmed after repeated failures and will stay off until you exit safe mode." → "Shots are paused because something kept failing. Venice usually turns them back on by itself once the stream has been steady for 30 seconds, or press Exit safe mode." The dead "Reconnecting" pill claim is now documented in TopStatusBar (bar only mounts in safe mode); moving it is a layout change for the owner.
- CL3-F8-004: `customerRemoteStatus` maps `RP_IN_USE` / `80108b10` / "already in use" to RP-10 "Another device is using Remote Play on this PS5 (code RP-10). Close Remote Play there, then press Connect." before every other rule. **Needs** the fork/`remote_play_client.py` to put the quit reason in the status (not my files).
- CL3-F8-012: AuthGate TLS/certificate → body "Couldn't make a secure connection to Venice's servers." + hint to check date/time first. `test_copy_fixes_20260923.py` HINT_CASES updated. The engine backward-step reset (CL2-P8-004) is AutomationEngine, not mine.
- CL3-F8-002 (detection-unavailable wording): the strings live in the meter-blindness function another agent owns, and `test_venice_ui_contract.py` pins "METER BLIND: %1 shots with no meter detected". I added the Live-page **NOT TIMING** banner bound to `meterBlindWarning` + `meterBlindHint` (MOTD wins the slot, so patch-day text overrides the hint). Proposed copy for the owning agent: DETECTION UNAVAILABLE line → "Venice can't see the shot meter, so it isn't timing shots. Take a couple of shots with the meter visible and it turns back on." (numbers to an engineering line); DETECTION RESTORED → "Venice can see the shot meter again; timing is back on."; `meterBlindHint()` → "Venice can't see the shot meter, so it isn't timing your shots. Check the in-game meter is on and set to Arrow2 (White). If 2K just updated, see #announcements."

### Installer text changes for Codex (installer/ is not mine)
1. `installer/orion.iss:150` WelcomeLabel2 "Venice is in beta: PS5 with a capture card is the supported setup." → "Venice is in beta: PS5 with a capture card or PS5 Remote Play is fully supported. Xbox is experimental."
2. `installer/build_installer.ps1:39,41,93,114,193,198` all say an unsigned installer must "never ship" and `:198` refuses to build without `-SignToolCommand`. With the owner's unsigned-beta decision this needs an explicit, audited switch (e.g. `-UnsignedBeta`, still requiring the Ed25519-signed package) and wording that allows publishing. Same text in `docs/LAUNCH_PACKING_CHECKLIST.md` Step 3 ("never ship it").
3. `installer/orion.iss:6-11,133` comments say the installer "must itself be EV-signed"; update to the beta decision.

### Other owners
- `website/public/app.js:445` still opens Discord for `/claim_trial` when `config.trial !== true` (the note text is now "Trial sign-up on the website is paused for a moment — try again shortly."). Change the fallback to not leave the page.
- `native_orion/src/LicenseClient.cpp` `licenseErrorUserText`: add a `service_disabled` case with the same sentence (I handled it in the controller and QML); "This licence is linked to a different PC." → "Your access is linked to a different PC." (macros quote the current text).
- `backend/lambda_function.py:632-634`: send `service_disabled` as the code and the reason as `message` (CL3-F8-008).
- `discord_launch/launch_embeds/announcements_2026-09-21.md:64` (a posted record) still says capture card is "the supported setup".
- `tests/test_packer_hardening.py::test_verify_only_refuses_an_unmanifested_runtime_file` fails in the current tree with "Unlisted (unverified) file in package" instead of `UNMANIFESTED_PACKAGE_FILE`: another agent's in-flight packer/verifier change, unrelated to the filter edit.

## 6. RT-HIGH-04 (docs only)

- `docs/support/PATCH_DAY_RUNBOOK.md`: detect → communicate → confirm → kill/keep → ship signed update → verify → recover, with the role table (owner / staff / Claude / Codex) and only commands that exist (`orion-admin server-config set --motd-*`, `orion-admin killswitch status|engage|release`, `run_orion.local.ps1`, `panel_grade.py`, `replay_framedump.py`, `session_report.py`, `scripts\build_orion_sidecar.ps1`, `sidecar_bundle_manifest.py --verify`, `verify_orion.ps1 -StrictSecurity`, `pack_lethe_release.py`, `verify_release_integrity.py`, `--min-client-version`). It names the open gaps it depends on (RT-CRIT-02 warm kill state, RT-HIGH-02 elevation, RT-MED-11 artifact URL).
- `docs/support/SUPPORT_MACROS.md`: 30 macros quoting the post-patch in-app strings (install/SmartScreen + Get-FileHash, keyless unlock, trial on the website, codes, clock, NOT TIMING, capture busy / 30 Hz preview only / dropping frames / safe mode, RP-10 and RP-05..09, ST-01/ST-02, sleep, lease, update, patch days, HWID, billing, Xbox experimental).
- `discord_launch/launch_embeds/patch_day_status_templates.json`: `patch_detected`, `service_paused`, `fix_shipped`, `all_clear` (embed + Nereus text; placeholders `{VERSION}` `{TIME}` `{DETAIL}`; never posted automatically).
- **Not implemented, what it would need** (also in the runbook): fleet counters on `/api/license/check` (`armed_epochs`, `owned_locks`, `detection_unavailable_trips`, `blind_backstop_blocks` per version per 5 min; backend aggregation, 50 %-drop owner alert, metrics number; client bounded payload + test); signed `detection_policy {autonomous: off, message}` in the lease/heartbeat (client: no release while off, banner shows message, unknown fails closed; backend: owner-only, audited, TOTP step-up); meter-model mismatch sentinel; a timed signed no-op update drill; a canary channel.

## Tests run (all offline)

- `.venv\Scripts\python.exe -B -m pytest` over `test_customer_states_pe_20260923` (16 new), `test_copy_fixes_20260923`, `test_orion_bot_commands`, `test_release_filter_hygiene`, `test_venice_ui_contract`, `test_non_live_production_surface_contract`, `test_preview_qml_contract`, `test_venice_live_page_cleanup_contract`, `test_xbox_path_hazards`, `test_capture_identity_pc`, `test_gui_cadence_contract`, `test_ship_defaults` → **191 passed**.
- Every other test file that reads an edited source (16 files incl. `test_release_packaging`, `test_security_audit`, `test_installer_venice_service`, `test_route_transition_source_contract`, `test_p_b_install_update_regressions`) → **255 passed**; `test_release_filter_hygiene` + `test_security_audit` + `test_release_packaging` after the last filter edit → **74 passed**.
- `node --test tests/worker.test.mjs` (website/) → **50 passed, 0 failed**.
- Fail-first: the bot-copy, patch-day docs and DEPLOY_LOG tests failed before their fixes. The C++/QML source contracts were written after those edits, so they did not fail first; the native QtTests are the behavioural proof and still need the central build.
- All embed JSON parses; no field over 1024 chars; largest message 2485 chars.

## Open TODOs

1. Build + run the native targets above (dev and production trees), then a QML smoke.
2. Owner: redeploy Triton for the bot copy and the `/claim_trial` redirect (to keep the in-Discord claim instead, restore the removed `ROUTE_TRIAL` relay).
3. Codex: installer/build-script wording for the unsigned beta (list above).
4. RP-10 needs the fork to report the quit reason.
5. Keyless settings signature → DPAPI-keyed HMAC (design + migration) if real tamper-evidence is wanted.
6. VM: sleep 1 min / resume with and without a session (no SAFE MODE); kill the process between the settings and signature writes on a copied production install.
