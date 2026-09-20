# Player-anchor acquisition, 2026-09-19

Owner-visible question: *"when I'm holding Square there is always a meter on screen — read
it 100 % of the time, no false locks, no late reads."* This is the acquisition half of that:
does `player_anchor.py` find the owner's own nameplate on every press, so that everything
gated on the anchor (the relaxed low-fill floors and gate-9 abstain inside the patch, the
range classifier, the pose lock) can actually run?

Corpus: the five 09-17 / 09-18 press-window dumps (60 fps JPEG, per-session directories),
**145 presses**, joined to their own `shot_records/*.jsonl`. Tooling:
`tools/diagnostics/anchor_acquire_study.py`, `anchor_pickup_study.py`,
`shot_range_confusion.py`. Everything below is measured on those dumps; nothing is inferred
from a live session.

---

## 0. The brief's premise was stale, and the honest starting point is higher

The task was written against the 2026-09-17 measurement: *"the PS-disc correlation sits
around 0.519 against `ORION_ANCHOR_PS_MIN` 0.58 … locks on only ~2-34 % of presses."* That
was the state **before** the `ORION_ANCHOR_ACQUIRE` pass (cropped core template, wide scale
ladder, half-res coarse pass, per-row template sizing, scaled dy, identity-taught offsets),
which is already in the working tree. Re-measured on the five dumps with the repo exactly as
found, the oracle-confirmed PS correlation is **p50 0.82-0.91**, not 0.519, and the anchor
already locks **131/145 = 90.3 %** of presses.

So this pass is not "fix a broken anchor". It is: find what the remaining 14 presses have in
common, fix that, and prove nothing moved on the false-lock side.

---

## 1. Acquisition — before / after

`anchor_acquire_study.py` replays each press window frame by frame through the real
`PlayerAnchor` on the dump's own clock, with `ARM` armed, and scores it against an **oracle**
that does not come from this module: a wide multi-scale disc sweep seeded at the plate the
*production detector's own* meter box (from `frames.csv`) predicts, then propagated forwards
and backwards. `locked` = the anchor's plate lands within 40 px of the oracle plate inside
600 ms of the press; `in time` = it does so inside 150 ms.

`plate visible` is the honest denominator: a press where the oracle can seed no plate at all
(`no_plate`) has no ground truth — on those the anchor cannot be scored, not "fails".

| dump | presses | plate visible | **locked before** | **locked after** | of visible before → after | in time (≤150 ms) before → after |
|---|---|---|---|---|---|---|
| session_20260917_050219 (Pill, owner court) | 53 | 48 | 45 (84.9 %) | **48 (90.6 %)** | 93.8 % → **100.0 %** | 38 (71.7 %) → **45 (84.9 %)** |
| session_20260917_142445 (Arrow2) | 18 | 18 | 18 (100 %) | **18 (100 %)** | 100 % → 100 % | 18 (100 %) → **18 (100 %)** |
| session_20260918_135725 (fade drill) | 36 | 35 | 32 (88.9 %) | **34 (94.4 %)** | 91.4 % → **97.1 %** | 27 (75.0 %) → **31 (86.1 %)** |
| session_20260918_152024 (fade drill) | 17 | 17 | 16 (94.1 %) | **17 (100 %)** | 94.1 % → **100.0 %** | 13 (76.5 %) → **17 (100 %)** |
| session_20260918_162245 (fade drill) | 21 | 21 | 20 (95.2 %) | **20 (95.2 %)** | 95.2 % → 95.2 % | 19 (90.5 %) → **20 (95.2 %)** |
| **all five** | **145** | **139** | **131 (90.3 %)** | **137 (94.5 %)** | 94.2 % → **98.6 %** | 115 (79.3 %) → **131 (90.3 %)** |

Failure histogram, all five dumps:

| reason | before | after |
|---|---|---|
| `ok` | 131 | **137** |
| `not_in_topk` (the plate is there, at the right scale, over the floor — the coarse ranking dropped it) | 6 | **0** |
| `identity` (a plate was locked, not the owner's) | 2 | **2** |
| `no_plate` (the oracle itself cannot seed — no confirmed meter box in the window) | 6 | 6 |

Every dump is at or above the 80 % bar on raw presses (90.6 / 100 / 94.4 / 100 / 95.2 %) and
at 95-100 % of the presses that have ground truth at all.

### 1.1 What `not_in_topk` actually was

`_coarse_map` already sizes its template **by the row** (the plate rides at the player's feet,
so its apparent size is a function of `y`: `scale ≈ SCALE_A + SCALE_B·y`). That is exactly why
the *global* top-k could not rank across the band: the top strip's template is about half the
bottom strip's, and a small template's `TM_CCOEFF_NORMED` is high on anything.

Measured on the failing presses (`session_20260918_152024` seq 8, `session_20260917_050219`
seq 9/18/23, `session_20260918_135725` seq 2/15) — the global top-10 coarse peaks and the true
plate, on one frame each:

```
152024 seq 8, press+21 ms, oracle plate at (353,580), coarse value 0.551
  global peaks: 0.68@(1260,335) 0.67@(908,313) 0.66@(532,393) 0.64@(652,387)
                0.63@(1182,335) 0.63@(404,323) 0.62@(1216,335) 0.62@(1192,303) ...
  -> the true plate is not in the global top-16.  Per-strip top-3: rank 3 overall,
     rank 1 in its own strip.
050219 seq 9, press+32 ms, oracle plate at (114,580), coarse 0.485
  global: 0.69@(730,323) 0.69@(104,365) 0.67@(760,321) ...   per-strip: rank 5
135725 seq 15, press+59 ms, oracle plate at (340,588), coarse 0.610
  global: 0.80@(38,261) 0.77@(124,261) 0.72@(88,261) ...      per-strip: rank 3
```

Every competing peak sits at `y` 257-470 — the crowd, the stands, the scoreboard, and every
*far* player's nameplate (drawn small because the player is far). The owner's own plate, low
in the band at `y` 578-620, scores 0.46-0.61 and never reaches the refine pass.

**Fix:** take the coarse peaks **per y-strip** instead of globally — the same
`ORION_ANCHOR_COARSE_TOPK` budget, divided across the strips `_coarse_map` already uses
(`ceil(topk / strips)` each, suppression still applied in the parent map so a peak on a seam
is not taken twice). No extra refine work; the candidates simply stop all coming from the
crowd. `not_in_topk` 6 → 0.

### 1.2 What `identity` was, and what is left of it

`_track` re-locks on **disc correlation alone**. An acquisition that opens on a teammate's
plate is therefore carried for the whole press by the cheap path: on
`session_20260918_135725` seq 14 (a far Left Fade, plate scale 0.65, the owner's plate at
coarse rank 5) the anchor locked a neighbour 15 ms after the press and tracked it for **157 of
167 frames**.

**Fix:** once the owner's gamertag is known, a *run* of tracked frames the tag refuses
(`tx < ORION_ANCHOR_TX_MIN`) drops the track, so the next acquisition can re-rank with
identity — the term that picks the owner out of five nameplates. A run and not one frame: the
ball, a crossing player or a scale wobble costs a single frame's correlation on the owner's own
plate too. Instrumented as `PlayerAnchor.stats["id_drop"]`.

**It ships OFF (`ORION_ANCHOR_TRACK_ID_MISS=0`) because it bought nothing measurable.**
Every one of the +6 locks came from the per-strip change; the run rule added zero, did not
close either real `identity` press, cost acquisitions (163 -> 243 on
session_20260917_050219, anchor max 12.6 -> 16.0 ms), and is the arm in which the confident
false-lock count moved 0 -> 2. The mechanism, the knob and the tests stay; turning it on
needs a `TX_MIN` that actually separates two plates and a fresh refusal run.

**It did not close the two `identity` presses on the real dumps, and the report does not
claim it did.** On a real court an impostor's gamertag still correlates ~0.4-0.6 against the
owner's learned crop, i.e. above the 0.35 `TX_MIN` ranking floor, so the run rule rarely
fires on them. It does fire where the plate genuinely changes (7 drops over 348 armed frames
on session_20260918_152024, with patch-contains-the-live-box still 137/137 = 100 %). Raising
`TX_MIN` would catch the remaining two but also gates `_note_confirmed` and the confidence
that earns the right to refuse — not a change to make without its own false-lock run.

Remaining, after: 2 `identity` (135725 seq 14, 162245 seq 21 — both far fades with a
0.65-scale plate) and 6 `no_plate`.

### 1.3 False locks — unchanged

`anchor_acquire_study.py --refusal` re-runs the 09-15 false-lock population on the 09-12
reference dump: 12 000 frames, "junk" = a box the live detector accepted on a frame more than
2.5 s from any press in the session's press table.

All four cells of the 2×2 were run, because neither knob's effect here is what it looks like
from the combined arm alone:

| per-strip | run rule | junk boxes | in a patch | in a **confident** patch | refused | refused (confident only) | real boxes in patch | confident frames |
|---|---|---|---|---|---|---|---|---|
| 0 | 0 | 766 | 131 | **0** | 82.9 % | **100.0 %** | 36.8 % | 1090 |
| **1** | **0** — **the shipped 09-19 default** | 766 | 145 | **0** | 81.1 % | **100.0 %** | 32.9 % | 451 |
| 0 | 3 | 766 | 131 | **0** | 82.9 % | **100.0 %** | 36.8 % | 1090 |
| 1 | 3 | 766 | 133 | **2** | 82.6 % | 99.7 % | 70.2 % | 1093 |

The number that matters is the **confident** column: an anchor may only REFUSE a candidate
outside its patch once `conf ≥ ORION_ANCHOR_REFUSE_CONF`. **The shipped default holds the
invariant the 09-15 study shipped on — 0 of 766 junk boxes inside a confident patch, exactly
as before.** The 14 extra junk boxes land in *non*-confident patches, which no gate acts on: a
non-confident anchor may help the search and may never veto.

Three things the decomposition says that the combined arm hid, and that correct an earlier
reading of it:

* **The run rule alone is a no-op on this corpus** — row 3 is byte-identical to row 1 on every
  column, confident-frame count included. It does not, by itself, cost a single refusal.
* **The 2 confident junk boxes and the 70.2 % real containment are an INTERACTION, not either
  knob.** Neither alone produces either number. So "the 70.2 % belongs to the run rule" is
  wrong; it only exists with both knobs on, which is not what ships.
* **The shipped default reaches refuse-confidence far less often on THIS corpus: 451 confident
  frames against 1090.** That is new and is not claimed as a win. It is safe-direction for
  false locks (fewer vetoes cannot create one, and the locator's own gates are unchanged), but
  it does weaken the anchor's veto on the 09-12 park court, and the raw in-patch containment
  there drops 36.8 % → 32.9 %. On the five 09-17/09-18 dumps this lane actually targets the
  same measurement moves the other way — patch containment 0.828 → 0.971 on 135725, 0.909 →
  0.952 on 152024, with `outside_patch_accepts` 0 in every arm (§3) — so the ship stands on
  those, and the 09-12 number is recorded as the cost rather than rounded away.

---

## 2. Range — measured, and still `unknown`

`shot_range.py` reads the nameplate's left `[3]` cell at the plate the anchor found, 40-120 ms
after the press, and ships `ORION_SHOT_RANGE_CALIBRATED=0` because the 2026-09-17 matrix scored
38.5 % on 26 joined presses. The stated blocker was the anchor: *"player_anchor locked the
owner's plate on 2 of 360 pickups."*

That blocker is gone. The live sampler has now written `range_cells` on **102** presses that
also carry the game's own `banner.distance_ft` — a 4× corpus, six sessions
(`tools/diagnostics/shot_range_confusion.py --live-records`). Label: `three` at **≥ 22.0 ft**
(the corner three, i.e. the shortest shot that can be behind the arc; the tool takes
`--arc-ft`, and 23.75 ft moves no conclusion because there is no separation at any cut).

```
label \ verdict    three   mid   unknown
three (>= 22 ft)      79     9       0
mid                   14     0       0        agreement 79/102 = 77.5 %

three   precision 0.849 (79/93)    recall 0.898 (79/88)
mid     precision 0.000 (0/9)      recall 0.000 (0/14)

three   dark p10/p50/p90 0.284/0.493/0.561    bright 0.253/0.355/0.446
mid     dark p10/p50/p90 0.423/0.536/0.568    bright 0.251/0.338/0.412
```

**Mid recall is zero.** The 77.5 % is the 88:14 class imbalance of three-point drills, not a
classifier. The two populations do not separate on the statistic at all — the `mid` rows are,
if anything, *darker* than the `three` rows — and the nine `three → mid` calls are plate
misreads (`dark 0.0` with `bright` 0.0 / 0.816 / 1.0), not an icon that was absent.

The reason is now visible instead of inferred. The nameplate strips were cut at the plate the
anchor found for the sub-arc presses that still have frames on disk
(`D:\NexusVision\anchor_acq_2026-09-19\plates`):

| press | banner distance | plate as drawn |
|---|---|---|
| 162245 seq 19 | 17.2 ft | `[3][PS][trimuzis]` — the "3" cell is drawn |
| 162245 seq 18 | 19.7 ft | `[3][PS][trimuzis]` |
| 162245 seq 20 | 20.1 ft | `[3][PS][trimuzis]` |
| 135725 seq 36 | 21.1 ft | `[3][PS][trimuzis]` |
| 162245 seq 2 | 21.2 ft | `[3][PS][trimuzis]` |

On the owner's court the cell is drawn on shots the game itself scores at **17 ft**. Whatever
it marks there, it is not "behind the arc at the press". No threshold on that ROI can produce
`mid`, and a better ROI geometry is not the fix either — the icon is present, at the pitch the
module already uses.

**So `shot_range` stays uncalibrated and keeps reporting `range=unknown`.** Arming it would
ship a constant `three` into `lead_offset_fade_mid_ms`. The path stays wired and recording;
the lead the module's own docstring already named is the only one still standing: the banner's
own DISTANCE is an exact label one shot late, which an engine-side trim can key on without any
cell read.

---

## 3. Pickup — before / after (measured, and it barely moved)

`anchor_pickup_study.py --mode on --top-strip 1` replays each dump straight through the real
`meter_locator_cv.MeterContourLocator`, publishing the press window exactly as
`simple_meter_reader` does, with the anchor driving the search. **Both arms run the same
locator and the same floors**; the only difference is the anchor, `before` =
`ORION_ANCHOR_COARSE_PER_STRIP=0 ORION_ANCHOR_TRACK_ID_MISS=0`, `after` = both on.

> Caveat, stated up front: the `after` arm here was measured with the run rule at 3. It now
> ships at 0 (1.2), so the `after` column is an upper bound on what the shipped default does,
> not the shipped default itself.

| dump | first-sight fill p50/p90 | first sight ms after press p50/p90 | outside-patch accepts | patch contains the live box |
|---|---|---|---|---|
| 050219 (Pill) | 15.0/18.2 -> 17.0/18.6 | 651/793 -> 740/811 | 0 -> 0 | 31/55 -> 23/45 |
| 142445 | 15.5/99.8 -> 15.0/**71.1** | 774/844 -> 769/**827** | 0 -> 0 | 0.544 -> 0.529 |
| 135725 | 16.0/70.2 -> 16.0/**61.2** | 800/914 -> 797/**897** | 0 -> 0 | 0.828 -> **0.971** |
| 152024 | 15.0/16.0 -> 15.0/16.0 | 816/870 -> 815/870 | 0 -> 0 | 0.909 -> **0.952** |
| 162245 | 15.0/16.2 -> 16.0/75.2 | 851/949 -> 852/1115 | 0 -> 0 | 0.746 -> **0.800** |

**What this says, plainly: the acquisition fix did not measurably move first sight.** The p50
fill is flat (15-16 %) and the p50 latency is flat (770-850 ms) on every dump. The p90 tail
improves on three of the four Arrow2 dumps and gets worse on 162245. What it does move is
**patch containment** — the anchor's predicted patch holds the live meter box on 97.1 % of
armed frames on 135725 against 82.8 % before, 95.2 % against 90.9 % on 152024 — which is the
precondition for the relaxed in-patch floors to fire, not the fill number itself.

`outside_patch_accepts` is **0 on every dump in both arms**: no accepted box fell outside a
confident anchor's patch.

Two things this table is NOT:

* **050219 is not measurable with this instrument.** That session is the Pill meter style and
  `MeterContourLocator` accepts a box on 3 of 53 shots there (27 accepts in 7 829 frames;
  per-frame recall 0/118 on frames where the live dump had a plausible box). The Pill fill
  path is another agent's lane this week. Its fill/ms cells are noise.
* **It is not a STALE LOCK measurement.** `loc.stats["stale_lock"]` is not populated by this
  harness on any arm, so no before/after claim is made about stale locks.

**Nothing in the Arrow2 fill measurement changed.** The edits are confined to `player_anchor.py`
— which plate is found and where its patch is — plus a docstring in `shot_range.py` and the
three diagnostics tools. `simple_meter_reader.py`, `meter_locator_cv.py` and every fill,
sub-pixel, ruler and green-cap path are untouched; the 1 113 passing tests below include every
suite that imports `simple_meter_reader`.

---

## 4. Files changed

| file | lines | what |
|---|---|---|
| `player_anchor.py` | 112-136 (docstring: the two new knobs + the 09-19 WHY) | documentation of the knobs below |
| | 467, 471-473, 483 | `_id_miss` run counter, `stats["id_drop"]` |
| | 616-645 | `ORION_ANCHOR_TRACK_ID_MISS`: a run of gamertag refusals drops the track |
| | 1190-1233 | `ORION_ANCHOR_COARSE_PER_STRIP`: coarse peaks taken per y-strip |
| `shot_range.py` | 77-110 (docstring) | the 2026-09-19 re-measurement; verdict stays `unknown` |
| `tests/test_player_anchor_acquire.py` | 468-646 | 12 new tests: per-strip peaks + identity-vs-track, both kill switches |
| `tests/test_shot_range.py` | 309-376 | 6 new tests: the measured mid population is a regression guard against arming |
| `tools/diagnostics/anchor_acquire_study.py` | 126-147, 691-710 | per-session dump directories; presses whose `framedump` block never got attached are recovered from the `ep<N>_` file names; `--session a,b,c` |
| `tools/diagnostics/anchor_pickup_study.py` | 63-79, 88-103 | the `ep<N>_f<idx>_<d>_raw.jpg` press dumps and `--presses <session>.jsonl` |
| `tools/diagnostics/shot_range_confusion.py` | 84-146, 163-186 | `--live-records`: the matrix from the recorded cells + banner DISTANCE |

**Not touched:** `meter_locator_cv.py` (the anchored-search gates needed no change — the fix
is upstream, in which plate the anchor hands them), `simple_meter_reader.py`, anything under
`native_orion/`.

### Env knobs (new)

| knob | default | meaning |
|---|---|---|
| `ORION_ANCHOR_COARSE_PER_STRIP` | `1` | take the coarse peaks per y-strip rather than globally. `0` restores the 09-17 global pick exactly. |
| `ORION_ANCHOR_TRACK_ID_MISS` | `3` | consecutive tracked frames the learned gamertag may deny before the track is dropped and re-acquired. `0` disables; inert until an identity exists. |

Both are read per call through `_fenv`, so a live toggle needs no restart, and both are pure
kill switches: setting them to `0` reproduces the measured "before" column above.

---

## 5. Could not verify

* ~~The `ORION_ANCHOR_TRACK_ID_MISS`-only refusal arm was still running at hand-back.~~
  **Closed**: it finished, it is a byte-identical no-op against the baseline, and all four
  cells of the refusal 2×2 are now in §1.3. Nothing in the refusal picture is unmeasured.
* **Pickup on session_20260917_050219 is not measurable** with `MeterContourLocator`: it is
  the Pill style and the locator accepts on 3 of 53 shots there.
* **STALE LOCK** is not populated by `anchor_pickup_study.py`, so no before/after claim is
  made about it.
* **The pickup `after` column was measured with the run rule at 3**, which now ships at 0, so
  it is an upper bound on the shipped default rather than the shipped default itself. The
  acquisition table (§1) is unaffected: the run rule contributed 0 locks there.
* **`shot_range` was not armed.** The deliverable asked for `mid`/`three` instead of
  `unknown`; the measurement (§2) says the cell cannot produce `mid` on this court, so the
  verdict stays `unknown` and the matrix is reported as the reason.
* The two remaining `identity` presses (135725 seq 14, 162245 seq 21) and the six `no_plate`
  presses are unfixed and unexplained beyond §1.2.

## 6. Tests

```
tests/test_player_anchor.py + test_player_anchor_acquire.py
  + test_shot_range.py + test_shot_range_clock.py
  + test_ship_defaults.py                            158 passed  (+13 acquire, +6 range)
tests/test_ship_defaults.py + every test that
  imports simple_meter_reader (52 files)             1121 passed, 16 skipped, 0 failed
```

(`test_ship_defaults.py::test_the_rest_of_the_native_ship_list_is_where_the_launch_line_left_it`
failed mid-pass on `bannerTrimBiasVotes` while the concurrent native lane had it at `0`; it is
green again in the final run.)

All runs used `--basetemp=D:/NexusVision/pytest_tmp/anchor`.
