#!/usr/bin/env python3
"""Deterministic synthetic-occlusion evaluation for the runtime meter locator.

The evaluator uses only positive rows whose ``split`` is ``heldout`` in the
low-fill hard-set manifest.  Every manifest box is cross-checked against its
YOLO label before inference.  For each box it creates bounded occlusions at
0/10/20/30 percent using horizontal strips, vertical strips, and a centred
patch.  Occluder pixels are the per-channel median of a ring immediately around
the ground-truth box, approximating nearby court/HUD background without copying
another meter feature into the box.

Determinism contract:

* manifest rows have a stable sort order;
* occluder geometry and pixels contain no randomness;
* each model/frame/level/placement combination is evaluated exactly once; and
* CSV/JSON rows and keys have stable ordering.

Wall-clock inference latency is intentionally measured and therefore naturally
varies between runs.  This tool trusts the manifest's split assignment; it does
not audit or claim that the selected model was trained independently of those
images.  Reports call the set "manifest-declared heldout" for that reason.

Example::

    python tools/training/eval_meter_occlusion.py \
      --model runs/detect/logs/diagnostics/meter_train/\
meter2k27_n4_lowfill/weights/best.onnx \
      --output-dir .codex_artifacts/timing_occlusion_fix_20260904/occlusion_n4

An optional ``--min-recall`` gate applies to every non-empty level/placement
cell.  Gate failure exits 3; invalid inputs or an unloadable model exit 2.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

import cv2
import numpy as np


REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

DEFAULT_HARDSET = (
    REPO
    / "runs"
    / "detect"
    / "logs"
    / "diagnostics"
    / "meter_train"
    / "lowfill_hardset"
)
DEFAULT_MODEL = (
    REPO
    / "runs"
    / "detect"
    / "logs"
    / "diagnostics"
    / "meter_train"
    / "meter2k27_n4_lowfill"
    / "weights"
    / "best.onnx"
)

DEFAULT_LEVELS = (0, 10, 20, 30)
DEFAULT_PLACEMENTS = (
    "top_strip",
    "middle_strip",
    "bottom_strip",
    "left_strip",
    "right_strip",
    "center_patch",
)
_HORIZONTAL = {"top_strip", "middle_strip", "bottom_strip"}
_VERTICAL = {"left_strip", "right_strip"}
_ALL_PLACEMENTS = set(DEFAULT_PLACEMENTS)
_REQUIRED_MANIFEST_COLUMNS = {
    "split",
    "image",
    "session",
    "idx",
    "source",
    "x",
    "y",
    "w",
    "h",
}
_POSITIVE_SOURCES = {"propagated", "detected_lowfill"}


@dataclass(frozen=True)
class Sample:
    """One manifest-declared heldout positive with a verified label box."""

    image: str
    image_path: Path
    label_path: Path
    session: str
    idx: int
    source: str
    box: tuple[int, int, int, int]
    fill_est: Optional[float]


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_float(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not math.isfinite(number):
        raise ValueError(f"non-finite {field}: {value!r}")
    return number


def _positive_int(value: Any, field: str) -> int:
    number = _finite_float(value, field)
    rounded = int(round(number))
    if abs(number - rounded) > 1e-6 or rounded <= 0:
        raise ValueError(f"{field} must be a positive integer, got {value!r}")
    return rounded


def iou_xywh(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection-over-union for ``(x, y, width, height)`` boxes."""

    ax, ay, aw, ah = (float(v) for v in a)
    bx, by, bw, bh = (float(v) for v in b)
    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0
    ax1, ay1 = ax + aw, ay + ah
    bx1, by1 = bx + bw, by + bh
    ix0, iy0 = max(ax, bx), max(ay, by)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    union = aw * ah + bw * bh - intersection
    return intersection / union if union > 0.0 else 0.0


def _bounded_box(
    box: Sequence[int], image_width: int, image_height: int
) -> tuple[int, int, int, int]:
    if len(box) != 4:
        raise ValueError("box must contain x, y, width, height")
    x, y, w, h = (int(v) for v in box)
    if w <= 0 or h <= 0:
        raise ValueError(f"box has non-positive dimensions: {tuple(box)!r}")
    if x < 0 or y < 0 or x + w > image_width or y + h > image_height:
        raise ValueError(
            f"box {tuple(box)!r} is outside image {image_width}x{image_height}"
        )
    return x, y, w, h


def occluder_rect(
    box: Sequence[int], level_percent: int, placement: str
) -> tuple[int, int, int, int]:
    """Return a deterministic occluder rectangle bounded by ``box``.

    Strip variants span one dimension of the meter.  ``center_patch`` is as
    square as the narrow meter geometry permits.  Integer quantisation means the
    realised coverage can differ slightly from the requested percentage; every
    report includes the realised ratio.
    """

    x, y, w, h = (int(v) for v in box)
    if w <= 0 or h <= 0:
        raise ValueError("box dimensions must be positive")
    if placement not in _ALL_PLACEMENTS:
        raise ValueError(
            f"unknown placement {placement!r}; expected {sorted(_ALL_PLACEMENTS)}"
        )
    level = int(level_percent)
    if level <= 0 or level >= 100:
        raise ValueError("occlusion level must be in the range 1..99")
    fraction = level / 100.0

    if placement in _HORIZONTAL:
        oh = min(h, max(1, int(round(h * fraction))))
        if placement == "top_strip":
            oy = y
        elif placement == "bottom_strip":
            oy = y + h - oh
        else:
            oy = y + (h - oh) // 2
        return x, oy, w, oh

    if placement in _VERTICAL:
        ow = min(w, max(1, int(round(w * fraction))))
        ox = x if placement == "left_strip" else x + w - ow
        return ox, y, ow, h

    target_area = max(1, int(round(w * h * fraction)))
    ow = min(w, max(1, int(round(math.sqrt(target_area)))))
    oh = min(h, max(1, int(round(target_area / float(ow)))))
    # Pick the closest bounded integer rectangle if rounding the initial height
    # left more error than adding/removing one row.
    candidates = []
    for candidate_h in {max(1, oh - 1), oh, min(h, oh + 1)}:
        candidates.append((abs(ow * candidate_h - target_area), candidate_h))
    _, oh = min(candidates)
    ox = x + (w - ow) // 2
    oy = y + (h - oh) // 2
    return ox, oy, ow, oh


def nearby_ring_median(
    frame_bgr: np.ndarray,
    box: Sequence[int],
    *,
    margin: Optional[int] = None,
) -> np.ndarray:
    """Return the BGR median of a background ring surrounding ``box``."""

    if not isinstance(frame_bgr, np.ndarray) or frame_bgr.ndim != 3:
        raise ValueError("frame must be an HxWxC numpy array")
    height, width = frame_bgr.shape[:2]
    x, y, w, h = _bounded_box(box, width, height)
    pad = (
        max(3, int(round(max(w, h) * 0.10)))
        if margin is None
        else max(1, int(margin))
    )
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(width, x + w + pad), min(height, y + h + pad)
    crop = frame_bgr[y0:y1, x0:x1]
    mask = np.ones(crop.shape[:2], dtype=bool)
    mask[y - y0 : y + h - y0, x - x0 : x + w - x0] = False
    pixels = crop[mask]
    if pixels.size == 0:
        # This is reachable only if a box fills the entire frame.  The fallback
        # remains deterministic and makes the function total for synthetic tests.
        pixels = frame_bgr.reshape(-1, frame_bgr.shape[2])
    median = np.median(pixels.astype(np.float64), axis=0)
    if np.issubdtype(frame_bgr.dtype, np.integer):
        info = np.iinfo(frame_bgr.dtype)
        median = np.clip(np.rint(median), info.min, info.max)
    return median.astype(frame_bgr.dtype)


def apply_occlusion(
    frame_bgr: np.ndarray,
    box: Sequence[int],
    level_percent: int,
    placement: str,
) -> tuple[np.ndarray, Optional[tuple[int, int, int, int]], float, np.ndarray]:
    """Copy ``frame_bgr`` and fill one bounded occluder with ring background."""

    if not isinstance(frame_bgr, np.ndarray) or frame_bgr.ndim != 3:
        raise ValueError("frame must be an HxWxC numpy array")
    height, width = frame_bgr.shape[:2]
    x, y, w, h = _bounded_box(box, width, height)
    fill = nearby_ring_median(frame_bgr, (x, y, w, h))
    result = frame_bgr.copy()
    level = int(level_percent)
    if level == 0:
        return result, None, 0.0, fill
    rect = occluder_rect((x, y, w, h), level, placement)
    ox, oy, ow, oh = rect
    result[oy : oy + oh, ox : ox + ow] = fill
    realised = (ow * oh) / float(w * h)
    return result, rect, realised, fill


def _label_box(
    label_path: Path, image_width: int, image_height: int
) -> tuple[float, float, float, float]:
    lines = [line.strip() for line in label_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"{label_path}: expected exactly one positive label, found {len(lines)}")
    fields = lines[0].split()
    if len(fields) != 5 or fields[0] != "0":
        raise ValueError(f"{label_path}: expected one class-0 xywh label")
    cx, cy, nw, nh = (_finite_float(v, "label coordinate") for v in fields[1:])
    if not all(0.0 <= v <= 1.0 for v in (cx, cy, nw, nh)) or nw <= 0 or nh <= 0:
        raise ValueError(f"{label_path}: invalid normalised label coordinates")
    w, h = nw * image_width, nh * image_height
    return cx * image_width - w / 2.0, cy * image_height - h / 2.0, w, h


def verify_manifest_label(
    image_path: Path,
    label_path: Path,
    manifest_box: Sequence[int],
    *,
    tolerance_px: float = 1.1,
) -> None:
    """Cross-check a manifest box against its materialised YOLO label."""

    frame = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError(f"unreadable hard-set image: {image_path}")
    height, width = frame.shape[:2]
    expected = _bounded_box(manifest_box, width, height)
    if not label_path.is_file():
        raise ValueError(f"missing hard-set label: {label_path}")
    actual = _label_box(label_path, width, height)
    delta = max(abs(float(a) - float(b)) for a, b in zip(expected, actual))
    if delta > float(tolerance_px):
        raise ValueError(
            f"{label_path}: label/manifest mismatch {delta:.3f}px exceeds "
            f"{tolerance_px:.3f}px; manifest={expected!r} label={actual!r}"
        )


def load_verified_samples(
    hardset: os.PathLike[str] | str,
    *,
    split: str = "heldout",
    max_images: int = 0,
) -> list[Sample]:
    """Load stable-order positive rows and cross-check every materialised label."""

    root = Path(hardset).resolve()
    manifest_path = root / "manifest.csv"
    if not manifest_path.is_file():
        raise ValueError(f"missing hard-set manifest: {manifest_path}")
    with manifest_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = sorted(_REQUIRED_MANIFEST_COLUMNS - columns)
        if missing:
            raise ValueError(f"manifest missing required columns: {', '.join(missing)}")
        rows = list(reader)

    selected = [
        row
        for row in rows
        if row.get("split") == split and row.get("source") in _POSITIVE_SOURCES
    ]
    selected.sort(
        key=lambda row: (
            str(row.get("session", "")),
            int(_finite_float(row.get("idx"), "idx")),
            str(row.get("image", "")),
        )
    )
    if max_images > 0:
        selected = selected[: int(max_images)]
    if not selected:
        raise ValueError(f"manifest contains no positive rows for split={split!r}")

    samples: list[Sample] = []
    for row in selected:
        image_name = str(row["image"])
        if Path(image_name).name != image_name:
            raise ValueError(f"manifest image must be a basename: {image_name!r}")
        x = int(round(_finite_float(row["x"], "x")))
        y = int(round(_finite_float(row["y"], "y")))
        w = _positive_int(row["w"], "w")
        h = _positive_int(row["h"], "h")
        image_path = root / "images" / split / image_name
        label_path = root / "labels" / split / (Path(image_name).stem + ".txt")
        verify_manifest_label(image_path, label_path, (x, y, w, h))
        fill_raw = row.get("fill_est", "")
        fill_est = None if fill_raw in (None, "") else _finite_float(fill_raw, "fill_est")
        samples.append(
            Sample(
                image=image_name,
                image_path=image_path,
                label_path=label_path,
                session=str(row["session"]),
                idx=int(_finite_float(row["idx"], "idx")),
                source=str(row["source"]),
                box=(x, y, w, h),
                fill_est=fill_est,
            )
        )
    return samples


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), percentile))


def _round(value: float) -> float:
    return round(float(value), 6)


def _summary_cell(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    latencies = [float(row["latency_ms"]) for row in records]
    count = len(records)
    hits = sum(bool(row["hit"]) for row in records)
    return {
        "count": count,
        "hits": hits,
        "recall": _round(hits / count) if count else 0.0,
        "latency_ms": {
            "median": _round(_percentile(latencies, 50)),
            "p90": _round(_percentile(latencies, 90)),
            "p95": _round(_percentile(latencies, 95)),
            "p99": _round(_percentile(latencies, 99)),
            "max": _round(max(latencies) if latencies else 0.0),
        },
    }


def summarise_records(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_level: dict[str, Any] = {}
    by_cell: dict[str, Any] = {}
    for level in sorted({int(row["occlusion_percent"]) for row in records}):
        level_rows = [row for row in records if int(row["occlusion_percent"]) == level]
        by_level[str(level)] = _summary_cell(level_rows)
        placements = sorted({str(row["placement"]) for row in level_rows})
        by_cell[str(level)] = {
            placement: _summary_cell(
                [row for row in level_rows if str(row["placement"]) == placement]
            )
            for placement in placements
        }
    return {
        "overall": _summary_cell(records),
        "by_level": by_level,
        "by_level_and_placement": by_cell,
    }


def evaluate(
    locator: Any,
    samples: Sequence[Sample],
    *,
    levels: Sequence[int] = DEFAULT_LEVELS,
    placements: Sequence[str] = DEFAULT_PLACEMENTS,
    iou_threshold: float = 0.30,
    warmup: int = 5,
    progress_every: int = 25,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Evaluate a loaded runtime locator and return stable-order detailed rows."""

    unique_levels = tuple(dict.fromkeys(int(level) for level in levels))
    if not unique_levels or any(level < 0 or level >= 100 for level in unique_levels):
        raise ValueError("levels must contain unique values in the range 0..99")
    unique_placements = tuple(dict.fromkeys(str(value) for value in placements))
    if not unique_placements or any(value not in _ALL_PLACEMENTS for value in unique_placements):
        raise ValueError(f"placements must come from {sorted(_ALL_PLACEMENTS)}")
    if not (0.0 < float(iou_threshold) <= 1.0):
        raise ValueError("IoU threshold must be in the range (0, 1]")
    if not samples:
        raise ValueError("at least one sample is required")

    first = cv2.imread(str(samples[0].image_path), cv2.IMREAD_COLOR)
    if first is None:
        raise ValueError(f"unreadable hard-set image: {samples[0].image_path}")
    for _ in range(max(0, int(warmup))):
        locator.detect_box(first)

    records: list[dict[str, Any]] = []
    for sample_index, sample in enumerate(samples):
        frame = cv2.imread(str(sample.image_path), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError(f"unreadable hard-set image: {sample.image_path}")
        variants: Iterable[tuple[int, str]] = (
            (level, "clean") if level == 0 else (level, placement)
            for level in unique_levels
            for placement in (("clean",) if level == 0 else unique_placements)
        )
        for level, placement in variants:
            if level == 0:
                perturbed = frame.copy()
                rect = None
                realised = 0.0
                fill = nearby_ring_median(frame, sample.box)
            else:
                perturbed, rect, realised, fill = apply_occlusion(
                    frame, sample.box, level, placement
                )
            start_ns = time.perf_counter_ns()
            detected = locator.detect_box(perturbed)
            latency_ms = (time.perf_counter_ns() - start_ns) / 1_000_000.0
            if detected is None:
                detected_box = None
                confidence = 0.0
                overlap = 0.0
            else:
                dx, dy, dw, dh, confidence = detected
                detected_box = [int(dx), int(dy), int(dw), int(dh)]
                overlap = iou_xywh(detected_box, sample.box)
            records.append(
                {
                    "sample_index": sample_index,
                    "session": sample.session,
                    "idx": sample.idx,
                    "image": sample.image,
                    "source": sample.source,
                    "fill_est": sample.fill_est,
                    "gt_box": list(sample.box),
                    "occlusion_percent": level,
                    "placement": placement,
                    "occluder_rect": None if rect is None else list(rect),
                    "realised_occlusion": _round(realised),
                    "occluder_bgr": [int(value) for value in fill],
                    "detected_box": detected_box,
                    "confidence": _round(float(confidence)),
                    "iou": _round(overlap),
                    "hit": bool(detected is not None and overlap >= iou_threshold),
                    "latency_ms": _round(latency_ms),
                }
            )
        if progress_every > 0 and (
            (sample_index + 1) % progress_every == 0
            or sample_index + 1 == len(samples)
        ):
            print(f"evaluated {sample_index + 1}/{len(samples)} images", flush=True)
    return records, summarise_records(records)


def recall_gate_failures(summary: Mapping[str, Any], minimum: float) -> list[str]:
    """Return stable level/placement descriptions below ``minimum`` recall."""

    threshold = float(minimum)
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("minimum recall must be in the range 0..1")
    failures: list[str] = []
    cells = summary.get("by_level_and_placement", {})
    for level in sorted(cells, key=lambda value: int(value)):
        for placement in sorted(cells[level]):
            cell = cells[level][placement]
            if int(cell.get("count", 0)) and float(cell.get("recall", 0.0)) < threshold:
                failures.append(
                    f"level={level}% placement={placement} recall="
                    f"{float(cell['recall']):.6f} < {threshold:.6f}"
                )
    return failures


def _write_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sample_index",
        "session",
        "idx",
        "image",
        "source",
        "fill_est",
        "gt_box",
        "occlusion_percent",
        "placement",
        "occluder_rect",
        "realised_occlusion",
        "occluder_bgr",
        "detected_box",
        "confidence",
        "iou",
        "hit",
        "latency_ms",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for record in records:
            row = dict(record)
            for key in ("gt_box", "occluder_rect", "occluder_bgr", "detected_box"):
                row[key] = "" if row[key] is None else json.dumps(row[key], separators=(",", ":"))
            writer.writerow(row)


def _print_report(report: Mapping[str, Any]) -> None:
    config = report["config"]
    print("\n# Meter synthetic-occlusion robustness")
    print(
        "NOTE: manifest-declared heldout rows only; this evaluator does not "
        "independently certify training-set separation."
    )
    print(
        f"model={config['model']} sha256={config['model_sha256']} "
        f"provider={config['provider']} confidence={config['confidence_threshold']:.3f}"
    )
    print(
        f"images={report['dataset']['images']} variants={report['dataset']['variants']} "
        f"IoU>={config['iou_threshold']:.2f}"
    )
    print("\n| occlusion | placement | hits / count | recall | median ms | p95 ms |")
    print("|---:|:---|---:|---:|---:|---:|")
    for level in sorted(report["summary"]["by_level_and_placement"], key=int):
        for placement, cell in report["summary"]["by_level_and_placement"][level].items():
            print(
                f"| {level}% | {placement} | {cell['hits']} / {cell['count']} | "
                f"{100.0 * cell['recall']:.2f}% | "
                f"{cell['latency_ms']['median']:.3f} | {cell['latency_ms']['p95']:.3f} |"
            )
    print("\n| occlusion aggregate | hits / count | recall | median ms | p95 ms |")
    print("|---:|---:|---:|---:|---:|")
    for level, cell in report["summary"]["by_level"].items():
        print(
            f"| {level}% | {cell['hits']} / {cell['count']} | "
            f"{100.0 * cell['recall']:.2f}% | "
            f"{cell['latency_ms']['median']:.3f} | {cell['latency_ms']['p95']:.3f} |"
        )


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--hardset", type=Path, default=DEFAULT_HARDSET)
    parser.add_argument("--split", default="heldout")
    parser.add_argument("--levels", nargs="+", type=int, default=list(DEFAULT_LEVELS))
    parser.add_argument(
        "--placements", nargs="+", default=list(DEFAULT_PLACEMENTS), choices=DEFAULT_PLACEMENTS
    )
    parser.add_argument("--conf", type=float, default=0.35)
    parser.add_argument("--iou", type=float, default=0.30)
    parser.add_argument("--warmup", type=int, default=5)
    parser.add_argument("--max-images", type=int, default=0)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument(
        "--min-recall",
        type=float,
        default=None,
        help="fail exit 3 if any level/placement cell is below this 0..1 recall",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="write meter_occlusion_report.json and meter_occlusion_rows.csv",
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        model = args.model.resolve()
        hardset = args.hardset.resolve()
        if not model.is_file():
            raise ValueError(f"model does not exist: {model}")
        if not 0.0 < args.conf <= 1.0:
            raise ValueError("confidence threshold must be in the range (0, 1]")
        samples = load_verified_samples(
            hardset, split=args.split, max_images=args.max_images
        )
        from meter_detector_yolo import MeterYoloLocator

        locator = MeterYoloLocator(model_path=str(model), conf_thres=args.conf)
        if not locator.ok:
            raise ValueError(f"runtime MeterYoloLocator could not load {model}")
        records, summary = evaluate(
            locator,
            samples,
            levels=args.levels,
            placements=args.placements,
            iou_threshold=args.iou,
            warmup=args.warmup,
            progress_every=args.progress_every,
        )
        manifest = hardset / "manifest.csv"
        report: dict[str, Any] = {
            "schema_version": 1,
            "scope": (
                "Synthetic deterministic occlusion on manifest-declared heldout positives; "
                "no independent training-leakage audit or real-occlusion claim."
            ),
            "config": {
                "model": str(model),
                "model_sha256": sha256_file(model),
                "provider": str(locator.provider),
                "model_imgsz": int(locator.imgsz),
                "confidence_threshold": float(args.conf),
                "iou_threshold": float(args.iou),
                "levels_percent": list(dict.fromkeys(int(v) for v in args.levels)),
                "placements": list(dict.fromkeys(str(v) for v in args.placements)),
                "warmup": max(0, int(args.warmup)),
            },
            "dataset": {
                "hardset": str(hardset),
                "manifest": str(manifest),
                "manifest_sha256": sha256_file(manifest),
                "split": args.split,
                "images": len(samples),
                "variants": len(records),
                "labels_cross_checked": len(samples),
            },
            "summary": summary,
            "gate": {"minimum_recall": args.min_recall, "failures": []},
        }
        if args.min_recall is not None:
            report["gate"]["failures"] = recall_gate_failures(summary, args.min_recall)

        _print_report(report)
        if args.output_dir is not None:
            output_dir = args.output_dir.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            csv_path = output_dir / "meter_occlusion_rows.csv"
            json_path = output_dir / "meter_occlusion_report.json"
            _write_csv(csv_path, records)
            json_path.write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            print(f"\nJSON: {json_path}")
            print(f"CSV:  {csv_path}")
        failures = report["gate"]["failures"]
        if failures:
            print("\nRECALL GATE FAILED:", file=sys.stderr)
            for failure in failures:
                print(f"- {failure}", file=sys.stderr)
            return 3
        return 0
    except (OSError, ValueError) as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
