# TASK.md - Active Work for the Claude + Codex Loop

> Header tokens (read by `agent-loop.ps1`):
> ALLOW_SECURITY_FILES: no
> GAMEPLAY_CHANGES: yes

This file holds one focused task at a time. Claude implements the Active Task;
Codex reviews it. When a task is approved, move it to Done and promote the next
item from Backlog.

Scope for this pass:
- Gameplay/core timing changes are allowed only for the files named in the Active
  Task.
- No secrets, auth, licensing, deployment, release, package, or code-signing files.
- Keep each patch narrow, test-backed, and reversible.
- If progress requires live PS5/Remote Play gameplay, set
  `LIVE_VALIDATION_REQUIRED: yes` in `STATUS.md` and stop.
- If 10 rounds are exhausted and the task is still not resolved, continue with
  another bounded batch after reviewing `STATUS.md`, unless a hard stop condition
  fired.

---

## Active Task

> **Round 22 (user-directed, NOT the bounded loop):** fixed the post-release grader recede-INVERSION
> (`evaluatePostReleaseMeter` graded deflated-to-~52% LATE shots as EARLY → clock runaway). Detector
> proven ACCURATE (the brief's "52% false-lock" premise was disproven) — `meter_detector.py` untouched.
> Fix = peak-gated recede→LATE verdict + `meterRecedeLatePct`; clock-runaway divergence guard in
> `learnFromOutcome` (`calDivergenceGuardShots`). +2 native tests, ctest 100%. NOT committed. See
> `STATUS.md` Round 22 and `.claude/plans/zazzy-drifting-lerdorf.md`. LIVE_VALIDATION_REQUIRED.

**T1b - Live release output + Go-To + detector dropouts.**

Supersedes T1 (its timing/reachability + telemetry work is done; see Done). The 10:28
local batch (`Videos/2026-06-04 10-28-09.mp4` + `logs/orion_native.log` 15:28-15:33Z,
CDT/UTC-5) showed the remaining blockers are release OUTPUT and detection dropouts,
not feed: standstill `green_confirmed` shots logged at 65-76% fill yet landed
late/no-interaction, fades still `timeout_fallback` at low fill, and Go-To
`max_hold_safety` dumps at 13-42% (early/pump-fake). Do NOT tune `early_late_offset_ms`
yet — first prove releases reach ViGEm and correlate logged fill to the video's real
meter. Each step stays narrow and test-backed.

**Do:**
1. (Step 0) Build `tools/diagnostics/correlate_releases.py`: parse "Release issued"
   lines, compute video time from `--video-start-utc 2026-06-04T15:28:09Z`, replay the
   video (cropped to the stream ROI) through the detector for a `video_fill_pct`
   column, emit `logs/diagnostics/release_correlation.csv`.
2. (Step 1) Add ViGEm submit verification: a `releaseSeq` on `ShotContext` echoed in
   the "Release issued" line and a new `Release submit: seq= ok= square_bit= ...` line
   logged at the actual submit tick. Instrumentation only — no timing/output change.
3. (Step 2) Go-To: add `gotoMinReleaseFillPct`; suppress `max_hold_safety` low-fill
   dumps for Go-To, keep holding for a fresh meter/green, and ABORT (no blind shot) at
   an absolute hard cap. Never dump at low fill.
4. (Step 3) Detector dropouts: evidence-first — replay the video to localize
   `roi_not_found`/`green_not_found`, then make the narrowest `meter_detector.py` fix,
   guarded by `tests\test_meter_detector_video.py` + `tests\test_roi_relock.py`.
5. Add/update focused tests for every behavior change; keep telemetry honest.
6. (Step 4) Standard gate: `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1`
7. (Step 5, Round 17) Go-To release-path ATTRIBUTION instrumentation (diagnose-only, no behavior
   change): add a read-only path-attribution snapshot to `ShotContext`
   (`greenConfirmedAtRelease`/`targetModeAtRelease`/green-confirm-race/velocity/crossing/withinReach),
   emit a SEPARATE seq-paired `Release attribution:` line (leave `Release issued:` byte-for-byte),
   and extend `correlate_releases.py` (add the `seq` column + `attr_*` columns + a Go-To attribution
   printout). Goal: definitively attribute the bimodal Go-To split (`green_confirmed`≈100 vs
   `predictive_target`≈70) on ONE live batch run at `early_late_offset_ms = 0`, BEFORE choosing a
   timing fix. Do NOT change the offset in code or touch detector/capture/ownership/HidHide.

**Files allowed to change:**
- `tools/diagnostics/correlate_releases.py`
- `native_orion/src/OrionAppController.cpp`
- `native_orion/src/AutomationEngine.cpp`
- `native_orion/src/AutomationEngine.h`
- `native_orion/tests/AutomationEngineTests.cpp`
- `meter_detector.py`
- `scripts/verify_orion.ps1`
- `STATUS.md`, `TASK.md`
- (read-only) `native_orion/src/VirtualController.{cpp,h}`
- Go-To freshness gate + strict arming (11:58 batch) also allows:
  - `native_orion/src/RemotePlaySession.cpp` (parse the sidecar raw-accept flag)
  - `remote_play_orchestrator.py` (emit `raw_fed` on the fusion payload)
  - `native_orion/backend/autogreen_sidecar.py` (serialize the new fusion field)
- Round 21 (user-directed: per-type ACQUIRE→LOCK calibration + meter-appearance anchor A/B +
  Go-To clamp unrail) also allows:
  - `native_orion/src/AppConfig.cpp`, `native_orion/src/AppConfig.h`
    (`feedforward_anchor` setting; `shotTypeMeterToReleaseMs` + `shotTypeCalPhase` learning maps)
  - `native_orion/src/OrionAppController.cpp` (persist the new learning maps + enriched
    `Shot outcome:` telemetry)

**Explicitly NOT allowed:** capture/decoder/controller-remap rewrites,
security/licensing files, release/deploy/package files, secrets, or unrelated UI
changes unless promoted into a new Active Task.

**Acceptance criteria:**
- `correlate_releases.py` produces the per-release CSV with `video_fill_pct`.
- Every release logs a paired `Release submit:` line proving the cleared-Square write.
- Go-To no longer releases via `max_hold_safety` at low fill; native tests cover it.
- Detector `roi_not_found`/`green_not_found` rate on the batch video is shown to drop
  (offline), with no regression in `test_meter_detector_video.py`/`test_roi_relock.py`.
- `scripts\verify_orion.ps1` passes, or the work stops with a precise failing log.
- No secrets/release/deploy files are touched.
- Final correctness (do the changes fix the in-game shots) needs a live PS5 batch — set
  `LIVE_VALIDATION_REQUIRED: yes` and stop when offline work is exhausted.

---

## Backlog

- **T2** - Cross-check that every `tests\test_*.py` file is referenced by
  `verify_orion.ps1`; list any others that are missing. Report only unless
  promoted.
- **T3** - Add a short "Running verification" subsection to `README.md` that links
  to `TEST_COMMANDS.md`.
- **T4** - Add Headroom as optional agent tooling only, not as an Orion runtime
  dependency. Current loop uses `tools/agents/compact_agent_context.py` as the
  safe local fallback; replace/augment it with Headroom once the CLI install is
  reliable on this machine.

---

## Done

- **T1** - Stabilize live shot timing and verification wiring (offline phase). Wired
  `test_stability_tracking.py` into `verify_orion.ps1`; shipped the per-shot-type
  reachability cap (`maxPredictiveRisePct=25`/`reachabilitySlackPct=6`); pinned every
  reachable release/waiting reason code with a dedicated native test. Offline coverage
  complete (Rounds 1-9). The live-correctness question moved to T1b.

---

## How to add a new task

1. Write it as a single small, reversible step.
2. Name the exact files it may change.
3. State acceptance criteria and which tests prove it.
4. If it must touch a security/licensing/release file, set
   `ALLOW_SECURITY_FILES: yes` (raw secret material is still never auto-edited).
5. If it must touch gameplay/core timing code, set `GAMEPLAY_CHANGES: yes` and name
   the precise files and intended behavior change.
