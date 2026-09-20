# Animation anchor V2 — measured design (2026-09-16)

> Supersedes the design half of [`ANIMATION_ANCHOR.md`](ANIMATION_ANCHOR.md) (2026-06-20).
> That doc was design + an offline proof on ONE 120 fps practice-gym clip. This one is a
> **measurement pass on the 2K27 data we actually have**, and it changes three of its
> recommendations (ROI crop, `ultralytics.predict`, "more data fixes the cue model").
>
> Owner's framing: *"we can build the animation-anchor too, whatever gets shots consistent —
> unlike everyone else's it'll be a hell of a lot more consistent."*
>
> **Scope of this pass: design + measurement only.** No engine, sidecar, reader or detector
> source was touched. Every tool added is under `tools/diagnostics/`; every bulky artefact is
> under `D:\NexusVision\anchor_study\`.

---

## 0. The one-paragraph answer

A pose anchor is **cheap enough** (7.0 ms p50 on a side thread, 0 ms of detect-thread stall
measured) and the model **works on 2K27 frames** (96 % detection, 15/17 keypoints, a clean
0.45 → 0.05 wrist raise per shot). But **none of the data on this box can measure its jitter**:
the two 2K27 framedumps are 8.8 and 6.2 fps, the 2K26 60 fps corpus is on an unmounted drive,
and the one 60 fps-derived pose cache that survived is **tracking the wrong player on 84 % of
its shots** — which also retires the "not enough data" verdict on the 2026-08-09 cue-TCN
failure. The single measurement that *does* land is the one that matters most for the thesis:
over 27 shots the wrist-rise time and the drawn-meter onset move together with **slope 1.048
(r = 0.653)** — the drawn meter's variable onset lag IS the animation's own clock, so an
earlier pose landmark can in principle replace it. **Next action is not code: it is one
60 fps framedump** (`ORION_FRAMEDUMP_INTERVAL=0` on D:). Everything else in this doc is
specified so that dump is the only thing standing between here and a shippable anchor.

---

## 1. Assets: what is actually in `models/`

| file | task | note |
|---|---|---|
| `orion_pose2k_n_v2.pt`, `orion_pose2k_n_v3.pt` | **pose** | v2 is what `pose_timing.py` loads today |
| `orion_pose2k_n_v3_256.onnx`, `_256_op17.onnx` | **pose** | 256-input exports of v3 |
| `models/onnx/pose_256.onnx`, `pose_320.onnx`, `pose_640.onnx` | **pose** | unlabelled exports, provenance unclear |
| `orion_meter_detector.onnx`, `orion_meter_n*.pt`, `orion_meter_n_v7_release.onnx/.pt`, `orion_bar_n.pt`, `orion_bar_park.pt`, `meter_reader*.pt/.json` | meter | not pose |
| `orion_player_detect_v9.pt`, `user_player_classifier.pt` | player box / identity | the player-lock lane |
| `shot_start_classifier.pt`, `release_predictor_v2.pt`, `fill_forecaster/` | timing heads | not pose |
| `hand_landmarker.task` | MediaPipe hand | unused |

**The head-to-head winner is not in `models/`:**
`logs/diagnostics/pose_train/pose2k_n_eff_phase2/weights/best.pt` — verified this pass as
**YOLO26n-pose**, `kpt_shape [17, 3]`, one class (`person`), **2,926,494 params fused,
2.7 GFLOPs**, trained 2026-08-09 from `pose2k_n_eff_phase1` at imgsz 640 on
`C:\VeniceTraining\no_meter_prep\pose_ds\pose2k.yaml` (**that dataset path no longer exists** —
if the model is ever retrained the corpus must be rebuilt; see §5). Everything below uses
this file.

`remote_play_orchestrator.py` builds `PoseTimingDetector` **only when `config.no_meter_enabled`**
(line ~1852), and points it at `orion_pose2k_n_v2.pt` — so on every meter-mode session the
`POSE ARM:` line prints *"pose detector None"* and only the shot gate arms. The anchor lane is
therefore **dark in the shipped configuration**, which is the right default until the numbers
below exist.

---

## 2. Cost — measured on this box (RTX 3060, 12 GB; onnxruntime-gpu 1.22, torch 2.13+cu126)

Tools: `tools/diagnostics/anchor_pose_bench.py`, `anchor_pose_bench_trt.py`,
`anchor_thread_budget.py`. Artefacts: `pose_cost.json`, `pose_cost_trt.json`,
`thread_budget.json`, and the exported `pose26n_*.onnx`.

### 2.1 Export

| input | fp32 | fp16 |
|---|---|---|
| 256 | 2.01 s, 11.9 MB | 4.06 s, 6.0 MB |
| 320 | **1.59 s, 11.9 MB** | 4.50 s, 6.0 MB |
| 384 | 1.62 s, 12.0 MB | 4.75 s, 6.1 MB |

opset 17, `simplify=True`. Export is a build-time step, not a runtime cost. The head is
YOLO26 **end-to-end (NMS-free)**: output `(1, 300, 57)` = `[x1,y1,x2,y2,score,cls, 17×(x,y,c)]`,
so there is **no NMS to pay for and no NMS threshold to tune** — a real simplification versus
the v8/v11 pose heads the old tooling decodes.

### 2.2 Per-frame latency (400 iterations each, warm)

| path | p50 | p90 | p99 | max |
|---|---|---|---|---|
| ORT **CUDA** fp32 128 | 6.92 | 12.78 | 15.85 | 17.91 |
| ORT **CUDA** fp32 192 | 6.86 | 10.18 | 15.08 | 17.14 |
| ORT **CUDA** fp32 256 | 6.76 | 9.51 | 14.64 | 17.22 |
| ORT **CUDA** fp32 **320** | **7.02** | 9.75 | 14.68 | 17.39 |
| ORT **CUDA** fp32 448 | 9.23 | 10.68 | 12.38 | 13.94 |
| ORT **CUDA** fp32 640 | 9.93 | 11.26 | 13.55 | 14.84 |
| ORT CUDA fp16 320 | 8.65 | 16.79 | 20.45 | 22.52 |
| ORT CUDA fp32 320 + **IO binding** | 8.36 | 9.76 | 11.17 | 13.35 |
| ORT **TensorRT EP** fp32 320 | 7.85 | 8.83 | **10.20** | 12.37 |
| ORT **CPU** fp32 320 (2 threads) | 13.96 | 17.30 | 18.39 | 19.55 |
| `ultralytics.predict` fp16 320 | 18.63 | 23.98 | 33.94 | 35.68 |
| raw `torch` forward fp16 320 | 28.93 | 37.11 | 46.83 | 49.06 |

Four conclusions, three of which contradict the 2026-06-20 recipe:

1. **The model is overhead-bound, not compute-bound.** p50 is flat (6.8–7.0 ms) from a
   128 px input to a 320 px input. **A smaller crop buys nothing.** (This confirms the
   2026-06-20 self-correction and retires the original "ROI-crop → single-digit ms" claim.)
2. **fp16 is not faster** — it is *slower* at 320 (8.65 vs 7.02) and its tail is worse.
   Ship the **fp32** export; keep fp16 only as a VRAM lever.
3. **`ultralytics.predict` costs 2.7× the ONNX path, and raw `torch` costs 4×.** The
   "~12 ms/frame" figure in `ANIMATION_ANCHOR.md` was the wrapper, not the model. The live
   path must be `onnxruntime`, never `ultralytics`.
4. **TensorRT is the only thing that improves the TAIL** (p99 10.2 vs 14.7). The p50 gain is
   ~0. If a tail target is ever set, TRT is the lever; it is not needed for the plan in §4.

Also on the timed path, per frame:

* **preprocess** (crop → resize 320 → RGB → CHW → /255) = **3.48 ms p50** on CPU. That is half
  the inference cost and is pure numpy/OpenCV — it should be folded into the model (a uint8
  input with the normalisation as graph ops) or done on an already-resized buffer.
* **decode** (argmax over 300 queries + reshape 17×3) = **0.04 ms**. Free.

### 2.3 The detect-thread question (the one that actually gates the ship)

`anchor_thread_budget.py` runs a synthetic **detect thread at 60 Hz doing ~3 ms of
locator-shaped numpy work** and records how late each of its wakes is, with and without a
pose thread beside it. 12 s per case, first 60 ticks discarded.

| case | detect tick late p50 | p90 | p99 | max | ticks > 16.7 ms late | pose achieved |
|---|---|---|---|---|---|---|
| detect alone (control) | 0.29 | 0.49 | 0.60 | 0.75 | **0 %** | — |
| + pose **every frame** | 0.31 | 0.52 | 0.73 | **2.61** | **0 %** | 48.5 Hz (p50 8.67, p99 17.81) |
| + pose **every 2nd frame** | 0.29 | 0.50 | 0.78 | 1.54 | **0 %** | 25.1 Hz |
| + pose **every 4th frame** | 0.29 | 0.48 | 0.58 | 0.69 | **0 %** | 12.6 Hz |

**A pose thread does not stall the detect thread.** onnxruntime releases the GIL inside
`Run()`, and the worst detect-tick lateness a full-rate pose thread ever caused was **2.61 ms**
— an order of magnitude inside the 16.7 ms tick, with zero ticks missed. This is the opposite
of the 2026-09-14 GIL regression, whose cost was a 2.81 ms full-frame `array_equal` **on the
detect thread itself**. The rule that survives is *"not on the detect thread"*, not
*"no neural network"*.

**Recommendation — every 2nd frame, inside the press window only, on its own thread.**
Every-frame is achievable (48.5 Hz) but only by dropping ~19 % of ticks and with a worse tail;
30 Hz sampling costs ~16.7 ms of extra event quantisation, which §3.4 shows is ~1 ms of
interpolated event error — irrelevant. Gating on the press window (the same arm the
`player_anchor` already uses) keeps the GPU idle ~85 % of wall time.

### 2.4 Two deployment traps found while measuring

* **DirectML was not measurable.** This venv has `onnxruntime-gpu`, not
  `onnxruntime-directml`, and the two cannot coexist in one environment. If a non-NVIDIA
  fallback is a ship requirement it must be benchmarked in a separate sidecar venv. Not done.
* **`onnxruntime-gpu` silently falls back to CPU unless `torch` is imported first.**
  Measured: a session created in a fresh thread without `import torch` fails with
  `cublasLt64_12.dll ... is missing` and runs at 22–25 ms on CPU while reporting success at
  the API level. torch's `__init__` is what puts the CUDA/cuDNN DLL directory on the search
  path. Any sidecar that loads the pose model **must** either import torch first or call
  `os.add_dll_directory` on the CUDA bin dir, **and must assert
  `sess.get_providers()[0] == "CUDAExecutionProvider"`** — otherwise the anchor quietly
  becomes a 25 ms CPU job.

---

## 3. Signal — what the pose actually says on 2K27

Tools: `anchor_drill_pose.py`, `anchor_drill_events.py`, `anchor_sampling_limit.py`,
`anchor_cue_events.py`. Artefacts: `drill_pose*.{npz,json,csv}`, `drill_events.{json,csv}`,
`sampling_limit.json`, `cue_events.{json,csv}`, `poseA.npz`, `poseB.npz`.

Two 2K27 sessions were used:

* **A — `session_20260915_185359`** (the drill dump): 824 frames, **8.84 fps**
  (median gap 113.1 ms), 29 releases, banner verdicts from `_analysis/panel_grade.json`
  (15 EXCELLENT / 9 LATE attributed / 2 EARLY / 2 UNKNOWN), **solo** (MyCourt).
* **B — `session_20260912_201355`**: 12 003 frames, **~6.2 fps** (median bracket 161.6 ms),
  72 releases with a press/release pair from `logs/diagnostics/hud_landmark_study/shots_table.csv`,
  **multi-player** (Theater, 2–4 people in frame).

### 3.1 The model works on 2K27 — the *player lock* is what breaks

| | session A (solo) | session B (multi-player) |
|---|---|---|
| frames with ≥1 person | **96.3 %** | 99.5 % |
| persons/frame p50 / p90 / max | 1 / 1 / 2 | **2 / 3 / 4** |
| shooter box confidence (median) | 0.60 | 0.60 |
| keypoints conf ≥ 0.5, of 17 | **15** | 15 |
| **nearest-to-meter-box track jumps > 120 px** | **0.0 per frame** | **0.20 per frame** |

The model — fine-tuned on **2K26** — transfers to 2K27 without retraining: same confidence,
same 15/17 keypoints in both. What does not transfer is the **identity rule**. In the 5v5
session the "person nearest the meter box" changes player on **one frame in five**, and that
alone explains why session B's event times scatter with a MAD-sd of **363–774 ms** against
session A's 79 ms. See §5.

**And the same failure already cost us a model.** The 2026-08-09 cue-TCN was trained from
`D:\VeniceTraining\no_meter_prep\cue_labels_seq_cache.npz` (251 shots × up to 90 samples of
17 keypoints, sampled every 3rd frame of 59.94 fps video = 50.05 ms). Checking each cached
track's keypoint centroid at its own labelled release frame against the same record's
`player_bbox_at_release`:

> **only 41 of 251 tracks (16 %) are on the labelled shooter** (dx median −31 px, p10 −703,
> p90 +412). The tracks carry **no shot signature at all** — wrist-to-shoulder height wanders
> 0.3–0.5 body-heights with no raise at the release.

So the cue-TCN's val MAE of 582 ms was **not** a data-volume problem, and "251 shots → 619
windows was too little" should be retired as the diagnosis. It was a **player-lock** problem.
The cache is unusable for this study and should not be used to seed a V2 model either.

### 3.2 The signal that exists: the wrist raise

On session A, wrist-y normalised inside the person box (0 = box top, 1 = box bottom) falls
from **~0.45 at rest to ~0.05 overhead** across every shot — depth 0.26–0.97, median 0.43.
Example (seq 22, Right Fade, hold 949 ms, EXCELLENT), `dt` = ms after the press:

```
dt     -426  -310  -208   -91    11   128   239   444   558   674   780   945  1067  1168
wr_y  0.272 0.396 0.463 0.534 0.422 0.358 0.401 0.438 0.545 0.531 0.398 0.153 0.099 0.012
```

The event used below is the **linearly interpolated crossing of `rest − f·depth`, scanned
BACKWARD from the highest-hand sample** (so it belongs to *this* release, not an earlier
dribble). `f` = 0.25 / 0.50 / 0.75.

**Crop rule (measured, 508 paired frames, session A, 1280×720):** the shooter's person-box
centre sits at the meter box centre **+58 px in x** (p10 −65, p90 +81) and **+53 px in y**
(p10 +5, p90 +72); the person box is **167 px tall** (p10 134, p90 194). A 320×320 crop at
that offset therefore contains him — but see §3.5: **do not crop.**

### 3.3 Event timing at 8.8 fps — and why it is not the answer

| quantity (session A) | n | median | MAD-sd |
|---|---|---|---|
| press → **meter onset** (full-rate, from `detframes_window.csv`), Standstill | 20 | 508.4 ms | **21.2 ms** |
| press → **pose half-rise**, Standstill | 18 | 619.7 ms | 79.5 ms |
| press → meter onset, Right Fade | 7 | 857.8 ms | 70.9 ms |
| press → pose half-rise, Right Fade | 7 | 959.4 ms | 78.6 ms |

Two things to read off, and only one of them is a real result:

* **The pose half-rise lands ~89 ms AFTER the drawn meter onset** (median over 27 shots). As
  an onset *replacement* the half-rise is therefore useless — it arrives later than the thing
  it would replace. **Any useful anchor must be an EARLIER landmark** (the gather / knee dip
  / first upward wrist velocity), which 113 ms sampling cannot resolve at all.
* **The apparent jitter advantage of the meter onset is not a real comparison.** The 113 ms
  bracket puts ~48 ms of pure interpolation error into every pose event (§3.4) — the pose
  number is an instrument reading, not a property of the animation.

**The one measurement that survives the sampling** is the coupling. Over 27 shots, regressing
press→pose-half-rise on press→meter-onset:

> **Pearson r = 0.653, slope = 1.048**, pose − onset median +88.8 ms, residual sd 200.9 ms.

A slope of 1.00 is the thesis. A shot whose drawn meter appears 100 ms later is a shot whose
wrist rises 100 ms later: **the "variable 0–130 ms onset lag" is not a drawing artefact
layered on a fixed animation — the animation itself is what moved.** That is exactly the
condition under which an earlier pose landmark is worth having, and exactly the condition
under which the tempo bucket should be keyed on the animation rather than on the drawn bar.
(The residual is dominated by the instrument; r = 0.653 is a floor, not a ceiling.)

### 3.4 Can the pose separate the 11 LATE from the 15 EXCELLENT? **Not at this frame rate.**

The forensic core of the problem: on the drill dump the LATE and EXCELLENT shots are
identical on *every* meter quantity (fill@command 43.0 ± 0.9 both, slope 0.180, peak
90.5 ± 0.4, box h 119 ± 1). Two pose tests were run.

**Test 1 — animation phase at the release command** (wrist-y interpolated at the fire instant,
expressed as 0 = rest, 1 = hand overhead):

| verdict | n | phase median | sd |
|---|---|---|---|
| EXCELLENT | 14 | 0.665 | 0.241 |
| LATE | 8 | **0.706** | 0.102 |

**Test 2 — release command minus pose half-rise:** EXCELLENT 50.5 ms (MAD-sd 43.8),
LATE 39.8 ms (MAD-sd 25.9).

The *direction* of Test 1 is right — LATE shots fire further into the animation, with the hand
higher (wrist-y 0.152 vs 0.171) — but the effect is ≈ 0.04 phase units ≈ **15 ms**, against a
within-group sd of 0.10–0.24. This is **not evidence that the pose cannot separate them.** It
is evidence that the instrument cannot:

**Sampling budget (`sampling_limit.json`).** Decimating session A's own tracks from a 113 ms
bracket to 226 ms shifts each shot's event by |median| 42.0 ms (sd 143.4). Fitting the
linear-interpolation error law `err ≈ k·bracket²` to that shift gives

| bracket | interpolation error sd |
|---|---|
| 113 ms (today's dump) | **47.8 ms** |
| 16.7 ms (a 60 fps dump) | **1.04 ms** |

**The discriminator we are hunting is ~15–25 ms wide and the instrument's floor is ~48 ms.**
The question is not answerable with these dumps and will not become answerable by analysing
them harder. A 60 fps dump moves the instrument floor to ~1 ms — 15× below the effect — and
additionally allows fitting the event over ~25 samples instead of interpolating between 2,
which attacks the per-frame keypoint noise as well.

### 3.5 Crop vs full frame — **do not crop** (contradicts the 2026-06-20 recipe)

260 session-A frames, 320×320 crop at the §3.2 offset vs the full frame at imgsz 640:

| | full frame @640 | 320 crop @320 |
|---|---|---|
| frames with no person | 0 by construction¹ | **23 (8.8 %)** |
| box confidence (median) | **0.552** | 0.421 |
| keypoints conf ≥ 0.5 | 14 | 14 |
| cost p50 (§2.2) | 9.93 ms | 7.02 ms |

¹ the 260 frames compared are exactly those where the full frame **did** find a person, so
the crop's 8.8 % is a loss *relative to* the full frame, not an absolute miss rate. The
full frame's own absolute rate over all session-A shot-window frames is 96.3 % (§3.1).

`|wr_y_crop − wr_y_full|` = 0.029 normalised units median, 0.079 p90 (≈ 5 px / 13 px on a
167 px player). The crop **loses the player on 1 frame in 11** and scores lower, for 2.9 ms.
The reason is arithmetic: at 720p a 167 px player letterboxed into a 640 input is already
~148 px, so the crop adds no resolution — it only adds a way to miss. **Run the full frame at
448 or 640** (448 is 9.23 ms p50 with the *best* tail of the fp32 sweep, 12.38 ms p99) and
select the shooter from the returned people. Keep the §3.2 offset as the **selection** rule,
not as a crop.

---

## 4. The record to log — free labels from normal play

The point of the live record is that **every shot the owner already plays is a labelled
training example**, and three independent labels already exist in the codebase:

| label | where it comes from | quality |
|---|---|---|
| **banner verdict** | `tools/timing/panel_grade.py` offline; the live banner reader (30/30 recall, onset→emit median 115 ms) | the game's own grade; 26 of 30 words known |
| **release oracle** | reader retraction gap, `RELEASE ORACLE:` line, ≤3 px = GREEN | **25/25 agreed with the banner** on the drill dump, **35/36 live** — banner-free |
| **"3"-icon off** | `tools/diagnostics/hud_3pt_icon.py` | icon-off − release **median 163 ms, rMAD 34, sd 61** over 24 clean shots; corr(icon_off−press, hold) = 0.98 vs 0.27 for the release → **release-locked**. Never precedes the release, so it can never *time* a shot — it is a label and a possession gate only. |

### 4.1 The per-shot record

One JSONL line per physical shot epoch, written by the sidecar on the existing stdout JSONL
channel (`emit_stdout_jsonl`, same channel as `release_oracle`), keyed on the shot-gate
physical epoch so it joins to `TIP RESERVATION` / `BANNER TRIM` / `ORACLE:` without a new
correlation scheme:

```jsonc
{"event":"anchor_record","release_seq":<physical epoch>,"schema":1,
 "t0_ms":<capture-clock ms of the physical Square edge>,   // every t below is relative to t0
 "press":{"shot_type":"Standstill|Left Fade|Right Fade|Post Fade|Go-To",
          "intent":"square_edge|stick_down_edge","ls":[x,y],"r2":<0-255>,
          "sprint_released":0|1},
 "meter":{"onset_ms":<firstMeterSeen - press>,"tempo":"quick|normal|slow",
          "first_fill_pct":,"slope_pp_per_ms":,"tip_eta_ms":,"green_obs_start":,
          "green_obs_width":},
 "command":{"release_ms":,"lead_ms":,"lead_source":"user|auto","banner_trim_ms":,
            "lead_offset_ms":,"hold_band":"late|early|none","fire_target":"frame_centre"},
 "labels":{"banner":"EXCELLENT|LATE|EARLY|"", "banner_lag_ms":,
           "oracle_gap_px":,"oracle_verdict":"GREEN|MISS",
           "icon_off_ms":,            // null when the plate was not read
           "settled_fill":,"peak_fill":},
 "pose":{"model":"pose26n_448_fp32","provider":"CUDAExecutionProvider","hz":30,
         "lock":{"source":"meter_box|plate|track","conf":,"flips":<identity changes in window>},
         "box_h_px":,"scale_px":,
         "t":[ ...ms relative to t0, one per sample... ],
         "k":[ [ [x,y,c] x17 ] ... ]   // crop-normalised: x,y in [0,1] of the PERSON BOX
        }}
```

Window: **press − 200 ms to release + 400 ms**, which is the span §3 needs to see both the
gather and the peak. Size: at 30 Hz that is ~19 samples × 17 × 3 floats ≈ **1.2 KB** as
float32, ~4 KB as JSON text. At 60 Hz, ~2.3 KB / ~8 KB. A 60-shot session is well under 1 MB.
Three notes that matter:

* **Normalise in the record, not in the analysis.** Store keypoints as fractions of the person
  box, plus `box_h_px` and `scale_px` separately. Camera pan and zoom otherwise poison every
  absolute coordinate — that is what made the 2K26 cache unrecoverable.
* **Store `lock.flips`.** §3.1 shows identity churn is the dominant error term in a real game;
  a record without it cannot be filtered afterwards.
* **Store the raw track, not an extracted event.** The event definition will change (§3.3 says
  the half-rise is the wrong landmark); the track is what makes that re-derivable offline.

### 4.2 How many shots for a per-tempo regression

The target is a **robust linear fit per (type, tempo)**, `release = a_bucket + b·event + c·pace`,
**not** a TCN. Sizing, with σ the per-shot residual and ε the acceptable error on the fitted
intercept (the natural ε is the trim step, **3 ms**):

* mean-only per bucket: `n ≥ (σ/ε)²`
* with a slope shared across buckets: same per-bucket n, plus ~50 shots once, globally

| assumed σ | n per bucket for ε = 3 ms | n per bucket for ε = 5 ms |
|---|---|---|
| 15 ms | 25 | 9 |
| **20 ms** | **45** | **16** |
| 30 ms | 100 | 36 |
| 48 ms (today's instrument) | 256 | 92 |

The grid is 5 shot types × 3 tempo sub-buckets (the thresholds already shipped:
Standstill quick < 500 / normal 500–580 / slow > 580 ms of engine onset; fades < 775 /
775–915 / > 915). Full grid at σ = 20 ms, ε = 3 ms ≈ **675 graded shots**. Recent sessions
run 20–57 graded releases, so that is **15–30 sessions of normal play** for the whole grid —
but the two buckets that carry almost all traffic (Standstill/normal, fade/normal) need
**~90 shots total, i.e. 2–4 sessions**. Seed those two, leave the rest on the global fit,
and let the per-bucket intercepts unlock as each bucket passes its own n.

Use a **robust** fit (Theil–Sen or Huber): the archive shows two contaminating populations —
the ghost-at-press shots (holds 690–734 vs 595–660) and the `sprint_release_on_square` dead
presses. Robustness costs ~1.5× in n; the table above is already conservative enough to
absorb it.

---

## 5. Fusion — how the anchor enters WITHOUT replacing the meter

The meter stays authoritative. That is not deference to the old doc; it is what the data says:
the meter path lands the same position every shot to ±2 ms, and no pose quantity measured here
is within an order of magnitude of that.

**(a) Anchor-sourced tempo bucket — SHIP THIS FIRST.**
Today `banner_lead_trim`'s tempo sub-buckets are keyed on `firstMeterSeenMs − physicalPressMs`
— the *drawn* onset. §3.3 shows the drawn onset and the animation move together with slope
1.048, so the drawn onset is a *proxy* for the animation with the onset-lag noise folded in;
an earlier pose landmark is the same quantity measured directly and earlier. This is the
cheapest possible integration: **the anchor changes one bucket key and nothing else.** No fire
instant moves, no predictor is added, the fusion cannot make a single shot late, and the
21:00 session's failure mode (a trim stepping on alternating E,L,E,L because two animation
classes shared one bucket) is exactly what it fixes. Kill switch:
`ORION_ANCHOR_TEMPO_KEY=0` → fall straight back to the meter onset key.

**(b) Inverse-variance fusion with the meter tip — NOT YET.**
Fusing `tip_eta` from two predictors requires both σ's, and the anchor's σ is unmeasured
(§3.4). Fusing with a guessed σ is how the phase/sampler weighting got into trouble before.
Gate: ship (b) only after a 60 fps dump gives a per-bucket residual sd, and only if that sd is
below the meter path's `phase_sigma_ms` (13.0 today) — otherwise inverse-variance weighting
will correctly assign the anchor ~no weight anyway, and the complexity buys nothing.

**(c) NO METER hold predictor for fades — the endgame, third.**
This is the owner's actual ask (*"late fades = animation onset drift, needs the pose anchor"*)
and the case where there is no meter to fuse with, so the anchor is not competing with a 13 ms
σ — it is competing with a **blind fixed hold**, which is a far lower bar. But it is also the
only one of the three where a bad anchor fires a bad shot, so it goes last, behind the beta
label the fades already carry.

**Ship order: (a) now → (b) gated on the 60 fps residual → (c) behind (b).**

**Kill switches, in the shape this codebase already uses:**

| switch | default | effect |
|---|---|---|
| `ORION_POSE_ANCHOR` | **0** | master; 0 makes the whole lane inert (no model load, no thread) |
| `ORION_POSE_ANCHOR_SHADOW` | 1 (when master is on) | compute + log the `anchor_record` only; **never** touches a bucket |
| `ORION_ANCHOR_TEMPO_KEY` | 0 | (a): key the trim's tempo sub-bucket on the anchor instead of the meter onset |
| `ORION_POSE_ANCHOR_HZ` | 30 | duty (§2.3) |
| `ORION_POSE_ANCHOR_MIN_CONF` | 0.35 | below this the anchor abstains and the meter onset key is used |

The abstain path is the important one: **an anchor that cannot see the shooter must fall back,
never refuse** — the same law `player_anchor.py` already states for the nameplate anchor
(`refuse_ok`), and for the same reason (an anchor on the wrong player that is allowed to act
turns a good shot into a wrong one).

---

## 6. Risks

1. **Player lock in 5v5 — this is the top risk, and it is already measured failing.**
   Session B: 2–4 people in frame, and the nearest-to-meter-box rule changes identity on
   **20 % of frames**; the 2K26 cue cache was on the wrong player for **84 % of its shots**.
   The pose model is not the problem and a better pose model will not fix it. Mitigations, in
   order: (i) reuse `player_anchor.py`'s learned-gamertag plate identity — it already solves
   exactly this problem for the meter and is already gated behind
   `ORION_PLAYER_ANCHOR=1` in the ship config; (ii) require track continuity (reject a
   person whose centre moved > 120 px between samples); (iii) log `lock.flips` and drop any
   shot with flips > 0 from the training set. **Any anchor evaluated only on solo-drill data
   is evaluated on the easy case.**

2. **Occlusion.** Not separable from (1) in the present data — 15/17 keypoints held in both
   sessions, so the model is not visibly losing limbs, but a defender between the camera and
   the shooter was never isolated. Needs a labelled 5v5 60 fps dump.

3. **Camera angle.** Everything here is one camera (2K Cam in MyCourt / Theater). Broadcast
   and 2K cam differ in player scale and in whether the shooter faces the camera; the
   scale-normalised, body-relative features in §4.1 are the defence, but it is **untested**.
   The per-(type, tempo) fit will silently absorb a camera change as a bias — so the record
   must carry a camera/mode tag if the anchor is ever used outside MyCourt.

4. **Jumpshot-base drift.** The anchor's constant is per (type, tempo); a new jumpshot base
   changes the animation and therefore the constant. The existing learning machinery already
   handles this shape (decay + reset on a slider move); the anchor's constants should live in
   the same place with the same reset semantics, and a base change should be detectable as a
   step in the per-bucket residual.

5. **Model provenance.** `best.pt` is a 2K26-trained model that we are now measuring on 2K27,
   and its training corpus (`C:\VeniceTraining\no_meter_prep\pose_ds\`) **no longer exists**.
   It transfers fine today (§3.1), but it cannot be retrained or audited. Before the anchor is
   load-bearing, copy `best.pt` into `models/` (it is the head-to-head winner and is currently
   only in a `logs/` training output) and rebuild a corpus — **from a correctly player-locked
   60 fps dump**, not from the poisoned cache.

6. **THE DATA BLOCKER — stated plainly.**
   Nothing in §3 is a verdict on the animation anchor; it is a verdict on 8 fps dumps. To
   answer "which landmark, and with what jitter", we need:

   > **One framedump session with `ORION_FRAMEDUMP_INTERVAL=0`, written to D:.**

   * **Why 0:** the default is 0.2 s; the drill got 8.84 fps and the interpolation floor is
     47.8 ms. At 60 fps the floor is **1.04 ms** (§3.4) — 15× below the 15–25 ms effect.
   * **Where:** `D:` only. C: has ~5.1 GB free; D: has ~425 GB. At the drill's ~0.9 MB/frame,
     60 fps costs **~54 MB/s ≈ 3.2 GB/min**. A **3-minute, ~30-shot** session is ~10 GB —
     fine on D:, impossible on C:.
   * **What it must contain:** ~30 releases with banner verdicts, **a spread of LATE and
     EXCELLENT** (the drill's 9/15 split is ideal), both Standstill and fades, and — for
     risk (1) — a **second session in 5v5 / Theater** even if shorter.
   * **Known hazard:** the drill session wrote 826 of 1421 frames (`dropped=595`,
     `skipped=119`, `stopped:idle`). At 60 fps the writer will be the bottleneck. Check
     `frames.csv` gap percentiles **before** analysing; a dump whose p90 gap is 33 ms is two
     frames, not one, and the interpolation floor scales as bracket².
   * **Do not** re-derive anything from `cue_labels_seq_cache.npz` (§3.1).

---

## 7. What was produced

**Tools (all read-only, all new, all under `tools/diagnostics/`):**

| tool | answers |
|---|---|
| `anchor_pose_bench.py` | §2.1, §2.2 — export time, ORT CUDA/CPU vs ultralytics vs torch, pre/post cost |
| `anchor_pose_bench_trt.py` | §2.2 — input-size scaling (the overhead-bound proof), IO binding, TensorRT EP |
| `anchor_thread_budget.py` | §2.3 — detect-tick lateness with/without a pose thread, at three duties |
| `anchor_drill_pose.py` | §3.1, §3.2 — full-frame pose over the drill dump; crop rule; LATE-vs-EXC on raw pose quantities |
| `anchor_drill_events.py` | §3.3 — wrist-rise crossings vs meter onset vs release, both sessions, by type and by verdict |
| `anchor_sampling_limit.py` | §3.1, §3.4 — track quality, decimation, the 113 → 16.7 ms extrapolation, coupling |
| `anchor_cue_events.py` | §3.1 — the 2K26 cache study (and the finding that it is unusable) |

**Artefacts — `D:\NexusVision\anchor_study\` (~100 MB):**
`pose_cost.json`, `pose_cost_trt.json`, `thread_budget.json`, `sampling_limit.json`,
`drill_pose.npz`, `drill_pose_join.json`, `drill_pose_shots.csv`, `drill_events.json`,
`drill_events.csv`, `cue_events.json`, `cue_events.csv`, `poseA.npz`, `poseB.npz`,
`pose26n_{128,192,256,320,384,448,640}_{fp32,fp16}.onnx`, `trt_cache/`, `logs/`.

**Not touched:** any engine, sidecar, reader, detector or locator source; `settings.json`;
`learning.json`; git.
