# Red-team: timing / grading / learning ARBITRATION in AutomationEngine

Read-only adversarial audit of `native_orion/src/AutomationEngine.cpp` / `.h`.
Scope: how the estimators are arbitrated (which one fires, when) and how the
learned clocks/offsets update (feedback poisoning, double-counting, which-fires-
vs-which-learns mismatches, flag byte-identity). NOT individual estimator math.

Confirmed already-fixed (not re-reported):
- **#1 holdStartFrameAgeMs early-bias** — `triggerRelease` learns from
  `ptsHoldStart = holdStartMs - holdStartFrameAgeMs` (cpp:3911, 3985). Fixed.
- **#6 measured-lead double-counts networkOffsetMs** — `effectiveLatency` uses
  `useMeasuredLead ? 0.0 : abs(networkOffsetMs)` (cpp:2244) and `fusedLead`
  never re-adds network (cpp:2528-2535). Fixed.

---

## FINDING 1 — Fade feedforward loop never closes in production (autonomous). HIGH / HARD

**Files:** fire side cpp:2122, 2670-2700; learn side cpp:4537-4602 (autonomous
branch), 3929-3939 & 3960-3997 (triggerRelease seeding/global learning).

**Mechanism.** Fades are deliberately carved OUT of the global clock on the FIRE
side (`isFade` at cpp:2122; `holdClock`/`meterClock` fall back to the *per-type*
`shotTypeFeedforwardMs`/`shotTypeMeterToReleaseMs`, cpp:2677-2682) and the fade
feedforward lead is the *per-type* offset (`ffOffset = shotTypeOffsetMs(...)` for
`isFade`, cpp:2699-2700), NOT `effectiveLatency`. So a fade's feedforward fire
deadline is governed entirely by two per-type knobs:
`anchorValidMs + shotTypeMeterToReleaseMs[fade] - shotTypeOffsetMs[fade]`.

But in production `autonomousVision` is ON (launcher forces
`ORION_AUTONOMOUS_VISION`; comment cpp:2209). Under that flag `learnFromOutcome`
takes the autonomous branch (cpp:4537) which dials only `learnedLatencyMs`,
`shotTypeLatencyMs`, `shotTypeTargetOffsetPct` and then `return`s — it NEVER
writes `shotTypeMeterToReleaseMs`, `shotTypeFeedforwardMs`, or
`shotTypeLearnedOffsetMs`. Neither of the fade's two fire-controlling knobs is a
teacher target. The fade FF path also ignores `learnedLatencyMs`/`shotTypeLatencyMs`
(those live only in `effectiveLatency`, which the fade FF path does not read).

The per-type clocks are only ever written by the one-time seeding in
`triggerRelease` (cpp:3919-3939), gated `clock <= floor` — i.e. **while unarmed**.
Shot 1 of a fade type fires via vision (clock unarmed → `fastTypeClockOwns` false)
and seeds the clock; from shot 2 on `fastTypeClockOwns` is true (cpp:2689) and the
feedforward clock OWNS the release — and is now frozen forever.

**Failure scenario.** Any Fade type (Left/Right/Post/Back/Front), autonomous ON
(production), gradeV2 OFF, fused authority OFF (all defaults). After ~1 seeding
shot every fade fires on its per-type feedforward clock. If that seeded clock is
biased (it is seeded from a *single* early vision-timed fade whose own lead was
mis-modeled — see Finding 2), the post-release grader's correction is routed to
`learnedLatencyMs`, a knob the fade FF fire does not consume. The bias is
**systematic and permanent** for every fade of that type. `shotTypeTargetOffsetPct`
(the one autonomous knob the fade fire reads, cpp:2304) shifts the *fill target*,
not the clock deadline, and is clamped ±3% — it cannot correct a clock-time bias.

**Which-fires-vs-which-learns mismatch:** fires on `{shotTypeMeterToReleaseMs,
shotTypeOffsetMs}[fade]`; learns into `{learnedLatencyMs, shotTypeLatencyMs}`.
Disjoint sets. This is the textbook arbitration/learning decoupling.

**Fix.** In the autonomous branch of `learnFromOutcome`, for `isFade` buckets (and
any bucket whose FF path uses `shotTypeOffsetMs` rather than `effectiveLatency`),
also integrate the outcome error into `shotTypeMeterToReleaseMs[bucket]` /
`shotTypeOffsetMs[bucket]` (bounded, slow), OR route fades through the fused/
gradeV2 closed loop. Simplest: mirror the per-type clock nudge (cpp:4688-4701)
into the autonomous branch for fade buckets only.

**Stress-test of the negative** ("maybe fades are meant to be calibrated only by
banner/oracle"): the banner/oracle path (`setShotVerdict`→`learnFromOutcome` with
`authoritativeVerdict_`) still hits the SAME autonomous branch and still only
moves `learnedLatencyMs`/per-type-latency — it also does not write the fade clock.
So even the "authoritative" path cannot calibrate a fade's dominant fire path.
The decoupling is real, not a mode I missed.

---

## FINDING 2 — rebaselineLeadClocks over-shifts fade (per-type) clocks. HIGH (gated ORION_MEASURED_LEAD) / HARD

**Files:** cpp:3694-3703 (the shift), 3671; consumers cpp:2699-2702 (fade
`ffOffset`), 1182-1184 (call site).

**Mechanism.** When `ORION_MEASURED_LEAD` engages, `rebaselineLeadClocks` adds
`delta = measuredLatencyMs_ - learnedLatencyMs` (~+69ms) to EVERY per-type clock:
`shotTypeFeedforwardMs` (cpp:3694-3698) and `shotTypeMeterToReleaseMs`
(cpp:3699-3703). Its stated premise (cpp:3667): "the clocks absorbed the OLD ~6ms
lead; now we subtract ~75, so shift them by the delta or every path fires early."
That premise holds ONLY for clocks whose feedforward path subtracts
`effectiveLatency` (Go-To, and non-fade under `globalApplies` — whose lead does
jump from ~6→~75, so a +delta clock shift keeps the fire stable).

For **fades it is false**: the fade FF path subtracts `shotTypeOffsetMs`
(cpp:2699), which does NOT change when the measured lead engages. So the fade's
fire deadline `= anchorValidMs + (meterClock + delta) - shotTypeOffset` moves
**later by exactly `delta` (~69ms)** with no compensating lead increase → every
fade feedforward fire lands ~69ms LATE. This is precisely the transient the
rebaseline was written to PREVENT — but inflicted on fades, for the wrong reason.

**Compounds Finding 1:** because the fade per-type clock is write-frozen in
autonomous mode, the ~delta step is **permanent** — the grader never walks it back.

**Fix.** In `rebaselineLeadClocks`, skip (or apply a per-type-appropriate shift to)
buckets whose FF lead is `shotTypeOffsetMs`, i.e. `isFade` buckets. Only shift
clocks whose feedforward path actually swaps its lead base to the measured value.

**Stress-test:** could the pre-existing fade clock have been ~delta too SHORT so
the +delta happens to correct it? The *fire-deadline* shift is unambiguous math
(`meterClock += delta`, `ffOffset` unchanged ⇒ deadline += delta); whether that is
net-corrective depends on an unrelated prior bias, so applying an uncontrolled
~69ms step "for a reason that does not apply to fades" is unsound regardless of
sign. Not a false positive.

---

## FINDING 3 — learnFromOutcome double-counts the correction (offset AND clock). MED / HARD

**Files:** cpp:4678-4701 (per-type ACQUIRE→LOCK branch).

**Mechanism.** For a per-type feedforward fire the deadline is
`anchorValidMs + meterClock - ffOffset`, with `ffOffset = shotTypeOffsetMs =
base + shotTypeLearnedOffsetMs` (cpp:1704-1711). On a single LATE outcome
(`errorMs > 0`), `learnFromOutcome`:
- `shotTypeLearnedOffsetMs += stepFor(errorMs)` (cpp:4679) ⇒ `ffOffset` up ⇒
  deadline **−step**;
- `shotTypeMeterToReleaseMs -= stepFor(errorMs)` (cpp:4698) ⇒ deadline **−step**.

Total deadline motion for one error = **−2·step**. With `annealGain` up to
`outcomeGainInitial = 0.6` (cpp:442, 4633) the effective loop gain reaches ~1.2 —
above unity → over-correction and EARLY/LATE bang-bang; even the stable 0.3 gain
becomes an effective 0.6. The same double-move hits the hold-start anchor
(`holdThresh = holdClock - ffOffset`, both moved).

**Scope.** Only the non-autonomous per-type calibration path (autonomous branch
returns before this code; `calibrationFrozen` must be off or
`calibrationMode && authoritativeVerdict`). So it bites dev/test and any
banner/oracle-driven per-type calibration session, not the autonomous default.
Hence MED, not HIGH.

**Fix.** Correct ONE knob per outcome, not both — either move the offset (vision
lead) OR the clock (feedforward), keyed on which estimator actually fired this
shot (`shot_.releaseReasonCode`), or halve the combined gain. The vision path
reads only the offset, so the offset should own vision-timed outcomes and the
clock should own feedforward-timed outcomes.

---

## FINDING 4 — consensus_target fires are excluded from all learning. LOW / SOFT

**Files:** cpp:3286-3289 (sets `consensus_target`); cpp:3890-3896 (`visionTimed`
does not include it).

`visionTimed = green_confirmed || predictive_target || pose_release_reactive`. A
consensus release (green crossing + velocity crossing agree within
`consensusWindowMs`, cpp:3280-3289) is a genuinely vision-timed, high-quality
release, yet it seeds no clock, updates no velocity prior, and feeds no global
learning. This is lost signal, not poisoning (no wrong update), so LOW — but on a
build where consensus fires often, per-type clocks under-seed / under-converge.

**Fix.** Add `consensus_target` to the `visionTimed` set (it is at least as
trustworthy as `predictive_target`).

---

## FINDING 5 — bandit lead attributed to fused-owned fires it did not affect. LOW / SOFT

**Files:** cpp:2229-2271 (banditLead folded into `effectiveLatency`),
cpp:2532-2535 (`fusedLead` excludes banditLead), cpp:3801-3803 (`noteRelease`).

When both `ORION_BANDIT_LEAD` and `ORION_FUSED_FIRE` are on and the fused
posterior owns the shot, the fire uses `fusedLead` (no bandit offset), but
`triggerRelease` still calls `banditTuner_.noteRelease(...)` and the arriving
green-zone self-grade is attributed to the current arm. The bandit then "learns"
from shots its ±4ms arm never perturbed → its lock converges on noise. Narrow
(needs both experimental flags + calibrationMode) → LOW.

**Fix.** Skip `noteRelease` (or don't attribute the grade) when the release code
is `fused_posterior`, or add `banditLead` into `fusedLead`.

---

## Byte-identity when OFF — verified clean

Each ceiling-stack flag is byte-identical to the legacy path when its flag is off:
- `ORION_PLATEAU_AIM`: `plateauAim = enabled && greenTracker_.confirmed()`;
  `plateauShiftMs` reset to 0 unconditionally (cpp:2313-2318, 2365-2369). ✓
- `ORION_TEMPLATE_ARRIVAL`: guarded `enabled && templateArrival_.matched()`;
  diagnostics-only writes when off, `crossing` untouched (cpp:2345-2364). ✓
- `ORION_REG_FUSION`: `enabled && regConf>=min && regTipMs>0` (cpp:2333). ✓
- `ORION_MEASURED_LEAD`: `useMeasuredLead` requires `measuredLeadEnabled`;
  `banditLead` = 0 when off (cpp:2224, 2229). ✓
- `ORION_BANDIT_LEAD`: `banditLead` 0 and `noteRelease` guarded (cpp:2229, 3801). ✓
- `fusedFireEnabled`/`priorPosteriorBlendEnabled`/`blindFireSuppressEnabled`/
  `gradeV2Enabled`: all wrapped and early-return / no-op when off. ✓

The ON-paths of Findings 1–2 are the exception: they are individually correct in
isolation but their FIRE-side carve-out (fades off the global clock) is not matched
on the LEARN side, so the flags are not *jointly* consistent.
