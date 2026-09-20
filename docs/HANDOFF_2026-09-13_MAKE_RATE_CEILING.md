# Make rate: the "80 % ceiling on open shots" is refuted — and where the makes are

Date: 2026-09-13. Instrument: the game's own feedback panel via `tools/timing/panel_grade.py`
(the only validated grader), plus a new per-shot meter-timeline tool
(`tools/timing/meter_trajectory.py`, 60 fps detframes CSV). Every number below is from the
banner or from the capture, never from an engine self-grade.

## 1. Where the 80 % came from, and why it is not a ceiling

The figure is **11/14 open shots** in ONE in-game session (09-12 20:13 local,
`session_20260912_201355`). Wilson 95 % CI for 11/14 is **52 %..92 %**. The whole session was
18/26 = 69 % (CI 50..83). No ceiling can be read off n=14.

Same rig, same build, same frozen aim (403.3), two hours earlier in a drill
(`session_20260912_145457`, actually 19:16-19:39 local): **40/44 = 91 %** (CI 79..96).
The ButtonShot portion of that drill was **14/14**; Standstill **21/22**. Fisher drill vs game
p=0.045; drill vs game-open p=0.34 (n too small to separate).

So the pipeline demonstrably produces 90 %+; the in-game session lost makes to identifiable
causes (section 3), not to a precision floor.

## 2. What the engine and the capture say about the misses

Per-shot engine variables (phase sample, hold->release, fill@release) are IDENTICAL on lates
and greens (game session: EXCELLENT sample 335.5 ms / h2r 101 / fill 37.9; red LATE 331 /
102 / 37.8; white LATE 334 / 100 / 39.7). Reproduces the 09-03 finding.

The meter's own timeline, from the 60 fps detframes (ms after the 20 % crossing, ButtonShot):

| quantity | greens (n=18) | note |
|---|---|---|
| command - t20 | 135, sd 2.9 | the engine fires at a consistent anchor-relative time |
| t40 / t60 / t80 / t90 | 111 / 209 / 297 / 346 | Standstill t80 sd ~3 ms without the hitch shot |
| freeze - command | 215, core sd ~4 | the only post-command witness; white lates 214/3.0 = normal |
| freeze fill | 90.6, sd 1.4-2.4 pp | reader ruler noise; does NOT separate verdicts |

Drill (Standstill, ButtonShot, n=8): t80 299 sd 2.3, freeze-cmd 217 sd 4.1 -> 14/14 green.
The Gaussian core in-game is the same size as in the drill. The in-game loss is tails.

Per type (t80 after t20): Standstill 299 (n=22), Left Fade 290 (n=7), Right Fade 282 (n=3);
drill LF 294 (n=2), RF 299 (n=2). Fades' meters reach 80 % ~8-10 ms before Standstill's.
Built-in trims (`tip_phase_type_trim_enabled: true`, map absent -> defaults LF -4, RF -6) are
LIVE (`PHASE SAMPLE ... type_trim_ms=-4.0/-6.0`) and cover about half of that.

## 3. The 8 misses of the game session, one by one

| seq | type | verdict | coverage | what the timeline shows |
|---|---|---|---|---|
| 3 | Right Fade | yellow LATE | SOLID CONTEST | whole meter ~8 % faster (t40 102, t80 275): a different/faster animation under heavy contest |
| 10 | Standstill | white LATE | SEMI-OPEN | everything normal (slight late) |
| 12 | Standstill | white EARLY | WIDE OPEN | everything normal; command even 9 ms later than usual |
| 18 | Left Fade | red LATE | LIGHT CONTEST | normal LF timeline (t80 290) |
| 22 | Standstill | red EARLY | LIGHT CONTEST | meter **47 ms SLOWER** (t80 ~344) plus a -4.7 pp box jump mid-rise; command at the normal time -> 47 ms early |
| 25 | Standstill | red LATE | BOTHERED | normal timeline; meter bounced 5.4 pp (~25 ms overshoot) |
| 27 | Standstill | white LATE | OPEN | everything normal (slight late) |
| 28 | Standstill | red LATE | WIDE OPEN | at +79 ms after the command the fill jumped 58->72 in ONE frame (4 frames of animation) and the box grew 121->136 px; bounced 11.5 pp. Game-side hitch / animation swap; capture had no drops (frame_no contiguous, dup 0 %) |

Reading: 2 game-side animation anomalies (22, 28), 2 fades under contest (3, 18), 3 Standstill
lates of which 2 contested (10, 25, 27), 1 slight early (12). Open shots without the hitch:
11/13 = 85 %, misses symmetric and slight.

Pooled over both graded sessions: **9 LATE vs 3 EARLY** (binomial p=0.15). A late is a certain
miss in 2K27; an early inside the window is not. The aim sits closer to the tip than the
asymmetry warrants.

## 4. Refuted today — do not chase

* **Post-command transport jitter.** Fork log (22:40 local session): queue_wait - transport
  p50 31 us, p99 346 us, max 571 us. Freeze timing on the white lates is normal (214 ms, sd 3).
* **Capture-phase lock (Experiment C).** Capture frame arrival phase vs a 60 Hz grid: |residual|
  p50 2.5 ms, p90 4.5 ms; the engine reports coherence 0.30, lock=0 on every shot. The phase is
  not observable on this rig, so the console-tick quantization term stays.
* **Sampler / curve stretch / anchor consensus.** The timeline is deterministic to ~3 ms, so
  there is nothing to refine on normal shots; the one slow-meter shot (#22) had only ~4 pp of
  true deficit at the command instant, coincident with a 4.7 pp box jump. No safe trigger, and
  the only useful correction direction (delay) is the one that turns a good shot into a late.
* **Tempo mode.** 2K27 (Mike Wang, 2KIntel): Rhythm and Button "tuned to have no clear
  advantage"; matching tempo widens the window, bad tempo shrinks it. The bot's untuned
  TempoSquare drilled 26/30 vs 14/14 Button in the same drill. Medium-term only.
* **"Aborts on a clear meter".** Post-shape-gate (09-12 22:34 local onward): 67 releases,
  3 aborts (4.5 %): two `ownership_proof_incomplete` (one deflating ghost first_fill 82.6, one
  single-sample) and one `detector_authority_lost`. The 83 `press_unanswered_no_meter` lines in
  the game session are NOT shots: 79/83 had no shot panel within 2.5 s (steal taps etc.), and
  50/59 game panels with no engine release had no Square press within 3 s (teammates' shots).

## 5. Levers, ranked

1. **Disk. 12 GB free of 931 GB.** The game session logged `FRAMEDUMP backpressure: dropped=20860`
   and the detframes/framedump are disabled below the floor, so the post-gate sessions have NO
   ground truth at all. The five graded framedump sessions hold 30 GB and their
   `panel_grade.csv` files are written; archiving them restores measurement. (Owner's call.)
2. **Aim placement, measured not guessed.** One in-game session with
   `run_orion.local.ps1 -Framedump -AllowTimingOverrides` and
   `ORION_DEV_FIRE_OFFSET_SWEEP=list:-8,-4,0,4` (cycled per shot). `panel_grade.py` now joins
   the `Release devoffset:` line and prints green / late / early BY OFFSET. Set the Tip Timing
   card to the offset where lates vanish before earlies appear. Expected +2-3 pp overall, more
   on contested shots (10/26 of in-game shots; 60 % vs 79 % open).
3. **Fade trims.** Fades' meters are 8-10 ms ahead of Standstill's; the live trims are -4/-6.
   Candidate: `"tip_phase_type_trim": {"Left Fade": -6, "Right Fade": -8}` in settings.json
   (clamp +/-9; edit with the app CLOSED, then re-sign; a file that carries the map REPLACES the
   defaults). Or read the by-type x by-offset split from the sweep session first.
4. **Contest** is where the makes go (contested 60 %). The game draws COVERAGE only after the
   shot, so a contest-aware aim needs a real-time defender detector (post-launch). The sweep in
   (2) is the only global response available now.
5. **Game-side animation anomalies** (2/26) are not reachable from the PC. Log them, don't tune
   on them.

## 6. Commands

```
.\.venv\Scripts\python.exe tools\timing\panel_grade.py logs\diagnostics\framedump\<session>
.\.venv\Scripts\python.exe tools\timing\meter_trajectory.py logs\diagnostics\detframes.csv <t_lo> <t_hi> logs\diagnostics\framedump\<session>\panel_grade.csv
```
`<t_lo>/<t_hi>` are ISO UTC bounds of the session in `logs/orion_native.log` (local = UTC-5);
detframes.csv rotates to `detframes_<stamp>.csv` at the next launch.
