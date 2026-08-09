# Phase 1 Wiring Audit — Precision Core

End-to-end wiring checklist for everything Phase 1 added or changed. "static" = verified
by code inspection this pass; "test" = covered by an automated test (name given);
"live" = needs the Phase 7 live/stress session to sign off.

## 1A Autonomous calibration

| Wiring | Verified |
|---|---|
| `banner_calibration` default ON (AppConfigData) → saved/loaded (`AppConfig.cpp:174/:501`) | static |
| Banner verdict pipe: orchestrator → sidecar `payload["banner"]` → RemotePlaySession → `setBannerVerdict` (OrionAppController connect) | static |
| Banner-unclear fallback: stored self-grade applies after N silent shots | test `bannerUnclearFallbackAppliesSelfGradeAfterStreak` |
| Any banner verdict resets the unclear streak + deadline | static (both branches of `setBannerVerdict`) |
| `early_late_offset_ms` folded once into per-type learned offsets at load, key zeroed | test `earlyLateOffsetFoldsOnceIntoPerType` |
| Auto-recalibration on meter style/color change + stream Running transition (`recalibrateAllShotTypes` at OrionAppController :1127/:2670/:2763) | static |
| Divergence-guard trip drops type to ACQUIRE with fresh annealing window | test `divergenceTripResetsAnnealingWindow` |

## 1B Shot-execution correctness

| Wiring | Verified |
|---|---|
| Held output forced every Armed/Holding tick (`forceHeldOutput`), all shot modes | test `holdOutputHasNoGapAndExactlyOneReleaseEdge` |
| Exactly one release edge per shot; no X re-press post-release | test (same) |
| Go-To RS pinned −127 every hold tick; single neutral edge on release; X never pulsed | test `goToHoldsStickContinuouslyWithSingleNeutralEdge` |
| Pump-fake race at the controller: fire-thread `firedUnconsumed()` re-check converts a held submit to released under `submitMutex_` | static (live confirm in Phase 7) |

## 1C RTT sync

| Wiring | Verified |
|---|---|
| Sample-and-hold: offset frozen at hold-start, pending updated continuously | test `networkOffsetSampledAndHeldPerShot` |
| LOCK captures per-type RTT baseline → `rttBaselineUpdated` → persisted (`shot_type_rtt_baseline_ms`) | static + test `learningRoundTripsRttBaselineAndVelocityPrior` |
| Per-shot delta clamp vs LOCK baseline (±25, wifi ±12) | test `rttBaselineDeltaClampBoundsDrift` |
| Auto wifi mode from jitter EMA; tightens clamp + freshness window | test `wifiModeEngagesOnJitterAndTightensClamp` |
| All 7 native offset feeds route through `updateNetworkQuality(offset, jitter)` | static |
| Ping cadence 400→150 ms while a shot is in flight (`notify_meter_active` from orchestrator feed gate) | test `test_meter_active_tightens_ping_window` + static |
| Live decode comp: orchestrator `observe_frame_age_ms` → EMA replaces fixed 7.5 ms | test `test_decode_comp_uses_measured_frame_age_ema` |
| `predicted_offset_ms` genuinely forward-looking (unclamped predicted half-RTT) → `syncAdjustMs` → `networkAutomationOffset()` Auto path | test `test_predicted_offset_tracks_rising_rtt` + static |
| QOS dead fields deleted (snapshot, sidecar payload, native debug line → now shows decode comp) | static + test `test_snapshot_has_no_qos_fields` |
| `align_release()` KEPT — not dead: controller_remap.py phase-locked path (standalone Python mode) calls it | static |

## 1D Sub-tick release scheduler

| Wiring | Verified |
|---|---|
| Engine commits deadline within 6 ms horizon; holding frames show `release_scheduled` | test `scheduledFireConfirmCompletesRelease` |
| Fire thread (`OrionPreciseFireThread`) arm/confirm round-trip before `process()` | static (controller side); engine confirm path under test |
| Grace fallback: no confirm within deadline+8 ms → in-tick release | test `scheduledFireFallsBackInTickWithoutConfirm` |
| `clearScheduledFire` on beginShot / triggerRelease / abort / reset | static |
| Telemetry: new `Scheduled fire:` key=value log line (no existing parsed line edited) | static |
| Sub-ms scheduled-vs-actual delta (p95) | live (Phase 7 metric) |

## 1E Math/prediction

| Wiring | Verified |
|---|---|
| Velocity prior EMA from vision-timed releases → `velocityPriorUpdated` → persisted (`shot_type_velocity_prior_pct_ms`) | test `velocityPriorLearnsFromVisionRelease` |
| Live velocity clamped to [prior×0.5, prior×1.6] in processHolding | static |
| `clockVisionDivergenceMs` stamped in release attribution | static |
| Quadratic (decel-aware) crossing predictor adopted on residual gate | pre-existing (`TemporalSampler::predictCrossingMs`) |

## learning.json symmetry

`saveLearning` writes / `loadLearningObject` reads: feedforward, meter-to-release,
learned offsets, learn counts, cal phase, greens/misses/rail, **rtt baseline,
velocity prior** — round-trip covered by `learningRoundTripsRttBaselineAndVelocityPrior`.

## Deferred to Phase 7 (live/stress)

- Pipe reconnect / controller churn / stream stop-start sweeps.
- Scheduler fire-accuracy p95 from live `Scheduled fire:` lines.
- Banner grader live hit-rate + fallback engagement under real UNCLEAR streaks.
- Wifi-mode engagement on a real jittery link.

Gate status this pass: full ctest green (111 native tests), pytest 136 passed.
