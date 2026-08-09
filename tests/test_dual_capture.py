"""Dual-capture rig (M5): alignment math against synthetic ground truth + the recorder's
shot-gated state machine and archive plumbing with fake taps.

The whole point of testing here: when the real play session happens, only I/O can fail --
every algorithmic claim (Theil-Sen CFR, the a*pts+b correlation fit, per-shot residuals,
label interpolation + QC gates) is already proven on scenarios with KNOWN truth.
"""
import importlib.util
import json
import os
import sys
import types

import numpy as np
import pytest


def _load_tool(name, filename):
    """Load a tools/diagnostics module by EXPLICIT path: putting that directory on
    sys.path would let its retired simple_meter_reader.py prototype shadow the root
    module for every test collected after this one."""
    path = os.path.join(os.path.dirname(__file__), "..", "tools", "diagnostics", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_align = _load_tool("_dc_align_ut", "dual_capture_align.py")
_rec = _load_tool("_dc_recorder_ut", "dual_capture_recorder.py")

theil_sen = _align.theil_sen
smooth_teacher_timeline = _align.smooth_teacher_timeline
despike = _align.despike
rise_rate_trace = _align.rise_rate_trace
fit_time_map = _align.fit_time_map
shot_windows = _align.shot_windows
per_shot_residual = _align.per_shot_residual
refine_with_shots = _align.refine_with_shots
isotonic_residual = _align.isotonic_residual
transfer_labels = _align.transfer_labels
ShotGate = _rec.ShotGate
DualCaptureRecorder = _rec.DualCaptureRecorder
band_activity = _rec.band_activity

RNG = np.random.default_rng(20260707)


# --------------------------------------------------------------------------- #
#  synthetic ground-truth session
# --------------------------------------------------------------------------- #
A_TRUE = 1.0 + 37e-6            # 37 ppm crystal drift
B_TRUE = 123.456                # s: teacher epoch = A*pts + B


def _make_session(n_shots=6, fps=60.0, shot_len_s=0.5, gap_s=5.0, noise_pp=1.0):
    """Teacher: 60fps epoch timeline with fill ramps. Student: pts timeline whose TRUE
    mapping to teacher time is A_TRUE*pts + B_TRUE; activity = rise motion + noise.
    Shot starts carry a RANDOM sub-frame phase: game animations are not vsync-locked to
    the capture grid, and a phase-locked synthetic session would make every shot's
    edge-quantization error identical -- a systematic bias no aggregator could average,
    which real sessions do not have."""
    dur = n_shots * (gap_s + shot_len_s) + 3.0
    t_T = B_TRUE + np.arange(0, dur, 1 / fps)
    starts = [B_TRUE + 2.0 + k * (gap_s + shot_len_s) + float(RNG.uniform(0, 1 / fps))
              for k in range(n_shots)]
    fill = np.zeros_like(t_T)
    det = np.zeros_like(t_T, dtype=bool)
    for s0 in starts:
        m = (t_T >= s0) & (t_T < s0 + shot_len_s)
        fill[m] = np.linspace(5, 98, int(m.sum()))
        det[m] = True
    fill = fill + RNG.normal(0, noise_pp, fill.size) * det
    conf = np.where(det, 1.0, 0.0)
    # student pts such that A*pts + B spans the same epoch window
    pts = (t_T - B_TRUE) / A_TRUE + RNG.normal(0, 0.0005, t_T.size)   # ~0.5ms pipe jitter
    pts = np.sort(pts)
    true_teacher_time = A_TRUE * pts + B_TRUE
    rate = np.zeros_like(pts)
    for s0 in starts:
        m = (true_teacher_time >= s0) & (true_teacher_time < s0 + shot_len_s)
        rate[m] = 1.0
    activity = 3.0 * rate + np.abs(RNG.normal(0.4, 0.3, pts.size))    # motion + scene noise
    return dict(t_T=t_T, fill=fill, det=det, conf=conf, pts=pts, activity=activity)


# --------------------------------------------------------------------------- #
#  primitives
# --------------------------------------------------------------------------- #
def test_theil_sen_robust_to_outliers():
    x = np.arange(1000.0)
    y = 3.5 * x + 7.0
    y[::50] += RNG.normal(0, 400, y[::50].size)      # 2% wild outliers (USB stalls)
    slope, intercept = theil_sen(x, y)
    assert slope == pytest.approx(3.5, abs=0.01)
    assert intercept == pytest.approx(7.0, abs=2.0)


def test_smooth_teacher_timeline_removes_jitter():
    n = np.arange(2000.0)
    truth = 10.0 + n / 60.0
    noisy = truth + RNG.normal(0, 0.002, n.size)     # 2ms USB arrival jitter
    fitted, slope, rms = smooth_teacher_timeline(n, noisy)
    assert slope == pytest.approx(1 / 60.0, rel=1e-4)
    assert float(np.abs(fitted - truth).max()) < 0.001


def test_despike_kills_single_frame_spikes_keeps_ramps():
    f = np.linspace(0, 100, 60)
    f[30] += 25.0                                     # contour-split spike
    out = despike(f)
    assert abs(out[30] - np.linspace(0, 100, 60)[30]) < 3.0
    assert np.allclose(out[:29], f[:29])              # the ramp itself untouched


def test_isotonic_residual_separates_clean_from_corrupt():
    clean = np.linspace(0, 95, 30) + RNG.normal(0, 0.5, 30)
    corrupt = clean.copy()
    corrupt[12:18] -= 30.0                            # mid-rise collapse (double-rise merge)
    assert isotonic_residual(clean) < 1.0
    assert isotonic_residual(corrupt) > 4.0


def test_shot_windows_finds_rises_ignores_blips():
    s = _make_session(n_shots=3)
    w = shot_windows(s["t_T"], s["fill"], s["det"])
    assert len(w) == 3
    blip_fill = np.zeros(600)
    blip_det = np.zeros(600, dtype=bool)
    blip_fill[100:104] = (3, 6, 9, 12)                # 12pp blip: not a shot
    blip_det[100:104] = True
    assert shot_windows(np.arange(600) / 60.0, blip_fill, blip_det) == []


# --------------------------------------------------------------------------- #
#  Stage 2 + 3: the load-bearing recovery guarantees
# --------------------------------------------------------------------------- #
def test_stage2_plus_shot_refine_recovers_time_map():
    """The full pipeline guarantee: Stage 2's global correlation is coarse by construction
    (the objective PLATEAUS -- both signals are near-binary inside a shot), and Stage 3's
    per-shot edge residuals sharpen it. The refined MAPPING must be < 5 ms at both ends
    of the session (where a-error shows; b compensates at the centroid)."""
    s = _make_session(n_shots=10, gap_s=12.0)              # ~125 s session
    t_r, r_r = rise_rate_trace(s["t_T"], s["fill"], s["det"])
    b0 = float(np.median(s["t_T"]) - np.median(s["pts"]))   # coarse QPC-style seed
    a, b, score = fit_time_map(t_r, r_r, s["pts"], s["activity"], b0)
    assert score > 0.5
    windows = shot_windows(s["t_T"], s["fill"], s["det"])
    a, b, deltas = refine_with_shots(a, b, t_r, r_r, s["pts"], s["activity"], windows)
    assert len(deltas) >= 8                                 # residual per usable shot
    for pt in (float(s["pts"][0]), float(np.median(s["pts"])), float(s["pts"][-1])):
        err_ms = abs((a * pt + b) - (A_TRUE * pt + B_TRUE)) * 1000.0
        assert err_ms < 5.0, f"map error {err_ms:.2f} ms at pts={pt:.1f}"


def test_per_shot_residual_recovers_injected_offset():
    s = _make_session()
    t_r, r_r = rise_rate_trace(s["t_T"], s["fill"], s["det"])
    windows = shot_windows(s["t_T"], s["fill"], s["det"])
    inject = 0.008                                     # 8 ms
    d = per_shot_residual(A_TRUE, B_TRUE - inject, t_r, r_r, s["pts"], s["activity"],
                          windows[0])
    assert d is not None
    # single-shot precision floor is ~half a frame of edge phase (+-8 ms worst); the
    # AGGREGATE map (Theil-Sen over all shots) is the real guarantee, tested above
    assert d == pytest.approx(inject, abs=0.006)


def test_transfer_labels_interpolates_and_scales():
    s = _make_session()
    n = s["t_T"].size
    teacher = {"t_s": s["t_T"], "fill": s["fill"], "detected": s["det"], "conf": s["conf"],
               "x": np.full(n, 900.0), "y": np.full(n, 470.0),
               "w": np.full(n, 24.0), "h": np.full(n, 130.0),
               "frame_w": np.full(n, 1920), "frame_h": np.full(n, 1080)}
    student = {"pts_s": s["pts"], "w": np.full(s["pts"].size, 1280),
               "h": np.full(s["pts"].size, 720)}
    labels, report = transfer_labels(A_TRUE, B_TRUE, teacher, student)
    assert report["scale_ok"] and report["sx"] == pytest.approx(2 / 3, abs=1e-3)
    assert report["shots_pass"] == 6
    got = labels["fill"]
    lbl = np.isfinite(got)
    assert int(lbl.sum()) > 100
    # mid-rise label accuracy: reconstruct truth at the mapped times
    true_t = A_TRUE * s["pts"][lbl] + B_TRUE
    truth = np.interp(true_t, s["t_T"][s["det"]], despike(s["fill"])[s["det"]])
    assert float(np.median(np.abs(got[lbl] - truth))) < 1.5     # <= ~1.5pp label noise
    assert labels["x"][lbl][0] == pytest.approx(600.0, abs=1.0)  # 900 * 2/3


def test_transfer_labels_quarantines_bad_scale_and_bad_shots():
    s = _make_session(n_shots=2)
    n = s["t_T"].size
    fill = s["fill"].copy()
    w = shot_windows(s["t_T"], fill, s["det"])
    m = (s["t_T"] >= w[0][0]) & (s["t_T"] <= w[0][1])
    idx = np.flatnonzero(m)[5:11]
    fill[idx] -= 40.0                                  # corrupt shot 0 (non-monotone)
    teacher = {"t_s": s["t_T"], "fill": fill, "detected": s["det"], "conf": s["conf"],
               "x": np.full(n, 900.0), "y": np.full(n, 470.0),
               "w": np.full(n, 24.0), "h": np.full(n, 130.0),
               "frame_w": np.full(n, 1920), "frame_h": np.full(n, 1080)}
    student_ok = {"pts_s": s["pts"], "w": np.full(s["pts"].size, 1280),
                  "h": np.full(s["pts"].size, 720)}
    labels, report = transfer_labels(A_TRUE, B_TRUE, teacher, student_ok)
    verdicts = {q["shot"]: q["pass"] for q in report["shots"]}
    assert verdicts[0] is False and verdicts[1] is True
    assert not np.isfinite(labels["fill"][labels["shot_id"] == 0]).any()
    # non-uniform scale (overscan / wrong output mode) -> ALL shots quarantined
    student_bad = {"pts_s": s["pts"], "w": np.full(s["pts"].size, 1280),
                   "h": np.full(s["pts"].size, 680)}
    _, report_bad = transfer_labels(A_TRUE, B_TRUE, teacher, student_bad)
    assert report_bad["scale_ok"] is False
    assert report_bad["shots_pass"] == 0


# --------------------------------------------------------------------------- #
#  recorder: state machine + archive plumbing (fake taps, no hardware)
# --------------------------------------------------------------------------- #
def test_shot_gate_transitions():
    g = ShotGate(tail_s=0.5)
    assert g.feed(0.00, True, "rising") is None        # 1st rising: not yet
    assert g.feed(0.02, True, "rising") == "start"     # 2nd consecutive: start
    assert g.active
    assert g.feed(0.30, True, "peak") is None          # healthy tracking keeps it open
    assert g.feed(0.50, True, "spent") is None         # spent starts the tail...
    assert g.feed(0.90, True, "spent") is None
    assert g.feed(1.01, True, "spent") == "stop"       # ...tail elapsed
    assert not g.active
    assert g.feed(1.10, True, "rising") is None        # re-trigger needs 2 again
    assert g.feed(1.12, True, "rising") == "start"


def test_shot_gate_lock_loss_also_closes():
    g = ShotGate(tail_s=0.2)
    g.feed(0.0, True, "rising")
    g.feed(0.02, True, "rising")
    assert g.active
    g.feed(0.10, False, "")                            # lock lost -> tail
    assert g.feed(0.35, False, "") == "stop"


class _FakeReader:
    """Scripted reader: (detected, fill, rise_state) per call."""
    def __init__(self, script):
        self.script = list(script)
        self.i = 0

    def read(self, frame, ts=None):
        d, f, rs = self.script[min(self.i, len(self.script) - 1)]
        self.i += 1
        return {"detected": d, "fill": f, "confidence": 1.0 if d else 0.0,
                "bbox": [900, 470, 24, 130] if d else [0, 0, 0, 0], "rise_state": rs}


def _fd(**kw):
    return types.SimpleNamespace(**kw)


def test_recorder_end_to_end_with_fake_taps(tmp_path):
    out = str(tmp_path / "S1")
    script = ([(False, 0.0, "")] * 5
              + [(True, 10.0 + 8 * k, "rising") for k in range(9)]
              + [(True, 85.0, "peak"), (True, 60.0, "spent")]
              + [(False, 0.0, "")] * 60)
    rec = DualCaptureRecorder(out, teacher_src=None, student_src=None,
                              reader=_FakeReader(script), preroll=4, neg_every_s=2.0,
                              meta={"rung": "Balanced"})
    frame = np.zeros((108, 192, 3), np.uint8)
    y = np.zeros((72, 128), np.uint8)
    payload = bytes(128 * 72 * 3 // 2)
    events_t, events_s = [], []
    t0 = 1_700_000_000.0
    for k in range(len(script)):
        ts_ns = int((t0 + k / 60.0) * 1e9)
        events_t.append(rec.teacher_tick(_fd(frame=frame, epoch_ns=ts_ns,
                                             timestamp_ns=ts_ns, frame_number=k)))
        events_s.append(rec.student_tick(_fd(payload=payload, y_plane=y, fmt=0, pts=k * 16667,
                                             epoch_ns=ts_ns, timestamp_ns=ts_ns,
                                             frame_number=k)))
    rec.flush()
    assert "start" in events_t and "stop" in events_t
    n_shot_events = sum(1 for e in events_s if e == "shot")
    assert n_shot_events >= 10
    assert sum(1 for e in events_s if e == "neg") >= 1
    # the INDEX is the source of truth: it also carries the drained pre-roll frames,
    # which are archived without a per-frame tick event
    idx = open(os.path.join(out, "student_index.csv"), encoding="utf-8").read().splitlines()
    assert idx[0].startswith("seq,pts_us")
    shot_rows = [l for l in idx[1:] if ",shot," in l]
    assert len(shot_rows) == n_shot_events + 4          # streamed + drained pre-roll(4)
    assert os.path.getsize(os.path.join(out, "shots", "shot_000.nv12")) \
        == len(shot_rows) * len(payload)
    meta = json.load(open(os.path.join(out, "session_meta.json"), encoding="utf-8"))
    assert meta["shots"] == 1 and meta["rung"] == "Balanced"
    teacher = open(os.path.join(out, "teacher.csv"), encoding="utf-8").read().splitlines()
    assert len(teacher) - 1 == len(script)
    assert os.path.exists(os.path.join(out, "teacher_retro.csv"))


def test_band_activity_zero_on_static_positive_on_motion():
    a = RNG.integers(0, 255, (720, 1280)).astype(np.uint8)
    assert band_activity(a, a.copy(), 1280, 720) == 0.0
    assert band_activity(a, np.roll(a, 4, axis=1), 1280, 720) > 1.0
    assert band_activity(None, a, 1280, 720) == 0.0
