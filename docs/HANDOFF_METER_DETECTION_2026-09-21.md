# Meter detection handoff — 2026-09-21 (Claude → Astra)

Owner's decision 2026-09-21 ~05:30: **meter detection is Astra's lane from here.** Everything
else (launcher, installer, updater, website, transport, packaging) stays with Claude. The owner
accepts the online timing variance; this lane is about the *detector*, not the lead.

## State of the evidence (two live sessions tonight, same rig, Park/Rec)

Session A 03:18–05:04 (old reader), session B 05:05–~05:30 (new reader). Logs:
`logs/orion_native.log` (UTC stamps), `logs/orion_user.log` (local). Grep with `tr -d '\000'`.

| metric | A (old) | B (new) |
|---|---|---|
| bot-fired shots | 68 | 6 |
| graded landings with travel < 20 pp (a false lock firing) | **0 / 54** | 0 / 6 |
| real attempts (hold ≥ 600 ms) the bot never owned | 2 of 68 | 0 |
| first-sight after press: median / p90 / max (ms) | 516 / 767 / 1499 | 500 / 700 (shots only) |
| idle "Shot meter detected" with no shot | 65 / 105 | 43 / 52 |
| STATIC ZONE quarantined / released | 15 / **5 (all false)** | 6 / **1 (`armed`, real)** |
| reads withheld by static-zone during presses | 200 (one press 1499 ms late) | 90, all in a no-shot dribble stretch |
| `staticq_repeat` (proposer re-handing the same quarantined object) | n/a | **77 / 90** |
| anchor `refused_outside_patch` on pickups | 0 on every pickup | 0; anchor conf 0.35 all session |

**Conclusions the numbers support**
1. False locks are real and visible, and **none of them fired** — every landing measured a
   meter that travelled 47–61 pp. Precision is done. Do not add refusal layers; every one we
   have added has at some point eaten a real first read (09-16: six consecutive blind presses).
2. The remaining losses are **recall and latency on the real meter**: one press whose meter was
   never proposed (A, 03:58:43, `first_sight=-1`, nothing withheld), one proposed at 902 ms, and
   a latency tail (p90 767, max 1499). The tail *is* the abort.
3. The defences cost real meters through *withholding*, and 86 % of withheld reads in B were the
   proposer handing the same quarantined object back (`staticq_repeat=77/90`). In A that
   mechanism made one real meter 1499 ms late (epoch 60: 90 reads withheld in one press).

## What Claude changed tonight (uncommitted, on disk, live in session B)

`simple_meter_reader.py`
- Static-zone **release** while idle now needs the ORDERED rise proof (`_det_lock_up_n >=
  _fresh_up_req()`, i.e. 3 consecutive reads each `_fresh_up_pp` higher, reset by any drop).
  An **armed press keeps the old first-rise release** (`lock_proved_rise_armed`). Reason: all 5
  releases in A were a single jittery read ≥ 2 pp and the spot was re-quarantined 1–2 s later.
- **Repeat-offender TTL**: a 96 px cell quarantined again after its record expired gets the
  base TTL doubled per prior quarantine (cap 8×, ledger forgets after 600 s, a real meter rising
  there clears the cell). Knob `ORION_READER_STATIC_ZONE_REPEAT_ESCALATE=0` pins it off.
- Health line: `staticq_repeat=N` and `anchor=idle/patch/refused/none/lo/mid/hi`, placed
  inside the native relay's **300-char trim** (`RemotePlaySession.cpp` `trimmed.left(300)`) —
  the proposer's `cv={}` tail is cut off in `orion_native.log` and always was.

`meter_locator_cv.py` — stats `idle_hit` (box proposed with no press armed; `_update_anchor`
is skipped on those frames by design), `anchor_none/lo/mid/hi` (armed-frame confidence
histogram against `ORION_ANCHOR_REFUSE_CONF`).

Tests: `tests/test_reader_fresh_onset_lock.py` +6 (jitter read stays quarantined; armed
first-rise escape; repeat TTL; escalation flag off; idle hits counted/anchor never evaluated
idle; repeat withholds counted; field-order pin extended). **834 pass** across every reader,
locator and anchor suite. Theater framedump replay bit-identical before/after — but **neither
offline corpus (Theater, Rec) produces a single STATIC ZONE event**, so the replay is a
no-regression check only. Only live park footage grades this.

Trap when writing static-zone tests: 30 consecutive full-frame nofinds DECAY a strike
(`_note_static_zone_absence`), so a dead-air loop lifts the quarantine on its own and the test
passes without the path under test ever running. Assert `_static_zones == []` (release) versus a
leftover `n=1` (decay).

## Ranked next steps (Claude's read — Astra decides)

1. **Grade session B properly** and the next park session: quarantine releases (expect ~0
   idle), first-sight p90 (must not exceed 767 ms), `staticq_repeat`, and the `anchor=`
   histogram (records from the next launch onward).
2. **Proposer exclusion** (if `staticq_repeat` stays high): when the reader withholds a
   quarantined candidate, `meter_locator_cv._find` should rank the *next* candidate instead of
   re-proposing the same object; fall back to the excluded one when it is the only candidate, so
   it is never worse than today. This attacks the latency tail directly. Design note: the
   quarantine lives on the reader; the locator's roaming ROI / `_last_box` keeps re-finding the
   object, and `forget_position()` alone does not change the band-scan ranking.
3. **One ~10-minute park framedump** (`run_orion.local.ps1 -Framedump`, mind the disk). The
   never-proposed case can only be diagnosed per frame with
   `tools/diagnostics/analyze_framedump.py` (gate attribution). No park footage exists offline.
4. **Latency tail → p90 < 600 ms**: locator cadence, anchor-patch miss → full band scan where a
   bigger white impostor outranks the meter on (conf, area). Anchor conf sat at 0.35 all of
   session B (< 0.75 refuse threshold) — the histogram will say whether the anchor can ever lead.
5. **Perception, not detection**: the idle flicker on white park objects is cosmetic (those
   locks never fire). Draw unproven idle locks differently (dashed) rather than refusing more.
   `ORION_READER_IDLE_PUBLISH_GATE` exists but is OFF by ship config because it once blinded a
   first read — only re-enable behind a test that pins "first read of an armed press is never
   withheld by the idle gate".

**Not on the path**: running the anchor on idle frames; any universal "abort everything that
isn't a meter" rule.

## Instruments that already exist
- `PICKUP: epoch=… first_sight_ms_after_press=… anchor_used=… anchor_conf=… refused_outside_patch=…` — one per press.
- `PRESS WITHHOLD SUMMARY: … static_zone=N ghost_static=N cold_first=N …` — one per press close.
- `Release landing: … peak_fill=… green_start=… green_end=… travel_pp=…` — the false-fire oracle
  (a static object cannot travel 47 pp).
- `DETECTOR HEALTH … staticq=zones/withheld staticq_repeat=N anchor=…` — every ~2 s.
- `tools/diagnostics/replay_simple_reader.py --session <framedump>` — offline regression numbers.

## Session C grade (12:38L-14:00L, owner's last test, graded by Claude)

Read from logs/orion_native.log(.1) after the 12:38L relaunch (sha d80be5f8 OrionStream, the
tightened static-zone release + repeat TTL reader live).

- Banner verdicts: 24 -> EXCELLENT 12 / LATE 6 / EARLY 6 (50 %). Wide open: 5 EXC / 2 LATE.
  A contested Park session (BOTHERED / LIGHT / SEMI-OPEN on 12 of 24), so not comparable to
  session B's 62 % on its own; the wide-open split is the like-for-like number.
- Reader first sight after the press (PICKUP first_sight_ms_after_press, n=78): median 500,
  **p90 836, max 1999 ms**. This is the onset the engine sees; the online game delay is ~500
  of it, and the tail above ~600 is where the engine's late misses live (see
  docs/ONSET_FEEDFORWARD_2026-09-21.md). Whether the p90 tail is the game or the reader is
  the open question for this lane: a framedump of a >800 ms first-sight press would settle it.
- Static-zone quarantine: peak health staticq=0/0, staticq_repeat=0, press_fresh_withheld=0 ->
  the tightened release did NOT withhold any real meter this session (session B withheld 166).
  Anchor counters peaked at anchor=6459/778/28/162/1142/370/1504 (idle/patch/refused/none/lo/
  mid/hi); tipless=0/6.
- GHOST LOCK DROPPED POST-PRESS: 15; PRESS WITHHOLD SUMMARY lines: 10 (ghost_static>0). No
  false fire: no landing travelled < 40 pp.
- Update check: endpoint says no manifest published (HTTP 404) - benign, launcher unaffected.
- Stuck buttons: 6 bounded r3 OUTPUT DIVERGENCE windows (41-224 ms, waiting_for_genuine_meter /
  tempo_remap_passthrough, by design), 0 watchdog fires; the duplicate-release fix was live
  (279 redundant release datagrams in the stream log).

Hand-off ask for Astra: the first-sight p90 (836 ms) is the detection-side half of the late
tail; the engine-side half (per-shot onset feedforward) is built and in Claude's lane.
