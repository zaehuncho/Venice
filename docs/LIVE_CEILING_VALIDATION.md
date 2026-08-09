# Live ceiling-stack validation — flip runbook

**Goal:** prove the perfect-green ceiling stack against a real PS5, then flip it on for keeps.
Everything in the stack ships **default-OFF and byte-identical when off** (verified by the timing
red-team), so today's binary behaves exactly as it did before — this runbook is how we turn it on
*with evidence*, not on faith.

**green = make.** A perfect green means the ball went in; anything else missed. So the only metric
that matters at the end is the **green rate**, graded from the meter's own neon band — not "close to
the tip," not a feel. The absolute ceiling for this design is ~92–99% greens (the wall is the PS5's
unobservable 60 Hz input tick, not our software).

This doc is the **flip** procedure. For the raw detection-health batch (lock rate, phantom locks,
blind-fire %) run `LIVE_BATCH_RUNBOOK.md` first — a clean detection batch is the entry gate here.

---

## Preconditions (all must hold)
1. **Wired Ethernet** on both PC and PS5. Wi-Fi jitter defeats the whole exercise — the stack times
   the tip to single-digit ms; a stuttering link adds tens. If it's not wired, stop.
2. **No training / GPU job running.** `nvidia-smi` < 10%. Training pegs the box and muddies timing.
3. **Controller into the PC** (not paired to the PS5), HDCP OFF on the PS5, shot meter ON, default
   meter style/size.
4. **One launch only** per stage — no rapid relaunches (API rate limit + clean telemetry both want it).
5. Latest binary built from this branch (launcher + anti-tamper bundle, build green).

---

## Stage 0 — sanity (5 min, no shooting)
```powershell
.\run_orion.local.ps1 -Detdiag
```
- Connect Chiaki, confirm the live feed is crisp and the box glues to the meter through a couple of
  practice shots (do **not** grade these — this is just "is the pipe healthy").
- Grep the log for `DETDIAG` to prove per-frame telemetry is live (the build-marker line does NOT).
- If the box drifts, disappears through the shooting motion, or phantom-locks on décor → stop and
  fix detection first; the timing stack can't rescue a bad read.

---

## Stage 1 — SHADOW batch (the stack computes, but does NOT change your fire)
Shadow mode runs every estimator (fused lead, measured lead, plateau-aim, template-arrival, press-t0,
bandit, green-zone) and **logs what it *would* have done** next to what actually fired. Zero risk —
your shots fire exactly as they do today.

Launch with the shadow flags on, fire flags OFF:
```powershell
$env:ORION_FUSED_SHADOW      = "1"   # log fused-fire decision, don't act on it
$env:ORION_ANIM_ANCHOR_SHADOW= "1"   # log template-arrival prediction vs actual
$env:ORION_GREEN_ZONE_WINDOW = "1"   # derive the green band from the meter's own neon color
$env:ORION_GREEN_SELF_GRADE  = "1"   # self-grade each shot green/early/over from that band
# fire path stays classical: ORION_FUSED_FIRE and ORION_MEASURED_LEAD REMAIN UNSET
.\run_orion.local.ps1 -Detdiag -Framedump
```

**The batch — aim 30–40 shots, ~20 min. Cover the matrix (failure modes are type-specific):**
- 10–12 **Standstill** — the bread-and-butter timing check.
- 8–10 **Fades**, left *and* right — the far-left slid-out meter is the edge case, AND fades are where
  the timing red-team found the learn-loop bug (see "Known caveat" below).
- 5–6 **Moving / off-dribble** — camera-pan tracking.
- 4–5 **Go-To** — must still fire every time (never blind-fires by design).
- 3–4 **weird**: near the scorebug, red-jersey defender close, one right after a cutscene.

Note (or screen-record) any shot where the box wasn't glued, a box appeared on a non-meter, Square
failed to release, or the stream hiccuped.

Immediately after, before any relaunch:
```powershell
Copy-Item logs\diagnostics\detframes.csv logs\diagnostics\detframes_SHADOW_$(Get-Date -Format yyyyMMdd_HHmm).csv
```

---

## Stage 2 — read the shadow report → GO / NO-GO
```powershell
python tools\timing\fused_shadow_report.py    # would-fire vs actual, per shot + per type
python tools\diagnostics\analyze_shadow.py     # green-zone self-grade breakdown
```
**GO to Stage 3 only if all hold:**
- Shadow fused-fire would have landed the tip with **tighter σ than the classical fire actually did**
  (the whole point — if it's not tighter, don't flip).
- Green-zone self-grade agrees with what you *saw* on screen (spot-check 5 shots against the recording)
  — this proves the auto-grader is trustworthy before we let it grade the flip.
- **Per shot type**, not just the aggregate — especially fades. If fades shadow-worse, flip only the
  types that pass and leave fades classical (see caveat).

**NO-GO** → the shadow log tells you which estimator is off; fix offline, re-shadow. Costs nothing.

---

## Stage 3 — FLIP the fire path (this is the real thing)
```powershell
$env:ORION_FUSED_FIRE        = "1"   # act on the fused decision
$env:ORION_MEASURED_LEAD     = "1"   # use the oracle-measured round-trip lead
$env:ORION_GREEN_ZONE_WINDOW = "1"
$env:ORION_GREEN_SELF_GRADE  = "1"
# Optional ceiling levers — turn on ONE at a time across separate batches, never all at once,
# so a regression is attributable:
#   $env:ORION_PLATEAU_AIM      = "1"   # aim the cap-hold plateau (~50ms @ fill=100), not green-center
#   $env:ORION_TEMPLATE_ARRIVAL = "1"   # predict green-arrival from the fill animation (~14-16ms σ)
#   $env:ORION_PRESS_T0         = "1"   # anchor the hold clock to the native press t0
#   $env:ORION_BANDIT_LEAD      = "1"   # online lead auto-tune
.\run_orion.local.ps1 -Detdiag -Framedump
```
Fire the **same 30–40 shot matrix** as Stage 1. Copy the CSV out afterward:
```powershell
Copy-Item logs\diagnostics\detframes.csv logs\diagnostics\detframes_FIRE_$(Get-Date -Format yyyyMMdd_HHmm).csv
```

---

## Stage 4 — grade the greens (the verdict)
```powershell
python tools\diagnostics\grade_live_batch.py    # green rate, per type, vs the shadow prediction
python tools\timing\live_batch_report.py
```
**Success bar (green = make):**

| metric                      | classical (Stage 1) | flipped target |
|-----------------------------|---------------------|----------------|
| **green rate (overall)**    | your baseline       | **≥ 90%**, trending to the 92–99% ceiling |
| green rate — standstill     | baseline            | highest; should hit the ceiling first |
| green rate — fade           | baseline            | ≥ standstill − a few pts (watch the caveat) |
| σ of release vs tip         | baseline            | **tighter than classical** or don't keep the flip |
| blind-fire releases         | —                   | < 5% |
| Go-To release (never abort) | —                   | fires every shot |

If flipped ≥ classical on green rate **and** σ, the flip stays. If a specific type regresses, unset
its lever and re-batch that type only. Persist the winning flag set into `run_orion.local.ps1`.

---

## Known caveat — fades (from the timing red-team, docs/REDTEAM_TIMING_ARBITRATION.md)
Under `autonomousVision` (the shipping default) the **fade timing loop never closes**: a fade fires on
`shotTypeMeterToReleaseMs`/`shotTypeOffsetMs` but `learnFromOutcome` only updates `learnedLatencyMs`,
which the fade fire never reads. So the per-type fade clock is seeded once and then frozen — any
systematic fade mistiming is **permanent, not self-correcting**. Two consequences for this runbook:
1. Grade fades as their **own** bucket. A flat fade green rate that won't improve across batches is the
   symptom, not bad shooting.
2. The native fix for this (close the fade learn-loop) is queued in the fix pass. If it lands before
   your live day, fades should self-correct like every other type. If not, the fade clock is whatever
   its one-time seed made it — good seed = good fades, bad seed = uniformly-off fades that need a
   manual `shotTypeOffsetMs[fade]` nudge.

## Also live-only (separate, not blocking the flip)
- **120 fps capture** verification (frame-FPS forced to 60/120 in the deploy — confirm no dupes).
- **Dual-capture rig** if you want ground-truth latency measurement.
- The fork decode-corruption fix (reference-frame bitmap, docs/REDTEAM_CHIAKI_FORK.md F1) only shows
  under real packet loss — worth a deliberately-congested test once the fix lands.
