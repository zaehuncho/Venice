# P8 Real-world robustness: Claude lane report (2026-09-22)

Scope: how Venice behaves under the conditions real customers create, and whether the customer ends up in a safe state they can understand. The conditions are sleep and resume, network loss, PS5 rest mode, a console address change, clock jumps and DST, two instances, other apps holding the console or the card, one licence on two PCs, forced reboots, a full disk, long sessions, a sidecar crash or hang, and unplugged hardware.
Method: read-only trace of `native_orion/src`, the Python sidecar and orchestrator, `backend/lambda_function.py`, and the fork at `C:\Users\aaron\Desktop\chiaki-ng-src`. Nothing was built, launched or edited.
Ranking: an unwanted input or stuck control first, then a silent broken state, then confusing but safe failures.

---

### [CL2-P8-001] high — A second Venice instance is allowed; its Connect kills the first instance's live stream and collides on every shared resource
- Lane: P8
- Exploit / failure path:
  1. `main.cpp:342-349` creates the `Local\OrionNativeLauncher` mutex, but `alreadyRunning` is only used to forward an `orion://` deep link. A plain second launch carries on and builds a full controller, sidecar and QML UI. This happens after a double-click on the shortcut, or after the taskbar icon plus the Start menu.
  2. On Connect, instance B frees the "fixed Orion named pipes" by killing **every** chiaki-family process on the machine by image name: `remote_play_client.py:665-690` (`terminate_chiaki_processes`), `RemotePlaySession.cpp:180-199` (`reapOrphanStreamClientsBlocking`), and `OrionAppController.cpp:6983-6988` (taskkill `/IM OrionStream.exe /F /T`). Instance A's live OrionStream is terminated mid-session.
  3. Both instances use the same fixed names and files: `\\.\pipe\orion_frames` (`RemotePlaySession.cpp:2439`), the input pipe, the capture card (B's preview reports that the device is in use), `settings.json` with its `.sig`, `learning.json`, and `orion_native.log`.
  4. Speculative: when A's direct pipe dies, A falls back to its ViGEm virtual pad (see the fork comment at `orioninputbridge.cpp:148-153`, "ViGEm fallback"). Until A notices the Remote Play error and unplugs the pad (`OrionAppController.cpp:6191`), A's engine and B's pipe could both drive B's new console session through the same physical Square press.
- Affected: `native_orion/src/main.cpp`, `RemotePlaySession.cpp`, `remote_play_client.py`, `OrionAppController.cpp`
- Impact: A's session dies with no explanation. Settings and learning writes from the two instances can interleave. There is a speculative window in which two engines answer one press. Customers do double-launch.
- Reproduction (high level): start Venice and connect, then start Venice again and press Connect in the second window. Observe A's stream drop and the "device in use" preview error in B.
- Required fix: in `main.cpp`, when `alreadyRunning` and no deep link, bring the existing window to the front (post a `WM_COPYDATA` "activate", which `DeepLinkNativeFilter` already receives) and `return 0`. Separately, scope the stream-client kill to PIDs this launcher spawned (a job object or a recorded PID list) instead of image names machine-wide.
- Verification test: launch twice. The second process exits within 1 s and the first window comes to the front. Add a unit test that calls the guard with `alreadyRunning=true` and no deep link and expects exit. Also check that a standalone chiaki-ng started by the user survives a Venice Connect.
- Confidence: confirmed (steps 1-3 are code-proven); step 4 is speculative

### [CL2-P8-002] high — The fire lease lapses silently after sleep or an internet drop; recovery waits for the next 5-minute tick and nothing on screen says why
- Lane: P8
- Exploit / failure path:
  1. Fire requires `LeaseGate::fireAllowed()`: server expiry (`lease_expires_at` = server now + 900 s, `lambda_function.py:1702,1824`) compared against the wall clock, plus 15 min of monotonic staleness (`LeaseGate.cpp:84-96`, `LeaseGate.h:31-37`). This is compiled on in production (`LeaseGate.cpp:33-37`).
  2. A heartbeat that fails in transport is ignored and nothing retries it (`OrionAppController.cpp:2375-2386`). The next attempt is the fixed 5-minute `licenseHeartbeatTimer_` (`OrionAppController.cpp:2403-2408`).
  3. Venice handles no power or network events: there is no `WM_POWERBROADCAST` or network-reachability hook anywhere in `native_orion/src`. After a sleep longer than the remaining lease, or a WAN drop of about 10 min or more (router reboot, ISP blip, Wi-Fi rejoining slowly after resume), `automationSecurityAllowed()` turns false (`OrionAppController.cpp:11033-11035`).
  4. The only trace is a throttled log line, "Meter-gate arm SUPPRESSED … security=0" (`OrionAppController.cpp:12852-12866`). The meter overlay also disappears (`:12057`). No QML binds a lease state (grep `lease` in `qml/` finds none), and `securityState_` stays "Ready" because it is derived from `securityLockActive_` only (`OrionAppController.cpp:15211-15223`).
  5. Shots stay manual until the next timer tick lands a successful heartbeat, up to 5 min after connectivity returns.
- Affected: `OrionAppController.cpp` (heartbeat wiring, status), `LeaseGate.*`, `LicenseClient.cpp`
- Impact: this is a silent broken state for every customer who leaves Venice open across sleep, or who loses internet while the LAN session keeps running. The bot looks connected and simply stops shooting. Fails closed, so there is no unwanted input.
- Reproduction (high level): connect, then block `api.zaeorion.com` in the firewall for 16 min while the stream runs. Unblock. Shots stay manual for up to 5 more min, and the UI still says Ready.
- Required fix:
  - (a) After a transport-failed heartbeat, retry with backoff (15 s, 30 s, 60 s, then the 5-min cadence).
  - (b) Call `validate()` immediately on `PBT_APMRESUMEAUTOMATIC` and on a `QNetworkInformation::reachabilityChanged` to Online.
  - (c) Add a `leaseState` Q_PROPERTY and a banner: "Reconnecting to Venice servers — shots are manual until this clears."
- Verification test: in a LicenseClient fixture, fail 3 heartbeats, then succeed. Assert the retries land at about 15, 30 and 60 s, and assert `fireAllowed()` returns within one retry of recovery. In a UI test, the banner appears while `fireAllowed()` is false with `authenticated_` true, and clears when it turns true.
- Confidence: confirmed (code-proven)

### [CL2-P8-003] medium — Resume from sleep (or a forward clock step over 6 s) latches SAFE MODE as "UI thread froze"; a backward step blinds the freeze watchdog
- Lane: P8
- Exploit / failure path:
  1. The GUI-freeze worker compares `QDateTime::currentMSecsSinceEpoch()` with `guiHeartbeatMs_`, which is also wall-clock (`OrionAppController.cpp:4908-4929`). The heartbeat is refreshed only by the 2 s `onWatchdogTick` (`:6264-6267`).
  2. On resume, the worker's next 500 ms check can run before the GUI tick. The age is the whole sleep, so the worker trips (`automation_.setArmed(false)`, neutral), and the next tick latches `enterSafeMode("UI thread froze for over 6 seconds")` (`:6277-6280`).
  3. The PS5 has meanwhile dropped the session. Recovery then follows the fork's soft-fault reconnect (known **CL-001**). Safe mode auto-exits only after 30 s of a healthy stream, at most 2 times per session (`OrionAppController.h:2787-2788`).
  4. A W32Time step of more than 6 s forward does the same with no sleep involved.
  5. A backward step makes `age` negative and extends `guiFreezeSuppressUntilMs_` (`:8071`). The freeze watchdog is blind for the size of the step.
- Affected: `OrionAppController.cpp` (freeze watchdog, safe mode), `OrionAppController.h`
- Impact: the state is safe (disarm and neutral), but the message is wrong ("UI froze" after a sleep), and a long session spends its 2 auto-recoveries on sleeps. A blind watchdog after a backward step removes a safety net.
- Reproduction (high level): connect, sleep the PC for 1 min, resume. The log shows "SAFE MODE: UI thread froze for over 6 seconds".
- Required fix: move `guiHeartbeatMs_`, `guiFreezeSuppressUntilMs_` and the other `…UntilMs_` or `…SinceMs_` watchdog stamps to a steady clock. Register for power notifications (`RegisterSuspendResumeNotification`, or `WM_POWERBROADCAST` in the existing native filter):
  - On `PBT_APMSUSPEND`: disarm, neutral, `disconnectRemotePlay(false)`, and set a "suspended" suppression.
  - On resume: reset heartbeats, trigger the licence heartbeat (CL2-P8-002), and show "Resumed from sleep — press Connect."
- Verification test: a unit test on the watchdog with an injected clock, where a jump of +3600 s during suspend does not trip and a jump of −3600 s does not blind it. Manually, sleep and resume shows the resume message, not SAFE MODE.
- Confidence: confirmed (code-proven); how the resume race is ordered is probable

### [CL2-P8-004] medium — A backward wall-clock step freezes vision for the length of the step (silent manual shots)
- Lane: P8
- Exploit / failure path:
  1. Capture frames are stamped with the wall clock (`capture_card_backend.py:1149`, `time.time_ns()`). The measurement epoch passes through unchanged (`autogreen_sidecar.py:2580-2587`).
  2. The engine treats any `captureTsMs` below the last accepted one as out of order (`AutomationEngine.cpp:3140-3143`). It returns **before** updating `lastDetectorCaptureTsMs_` (`:3662-3679` vs `:3680-3683`).
  3. After a backward step of S seconds, every frame is `out_of_order_frame` for S seconds. The only reset is `AutomationEngine::reset` (`:5250`).
  4. The epoch-to-engine bridge re-converges at α=0.05 (`:3583`). A step under about 1 s passes the 150-3000 ms sanity clamp and shifts `FusedAnchorLearn` labels (`:3593-3606`) when autonomous timing is off.
- Affected: `AutomationEngine.cpp`, `capture_card_backend.py`, `remote_play_orchestrator.py`
- Impact: after a W32Time step correction or a manual clock fix, the bot stops reading meters for the length of the step, with no message. It fails closed.
- Reproduction (high level): during a live session, set the clock back 2 min. Every shot is manual for 2 min, and `detStaleFrameDrop` climbs.
- Required fix: treat `captureDeltaMs < -1000` as a clock discontinuity. Reset `lastDetectorCaptureTsMs_` and the epoch bridge, and emit a `Clock step detected` diagnostic. Longer term, stamp captures with the monotonic clock and map them to epoch only for logs.
- Verification test: an engine unit test that feeds frames with captureTs dropping by 60 000 ms. They are accepted within 2 frames, and the diagnostic is emitted once.
- Confidence: confirmed (code-proven)

### [CL2-P8-005] medium — The console address the sidecar adopts after a DHCP move never reaches native: Meter Delay and the RTT probe keep the stale IP
- Lane: P8
- Exploit / failure path:
  1. On the 09-22 move (.81 -> .126), the Python client adopts the address that answers discovery (`remote_play_client.py:2155-2229`). It explicitly does **not** rewrite settings: "the log line tells the owner what to update" (`:2166-2167`).
  2. Native keeps the stale `remotePlayConsoleIp` for the WinDivert bridge filter (`NetworkBridge.cpp:618-628`, `:1044-1045`, fed from `OrionAppController.cpp:4368`, `:15137`) and for VeniceNet (`:4581`, `:15141`).
  3. The orchestrator points the RTT ping at the native-supplied IP (`remote_play_orchestrator.py:3155-3168`).
  4. Native discovery runs only when the IP is empty (`OrionAppController.cpp:7461-7465`).
- Affected: `remote_play_client.py`, `remote_play_orchestrator.py`, `OrionAppController.cpp`, `NetworkBridge.cpp`, `VeniceNetClient.cpp`
- Impact: the stream connects, so the customer believes all is well, but Meter Delay filters packets for an address that no longer exists. That makes it a silent no-op, and the RTT probe targets a dead host. Every Connect also pays the drift lookup again.
- Reproduction (high level): register the PS5, change its DHCP reservation, connect. The stream works, `set_filter` is logged for the old IP, and meter delay never engages.
- Required fix:
  - The sidecar emits `{"event":"console_ip_adopted","ip":…}` together with the matched host id.
  - Native accepts it only if the host id matches the single registration. It then calls `persistConfig`, which re-signs, and re-applies `setConsoleIp` to NetworkBridge and VeniceNet.
- Verification test: a fixture with the registration at .81 and discovery answering at .126. After Connect, the config IP is .126, `set_filter console_ip=.126` is sent, and the RTT target is .126.
- Confidence: confirmed for the IP flow; the Meter Delay no-op consequence is probable

### [CL2-P8-006] medium — A PC clock off by more than 5 min cannot unlock, and the customer sees the raw code `timestamp_expired`
- Lane: P8
- Exploit / failure path:
  1. The client sends `request_timestamp` from `std::time` (`LicenseClient.cpp:405-411`). The server rejects `|skew| > 300 s` with `err("timestamp_expired", 401)` and no `message` (`lambda_function.py:22`, `:621-622`, `err()` at `:259-270`).
  2. `licenseErrorUserText` falls back to `result.message`, which the parser sets to the bare code (`LicenseClient.cpp:121`, `:278-283`).
  3. A clock that is years off (a dead CMOS battery) instead yields "TLS verification failed." (`:433`).
- Affected: `backend/lambda_function.py`, `LicenseClient.cpp`
- Impact: a paying customer is locked out by a common home-PC condition, and the error text does not tell them how to fix it.
- Reproduction (high level): set the clock 10 min fast, turn off automatic time, unlock. AuthGate shows `timestamp_expired`.
- Required fix: server message: "Your PC clock is off by more than 5 minutes. Turn on Settings › Time › Set time automatically, then try again." Also return `server_time` so the client can say by how much. Map `timestamp_expired` in `licenseErrorUserText`, and map TLS date errors to the same hint.
- Verification test: a lambda unit test with a timestamp of now−600 returns the message. A client test maps the code to that text.
- Confidence: confirmed (code-proven)

### [CL2-P8-007] medium — A forced reboot, power loss or full disk between the settings save and its signature leaves production locked with an opaque reason
- Lane: P8
- Exploit / failure path:
  1. `saveConfigSilently` commits `settings.json`, then separately commits `settings.json.sig` (`OrionAppController.cpp:15022-15029`). A signature failure is only logged, and the function still returns `true`.
  2. If the process dies between the two commits (a Windows Update restart kills it after its grace period), or the disk fills, the old signature stays. The next evaluation locks with "Settings signature missing or invalid" (`SecurityManager.cpp:432-436`).
  3. Production has no auto-heal: dev re-signs (`:365-370`), and the bootstrap runs only when the signature is **missing** (`:384-394`, `OrionAppController.cpp:1972`).
  4. The lock clears only on the next settings save. The Connect path's `saveRemoteSettings()` runs *after* the lock check (`OrionAppController.cpp:7446-7453`), so pressing Connect never heals it.
- Affected: `OrionAppController.cpp`, `SecurityManager.cpp`, `AppConfig.cpp`
- Impact: the customer cannot use the bot after an ordinary crash or reboot, and support has no scripted fix. Fails closed.
- Reproduction (high level): change a setting, kill the process between the two commits (a debugger breakpoint or a fault-injection hook in a dev build with the production policy), relaunch. Remote Play is blocked by the security lock.
- Required fix: make the pair atomic. Either write the digest into a single file with the settings (a signed envelope), or write the signature first under `.sig.new` and rename both in the documented order, with the evaluator accepting the `.new` pair. Minimal fallback: a customer action "Repair settings" that restores **defaults** and signs them, which does not bless tampered content, plus clear copy for the lock.
- Verification test: fault-inject a kill between the two commits 100 times. After relaunch, the state is either the old pair or the new pair, never locked.
- Confidence: confirmed (code-proven window; real-world frequency is low)

### [CL2-P8-008] low — Renaming the PC re-binds the licence; a second PC gets the raw code `device_mismatch`
- Lane: P8
- Exploit / failure path:
  1. `deriveMachineId()` hashes the **hostname** together with the machine unique id and MachineGuid (`MachineIdentity.cpp:52-61`). Renaming the PC produces a new id.
  2. Activation then returns `err("device_mismatch", 403)` with no message (`lambda_function.py:750-753`), which the client shows verbatim. The heartbeat path has a proper message (`:1786-1788`).
  3. One licence used on two PCs gets the same raw code on the second PC. The first PC keeps working, which is correct.
- Affected: `MachineIdentity.cpp`, `backend/lambda_function.py`
- Impact: a benign PC rename costs a customer one of their 3 free HWID resets, plus a support ticket.
- Required fix: add `message="This licence is bound to a different PC. Use /reset_hwid in Discord or open a ticket."` to the activation branch. Consider dropping the hostname from the derivation, with a one-time server-side migration that accepts the old id.
- Verification test: a lambda test where the activation mismatch returns the message. A MachineIdentity test that renaming the host keeps the id (after the change).
- Confidence: confirmed

### [CL2-P8-009] low — The official PS Remote Play app holding the console produces a generic timeout, not "another Remote Play app is connected"
- Lane: P8
- Exploit / failure path: the fork classifies `CHIAKI_RP_APPLICATION_REASON_IN_USE` as `…RP_IN_USE` (`chiaki-ng-src/lib/src/session.c:1142-1143`). Nothing in the launcher or the Python client maps it (grep `in_use` finds no match in `remote_play_client.py` or `RemotePlaySession.cpp`). The Python client reports "no fresh console-session readiness marker was observed" (`remote_play_client.py:1827`). OBS holding the card is handled well: there is a named message at `capture_card_backend.py:1083`.
- Affected: fork status output, `remote_play_client.py`
- Impact: confusing but safe. The customer retries instead of closing the other app.
- Required fix: the fork writes its quit reason into its status marker or stderr, and the Python client maps `RP_IN_USE` to "PS Remote Play is already connected to this PS5. Close it, then Connect."
- Verification test: a fixture marker carrying `RP_IN_USE` yields the customer text.
- Confidence: probable

### [CL2-P8-010] low — The customer activity log uses local time with no offset, while the disk log uses UTC
- Lane: P8
- Exploit / failure path: the activity ring uses `HH:mm:ss` local time (`OrionAppController.cpp:14966-14967`). The shot log `UserFacingLog` uses `yyyy-MM-dd HH:mm:ss` local time (`AutomationEngine.cpp:255-266`). The disk log uses UTC ISO (`:14992`). On a DST fall-back, one hour repeats in the customer log, and screenshots sent to support cannot be joined to the disk log without knowing the customer's zone.
- Affected: `OrionAppController.cpp`, `AutomationEngine.cpp`
- Impact: support friction only.
- Required fix: append the UTC offset (`t` or `±HH:mm`) to the local stamps.
- Verification test: a formatting unit test.
- Confidence: confirmed

---

## Known IDs this lane re-hit (real-world triggers, not new findings)

| Known ID | Real-world trigger found here | Evidence |
|---|---|---|
| **CX-015** (unbounded log-sink wait) | **Disk full.** `writeLosslessly` retries forever and never checks `stopping_` (`OrderedFileLogSink.cpp:205-251`). `enqueue` blocks the GUI thread once `maxOutstandingBytes` is reached (`:79-83`). The freeze watchdog then disarms (safe), but **the app cannot exit**: `stopAndDrain` joins that worker (`:110-124`, called from the destructor `OrionAppController.cpp:4964`). The customer has to use Task Manager. | code-proven |
| **CL-001** (fork soft-fault reconnect) | Sleep and resume, a LAN or ICS drop, the PS5 going to rest mid-session. All of these end in the fork's reconnect path. | see CL2-P8-003 |
| **CL-006** (telemetry silence gap) | A hung sidecar: `transportAgeMs_` and `frameAgeMs_` keep their last values (`RemotePlaySession.cpp:4292-4317`), so the transport watchdog (`SidecarWatchdog.h` `streamTransportNeedsRestart`) never fires. | code-read |
| **GM-002 / CX-001** (learning.json backup) | Forced Windows Update restart or power loss during a learning save. | trigger only |

## Checked and safe

- **Controller unplugged mid-shot:** `GIDC_REMOVAL` on the active pad disarms, resets and neutralises (`OrionAppController.cpp:5265-5300`). The poll clears held Square and the pressed overlay (`:12500-12540`).
- **Venice crash or kill while it owns input:** the fork treats the pipe loss while owning as fatal and stops the session (`orioninputbridge.cpp:360-366`), so the console never holds a stale press.
- **Sidecar crash:** the status names the crash with its stderr tail (`RemotePlaySession.cpp:2600-2611`). Repeated trips escalate to safe mode (`OrionAppController.cpp:6195-6224`).
- **Capture card unplugged, or OBS holding it:** the transport watchdog restarts the sidecar after 8 s (`SidecarWatchdog.h:57-66`), paced 3 s for the Elgato handle. The OBS cause is named (`capture_card_backend.py:1083`).
- **Clock rollback vs the lease:** staleness is monotonic (`LeaseGate.cpp:14-24,92`) and expiry is signed by the server, so a rollback cannot extend fire.
- **Fire scheduling:** monotonic (`FireEpochClock.h:57-65`).
- **Long sessions:**
  - The disk log rotates at 16 MiB with one backup (`OrderedFileLogSink.h:24`, `.cpp:185-203`).
  - The activity rings are capped (`kActivityRingMaxLines`).
  - The telemetry sample buffers are capped (`RemotePlaySession.cpp:4245-4248`).
  - Detection CSVs are off for customers (`remote_play_orchestrator.py:1436`, `ORION_DETCSV` default 0).
  - Pipe sequence counters are 32-bit.
  - One note: only 2 safe-mode auto-recoveries per session (`OrionAppController.h:2788`), so a 4 h+ session with several sleeps needs a manual reset.
- **PS5 in rest mode at Connect:** discovery reaches a console in Standby (`RemotePlaySession.cpp:1434,1570`), and the Python client wakes it before spawning (`remote_play_client.py:2231+`).

---

**Verdict: needs changes**

Top 5 fixes in priority order:
1. **CL2-P8-001:** single-instance exit in `main.cpp`, and a PID-scoped stream-client kill.
2. **CL2-P8-002:** heartbeat retry with backoff, an immediate heartbeat on resume or network return, and a visible "shots are manual: reconnecting to servers" state.
3. **CL2-P8-003:** power-event handling plus steady-clock watchdog stamps, so a resume is never reported as "UI froze" and a backward step never blinds the watchdog.
4. **CL2-P8-004:** clock-step detection in the engine's frame-order check, so vision does not go dark for the size of the step.
5. **CL2-P8-005:** propagate the adopted console IP to native (NetworkBridge, VeniceNet, RTT) and persist it.
