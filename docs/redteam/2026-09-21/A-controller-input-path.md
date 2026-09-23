# Surface A — controller input path (physical pad → launcher → named pipe → fork → takion)

Internal red team, 2026-09-21 owner session. Read-only analysis; no source edited, nothing built, nothing launched.

**Both owner-reported bugs are located.**

- **Bug (1) "TRIANGLE + CIRCLE disconnects Venice" — ROOT CAUSE FOUND. Confidence: very high (~97%).**
  It is not a chord handler and not the pipe buffer. The fork's **40 ms release echo sits at the head of the
  same FIFO as the next required edge**, while the bridge gives that edge only **25 ms** before it declares a
  fatal fault and calls `chiaki_session_stop()`. A chord's *second* release always lands inside the first
  release's 40 ms window. The `expedite_history_repeats()` mitigation exists in source but **is not in the
  running binary** (proven by symbol inspection of the linked object). Proven end-to-end with
  OrionStream's own session log, which nobody had looked at.
- **Bug (2) "R2 clamps down" — ROOT CAUSE FOUND for the class; one exact trigger instance not captured
  in this log. Confidence: high (~85%) that it is a lost R2-release history datagram that no mechanism
  repairs, with a named hole in the new release-redundancy.** The launcher is exonerated by its own
  divergence audit (0 divergence events in the whole session).

New primary evidence introduced by this report: **`%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log`** —
OrionStream writes a full `Orion transport:` trace there. Five files from the incident session survive.
They contain the smoking gun. They are not relayed into `logs/orion_native.log`, which is why the
transport failure looked like a black box.

Findings ranked most severe first.

---

## A-1 · BLOCKER · The 40 ms release echo head-of-line-blocks the next required edge past the bridge's 25 ms deadline, and a single miss kills the whole Remote Play session — this is bug (1)

**Failure path**

1. Launcher writes a `MustDeliver` packet per button/trigger edge and blocks for the exact delivery ACK.
2. Fork bridge enqueues it, ACKs `Enqueued`, then waits **25 ms** for local UDP delivery
   (`gui/src/orioninputbridge.cpp:20`, `:281-282`).
3. A *pure release* enqueues **three** FIFO entries: primary, a 3 ms twin, and a **40 ms echo**
   (`lib/src/feedbacksender.c:260-302`, delays at `lib/include/chiaki/orioninput.h:23-28`).
4. The sender dequeues **only from the head** and **only when `head->not_before_ms <= now`**
   (`lib/src/feedbacksender.c:893-901`). A not-yet-due echo therefore stalls everything behind it,
   including a deadline-bound `MustDeliver`.
5. The next edge arrives < 40 ms later (a chord: the two releases are one human hand). Its 25 ms budget
   expires before the echo's 40 ms spacing does.
6. `fail_transport(..., CHIAKI_ERR_TIMEOUT, "local_delivery_timeout_or_mismatch")`
   (`orioninputbridge.cpp:286-291`) → writes a `Failed` ACK → **`chiaki_session_stop(session_)`**
   (`orioninputbridge.cpp:163-164`). The entire Remote Play session dies.
7. Launcher sees stage=Failed → `WrittenUnconfirmed` → `closePipe()`
   (`native_orion/src/OrionInputClient.cpp:350-352`, `:562-572`) → route falls back to ViGEm/XUSB →
   sidecar restarts OrionStream → "Venice disconnected".

**Affected files**

- `chiaki-ng-src/gui/src/orioninputbridge.cpp:20` (`ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25`)
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:281-291`, `:143-165`
- `chiaki-ng-src/lib/src/feedbacksender.c:893-901` (head-only, due-only dequeue)
- `chiaki-ng-src/lib/src/feedbacksender.c:1147-1156` (echo armed at `completed_ms + 40`)
- `chiaki-ng-src/lib/include/chiaki/orioninput.h:28` (`CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS 40u`)
- `NexusVision/native_orion/src/OrionInputClient.cpp:562-572`

**Impact** — Blocker. Any two button releases inside 40 ms tear down Remote Play. Chords are the reliable
case; the owner found it in seconds. 10 teardowns in a 22-minute session.

### Evidence — supporting (this is direct, not circumstantial)

**(a) Every one of the 10 incidents is the same fault, and it is a timeout.**
All non-nominal `Input hook heartbeat` lines in `logs/orion_native.log` carry
`ack_winerr=31 ack_stage=128 ack_error=15`, i.e. `ERROR_GEN_FAILURE` / `OrionInputAckStage::Failed` /
`ChiakiErrorCode 15 = CHIAKI_ERR_TIMEOUT`. `ack_failures` increments exactly once per incident (1→10):
one failed transaction each time, never a storm. `failures=0` (write failures) for the whole session.

**(b) OrionStream says it outright.**
`%APPDATA%\Chiaki\Chiaki\log\chiaki_session_2026-09-21_22-22-44-879879.log:406`

```
[22:22:52:661661] [E] Orion transport: fatal_local_delivery_fault source_seq=30905 error=Timeout
                      reason=local_delivery_timeout_or_mismatch queue_abort=Success action=stop_session
```

and `chiaki_session_2026-09-21_22-23-00-313313.log`

```
[22:23:28:012012] [E] Orion transport: fatal_local_delivery_fault source_seq=30944 error=Timeout ...
```

`30905` and `30944` match `writes=30905` / `writes=30944` in the launcher heartbeats at those instants.

**(c) The chord is captured live, with the echo visibly blocking.** Incident #10, same file:

```
27:775  pipe_received source_seq=30941 btn=10        <- 0b1010 = MOON|PYRAMID = CIRCLE + TRIANGLE held
27:937  pipe_received source_seq=30942 btn=8         <- circle released (pure release)
27:937  edge_enqueued  source_seq=30942 depth=3      <- primary + 3 ms twin + 40 ms echo
27:939  edge_dequeued  source_seq=30942              <- primary completes at :939  => echo armed :979
27:942  redundant_history_dequeued source_seq=30942  <- the 3 ms twin
27:962  pipe_received source_seq=30943 btn=0         <- triangle released
27:963  edge_enqueued  source_seq=30943 depth=4      <- 30942's echo STILL QUEUED + 3 new entries
27:980  redundant_history_dequeued source_seq=30942  <- echo finally serviced at :980 == armed :979
27:980  edge_dequeued  source_seq=30943  ... ack_local_delivery queue_wait_us=17118   <- 17 ms behind it
27:984  redundant_history_dequeued source_seq=30943  <- 30943's twin; echo armed :980+40 = 28:020
27:986  pipe_received source_seq=30944 btn=0         <- deadline :986 + 25 ms = 28:011
28:012  fatal_local_delivery_fault source_seq=30944 error=Timeout
```

30944 needed the queue to clear at 28:011; the echo ahead of it was due at 28:020. **Missed by 9 ms.**
Incident #9 (seq 30905) is byte-for-byte the same shape: `depth=4`, `ack_enqueued` at `52:636`,
no `edge_dequeued` ever, fatal at `52:661` — exactly 25 ms later.

**(d) The echo is NOT being expedited in the running build — proven, not inferred.**
The echo is serviced at *armed time + 1–2 ms* every single time, never early:
`:761→:763`, `:027→:029`, `:979→:980`. The running `OrionStream.exe`
(`sha256 0dad53db1267721b7e0db029b58f8447722e9b2f689e93405b65c0333527f149`, the exact hash the launcher
logged at `orion_native.log` 03:11:12.719) was linked at **18:04:28** from
`chiaki-ng-src/build-orion-optimized-ffmpeg7/lib/CMakeFiles/chiaki-lib.dir/src/orioninput.c.obj`
compiled at **18:03:14**. `lib/src/orioninput.c` was last edited at **18:25:23** — 22 minutes later.
Symbol scan of that object:

```
chiaki_orion_input_is_pure_button_release     <- present
chiaki_orion_input_needs_redundant_send       <- present
chiaki_orion_input_queue_init / peek / pop / push
chiaki_orion_input_queue_expedite_history_repeats   <- ABSENT
```

So the shipped binary has the 3 ms twin and the 40 ms echo but **not** the mitigation that was supposed to
make them safe. The mitigation is uncommitted working-tree source that was never rebuilt or redeployed.

**(e) The margin was gone even when it didn't fail.** Across the three populated session logs,
**58 of 524** acked transactions waited > 10 ms in the queue, peaking at **22 681 µs** — 2.3 ms under the
25 ms cliff. This was not a corner case; it was the steady state after every release.

### Evidence — refuting / ruled out

- **Not the pipe buffer, not queue capacity.** `failures=0` and `max_us=347→400` (max write 0.4 ms) for the
  whole session: no write ever blocked or failed. Observed queue `depth` never exceeded **4** of 64.
  `edge_queue_overflow` / `ownership_barrier_overflow` never logged.
- **Not the bridge's coalescer.** `coalesced/s=0` in every `OrionInputBridge: inject/s=` line during the
  incidents; the coalescer correctly stops at button edges (`orioninputbridge.cpp:202-214`).
- **Not raw write volume.** Incidents #5/#8/#9/#10 fired after only **4–8** writes in the preceding second,
  straight out of a 1 write/s idle. Incident #4 fired at 126 writes/s. Rate is not the variable;
  *two releases within 40 ms* is.
- **Not a triangle+circle hotkey.** No combo handling exists in either tree (only the D-pad-Up meter-delay
  bypass). The chord matters only because it produces two releases in one hand motion.
- **Not the launcher's ACK budget.** Launcher budgets are 25 ms enqueued + 50 ms final
  (`OrionInputClient.cpp:80,83`) and are strictly looser than the fork's 25 ms. The fork always fires first,
  and it fires by writing an explicit `Failed` ACK, which is what the launcher recorded.
- **What would refute this:** a fatal with `depth<=1` at enqueue time and no redundant entry ahead of it,
  or a `redundant_history_dequeued` landing well *before* `armed`. Neither occurs anywhere in the logs.

### Proposed fix (minimal, in order)

1. **Do not let a best-effort copy block a deadline-bound entry.** In `feedbacksender.c:893-901`, if the head
   is `redundant && redundant_history` and not yet due, **skip it and scan forward** for the first due
   non-redundant entry instead of stalling. (The echo is idempotent; strict FIFO ordering against it buys
   nothing.) This is the correct structural fix and is robust to the constants.
2. **Rebuild and redeploy.** Even the intended `expedite_history_repeats()` mitigation is absent from the
   shipped binary. Ship it with (1), never instead of (1).
3. **Make the constants non-colliding regardless:** `CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS` (40) must be
   strictly less than `ORION_LOCAL_DELIVERY_TIMEOUT_MS` (25) minus jitter, or the two must not share a queue.
   They were chosen independently in different files and collide by construction.
4. **Stop escalating a missed input edge into a session teardown** — see A-2.

### Verification test

- Fork ctest: extend `test/feedbacksender_orion.c` — enqueue a pure release, let the primary complete,
  then enqueue a second `MustDeliver` 5 ms later and assert `chiaki_feedback_sender_wait_orion_delivery`
  returns `SUCCESS` within 25 ms with the echo still queued. This test fails against the 18:03 binary's
  behaviour today.
- Live: hold TRIANGLE+CIRCLE and release, 20 times. Pass = zero `fatal_local_delivery_fault` in
  `%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log` and zero "Controller route isolation" lines.
- Regression guard: assert `max(queue_wait_us) < 10000` over a 10-minute session (today: 22 681).

---

## A-2 · BLOCKER · One missed 25 ms input deadline tears down the entire Remote Play session (fail-closed is aimed at the wrong blast radius)

**Failure path** — `fail_transport()` (`orioninputbridge.cpp:143-165`) responds to *any* single local
delivery miss by `stop_.store(true)` **and `chiaki_session_stop(session_)`**, then breaks the outer accept
loop so the pipe server never comes back in that process. Recovery is only possible by the sidecar
respawning OrionStream — a ~5–25 s outage with video loss, an input-authority revocation, a route
attestation re-key, and a ViGEm fallback window.

**Affected** — `chiaki-ng-src/gui/src/orioninputbridge.cpp:150,163-164,349-350`;
`lib/src/feedbacksender.c:453-494` (`abort_orion_transport` latches `orion_ownership_active = true`
permanently, so physical passthrough is masked for the rest of that session's life).

**Impact** — Blocker. This is what converts A-1 (a 9 ms scheduling miss) into "Venice disconnected". It also
converts every other fault on this surface (`enqueue_failed`, `edge_queue_overflow`,
`enqueue_ack_write_failed`, `owner_pipe_disconnected`) into the same maximal outcome. The comment at
`:337-339` justifies this for *ownership ambiguity*; it is not justified for a redundancy copy running late.

**Repro** — log lines 22212–22215, 22611–22613, 22884–22886, 23290–23292, 24324–24327, 24473–24475,
24514–24516, 24568–24571, 24625, plus `logs/orion_native.log` 03:11:14.827 onward
(`Remote Play input session failed readiness ... the current Chiaki session ended before becoming ready`).

**Root cause hypothesis** — The design treats "I could not prove this packet reached the local socket in
25 ms" as equivalent to "the bot may have a stale shot command in flight". Those are different risks.
*Supporting:* the fatal path is reached with `queue_abort=Success`, i.e. the queue was cleanly aborted and
nothing stale existed. *Refuting:* for a genuine duplex loss while owning, session stop really is the safe
answer — so the fix must discriminate, not remove.

**Proposed fix** — Split the outcomes. A `MustDeliver` that times out but whose queue aborted cleanly should:
(i) return `Failed` to the launcher, (ii) drop the Orion queue, (iii) **keep the session and the pipe alive**
and let the launcher's existing `inputRouteAwaitingRecovery_` re-seed path do its job. Reserve
`chiaki_session_stop()` for `owner_pipe_disconnected` and for enqueue-side faults where a shot packet
may already be queued. Add a bounded strike counter (e.g. 3 timeouts in 10 s) before escalating.

**Verification** — Inject a synthetic 30 ms sender stall; assert the session survives, the launcher logs
`Direct controller pipe recovered`, and no `StreamConnection is disconnecting` appears.

---

## A-3 · HIGH · R2 (and L2) releases reach the console only as history events, with a named hole in the new redundancy — this is bug (2)

**Failure path**

1. **Triggers are not in the feedback STATE packet at all.** `ChiakiFeedbackState`
   (`lib/include/chiaki/feedback.h:16-25`) carries sticks, gyro, accel and orientation — **no `l2`/`r2`**.
   `feedback_sender_send_state` (`lib/src/feedbacksender.c:520-536`) therefore cannot re-assert a trigger.
   Trigger state travels **only** as Feedback History events
   (`feedbacksender.c:715-736`, `CHIAKI_CONTROLLER_ANALOG_BUTTON_R2`), on change, over unacknowledged UDP.
2. Consequence: a single lost R2-release datagram leaves the PS5 holding R2 — sprint stays on — until some
   later history packet happens to re-carry the event. Exactly the stuck-button class that the 2026-09-21
   change was written to close.
3. **The new redundancy does not cover the common case.** `chiaki_orion_input_is_pure_button_release`
   (`lib/src/orioninput.c:64-82`) requires `pressed == 0`:
   ```c
   return (released != 0 || trigger_released) && pressed == 0 && !trigger_increased;
   ```
   If the R2 → 0 edge shares a packet with **any** new button press, **no twin and no echo are emitted**.
4. That sharing is routine, not exotic: the launcher samples at a **4 ms** tick
   (`native_orion/src/OrionAppController.cpp:4410`, `inputPollTimer_.setInterval(4)`) and writes at most one
   packet per tick, so two physical edges within 4 ms collapse into one packet. "Let go of sprint and press
   Square/X" is a 2K reflex that lands inside 4 ms constantly.
5. Partial trigger travel is also uncovered by design (only the terminal `→ 0` transition qualifies), so an
   R2 that decays 255 → 40 → 0 across ticks only gets copies on the final step.
6. **Nothing else repairs it.** The 1 Hz idle liveness probe `reassertLastState()`
   (`OrionInputClient.cpp:466-489`) re-sends the last confirmed state with `MustDeliver` only — the fork sees
   an equal state, so `button_edge == false`, `redundant_history == false`, and it emits a **feedback STATE**
   with no history. Confirmed in the session log: every idle probe is
   `edge_dequeued ... needs_state=1 needs_history=0`. The probe proves the pipe is alive and proves nothing
   about button or trigger state.
7. The rolling `FEEDBACK_HISTORY_RESEND_EVENT_COUNT 0x4` window (`feedbacksender.c:11,655-656`) gives partial
   repair — but only on the *next* history packet, and a chord press+release is 4 events, which pushes the R2
   event out of the window immediately.

**Affected** — `lib/src/orioninput.c:64-82`; `lib/include/chiaki/feedback.h:16-25`;
`lib/src/feedbacksender.c:520-536`, `:715-736`, `:255-258`;
`native_orion/src/OrionInputClient.cpp:466-489`; `native_orion/src/OrionAppController.cpp:4410`.

**Impact** — High. Sprint latches on at the console. Unrecoverable by the player except by pressing and
releasing R2 again (and cosmetically confusing because the launcher's own UI shows R2 released).

### Evidence — supporting

- No trigger fields in `ChiakiFeedbackState`; verified by reading the struct, not by inference.
- Idle probes are `needs_history=0` in `chiaki_session_2026-09-21_22-23-36-009009.log` — the liveness
  mechanism structurally cannot re-assert R2.
- `pressed == 0` is an explicit guard in the release-redundancy predicate.
- 4 ms sampling tick means edge collapsing is a normal event, not a race.

### Evidence — refuting (and what it rules out)

- **The launcher is exonerated.** `observeOutputDivergence` (`OrionAppController.cpp:11751-11800`, wired at
  `:13369`) logs `OUTPUT DIVERGENCE START/END` for `r2` whenever output ≠ physical. **Zero such lines exist in
  the entire 8 MB log.** The launcher never held r2 after the pad released it. So this is *not* the engine
  latch at `OrionAppController.cpp:6946-6947` (`if (cfg.defenseSprintAssist && output.r2 > 24) output.r2 = 255`)
  and not the sprint-release shaping.
- **Not a stale re-seed.** `reassertLastState()` and `releaseSquareForWatchdog()`
  (`OrionInputClient.cpp:443-464`) copy `last_`, which only advances after a seq-matched ACK, so it always
  equals what OrionStream holds; `ensureConnected()` clears `haveLast_` so a reconnect seeds a fresh full
  state. A stale `r2=255` snapshot cannot be replayed onto a pad that has moved on. **The packet's
  hypothesis "a stale snapshot with r2=255 is re-seeded" is refuted.**
- **Not the ViGEm fallback mapping.** `VirtualController.cpp:346-347` maps `l2`/`r2` 1:1
  (`triggerToByte` = clamp 0..255) and `toDs4` additionally sets the digital L2/R2 bits at > 24; no latch,
  no inversion, no dead zone that could hold 255.
- **Note on the recovery gate:** "physical shot controls were neutral" (`ShotIntentPolicy.h:75-86`) tests
  **only `!square()` and right-stick-centre**. R2/L2 are *not* in it. That is not the R2 bug's cause, but it
  means the fail-closed re-seed can and does fire while R2 is held — see A-6.
- **What would refute A-3:** a captured `r2` history event whose datagram provably reached the console while
  the console still held sprint. I could not capture an R2 incident in this log — the owner's R2 reports are
  from 18:57 and 20:52 local, and the only trace in those windows is the *launcher's* view (`r2=255` while
  sprint was genuinely held). Closing that gap needs the instrumentation in the verification step below.

### Proposed fix (minimal)

1. **Remove the `pressed == 0` veto for the trigger half of the predicate.** A trigger `→ 0` transition is
   idempotent to duplicate regardless of what buttons went down in the same packet. Split the predicate:
   ```c
   const bool button_release_safe = released != 0 && pressed == 0;
   const bool trigger_release_safe = trigger_released && !trigger_increased;
   return button_release_safe || trigger_release_safe;
   ```
   (Copying a release never invents input; that is the whole justification already stated in the comment at
   `orioninput.c:66-72`.)
2. **Give the 1 Hz idle probe teeth.** When the route is idle and a trigger is at rest, have
   `reassertLastState()` request a history re-assert (or have the fork re-emit the last terminal
   trigger/button state as history) so a stuck trigger self-heals within one second instead of never.
3. Apply A-1's fix first — copies that can be dropped by a queue stall protect nothing.

### Verification test

- Fork unit test: `is_pure_button_release({r2=255,buttons=0}, {r2=0,buttons=BOX})` must return **true**
  after the fix (today: false). Add the mirrored L2 case.
- Live instrumentation: add one `CHIAKI_LOGI` to `feedback_sender_record_history` for
  `CHIAKI_CONTROLLER_ANALOG_BUTTON_R2` events, then reproduce sprint-release-into-shot 100×; join the
  emitted `r2=0` events against the launcher's `Physical shot epoch ... r2=` lines. Any launcher-side
  `r2=0` with no corresponding history event (or a single un-echoed one) is the defect.

---

## A-4 · HIGH · The release-repair duplicate is scheduled 24 ms after a release, i.e. deliberately inside the 40 ms echo window, and closes the pipe itself

**Failure path** — On any digital release edge in the output, the launcher schedules
`reassertLastState()` at `+24 ms` (`OrionAppController.cpp:13673`, constant
`OrionAppController.h:2239 kHookReleaseRepairDelayMs_ = 24`). That probe is a `MustDeliver` with its own
25 ms bridge budget, so it needs the queue clear by `release + 49 ms`. The echo of the very release that
triggered it is due at `release + 40 ms`. The margin is **9 ms**, and it evaporates whenever the release's
primary completes a few ms late or a second release intervenes. When the probe times out, `transmitLocked`
sees `WrittenUnconfirmed` and **calls `closePipe()` itself** (`OrionInputClient.cpp:566-571`).

**Affected** — `native_orion/src/OrionAppController.cpp:13673`, `:13685-13716`;
`native_orion/src/OrionAppController.h:2239`;
`native_orion/src/OrionInputClient.cpp:562-572`.

**Impact** — High. This answers the packet's question directly: **yes, the release-repair duplicate does
write into a pipe that is about to fail, and in at least two incidents it *was* the transaction that failed
and closed the pipe.**

**Evidence — supporting**

- `logs/orion_native.log:24514` and `:24568`:
  `Input release-repair duplicate FAILED (result=5)` — `result=5` is
  `InputRouteWriteResult::WrittenUnconfirmed` (`native_orion/src/PreciseFirePolicy.h:55-67`), which is only
  produced by `waitForDeliveryAck` and is immediately followed by `closePipe()`. In both cases the
  `Controller route changed` line comes **after** the repair line (24515, 24569) — the repair closed it.
- Contrast: the other eight incidents log `result=0` (`Failed` = pipe already gone), i.e. the repair merely
  observed a pipe someone else had already closed.
- Arithmetic: 24 + 25 = 49 vs an echo due at 40. The two constants live in different repositories and were
  chosen with no knowledge of each other.

**Evidence — refuting** — The two `result=5` incidents fall in a window whose chiaki session logs have
already been pruned (only 5 files are retained), so I cannot show the matching
`fatal_local_delivery_fault` for them. The inference rests on the `result=5` semantics plus the identical
signature (`ack_stage=128 ack_error=15`) on those heartbeats, which *is* present.

**Proposed fix** — Move the repair outside the echo window: `kHookReleaseRepairDelayMs_` must exceed
`CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS` plus margin (e.g. 60 ms), **or** make the repair a
non-`MustDeliver` probe that cannot fail closed. Better: make A-1's skip-ahead fix land, which removes the
coupling entirely. Also stop the repair from closing the route on a single unconfirmed probe — it exists to
*heal* the route, and closing it converts a diagnostic into an outage.

**Verification** — Assert that a `reassertLastState()` issued at release+24 ms completes within budget while
a 40 ms echo is queued.

---

## A-5 · MEDIUM · A history-replay identity mismatch silently kills the feedback sender thread

**Failure path** — `feedback_sender_flush_history_locked` returns `CHIAKI_ERR_INVALID_DATA`
(`lib/src/feedbacksender.c:602-609`) when a `redundant_history` entry is dequeued but the single
`orion_repeat_history` slot does not match `(history_dirty_source_seq, history_dirty_source_flags)`. The
thread loop treats anything other than `SUCCESS`/`OVERFLOW` as terminal:
```c
feedback_sender->should_stop = true;
break;                      // feedbacksender.c:877-882  and  :969-972
```
The sender thread **exits**. Nothing logs "the sender died". Every subsequent
`wait_orion_delivery` then times out, producing A-1's fatal path — i.e. it presents as the same
"disconnect", with a completely different cause.

**Precondition (why it is reachable)** — The producer's `button_edge` is measured against
`orion_pending_state`; the sender's `history_change` is measured against `controller_state_history_prev`.
These are different baselines. `chiaki_feedback_sender_set_controller_state` (the SDL/physical passthrough
path, `feedbacksender.c:182-205`) advances `controller_state_history_prev` **without** advancing
`orion_pending_state`, whenever `orion_ownership_active` is false — which is the case for every packet
before the first flagged one in a session. A primary that then produces `needs_history=0` never refreshes
the repeat slot, and its twin walks into the mismatch.

**Affected** — `lib/src/feedbacksender.c:598-613`, `:877-882`, `:969-972`, `:163-179`;
single-slot state at `lib/include/chiaki/feedbacksender.h` (`orion_repeat_history*`).

**Impact** — Medium (narrow precondition, severe and unattributable consequence).

**Evidence** — Supporting: the code path is unconditional and the only diagnostic is a rate-limited
`Feedback Sender failed to format history buffer ... fail_closed=1`, which does **not** appear in the
retained session logs. Refuting: it did not fire in this session — all 10 incidents are `Timeout`, and the
formatter error string is absent, so A-5 is *not* what happened on 2026-09-21.

**Proposed fix** — On identity mismatch, **drop the redundant copy** (it is optional by definition) and
continue, logging once; never stop the sender for a failure in a best-effort path. Reserve `should_stop` for
the real invariant failure (the non-repeat formatter). Additionally, log a fatal line when the sender thread
exits so this can never again masquerade as a transport timeout.

**Verification** — Unit test: dequeue a `redundant_history` entry with a stale repeat slot; assert the
sender survives, the primary still completes, and exactly one warning is emitted.

---

## A-6 · MEDIUM · The fail-closed recovery gate ignores triggers, so the route re-seeds while R2 is held

**Failure path** — "Direct controller pipe recovered: forced write accepted after physical shot controls
were neutral" is gated on `routeRecoveryShotInputsNeutral` → `shotInputControlsNeutral`
(`native_orion/src/ShotIntentPolicy.h:75-86`), which checks **only** `!state.square()` and right-stick
radius. `l2`, `r2`, and every other button are outside the gate. The recovery seed can therefore be taken
mid-sprint, and the "controls were neutral" claim in the log line is not what the predicate tests.

**Affected** — `native_orion/src/ShotIntentPolicy.h:75-86`;
`native_orion/src/ControllerRoutingPolicy.h:209-216`;
`native_orion/src/OrionAppController.cpp:13911-13932`.

**Impact** — Medium. Mostly a truthfulness/attribution defect today: the re-seed builds a fresh full-state
`MustDeliver` from the *current* pad, so it delivers the correct `r2`, and `haveLast_=false` prevents any
stale replay. But it makes the log line misleading during exactly the investigation it exists for, and it
leaves the gate blind to a trigger that is genuinely stuck.

**Evidence** — Supporting: `logs/orion_native.log:22918, 23365, 24362, 24506, 24550, 24605` all emit the
"physical shot controls were neutral" claim; the predicate is two lines long and tests neither trigger.
Refuting the stronger claim: `ensureConnected()` (`OrionInputClient.cpp:196-218`) sets `haveLast_ = false`,
and `classifyOrionInputPacketFlags(nullptr, p)` (`OrionInputClient.h:190-194`) forces a full-state
`MustDeliver` seed, so **no stale `r2=255` snapshot can be replayed**.

**Proposed fix** — Either extend the predicate to `l2 == 0 && r2 == 0 && buttons == 0` for the *route
recovery* use (keeping the shot-intent use as-is), or rename the log line to say what it actually proved.

**Verification** — Unit test `routeRecoveryShotInputsNeutral` with `{r2=255}` and assert the chosen
semantics; live, confirm the recovery line only appears with a fully idle pad.

---

## A-7 · MEDIUM · The REDUNDANT_FLICK copy is not expedited at all, and eats a third of the budget

`expedite_history_repeats` only clears entries with `redundant && redundant_history`
(`lib/src/orioninput.c:40-49`). The tempo-flick copy is pushed with `redundant = true`,
`redundant_history = false`, `not_before_ms = UINT64_MAX` (`feedbacksender.c:303-309`) and is armed to
`completed_ms + CHIAKI_ORION_INPUT_REDUNDANT_DELAY_MS` (8 ms) at `:1154`. It therefore head-of-line-blocks
the next `MustDeliver` for up to 8 ms of a 25 ms budget — survivable alone, fatal when stacked on the
17 ms echo waits measured in A-1. It is emitted on every non-neutral right-stick reversal
(`OrionInputClient.h:217-231`), i.e. on every Tempo shot.

**Impact** — Medium. Silent 32% budget tax on the bot's own shot path; contributes to A-1.
**Fix** — Include it in the skip-ahead logic of A-1's fix (or in the expedite predicate).
**Verification** — Assert `queue_wait_us < 5000` for the `MustDeliver` that follows a `REDUNDANT_FLICK`.

---

## A-8 · LOW/MEDIUM · Other ways to wedge this path (enumerated, with status)

| # | Wedge | Mechanism / file:line | Status |
|---|---|---|---|
| 1 | Stuck button | History-only delivery + single datagram; A-3's `pressed == 0` hole | **Real** — partially fixed 2026-09-21, hole remains |
| 2 | Stuck trigger (R2/L2) | No trigger field in `ChiakiFeedbackState` (`feedback.h:16-25`); nothing re-asserts | **Real — bug (2)** |
| 3 | Lost release under queue stall | Echo/twin dropped by `abort_orion_transport` (`feedbacksender.c:470`) on any fault | **Real**, follows A-1/A-2 |
| 4 | Sticks frozen at last value | If the sender thread exits (A-5) the console keeps the last feedback state forever; no watchdog | **Latent** |
| 5 | Ownership mask never lifted | `abort_orion_transport` latches `orion_ownership_active = true` (`:480`) and `fail_transport` latches `owning_ = true` (`:147`); safe only because the session is stopped — if `chiaki_session_stop` ever fails, the physical pad is permanently masked with the stream still up | **Latent, by design** |
| 6 | Double press | `is_pure_button_release` never copies a press (`orioninput.c:73-81`); bridge coalescer stops at every button/trigger edge (`orioninputbridge.cpp:207-208`) | **Not reachable** — correctly handled |
| 7 | Edge dropped by coalescing | Same guard; `coalesced/s=0` observed throughout | **Not reachable** |
| 8 | Input queue overflow (64) | `edge_queue_overflow` → `CHIAKI_ERR_OVERFLOW` → `fail_transport("enqueue_failed")` → session stop | **Latent**; max observed depth 4. Same fatal escalation as A-2 |
| 9 | Delivery-completion ring overflow (64) | `feedbacksender.c:802-813` evicts the **oldest** completion; a bridge waiting on that seq then times out → session stop | **Latent** |
| 10 | UDP retry backoff stacking | `orion_retry_not_before_ms` sleeps 1→2→4→8→16 ms holding the whole queue (`:833-845`, `:1095-1102`); 2–3 consecutive `local_udp_rejected` alone exceeds 25 ms | **Latent**; not logged in this session |
| 11 | Pipe reconnect race | Handled: `resetConnection()` clears `haveLast_`/`last_`/throttle (`OrionInputClient.cpp:154-166`); bridge publishes `live_pipe_` before `ConnectNamedPipe` (`orioninputbridge.cpp:122-127`) | **Closed** |
| 12 | Stale ACK consumed by the wrong caller | `waitForDeliveryAck` discards non-matching `sourceSeq` (`OrionInputClient.cpp:348-349`) but keeps looping until the deadline — a backlog of stale acks can consume the whole 25/50 ms budget | **Latent, low** |
| 13 | Sequence wrap | `p.seq = ++seq_` (uint32, never reset). On wrap, `seq == 0` is ignored by `feedback_sender_record_delivery_locked` (`:800-801`) → permanent timeout | **Theoretical** (~5 years at 2.2M writes/day) |
| 14 | Physical release dropped while the bot owns a shot | Covered by `squareOutputWatchdog_` + `releaseSquareForWatchdog()` (two fresh-sequence Square-ups, `OrionInputClient.cpp:443-464`) — Square only, no equivalent for any other button or trigger | **Partial** |
| 15 | 4 ms tick starved by ACK waits | The input tick (`OrionAppController.cpp:4410`) blocks in `waitForDeliveryAck` for up to 25+50 ms per edge under `ioMutex_`; during a chord the pad is sampled far below 250 Hz | **Real**, aggravates edge collapsing (A-3 step 4) |

---

## A-9 · MEDIUM · Observability: the failing side writes its verdict to a log nobody reads, and prunes it

`logs/orion_native.log` contains **zero** `Orion transport:` lines. The transport's own verdict
(`fatal_local_delivery_fault ... reason=...`) goes only to
`%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log`, which `CreateLogFilename` (`gui/src/sessionlog.cpp:206+`)
prunes to the **five most recent** sessions. Because every incident spawns a new OrionStream, the five
survivors covered only incidents #9 and #10; the other eight were already destroyed by the time anyone
looked. The launcher's heartbeat carries `ack_stage`/`ack_error`, which is enough to *classify* the fault,
but nothing in the launcher decodes them.

Secondary: `SessionLog::Log` (`gui/src/sessionlog.cpp:84-95`) does `file->write()` + **`file->flush()`** per
line under a process-global `file_mutex`, and the Orion path emits ~14 lines per button release. That is a
synchronous disk write inside the 25 ms budget on the critical thread. It is not the cause here
(`transport_us` stayed at 18–185 µs), but it is an unbounded latency source sitting directly in the path
that A-1 proves has no margin.

**Fix** — (1) Relay OrionStream's stderr/session-log `[E]` lines into `orion_native.log` with the `Sidecar:`
treatment. (2) Decode `ack_stage=128 ack_error=N` into a human verdict in the heartbeat. (3) Raise the
session-log retention, or write the Orion transport trace to its own non-pruned file. (4) Demote the
per-packet `CHIAKI_LOGI` trace to `CHIAKI_LOG_VERBOSE`, or make the sink asynchronous.

---

## Summary of what to change, in priority order

1. `feedbacksender.c:893-901` — skip not-yet-due **redundant** entries instead of stalling the FIFO head. *(A-1, A-7)*
2. `orioninputbridge.cpp:286-291` — stop calling `chiaki_session_stop()` for a clean-abort delivery timeout; return `Failed` and let the launcher re-seed. *(A-2)*
3. **Rebuild and redeploy OrionStream.** The shipped binary (18:04) is missing the 18:25 source. *(A-1)*
4. `orioninput.c:81` — let a terminal trigger release qualify even when a button press shares the packet. *(A-3)*
5. `OrionAppController.h:2239` — move the release repair past the echo window (or drop its fail-closed). *(A-4)*
6. `feedbacksender.c:602-613` — drop a mismatched redundant copy instead of killing the sender thread. *(A-5)*
7. Relay/retain OrionStream's transport log. *(A-9)*
