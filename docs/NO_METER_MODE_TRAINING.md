# No-Meter Mode — Training Data Pipeline

Turns the owner's gameplay drive (~2100 mixed files: gameplay, menus, photos) into a
training-ready corpus for No-Meter Mode: a pseudo-labeled YOLO-pose dataset for the
student model, plus frame-level weak labels for the five release cues
(**set point, jump, flick, push, release**).

The pipeline **consumes** the existing, load-bearing tools without modifying them:
`pose_timing.py`, `tools/training/pose_pseudolabel_dataset.py`,
`tools/training/pose_finetune.py`, `tools/diagnostics/prepare_e2e_data.py` stay
byte-identical — they are used for other diagnostics.

## Stage 0 — CUDA torch preflight (ALWAYS FIRST)

Nothing runs until `torch.cuda.is_available()` is True. This is deliberate and
non-negotiable: the 2026-07-06 0/28 batch root cause was a silently-CPU detector at
8fps, and this rig has previously carried a **CPU-only** torch build
(`2.13.0+cpu`) where `import torch` succeeds but CUDA is absent. "It imports" is
not the test.

`tools/training/no_meter_mode_prep.py` runs the check automatically and fails loud
with the fix:

```
"<your python>" -m pip uninstall -y torch torchvision
"<your python>" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126
"<your python>" -m pip install ultralytics
```

Use the python you will actually train with (the sidecar venv lacks
torch/ultralytics entirely; the system Python312 has the CPU trap). Verify by hand
if in doubt:

```
python -c "import torch; print(torch.__version__, torch.cuda.is_available())"
```

Expected: a `+cu…` version string and `True` on the RTX 3060.

> Ship-side note: this CUDA torch requirement is for the **training rig only**.
> The live sidecar ships the ONNX/TensorRT export route (see the No-Meter audit);
> never ship CPU inference.

## Stage 1 — Menu/junk clip filter

The drive contains menus, lobbies, loading screens, replay cut-scenes, and still
photos. Filtering happens **before** any GPU labeling so the teacher never wastes
passes on junk:

- **Still images** are skipped by extension (they carry no temporal signal; using
  them for pose-only pseudo-labels is a possible later extension, deferred).
- **Heuristic tier** (cv2-only, importable as
  `from no_meter_mode_prep import classify_frame, scan_video`): 9 frames sampled
  evenly per clip; a frame is "menu-like" if one coarse HSV color bin dominates
  (>60% — flat menu art), the frame is near-black (loading), or the
  saturation-weighted hue spread is tiny (monochrome UI). A clip needs ≥1/3
  gameplay-like samples to survive.
- **Player-detect gate**: the first 3 gameplay-like samples are run through
  `models/orion_player_detect_v9.pt`; if no `Player` box appears in any of them the
  clip is rejected (`no_player_found`). Disable with `--no-player-gate`.

This extends the `--max-scan` early-abort idea from
`pose_pseudolabel_dataset.py:41-42` into a decision made before labeling starts.
Cheap wins over perfect classification: thresholds live at the top of
`no_meter_mode_prep.py` and are meant to be tuned against the real drive.

## Stage 1b — Fast batched pseudo-labeling (`tools/training/fast_pseudolabel.py`)

The Stage 2 fan-out below (one `pose_pseudolabel_dataset.py` subprocess per kept
clip) reloads the 62.7M-param teacher **once per clip** — on the 1040-clip corpus
that is ~50 minutes of pure model-load + CUDA-context overhead, and a measured run
cleared only ~101 clips in 30 minutes (~5 h projected). `fast_pseudolabel.py` is
the fast path: one process, **one teacher load**, menu filter + batched FP16
labeling per clip, **identical output contract** (dataset layout, label lines,
stems, `sources.json`, manifest schema/path — downstream cannot tell them apart;
`split_train_val.py --dry-run` verified against its output).

**Measured on the RTX 3060 (2026-08-09, 20-clip benchmark with defaults):**

- **19.8 clips/min end-to-end** (menu filter + labeling, 60 frames/clip) →
  **~53 min projected for all 1040 clips** (vs ~5 h for the fan-out).
- Per-frame teacher inference @960: fp32 batch 1 = 48.0 ms → **FP16 batch 16 =
  24.7 ms (1.94×)**. FP16 alone is 1.57×; batching adds 1.24× under FP16 (only
  1.07× under fp32 — the 3060 is already saturated by one fp32 image at 960).
  `--imgsz 640` would halve it again (12.2 ms) but 960 already fits the budget.
- FP16 parity vs fp32: 0.15 px mean / 0.21 px max top-player keypoint delta,
  92 vs 93 detections over 16 frames — no visible label degradation
  (`--no-half` exists as the escape hatch).
- Disk: `--save-imgsz 960` (saved jpg longest side; default) = **115 KB/frame →
  ~6.8 GB** for the full corpus, vs ~41 GB at native 1080p (which C: cannot
  hold). Labels are normalized against the **original** frame the teacher saw and
  the resize is uniform, so coordinates are unaffected (asserted at runtime, and
  verified externally: worst round-trip deviation 0.5 px on the original frame).

**Defaults that differ from the fan-out (deliberate):** teacher
`yolo26x-pose.pt` (faster and +7.2 AP over `yolov8x-pose` on COCO pose),
`--per-video 60` (was 150 — frames within a clip are highly correlated, so
frames 61–150 add little; 60 × 1040 ≈ 62k frames is still generous and keeps the
fine-tune epochs sane), output root `D:\VeniceTraining\no_meter_prep` (C: lacks
the space; a **free-space preflight** costs the planned frames against the target
drive and fails loud *before* the teacher loads).

**Resumable:** every finished clip is appended to `<out>/progress.jsonl`; a
crash 40 minutes in loses nothing — rerunning skips completed clips (`--resume`
default, `--no-resume` to redo). Progress/ETA line printed every N clips.

**Ultralytics 8.4 trap** (why the fan-out never got FP16): `half=` is a
**deprecated no-op** — FP16 is `quantize=16`, and the predictor freezes its
precision at the **first** `predict()` call of a `YOLO` instance, so a warmup
predict without `quantize` locks fp32 permanently. `fast_pseudolabel.py` passes
identical kwargs on every call.

Full-corpus run (Stage 3 cue labels are a separate pass — opt in with
`--cue-labels`, or run `prepare_release_cue_labels.py` on
`<out>/kept_videos.txt` afterwards):

```
python tools\training\fast_pseudolabel.py --videos-root "E:\PS5\CREATE\Video Clips\NBA 2K26"
python tools\training\split_train_val.py --dataset D:\VeniceTraining\no_meter_prep\pose_ds --val-source-allowlist owner
```

The orchestrator can also route its Stage 2 through this extractor with
`no_meter_mode_prep.py --fast` (the subprocess fan-out below remains the default
and the fallback).

## Stage 2 — Teacher pseudo-labeling (delegated)

Each kept clip is passed to the **unmodified**
`tools/training/pose_pseudolabel_dataset.py` (one subprocess per clip,
`--videos-glob` given the exact escaped path). Output is the existing YOLO-pose
format — `class cx cy w h (px py v)*17` txt lines under
`images/{train,val}` + `labels/{train,val}` + `pose2k.yaml`. No new format.

Provenance: every video stem is tagged in `<dataset>/sources.json`
(`{"<video_stem>": "owner" | "youtube"}`). The YOLO txt files themselves stay
untouched; the tag rides in the sidecar file keyed by the stem prefix.

Owner data uses teacher confidence **0.55** (the established default).

## Stage 3 — Release-cue label prep (weak supervision)

`tools/training/prepare_release_cue_labels.py` builds frame-level ground truth for
the five cues from **meter-confirmed shots** (the meter is ON in the training
recordings — it is the oracle, per the `prepare_e2e_data.py` pattern):

| Cue | Recipe |
|---|---|
| release | meter-fill edge: max fill in the shot span (must reach `--min-tip-fill`, default 70) |
| flick | wrist-velocity peak in the ascent (fastest upward wrist motion ≤300ms before release) |
| push | ascent onset: walk back from the flick to where upward velocity first crossed threshold |
| set point | wrist-y minimum before the ascent (image coords: min y = highest raise pre-push) |
| jump | feet-y minimum (mean ankle-y at its highest, −400…+150ms around release) |

Cues are **animation-level invariants** — the same recipe for every shot type, no
per-type branching (owner correction). Each cue carries a 0–1 confidence
(trajectory coverage × cue-strength) and each shot an ordering check
(`set ≤ push ≤ flick ≤ release`), so a downstream trainer can weight or drop weak
labels.

Pose here is YOLO-pose run directly with a meter-nearest player lock (the
`pose_phase_analyzer.py` pattern) because the cue set needs **ankles**, which the
`PoseTimingDetector` ring buffer (wrist/hip/knee only) does not carry.

Output: `release_cue_labels.json` — per-shot records with cue frames, times,
ms-before-release offsets, confidences, `source` tag, plus a printed median/IQR
summary per cue.

## Stage 4 — Train/val split (owner-only validation)

**Validation must be the owner's own gameplay.** YouTube frames grade nothing we
care about (overlays, face-cams, builds the owner never plays).
`tools/training/split_train_val.py` enforces it:

```
python tools/training/split_train_val.py --dataset logs/diagnostics/no_meter_prep/pose_ds \
    --val-frac 0.2 --val-source-allowlist owner
```

- Frames whose source is not in the allowlist (`youtube`, and fail-closed
  `unknown`) can never enter val; any already sitting there are evicted to train.
- The split is made at **video granularity** by default — frames of one clip are
  correlated, and a frame-level split would leak train footage into val.
- The script exits non-zero if val would end up empty or contaminated.

## Stage 4b — Efficient fine-tune + active learning (2026-08-08)

Four efficiency wins layered **around** the unmodified `pose_finetune.py` (its CLI
cannot express `freeze`/`amp`/`lr`, so `tools/training/pose_finetune_efficient.py`
replicates its exact `train()` kwargs — device 0, project
`logs/diagnostics/pose_train`, patience 15, plots off — and extends them; a drift
guard warns if the base script's config surface ever changes).

```
python tools/training/pose_finetune_efficient.py \
    [--data logs/diagnostics/pose_ds/pose2k.yaml] [--student yolo11n-pose.pt] \
    [--phases 320:20,640:10] [--freeze 10] [--verbose-freeze] [--boost-weight 1]
```

**Win 1 — backbone freeze (`--freeze 10`, default).** The teacher/student pose
backbones were trained on COCO; layers 0..9 (conv stem + C2f/C3k2 blocks + SPPF)
already carry generic person-pose features that 2K avatars do not invalidate — only
the head needs domain adaptation. Freezing them cuts training ~40% (backward pass
mostly gone) and reduces overfitting risk on the 1040-clip corpus. The default
student `yolo11n-pose` has an extra `C2PSA` block at index 10: `--freeze 11` locks
the full YOLO11 backbone; `--verbose-freeze` prints the per-layer frozen/trainable
table so you can see exactly what is locked; `--no-freeze` is the escape hatch.

**Win 2 — progressive resolution (`--phases 320:20,640:10`, default).** 20 epochs
at imgsz 320 (≈¼ the per-epoch cost) then 10 at 640, phase-2 weights initialized
from the phase-1 `best.pt`. Deliberately **not** Ultralytics `resume=True` — resume
restores the checkpoint's *own* imgsz/epochs and refuses a changed config; loading
`best.pt` as the next phase's starting weights *is* the cross-resolution carry.
Per-phase wall-clock is logged to the manifest. `--single-phase` bypasses
(`--imgsz`/`--epochs` then behave exactly like `pose_finetune.py`).

**Win 3 — AMP + optimizer pins.** `amp=True` is passed explicitly every phase and
logged up front (the Ultralytics default is True, but a stray cfg override would
silently cost the ~1.8×). `cos_lr=True`, `warmup_epochs=3`; freeze mode pins
**AdamW lr0=1e-3** (higher than a full fine-tune would tolerate — the frozen
backbone can't diverge). A `training_config.json` lands next to the final weights
(student, freeze, phases + per-phase wall clock, lr, AMP, boost, torch/ultralytics
versions) so eval always knows what produced them.

**Expected wall-clock (1040-clip corpus ≈ 100k pseudo-labeled frames, RTX 3060
12 GB, estimates — nothing trained yet):** baseline `pose_finetune.py` 40 epochs @
640 ≈ **14–15 h**; phases alone cut the epoch-equivalents 40 → 15 (−62%), freeze
takes ~35–40% off each step → **≈3.5–4 h**, roughly **4× / ~11 h saved** per full
fine-tune. AMP is insurance, not additional savings (already-default).

**Win 4 — active learning (`tools/training/pose_active_learning.py`, rank +
export only, NO retraining).** Runs the trained student over the **full corpus**
(not just the labeled subset), scores each sampled gameplay frame

```
uncertainty = mean(1 − conf) over detections + 0.25 × count(0.3 < conf < 0.6)
```

(the second term is the "hesitation zone"; a gameplay frame with **zero**
detections scores 1.0 — the student missed the player outright). Menu frames are
skipped via the importable `no_meter_mode_prep.classify_frame` heuristic so menu
art can't flood the queue.

```
python tools/training/pose_active_learning.py \
    --model logs/diagnostics/pose_train/pose2k_n_eff_phase2/weights/best.pt \
    --top-k 200 --min-uncertainty 0.3
```

Outputs under the dataset dir (default `logs/diagnostics/pose_ds/`):
`active_learning_queue.json` (top-K sorted by uncertainty: video path, frame
index, score components, current-model prediction),
`active_learning_review/qNNNN_*.jpg` (predictions overlaid — flip through these),
and `active_learning_review/clean/al_*.jpg + .txt` (clean frames + prefilled
YOLO-pose labels in the exact `pose_pseudolabel_dataset.py` line format, ready to
hand-correct). ~30–45 min for 1040 clips × 60 sampled frames on the 3060.

**The loop:** (1) flip through the overlays, (2) hand-correct the prefilled labels
in `clean/` with your tool of choice, (3) copy each `al_*.jpg`/`al_*.txt` pair into
the dataset's `images/train` + `labels/train`, (4) re-run
`pose_finetune_efficient.py --boost-weight 3`. The boost oversamples hand-labeled
train frames 3× by idempotent file duplication (`__boost` copies, cleaned and
re-made every run) — Ultralytics has no per-sample loss-weight hook, and N×
duplication equals N× loss weight in expectation. Boost membership = stems in
`<dataset>/hand_labeled.txt` if present, else the `al_` stem prefix; exported stems
are auto-merged into `sources.json` as `owner` so the Stage 4 provenance invariant
holds.

Both scripts run the same fail-loud CUDA preflight as Stage 0.

## Stage 5 — Cue temporal model (insurance: learned release timing)

Insurance for the case the pipeline was built to expect: the heuristic cue IQRs from
Stage 3 **miss the green window** (run `tools/diagnostics/pose_cue_fusion.py` first —
if fusion's calibrated IQR already fits, skip this stage). When it doesn't, a small
learned model predicts release timing directly from pose sequences.

**Architecture** (`tools/training/cue_temporal_model.py`): a 1D TCN — 4 residual
blocks of dilated **causal** convolutions (channels 64/128/128/128, kernel 3,
dilations 1/2/4/8, dropout 0.1, **321,927 params**, receptive field 61 frames ≈ the
60-frame window). Causal = left-pad + right-chomp, so the last timestep never sees a
future frame — the shipping constraint, since the runtime queries at "now". Two heads
read the last timestep:

- **Timing head** — scalar *frames-until-release* from the window's last frame (MSE).
  This is what the bot USES: it feeds the tip-timing arm decision the same way the
  meter tip does.
- **Cue head** — softmax over `{set_point, push, jump, flick, release, none}` (CE).
  Live corroboration with the meter + interpretability while debugging.

Loss = `0.6 * timing_mse + 0.4 * cue_ce` (timing weighted higher — it is what flies).

**Features per frame** (17 COCO joints × `[x, y, v]` = 51 channels, `(N, 51, T)`
channels-first): translation-invariant (minus bbox center), scale-invariant (over
bbox diagonal), handedness-invariant (left-handed mirrored to right via
`COCO_FLIP_IDX` + x negation), and missing joints keep a **raw 0/1/2 visibility
channel** instead of silent zero-fill — the model learns to down-weight occluded
joints. The full contract is written to `cue_temporal_model_meta.json`; **the runtime
must mirror it exactly or predictions are garbage.**

**Data**: `release_cue_labels.json` (Stage 3) + the source videos it references. The
Stage 2 YOLO frames are ~12fps decorrelated samples, so 60fps windows are re-extracted
from the videos and cached to `<labels>_seq_cache.npz` next to the labels JSON —
re-runs skip the OpenCV re-read entirely. Per shot, 4 windows are cut ending at
release-minus-jitter (jitter ∈ [0, window/2]) so training sees varied
"how far from release" contexts, matching the runtime query pattern. Windows where
>30% of frames miss a shooting-arm joint (wrist/elbow/shoulder) are rejected.
Validation is **owner-only at video granularity** (same fail-closed invariant as
Stage 4); early stop on the val **green-window hit rate** (predicted release within
±30ms of truth), patience 8.

```
python tools/training/train_cue_temporal_model.py \
    --labels logs/diagnostics/no_meter_prep/release_cue_labels.json \
    --out logs/diagnostics/cue_temporal
python tools/training/eval_cue_temporal_model.py \
    --checkpoint logs/diagnostics/cue_temporal/cue_temporal_model.pt
python tools/training/export_cue_temporal_onnx.py \
    --checkpoint logs/diagnostics/cue_temporal/cue_temporal_model.pt
```

The trainer runs the same CUDA preflight as Stage 0 (fail-loud, exact pip fix
printed); the exporter too. Eval is CPU-fine (~0.3M params). The exporter writes
`cue_temporal_model.onnx` — **opset 17, fully static shape `[1, 51, window]`**
(DirectML-ready, the sidecar shipping pattern) — then runs an ORT-CPU parity check
on 10 val sequences: max abs diff vs the torch model must be < 1e-4 on both heads or
the export fails. Outputs land together: `cue_temporal_model.pt` (retraining),
`cue_temporal_model.onnx` (shipping), `cue_temporal_model_meta.json` (runtime
contract: window, classes, normalization, frames→ms rule).

**Expected wall-clock on the 3060, ~5000 labeled shots**: extraction dominates the
first run — ~450k pose inferences ≈ 40–75 min (one-time, cached); training itself is
light (~20k windows × 50 epochs ≈ 10–30 min); **~1–2 hours total first run**,
minutes on re-runs once the cache exists. Model inference is ~15M MACs per window —
well under 2ms/frame on the 3060, sub-ms typical.

## Running the whole thing

```
python tools/training/no_meter_mode_prep.py --videos-root "D:\<owner drive>" --source owner
python tools/training/split_train_val.py --dataset logs/diagnostics/no_meter_prep/pose_ds
python tools/training/pose_finetune.py --data logs/diagnostics/no_meter_prep/pose_ds/pose2k.yaml
```

(or swap the last line for `pose_finetune_efficient.py` — Stage 4b — for the ~4×
faster freeze + progressive-resolution schedule on the same dataset yaml).

The orchestrator writes `no_meter_mode_manifest.json` (per-video
kept/rejected/labeled counts, artifact paths, next-step commands) — the single
input file for training scripts. Expect the pseudo-label stage to dominate
runtime on ~2100 clips (one teacher subprocess per kept clip; batch it overnight,
or thin with `--max-videos` / `--per-video` first).

After `pose_finetune.py`, evaluate with the existing
`tools/training/pose_eval_compare.py` and latency-check with
`tools/training/pose_export_benchmark.py`.

## YouTube augmentation — deferred, OFF by default

`tools/training/youtube_augment.py` is a shim for a **secondary** source: jump-shot
animation variety from builds the owner doesn't play. It does nothing without an
explicit `--enable`. When enabled it downloads via yt-dlp at highest quality,
blacks out the **top-right quadrant** (streamer face-cam) before labeling AND
saving, labels at a **harder teacher threshold (0.75 vs 0.55)**, prefixes stems
`yt_`, tags `source=youtube`, and writes to the **train split only**.

It is *not* for temporal-head/cue training — YouTube footage has no controlled
meter setup, so it can never produce release-cue ground truth.

## What this pipeline does NOT cover (deferred)

- ~~Training the cue/temporal model itself.~~ **Now covered — see Stage 5** (still
  gated on the heuristic cue IQRs missing the green window; run
  `tools/diagnostics/pose_cue_fusion.py` first — fusion may suffice).
- **Live emission of the new cues.** `pose_timing.py` emits `push`/`release` only;
  `set_point`/`jump`/`flick` need landmark-kind wiring end-to-end
  (pose_timing emit → sidecar passthrough → `AutomationEngine::updatePoseLandmark`
  kind gate + scheduler branches). Tracked in the No-Meter audit memory.
- **Still-photo pseudo-labeling** (skipped as junk today).
- **Sidecar venv/ship deps** (ONNX/TensorRT export route) — separate task.
