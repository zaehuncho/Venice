# P4: Button presses and controller input (Claude, internal wave 2026-09-22)

Read-only review. No source was edited, nothing was built, deployed or launched, and the fork's dirty tree was left untouched. The only file written is this report.

**Scope:** physical pad capture, then the native engine, then the named pipe, then the fork bridge, then takion to the PS5, plus the hand-back of control to the human.

**Files read:**
- `native_orion/src/{OrionInputClient.*, OrionAppController.*, ShotIntentPolicy.h, SidecarWatchdog.h, ControllerRoutingPolicy.h, PreciseFirePolicy.h, SquareOutputWatchdog.h}`
- fork `gui/src/orioninputbridge.cpp`, `gui/src/streamsession.cpp`
- fork `lib/src/{feedbacksender.c, orioninput.c, session.c}` and `lib/include/chiaki/orioninput.h`

## What ran tonight

**Identity.** The fork image is the patched `7786742a…`. The launcher log shows `Remote Play client image … sha256=7786742a…` at 21:46:27Z, 21:49:18Z, 21:55:27Z and 22:22:00Z. Fork sources have mtime 01:24 on 09-22, and the launcher input sources have mtime 01:30–01:31. Neither changed after Codex's final gate, so every Codex per-ID verdict still applies.

The launcher that ran tonight carried the CL-004 patch. The equal-state repair transaction lands **60–65 ms** after the release history in 412 of 457 measured cases (it was 24 ms before the patch). `OrionNative.exe` was rebuilt at 17:51 local, which is after the session.

**Evidence of what works.** Owner session 21:46Z–22:31Z; `orion_native.log.1` from line 25669; four `chiaki_session_2026-09-22_16/17-*.log` files.

| Signal | Tonight |
|---|---|
| `ack_stage=128`, `local_delivery_timeout`, `soft_drop`, `local_udp_rejected`, `edge_queue_overflow`, completion overflow, formatter failure | **0** |
| Pure releases with a twin copy | 1,009 releases, 2,011 history copies (the +40 ms echo was shed only a handful of times) |
| Max `queue_wait_us` / max queue depth | **683 µs** / **3** (the 09-21 incident peaked at 22,681 µs and depth 4) |
| Heartbeat `ack_failures` / write `failures` | 0 / 0 |
| `fatal_local_delivery_fault` | **4, all `reason=owner_pipe_disconnected` at the owner's own Stop** (see CL2-P4-008) |

**What tonight did NOT exercise.** The following findings live in these paths:
- Two releases within 40 ms: **1** pair in about 900 releases, so the chord path barely ran.
- The CL-001 soft path: 0 runs.
- The own=0 ownership barrier: 0 runs.
- Physical-pad transport recovery: 0 runs.
- Route recovery: 0 runs.
- Any unflagged trigger-travel FIFO entry (`flags=0x00`): 0.
- Any FIFO entry with `source_seq=0` (see CL2-P4-005): 0.

Tonight proves the healthy path. It does not prove the recovery paths.

---

### [CL2-P4-001] high — No dead-man switch on the owned route: a stalled launcher GUI thread leaves the console holding the last input indefinitely, and the human is locked out

- **Lane:** P4
- **Failure path:**
  1. Human input is sampled and written only by `pollPhysicalController`. It runs on a 4 ms `QTimer` on the **GUI thread** (`OrionAppController.cpp:4412-4418`), and `OrionAppController` has no `moveToThread` call.
  2. On the fork side, the feedback sender re-sends `controller_state` every 50 ms forever (`feedbacksender.c:9`, `:892-894`, `:1020`). While `owning_` is set, `StreamSession::SendFeedbackState` re-injects the bridge's last `currentState()` (`streamsession.cpp:1386-1397`).
  3. The bridge blocks in `ReadFile` with no timeout (`orioninputbridge.cpp:185`). Nothing on either side notices that packets have stopped arriving while the pipe stays open.
  4. So a GUI stall or deadlock leaves the console holding the last owned state: left stick deflected, R2 (sprint) held, a face button held. It holds for the whole stall. The human cannot override it, because the physical pad is read only by the stalled launcher and chiaki ignores SDL/ViGEm while `owning_`.
  5. A known unbounded wait already sits on this thread: CX-015, the unbounded log-sink wait. The 1 Hz idle reassert and the 60 ms release repair also run on this thread, so they stall with it.
- **Affected:** `native_orion/src/OrionAppController.cpp:4412-4418`; fork `gui/src/orioninputbridge.cpp:182-190`, `gui/src/streamsession.cpp:1386-1397`, `lib/src/feedbacksender.c:1018-1021`.
- **Impact:** A runaway player: they keep sprinting in the last direction, or keep holding a button, during any launcher stall. If the stall is a deadlock, this lasts until the customer kills Venice. It is an unwanted live-game input, and it is invisible in logs: there is no input-tick gap telemetry.
- **Reproduction (high level):** In a dev build, block the GUI thread for 3 s while holding the left stick and R2. The console keeps running and sprinting for 3 s after the physical release, and no line is logged.
- **Required fix:**
  - Launcher: add a steady-clock liveness heartbeat on the pipe. It can be an unflagged equal-state packet sent from a non-GUI timer, or better, move the input tick off the GUI thread.
  - Fork: track the time since the last pipe packet. If owning and no packet or heartbeat has arrived for 250–500 ms, enqueue an owned **neutral** transaction, then keep ownership latched (never unmask onto stale work). Log `owner_silent_neutralized`.
  - Add input-tick gap telemetry (max gap per heartbeat).
- **Verification test:** A fork unit test with a fake clock: owning, last state `{r2=255, lx=max}`, no packets for 500 ms. The next sent state is neutral, with exactly one log line. A launcher test that stalls the tick for 2 s and asserts the gap metric.
- **Confidence:** confirmed (code-proven absence of any timeout). How often a GUI stall happens is not measured.

### [CL2-P4-002] medium — The CL-003 patch widened the *shared* neutral predicate: after a pad blip, holding R2 keeps the whole pad forced neutral, and the on-screen instruction does not mention triggers

- **Lane:** P4. This is a regression introduced by the 09-22 patch.
- **Failure path:**
  1. `shotInputControlsNeutral` now requires `l2 == 0 && r2 == 0` (`ShotIntentPolicy.h:84-90`, uncommitted diff). A-6 proposed widening only the *route-recovery* use and keeping the shot-intent uses unchanged. The patch changed the one shared function, so three callers inherit it:
     - **Physical transport recovery** (`ShotIntentPolicy.h:186-200`), entered by `beginTransportRecovery()` when the selected pad goes silent or is removed (`OrionAppController.cpp:12539`). While it is active, `failClosedNeutralThisTick` forces the **entire** output neutral (`OrionAppController.cpp:13004-13006`, `:13178-13181`): movement, sprint and pass. The pad does not come back until Square is up, the right stick is centred **and both triggers are at 0** for three samples. The lifecycle text still says only "release Square and center the right stick" (`:12954-12957`). A player holding sprint when the pad blips (the known silent-USB-pad class) sees a dead controller and gets the wrong instruction.
     - **Input-Timed readiness** (`AutomationEngine.cpp:7310-7330`). Holding R2 now clears `inputTimedReady_`, so a shot started off a sprint is never automated. This is latent: No-Meter is shelved (`AppConfig.cpp:24-37`, `g_inputTimedAllowed = false`). It becomes live the day that fence is lifted.
     - **Route-recovery re-arm** (`OrionAppController.cpp:13072-13083`). Automation stays disarmed while R2 is held. This is the intended half and is acceptable.
- **Affected:** `native_orion/src/ShotIntentPolicy.h:78-90,186-200`; `OrionAppController.cpp:12954-12957,13004-13006`; `AutomationEngine.cpp:7310-7330`.
- **Impact:** After a pad disconnect or reconnect, a sprinting player's whole controller stays dead, with a misleading message. It lasts until they happen to release R2. The Input-Timed regression is waiting to ship with No-Meter.
- **Reproduction (high level):** Hold R2, unplug and replug the pad (or let HID go silent), keep R2 held and move the left stick. The output stays neutral. Release R2 and the output returns about 12 ms later.
- **Required fix:** Restore `shotInputControlsNeutral` to Square plus the right stick. Add a separate `routeRecoveryInputsNeutral` that also requires the triggers, and use it only at `ControllerRoutingPolicy.h:209-216`. If triggers must also gate transport recovery, fix the lifecycle text to say "release Square and the triggers, and center the right stick".
- **Verification test:** Transport recovery with `{r2=255}` held for 3 samples clears the fence (or, if kept, the UI text names triggers). `processInputTimedIdle` with `{r2=255, then +square}` arms when the fence is lifted in a test. Route recovery with `{r2=255}` does not clear.
- **Confidence:** confirmed (code). Not exercised tonight: 0 transport recoveries.

### [CL2-P4-003] medium — A soft fault throws away the release's redundant copies, and the re-seed cannot regenerate them (stale `orion_pending_state` baseline)

- **Lane:** P4. This adds a new failure case to CL-001 and CL-003.
- **Failure path:**
  1. `chiaki_feedback_sender_drop_orion_transport` (`feedbacksender.c:501-535`) clears the queue, including the 3 ms twin and the 40 ms echo of an already-sent release, and zeroes `orion_repeat_history_size`.
  2. It does **not** reset `orion_pending_state`. That field was advanced at enqueue time (`:322`), so it describes the dropped packet, not what reached the socket.
  3. The launcher's forced full-state seed after reconnect (`OrionInputClient.cpp:190-194`, `:216`) usually equals the timed-out packet, because the human has not moved. So `button_edge` (`:235-237`, computed against `orion_pending_state`) is **false**, and `redundant_history` (`:255`) is false.
  4. On dequeue, `history_change` is measured against `controller_state_history_prev`, the last *applied* state (`:950-958`). So the release is sent as **one** history datagram with no twin and no echo.
- **Effect:** Every release that crosses a soft fault reaches the console as a single UDP datagram. That is the exact stuck-button class the copies exist to close, arriving at the moment the link is least healthy. The same applies to a bot Square-up (SHOT_RELEASE) whose copies were queued behind the timed-out packet.
- **Affected:** fork `lib/src/feedbacksender.c:235-258,322,501-535,950-958`.
- **Impact:** Stuck button or stuck R2 after a recovered timeout. Probability per event is the datagram loss rate, which is small but nonzero.
- **Reproduction (high level):** Fork unit test: enqueue an R2 release, let the primary complete, and time out the next MustDeliver so a soft drop happens. Push a seed identical to the release. Today it produces one history send and zero `redundant_history_dequeued`.
- **Required fix:** In `drop_orion_transport`, set `orion_pending_state = controller_state_history_prev` (the last applied state), so the seed is classified against what the console was actually given. Or have the bridge mark the post-soft-fault seed so the fork copies any release it contains.
- **Verification test:** The test above ends with primary + twin + echo for the release carried by the seed, and no copy of any press.
- **Confidence:** confirmed (code-proven). Not exercised tonight: 0 soft drops.

### [CL2-P4-004] medium — After a soft fault, fork and launcher disagree on who owns input: the launcher can declare "ViGEm carries PS5 input" while the fork discards ViGEm and replays the stale pipe state

- **Lane:** P4. This adds evidence to CL-001 / CX-004 / GM-003 and a new mismatch.
- **Failure path:**
  1. On `TIMEOUT+NONE` the bridge latches `owning_ = true`, writes the Failed ACK, logs twice through the synchronous per-line-flushed session log (CL-021), then disconnects, closes and recreates the pipe (`orioninputbridge.cpp:298-311,360-377`, then `:105-132`).
  2. The launcher closes on the Failed ACK and retries about 4 ms later. `ensureConnected()` stamps `lastConnectAttemptMs_` **before** trying (`OrionInputClient.cpp:201-208`). If the fork has not recreated the instance yet (a log flush or pipe-security setup slower than one tick), the attempt fails and the next one is throttled for **500 ms**.
  3. In that window `hookWrite = Failed`, so `hookOwnsInput = false`. The launcher logs "Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input" (`OrionAppController.cpp:13952-13957`) and feeds the human's input to ViGEm.
  4. The fork still has `owning_ = true`. So `SendFeedbackState` ignores ViGEm and re-asserts the bridge's stale `currentState()` (`streamsession.cpp:1386-1397`). `DpadSendFeedbackState` returns early (`:1315`). The human is dead and the console holds the pre-fault state, while the launcher log says the opposite.
  5. If seeds keep timing out, escalation is slower than it looks. The first input-only recovery needs **5 consecutive** disconnected heartbeats (`SidecarWatchdog.h:86-131`). Before the first recovery sets `inputRouteAwaitingRecovery_`, any heartbeat that samples the pipe during a seed attempt resets the counters (`OrionAppController.cpp:14064-14069`). With the pipe up about 10% of each 500 ms cycle, the expected first-recovery delay is about 7–12 s, followed by the console's about 9 s "already in use" re-handshake.
- **Affected:** `native_orion/src/OrionInputClient.cpp:196-218`; `OrionAppController.cpp:13596-13600,13952-13957,14000-14069`; fork `gui/src/orioninputbridge.cpp:298-311,360-377`, `gui/src/streamsession.cpp:1315,1386-1397`.
- **Impact:** A human-input blackout of 0.5 s (a single race) to about 20 s (a repeated-timeout loop), with a stale held state on the console and a false route log. It bounds CL-001's "unbounded" in practice, but slowly and not deterministically.
- **Reproduction (high level):** Inject one 45 ms sender stall, then add a 5 ms sleep before `CreateNamedPipeA` in the bridge's re-listen. Watch for the ViGEm-isolation line while the console ignores stick input for about 500 ms.
- **Required fix:**
  - Launcher: after a Failed/WrittenUnconfirmed on an owned route, do not throttle the next `ensureConnected()`. Clear `lastConnectAttemptMs_` on close-after-ack-failure, and retry every tick for about 100 ms.
  - Launcher: never log or treat ViGEm as carrying input while the last pipe generation ended ambiguously. Say "input held by stream client, re-seeding".
  - Fork: recreate the pipe instance *before* logging.
  - Count soft faults in the heartbeat, and make any soft fault latch the recovery counters in the same way `inputRouteAwaitingRecovery_` does.
- **Verification test:** A fault fixture with a delayed re-listen reconnects within 50 ms. A repeated-timeout fixture reaches the input-only recovery within 5 s, deterministically.
- **Confidence:** probable. The code path is proven; the race odds are unmeasured, and it was not exercised tonight.

### [CL2-P4-005] medium — A second producer: chiaki's Qt keep-alive injects the bridge's state outside the bridge's ordering, and a stale read can re-press a just-released button

- **Lane:** P4
- **Failure path:**
  1. While `owning_`, `SendFeedbackState` runs on the Qt thread for SDL controller events, throttled to 33 ms. It calls `currentState()` and then `chiaki_session_set_controller_state_nowait` with `source_seq=0`, `flags=0` (`streamsession.cpp:1386-1397`). The bridge thread does `state_ = s` and then its own `nowait_ex` (`orioninputbridge.cpp:252-265`). The two are not serialized with each other.
  2. The stale interleaving:
     1. The Qt thread reads A (Square held).
     2. The Qt thread is preempted.
     3. The bridge publishes and enqueues B (Square up) plus its twin and echo.
     4. The Qt thread resumes with A.
     5. `nowait_ex` sees `button_edge` A vs B and enqueues **A**, a Square **press**, after the release (`feedbacksender.c:235-243`).
  3. The console sees release, then re-press. It heals only on the next producer edge: the next keep-alive, the next launcher edge, or the 60 ms release repair, which re-sends B.
  4. Benign variant: if the keep-alive enqueues B first, the bridge's flagged B has `button_edge == false`. A SHOT_RELEASE carrying a press then loses its copies.
- **Affected:** fork `gui/src/streamsession.cpp:1386-1397`, `gui/src/orioninputbridge.cpp:252-265`, `lib/src/feedbacksender.c:222-340`.
- **Impact:** A phantom re-press of a just-released button for up to about 60 ms (a second Square or X). It is rare, since it needs a preemption inside a microsecond window at up to 30 Hz. It is dormant on the owner rig: tonight there were 0 FIFO entries with `source_seq=0`, because HidHide cloaks the pad and ViGEm is held neutral, so SDL raises no events. It goes live whenever chiaki's SDL sees a live pad: a stale or missing HidHide cloak, or a second controller.
- **Reproduction (high level):** Unit-level: interleave `currentState()` / bridge publish+enqueue / keep-alive `nowait` in that order, and observe a FIFO press after the release. Live: un-cloak the DualSense and grep the session log for `edge_enqueued source_seq=0`.
- **Required fix:** While owning, the keep-alive must not enqueue. Either drop it (the sender's own 50 ms re-send already covers keep-alive), or have it call a slot-only API that never creates FIFO entries and never moves `orion_pending_state` across a button or trigger edge.
- **Verification test:** Fork test: a keep-alive injection with a stale button state after a newer enqueue produces no FIFO entry and no history event. Log assertion: zero `edge_enqueued source_seq=0` in an un-cloaked session.
- **Confidence:** probable (code-proven race; frequency unmeasured).

### [CL2-P4-006] medium — SHOT_RELEASE bypasses the fork's own "never copy a press" rule

- **Lane:** P4
- **Failure path:**
  1. `redundant_history = button_edge && (SHOT_RELEASE || is_pure_button_release(...))` (`feedbacksender.c:255-258`). `is_pure_button_release` refuses anything with `pressed != 0` or a trigger increase (`orioninput.c:73-81`), because "a duplicate PRESS would be a phantom input". SHOT_RELEASE skips that check.
  2. The launcher sets SHOT_RELEASE on any packet whose Square falls, or whose right stick leaves the shot band (`OrionInputClient.h:209-236`). The tick path does not split a new press out of it: the CL-003 split covers only trigger releases (`OrionInputClient.cpp:420-439`).
  3. So a Square-up that shares a 4 ms tick with a new X, Circle, R2 re-press, or L2 rise has its **press** replayed twice (3 ms and 40 ms later).
- **Affected:** fork `lib/src/feedbacksender.c:255-258`; `native_orion/src/OrionInputClient.h:209-236`.
- **Impact:** A double pass, a double call-for-ball, or a sprint re-press, if the console treats a replayed history packet as new events. The fork's authors assume it does. Uncommon, because a human press must coincide with the bot or human Square-up tick.
- **Reproduction (high level):** Fork unit test: previous `{square}`, current `{cross}`, flags SHOT_RELEASE. Today it yields two redundant history entries.
- **Required fix:** `redundant_history = button_edge && is_pure_button_release(...)`. A SHOT_RELEASE carrying a press must instead be split launcher-side, the same way as CL-003: send the pure Square-up first, then the press.
- **Verification test:** The above yields zero copies, or a launcher split that emits `{square-up}` then `{cross}` with copies only on the first.
- **Confidence:** probable for the copy; speculative for the console consequence.

### [CL2-P4-007] low — Unflagged analog trigger travel has no backpressure; overflow is session-fatal, not soft

- **Lane:** P4. This is a residual of the CL-010 fix.
- **Failure path:**
  1. After CL-010, intermediate trigger values are no longer MustDeliver (`OrionInputClient.h:201-210`), so the launcher writes them without waiting.
  2. The fork still treats every trigger change as `button_edge` and puts it in the 64-entry FIFO (`feedbacksender.c:235-237`, `:264-271`). The bridge coalescer also stops at every trigger difference (`orioninputbridge.cpp:219`).
  3. XInput pads pass the raw 0–255 value with no deadzone (`OrionAppController.cpp:687-688`). A partially held trigger that jitters at 250 Hz, during a UDP-rejection episode where the sender backs off up to 16 ms per entry (`feedbacksender.c:1123-1134`), fills 64 entries in about 0.25 s.
  4. The result is `edge_queue_overflow`, then `enqueue_failed`, then `fail_transport`, then `chiaki_session_stop` (`orioninputbridge.cpp:266-270`). That is the fatal path CL-001 moved timeouts away from.
- **Mitigating:** DualSense forces 255 once the digital L2/R2 bit is set (`OrionAppController.cpp:544-545`). Tonight there were zero `flags=0x00` FIFO entries, and Xbox is gated.
- **Required fix:** Put analog trigger travel in the latest-wins slot unless the zero crossing changes. Or make the launcher quantize triggers, for example with a hysteresis of 4 counts.
- **Verification test:** A fork test pushing 200 jittering unflagged trigger values with the transport rejecting: no overflow, and a final value equal to the last one.
- **Confidence:** speculative.

### [CL2-P4-008] low — Every normal Stop is logged as `fatal_local_delivery_fault`, and the own=0 hand-back barrier is never used

- **Lane:** P4 (forensics)
- **Evidence:** All four sessions tonight end with the same sequence at the owner's own Stop:
  - `ack_write_failed source_seq=0 stage=128 win_error=232`, then
  - `fatal_local_delivery_fault … reason=owner_pipe_disconnected … action=stop_session`.

  These appear in `chiaki_session_2026-09-22_16-46-27…:2730-2731`, `16-49-21…:1870-1871`, `16-55-27…:22408-22409` and `17-22-00…:13733-13734`. The launcher sends an ACKed owned neutral and then just closes the pipe (`OrionAppController.cpp:6074-6107`). own=0 is sent only on the Error path (`:6123-6139`), and `ownership_barrier_*` never appears.
- **Impact:** A grep for the transport's fatal verdict now counts one false positive per clean session. The fork's ordered passthrough unmask (`feedbacksender.c:1180-1195`) is exercised by nothing live.
- **Required fix:** On a planned stop, send own=0 (ACKed barrier), then close. Log a distinct `owner_detached_clean` when the pipe closes after a completed barrier, or after an ACKed neutral with an empty queue.
- **Verification test:** A clean Stop shows `ownership_barrier_complete` and no `fatal_local_delivery_fault`.
- **Confidence:** confirmed.

### [CL2-P4-009] low — The stale-Square output watchdog is OFF in production, so earlier reports overstate Square protection

- **Lane:** P4
- **Evidence:** `squareOutputWatchdogEnabled_ = ORION_SQUARE_OUTPUT_WATCHDOG == 1`, default 0 (`OrionAppController.cpp:2045-2049`). Tonight it logged `SQUARE OUTPUT WATCHDOG: enabled=0` twice. So `releaseStaleSquareOutputLocked` returns immediately (`:6056`), including inside CL-007's `neutralizeOwnedInput`.
- **Correction to earlier reports:** A-8 row 14 ("covered by `squareOutputWatchdog_`") and the "Square watchdog copies" cited in the CL-007 approval describe code that is inactive in production. Protection against a stuck bot Square is the engine's `maxHoldMs` ceilings plus the neutral cleanup transaction.
- **Impact:** No defect was found in what is actually live; the gap is in the documentation and in approval evidence.
- **Required fix:** State this in the CL-007 record. If the watchdog is meant to ship, enable it with a test that a human-held Square longer than 1.5 s is not force-released.
- **Confidence:** confirmed.

---

## Carried IDs: new evidence only (not re-reported)

- **CL-001 / CX-004 / GM-003:** CL2-P4-003 and CL2-P4-004 add two concrete consequences of the soft path:
  - a single-datagram release after a soft fault;
  - a 500 ms reconnect-throttle blackout, with the launcher falsely declaring ViGEm active.

  They also add a practical bound: 5 consecutive down heartbeats. The soft path has **never run live**: 0 `soft_drop` in all retained session logs.
- **CL-003 / GM-004:** The cross-trigger `pre` defect stands. Also:
  - the split exists only for trigger releases, so SHOT_RELEASE plus a press is unsplit (CL2-P4-006);
  - the widened neutral predicate has side effects (CL2-P4-002).
- **CL-005:** Nothing new. CL2-P4-004's slow escalation is what a silently dead sender would feed into.
- **CL-002 (echo shedding):** Tonight's data supports it. The echo was admitted on about 99% of releases, the max wait was 683 µs, and there was one release pair within 40 ms, so the chord case is still essentially untested live.
- **Analog-edge carryover (CL-010):** Closed on the launcher side. The residual fork FIFO pressure is CL2-P4-007.

## Verdict

**needs changes.** Tonight's healthy path is clean and fast, and none of these findings reproduces in it. The recovery and hand-back paths still carry:
- one high: no dead-man on the owned route (CL2-P4-001);
- one patch regression: the widened neutral predicate (CL2-P4-002);
- three medium stuck or phantom input cases (CL2-P4-003, CL2-P4-004, CL2-P4-005).

The carried blockers CL-001, CL-003 and CL-005 keep the wave-level verdict **blocked** until they close.

**Top 5 fixes, in priority order:**
1. Fork owner-silence dead-man plus launcher liveness and input-tick gap telemetry (CL2-P4-001).
2. Split the neutral predicate back: triggers only for route recovery, and fix the lifecycle text (CL2-P4-002).
3. Resync `orion_pending_state` on soft drop so the seed re-copies releases (CL2-P4-003). Remove the 500 ms reconnect throttle after an owned-route ACK failure and fix the ViGEm claim (CL2-P4-004).
4. Stop the Qt keep-alive from enqueueing while owning (CL2-P4-005).
5. Drop the SHOT_RELEASE copy bypass and split press-plus-Square-up launcher-side (CL2-P4-006). Then send own=0 on a planned Stop (CL2-P4-008).
