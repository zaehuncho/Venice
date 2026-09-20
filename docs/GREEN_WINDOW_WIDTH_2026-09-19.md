# The green window's WIDTH, not the shot type, is what separates a make from a miss
2026-09-19. Source: `logs/orion_native.log.1` + `logs/orion_native.log`, every graded release from
2026-09-18T00:14Z to 2026-09-19T02:48Z (281 releases with a banner verdict, 252 with a landing line).
Instruments: the game's own feedback panel (`BANNER VERDICT:`), the reader's landing measurement
(`Release landing: ... green_start= green_end=`) and the retraction oracle (`RELEASE ORACLE: gap_px=`).

## 1. Window width predicts the verdict, and it predicts it hard

| type | window p25 / p50 / p75 (pp) |
|---|---|
| Standstill | 2.63 / 3.62 / 4.83 |
| Fade | 1.52 / 2.17 / 3.66 |

| type | slice | n | EXC | LATE | EARLY | green |
|---|---|---|---|---|---|---|
| Standstill | narrow (≤ p25) | 44 | 17 | 16 | 11 | **39 %** |
| Standstill | mid | 87 | 71 | 14 | 2 | **82 %** |
| Standstill | wide (≥ p75) | 44 | 32 | 11 | 1 | 73 % |
| Fade | narrow (≤ p25) | 20 | 8 | 6 | 6 | **40 %** |
| Fade | mid | 37 | 21 | 7 | 9 | 57 % |
| Fade | wide (≥ p75) | 19 | 17 | 0 | 2 | **89 %** |

On a narrow window the misses are SYMMETRIC (16 late / 11 early on standstills, 6 / 6 on fades), so
this is variance against a smaller target, **not** a bias a lead offset could remove. The window
shrinks from BELOW: on narrow-window standstills `green_start` sits at 93.6 vs 91.4 on the rest while
`green_end` is unchanged (95.4 vs 95.6), which is consistent with the shipped aim-high doctrine.

## 2. Fades are NOT timed worse than standstills — the controlled comparison

Landing precision, independent of the window (oracle |miss|, px):

| type | n | p25 / p50 / p75 / p90 | ≤ 3 px |
|---|---|---|---|
| Standstill | 113 | 1.0 / 2.0 / 5.0 / 9.0 | 63 % |
| Fade | 74 | 1.0 / 3.0 / 4.0 / 8.0 | 64 % |

Green rate at MATCHED window width:

| window width | Standstill | Fade |
|---|---|---|
| 0 – 2.0 pp | 32 % (n=25) | 42 % (n=36) |
| 2.0 – 3.0 pp | 68 % (n=38) | 67 % (n=18) |
| 3.0 – 4.5 pp | 82 % (n=60) | 88 % (n=8) |
| ≥ 4.5 pp | 71 % (n=52) | 86 % (n=14) |

**The fade/standstill gap disappears once width is held constant.** The bot does not time fades worse;
the fade's green window is ~40 % smaller (2.17 vs 3.62 pp p50), and half of all fades fall in the
0-2.0 pp bucket where nothing scores well. This retires "fades need work as a timing problem":
the lever for fades is the same as for everything else — reduce |miss| — not a fade-specific offset.

## 3. The window is knowable ~50 ms BEFORE the release

`Release attribution: ... greenConfirmed= greenWidth= greenConfirmMs=` shows the engine already
confirms the window on **192 of 281 shots (68 %)**, a median **50 ms before** the release, and the
width it reports then agrees with the width measured at landing (difference p10/p50/p90 =
−1.68 / −0.07 / +1.01 pp, n=190). Shots where the window was confirmed at fire time grade 69 % green
vs 58 % when it was not. So an adaptive policy is *possible*. It is not yet *justified*: because the
narrow-window misses are symmetric, there is no offset to apply — only a precision gain would help,
and that is what the pickup-stall work targets.

## 4. Caveats
- The ≥ 4.5 pp bucket grades WORSE than 3.0-4.5 on standstills (71 % vs 82 %). Very wide readings are
  suspect: the maximum observed `greenWidth` at fire is 16.5 pp and one EXCELLENT landing reported
  `green=[71.5, 85.3]`. Some wide readings are misreads, so do not treat width as a clean covariate
  above ~5 pp without re-validating the reader's green cap on those frames.
- The oracle gap is a |miss| MAGNITUDE, not a signed millisecond ruler (see
  `docs/STANDSTILL_LATES_2026-09-19.md` §"Correction": slope +0.0047 px/ms, r=0.048 within EXCELLENT).
- Widths here are measured at landing except in §3; both agree, which is why §1 uses the landing value.
