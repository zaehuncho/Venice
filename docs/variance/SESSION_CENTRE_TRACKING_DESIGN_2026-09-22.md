# Per-session centre tracking: design (2026-09-22, draft, not built)

## 1. What the data says (online open standstills, 09-15 to 09-22, 15 sessions, sweep sessions excluded)

**The verdict carries over one shot, and no further.**

| Lag | Correlation of the signed verdict (EARLY −1 / EXC 0 / LATE +1) | z vs shuffled |
|---:|---:|---:|
| 1 | +0.196 | +5.7 |
| 2 | +0.044 | +1.7 |
| 3 | −0.025 | −0.1 |
| 5 | −0.092 | −2.0 |
| 10 | +0.025 | +1.1 |
| 20 | −0.044 | −0.5 |
| 40 | −0.064 | −0.7 |

- The correlation is strongest when shots are close together: r = +0.32 when the next press is within 4 s of the previous one.

**What follows each verdict:**

| After | n | Next EARLY | Next EXCELLENT | Next LATE |
|---|---:|---:|---:|---:|
| EARLY | 87 | 21% | 66% | 14% |
| EXCELLENT | 486 | 12% | 74% | 15% |
| LATE | 156 | 6% | **47%** | **46%** |

**The late state is not the bot.**
- After a LATE, the bot's release-after-onset is unchanged (135 vs 143 ms).
- The meter appears later after the press (onset median 534 vs 501 ms), and the current LATE shots have onset 544 against 502 for greens.

**The previous verdict is not already captured by the current onset.** Logistic model for LATE (n = 448 fast follow-ups, under 7 s):

| Term | Coefficient |
|---|---:|
| Current onset (per 40 ms) | 0.14 |
| Previous LATE | 1.76 |
| Previous EARLY | −0.13 |

- Adding the previous verdict: likelihood-ratio p = 5.8e-10. Adding the previous shot's onset: p = 0.41.
- At a median onset, P(LATE) is 11% after a non-LATE shot and 43% after a LATE one.
- The state is hidden from the meter we see before firing; the banner reveals it one shot later.

**Session lean.**
- The mean signed verdict per session has sd 0.13 (range −0.26 to +0.26).
- The existing BannerLeadTrim sees these verdicts, but its directional hysteresis makes it deliberately slow. It stepped 7 times in 430 shots on 09-22.

**Invalid pilot (do not reuse).** Shots after a LATE in the two sweep sessions, split by their random offset:

| Offset | n | EXCELLENT | EARLY |
|---|---:|---:|---:|
| ≤ −5 ms | 10 | 20% | 60% |
| −5 … +5 ms | 35 | 63% | 11% |

- This is confounded: most LATEs in those sessions were caused by our own + offsets, not by the hidden state.
- Its only lesson: a correction applied when the state is NOT late costs EARLY misses fast. Keep K small.

## 2. Variance by timescale and observability (the measurement framework)

| Component | Timescale | Observable before the fire? | Instrument | Lever |
|---|---|---|---|---|
| A. Onset jitter | per shot | yes (press → meter onset) | regression on onset | onset feedforward (built, ships at 0.2 / 10 ms) |
| B. Hidden late state | ~1 shot, strongest < 4 s | no, only the previous banner | lag-1 table and logistic (above) | **late carry (new, below)** |
| C. Session lean | a session | slowly, via verdict counts | between-session sd of the signed verdict | BannerLeadTrim (exists, slow) |
| D. Residual | per shot | no | what is left after A–C | none. Only a wider window (player ratings) |

- The sweep converts verdict rates into milliseconds. From block 2's psychometric curve, the LATE rate rises from ~10% at 0 ms to ~36% at +20 ms. A 43% post-LATE LATE rate is therefore like being ~+15–20 ms late on the shots that really are in the state.
- Better measurement would find a **pre-fire** observable for B, so the first shot of a streak can be corrected too (§4).

## 3. Design: late carry (component B)

- **Rule.** A one-shot pull on the next fire.
  - Condition: the previous Standstill shot was graded **LATE** (attributed banner, `attributed=1`); this press comes ≤ 7 s after that one; same court; same shot type.
  - Action: apply **−K ms** to this shot's scheduled fire.
  - After EARLY: nothing (its coefficient is ~0).
  - It does not accumulate: a second LATE re-applies −K, it does not make it −2K. It expires after one shot, matching lag 2 ≈ 0.
- **Size.** Start with K = 8 ms, hard cap 15.
  - Only ~43% of post-LATE shots are really in the late state, and the rest pay EARLY for any pull.
  - So the expected-value optimum sits well under the ~15–20 ms the state itself is worth.
  - The prospective A/B sets the final K.
- **Where.**
  - Applied at the same choke point as the dev offset and the onset FF: `scheduleFire` in AutomationEngine, the single place a deadline becomes the fire.
  - It stacks with the FF. The total displacement is bounded by the existing trim / FF bound.
  - The shot it displaces is fenced from the learners, like `onset_ff_displaced`.
- **The race.** The banner for shot n arrives ~1.2–1.3 s after its release. If the next press fires before it is attributed, the carry cannot apply: log `LATE CARRY: reason=verdict_pending` and apply nothing. That fraction must be measured; fast re-shots may lose some of the benefit.
- **Log line.** `LATE CARRY: prev_seq= prev_verdict= gap_ms= applied_ms= arm= shot_attempt=`, one per eligible shot, applied or not.
- **Dev A/B hook** (production-excluded, like `ORION_DEV_FIRE_OFFSET_SWEEP`): `ORION_DEV_LATE_CARRY_AB="0,8"` randomises K per eligible shot.

## 4. Pre-registered validation (before any data)

- **Population:** open standstills that follow a LATE within 7 s; the banner was attributed before the fire.
- **Arms:** K ∈ {0, 8} ms, randomised per eligible shot.
- **Primary:** the EXCELLENT rate at 8 minus the rate at 0, with no-banner counted as a miss. Newcombe 95% interval.
- **Secondary:** the EARLY rate must not rise by more than it saves in LATE, and non-eligible shots must be unchanged.
- **Size:** ~110 eligible shots per arm, to detect +15 points. At ~20% eligibility that is **~1,100 total shots**, across ≥ 3 sessions (different days).
- **Ship rule:** EXC gain ≥ 5 points with the interval excluding 0 → K = 8 becomes the default. Otherwise do not ship.
- **Honest prize:** the state affects ~20% of shots. Moving those from 47% toward ~62% is worth **+3 points overall**, perhaps 76 → 79–80 on a good session. It is not mid-80s by itself.

## 5. Brainstorm: getting ahead of the state (make B visible before the fire)

1. **Input-path RTT from the fork.** Chiaki's takion layer acks our input datagrams. Logging the ack RTT continuously in OrionStream measures "our press reaches the PS5 late" before the fire. This is the most direct candidate for component B. It costs a small fork change plus a log line; no timing behaviour changes.
2. **Network capture, block 2.** `D:\NexusVision\netcap\20260922_220018` (74 MB).
   - Test court RTT at the press, PS5 uplink cadence and inter-arrival jitter against the verdict, with the randomised offset as a covariate. The randomisation makes this cleaner, not dirtier.
   - Script: `tools/timing/net_join.py`.
   - One session only: exploratory.
3. **Capture-side frame pacing.** Whether the PS5's video frame cadence (duplicate or late frames) shifts in the late state is already logged as `Capture health` (`dup%`, `uniqfps`).
4. **Press → animation-start latency** (the gather anchor) as a second onset-like observable. See [ANIMATION_ANCHOR_V2](../ANIMATION_ANCHOR_V2.md).
5. **Not levers:** re-shot pacing (the customer controls it) and ratings (a customer-side window width).

If any of 1–4 predicts LATE before the fire as strongly as the previous verdict does, it replaces the late carry. It would also correct the FIRST shot of a streak, which the carry never can.

## 6. Result of §5.2: the network capture (09-22 night)

**Setup.**
- 378 graded standstills from both sweep blocks, joined to `netcap/20260922_220018`, with the randomised offset as a covariate.
- Game server `3.227.236.64:30004` (AWS). The PS5 sends at 20 Hz; the server sends at ~21 Hz (47 ms).
- The server drops ICMP, so there is no ping RTT; the downlink is measured by arrival jitter against the server's tick.

**Result: none of these predict LATE before the fire.**

| Feature | p |
|---|---:|
| Relative server→PS5 one-way delay, last 1 s | .26 |
| Relative server→PS5 one-way delay, last 3 s | .52 |
| Max server packet gap, last 2 s | .12 |
| Server packet gap sd, last 2 s | .04, wrong sign; 1 of 8 tests |
| Gateway RTT | n.s. |
| PS5→PC Remote Play rate | n.s. |

- Quartiles of downlink delay give LATE rates of 20% / 24% / 15% / 17%: no trend.
- **The downlink and the home LAN are ruled out** at this power (one evening, verdicts dominated by the offsets).

**The late carry itself is stable across days.** P(LATE after a LATE) vs P(LATE otherwise):

| Day | After LATE | Otherwise |
|---|---:|---:|
| 09-17 | 26% | 13% |
| 09-19 | 67% | 19% |
| 09-20 | 57% | 10% |
| 09-21 | 29% | 14% |

**Remaining unobserved pipes:**
1. Our input path PC→PS5 (takion ack RTT, a fork log line: §5.1).
2. The PS5→server uplink, which passive capture cannot see.

## 7. Build status (09-22 night)

**Engine.** `AutomationEngine` gains `lateCarryForShotMs` / `noteLateCarryVerdict`.
- **Recording.** The previous release is stamped in `noteOnsetFeedforwardRelease`, and its verdict in `observeBannerVerdict` (ahead of the trim kill switch). Only the most recent release's verdict counts.
- **Decision.** One per physical shot. It is re-decided only while `verdict_pending`, so a verdict that lands before the fire still counts. Reasons: `no_previous`, `not_standstill_pair`, `stale` (> 7 s since the previous release), `verdict_pending`, `prev_not_late`, `eligible`.
- **Application.** The carry is applied after the onset FF at the same choke point. It is folded into `schedFireAppliedOnsetFfMs_`, so the existing learner fences cover carried shots. Its own cap is 15 ms.
- **Arming.** Only by `ORION_DEV_LATE_CARRY_AB` (dev builds; the parse is `#ifndef ORION_PRODUCTION_BUILD`). A production build never carries.

**Launcher.** `run_orion.local.ps1 -LateCarryAB` sets the arms to `0,8`. The variable is in the scrub list, the switch survives the elevation relaunch, and it is refused together with `-OffsetSweep` or `-OffsetList`.

**Verified.** It builds cleanly (17:51 local) and ctest passes 30/30. There is no dedicated unit test yet; the first live check is the `LATE CARRY:` lines.

**Grading.** Join each `LATE CARRY: reason=eligible arm=` line (by physical_epoch) to the shot record's banner.
