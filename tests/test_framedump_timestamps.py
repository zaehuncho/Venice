"""The async framedump must never substitute PNG-writer time for frame time."""

import csv

import remote_play_orchestrator as rpo


def test_framedump_index_records_producer_and_writer_clocks(tmp_path, monkeypatch):
    orch = rpo.RemotePlayOrchestrator.__new__(rpo.RemotePlayOrchestrator)
    orch._framedump_dir = str(tmp_path)
    orch._framedump_index_failed = False
    orch._framedump_index_fh = None
    monkeypatch.setattr(rpo.time, "time", lambda: 2_000.25)
    info = {
        "t": 50.0,
        "wall": 1_000.125,
        "det": True,
        "fill": 42.0,
        "conf": 0.9,
        "gc": 95.0,
        "bbox": (1, 2, 3, 4),
        "rej": "",
    }

    orch._framedump_write_index(7, info)
    orch._framedump_index_fh.close()

    with open(tmp_path / "frames.csv", newline="", encoding="utf-8") as fh:
        [row] = list(csv.DictReader(fh))
    assert float(row["t_wall"]) == 1_000.125
    assert float(row["write_wall"]) == 2_000.25
    assert int(row["idx"]) == 7
