# Tip-Timing Consistency — build ideas + adversarial review (2026-08-12)

**Repo:** `C:\Users\aaron\Desktop\NexusVision` · **Branch:** `fix/timing-input-and-remoteplay-blockers`
**Answer to** `REVIEW_ASK_2026-08-12.md`. Read-only: nothing was edited, built, run, or committed. `settings.json`,
`learning.json`, `run_orion.local.ps1` untouched; no service touched; app not stopped.
**Method:** direct read-only analysis of `logs/orion_native.log` (n=609 graded landings, pooled across sessions,
**grouped within shot-type — never joined on `seq` across sessions**) + three parallel read-only code scouts over the
C++ fire path, the Square passthrough (#88), and the Part-4 bug shape. Every claim carries `file:line`; unproven items
marked SPECULATION.

Two deliverables in one doc:
- **Part A — how to enforce tip-timing consistency** (your primary ask). Ranked, buildable, and each tagged
  `NEW` / `PLANNED-UNBUILT` / `REFUTED-DO-NOT`.
- **Part B — the adversarial review** you asked me to run (Square #88, Part-4 hunt, Part-2/3 claim checks), ranked by
  ship risk before Aug 28.

---

## Part 0 — The diagnosis, from your own logs (this is what the residual actually IS)

Before any idea: I re-derived the residual from `logs/orion_native.log`, and it is **not one number and not what C1 says
it is.** Four facts, all reproducible read-only:

**F1 — Every miss is UNDER-timed. Zero over.** Across all five shot types, `peak_fill < green_start` accounts for
100% of misses; `peak_fill > green_end` = **0.0%**. The meter caps at 100 and `green_end` is ~100, so over-timing is
physically absent. You only ever leave green out the **bottom**.

**F2 — It is a VARIANCE problem, not a bias problem — "aim higher" is dead.** For every shot type, the optimal single
aim offset that maximises in-green is **+0.0** (a constant shift trades under-misses for over-misses one-for-one because
the top of the distribution is already pinned at the 100 cap). Miss rate is set purely by the **spread** of `peak_fill`,
not its center (center sits within ~1pp of `green_center` on every type). This matches memory
`[[green-window-shrinks-aim-high-is-correct]]` — and now it's quantified.

**F3 — The residual splits by shot type, with OPPOSITE causes.** Correlating fire-point (`fill_at_rel`) against the
make/miss determinant (`peak_fill`):

| shot | n | corr(far,peak) | corr(far,travel) | peak_sd (pp) | under-miss % |
|---|---|---|---|---|---|
| Standstill | 296 | **−0.07** | −0.93 | 3.11 | 11.8% |
| No Dip | 45 | +0.01 | −0.61 | 3.87 | 6.7% |
| Right Fade | 113 | **+0.52** | −0.41 | 8.06 | 13.3% |
| Left Fade | 100 | **+0.50** | −0.67 | 4.59 | **23.0%** |
| Go-To | 55 | +0.50 | −0.63 | 2.40 | 18.2% |

- **Standstill / No Dip** (`corr(far,peak)≈0`): the meter is a near-perfect error corrector — fire early, it travels
  more; fire late, it travels less; `peak` lands the same regardless (that's the −0.93). Here the residual is genuinely
  **downstream / measurement noise**, and C1 holds *for these types*.
- **Fades / Go-To** (`corr(far,peak)≈+0.5`): fire-timing jitter **leaks straight into `peak`** — fire late, peak higher,
  miss low. Here **a tighter fire provably tightens the make/miss determinant.** C1's blanket "no reader/predictor fix
  can help the residual" is **too pessimistic for these types** — and they include your worst offender (Left Fade, 23%).

**F4 — The lead that produces all this is a per-session CONSTANT.** `fireAtMs = predictedTipMs − effectiveLeadMs`
(`AutomationEngine.cpp:6863`), and `effectiveLeadMs = measuredLeadForActuationMs()` returns your **fixed user Shot Lead
(~290 ms)** whenever authority exists (`:10291`, `:10316`, `:10337`). **Nothing adapts the lead per shot** — so the
shot-to-shot `travel` variance in F3 is never corrected in flight. That is the seam every idea below pushes on.

> **Honest framing of "no mistimed shots, no matter what":** you cannot un-send a packet, so under variable network
> jitter a *pure open-loop* guarantee is physically impossible. The achievable version is two-pronged: **(1) shrink the
> horizon and the jitter so mistimes become rare** (A1, A4, A5, A6), and **(2) when the pipeline is measurably in a bad
> regime, DON'T fire** (A3) — trade one shot for never shipping a bad one. Together that is "perfect unless real lag/
> jitter, and when there is, a clean no-shot instead of a brick."

---

## Part A — Buildable levers to enforce tip-timing consistency (ranked)

Tags: `NEW` = not proposed anywhere I found · `PLANNED-UNBUILT` = specced in your docs, not in the shipping path ·
`REFUTED` = already killed, do not revisit.

### A1 `PLANNED-UNBUILT` — Build the reactive/closed-loop fire rung (shrinks the jitter horizon ~4.6×)
**The single biggest consistency lever, and my F3 data is the mechanistic proof it works.**
- **What exists today:** the autonomous path is purely predictive and commits ~290 ms **ahead** of the tip
  (`AutomationEngine.cpp:6863`). The only reactive-on-observed-crossing code is the legacy `reactive_target` block
  (`:8658`) — but it is **bypassed** (`processAutonomousLiveMeterHolding` returns at `:7313` before reaching it),
  fires **zero-lead** on a **discrete** frame, and lands late by design. So the reactive rung is **proposed-only.**
- **Build:** add a rung to `processAutonomousLiveMeterHolding` that, once armed, fires when the **live sub-frame crossing
  estimate** (already produced by `TemporalSampler::predictCrossing`, `:429-635`) projects `peak` onto `green_center`
  within the **output-only lead (~63 ms)** — not the whole-pipeline 290 ms. Lead by the release leg, not the prediction.
- **Why it enforces consistency:** the 290 ms predictive horizon is the window over which drift, RTT wander and jitter
  accumulate before the shot lands. Cutting it to ~63 ms cuts that accumulation ~4.6×. F3 shows fire jitter **leaks into
  peak on Fades/Go-To** (`corr≈+0.5`) — those are exactly the types a shorter horizon tightens. Delay-invariant too
  (reference and action ride the same delayed pipe), so it doubles as the meter-delay fix.
- **Risk:** MEDIUM — new rung in the highest-consequence file. Gate behind a flag **and** default-OFF; byte-identical
  when disarmed. Pre-req: measure true output latency on the rig (see A7).
- Refs: `AutomationEngine.cpp:6090, 6863, 7313, 8658, 10291`; design trail `METER_DELAY_PLAN_2026-08-12.md §8`.

### A2 `NEW` — Per-shot adaptive lead: a bounded PI controller on peak-error (kills the drift-to-abort)
**Different from the planned `phase_drift_clamp` — that just fences the existing walking learner. This replaces the walk
with a real controller on the *right* error signal.**
- **Observation:** the lead is a session constant (F4), while the graded `peak_fill` of every shot is observable ~2-3
  frames after release on the same pipe (`recordLandingLeadSample`, `AutomationEngine.cpp:14013`). Today that error feeds
  a slow phase-aim learner that **drifts monotonically** (439.2→431.0 over one 70-shot batch, per `BOT_MAXOUT_PLAN`) and
  is the mechanism behind `live_tip_deadline_missed` drift-to-abort.
- **Build:** a small integral controller on `e = green_center − peak_fill`: `lead += k·ê` with a **deadband** (ignore
  |e| < ~1pp so noise doesn't chase), **anti-windup**, and a **hard clamp** to `frozenLead ± band`. It nudges the
  *additive lead scalar*, never the phase constant — so it can only pull the fire point toward the known-good center and
  **cannot manufacture an unschedulable deadline** (strictly weaker than the shipped `tip_phase_aim_frozen`).
- **Why it enforces consistency:** holds `peak` at `green_center` in **1-2 shots** and **self-heals regime shifts** (RTT
  step, meter-delay toggling) that today take the slow learner a whole session to chase — or never, because it aborts
  first. This is the direct antidote to "perfect on shot 2, aborting on shot 20."
- **Risk:** LOW-MEDIUM — changes live release timing, so arm behind a counted live batch (standing rule). Feed it the
  **clean** peak from A6, not the discrete one.
- Refs: fixed lead `AutomationEngine.cpp:10291/10316/10337`; landing peak `:14013/:14048`; drift evidence
  `PICKUP_PROMPT_TIP_TIMING_METER_DELAY.md §4`, `BOT_MAXOUT_PLAN_2026-08-11.md §2.1`.

### A3 `NEW` — Consensus / confidence veto gate: fire only on agreement, skip on divergence
**This is the actual "no mistimed shot, no matter what" mechanism — precision over recall.**
- **Observation:** today the bot fires whenever a release is *schedulable*, even when its own predictors disagree — which
  is precisely the moment it is about to mistime. You already compute two independent tip estimates (phase crossing +
  press-anchored, both inside `canonicalAutonomousTipDecision`, `:10752`) and a predictor sigma (`predictedTipSigmaMs`,
  `:6961`).
- **Build:** a gate that fires only when `|phaseTip − pressAnchoredTip| < τ` **and** `sigma < σ_max`. On divergence or
  high sigma, **hold/skip** (a clean no-shot) instead of committing a bad release. τ and σ_max tunable per shot type.
- **Why it enforces consistency:** converts would-be under-times into no-shots. You asked for "no under/overtimed shots
  all the time no matter what" — that is definitionally a decision to **not fire** when the pipeline can't be trusted.
  This is the lever that makes the guarantee real, at the cost of some shot volume during genuine lag/jitter.
- **Not the refuted idea:** the docs rejected press-anchored as a *primary firing source* (σ 44-83 ms vs a ~13 ms
  window). Using it as a **veto witness** is the opposite use and is untested. REFUTED ≠ this.
- **Risk:** LOW mechanically (it only ever *suppresses* a fire), but tune τ/σ_max so it doesn't over-skip good shots.
- Refs: `AutomationEngine.cpp:10752, 6961`; press-anchored formula `:11180-11181`.

### A4 `PLANNED-UNBUILT` — Arm MMCSS "Pro Audio" on the chiaki feedback-sender thread (biggest raw jitter kill)
- **Observation:** the scout confirmed the chiaki send leg is **event-driven and immediate** — `feedback_sender_thread_func`
  wakes on the state-changed condition and sends at once (`feedbacksender.c:244, 277-281`). That thread runs at **NORMAL
  priority**, so it can be preempted up to a scheduler quantum (~1-15 ms) **right at the send instant.** That is raw
  jitter injected into `fill_at_rel`, and F3 shows that jitter **leaks into peak on Fades/Go-To.**
- **Build:** register MMCSS "Pro Audio" in `feedback_sender_thread_func`, reverted on exit (mirror of the takion recv
  thread; **not** `TIME_CRITICAL`). Full verbatim hunks already written: `.audit_raw/BUILD_chiaki_mmcss.md`.
- **Why it enforces consistency:** collapses the single largest input-jitter source from milliseconds toward
  sub-millisecond. The native worker is already hi-res + spin-wait precise (`OrionAppController.cpp:1350-1400`); the
  chiaki leg is the unhardened one.
- **Risk:** LOW. Confirm in Process Explorer; env kill-switch available.
- Refs: `feedbacksender.c:244, 277-281`; plan `BOT_MAXOUT_PLAN_2026-08-11.md §1.1`.

### A5 `NEW` — Light up the DORMANT console-tick phase alignment (build the missing probe, the consumer already exists)
- **Observation:** a big share of the residual ±16 ms is **quantization beat** between your send clock and the console's
  60 Hz input poll. You already have **both** consumers written: `tickAlignedFireDeadlineMs` (`OrionAppController.cpp:11044-11062`,
  "Default OFF") and the earlier-only tick-snap in `scheduleFire` (`AutomationEngine.cpp:11731-11757`). They are **inert**
  because the tick-phase estimate never locks — "not one probe label has ever been accepted on this install" (`:11719`).
- **Build:** the missing piece is only the **phase probe** — a short warmup that staggers releases across the tick
  interval and fits the console poll phase from the observed landing quantization (feed
  `latency_estimator.py` Phase-2 `tick_phase_ms/conf/sd_ms`, `:41-46`). Once it locks, the existing snap logic dedents
  every fire to a consistent sub-poll phase automatically.
- **Why it enforces consistency:** removes the send-vs-poll beat — a pure jitter kill with **the expensive half already
  built and gated.** Cheapest ratio of "consistency gained" to "new code."
- **Risk:** LOW — the consumers are default-OFF and only engage once the probe produces a trustworthy fit.
- Refs: `OrionAppController.cpp:11044-11062`; `AutomationEngine.cpp:11719-11757`; `latency_estimator.py:41-46`.

### A6 `NEW` — Sub-frame peak estimation for the GRADE/learner signal
- **Observation:** the crossing prediction is already sub-frame (`TemporalSampler`, `:429-635`), but the **peak that
  grades each shot** is the discrete max observed frame `meterCapPeakFillPct_` (`AutomationEngine.cpp:14013`) — quantized
  to ~16.7 ms / ~3.7pp buckets. On the self-correcting types (Standstill, `corr(far,peak)≈0`) that quantization noise is
  a real chunk of the peak_sd 3.11pp — i.e. part of the "irreducible" residual is **measurement**, not the shot.
- **Build:** fit post-release `fill(t)` with the same weighted least-squares the sampler already runs and take the
  **continuous peak/asymptote** instead of the discrete max. Emit that as the graded `peak_fill`.
- **Why it enforces consistency:** a cleaner error signal makes A2's controller and the phase-learner converge tighter
  and stop chasing quantization noise. Small standalone gain, but it **multiplies** A2.
- **Risk:** LOW — grading/telemetry only; does not change *when* a shot fires.
- Refs: `AutomationEngine.cpp:14013/14048`; sampler fit `:429-635`.

### A7 `NEW` — Unblock the data: resolve the two-instrument contradiction + log per-shot features on MADE shots
**A prerequisite that makes A1/A2 measurable instead of guessed.**
- **Two of your latency instruments openly disagree.** The frozen-meter oracle says release→observed-freeze **is**
  release-path latency (`latency_estimator.py:1-13`); the C++ `recordLandingLeadSample` header says that same post-release
  travel is **capture-side staleness, uncorrelated with the command** (`AutomationEngine.cpp:14017-14019`). That
  contradiction is exactly your `travel_pp 52 vs 59` question. A rig probe (press→first-meter-deflection on the same pipe)
  settles which is right and pins the ~63 ms A1 needs.
- **The rich at-fire feature dump (`TIP DEADLINE DECISION`, `:7021-7068`) is emitted only on MISSES.** On a *made* shot,
  only `armed_source/armed_sigma/armed_fill/command_eta` survive to the `Outcome identity` line (`:14606-14620`), and
  **RTT-at-fire is latched (`shot_.networkOffsetMs`) but never emitted on the landing line.** So today you cannot regress
  travel against pipeline state on the shots that matter.
- **Build:** emit velocity-at-fire, `frame_age_ms`, RTT-at-fire, sigma and `command_eta` on **every** graded landing
  line. Then a 20-minute offline regression tells you whether the Fade residual is RTT-driven, velocity-driven, or truly
  random — i.e. whether A2's controller should be feed-forward (keyed on RTT) or purely feedback.
- **Risk:** NONE — observability only.
- Refs: `AutomationEngine.cpp:7021-7068, 14606-14620`; `latency_estimator.py:1-13`; RTT latch `:3028/:4642`.

### Dead ends — do NOT re-open (confirmed refuted)
- Meter delay as a **greens-improver via the predictive path** — refuted end-to-end (`METER_DELAY_PLAN §2`). (Reactive
  path A1 is the only live use.)
- **Aim higher / per-type aim offset** — F2 proves a constant offset buys **exactly zero**; it's a spread problem.
- `ORION_READER_SUBPIX_EDGE` (adds 1.88 ms bias to remove 0.23 ms noise), `ORION_CAPTURE_MJPG` (driver returns YUY2),
  1080p detection (null, p=0.80) — all correctly parked (`REVIEW_ASK C4`).
- Inflate tip-timing constant to widen headroom / lower base lead under delay — both mistime by construction
  (`METER_DELAY_PLAN Options 3-4`).
- **`travel_pp` as an independent signal** — it is `peak − fill_at_rel` by definition (`:14048`); see B-C1.

---

## Part B — Adversarial review (ranked by ship risk before Aug 28)

Theme: every Part-4 finding is **diagnostic blindness** — a reason computed then discarded — not a mistimed-shot bug.
None change *when* the bot fires; they change *whether you can see why a shot failed* during the last calibration window.
That is the load-bearing risk right now.

### B1 [MODERATE] Square passthrough fabricates a phantom "engine override bug" in the release-ownership trace
`OrionAppController.cpp:9800, 9836-9838`, read as a bug at `:9871-9874`
Holding R3 during a **real shot's Releasing/Cooldown window** makes the wrapper inject `XINPUT_GAMEPAD_X` into `output`
for those ticks, so `updateReleaseOwnershipTrace` records `out_cleared_all=0 / max_out_sq=1` — which the code's own
comment reads as *"any 0 here is a real engine override bug."* The passthrough **manufactures the exact signature this
branch exists to hunt** ("locate the residual timing error"). Telemetry-only, niche trigger — but it poisons the
diagnostic you're using to ship. **Fix direction:** exclude passthrough-injected X from the ownership trace, or tag it.

### B2 [MODERATE] Acquire-veto reason is written then overwritten — operator always sees `no_meter`
`simple_meter_reader.py:5982-5997` overwritten at `:6125` (or `:6182`)
The cold-acquire B7/B8 vetoes assemble a rich `last_debug` (`border_veto` / `arm_edge_veto`, with box and running counts),
then set `col=None`; the downstream no-meter path **overwrites `last_debug` with `stage="no_meter"`** before the
orchestrator reads it (`:1211`). The `_border_vetoes` / `_arm_edge_vetoes` counters' **only** reader is that doomed field,
so they're dead too. **Failure:** a mullion/signage/edge-clipped red blob is correctly rejected, but you see only
`no_meter` and cannot tell "nothing on screen" from "decor vetoed N times." Same shape as the known `:5678-5681` bug, in
the nominal path. **Fix:** don't overwrite `last_debug` when it already holds a veto stage this frame.

### B3 [MODERATE] Capture-health tail STILL truncated — `core_black_run` / `core_static_run` never reach the log
`remote_play_orchestrator.py:2916-2939` cut by `RemotePlaySession.cpp:2682` `.left(300)`
The `SUSPECT`/`cap_mode` move to the front (commit `f77be10`) fixed only those two fields; the line still renders ~360
chars against a 244-char effective budget, so the tail — `preview_dup_refresh`, `core_black_run`, `core_static_run`
(degraded/frozen-feed **failure** indicators) — is still cut. The test pins only the rescued fields
(`test_capture_health_diagnostics.py:101-102`), so the truncation ships knowingly. **Fix:** raise `.left(300)` (same cap
at `:2603, :2682, :2716`) or move the two `core_*_run` fields ahead of the `raw_*` block.

### B4 [LOW-MOD, latent] Config band lets `meter_delay_lead_offset_ms` exceed the schedulable ceiling with no complaint
`AppConfig.cpp:789-804` (silent `std::clamp`, no warn), band `AppConfig.h:606-607`
`cleanDouble`/`cleanInt` clamp every value silently. `meter_delay_lead_offset_ms` band max is **400**, but the live
schedulable ceiling `maxSchedulableTipLeadMs()` is **386.3** (B-C confirms) — so the band's own maximum already exceeds
the ceiling, and `base_lead + offset` can load far past 386 **with zero config-time diagnostic.** This is the shape of
the fixed "155 → 445 → 151/151 aborts" bug. A runtime guard exists (`METER DELAY LEAD UNCALIBRATED`,
`AutomationEngine.cpp:13247-13303`) but fires **only when delay is applied**; with `meter_delay_enabled=false`
(`settings.json:73`) the over-ceiling value is accepted and reported nowhere. **Live values are safe today** (lead 297 +
offset 90 = 387 with delay on, delay off) — latent, not active. **Fix:** warn at load when `base_lead + max(0,offset)`
exceeds `maxSchedulableTipLeadMs()`.

### B5 [LOW] Courtwide steal veto discards the candidate and can suppress the search line entirely
`simple_meter_reader.py:5675-5704` (the known `:5678-5681`, verified exactly)
B8/B7 set `_cw=None`; the log's `found` is `int(_cw is not None)` → `found=0` after a veto, `box=-`; and the emit gate
fires only when `_cw is not None` **or** the epoch changed, so a vetoed candidate on a repeat search within an epoch
produces **no line at all.** A found-then-vetoed steal is indistinguishable from "nothing found." **Fix:** log the veto
reason before nulling `_cw`.

### B6 [MODERATE, UX] Square passthrough is default-ON via the Tempo toggle with NO UI to see / rebind / disable it
No `Q_PROPERTY` for `squarePassthroughEnabled`/`squarePassthroughButton` anywhere (`OrionAppController.h` has only
`tempo*` siblings); `setTempoEnabled` (`:9076`) arms the passthrough gate with compiled default `button="r3"`. A user who
flips "Tempo Shot" on **silently loses R3** (right-stick click) to a Square rewrite with no discoverable cause and no way
out but hand-editing `settings.json`. Default installs are safe (`tempoRemapEnabled` defaults false, `AppConfig.h:419`).
**Fix:** expose an enable + rebind in the Tempo UI, or document it.

### B7 [LOW] Square passthrough gate ignores engine armed/enabled state
`AutomationEngine.cpp:3061` keys off the config flags only. When automation is off/disarmed, `processInternal` already
returns `physical` unmodified (`:3185`) — Square is reachable — yet the wrapper still injects a second Square on R3, the
exact "surprise second binding" the commit says it avoids. Benign (same Square the physical button would emit), but a
rationale mismatch. **Fix:** gate on whether the remap actually consumed Square this tick.

### B8 [TRIVIAL] "Five return points" is six
`processInternal` returns at `AutomationEngine.cpp:3174, 3179, 3182, 3185, 3214, 3266` (the commit + `:2469-2471` say
five). No behavioral impact — all six are inside the private method, so all flow through the wrapper. Doc/count only.

### Square #88 — core safety CONFIRMED (do not re-investigate)
- **No false-shot path.** Every shot-intent read keys off `physical` (`lastPhysical_ = physical`, `:3084`;
  `shotsAttempted_++` under `physical.square()` only, `:4669/6118`); no engine state stores prior `output`; the PRESSED
  overlay reads physical (`:10640`); latency attestation requires neutral physical so a held R3 disqualifies it. Injected
  output-X reaches only the console forwarders (the intended steal).
- **R3 consumption safe** — only output-forwarders read output-R3; the three cited non-consumers confirmed
  (`WinMmButtonMapping.h:47`, `OrionAppController.cpp:479/529`).
- **Rename clean** — one production caller (`OrionAppController.cpp:10912`); `processInternal` reached only via the wrapper.
- **Default bit** resolves to `RIGHT_THUMB`; bit-held test reads `physical`; L3 is opt-in only.
- **All six config-plumbing sites** present and consistent; `VeniceProfile.*` does not exist; load uses fallback-to-default.

### Part-2 / Part-3 claim checks
- **C1 is CIRCULAR (confirmed).** `travel_pp := peak_fill − fill_at_rel` (`AutomationEngine.cpp:14048`). With
  `fill_at_rel` held ~equal (39.0 vs 39.4), "differ in travel_pp" is the *same statement* as "differ in peak" — and peak
  is the grouping variable. The **non-circular** fact is the equal command fill; drop travel_pp from the argument.
  **My Part-0 refines the conclusion:** "residual downstream of the press" is true **only for the self-correcting types**
  (Standstill/No Dip); for Fades/Go-To fire jitter leaks into peak, so it is *not* purely downstream there.
- **C5 green_zone instrument is meaningless — distrust it, but not because the label is hardcoded.** `_gz_end_shot`
  (`simple_meter_reader.py:4589-4627`) writes a real 3-way label, but `fill_used` is the fill at the **release frame**
  (~39) while `g_lo` is the ~20th percentile of **top-of-meter** fills (~90) — ~55pp apart on the curve — so every record
  is `EARLY` and, with `g_hi=100`, nothing is ever `OVER`. Structural mismatch, not a broken field. The reader's own
  docstring concedes it "is never evidence that NBA 2K made or missed the shot" (`:4592-4595`). **Believe the
  `meter_x≥0.70` finding over this instrument.**
- **B1 ceiling CONFIRMED = 386.3 ms.** `maxSchedulableTipLeadMs() = effectiveTipPhaseConstantMs() − 30`
  (`AutomationEngine.cpp:13200`, margin `:69`); live `effectiveTipPhaseConstantMs = 342.3 + 74.0 = 416.3`; ceiling
  `= 386.3`. Matches the code's own worked examples (`:13237, :13263`) and the ask.
- **Part-2b (offset 90):** total lead 297 + 90 = **387 with delay on** — one ms over the 386.3 ceiling, i.e. the very
  edge. Safe only because delay is currently off; if you enable meter delay, 90 is **not** a safe offset at this ceiling
  (see B4). The `155 → 90` change was directionally right; the correct value under delay-on is ≤ **89**.

---

## Bottom line
The consistency win is **not** a better reader or a smarter tip predictor — F2 kills "aim higher," and the reader is
already sub-frame on the crossing. It is **(1) shrink the fire horizon** (A1 reactive rung, A4 MMCSS, A5 tick-align —
one already-written consumer just needs a probe), **(2) close the loop per-shot** (A2 bounded controller on peak-error,
fed by A6's clean peak), and **(3) refuse to fire when the pipeline can't be trusted** (A3 consensus veto — the real
"no bad shot, no matter what"). A7 is the cheap observability change that turns the Fade residual from a guess into a
measured number before you build A1/A2. On the review side, nothing is a ship-blocker, but the four diagnostic-blindness
findings (B1-B3, B5) are worth an hour each *now*, because they're the eyes you tune the timing with before Aug 28.

*Numbers in Part 0 reproduce from `logs/orion_native.log` read-only, grouped within shot-type (no cross-session `seq`
join). Code claims carry `file:line` to the main tree, not the `.claude/worktrees/*` copies.*
