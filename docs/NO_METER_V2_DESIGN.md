# NO METER v2 — press-anchored release the owner can actually tune

Status: DESIGN (no engine code changed by this document).
Data: `tools/timing/nometer_prior_audit.py`, run over `logs/orion_native.log.1` +
`logs/orion_native.log` (2026-09-12T17:33Z → 2026-09-14T20:29Z), 487 graded meter-path shots,
41 of them joined to the game's own TIMING banner
(`logs/diagnostics/framedump_archive/session_20260912_{124114,145457}/panel_grade.csv`).
Outputs: `logs/diagnostics/nometer_study/`.

---

## 1. What is actually broken

### The control law subtracts a quantity that does not exist on this path

`AutomationEngine::processInputTimedIdle` (v1):

```
hold = max(kInputTimedMinHoldMs /*390*/, pressAnchoredLearnedTipMs(type) - inputTimedLeadMs)
```

**The `- lead` term is the whole bug.** A shot lead exists on the *vision* path because the
engine fires relative to a tip it predicts in **video** time, which is late by the video
latency, and the command then needs its own travel time. None of that applies here.

Press-down and release travel the **same** pipe. From the log (epoch 111, 2026-09-13T02:19):

```
02:19:10.749Z  Physical shot epoch: epoch=111 intent=square_edge
02:19:10.750Z  Square-down delivery identity: physical_epoch=111 square_bit=1 delivery_stage=local_udp_accepted
02:19:11.789Z  Release submit: seq=59 square_bit=0 backend=PIPE
```

The console therefore sees a Square hold of **exactly** `release − press`, shifted whole by the
command latency. The command latency **cancels**. The only quantity that decides where the
release lands in the animation is the **hold duration**. There is nothing to lead.

### The prior was never short — it is (to ~6 ms) the hold itself

`pressAnchoredLearnedTipMs` is fed by `emitPressTipObservation()`:

```
real_tip_wall = meterCapPhaseStopMs_ (the CONFIRMED METER STOP, in video time)
                - applied_delay - V          // V = pressAnchoredVideoLatencyMs() = l_fixed
press_to_tip  = real_tip_wall - press_wall
```

The "tip" it dates is the meter **stop** — and the meter stops because *we released*. So the
learner is not measuring an independent animation landmark at all; it is measuring **our own
release, seen through the video, converted back to console time**. Empirically:

| | eps = press_to_tip − hold, all shots | eps, V healthy (185–220 ms) |
|---|---|---|
| Standstill | +7.4 ms (IQR 18.0, n=267) | **+5.9 ms** (n=221) |
| Left Fade | +12.7 ms (n=97) | **+7.7 ms** (n=77) |
| Right Fade | +14.0 ms (n=77) | **+8.7 ms** (n=63) |
| Go-To | +91.8 ms (n=37) | +81.2 ms (n=19) |
| No Dip | +131.1 ms (n=9) | — (no V-healthy samples) |

`eps` is `(real command→visible-stop loop) − V`; it tracks V's health and nothing else
(when `V_ms` dipped to 146–157 the same shots showed eps 88–139 ms with the hold unchanged).

**So: prior ≈ hold + ~6–9 ms.** The v1 law then subtracts a 272 ms lead on top and lands
~260 ms early — which is exactly what the owner reported.

### The numbers that close the case

| type | n | hold median (press→release the METER path uses) | rMAD | prior_ms (from the NO METER log) | v1 `hold = prior − 272` | error |
|---|---|---|---|---|---|---|
| Standstill | 267 | **653.8 ms** | 25.6 | 670.2 (n=100) | 398.2 | **−256 ms (early)** |
| Right Fade | 77 | **956.6 ms** | 52.0 | 997.2 (n=100) | 685.2 @ lead 312 | **−271 ms** |
| Left Fade | 97 | **928.8 ms** | 50.4 | — | — | — |
| Go-To | 37 | 2093.4 ms | 59.5 | — | — | — |
| No Dip | 9 | 636.4 ms | 92.4 | — | — | — |

The slider could never reach the truth: `inputTimedLeadMs` clamps to 150..400, so the longest
Standstill hold v1 can produce is `670.2 − 150 = 520.2 ms` — still **134 ms early** — while the
short end (`670.2 − 400 = 270.2 ms`) is deep inside pump-fake territory.
That is "no matter how I tuned the lead it would be early; the late part gave me a pump fake",
exactly.

Two supporting facts: the timer itself is blameless (`NO METER RELEASE` lateness median
**0.002 ms**, max 0.012 ms over 53 releases), and the clock join used above is sound
(hold measured on the monotonic clock vs the UTC log lines agree to **−0.1 ms**, IQR 0.9,
n=486).

> **Correction owed to `AutomationEngine.h`.** The `kInputTimedMinHoldMs` comment states
> "the one hold PROVEN to shoot is the meter path's own Standstill hold at lead 272-279
> (~391-398 ms, hundreds of shots)". That is false: 391–398 ms is `prior − lead`, a NO METER
> number. The meter path's own Standstill hold is **654 ms** (n=267, rMAD 26). The floor is
> still worth keeping — but for the pump-fake reason only, not that one.

---

## 2. The pump-fake commit threshold

What the logs *can* say:

* The whole 2026-09-13T06:56–07:00Z NO METER session ran with detection dead
  (`NO METER: 3 shots with no detection — Detection is set to Purple`), so no landing was
  graded and **the log cannot tell a pump fake from a shot**. Every one of the 55 arms that
  was not cancelled produced a `NO METER RELEASE` line. Holds swept: 270.2, 308.2, 333.2,
  346.2, 353.2, 358.2, 368.2, 371.2, 389.2, 398.2, 404.2, 427.2, 434.2, 469.2, 480.2, 482.2,
  492.2, 507.2, 520.2 (Standstill) and 685.2, 796.2 (Right Fade).
* The meter-path corpus gives an upper bound from the other side: of 487 shots that produced a
  real meter and a graded landing, only **2** held under 450 ms —
  **440.3 ms** (Standstill, peak_fill 35.8 — a meter appeared, so the game took it as a shot)
  and **480.4 ms** (Standstill, peak_fill 90.3, an ordinary shot). One 333.6 ms Right Fade
  record is degenerate (`fill_at_rel=0.0`) and is not evidence either way.

**Verdict:** the threshold is not measurable from the logs on hand. The only quantitative
evidence is the owner's own bracket, recorded in the engine header: slider ≥ 60
(lead ≥ 299 → hold ≤ 371 ms) pump-faked; slider 53 (lead 281 → hold 389 ms) shot. The boundary
lies in **(371, 389] ms**, and 440 ms is independently proven to shoot. Marked UNVERIFIED
beyond that.

v2 makes the question moot: its *minimum reachable* hold is 500 ms, ~110 ms above the highest
plausible threshold.

---

## 3. NO METER v2 — the control law

```
hold(type, mode) = clamp_lo( H_ref + Δ(type) + R(mode), kNoMeterHardFloorMs )

H_ref  = 500 + 3 * (slider - 1)        ms   slider 1..100  ->  500 .. 797 ms
Δ(type)                                ms   per-type offset, table below, Standstill = 0
R(mode) = +40 ms if Rhythm/TempoSquare, else 0
kNoMeterHardFloorMs = 450              ms   (pump-fake bracket 371..389 + ~60 ms margin)
```

**One slider, in milliseconds, and it is the only thing the owner touches.**
3 ms per step — finer than the Standstill shot-to-shot spread (rMAD 21 ms), coarse enough that
the full 100 steps span ±150 ms around the default, which is ±6 rMAD.

**Default: slider 51 → H_ref = 650 ms**, the measured Standstill press→release hold
(651.3 ms over 238 ButtonShot vision shots; 649.5 ms over the 9 banner-GREEN Standstill shots
in the V-healthy subset; 666.8 ms over all 20 banner-GREEN Standstill shots — all inside one
slider step of 650).

The floor can never bind from the slider (500 + min Δ = 485 ms > 450), so **"tune it late and
you get a pump fake" is impossible by construction** — the failure the owner hit cannot recur.

### Δ(type) — measured, ButtonShot, this rig

| type | mode | n | hold median | rMAD | **Δ vs Standstill** | confidence |
|---|---|---|---|---|---|---|
| Standstill | ButtonShot | 238 | 651.3 | 20.8 | **0** | strong |
| Left Fade | ButtonShot | 83 | 927.1 | 49.9 | **+276** | strong |
| Right Fade | ButtonShot | 66 | 955.3 | 48.1 | **+304** | strong |
| No Dip | TempoSquare only | 9 | 636.4 | 92.4 | **−15** | weak — seed, do not ship as measured |
| Go-To | GoToStick | 37 | 2093.4 | 59.5 | **+1442** | see below |

Rhythm (`ShotMode::TempoSquare`) holds are consistently **longer**:
Standstill 691.7 (n=29, +40.4), Left Fade 954.0 (n=14, +26.9), Right Fade 1011.1 (n=11, +55.8)
→ a single `R = +40 ms` covers it inside the noise. Rhythm is a *mode* offset, not a type: it
must not be folded into Δ, because the same type is shot both ways.

**Go-To**: `hIQR` is 71–282 ms and the wind-up is genuinely variable (the animation, not the
measurement). Keep Go-To **out** of the blind path — the same exclusion `pressAnchoredTipMs`'s
factory-bootstrap already applies. A Go-To is also a stick shot, so it rarely reaches the
Square-press arm at all.

### Seeding Δ for a type with few samples

Δ is a *difference of holds*, so `eps` and every constant latency cancel out of it — which is
why Δ can be seeded from the existing press→tip factory priors without correction:

```
Δ_seed(type) = pressAnchoredTipMs[type] - pressAnchoredTipMs["Standstill"]
```

Current factory seeds give Δ_seed: Left Fade +208, Right Fade +248, No Dip −213, Go-To +1265 —
**60–140 ms short for the fades and Go-To, and 200 ms wrong for No Dip**. Reseed
`AppConfigData::pressAnchoredTipMs` (or a new per-type Δ map) from the measured column above:
Standstill 651, Left Fade 927, Right Fade 955, No Dip → seed from Standstill (651) until it
has ≥ 20 samples, Go-To 2093.

Adoption rule, mirroring the existing weight gate: use the **measured** Δ once the type carries
`≥ kInputTimedPriorMinWeight (8)` observations; below that use Δ_seed; a type with neither
falls back to Δ = 0 (Standstill) rather than to a global delay.

### Learn the hold, not the tip

Long-term, the learner should record the **hold** (`command_issued − press_wall`) on every
graded meter landing, not `press_to_tip`. Same line, same shots, one subtraction fewer — and it
drops the dependence on `V`, which is the only thing that moves `eps` (this corpus shows eps
swinging 6 → 139 ms purely on V drift, with the hold unchanged). Until then, `hold = prior − eps`
with the small per-type eps below is within a few ms.

### What the log line must carry

```
NO METER v2: hold_ms=690.0 type=Standstill mode=TempoSquare slider=51 href_ms=650.0
             delta_ms=0.0 delta_src=measured delta_n=238 rhythm_ms=40.0
             floor_ms=450.0 floored=0 prior_ms=670.2 prior_n=100 arm=7
```

Every term of the sum, its provenance, and the prior it did *not* use — so
`nometer_prior_audit.py` can grade a NO METER session the same way it grades the meter path,
and so a future "it fires early" report can be answered from one grep.

### How the owner tunes it

One rule, using the game's own banner:

* **EARLY** → slider **up** (longer hold). 1 step = 3 ms ≈ 0.54 pp of meter fill.
* **LATE** → slider **down**.
* Measured on this rig (n=470 landings): green window **2.88 pp median** (p25 1.84, p75 4.69)
  at **0.1805 pp/ms** → **16 ms median** (p25 10 ms, p75 26 ms) — about **5 slider steps end
  to end**. Move 2–4 steps at a time, not 20.

Label the control **"No Meter Hold"** and show the millisecond value next to it. It is a
duration, and it should read like one.

### Drop `inputTimedLeadMs` — yes, explicitly

**Recommendation: delete the No Meter Lead control and the `- inputTimedLeadMs` term.**
It is a vision-path quantity on a path that has no vision. Keeping it means every future Shot
Lead change silently re-breaks NO METER by the same 150–400 ms, and it is the direct cause of
the owner's failed tuning session. `inputTimedDelayMs` (the flat 500 ms fallback) also goes:
v2's `H_ref + Δ` covers the no-prior case with a number that is at least the right order.

---

## 4. The same fix for the meter-path fallback

`maybeFirePressAnchoredFallback()` (the `press_unanswered_no_meter` rescue) currently uses the
**identical broken law**:

```cpp
const double leadMs = measuredLeadForActuationMs();
const double holdMs = std::max(kInputTimedMinHoldMs, priorMs - leadMs);
const double fireAtMs = squareHoldStartMs_ + holdMs;
```

With this rig's Standstill prior 670.2 and measured lead 274, that fires at **396 ms** against a
proven 651 ms — **every rescued shot is ~256 ms early.** There are 311 `press_unanswered_no_meter`
presses in this corpus, so this path is not rare.

**Preferred fix (recommend to the fallback agent): drop the `- leadMs` and let the bias be the
small `−eps` correction.**

```
hold = max(kNoMeterHardFloorMs /*450*/, prior + press_anchored_fallback_bias_ms(type))
```

| type | `press_anchored_fallback_bias_ms` (law WITHOUT the lead term) |
|---|---|
| Standstill | **−6** |
| Left Fade | **−8** |
| Right Fade | **−9** |
| Go-To | **−81** |
| No Dip / unknown | **−10** (default) |

A flat **−10 ms** is within noise for everything except Go-To.

**If the `- leadMs` term must survive** (`hold = max(floor, prior + bias − lead)`), the bias has
to add the lead back and becomes rig- and setting-dependent — at this rig's measured lead of
274 ms:

| type | bias with the lead term retained |
|---|---|
| Standstill | +268 |
| Left Fade | +266 |
| Right Fade | +265 |
| Go-To | +193 |

This form is fragile and **not recommended**: `measuredLeadForActuationMs()` is user-tunable
(150..400) and drifted 261→281 ms inside this corpus alone, so any Shot Lead change silently
re-breaks the fallback by that amount.

---

## 5. Honest expectations

Open loop has no feedback term, so the whole error budget is the hold's own spread:

| type | n | rMAD of hold | within ±8 ms (half the median green window) | within ±16 ms | within ±30 ms |
|---|---|---|---|---|---|
| Standstill | 267 | 25.6 ms | **23 %** | 48 % | 67 % |
| Right Fade | 77 | 52.0 ms | 17 % | 25 % | 43 % |
| Left Fade | 97 | 50.4 ms | 13 % | 23 % | 47 % |
| Go-To | 37 | 59.5 ms | 19 % | 27 % | 41 % |

Read that as the **ceiling** for a perfectly centred blind release: roughly **1 green in 4 on
Standstill**, ~1 in 6 on the fades. The meter path on the same rig graded 38 EXCELLENT out of
41 banner-joined shots. v2 removes a 256 ms systematic error — it cannot remove the animation's
own variance, and no press-anchored law can. Ship it as "shoots without the meter", not as
"shoots like the meter". (For reference: at the v1 law the Standstill release is 256 ms early,
i.e. **0 %** — so this is 0 → ~25 %, not a regression anywhere.)

---

## 6. Re-running the numbers

```
.venv/Scripts/python.exe tools/timing/nometer_prior_audit.py
.venv/Scripts/python.exe tools/timing/nometer_prior_audit.py --v-band 185 220 \
    --out logs/diagnostics/nometer_study/v_healthy
```

Writes `nometer_prior_summary.txt`, `nometer_prior_by_type.csv` (the Δ / bias table),
`nometer_prior_shots.csv` (per shot) and `nometer_arms.csv` under
`logs/diagnostics/nometer_study/`. Analysis tooling only — never imported by the engine,
orchestrator or sidecar.
