# A2 × B1 De-Conflict — Diagnosis + Surgical Fix (to flip `ORION_READER_OCCL_WIDE` ON)

_Generated 2026-07-18. Source: provably-fable research workflow (`wf_dbf7a17a-f92`) run read-only against `C:/Users/Administrator/Desktop/NexusVision`, branch `feat/detection-template-anchor-qml-render`. The diagnosis was **empirically reproduced at gate level** and the recommended fix **validated on a scratchpad copy** (repo untouched). Spec-only — nothing applied._

---

## Context

Five default-gated reader flags each pass 46/46 individually, but **all-5-ON regresses to 44/46** — a flag interaction blocking the flip of **B1 (`ORION_READER_OCCL_WIDE`, the left-side arm-cross occlusion fix)** to ON. Bisect: `mid_rise_glitches=1` on `session_20260706_190737` (A2×B1) and `offshot_falselock_episodes=1` on `session_20260707_135309` (fragile all-5 knife-edge); dropping A2 **or** B1 clears both. Goal: flip B1 ON, hold 46/46, preserve A2's stale-break value **and** B1's widened coast.

**Measured head status (official `run_gates`, all-5-ON):** `190737` mid_rise=1 (the sole live failure → 17/18 on the two-session set); `135309` `offshot_falselock=0` on the current head/window — **the false-lock episode is latent, not firing on head.** So the ship-blocker reduces to the mid-rise glitch; the false-lock is an optional hardening follow-up.

---

## Diagnosis 1 — the mid-rise `>20pp` backward drop (A2 × B1)

**Reproduced:** shot span (212,290), peak 94.7; window ordinals **o=218→o=219: 48.07 → 11.85 = 36.2pp backward** → `replay_gates.py:285-303` counts 1. Mechanism, fully traced in `simple_meter_reader.py`:

1. A2's **coast-steal** (`:2455-2486`) fires earlier (o=123) and supersedes a stale dead-hold — its *designed* behavior — so the reader keeps producing fresh **armed rising-red** reads late into shot 2 (o≈169-187, fills 40→50).
2. Every armed rising-red read **re-latches B1's 900ms window** at `:2834-2838` (`_occl_wide_until = ts + 0.9s`); last latch ~o=187 → deadline ~o=241.
3. The meter vanishes; the coast from o=188 emits the **frozen 48.07 echo** as `detected:True meter_memory` (`:2668-2671`) and **survives the unarmed inter-shot gap** (o=196-211) *only* because `_grace = _grace_live or _occl_wide_live` (`:2551`) selects the 0.002 armed decay (`:2555`) + the armed-hold `CONF_MIN` floor (`:2570-2572`). The window **by design outlives disarm** (never cleared at `:2578-2598`).
4. The echo enters PASS1 span 3 still detected; at o=215/o=217 relocate returns **degenerate hits (subpix≤0.5)** → `green_hold` (`:2684-2689`) **re-emits the same 48.07 as a fresh detected frame while resetting `_coast_n = 0`** (`:2676`).
5. o=219: first genuine red of the **new** shot reads 11.85. **B1's own anti-glitch launder** at `:2690` requires `_coast_was >= 12` but sees `_coast_was = 1` (reset by the o=217 green_hold accept) → **skipped**; no other backward guard exists on the default path (`:2806` assigns, `:2885` emits unconditionally) → the unlaundered `48.07 → 11.85` step.

**The defect in one line:** a `green_hold` coast-counter reset defeats B1's own `>=12` launder floor **at the inter-shot seam that B1's 900ms window itself created.** Both flags necessary — control runs confirm: A2-OFF/B1-ON → dead-hold never yields late rising reads, window expires pre-gap, 0 glitches; B1-OFF → unarmed echo dies in ~5 frames (`CONF_RATE 0.01`) before span 3, 46/46.

## Diagnosis 2 — the all-5 off-shot décor false-lock (latent on head)

Compound, knife-edge: (1) B1's window is **never cleared on disarm or normal lock-drop** (`:2578-2598` doesn't touch `_occl_wide_until`; only the A2 hard break `:2979` + `_reset_state:1305` do) → up to 900ms of off-shot wide-grace; (2) off-shot `_occl_wide_live` keeps the coast **detected + static** (`meter_memory`, `:2668-2671`); (3) once `_coast_n>=8`, A2's steal (`:2455-2458`) runs a **band-wide cold scan and re-seats on a décor column with no rise-probation** (probation is armed only in the true cold branch `:2538-2541`) and no N4 refuse-cooldown (`:2498`); (4) the breaker can't kill it inside the metric's 5-frame `MIN_CORE` and is frozen by `_occl_wide_live` grace (`:2920-2921`, counter pinned at `:2936-2937`) + A2's armed-frames-as-grace (`:2929-2930`).

**Why any single flag clears it:** B1 supplies the off-shot survival budget; A2 the band-wide probation-free re-seat + detected continuity; A3's `_scale_est` widens the steal scan gates (`_scale_gates` in `_scan`, `:1585,:1603-1605`); B2's `_band_eff()` shifts the scan band (`:2458`); A1's lock-drop clearing re-segments the run's fills (whether range stays `<12.0` STATIC). Each nudges one frame across one of the three episode criteria — the definition of the knife-edge.

---

## De-conflict options (3 of 4 killed by the agent)

### ✅ Option 1 — Held-echo provenance launder floor (RECOMMENDED, VALIDATED)
Make B1's `>20pp` launder fire on **`>=12 consecutive HELD-fill emissions`** (coast `meter_memory` + `green_hold` frames) instead of `>=12 coast frames`, so a green-hold re-lock can no longer reset the floor at the seam. New counter `_held_run`.

**File anchors:** `simple_meter_reader.py:2690` launder gate; increments `:2609` + `:2689`; resets `:2472` (steal reseat), `~:2712` (occl_relock body), `:2979` (A2 hard break), the genuine-red reset just before the N4 block `~:2717`, and init `:1306` in `_reset_state`. **~10 lines, every read guarded behind `self._occl_wide`** → shipped default set byte-identical.

```python
# _reset_state (:1306)
self._held_run = 0
# coast branch, after self._coast_n += 1 (:2609)
self._held_run += 1
# green_hold block, after green_hold = True (:2689)
self._held_run += 1
# launder gate (:2690)
if (self._occl_wide and (_coast_was >= 12 or self._held_run >= 12)
        and evidence == "red" and not green_hold
        and self.last_fill - subpix > 20.0):
    ...existing occl_relock reseat...
    self._held_run = 0
# genuine-red reset (before N4 block, ~:2717)
if evidence == "red" and not green_hold:
    self._held_run = 0
# also reset at steal reseat (:2472) and A2 hard break (:2979)
```

**Mechanism:** the o=215/217 green_hold accepts now **extend** the held-run rather than reset it, so at o=219 B1's own designed one-frame `occl_relock` launder fires (`detected:False`), the metric's `prev` resets (`replay_gates.py:299`), continuity resumes on the new shot's real rise. A2's steal, B1's window/grace/floor — all untouched.

**Regresses if wrong:** one extra `detected:False` frame wherever a genuine red `>20pp` below the held value follows `>=12` held frames → could shave `within_shot_detect_rate` (floor 0.85) or delay a genuine instant-deflate report by 1 frame. **Measured: det_rate 0.9803 → 0.9788, well above floor.**

**Empirically validated** (patched scratchpad, repo untouched, gate-exact 2-pass): `190737` all-5 mid_rise **1 → 0**, identical PASS1 spans, det_rate 0.9803→0.9788, maxgap 3 (floor 5), median_peak 91.55 unchanged; `135309` all-5 unchanged (0/0). **All 6 `steal_reseat` events identical pre/post at ordinals {123,320,423,516,686,734}** → A2's supersession preserved.

### ⚠️ Option 3 — Clear `_occl_wide_until` on confirmed lock-drop (optional falselock hardening only)
At `:2578-2598`, add `self._occl_wide_until = 0.0` so a dropped lock's window can't extend the next cold lock's coast. **Does NOT fix the mid-rise seam** (the lock never dropped there). The stronger *disarm*-clear variant **destroys B1's core value** (live hw release happens at the shot; the arm-cross occlusion is post-disarm). Ship separately as pure falselock hardening **only if the knife-edge ever fires on head.**

### ❌ Option 2 — Gate A2's steal off during B1 windows — KILLED
The traced glitch **doesn't go through the steal** (the steal scan found nothing at o=212-214); this would fix `190737` only by suppressing the o=123-class steals — i.e. **regressing A2's supersession**, the exact recovery mechanism it exists for.

### ❌ Option 4 — Generic backward-step rejection on the default path — KILLED
A hold is worse than a launder (keeps advertising the stale echo into the new shot), and a genuine instant deflate (o=133→137: 95.1→50.3, real) would be **wrongly held**, corrupting deflate tracking + the `fill_vs_chain` gate on `session_20260704_210801`. This is precisely why the `_trajectory_gate` (`:2350-2376`) ships `_robust`-gated default-OFF.

---

## Recommended: **ship Option 1.**

Minimal (~10 lines), flag-guarded (default set unchanged), validated. Preserves A2's stale-break (steal/reseat/launder `:2455-2486`, fresh-rise gating `:2659-2667`/`:2824-2833`, hardened breaker `:2929-2987` all untouched; 6 steal events identical) and B1's widened arm-cross coast (900ms latch, grace/decay/floor, `meter_memory` hold all untouched; the 27-frame held run into span 3 still emits `detected:True` — only the already-discontinuous seam read gets B1's own one-frame launder).

---

## Test plan (all from repo root, `C:/Python314/python.exe`)

- **Step 0 — baseline (recorded):** `$env:ORION_READER_OCCL_WIDE='1'; python tools/regression/run_gates.py --sessions session_20260706_190737 session_20260707_135309 --no-timing` → 17/18, FAIL mid_rise=1 on `190737`.
- **Step 1 — apply patch, re-run same:** PASS = **18/18**; `190737` mid_rise==0; both sessions offshot_falselock==0, top_clips==0, within_shot_detect_rate≥0.85, within_shot_maxgap≤5, median_shot_peak≥82, camera_adapt x1.6≥0.78 & x0.6≥0.78.
- **Step 2 — full all-5 sweep:** `$env:ORION_READER_OCCL_WIDE='1'; python tools/regression/run_gates.py --no-timing` → **"DETECTION GATES: 46/46 passed"**, `read_ms_median < 5.0` every session.
- **Step 3 — default-set no-change:** `Remove-Item Env:ORION_READER_OCCL_WIDE; python tools/regression/run_gates.py --no-timing` → 46/46 (launder short-circuits on `self._occl_wide` → shipped behavior identical).
- **Step 4 — A2 preservation (proxy):** `replay_simple_reader.py --session session_20260706_190737 --jsonl <out>`; `count(reject=='steal_reseat')` at ordinals {123,320,423,516,686,734} preserved; `frozen_fill_max ≤` head; rising_frames within 2%.
- **Step 5 — B1 preservation:** from the same jsonl, max within-shot contiguous `meter_memory` run ≥ B1-alone (the 27-frame o=188-214 hold persists); `within_shot_maxgap ≤ 3`; `count(reject=='occl_relock') == 2` on `190737` (o=219 seam + o=608; was 1 pre-fix — the new one **is** the fix firing).
- **Step 6 — pytest:** `python -m pytest tests/test_simple_meter_reader.py tests/test_detection_regression.py` all green (A2 tests :500,515,624-653; B1 latch :684-711).
- **Step 7 — knife-edge watch:** re-run Step 1 with `$env:ORION_GATE_WINDOW='0'` on `session_20260707_135309` (sweeps full session outside the 900-frame window) → offshot_falselock stays 0.

---

## Invariant check (Option 1)

1. **0 off-shot false-locks:** touches only the in-lock launder **measure** inside B1's flag-guarded backward-step branch (`:2690`); off-shot acquisition (`:2487-2543`), steal gates (`:2458→_scan:1585`), wide latch (`:2834-2838`), breaker (`:2920-2987`) all byte-identical. A launder can only **remove** a detected frame from an off-shot run, never extend one. Measured 0→0 both sessions.
2. **No tip-clip:** no geometry/segmentation/box/top_row touched (`_read_fill:2019` untouched); the launder emits bbox `[0,0,0,0]` on a `detected:False` frame, which the `top_clips` scan (`replay_gates.py:280-283`) skips. Measured 0→0.
3. **0.6×–1.6× scale survival:** `_held_run` is a pure frame count, no pixel/scale constant; `_scale_gates`/`_band_eff`/vzoom/A1/A3 untouched. Head 0.882-0.883 vs 0.78 floor.

**Budget:** +2-3 integer ops/frame, no new scans → `read_ms_median` unchanged, well under 5ms. Default set (`ORION_READER_OCCL_WIDE=0`) is a dead counter → behavior-identical (Step 3: 46/46).

---

_Every claim verified against the shipping tree; the fix was validated on a patched scratchpad copy, not applied to the repo. To implement: apply the Option 1 diff, then run the test plan Steps 1→7._
