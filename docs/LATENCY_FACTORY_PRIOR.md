# The route latency factory prior

`models/latency_factory_prior.json` is the zero-setup seed for end-to-end actuation latency. It is
the value the engine actuates on before this machine has produced any marker-backed label of its
own, so it is a live actuation constant, not documentation.

Rebuild it with:

```
./.venv/Scripts/python.exe tools/timing/build_latency_factory_prior.py --write
./.venv/Scripts/python.exe tools/timing/build_latency_factory_prior.py --check   # exit 1 on drift
```

## Why this file was rewritten

The previous version of this artifact could not be defended. Verified 2026-08-02:

* It was **untracked by git**, and the value `241.4` appeared nowhere in `HEAD`. No generator
  existed. Yet the file **is packaged into the shipped sidecar bundle**
  (`tools/sidecar_bundle_manifest.py`, `scripts/build_orion_sidecar.ps1`), so releases embedded an
  actuation constant with no source of truth and no review trail.
* `241.4` came from **one** 2-sample controlled calibration on capture-card + PIPE at
  `2026-08-02T13:14:31Z` (`logs/orion_native.log:27638`: `fixed_ms=233.1 sd_ms=4.0 n=2
  validated=1`), plus `TICK_WAIT_EXPECT_MS = 8.3` (`latency_estimator.py:81`).
  `233.1 + 8.3 = 241.4` exactly.
* That single number was hand-copied into **all four** route profiles. Only `sd_ms` was varied
  (24 / 32 / 36 / 42), and those four values have no traceable derivation whatsoever.
* The file therefore read as four independent route measurements. It was one measurement, copied.

### The evidence actually available

Re-derived independently from `logs/orion_native.log` (and `logs/orion_native.log.1`, which
contains zero latency observations). All 8 accepted labels in the entire log corpus:

| UTC | total_ms | video | controller |
|---|---|---|---|
| 2026-08-02T10:44:29Z | 231.9 | capture_card | pipe |
| 2026-08-02T10:44:36Z | 254.0 | capture_card | pipe |
| 2026-08-02T13:14:04Z | 250.3 | capture_card | pipe |
| 2026-08-02T13:14:11Z | 250.6 | capture_card | pipe |
| 2026-08-02T13:14:31Z | 232.5 | capture_card | pipe |
| 2026-08-03T00:15:15Z | 214.1 | capture_card | pipe |
| 2026-08-03T00:16:50Z | 188.3 | capture_card | pipe |
| 2026-08-03T02:48:52Z | 193.7 | capture_card | pipe |

Two facts follow, and they are the whole basis of this artifact:

1. **Only one route has ever been measured.** Every accepted label is capture-card + PIPE.
   `decoder-*` and `*-vigem` have never been measured even once. Corroboration: the log contains
   exactly **one** estimator route-scope digest (`scope=17f246f9e138`) across its entire span, the
   only controller route token ever proposed, acknowledged or rejected is `route=pipe` (423
   occurrences, zero `vigem`), and the pre-encryption pipe input hook is enabled 21 times and
   never torn down. The `Controller route: RawInput ... -> ViGEm/XUSB -> Chiaki` lines describe
   local RawInput mirroring and are *not* the delivery route — conflating the two would wrongly
   book PIPE evidence as ViGEm evidence. A short `tier=decoder` excursion exists at 10:50 but no
   label falls inside it, which is why the builder attributes routes per-label rather than
   per-log.
2. **There is a real regime shift inside that one route.** Splitting the series at its single
   largest time gap (~11h) reproduces the sessions exactly:
   * 2026-08-02 daytime: {231.9, 254.0, 250.3, 250.6, 232.5}, mean **243.86**
   * 2026-08-03 night: {214.1, 188.3, 193.7}, mean **198.70**

   Welch t ≈ 4.9. So `241.4` faithfully measured a regime that no longer holds. This is precisely
   what stranded the 2026-08-03T02:48 live session: 13 physical shot epochs, 2 releases, 8
   `live_tip_deadline_missed` aborts.

## How the current values are derived

| profile | mean_ms | sd_ms | evidence_n |
|---|---|---|---|
| capture-card-pipe | 218.5 | 28.7 | 8 |
| decoder-pipe | 218.5 | 34.5 | 0 |
| capture-card-vigem | 226.8 | 34.5 | 0 |
| decoder-vigem | 226.8 | 39.5 | 0 |

**capture-card-pipe** — the only fitted profile. Labels are weighted by recency with a 12h
half-life (chosen to be comparable to the ~11h regime gap: the newer regime dominates without
discarding the older one). That gives a weighted mean of **218.5 ms** and a Kish effective sample
size of 6.97. The sd is the posterior-*predictive* width `sqrt(var * (1 + 1/n_eff))` = 28.68 ms —
the spread of the observations plus the uncertainty in the mean itself, which at n_eff ≈ 7 is not
negligible and must not be hidden. It is floored by a regime-straddle term (25.32 ms, the distance
from the weighted mean to the furthest regime mean) so that a session drawn entirely from either
observed regime is not treated as an outlier. `max(28.68, 25.32) = 28.7`.

**The three unmeasured profiles** keep that mean and widen the sd by explicit, declared ignorance
terms. Each is a symmetric uniform on `[-a, +a]`, contributing `a/sqrt(3)`, centred on zero
because the *sign* of the differential is unknown too:

* *video* (`±2` frame periods @60Hz = ±33.3 ms → 19.2 ms): HDMI capture vs. network decode differ
  by whole frame periods, but which is slower depends on encoder queue depth and card buffering.
* *controller* (`±4` poll periods = ±33.2 ms → 19.2 ms): an emulated ViGEm pad traverses the
  Windows HID/USB stack before reaching the same transport PIPE writes to directly.

`capture-card-vigem` and `decoder-vigem` additionally shift the mean by **exactly one console
input-poll period** (`TICK_WAIT_EXPECT_MS = 8.3`), the only physically-motivated ViGEm delta: a
press landing mid-tick waits for the next sample. That is a point estimate, not a measurement,
which is why the accompanying sd is wide.

Variances add in quadrature, so e.g. `decoder-vigem = sqrt(28.68² + 19.25² + 19.17²) = 39.5`.

## Guard rails

* **Hard bounds** `15 ≤ mean_ms ≤ 500` and `6 ≤ sd_ms ≤ 100` are enforced independently by
  `latency_estimator.py`, `tools/sidecar_bundle_manifest.py`, and
  `native_orion/src/AutomationEngine.cpp`. The builder re-asserts them before writing.
* **Do not rename** `schema`, `model_id`, or the four profile `name`s.
  `AutomationEngine.cpp::factoryLatencyPriorMatchesRoute` parses the published source string
  `venice-e2e-route-prior:<name>` by prefix and by `-pipe` / `-vigem` suffix, and
  `_load_factory_prior` selects by `scope_contains`. Renaming silently disables the prior.
* The added provenance fields (`evidence_n`, `evidence_route`, `evidence_window_utc`,
  `derivation`, top-level `generated_by` / `generated_from`) are ignored gracefully by every
  consumer — all of them read known keys via `.get()` and none reject unknown ones. Verified.
* `model_version` is now content-derived (`sha256-<12>`) over the numerics the runtime consumes,
  so a hand-edit is detectable; `--check` exits 1 on any drift from a fresh regeneration.

## Effect on the two live-session defects

The combined-sigma gate is
`sqrt(predictorSigma² + leadSd² + tickSigma²) ≤ 65 ms` (`AutomationEngine.cpp:8474`, cap at
`AutomationEngine.h:737`), with predictor sigma growing 0.20 ms per ms of prediction horizon and
`tickSigma` defaulting to 4.8 ms. Validity therefore requires a bounded horizon, and scheduling
requires horizon ≥ the actuation lead, so the feasibility window is `H_max − lead`:

| | lead | seed sd | H_max | feasibility window |
|---|---|---|---|---|
| before | 241.4 | 24.0 | 301.1 | 59.7 ms |
| after | 218.5 | 28.7 | 290.6 | **72.1 ms** |

The wider sd does cost ~10 ms of usable horizon, but the lower, evidence-backed lead returns more
than twice that, so the window grows by ~12 ms.

The seed also interacts with the authority-move allowance the lead engineer added to
`latency_estimator.py` (`k(n)·factory_sd`, `k(n) = 3.0·(1 − e^(−n/2))`). At the first label
`k(1)·sd = 33.9 ms`, so a single 193.7 ms observation pulls the published lead all the way to it
(`218.5 − 33.9 = 184.6 < 193.7`). Under the old 241.4/24.0 seed the same label could only reach
213.1 ms and stayed 19 ms high — enough to keep missing deadlines, and an aborted shot emits no
release marker, so label #2 could never arrive.

Note also that `autonomousTipMaxCombinedSigmaMs = 65.0` was explicitly sized against the *old*
decoder+ViGEm sd of 42.0. The new widest sd is 39.5, so that ceiling's sizing assumption is
strictly safer than before.
