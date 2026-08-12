# Venice — Master Rig Batch

Ship 2026-08-28. Branch `fix/timing-input-and-remoteplay-blockers` @ `70f980d`, pushed.
**Relaunch before testing — five commits landed since the build you last ran.**

---

## FIRST: a number I got wrong

I told you we were "done bot-wise" off a **3.7% under-timed** figure. That used `peak < 95` as the
miss threshold — a bar I invented. Your green window starts at **98.0**. Scored against the real
window, the same session was:

```
peak < 95          (my bar)            3.7%
peak < green_start (the real bar)     25.9%     <- one in four
peak > green_end                       0.0%
in green                              74.1%
```

**We are not done bot-wise.** One shot in four misses green, always low — over-timing does not
exist at all (0.0% across every shot type, all 528 delay-off landings). It is a *spread* problem:
the window is ~2 pp wide and peak spread is 3.3 pp (Standstill) to 8.4 pp (Right Fade).

That also kills "aim higher" permanently. The optimal constant aim offset is **+0.0** for every
shot type — a shift just trades under-misses for over-misses. Only narrowing the distribution helps.

---

## What changed since your last run

| commit | what |
|---|---|
| `26261bf` | **A7** — every graded landing now carries `vel_at_rel`, `frame_age_ms`, `rtt_ms` |
| `1fc8f6a` | steal no longer poisons the ownership trace; load-time warning if the lead can't be scheduled |
| `70f980d` | Meter Delay card hidden; connect readiness poll 100 ms → 25 ms |
| `021f6af` | **R3 = Square** (the steal), earlier today |
| settings | `press_anchored_predictor_enabled` **false**, `meter_delay_lead_offset_ms` **90 → 80** |

The offset move matters: your lead **learned from 290 to 297 during today's sessions**, which
silently pushed my "safe" 90 to 387 against a 386.3 ceiling — 0.7 ms over, i.e. delay would have
aborted every shot again. 80 leaves 9.3 ms of margin, and the new warning now catches it at load
instead of costing you a session.

---

## The session — one sitting, ~15 minutes

Relaunch, delay off (the card is hidden now), then:

1. **~40 normal shots**, usual mix.
2. **~10 Go-To from beyond half court**, in one block so I can find them by timestamp.
3. **R3 on defense** — the steal. Tell me whether it works; the log can't tell me.
4. **Anything that felt wrong.** You've been right three times today when the numbers weren't.

### What I'll read

**Consistency:** `peak < green_start`, against the **25.9%** baseline. Not my old 95 bar.

**A7's payoff:** with `vel_at_rel` / `frame_age_ms` / `rtt_ms` now on every landing, I can finally
regress the residual against pipeline state and answer the question that decides the next build —
is the Fade miss rate RTT-driven, velocity-driven, or random? That determines whether a lead
controller should be feed-forward or pure feedback. One session of data is enough.

**Go-To:** the courtwide steal has seated **19 times ever**. A clean block tells me whether the
ladder never finds a candidate or finds one and a veto discards it — different fixes.

---

## Why the residual is not what I said (corrected)

I claimed it was "downstream of the press" and that no predictor or reader work could help.
**That was circular** — `travel_pp` is `peak_fill − fill_at_rel` by definition (652/652 rows), and
peak was my grouping variable, so "they differ in travel" was the same sentence as "they differ in
peak".

The real structure splits by shot type, and it reproduces independently:

```
shot           n    corr(fill_at_rel, peak)   peak_sd   under-miss
Standstill   227           -0.00               3.34       15.0%
No_Dip        45           +0.01               3.91        6.7%
Right_Fade   104           +0.62               8.42       14.4%
Left_Fade     97           +0.56               4.68       22.7%
Go-To         55           +0.50               2.42       18.2%
```

**Standstill / No Dip** self-correct — fire early, the meter travels further, peak lands the same.
Nothing upstream helps there.
**Fades / Go-To** leak fire jitter straight into peak. A tighter fire *does* tighten the outcome —
and they are the worst offenders. My blanket "no predictor work helps" was wrong for exactly the
shots that need it most.

---

## Still confirmed dead — do not revisit

- **Aim higher / per-type aim offset** — optimal constant offset is +0.0. Proven, not argued.
- **Sub-pixel reading** — `ORION_READER_SUBPIX_EDGE` exists and is off on purpose: removes 0.23 ms
  of noise, introduces 1.88 ms of bias.
- **1080p detection** — null against a same-day control (p=0.80).
- **Capture format** — `ORION_CAPTURE_MJPG` is a no-op; the HD60X returns YUY2 regardless.
- **Meter delay as a meter-quality feature** — green width 13.9 pp at D=200 vs 2.8 pp at D=0, and
  bounce-back 100% vs 16.8%. It makes the read *worse*. Card is now hidden.

## Next, once the session lands

1. **A5 tick probe** — both consumers are already written and gated; only the phase probe is
   missing. Verified: **zero `tick_phase` entries in the entire log**, and the code says so at
   `AutomationEngine.cpp:11719` — *"not one probe label has ever been accepted on this install."*
   Kills the ±8.3 ms send-vs-poll beat, which is 1.5 pp of a 2 pp window. **Not attempted today** —
   it is real new measurement code, and I would rather build it against A7's data than rush it.
2. **A2 bounded lead controller**, fed by A7's regression.
3. **A6 sub-frame peak** — the graded peak is a discrete frame max quantised to ~3.7 pp, which is
   *larger than the entire green window*. We are grading with a ruler coarser than the target.

## Ships Aug 28 regardless

`#47` capture-revocation fix · IPC + QProcess leak fixes · `0.0`-epoch velocity guard ·
port-mismatch warning · `cap_mode` + SUSPECT diagnostics · A7 landing features · press-anchored
predictor off · R3 Square passthrough · lead-ceiling warning · Meter Delay hidden ·
**installer repackage LAST, by the 24th — the only item with no slack.**
