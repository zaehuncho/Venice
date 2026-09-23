# Onset feedforward (2026-09-21)

Owner ask: "when I'm wide open ... how can we make slightly more wide open shots" and, on the
diagnosis, "onset forward anyway".

## The measurement

`logs/orion_native.log` + `.log.1`, 347 banner-graded standstills, three online sessions
(2026-09-20 14:10L n=13, 2026-09-20 16:43L n=211, 2026-09-21 03:22L n=60).

The meter onset after the press (the game's online sync delay as the engine sees it):

| session | onset median | IQR | sd | lag-1 autocorr | shot-to-shot residual sd |
|---|---|---|---|---|---|
| 09-20 14:10L | 515 | 500-543 | 66 | -0.02 | 62 ms |
| 09-20 16:43L | 508 | 498-554 | 66 | +0.34 | 62 ms |
| 09-21 03:22L | 509 | 498-570 | 71 | -0.03 | 70 ms |

It is re-rolled on every shot. A verdict integrator (the banner lead trim: two CONSECUTIVE
agreeing verdicts per 3 ms step, +-15 cap, cold start each launch) moved ONCE in the 74-verdict
09-21 session. Offline practice onset is 300-400 ms and grades 92% EXCELLENT.

Verdict vs the shot's onset deviation from its local 10-shot median (online, n=284):

| deviation | EXC | LATE | EARLY | n |
|---|---|---|---|---|
| < -60 | 100% | 0% | 0% | 4 |
| -60..-20 | 74% | 14% | 11% | 35 |
| -20..+20 | 71% | 14% | 15% | 154 |
| +20..+60 | 43% | 43% | 13% | 46 |
| > +60 | 44% | 47% | 9% | 45 |

One-sided: a later-than-usual meter misses LATE half the time; an earlier one does not miss.

An ordinal (Gaussian landing, fixed window) fit of the two late tails puts the correctable
BIAS at ~0.7-0.9 window half-widths, i.e. **~10-12 ms**, with the landing sd also ~1.5x wider
there (shorter runway). Centering those tails is worth ~+5-8 points on the ~32% of shots in
them, **~+2-3 points overall**; the rest of the tail loss is variance this term cannot buy back.
(The "+9 points" first quoted in chat assumed the whole tail loss was bias; it is not.)

## The rule (`native_orion/src/OnsetFeedforward.h`)

Per shot-type bucket (`BannerLeadTrim::bucketFor`): a reference = median of the last
`window` (10) COMPLETED shots' onsets, fed at the release from the same stamp the banner trim
trusts (`noteBannerTrimRelease` -> `noteOnsetFeedforwardRelease`, one per physical epoch, blind
releases excluded). For the live shot: `deviation = onset - reference`;
`displacement = clamp(-gain * deviation, +-clamp_ms)`; `one_sided` keeps `deviation <= 0` at 0.
Fewer than `min_samples` (4) in the bucket = cold = nothing moves.

Applied in `AutomationEngine::scheduleFire` at the single choke point where a deadline becomes
THE scheduled fire, right after the dev sweep offset and under its rules: AutonomousMeterVision
authority only, never a calibration probe, never a guaranteed-past deadline, the applied
displacement retained in `schedFireAppliedOnsetFfMs_` so every undisplaced comparison recovers
`deadline - dev - ff`, and every lease bound that admits a positive dev displacement admits the
sum. `tipAbsMs` is never touched. Unlike the dev offset it does NOT fence the learners.

## Knobs (settings keys / env, clamped on every route)

| setting | env | default | band |
|---|---|---|---|
| onset_ff_gain | ORION_ONSET_FF_GAIN | 0.2 | 0..1 (0 = off) |
| onset_ff_clamp_ms | ORION_ONSET_FF_CLAMP_MS | 10 | 0..40 |
| onset_ff_window | ORION_ONSET_FF_WINDOW | 10 | 3..40 |
| onset_ff_min_samples | ORION_ONSET_FF_MIN_SAMPLES | 4 | 2..20 |
| onset_ff_one_sided | ORION_ONSET_FF_ONE_SIDED | 1 | 0/1 |

Ships ON at 0.2 / 10 ms: the ceiling IS the expected full correction and the gain reaches it at
a +50 ms deviation. `run_orion.local.ps1` scrubs the env knobs unless `-AllowTimingOverrides`.

## Log lines (all key=value, append-only)

- `ONSET FF: reason=applied|cold|one_sided|zero|no_onset|off bucket= onset_ms= ref_ms= dev_ms=
  offset_ms= applied_ms= n= gain= clamp_ms= net_offset_ms= shot_attempt= physical_epoch=` -
  one per arm token (+ one per decision change inside it).
- `Release onsetff: seq= applied_ms= scheduled= shot_attempt=` - seq-paired with the release,
  0 for an in-tick release, whenever the term is on.
- `PHASE SAMPLE ... onset_ff_ms=` - present only when the landing flew displaced.
- `SHOT RECORD ... court_rtt_ms= court_jitter_ms= court_ready=` (sidecar) - the court RTT the
  sampler held at the press, so onset jitter can be regressed on network state.

## Grading (owed, 3+ online sessions)

Join `Release onsetff` (seq) with `BANNER VERDICT` and the SHOT RECORD onset. Two questions:
1. Did the +20..+60 and > +60 deviation groups move toward the centre group's EXCELLENT rate
   (43-44% -> ~50%)? If they moved past it into EARLY, the gain is too high.
2. Does `court_jitter_ms` track onset deviation? If network jitter is single-digit ms while
   onset jitters 60-70, the game's netcode is the source and only feedforward helps; if they
   correlate, the packet bridge is a second lever.

The Codex-reviewed +-25 ms `ORION_DEV_FIRE_OFFSET_SWEEP=list:-25,25` screen (actuation gain,
180 assignments) still stands for whoever wants the gain identified rather than fitted; it
costs makes during those sessions and the owner chooses whether to run it.

## Tests

`native_orion/tests/AutomationEngineTests.cpp`: `onsetFeedforwardPolicyMathAndColdStart`,
`onsetFeedforwardDisplacesScheduledDeadlineEarlier`,
`onsetFeedforwardInertWithoutReferenceGainOrLateOnset`.
`tests/test_shot_records.py`: the court RTT stamp on the record and the summary line.

## Round 2 (Codex review 2026-09-21, BLOCKED -> fixed)

- **Learner fences (F4).** A feedforward-displaced release (`lastReleaseOnsetFfMs_ != 0`) is now
  a displacement for every learner, exactly as a dev-sweep or band-clamped one: the release
  MARKER is dropped and logged (`ONSET FF: marker_suppressed_onset_ff`) so the sidecar latency
  estimator never sees it; `recordPhaseConstantSample` logs but never accepts it
  (`onset_ff_fence=1`); `recordLandingLeadSample` drops it; the banner trim refuses its verdict
  AND its oracle (`BANNER TRIM: ignored reason=onset_ff_displaced`, `ORACLE ... used=0
  reason=onset_ff_displaced`) via `onsetFfMs` stamped on the release ring. The shot-gate
  release (records, banner attribution) is a different message and is untouched.
- **Reset + staleness (F5).** `AutomationEngine::reset()` clears the ring, the epoch fence and
  the carried displacement. A bucket whose last completed onset is older than
  `OnsetFeedforwardLimits::staleMs` (10 min) is refused (`stale`) and emptied before the next
  onset enters it, so a new game/court starts cold. (No explicit court hook exists in the
  engine; the staleness fence is the context change the engine can see.)
- **One bound (F6).** The banner trim this shot already spends and the feedforward may not sum
  past `banner_trim_max_ms` (15): `bound = max(0, 15 - max(0, trim))`; a bucket at +15 gets no
  feedforward (`reason=trim_bound`), one at +9 gets at most 6. Logged as `trim_ms= bound_ms=`.
- **Grader (F7).** `tools/timing/onset_ff_grade.py` joins ONLY by physical epoch
  (`BANNER VERDICT release_seq` = shot-gate epoch = `SHOT RECORD epoch` = `ONSET FF
  physical_epoch` = `Release onsetff physical_epoch`, the last field added for this).
- Tests: `onsetFeedforwardFencesPhaseAndTrim`, `onsetFeedforwardIsBoundedByTheBannerTrimAndResets`.
  Not unit-tested: the marker drop itself (needs the full driven-release fixture); it mirrors
  the hold-band branch one line above it and is greppable live.

## Round 3 (Codex re-review, residuals -> fixed)

- **Per-release normalisation (F4 residual).** `lastReleaseOnsetFfMs_` is normalised at the TOP of
  the release path (`shot_.firedByScheduler ? carried : 0`), before the marker and before the
  shot-gate release stamps the trim ring, so an in-tick release after a displaced scheduled one
  carries 0 everywhere. Test: `onsetFeedforwardDisplacedReleaseDropsTheMarkerAndNormalisesPerRelease`
  (scheduled displaced release: no marker, `marker_suppressed_onset_ff`, `applied_ms=-10.00
  scheduled=1`, ring -10; the next in-tick release: marker emitted, `applied_ms=0.00 scheduled=0`,
  ring 0).
- **Court hook (F5 residual).** `RemotePlaySession::courtChanged(ip)` fires once per new PUBLIC
  court from the telemetry RTT snapshot (never the local startup target, never twice for the
  same court); `OrionAppController` routes it to `AutomationEngine::noteContextChanged("court_changed")`
  which drops the ring/fences (`ONSET FF: context_reset reason= dropped_onsets=`) and leaves the
  displacement a release already carried alone. The 10-minute staleness stays as the fallback.
  Tests: `courtChangedFiresOncePerNewPublicCourt` (session), context leg of
  `onsetFeedforwardIsBoundedByTheBannerTrimAndResets` (engine).
- **Comment (F6 residual)** at the choke point now describes the five fences.
- **Audit completeness (C1).** `security_audit.py` also reads the completeness half of the policy:
  a shipped app (its exe present) with no Controls tree -> HIGH `PACKAGE_QT_ROOT_MISSING`; a pinned
  style missing any of its files -> HIGH `PACKAGE_QT_STYLE_INCOMPLETE`. Fixtures without the exes
  are untouched. Test: `test_package_audit_requires_the_qt_tree_and_a_complete_pinned_style_for_each_shipped_app`.
- **C2 (stale package).** Regeneration waits on the sidecar source-binding gate: Astra's reader
  edits landed after the 14:32 sidecar build and are not Claude's to ship.
