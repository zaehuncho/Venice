# Venice — Master Rig Batch

Ship: 2026-08-28. Branch `fix/timing-input-and-remoteplay-blockers`.
**Updated 2026-08-12 after S1/S2 came back. Both are closed. The batch is now two delay tests.**

---

## RESOLVED — stop testing these

**S1, capture format: NO-OP.** `ORION_CAPTURE_MJPG` does nothing on the HD60X. The code requests
MJPG at `capture_card_backend.py:696`, and the driver hands back `YUY2` regardless — confirmed live
via the new `cap_mode=1920x1080@60/YUY2/buf-1` field. We were already on 4:2:2. All 362 baseline
landings were too. The chroma-subsampling theory for the green smear is dead.

**S2, 1080p detector: NULL.** 47 landings.

| | 6pp+ share | bad (settled<90) |
|---|---|---|
| pooled 720p baseline (n=362) | 35.1% | 29.8% |
| **18:14 session, 720p, same day (n=41)** | **22.0%** | **17.1%** |
| S2, 1080p (n=47) | 19.1% | 10.6% |

Significant against the pooled baseline (p=0.03), **not significant against the session shot six
minutes earlier** (p=0.80). It was a day effect, not a resolution effect. The control even carried
5 No Dip attempts (our worst shot type) to S2's zero and still tied. Detector cost was a non-issue
— `detect_ms` p50 0.1 ms either way.

**Conclusion: the green-window smear is not a capture-quality problem.** Both fidelity levers are
spent. Do not spend more days here.

---

## The delay question, restated honestly

Owner's insight, 2026-08-12: delay may not help the *bot* at all — it may win **contested shots**,
because the meter is decoupled from the animation and the contest resolves before the meter does.

Mechanically this holds up, and it is the first theory that explains a measurement we already had.
`MeterDelayIntercept.h:7` and the filter clause at `.cpp:507` show the hold is **inbound only** —
public court server → console. Your console's local simulation runs untouched; only what it
*receives* is late. A symmetric delay would shift the whole interaction uniformly and cancel; we
measured that it does not cancel, and inbound-only is exactly the asymmetry that explains why.

It also means your console's picture of the defense is stale by D. A defender closing out in the
last 200 ms has not arrived yet in your console's world.

**Unknown, and not knowable from our code:** whether 2K evaluates contest on the shooter's console
or server-side. If shooter-side, the mechanism pays. If server-side, it does not. That is what
Test B measures.

### What the old delay data does and does not say

409 landings join to a press-tip observation, so each one carries its `applied_delay_ms`:

| | n | width p50 | 6pp+ | settled p50 |
|---|---|---|---|---|
| D=0 | 364 | 2.8 pp | 26.1% | 95.6 |
| D=200 | 44 | 17.1 pp | 90.9% | 52.7 |

That looks catastrophic — but **every one of those 44 delayed shots predates the press-anchored
predictor.** `press_anchored` appears 0 times in all four D=200 sessions (Aug 9-10) and first
appears Aug 11. Those landings measure a configuration we no longer run.

They are also confounded: `green_obs_width` is measured over the post-release settle window, so
"the reader saw a wider band" and "the old predictor fired late" cannot be separated in that data.

**So we have no valid current evidence about delay, in either direction.** Every earlier
conclusion — including the "delay gives a cleaner meter" claim in `MeterDelayController.h:9-11`,
and my own reading of it — rests on pre-predictor data.

### What IS live now

`settings.json`, verified today:

```
press_anchored_predictor_enabled  True
press_anchored_tip_n              Standstill 100, Left Fade 100, Right Fade 100, No Dip 65, Go-To 48
press_anchored_tip_ms             637.8 / 829.3 / 866.8 / 460.6 / 1959.6
press_anchored_tip_sigma_ms       73.5 / 44.1 / 46.7 / 67.1 / 52.6
meter_delay_lead_offset_ms        155
```

Phase B is **on**, and every shot type is past the `min_samples=30` floor. This is the piece that
was missing in August 9-10. The delay-conditional aim shift also already exists and is already
delay-gated (`appliedMeterDelayLeadOffsetMs()`, `AutomationEngine.cpp:10212` — returns 0 unless a
delay is actually applied). Whether 155 is the right number is unmeasured.

---

## TEST A — can the bot still time under delay?  *(run this FIRST)*

Both of the owner's asks — minimum aborts, hard tip enforcement under delay — are blocked on this
one measurement. We cannot tune an offset we have never observed with the current predictor.

1. Meter Delay **ON at 200 ms** in the UI. Confirm it actually engages — if the service logs
   `court endpoint qualified on port NNNNN which is OUTSIDE the intercept filter range`, delay is
   reporting active while doing nothing and the session is void.
2. ~40 shots, normal mix.
3. Close with the window X.

Read against the D=0 numbers above. **Grading note:** `settled_fill` bands SHIFT with delay — at
D=200 a good landing reads **81-83**, not 95-97, and a late one collapses to 50-53. Do not read
D=200 landings against the D=0 threshold; that is what makes delay look apocalyptic when it is
merely late.

- **Bot times fine** (settled clusters 81-83, aborts near the 8% we see at D=0) → go to Test B.
- **Bot fires late** (clusters 50-53) → the 155 ms offset is wrong, and this session gives us the
  size of the correction for the first time. That is a tuning fix, not a rewrite.
- **Bot mostly aborts** → the press-anchored path is not engaging under delay; that is a code bug
  and I will chase it with the session log.

## TEST B — is delay worth having at all?  *(only meaningful once A passes)*

The contested-shot question. This is the one that decides whether delay ships.

Same court, same shot, **deliberately contested** — let a defender close out on you.
~30 attempts with delay OFF, then ~30 with delay at 200 ms. Count **makes**, not meter quality.

- **Contested make% jumps** → the owner's mechanism is real, delay is a genuine competitive
  feature, and it is worth real engineering.
- **No change** → delay costs timing accuracy and buys nothing. Kill the feature, strip the UI,
  and reclaim the days. That is a good outcome too.

Keep timing quality out of this comparison — Test A already established whether the bot can time
under delay, and if it can't, B is unmeasurable.

---

## Launching with a flag (this bit bit us three times today)

`$env:X = "1"; .\run_orion.local.ps1` **from a normal shell silently does nothing.** The launcher
relaunches itself elevated (`run_orion.local.ps1:27`) and the new process does not inherit your
exported environment — the script's own comment at `:457-462` documents this trap.

Launch from an **already-elevated** terminal, one paste:

```powershell
cd C:\Users\aaron\Desktop\NexusVision; $env:FLAG = "1"; .\run_orion.local.ps1
```

Must print `Running elevated`, must NOT print `Not elevated -> relaunching`.

Verify in `logs\orion_native.log`:
- `Sidecar env keys: ...` — ground truth for which `ORION_*` keys the sidecar got.
- `Capture health: ... cap_mode=... ` — what the driver actually negotiated.

Close Orion with the **window X**. The launcher force-kills (`:492` `Stop-Process -Force`), which
is the thing that can zero `actuation_lead_ms` mid-write.

---

## NOT READY — do not test

- **Square tap (#88)** — fixed in the wrong layer, reopened. The C++ engine owns the Square bit
  (`AutomationEngine.cpp:5781-5786`). Steals still will not work.
- **`meterSettleAllowSmoothMotion`** — default OFF, conflicts with two existing guards, needs an
  offline framedump A/B.
- **`phase_veto_directional`** — 12 test pins fail.

## Ships Aug 28 regardless

`#47` capture-revocation fix · IPC + QProcess leak fixes · `0.0`-epoch velocity guard ·
port-mismatch warning · `cap_mode` + un-truncated SUSPECT diagnostics ·
meter delay **off by default** unless Test B passes · installer repackage **last**, by the 24th.
