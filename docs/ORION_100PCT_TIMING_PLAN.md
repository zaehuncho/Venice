# Orion — Tip-Perfect Meter Timing: 13-Day Research-to-Ship Plan

_Generated 2026-07-18. Source: 17-spec fable research fan-out (`wf_f3a566ba-d51`) + live-tree grounding survey. Every item is tagged **NEW** or **IMPROVE-`<module>`**, ranked by leverage, and sequenced to not collide with the live root-branch wave._

---

## Context — why this exists

The end goal: the bot reads the NBA 2K shot meter and releases at the **tip** on both the HDMI capture-card path and the H.264 remote-play path, for **every shot type**, every time. The honest physical framing of "100%" is:

> **per-shot-type release-time σ  <  green-window half-width**, keyed per `{shot_type × path}` cell — not `error == 0`.

If the release jitter is tighter than the make-window is wide, every shot lands inside it. That is the target metric, and it is achievable. `error == 0` is not (lossy encode + network jitter + console frame quantization delete information no algorithm can recover). The whole program is about shrinking σ per cell below each cell's half-width, with **zero regression** to the three guarded invariants.

### The three guarded invariants (never regress)
1. **Zero off-shot décor false-locks.**
2. **Box never clips the arrow tip.**
3. **Lock survives a 0.6×–1.6× camera-scale change.**

### Discipline (non-negotiable, matches the shipped `ORION_*` pattern)
Every new adaptive state ships **frozen-by-default behind an `ORION_*` flag**, **shadow-grades** against the shipped path in the certification harness, and is **enabled per-cell**, never globally. This is the exact pattern C2 (`ORION_FREEZE_CAL`, commit `15e0f9a8`) enforced after unfrozen online adaptation drifted a session. **T1d is the sharpest risk here** — see §Invariant Risks.

---

## Ground truth — what already exists (do NOT rebuild; improve)

The live survey found much of the "novel" idea-space already prototyped on the **root branch `feat/detection-template-anchor-qml-render`** (`92cf4a5b`), mostly flag-gated and shadow-first:

| Mechanism | Already in tree | Real gap → this plan |
|---|---|---|
| Tick-phase estimator | `latency_estimator.py`: `fit_tick_phase()`, `TICK_PERIOD_MS`, `tick_phase_ms/conf/sd` (probe-driven sawtooth fit) | The **scheduler that consumes phase to snap the press onto a tick** (T1a) |
| Rise-curve template + analytic crossing | `tip_registration_infer.py`: `RegistrationPredictor`, persisted template `g`, dense `g_inverse()` inverse table, per-duration priors | A **per-shot-type template *library* + classifier** (single template today) → T1b, and the curvature **G4 gate** → T1c |
| Crossing predictor | `controller_remap.py`: `predict_crossing_ms()` (linear-vs-quadratic residual-gated), `eta_to_green_ms`, `_check_predictive_release`, `eff_latency` staleness | Feed it the matched-template prior + tick-lock; add 3-estimator fusion → T1d |
| Fused fire / flip referee | root: `ORION_FUSED_FIRE` fused `t_tip` posterior (`576ec071`), flip machinery (`95257697`), `tools/timing/fused_shadow_report.py` | Add tick-locked + matched-template as estimators; **EWMA re-aim FROZEN** → T1d |
| Animation phase anchor | `animation_anchor.py`: `MotionPeakDetector`, shadow-first landmark independent of meter | Promote/prove as the phase-infill clock for T2b |
| Vertical-zoom follow | root: `ORION_READER_VZOOM` B2 vertical-band-follow (`1babf03c`, default OFF) | Motion-measured scale-rate (T2c) is the challenger; likely cut vs T2a |
| Occlusion coast | root: `ORION_READER_OCCL_WIDE` (`89dba960`), `ORION_READER_SHOT_COAST` (`998e0dd0`) — **blind** wall-time coast | Replace blind coast with **censored-interval fusion + phase infill** → T2b |
| Compressed path reader | `compressed_meter_reader.py`: luma-acquire, registry-match, structure-acquire, stale-ROI | Add chroma-blob moments (T3b), green-flash (T3a), domain-norm (T3c) |
| Regression / shadow harness | `tools/regression/run_gates.py`, `replay_gates.py`, `gate_floors.json`; `tools/timing/fused_shadow_report.py`, `tip_registration_eval.py` | Add the **{shot_type × path} σ-vs-half-width matrix + invariant corpora** → T4c |

---

## Collision map — sequence around the live wave

- **Live wave is on the root branch**, not the stale `agent-*` worktrees (all four are Jun 24–26, clean/committed). Named theme work (vzoom, stale-lock, occlusion, C2 freeze, tempo release, fused_fire, detection-100) all lives on root behind `ORION_*` flags.
- **Your timing lane is collision-free.** No worktree touches `controller_remap.py`, `latency_estimator.py`, `tip_registration_infer.py`, `animation_anchor.py`, `rtt_sync_engine.py`, `simple_meter_reader.py`, or `compressed_meter_reader.py`. → **Waves 1–2 timing items (T1a/b/c/e, T5a, T4c) have a clean lane** and can start immediately.
- **`meter_detector.py` is the one hotspot** — three-way writer (stale `a58c06` + active `ac2173` + root branch). Any detection-side item that edits it (T2a/T2b/T2c band/scale, some T3) must coordinate with `ac2173`'s lane and rebase on root. Prefer implementing detection channels in the **reader modules** (`simple_meter_reader.py`, `compressed_meter_reader.py`) where possible to stay out of the hotspot.
- **Base all new work on root `feat/detection-template-anchor-qml-render`**, not the stale worktrees. Follow the established `ORION_*` default-OFF + shadow-grade convention.

---

## Leverage ranking (all 17)

| Rank | Item | Tag | Lev | Cost | One-line why |
|---|---|---|---|---|---|
| 1 | **T1a** tick-phase PLL press-snap | IMPROVE-`latency_estimator.py` / NEW scheduler | 9.5 | 0.01ms | Deletes the ~4.8ms **white** tick-quantization term — a hard floor under every other gain; zero pixel/invariant risk |
| 2 | **T5a** Windows RT jitter floor | NEW (fire loop) | 9 | 0ms | Removes 1.5–6ms actuation σ; enables T1a/T1c to actually land at the actuator |
| 3 | **T4c** σ-matrix + invariant corpus harness | IMPROVE-`tools/regression`,`tools/timing` | 9 | 0.05ms | The ruler — must exist before any wave-2 enable or every A/B is vibes |
| 4 | **T1b** per-type rise-curve template library | IMPROVE-`tip_registration_infer.py` | 8.5 | 0.05ms | fade/occlusion error accel·h² → σ·h; feeds T1c + T1d |
| 5 | **T1c** analytic sub-frame crossing | IMPROVE-`tip_registration_infer.py`/`controller_remap.py` | 8 | 0.03ms | Kills 16.7ms fire-grid quantization; tie-or-beat gate = today's Kalman on reject |
| 6 | **T3b** chroma-blob moment locate | NEW-`compressed_meter_reader.py` | 7.5 | 0.4ms | Blur-invariant centroiding densifies remote-path measurements |
| 7 | **T2b** occlusion censored-fusion + phase infill | IMPROVE-`ORION_READER_SHOT_COAST` | 7.5 | 0.6ms | Turns blind coast (the frames you fire in) into bounded interval updates |
| 8 | **T2a** median-flow LK rigid tracking | NEW | 7 | 0.8ms | Measured velocity on x:1100→11 slides; feeds T2b/T2e |
| 9 | **T1e** ViGEm output-jitter rig | NEW | 6.5 | 0.02ms | Validates T5a; contamination flag = prerequisite to ever unfreeze clocks |
| 10 | **T3a** red→green flash (CFAR) | NEW | 6.5 | 0.2ms | Only sub-frame observation of the make-window itself; shadow-clock only |
| 11 | **T1d** 3-estimator fusion + divergence telemetry | IMPROVE-`ORION_FUSED_FIRE` | 6 | 0.2ms | Modest σ gain, but divergence = missing staleness detector; **re-aim FROZEN** |
| 12 | **T4b** scene-threat veto stack | NEW (corpora) | 6 | 0.5ms | Lets aggressive measurement items ship without gambling invariant (i) |
| 13 | **T3c** noise-gated guided filter | NEW | 5.5 | 0.4ms | ~20–30% remote σ cut; changes T3b's pixels → grade jointly |
| 14 | **T2c** scale-from-flow-divergence | OVERLAP-`ORION_READER_VZOOM` | 5 | 0.8ms | Overlaps T2a scale-rate; **A/B challenger — one of T2a/T2c is cut** |
| 15 | **T2e** flow-gated 1-D Wiener deblur | NEW | 4.5 | 0.08ms | Cheap capture-path fade gain; late add-on, depends on T2a |
| 16 | **T2d** multi-hypothesis tracker | **DEFER** | 4 | 1.2ms | Defends a zero-violation invariant; arm-time forced promotion is the sharpest edge |
| 17 | **T4a** skin taxonomy | **DEFER** | 3 | 0.2ms | Scope expansion, zero σ gain on current Straight-Bar/red config |

**Highest-leverage first A/B: T1a (with T5a as its substrate, same wave).** The ~4.8ms uniform tick quantization is white per shot — no clock/template/fusion can learn it away. T1a converts it to a constant the existing frozen clocks absorb → theoretical residual ~0.4ms capture / ~1.2ms remote. It never reads a pixel (all three invariants structurally unreachable), unlocked = byte-identical to shipped, and it tests on arrival-timestamp logs that already exist. Remote-play lock failure = measured no-op, not regression; capture still banks the full win.

---

## Frame-budget reality (hard `<5ms/frame` cap)

The 17 items sum to **~5.5ms/frame before existing pipeline cost.** The plan is only coherent with:
- **Drop T2d entirely** (−1.2ms) — defer to a real false-lock reappearance.
- **Cut one of T2a / T2c** in wave 4 (overlapping 0.8ms scale-rate producers) — A/B picks the winner.

Accepted-stack budget after cuts stays under the cap. Measure real cost per item in T5a's rig; re-confirm the sum in the wave-4 matrix.

---

## The 13-day waves

### Wave 1 (days 1–3) — clean clock domain + honest ruler
**Items:** T4c, T5a, T1a (shadow), T1e — all off the detection path, **zero invariant exposure**, collision-free lane.
- **T4c** harness must be live before any wave-2 enable decision. Build the `{shot_type × path}` σ-vs-half-width matrix (bias-folded RMS + CIs, sample-count floor → rare cells report `INSUFFICIENT-RESOLUTION`, not `PASS`) + zero-tolerance invariant corpora. Extend `tools/regression/replay_gates.py` + `tools/timing/fused_shadow_report.py`.
- **T5a + T1e** are a pair: the ViGEm loopback rig (T1e) is T5a's before/after proof instrument; its per-press contamination flag protects clock/oracle stats from the outliers that caused the original session drift.
- **T1a ships shadow-mode immediately**, logging would-have-snapped deltas so its enable gate has 2 sessions of data by wave 3.

### Wave 2 (days 3–7) — better measurements
**Items:** T1b, T2a, T3b, T3c. Background: T4b corpus **collection** starts (20k frames/bucket is long-lead).
- **T1b** offline template-library build (labeled dumps, **≥200 shots/type**) is the long pole — start day 3. Frozen library feeds T1c (G4 curvature gate) and T1d.
- **T2a, T3b, T3c** are independent detection-adjacent channels, each with a fallback-to-shipped floor. **T3b + T3c graded jointly** (T3c changes T3b's input pixels). Prefer implementing T3b/T3c in `compressed_meter_reader.py` to avoid the `meter_detector.py` hotspot.

### Wave 3 (days 7–10) — better prediction from those measurements
**Items:** T1c, T2b, T2e, T3a, T2c.
- **T1c** needs T1b's frozen curvature stats (G4 gate). **T2b** needs T2a's motion for its track-compensated residual warp. **T2e** hard-depends on T2a's slide velocity. **T3a** needs T4c for its shadow-clock replay gate. **T2c** runs only as the A/B challenger to T2a's scale-rate.
- **T1a enable decision** made at start of this wave from its wave-1/2 delta logs (gate: `std(delta mod T) < 2ms` over 2 sessions).

### Wave 4 (days 10–13) — cross-check + budget cut
**Items:** T1d, T4b.
- **T1d** requires T1b templates + T1c estimator both proven; **ships with EWMA re-aim FROZEN (telemetry-only)** — otherwise it is a live online-adaptation violation.
- **T4b** finishes veto tuning against its now-complete corpora.
- **Deliverable:** full T4c matrix A/B of the assembled stack + the budget cut (drop T2d, pick T2a **xor** T2c, confirm total <5ms). Consider a **T4c re-baseline** — after T1a+T5a land, oracle σ shrinks (tick-aligned probes), tightening the measurement floor.

---

## Cross-dependency graph (the real one)
- **T1b → T1c** (G4 curvature clamp consumes frozen per-type stats) and **T1b → T1d** (inverse curve + matched template *are* the T1b library).
- **T5a → T1a, T5a → T1c** (tick-snaps and sub-frame crossings are only honorable with sub-ms actuation).
- **T5a ↔ T1e** (rig is T5a's proof; contamination flag guards clock stats).
- **T2a → T2e** (PSF length `L = α·|v|`), **T2a → T2b** (warp quality), **T2a ↔ T2c** (budget conflict, wave-4 A/B).
- **T3c → T3b** (filter upstream of moment pixels — grade as a pair).
- **T4c → everything** (every enable graded through the matrix + invariant corpora).

---

## Invariant risks & mitigations
- **Invariant (ii) tip-clip — most threatened:** T2c (integrated divergence bias inside the +7% margin if isotropy gate leaks), T3b (blur-probe over-subtraction — mitigated by expand-only `box_top = min(moment_tip, hard_mask_top)`, must be verified in T4c's sub-pixel tip corpus, not assumed), T3c/T2e (systematic edge shift — do-no-harm selectors whose thresholds are themselves regression-tested).
- **Invariant (i) décor false-lock:** only T2d and T4b touch acquisition. T2d's arm-time forced promotion of a degraded candidate is the sharpest edge → **deferred.** T2b/T3a/T3b are armed-only measurement channels — structurally cannot create locks.
- **Invariant (iii) 0.6×–1.6×:** T2c freeze-then-re-anchor and T4b cut-veto threshold — both have explicit zoom-sweep fail thresholds; T4c is the tripwire.
- **Session-drift freeze discipline — worst offender is T1d:** its per-type EWMA divergence re-aim is live mid-session adaptation → **ship re-aim FROZEN (telemetry-only), promote only via T4c A/B.** T3a's `clock.observe_entry` stays shadow-clock-only. T1a's PLL state and T3c's noise EMA follow the sanctioned pattern (lock-gate / arm-latch + shadow delta logging + A/B enable) — **T1a is the template all others copy.**

---

## Per-item spec pointers (mechanism seed + numeric fail-gate)

> Full implementation-ready specs (mechanism + pseudocode + regression guard + offline test) are in the workflow journal: `.claude/projects/.../subagents/workflows/wf_f3a566ba-d51/journal.jsonl`. Seeds below.

- **T1a** — 2nd-order PI-PLL on capture-arrival timestamps; integer-tick unwrap makes drops free; gear-shift ACQ/TRK gains + Huber clip + burst freeze; NCO tracks 59.94-vs-60 skew; `schedule_release` snaps to nearest predicted tick − `phi_cal`, clamped ±T/2, **only when locked**. **FAIL:** synthetic post-lock phase RMS > 0.8ms or >1 slip/10k; real capture next-frame median|resid| > 1.0ms / RMS > 1.5ms; no lock within 300 frames on >20% sessions.
- **T1b** — canonical monotone `g_k(τ)`, online estimate ONE param (time-scale `s`); classify type at ARM from 7-dim early-rise feature vector. **FAIL:** any type median|e|_B exceeds A by >2ms at 100ms horizon; fade/moving median|e|_B > 0.75·A (<25% gain on motivating classes); harmful-misclass ≥2%.
- **T1c** — closed-form quadratic-root crossing off T1b curve + residual G4 gate; every reject = today's Kalman crossing. **FAIL:** 2-frames-out median|e| > 6ms HDMI / 9ms remote; p95|e| of accepted > green half-width (14ms nominal); accepted median|e| > Kalman baseline.
- **T1d** — 3-estimator (quadratic / tick-locked / matched-template) Mahalanobis consensus; agree→fire, diverge→fire most-trusted + log; **re-aim FROZEN.** **FAIL:** fused σ > 1.10× min(standalone σ) any type; median|err| > 8ms HDMI / 16ms remote; any armed shot with zero fire.
- **T1e** — ViGEm loopback: intended-press-ts vs actual-delivery, fit distribution, fold variance into schedule. **FAIL:** σ(t_obs−t_sched) > 1.0ms; P99 > 3.0ms; |drift| 2h apart > 0.5ms; dip-test p≤0.05.
- **T5a** — MMCSS pro-audio class + `timeBeginPeriod` + dedicated fire thread priority/affinity + alloc/GC-free hot path + waitable-timer-then-QPC-spin. **FAIL:** AFTER robust σ(observed−deadline) > 0.5ms; AFTER p99 > 2.0ms under load; improvement ratio < 3×.
- **T2a** — sparse LK on ~15 meter-neighborhood points, median-flow velocity-only measurement + affine outlier reject; fuses with Kalman, never replaces color-confirm. **FAIL:** median box-center err after 200ms blackout > 0.5·box_width on wing-slide set; flow-fused > shipped coast on ANY condition (must be Pareto ≥ baseline); >0 new décor locks.
- **T2b** — occlusion mask = skin-chroma AND flow-discontinuity; read un-occluded rows as Tobit censored intervals + phase infill; worst case degenerates to today's coast. **FAIL:** occluded-frame tip err > 1.0 fill-frame or not ≤0.75× blind coast; GT tip outside [fL,fU] in >2% frames.
- **T2c** — affine-fit flow divergence → d(scale)/dt + width DC anchor; freeze-on-high-residual. **FAIL:** per-frame |s_est/s_true−1| > 3%; static-clip drift > 2%; real-zoom endpoint err > 4%; any décor lock/tip-clip.
- **T3a** — CFAR flank detector on dHue/dt at cap axis; shadow-clock only. **FAIL:** recall <95% capture / <90% remote; median|t_evt−t_truth| > 8.3ms or P90 > 16.7ms; false events > 0.5/hr.
- **T3b** — connected chroma-blob 2nd-order moments (centroid + covariance eigvecs), tall-thin-vertical gate, expand-only tip rule, in-situ blur probe. **FAIL:** median|y_tip| > 1.2px or p95 > 3.0px @CRF28; >0 accepts on 20k off-shot corpus; true-bar acceptance <90%.
- **T3c** — noise-gated self-guided filter, strength from local σ_n EMA (~0 on clean). **FAIL:** remote σ improves <20%; capture |tip_on−tip_off| > 0.1px median / >0.25px p99; ≥1 new décor lock.
- **T2e** — flow-gated 1-D Wiener deblur of tip profile, `L=α|v|`, above flow-speed threshold only. **FAIL:** HIGH-frame tip err improves <30%; any non-identical output on gated-off clean frames; fade σ increases.
- **T4b** — scene-threat matrix, two-independent-cues veto stack, cut/dup detectors stop Kalman poisoning. **FAIL:** >0 false-locks any 20k bucket; on-shot acquisition drops >1.0% abs; >0 tip-clip; cut-veto fires >0.5% on-shot.
- **T4c** — self-testing σ matrix; synthetic cases must classify correctly (`N(0,1.2W)`→FAIL, `N(0.9W,0.1W)`→FAIL, n=10→INSUFFICIENT, planted false-lock=5, replay determinism diff=0).

---

## Verification

All from repo root; default interpreter `C:\Python314\python.exe`.

- **Authoritative gate (run before every enable flip):**
  `powershell -ExecutionPolicy Bypass -File scripts\verify_orion.ps1`
  (`-StrictSecurity` for the ship gate; `-Python <path>` to override.)
- **Offline replay gates:** `python tools\regression\run_gates.py` / `replay_gates.py` (floors in `tools\regression\gate_floors.json`). Mark slow suites with the `slow` pytest marker; skip via `ORION_SKIP_SLOW_REGRESSION=1`.
- **Timing / shadow proof:** `tools\timing\fused_shadow_report.py`, `tip_registration_eval.py`, `pts_lock_jitter_report.py`, `oracle_join.py`. New T4c matrix plugs in here.
- **Per-change fast subset (sidecar set):** `python -m pytest tests\test_controller_remap.py tests\test_meter_detector_motion.py tests\test_roi_relock.py -q`.
- **Every item's own offline test** (above) runs on the per-frame PNG dumps / arrival-timestamp logs before any live enable. Enable **per-cell** only after the T4c matrix shows a win with zero invariant-corpus violations.

### Program safety property
Every wave's items **degrade to the shipped path on failure** (unlocked PLL = byte-identical; rejected crossing = today's Kalman; failed flow = today's coast; frozen re-aim = telemetry-only). The program's worst case at every point is **today's behavior.**
