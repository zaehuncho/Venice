# A1 — Native timing engine

**Decision: needs changes.** Three newly isolated defects are below. None establishes a console-delivery failure or a new critical release blocker. Source and settings were not changed; all builds were limited to copied source under `%TEMP%`.

## Evidence boundary and coverage

Read the requested audit prompt, `AGENTS.md`, master M-01–M-41 report and progress, prior Codex reliability report, post-launch roadmap, variance documents, and the prior P6 engine findings. Reviewed release/schedule/authority/reset paths in `AutomationEngine.*`, `OnsetFeedforward.h`, `BannerLeadTrim.h`, and the relevant precise-fire, release and feedforward policies. Existing known timing defects are not relabelled as new findings. The fixed +10 ms shift remains refuted by the recorded prospective comparison; these findings do not propose a new global timing bias.

The four source files were re-read and their SHA-256 values compared with the compiled scratch copies immediately before this report:

| Source under `C:\Users\aaron\Desktop\NexusVision\native_orion\src` | SHA-256 |
|---|---|
| `AutomationEngine.cpp` | `D0B36FB9AF9F76B270D8FD3ABCA7E2F168141689B481B54652CFA93F2BC59E71` |
| `AutomationEngine.h` | `FC06013FFB7E3F833DD07BC2D49CCEFD86525A17D0188D04EDD19F0AFD7B0B2B` |
| `BannerLeadTrim.h` | `CF046217E8E225FCD8C0539CCAF5A96E556C79758B4B47F795FE7F0EBF8FD6FE` |
| `OnsetFeedforward.h` | `9073D0D853D7889B23D4E12289F534DEB8A146E367FFA38D63D52D9B8A8F1C48` |

Scratch evidence:

- `C:\Users\aaron\AppData\Local\Temp\nexus-a1-engine-audit-20260923\`: copied engine/AppConfig/headers, CMake project, `audit.cpp`, `results.txt`, coherent rebuilt `OrionCommon.dll`, `AutomationCore.dll`, and `a1-engine.exe`. The harness uses the existing `AutomationEngineTests` friend seam; it invokes no launcher, controller transport, or live service.
- `C:\Users\aaron\AppData\Local\Temp\nexus-a1-policy-audit-20260923\`: copied policy headers, `audit.cpp`, `results.txt`, and a rebuilt unmodified `ShotVerdictTallyTests.cpp` with `existing-tally.txt`.
- Configuration/build: `cmake -S <scratch> -B <scratch>/build -G "Visual Studio 17 2022" -A x64 -DCMAKE_PREFIX_PATH=C:/Users/aaron/Qt/6.8.0/msvc2022_64`, then `cmake --build <scratch>/build --config Release`; both completed with exit 0. Qt 6.8.0/MSVC 19.44.35228.0. Scratch-only build warnings: temporary intermediate directories and three pre-existing ignored `[[nodiscard]]` results; no compile/link failure.
- With `C:\Users\aaron\Qt\6.8.0\msvc2022_64\bin` prepended to `PATH`, rebuilt `a1-existing-tally.exe -o <scratch>/existing-tally.txt,txt` returned **58 passed, 0 failed, 0 skipped, 0 blacklisted; exit 0**. These policy tests do not exercise the two engine lifecycle/schedule regressions below.
- The first attempt to use the existing `native_orion/build/Release/OrionNativeTests.exe` instead returned exit 1/access violation `0xc0000005` in `BannerLeadTrim::resolvedKey` during `onsetFeedforwardFencesPhaseAndTrim`. The EXE is dated **01:58:25Z**, its `AutomationCore.dll` **02:33:21Z**, and the source/header **02:19:39Z** on 2026-09-23. This is contaminated mixed-generation evidence, **not a demonstrated current-source crash**. A7 was notified; the fresh scratch harness avoids mixing those artifacts. No full native-suite pass is claimed here.

## New findings

### [AUD-A1-001] medium — A queued release oracle survives reset and changes the next context's trim
- Kind: reliability
- Evidence: `native_orion/src/AutomationEngine.cpp:5095-5122,5235-5290` resets shot/latency state but never clears `pendingReleaseOracles_` or `bannerTrimReleases_`; `:15836-15895` subsequently consumes the old entry and changes the trim. Contrast the explicit invalidation already implemented for lead changes (`:1796-1803`) and the trim enable interval (`:1530-1535`). A real sidecar start calls the generation boundary at `native_orion/src/OrionAppController.cpp:2766-2767`. Fresh scratch engine output was literally:
  ```text
  reset_before releases=1 pending=1 trim=0.0
  reset_after releases=1 pending=1
  reset_then_flush trim=3.0
  generation_after releases=1 pending=1
  ```
  Input: two consumed oracle misses in the same bucket, one third parked miss, `reset()`, then expiry flush; separate case parks a miss then calls `beginSidecarProcessGeneration()`. Command: `C:\Users\aaron\AppData\Local\Temp\nexus-a1-engine-audit-20260923\build\Release\a1-engine.exe`; exit 0. The old parked result, not a newly released shot, closes the batch and moves the trim.
- Customer impact: following a disconnect/restart/reset, a deferred measurement from the previous capture context can still move the effective lead. A customer can see timing change before the new context has supplied a graded shot. This is not an epoch-collision claim: the demonstrated failure needs no epoch reuse and no stale external message, because the oracle is already parked locally.
- Proposed change: discard outstanding attribution and oracle work at the generation boundary, which `reset()` already calls. Preserve the earned trim value; do not silently zero the user's effective lead. Separately define whether accumulated *completed* history should persist across reconnects. Proposed only:
  ```diff
  --- a/native_orion/src/AutomationEngine.cpp
  +++ b/native_orion/src/AutomationEngine.cpp
  @@ -5240,4 +5240,6 @@ void AutomationEngine::beginSidecarProcessGeneration()
       // A restart ends the old capture/label namespace. Neither a late landing
       // nor a restarted post-hoc counter can teach the prior process's shot.
  +    pendingReleaseOracles_.clear();
  +    bannerTrimReleases_.clear();
       cancelPostReleaseGrade(meterCapSeq_);
  ```
- Verification: add both `reset()` and direct generation-boundary regressions. Park the third miss, cross the boundary, advance beyond grace, and assert no `bannerLeadTrimUpdated`, no trim delta, empty pending/release rings, and refusal of the old epoch. An oracle attached to a newly released epoch must still work. Repeat a disconnect/reconnect event-loop test in the coherent rebuilt unit.
- Confidence: confirmed

### [AUD-A1-002] medium — The repaired 2 ms feedforward floor is bypassed by the later trim cap
- Kind: reliability
- Evidence: `native_orion/src/OnsetFeedforward.h:142-167` implements CL2-P6-004's 2 ms floor, but `native_orion/src/AutomationEngine.cpp:17650-17669` subsequently clips the decision against remaining banner-trim headroom and still uses **0.05 ms** as its zero floor. `:13242-13250,15568-15577,15870-15881` fence learners for any nonzero actual displacement. Fresh scratch-engine output was literally `real_schedule trim=14.0 armed=1 ff=-1.0 deadline_shift=-1.0`, exit 0. This calls the actual copied `scheduleFire`, not a rewrite of its arithmetic. Input: the shipped 0.2/10 FF, reference onsets 520 ms, live onset 570 ms, a +14 ms banner trim (six LATE verdicts to +15, then the normal excellent decay), a future 60 ms deadline and the existing rolling-authority fixture. This is **new post-fix evidence for M-25 / CL2-P6-004**, not a re-report of the retired 0.05 ms policy threshold.
- Customer impact: once the trim nears its ceiling, even a 1 ms final FF shift still discards the latency marker, banner and oracle evidence. The promised evidence-retention fix does not hold end to end, potentially slowing recovery from session bias. The frequency/effect size in customer sessions has not been measured.
- Proposed change: enforce the same physical floor on the **final candidate shift**, after trim and past-deadline clipping, and apply no shift below it. Expose one shared policy constant rather than retaining two floors. The essential final-application change is below; the shared constant declaration should move from private to public in `OnsetFeedforward` in the implementation change. Proposed only:
  ```diff
  --- a/native_orion/src/AutomationEngine.cpp
  +++ b/native_orion/src/AutomationEngine.cpp
  @@ -17666,5 +17666,12 @@
               if (displaced <= now) {
                   displaced = now + 0.05;   // never arm a guaranteed-past deadline
               }
  -            schedFireAppliedOnsetFfMs_ = displaced - deadlineMs;
  -            deadlineMs = displaced;
  +            const double appliedMs = displaced - deadlineMs;
  +            if (std::abs(appliedMs) >= orion::OnsetFeedforward::kMinAppliedMs) {
  +                schedFireAppliedOnsetFfMs_ = appliedMs;
  +                deadlineMs = displaced;
  +            } else {
  +                decision.applied = false;
  +                decision.offsetMs = 0.0;
  +                decision.reason = QStringLiteral("final_zero");
  +            }
  ```
- Verification: extend `onsetFeedforwardIsBoundedByTheBannerTrimAndResets` beyond the present 0/6 ms headroom cases (`native_orion/tests/AutomationEngineTests.cpp:50953-50990`, current concurrent test snapshot). Cover headroom 0, 0.5, 1, 1.99, 2 and 3 ms, restored fractional trims, and a near-now clipping case. Under-floor cases must preserve the original deadline, carry exactly zero, emit the normal marker and permit a paired banner/oracle observation; 2 ms and larger remain displaced/fenced. Do not merely weaken the learner fence while still displacing the release.
- Confidence: confirmed

### [AUD-A1-003] medium — First range-specific trim update discards its inherited baseline and can step the wrong way
- Kind: bug
- Evidence: `native_orion/src/BannerLeadTrim.h:1078-1089` (the `resolvedKey` fallback) allows a fade's new range bucket to read its existing `(type, tempo)` trim, but `:502` and `:760` start both banner and oracle updates from `trim_.value(out.key, 0.0)`. A newly created range key therefore writes a delta from zero rather than from the lead those shots actually used. Scratch policy output:
  ```text
  range_before=12.0
  range_late_update_before=0.0 after=3.0 actual_after=3.0
  oracle_range_after=3.0
  ```
  Input: restore `Left Fade/normal=24` (normal half-strength restore yields +12), then two LATEs at normal onset 840 ms in `Range::Mid`; separately, three oracle misses in the same new range. Command: `C:\Users\aaron\AppData\Local\Temp\nexus-a1-policy-audit-20260923\build\Release\a1-policy.exe`; exit 0. The +12 → +3 transition delays the next shot by 9 ms in response to LATE, the opposite of the rule's intended direction.
- Customer impact: when pre-fire range becomes available, a returning customer's earned generic fade correction can abruptly jump in the wrong direction as the range bucket first learns. A series of EXCELLENTs can also fail to decay an inherited nonzero trim because the updater sees zero. **Activation is conditional:** M-40 / CL2-P1-003 already documents the current range signal being unknown; this report does not claim the normal presently unknown-range path exhibits the bug. Fix before enabling/recovering range authority.
- Proposed change: initialise the update from the same fallback-resolved value the live shot reads, without sharing the range buckets' verdict windows. Apply to both observation paths. Proposed only:
  ```diff
  --- a/native_orion/src/BannerLeadTrim.h
  +++ b/native_orion/src/BannerLeadTrim.h
  @@ -502,1 +502,2 @@
  -        const double before = trim_.value(out.key, 0.0);
  +        const double before = trim_.value(out.key,
  +            trimMsForType(shotType, out.tempo, out.range));
  @@ -760,1 +761,2 @@
  -        const double before = trim_.value(out.key, 0.0);
  +        const double before = trim_.value(out.key,
  +            trimMsForType(shotType, out.tempo, out.range));
  ```
- Verification: add `ShotVerdictTallyTests` cases with inherited +12 and -12 trims. A first eligible range-specific LATE step must be +12 → +15, EARLY must be +12 → +9, oracle descent must step from +12, and the ordinary excellent decay must reach +11. Assert the generic bucket and the other range remain unchanged. Cover unknown range, direct existing range keys, restore, and range/tempo kill switches. The currently rebuilt 58-test policy suite passes without covering this inherited-baseline transition.
- Confidence: confirmed

## Existing IDs and deliberately unchanged conclusions

- **M-25 / CL2-P6-001, -002, -003:** the earlier reports already cover displaced learner leakage, the gain-zero delayed fence, and the development-sweep oracle fence. Inspection still finds the old `devFireOffsetArmed_`-only learner checks and gain-zero normalisation, and the oracle consumer still lacks the banner path's dev-offset fence. These are not new AUD items. The required regression should cover **both** banner and oracle; the banner-only source repair does not close CL2-P6-003.
- **M-24 / CL2-P6-006:** FF environment/A-B parsing is now inside the production compile guard. A fresh production package attestation remains required; source inspection is not package approval.
- **M-16, M-40:** Go-To acquisition and missing pre-fire range remain in their established areas/IDs. This report does not infer a new range correction from post-banner distance.
- **CL2-P6-005:** the development-only carry's context/type/ceiling issues are already documented and are not re-enumerated here.
- The fire-to-wire measurements bound the observed local PC path, not console receipt, and do not validate all scheduler/error paths under arbitrary load. No claim of a universal make-rate improvement or perfect reliability follows from these unit probes.


