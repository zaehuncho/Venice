# Surface D — session lifecycle, decoder/frame path, fork working tree

Internal red team, 2026-09-21 owner test session. Source read-only; nothing built, launched or committed.
Fork reviewed as the **working tree** (`C:/Users/aaron/Desktop/chiaki-ng-src`, branch `orion`, uncommitted
Takion/thread/time affinity + AV-clock + third-release-echo edits), not HEAD.

## Headline

**I believe I have the root cause of owner bug (1) ("TRIANGLE + CIRCLE makes Venice disconnect").
Confidence: HIGH (~85%).** It is not a chord shortcut and it is not the stream. It is
`OrionInputBridge`'s **25 ms local-delivery budget**: when the fork's feedback sender does not
confirm a flagged input transaction inside 25 ms, the bridge calls `chiaki_session_stop()` and
**kills the entire Remote Play session**. The log proves the fault arrived as
`stage=Failed(0x80)` / `error=15 (CHIAKI_ERR_TIMEOUT)`, which is reachable from exactly one
call site. TRIANGLE+CIRCLE is not special as a *chord*; it is special as **two pure button
releases inside the same 40 ms echo window**, which is the highest-queue-pressure input the
new uncommitted release-redundancy code can be given.

Bug (2) (R2 clamped) is owned by the input/controller surface; the piece that belongs to me
is documented under F-4 (recovery is gated on neutral shot controls and the 3 s × 3 recovery
budget cannot cover a post-kill console re-handshake).

**Answer to the chord question (goal 3): NO.** See F-13.

---

## F-1 · BLOCKER · A 25 ms input-ACK budget tears down the whole Remote Play session

**Failure path**
`OrionInputBridge::run()` → `chiaki_session_wait_orion_delivery(..., 25 ms)` times out →
`fail_transport(seq, CHIAKI_ERR_TIMEOUT, "local_delivery_timeout_or_mismatch")` →
`chiaki_session_stop(session_)` → PS5 session ends → the launcher's input pipe closes →
route falls back to ViGEm → sidecar readiness probe reports "the current Chiaki session ended".

**Affected**
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:20` — `ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25`
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:281-292` — the wait and the mismatch test
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:143-165` — `fail_transport`, **`:164 chiaki_session_stop(session_)`**
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:349-350` — `if(transport_fault) break;` (the bridge thread exits **permanently**)
- Consequences in native: `native_orion/src/OrionAppController.cpp:12977-12983` (route revoked),
  `:13714` (release-repair FAILED), `:13931` (pipe recovered)
- Readiness verdict: `remote_play_client.py:1825`, surfaced at `remote_play_orchestrator.py:2824`

**Impact** — Release blocker. A single 25 ms scheduling/queueing hiccup on the feedback-sender
thread ends the customer's game session. It is not fail-closed in the safe direction: the safe
response to "one input transaction was slow" is to drop that transaction and re-seed, not to
disconnect Remote Play. Measured 10 occurrences in a 22-minute session.

**Reproduction** — Play with the bot live; press TRIANGLE and CIRCLE together (any two button
releases inside ~40 ms of each other will do) while the input write rate is bursting.
`logs/orion_native.log:22212-22215` and the nine repeats listed in the evidence packet.

**Root-cause hypothesis and the evidence**

*Supporting:*
1. The ACK the launcher received carries the exact signature of this call site —
   `logs/orion_native.log`, heartbeat at `2026-09-22T03:11:13.072Z`:
   `ack_stage=128 ack_error=15`. `OrionInputAckStage::Failed = 0x80 = 128`
   (`gui/include/orioninputbridge.h:51`) and error 15 is `CHIAKI_ERR_TIMEOUT`
   (16th enumerator, `lib/include/chiaki/common.h:42-57`). `fail_transport` is the only
   writer of stage `Failed`, and of its four reasons **only**
   `local_delivery_timeout_or_mismatch` can carry `CHIAKI_ERR_TIMEOUT`:
   `enqueue_failed` carries the enqueue error (`CHIAKI_ERR_OVERFLOW` from
   `feedbacksender.c:253-270`), and both ack-write reasons and `owner_pipe_disconnected`
   hard-code `CHIAKI_ERR_NETWORK` (`orioninputbridge.cpp:272, 306, 339`).
2. The write burst immediately precedes it. Idle rate was 1 write/s for the preceding 30 s
   (`03:10:43.988` → `03:11:11.066`, writes 8904→8934). The heartbeat at `03:11:12.068`
   shows **writes=8961 (+27 in one second) with ack_seq stuck at 8939 — a 22-packet ACK
   backlog** and `ack_wait_us=0`. 480 ms later the pipe was closed.
3. The 40 ms echo Astra added is *larger than the 25 ms budget it must not block*.
   `CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS 40u`
   (`lib/include/chiaki/orioninput.h:26`) vs the bridge's 25 ms. The author saw this —
   `feedbacksender.c:274-277` literally says *"the bridge's delivery budget is only 25 ms"* —
   and mitigated it with `chiaki_orion_input_queue_expedite_history_repeats()`
   (`lib/src/orioninput.c:40-50`). That mitigation removes the *artificial* wait but does
   nothing about the **work**: a chord release enqueues 3 entries per release
   (`feedbacksender.c:276-278`, `entries_needed = 1 + redundant_state + 2*redundant_history`),
   so two releases inside one window put up to 6 strict-FIFO datagrams ahead of the next
   flagged packet, each requiring a separate lock/encrypt/sendto on the feedback thread.
   That is the queue-pressure mechanism by which TRIANGLE+CIRCLE specifically, rather than a
   single button, reaches the budget.
4. The releases qualify. `chiaki_orion_input_is_pure_button_release`
   (`lib/src/orioninput.c:64-81`) returns true for any button-up with no simultaneous press —
   which is every physical TRIANGLE-up and CIRCLE-up, not just the bot's Square.
   Before this uncommitted change only `SHOT_RELEASE` got copies.

*Refuting / what would falsify this:*
- If `ack_error=15` could be produced by `enqueue_failed`. It cannot:
  `chiaki_feedback_sender_set_controller_state_nowait` returns `CHIAKI_ERR_OVERFLOW` (5) on a
  full queue (`feedbacksender.c:253`), which would print `ack_error=5`.
- If the 25 ms wait had been satisfied and the mismatch branch fired instead
  (`delivery_status != expected_status`). That path passes `CHIAKI_ERR_UNKNOWN` (1), not 15.
- **Not yet proven:** that the chord (rather than ambient jitter) is what consumed the budget.
  The bridge and feedback sender log `pipe_received`, `edge_enqueued` and
  `ack_local_delivery ... queue_wait_us=` at `CHIAKI_LOGI`, and **OrionStream's own log for this
  session is not in `orion_native.log`** — only the launcher-side view survived. Pulling the
  matching `logs/chiaki_session_*.log` for 03:11:12 and reading `queue_wait_us` on the seconds
  before the fault would settle it in one step. If those lines show `queue_wait_us` in the
  hundreds of microseconds right up to the fault, the trigger is a *stall* (see F-2/F-3), not
  queue depth, and the fix in F-1 still stands but the trigger attribution moves.

**Minimal fix (two lines of policy, not architecture)**
1. `orioninputbridge.cpp:286-292` — a delivery timeout must **not** be fatal. Send the `Failed`
   ACK (the launcher already handles it: it re-seeds and falls back to ViGEm within 2 ms,
   `OrionAppController.cpp:13714/12977`), **do not** call `chiaki_session_stop`, **do not** set
   `stop_`, and `continue` the read loop so the next packet is served. Reserve
   `chiaki_session_stop` for `owner_pipe_disconnected` and ack-write failure, where the transport
   itself is gone. Rationale: a timeout means "this transaction is late", and the launcher's
   own fail-closed gate already neutralises the pad; killing the console session converts a
   recoverable 25 ms miss into a 7-second outage plus three failed recoveries.
2. Raise `ORION_LOCAL_DELIVERY_TIMEOUT_MS` above the worst queued-work bound it must cover.
   With a 40 ms echo in the FIFO, 25 ms is arithmetically indefensible; 60 ms is the smallest
   value that cannot be beaten by one echo plus a scheduler quantum.

**Verification test**
- Unit (fork, no build by me — for Astra): extend `test/feedbacksender_orion.c` with a case that
  enqueues two pure releases 3 ms apart and asserts (a) the 6 resulting entries all drain and
  (b) a flagged packet pushed immediately after reaches `UDP_ACCEPTED` within the budget.
- Live: replay the owner's chord 20× with the fork's `Orion transport:` lines captured to
  `logs/chiaki_session_*.log`; pass = 0 `fatal_local_delivery_fault`, 0
  `Controller route isolation` in `orion_native.log`.
- Regression guard: `grep -ac "fatal_local_delivery_fault" logs/chiaki_session_*.log` must be 0
  for a full session.

---

## F-2 · BLOCKER · The feedback sender hard-stops on a history-flush error, and nothing above it is told

**Failure path**
`feedback_sender_flush_history_locked()` returns any error other than `CHIAKI_ERR_OVERFLOW` →
`feedbacksender.c:968-973` sets `should_stop = true; break;` → the sender thread exits →
no in-flight Orion transaction can ever complete → **every** subsequent flagged packet hits
F-1's 25 ms timeout → session kill.

**Affected**
- `chiaki-ng-src/lib/src/feedbacksender.c:968-973` — `if(history_flush_err != SUCCESS && != OVERFLOW) { should_stop = true; break; }`
- `chiaki-ng-src/lib/src/feedbacksender.c:600-609` — the **new, uncommitted** `repeat_history`
  identity check that can return `CHIAKI_ERR_INVALID_DATA`
- `chiaki-ng-src/lib/include/chiaki/feedbacksender.h:50-54` — `orion_repeat_history*`, a **single**
  snapshot slot shared by every release in flight
- `chiaki-ng-src/lib/src/feedbacksender.c:484` — `abort_orion_transport` zeroes
  `orion_repeat_history_size`, which is exactly the `== 0` condition that makes the next queued
  echo return `INVALID_DATA`

**Impact** — A new code path introduced by the uncommitted diff can silently retire the only
thread that delivers input, and the *only* symptom the rest of the system gets is F-1's
disconnect. There is no `session_stop` with a reason, no event, no state change — just a dead
thread. This is a fail-open-then-fail-catastrophically construction.

**Root-cause hypothesis**
`orion_repeat_history` is one slot, but the queue can hold echoes for more than one `source_seq`
(three entries per release, releases arriving 3 ms apart). Strict FIFO makes the common
interleaving safe, but `abort_orion_transport` (called by `fail_transport` itself,
`orioninputbridge.cpp:150`) zeroes the slot while echoes may still be queued — so **an F-1 fault
can leave the queue in the state that makes F-2 fire on the next session**, and F-2 then
guarantees F-1 fires again. That is a plausible mechanism for the *clustering* in the log
(10 events in 22 minutes, five of them inside 90 seconds: `22212`, `22611`, `22884`, `23290`,
`24324`, `24473`, `24515`, `24569`, `24625`).

*Supporting:* the cluster shape; `ack_failures` in the heartbeat monotonically climbing
(1→2→5→6→7 across `03:11:13`…`03:22:36`) rather than resetting, i.e. each fault leaves residue.
*Refuting:* `abort_orion_transport` also drains `orion_queue` — if it does (I did not read its
full body; `feedbacksender.c:456-490` is the region) then the stale-echo mechanism is closed and
F-2 is only reachable through a genuine identity mismatch. **This should be confirmed before the
fix is written.** Either way `should_stop = true` on a formatting error is wrong.

**Minimal fix**
1. `feedbacksender.c:968-973` — treat a `repeat_history` `INVALID_DATA` like `OVERFLOW`: drop the
   *optional* copy, clear `orion_inflight_needs_history`, complete the transaction, log once.
   An optional reliability echo must never be able to stop the sender. Keep the hard stop only
   for errors on a **primary** (`!redundant`) entry.
2. Whatever remains fatal must call `chiaki_session_stop` with an explicit reason rather than
   dying silently, so the launcher's log says *why*.

**Verification test** — force `orion_repeat_history_size = 0` in a unit fixture with a queued
`redundant_history` entry and assert the sender keeps running and the next primary completes.
`test/feedbacksender_orion.c` (448 new lines in the working tree) is the right home.

---

## F-3 · HIGH · The uncommitted physical-core affinity is unvalidated topology and fails silently

**Affected**
- `chiaki-ng-src/lib/src/thread.c:149-206` — `chiaki_thread_set_physical_core_affinity`
- `chiaki-ng-src/gui/src/streamsession.cpp:53-74` — the callback; **`:72 (void)chiaki_thread_set_physical_core_affinity(physical_core);`**
- Call sites: `lib/src/takion.c:1102` (TAKION), `lib/src/feedbacksender.c:825` (FEEDBACK),
  `lib/src/gkcrypt.c:504` (GKCRYPT)

**Answering each sub-question**

| Question | Answer |
|---|---|
| Can it pin two latency-critical threads to one core? | **No, and this is the fix the diff makes.** The old `1u << 0` / `1u << 1` put Takion on LP0 and the feedback sender on LP1 — the same physical core on the 5800X. The new code selects the whole physical core's `GroupMask`, so cores 0/1/2 are genuinely distinct. Verified at `thread.c:171-188`. TAKION and TAKION_SEND still share physical core 0 by design (`streamsession.cpp:63, 66`) — on SMT that is 2 LPs and fine; on a **non-SMT** part it is one LP for both, unchanged from before. |
| Can it pin to a nonexistent core? | **No.** The walk increments `core_index` only on `RelationProcessorCore` and falls off the end returning `CHIAKI_ERR_THREAD` (`thread.c:200-201`). On a 2-core box the GKCRYPT pin simply does not happen. The comment at `streamsession.cpp:69-71` states this is intentional. |
| Is `SetThreadGroupAffinity` failure ignored? | **Yes — and this is the real finding.** `thread.c:180-184` returns `CHIAKI_ERR_THREAD`, and `streamsession.cpp:72` discards it with `(void)`. Nothing is logged, at any level. Failure is guaranteed whenever the desired mask is not a subset of the process affinity mask — a job object, `start /affinity`, a VM with a CPU set, or a `SetProcessAffinityMask` from any tool. A support engineer reading a customer's log cannot tell a pinned build from an unpinned one. Given F-1, *this is the difference between a smooth session and a disconnect*, and it is invisible. |
| Can it crash? | **No.** `malloc` is checked (`:156`), the two-call size protocol is checked including `buffer_size == 0` (`:151-153`), and the record walk bounds-checks `info->Size` both below (`sizeof(DWORD)*2`) and above (`> end - cursor`) before advancing (`:169-170`). I found no out-of-bounds or infinite-loop path. |
| Hybrid P/E-core Intel? | **Unhandled, and the most dangerous real-world case.** `RelationProcessorCore` enumerates E-cores as ordinary cores and the code **ignores `Processor.EfficiencyClass`** entirely (`thread.c:171`). Index 1 and 2 are P-cores only by firmware convention, not contract — on the Core Ultra LP-E topologies the low indices are not reliably performance cores. Worse, `SetThreadGroupAffinity` is a *hard* confinement that overrides Thread Director, so a feedback sender that lands on an E-core stays there under EcoQoS. Per F-1, that thread being slow is what disconnects the session. |
| VM / unexpected layout? | Handled by falling through to "unpinned", which is correct — but silently (see above). |

**Impact** — On the owner's 5800X this diff is a genuine improvement. On a hybrid Intel customer
machine it can hard-pin the input-timing thread to an efficiency core, and the failure mode is
not "slightly more jitter" — via F-1 it is "Venice disconnects".

**Minimal fix**
1. `streamsession.cpp:72` — capture the return and `CHIAKI_LOGI`/`CHIAKI_LOGW` it once per thread:
   `"Orion affinity: thread=%s physical_core=%u result=%s"`. One line, makes every future
   latency report interpretable.
2. `thread.c:171` — skip cores whose `Processor.EfficiencyClass` is below the maximum observed
   in the same enumeration (one extra pass over the same buffer). If no performance-class core
   with the requested index exists, return `CHIAKI_ERR_THREAD` and stay unpinned.
3. Consider `SetThreadIdealProcessorEx` instead of hard group affinity: it gives the scheduler the
   placement hint without removing its ability to migrate off a parked or throttled core.

**Verification test** — the working tree already has `test/thread_affinity.c` (untracked). Add
cases for: 1 physical core, 2 physical cores (index 2 must fail cleanly), a mocked hybrid layout
where index 1 is `EfficiencyClass 0` (must be skipped), and a restricted process affinity mask
(must return `CHIAKI_ERR_THREAD`, not succeed). Field check: run the launcher under
`start /affinity 3` and confirm the new log line reports the failures.

---

## F-4 · HIGH · Input-link recovery cannot cover a console re-handshake, so one 25 ms miss becomes a hard Error

**Failure path**
F-1 kills the session → `RemotePlaySession::recoverInputLink()` → sidecar
`recover_input_link()` → three attempts at a **3.00 s readiness budget** each, ~2.2 s apart →
all three report "the current Chiaki session ended before becoming ready" → Error state,
automation disarmed, the owner must reconnect by hand.

**Affected**
- `native_orion/src/RemotePlaySession.cpp:3690-3752` — `recoverInputLink`, `kInputSessionRecoveryDeadlineMs`
- `remote_play_orchestrator.py:3236-3320` — the 3-attempt loop, `readiness_budget=3.00s`
- `remote_play_client.py:1780-1861` — the readiness poll and `_reap_failed_start`
- Log: `logs/orion_native.log:22231, 22236, 22244` (three failures), then
  `03:11:19.646 "Input-link recovery exhausted"` → `03:11:19.660 "Remote Play error"`

**Impact** — Converts a recoverable input fault into a full stop. Observed: 10 faults, 8
recoveries, and the two that did not recover ended the owner's session.

**Root-cause hypothesis** — the recovery budget was sized for a *healthy* cold launch. The
client's own measurement comment says the Chiaki child launch plus PS5 stream handshake is
**~2.4 s** (`remote_play_client.py:1805-1807`). A 3.00 s budget leaves ~0.6 s of margin and
**zero** allowance for the console first tearing down the session `chiaki_session_stop` just
killed. Three attempts inside 4.8 s all land inside that teardown window.

*Supporting:* every one of the three failures reports `last_session_state == "ended"`
(`remote_play_client.py:1825`) — the child launched and the console refused/ended the session,
which is the signature of a console that has not released the previous one. The recoveries that
*did* succeed (`03:14:43`, `03:16:36`, `03:22:00`, `03:22:31`, `03:22:46`) all came **4-9 s
after** their fault, i.e. after the console had more time.
*Refuting:* if the fork's `chiaki_session_stop` is synchronous and the PS5 frees the slot in
under a second, the budget is not the binding constraint and the cause is elsewhere (e.g. the
named-pipe collision in F-8). Distinguish by timing a manual reconnect after a forced
`fatal_local_delivery_fault`: if it succeeds at 4 s but not at 2 s, this finding is confirmed.

**Minimal fix** — after a fault whose class is "console session ended", make attempt 1 wait a
console-release beat (≈3 s) before launching, and raise the per-attempt readiness budget to
6 s. Total worst case stays inside the existing `kInputSessionRecoveryDeadlineMs`. Do **not**
add attempts; add *time*, because the console, not the client, is the slow party.

**Verification test** — instrument `remote_play_orchestrator.py:3298` to log the elapsed time
from the fault to each attempt, force 10 faults, and require ≥9/10 recoveries.

---

## F-5 · HIGH · The AV-clock CSV does file I/O on the Takion receive thread — the one pinned to core 0

**Affected**
- `chiaki-ng-src/lib/src/orionavclock.c:132-165` — `fprintf` per frame, `fflush` per second, both
  inline
- `chiaki-ng-src/lib/src/takion.c:1706-1717` — the call site, inside `takion_handle_packet_av`,
  which runs on the Takion thread (`takion.c:1102` sets that thread's affinity)
- Enabled by `run_orion.local.ps1:687-688` — **the owner's dev session had this ON**

**Impact** — ~60 `fprintf` per second plus a synchronous `fflush` of ~6 KB every second on the
thread that receives every video and audio datagram *and* shares physical core 0 with
`CHIAKI_THREAD_NAME_TAKION_SEND`. `fflush` to `logs/diagnostics/` is a blocking write to the
same C: volume that the framedump harness is hammering with 1280×720 PNGs; that harness already
has a measured 250 ms single-write stall guard (`run_orion.local.ps1:679-681`), which is direct
evidence that writes on this box *do* stall for hundreds of milliseconds. A stalled Takion
receive thread is packet loss and receive jitter — and per F-1, jitter is a disconnect.

This also **contaminates the timing lane's own instrument**: the AV clock is being used to
measure onset jitter, and it perturbs the thread it measures.

**Reproduction** — `ORION_AVCLOCK=1`, play a session, correlate the once-per-second `fflush`
cadence against `orion_avclock_*.csv` inter-arrival outliers.

*Refuting evidence:* 64 KB of `setvbuf` buffering (`orionavclock.c:92`) means the steady-state
`fprintf` is a memcpy, and 6 KB/s to a warm page cache is usually sub-millisecond. The finding
stands on the *tail*, not the mean.

**Minimal fix** — hand the record to a single-producer ring and do the `fprintf`/`fflush` on a
dedicated low-priority writer thread. Failing that, at minimum drop the periodic `fflush`
(`orionavclock.c:147-163`) and rely on the 64 KB buffer plus the `fclose` in
`chiaki_orion_avclock_free`; a crashed session losing the tail of a diagnostic CSV is a far
smaller cost than a stalled receive thread.

**Verification test** — run two 10-minute sessions with `ORION_AVCLOCK` on and off and compare
the p99 of the fork's own frame inter-arrival and `queue_wait_us`. The instrument must not move
the p99.

---

## F-6 · MEDIUM · A diagnostic env var changes the AV-packet parse path and advances gkcrypt key state

**Affected**
- `chiaki-ng-src/lib/src/takion.c:1698-1721` — the restructured `takion_handle_packet_av`
- `chiaki-ng-src/lib/src/takion.c:1830` — `av_packet_parse` calls
  `chiaki_key_state_request_pos(key_state, key_pos_low, /*commit=*/true)`
- `remote_play_orchestrator.py` (`client_cfg`) — `disable_video=bool(self._cc_mode)`

**The change** — previously, with `CHIAKI_VIDEO_DISABLED`, a video packet was freed *before*
`av_packet_parse`. Now, when `takion->orion_avclock` is non-NULL, the packet is parsed first and
only then dropped. Parsing **commits** a key position into the shared `ChiakiKeyState`.

**Impact** — Orion runs capture-card sessions with `disable_video=True`, so this is not a corner
case: it is the production configuration. A debugging environment variable now alters which
packets advance the encryption key-position rollover tracker. I found no correctness defect —
feeding the tracker *more* of the same counter space should if anything improve rollover
detection — but a diagnostic must never be able to reach the crypto path, and this one does,
unreviewed and untested.

*Supporting:* the code path is unambiguous (`takion.c:1701` gates on `!takion->orion_avclock`).
*Refuting:* if `ChiakiKeyState` maintains separate per-stream positions this is inert. I did not
locate a `keystate.c` in the tree, so the implementation lives elsewhere and this should be
confirmed rather than assumed.

**Minimal fix** — record the arrival timestamp against the *unparsed* header instead. The frame
index is at a fixed offset in the AV header; a 6-line local decode in `orionavclock.c` removes
the need to call `av_packet_parse` at all in the video-disabled case, restoring the original
early-free.

**Verification test** — a unit case asserting that with `CHIAKI_VIDEO_DISABLED` set, the key
state after N video packets is identical whether or not the AV clock is enabled.

---

## F-7 · MEDIUM · `ORION_AVCLOCK_PATH` is unvalidated: unbounded truncating open, no containment, cross-session clobber

**Affected** — `chiaki-ng-src/lib/src/orionavclock.c:72-99`

| Sub-question | Finding |
|---|---|
| Traversal | **Not validated.** `getenv("ORION_AVCLOCK_PATH")` goes straight into `fopen(path, "wb")` (`:87`). No containment check, no extension check, no rejection of `..`, UNC paths or device names. `"wb"` **truncates**. Anything writable by the launcher's user can be zeroed — `settings.json`, `license_cache.enc`, `release_manifest.json`. Same-user privilege only, so not a privilege escalation, but it is an integrity primitive handed to anything that can set an environment variable on the child (and the child's environment is inherited from the launcher). |
| Truncation | **Yes, and across sessions.** The path is fixed for a whole launcher run (`run_orion.local.ps1:688` stamps it once at start-up), but a Takion is created **per Remote Play connection**. Every reconnect, court change and input-link recovery opens the *same* path with `"wb"` and **destroys the previous session's data**. The owner's session had 10 recoveries; at most the last survived. |
| Senkusha vs session double-open | **Correctly handled.** `takion.c:465-466` gates creation on `info->enable_crypt`, and `senkusha.c:155` sets `takion_info.enable_crypt = false`, so the preflight Takion never creates the clock. The guarding comment is accurate. |
| Two *processes* | **Not handled.** Input-link recovery launches a replacement OrionStream while the old one may still be closing. Both inherit the same `ORION_AVCLOCK_PATH` and both `fopen(..., "wb")` it. On Windows the second open succeeds (no deny-write share mode is requested), giving two independent `FILE*` with independent offsets writing interleaved garbage into one CSV. |
| Relative path | Falls back to a bare filename in the **current working directory** (`:76-85`), which for a child launched by the sidecar is not a defined location. |

**Impact** — Low security severity (same-user), medium *data* severity: the diagnostic that the
timing lane is basing conclusions on is silently destroyed by exactly the reconnect events the
lane is investigating.

**Minimal fix**
1. Require the path to be absolute, reject any component equal to `..`, and require a `.csv`
   suffix; log and disable otherwise.
2. Open with `"ab"` (append) and write the header only when the file is empty, so reconnects
   extend rather than destroy.
3. Append the process id and a monotonic stamp to the filename so two generations cannot collide.

**Verification test** — set `ORION_AVCLOCK_PATH` to a copy of a throwaway `settings.json`, start
and stop a session twice, and assert the file is a well-formed CSV containing **both** sessions.

---

## F-8 · MEDIUM · After a transport fault the input pipe server is gone for the life of the process

**Affected** — `chiaki-ng-src/gui/src/orioninputbridge.cpp:349-350`, `:110-121`

After `fail_transport`, `break` exits the **outer** loop, so `CreateNamedPipeA` is never called
again. The pipe is created with `nMaxInstances = 1` (`:114`). Two consequences:

1. The OrionStream process stays alive — `remote_play_client.py:1852-1856` documents that
   *"`Session has quit` often leaves the OrionStream GUI process alive in its internal retry
   loop"* — but with **no input pipe server**. The launcher's heartbeat correctly reports
   `connected=0`, so it is not fooled; but the process is a zombie that only a kill resolves.
2. If that zombie is *not* reaped (`_reap_failed_start` returning false is an explicitly handled
   case, `remote_play_client.py:1857-1861`) and it still held the name, a replacement
   OrionStream's `CreateNamedPipeA` fails with the single-instance error, logs
   `"OrionInputBridge: CreateNamedPipe failed"` and retries every 500 ms forever. This is an
   alternative explanation for the 3/3 recovery failure in F-4 and should be checked against the
   fork's session log before F-4's fix is chosen.

**Minimal fix** — on `transport_fault`, do not `break` the outer loop; reset `transport_fault`,
`owning_ = false` and re-enter `CreateNamedPipeA` so the bridge can serve the next client.
Combined with F-1's fix this removes the need to replace the process at all for a timeout.

**Verification test** — force a transport fault and assert a new `"OrionInputBridge: waiting for
Orion on"` line appears within 1 s and the launcher's `connected=` returns to 1 without a process
restart.

---

## F-9 · LOW · The AV-clock wall projection silently absorbs system-clock steps

`chiaki-ng-src/lib/src/orionavclock.c:36-58`. The monotonic→wall map is re-synced whenever the
arrival is ≥1 s past the last sync. An NTP step, a DST change or a manual clock change between
two syncs shifts every subsequent `wall_ms` with **no marker in the CSV**. `project_wall_us`
additionally clamps an underflow to `0` (`:57`), which writes `0.000` rather than an error.
The timing lane joins this column against detector wall-clock grids, so a step is a silent
correlation break.

The **monotonic** side is sound: `chiaki_time_now_monotonic_us` (`lib/src/time.c:11-28`) does not
wrap (64-bit QPC at 10 MHz is ~29,000 years) and never goes backwards. Note that the *old*
formula it replaces — `v.QuadPart *= 1000000` before dividing — **overflowed `int64` after
~10.7 days of system uptime** at a 10 MHz QPC, wrapping the monotonic clock negative. Astra's
change is a genuine latent-bug fix and should be kept.

**Minimal fix** — emit `sync_wall_ms` deltas and flag any resync where the wall delta differs
from the monotonic delta by more than a few ms; write a `step` marker row.

---

## F-10 · LOW · `SharedMemoryFrameReader::open()` does not close cached notification handles

`native_orion/src/SharedMemoryFrameReader.cpp:77-79` calls `closeMappingHandles()` only, while
`configureTransport` (`:71-75`) and `close()` (`:471-475`) call `closeNotificationHandles()`.
`readyEvent_` is a **named** kernel object opened lazily and cached by handle
(`:142-145`); if `open()` is ever reached after the transport names change without going through
`configureTransport`, the reader waits on the previous generation's event and falls back to the
250 ms safety timeout per frame.

**Not currently exploitable**: the only consumer, `SharedMemoryFramePump.cpp:236-237`, always does
`source_->close(); source_->configureTransport(...)` on reset, so the invariant holds today. This
is a latent API footgun, filed so a future caller does not step on it.

**Minimal fix** — make `open()` call `close()` instead of `closeMappingHandles()`.

---

## F-11 · LOW · `chiaki_takion_connect` leaks the AV-clock `FILE*` if the Takion thread fails to start

`chiaki-ng-src/lib/src/takion.c:464-472`. The clock is created, then
`err = chiaki_thread_create(...)` — and `err` is **discarded**; the function returns
`CHIAKI_ERR_SUCCESS` unconditionally. The ignored return is pre-existing upstream, but it now also
strands an open `FILE*` and a 64 KB buffer, and `chiaki_takion_close` will then join a thread that
was never created. **Minimal fix**: check `err`, `chiaki_orion_avclock_free` and `goto error_sock`.

---

## F-12 · INFORMATIONAL · Verified benign in the uncommitted diff

- **`videoreceiver.c:132-133`** — hoisting the `next_frame` computation above the adaptive-stream
  block is a **semantic no-op**. I read the intervening block (`:135-152`): it writes
  `profile_cur`, emits the header sample and parses the bitstream header, and touches neither
  `frame_index_cur` nor `frame_index_prev`. No decoder-wedge risk. Confirmed, no action.
- **Senkusha AV-clock containment** (`takion.c:465`, `senkusha.c:155`) — correct, see F-7.
- **Frame export** (`gui/src/orionframeexport.cpp`) — the writer's blocking `WriteFile` is
  correctly unblocked by the destructor's `DisconnectNamedPipe` under `mutex_` (`:101-108`),
  the pending slot is freed on disconnect (`:355-365` of the file) so a stale frame never
  crosses a reconnection, and `connected_` is re-checked under the same mutex after the GPU
  readback (`:176-182`). **Crucially, this path was inert in the owner's session**: the log shows
  `tier=capture_card` and the orchestrator sets `disable_video=True` in capture-card mode, so the
  decoder produced no frames and the frame-export pipe carried nothing. **The "frame export
  stalling → sidecar readiness failure" hypothesis in goal 4 is REFUTED for this session.**

---

## F-13 · Goal 3 — Can TRIANGLE + CIRCLE (or any chord) reach a chiaki quit/menu/stream-control shortcut?

**NO.** Stated with high confidence.

- The **only** controller-button combo in the fork is the upstream dpad/touchpad-emulation
  toggle, evaluated at **`chiaki-ng-src/gui/src/streamsession.cpp:1346`** (SDL path) and
  **`chiaki-ng-src/gui/src/streamsession.cpp:1428`** (Setsu path). It sets `dpad_regular = !dpad_regular`
  and nothing else — it cannot stop, quit, disconnect or open a menu.
- Its default binding is **L1 + R1 + D-pad Up**, not TRIANGLE+CIRCLE:
  `gui/src/settings.cpp:1051, 1058, 1065, 1072` default to `9, 10, 7, 0`, mapped through
  `streamsession.cpp:382-393` as `1 << (n-1)` → `1<<8` (L1), `1<<9` (R1), `1<<6` (D-pad Up),
  unset — against `lib/include/chiaki/controller.h:17-32`, where TRIANGLE is `1<<3` and
  CIRCLE is `1<<1`.
- The only `chiaki_session_stop` in the GUI is `StreamSession::Stop()` at
  **`gui/src/streamsession.cpp:902-905`**, and its callers are the worker teardown (`:96`, `:152`,
  `:2016`) — never controller state.
- The session's own quit event (`:2458-2465`) is *reported* to the GUI, not raised by it.
- The second `chiaki_session_stop` in the process is **`gui/src/orioninputbridge.cpp:164`** —
  F-1. That is the one that fired.

So the owner's observation is real but the mechanism is not a hotkey: TRIANGLE+CIRCLE produced
two pure button releases inside one echo window, and the *delivery machinery* killed the session.

---

## F-14 · Goal 1 — How the launcher detects OrionStream exit, and what it does

Documented for completeness; no defect beyond F-8.

- **Direct process supervision: none.** `RemotePlaySession` owns the *Python sidecar* QProcess
  (`RemotePlaySession.cpp:2537-2700`), not OrionStream. OrionStream is launched and reaped by
  `remote_play_client.py` inside the sidecar.
- **The real liveness signal is the input pipe.** `OrionInputClient`'s heartbeat
  (`connected=/writes=/ack_seq=`) is what flips first — 1 ms before anything else in the log.
- **On pipe loss** the launcher: revokes the scheduled latency-route attestation
  (`OrionAppController.cpp:12971-12983`, the "Controller route changed before automation process"
  line — this is a *consequence*, not a cause, answering goal 4), fails the release-repair
  duplicate (`:13714`), and isolates the route to ViGEm/XUSB. The bot is disarmed, not left firing
  into a dead pipe. **This part is correct and fails closed.**
- **Re-arm is gated on neutral physical shot controls** (`:13931`, "forced write accepted after
  physical shot controls were neutral"). This is the mechanism that ties into owner bug (2): with
  R2 held at `255`, the re-seed waits, and the console keeps the last state it was told.
- **Can the launcher believe a stream is live when OrionStream died?** In the *opposite*
  direction, yes and it is the observed case: OrionStream's process stays alive while its PS5
  session is dead (`remote_play_client.py:1852-1856`), and in capture-card mode the HDMI preview
  keeps flowing, so the UI shows "Running" with live video while zero input reaches the console —
  for 7 seconds at `03:11:12`–`03:11:19`. The launcher does detect it (via the pipe, within 1 ms)
  and does say so ("Remote Play input readiness was lost", `03:11:12.672`). The UI honesty is
  adequate; the recovery (F-4) is what fails.
- **Handle/thread leaks:** none found in `RemotePlaySession`. The sidecar QProcess leak on
  unsolicited exit was already fixed (`:2671-2673`), the Win32 job handle is retired per
  generation (`:2555`), and the destructor deliberately uses `delete` rather than `deleteLater`
  with handlers disconnected first (`:704-721`). `SharedMemoryFrameReader` closes mapping, mutex,
  view and both events (`:453-475`) — see F-10 for the one latent gap.
- **Executable policy** (`RemotePlayExecutablePolicy.h`) is sound: production accepts only the
  image below the running executable, containment survives junction/symlink resolution
  (`:119-145`), and the identity hash rejects size/mtime/canonical-path movement around the read
  (`:34-80`). No finding.

---

## Verdict

**BLOCKED** for release until F-1 and F-2 are fixed.

- F-1 is a customer-visible disconnect caused by a 25 ms budget that the same tree's own 40 ms
  echo can exceed, and the response to exceeding it is to stop the console session.
- F-2 lets an optional reliability echo silently retire the input thread.
- F-3 must not ship to hybrid-CPU customers without an efficiency-class check, and must log its
  result either way — without that line, every future latency report from the field is
  uninterpretable.
- F-4 turns a recoverable fault into a hard stop and should be fixed in the same pass.
- The uncommitted fork changes are **not release-ready as a set**; F-5/F-6/F-7 are diagnostic
  scaffolding (`ORION_AVCLOCK`) that reaches the network thread, the crypto parse path and the
  filesystem, and must be gated out of the packaged build entirely rather than merely defaulted
  off.
