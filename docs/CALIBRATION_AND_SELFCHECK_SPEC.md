# Guided Lead Calibration + Startup Self-Check — implementation spec

**Written 2026-08-04. Queued behind the animation-phase predictor and the Red+Purple colour work.**

## Why these two and not more predictor work

Five predictive hypotheses were measured and killed on 2026-08-04: sub-pixel reads, template
fit, the decoder video path, "error grows with horizon", and fed-tier coverage gaps. Each was
confidently argued and each was wrong. Anything whose value depends on our physics model being
correct carries that risk.

These two do not. A **check** either passes or names a specific failure — it cannot be wrong in
the way a predictor can. A **human-in-the-loop calibration** converges on the user's real number
without needing any of our models to be right, because it measures TOTAL residual error rather
than any one component of it.

That property is the entire point. Do not "improve" either feature by making it infer more.

---

## 1. Guided Lead Calibration

### The oracle problem this solves

The game's on-screen `TIMING: EARLY / GOOD / LATE` banner is **the only signal that has ever
tracked reality on this project**. Every automated substitute failed:

| instrument | failure |
|---|---|
| `travel_pp / velocity` | circular — it IS `peak_fill - fill_at_release` (AutomationEngine.cpp:10702), contains no clock |
| `settled_fill` vs green | flat response — 18ms of lead moved it 0.28pp |
| `(peak - fill_at_rel)/vel` | circular, restates its own input |
| `(f_stop - fill_at_rel)/vel` | tracks the lead 1:1, goes negative |

The automated banner READER was retired for sound reasons (1.2-2.7s delay, ~1.5s linger, so
verdicts mispair to the wrong shot in rapid play). **A human reading it has none of those
problems.** This design uses the human as the oracle and never OCRs anything.

It also replaces a mechanism that is actively wrong: the engine currently seeds the lead from
`travel_pp / velocity` after `actuationLeadSeedMinSamples = 12` landings, and that quantity
**over-reads the true lead by ~70ms**. Not a tuning error — a wrong instrument.

### Flow

1. User opens Calibration (Live tab).
2. Prompt: *"Take a shot. Watch the game's TIMING banner, then tap what it said."*
3. Bot fires at the current lead.
4. Three large buttons: **EARLY** / **GOOD** / **LATE**, plus **SKIP** (banner missed).
5. Adjust, repeat.
6. Lock, save, show the final number.

### Algorithm — bisection with reversal-triggered step reduction

```
step = 20.0                      # ms
lead = current effective lead
loop:
    EARLY -> lead -= step        # LARGER lead fires EARLIER, so EARLY means lead is too big
    LATE  -> lead += step
    GOOD  -> goodRun += 1 ; continue without moving
    SKIP  -> discard, no state change
    on direction reversal -> step = max(step / 2, 1.0)
    any EARLY or LATE -> goodRun = 0
    lock when goodRun >= 3, or step < 2.0
    clamp lead to [150, 450]     # AppConfig.h bounds, do not widen
```

Converges in 10-15 shots from any starting point. **The direction is assertion-critical and has
been inverted in UI copy before** — pin it with a test: larger lead fires earlier, therefore a
consistent EARLY verdict must DECREASE the lead.

### Persistence

Write `actuation_lead_ms` + `actuation_lead_user_set = true`. That path only became live on
2026-08-04 — either `ORION_LEAD_FLOOR_MS` or `ORION_LEAD_BIAS_MS` sets
`config_.leadOverrideFromEnv = true`, and `AutomationEngine.cpp:9094` then skips the
`userActuationLeadMs` branch entirely. Both are now commented out of `run_orion.local.ps1`.
**If either is ever re-enabled, calibration is silently inert** — detect that and refuse to
start with an explicit message rather than running a flow that cannot take effect.

### Guards

- Refuse to start unless a shot can actually be armed (no capture -> no calibration).
- Refuse to start if `leadOverrideFromEnv` is set (see above).
- Never write outside [150, 450].
- Nothing here may touch the fire path, fail-closed behaviour, or the abort logic. Calibration
  only ever changes one persisted number between shots.

---

## 2. Startup Self-Check

Six independent booleans. Each is either fine or names one specific, actionable problem. No
inference, no scoring, no aggregate "health" number.

| check | source | failure message |
|---|---|---|
| capture live | `uniqfps > 0` | "No video — check your capture card / Remote Play connection" |
| resolution supported | `_detector_frame_contract_reason` (remote_play_orchestrator.py:85-113) | "1024x576 not supported — need 1280x720 or 1920x1080" |
| sidecar up | existing sidecar watchdog state | "Detection engine failed to start" |
| input path connected | input hook / ViGEm heartbeat | "Controller path not connected — shots cannot be sent" |
| meter seen in N armed shots | `meterBlindWarning` | "NO METER — CHECK COLOR" |
| lease signed (**production only**) | `LeaseGate` | "Server lease unavailable — automation will stop in 15 min" |

The meter check is **already being implemented** alongside the Red+Purple colour work. Reuse it,
do not duplicate it.

### Why the last row matters

A production build force-enables the lease gate (`LeaseGate.cpp:35-38`, no env can disable it)
and demands a signature the deployed backend never sends. Today that presents as automation
silently dying ~15 minutes after activation, with no message. The dev build has the gate off, so
this is invisible on the dev rig by construction. This row converts it into a launch-time
statement.

### The design rule these share

This project's defining failure mode is **subsystems reporting healthy while dead**: the packet
bridge logged "connected" while WinDivert was denied; `FAKELOCK_BREAK` sat dead in production for
months while the gates reported `falselock: 0`; the colour picker offered four options and was
inert; the lead slider logged "your value" and discarded it.

Every check here must therefore derive from a signal that is **independent of the subsystem it is
checking**. The meter check is the model: it counts physical Square presses with zero detection,
so a detector that cannot see anything is structurally unable to suppress its own alarm.
