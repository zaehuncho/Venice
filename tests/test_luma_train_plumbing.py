#!/usr/bin/env python3
"""Pure-plumbing tests for the LUMA meter-locator retrain + per-phase eval.

These test ONLY the import-safe module-level helpers (arg->kwarg composition, dataset-dir
validation, phase bucketing math). They must NOT require ultralytics/YOLO — both tools import
YOLO lazily inside main()/train paths, so importing the modules here is safe.

Run (RTK rewrites bare `pytest`, so drive pytest.main from a heredoc):
    python - <<'EOF'
    import pytest, sys
    sys.exit(pytest.main(["-q", "tests/test_luma_train_plumbing.py"]))
    EOF
"""
import importlib.util
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relpath))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


train = _load("train_meter_detector", os.path.join("tools", "training", "train_meter_detector.py"))
evalm = _load("eval_meter_locator", os.path.join("tools", "diagnostics", "eval_meter_locator.py"))


# --------------------------------------------------------------------------- #
#  training_aug_kwargs
# --------------------------------------------------------------------------- #
# The exact geometry-aug set the trainer has always passed to m.train(). If the default path
# ever changes, this literal must change with it — that is the point of asserting it.
DEFAULT_AUG = {
    "degrees": 0, "shear": 0, "perspective": 0,
    "mosaic": 0.5, "scale": 0.5, "translate": 0.2,
}


def test_aug_default_matches_in_file_values():
    assert train.training_aug_kwargs(False) == DEFAULT_AUG


def test_aug_luma_zeroes_hue_sat_keeps_rest():
    luma = train.training_aug_kwargs(True)
    base = train.training_aug_kwargs(False)
    # hue + saturation jitter zeroed for replicated-gray input; brightness jitter kept.
    assert luma["hsv_h"] == 0.0
    assert luma["hsv_s"] == 0.0
    assert luma["hsv_v"] == 0.4
    # every geometry kwarg is byte-for-byte the default set.
    for k, v in base.items():
        assert luma[k] == v
    # luma adds exactly the three hsv keys and nothing else.
    assert set(luma) == set(base) | {"hsv_h", "hsv_s", "hsv_v"}


def test_aug_default_has_no_hsv_keys():
    # the no-flag path must not start setting hsv (would change historical training behaviour).
    d = train.training_aug_kwargs(False)
    assert "hsv_h" not in d and "hsv_s" not in d and "hsv_v" not in d


# --------------------------------------------------------------------------- #
#  extra_dataset_error (the fail-fast validation)
# --------------------------------------------------------------------------- #
def test_extra_dataset_error_lists_missing(tmp_path):
    err = train.extra_dataset_error(["rung18/yolo", "rung23/yolo"], str(tmp_path))
    assert err is not None
    assert "ERROR" in err
    assert "rung18/yolo" in err and "rung23/yolo" in err
    assert err.count("missing:") == 2


def test_extra_dataset_error_none_when_present(tmp_path):
    (tmp_path / "rung18").mkdir()
    assert train.extra_dataset_error(["rung18"], str(tmp_path)) is None


def test_extra_dataset_error_none_when_empty(tmp_path):
    assert train.extra_dataset_error(None, str(tmp_path)) is None
    assert train.extra_dataset_error([], str(tmp_path)) is None


def test_extra_dataset_error_partial_missing(tmp_path):
    (tmp_path / "present").mkdir()
    err = train.extra_dataset_error(["present", "gone"], str(tmp_path))
    assert err is not None
    assert "gone" in err and "present" not in err   # only the missing one is listed


# --------------------------------------------------------------------------- #
#  phase bucketing
# --------------------------------------------------------------------------- #
def _synthetic_shot():
    """One shot: fill ramps 0->98 over 500 ms, then drops 98->0 over the next 500 ms.
    1 ms frame spacing. Returns (pts_s, fill, shot_id)."""
    t = np.round(np.arange(0.0, 1.0001, 0.001), 6)
    up = t <= 0.5
    fill = np.where(up, 196.0 * t, 98.0 * (1.0 - 2.0 * (t - 0.5)))
    fill = np.clip(fill, 0.0, 98.0)
    shot_id = np.zeros(t.size, dtype=np.int64)
    return t, fill, shot_id


def test_phase_of_scalar_precedence():
    span = (0.0, 98.0, 0.5)   # t_start, max_fill, t_max
    assert evalm.phase_of(0.05, 10.0, span) == "rising-early"   # first 120 ms
    assert evalm.phase_of(0.11, 22.0, span) == "rising-early"
    assert evalm.phase_of(0.20, 39.0, span) == "rising-late"    # past 120 ms, fill < 85
    assert evalm.phase_of(0.45, 88.0, span) == "peak"           # fill >= 85, before max
    assert evalm.phase_of(0.50, 98.0, span) == "peak"           # the max frame itself
    assert evalm.phase_of(0.80, 39.0, span) == "spent"          # after max, big drop
    # precedence lock: after max with fill still >= 85 but dropped > 8 pp -> spent, not peak.
    assert evalm.phase_of(0.56, 88.0, span) == "spent"
    # just after max, fill still within 8 pp of peak -> still peak (not yet spent).
    assert evalm.phase_of(0.505, 96.0, span) == "peak"


def test_bucket_phases_over_synthetic_shot():
    t, fill, shot_id = _synthetic_shot()
    phases = evalm.bucket_phases(t, fill, shot_id)

    # first 120 ms -> rising-early (regardless of fill).
    assert np.all(phases[t < 0.12] == "rising-early")
    # a clearly rising-late frame.
    assert phases[np.argmin(np.abs(t - 0.20))] == "rising-late"
    # ascending fill >= 85 window (before the max) -> peak.
    asc_peak = (t >= 0.44) & (t <= 0.50)
    assert np.all(phases[asc_peak] == "peak")
    # well after the drop -> spent.
    assert np.all(phases[t >= 0.60] == "spent")
    # every labeled frame got a phase.
    assert np.all(phases != "")
    assert set(np.unique(phases)) <= set(evalm.PHASES)


def test_bucket_phases_ignores_unlabeled_frames():
    t, fill, shot_id = _synthetic_shot()
    # append inter-shot frames: shot_id -1, fill NaN (as the aligner writes them).
    t2 = np.concatenate([t, [1.1, 1.2]])
    fill2 = np.concatenate([fill, [np.nan, np.nan]])
    sid2 = np.concatenate([shot_id, [-1, -1]])
    phases = evalm.bucket_phases(t2, fill2, sid2)
    assert phases[-1] == "" and phases[-2] == ""       # unlabeled -> empty
    assert np.all(phases[:t.size] != "")               # labeled ones still bucketed


def test_compute_shot_spans_picks_max():
    t, fill, shot_id = _synthetic_shot()
    spans = evalm.compute_shot_spans(t, fill, shot_id)
    assert set(spans) == {0}
    t_start, max_fill, t_max = spans[0]
    assert t_start == 0.0
    assert abs(max_fill - 98.0) < 1e-6
    assert abs(t_max - 0.5) < 1e-3


# --------------------------------------------------------------------------- #
#  report format
# --------------------------------------------------------------------------- #
def test_format_meta_report_prefixes_rung_and_sums_overall():
    stats = {"rising-early": (5, 5), "rising-late": (8, 10), "peak": (9, 10), "spent": (2, 5)}
    lines = evalm.format_meta_report("rung18", stats, conf=0.35, imgsz=960)
    body = [ln for ln in lines if ln.startswith("rung18")]
    assert len(body) == len(evalm.PHASES) + 1          # one line per phase + OVERALL
    overall = [ln for ln in body if "OVERALL" in ln][0]
    assert "24/30" in overall                          # 5+8+9+2 hits / 5+10+10+5 total
    assert "80.0%" in overall


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main(["-q", os.path.abspath(__file__)]))
