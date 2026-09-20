"""Corpus-selection tests for the production registration-model builder."""

import os
from datetime import datetime, timezone

import pytest

from tools.timing import build_tip_registration as builder


def _set_mtime(path, value):
    timestamp = value.replace(tzinfo=timezone.utc).timestamp()
    os.utime(path, (timestamp, timestamp))


def test_parse_since_normalizes_offsets_to_utc():
    parsed = builder.parse_since("2026-09-01T01:30:00-05:00")
    assert parsed == datetime(2026, 9, 1, 6, 30, tzinfo=timezone.utc)


def test_parse_since_rejects_non_iso_value():
    with pytest.raises(builder.argparse.ArgumentTypeError, match="invalid --since"):
        builder.parse_since("last Tuesday")


def test_repeatable_globs_are_deduplicated_and_filtered_by_capture_date(tmp_path):
    old = tmp_path / "detframes_20260831_235959.csv"
    dated = tmp_path / "detframes_20260901_000001.csv"
    live = tmp_path / "detframes.csv"
    for path in (old, dated, live):
        path.write_text("t_ms,fill_pct,detected\n", encoding="utf-8")
    _set_mtime(old, datetime(2026, 9, 3))
    _set_mtime(dated, datetime(2026, 8, 1))
    _set_mtime(live, datetime(2026, 9, 2))

    paths = builder.resolve_csv_paths(
        [str(tmp_path / "detframes*.csv"), str(dated)],
        builder.parse_since("2026-09-01"),
    )

    assert paths == [live.resolve(), dated.resolve()]


def test_training_manifest_binds_source_hash(tmp_path):
    source = tmp_path / "detframes_20260902_225033.csv"
    source.write_bytes(b"t_ms,fill_pct,detected\n1,25,1\n")

    manifest = builder.build_training_manifest(
        [source], [str(source)], builder.parse_since("2026-09-01")
    )

    assert manifest["since_utc"] == "2026-09-01T00:00:00Z"
    assert manifest["sources"] == [{
        "path": str(source),
        "size_bytes": source.stat().st_size,
        "sha256": "E50CB00662F0B527EE5341B524743A70D0AF1243E5AC960F6DE26B4567ECA1F2",
    }]
