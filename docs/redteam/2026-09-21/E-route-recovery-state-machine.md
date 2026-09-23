# Surface E — controller-route + recovery state machine (launcher ⟷ fork ⟷ sidecar)

Internal red team, 2026-09-21 owner session. Source read-only; no builds, no launches, no commits.
Log = `logs/orion_native.log` (8 MB, grep -a). Fork = `C:\Users\aaron\Desktop\chiaki-ng-src` (branch `orion`, **working tree**, uncommitted Astra/STUCK-BUTTON edits).

---

## TL;DR

**The launcher never initiated a single close.** Every one of the 10 incidents begins in the **fork's
`OrionInputBridge`**, which treats a *25 ms local-delivery latency overrun* as a **fatal transport fault**
and calls `chiaki_session_stop()` — killing the whole Remote Play console session, wiping every queued
button/trigger RELEASE, and costing ~9 s of rebuild. The launcher's `Failed` ACK handling, pipe close,
ViGEm fallback, route-attestation revoke and fail-closed gate are all *correct reactions* to that.

Proof in one line: **`failures=0` in every single heartbeat of the session** (no write ever failed), while
the closing heartbeats carry `ack_stage=128` (`OrionInputAckStage::Failed`), `ack_error=15`
(`CHIAKI_ERR_TIMEOUT`) and `ack_winerr=31` (`ERROR_GEN_FAILURE`, synthesised by the client at
`OrionInputClient.cpp:352`). The server told the client to fail closed. The client obeyed.

**Regression bisect (decisive):** OrionStream `sha256=d80be5f8…` (7,019,702 B) ran 2026-09-20 18:44 →
2026-09-21 20:51 — **13,237 heartbeats, 0 ack failures, 0 `ack_stage=128`, 0 "direct pipe closed"**
(`logs/orion_native.log.1` + `logs/orion_native.log:1794`), including bursts of **136 writes/s**.
A new image `sha256=0dad53db…` (7,020,214 B, +512 B) first ran at 22:06 local
(`logs/orion_native.log:21103`) — and produced **10 fatal Failed-ACKs in 21 minutes**. `0dad53db` is the
build carrying today's uncommitted *"[2026-09-21 STUCK BUTTON] a THIRD copy of every pure release"*
change, which triples the fork-side queue work per release under an **unchanged 25 ms budget**.

---

# FINDING E1 — BLOCKER

## A 25 ms local-delivery latency overrun tears down the entire Remote Play session

**Severity:** blocker (release-blocking: the app "disconnects" during normal play, and the PS5 is left
holding whatever buttons/triggers were down)

**Failure path**
`launcher writes a MustDeliver packet` → `bridge enqueues it` → `bridge waits ≤25 ms for the feedback
sender to reach the UDP socket` → **timeout** → `fail_transport()` → `chiaki_session_abort_orion_transport()`
(wipes the queue + pending history) → `Failed` ACK → `chiaki_session_stop()` → session ends → sidecar
readiness fails → 3-attempt recovery ladder → ~9 s outage → repeat on the next input burst.

**Affected files**
- `C:\Users\aaron\Desktop\chiaki-ng-src\gui\src\orioninputbridge.cpp:20` — `ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25`
- `…\orioninputbridge.cpp:281-292` — the wait and the fatal escalation (`"local_delivery_timeout_or_mismatch"`)
- `…\orioninputbridge.cpp:143-165` — `fail_transport`: `:150` abort, `:151` Failed ACK, `:163-164` `stop_` + `chiaki_session_stop`
- `…\orioninputbridge.cpp:334-340` — `owner_pipe_disconnected`, the *second* session-stop path
- `C:\Users\aaron\Desktop\chiaki-ng-src\lib\src\feedbacksender.c:459-495` — `abort_orion_transport` calls
  `chiaki_orion_input_queue_init` (`:470`) and zeroes `history_packet_len` (`:483`) → **every queued
  release is discarded**
- `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionInputClient.cpp:350-352` — client maps
  `stage == Failed` to `WrittenUnconfirmed` + `ERROR_GEN_FAILURE`; `:566-571` closes the pipe
- `C:\Users\aaron\Desktop\NexusVision\native_orion\src\OrionAppController.cpp:12979-12986` — the
  `"Controller route changed…"` line (a *consequence*, see E4)

**Impact**
- The owner's reported **"Venice disconnects"** (bug 1). It is not the video stream — capture/detector stay
  live throughout; it is the console *input session* being stopped by our own bridge.
- ~9 s of no console input per incident; automation disarmed; 10 incidents in 21 min.
- On the 03:11 incident the sidecar's ladder exhausted (`logs/orion_native.log:22244-22246`) and the app
  fell all the way to `Remote Play: Error` + `Controller state: Physical controller live`.
- **Root cause of bug 2 (R2 clamps down)** as well: `abort_orion_transport` drops the queued
  trigger/button *release* history events and then stops the session, so the PS5 keeps holding R2 (or
  Square/X) until a fresh session seeds a full state ~9 s later. Confidence: high.

**Reproduction (observed, 10×)**
1. Stream running, pipe route owned (`ack_stage=2`, `connected=1`).
2. Produce a burst of button/trigger edges (a TRIANGLE+CIRCLE chord, a sprint ramp, a dribble flick).
3. One flagged packet's local delivery exceeds 25 ms.
4. `Input hook heartbeat: connected=0 … ack_winerr=31 ack_stage=128 ack_error=15`.
5. `INPUT LINK LOST: reason=session_ended`.

**Direct measurement of the overrun**
```
24517: 2026-09-22T03:22:22.929Z  … writes=30843 ack_failures=7 ack_winerr=31 ack_stage=128 ack_error=15 ack_wait_us=25602
24724: 2026-09-22T03:23:27.098Z  … writes=30936 ack_failures=9  ack_winerr=0  ack_stage=2   ack_error=0  ack_wait_us=22806   <- survived, 2.2 ms of margin
24735: 2026-09-22T03:23:28.100Z  … writes=30944 ack_failures=10 ack_winerr=31 ack_stage=128 ack_error=15 ack_wait_us=25119   <- died
```
`ack_wait_us = 25602 / 25119` is the 25 ms bridge budget plus pipe wakeup, to the microsecond.

**Steady-state margin is already gone.** A census of every heartbeat in the session shows the *healthy*
ACK latency sits on a **14–18 ms plateau** (109 samples ≥ 3 ms; 78 of them in 13.5–18.3 ms), e.g.
lines `21519, 21771, 21849, 22036, 23139, 23546, 24116, 24310, 24462, 24566, 24621`. Two independent
mechanisms park the feedback sender for exactly that long:
- the UDP retry ladder, capped at `CHIAKI_ORION_INPUT_RETRY_MAX_MS = 16` (`lib/include/chiaki/orioninput.h:29`),
  applied as a hard loop gate at `feedbacksender.c:835-846`;
- the Windows default 15.6 ms timer quantum on `chiaki_cond_timedwait_pred` — the feedback-sender thread has
  **no priority boost**: the MMCSS Pro Audio boost was reverted in fork commit `c7515213`.

So the design budget is 25 ms and the observed p90 is ~16 ms. Anything that adds one more queue entry or
one retry crosses it.

**Root-cause hypothesis**
Today's uncommitted fork change made **every pure button release, and every terminal trigger-to-rest
edge, enqueue three FIFO entries instead of one** (primary + 3 ms twin + 40 ms echo), while the sender
drains **at most one entry per loop iteration** and the bridge's per-packet budget stayed at 25 ms.
A two-button chord therefore queues 8 entries where the previous build queued 4–5.

- `lib/include/chiaki/orioninput.h:22-29` — `QUEUE_CAPACITY 64`, `RELEASE_REDUNDANT_DELAY_MS 3`,
  `RELEASE_ECHO_DELAY_MS 40`, `RETRY_MAX_MS 16`
- `lib/src/feedbacksender.c:263-265` — `entries_needed = 1 + redundant_state + 2*redundant_history`
  (`git diff` shows the old line was `entries_needed = redundant_release ? 2 : 1`)
- `lib/src/feedbacksender.c:96-99` — `redundant_history` now fires for **any** pure release, not just SHOT_RELEASE
- `lib/src/feedbacksender.c:885-897` — "Consume at most one queued Orion transaction per sender iteration"
- `lib/src/orioninput.c:64-82` — `is_pure_button_release` explicitly includes `r2_state != 0 → 0`
- The author of the change already saw the conflict and wrote it down at `feedbacksender.c:274-276`:
  *"A delayed optional history copy cannot hold the next required edge behind the 40 ms idle spacing
  (the bridge's delivery budget is only 25 ms)."* The `expedite_history_repeats` mitigation
  (`:277`) removes the *artificial wait* but **not the FIFO work** — the entries still have to be sent,
  one per iteration, inside the next packet's 25 ms window.

**Evidence that SUPPORTS the hypothesis**
1. Binary bisect: 24 h / 13,237 heartbeats on `d80be5f8` = 0 failures; 21 min on `0dad53db` = 10 failures.
   (`logs/orion_native.log.1` whole file; `logs/orion_native.log:1794` vs `:21103`.)
2. `git status` in the fork shows `lib/src/feedbacksender.c`, `lib/src/orioninput.c`,
   `lib/include/chiaki/orioninput.h` **modified, uncommitted**, and `git diff` dates the change
   `[2026-09-21 STUCK BUTTON]`.
3. The fatal error code is `CHIAKI_ERR_TIMEOUT` (15), not `OVERFLOW` (5) or `NETWORK` (6) — it is a
   *latency* fault, not a capacity or socket fault.
4. `ack_wait_us` at the two fatal heartbeats equals the budget (25.6 / 25.1 ms).
5. The surviving p100 sample is 22.8 ms — 2.2 ms of margin one second before the kill.

**Evidence that would REFUTE it (and what the log actually says)**
- *"It's just the write burst."* **Refuted.** `logs/orion_native.log:1120` (18:57:07) shows **136 writes/s
  with `ack_stage=2`, 0 failures**; `:872` shows 131 writes/s clean. Conversely the 03:21:51 incident
  (`:24324`, `:24340`) killed the session on a **4-writes/s** second. Burst size is neither necessary nor
  sufficient — *queue depth × per-entry latency* is.
- *"It's the launcher's pipe write timing out (`kWriteTimeoutMs = 3`)."* **Refuted.** `failures=0` in
  every heartbeat of the whole session — `writeFailures_` is only incremented at
  `OrionInputClient.cpp:556`, immediately before `closePipe()`. The client never closed first.
- *"The console/PS5 dropped us."* **Refuted.** The sidecar's message is
  `"the current Chiaki session ended before becoming ready"` (`remote_play_client.py:1823-1826`,
  `last_session_state == "ended"`), i.e. *our own* child ended its session; and capture/transport health
  stayed nominal throughout (`Capture health: … stall=0/0 uniqfps=60`, e.g. `:24475`-adjacent lines).
- *What would refute it now:* run `0dad53db` with `CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS` disabled
  (back to 2 copies) and still see `ack_stage=128`; or find `edge_queue_overflow` / `local_udp_rejected`
  lines in the OrionStream child log dominating the failures (the child log is **not** captured into
  `orion_native.log` — see E6).

**Proposed fix (minimal, does not weaken fail-closed)**

*F1 — split "packet-fatal" from "session-fatal" (`orioninputbridge.cpp:286-292`).*
`CHIAKI_ERR_TIMEOUT` on `chiaki_session_wait_orion_delivery` must fail **that packet** closed and nothing
more: write the `Failed` ACK for that `source_seq` (the launcher already does the right thing with it —
close the pipe, drop to ViGEm, disarm, re-seed on reconnect) and **do not** call
`chiaki_session_stop()`. Keep `enqueue_failed`, `enqueue_ack_write_failed`, `delivery_ack_write_failed`
and a genuine *status mismatch* fatal. Concretely: pass a `bool fatal` to `fail_transport`, and on the
non-fatal path call a new `chiaki_session_drop_orion_queue()` — `abort_orion_transport` minus
`should_stop`, keeping `orion_ownership_active = true` so physical/SDL passthrough is never unmasked —
then `break` the inner read loop with a `packet_fault` flag that suppresses the
`owner_pipe_disconnected` escalation at `:334-340`, so the bridge loops back to `ConnectNamedPipe`.
Recovery then costs ~0.5 s (the client's reconnect throttle, `OrionInputClient.cpp:202`) instead of ~9 s,
with no console-session teardown and no discarded releases.

*F2 — align the budget with the client's own tolerance (`orioninputbridge.cpp:20`).*
The client already waits `kEnqueuedAckTimeoutMs = 25` **then** `kFinalDeliveryAckTimeoutMs = 50` from the
ENQUEUED proof (`OrionInputClient.cpp:80-83`, `:353-359`). The bridge's 25 ms is therefore *tighter than
the client that depends on it*, for no reason. Raise `ORION_LOCAL_DELIVERY_TIMEOUT_MS` to **40** — still
10 ms inside the client's 50 ms ceiling, still fail-closed, and above every latency sample observed in
this session (max 25.6 ms).

*F3 — shed the optional echo under load (`feedbacksender.c:263-265, 134-143`).*
Keep the stuck-button protection where it matters (idle, one release at a time); skip enqueueing the
40 ms echo when `feedback_sender->orion_queue.count > 2`. A release that is immediately followed by more
input is already re-asserted by the next history event, so nothing is lost. This removes the amplification
without reverting the fix for the stuck-X/Cross bug.

**What MUST stay fail-closed (do not touch)**
- `OrionInputClient.cpp:554-559` — any write failure/ambiguity closes the pipe without advancing `last_`.
- `OrionInputClient.cpp:562-572` — `WrittenUnconfirmed` closes the pipe; never race ViGEm against a
  possibly-queued direct packet.
- `OrionInputClient.cpp:429-430` + `:216` — de-dup is valid only while `haveLast_` is provably what
  OrionStream holds; reconnect forces a full-state MustDeliver seed.
- `OrionAppController.cpp:13921-13932` — `inputRouteAwaitingRecovery_` cleared **only** by a forced write
  accepted with physical shot controls neutral for 3 consecutive frames
  (`PreciseFirePolicy.h:398-407`, `ShotIntentPolicy.h:75-86`).
- `OrionAppController.cpp:12968-12986` — revoke the latency-cache attestation the instant the live route
  stops matching the attested one. The bot must never fire on a route it cannot prove.
- `feedbacksender.c:106-113` — a genuine `edge_queue_overflow` stays fatal; that is real backpressure.

**Verification test**
1. Unit (fork, `test/feedbacksender_orion.c`): enqueue a pure release, stall the transport mock for 30 ms,
   assert `chiaki_feedback_sender_wait_orion_delivery` returns `CHIAKI_ERR_TIMEOUT` **and**
   `should_stop == false` and `orion_ownership_active == true` (i.e. the session survives).
2. Unit (fork, `test/orioninput.c`): 4 chord edges (2 presses, 2 pure releases) ⇒ assert queue depth ≤ 6
   with F3 applied, and that every entry's `not_before_ms <= now` after the next edge.
3. Live: replay the chord — mash TRIANGLE+CIRCLE ~7 Hz for 10 s while streaming. Pass = zero
   `ack_stage=128` heartbeats and zero `INPUT LINK LOST: reason=session_ended` in `logs/orion_native.log`.
4. Regression guard: grep the session for `ack_wait_us` ≥ 20000; should be empty.
5. `scripts\verify_orion.ps1 -StrictSecurity` (fork/runtime packaging changed).

---

# FINDING E2 — HIGH

## The launcher's own liveness probes are MustDeliver transactions timed to collide with the release drain

**Severity:** high (self-inflicted; it converts an idle moment into a session kill)

**Affected files**
- `native_orion/src/OrionAppController.h:2239` — `kHookReleaseRepairDelayMs_ = 24`
- `native_orion/src/OrionAppController.cpp:13668-13675` — arms the repair at `release edge + 24 ms`
- `native_orion/src/OrionAppController.cpp:13684-13718` — the repair; logs
  `"Input release-repair duplicate FAILED (result=%1)"`
- `native_orion/src/OrionAppController.cpp:14085-14106` — the 1 Hz idle `reassertLastState()` probe
- `native_orion/src/OrionInputClient.cpp:466-489` — `reassertLastState` sets `OrionInputMustDeliver`
- `chiaki-ng-src/lib/include/chiaki/orioninput.h:24,28` — twin at **3 ms**, echo at **40 ms**

**Impact**
The repair fires at **+24 ms** after a digital release — i.e. *inside* the window in which the fork is
still draining that release's twin (+3 ms) and echo (+40 ms). The repair is itself MustDeliver, so its own
25 ms budget starts while two idempotent copies of the release sit in front of it in the FIFO. Three
incidents died exactly this way, with `result=5` (`WrittenUnconfirmed` — *this* probe's ACK failed, i.e.
the launcher's own liveness packet was the one that killed the session):

```
24514: 03:22:22.817  Input release-repair duplicate FAILED (result=5): direct pipe closed…
24568: 03:22:37.423  Input release-repair duplicate FAILED (result=5): direct pipe closed…
24726: 03:23:28.012  Input release-repair duplicate FAILED (result=5): direct pipe closed…
```
(`result=0` = `Failed` = the pipe was *already* closed by someone else; `result=5` = `WrittenUnconfirmed`
= this call closed it. See `native_orion/src/PreciseFirePolicy.h:55-68`.)

Worst instance: the route recovered at `24506: 03:22:21.777` and was killed again **1.04 s later** by the
repair probe at `24514`.

**Refuting evidence considered:** the probes are *not* the cause of the other 7 incidents (`result=0`
there — the pipe was already gone). They are an amplifier and a second trigger, not the root cause.

**Fix (minimal)**
- Raise `kHookReleaseRepairDelayMs_` from 24 to **60** so it lands past the fork's 40 ms echo spacing; or
  skip the repair entirely when the last accepted packet carried `OrionInputShotRelease`, since the
  fork's own release redundancy already re-proves that exact state.
- Gate the 1 Hz idle reassert (`:14090-14095`) on `idleInputNowMs - lastReleaseEdgeMs >= 60` in addition
  to the existing 250 ms digital-rest requirement.
- Both are pure *liveness* probes. Their fail-closed semantics are unchanged — they still close the pipe
  on an unconfirmed ACK; they simply stop racing the transport's own reliability machinery.

**Verification test**
Offline: assert `kHookReleaseRepairDelayMs_ > CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS` as a static
assert / a doc-tested invariant pair, so the two trees can never drift into a collision again.
Live: after a Square release, confirm the next `Input hook heartbeat` shows `ack_wait_us < 5000`.

---

# FINDING E3 — HIGH

## Analog trigger travel is classified as a "button edge" ⇒ every L2/R2 step is a MustDeliver round trip

**Severity:** high (this, not the chord, is the highest-rate generator of the burst)

**Affected files**
- `native_orion/src/OrionInputClient.h:200-213` — `buttonOrTriggerEdge` includes
  `previous->l2_state != current->l2_state || previous->r2_state != current->r2_state` ⇒ `MustDeliver`
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:190, 206, 207-208` — the coalescer **stops** at any flagged
  packet *and* at any trigger value change, so trigger steps are never coalesced
- `chiaki-ng-src/lib/src/orioninput.c:77-81` — the terminal `r2 → 0` step is additionally a *pure release*
  ⇒ 3 queue entries

**Impact**
A single DualSense sprint pull produces 10–40 distinct `r2_state` values in ~150 ms. Each one is a
separate synchronous ACK transaction with its own 25 ms budget, none of which may be coalesced, and the
last one mints 3 FIFO entries. This is the mechanism behind the burst pattern
`writes 8934 → 8961` (27 in one second, `logs/orion_native.log:22209` → `:22211`) with **no other log
line in between** — no shot epoch, no `Special button edge` (which only covers options/create/ps/touchpad,
`OrionAppController.cpp:12236-12238`). It also explains the owner's R2 report: the trigger is the control
most likely to be mid-travel when the session is torn down, and its release events are the ones
`abort_orion_transport` throws away.

**Fix (minimal)**
Treat *intermediate* trigger travel as ordinary latest-wins movement, and keep MustDeliver only for the
transitions that carry meaning:
```
const bool triggerEdge =
    (previous->l2_state == 0) != (current->l2_state == 0)      // press / terminal release
 || (previous->r2_state == 0) != (current->r2_state == 0);
```
i.e. in `classifyOrionInputPacketFlags` split `buttonOrTriggerEdge` into `buttons != buttons` (always
MustDeliver) and this zero-crossing trigger rule. Partial travel still reaches the console through the
ordinary latest-wins state packet — exactly as the stick already does — and `orioninput.c:78` already
uses the same zero-crossing definition for "terminal trigger release". Nothing becomes unproven: the
press edge and the release edge remain ordered, ACKed transactions.

**Verification test**
Unit: `classifyOrionInputPacketFlags` with `r2 = 0→12→37→255→190→0` must return `MustDeliver` for exactly
the first and last steps. Live: a full sprint ramp should now show ≤ 3 `ack_stage=2` transactions instead
of ~30; `writes/s` during play should fall by roughly an order of magnitude.

---

# FINDING E4 — MEDIUM

## "Controller route changed … authority revoked" is correct, and the ack-lag fields are a red herring

**Severity:** medium (diagnostic only — but it has been read as the cause, which it is not)

**Affected files**
- `native_orion/src/OrionAppController.cpp:12963-12986` — the log line; `liveRouteBeforeProcess` from
  `PreciseFirePolicy::liveControllerRoute` (`PreciseFirePolicy.h:163-176`)
- `native_orion/src/OrionTypes.h:34-39` — `None=0, Pipe=1, VigemDs4=2, VigemXusb=3`
- `native_orion/src/OrionAppController.cpp:13961-13984` — the heartbeat writer
- `native_orion/src/OrionInputClient.cpp:228-372` — `waitForDeliveryAck`, the only writer of
  `lastAckSourceSeq_` / `lastAckExpectedSeq_`

**There is no ack-lag watchdog.** Nothing in either tree reads `ack_seq` vs `expected_seq` vs `writes` and
turns it into a route change. The two fields are written **only** inside `waitForDeliveryAck`, i.e. only
for MustDeliver / ownership-release packets (`OrionInputClient.cpp:563-565`). Plain latest-wins packets
(stick movement) never touch them. So `writes=8961 / ack_seq=8939` is **normal and benign**: 22 of those
27 writes were unflagged movement packets that were never supposed to be ACKed. `ack_seq == expected_seq`
in every heartbeat of the session — the ACK protocol never once mismatched.

`generation=N attested=1 live=3` means literally: *the attested route was `Pipe` (1), the live route is
now `VigemXusb` (3)*, which is true the instant `orionInput_.connected()` goes false. The revoke is
correct and must stay. What is false is the *premise* — the pipe should not have been closed.

Equally, `"Timing route acknowledgement crossed a sidecar scope transition: old_epoch=3 new_epoch=4;
proof revoked"` (`OrionAppController.cpp:2782-2791`, sidecar side `remote_play_orchestrator.py:2928`,
`:3011`) is not a fault: a fresh Chiaki child = a fresh sidecar latency scope epoch, so the in-flight
generation-3 proof is correctly discarded and generation 4 is re-issued 2 ms later
(`logs/orion_native.log:22219-22221`). Both lines are downstream bookkeeping.

**Fix (minimal, optional):** add the *reason* to the route-change line so it cannot be misread —
e.g. append `pipe_connected=0 last_ack_stage=128 last_ack_error=15`. Zero behavioural change.

---

# FINDING E5 — MEDIUM

## The repeat-history invariant check can stop the feedback-sender thread outright

**Severity:** medium (latent; not observed in this log, but it is a session-killing path on a data mismatch)

**Affected files**
- `chiaki-ng-src/lib/src/feedbacksender.c:598-636` — `repeat_history` identity check returns
  `CHIAKI_ERR_INVALID_DATA` when `orion_repeat_history_size == 0` or the saved `source_seq`/`flags` do not
  match `history_dirty_*`
- `chiaki-ng-src/lib/src/feedbacksender.c:874-882` and `:962-967` — the caller:
  `if(history_flush_err != SUCCESS && != OVERFLOW) { should_stop = true; break; }`

**Impact** A redundant-history entry whose primary never formatted a history packet (primary had
`needs_history == false`) hits the `INVALID_DATA` branch, which **terminates the feedback sender thread**
— every subsequent input, including releases, is silently undeliverable and the session dies with no
`Failed` ACK to tell the launcher why. This path is brand new (same uncommitted 2026-09-21 change) and is
reachable whenever the primary's `controller_state_has_history_change` is false.

**Fix (minimal):** treat `INVALID_DATA` on the repeat path like `OVERFLOW` — drop the *optional* copy
(clear `orion_inflight_needs_history`, log once) and continue. An optional idempotent repeat is never
worth killing the transport; the primary was already delivered and ACKed.

**Verification test:** unit — dequeue a `redundant_history` entry with
`orion_repeat_history_size == 0`; assert `should_stop == false` and that the primary's completion was
still recorded.

---

# FINDING E6 — LOW

## The OrionStream child log is not joined into orion_native.log, so the server-side reason is invisible

`grep -ac "Orion transport" logs/orion_native.log` → **0**. Every `fail_transport` reason string
(`"local_delivery_timeout_or_mismatch"`, `"enqueue_failed"`, `"owner_pipe_disconnected"`,
`edge_queue_overflow`, `local_udp_rejected … retry_ms=`) lives only in the per-session child log named in
`INPUT LINK LOST: … session_log=chiaki_session_2026-09-21_22-21-58-816816.log`
(`logs/orion_native.log:24476`). Diagnosing this required reading the fork source and inferring the reason
from `ack_error=15`.

**Fix (minimal):** on `INPUT LINK LOST`, have the sidecar tail the named session log for
`^Orion transport:` / `fatal_local_delivery_fault` and emit those lines into `orion_native.log`. Pure
observability; no behaviour change. Without it, the next occurrence is just as expensive to diagnose.

---

# 1. The periodic cycle from 22:22 — sequence diagram

Times are the log's UTC stamps (22:2x local = 03:2x Z). `L#` = line in `logs/orion_native.log`.
One full period, 03:22:12.656 → 03:22:22.817 (the tightest of the four):

```
 LAUNCHER (OrionNative)            FORK (OrionStream / OrionInputBridge)         SIDECAR (remote_play_orchestrator)
 ─────────────────────             ──────────────────────────────────           ────────────────────────────────
 t-1.0s  burst of MustDeliver
         packets (43→46 writes/s)
         L24472 writes=30667
         OrionAppController.cpp
           :13470 submit path
              │ WriteFile 24 B ───────►  ReadFile, coalescer refuses to
              │                          coalesce a flagged packet
              │                          orioninputbridge.cpp:190,206
              │                       ── chiaki_session_set_controller_state_nowait_ex
              │                          feedbacksender.c:263  entries_needed = 3
              │                          (primary + 3 ms twin + 40 ms echo)
              │  ◄── ACK Enqueued ────   orioninputbridge.cpp:263
              │  (client arms the 50 ms
              │   final window,
              │   OrionInputClient.cpp:358)
              │                          chiaki_session_wait_orion_delivery(seq, 25 ms)
              │                          orioninputbridge.cpp:281
              │                             feedback sender is behind:
              │                             • ≤1 entry drained per iteration :885
              │                             • retry gate ≤16 ms            :835
              │                             • cond wait quantum ~15.6 ms
              │                          ***  25 ms elapses  ***
 ┌──────────────────────────────── 1. FORK INITIATES THE CLOSE ────────────────────────────────┐
 │            │                     fail_transport(seq, CHIAKI_ERR_TIMEOUT,                    │
 │            │                       "local_delivery_timeout_or_mismatch")                    │
 │            │                       orioninputbridge.cpp:288-290                             │
 │            │                       :150 abort_orion_transport  → feedbacksender.c:470       │
 │            │                                queue_init()  ← EVERY QUEUED RELEASE DISCARDED  │
 │            │  ◄── ACK stage=0x80 ── :151 orion_write_input_ack(Failed, err=15)              │
 │            │                       :163-164 stop_ = true; chiaki_session_stop(session_)     │
 └─────────────────────────────────────────────────────────────────────────────────────────────┘
 03:22:12.656 client: stage==Failed
         → fail(ERROR_GEN_FAILURE)
           OrionInputClient.cpp:351-352
         → WrittenUnconfirmed
         → closePipe()          :566-571
                                          :347 DisconnectNamedPipe, bridge thread exits
 L24473 "Controller route changed before automation process:
         generation=16 attested=1 live=3; scheduled authority revoked."
         OrionAppController.cpp:12979
 L24474 "Input release-repair duplicate FAILED (result=0): direct pipe closed"
         OrionAppController.cpp:13714      (result=0 = Failed = pipe ALREADY gone)
 L24475 "Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input."
         OrionAppController.cpp:13943
 L24482 heartbeat connected=0 ack_winerr=31 ack_stage=128 ack_error=15   ← the server's verdict, verbatim

 03:22:12.855                                                    L24476 "INPUT LINK LOST: reason=session_ended
                                                                        pid=39484 session_log=chiaki_session_…"
 03:22:12.858 inputRecoveryStarted                               remote_play_orchestrator.py:3264 ladder
         OrionAppController.cpp:2667-2694                        plans = ((0.0,3.0),(0.75,9.0),(1.25,3.0)), 17 s deadline
         inputRouteAwaitingRecovery_ = true
         automation_.setArmed(false)
         orionInput_.resetConnection()   ← haveLast_=false, next packet is a full-state seed
 L24477-24481 "Input-only recovery started… capture/detector retained"

 03:22:16.831                                                    L24492 "Remote Play input session failed readiness:
                                                                        …the current Chiaki session ended before
                                                                        becoming ready"   remote_play_client.py:1823
                                                                 (attempt 1 raced the dying session)
 03:22:21.776                                                    L24503 "recover_input: fresh Chiaki console session ready"
 03:22:21.777 pipe reconnects; forced MustDeliver seed accepted
 L24506 "Direct controller pipe recovered: forced write accepted after physical shot controls were
         neutral; fail-closed gate cleared."   OrionAppController.cpp:13921-13932
         (routeRecoveryReady: pipeAccepted && Square up && RS centred && 3 neutral frames)
 L24508 "Controller route isolation: direct Chiaki pipe owns PS5 input"  → generation=17 attested

 03:22:22.817  ← 1.04 s later, the +24 ms release-repair probe (kHookReleaseRepairDelayMs_ = 24)
 L24514 "Input release-repair duplicate FAILED (result=5)"  ← result=5 = WrittenUnconfirmed:
         THIS probe's ACK timed out (heartbeat L24517 ack_wait_us=25602).  Cycle repeats.
```

Repeats at `L24569` (03:22:37.423), `L24625` (03:22:52.662), `L24727` (03:23:28.013).
Period = ~8.4–9.0 s of rebuild (sidecar ladder + Chiaki child boot + PS5 handshake) plus 1–14 s of play.

### Who initiates each close

| # | time (Z) | initiator | evidence |
|---|---|---|---|
| all 10 | — | **the fork bridge** (`fail_transport`, `orioninputbridge.cpp:288`) | `ack_stage=128`, `ack_error=15`, `failures=0` |
| 7 of 10 | e.g. 03:11:12, 03:13:39, 03:14:34, 03:16:07, 03:21:51, 03:22:12, 03:22:52 | fork kills it during a **gameplay** MustDeliver packet | repair sees `result=0` — pipe already gone |
| 3 of 10 | 03:22:22, 03:22:37, 03:23:28 | fork kills it during the **launcher's own release-repair probe** | repair sees `result=5` = it closed the pipe itself |

The launcher's `closePipe()` is always the *second* event, and always a reaction to a `Failed` ACK.
The sidecar never closes anything: it only observes `session_ended` and relaunches.

### Is it a livelock?

**No.** There is no mutual blocking. Every recovery *genuinely succeeds*: the sidecar proves a fresh
console session (`recover_input: fresh Chiaki console session ready`), and the launcher's forced write is
accepted **1–4 ms later** (`L24503` → `L24506`, `L24547` → `L24550`, `L24602` → `L24605`). The
launcher's "forced write after physical shot controls were neutral" gate is not fighting the sidecar's
readiness check, and the fork's disconnect is not fighting either of them.

It is a **retrigger loop**: the fault is re-armed by *ordinary continuing input*. Each rebuild restores a
route whose per-packet budget is already inside the noise floor, so the next burst (or, three times, the
launcher's own +24 ms probe) trips it again.

### What ends it

Two exits, both observed:
1. **Input goes idle.** From `03:23:01.846` (`L24665`) the heartbeats drop to `writes` +1/s — the 1 Hz
   idle reassert only — and the route survives 26 s untouched (`L24665` → `L24727`). After the final
   recovery at `03:23:37.509` (`L24768`) the session runs to the end of the log.
2. **The sidecar's ladder exhausts.** At `03:11:19.642` (`L24244-24246`) all 3 attempts fail inside the
   17 s deadline (`remote_play_orchestrator.py:3260-3320`); the app transitions to
   `Remote Play: Error` + `Controller state: Physical controller live`, so
   `directInputWriteAllowed(...)` is false, no MustDeliver packet is ever written, and there is nothing
   left to trip. The cycle stops — by giving up.

---

# 2. The FIRST incident, 03:11:12Z — exact trigger

**The burst.** `writes 8934 → 8961` between `L22209` (03:11:11.066) and `L22211` (03:11:12.068):
**27 packets in one second**, with `ack_seq` advancing only 8934 → 8939 (5 flagged transactions) and
`ack_failures=0`, `failures=0`. No other log line falls in that second.

**What produces ~27 writes.** Every call to `OrionInputClient::sendDetailed` that changes the mapped
packet writes one 24-byte record (`OrionInputClient.cpp:429-432`). The de-dup key is
`buttons | left_x | left_y | right_x | right_y | l2_state | r2_state | own` (`:62-67`). With RawInput
polling the DualSense at ~250 Hz, 27 changes/s is:
- ~4 button-mask edges per TRIANGLE+CIRCLE chord (TRI↓, TRI+CIR↓, one up, the other up — the two buttons
  essentially never land in the same 4 ms report), so ~7 chords/s; **and/or**
- any L2/R2 travel, since **every analog trigger step is its own packet** (finding E3).

Five of the 27 were flagged (`ack_seq` 8934→8939) — consistent with 4 chord edges plus one stick
band-crossing or trigger zero-crossing.

**Is `ack_seq` lag the watchdog input? No — there is no such rule.** See finding E4. `writes` racing
ahead of `ack_seq` is the *designed* behaviour for unflagged latest-wins packets. The only watchdogs that
exist are:
- `OrionInputClient::waitForDeliveryAck` — `kEnqueuedAckTimeoutMs = 25` for the ENQUEUED proof,
  then `kFinalDeliveryAckTimeoutMs = 50` from that proof (`OrionInputClient.cpp:80-83, :268, :358`);
  and `kWriteTimeoutMs = 3` for the overlapped write itself (`:73, :523`).
- the **bridge's** `ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25` (`orioninputbridge.cpp:20, :282`) — **this is
  the one that fired**.
- the hook-down escalation (`OrionAppController.cpp:13990-14059`): 3 input-only recoveries
  (`kInputHookRecoveryMaxAttempts`), then one contained sidecar restart. Counted in whole heartbeats
  (1 Hz) and only while `connected == false` — it plays no part in *causing* the close.
- the recovery clear (`PreciseFirePolicy.h:398-407`): `pipeAccepted && Square-up && |RS| < neutralRadius`
  for `3` consecutive GUI frames.

**The chain at 03:11:12** (`L22211` → `L22228`):
1. 27 writes, 5 of them flagged; the bridge serialises all 5 through `wait_orion_delivery(25 ms)`.
2. Each chord release mints 3 FIFO entries (`feedbacksender.c:263, 126-143`); the sender drains ≤1 per
   iteration (`:885`) with a ~15.6 ms wake quantum and a ≤16 ms retry gate (`:835`).
3. Packet `seq=8977` misses its 25 ms window → `fail_transport(TIMEOUT)` → `Failed` ACK + session stop.
4. `L22228` heartbeat: `connected=0 writes=8977 failures=0 ack_failures=1 ack_winerr=31 ack_stage=128
   ack_error=15 ack_seq=8977 expected_seq=8977`.

**Can a human TRIANGLE+CIRCLE chord produce it? YES — and it is the most likely proximate trigger.**
Not through any combo handler: there is none (confirmed — the only chord/hotkey in either tree is the
D-pad-Up meter-delay bypass). The mechanism is purely arithmetic:
- a chord is **two pure releases**, and a pure release is the *only* input class that mints 3 FIFO
  entries (`orioninput.c:64-82` + `feedbacksender.c:96-99, 126-143`);
- two releases arriving within one drain window queue **6 entries**, plus 2 press primaries = 8;
- the bridge grants the *second* release 25 ms to reach the socket while the *first* release's twin and
  echo are still ahead of it in a strictly-FIFO, one-per-iteration queue.
- Neither button is logged (`OrionAppController.cpp:12236-12238` logs only options/create/ps/touchpad),
  which is exactly why the second in question contains 27 writes and nothing else.

**Why it did not happen yesterday:** on `d80be5f8` a chord queued 4–5 entries, not 8, because
`redundant_release` fired only for a right-stick Tempo reversal (`git diff lib/src/feedbacksender.c`,
old line `entries_needed = redundant_release ? 2 : 1`). 24 h / 13,237 heartbeats on that build:
**zero** ACK failures.

---

# 3. Minimal change set (ranked), and what must stay fail-closed

**Do (in this order):**
1. **F1 — `orioninputbridge.cpp:286-292`: a local-delivery TIMEOUT fails the *packet*, not the *session*.**
   Write the `Failed` ACK; drop the queue via a new `drop_orion_queue()` (`abort_orion_transport` minus
   `should_stop`, `orion_ownership_active` stays true); break the read loop with a flag that suppresses
   the `owner_pipe_disconnected` escalation at `:334-340`; loop back to `ConnectNamedPipe`. Status
   *mismatch*, enqueue failure and ACK-write failure stay session-fatal.
   → turns a 9 s console-session teardown into a ~0.5 s pipe reconnect, and stops discarding releases.
2. **F2 — `orioninputbridge.cpp:20`: `ORION_LOCAL_DELIVERY_TIMEOUT_MS 25 → 40.`**
   Still strictly inside the client's own 50 ms final-ACK ceiling (`OrionInputClient.cpp:83`), so the
   client remains the outer fence and still fails closed. Removes every overrun observed (max 25.6 ms).
3. **E3 — `OrionInputClient.h:200-203`: only trigger *zero-crossings* are MustDeliver.**
   Intermediate analog travel becomes latest-wins, like the stick. ~10× fewer synchronous transactions.
4. **E2 — `OrionAppController.h:2239`: `kHookReleaseRepairDelayMs_ 24 → 60`** (past the fork's 40 ms echo),
   and gate the 1 Hz idle reassert on ≥60 ms since the last release edge.
5. **F3 — `feedbacksender.c:134-143`: skip the 40 ms echo when `orion_queue.count > 2`.**
   Keeps the stuck-button fix at idle, sheds it under load where the next edge re-asserts anyway.
6. **E5 — `feedbacksender.c:598-636`: `INVALID_DATA` on the repeat-history path drops the optional copy**
   instead of setting `should_stop`.
7. **E6 — join the OrionStream child log into `orion_native.log` on `INPUT LINK LOST`.**

**Must stay fail-closed — none of the above touches any of these:**
- ambiguous/failed pipe write ⇒ `closePipe()`, `last_` untouched (`OrionInputClient.cpp:554-559`);
- `WrittenUnconfirmed` ⇒ `closePipe()`, never race ViGEm against a possibly-queued direct packet (`:566-571`);
- reconnect ⇒ `haveLast_ = false` ⇒ first packet is a forced full-state MustDeliver seed (`:216`, `:190-193`);
- `inputRouteAwaitingRecovery_` cleared only by an accepted forced write with Square up, RS centred, for
  3 consecutive frames (`OrionAppController.cpp:13921-13932`, `PreciseFirePolicy.h:398-407`);
- latency-cache attestation revoked the moment live route ≠ attested route (`OrionAppController.cpp:12968-12986`);
- scheduled-fire tokens bound to `(generation, route)` and never inherited across a reconnect
  (`PreciseFirePolicy.h:78-90, 178-188`);
- `edge_queue_overflow` stays fatal (`feedbacksender.c:106-113`) — that is real backpressure, not latency;
- `owning_` stays latched while a client is gone, so SDL/physical passthrough is never unmasked over
  stale commands (`orioninputbridge.cpp:147`, `feedbacksender.c:479-481`).

**Merely over-eager (the whole bug):** escalating a *transient 25 ms latency overrun* to
`chiaki_session_stop()` + `queue_init()`. Fail-closed for that packet was already complete and already
handled end-to-end; the session teardown adds no safety and costs 9 s of input, the queued releases, and
the bot's arm state.

---

# 4. Other inputs that can start the cycle

Ranked by likelihood on this rig.

| # | trigger | mechanism | anchor |
|---|---|---|---|
| 1 | **L2/R2 trigger ramp (sprint)** | every analog step is a MustDeliver, non-coalescable round trip; the terminal `→0` step additionally mints 3 FIFO entries | `OrionInputClient.h:200-203`; `orioninputbridge.cpp:207-208`; `orioninput.c:77-81` |
| 2 | **ICS-link UDP congestion** | `local_udp_rejected` ⇒ retry ladder 1/2/4/8/**16 ms**, applied as a hard loop gate; two consecutive rejects = 32 ms > the 25 ms budget, guaranteed kill | `feedbacksender.c:835-846, 1096-1119`; `orioninput.h:29` |
| 3 | **Any rapid multi-button chord** (X+O, L1+R1, pass-and-cut, D-pad+face) | two pure releases in one drain window = 6 FIFO entries; identical to TRI+CIR, nothing chord-specific | `feedbacksender.c:96-99, 126-143` |
| 4 | **Right-stick shot-band crossings** (dribble moves, Tempo flicks) | `orionVerticalShotBand` crossings mint `MustDeliver | ShotRelease | RedundantFlick`; a reversal adds a 4th entry. Ordinary stick movement is *not* flagged and does coalesce — only crossings are dangerous | `OrionInputClient.h:173-184, 216-229` |
| 5 | **USB pad power / re-enumeration** (the known EPM=1 silent-DualSense issue) | RawInput emits a neutral report then the full held state: a full-state MustDeliver burst plus a pure release for *every* held button and trigger at once | `OrionInputClient.h:190-193`; `OrionAppController.cpp:12240-12248` |
| 6 | **Sidecar / OrionStream restart itself** | `resetConnection()` clears `haveLast_`; the first packet is a forced full-state MustDeliver seed landing on a feedback sender that has not settled. Observed: recovered `03:22:21.777` → killed `03:22:22.817`, 1.04 s | `OrionInputClient.cpp:154-166, 216`; `OrionAppController.cpp:2686-2689` |
| 7 | **GUI-thread stall / log rotation / court change** | the idle reassert and the +24 ms repair both run on the GUI tick; a stall bunches several MustDeliver transactions into one window. Log rotation and a court-change re-key both stall that tick | `OrionAppController.cpp:13952-14106`; `remote_play_orchestrator.py:3011` |
| 8 | **Windows timer-resolution drop** | with no 1 ms `timeBeginPeriod` holder, `chiaki_cond_timedwait` granularity is ~15.6 ms — 62 % of the 25 ms budget spent on scheduling alone. The feedback-sender thread has **no** MMCSS boost (reverted in fork `c7515213`), so it is fully exposed | `feedbacksender.c:849-862`; fork commit `c7515213` |
| 9 | **Anything that raises the fork's send latency** (Astra's uncommitted Takion/thread/time-affinity edits are in this same working tree) | the budget has ~7 ms of margin in steady state; any added latency on the feedback path lands directly on it | `git status` in the fork: `lib/src/takion.c`, `lib/src/thread.c`, `lib/src/time.c` modified |

---

## Confidence

- **Bug (1) "Venice disconnects on TRIANGLE+CIRCLE" — root cause found. Confidence: high (~90 %).**
  The fork bridge stops the Remote Play session on a 25 ms local-delivery timeout; a two-button chord is
  the cheapest way for a human to create the queue depth that exceeds it, and the 2026-09-21 three-copy
  release change is what shrank the margin. Binary bisect (`d80be5f8` clean 24 h / 13,237 heartbeats vs
  `0dad53db` 10 kills in 21 min), `ack_error=15` = `CHIAKI_ERR_TIMEOUT`, and `ack_wait_us = 25602 / 25119`
  all agree. Remaining 10 %: the OrionStream child log (which carries the literal reason string) was not
  captured, so the *reason* is inferred from the error code rather than read.
- **Bug (2) "R2 clamps down" — contributing root cause found. Confidence: medium-high (~70 %).**
  `chiaki_session_abort_orion_transport` wipes the queue and the pending history packets
  (`feedbacksender.c:470, 483`) and *then* stops the session, discarding exactly the release events that
  were in flight; the console then holds R2 for the ~9 s of the outage, during which the ViGEm fallback
  reaches nothing. Surface D/others should check whether a second, independent R2 latch exists on the
  launcher side; the 18:57 excerpts in the packet (`r2=255` persisting across `Release tick`) predate this
  build and may be a separate defect.
