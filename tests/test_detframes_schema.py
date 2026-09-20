"""detframes.csv schema guard.

The Round 33 live batch's detection diagnosis rides on detframes.csv. Two
invariants protect it:

  1. The first 13 columns are the original Round 32 set, byte-identical —
     existing readers (analyze_release_timing.py, release_action_timeline.py,
     analyze_fed_rate.py) are csv.DictReader-based so APPENDED columns are safe,
     but renaming/reordering the legacy names would silently break them.
  2. The row format string produces exactly one value per header column, so a
     drifting edit can't shear the CSV (a short row makes DictReader fill None
     and every downstream float() crash).
"""
import csv
import io
import re

from remote_play_orchestrator import _DETCSV_HEADER, _DETCSV_ROW_FMT

_LEGACY_PREFIX = (
    "t_ms,detected,fill_pct,confidence,x,y,w,h,rejection_reason,"
    "green_center_pct,green_confidence,frame_w,frame_h"
)

_SPEC_RE = re.compile(r"%[-+ #0]*(?:\d+)?(?:\.\d+)?[dfs]")


def _sample_values():
    """One plausible, comma-free value per conversion spec, in order."""
    vals = []
    for spec in _SPEC_RE.findall(_DETCSV_ROW_FMT):
        if spec.endswith("d"):
            vals.append(7)
        elif spec.endswith("f"):
            vals.append(3.5)
        else:
            vals.append("tok")
    return tuple(vals)


def test_legacy_columns_unchanged():
    assert _DETCSV_HEADER.startswith(_LEGACY_PREFIX), (
        "the first 13 detframes.csv columns are load-bearing for existing "
        "diagnostic tools — append new columns, never rename/reorder these"
    )


def test_row_fmt_matches_header_width():
    header_cols = _DETCSV_HEADER.split(",")
    specs = _SPEC_RE.findall(_DETCSV_ROW_FMT)
    assert len(specs) == len(header_cols), (
        f"row format has {len(specs)} conversions but header has "
        f"{len(header_cols)} columns"
    )


def test_rendered_row_parses_under_header():
    rendered = _DETCSV_ROW_FMT % _sample_values()
    rows = list(csv.DictReader(io.StringIO(_DETCSV_HEADER + "\n" + rendered)))
    assert len(rows) == 1
    row = rows[0]
    assert None not in row.values(), "row is narrower than the header"
    assert None not in row, "row is wider than the header"
    # Spot-check a legacy column and an appended one resolve by name.
    assert row["t_ms"] is not None
    assert row["stab_event"] is not None
    assert row["dup_pct"] is not None
    # Frozen-meter latency-oracle columns (appended after frame_no) resolve by name.
    assert row["pts"] is not None
    assert row["rel_seq"] is not None
    assert row["mtr_phase"] is not None


def test_latency_oracle_columns_present():
    cols = _DETCSV_HEADER.split(",")
    for name in ("pts", "rel_seq", "mtr_phase"):
        assert name in cols, f"{name} column missing from detframes schema"
    # they must be APPENDED (after the original frame_no), never inserted mid-schema.
    assert cols.index("pts") > cols.index("frame_no")


def test_quality_diag_columns_present():
    """Compressed-stream quality/staleness diagnostics (the offline tuning record for the
    quality-adaptive reader). Appended after mtr_phase; producers land incrementally so
    offline tooling written against this schema must not break as they do."""
    cols = _DETCSV_HEADER.split(",")
    for name in ("q_frame", "q_session", "edge_curv", "sig_width", "ncc_margin",
                 "roi_sad", "stale", "valid", "R_used", "is_iframe"):
        assert name in cols, f"{name} column missing from detframes schema"
    assert cols.index("q_frame") > cols.index("mtr_phase")


def test_fill_ruler_provenance_columns_present_and_appended():
    """A phase crossing is valid only within one estimator/source generation.

    Keep those identities in the diagnostic record so a live batch can verify the
    same contract the native engine enforced instead of inferring it from fill.
    """
    cols = _DETCSV_HEADER.split(",")
    names = (
        "coarse_fill_pct",
        "fill_estimator_mode",
        "fill_estimator_generation",
        "frame_integrity_generation",
    )
    for name in names:
        assert name in cols, f"{name} column missing from detframes schema"
    assert cols.index("coarse_fill_pct") > cols.index("det_h")
