# Venice — external engineering task: detection stability + latency reduction

## 1. Context

Venice is an NBA-2K shot-timing assistant. Repo: `C:\Users\aaron\Desktop\NexusVision`
(Windows, C++/Qt engine under `native_orion/src/` plus a Python sidecar).

It captures the PS5 Remote Play video feed, detects the on-screen shot meter, predicts when
the meter reaches the "tip" of the jumpshot animation, and presses the shot button at that
instant. Timing accuracy is the entire product.

It ships **Sunday 2026-08-09** to paying customers.

You have two tracks below. Track 1 (detection stability) is the higher priority; do it first
unless you find a reason not to, and say what that reason is.

---

## 2. Rules of engagement — read before starting

This codebase has repeatedly been damaged by confident, plausible-sounding conclusions that
turned out to be artifacts. Recent, real examples from this project:

- A "60 Hz quantization explains 34% of timing variance" regression was **circular algebra** —
  the regressor contained the regressand by construction. Every performance ceiling derived
  from it had to be retracted.
- Two separate root-cause theories for a stepback bug were argued convincingly and both were
  **wrong**. The logs disproved them. The actual cause was found by reading working
  third-party implementations, not by reasoning.
- A "latency" instrument in this codebase turned out to be derived from the very release it
  claimed to measure — circular, and it had produced zero valid observations.

So:

- **Every claim must be backed by a measurement you actually ran.** State the method.
- **Check for circularity before reporting any correlation or regression.** Write out
  explicitly whether the two quantities share a term by construction. If they do, it is void.
- **Do not trust a fix because tests pass.** Tests here have repeatedly been greener than
  reality because fixtures were cleaner than live frames.
- **Label anything you could not prove as unproven.** A clearly-flagged "I could not
  demonstrate this" is worth far more here than a confident wrong answer.
- Distinguish **soft** negatives (could be retested differently) from **hard** ones
  (fundamentally impossible).

### Recurring trap in this codebase

**Gates unreachable on real data.** A threshold gets added, it cannot be met on live frames,
the feature is silently off forever, and the tests stay green because the fixture is cleaner
than reality. If you introduce any threshold, prove it is actually met on recorded data and
report the hit rate.

---

## 3. Already handled — do NOT work on these

Another engineer is actively fixing these. Duplicating them wastes your effort:

- **Reconnect timing deadlock (root-caused already).** On a Remote Play reconnect, the
  sidecar emits `decoder_or_capture_source_generation_transition`, which resets latency
  authority and zeroes the route attestation generation. The engine then refuses to fire, and
  re-earning authority requires bot releases that the gate itself blocks — a deadlock that
  needs an app restart. Evidence: 0 armed tokens and 0 shot outcomes after reconnect.
- **Tempo/fade stepback.** Root cause identified from working Cronus Zen scripts: a fade
  requires the Pro Stick pushed **up** (`RY = -100`), while a normal shot uses **down**
  (`RY = +100`). Emitting down during turbo+direction is a stepback by definition.
- UI layout, packaging, installer, code signing.

---

## 4. Already measured — do NOT re-derive

Start from these. They are measured, not assumed.

- **The capture card dominates latency.** Of a roughly **~300 ms** end-to-end loop, the PC
  side is only about **25 ms**; the Elgato capture card contributes roughly **160 ms**.
  Prediction cleverness cannot touch that — only a shorter path can.
- **A lower-latency path already exists and is unhardened:** a `video_source: decoder` route
  that takes frames from the Remote Play decoder directly, bypassing the capture card. This
  is the single biggest known lever and is already partially built.
- **The command path is a named PIPE, not ViGEm.** All controller submits bypass HID. Any
  analysis resting on a ViGEm/HID timing prior (e.g. a ±33 ms USB polling jitter figure) is
  irrelevant to the shipped route. Verify which path a measurement refers to before using it.
- **Actuation budget is ≤14.8 ms**, with an irreducible ~6 ms animation floor. Do not
  re-measure this.
- **Corroboration, not scan narrowness, is the false-positive guard** in meter detection. Do
  not "fix" instability by narrowing the search window — that historically broke acquisition.

---

## 5. Evidence and tools available to you

- **Offline replay/framedump harness** under `tools/` — runs the detector against real
  captured frames with **no live session needed**. This is your primary instrument: it lets
  you measure detection stability before/after a change. Find it and use it.
- **Recorded detector frames:** `detframes_20260804_074153.csv`.
  **Do NOT use session `session_20260804_032333`** — it interleaves two separate captures
  spliced together, so any statistic computed over it measures an artifact.
- **Live logs:** `logs/orion_native.log` (large — grep it, do not read it whole).
  Also `logs/sidecar_fault.log`, `logs/sidecar_crash.log`.
- Source: `native_orion/src/` (C++ engine, capture, remote play), plus the Python sidecar
  (`remote_play_orchestrator.py` and related).

---

## 6. TRACK 1 (priority) — Make meter detection stable

### The problem

The on-screen meter detection box **flickers, shrinks, and bloats in size** during play. The
owner's direct observation is the key datum:

> "the bot works best when the meter detection isnt flickering or shrinking or bloating up in size"

So instability is not cosmetic — it appears to degrade timing accuracy. Find out **why the box
is unstable, and fix it**, proven on real recorded frames.

### Questions to answer with measurements

1. **Quantify the instability first.** Over recorded frames, what is the frame-to-frame
   variance in the lock box's x, y, width, height? How often is the lock dropped and
   re-acquired? Produce numbers before proposing anything.
2. **Classify the failure modes** and give the proportion of each:
   (a) losing the lock and re-acquiring, (b) holding the lock but resizing, (c) jumping to a
   different screen region. These have different fixes.
3. **Locate the mechanism** — is it the locator, the confirmation/corroboration gate, or the
   box-geometry estimator? Give the specific `file:line`.
4. **Does instability actually correlate with timing error?** The owner believes it does.
   Test it — join detection stability against shot outcomes and report the correlation,
   *including if it is weak or absent*. A negative result is genuinely valuable here: it
   would mean effort should go elsewhere. Watch for circularity when constructing this join.
5. **Propose and validate a fix**, with before/after numbers from the replay harness.

---

## 7. TRACK 2 — Cut end-to-end latency

Every millisecond of latency must be *predicted* rather than *observed*, so latency converts
directly into timing error. Real latency reduction beats any additional prediction cleverness.

### Tasks, in priority order

1. **Harden the `video_source: decoder` path.** Find it, determine what is missing or unsafe,
   and assess: how much latency does it actually remove, and what breaks? This targets the
   ~160 ms that dominates the budget, so it is by far the highest-value item. **Measure the
   delta — do not assume it.**
2. **Measure the current pipeline stage-by-stage** with a non-circular instrument:
   frame arrival → detection → decision → command submit → observable effect. Report where
   the PC-side ~25 ms actually goes.
3. **Find remaining avoidable buffering:** queue depths, redundant copies, sleeps, format
   conversions, throttles or cadence limits in the frame path.
4. Only then propose further reductions, each with a measured before/after.

---

## 8. Deliverable

For each track:

1. A **quantified** description of the problem (numbers, not adjectives), with the method used.
2. The **mechanism**, cited to specific `file:line`.
3. A **concrete patch**.
4. **Before/after measurements** proving it works on real data (replay harness for Track 1;
   stated instrument for Track 2).
5. An explicit list of **what you could not prove**, and which negatives are soft vs hard.

If the two tracks conflict — e.g. the decoder path changes frame timing in a way that affects
detection stability — say so explicitly rather than optimising one at the other's expense.
