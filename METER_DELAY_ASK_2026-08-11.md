# Meter Delay — design ask

**Produce a PLAN, not a patch.** Do not modify code. The feature is fully wired and the blocker is
arithmetic, so a diff written before the arithmetic is solved will be wrong.

Read `BUG_AUDIT_BRIEF.md` §2 (hard safety rules) and §3 (verification traps) first — especially:
`logs/orion_native.log` is append-only across ~30 sessions with `seq`/`physical_epoch` restarting
at 1 in each, so **namespace every log join by session**; and anchor grep patterns (`abort` matches
the field `aborted=0`).

---

## What the feature is

`VeniceNetSvc.exe` (LocalSystem, WinDivert) intercepts PS5 Remote Play UDP and holds packets a
configurable number of ms. On screen the shot meter appears to fill later, which is meant to buy
the CV pipeline more time to read it. Slider range 100–600 ms; IPC on 127.0.0.1:47291.

## What is ALREADY TRUE — do not re-derive or contradict without new evidence

1. **The wiring is CORRECT and the toggle WORKS.** An audit specifically suspected a broken QML
   toggle and **refuted** it: the QML → `setMeterDelayEnabled` → `applyMeterDelayRuntimeConfig` →
   controller → DLL path is complete. What the owner saw was the honest "backend disarmed" banner.
   Do not go looking for a wiring bug.

2. **THE BLOCKER IS ARITHMETIC.** Base Shot Lead 280 ms + the offset the delay requires (~155 ms)
   = 435 ms, against a usable ceiling of 398 ms. Every shot aborts with `SHOT LEAD CONFLICT`.
   Measured headroom is **exactly +118 ms**. At offset 155 the owner got **10 aborts, 0 shots**.

3. **WHY it starves.** Every in-video tip source (`phase`, `sampler`,
   `registration+sampler_far`) is anchored on the DELAYED video, so under delay none of them can
   warn far enough ahead to schedule a release. Three legs, one moving floor.

4. **Delay does NOT slow the meter.** Measured from frames: fill rate is unchanged —
   **283 / 268 / 275 ms at D = 0 / 200 / 600**. What delay actually does is *detach* the meter from
   the animation: the bar completes AFTER the ball has left the hand. Observed shift ≈ **0.57 × D**.
   The premise "it slows the bar down so CV has more time" is FALSE on our measurements.
   Any plan built on that premise is wrong.

5. **Grading under delay needs a SHIFTED threshold.** `settled_fill`'s good band slides down with
   D: **95–97 at D=0, 81–83 at D=200**, and a LATE bounces to **50–53**. `peak_fill` is
   information-free under delay. Validated 9/9 against the in-game banner. A fixed threshold
   mislabels delayed shots as LATE, which has already fooled us once.

6. **The press-anchored predictor exists precisely for this.** It is the only tip source NOT
   anchored on delayed video: `tip_abs_visible = press_wall + press_to_tip[shot_type] + V +
   applied_delay`. Every term is knowable without reading the delayed bar. It is live
   (`press_anchored_predictor_enabled = true`) with real learned per-type constants
   (Standstill 648 ms n=100, Right Fade 859 n=100, Left Fade 831 n=86, Go-To 1981 n=34,
   No Dip 442 n=29). Latest measurement: **26 shots, 57.7% good vs phase's 78.3%** — it works but
   is currently the weaker source. **This is almost certainly the key to the whole feature.**

7. **Known service-side defects** (verified, unfixed): the running service caps delay at **300 ms**
   until restart even though the slider allows 600; and `CourtIpDetector` qualifies ports
   **30000-30099** while `MeterDelayIntercept`'s WinDivert filter matches only **30000-30020**, so
   a court flow on 30021-30099 makes meter-delay report `active=true` while intercepting **zero**
   packets — a silent no-op.

---

## The question to answer

**Can meter delay be made to produce more greens than delay-off, and if so how?**

That is genuinely open. It is entirely possible the answer is "no, and here is the proof" — that
outcome is valuable and should be stated plainly rather than talked around.

Address specifically:

1. **The 118 ms gap.** Is it closable? Options to evaluate: lower the base lead under delay; make
   the press-anchored member primary (not fallback) whenever `applied_delay > 0`; a smaller delay
   (100-150 ms) where the offset fits inside the headroom; something else.
2. **Does a delayed meter actually help CV at all?** Given fact 4 (rate unchanged, meter merely
   detached), what is the mechanism by which delay would improve detection? If there isn't one,
   say so — that kills the feature and saves 2 weeks.
3. **Per-shot-type behaviour.** The required offset may differ per type (press→tip spans 442 ms to
   1981 ms). Does one global delay/offset even make sense?
4. **What to ship on Aug 28** if it can't be made to work: remove the UI, leave it disabled with an
   honest explanation, or ship it flagged experimental.

## Deliverable

A plan with: the arithmetic worked through explicitly, a ranked list of options with expected gain
and risk, a specific **measurable** rig experiment for each (shot counts and pass/fail criteria the
owner can actually run), and a clear recommendation — including "abandon" if that's where the
numbers land.

Flag every assumption you could not verify from code or logs. If anything above contradicts what
you find, **say so loudly** — items 1-7 are our current understanding and parts of it have been
wrong before.
