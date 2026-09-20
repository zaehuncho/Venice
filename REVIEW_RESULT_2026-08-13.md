# Venice — second-opinion review result, 2026-08-13

**Answer to** `REVIEW_ASK_2026-08-13.md`. Read-only: nothing edited, built, run, or committed.
`settings.json` / `learning.json` / `run_orion.local.ps1` untouched; no service touched; app not stopped.
**Ship: 2026-08-28 (15 days). Installer freeze: the 24th (your only no-slack item).**

**Method:** direct read-only analysis of `logs/orion_native.log` (n=144 graded landings in this snapshot; sigma
sampled from 811 reservation lines) + five parallel read-only code scouts over the latency estimator, the capture→detect
path, the Go-To settle/detection path, the fake-lock chain, and the Square #88 release-window question. Every claim
carries `file:line`; unproven items marked SPECULATION or UNVERIFIED.

You asked for precision over politeness and said the last two things I caught were cases where you were confidently
wrong. In that spirit the first section is where **I** was wrong.

---

## 0. Two corrections to my OWN prior review (be equally hostile to me)

**0a. I retract "reactive fire" (my A1 from 2026-08-12). It is not physically available in capture-card mode.**
My reactive-fire idea borrowed the meter-delay "self-cancel" argument, which assumed capture+decode was ~15-25 ms. Your
own rig fact says the loop is 227-235 ms with **PC-side only 12.6 ms and the card+decoder dominating (~215 ms)** — and a
scout confirmed the software path is already minimal, so that ~215 ms is real hardware latency. The bot's meter view is
therefore ~215 ms **stale relative to the console**, and that latency is bot-only — it never cancels (WinDivert delay is
shared between view and grading; base capture latency is not). React to the green you *see* and you land ~215 ms late.
The engine already encodes this: the phase constant is `tipPhaseConstantMs = 393.0` built on
`tipPhaseSeedPhysicalMs = 319.0` (`AutomationEngine.h:1415, :1468`) — the measured physical press→visible latency. You
**cannot** fire below it. Reactive firing is off the table; the ~290 ms predictive lead is transport, not a modeling
choice. Your own table already reopened "reactive timing" — the honest answer is: reopen it only if you can get a
low-latency view (HDMI-passthrough tap into the PC, not the capture frame), otherwise it stays dead.

**0b. My "optimal aim offset is +0.0 / aim-higher is dead" (which you adopted) is itself a saturation artifact — aim-higher
is UNTESTED, not dead.** You correctly flagged that `peak_fill` saturates at 100 = `green_end`, so over-timing is
invisible. The deeper consequence you didn't state: the "+0.0 optimal offset" was **measured by shifting `peak_fill` and
counting `peak+k ≤ green_end(100)`** — i.e. it treated a shot that would peak at 101 as an **over-miss**, when in-game it
**caps at 100 = green (free)**. So the calc penalized the exact upside that is physically free. The over-side is a free
ceiling; shifting the aim up rescues under-misses at no measured over-cost. **"Aim higher" is a cheap, must-run rig A/B,
not a dead idea** (details in §6, with the direction tension called out honestly).

---

## Verdict up front

| Q | Verdict |
|---|---|
| **Q1 late shots** | **Wrong culprit.** The lead is fine and the estimator is a red herring — it can *never* change the fire lead on this rig (user Shot Lead replaces it, `AutomationEngine.cpp:10391`), and its 95% rejection is a correct fail-closed guard. Real cause: `predictor_sigma_ms` (15.1) **exceeds the ~11 ms window**, dominated by the phase member's **13 ms anchor-dating residual**, fired as a **sigma-free point estimate aimed at the late edge**. The 2 aborts were *within 1 sigma* — ordinary draws, not the tail you hypothesized. |
| **Q2 Square #88** | **Confirmed safe.** No release-window state corruption beyond the telemetry bug you already fixed. Two LOW residuals (an injected Square can leak to the console during your *own* TempoStick/GoToStick shot; one cosmetic log line). |
| **Q3 Go-To** | **You have it the right way round — proven.** Grading is upstream of detection: **46/110 Go-To fires (42%) are silently ungraded** with the meter fully seen (samples_n≈181), fire=landing parity 110=110, courtwide detection alive (216 seatings). But your fix is **mis-advertised as a runtime flag** — it's a compile-time constant — and flipping it re-opens a real false-EXCELLENT bug. |
| **Q4 fake-lock** | **Your mental model is stale.** The `locator→loc_mem→park→T5` chain is the **retired** detector; the shipped reader is `SimpleMeterReader`, and `ORION_READER_FAKELOCK_BREAK` is documented **unreachable in production**. Concrete near-zero-cost instrumentation spec below. |
| **Q5 completeness** | The biggest forgotten items for *this* product: **AV/SmartScreen false-positive** (ship-killer), **fresh-install off-aim seed** (first-impression killer), **`actuation_lead_ms=0` on mid-write kill** (silent brick), and **per-rig latency variance** (every number here is one rig). |

**If I could change only one thing before Aug 28:** run the aim-higher rig A/B (§6.1) — it is the cheapest test with the
largest plausible effect on the 26% miss rate, and it directly falsifies or confirms 0b. Second: fix Go-To grading (§Q3).

---

## Q1 — Rare late shots: the frozen lead is NOT the cause

**Attack on your hypothesis ("fixed lead + ~15 ms predictor noise, tail crosses the deadline every few dozen shots"):
half-right, wrong mechanism, wrong culprit.**

**The estimator cannot change the lead — chasing it is wasted budget.** `measuredLeadForActuationMs()` returns
`config_.userActuationLeadMs` whenever *any* authority exists, factory OR validated (`AutomationEngine.cpp:10391`); the
measured latency actuates **only if you leave the Shot Lead unset/out-of-band** (`:10361-10372`, gated behind the
default-OFF `ORION_USER_LEAD_AUTHORITY`). The miss log says it outright: *"lead_kind above names only the AUTHORITY, which
the in-band Shot Lead replaces"* (`:7078-7079`). So `499/499 lead_kind=factory` is not a bug and not the problem — the
lead you tuned **is** the actuation lead regardless of what the estimator does.

**The 95% rejection is a correct fail-closed guard, not a bug.** `near_cap` rejects any freeze with `f_stop > 95.0`
(`latency_estimator.py:1982-1987`, threshold `:451/:488`). The green window sits at fill **98-100**
(`AutomationEngine.cpp:6906`), *above* 95 — so **every clean green shot is un-learnable by construction.** The estimator's
own docstring concedes it: *"the bot aims at the TIP — mean peak 98.4 — so only ~8% of shots can ever clear that gate,
and those are the mistimed ones. The better the bot gets, the less its own latency estimator is allowed to learn"*
(`latency_estimator.py:1044-1049`). Relaxing `near_cap` would feed the posterior a biased diet of short shots. Leave it.
Promotion needs ≥6 validation-capable labels at SD ≤ 3.3 ms (`:94-95, :2683-2708`) — structurally near-unreachable on a
well-timed rig, and **moot** even if reached (the lead is overridden anyway).

**The real cause — predictor variance exceeds the window, and the fire ignores it.**
`predictor_sigma_ms = √(sigmaMs² + leadSigmaMs² + tickSigmaMs²)` (`AutomationEngine.cpp:11144-11147`). For the
`source=phase` aborts: `√(13.0² + 6.0² + 4.8²) = 15.101` — **your exact logged number**, and **74% of the variance is the
phase member's fixed 13 ms dating residual** (`tipPhaseSigmaMs`, `AutomationEngine.h:1473`). Two consequences:
- **Sigma (15 ms) already exceeds the ~2 pp ≈ 11 ms green window.** I confirmed this holds on 811/811 reservation lines
  (typical `predictor_sigma_ms` p50 ≈ 31 ms — your 15.1 is the *floor*, the phase-only best case). **No lead value,
  however perfect, keeps a point-estimate predictor with sigma > window reliably inside the window.**
- **The fire is sigma-free and aimed at the late edge.** `fireAtMs = predictedTipMs − effectiveLeadMs − greenCenterOffsetMs`
  (`:6911`); sigma appears nowhere in it, and `autonomousGreenCenterFrac = 0.0` (`AutomationEngine.h:963`) aims at fill
  **100** — zero late-side tolerance. The only sigma gate is a loose `combinedSigma ≤ 75.0` validity cap (`:11154`), which
  15.1 never trips.

**Why the lates are *rare*, not routine (and this refutes your "noise tail" framing):** if 15-31 ms sigma were the
realized landing error, misses would be ubiquitous, not "every few dozen shots." Realized landing sd is ~1.5 pp ≈ **8 ms**
(your own `peak_sd` table), ~2-4× tighter than nominal sigma — because the meter self-corrects (the −0.9 fire→travel
coupling) in a way the sigma model doesn't capture, and the high side caps free in green. So **predictor_sigma is ~4×
pessimistic about realized error, and the rare lates are OUTLIER events** (RTT spike, dropped frame, scheduler stall),
**not** the distribution tail. Both your aborts (lateness 9.8 / 3.9 ms) were *inside* 1 sigma — body-of-distribution
draws. Clipping "the tail" would not have caught them.

**What actually reduces late shots, ranked:**
1. **The dominant sigma term is the phase member's 13 ms dating residual** — the *only* lever that materially shrinks
   sigma is a tighter anchor-dating / phase model. Promoting the estimator moves combined sigma 15.10 → 14.25 (nothing).
   Medium-term modeling work, but it is the true root.
2. **Aim off the late edge** (`autonomousGreenCenterOffsetMs`, `:10414`, exists and only ever fires earlier, bounded) —
   *for the late-side aborts specifically*, firing a hair earlier buys schedule margin. **But see §6.2: this pulls
   OPPOSITE to the aim-higher lever that fixes the 26% under-miss rate. Do not enable it blind.**
3. **Kill the outlier jitter** (MMCSS on the Chiaki feedback-sender thread, still unbuilt — `feedbacksender.c:244`,
   hunks in `.audit_raw/BUILD_chiaki_mmcss.md`). Since the lates are outlier events, this is the targeted fix, not
   lead-tuning.
4. **Do NOT invest in the latency estimator.** It cannot change the lead (`:10391`). Lowest payoff on the page.

---

## Q2 — Square passthrough (#88): confirmed safe

No control/state corruption during the Releasing window beyond the telemetry bug you already fixed. Proof: the release
handlers `processReleasing`/`processCooldown` read neither `output` nor `physical` (`AutomationEngine.cpp:8835, :8923`);
output-X never re-enters the engine — the app's own virtual pad is excluded from device selection
(`ControllerDeviceSelector.cpp:53`) and there is no `lastOutput_`/`prevOutput_` square read (only `lastPhysical_=physical`
`:3124`); the physical-square latches updated during Releasing read `physical.square()`, which R3
(`XINPUT_GAMEPAD_RIGHT_THUMB`) never sets (`:3084, :3135-3148`); and the engine reads R3 nowhere but the bit-map
(`:3084-3085`).

**Two LOW residuals (neither is state corruption):**
- **L1 — an injected Square can leak to the console during your OWN stick shot.** `applyShotReleaseEdge` clears X for
  `ButtonShot`/`TempoSquare` but **not** for `TempoStick`/`GoToStick` (`ShotReleasePolicy.h:69-91`). Hold R3 during a
  TempoStick/GoToStick shot with Tempo on → the injected Square is delivered to the game at the release instant. Extra
  button to the game, not engine corruption. **One-line fix if you want it airtight: gate `applySquarePassthrough` on
  `shot_.state == Idle` (`AutomationEngine.cpp:3100`).** This also closes the "gate ignores armed state" note from my last
  review.
- **L2 — cosmetic:** the "Shot state:" transition log reads `output.square()` without the passthrough subtraction
  (`OrionAppController.cpp:9777`), so a steal during a bot shot's state transition logs `out_sq=1` there. The
  load-bearing `out_cleared_all` assertion uses the corrected value (`:9807`), so this is a secondary-log mismatch only.

**In-game verification you asked about:** the log genuinely cannot tell "R3 produced a steal" from "R3 did nothing"
because the steal outcome lives in the game, not the meter. The cheapest proof is a one-session manual test: Tempo on,
hold R3 on defense, confirm the steal animation fires. No code needed.

---

## Q3 — Go-To: grading IS upstream of detection (proven), but your fix is mis-advertised

**3a — settle rule: your theory CONFIRMED.** `no_settled_run` is decided at `AutomationEngine.cpp:14200-14228`;
`meterSettledFrames()` (`:12883-12974`) walks maximal **static-bbox** runs — a step breaks the run when
`hypot(dx,dy) > maxMove` with `maxMove = 12.0 px` (`:12930`, `AutomationEngine.h:710`). A marker riding a running player
moves >12 px/frame every frame → length-1 runs → empty `good` → `no_settled_run`. The function header says so verbatim
(`:12906-12908`). **Caveat (hostile):** ~4 of 46 Go-To rejections have `meter_jump` *inside* the graded-Standstill band
(0.0036-0.0089) — those bboxes barely moved and still failed to settle, so the **fill-tolerance tail** rejects some too.
Bbox motion is the majority cause, not the sole one. Also note `meter_jump` is not literally the per-step displacement —
it's the **max** single-frame *normalized* displacement (`:14165-14182`), while the gate uses **raw-pixel per-step**; to
compare them you must multiply by frame width.

**3b — the fix (`e1f1e2b` / `ORION_SETTLE_SMOOTH_MOTION`) is not what the commit claims.**
- **It is NOT a runtime flag.** `meterSettleAllowSmoothMotion` is a compile-time `= false` (`AutomationEngine.h:738`) —
  no `getenv`, no `settings.json` key, no `applyConfig` line anywhere in `native_orion/src`. The commit's *"Live-A/B it
  (ORION_SETTLE_SMOOTH_MOTION=1)"* is **fiction**; enabling it = **edit source + rebuild native**. (This is the same
  compile-time-only pattern as the `meterSettle*` family I flagged on 2026-08-11.) **First action: make it a real
  settings/env toggle so it can actually be A/B'd without a rebuild before the installer freeze.**
- **Flipping it re-opens a real, previously-shipped bug.** The two "guards" it conflicts with are test cases
  (`AutomationEngineTests.cpp`): `postReleaseMovingMeterIsNotGraded` (`:6873`) and `settleGradingRequiresBboxMotionSignal`
  (`:6905`) — the latter guards a **real bug that shipped** (RemotePlaySession built DetectionResult with no x/y/w/h → all
  bbox motion read as 0 → sliding fades looked settled → **false EXCELLENT**, `:6907-6915`). Both slide 40 px/frame;
  `maxTravel = 48 px > 40`, constant slide → accel 0 ≤ 6, frozen fill → net-drift 0 ≤ 1.5 → **the flag grades exactly what
  the guards assert must be skipped.** The conflict is documented verbatim as "DELIBERATE AND UNRESOLVED"
  (`:6938-6944`). With the bbox gate removed, fill-freeze is the sole discriminator, and the fill tail is a weak
  ~0.8 pp/frame rate gate (your own fix-2 admits this) — a marker still *sliding* with frozen fill for 6 frames now
  grades. **So: don't just flip it. Re-baseline those two guard tests deliberately using net-drift(1.5 pp) +
  motionMinFrames(6) as the discriminator, and validate on framedump Go-To captures.**
- **SPECULATION (stale build logs):** `settleGradingRequiresBboxMotionSignal` shows intermittent FAIL even with the flag
  OFF in `native_orion/build/native-tests-race*.txt` — a possible race in the settle/grading path. Low confidence; worth
  one look.

**3c — is (a) upstream of (b)? YES, proven, not asserted.** In `logs/orion_native.log`: **110 Go-To fires = 110 Go-To
landings** (no hidden "fired but never landed" population), and **46/110 (42%) are `no_settled_run` — ALL with
samples_n 174-182**, i.e. the meter was detected the entire ~3 s window. So for every Go-To that reached the grader,
detection worked and grading failed, 100% of the time. Go-To is 79% of all settle rejections (46 of 58). The courtwide
detection ladder is **alive**, not dead: your "19 seatings" is only the strict-red slice of the rarest *mid-shot
supersession* tier — the **cold courtwide acquire caught the beyond-half-court meter 216 times**
(`simple_meter_reader.py:5664-5771`; `READER ACQUIRE tier=courtwide:*`). The only detection failure the logs *cannot* see
is Go-To shots **never taken** (no `Release timing` line) — and there is **zero positive evidence** of that population.
**Do not spend Aug-28 budget chasing detection-past-half-court; fix grading.** Cost of the 42% gap: each `no_settled_run`
calls `emitPressTipObservation(graded=false)` (`:14226`) — logged for census, **never fed to the learner** — so the Go-To
press-anchored learner sees only ~64/110 shots, and not a random 64 (it's the *stationary* Go-Tos), which biases it.

---

## Q4 — Rare fake/stale meter locks: your chain is retired; here's real instrumentation

**Your mental model is stale.** The named `locator→loc_mem→park→T5` chain is the **retired legacy detector**
(`remote_play_orchestrator.py:1465-1484` — it raises `RuntimeError` unless dev-selected); its corroboration guard
(`meter_detector.py:96-141`) **does not run in production.** The shipped reader is `SimpleMeterReader`, and its
`ORION_READER_FAKELOCK_BREAK` is documented **unreachable in production** (`simple_meter_reader.py:1329-1339`): the
`_shot_armed_hw` grace zeroes `_static_fill_n` every frame, so `static_fake_lock` *"appears ZERO times in every live log"*
while the same logs carried 2.1-8.8 s dead/false locks it was built to kill. **That is itself a latent correctness gap
worth noting** — a guard that never fires.

**How a fake/stale lock happens (shipped path):** a window mullion at x≈0.11·W reads as a textbook Arrow2 column under
the relaxed `_MICRO_RED` band; once it latches `_lock_red`/`_lock_green`/`_lock_w_ref` (`:6025-6032`), `_relocate`
re-finds it "every frame forever" (`:1354-1360`) — shape, position and pixel-variance can't separate it from a real
meter. The shipped corroboration guard (`structure = green≠None OR (courtwide_lock AND monotonic_rise)`, `:3349-3350`)
blocks gameplay **ownership** but not the **lock/render** (`:3359-3364`). The coast/HOLD path dead-reckons the last box
(`:6130-6135`), so a frozen-fill decor lock coasts indefinitely.

**Instrumentation spec (respects 60 fps — per-TRANSITION, not per-frame; ≤~3 lines/shot).** The gap: the lock **seat**
logs (`READER ACQUIRE`, `:6013-6021`) and the **steal** logs (`METER STEAL SEARCH`, `:5692`), but the lock **DROP**
(`:6088-6129`) logs nothing to the forensic sink — yet drop is exactly when lock age and total displacement are known.
Add one **`READER LOCK DROP`** line, emitted at `:6090` immediately *before* identity is zeroed (`:6098/:6106`), with:

| field | meaning | cost |
|---|---|---|
| `tier`/`source`, `reason` | seat tier + drop reason (`lock_drop`/`roi_not_found`/`steal_supersede`) | already have (`:6001/:6017/:6091`) |
| `age_frames` | frames since seat | +1 int/frame — the only per-frame add |
| `disp_px` | `hypot(cx−seat_cx, cy−seat_cy)` | 2 floats stored at seat; **one hypot at drop** |
| `red0`→`red1`, `w0`→`w1` | red-fraction & box-width at seat vs now | already computed by the band/relock gates (`:5645-5648`) |
| `ever_rose`, `coast_n`, `static_fill_n`, `held_run`, `peak_fill` | live lock state | all already maintained |

The tells fall straight out: a **decor fake-lock** shows `ever_rose=0`, large `age_frames`, `red1≈red0`, `w1≈w0`; a
**stale echo** shows high `coast_n`/`static_fill_n` with a byte-frozen `peak_fill`. It reuses the already rate-limited
`_acq_logger` (`:5682-5690`), so it costs one int increment on the hot path and nothing else. This also lets you finally
**quantify** the unreachable `FAKELOCK_BREAK`.

---

## Q5 — Completeness before Aug 28 (what a 15-days-from-EA team forgets)

Ranked by "kills the launch if missed," specific to THIS product:

1. **AV / SmartScreen / Defender false-positive — the #1 forgotten ship-killer for this software class.** You inject
   controller input through a named pipe into a **patched Chiaki** (`OrionStream`) and read HDMI capture — a textbook
   input-automation signature. A fresh-downloaded ~190 MB installer that isn't **Authenticode-signed** (Ed25519 manifest
   signing is internal integrity, NOT the OS trust chain) triggers SmartScreen "unknown publisher," and Defender may quar-
   antine `OrionStream.exe`. **Verify before the 24th:** is the installer + every shipped `.exe` Authenticode-signed with a
   cert Windows trusts, and does a clean-VM download-and-run pass without a SmartScreen wall or a Defender detection?
   This is the single most likely thing to brick a paying customer's first 5 minutes.
2. **Fresh-install off-aim seed — the first-impression killer.** `learning.json.learned_phase_physical_ms=278` vs
   `tipPhaseSeedPhysicalMs=319` — a fresh customer starts ~41 ms off this rig's tuned aim (flagged in
   `BOT_MAXOUT_PLAN §3.1`). Their first session is bad greens → refund/churn before the learner converges. **Re-seed the
   shipped constant from pooled `PHASE SAMPLE` medians before GA.**
3. **`actuation_lead_ms=0` on mid-write kill — silent brick.** Per your own memory, a force-kill mid-write zeroes
   `actuation_lead_ms`, and factory-prior firing then looks like a regression the customer can't diagnose. A customer who
   alt-F4s or crashes hits this. **Atomic settings write (temp + rename) + validate-on-load (reject a 0/out-of-band lead
   and fall back to the last good value).**
4. **Per-rig latency variance — every number in this review is ONE rig.** The 227-235 ms loop, the 319 ms phase seed,
   the 35 ms HD60X device estimate (`capture_card_backend.py:131`) are yours. A customer with a different capture card,
   PS5 network, or monitor is off from frame one, and the estimator can't rescue them (Q1: it never gains authority /
   is overridden). **Decide explicitly: does EA ship a guided first-run Shot-Lead calibration (from the in-game TIMING
   banner), or does it ship your constants and hope?** Given Q1/Q3 show the **user Shot Lead IS the actuation lead**, a
   foolproof calibration flow is the highest-value first-run feature and the easiest to forget to make foolproof.
5. **Log growth + crash telemetry.** `orion_native.log` is 11 MB and grows per session — confirm rotation/retention so a
   multi-week customer doesn't fill a disk. And after the recent `0xC0000374` heap-corruption fix, add a minidump-on-crash
   + upload path so the *next* one is diagnosable remotely instead of a screenshot ask.
6. **Uninstall cleanliness.** The service (`VeniceNetSvc`), any WinDivert driver, and the virtual controller must uninstall
   cleanly — a leftover kernel driver or service is a support nightmare and an AV flag on reinstall.
7. **Freeze the shot path before the installer.** The installer is your only no-slack item (the 24th). Every timing change
   here (aim A/B, Go-To settle, MMCSS) must land and bake **before** that, or you're rebuilding the installer at the
   deadline. Recommend a hard shot-path freeze by the **22nd**.

---

## 6. Max-out + low-latency sweep (your added ask)

**The honest headline: the PC-side latency is already maxed; the floor is hardware, so "super low latency" is not where
the wins are — variance and grading are.** A scout traced the full capture→detect path: `CAP_PROP_BUFFERSIZE=1`
(`capture_card_backend.py:729`), a depth-2 latest-wins ring with event-driven latest-pull (`chiaki_backend.py:488-506`),
ROI-crop-before-colorconv on the hot path (`simple_meter_reader.py:4754-4759`), raw YUY2 (no JPEG decode). The 12.6 ms is
dominated by the **inherent `cap.read()` frame-arrival wait (~8 ms — the frame does not exist any sooner)**. The ~215 ms
is HD60X + HDMI + PS5, untouchable in code.

### Ranked max-out levers

| # | Lever | Why | file:line | Effort / risk |
|---|---|---|---|---|
| 6.1 | **Aim-higher rig A/B** (reopens 0b) | Over-side caps free in green; the "+0.0 offset" was a saturation artifact. Largest plausible effect on the 26% miss rate, cheapest test. | `autonomousGreenCenterOffsetMs :10414`, `greenCenterFrac :963` | LOW — one rig session |
| 6.2 | **Fix Go-To grading** (Q3) | 42% of Go-To silently ungraded; learner starved & biased. | settle `:12883-12974`; wire the flag `H:738` | MED — rebuild + re-baseline 2 guard tests + framedump A/B |
| 6.3 | **Shrink the 13 ms phase dating residual** (Q1) | 74% of predictor sigma; the only lever that materially tightens the window-vs-sigma gap. | `tipPhaseSigmaMs H:1473`, phase model | MED-HIGH — modeling |
| 6.4 | **Arm MMCSS on the Chiaki sender** | The rare lates are outlier jitter events; this is the targeted kill, not lead-tuning. | `feedbacksender.c:244`; `.audit_raw/BUILD_chiaki_mmcss.md` | LOW — hunks written |
| 6.5 | **Q4 lock-drop instrumentation** | Cheap, zero behavior change; converts an un-repro'd bug into data. | `simple_meter_reader.py:6090` | LOW |
| 6.6 | **Small PC-side ms**: drop the torn-frame verify pass (~1-3 ms), 720p capture (~1-2 ms; 1080p was null vs control) | Marginal but free. | verify `capture_card_backend.py:195/992`; dims `:374-375` | LOW-MED |
| 6.7 | **Honest device-latency accounting** (`ORION_CAPTURE_DEVICE_LATENCY_MS≈35`) | Doesn't cut latency — makes `frame_age_ms` truthful so the lead isn't lying to the predictor. | `capture_card_backend.py:131` | HIGH — needs live re-tune |

### The one tension you must decide (6.1 vs 6.2's late-side)
**Aim-higher (6.1) and "fire earlier for schedule margin" (Q1 remediation #2) pull OPPOSITE.** Firing later/higher cuts
the 26% under-miss rate (over caps free); firing earlier cuts the ~1% rare-late aborts. The under-miss (26%) dwarfs the
rare-late (1%), and the rare lates are outlier events better killed by MMCSS (6.4) than by aiming earlier. **So the
higher-value direction is aim-higher — but it is genuinely UNPROVEN, and the saturation means you cannot settle it from
logs. Run the 6.1 A/B (measure real make-rate at +2/+4 ms lead vs baseline, ~40 shots each). Do NOT enable the
earlier-aim `greenCenterOffset` blind — it likely trades your dominant problem for your rarest one.**

### Do NOT chase (dead levers, evidence in hand)
- Reactive fire (§0a — not viable in capture mode), the latency estimator (Q1 — can't change the lead), detection-past-
  half-court (Q3 — logs don't show it), sub-pixel edge / 1080p / MJPG / meter-delay-as-legibility (your table, confirmed),
  and raw capture-latency shaving beyond the ~1-4 ms in 6.6 (the floor is the HD60X).

---

## Bottom line
Your instincts on Q3 are right and now proven; your Q1 hypothesis names the wrong culprit (the lead and the estimator are
both red herrings — it's predictor sigma > window, dominated by a 13 ms dating residual, fired sigma-blind at the late
edge); Q2 is safe; Q4's chain is retired and its guard is unreachable. The two things I had wrong last time cut in your
favor: reactive fire is dead in capture mode, and "aim higher" is *untested*, not dead — and it's the cheapest high-upside
test you can run before the 24th. The latency floor is hardware; stop chasing it. Spend the 15 days on: the aim A/B, the
Go-To grading fix (wired as a real toggle, guards re-baselined), MMCSS for the outlier lates, and the four EA-ship items
in Q5 — with the installer frozen by the 22nd.

*Numbers reproduce from `logs/orion_native.log` read-only, grouped within shot-type (no cross-session `seq` join). Code
claims cite `file:line` to the main tree, not `.claude/worktrees/*`.*
