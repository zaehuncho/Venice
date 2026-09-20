#!/usr/bin/env python3
"""Audit saved detector grades by session without promoting a model.

Consumes eval_meter_lowfill.py CSVs, validates exact manifest coverage, and
prices ORION_METER_DETECTOR_CONF at 0.35 and 0.25. Dataset labels/fill estimates
are mined proxies, not human-banner outcomes or independent pixel truth.
"""
from __future__ import annotations

import argparse
import collections
import csv
import hashlib
import json
from pathlib import Path
import sys

REPO = Path(__file__).resolve().parents[2]
BUCKETS = ("0-10", "10-20", "20-30", "30-50", "50+")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def fill_bucket(value):
    number = float(value)
    for index, limit in enumerate((10, 20, 30, 50, 101)):
        if 0 <= number < limit:
            return BUCKETS[index]
    raise ValueError(f"invalid fill estimate: {value}")


def selected_manifest(rows):
    return [r for r in rows if (r["split"] == "heldout" and r["source"] != "negative")
            or (r["split"] in {"heldout", "heldout_inel"} and r["source"] == "negative")]


def key(row, evaluation=False):
    kind = row["kind"] if evaluation else ("neg" if row["source"] == "negative" else "pos")
    return row["session"], int(row["idx"]), row["split"], kind


def validate_coverage(manifest, grades):
    expected = collections.Counter(key(r) for r in selected_manifest(manifest))
    actual = collections.Counter(key(r, True) for r in grades)
    if expected != actual:
        raise ValueError(f"grade coverage mismatch: missing={sum((expected-actual).values())} "
                         f"extra={sum((actual-expected).values())}")
    if any(count != 1 for count in expected.values()):
        raise ValueError("duplicate session/frame/split keys in manifest")
    lookup = {key(r): r for r in selected_manifest(manifest)}
    for row in grades:
        if row["kind"] == "pos":
            expected_fill = float(lookup[key(row, True)]["fill_est"])
            if abs(float(row["fill_est"]) - expected_fill) > 0.001:
                raise ValueError("grade/manifest fill estimate mismatch")


def summarise(grades):
    if not grades:
        raise ValueError("empty grades")
    models = sorted(k[:-5] for k in grades[0] if k.endswith("_conf"))
    if not models:
        raise ValueError("no model confidence columns")
    cells = []
    sessions = sorted({r["session"] for r in grades}) + ["ALL"]
    for session in sessions:
        selected = grades if session == "ALL" else [r for r in grades if r["session"] == session]
        for kind in ("pos", "neg"):
            rows = [r for r in selected if r["kind"] == kind]
            for bucket in ((*BUCKETS, "ALL") if kind == "pos" else ("no-meter",)):
                sub = rows if bucket in {"ALL", "no-meter"} else [r for r in rows if fill_bucket(r["fill_est"]) == bucket]
                for model in models:
                    for conf in (0.35, 0.25):
                        count = sum(float(r[f"{model}_conf"]) >= conf and
                                    (kind == "neg" or float(r[f"{model}_iou"]) >= 0.30) for r in sub)
                        cells.append({"session": session, "kind": kind, "bucket": bucket,
                                      "model": model, "confidence": conf, "count": count,
                                      "n": len(sub), "rate": count / len(sub) if sub else None})
    return cells


def dataset_audit(manifest, hardset, compare_dirs):
    selected = selected_manifest(manifest)
    sessions = {split: sorted({r["session"] for r in manifest if r["split"] == split})
                for split in sorted({r["split"] for r in manifest})}
    trained = set(sessions.get("train", [])) | set(sessions.get("val", []))
    held = set(sessions.get("heldout", [])) | set(sessions.get("heldout_inel", []))
    hashes = collections.defaultdict(list)
    for row in selected:
        image = hardset / "images" / row["split"] / row["image"]
        label = hardset / "labels" / row["split"] / (Path(row["image"]).stem + ".txt")
        if not image.is_file() or not label.is_file():
            raise ValueError(f"missing selected image/label: {image}")
        labels = label.read_text().strip().splitlines()
        if row["source"] == "negative" and labels:
            raise ValueError(f"negative has nonempty label: {label}")
        if row["source"] != "negative" and len(labels) != 1:
            raise ValueError(f"positive must have one label: {label}")
        hashes[sha256(image)].append(str(image))
    duplicate_selected = [names for names in hashes.values() if len(names) > 1]
    intersections, count = [], 0
    for folder in compare_dirs:
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".bmp"}:
                continue
            count += 1
            digest = sha256(path)
            if digest in hashes:
                intersections.append({"training_or_val": str(path), "heldout": hashes[digest]})
    return {"counts_by_split_source": dict(sorted(collections.Counter(
                r["split"] + ":" + r["source"] for r in manifest).items())),
            "sessions_by_split": sessions,
            "named_hardset_session_overlap": sorted(trained & held),
            "selected_images": len(selected), "selected_unique_hashes": len(hashes),
            "selected_duplicate_hashes": duplicate_selected,
            "comparison_images": count, "training_val_exact_file_overlaps": intersections,
            "limitation": "Original combo Arrow2 images lack a source-session manifest; exact file hashes do not rule out re-encoded near duplicates. Fill and boxes are mined proxies."}


def missed_frame_audit(manifest, grades):
    """Runtime misses with verified propagated boxes; never call these true tracker losses."""
    grade_lookup = {key(r, True): r for r in grades}
    output = []
    for session in sorted({r["session"] for r in manifest}):
        rows = [r for r in manifest if r["session"] == session and r["source"] == "propagated"]
        if not rows:
            continue
        rows.sort(key=lambda r: int(r["idx"]))
        groups = 1 + sum(int(b["idx"]) != int(a["idx"]) + 1 for a, b in zip(rows, rows[1:]))
        motion = []
        for a, b in zip(rows, rows[1:]):
            if int(b["idx"]) - int(a["idx"]) != 1 or a["shot"] != b["shot"]:
                continue
            ax, ay = float(a["x"]) + float(a["w"]) / 2, float(a["y"]) + float(a["h"]) / 2
            bx, by = float(b["x"]) + float(b["w"]) / 2, float(b["y"]) + float(b["h"]) / 2
            motion.append(((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5)
        paired = [grade_lookup[key(r)] for r in rows if key(r) in grade_lookup]
        offline = {}
        if paired:
            models = sorted(k[:-5] for k in paired[0] if k.endswith("_conf"))
            offline = {f"{m}@{c}": sum(float(r[m + "_conf"]) >= c and float(r[m + "_iou"]) >= .30
                                       for r in paired) for m in models for c in (.35, .25)}
        output.append({"session": session, "runtime_missed_propagated_frames": len(rows),
                       "consecutive_missed_runs": groups, "adjacent_motion_pairs": len(motion),
                       "mean_label_center_displacement_px": sum(motion) / len(motion) if motion else None,
                       "max_label_center_displacement_px": max(motion) if motion else None,
                       "offline_graded_missed_frames": len(paired), "offline_detectable_hits": offline})
    return output


def markdown(report):
    lines = ["# Low-fill evaluation: session audit", "",
             "Threshold knob: `ORION_METER_DETECTOR_CONF`; table prices 0.35 versus 0.25 without changing the live value.",
             "Mined labels/fill estimates are proxies; these detector grades do not establish human-banner outcomes.", "",
             "| Session | Kind | Fill | Model | Conf | Hits or FP / n | Rate |",
             "|---|---|---|---|---:|---:|---:|"]
    for row in report["cells"]:
        rate = "unavailable" if row["rate"] is None else f"{100*row['rate']:.2f}%"
        lines.append(f"| {row['session']} | {row['kind']} | {row['bucket']} | {row['model']} | {row['confidence']:.2f} | {row['count']}/{row['n']} | {rate} |")
    lines += ["", "## Integrity", "", "```json", json.dumps(report["dataset"], indent=2), "```", "",
              "## Camera/occlusion limits", "",
              "Propagated boxes identify runtime-missed visible candidates. Offline detectability separates model misses from async/tracking/lifecycle candidates, not tracking alone. Consecutive missed runs are not shot-abort or tracker-loss counts. True center error, camera setting, and live acquisition latency need independent labels and the next live batch.", "",
              "```json", json.dumps(report["runtime_missed_candidates"], indent=2), "```", ""]
    return "\n".join(lines)


def self_test():
    assert [fill_bucket(v) for v in (0, 10, 20, 30, 50, 100)] == [*BUCKETS, "50+"]
    try:
        fill_bucket(float("nan"))
    except ValueError:
        pass
    else:
        raise AssertionError("nonfinite fill accepted")
    manifest = [{"session": "s1", "idx": "1", "split": "heldout", "source": "propagated", "fill_est": "15"}]
    grades = [{"session": "s1", "idx": "1", "split": "heldout", "kind": "pos", "fill_est": "15", "new_conf": ".3", "new_iou": ".8"}]
    validate_coverage(manifest, grades)
    try:
        validate_coverage(manifest, [])
    except ValueError:
        pass
    else:
        raise AssertionError("missing grade accepted")
    cells = summarise(grades)
    actual = {(r["confidence"], r["count"], r["n"]) for r in cells if r["session"] == "ALL" and r["bucket"] == "10-20"}
    assert actual == {(.35, 0, 1), (.25, 1, 1)}
    assert all(r["rate"] is None for r in cells if r["bucket"] == "0-10")
    grades[0]["new_iou"] = ".29"
    assert all(r["count"] == 0 for r in summarise(grades))
    print("LOWFILL AUDIT SELFTEST: 7 passed")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hardset", type=Path, default=REPO / "runs/detect/logs/diagnostics/meter_train/lowfill_hardset")
    parser.add_argument("--grades", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)
    if args.self_test:
        return self_test()
    if args.grades is None or args.output is None:
        parser.error("--grades and --output are required")
    manifest = read_csv(args.hardset / "manifest.csv")
    grades = read_csv(args.grades)
    validate_coverage(manifest, grades)
    dirs = [REPO / "datasets" / dataset / "images" / split
            for dataset in ("meter2k27", "meter2k27_pill_park") for split in ("train", "val")]
    dirs += [args.hardset / "images" / split for split in ("train", "val")]
    report = {"grades_path": str(args.grades.resolve()), "grades_sha256": sha256(args.grades),
              "manifest_sha256": sha256(args.hardset / "manifest.csv"),
              "dataset": dataset_audit(manifest, args.hardset, dirs),
              "cells": summarise(grades), "runtime_missed_candidates": missed_frame_audit(manifest, grades)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    negatives = sum(r["kind"] == "neg" for r in grades)
    print(f"LOWFILL AUDIT: rows={len(grades)} negatives={negatives} output={args.output.resolve()}")
    audit = report["dataset"]
    print(f"LOWFILL AUDIT: session_overlap={len(audit['named_hardset_session_overlap'])} exact_file_overlap={len(audit['training_val_exact_file_overlaps'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
