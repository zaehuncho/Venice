needs changes

# Second eyes: online grading clock (2026-09-22)

**Beta verdict: weakened, not refuted.** The *association* between a later detected onset and a later banner grade is reproducible. It is not yet proof that 45% of the game's online grade uses a press clock, nor a measured network delay. The proposed 0.45/40/two-sided tuning and the 76.8% EXCELLENT projection should not be treated as validated. No source or runtime was changed for this review.

## Corpus and method

- Read the finding, the 57 `D:\NexusVision\shot_records\*.jsonl` files, `net_probe.py`, `net_join.py`, `OnsetFeedforward.h`, and the `AutomationEngine::scheduleFire` ONSET FF branch. The sorted record-set digest is SHA-256 `fb9403a7064315c90ac4a50474d853148a2436ada76da91dd588e73346de7224`, computed by hashing each filename, NUL, then that file's SHA-256 digest in filename order.
- Reproduced the stated selection: released, banner-graded Standstills with `onset_source=detect_loop`; sessions with at least 12 such shots; per-session median onset and **hold** centring; exclude onset deviations beyond 200 ms and meter-relative deviations beyond 60 ms. This gives **816 online shots / 27 sessions** (548 EXCELLENT, 170 LATE, 98 EARLY) and **39 offline shots** (37/39 EXCELLENT). The 09-20/21 online subset is **470/14**, not 471/14 under this exact rule. The 81 excluded online shots include 43 EXCELLENT, 24 LATE, and 14 EARLY; several have zero/implausible onsets.
- Fit ordered probit/logit to `meter_c = (hold - median_session(hold)) - (onset - median_session(onset))` and `onset_c = onset - median_session(onset)`. In the probit, the latent mean is `(meter_c + beta * onset_c) / sigma`; two cutpoints are estimated. Uncertainty below resamples whole sessions, not individual shots. Models were fitted with SciPy 1.18; no app launch or build was used.

## 1. Does beta survive attacks?

| Fit or selection | n | beta | Conditional scale / note |
|---|---:|---:|---|
| Reproduced ordered probit | 816 | **0.440** | sigma 43.54 ms; cuts -54.61, +39.78 ms; 400-session-bootstrap 90% beta interval **0.284–0.700** |
| Ordered logit | 816 | 0.473 | Link choice changes beta by +0.033 |
| Gaussian random-intercept ordered probit, 20-point integrated quadrature | 816 | 0.397 | Within-session sigma 38.14 ms; random-intercept SD 8.49 ms |
| Per-session intercepts, unpenalized | 816 | 0.331 | sigma 29.9 ms; many nuisance parameters, so this is a sensitivity fit, not a new noise estimate |
| Per-session intercept **and width** shifts, Gaussian-prior MAP penalty 1 per latent shift | 816 | 0.345 | sigma 30.9 ms; cutpoints are not constant across sessions |
| Meter-relative median centring instead of separate hold/onset medians | 816 | 0.362 | sigma 35.8 ms |
| One global median for onset and meter-relative timing | 816 | 0.578 | sigma 55.3 ms; conflates between-session contexts |
| Looser filters: 300 ms onset / 100 ms meter-relative | 836 | 0.408 | sigma 50.6 ms |
| Tighter filters: 120 ms onset / 25 ms meter-relative | 755 | 0.674 | sigma 42.0 ms |
| No deviation filters | 897 | 0.946 | sigma 136.2 ms; invalid/other-mode tails dominate |
| 09-17/18 only | 346 | 0.325 | sigma 37.6 ms |
| 09-20/21 only | 470 | 0.539 | sigma 47.7 ms |

The baseline likelihood-ratio statistic against beta=0 is 95.87 (nominal chi-square p=1.2e-22); with a free per-session intercept it is 107.40. Leave-one-session-out beta is 0.376–0.485. Thus a positive **conditional association** is not a single-session artefact. Magnitude is model/selection-sensitive, however: even plausible centring and cutpoint variants move it from roughly 0.33 to 0.58. Separate daily estimates are particularly nonstationary (09-20: 0.924 on 240 shots; 09-21: 0.216 on 230), and the 09-20 session bootstrap is very wide. A two-era heteroskedastic probit gives beta 0.434, with older/newer scales 41.4/45.1 ms; a logit link does not erase the signal.

The alternate clock is only a partial falsifier. On the **same 469 newer records**, replacing capture-wall `onset_ms` with `onset_read_ms` changes beta **0.537 → 0.508**. Their difference has SD 3.09 ms overall (median within-session SD 2.20 ms). The older `onset_engine_ms` is exactly `onset_ms + 36 ms` for all 346 selected older shots; its beta is necessarily unchanged (0.325). A uniform 60 Hz frame-phase quantisation alone has SD about 4.8 ms. Under a simple independent classical-error explanation, manufacturing beta 0.44 from the observed 43.55 ms onset SD would require roughly **28.9 ms** onset-error SD, not 4.8 ms. But both newer clocks observe the *same detected event*, so a shared, variable first-detection lag remains untested; the 3.09 ms clock difference cannot rule it out.

The feedback-loop concern is real, not resolved by these records. The current schedule applies onset feedforward at `scheduleFire`, while a displaced release is fenced from the banner-trim learner. Joining the two retained native logs by physical epoch **and** record interval gives an actual release displacement for only **15/816** selected online shots, of which **3** are nonzero; missing is unknown, not a zero-displacement control. Earlier banner trim, aim changes, and session-context changes are likewise not randomised in the records. The observed beta is not a causal gain estimate.

**Implementation check:** ordinary configuration of gain 0.45/clamp 40 does **not** allow 40 ms of earlier fire. At `AutomationEngine.cpp:17505–17529`, the non-A/B early bound remains `bannerTrimMaxMs` (15 ms) minus trim already spent; the A/B arm alone raises that ceiling to its clamp. In this sample 206/816 onsets exceed +33.3 ms relative to their session median, where a 0.45 rule requests more than 15 ms earlier even before any banner trim. A setting-only change therefore cannot execute the headline 0.45/40 policy as modelled.

## 2. Is the “unexplained 40 ms” online-only?

**No such component is identified.** In the stated ordered-probit parameterisation, **43.54 ms is the conditional residual sigma *after* onset and meter timing are in the model**. The measured onset SD is 43.55 ms, so beta times onset is 19.15 ms SD. Subtracting 19 ms in quadrature from the already-conditional 43.54 ms to declare an “unexplained 40 ms” double-counts the onset adjustment. More importantly, ordinal labels do not observe a clock-error SD directly: width variation, session aim drift, contest/context, OCR labels, and detector lag can all appear in that latent scale. The random-intercept fit moves the conditional scale to 38.1 ms; flexible session cutpoints move it near 30 ms, with overfit risk. The 400-session-bootstrap 90% interval for the original scale is **29.7–64.2 ms**.

The offline comparison establishes a **rate gap**, not a clean variance component. The selected offline session is 37/39 EXCELLENT = 94.9% (exact binomial 95% interval **82.7–99.4%**); an ordinal beta/sigma fit with only one EARLY and one LATE is unstable and not a valid offline noise estimate. The original table's ~72–74 ms hold/onset SDs come from all **40** graded offline shots; the stated 39-shot filtered set has hold/onset SD **48.5/48.3 ms** and meter-relative SD **3.7 ms**. One filtered-out EXCELLENT at onset 711.85 ms / hold 940.4 ms creates most of that SD difference. The high offline success strongly motivates a matched online/offline experiment, but cannot isolate server, network, shot-window, or instrumentation noise.

No court-network attribution can be made from these records. All **88** records that stamp `network.court_ready` have value **0** and RTT/jitter **0.0**; the other records have no ready court sample. `D:\NexusVision\netcap` contains no capture to join.

## 3. Other predictors, out of sample

For each predeclared candidate, compared an ordered-probit baseline (meter-relative timing + onset) with that candidate on **the identical available rows**. Five `GroupKFold` folds hold out whole sessions; continuous features are scaled using training folds only. The table reports held-out log-likelihood gain in **nats per shot** (positive is better). One-sided session-level sign-flip tests were adjusted by Holm across all **17** candidates. These are exploratory associations, not intervention estimates; grade-panel coverage/distance are retrospective descriptors.

| Candidate beyond hold/onset | n / sessions | Held-out gain | Holm p |
|---|---:|---:|---:|
| Tempo (quick/slow, older era only) | 346 / 13 | +0.0111 | 0.58 |
| Previous verdict (EARLY/LATE indicators) | 789 / 27 | +0.0067 | 1.00 |
| Onset fill | 816 / 27 | +0.0038 | 1.00 |
| Pickup first-sight time | 470 / 14 | +0.0036 | 1.00 |
| Pickup first-sight fill | 470 / 14 | +0.0009 | 1.00 |
| Green-confirm fill before release (log join) | 284 / 8 | +0.0008 | 1.00 |
| Distance | 716 / 24 | -0.0010 | 1.00 |
| Rhythm flag (only 11 positives) | 816 / 27 | -0.0010 | 1.00 |
| Time rank within session | 816 / 27 | -0.0012 | 1.00 |
| Open vs contested coverage | 261 / 10 | -0.0013 | 1.00 |
| Release-snapshot confirmed green width (log join) | 284 / 8 | -0.0016 | 1.00 |
| Release-path velocity (log join) | 292 / 8 | -0.0025 | 1.00 |
| Green-confirm time (log join) | 277 / 8 | -0.0029 | 1.00 |
| Pickup anchor used | 470 / 14 | -0.0044 | 1.00 |
| Release-path crossing ETA (log join) | 292 / 8 | -0.0064 | 1.00 |
| Pickup anchor confidence | 470 / 14 | -0.0075 | 1.00 |
| Reader-minus-capture onset clock delta | 470 / 14 | -0.0079 | 1.00 |

**Nothing survives multiplicity control.** The unadjusted tempo test is p=0.034, but its adjusted p=0.58 and it is limited to the older 13 sessions; do not promote it. Green width was previously implicated in a different 09-18/19 sample (`docs/GREEN_WINDOW_WIDTH_2026-09-19.md`); this 09-20/21 log-joined subset is only eight sessions, with a 3.7%-width mode on 109/284 shots. This test neither re-establishes nor refutes that older width result. Post-release oracle gap was deliberately excluded as outcome leakage.

## 4. What the proposed network probe cannot see

- `net_probe.py` records UDP/IPv4 fields from the PS5-side adapter. It does not identify the encrypted Remote Play datagram carrying a specific press/release, a game input-bearing PS5-to-court datagram, the server's receipt/grade time, or one-way path delay. `net_join.py` uses “first packet after” and “first non-modal-sized packet after” as **proxies**. Periodic traffic can satisfy both without carrying the input. Its inbound inter-arrival mode is not proof of the game's simulation tick.
- The “court” is whichever public peer becomes most frequent after 200 packets. That is a candidate, not a verified game endpoint. IPv6 fields, TCP, alternate peers, relays, and endpoint rotation are not covered by the CSV parser. Both PC/RP and PS5/court timestamps need an observed clock-alignment and packet-loss check before millisecond conclusions.
- The 20 Hz ICMP samples are separated by nominally 50 ms, can time out at 250 ms, measure a round trip on a possibly different path, and cannot resolve an individual release's uplink or server processing. The code records the duration of `IcmpSendEcho`, not a server application acknowledgement; [Microsoft documents it as ICMP echo/reply](https://learn.microsoft.com/en-us/windows/win32/api/icmpapi/nf-icmpapi-icmpsendecho).
- “Headers only” is not true for the saved `capture.pcapng`: `-s 96` saves the **first 96 bytes of each packet**, which can include UDP payload after the link/IP/UDP headers. The CSV is header fields, but the pcap is not necessarily payload-free. [Wireshark's TShark manual defines `-s` as a byte snaplen](https://www.wireshark.org/docs/man-pages/tshark).
- The proposed `net_join.py` improvement test is an **in-sample** likelihood-ratio screen over five features with no session-held-out prediction or multiplicity control. A positive result there would be a lead to validate, not identification of the residual component.

**Fastest cheap discriminating measurement:** in one stable online court, timestamp the exact encoded press/release input at the Remote Play sender and capture the PS5-side packet stream simultaneously; add a small randomised, logged release offset per physical shot and no-input control windows. Validate a packet signature against those randomised events before treating a “next packet” as input-bearing. Join these to the banner and gateway/court ICMP samples on held-out sessions. The random offset identifies the *effective grade-vs-delivered-release slope*; sender-to-wire delay and PS5 egress phase then test whether the loss precedes the server. Header-only capture still cannot uniquely separate server receive jitter from server grading logic; that last split requires a trusted server-side timestamp or a game-provided timing oracle.

## 5. Mid-80s: reachable or ruled out?

Under the **stated fixed-band ordered-probit model**, the fitted cutpoint width is 94.39 ms and residual sigma is 43.54 ms. Even a perfect fire time centered on the band has maximum EXCELLENT probability `2 Φ(94.39/(2×43.54)) − 1 = **72.16%**` (90% whole-session-bootstrap interval **70.8–74.3%**). The published **76.8%** feedforward-plus-7-ms simulation is therefore internally inconsistent with that same model and scale. A real policy has additional meter-relative scatter, so its model ceiling is lower. To put the fixed-band ceiling at **85%**, conditional sigma would have to fall to **≤32.8 ms**, or the band would have to widen. This is a **conditional model bound, not proof of a physical online ceiling**: the width/noise assumptions may be wrong, and an intervention could change them. The offline 95% shows a different regime is possible but does not show it is transferable online.

## Ranked next measurements / decision gates

1. **Ground-truth actuation and packet identity first.** Run a stable-court, randomised per-shot release-offset sweep with exact applied offset and input-sender timestamps; verify RP packet classification against no-input windows. Grade by held-out session and report the offset slope, mean shift, spread, and whether onset still matters after *delivered* release timing. This can falsify the press-clock interpretation without guessing from passive cadence.
2. **Capture concurrent network timing, but keep it observational.** On the same shots, collect PS5-side packet timestamps, flow identity, packet loss, gateway/court ICMP, and record a clock-alignment check. Use per-shot release-minus-press features and session-held-out/multiplicity-controlled prediction; compare incremental explanatory power with sender-to-wire delay. Do not label a top public peer or modal packet as the court/input without validation.
3. **Independently date onset and shot-window width.** Use frame-level capture timestamps/visual annotation on a stratified sample, quantify first-detection lag against true meter appearance, and retain pre-release confirmed green width. A shared ~29 ms onset-detection error could mimic the fitted beta; a 60 Hz quantisation error alone cannot.
4. **Matched online/offline blocks and intervention A/B.** Keep the same shot, location, release policy, and capture path; alternate online/offline blocks and randomise **actual applied** FF arm (not merely its settings). Report grade and width distributions, session-level confidence intervals, and the maximum rate achieved. Only then promote a gain/clamp change. If production settings are used rather than the A/B arm, verify the 15 ms early bound actually permits the intended displacement.

**Decision:** preserve the positive-onset finding as a hypothesis worth testing; revise the variance split, the 76.8% projection, and the setting-only 40 ms FF recommendation before using this analysis to tune or claim an online ceiling.
