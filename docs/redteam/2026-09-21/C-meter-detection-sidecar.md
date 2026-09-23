# Surface C — meter detection + sidecar → native shot gate

Internal red-team report, 2026-09-21 owner test session (`logs/orion_native.log`,
2026-09-22T03:05Z–03:27Z = 22:05–22:27 local). Source read-only; no builds, no launches, no commits.
Python-only pytest was run (`-p no:cacheprovider`, `--basetemp` under this agent's scratch dir).

**Headline answer to goal 1, stated up front: the sidecar's "Remote Play input session failed
readiness" is a CONSEQUENCE, never a cause.** It is emitted 0.1–2.3 s *after* the pipe has already
closed, by a relaunch the native itself ordered, and the relaunched Chiaki child is refused by the
PS5 with `Remote is already in use`. I found the actual initiator in the fork's own session logs and
it is neither detection nor readiness — see F1. Confidence: **very high** (direct, timestamped,
byte-level evidence from three independent logs).

Findings are ranked most severe first. Severities are for the whole product, not for my surface.

---

## F1 — BLOCKER — ROOT CAUSE OF OWNER BUG (1): a 25 ms MustDeliver ACK timeout on a redundant button-release echo kills the whole Remote Play session

**This is the root cause of "TRIANGLE + CIRCLE together makes Venice disconnect". Confidence: very
high.** It is owned by the fork (Astra / input lane), not by my surface, but I am reporting it here
because it is the answer to my goal-1 question and because nobody can fix the readiness loop without it.

**Failure path**

`OrionInputBridge::run()` waits synchronously for local UDP acceptance of every *flagged*
(MUST_DELIVER / SHOT_RELEASE) packet, with a hard 25 ms budget. On timeout it calls
`fail_transport(..., "local_delivery_timeout_or_mismatch")`, which calls `chiaki_session_stop()` —
it tears down the entire Remote Play session, not just the packet.

- `chiaki-ng-src/gui/src/orioninputbridge.cpp:20` — `static constexpr uint64_t ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25;`
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:279-292` — `chiaki_session_wait_orion_delivery(...)` →
  on `delivery_err != SUCCESS || status != expected` → `fail_transport(..., "local_delivery_timeout_or_mismatch")`
- `chiaki-ng-src/gui/src/orioninputbridge.cpp:150-165` — `fail_transport` = log + `stop_.store(true)` +
  `chiaki_session_stop(session_)` (`action=stop_session`)

**Evidence — the owner's exact button combination, reconstructed from the fork's own session log**

`%APPDATA%/Chiaki/Chiaki/log/chiaki_session_2026-09-21_22-23-00-313313.log:427-460`

```
22:23:27.775  pipe_received source_seq=30941 flags=0x01 btn=10 own=1     <- 0x0A = MOON(1<<1)|PYRAMID(1<<3) = CIRCLE + TRIANGLE down
22:23:27.776  ack_local_delivery source_seq=30941 stage=2 queue_wait_us=178
22:23:27.937  pipe_received source_seq=30942 flags=0x01 btn=8            <- circle up (triangle still held)
22:23:27.937  edge_enqueued  source_seq=30942 depth=3 redundant=1 redundant_history=1
22:23:27.942  redundant_history_dequeued source_seq=30942                <- 3 ms twin
22:23:27.980  redundant_history_dequeued source_seq=30942                <- 40 ms echo
22:23:27.962  pipe_received source_seq=30943 flags=0x01 btn=0            <- triangle up
22:23:27.963  edge_enqueued  source_seq=30943 depth=4 redundant=1 redundant_history=1
22:23:27.980  ack_local_delivery source_seq=30943 stage=2 queue_wait_us=17118   <- 17 ms, already 68% of budget
22:23:27.984  redundant_history_dequeued source_seq=30943
22:23:27.986  pipe_received source_seq=30944 flags=0x01 btn=0            <- the release echo
22:23:27.986  edge_enqueued  source_seq=30944 depth=2
22:23:28.012  [E] fatal_local_delivery_fault source_seq=30944 error=Timeout
              reason=local_delivery_timeout_or_mismatch queue_abort=Success action=stop_session
22:23:28.012  StreamConnection is disconnecting … Session has quit
```

`logs/orion_native.log:24726` — `2026-09-22T03:23:28.013Z Input release-repair duplicate FAILED
(result=5): direct pipe closed`. The native sees the pipe close **1 ms after** the fork stopped the
session. The pipe close is the *effect*.

Census of all retained fork session logs for this session: **3 of 3 session deaths were
`fatal_local_delivery_fault → stop_session`**; 2 of 3 were `local_delivery_timeout_or_mismatch`
(22:22:52.661 seq=30905, 22:23:28.012 seq=30944), the third was the normal
`owner_pipe_disconnected` at shutdown (22:26:55.585).

**Why two buttons matter.** `is_pure_button_release` gives every pure-button release a 3 ms twin
plus a 40 ms echo (fork release-redundancy, 09-21). One two-button press/release generates 1 down
edge + 2 up edges, each of which fans out to 3 flagged packets — up to ~7 MustDeliver transactions
inside ~50 ms, all serialised through one feedback-sender tick queue. Observed `queue_wait_us`
climbs 178 → 15718 → 17118 → **timeout**. Triggers (L2/R2) are analog fields and are excluded from
`is_pure_button_release`, which is why the owner sees this with face buttons and not with R2.

**Impact.** BLOCKER. A perfectly healthy local pipe hiccup of >25 ms terminates the Remote Play
session, which cascades into: route fallback to ViGEm, 3 failed relaunches (F2), two full sidecar
restarts, loss of the latency authority (F6), loss of ~3 s of input each time, and a stuck R2 at the
console if the trigger was held (owner bug 2). Ten such events in a 22-minute session.

**Reproduction.** With the bot connected via the fork's direct pipe, press and release TRIANGLE +
CIRCLE together (or any two face buttons) several times in quick succession while the engine is
also sending its own edges. Watch the newest `%APPDATA%/Chiaki/Chiaki/log/chiaki_session_*.log`
for `fatal_local_delivery_fault … reason=local_delivery_timeout_or_mismatch`.

**Hypothesis — supporting evidence.** The 3 timestamped fork logs above; `queue_wait_us` already at
15–17 ms on the surviving packets; `depth=4` at the moment of the burst; the native heartbeat at
`logs/orion_native.log:22211` showing `writes=8961` against `ack_seq=8939` (22 unacked writes in
<1 s at a nominally 1 Hz idle rate) immediately before the first incident.

**Hypothesis — refuting evidence I looked for and did NOT find.** (a) No `enqueue_failed`,
`enqueue_ack_write_failed` or `delivery_ack_write_failed` anywhere, so it is not a pipe-write
fault. (b) `owner_pipe_disconnected` appears exactly once, at clean shutdown — so the native did
**not** close the pipe first. (c) No network/Takion error precedes the fault. (d) The failing packet
at 22:23:28 had `depth=2`, not 4 — so raw queue depth alone is not the discriminator; the total
flagged-packet *rate* over the preceding ~200 ms is.

**Minimal fix (fork; for Astra).** A local-delivery timeout must not be fatal.
1. In `orioninputbridge.cpp:279-292`, treat `CHIAKI_ERR_TIMEOUT` as *non-fatal*: write the ACK with
   a `TimedOut` stage and continue the read loop. Reserve `fail_transport` for genuine
   mismatch/enqueue/ack-write errors. Only an *unbroken run* of N consecutive timeouts (N ≥ 5)
   should escalate.
2. Raise `ORION_LOCAL_DELIVERY_TIMEOUT_MS` to at least 3× the feedback-sender tick (≥ 60 ms) —
   17 ms of legitimate queue wait was already observed on a *successful* packet.
3. Do not emit the 3 ms twin / 40 ms echo for a release whose own edge has not yet reached
   `local_delivery_complete` (coalesce the redundancy behind the primary), so a multi-button burst
   cannot triple its own queue.

**Verification test.** Fork unit test over `OrionInputBridge`'s flagged-packet path: inject a
synthetic `CHIAKI_ERR_TIMEOUT` from `chiaki_session_wait_orion_delivery` and assert
`chiaki_session_stop` is NOT called and the read loop continues; assert it IS called after the Nth
consecutive timeout. Live: repeat the two-button repro for 5 minutes and assert zero
`fatal_local_delivery_fault … Timeout` lines and zero `Controller route isolation: direct pipe
unavailable` lines in `logs/orion_native.log`.

---

## F2 — HIGH — readiness failure is a pure consequence, and the recovery it triggers is self-amplifying

**Direction, proven.** Message flow, both ways, with citations:

*Downstream (what actually happened — 4 independent hops, all after the fact):*

| t (UTC) | log:line | event |
|---|---|---|
| 03:11:12.551 | `logs/orion_native.log:22213` | `Input release-repair duplicate FAILED (result=0): direct pipe closed` |
| 03:11:12.552 | `:22215` | `Controller route isolation: direct pipe unavailable; ViGEm/XUSB fallback carries PS5 input` |
| 03:11:12.672 | `:22225` | native: `Remote Play input readiness was lost; attempting one bounded in-place input recovery` |
| 03:11:12.723 | `:22227` | native: `Recovering Chiaki input link in place` |
| 03:11:14.827 | `:22231` | **sidecar: `Remote Play input session failed readiness`** (attempt 1) |
| 03:11:17.045 | `:22236` | sidecar: same (attempt 2) |
| 03:11:19.645 | `:22244` | sidecar: same (attempt 3) |
| 03:11:19.646 | `:22245` | sidecar: `Input-link recovery exhausted after 3 attempt(s)` |

The readiness warning therefore trails the pipe close by 2.276 s and trails the *recovery order* by
2.104 s. It cannot be its cause.

**And the reason the relaunch fails is on the console, not in Orion:**
`%APPDATA%/Chiaki/Chiaki/log/chiaki_session_2026-09-21_22-22-55-669669.log` (the whole 959-byte file):

```
22:22:56.589 [E] Reported Application Reason: 0x80108b10 (Remote is already in use)
22:22:56.589 [I] Session has quit
```

That "Session has quit" is one of `_CHIAKI_SESSION_END_MARKERS`
(`remote_play_client.py:95-98`), so `_SessionReadinessTracker._poll_locked` returns `"ended"`
(`remote_play_client.py:243`), `is_session_ready()` returns False (`remote_play_client.py:1595-1605`),
and `ensure_running` renders exactly the observed string — *"the current Chiaki session ended before
becoming ready"* (`remote_play_client.py:1824-1827`). The PS5 simply has not yet released the
session the fork killed in F1.

*Upstream (the path that DOES exist, and is the amplifier):* the sidecar's readiness verdict **is**
able to make the native revoke the route — but only after the route is already gone.

- `native_orion/backend/autogreen_sidecar.py:2601` — every telemetry payload carries
  `"input_ready": bool(input_ready_fn and input_ready_fn())`, i.e. a live
  `RemotePlayOrchestrator.input_link_ready()` poll (250 ms cache,
  `remote_play_orchestrator.py:2833-2879`).
- `native_orion/src/RemotePlaySession.cpp:4210-4216` — on every telemetry line, if
  `shouldAutoRecoverInputFromTelemetry(...)` → `recoverInputLink()`.
- `native_orion/src/SidecarWatchdog.h:161-167` — the predicate is
  `running && !recoveryPending && (!hasExplicitInputReady || !inputReady)`. **Missing evidence is
  treated identically to an explicit false.**
- `native_orion/src/RemotePlaySession.cpp:3690-3740` → `sendSidecarCommand({{"cmd","recover_input"}})`
- `native_orion/backend/autogreen_sidecar.py:1161-1188` → `orch.recover_input_link()`
- `remote_play_orchestrator.py:3236-3322` — tears down the *existing* manager
  (`manager.stop()`, line 3255) and relaunches up to 3× against a 17 s deadline.

**The amplification.** `recover_input_link` unconditionally destroys the current Chiaki client
before it knows whether a replacement can be created. Each failed relaunch leaves the console
holding a session for several more seconds, so attempts 2 and 3 fail for the same reason. The
session record: **10 pipe-closed events, 8 recoveries, 2 escalations to a full contained sidecar
restart** (`logs/sidecar_crash.log`: `exit 2026-09-21 22:11:22 reason=shutdown-cmd`,
`exit 22:13:47`; `logs/sidecar_fault.log`: `sidecar start 22:11:25`, `22:13:50`). That is the
22:22-onward "every 10–15 s" cadence the owner saw.

**Impact.** HIGH. A transient 25 ms fault (F1) is escalated by this machinery into repeated whole-
session teardowns, two sidecar restarts, and ~24 s of lost input per incident.

**Reproduction.** Trigger F1 once. Observe `Recovering Chiaki input link in place` followed by 1–3
`Remote Play input session failed readiness` lines and, if all 3 fail, a full sidecar restart.

**Supporting evidence.** The table above; `INPUT LINK LOST: reason=session_ended pid=5076
exit_code=None` (`logs/orion_native.log:22614`) — `exit_code=None` proves the Chiaki *process* was
still alive when the *session* was declared dead, which is exactly the `chiaki_session_stop()`
signature of F1, not a crash.

**Refuting evidence I looked for.** I hypothesised that the readiness tracker was latching onto the
*previous, dying* child's log (its mtime is newest and its shutdown bytes push it past the launch
baseline — `remote_play_client.py:166-176`, `_select_current_log`). **Refuted:** the baseline is
re-snapshotted immediately before every launch (`remote_play_client.py:1687-1688`) *and* inside the
standby-promotion path (`remote_play_client.py:2455-2457`), and the retained 959-byte log shows the
*new* child's own genuine `Remote is already in use`. I also hypothesised the standby prewarm pool
could inject a competing log; **refuted** for the same reason — a standby opens no session and
writes no `chiaki_session_*.log`.

**Minimal fix (sidecar).**
1. `remote_play_orchestrator.py:3244-3256` — do **not** stop the existing manager when the failure
   reason is a pipe/route fault and `manager.is_session_ready()` is still true. Probe first;
   only replace a child that has genuinely lost its session.
2. `native_orion/src/SidecarWatchdog.h:161-167` — require the false verdict to be *explicit and
   twice consecutive* (`hasExplicitInputReady && !inputReady` on two successive telemetry payloads)
   before ordering a recovery. Today a single dropped/short payload orders a session teardown.
3. `remote_play_orchestrator.py:3268-3300` — insert a `Remote is already in use` backoff: when the
   readiness reason is `session_ended` *and* the attempt lasted < 1 s, wait ≥ 4 s before the next
   attempt instead of the current 0.75 s/1.25 s plan (the observed console release took ~5 s;
   attempt 2 at +4.9 s succeeded, attempt 1 at +2.2 s did not).

**Verification test.** `tests/test_readiness_incident.py` — add: a manager whose tracker reports
`ready` must not be stopped by `recover_input_link`; and a `Remote is already in use` /
`Session has quit` fixture log must produce reason `session_ended` with the new backoff applied.
`tests/test_remote_play_client_lifecycle.py` covers the launch path. Both suites pass today
(140 passed, see "Test baseline" below), so any change must keep them green.

---

## F3 — HIGH — in No-Meter / Input-Timed modes the sidecar↔native epoch binding is switched OFF, so a ghost meter can own a press

**Failure path.** The single strongest defence against a false lock or a previous shot's feedback
meter taking ownership is the epoch join: the reader stamps `gameplay_structure_epoch = N` and the
engine requires it to equal `physicalShotEpoch_`. That requirement is gated:

- `native_orion/src/AutomationEngine.cpp:6271-6272` —
  `strictOwnershipProof = latencyCalibrationAutomatic_ || autonomousLiveMeterTimingEnabled();`
- `native_orion/src/AutomationEngine.cpp:14330-14333` —
  `autonomousLiveMeterTimingEnabled() = config_.autonomousVision && !config_.noMeterEnabled && !config_.inputTimedEnabled;`
- `native_orion/src/AutomationEngine.cpp:14349-14355` — `noMeterVisionAssistActive()` **requires**
  `config_.inputTimedEnabled`.

So in the shipped No-Meter Vision Assist mode, `strictOwnershipProof == false` by construction, and
in `recordPendingMeterOwnershipSample` that disables, in one step:

- `:6287-6288` `physicalEpochCurrent` becomes unconditionally true;
- `:6326-6328` + `:6389-6390` `currentGameplayEpochProof` is no longer required;
- `:6302-6303` the proof switches from the canonical `coarseFillPct` to the raw `fillPct`;
- `:6322-6325` `canonicalCoarseValid`, `usableIdentity` and `canonicalTimingIdentity` are all dropped;
- `:6280-6282` the pending window widens from `buttonNoMeterAbortMs` to `squareHoldArmMs`.

**Impact.** HIGH. In exactly the mode built so vision "owns any release it can actually see", the
*only* remaining guard against the previous shot's feedback meter bridging the press is the
sidecar's own ghost breaker (`simple_meter_reader.py:2022-2060`) — a heuristic, not a proof. A ghost
that survives the breaker can arm and release a real press at the wrong instant, which presents to
the owner as a random early/late shot with no diagnostic.

**Reproduction.** Enable Input-Timed + No-Meter Vision Assist, take two shots ~1 s apart so the
first shot's feedback meter is still rendered at the second press, and set
`ORION_OWNERSHIP_TRACE=1`. The `OWNERSHIP INPUT` line (`AutomationEngine.cpp:6363-6377`) will show
`structure_ok=0 genuine=1 eligible=1` — i.e. owned without the stamp.

**Supporting evidence.** The four code sites above; the design note at
`simple_meter_reader.py:4588-4590` states the requirement the engine is skipping
("publish `gameplay_structure_verified` with `gameplay_structure_epoch == N` … BEFORE fill crosses
~40%"); the ghost-meter incident at `logs/orion_native.log:22989` shows a 46.8 % leftover meter
still being locked 1.09 s into a press.

**Refuting evidence.** In this session the mode was the *meter* path, so `strictOwnershipProof` was
true and every ownership sample was epoch-checked — all 21 `SHOT NOT OWNED` lines report
`stamp_epoch_seen=0` with `unstamped=0`, meaning nothing was proposed at all, not that something
unstamped was accepted. I did **not** observe this hole being exercised live; it is a code-path
finding. It also requires a rendered ghost to survive the sidecar breaker.

**Minimal fix.** Make the epoch join unconditional. In `AutomationEngine.cpp:6271-6272`:
`strictOwnershipProof = latencyCalibrationAutomatic_ || autonomousLiveMeterTimingEnabled() ||
noMeterVisionAssistActive();` — the vision-assist release is the meter release (the code says so at
`:14392-14396`), so it must carry the meter release's proof. Same edit at `:3072` and `:6198`.

**Verification test.** New `tests/native` (or the existing AutomationEngine gtest harness) case:
with `inputTimedEnabled=1, noMeterVisionAssist=1, autonomousVision=1`, feed a `DetectionResult`
whose `gameplayStructureEpoch == physicalShotEpoch - 1` and assert
`recordPendingMeterOwnershipSample` returns false.

---

## F4 — MEDIUM — a pipe/route fault revokes the *meter timing* authority, so detection is collaterally disarmed by an input-lane fault

**Failure path.** The controller-route transition (F1/F2) re-keys the latency route in the sidecar,
and the subsequent sidecar restart re-negotiates the capture mode, which resets the latency
authority. Until the controller-route attestation is re-earned, `measuredLeadAuthoritative()` is
false and the engine will not fire on a measured lead.

- `logs/orion_native.log:22219` — `Latency route re-keyed (controller_delivery_route_transition);
  restored authority awaits fresh health` (t = 03:11:12.572, 21 ms after the pipe closed)
- `logs/orion_native.log:22327` — `Latency authority reset (capture_negotiated_mode_attested);
  fresh route evidence required` (03:11:36.421, after sidecar restart #1)
- `logs/orion_native.log:22698` — same at 03:14:00.891 (after restart #2)
- `remote_play_orchestrator.py:2879-2920` `_rekey_latency_route`; `:4989-4999`
  `_revoke_restored_latency_on_source_transition`
- `native_orion/src/AutomationEngine.cpp:14400-14415` — `exactControllerRouteEcho` requires a
  non-zero attestation generation; the reset zeroes it (the deadlock the comment at `:14412-14420`
  documents for 2026-08-06).

**Impact.** MEDIUM. Every input-pipe hiccup costs the meter path its measured-lead authority, on top
of the input loss. Twice in 22 minutes here. The owner experiences it as "the bot went dumb after
the disconnect", with no message saying so.

**Reproduction.** Trigger F1; watch for the two lines above and for `lead_ready=0` on any
`SHOT NOT OWNED` line in the following seconds.

**Supporting evidence.** Three cited log lines plus the documented deadlock comment.
**Refuting evidence.** It *does* recover: `SHOT NOT OWNED` at `logs/orion_native.log:25xxx`
(03:24:30 onward) reports `lead_ready=1`, so the reset is transient, not permanent. Severity is
therefore MEDIUM, not HIGH.

**Minimal fix.** Distinguish *route* transitions from *source* transitions. A controller-delivery
route change (pipe → ViGEm → pipe) does not change the video source and must not invalidate the
capture-side posterior; only re-key the controller-route attestation and keep the frame-clock
authority. `remote_play_orchestrator.py:2879-2920`: split `_rekey_latency_route` so
`controller_delivery_route_transition` no longer flows into
`_revoke_restored_latency_on_source_transition`.

**Verification test.** `tests/test_orchestrator_capture.py` / a new
`tests/test_latency_route_rekey.py`: assert a `controller_delivery_route_transition` preserves
`latency_estimator_scope_epoch()` and the restored posterior, while a
`decoder_or_capture_source_generation_transition` still clears it.

---

## F5 — MEDIUM — production default writes a per-shot play corpus to a hard-coded `D:\` path

**Failure path.** `shot_records.py:220-223` — `ORION_SHOT_RECORDS` defaults to `'1'` (ON).
`shot_records.py:234-241` — when `ORION_SHOT_RECORDS_ROOT` is unset the root is
`os.path.join('D:\\', 'NexusVision', 'shot_records')`. Nothing under `native_orion/src/` or
`scripts/` ever sets either variable (grepped), so the shipped launcher inherits the default.

**Evidence it is live:** `D:\NexusVision\shot_records\` contains `session_20260921_220557.jsonl`
(20,924 B), `session_20260921_221126.jsonl` (3,601 B), `session_20260921_221350.jsonl` (44,692 B) —
three files for *this one* session, split by the two sidecar restarts of F2.

**Impact.** MEDIUM. (a) On a customer machine that has a D: drive, Venice silently writes a growing
JSONL record of every shot (types, timings, network RTT, banner verdicts) outside the app's own
data directory, with no retention policy and no user-visible disclosure — a blue-team/privacy item.
(b) On a machine without D:, `os.makedirs` raises, the exception is swallowed
(`shot_records.py:256-258`) and the diagnostic is silently off — which is *safe* but means the
corpus the anchor model is fitted on is machine-dependent. (c) The restart-driven file split means
no single file spans a session, so any offline analysis silently loses cross-restart continuity.

**Reproduction.** Launch the packaged app on a machine with a D: drive, take one shot, and look for
`D:\NexusVision\shot_records\session_*.jsonl`.

**Supporting evidence.** The three files above plus the code paths.
**Refuting evidence.** `shot_records.py:226-232` already disables the recorder under pytest, and
`create()` never raises — so this is a default/packaging defect, not a crash risk. I did not find
any secret or credential in the record schema.

**Minimal fix.** `shot_records.py:234-241`: default the root to
`%LOCALAPPDATA%\NexusVision\Orion Native\shot_records` and default `ORION_SHOT_RECORDS` to `'0'`
unless `ORION_DEV_TOOLS`/a dev marker is set; keep `D:\` only behind an explicit
`ORION_SHOT_RECORDS_ROOT`. Add the directory to the release filter policy so it can never be packaged.

**Verification test.** `tests/test_shot_records.py` (extend): assert
`ShotRecorder.create(env={})` returns `None` in a production-shaped env, and that when enabled the
path resolves under `%LOCALAPPDATA%` and never under a bare drive letter. Add a
`scripts/verify_orion.ps1 -StrictSecurity` assertion that no shipped Python contains an absolute
`D:\` or `C:\Users\` path.

---

## F6 — MEDIUM — the CV self-arm can hold the reader's relaxed-acquire gate open indefinitely with no physical press

**Failure path.** `remote_play_orchestrator.py:7874-7882` — any detection whose `rise_state ==
'rising'` **or** whose `fill_velocity_pct_s > 40.0` calls `_refresh_cv_shot_gate(_snap_seq)`.
`remote_play_orchestrator.py:2519-2528` then writes a *full new* window:
`_shot_gate_deadline_seq = frame_seq + 2400` and `_shot_gate_deadline_monotonic = now + 20.0`
(constants at `:1663` and `:1667`). There is no cap on consecutive self-arms and no requirement that
a physical press ever occurred.

While that merged gate is armed, `_push_reader_shot_gate` (`:2530-2556`) hands the reader
`set_shot_state(armed=True, ...)`, which relaxes its early-rise acquire gate and extends its coast —
i.e. it makes the reader *more* willing to lock, which makes the next false lock more likely, which
re-arms the gate. It is a positive feedback loop with no physical anchor.

**Impact.** MEDIUM for detection quality, LOW for safety. This is the mechanism behind the known
"65 idle false locks / static_zone withheld 200 reads" pattern: idle false locks keep the reader in
shot mode, the static-zone quarantine then starts withholding *real* reads in the same 96×192 px
cells (`simple_meter_reader.py:2206-2213`, `:14141-14175`).

**Reproduction.** Sit in the park with no press for 60 s in front of a bright vertical object
(sideline, white jersey stripe) and watch `SHOT-GATE ARMED` / `DISARMED` transitions
(`remote_play_orchestrator.py:2540-2546`) with `src=cv` and no `Physical shot epoch` line nearby.

**Supporting evidence.** The three code sites; the design comment at `:2521-2523` explicitly notes
"a CV-armed window must not open colour training — a false lock training itself was the failure mode
this kills", i.e. the risk is acknowledged for the *hardware* gate but not for the merged one.

**Refuting evidence — important, this cannot fire the bot.** `_refresh_cv_shot_gate` deliberately
never touches `_shot_gate_hw_deadline_seq`, so `_shot_armed_hw` stays false; the box latch
(`simple_meter_reader.py:7176-7181`), the colour-training gate and the static-rank publish
(`:7408-7418`) all require `_shot_armed_hw`. Native ownership additionally requires a non-zero
`physicalShotEpoch` (`AutomationEngine.cpp:6287-6288`). In this session `staticq=0/0` and
`press_fresh_withheld=0` on every DETECTOR HEALTH line, so nothing was actually withheld — consistent
with "false locks cost real meters, not fires."

**Minimal fix.** `remote_play_orchestrator.py:2519-2528`: cap the CV self-arm. Refuse to *extend*
the merged window past `N` consecutive CV-only refreshes without an intervening hardware arm (e.g.
allow at most 3 s of CV-only arm, then require a `Physical shot epoch`), and never let a CV refresh
push the monotonic deadline more than `_shot_gate_max_seconds` beyond the *first* CV arm of the run.

**Verification test.** `tests/test_shot_gate_atomicity.py` (extend): drive 200 CV-only refreshes with
no `arm_shot_gate` and assert `_shot_gate_state()` reports disarmed after the cap; assert
`_shot_gate_hw_deadline_seq` never advances.

---

## F7 — LOW — the dark-frame fail-closed path is correct; here is the audit that proves it (no wedge found)

Goal 2 asked whether the fail-closed feed can wedge the shot gate "armed but blind" or strand the
ownership handshake. **I could not construct such a path.** Recording the audit because a negative
result is load-bearing for the release decision.

*What stops.* `remote_play_orchestrator.py:5715-5718` classifies a dark grab as
`reject_reason='dark_frame'`; `:5752` calls `_note_frame_reject(..., fail_closed=True)`
(`_frame_reject_fails_closed`, `:4873-4882`, only exempts `source_poll_repeat`/`pixel_duplicate`).
`_note_frame_reject` (`:4817-4870`) then, under one lock: bumps the integrity generation, sets
`_capture_integrity_healthy=False`, calls `_clear_actionable_state_locked()`, and republishes an
immutable `_ProcessedFrameSnapshot` that **preserves the old frame identity but carries
`integrity_healthy=False`** and no actionable state (`_ProcessedFrameSnapshot` defaults at
`:411-451` are all fail-closed: `meter_present=False`, `raw_fed=False`,
`gameplay_structure_verified=False`, `gameplay_structure_epoch=0`).

*How it reaches native.* `native_orion/backend/autogreen_sidecar.py:2664-2667` →
`feed_healthy = integrity_healthy && !backend_frozen`;
`native_orion/src/RemotePlaySession.cpp:4629` parses it and `:4658` ANDs it into `sidecarFresh`,
so `sidecarResult.detected=false` and `sidecarResult.staleFrame=true`.
`AutomationEngine.cpp:6320-6325` requires `!result.staleFrame` for `genuineCurrent`. Fail-closed.

*How it recovers.* `_finish_processed_frame` (`:4884-4962`) restores health only when
`success && !backend_frozen && frame_generation == current_generation` — i.e. exactly one clean
detector completion whose captured generation is still current. A late completion racing a newer
reject publishes unhealthy metadata and returns without inventing a second rejection (`:4953-4962`).
I found no path that leaves `_capture_integrity_healthy` false after a good frame completes.

*No armed-but-blind wedge.* The gate has **two** independent caps — frame-sequence and wall clock
(`remote_play_orchestrator.py:2558-2573`) — and the comment at `:2564-2567` states why: the frame
sequence freezes on dark frames, so a sequence-only arm would never expire. Wall time is
authoritative. Observed live: the dark runs at `logs/orion_native.log:21502-21551` (count 60→420,
03:06:45–03:07:06) and `:22395-22467` (03:11:59–03:12:30) both ended with a normal
`SHOT-GATE DISARMED` and no stuck arm.

*No stranded handshake.* When the reader proposes nothing, the native does not wait forever: it
emits `METER VISION WAIT … blind_release_suppressed=1` and then `SHOT NOT OWNED …
output=pass_through` (`logs/orion_native.log:22987`, `:22996`) and hands the press back to the
player. 21 such presses this session, 0 hangs.

*No stale-evidence ownership.* `gameplay_structure_epoch` is latched to its captured epoch, never
the mutable current one (`simple_meter_reader.py:4670-4681`), rides the same atomic snapshot as the
frame identity (`remote_play_orchestrator.py:4704-4713`), and is compared against
`physicalShotEpoch_` at 8 separate native sites (`AutomationEngine.cpp:3089, 3745, 4072, 5590, 6328,
6417, 19609`, `MeterOverlayPolicy.h:141, 217`). The one hole is **F3** (that comparison is switched
off in No-Meter/Input-Timed mode).

**Residual LOW item.** `logs/orion_native.log:21502` shows `dark_frame count=60` and `:21505`
`count=180` 2 s later — a 120-frame jump, because the warning is throttled to `n==1 || n%60==0`
(`remote_play_orchestrator.py:4862-4863`) and the counter is cumulative and never reset. Reading the
log, "count=60" and "count=180" look like two separate 60-frame incidents; they are one continuous
7 s blackout. **Fix:** log the *current run length* (`self._black_run`) alongside the lifetime count.
**Verify:** `tests/test_frame_integrity_pipeline.py` (47 tests, currently green) — assert the
warning text carries a run length that resets on the first healthy frame.

---

## F8 — LOW — `BOX LATCHED … native_ownership=unconfirmed` is a hard-coded string, not a state

`simple_meter_reader.py:7190-7193`:

```python
_acq_logger.error(
    "BOX LATCHED: epoch=%d generation=%d box=[%d,%d,%d,%d] "
    "reader_geometry_latched=1 native_ownership=unconfirmed",
    _ep, _gen, *self._box_latch_box)
```

Both `reader_geometry_latched=1` and `native_ownership=unconfirmed` are literals inside the format
string. All **71** `BOX LATCHED` lines in `logs/orion_native.log` read `native_ownership=unconfirmed`
(`grep -o 'native_ownership=[a-z_]*' | sort | uniq -c` → `71 native_ownership=unconfirmed`), e.g.
`:25397`.

**Impact.** LOW, but it is a forensics trap: a reviewer joining these lines to `SHOT NOT OWNED`
would conclude "the reader latched 71 boxes and native owned none of them", which the field does not
say. It cost me a diagnostic detour and will cost the next reader one.

**Minimal fix.** Either print the real state (the reader does know `self._physical_shot_epoch` and
whether a structure proof was latched for it — `self._gameplay_structure_proof_epoch`, `:4676-4681`)
or delete the field. Suggested: `native_ownership=%s` with
`'stamped' if self._gameplay_structure_proof_epoch == _ep else 'unstamped'`.

**Verification test.** `tests/test_simple_reader_detfill_latch.py` (extend): latch a box with and
without a structure proof and assert the emitted line differs.

---

## F9 — LOW — the static-zone quarantine's only live trace is a counter inside a line the native truncates at 300 chars

`STATIC ZONE WITHHELD` is logged at **debug** level (`simple_meter_reader.py:14164-14169`), which
never leaves the sidecar. The only surviving signal is `staticq=<n>/<m>` inside the `DETECTOR HEALTH`
line, and `native_orion/src/RemotePlaySession.cpp:2953` and `:2997` truncate every relayed sidecar
line with `trimmed.left(300)`. The truncation is visible in the log: every `DETECTOR HEALTH` line
ends mid-word (`… fresh=46007 stale=0 see`). The reader's own comment
(`simple_meter_reader.py:2145-2153`) documents that this trim already cost a day of diagnosis once,
and the counters were moved to the front as the mitigation — so `staticq=` does survive today, but
the fields after ~column 300 (`seen=`, and everything the comment calls "the forensic tail") do not.

**Impact.** LOW in this session (`staticq=0/0`, `press_fresh_withheld=0` on every line — nothing was
withheld). HIGH the day it matters: a quarantine that silences a *real* meter (escalating TTL up to
8 × 20 s = 160 s over a 96 × 192 px cell, `simple_meter_reader.py:2206-2237`) is otherwise invisible.

**Minimal fix.** Promote `STATIC ZONE WITHHELD` to `warning` but rate-limit it to first-hit-per-zone
plus one per 0.5 s (the same idempotence pattern already used for ghost evictions,
`simple_meter_reader.py:2066-2072`); and split `DETECTOR HEALTH` into two ≤ 280-char lines rather
than letting the tail be cut.

**Verification test.** `tests/test_simple_reader_sidecar_wiring.py` (extend): assert every
`_acq_logger` record the reader emits at WARNING/ERROR is ≤ 280 characters.

---

## F10 — LOW — GPU provider fallback is honest, but only visible in a line that can be truncated

`meter_detector_yolo.py:103-130` resolves providers in a fixed priority
(CUDA → DirectML → CPU) and always keeps CPU as the final fallback; `:228` records the *actual*
provider (`self._sess.get_providers()[0]`), which is then surfaced as `provider=` at the very front
of the `DETECTOR HEALTH` line. So a silent DirectML→CPU demotion **is** observable — `provider=` is
the first field and survives the F9 truncation.

**Not active this session:** every `DETECTOR HEALTH` line reads `provider=cv-contour`
(`logs/orion_native.log`, e.g. the 03:26:54.609 line), i.e. the CV proposer, not ONNX. `infer=7–10ms`,
`calls=46017`, `stale=0`.

**Residual.** `meter_detector_yolo.py:250-251` disables the GPU preprocessing graph whenever the
provider is not CUDA, and records the reason in `self._prep_disabled` — which is **not** printed on
the health line. On a DirectML box this silently costs the preprocessing win with no log at all.
**Fix:** append `prep=<ok|reason>` to the health line. **Verify:**
`tests/test_meter_detector_provider_priority.py` (extend) — assert the emitted health payload
carries the prep state for each provider.

---

## Test baseline (run by this agent, read-only)

```
pytest tests/test_readiness_incident.py tests/test_orchestrator_feed_gate.py \
       tests/test_frame_integrity_pipeline.py tests/test_shot_gate_atomicity.py
  -> 47 passed in 0.92s

pytest tests/test_simple_reader_ghost_press.py tests/test_simple_reader_lock_lifecycle.py \
       tests/test_meter_locator_static_ranking.py tests/test_remote_play_client_lifecycle.py \
       tests/test_shot_gate_arm_observability.py
  -> 140 passed in 13.31s
```

(`-p no:cacheprovider`, `--basetemp` under this agent's scratch dir, no repo writes.)

---

## Things I checked and found CLEAN (recorded so nobody re-walks them)

- **Reader→native epoch join in the meter path.** Latched to the captured epoch
  (`simple_meter_reader.py:4670-4681`), carried on the atomic snapshot
  (`remote_play_orchestrator.py:4704-4713`), enforced at 8 native sites. Only F3 bypasses it.
- **Release-marker identity.** `remote_play_orchestrator.py:8360-8376` drops any release marker
  whose `physical_epoch`/`shot_attempt` do not match the live gate epoch and pose-arm token, with a
  log line. Zero such drops in this session.
- **Frozen-echo defence.** `pixel_duplicate` is deliberately non-fail-closed
  (`remote_play_orchestrator.py:4873-4882`) but `pixel_age_ms` still climbs because the processed
  snapshot does not advance, and `RemotePlaySession.cpp:4658` gates on `pixelAgeMs_ <= 200.0`.
  A frozen feed therefore fails closed on age, while a legitimate loading screen keeps
  `transport_age_ms` low and does not trip the native 8 s stall watchdog. Correct by construction.
- **Shot-gate arm latency.** `arm_delivery_ms` on every `SHOT-GATE ARM RECEIPT` this session is
  0.04–2.04 ms — the `@_with_shot_gate_lock` serialisation between the stdin thread and the CV
  thread is not a bottleneck.
- **SHM preview transport.** `open_fail_run=0 read_fault_run=0 timestamp_rejects=0
  event_wait_failures=0` across the whole session (`logs/orion_native.log:25614`). No degradation.
- **Ghost breaker on epoch 22.** I initially suspected the ghost quarantine had eaten a real,
  late-acquired meter (`:22989`, lock dropped at 46.8 % 1.09 s into a 1.59 s press, then
  `PRESS WITHHOLD SUMMARY: ghost_static=4 cold_first=4` at `:23000`). **Refuted:** the object
  produced `GHOST IDENTITY RETIRED AFTER FULL ABSENCE: full_nofinds=2` 45 ms later — it vanished
  entirely, which a live meter cannot do. The breaker was right; that press genuinely had no meter.
- **Standby-pool / readiness-tracker interaction.** Hypothesised the standby child's log could
  poison `_select_current_log`. Refuted — `_promote_standby` re-snapshots the baseline and resets
  the tracker (`remote_play_client.py:2455-2457`), and a standby opens no session so writes no
  session log.
- **Deferred command queue.** `recover_input` and `start_stream` are deferred to a worker
  (`autogreen_sidecar.py:3195-3198`); shot-gate arm/release/disarm stay inline on the stdin thread,
  so a 17 s recovery cannot delay a press. The queue is bounded and refusal is reported, not
  silently dropped (`autogreen_sidecar.py`, `_defer_command`).

---

## Answers to the four goals, in one line each

1. **Readiness** — CONSEQUENCE. The fork stops the session (F1), the pipe closes 1 ms later, the
   native orders a recovery 120 ms later, and the readiness warning arrives 2.1 s after that because
   the PS5 answers the relaunch with `Remote is already in use`. There *is* a sidecar→native
   revocation path (telemetry `input_ready` → `recoverInputLink`), and it is the **amplifier**, not
   the trigger (F2).
2. **Fail-closed feed** — correct and un-wedgeable as audited (F7); the detector's authority is
   revoked atomically and restored by one clean completion; the gate has a wall-clock cap so it
   cannot stay armed-but-blind; the native falls back to `pass_through` rather than waiting forever.
3. **False positives** — none could fire the bot on a non-shot *in the meter path* (ownership needs
   a physical epoch and an epoch-matched structure stamp). The exposure is F3 (that stamp check is
   off in No-Meter/Input-Timed mode) and F6 (idle CV self-arm feeds the false-lock churn). Cost this
   session was zero (`staticq=0/0`, 0 quarantine withholds).
4. **Robustness** — F4 (input fault silently disarms meter timing), F5 (`D:\` corpus on by default),
   F8/F9/F10 (three silent-degradation / mis-leading-forensics defects in the only telemetry that
   would reveal any of this).
