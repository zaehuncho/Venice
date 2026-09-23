# P6 — Timing engine and learners (Claude, read-only)

Lane P6, ID prefix `CL2-P6-`. Scope: the code added since the 09-21 wave in `native_orion/src`:
`OnsetFeedforward.h` and its use in `AutomationEngine::scheduleFire` (the ONSET FF block and the
`ORION_ONSET_FF_AB` arms), the late carry (`ORION_DEV_LATE_CARRY_AB`, `lateCarryForShotMs`,
`noteLateCarryVerdict`, folded into `schedFireAppliedOnsetFfMs_`), the dev offset hook
(`ORION_DEV_FIRE_OFFSET_SWEEP`), the banner trim fences, and learning.json persistence in `AppConfig.*`.
No source, settings or git state was changed. Nothing was built or launched. The log evidence
comes from `logs/orion_native.log.1` (09-21 18:56Z to 09-22 22:31Z) and `logs/orion_native.log`.

Not re-reported: GM-002/CX-001/CX-008 (learning.json backup), GM-006/CX-007/CX-020 (calibration
cancel), and 09-21 B-7 (the aim bucket vs the teach bucket; `staleMs` not bound to config). B-7 is
unchanged: `AutomationEngine.cpp:15320` still passes `OnsetFeedforwardLimits{}.staleMs`. I found
more evidence for GM-002; it is under "Evidence for known IDs" below.

## What I verified and found correct

| Claim | Verdict | Evidence |
|---|---|---|
| "Five fence sites keyed on `lastReleaseOnsetFfMs_`" | **All five exist and are keyed correctly** when `onset_ff_gain > 0` | (1) release marker `AutomationEngine.cpp:13234-13249`; (2) banner verdict `:15547-15557` via the ring stamp `:15427`; (3) release oracle `:15851-15860`; (4) phase-constant sample `:22758`, `:22773`; (5) landing-lead sample `:23069-23072`. The arm-time model recovery `:20823-20826` is a sixth site. |
| In-tick release does not inherit the previous scheduled shot's displacement | OK | `:13234` normalises to 0 unless `shot_.firedByScheduler`. `setPhysicalShotEpoch` (`:5561`) cancels the prior pending grade at the next press, and `reset()` also cancels it, so the delayed sites (4) and (5) cannot read a later shot's value. |
| `observeBannerVerdict`'s `releaseSeq` is a physical epoch | **Confirmed** | `OrionAppController.cpp:8363` forwards the sidecar's `release_seq`. The sidecar's feed is `release_shot_gate(shot_epoch)` (`remote_play_orchestrator.py:2268-2289`), which is driven by `shotGateRelease(physicalShotEpoch, …)` (`AutomationEngine.cpp:23806-23807`). Only `attributed==1` panels reach the engine. |
| Dev hooks are dead in a production build | **Confirmed** | `CMakeLists.txt:56` defines `ORION_PRODUCTION_BUILD` for the whole directory. The sweep parser at `:17284-17345` and the late-carry parser at `:1661-1683` are both compiled out. `devFireOffsetArmed_` (`.h:5693`, default false) has no other writer. `lateCarryAbArmsMs_` is cleared at `:1660` before the guarded block, so `lateCarryForShotMs` returns 0. |
| Sign convention (earlier = negative) | Consistent | The FF offset is `-gain*dev` (`OnsetFeedforward.h:143`). The carry is `deadline - carry`, so the applied value is negative (`:17690-17694`). The dev offset is added (`:17573`). Every undisplaced-base recovery subtracts `dev + ff` (`:10078`, `:10831`, `:14034`, `:18189`). |
| Production bound on the two "fire earlier" terms | OK | The FF is capped at `bannerTrimMaxMs − trimEarlier` (`:17636-17647`). Trim + FF never exceed `banner_trim_max_ms` (15 by default, 40 at most). |
| Late carry does not integrate | OK | It is a fixed K per shot, never additive across shots (`:15368-15378`). A run of LATE verdicts cannot ratchet it. |
| Late carry verdict race | OK as designed | A verdict for an older epoch is dropped (`:15329`). A `verdict_pending` shot is re-decided at its next re-arm only (`:15349`). A verdict that lands after the current shot's release is correctly ignored, because the stamp has already moved to the current epoch. |

Live log check, 09-22 21:55–22:32Z. The window holds two runs:
- **Run A** (21:55:42–22:22): A/B arms `0.2:10:1` / `0.45:40:0` (112 / 115 decisions).
- **Run B** (22:22:30–): the single setting 0.2/10/one-sided.

`ORION_DEV_FIRE_OFFSET_SWEEP` was armed in both runs. Results:
- The marker was suppressed on every FF-displaced release: 157/157 in run A and 92/92 in run B, with no mismatches in either direction.
- Every displaced release that got a verdict logged `BANNER TRIM: ignored reason=onset_ff_displaced` (134 and 89).
- `LATE CARRY` never ran: 0 lines.
- All 423 `PRESS-TIP OBSERVATION` lines are `accepted=0`. That is only because the dev sweep's session-wide fence was up, and it hides CL2-P6-001.

---

## Findings, most severe first

### [CL2-P6-001] medium (high if NO METER, the hold band, the press-anchored predictor or the 0.45/40 arm ships) — FF-displaced releases are NOT fenced from four release-dated learners; two of them persist
- Lane: P6
- Exploit / failure path:
  1. The FF ships on (gain 0.2, one-sided), so it only ever moves the fire **earlier** (`OnsetFeedforward.h:138-144`).
  2. The dev sweep fences every release-dated learner with `devFireOffsetArmed_`. The FF fences only the five sites above. Four learners carry the dev fence but not the FF one:
     - **(a) `emitPressTipObservation`** (`AutomationEngine.cpp:22255-22264`, `:22329-22330`). This feeds `recordNoMeterHoldObservation` (hold = release − press, a direct ride on the displaced release; persisted to learning.json `no_meter_hold_by_type` through `noMeterHoldLearned`, `OrionAppController.cpp:3277-3282`). It also feeds `recordPressToTipObservation` (the tip is dated from the meter stop, which rides the release; persisted to settings.json `press_anchored_tip_ms`, `OrionAppController.cpp:3251-3259`). Both paths fence dev, late-fire, hold-band and blind releases, but not `lastReleaseOnsetFfMs_`.
     - **(b) Per-type velocity prior** (`:13389`, persisted as `shotTypeVelocityPriorPctMs`).
     - **(c) Per-type feedforward/meter-clock seed** (`:13349`, only while the clock is still unarmed).
     - **(d) `lastReleaseVisionConfident_`** (`:13339`). This is inert in autonomous meter mode, because `learnFromOutcome` returns at `:18723`.
  3. Because the FF is one-sided, every leaked sample is biased in the same direction: shorter holds and an earlier press→tip. This is the "freeze rides the release" trap: the learner re-aims toward the displacement.
- Live evidence, from production-like runs where the dev sweep was not armed:

  | Run start | FF-displaced releases | `accepted=1` PRESS-TIP on a displaced release |
  |---|---|---|
  | 09-21 20:51 | 5 | 4 |
  | 09-22 03:06 | 1 | 1 |
  | 09-22 21:47 | 1 | 1 |
  | 09-22 21:49 | 1 | 1 |

  Example: `20:53:21.662Z ONSET FF: reason=applied … applied_ms=-10.00 … physical_epoch=14` → `20:53:22.975Z PRESS-TIP OBSERVATION: physical_epoch=14 … confidence=H … accepted=1` (weight 1.0). The marker and the banner were fenced for the same shot: `BANNER TRIM: ignored reason=onset_ff_displaced release_seq=14`.
- Affected: `native_orion/src/AutomationEngine.cpp:13339-13398`, `:22234-22330`; learning.json `no_meter_hold_by_type` and `velocity_prior`; settings.json `press_anchored_tip_*`.
- Impact:
  - At 0.2/10 the median shift is a few ms, because only the upper-onset half is displaced, by ≤10 ms.
  - Today's customers are barely touched: `AppConfig::save` forces `noMeterEnabled=false`, the hold band is OFF and the press-anchored predictor is OFF.
  - Every customer still silently persists biased calibrations that those features will read later.
  - Under the 0.45/40 arm now being A/B'd, the displacement reaches −40 ms. Run A: 157 of 227 releases were displaced, with a range of −40 to +40 ms. The drift then becomes material.
- Reproduction (high level): build without the dev sweep, keep the FF on, and shoot 20+ online standstills. Join `Release onsetff applied_ms≠0` to `PRESS-TIP OBSERVATION accepted=1` on the epoch.
- Required fix: add `&& lastReleaseOnsetFfMs_ == 0.0` at the four sites. Better: one helper `releaseDisplacedForLearners()` = dev armed ∨ late-fire ∨ hold band ∨ FF/carry ≠ 0, called by every release-dated learner, so the next displacement cannot miss a site.
- Verification test: extend `onsetFeedforwardFencesPhaseAndTrim` to cover a displaced graded landing:
  - `noMeterHoldByType`, `pressAnchoredTipMs` and `shotTypeVelocityPriorPctMs` stay unchanged;
  - a PRESS-TIP line logs `accepted=0`;
  - an undisplaced control shot does update them.
- Confidence: confirmed (code-proven and seen in the log).

### [CL2-P6-002] medium — `lastReleaseOnsetFfMs_` is zeroed when `onset_ff_gain == 0`, which unfences the phase-constant and landing-lead learners for A/B-arm and late-carry displacements
- Lane: P6
- Exploit / failure path:
  1. The displacement term runs when `config_.onsetFeedforwardGain > 0.0 || !onsetFfAbArms_.isEmpty()` (`:17600`). The late carry runs whenever its arms are set (`:17686-17696`). Neither needs gain > 0.
  2. The release path keeps the value only `if (config_.onsetFeedforwardGain > 0.0)`, and otherwise sets `lastReleaseOnsetFfMs_ = 0.0` (`:13286-13299`).
  3. Sites (1)–(3) read the value before that line (`:13234`, and the ring stamp through `emitShotGateRelease` at `:13265`), so they stay fenced. Sites (4) phase constant (`:22758`) and (5) landing lead (`:23070`) run about 1.2 s later and read 0.
  4. With `onset_ff_gain=0` plus `ORION_ONSET_FF_AB` (production-reachable, see CL2-P6-006), or plus `ORION_DEV_LATE_CARRY_AB`, a displaced landing teaches the shared tip-phase constant, the aim constant every shot uses. "Carry on, FF off" is exactly the configuration an owner would use to isolate the carry.
  5. The seq-paired `Release onsetff` join line is also not emitted, so the offline grader cannot see the displacement.
- Affected: `AutomationEngine.cpp:13286-13299`, `:22758`, `:23070`.
- Impact: in an experiment the owner is likely to run, the aim constant silently absorbs deliberately displaced landings, and the experiment's own join data is lost.
- Reproduction (high level): set `onset_ff_gain=0` and `ORION_DEV_LATE_CARRY_AB=0,10` in a dev build. After a LATE→standstill pair with arm 10, the `PHASE SAMPLE` / `Tip phase measurement` line has no `onset_ff_fence=1` and is accepted.
- Required fix: key the keep-or-zero at `:13286` on "a displacement term was live this shot" (`gain>0 || !onsetFfAbArms_.isEmpty() || !lateCarryAbArmsMs_.isEmpty()`), or drop the else-branch. The value is already normalised at `:13234`.
- Verification test: gain 0 plus A/B arms (and gain 0 plus carry arms), with one displaced shot. Assert the phase sample is rejected with `onset_ff_fence=1`, the landing-lead window does not grow, and `Release onsetff` is logged.
- Confidence: confirmed (code-proven).

### [CL2-P6-003] medium — The dev fire-offset sweep does not fence the banner trim or the release oracle; a deliberately late shot stepped the persisted trim earlier on 09-22
- Lane: P6
- Exploit / failure path:
  1. The sweep's own banner says "phase/feedforward/lead learning is FENCED for this session" (`:17331-17336`). `devFireOffsetArmed_` is checked at `:13270`, `:13342`, `:13349`, `:13389`, `:20823`, `:22263`, `:22329`, `:22755` and `:23069`. It is not checked anywhere in `observeBannerVerdict` (`:15480-`) or the oracle sweep (`:15815-`). Those check only `onsetFfMs`.
  2. Live, run A:
     - `22:05:53.699Z DEV FIRE OFFSET APPLIED … applied_ms=23.72 shot_attempt=44` (FF `one_sided`, 0 ms).
     - Then `ORACLE: epoch=44 gap_px=4.0 proxy=miss … used=1`.
     - Then `22:05:56.939Z BANNER TRIM: verdict=MISS type=Standstill trim_ms=+3.0 -> +6.0 … source=oracle … reason=step`.
     - A fire displaced **24 ms late** on purpose moved the trim **3 ms earlier**, the wrong-way learning trap.
  3. Every run A release had a dev draw. 164 `BANNER TRIM: calibrating` verdicts plus oracle steps fed the trim through the whole sweep. `banner_lead_trim_by_type` is persisted to learning.json (`OrionAppController.cpp:3287-3291`) and only decays 50 % at the next start.
- Affected: `AutomationEngine.cpp:15480-15560`, `:15815-15870`; the owner's learning.json.
- Impact:
  - It is dev-build only, so no customer is affected.
  - The owner's calibration carries sweep residue into the next session.
  - The trim moves the base deadline in the middle of the sweep, which confounds the sweep's own offset→verdict regression.
- Reproduction (high level): arm `ORION_DEV_FIRE_OFFSET_SWEEP=uniform:-25:25` and grep for `BANNER TRIM: verdict=… reason=step` while the sweep is armed.
- Required fix: refuse the verdict and the oracle when `devFireOffsetArmed_` (the same `COVERAGE_EXCLUDED` treatment as `onsetFfMs != 0`, with `reason=dev_offset`). Also stamp the dev offset into `BannerTrimRelease` if the owner wants a per-shot rather than a per-session fence.
- Verification test: with the sweep armed, a LATE or oracle-MISS verdict on a displaced epoch leaves `bannerLeadTrimMsForTests` unchanged and logs `ignored reason=dev_offset`.
- Confidence: confirmed (seen in the log and code-proven).

### [CL2-P6-004] medium — The FF fence has no magnitude floor: at the shipped 0.2/10 setting it discards about 45 % of every learner's evidence, much of it for sub-millisecond displacements
- Lane: P6
- Exploit / failure path:
  1. `decide()` applies any `|offset| ≥ 0.05 ms` (`OnsetFeedforward.h:145`). All five fences test `!= 0.0`.
  2. One-sided by construction, about half of all shots (onset above the bucket median) are displaced.
  3. Live, run B (shipped setting, 202 releases):
     - 92 were displaced (46 %);
     - 23 of those by <1 ms and 41 by 1–4 ms, so 70 % were under one 4 ms tick;
     - the whole log shows 256/517 displaced, 55 of them under 1 ms.
     - Example: `22:00:52.164Z ONSET FF: reason=applied … applied_ms=-0.05`.
  4. Each such release loses its latency-estimator marker (the estimator's **only** input), its banner verdict, its oracle and its phase sample.
- Affected: `OnsetFeedforward.h:145`; the fence sites `:13235`, `:15547`, `:15851`, `:22758`, `:23070`.
- Impact:
  - The already-starved banner trim (it moved once in 74 verdicts per the FF header) and the latency estimator lose half their evidence.
  - The loss is not random: it is the late-onset half. If per-shot transport latency correlates with online onset (plausible: same network), the surviving markers under-measure latency and the measured lead reads short, which means later fires. **(Speculative; see the verification test.)**
- Reproduction (high level): count `Release onsetff applied_ms` values ≠ 0 against the total, per session.
- Required fix:
  - Raise the zero floor in `decide()` to a physically meaningful size (for example 1–2 ms, below which the displacement is also not applied).
  - Or fence only when `|applied| ≥ floor`.
  - For the phase sample and landing lead, prefer **correcting** by the known displacement (subtract it, as the type/press trims already are at `:22743-22748`) over discarding.
- Verification test:
  - Unit: a 0.3 ms decision yields `reason=zero`, the marker is emitted and the trim observes.
  - Offline: across the 09-21/22 corpus, regress marker-estimated latency on `onset − ref`. A non-zero slope confirms the selection bias.
- Confidence: confirmed for the starvation; speculative for the bias.

### [CL2-P6-005] low — Late carry (dev-only) edge cases: no court-change reset, cached across a type upgrade, stacks outside the unified early ceiling, verdict word parsed differently from the trim, no tests
- Lane: P6
- Failure paths:
  1. **No court-change reset.** `noteContextChanged` (`:15281-15297`) resets the FF ring but not the `lateCarryPrev*` / `lateCarryShot*` state, which only `reset()` clears (`:5101-5109`). A LATE on the old court less than 7 s before the change (`kLateCarryWindowMs`, `.h:6477`) carries into the first shot on the new court.
  2. **Cached across a type upgrade.** Eligibility needs `bucketFor(shot_.shotType)=="Standstill"` at the first decision. The result is then cached per physical epoch (`:15349-15350`). A 200 ms blind type upgrade to a fade re-arms inside the same epoch, and the fade keeps the carry. `lateCarryPrevBucket_` is stamped from the *release-time* type (`:15312`): the same arm-vs-release skew as B-7.
  3. **Outside the unified early ceiling.** The FF is bounded by "trim + FF ≤ banner_trim_max_ms" (`:17629-17647`), but the carry adds up to `kLateCarryMaxMs=15` on top. The worst case is 30 ms earlier at the defaults and 55 ms with a 40 ms A/B arm. The sweep adds more: run A's worst trim+FF+dev was 42 ms earlier.
  4. **Verdict word parsed differently.** `noteLateCarryVerdict` uses `startsWith("LATE")` (`:15336`), while `BannerLeadTrim::codeFor` uses `contains("LATE")` (`BannerLeadTrim.h:430`). The game has a "SLIGHTLY LATE" verdict (`tools/timing/banner_reader.py:35`). The shipped template library holds only EARLY/EXCELLENT/LATE today, so this is latent.
  5. **Conflated logging.** The carry is folded into `schedFireAppliedOnsetFfMs_`. `Release onsetff applied_ms` and `onset_ff_ms` then report FF + carry, while the `ONSET FF` line reports the FF alone, so an FF A/B graded while the carry is armed is contaminated.
  6. **No tests.** `native_orion/tests/AutomationEngineTests.cpp` has no late-carry case.
- Affected: `AutomationEngine.cpp:15281-15297`, `:15299-15397`, `:17680-17697`.
- Impact: dev-only experiment integrity. In a production build the carry is unreachable.
- Required fix:
  - Reset the carry in `noteContextChanged`.
  - Re-evaluate eligibility when `bucketFor` changes inside an epoch.
  - Include the carry in `boundMs`, or document the separate ceiling.
  - Use `contains("LATE")` (the same helper as the trim).
  - Log the carry in its own `Release latecarry` field.
  - Add a test for each item.
- Verification test: unit tests for court change → `no_previous`; Standstill→Left Fade upgrade → carry 0; trim 15 + FF + carry ≤ ceiling; "SLIGHTLY LATE" → eligible.
- Confidence: confirmed (code-proven).

### [CL2-P6-006] low — The onset-FF env knobs, including the randomised A/B, are live in a production build and bypass the signed settings
- Lane: P6 (touches L5)
- Failure path:
  1. `ORION_ONSET_FF_GAIN`, `_CLAMP_MS`, `_WINDOW`, `_MIN_SAMPLES`, `_ONE_SIDED` and `ORION_ONSET_FF_AB` are read in `applyConfig` (`:1603-1655`) outside any `#ifndef ORION_PRODUCTION_BUILD`.
  2. An `ORION_ONSET_FF_AB` arm with `one_sided=0` displaces fires **later** as well. The trim bound caps only the early side (`:17641`). The run A log shows 53 later applications, up to +40 ms.
  3. Values are clamped into the settings bands (gain ≤1, clamp ≤40), so this is bounded self-tamper only: nothing is unlocked. It is still a randomised timing experiment reachable on customer machines, outside the settings signature.
- Affected: `AutomationEngine.cpp:1603-1655`, `OnsetFeedforward.h:185-218`.
- Required fix: wrap the A/B parse (and preferably the other five knobs) in `#ifndef ORION_PRODUCTION_BUILD`, as was done for the late carry. Bound the later side by the clamp and the trim together.
- Verification test: a production-define unit/compile test showing `onsetFfAbArms_` stays empty with the env set. Add `ORION_ONSET_FF_AB` to `security_audit`'s production-string scan.
- Confidence: confirmed (code-proven).

### [CL2-P6-007] low — Hygiene
- The comment at `:15424-15426` says the ring stamp is "normalised already by the seq-paired `Release onsetff` line above". The stamp actually runs *before* that line (`:13265` vs `:13287`) and relies on the `:13234` normalisation. The behaviour is correct, but the comment is misleading for the next edit, and CL2-P6-002 is exactly that kind of ordering trap.
- A METER BACKSTOP release (`:22064`) reaches `noteBannerTrimRelease` without passing `:13234`, so its ring entry takes the *previous* scheduled shot's `lastReleaseOnsetFfMs_`. This fails safe (at worst a verdict is wrongly excluded), but it should be normalised to 0 at the backstop.
- `noteContextChanged` still hard-codes four bucket names for `dropped_onsets` (B-7 item 3, still open).

---

## Evidence for known IDs (not new)

**GM-002 / CX-001 (learning.json backup not semantically validated), with two more paths:**
1. **An empty object destroys the good backup.** `reloadLearning` treats a valid-but-empty `{}` as `Ok` and loads defaults (`AppConfig.cpp:275-280`). The next `saveLearning` sees `readObjectStatus == Ok` and replaces the good `.bak` with `{}` (`AppConfig.cpp:1254-1259`, the unchecked `remove`+`copy` per CX-008).
2. **The backup is only one save old.** The backup rotates on **every** `saveLearning`, and several learners save per shot (velocity prior, no-meter hold, trim). The `.bak` is therefore always one save behind, not a "last good" generation. A drift such as CL2-P6-001 or CL2-P6-003 is in `.bak` one shot later.

**B-7 (09-21):** still open, unchanged.

---

## Verdict

**needs changes**

No finding here is a customer-facing blocker at the shipped 0.2/10 one-sided setting, and every dev hook is truly compiled out of a production build. But the "every displaced release is fenced from every learner" claim is false in production (CL2-P6-001) and in two owner experiment configurations (CL2-P6-002, CL2-P6-003). Also, the fence as written discards about half of all learner evidence (CL2-P6-004). Fix CL2-P6-001 before promoting the 0.45/40 arm.

**Top 5 fixes, in priority order**
1. **CL2-P6-001:** one `releaseDisplacedForLearners()` predicate, applied to the press-tip/no-meter-hold, velocity-prior, clock-seed and vision-confident sites.
2. **CL2-P6-002:** stop zeroing `lastReleaseOnsetFfMs_` when gain is 0 but an A/B or carry arm is live.
3. **CL2-P6-003:** fence the banner trim and the oracle while the dev sweep is armed.
4. **CL2-P6-004:** a physical magnitude floor for FF application and fencing, and correct rather than discard where the displacement is known.
5. **CL2-P6-006 + CL2-P6-005:** compile the FF A/B out of production builds, and fix the carry's court reset, type-upgrade cache, ceiling, word parse and tests before it graduates from dev.
