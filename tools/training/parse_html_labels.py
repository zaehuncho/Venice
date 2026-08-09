#!/usr/bin/env python3
"""Parse labels pasted back from the HTML labeler into YOLO label files.

Input lines (from the labeler's Copy button):  <frame.jpg> <cx> <cy> <w> <h>   (normalized 0-1)
or  <frame.jpg> none.  Writes labels into logs/diagnostics/bar_label/labels/ for training.

Usage: paste the text into a file then:
  C:\\Python314\\python.exe tools/training/parse_html_labels.py <pasted.txt>
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: parse_html_labels.py <pasted_labels.txt>")
        return 2
    root = "logs/diagnostics/bar_label"
    os.makedirs(os.path.join(root, "labels"), exist_ok=True)
    n_box = n_none = n_skip = 0
    for line in open(sys.argv[1], encoding="utf-8"):
        parts = line.split()
        if not parts:
            continue
        name = parts[0]
        stem = os.path.splitext(name)[0]
        if not os.path.exists(os.path.join(root, "images", name)):
            print(f"  skip (no image): {name}"); n_skip += 1; continue
        lp = os.path.join(root, "labels", stem + ".txt")
        if len(parts) >= 2 and parts[1].lower() == "none":
            open(lp, "w").close(); n_none += 1; continue
        if len(parts) < 5:
            n_skip += 1; continue
        cx, cy, w, h = (float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4]))
        with open(lp, "w") as fh:
            fh.write(f"0 {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}\n")
        n_box += 1
    print(f"wrote {n_box} bar labels + {n_none} no-bar ({n_skip} skipped)")
    pos = sum(1 for f in os.listdir(os.path.join(root, "labels")) if os.path.getsize(os.path.join(root, "labels", f)) > 0)
    print(f"total positive labels now: {pos}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
