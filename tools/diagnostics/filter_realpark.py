#!/usr/bin/env python3
"""Filter the auto-labeled real-park set (datasets/meter_real_park) down to genuine shot-meter labels,
dropping the MENU/CARD/STATS-screen false positives the auto-labeler emits on the big session clips
(Player Matchup stat bars, Player-of-the-Game card reveals, MP Recap, Who's Online, Game Stats).

The auto-labeler's green-anchor + red/purple track latches onto green attribute bars / card art on
full-screen menus. A geometry + play-band gate keeps only thin, tall, player-attached, gameplay-region
boxes -- tuned to the KNOWN-GAMEPLAY (Jun-24) label distribution. Verified via KEPT/REJECTED montages:
KEPT is dominated by real red/purple meters on court, REJECTED is dominated by menu screens.

ASYMMETRY (deliberate): a menu wrongly kept as a POSITIVE teaches the "locks onto random objects" ship
blocker, whereas dropping a real meter only costs a little recall. So the gate errs toward rejecting.

Rebuilds train.txt/val.txt with a CLIP-HELD-OUT val split (no temporal leakage). Run after
autolabel_park_clips.py, before training.
"""
import argparse, glob, os
import numpy as np


def keep(cx, cy, w, h):
    asp = h / max(w, 1e-6)
    return (0.03 <= cx <= 0.93) and (0.18 <= cy <= 0.86) and \
           (0.005 <= w <= 0.020) and (0.015 <= h <= 0.20) and (2.2 <= asp <= 12.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join("datasets", "meter_real_park"))
    ap.add_argument("--val-clip", default="20260624212347", help="held-out clip id for val (no leakage)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    d = args.dir if os.path.isabs(args.dir) else os.path.join(root, args.dir)
    img_dir, lab_dir = os.path.join(d, "images"), os.path.join(d, "labels")

    kept, dropped_lbl, dropped_img = [], 0, 0
    for lp in glob.glob(os.path.join(lab_dir, "*.txt")):
        lines = [l for l in open(lp) if len(l.split()) == 5]
        good = [l for l in lines if keep(*[float(x) for x in l.split()[1:]])]
        stem = os.path.splitext(os.path.basename(lp))[0]
        ip = os.path.join(img_dir, stem + ".png")
        if not os.path.isfile(ip):
            ip = os.path.join(img_dir, stem + ".jpg")
        if not good:
            # no surviving meter box -> drop the frame entirely (it was a menu/false-positive frame)
            dropped_lbl += len(lines); dropped_img += 1
            if not args.dry_run:
                if os.path.isfile(lp):
                    os.remove(lp)
                if os.path.isfile(ip):
                    os.remove(ip)
            continue
        dropped_lbl += len(lines) - len(good)
        if not args.dry_run and len(good) != len(lines):
            open(lp, "w").write("".join(good))
        kept.append(ip)

    # clip-held-out split
    train = [p for p in kept if args.val_clip not in os.path.basename(p)]
    val = [p for p in kept if args.val_clip in os.path.basename(p)]
    if not args.dry_run:
        open(os.path.join(d, "train.txt"), "w").write("\n".join(sorted(train)) + "\n")
        open(os.path.join(d, "val.txt"), "w").write("\n".join(sorted(val)) + "\n")
        for c in glob.glob(os.path.join(d, "*.cache")):
            os.remove(c)
    print(f"KEPT {len(kept)} frames (train {len(train)} / val {len(val)}) | "
          f"dropped {dropped_img} menu/false frames, {dropped_lbl} boxes"
          + ("  [DRY-RUN]" if args.dry_run else ""))


if __name__ == "__main__":
    main()
