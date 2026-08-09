# Orion — Compressed-path (Chiaki, capture-card-less) Design (grounded, 2026-07-09)

Design only — plugs into existing functions. Path: `ORION_COMPRESSED_READER=1` → `CompressedMeterReader` (subclasses `SimpleMeterReader`) + `luma_meter.py` + `stream_quality.py`, fed BT.709 frames from `chiaki_backend._to_bgr`.

## 🐞 TWO real bugs found while grounding (fix these FIRST — offline, pure Python, ~free)
1. **`set_stream_profile()` is NEVER called.** The orchestrator builds the reader (`remote_play_orchestrator.py:536`) but never calls `set_stream_profile` → `QualityEstimator` starts at `prior=1.0` (**pristine**) on a 4Mbps stream → the luma branches gate OFF exactly when needed (the `:466` code comment documents this exact failure). **Fix:** call `self._meter_detector.set_stream_profile(w,h,fps,bitrate_kbps)` from the Chiaki rung config right after `:536`. The whole quality controller is currently mis-initialized. **This is prerequisite for everything else to even be measurable.**
2. **`MeterReaderY` (the trained Y-student) is wired to NOTHING.** `meter_reader_y_infer.py` `read()`/`verify()` are built + tested + shelved — no caller. Wire `verify()` as a CONFIRMER only (relax-never-suppress, like `set_acquire_hint`).

## 1. Luma fill-read + green-tip anchor (720p/4M, blur, 4:2:0) — offline-buildable
Spine exists (`_read_fill:451` → chroma-collapse → K2 luma fit `luma_meter.fit_fill_boundary` IRLS-Huber logistic). Upgrades:
- **(a) Velocity-aware edge de-lag** — the logistic midpoint `y0` lags the instantaneous leading edge on fast rise (fill p90 tail 14-35pp); bias `+k·sign(vel)·min(s,|vel|·Δt·px/pct)`, quality-gated (low q_frame + |vel|>STALE_VEL_MIN) so pristine untouched.
- **(b) Per-column robust boundary** — `prof.mean(axis=1)` (`:487`) lets one ringing macroblock column bias the mean; fit on 3 vertical-third sub-means, take median y0.
- **(c) Temporal tip-lock** — the tip row is a per-shot invariant; once `fillable_h` stabilizes, predict the tip row + run `find_tip_sliver` as a ±window refinement (warm-start like `prev_y0`), not cold every frame.
- **Native Y plane** — `_split_planes:1176` already makes a zero-copy Y view, then `_to_bgr` throws it away + the reader does BGR2GRAY (`:485`) — a BT.709 round-trip to rebuild luma it had. Pass Y through (backlog TIER-1 #5). Near-free plumbing.

## 2. Shape acquire that survives compression — offline
Keep `luma_meter.rail_pair_scan` (0.6ms, no model, interior-veto décor guard) as primary. Changes:
- **(a) Enable the quality-gated UNARMED luma acquire** — `_luma_acquire:211` hard-returns None when `not armed`, but at 4M chroma acquire is blind so the early rise is lost pre-arm. Enable unarmed rail acquire ONLY when q_session low + chroma dead N frames, requiring `e2 AND e3 AND h>=_h_acq` + décor registry + the **E4 rise-confirm** (`read():399-421`, zeroes any lock whose fill never rises). E4 makes unarmed acquire non-catastrophic. **Biggest CV recall lever.**
- **(b) Two-luma-cue geometric corroboration** — port the pop-in prior (`simple_meter_reader.py:939-948`): bright tip sliver a fixed ~150-165px above the dark→mid step, centred. Décor guard, no model.
- **(c) `MeterReaderY.verify()` as confirmer only** (closes bug #2) — confirms a rail candidate under total chroma collapse + shrinks the scan region; a lock still requires rails + evidence + E4.

## 3. Quality→variance controller — ~90% built; close the loop
Collapse contract asserted (`for_quality(1.0)==ReaderParams()`). Two-timescale split correct (q_frame→R continuous; q_session→discrete + luma gate via Schmitt). Work: **wire the rung prior (bug #1)** so q_session starts in the right band; **actually consume `R`** (confirm `FillKalman.update(r=)` gets fed, not just logged); fail-open guard already correct.

## 4. Training recipe
**Offline-buildable NOW (before 07-15):** codec-augmented synthetic Y crops — `gen_meter_reader_synth_y.py --n-sequences 500` (sequence-encode → harvest a P-frame, per-rung bitrate, untagged colorspace for self-consistency) → train `MeterReaderNetY` (heads: fill+green, present, logvar σ) via `train_meter_reader_y.py`. YOLO `--luma` retrain judged PER-PHASE (`eval_meter_locator.py --meta`, never aggregate). Rung sweep `tools/quality/rung_replay_eval.py` — use for recall/fill-error, NOT temporal params (harness ~15fps, 4× off the 60fps design point).
**Needs live dual-capture (post-07-15):** teacher→student distillation — `dual_capture_recorder.py` + `dual_capture_align.py` (Theil-Sen CFR align, <5ms). Pristine reader on the pts-aligned teacher = soft label for the compressed student Y crop. Non-optional because synth runs untagged bt601 to isolate compression, but live is BT.709 — only dual-capture exercises real colorspace + green-tip survival.

## 5. P(made) ceiling per rung + highest-leverage move
Detection recall 92-96% at every rung (post BT.709 fix); what dies = green tip (mask 0.0→luma sliver), early-rise recall (79-90%), fill p90 tail (14-35pp). P(made) = acquire-in-time × fill-accurate × no-transport-spike:

| Rung | P(made) ceiling | Binding constraint |
|---|---|---|
| 1080p60 HEVC 30-50M wired | 88-92% | transport jitter |
| 720p60 12M (Performance) | 82-88% | early-rise + fill tail |
| 720p60 4M (Balanced, default) | 68-78% | acquire-blind chroma + green-tip death |
| 540p/360p ~1.2-2M | 55-68% | meter 7-11px wide, resolution-bound |

Transport jitter is the floor, not CV — a capture card or Ethernet buys back the gap.
**Single highest-leverage move: request an IDR at shot-arm + shorten GOP for the shot window** (`stream_connection_send_idr_request` exists in the fork `lib/src/streamconnection.c:1285`; `force_iframe_interval` is parsed-but-INERT, never added to launch). Raises source SNR for the 200-400ms the timing reads → attacks green-tip + early-rise + fill-tail at the ROOT. Fork work (M6), needs live + coordinate with the timing session.
**DO FIRST (offline, unlocks the above):** wire `set_stream_profile` (bug #1) + enable unarmed luma acquire (§2a with E4 safety). Pure Python, prerequisite for the IDR win to be measurable.

## Ladder-eval empirical result (07-09, corroborates the above)
Ran the adaptive `CompressedMeterReader` on re-encoded framedumps across 4 rungs (dup=realistic bit budget, seq=motion-stress). Findings:
- **Detection survives pure compression** (dup 92-98%) but **collapses under motion at low bitrate** (seq 57-86%) → the floor is TEMPORAL (P-frame motion × low bitrate), NOT spatial quality.
- **RED chroma is the fragile channel** — survival 0.6-0.7 at rest → **0.13-0.26 under motion** (red desaturates first = the acquisition weak link).
- **Early-rise dies first** (the market-critical phase; timing fires on the rise).
- **Green tip NOT trustworthy on compressed** — chroma cap survival 0.0 (Jul-6 large-sample); luma-sliver replacement non-monotonic (over-fires on low-bitrate blocking 195-753 anchors vs 200 clean, under-fires on high-bitrate softening 27). → **new offline-fixable item: stabilize the luma green/tip anchor (require chroma corroboration OR temporal persistence before emitting green).**
- **Fill MAE UNFALSIFIABLE offline** (~30-46pp even reader-vs-itself) — stateful-reader divergence over the long armed session (the 180-frame coast, `e111fce5`) swamps it. Needs dual-capture ground truth (07-15).
- **Harness caveat:** `e111fce5` coasts 787/1327 frames → collapses the SimpleMeterReader fresh-detection base (542→112) → cross-reader ladder comparisons are noisy. Fix: count armed-hold as "detected" in the harness det-rule, or re-arm per shot.
- **Live-gated → 07-15:** BT.709 red-recovery magnitude (harness does a self-consistent bt601 round-trip — can't test the live decode matrix), motion tuning of stale-gate/vel_win/coast (harness ~15fps, 4× off the 60fps design point), real PS5 hardware-encoder artifacts, fill ground truth.
