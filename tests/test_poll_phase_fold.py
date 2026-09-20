from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "timing"))

import poll_phase_fold as fold  # noqa: E402


def _circular_error(left: float, right: float, period: float) -> float:
    return abs((left - right + period / 2.0) % period - period / 2.0)


def test_synthetic_sawtooth_recovers_cliff_and_is_significant():
    rng = np.random.default_rng(4080)
    period = fold.DEFAULT_PERIOD_MS
    cliff = 6.25
    height = 3.2
    phases = rng.uniform(0.0, period, 320)
    residuals = height * (((phases - cliff) % period) / period - 0.5)
    residuals += rng.normal(0.0, 0.22, phases.size)
    strata = np.where(np.arange(phases.size) % 2, "Standstill", "Left Fade")

    result = fold.fit_models(
        phases, residuals, strata, period_ms=period,
        permutations=4000, seed=4080, cliff_step_ms=0.05)

    assert result.n == 320
    assert result.saw_height_pp == pytest.approx(height, abs=0.18)
    assert _circular_error(result.saw_cliff_ms, cliff, period) < 0.15
    assert result.saw_r2 > 0.90
    assert result.saw_p < 0.01
    assert "poll grid: CONFIRMED" in fold.verdict_line(result, [result], period)


def test_null_dataset_reports_not_detected():
    period = fold.DEFAULT_PERIOD_MS
    phases = np.linspace(0.0, period, 64, endpoint=False)
    residuals = np.zeros_like(phases)
    result = fold.fit_models(
        phases, residuals, ["Standstill"] * len(phases),
        permutations=128, seed=4080)

    assert result.saw_height_pp == 0.0
    assert result.saw_r2 == 0.0
    assert result.saw_p == 1.0
    assert "poll grid: NOT DETECTED" in fold.verdict_line(result, [result], period)


def test_log_parser_joins_fire_stamp_raw_peak_and_epoch(tmp_path: Path):
    log = tmp_path / "orion_native.log"
    log.write_text(
        "\n".join([
            "2026-09-03T04:20:30.000Z  Physical shot epoch: epoch=17 intent=square_edge route=RawInput",
            "2026-09-03T04:20:30.800Z  Release submit: seq=4 ok=1 backend=PIPE fire_epoch_ms=1788409230800.375",
            "2026-09-03T04:20:30.801Z  Release delivery identity: physical_epoch=17 release_seq=4 delivery_stage=local_udp_accepted",
            "2026-09-03T04:20:32.000Z  Release landing: seq=4 graded=1 peak_fill=94.0 raw_peak_fill=95.25 shot=Right Fade",
            "2026-09-03T04:20:33.000Z  Release submit: seq=5 ok=1 backend=PIPE",
            "2026-09-03T04:20:34.000Z  Release landing: seq=5 graded=1 raw_peak_fill=96.0 shot=Standstill",
        ]) + "\n",
        encoding="utf-8",
    )

    releases, stats = fold.parse_releases([log])
    assert stats.successful_submits == 2
    assert stats.stamped_submits == 1
    assert stats.graded_landings == 2
    assert stats.joined == 1
    assert len(releases) == 1
    release = releases[0]
    assert release.seq == 4 and release.physical_epoch == 17
    assert release.raw_peak_fill == pytest.approx(95.25)
    assert release.shot == "Right Fade"


def test_fold_uses_preceding_frames_and_session_type_median(tmp_path: Path):
    releases = [
        fold.Release(1, 1_018.0, 1_018.0, 1_900.0, 95.0, "Standstill", 1, 1),
        fold.Release(2, 1_032.0, 1_032.0, 1_920.0, 97.0, "Standstill", 2, 1),
    ]
    console = fold.GridSeries(
        "session_a",
        tmp_path / "avclock.csv",
        np.asarray([1_000.0, 1_016.667, 1_033.334]),
        np.asarray([40, 41, 42]),
    )
    capture = fold.GridSeries(
        "capture_a",
        tmp_path / "detframes.csv",
        np.asarray([1_005.0, 1_021.667]),
    )

    records, missing = fold.fold_releases(
        releases, [console], [capture], fold.DEFAULT_PERIOD_MS, 100.0)

    assert missing == 0 and len(records) == 2
    assert records[0].console_frame_index == 41
    assert records[0].console_phase_ms == pytest.approx(1.333, abs=1e-9)
    assert records[1].console_phase_ms == pytest.approx(15.333, abs=1e-9)
    assert records[0].capture_phase_ms == pytest.approx(13.0, abs=1e-9)
    assert [record.residual_pp for record in records] == [-1.0, 1.0]


def test_avclock_alignment_uses_sync_pair_and_reports_clean_period(tmp_path: Path):
    path = tmp_path / "avclock_test.csv"
    period_us = 16_667
    rows = ["frame_index,arrival_qpc_us,wall_ms,sync_qpc_us,sync_wall_ms"]
    for index in range(700):
        qpc = 10_000_000 + index * period_us
        wall = 1_788_409_230_000.0 + (qpc - 10_000_000) / 1000.0
        rows.append(f"{index},{qpc},{wall + 50.0:.3f},10000000,1788409230000.000")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    grid = fold.load_avclock(path)
    assert grid.wall_ms[1] - grid.wall_ms[0] == pytest.approx(16.667, abs=1e-3)
    count, period, rmad = fold.avclock_quality(grid)
    assert 598 <= count <= 601
    assert period == pytest.approx(16.667, abs=1e-6)
    assert math.isfinite(rmad) and rmad < 1e-3


def test_avclock_quality_uses_mid_session_window(tmp_path: Path):
    frames = np.arange(1_800, dtype=np.int64)
    intervals = np.full(frames.size - 1, 33.366)
    intervals[:100:2] = 5.0
    intervals[1:100:2] = 61.732
    times = 1_788_409_230_000.0 + np.concatenate(([0.0], np.cumsum(intervals)))
    grid = fold.GridSeries("session", tmp_path / "avclock.csv", times, frames)

    count, period, rmad = fold.avclock_quality(grid)

    assert 299 <= count <= 301
    assert period == pytest.approx(33.366, abs=1e-6)
    assert rmad < 1e-3


def test_cli_requires_at_least_four_thousand_permutations():
    parser = fold.build_parser()
    assert parser.parse_args([]).permutations == 4000
    with pytest.raises(SystemExit):
        fold.main(["--permutations", "3999"])
