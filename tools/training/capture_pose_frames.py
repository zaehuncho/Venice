"""Standalone capture-card frame grabber for pose-model training data.

WHY THIS EXISTS INSTEAD OF ORION'S FRAMEDUMP
--------------------------------------------
Orion's built-in framedump is armed by ORION_FRAMEDUMP* env vars read once in
RemotePlayOrchestrator.__init__. The launcher self-elevates via
`Start-Process -Verb RunAs`, which builds a FRESH environment block for the
elevated process -- so env vars set by a wrapper do not survive the UAC hop and
the dump silently never arms. Rather than fight that, this reads the capture
card directly.

For pure pose data none of Orion's pipeline is needed: no detector, no sidecar,
no elevation, no meter. Just frames off the card.

HARD REQUIREMENT: the capture card is single-open. CLOSE ORION FIRST or this
will fail to open the device (same conflict as OBS holding the Elgato).

USAGE
    python tools/training/capture_pose_frames.py            # 2 fps, JPEG q95
    python tools/training/capture_pose_frames.py --fps 3
    python tools/training/capture_pose_frames.py --png      # lossless, ~10x bigger
    Ctrl+C to stop early; everything written so far is kept.

FORMAT NOTE: defaults to JPEG q95, not PNG. The pose model's existing 40k
training frames came from H.264 video, so lightly-compressed JPEG is CLOSER to
that distribution than lossless PNG -- and it is ~10x smaller (2.4 GB vs 24 GB
for 12k frames). Pass --png if you specifically want lossless.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

try:
    import cv2
except ImportError:
    sys.exit("opencv not available. Use the repo venv:\n"
             r"  .\.venv\Scripts\python.exe tools\training\capture_pose_frames.py")


def main() -> int:
    ap = argparse.ArgumentParser(description="Grab capture-card frames for pose training.")
    ap.add_argument("--index", type=int, default=0,
                    help="DirectShow device index (settings.json capture_card_index = 0)")
    ap.add_argument("--fps", type=float, default=2.0,
                    help="frames SAVED per second (source stays 60fps); default 2")
    ap.add_argument("--max", type=int, default=12000, help="stop after N saved frames")
    ap.add_argument("--out", default=r"D:\VeniceTraining\framedump",
                    help="output root; a session_<timestamp> dir is created inside")
    ap.add_argument("--png", action="store_true", help="lossless PNG instead of JPEG q95")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    args = ap.parse_args()

    if args.fps <= 0:
        sys.exit("--fps must be > 0")
    interval = 1.0 / args.fps

    # DSHOW explicitly: it is the backend Orion negotiates against, and MSMF
    # reports different geometry on this card.
    cap = cv2.VideoCapture(args.index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        sys.exit(
            f"Could not open capture device index {args.index}.\n"
            "  * Is Orion still running? It holds the card exclusively -- close it.\n"
            "  * Is OBS (or any other capture app) open?\n"
            "  * Try a different --index if you have multiple video devices."
        )

    # MJPG is required for 1080p60 over USB on Elgato-class cards; without it the
    # driver silently negotiates a lower mode.
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    cap.set(cv2.CAP_PROP_FPS, 60)

    got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    got_fps = cap.get(cv2.CAP_PROP_FPS)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session = os.path.join(args.out, f"session_{stamp}")
    os.makedirs(session, exist_ok=True)

    ext = ".png" if args.png else ".jpg"
    write_params = [] if args.png else [int(cv2.IMWRITE_JPEG_QUALITY), 95]

    print(f"  device index : {args.index} (DSHOW)")
    print(f"  negotiated   : {got_w}x{got_h} @ {got_fps:g}fps")
    if got_w != args.width or got_h != args.height:
        print(f"  WARNING: wanted {args.width}x{args.height} -- card gave {got_w}x{got_h}")
    print(f"  saving       : {args.fps:g} fps as {ext[1:].upper()}")
    print(f"  output       : {session}")
    print(f"  max frames   : {args.max}")
    print("  Ctrl+C to stop early.\n")

    saved = 0
    read_fail = 0
    next_save = time.monotonic()
    t0 = time.monotonic()

    try:
        while saved < args.max:
            ok, frame = cap.read()
            if not ok or frame is None:
                read_fail += 1
                # A few dropped reads are normal on HDMI mode changes; a long run
                # means the source is gone (console asleep / cable pulled).
                if read_fail > 300:
                    print("\n  lost the capture source (300 consecutive bad reads) - stopping.")
                    break
                time.sleep(0.01)
                continue
            read_fail = 0

            now = time.monotonic()
            if now < next_save:
                continue
            next_save = now + interval

            path = os.path.join(session, f"f{saved:06d}{ext}")
            try:
                cv2.imwrite(path, frame, write_params)
            except Exception as exc:  # keep recording even if one write fails
                print(f"  write failed on {path}: {exc}")
                continue
            saved += 1

            if saved % 50 == 0:
                mins = (time.monotonic() - t0) / 60.0
                print(f"  saved {saved:5d}  ({mins:.1f} min)", flush=True)
    except KeyboardInterrupt:
        print("\n  stopped by user.")
    finally:
        cap.release()

    mins = (time.monotonic() - t0) / 60.0
    try:
        total_mb = sum(
            os.path.getsize(os.path.join(session, f)) for f in os.listdir(session)
        ) / (1024 * 1024)
    except OSError:
        total_mb = float("nan")
    print(f"\n  DONE: {saved} frames in {mins:.1f} min -> {total_mb:.0f} MB")
    print(f"  {session}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
