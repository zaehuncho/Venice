# Tip-Timing Backlog — grounded from 6-AI brainstorm × 7-agent validation (2026-07-06)

## How this was produced
Six external models (GPT-5.5, GPT-5.3, two Fable-5, Gemini 3.1, Gemini 3.5) independently brainstormed the "hit the tip" problem and **converged on one architecture**. Seven agents then validated every idea against *our actual repo and recorded data* (not the abstract). This backlog is the result: ranked by leverage×effort, tagged exists/net-new, each with an offline validation.

## The core finding (what every agent agreed on)
1. **Make-rate is latency-limited, not math-limited.** Our own proof (`tools/timing/TIP_TIMING_PROOF.md`) already showed the oracle ≈100% — a perfect fire-frame always exists. Cleverer prediction competes for the residual; **cutting latency + committing closer is the master lever.**
2. **Most of the proposed architecture already exists** in the repo — built, flag-gated, telemetry-only, not promoted to drive release (predictors, RTT engine, ROI-lock, latest-frame queue, shot classifier, direct inject, sub-pixel reader, PTS anchors). The redesign is mostly **promote + plumb**, not build-from-scratch.
3. **The single biggest actionable gap: the bot under-compensates latency by ~50 ms.** Frozen-meter oracle measures real release-path latency at **~70 ms median** (IQR 40–100); the automation models only **~13 ms**. That ~50 ms ≈ 6–7% of fill — enough to miss a 5%-wide window by itself.
4. **Honest ceiling: ~75–90% green on median LAN shots**, set by an irreducible ~10–17 ms per-shot jitter (the drift is compensable; the residual is not) + the 60 Hz input tick. Nobody hits 100% over remote play.

---

## TIER 1 — cheap, high-leverage, do first ("measure latency properly + wire what's built")

| # | Item | Why | Status | Effort | Offline validation |
|---|---|---|---|---|---|
| 1 | **Seed + measure the lead from the frozen-meter oracle** (fix the ~50 ms under-compensation) | Direct make-rate win, independent of any predictor | Oracle data exists; not used to set the lead | Low-Med | Invert fill(t) at F_stop vs release ts on real shots (done → ~70 ms); confirm live |
| 1b | **Boot-probe shots** — a few deliberate warmup releases seed the lead to ~70 ms | Lead reaches truth in 2–3 shots instead of ~15 from zero (our proof says exactly this) | Net-new (only an RTT ping warmup exists) | Low | Replay: seeded vs cold convergence in `tip_predictor_eval.py` panel C |
| 2 | **Wire tick-phase-lock into the fire path** (`TickSynchronizer.align_release`) | Turns ±8 ms 60 Hz tick quantization into ~±2–3 ms; 3 agents independently flagged it built-but-unwired | BUILT, never called on release | Low | Tick-phase **staircase experiment**: sweep commanded release 1 ms/step in free throws, look for the 16.7 ms staircase in frozen-fill readback |
| 3 | **Commit the fork's uncommitted low-latency layer** | `set_controller_state_nowait` + decode/takion changes are working-tree-only — one `git checkout` from reverting the whole direct-inject path | Uncommitted | Trivial | n/a (git hygiene) |
| 4 | **Add the enabling logs**: `pts` column in detframes, the **RTT ms value** (RTTSyncEngine writes ~27k valueless "retargeted" lines), a shot-`seq` id into detframes, a frozen-meter-vs-feedback-UI tag | Unlocks clean per-shot latency + PTS-domain fit offline | Absent | Low | Schema test + re-run the oracle join cleanly |

---

## TIER 2 — real, bigger, after Tier 1

| # | Item | Why | Status | Effort |
|---|---|---|---|---|
| 5 | **Registration as the FAR-HORIZON fusion member** (commit ≳200 ms → registration; <160 ms → Kalman/CNN) | Benchmark: ~2× the ±20 ms make-rate at 200 ms commit (38% vs 16%), flat error where extrapolators blow up. **Conditional: value shrinks as latency drops.** Needs a per-type T-prior | Prototyped offline (`tools/timing/tip_registration_eval.py`); not wired | Med |
| 6 | **Uncertainty/abstain layer** — sigma head on the forecaster → ensemble-disagreement confidence gate → reliability + selective-risk curves | Lets the bot *know when it doesn't know* and widen/hold instead of firing blind; respects the latency-limited reality | Predictors exist; no uncertainty head, no disagreement gate | Med (offline-first) |
| 7 | **Confidence-gate the live lead-learner** | The autonomous global-lead learner runs live and is fed by the unreliable dead-top grade (the LATE-66 artifact); gating updates on fire-time vision confidence is the deeper fix vs the divergence-guard band-aid | Net-new (current gates are post-hoc grade-reliability only) | Low-Med |
| 8 | **min-filter clock + uplink/downlink split** | Separate the uplink latency the release must lead against from downlink/decode jitter it should *not* chase; composes with the oracle. Caveat: our "PTS" is local capture-time, so the split leans on the oracle | Min→EMA today; split absent; needs `pts` log | Med |
| 9 | **VRAM zero-copy decode path** (kill the GPU→CPU→GPU round-trip) | The fork's own perf plan calls it the "single biggest latency win (~8–15 ms)" | Planned (CHIAKI_MAX_PERFORMANCE_PLAN 2.2), not built | High |
| 10 | **Uncap CV export FPS** (`CHIAKI_ORION_FRAME_FPS=0`) | Export defaults to 30; CV should run uncapped | Env-flag exists | Trivial |

---

## TIER 3 — experiments / optional
- **First-order-lag closed-form predictor** `p(t)=100−C·e^{−kt}` (Gemini's best): a cheap drop-in 4th predictor in `tip_predictor_eval.py`; may tighten near-tip at *low* latency; analytically stable (no `bias_ms` hack, no quadratic instability). Build the experiment, not a headline.
- **Variance-quadrature jitter margin** `z·√(σ²_RTT+σ²_age)`: fold frame-age variance into the existing adaptive margin. Small.

## DISCARD (premise doesn't hold for us, or already better)
Full mixture-of-experts, regime-split latency, sub-frame interpolation (120 fps does it deterministically), asymmetric loss (measure first), per-session template warmup, re-opening continual learning (contradicts the deliberate frozen-calibration design), DTW shot-type (type is ground-truth from the injected controller input at zero latency), passive UDP inject→response RTT (no ACK to correlate), "kill the jitter buffer" (there is no AV-sync buffer; the reorder queue is loss-recovery).

## The honest ceiling (say it plainly)
~**75–90% green** on median-difficulty **LAN** shots. Set by irreducible ~10–17 ms jitter (post-drift-compensation) + the 60 Hz console tick. At the current 200 ms+ commit, even debiased registration caps ~38% at ±20 ms because fill-reader variance **projected 200 ms forward** dominates → **the master lever is cutting latency so you commit closer**, where the variance is smaller and the near-tip predictors are sharp. Registration + the lead fix are how you survive the latency you can't remove. Over WAN the uplink jitter dominates and the ceiling collapses — this is fundamentally a LAN bot.

## One-line verdict
Do Tier 1 (measure the lead, wire tick-lock, commit the fork, add the logs) before touching a predictor — it's most of the win for least effort. Then registration-in-fusion + the uncertainty layer. The predictor was never the bottleneck; the latency was.
