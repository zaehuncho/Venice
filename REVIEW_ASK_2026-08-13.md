# Venice — second-opinion review, 2026-08-13

You reviewed this project on 2026-08-11 and were right twice: you caught that my `travel_pp`
decomposition was circular, and that I was scoring misses against a threshold I had invented.
Both corrections changed the work. Please be equally hostile here.

**Ship: 2026-08-28 (early access). 15 days.**

---

## What Venice is

An NBA 2K26 shot-timing bot. An Elgato HD60X carries the PS5's HDMI out into a Windows box; a
Python sidecar reads the shot meter from the capture frames; a C++ engine (Qt) decides when to
release and sends the release through a named pipe into a patched Chiaki (`OrionStream`) that owns
the console input session. The user shoots with Square; the bot owns the release.

Rig facts that constrain everything:
* Capture-card mode. Chiaki is **input-only**; the card is the video.
* Measured loop latency 227–235 ms. PC-side is 12.6 ms median; the card and decoder dominate.
* Green window is **~2 pp wide**, `green_start` 98.00 (sometimes 97.20), `green_end` **100.00**.
* Meter rise velocity at release ≈ **0.18 pp/ms**, so 1 pp ≈ 5.6 ms.

---

## The five questions

### Q1. Rare late shots — is the frozen lead the cause?

Last session, 499/499 release decisions logged `lead_kind=factory`. The latency estimator saw 101
observations and accepted **5**, all of which only ever reached status `warming`:

```
accepted=0 status=rejected_near_cap    79
accepted=0 status=rejected_deflate     16
accepted=1 status=warming               5
accepted=0 status=rejected_outlier      1
```

The two aborts that session were `disposition=rejected_missed source=phase`, with
`lateness_ms=9.809` and `lateness_ms=3.874` against `predictor_sigma_ms=15.101` and
`lead_ms=297/294`.

My hypothesis: the lead is effectively a fixed constant, the predictor carries ~15 ms of noise, and
the tail of that noise crosses the deadline every few dozen shots — which is exactly what the owner
describes as "rare late shots."

**Attack this.** Is a 95% rejection rate on the latency estimator a bug or a correct fail-closed
guard? Is `near_cap` rejecting observations it should keep? And is a fixed-lead + noisy-predictor
system really the explanation, or am I pattern-matching?

### Q2. Square passthrough (#88) — is the design sound?

With the tempo remap on, Square is owned by the engine and the user's own Square taps get
swallowed, so they cannot steal on defense. Shipped 2026-08-12: **R3 emits a real Square** and is
exempt from the remap, applied once on the FINAL output in `process()` because `processInternal()`
has six return points and Square is cleared at 21 sites.

L3 was deliberately rejected: L3 is turbo in 2K, so binding Square there fires a steal on every
sprint. R3 is otherwise unread by the engine.

**Never verified in game.** The log cannot distinguish "R3 produced a steal" from "R3 did nothing."
Is there a failure mode I have not considered — does a real Square during a live shot's Releasing
window corrupt engine state? (I fixed one instance of this: the steal was fabricating
`out_cleared_all=0` in the release-ownership trace, whose own comment calls that "a real engine
override bug.")

### Q3. Go-To beyond half court

Two separate problems, and I want to know if I have them the right way round.

**(a) Grading.** Last session: 13 Go-To shots, **10 rejected `no_settled_run`**, 3 graded. Every
rejection had `samples_n≈181` — the meter was seen the entire window — and filled normally
(discarded peaks: 100.00, 99.24, 98.48, 99.24, 100.00, 97.36, 97.62, 97.77, 97.59, 97.62). The tell
is `meter_jump`: **0.016–0.025** on discarded Go-Tos vs **0.006–0.009** on graded Standstill/Fades.
The settle rule only walks *static*-bbox runs, so a marker riding a running player yields length-1
runs and never grades. Fix written 2026-08-11 (`e1f1e2b`, `ORION_SETTLE_SMOOTH_MOTION`), **still
default OFF** — it conflicts with two existing settle guards and never got its A/B.

Consequence I care about: the Go-To learner only ever sees 23% of its shots, and not a random 23%.

**(b) Detection past half court.** Separately, some Go-To shots reportedly never read the meter at
all beyond half court. A "courtwide steal" ladder exists (`ORION_READER_STEAL_COURTWIDE`, default
ON) but has seated **19 times in the entire history of the log**.

Is (a) really upstream of (b), or am I about to fix grading and discover detection was the whole
problem?

### Q4. Rare meter-detection locks

Occasionally the reader appears to lock onto something that is not the meter, or holds a stale
lock. There is a `ORION_READER_FAKELOCK_BREAK` flag and a corroboration guard in the
locator→loc_mem→park→T5 fallback chain. I do not have a clean reproduction and I have not
quantified the rate — treat this as "owner-reported, uninstrumented."

What instrumentation would you add to catch it, given the constraint that the detector runs at
60 fps and anything expensive competes with the shot path?

### Q5. Completeness before Aug 28

What is missing that we have not listed? Specifically: what would you check that a team 15 days
from an early-access ship usually forgets?

Hard constraint: **installer repackage must land by the 24th** — it is the only item with no slack.

---

## Current numbers (last session, 89 graded landings)

```
shot           n  under%  peak_mu  peak_sd  gap_to_green_center  corr(fill_at_rel,peak)
Left Fade     34   26.5%   98.30     1.72         -0.54                  0.17
Right Fade    25   12.0%   98.64     0.97         -0.31                  0.46
Standstill    19   21.1%   98.51     1.25         +0.42                  0.21
No Dip         8   12.5%   97.81     1.66         -0.42                  0.63
Go-To (graded) 3   66.7%   98.18     1.29         -1.70                  0.23
Go-To (all 13)    53.8%   -- including the 10 discarded above
TOTAL         89   21.3%   -- but ~26% once the discarded Go-Tos are counted
```

**The 21.3% is flattered by discarding the worst shots.** Counting the 10 ungraded Go-Tos (7 of
which were under `green_start`), the true rate is 26/99 ≈ 26%, statistically identical to the 25.9%
baseline from the previous session. No improvement.

`corr(fill_at_rel, peak)` matters: Standstill ≈ 0 means the meter self-corrects (fire early, it
travels further, peak lands the same), so tightening the fire does nothing there. Fades and No Dip
at +0.46…+0.63 leak fire jitter straight into peak, so for those a tighter fire *does* tighten the
outcome.

### A measurement caveat that undermines part of the above

**`peak_fill` saturates.** 20.2% of landings pin at exactly `100.00`, and `green_end` IS `100.00`.
So "over-timing is 0.0%" — which I previously reported as a finding — is a ceiling artifact, not
headroom. One whole side of the error distribution is unmeasurable with this instrument. Any
argument that depends on "we never over-time" is void, including my own earlier claim that the
optimal constant aim offset is +0.0.

---

## Already refuted — please do not re-propose without new evidence

| Idea | Why it's dead |
|---|---|
| Aim higher / per-type aim offset | Optimal constant offset measured +0.0. **But see the saturation caveat — this one is now only half-dead.** |
| Sub-pixel meter edge | `ORION_READER_SUBPIX_EDGE` exists, off on purpose: removes 0.23 ms of noise, introduces 1.88 ms of bias |
| 1080p detection | Null against a same-day control, p=0.80 |
| MJPG capture format | No-op; the HD60X negotiates YUY2 regardless |
| Meter delay as a legibility aid | Green width 13.9 pp at D=200 vs 2.8 pp at D=0; bounce-back 100% vs 16.8%. It makes the read *worse* |
| Per-type tip phase constant | Go-To anchor→tip 354.0 ms vs 353.4 ms pooled; between-type spread is under within-type noise |
| `travel_pp` decomposition | Circular — `travel_pp := peak_fill − fill_at_rel` by definition (you caught this) |
| Reactive (non-predictive) timing | Was wrongly "refuted by latency"; that argument was mine and it was wrong. Open again, but the detached-green problem is the real obstacle |

## Built but never proven

* **A5 tick probe** — both consumers written and gated; **zero `tick_phase` entries have ever been
  accepted on this install**. Targets a ±8.3 ms send-vs-poll beat, ~1.5 pp of a 2 pp window.
* **A6 sub-frame peak** — the graded peak is a discrete frame max quantised to ~3.7 pp, *larger
  than the entire green window*. We grade with a ruler coarser than the target.
* **Press-anchored predictor** — armed once, hijacked good shots (fired at fill ~18 instead of ~38);
  now default OFF.

## Recent fixes you may want to check

* Capture-card heap corruption (`0xC0000374`): the in-process re-open built a new MSMF graph while
  an abandoned reader tore the old one down. Now serialised by a device-graph lock; frame reads are
  deliberately outside it.
* Connect promotion: a 2.5 s "protocol grace" was failing promotions closed. One sidecar process
  served four Connect presses — rejected twice, accepted twice — so the timer was measuring ack
  latency, not capability. Made non-fatal; the 20 s deadline remains the only fail-closed bound.
* Rhythm/flick: `actualFlickMs` was computed on every tempo release and emitted nowhere, and the
  flick-hold slider's bottom 34 ms was dead travel (`max(flickHold, releasePulseMs)`, releasePulse
  fixed at 50). Both fixed; flick timing is now on every graded landing.

---

## What I want back

Ranked, with reasoning. Say plainly which of Q1–Q5 you think I have wrong, and if you believe the
highest-value action is something not on this page, say that instead. Precision over politeness —
the last two things you caught were both cases where I was confidently wrong.
