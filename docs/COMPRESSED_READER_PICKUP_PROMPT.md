# PICKUP PROMPT — Compressed-Stream (Chiaki) Meter Reader

> Paste this to a fresh Claude session working in C:\Users\Administrator\Desktop\NexusVision.

You are picking up the **luma-adapted meter reader for the compressed Chiaki stream** — the
detection path for users WITHOUT a capture card (PS5 Remote Play, H.264 4:2:0, default
720p60). The design plan (approved 2026-07-07) and milestones M0–M5 tooling + M7 pipeline
are BUILT, TESTED (148 tests green), and COMMITTED. Your job is the remaining work listed
under NEXT, using the artifacts below.

## What exists (commits, this branch: feat/detection-template-anchor-qml-render)

- `f0089b54` — **M0–M4 core.** `CompressedMeterReader(SimpleMeterReader)` (flag
  `ORION_COMPRESSED_READER=1`, orchestrator-selected): chroma-first with K2 robust logistic
  LUMA fill fit + bright tip-sliver anchor (`luma_meter.py`), armed-only K1 rail-pair
  acquisition with E1–E5 evidence bits + décor registry, encoder-skip stale gate with a
  frozen-echo run-cap, quality layer (`stream_quality.py`: q_frame→R, Schmitt-banded
  q_session→`ReaderParams.for_quality(q)`; `for_quality(1.0)==ReaderParams()` is an asserted
  identity). Collapse contract on real framedumps: byte-parity with SimpleMeterReader with
  luma off; bounded additive-only divergence with it on. Also: `ReaderParams` refactor,
  PTS→epoch oracle feed (`_frame_measurement_epoch_ms`), detframes +10 quality columns,
  default rung Balanced→Performance (720p60/12Mbps — Performance was unreachable before),
  `FillKalman.update(r=)` + `predict_to()`. My orchestrator/chiaki_backend/sidecar edits
  were swept into the timing session's `43abdd25`.
- `0d930159` — **M5 rig.** `tools/diagnostics/dual_capture_recorder.py` (teacher capture
  card + student ORFR pipe, shot-gated lossless NV12 archive, retro armed re-run, inline
  activity proxy) + `dual_capture_align.py` (Theil-Sen CFR → QPC coarse → a·pts+b
  correlation → per-shot centroid residuals; **<5 ms mapping proven on synthetic truth**;
  label emit with QC gates + the sx=sy=2/3 affine tripwire).
- `5761bf72` — **M7 pipeline + M4 harness** (4 parallel agents, integrated):
  `gen_meter_reader_synth_y.py` (codec-augmented Y crops), `train_meter_reader_y.py` +
  `meter_reader_y_infer.py` (MeterReaderNet-Y: fill/green/present/log_var heads, flag
  `ORION_METER_READER_Y`; generator→trainer→infer handshake verified e2e),
  `train_meter_detector.py --luma` + `eval_meter_locator.py --meta` (per-phase recall),
  `tools/quality/rung_replay_eval.py` (adaptive vs frozen vs pristine per rung).

## The two discoveries that reframed the problem

1. **BT.601/709 hue bug (fixed, needs live validation).** The live pipe decoded the PS5's
   BT.709 stream with cv2's BT.601 constants — measured: the matrix mismatch ALONE zeroes
   the red inRange mask at 12 Mbps. Fixed in `chiaki_backend._to_bgr` (BT.709 via
   cv2.transform, `ORION_PIPE_BT709` default on). Much of the historical "compression kills
   red acquisition" may be THIS. Never tag bt709 in ffmpeg without matching the decode
   matrix (cv2.VideoCapture decodes bt601).
2. **Chroma survives honest re-encoding** (`tools/diagnostics/reencode_ladder.py`,
   report in `logs/diagnostics/reencode_ladder/`): 92–96% detection recall at EVERY rung
   incl. 360p/1.2Mbps. What dies: the GREEN TIP (median mask survival 0.0 → the luma
   sliver anchor), early-rise recall (79–90%), and the fill-error tail (p90 14–35pp).

## NEXT (priority order)

1. **Live smoke on the decoder path** (needs the rig): `ORION_COMPRESSED_READER=1` on a
   real Chiaki session at Performance (12M) and Balanced (4M). First question: how much
   chroma acquisition did the BT.709 fix alone recover? Check detframes q_frame/q_session/
   fill_path columns.
2. **Dual-capture session** (user at the PS5, ~30 min MyCourt):
   `python tools/diagnostics/dual_capture_recorder.py --out logs/dual/S1 --rung Balanced`
   (preflight in its docstring: PS5 1080p60/SDR, bot NOT in decoder mode, card free), then
   `python tools/diagnostics/dual_capture_align.py logs/dual/S1`. Validate the align_report
   (stage2_score, delta scatter, affine tripwire) before recording the full ladder campaign.
3. **Real training runs** once S-sessions exist: gen_meter_reader_synth_y (full 500) +
   teacher-labeled crops → train_meter_reader_y; YOLO `--luma` retrain; judge on per-phase
   recall (eval_meter_locator --meta), NEVER aggregate.
4. **K3 tip-registration serving** (promote tools/timing/tip_registration_eval.py to
   serving, PTS-domain, publish via predicted_tip_ms): ⚠ COORDINATE FIRST — a concurrent
   timing session owns that orchestrator region (see its Phase-0/1 commits).
5. **M6 fork work** (with a live rig): ORF2 header (pict_type; bump magic, widen
   `chiaki_backend._HEADER`) + IDR-at-shot-arm (`stream_connection_send_idr_request`
   exists at lib/src/streamconnection.c:1285 in C:\Users\Administrator\Desktop\chiaki-ng-src).

## Traps (all learned the hard way this session)

- **Run pytest via a `python - <<'EOF'` heredoc calling `pytest.main([...])`** — the RTK
  shell hook rewrites bare pytest commands and can show STALE summaries.
- **Never put tools/diagnostics on sys.path in tests** — its retired
  `simple_meter_reader.py` prototype shadows the root module for every test collected
  after. Load tools by explicit importlib path and register in `sys.modules` BEFORE
  `exec_module` (Python 3.14 dataclasses require it).
- **A concurrent session works this same tree** (timing Phase 0/1). Stage files
  individually; verify `git diff --stat <file>` attribution before committing.
- **The rung harness/probe runs at the framedump's ~15 fps cadence** — all time-tuned
  machinery (stale gate, vel_win, coast decays) runs 4× off its 60 fps design point there.
  Adaptive-row peak-recall deficits on the harness are probably probe artifacts: do NOT
  tune reader temporal parameters against it; wait for dual-capture 60 fps ground truth.
  Harness encodes are threads=1 (deterministic) — keep them that way.
- The pipe is single-consumer; the dual-capture recorder must be its only reader.

## Verify-everything command

```
python - <<'EOF'
import pytest
pytest.main(["tests/test_compressed_meter_reader.py","tests/test_stream_quality.py",
 "tests/test_luma_fit.py","tests/test_pipe_bt709.py","tests/test_dual_capture.py",
 "tests/test_meter_reader_y.py","tests/test_gen_meter_reader_synth_y.py",
 "tests/test_luma_train_plumbing.py","tests/test_rung_replay_eval.py",
 "tests/test_simple_meter_reader.py","tests/test_simple_reader_sidecar_wiring.py",
 "tests/test_fill_kalman_quadruples.py","tests/test_release_marker_relay.py",
 "tests/test_detframes_schema.py","-q"])
EOF
```

Full design rationale: the approved plan (memory: `compressed-reader-shipped`), the error
budget + ceilings (P(green) ≈0.78 @720p60/12M, ≈0.69 @4M, wired LAN), and
docs/CHIAKI_STREAM_BACKLOG.md (pre-dates the SimpleMeterReader pivot — this work re-based it).
