# Chiaki-Stream (Compressed) Detection Backlog — 9-AI brainstorm × 5-agent code-review (2026-07-06)

## Context
Meter detection on the **compressed PS5 Remote Play stream** (Chiaki decoded H.264/HEVC 4:2:0, default Balanced=720p60 ~4 Mbps, ladder to 1080p/HEVC/30-50 Mbps) — the video source for **users without a capture card**. It is a real, supported detector source (`remote_play_orchestrator.py` `frame_source='decoder'`, `remote_play_cv.py`), NOT "inject only." Nine external models brainstormed; five agents ground-truthed every idea against the code. **`DETECTION_BACKLOG.md`'s "Remote Play = inject only / moot on MJPG" was a genuine defect — corrected there.**

## Verified corrections (agents audited the "free asset" claims — several are less free than claimed)
- **`is_iframe` is a DEAD field** — declared on `FrameData` but *never produced* (the 28-byte wire header `<IIIIIIq` has no I-frame slot) and never read. Harvesting frame-type needs **fork work on both ends** (emit from the decoder + widen the header). Not a free wire-in.
- **`force_iframe_interval=8` is INERT** — parsed from config but never added to the OrionStream launch command, so the ~133 ms GOP is aspirational, not in effect.
- **There is no "fit residual"** in `_gaussian_threshold_crossing` (it's a 3-point parabolic interp → exact by construction). What's computed-and-discarded is the **curvature** (`denom = pm-2·p0+pp`, `meter_detector.py:992`) — a legitimate edge-sharpness/quality proxy. Surface *that*.
- **The current sub-pixel reader is a fixed 20%-threshold crossing on a *binarized chroma* (red-mask) profile** — doubly compression-fragile (chroma is what 4:2:0 kills; a fixed threshold drifts as blur varies). This is the real fill weak-link.
- **`_ParkTracker` ACQUIRES on pure-red HSV (`_S_MIN=180`, chroma) but TRACKS on a luma notch NCC (0.12 px).** Confirmed. The compression-fragile half is *acquire*; the tracker is already luma-robust.

## TIER 0 — foundation (build first; everything downstream needs these)
- **The served-recall oracle** (already `DETECTION_BACKLOG.md` P1) — a ruler that measures *correctness*, not self-agreement. Predict-and-commit, retrains, and quality-tiers are all unfalsifiable without it.
- **Dual-capture self-labeling rig** (Sonnet's best) — run capture-card + Chiaki off one console simultaneously, `pts`-align → the pristine detector's output is *free ground-truth* for the compressed frames. Real new orchestration (`frame_source` is single-select today), but it reuses both existing backends and is the enabler for distillation + augmentation validation.

## TIER 1 — ship-now, NO fork work, high-confidence
| # | Item | Why | Priority |
|---|---|---|---|
| 1 | **Luma-edge thin-column acquire** parallel to `_red_mask` (`meter_detector.py:2585`) | GLM's best *shippable* pick. Acquire is where the compressed path dies (chroma HSV `_S_MIN=180` desaturates under 4:2:0); add a luma-gradient candidate. No fork work. | **P1** |
| 2 | **Luma + FREE-WIDTH SIGMOID fit on a spline-interpolated profile** — replaces the fixed-threshold-on-chroma reader | Gemini's best. Center stays unbiased as blur varies; fitted width = quality signal; spline (not neural SR) adds zero fabricated structure. **Helps the capture-card path too.** | **P1** |
| 3 | **Stale-frame-as-missing-data** — SAD/NCC the ROI vs the previous frame; encoder-skip ⇒ coast the filter, don't feed it, don't decay lock/rise-gate | Fable's best. Dissolves the dominant low-bitrate failure (P-frame stall→jump). Reuses the 0.1 ms notch-NCC; luma-variance is a local proxy for "stale" — no `is_iframe` needed. | **P1** |
| 4 | **Curvature → FillKalman adaptive R** (surface `denom` at `:992`) | Cheapest win — the number is already computed. Edge-sharpness → measurement noise. | **P1** |
| 5 | **NV12 end-to-end** — ship the native NV12 (pipe already speaks `_FMT_NV12=0`), skip the redundant NV12→BGRA | Near-free plumbing: saves ~1 ms + dodges a BT.709 colorspace hazard + hands the reader the native luma plane. (Not "unsmear luma" — BGRA preserves the Y plane.) | **P1 (cheap)** |

## TIER 2 — fork + data (the structural compressed wins)
- **Frame-type export** — emit `pict_type` from the OrionStream decoder + widen the wire header → populate `FrameData.is_iframe` → then **I-frame innovation-gating** (trust I-frame corrections, damp P-frame) and **on-demand IDR at shot-start** (bounds drift to one GOP). *Also actually plumb `force_iframe_interval` into the launch command.*
- **H.264-retargeted codec augmentation** — the synth degrade (`cheap_card_degrade`/`hdmi_degrade`) is MJPEG/spatial-only; add `rp_codec_burst_degrade` (ffmpeg **sequence** encode, gop=8, extract a P-frame) to reproduce the temporal artifacts stills cannot. Prerequisite for a 720p-compressed retrain.
- **Pristine→compressed distillation** (GPT's best) — capture-card teacher supervises a compressed student (learns *what compression destroyed*); uniquely enabled by owning both the pristine source and the fork. Needs the dual-capture rig. Cheap on-ramp: per-source BN/adapter head.
- **Quality-as-variances, one codepath** (all AIs) — a small quality estimator (curvature width + luma-HF energy + chroma/luma-edge ratio) drives R / gates / coast / temporal-window; capture-card collapses to "everything tight" = today's tuned path (zero regression by construction).

## TIER 3 — later / conditional
- **Predict-and-commit inversion** (GLM's north star) — on the compressed tier, predict the tip from the *sharp mid-rise* (via `fill_forecaster_infer` + `FillKalman.ms_to_target`) and use the smeared tip only as confirmation. Highest ceiling, but **gated on the oracle** (can't tell early-correct from early-wrong without it).
- **MV/QP export** (`AV_CODEC_FLAG2_EXPORT_MVS`) → MV-assisted Kalman coast. Harder fork work; the codec's own motion belief tracks the same lag the image shows.
- **Edge-locked temporal stacking** (temporal super-resolution powered by the fill's own motion) + **keypoint/soft-argmax reader** for the 720p tail (overlaps the existing `MeterROINet`).

## DROP (verified redundant/weak)
Full IMM / multi-hypothesis rewrite (formalizes coast+jump-gate+3-strike behaviors `MeterBoxKalman` already has; the real décor-drift was the two bugs, now fixed) · HOG-KCF (lost to notch-NCC) · macroblock-phase "sawtooth" (speculative; the fitted width absorbs blur bias) · template-subtraction (undermined by the translating HUD) · neural ROI super-resolution *for measurement* (hallucinates edges off macroblocks — use the 1D spline) · motion-aligned temporal denoise (repo-deprioritized; edge precision isn't the bottleneck).

## Honest ceiling
~**85–92%** at good 1080p/high-bitrate on a wired LAN; ~**65–80%** at 720p/low-bitrate. **Transport jitter (video + input both over the network) is the floor**, not CV. Right response: surface a live link-quality grade, use predict-and-commit + a ~95%-fill safety-release fallback, and be honest that a capture card (or Ethernet) buys back the gap.

## Build order (verified)
oracle + dual-capture rig → **#1 luma acquire + #2 sigmoid/spline/luma reader + #3 stale-frame coast + #4 curvature→R + #5 NV12** (all no-fork) → H.264 augmentation + 720p retrain → fork(frame-type export) → I-frame gating + predict-and-commit → quality-as-variances → MV/QP export.
