# Meter Delay — Design Plan

**Answer to `METER_DELAY_ASK_2026-08-11.md`.** PLAN only — no code was modified, the app was not run.
**Date:** 2026-08-12 · **Branch:** `fix/timing-input-and-remoteplay-blockers` · **Ship (EA):** 2026-08-28
**Method:** 5 parallel read-only agents traced every number in the ask to real `file:line`, cross-checked
against live `settings.json` + `learning.json` + `logs/orion_native.log` (session-namespaced, anchored
greps per `BUG_AUDIT_BRIEF.md` §2/§3); the fill-rate claim was then **re-measured frame-by-frame on the
owner's own two clips** (§9), and the predictive-vs-reactive architecture traced end-to-end (§8).
CONFIRMED = found in code/log or measured. REFUTED = code/frames say otherwise.

---

## Verdict up front

**Two questions, two answers — read both.**

**(a) Can the bot's CURRENT (predictive) architecture make more greens with delay? NO.** The code disproves
the premise; it doesn't merely fail to prove it. Two walls — *for the predictive path*:

1. **Delay gives the predictor no extra time.** Fill geometry is delay-invariant — proven by the code's own
   math (a constant hold is "bit-for-bit the original stream, merely time-shifted") **and now re-measured
   frame-by-frame on the owner's two clips (§9): fill rate identical, 270 vs 276 ms.** Delay moves the
   readable window **later** and **inflates the required lead** past the ceiling any in-video anchor can
   supply. Strictly harder to schedule. The only benefit the codebase claims for delay is **legibility**,
   never lead.
2. **The one delay-immune source can't hit the window open-loop.** Press-anchored is the only tip source not
   tied to the late meter, but it's **adopt-only-on-failure**, phase pre-empts it, the code's post-mortem
   says it **"HAS NEVER ARMED"** reliably, and its dispersion is **±44–83 ms** against a ~13–32 ms green
   window.

**(b) Can meter delay be made to work AT ALL? YES — by switching to a REACTIVE fire under delay (§8).** This
is exactly how human "delay method" players green on *more* delay than we run: they **react** to the delayed
bar instead of predicting ahead, and delay self-cancels for a reactor (reference and action ride the same
delayed pipe). A reactor needs only **~63 ms** of output-latency lead vs the predictor's **290 ms** — so it
is **delay-invariant** where the predictor is delay-fragile. Every ingredient already exists in the code.

**Recommendation (unified):**
1. **Build the reactive mode** — §8 Option 6, behind a flag + `applied_delay>0`, default-OFF. This is the
   real path and it supersedes the earlier "abandon" line in §6.
2. **Land the two service correctness fixes** — §5 Option 5 (narrow the detector port range; verify the
   deployed cap) — regardless of the delay verdict.
3. **Keep ship-disabled (§5.1) as the fallback** only if the reactive experiment fails its pre-registered
   bar.
The full ordered roadmap is §10.

---

## 0. Fact-check of the ask (CONFIRMED / REFUTED, with live-state drift flagged LOUD)

| Ask's claim | Verdict | Live/real value |
|---|---|---|
| Base Shot Lead **280 ms** | **REFUTED (stale)** | live `actuation_lead_ms = 290` (`settings.json:4`); `280` is nowhere in `native_orion/src` |
| Usable ceiling **398 ms** | **REFUTED (stale, derived not constant)** | live ceiling computes to **~386 ms** = `effectiveTipPhaseConstantMs() − 30` (`AutomationEngine.cpp:13161`) |
| Headroom **+118 ms** | **REFUTED (stale + conflated)** | live headroom = `ceiling − baseLead` = **~96 ms** (matches code comments "96", `AutomationEngine.cpp:13198`). The `118` in code is an **unrelated** single-landing offset (`AppConfig.h:589`), not a headroom. |
| Offset **~155 ms** | **CONFIRMED as a live user setting; REFUTED as a formula** | `meter_delay_lead_offset_ms = 155` (`settings.json:74`); operator-calibrated from the game's TIMING banner — code explicitly says the "lead += delay" formula is REFUTED (`AppConfig.h:585-593`) |
| At offset 155: **10 aborts, 0 shots** | **CONFIRMED (mechanism); tally is empirical** | abort predicate `AutomationEngine.cpp:7035`; 26 real `SHOT LEAD CONFLICT` lines in the log, one Aug-10 D=200 session ran aborts #8→#15+ |
| Delay does NOT slow the meter (rate unchanged) | **CONFIRMED by code** | design math: constant delay = time-shift, rate multiplier `1/(1+D′)=1.0` (`nexus_svc.py:370-379`); geometry unchanged (`AutomationEngine.cpp:6682`) |
| Every in-video tip source anchored on the late meter | **CONFIRMED** | sampler/registration/phase all born from `result.fillPct` at capture time (`:2931`, `:2747`, `:13018`) |
| Press-anchored is the only delay-immune source | **CONFIRMED** | `tipAbsVisibleMs = pressWallMsForEpoch_ + learnedTipMs + vMs + meterDelayAppliedMs_` (`:11180`), press stamped pre-pipeline (`:3579`) |
| Press-anchored **"57.7% good vs phase 78.3%"**, "the key to the feature" | **UNVERIFIED — self-sourced** | that figure appears **only in the ask**. Real batches: 4/6 good and 0/2 (`RIG_BATCH_2026-08-11.md:24`, `.audit_raw/P1b_timing_accuracy.md:14`); code post-mortem `:11121-11144` says it has never reliably armed |
| Service caps delay at **300 ms** until restart | **REFUTED for current source** | current hard cap is **600** (`MeterDelayIntercept.cpp:140`, `kHardMaxMs=600`). "300" is a **stale docstring** + old-value changelog (`nexus_svc.py:1030,:394`). Real *only* if a **pre-2026-08-08 binary** is deployed (cap is a `constexpr` baked at bridge start). |
| Port mismatch 30099 (detect) vs 30020 (intercept) | **CONFIRMED** | `CourtIpDetector.h:36-37` (30000–30099) vs `MeterDelayIntercept.cpp:510-513` (30000–30020) |

> **Bottom line on the numbers:** `280 / 398 / 118` are internally self-consistent and match the **Aug-10
> log snapshot**, but not the **live state**, which reads **290 / 386 / 96**. The learner has drifted the
> tip-phase constant since the ask was written. **This does not change the answer** — the offset (155) still
> dwarfs the headroom (96 or 118) either way — but every specific figure below is given in both epochs so
> nobody re-derives from a stale one.

---

## 1. The arithmetic, worked through

**The abort.** Live-tip shots abort when the lead the actuator must apply exceeds what the tip anchor can
schedule (`AutomationEngine.cpp:7035`):

```cpp
if (config_.tipPhaseEnabled && effectiveLeadMs > maxSchedulableTipLeadMs())
    → emit "SHOT LEAD CONFLICT" → relinquishAutonomousLiveMeterShot(..., "live_tip_deadline_missed")
```

**The two quantities:**

```
effectiveLeadMs      = actuation_lead_ms + appliedMeterDelayLeadOffsetMs()      (:10266)
                       = baseLead + (offset, applied only when delay is running, :10227)

maxSchedulableTipLeadMs = effectiveTipPhaseConstantMs() − 30                     (:13161, margin :69)
```

| Term | Aug-10 log snapshot | Live now (`settings.json`+`learning.json`) |
|---|---|---|
| base lead | 280 | **290** |
| ceiling (usable max) | 398 | **~386** |
| headroom (`ceiling − base`) | **+118** | **~96** |
| offset (delay running) | 155 | 155 |
| effective lead = base + offset | **435** | **445** |
| conflict margin (`effLead − ceiling`) | **+37 over** | **+59 over** |

Either way: **the configured offset (155) is larger than the entire headroom (96–118).** The moment meter
delay is enabled, effective lead jumps past the ceiling and **every** live-tip shot aborts. Meter delay is
currently `enabled=false`, so this is a **projected** outcome of turning it on, not a bug firing today.

**Why the offset is needed at all.** Delay detaches the on-screen meter from the shot animation by
≈ `0.57·D` (ask fact 4; asserted from frame measurement — *I did not re-derive this, see §7*). The bar now
completes **after** the ball leaves the hand, so the actuator must fire earlier relative to the meter — the
offset. At D=200 the operator calibrated 155 ms from the in-game banner.

**The gap is structural, not a tuning miss.** To fire an in-video shot under delay you need
`offset(D) ≤ headroom`. You have one empirical anchor: `offset(200) = 155`. Extrapolating linearly from
that single point (~0.78·D) or from the detachment slope (~0.57·D):

| To fit headroom of… | at ~0.78·D | at ~0.57·D |
|---|---|---|
| 96 ms (live) | D ≤ ~123 ms | D ≤ ~168 ms |
| 118 ms (Aug-10) | D ≤ ~151 ms | D ≤ ~207 ms |

So a delay in the **~100–150 ms band** is the *only* region where an in-video (phase) shot can still
schedule. Above that, phase is starved and you are left with press-anchored alone. **Single anchor point —
this linearity is a weak assumption and must be measured (§7).**

---

## 2. Does a delayed meter help CV at all? — NO MECHANISM FOUND

Traced end-to-end through `meter read → predict → schedule`. Delay cannot buy the CV pipeline more usable
time. Five independent reasons, all from code:

1. **Fill geometry is delay-invariant.** `AutomationEngine.cpp:6682`: *"the meter's own anchor→stop geometry
   is unchanged under delay (PHASE SAMPLE 355-380ms on vs 332-372ms off)."* Same window width, same frame
   count at any D. There are no extra frames to gain — matches the ask's 283/268/275 ms.
2. **In-video anchors are bounded to ~one tip-constant of warning, and delay does not raise that bound.**
   `maxSchedulableTipLeadMs()` = `tipPhaseConstant − 30` (~386–391 ms). A crossing cannot be dated earlier
   than the fill is observed.
3. **Delay raises the *required* lead while the warning horizon stays fixed.**
   `fireAtMs = predictedTipMs − effectiveLeadMs` (`:6824`); `effectiveLeadMs` grows by the offset under
   delay. Correct lead under an inbound delay D is `loop + D`, but the meter still arrives only
   ~tip-constant ahead → `visibleEvidenceLeadStarvedByMeterDelay()` (`:13164`) **withdraws** the in-video
   extrapolators. Harder, not easier.
4. **The only benefit the codebase claims is legibility, not lead.** `MeterDelayController.h:9-11`: delay
   *"decouple[s] the shot meter from the shot animation, which gives the vision reader a cleaner meter to
   read."* That is read **quality** (meter stops overlapping the release animation). **No consumer converts
   "cleaner" into "earlier"** — the whole schedule path dates the deadline *from* the observed tip, so a
   later meter yields a later `predictedTipMs` with the same lead requirement.
5. **Firing to the detached green lands late on the animation.** Because the bar completes after the ball
   leaves the hand, hitting the meter's green window is not the same instant as a good release.

> **Language correction for the plan (from code review `:6676-6683`):** the Remote-Play video (console→PC)
> is **not** delayed — only the game-server→console UDP flow is filtered. The precise cause is *"the meter
> is detached / rendered late relative to the animation,"* not *"the video is delayed."* Substance of the
> ask's mechanism claim holds; the phrasing should be fixed.

**If there is any live win from delay, it is a narrow, unquantified legibility gain at small D — a
detection-*accuracy* improvement, never a scheduling-*time* improvement. That is the single hypothesis
worth one experiment (Option 1). Everything else is refuted.**

---

## 3. Per-shot-type reality (dispersion vs the green window)

One global delay/offset does **not** make sense — press→tip spans **463 ms (No Dip) to 1969 ms (Go-To)**,
and the dispersion that decides whether a press-anchored release can hit green varies 2× across types.
Live values (`settings.json:128-148`):

| Shot type | press→tip ms | n | σ (ms) | Est. green rate if press-anchored & perfectly centered* |
|---|---|---|---|---|
| Left Fade | 829 | 100 | **45** | ~11% |
| Right Fade | 862 | 100 | **51** | ~10% |
| Go-To | 1969 | 42 | 54 | ~10% |
| No Dip | 463 | 60 | 69 | ~7% |
| Standstill | 639 | 100 | **83** | ~6% |

\* `P(|N(0,σ)| < 6.5 ms)` for a ~13 ms green window, **best case** (zero bias). Compare **delay-off phase
≈ 78% good**.

> **This estimate is NOT a ceiling.** `BUG_AUDIT_BRIEF.md` §3 explicitly warns that quantization-style
> performance arithmetic ("60 Hz = 34%") was tried and **retracted as circular**. Treat the table as a
> *prior* that says "single-digit-to-low-teens greens, far below delay-off," to be **confirmed or refuted on
> the rig**, not as proof. The direction is corroborated by the code's own post-mortem and by the real
> batches (4/6, 0/2 — too small to settle, not encouraging).

The two lowest-σ types (Left/Right Fade, both n=100) are where press-anchored has any chance, so any
experiment should run there — if it fails on those, it fails everywhere.

---

## 4. Ranked options — gain, risk, and a measurable rig experiment for each

### Option 1 — Narrow delay (~100–150 ms), keep phase primary *(predictive-path hedge; see Option 6 for the real #1)*
- **Idea:** auto-clamp D so `offset(D) ≤ headroom`, so effective lead stays under the ceiling, **phase is
  never starved**, and you keep accurate phase timing *plus* whatever small legibility gain delay buys.
- **Gain:** LOW-but-real if legibility at small D helps detection on animation-heavy shots; otherwise ~0.
- **Risk:** **LOW.** No shot-path logic change if implemented as a clamp on D + a banner-calibrated offset.
  Only failure mode is offset mis-calibration re-tripping the conflict — prevented by the auto-clamp.
- **Rig experiment (RUN FIRST, it's the safe one):** pick the shot type whose delay-off phase greens are
  cleanest. **60 shots delay-off** vs **60 shots at D=120** (offset calibrated from the banner, auto-clamped
  ≤ headroom). **PASS:** green rate at D=120 ≥ delay-off green rate **AND** abort rate ≤ 5%. **FAIL:** fewer
  greens, or aborts > 10%. Expected: neutral-to-slightly-positive greens, near-zero aborts.

### Option 2 — Press-anchored primary under delay *(rank #2 — the falsifier)*
- **Idea:** when `applied_delay > 0` and in-video is starved, promote `press_anchored` ahead of the phase
  exemption so it actually arms (`AutomationEngine.cpp:11070-11078` + `:11194`).
- **Gain:** LOW (§3: est. 6–12% greens vs delay-off 78%). This is the **only** option that fires at all at
  large D, so it's worth *disproving with data* rather than by argument.
- **Risk:** **MEDIUM–HIGH.** Real change to the highest-consequence file (the shot path), touching the
  phase-exemption invariant. Regression surface on epoch/generation guards. And it fires *inaccurately* even
  when it fires.
- **Rig experiment:** behind a flag, force press-anchored primary at D=200 on **Left Fade + Right Fade**
  (lowest σ, n=100). **100 shots.** **PASS:** green rate ≥ delay-off phase green rate for the same type.
  **FAIL (predicted):** green rate in single-digits/low-teens. Value: a clean, cheap kill.

### Option 3 — Inflate Tip Timing to widen headroom to ≥155 *(reject)*
- **Idea:** push `learned_phase_physical` toward band max (430) → ceiling ~458 → headroom ~168 ≥ 155.
- **Gain:** **NEGATIVE.** The phase constant would no longer match where the meter actually anchors → the
  schedule fires on a runway that isn't there → systematically mistimed. This is the exact "two-knob
  impossibility" the tests already encode (`AutomationEngineTests.cpp:653`).
- **Risk:** HIGH — corrupts delay-**off** timing too (same constant). **No experiment; refuted by
  construction.**

### Option 4 — Lower base lead under delay *(reject)*
- **Idea:** cut `actuation_lead_ms` so 290+155 fits.
- **Gain:** **NEGATIVE.** Base lead covers loop + pipeline latency; the correct lead under delay is
  `loop + D` (**higher**, not lower). Cutting it fires late systematically.
- **Risk:** HIGH. **No experiment.**

### Option 5 — Fix the two real service defects regardless *(do this independent of the verdict)*
- **Port mismatch (CONFIRMED):** narrow `CourtIpDetector` to **30000–30020** to match the WinDivert filter
  (no 2K26 port evidence on the 30021–30099 tail; widening the filter is the behaviour-changing direction,
  narrowing the detector is the conservative one). Today a court flow on 30021–30099 makes meter delay
  report `active=true` while intercepting **zero** packets — a silent no-op that can burn a whole tuning
  session.
- **Cap (verify):** confirm the **deployed** `VeniceNetSvc.exe` is a post-2026-08-08 build (cap 600). If a
  stale binary is installed it still caps at 250/300 until rebuilt+reinstalled. Fix the stale docstring at
  `nexus_svc.py:1030` while you're there.
- **Gain:** correctness. **Risk:** LOW. **Experiment:** with delay on and a known court port in 30021–30099,
  assert `intercepted > 0` after the narrow-fix.

---

## 5. What to ship on Aug 28

Ranked, given §2 + §3:

1. **Ship disabled + one honest sentence.** Default is already `meter_delay_enabled=false`; the "backend
   disarmed" banner is already honest. Zero risk, zero new work. *This is the recommended EA state.*
2. **Ship flagged experimental, hard-clamped to Option 1's narrow-delay regime**, with the port fix landed —
   *only if* a champion wants the toggle visible. This is the one configuration that is both safe (can never
   trip the conflict) and possibly useful. Do **not** ship the current large-D behaviour: it aborts 100%.
3. **Remove the UI** — only if the team decides the toggle is a permanent footgun. More work; can wait past
   EA. Not required for a safe ship.

**Do not ship:** meter delay enabled at D≥~170 with the current arbitration. That is a guaranteed
all-abort session for a paying customer.

---

## 6. Recommendation for the PREDICTIVE path only — ⚠️ SUPERSEDED by §8/§10

> **This section is retained as the evidence trail for the predictive architecture. Its "abandon" conclusion
> was correct when scoped to predict-and-lead, but the reactive path (§8) is the live recommendation. Read
> §10 for the actual roadmap.**

**Abandon meter delay as a greens-improver *if the architecture stays predictive*.** The code disproves the
premise: delay doesn't widen the read window, it shifts it later and inflates the required lead past the
schedulable ceiling; the only delay-immune *open-loop* source disperses far wider than the green window.
Concretely:

1. **This week:** run **Option 2** (the falsifier) — flag on, Left+Right Fade, 100 shots, pre-registered
   bar (green rate ≥ delay-off phase). Cheap, and it converts "we think it can't work" into "we measured
   that it doesn't." **Budget: one rig session.**
2. **In parallel:** land **Option 5** (narrow the detector port range; verify the deployed cap). Correctness
   fixes worth having regardless of the delay verdict.
3. **If Option 2 fails as predicted (expected):** ship **disabled + honest explanation** for Aug 28 (§5.1)
   and redirect the two weeks to the variance-kill levers in `BOT_MAXOUT_PLAN_2026-08-11.md`, which attack
   the *actual* inconsistency source.
4. **Optional low-priority hedge:** run **Option 1** (narrow-delay A/B, 60+60) to see if small-D legibility
   is a real detection-accuracy win worth a hard-clamped experimental toggle. If it's neutral, don't ship
   the feature at all.

**The honest headline the ask asked for: "No — meter delay cannot produce more greens than delay-off, and
here is why," saves ~2 weeks** versus chasing an offset/lead tune that the arithmetic and the mechanism both
say cannot close.

---

## 7. Assumptions I could NOT verify + contradictions with the ask (flagged loud)

**NOW VERIFIED on the owner's own footage (2026-08-12) — the load-bearing assumption is CONFIRMED:**
- **Fill rate is unchanged by delay.** Measured directly from two owner-supplied 1080p/59.94 fps clips (one
  delay, one not), tracking the meter by its green-tip + red-fill colour and measuring the 20→80% charge
  time on 4 clean full charges in each: **Video A = 266/271/270/271 ms (median 270), Video B =
  276/277/277/274 ms (median 276).** Six ms apart — a third of one frame — i.e. identical. Both reach full
  in ~400 ms. A frame-by-frame side-by-side montage (every 33 ms) shows the two meters climbing near-empty
  → full at the same pace. This independently reproduces the ask's 283/268/275 and the code's
  `AutomationEngine.cpp:6682` ("geometry unchanged under delay"). **Conclusion: delay does NOT slow the
  fill; it shifts the meter later and makes it linger (decoupling). §2's "no extra CV time" verdict is
  confirmed on real frames, not just asserted.** Artefacts: `scratch/meterdelay_probe/meter_side_by_side.png`,
  `fill_compare.png`. *Confidence: CONFIRMED (measured).* Detachment magnitude ≈ 0.57·D not separately
  re-measured here.
- **~13 ms green window width.** From code reading, not a live measurement. The §3 estimates hinge on it —
  if the effective good-band is wider under a shifted `settled_fill` threshold (ask fact 5), press-anchored
  green rates rise proportionally. **Measure the real good-band width per shot type before trusting §3.**
- **offset(D) linearity.** Extrapolated from a single anchor (155 @ D=200). The §1 "D ≤ ~123–207 ms" band is
  only as good as that assumption. Measure offset at D=100 and D=150 directly.

**Contradicts the ask — say so loudly:**
1. **Numbers are stale.** Base lead **290** not 280; ceiling **~386** not 398; headroom **~96** not 118. The
   ask's figures are a valid Aug-10 log snapshot; live state drifted. Conclusion unchanged.
2. **"118" is not headroom.** It's an unrelated single-landing lead-offset (`AppConfig.h:589`), conflated in
   the ask with `398−280`.
3. **Fact 7 (300 ms cap) is wrong for current source.** Current cap is **600**; "300" is a stale docstring.
   Real only on a pre-2026-08-08 deployed binary. Verify the deployment.
4. **Fact 6 (press-anchored "the key", 57.7% good) is unverified and probably optimistic.** The 57.7%/78.3%
   figure appears **only in the ask**. Code post-mortem (`:11121-11144`) says the member has never reliably
   armed; real batches are 4/6 and 0/2; live σ (44–83 ms) can't hit a ~13 ms window. Press-anchored is the
   only source that *fires* under delay, but "fires" ≠ "hits green."
5. **"Delayed video" is the wrong mental model.** The video isn't delayed; the server→console flow is —
   which *detaches the meter from the animation*.

---

## 8. THE WORKAROUND — delay-native reactive fire (why humans green on delay and the bot doesn't)

**Reframe (owner, 2026-08-12):** human players run this *same* meter delay — more of it — and time perfectly
to the tip. If they can, "delay can't work" is false. It is false. The error in §2/§6 was scoping the
question to the bot's **predictive** architecture. There is a second architecture — **reactive/closed-loop**
— that the humans are actually using, and the code can support it.

### 8.1 Why the human wins and the bot starves — architecture, not physics
- **Human = closed loop.** Watches the delayed bar, releases on the *visible* green. Delay is
  **self-cancelling** for a reactor: the meter they read and the release they send ride the **same** delayed
  pipe, so "release on visible green" = "release on actual green." More delay *helps* — it peels the meter
  off the fast animation into a clean, late, learnable window.
- **Bot = open-loop predictive.** `processAutonomousLiveMeterHolding` fires
  `fireAtMs = predictedTipMs − measuredLeadForActuationMs()` (`AutomationEngine.cpp:6824`), where the lead is
  the user's **whole-pipeline 290 ms** Shot Lead (`:10294`), applied against a tip *predicted* from early
  samples. It must commit ~290 ms **before** the tip. Delay pushes the readable meter later and inflates the
  lead to 445 → predict-ahead exceeds the horizon → `SHOT LEAD CONFLICT` on every shot. **It is leading when
  it should be reacting.**

### 8.2 The crux number: a reactor needs ~63 ms of lead, not 290
The 290 ms is **not** the bot's output latency — it is the anticipation of the entire
press→animation→meter→visible-tip pipeline, applied against a prediction (`:10281-10289`). The bot's TRUE
output path (press → console registration) is small:

| output-path term | value | source |
|---|---|---|
| controllerChain (`latency_compensation_ms`) | 45 ms | `AutomationEngine.cpp:1371`, settings |
| remotePlayPipeline (internal) | 55 ms | `AutomationEngine.h:191` |
| **modeled pure output path** | **~63 ms** ("GLM-validated 63 ms path") | `AutomationEngine.cpp:7515` |
| learned_latency (estimator) | 75 ms | `learning.json:9` |
| LAN RTT to console | 3–13 ms | `rtt_sync_engine.py`, `learning.json:80-84` |

A reactor watching the live meter leads only by this output path (**~45–75 ms**) — a **4–5× smaller
horizon** than 290. The meter is readable for ~370–400 ms of fill; 63 ms fits inside that with ~300 ms to
spare, **at any delay**. That is the whole point: **the reactive fire is delay-invariant; the predictive
fire is delay-fragile.** Delay stops being a starve and becomes a comfortable, late, readable window — the
same thing that makes it *easier* for the human.

### 8.3 Self-cancelling for the bot too
Meter-delay holds server→console, so the console renders the meter late and the **capture card sees that
same late meter** (`RemotePlaySession.cpp:4221`, `AutomationEngine.cpp:2481`), plus only ~15–25 ms of
capture/decode on top. The bot's reference (its delayed meter view) and the game's grading reference
(delayed console state) carry the **same** D — so a reactive fire cancels the delay exactly as the human's
does.

### 8.4 It fixes the dispersion wall that killed Option 2
Open-loop press-anchored disperses **±44–83 ms** (live σ) against a ~13–32 ms green window → can't hit it
(§3). A **closed-loop** reactor doesn't ride a predicted constant — it watches the actual bar, so its error
collapses to (output-latency jitter + ~1 frame capture + quantization) ≈ **±10–20 ms**, which is *in range*
of the window. Press-anchored becomes the **coarse** delay-immune anchor; the live delayed meter does the
**fine** correction. That hybrid is the human's edge, ported.

### 8.5 Every ingredient already exists in the code
- current fill `shot_.fillPct`; learned green-window start `greenTracker_.startPct()` (driven by
  `release_threshold=93`)
- a working reactive-crossing rung **pattern** already in the (bypassed) legacy `processHolding` block 4
  (`AutomationEngine.cpp:8619`, `reactive_target`) — but it aims fill≥100 with **zero** lead so it lands
  late by design (`:8611`); **adapt it, don't reuse it**
- the precise-fire scheduler (`scheduleFire`, `:6593`) and green geometry are unchanged

### Option 6 — Delay-native reactive fire *(NEW — the real path, RANK #1)*
- **Idea:** under `applied_delay > 0`, add a rung to `processAutonomousLiveMeterHolding` that fires when the
  **live observed** fill crosses `greenTracker_.startPct()` **minus an output-latency margin**
  (~63 ms × fill-rate ≈ ~14 % of fill → fire at ~79 %), instead of `predictedTip − 290`. Lead by output
  latency, not the whole pipeline.
- **Expected gain:** HIGH if it holds — it is literally the human method and the only option consistent with
  "humans green on *more* delay." Converts delay from a guaranteed all-abort into a working mode.
- **Risk:** MEDIUM. New rung in the highest-consequence file (the shot path); gate behind a flag **and**
  `applied_delay>0`, default-OFF, byte-identical when disarmed.
- **Rig experiment:**
  1. **Measure true output latency first** — a press→meter-move probe on the rig; confirm ~63 ms. The
     reactive threshold's accuracy is set by this number.
  2. Flag on, one shot type (start lowest-σ: Left/Right Fade), **D=200**, threshold = `green_start −
     63 ms × rate`. **60 shots.** **PASS:** green rate ≥ delay-off phase green rate **AND** aborts ≤ 5 %.
  3. Sweep the threshold ±30 ms (±~2 frames) to map the sweet spot.
  4. Repeat at **D=400 / 600** to confirm **delay-invariance** (the human's "more delay still works"). If
     green rate holds flat as D climbs, the mechanism is proven.
- **Kill criterion:** if green rate at D=200 is below delay-off after the threshold sweep, the self-cancel
  assumption is wrong on this pipeline → fall back to §5.1 (ship disabled).

### Recommendation change (supersedes §6)
**Do not abandon — build Option 6.** The predictive architecture cannot use delay (§2 stands *for it*); the
reactive architecture can, and it is how the delay method works in the wild. Sequence: (1) measure true
output latency; (2) prototype the reactive rung behind a flag; (3) run the 60-shot experiment at D=200;
(4) if green rate ≥ delay-off, sweep D up to confirm delay-invariance and ship experimental. Keep §5.1
(disabled-by-default) as the fallback only if the experiment fails. Land Option 5 (port fix + cap check)
regardless.

---

## 9. Empirical measurement — the owner's two clips (2026-08-12)

The owner supplied two 1080p / 59.94 fps clips (one delay, one not) to test "the delayed meter fills
slower" directly. It does not.

**Method.** Tracked the meter per frame by its own colour — the saturated green arrow-tip is a unique
on-screen locator; the red fill inside that column is the charge. Fill measured as **raw self-normalized
height** (scale-invariant, immune to where each shot released). 4 clean full charges per clip. Encoding /
log traps per `BUG_AUDIT_BRIEF.md` §3 respected (utf-8 stdout, no cross-session joins). Cross-checked four
ways (raw height, absolute bar %, the app's own historical numbers, the ask's 283/268/275) — all converge.

**Result 1 — fill RATE is identical (delay does NOT slow the fill).**

| | 20→80 % charge times | median | full 0→100 | rate |
|---|---|---|---|---|
| **Video A** | 266 / 271 / 270 / 271 ms | **270 ms** | ~400 ms | 0.222 %/ms |
| **Video B** | 276 / 277 / 277 / 274 ms | **276 ms** | ~400 ms | 0.217 %/ms |

6 ms apart — **a third of one frame** (16.7 ms). A frame-by-frame side-by-side montage (every 33 ms) shows
both meters climbing near-empty → full at the same pace. This reproduces the ask's fact 4 and the code's
`AutomationEngine.cpp:6682` on real frames.

**Result 2 — the meter is DECOUPLED, not slowed (this is what "looks slower" actually is).** A side-by-side
video aligned at the exact frame the meter starts shows: at meter-onset, in one clip the player is **already
mid-shot** (arms up, ball gone); in the other the player **doesn't begin the shooting motion until ~700 ms
later**. The meter's timing *relative to the shot animation* differs sharply between clips — later-to-appear
plus lingering-at-full reads as "slower fill" from the couch, but the 0→100 % speed is unchanged. This is
the decoupling the whole feature is built on, seen directly.

**Which clip is delay:** the one whose charges complete and then **linger pinned at 100 %** (meter finished
while the animation hasn't caught up) and whose meter is on-screen ~74 % of the clip vs ~30 %. Owner to
confirm; the rate finding holds either way.

**Not yet measured (flagged, not faked):** the precise **meter-onset → ball-release offset** per shot (the
hard decoupling number). Needs per-shot ball / arm-apex tracking; the grade banner is itself delayed so it
is not a clean cross-clip reference.

**Artifacts** (`scratch/meterdelay_probe/`): `meter_side_by_side.png` (montage), `fill_compare.png`
(onset-aligned curves), `side_by_side.mp4` (both clips aligned at meter start), plus the tracker/analysis
scripts (`track2.py`, `final.py`, `montage.py`, `video_sxs.py`).

**Bearing on the plan:** confirms §2 "no extra time" for the *predictive* path (window not wider, just
later) **and** motivates §8 — the human exploits a decoupled-but-same-rate meter **reactively**, not via a
slower fill. The measurement is what turns Option 6 from a guess into the recommended build.

---

## 10. Execution roadmap (the unified plan)

Ordered, risk-gated. Everything default-OFF / byte-identical until a flag is armed, per the 17-day rule.

| # | Step | Type | Gate to proceed |
|---|---|---|---|
| 1 | **Land Option 5** — narrow `CourtIpDetector` to 30000–30020; verify deployed `VeniceNetSvc.exe` cap; fix stale docstring | correctness, low risk | port test: court flow on 30021–30099 now yields `intercepted>0` |
| 2 | **Measure true output latency** on the rig (press→meter-move probe) | measurement | a real number near ~63 ms |
| 3 | **Prototype the reactive rung** (§8 Option 6) in `processAutonomousLiveMeterHolding`, behind a flag + `applied_delay>0`, default-OFF | code, medium risk | all suites green; disarmed build byte-identical |
| 4 | **Experiment A** — reactive, D=200, Left/Right Fade, threshold = green_start − 63 ms×rate, 60 shots | rig | **PASS:** green ≥ delay-off phase AND aborts ≤5 % |
| 5 | **Threshold sweep** ±30 ms; then **D=400/600** repeat | rig | green rate holds flat as D climbs ⇒ delay-invariance proven |
| 6 | **Ship decision** | — | PASS ⇒ ship reactive-delay experimental; FAIL ⇒ §5.1 ship-disabled + honest note |

**Fallbacks if the reactive path fails at step 4:** (a) §5.1 ship-disabled (zero risk, recommended EA
default); (b) Option 1 narrow-delay hedge if small-D legibility measures as a real detection win. Do **not**
ship the predictive path with delay enabled at D≥~170 — that is a guaranteed all-abort session.

---

### Appendix — key `file:line` references
- Abort predicate + emit: `native_orion/src/AutomationEngine.cpp:7035`, `:7049-7061`
- Ceiling / margin: `:13161` (`maxSchedulableTipLeadMs`), `:69` (`kTipLeadScheduleMarginMs=30`), `:13142-13155` (`effectiveTipPhaseConstantMs`)
- Effective lead + offset gate: `:10266-10277` (`measuredLeadForActuationMs`), `:10227` (`appliedMeterDelayLeadOffsetMs`, 0 unless delay running)
- Starvation withdrawal + phase exemption: `:13164` (`visibleEvidenceLeadStarvedByMeterDelay`), `:11070-11078`
- Press-anchored formula + adopt gate: `:11180-11181`, `:11194` (`if(!decision.valid)`), press stamp `:3579-3580`
- Press-anchored post-mortem ("HAS NEVER ARMED"): `:11121-11144`
- Delay is time-shift not rate change: `nexus_svc.py:362-363,370-379`; `AutomationEngine.cpp:6682-6683`
- Legibility-only benefit: `native_orion/venicenet_service/MeterDelayController.h:9-11`
- Port ranges: `CourtIpDetector.h:36-37` (30000–30099) vs `MeterDelayIntercept.cpp:510-513` (30000–30020)
- Delay cap: `MeterDelayIntercept.cpp:140` (`kHardMaxMs=600`); stale "300" `nexus_svc.py:1030,:394`
- Clamps: `actuation_lead_ms` `{0}∪[150,800]` (`AppConfig.h:537,563-564`); `meter_delay_ms` def 250 `[100,600]` (`AppConfig.h:393,403-404`); `meter_delay_lead_offset_ms` def 0 `[-200,+400]` (`AppConfig.h:599-601`)
- Live state: `settings.json:4,73,74,75,126,128-148`; `learning.json:10-11`
- Reactive workaround (§8): predictive fire `AutomationEngine.cpp:6824`; lead=user-290 `:10294`; live path `processAutonomousLiveMeterHolding :6051`; bypassed legacy reactive rung `:8619` (`reactive_target`, zero-lead, `:8611`); green tracker `greenTracker_.startPct()` ← `release_threshold=93`; output-path latency 45+55≈63 ms `:1371`/`AutomationEngine.h:191`/`:7515`; capture self-cancel `RemotePlaySession.cpp:4221`, `AutomationEngine.cpp:2481`
