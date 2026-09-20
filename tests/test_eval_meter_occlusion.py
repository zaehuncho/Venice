"""Regression tests for the deterministic synthetic-occlusion evaluator."""

from __future__ import annotations

import csv
from pathlib import Path

import cv2
import numpy as np
import pytest

from tools.training.eval_meter_occlusion import (
    DEFAULT_PLACEMENTS,
    Sample,
    apply_occlusion,
    evaluate,
    iou_xywh,
    load_verified_samples,
    nearby_ring_median,
    occluder_rect,
    recall_gate_failures,
)


def test_occlusion_is_deterministic_bounded_and_non_mutating():
    frame = np.full((90, 120, 3), (17, 31, 47), dtype=np.uint8)
    box = (40, 20, 20, 50)
    frame[20:70, 40:60] = (220, 210, 200)
    original = frame.copy()

    first, first_rect, first_ratio, first_fill = apply_occlusion(
        frame, box, 20, "center_patch"
    )
    second, second_rect, second_ratio, second_fill = apply_occlusion(
        frame, box, 20, "center_patch"
    )

    assert np.array_equal(frame, original)
    assert np.array_equal(first, second)
    assert first_rect == second_rect
    assert first_ratio == second_ratio
    assert np.array_equal(first_fill, second_fill)
    assert tuple(int(value) for value in first_fill) == (17, 31, 47)
    ox, oy, ow, oh = first_rect
    x, y, w, h = box
    assert x <= ox < ox + ow <= x + w
    assert y <= oy < oy + oh <= y + h
    assert abs(first_ratio - 0.20) <= 1.0 / (w * h) * max(w, h)
    outside = np.ones(frame.shape[:2], dtype=bool)
    outside[oy : oy + oh, ox : ox + ow] = False
    assert np.array_equal(first[outside], original[outside])


@pytest.mark.parametrize("level", [10, 20, 30])
@pytest.mark.parametrize("placement", DEFAULT_PLACEMENTS)
def test_all_occluder_shapes_stay_inside_gt(level, placement):
    box = (7, 11, 27, 121)
    x, y, w, h = box
    ox, oy, ow, oh = occluder_rect(box, level, placement)

    assert x <= ox < ox + ow <= x + w
    assert y <= oy < oy + oh <= y + h
    realised = ow * oh / float(w * h)
    # At narrow meter widths, one-pixel strip quantisation is the worst case.
    assert abs(realised - level / 100.0) <= max(1.0 / w, 1.0 / h) + 1e-12


def test_nearby_ring_excludes_meter_pixels():
    frame = np.full((40, 50, 3), 23, dtype=np.uint8)
    frame[10:30, 20:30] = 251

    median = nearby_ring_median(frame, (20, 10, 10, 20), margin=4)

    assert median.tolist() == [23, 23, 23]


def _write_fixture_hardset(tmp_path: Path, *, mismatch: bool = False) -> Path:
    root = tmp_path / "hardset"
    (root / "images" / "heldout").mkdir(parents=True)
    (root / "labels" / "heldout").mkdir(parents=True)
    frame = np.zeros((100, 200, 3), dtype=np.uint8)
    image = "s_fixture_00001.png"
    assert cv2.imwrite(str(root / "images" / "heldout" / image), frame)
    # Manifest box (40, 20, 20, 50) -> cx=.25 cy=.45 w=.10 h=.50.
    cx = 0.30 if mismatch else 0.25
    (root / "labels" / "heldout" / "s_fixture_00001.txt").write_text(
        f"0 {cx:.6f} 0.450000 0.100000 0.500000\n", encoding="utf-8"
    )
    fields = [
        "split",
        "image",
        "session",
        "idx",
        "source",
        "x",
        "y",
        "w",
        "h",
        "fill_est",
    ]
    with (root / "manifest.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerow(
            {
                "split": "heldout",
                "image": image,
                "session": "s_fixture",
                "idx": 1,
                "source": "propagated",
                "x": 40,
                "y": 20,
                "w": 20,
                "h": 50,
                "fill_est": 7.5,
            }
        )
    return root


def test_manifest_rows_are_cross_checked_against_labels(tmp_path):
    root = _write_fixture_hardset(tmp_path)

    samples = load_verified_samples(root)

    assert len(samples) == 1
    assert samples[0].box == (40, 20, 20, 50)
    assert samples[0].fill_est == 7.5


def test_label_mismatch_is_rejected(tmp_path):
    root = _write_fixture_hardset(tmp_path, mismatch=True)

    with pytest.raises(ValueError, match="label/manifest mismatch"):
        load_verified_samples(root)


class _FixedLocator:
    def __init__(self, detected_box):
        self.detected_box = detected_box

    def detect_box(self, _frame):
        if self.detected_box is None:
            return None
        return (*self.detected_box, 0.91)


def test_evaluate_runs_clean_once_and_each_requested_placement(tmp_path):
    image = tmp_path / "frame.png"
    frame = np.full((80, 100, 3), 25, dtype=np.uint8)
    frame[10:60, 30:50] = 220
    assert cv2.imwrite(str(image), frame)
    sample = Sample(
        image=image.name,
        image_path=image,
        label_path=tmp_path / "unused.txt",
        session="s",
        idx=1,
        source="propagated",
        box=(30, 10, 20, 50),
        fill_est=5.0,
    )

    records, summary = evaluate(
        _FixedLocator((30, 10, 20, 50)),
        [sample],
        levels=(0, 10, 30),
        placements=("top_strip", "right_strip", "center_patch"),
        warmup=0,
        progress_every=0,
    )

    assert len(records) == 7  # one clean + three placements at each nonzero level
    assert [row["placement"] for row in records].count("clean") == 1
    assert all(row["hit"] for row in records)
    assert summary["by_level"]["0"]["count"] == 1
    assert summary["by_level"]["10"]["count"] == 3
    assert summary["by_level"]["30"]["recall"] == 1.0


def test_iou_threshold_distinguishes_shifted_detection(tmp_path):
    image = tmp_path / "frame.png"
    assert cv2.imwrite(str(image), np.zeros((80, 100, 3), dtype=np.uint8))
    sample = Sample(
        image=image.name,
        image_path=image,
        label_path=tmp_path / "unused.txt",
        session="s",
        idx=1,
        source="propagated",
        box=(30, 10, 20, 50),
        fill_est=5.0,
    )
    # A 15-pixel horizontal shift leaves 5/35 ~= .143 IoU.
    records, _ = evaluate(
        _FixedLocator((45, 10, 20, 50)),
        [sample],
        levels=(0,),
        warmup=0,
        progress_every=0,
        iou_threshold=0.30,
    )

    assert iou_xywh((30, 10, 20, 50), (45, 10, 20, 50)) == pytest.approx(1 / 7)
    assert records[0]["hit"] is False


def test_recall_gate_checks_each_placement_not_only_aggregate():
    summary = {
        "by_level_and_placement": {
            "0": {"clean": {"count": 10, "recall": 1.0}},
            "10": {
                "left_strip": {"count": 10, "recall": 1.0},
                "right_strip": {"count": 10, "recall": 0.8},
            },
        }
    }

    assert recall_gate_failures(summary, 0.9) == [
        "level=10% placement=right_strip recall=0.800000 < 0.900000"
    ]
