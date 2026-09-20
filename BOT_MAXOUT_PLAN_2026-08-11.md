# Venice / Orion — Bot Max-Out Plan (Consistency-First)

**Date:** 2026-08-11 · **Ship:** 2026-08-28 (17 days) · **Branch:** `fix/timing-input-and-remoteplay-blockers`
**Companion:** `BUG_AUDIT_REPORT_2026-08-11.md` · **Full apply-ready hunks:** `.audit_raw/BUILD_*.md`

---

## The thesis — the bot is a beast; the enemy is VARIANCE

"Already a beast but inconsistent" is a variance problem, not a speed problem. Most of the raw latency floor is console-bound (PS5 Remote Play round-trips) and can't be out-engineered. What *can* be driven to zero is the **spread** — the reason the bot is perfect on shot 2 and aborting on shot 20. Every lever below attacks one of three variance sources along the pipe **measure → decide → send**:

| Variance class | What it looks like | Levers |
|---|---|---|
| **Drift** (slow) | aim walks across a session → first-few-perfect, progressive-lates, then aborts | Phase-learner drift clamp; sampler debias; EARLY no-learn; latency corroboration gate |
| **Jitter** (fast) | ±1–15 ms random on the exact Square press | Chiaki sender MMCSS boost; hot-path log demote; `0.0`-epoch velocity guard |
| **Regime** (state) | silently on a wrong latency model / wrong thresholds for a whole session | #47 split-flag (merged); latency corroboration gate; per-style detector profiles |

**Design rule for a 17-day window:** every code change lands **default-OFF / byte-identical**, so the binary you ship is unchanged until you *choose* to arm a flag on your own rig, one at a time, with evidence between each. Nothing here can destabilize the ship build by merely landing.

---

## Phase map (risk-ordered)

| Phase | Theme | Behaviour at merge | Gate to advance |
|---|---|---|---|
| **0** | Zero-behaviour-change hygiene | Identical at steady state | All suites green |
| **1** | Jitter kill | New thread priority only | MMCSS confirmed in Process Explorer + 1 clean session |
| **2** | Drift kill (aim path, flagged) | Identical until flag armed | Counted live batch per flag |
| **3** | GA hardening | New skins / connect time | Per-skin A/B; connect measured |

---

# PHASE 0 — Zero-behaviour-change (land now, ships safe)

These are byte-identical at steady state or observability-only. They can merge immediately and ride the ship build with no risk.

### 0.1 — Verify the #47 fix in CI (no code change)
**#47 is already fixed on-branch** (commit `85f0e20`, verified in `git`). Do NOT re-apply. Just pin it in CI:
```
python -m pytest tests/test_remote_play_frame_pipe.py -q
```
Must be green: `test_capture_route_recovers_cold_after_transient_msmf_fallback` (+ the four sibling revocation tests). Detail: `.audit_raw/CORRECTION_47_already_fixed.md`.

### 0.2 — `0.0`-epoch velocity guard  *(jitter/regime; LOW)*
`remote_play_orchestrator.py:4873` — a fail-closed epoch passes `ts=0.0` into the velocity clock (`0.0 is not None`, so no `perf_counter` fallback), poisoning `_vel_hist` at startup/degraded frames.
```python
# OLD (:4873)
                        result = self._meter_detector.detect(frame, ts=_frame_wall_ms / 1000.0)
# NEW
                        _detect_ts = (_frame_wall_ms / 1000.0) if _frame_wall_ms > 0.0 else None
                        result = self._meter_detector.detect(frame, ts=_detect_ts)
```
Steady-state capture-card frames carry a real epoch → shipping path byte-identical.
**Validate:** `python -m pytest tests/test_orchestrator_capture.py tests/test_simple_meter_reader.py tests/test_remote_play_frame_pipe.py -q`
**Rollback:** revert two lines. **Gain:** rise-velocity/eta/self-arm read correctly from frame 1 instead of self-healing after real epochs flow.

### 0.3 — Per-style detector threshold profiles  *(accuracy scaffold; ~ZERO at default)*
`simple_meter_reader.py` — a `MeterStyleProfile` dataclass whose default `("Arrow2","Red")` reproduces the current constants **byte-for-byte** (verified: every live reader already carries `red_row_thr=0.20`/`green_row_thr=0.15`; `for_quality()` never changes them). Resolves once per (style,colour) key, zero per-frame alloc. Full structure + all 12 threshold call-site edits in `.audit_raw/BUILD_detector_profiles.md`.
**Pin the invariant:**
```python
def test_default_style_profile_matches_shipped_constants():
    from simple_meter_reader import _resolve_style_profile, ReaderParams
    p = _resolve_style_profile("Arrow2", "Red"); d = ReaderParams()
    assert (p.red_row_thr, p.green_row_thr) == (d.red_row_thr, d.green_row_thr)
    assert (p.chevron_density_gate, p.chevron_v_min, p.chevron_s_max) == (0.30, 141, 89)
```
**Validate:** the reader suite + offline replay/framedump harness (byte-identical fill/box/detect stream pre vs post). **Gain:** 0 for the current user; unlocks tuning a new 2K26 skin without touching pixel code — faint-cap frames on a new skin stop bailing.

### 0.4 — Chiaki hot-path log demote  *(jitter; VERY LOW, observability-only)*
`chiaki-ng-src`: demote the 11 per-shot `CHIAKI_LOGI`→`CHIAKI_LOGV` lines on the inject/send path (`orioninputbridge.cpp:224,275,309`; `feedbacksender.c:279,333,502,522,850,869,1073,1082`). `LOGV` compiles out under the shipping `-DNDEBUG`. **Load-bearing strings preserved** (the `streaminfo` readiness marker, the 1/s summary, all `LOGE` errors). Exact lines in `.audit_raw/BUILD_chiaki_mmcss.md` (Change 2).
**Validate:** `orion_build_optimized.bat`; tail a shot burst — per-shot lines gone, summary/readiness/errors remain. **Gain:** removes tens-of-µs–low-ms of `vsnprintf`+stdout jitter per shot.

### 0.5 — Remove the `TCP_NODELAY`-on-UDP no-op  *(polish; LOW)*
`chiaki-ng-src/lib/src/takion.c:282` — a `setsockopt(IPPROTO_TCP, TCP_NODELAY)` on a `SOCK_DGRAM` socket; a no-op on a false premise. Remove to kill false confidence (real input timeliness is Phase 1). Optional; bundle with 0.4.

**Phase 0 exit gate:** full Python suite + `OrionNativeTests` + the fork build all green; replay harness byte-identical. → advance to Phase 1.

---

# PHASE 1 — Jitter kill (the single biggest consistency win)

### 1.1 — MMCSS "Pro Audio" on the Chiaki feedback-sender thread  *(jitter; LOW)*
The thread that encrypts + UDP-sends **every shot packet** runs at NORMAL priority — it can be preempted for up to a scheduler quantum (~1–15 ms) right at the press. Register MMCSS "Pro Audio" in `feedback_sender_thread_func` (mirror of the takion recv thread), reverted on exit. **NOT** `TIME_CRITICAL` (that preempted the MMCSS takion thread and caused shot-correlated lag). FEEDBACK is core 1, TAKION core 0 — separate cores, same class → cannot steal takion's reservation.

Registration (`feedbacksender.c:744`, after the affinity call) + revert (`:1095`, before `return NULL`) — full verbatim hunks in `.audit_raw/BUILD_chiaki_mmcss.md` (Change 1). Dynamic-loaded `avrt.dll` (no new link/header deps), `_WIN32`-guarded. Optional runtime kill-switch: gate on `!getenv("CHIAKI_ORION_NO_MMCSS")`.

**Validate:** `orion_build_optimized.bat` → `build-orion-optimized\gui\OrionStream.exe`. Confirm in Process Explorer: the "Chiaki Feedback Sender" thread's dynamic priority rises into the Pro-Audio range (~15+) vs base 8, sitting alongside the takion recv thread. Rigorous: WPA/xperf ETW — sender-TID ready-queue latency at shot instants drops from up-to-a-quantum toward near-zero.
**Rollback:** `git checkout -- lib/src/feedbacksender.c` + rebuild (or the env kill-switch).
**Gain:** the shot-send fires within microseconds of the wake instead of waiting to be rescheduled — Square-press send variance collapses from milliseconds toward sub-millisecond. **This is the largest single input-jitter source in the product.**

**Phase 1 exit gate:** MMCSS confirmed active + one clean live session with no regression in connect/stream health. → advance to Phase 2.

---

# PHASE 2 — Drift kill (aim path; behind default-OFF flags, arm one at a time)

All four land default-OFF (byte-identical) and are armed **individually on your rig, each behind its own counted live batch**. Order matters: arm the two that directly stop the drift-to-abort first (2.1, 2.2), then the estimator gate (2.3), then the freeze-lift safety (2.4).

### 2.1 — Phase-learner drift clamp  *(drift; LOW)* — `phase_drift_clamp_enabled` (default OFF), `phase_drift_clamp_band_ms` (10.0, clamp [0,60])
The learned aim `learnedPhasePhysicalMs_` rides its own release and walks monotonically (measured 439.2→431.0 ms over one 70-shot batch — most of a landing sigma). Clamp it to `frozenAimPhysicalMs_ ± band` (the value the session opened with), bounding **both** the in-use and the persisted value while still letting a genuinely different jumpshot be learned. It only pulls a positive additive scalar toward the known-good center → **cannot** manufacture an unschedulable deadline (no no-release risk; strictly weaker than the shipped `tip_phase_aim_frozen`).
Clamp inserted in `recordPhaseConstantSample()` after `:13914`; 5-site flag plumbing. Full hunks: `.audit_raw/BUILD_phase_drift_sampler.md` (Change 1).

### 2.2 — Sampler debias (schedulability-conditioned veto guard)  *(drift; LOW–MED)* — `phase_veto_schedule_guard` (default OFF)
The symmetric live-meter veto demotes a healthy *still-schedulable* phase tip onto the sampler's crossing, which carries a +61–92 ms EARLY bias and instantly reports its deadline missed → `live_tip_deadline_missed`, `release_seq=0` (the 16-abort census). This guard declines that demotion **only** when keeping phase is schedulable AND demoting to the sampler is not.
> **Why this is NOT the pulled `phase_veto_directional`:** that flag cleared the veto for *every* sampler-earlier disagreement — including the 12-pin synthetic regime where the sampler crossing is *itself* schedulable and demoting releases cleanly (which is why those pins pass today). This guard fires **only** when `samplerCommandDeadlineMs <= atMs` (sampler NOT schedulable); in the pin regime the sampler *is* schedulable, so the guard stands down and the demotion proceeds byte-for-byte → **pins stay green**. It can only swap a proven-imminent-abort for a proven-schedulable release; it can never turn a schedulable release into a never-releasing wait — the exact property the pulled flag lacked. `phase_veto_directional` is left untouched and OFF.

Guard inserted in `canonicalAutonomousTipDecision` after the untouched directional gate (`~:11030`); 5-site plumbing. Full hunks: `.audit_raw/BUILD_phase_drift_sampler.md` (Change 2). **Because it changes live release timing, arm only behind a counted live batch** (standing rule).

### 2.3 — Latency-authority jump corroboration gate  *(regime/drift; LOW–MED)* — `ORION_LATENCY_JUMP_CORROBORATION` (env, default OFF), allowance 45 ms
A confident far label (the 303.8 ms / sd 6.0 spike vs ~210 baseline) is folded straight into the posterior and owns the lead for ~5 shots. Quarantine any label deviating > allowance until a **2nd consistent same-side sample** confirms it; a lone spike costs 0 shots, a real regime change is delayed ~1 shot, and a persistent streak force-promotes so it can **never stall**. Only bites without a Shot Lead or under an applied meter delay (the Shot-Lead path is already insulated). 5 hunks in `.audit_raw/BUILD_latency_gate_early.md` (Task A).
**Validate:** `python -m pytest tests/test_latency_estimator.py -q` (green flag-OFF).

### 2.4 — EARLY-contested no-learn (freeze-lift safety)  *(drift; LOW)* — `grade_early_contested_no_learn` / `ORION_EARLY_CONTESTED_NO_LEARN` (default OFF)
The self-grade EARLY magnitude uses a fixed 5.5 ms/pp near-anchor slope; near the decelerated tip a contested low-peak fade yields an inflated "−57 ms". Today the freeze masks it, but if the freeze is ever lifted (the demo/tuning path) it walks the aim. Gate EARLY-magnitude **learning** behind `peakReachedGreen == false` — a shot whose meter never reached green teaches the aim nothing. **Verdicts and displayed `errorMs` are unchanged**; only the learner is starved. 6 hunks in `.audit_raw/BUILD_latency_gate_early.md` (Task B).
> This is the *general* fix for the log-signal that misled the audit and a prior tuning session. Pairs with the report's §5.2 recommendation to emit `fill_gap_pp` instead of synthetic ms for contested shots.

**Phase 2 validation (all four):**
```
cmake --build native_orion/build --config Release --target OrionNative OrionNativeTests
ctest --test-dir native_orion/build -C Release -R OrionNativeTests --output-on-failure
python -m pytest tests/test_latency_estimator.py -q
```
All green with flags OFF (no existing test edits needed). Optional new pins specced in the stash for the two engine flags.
**Phase 2 exit gate:** for each flag, a counted live batch on the owner rig showing tighter EARLY/LATE spread and zero `live_tip_deadline_missed` at the session tail before promoting it toward default-ON.

---

# PHASE 3 — GA hardening (post-EA, by ship+)

### 3.1 — Re-derive the shipped phase seed
`learning.json.learned_phase_physical_ms=278` vs `tipPhaseSeedPhysicalMs=319` — a fresh install starts **41 ms** off this rig's tuned aim. Pool `PHASE SAMPLE effective_const_ms` medians across installs and re-seed before GA so new customers start on-aim.

### 3.2 — Cap-detection second anchor  *(accuracy; MED, own A/B)*
The single largest undetected-frame bucket (**875 / 4637** in the verification session) is `no_cap_anchor` bails when the green cap is faint. Add a colour-agnostic second anchor (silver-chevron top — already computed — and/or a luma sliver) so faint-cap frames yield measured reads instead of not-detected → more fresh samples → lower eta variance. Behaviour-changing (former not-detected → detected), so its own env flag + A/B, **after** the Phase 0 profile refactor. Spec in `.audit_raw/BUILD_detector_profiles.md` §4.

### 3.3 — Senkusha connect-time compression  *(latency; MED)*
Connect is ~3.1–3.4 s, dominated by Chiaki sidecar bring-up (the 2.5 s fallback is cosmetic). On the known bot LAN, force the Senkusha MTU fallback (`mtu_in=mtu_out=1454`) after a single RTT ping (or build with it off) + drop the fixed 10 ms `session.c:575` sleep — a few hundred ms on LAN. Risk: wrong MTU on a non-1454 path; safe on a controlled LAN, so gate it.

### 3.4 — Service per-packet pooling + port-range alignment
From the audit: pool the per-packet `Packet`/broadcast allocations in `MeterDelayIntercept.cpp` (allocator churn on the LocalSystem hot path), and narrow `kCourtPortMax` 30099→30020 so meter-delay stops silently no-oping on ports >30020. Both MEDIUM, non-timing.

---

## Global flag enable-order (owner rig, after code merges default-OFF)

Land all phases as code first (ship-safe, nothing changes). Then arm in this order, one session of evidence between each:
1. **MMCSS** (1.1) — pure jitter kill, no aim change. Expect immediate tighter clustering.
2. **`phase_drift_clamp_enabled`** (2.1) — stops slow drift. Expect the tail-of-session lates to stop growing.
3. **`phase_veto_schedule_guard`** (2.2) — stops the false demotions → the `live_tip_deadline_missed` aborts. Expect zero session-tail aborts.
4. **`ORION_LATENCY_JUMP_CORROBORATION`** (2.3) — only if you run without a Shot Lead or with meter delay on.
5. **`grade_early_contested_no_learn`** (2.4) — arm before ever lifting the calibration freeze.

Each flag is independently reversible with **no rebuild** (unset the key/env). If any session regresses, drop that one flag and continue.

---

## What this buys you, in one line
Phase 0 removes avoidable noise for free; Phase 1 collapses the shot-send jitter to sub-millisecond; Phase 2 stops the aim from drifting and the biased sampler from killing good shots — which together turn "*sometimes* a beast" into "*every shot* a beast, until real lag or jitter says otherwise." That last clause is physics; everything before it is now yours to close.

---
*All diffs verified against live code by the build agents; full apply-ready hunks with verbatim OLD/NEW blocks are in `.audit_raw/BUILD_*.md`. Nothing in this plan has been applied to the working tree — it is yours to land phase by phase.*
