# Surface B — bot controller / AutomationEngine

Red-team pass, 2026-09-21 owner session (`logs/orion_native.log`, 25,634 lines,
2026-09-22T03:05Z–03:27Z plus the 18:57/20:52 excerpts).
Scope: `native_orion/src/AutomationEngine.{h,cpp}`, `PreciseFirePolicy.h`,
`ShotGateProtocol.h`, `OnsetFeedforward.h`, `LeadCalibrationPolicy.h`, the automation
hooks in `OrionAppController.cpp`, and the seams where those meet the fork
(`C:/Users/aaron/Desktop/chiaki-ng-src`, branch `orion`, dirty tree).

Source was read only. Nothing was built, launched, edited or committed.

---

## Verdict on the two owner bugs

**Bug (1) "TRIANGLE + CIRCLE makes Venice disconnect" — root cause IDENTIFIED, confidence
HIGH for the mechanism, MEDIUM for the exact trigger.**

The "disconnect" is `chiaki_session_stop()` called from the fork's bridge because a
*flagged* (MustDeliver) pipe transaction did not reach local UDP acceptance inside a
25 ms budget:

```
chiaki-ng-src/gui/src/orioninputbridge.cpp:20   ORION_LOCAL_DELIVERY_TIMEOUT_MS = 25
chiaki-ng-src/gui/src/orioninputbridge.cpp:280-291  wait_orion_delivery -> fail_transport("local_delivery_timeout_or_mismatch")
chiaki-ng-src/gui/src/orioninputbridge.cpp:143-164  fail_transport -> stop_ = true; chiaki_session_stop(session_)
chiaki-ng-src/gui/src/orioninputbridge.cpp:339      owner_pipe_disconnected -> same fail_transport
```

The heartbeat proves the timeout, not a write error:

```
22269:2026-09-22T03:11:13.072Z Input hook heartbeat: connected=0 writes=8977 failures=0
      ack_failures=1 ack_winerr=31 ack_stage=128 ack_error=15 ...
```

`ack_stage=128` is the `Failed` ack stage; `ack_error=15` is `CHIAKI_ERR_TIMEOUT`
(`chiaki-ng-src/lib/include/chiaki/common.h:42-57`, 0-based index 15). `failures=0` — the
native never failed a write. The console link was killed by a *late* transaction.

Surface B's contribution is **B-1** below: the input release-repair duplicate
(`OrionAppController.cpp:13668-13716`) fires a **fresh MustDeliver transaction 24 ms after
every digital release edge on the OUTPUT**, including the player's own pass-through
releases. Two face buttons released together, or in quick succession, produce several
MustDeliver transactions on top of the fork's own 3-entry-per-pure-release redundancy
(`chiaki-ng-src/lib/src/feedbacksender.c:243-302`, 64-entry queue,
`lib/include/chiaki/orioninput.h:22`). Each MustDeliver **blocks the bridge's reader thread**
for up to 25 ms; the burst is visible in the heartbeat exactly at the incident:

```
22208:03:11:11.066Z  writes=8934 ack_seq=8934        (steady state ~1 write/s)
22212:03:11:12.068Z  writes=8961 ack_seq=8939 ack_wait_us=0 ack_attempted=0
22269:03:11:13.072Z  writes=8977 connected=0 ack_winerr=31 ack_stage=128 ack_error=15
```

27 writes in one second, then 16 more, with the ack stream 22 sequences behind — then the
pipe is gone. Every later incident repeats the shape, and twice the repair duplicate is the
*last* thing the native did before the route flipped:

```
24514:03:22:22.817Z  Input release-repair duplicate FAILED (result=5)   <-- WrittenUnconfirmed
24515:03:22:22.818Z  Controller route changed before automation process: generation=17 ...
24568:03:22:37.423Z  Input release-repair duplicate FAILED (result=5)
24569:03:22:37.424Z  Controller route changed before automation process: generation=18 ...
```

`result=5` is `InputRouteWriteResult::WrittenUnconfirmed` (`PreciseFirePolicy.h:55-68`): the
repair's own ack never came back. The other eight repair lines are `result=0` (`Failed`),
i.e. the pipe was already gone — those are the *symptom*. The two `result=5` lines are the
repair racing the teardown.

What is NOT established: that Triangle+Circle *specifically* is the trigger. There is no
combo handling anywhere (confirmed by grep), and the log carries no per-button trace. The
mechanism is rate-of-flagged-edges, and Triangle+Circle pressed together is simply a cheap
way for a human to produce two near-simultaneous pure releases.

**Bug (2) "R2 clamps down for no reason" — root cause NOT proven. Two engine-side
candidates, confidence MEDIUM.**

The engine did **not** shape R2 in the cited excerpts. `grep -ac "SPRINT RELEASED FOR SHOT"`
and `grep -ac "R2 HELD THROUGH PRESS"` both return **0** over the whole 8 MB log, and every
`Physical shot epoch:` line carries `sprint_released=0`. The `r2=255` on the `Release tick:`
lines is `output.r2` (`OrionAppController.cpp:11712-11724`) passing a physically-held trigger
straight through. Both R2 latches
(`AutomationEngine.cpp:4680-4690`, the engine's only writer of `output.r2`) were inert.

The two live candidates are **B-1** (a MustDeliver re-assert of a *stale* `last_` packet can
push R2 back up at the console after the player released it) and **B-3** (the scheduled-fire
release packet replays the pad's trigger byte frozen at arm time). Neither can be confirmed
from this log because no line joins `output.r2` to a *delivered* packet outside the
`Square-down delivery identity` audit. See B-4's verification test for the instrument that
would settle it in one session.

---

## Findings, most severe first

### B-1 — HIGH — The release-repair duplicate re-asserts a possibly-stale pad state as a blocking MustDeliver transaction, on every player button release

**Path.** `OrionAppController.cpp:13473-13477` computes `outputDigitalReleaseEdge` from
`PreciseFirePolicy::controllerDigitalReleaseEdge` (`PreciseFirePolicy.h:213-226`), which fires
on **any** button up, **any** d-pad direction lost, **any** L2/R2 fall to zero, and touchpad
release — the player's own inputs included, not just the bot's Square. `:13668-13674` then
arms `hookReleaseRepairDueMs_ = now + 24 ms` (`OrionAppController.h:2239`). At `:13685-13716`
the repair calls `OrionInputClient::reassertLastState()` (`OrionInputClient.cpp:466-500`),
which sends `last_` as a **fresh MustDeliver transaction**.

Two defects compound:

1. **The re-asserted state can be stale.** `sendDetailed()` deliberately does not update
   `last_` when the write returns `Failed` or `WrittenUnconfirmed`
   (`OrionInputClient.cpp:435-440`). After any ambiguous write, `last_` is the state *before*
   it. A reassert then re-publishes that older state. Chiaki's feedback **state** datagram
   carries `l2_state`, `r2_state` and both sticks (buttons travel only as history events — the
   fork says so itself at `lib/src/orioninput.c:67-69`), so a stale re-assert **can put R2
   back up at the console** while it cannot manufacture a phantom button press.
2. **It is a blocking transaction on the bridge's reader thread.** `flags != 0` ⇒
   `ack_required` ⇒ the bridge waits up to 25 ms inside its `ReadFile` loop
   (`orioninputbridge.cpp:260-291`) before it can consume the next pipe packet. During a
   button burst this is head-of-line blocking on the one thread that drains the pipe.

Also: the due time is **never cancelled**, only overwritten. The gate at `:13686-13692`
(`pendingSubmitSeq_ < 0 && scheduledFireDeadlineMs() < 0 && lastArmedFireToken_ == 0`)
*postpones* the repair through a whole release window, so a repair scheduled at the bot's
release edge can fire ~250 ms later, against a pad the player has since changed.

**Affected.** `native_orion/src/OrionAppController.cpp:13473-13477`, `:13668-13716`;
`native_orion/src/OrionInputClient.cpp:429-440`, `:466-500`;
`native_orion/src/PreciseFirePolicy.h:213-226`.

**Impact.** (a) Contributes the extra flagged transactions that starve the bridge's 25 ms
delivery budget → `local_delivery_timeout_or_mismatch` → `chiaki_session_stop` → the owner's
"disconnect". (b) Can re-assert a released trigger at the console → the owner's "R2 clamps
down". Release blocker for the beta: the failure is a full session kill, not a degraded shot.

**Reproduction (high level).** With a live session and the bot idle, mash four face buttons
for ~2 s. Watch `writes=` in the heartbeat climb well past the ~1/s idle rate and
`ack_seq` fall behind `writes`; the session drops within a few seconds. The owner's session
did this ten times in 11 minutes.

**Hypothesis, supporting evidence.** 10× `Input release-repair duplicate FAILED`, 10 route
changes, 8 recoveries, all inside 11 minutes with **no shot activity at the first incident**
(log 22195–22235 between 03:11:04 and 03:11:12 contains no `Physical shot epoch`, no
`Release tick`, no `Shot state`). Two incidents have the repair's own `result=5` *one
millisecond before* the route change. `failures=0` throughout — the native's writes
succeeded; the *latency* is what killed the link.

**Evidence that would refute it.** A bridge log (`Orion transport: ack_local_delivery ...
queue_wait_us=`) showing the timed-out transaction was a bot Square-release rather than a
repair duplicate; or a repeat of the burst with `ORION_INPUT_RELEASE_REPAIR` disabled that
still drops the session. The fork's own log is not in this packet, so this remains
mechanism-proven and trigger-inferred.

**Minimal fix.**
1. Restrict `outputDigitalReleaseEdge` to controls the engine actually owns (Square, and the
   right stick under a Tempo/Go-To release) instead of the whole pad. A pass-through button
   the engine never touched needs no *engine* repair — the fork's own twin+echo already
   covers it (`lib/src/orioninput.c:64-82`, `lib/src/feedbacksender.c:243-302`).
2. Rate-limit the repair to at most one in flight and at most one per N ms (N ≥ 100), and
   **cancel** a pending repair when a newer real write lands, instead of only postponing it.
3. Make `reassertLastState()` refuse to send when `last_` is older than the newest observed
   pad sample — re-assert the *current* state or nothing.

**Verification test.** `AutomationEngineTests.cpp` cannot reach `OrionInputClient`, so the
first two parts belong in a new pure-policy seam. Sketch:

```cpp
// PreciseFirePolicyTests: the repair edge must be OWNED-control-only.
void releaseRepairEdgeIgnoresUnownedButtons()
{
    ControllerState prev{}; prev.buttons = XINPUT_GAMEPAD_Y | XINPUT_GAMEPAD_B; // triangle+circle
    ControllerState now{};                                                      // both released
    QVERIFY(!PreciseFirePolicy::ownedDigitalReleaseEdge(prev, now));   // new predicate
    prev = {}; prev.buttons = XINPUT_GAMEPAD_X; now = {};
    QVERIFY(PreciseFirePolicy::ownedDigitalReleaseEdge(prev, now));    // Square still repairs
}
```

---

### B-2 — HIGH — `inputRecoveryStarted` resets the engine and closes the pipe without neutralizing owned output; the abort-into-drain is erased before it can run

**Path.** `OrionAppController.cpp:2668-2695`:

```
2674   inputRouteAwaitingRecovery_ = true;
2676   automation_.setArmed(false);
2677   automation_.reset();
...
2687   orionInput_.resetConnection();        // closePipe(); haveLast_ = false
```

`AutomationEngine::reset()` (`AutomationEngine.cpp:5056-5140`) wipes `shot_` to a fresh
`ShotContext{}` (Idle) at `:5072` **and** calls `clearOwnedOutputDrain()` at `:5140`. The
engine's own disarm handler — the code that converts a live owned gesture into a
`ReleasedUntilPhysicalEnd` drain or an `automation_disarmed_abort`
(`AutomationEngine.cpp:4890-4911`, `:4912-4936`) — runs on the **next** `process()` tick, and
by then the context is already Idle, so it latches nothing and aborts nothing.
`OrionInputClient::resetConnection()` (`OrionInputClient.cpp:154-166`) closes the handle with
no write. The ordered `own=0` barrier is never sent.

Every sibling teardown pairs `automation_.reset()` with `neutralizeOwnedInput()`
(`OrionAppController.cpp:6071-6105`): see `:4223`, `:4821`, `:5116`, `:5295`, `:6114`,
`:6199`, `:12539`. The in-tick resets at `:13031` and `:13087` are covered by
`failClosedNeutralThisTick`. **`:2677` is the only reset with neither.**

**Affected.** `native_orion/src/OrionAppController.cpp:2668-2695`;
`native_orion/src/AutomationEngine.cpp:5056-5140`, `:4890-4936`;
`native_orion/src/OrionInputClient.cpp:154-166`.

**Impact.** Whatever the console last saw stays latched: a bot-held Square mid-hold, a
Tempo gather stick, or a trigger the engine shaped. No release edge, no drain, no
`SHOT NOT OWNED` census line, no `Owned input cleanup:` line. This is a *stuck ownership*
path by construction, and it is on the exact code path taken ten times in the owner's
session (`22232:03:11:12.720Z Input-only recovery started: controller/fire authority
revoked`).

**Reproduction.** Hold Square so the bot owns the shot; force an input-readiness loss
(`Remote Play input readiness was lost` — log line 22230, 03:11:12.672Z) during the hold.
The log will show `Input-only recovery started` with no preceding `Owned input cleanup:` and
no `Release ownership:` line for that shot.

**Hypothesis, supporting evidence.** The asymmetry against seven sibling call sites is
textual and unambiguous. In the owner's session the engine happened to be Idle at every
incident (no shot in flight — see B-1's refutation R3), so no stuck Square was produced;
that is luck, not a guard.

**Evidence that would refute it.** A path I did not find that neutralizes the route between
`RemotePlaySession::inputRecoveryStarted` being emitted and this slot running. I searched
`neutralizeOwnedInput`, `releaseStaleSquareOutputLocked` and `resetConnection` call sites and
found none on this path. Also partly mitigating: in practice the pipe is usually *already*
dead when this fires (`connected=0` in the heartbeat one second later), so the neutral would
fail anyway — but `inputRecoveryStarted` is also emitted on a promotion/readiness timeout
with the pipe still live, and that is the case this leaves unguarded.

**Minimal fix.** In the slot, before `orionInput_.resetConnection()`:

```cpp
automation_.setArmed(false);
neutralizeOwnedInput();          // <-- add: releases Square, submits neutral, own=0 barrier
automation_.reset();
```

`neutralizeOwnedInput()` already takes `submitMutex_` and already no-ops when the route is not
owned, so the ordering is the only change. (Keep `reset()` after it so the drain/abort
handler is not needed at all.)

**Verification test.**

```cpp
// AutomationEngineTests: a reset must not silently swallow an owned gesture.
void resetDuringOwnedHoldLeavesNoStrandedOwnership()
{
    AutomationEngine engine; ControllerState physical;
    driveToHolding(engine, physical);
    QCOMPARE(engine.context().state, HoldState::Holding);
    engine.setArmed(false);
    // The disarm handler must be given a tick BEFORE reset(), or the drain is lost.
    const ControllerState out = engine.process(physical);
    QVERIFY(!out.square());                                  // owned Square released
    QVERIFY(engine.ownedOutputDrainActiveForTest());         // drain latched, not erased
    engine.reset();
    QCOMPARE(engine.context().state, HoldState::Idle);
}
```

---

### B-3 — MEDIUM — The scheduled-fire release packet freezes the *whole* pad at arm time and replays it at the deadline; only the blind InputTimed token is refreshed

**Path.** `OrionAppController.cpp:13230` copies this tick's `output` into `fireOutput`,
`applyShotReleaseEdge()` (`ShotReleasePolicy.h:85-125`) touches **only** Square and the right
stick, and the worker sends that frozen packet at the deadline
(`OrionAppController.cpp:1612`, `:1715-1731`). `isValidShotReleaseOutput()` (`:1696-1706`)
validates only Square and the right stick. L2, R2, the d-pad and every other face button are
replayed exactly as they were when the token was armed.

The refresh that would fix this exists but is gated:

```
13197   if (automation_.scheduledFireIsBlindInputTimed()) {
13211       fireThread_->refreshReleaseOutput(schedToken, refreshContext.mode, refreshOutput, refreshStyle);
```

with the comment "Meter/pose/vision tokens are untouched: they arm inside their own horizon
and are re-armed, not refreshed, when anything changes" — but "anything changes" means the
*deadline*, not the pad.

**Affected.** `native_orion/src/OrionAppController.cpp:13188-13215`, `:13228-13245`, `:1610-1706`;
`native_orion/src/ShotReleasePolicy.h:85-125`.

**Impact.** A trigger or button the player changes between arm and fire is inverted for one
packet at the release instant: a released R2 is re-pressed (a phantom sprint input exactly at
the shot), or a freshly-pressed Circle/Triangle is cancelled and then re-pressed by the next
GUI tick — a genuine press-release-press glitch on a real button. Because a re-assert is a
trigger *increase*, `chiaki_orion_input_is_pure_button_release`
(`lib/src/orioninput.c:73-81`) refuses to copy it, so it gets no redundancy; the corrective
release that follows does. Normally self-healing within one tick.

**Bound (honest).** Most tokens are re-armed on every sidecar payload (~16.7 ms), and only an
*imminent* token is protected from re-arming — `imminentTokenWindowMs()`
(`AutomationEngine.cpp:13709-13727`) is about one frame gap. So the usual staleness is
≤ ~17 ms. The exception is a token that deliberately rides an evidence gap:
`autonomousVisionScheduleLeaseCurrent()` (`AutomationEngine.cpp:17700-17716`) and
`armedPhaseTokenCarriesAnchorAuthority()` keep a token alive through a detector blink for the
whole lease, and `adaptiveAutonomousSchedulerHorizonMs()` (`:17146`) clamps the horizon to
[22, 135] ms. Worst case is therefore ~135 ms of frozen trigger state.

**Reproduction.** Hold R2, press Square, release R2 during the meter's rise, and watch the
`Square-down delivery identity: ... delivered_r2=` field for the *release* transaction. It
will report the arm-time value, not 0.

**Hypothesis, supporting evidence.** Purely structural — the code path is unambiguous, and
the blind-token refresh exists precisely because someone already noticed the class.

**Evidence that would refute it.** A per-fire delivered-packet log showing `r2` always
matching the pad at the deadline. The current `Square-down delivery identity` line only
covers the *press*, not the release, so the log cannot decide this either way today.

**Minimal fix.** Move the `refreshReleaseOutput()` call out of the
`scheduledFireIsBlindInputTimed()` branch and apply it to every armed token, re-deriving
`fireOutput` from this tick's `output` each tick the token stays armed. The deadline and the
route binding are untouched by `refreshReleaseOutput` (`PreciseFirePolicy.h:36-47` governs
retarget, not the packet), so the timing contract does not change.

**Verification test.**

```cpp
void scheduledReleasePacketTracksTheLivePadNotTheArmInstant()
{
    // Arm a meter token with R2 held, then release R2 before the deadline.
    // The packet handed to the worker must carry r2 == 0.
    ControllerState physical; physical.r2 = 255;
    driveToArmedMeterToken(engine, physical);
    const quint64 token = engine.scheduledFireToken();
    physical.r2 = 0;
    engine.process(physical);
    QCOMPARE(static_cast<int>(engine.scheduledReleaseOutputForTest(token).r2), 0);
}
```

---

### B-4 — MEDIUM — The stale-output watchdog is Square-only; a stranded trigger or face button has no recovery path at all

**Path.** `squareOutputWatchdog_` (`OrionAppController.cpp:13455-13472`) observes only the
Square bit and can only call `releaseStaleSquareOutputLocked()` (`:6051-6069`), which clears
`1u << 2` and nothing else. `OrionInputClient::releaseSquareForWatchdog()`
(`OrionInputClient.cpp:443-464`) likewise masks only the Square bit.
`observeOutputDivergence()` (`OrionAppController.cpp:11745+`) watches L2/R2 and every
non-Square button — but it **only logs**, and it logged nothing in this session
(`grep -a -i diverg` over the whole log: 0 hits).

**Affected.** `native_orion/src/OrionAppController.cpp:6051-6069`, `:13455-13472`, `:11745+`;
`native_orion/src/OrionInputClient.cpp:443-464`.

**Impact.** This is the *other half* of bug (2): even if something (B-1 or B-3) strands R2 at
the console, nothing in the native will ever correct it. The player's only recovery is to
produce another trigger edge themselves. Correspondingly the log carries no evidence either
way, which is why bug (2) cannot be closed from this session.

**Refuting note worth recording.** The evidence packet states the fork's redundancy covers
"pure button releases only — triggers are analog fields l2/r2". That is **wrong**:
`chiaki_orion_input_is_pure_button_release` explicitly includes the transition to rest for
both triggers (`chiaki-ng-src/lib/src/orioninput.c:75-81`, `trigger_released`). So a genuine
R2 *release* is already protected by the 3 ms twin and the 40 ms echo. That strengthens the
case that a stuck R2 is a *re-assert* (B-1 / B-3), not a lost release datagram.

**Minimal fix.** Generalise the watchdog to a per-control stale-output check driven by
`PreciseFirePolicy::controllerDigitalControlsAtRest()` (`PreciseFirePolicy.h:204-209`, which
already covers `buttons`, `dpad`, `l2`, `r2` and `touchpad`): when the physical pad has been
at rest for N consecutive polls and the last confirmed pipe packet is not, send one forced
neutral MustDeliver and log it.

**Verification test.** Pure policy, no thread:

```cpp
void staleOutputWatchdogCoversTriggersNotJustSquare()
{
    ControllerState physical{};              // fully at rest
    ControllerState confirmed{}; confirmed.r2 = 255;
    QVERIFY(PreciseFirePolicy::controllerDigitalControlsAtRest(physical));
    QVERIFY(!PreciseFirePolicy::controllerDigitalControlsAtRest(confirmed));
    QVERIFY(staleOutputReleaseDue(physical, confirmed, /*restPolls=*/3));
}
```

---

### B-5 — MEDIUM — "physical shot controls were neutral" is a Square+right-stick-only predicate; the recovery gate's log line overstates what it proved

**Path.** `routeRecoveryShotInputsNeutral()` (`ControllerRoutingPolicy.h:209-216`) forwards
straight to `shotInputControlsNeutral()` (`ShotIntentPolicy.h:75-84`), which tests only
`!state.square()` and the right-stick radius. L2, R2, the d-pad and every other button are
ignored. This feeds `preciseFireRecoveryNeutralFrames_` (`OrionAppController.cpp:13061-13074`)
and therefore `PreciseFirePolicy::routeRecoveryReady()` (`PreciseFirePolicy.h:398-407`) and
the line the owner reads:

```
22918:03:14:43.657Z  Direct controller pipe recovered: forced write accepted after physical
                     shot controls were neutral; fail-closed gate cleared.
```

Two stricter predicates already exist beside it and are not used here:
`controllerStateFullyNeutral()` (`PreciseFirePolicy.h:190-197`) and
`controllerDigitalControlsAtRest()` (`:204-209`).

**Affected.** `native_orion/src/ShotIntentPolicy.h:75-84`;
`native_orion/src/ControllerRoutingPolicy.h:209-216`;
`native_orion/src/OrionAppController.cpp:13061-13074`, `:13926-13932`.

**Impact.** The re-seed after a pipe recovery happens while a trigger is held. The
recovery probe itself writes the *live* pad, so it does not by itself strand R2 — but the
3-neutral-frame fence, whose whole purpose is "`AutomationEngine::reset` cannot re-arm a
still-held gesture as a second takeover" (`PreciseFirePolicy.h:394-397`), cannot see a trigger
at all, and the log line claims more than it checked. Low exploitability; real diagnostic
cost — this is exactly why the R2 reports are unresolvable from the log.

**Reproduction.** Hold R2 through a pipe-recovery cycle; the `Direct controller pipe
recovered ... physical shot controls were neutral` line still prints.

**Hypothesis, supporting/refuting.** Supporting: the predicate is three lines long and
unambiguous. Refuting the *severity*: the comment at `PreciseFirePolicy.h:199-203` is a
deliberate design note explaining why the *stick* is excluded from the release-repair
at-rest test — so narrowing is intentional there. It is not documented as intentional for
the route-recovery gate, and the two paths use different predicates today.

**Minimal fix.** Have `routeRecoveryShotInputsNeutral()` require
`shotInputControlsNeutral(...) && PreciseFirePolicy::controllerDigitalControlsAtRest(state)`,
or, minimally, add `&& state.l2 == 0 && state.r2 == 0` and change the log line to name what it
checked.

**Verification test.**

```cpp
void routeRecoveryNeutralRefusesAHeldTrigger()
{
    ControllerState s{}; s.r2 = 255;             // Square up, sticks centred, R2 buried
    QVERIFY(!routeRecoveryShotInputsNeutral(s, 40.0, 40.0));
}
```

---

### B-6 — MEDIUM — `scheduleFire()` enforces the controller-route binding only for `InputTimed`; every other authority can mint a token with `generation=0`, which then costs a full engine reset

**Path.** `AutomationEngine.cpp:17293-17295` snapshots the attestation, but the validity check
at `:17324-17328` is inside an `authority == ScheduledFireAuthority::InputTimed` branch:

```
17324   if (authority == ScheduledFireAuthority::InputTimed
17325       && (!inputTimedAuthorityCurrent(now)
17326           || std::abs(deadlineMs - noMeterBackstop_.deadlineMs) > 1e-6
17327           || (controllerRouteBindingRequired_ && !routeAttestation.valid()))) {
17328       return rejectGate(ArmGate::NoAuthority);
```

A `MeterVision` / `AutonomousMeterVision` / `Pose` token therefore stores
`schedFireRouteGeneration_ = 0`, `schedFireRoute_ = None` (`:17603-17604`) whenever the
attestation is momentarily invalid — which includes the whole window between a route change
(`OrionAppController.cpp:12976-12985`, `setControllerDeliveryRouteAttestation(0, None)`) and
the next successful neutral re-proof.

**Affected.** `native_orion/src/AutomationEngine.cpp:17293-17295`, `:17324-17328`, `:17600-17605`;
`native_orion/src/OrionAppController.cpp:1656-1690`, `:12996-13040`.

**Impact.** Fail-closed is preserved — the worker's submit-time check rejects it
(`PreciseFireRouteBinding::valid()`, `PreciseFirePolicy.h:86-89`; worker at
`OrionAppController.cpp:1660-1688`) — but the *cost* of the rejection is severe and
disproportionate: `failed_ = true` → `takeFailure()` → `preciseFireDeliveryFault_ = true`,
`automation_.setArmed(false)`, `automation_.reset()` and a forced neutral tick
(`OrionAppController.cpp:12996-13040`). A token that could have been refused for free at arm
time instead destroys the shot *and* resets the engine *and* opens the recovery gate. This is
a loss amplifier on exactly the flap the owner hit.

**Reproduction.** Drive a route change while a meter token is being armed; the log will show
`Precise release NOT_SUBMITTED: token=... route_binding_changed` rather than a clean
`SCHEDULE FIRE REJECT: gate=NoAuthority`. (Not present in this session's log — the route
changes all landed on an idle engine.)

**Hypothesis, supporting/refuting.** Supporting: the gate is textually InputTimed-only.
Refuting the *severity*: `controllerDeliveryRouteAttestationSnapshot()` returns `{}` only when
`!armed_` or the attestation was revoked (`AutomationEngine.h:3617-3633`), and `scheduleFire`
already refuses when `!armed()` (`:17288-17290`), so the window is narrow — a revoked-but-
still-armed tick. Also, `setControllerDeliveryRouteAttestation` already fences an *existing*
armed token (`AutomationEngine.cpp:14591-14598`); the gap is only for a token armed *after* the
revoke and *before* the re-proof.

**Minimal fix.** Hoist the route check out of the InputTimed branch:

```cpp
if (controllerRouteBindingRequired_ && !routeAttestation.valid()) {
    return rejectGate(ArmGate::NoAuthority);
}
```
placed immediately after `:17295`, before the horizon arithmetic.

**Verification test.**

```cpp
void everyAuthorityRefusesToArmWithoutARouteAttestation()
{
    engine.setControllerRouteBindingRequired(true);
    engine.setControllerDeliveryRouteAttestation(0, LatencyControllerRoute::None);
    driveToHolding(engine, physical);
    feedGenuineMeterFrames(engine);                       // would normally arm
    QCOMPARE(engine.scheduledFireDeadlineMs(), -1.0);     // no token at all
    QCOMPARE(engine.scheduledFireRouteGeneration(), 0ull);
}
```

---

### B-7 — LOW/MEDIUM — Onset feedforward: aim bucket and teach bucket can differ, and the staleness fence is not config-bound

**Path.**
1. `scheduleFire()` picks the bucket from the **arm-time** type:
   `AutomationEngine.cpp:17477` `bucket = BannerLeadTrim::bucketFor(shot_.shotType)`.
   `noteOnsetFeedforwardRelease()` writes the ring using the **release-time** type:
   `:23637-23640` → `:15257-15271`. `ShotGateProtocol.h:64-68` documents the 200 ms blind type
   grace that upgrades `Standstill` → a fade mid-press, so a shot regularly aims off one
   bucket's median and teaches another.
2. `OnsetFeedforwardLimits` is built from config for gain/clamp/window/minSamples/oneSided
   (`:17471-17476`) but `staleMs` is left at the struct default
   (`OnsetFeedforward.h:171`, 600 000 ms), and `observe()` is called with
   `orion::OnsetFeedforwardLimits{}.staleMs` (`:15269-15271`). The 10-minute context fence is
   therefore not owner-tunable and not covered by any settings round-trip test.
3. `noteContextChanged("court_changed")` (`:15239-15255`) resets the ring but does **not**
   touch a token already armed off the old court's reference, and its `dropped_onsets` count
   hard-codes four bucket names (`Standstill`, `Left Fade`, `Right Fade`, `Other`) while
   `bucketFor()` is the authority on the set.

**Affected.** `native_orion/src/AutomationEngine.cpp:15239-15271`, `:17470-17520`, `:23637-23640`;
`native_orion/src/OnsetFeedforward.h:163-232`.

**Impact.** Small, bounded by design — the clamp is 10 ms and the trim bound at `:17491-17497`
caps the sum with the banner trim. Bucket skew costs accuracy in the reference median, not
safety. The hard-coded `staleMs` is a config/behaviour divergence risk if a future owner knob
is added.

**Not a defect (checked and refuted).** The ring is fed for *every* release regardless of
whether that release was displaced by the feedforward, the dev sweep or the hold band. That
looked like a feedback loop, but `liveShotOnsetMs()` (`:15154-15161`) is
`firstMeterSeenMs - physicalPressMs` — strictly upstream of the fire — so a displaced fire
cannot move the signal that aimed it. No poisoning.

**Minimal fix.** (a) Stamp the bucket into `ShotContext` at arm and teach that same bucket at
release, or (equivalently, and cheaper) re-read `bucketFor(shot_.shotType)` at release and
accept the release-time bucket as canonical *and* make `decide()` read it at the same point —
one of the two, not both. (b) Add `onsetFeedforwardStaleMs` to `RemapConfig` and pass it at
both sites. (c) Derive the `dropped_onsets` count from the ring's own keys.

**Verification test.** Extend the existing
`onsetFeedforwardIsBoundedByTheBannerTrimAndResets()`:

```cpp
void onsetFeedforwardAimsAndTeachesTheSameBucketAcrossATypeUpgrade()
{
    // Arm as "Standstill", upgrade to "Left Fade" inside the 200 ms grace, release.
    // The onset must land in exactly the bucket the aim consulted.
    QCOMPARE(engine.onsetFeedforwardSamplesForTest("Left Fade"), 1);
    QCOMPARE(engine.onsetFeedforwardSamplesForTest("Standstill"), 0);
}
```

---

### B-8 — INFORMATIONAL (checked, benign) — `SQUARE SUPPRESSED: gate=engine:Holding/release_scheduled`

The suppression census over the session:

```
71  engine:Releasing/release_scheduled
71  engine:Idle/waiting_for_button_release
71  engine:Idle/Idle
71  engine:Cooldown/release_scheduled
 4  engine:Holding/release_scheduled
 2  engine:Idle/stabilizing_tempo_movement_intent
 1  engine:Idle/waiting_for_tempo_movement_commit_ack
 1  engine:Idle/waiting_for_genuine_meter_before_ownership
```

Four ticks report *output Square up while the engine still says Holding* — the exact
signature of a release that beat its own state machine. It is deliberate:
`OrionAppController.cpp:13444-13452` converts this tick's held output to the release edge when
`fireThread_->firedUnconsumed()`, precisely so the GUI tick does not re-press on top of the
worker's fire ("re-pressing on top of the fire is exactly the pump-fake artifact"). 4 in 75
releases is the expected rate for a fire that lands after `process()`.

All 75 `Release ownership:` lines carry `out_cleared_all=1` — no engine override of the
release. The 4 with `phys_held_all=0` all have `phys_release_t_ms=0`, i.e. the player was
already off the button at the first tick of the window, not a mid-window race.

---

## Goal 2 — what happens to a SCHEDULED fire when the pipe closes mid-shot

**Answer: it is DROPPED, not replayed and not held. The fail-closed chain is correct and
triple-redundant. The physical release that happens during the fallback is honoured on the
new route by the ordinary mirror, but the *bot's* release for that shot is lost, and the cost
is a full engine reset.**

The chain, in the order it runs:

1. **Pre-`process()` revoke.** `OrionAppController.cpp:12963-12985`. The tick recomputes
   `PreciseFirePolicy::liveControllerRoute()` (`PreciseFirePolicy.h:163-176`) — with
   `orionInput_.connected()` now false it returns `VigemXusb` — sees the mismatch against the
   attestation, and calls `latencyCacheRouteAttestation_.revoke()` +
   `automation_.setControllerDeliveryRouteAttestation(0, None)`. This is the log line:

   ```
   22212:03:11:12.550Z Controller route changed before automation process:
                       generation=2 attested=1 live=3; scheduled authority revoked.
   ```
   `attested=1` is `LatencyControllerRoute::Pipe`, `live=3` is `VigemXusb`.

2. **The engine fences the armed token synchronously.**
   `AutomationEngine::setControllerDeliveryRouteAttestation` (`AutomationEngine.cpp:14584-14606`)
   calls `invalidateUnconfirmedSchedule(false, "route_reattestation")` (`:17628-17673`)
   *before* publishing the new attestation. That emits `visionScheduleInvalidating(token)`,
   which is **direct-connected** to the controller and does not return until the worker's
   copied token is disarmed or confirmed. So the fire thread cannot still be holding a
   Pipe-bound deadline after this line.

3. **The worker's own submit-time guard (belt and braces).**
   `OrionAppController.cpp:1656-1688`. Under both `submitMutex_` and `m_`, immediately before
   touching either route, it re-checks `automation_.scheduledFireRouteBindingMatches()` and
   `PreciseFirePolicy::routeBindingMatches()` (`PreciseFirePolicy.h:178-188`). On mismatch it
   `setArmed(false)`, submits **neutral** to ViGEm, sends a neutral owned packet down the pipe
   if the route is still owned, and publishes
   `failed_ / failedDetail_ = "route_binding_changed generation=N bound=B live=L"`.
   It never writes the release output on the new route.

4. **The GUI mirror refuses to replay it either.** `OrionAppController.cpp:13503-13538`.
   `routeBoundRelease && !precisionBindingMatches` ⇒ `precisionRouteRejected`, the output is
   forced to a full neutral, `releaseStaleSquareOutputLocked("controller_route_changed")`
   runs, and `:13826-13831` logs
   `Precision release route rejected: ... release was not replayed on the live route.`
   (Zero occurrences in this session's log — consistent with the engine being idle at every
   route change.)

5. **The failure is consumed on the next tick and costs the whole engine.**
   `OrionAppController.cpp:12996-13040`: `preciseFireDeliveryFault_ = true`,
   `automation_.setArmed(false)`, `cancelPostReleaseGrade()`, `automation_.reset()`,
   `failClosedNeutralThisTick = true`, lifecycle → `ControllerFault`, and
   `Precise release NOT_SUBMITTED: token=... route_binding_changed`.

**Is a physical release during the fallback honoured on the new route?** Yes, but only as an
ordinary mirror write. `failClosedNeutralThisTick` forces a full neutral immediately
(`:13508-13511`), and thereafter `hookOwnsInput` is false so `virtualOutput = output` goes to
ViGEm (`:13583-13600`). The player's Square-up is delivered on XUSB. What is *not* delivered
is the bot's timed release: the shot is aborted, the grade is cancelled
(`automation_.cancelPostReleaseGrade(pendingSubmitSeq_)`), and the user sees
`Release not submitted` / `Precise release NOT submitted; controller route recovery required`.
Recovery then waits on `routeRecoveryReady()` — three neutral frames plus an accepted write —
before automation re-arms (`:13920-13940`, `syncEngineArmed()` at `:5766-5768`).

**The 03:22–03:23 flap** (log lines 24324–24625, six route changes in 61 s) shows the same
chain running repeatedly with the engine idle: pipe closes → revoke → isolation →
readiness-failure warnings from the sidecar → `Direct controller pipe recovered` → and within
10–15 s another close. Notably at `24514/24515` and `24568/24569` the *release-repair
duplicate* reports `result=5` (WrittenUnconfirmed) **one millisecond before** the route change
— i.e. the last transaction the native attempted before the link died was a repair duplicate,
not a shot. That is finding B-1.

**Residual gap.** Steps 1–4 are sound. The gap is B-6: because `scheduleFire()` does not
require a valid attestation for meter/pose authorities, a token can be *created* in the
revoked window and then destroyed at step 3, converting a free arm-time refusal into a
full-engine reset.

---

## Goal 3 — can the engine's release marker or release-repair duplicate race the physical release?

**Short answer: a *phantom button press* is impossible; a *phantom trigger re-assert* is
possible; a press-release-press on a real button is possible only through B-3, not through the
repair duplicate.**

Worked through:

1. **The repair cannot manufacture a button press.** `reassertLastState()` re-sends an
   *identical* packet. On the fork side `feedback_sender_apply_state_orion` computes
   `button_edge` against `orion_pending_state` (`lib/src/feedbacksender.c:235-237`) — equal
   state ⇒ no edge; the entry is pushed only because `flagged` is true
   (`:259`), `redundant_history` is `button_edge && (...)` ⇒ **false** (`:255-258`), and the
   dequeue path sets `needs_state` via `force_state = MUST_DELIVER && !history_change`
   (`:946-952`). The result is one extra **feedback STATE** datagram and **no history event**.
   PS5 buttons travel only as history events (the fork says so at
   `lib/src/orioninput.c:67-69`). So no phantom press. **The "release-repair duplicate causes
   a double shot" hypothesis is REFUTED.**

2. **But the state datagram carries the analog triggers.** `ChiakiFeedbackState` carries
   sticks + `l2_state`/`r2_state`. So a repair whose `last_` is stale — and `last_` is
   *deliberately* not advanced past a `Failed`/`WrittenUnconfirmed` write
   (`OrionInputClient.cpp:435-440`) — **can push a released trigger back up at the console**.
   That is finding B-1(1), and it is the strongest surviving candidate for bug (2).

3. **The repair cannot cancel a real release.** It re-sends the *current confirmed* state, and
   the gate at `:13686-13692` postpones it while a release is pending, so it never lands inside
   a release window. Its worst timing failure is landing *after* the window against a pad the
   player has since changed, which is case 2.

4. **The fork's 40 ms echo cannot cancel a later press.** I expected it to replay a stale
   release state; it does not. The `redundant_echo = 2` entry sets only
   `orion_inflight_needs_history = true` and `history_dirty` (`lib/src/feedbacksender.c:930-941`)
   and then re-flushes the *current* history buffer — it never calls
   `feedback_sender_apply_state_locked` with the frozen `entry.state`. The echo is genuinely
   idempotent. **REFUTED.**

5. **The one real press-release-press path is B-3.** The scheduled-fire packet freezes every
   non-shot button at arm time. A button the player *presses* during the token's flight is
   delivered as UP by the fire packet and re-pressed by the next GUI tick — press, release,
   press, all as real history events, all within ~17–135 ms. This is the only mechanism on this
   surface that can produce a genuine double edge on a button the player is holding.

6. **The bot's own release is not at risk from the physical release.**
   `noteOwnedSquareReleaseDelivered()` (`ShotIntentPolicy.h:200-221`) only *shortens* the
   re-arm debounce from three UP reports to two after a confirmed delivery, and
   `noteOwnedSquareReleaseDeliveredForEpoch()` (`:226-233`) epoch-fences it so a queued
   completion from an older shot cannot grant the shortcut. The `LATCH LEAK BROKEN AT FRESH
   PRESS` canary (`AutomationEngine.cpp:7965-7979`) fired **zero** times this session, and all
   71 `waiting_for_button_release` IDLE-GATE episodes cleared into
   `Idle / pass-through sqLatch=0 sqPhys=0` — no sqLatch stall in this log.

---

## Refuted hypotheses (recorded so they are not re-opened)

| # | Hypothesis | Verdict | Evidence |
|---|---|---|---|
| R1 | A scheduled fire is replayed on the ViGEm route after the pipe closes | **Refuted** | Three independent guards: `AutomationEngine.cpp:14591-14598` fences the token; `OrionAppController.cpp:1660-1688` re-checks the binding under both locks and neutrals both routes; `:13503-13538` + `:13826-13831` refuse a GUI replay. |
| R2 | The fork's 40 ms echo replays a stale release and cancels a later press | **Refuted** | `feedbacksender.c:930-941` — the echo sets `needs_history` only and re-flushes the *current* history buffer; the frozen `entry.state` is never applied. |
| R3 | A stale precise-fire packet caused the 03:11:12 write burst | **Refuted** | Log lines 22195–22235 (03:11:04 → 03:11:12) contain no `Physical shot epoch`, no `Release tick`, no `Shot state`. The engine was idle. The burst is physical button activity + repair duplicates. |
| R4 | The engine shaped R2 in the 18:57/20:52 excerpts (sprint-release or R2-hold latch) | **Refuted** | `grep -ac "SPRINT RELEASED FOR SHOT"` = 0 and `grep -ac "R2 HELD THROUGH PRESS"` = 0 over the whole log; every epoch line carries `sprint_released=0`. Those `r2=255` values are pass-through (`AutomationEngine.cpp:4684-4690` is the only writer and both latches were inert). |
| R5 | The onset feedforward poisons its own reference ring via displaced releases | **Refuted** | `liveShotOnsetMs()` (`AutomationEngine.cpp:15154-15161`) is `firstMeterSeenMs - physicalPressMs`, strictly upstream of the fire. A displaced fire cannot move the signal that aimed it. |
| R6 | `sqLatch=1 + waiting_for_button_release` indicates a stuck-ownership leak in this session | **Refuted for this log** | 71 episodes, all clearing to `sqLatch=0 sqPhys=0`; `LATCH LEAK BROKEN AT FRESH PRESS` fired 0 times; all 75 `Release ownership:` lines report `out_cleared_all=1`. |
| R7 | Triggers get no redundancy in the fork, so a lost R2 release explains bug (2) | **Refuted** | `chiaki_orion_input_is_pure_button_release` includes `trigger_released` for both L2 and R2 (`lib/src/orioninput.c:75-81`); an R2 release already gets the 3 ms twin and the 40 ms echo. The packet's note on this point is incorrect. |

---

## What I could not decide from this evidence

- **Whether Triangle+Circle specifically triggers bug (1)**, versus any sufficiently fast
  multi-button release. No per-button trace exists in `orion_native.log`, and the fork's own
  `Orion transport:` log (which carries `edge_queue_overflow`, `queue_wait_us` and the
  per-transaction ack stages) is not in this packet. One session with the fork's log captured
  would settle it.
- **Whether R2 is stranded by B-1 or by B-3.** Both are live; neither leaves a trace today.
  The `Square-down delivery identity` line covers only the *press*. Adding the same
  `delivered_r2=` field to the *release* transaction and to the repair duplicate would
  discriminate them in one session.

---

## Recommended order of work

1. **B-1** (blocker) — scope the release-repair edge to owned controls, rate-limit it, and stop
   re-asserting a stale `last_`. This is the one finding that kills the session.
2. **B-2** (blocker-adjacent) — add `neutralizeOwnedInput()` to the `inputRecoveryStarted` slot.
   Three-line change, removes a whole stuck-ownership class.
3. **B-4** — generalise the stale-output watchdog beyond Square, and add `delivered_r2=` to the
   release audit so bug (2) becomes decidable.
4. **B-3** — refresh the scheduled release packet on every armed tick, not only for the blind
   token.
5. **B-6**, **B-5**, **B-7** — cheap correctness/diagnostic hardening.
