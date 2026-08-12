# Venice — Master Rig Batch

Ship 2026-08-28. Branch `fix/timing-input-and-remoteplay-blockers` @ `021f6af`, pushed.
**This is ONE session, ~15 minutes.** Meter delay is shelved. Everything below is delay-OFF.

---

## Before you start

**Relaunch.** The running app predates `021f6af`, so R3 does nothing yet.

```powershell
cd C:\Users\aaron\Desktop\NexusVision; .\run_orion.local.ps1
```

Meter Delay **OFF**. No env flags — the shipping default config is what we are measuring.
Close with the **window X** when done (the launcher force-kills, and that can zero
`actuation_lead_ms` mid-write).

Two settings I changed with the app closed, both already saved:

| | was | now | why |
|---|---|---|---|
| `press_anchored_predictor_enabled` | true | **false** | it was hijacking shots and firing at fill 18 |
| `meter_delay_lead_offset_ms` | 155 | 90 | only matters with delay on; 155 exceeded the 96 ceiling |

---

## The session — four things, one sitting

**A. ~40 normal shots**, your usual mix, MyCourt or a game. This is the consistency number.

**B. ~10 Go-To from beyond half court**, nothing else mixed in. Take them in a block so I can
find them in the log by timestamp.

**C. Press R3 a few times on defense** — that is the new steal button.

**D. Tell me anything that felt wrong.** Your read has been right every time today: "76 aborts,
you sure?", "square passthrough not working", "sometimes it only times the meter halfway". All
three were real and all three were things the numbers alone had not shown me.

---

## What each one is checking

### A — consistency (the "are we done" number)

Delay-off baseline, phase-armed, n=421:

| | share |
|---|---|
| UNDER-timed (peak_fill < 95 — fired before the tip) | 10.7% |
| OVER-timed (reached the tip, then settled < 90) | 6.9% |
| clean (peak ≥ 95 **and** settled ≥ 95) | 64.6% |

**PASS: under-timed drops well below 10.7%.** The press-anchored predictor accounted for a chunk
of it — every shot it armed failed to top out — and it is now off. First post-fix sample was 14/14
topping out, which is promising and far too small to trust.

`peak_fill` is the honest instrument here and you can read it yourself in the Activity log:
`peak = fill_at_rel + travel_pp`, and the meter stops where the release registers. **Near 100 means
it released at the tip; anything in the 80s means it fired early.** Only valid with delay off.

### B — Go-To beyond half court

`ORION_READER_STEAL_COURTWIDE` already defaults ON, so the fix we shipped for this is active and
you are still seeing it fail. The courtwide steal has only ever seated **19 times**.

The candidate it finds gets nulled by one of two vetoes before anything is logged
(`simple_meter_reader.py:5678-5681` — B8 clipped-sliver, B7 prior-presence), and for a
beyond-half-court meter at `h=4-9 px` both are plausible. A clean block of 10 lets me tell
"never found a candidate" from "found one and threw it away". Those are different fixes.

### C — Square / steals (#88, `021f6af`)

Tempo remap consumes Square at 21 sites across 12 functions, which is also why the steal died.
**R3 now emits a real Square** that the remap never touches — including mid-shot, while Square is
held and the stick is being driven. The click is consumed so the game does not also see a stick
press.

L3 is deliberately not the default: it is turbo in 2K, so Square there would steal on every sprint.
Change with `square_passthrough_button` (`r3` / `l3` / `none`).

My previous attempt at this (`73f8fcb`) tried to tell a tap from a hold and replay it, in the
Python layer, which cannot reach the console on a capture-card rig. This one cannot guess wrong.

---

## Where "strictly enforced tip timing" actually stands

Worth reading before deciding we are done, because the remaining error is not where we assumed.

```
                 fill_at_rel      travel_pp
under-timed         39.0            52.4
reached the tip     39.4            59.4
```

**The bot commands the release at the identical moment on both.** Command-point spread is 6.4 pp
(~35 ms); travel spread is 9.7 pp (~53 ms). The larger variance is *downstream of the press* — how
far the meter moves before the release registers.

So the residual is **not** fixable by a better predictor, a better reader, or sub-pixel work:

- Sub-pixel already exists (`ORION_READER_SUBPIX_EDGE`) and is off on purpose — measured at
  **0.23 ms** of noise removed against a **1.88 ms** bias introduced.
- 1080p detection: tested, null against a same-day control.
- Capture format: `ORION_CAPTURE_MJPG` is a no-op, the HD60X gives YUY2 regardless.

What would address it is aiming through the meter's own rate — fire when
`fill + velocity x latency >= 100` rather than at a fixed predicted moment. The engine already
computes exactly that quantity (`expectedRisePct = velocityPctPerMs * effectiveLatency`,
`AutomationEngine.cpp:8419`) — but only the vision-crossing release path consumes it, and your
shots fire on the **phase** path (`targetMode=meter_tip_phase`), which is rate-blind. The bounded
±18 ms vision nudge is gated on green-confirmation, and these shots log `greenConfirmed=0`.

**That is the one real bot-side change left.** It is a shot-path edit, which is the riskiest kind,
so I want session A's number first — if under-timing is already near zero with the predictor off,
it is not worth the risk this close to ship.

---

## Not in this batch

- **Meter delay** — shelved. Revives only if the contested-shot A/B says the netcode advantage is
  real. Inbound-only hold is confirmed (`MeterDelayIntercept.h:7`), and the "cleaner meter" claim
  is refuted: green width was 13.9 pp at D=200 vs 2.8 pp at D=0.
- **`meter_x` right-side effect** — real and reproducible (7/7 sessions, 16% vs 38%) but a second
  independent instrument disagrees about direction, so the metric itself is suspect. Needs a
  framedump, not more log archaeology.
- **Stick-value randomisation** — the reference GPC jitters every injected flick by ±7; we emit
  exactly `(0, ±127)` every time, a perfectly repeatable fingerprint. No evidence 2K looks. Your
  call, not mine to change unilaterally.
- `meterSettleAllowSmoothMotion`, `phase_veto_directional` — still not ready.

## Ships Aug 28 regardless

`#47` capture-revocation fix · IPC + QProcess leak fixes · `0.0`-epoch velocity guard ·
port-mismatch warning · `cap_mode` + un-truncated SUSPECT diagnostics · press-anchored predictor
off · R3 Square passthrough · meter delay off by default · **installer repackage LAST, by the 24th
— the only item with no slack.**
