#!/usr/bin/env python3
"""Fast interactive STAMINA-BAR labeler (run at the PC / over RDP with a display).

Shows each extracted frame with my HSV PROPOSAL pre-drawn (red). For each frame:
  - press  A     -> accept the red proposal as the label (the common case)
  - DRAG mouse   -> draw the correct box, then press ENTER/SPACE to save it
  - press  N     -> no stamina bar visible in this frame (skip, no label)
  - press  B     -> go back to the previous frame
  - press  Q     -> quit and save progress (resumable)
Writes YOLO labels (class 0 = stamina_bar) + a dataset yaml; then run pose-style training.
Usage: C:\\Python314\\python.exe tools/training/label_bars.py
"""
from __future__ import annotations

import glob
import json
import os

import cv2

ROOT = "logs/diagnostics/bar_label"
DISP_W = 1280

_drag = {"down": False, "x0": 0, "y0": 0, "x1": 0, "y1": 0, "box": None}


def _on_mouse(ev, x, y, flags, _):
    if ev == cv2.EVENT_LBUTTONDOWN:
        _drag.update(down=True, x0=x, y0=y, x1=x, y1=y, box=None)
    elif ev == cv2.EVENT_MOUSEMOVE and _drag["down"]:
        _drag["x1"], _drag["y1"] = x, y
    elif ev == cv2.EVENT_LBUTTONUP:
        _drag["down"] = False
        x0, y0, x1, y1 = _drag["x0"], _drag["y0"], x, y
        _drag["box"] = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def main() -> int:
    imgs = sorted(glob.glob(os.path.join(ROOT, "images", "*.jpg")))
    if not imgs:
        print("no frames - run extract_bar_label_frames.py first")
        return 1
    os.makedirs(os.path.join(ROOT, "labels"), exist_ok=True)
    proposals = json.load(open(os.path.join(ROOT, "proposals.json"))) if os.path.exists(os.path.join(ROOT, "proposals.json")) else {}
    cv2.namedWindow("label bars"); cv2.setMouseCallback("label bars", _on_mouse)
    i, done = 0, 0
    while 0 <= i < len(imgs):
        path = imgs[i]; stem = os.path.splitext(os.path.basename(path))[0]
        lp = os.path.join(ROOT, "labels", stem + ".txt")
        fr = cv2.imread(path); H, W = fr.shape[:2]
        scale = DISP_W / W; disp = cv2.resize(fr, (DISP_W, int(H * scale)))
        prop = proposals.get(stem)
        _drag["box"] = None
        while True:
            view = disp.copy()
            if prop:
                x, y, w, h = prop
                cv2.rectangle(view, (int(x * scale), int(y * scale)), (int((x + w) * scale), int((y + h) * scale)), (0, 0, 255), 2)
                cv2.putText(view, "A=accept proposal", (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            if _drag["down"] or _drag["box"]:
                b = (_drag["x0"], _drag["y0"], _drag["x1"], _drag["y1"]) if _drag["down"] else _drag["box"]
                cv2.rectangle(view, (b[0], b[1]), (b[2], b[3]), (0, 255, 0), 2)
            cv2.putText(view, f"{i+1}/{len(imgs)}  drag=box ENTER=save  N=none B=back Q=quit  (labeled {done})",
                        (10, view.shape[0] - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
            cv2.imshow("label bars", view)
            k = cv2.waitKey(20) & 0xFF
            if k == ord('q'):
                cv2.destroyAllWindows(); print(f"saved {done} labels"); _write_yaml(); return 0
            if k == ord('b'):
                i = max(0, i - 1); break
            if k == ord('n'):
                open(lp, "w").close(); i += 1; break          # negative (no bar)
            if k == ord('a') and prop:
                _save(lp, prop[0], prop[1], prop[2], prop[3], W, H); done += 1; i += 1; break
            if k in (13, 32) and _drag["box"]:                # ENTER/SPACE with a drawn box
                b = _drag["box"]
                _save(lp, b[0] / scale, b[1] / scale, (b[2] - b[0]) / scale, (b[3] - b[1]) / scale, W, H)
                done += 1; i += 1; break
    cv2.destroyAllWindows(); _write_yaml(); print(f"DONE labeling: {done} labels")
    return 0


def _save(lp, x, y, w, h, W, H):
    cx, cy = (x + w / 2) / W, (y + h / 2) / H
    with open(lp, "w") as fh:
        fh.write(f"0 {cx:.6f} {cy:.6f} {w / W:.6f} {h / H:.6f}\n")


def _write_yaml():
    yaml = os.path.join(ROOT, "bars.yaml")
    with open(yaml, "w") as fh:
        fh.write(f"path: {os.path.abspath(ROOT)}\ntrain: images\nval: images\nnames:\n  0: stamina_bar\n")


if __name__ == "__main__":
    raise SystemExit(main())
