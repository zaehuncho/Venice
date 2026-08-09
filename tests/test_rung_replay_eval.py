#!/usr/bin/env python
"""tests for tools/quality/rung_replay_eval.py -- the M4 rung A/B harness.

Covers:
  * import-safety (module loads via explicit-path import with repo-root sys.path discipline,
    resolves the PRODUCTION simple_meter_reader not the tools/diagnostics shadow, and running
    the import has no encode/report side effects)
  * the pure report-row / delta computation on FABRICATED metric dicts (no frames)
  * the reader-construction contract (adaptive auto-quality vs frozen q=1 row)
  * a skipif smoke run (framedump session + ffmpeg present) over --max-frames 40 --rungs Balanced
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shutil
import sys

import pytest

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
_MODPATH = os.path.join(_REPO, "tools", "quality", "rung_replay_eval.py")
_SESSION = "session_20260704_210801"
_SESSION_DIR = os.path.join(_REPO, "logs", "diagnostics", "framedump", _SESSION)


def _load_module():
    """Load the harness by explicit path (register in sys.modules before exec -- the same
    discipline the harness uses for reencode_ladder)."""
    spec = importlib.util.spec_from_file_location("rung_replay_eval", _MODPATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


rr = _load_module()


# --------------------------------------------------------------------------- #
#  import safety
# --------------------------------------------------------------------------- #
def test_module_loads_and_exposes_api():
    for name in ("build_adaptive_reader", "build_frozen_reader", "compute_variant_metrics",
                 "compute_deltas", "format_rung_block", "encode_decode", "evaluate", "main"):
        assert callable(getattr(rr, name)), f"missing {name}"
    assert rr.RUNGS and "Balanced" in rr.RUNGS
    # RUNGS must be the REAL probe's ladder (proves reencode_ladder loaded by path, not a stub)
    assert rr.RUNGS == rr._RL.RUNGS


def test_repo_root_reader_not_the_diagnostics_shadow():
    # the production reader must win over tools/diagnostics/simple_meter_reader.py
    assert rr.SimpleMeterReader.__module__ == "simple_meter_reader"
    src_dir = os.path.dirname(os.path.abspath(inspect.getfile(rr.SimpleMeterReader)))
    assert src_dir == _REPO, f"shadowed reader loaded from {src_dir!r}"


def test_import_has_no_report_side_effect():
    # importing the module must not encode or write a report; re-loading is idempotent.
    out_dir = os.path.join(_REPO, "logs", "diagnostics", "rung_replay_eval", "__import_probe__")
    _load_module()
    assert not os.path.exists(out_dir)


# --------------------------------------------------------------------------- #
#  pure metric computation on fabricated rows (no frames)
# --------------------------------------------------------------------------- #
def test_compute_variant_metrics_recall_and_phases():
    pristine = [
        {"i": 0, "det": True, "fill": 10.0, "rise": "rising"},
        {"i": 1, "det": True, "fill": 50.0, "rise": "rising"},
        {"i": 2, "det": True, "fill": 90.0, "rise": "peak"},
        {"i": 3, "det": True, "fill": 60.0, "rise": "spent"},
        {"i": 4, "det": False, "fill": 0.0, "rise": ""},
    ]
    rung = [
        {"i": 0, "det": True, "fill": 12.0},   # both, rising, |d|=2
        {"i": 1, "det": False, "fill": 0.0},   # pristine-only, rising
        {"i": 2, "det": True, "fill": 88.0},   # both, peak, |d|=2
        {"i": 3, "det": True, "fill": 61.0},   # both, spent, |d|=1
        {"i": 4, "det": True, "fill": 5.0},    # rung-only
    ]
    m = rr.compute_variant_metrics(pristine, rung)
    assert m["pristine_det"] == 4
    assert (m["both"], m["pristine_only"], m["rung_only"]) == (3, 1, 1)
    assert m["recall_vs_pristine_pct"] == 75.0
    assert m["per_phase_recall_pct"] == {"rising": 50.0, "peak": 100.0, "spent": 100.0}
    assert m["per_phase_counts"]["rising"] == {"pristine": 2, "both": 1}
    fa = m["fill_absdelta"]
    assert fa["n"] == 3
    assert fa["median"] == 2.0
    assert fa["p90"] == 2.0


def test_compute_variant_metrics_empty_joint_detection():
    pristine = [{"i": 0, "det": True, "fill": 10.0, "rise": "rising"}]
    rung = [{"i": 0, "det": False, "fill": 0.0}]
    m = rr.compute_variant_metrics(pristine, rung)
    assert m["both"] == 0 and m["recall_vs_pristine_pct"] == 0.0
    assert m["fill_absdelta"] == {"n": 0}          # reencode_ladder._stats empty shape


def test_compute_deltas_adaptive_minus_frozen():
    adaptive = {"recall_vs_pristine_pct": 80.0,
                "fill_absdelta": {"n": 5, "p90": 4.0},
                "per_phase_recall_pct": {"rising": 70.0, "peak": 90.0, "spent": 60.0}}
    frozen = {"recall_vs_pristine_pct": 75.0,
              "fill_absdelta": {"n": 5, "p90": 5.5},
              "per_phase_recall_pct": {"rising": 66.0, "peak": 90.0, "spent": 55.0}}
    d = rr.compute_deltas(adaptive, frozen)
    assert d["recall_delta_pct"] == 5.0
    assert d["fill_p90_delta"] == -1.5
    assert d["per_phase_recall_delta_pct"] == {"rising": 4.0, "peak": 0.0, "spent": 5.0}


def test_compute_deltas_missing_p90_is_none():
    adaptive = {"recall_vs_pristine_pct": 0.0, "fill_absdelta": {"n": 0},
                "per_phase_recall_pct": {}}
    frozen = {"recall_vs_pristine_pct": 0.0, "fill_absdelta": {"n": 0},
              "per_phase_recall_pct": {}}
    d = rr.compute_deltas(adaptive, frozen)
    assert d["fill_p90_delta"] is None
    assert d["recall_delta_pct"] == 0.0
    assert d["per_phase_recall_delta_pct"] == {}


def test_format_rung_block_is_pure_text_with_delta_column():
    adaptive = {"frames": 40, "pristine_det": 30, "both": 24, "pristine_only": 6, "rung_only": 1,
                "recall_vs_pristine_pct": 80.0, "fill_absdelta": {"n": 24, "p90": 4.0},
                "per_phase_recall_pct": {"rising": 70.0, "peak": 90.0, "spent": 60.0},
                "per_phase_counts": {"rising": {"pristine": 12, "both": 8},
                                     "peak": {"pristine": 10, "both": 9},
                                     "spent": {"pristine": 8, "both": 7}}}
    frozen = {"frames": 40, "pristine_det": 30, "both": 22, "pristine_only": 8, "rung_only": 2,
              "recall_vs_pristine_pct": 73.3, "fill_absdelta": {"n": 22, "p90": 5.5},
              "per_phase_recall_pct": {"rising": 66.0, "peak": 90.0, "spent": 55.0},
              "per_phase_counts": {"rising": {"pristine": 12, "both": 7},
                                   "peak": {"pristine": 10, "both": 9},
                                   "spent": {"pristine": 8, "both": 6}}}
    d = rr.compute_deltas(adaptive, frozen)
    lines = rr.format_rung_block("Balanced", adaptive, frozen, d, 0.742, 0.611)
    assert isinstance(lines, list) and all(isinstance(x, str) for x in lines)
    blob = "\n".join(lines)
    assert "Balanced" in blob
    assert "d(adp-frz)" in blob
    assert "+6.7" in blob        # recall delta 80.0 - 73.3
    assert "-1.50" in blob       # fill p90 delta 4.0 - 5.5
    assert "0.742" in blob and "0.611" in blob


def test_format_rung_block_tolerates_missing_p90():
    empty = {"frames": 0, "pristine_det": 0, "both": 0, "pristine_only": 0, "rung_only": 0,
             "recall_vs_pristine_pct": 0.0, "fill_absdelta": {"n": 0},
             "per_phase_recall_pct": {}, "per_phase_counts": {}}
    d = rr.compute_deltas(empty, empty)
    lines = rr.format_rung_block("UltraLow", empty, empty, d, None, 0.3)
    blob = "\n".join(lines)
    assert "UltraLow" in blob and "n/a" in blob


# --------------------------------------------------------------------------- #
#  reader-construction contract
# --------------------------------------------------------------------------- #
def test_adaptive_reader_is_auto_quality():
    a = rr.build_adaptive_reader()
    assert a._luma_gate_auto is True          # quality-managed luma gate
    assert a._params_fixed is False           # quality adaptation live
    assert isinstance(a, rr.CompressedMeterReader)


def test_frozen_reader_is_pinned_q1_row():
    f = rr.build_frozen_reader()
    assert f.NCC_LOCK == 0.60                  # shipped capture-card constant, unadapted
    assert f._luma_gate_auto is False          # explicit luma_tracking -> sticks
    assert f._luma_tracking is True            # luma branches available (frozen thresholds)
    assert f._params_fixed is True             # ReaderParams() disables quality adaptation


# --------------------------------------------------------------------------- #
#  smoke run (needs the framedump session + ffmpeg)
# --------------------------------------------------------------------------- #
def _frames_present():
    if not os.path.isdir(_SESSION_DIR):
        return False
    try:
        return any(n.endswith("_raw.png") for n in os.listdir(_SESSION_DIR))
    except OSError:
        return False


@pytest.mark.skipif(shutil.which("ffmpeg") is None or not _frames_present(),
                    reason="framedump session or ffmpeg missing")
def test_smoke_report_files_exist_and_parse():
    rc = rr.main(["--session", _SESSION, "--rungs", "Balanced", "--max-frames", "40"])
    assert rc == 0
    out_dir = os.path.join(_REPO, "logs", "diagnostics", "rung_replay_eval", _SESSION)
    txt = os.path.join(out_dir, "report.txt")
    js = os.path.join(out_dir, "report.json")
    assert os.path.isfile(txt), "report.txt missing"
    assert os.path.isfile(js), "report.json missing"
    with open(js, encoding="utf-8") as fh:
        obj = json.load(fh)
    assert obj["session"] == _SESSION
    assert obj["rungs"] and obj["rungs"][0]["rung"] == "Balanced"
    block = obj["rungs"][0]
    for key in ("adaptive", "frozen", "deltas", "q_session_observed_median",
                "q_session_expected_prior"):
        assert key in block, f"missing {key}"
    assert "recall_delta_pct" in block["deltas"]
    with open(txt, encoding="utf-8") as fh:
        assert "d(adp-frz)" in fh.read()
