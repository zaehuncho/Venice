needs changes

# A3 — End-to-end input path and the 02:28:59Z disconnect

## Boundary and evidence identity

Read-only source review of `C:\Users\aaron\Desktop\NexusVision` and `C:\Users\aaron\Desktop\chiaki-ng-src`; only this report was written. No application launch, settings change, live-service request, deployment, source patch, or build was performed. Existing isolated unit/pipe executables were run. The fork was being edited/built concurrently; results below are tied to executable hashes, not an assumed immutable working tree.

NexusVision HEAD observed: `0eca7d2251f92d2e94edcd769c5fc96172def5dd`. Fork HEAD: `c7515213abbebd2e28eb20dfb2eed9e0eb769dcb`. Both trees are dirty. Final source recheck at03:05Z (historical test hashes remain separately recorded below):

| Fork file | SHA-256 |
|---|---|
| `gui/src/streamsession.cpp` | `B4737F8231FA89B248204B4CE7755471DCAE33A76B2B31926E73D1813591C189` |
| `gui/src/qmlcontroller.cpp` | `558DE0F8F4AF6EC966CD22F9F02E5D8E82A21DDA5980DC0D5C2A23181F00B911` |
| `gui/src/sessionlog.cpp` | `769EDE5141E3445EEC4E63133EF942A1AFC0737E3F8B9DCFDD2B23EF6C5A1381` |
| `gui/src/orioninputbridge.cpp` | `9069DF3046F3DC8BA31ABEEBC6185C626BF6574E584459EAE3B96197FBE00839` |
| `gui/src/orionexittrail.cpp` | `EFD701A784399A9475379DF51968840113530304B61F53AFC895F8A28BBBCF91` |
| `gui/src/orioninputbridgesession.cpp` | `C9719043D1E9F6540D18D7B4A45EF681517BDF27D2CC127A20F518FF475F86FB` |
| `gui/src/qmlbackend.cpp` | `FC3E043986CCED2315304BE553F65A60922EEE461488A9446FC1E6BACC9AB751` |
| `gui/src/main.cpp` | `06550E9F5B2AF2E6712DA943EC27A36F45C729E422BEA9FDFE03751F00BDF6EE` |
| `lib/src/feedbacksender.c` | `9FED28693093C11CCDF35F55BF1A215DDCD796629E412576D293BA7F9FA8ACC6` |

The deployed `native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` initially hashed `D38DC7C90C79B80A8EE1CFC3569D291302A013879A0BA9C9E779FABA2F86D617`, matching the incident's native identity record. At the final recheck it had been concurrently replaced with SHA-256 `397017FAB105DA33AEA012B9C9DDB54009D13FDDE090A0925812588F13DDCAD3`; this auditor did not launch or deploy it. The newly reviewed source, release re-sends, and real-pipe fixture do not identify the old binary's termination cause or prove behavior of this newly deployed GUI executable.

Read the master M-list, prior Codex reliability report, roadmap, and relevant variance evidence. M-03/M-04/M-28/M-31/M-32 and the prior missing real-pipe gate are retained references, not relabeled discoveries. The historical 425-release fire-to-wire result remains evidence about PC egress timing in that capture, not console receipt, all-build reliability, or proof about this disconnect.

## Incident reconstruction: established facts, not a guessed cause

Native evidence: `C:\Users\aaron\Desktop\NexusVision\logs\orion_native.log:23000-23020`:

| UTC | Observation |
|---|---|
| 02:28:57.311 / .427 | Options down/up; `route=pipe hook=1`. |
| 02:28:58.835 | Pipe heartbeat connected; writes=55318; failures=0; ACK failures=0; last exact ACK seq55317; ABANDON count=0. |
| 02:28:59.466 | Second Options down, still labeled pipe. |
| 02:28:59.470 | Controller route generation changes; direct pipe becomes unavailable. |
| 02:28:59.520 (relayed .524) | Sidecar observes `INPUT LINK LOST: reason=process_exited pid=60604 exit_code=1`. |
| 02:28:59.584 | In-place input recovery begins, preserving capture/detector. |
| 02:28:59.836 | Disconnected heartbeat; writes=55324; failures=1; ACK failures=0; ABANDON count=0. |

The named Chiaki log was initially present at `C:\Users\aaron\AppData\Roaming\Chiaki\Chiaki\log\chiaki_session_2026-09-22_21-08-12-490490.log`, length 2,571,683 bytes. Its final inspected lines were ordinary seq55317 state/local-delivery/ACK messages at local `21:28:58:693693` (=02:28:58.693Z), with `console_ack=0`. No `Session has quit`, `Ctrl stopped`, fatal delivery, owner-silence, or ACK-write failure marker appeared in the inspected incident log. It recorded an Xbox 360 virtual controller and the pipe connection. **The log disappeared during this audit before a full-file hash was captured; the earlier inspected excerpts remain tool-output evidence, not a preserved immutable file.** See AUD-A3-002.

Three next logs (`21-28-59-791791`, `21-29-02-026026`, `21-29-04-658658`) record `0x80108b10 (Remote is already in use)`. This is consistent with a not-yet-released console session, not identification of what killed the process. Read-only Application event query for IDs 1000/1001/1002 in local21:28:45..21:29:15 returned no matching records; absence is not proof that no unreported runtime fault occurred.

`orion_native.log:23050` schedules AUTO-RETRY at02:29:05.876; the later `Connect pressed` line at02:29:07.872 immediately follows `AUTO-RETRY firing`. Therefore **that line is not evidence of a manual click**. A subsequent startup watchdog at02:29:18.148 restarts detection after transport age10280ms; A2 should own startup/watchdog analysis rather than count it as a second independently established A3 cause.

**Conclusion:** abrupt process exit is confirmed; its initiator is unresolved. External force termination is consistent with exit1 plus missing clean-session markers and delayed reconnect. The retained record does not prove that route, an Options-induced GUI close, a crash, or any specific caller. The report of the same issue on earlier builds further argues against attributing it solely to tonight's changes without an earlier matched trace.

## Findings

### [AUD-A3-001] high — Abrupt OrionStream exits have no authoritative initiator/exit-reason record
- Kind: reliability
- Evidence: incident timeline above. **Concurrent partial fix observed at03:05Z:** `gui/src/main.cpp:366,378,518` now writes process-exit status; `orionexittrail.cpp` adds an atexit fallback, while `qmlbackend.cpp:525,1198,1294-1325,1711` and `orioninputbridgesession.cpp:36` add teardown/session/window/bridge cause notes. This improves child-side evidence and supersedes the earlier claim that event-loop returns have no final record. It does not supply the missing parent initiator ledger or classify forced/runtime loss. `gui/src/sessionlog.cpp:51-65,68-96` already flushes on shutdown **and every line**, so merely adding `flush()` is not a fix. Launcher `remote_play_client.py:575-583` explicitly uses `TerminateProcess(handle,1)`, while multiple manager stop/failure/standby paths call `Popen.terminate()/kill()`. Those paths do not share a correlated, always-on termination ledger. No mid-session explicit `exit(1)` was found in the inspected fork GUI/library paths.
- Customer impact: a paying user loses input mid-game and may face repeated already-in-use reconnect failures; support cannot distinguish an intentional local kill, transport shutdown, GUI action, or runtime failure. The same symptom can recur without yielding a decisive trace.
- Proposed change: add a small structured termination ledger shared by native/sidecar/fork: process ID **and creation stamp**, session generation, initiating subsystem, request time, reason enum, requested exit code, graceful/forced flag, call result, observed exit code, final input seq/ACK stage, bridge ownership and queue state. Emit before every force-kill or job close and before every session-stop request. Retain and validate the new child exit trail; add any missing `aboutToQuit` coverage and keep a small crash-resistant ring/sidecar breadcrumb independent of the bulk per-frame log. Preserve stderr and fatal-handler output; never include credentials or raw input payload history. A process forcibly killed cannot flush its own final line, so the **parent's pre-kill line is mandatory**.
- Verification: local fake-child integration matrix for clean close, confirmed GUI stop, bridge terminal fault, sidecar deadline reaper, tracked-PID kill, startup failure, and unclassified external disappearance. Assert each yields exactly one unambiguous classification or explicitly `external_or_runtime_unknown`, never infer clean shutdown solely from process exit. Re-run against the exact packaged executable set. Keep the original incident open until the next trace identifies its initiator.
- Evidence qualification: The residual parent-ledger gap and incident are confirmed; the specific termination mechanism remains speculative. New `gui/include/orionexittrail.h:10-13` overstates that absence of `process_exit` means external termination: abort, fatal runtime failure, output/storage failure, or an uninstalled/unreached handler can also omit it. Classify absence as unknown, not external proof. Normal-return/atexit coverage is source-reviewed only; no full GUI binary was launched.
- Confidence: confirmed

### [AUD-A3-002] medium — Reconnect churn can evict the incident log before support collects it
- Kind: reliability
- Evidence: fork `gui/src/sessionlog.cpp:189,205-244` prunes all but five existing logs whenever a new session filename is created, without reserving an abnormal-exit log. This audit initially read six retained files including the incident, then the incident path was absent at02:52:27Z and only the five newer reconnect/session logs remained. The deleting actor was not traced, but the five-file policy itself is confirmed and allows precisely this loss.
- Customer impact: several failed reconnects replace a long problem session with short startup failures; the evidence needed to fix the customer's disconnect disappears.
- Proposed change: on unexpected process exit, copy the closed incident log plus bounded native/sidecar context to a timestamped incident bundle before any reconnect can trigger pruning. Hash the copied files. Retain by a bounded byte/day budget with at least the most recent abnormal session pinned until successfully exported; do not solve this with unlimited retention. Raise ordinary retention enough to span the configured multi-layer recovery attempts.
- Verification: fixture creates one abnormal session plus more than six short retries, invokes actual retention, and proves the incident bundle/hash survives while old ordinary logs are pruned within the byte budget. Test missing/full/unwritable bundle storage without blocking input/recovery.
- Evidence qualification: The policy and observed loss are confirmed; ordinary retention as the specific deletion mechanism remains probable.
- Confidence: confirmed

### [AUD-A3-003] medium — Initial-session retry timer passes seconds as milliseconds
- Kind: bug
- Evidence: fork `gui/src/streamsession.cpp:42` defines `SESSION_RETRY_SECONDS 20`; `:2466-2468` bounds the retry window with `*1000` but schedules `QTimer::singleShot(SESSION_RETRY_SECONDS / 3,...)`, i.e. **6 milliseconds**, not approximately6.7 seconds. `lib/src/session.c:305-311` starts another session thread on each Start; the retry path also deserves an explicit join/lifecycle test. This is pre-connection recovery, not a proved cause of the mid-game exit (the incident was already connected).
- Customer impact: rapid connect failures can generate closely spaced retry attempts, noisy logs, and avoidable reconnect churn while the console still considers its prior session busy.
- Proposed change: choose one owner for retries (prefer launcher policy in embedded mode), or apply the minimal unit repair below and explicitly join the previous attempt before restarting. Do not simply increase all startup budgets.

```diff
--- a/gui/src/streamsession.cpp
+++ b/gui/src/streamsession.cpp
@@
-				QTimer::singleShot(SESSION_RETRY_SECONDS / 3, this, &StreamSession::Start);
+				QTimer::singleShot(SESSION_RETRY_SECONDS * 1000 / 3, this, &StreamSession::Start);
```

- Verification: fake-clock Qt event-loop test injects immediate initial-session errors and asserts no second Start before6666ms, bounded attempt count within20s, one live session thread, cancellation when Stop/destruction occurs, and no retry after an established connection quits. For launcher-owned embedded retries, instead assert the fork does not retry internally and preserves the single authoritative error.
- Evidence qualification: Timer units are confirmed; customer cost depends on which external readiness manager tears down first.
- Confidence: confirmed

### [AUD-A3-004] medium — Embedded fallback controller events can still drive hidden Chiaki menus
- Kind: test-gap
- Evidence: fork `gui/src/qmlbackend.cpp:1942-1969` creates `QmlController` for available SDL devices even in Orion mode; `gui/src/qmlcontroller.cpp:10-25,43-77,116-137` turns gameplay edges into synthetic Qt keys and sends them directly to the window. `qmlmainwindow.cpp:7617-7631` routes non-spontaneous keys to QML even during a session. Defaults in `settings.cpp:1079-1111` enable the **L1+R1+L3+R3 release chord**, which emits Ctrl+O; `qmlmainwindow.cpp:7565-7567` opens/toggles the stream menu. Inline `StreamView.qml:456,483-498` focuses Close, and Return activates it; stop dialog focuses Sleep and Return confirms it (`:953-968`). There is no embedded-mode suppression at this synthetic-key boundary. The concurrent fix at `qmlbackend.cpp:1303-1308` now makes launcher-mode `closeRequested()` stop directly without a dialog or console sleep; this repairs managed WM_CLOSE shutdown but does not suppress the synthetic menu route.
- Customer impact: under a route/configuration where a nonneutral SDL pad remains visible (e.g. XUSB fallback or another controller), game controls can operate a hidden/separate stream menu and terminate the session. The old default Ask/Sleep path is now bypassed for launcher pipe mode, but direct menu Close still stops. This is a conditional UI routing risk, not proof of the reported crash.
- Proposed change: suppress QmlController-generated navigation/shortcuts in launcher-managed embedded mode; retain native launcher input ownership and explicit disconnect UI. Keep standalone Chiaki navigation unchanged. If embedded menus are intentionally retained, require explicit visible UI focus and a separate confirmation gesture, never a gameplay chord alone.
- Verification: offscreen Qt fixture with fake Controller emits Options alone, the default chord, Cross, Circle, stick navigation and repeated held keys in pipe, fallback, extra-controller and standalone configurations. Embedded mode must emit zero menu/close/sleep requests; standalone must retain its intentional controls. Record focus/window/active-overlay state and source-device identity in the next incident trace.
- Evidence qualification: The source path is confirmed; no live GUI reproduction was performed.
- Confidence: probable

**Important negative check:** `Options -> Qt::Key_Menu` is **not** `Ctrl+Q` and active StreamView has no standalone Menu-to-close handler. Healthy direct-pipe mode currently submits neutral ViGEm (`NexusVision/native_orion/src/OrionAppController.cpp:13810-13827`), and `remote_play_client.py:894-911` excludes physical Sony devices from SDL. Therefore the incident's raw `Special button edge: options=1 route=pipe` does **not** establish that Chiaki received a synthetic UI key. A current direct-pipe Options-only path to exit1 was not established. In an already-open menu, Menu closes the separate menu (`StreamMenuWindow.qml:76-79`), not the session.

### [AUD-A3-005] medium — Real-pipe fixture now passes; native-client and GUI composition remain untested
- Kind: test-gap
- Evidence: Prior Codex reliability report left real Windows pipe coverage open under M-03/M-04. Concurrent work now adds `fork/test/orion_bridge_pipe_test.cpp` and `test/CMakeLists.txt:39-45`, using actual bridge overlapped I/O and the real feedback sender with fake UDP/session-stop callbacks. Earlier exact-hash builds failed as recorded below, but the first corrected executable SHA-256 `04714C8BE06512B3D6564FE8392654D768800A536527811BF33C15008523E733` passed **9/9 in six fresh-process runs**. The latest executable SHA-256 `7FD3EE72FED5A56A38531E7AD3B6877A4C07AAAAAEB7D395F2C891A6A8702D05` then passed **9/9 in three fresh-process runs**, each exit0, with the same hash checked before each run and after the last. These are separate build-specific populations, not nine runs of one binary. Thus the prior blanket missing-real-pipe gate and this report's initial high failing-harness classification are superseded. The fixture still uses a scripted client rather than `OrionInputClient` and does not run Qt menus/process lifecycle or a console session.
- Customer impact: The newly covered Windows server recovery scenarios have substantially better local evidence. Remaining native-client/Qt composition errors can still lose input or close a stream despite isolated client/server tests passing; none was demonstrated against a current integrated binary here.
- Proposed change: Keep the corrected stage-synchronized fixture; compose the native client with that server harness and add the offscreen GUI/termination matrix in AUD-A3-001/004. Preserve the historical failed hashes as development evidence, not a claim that the current fixture fails. Avoid timer-only synchronization that depends on one scheduler race winning.
- Verification: Extend the same-hash repeated runs to scheduler-load/cancellation variants, then run the actual native client's ACK timeout, ABANDON, reconnect seed and ownership barrier against the bridge. Assert bounded caller and server shutdown, monotonic sequence/ownership, exactly one terminal stop, and correct parent exit classification. Console receipt remains a separate future gate. Local repeated passing runs are not a production reliability probability estimate.
- Evidence qualification: Three latest-hash runs passed; six historical passing runs belong to the older hash. The composition coverage gap is confirmed.
- Confidence: confirmed

## Complete inspected termination surface map

All fork-relative paths below are under `C:\Users\aaron\Desktop\chiaki-ng-src`. This inventories source-level termination/stop entry points; dependency/internal OS failures are a separate class, not exhaustively enumerable from these files. Line ranges below identify the earlier inspected surface map; the new rows describe the03:05Z concurrent changes and exact current entry points.

| Entry point | Reachability / semantics / expected evidence |
|---|---|
| `gui/src/main.cpp:124-136,238-299` | Explicit return1 for library/audio initialization, missing registration, invalid supplied credential lengths, conflicting fullscreen/zoom/stretch flags or PIN length. Startup only; cannot explain a20-minute established stream without a second process identity mix-up. No credential values should be logged. |
| `main.cpp:200-218,330-352` | Parser error/help(1), optional CLI subcommands returning their own status. Startup/CLI, not normal gameplay. |
| Current `main.cpp:366,378,518`; `orionexittrail.cpp` | Main/stream/standby now record the event-loop return code, with an atexit fallback code=-1. Normal quit remains distinct from forced termination. No inspected explicit in-play exit(1) setter. A missing line cannot prove external termination. |
| `main.cpp:434-449,482-495` | Standby local control quit or failed promotion queues Qt quit. Not controller Options; startup/control command path. |
| `qmlmainwindow.cpp:2533,2575,5168` | SessionQuit -> application quit for stream/deferred/exit-on-stream-exit modes. A session stopping is not intrinsically process exit1. |
| `qmlmainwindow.cpp:2905-2919` | Missing/uncreatable QML root queues quit during initialization. |
| `qmlmainwindow.cpp:4879,5020,5240` | qFatal for OpenGL backend/context initialization failure. Normally logs fatal output; exact OS runtime exit encoding is dependency-specific, not asserted as1. Embedded settings normally select Vulkan. |
| `qmlmainwindow.cpp:6339-6387` | Vulkan device/renderer fallback stops session, then quits for launcher-managed relaunch in Orion mode (or starts replacement standalone process). Renderer/device fault, not ordinary Options semantics. |
| `qmlmainwindow.cpp:7568-7572,7642-7650`; `qmlbackend.cpp:1279-1302` | Ctrl+Q or WM_CLOSE enters closeRequested. Standalone Connected Ask opens dialog; AlwaysSleep sleeps then stops; other policies stop directly. **Current launcher pipe mode** `qmlbackend.cpp:1303-1308` now stops directly without Ask/sleep. Focus loss/exposure alone does not stop; current expose handling intentionally preserves streaming while occluded (`:7669-7674`). |
| `qmlcontroller.cpp`; `qml/StreamView.qml:913-1087` | Controller chord -> Ctrl+O menu; focused Close -> closeRequested; explicit Sleep/No selection -> stopSession. Options alone is not this sequence. |
| `qml/StreamView.qml:1114,1193-1197`; `qml/PsnView.qml:161` | Login PIN / connection dialog rejection or Cancel stops the session. Connection/error UI state is required. |
| `qmlbackend.cpp:558-568,737-745,1679-1696` | OS sleep callback, unregistered-console identity check, explicit stopSession; Stop delegates to chiaki_session_stop. |
| `gui/src/orioninputbridge.cpp:214-263,330-363,560-662` | Fatal enqueue, exact-delivery mismatch/dead sender, missing ACK reader without ABANDON, owned pipe loss without ABANDON; or soft-fault budget/hold expiry, failed neutral/drop/failed ACK escalation. Current production forwarding wrapper moved to `orioninputbridgesession.cpp:34-37`, records `orion_bridge_fatal` and calls session stop. Each terminal branch logs `fatal_local_delivery_fault ... action=stop_session`; no process exit1 call. Ordinary play can encounter scheduler/pipe/transport faults, not only deliberate Disconnect. |
| `orioninputbridge.cpp:367-400` | Event creation failure returns bridge thread only; pipe setup/creation errors retry. This can lose input readiness without directly exiting OrionStream. |
| `lib/src/orioninput.c:186-305` / `lib/src/feedbacksender.c` | Fault policy marks terminal or neutral; sender termination wakes delivery waiters and can make the bridge stop. Neither directly exits the application. Existing M-03/M-04/M-28 scope, not a new root-cause assertion. |
| `lib/src/session.c:520-809`; `lib/src/streamconnection.c` | Initial negotiation/control failure, stop flag, disconnected transport/remote shutdown, stream run errors all converge on session quit event. Typical retained evidence includes `StreamConnection...`, `Ctrl stopped`, `Session has quit`. A remote failure is possible during play, but the incident lacks these terminal lines. |
| `qml/MainView.qml:47,94`; `qml/SettingsDialog.qml:918,985` | User-facing lobby Quit and settings restart/quit controls use Qt.quit. Not Options-only gameplay paths. |
| NexusVision `remote_play_client.py:529-596,599-715,1678,1938-1969,2045-2098,2539` | Explicit PID force-kill with code1, image sweep fallback, failed-start cleanup, manager stop escalation, prelaunch stale cleanup. Windows Popen terminate/kill bypasses the session disconnect handshake. These are plausible **classes** for this signature, not identified incident callers. |
| `remote_play_client.py:1215-1219,1481,2366-2382,2681`; `chiaki_backend.py:312-318` | Standby discard/expiry, failed promotion/identity, alternate process cleanup. Normally startup/retirement, not a healthy active session; correlate tracked generation before blaming one. |
| `native_orion/src/RemotePlaySession.cpp:683-726,739-745,2550-2562,2802-2857`; `AsyncProcessRetirer.h:103-123` | Sidecar stop/destruction/error may close KILL_ON_JOB_CLOSE ownership or use deadline reaping, taking its stream descendant down without a final child breadcrumb. Normal current retirement is asynchronous and logged. No preceding incident shutdown marker was observed before02:28:59.520. |
| `native_orion/src/OrionAppController.cpp:7134-7162` | Manual Open Chiaki path still invokes image-name taskkills when no stream window is found. No Options binding was found. |
| `native_orion/src/RemotePlaySession.cpp:148-195` | Older blocking image-sweep helpers remain defined but had **no call sites** in the current file search. Do not attribute the incident to dead helper definitions. |
| External process/OS/runtime | Another process may terminate code1; forced job cleanup, unhandled failures and dependency behavior may skip normal logs. No local source-only inventory proves absence of these. Capture a parent ledger and permitted local crash diagnostics rather than assert a specific culprit from lack of a Windows event. |

## Existing-test results and coverage limits

Working directory for these commands: `C:\Users\aaron\Desktop\chiaki-ng-src`.

```powershell
$env:PATH='C:\msys64\mingw64\bin;C:\Users\aaron\Desktop\chiaki-ng-src\ffmpeg-n7.1-latest-win64-gpl-shared-7.1\bin;'+$env:PATH
& .\build-orion-unit-ffmpeg7\test\chiaki-unit.exe
& .\build-orion-unit-ffmpeg7\test\orion-bridge-pipe-test.exe
```

- Unit executable SHA-256 `AE1AB883F10FF3783A245C2C0F954FD88545FBE6BA1D2C60A16F52C63C79BCE2`: literal result `165 of 165 (100%) tests successful, 0 (0%) test skipped.` **Exit0.** Includes new release re-send cases. Earlier incorrectly scoped invocation with `/orion_input /feedbacksender_orion` ran zero tests and was discarded, not counted as a pass.
- Pipe executable SHA-256 `CA64E1C6391A54B510D029E4DEF1826DEC422E75A7AF0BB97ABBBC4D22FBDCD5`, run1: `3 of 8 (37%) tests successful`, **exit1**. Failures: ACK-timeout/ABANDON send helper, no-ABANDON terminal deadline, owner-silence lower bound (printed387ms), budget helper, hold-expiry helper. Run2, same hash before/after: `7 of 8 (87%) tests successful`, **exit1**; only `ack_timeout_without_abandon_is_terminal` line411 failed (`stop_calls==1` within500ms). Dead-man printed411ms; hold expiry2130ms.
- Concurrently replaced pipe executable SHA-256 `04DFBE283CBCB60896E4D9549553F20CFC967C7AF35C81286540A2643CE50E28`, verbose run (`$env:ORION_PIPE_TEST_VERBOSE='1'`): `8 of 9 (88%) tests successful`, **exit1**. `delivery_timeout_failed_ack_soft_path` line392 failed: `c.read_ack(&a, 65) && a.source_seq == 2 && a.stage == (uint8_t)OrionInputAckStage::Failed`. Server log in that run said `soft_fault cause=local_delivery_timeout ... failed_ack=1 action=reconnect_pipe session=kept ownership=latched`. Other observed cases, including enqueue-stall ABANDON/no-ABANDON, passed. This newer run is **not the same test population/build** as the first two.

- **Earlier corrected pipe build (superseded by the next build):** SHA-256 `04714C8BE06512B3D6564FE8392654D768800A536527811BF33C15008523E733`, checked before/after the first run and before each of five additional fresh-process invocations plus after the last. All **six** returned the literal result `9 of 9 (100%) tests successful`, **exit0**. First corrected run printed owner silence402ms, hold expiry2092ms. At that run, source `gui/src/orioninputbridge.cpp` hashed `89F46BAAFF89CC6B814BC396AA1CF41BF19D7CED293313F2643BC48C4BB00CA6`. This supersedes the failed-harness status above while preserving the build-specific chronology. The five-run repeat command used the same PATH and `for($i=1;$i -le 5;$i++){ Get-FileHash .\build-orion-unit-ffmpeg7\test\orion-bridge-pipe-test.exe; & .\build-orion-unit-ffmpeg7\test\orion-bridge-pipe-test.exe; "TEST_EXIT=$LASTEXITCODE" }`.

- **Latest pipe build at03:05Z:** SHA-256 `7FD3EE72FED5A56A38531E7AD3B6877A4C07AAAAAEB7D395F2C891A6A8702D05`; three fresh-process invocations each printed `9 of 9 (100%) tests successful` and `TEST_EXIT=0`. Hash checked before each invocation and after the last; no hash drift. Owner-silence readings401/401/402ms; hold expiry2071/2063/2092ms. Exact repeat command uses the PATH above and `for($i=1;$i -le 3;$i++){ "RUN=$i"; Get-FileHash .\build-orion-unit-ffmpeg7\test\orion-bridge-pipe-test.exe; & .\build-orion-unit-ffmpeg7\test\orion-bridge-pipe-test.exe; "TEST_EXIT=$LASTEXITCODE" }; Get-FileHash .\build-orion-unit-ffmpeg7\test\orion-bridge-pipe-test.exe`. Current bridge source SHA-256 `9069DF3046F3DC8BA31ABEEBC6185C626BF6574E584459EAE3B96197FBE00839`. The production session binding is now a separate Qt translation unit; this real-pipe harness does not execute the new Qt exit trail or managed WM_CLOSE behavior.

Current source review supports bounded neutral/ownership policy and observes fresh release re-send cancellation on a newer DOWN (`feedbacksender.c:856-875`). It does not replace the outstanding composition/UI/exit-cause tests. No claim of console acceptance, resolved incident, production approval, or “perfect” reliability follows from these local tests.
