# P-G: ship parity (2026-09-23)

Source: `docs/redteam/2026-09-23-final/SHIP_PARITY_AUDIT.md`. This patch applies R1-R8, R10 and R11. **R7 is included** because the owner approved it mid-task ("yes you have my ok"). R9 (optional `ORION_PROBE_LEAD_PERSIST`) is not applied.

**Status: nothing native was built or run.** Only focused Python contract tests were run. No commits, no deploys. The owner's dev and installed `settings.json` / `learning.json` were not touched. Running processes were not touched.

## 1. Changes

| # | What | Where |
|---|---|---|
| R1 | `ORION_TIP_PHASE_SOLO` added to `kShippedNativeTimingProfileFlags`. Production force-pins it to `1`. The compiled `tipPhaseSolo=false` stays. | `native_orion/src/SidecarReaderProfile.h` |
| R2 | New `kShippedNativeTimingProfileValues` holding `{"ORION_CURVE_STRETCH_ALPHA", "0"}`. `applyShippedNativeTimingProfile` resolves it like the flags: production force-pins, dev keeps an explicit value, and silence gets the profile value. The 0.6 source default in `AutomationEngine.h` is untouched. The reader-lane Python files are untouched. | same |
| R3 | `kShippedTimingProfileId` changed from `2k27-2026-09-04-v2` to `2k27-2026-09-23-v3`, with both pins updated. | `SidecarReaderProfile.h`, `tools/quality/consistency_bench.py`, `tests/test_consistency_bench.py` |
| R4 | `tipPhaseAnchorBase20 = true` | `native_orion/src/AppConfig.h` (the loader inherits the member default) |
| R5 | `tipPhaseTypeTrimEnabled = true` (the LF -4 / RF -6 map was already the default) | `AppConfig.h` |
| R6 | `ownershipProofTwoFrame = true`. `anchorRiseMinPct` stays 3.0, and a test pins that pairing. | `AppConfig.h` |
| R8 | `noMeterFadeTrimMs = 6.0` in three places: the header member, the loader fallback, and the `save()` NaN fallback | `AppConfig.h`, `AppConfig.cpp` |
| R7 | `tipPhaseAimFrozen = true`. New `LearningData::kShippedPhasePhysicalMs = 271.0` (canonical base-30). `AppConfig::applyShippedPhasePrior()` installs it whenever the learning slot has no prior; it runs in the constructor and on every exit of `reloadLearning()`. The `LearningData{}` struct default stays -1, so engine fixtures keep the seed path. | `AppConfig.h/.cpp` |
| R7 | One-time learned-aim reset in `load()`, gated on `kAimResetSettingsVersion = 3` (details below) | `AppConfig.cpp` |
| R7 ordering | The `frozenAimPhysicalMs_` latch moved to after the base-20 constellation block (see §4) | `native_orion/src/AutomationEngine.cpp` (`applyConfig`) |
| R10 | `kSettingsVersion` bumped from 2 to 3. Five append-only v3 rules were added (below). The stale "never confirmed by a counted live batch" comment for two-frame and base-20 was rewritten with the live evidence. | `AppConfig.h`, `AppConfig.cpp` (`settingsMigrations`) |
| R11 | Pins updated. `docs/SHIP_CONFIG.md` gains the §3b rows and the §2 profile row, a new §7, and the left-fade row fixed from `8.0` to **`-6.0`**. | see §2 |

### The v3 rules

All five use the existing runner. A value is rewritten only if the key is absent or still at the OLD default. Once a file is stamped v3 it is never migrated again.

| Key | Old default | New value | Companion veto |
|---|---|---|---|
| `tip_phase_anchor_base20` | false | true | none |
| `tip_phase_type_trim_enabled` | false | true | none |
| `ownership_proof_two_frame` | false | true | none |
| `no_meter_fade_trim_ms` | 0 | 6 | none |
| `tip_phase_aim_frozen` | false | true | `tip_timing_user_set` |

**Why migrating an old-default value is safe here.** In general you cannot tell whether a stored old-default value was a deliberate choice. For these keys we can:
- None of the four booleans has ever been in the customer UI.
- The fade-trim slider is on the NO METER card, and that card is unmounted in the customer build.
- So a stored `false` or `0` is `save()` echoing the old default, not a choice.
- Any other stored value is left alone, and any change made after v3 is never re-migrated.

### The one-time aim reset (R7, owner-approved)

It is decided from the **pre-migration** settings object. It runs only when all of these hold:
- `settings.json` exists (it is an existing install),
- its `settings_version < 3`,
- `aimIsUserOwned()` is false.

`aimIsUserOwned()` is true when `tip_timing_user_set` or `tip_phase_aim_frozen` is present and not `false`. A malformed value counts as owned. `tip_phase_aim_frozen = true` counts as owned because before v3 the default was false, so a stored `true` is evidence of a lock the user chose.

When the reset runs:
- `learning.json` `learned_phase_physical_ms` is set to 271 and persisted once.
- `measured_phase_physical_ms` is untouched.
- One log line is written: `[AppConfig] settings migration v3 applied: learned_phase_physical_ms X -> 271.0 ...`.

Results for the known installs:
- **Fresh install** (no `settings.json`): the prior lives in memory only and nothing is written. The existing "fresh load never writes" and quarantine tests still hold.
- **Owner's installed copy** (v2, unfrozen, not user-set, 291.63): reset to 271 and frozen.
- **Owner's dev copy** (`tip_timing_user_set` and frozen both true, already 271): untouched. The next dev launch only re-stamps the file at v3. Every other v3 key is already at its new value.

### Settings-signature transaction (RT-MED-09)

The migration runs inside `config_.load()`. The existing startup path wraps that load in `beginSettingsWrite` and then `commitSettingsWrite`, but only when the pair verified before load (`OrionAppController.cpp` ~1990-2010). So the v3 rewrite of `settings.json` is re-signed and logged as "settings upgraded for this version and re-signed", the same way the v2 step was.

`learning.json` is not part of the signed pair. The aim reset writes it with the normal `saveLearning()`, so no second transaction is needed.

If the pair did not verify, the existing bootstrap and "Repair settings" path is unchanged.

### Meter delay: verified as a no-op, no change

`kMeterDelayShelved = true` and `meterDelayActive(d) = !kMeterDelayShelved && d.meterDelayEnabled` (`OrionAppController.cpp:96-101`). Every runtime consumer goes through `meterDelayActive`: the actuator enable, the packet-bridge link, the backend checks, and the sidecar input. The engine never reads `meterDelayEnabled`. So the installed file's `meter_delay_enabled=true` does nothing. `tests/test_ship_parity_20260923.py::test_meter_delay_setting_cannot_arm_the_shelved_feature` pins this: no raw read of the flag outside the gate and the setter.

## 2. Tests

### Python (run, all green)

Command:

`.venv\Scripts\python.exe -B -m pytest tests/test_ship_parity_20260923.py tests/test_ship_defaults.py tests/test_shipped_reader_defaults.py tests/test_consistency_bench.py -q -p no:cacheprovider --basetemp C:/Users/aaron/AppData/Local/Temp/parity-tests`

Result: **72 passed**. Also run: `test_capture_fps_env.py`, `test_venice_live_page_cleanup_contract.py` and `test_venice_ui_contract.py` (50 passed).

Fail-first status:
- `tests/test_ship_parity_20260923.py` (new, 15 tests) was run before the source edits. It gave 9 failed / 6 passed.
- The 6 that passed were the R1-R3 profile pins, the loader-inherits-member pin and the meter-delay pin. The profile header had already been edited when the file was first run. Against HEAD the R1-R3 pins fail trivially: `ORION_TIP_PHASE_SOLO` is not in the flag block, and `kShippedNativeTimingProfileValues` and the v3 id are absent.
- `tests/test_shipped_reader_defaults.py` now pins `ORION_TIP_PHASE_SOLO` in the native profile and the `{"ORION_CURVE_STRETCH_ALPHA", "0"}` row.
- `tests/test_ship_defaults.py` now pins the five new `AppConfig.h` defaults plus `anchorRiseMinPct 3.0`.

### Native (written, NOT built or run)

New tests:
- `RemotePlayExecutablePolicyTests::nativeTimingProfilePinsSoloAndStretchInProduction`
  - Production overrides inherited `SOLO=0` / `ALPHA=0.6`.
  - Dev keeps them.
  - Dev with no value set gets the profile values.
  - Checks the id `2k27-2026-09-23-v3`.
  - Saves and restores the process env.
- `AutomationEngineTests::shipParityV3MigrationCarriesTheValidatedConfig`
  - Uses the owner's installed shape: v2, explicit false/0, aim 291.63.
  - After load: all five migrated, aim 271 in memory and on disk, the measured median untouched, stamped v3.
  - A second launch does **not** reset again (288 survives, settings bytes unchanged).
- `AutomationEngineTests::shipParityV3MigrationKeepsDeliberateValuesAndUserOwnedAim` covers five cases:
  - user-set + frozen, with fade trim 9: trim and aim both kept;
  - frozen only: aim kept;
  - malformed companion: aim kept, and the freeze is vetoed;
  - a v3 file with explicit false/0: file untouched;
  - v2 with no `learning.json`: 271 is written once.
- `AutomationEngineTests::shipParityFreshInstallFiresLikeTheOwnersDevSetup` is **the equivalence test the owner asked for**.
  - The fresh side is `AppConfig` on an empty dir: no files written, factory prior 271.
  - The dev side is identical inputs plus the owner's explicit dev values and `learning.json` 271.
  - It checks: equal effective constant (= 271 + 58.3 + (393 - 319)), anchor 20, ladder, seed, two-frame, freeze, type trims, and fade blind holds.
  - It stages the **same shot** for Standstill and Right Fade and requires **bit-identical** `canonicalAutonomousTipDecision` tip and source.
  - After 8 far-off landings, both aims are still unmoved (frozen).
- `AutomationEngineTests::shipParityFrozenSeedLatchIncludesTheBase20Shift` is the ordering regression.
  - Setup: frozen, no prior, base-20 switching on in the first `applyConfig`.
  - After landings, the learned aim must equal the **base-20** seed.
  - Before the fix it latched 319, which is 58.3 ms early. This test fails-first against the old ordering.

Updated for the new defaults:
- `settingsMigrationVersion0FileAppliesOnlyTheRatifiedSafetyRule`: two-frame and base-20 are now migrated true, and it now also checks the rest of v3 and the aim.
- `settingsMigrationRegistryIsAnExplicitAllowlist`: version 3, six rules pinned by shape. Two-frame and base-20 were removed from `countedBatchOnly`, with the evidence cited.
- `ownersSettingsFileRoundTripsLosslesslyThroughMigration`: v3 rewrites are sanctioned exactly like the v2 one, and the fade-trim note is updated.
- `shipConfigDefaultsArePinned` and `shipConfigDefaultsSurviveAKeylessSettingsFile`: the new defaults, the loader fallbacks and the factory prior.
- `anchorBase20ConstellationMovesTogether` and `anchorBase20DatingUsesMeasuredRungOffsets`: the stock engine is now base-20, and base-30 is checked with an explicit flag.
- `aimFreezeHoldsTheLearnedConstantStill` and `tipTimingManualValueRoundTripsAndOutranksLearner`: the freeze now ships ON.
- `tipPhaseTypeTrimIsSymmetricAndDefaultOff`: the default is asserted ON. The OFF leg sets `tipPhaseTypeTrimEnabled`, `tipPhaseAnchorBase20` and `tipPhaseAimFrozen` false explicitly, because it is kill-switch coverage. The name was kept.

**Fixture pins, from a static fallout audit.** I read the whole of `AutomationEngineTests.cpp` and the other seven test files in five read-only passes. Tests whose subject is something else, but whose arithmetic assumed an old default, now pin the old value right after `AppConfigData` construction. Each pin carries the comment `// [SHIP_PARITY 2026-09-23] pre-ship default pinned`. There are 46 insertions across 36 functions/helpers, plus 4 hand pins:

- **`ownershipProofTwoFrame=false`** (the fixtures feed a 3rd-frame proof they assert):
  - `strictSquareProofSurvivesRejectedDecoderBlink`, `pendingSquareProofSurvivesInputPollDropoutRegularAndNoDip`, `intermittentSquareProofAndConsecutiveRegularNoDipAreOwned`, `readyStickRequiresCurrentEpochMeterProof`
  - `shotAbortIdentityCarriesTheClassifiedShotType`, `shotAbortIdentityIsEmittedFromEveryAbortSite`
  - `ownershipPromotionCensusPreservesRestartHistory`, `ownershipProofStillBreaksOnADetectorShapeChange`, `ownershipProofRunwayAwareGrantsSecondFrameOnlyWhenTight`
  - `tempoTipParityDefaultOffPinsTempoStickArmedDwell`
  - `latencyCalibrationProbeRequiresGenuineRisingMeter`, `coldStickCalibrationRejectsUnsafeProofAndCancelledEpoch`, `stickCalibrationAbortFenceRequiresThreeNeutralSamples`, `calibrationOwnershipCeilingBoundsEveryMode`, `latencyCalibrationRequiresAutonomousLiveMeterAuthority`
  - `automaticLatencyLeadConvergenceDoesNotStrandHeldSquare`, `cancellingOwnedLatencyCalibrationProbeRelinquishesWithoutRelease`, `automaticLatencyCalibrationPromotesOnlyGenuineRise`, `automaticLatencyCalibrationRejectsDelayedPriorShotEpoch`
- **`tipPhaseAnchorBase20=false`** (base-30 arithmetic or fixture):
  - `sessionLeadProbeTrimsTheLeadOnceFromTheSessionsOwnFreezeTiming`, `shotLeadConflictDiagnosedAtConfigAndOnMissedDeadline`, `meterDelayLeadOffsetHeadroomArithmeticIsOffsetBased`, `tipTimingFreezeDivergenceWarnsAndPersistsMeasuredMedian`
  - `slowMeterDeferHoldsUndercuttingArmThenFiresAtThePhaseDeadline`, `subtickMirrorHonoursThePhaseAnchorImminentHold`, `frameBoundaryWobbleDoesNotRearmTheToken`
  - `tipPhaseAnchorDatesTheFirstUpwardCrossing`, `phaseAnchorImminentHoldIsNarrowAndRisingOnly`, `rungImminentHoldExtendsToLadderRungsOnlyWhenFlagged` (the base-20 leg re-sets true), `tipPhaseLadderRescuesAMeterFirstSeenAboveTheBaseAnchor`, `tipPhaseIsMeanNeutralAtTheFiringDecision`
  - `sparseEarlyRiseCannotPrematurelyReleaseRegularOrNoDip` (at-risk only)
  - helper `runFrameNativeVisionArm`
- **Both base-20 and aim-frozen false** (learner tests): `phaseLearnerShrinksTowardTheObservationBeforeTheWindowFills`, `tipPhaseConstantLearnsPhysicalTermWithoutEatingTheAimOffset`.
- **`tipPhaseAimFrozen=false`**: `fadePhaseLearnerUsesReleaseTimeTrim` (otherwise it passes without testing anything).
- **`noMeterFadeTrimMs=0`**:
  - helpers `meterBlindBackstopConfig` and `noMeterConfig`;
  - local pins in `inputTimedUsesTheBlindHoldLaw` and `noMeterHoldLearnerRoundTripsAndOverridesTheTable`.

The audit of the other seven native test files found no breakage: `VeniceProfileTests`, `MeterDelaySettingsPropertyTests`, `RemotePlayExecutablePolicyTests`, `XboxRemotePlayPolicyTests`, `PreviewPresentationBufferTests`, `LauncherResponsivenessTests`, `RouteTransitionIntegrationTests` and `SettingsSignatureTransactionTests`.

**Residual risk.** The audit was static. About 500 fixtures build a default `AppConfigData`. Tests that use `promoteStrictAutonomousSquare` and the closed-loop helpers now own one frame earlier; the audit found no exact assertion on that frame, but only a run proves it. If the central run shows more fallout, the fix pattern is the same: pin the old value in the fixture whose subject is something else. Do not revert the ship default.

### Native targets to build and run (centrally)

- `OrionNative` (dev and production configs). Production also needs the packaged sidecar or installer rebuild.
- `AutomationEngineTests` (full).
- `RemotePlayExecutablePolicyTests` (full).
- Recommended as well: `MeterDelaySettingsPropertyTests`, `VeniceProfileTests`, `XboxRemotePlayPolicyTests`, `PreviewPresentationBufferTests` and `SettingsSignatureTransactionTests`. They are unaffected by the audit, but they share `AppConfig`.
- Then `scripts\verify_orion.ps1 -StrictSecurity`. The change touches the settings migration, signing re-sign and the shipped profile.

## 3. R7: what shipped (owner-approved)

This is "ship 271 frozen as the built-in prior, plus a one-time reset of existing installs' learned aim to 271":

1. `AppConfigData::tipPhaseAimFrozen` defaults to `true`, and the v3 rule `{3, tip_phase_aim_frozen, false -> true, companion tip_timing_user_set}` carries it to existing installs.
2. `LearningData::kShippedPhasePhysicalMs = 271.0` is the factory prior. It is installed by `AppConfig`, the persistence layer, and not by the `LearningData{}` struct default, which stays -1 so ~900 engine fixtures keep the seed path. Every product path gets it: the constructor, and every `reloadLearning()` exit including missing file, quarantine and backup restore.
3. The one-time reset of `learned_phase_physical_ms` to 271 happens in `load()`, gated as described in §1.
4. The effective aim equals dev: 271 canonical, +58.3 on base-20, + (393 - 319). The equivalence test pins this bit for bit on a staged decision.

**Caveats for the owner:**
- Aim and lead trade 1:1. 271 is this rig's validated aim with lead 274. A customer on a different jumpshot now starts frozen, and the learner does not move it until Tip Timing is unlocked. The card is unmounted in the customer UI.
- The divergence warning still runs. Auto-unlock is still off (v2).

## 4. The ordering finding: real, but not on the owner's dev path

**Audit claim:** `frozenAimPhysicalMs_` was taken at `AutomationEngine.cpp` ~1421, before the base-20 constellation at ~2503.

**Verdict: REAL for the seed-fallback branch only.**

The latch was:

`frozen = learning.learned > 0 ? learning.learned + phaseRestoreShiftMs : config_.tipPhaseSeedPhysicalMs`

- **Prior branch (the owner's dev setup: 271 + frozen + base-20): always correct.** `phaseRestoreShiftMs` is computed from `settings.tipPhaseAnchorBase20`, not from `config_`, so the latch was 271 + 58.3 from the first pass. That is why dev never showed the bug.
- **Seed branch (frozen, no learned prior, base-20 turning on in the same pass):** it read `config_.tipPhaseSeedPhysicalMs` **before** this pass moved it. It latched the base-30 seed, 319 (and without the meter-time scale), onto the base-20 clock.
  - Before the first landing, effective = `config_.tipPhaseConstantMs`, which is correct.
  - From the first accepted landing, `recordPhaseConstantSample` copies the latch into `learnedPhasePhysicalMs_`. The aim then drops to 319 + 74 = 393 against a correct 451.3: **58.3 ms early**.
  - It only fixes itself when a later `applyConfig` re-latches with the already-moved seed.
- **Before R7 this was reachable:** a fresh install that is frozen by hand-edit, or a user who froze before the learner persisted anything.
- **After R7 the product never takes the seed branch,** because the factory prior always fills the slot.

**Fix anyway (defence in depth):** the latch now sits right after the constellation block, in the same `applyConfig`. There is no `return` in between, and nothing in between reads the latch. The prior branch is unchanged. `shipParityFrozenSeedLatchIncludesTheBase20Shift` pins it and fails against the old ordering.

## 5. Open follow-ups (not done here)

- **VeniceProfile import** (`VeniceProfile.h:298-301`) applies `tip_phase_aim_frozen` and `tip_timing_user_set` exactly as given. A profile exported before v3 (`frozen:false`) would unfreeze the aim after migration. Decide whether an import should ignore `frozen:false` unless `tip_timing_user_set` is true.
- **Failed `learning.json` save during the reset.** If that save fails but the settings save succeeds, the next launch is v3 and does not retry. The aim then stays at the old value, frozen. This is logged ("learning.json save failed"). It is rare: the disk writes `settings.json` in the same breath.
- **Stale comment, cosmetic:** `PreviewPresentationBufferTests.cpp` ~195 says "production defaults: base-30".
- **R9** (`ORION_PROBE_LEAD_PERSIST`) was not applied. It affects onboarding only.

## 6. Central run #1 fallout (1238 passed / 6 failed): triage

All 6 failures are test-fixture fallout; **none is a product bug**. The attributions come from reading the code, not from a re-run. Each fix pins the old default in that fixture, with the `[SHIP_PARITY 2026-09-23]` tag.

| Test | Default | Mechanism | Verdict / fix |
|---|---|---|---|
| `measuredLeadValidatedUsesPosteriorMean` | two-frame | `promoteStrictAutonomousSquare`-style 5/9/13 feed: two-frame owns at fill 9, so the fill-13 `process()` is a holding tick that arms on the provisional 227.1 lead. The converged 235.4 sample then hits the existing keep-the-armed-token rules and does not re-key `effectiveLatencyMs`. Fills never reach 20, so base-20 is inert here. | Fixture. Pin `ownershipProofTwoFrame=false`. |
| `subtickMirrorHonoursThePhaseAnchorImminentHold` | two-frame (base-20 was already pinned) | Same: ownership at 9 means the promotion's fill-13 `process()` arms below the band. The deadline read at 24.4 is that earlier token, not a mirror arm inside the band. The fixture's own premise ("the tick is never run past this point") breaks. The imminent-hold logic itself is regime-agnostic: `phaseAnchorImminentTargetLevelPct` follows `tipPhaseAnchorPct`. | Fixture. Pin two-frame false. |
| `slowMeterDeferNormalProfileIsBitIdenticalToDefault` | base-20 | The "normal profile" is defined at anchor 30. `slowMeterDeferEnabled` is **env-only, default OFF, not in the shipped profile or the dev launcher**, so how it diverges at base-20 is not a ship question. | Fixture. Pin base-20 and two-frame false. Follow-up only if that lab flag is ever productised. |
| `tipFireFloorIgnoresNonsenseVelocity` | base-20 | The 5→15→25 burst was built to stay below the base-30 anchor. At base-20 the 15→25 straddle dates the phase member (overshoot 5 < 12), so the reason code becomes the phase path's. The phase path does not use velocity and its deadline is ~anchor+451−75, far from the 13 ms the test advances, so this is not an early dump. The same straddle-dating exists at base-30 for a 25→35 burst, so this is not a new exposure class. | Fixture. Pin base-20 and two-frame false. |
| `ownersSettingsFileRoundTripsLosslesslyThroughMigration` | v3 stamp | The owner's live file is v2 and the stamp advances to 3. | The `settings_version` rewrite is now sanctioned only when original < current, and only to `kSettingsVersion`. The v3 rule keys were already sanctioned. The coordinator's QSKIP is kept. |
| `meterDelayUncalibratedLeadIsDiagnosed` | base-20 | `maxSchedulableTipLeadMs` = effective constant − 30. Base-20 moves it from 363 to 421.3, so the AUTO meter-delay offset goes from 51.3 to 109.6, the keyed lead goes from 346.3 to 404.6, and the fixture's 350 ms tip is no longer schedulable. That is correct arithmetic: the base-20 anchor gives more runway. **Meter delay is shelved** (`kMeterDelayShelved`), so this is not a product path. | Fixture. Pin base-20 false in the helper `stuffSchedulableDelayShot`. Its callers were all written against the base-30 ceiling. |
