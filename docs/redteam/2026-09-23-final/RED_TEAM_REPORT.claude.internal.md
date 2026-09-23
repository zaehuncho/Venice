# Claude internal red-team report: frozen rc1 (2026-09-23)

**Claude lane verdict: blocked.** This covers only the Claude integration lane (F1–F8). It is **not** an approval of the whole release; only the owner/Codex release gate can approve the exact package.

- **Blocked** because of three HIGH install/update defects in F7, and because the package gates are not run on rc1: Standard, StrictSecurity, and the standard-user VM canary.
- I found **no** auth unlock without entitlement, **no** tampered or unsigned update install, **no** secret disclosure, **no** stuck-input path and **no** silent fire on a missing or wrong meter.
- Per-lane verdicts: F1 needs changes, F2 needs changes, F3 needs changes, F4 needs changes, F5 needs changes, F6 needs changes, **F7 blocked**, F8 needs changes.

| Field | Value |
|---|---|
| Candidate | `CANDIDATE_SHEET_rc1.md`. Venice source `59129e5`, fork `67eec725`, frozen tree `C:\Users\aaron\VeniceRC\rc1-20260923` |
| Hashes re-verified (all equal the sheet) | Installer `d0383f12…8d50`, ZIP `209bd83f…7567`, `update_manifest.json` `2652b3d6…ef5f`, `release_manifest.json` `1083ea69…e950`, `.sig` `63acba1c…524d`, `security_policy.json` `1ffa1ef7…5188`, `OrionNative.exe` `40d7afb4…711d`, `OrionUpdater.exe` `838cace7…6e1b`, `OrionSidecar.exe` `6e3bf77f…451e`, `OrionStream.exe` `1f659a46…0897`, and the 7 native DLLs |
| HEAD check | `ea8729b` changes only `docs/redteam/2026-09-23-final/CANDIDATE_SHEET_rc1.md` relative to `59129e5` (`git diff --name-only 59129e5 ea8729b`). Fork HEAD is `67eec725` with no modified tracked files |
| Lane evidence | `claude_lanes/F1.md`–`F8.md` hold the full reproductions, literal outputs and closure tests. Scratch files are in `%TEMP%\claude-F1` … `claude-F8`. Integration identity evidence is in `%TEMP%\venice_rc1_claude_redteam\identity.txt` |
| Boundaries kept | Read-only on source, fork, candidate and the owner's settings/learning. No OrionNative launch, installer run, commit, deploy or signing. No live Stripe, Discord, AWS, Cloudflare, PSN or 2K calls. Backend tests ran only as focused files that had been read first, under the process-local offline guard (`A7_OFFLINE_GUARD_BLOCKS 0`); this is not OS-level egress containment. |

### Changes made after the freeze (not part of rc1)

The working tree changed after rc1 was frozen. **None of these changes is in the rc1 bytes, and none is credited as a fix here:**

- `installer/orion.iss`: dark style and copy edits. I checked that it does **not** touch `DisableDirPage`, `[Dirs]` permissions, `UninstallDelete`, `CloseVeniceProcesses` or `OrionStream`, so CL3-F7-003 and CL3-F7-007 are unchanged.
- New `installer/assets/venice-wizard-*`, the deleted old wizard bitmaps, and untracked `tools/installer/`.
- `native_orion/tests/AutomationEngineTests.cpp`: modified by another actor while this report was being finalized. It is not reviewed here and not in rc1.
- `tests/test_remote_play_client_stale_sweep.py`: a post-freeze edit that addresses CL3-F3-001. It still needs independent verification in the next candidate.
- `CANDIDATE_SHEET_rc1.md` itself has an uncommitted post-freeze note and a correction to the "no strings" row. The sheet line "clean except `learning.json`" is now stale.

A previous draft of this file (13:23) listed a working-tree "fix" for CL3-F3-001. This version supersedes it, because that fix is outside rc1.

---

## Immediate owner notices

1. **Privilege boundary (CL3-F7-003).** The installer lets the customer choose any install directory, applies no ACL, and registers the LocalSystem `VeniceNetSvc` from `{app}\packet_bridge`. On a user-writable parent (for example `D:\` or `C:\`), a standard user can reach SYSTEM. The default Program Files install is not affected. Tier: source-confirmed; the ACL on the chosen folder is not proven on a VM.
2. **Updates never apply on a default install (CL3-F7-001, M-14).** `QFileInfo::isWritable()` ignores NTFS ACLs, so no UAC prompt appears and the unelevated updater cannot stage an update. This fails closed, but emergency and key-rotation updates cannot reach customers.
3. **StrictSecurity is predicted to fail** on `OrionRouteTransitionTests`: it passes 3 of 5 in the production profile and 5 of 5 in dev (CL3-F2-001 = CL3-F3-004). The cause is a fixture defect: the model is missing beside the test EXE, and the product correctly fails closed.
4. **The 02:28:59Z "unexplained" OrionStream exit code 1 was very likely our own test suite** (CL3-F3-001). This is a strong hypothesis, time-correlated to 40 ms, with no initiator record. Do not run broad pytest while Venice is streaming.

No finding required stopping another lane for an auth unlock, tampered update install, stuck input, secret disclosure or silent fire.

---

## Release blockers for this lane

| Blocker | Why it blocks | Closure |
|---|---|---|
| CL3-F7-001 (M-14) | The incident-response recovery path ("publish mandatory update") does not work for default installs | VM: a standard user and a UAC admin each get a UAC prompt, the update applies, and the post-update runtime hashes match the new release exactly |
| CL3-F7-003 (M-07) | A standard user can reach SYSTEM on a custom install path | `DisableDirPage=yes` or a Program-Files-only check, plus an explicit `{app}` ACL. VM `icacls` and `sc qc` evidence |
| CL3-F7-002 | A failed apply plus a failed rollback leaves a mixed old/new unit, which violates "one complete previous or new signed unit" | C27/C28 re-run with the lock held and with it released; the inventory must equal exactly the old unit or exactly the new one |
| Gates not run | Standard, StrictSecurity (predicted red, above), the standard-user VM canary, and the final signed publish manifest (CL3-F1-002) | Owner-run on this exact unit, with transcripts |

---

## Findings, ranked from CRITICAL down to LOW (deduplicated by root cause)

Evidence tiers: **PKG** = observed in packaged bytes or behaviour; **FIX** = fixture reproduction; **SRC** = source-confirmed; **OBS-DEV** = observed in the owner's dev-launcher logs, not the package; **HYP** = hypothesis. "Candidate" is rc1 unless stated otherwise.

### CRITICAL

None.

### HIGH

| ID | Tier | Component | Failure path → impact | Fix / closure |
|---|---|---|---|---|
| **CL3-F7-001** (M-14) | FIX (Qt 6.8.0 probe of the exact expression) + PKG (`asInvoker` manifests) | `OrionAppController.cpp:5874-5877`; `OrionUpdater.exe` `838cace7` | The probe reported `C:\Program Files\Venice … installDirWritable=1 elevationRequested=0`, although the ACL is `Users:(RX)`. The launcher then starts the updater without `runas`, staging fails ("Could not stage updater outside install"), and the launcher keeps running the old version. **No optional, mandatory or emergency update installs on a default install.** This fails closed | Use a real write probe or `QNtfsPermissionCheckGuard`, or give the updater a `requireAdministrator` manifest. Closure: VM UAC update with before/after hashes; declining UAC leaves the install unchanged |
| **CL3-F7-003** (M-07) | SRC + parent-ACL observation; VM not run | `installer/orion.iss` (no `DisableDirPage`, no `[Dirs]` Permissions); LocalSystem `VeniceNetSvc` loads `WinDivert64.dll` from `{app}\packet_bridge`; its SDDL lets Interactive Users start it | Custom directory under a user-writable parent → swap the EXE or DLL → start the service → **SYSTEM**. Also, `[UninstallDelete] {app}` erases an existing folder the user chose (data loss) | Fix per the blocker table. Closure: `icacls` shows no user write access, `sc qc` shows a binPath under Program Files, `D:\Venice` is refused, and uninstall keeps sibling files |
| **CL3-F7-002** | FIX (C27/C28, dev-profile updater from the same source) | `UpdaterArchive.cpp:224-229` (delete-then-copy, also during restore), `restoreTree :588-601` (stops at the first failure) | A foreign handle on a runtime file (antivirus, backup tool, orphaned process) gives `Update failed AND rollback failed`. `Qt6Core.dll`, `UpdaterCore.dll` and `release_manifest.json/.sig` stay NEW while the rest is OLD. Integrity then locks automation and a reinstall is needed. (A6-001's self-lock is fixed; this is the foreign-lock case) | Rename-aside or `MoveFileEx`, restore that continues past failures and skips entries already equal, retries on sharing violations, and a durable recovery marker. Closure: C27/C28 produce exactly the old or exactly the new unit |
| **CL3-F8-010** (M-13 ops, M-14 drill) | SRC (grep and file inventory) | Backend `/api/license/check` has no counters; `global_kill` turns off everything or nothing; no mismatch sentinel; no `PATCH_DAY_RUNBOOK.md`; no signed-update drill; no templates; `docs/support/SUPPORT_MACROS.md` is **missing** | On the first 2K patch the owner finds out from Discord, the only lever is killing the whole product (with the wrong copy, CL3-F8-008), and the fix goes through an update pipeline that has never been drilled. **Launch readiness, not a security blocker** | Before launch: a runbook, 4 embed templates, support macros, and one timed no-op signed-update drill. Later: fleet counters, `detection_policy.autonomous=off`, and a mismatch sentinel |

### MEDIUM

| ID | Tier | Finding (component) | Impact | Fix direction |
|---|---|---|---|---|
| **CL3-F2-001 = CL3-F3-004** | FIX (prod `build_prod_review`) | `OrionRouteTransitionTests`: production 3/5, dev 5/5. The fake sidecar has no `models/orion_meter_detector.onnx` beside it, so the production start gate refuses (`RouteTransitionIntegrationTests.cpp:179,248`; `RemotePlaySession.cpp:2271-2288`) | StrictSecurity predicted red. AUD-A2-001/002 are unproven in the shipping profile | Stage a fixture model (or add a sanctioned seam), and keep a separate negative test for the missing model |
| **CL3-F1-002** | FIX + SRC | The frozen `update_manifest.json` has a signed empty `artifact_url`. Publishing needs a new signature over a different payload, so the manifest clients will trust is created after the freeze | A chain-of-custody gap, not a client bypass (Ed25519 and SHA-256 are still enforced) | Re-sign with the final URL and add its SHA-256 to the sheet before the Codex gate |
| **CL3-F7-004** | FIX (C25) | The downgrade gate trusts the CLI `--current-version` (`updater_main.cpp:414`); `--manifest-url` accepts any HTTPS host | Rollback to an older **signed** build once older releases exist. No exposure at 1.0.0 | Derive the version from the verified installed manifest, and pin the manifest host in production |
| **CL3-F7-005** | FIX (C20) | A signed ZIP can install unmanifested files, including Qt plugin directories (`SecurityManager.cpp:578-610` checks listed files only) | The "unmanifested runtime files" class, reached through the update channel. Needs a publisher mistake, not an attacker | Refuse any staged file that is not in the new signed manifest |
| **CL3-F6-001** (M-18) | SRC | `resolve_owner` enforces TOTP only on the break-glass secret path. An **owner-role staff token** reaches `totp_disable` and `rotate_admin_secret` with no fresh code (`lambda_function.py:3481-3512, 4819-4842`) | A stolen owner-role token plus its machine binding plus an allowlisted IP can switch 2FA off. **The lane rated this LOW; I raise it to MEDIUM** because it defeats the exact property M-18 was meant to close | Require the TOTP header for those actions whatever authenticated the caller, and audit `owner.totp_denied` |
| **CL3-F4-009** (M-26, regressed) | SRC + line-for-line model | Calibration Cancel on an Auto install goes through `setActuationLeadMs`, which clamps to ≥150 and forces `user_set=true` (`OrionAppController.cpp:9346-9410`). Model output: `before=(0,user=False) after=(150,user=True)`, while the log says "back at 0 ms" | About 120 ms too little lead is pinned, so every live-tip shot is late (late = miss) until the slider is moved. No tests exist for this path | Snapshot and restore the exact `{lead, user_set, source}` tuple. Add a controller-level test |
| **CL3-F4-007 + CL3-F8-009** (M-13) | OBS-DEV + SRC; **fire safety adjudicated below** | The DETECTION UNAVAILABLE / METER BLIND latch (a) never advances for meterless METER-mode Square presses, because `physicalShotEpoch` is set only in `beginShot`, and (b) can be reset by idle raw sightings before the idle check (`OrionAppController.cpp:11731-11749`). Two meterless Go-To pushes also clear it | On patch day the bot silently stops timing and the customer gets **no** unavailable state. In the live wrong-style control (80 presses, 0 releases) the latch never fired | Feed the latch from the engine's `press_unanswered_no_meter` epoch, count only in-epoch detections, and clear it only on vision-owned shots |
| **CL3-F4-008** (M-25) | OBS-DEV (6/6 applied FF epochs trained) + SRC | FF-displaced releases still train the press→tip, no-meter hold, per-type clock and velocity-prior learners (`AutomationEngine.cpp:22306, 22372, 13357, 13364, 13404`) | Slow one-directional drift of the NO METER law and the persisted priors. METER-mode tip timing is not directly affected | Add a `lastReleaseOnsetFfMs_ == 0` fence at those sites |
| **CL3-F4-006** (M-12) | SRC; live effect HYP | `loadLearningObject` accepts any finite value for `bias_pct`, `learned_latency_ms` and the per-type maps. The `.bak` rotation copies any syntactically valid file | A semantically corrupt `learning.json` can silently move the aim and rotate into "last good" | Band every field at load, and never rotate a file that fails the semantic check |
| **CL3-F5-007** (M-33, CL2-P3-007) | FIX | If the explicit pick is busy, adoption falls to another "card"-named device, and `identity_verified=True`. The hints `"capture"` and `"avermedia"` classify `screen-capture-recorder` and an AVerMedia webcam as cards | The customer's explicit choice is not authoritative; timing can run on another device. A wrong fire still needs meter-shaped pixels | When a selected ID exists, adopt only that ID; narrow the hints |
| **CL3-F5-008** (A4-001, wider) | FIX + SRC | The device inventory is frozen for the whole sidecar lifetime (no `WM_DEVICECHANGE`). After an unplug/replug, a reopen at the index yields `physical Integrated Webcam basis configured identity_verified True` | A webcam can be granted capture identity. **The sheet's accepted "same-second hot-plug" wording understates this: the window is the whole session.** The owner must re-accept with the wider wording or fix it | Take a fresh inventory for every reopen, or restart on device change. Long term: a moniker-bound open |
| **CL3-F5-006** (M-33) | FIX | A 30 Hz request qualifies a 30 fps feed (`min_fps = requested × 0.85`). The timing stack was fitted only at 60 | Fire authority on an unvalidated route | Qualify against 60, or make 30 Hz preview-only |
| **CL3-F3-002** (CL2-P2-005) | OBS-DEV (candidate fork) + FIX | Every normal capture-card close is recorded as `forced_by_parent`/`child_fault` and pinned. With `_MAX_BUNDLES=4`, four normal closes evict a real incident bundle | Undoes AUD-A3-002; support cannot tell a normal close from a fault | Send an ownership release before the pipe close, record the parent's intent, and pin abnormal bundles separately |
| **CL3-F3-001** | SRC + FIX (fakes) + field correlation | `tests/test_remote_play_client_stale_sweep.py:145-155` calls the real sweep, which runs `TerminateProcess(h,1)` on every OrionStream, and skips only *after* the kill | Kills live owner sessions and produced false disconnect evidence (see AUD-A3-001). **No customer-package impact** | Make the test non-destructive, add a guard fixture that forbids killing foreign PIDs, and mark real-process tests `slow`. (The post-freeze edit is not rc1 evidence) |
| **CL3-F8-005** | SRC + PKG | Four owner decisions contradict each other across shipped copy: installer signed vs unsigned, whether Xbox is experimental or unsupported, trial in Discord vs on the home page, and whether Remote-Play-only is supported (including the in-app `StreamSetupForm.qml:263`) | Customers make purchase decisions on contradictory scope; refund and dispute exposure | The owner decides each one; generate all channels from one fact table |
| **CL3-F8-006** (M-36) | SRC + PKG | `settings.json` and its `.sig` are two separate commits. After a crash in between, production locks with "Settings signature missing or invalid", there is no repair action, and Connect cannot heal it | A paying customer is stuck after an ordinary forced reboot | Save and sign atomically, and add a customer "Repair settings" action |
| **CL3-F8-007** (M-35) | SRC | The freeze watchdog uses wall-clock time, so every sleep/resume latches SAFE MODE ("UI thread froze…") and uses up one of the two auto-recoveries | Fails closed, but a day-1 ticket generator | Use a steady clock and handle `PBT_APMSUSPEND` |
| **CL3-F8-008** | FIX (real `hintFor` under node) + SRC | The activation kill returns `service_disabled: <reason>` as the error code; the client and `hintFor` map it to "Check your internet connection" | On kill-switch day every customer is told their internet is broken | Send a separate code and message, and add a client mapping |

### LOW

- **Release hygiene**
  - CL3-F1-001: `DEPLOY_LOG.txt` (internal IDs and incident history), QML dev comments and a build path ship in the package.
  - CL3-F5-001: PDB and `__FILE__` user paths appear in every PE. `VeniceNetSvc.exe`'s PDB path points to the **dev** build dir; its build profile should be confirmed.
  - CL3-F1-005: a dead Python helper with developer interpreter discovery ships in `RemotePlayCore`.
- **Environment surface** (CL3-F1-004 = CL3-F4-002): about 70 unfenced timing and fire-policy `ORION_*` env overrides are honoured by production `AutomationCore`. No auth effect, and no blind fire in METER mode. The sheet row "no `ORION_LEAD_*` strings" was literally wrong: the strings appear as message text, while the env reads are fenced.
- **Publication** (CL3-F1-003, AUD-A6-003): packager publication survives exceptions but not process death. Missing paths and orphaned `.previous-*` files are left, and there is no journal. Old and new files never coexist at the fixed paths.
- **Updater**
  - CL3-F7-006: `../`, leading `/` and `..\` entries are refused only as "truncated/corrupt", which relies on a Qt quirk.
  - CL3-F7-007: the installer does not close OrionStream, and the updater's owner list names `venicenet.exe` instead of `VeniceNetSvc.exe`.
- **Input**
  - CL3-F2-002 (M-32 residual): the Qt keep-alive can re-press a released button. Not observed: 0 `edge_enqueued source_seq=0`.
  - CL3-F2-003: on pipe loss, gameplay is mirrored to the desktop XUSB pad for 100–170 ms.
  - CL3-F2-004: misleading Square-up audit, UI-isolation and normal-quit log lines.
- **Session**
  - CL3-F3-003: incident-bundle redaction erases cause codes.
  - CL3-F3-005: in-place recovery gives up after about 6 s, while the console holds the session for 13–18 s.
  - CL3-F3-006: telemetry-silence aging across a sidecar restart has no fixture.
  - CL3-F3-007: messages name the configured console IP, not the effective one (M-34).
- **Timing**
  - CL3-F4-001: the oracle path lacks the dev-offset fence (dev builds only).
  - CL3-F4-003: Dial, Straight, Sword and Arrow are still accepted and bundled. Only Pill is excluded; the UI offers Arrow2 only.
  - CL3-F4-004: the mid-range left fade uses +6 over the −6 left-fade default. Inert while range is 100 % unknown.
  - CL3-F4-005: shot records carry no build or settings identity.
- **Capture and first run**
  - CL3-F5-002: the saved device ID is not used to re-resolve a reordered index (fails closed, with misleading copy).
  - CL3-F5-003: a false "duplicated frames" hardware alarm on a healthy HD60 X.
  - CL3-F5-004: the compiled sidecar still honours `%USERPROFILE%\Desktop\NexusVision\settings.json`, so owner-PC package tests are not fresh-machine tests.
  - CL3-F5-005: the Windows 10 font fallback can truncate "Calibrate my lead".
- **Money**: CL3-F6-002 (M-23 residual): no Stripe `event.id` dedup at the Worker. Entitlement stays idempotent; replays duplicate alerts and audit rows.
- **Customer copy**
  - CL3-F8-001: the raw transport-watchdog text reaches the feed and the SAFE MODE dialog.
  - CL3-F8-002: detection-unavailable copy is jargon, and on patch day it points at the wrong cause.
  - CL3-F8-003: the safe-mode dialog contradicts the Activity line, and the status pill is never visible.
  - CL3-F8-004: "Remote is already in use" is unmapped, so customers get a Connect loop.
  - CL3-F8-011: the Discord bot copy asks for a key, says "Chiaki" and "defaults work best".
  - CL3-F8-012: a clock years off gets the "check internet" hint, and a backward clock step still drops frames (CL2-P8-004).
- **Integration (this report)**
  - CL3-INT-001: gate evidence does not describe rc1. `build_prod_review` had no CTest record since 09-21 before this round (`Testing/Temporary/LastTest.log`, 09-21 15:16). The sheet's play test ran on dev launcher `D2A9920F`, which is no longer the dev build on disk (`9a8a7a3e`, 12:39). The engine and controller sources changed at 12:28, mid-session. So the owner's 80 % EXC session is evidence for neither the rc1 package nor the rc1 engine source. Fix: put run records of the production-profile tests in the sheet, and stamp builds in shot records (CL3-F4-005).

### Adjudicated disagreement between lanes (CL3-INT-002)

F8 (CL3-F8-009, and the composition in CL3-F8-012) claimed that a starved latch lets "the backstop keep releasing presses on the fixed hold table". **I checked this against rc1 source and it is refuted for METER mode.**

- `AutomationEngine.cpp:2129` hard-codes `config_.greenWindowPriority = true`.
- `maybeFireMeterBlindBackstop` then returns false at `:21936` (`if (config_.greenWindowPriority) { … "METER VISION WAIT … blind_release_suppressed=1" …; return false; }`) before any fire.
- This matches F4's fixtures (`meterModeNeverAnswersAnUnownedPressBlindly` and siblings, 22 passed / 0 failed / 1 skipped) and the live wrong-style control (80 presses, 0 releases).

So CL3-F8-009 is kept as a **visibility and latch** defect merged into the M-13 row, not as a silent-fire finding. Command: `git show 59129e5:native_orion/src/AutomationEngine.cpp`, lines 2129, 21813-21845 and 21930-21945 read.

---

## Evidence by lane (summary; full detail in `claude_lanes/`)

| Lane | Verdict | Key exact evidence | Main untested gap |
|---|---|---|---|
| F1 coherent unit | needs changes | 578 files = 576 manifested + manifest + sig. 184/184 native and 63/63 sidecar files byte-equal to their build outputs. `OrionStream` equals the fork build, and `ninja -n` reported "no work to do". Sidecar `--verify` exit 0. `verify_release_integrity … RESULT: PASS (576 files)`. Update-manifest signature VALID; edits to `artifact_url`, version or rollback → INVALID. ZIP equals the tree with no unsafe entries. No Owner/Staff, `.pdb`, tests, `pill.json` or `update_pubkeys.json` in the package. One embedded key, `orion-ed25519-v1`. `security_policy.json` is fail-closed and production hard-codes the manifest requirement | Installer payload inferred, not extracted. The final publish manifest does not exist yet |
| F2 input/route | needs changes | Production-profile InputProtocol 54/54, Retry 40/40, RemotePlayPath 18/18 (+2 skips), Xbox 12/12. `chiaki-unit` 165/165, pipe harness 11/11, managed-policy and retention passed. Every fault ends neutral: `owner_silent_neutralized silent_ms=407`, soft-fault budget 3/3 → `terminal_neutral confirmed=1`, hold expiry 2018 ms. Live Square: 25/25 bot releases with `max_out_sq=0`, no re-press | No real client ↔ real bridge composition (AUD-A3-005). No console receipt (`console_ack=0`). No Square+Triangle→Cross fixture |
| F3 decoder/session | needs changes | Recovery keeps authority revoked (`controller/fire authority revoked`, `PRESS UNDELIVERABLE … sent=0`, fresh route proof before re-arm). Exit-evidence tests 10/10 pass despite CL3-F3-002 (a coverage gap) | No decoder/PTS or capture-pipe soak. No RP-only session. No run of the packaged unit |
| F4 timing/reader | needs changes | A1-001/002/003 closed by fixture (`final_zero`/`trim_bound` strings present in the package DLL). FF, dev-sweep, late-carry and `LEAD_*` env reads absent from production. Corrupt (unparseable) `learning.json` is quarantined. Left fade −6 plus migration verified. Scoreboard 25 graded at 80/0/20, **not at a matched build** | The compiled sidecar was not replayed on any fixture. No framedump replay. The test binaries used were dev-macro builds |
| F5 capture/first run/perf | needs changes | Authority is withheld for: no card, busy, webcam, unpicked generic, low/gappy fps, and the MSMF fallback. No D: → LOCALAPPDATA, and shot records are OFF in the compiled build (M-29 closed). Log-sink `failingStorageNeverBlocksProducerOrShutdown` 6/6. Offscreen render at 150 % shows no overflow with Segoe fonts | No VM without D:. No hardware hot-plug, OBS or 30 Hz drill. No rendering of the packaged app |
| F6 licence/money/admin | needs changes (package approval depends on live/owner gates) | A5 15/15. RBAC/webhook/device-cap/admin 169/169. Pair-status route 1/1. Worker 47/47 (all guard 0, exit 0). `LeaseGate` is always enabled in production; `ORION_SKIP_UPDATE_GATE` is dead (`CMakeCache.txt` probe count 0 in the EXE). Only `https://api.zaeorion.com`. Kill-switch read fails closed. Durable nonce with ±300 s skew. `audit_unavailable` refuses the action. Device cap = 1, atomic | Live Stripe, Discord, Gateway route and IAM/WAF posture. Real two-PC lease. No fixture for the TOTP bypass yet |
| F7 install/update/recovery | **blocked** | The production updater refuses local manifest/artifact flags (exit 2); embedded key fingerprint = pinned key. 29-case matrix: signature, key-id, alg/HMAC, SHA-256, URL, http, downgrade, traversal, absolute, symlink, libcrypto and release-manifest tamper all refused with the install digest unchanged (`f9070139…`). Injected post-prune fault → exact rollback | VM install/UAC/uninstall/driver canary. Production-key TLS update. The matrix used the dev-profile updater from the same source (the production binary refuses local fixtures by design) |
| F8 customer states/patch day | needs changes | Lease-lapse banner and heartbeat policy 23/23. ActivityFeed 15/15. `timestamp_expired` → clock copy. Heartbeat kill → deauthenticate. Update-required copy. Copy fixes present in the rc1 EXE (`scan.py`) | Nothing rendered. No sleep, Wi-Fi, clock or reboot drills. `SUPPORT_MACROS.md` does not exist |

---

## M-01 – M-41 matrix

Dispositions: **closed** = closed by exact evidence (tier noted); **open** = still open; **N/A** = not applicable; **NT** = not tested.

| M | Disposition | Basis |
|---|---|---|
| M-01 | **open** | The identity half is closed (PKG byte-equality, F1). The gate half, Standard + Strict on this unit, has not been run |
| M-02 | closed (FIX, dev build of the same source) | Sink 6/6. Disk-full on the package: NT |
| M-03 | closed (FIX, fork) | Budget/hold fixtures. Observability residual CL3-F3-002 |
| M-04 | closed (FIX) | Dead-man 407 ms. Live tick gap 151.5 ms mid-play is bounded |
| M-05 | NT | Pipe name / peer image check not exercised |
| M-06 | NT | Service token and ProgramData ACL need the VM |
| M-07 | **open** | CL3-F7-003 |
| M-08 | closed (SRC) | `main.cpp` mutex plus window activation. Two real launches: NT |
| M-09 | closed (FIX, policy) | Heartbeat 23/23. Live sleep/Wi-Fi: NT |
| M-10 | closed (FIX, production) | `triggerReleaseSplitCarriesOnlyTerminalEdges`, `triggerHeldBlocksOnlyRouteRecovery` (InputProtocol 54/54) |
| M-11 | **open** | No event-loop fixture. CL3-F3-006 |
| M-12 | **open** | CL3-F4-006 |
| M-13 | **open** | Fire is fail-closed (verified, CL3-INT-002). No unavailable state (CL3-F4-007/F8-009). No ops (CL3-F8-010) |
| M-14 | **open** | CL3-F7-001; drill CL3-F8-010 |
| M-15 | closed (FIX) for the generic, webcam and index-0 cases | Residuals CL3-F5-007/008 |
| M-16 | NT | Reader and Go-To late-meter fixture not run (Astra) |
| M-17 | NT | No RP-only session. Copy overclaims (CL3-F8-005) |
| M-18 | **open** (partial) | CL3-F6-001 |
| M-19 | NT | DLL search order not exercised |
| M-20 | NT | Owner policy |
| M-21 | closed (SRC) | `kill_state_unavailable` fails closed |
| M-22 | closed (SRC) for the traced config, kill and staff routes | Completeness across all routes is a source claim |
| M-23 | mostly closed (SRC + FIX) | Residual CL3-F6-002 |
| M-24 | closed (PKG strings) for its scope | Broader env surface CL3-F1-004 |
| M-25 | **open** | CL3-F4-008; oracle half CL3-F4-001 (dev only) |
| M-26 | **open (regressed)** | CL3-F4-009 |
| M-27 | NT | Cancel can now *install* the floor (CL3-F4-009) |
| M-28 | closed (FIX) | `sender_exit_*_is_fatal`, `sender_normal_stop_publishes_once` |
| M-29 | closed (FIX) | Compiled default OFF; LOCALAPPDATA fallback |
| M-30 | NT | — |
| M-31 | **open** (SRC) | The CL2-P2-002 fini race is unchanged in the fork |
| M-32 | **open** (residual) | CL3-F2-002 |
| M-33 | **open** | CL3-F5-002/006/007; plain errors partial (CL3-F8-001) |
| M-34 | **open** | CL3-F3-007 |
| M-35 | partial | Server nonce and skew closed (SRC). Resume SAFE MODE (CL3-F8-007) and backward step (CL3-F8-012) open |
| M-36 | **open** | CL3-F8-006 |
| M-37 | NT | — |
| M-38 | N/A | A single binding with an atomic cap of 1 is what is sold |
| M-39 | NT | — |
| M-40 | **open** | Range is 100 % unknown; mid-fade hazard CL3-F4-004 |
| M-41 | NT | No corpus or framedump replay |

## 09-23 fixup (AUD) dispositions

| AUD | Disposition |
|---|---|
| A1-001, A1-002, A1-003 | closed (FIX; A1-002 also PKG strings) |
| A2-001, A2-002 | **open (partial)**: production route fixture 3/5, and no controller is constructed |
| A2-003 | closed (FIX, production build: 54/54) |
| A2-004 | NT beyond the sink test |
| A3-001 | **open**: strong hypothesis that the initiator was the test suite (CL3-F3-001); normal closes are mislabelled (CL3-F3-002) |
| A3-002 | **open**: normal-close pinning plus the 4-bundle cap, and redaction (CL3-F3-002/003) |
| A3-003 | NT |
| A3-004 | closed (policy fixture), not live |
| A3-005, A7-006 | **open**: no integrated package/GUI/hardware composition |
| A4-001 | **open, wider than the accepted wording** (CL3-F5-007/008) |
| A4-002, A4-003, A8-003 | NT in this round |
| A5-001 | fixed in source/fixtures; **owner Gateway route application still owed** |
| A5-002, A5-003, A5-004 | closed (SRC + FIX) |
| A6-001 | closed for the self-lock (FIX C26); **open for foreign locks** (CL3-F7-002) |
| A6-002 | closed (FIX C00/C26) |
| A6-003 | **open (partial)** (CL3-F1-003) |
| A7-001 | NT (harness) |
| A7-002 | supporting fixture only (production SHM tests 2/2) |
| A7-003 | consistent (the no-D: and unwritable-root cases pass) |
| A7-004 | valid but process-local, as the fixup itself states |
| A7-005 | NT |
| A8-001 | deferred (owner) |
| A8-002 | Pill is dormant and excluded; the other uncertified styles are still accepted (CL3-F4-003) |

---

## Tests still required before this lane could move off "blocked"

1. Fix CL3-F7-001, CL3-F7-002 and CL3-F7-003 in an owner-authorized patch phase. Then build a **new** frozen candidate.
2. Owner, on that exact unit:
   - Standard and StrictSecurity, with `OrionRouteTransitionTests` passing 5/5 in the production profile;
   - the final signed publish manifest's hash recorded in the sheet.
3. Standard-user VM canary:
   - default and custom-directory install, with `icacls`/`sc qc` evidence;
   - a UAC update as both a standard user and an admin, with before/after hashes;
   - C27/C28-style locked-file apply;
   - an orphaned OrionStream and a running VeniceNetSvc during update;
   - three uninstall modes;
   - no D: drive;
   - no Desktop settings file.
4. Owner rig:
   - a packaged-unit play session;
   - capture unplug/replug with a webcam attached;
   - two cards with an explicit pick;
   - a 30 Hz selection;
   - sleep/resume;
   - reboot mid-save;
   - "Remote is already in use".
5. Before launch: a patch-day runbook, templates and support macros, plus one timed no-op signed-update drill.

## Deduplicated merge with Codex and Gemini

**Deferred.** The prompt pack requires both Codex reports (`RED_TEAM_REPORT.codex.security.md` and `RED_TEAM_REPORT.codex.reliability.md`) and Gemini's report before a merge. The Gemini internal report now exists, but neither Codex report was present when this was written, so I have not proposed a merge. I also do not certify my own earlier patches: every "closed" above rests on fixture, package or source evidence gathered in this round, not on implementation claims.
