# HANDOFF — NBA 2K27 shot-timing bot, 2026-08-31

You are picking up mid-investigation on **NexusVision / Venice / Orion**, a commercial NBA 2K27
shot-timing bot. Read this whole document before touching anything. It is written to be pasted
into a fresh session; it assumes you have no prior context.

## TL;DR — read this paragraph if you read nothing else

The bot's measured green rate has fallen 52% → 18% over four days. **It is almost certainly not
a scheduling regression.** Graded per session with one consistent script (§4), the bot's landing
point has been essentially flat (92.4 → 93.3) while the *measured green window* marched
monotonically upward by **5.2 pp** (`green_start` 91.04 → 96.28). The window moved away from the
bot, not the other way round — and the most likely reason is that we repeatedly changed the
**fill scale itself** (detector retrain, sub-pixel fill, rung fill) while the aim constant stayed
pinned at `kTipTargetPct = 100.0`. **We have been changing the ruler while measuring with it.**
Start at §4a. Separately, the one engine change made today delivered its promised variance win
(sd 3.69 → 2.27, −38% vs −41% predicted) but also moved the median 1.9 pp early, which is
unexplained — keep the win, chase the shift.

The other standing instruction: **the owner's eyes and our instruments disagree** (he counted 3%
greens where the log says 18%). Do not fully trust either until §3 is resolved. Most of the
wasted effort in this project has come from trusting an instrument that turned out to be
circular, truncated, or — as now looks likely — drifting.

---

## 0. What the system is

- **Rig**: PC + Elgato HD60 X capture card + PS5. Video comes in over the capture card; input
  goes out over PS5 Remote Play through a patched `chiaki` fork (input-only; the video path is
  the card, not chiaki).
- **Vision**: `simple_meter_reader.py` (Python sidecar) reads the shot meter. A trained YOLO11n
  single-class detector (`meter_detector_yolo.py`, ONNX/CUDA) **proposes** the meter box; a
  colour/luma reader **measures the fill** inside that box.
- **Decision**: `native_orion/` (C++/Qt) — `AutomationEngine` schedules the release.
- **Mechanic being exploited**: in 2K27 a **perfectly timed green goes in every time**,
  regardless of shot type, distance, or contest. Contest only *shrinks* the window; it never
  adds randomness. So **make rate == green rate**, and this is a pure timing-precision problem.
  A **late release is a 100% miss.**
- **Owner's target**: high 90s green rate, "maybe 95%".

The bot presses and holds Square, watches the meter fill, and schedules the release ~300 ms
ahead of the predicted moment the fill reaches the tip.

---

## 1. State as of this handoff

### Running / built
- App **was launched** at 14:46 local (PID 17548, window title "Venice"). It may still be up.
  **Never launch `OrionNative.exe` directly** — always `run_orion.local.ps1`, or an orphan
  sidecar can silently wreck timing.
- `native_orion/build/Release/OrionNative.exe` built 14:32, newer than all sources (latest
  14:25). The fix described below **is** compiled in.
- Tests: **550 passed / 1 failed / 6 skipped**. The 1 failure is
  `scheduledFireConfirmCompletesRelease`, a long-documented clock-drift flake that fails
  standalone too. Not a regression.

### Config the last test ran under (do not change without recording it)
```
actuation_lead_ms      = 300      (owner-set; empirically chosen)
meter_color            = White    (2K27 meter is white; "Red" starves shot certification)
meter_style            = Arrow2
meter_delay_enabled    = false
meter_overlay_color    = #FF2BD6  (magenta)
```
The live file is the **repo** `settings.json` (dev build). The one under
`%LOCALAPPDATA%\NexusVision\Orion Native\` is stale (2026-08-08) and belongs to production
builds — do not be fooled by it.

Launcher env: `ORION_METER_DETECTOR=1`, `ORION_METER_DETECTOR_SYNC_ACQUIRE=1`,
`ORION_HORIZON_DEBIAS=1`. Sub-pixel fill and rung-tolerant fill are **default-ON in code**.

### The change under test
`[ORION_TIP_PHASE_IMMINENT]` — a guard written 2026-08-04 that refuses to arm a release when
the phase anchor is about to arrive. It had been wired **only into the 4 ms tick**. Measurement
across four prior sessions showed **112 of 112 production arms come from the sub-tick mirror**
(`arm_site=subtick`); the tick arms nothing. So the guard had **never run in production.** It
was mirrored into `reevaluateScheduleOnFreshSample` (`AutomationEngine.cpp:12731-12816`) as a
pure refusal — it creates no command and moves no schedule.

**Predicted** (offline counterfactual on 95 prior fires): Standstill landing sd 5.90 → 3.47 pp
(−41%), median landing unchanged at 95.19, late tail unchanged.

---

## 2. What actually happened in the live test — the raw numbers

**These are verified. Parsed from `logs/orion_native.log`, timestamps 19:46:36Z to 19:52:18Z
(= 14:46–14:52 local).**

52 releases, **49 graded landings**, all with a measured green window (`green_obs_n >= 3`).

| metric | value |
|---|---|
| EARLY (`peak_fill < green_start`) | **40 / 49 = 82%** |
| GREEN (`green_start <= peak_fill <= green_end`) | **9 / 49 = 18%** |
| LATE (`peak_fill > green_end`) | **0 / 49 = 0%** |
| median `peak_fill` | **93.33** |
| sd `peak_fill` | **2.27** (min 86.1, max 96.5) |
| median `green_start` | 96.28 |
| median `green_end` | 99.05 |
| median window width | 2.82 pp |
| median early shortfall (`green_start − peak`) | **4.66 pp** (p10 0.96, p90 7.88, max 9.74) |
| median `vel_at_rel` | ~0.175 %/ms |
| median `fill_at_rel` | ~34.5 |
| median `travel_pp` | ~59 |

By shot type:

| type | n | green | early | median peak | median green_start |
|---|---|---|---|---|---|
| Standstill | 39 | 9 | 30 | 93.53 | 96.04 |
| Right Fade | 6 | 0 | 6 | 91.01 | 96.50 |
| Left Fade | 4 | 0 | 4 | 89.81 | 96.04 |

Arm telemetry for the run: `arm_site=subtick` **33**, `arm_site=tick` **0**,
`armed_source=phase` **59**. The new hold fired: **26** `site=subtick` holds (196 total
including the tick's now-redundant copies).

### The owner's own count of the same batch
> "90% earlies 7% lates 3% greens — this is a rough estimate"

---

## 3. The contradiction — this is the crux, start here

| | owner's eyes | our log |
|---|---|---|
| early | ~90% | 82% |
| green | **~3%** | **18%** |
| late | **~7%** | **0%** |

**Early agrees. Green and late do not.** The log claims 6x more greens than the owner saw, and
claims zero lates where the owner saw ~7%.

At least one of these is wrong and you must find out which:

**(A) `peak_fill` cannot see lateness.** The owner has confirmed that in 2K27 the meter
**bounces past the tip** after release, and the depth of the descent encodes how late you were.
If the reader loses the meter or stops sampling at the bounce, a late shot's `peak_fill` would be
recorded *below* `green_end` and misgraded. **Partially refuted by the §4 table**: other sessions
recorded lates at 20.0%, 6.9%, 5.3%, 4.0%, 2.9%, 2.5% — so the instrument demonstrably *can* see
lateness. The 0% in the last run may therefore be genuine. Still worth confirming the bounce is
sampled, but this is no longer the leading hypothesis.

**(B) `green_start` / `green_end` are unreliable per-shot.** Consecutive Standstill windows from
the run: `[94.29, 97.17]`, `[96.83, 99.65]`, `[98.48, 100.00]`, `[98.94, 100.00]`,
`[99.42, 100.00]`, `[93.40, 97.63]`. That is a **7x spread in window width** (0.58 pp to
4.23 pp) across similar shots in one session, and a window reported as `[99.42, 100.00]` is
implausible. **Now the leading hypothesis**, and §4a shows the same quantity has drifted
monotonically by 5.2 pp across eight sessions — which is very hard to explain as anything but a
measurement artifact.

**(C) The owner's estimate is rough.** He said so himself. But he is watching the game's own
outcome, which is ground truth for makes, and "3% greens" is consistent with a very low make
rate. Do not dismiss this — he has been right and the instruments wrong before. Note his "7%
lates" may include shots the bot never timed, which he then released manually; those do not
appear as `Release landing:` rows at all (52 releases produced 49 landings).

**How to settle it:** capture a framedump session (`run_orion.local.ps1 -Framedump`) and, for
20–30 releases, compare the recorded `green_start` / `green_end` / `peak_fill` against the actual
frames, **including the post-release bounce**. The specific question: does the green band in the
pixels sit where the log says it sits? Every downstream number in this document depends on the
answer.

---

## 4. THE MOST IMPORTANT TABLE IN THIS DOCUMENT

Every session in `logs/orion_native.log`, graded by **one identical script**
(`grade_sessions.py`, see §5). This is apples-to-apples:

| session | n | green% | early% | late% | medPeak | sdPeak | **medGS** | medGE | medVel | medTrav | medFAR |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 08-28 22:22 | 10 | 40.0% | 40.0% | 20.0% | 92.38 | 2.12 | **91.04** | 92.93 | 0.1380 | 57.03 | 34.58 |
| 08-30 05:42 | 6 | 50.0% | 50.0% | 0.0% | 91.51 | 0.43 | **92.22** | 94.60 | 0.1686 | 57.87 | 33.64 |
| 08-30 16:25 | 40 | 52.5% | 45.0% | 2.5% | 94.31 | 11.37 | **94.34** | 96.24 | 0.1739 | 59.17 | 34.79 |
| 08-31 00:17 | 34 | 41.2% | 55.9% | 2.9% | 95.19 | 2.72 | **95.24** | 98.66 | 0.1757 | 59.28 | 35.37 |
| 08-31 04:51 | 29 | 48.3% | 44.8% | 6.9% | 95.03 | 8.40 | **95.19** | 98.58 | 0.1787 | 57.73 | 37.12 |
| 08-31 16:44 | 38 | 26.3% | 68.4% | 5.3% | 95.22 | 5.88 | **95.44** | 98.13 | 0.1817 | 56.55 | 38.01 |
| 08-31 18:20 | 25 | 28.0% | 68.0% | 4.0% | 95.24 | 3.69 | **95.90** | 98.91 | 0.1776 | 57.82 | 37.32 |
| **08-31 19:49** | 49 | **18.4%** | 81.6% | 0.0% | **93.33** | **2.27** | **96.28** | 99.05 | 0.1753 | 58.79 | 34.24 |

Three things fall straight out of this, and they change the whole diagnosis.

### 4a. `green_start` has drifted upward by 5.2 pp, monotonically, across every session

**91.04 → 92.22 → 94.34 → 95.24 → 95.19 → 95.44 → 95.90 → 96.28.** `green_end` did the same
(92.93 → 99.05). Meanwhile the bot's landing barely moved (92.38 → 93.33).

**The bot did not drift away from the window. The window drifted away from the bot.** That single
fact explains most of the green-rate collapse, and it is not a scheduling problem at all.

A monotonic 5 pp march across eight sessions is not a shot-mix artifact. The overwhelmingly
likely cause is that **we kept changing the ruler while measuring with it.** The fill scale is
`100 * filled_rows / (box_height - 1)` — it is defined by the detector box. Over exactly this
window we: retrained the detector (`meter2k27_n3_pill`, 08-30 — note `green_start` jumps
92.22 → 94.34 across that boundary), turned on sub-pixel fill (documented as reading slope
**+7.7% higher**), and added rung-tolerant fill. Each one redefines what "fill = 100" means.

The bot aims at a hardcoded `kTipTargetPct = 100.0`. **If the scale moved, the aim moved with
it, in physical terms, without anyone changing an aim constant.**

**This is the first thing to investigate.** It is also the most likely single explanation for why
the owner's make rate has fallen while every component metric looked healthy.

### 4b. The green-rate collapse started BEFORE today's fix

52.5% → 41.2% → 48.3% → **26.3% → 28.0%** → 18.4%. The 16:44 and 18:20 sessions were already at
26–28%, and both predate the imminent-hold mirror. **Do not attribute the collapse to that fix.**

### 4c. The fix did exactly what it promised — on variance

`sdPeak` across the last three sessions: **5.88 → 3.69 → 2.27.** The final step is **−38%**
against a predicted −41%, and 2.27 is the best spread ever recorded on a batch of meaningful size.
That is a real, delivered win and should not be thrown away while chasing the median.

What the fix *did* also do is move the median early: `medFAR` (fill at release) dropped
**37.32 → 34.24**, so the bot is commanding the release ~3 pp earlier in fill, and `medPeak`
followed (95.24 → 93.33). That was **not** predicted and is not yet explained — a refusal to arm
early should, if anything, push the median later. **Treat this as an open defect**, and A/B it
with `tipPhaseImminentHoldBandPct` 6 vs 0 to confirm causation before assuming it.

---

## 5. The grading script

```
C:\Users\aaron\AppData\Local\Temp\claude\C--Users-aaron-Desktop-NexusVision\
  7396b864-d4d6-441b-840e-8730daa5e4ba\scratchpad\grade_sessions.py
```

Already run — its output is the table in §4. Re-run it after every test batch; it is the only
consistent grader in the project. It parses every `Release landing:` line in
`logs/orion_native.log`, buckets into sessions on >10 min gaps, and grades `peak_fill` against
that shot's own `[green_start, green_end]`.

If the scratchpad has been cleaned it is ~40 lines and trivial to rewrite: regex
`(\w+)=(-?[\d.]+)` over `Release landing:` lines, bucket by timestamp gap, grade `peak_fill`
against `[green_start, green_end]`, report medians and sd.

**Caveat that still stands:** the older subagent baseline quoted "41.1% green, sd 5.52,
median 95.19" from a 4-session pool. The per-session table shows those sessions were
heterogeneous (sd ranges 0.43 to 11.37), so any pooled statistic across them is close to
meaningless. Grade per session, always.

---

## 6. The owner's suggestion, and the trap in it

> "me moving the shot lead probably wouldve enabled more greens"

He is directionally right on the arithmetic. Median shortfall is 4.66 pp; at 0.175 %/ms that is
~27 ms. Lead 300 → ~273 would, under a naive rate model, move the median landing from 93.33 to
~98.0 — inside the measured window `[96.28, 99.05]`.

**But there is a directly conflicting measurement.** A prior A/B by the owner moved the lead by
10 ms and the median landing moved **0 pp**, where the rate model predicted +1.8 pp. That result
is recorded in memory as `lead-is-a-weak-lever-on-landing-position` and concluded the error is
**variance, not offset**.

So `dLanding/dFireTime` is **disputed by an order of magnitude**, and this is the single most
important unresolved physical question in the project. Everything about aim depends on it.

**Recommended experiment** (after §5 and §3):
- One batch at lead 300, one at lead 285, nothing else changed, ≥40 shots each, Standstill only,
  graded by the same script.
- If the median moves ~2.6 pp, the rate model is right and the lead is the lever.
- If it moves ~0, the prior A/B is right, the shortfall is not an offset, and chasing the lead is
  a dead end — go back to variance.
- **Stop immediately if lates rise.** A late is a guaranteed miss; an early at least sometimes
  drops. The asymmetry is brutal and it is why the bot is deliberately tuned early.

---

## 7. Established facts — do NOT re-derive these

Each cost real time to establish. Treat as settled unless you have new evidence.

- **Tip = guaranteed make.** No RNG. Contest shrinks the window but never randomises it. Late =
  100% miss. Owner-confirmed.
- **Aiming AT the tip is wrong.** Measured shift table (Standstill n=82, each landing shifted
  against its *own* window): +1.5 pp → 54.9% green / 8.5% late; +2.0 pp → 47.6% / **23.2% late**;
  all the way to the tip → ~28% green / **~59% late**. `green_end` is **not** pinned at 100
  (median 98.3 in that corpus, 99.05 in this one). The window's **bottom** is the binding edge.
  Correct aim is `tip − 3σ`, so **shrink σ first; the aim point rises on its own.**
- **Every production arm is `arm_site=subtick`.** The 4 ms tick arms nothing. Any guard wired
  only into the tick path is dead code that reads as live. Two were found; one is fixed (the
  imminent hold), one remains — see §8.
- **`expectedRisePct` / `withinReach` are dead code** under autonomous vision. `processHolding()`
  delegates and returns at `AutomationEngine.cpp:7639`; the block computing them sits ~1000 lines
  below that return. There is **no dormant "rate-compensated aim" to switch on** — the live path
  already applies the same time-domain compensation (`predictedTip − lead − greenCenterOffset`).
  A full subagent run was wasted on this; don't repeat it.
- **The game is NOT frame-quantized** (p=0.0027, power 0.976). There is no 85% ceiling from
  quantization. At sd 3 ms → ~97%; at sd 2 ms → ~99.9%.
- **Schedulability margin IS the aim.** `command_eta = anchorTime + phaseConst − lead − now`.
  Lowering lead and phase constant together leaves it *exactly* unchanged. You cannot buy
  abort-margin without spending late tail.
- **Top-rung meters are unschedulable by construction.** At anchor 40 (`phase_const_ms` 308.7)
  the margin against a 300 ms lead is 8.7 ms — under one detector frame. Not a bug; geometry.
- **Detection is not the limiter.** ~99.7% on both Arrow2 and Pill with **0%** false locks
  (region/size gate in `meter_detector_yolo.py::_plausible`). Don't spend time here.
- **The "stuck Square button" is not our bug.** The game ignores Square during ball-retrieval
  windows. Proven: runs of 6 presses over 2.4 s, every run ending with a right-stick shot that
  fired immediately.

---

## 8. Known-broken / flagged, not yet fixed

- **`greenCenterOffsetMs` is inert on the arm.** The tick subtracts it from the fire deadline
  (`AutomationEngine.cpp:7197`); the sub-tick mirror (`:12647`) and
  `updateAutonomousTipReservation` (`:11125`) do not. Harmless at the shipped `0.0`, but setting
  `ORION_GREEN_CENTER_FRAC` / `autonomousGreenCenterFrac` today does **nothing** on the arm and
  would cause tick↔mirror deadline churn. **Fix this parity before anyone sweeps that knob.**
- **Fades are materially worse** (median peak 89.81 / 91.01 vs 93.53 Standstill). The cause is
  **not** scheduling — measured fire rates were Fade 81% vs Standstill 86%, and the fade aborts
  took ownership at press+912 ms / press+1274 ms with the meter **first seen at fill 25.7 and
  31.8** (healthy shots: press+558 ms, first seen at 6–13). That is **late detector acquisition
  on fades**, upstream of the engine, in the reader. Untouched.
- The 196-vs-26 hold count means the tick still emits its now-redundant copy every 4 ms. Noisy
  logs, harmless. Consider suppressing the tick's copy.

---

## 9. Refuted — do not try these again

- **Fill denominator latch.** Made σ_y 15% *worse* and crossing scatter +91%. Removed.
- **Brightness-snap box refinement** (`_refine_box_local`). p90 error 30 → 50 px. Worse.
- **Meter-speed scaling of the aim.** 75% worse spread, shifts into the late tail.
- **Arm-source-switching as the cause of streaks.** All the same source.
- **A sigma cap on release authority.** Broke 26 tests — the wide fallback chain is load-bearing
  by design. Fully reverted.
- **Centring the aim in the window.** Owner's own reasoning, and correct: aim at the tip side so
  that if the window shrinks under contest, the shot still lands inside it.
- **`ORION_FILL_FORECAST`** — a torch model that never loads under `ORION_SIMPLE_READER=1`. Dead
  weight, not a lever.
- **1080p detection** (null, p=0.80) and **`ORION_CAPTURE_MJPG`** (no-op; the HD60X driver
  returns YUY2 regardless).

---

## 10. Traps that have bitten this project repeatedly

1. **Silent link-step skip.** `OrionNative.exe` sometimes does not relink despite rebuilt object
   files. **Delete the exe, then rebuild**, and verify the timestamp advanced past every source
   file. This has caused multiple "the fix didn't work" false alarms.
2. **`OrionNativeTests.exe` produces no streamed output.** Use `-o <file>,txt` and read the file.
3. **Never start `OrionNative.exe` directly.** Use `run_orion.local.ps1`.
4. **Force-killing Orion corrupts `settings.json`** (zeroes `actuation_lead_ms`). Back the file
   up before edits; close the app cleanly.
5. **`.git` is HIDDEN on Windows** — a copy without `-Force` silently skips it. The repo has been
   lost this way once. Remote is `github.com/zaehuncho/Venice`; push every session.
6. **Consistency-bench replays are nondeterministic** (1121/2716 rows differed on identical
   code). Use `SYNC=1` and byte-identity, never a loose comparison.
7. **Grep logs with `-a`** (they contain binary) and read with `utf-8-sig`.
8. **Tests can be green because fixtures are cleaner than reality.** Several gates that pass in
   tests are unmeetable on live data.
9. **Instruments have repeatedly been circular.** `travel_pp` is circular for latency;
   `settled_fill` grades on a threshold that slides with meter delay; `peak_fill` is
   information-free when meter delay is ON (it is currently OFF, so it is *supposed* to be valid
   — but see §3).

---

## 11. Suggested order of work

1. **Chase the `green_start` drift (§4a). This is the main event.** Correlate the per-session
   `medGS` column against the reader/detector changes committed between those sessions
   (detector retrain 08-30, sub-pixel fill, rung fill). Concretely: replay one fixed framedump
   through the reader at each of those code revisions and see whether `green_start` for the
   *same frames* changes. If it does, the fill scale is not stable and **the aim constant
   `kTipTargetPct = 100.0` has silently meant a different physical point in every session.**
   Nothing else on this list matters as much.
2. **Validate the green window against pixels** (§3) with a framedump. Does the green band sit
   where the log says?
3. **A/B the imminent hold** (`tipPhaseImminentHoldBandPct` 6 vs 0, ≥40 Standstill shots each).
   Confirm whether it caused the −3 pp `fill_at_rel` shift, or only the variance win. **Keep the
   variance win** (sd 3.69 → 2.27) — if the hold is causing the median shift, fix the shift, do
   not revert the hold.
4. **Then, and only then**, the lead A/B in §6 to settle `dLanding/dFireTime`.
5. Fix the `greenCenterOffsetMs` parity gap (§8) before touching that knob.
6. Fades: attack **detector acquisition latency**, not the engine.

Re-run `grade_sessions.py` after every batch, and compare **per session**, never pooled.

---

## 12. File map

| path | role |
|---|---|
| `simple_meter_reader.py` | the reader: fill measurement, NCC tracking, ghost breaker, sub-pixel edge |
| `meter_detector_yolo.py` | YOLO meter-box proposal + `_plausible()` region/size gate |
| `native_orion/src/AutomationEngine.{cpp,h}` | scheduling, arming, the imminent hold (`:12731`) |
| `native_orion/src/OrionAppController.cpp` | log emission, `Release attribution:` / `Release landing:` |
| `native_orion/src/AppConfig.{h,cpp}` | settings load/migrate; **pins overlay colour to the compiled default on every load** |
| `native_orion/tests/AutomationEngineTests.cpp` | 556 tests |
| `run_orion.local.ps1` | **the mandatory launcher**; `-Framedump` / `-Detdiag` switches |
| `logs/orion_native.log` | the engine log; `Release landing:` lines are the grading source |
| `tools/quality/consistency_bench.py` | offline replay bench |
| `tools/quality/green_window_probe.py` | green-window measurement |

Key log lines to grep:
- `Release landing:` — the graded outcome, one per shot
- `Release attribution:` — the decision inputs
- `TIP PHASE IMMINENT HOLD: site=subtick` — proves the new fix is reached
- `arm_site=` / `armed_source=` — which path armed
- `scope EMPTY` — the DHCP-drift failure that benches the bot entirely

---

## 13. How to work with the owner

He is technical, tests on real hardware, and **routinely refutes incorrect diagnoses with
measurements** — this is welcome and has repeatedly saved the project. Give him numbers, not
reassurance. If something is unverified, say so plainly. He has explicitly released the ship
deadline in favour of quality, so there is no pressure to claim a fix works before it is
measured.

He is currently testing on **Arrow 2** meter style. **Pill** is supported (detector retrained to
99.7%) but not yet live-tested. **Straight** has never been seen live — deferred.
