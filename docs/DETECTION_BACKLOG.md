# Meter-Detection Backlog — 8-AI brainstorm × 5-agent code-review (2026-07-06)

> **CORRECTION (2026-07-06, per a 5-agent Chiaki review):** the "Two architecture facts" below over-scoped — "Remote Play = controller inject only" and "H.264/4:2:0 ideas moot" are true *for the capture-card path*, but the **Chiaki decoded H.264 stream is a real, supported detector source** (`frame_source='decoder'`, `remote_play_cv.py`) used by every capture-card-less user. The compression ideas are NOT moot for them — see `docs/CHIAKI_STREAM_BACKLOG.md`. This doc is the *capture-card* (currently-tuned) path only.

## How this was produced
Eight external models (Sonnet-5, three Fable-5, two Gemini, GLM-5.2, two GPT-5) brainstormed the detection problem; five agents (one per vendor) ground-truthed every idea against the actual detector code. Same outcome shape as the timing backlog: **most ideas already exist (often stronger); the real wins are a few bugs + a few net-new fixes.**

## Two architecture facts that reshape the AI advice
1. **The detector reads a near-pristine HDMI CAPTURE-CARD 1080p60 MJPG feed** (`capture_card_backend.py`), NOT the Remote-Play H.264 stream (Remote Play = controller inject only). So **every "recover the 8px meter from H.264 4:2:0 macroblock/P-frame smear" idea is moot** — MJPG is intra-frame, high-bitrate. Only static 8×8 JPEG quantization remains, and it's slight.
2. **Precision is already largely solved.** `subpixel_fill_metrics` already does Gaussian-crossing + parabolic sub-pixel to ±0.2 row (Gemini found the formula ships *verbatim*). The dominant errors are **serve-gating, a quantized SERVED-fill path, and the total absence of a ground-truth oracle** — not edge precision.

## The two live bugs (confirmed by all 5 agents — fix first)
- **`_peak_hold_fi` cross-track leak (a regression we shipped in 8268cdd7).** One scalar on `_ParkTracker` (`:2553`) is written by *any* peaking track (`:2767`) and read for *all* tracks (`:2644`), so while any track peaks a brand-new static-red blob can lock while **skipping the rise gates for 24 frames (0.4–0.8 s)** — right during peak→release→deflation. Per-track fix (~4 lines): add `peak_hold_fi` to `_Tk.__slots__`, scope the write/read to `t.`, delete the shared scalar. Preserves the intended re-lock, closes the leak.
- **`loc_strong` corroboration bypass (`:1454`).** With `min_consecutive_valid_frames=1`, a single conf≥0.55 locator box latches on décor with **zero** rise/green/motion evidence. Fix: require it paired — `loc_strong and (rising or green_ok or recent_prox)`. (The v7 locator retrain already attacks the root cause; this is belt-and-suspenders.)

## TIER 1 — bugs + near-free served-path fixes
| # | Item | Why | Status | Effort |
|---|---|---|---|---|
| 1 | **Fix `_peak_hold_fi` per-track** | Shipped regression; false-lock amnesty at the worst moment | Bug | ~4 lines |
| 2 | **Pair `loc_strong` with a behavioral signal** | Single-frame décor latch | Bug | ~1 line |
| 3 | **Sub-pixel the SERVED `_measure_track` fill-top** | The served fill (feeds validator + native velocity) uses *integer box edges* (`fill_top=my`, `:3894`) → ~1–2% quantization. Reuse the existing `_gaussian_threshold_crossing` interpolator. No model, no CNN | Net-new (near-free) | S |
| 4 | **Reference fill to the TIP (distance-to-tip), not box height** | Served fill is normalized by box height (`:1094`) → box jitter injects straight into fill%. Rectified-crop tip-anchor differencing cancels it | Net-new | S–M |

## TIER 2 — the oracle + the quality coupling (the real force-multipliers)
| # | Item | Why | Priority |
|---|---|---|---|
| 5 | **Independent timing oracle + release-window / per-phase served-recall metric** | **4 of 5 agents independently converged here.** There is NO ground-truth oracle today — the "true tip" is the argmax of the same signal in hindsight, so every offline metric measures *smoothness/self-agreement*, not correctness ("a reader can be smoothly, agreeingly wrong"). This is the ruler you measure every other change with, AND the structural fix for the aggregate-recall trap that caused the live 0/28. **Build before any refactor.** | **P1** |
| 6 | **Surface the sub-pixel fit RESIDUAL as a per-frame quality signal into timing abstain** (GPT's best) | The residual is *already computed and discarded*; feeding it to the release-abstain logic couples detection-quality → timing-confidence, and is *better on the pristine feed* (true edge sharpness, not codec noise) | P1 |
| 7 | **Per-STATE recall as a model-promotion CI gate** (GLM's best) | Blocks a model shipping on phase-bucketed recall (rise/cap/deflate) instead of aggregate — the training-code metrics already exist | P2 |

## TIER 3 — bigger, conditional
- **Reader-architecture fix** — the shipped CNN reader global-average-pools away vertical position (why it loses to row-counting); a 1D soft-argmax head fixes it, *or* wire+validate the already-built `MeterROINet` row-regression model. **Only if Tier-1 #3/#4 aren't enough** — the served-path sub-pixel is cheaper.
- **Hard-negative mining** from `session_20260706_043919` (100% décor false locks — free labels) → next locator retrain. Attacks the false-lock root.
- **Player-motion coherence** corroborator (the v9 player model exists but isn't wired) — a décor discriminator for `loc_strong` latches. Medium.
- **Auto-color as background-vs-ANY-fill (LDA)** — only if a non-red meter ships (auto-color is dormant; Red effectively pinned).
- **Phase-aware state machine** (formalize the implicit ACQUIRE/LOCKED/peaked/warm states) — **LAST.** It would make bug classes like #1/#2 structurally impossible, but it's a high-risk rewrite of the most load-bearing, most-patched code; do it only after the oracle (#5) can prove no regression.

## DISCARD / already-shipped (don't rebuild)
NCC ROI tracker (already 0.12px @0.1ms, on the appearance-stable *notch* not the changing fill), parabolic sub-pixel edge (ships verbatim), ROI-lock/re-detect, dual-range green mask, kinematic FP gate (superseded by rise-displacement), KCF / micro-CNN trackers (worse than NCC-on-notch + drags torch onto the hot path), all H.264/4:2:0-compression ideas (moot on MJPG), sub-frame interpolation, camera-motion estimator (doesn't exist; costs more than assumed for the weakest payoff).

## One-line verdict
**Kill the two bugs, sub-pixel + tip-anchor the *served* fill, build the served-recall oracle/metric, couple the fit-residual into timing.** The precision is already there; the detector was losing to its own serve-gating, a quantized served-fill, and having no ruler to measure correctness. Nothing here needs a big new model.
