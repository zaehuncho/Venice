# P2: Decoder and stream path reliability (Claude lane, 2026-09-22)

Internal red team, lane **P2**, ID prefix `CL2-P2-`. This was a read-only pass. I did not edit, build, launch or deploy anything. The fork was read as its **working tree** (`C:\Users\aaron\Desktop\chiaki-ng-src`, branch `orion`, 21 modified files and about 1,100 inserted lines on top of `c7515213`), and that tree was not touched.

## Evidence base

| Source | Span | Used for |
|---|---|---|
| `logs/orion_native.log.1` (16.8 MB) | 2026-09-21 18:56Z to 2026-09-22 22:31Z | Session lifecycle, heartbeats, recoveries |
| `%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log` (5 files, the most that are retained) | 09-22 04:27 local (one 8 s harness run), then 16:46, 16:49, 16:55 and 17:22 local | The fork's own transport, decoder and teardown lines |
| Deployed `native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe` | sha256 `7786742a…`, 7,023,016 B, built 09-22 01:27 | Confirms that today's four sessions ran the post-CL-001 image |

**Health of today's shipped image (capture-card mode).** Four sessions ran from 21:46:27Z to 22:31:39Z, about 39 minutes in total.
- **Input transactions:** 4,308 MustDeliver transactions (`ack_enqueued`). There were 0 soft timeouts, 0 `ack_stage=128` and 0 recoveries.
- **Fork-side delivery wait:** `queue_wait_us` p50 is 161 to 177 µs, p99 is 523 µs and the maximum is 683 µs, against a 40 ms budget.
- **Launcher-side ACK wait:** `ack_wait_us` p99 is 698 µs and the maximum is 1,191 µs across 2,360 heartbeats, all with `ack_failures=0`.
- **Session ends:** every session ended because the owner shut the app down.

The 10-fault cluster at 03:11 to 03:23Z ran the older image `0dad53db` and is already covered by CL-001/CL-002. **The soft-fault path added by the CL-001 patch has not run once in the field.**

---

## Findings (ranked)

### [CL2-P2-001] high: Remote-Play-only mode (decoder pipe, no capture card) ships with no field evidence
- **Lane:** P2
- **Failure path:**
  1. In a production build without a capture card, `RemotePlaySession.cpp:2431-2449` pins `ORION_REQUIRE_FRAME_PIPE=1`. That makes the decoded-frame pipe the detector's only source (`remote_play_orchestrator.py:1162-1165`).
  2. The latency prior for this route is `models/latency_factory_prior.json` → `"decoder-pipe"`: `evidence_n: 0`, `evidence_route: "never measured"`. Its mean is borrowed from the capture-card route, with a ±33 ms "ignorance" widening. The bot therefore fires with an unmeasured video delay.
  3. The only retained decoder-pipe run is the 8-second harness session `chiaki_session_2026-09-22_04-27-13-974974.log`. It was not started by the launcher: there is no input hook, and it shows `export cap 60 fps`, the aliasing cap that `RemotePlaySession.cpp:2459-2469` says delivers about 37 fps. Every decoded frame after the first 0.1 s was replaced at the render stage (**420 `[drop] reason=pending_overflow_disabled_replace` in about 7 s**, 60 per second). The export reader reconnected at 04:27:19.638 (`OrionFrameExport: waiting for Orion`), about 4.9 s after its first attach. The process was then killed, and no `Session has quit` line was written.
  4. No `orion_native.log` session in the retained window ran with the decoder pipe as the video source. Every session today was `tier=capture_card`.
- **Affected:** `remote_play_orchestrator.py` (required-pipe path), `chiaki_backend.py` `OrionFramePipeBackend`, fork `gui/src/orionframeexport.cpp` and `gui/src/qmlmainwindow.cpp` (pipe-mode render), `models/latency_factory_prior.json`.
- **Impact:** A customer without a capture card gets a timing lead fitted to a different video path, plus a decoder and export pipeline that has never run a full session on this build. The likely failure is frequent early or late shots rather than a crash. This is also the owner's 09-21 Astra gate ("remote-play-only BLOCKED on one measurement"). It is filed here because no red-team table carries it.
- **Reproduction:** Remove the card, connect a production-mode build, play 10 minutes, then grade with the banner grader and read `preview_pipeline` / `cadence_stats`.
- **Required fix:** Run the paired-teacher measurement (`tools/diagnostics/dual_capture_recorder.py`: capture card and decoder pipe on the same stream), write a measured `decoder-pipe` prior with `evidence_n > 0`, and run one launcher-driven RP-only session of 20 minutes or more. Until then, either gate RP-only behind an explicit "experimental" acknowledgement (as was done for Xbox) or do not advertise it.
- **Verification test:** `evidence_n ≥ 50` for `decoder-pipe`. A launcher-driven RP-only session log should show `export cap 120 fps`, a delivered export rate of 59 fps or more over `cadence_stats`, no reader reconnects, and a banner-graded make rate within the capture-card band.
- **Confidence:** confirmed (the evidence is absent: evidence_n=0 and 0 retained sessions).

### [CL2-P2-002] medium: When the console ends a session, the feedback sender is torn down under a live input-bridge thread (race on destroyed mutexes)
- **Lane:** P2
- **Failure path:**
  1. `lib/src/streamconnection.c:330-333`: when a stream connection ends, `feedback_sender_active = false` is set and then `chiaki_feedback_sender_fini()` runs on the session thread. "Ends" includes the PS5 dropping the session, a network loss, rest mode, or another device taking Remote Play.
  2. `fini` (`feedbacksender.c:142-157`) broadcasts `orion_delivery_cond`, joins the sender thread, then runs **`chiaki_mutex_fini(orion_pending_mutex)`, `chiaki_cond_fini(orion_delivery_cond)` and `chiaki_mutex_fini(state_mutex)`**. On Windows these are `DeleteCriticalSection` and condition-variable teardown (`thread.h:65-72`).
  3. The `OrionInputBridge` thread is **not** joined at that point. It lives until the `StreamSession` destructor (`streamsession.cpp:776-778`), and the launcher keeps writing to it: game input, plus the 1 Hz MustDeliver idle reassert. The bridge reads `feedback_sender_active` **without any lock** (`session.c:374, 384, 394, 402, 410`), then enters `chiaki_feedback_sender_set_controller_state_nowait_ex` (which takes `orion_pending_mutex`, `feedbacksender.c:230`) or `wait_orion_delivery`. The latter sleeps on `orion_delivery_cond` holding `state_mutex` for up to 40 ms (`feedbacksender.c:424-452`), and after `fini`'s broadcast it must re-acquire a critical section that `fini` is about to delete.
  4. The safety comment at `session.c:367-373` ("StreamSession's destructor joins the OrionInputBridge thread BEFORE stream_connection finis the feedback sender") holds only for local teardown. `fini` is reached from `chiaki_stream_connection_run`, not from the destructor. The comment at `feedbacksender.c:150-151` makes the same false assumption.
- **Affected:** fork `lib/src/session.c:356-414`, `lib/src/streamconnection.c:330-333`, `lib/src/feedbacksender.c:142-157, 225-232, 417-454`, `gui/src/orioninputbridge.cpp:263-311`.
- **Impact:** A console-side session end that coincides with an in-flight flagged packet causes undefined behaviour on a deleted `CRITICAL_SECTION`: OrionStream crashes or deadlocks. The bot fires about 1 flagged packet per second at idle and bursts at 40+ per second, so a mid-play drop has a real chance of landing in the window. A deadlocked bridge keeps the process alive with the pipe held (compare CL-018), which then has to be caught by the reap path. There is no data loss, but the result is a hung or crashed child during exactly the recovery that most needs a clean one.
- **Reproduction:** In a fork unit fixture, start a waiter in `chiaki_feedback_sender_wait_orion_delivery(…, 40)` on a second thread, call `chiaki_feedback_sender_fini` from the main thread, and run under Application Verifier (Locks check). Live: pull the PS5's network cable while the bot is mid-burst.
- **Required fix:** Make the active flag and the teardown a real handshake. Either:
  - add a bridge-side reader count guarded by `feedback_sender_mutex` (or an SRW lock), so that `fini` flips `active=false` under that lock and waits for in-flight bridge calls to drain before destroying anything; or
  - have `StreamSession` stop and join the bridge on `CHIAKI_EVENT_QUIT` before the stream connection is torn down.

  Then correct the two comments.
- **Verification test:** The fixture above passes 10,000 iterations under Application Verifier with the waiter and an enqueuer racing `fini`, with no deleted-lock access and every waiter returning `CHIAKI_ERR_CANCELED` or `UNINITIALIZED`.
- **Confidence:** probable. The race is proven in the code, but no field crash has been observed, and the 5 retained child logs contain no console-side session end.

### [CL2-P2-003] medium: The CL-001 soft fault is one-sided. A launcher-side ACK timeout still kills the console session.
- **Lane:** P2
- **Failure path:**
  1. `OrionInputClient.cpp:80`: `kEnqueuedAckTimeoutMs = 25` (and the final window is 65 ms).
  2. On any client-side ACK timeout, `waitForDeliveryAck` returns `WrittenUnconfirmed`, and `transmitLocked` calls `closePipe()` (`:593-600`). The comment there even says *"Closing the duplex client makes OrionStream terminate the ambiguous session."*
  3. In the fork, the bridge's next ACK write fails with `ERROR_NO_DATA` (232). That leads to `fail_transport(…, "enqueue_ack_write_failed"/"delivery_ack_write_failed")`, or on read, `owner_pipe_disconnected`. Both are **fatal**: `chiaki_session_stop()` (`orioninputbridge.cpp:155-177, 284, 332, 360-366`).
  4. The CL-001 patch made only the fork's own 40 ms timeout soft. The **25 ms** ENQUEUED budget on the launcher side (which covers bridge-thread scheduling plus `set_controller_state_nowait_ex` taking `orion_pending_mutex`) is still a console-session kill, followed by the ~9 s rebuild E measured.
- **Affected:** `native_orion/src/OrionInputClient.cpp:73-83, 266-375, 580-601`; fork `gui/src/orioninputbridge.cpp:155-177, 360-366`.
- **Impact:** This is the same customer symptom as CL-001 ("Venice disconnects"), reached from the other end of the pipe. Today it has 0 occurrences in 4,308 transactions (ENQUEUED is sub-millisecond when healthy). The exposure is scheduler or disk stalls on the bridge thread; see CL2-P2-004 and CL-021 for a mechanism that holds `state_mutex` across a file write.
- **Reproduction:** In a fixture, delay the fork bridge's ENQUEUED write by 30 ms once. The launcher closes, and the child log shows `fatal_local_delivery_fault … reason=owner_pipe_disconnected action=stop_session` or `…ack_write_failed…`.
- **Required fix:** Give the pipe close a reason. Before closing on a client-side timeout, send nothing further, then treat a pipe loss that follows a client timeout the same way as the fork's soft path: drop the unsent queue, keep the ownership latch, re-listen. The simplest version is a one-byte "abandon" control message, or a distinct `own=0xFF` packet with no ACK. This lets the fork tell "launcher gave up on seq N" apart from "launcher died". Keep `owner_pipe_disconnected` fatal only for a close with no preceding abandon.
- **Verification test:** Inject a 30 ms ENQUEUED delay 20 times. Expect 0 `chiaki_session_stop`, 20 reconnect-and-seed cycles, and every seed confirmed neutral within 1 s.
- **Confidence:** confirmed (code-proven path; not observed in today's sessions).

### [CL2-P2-004] medium: In Remote-Play-only mode OrionStream logs every decoded frame, and those writes share the lock the input path logs under
- **Lane:** P2 (touches P5)
- **Failure path:**
  1. In pipe mode (`orion_pipe_mode_`, `qmlmainwindow.cpp:4783`) the render stage never promotes the pending frame, so every new frame hits `logDroppedFrameReason("pending_overflow_disabled_replace")` (`qmlmainwindow.cpp:3699-3709`). That is an **unthrottled** `qCInfo` per frame. Evidence: 420 lines in 7 s (60 per second) in the only retained pipe-mode log.
  2. Every chiaki log line goes through `SessionLog::Log` (`gui/src/sessionlog.cpp:67-95`): a regex sanitiser, `QDateTime` formatting, and a `QFile::write` plus **`flush()`** under one process-wide `file_mutex`.
  3. The input path logs through the same sink. `edge_dequeued` is logged **while the feedback sender holds `state_mutex`** (`feedbacksender.c:997-1002`, right after `feedback_sender_apply_state_locked`), and `local_udp_accept` is logged before the completion is published (`:586-592`). The bridge's `wait_orion_delivery` needs that same `state_mutex`.
  4. So in RP-only mode, a slow flush on the 60 Hz frame-log stream (for example with the disk busy) directly stretches the input delivery wait. This is the always-on version of CL-017's AV-clock concern, and the per-line flush is CL-021, which becomes more serious here.
- **Affected:** fork `gui/src/qmlmainwindow.cpp:1974-1981, 3699-3709, 6788-6800`; `gui/src/sessionlog.cpp:67-95`; `lib/src/feedbacksender.c:586-592, 997-1002`.
- **Impact:**
  - About 216,000 lines and roughly 20 MB per hour of junk in the child log. At the 5-file retention (CL-019), a single long session pushes out older evidence.
  - CPU spent on regex and formatting.
  - A lock shared by the 60 Hz decode-side logger and the input thread, which feeds CL2-P2-003's 25 ms budget and the fork's 40 ms soft budget. Capture-card mode is unaffected because video is disabled, so today's clean numbers do not cover this.
- **Reproduction:** Run a launcher-driven RP-only session. Then `grep -ac "\[drop\]" chiaki_session_*.log` should be about 60 × seconds; correlate p99 `queue_wait_us` with it compared against capture-card mode.
- **Required fix:**
  1. In pipe mode, either skip `storePendingFrame` entirely (the export tap is in the decode callback) or rate-limit `[drop]` to one summary line per second with a count.
  2. Move the `edge_dequeued` log after `state_mutex` is released.
  3. For CL-021, have `SessionLog` buffer lines and flush from a timer rather than per line.
- **Verification test:** A 10-minute RP-only session writes fewer than 700 `[drop]` lines. p99 `queue_wait_us` is within 10 % of capture-card mode, and no input-path log call runs under `state_mutex` (checked by review or a lock-order assert).
- **Confidence:** probable. The flood is confirmed in the harness log; it is inferred for a launcher-driven session.

### [CL2-P2-005] low: Every clean shutdown is logged as a fatal transport fault, so the planned regression guard cannot work
- **Lane:** P2
- **Failure path:** All 4 of 4 sessions today end with `ack_write_failed source_seq=0 stage=128 win_error=232` and `fatal_local_delivery_fault … reason=owner_pipe_disconnected action=stop_session`. Each coincides with `Application shutdown requested` in `orion_native.log.1` (lines 27387, 28771, 47965, and 22:31:39Z). The launcher closes the pipe while the fork is still `owning_` and never sends the `own=0` ownership-release barrier, so the fork's ambiguous-loss path (`orioninputbridge.cpp:360-366`) doubles as the normal exit.
- **Affected:** `native_orion/src/OrionInputClient.cpp` (destructor and `closePipe`), the shutdown teardown in `OrionAppController`, fork `gui/src/orioninputbridge.cpp:360-366`.
- **Impact:** The guard proposed in D F-1 and in the Codex re-test plan ("`fatal_local_delivery_fault` must be 0 per session") fails on every normal session, so support cannot tell a real fault from a quit. This is observability only; the console is disconnected cleanly either way.
- **Required fix:** On an intentional stop, send one `own=0` release (MustDeliver) before closing the pipe, and in the fork log a clean `owner_released_then_closed` at INFO. Keep the fatal label only for a close while still owning.
- **Verification test:** Start, play and quit 5 times. Expect `grep -ac fatal_local_delivery_fault` = 0 in every child log and `OwnershipReleased` ACKed before each close.
- **Confidence:** confirmed.

### [CL2-P2-006] low: Long sessions have no process-health telemetry, and total receive blackouts are only DEBUG lines in a pruned log
- **Lane:** P2
- **Failure path:**
  - `orion_native.log.1` has no memory, handle or thread counters for OrionStream or the sidecar (0 hits for `rss_mb`, `handles=`, `working_set`, `private_mb`). The longest retained session is 26 minutes, so nothing shows how the stream path behaves over hours.
  - The fork logged `Clamping reported packet loss: measured=100.0%` twice in the 26-minute session (17:06:24 and 17:17:19 local). Each is a window of 100 ms or more in which **every** stream packet was lost. They appear only at `[D]` level in the child log, and nothing reaches `orion_native.log`. In capture-card mode the input path is outbound and unaffected. In RP-only mode each one is a gap of 6 or more frames that the detector will see as a stall.
- **Affected:** fork `lib/src/congestioncontrol.c:32-38`, `RemotePlaySession` heartbeat (no child-process stats).
- **Impact:** Leaks or blackout clusters in a multi-hour customer session would be invisible until they turn into a crash or a run of misses.
- **Required fix:**
  1. Add a 60-second `Process health:` line in the launcher: working set, private bytes, handle count and thread count for OrionNative, the sidecar and OrionStream (`GetProcessMemoryInfo`, `GetProcessHandleCount`).
  2. Relay the fork's loss-clamp events (count per minute) through the sidecar alongside the CL-019 relay.
- **Verification test:** A 2-hour soak shows flat handle and thread counts (slope near 0) and working-set growth under 5 %, and loss windows appear in `orion_native.log`.
- **Confidence:** confirmed (the gap is observed; any leak is unproven).

---

## Evidence added to known items (not re-reported as new)

- **CL-001 (blocked).** The latched soft fault does more than *retain* stale owned output: it **re-asserts** it.
  - `state_` is overwritten with every packet **before** it is enqueued (`orioninputbridge.cpp:252-255`), so the packet that timed out becomes the bridge's current state.
  - While `owning_` stays latched, `StreamSession::SendFeedbackState` (connected to every controller's `StateChanged`, `streamsession.cpp:1263`) re-injects `orion_bridge->currentState()` at up to 30 Hz (`streamsession.cpp:1386-1397`). Those `StateChanged` events come from the **ViGEm pad the launcher has just fallen back to** (the child log shows `Controller 0 opened: "Xbox 360 Controller"`).
  - So during a soft fault the launcher's own fallback movement pumps the stale state to the console, and the fallback route itself is masked. The bounded-budget, neutralise-on-exhaustion fix Codex required must also clear `state_` to idle (or to the last *confirmed* state), not only drop the queue.
- **CL-005 / CL-018.** No change. CL-018 is fixed for the timeout path (the bridge re-listens, `orioninputbridge.cpp:370-379`). Every fatal path still `break`s out permanently.
- **CL-016 / CL-017.** Still present and ungated in the working tree (`takion.c:1698-1721`, `orionavclock.c:31-154`), and `run_orion.local.ps1:708-711` still turns `ORION_AVCLOCK=1` on for the owner's sessions. Neither the launcher nor the sidecar references `ORION_AVCLOCK`, so a packaged build is inert only as long as the variable is never inherited. Strip it in `RemotePlaySession`'s child environment.
- **CL-019.** Confirmed with a cost: the child logs for the 09-21/22 03:11Z incident cluster **are already gone**. Only 5 `chiaki_session_*.log` files are retained, the oldest from 09-22 04:27 local. One launcher restart per session, plus harness runs, cycles the evidence out within a day.
- **Checked and clean.**
  - `OrionFramePipeBackend` shutdown order: the client is closed before `frame_backend.stop()`, so the synchronous `ReadFile` breaks on pipe loss and `CloseHandle` does not wait behind it (`remote_play_orchestrator.py:3571-3590` before `:3714`).
  - The frame-export writer drops the stale pending frame on disconnect (`orionframeexport.cpp:355-365`), and the export path is fed from the decode callback, independent of the render stall above.
  - The input-link ladder is bounded: sidecar 3 attempts in 17 s (`remote_play_orchestrator.py:3263-3310`), then native 3 input-only attempts plus one contained full restart (`SidecarWatchdog.h:86-131`), then `HoldFailClosed` with a visible ControllerFault.
  - Standby pre-boot spawns only when no client manager exists and never beside another chiaki process (`remote_play_orchestrator.py:3203-3218`).

## Verdict

**needs changes.** On the capture-card route that the owner actually ran, the stream path is healthy: 39 minutes, 4,308 transactions, 0 faults, and delivery p99 under 1 ms against a 40 ms budget. The open CL-001 and CL-005 blockers still gate the release. On top of them, Remote-Play-only mode has no field evidence (CL2-P2-001) and should not be sold as supported until it is measured.

### Top 5 fixes, in priority order
1. **CL2-P2-001:** Measure the decoder-pipe route (paired-teacher run and a 20-minute launcher-driven RP-only session), or gate RP-only as experimental.
2. **CL-001 addendum:** On a soft fault, clear the bridge's `state_` so the 30 Hz keep-alive cannot re-assert a timed-out command, together with Codex's required bound and neutralisation.
3. **CL2-P2-002:** Make `feedback_sender_active` and `fini` a locked handshake, or join the bridge on session quit, before any console-side drop can hit a deleted critical section.
4. **CL2-P2-003:** Give the launcher's pipe close a reason so a client-side ACK timeout takes the same soft path as the fork's.
5. **CL2-P2-004 / CL-021:** Throttle the pipe-mode `[drop]` log, move `edge_dequeued` out from under `state_mutex`, and batch `SessionLog` flushes.
