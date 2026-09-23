# P-D timing/controller patch: RT-MED-01, RT-MED-02, RT-MED-03, RT-MED-04 (+CL3-F8-009), RT-LOW-06

Author: Claude patch agent P-D, 2026-09-23. Scope is source and tests only. Nothing was built,
launched, committed or deployed. The owner's `settings.json` and `learning.json` were read only, to
check the new bands against them, and were never written. Timing defaults are unchanged: left fade
-6, FF gain 0.2 / clamp 10 / 2 ms floor, and Pill still withdrawn.

**Status:** code-complete, **not compiled**. The native tests below were written fail-first and
still need the central build.

**Python contract tests that I ran:** 20 source-contract files, **246 passed, 1 xfailed, 2 xpassed**.

## Native build list (Claude builds centrally)

| Target | Why |
|---|---|
| `OrionCommon` | `AppConfig.{h,cpp}`: the style rule, the learning bands and the lead-provenance helpers |
| `AutomationCore` | `AutomationEngine.{h,cpp}`: the new signal, the accessor and the learner fences |
| `OrionNative` | `OrionAppController.{h,cpp}` and the new header `MeterBlindnessLatch.h` (header-only, no Q_OBJECT, no CMake change) |
| `OrionNativeTests` | `tests/AutomationEngineTests.cpp` |
| `OrionRemotePlayPathTests` | `tests/RemotePlayExecutablePolicyTests.cpp` |

### Tests to run

- `OrionNativeTests`:
  - `onsetFeedforwardFencesPressTipHoldAndReleaseClockLearners`
  - `learningSemanticGarbageIsQuarantinedAndNeverRotated`
  - `meterBlindLatchTripsOnUnansweredMeterPresses`
  - `meterPressUnansweredSignalCarriesTheEpoch`
- `OrionNativeTests` regression neighbours:
  - `onsetFeedforwardFencesPhaseAndTrim`
  - `onsetFeedforwardIsBoundedByTheBannerTrimAndResets`
  - `pressTipObservationUnderDelayIsLoggedButNotLearned`
  - `learningRecoversFromBackupWhenCorrupt`
  - `learningWithoutBackupQuarantinesAndDefaults`
  - `meterVisionPrioritySuppressesDefaultBlindBackstop`
  - `meterBlindBackstopHonoursItsFlag`
  - `meterBlindBackstopStandsDownWhileDetectionUnavailable`
  - `meterModeNeverAnswersAnUnownedPressBlindly`
  - `gotoMeterWaitBlindFiresAtSafetyCapNeverHangs`
  - `visionHoldBand*`
  - the full binary
- `OrionRemotePlayPathTests`:
  - `savePersistsPillBetaAndDefaultsUnknownStyles` (updated)
  - `loadMigratesLegacyMeterStylesToArrow2` (new, dev build)
  - `leadCalibrationCancelRestoresExactProvenance` (new, dev and prod)
  - `nonPillStylesKeepTodaysSidecarEnvironmentByteForByte` (unchanged, still passes raw styles to the route)
- Python: `tests/test_venice_ui_contract.py`, 23 passed locally.

### Process note

In `OrionAppController.cpp`, most changes were small Edit-tool edits. One exception was a single
Python read-modify-write, with a window of a few milliseconds, that replaced the
`observeMeterBlindness` body and the head of `observeBotOwnership`. Afterwards I confirmed that the
concurrent edits by the other agents are intact in the diff: the `startUpdate`, safe-mode,
watchdog and `flushPendingLogs` hunks are all still present. Codex should still re-diff the
update-elevation function.

---

## RT-MED-01 / CL3-F4-009: Calibration Cancel restores the exact tuple

**Defect:** Cancel restored through `setActuationLeadMs()`. That function clamps to [150, 800] and
latches `user_set=true`, so:
- An Auto install `(0, false)` came back as `(150, true)`, or as wherever the last step left it.
- A measured seed `(270, false)` lost its seed status permanently.
- The log line reported the wrong value.

**Fix:**
- `AppConfig.h` (after `mirrorActuationLeadIntoSourceStash`) adds:
  - `ActuationLeadProvenance {leadMs, userSet, routeKey}`
  - `captureActuationLeadProvenance()`
  - `restoreActuationLeadProvenance()`, which writes the tuple back verbatim, re-mirrors the per-route stash, and returns `Restored`, `Unchanged` or `RouteChanged`
  - `describeActuationLeadProvenance()` for the log line
- `OrionAppController.h` (~978) replaces `leadCalStartLeadMs_` with two members:
  - `leadCalStartLead_`
  - `leadCalWroteLead_`, which is set only when a graded step actually persisted a lead
- `OrionAppController.cpp`:
  - `beginLeadCalibration` (~9483) snapshots the tuple.
  - `reportLeadCalibrationVerdict` (~9561) marks the write.
  - `cancelLeadCalibration` (~9500-9535) restores the snapshot without clamping or latching `user_set`, then saves.
  - The Cancel log now reads `restored the lead to Auto (not set; ...)` or `... 270 ms (measured on this setup)` or `... 280 ms (your value)`.
  - The banner tally is reset only when the tuple actually changed.
  - If the video route changed mid-calibration, Cancel writes nothing and says so.
- **Behaviour kept:**
  - Lock/Done still keeps the calibrated value.
  - Cancel with no persisted step logs "unchanged".

**Tests:**
- `RemotePlayExecutablePolicyTests::leadCalibrationCancelRestoresExactProvenance` covers:
  - Auto, seeded and user tuples, each put through a modelled old step (`stepLikeSetActuationLeadMs`) and then restored exactly, including the stash
  - Auto stays Auto: no stash entry, and a measured seed is accepted again
  - a route switch writes nothing
  - the log wording
- `tests/test_venice_ui_contract.py::test_lead_calibration_cancel_restores_exact_provenance` pins the controller wiring, since the controller has no native harness. It fails against HEAD.
- `test_shot_verdict_tally_is_one_component_under_both_tuning_sliders`: the `resetBannerTally()` count goes 4 → 5, with a comment.

**Residual (not changed, flagged):**
- On an Auto install, `begin()` starts the bisection at 300 ms but does not apply it. The first verdicts therefore grade whatever lead the engine was actually using.
- If three GOODs lock at 300 without ever moving, the "Locked at…" message names 300, but nothing is persisted.
- This is pre-existing and needs an owner decision; it is not part of the Cancel defect.

## RT-MED-02 / CL3-F4-008: every learner fenced on the feedforward displacement

**Defect:** only the phase constant, the marker, the banner trim and oracle, and the landing lead
refused FF-displaced releases. These still learned from them:
- press→tip
- the no-meter hold
- the per-type clock seeds
- the velocity prior
- the global clocks
- the lead-outcome confidence bit

On 09-23, 6 of 6 FF-applied epochs logged `PRESS-TIP ... accepted=1`.

**Fix (`AutomationEngine.{h,cpp}`):**
- **One predicate.** `releaseLearningDisplaced()` (h ~5700) is `devFireOffsetArmed_ || lastReleaseOnsetFfMs_ != 0`. `lastReleaseOnsetFfMs_` is already normalised in `triggerRelease` to what this release carried, and to 0 for an in-tick release. It is now used at:
  - `lastReleaseVisionConfident_` (cpp ~13364)
  - the per-type FF and meter clock seed (~13373)
  - the velocity prior (~13414)
  - the global appear, hold and rise EMAs (~13440)
- **New sample-and-hold `meterCapOnsetFfMs_`** (h ~6200), set in `startPostReleaseMeterCapture` (~19217). The delayed learners judge **this** release, not whichever one fired last:
  - no-meter hold gate (~22320)
  - PRESS-TIP `accepted` (~22391): the line is still logged, now with `accepted=0`
- **Else-branch zeroing removed.** The branch that zeroed `lastReleaseOnsetFfMs_` when the configured gain was 0 (~13319) is gone. Only a dev A/B arm could displace in that state, and zeroing it unfenced every learner (CL2-P6-002). With gain 0 and no arms, the value is already 0, so there is no production change.
- **Floor unchanged.** The 2 ms floor is in `OnsetFeedforward.h` and the final-zero clip. A sub-floor shot carries 0 and still trains. `OnsetFeedforward.h` was not edited.

**Tests:** `onsetFeedforwardFencesPressTipHoldAndReleaseClockLearners` has three parts:
- **(a) Post-release learners.** A perfect landing with displacement 0 is accepted, learns press→tip and learns the hold. With -6 ms the result is `accepted=0`, weight 0 and no hold.
- **(b) Real `triggerRelease`.** Uses the route-binding fixture, marked scheduler-fired. The undisplaced control seeds the Standstill clock and is vision-confident. A -6 ms release seeds nothing, is not confident, and the capture window carries -6.
- **(c) The predicate.**

## RT-MED-03 / CL3-F4-006: semantic bands on learning.json

**Defect:** `loadLearningObject` accepted any finite double for most fields. The `.bak` rotation also
promoted any *syntactically* valid file, so garbage became "last good".

**Fix (`AppConfig.cpp`):**
- **One band table** in the anonymous namespace (~121-185), `kLearningScalarBands` and `kLearningMapBands`. Each band follows the engine's own learner band or clamp, slightly widened. 0 stays legal wherever the engine reads 0 as unarmed.
  - `bias_pct` ±10
  - `learned_latency_ms` and `shot_type_latency_ms` ±130
  - learned offsets ±120
  - RTT baselines ±100
  - clocks 0..3000 or 0..6000
  - `probe_spawn_offset_ms` 0..2000
  - velocity 0..2
  - cal phase 0..2
  - per-level nudges ±20
  - phase fields 200..500
  - no-meter hold median 200..4000 with n ≥ 1
  - banner trim within ±`kPersistCeilingMs`
  - Wrong JSON types are also violations.
- **Validator.** `AppConfig::learningSemanticViolations(obj)` (public static, h ~2161; cpp ~331) returns the offending keys, never values.
- **Per-field loader.** `loadLearningObject` (~2396): each field goes through its band, and an out-of-band value keeps the default.
- **`reloadLearning` (~450).** A syntactically valid but semantically bad file is renamed to `learning.json.invalid-<utc>`. Then:
  - If the `.bak` also passes the check, the `.bak` is restored.
  - Otherwise the in-band fields of the bad file are kept and the rest fall to defaults.
  - In both cases the launcher logs `learningLoadNote` once.
- **Unreadable-file path (~488).** It now also refuses a semantically bad `.bak`.
- **`saveLearning` (~1459).** It rotates the current file into `.bak` only when that file is semantically valid.
- **Checked against real data.** The owner's repo `learning.json` and the `%LOCALAPPDATA%` `learning.json` both pass the bands. Examples: `learned_phase_physical_ms` 271 and 240, `no_meter_hold` up to 2044 ms, `probe_spawn_offset_ms` 178.2.

**Tests:** `learningSemanticGarbageIsQuarantinedAndNeverRotated` has four parts:
- **(a) Validator.** Covers the owner-shaped file, the empty file, and the finite extremes `1e300`, `-1e9`, `bias_pct` -60, negative clocks and wrong types, for every scalar and every map.
- **(b) Good backup present.** The bad file is quarantined as `.invalid-*`, the backup is restored, and `bias` is 0 with no 1e300 offset.
- **(c) No backup.** The bad fields fall to defaults, the in-band phase value 271 is kept, and the file is not rewritten on load.
- **(d) Rotation guard.** A semantically bad current file is never promoted over a good `.bak`.

## RT-MED-04 / CL3-F4-007 + CL3-F8-009: meter-blind warning and DETECTION UNAVAILABLE latch

**Defects:**
- **The streak never advanced for Square presses in METER mode.** It keyed on `shot_.physicalShotEpoch`, which only `beginShot` sets. A meterless METER press never begins a shot; it ends as `press_unanswered_no_meter`.
- **Idle raw sightings reset the streak.** This could starve the trip indefinitely.
- **Recovery counted any Holding or GreenWindow epoch.** That includes Go-To `await_meter` pushes with no meter.

**Fix:**
- **`src/MeterBlindnessLatch.h` (new, pure, header-only).** It holds these rules:
  - **Streak:** counts completed unanswered presses, one per epoch, with hold ≥ 300 ms. Taps, pump fakes and menu presses do not count.
  - **Raw detections:** count only with a non-zero active press epoch. An in-press sighting resets the streak and exempts that press from being counted.
  - **Trip and recovery:** the latch trips at 3 presses and clears after 2 distinct vision-owned shots. While latched, a blip clears the streak but not the warning.
- **Engine.**
  - New signal `meterPressUnanswered(epoch, holdMs)` (h ~4063), emitted once per epoch at the `press_unanswered_no_meter` terminal (cpp ~8641). It is observational only; the physical pass-through is unchanged.
  - New accessor `activePhysicalPressEpoch()` (h ~6255): the owned shot's epoch, otherwise the pending Square press's epoch, otherwise 0.
- **Controller.**
  - The telemetry call site (~3036) now passes `automation_.activePhysicalPressEpoch()`.
  - A connect (~3733) relays the engine signal to `observeMeterPressUnanswered`.
  - `observeMeterBlindness` handles only NO METER reset and in-press sightings.
  - `applyMeterBlindLatchEvent` mirrors the latch into `meterBlindWarning_`, `detectionUnavailable_` and `AutomationEngine::setDetectionUnavailable`, and writes the log and signal.
  - `observeBotOwnership` recovers only on `meterSeenThisShot` in Holding, GreenWindow or Releasing, with inputTimed excluded.
  - The trip log is reworded, because "blind timer shots are OFF" described nothing that changes in METER mode: `DETECTION UNAVAILABLE: 3 shot presses in a row got no meter - Venice is not timing shots until 2 shots with a visible meter prove detection is back.`
  - NO METER mode now resets the whole latch, including the engine gate.
- **Header members.** The old ones (`meterBlindEpoch_`, `meterBlindEpochSawMeter_`, `meterBlindStreak_`, the `detectionRecovery*` fields and both constants) are replaced by `meterBlindLatch_`.
- **Silent-fire protection unchanged.** `applyConfig` still hard-codes `greenWindowPriority`. The terminal still reports `backstop=green_window_priority`, and the engine gate still stands the backstop down.

**Tests:**
- `meterBlindLatchTripsOnUnansweredMeterPresses` covers these cases:
  - 3 long presses with an idle sighting between #2 and #3 trip the latch (this failed under the old rule)
  - duplicate epochs, taps and NaN holds never count
  - an in-press sighting resets the streak and exempts that press
  - while latched, a blip keeps the warning
  - two distinct vision-owned shots recover it, and the same epoch twice does not
  - a Go-To `await_meter` context has `meterSeenThisShot=false`
  - reset clears everything
- `meterPressUnansweredSignalCarriesTheEpoch` uses the shipped config with `greenWindowPriority` on and checks:
  - the active press epoch is 4405 while the press is held
  - exactly one signal fires after release, with that epoch and a hold of at least 300 ms
  - no bot release and no `METER BACKSTOP: fired`
  - the press passes through
- `test_venice_ui_contract.py` pins the new wiring. It also asserts the old `shot_.physicalShotEpoch` call site is gone.

**Replay check (rule applied to the owner's logs, approximate):** "owned" was proxied by the
PRESS-TIP, onsetff and PRECISE lines, and in-press sightings could not be seen.
- `orion_native.log.1`: 4 long unanswered presses, 227 owned, **0 trips**.
- `orion_native.log` (09-23): 24 long unanswered presses, 107 owned, **3 trips**, at:
  - 01:09:53Z
  - 01:18:14Z
  - 17:03:17Z. This trip coincides with `Detector frame rejected: reason=dark_frame` at session start, when the meter genuinely could not be read, so the warning is correct there.

  The 01:09 and 01:16-01:18 clusters are runs of 3 to 6 consecutive 0.45-1.8 s meterless Standstill presses. The owner should confirm those were real meterless windows.

**Residual risk:** long Square holds with no meter (for example holding Square outside a
shot) can raise an advisory false positive. It clears after 2 owned shots, and it changes no firing
in METER mode. The threshold is `MeterBlindnessLatch::kMinShotHoldMs`.

**Not done:** replaying the altered, missing and false-lock corpora through the compiled sidecar,
which the report asks for. That needs a build and the Astra reader lane.

## RT-LOW-06 / CL3-F4-003: meter style normalised to the offered set

**Fix:**
- `AppConfig.h` `normalizedMeterStyle` (~73) now returns the canonical `"Arrow2"` for every input. That covers Arrow, Dial, Straight, Sword, Pill, unknown values and any spelling of arrow2.
- `AppConfig.cpp` load (~1574) logs `METER STYLE MIGRATED: stored meter_style '<x>' is not offered; using Arrow2.` whenever the stored value differs.
- The existing ingresses inherit the rule unchanged:
  - `save()` persists `Arrow2`.
  - `setMeterStyle` accepts only `"Arrow2"`.
  - `switchProfile` already logs `Profile '<p>': meter style '<x>' is not available; using Arrow2.`
- The Pill route stays compiled but unreachable, as the existing Python guard requires.

**Tests:**
- `savePersistsPillBetaAndDefaultsUnknownStyles` now asserts that every legacy style maps to Arrow2. Its old row `"  Straight " → "Straight"` is the one that failed before the fix.
- New `loadMigratesLegacyMeterStylesToArrow2` (dev build) checks that Straight, Dial, Sword, Arrow, Pill and Mystery all load and re-save as Arrow2.

**Not done:** the packaged `meter_styles/*.json` for Arrow, Dial, Straight and Sword are still in
the package. Excluding them belongs to the packaging lane (`tools/package_orion_release.py`), which
I do not own. They are now unreachable from settings and profiles.
