# Standstill LATEs — forensic report (2026-09-19)

Read-only analysis. Sources: `D:\NexusVision\shot_records\*.jsonl` (24 sessions, 1 548 shot
records with press_ts ≥ 2026-09-17T10:00Z), `logs/orion_native.log.1` (2026-09-18T00:14Z →
18:29Z) and `logs/orion_native.log` (18:29Z → 2026-09-19T02:48Z), and the 60 fps reader trace
`D:\NexusVision\framedump\session_20260917_142445\frames.csv`.

Scope reproduced exactly: over the two log files, OPEN + WIDE OPEN standstills went
**76 EXCELLENT / 18 LATE / 2 EARLY**, matching the first pass. Since 2026-09-17T10:00Z there
are **401 standstills with a banner verdict** (276 EXC / 80 LATE / 29 EARLY + 16 with no gap).

---

## 1. Ranked recommendation

| # | Fix | Verdict | Effect on the 18 Sept-18 OPEN LATEs | Risk |
|---|-----|---------|-------------------------------------|------|
| **1** | **Ownership-proof restart on detector-box geometry (class b)** | **BUILD — this is the ship fix** | **−5.2 of 18 (−29 %)** | none measured: that stratum has 0 EARLY |
| 2 | Animation anchor / late-onset discrimination (class c) | Build after ship (already green-lit) | −2.9 of 18 (−16 %), unproven | medium: needs the pose lane |
| 3 | Global lead shift of +3/+6/+9 ms (A) | **DO NOT SHIP** | −0.8 / −1.6 / −2.3 LATE but **+0.8 / +1.7 / +2.7 EARLY** | net ≈ 0 at −3 ms, net **negative** past −6 ms |
| 4 | Standstill FadePhaseCatchup equivalent (C) | **DO NOT BUILD** | 0 | the premise (a discrete progression jump) does not exist in the frame data |
| 5 | Hold-law cap at 690 / 700 / 710 ms (D) | **DO NOT BUILD** | −7 / −6 / −5 of 18 | breaks **14 / 7 / 6** EXCELLENTs on the same slice and guts slow-tempo shots (cut by up to 400 ms) |

### The one-paragraph version

The 18 open-standstill LATEs are not one population. **Six of them (33 %) are a mechanical
pickup stall that the engine already logs and names**: the detector box changes shape while the
ownership-proof chain is being built, the chain restarts 4–7 times (`break_geometry` 3–6), the
first *accepted* meter sample arrives at fill 34–40 % instead of 17 %, about 120–160 ms after
`BOX LATCHED`, and the engine then fires with a negative `command_eta_ms` — its own
`TIP DEADLINE DECISION: disposition=fired_late`. That stratum is **66.7 % LATE (6/9)** against
**8.3 % (6/72)** for normal-pickup shots, and it contains **zero** EARLYs. Four more (class c)
fired against a meter drawn 60–130 ms late, where we cannot tell a late *render* from a
genuinely later *animation* — that is what the animation anchor is for. The remaining six are
indistinguishable from EXCELLENTs on every instrument we have: same `fill_at_rel` (37.9 % vs
37.6 %, AUC 0.51), same `hold − onset` (133.9 vs 132.4 ms, AUC 0.55), same lead. **Do not spend
the ship on those six.** Fix the pickup stall; leave the lead alone.

---

## 2. What the data does and does not say

### 2.1 The oracle gap is an oracle, not a ruler — do not convert it to ms

The brief asked to use `oracle.gap_px` as the landing ruler. It separates verdicts almost
perfectly and it is worth keeping for grading, but **it has no ms scale**, and this report does
not give it one:

* Separation (standstill, since 09-17T10:00Z): `gap > 2.5 px` catches **64/73 LATEs** and
  misflags **6/257 EXCELLENTs**. Sept-18 OPEN: EXC ∈ [−3, +2] px (n = 76), LATE ∈ [+1, +12] px
  (n = 17).
* But **inside** the EXCELLENT class the gap does not move with our command at all:
  `gap_px ~ resid(hold|onset)` slope **+0.0047 px/ms, r = +0.048, n = 238**. Pooled it is
  +0.057 px/ms (17.4 ms/px) — **26 % of the physical meter rate** — and that pooled slope is
  driven entirely by the class separation, not by a within-class response.
* `settled_fill` regresses on the command residual at **−0.148 pp/ms** — the *wrong sign* for a
  meter position (the meter rises at +0.19 pp/ms). It is a post-shot **retraction**: the game
  retracts further for a worse shot. EARLY shots also sit at large positive gaps (median +5 px),
  so the gap is a |miss| magnitude, not a signed early/late axis.

Conclusion: `gap_px` is a *verdict proxy*, which is what the 27/27 and 35/36 historical
validations actually established. Converting px → ms through the meter rate would have produced
a fabricated ms-per-px budget.

**Physical scale, for the record** (from the 60 fps trace, 17 rises): the meter runs at
0.160 pp/ms at 20–30 % fill, 0.192 at 30–50 %, 0.218–0.227 at 60–80 %, 0.191–0.216 at 80–90 %.
The oracle's own scale is **0.935 pp per oracle px** (median of `gap_pct / gap_px`, n = 318), so
one oracle pixel is **4.1–4.9 ms of meter travel** near the landing zone (`bbox_h` median 120 px
⇒ 1.2 reader px per pp; the oracle measures on a tighter box). Use ≈ **4.5 ms/px ± 0.5** *only*
for intuition about meter geometry — the regressions above show the landing does not respond to
our command at that rate.

### 2.2 The valid ms ruler is the engine's own `lateness_ms`

There is one instrument with a real, causal ms scale: `TIP DEADLINE DECISION` reports
`lateness_ms`, how far past the commit deadline the first usable sample arrived. Twelve such
events exist across both logs:

```
lateness_ms:  7.3 EXC | 8.8 LATE | 14.9 EXC | 25.0 LATE | 27.5 LATE | 29.8 LATE
             30.5 EXC | 33.7 LATE | 34.1 LATE | 37.3 LATE | 42.4 LATE | 44.5 LATE
```

Below ~15 ms late the shot still grades EXCELLENT; above ~25 ms it is LATE 8 times out of 9.
**That puts the usable late margin at roughly 20 ms**, and it is the only number here derived
from a direct cause→verdict pairing rather than a correlation.

### 2.3 What does NOT distinguish a LATE from an EXCELLENT

Sept-18 OPEN standstills, LATE (n = 18) vs EXCELLENT (n = 76), AUC:

| variable | LATE med | EXC med | AUC |
|---|---|---|---|
| `fill_at_rel` (meter fill at the command) | 37.9 % | 37.6 % | **0.508** |
| `hold − onset` | 133.9 ms | 132.4 ms | **0.553** |
| residual hold vs the EXC hold law | +3.3 ms | +2.4 ms | 0.593 |
| `fire_centre_delta_ms` | 0.06 | 0.12 | 0.379 |
| `predictor_sigma_ms` | 14.04 | 14.04 | 0.476 |
| distance (ft) | 25.2 | 25.1 | 0.514 |
| absolute `hold` | 660.4 ms | 648.2 ms | 0.675 |
| engine first-sight ms | 603.7 ms | 542.8 ms | 0.683 |
| **`command_eta_ms` < 0** | — | — | **P(LATE) 66.7 % vs 14.3 %** |
| **latch → first reservation ≥ 100 ms** | — | — | **P(LATE) 80 % vs 18.9 %** |

The engine fires at a *fixed meter phase by design* — `fill_at_rel` p10/p50/p90 = 36.7 / 37.6 /
40.7 % for EXCELLENT and 35.4 / 37.9 / 43.8 % for LATE. That is why `hold − onset` is
uninformative: it is dominated by noise in `onset`, not by where on the meter we fired.
Calibrated against the physically observed fill, `hold − onset` has a reliability of only
**0.07** and `resid(hold|onset)` **0.15** — i.e. 85–93 % of their variance is measurement noise.

There is also no absolute hold law. Within onset 510–530 ms, EXC holds are 639.8 / 648.5 / 657.7
(p10/50/90, n = 98) and LATE holds are 642.7 / 652.2 / 659.4 (n = 20) — a 3.7 ms median
difference. And EXCELLENTs exist at holds of 806–842 ms (onsets 670–723 ms), and one at
1 103.8 ms.

---

## 3. Sub-population decomposition

Class definitions used in the tables (data-driven, tightened from the first pass):

* **b late-pickup** — `TIP DEADLINE DECISION: fired_late`, OR ownership-census
  `first_fill ≥ 32 %`, OR `BOX LATCHED` → first reservation ≥ 60 ms, OR
  `source=phase_firstsight`, OR engine first-sight fill ≥ 35 %.
* **c late-onset** — sidecar `onset_ms ≥ 565 ms` (EXC p50 = 518.7) and not class b.
* **a marginal** — everything else with a plausible onset and gap ≤ 6 px.
* **other** — ghost/zero onsets, quick-tempo pickups with no clean meter start.

| scope | class | n | LATE | P(LATE) | EXC | EARLY |
|---|---|---|---|---|---|---|
| Sept-18 OPEN | a marginal | 72 | 6 | **8.3 %** | 64 | 2 |
| | b late-pickup | 9 | 6 | **66.7 %** | 3 | 0 |
| | c late-onset | 13 | 4 | 30.8 % | 9 | 0 |
| | other | 2 | 2 | — | 0 | 0 |
| Sept-18, all coverage | a marginal | 137 | 15 | 10.9 % | 115 | 7 |
| | b late-pickup | 17 | 11 | 64.7 % | 5 | 1 |
| | c late-onset | 32 | 15 | 46.9 % | 14 | 3 |
| | other | 14 | 5 | 35.7 % | 5 | 4 |
| Since 09-17T10:00Z | a marginal | 275 | 29 | 10.5 % | 221 | 25 |
| | b late-pickup | 17 | 11 | 64.7 % | 5 | 1 |
| | c late-onset | 59 | 25 | 42.4 % | 29 | 5 |
| | other | 50 | 15 | 30.0 % | 21 | 14 |

### 3.1 Class b — root cause, named by the engine's own instruments

Two traces, same session, six minutes apart (ms after the physical press):

```
CONTROL  (epoch 74, WIDE OPEN, EXCELLENT)
  +536  BOX LATCHED: box=[942,313,26,112]
  +561  TIP RESERVATION created  fill_pct=22.33  command_eta_ms=+90.4
  +654  Release issued  fill 37.0%  code=live_meter_tip        -> EXCELLENT, gap 0

STALL    (epoch 72, WIDE OPEN, LATE)
  +557  BOX LATCHED: box=[831,318,26,110]
  +677  Ownership proof census: proof_samples=3 ... restarts=4 break_geometry=3
  +679  Ownership geometry break: old_box=813,318,44,118 new_box=830,319,26,118
        iou=0.583 width_scale=0.591
  +679  TIP RESERVATION created  fill_pct=43.68  command_eta_ms=-25.0
  +679  TIP DEADLINE DECISION: disposition=fired_late lateness_ms=25.0 tolerance_ms=24.0
  +680  Release issued  fill 43.7%  code=live_tip_fired_late   -> LATE, gap 8
```

The reader had the box at +557. The **engine** would not accept a proof sample for another
122 ms because the box width oscillated 44 px ↔ 26 px and restarted the 3-sample
ownership-proof chain. `break_geometry` accounts for **70 of the 73 recorded breaks** across 179
censused standstills (drop = 2, anchor = 1, gap / identity / estimator_ruler = 0).

Stratified on the census `first_fill` (fill at the first *accepted* proof frame):

| census `first_fill` | n | LATE | EXC | EARLY | P(LATE) |
|---|---|---|---|---|---|
| < 25 % (normal) | 163 | 34 | 116 | 13 | 20.9 % |
| 25–32 % | 5 | 0 | 4 | 1 | 0 % |
| **≥ 32 %** | **11** | **7** | **4** | **0** | **63.6 %** |

All eleven of the ≥ 32 % shots carry `restarts` 4–7 and `break_geometry` 3–6, with one exception
(23:33:21Z, `break_anchor = 1`). `ORION_OWNERSHIP_PROOF_LENIENCY` is already implemented and
defaults TRUE (`native_orion/src/AutomationEngine.cpp:1758-1779`,
`native_orion/src/AppConfig.h:1362`), and the 2-frame ownership-proof variant is built but
flagged off — neither prevented these. `max_proof_samples` was 3 on every censused shot.

The second stall trace (epoch 206, 09-18T04:06:15Z) shows the downstream symptom explicitly:
`PHASE FIRST-SIGHT ANCHOR: first_fill=40.36 missed_rungs=5` followed by
`TIP DEADLINE DECISION: disposition=fired_late lateness_ms=42.374` — five rungs of meter were
drawn, seen by the reader, and never reached the predictor.

### 3.2 Class c — honest limits

Class c is "the meter was drawn ≥ 565 ms after the press". The engine follows it at the same
meter phase and still grades LATE 31–47 % of the time. The frame dump (§4) shows one such case
at frame resolution: the meter genuinely appeared 50 ms late and rose at a normal rate. We
**cannot** tell from the meter alone whether the animation itself started later (in which case
following the meter is correct — and 9 of the 13 open class-c shots *were* EXCELLENT) or the
meter was rendered late for an unchanged animation. That is exactly the discrimination the
animation-anchor v2 plan exists to supply.

---

## 4. The 60 fps dump: `session_20260917_142445`

**Log window.** `t_wall` is UTC Unix seconds: the trace spans
**2026-09-17T19:26:26.605Z → 19:27:30.449Z** (63.84 s, 2 939 rows, frame dt median 16.885 ms,
p95 25.1 ms). **`logs/orion_native.log.1` starts at 2026-09-18T00:14Z, so no engine log covers
this dump** — the rotated Sept-17 log is gone. The verdicts therefore come from
`D:\NexusVision\shot_records\session_20260917_142445.jsonl` (18 records, 16 with a banner),
joined by `press_ts_ms` → first detected frame. 19 detected runs; 16 rises have a verdict. None
of these shots carried a coverage class (the banner coverage OCR was not yet emitting).

**Standstill rises with a verdict: 7 EXCELLENT, 1 LATE (epoch 7, gap 12 px).** Fades:
6 EXCELLENT, 1 LATE.

Interpolated crossing intervals (ms):

```
 ep type         verdict  gap   press->20%   20-40   40-60   60-80   80-90   20-80
  2 Standstill   EXC     -1.0      531.6     115.0    98.1    87.9    46.5   301.0
  3 Standstill   EXC      0.0      518.6     116.0   118.3    84.4    53.0   318.6
  4 Standstill   EXC     -1.0      535.8     115.1    99.7    86.9      -    301.8
  5 Standstill   EXC      2.0      899.0     113.7    98.0    88.6   183.9   300.3   (slow tempo)
  6 Standstill   EXC     -2.0      531.9     114.0    98.6    89.2      -    301.8
  7 Standstill   LATE    12.0      582.1     114.3    96.4    92.3      -    302.9   <-- the LATE
  8 Standstill   EXC     -3.0      531.2     111.6    97.8    86.1    49.3   295.5
  9 Standstill   EXC     -1.0      532.2     115.8    97.8    88.3    49.5   301.9
 16 Left Fade    LATE     8.0      788.8     106.1    75.9    94.0    49.1   276.0
```

**Per-rise answer for every standstill rise with a verdict:**

* **(i) discrete forward jump — NO, 0 of 16 rises.** Running the `FadePhaseCatchup.h` criterion
  against an empirical reference curve built from the EXCELLENT rises (consecutive frames,
  fill ≤ 40 %, geometry-continuity gate applied, 0.65–1.35 × period dt gate applied): the
  **maximum** excess progression per rise is 1.4–6.0 ms (EXCELLENT median 3.5 ms; the two LATEs
  are 3.6 and 2.5 ms). The criterion demands 1.25–2.5 frame periods = **20.8–41.7 ms**. No rise
  reaches a third of the threshold. Caveat: `frames.csv` exposes a single pixel ruler, so the
  two-ruler agreement clause could not be tested — but that clause only makes firing *harder*.
* **(ii) faster steady rate — NO.** The LATE standstill's 20→80 % interval is 302.9 ms against an
  EXCELLENT standstill range of 295.5–318.6 (median 301.8). Band by band it sits inside the
  EXCELLENT spread. The LATE fade (ep 16, 276.0 ms) is faster, but that is one fade rise and
  fades are a different animation.
* **(iii) normal rate, the judge moved — YES, the only supported reading.** Epoch 7's meter was
  **drawn 50 ms late** (press → 20 % = 582.1 ms vs an EXCELLENT cluster of 518.6–535.8, median
  531.7), rose at a normal rate, and the engine followed it: hold 695.2 vs an EXCELLENT median
  of 647 (+48 ms). Same meter-relative landing, LATE verdict. Note the counter-example in the
  same dump: epoch 5's meter was drawn **367 ms** late (press → 20 % = 899.0) with the engine
  following (hold 1 013.1), and it graded **EXCELLENT** — so a late onset is not by itself
  fatal, which is precisely why a hold cap is wrong (§5 D).

**Aggregate: 0 / 16 rises support (i); 0 / 8 standstill rises support (ii); 1 / 1 standstill
LATE supports (iii).** The LATE arm of this dump is **one** standstill rise: it can refute the
catch-up hypotheses but cannot establish the (iii) mechanism on its own.

---

## 5. Counterfactuals

### (A) Global lead shift on open standstills — **reject**

Method: within a tight onset band (500–535 ms, n = 202, which removes most of the onset noise),
build Gaussian-kernel (bw = 9 ms) estimates of P(LATE | hold) and P(EARLY | hold), then shift
every hold by −Δ. A uniform shift is *not* subject to the regression attenuation that flattens
the per-shot residual, so this is the right estimator.

```
 hold   P(LATE)  P(EARLY)      shift        LATE    EARLY   net good shots (of 202)
 630      7.5%     15.8%       baseline    13.7%     7.0%        -
 640      9.7%      8.8%       -3 ms       12.4%     8.2%       +0.2
 650     14.5%      4.2%       -6 ms       11.1%     9.8%       -0.4
 660     20.5%      3.2%       -9 ms       10.0%    11.5%       -1.7
 670     26.7%      3.9%      -12 ms        9.1%    13.4%       -3.5
```

The LATE and EARLY curves cross at hold ≈ 641 ms; the population median is 646.4 ms. **The
optimum shift is +2 ms earlier and it is worth 0.1 pp** (20.7 % → 20.6 % bad). Every LATE
converted costs roughly one EARLY. Scaled to the 18 Sept-18 OPEN LATEs: −3 ms ≈ −0.8 LATE /
+0.8 EARLY, −6 ms ≈ −1.6 / +1.7, −9 ms ≈ −2.3 / +2.7.

*Caveat, stated plainly:* restricted to the OPEN class alone the optimum looks like +18 ms
(bad 13.9 % → 9.8 %), because open standstills contain only **5 EARLYs in 148 shots** against
23 LATEs — the EARLY curve is unestimable there. That asymmetry is real and interesting (a wide
window forgives an early miss while the window top is invariant), but it is **not** evidence
that 9–18 ms of lead is free: 3.4 % of one class is far too thin to bet the ship on, the engine
cannot know coverage before the press, and the all-standstill estimate says the lead is already
sitting on its optimum. Measured leads were 274–298 ms with `banner_trim_ms` only ever
0.0 / 0.8 / 1.5 / 6.0 and `lead_offset_ms` 0.0 throughout, so the fleet was effectively at one
lead the whole time.

### (B) Earlier pickup for class b — **build, best value in the set**

Nine of the 96 Sept-18 OPEN standstills are class b; six are LATE (66.7 %). The normal-pickup
baseline on the same slice is 8.3 % (class a). If the stall were removed and those nine behaved
like class a:

```
 expected LATE in class b: 9 x 0.083 = 0.8   (observed 6)
 CONVERTS 5.2 of the 18 open LATEs  =  -29 %      18 -> ~12.8
 EARLY cost: 0 observed EARLYs in the class-b stratum (n = 17 across all coverage)
```

Across all coverage on Sept 18 the class is 17 shots, 11 LATE — converting ≈ 9.5 of 46. The
precondition "first sight at ≤ 25 % fill" is exactly what the stratification tests: shots whose
first accepted proof frame is < 25 % fill run at 20.9 % LATE, i.e. the ordinary rate. This is a
*ceiling*: it assumes the stall is fully removable. The mechanism — `break_geometry` restarts of
the 3-sample ownership-proof chain — is a software condition on our side of the wire, not a
vision limit, so the ceiling is plausible, but only a live A/B can confirm it. n = 9 on the
open slice.

### (C) A standstill FadePhaseCatchup — **do not build**

Condition (i) is not met anywhere in the dump (§4): max excess 1.4–6.0 ms against a 20.8–41.7 ms
threshold. The note that FadePhaseCatchup never fired in any Sept-18 session is consistent —
there is nothing for it to fire on. Building the standstill twin would add a correction path
with no evidence behind it, and the helper is explicitly designed to *ignore* gradual drift,
which is the only thing the traces actually show.

### (D) Hold-law cap — **do not build**

| cap | slice | LATEs above cap | EXCELLENTs above cap |
|---|---|---|---|
| 690 ms | Sept-18 OPEN | 7 of 18 | **14 of 76** |
| 700 ms | Sept-18 OPEN | 6 of 18 | **7 of 76** |
| 710 ms | Sept-18 OPEN | 5 of 18 | **6 of 76** |
| 690 ms | since 09-17T10:00Z | 38 of 80 | 48 of 276 |
| 700 ms | since 09-17T10:00Z | 30 of 80 | 31 of 276 |
| 710 ms | since 09-17T10:00Z | 28 of 80 | 24 of 276 |

Even at the most favourable cap (700 ms on the open slice) the trade is 6 LATEs for 7
EXCELLENTs — worse than a coin flip — and that understates the damage: the 31 EXCELLENTs above
700 ms would have been cut short by 2 ms to **403.8 ms** (median 66.5 ms). Those are slow-tempo and
slow-gather shots whose whole animation ran late; firing them at 700 ms would fire before the
meter existed. Epoch 5 of the Sept-17 dump (hold 1 013.1 ms, EXCELLENT, gap +2) is the canonical
counter-example.

---

## 6. Per-shot tables

Columns: `ofil` = sidecar onset fill %; `fs_ms` / `fsfil` = engine first reservation time after
press (frame-age corrected) and its fill %; `pxf` = fill at the first *accepted* ownership-proof
frame (`Ownership proof census: first_fill`); `l2r` = `BOX LATCHED` → first reservation, ms;
`src` = tip source on the last reservation carrying one; `code` = `Release timing` code;
`lead` / `trim` = `lead_ms` and `banner_trim_ms` in effect at that reservation (there is no
`Shot lead set to` line in these builds — the lead in effect is only observable on the
`TIP RESERVATION` line); `gap` = `oracle.gap_px`; `f@rel` = fill at the release command; `fcd` =
`fire_centre_delta_ms` (−99 = `fire_target` was `instant` / `none`, i.e. not a frame-centre
fire); `deadline/late` = `TIP DEADLINE DECISION` disposition and `lateness_ms`.

Dashes in the Sept-17 rows are structural: no engine log survives before 2026-09-18T00:14Z, so
those shots carry sidecar-only fields.

```
#### TABLE 1 - every standstill LATE since 2026-09-17T10:00Z (n=80)
  ts                sess    ep  coverage      tmp  hold   onset ofil  fs_ms fsfil pxf  l2r  src            code             lead trim  gap f@rel  fcd    deadline/late  class
   09-17T10:03:29Z 050219  19            - norm  665.4  471.3   12      -     -    -    -              -                -    -    - 13.0     -     -          -/   0 other
   09-17T10:03:32Z 050219  20            - quic  670.7    6.6   81      -     -    -    -              -                -    -    -    -     -     -          -/   0 other
   09-17T10:03:35Z 050219  21            - quic  664.9  436.4   11      -     -    -    -              -                -    -    -    -     -     -          -/   0 a marginal
   09-17T10:03:45Z 050219  24            - quic  681.1  456.0    0      -     -    -    -              -                -    -    -    -     -     -          -/   0 a marginal
   09-17T11:14:39Z 061411   3            - norm  656.7      -    -      -     -    -    -              -                -    -    -    -     -     -          -/   0 other
   09-17T11:15:37Z 061411  18            - quic  610.2  426.1   18      -     -    -    -              -                -    -    -    -     -     -          -/   0 a marginal
   09-17T19:26:44Z 142445   7            - slow  695.2  563.6   17      -     -    -    -              -                -    -    - 12.0     -     -          -/   0 other
   09-17T19:34:48Z 143315   7            - norm  656.2  518.1   17      -     -    -    -              -                -    -    -  7.0     -     -          -/   0 other
   09-17T19:43:42Z 143315  32 LIGHT CONTEST norm  649.6  528.5   18      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T19:45:23Z 143315  37 LIGHT CONTEST slow  695.6  559.8   17      -     -    -    -              -                -    -    - 12.0     -     -          -/   0 other
   09-17T19:53:52Z 143315  54    SEMI-OPEN norm  650.8  516.8   17      -     -    -    -              -                -    -    - 15.0     -     -          -/   0 other
   09-17T19:55:06Z 143315  57         OPEN slow  694.6  560.1   17      -     -    -    -              -                -    -    -  4.0     -     -          -/   0 a marginal
   09-17T19:57:26Z 143315  64 LIGHT CONTEST slow  803.9  666.5   17      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 c late-onset
   09-17T20:01:57Z 143315  69     BOTHERED norm  654.4  517.9   17      -     -    -    -              -                -    -    -  4.0     -     -          -/   0 a marginal
   09-17T20:20:25Z 143315 110    SEMI-OPEN norm  657.0  526.7   18      -     -    -    -              -                -    -    -  3.0     -     -          -/   0 a marginal
   09-17T20:28:39Z 143315 141 SOLID CONTEST norm  653.9  542.2   21      -     -    -    -              -                -    -    -  4.0     -     -          -/   0 a marginal
   09-17T20:45:48Z 143315 159 LIGHT CONTEST slow  726.7  595.9   18      -     -    -    -              -                -    -    - 17.0     -     -          -/   0 c late-onset
   09-17T20:48:28Z 143315 171    SEMI-OPEN slow  805.0  690.3   19      -     -    -    -              -                -    -    -  9.0     -     -          -/   0 c late-onset
   09-17T20:59:15Z 143315 190 LIGHT CONTEST norm  666.3  532.5   17      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T21:02:49Z 143315 206     BOTHERED norm  648.5  513.6   17      -     -    -    -              -                -    -    -  5.0     -     -          -/   0 a marginal
   09-17T21:03:36Z 143315 212 LIGHT CONTEST quic  655.5  462.5   19      -     -    -    -              -                -    -    -  5.0     -     -          -/   0 a marginal
   09-17T21:57:29Z 165232   9 LIGHT CONTEST slow  697.7  559.8   17      -     -    -    -              -                -    -    -  3.0     -     -          -/   0 a marginal
   09-17T22:25:16Z 165232  67     BOTHERED slow  998.1  865.0   17      -     -    -    -              -                -    -    -  5.0     -     -          -/   0 c late-onset
   09-17T22:29:59Z 165232  75    WIDE OPEN slow  736.7  602.0   18      -     -    -    -              -                -    -    -  4.0     -     -          -/   0 c late-onset
   09-17T22:32:58Z 165232  82    WIDE OPEN norm  645.1  505.7   17      -     -    -    -              -                -    -    -  3.0     -     -          -/   0 a marginal
   09-17T22:56:11Z 165232 144         OPEN slow  712.5  572.6   17      -     -    -    -              -                -    -    -  9.0     -     -          -/   0 c late-onset
   09-17T22:56:45Z 165232 147    SEMI-OPEN slow  762.1  607.4   15      -     -    -    -              -                -    -    - 12.0     -     -          -/   0 c late-onset
   09-17T22:57:57Z 165232 151 HEAVY CONTEST slow  757.1  612.8   14      -     -    -    -              -                -    -    -  7.0     -     -          -/   0 c late-onset
   09-17T22:58:09Z 165232 153    WIDE OPEN norm  657.6  516.7   14      -     -    -    -              -                -    -    -  1.0     -     -          -/   0 a marginal
   09-17T23:00:37Z 165232 178            - norm  632.1  521.4   20      -     -    -    -              -                -    -    - 14.0     -     -          -/   0 other
   09-17T23:36:18Z 183515  11            - slow  771.8  654.0   20      -     -    -    -              -                -    -    -  5.0     -     -          -/   0 c late-onset
   09-17T23:36:29Z 183515  15            - slow  684.8  568.7   19      -     -    -    -              -                -    -    -  6.0     -     -          -/   0 c late-onset
   09-17T23:37:03Z 183515  25            - norm  652.8  526.8   18      -     -    -    -              -                -    -    -  9.0     -     -          -/   0 other
   09-17T23:37:06Z 183515  26            - quic  681.1  393.9   16      -     -    -    -              -                -    -    -  5.0     -     -          -/   0 other
   09-18T00:11:02Z 190715   4            - slow  724.2  605.2   20      -     -    -    -              -                -    -    -  8.0     -     -          -/   0 c late-onset
   09-18T00:12:33Z 190715  28            - slow  693.2  548.7   18      -     -    -    -              -                -    -    - 16.0     -     -          -/   0 other
   09-18T00:12:41Z 190715  30            - slow  765.6  623.4   17      -     -    -    -              -                -    -    - 13.0     -     -          -/   0 c late-onset
   09-18T00:12:44Z 190715  31            - slow  777.7  651.6   19      -     -    -    -              -                -    -    - 14.0     -     -          -/   0 c late-onset
   09-18T00:13:11Z 190715  39            - quic  714.0  272.3   39      -     -    -    -              -                -    -    -  7.0     -     -          -/   0 other
   09-18T01:28:12Z 201500   3            - slow  766.9  629.4   17  689.4  27.4   17   42          phase   live_meter_tip  276  1.5 49.0  35.5  0.07          -/   0 c late-onset
   09-18T01:31:52Z 201500  13         OPEN norm  650.6  512.0   17  532.8  22.6   17    0          phase   live_meter_tip  274    -  2.0  39.1  0.22          -/   0 a marginal
   09-18T01:33:56Z 201500  22    WIDE OPEN slow  718.1  567.6   18  701.0  45.0   38  118          phase   live_meter_tip  286  0.0  3.0  39.3  0.23 fired_late/  44 b late-pickup
   09-18T01:40:01Z 201500  63    SEMI-OPEN quic  745.3  206.8   11  710.0  40.3   11  496 phase_firstsight   live_meter_tip  279  0.0  5.0  37.6  0.14 fired_late/  30 b late-pickup
   09-18T01:42:46Z 201500  70    WIDE OPEN norm  649.9  541.2   20  558.7  25.2   20    3          phase   live_meter_tip  279    -  3.0  38.1  0.19          -/   0 a marginal
   09-18T02:52:32Z 215034   6     BOTHERED norm  651.7  527.8   17  539.6  22.7   17  -21          phase   live_meter_tip  279    -  5.0  35.5  0.07          -/   0 a marginal
   09-18T03:00:21Z 215034  28     BOTHERED norm  643.1  518.9   17  535.5  22.4   17   -1          phase   live_meter_tip  284    -  5.0  38.4 -0.01          -/   0 a marginal
   09-18T03:07:37Z 215034  55    SEMI-OPEN quic  705.4  235.5   17  373.5  16.7    8  122          phase   live_meter_tip  284  0.0    -  40.9  0.20          -/   0 b late-pickup
   09-18T03:12:42Z 215034  68    WIDE OPEN slow  749.4  628.5   18  647.5  23.1   18    3          phase live_tip_fired_l  286    -  9.5  44.1 -99.00          -/   0 c late-onset
   09-18T03:13:43Z 215034  72    WIDE OPEN slow  697.6  570.6   17  593.2  22.6   17    3          phase   live_meter_tip  286    -  5.0  32.5  0.06          -/   0 c late-onset
   09-18T03:14:36Z 215034  80 SOLID CONTEST norm  639.4  513.5   17  537.6  22.6   18    6          phase   live_meter_tip  286    -  5.5  38.1  0.19          -/   0 a marginal
   09-18T03:54:09Z 224059 182    SEMI-OPEN slow  698.9  563.4   17  583.2  23.0   17    2          phase   live_meter_tip  274    -  5.0  40.8  0.09          -/   0 a marginal
   09-18T04:05:45Z 224059 204    SEMI-OPEN slow  800.6  671.6   17  684.0  22.3   17   -6          phase   live_meter_tip  274    -  6.0  39.1  0.22          -/   0 c late-onset
   09-18T04:06:15Z 224059 206    WIDE OPEN norm  700.2  522.7   17  674.2  46.9   39  132 phase_firstsight   live_meter_tip  274  0.0 12.0  36.2  0.15 fired_late/  42 b late-pickup
   09-18T04:12:37Z 224059 223 LIGHT CONTEST norm  671.2  534.7   17  559.8  22.4   17    8          phase   live_meter_tip  274    - -1.0  39.3  0.23          -/   0 a marginal
   09-18T04:22:32Z 224059 256         OPEN quic  662.2  265.3   19  623.8  40.6   10  296 phase_firstsight live_tip_fired_l  279  0.0    -  44.1 -99.00 fired_late/  34 b late-pickup
   09-18T04:29:19Z 224059 276    WIDE OPEN slow  714.8  592.5   18  614.2  23.4   17    4          phase   live_meter_tip  274    -  8.0  38.1  0.19          -/   0 c late-onset
   09-18T04:30:51Z 224059 286 LIGHT CONTEST slow  713.5  559.3   18  696.7  43.9   38  119          phase live_tip_fired_l  286  0.0  4.0  43.9 -99.00 fired_late/  27 b late-pickup
   09-18T04:37:48Z 224059 296    WIDE OPEN norm  653.9  500.6   17  630.5  43.3   38  118          phase live_tip_fired_l  286  0.0  4.0  43.3 -99.00 fired_late/  37 b late-pickup
   09-18T04:39:27Z 224059 299    SEMI-OPEN slow  770.9  654.5   17  673.1  22.5   17    4          phase   live_meter_tip  289    - 11.0  45.8 -99.00          -/   0 c late-onset
   09-18T04:42:21Z 224059 306    WIDE OPEN slow  710.8  573.0   18  688.3  41.0   38  100          phase live_tip_fired_l  292  6.0  2.0  41.0 -99.00 fired_late/  34 b late-pickup
   09-18T04:42:48Z 224059 308    SEMI-OPEN slow  768.6  654.2   18  676.0  23.0   17    2          phase   live_meter_tip  295    - 16.0  34.9  0.07          -/   0 c late-onset
   09-18T04:52:58Z 224059 328    WIDE OPEN norm  633.4  540.8   22  565.1  28.0   22    8          phase   live_meter_tip  286    -  4.0  37.2  0.08          -/   0 a marginal
   09-18T04:53:26Z 224059 332    WIDE OPEN norm  634.1  507.0   17  529.6  22.4   17    2          phase   live_meter_tip  286    -  9.0  37.3  0.06          -/   0 other
   09-18T18:28:48Z 040815   6            - norm  638.3  500.2   17  541.0  25.4   20   23          phase   live_meter_tip  274    -  4.0  40.6  0.18          -/   0 a marginal
   09-18T18:28:52Z 040815   7            - norm  662.5  526.3   17  545.0  22.8   17    0          phase   live_meter_tip  274    -  2.0  40.8  0.09          -/   0 a marginal
   09-18T22:45:36Z 174450  33            - quic  687.3    1.5   90  547.3  19.0   15  315          phase   live_meter_tip  275  0.8  5.0  37.0  0.06          -/   0 b late-pickup
   09-18T22:45:48Z 174450  37            - norm  659.0  521.0   17  543.9  22.7   17    4          phase   live_meter_tip  275    -  4.0  40.7  0.14          -/   0 a marginal
   09-18T22:57:31Z 174450  62    SEMI-OPEN slow  748.6  618.9   17  636.4  22.7   17    2          phase   live_meter_tip  274    -  4.0  37.6  0.05          -/   0 c late-onset
   09-18T23:00:52Z 174450  72    WIDE OPEN norm  679.7  534.1   20  655.4  43.7   37   98          phase live_tip_fired_l  274  0.0  8.0  43.7 -99.00 fired_late/  25 b late-pickup
   09-18T23:14:34Z 174450 104     BOTHERED norm  658.3  528.3   17  543.0  22.2   17   -6          phase   live_meter_tip  274    -  3.0  37.0 -0.01          -/   0 a marginal
   09-18T23:18:33Z 174450 112     BOTHERED slow  694.9  572.0   18  584.1  24.9   18   -2          phase   live_meter_tip  274    - -1.0  38.1 -99.00          -/   0 c late-onset
   09-18T23:31:39Z 174450 135    SEMI-OPEN norm  653.4  518.6   17  536.9  22.8   17    1          phase   live_meter_tip  274    - 11.0  40.6  0.14          -/   0 other
   09-18T23:32:15Z 174450 139         OPEN norm  652.6  534.0   20  551.5  25.4   20   -1          phase   live_meter_tip  274  0.0  3.0  37.4  0.05          -/   0 a marginal
   09-18T23:33:21Z 174450 141 LIGHT CONTEST slow  863.2  751.6   39  836.0  41.4   35  -37          phase live_tip_fired_l  274  0.0  5.0  41.4 -99.00 fired_late/   9 b late-pickup
   09-18T23:34:55Z 174450 147         OPEN norm  647.7  512.3   17  519.6  20.2   17  -12          phase   live_meter_tip  277    - 10.0  37.6  0.12          -/   0 other
   09-18T23:38:16Z 174450 152    WIDE OPEN slow  741.8  628.1   18  646.7  23.7   18    2          phase   live_meter_tip  274    -  8.0  35.5  0.08          -/   0 c late-onset
   09-18T23:48:45Z 174450 172    SEMI-OPEN slow  800.5  673.2   18  696.3  22.5   17    8          phase   live_meter_tip  274    - 12.0  45.9 -99.00          -/   0 c late-onset
   09-19T00:30:57Z 192239  18         OPEN norm  649.0  516.7   17  534.1  22.9   19   -1          phase   live_meter_tip  276    -  3.0  35.0 -0.07          -/   0 a marginal
   09-19T02:41:16Z 213313   5     BOTHERED slow  902.8  750.9   14  788.8  23.1   18    1          phase   live_meter_tip  274    - 15.0  37.8  0.38          -/   0 c late-onset
   09-19T02:47:16Z 213313  35         OPEN norm  658.5  500.0   15  544.3  22.1   18   -5          phase   live_meter_tip  276  1.5  5.0  36.7 -99.00          -/   0 a marginal

#### TABLE 2 - matched EXCELLENT standstills (same session, nearest onset; n=79)
  ts                sess    ep  coverage      tmp  hold   onset ofil  fs_ms fsfil pxf  l2r  src            code             lead trim  gap f@rel  fcd    deadline/late  class
   09-17T10:02:44Z 050219   4            - quic  646.8    4.0    0      -     -    -    -              -                -    -    -  0.0     -     -          -/   0 other
   09-17T10:02:50Z 050219   6            - quic  616.4  437.3   16      -     -    -    -              -                -    -    -    -     -     -          -/   0 a marginal
   09-17T10:03:09Z 050219  12            - quic  662.3  457.8    0      -     -    -    -              -                -    -    -    -     -     -          -/   0 a marginal
   09-17T10:03:54Z 050219  27            - norm  634.5  464.7    0      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T11:15:43Z 061411  20            - norm  671.3  534.4   18      -     -    -    -              -                -    -    -  1.0     -     -          -/   0 a marginal
   09-17T19:26:35Z 142445   4            - norm  651.2  519.4   17      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T19:34:36Z 143315   4            - slow  687.3  567.6   18      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 c late-onset
   09-17T19:34:44Z 143315   6            - norm  672.2  534.1   17      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T19:42:25Z 143315  23            - norm  654.1  518.1   17      -     -    -    -              -                -    -    -  0.0     -     -          -/   0 a marginal
   09-17T19:52:29Z 143315  51            - norm  661.8  525.6   17      -     -    -    -              -                -    -    -  0.5     -     -          -/   0 a marginal
   09-17T20:02:10Z 143315  72    WIDE OPEN norm  649.0  513.6   17      -     -    -    -              -                -    -    - -2.5     -     -          -/   0 a marginal
   09-17T20:02:35Z 143315  76    WIDE OPEN norm  642.7  525.2   20      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 a marginal
   09-17T20:06:36Z 143315  84    WIDE OPEN norm  605.8  467.8   18      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T20:07:40Z 143315  87    SEMI-OPEN slow  697.7  561.9   17      -     -    -    -              -                -    -    - -3.0     -     -          -/   0 a marginal
   09-17T20:23:01Z 143315 125    WIDE OPEN slow  738.2  604.3   18      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 c late-onset
   09-17T20:29:15Z 143315 144     BOTHERED slow  692.9  563.0   18      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 a marginal
   09-17T20:45:28Z 143315 154    WIDE OPEN norm  655.6  525.9   18      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 a marginal
   09-17T20:45:36Z 143315 155    WIDE OPEN slow  937.6  806.9   18      -     -    -    -              -                -    -    -  0.0     -     -          -/   0 c late-onset
   09-17T20:47:37Z 143315 170    WIDE OPEN norm  646.4  518.6   17      -     -    -    -              -                -    -    -  0.0     -     -          -/   0 a marginal
   09-17T21:03:17Z 143315 210         OPEN norm  647.9  517.1   17      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 a marginal
   09-17T22:15:03Z 165232  46    WIDE OPEN norm  643.9  505.7   17      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 a marginal
   09-17T22:16:24Z 165232  48    WIDE OPEN slow  705.1  566.4   17      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 c late-onset
   09-17T22:26:53Z 165232  69            - slow  912.7  772.8   16      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 c late-onset
   09-17T22:30:03Z 165232  76    SEMI-OPEN slow  735.7  599.9   17      -     -    -    -              -                -    -    -  1.5     -     -          -/   0 c late-onset
   09-17T22:33:45Z 165232  84    WIDE OPEN slow  683.3  569.3   20      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 c late-onset
   09-17T22:54:42Z 165232 143    WIDE OPEN norm  667.4  520.3   17      -     -    -    -              -                -    -    -  1.0     -     -          -/   0 a marginal
   09-17T23:00:27Z 165232 175            - slow  786.6  661.8   15      -     -    -    -              -                -    -    -  2.0     -     -          -/   0 c late-onset
   09-17T23:00:33Z 165232 177            - norm  637.2  516.6   17      -     -    -    -              -                -    -    -  3.0     -     -          -/   0 a marginal
   09-17T23:00:40Z 165232 179            - slow  683.5  558.9   17      -     -    -    -              -                -    -    -  2.0     -     -          -/   0 a marginal
   09-17T23:35:55Z 183515   5            - norm  659.7  533.3   17      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T23:36:32Z 183515  16            - norm  648.3  515.4   18      -     -    -    -              -                -    -    - -2.0     -     -          -/   0 a marginal
   09-17T23:36:36Z 183515  17            - norm  632.6  520.3   18      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-17T23:37:08Z 183515  27            - quic  633.7  345.1   16      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 other
   09-18T00:10:53Z 190715   2            - slow  669.9  563.9   18      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 a marginal
   09-18T00:11:07Z 190715   6            - slow  697.5  576.5   20      -     -    -    -              -                -    -    - -1.0     -     -          -/   0 c late-onset
   09-18T00:12:24Z 190715  25            - slow  691.0  548.2   17      -     -    -    -              -                -    -    - -3.0     -     -          -/   0 a marginal
   09-18T00:13:01Z 190715  36            - slow  707.4  564.2   18      -     -    -    -              -                -    -    - -3.0     -     -          -/   0 a marginal
   09-18T00:13:14Z 190715  40            - quic  654.8  361.9   42      -     -    -    -              -                -    -    - -7.0     -     -          -/   0 other
   09-18T01:28:09Z 201500   2            - slow  766.5  630.2   17  670.8  25.0   17   24          phase   live_meter_tip  276  1.5 -1.0  36.8  0.08          -/   0 c late-onset
   09-18T01:28:20Z 201500   6            - quic  653.0  217.0   40  537.9  22.4   18    6          phase   live_meter_tip  274    - -1.0  40.8  0.09          -/   0 other
   09-18T01:33:45Z 201500  21         OPEN norm  638.4  512.0   18  533.0  22.9   18    4          phase   live_meter_tip  286    - -2.0  40.9  0.20          -/   0 a marginal
   09-18T01:39:23Z 201500  59    WIDE OPEN norm  648.1  531.5   17  546.4  22.7   16    3          phase   live_meter_tip  286    -  0.0  37.5  0.14          -/   0 a marginal
   09-18T01:42:34Z 201500  69    WIDE OPEN slow  673.0  547.7   17  594.6  28.5   23   29          phase   live_meter_tip  279    - -2.0  37.6  0.03          -/   0 a marginal
   09-18T02:54:41Z 215034  14    WIDE OPEN slow  697.8  569.4   18  623.5  29.1   20   36          phase   live_meter_tip  284    - -1.0  40.7  0.18          -/   0 c late-onset
   09-18T02:55:04Z 215034  15    WIDE OPEN norm  645.6  518.4   17  538.0  23.1   18    3          phase   live_meter_tip  284    - -1.0  40.6  0.18          -/   0 a marginal
   09-18T03:04:56Z 215034  37    SEMI-OPEN norm  653.0  528.2   17  545.7  22.3   17    1          phase   live_meter_tip  284    - -1.0  39.1  0.22          -/   0 a marginal
   09-18T03:07:43Z 215034  57            - norm  642.9  498.5   17  535.5  22.3   17   20          phase   live_meter_tip  284  0.0 -3.0  39.3  0.23          -/   0 a marginal
   09-18T03:11:28Z 215034  67     BOTHERED slow  770.0  648.9   18  668.3  23.1   17    4          phase   live_meter_tip  286    -  2.0  37.6  0.14          -/   0 c late-onset
   09-18T03:14:24Z 215034  77    WIDE OPEN norm  639.8  513.4   17  551.5  25.3   17   19          phase   live_meter_tip  286    - -2.5  37.6  0.03          -/   0 a marginal
   09-18T03:52:39Z 224059 177         OPEN norm  640.2  503.0   17  543.1  25.2   17   20          phase   live_meter_tip  274    -  1.0  35.5  0.07          -/   0 a marginal
   09-18T03:52:47Z 224059 178    WIDE OPEN slow  699.1  561.6   17  601.2  25.2   17   20          phase   live_meter_tip  274    -  1.0  40.7  0.18          -/   0 a marginal
   09-18T03:54:28Z 224059 188    WIDE OPEN norm  694.0  540.2   15  593.7  22.6   19   38          phase   live_meter_tip  274    -  1.0  37.7  0.19          -/   0 a marginal
   09-18T04:04:48Z 224059 202    WIDE OPEN slow  731.7  617.0   19  631.6  25.0   20   -0          phase   live_meter_tip  274    - -1.0  38.4 -0.01          -/   0 c late-onset
   09-18T04:05:21Z 224059 203    WIDE OPEN slow  696.4  562.2   17  581.2  22.2   17   -4          phase   live_meter_tip  274    - -1.0  38.2  0.07          -/   0 a marginal
   09-18T04:12:16Z 224059 221    WIDE OPEN slow  747.8  615.2   17  633.7  22.7   18   -2          phase   live_meter_tip  277    -  0.0  40.7  0.30          -/   0 c late-onset
   09-18T04:13:26Z 224059 229    WIDE OPEN slow  717.0  583.4   17  604.6  22.6   18    2          phase   live_meter_tip  279    - -2.0  38.7  0.09          -/   0 c late-onset
   09-18T04:21:10Z 224059 249         OPEN norm  647.3  522.8   17  540.4  22.8   17    1          phase   live_meter_tip  279    -  0.0  37.5  0.19          -/   0 a marginal
   09-18T04:31:24Z 224059 287     BOTHERED norm  626.3  499.9   17  521.6  22.9   17    2          phase   live_meter_tip  286    -  1.0  37.5  0.04          -/   0 a marginal
   09-18T04:34:28Z 224059 293    WIDE OPEN norm  653.7  529.7   17  549.8  22.3   17   -1          phase   live_meter_tip  286    - -1.0  37.2 -0.03          -/   0 a marginal
   09-18T04:45:52Z 224059 318            - norm  634.1  507.4   17  529.5  22.4   17    7          phase   live_meter_tip  286    - -2.0  36.9  0.07          -/   0 a marginal
   09-18T04:53:04Z 224059 330    WIDE OPEN slow  836.7  722.9   18  730.9  20.3   18   -9          phase   live_meter_tip  298    - -3.0  34.7  0.07          -/   0 c late-onset
   09-18T04:55:17Z 224059 344 SOLID CONTEST slow  691.1  567.5   18  589.0  23.2   18    3          phase   live_meter_tip  289    -  2.0  38.2  0.06          -/   0 c late-onset
   09-18T18:28:46Z 040815   5            - norm  643.6  506.7   17  529.3  22.7   18    5          phase   live_meter_tip  274    -  1.0  40.7  0.18          -/   0 a marginal
   09-18T18:29:59Z 040815  26            - norm  662.5  527.1   18  547.6  23.1   18    4          phase   live_meter_tip  277    -  0.0  37.6 -99.00          -/   0 a marginal
   09-18T22:45:28Z 174450  31            - quic  704.0  168.2   36  589.5  22.6   18  287          phase   live_meter_tip  275    -  0.0  37.8  0.38          -/   0 b late-pickup
   09-18T22:51:07Z 174450  52    WIDE OPEN norm  674.5  536.1   17  557.9  22.9   19    2          phase   live_meter_tip  275    -  1.0  40.7  0.20          -/   0 a marginal
   09-18T22:54:45Z 174450  59         OPEN norm  642.8  526.6   20  542.8  25.4   20   -2          phase   live_meter_tip  275    -  1.0  37.5  0.05          -/   0 a marginal
   09-18T23:01:19Z 174450  75    WIDE OPEN slow  706.6  577.2   17  591.0  22.5   18    0          phase   live_meter_tip  275    - -1.0  37.2  0.12          -/   0 c late-onset
   09-18T23:07:40Z 174450  90    WIDE OPEN norm  656.9  518.5   17  541.6  22.1   17    7          phase   live_meter_tip  274    - -3.0  40.1  0.12          -/   0 a marginal
   09-18T23:08:11Z 174450  92    WIDE OPEN norm  656.0  527.6   17  540.4  22.2   18   -5          phase   live_meter_tip  274    - -2.0  40.2 -0.18          -/   0 a marginal
   09-18T23:27:15Z 174450 121         OPEN slow  803.0  688.6   19  702.5  24.1   19    0          phase   live_meter_tip  274    -  0.0  35.7 -0.00          -/   0 c late-onset
   09-18T23:33:55Z 174450 143     BOTHERED slow  821.7  701.3   19  722.2  24.1   19    5          phase   live_meter_tip  274    - -2.0  38.9  0.08          -/   0 c late-onset
   09-18T23:35:17Z 174450 150    WIDE OPEN norm  657.7  521.6   18  544.0  23.0   17    5          phase   live_meter_tip  277    - -2.0  38.0  0.13          -/   0 a marginal
   09-18T23:38:46Z 174450 154    WIDE OPEN slow  696.7  563.7   17  587.1  23.0   17    6          phase   live_meter_tip  277    - -2.0  37.5  0.05          -/   0 a marginal
   09-18T23:38:56Z 174450 156         OPEN norm  647.8  512.6   18  581.1  31.7   28   51          phase   live_meter_tip  274    -  0.0  37.7  0.02          -/   0 a marginal
   09-18T23:42:53Z 174450 165    WIDE OPEN slow  648.6  569.4   23  588.5  28.2   23    4          phase   live_meter_tip  277    -  0.0  34.1  0.01          -/   0 c late-onset
   09-19T00:26:07Z 192239   2    WIDE OPEN norm  647.3  510.4   17  557.3  25.6   17   27          phase   live_meter_tip  276    - -1.0  33.8 -0.23          -/   0 a marginal
   09-19T02:37:22Z 213313   1    WIDE OPEN norm  647.7  500.0   15  583.3  30.8   17   39          phase   live_meter_tip  276  1.5 -2.0  33.8 -0.23          -/   0 a marginal
   09-19T02:37:49Z 213313   3         OPEN slow  823.9  669.9   14  723.2  25.2   17   14          phase   live_meter_tip  274    - -2.0  40.4  0.41          -/   0 c late-onset
```

---

## 7. Sample sizes and honesty notes

* The headline class-b stratum is **9 open shots (6 LATE)**, 17 across all coverage (11 LATE).
  It is the strongest effect in the data (P(LATE) 66.7 % vs 8.3 %, and 80 % vs 18.9 % when cut
  on `latch → first reservation ≥ 100 ms`), but it rests on single-digit counts on the open
  slice. The mechanism is nevertheless *directly logged* — `break_geometry`, `restarts`,
  `fired_late`, `lateness_ms` — not inferred.
* The 12 `TIP DEADLINE DECISION: fired_late` events are the entire population of that event in
  both logs.
* `frames.csv` contains **one** standstill LATE. It refutes the catch-up hypotheses; it does not
  establish the (iii) mechanism by itself.
* No engine log exists for any session before 2026-09-18T00:14Z, so 39 of the 80 LATEs have
  sidecar-only fields. All Sept-17 sessions predate commits `b411cbd … e104e88` (the reader /
  landmark-lock work), so their absolute rates should not be pooled with Sept 18 for tuning.
* The `oracle.gap_px` → ms conversion the brief proposed is **not** supported; §2.1 shows why.
  The 2–3 px band is indeed fuzzy (one LATE at +2.0, one EXCELLENT at +2.0, six EXCELLENTs above
  +2.5 across the full set) — but the bigger problem is that the gap does not respond to our
  command time at the physical rate, so no threshold on it can be read as a ms budget.
* Nothing in this analysis was written to the engine, the sidecar, `settings.json`, or git.
