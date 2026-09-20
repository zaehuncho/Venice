# Poll-phase tracker — measurement and verdict (2026-09-17)

**Question (owner, 09-17 00:30):** the console samples the pad once per frame, so the release edge
quantises onto a 60 Hz poll grid. On a ~1-frame fade window the grade should then depend on the
PHASE between our release instant and that grid. If the phase is stable within a session, the
game's own verdicts tell us which side of a poll we landed on, and a per-session phase estimate
could steer the fire instant to the safe side — a much faster loop than the 3 ms banner trim.

**VERDICT: the phase is measurable, stable and cheap to track — and the verdict does not depend
on it. Do not build the tracker.**

- The console's frame grid *is* recoverable and *is* stable: it drifts **−0.05 ms/s** against the
  engine clock (implied console period **16.666 ms = 60.003 Hz**) and the per-shot grid estimates
  agree to **sd 1.07 ms** across a whole 150 s session. A session-level phase model is trivially
  feasible. That half of the hypothesis holds.
- The release instant is measured against that grid to about **±1 ms**, and the corpus covers the
  whole frame (pooled circular concentration R = 0.068; uniform = 0).
- Across **284 banner-graded releases in 12 sessions**, P(EXCELLENT) folded on that phase has a
  first-harmonic swing of **4.5 pp** against a permutation null whose 95th percentile is **11.3 pp**
  (p = 0.73). Nothing survives in any subgroup, at any period from 3 to 50 ms
  (scan-corrected p = 0.79), per session (p = 0.62), or across session halves (p = 0.57).
- On the continuous **RELEASE ORACLE gap** (n = 221, which separates EXCELLENT from LATE by 9 px of
  median retraction gap), the phase fold gives **1.22 px** against a null p95 of **1.60 px**
  (p = 0.19). A genuine poll coin-flip would have to move this by order 9 px.
- **Bound:** a true peak-to-trough swing of 20 pp in P(green) across the frame would have produced an
  amplitude this small only **4.9 %** of the time; 25 pp → 0.6 %; 30 pp → 0.3 %. A real
  1-frame-window coin flip is a ~100 pp swing. **The strong form of the hypothesis is refuted.**

What is left over is in §8: the residual LATE/EXCELLENT split is best predicted by *how late the shot
was armed* (`tip_eta_ms` AUC 0.607, p = 0.002; `resv_fill_pct` AUC 0.404, p = 0.012) and by the
*quality of the grid fit itself* (`frame_phase_sd_ms` AUC 0.410, p = 0.012) — not by the console
poll and not by reader timestamp age (`frame_age_ms` AUC 0.428, p = 0.17). §9 says what would
resolve the remainder; §10 records a separate measured finding that matters more than the tracker
would have: **frame-centred firing is not actually concentrating the release on the frame centre.**

---

## 1. Corpus and instruments

Session extracts from the scratchpad (the same 16 runs the 09-17 00:15 fade study used), parsed
read-only. Nothing was launched; no engine or reader source was touched.

| session log | local start | shots | EXC | LATE | EARLY | ungraded | leads seen | release-phase R | grid drift ms/s |
|---|---|---|---|---|---|---|---|---|---|
| cur_session.log | 09-15 15:49 | 26 | 14 | 0 | 8 | 4 | 286 | 0.434 | −0.050 |
| cur_session2.log | 09-15 16:50 | 29 | 19 | 3 | 3 | 4 | 274–286 | 0.297 | −0.038 |
| cur_session3.log | 09-15 17:36 | 13 | 5 | 6 | 0 | 2 | 274 | 0.556 | −0.050 |
| cur_session4.log | 09-15 20:37 *(the framedump drill)* | 29 | 6 | 7 | 2 | 14 | 274 | 0.307 | −0.052 |
| cur_session6.log | 09-15 23:52 | 39 | 22 | 13 | 2 | 2 | 274–302 | 0.018 | −0.060 |
| cur_session10.log | 09-16 14:59 | 19 | 13 | 6 | 0 | 0 | 274 | 0.368 | −0.085 |
| cur_session11.log | 09-16 15:23 | 45 | 25 | 15 | 3 | 2 | 274–286 | 0.099 | −0.045 |
| cur_session12.log | 09-16 20:26 | 27 | 21 | 3 | 2 | 1 | 261–280 | 0.205 | −0.025 |
| cur_session13.log | 09-16 20:43 | 21 | 9 | 6 | 4 | 2 | 269–282 | 0.083 | −0.090 |
| cur_session14.log | 09-16 21:00 | 20 | 11 | 3 | 6 | 0 | 274–289 | 0.069 | −0.050 |
| cur_session15.log | 09-16 22:20 | 38 | 23 | 5 | 4 | 6 | 274–289 | 0.189 | −0.050 |
| cur_session16.log | 09-16 23:04 | 6 | 5 | 1 | 0 | 0 | 274 | 0.966 | — |

**Totals: 323 released vision shots; 284 with an attributed banner (180 EXCELLENT / 70 LATE /
34 EARLY = 63.4 % green); 221 with a `RELEASE ORACLE` gap.**
Types: 240 Standstill, 47 Right Fade, 32 Left Fade, 4 other.

### The instrument chain, and what each link is worth

| link | source | measured precision |
|---|---|---|
| release instant, engine clock | `Precise dispatch timing: … command_issued_ms=` | µs resolution |
| game-frame grid | `FRAME PHASE: … phase_ms= period_ms=` (60 Hz grid least-squares-fitted to the fill staircase) | **sd 1.07 ms, MAD 0.55 ms** (n = 306, per-shot residual after removing the per-session drift) |
| release ⟂ grid, same clock | identity `command_issued_ms = anchor_ms + phase_const_ms − lead_ms` | median **−0.006 ms**, MAD **0.030 ms**, 82 % within ±1 ms |
| PC-side dispatch jitter | `active_route_wait_ms` / `pipe_write_us` / `pipe_ack_wait_us` | 0.399 ms median, **sd 0.155 ms** / 11 µs / 383 µs |
| verdict | sidecar `BANNER VERDICT:` attributed by time (window 500–3200 ms) | lag median **1270 ms** (p10 980, p90 1557); **93 % agreement** (191/206) with the epoch-keyed `RELEASE ORACLE` proxy |
| verdict (continuous) | `RELEASE ORACLE: … gap_px=` keyed on the physical epoch — no time attribution at all | EXC median 0 px (IQR 0–1), LATE 9 px (4.75–12), EARLY 4 px (3–5) |
| PC → console input transport | **not instrumented** | the one gap — see §9 |

**Phase definitions folded.** `phase_frame_ms` = `(command_issued_ms − phase_ms) mod 16.667` — the
release's distance past the preceding boundary of *this shot's own* recovered console grid. This is
the physically meaningful one. `phase_engine_ms` and `phase_wall_ms` (the raw engine clock and the
`fire_epoch_ms` wall stamp, mod 16.667) are folded as controls.

**Coverage.** Pooled `phase_frame_ms`: n = 323, mean 8.32 ms, sd 4.61 ms, **R = 0.068** (uniform sd is
4.81, R = 0). The 16-bin histogram is `13 21 31 20 12 14 12 25 49 19 20 9 17 25 19 17` — not uniform
(χ² = 67.7, df = 15) because of one excess bin at the frame centre (§10), but **every 1.04 ms slice of
the frame holds 9–49 shots**. The frame is covered edge to edge; a phase effect had somewhere to show,
and the permutation null is computed against the phases as they actually fell, so the lumpiness costs
the test nothing.

---

## 2. The fold: does the verdict depend on the release phase?

Effect size = first-harmonic amplitude `A = 2·|mean((y − ȳ)·e^{iθ})|`, i.e. the peak-to-trough swing
of the best-fitting sinusoid in the units of y. Null = 4000 permutations of y **within session ×
shot-type strata** (each session keeps its own green rate, each type its own difficulty; only the
phase relation is broken). "saw" = the best split of the circle into two half-frame arcs, the shape a
hard poll boundary would make.

### ALL graded vision releases — n = 284 (EXC 180 / LATE 70 / EARLY 34)

| phase | outcome | sine amp | null med | null p95 | peak @ms | p | saw height | cliff @ms | p |
|---|---|---|---|---|---|---|---|---|---|
| **phase_frame_ms** | P(EXCELLENT) | **0.045** | 0.063 | 0.112 | 14.73 | **0.727** | 0.088 | 1.74 | 0.750 |
| **phase_frame_ms** | P(LATE) | 0.061 | 0.042 | 0.084 | 7.39 | 0.234 | 0.092 | 3.82 | 0.426 |
| **phase_frame_ms** | signed(LATE−EARLY) | 0.081 | 0.058 | 0.120 | 7.92 | 0.257 | 0.122 | 3.12 | 0.516 |
| phase_engine_ms | P(EXCELLENT) | 0.022 | 0.046 | 0.095 | 11.88 | 0.867 | 0.054 | 0.35 | 0.944 |
| phase_engine_ms | P(LATE) | 0.015 | 0.041 | 0.085 | 1.84 | 0.920 | 0.046 | 4.86 | 0.956 |
| phase_engine_ms | signed(LATE−EARLY) | 0.018 | 0.054 | 0.113 | 16.34 | 0.928 | 0.056 | 3.82 | 0.978 |
| phase_wall_ms | P(EXCELLENT) | 0.042 | 0.047 | 0.098 | 16.27 | 0.577 | 0.085 | 5.56 | 0.626 |
| phase_wall_ms | P(LATE) | 0.051 | 0.039 | 0.081 | 11.06 | 0.299 | 0.096 | 5.56 | 0.336 |
| phase_wall_ms | signed(LATE−EARLY) | 0.094 | 0.052 | 0.109 | 12.17 | 0.103 | 0.162 | 0.00 | 0.118 |

### Subgroups (best p over the three outcomes, on `phase_frame_ms`)

| subgroup | n | EXC/LATE/EARLY | best sine amp | best p | best saw p |
|---|---|---|---|---|---|
| Standstill | 214 | 139 / 52 / 23 | 0.062 | 0.222 | 0.275 |
| Fades (Left + Right) | 66 | 38 / 17 / 11 | 0.183 | 0.287 | 0.594 |
| Right Fade alone | 38 | 22 / 10 / 6 | 0.155 | 0.540 | 0.529 |
| `fire_target=frame_centre` | 252 | 164 / 57 / 31 | 0.095 | 0.205 | 0.427 |
| grid LOCKED at dating | 83 | 50 / 22 / 11 | 0.121 | 0.483 | 0.448 |
| tempo=normal | 32 | 24 / 6 / 2 | 0.260 | **0.034** | 0.076 |

Two nominal hits appear across the whole exercise: `tempo=normal` signed, p = 0.034 (n = 32), and
Standstill on `phase_wall_ms` signed, p = 0.047. That is **2 hits out of 126 folds** (7 blocks × 3
phase variables × 3 outcomes × 2 statistics), where ~6 are expected by chance. Neither is on the
physically meaningful variable in a slice with real n, and neither reproduces on the other phase
variables in the same slice. They are multiplicity, not signal.

### The strongest single test: the continuous oracle gap

The `RELEASE ORACLE` gap is a graded, epoch-keyed outcome with no attribution step and near-perfect
discrimination (EXC median 0 px vs LATE 9 px). It is worth more power than the 3-class banner.

| outcome | n | pooled sine amp | null p95 | p | per-session amp | null p95 | p |
|---|---|---|---|---|---|---|---|
| `oracle_gap_px` | 221 | 1.221 px | 1.600 px | **0.186** | 2.162 | 2.697 | 0.396 |
| gap clipped at 0 | 221 | 1.219 px | 1.554 px | 0.177 | 2.132 | 2.623 | 0.352 |

**A poll coin flip would move this outcome by order 9 px peak-to-trough. It is bounded at 1.6 px.**

---

## 3. The hypothesis in its own shape: a phase that differs BY SESSION

Pooling across sessions would hide a phase that is stable within a session but unknown across them —
which is exactly what the owner proposed. Three tests that pooling cannot do:

| test | statistic | n | sessions | observed | null median | null p95 | p |
|---|---|---|---|---|---|---|---|
| **A.** per-session amplitude (n-weighted mean of the within-session first harmonic), `phase_frame_ms` | amp | 284 | 11 | 0.161 | 0.168 | 0.210 | **0.620** |
| A. same, `phase_engine_ms` | amp | 284 | 11 | 0.160 | 0.170 | 0.216 | 0.660 |
| A. same, `phase_wall_ms` | amp | 284 | 11 | 0.157 | 0.170 | 0.217 | 0.687 |
| **B.** split-half consistency: cos(green-phase angle in half 1 − angle in half 2), `phase_frame_ms` | cos | 284 | 11 | **−0.006** | +0.036 | +0.398 | **0.567** |
| B. same, `phase_engine_ms` | cos | 284 | 11 | −0.057 | −0.010 | +0.368 | 0.586 |

The observed per-session amplitude is **below** its own null median. The split-half consistency —
the single property a phase tracker has to exploit, "the safe phase at the start of the session is
still the safe phase at the end" — is **zero**.

**Cross-session agreement of the green-phase peak: circular R = 0.193 over 11 sessions**, against
R ≈ 0.30 for 11 angles drawn at random. Median |split-half peak gap| = 2.76 ms out of a possible
8.33 ms. The per-session peaks are 8.40, 12.60, 13.68, 4.30, 0.96, 1.35, 8.83, 0.02, 5.83, 1.44,
14.19 ms — scattered over the whole frame.

### Is the phase we land on even predictable from recent shots?

| session | n | lag-1 circular corr | mean \|phase − circ. median of previous 10\| | uniform expectation |
|---|---|---|---|---|
| cur_session.log | 22 | +0.307 | 3.40 ms | 4.17 ms |
| cur_session10.log | 19 | +0.072 | 4.52 | 4.17 |
| cur_session11.log | 43 | +0.020 | 4.13 | 4.17 |
| cur_session12.log | 26 | +0.166 | 4.26 | 4.17 |
| cur_session13.log | 19 | −0.467 | 4.07 | 4.17 |
| cur_session14.log | 20 | −0.341 | 4.68 | 4.17 |
| cur_session15.log | 32 | +0.023 | 3.91 | 4.17 |
| cur_session2.log | 25 | +0.402 | 2.82 | 4.17 |
| cur_session4.log | 15 | +0.063 | 2.05 | 4.17 |
| cur_session6.log | 37 | +0.103 | 5.08 | 4.17 |

Lag-1 correlation averages ≈ +0.03 with both signs represented; the 10-shot window predicts the next
release phase no better than "uniform". Even if a safe phase existed, the *fire* does not currently
hold a phase from shot to shot well enough to exploit it without a deliberate change (§10).

---

## 4. Period scan — is there ANY input cadence in there?

Per-session amplitude on `command_issued_ms mod T`, 189 periods swept from 3 to 50 ms, with a
**scan-corrected** p (null = the same max-over-periods statistic on permuted verdicts):

| period ms | Hz | observed amp | null p95 | pointwise p |
|---|---|---|---|---|
| 4.0000 | 250 | 0.176 | 0.210 | 0.375 |
| 8.3335 | 120 | 0.156 | 0.200 | 0.546 |
| **16.6667** | **60 (console frame)** | **0.170** | **0.216** | **0.519** |
| 20.0000 | 50 | 0.226 | 0.208 | 0.011 |
| 29.7500 | 33.6 | 0.233 (scan max) | — | 0.015 |
| 33.3333 | 30 | 0.226 | 0.210 | 0.015 |
| 33.3660 | 29.97 (the measured Remote Play stream cadence, 09-03) | 0.157 | 0.210 | 0.632 |
| 50.0000 | 20 | 0.211 | 0.211 | 0.057 |

**Scan-corrected p over the whole 3–50 ms sweep = 0.787.** The three pointwise hits are what 189
correlated tests produce; none is at a plausible cadence, and the one period with a physical claim on
it (33.366 ms, the stream's own measured period) is null. This reproduces the 2026-09-03 verdict
("POLL PHASE NOT DETECTED", n = 130, fork AV-clock stamps) on a different instrument, a different
outcome variable (banner + oracle, not landing fill) and 2.2× the sample.

---

## 5. Drift — the one part of the hypothesis that holds

Grid search on a drift rate d (ms of grid phase per second of engine time) maximising the circular
concentration of the per-shot grid residues inside one session:

| session | shots | span s | best drift ms/s | R at best | R at d = 0 | implied console period ms | implied Hz |
|---|---|---|---|---|---|---|---|
| cur_session.log | 26 | 156 | −0.050 | 0.997 | 0.539 | 16.6658 | 60.003 |
| cur_session10.log | 19 | 78 | −0.085 | 0.949 | 0.699 | 16.6653 | 60.005 |
| cur_session11.log | 45 | 159 | −0.045 | 0.914 | 0.623 | 16.6659 | 60.003 |
| cur_session12.log | 27 | 93 | −0.025 | 0.896 | 0.871 | 16.6663 | 60.001 |
| cur_session13.log | 21 | 76 | −0.090 | 0.882 | 0.654 | 16.6652 | 60.005 |
| cur_session14.log | 20 | 67 | −0.050 | 0.968 | 0.904 | 16.6658 | 60.003 |
| cur_session15.log | 38 | 171 | −0.050 | 0.914 | 0.504 | 16.6658 | 60.003 |
| cur_session2.log | 29 | 112 | −0.038 | 0.941 | 0.824 | 16.6660 | 60.002 |
| cur_session3.log | 13 | 89 | −0.050 | 0.941 | 0.811 | 16.6658 | 60.003 |
| cur_session4.log | 29 | 100 | −0.052 | 0.985 | 0.832 | 16.6658 | 60.003 |
| cur_session6.log | 39 | 142 | −0.060 | 0.905 | 0.523 | 16.6657 | 60.004 |

**Drift is −0.05 ms/s (≈ 50 ppm of crystal offset), consistently, in every session.** The grid phase
moves 3 ms per minute; a session-level phase estimate would stay inside ±1.5 ms for a 60 s window
with no drift term at all, and inside ±0.5 ms with one. Concentration after removing drift is
0.88–1.00 — the recovered grid is a real, coherent 60 Hz clock, not a fit artefact.

Two corollaries worth keeping:
- **The console is at 60.003 Hz on our clock, not 59.94.** The 09-03 note's 59.94 figure came from
  the Remote Play *stream* cadence (29.97 Hz in capture-card/input-only mode), which is a different
  clock from the game's render/grade clock that the fill staircase exposes. The engine's 16.6667 ms
  constant is right to 1 part in 20 000.
- **This is the positive control for §2.** The instrument that found nothing is the same instrument
  that resolves a 60 Hz grid to ±1.07 ms over 150 s. The null is not a resolution failure.

---

## 6. Power — what size of effect this corpus would have caught

Observed pooled sine amplitude on `phase_frame_ms`, P(EXCELLENT), n = 284: **0.045**. Permutation
critical value (95th percentile): **0.113**.

| injected peak-to-trough swing in P(green) | detection rate at α = 0.05 | P(observing an amplitude ≤ 0.045 if true) |
|---|---|---|
| 5 pp | — | 0.385 |
| 10 pp | 11 % | 0.277 |
| 15 pp | 22 % | 0.105 |
| **20 pp** | 47 % | **0.049** |
| 25 pp | — | 0.006 |
| **30 pp** | **88 %** | 0.003 |
| 50 pp | 100 % | — |
| 80 pp | 100 % | — |

**95 % upper bound on the true swing: ~20 pp.** The hypothesis as stated — a coin flip on a
one-frame window decided by which side of a poll we land on — is a ~100 pp swing. It is excluded by
more than a factor of five.

### Convergence, had an effect existed (the question as asked)

Simulated circular-mean tracker, phases uniform, shots needed to place the safe phase within ±3 ms:

| swing | 68 % of the time | 90 % of the time |
|---|---|---|
| 10 pp | 150 shots | 800 shots |
| 20 pp | 50 | 150 |
| 30 pp | 20 | 75 |
| 50 pp | 10 | 30 |
| 100 pp | 10 | 10 |

Read with §5: drift (3 ms/min) would not have been the binding constraint — at 30 pp a tracker
converges inside a 20-shot window, which is ~90 s. **The tracker's arithmetic was never the problem;
the signal is.**

---

## 7. The 09-15 drill, shot by shot — the "identical landings graded LATE vs EXCELLENT"

The forensics that motivated this work (`D:\NexusVision\framedump\_analysis\`, 09-15 20:37 local
drill) found LATE and EXCELLENT shots with identical fill@command, identical slope, identical peak,
and concluded "poll-phase coin flip on a ~1-frame window". Here are those same shots with the phase
filled in:

| seq | type | banner | release phase in frame (ms) | frame_offset_ms | fill@release | lead |
|---|---|---|---|---|---|---|
| 2 | Standstill | EXCELLENT | 3.40 | 6.46 | 38.00 | 274 |
| 3 | Standstill | EARLY | 4.53 | 5.21 | 37.80 | 274 |
| 4 | Standstill | **LATE** | 4.03 | 0.00 | 37.80 | 274 |
| 5 | Right Fade | **LATE** | 6.45 | 3.05 | 36.20 | 274 |
| 6 | Standstill | EXCELLENT | 2.62 | 0.00 | 37.80 | 274 |
| 7 | Standstill | **LATE** | 3.27 | 6.08 | 37.90 | 274 |
| 8 | Standstill | EXCELLENT | 3.56 | 5.76 | 37.90 | 274 |
| 10 | Standstill | **LATE** | 2.40 | 6.80 | 38.00 | 274 |
| 12 | Standstill | EXCELLENT | 2.05 | 0.00 | 38.20 | 274 |
| 18 | Standstill | **LATE** | 11.43 | −2.77 | 39.30 | 274 |
| 22 | Right Fade | EXCELLENT | 3.29 | 5.18 | 36.60 | 274 |
| 24 | Left Fade | EARLY | 2.73 | 5.55 | 37.10 | 274 |
| 26 | Standstill | **LATE** | 1.04 | 7.05 | 38.10 | 274 |
| 27 | Standstill | EXCELLENT | 1.55 | 6.54 | 38.00 | 274 |
| 29 | Right Fade | **LATE** | 7.83 | −7.24 | 34.40 | 274 |

Standstills in the tight cluster (phase 1.0–4.6 ms, i.e. inside a quarter of one frame): **5
EXCELLENT, 4 LATE, 1 EARLY, fully interleaved** — 2.05/2.62/3.40/3.56/1.55 green against
1.04/2.40/3.27/4.03 LATE. The coin flip on those shots is not a poll-phase coin flip: the phases
are the same to within a millisecond and the verdicts differ anyway.

---

## 8. The alternative explanation, on the same scale

AUC for separating EXCELLENT from LATE (0.50 = no separation; < 0.50 = a larger value means more
LATE). Permutation p, 400 draws, same session × type strata.

| covariate | n | AUC (EXC vs LATE) | perm p | AUC (EXC vs EARLY) |
|---|---|---|---|---|
| **phase_frame_ms** (the poll hypothesis) | 250 | **0.513** | **0.763** | 0.495 |
| frame_age_ms (the reader-timestamp alternative) | 250 | 0.428 | 0.167 | 0.608 |
| predictor_sigma_ms | 250 | 0.465 | 0.317 | 0.499 |
| lead_ms | 250 | 0.597 | 0.115 | 0.312 |
| release_fill_pct | 250 | 0.451 | 0.130 | 0.614 |
| **resv_fill_pct** (fill when the shot was reserved) | 250 | **0.404** | **0.012** | 0.422 |
| command_eta_ms | 250 | 0.556 | 0.085 | 0.634 |
| **tip_eta_ms** (runway left at the reservation) | 250 | **0.607** | **0.002** | 0.546 |
| **frame_phase_sd_ms** (quality of the grid fit) | 250 | **0.410** | **0.012** | 0.589 |
| frame_offset_ms | 250 | 0.578 | 0.142 | 0.506 |
| frame_edges | 250 | 0.466 | 0.287 | 0.481 |
| banner_trim_ms | 250 | 0.526 | 0.382 | 0.498 |
| phase_const_ms | 250 | 0.567 | 0.105 | 0.573 |
| capture_coherence | 250 | 0.552 | 0.117 | 0.416 |
| `oracle_gap_px` *(control — the oracle should separate them)* | 188 | 0.009 | 0.002 | 0.028 |

**What the residual scatter is.** The phase carries nothing (AUC 0.513). The reader's own timestamp
age — the alternative the task asked to price — carries a little but does not clear the null on the
LATE side (AUC 0.428, p = 0.17); it is real on the EARLY side (0.608), which is consistent with a
stale frame making the engine fire *before* the tip, not after. The two covariates that do clear are
both about **arming late**: `tip_eta_ms` (how much runway the reservation had — LATE shots were
reserved with less) and `resv_fill_pct` (LATE shots were reserved at a higher fill). The third,
`frame_phase_sd_ms`, says LATE shots had a *worse-fitting grid* — the same shots whose staircase was
noisy, i.e. whose whole timing chain had less to work with. The oracle control behaves exactly as it
should, which validates the outcome variable.

Even the best of these is a weak separator (AUC 0.607 ⇒ ~11 pp of lift). **Most of the LATE /
EXCELLENT split is not explained by anything the engine currently logs** — which is the honest
statement of where the remaining ~1-frame scatter lives.

---

## 9. What would actually resolve it

The chain is measured end to end except one link, and that link is where a real poll effect could
still be hiding *smeared*:

```
tip prediction --(engine clock, us)--> command_issued_ms
   --(pipe, sd 0.155 ms, measured)--> Chiaki fork
   --(network + console input queue: NOT INSTRUMENTED)--> console pad state
   --(console poll, unknown phase)--> the graded frame
```

If the unmeasured link jitters with an sd of several ms, it smears the release phase at the console
and the null in §2 would be consistent with a real poll we simply cannot see from the PC. Three ways
to settle that, cheapest first:

1. **A deliberate dither experiment — the only causal test, and it is cheap.** Add a knob that
   offsets the fire instant by a *randomised* ±(0 or 8.33) ms per shot (bounded to half a frame,
   applied after the frame-native centre, logged as `POLL DITHER: epoch= arm= offset_ms=`), so the
   two arms differ by exactly half a poll period *at the console* regardless of what the transport
   does to the absolute phase. Grade with the banner and the oracle. From §6's arithmetic, a
   two-proportion test at 80 % power needs **40 shots per arm (80 total) to see a 30 pp difference,
   94 per arm (188) for 20 pp, 168 per arm for 15 pp.** One evening's drill answers 30 pp; a weekend
   answers 20 pp. This is the experiment to run *if the owner wants the question closed* rather than
   parked — it is a measurement knob, not a shipped feature, and it defaults off.
2. **A takion-side receipt stamp.** If the console echoes input receipt on the feedback channel, the
   fork can stamp it (the AV-clock stamp deployed 09-02 is the template: stamp after
   `av_packet_parse`, export on the logger). That converts the unmeasured link into a measured one
   and makes the phase at the console directly observable — no verdicts needed.
3. **A 60 fps press-window dump with a pad-driven on-screen marker.** Not the meter onset: §"09-17
   00:15" already measured press→onset at 667–1091 ms, about 25 frames of animation variability, so
   the meter cannot serve as a poll marker. A marker driven directly by a pad edge (controller LED /
   haptic / a menu cursor step) would date input registration to one frame.

Ranked by value: **(1) settles the question, (2) settles the mechanism, (3) is a fallback.** None of
them is worth doing before §10.

---

## 10. Side finding, and it matters more than the tracker would have

**`fire_target=frame_centre` is only weakly putting the release at the centre of the frame the same
shot recovered — it is buying roughly a quarter of the margin it was designed for.**

| group | n | mean \|phase − centre\| | within ±1 ms of centre | within ±2 ms | more than 6 ms away | sd | circular R |
|---|---|---|---|---|---|---|---|
| `fire_target=frame_centre` | 287 | **3.83 ms** | 23 % | 32 % | 25 % | 4.58 | 0.075 |
| `fire_target=instant` | 36 | 4.07 ms | 17 % | 31 % | 25 % | 4.79 | 0.078 |
| *(uniform)* | — | 4.17 ms | 12 % | 24 % | 28 % | 4.81 | 0 |
| *(a fire truly centred on this grid)* | — | ~1 ms | ~90 % | ~99 % | ~0 % | ~1–2 | ~0.9 |

There is a real excess at the centre — 49 shots in the central 1.04 ms bin against 20 expected, 23 %
within ±1 ms against 12 % by chance — so the mechanism is alive on *some* shots. But the bulk is not
concentrated: mean distance from the centre 3.83 ms where uniform gives 4.17 and true centring would
give ~1, and a quarter of the shots still land more than 6 ms away, no better than chance. Centring
is claimed on 89 % of shots and delivers ~a quarter of the ±8.3 ms margin `GameFramePhase.h` designed
it to buy (its own model: P(release lands in the intended frame) 49 % → 52 % at σ 11.7 ms, 62 % → 70 %
at σ 8.0, 76 % → 91 % at σ 5.0).

This is not an instrument artefact: §5 shows the per-shot grids agree with each other to sd 1.07 ms
across a session, and the grid at fire time has *more* step edges than the one logged at anchor
dating, so two estimates good to ~1 ms cannot disagree by sd 4.6 ms.

Two exact identities that bracket where it goes wrong, both measured on the same 287 shots:

- the **schedule** carries the offset: `command_eta_ms − (tip_eta_ms − lead_ms) = frame_offset_ms`
  for **99 %** of frame-centre shots, within 0.05 ms;
- the **fire** lands as if it did not: `command_issued_ms = anchor_ms + phase_const_ms − lead_ms`
  with median −0.006 ms and **MAD 0.030 ms** — no `frame_offset_ms` term
  (`corr(residual, frame_offset) = 0.345`, not 1.0; a slope of 0.29 where 1.0 would mean the offset
  reached the console).

Together they imply `tipAbs = anchor_ms + phase_const_ms − frame_offset_ms`, i.e. the centring offset
is being cancelled somewhere between the tip the grid was applied to and the anchor-dated tip the
fire actually used, on the ~77 % of shots that do not land near the centre. **One log line settles
it:** emit the grid phase and the realised distance to the intended centre *at the fire*, not only at
anchor dating — e.g. append `fire_grid_phase_ms=` and `fire_centre_delta_ms=` to the `Release submit:`
line, and the next graded session says immediately which shots were centred and which were not. If
the delta is large on most shots, frame-native firing has been mostly inert since 09-15 and
recovering it is worth 3–15 pp on the header's own model — which is more than the poll tracker was
ever going to be worth, and it needs no new instrument.

*(Note this does not weaken §2: a release phase spread uniformly over the frame is exactly the
coverage the test needed. The hypothesis was tested at full strength.)*

---

## 11. Reproducing this

```powershell
# 1. extract (read-only; writes D:\NexusVision\poll_phase\shots.csv)
.\.venv\Scripts\python.exe tools\timing\poll_phase_probe.py `
    --logs "<scratchpad>\cur_session*.log" --out D:\NexusVision\poll_phase

# 2. the fold  -> D:\NexusVision\poll_phase\fold.md, fold.json
.\.venv\Scripts\python.exe tools\timing\poll_phase_fold_v2.py --perms 4000

# 3. stability, drift, power, alternatives -> stability.md
.\.venv\Scripts\python.exe tools\timing\poll_phase_stability.py

# 4. per-session / split-half / period scan / convergence -> persession.md
.\.venv\Scripts\python.exe tools\timing\poll_phase_persession.py
```

New tools, all read-only, none imported by the engine or the reader:
`tools/timing/poll_phase_probe.py`, `poll_phase_fold_v2.py`, `poll_phase_stability.py`,
`poll_phase_persession.py`. Outputs in `D:\NexusVision\poll_phase\`
(`shots.csv`, `parse_summary.json`, `fold.md`, `fold.json`, `stability.md`, `persession.md`).

## 12. Standing conclusions this replaces or confirms

- **Confirms** 2026-09-03 ("POLL PHASE NOT DETECTED", n = 130, fork AV-clock stamps, landing fill as
  the outcome) on a better instrument, a better outcome and 2.2× the n. Phase locking stays closed.
- **Corrects** the 09-15 forensics line "standstill LATEs ↔ nothing (poll-phase coin flip on a
  ~1-frame window)": the coin flip is real, the poll-phase explanation for it is not (§7).
- **Corrects** the 09-03 note that the console runs at 59.94 Hz: the *stream* does; the game's
  render/grade clock measures **60.003 Hz** on our clock, drifting −0.05 ms/s (§5).
- **Adds** an open engine question with a one-line diagnostic: frame-centred firing is not
  concentrating the release (§10).
