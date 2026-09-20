# Timing-consistency lane — 2026-09-01 batch report

Lane: estimator / phase-anchor math / deadline construction / scheduler variance / release
consistency. Codex's lane (overlay, capture, Chiaki/PIPE transport, framedump, detector worker)
was not touched. Target: measured 95%+ open-shot tip timing, human banners as the authority.

## TL;DR

1. **The current-log batch has a consistent early displacement plus substantial post-command /
   instrument spread.** Every session/type median is below the measured tip, but near-tip
   deceleration prevents a validated percentage-point-to-millisecond conversion. Command-phase
   sensitivity is small for fades and non-zero for Standstill; this is not an estimator-only failure.
2. **One engine defect fixed in this lane:** the Tip Timing "learner" measures anchor→**freeze**,
   and the freeze rides the release, so it measures where the bot *landed*, not the tip. Its
   legacy auto-unlock and its "Reset Tip Timing" advice could have moved the target in the wrong
   direction. Auto-unlock is now opt-in and upgrade-safe; the divergence line now states the
   instrument-estimated landing shift and recommends a controlled correction A/B.
3. **No timing constant was moved in code.** Shot Lead 300 → 285 moves the PC command exactly
   15 ms later, but the landing response must be measured. The remaining spread can include
   estimator sensitivity, transport/registration cadence, and freeze-position measurement.
4. **The log cannot grade greens to better than ±2 pp.** `green_start` per-shot spread is as large
   as the landing spread. Human-counted banners are mandatory for any claim.

## Codex independent audit and integration corrections

The directional result survives review; several categorical and quantitative claims below do
not. Treat this section as superseding the exact-magnitude and causal language in the original
lane report:

- The 336 releases are the **15:38-20:08Z current-log batch**, not the complete 2026-09-01 day.
  `orion_native.log.1` contains another 230 landings (219 gradeable) from the same date.
- Only 309/336 releases have a usable log window. Their signed fill result is strong: median
  `peak-green_end=-4.01 pp`, 94.5% below the measured tip, and every session/type median negative.
  This establishes the direction (later), not an exact millisecond correction.
- `e_tip_ms` and the shift sweep used `vel_at_rel`, sampled near command time, as though it also
  described the decelerating meter top. Current traces show strong nonlinearity near the tip.
  Therefore `-20 ms`, `+15 ms -> +2.8 pp`, and the predicted green-rate gain are model-dependent,
  not validated measurements. The tool now labels these outputs local-linear sensitivities.
- Estimator contribution is not literally zero: pooled Standstill slope is about 0.36 and the
  session/type-demeaned slope is about 0.16. Host scheduler and local PIPE variance are ruled out
  as dominant; remaining spread may combine estimator sensitivity, asynchronous transport,
  console/game registration, and freeze-position measurement.
- Sidecar command-to-freeze observations must be split by acceptance. Accepted-only rMAD is about
  12.3 ms; the former mixed accepted/rejected report showed 16.8 ms.
- `300 -> 285` moves the PC deadline exactly 15 ms later (`fireAt = predictedTip - lead`). It does
  **not** prove the landing moves 15 ms. Human-banner A/B data remains the decision authority.

Integration fixes after review:

- Settings schema v2 migrates legacy auto-persisted `tip_timing_auto_unlock=true` to false once;
  a current-v2 user may explicitly opt back in. A default flip alone was not upgrade-safe.
- The divergence diagnostic now calls `d` an instrument estimate and recommends a controlled,
  banner-counted A/B rather than presenting it as ground truth.
- `epoch_table.py` now checks identity across all releases, counts both owned aborts and
  pre-ownership no-fire terminals without double-counting paired log lines, stratifies
  accepted/rejected latency observations, and removes its hard-coded causal verdict.
- `preflight_tip_batch.ps1 -ExpectedShotLeadMs <arm>` certifies an explicitly declared A/B lead
  while retaining 300 ms as its reference default. It also pins Tip Timing, the active profile,
  and the forward-crossing gate, and checks the loaded timing DLLs rather than only the EXE.
- Final combined verification after integration: 814 Python tests passed (2 skipped, 1 expected
  failure, 1 expected-pass marker) and all 17 native CTest targets passed; verifier ended `[orion] OK`.

## The batch

| session (UTC) | n graded | green | early | late | landing med (pp) | landing rMAD | green_start med | tip (green_end) med |
|---|---|---|---|---|---|---|---|---|
| 15:38 | 56 | 41.1% | 51.8% | 7.1% | 94.23 | 2.79 | 94.88 | 97.06 |
| 16:27 | 46 | 15.2% | 78.3% | 6.5% | 93.68 | 1.73 | 95.59 | 97.66 |
| 17:14 | 64 | 26.6% | 68.8% | 4.7% | 94.26 | 1.93 | 95.19 | 98.08 |
| 18:27 | 58 | 24.1% | 70.7% | 5.2% | 93.91 | 1.96 | 95.69 | 97.72 |
| 19:13 | 70 | 32.9% | 61.4% | 5.7% | 93.74 | 2.05 | 94.53 | 97.67 |
| 20:06 | 15 | 13.3% | 86.7% | 0.0% | 92.97 | 0.80 | 95.84 | 97.62 |

336 released, 309 gradeable, 63 no-fire terminals: 43 owned aborts plus 20 independent
`press_unanswered_no_meter` terminals. This is 63/399 attempts (15.8%), not 43/379.
Shot mix: 178 Standstill, 84 Left Fade, 74 Right Fade. Lead 300 ms all day (`lead_source=user`).
All arms `arm_site=subtick`, 308/311 `armed_source=phase`. Build caveat: `OrionNative.exe` was
rebuilt at ~19:58Z, between sessions 5 and 6 — treat session 6 separately.

Reproduce every number in this report:

```
python tools/timing/epoch_table.py --day 2026-09-01 --csv epochs_0901.csv
```

## The epoch table (step 2)

`tools/timing/epoch_table.py` (new) joins, per released shot: session, seq, physical epoch,
shot type, first usable fill (`first_fill` / ownership `own_first_fill` + press age), arm source,
fill and horizon at arm (`armed_fill`, `armed_eta`), predictor sigma, schedule vs delivery token,
delivery stage, hold→release, ownership→release, scheduler lateness (`deltaMs`), hook ack wait,
fill at release, peak (landing), green window, phase sample (anchor→freeze), c20/c25/c30/c35
consensus witnesses and corrections, retargets, token kills/holds, the sidecar's command→freeze
latency, and the graded verdict against the shot's own window. Aborts are attributed to their
ownership `first_fill`. `tests/test_epoch_table.py` pins the join on a synthetic transcript.

## Categorisation (step 3)

**(a) Bias — YES, −20 ms, uniform.** `e_tip` (peak − green_end, in ms via `vel_at_rel`), median by
session × type: −14.2 … −28.9, every cell negative; per type −24.6 (Left Fade), −22.0 (Right
Fade), −19.2 (Standstill). Independent instrument, same number: `TIP TIMING DIVERGENCE: manual
421ms vs measured 401ms (d=-20ms)` logged all day. Counterfactual shift sweep against each shot's
own window: log-green 27.8% → 38–39% at +15…+20 ms, log-late 5.5% → 27–43%.

**(b) Heteroscedastic estimator noise — NO.** Fill at release rMAD 1.4–1.75 pp (mostly the sample's
own frame age). corr(fill_at_rel, peak): Left Fade 0.03, Right Fade 0.01, Standstill 0.35; OLS slope
0.02 / 0.01 / 0.36. Standstill landers below 92 vs above 96: fill_at_rel 38.41 vs 38.36, first_fill
11.6 vs 12.2, armed_fill 21.8 vs 21.9, horizon 96.3 vs 96.3, hold→release 156 vs 154 — identical
command; travel after the command 52.5 vs 59.0 pp; anchor→freeze 313 vs 331 ms.

**(c) Detector sample / anchor quantisation — NO.** Normalised rung witnesses (Codex's consensus)
agree to rMAD 1.8–1.9 ms (p10/p90 ±3 ms); c30 correction median −0.11 ms, rMAD 0.65; 221/309 shots
retargeted at stage 30 and their landing rMAD (2.07 pp) is no better than non-retargeted (1.93).

**(d) Command / PIPE — NO.** `deltaMs` p90 0.5 ms, max ~1.3; hook ack 200–800 µs; corr with the
landing −0.01 / −0.02. All 309 deliveries `local_udp_accepted`, 0 token mismatches. (Codex
measured the same.)

**(e) Console tick / post-command jitter — YES, this is where the spread lives.** Landing rMAD by
type: Left Fade 1.22 pp (~6.5 ms), Right Fade 1.78 (~9.6 ms), Standstill 2.24 (~12 ms). Left Fade
in sessions 2 and 4 landed with rMAD 0.54–0.59 pp (~3 ms) and was 93–100% early: bias-only
failure. Sidecar command→freeze latency rMAD 12–19 ms with heavy tails; PHASE SAMPLE anchor→freeze
rMAD 10–12 ms (anchor noise cancels in it by construction). No sawtooth of landing vs release
frame-age, so the console's input sampling is not phase-lockable from the PC using the capture
grid as the reference.

**(f) Identity / authority churn — NO for released shots.** No-fire is the real churn: 27
deadline-missed aborts had ownership `first_fill` median 30.2 (p10 9, p90 37.7) vs released median
7.5 (p90 20.8); 23/27 were first seen at ≥20% fill, 14/27 at ≥30%. Only 4/27 ever armed. This is
late detector acquisition (Codex's lane). A 15–20 ms shorter lead widens the schedulable band by
~3–4 pp of first fill and would rescue the 30–34 band.

**Instrument caveat (load-bearing).** `green_start` per-shot rMAD 1.5–2.35 pp, window width rMAD
1.2 pp, corr(width, green_start) −0.58…−0.69 (the bottom edge is the noisy read); peak − settled
median 2.1 pp. Shots with a measured window under 8 ms were 97.5% "early" with a landing median
identical to the rest — the ruler moved, not the bot. This is why the owner's eye count and the
log disagree, and why no green rate in this document is a claim.

## What changed (step 4) — exact files and boundaries

| file | function / region | change |
|---|---|---|
| `native_orion/src/AppConfig.h` | `AppConfigData::tipTimingAutoUnlockEnabled` (comment block + default, ~L277–296) | default `true` → `false`, with the measurement |
| `native_orion/src/AutomationEngine.h` | `RemapConfig::tipTimingAutoUnlockEnabled` (~L1297–1305) | default `true` → `false` |
| `native_orion/src/AutomationEngine.cpp` | `AutomationEngine::maybeWarnTipTimingDivergence` (whole body of the emit, ~L14957–15000) | divergence line now: `... shots are landing ~|d|ms EARLY/LATE of the aim this manual value was calibrated for ... move Shot Lead by ±d ms (or Tip Timing by ∓d) ... Do NOT Reset Tip Timing`. Prefix `TIP TIMING DIVERGENCE:` unchanged (parsers). |
| `native_orion/tests/AutomationEngineTests.cpp` | five `tipTimingAutoUnlock*` tests opt in explicitly; two new tests `tipTimingAutoUnlockIsOptInBecauseTheFreezeRidesTheRelease`, `tipTimingDivergenceNamesTheLandingShiftAndForbidsReset` (registered after `tipTimingAutoUnlockLeavesAnAgreeingLockAlone`) | |
| `tools/timing/epoch_table.py` (new), `tests/test_epoch_table.py` (new) | the step-2/3 instrument | |
| `.gitignore` | `native_orion/build-*/` | side build tree used to build tests while the app holds `build/Release` DLLs |

Not touched: `OrionAppController.cpp`, `simple_meter_reader.py`, `PreciseFirePolicy.h`,
`PreciseWaitTimer.h`, `TemporalSampler::predictCrossing`, any lead/phase constant, any gate.
No overlay/capture/Chiaki hunk was edited.

Why the learner is wrong-signed: the sample is `stop − anchor` where the stop is the meter freeze,
and "the stop rides the release" (comment in `recordPhaseConstantSample`). So
`sample = (C_eff − lead) + L(command→freeze)`, anchor noise cancels, and the learner's fixed point
`C_eff(n+1) = sample(n) + aimOffset` moves by `(aimOffset − lead + L)` per window = −20 ms/window
on this rig — a runaway toward the 240 ms learn floor. The repo `settings.json` already carried
`tip_timing_auto_unlock=false` (and `scripts/preflight_tip_batch.ps1` blocks a batch otherwise);
the installed `%LOCALAPPDATA%` settings lacked the key, so the compiled default was live there.

## Why nothing else moved

- The lane forbids global constant shifts without evidence that a mechanism is the cause. The
  evidence points at the operator lead (bias) and downstream jitter (variance), not at
  `predictCrossing`, the phase constant, the horizon debias, or the imminent hold.
- The imminent hold: held vs not held landed 93.72 vs 94.09 (rMAD 2.25 vs 1.71) — no effect worth
  a change.
- Codex's c30 consensus: corrections are sub-millisecond on this rig; it is harmless and stays.
- Ownership / route / deadline / false-shot gates: untouched.

## Offline evidence before/after (step 7)

- Before: default config + a sustained −30 ms divergence → `tipTimingAutoUnlockRequested` after
  window + 9 samples, learner walks the aim earlier. After: 0 unlock signals after window + 40,
  aim unchanged; the divergence line reads `(d=-30ms) ... ~30ms EARLY ... move Shot Lead by -30ms
  (or Tip Timing by +30ms) ... Do NOT Reset Tip Timing` (and the LATE sign for +25).
- `OrionNativeTests` (side build `native_orion/build-timing`, same sources): 565 passed, 0 failed,
  8 skipped; all nine `tipTiming*` tests pass.
- Python: `tests/test_epoch_table.py` 4/4; full suite 1727 passed / 18 skipped with
  `tests/test_nexus_svc_delay.py::test_delay_is_clamped_to_the_hard_safety_range` deselected —
  it fails on an untouched file (pre-existing, meter-delay clamp 300 vs the 600 band; not this lane).
- `scripts/verify_orion.ps1` was NOT run end-to-end: it relinks `OrionNative.exe` and
  `AutomationCore.dll`, and Codex's relaunched app holds both. Run it once the app is closed.

## Remaining uncertainty

1. Human banner count for this batch is missing; the log-derived green class is not outcome truth.
2. The landing is consistently below the measured tip, but the correction magnitude in
   milliseconds is unknown because command-time velocity does not describe the decelerating top.
3. What the post-command spread is made of: Remote Play input cadence, the game's input sampling,
   or freeze-position read noise. Type ordering and mixed sessions prevent a clean type-causal claim.
4. Session 6 ran on a different binary than sessions 1–5.

## Live test plan (categorised; human banners are the outcome)

Preconditions: `tip_phase_aim_frozen=true`, `tip_timing_auto_unlock=false`,
`tip_timing_user_set=true` (preflight enforces), same jumpshot, practice open shots.

1. **Causal deadline arm:** keep Shot Lead 300 and use the dev-only final-deadline sweep with a
   pre-randomized, balanced `0,+15 ms` list in blocks of four. Run 80 Standstills (40/arm), keep
   every other authority pinned, and human-count every banner. Primary physical endpoint is the
   direct `PHASE SAMPLE` difference in milliseconds; secondary is peak shift in pp with no
   `vel_at_rel` conversion. The hook occurs after acquisition gates, so it cannot test no-fire rescue.
2. **Production-path confirmation:** if arm +15 moves the landing in the intended direction, use
   the UI/signed settings to compare actual Shot Leads 300 and 285 in balanced mini-blocks. Count
   every attempt, including no-fire and ungraded outcomes, intent-to-treat.
3. **Type/acquisition follow-up:** only after the causal Standstill result, repeat balanced blocks
   for Left Fade, Right Fade, deep shots, and one online mode. Report the complete 63/399-style
   attempt denominator, `first_fill`, and human banners; do not infer 95% from log fill classes.

## Hand-off to Codex's lane

- Post-command jitter: identical PC-side command timing lands 18–25 ms apart; sidecar
  command→freeze rMAD 12–19 ms. Candidates: Remote Play input injection cadence on the console,
  game input sampling, freeze detection. Attribution needs a timestamp at the Chiaki send and at
  the first frame showing the freeze.
- Late acquisition: 27 deadline-missed aborts with ownership `first_fill` median 30 (released
  median 7.5). Detector `infer=31ms` in `DETECTOR HEALTH` is every other frame.
- Green-start read: per-shot rMAD 1.5–2.35 pp; the bottom edge, not the tip, is the noisy one.

## Fable response to the audit (2026-09-01, pre-A/B)

Accepted: the exact-millisecond claims were model-dependent; `vel_at_rel` (~0.185 %/ms at fill
~38) is not the near-tip rate. Measured on today's per-frame CSV
(`logs/diagnostics/detframes_20260901_133418.csv`, 86 clean rises, freeze-edge pairs excluded):
the 2K27 meter **accelerates** monotonically, 0.17 %/ms at 5-20% -> 0.22 at 50-60 -> 0.24-0.27 at
60-85%. Above 85% there is no clean data (rises end at the freeze, p50 top 91.0, p90 94.3), so
neither acceleration nor deceleration at the very tip is measured. At the highest measurable rate
the 4 pp landing gap is ~15-17 ms, not 21: the +15 ms arm is the right size for the A/B. The
"20 ms" from `TIP TIMING DIVERGENCE` is a time-domain number, but relative to the frozen
calibration, not to the tip, so it too is directional only.

Accepted: 63 no-fire terminals (15.8% of 399 attempts; the 20 `press_unanswered_no_meter` lines
were missed by the first grader), accepted/rejected latency split, settings-v2 migration for the
auto-unlock flag, and the softened divergence wording.

Design notes for the A/B (`ORION_DEV_FIRE_OFFSET_SWEEP=list:0,15`): the list mode CYCLES per shot
(0, 15, 0, 15 ...), so arms alternate rather than randomise; that is balanced and free of any
plausible period-2 confound. Power: the landing shift is the readout that decides the question --
Standstill landing rMAD ~2.2 pp gives a median SE of ~0.45 pp at n=40, so a +3 pp shift is ~6 SE.
The banner green-rate difference is NOT decidable at 40/arm (SE ~10 pp on a ~10 pp effect); it
must not be read as a verdict on Shot Lead 285. Decision rule: if the +15 arm lands >= 2 pp later
(median, per-arm rMAD reported), the command-time lever moves the registered release and 285
becomes the candidate for a banner-counted confirmation batch; if it lands < 1 pp later, the old
"lead is a weak lever" result stands and the bias is downstream.

Rig notes: the console is at `192.168.137.126` (settings.json `remote_play_console_ip`, used by
every session today); the preflight probed a hardcoded `.100` and now reads the setting. Free disk
is 8.3 GB with 68 GB of old framedumps under `logs/diagnostics/framedump` -- run the A/B WITHOUT
`-Framedump`. "Latency route scope EMPTY" at app start is normal; shoot only after
"Timing route acknowledged directly by sidecar" appears (it did within 20 ms of every connect
today). Launch for the A/B:

```
$env:ORION_DEV_FIRE_OFFSET_SWEEP = 'list:0,15'
.
un_orion.local.ps1 -AllowTimingOverrides -Detdiag
```

## A/B batch result (2026-09-01 21:38-21:44Z, `list:0,15`, 47 draws, 45 releases, 36 graded)

| arm | n | log G/E/L | landing med | rMAD | fill at release | green_start | green_end |
|---|---|---|---|---|---|---|---|
| 0 ms | 18 | 5 / 12 / 1 | 94.86 | 1.91 | 38.00 | 95.37 | 98.19 |
| +15 ms | 18 | 8 / 8 / 2 | 95.78 | 1.93 | 41.31 | 94.61 | 98.38 |

Standstill only: 94.21 -> 95.97 (+1.75 pp, n=10 vs 8). The command moved exactly as commanded
(fill at release +3.3 pp at ~0.22 %/ms) but the landing moved ~+1-1.75 pp. **The meter is slow
near the tip**: 0.07-0.12 pp/ms at 92-97% by three independent instruments (late-shot frame
rates 0.06-0.09; Deming slope of landing on anchor->freeze time 0.074 Standstill / 0.119 Right
Fade; the A/B itself), against 0.22 at the release point. Consequences: the early bias is
~25-35 ms in time (not 15-20), the 3 pp window is ~30 ms wide, and the next arm should be
`list:0,25` (or Shot Lead ~275), banner-counted. The 2026-08-13 "lead is a weak lever" result was
this slow tip, not a dead lever.

Owner's banner read (no letters recorded): majority early, fades late when missed. Consistent with
the log (20 E / 13 G / 3 L; both lates in the +15 fade arm).

Consistency: lag-1 autocorrelation of the Standstill landing in the five non-A/B sessions is
+0.11 / +0.01 / +0.13 / -0.24 / -0.07 (no beat); "5-10 perfect then regress" is a ~40% coin plus
per-session window-read drift. The sidecar's command->freeze latency has a detection tail (28
shots >280 ms landed normally) and must not be read as registration jitter. Rung-witness
disagreement (`c30 witness_range_ms`) correlates +0.45 with |landing deviation| -- a usable
bad-shot flag. Fallback arms (`registration+sampler_far`) reached release 3/347 times, all landing
82-85.

Stuck Square (475 presses today): every press produced a locally accepted Square-down; no press
arrived while the engine still owned a shot; 24 got no meter -- 9 sprinting/moving, 4 taps under
200 ms, 4 within 2.3 s of the previous release, 3 first-press-of-session, 4 unclear (one with the
ball in hand, standing still, 1.3 s hold). Transport-lane hypothesis: the hook writes one UDP
packet per state change and ~1/s when idle, so a lost edge packet is not repaired until the next
state change. Suggested fix (Codex lane): resend edge state 2-3x within ~30 ms or a 5-10 Hz
keepalive on the hook path; then re-count ignored ball-in-hand presses.

## Batch 2 setup (2026-09-01 ~21:55Z, owner handed control to the timing lane)

- `settings.json` `actuation_lead_ms` 300 -> **275** (backup `settings.json.bak-lead300-*`, re-signed
  with `tools/diagnostics/_resign_settings.py --write`). Basis: the 0/+15 A/B moved the landing
  ~+1-1.75 pp per 15 ms in the slow tip region (0.07-0.12 pp/ms), so +25 ms is the expected shift
  from a 93.8-94.9 median toward ~96.5-97 (window ~[95.2, 97.9]). Universal: the lead compensates
  PC->console registration, which does not depend on game mode.
- No dev sweep, no overrides (`-Detdiag -NoElevate`); preflight `-ExpectedShotLeadMs 275` CLEAR;
  binaries in `build/Release` (16:06-16:10 local) are newer than all sources, no rebuild.
- Not changed: phase constant (421.3 frozen), `phase_veto_directional=true` (already on),
  gates, detector. Known 1% class left open: a non-phase arm promoted at fill 21-27 with a
  ~25 ms deadline before the phase anchor is dated fires at 82-85 (3/347); the phase member
  arrived 30 ms later and the token was already irreplaceable. Candidate fix: extend the
  imminent-hold refusal one band ABOVE the anchor while the anchor is undated.
- Cross-mode protocol: same lead everywhere; grade per session AND per mode with
  `tools/timing/epoch_table.py`; contested windows are narrower (owner mechanic), so compare
  the landing median to the tip, not the green rate, across modes.

## Batch 2 result (lead 275, 2026-09-01 22:10-22:16Z) and the instrument correction

Owner's banner read: every miss was LATE. Log grade: 17 EARLY / 6 GREEN / 1 LATE. The log was
wrong: joining each release to the raw detector frames (`detframes_*.csv`, `fill_pct` by
`wall_ms`) shows the bar reaching 100.0 and holding it 50-120 ms on a quarter of the shots,
while the logged `peak_fill` for the same shots read 95-97. `peak_fill` is the SUB-PIXEL fill
and it saturates below a full bar; the coarse read reaches 100. Every "slow tip" number in the
earlier sections came from that clipping and is withdrawn: the ramp does not decelerate in a
way the data can show, and the 0.07-0.12 pp/ms slopes were clipped peaks.

Calibration from raw rise maxima joined to releases (fraction of shots whose bar reached >= 99):

| arm | n | raw max med | >= 99 | 97-99 | 95-97 | < 95 | top hold |
|---|---|---|---|---|---|---|---|
| lead 300 (sessions 1-6) | 202 | 94.2 | 6% | 9% | 23% | 61% | 17 ms |
| A/B arm 0 (300) | 22 | 95.9 | 14% | 18% | 36% | 32% | 17 ms |
| A/B arm +15 | 21 | 96.4 | 19% | 29% | 33% | 19% | 75 ms |
| lead 275 | 24 | 93.8 | 25% | 17% | 4% | 54% | 42 ms |

At 275 the distribution is bimodal because a brief top (one or two frames) is missed by a
detector that runs every other frame, so many lates read as "< 95". The tip is ~+15 ms from the
lead-300 command: **Shot Lead set to 285** (settings re-signed, backup `settings.json.bak-lead275-*`).

Shipped with this batch (built into `build/Release`):
- `Release landing:` now carries `raw_peak_fill=` (max of coarse and sub-pixel) and
  `top_hold_ms=` (dwell at raw >= 99) before `shot=`; `tools/timing/epoch_table.py` reads them
  and reports the raw >= 99 share. Test `releaseLandingReportsRawTopWhenSubpixelClipsBelowIt`.
- `MeterOverlayPresentationTracker`: 1.5 px position deadband so one-pixel detector chatter no
  longer moves the outline; larger moves are still followed on the same frame.

Stutter: the sidecar kept 60 fps throughout; the QML frame dispatch (UI thread) stalled ~1.65 s
inside the 5 s window ending 22:12:41.9 (present_gap_max 1668 ms), with 40-60 ms hitches after
other releases. Disk is at 8.3 GB free with 68 GB of old framedumps; every landing also writes
learning.json synchronously. Free the disk first, then instrument the UI thread (Codex lane).

## Batch 3 (lead 285, 22:32-22:40Z): 22 practice Standstills

Owner's banners: mostly perfect, 4-5 late, 0 early. Log with the new raw-top field: raw peak
median 95.8 (rMAD 1.85), tip ~97.9, one full-bar late (raw 100, dwell 586 ms), three at 97-98,
eight at 92.4-94.6 that the log calls EARLY and the owner saw as green -- the green_start read
sits 1-2 pp too high on those, so log-graded EARLY below ~95 is still not trusted. Lead set to
**290** (fire 5 ms earlier) on the owner's late-only read; expected: lates drop to a few, first
earlies may appear.

Overlay: replaced the pure pass-through with an alpha-beta tracker (`MeterOverlayPolicy.h`):
still meter inside 1.5 px is held exactly; a re-seat over 8 px snaps; in between, velocity
carries the prediction (zero steady-state lag on pans/fades) and the residual gain scales with
residual size so pixel chatter is damped ~65%. Preview test relaxed to <=2 px during the first
8 frames of velocity acquisition, <=1 px after.

## Batch 4 (lead 290, 22:42-22:45Z, 28 released): "all over the place"

Owner: some excellent, then late, then an early-only stretch, then normal. Box height was
stable all session (119-125 px rise, 118-121 freeze), so the fill ruler did not move. Three
regimes in the log: (1) phase-armed Standstills landed raw 96.9-99.1 (at/near the tip);
(2) three sampler-armed shots after a late first sight (fill 30-35, epochs 17/18/23) fired
~35-50 ms late -- fill at release 47-49 vs 40 -- and ran the bar to 100 with 0.9-1.9 s holds;
the linear horizon de-bias moved them only +9/+7/-17 ms (the last one the WRONG way, short
horizon below the pivot); (3) the right-corner fades landed raw 95-96.6, ~1.5 pp below the
Standstills. The green-window read was unusable this session (windows like [99.2,100] and
[79,100]); log grades are not to be quoted for it.

Shipped: **ramp-shape correction** for the sampler path (`rampShapeRemainingFactor`, header
note at `RemapConfig::samplerRampShapeEnabled`): the measured 2K27 acceleration (0.16+0.0013 f
%/ms to an 85% knee, 0.24 above) is integrated from the current fill to the top and the fit's
remaining time rescaled (factor ~0.82 at fill 37, ~0.87 at 53; clamp 90 ms). Replaces the
linear de-bias when on. OFF in code (linear closed-loop fixtures), `ORION_RAMP_SHAPE=1` in the
launcher. Test `rampShapeCorrectionShortensTheSamplerHorizonByTheMeasuredCurvature`. Expected
effect: sampler-armed shots (late first sight) move ~40-60 ms earlier, from full-bar late to
near the tip. Phase-armed shots unchanged. The fade offset is left alone (n=6, not powered).

## Batch 5 (lead 290 + ramp shape, 32 released) and the consistency verdict

Owner: Standstills near perfect (a few lates and earlies); fades are the issue. Log (raw top):
Standstill median 96.25, rMAD 2.0, one full-bar, five under 95; Right Fades median 95.5 (n=8),
~1 pp lower, consistent with the 1.5-1.7 pp fade offset over ~100 fades in sessions 1-6. Fade
green-window reads are unusable (ge=100 top-edge slivers), so the fade miss SIGN comes from the
owner's banners (pending). Per-type trim (`tip_phase_type_trim`) is the built lever.

Variance decomposition on 60 phase-armed Standstills with the raw top (residual rMAD 1.63 pp):
anchor->freeze time r=0.67 (console registration jitter), rung-interval speed proxy r=0.16,
fill-at-release r=-0.05, first sight r=0.26, frame age r=-0.28. **Phase locking is dead**: the
command phase against the CONSOLE frame grid (capture cadence, genlocked) gives amplitudes
0.4-1.2 pp, 1 of 8 sessions at p<0.05 (chance), random phase offsets across sessions. Floor
with this architecture ~1.3-1.6 pp rMAD; a 3 pp open window centred gives the observed "near
perfect"; a ~1.5 pp contested window will catch about half at any aim.

Shipped: **above-anchor imminent hold** -- the refusal now also covers one band ABOVE an undated
base anchor (fill 20-26 under base-20), so a sampler/far arm cannot fire at fill 27 while the
dating rung is a frame away (4 guaranteed misses at 82-85 in ~380 releases). Two contract tests
updated to the new band; `imminentHoldCoversTheBandJustAboveAnUndatedAnchor` added. 570/0.

## Session lead probe (built 2026-09-01 ~23:30Z, owner approved "build the probe")

Diagnosis behind it: within a session the shot-to-shot scatter is now ~0.9 pp on Standstills
(best batch: 10 Standstills rMAD 0.90; 8 Right Fades within 1 pp of each other), but the MEAN
moves 1-2 pp between sessions/types/spots and the anchor->freeze median moves with it
(Standstill session medians 95.2/97.1/96.5/95.6 vs anchor->freeze 322/342/335/331 at one lead).
Phase locking is dead (console-grid test, 8 sessions, 1 of 8 at p<0.05, random phase offsets).

Mechanism: `sessionLeadProbeEnabled` (settings `session_lead_probe`, default OFF in code, ON in
the dev settings.json). After `sessionLeadProbeMinSamples`=8 accepted phase samples in a
session, the median anchor->freeze is compared with `LearningData::leadReferencePhysicalMs`
(captured automatically the first time a Shot Lead value completes a probe window; persisted
with the lead it belongs to; a changed lead recaptures). Trim = 0.6 x difference, clamped to
+/-10 ms, latched for the session, added to the user lead on the actuation path only. Reads a
latency, never a landing grade; never moves the aim constant. Seeded tonight with 335 ms @ 290
(the centred batch). Log line: `SESSION LEAD PROBE: disposition=trim_latched|reference_captured`.
Files: AppConfig.h/.cpp (setting + learning keys), AutomationEngine.h/.cpp (config, members,
applyConfig restore/reset, recordPhaseConstantSample probe, measuredLeadForActuationMs),
OrionAppController.cpp (one connect persisting the reference), test
`sessionLeadProbeTrimsTheLeadOnceFromTheSessionsOwnFreezeTiming`.

Fades: NOT trimmed. The fade offset flips sign between sessions (RF 95.5 then 97.2 at one lead),
so a fixed per-type trim would be wrong half the time; the probe addresses the session part, the
spot part needs the five-spot batch.

Hardware note for the owner's Cronus Zen: it is the auth bridge (DualSense on the Zen for PS5
auth); the missing piece is a PC-driven USB gamepad on the Zen's input port (a Pico with XInput
firmware over serial) and a new OrionInputClient backend. What it buys is a sub-ms actuation
path that makes the game's 60 Hz input sampling visible and lockable to the capture-frame clock
-- the only route past the Remote Play floor. Remote Play's own floor stays ~4 ms.

## Screen-position fill bias (found 2026-09-01 23:55Z) -- READER, Codex lane, top priority

Lead-290-era phase-armed Standstills with the raw top and a detector box (n=83), binned by the
meter box's screen x (1280 wide):

| box x | n | raw landing med | anchor->freeze med | fill at release |
|---|---|---|---|---|
| 300-700 | 27 | 96.4-97.9 | ~337 ms | 40-42 |
| 700-1100 | 44 | 94.3-96.3 | 321-334 ms | 40 |

corr(box x, raw landing) = -0.45, corr(box x, anchor->freeze) = -0.52, corr(box x, fill at
release) = -0.09. The command phase does not depend on position; the READ of the frozen bar
and the freeze-detection time do: on the right half of the screen the fill reads ~1.5-2 pp low
and the stop is detected ~12-15 ms early. Owner banners on right-side sessions say LATE while
the log reads early, which is what a low-biased read produces. Box height is constant (120-121).
This is also a large part of the "mean wanders between sessions/spots" finding and of the
left/right fade asymmetry (fade direction moves the meter horizontally).

Consequence for the session lead probe: it reads the same biased freeze timing, so a right-side
session made it move the lead the WRONG way (-8 ms, more lates). Probe switched OFF in settings
(`session_lead_probe=false`); code stays. Re-enable only once the reader's horizontal bias is
fixed, or gate the probe on a centred meter (normalized x 0.35-0.65).

Left Fade +6 ms trim stays (banner-consistent early in every session). Right Fade untrimmed.

## 2026-09-02 — Reader ruler: per-shot seed replaced by a session median

**Finding (framedump session_20260901_190427, 47 shots, 733 measured frames).** The served box is
locked to the meter (top slack 8 px, bottom 12-13 px, rMAD 0.00 px per shot at every screen x).
The reader's sub-pixel fill ruler (scale D + box-to-base offset) is latched per shot from the first
3 clean frames at acquisition; replaying `_measure_fill_in_box` on the dump shows the latched offset
spanning 9.2-13.2 px across shots (4 px = 3.4 pp). Live, the green cap (fixed structure) read 93-99
with a between-shot rMAD of 1.94-2.15 pp; a box-referenced ruler read the same cap at rMAD 0.81 with
no position dependence. On the same frames the 20% anchor was dated up to 19 ms apart by the two
rulers. 1 pp of ruler = ~5 ms of anchor = ~1 pp of landing: the size of the residual earlies/lates,
and the mechanism behind the right-corner / off-the-dribble / fade misbehaviour (the seed is taken
while the box is still settling and the shooter is moving).

**Fix.** `ORION_METER_SUBPIXEL_SESSION_RULER=1` (launcher; code default 0): every completed per-shot
seed joins a session history; once 3 exist the emitted (D, off) is the median of the last 15. The
numerator (base - edge) is untouched, units/aim/learned rate unchanged, fail-open byte-identical
below 3 seeds, a genuine rescale clears the history. `simple_meter_reader.py::_adopt_session_ruler`;
`tests/test_simple_reader_session_ruler.py` (synthetic meter, per-shot seed jitter: spread 3.4 pp ->
0.4 pp). Offline A/B on the dump: latched offset range 3.99 -> 2.15 px, rMAD 0.57 -> 0.22 px (the
20 fps replay seeds cleaner than live, so this understates the live gain).

**Live validation metric.** Launch with `-Framedump`; per shot take the median `green_center_pct`
over frames with fill 15-85 (frames.csv); the between-shot rMAD was 1.94-2.15 pp, target < 0.8 pp.
Also: right-corner / off-dribble / fade landings should no longer sit apart from Standstill.

**Not changed (data says no, for now).** Within-shot ruler switching (sub-pixel gate failure ->
coarse box ruler) is 7% of anchor-band frames, concentrated in one low-contrast shot. The base row
does not drift with fill. Base-to-cap distance is 98.93 px with 0.53 px per-frame rMAD: a two-anchor
session scale is the next step if the seed median is not enough.

## 2026-09-02 — Two traps closed before the ruler batch

**Hidden default type trim.** `AppConfig.h` ships `tipPhaseTypeTrimMs` defaults {Left Fade -4, Right
Fade -6} and the loader merged the settings map per key, so the 09-01 file `{"Left Fade": 6}` left
Right Fade -6 live (2 right fades fired 6 ms early in the 19:04 batch; the owner never set it). The
loader now REPLACES the map when the file carries one (`AppConfig.cpp`), the dev settings carry an
explicit `"Right Fade": 0`, and `tipPhaseTypeTrimFileMapReplacesTheBuiltInDefaults` pins it. Rule:
with the trim enabled, name every type in the file.

**Console address drift.** The PS5's ICS DHCP lease moved .100 -> .138 -> .126 across two days; each
move killed the input session ("the current Chiaki session ended before becoming ready") while the
console answered discovery one address over. `RemotePlayClientManager._resolve_console_host` now
broadcasts a discovery SRCH on the /24 when the configured host answers neither its session port nor
discovery, adopts a SINGLE answering console for that launch (ERROR-level log names the drift and the
address to put in settings), never guesses between two, and is cached 30 s so a standby pre-spawned
at the stale address is discarded and the cold path spawns at the adopted one. `ps5_wake.discover_consoles`
/ `subnet_broadcast`; six tests in `tests/test_ps5_wake.py`. `ORION_PS5_DISCOVER_DRIFT=0` disables.

## 2026-09-02 — Ruler-transition fence: tolerate a consistent step, fence a reset

**Measured.** `fill_ruler_transition` cancelled 17/184 promoted tokens on 09-01 evening and 14/140 on
09-02 morning. Every event was subpixel->subpixel, generation +1, 35-98 ms BEFORE the scheduled fire:
the reader's base anchor dropping to the box anchor for one frame (a ~1 pp ruler nudge), not a reset.
The cancel then re-armed two fresh frames later, past the lead runway on a 60 fps rise, so most of
these ended as `live_tip_deadline_missed` (22 of 140 presses today). A promoted token's fire instant
was already proven at the arm; killing it converts a release into a guaranteed bad shot.

**Change (`AutomationEngine::fenceArmedTokenOnFillIntegrityBreak`).** On a ruler transition the fresh
fill is compared with what the ARMED ruler predicts for that instant: the nearer of the previous
genuine sample and the arm-time fill, extrapolated at 0.20 %/ms; band = `rulerTransitionTolerancePct`
(3.0) + 0.05 pp/ms of horizon, capped below the same-ruler rollback limit, horizon <= 120 ms. Agreement
-> "TIP FILL RULER TRANSITION TOLERATED", token kept, rollback comparator re-based on the new ruler
(mode/generation/fill/eta). Disagreement, or no reference -> the fence exactly as before, now naming
expected/fresh/tolerance. `ORION_RULER_TRANSITION_TOLERANCE_PCT` overrides (0 = shipped behaviour).
Tests: `rulerTransitionWithConsistentFillKeepsTheArmedToken` (kept + re-based + a 23 pp rewind on the
new ruler still fences; a 6 pp step fences; tolerance 0 fences), the existing rollback/transition
tests unchanged. Rulers are still never compared to each other.

## 2026-09-02 — Session-ruler batch (11:57-12:15, 92 releases), log-graded

Raw peak from shot three on (session ruler active): median 96.23, rMAD 1.40. Spots: left 96.5,
centre 96.2, right 95.8 (the right-side depression shrank to 0.7 pp). Right Fade 96.5 rMAD 0.56,
Left Fade 96.9 rMAD 1.7, Standstill 96.0 rMAD 1.68; one raw >= 99, five below 93, no full-bar dwell.
The first two shots (still on the per-shot latch, during an inflated detector box) landed raw 76-78.
Offline on the dump: reader-vs-pixel ruler spread between shots 2.2 -> 1.5 pp; the green-cap read
still wanders ~1.6 pp between shots (n=10, per-frame noise floor ~0.6), so the ruler is better but
the cap read is not a clean ruler instrument. No-fire 30/134 = 22% (deadline 16, no-meter 10,
ownership 4); the fence tolerance above targets the deadline part. Owner banners pending.
Full native suite after the fence + loader changes: 571 pass / 0 fail / 8 skip.

## 2026-09-02 — Fence-tolerance batch (12:45-12:49, 67 presses, 58 graded)

Owner: "decent batch, most misses were lates, a few earlies, a 7-8 shot late run then back to normal".
**No-fire 2/67 = 3%** (was 30/134 = 22%): one tolerated transition, two genuine fences, zero deadline
misses. Landings raw median 96.23, rMAD 2.33; 8 below 93, 10 at or above 98, one at 99.7, no full-bar
dwell. The late run is shots 2-10 (session start, left corner): normalized anchor->freeze 340-355 ms
against 334 for the rest of the session, i.e. a +6..+15 ms registration-latency excursion, not the
ruler (anchor->freeze is ruler-invariant) and not systematic across sessions (first-10 vs rest differs
by -10..+11 ms over 8 sessions). Anchor->freeze is now position-free (corr with meter_x +0.01/+0.07 in
both ruler sessions; it was position-biased under the per-shot latch), so a pn-driven lead correction
is no longer contaminated by position. Meter speed at release does not predict the landing (r = +0.04;
Standstill -0.06). Fill-at-release vs landing: r +0.24, slope 0.18 pp/pp (09-01: +0.33 / 0.28) -- weak,
not a fire-phase or ruler-offset signature. Capture-frame wall timestamps in the framedump are not
grid-locked (phase concentration R 0.03-0.21, residual ~5 ms), so a console-poll phase test needs the
60 fps capture-callback timestamps, not the dump.
**Action:** Shot Lead 290 -> 293 (the distribution sits ~0.7 pp higher than the 290 calibration
batches and the owner saw mostly lates); everything else unchanged.

**Fence addendum (imminent forward step).** Live 12:47, token 43 was fenced 1.8 ms before its fire on
a +5.6 pp forward step (band 3.66) and the shot was lost. Inside one source cadence of the deadline a
token cannot be re-armed, and a forward step is not a reset, so an imminent token now keeps a forward
step up to the same-ruler rollback limit (logged `basis=imminent_forward`); a rewind inside the window
still fences. Test block added to `rulerTransitionWithConsistentFillKeepsTheArmedToken`. In code, not
yet in the running build (rebuild after the lead-293 batch).

## 2026-09-02 — Lead-293 + imminent-forward batch (13:00-13:1x, 38 presses, 35 graded)

Owner: "decent batch, a few lates and earlies still, slightly more consistent". **No-fire 0/38.**
Seven ruler transitions tolerated (none imminent), zero fenced. Raw landing median 96.19, rMAD 1.41;
2 below 93, 2 at or above 98, none at 99, no full-bar dwell. Spots: left 97.1 (n=6), centre 95.7,
right 96.2. Anchor->freeze normalized: first 10 shots 339.9 vs 329.5 after (+10 ms) -- the same
session-start excursion as the 12:45 session (2 of the last 3 sessions), earlier sessions mixed.

Three-batch ladder today (log-graded raw, first two shots excluded):
| batch | change | raw med | rMAD | no-fire |
| 11:57 | session ruler | 96.23 | 1.40 | 22% |
| 12:45 | + fence tolerance | 96.23 | 2.33 | 3% |
| 13:00 | + lead 293, imminent-forward | 96.19 | 1.41 | 0% |

## 2026-09-02 — Reader: box-relative base hold (ORION_METER_SUBPIXEL_BASE_HOLD, launcher ON)

In the 13:00 batch 7 of 35 shots stepped 2-3 pp after the arm (tolerated, so nothing was lost, but
the landing read and the post-arm refinement still jump). Cause: when the base falloff is not
measurable on a frame and the 120 ms velocity hold has aged out, the sub-pixel path fell to the BOX
anchor `(bh-1 - edge)/D`, a different ruler from `(base + off - edge)/D` by `off - (bh-1-base)`,
i.e. the box slide since the seed plus the seed-vs-session offset. The reader now keeps the lock's
last measured base-to-box offset and places the base on the current box (`anchor=base_held`,
`hold=box_rel`, bounded 0.8 s, cleared on every lock reset), so the family stays "base" and native
sees no estimator step. Fail-open, flag-gated; clean frames byte-identical.
`tests/test_simple_reader_base_hold.py` (synthetic shelf defeats the base measurement: without the
flag the frames go to the box family with a 1.7 pp step, with it the fill stays continuous).

## 2026-09-02 evening — Fleet verdicts (five lanes, read-only, scratchpad/lane_*)

1. **Transport** (chiaki-ng-src orion, feedbacksender.c / takion.c): fire -> wire 0.45 ms typical,
   1.9 ms worst of 244; no send tick, no batching. Feedback packets bypass the reliable buffer: a
   Square-up is ONE UDP history datagram, repaired only by the next button edge = the stuck-Square
   miss. Recommended: redundant SHOT_RELEASE history datagram +3 ms (sketch in the lane report).
   The console's frame cadence is observable at the first AV packet per frame_index (videoreceiver.c).
2. **Variance budget** (192 shots, 4 sessions): the engine's anchor->freeze (pn) and the sidecar's
   command->freeze disagree (corr ~0) -> pn is mostly stop-dating noise; physical registration jitter
   <= 3-5 ms. True landing spread ~1.2 pp (~5 ms) after 0.8 pp read noise. Largest real term = fire
   phase (~1 pp: anchor dating + mid-shot ruler steps). No run clustering beyond chance. Candidate:
   multi-sample anchor fit over 15-35% (1.0 -> ~0.6 pp).
3. **Lead tracker** (350 shots, 9 sessions): do not ship; wash in the ruler era (pn no longer predicts
   the landing), any gain >= 0.5 hurts. Needs a raw-frame freeze instrument first.
4. **Grid phase**: detframes `wall_ms` IS genlocked (capture CadenceLock, 60.00 Hz, 0.08 ms rmad) but
   only with -Detdiag. Pooled fold on 09-01 (197 shots): no common phase (p 0.92); per session 1/5
   nominal. Not a lever now; decisive test = -Detdiag batches + true fire wall stamp + ~250 shots.
5. **Reader census**: 92% of shots date the 20% anchor on the box0 seeding path BEFORE the lock's seed
   completes -> the session ruler never reached the anchor; paired jitter 0.46-0.63 pp (2.5-3.5 ms).
   Fix = provisional session-ruler adoption at lock start (ORION_METER_SUBPIXEL_SESSION_PROVISIONAL,
   shipped, launcher ON, 3 tests). Base hold correct but barely touches anchor-band frames.

**Corrected no-fire counts** (abort-identity lines were missed before): 11:57 29%, 12:45 5%, 13:00 5%,
19:57 31% (8 deadline aborts, kills: unattributed 19, tick_dropout_guard 11, subtick_fire_at_past 5;
lead_kind stayed factory) -- under investigation (vision dropouts in that session, not the reader
ruler changes: the base hold does not touch presence).

## 2026-09-02 evening — Two engine changes after the fleet, plus the provisional ruler

**User-lead liveness (`[ORION_USER_LEAD_LIVENESS]`, AutomationEngine.cpp telemetry ingest).** In the
19:57 session 8 of 32 presses died as `live_tip_deadline_missed`: each time the token was armed with
the user lead authoritative, then 3 ms later `owned_square_measured_lead_unready` + `TIP TOKEN KILL
site=tick_dropout_guard` (19 more kills unattributed) while the estimator itself was healthy
(observations warming n=1..6, sd 5.4 -> 2.7). Mechanism: any payload whose latency VALUE snapshot
fails the fail-closed value gate (`latencySnapshotValid`) fell into the else-branch that clears
`measuredLeadTelemetryPresent_`/`measuredLeadLastUpdateMs_`/route echo — the user-typed lead's whole
liveness proof — so `measuredLeadAuthoritative()` went false for one tick and the guard killed the
token. Fix: when such a payload still carries a live scope epoch + delivery route, keep liveness and
the route echo (value authority is still revoked); with no route carried, clear exactly as before.
Plus `LEAD AUTHORITY LOST: ...` (one line per armed attempt) naming every clause (telemetry_present,
age, scope, expected/measured route+gen, kind, n, sd, epoch_ready, armed source/eta/runway) so the
next occurrence is attributable from the log. Test `userLeadSurvivesValueColdPayloadOnTheLiveRoute`.
Open question: WHICH payloads were value-cold mid-session (two autogreen_sidecar.py processes were
observed running in every launch today — .venv python and Python312 python — a duplicate feed would
interleave payloads; verify with the new diagnostic line next session).

**Provisional session ruler** (`ORION_METER_SUBPIXEL_SESSION_PROVISIONAL`, launcher ON): see the
fleet reader lane; tests in test_simple_reader_session_ruler.py (8 pass).

## 2026-09-02 21:58 batch (full build: provisional ruler + liveness) — owner: "mostly lates and earlies,
somewhat consistent, not viable in game yet; step back and reinvestigate"

59 presses, 52 graded. No-fire 6/59 = 10%, ALL press_unanswered_no_meter (acquisition), zero deadline
misses, zero LEAD AUTHORITY LOST lines (the liveness fix held), 5 tolerated / 1 fenced transition.
Raw landing median 96.17, rMAD 1.48; 8 at or above 98, 2 at 99+, 1 below 93. Types within 0.6 pp of
each other; spots within 0.25 pp. Anchor->freeze 336 rMAD 12 (no session-start excursion).
Day ladder (shots 3+, raw med / rMAD / no-fire): 11:57 96.23/1.40/29% -> 12:45 96.23/2.33/5% ->
13:00 96.19/1.41/5% -> 19:57 94.94/1.47/31% -> 21:58 96.17/1.48/10%.
Reading: the log-graded spread has been flat at ~1.4-1.5 pp all day regardless of change; the
median moves ±1 pp between sessions (19:57 vs the rest) on the same lead; the owner's banner verdict
is worse than the log's tails suggest. The log's absolute scale is not the game's window.

## 2026-09-02 22:00 — Batch on the full build (21:58, 59 presses, 52 graded) and STATE OF PLAY

Owner: "mostly lates and earlies". Log: no-fire 6/59 = 10%, all `press_unanswered_no_meter` (0 deadline
misses, 0 LEAD AUTHORITY LOST lines -> the liveness fix holds); 5 tolerated ruler steps, 1 fenced.
Raw landing median 96.17, rMAD 1.48; 8 at >= 98 and 2 at >= 99 (lates), 1 below 93. Standstill 95.9
rMAD 2.0, Left Fade 96.5 / 1.4, Right Fade 96.1 / 1.6; left 96.3 / centre 96.2 / right 96.0.
Anchor->freeze 336 rMAD 12 (first10 334 vs rest 338: no excursion). The last three full-build batches
are statistically the same (rMAD 1.41 / 1.47 / 1.48); lead 290 -> 293 did not visibly shift the median
(96.2 in three of four sessions; the 19:57 session read 95.4).

**STATE OF PLAY for whoever picks this up (Codex on the venice bridge):**
- Live build = session ruler + provisional adoption + base hold (reader, launcher flags) ; fence
  tolerance + imminent-forward + user-lead liveness + LEAD AUTHORITY LOST diagnostic + trim-loader fix
  (engine, build/Release 21:16) ; lead 293, Left Fade +6, Right Fade 0 ; console .126 (drift resolver
  in the sidecar). Tests: native 572 pass, reader 425+8 pass.
- Measured budget (fleet, 192 shots): true landing spread ~1.2 pp (~5 ms); fire phase ~1 pp is the
  largest real term; console registration <= 1 pp; landing read noise 0.8 pp (log only).
- NOT levers (measured): adaptive lead tracker (wash), poll-phase lock (no common phase; needs
  -Detdiag + a true fire wall stamp + ~250 shots), PC transport (0.45 ms).
- NEXT, in order: (1) redundant SHOT_RELEASE history datagram +3 ms in the Chiaki fork (stuck-Square
  miss class; sketch in scratchpad/lane_transport); (2) multi-sample anchor fit over 15-35% on the
  steady ruler (fire phase 1.0 -> ~0.6 pp; prove offline on the 60 fps detframes first — launch
  batches with -Detdiag); (3) freeze instrument from raw frames (prerequisite for any pn-driven lead
  logic); (4) the `press_unanswered_no_meter` 10% (detector acquisition on the press).
- Grading rule: count BOTH "SHOT NOT OWNED" and "Shot abort identity ... reason=" lines; raw_peak_fill
  >= 98 ~ late, < 93 ~ early on this ruler; owner banners are the authority.

## 2026-09-02 23:15 — Anchor-fit verdict (offline, 09-01 60 fps detframes, 343 rises) and the poll floor

Split-sample estimator noise (two independent datings of the same rise, rMAD of the difference):
straddle-20 vs straddle-30 normalized 3.81 ms (per-crossing noise ~2.7 ms; the +4.85 ms median is the
level-adjustment calibration, not noise); ramp fit 15-35 vs straddle-20 1.81 ms; fit 15-25 vs 25-35 only
n=11 (a 10-pp band is 3-4 frames at 60 fps). With the engine's c30 median of three crossings the anchor
already sits at ~1.6 ms; a full-band fit could reach ~1.2 ms. Gain <= 0.4 ms = <= 0.1 pp. **Not
building the anchor fit.** The fleet's "fire phase ~1 pp" is therefore mostly the tolerated ruler steps
moving the landing READ (log-only), not the fire.

Arithmetic that now matters: the console samples input once per frame, so an unlocked press has a
uniform 0-16.7 ms registration delay (sigma 4.8 ms ~ 0.9-1.1 pp at the near-tip rate). A ~3 pp green
window at ~0.24 pp/ms is ~12.5 ms wide: with sigma 4.8 ms perfectly centred, ~79% of shots land inside;
95% needs sigma <= 3.2 ms. The measured true spread (~1.2 pp) is already at that floor. **95% is not
reachable by centring or by sharper anchors; it needs the poll phase (or a hardware path that lands the
press at a known phase).** Path: (1) Sol task #2 fire wall stamp + -Detdiag batches; (2) fork instrument:
stamp the first AV packet of every frame_index (videoreceiver.c) and export it — the console's own frame
clock, sub-ms on wired ICS, likely stable across sessions unlike the capture-card grid; (3) pooled fold;
(4) if a sawtooth shows, time the fire to the poll. Interim: lead re-centre +5 ms (293 -> 298) because the
banners and the log tails both say lates dominate (10 of 52 at >= 98 vs 1 below 93).

## 2026-09-02 23:05 — Lead 298 batch (91 presses, 81 graded): REVERTED to 293

Landing median 96.60 rMAD 1.56 (lead 293 batch: 96.17 / 1.48) — a +5 ms lead moved the landing UP 0.4 pp
(session noise), and cost 6 `live_tip_deadline_missed` aborts, all `unschedulable_lead` (tip_eta 283-297
ms < 298 at arms made at fill 31-41). 12/79 at >= 98, 3 < 93. No LEAD AUTHORITY LOST lines, 1 tolerated
step. Verdict: the lead is not a usable centring lever at the +/-5 ms level against +/-0.5 pp session
variation, and every ms of lead eats arm runway. Reverted to 293 (re-signed, relaunched 23:00:36 with
-Detdiag). Bus: Sol (Codex) built the redundant-release client (SHA C8464B4E...) but it links ffmpeg 8
(avcodec-62/avutil-60) while the shipped tree ships ffmpeg 7 (avcodec-61/avutil-59): "missing dlls" at
launch; restored the previous client (D4412372...). Sol also built the AV-clock instrument (task #3,
ORION_AVCLOCK=1, CSV frame_index/arrival_qpc_us/wall_ms; SHA EC695A24...) — same DLL blocker. Launcher now
sets ORION_AVCLOCK=1 + ORION_AVCLOCK_PATH (inert on the old client).
Grid fold preview, tonight's 81 shots (engine-tick fire stamp, wall_ms grid R=0.98): sine amp 1.25 pp,
phase -134 deg, permutation p=0.29 (noise-only ~0.76 with heavy tails). Suggestive, not decisive.

## 2026-09-02 23:15 — Fork client v2 LIVE (redundant release + AV clock), AV clock empty

Sol's ffmpeg-7 rebuild (OrionStream.exe SHA 5CF3171826C2DC5E06AFBC211D4CC24886AAEFD6E2C2287617EC036DC1E81CE7,
imports avcodec-61/avutil-59, zero unresolved imports against the deploy tree) deployed to
native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/ (known-good D4412372... backed up beside it) and
relaunched 23:08:39. App log confirms: "Remote Play client image: context=preview-promotion
source=repository size=7012546 sha256=5cf31718... path=...deploy...OrionStream.exe" ("repository" = the
repo deploy dir; read the path field of the LAST such line, not a prefix grep). Batch on it: healthy
(34 presses / 31 releases / 0 unanswered / hook failures 0 at the time of writing).
OPEN: logs/diagnostics/avclock_20260902_230839.csv holds only the header — this rig runs Chiaki
input-only (video from the Elgato), so either the video receiver never sees packets in that mode or
they are dropped before the frame_index parse. Sol is checking; if the console sends no AV packets in
input-only mode, the console frame clock must come from another packet cadence.
Launcher (Sol): -Framedump now implies -Detdiag/ORION_DETCSV=1 (explicit -NoDetdiag opt-out); the
ORION_AVCLOCK lines stay inside the Framedump block.

## 2026-09-02 23:15 — Batch on fork client v2 (48 presses, 46 graded); Left Fade trim removed

Owner: "really good batch, no stuck square, everything great, a 3-shot late run, fades mostly lates".
Log: no-fire 2/48 = 4% (1 deadline, 1 no-meter; 0 unanswered presses), 2 tolerated steps, 0 fenced,
0 LEAD AUTHORITY LOST. Raw median 97.03 rMAD 1.36; 9 at >= 98 + 1 at 99.4; 1 below 93. Types: Standstill
97.2 (5/22 >= 98), Left Fade 97.2 (4/10 >= 98), Right Fade 95.6 rMAD 0.56 (0 lates). The 3-shot late run
(99.4, 98.1, 98.1 at shots 4-6) is the poll-jitter tail; a session-start warm-up bias is NOT supported
(shots 3-12 vs 13+ across six sessions: median +0.43 pp, 4/6 positive, range -1.1..+1.4).
FADES: across today's seven sessions the Left Fade offset vs same-session Standstill pooled +0.34 pp
(SE 0.95, not significant) while carrying the +6 ms trim (positive trim = fires LATER); Right Fade
-0.45 pp pooled, -1.6 pp in the last two sessions (trim 0). The owner's banners say fades run late, the
log says Left Fades sit at the late edge -> the +6 ms Left Fade trim is not earning its keep: REMOVED
(tip_phase_type_trim_enabled false, map {Left Fade 0, Right Fade 0}, re-signed, relaunched 23:14:25).
No Right Fade constant added (two sessions is not a powered split; note the direction: early).
NOTE on absolute scale: the log median wanders 95.2..97.2 across sessions at the same lead (the session
ruler median differs per session), so log medians are not comparable across sessions; grade within a
session and by banners.

## 2026-09-02 23:35 — Trim-off batch (23:14, 51 presses, 32 graded) and the stamped-build deploy

Log: 17 of 51 presses `press_unanswered_no_meter` (Square presses with no meter = passes/non-shots if
the owner was in a game mode; not a timing miss), 1 deadline, 3 tolerated, 1 fenced. Graded 32: median
95.71 rMAD 2.09; Left Fade 94.96 rMAD 2.77 (2 early) — with the +6 trim removed the Left Fades moved
DOWN ~2 pp vs the previous session (97.2), i.e. untrimmed Left Fades run early (as 09-01 found) and the
+6 overshot; the right value is likely ~+3 but n=10 on a wandering ruler — wait for banners before
re-adding. Standstill 95.4 rMAD 1.30, Right Fade 96.7 rMAD 0.98.
DEPLOY (owner idle 12 min): fork client v3 (SHA 8A2C69525586E4E66D1A618F020DE2955B17C159A067A9186B01797FB59FEE62,
AV-clock stamp after av_packet_parse in capture-card mode, logger only on encrypted streaming takion) +
Release rebuild of OrionNative with Sol's fire_epoch_ms on the "Release submit" line (FireEpochClock.h,
FireEpochClockTests 5/5, OrionNativeTests 572/0/8) + relaunch. Sol's tools/timing/poll_phase_fold.py
(tests 6/6) is the verdict tool for the first stamped batch.

## 2026-09-03 10:05 — First fully stamped batch (170 presses, 133 graded): POLL PHASE NOT DETECTED

Build: fork client v3 (8A2C6952, redundant release + AV clock), OrionNative with fire_epoch_ms, lead 293,
trims off. Instruments live: 154 stamped submits; avclock CSV 17,331 rows — the Remote Play stream in
capture-card/input-only mode runs at 29.970 Hz (period 33.366 ms, residual rMAD 0.41 ms over any 10-s
window; the tool's "first 10 s" quality line reads the stream start and is misleading), i.e. the console
frame clock is clean and the console vsync is 59.94 Hz, not 60.00.
Batch: no-fire 17/170 = 10% (6 deadline, 9 no-meter, 2 ownership), 9 tolerated steps, 0 fenced, 0 LEAD
AUTHORITY LOST. Landing median 96.19 rMAD 1.45; 6 below 93, 17 at >= 98, 1 at 99; three discrete
failures (85, 69, 84). Types: Standstill 96.1 / 1.49, Left Fade 96.2 / 1.44 (trim 0), Right Fade
96.6 / 1.85, No Dip 97.1; spots left 96.4, centre 95.2, right 96.2.
**Fold (tools/timing/poll_phase_fold.py, n=130 joined, 4000 permutations, console clock AND capture
grid, period 16.667 and 16.683):** sine amplitude 0.28 pp (p 0.29), sawtooth height -1.09 pp at cliff
11.05 ms (p 0.31); noise-only sine amplitude at this n ~0.22 pp; a full 60 Hz poll would show ~1.4 pp
sine / ~3 pp sawtooth at > 6 sigma. **Verdict: no 60 Hz input-poll structure in the landings.** The
"poll floor" arithmetic of 23:15 is withdrawn: the residual (~1.45 pp rMAD, ~6 ms) is unstructured and
is not removable by phase locking. Phase locking is closed, this time with the right instruments.

**Fold at other periods (same 130 stamped shots, 4000 permutations, console clock):** 33.366 ms (RP 30 Hz
frame): sine 0.04 pp p 0.97, saw 0.74 pp p 0.85; 8.3415 ms (120 Hz): sine 0.11 p 0.85, saw 0.74 p 0.75;
4.0 ms (250 Hz): sine 0.14 p 0.73, saw 0.72 p 0.85. No periodic input-registration structure at 30, 60,
120 or 250 Hz. Sol reproduced the 60 Hz verdict independently. HARDWARE PATH PAUSED by the owner (no
microcontroller, Titan Two sold; Sol's research: the Titan Two's PC-forwarding feature is KMG Capture in
Gtuner IV, which would take Orion's existing ViGEm XUSB pad with zero new code; the Zen's PROG port is
config/monitor only). Owner directive: exhaust software and network first. Sol task #6 = no-fire
decomposition (17/170) + the 3 discrete failures with root causes and fix proposals.

**Session-median wander is the RULER, not physics (09-03 pixel test, scratchpad/session_offset_pixel.py).**
Frozen-bar fill measured from the framedump PNGs against the served box with fixed slack (100 = cap
apex), per-session median of per-shot max: 11:57 97.9, 12:26 97.4, 19:57 96.1 (n=9), 21:58 97.4,
22:53 97.1, 23:08 97.9, 23:14 97.1, 10:05 97.4 — flat within +/-0.5 pp while the log's raw_peak medians
wandered 95.2..97.2 across the same sessions. The bot's physical landing is stable at ~97.4 on the apex
scale (2.6 pp below the tip); do not chase session offsets with the lead. Log medians are only
comparable within a session.

**09-03 residual-structure sweep (pixel plateau per shot, scratchpad session_sequence.py / rate_vs_landing.py):**
- Runs: lag-1 autocorrelation of the landing sequence ~0 in the two big sessions (n=35 r=+0.07 p=0.77;
  n=40 r=+0.07 p=0.68); one 16-shot session carried a 6-shot early episode (r=+0.45 p=0.08). Lates and
  earlies are memoryless -> no adaptive/EWMA lead lever (matches the earlier tracker wash).
- Render-frame comb (3.27 pp = 0.196 %/ms x 16.68 ms): NOT DETECTED (Rayleigh p 0.12-0.51 big sessions);
  plateau values sit on the 1-px grid. The frozen fill is not frame-quantized.
- Meter rate per shot: 3-4-point rise slopes rmad 5-6% = the ~8 fps dump's noise floor; corr(rate-predicted
  shift, observed landing) = +0.07, regression slope 0.06. The 86-87 earlies had slopes -4% and 0%.
  Rate variation is NOT the cause of the earlies.
- Link: the PS5 sits on a WIRED ASIX USB-to-GbE adapter (ICS 192.168.137.1 -> .126, 1 Gbps).
- Network: 83/83 ICMP <1 ms; 200 UDP discovery round trips rmad 0.031 ms, p99-p50 0.13 ms, 0 loss
  (the 100 ms is the console's deliberate responder delay). The wire is not a lever.
- Fork feedback sender (feedbacksender.c): the press travels as a Feedback HISTORY packet; the Orion
  queue is drained one transaction per iteration, gated only by the entry's not_before_ms; the 8 ms
  FEEDBACK_STATE_TIMEOUT_MIN_MS is defined but unused, so nothing spaces our press behind a periodic
  state packet. No software delay found on the PC side of the wire.

## 09-03 no-fire decomposition of the 10:05 batch -> the meter is CONVEX and the registration template was 2K26

Sol's table: NexusVision-scratchpad/Sol/nofire-1005/report-v2.md. 170 Square edges = 154 releases +
16 no-fires; 9 no-meter presses are non-shots (pass-through), so the shot-intent denominator is 161:
fired 154/161, genuine no-fire 7/161 (4.3%), bad incl. 3 earlies 10/161 (6.2%).

Root causes (pixel / 60 fps evidence: scratchpad nofire_shots_pixels.py, curve_crossings.py,
rebuild_tipreg_2k27.py):
- 5 of 6 deadline misses (e69/e111 Left Fade, e138/e142/e143 No Dip): models/tip_registration.json
  was built 2026-08-04 on the 2K26 meter (priors T0 267/367/450 ms). On 2K27 captures its ms_to_tip
  is biased -182 ms (fill>=12) / -126 ms (fill>=20). e69/e111 died `unschedulable_lead` on their
  FIRST sample while the pixels show the tip 420-440 ms away; the No Dip trio held on
  registration_far_disagreement until the runway was gone. Sol rebuilt the template on 15 2K27
  sessions (157 shots, model_version sha256-4be90a1db390) and published it; the 2K26 file is at
  models/tip_registration.json.bak-2k26-20260903.
- The 2K27 meter is CONVEX (60 fps crossings, 48 clean shots): local rate 0.158 (10-20), 0.176,
  0.190, 0.201, 0.216, 0.223, 0.228, 0.245 (80-90) pp/ms. Fill-only time-to-90 map: from 20%
  334.5 ms rmad 8.9; 30% 277.7 rmad 6.9; 40% 225.9 rmad 5.8; 50% 176.6 rmad 4.6. A straight line
  at 20-35% claims 10-16% too much runway, which is why the sampler "contradicted" a correct
  phase on every No Dip.
- e139 (landing 84) was a genuinely SLOW meter: 0.114/0.143/0.167/0.173/0.188/0.206 vs standard,
  so the phase fired ~80 ms early. e125 (landing 68.6): a 3-sample pre-anchor sampler armed at
  fill 10.84 with a 47 ms ETA. e50 (landing 84.9): the previous shot's ~81% bar re-read through a
  39-41 px wide box (33/39.6/46.2/83.0/0.0) escaped the ghost zone as a "real onset". e174: the
  reader's ghost quarantine held the new meter until fill 40 (1 event). e186: first sight at 45.7,
  ownership right to refuse.
- Early-interval rate compensation REFUTED on the corpus (leave-one-out regression on t(20->30)
  makes T90 WORSE, 9.6 -> 22 ms rmad: the early interval is dominated by acquisition artifacts).
  Only the gated, once-latched stretch on an authoritative slope deficit >20% is used.

Engine change [ORION_CURVE_MODEL 2026-09-03] (AutomationEngine; kill switch ORION_CURVE_MODEL=0;
applies only while the effective phase constant is within 300-480 ms, i.e. this meter):
1. Curve table t(20->f) in the engine's fill ruler (ladder secants to 40, measured crossings
   beyond); curveTipEtaMs(f, age) = effective phase constant + type trim - t(20->f) - age.
2. Miss path, pre-anchor fill only: a registration-family deadline is HELD
   (`disposition=curve_runway_remains`, release code owned_square_curve_runway) while the curve's
   command ETA exceeds the scheduler grace.
3. Decision arm sites (tick + subtick): `SCHEDULE FIRE CURVE GATE: gate=curve_premature` for a
   sampler-family, pre-anchor candidate when the curve's command ETA is beyond the horizon and
   exceeds the candidate's by > 25 ms (ORION_CURVE_ARM_MARGIN_MS). scheduleFire() is untouched.
4. Phase corroboration compares the sampler crossing corrected by the curve's linear-bias factor
   (0.84 at 20 ... 1.0 at 80) instead of the raw straight line.
5. `TIP PHASE RATE STRETCH:` latched once per shot when the authoritative sampler (n>=5, fill
   15-45) sits >20% below the standard local rate: stretch = 1 + 0.6*deficit, cap 1.40
   (ORION_CURVE_STRETCH_ALPHA).
6. press_unanswered_no_meter is not emitted for physical_epoch 0 (the e32 duplicate).
Tests: curveModelTableMatchesTheLadderAndIsMonotone, registrationDeadlineContradictedByTheCurveIsHeld,
curvePrematureArmIsRefusedButAFastMeterIsNot, slowMeterLatchesOneRateStretchAndTheCurveStopsFalseVetoes,
curveCorrectedCorroborationAcceptsAStandardMeterAtLowFill.

Game-side (Sol, 2K27 menus): the slower-meter lever is MyCAREER > Animations > Jump Shot Creator >
Release Speed (Quick / ~3/4; recalibrate the lead after changing it). Risk-Reward timing profiles
were removed in 2K26 and are not in the 2K27 material seen so far.

Review round (Sol #52/#53) folded in: curveTipEtaMs is anchor-correct (adds t(20->anchorPct), so a
base-30 engine is not 58.3 ms early); the stretch scales ONLY the physical component
(constant + (constant - aim offset) x (stretch - 1); 393/319 keeps the 74 ms aim offset); the
latched factor is logged as rate_stretch= on both reservation_promoted lines; the arm gate is a
direct ratio (candidate < 0.5 x curve command ETA and short by > margin), independent of the
scheduler horizon. Full OrionNativeTests 577/0/8 (Claude) and 579/0/6 (Sol);
scheduledFireConfirmCompletesRelease is a wall-clock quantization flake (system_clock double vs
QDateTime integer ms on Windows), unrelated.

Reader [ORION_READER_BOX_WIDTH_GATE 2026-09-03] (simple_meter_reader.py, default ON; v3 after
Sol's reviews #55/#57): once >= 8 accepted reads exist, a box whose width/height RATIO (served box,
or the lifecycle-ACCEPTED fresh proposal when one was accepted this frame -- never a refused
teleport proposal) exceeds 1.35 x the session median ratio is a REJECTED frame (detected False,
fill/coarse/raw 0.0, rejection_reason box_width_implausible, `BOX WIDTH IMPLAUSIBLE` log); wide
side only (legitimate width-17-after-23 frames exist); the ratio is scale-invariant so a
resolution/camera scale change is not "wide"; 3 consecutive hits retire the lock so the detector
re-acquires (ORION_READER_BOX_WIDTH_RETIRE_N). Deliberately NO consecutive-hit history reset
(Sol #59: it would fail open on a persistent false geometry; the ratio already handles scale).
Knobs _GATE / _RATIO / _MIN_N. Live e50 (39-41 px vs 25-28, ratio 0.32 vs 0.21) is the case.
Tests: tests/test_simple_reader_box_width_gate.py (10: fail-closed, transition on the first wide
frame, refused-teleport immunity, retirement, scale invariance, one-sided, knobs). e174 (quarantine delay after a full-absence
retirement) is documented, not changed: it needs a framedump-replay-backed lifecycle change.

Deploy state 09-03 11:35: native_orion/build/Release/OrionNative.exe rebuilt with the curve model;
models/tip_registration.json = 2K27 (Sol); reader gate in the working tree. Relaunch with the
launcher (-Framedump -NoElevate) to pick all three up. Grade the next batch on: no-fire count vs
7/161, `curve_runway_remains` / `curve_premature` / `RATE STRETCH` line counts, and the landing
distribution from the pixel plateau (scratchpad session_sequence.py), banners ruling.

## 09-03 afternoon: the seven external reviews, tested on our own data

The owner ran docs/PROMPT_external_review_timing_residual.md through seven models. Their decisive
tests were run here (scratchpad paired_landing_duration.py, tail_decompose.py, inline scripts):
- Bookkeeping: the fire is at fill 41.5 (promotion fill 22.2 + 105 ms ETA), not at the 20% anchor.
  The relevant post-fire duration is 41.5->97 (~217 ms to 90, rMAD 4.2 ms = 1.9%), so "the cause
  is bigger than the effect" does not apply.
- Paired test (engine peak_fill vs the same shot's 60 fps durations, n=72 core / 137 landings):
  landing vs post-fire duration r=-0.25, slope -0.009 pp/ms (the animation model predicts -0.22);
  regressing it out moves rMAD 1.80 -> 1.74. Pre-fire duration vs landing r=-0.08; fill at the
  command vs landing r=-0.06; pre-fire duration vs fill at the command r=-0.06. Meter speed, seen
  from either side of the fire, explains <=3% of the landing variance. The "animation-duration
  variability" inference is NOT supported; the residual is downstream of the command and
  uncorrelated with anything the reader sees.
- Shape: engine core landings excess kurtosis +0.09 (95% -0.94..+1.27), Gaussian-like; not the
  flat top a uniform 16.7 ms poll wait would give.
- Estimator: with pre-fire duration uncorrelated with the landing there is nothing for a
  per-shot scale estimator to recover (GLM's 3-5x claim assumed the fire 135-240 ms after the
  anchor; it is ~105 ms and the data show no persistence into the landing).
- Press->20% crossing: median 769 ms, rMAD 58 ms (animation branching by shot type), r=+0.09 with
  the landing: no shared per-shot latency component.
- Pixel-plateau caveat: the ~9 fps framedump plateau misreads LATE shots (bounce descent) as
  early and merges consecutive shots; use the engine's peak_fill for shape/correlation work and
  the pixels only for medians.
Net: no software-side goldmine for the per-shot core. What remains: (1) the ORION_DEV_FIRE_OFFSET_SWEEP
calibration batch (uniform:-8:8, dev build only, banner-graded) to measure the plant gain: if 1 ms of
command does not move the landing ~0.2 pp, the console quantizes and a slower Release Speed is the
lever; (2) the tails, which today's engine/reader/template changes target; (3) ship.

## 09-03 15:00: Release Speed at minimum did NOT change the meter (k = 0.998)

Owner set the Jump Shot Creator Release Speed all the way down and shot 21 presses (session
20260903_145810). 60 fps detframes, 27 clean rises: 20->30 57.3 ms (ref 56.9), 20->41.5 114.2 (ref
117), 30->90 276.0 (ref 277.7), 20->90 333.9 (ref 334.5). Engine: fill at fire 40.7, anchor->freeze
342 ms, landing median 96.2 -- all unchanged. Either the setting does not apply in the mode being
shot (Jump Shot Creator is the MyPlayer's animation) or it does not alter the meter's fill time.
No rescale applied; ORION_METER_TIME_SCALE stays 1.0 in the launcher.

Prepared anyway (in the tree, tests 578/0/8): [ORION_METER_TIME_SCALE] env knob scaling every
compiled meter-time constant (base20 shift, ladder offsets, learner band, phase seed, curve table
offsets, curve rates, curve applicability; aim offset and lead untouched) and
tools/timing/rescale_meter_time.py (--measure detframes.csv -> k; --apply k rescales learning.json,
idempotent via meter_time_scale). The Release exe (11:35) predates the knob; rebuild before using a
k != 1.0.

## 09-03 15:19 session: the remaining ABORT classes (owner: "fix the shot aborts before ship")

93 presses / 77 releases so far: 5 live_tip_deadline_missed + 3 ownership_proof_incomplete + 3
non-shot presses. Decomposed:
- Lead-slider artifacts (e60 lead 800, e61 lead 384): the owner dragged the Shot Lead slider
  mid-batch; the intermediate values apply live and every shot during the drag is unschedulable.
  Not a bot bug; set the lead once per block.
- DETECTOR BLIND SPOT at low fill (e77/e89/e93 + e44): first positive read 630-1125 ms after the
  press at fill 40-71 with 12-24 detector_no_meter frames while the meter was visible (viewed
  f02831_0_raw.png: bar at ~15-20% next to the player). Offline, the shipped model at conf 0.05
  returns NOTHING on e89 +851/+952 ms and 0.17 on e77 +609 ms: a training gap, not a threshold.
  First sight at 40-70% => ownership refused / unschedulable_lead. Handed to Sol: hard-example
  mining from the three 09-03 framedump sessions + fine-tune of meter2k27_n3_pill, and pricing
  ORION_METER_DETECTOR_CONF=0.20/0.25 (new env knob in meter_detector_yolo.get_locator(),
  default 0.35) on the replay harness. This is the biggest remaining abort class.
- e71: a promoted phase token killed by a +7.3 pp mid-shot ruler step 19.4 ms before its fire
  (fill_ruler_transition fence, imminent window 16.7 ms), then re-armed from registration ->
  unschedulable. Fixed both sides: reader [ORION_METER_SUBPIXEL_RULER_LOCK] (default ON) keeps
  the provisional session ruler for the whole shot, the completed seed only feeds the NEXT shot's
  history (tests: test_ruler_lock_keeps_the_provisional_ruler_for_the_whole_shot +
  test_without_the_ruler_lock_the_seed_re_emits_its_own_ruler); engine imminent-forward keep
  widened to 2 frame gaps (kImminentForwardKeepFrameGaps). Engine suite 577/1/8 (the 1 = the
  known wall-clock flake), reader suites green.
Deploy: the Release exe in native_orion/build is the 11:35 build (curve model, no meter-time
scale, old fence window); a staged build with everything above lands in
native_orion/build-timing/Release/OrionNative.exe and must be copied over while the app is closed
(or rebuild native_orion/build then). The reader/detector changes are picked up at relaunch.

## 2026-09-04 — Independent Venice launch-readiness recheck

**Decision: needs changes; 95% human-banner consistency is not established.**
The Downloads handoff predates material September 4 work. The canonical n4 detector,
clock/source-identity changes, and slider apply-on-release logic already existed at
the start of this pass. No bridge, live gameplay, controller actuation, signing-key
change, model promotion, threshold adjustment, or commit was performed.

### A. Detector table — fresh same-pool comparison

Runtime locator, CUDA, 960-pixel FP16, IoU >= 0.30; 461 held-out positives and
2,072 negative images, including the additional 652 ineligible frames:

| Model / confidence | Positive hits / 461 | False positives / 2,072 |
| --- | ---: | ---: |
| n3 / 0.20 | 448 | 0 |
| n3 / 0.25 | 446 | 0 |
| n3 / 0.35 | 444 | 0 |
| n4 / 0.20 | 461 | 3 |
| n4 / 0.25 | 461 | 2 |
| n4 / 0.35 | 461 | 1 |

Independent QC: 100/100 sampled positive boxes are real meters across 23 sessions;
this does not validate exact numeric fill estimates. Four true held-out sessions do
not overlap training. Train/validation overlap 19 sessions but not shots. The n4 run
has only 9 completed epochs, not 25; a prior resume stopped during epoch 10.
Eleven negative rows have ambiguous generation/timestamp provenance, 16 menu negatives
fail the no-press +/-1.5-second criterion, and three held-out sessions lack historical
press-log coverage. The 2,072 images do not certify 2,000 time-qualified gameplay negatives. See
`.codex_artifacts/venice_20260904/detector_audit.md` for the complete limitations.

The 0.35 false positive is a park billboard timer. With the exact production profile
and DirectML, repeating its static image produces n4 positive fill on 58/60 unarmed
and 59/60 simulated-armed frames; n3 produces none. On 21 distinct surrounding recorded
frames at their original cadence, both produce zero positive fills. This is a
static-input robustness defect, not a demonstrated native false fire.

A frozen compact-cap candidate removes the billboard while keeping all 461 low-fill
held-out positives, but rejects 10/311 original Arrow2 ground-truth crops. Composed
white/rail gates also reject 29/48 Pill crops; GT-crop results are not predicted-box
recall. Cap-check overhead measured median/p95 1.13/3.25 ms. No guard was promoted.
Next: independent all-style predicted-box and temporal negative tests, or a separately
trained hard-negative candidate with a genuinely unused test split.

The exporter itself is repaired: repeated exports of best.pt formerly reused
best.onnx, so the 1280 export could overwrite the 960 file before being renamed.
Each resolution now exports in an isolated staging folder; publication occurs only
after all exports succeed. Publication failures restore prior files; an unsuccessful
restoration retains recovery backups and reports their directory. Six mock-only
regression cases pass; no real checkpoint or shipped ONNX was rewritten.

### B/C. Build and clock evidence

The standard baseline passed 980 Python cases and all 18 native CTest targets. The
current executable's 280 core-DLL imports resolve to colocated DLL exports. The old
build-timing directory is gone, so EXE-only ABI mismatch remains a hypothesis for
the historical failed startup, not a proven diagnosis. No live startup was attempted.

The already-repaired scheduledFireConfirmCompletesRelease passes 10/10 and the
clock suite passes 12 cases. A fresh StrictSecurity attempt passed 996 Python cases
and all 18 development targets, then failed the production OrionNativeTests target
without assertion output. Eleven explicit-log reruns each passed 570 cases with 16
skips, and the exact CTest target retry passed. The original failure remains unexplained;
it is not erased by retries. Preserve explicit QtTest failure logs on the next full gate.
The current signing environment has no ORION_UPDATE_SIGNING_KEY_PEM, and prior strict
packaging also failed closed without the existing trusted signing key. No signed
production package is approved by this pass.

### D. Corrected abort ledger

Both abort line families are counted and deduplicated by physical epoch:

| Session | Presses | Releases | Landing records | Unique abort epochs | No explicit terminal |
| --- | ---: | ---: | ---: | ---: | ---: |
| 133124 | 57 | 53 | 53 | 3 | 1 |
| 145810 | 21 | 17 | 17 | 1 | 3 |
| 151910 | 93 | 77 | 76 | 11 | 5 |
| 221630 | 27 | 13 | 13 | 10 | 4 |
| Total | 198 | 160 | 159 | 25 | 13 |

151910 e86 is a real missed shot, not a non-shot: its saved images show the shooting
meter despite detector_no_meter. Only e20/e52 have inspected menu evidence supporting
the old three-non-shot claim, so this session contains at least 9 real logged failures,
not 8. Existing n4 recovers the e86 sequence at first positive fill 15.74% around +471 ms
while n3 emits none; this is a training-session case, not a new held-out result.
The 221630 folder is empty, so its eight no-meter presses are not pixel-certified non-shots.

Local acquisition/ownership cases remain at e44/e55/e77/e89/e93 and 221630 e24.
Local scheduling deltas in these old logs are sub-ms, but console receipt is not
observed. Prior 800/384-ms lead changes are a demonstrated local configuration effect;
the present slider already applies on release. Historical unsigned-underflow-shaped
ACK samples are invalid measurements; current source has checked per-transaction clocks.
These facts refute attributing every failure to the console, but do not identify the
remaining random early/late human outcomes. All 21 stretch events were paired to peaks;
there is no causal unstretched counterfactual or authoritative banner join.

The consistency tool is now repaired: ownership refusals count as aborts, ungraded
releases remain explicit, epoch resets split sessions, exact duplicate press records
do not split them, causal joins reject pre-press/pre-release evidence, explicit release
identity beats the nearest press, and missing/nonfinite/reversed bands stay ungraded.
The new tests fail 15/21 against the original and pass 21/21 after repair; five independent
join probes pass. In the same 27-press old log session this correctly changes accounted
aborts from 2 to 10 and silent presses from 12 to 4; no gameplay outcome changed.

### E. Design decisions — not built

Blind fallback, assuming the listed +/- values are Gaussian standard deviations and
a centered FULL 3.5-pp window at 0.22 pp/ms: the half-width is 7.95 ms. Best-centered
prior-only hit probabilities are Standstill 18.50%, No Dip 7.37%, Left Fade 13.73%,
Right Fade 19.63%, Go-To 9.89%. These are hypothetical probabilities, not observed green
rates; any bias can lower them. The pass-through success rate is unknown, so a benefit
over pass-through is not established. Ownership/deadline gates stay intact.

Slider apply-on-release is already present; any future pending-per-shot setting policy
needs an explicit configuration/epoch test rather than new live per-type constants.
Capture-card absolute delay still needs a synchronized end-to-end measurement; local
write/ACK completion is not console registration. Any owner-recorded comparison uses
recordings and user-visible outcomes only; no competitor binary was inspected.

Define the 95% gate on eligible attempted shots, including real no-fires, with independently
read human banners and explicit unknowns. Keep sessions, shot types, model hash, reader
profile, lead, frame timestamps, and release identity frozen/recorded. Report a confidence
interval and unknown rate, not only successful-release peak-fill spread.

### F. Cleanup / commit plan — no commits performed

Keep the original seven groups: (1) reader/ruler/lock/tests; (2) engine timing/curve/tests;
(3) registration assets/builders; (4) connection resolver/wake; (5) fork transport/clock;
(6) docs/handoffs; (7) offline tools. Add these diagnostic/export repairs and their tests
to group 7. Existing unrelated deletions and dirty changes were preserved. Audit backups,
framedumps, model checkpoints, scratch probes and `.codex_artifacts` are not release inputs.

The bounded knob inventory covers 123 timing/detector names, 26 launcher assignments,
and 17 production profile pins. BOX_PREDICT=1 (code 0, cap20 vs40) and BOX_TIGHT=2 (code0)
remain launcher-only experiments; do not silently promote them to installers. The primary
ruler/base-hold/armed-acquire/precise-wait settings have production defaults or profile pins.

Evidence lives in `.codex_artifacts/venice_20260904/`: `detector_audit.md`,
`timing_audit.md`, `timing_abort_ledger.csv`, `build_test_audit.md`,
`production_native_failure_audit.md`, `candidate_guard_report.md`,
`timing_knob_inventory.md`, and `VERIFICATION.txt`.

## 2026-09-08 — The detector was swapped on 09-04; the post-swap "random earlies/lates" are detection

**Finding.** `models/orion_meter_detector.onnx` (sha256 1e3886af…) was replaced on 2026-09-04 13:50
local by `meter2k27_n4_lowfill/weights/best.onnx` (9-epoch low-fill fine-tune). The shipped detector is
`meter2k27_n3_pill/weights/best.onnx` (sha256 6af3199c…). No log line, handoff entry or bus message
announced the promotion; `meter_detector_yolo._resolve_default_model` had n4 ahead of n3 in the dev
fallback list. Every shot on log-day 09-03 ran on n3; every shot on 09-05 and 09-08 ran on n4.

**Same-frame geometry (190 frames of session_20260903_151910, both models):**
n3 box h 107 px (p10–p90 106–109), w 23; n4 h 121 (116–124), w 26, top-y −7 px, bottom +7 px.
Within-band h sd: fill 10–30 1.08 → 1.95 px; 50–70 1.68 → 3.18; 70–90 1.05 → 5.60 px.
Live detframes agree: det_h 106–107 on all five 09-03 sessions → 120–122 on all five 09-04/05 sessions,
within-shot det_h sd 0.65 → 1.8–2.6 px, read 20→90 crossing 332–338 → 384–398 ms (+17 %, rMAD 3–8 →
8–16 ms). Release Speed was refuted PRE-swap (09-03 22:23, 338 ms), so the +17 % is the ruler.

**Engine effect (`tools/timing/epoch_table.py`, phase arms, within fixed session+lead):**
fill-at-release rMAD 1.0–2.2 pp (09-03) → 2.2–3.3 (09-05, one session 11.3) → 3.7–5.0 (09-08);
r(fill-at-release, landing) −0.5..+0.1 → +0.34..+0.84 → +0.47/+0.72 (slopes +0.16..+0.72);
landing rMAD 1.0–2.0 → 3.3/6.2 pp; TIP PHASE RATE STRETCH 21/320 shots → 179/180 (deficit ≈0.22
every shot, constant 421 → ≈476 ms); TIP SAMPLER RULER RESET 0 → ≈1 per shot; own_first_fill 11 → 17;
anchor rungs 25/30/35 share up. The 09-03 "residual is downstream of the command" result stands for
n3; on n4 the estimator became the dominant term.

**Actions.** Canonical restored to n3 (copy of `meter2k27_n3_pill/weights/best.onnx`, sha verified);
resolver fallback order n3 before n4 with the reason in a comment; memory
`detector-n4-swap-shifted-the-ruler`. All 09-05 and 09-08 timing data is contaminated: never pool it
with 09-03, never tune lead/stretch from it. Promotion gate for any detector candidate: box-geometry
parity with n3 on the same frames (median h ±1 px, top-y ±1 px, within-band h sd ≤ 1.25× n3, recall
≥ n3 − 1 pp) — `tools/diagnostics/detector_geometry_parity.py` (in progress). Astra was stopped by
the owner 09-08 ~17:00; its resumed n4 training (isolated copy under `.codex_artifacts/lowfill-p2-20260908/`)
does not publish and can be ignored or killed. Detection lane now: lock-acquisition latency audit,
n4 label-geometry root cause, static edge-based ruler prototype (agents, reports in the session scratchpad).

### 2026-09-08 evening — detection lane: what landed (all default-ON, each with a kill switch)

Agent audits (reports in the session scratchpad `agent_acq/`, `agent_labels/`, `agent_ruler/`):
onset→first read 105 ms median (13 pp) on n3 — recall is NOT the cause (n3 scores 0.92–0.95 on the
empty meter 18–79 ms before the live first read); it is cadence (27 ms inference + 15 ms gap) plus a
reader-side pan-offset tail (26/329 shots hold a 0.0 lock 82 ms median with the box 13 px beside the
meter). n4's +14 px boxes = labels taken from the reader's OVERLAY box (`mine_lowfill_hardset.py:63,
162-174, 518-523`), not the detector box. The meter's own landmarks (tip apex, chevron-notch apex)
give height 99.80 px with 0.11 px rMAD over 2519 frames; the YOLO box adds ~0.5 pp/frame of fill noise.

1. `meter_detector_yolo.py` [ORION_METER_DETECTOR_GPU_PREP]: blob construction on the CUDA provider
   (Cast/Mul/Cast/Transpose graph, io-bound into the detector). detect_box 27.1 → 11.4 ms on 100 live
   frames, 0 box differences; self-test at load refuses any mismatch; falls back to the CPU path.
   Sidecar log line `METER DETECTOR load: … gpu_prep=True`. Model file untouched.
2. `simple_meter_reader.py` [ORION_METER_SUBPIXEL_NOTCH]: sub-pixel anchor = chevron-notch apex on
   the 2 centre columns of whiteness min(B,G,R); fill edge and core mask on whiteness (red-court
   frames stay sub-pixel); in the green-cap zone green rows are a valid plateau and the width gate is
   4.5 rows; edge-gate failure with a measured notch → integer edge on the notch ruler (`base_int`,
   same 'base' identity). Replay (real reader, real t_ms, SYNC detector) session_20260903_151910 /
   133124: plateau sd 0.589 → 0.291 / 0.210 pp, estimator generations per shot 3 → 1, frames without
   sub-pixel 639 → 32; +0.08 ms per frame. Kill: `ORION_METER_SUBPIXEL_NOTCH=0`.
3. `simple_meter_reader.py` [ORION_METER_DETECTOR_ARMED_HOT]: every frame is a worker-priority
   submit while `_shot_armed_hw`, bounded to ARMED_HOT_MAX_MS=1200 per arm epoch (a stuck arm falls
   back to ordinary cadence). Expected onset→consumed ~64 → ~20 ms; live-unverified.
4. `simple_meter_reader.py` [ORION_READER_RESEAT_X]: a held box reading 0.0 is re-seated on the white
   ribbon found within ±18 px on THIS frame (one run of 0.30–0.85 box width, single vertical white
   run ending in the lower half); the tracker box moves with it. Fail-closed. Live-unverified.
5. `tools/diagnostics/detector_geometry_parity.py` + tests: promotion gate (n3 vs n4 FAILS on h +14,
   top-y −7, band sd 4.83×; n3 vs n3 PASSES). `meter_detector_yolo.py` is UNTRACKED in git — commit
   it with the reader group.
Tests: reader-related 1114 pass; new `tests/test_simple_reader_notch_anchor.py` (9),
`tests/test_simple_reader_reseat_x.py` (3), `tests/test_detector_geometry_parity.py` (10);
`test_stuck_arm_cannot_create_continuous_priority_inference` now also covers the bounded hot window.
Replay harness: scratchpad `replay_reader_eval.py <session_dir> <out.jsonl>` (production flags, real
frame timestamps) — run it before/after any reader change.
Next: live framedump batch → command-fill rMAD (target ≤1.5 pp, was 3.7–5.0 on n4), first-read fill
(15 → <8), zero-fill lock tail (26/329 → ~0), `hot_submit`/`reseat_x` diag counters; then D_session
from the landmarks (apex+H) so the first shot of a session is on the stable ruler from frame one.

### 2026-09-08 late evening — per-shot GAME-VERDICT diagnostic (banner OCR ⨝ engine), first findings

`tools/timing/banner_reader.py scan --session <framedump dir> --out events.csv --crops <dir> --auto-scale`
reads 2K27's TIMING panel (EXCELLENT / EARLY / LATE; colour = severity: white slight, yellow, red) —
verified on session_20260903_133124 (39 events) and 151910 (70). New `tools/timing/banner_join.py
--day <UTC day> --events ... [--logs ...] [--csv ...]` joins each verdict to the engine release
0.2–3.5 s before it (FIFO) and prints per-verdict medians + separation from GREEN in rMAD units.
Analysis-only (owner directive: a banner read never reaches the engine).

09-03 (n3, old reader, leads 287–293), 95 joined shots: GREEN 47, LATE 30 (red 17, white 9, yellow 4),
EARLY 18 (white 12). **Severe (red) lates fired late**: fill@release 54.0 vs green 40.5 (d=+5.0 rMAD),
hold→rel 212 vs 121 ms, travel 41.7 vs 56.4 pp — a late COMMAND class (token kill/held/registration),
not console jitter. Slight (white) lates and earlies are indistinguishable from greens in every engine
variable (|d| < 1: fill@release, anchor→freeze, frame_age, stop_shift) — that is the residual.
Landing (peak) does NOT separate slight late from green on the old reader (97.0 vs 97.1).
Tonight (new reader, lead 285/290/311): only 2/36 command-late outliers (far 67, 51), so the red class
is mostly gone; remaining lates are the slight class. Lead blocks: 285 → landing 93.4 rMAD 1.9;
290 → 94.9 rMAD 1.7 (+12 ms anchor→freeze, frame_age +6 ms: a session-level latency shift, not the
lead); 311 → 92.55 rMAD 0.47 (n=9).
**Blocker for the next step**: framedump and detcsv were silently disabled tonight — disk floor
(free 9.4 GB < max(10 GB, 2 % of 930 GB = 18.6 GB); knobs ORION_FRAMEDUMP_MIN_FREE_GB/_PCT). No frames
= no verdict labels. Next: free disk, batch with framedump, then landing-vs-verdict scatter on the new
reader (if landing predicts the verdict, the residual is pure release-time spread and the window top
in our scale gives the exact aim; if not, the game grades on something the capture does not show).

### 2026-09-09 night — lead sweep, session-fps probe, capture-phase lock (Experiment C) and its bug

Seven labelled batches tonight (banner ⨝ engine, n3 detector, landmark reader): lead 285/290/305/
300/303, session 30 fps and 120 fps, then the phase lock. Findings that held across all of them:
fill@release identical for greens and lates (rMAD 0.7–2 pp); ~50 % green at every lead; the lead only
moves the early/late split; the PC path ends at `CHIAKI_ORION_DELIVERY_UDP_ACCEPTED` within ~1 ms
of the fire; link is Ethernet. Session fps moves the mean only (30 fps freeze +17 ms, 120 fps −10 ms).
By shot type over the 7 batches: Standstill n=98 49 G / 26 L / 23 E; Right Fade 33: 15/13/5; Left
Fade 30: 14/13/3 — the stationary meter carries the same early/late randomness, which refutes any
"stale box on moving shots" mechanism (Gemini #95/#97).
Refuted and retired tonight: press-latency trim (no correlation on a clean batch), colour-only
reader, the pasted 2K Vision red reader (4/86 shots), fork 8 ms pacing (unused constant), sender
thread priority (0.5 ms), YOLO frame budget (11 ms async), sessionLeadProbe (off). Curve stretch was
firing spuriously on early locks (fit on the 16–17 % segment) → launcher pins ORION_CURVE_STRETCH_ALPHA=0.
Phase effect (69 shots, no lock): late rate vs release phase in the capture frame cycle 26→39→43→61 %
by quartile → [ORION_CAPTURE_PHASE_LOCK] (nearest-phase rounding at the scheduleFire choke point,
target 2.0 ms, env ORION_CAPTURE_PHASE_LOCK / _TARGET_MS, log `CAPTURE PHASE:`).

**Experiment C batch (session_20260909_194153, lead 300/303, lock on)**: 27 labelled = 13 G / 9 E /
5 L. The lock engaged on 10 (coherence ≥ 0.90): 5 G / 4 E / 1 L; unlocked 17: 8 G / 5 E / 4 L.
**Bug**: every applied shift was −10.7..−23.1 ms (median −14.6). The fold-back check compared the
locked time against the MEMBER `schedFireAuthorityExpiryMs_` (previous arm's lease, already past;
−1 on the first arm) instead of this arm's local `authorityExpiryMs`, so every lock was folded back
a full period — a hidden +15 ms lead on 10 shots, which is the early swing. Fixed: compares this
arm's lease; a forward rounding the lease cannot cover leaves the deadline alone (no fold-back,
`suppressed=1`), a would-be-past lock likewise (`suppressed=2`); the log line now carries
`lease_eta_ms` and `suppressed`. Regression test `capturePhaseLockUsesThisArmsLeaseAndNeverFoldsBack`
fails on the old code (shift −11.67 for a +5 forward case); suite 790 / 0 / 6. Batch C therefore says
nothing about the phase hypothesis yet — rerun with the corrected lock is the next batch.
Gemini (Venice bus) delivered the Takion fact that buttons live only in FEEDBACK_HISTORY and the
release is sent twice (history + redundant history at +3 ms) → Experiment B = fork toggle
`ORION_CHIAKI_SINGLE_RELEASE` + wire_us log (its diff prints transport_us twice; needs
`chiaki_time_now_monotonic_us()`); deploy = build-orion-optimized-ffmpeg7, verify avcodec-61 /
avutil-59 / swresample-5 imports, overwrite deploy/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe.
Queued: reader candidate gates channel-spread ≤ 25 and vertical-support ≥ 12 (with tests, replay
before/after); transport A/B (official app + ViGEm) if B is not decisive.

### 2026-09-10 — pure-CV proposer A/B, the Gemini direct-trigger incident, Clean overlay

Owner asked to rule the YOLO path in or out. Built `meter_locator_cv.py` (ORION_METER_PROPOSER=cv,
explicit; dev launcher pins it): white achromatic column (V≥225, spread≤25, support 6 px, width 8–22
@720p) + green tip confirmed 100 px above the white bottom (measured 149 boxes, rMAD 1.5); a column
85–108 px tall passes with a washed-out tip (fill >88 %). Box mimics the YOLO box (26 wide, pads 8/8).
Offline vs the reader's boxes: centre 0.0 ±0.7 px, off-box 0 on three sessions, ~1 ms roaming / ~5 ms
full band; the wrapper passes the frame ts to the base (`accepts_ts`) so the ROI hold runs on frame
time. False proposals: 5 single frames in 1,487 no-meter frames (nameplate under the green ball, menu
text, a player); an outline gate (two light edges beside the column, best-edge support ≥0.45; real
median 0.98) catches all five but with a one-frame confirmation it moved the replay's first read
from 15 to 40 % on the SPARSE framedump — left OPT-IN (ORION_CV_OUTLINE_MIN=0.45) and priced live via
`cv={outline_weak=..}` in DETECTOR HEALTH. Do not loosen the gates to V220/support 4/width 7–24
(false locks) and do not add a "green above the tip" guard (hot-streak cloud kills real tips).
Live: 01:25Z batch (CV, phase path, lock off) 27/27 timed, 18 labelled = 11 G / 5 E / 2 L (yellow),
late 11 % vs 30–40 % on YOLO batches (owner moved the lead 303→263 mid-batch; n soft).
**Incident:** Gemini (Antigravity) edited engine/AppConfig/tests/reader/detector/profile/launcher/
settings at 21:39–22:21 local and rebuilt: a "2k_Vision Pure CV Direct Trigger" fired 9/9 shots
reactively at fill ~82 with a 45 ms lead (no TIP RESERVATION) = "bot doesn't time shots". All of it
reverted (engine 790/0/6, reader+detector 552 pass); launcher pins for CAPTURE_PHASE_LOCK,
CHIAKI_SINGLE_RELEASE (fork never rebuilt) and CV_DIRECT_TRIGGER removed; its MIN_INTERVAL 0 for
cv-contour kept. Rule restated on the bus: diffs only.
**Overlay:** style "Clean" (1 px stroke, 3 px gap, no keyline; HUD one colour/size, still FILL/TIP/
FIRE) — default in AppConfig, set in settings.json with colour #F2F2F2, re-signed.
**Agents (owner-authorised):** Meter Detection card wiring (setting `meter_proposer`, sidecar env,
detector health telemetry → `detectorHealthLine`) and `tests/test_meter_locator_cv.py` + review.

### 2026-09-10 evening — Shot Lead card, Meter Delay × Shot Lead, Meter Detection card, locator review

- **Shot Lead card**: the slider now shows a 100 ms window around the current value (1 ms steps;
  field and ±1 buttons still reach 150–800). Under Meter Delay a note states what is added.
- **Meter Delay works WITH the lead** ([ORION_METER_DELAY_LEAD_AUTO]): offset setting 0 = AUTO —
  the engine adds the APPLIED delay to the user's Shot Lead, clamped to the schedulable headroom
  (ceiling − lead − one frame). A manual non-zero offset still wins verbatim. Controller exposes
  `meterDelayMaxUsableMs` (= ceiling − lead) and `meterDelayLeadOffsetAppliedMs`; the Meter Delay
  card shows "Usable with Shot Lead X: up to Y ms" and warns past it. Physics: the visible meter
  lags the grade clock by D, so the release must come D earlier (the 08-12 D=200/offset-90 test
  landed 61 % late = short by ≈ D). The 08-09 "not a curve" note was a data decision, not physics.
  VeniceNetSvc IS installed on the rig (demand-started, "authenticated packet bridge"); the delay
  was simply disabled in settings today (meter_delay_enabled=false, applied 0).
- **Meter Detection card** (agent): setting `meter_proposer` (cv|yolo, default cv) → sidecar env
  ORION_METER_PROPOSER (dev env may override; production pins the setting); sidecar emits
  `detector_health` every 2 s → `detectorHealthLine`/`detectorProvider`; Style locked to Arrow2
  under Pure CV. Applies at the next sidecar launch.
- **CV locator review fixes** (agent B's list, all applied): mirrored morphology anchors (even
  kernels walked the white bottom 1–2 rows), outline bands moved off the fill column and scaled,
  tile hits stored in full-frame coordinates, width gate before the top-12 cap, confirmed tip
  out-ranks the meter-tall fallback, pending sightings cleared on a miss and capped at two frames
  of tolerance, clock never regresses, `reset()` propagated from the wrapper, `error` counter,
  pad_bot 10 (dh 0 vs the reader's tracked box). tests/test_meter_locator_cv.py 31 tests.

### 2026-09-10 late — Shot Lead 1..100 scale, simplified cards, residual EARLY/LATE workflow

- **Shot Lead card is a 1..100 knob** (owner: "1-100 ONLY"): value V ↔ actuation_lead_ms =
  150 + (V−1)·250/99 (≈2.5 ms per step; 1 = 150 ms, 100 = 400 ms — the whole schedulable band on
  2K27, ceiling ≈ Tip Timing − 30). The engine and settings stay in ms; only the card converts.
  Live readout while dragging; hint at the bottom: EARLY → lower, LATE → raise (higher = releases
  earlier). Conflict banner speaks in the 1..100 scale too.
- **Cards cut to the essentials**: Meter Delay = Enable + slider (+ one usable-max line and the
  service status); Meter Detection = Style/Color (locked) + AUTO DETECTION + health line; the YOLO
  picker is gone (YOLO shelved; `meter_proposer` setting still exists, default cv).
- **Tonight's CV batch (session_20260910_195958, owner swept the lead 175→268 mid-batch)**: 18
  labelled = 4 G / 11 L (6 red, fill@release 46 vs 40 = late-command class again, hold→rel 158) /
  3 E. Red lates sat at leads 228 median — the low-lead part of the sweep.
- **Workflow launched** (owner-authorised): 9 angles (poll phase, late-command class, fill scale
  drift between proposers, reader cadence, engine internals, shot type, capture timing, Chiaki fork
  path, statistical skeptic) over `scratchpad/workflow/verdict_dataset.csv` (214 labelled shots,
  10 batches), each finding refuted by two independent agents, then a ranked PLAN.md.

### 2026-09-11 — harvested workflow findings, consensus fix, CV pads, and the INFLATED MEASUREMENT BOX

- The 44-agent workflow hit the session limit; 10 agents finished. Harvested (scratchpad/workflow/
  FINDINGS_harvested.json): phase-lock premise does NOT replicate (n=203; the 26→61 % figure was a
  t_rel binning artifact) — lock stays off; the duplicate release datagram is NOT the spread (wire
  gap 3.5–3.7 ms in every class, n=53); two rulers (YOLO tracked box vs CV box) put the engine's
  rung table/tip constant on different physical fill; refused witness-spread consensus marks a
  67 %-late population; fades ~7 ms shorter phase; stretch caused 5/12 severe lates in 0909a/b.
- Applied: [ORION_ANCHOR_CONSENSUS_APPLY_ON_SPREAD] default ON (env "0" = legacy refusal; tests
  updated); CV pads 4/3 (replay rise RMS 8.0→3.2 ms, landing 87.6→90.1; YOLO 93.6); stretch
  compiled default stays 0.6 (46 mechanism tests) — the launcher pin ORION_CURVE_STRETCH_ALPHA=0 stands.
- **Live game 11:28 (practice 29 shots, game 4 timed + 1 layup, owner 1 make):** practice at lead
  299 (value 60) = 6/8 green; game windows read 97–99.7 (contested, narrow) vs 90.5–96.5 in practice.
- **Plain-sight defect found in detframes.csv:** the reader MEASURES in the emit-smoothed tracked
  box, whose dims drift from the detector's: CV boxes 107–110 px, measured boxes 118–121 px on
  33/36 practice shots and 130–145 × 30–44 px on game shots ("inflates at fill 14–21, stage=track").
  denom = bh−1 → a 9–30 % per-shot ruler change. Replay (sync) does NOT reproduce it (diff 0), so
  it is a live async/coast/scale-match effect. Fix [ORION_READER_LOCK_BOX_DIMS] (default on for
  cv-contour): measurement and display dims pinned to the fresh detector box (≤0.35 s), tracker
  keeps x/y bottom-aligned; counter `dims_locked=` in DETECTOR HEALTH and the health snapshot.
  LIVE-UNVERIFIED — next batch: served h must equal det h in detframes.csv.
- Small 3-agent "plain sight" sweep workflow launched (engine / sidecar / io partitions).

### 2026-09-11 — three-sweeper "plain sight" workflow (engine / sidecar / io): what it found

Full findings: scratchpad/workflow/SWEEP_findings.json (and agent_out/). Applied today unless noted.
- **Press-latency trim contaminated batches 0909a/b/c**: the 09-09 binary ran the trim ON by default
  (cap 30, not positive-only) and displaced 19 shots by up to ±30 ms, incl. two manufactured red
  verdicts (0909a seq19 +13 → LATE-red, seq23 −30 → EARLY-red). Off since 09-09 21:13Z. Applied:
  the four ORION_PRESS_LATENCY_TRIM* knobs (+ CAPTURE_PHASE_LOCK*, CV_DIRECT_TRIGGER) join the
  launcher scrub list. TODO: `trim_ms` on the Release timing line; drop/stratify the 10 trimmed
  rows when re-using 0909a–c.
- **Rung table vs CV ruler**: old pads 8/10 put anchor-25/30 shots +4.3–8.5 ms LATE (a between-
  anchor-level bias inside a batch); pads 4/3 measured live today leave ≤0.7 ms at 25, +0.3 at 30;
  the c30 consensus retarget is alive again (0/23 refused vs 116/116 the day before). The sidecar
  sweeper: the last 3.5 pp vs YOLO is the denominator (D 109 vs 105); pads 1/2 would give exact
  parity but only 1 px of air over the tip — kept 4/3 (constant offset absorbed by the lead).
- **Fire path is clock-consistent end to end** (sample time = capture time, converted once at
  receipt; precise-fire deltaMs median 0.00 p99 0.03 ms; no mixed clock or rounding adds a frame).
  The CAPTURE PHASE "coherence collapse" is an instrument artifact (hard-coded 16.667 ms vs the
  card's 16.64–16.65 ms period).
- **CadenceLock false drop** (capture_card_backend.py): a read returning 12.5–20.8 ms late was
  accepted as a 2-frame drop and the next ~5 frames stamped +16.6 ms until relock (0.5/min live,
  1–2 shots per 50). Applied: k≥2 acceptance is PROVISIONAL — if the next read lands (k−1) periods
  behind the advanced grid the lock steps back at once (`skip_reverts` counter). Synthetic check:
  a 14/20 ms late read now poisons only itself (−0.2 ms after), real 1/2-frame drops unchanged;
  37 capture tests pass.
- learning.json learned_phase_physical_ms=289 (base-30) is ~20 ms longer than the phase measured
  today (frozen; absorbed by the lead) → unfreeze for ONE session on the CV ruler, then refreeze
  and re-seed the lead (bookkeeping, not a spread fix). captureAgeLeadCapMs=20 clips the sampler
  path only. Capture device latency (~35 ms) is assumed, not measured → between-session steps.
- **Still the top live candidate for the random split: the inflated measurement box** (see the
  previous section) — LIVE-UNVERIFIED until the next game's detframes show served h == det h.

### 2026-09-11 afternoon — game 2 (a lot better), the aborts, and a CORRECTION on the "inflated box"

- Game 2 (session 120503, lead 286 = value 55): practice 18 G / 10 L (8 white) / 3 E; game 5 timed
  shots = 4 green + 1 white late on a Right Fade (owner: "only random late was a middy fade, no
  random earlies/lates"). 12 unowned presses: 8 no-meter (layups/taps) + 4 `ownership_proof_incomplete`
  = the "2 aborts on wide-open shots".
- **CORRECTION**: the "measurement box inflated to 120/139 px" finding was the DISPLAY box
  (`_tight_display_box`, BOX_TIGHT=2, side reach 18 → w up to 44), not the measurement box; the
  replay showed measurement == detector. `ORION_READER_LOCK_BOX_DIMS` stays (harmless, pins both).
- **The aborts' real mechanism (frames 5020–5040 / 5837–5858):** the live reader locked a wrong
  object at the press — first a colour-tier seat at y≈440 (CV proposer returned None on those
  frames), then a nameplate-like bar at y≈576 (w 26, fill stuck at 12) — while the real meter rose
  at y≈280 (the standalone CV locator and the sync replay both track it). Ownership proof restarted
  on the false samples and the user's own release went through. Two fixes:
  (1) `ORION_READER_DETECTOR_ONLY_SEAT` (default on for cv-contour): a colour-tier candidate that no
  fresh detector box (≤0.12 s) overlaps at IoU ≥ 0.3 may not seat (`det_only_veto=` counter);
  (2) the outline gate is now ON in the launcher (`ORION_CV_OUTLINE_MIN=0.45`; nameplate/text/jersey
  candidates score ≤ 0.36 vs real meters 0.98; one-frame confirmation for outline-less sights).
  Both LIVE-UNVERIFIED: watch `det_only_veto=`, `cv={no_outline=,outline_bridged=}`, first-read
  fill, and the count of `ownership_proof_incomplete` in the next game.
- Owner propositions logged: aim at the middle of the green window (engine has
  autonomousGreenCenterFrac / ORION_GREEN_CENTER_FRAC but memory says it is inert on the subtick
  arm — parity fix needed first; the in-game window reading (green band 97–99.7) also differs from
  practice, so calibrate before trusting it); C++ OpenCV port (timing uses capture stamps, so
  processing latency does not move the release; CV locator 1 ms, reader 2.5 ms — CPU headroom only);
  drop the +3 ms redundant release datagram (measured no timing effect, n=53; needs a fork rebuild).

### 2026-09-11 15:20 — outline gate REFUTED live; seat veto kept; shutdown crash fixed

- Sessions 13:16 and 13:48 (gate ON): 8 ownership aborts; frames around them show the reader's
  box jumping across the screen (y 214→191→410→569→265) with the fill flapping 88→24→10→75: the gate
  refused the real meter on live frames and the locator's fallback candidates (conf 0.75 meter-tall
  bars) won. Gate-off sessions (11:28, 12:05) never did this. `ORION_CV_OUTLINE_MIN` is now pinned
  to 0 in the launcher with the live evidence; do not re-enable without a different mechanism
  (e.g. only refuse a candidate when a BETTER outlined candidate exists in the same frame).
- The detector-only seat veto stays (READER ACQUIRE seats 64 → 0). The 26 "unanswered" presses of
  13:16 were genuine no-meter presses (post moves / fakes: a meter visible after only 3/26).
- Shutdown crash (0xc0000409, two dumps: 00:21 and 11:46): std::bad_alloc thrown from Qt on the
  precise-fire thread during teardown → uncaught on a std::thread → abort. Fixed: run() wraps
  runLoop() in try/catch (fail the mailbox closed, keep serving, no Qt in the handler), the worker
  is joined FIRST in prepareForApplicationExit, and Release builds now emit a PDB (/Zi, /DEBUG:FULL).
  The 15:19 close went through cleanly.
- Session 13:16 graded: 5 green / 1 early / 1 red late of 7 banners.
- **Second shutdown crash (15:19, with PDB):** main thread, `QObject::~QObject` of the controller →
  `removePostedEvents` → `~QMetaCallEvent` → `QSemaphore::release` on a dead semaphore: the
  controller was declared AFTER the QML engine in main() so it died first, while the engine, its
  windows and the render thread were alive and a blocking cross-thread call to it was still queued
  from a thread that had already exited. Fix in main.cpp: controller constructed before the engine
  (destroyed after it) + a 3-pass sendPostedEvents/processEvents drain after QApplication::exec().
  Compiles; LIVE-UNVERIFIED until the next close (check WER Application log for OrionNative 1000/1001).

## 2026-09-11 16:55 — "random controller disconnects" = silent-but-enumerated USB pad (EPM=1 port) + 2.5 s teardown

Owner report: the controller randomly disconnects mid-play ("especially on dark or static screens"), and the
TIMING WARMING UP banner shows in menus. Evidence (logs/orion_native.log + .log.1, all UTC):

- 5 "Physical pad unavailable - virtual pad torn down" events this session (20:52:13, 20:52:21, 21:07:18,
  21:07:23, 21:19:07) and 12 in the rotated log (burst 20:07:53-20:09:48, then 20:15, 20:22, 20:23, 20:30,
  20:39 x2). Only the LAST one (21:19:05) had a GIDC_REMOVAL ("Physical controller removed") before it - the
  owner unplugging the pad at the end. The other 16 are the poll's silence path: the raw-input worker
  (`OrionRawInputWorker`, stamps `lastReportMs` on EVERY decoded Sony report, no dedupe) received nothing for
  > 750 ms, `controllerSelector_.select()` went inactive, and after `physicalMissingTeardownGraceMs_`
  (2.5 s armed) the virtual ViGEm pad was disconnected -> Chiaki -> the PS5 shows a controller drop.
- The silences are 4.8 s, 51.7 s, 4.0 s, 10.4 s (measured from the 1 s "Input hook heartbeat" cadence, which
  only runs while the selected pad is live) and the pad stayed ENUMERATED throughout ("Physical detected,
  waiting for input report"; Kernel-PnP/Configuration logged nothing; no GIDC events). Idle is NOT the
  trigger: 89 idle stretches >= 8 s in the rotated log (longest 923 s) and 13 in this one (107 s) kept
  streaming reports with no teardown; the 21:07:15 silence began while the owner was actively moving
  (hook writes +91 in the last second). The "dark/static screen" observation is reverse causality: the
  teardown pauses the game.
- The pad was on a NEW USB port today (HID instance 8&28E0002D -> USB `VID_054C&PID_0CE6&MI_03\7&17e45d63&0&0003`,
  location 0002...005) whose `EnhancedPowerManagementEnabled` is still the default 1. Four of the nine
  DualSense port entries are EPM=1; the five on the usual port are 0. The owner pressed Fix Controller at
  20:09:47Z and it FAILED ("open failed ... Start Venice through its launcher (administrator)") because the
  launcher runs `-NoElevate`. This is exactly the failure class the 2026-08-08 pad gate note documents
  ("enumerated + silent = USB selective-suspend/resume artifact ... a wake probe can clear").
- HidHide is installed but cloak OFF with no devices/apps -> not a factor. Lightbar is Solid (writes only on
  colour change) -> not a factor.

Shipped (built 16:49, OrionNativeTests 792/0/6, all 18 test binaries rc=0, relaunched 16:52 via the launcher):
1. `ControllerRoutingPolicy.h`: `silentPadTeardownGraceMs(enumerated, armedOrOwning)` — enumerated+silent
   holds the (already neutral) virtual pad for `kSilentEnumeratedPadHoldMs` = 30 s; not enumerated keeps
   2.5 s armed / 8 s idle. Test `silentPadTeardownGraceHoldsAnEnumeratedPadNeutral`.
2. `OrionAppController`: the poll's silence onset uses that grace; at 400 ms of silence it calls the new
   `nudgePhysicalPadOnce()` (the open + HidD_GetInputReport half of `wakePhysicalPadAndConfirmReport()`,
   now shared) once per episode and logs "Physical controller enumerated but silent for N ms - nudged the HID
   collection (open= report_poll=); virtual pad held neutral for up to 30000 ms". The GIDC_REMOVAL handler pins
   the unplug grace explicitly (it used to inherit the previous episode's value). Onset actions
   (disarm, revoke route attestation, engine reset, neutralize) are unchanged - no gate weakened.
3. `tools/diagnostics/fix_dualsense_usb_power.ps1`: self-elevating (one UAC prompt) EPM 1 -> 0 for every Sony
   pad USB entry; the same registry change as the in-app button. Owner must run it and re-plug the pad.
4. `native_orion/src/main.cpp` shutdown-order fix is now LINKED (the 16:30:27 close crashed again in Qt6Core,
   c0000005 at +0xdd3e, WER 1000/1001). Verify on the next close.

Warm-up banner "TIMING WARMING UP - SHOTS STAY MANUAL": bound to `latencyCalibrationReady` =
`autonomousLiveMeterReady()` = `measuredLeadAuthoritative(now)`, which needs the measured/user lead's route echo
(`measuredLatencyScopeEpoch_ != 0`, route == attested route) and a lead telemetry refresh within 250 ms.
`automation_.reset()` (called at every silence onset and pad removal) zeroes the scope epoch and the authority,
so after each disconnect the banner is TRUE until the sidecar re-stamps the lead on the regenerated route -
shots really are manual then (`liveLeadReady=0` -> SHOT NOT OWNED). It is a symptom of the disconnects, not a
separate bug; nothing changed there.

Still open: the two gate-off aborts (20:44:34 wide first box w=50 + non-monotonic fill; 21:01:01 empty frame
window) - grading agent running on the 15:19 session.

### 2026-09-11 17:20 — session 0911d graded (agent; outputs scratchpad/agent_out/grade_0911d, join copied to scratchpad/banner/joined_0911d.csv)
- Framedump `session_20260911_151926` stops at 12000 frames (ORION_FRAMEDUMP_MAX) = 20:49:36Z; the last 41 min of
  play (incl. the 21:01:01 abort) are unverifiable. Raise the cap or rotate dumps if whole sessions must be graded.
- INSTRUMENT DEFECT: `banner_reader.py scan` only finds the 1-cell TIMING box; it missed every 3-cell
  TIMING|COVERAGE|DISTANCE and TIMING|FT-RATING panel on The Theater court (tool join 1/10). The agent read the
  10 in-dump releases by eye from contact sheets and joined with the unmodified banner_join.py.
- 9 graded: GREEN 5 / LATE 4 (red FT, yellow FT, white x2) / EARLY 0. Jump shots only 5 G / 2 white-late (71%).
  First session where the bot released FREE THROWS: both late (FT meter ~1.6x faster than the jump-shot curve).
- Aborts: all 9 have release_seq=-1 (never owned): 7 square_early_release on 93-130 ms taps (the owner released
  before 3 rising frames), 1 pending_stick_fault, 1 stamp_missing. The 20:44:34 wide-open abort (ep61) was a
  320 ms press under a jumbotron camera cut: garbage reads 12-85% on 5 boxes, no panel. 5 confirmed no-fires
  where the owner's own release produced a panel (2 G / 2 L / 1 E).
- 10 releases in the first 15 min, 3 in the next 55 while presses continued; engine self-grade matched 3/9.

### 2026-09-11 18:55 — controller injection Q + "Options / touchpad don't work"
Route recap: physical DualSense via Raw Input (dedicated pump thread) -> `OrionInputClient` named-pipe hook into
the Chiaki fork (`gui/src/orioninputbridge.cpp` copies `pkt.buttons` verbatim into ChiakiControllerState; the
bitmask IS chiaki's enum, `feedback.c` encodes OPTIONS 0xac/SHARE 0xad/TOUCHPAD 0xb1/PS 0xae) -> Takion
FEEDBACK_HISTORY. ViGEm X360 pad is held neutral while the pipe owns input; fallback only. HidHide cloak off.
- Options (byte 9 bit 0x20 -> START -> PS_OPTIONS bit 12), Create, PS (byte 10 bit 0x01 -> GUIDE -> bit 15) are
  decoded and mapped end to end; no masking found on either side. Unverified live (pad unplugged).
- TOUCHPAD CLICK WAS NEVER ON THE WIRE: `decodeSonyReport` fills `ControllerState::touchpad` (byte 10 bit 0x02)
  but `mapButtons()` only reads the XInput word. Fixed in `OrionInputClient.cpp` (`if(state.touchpad)
  p.buttons |= PS_TOUCHPAD;`). OrionInputProtocolTests 43/0, OrionNativeTests 792/0/6.
- New edge-only log line "Special button edge: options= create= ps= touchpad= route=pipe|xusb hook=" in
  `pollPhysicalController()` so the next session proves decode + route for each press.
- Shutdown fix EVIDENCE: the 16:52 instance (first build with the main.cpp order fix) was closed by the owner at
  17:38 with NO WER 1000/1001 - the first clean close after the 15:19/16:30 crashes. Rebuilt 18:52, relaunched
  18:53 via the launcher.

### 2026-09-11 20:20 — the aim constant was the mover; Tip Timing card UNRETIRED + frozen
Owner question: "is there anything moving the values that should be frozen in place? ex my lead is 60".

ANSWER ON THE LEAD: no. Three writers exist — the measured seed (locked out by
`actuationLeadAcceptsMeasuredSeed`: needs !userSet && !(lead>0), and settings has
`actuation_lead_user_set: true`), `reportLeadCalibrationVerdict` (only while the calibration wizard
runs) and `nudgeActuationLeadMs` (Q_INVOKABLE, called from NO qml — dead). Every change tonight has
an owner-action log line ("Shot lead set to 274 ms (your value; measurement will not change it)").
The 286/299/261/274 values in the log are all slider moves.

WHAT WAS ACTUALLY MOVING — the effective aim constant, measured on the 19:38 session (98 shots):
`Tip phase measurement: effective_const_ms` walked **388.4 -> 369.5 ms over n=3..15, monotonic**,
while lead_ms sat at 299. Shot Lead is a constant offset against a moving target, which is exactly
why tuning it never removed the random earlies/lates. Two faults stacked:
  1. `learning.json` was ZERO BYTES (created empty 19:32:22), so there was no prior to start from.
     The app rewrote it correctly later; its own `measured_phase_physical_ms` = 286.42 landed within
     1.3 ms of the seed below, which validates the value.
  2. The persist gate is `if (n >= window)` with `tipPhaseLearnWindow = 20`; the session's active
     bucket reached 15, so it never wrote one either. Every launch therefore started cold and spent
     the whole session converging. The code's own 2026-08-06 note documents the harm (aim walked
     439.2->431.0 in one batch, second half graded sd 8.93 -> 11.91).

SHIPPED:
- `learning.json` seeded `learned_phase_physical_ms = 287.7` (converged base-30 median, anchor_pct
  30.0 so no base-20 shift applies and the value is already canonical).
- `settings.tip_phase_aim_frozen = true` + re-signed.
- **TipTimingCard UNRETIRED** (`RemotePlayPage.qml`, mounted right after ShotLeadCard). It was
  retired 2026-08-28 ("combine timing into one control") on the premise the engine auto-aims well
  enough — refuted above. The card already had everything: the effective aim in ms, -5/-1/+1/+5
  nudges, Reset, the divergence banner, the EARLY->Later / LATE->Earlier guidance, and a
  "Hold steady" toggle bound to `orion.tipTimingLocked`, which reads/writes
  `config_.data().tipPhaseAimFrozen`. Nothing new was written; it was dead code in the tree.
- `setTipTimingLocked` log line corrected: said "for this session", but the flag is PERSISTED, so
  the lock survives restarts.

VERIFIED LIVE (20:19 instance): the learner window filled to **n=20** and raw samples swung
326.2 / 358.4 / 360.3 / 393.0 ms while `effective_const_ms` held at **361.7** on every one. Before
the freeze a full window moved the aim. UI screenshot confirms the card renders "Tip Timing /
361.7 ms / Locked". OrionNativeTests 792/0/6; qmllint clean (only the module-wide `unqualified` class).

STILL OPEN: `autonomous_vision` is false and `feedforward_anchor` is `meter_appear`; both were
true/hold_start all afternoon and flipped at the 19:38 launch, in the same window learning.json was
zeroed. The 98-shot session ran on that path (47 EARLY / 35 EXCELLENT / 10 LATE). Two attempts to
restore them were overwritten because the RUNNING app owns settings.json — must be set with the app
CLOSED, and the owner has not confirmed whether he flipped them deliberately. Also still open:
tempo remap restore (engine path intact, gated only by `tempo_remap_enabled`).

### 2026-09-11 21:56 — ONE fix for the residual earlies/lates: [ORION_TIP_PHASE_SOLO]
Owner: "this should be the single fix... less engineering and complications and more efficiency".

THE MEASUREMENTS THAT DECIDED IT (all 2026-09-11, this rig):
- The phase member (witnessed anchor crossing + frozen constant) was ALREADY the armed source on
  81.4% of fired shots (n=506). It is not a new path; it is the majority path.
- anchor->tip time, per shot type at anchor 30: Standstill sd 11.7 ms (n=200), Right Fade 13.1
  (n=47), Left Fade 18.3 (n=32), Go-To 23.6 (n=23). No extrapolation, no bias to correct.
- The sampler path extrapolates ~370 ms from a short fit. Offline A/B on 126 paired real rises
  (fit 6 samples ending <=35% fill, predict the ground-truth 70% crossing): IQR 16.0 ms AND a
  +37.9 ms SYSTEMATIC LATE BIAS (the meter accelerates; a line cannot see it). That bias is what
  the horizon de-bias / ramp-shape corrections exist to cancel, and the correction then sits on
  its own 90 ms clamp on 39.3% of reservations (n=987).
- TESTED AND REFUTED: robust (median-of-slopes / Theil-Sen) fit on the sampler. Same 126 rises,
  IQR 16.0 -> 17.9 ms, i.e. slightly WORSE. The spread is extrapolation distance, not outliers.
  (Frame-level noise is genuinely tiny: inside a clean monotone rise, sub-pixel |2nd diff| median
  0.130 pp; 3.8% outliers inflate sigma_y 0.074 -> 0.825 pp, but a 6-sample window rarely holds
  one, so robustifying buys nothing. Do not re-propose this.)
- ALSO REFUTED EARLIER THE SAME DAY: "commit later". The engine already re-fits the armed deadline
  every frame; its own comment says "the fire instant is set by the LAST refinement, not the first
  arm" (89% of shots keep a bit-identical instant when the arm window moves). The promotion line's
  reservation_age_ms=0 / updates=1 is a LATCHED log, not the decision.

SHIPPED — exactly one condition, in the phase branch of the tip decision:
  `const bool samplerMayVeto = !config_.tipPhaseSolo;`
  `if (horizonPlausible && (!samplerMayVeto || (!contradicted && !flatMeter))) { ...phase wins }`
A dated, horizon-plausible phase member can no longer be withdrawn by the extrapolating sampler.
horizonPlausible is still required (the fail-closed edge). With no phase member the old fused path
runs untouched, so no shot is lost. `RemapConfig::tipPhaseSolo`, default OFF in code, pinned ON by
`$env:ORION_TIP_PHASE_SOLO = "1"` in run_orion.local.ps1 (line 206, after the line-90 scrub list,
which does not contain it). Test `tipPhaseSoloIgnoresTheSamplerVetoOnADatedPhaseMember` pins the
gate in pure form. OrionNativeTests 793/0/6. Built + relaunched 21:56.

ALSO: RhythmCard UNRETIRED (owner request) — mounted in RemotePlayPage after ShotLeadCard; binds
only to existing orion.tempoEnabled / orion.tempoFlickHoldMs, so it was a mount, not new code.
TipTimingCard retired again (owner: with the aim pinned it made no timing difference); the FREEZE
stays on (settings.tip_phase_aim_frozen = true, learning.json learned_phase_physical_ms = 271 after
the owner's own nudges to 345 ms effective).

STILL OPEN: autonomous_vision=false and feedforward_anchor=meter_appear (both flipped at the 19:38
launch, unconfirmed whether owner-intended; must be edited with the app CLOSED since the running app
rewrites settings.json on exit). Tempo remap restore. Go-To stays the worst type (sd 23.6 ms) and
the owner has accepted it as "alright".

### 2026-09-11 22:16 — UI trim + [ORION_CV_TIP_CORROBORATE] (owner batch)
1. RhythmCard rewritten as ONE switch (`rhythmFlickToggle` -> orion.tempoEnabled) plus a status
   line. The hold-length slider is gone from the UI; settings tempo_flick_hold_ms and the engine's
   50 ms floor are untouched, so restoring it is a UI change only.
2. Rhythm MOVED into MeterConfigPanel directly above the Meter Delay card (owner placement); the
   RemotePlayPage mount is gone.
3. Meter Delay shortened: the permanent "added to Shot Lead automatically" line and the raw
   meterDelayStatusText line are removed. One line remains and only when it matters -- when the
   delay exceeds what the lead can absorb.
4. LOCATOR: the conf-0.75 acceptance ("a meter-tall achromatic column IS the meter") took a white
   bar on GEOMETRY ALONE with no green tip. Owner symptom: the box snaps onto the COURT for a split
   second -- a court line at the right angle is also an 8-22 x ~90 px achromatic bar. That path now
   needs corroboration: an unconfirmed candidate must be seen at the same place within
   `tip_recent_s` (0.20 s, same tolerance law as the outline gate, 0.6 px/ms growth); a first
   sighting is REMEMBERED (`_tip_pending`), not accepted. New stat `no_tip_lone`.
   `ORION_CV_TIP_CORROBORATE` default ON (=0 to disable).
   VALIDATED on session_20260911_201957 (5299 frames): confirmed conf-0.90 detections
   1214 -> 1214 = 100% KEPT (zero cost to real meters); the single conf-0.75 acceptance refused.
   The 35 boxes that land on reader-NO-METER frames are ALL conf 0.90 and are GHOST meters
   (rejection ghost_static_press 27 / roi_not_found 8) correctly refused downstream -- not court
   locks, so they are untouched by design.
   Tests: test_meter_locator_cv 33 passed (the washed-tip test now pins the one-frame cost, plus
   new `test_one_frame_court_stranger_never_locks` and
   `test_confirmed_green_tip_is_never_delayed_by_the_corroboration`); +test_shipped_reader_defaults
   = 39 passed. Built + relaunched 22:16.
   CAVEAT: the offline dumps are sparse (one frame per interval) so corroboration cannot bridge in
   replay; that the court flash is GONE needs the owner's eyes on a live session.

### 2026-09-11 22:27 — the residual LATES are a RUNWAY ceiling, not a calibration offset
Owner after the phase-solo build: "random earlies removed, now it's random LATES no matter how I
tune the lead". That phrasing is the diagnosis: a lead that cannot fix it is not an offset problem.

MEASURED (session from the 22:16 launch, 75 shots):
- The phase member EXISTS on only 13 of 182 reservations (7%). It is the armed source on 15 of 75
  fired shots (20%) — the other 80% fall through to the SAMPLER, which is measured to carry a
  +37.9 ms LATE bias (see the 21:56 entry). That alone produces random lates.
- When phase does exist: const 345 ms, lead 286 -> **runway after the anchor = 59 ms**, and the
  observed command_eta at those reservations is median 31.7 ms, min 15.8 ms. A capture frame is
  16.7 ms, so the engine routinely arrives with ONE FRAME or less of margin; any dropped detector
  frame blows the deadline and the shot fires late.
- THE CEILING, which is why no lead works: runway = constant - lead. Lower the lead -> earlies.
  Raise the lead -> the runway shrinks (286->71 ms, 300->45, 320->25) -> more lates. There is no
  value that satisfies both. The owner was tuning inside a trap.

FIX — ONE SETTING, no code: `tip_phase_anchor_base20 = true` (anchor 20% instead of 30%).
- Runway 71 -> 129 ms at the owner's lead of 274. Roughly double the margin.
- The aim does NOT move: learning.json stays canonically base-30 (271) and the engine adds
  kAnchorBase20ShiftMs (+58.3) on restore, giving an active physical prior of 329.3 ms against a
  MEASURED anchor-20 median of 332.1 ms (n=42) — 2.8 ms apart, so the shift is correctly
  calibrated for this rig's current constant.
- Anchor 20 is also the TIGHTER rung on this rig: anchor-20 Standstill sd 6.18 ms (n=42) vs
  anchor-30 sd 11.69 ms (n=200).
Re-signed and relaunched 22:27. Revert = set the key back to false + re-sign.

ALSO: the owner turned the Rhythm toggle ON, so tempo/tempo_remap_enabled/tempo_flick_enabled are
now true — that is the tempo remap path he asked for earlier, reached through the new toggle.

### 2026-09-11 23:26 — BANNER-GRADED: offset is zero, spread is the floor, type trim is real
123 releases across session_20260911_222756 (S1) and _224102 (S2), graded on the GAME's panel.
Outputs: scratchpad/agent_out/grade_final/ (per_shot_joined.csv, REPORT_*.txt, panel_*.py).

INSTRUMENT: `banner_reader.py scan` returned **0 events on 6,542 frames**. Root cause: with the
Rhythm toggle ON every bot shot is mode=TempoSquare and the game shows a **RHYTHM** panel, not a
TIMING one. A replacement reader (panel-strip colour signature -> events -> NCC word clustering)
is in grade_final/ and is reusable. Banner onset is +1.0..1.8 s after release, not +0.3 s.

THE KEY NUMBERS (ordered-probit MLE over the owner's own lead sweep 261/264/269/274, bootstrap
B=300; the Shot Lead itself is the ruler because the engine's meter numbers are blind to this
error — fillAtRel vs severity rho=+0.19 and peak_fill/settled_fill/travel_pp run BACKWARDS,
rho=-0.41/-0.42/-0.50):
- **offset at lead 274 = -2.3 ms (CI -5.3..+3.2) = statistically ZERO; zero-crossing at 271.7 ms**
- **spread sigma = 10.7 ms (CI 6.2..19.4)** against a fitted green HALF-window w = 12.1 ms, i.e.
  sigma ~= 0.9 w. Ceiling at the perfect lead ~= **74% green**.
- CONFIRMS the owner's "raise it -> earlies, mid -> a mix": offset 0, spread ~ the window. The
  32-late-vs-9-early imbalance is entirely the lead (52 of 108 shots ran at 261 = +10.7 ms).
- 108/108 shots armed from `phase` — phase-solo + base20 are working exactly as designed.
- NOTHING engine-side explains the residual: |rho| <= 0.15 for command_eta, tip_eta, frame_age,
  sched_delta, coherence, meter_x/y; **cycle_phase_ms +0.12 (n=61) is NULL** — the capture-phase
  theory is refuted again on independent data.

TYPE DEPENDENCE IS REAL (this supersedes the old "constant rightly global" note for FADES):
at lead 261 fades were 1/11 green vs Standstill 26/38 (Fisher p=0.0011); at 274 fades 8/10 vs
Standstill 16/28 (fades 1/11 -> 8/10 across 13 ms, p=0.0019; Standstill flat p=0.44). Per-type mu
at 274: Left Fade +5.0, Right Fade +6.8, Standstill -5.0 ms. Fades want ~12 ms more lead.

APPLIED (app closed, re-signed, relaunched 23:26): `actuation_lead_ms` 264 -> **272**;
`tip_phase_type_trim_enabled` false -> **true**. The stored trims (Left Fade -4, Right Fade -6)
were ALREADY correctly signed — trim ADDS to the constant and constant = later tip = later fire,
so fades landing late need a NEGATIVE trim — and are within 1 ms of the graded values. They were
simply switched off.

TWO CONFIG HAZARDS FOUND, not timing bugs:
1. **Rhythm ON is costly.** The tempo word read FAST on 49/49 non-green shots (never SLOW/good),
   and the 6 shots where the flick did not register at all were **6/6 LATE-red**. It also breaks
   the banner grader (RHYTHM panel, not TIMING). Recommend OFF unless the owner wants it.
2. **Meter Delay is unusable as configured.** Enabled mid-S2 it folded into the lead (269 ->
   357/375 ms), drove command_eta to 5-22 ms and produced **14/15 LATE-red**.

### 2026-09-12 01:00 — tools/timing/panel_grade.py: one-command banner grading
`banner_reader.py scan` returns 0 events on this court (verified, 6,542 frames): it finds only
the 1-cell TIMING box, and with Rhythm ON every bot shot shows a RHYTHM panel. Grading was
therefore a bespoke ~30 min job per session, which is why we tuned blind for so long.

NEW: `python tools/timing/panel_grade.py <framedump_session_dir>`. Prints the green rate, the
verdict mix, and a breakdown BY LEAD and BY SHOT TYPE; writes `<session>/panel_grade.csv`.
How it works: per-frame colour signature of the panel strip -> runs of stable colour = one
verdict -> the verdict WORD is fingerprinted as a binary mask and matched (NCC >= 0.90) against
`tools/timing/panel_templates.npz`. Templates are labelled ONCE and reused forever.
`--label` writes review/clusters.png + labels.template.json; fill it in, re-run with
`--seed <that file>`. An unlabelled word is reported UNKNOWN and EXCLUDED, never guessed.

Three gates were needed and each was found by looking at the cluster sheet, not by theory:
1. fingerprint the word INSIDE the cell's coloured border (the border alone is identical for
   every verdict of a colour -- without this, 31 clusters for ~4 words);
2. require the panel's DARK BACKING PLATE inside the border (the wooden court is saturated
   yellow and clears the strip's darkness floor);
3. a WHITE-severity cell is unsaturated -- fall back to a brightness mask, or 3 of 14 lates are
   silently dropped (and the green rate reads better than it is, the worst direction to err).

VALIDATED against the hand grade of session_20260911_222756: tool 33 EXCELLENT / 14 LATE /
3 EARLY = 66% green; hand grade 33 G / 14 LATE / 3 EARLY = 66%. EXACT on all four numbers.
Then run on session_20260911_224102 with NO labelling: 35 G / 24 LATE / 8 EARLY (hand: 35 / 32 /
6) -- greens EXACT, and it independently reproduced the meter-delay disaster (lead 356.6/357:
14 LATE, 0 green, 0%). The ~8 lates it misses there are verdict/background combos not yet in the
library; one more `--label` pass closes them. The library only ever grows.

NOTE for the next session: the per-lead table on 224102 reads 261 -> 60%, 264 -> 91% (n=11),
269 -> 100% (n=3), 274 -> 53% with SIX earlies. Small n, but it does NOT support 274 being the
optimum on that session; 264-269 looks better. Grade the next clean run before moving the lead.

### 2026-09-12 20:14 — the two "ship blockers": aborts with a clear meter, and contest
ABORTS. 53 abort-identity records across the logs; ALL 53 carried a located meter (meter_x>0), so
every one is literally "an abort with a clear meter on screen". ownership_proof_incomplete 30,
live_tip_deadline_missed 17, ownership_structure_stamp_missing 4, detector_authority_lost 2.
- proof_incomplete SITE: square_early_release 28, pending_stick_fault 2 -> it is NOT Rhythm.
- The proof needs >=3 samples (2 with ownership_proof_two_frame) AND a rise >= anchorRiseMinPct
  (3.0 pp) from the first sample. ownershipProofRunwayAware is already ON by default.
- The 30 by fill trajectory:
  * DEFLATING 10: first_fill median 58.5 %, 9/10 at >=40 %, 9/10 with ZERO proof samples, and they
    arrive a median 4.0 s (max 14.1 s) after the previous shot = the PRIOR shot's leftover feedback
    meter. The gate is correctly refusing a ghost.
  * FLAT 17: first_fill median 23.6 %, e.g. 7 samples at 13.3 -> 13.2 %. A stationary meter during
    the gather, nothing to time. Not cadence: completed proofs span exactly 3 consecutive frames.
    No tie to the prior shot (median 15.3 s, up to 324 s).
  * RISING 3.
- So ~90 % of proof aborts are CORRECT refusals. The 2-frame proof cannot rescue FLAT (the rise rule
  binds, ~2.6 pp across two early-meter frames), and lowering the 3 pp rise weakens a false-shot
  gate. Do not "fix" these.
- deadline_missed in the 09-13T00Z session (7): press_anchored 4 (tip_eta 202-254 ms vs lead 272 --
  the predictor re-enabled 09-12 for the owner's no-meter test, the same hijack memory already
  recorded), phase 3 (anchor 35/40, meter first seen at 40-51 % fill).
- FIX APPLIED: press_anchored_predictor_enabled -> false (app closed first, re-signed, relaunched
  20:13). Returns to the validated config that armed 108/108 from phase. Trade-off: the no-meter
  test path is off again.

CONTEST. All three candidate signals REFUTED:
1. There is no defender/contest classifier at runtime. learning.json per_level nudge slots are
   never read; `contested*` in AutomationEngine means ESTIMATOR disagreement, not defence.
2. green_obs_width is post-release and outcome-contaminated: with 4 artifact widths >6 pp removed,
   the WIDEST tertile is worst (46 % green, 0 % early / 54 % late, n=35). A late shot makes the
   window look wide -- reverse causality.
3. PRE-release green centre (<=450 ms before release, 98/123 graded shots) is effectively constant:
   median 94.75, middle tertile spans only 94.57-94.94. Tertiles 55/76/56 % green with the SAME
   early/late split at both extremes (T1 E12 L33, T3 E9 L34). A raised window bottom would push
   high-centre shots EARLY; it does not. The extremes track read quality, not contest.
Surviving explanation: contested misses are the same sigma ~10.7 ms precision floor landing on a
narrower window (owner-confirmed mechanic: invariant top, tip aim correct). No contest-specific
offset has been shown to exist.
THE ONLY CANDIDATE NEXT STEP: the game's own COVERAGE cell is ground-truth contest, but it is
COURT/MODE DEPENDENT, not merely "Rhythm off". A 3-cell TIMING|COVERAGE|DISTANCE panel was reported
on The Theater court (09-11), while panels read on 09-12 were RHYTHM|DISTANCE (Rhythm on) and one
plain TIMING|DISTANCE with NO coverage cell. So FIRST confirm a COVERAGE cell actually appears on a
real Rhythm-off panel from the owner's current court; only then extend tools/timing/panel_grade.py
to read it and split green rate by coverage. Directional misses -> a fixable per-contest
offset; symmetric -> the precision floor, and contest cannot be tuned away.

### 2026-09-12 20:50 — COVERAGE ground truth on the owner's court: contest MEASURED
- With Rhythm OFF this court shows TIMING|COVERAGE|DISTANCE. Coverage labels seen: WIDE OPEN,
  OPEN, SEMI-OPEN, BOTHERED, LIGHT CONTEST, SOLID CONTEST. This resolves the "court-dependent"
  caveat above FOR THIS COURT.
- session_20260912_201355, lead 269, phase-solo, 34/34 shots armed from phase, press-anchored OFF.
  Dump hit the 12000-frame cap (01:14:48-01:44:49Z): 28 of 34 shots inside, 26 with a readable
  panel (seq 17 and 24 blank). Read by eye from review/wide_panels.png + panels_p1.png + panels_p2.png.
- Green by coverage: WIDE OPEN 6/8, OPEN 5/6, SEMI-OPEN 1/2, BOTHERED 3/4, LIGHT CONTEST 3/5,
  SOLID CONTEST 0/1. Contested (BOTHERED+LIGHT+SOLID) 6/10 = 60 % vs open (WIDE OPEN+OPEN)
  11/14 = 79 %.
- SEVERITY is the signal: contested misses 4/4 medium-or-severe (3 red, 1 yellow); open misses 2 of 3
  slight (white). A narrower window makes the same error land harder = the sigma ~10.7 ms precision
  floor on a smaller target, now confirmed with the game's own contest label.
- DIRECTION is not separable: all misses 6 late / 2 early. The one contested EARLY-red (seq 22)
  released at fill 30.1 vs a 36-41 norm = an engine early release, not contest. The overall late lean
  fits lead 269 sitting ~2.7 ms under the graded zero-offset point (271.7). No contest-specific
  offset at n=10.
- COVERAGE is drawn only AFTER the shot: validation ground truth, never a fire-time input. There is
  still no pre-release contest signal; a contest-aware aim needs a real-time defender detector.
- TOOL GAP: panel_grade.py is not 3-cell aware. With TIMING and COVERAGE both coloured, word_mask's
  saturated bounding box spans both borders and fuses the two words, so it cannot grade Rhythm-off
  sessions until it splits the panel into cells.

### 2026-09-12 22:40 — the three ship-blockers are ONE locator defect + one USB entry (owner batch)
Owner after the park session (20:48-21:27 local, no framedump): "still shot aborts on meters",
"false detections on white jersey/people in the park", "sometimes button presses dont register".
- ABORTS (24 of 106 shots, user log): 12 ownership_proof_incomplete, 7 detector_authority_lost, 3
  live_tip_deadline_missed, 2 ownership_structure_stamp_missing. The native log ROTATED at 21:19:10
  (orion_native.log keeps ~780 KB), so only the last 10 have engine detail:
  * RISING meters refused on `break_geometry=1` (6.7->45.4 over 20 samples; 17.1->44.0 over 33;
    8.3->12.6 over 20, all Right_Fade) — the proof episode was torn down by the geometry-continuity
    check (IoU>=0.10, dim scale 1.5, aspect 1.25) and could not reopen above the 40 % first-sight
    bound. The court dump shows the mechanism: at 20:43:47 the reader's box was (260,312,36,119)
    — 36 px wide, not a meter — then jumped 51 px to (209,318,26,115): a FALSE LOCK RE-SEATING
    onto the real meter mid-rise. IoU 0, aspect scale 0.75 -> break -> live_tip_deadline_missed.
  * FLAT "meters" at two recurring screen spots, (0.32,0.20) and (0.98,0.69): 20.4->19.8 over 28
    samples, 21.7->20.8 unstamped, and the detector_authority_lost at the same coordinates. Static
    white things in the park scene, not meters.
  So the abort blocker and the false-detection blocker are the same defect: the CV proposer takes
  white columns that are not the meter; at the press they sit ON the shooter (his own jersey) and
  the real meter is found late (deadline) or displaces them (geometry break).
- FALSE DETECTIONS, quantified on session_20260912_201355 (owner's court, 12,000 frames, 60 rise
  runs): production `_find` accepted 222 columns OUTSIDE any shot on a 6,000-frame half-sample.
  Contact sheets (scratchpad sheet_offshot_raw.png): the owner's OWN white FEVER 22 jersey with the
  green alien head as the "tip" (conf 0.90!), other white jerseys/shorts/socks, the scoreboard's
  white bar, the shot chart, menus. Live: 1,120 "Shot meter detected" user-log events for 106
  shots; DETECTOR HEALTH found=31 % of all frames; 572 locks/485 drops in 7 minutes.
- WHY THE EXISTING GUARDS MISS THEM: the outline gate (7) measures the translucent track edge, which
  takes the court's colour — median support 0.00 on this wood floor for REAL meters (it was 0.98 on
  the court it was designed on). Tip corroboration only refuses one-frame strangers; a jersey is
  co-located with itself every frame. A dark-track test was tried and REFUTED: the unfilled track is
  translucent white over a light floor (real track V p90 = 212), it cut 31 % of real meters.
- FIX SHIPPED: gate 9, [ORION_CV_SHAPE_GATE] (default ON) in meter_locator_cv.py, judged INSIDE the
  candidate loop so a refused jersey hands the frame to the next candidate (the meter, which loses
  on area). Only the meter's OPAQUE parts are trusted: the white fill must be a straight rectangle
  (row-width spread <= 0.35 of the median above the notch, both edge sd <= 1.2 px, solidity >=
  0.75) and a confirmed tip must be compact (green in the 8-14 px ring beside the centre line <=
  0.25*g_in + 4). Fills < 14 px are deferred (a real fill grows ~4 px/frame at 60 fps: one frame;
  a static 8x10 px jersey bit never grows). A candidate co-located with the box accepted within
  0.20 s is bridged (tracking a full/occluded fill is not first sight).
  Offline (both2 rule, 6,000 frames): every "real" it refused was a jersey the sidecar itself had
  locked; the 9 remaining false were all h<=13 (now deferred) + one mascot pole on the 0.75 path.
  PRODUCTION PATH replay (detect_box with frame ts, gate ON vs OFF, 6,000 frames):
    other_det 157 -> 19, nodet 48 -> 2, ghost 177 -> 161, rise 49 -> 34; ROI holds 499 -> 223.
    The 21 remaining off-shot finds are ALL real meters by eye (mid-rise and post-release meters
    the label logic missed) = zero false finds outside shots on this corpus. The first cut lost
    ghosts (177 -> 54): a FULL fill takes the capsule's arrow shape at the top and cold-failed the
    constant-width test; the judged body now skips the arrow rows (top ~10 px below the tip), so a
    post-release meter re-acquired cold is still proposed and the landing grade survives. Rise
    losses are the jersey "rises" + the one-frame <14 px deferral, which the 10 fps dump
    overstates 6x (100 ms per sampled frame vs 17 ms live) — the live cost is unproven until the
    next session: watch DETECTOR HEALTH lifecycle(locks/drops) and cv stats shape_short /
    shape_irregular / tip_spill / shape_bridged.
  Tests: tests/test_meter_locator_cv.py 40 pass (7 new: jersey strip under a green blob, straight
  strip with a wide green band, green court line behind the meter is not a spill, short fill
  deferred then taken at 15 %, bridging through an occluded fill, full meter with its arrow top
  taken cold, meter wins over a bigger jersey).
  A/B switch: ORION_CV_SHAPE_GATE=0. Needs an app RELAUNCH (the sidecar imports the module once).
- BUTTON PRESSES: no swallowed Square in the surviving log window — all 23 idle-state
  "SQUARE SUPPRESSED" lines are the 0.45 s post-fire drain (Releasing -> Cooldown -> Idle), hook
  heartbeat failures=0. BUT the pad the owner is using RIGHT NOW is USB instance
  VID_054C&PID_0CE6&MI_03\7&10A57DC9 and it is the ONE Sony entry still at
  EnhancedPowerManagementEnabled=1 (nine older instances are 0 — the fix script ran before this
  port was first used). That is the 09-11 silent-pad stall (4-52 s of no reports while enumerated)
  = "presses don't register". Owner step: re-run tools\diagnostics\fix_dualsense_usb_power.ps1
  (UAC), unplug/re-plug.
- Also: the owner moved Shot Lead 269 -> 279 in the UI this session (SHOT LEAD DISAGREEMENT lines);
  graded optimum was 272. Left as set.
- LIVE RESULT 22:34-22:48 local (relaunch, no framedump, lead 279): 38 presses of which 12 were
  <200 ms taps with no meter (pass/fake), 18 real shots -> 17 bot-fired, 0 ownership_proof_incomplete,
  0 live_tip_deadline_missed, 1 detector_authority_lost. Locator found-rate 31 % -> 5.1 % of frames;
  "Shot meter detected" 10.5 per shot -> 1.5; locks +33 / drops +24 in 13 min (the outline gate did
  309 drops on 330 locks — this one does not churn). Owner: "BIG MILESTONE NO ABORTS".

### 2026-09-12 23:10 — owner's ship batch: gate-only band top, Meter Delay shelved, warm-up banner gone
- [ORION_METER_GATE_TOP] meter_locator_cv.py: the ACCEPTANCE gate's top is now its own knob,
  default = the scan's own top (band_top - 0.12 = 0.08). The scan band is untouched, so no infer
  cost (the 09-12 band_top=0.12 attempt moved both and was reverted). Motivation stands from the
  7,509-frame measurement: highest meter centre 0.216 vs a 0.20 gate = clipped meters on jumping
  fades / far shots = press_unanswered_no_meter on real holds (tonight: 3 holds of 732/882/1548 ms
  with no meter ever found, Left Fade / Right Fade / Standstill). The scoreboard that lives in the
  0.08-0.20 strip is refused by the shape gate. Replay with the wide gate: identical to the 0.20
  run (other_det 19, nodet 2, ghost 161, rise 34) -- no new false finds. Old behaviour:
  ORION_METER_GATE_TOP=0.20. tests: 46 pass (scoreboard-band test rewritten to the new contract).
- Meter Delay card REMOVED from MeterConfigPanel.qml (owner: "port and shelve"). Backend,
  VeniceNetSvc, D-pad Up bypass, profile keys, MeterDelaySettingsPropertyTests untouched; a
  persisted meter_delay_enabled still applies (shipped default OFF).
- "TIMING WARMING UP — SHOTS STAY MANUAL" banner REMOVED from RemotePlayPage.qml (owner
  screenshot). State, Setup-page Timing pill and log line untouched.
- Both QML removals are compiled into OrionNative (qt_add_qml_module): rebuilt
  native_orion/build/Release after the owner closed the app.
- AIM POLICY question ("aim at the tip of the green window, else the meter tip"): already the
  behaviour and the two points coincide. The pre-release "green" the reader observes IS the tip
  triangle in fill coordinates: green_end p10/50/90 = 95.2/95.8/96.4 (sd 1.0 pp), the same for
  Standstill / Left Fade / Right Fade / Go-To (35 releases 22:34-22:48). The autonomous path
  targets kTipTargetPct=100 on every shot; GreenWindowTracker::targetPct (start/center/tip/end)
  only serves the legacy path. Aiming at observed green_end would land in the same place after a
  lead re-grade and add ~4 ms of reading noise. NOT changed.
- CONTEST question ("the window shrinks on contest, how does the bot adapt"): on this meter style
  nothing shrinks on screen -- the drawn green is the tip triangle, constant. Adapting per shot
  needs either (a) a meter style that draws the real window band, which the tracker would read
  (then target mode "center" on the live path is a small change), or (b) a pre-release defender
  detector. Measured 09-12 the contested misses were the precision floor on a narrower window with
  no separable offset (6 late / 2 early overall), so (b) has no aim to move; (a) is the only
  honest lever and it is a GAME setting.
- Rhythm: rides the same ownership gate + phase timing as Square (tempo_tip_parity only touches
  stick-initiated TempoStick). Never ON in a graded session; nothing measured to improve.

### 2026-09-13 00:30 — Chiaki fork feedback-send audit (read-only, agent) + NO METER rework
- FORK SEND PATH: the release edge is NOT rate-limited. FEEDBACK_STATE_TIMEOUT_MIN_MS (8) is
  defined but referenced nowhere in the fork; a flagged Square-up goes pipe -> bridge thread
  (HIGHEST, blocking ReadFile, never coalesced) -> lossless orion_queue + cond signal -> the
  Chiaki Feedback Sender thread dequeues it on its next iteration and sends FEEDBACK_HISTORY
  (takion_send_feedback_packet: encrypt + GMAC + blocking send()). Left-stick packets are
  latest-wins and outranked by the queue, so a fade's stick traffic cannot delay the release.
- REMAINING FORK-SIDE JITTER (us-typical, ms tails): (1) the sender thread runs at DEFAULT
  priority pinned to physical core 1 (streamsession.cpp orion_thread_affinity_cb) - the largest
  wake-tail risk; (2) CHIAKI_LOGI calls held under orion_pending_mutex / state_mutex on the edge
  path; (3) the fork has timeBeginPeriod(1) but NO PROCESS_POWER_THROTTLING_IGNORE_TIMER_RESOLUTION
  opt-out (the native app has it), so an occluded OrionStream window can coarsen its timed waits
  to 15.6 ms (only the redundant-copy / retry / keep-alive waits, not the edge itself);
  (4) the redundant-copy head-of-line hold (3 ms after a Square-up, 8 ms after a Tempo flick) only
  delays the NEXT edge - i.e. Tempo flick->neutral, never a button-shot release.
- PROPOSED MINIMAL FORK PATCH (not applied): SetThreadPriority(HIGHEST) on the feedback sender
  in orion_thread_affinity_cb; move the two edge CHIAKI_LOGI calls after the unlock; add the
  power-throttling opt-out in main.cpp. Verify with the fork log: `ack_local_delivery ...
  queue_wait_us=` minus `local_udp_accept kind=history ... transport_us=` isolates the
  sender-wake jitter - histogram before/after.
- NOTHING measures PS5 receipt (console_ack=0 everywhere); the last observable point is send().
  Native side: `Release submit: hook_write_us / hook_ack_wait_us` - the ack wait spans the Takion
  send() plus the return leg, so forward delay <= hook_ack_wait_us.
- NO METER REWORK [ORION_NO_METER_LEAD] (owner: "values should span 1-100 like the shot lead; two
  separate paths"): processInputTimedIdle now classifies the shot type from the stick (same
  classifier as the meter path) and holds Square for pressAnchoredTipMs[type] - inputTimedLeadMs
  (new setting input_timed_lead_ms, 150..400, default 272) when the prior carries weight >= 8;
  otherwise the old global input_timed_delay_ms (kept, no UI). Floor 100 ms. NoMeterCard.qml is now
  a 1..100 "No Meter Lead" slider on Shot Lead's scale; the page's METER/NO METER switch hides the
  other path's cards (MeterConfigPanel + ShotLeadCard vs NoMeterCard + noMeterRhythmCard).
  Tests: inputTimedUsesPerTypePriorMinusLead (prior-lead, fallback, floor) + round-trip clamps.
- BUILD/TEST STATE 01:50 local: OrionNativeTests 802 pass / 0 fail / 6 skipped (the owner-settings
  round-trip allowlist gained input_timed_lead_ms); OrionNative rebuilt (01:46) with the NO METER
  rework + page switch + the two earlier QML removals and relaunched (pid 27232, "Input timer:
  enabled=0 delay_ms=500.0 lead_ms=272.0"). Live sessions tonight on the shape gate: 22:34-22:48
  18 shots / 0 proof aborts; 23:xx-00:31 32 fired / 2 ownership_proof_incomplete (both
  square_early_release, meter_jump ~0 = the owner let go before the proof) / 3 real holds with
  no meter found (366 ms Left Fade, 1215 + 494 ms Standstill) = the far/fade recall item.
- FORK TAIL MEASURED, LEVER CLOSED: `Release submit: hook_ack_wait_us` over the 91 releases in
  the current native log = p50 299 us / p90 476 / p99 985 / max 985 us. That interval spans the
  Takion send() plus the ack return leg, so the forward pipe->fork->send delay is < 1 ms worst
  case tonight. Against sigma ~10.7 ms it is not a contributor; the fork priority/log/throttling
  patch is NOT worth applying. (The fork's own OrionStream log is not relayed into
  orion_native.log, so queue_wait_us/transport_us could not be histogrammed directly.)
- NO METER PUMP-FAKE BUG (owner, 01:5x local): a high No Meter Lead made the hold shorter than the
  game's set point -> pump fake instead of a shot. Holds tried tonight: lead 400 -> 270 ms, 362 ->
  308, 337 -> 333, 312 -> 358, 302 -> 368, 281 -> 389, 272 -> 398 ... 150 -> 520. Owner bracket:
  slider >= 60 (hold <= 371 ms) pump-faked, 53 (389 ms) shot. FIX: kInputTimedMinHoldMs = 390
  (the meter path's proven Standstill hold at lead 272-279); log says
  source=prior-lead_FLOORED_pump_fake_guard when it engages. Standstill's usable slider range is
  therefore 1..53; fades never reach the floor. The meter path cannot fire that early (deadline
  cannot precede the witnessed anchor) which is why it never showed there.

### 2026-09-13 02:15 — fade / far-shot forensics on the old dumps (read-only, agent)
- Replayed the CURRENT locator (shape gate ON, gate top 0.08) and three variants over sessions
  145457, 201355, 124114 (10 fps dumps + the sidecar's 60 fps detframes for verdicts; native log
  .1 for engine outcomes). Artifacts: scratchpad\fades\ (shots_*.csv, traj_*.txt, sheet_*.png,
  replay_*.csv, replay_locator.py/shots.py/rle.py/sheet.py/census.py/gatediag.py).
- VERDICT: the locator is not the fade problem. On every frame where a meter is on screen with
  its tip the current locator finds it; RELEASED shots were first seen at fill 15-17 % (p90 19).
  The failures are METER ONSET: on fades the meter is not on the capture feed until ~+695 ms
  median after the press (up to +1.7 s, first-seen fill ~11), and in the fade-heavy 201355 session
  21 of 54 fades never showed a meter within 2.5 s (crops: gather / pump-fake, no meter graphic).
  CAVEAT: 201355's second half is the disk-full stall (frame age 2-7 s) — its 44/54 NOT_OWNED
  fades are contaminated; 145457 (healthy rig) released 10/12 fades and its 2 fade failures were
  late onset (+744 ms). Aborts that did reach the engine had first_fill 24-36 %, i.e. acquired
  late against a short runway (fade tip ~1000 ms after the press, onset ~650-700 -> ~300 ms of
  visible rise minus lead 279).
- ORION_METER_GATE_TOP is INERT on these dumps: cur vs gate20 byte-identical on every frame (no
  meter centre above 0.20 H in gameplay; min box-top ~91 px). Harmless; kept for the extreme
  far/high case it was measured for (centre 0.216 on 09-12).
- ORION_CV_SHAPE_GATE costs a handful of single first-sight frames on fades at fill 5-22 (e.g.
  145457 idx 1053 tip_spill = the green-ball cosmetic beside the tip; idx 3277/3282; 201355 idx
  5852, 8612, 9427-9430, 11859), each followed by a confirmed co-located frame -> ~1 frame live.
  It correctly rejects the previous shot's spent meter (145457 idx 3302, 2146). Keep it.
- Remaining fade lever (engine, not locator): runway. The validated 2-frame ownership proof
  (settings ownership_proof_two_frame, OFF; saves ~14 ms; never pair with a 4.0 pp rise — rise is
  3.0) buys one frame on exactly these late-onset fades. Flip it at the next app close and grade
  fades on the banner. Nothing else measured is cheaper.

### 2026-09-13 02:30 — panel_grade.py is 3-cell aware (agent; validated)
- tools/timing/panel_grade.py rewritten around a per-frame cell splitter (find_cells: the grey
  frame's long bright top/bottom rows + vertical dividers -> spans with a dark plate; per-cell
  colour; coloured cell fingerprint = saturated pixels inside the border, white cell = white text
  of the lower line). Roles: 3 cells = TIMING|COVERAGE|DISTANCE; 2 cells = TIMING + COVERAGE-or-
  DISTANCE; a first cell reading a coverage word is re-roled. Vocabulary adds WIDE OPEN, OPEN,
  SEMI-OPEN, BOTHERED, LIGHT CONTEST, SOLID CONTEST, HEAVY CONTEST, SMOTHERED. LAG_MIN_S 0.7 ->
  0.4 (greens land 0.51-0.91 s after the release on this court). CSV gains timing_color,
  coverage, cov_ncc, cells; new PER SHOT table + coverage-by-timing summary; --review DIR.
  Templates 8 -> 29 (backup of the old .npz in the session scratchpad). DISTANCE is located, not read.
- VALIDATION: session_20260912_201355 28/28 vs the hand table (WIDE OPEN 6/8, OPEN 5/6, SEMI-OPEN
  1/2, BOTHERED 3/4, LIGHT 3/5, SOLID 0/1; 6 LATE / 2 EARLY); legacy Rhythm-ON session_20260911_
  222756 51/51 reproduced. This is THE instrument for the owner's "95 % wide open" question: play a
  framedump session on the court, run the grader, read open-shot green % and the EARLY/LATE split.
- panel_grade.py + panel_templates.npz are UNTRACKED in git (never committed) - commit them.

### 2026-09-13 02:45 — NO METER compiled out of the shipped app (owner: "compile out the no meter section")
- [ORION_INPUT_TIMED_COMPILED_OUT] AppConfig::load()/save() force input_timed_enabled=false and
  OrionAppController::setInputTimedEnabled(true) is a no-op unless the build defines
  ORION_ENABLE_INPUT_TIMED (only OrionNativeTests defines it, CMakeLists.txt, so the engine path
  and its tests stay live). RemotePlayPage.qml: the METER/NO METER switch card, NoMeterCard and
  noMeterRhythmCard mounts are removed; MeterConfigPanel/ShotLeadCard are unconditional; the
  timing pill no longer references inputTimedEnabled. NoMeterCard.qml stays in the tree. The
  owner's settings still carry input_timed_enabled=true from tonight's test; the forced-false on
  load means the meter path is active on the next launch and the file is rewritten false on exit.
- The 390 ms pump-fake floor and the per-type prior-minus-lead hold remain in the engine for a
  future re-enable; the owner's final feedback test is meter-only "just like yesterday".
- 14:09 local: the NO METER gate is a RUNTIME test opt-in (AppConfig::inputTimedAllowed /
  setInputTimedAllowedForTesting, inside namespace orion) because AppConfig lives in the
  OrionCommon SHARED library and a compile define on the test target cannot reach it. Full suite
  802 pass / 0 fail / 6 skipped; OrionNative rebuilt 14:09 and relaunched for the owner's final
  meter-only feedback session.

### 2026-09-13 21:40 local — the "4-5 straight lates" streak: a reader stall, not the aim
- Session 21:10-21:16 local (13 releases, 0 aborts). Shots 6-10 (21:15:15-26) sit on the only
  reader stall: sidecar `Capture health ... cv_fps=51 detect_ms=21.3` at 21:15:12 (over the 16.7 ms
  budget -> skipped frames), preview gap max 40.5 ms at 21:15:17, locator infer 12-13 ms (normal
  2-8). PHASE SAMPLE anchor->freeze on those shots: 349/333/339/289/351 vs 336 +- 3 elsewhere =
  noisy anchor dating = lates. Delivery clean (hook_ack_wait 342-795 us), no card dups, all five
  at meter x~0.70 (crowd behind). Greens resumed when the reader caught up.
- CAUSE in the locator: the gate-8 lone-column check was a Python loop over every white
  component per candidate (O(cands x keep)): 12.4 ms alone on a crowd frame with 306 components
  (profile on the 201355 dump: mask 3-8 ms, morph 2.6-3.3, CCL 1.3-6.7, lone loop 0-12.4).
- FIX (meter_locator_cv.py `_find`): component table as numpy arrays; keep/cands/order and the
  twin test vectorized, same rules. Worst frame 29.4 -> 11.3 ms, p99 19.5 -> 17.6, p50 10.75 ->
  9.9 (the fixed mask/morph/CCL cost dominates the median). 40/40 locator tests; in-process A/B
  vs the loop version on 6,000 dump frames: result-identical. Live on the next relaunch.
- REMAINING LEVER if streaks recur: the full-band scan is ~10 ms median on a bright scene, so
  idle (un-armed) frames could be scanned at half cadence to keep armed frames inside the budget
  (reader-side policy, untested). Disk still 11-12 GB, so the next occurrence is not dumped.
