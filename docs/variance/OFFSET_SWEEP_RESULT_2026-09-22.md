# Offset sweep, block 2: result (2026-09-22, interim, not the formal pre-registered run)

**Session:** `session_20260922_165506`, started 21:55 UTC, launched with `-VarianceHunt -OffsetSweep`.

**Shots:** 225 offset draws, of which 190 graded Standstill shots joined to a draw. Distance: median 24.6 ft, p10–p90 23.8–27.0 ft.

**How the table was made:**
- Script: `scratchpad/sweep_table.py`. This is my own join, not `tools/timing/sweep_analysis.py`.
- Each record was joined to the nearest earlier `DEV FIRE OFFSET DRAW` (0–400 ms before its release).
- The formal script still needs the owner's protocol attestation (same spot, open standstills) and the build/settings hashes.

| Offset bin (ms) | Graded | EXCELLENT | 95% CI | EARLY | LATE | No banner | EXC incl. no-banner as a miss |
|---|---:|---:|---|---:|---:|---:|---:|
| −25 … −15 | 28 | 46% | 30–64% | 15 | 0 | 11 | 33% (13/39) |
| −15 … −5 | 45 | 58% | 43–71% | 17 | 2 | 12 | 46% (26/57) |
| −5 … +5 | 38 | 71% | 55–83% | 7 | 4 | 6 | 61% (27/44) |
| **+5 … +15** | **35** | **86%** | **71–94%** | 0 | 5 | 1 | **83% (30/36)** |
| +15 … +25 | 44 | 59% | 44–72% | 2 | 16 | 3 | 55% (26/47) |
| **All** | 190 | 64.2% | 57.2–70.7% | | | | |

## What it shows

1. **The offsets are delivered.** EARLY verdicts rise monotonically with earlier offsets (15 → 0) and LATE verdicts with later ones (0 → 16). The first stage works: the offset reaches the game's grade.
2. **The narrow fixed-band model is refuted.** For a W-ms band, mean success under a uniform ±25 ms sweep can be at most E[W]/50. That caps a 16 ms band at 32% and a 20.55 ms band at 41%. The observed 64.2% has a lower bound of 57.2%.
   - This holds for delivery at 1:1. The monotone EARLY/LATE crossover supports that, but the formal fidelity gate has not been run.
   - The visible ~15 ms green is not the band the grade behaves as.
3. **The bot fires early.** Unshifted shots (−5…+5) green 71% (61% counting no-banners as misses). Shots shifted +5…+15 ms green 86% (83%).
   - A quadratic fit to the graded shots peaks at +5 ms (77%).
   - Missing banners concentrate on the early side (23 of 33 are at ≤ −5 ms), which sharpens the peak.
   - **Caveat (winner's curse).** The +10 bin was chosen after the fact from five bins. The shift must be confirmed with a PRE-SPECIFIED test.
4. **The feedforward A/B shows no difference:** gain 0.20 gave 63.6% (99 shots) against 64.8% for gain 0.45 (91 shots). At this n, and with ±25 ms of added noise, that was expected. It is uninformative, not a null.

## Next: confirmation (pre-specified here, before the data)

- **Command:** `run_orion.local.ps1 -OffsetList "0,10"`. Shots alternate 0 / +10 ms. There is no FF A/B (the shipped FF stays in place) and no uniform sweep.
- **Primary:** the EXCELLENT rate at +10 minus the rate at 0. A missing banner counts as a miss. Two-sided, α = 0.05, Newcombe interval.
- **Size:** ≥ 100 shots per arm, ideally in a different session or on a different day from block 2. The +10 peak could be specific to this session's latency.
- **Decision:**
  - If +10 beats 0 by ≥ 5 points and the interval excludes 0, ship a +10 ms fire bias as a source default. The bias is reviewed for its interaction with the lead learner and the trim, since the shots it displaces are fenced from the learners.
  - If the difference is under 5 points or the interval crosses 0, do not ship it.

## CONFIRMATION RESULT: +10 ms REFUTED (same evening, `session_20260922_172143`)

- **Run:** `-OffsetList "0,10"`, 202 alternating shots. Shipped FF, no A/B. Distance p10 / median / p90 = 23.0 / 24.2 / 26.2 ft.

| Arm | n | EXCELLENT (primary: no banner = miss) | 95% CI | EARLY | LATE | No banner |
|---|---:|---:|---|---:|---:|---:|
| 0 ms | 101 | **76.2%** | 67.1–83.5% | 9 | 12 | 3 |
| +10 ms | 101 | **56.4%** | 46.7–65.7% | 4 | 33 | 7 |

- **Difference:** +10 minus 0 = **−19.8 points**, Newcombe 95% [−31.9, −6.8]. By the pre-specified rule the +10 bias is **NOT shipped**.
- **By half:**
  - First half: 41/51 at 0 against 23/50 at +10.
  - Second half: 36/50 at 0 against 34/51 at +10.
  - The gap narrowed late in the session (the owner's "excellent streak at the end").

**What this means:**
- The block-2 "+10 = 86%" was mostly winner's curse and/or a session-specific centre. In block 2 the unshifted shots leaned early (EARLY 7 / LATE 4, 61% with no-banners). In this session they were centred (EARLY 9 / LATE 12, 76%).
- Block 2's quadratic peak (+5 ms, 77%) matches this session's unshifted rate. The bot's normal timing was already at the optimum here.
- The banner trim does not explain the difference. It was ≤3 ms and decayed to 0 in block 2, and it stayed at 0 here.
- **What survives:**
  1. The narrow fixed band is refuted, since block 2 greened 64% under the sweep.
  2. The best offset differs between sessions by a margin comparable to 10 ms. That matches [online-latency drift](ONLINE_GRADING_CLOCK_2026-09-22.md).
- **The lever is not a fixed shift.** It is tracking the per-session centre faster than the trim does today, and that needs its own design plus a pre-specified test.

## Note (red team P6, CL2-P6-003)

The dev offset sweep was NOT fenced from the banner trim. At 22:05:56 a +24 ms shot moved the trim 3 ms earlier. The trim stayed ≤ 3 ms for the whole of block 2 and was 0 throughout the confirmation, so no bin changes by more than that. The conclusions above stand.

Related: CL2-P6-004. The feedforward fence has no size floor and drops ~45% of learner evidence, which likely explains the trim's slowness (7 steps in 430 shots).
