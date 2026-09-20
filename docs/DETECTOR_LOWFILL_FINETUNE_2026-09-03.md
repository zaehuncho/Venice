# Meter detector low-fill fine-tune (2026-09-03)

**Status (independent recheck 2026-09-04): needs changes.** n4 was already promoted to
`models/orion_meter_detector.onnx` before this audit; no model or threshold was changed here.
The complete negative-pool comparison and signed production package gate remain open.
Offline detector/occlusion evidence does not establish live human-banner shot success.

## 2026-09-04 independent evidence correction

The fresh, same-pool CUDA comparison uses 461 held-out positives and 2,072 negative
images (1,420 gameplay + 652 ineligible). At confidence 0.20 / 0.25 / 0.35,
n3 recalls 448 / 446 / 444 positives with 0 / 0 / 0 false positives;
n4 recalls 461 / 461 / 461 with **3 / 2 / 1 false positives**. Thus the narrower
1,420-negative table below is not a clean result on the full pool and does not satisfy
the handoff's equal-or-better false-positive criterion. The surviving 0.35 false positive
is a park billboard timer, not a meter.

With the exact production reader profile and DirectML, repeating that static image
for 60 frames produces positive fill on 58 unarmed / 59 simulated-armed frames with
n4 (n3: zero). In the **21 distinct recorded surrounding frames at original cadence**,
both models produce zero positive fills: the isolated detection does not establish a
lock there. This establishes a static-input robustness issue, **not a demonstrated native
false fire**. No native controller or gameplay calls were made.

The run still contains only **9 completed training epochs** (best checkpoint epoch 7);
the previous resume log stopped during epoch 10. The 25-epoch recipe below is not proof
of completed training. Independent QC found 100/100 sampled positive boxes to be real
meters across 23 sessions. Numeric fill buckets are estimated, not human fill truth.
Four held-out sessions are disjoint from training; train/validation share 19 sessions
but no shots. Eleven negative rows have generation-ambiguous timestamp joins; 16 menu
negatives fail the no-press +/-1.5-second criterion; three held-out sessions lack
historical press-log coverage. The 2,072 evaluated images are therefore not 2,000
fully time-qualified no-shot gameplay negatives.

Historical session 151910 epoch 86 was incorrectly called a non-shot: its original
images contain a real shooting meter. Current n4 recovers that sequence (first positive
fill about 15.74% at +471 ms; n3 produces none), but this is a training-session case,
not new held-out accuracy evidence. No 95% shot-success claim follows from it.

Reproducible evidence: `.codex_artifacts/venice_20260904/detector_eval_current.csv`,
`detector_eval_current.log`, `detector_audit.md`, and `timing_audit.md` in that directory.
Keep thresholds and shipped models unchanged until a frozen candidate passes a broader
session-disjoint evaluation. The existing signing workflow is still required for
StrictSecurity to complete its package stage.

## TL;DR

* The reported blind spot reproduces exactly: through `MeterYoloLocator` (same 960-px FP16 ONNX,
  same letterbox, CUDA EP or CPU EP) `f02830_0..f02832_0` of session_20260903_151910 return
  **None at conf 0.05**, `f02833_0` 0.86, `f02478_0` 0.17. The live sidecar runs the CUDA EP
  (`DETECTOR HEALTH provider=CUDAExecutionProvider infer=34-40ms`).
* But when the same live-missed frames are mined across **24 framedump sessions** (450 verified
  frames between the meter's appearance and the pipeline's first positive), the shipped model
  detects **418/450 of them offline** at conf 0.35. Every one of the **32 real misses is a meter
  over a parquet/wooden floor** (151910's practice court, 141000's purple-trim park court), at ANY
  fill (17 of the 32 are lingering feedback meters at ~50 %). On the MyCourt (blue) sessions the
  offline recall is 100 % in every fill bucket, so those live `detector_no_meter` frames are the
  async detector's staleness (one 34-40 ms inference + hand-off, i.e. ≤1 dumped frame at the ~9 fps
  dump cadence), not a model gap. The real blind spot is **court-specific, not fill-specific**, and
  it is not a threshold problem (every real miss has NO box at conf 0.05).
* Fine-tune result: n4 recovered every held-out propagated miss at the retained 0.35 threshold
  and stayed clean on all 1,420 held-out negatives.  See the reproducible tables below.

## 1. What was measured first (premise check)

| frame (session_20260903_151910) | shipped n3 @conf 0.05, CUDA EP | CPU EP |
|---|---|---|
| f02830_0 (meter just appeared, 0 %) | None | None |
| f02831_0 (~15 %) | None | None |
| f02832_0 (~30 %) | None | None |
| f02833_0 (~45 %) | 0.856 | 0.857 |
| f02834_1 (79 %) | 0.740 | 0.741 |
| f02477_0 | None | None |
| f02478_0 | 0.172 | 0.170 |
| f02479_1 (52 %) | 0.580 | 0.583 |

Shipped ONNX facts (from the file's metadata): `imgsz [960,960]`, FP16 input (`quantize: 16`),
opset 18, static batch 1, ultralytics 8.4.116 -- training was at 1280 but the export (and therefore
live inference, since the locator takes imgsz from the model) is 960. The new model is exported the
same way (plus a 1280 variant for comparison).

## 2. Hard-example mining (`tools/training/mine_lowfill_hardset.py`)

Per shot (= run of `detected=1` frames in `frames.csv`):

1. Take the first detected frame's box. Find the two silver **outline rails** in it (per-column
   median luminance over rows above the fill, local peaks 14-30 px apart with the green cap between
   them) and re-cut the box to rails ±4 px horizontally and cap-top −3 px / 120 px tall vertically
   (the pipeline's own boxes are often offset or over-wide by up to 10 px; the mode of their
   height is 119-121 @720p).
2. Build a **masked NCC template** from that box: keep the ring, arrow top + cap and bottom
   chevron, blank the interior where the fill level changes.
3. Walk **backwards** through the preceding `detected=0` frames (`detector_no_meter`,
   `roi_not_found`, …): masked NCC search ±120/±80 px around the previous box (the meter follows
   the shooter); fall back to green-cap blob proposals; re-centre on the cap centroid; lock with a
   ±4 px rail search and the cap top.
4. **cv2 verification** of every propagated box: a compact green cap in the top 30 %, centred,
   not a stripe crossing the box (the practice court has green light streaks that fooled the first
   pass); a white column (S≤40, V≥220, width ≥30 % of the box, standing on the base, near-constant
   row width) OR — for 0 % frames where the white is still hidden inside the bottom chevron — a
   row-wise rail test (both rails brighter than the track interior on ≥50 % of rows) plus NCC
   ≥0.50; cap position within 4 px of the template's. If the template frame has no measurable
   rails (bright parquet/grey floors: no luminance contrast), only white-column evidence is
   accepted. Stop at the first failing frame.
5. Detected frames with `fill_pct < 35` are kept too (`detected_lowfill`, pipeline box verified).
6. Negatives: gameplay `detected=0` frames >1.5 s from any detection and from any
   `Physical shot epoch` in `orion_native.log`, frame-wide cap+column guard clean; plus (held-out
   FP pool only) guard-checked `gameplay_ineligible` frames (menus/park/broadcast).

Fill estimate: `fill_pct ≈ 128.2·(white_rows/box_h) + 3.4`, fitted on 6,083 detected frames of
all fills (residual median 2.7 pp). Frames with no visible white column land in the 0-10 bucket.

Every threshold was calibrated by contact-sheet review (`--calib`); the last review of 48 random
accepts, 40 template boxes, and the held-out and older-session propagated frames found no wrong
box.

### Data

Sessions (all 1280×720 Arrow2): training = session_20260903_{133124,145810,151910} + 17 sessions
from 2026-08-31..09-02 recorded with the same shipped model; held-out (never trained on) =
session_20260903_100538, session_20260901_141000, session_20260902_115728,
session_20260902_195735.

| split | propagated (live-missed) | detected_lowfill | negatives |
|---|---|---|---|
| train | 284 | 1,128 | 434 |
| val (by shot, model selection only) | 34 | 215 | 0 |
| heldout | 132 | 329 | 1,420 |
| heldout_inel (menus/park, FP only) | – | – | 652 |

Propagated fill buckets (fill_est): 0-10: 336, 10-20: 49, 20-30: 5, 30-50: 8, 50+: 52. The
10-30 % buckets are thin by construction: the framedump runs at ~9 fps (114 ms), the meter fills
at ~0.2 %/ms, and the pipeline locks at a median first fill of 16-26 %, so a dumped frame rarely
lands inside that window before the lock. The two 30 fps sessions (0831_114137, 0831_131603)
gave nothing: the detector locked at 0 % fill there.

Output: `runs/detect/logs/diagnostics/meter_train/lowfill_hardset/{images,labels}/{train,val,heldout,heldout_inel}`
+ `manifest.csv` (source frame, box, fill_est, csv_fill, NCC, rail fraction, mode, old_detected).

## 3. Where the shipped model actually fails (offline, conf 0.35, IoU ≥ 0.30)

| session (court) | propagated n | old misses |
|---|---|---|
| s151910 practice court, parquet + grey floor (train) | 52 | **15** |
| s141000 purple-trim park, parquet (held-out) | 21 | **17** |
| 20 MyCourt-blue sessions | 377 | 0 |
| all `detected_lowfill` frames (1,672) | – | 2 |

## 4. Fine-tune (`tools/training/finetune_meter_lowfill.py`)

Init = shipped `meter2k27_n3_pill/weights/best.pt` (best.pt-as-init, not resume); data =
`datasets/meter2k27_combo_lowfill.yaml` = original Arrow2 + Pill combo (2,358+687 train images,
nothing dropped) + the hard set train/val; 25 epochs, batch 8, imgsz 1280, AdamW lr0 2e-4, cosine,
no flips/rotations, mosaic 0.3, hsv 0.015/0.5/0.4 (the n3 recipe at a lower LR); patience 12;
seed 1234. Run dir `runs/detect/logs/diagnostics/meter_train/meter2k27_n4_lowfill/` (new; the
shipped dir is never written). Export: `format=onnx imgsz=960 quantize=16 simplify=True batch=1`
→ `weights/best.onnx` (drop-in), plus `weights/best_1280.onnx`.

## 5. Results

Evaluation used the runtime `MeterYoloLocator` preprocessing/postprocessing, IoU >= 0.30, the
960-pixel FP16 ONNX exports, and CUDA EP.  The 2,122-positive pool includes train, validation,
and held-out material; the separately reported 132-frame propagated result is held-out only.

| model / threshold | all positives | held-out propagated | held-out detected-lowfill | false positives |
|---|---:|---:|---:|---:|
| n3 / 0.20 | 2,092 / 2,122 | 119 / 132 | 329 / 329 | 0 / 300 |
| n3 / 0.35 | 2,088 / 2,122 | 115 / 132 | 329 / 329 | 0 / 300 |
| n4 / 0.20 | 2,122 / 2,122 | 132 / 132 | 329 / 329 | 2 / 1,420 |
| n4 / 0.25 | 2,122 / 2,122 | 132 / 132 | 329 / 329 | 1 / 1,420 |
| n4 / 0.30 | 2,122 / 2,122 | 132 / 132 | 329 / 329 | 1 / 1,420 |
| **n4 / 0.35 (release)** | **2,122 / 2,122** | **132 / 132** | **329 / 329** | **0 / 1,420** |

The promoted file is 5,460,707 bytes with SHA-256
`1e3886af40fa06ad4efe2b81bf9c44eadf7895ac549dfc25cf102137aba3c772`.

### Deterministic partial-occlusion gate

`tools/training/eval_meter_occlusion.py` cross-checks each manifest box against the materialized
held-out YOLO label, paints deterministic nearby-ring-median occluders, then grades the runtime
locator at confidence 0.35 and IoU 0.30.  On 461 held-out images / 8,759 variants:

| occluded GT area | recall |
|---:|---:|
| 0% | 461 / 461 (100.00%) |
| 10% | 2,736 / 2,766 (98.92%) |
| 20% | 2,648 / 2,766 (95.73%) |
| 30% | 2,629 / 2,766 (95.05%) |

Every non-top placement scored 98.92-100%.  Top-strip masks are the hard case (93.93% at 10%,
75.70% at 20%, 72.89% at 30%), so the runtime reader now treats a trajectory-lagging broad white
edge as top-censored only when the pixels above that edge also show direct foreground-mask
evidence.  A current positive locator can bridge it for at most 120 ms; one fresh
full-negative frame can bridge for at most 45 ms only with a same-shot/same-lock/structure-proven
rise and at least 50% broad bottom-connected meter support.  Recovered frames never extend their
own authority, and subsequent/stale/narrow/fully hidden negatives emit no fill.

CUDA evaluation latency was approximately 23.1 ms median and 31.6 ms p95.  The production build
uses the self-contained DirectML runtime and rejects a compiled bundle unless its end-to-end
detector smoke reports `DmlExecutionProvider` with p90 <= 35 ms.

## 6. Deliverables

* Canonical release model: `models/orion_meter_detector.onnx` (960 FP16).
* Training artifact: `runs/detect/logs/diagnostics/meter_train/meter2k27_n4_lowfill/weights/best.onnx`;
  `best.pt` remains alongside it.
* Hard set + manifest: `runs/detect/logs/diagnostics/meter_train/lowfill_hardset/`.
* Tools: `tools/training/mine_lowfill_hardset.py`, `tools/training/finetune_meter_lowfill.py`,
  `tools/training/eval_meter_lowfill.py`, `tools/training/eval_meter_occlusion.py`,
  `datasets/meter2k27_combo_lowfill.yaml`.

### Runtime and rollback

`MeterYoloLocator` resolves `models/orion_meter_detector.onnx` first.  A source checkout may fall
back to n4/n3 run-tree artifacts for development, while production force-pins the package-relative
canonical path and refuses to start the sidecar when it is absent.  The release builder validates
the ONNX through DirectML, includes it in the sidecar source-identity manifest, and runs the freshly
compiled executable's detector smoke before packaging.

Model rollback is an explicit artifact promotion: replace the canonical ONNX with the reviewed
n3 file, rebuild the sidecar, regenerate the signed release manifest, and rerun StrictSecurity.
Changing an environment variable is not a production rollback path.

## 7. Limits / honesty notes

* The hard set's truly hard frames (old model finds nothing) are only 32, on two parquet courts;
  the fine-tune sees ~13 of them plus 151910's parquet `detected_lowfill` frames. The held-out
  parquet test is session_20260901_141000 (21 propagated + 89 detected frames).
* `session_20260903_100538` (held-out, MyCourt) has no real model misses, so it measures
  regression, not the blind spot.
* Legacy tools: `eval_meter_detector.py framedump` runs (results above); `pill` mode needs the
  park clips directory (not on this machine); `eval_meter_2k27.py` defaults to
  session_20260827_122443, which is not on disk.
* The live acquisition delay on MyCourt is the async cadence (already addressed by
  `ORION_METER_DETECTOR_SYNC_ACQUIRE`, see run_orion.local.ps1:349-356), and no detector retrain
  changes that.
* Synthetic occlusion measures controlled robustness, not natural player/ball overlap.  Final
  approval still requires a long live batch with raw frames, release command epochs, delivery ACK
  timing, and banner outcomes joined per physical shot.
