# Animation-skeleton timing — NO-METER MODE

> **Purpose: a NO-METER shot-timing mode.** Time the shot purely from the player's animation
> (skeleton), with the shot meter OFF at runtime. The meter is used only OFFLINE to *calibrate* the
> animation→release timing. Status: **design + offline proof + self-trained model + shadow scaffold**.
> Built 2026-06-20. Companion: [`TIMING_AUTONOMY_STATE.md`](TIMING_AUTONOMY_STATE.md).

## Why / what it is
The user wants to shoot with the **meter off** and still green — timing the release from the jump-shot
ANIMATION. The animation is **deterministic per jumpshot** (same shot → same motion → same release
time), so the bot can fire open-loop from a pose landmark with NO meter feedback. This is the same
machinery as the autonomous global clock (Phase C) but anchored on a **pose landmark** instead of the
(now-absent) meter-appear. Two pieces:

1. **Player lock (meter-independent, ESSENTIAL):** at runtime there is no meter, so the user's player
   is located by the **persistent under-player marker / "floatie"** (the controlled-player indicator,
   pink Hue ~166 / Sat ~142 — the very thing the meter detector already characterizes + *rejects* so
   it isn't confused with the meter). Only the user's player shows it -> it inherently excludes the 9
   other players, no on-screen calibration. The pose = the skeleton directly ABOVE the marker.
2. **Release timing (animation-only):** detect a stable early animation landmark (gather / jump-start)
   via the pose, then SCHEDULE the release at `landmark + learned_offset` on the high-res one-shot
   scheduler (sub-frame precise — beats the pose's ~16-33ms frame resolution, which alone is too coarse
   for the ~30ms green window).

## Calibration (the only place the meter is used — OFFLINE)
The meter is the ground truth to LEARN `learned_offset`: record a batch with the meter ON, and the
correlation analyzer measures, per jumpshot, how long after the pose landmark the meter greens. Then
no-meter mode runs that offset with the meter off. So a meter-ON recording is the TRAINING DATA for
the meter-OFF mode. (The earlier "phase anchor vs meter-appear" framing is subsumed: same landmark,
but here it's the sole timing source, not a complement.)

## Key lever
The ~30ms green window vs pose jitter is the risk. A **slower-release jumpshot** does NOT enlarge the
green band (the make-point % is fixed by ratings) -- it makes the animation unfold slower, so the
bot's millisecond-level timing jitter costs less fill-%, i.e. the SAME band is **easier to hit**. See
`TIMING_AUTONOMY_STATE.md`.

## Hard constraints (why shadow-first, not a blind model)
1. **Latency is sacred.** An autogreener minimizes latency; a per-frame pose **NN** adds inference
   latency that can *cost more than it saves*. So: classical CV first (sub-ms), a pose NN only if it
   provably nets an *earlier* anchor after its own latency.
2. **No offline data.** Which detector works on 2K avatars, the landmark→tip timing, and its
   consistency are all unknown without footage. A blind model would overfit / mis-time.
3. **The meter stays authoritative.** The animation anchor is a *phase reference* for the clock, never
   a release trigger on its own. Meter + Phase C remain the release path; the anchor only re-times the
   clock when it's proven more consistent than meter-appear.

## Design
- `animation_anchor.py` — `AnimationAnchor` with a **pluggable** `Detector` (so a classical detector
  or a pose backend are interchangeable). Default OFF.
- **Default detector (classical, latency-safe):** frame-to-frame **motion energy** in the player region
  (a band around/below the meter), tracking the vertical-motion peak = the jump apex. No NN, no new
  dependency, sub-ms. Marked "candidate — validate live."
- **Upgrade path:** a lightweight pose backend (MoveNet/MediaPipe) behind the same interface, *only if*
  the classical anchor proves insufficient AND the latency budget holds.
- **Shadow mode:** settings flag `animation_anchor_shadow` (default OFF). When ON, the orchestrator
  calls the anchor each frame and logs an `AnimAnchor:` line (seq, anchorMs, kind, confidence) next to
  the meter detection — **it never changes release**. (Wiring is the next step after the module + tests.)

## Player locking in real games (which skeleton is the USER's?)
In the practice gym there's one player; in Park/Rec/Pro-Am there are up to 10, so pose detects many.
**Solution: the shot meter IS the identity signal** — 2K draws the meter only at the *user's* shooter,
so the user's player = the detected person **nearest the meter bbox** (which we already detect). This:
- needs **no on-screen calibration / skeleton outlining** (a one-time outline wouldn't survive players
  moving, re-spawning, or camera cuts each possession),
- auto-locks every shot, robust to teammates/opponents in frame,
- only matters when a shot is happening (meter present), which is exactly when we need the anchor.
Implemented in `analyze_pose_meter_correlation.py` (nearest-to-meter person selection); the live anchor
will use the same rule. Fallback when no meter is visible: highest-confidence person.

## Live-development plan (the only way to make it real)
1. Enable `animation_anchor_shadow`, run a labeled live batch on the new-export feed.
2. Analyze (`analyze_anim_anchor.py`, to build): per shot, anchor→tip duration + its **variance vs the
   meter-appear anchor's variance**. Acceptance gate: anchor→tip **p95 variance < meter-appear's** (and
   the anchor fires *before* the tip with margin > its compute latency).
3. Only if it wins: feed it to the engine as the clock's phase anchor (confidence-gated, meter
   authoritative), behind its own control flag, then live A/B vs meter-appear.

## Offline YOLO-pose proof (2026-06-20) — IT WORKS
Ran `tools/diagnostics/yolo_pose_probe.py` (YOLOv8n-pose, GPU) over a real 2K shot-batch recording
(`Videos/2026-06-06 12-47-20.mp4`, practice gym, 120fps):
- **94% player detection** (3294/3500 frames); shooting-wrist keypoint in essentially all.
- **17 release events** auto-detected in 58s at a clean **~2.3s cadence** (the drill pace).
- Release wrist-height **clustered 0.43–0.49** (normalized, 0=top) every shot — a steady, repeatable
  landmark (rest ~0.59 → a clear ~0.21 raise). Annotated frames confirm the skeleton sits on the
  avatar with the arms up at release; the meter + TIMING HUD are co-visible for later correlation.
- Detection confidence is modest (0.14–0.85; a 2K avatar isn't a perfect human) but keypoints land.

So a pose landmark IS extractable and consistent. The **value test still pending** (needs the
correlation): does the pose-release→meter-tip duration vary LESS than the meter-appear→tip duration
(~±73ms)? That's the gate that decides if it replaces the noisy meter-appear anchor. NOTE: YOLO stays
**offline-only** (the probe) — a per-frame pose NN in the live loop would add latency that defeats an
autogreener; the live shadow detector is the cheap classical motion-peak.

## Status
- [x] Design + this doc
- [x] `animation_anchor.py` scaffold (pluggable detector, classical motion-peak default, flag-gated) + synthetic unit tests
- [x] **Offline YOLO-pose proof on a real recording** (`yolo_pose_probe.py`) — player + release detected reliably
- [ ] correlation: pose-release→tip variance vs meter-appear→tip variance (the value gate) — needs the meter on the same frames
- [x] orchestrator shadow wiring + `AnimAnchor:` log line — env `ORION_ANIM_ANCHOR_SHADOW=1`, classical
      motion-peak detector, **compute + log only, never controls release**, fully guarded (try/except,
      flag-OFF default). Logs `AnimAnchor: tMs= kind= conf= motion= meterDet= meterFill=` to the
      sidecar log; `meterFill` at the anchor is the correlation signal.
- [ ] `analyze_anim_anchor.py` (after first shadow batch): variance of meterFill-at-anchor across shots
      (low = consistent phase anchor) + anchor→tip vs meter-appear→tip
- [ ] live consistency gate → (maybe) wire as the clock's phase anchor

## How to run the shadow batch (live, on return)
Set `ORION_ANIM_ANCHOR_SHADOW=1` before launch (alongside the normal run). Play a normal batch; grep
the sidecar log for `AnimAnchor:` lines. If `meterFill` at the anchor clusters tightly across shots,
the landmark is a usable phase anchor and beats the noisy meter-appear; then build the analyzer + wire
it (confidence-gated, meter authoritative). YOLO-pose (offline, `yolo_pose_probe.py`) is the richer
detector if the classical motion-peak proves too coarse.

## Pose model selection (measured on OUR 2K frames, 2026-06-20)
COCO mAP is for real humans; on the 2K avatar the measured numbers (40 frames, conf floor 0.10):
`yolov8n` 82% detect / 0.69 conf / 13.2 kp ; `yolov8s/m/x` & `yolo11x` = **100% detect**, conf up to
0.88, keypoints **plateau ~12.4-13.2/17**. Takeaways: (1) bigger model = no missed frames + higher
confidence — free win OFFLINE (latency irrelevant); (2) the ~4 weak keypoints are the **domain gap**
(stylized avatar), unchanged by size — only fine-tuning fixes that; (3) the shooting-arm keypoints we
anchor on are in the reliable set. So: teacher = `yolov8x/11x-pose`, student = `yolo11n-pose`.

## Self-training on our recordings (no hand labels) — `tools/training/`
1. `pose_pseudolabel_dataset.py` — run the x-pose TEACHER over the 2K recordings, write high-confidence
   single-player detections as YOLO-pose labels (built 308 frames: 245 train / 63 val from 6 clips).
2. `pose_finetune.py` — fine-tune the nano STUDENT (`yolo11n-pose`) on those pseudo-labels -> a fast,
   2K-domain-adapted model.
3. `pose_eval_compare.py` — base nano vs fine-tuned on the held-out val split.
The student adapts to the 2K look (detection/confidence/speed) but can't exceed the teacher on the
domain-gap keypoints; for the anchor that's fine. More + more-diverse frames (all high-conf clips)
improve generalization; a few HAND-labeled frames are the only way past the keypoint ceiling.

**RESULT (2026-06-20, fine-tuned yolo11n-pose, 30 epochs early-stopped, 63 held-out val frames):**
| model | detect% | conf | kp>0.5 |
|---|---|---|---|
| yolov8n-pose (base) | 68% | 0.47 | 14.3 |
| yolo11n-pose (base) | 59% | 0.43 | 13.9 |
| yolov8x-pose (teacher) | 100% | 0.72 | 12.5 |
| **fine-tuned nano (ours)** | **100%** | **0.84** | **14.6** |

The 2K-adapted nano reaches **100% detection** (base nano 59-68% on these harder frames) at **0.84
conf — above the x-pose teacher** — at nano speed. Weights: `models/orion_pose2k_n.pt` (gitignored).
Caveat: trained on only 308 self-labeled frames from 6 clips, so high conf partly = domain
specialization; validate generalization with a bigger/more-diverse pseudo-label set before trusting it
broadly. But the pipeline + the gain are proven.

## Low-latency recipe (if the pose anchor ever runs LIVE)
Biggest wins first: **ROI-crop to the player** (use the meter bbox -> ~200x200 input vs 1080p: huge
speedup AND the player is larger = more accurate) -> **nano student** -> **FP16 / TensorRT export**
(torch is CUDA cu126 -> 2-5x) -> **separate thread + shot-gated** (run only when the meter appears; the
anchor is an EARLY signal so its few ms are absorbed by the lead). Realistically single-digit-ms ->
live-viable. (Default live path stays the classical motion-peak until this is proven worth it.)

## Steps 1-3 results (2026-06-20 autonomous run)
1. **Scale + retrain:** 1799 self-labeled frames / 16 clips -> fine-tuned `yolo11n-pose` (`models/
   orion_pose2k_n.pt`): **100% detect / 0.88 conf / 14.4 kp** on 342 held-out frames (base nano
   69-86% / 0.35-0.39; above the x-pose teacher's 0.74 conf), at nano speed. Generalizes.
2. **Calibration (pose<->meter value gate):** pose detection 99.5%; the (crude wrist-minima) release
   landmark fires at meter fill **74% +- 13% std** (n=19). VERDICT **MODERATE** -- clearly tighter than
   the meter-appear anchor (spans 34-97%) but not yet < the ~8% green band. Tighten via an EARLIER,
   more repeatable landmark (jump-start) + the learned-offset SCHEDULE (don't fire on the noisy frame)
   + cleaner meter pairing (recording meter-detect was only 58%; the live feed is cleaner).
3. **Latency:** ~12ms/frame (~83fps) on an RTX 3060 via `ultralytics.predict()` -- OVERHEAD-bound (nano
   compute is tiny; ROI-crop + FP16 do NOT help because the cost is the predict() wrapper, not the GPU).
   Adequate for the anchor (keeps up with frame rate; release is scheduled off an early landmark so the
   inference latency is absorbed). Single-digit-ms only needs raw `model.forward` + minimal decode or
   TensorRT -- NOT ROI/FP16. Corrects the earlier "ROI->single-digit" recipe.

**Next refinement (the path to STRONG):** replace the wrist-minima release landmark with a jump-START
detector (earlier + more repeatable), schedule release at `jumpStart + learned_offset`, and validate
the offset's std on a real-game (Park/Rec) recording with the floatie player-lock.
