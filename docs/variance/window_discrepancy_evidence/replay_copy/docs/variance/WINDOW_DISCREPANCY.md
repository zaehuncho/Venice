needs changes

# Visible green window versus fitted EXCELLENT band

**2026-09-22, pre-sweep, offline review.** The discrepancy is real in the logged visual measurements versus the *normalised observational fit*. It is **not yet an identified sixfold difference between two physical acceptance windows**. The visible proxy is about **15 ms**, not 94 ms; the old model's millisecond interpretation needs the independent randomised-delivery calibration. No source, launcher, build, app, capture, or live endpoint was changed or started. The new analysis and amended plan are ready for review; this is not a runtime or shipping approval.

## 1. What was measured, on which shots

Inputs were the 57 JSONL files under `D:/NexusVision/shot_records`, plus both retained native logs. There are **2,974 unique physical records**, no malformed/conflicting record identities in this snapshot, and **816 selected historical online shots / 27 sessions**: 548 EXCELLENT, 170 LATE, 98 EARLY. The newer/older split is **471/345**. The latest record press is **2026-09-22 03:25:53.869 UTC**. No retained native line has an offset-sweep draw, sweep-armed marker, or logged A/B assignment; this is not new sweep data.

Frozen corpus digest (sorted filename, NUL, then binary file SHA-256):

```text
fb9403a7064315c90ac4a50474d853148a2436ada76da91dd588e73346de7224
```

Log SHA-256:

```text
orion_native.log.1  BEA62C5EB199642086C69EC8D4B7AEA162AB2C9A9D2D5182866F959E015373D3
orion_native.log    C56956943701329E2974D129279DCB6053A9D6DAAB50D2866BBF120D9F6010E0
```

### Exact identity join

1. Keep `(shot_record.session, integer epoch)` as the record key. JSONL `seq` is **not** native release seq. Deduplicate identical records/forwarded diagnostics; quarantine conflicting or ambiguous identities.
2. Merge both logs in UTC order; partition at observed physical-epoch/native-release-counter resets. There are **12 partitions**, not two independent files/processes. Match `Release onsetff` or the older `Release delivery identity` to exactly one same-epoch record within its press-to-close interval and **±150 ms of record release time**. The two identities must agree on process/attempt/native seq.
3. Match attribution/timing/issued/schedule lines by `(process,native seq)` in that same release interval. For **descriptive historical widths only**, `Release delivery identity` is an admissible fallback when `Release onsetff` did not yet exist. It does not invent an FF arm or applied offset.
4. Match delayed `Release landing` by that native process/seq within **0–5,000 ms after release**. Landing often arrives after the banner closed the JSONL record; requiring it inside press-to-close would unnecessarily lose valid widths. Never use the landing endpoints as a pre-fire predictor.

The logs contain 479 release identities and 474 landing lines. Across all records, **479** releases join uniquely: **423** legacy identity and **56** `Release onsetff`; no contradictory release identities were found. The 816-shot analysis cohort contains **284** positive attribution widths and **288** qualified landing measurements. Unmatched records are missing, not zero-width shots. Width availability covers **eight sessions**, not the entire 27-session population.

### Millisecond conversion is a calibrated-curve proxy

The current source is [AutomationEngine.cpp](C:/Users/aaron/Desktop/NexusVision/native_orion/src/AutomationEngine.cpp), around lines **20253–20307**, and [AutomationEngine.h](C:/Users/aaron/Desktop/NexusVision/native_orion/src/AutomationEngine.h), around **1342–1344, 1400–1403, 6626–6638**. The header lines in the question have moved. The convex curve's fill/time knots are:

```text
fill pp: 20   25    30    35     40     50    60    70    80    90    100
time ms:  0  29.42 58.30 86.22 112.58 163.7 211.4 257.4 302.4 344.4 385.5
```

Use the piecewise-linear integral **`T(end)-T(start)`**. At 90–100%, it is **4.11 ms per percentage point**, or 0.2433 pp/ms. The 90–100 segment is explicitly **extrapolated**, not directly measured per shot. For attribution's width-only field, use `4.11 * greenWidth`; endpoint-bearing landing/issued diagnostics use the integral. The inspected launcher sets time scale **1.0**; the five logged rate-stretch events are also 1.000. This does not independently certify every shot's actual top speed. The report therefore calls these **curve-equivalent milliseconds**, not video-observed time spent inside green.

Do **not** divide by `vel_at_rel`: the controller releases early, around 35–40% fill, to compensate actuation delay. That slower sampled velocity is not the near-top speed. Doing so would give a median **20.38 ms**, rather than **15.08 ms**, for the same landing rows. That mistake still would not explain a 94 ms band.

### Per-shot distributions in the 816-shot historical cohort

Pre-confirmed means `greenConfirmed=1`, positive width, valid `greenConfirmMs`, and `appearToRelMs-greenConfirmMs >= 20`. Plausible width is the already registered **0.5–5.0 pp** range. A landing needs `graded=1`, at least three green observations, and valid ordered endpoints in 0–100. No grade category was preferentially selected.

| Measurement | n | Median width, pp | Median, ms | IQR, ms | 10th–90th, ms |
|---|---:|---:|---:|---:|---:|
| Pre-confirmed plausible release snapshot | **265** | **3.70** | **15.207** | 15.207–16.029 | 10.439–18.495 |
| Plausible settled landing endpoints | **207** | **3.67** | **15.084** | 12.433–15.762 | 7.505–16.892 |
| All qualified landing endpoints, no 5-pp cap | **288** | 3.76 | **15.454** | 14.251–22.369 | 8.195–22.852 |
| Issued window endpoints, descriptive full range | **286** | — | **15.207** | 14.796–18.495 | 11.097–18.906 |

The plausible pre/landing means are **14.748 / 13.700 ms**, with SD **3.351 / 3.824 ms**. The unfiltered landing maximum is **29.984 ms**. Removing the width cap does not make the median remotely approach 94 ms. In the broader all-online graded Standstill set (1,085 shots / 51 sessions), plausible pre and landing medians are **15.207 / 15.022 ms**, respectively. This is not an artefact of the historical onset/meter-tail filter.

There are **194** plausible paired pre/landing widths. Median signed difference is just +0.01 pp, but median **absolute** difference is **0.26 pp**, p95 **1.73 pp**. That is not proof of first-confirmed versus release stability: the second measurement is later, and its detector differs. The pre-confirmation lead median is **68 ms**, not a width-latch timestamp. Also, **106/265** plausible pre widths are exactly 3.7 pp and their IQR is only **0.20 pp**, below the registered width-interaction gate of **1.0 pp**. The current corpus offers little independent width variation for an actionable aim rule.

Every joined shot, including missing fields, and source log line references are in [shots.csv](C:/Users/aaron/Desktop/NexusVision/docs/variance/window_discrepancy_evidence/dry_run_final/shots.csv). Distributions and per-session counts are in [summary.json](C:/Users/aaron/Desktop/NexusVision/docs/variance/window_discrepancy_evidence/dry_run_final/summary.json).

## 2. Is the gap physical, or a probit artefact?

**The measurement gap is robust; its physical interpretation is unresolved.** Relative to the requested 94.39 ms benchmark, the visible pre median is **6.21× smaller**. Plausible near-top rates 0.22–0.27 pp/ms would price 3.7 pp at 13.7–16.8 ms. A 94.39 ms traversal of 3.7 pp would require only 0.0392 pp/ms, about sixfold slower than the frozen curve. This is worth independent frame timing, not an assumed correction factor.

### A. The millisecond ruler was an assumption in the passive fit

The old normalised model is `mu = meter_c + beta*onset_c`, with free cutpoints and sigma. Conditional on fixing the coefficient of `meter_c` to one, those parameters are statistically identifiable. But **that coefficient is not a measured intervention response**: meter-relative timing is generated by the controller, detector, and feedback loop, not an independent randomised displacement.

If instead its physical gain is unknown, then multiplying **all** timing coefficients, cutpoints, and sigma by a positive constant leaves the probabilities unchanged. For example, multiply the old fit by **0.161108**: `W=15.207`, `sigma=7.015`, and a 0.161108 effect per observed meter-relative millisecond give exactly the same verdict probabilities. This is a demonstration of the missing calibration, **not evidence that the actual gain equals 0.161**. The earlier reported 0.29 delivery slope is another reason not to assume one-to-one actuation.

Consequently the prior ~43.5 ms is neither a directly observed noise SD nor a proven network component. The preregistration formerly requested a free delivered-offset coefficient *and* free sigma/cuts as a sensitivity. That is an exact scale degeneracy, now corrected before data: coefficient one after the first stage, with **fixed 0.8 and 1.2** calibration sensitivities, never a jointly free scale CI.

### B. Pooling/context and visible-width variation matter, but do not settle it

The new draft refits the historical model on the **same visible-data shots**, retaining the original session medians rather than recentering the subset:

| Observational cohort | n / sessions | beta | Fitted W | Fitted sigma | Normal-model peak |
|---|---:|---:|---:|---:|---:|
| Exact historical selection | 816 / 27 | **0.4460** | **95.824 ms** | **44.216 ms** | 72.145% |
| Pre-confirmed plausible widths | 265 / 8 | 0.3662 | **71.932 ms** | 35.026 ms | 69.551% |
| Plausible landing widths | 207 / 8 | 0.2287 | **59.872 ms** | 29.949 ms | 68.248% |

The new full fit is **not numerically identical** to the earlier 94.39/43.54 reproduction: a separately parameterised Nelder–Mead fit agrees with the new multistart MLE to <0.0001 ms; NLL is **637.258115**, versus **637.265182** at the quoted rounded old parameters. The same digest/counts make this a minor fit/selection-reproduction discrepancy, not new data. Its cause cannot be assigned precisely without the old executable analysis. It changes neither the ~72.2% model peak nor the width-discrepancy conclusion. The frozen predictions below deliberately retain 94.39/43.54 as the requested benchmark.

Matching the available-width population reduces the apparent ratio from about six to **four–five**, but does not collapse it to one. That demonstrates context/availability sensitivity, **not** that varying visual widths caused the reduction. Width, detector error, session drift, non-normal errors and historical control actions can all affect an effective pooled band. The sparse, concentrated pre-width distribution is not a sufficient test of their separate causes.

A truly narrow 12–20 ms band with independent **43.54 ms physical Gaussian noise** has a best-centred EXCELLENT rate only **10.96–18.17%**, nowhere near 67%. To preserve a ~72% peak with narrow widths, physical sigma would have to be about **5.5–9.2 ms**, or the additive/noise/selection assumptions would have to change. One cannot simultaneously keep the narrow physical band, the old physical sigma, and the old high success rate in that model.

### C. A distinct grading window remains a hypothesis

The displayed green interval could differ from the effective grade-versus-command interval, or its time conversion/measurement could be wrong. Current logs observe visual detections and delayed banner labels, not an authoritative grade-boundary timestamp. The proper result is **“15 ms visual proxy versus ~60–96 ms normalised observational band; physical explanation unproven”**, not “the server definitely has a 94 ms window.”

## 3. What the offset sweep should show

These exact competing curves and their assumptions are now frozen in [preregistration sections 7–8](C:/Users/aaron/Desktop/NexusVision/docs/variance/PREREGISTRATION_OFFSET_SWEEP.md). Predictions assume a 1:1 independent additive release displacement; a failed first-stage gate invalidates both physical comparisons.

### Shape at equal illustrative peak, offsets relative to the best centre

| W / sigma, ms | EXCELLENT at -25 | at -12.5 | at centre | at +12.5 | at +25 |
|---|---:|---:|---:|---:|---:|
| **94.39 / 43.54** | 64.63% | 70.21% | 72.16% | 70.21% | 64.63% |
| **12 / 5.54** | 0.03% | 11.97% | 72.16% | 11.97% | 0.03% |
| **16 / 7.38** | 1.06% | 26.83% | 72.16% | 26.83% | 1.06% |
| **20 / 9.23** | 5.19% | 38.58% | 72.16% | 38.58% | 5.19% |

At the historical illustrative centre **c=-7.415 ms**, with arm shift delta=0, the *actual* D=-25/0/+25 predictions are:

| Model | D | EARLY | EXCELLENT | LATE |
|---|---:|---:|---:|---:|
| Broad | -25 | 24.823% | **68.337%** | 6.840% |
| Broad | 0 | 10.488% | **71.467%** | 18.045% |
| Broad | +25 | 3.374% | **59.913%** | 36.713% |
| Narrow 16 | -25 | 90.298% | **9.676%** | 0.026% |
| Narrow 16 | 0 | 1.837% | **51.322%** | 46.841% |
| Narrow 16 | +25 | ~0% | **0.047%** | 99.953% |

The unknown centre and FF shift translate these curves. A 72% peak is used for an apples-to-apples illustration, not imposed on the new fit. Under a uniform -25..25 sweep with c=-7.415, the means are **68.97% broad**, versus **23.93 / 31.33 / 37.83%** for the 12/16/20 ms examples. EXCELLENT is not monotone; both models have zero success derivative at their own peak, whereas the EARLY/LATE tails and shoulders discriminate them.

### Why a mixture of narrow windows does not explain high sweep success

For a shot with interval width W_i, integrate its success indicator over all possible additive offsets, then average over arbitrary noise, centres and widths. The area under the success curve is **E[W_i]**. Thus independent uniform offsets across 50 ms imply **average success <= E[W_i]/50**: at most **24–40%** for 12–20 ms, or **41.1%** for the 5-pp/20.55-ms limit. Normality is unnecessary. A mixture can blur the shoulders but does not manufacture area.

This bound requires the interval/noise to be unaffected by assignment and offsets to be delivered 1:1. With a noncompliant fraction epsilon, the conservative bound becomes `E[W_i]/50 + epsilon`; with selected graded survivors or an unknown/nonuniform delivery distribution, the ideal bound is not directly applicable. Include finite-sample uncertainty; a 100-shot percentage is not a population truth. This is a prespecified cross-check, not an extra shipping test.

**Interpretation rules:** good fidelity plus broad, nearly flat high success across the range and a width profile wholly above 20.55 ms contradicts the narrow proxy-calibrated fixed-band model. Good fidelity plus steep EARLY-to-LATE transitions and a narrow band contradicts a physical 94 ms band. Broad/open profiles, failed goodness-of-fit, arm/context dependence, visual calibration failure, or delivery attenuation leave the explanation unresolved. **100 shots may distinguish these extreme shapes; 100 is still too few for useful separate broad-band/sigma estimates or a +3–5-point FF claim.** At n=200 the preregistered broad-model SEs remain roughly 24 ms for width and 11 ms for sigma. Do not call non-significance a confirmed ceiling.

## 4. Executable draft and review coverage

[sweep_analysis.py](C:/Users/aaron/Desktop/NexusVision/tools/timing/sweep_analysis.py) implements:

- Integer-safe epoch/process/native-seq/attempt joins, final carried D/F rather than refinement guesses, A=unknown rather than arm 0 when missing; per-shot provenance and attempt audit.
- Explicit first-100/200 native physical presses after a declared marker, complete-record ledger/protocol checks, all-attempt arm and attrition denominators; local H-on-Z/D HC3 equivalence and block-20 bootstrap. A 0.29 fixture fails; a 1.0 fixture passes.
- Gated ordered-probit MLE, band centre/width/sigma/peak profiles, boundary/open-profile reporting and bootstrap fallback; registered five-bin 2,000-refit goodness-of-fit; fixed-scale and onset/order/arm-interaction sensitivities.
- Raw assigned-policy FF risk difference, Newcombe-Wilson interval and exact conditional arm permutation; missing banners count as non-success, not EARLY; graded-only comparison, adjusted g-computation with 10,000 epoch bootstraps, warm-reference and arm-onset analyses.
- Width eligibility/stability audit, 3-df centre/width/slope interaction, profiles, quartile verdict curves and derivatives, quartile delivery checks, full-positive-width sensitivity. No frame audit means exploratory, never a pre-decision controller claim.
- Curve-integrated per-shot visible widths, historical matched-population fits and both competing prediction tables. Legacy calculations are separate from the randomised primary estimator.

The later **multi-session** network-feature and production-equivalent no-sweep/adaptive-policy validation are deliberately **not represented as completed analyses** by this single-session script. Its output states that those gates remain external work; it never authorises shipping or declares a ceiling just because an in-sample fit succeeds. A passive packet timestamp is not treated as a verified command-bearing datagram. Those limitations are part of the amended plan, not missing values silently filled with zeros.

### Reproduce the legacy dry run

From `C:/Users/aaron/Desktop/NexusVision` (choose a *new* output directory if rerunning):

```powershell
python -B tools/timing/sweep_analysis.py --dry-run --records-dir D:/NexusVision/shot_records --logs logs/orion_native.log.1 logs/orion_native.log --out-dir docs/variance/window_discrepancy_evidence/dry_run_final
python -B tools/timing/test_sweep_analysis.py
```

The dry run exits **0** as a completed *diagnostic*, with **`records=2974`, `assigned=0`, `status=PRIMARY_SUPPRESSED`, `fidelity_pass=False`, `primary_fit=False`**. The existing corpus has no randomised draw/arm evidence; no invented control observations enter a primary fit. `summary.json` contains the failure funnel, all legacy widths/fits, source hashes and full prediction tables. Output directories are immutable-by-convention: the script refuses an existing destination and detects source input changes during a run.

**22 offline tests pass.** The tests exercise uint64 identity, counter resets/rotation deduplication, ambiguous epochs, landing after close, missing assignments, final versus refinement values, multiple arm tokens, nonzero in-tick contradictions, record corruption, exact FF inference, scale invariance, broad/narrow likelihood recovery, fidelity rejection at slope 0.29, missing-banner policy denominators, first-press rather than surviving-grade sampling, profile inversion and the primary/width paths. A separate **synthetic-only** full-count analysis completed **2,000/2,000** goodness-of-fit refits and **10,000/10,000** FF bootstrap resamples; its fidelity gate passed and its unaudited width result stayed `exploratory_only`. It is not sweep evidence. The first full run found a NumPy Boolean JSON-serialization failure; explicit Boolean conversion and an artifact-serialization regression check fixed it before the passing rerun. Exact command/output/exit-status evidence and rollback verification are in [VERIFICATION.txt](C:/Users/aaron/Desktop/NexusVision/docs/variance/window_discrepancy_evidence/VERIFICATION.txt).

### Real trial interface — acquisition by the owner, not executed here

```powershell
python -B tools/timing/sweep_analysis.py --records-dir D:/NexusVision/shot_records --logs logs/orion_native.log.1 logs/orion_native.log --session SESSION_ID --start-utc START_WITH_UTC_OFFSET --block 100 --protocol-json docs/variance/RUN_PROTOCOL.json --capture-dir D:/NexusVision/netcap/CAPTURE_ID --out-dir docs/variance/SWEEP_BLOCK_100
```

The explicit protocol JSON must contain actual build/settings hashes, same-spot/open-online attestations, and exact offset/FF specifications listed in preregistration section 8. An independent frame audit is optional input via `--width-audit`, but required for confirmatory width interpretation. Missing protocol, missing capture or failed fidelity means **exit 2 / primary suppressed**, not “zero effect.” Extend to the already planned first 200 presses independently of interim grade results. No historical 200/60-ms outcome-tail filter is reused for the randomised sweep.

## 5. Findings and next decision

| Finding | Affected object / impact | Required next verification |
|---|---|---|
| ~15 ms visible proxy vs ~60–96 ms observational band | Physical interpretation of the grading-clock model; treating sigma as measured timing noise is premature | Calibrated randomised delivered-offset curve, plus independently timed visual top crossing |
| Free D coefficient with free cuts/sigma is exactly unidentified | Old preregistration sensitivity clause; false confidence intervals | Corrected before data to fixed coefficient and fixed 0.8/1.2 sensitivity; scale-invariance test verifies the issue |
| Current pre-width IQR 0.20 pp and release snapshot is not a latch | Width-conditioned aim; current observations do not establish usable pre-decision variation | Label-blinded first-confirmed frame audit, >=1-pp IQR, quartile first stages, then independent-session adaptive gain |
| Fresh exact passive MLE is 95.824/44.216, not exactly 94.39/43.54 | Small reproduction discrepancy; not a new causal result or material change in ceiling | Retain exact current outputs and old constants as approximate benchmark; do not splice parameter sets |
| No existing draw/arm/capture evidence for this experiment | Primary sweep/FF/noise claim would be fabricated if run as real data | Owner's preregistered acquisition, complete first-press ledger, capture collected, fidelity gate before grade inference |

**Next:** run the pre-registered measurement block, not a source tuning change. The discrepancy is a good reason to run it: it can falsify a large physical-width interpretation even when it is underpowered for a small FF improvement. Keep the later independent-session shipping/ceiling rules intact.
