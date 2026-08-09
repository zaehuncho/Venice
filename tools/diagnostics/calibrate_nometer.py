#!/usr/bin/env python3
"""No-meter calibration: pair the user's shot ANIMATION landmark with the feedback BANNER verdict.

For each clip: temporally lock the user (StaminaTracker), read the feedback banner each frame, and for
every EXCELLENT banner onset (a made-timing shot), find the user's shooting-wrist trajectory just
before it -> the wrist-raise ONSET (jump-start landmark) and PEAK (release proxy). The landmark->banner
offsets, if tight across shots, are the steady reference no-meter mode schedules the release from.
This measures the real YIELD (how many clean shots) + the offset consistency. See docs/ANIMATION_ANCHOR.md.

Usage: C:\\Python314\\python.exe tools/diagnostics/calibrate_nometer.py
"""
from __future__ import annotations

import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> int:
    os.environ.setdefault("YOLO_VERBOSE", "False")
    import cv2
    from ultralytics import YOLO
    import stamina_lock as SL
    import banner_reader as BR

    U = r"C:/Users/Administrator/.claude/uploads/6c02bdee-81f8-4af7-8256-c327f3d49019"
    clips = sorted(glob.glob(U + "/*master_playlist.mp4"))
    m = YOLO("models/orion_pose2k_n.pt")
    print(f"clips: {len(clips)}")

    onset_offsets = []   # jump-start landmark -> banner (ms)
    peak_offsets = []    # wrist peak (release) -> banner (ms)
    shots_total = shots_clean = 0

    for clip in clips:
        cap = cv2.VideoCapture(clip)
        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        trk = SL.StaminaTracker(m)
        rows = []  # (t, wristY_norm or nan, verdict)
        fi = 0
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            res = trk.update(fr)
            swy = np.nan
            if res is not None and res["kpts"] is not None:
                H = fr.shape[0]
                wy = [res["kpts"][j, 1] for j in (9, 10) if res["kpts"][j, 2] > 0.3]
                if wy:
                    swy = min(wy) / H
            v = BR.read_banner(fr)["verdict"]
            rows.append((fi / fps, swy, v))
            fi += 1
        cap.release()

        t = np.array([r[0] for r in rows])
        wy = np.array([r[1] for r in rows])
        verds = [r[2] for r in rows]
        # banner onsets (none/off -> excellent)
        onsets = [i for i in range(1, len(verds)) if verds[i] == "excellent" and verds[i - 1] != "excellent"]
        clip_clean = 0
        rest = np.nanmedian(wy) if np.any(~np.isnan(wy)) else np.nan
        for oi in onsets:
            shots_total += 1
            tb = t[oi]
            lo, hi = tb - 1.6, tb + 0.05
            idx = [j for j in range(len(t)) if lo <= t[j] <= hi and not np.isnan(wy[j])]
            if len(idx) < 4 or np.isnan(rest):
                continue
            sub_t = t[idx]; sub_wy = wy[idx]
            pk = idx[int(np.argmin(sub_wy))]            # wrist highest = release proxy
            if wy[pk] > rest - 0.08:                     # no real raise -> not a clean shot
                continue
            # jump-start = first frame before the peak where the wrist drops below rest-0.05
            onset_j = pk
            for j in idx:
                if j <= pk and wy[j] < rest - 0.05:
                    onset_j = j
                    break
            shots_clean += 1
            clip_clean += 1
            peak_offsets.append((tb - t[pk]) * 1000.0)
            onset_offsets.append((tb - t[onset_j]) * 1000.0)
        print(f"  {os.path.basename(clip)[:18]}: banner-onsets {len(onsets)}, clean shots {clip_clean}")

    print(f"\n=== YIELD: {shots_clean}/{shots_total} EXCELLENT shots had a clean user landmark ===")
    for name, arr in [("jump-start -> banner", onset_offsets), ("wrist-peak(release) -> banner", peak_offsets)]:
        if len(arr) >= 4:
            a = np.array(arr)
            print(f"  {name}: n={len(a)}  mean={a.mean():.0f}ms  std={a.std():.0f}ms  "
                  f"[{a.min():.0f},{a.max():.0f}]")
        else:
            print(f"  {name}: too few ({len(arr)})")
    print("\nLOW std on jump-start->banner => the animation landmark is a steady no-meter reference\n"
          "(release fires at jumpStart + learned_offset). HIGH std/low yield => need a labeled bar detector.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
