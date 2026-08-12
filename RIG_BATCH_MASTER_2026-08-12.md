# Venice — Master Rig Batch

**Supersedes `RIG_BATCH_2026-08-11.md`.** Ship: 2026-08-28. Branch `fix/timing-input-and-remoteplay-blockers`, HEAD `04a60c2`, all pushed.

---

## The one number this whole batch is chasing

321 graded landings, every session on disk:

| green window width | n | bad (settled<90) | bounce >=5pp |
|---|---|---|---|
| 0-3 pp | 134 | **5.2%** | 3.0% |
| 3-6 pp | 69 | 17.4% | 10.1% |
| 6-12 pp | 59 | 42.4% | 52.5% |
| 12+ pp | 59 | **96.6%** | 78.0% |

**"Inconsistent tip timing" and "meter bounce backs" are ONE problem.** Both track how cleanly the
reader resolves the green band. When it's clean the bot is 95% good and never bounces. When it
smears, it fails 97% of the time. **37% of shots (118/321) are in the 6pp+ danger zone.**

Things measured and RULED OUT as the cause, so we don't chase them again:
- **The predictor.** Already re-estimates a median of **13 times per shot** and commits only ~89 ms
  before fire. A pure reactor couldn't commit later than ~85 ms. Switching architectures buys ~4 ms.
- **Latency.** Capture staleness is p50 **9.4 ms** (n=1658), not the 227 ms I once believed.
- **Prediction accuracy.** Phase lands 78.3% when it can see. It's fine.

Everything below attacks **what the reader can see.**

---

## HOW TO SET AN ENV FLAG SO IT ACTUALLY ARRIVES  *(read this once; it bit us)*

**`$env:X = "1"; .\run_orion.local.ps1` from a NORMAL shell silently does nothing.** The launcher
checks elevation at `run_orion.local.ps1:27` and, if you are not admin, relaunches *itself* with
`Start-Process -Verb RunAs`. That is a **brand-new process that does not inherit your exported
environment** — only the `-Framedump` / `-Detdiag` switches survive, because they are passed as
arguments. The script's own comment at `:457-462` documents this exact trap biting a previous
framedump batch. Your variable dies with the unelevated shell and the app runs on defaults.

So: **launch from an already-elevated terminal.** Right-click Start → *Terminal (Admin)*, then

```powershell
cd C:\Users\aaron\Desktop\NexusVision
$env:ORION_CAPTURE_MJPG = "0"     # or whichever flag this session needs
.\run_orion.local.ps1
```

The launcher prints `Running elevated -> WinDivert packet capture available.` and must **not**
print `Not elevated -> relaunching as Administrator`. If you see the relaunch line, the flag is
gone — close and start over from an admin terminal.

**Two independent confirmations, both in `logs\orion_native.log`:**

1. `Sidecar env keys: ...` — ground truth for which `ORION_*` keys the sidecar process received
   (`RemotePlaySession.cpp:2294-2303`, names only, never values). Your flag must appear here.
2. `Capture health: ... cap_mode=1920x1080@60/YUY2/buf1 ...` — what the **driver actually
   negotiated**, not what we asked for. `ORION_CAPTURE_MJPG=0` does not *request* YUY2; it just
   skips the fourcc call (`capture_card_backend.py:694`) and takes the driver default, which may
   still be MJPG, or may be YUY2 at a **dropped frame rate** because uncompressed 1080p60 is
   ~250 MB/s. Either outcome voids the session, and neither is visible without this field.

Tell me once you are connected and I will check both before you spend 40 shots.

---

## RULE: one variable per session

Every session: **same shot count (~40), same shot mix** (mostly Standstill, some fades, a few
Go-To), **same court position**. Close with the **window X** — never Task Manager, a kill zeroes
`actuation_lead_ms`.

Before each session, confirm the competitor is not running (it held the Elgato on 2026-08-11 and
produced 205 MSMF grab failures):
```powershell
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like "*Helios*" }
```
Should print nothing.

Tell me after each one and I'll run the analyser before you start the next. **Do not stack two
variables** — if a combined run doesn't help, we won't know which lever failed, and that's how a
week gets burned.

---

## S1 — YUY2 instead of MJPG  *(free, no rebuild)*

We currently feed the detector **MJPG 4:2:0** — chroma at half vertical resolution — on a green cap
a few pixels tall. The code's own comment says YUY2 (4:2:2) gives a "sharper fill-top/green tip".

**From an ADMIN terminal** (see the launch section above — this is not optional):
```powershell
$env:ORION_CAPTURE_MJPG = "0"
.\run_orion.local.ps1
```
Connect, let me confirm `cap_mode=`, then ~40 shots.

- **PASS:** the 6pp+ share drops below 37%.
- **WATCH:** stutter. YUY2 at 1080p60 is uncompressed (~250 MB/s over USB). If the preview stutters
  or fps drops, that IS the result — say so and we revert.

---

## S2 — Feed the detector 1080p  *(new, `01d7856`)*

The card negotiates 1920x1080 and we downscale to 1280x720 before detection — while
`simple_meter_reader` declares its pixel priors **at 1080p** (`_REF_W,_REF_H = 1920,1080`) and
scales them *down* to whatever we hand it. We discard a third of the resolution, then shrink the
reader's native-resolution priors to match.

**From an ADMIN terminal:**
```powershell
Remove-Item Env:\ORION_CAPTURE_MJPG -ErrorAction SilentlyContinue   # back to default
$env:ORION_DETECTOR_1080P = "1"
.\run_orion.local.ps1
```
Connect, let me confirm `cap_mode=` reads `1920x1080`, then ~40 shots.

- **PASS:** same as S1 — 6pp+ share drops.
- **WATCH:** detector fps / preview backpressure. 2.25x the pixels per detect.

---

## S3 — Both together  *(only if S1 or S2 helped)*

```powershell
$env:ORION_CAPTURE_MJPG   = "0"
$env:ORION_DETECTOR_1080P = "1"
```
~40 shots. If both helped individually, this is the configuration we'd ship.

---

## S4 — METER DELAY, measured properly for the first time

**This is the important one, and it is not the test anyone has run before.**

Every previous attempt asked "can the bot fire under delay?" — answer no, it aborts, because the
required lead (445) exceeds the schedulable ceiling (~386). That question is settled and it made
the feature look dead.

But the code's only actual claim for delay is **legibility**, not time
(`MeterDelayController.h:9-11`: *"gives the vision reader a cleaner meter to read"*). A cleaner
meter is a **narrower green window** — which is the entire consistency problem above.

**So we measure green-window width under delay. The bot does not need to fire.** The reader emits
green geometry on every frame regardless of whether a shot is scheduled, which sidesteps the
chicken-and-egg completely.

1. Turn **Meter Delay ON at 200 ms** in the UI.
2. Play ~40 shots as normal. **Expect aborts — that is fine and expected. Ignore them entirely.**
   We are not measuring whether it fires. We are measuring what the reader SEES.
3. Close normally.

- **PASS:** the green-width distribution shifts left vs delay-off — shots move out of 6pp+ into
  0-3pp.
- **FAIL:** distribution unchanged. Then delay has no mechanism at all and we kill the feature,
  strip the UI, and spend the remaining days on S1/S2. **That is a good outcome too** — it retires
  a two-week rabbit hole in one session.

> If S4 passes, delay becomes worth real work — and *then* the reactive-fire design in
> `METER_DELAY_PLAN_2026-08-12.md` §8 matters, because reactive is the only way to exploit a
> cleaner-but-later meter. Not before. There is also an unresolved contradiction inside that plan
> (its §2.5 says firing to the detached green lands late; its §8.3 says delay self-cancels — both
> cannot be true) which S4 partly informs.

---

## S5 — Is the aim actually drifting?  *(free, no code)*

The max-out plan wants a new "phase-learner drift clamp". Before building it, test whether drift is
real using the freeze that **already ships**.

Arm the Tip-Timing **lock** in the UI (that latches `tip_phase_aim_frozen`), then play a **long**
session — **60+ shots**. Drift is a tail-of-session effect; 20 shots won't show it.

- **PASS:** tail-of-session lates/aborts stop growing → drift is real, the clamp is justified, and
  we know its size.
- **FAIL:** no change → the drift hypothesis is wrong and we just saved building it.

---

## NOT READY — do not test these yet

- **Square tap (#88).** My fix went into the WRONG LAYER and is reopened. The C++ engine owns the
  Square bit (`AutomationEngine.cpp:5781-5786` — `TempoSquare` deliberately converts Square into a
  right-stick gesture and strips the button; that IS tempo remap). The Python fix in `73f8fcb`
  cannot reach the console on your rig. **Steals will still not work.** Don't re-test it and don't
  count it as fixed until the C++ side lands.
- **`meterSettleAllowSmoothMotion`** — default OFF, and it conflicts with two existing guards.
  Needs an offline framedump A/B, not a live session.
- **`phase_veto_directional`** — confirmed twice: 12 test pins fail, risks unbounded no-release.

---

## Opportunistic, any session

If capture stalls, tell me — I'll grep for `capture_route_recovered_cold`. That line exists only on
the #47 ship-blocker recovery path, and seeing it would be the first real-hardware proof the fix
works. I have only been able to unit-test it.

If meter delay ever reports active while doing nothing, the service now logs
`WARNING court endpoint qualified on port NNNNN which is OUTSIDE the intercept filter range` — that
was previously a completely silent no-op that could burn a whole tuning session.

---

## What ships on Aug 28 regardless of every result above

- `#47` capture-revocation fix (landed, unit-tested, wants real-hardware confirmation)
- IPC thread/HANDLE leak + sidecar QProcess leak (landed)
- `0.0`-epoch velocity guard (landed)
- Port-mismatch warning (landed)
- Meter delay **disabled by default** unless S4 passes
- Installer repackage — **last**, by the 24th. It is the only item with no slack.
