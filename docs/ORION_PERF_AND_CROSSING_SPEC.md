# Orion — Frame-Budget Sheds + Real-Time Tip-Crossing Predictor (fix 2a)

_Generated 2026-07-18. Source: provably-fable workflow (`wf_1cca5cbb-cb9`, 3 high-effort agents, read-only `NexusVision`). The perf agent wrote a monkeypatch stage-profiler and **prototyped + frame-exact-verified** its top sheds over 1,099 real frames; the predictor agent grounded its estimator against `fill_kalman.py`'s own documented failure. Spec-only — nothing applied._

---

## PART A — Where the ~5.5ms goes, and how to get back under 5.0

### The headline: the 5.63ms is NOT the armed hot path

Measured, all-flags-ON, on a 449-frame slice of `session_20260717_231912`:

| State | % of gate-window frames | Cost (this box → gate-box ~×2.2) |
|---|---|---|
| **unarmed-cold** | 50% | 2.56ms → ~5.6ms |
| **coast/steal (band-wide `_scan`)** | 19–29% | 2.57ms → ~5.7ms |
| **armed-locked HOT PATH** | 20% | **0.73–0.94ms → ~1.6–2.1ms** |
| locked-coast <8 | 9% | 1.37ms |

**`read_ms_median` (`replay_gates.py:306`) is the median over ALL window frames — and the median frame is a cold or steal frame, not an armed one.** 69% of frames run a band-wide 1810×520 `_scan`; the armed timing path was never the problem. Of the cold frame's 2.6ms, **~1.6ms is provably dead work**: `morphologyEx` CLOSE + `findContours` on a mask with **0 red px** (measured on 80/80 idle cold frames), plus a **byte-identical second full-band `inRange`** in `_acquire_structure`. The steal path (`read() :2512`) runs the same band-wide `_scan` on every coast frame past 8 (measured 1.46–1.74ms).

### Ranked sheds (S1+S2 prototyped & frame-exact)

| Shed | Mechanism | File:line | Saving | Status |
|---|---|---|---|---|
| **S1 empty-band early-exit** | `countNonZero(raw)` (0.016ms) before `morphologyEx`+`findContours`; return `(None,0.0)` when `raw_px < ceil(red_frac_min·w_min·hmin/15)` — below that floor a passing contour is **mathematically impossible** (15 = CLOSE-kernel area bound) | `simple_meter_reader.py:1664-1667` (all `_scan` callers) | **−1.6ms** | **MEASURED, output-identical** |
| **S2 band-mask reuse** | Per-`detect()` memo of the band `inRange`; cold-miss computes the byte-identical full-band mask twice (`_scan:1665` then `_acquire_structure:1749`, same region+bounds) | `:1749` reuses `:1665` | **−0.78ms** | **MEASURED** |
| S3 unarmed coast NCC + steal-scan decimation | run NCC/steal `_scan` every 2nd frame **only** when unarmed+unlocked | steal `:2509-2512`, ncc `:1882` | −0.1..−0.3 | estimated; +16.7ms off-shot re-lock latency; needs A2 ordinal re-sweep |
| S4 (1a secondary) | VZOOM_DOWN turns steal frames → cold frames | — | −0.08 | MEASURED; **do not double-book** |
| ~~S5 `COLDSCAN_DECIMATE`~~ | the master plan's proposed primary shed | — | ~0.45 | **KILLED** — dominated by S1+S2 **and** actively harmful (delays fade pop-in acquisition by a frame, the one cold consumer that IS timing-relevant) |
| S6 NCC window-area cap | trim the p95 4.0ms NCC at the 420×520 widened window | `:1882` | ~0 median | optional p95 trimmer |

### Net projection & honest conclusion

**S1+S2 alone** (both prototyped, verified frame-exact — det/fill/bbox/rise/rejection identical over 1,099 frames across 3 sessions, 0 mismatches): this-box median `2.78→1.03`, `2.75→1.71`, `3.86→2.85` (ratios 0.37–0.74). **Projected gate-box `read_ms_median`: 5.63 × (0.37..0.74) = 2.1–4.2ms** — under 5.0 on every measured content mix, inside the 4.3–4.9 ideal even at the worst ratio, **without touching the armed path and without the plan's decimation shed.**

- **MEASURED:** all per-stage costs, state occupancy, S1+S2 savings + output identity, the 1a −0.08 secondary, the S5 kill.
- **ESTIMATED:** the ~2.2× box factor (inferred from docs' 5.63 baseline vs 2.51 measured here) → the absolute gate-box landing point.
- **Confidence:** HIGH that <5.0 is reached with S1+S2 (worst ratio lands 4.2; the shed removes cost from exactly the frame population the median sits in). MEDIUM on the precise absolute number until the human reruns `replay_gates` on the gate box.

**Honest residual:** the busiest decor-red session's 0.74 ratio leaves only ~0.8ms gate-box margin — if it lands 4.2–4.9 that's under gate but thin, and S3 (unarmed-only decimation) is the named next lever. **The plan's `COLDSCAN_DECIMATE` should be retired.** Nothing needs a quality trade on current measurements.

### Instrumentation & tripwire

- **Instrument** (env-gated `ORION_READER_PERF=1`, OFF = wrappers never installed, hot path byte-identical): wrap `_scan` (tag `scan_band` if region ≥300k px else `scan_win`), `_find_green_tip`, `_ncc_propose`, `_acquire_structure`, `_read_fill` with `perf_counter` deltas into `self.last_perf`, cleared at top of `detect()`. Add `perf`/`stage`/`state` to the `replay_simple_reader.py:100-117` row and per-state medians in `replay_gates.py:126-133`.
- **Tripwire (gate box, before/after S1+S2, same env `VZOOM_DOWN=1 OCCL_WIDE=1`):**
  - **(A) IDENTITY** — `replay_gates.py --dump --out after.json`; diff vs `before.json` ignoring only `read_ms_median`/`_wall_s`: **every other field must be BYTE-IDENTICAL** (`within_shot_*`, `offshot_falselock_episodes`, `top_clips`, `mid_rise_glitches`, `median_shot_peak`, `camera_adapt`, `fill_vs_chain`). Any diff = not the pure shed → revert.
  - **(B) GATES** — `run_gates.py --no-timing` → "DETECTION GATES: 46/46 passed", `read_ms_median < 5.0` every session.

---

## PART B — The real-time tip-crossing predictor (fix 2a `ORION_LIVE_TIP_FIRE`)

### Estimator: age-weighted Local Weighted Quadratic Regression (LWQR)

A **hardening of the existing `TemporalSampler`** (`AutomationEngine.cpp:124-316`), not a new filter.

- **α-β — KILLED:** fixed gains assume uniform dt; a gain low enough to kill the ~0.4pp sub-pixel noise **lags the fast fade (fires late)**, high enough to track it **whipsaws (fires erratic)**; no covariance to reopen after a gap → post-occlusion overconfidence.
- **Kalman constant-accel — KILLED as owner:** `fill_kalman.py`'s own header documents it — the CA model can't represent near-tip deceleration, so mid-fill extrapolation predicts the tip **~75ms EARLY**; its `+bias_ms` patch is a learned per-regime constant = **the exact drift class 2a escapes.**
- **LWQR wins:** regression on raw irregular timestamps is **variable-dt native** (no discretization); a missing frame is just one fewer point; a gap widens the fit CI (honest reopening); age-weighting keeps it responsive near the tip without whipsaw.

### Crossing solve (closed-form, with the load-bearing curvature branch)

All on the **capture timeline** (`captureTsMs`, bridged once via the A0 epoch↔steady bridge) so IPC jitter never smears the solve. Fit `f(x)=c+b·x+a·x²`, target `T_pp` = green-band-derived tip:
- `last.fill ≥ T_pp` → `t_c = t_last`;
- `use_quad ∧ |a|>1e-9`: `disc=b²−4a(c−T_pp)`; if `disc≥0` → `t_c = t_last + smallest root in [0,1200]ms`;
- `a < −1e-7` (**concave-down** — meter decelerating, will peak below target) → `t_c = t_last + vertex(−b/2a)` with the shipped peak-clamp (`:297-306`).

**This is the fix:** linear extrapolation of current `v` over the ~70ms lead **overshoots a decelerating rise → predicts the crossing early → input lands early → missed fast fade.** The residual-gated quadratic + peak-clamp produce the later, physical crossing. Then issue the release at `t_c − L`.

### Lead L — frozen-safe

`L = L_fixed(path) + rtt_now + TICK_WAIT_EXPECT`. **Two independent conjugate-normal posteriors on `L_fixed` keyed by PATH {capture_card, remote_play}** (the fixed leg genuinely differs — HD60X pipeline vs H.264 decode+present; a path switch must not drag a stale posterior). **Teacher: ONLY the frozen-meter oracle's rise-freeze labels** (censored-margin tail inversion, MAD outlier gate, 2-streak reopen — all shipped) + warmup probes. **NEVER the green self-grade EMA — that self-grade loop is precisely what drifted the clock to 250ms.** Before confident: bounded fallback to the persisted per-type clock, capped by the tip-gate deferral.

### Confidence gate (exact predicate)

Evaluated every 4ms tick + on every fresh sample: `LIVE_OWNS := F1 ∧ F2 ∧ S1 ∧ S2 ∧ T1 ∧ T2 ∧ G1 ∧ R1`:
- **F1** last sample fresh-accept ∧ `(now − t_last_fresh) ≤ 50ms`
- **F2** `freshAccepts(now,150ms) ≥ 3` ← **fix 1a is the prerequisite** that keeps F2 true through the arm dive
- **S1** fit `n≥4 ∧ span≥50ms`; **S2** `rms_w ≤ 1.0pp`
- **T1** `t_c > now ∧ (t_c−now) ≤ 250ms`; **T2** `v_fit ≥ 0.02pp/ms ∧ fill < target`
- **G1** max inter-sample dt in `[now−120ms, now] ≤ 60ms ∧ (t_issue − t_last_fresh) ≤ 80ms`
- Else → **backstop clock.** Never fires blind on a bad read; never abstains through a real release.

### Fade robustness (no per-type branch)

Every type-dependent quantity enters as a **live measurement** (`v,a` from the fit, tip+`w_time` from the color band). Time error from fill noise `≈ σ_f/v`: a fade rises ~0.28pp/ms (~2× standstill), so the same ~0.4pp noise costs **~1.4ms/sample on a fade vs ~3ms on a standstill** — the fade is *easier*, not harder; its shorter rise is bounded by the gate needing only `n≥4/span≥50ms`. Estimated `σ_cross`: **5–8ms standstill, 4–7ms fade** (pre-measurement).

### Cost & falsifiable test

- **Cost: 0.05ms**, and it runs in the **native engine** (`TemporalSampler`+`scheduleFire`), **0.00 added to `read_ms_median`.**
- **Test:** `replay_simple_reader.py --session … --jsonl` on `logs/diagnostics/framedump/session_*` (standstill-dense `231912`; fade-dense `190737`, `001259`); hand-label GT tip frames (`{first_frame, tip_frame, class, green_lo_pp, green_hi_pp}`); new `tools/timing/live_tip_fire_eval.py` (~200 lines) replays the predictor at a **swept L**, reports predicted-crossing vs GT error **standstill vs fade separately**. **Success: median <0.5 frame, tail inside the green-window half-width.** Report the ±0.5-frame (8.3ms) labeling floor with every result.
- **LIVE-ONLY:** the true per-path end-to-end L (every offline number is a seed; first live batch measures it, capture & remote as separate batches), remote `rtt_now` (blocked on the CHIAKI P9 STEP-1 q.rtt probe), tick-phase freshness, H.264 blur noise floor.

---

## PART C — Integration (the two live in different budgets)

**Budget reconciliation:** the predictor's 0.05ms runs in the **native engine**, not `read()` — so it adds **0.00** to the reader gate. Two separate ledgers:
1. **Reader median:** S1+S2 take gate-box `read_ms_median` 5.63 → 2.1–4.2ms. Predictor consumes none of it. **<5.0 with 0.8–2.9ms margin.**
2. **Fire-path per-frame:** armed reader ~1.6–2.1ms (unchanged by S1/S2 — output-identical) + IPC/sidecar + engine fit+solve+gate 0.05ms, against a 16.7ms frame period → **~14ms slack.** T1 burst-sampling (if it ships) buys extra armed reads at ~1.6–2.1ms each — ~6 in a 100ms tip window shift the median ~0. No conflict.

**Key interaction (stated as an INVARIANT, not luck):** the predictor's F2/S1/G1 gates **die if armed-path reads are decimated.** The perf plan preserves density by construction — S1/S2 are frame-exact everywhere; S3's gate excludes armed/hw-armed/grace/occl_wide. **Any future shed touching `(armed ∨ hw-armed ∨ grace)` frames MUST fail review.** And `COLDSCAN_DECIMATE` gets a **second independent kill**: it delays fade pop-in acquisition, corrupting the very first samples the predictor needs.

**Implementation priority (across both):**
1. **Shared instrumentation** (`ORION_READER_PERF` wrappers + unified JSONL row) — prerequisite, zero product risk, serves both.
2. **S1+S2 sheds → Phase-1 tripwire on the gate box** — largest certain win, output-identical, replaces the estimated ×2.2 factor with a measured number, settles the budget.
3. **Predictor offline harness** (`live_tip_fire_eval.py`) + labels + sweep — pure offline, falsifies the estimator per class/L before any C++ spend.
4. **Engine LWQR hardening** + crossing solve + confidence gate + per-shot telemetry — then the live batch measures L.

**Honest gaps:** (1) the ×2.2 gate-box factor is inferred — absolute <5.0 unproven until the tripwire runs there (busiest session leaves ~0.8ms margin); (2) the true per-path L is closed-loop-only — the first ≤3 shots of a cold session ride the persisted clock bounded only by tip-gate deferral; (3) `σ_cross` (5–8/4–7ms) is estimated until Phase 2 — **the tightest fade cells (~6.5ms half-width) may never clear the threshold, in which case 2a correctly defers those to the backstop rather than fires wrong.**

_Nothing applied — spec-only. Sequence: instrument → S1+S2 + tripwire (gate box) → predictor offline harness → engine LWQR → live L batch._
