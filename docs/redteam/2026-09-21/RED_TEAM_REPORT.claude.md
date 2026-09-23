# Red team report — claude — 2026-09-21
Working tree: NexusVision @ 0eca7d2 (+ uncommitted), chiaki-ng-src @ c7515213 (+ uncommitted)
Surfaces covered: WAVE 1 A–E (controller input path, automation engine, meter detection + sidecar
gate, session/decoder/fork thread pinning, route-recovery state machine)
Surfaces NOT covered: WAVE 2 S1–S16 (assigned to the Gemini/Codex prompt, PROMPT_WAVE2_ALL_PATHS.md)
Method: five read-only Opus agents, one per surface, from EVIDENCE_PACKET.md. Logs read:
logs/orion_native.log (owner session 2026-09-22T03:05–03:27Z) and the fork's own
%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log (two incident traces survived pruning). Python
tests run read-only: reader/sidecar 47 + 140 passed. Full evidence per surface:
A-controller-input-path.md, B-automation-engine.md, C-meter-detection-sidecar.md,
D-session-decoder-fork.md, E-route-recovery-state-machine.md. One finding per root cause; where
several agents found the same thing it is one entry with every citation.

## CATASTROPHIC

(none on these surfaces)

## CRITICAL

### [CL-001] CRITICAL — A 25 ms local-delivery miss on one input packet kills the whole Remote Play session
- Surface: A/D/E fork input bridge (amplifier of the owner's bug 1)
- Exploit / failure path: the bridge waits `ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25` for local UDP acceptance of a MustDeliver packet → timeout → `fail_transport("local_delivery_timeout_or_mismatch")` → `abort_orion_transport` (wipes every queued release) → Failed ACK (ack_stage=128, ack_error=15 TIMEOUT) → `chiaki_session_stop()`. The console session dies; the PS5 then answers the relaunch with "Remote is already in use" (0x80108b10) for ~9 s.
- Affected: chiaki-ng-src/gui/src/orioninputbridge.cpp:20, :281-292, :150-165; lib/src/feedbacksender.c:470
- Impact: any latency blip on the input path becomes "Venice disconnected" for the human; 10 kills in 21 min in the owner's session; the recovery ladder (3 × ~2.2 s) cannot cover the console re-handshake.
- Reproduction: run OrionStream 0dad53db; press TRIANGLE + CIRCLE together and release; see `fatal_local_delivery_fault … action=stop_session` in chiaki_session_*.log and `ack_stage=128 ack_error=15` in orion_native.log.
- Evidence: orion_native.log L22228 (03:11:13.072Z) and 102 more heartbeats with the signature; `failures=0` all session (the launcher never closed first); chiaki_session_2026-09-21_22-23-00 L427-460; ack_wait_us=25602 / 25119 at the two fatal ACKs (E).
- Root-cause hypothesis: fail-closed was applied to the SESSION where it belongs to the BOT's authority.
  - Supporting evidence: all 10 incidents carry the signature; every launcher reaction is a correct downstream consequence.
  - Evidence that would refute it: a kill without ack_error=15, or a `failures>0` heartbeat before one — none exist.
- Required fix: a local-delivery TIMEOUT fails the PACKET, not the session: write the Failed ACK, drop the queue (`owning_` stays latched so passthrough is never unmasked over stale commands), keep the pipe server alive and re-accept; keep status MISMATCH / enqueue failure / ACK-write failure session-fatal. Budget 25 → 40 ms (inside the client's 50 ms ceiling, OrionInputClient.cpp:83), treated as a tripwire that escalates only on consecutive misses.
- Verification test: fork unit test — a MustDeliver whose delivery is held > 40 ms → Failed ACK, queue dropped, session still running, next packet delivered. Owner rig: the chord no longer disconnects.
- Owner decision needed: no
- Confidence: high (four agents + the fork's own log)
### [CL-002] CRITICAL — The +40 ms third release echo head-of-line-blocks the FIFO under the 25 ms budget (the regression)
- Surface: A/E/C fork queue (trigger of the owner's bug 1)
- Exploit / failure path: every pure button release now enqueues 3 FIFO entries (primary, twin +3 ms, echo +40 ms); the sender dequeues only when `head->not_before_ms <= now`, so a not-yet-due echo at the head stalls the next REQUIRED edge; a chord's second release lands inside the first's 40 ms window and misses the 25 ms budget by ~9 ms.
- Affected: chiaki-ng-src/lib/include/chiaki/orioninput.h:28 (`CHIAKI_ORION_INPUT_RELEASE_ECHO_DELAY_MS 40`); lib/src/feedbacksender.c:276-278 (3 entries), :893-901 (head-only dequeue). The "expedite" mitigation at :274-277 is NOT in the shipped binary: orioninput.c was edited 18:25, the build is from 18:03, the symbol is absent from the object.
- Impact: regression bisect — OrionStream d80be5f8: 24 h, 13,237 heartbeats, 0 failures, bursts of 136 writes/s clean; 0dad53db: 10 kills in 21 min.
- Reproduction: two pure releases within 40 ms (any chord release; any sprint release + button).
- Evidence: A — seq 30942 `depth=3`, 30943 `depth=4` (`queue_wait_us=17118`), 30944 deadline 28:011 vs echo due 28:020, fatal 28:012; E — the bisect; C — L427-460.
- Root-cause hypothesis: delayed redundant copies share the FIFO with required edges. Supporting: depth=4 with an echo pending at each kill. Refuting: a kill with depth ≤ 2 — none.
- Required fix: (a) a not-yet-due REDUNDANT entry must never block a required edge — skip to the first due required entry or keep copies in a side queue; (b) keep the expedite (copies go ahead of the edge with their wait removed); (c) shed the echo when the queue is busy (count > 2), keep it at idle where stuck buttons actually happen; (d) rebuild + redeploy OrionStream — the shipped binary lacks the mitigation regardless of any code fix.
- Verification test: fork unit test — enqueue release A then release B within 5 ms; B's primary is sent before A's echo and B's ack_wait < 10 ms.
- Owner decision needed: no
- Confidence: high

## HIGH

### [CL-003] HIGH — R2 "clamps down": trigger releases can be lost with no repair path anywhere in the stack
- Surface: A (fork) + B (launcher) + E
- Exploit / failure path: `ChiakiFeedbackState` has no l2/r2 — triggers reach the PS5 only as HISTORY events, so one lost R2-release datagram latches sprint on. The redundancy vetoes itself: `is_pure_button_release` requires `pressed == 0`, so an R2→0 that shares a 4 ms launcher tick with any button press gets no twin and no echo. Nothing repairs it afterwards: the 1 Hz `reassertLastState()` is state-only (`needs_history=0`), the stale-output watchdog is Square-only, and the recovery "shot controls neutral" predicate ignores L2/R2. On a CL-001 abort the queued R2 release is wiped and the console holds R2 for the outage.
- Affected: chiaki-ng-src/lib/include/chiaki/feedback.h:16-25; lib/src/orioninput.c:75-81; native_orion/src/OrionAppController.cpp:4410 (tick merge), :6051-6069 / :13455-13472 (Square-only watchdog); ShotIntentPolicy.h:75-84; feedbacksender.c:470
- Impact: sprint stuck on until the next R2 edge; the owner saw it this session.
- Reproduction: release R2 in the same tick as a face-button press on a lossy link; or trigger CL-001 with R2 held.
- Evidence: A's decode of feedback.h and orioninput.c:81; zero `observeOutputDivergence` r2 lines in 8 MB (launcher exonerated); the stale-r2 re-seed and the XUSB mapping both refuted (A).
- Root-cause hypothesis: trigger releases are the one input class with neither wire redundancy nor a launcher-side corrector. Refuting evidence would be an r2 output≠physical divergence line — there are none.
- Required fix: the launcher emits a trigger zero-crossing as its OWN write (never merged into a tick with a press edge) so it qualifies as a pure release and gets the twin/echo; the fork lets a terminal trigger release qualify alongside a press; recovery neutrality includes l2 == r2 == 0; widen the stale-output watchdog to triggers.
- Verification test: fork unit test — {R2 255→0, Cross 0→1} → a redundant release copy is queued for the trigger; native test — a tick with both edges produces two writes, trigger release first.
- Owner decision needed: no
- Confidence: medium-high (mechanism certain; which instance bit the owner needs the `delivered_r2=` instrument A names)

### [CL-004] HIGH — The launcher's release-repair probe is timed to land inside the fork's echo window
- Surface: A/B/E launcher
- Exploit / failure path: 24 ms after EVERY digital release edge (including the player's pass-through buttons) the launcher writes a blocking MustDeliver "release-repair duplicate"; with the 40 ms echo pending that write is the one that times out — 2 of 10 incidents show `result=5` (WrittenUnconfirmed) closing the pipe itself. It also re-asserts `last_`, which does not advance past a Failed/Unconfirmed write.
- Affected: native_orion/src/OrionAppController.h:2239 (`kHookReleaseRepairDelayMs_ = 24`); OrionAppController.cpp:13473-13477, :13668-13716; OrionInputClient.cpp:435-440
- Impact: the launcher re-arms the retrigger loop by itself.
- Reproduction: any release while an echo is pending.
- Evidence: log 24514/24515 and 24568/24569 (result=5 → route change 1 ms later).
- Required fix: move the probe past the echo window (24 → 60 ms), gate the 1 Hz idle reassert on ≥ 60 ms since the last release edge, never reassert an unconfirmed `last_`.
- Verification test: native unit test on the probe scheduler; owner rig: no `result=5` closes.
- Owner decision needed: no
- Confidence: high
### [CL-005] HIGH — The feedback-sender thread exits silently on a history-flush or repeat-identity error
- Surface: D/A/E fork
- Exploit / failure path: any non-OVERFLOW `feedback_sender_flush_history_locked` error, or a repeat-history identity mismatch, sets `should_stop = true` and breaks the sender loop; nothing above is told; the next MustDeliver then times out and presents as CL-001.
- Affected: chiaki-ng-src/lib/src/feedbacksender.c:968-973, :598-636 (identity check on a single shared snapshot slot that `abort_orion_transport` zeroes at :484)
- Impact: unattributable input death; explains the 22:22 clustering (`ack_failures` 1→7 with no reset).
- Reproduction: force an INVALID_DATA on the repeat path (unit test).
- Evidence: code path; the formatter error string is absent from this session's logs, so it did not fire here but is reachable.
- Required fix: drop the optional copy and continue (log at ERROR); escalate to stop only after N consecutive hard send errors; never on a redundant copy.
- Verification test: fork unit test injecting the error → sender thread alive, next packet delivered.
- Owner decision needed: no
- Confidence: high

### [CL-006] HIGH — Recovery amplifies the fault: the sidecar kills a healthy child on MISSING evidence, and the ladder cannot cover the console re-handshake
- Surface: C/D sidecar ↔ launcher
- Exploit / failure path: telemetry `input_ready=false` → `shouldAutoRecoverInputFromTelemetry` (treats missing evidence as false) → `recoverInputLink` → `recover_input_link()` unconditionally kills a possibly-healthy Chiaki child; separately, recovery is 3 attempts ~2.2 s apart against a ~2.4 s PS5 re-handshake ("Remote is already in use").
- Affected: native_orion/backend/autogreen_sidecar.py:2601; native_orion/src/SidecarWatchdog.h:161-167; remote_play_client.py:95-98, :243, :1824-1827; the recovery ladder in OrionAppController.cpp (D F-4)
- Impact: 10 drops → 8 recoveries → 2 full sidecar restarts in 22 min; all 3 attempts failed at 03:11, the successes came 4–9 s later.
- Reproduction: any CL-001 event.
- Evidence: C's ordering proof (session stop → pipe closed +1 ms → recovery +120 ms → readiness −2.1 s); D F-4.
- Required fix: missing evidence is not a negative (require an explicit false, N consecutive); space the ladder past the re-handshake or wait for the "already in use" window to clear; with CL-001 fixed most of this path stops firing.
- Verification test: sidecar unit test for the watchdog predicate; log shows no `recover_input_link` on a healthy child.
- Owner decision needed: no
- Confidence: high

### [CL-007] HIGH — `inputRecoveryStarted` resets the engine without neutralising owned output
- Surface: B launcher
- Exploit / failure path: it is the only `reset()` call site with neither `neutralizeOwnedInput()` nor `failClosedNeutralThisTick`; `reset()` also wipes the drain before the disarm handler can latch it — a bot-held Square is stranded with no release, no drain, no census line.
- Affected: native_orion/src/OrionAppController.cpp:2668-2695 (cf. :4223, :4821, :5116, :5295, :6114, :6199, :12539); AutomationEngine.cpp:5140, :4890-4911
- Impact: the bot can leave Square held across a pipe recovery — input held outside a shot.
- Reproduction: trigger a pipe recovery while the bot owns a shot.
- Evidence: code; did not fire in this session (engine idle at every kill).
- Required fix: neutralise owned input before `reset()` in that slot (3 lines).
- Verification test: AutomationEngineTests — recovery during Holding → output Square released, drain latched.
- Owner decision needed: no
- Confidence: high

### [CL-008] HIGH — In No-Meter / Input-Timed modes the sidecar↔native epoch binding is switched off
- Surface: C/B engine
- Exploit / failure path: `strictOwnershipProof` is false by construction in those modes, disabling the `gameplay_structure_epoch == physicalShotEpoch` join, the coarse-fill proof and the timing-identity checks — a ghost or previous-shot meter can own a press with only the sidecar's heuristic ghost breaker in the way.
- Affected: native_orion/src/AutomationEngine.cpp:6271-6272 vs :14330-14333 vs :14349-14355
- Impact: bot fires on a non-shot in those modes.
- Reproduction: No-Meter mode, a feedback meter from the previous shot visible at the press.
- Evidence: code; C's F3.
- Required fix: keep the epoch join on in every mode; only relax the fill proof where the mode has no fill.
- Verification test: engine test with a stale-epoch proposal in Input-Timed mode → not owned.
- Owner decision needed: yes — confirm No-Meter mode is out of the beta (memory says it is) so this can ship as a gate rather than a fix.
- Confidence: medium

### [CL-009] HIGH — The uncommitted physical-core affinity fails silently and ignores efficiency cores
- Surface: D fork (uncommitted)
- Exploit / failure path: `SetThreadGroupAffinity` failure is discarded with `(void)` and never logged; `Processor.EfficiencyClass` is ignored, so on hybrid Intel the feedback sender can be hard-pinned to an E-core under EcoQoS — which, via CL-001, is a disconnect rather than jitter. Verified: cannot crash, cannot pin to a nonexistent core, and it does fix the old LP0/LP1 same-core collision.
- Affected: chiaki-ng-src/gui/src/streamsession.cpp:72; lib/src/thread.c:171
- Impact: a class of customer machines regresses silently.
- Required fix: log the failure at WARNING; skip E-cores (EfficiencyClass > 0); fall back to no pinning when the topology has < 3 physical P-cores.
- Verification test: unit test of the topology resolver with a hybrid layout fixture.
- Owner decision needed: no (Astra's lane — hand over)
- Confidence: high
## MEDIUM

### [CL-010] MEDIUM — Every analog trigger step is a MustDeliver round trip
- Surface: E/A launcher + fork. Trigger travel is classified as a button edge (OrionInputClient.h:200-203) and the fork's coalescer refuses flagged/trigger packets (orioninputbridge.cpp:190, :206-207) — an L2/R2 ramp is a burst of synchronous transactions that feeds CL-001. Fix: only trigger ZERO-CROSSINGS are MustDeliver; intermediate travel is latest-wins like the sticks. Test: ramp R2 0→255 → one MustDeliver at the crossing. Owner decision: no. Confidence: high.

### [CL-011] MEDIUM — The scheduled-fire packet freezes the whole pad at arm time
- Surface: B engine. The precise-fire packet snapshots l2/r2/dpad/every non-shot button when armed and replays it at the deadline (OrionAppController.cpp:13230, :1612); only the blind InputTimed token is refreshed (:13197-13213). Bounded ~17 ms normally, up to ~135 ms on an evidence gap — the only real press-release-press mechanism on this surface. Fix: refresh the non-shot fields from the live pad at submit. Test: arm, change R2, fire → packet carries the live R2. Owner decision: no. Confidence: high.

### [CL-012] MEDIUM — `scheduleFire()` enforces the controller-route binding only for `InputTimed`
- Surface: B engine (AutomationEngine.cpp:17324-17328). A meter/pose token can be minted with generation=0 and then destroys the whole engine at the worker's submit check instead of being refused for free. Fix: enforce the binding for every authority. Test: mint a meter token with generation 0 → refused at schedule. Owner decision: no. Confidence: high.

### [CL-013] MEDIUM — A controller-route fault silently revokes the METER timing authority
- Surface: C. "Latency route re-keyed (controller_delivery_route_transition)" → "Latency authority reset … fresh route evidence required" (orion_native.log L22219, L22327, L22698): an input-lane hiccup disarms the measured-lead path with no user-visible message. Recovers by itself. Fix: keep the lead authority across a route re-key when the route generation is proven again within N s; surface it in the activity feed. Owner decision: no. Confidence: medium.

### [CL-014] MEDIUM — Production default writes a per-shot play corpus to a hard-coded `D:\` path
- Surface: C. `ORION_SHOT_RECORDS` defaults ON and writes JSONL to `D:\NexusVision\shot_records` (shot_records.py:220-241); the launcher never sets the env var; three files from this session are on disk. Privacy + packaging + blue-team item. Fix: default OFF in production builds; path under the per-user data dir when on. Owner decision: yes — keep it for the owner's rig only? Confidence: high.

### [CL-015] MEDIUM — The CV self-arm can hold the reader's relaxed-acquire gate open indefinitely
- Surface: C (remote_play_orchestrator.py:7874-7882 → :2519-2528). Any "rising" detection with no physical press re-opens a 20 s / 2400-frame window with no cap — self-reinforcing false-lock churn (cannot fire: never touches the hardware arm). Fix: cap re-arms per idle period. Owner decision: no (Astra's lane). Confidence: medium.

### [CL-016] MEDIUM — A diagnostic env var makes video packets parse in video-disabled mode and advances gkcrypt key state
- Surface: D fork (uncommitted takion.c:1698-1721, :1830). A debug flag reaching the crypto path in Orion's exact production config (`disable_video` = capture-card mode). Fix: gate the parse behind the same condition as the consumer; never touch key positions for a discarded packet. Owner decision: no (Astra's lane). Confidence: medium.

### [CL-017] MEDIUM — `ORION_AVCLOCK_PATH` is unvalidated and its per-frame I/O runs on the Takion receive thread
- Surface: D fork (uncommitted takion.c:464-472 and the CSV writer). `fopen("wb")` with no containment check, relative paths resolve to CWD, every reconnect truncates the previous session, two OrionStream generations double-open it, the `FILE*` leaks if the thread fails to start, and the per-frame `fprintf` + 1 Hz `fflush` runs on the receive thread pinned to core 0 — the same disk the framedump harness stalls 250 ms on. Fix: validate + contain the path, open once per session with append, move the write off the hot thread. Owner decision: no (Astra's lane). Confidence: high.

### [CL-018] MEDIUM — After a transport fault the input pipe server is gone for the life of the process
- Surface: D/E fork (orioninputbridge.cpp:349). The single-instance pipe is never recreated after a fault, so any non-fatal fault would still need a full OrionStream restart. Fix: loop back to ConnectNamedPipe after a clean fault (part of the CL-001 fix). Confidence: high.

### [CL-019] MEDIUM — OrionStream's transport log is neither relayed nor retained
- Surface: A/E forensics. `grep -c "Orion transport" logs/orion_native.log` = 0; the child log lives in %APPDATA%\Chiaki\Chiaki\log, pruned to 5 files — the literal failure reason was invisible to every investigation until A dug it out. Fix: on INPUT LINK LOST, copy the last 200 `Orion transport` lines of the newest child log into orion_native.log; retain the last 20 files. Confidence: high.
## LOW

- [CL-020] LOW — No MMCSS boost on the feedback-sender thread (reverted in fork c7515213); the 15.6 ms Windows quantum alone eats 62 % of a 25 ms budget (E). Re-evaluate after CL-001.
- [CL-021] LOW — `SessionLog::Log` does a synchronous per-line `flush()` (~14 lines per release) inside the delivery budget (A). Buffer + periodic flush.
- [CL-022] LOW — REDUNDANT_FLICK copies are never expedited: an 8 ms tax on every Tempo shot (A).
- [CL-023] LOW — Onset feedforward: aim bucket and teach bucket can differ and the staleness fence is not config-bound (B-7). Bind both to config; assert bucket identity in the grader.
- [CL-024] LOW — AV-clock wall projection silently absorbs system-clock steps (D F-9); `SharedMemoryFrameReader::open()` does not close cached notification handles (D F-10, latent).
- [CL-025] LOW — Forensics traps: `BOX LATCHED … native_ownership=unconfirmed` is a hard-coded literal (simple_meter_reader.py:7190-7193); STATIC ZONE WITHHELD is debug-only and the DETECTOR HEALTH tail is cut by `trimmed.left(300)` in RemotePlaySession.cpp; the dark_frame count is cumulative so one 7 s blackout reads as two incidents (C).

## Checked and clean (recorded so nobody re-walks them)

The dark-frame fail-closed path revokes atomically, has a wall-clock cap and falls back to pass-through (21 SHOT NOT OWNED, 0 hangs). A scheduled fire during a pipe close is DROPPED, never replayed (three independent guards). The release-repair duplicate cannot manufacture a press; the 40 ms echo cannot cancel a later press. No chord shortcut exists in the fork (upstream's only combo is L1+R1+DpadUp). The launcher never initiated a close (`failures=0` all session); the stale-r2 re-seed and the XUSB trigger mapping are 1:1 and exonerated. The affinity code cannot crash or pin to a nonexistent core; the monotonic-clock rewrite fixes a real int64 overflow at ~10.7 days. Executable policy and RemotePlaySession handle/thread lifecycle are clean. The frame-export-stall hypothesis is refuted (capture-card mode runs `disable_video=True`).

## Executive summary

1. Both owner bugs are explained with the fork's own log, not inference. Bug 1: a 25 ms local-delivery miss calls `chiaki_session_stop()`; the miss is caused by tonight's +40 ms third release echo sitting at the head of the same FIFO as the next required edge. Bisect: 0 kills in 24 h before the echo, 10 in 21 min after.
2. The shipped OrionStream lacks the "expedite" mitigation entirely — rebuild + redeploy is mandatory whatever else changes.
3. Bug 2: triggers have no wire redundancy and no corrector anywhere; a lost R2 release latches sprint. The launcher is exonerated.
4. The launcher's own +24 ms repair probe and the sidecar's "missing evidence = false" recovery both amplify the fault; neither is the cause.
5. Everything the bot must never do (fire unowned, fire twice, replay a fire across a route change) held up — except one reset path that can strand a bot-held Square (CL-007) and one mode that switches the epoch join off (CL-008).
6. Astra's uncommitted fork changes are sound in the main but carry three real items (CL-009, CL-016, CL-017).
7. Everything else is robustness and forensics.

## Findings table

| id | severity | surface | one-liner |
|---|---|---|---|
| CL-001 | CRITICAL | fork bridge | 25 ms delivery miss → `chiaki_session_stop()` — one packet kills the session |
| CL-002 | CRITICAL | fork queue | +40 ms echo head-of-line-blocks required edges; shipped binary lacks the expedite |
| CL-003 | HIGH | fork + launcher | trigger releases have no redundancy and no corrector — R2 latches |
| CL-004 | HIGH | launcher | +24 ms release-repair probe lands inside the echo window; re-asserts unconfirmed state |
| CL-005 | HIGH | fork | sender thread exits silently on flush / identity errors |
| CL-006 | HIGH | sidecar + launcher | recovery kills a healthy child on missing evidence; ladder can't cover re-handshake |
| CL-007 | HIGH | launcher/engine | recovery reset strands a bot-held Square |
| CL-008 | HIGH | engine | No-Meter/Input-Timed modes switch the epoch join off |
| CL-009 | HIGH | fork (uncommitted) | affinity fails silently, ignores E-cores |
| CL-010 | MEDIUM | launcher + fork | every trigger step is a MustDeliver round trip |
| CL-011 | MEDIUM | engine | scheduled fire freezes the whole pad at arm time |
| CL-012 | MEDIUM | engine | route binding enforced only for InputTimed |
| CL-013 | MEDIUM | native | route fault silently revokes meter timing authority |
| CL-014 | MEDIUM | sidecar | shot-record corpus written to a hard-coded D:\ by default |
| CL-015 | MEDIUM | sidecar | CV self-arm churn with no cap |
| CL-016 | MEDIUM | fork (uncommitted) | debug flag reaches the gkcrypt key path |
| CL-017 | MEDIUM | fork (uncommitted) | AV-clock CSV unvalidated, I/O on the receive thread |
| CL-018 | MEDIUM | fork | pipe server never recreated after a fault |
| CL-019 | MEDIUM | forensics | OrionStream transport log not relayed or retained |
| CL-020–025 | LOW | various | MMCSS, per-line flush, flick expedite, FF buckets, clock/handles, log literals |

## Release verdict for the beta

**blocked** — by CL-001, CL-002, CL-003, CL-004, CL-005 (the owner's two reproduced bugs and their amplifiers). CL-006 and CL-007 should ship in the same patch; CL-008 needs the owner's word on No-Meter mode; CL-009/016/017 go to Astra. Re-verdict after the patch is built, redeployed, and the owner's chord + sprint test passes.

## Coordinator verification (Claude, at the cited lines — read-only)

Every line the agents lean on for the two owner bugs was re-read directly, not taken on trust:
- `orioninputbridge.cpp:20` `ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25`; `:143-165` `fail_transport` → `chiaki_session_abort_orion_transport` → Failed ACK → `chiaki_session_stop()`; `:288` the 25 ms wait; `:334-340` a plain client disconnect while owning escalates via `owner_pipe_disconnected`; `:349-351` `if(transport_fault) break;` — the pipe server is never recreated (CL-001, CL-018 confirmed).
- `feedbacksender.c:284-312` pushes primary, twin (`redundant_echo=1`), echo (`redundant_echo=2`), then the state copy; `:893-901` dequeues ONLY when the head is due — a not-yet-due echo blocks everything behind it (CL-002 confirmed). The expedite comment sits at `:274-277` in the working tree; A's symbol scan shows it absent from the shipped object.
- `orioninput.c:64-81` `is_pure_button_release` returns `(released || trigger_released) && pressed == 0 && !trigger_increased` — a trigger release sharing a packet with any press gets no copy (CL-003 confirmed; the packet's "triggers are not covered" claim was wrong — they are, unless a press coincides).
- `OrionInputClient.h:200-203` every button OR trigger change is MustDeliver (CL-010 confirmed).
- `OrionAppController.cpp:2668-2695` the `inputRecoveryStarted` slot calls `automation_.reset()` with no `neutralizeOwnedInput()` (CL-007 confirmed); `OrionAppController.h:2239` `kHookReleaseRepairDelayMs_ = 24` (CL-004 confirmed).
- `SidecarWatchdog.h:161-167` `return running && !recoveryPending && (!hasExplicitInputReady || !inputReady);` — missing evidence recovers (CL-006 confirmed).
- `ShotIntentPolicy.h:75-84` `shotInputControlsNeutral` = Square up + right stick centred; no L2/R2 (CL-003 confirmed).
- `logs/orion_native.log` L22228: `ack_stage=128 ack_error=15 ack_winerr=31`, 103 lines; deployed OrionStream 18:04:28 = the `0dad53db` image the owner tested.

## Proposed patch set — NOT applied (owner: "don't patch it yet")

Ordered so each step is independently testable. Nothing here weakens fail-closed: ambiguous writes still close the pipe, recovery still needs a forced write with shot controls neutral, fire tokens stay bound to (generation, route), `owning_` stays latched across every fault, and the bot still cannot fire on an unproven route.

Fork (chiaki-ng-src, branch orion):
1. `gui/src/orioninputbridge.cpp:281-292` — on `local_delivery_timeout`: write the Failed ACK, call a new `drop_orion_queue()` (abort minus `should_stop`; `owning_` stays true), `break` the read loop with a flag that suppresses the `:334-340` escalation, then fall through to re-accept (remove `:349-351`'s `if(transport_fault) break;` for this case). Status MISMATCH, enqueue failure and ACK-write failure keep the current fatal path. `:20` → 40 ms.
2. `lib/src/feedbacksender.c:893-901` — dequeue the first DUE entry whose predecessors are all redundant-and-not-due, instead of the head only; a required edge never waits on a copy. Keep the expedite at `:274-277`.
3. `lib/src/feedbacksender.c:284-312` — skip the echo (`redundant_echo=2`) when `orion_queue.count > 2`; keep it at idle.
4. `lib/src/orioninput.c:75-81` — let a terminal trigger release qualify when the only simultaneous change is a press whose copy would be suppressed anyway (copy the state with the pressed bits masked back to `previous`), or leave this to launcher step 8.
5. `lib/src/feedbacksender.c:968-973` and `:602-613` — INVALID_DATA on a redundant copy drops the copy and continues (log at ERROR); `should_stop` only after 16 consecutive hard send errors on REQUIRED entries.
6. Rebuild `build-orion-optimized-ffmpeg7` + unit tests (`build-orion-unit-ffmpeg7`) + redeploy `OrionStream.exe` — mandatory (the shipped binary predates the expedite).

Launcher (native_orion):
7. `OrionAppController.h:2239` `kHookReleaseRepairDelayMs_` 24 → 60; gate the 1 Hz idle reassert on ≥ 60 ms since the last release edge; never reassert an unconfirmed `last_` (`OrionInputClient.cpp:435-440`).
8. `OrionInputClient.h:200-203` — only trigger ZERO-CROSSINGS are MustDeliver, and a zero-crossing is emitted as its own write ahead of any button edge in the same 4 ms poll, so it qualifies as a pure release on the wire.
9. `OrionAppController.cpp:2668-2695` — neutralise owned output before `reset()` (3 lines, mirror `:4223`).
10. `ShotIntentPolicy.h:75-84` — `shotInputControlsNeutral` also requires `l2 == 0 && r2 == 0`; widen the stale-output watchdog (`:6051-6069`) to triggers.
11. `SidecarWatchdog.h:161-167` — require an explicit `inputReady == false` (drop the `!hasExplicitInputReady` arm); recovery ladder spacing ≥ 3 s or wait for the "already in use" window.
12. Forensics: on INPUT LINK LOST copy the last 200 `Orion transport` lines from the newest `%APPDATA%\Chiaki\Chiaki\log\chiaki_session_*.log` into `orion_native.log`; raise the child-log retention from 5 to 20.

Handed to Astra (uncommitted fork lane): CL-009 affinity logging + E-core skip; CL-016 gkcrypt parse gate; CL-017 AV-clock path validation and off-thread writes.

Verification after the patch: fork unit tests (queue skip-ahead, timeout-non-fatal, trigger-release copy, identity-mismatch survival); native ctest 30/30; probe: chord release ×20 with `Orion transport` lines relayed → 0 `fatal_local_delivery_fault`, 0 `ack_stage=128`; owner rig: TRIANGLE+CIRCLE ×20 and sprint-release under contact ×20 → no disconnect, no R2 latch; then re-verdict.

## PATCH APPLIED — 2026-09-22 ~01:50 (Claude lane; owner: "you patch, then Codex runs the final")

Verified: fork chiaki-unit **147/147** (+2 new), native ctest **30/30** (+2 new, 1 contract test
updated), backend **360/360**, staff-auth **17/17**, admin CLI unchanged, Worker **38/38**,
`verify_site.py` OK. Deployed: **OrionStream.exe only** (`7786742a…`, 01:27, backup + DEPLOY_LOG).
NOT deployed (owner's word): the launcher build (`native_orion/build/Release`, 01:33+), the Lambda,
both Workers.

| ids | change | where |
|---|---|---|
| CL-001 | a local-delivery **TIMEOUT** fails the packet: Failed ACK → `chiaki_session_drop_orion_transport` (unsent queue only; sender, dirty history and the ownership latch kept) → close the pipe instance → **re-listen**. `chiaki_session_stop` is no longer reachable from a timeout; mismatch / CANCELED (dead sender) / enqueue / ACK-write stay fatal. Budget 25 → 40 ms. | fork `gui/src/orioninputbridge.cpp`, `lib/src/{session,feedbacksender}.c`, headers; test `soft_drop_keeps_sender_alive` |
| CL-002 | Astra's expedite is now in the shipped binary (first time); the +40 ms echo is **shed when the queue holds > 2 entries** (`chiaki_orion_input_release_echo_allowed`). The "skip-ahead" idea was rejected: a copy re-sent after a later re-press would put the button back up (Astra's `no_late_up` test). | fork `feedbacksender.c`, `orioninput.{c,h}`; test `release_echo_allowed_only_when_quiet` |
| CL-003 | only trigger **zero-crossings** are MustDeliver; a trigger release that shares a poll with a new press is sent **first as its own transaction** (so the fork copies it); recovery neutrality now requires `l2 == r2 == 0`. | `OrionInputClient.{h,cpp}` (`splitTriggerReleases_` counter), `ShotIntentPolicy.h` |
| CL-004 | release-repair probe 24 → **60 ms**; client final-ACK ceiling 50 → 65 (above the new 40 ms budget). Idle reassert already gated at 250 ms of digital rest; B's "stale last_" path is closed by close-on-failure (A's reading held). | `OrionAppController.h`, `OrionInputClient.cpp` |
| CL-006 | watchdog recovers only on an **explicit** `input_ready=false` (four `static_assert`s + the contract test flipped). Ladder spacing: not changed — with CL-001 the session is no longer killed, so the "already in use" re-handshake does not occur. | `SidecarWatchdog.h`, `OrionInputProtocolTests.cpp` |
| CL-007 | `inputRecoveryStarted` now calls `neutralizeOwnedInput()` after `reset()`. | `OrionAppController.cpp` |
| GM-002 / CX-001 | `learning.json`: a valid file is promoted to `.bak` on every save; an unreadable file is **quarantined** (`learning.json.corrupt-<utc>`), the backup **restored**, the launcher logs "Learning data: …". `load()` now goes through the same loader. | `AppConfig.{h,cpp}`; tests `learningRecoversFromBackupWhenCorrupt`, `learningWithoutBackupQuarantinesAndDefaults` |
| GM-006 / CX-007 | calibration **Cancel restores** the lead the customer started with. | `OrionAppController.{h,cpp}` |
| CX-013 | kill-switch read failure returns the **last-known** state or **fails closed** (`kill_state_unavailable`); a known-enabled kill can never read as off. | `backend/lambda_function.py` |
| CX-014 | every licence / config mutation writes a durable `<action>.attempt` row **before** it commits and is refused (`503 audit_unavailable`) if it cannot. | `backend/lambda_function.py` |
| CX-002 | `charge.refunded` (full) and `charge.dispute.created` revoke through the existing `/api/bot/chargeback` (partial refunds recorded, not actioned — owner policy). Note: `handle_bot_provision` tags every Stripe row `source: "gumroad"` (legacy name), so the route finds them. | `website/src/worker.js` |
| GM-009 / CX-003 / CX-021 | profile page `$19.99/month`; Discord Worker "7-day trial". | `website/src/worker.js`, `discord_launch/orion_worker.js` |

Left as reported, deliberately: CL-005 (a genuinely dead sender still ends the session — that is the
right escalation and it triggers the launcher's relaunch; the copy-error paths are Astra's tested
fail-closed choice), CL-008 (owner: gate), CL-009/016/017 (Astra), CL-010's fork coalescer half,
CL-011/012/013/015/018-025, CX-008/015/016/017/018/019, GM-001 (downgraded), GM-005 (policy),
GM-007/008/010-019. Re-verdict after the owner's chord ×20 + sprint-release ×20 and Codex's pass.
