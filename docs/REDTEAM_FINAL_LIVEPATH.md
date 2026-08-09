# Red-team: FINAL live-path verification (capture → detect → time → fire → release)

End-of-cycle adversarial pass. Two jobs: (A) verify this session's four committed fixes
are correct + regression-free; (B) hunt for anything still wrong on the client live path.
**READ-ONLY** — no code changed, native NOT rebuilt (verification is static + the commit's
own "build + 667 tests green" claim). Scope excludes backend/ and the licensing lambdas.

Method note (project rule): every negative below was scrutinized before being asserted, and
each finding is labelled SOFT (timing-dependent / re-testable) vs HARD (deterministic).

---

## PART A — verdicts on the four committed fixes

### FIX 1 — Fade learn-loop closure (fcfa5c3b, learnFromOutcome autonomous branch) → **CONFIRMED-CORRECT**

Nudge: `fadeStep = globalClockGain * errorMs`; `clk = clamp(clk - fadeStep, floor, 5000)` on both
`shotTypeFeedforwardMs[fade]` and `shotTypeMeterToReleaseMs[fade]`
(`AutomationEngine.cpp:4633-4650`), offset left alone.

- **Sign is correct.** A fade's fire deadline on the dominant (feedforward) path is
  `anchor + clock − ffOffset`, with `ffOffset = shotTypeOffsetMs` for a fade
  (`AutomationEngine.cpp:2699-2709`). LATE ⇒ `errorMs>0` ⇒ `fadeStep>0` ⇒ `clk` decreases ⇒
  deadline decreases ⇒ **next fade fires earlier**. Correct (LATE → earlier), not runaway.
- **Stable / gain < 1.** `globalClockGain = 0.12` (`AutomationEngine.h:324`). One outcome moves the
  fired deadline by exactly one 0.12·err step. A GREEN (`|err| ≤ 0.5`) takes the diagnostic reset
  branch (`:4564`) and never reaches the nudge — a well-calibrated fade **cannot drift**; only
  off-green outcomes converge it.
- **No double-count with the offset path.** The autonomous branch returns at `:4655`, so the
  per-type ACQUIRE→LOCK code never runs here; offset is untouched in this branch. The other autonomous
  writers (`learnedLatencyMs`, `shotTypeLatencyMs`) do NOT reach the fade **feedforward** deadline
  (that deadline reads neither) — so for a feedforward-fired fade the clock nudge is the *only* mover.
  Single knob, single step.
- **Gated correctly.** The nudge sits under `leadMayMove` (`:4594-4595`) = authoritative-oracle AND
  not-frozen AND not-diverging AND not-artifact — the same reliability regime as the global lead, so
  fades freeze exactly when the grader freezes.
- **rebaselineLeadClocks interaction is now consistent.** That function skips fade buckets
  (`:3691` region, verified in diff), so fades move *only* via this learn nudge — no competing shifter.
- **autonomousVision OFF is byte-identical for this fix** (the nudge lives entirely inside the
  `if (config_.autonomousVision)` branch).

*Residual note (SOFT, PRE-EXISTING, not from this commit):* on the **vision** fade path
(`crossing − effectiveLatency`, taken before the clock arms), `effectiveLatency` for a fade in
autonomous mode reads `learnedLatencyMs + shotTypeLatencyMs` (`:2261-2262`), and a single autonomous
outcome moves *both* of those (`:4597`, `:4603`). That is a 2-knob move of the vision deadline, but
it is the inherent autonomous-vision design (both gains small: 0.10 / 0.08), applies to all types not
just fades, and is orthogonal to Fix 1's feedforward-clock closure. Flagging for awareness, not as a
regression.

### FIX 2 — Per-type double-count fix / Finding 3 (offset XOR clock, keyed on `lastReleaseWasVisionTimed_`) → **CONFIRMED-CORRECT**

- **Stamp is set on every release path and is consistent with the graded shot.** `visionTimed`
  (`:3904-3910`) = releaseReasonCode ∈ {green_confirmed, predictive_target, pose_release_reactive};
  everything else (feedforward_target, timeouts, blind, pose_feedforward) is false. Scheduled fires
  restore the original code into `shot_.releaseReasonCode` *before* `triggerRelease`
  (`:2136-2141`), so the stamp reflects the real estimator, not "release_scheduled".
- **No stale-stamp cross-shot hazard.** `lastReleaseWasVisionTimed_` (`:3923`) and the meter capture
  `meterCapSeq_ / meterCapShotType_` (`:4855-4856`, via `startPostReleaseMeterCapture` at `:3884`) are
  written together in the SAME `triggerRelease` call. `evaluatePostReleaseMeter` runs at the TOP of
  `process()` (`:1543`) before any new release in that tick, and any newer shot's release overwrites
  BOTH the capture and the stamp together — so a graded capture's `shotType` always has its matching
  stamp still in place. The stamp can never describe a different shot than the bucket being graded.
- **Attribution + signs correct.** Vision deadline = `crossing − effectiveLatency`, `effectiveLatency`
  includes `+shotTypeOffsetMs` ⇒ moving the offset owns the vision correction (LATE ⇒ `learned +=
  perTypeStep>0` ⇒ larger lead ⇒ earlier). Feedforward deadline = `anchor + clock − ffOffset` ⇒ moving
  the clock owns the feedforward correction (LATE ⇒ `clk −= perTypeStep` ⇒ earlier). Each outcome moves
  exactly one knob = one step. This genuinely removes the prior `−2·step` on the feedforward deadline
  (which depends on both clock and offset).
- **Scope note (not a defect):** this XOR logic lives in the non-autonomous per-type path, which is
  bypassed in the production default (`autonomousVision` ON returns at `:4655`). So Fix 2 hardens the
  dev/opt-out path; it is inert in the launcher-forced autonomous build. Correct as written.

### FIX 3 — tick-lock double-fire (N1) + fire-loop TOCTOU (N2) + atomic `armed_` (N3) → **CONFIRMED-CORRECT**

- **N1** (`OrionAppController.cpp:6063-6088` + `AutomationEngine.h:1266-1272`): the tick-lock nudge is
  applied at arm time while the engine held the *un-nudged* deadline. The real re-arm site is
  `reevaluateScheduleOnFreshSample`; its earlier-only clamp
  `if (fireAtMs >= schedFireDeadlineMs_ − 1e-6) return;` (`AutomationEngine.cpp:4394`) let a fresh
  crossing between the nudged and un-nudged deadline pass ⇒ `clearScheduledFire` + `scheduleFire` minted
  a new token ⇒ **second physical press**. `noteArmedFireDeadlineMs` writes the nudged (true) deadline
  back, so the clamp now sees the real fire instant and blocks the reschedule. The setter is guarded
  (`token == schedFireToken_ && schedFireDeadlineMs_ ≥ 0`), so a cleared/stale schedule can't be
  resurrected, and it is only called when the nudge actually moved the deadline ⇒ tick-lock OFF is
  byte-identical. The grace path is NOT weakened by moving the deadline earlier: `takeFired`/confirm is
  consumed before `process()` checks grace each tick, so a slow confirm never turns into an in-tick
  second fire. Correct.
- **N2** (`OrionAppController.cpp:871-883, 966-977`): after the ≤1.2 ms unlocked spin the loop
  re-acquires `m_` and re-checks `quit_ || aborted_ || token_ != token` before submit. Lock discipline
  is correct — the `continue` paths (`:951`, `:974`) all leave `m_` **held** so the next
  `cv_.wait(lock,…)` is well-formed; the submit block re-locks `m_` under `submitMutex_` (declared lock
  order submitMutex_→m_, preserved). `aborted_` is cleared on every `arm()` (`:866`), so it cannot
  stale-suppress a later legitimate fire; it suppresses only a same-token disarm/quit that genuinely
  raced the spin. Cannot wrongly suppress, cannot double-fire.
- **N3** (`AutomationEngine.h:1560`): engine `armed_` → `std::atomic<bool>`, single-writer (freeze
  watchdog) / GUI-reader; relaxed = plain load/store on x86, removes the UB, zero behavior change.

### FIX 4 — P1 atomic frame-bundle snapshot (aaea300e, remote_play_orchestrator.py) → **SUSPECT** (fixes its target, but introduces a NEW narrower tear — see Finding H1)

The fix achieves its stated goal: every timing consumer in the processing iteration now reads one
`self._frame_bundle` tuple (`:2093-2094`) instead of re-reading live `self._last_*` after the 6-40 ms
`detect()`, so the documented post-detect staleness skew (early fire) is closed. `_frame_age_ms`
(`:2544`), `_frame_measurement_epoch_ms(pts,epoch)` (`:2137`) and native-Y (`:2148`) all use snapshot
locals. The single-tuple reference read is GIL-atomic and tear-free **once published**.

BUT the publish ordering is inverted relative to the consumer's gate — a real regression the fix
created. See **Finding H1** below. Hence SUSPECT, not CONFIRMED.

---

## PART B — new findings (not in REDTEAM_TIMING_ARBITRATION / _CONCURRENCY / _FINDINGS)

### H1 — MED / SOFT — Fix 4 publishes `_frame_bundle` AFTER bumping `_frame_seq` ⇒ seq-gate can pass on frame N while the bundle still holds N−1
`remote_play_orchestrator.py:1802` (`self._frame_seq += 1`) then `:1807` (`self._frame_bundle = (…)`).

The processing loop's freshness gate keys on the LIVE `self._frame_seq` (`:2065`,
`self._frame_seq == last_seq`) and the LIVE `self._last_frame`, but the data it then consumes is the
**bundle** (`:2093`). Because the bundle is published *after* the seq bump, a GIL thread-switch in the
(comment-only) gap between `:1802` and the tuple store at `:1807` lets the processing thread observe the
new `_frame_seq` (gate passes), then read a `_frame_bundle` still holding **frame N−1**'s
`(img, ts, y, pts, epoch)`.

Concrete failure: for that one frame the loop runs `detect()` on N−1's image, stamps it with live
`_frame_seq = N`, and sets `last_seq = N` (`:2089`) — so frame N's genuine bundle is **never processed**
(next poll `_frame_seq == last_seq` ⇒ idle). Effects: (1) a **duplicate fill sample** — exactly the
velocity noise the seq-gate at `:2060-2064` exists to prevent; (2) `frame_age` computed from N−1's older
`ts` **over-reports** staleness by ~one frame interval (~16-33 ms), a bounded early-lead bias on that
frame; (3) first-frame variant: the bundle can still be the init `(None, 0.0, None, 0, 0.0)` → `detect(None)`
raises, swallowed by the `try/except`, one dropped frame.

Why NEW (scrutinized): **before** aaea300e the consumed field was `frame = self._last_frame`, set at
`:1778` *before* the seq bump at `:1802` — correct release order, so a thread that saw the new seq was
guaranteed the new frame. The fix moved the consumed payload (`_frame_bundle`) to *after* the seq bump,
inverting that. Distinct from the documented P1 (which was the post-detect live-`ts` read).

Severity MED / SOFT: the window is nanoseconds of pure Python, but Python's ~5 ms switch timer fires
continuously against a free-running capture loop, so it lands there occasionally over a session;
bounded blast radius (one stale/duplicate detection frame, conservative-ish timing direction).

**Fix:** publish `_frame_bundle` **before** `self._frame_seq += 1` (make the seq bump the release
barrier, published last), so any thread that sees the new seq is guaranteed the matching bundle. (The
`_last_frame` write at `:1778` is already correctly before the bump.)

### H2 — MED / SOFT — Go-To shot-gate window expires (~2 s) before a late-surfacing Go-To meter appears ⇒ reader loses the early-rise frames the gate exists to recover
`remote_play_orchestrator.py:825-826` sets both `_shot_gate_deadline_seq` and
`_shot_gate_hw_deadline_seq = _frame_seq + _shot_gate_arm_frames` (default 120 ≈ 2 s @60 fps, `:507`)
ONCE per physical arm; no periodic re-arm. Consumed at `:2112`. The only in-shot refresh is the CV
self-arm at `:2168-2178`, gated on `result.detected` + rising.

Failure: native itself notes Go-To meters "surface seconds into the hold"
(`AutomationEngine.cpp:2196-2197`). If the meter appears >2 s after the stick-arm, both gate deadlines
have already expired, so the reader is in COLD acquire (`simple_meter_reader.py:917`, `H_ACQ=33`,
`AR_MIN=1.8`) instead of the relaxed armed gate (`H_ACQ_ARMED=15`, `AR_MIN_ARMED=0.7`). The short
early-rise column is rejected cold ⇒ `detected=False` ⇒ the CV self-arm can't fire (chicken-and-egg)
until the meter has already grown past the cold gate. Net: the reader loses the first ~3-7 rise frames
on exactly the shot type the gate was built to help, delaying velocity/green acquisition and pushing
that Go-To toward a later/mistimed release. Fades (appear ~340-590 ms) and Standstill are inside the 2 s
window ⇒ unaffected, so this is Go-To-specific (hence MED, not HIGH).

**Fix:** re-arm (extend) the shot-gate deadline while the hold is still active and no meter has been
acquired (e.g. bump on each processed frame until `meterSeenThisShot`), or key the Go-To arm window on
hold-elapsed rather than a fixed frame budget.

### H3 — LOW / SOFT (design tradeoff, flagged for the record) — "meter loss ⇒ reset" is only true for a FROZEN feed, not a live vanish
Both stall watchdogs key on capture freeze (pixel age since last UNIQUE frame): orchestrator
`remote_play_orchestrator.py:2073-2082` (only in the idle branch `:2065`) and sidecar
`autogreen_sidecar.py:80-99`. When the meter vanishes while frames keep flowing (body occlusion, or a
genuine vanish the reader coasts as `stale_or_memory` echoes), neither watchdog fires, and native keeps
`meterDetected`/`lastDetectionMs` advancing on echoes (`AutomationEngine.cpp:1359-1360`) so the 180 ms
`meterFresh` gate never lapses. With `memoryExtrapolationEnabled` default-ON, occlusion <500 ms
extrapolates `fillPct` toward the tip (`:1342-1353`), which feeds reach/crossing — wrong-direction if
the loss is a true vanish rather than an occlusion.

Scrutinized: bounded at 500 ms, crossing velocity comes only from genuine samples, and reach/tip floors
still gate the fire; the engine fundamentally can't distinguish "occluded but rising" from "gone" inside
500 ms — this IS the intended feedforward-through-occlusion behavior. Kept LOW and noted only because
the two freeze-watchdogs create a false impression that any meter loss triggers a reset.

*(Release/anchor gates were checked and are clean: they use genuine-accept-only signals
—`sawFreshMeterThisShot`/`firstFreshAcceptMs`/`meterSeenThisShot` latch, `AutomationEngine.cpp:2184-2189`,
`:1370`— never `meterDetected`, so an echo/carryover cannot anchor the clock or satisfy a release. A
committed legacy scheduled fire cannot be cancelled by meter loss, but the commit horizon is
`schedulerHorizonMs = 6.0` ms, so it fires on the last fresh sample within ~6 ms — the intended sub-tick
contract, not a live miss.)*

### H4 — LOW / SOFT — torn cross-thread reads of gate/telemetry fields
(a) `_arm_shot_gate` writes `_shot_gate_deadline_seq / _hw_deadline_seq / _source` as separate stores
(`:825-827`) on the stdin/input-router threads; the processing loop reads deadline (`:2112`) and hw
(`:2115`) as separate loads → a one-frame `armed=true / hw=stale` split pushed into the reader's
colour-training bit (`_sg(_armed_now,0.0,_armed_hw_now)`, `:2126`); self-corrects next frame.
(b) The sidecar telemetry loop assembles `meter_present` / fill / `tracking` from unsynchronized reads
(`autogreen_sidecar.py:616,655,691`) of fields the CV thread mutates non-atomically — but native
reconciles at ingestion (forces fill/conf/vel→0/−1 when `meterPresent` false, requires `sidecarFill>0`
for `detected`), so torn combos collapse to a harmless 1-frame not-detected echo. Same class as the
already-documented P3; no misfire. LOW, note-only.

---

## Summary

| Fix | Verdict |
|-----|---------|
| 1 — Fade learn-loop closure | **CONFIRMED-CORRECT** (sign right, gain 0.12<1, greens don't drift, gated by leadMayMove, no feedforward double-count) |
| 2 — Per-type offset-XOR-clock (Finding 3) | **CONFIRMED-CORRECT** (stamp always consistent with graded shot; one knob/one step; inert in autonomous prod) |
| 3 — N1 tick-lock / N2 TOCTOU / N3 atomic armed_ | **CONFIRMED-CORRECT** (earlier-only clamp now honest; spin re-check lock discipline sound; aborted_ can't stale-suppress) |
| 4 — P1 frame-bundle snapshot | **SUSPECT** — fixes the post-detect skew but inverts publish order (Finding H1) |

New findings, ranked: **H1** (MED/SOFT — Fix 4 seq/bundle publish inversion; trivial fix: publish
bundle before the seq bump), **H2** (MED/SOFT — Go-To shot-gate expires before a late meter),
**H3** (LOW/SOFT — meter-vanish extrapolation, design tradeoff), **H4** (LOW/SOFT — torn gate/telemetry
reads, note-only).
