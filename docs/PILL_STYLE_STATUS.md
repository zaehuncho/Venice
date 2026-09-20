# Pill meter style — status, measurement and the rung-aware gate design

**Date:** 2026-09-17 · **Status:** MEASURED. Root cause isolated to ONE gate. No code changed.
**Scope:** measurement only. `meter_locator_cv.py` and `simple_meter_reader.py` were **not**
modified by this pass; the design below is for a follow-up to implement.
**Outputs:** `D:\NexusVision\pill_check\` · **Tools:** `tools/diagnostics/pill_*.py` (new, read-only)

---

## 0. The one-paragraph answer

Pill footage **does** exist on disk and it is 2K27. Run through it, the shipped proposer
(`ORION_METER_PROPOSER=cv`, `meter_locator_cv.MeterContourLocator`) proposes a box on
**0 of 642 Pill frames (0.0 %)** — and on **19 of 19 (100 %)** of the *Arrow2* frames that sit in
the same dataset, same park, same lighting. The cause is a single constant: gate 4's width floor.
The Pill's achromatic-bright fill column is **3 px wide @720p** (5–6 px @1080p) against a floor of
**8 px**; every other gate the Pill meets — gap-close, vertical support, the 100 px green-tip prior,
gate 9's shape and solidity — it already passes. The legacy ONNX proposer, which is still shipped
as `models/orion_meter_detector.onnx` and *is* the 08-30 Pill-trained net, reads the same frames
**661/661 (100 %)** at both 1080p and 720p. And the reader's rung-tolerant fill works on Pill inside
either box, within ~1 pp of a hand read. **The Pill is a proposer problem and nothing else.**

---

## 1. Footage found

`datasets/meter2k27_pill_park/` — built 2026-08-30 by `tools/training/label_pill_park.py` from the
owner's park/rec clips for the Pill detector retrain.

| | |
|---|---|
| frames | **774** full-frame PNG, **1920×1080** (687 train / 87 val) |
| labels | `labels/{train,val}/*.txt` (YOLO) + `audit.csv` with pixel boxes per frame |
| classes | **661 positive** (meter present, box labelled) / **113 hard negative** (no meter) |
| clips | 7 clip keys: `0945c915` `2bd92236` `2fe7bf89` `b75e982e` `23af440b` `4a9f0fca` (+`ed92bd25`, negatives only) |
| meter appearances | ~48 contiguous runs, of which **12 are shot-length** (≥15 frames; longest 149 f) |
| frame clock | per-frame contiguous indices; the replays drive the locator at 60 fps |
| game | **2K27** — park/rec court, scoreboard carries the `NBA 2K27 EARLY ACCESS` bug (`D:\NexusVision\pill_check\scoreboard_zoom.png`) |

**Per clip** (positive frames): `0945c915` 192 · `b75e982e` 179 · `2bd92236` 149 · `2fe7bf89` 93 ·
`23af440b` 29 · `4a9f0fca` 19.

> **`4a9f0fca` is NOT Pill.** Visual check (`D:\NexusVision\pill_check\clip4a9f_crop.png`): it is the
> **Arrow2** meter — chevron-notched arrow, solid wide white fill, green triangle apex — recorded in
> the same park. It is the built-in control for everything below. **True Pill = 642 positive frames.**

**Source `.mp4` clips are gone.** Searched `D:\VeniceTraining`, `D:\NexusVision`,
`D:\VeniceArchive`, `logs/diagnostics/{userclips,park_debug,pose_ds_park,meter_train}` and the repo
for the seven 8-hex clip keys: no hits. `E:` (the PS5 clip corpus) is **not mounted**. The 774
extracted frames + `audit.csv` are the whole surviving record — which is enough for detection work
and *not* enough for timing work (no presses, no releases, no banners).

### The Pill, measured (`tools/diagnostics/pill_geometry_probe.py`)

The Pill capsule is a **ladder**: a rounded translucent shell with a green dome at the apex and a
stack of bright rungs rising from the base. Measured over 300–400 GT frames:

| quantity | @1080p | @720p (what capture delivers) | shipped CV gate @720p |
|---|---|---|---|
| **achromatic-bright fill width** (V≥225, spread≤25) | **5** px (p95 6) | **3** px (p95 4) | `col_w_min` **8** … `col_w_max` 22 |
| rung height (bright run) | 11 px | 7 px (p5 5) | `min_support` 6 |
| divider height (dark gap between rungs) | 2 px | 2 px (max 2) | `gap_close` 3 (+2 ⇒ 5) |
| green apex → fill base (the capsule span) | 154 px | **102** px (p95 103) | `tip_gap` 100 ± 5 |
| fill stack height, full | 146 px | 97 px | — |
| fill stack height, onset (p5) | 21 px | **14** px | `shape_min_h` 14 |
| solidity after the shipped morphology | 0.99 | 0.99 | `shape_sol_min` 0.75 |
| gate-7 outline support | — | **0.00** (p90 0.04; 97 % below 0.45) | `outline_min` 0.0 (opt-in) |

Note the width is *physical*, not a threshold artefact: sweeping the V floor from 245 down to 150
moves the measured width from 5 px to 6 px @1080p, and relaxing `spread` to 255 moves it not at all.
The rung's bright core really is ~6 px wide at 1080p, and `INTER_AREA` to 720p averages it against
the dark shell either side down to 3.

---

## 2. Per-gate result table — where the Pill dies

`tools/diagnostics/pill_gate_autopsy.py` re-runs the shipped `_find` pipeline stage by stage with
the live locator's own constants and follows **the component sitting on the ground-truth meter**
(not whatever component happened to win the frame), so the answer is about the Pill and not about
the shooter's hoodie. All 661 GT frames:

| died at | @720p | @1080p | detail |
|---|---|---|---|
| **g4 width** (`col_w_min`) | **637 (96.4 %)** | **637 (96.4 %)** | component `w=3` against `8.0..22.0` (`w=5` against `12.0..33.0` @1080p) |
| ACCEPT | 19 (2.9 %) | 19 (2.9 %) | **all 19 are the Arrow2 clip `4a9f0fca`** |
| g3 no support | 4 (0.6 %) | 5 (0.8 %) | fully-occluded / 1-rung frames |
| g1 no bright | 1 (0.2 %) | 0 | |
| g2 gap-close | **0** | **0** | the 2 px dividers close cleanly; solidity after morphology 0.99 |
| g5 green tip | **0** | **0** | the Pill's 102 px span is inside the 100 ± 5 prior |
| g8 lone / g9 shape | **0** | **0** | never reached — the candidate dies at g4 first |

### End-to-end, through the shipped `AsyncMeterLocator(sync=True)`

`tools/diagnostics/pill_locator_replay.py` — full wrapper, real frame-to-frame history (roaming ROI,
gate-9 bridge, the four promotion pairs), 661 positive + 113 hard-negative frames.

| arm | recall on GT frames | IoU vs GT (p50) | false locks / 113 negatives |
|---|---|---|---|
| **CV, shipped, 1080p** | **19 / 661 = 2.9 %** (0/642 on real Pill) | 0.74 | 0 |
| **CV, shipped, 720p** | **19 / 661 = 2.9 %** (0/642 on real Pill) | 0.73 | 0 |
| CV, shipped, 720p, **press armed** (anchored search + onset window + tip-less path live) | **19 / 661 = 2.9 %** | 0.73 | — |
| CV + `ORION_CV_COL_W_MIN=3`, 720p | **577 / 661 = 87.3 %** | 0.74 | **0** |
| **CV + full Pill profile (§4), 720p** | **582 / 661 = 88.0 %** | **0.93** | **0** |
| **legacy YOLO proposer, 1080p** | **661 / 661 = 100.0 %** | **0.93** | — |
| **legacy YOLO proposer, 720p** | **661 / 661 = 100.0 %** | **0.93** | — |

Arming the press changes nothing: the anchored patch, the relaxed armed floors
(`ORION_CV_SHAPE_MIN_H_ARMED=8`, `ORION_CV_COL_W_MIN_ARMED=6`) and the tip-less path all sit
*behind* gate 4, and `min(6.0, col_w_min)` is still double the Pill's 3 px.

Contact sheet (yellow = ground truth, red = proposal):
**`D:\NexusVision\pill_check\pill_contact_sheet.png`** — three rows, shipped CV / CV+width-3 / YOLO.
Per-frame CSVs: `cv_1080_unarmed.csv`, `cv_720_unarmed.csv`, `cv_720_armed.csv`,
`cv_720_w3_pos.csv`, `cv_720_pillprofile.csv`, `yolo_1080.csv`, `yolo_720.csv`.

### The legacy YOLO proposer is still shipped, and it is the Pill net

`ORION_METER_PROPOSER` unset / `"yolo"` loads `models/orion_meter_detector.onnx`,
**sha256 `6af3199cdec60f49…`** — byte-identical to the `meter2k27_n3_pill/weights/best.onnx`
recorded in `docs/HANDOFF_2026-09-01_TIMING_LANE.md:1121`. It reads **every** Pill frame, including
the two held-out val clips (`23af440b` 29/29, `4a9f0fca` 19/19). The 08-30 "2.6 % → 99.7 %" claim
reproduces exactly.

### The reader's fill is fine — it is the box that is missing

`tools/diagnostics/pill_fill_check.py`, `SimpleMeterReader._measure_fill_in_box` at 720p, against a
hand read taken off the capsule's own landmarks (bright-stack top, green apex, fill base):

| frame | hand % | reader in the **YOLO** box | reader in the **CV** box (`COL_W_MIN=3`) |
|---|---|---|---|
| 56 | 2.9 | 0.0 | (refused) |
| 76 | 60.8 | 61.5 | 60.5 |
| 95 | 97.1 | 92.2 | 93.6 |
| 122 | 97.1 | 93.0 | 93.6 |
| 145 | 96.1 | 93.0 | 93.6 |
| 170 | 97.1 | 93.0 | (refused) |
| 197 | 96.1 | 93.9 | 93.6 |
| 593 | 28.2 | 32.5 | 29.1 |
| 627 | 97.1 | 93.0 | 93.6 |
| 683 | 97.1 | 93.0 | (refused) |

The 08-30 **RUNG-TOLERANT FILL** (`simple_meter_reader.py` ~:1794, `ORION_METER_RUNG_FILL=1`) is
doing its job: fill tracks the hand read within ~1 pp mid-range and ~4 pp at the top (the last rung
stops below the dome apex, so a box-relative 100 is not the apex). It is geometry-gated, not
style-gated, so it needs no Pill change at all.

---

## 3. What the product does with `meter_style = Pill` today

Four facts that together decide the ship route:

1. **`meter_style: "Pill"` is accepted and persisted.** `AppConfig.cpp:1233-1239` whitelists
   `pill`; an unknown value falls back to Arrow2, a valid one wins.
2. **The style never reaches the locator.** `meter_detector_yolo.py:872` constructs
   `MeterContourLocator()` with no arguments and the class reads no style; `SimpleMeterReader`
   keeps `_tracking_meter_style` only for the `ColorCalibrator` identity and for telemetry. The
   `contour` block in `meter_styles/Pill.json` (`w_min: 2`!) is read by nothing —
   `_load_style_config` deliberately ignores the legacy `search`/`contour` blocks.
3. **The UI hard-pins Pure CV and hides Pill.** `native_orion/qml/components/MeterConfigPanel.qml:41`
   is `readonly property bool pureCv: true` ("2026-09-10 owner: YOLO shelved"), so the Style combo
   shows only `["Arrow2"]` and is disabled. The lock is **display-only** — it does not rewrite
   `orion.meterStyle`.
4. **`meter_proposer` in `settings.json` is authoritative in production.**
   `SidecarReaderProfile.h:207-227` (`applyMeterProposerSetting`) takes the *setting* as the source
   of truth and ignores an inherited env value in a production build.

**Live hazard:** a `settings.json` carrying `meter_style: "Pill"` with the shipped
`meter_proposer: "cv"` gives a park/rec player a proposer that never sees the meter, while the UI
displays "Arrow2" because of (3). Every press in the park then fires blind. Nothing logs "the style
and the proposer disagree", because (2) means nothing in the pipeline knows they can.

---

## 4. Design — rung-aware gates, keyed on `meter_style = pill`

**Ship route A (recommended, no locator surgery): route the proposer by style.**
When `meter_style` normalises to `pill`, export `ORION_METER_PROPOSER=yolo`. The ONNX net is already
packaged, already Pill-trained, and measured here at 100 % / IoU 0.93 at both resolutions. One
change in `applyMeterProposerSetting` (or its caller in `RemotePlaySession.cpp:2294`), plus
unlocking the Style combo in `MeterConfigPanel.qml` for the YOLO case (the combo already lists Pill
and the non-`pureCv` branch already works). Cost: the detector's 8–19 ms infer instead of the CV
locator's ~0.5–5 ms, on the style where that is affordable. This keeps Arrow2/Straight on the CV
proposer **byte-identical**.

**Ship route B (if the CV proposer must own every style): the Pill profile below.**
Measured end-to-end at **88.0 % recall, IoU p50 0.93, 0/113 false locks**.

### The plumbing that does not exist yet (required by B, and by any style-keyed behaviour)

`MeterContourLocator` must learn the style. Minimal, additive, no behaviour change when absent:

```python
# meter_locator_cv.py
class MeterContourLocator:
    def __init__(self, style: str = ""):
        self._style = str(style or os.environ.get("ORION_METER_STYLE", "")).strip().casefold()
        _pill = (self._style == "pill")
        ...
    def set_active_style(self, style) -> None:      # the shim the reader already calls on the
        ...                                          # detector; re-derives the constants + reset()
```

- `meter_detector_yolo.get_locator()` passes `os.environ.get("ORION_METER_STYLE", "")`;
- `RemotePlaySession` exports `ORION_METER_STYLE` beside `ORION_METER_PROPOSER` from the same
  setting it already reads (`AppConfigData::meterStyle`);
- `SimpleMeterReader.set_active_style` forwards to the locator if it has the attribute (it already
  owns the `_tracking_meter_style` bookkeeping and already calls `reset_tracking()` on a change).

**Every constant below keeps its shipped value unless the style is `pill`.** A non-Pill install —
Arrow2, Straight, an empty style, a missing env — takes the identical code path with identical
numbers, so Straight/Arrow2 stay byte-identical by construction, not by promise.

### Per-gate: what a rung ladder fails, and the minimal relaxation

| gate | shipped @720p | Pill measured | change when `style == pill` | evidence |
|---|---|---|---|---|
| **4 — column width** ⟵ *the only binding gate* | `col_w_min` **8** (armed 6) … `col_w_max` 22 | fill core **3 px** (p5 2, p95 4) | **`col_w_min = 3`, `col_w_max = 8`, `col_w_min_armed = 3`** | alone: 2.9 % → **87.3 %** recall, 0/113 false locks. The **max** cap is not cosmetic: a Pill fill is *never* 8–22 px, so capping it is what stops the lowered floor admitting a jersey edge or a court line. |
| **4b — solidity** (`area < 0.35·w·h`) | 0.35 | **0.99** after gap-close | none | measured; the dividers are already closed before this test |
| **2 — gap close** | 3 (+2 ⇒ 5 rows) | dividers **2 px** (max 2) | none | 0 refusals. Do **not** raise it: at 8.5 px rung pitch a bigger kernel would bridge the fill stack into the empty track's 1 px rung TICKS above it and inflate `h` — i.e. inflate the fill. |
| **3 — vertical support** | 6 | rung **7 px** (p5 5) | none | 4/661 (0.6 %) losses, all occlusion. Support 6 is also what deletes the empty-track 1 px ticks — lowering it would be actively harmful. |
| **5 — green tip prior** | `tip_gap` **100** ± `tip_tol` 5 | span **102** px (p95 103, max 107) | **`tip_gap = 102.5`** (tol unchanged) | passes today with 3 px of a 5 px budget. The span is a fixed HUD element per style; re-centring the window is free and buys back the p95 tail. |
| **9 — shape floor** | `shape_min_h` 14 (armed 8) | onset fill stack p5 **14 px**; one rung = 7 px | none, **but** set `shape_min_h_armed = 7` (one rung) | costs `shape_short` on 8–14 frames (1–2 %). A Pill fill is quantised in 8.5 px rungs, so one rung is the natural floor; the armed path already requires the anchor patch + a rise pair, which a static jersey bit cannot produce. |
| **9 — straightness** (`shape_rw_cv_max` 0.35, `shape_edge_sd_max` 1.2) | 0.35 / 1.2 | a 3-px column quantises: one 4-px row gives `rw_cv` 0.33 | **`shape_rw_cv_max = 0.60`, `shape_edge_sd_max = 1.5`** | `shape_irregular` 44 (6.7 %) → 3 (0.5 %). Still refuses a ragged blob: a hoodie edge runs 0.8+. |
| **9 — tip spill ring** (green at 8–14 px from centre) | 8 … 14 px | the Pill capsule is **±9 px** @720p — the ring is **inside its own glow** | **ring = 10 … 16 px** | `tip_spill` 1–5 frames (≤0.8 %). The ring must start outside the capsule or the meter prices its own dome as spill. |
| **8 — lone column** (`lone_gap` 20 px) | 20 | the shooter's white hoodie sits 12–20 px off a 3 px column | **`lone_gap = 11`** | `not_lone` 95 → 79; recall 85.6 % → **88.0 %**, still 0/113 false locks. A *twin glyph* is by definition about the candidate's own size; consider additionally requiring `kw <= 3·w` in the twin predicate (untested here — measure before shipping). |
| **7 — outline support** (opt-in, `outline_min` default 0.0) | 0.45 when enabled | **0.00** median, 97 % below 0.45 | **force `outline_min = 0.0` for pill** | the gate's bands sit at `x+1..x+6` / `x+w-6..x+w-1` of a 26 px Arrow2 box; on the Pill those land on the dark track, not the shell. If anyone ever flips `ORION_CV_OUTLINE_MIN=0.45`, the Pill dies outright. |
| **box geometry** (not a gate) | `box_w` 26, `box_pad_top` 4, `box_pad_bot` 3 | capsule ~20 px wide; box measured **+6.0 px wide, −5.1 px short at the bottom**, top +0.7 px, centre-x ±0.0 | **`box_w = 20`, `box_pad_bot = 8`** | IoU p50 **0.74 → 0.93** — the same box quality as the ONNX detector. This matters downstream: the box height is the reader's fill denominator, and the engine's rung table and tip constant were learned on the YOLO-shaped box. |

**Composite, measured end to end** (`pill_locator_replay.py --scale 0.66667`, all knobs at once):

```
ORION_CV_COL_W_MIN=3  ORION_CV_COL_W_MAX=8  ORION_CV_TIP_GAP=102.5
ORION_CV_SHAPE_RW_CV_MAX=0.6  ORION_CV_SHAPE_EDGE_SD_MAX=1.5
ORION_CV_BOX_W=20  ORION_CV_BOX_PAD_BOT=8  ORION_CV_LONE_GAP_PX=11
  -> 582 / 661 = 88.0 % recall,  IoU p50 0.93,  0 / 113 false locks
```

(These env knobs exist today and were used to *measure* the profile. The shipped change should be
the style-keyed defaults above, not an env pin — a packaged install inherits no environment.)

### Why this must be style-gated and not global

`tools/diagnostics/pill_falselock_probe.py` on **session_20260912_201355** — the owner's own court,
the white FEVER jersey + scoreboard + shot-chart set gate 9 was written against — 700 sampled frames:

| arm | proposals | `col_cands` | `not_lone` |
|---|---|---|---|
| shipped | 12 / 700 (1.7 %) | 4,919 | 3,099 |
| `ORION_CV_COL_W_MIN=3` globally | 15 / 700 (2.1 %) | **8,164** | **5,560** |

`+3` proposals on frames the shipped gates were refusing on purpose, and **+66 % candidate churn per
frame** — i.e. a real per-frame CPU cost on the style that does not need the relaxation. Keyed on
`meter_style == pill`, Arrow2/Straight never execute a different line of code.

### What is still unproven (do not claim these)

- **Timing.** These clips carry no presses, no releases and no banners, so nothing here says the
  Pill's *fill ruler* or the engine's rung table is right on Pill. Recall and box geometry are all
  that has been established.
- **Live false-lock rate on a rec court.** 113 hard negatives from these clips is a floor, not a
  census. The 2026-09-12 probe above is Arrow2 pixels, not Pill pixels.
- **The `kw <= 3·w` twin rule** in gate 8 is proposed, not measured.
- **Low fill.** Recall at fill < ~15 % is where the remaining 12 % of refusals live
  (`no_tip` 79, `not_lone` 79, `shape_short` 8) — exactly the frames a press needs. The anchored
  search would normally carry them, and it could not be exercised here (no nameplate/press data).

---

## 5. What the owner needs to record (≈5 minutes)

Even on route A, a live Pill session is required before Pill ships — the dataset above settles
*detection* and settles nothing about *timing*.

**In 2K27:** Settings → Controller Settings → **Shot Meter → style = Pill**, Shot Meter **ON**,
meter colour left at the default (White). Go to **MyCourt or an empty Park court**.

**Launch:** the normal dev launch line with the **press-window dump** enabled, so every press writes
its own frame window plus the press/release records (the same artefacts
`tools/diagnostics/nometer_locator_replay.py` and `nometer_window_forensics.py` already consume:
`shot_records` + `press-window JPEGs` + the frames CSV with `t_wall` / `detected`).

**Take, in one sitting:**
- **~20 Standstill three-pointers** — stand still, shoot, let the meter run to green. Vary distance a
  little (top of the key, corner, one or two from beyond the arc).
- **~10 fades** — 5 left, 5 right. Fades are where the meter rides up the screen and where the
  09-15 `no_tip` failures lived; they are the ones that matter.
- Do not change the style, the HUD scale or the camera mid-session.
- **~5 minutes total.** If it is convenient, take 5 more shots in **Rec** (a real crowd + other
  players' white jerseys beside the meter is the false-lock case these clips are thin on).

**Also useful, 30 seconds:** one clip of the empty Pill capsule with **no shot** (stand still, meter
off) — pure negatives for the false-lock census.

**What the tools will need** — nothing new. The harness is already written:

```
tools/diagnostics/nometer_locator_replay.py     press-window replay through the real locator
tools/diagnostics/pill_locator_replay.py        recall / false-lock census + contact sheet
tools/diagnostics/pill_gate_autopsy.py          per-gate autopsy on the meter's own column
tools/diagnostics/pill_geometry_probe.py        the capsule's measured geometry
tools/diagnostics/pill_fill_check.py            reader fill vs hand fill in each proposer's box
tools/diagnostics/pill_falselock_probe.py       what a relaxation costs on non-Pill footage
```

`pill_*` read `datasets/meter2k27_pill_park` today; pointing them at a new session needs only the
frame directory and a box source (the press-window records supply both).

---

## 6. Reproduce

```bash
.venv/Scripts/python.exe tools/diagnostics/pill_geometry_probe.py --scale 0.66667
.venv/Scripts/python.exe tools/diagnostics/pill_gate_autopsy.py   --scale 0.66667
.venv/Scripts/python.exe tools/diagnostics/pill_locator_replay.py --proposer cv   --scale 0.66667
.venv/Scripts/python.exe tools/diagnostics/pill_locator_replay.py --proposer yolo --scale 0.66667
.venv/Scripts/python.exe tools/diagnostics/pill_locator_replay.py --proposer cv --scale 0.66667 \
    --env ORION_CV_COL_W_MIN=3,ORION_CV_COL_W_MAX=8,ORION_CV_TIP_GAP=102.5,\
ORION_CV_SHAPE_RW_CV_MAX=0.6,ORION_CV_SHAPE_EDGE_SD_MAX=1.5,ORION_CV_BOX_W=20,\
ORION_CV_BOX_PAD_BOT=8,ORION_CV_LONE_GAP_PX=11
.venv/Scripts/python.exe tools/diagnostics/pill_locator_replay.py --proposer cv --scale 0.66667 --cls neg --env ...
.venv/Scripts/python.exe tools/diagnostics/pill_fill_check.py --n 10 --env ORION_CV_COL_W_MIN=3
.venv/Scripts/python.exe tools/diagnostics/pill_falselock_probe.py \
    --dir "D:\NexusVision\framedump\session_20260912_201355" --stride 7 --limit 700
```

All outputs land in `D:\NexusVision\pill_check\`. Nothing writes to the repo, nothing touches a
running `OrionNative.exe`, and no git state was changed.

---

## 7. 2026-09-19 landmark ruler — the Pill fill is measured on the capsule, not on the box

**Status:** IMPLEMENTED, measured offline. **Not deployed, not committed, no live session yet.**
**Outputs:** `D:\NexusVision\pill_check\`

### 7.1 The defect

With `meter_style = Pill` the sidecar routes to the YOLO proposer (§4 route A) and
`SimpleMeterReader._measure_fill_in_box` measures the fill on a **box-relative** scale
(0 = box bottom, 100 = box top). That is right for Arrow2 — `meter_locator_cv`'s contour box is
fitted to the white column itself — and wrong for Pill, whose box is a **regression** carrying
~8.5 px of pedestal below the capsule's fill base and ~7 px of cap above the green apex at 720p
(~16 px on a ~102 px true span):

```
reader_in_YOLO_box  ~=  7.5 + 0.88 * hand          (measured, 720p AND 1080p)
```

Both an **intercept** and a **slope** error. The tip-phase anchor is the 20 % crossing, so on this
ruler 20 % happens at a true fill of ~14 % (the command goes early); and the box edge's jitter enters
the fill directly — on a **frozen** meter the box ruler swings **5.9 pp peak-to-peak (p50)**.

### 7.2 The ruler — `pill_fill_ruler.py`

The SAME measured fill edge, re-expressed against the capsule's own landmarks, all found inside the
same proposer box:

```
BASE  bottom row of the base-touching bright fill stack (divider gaps <= max(2, 0.025*bh) bridged)
APEX  top row of the connected green make-window cap - the exact row green_end already comes from
TOP   the fill edge the reader already found (rung-tolerant ladder block; sub-pixel when available)

fill = S * (base - top) / (base - apex)          S = ORION_PILL_RULER_SCALE, default 96.0
```

`S = 96` is what makes the ruler **equivalent to Arrow2's**: Arrow2's frozen `green_end` reads 96
and *is* the green dome top, so the Pill apex lands exactly where Arrow2's does and every constant
tuned on Arrow2 (the 20 % anchor, the learned rate, the aim at ~96) transfers unchanged. Independently
re-measured here on the Arrow2 dump `session_20260918_162245`: **green_end p10 95.52 / p50 96.11 /
p90 96.60 over 1745 frames.** The green band is re-expressed on the same ruler
(`[ORION_GREEN_SCALE_UNIFY]`).

### 7.3 The latch — and the 3-frame latch that was built first and MEASURED TO FAIL

The numerator is two image measurements, so box jitter already cancels out of it: over the ep46
box-breathing episode of `session_20260917_050219` (box 114 -> 153 -> 113 px on a frozen meter)
`base - top` holds at **exactly 100 px on every frame** while the box ruler reads 91.2 -> 74.1 -> 96.5.
Only the denominator (apex->base span) has to be latched. The literal spec — *median of the first 3
clean frames, re-latch on a >15 % jump* — was built first and **failed on this session**: at ep35 one
frame's cap measured a span of 125 px (>15 % out), which cleared the latch, and the three re-seeding
frames all landed inside the same box-breathing episode, so it re-latched at **111 px and held that
wrong scale for TEN consecutive shots** (green_end read 88.2 instead of 96.0). A second failure
(ep46) came from a cap that had run off the box's **top edge** and measured `apex = 0` on seven
consecutive frames, confirming a re-latch to 144 px and 15 frames of a 23 pp error.

What ships instead, both changes measured:

* the latch is the **median of a rolling window** of clean spans (`ORION_PILL_RULER_WINDOW`, default
  180 frames = 3 s) — still a median-of-3 for the first three frames, so never a first-frame latch;
* a cap on the box's top edge, or one implying a span outside the latch band, is **not a span
  measurement** and cannot teach the latch (2.3 % of replay frames / 19.3 % of live-box frames);
* a re-latch needs `ORION_PILL_RULER_RELATCH_N` (8) *consecutive* out-of-band measurements;
* the span is carried across a lock boundary (`ORION_PILL_RULER_CARRY`, default on): a press is not
  a new meter, and the shipped session ruler already measured that re-seeding per shot is worse;
* **fail-open everywhere**: no core band, no base, no cap, an implausible span, an exception -> the
  caller's existing box-ruler triple is returned unchanged.

Result: the latched span reads **102.0 px on every one of the 46 rises**, both arms.

### 7.4 Measured

**(a) Ground truth, `tools/diagnostics/pill_fill_check.py --n 40`** (`datasets/meter2k27_pill_park`,
hand ruler = the capsule's own apex->base span). Residual vs the target ruler `0.96 * hand`, frames
with hand in [5, 97.5]:

| scale | ruler | med | mean abs e | sd | max abs e | slope | intercept | within +/-1.5 pp |
|---|---|---|---|---|---|---|---|---|
| 720p (`--scale 0.66667`), n=38 | BOX (shipped) | 0.47 | 1.59 | 2.09 | 6.77 | 0.888 | **7.14** | — |
| | **LANDMARK (new)** | -0.01 | **0.51** | 0.64 | 1.86 | **0.966** | **-0.08** | **36/38** |
| 1080p (`--scale 1.0`), n=39 | BOX (shipped) | 0.35 | 1.71 | 2.29 | 6.19 | 0.889 | **6.82** | — |
| | **LANDMARK (new)** | -0.17 | **0.60** | 0.88 | 3.14 | **0.958** | **0.18** | **36/39** |

Target was `0.96 * hand +/- 1.5 pp, no intercept, no slope error`: **met** (slope 0.958-0.966 vs 0.96,
intercept <= 0.18 pp). The residual scatter that remains is the hand ruler's own edge convention
(`mx>=225 & (mx-mn)<=25`) vs the reader's (`V>=200 & S<=65`) disagreeing by 1-3 anti-aliased rows.

**(b) 60 fps Pill dump `D:\NexusVision\framedump\session_20260917_050219`** (7829 frames, 53 press
episodes), replayed with `tools/diagnostics/pill_ruler_replay.py`. Two independent readers over the
same frames, differing only by `ORION_PILL_RULER`; **their boxes agreed on 7829/7829 frames**, so
every pairing below is exact. The table is the `--boxes live` arm (the box stream the shipped session
actually served, from `frames.csv`).

| metric (per rise) | BOX ruler | LANDMARK ruler |
|---|---|---|
| fill velocity 36-40 %, p50 (IQR) | 0.144 (0.034) | **0.173 (0.021)** |
| 20 % crossing shift vs BOX, p50 | — | **+16.5 ms** (p25 7.5 / p75 37.8) |
| frozen `green_end`, p50 (IQR) | 95.54 (1.01) | **96.00 (0.000)** |
| frozen `green_end`, p10 / p90 / sd | 93.86 / 96.88 / 3.20 | **96.00 / 96.00 / 0.000** |
| latched span | — | 102.0 px on all 34 rises |

**Frozen-meter jitter** — frames where the capsule's true pixel fill height (`base - top`) is constant
to +/-1 px, so every movement in the reported fill is ruler noise (20 rises, median 85 frames each):

| | p25 | p50 | p75 |
|---|---|---|---|
| BOX ruler sd(fill) | 1.17 | **1.27** | 2.05 |
| LANDMARK sd(fill) | 0.27 | **0.41** | 0.46 |
| BOX peak-to-peak | 4.52 | **5.92** | 12.17 |
| LANDMARK peak-to-peak | 0.94 | **0.94** | 0.94 |

0.94 pp is exactly one pixel row (96/102) — the landmark ruler's residual jitter on a frozen meter is
the edge quantisation and nothing else.

The same dump driven through the reader's OWN proposer/tracking (`--boxes replay`, 46 rises) gives
velocity 0.162 -> 0.172 pp/ms and a 20 % crossing shift of **+22.6 ms** (p25 15.2 / p75 30.0).

**Not achieved:** the velocity **IQR** does not collapse to Arrow2's 0.003 (0.034 -> 0.021). Measured
directly, sigma_y about a line over the engine's commit band (fill 15-40 %) is **BOX 0.625 vs NEW
0.648** — unchanged. The rise-band scatter is not ruler error; the box ruler's damage is concentrated
where the box breathes (plateau and fades), which is exactly where the frozen-meter table shows it.

**(c) Arrow2 no-change proof** — `D:\NexusVision\framedump\session_20260918_162245` (3414 frames,
Arrow2, CV proposer, 2167 detected, max fill 91.9), replayed through the **pre-change** reader
(a pristine copy of `simple_meter_reader.py` with the hook removed, put on `sys.path` ahead of the
repo) and the **patched** reader, comparing `(detected, fill, bbox, green_end, rejection, velocity)`:

```
frames compared: 3414   FRAMES THAT DIFFER: 0
```

The two-arm `ORION_PILL_RULER` A/B on the same session is also 3414/3414 identical on every field.

**(d) Tests** — `.venv/Scripts/python.exe -m pytest ... --basetemp=D:/NexusVision/pytest_tmp/pill`

| suite | result |
|---|---|
| `tests/test_pill_fill_ruler.py` (new) | **22 passed** |
| every `grep -l -i pill tests/*.py` + `test_ship_defaults.py` + `test_shipped_reader_defaults.py` + `test_simple_meter_reader.py` + `test_reader_acquisition_flags.py` + `test_simple_reader_lock_lifecycle.py` + `test_green_zone_window.py` + `test_sidecar_bundle_manifest.py` + `test_security_audit.py` | **663 passed** |

### 7.5 Files changed, and the knobs

| file | change |
|---|---|
| `pill_fill_ruler.py` | **new** (498 lines) — the ruler, the latch, the landmark measurement |
| `simple_meter_reader.py` | **12 lines, one location**: `_measure_fill_in_box`, lines **8876-8887**, immediately after the `_measure_fill_in_box_legacy` call. Guarded by `self._tracking_meter_style == "pill"`; nothing else in the file was touched |
| `tests/test_pill_fill_ruler.py` | **new** — 22 tests incl. the ep35 and ep46 regressions and the non-Pill no-op |
| `tools/diagnostics/pill_fill_check.py` | third column (new ruler) + the residual/slope/intercept scoring table |
| `tools/diagnostics/pill_ruler_replay.py` | **new** — the A/B replay, the live-box paired mode, and the `--single` / `--ref-dir` / `--compare` before/after recorder used for (c) |

| env knob | default | meaning |
|---|---|---|
| `ORION_PILL_RULER` | `1` | master switch; `0` restores the shipped box ruler byte-for-byte |
| `ORION_PILL_RULER_SCALE` | `96.0` | `S` — where the green apex lands (Arrow2's frozen `green_end`) |
| `ORION_PILL_RULER_WINDOW` | `180` | frames of clean spans the latch takes its median over |
| `ORION_PILL_RULER_RELATCH` | `0.15` | out-of-band fraction (the sub-pixel ruler's 15 % rule) |
| `ORION_PILL_RULER_RELATCH_N` | `8` | consecutive out-of-band measurements that confirm a rescale |
| `ORION_PILL_RULER_CARRY` | `1` | carry the measured span across a lock/press boundary |

### 7.6 Open / unverified

1. **SHIP BLOCKER — the packer.** `scripts/build_orion_sidecar.ps1` passes an explicit
   `--include-module` list to Nuitka; `pill_fill_ruler.py` is imported *inside a function*. The
   manifest (`tools/sidecar_bundle_manifest.py`) already binds every repo-root module, so the build
   identity is correct, but **`--include-module=pill_fill_ruler` should be added** or a compiled
   sidecar may silently fail the import and fall back to the box ruler (the hook is fail-open, so it
   would be a silent feature-off, not a crash). Not changed here — outside this pass's scope.
2. **No press timeline.** `logs/orion_native.log.1` has rotated past 2026-09-17 (it now starts
   2026-09-18T00:14Z), so the Pill dump was replayed with the CV self-arm and `_physical_shot_epoch`
   stayed 0: the **per-press lock/carry path is exercised only by unit tests**, never against the
   live dump.
3. **`cap_ok = 0` on 19.3 % of live-box frames** (2.3 % in the replay-box stream) — the live box is
   much larger than the capsule, so the measured cap often implies a span outside the latch band. The
   fill is unaffected (it uses the latch); the green band on those frames is pinned top-at-`S` with
   its measured width. Worth watching in the first live session.
4. **The engine constants are unchanged.** The ruler moves the Pill 20 % anchor ~17-23 ms later, so
   the owner's Pill Shot Lead should go back up toward the Arrow2 value (~269) — **this has not been
   measured live and is a projection from the crossing shift.**
