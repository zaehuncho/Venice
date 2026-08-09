# Live confirmation batch — runbook (D6)

Purpose: one clean, fully-instrumented park session that (a) confirms the Phase-S stability fixes and
the hardened classical detection with REAL per-frame telemetry, and (b) after the locator passes the
offline gates, confirms the hybrid live. Compare against the 2026-07-02 baseline: **76% blind-fire,
green window seen in only 17% of in-shot detections, 3 stale drops, watchdog double-restart → safe mode.**

## Before you start
1. **No training running** (it owns the GPU and pegs the box — muddy timing + stutter):
   check `nvidia-smi` shows <10% GPU or ask Claude to pause/confirm.
2. One launch only — no rapid relaunches (the API rate limit + clean telemetry both want one connect).

## Launch
```powershell
.\run_orion.local.ps1 -Framedump -Detdiag
```
- `-Detdiag` = per-frame DETDIAG lines (0.04s). Verify it's LIVE by grepping the log for "DETDIAG"
  (the build marker line does NOT prove it).
- `-Framedump` = raw frame PNGs to logs\diagnostics\framedump (~30/s while capturing).

## The batch (aim ~25-35 shots, ~15-20 min)
Cover the matrix — the failure modes are type-specific:
- 8-10 Standstill (the bread-and-butter timing check)
- 6-8 Fades (left + right — the far-left slid-out meter is the edge-zone recall case)
- 4-6 Moving/off-dribble (camera-pan tracking)
- 3-4 Go-To (its abort/trust gate must still work — it never blind-fires)
- 3-4 deliberately WEIRD: shoot near the scorebug side of the screen, shoot with a red jersey
  defender close, one shot right after a cutscene ends (distractor stress).
Note (or screen-record) any shot where: the box wasn't glued to the meter, a box appeared on a
non-meter, Square failed to release, or the stream hiccuped.

## After — do these IMMEDIATELY (before any relaunch)
```powershell
Copy-Item logs\diagnostics\detframes.csv logs\diagnostics\detframes_BATCH_$(Get-Date -Format yyyyMMdd_HHmm).csv
```
(The rotation keeps 24 now, but the explicit copy is free insurance.)
Then tell Claude "batch done" — analysis is automated from there:
- per-shot lock rate + lock latency, phantom-lock rate, blind-fire %, green-in-shot %,
  release-path breakdown (feedforward vs green_confirmed), stale events, watchdog/safe-mode events,
  webcam-LED check (should be NONE), fill trajectory vs release timing per shot.

## Success bars vs the 07-02 baseline
| metric              | baseline | target        |
|---------------------|----------|---------------|
| blind-fire releases | 76%      | <5% (classical-only interim: <40%) |
| green seen in-shot  | 17%      | >90% (interim: >60%)               |
| per-shot lock       | ~50%     | >99% (interim: >85%)               |
| phantom locks       | frequent | ~0            |
| stale drops >1s     | 3        | 0             |
| watchdog restarts   | 2 + safe mode | 0        |
| webcam LED          | frequent | never         |
