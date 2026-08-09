"""Batch-scan all .mp4 files in a directory for meter-dense windows.

Usage:
    python batch_scan_meters.py <video_dir> [--min_hits 4] [--max_videos 80]
"""
from __future__ import annotations

import argparse, os, sys, subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video_dir")
    ap.add_argument("--min_hits", type=int, default=4)
    ap.add_argument("--max_videos", type=int, default=80)
    args = ap.parse_args()

    finder = os.path.join(ROOT, "tools", "diagnostics", "find_shot_windows.py")
    py = sys.executable

    videos = sorted(
        f for f in os.listdir(args.video_dir)
        if f.lower().endswith(".mp4")
    )
    videos = videos[:args.max_videos]

    results = []
    for v in videos:
        path = os.path.join(args.video_dir, v)
        try:
            out = subprocess.check_output(
                [py, finder, path], stderr=subprocess.STDOUT, text=True, timeout=120
            )
            # Parse: "NAME: N frames | meter-sample hits H (~P% present) | range A..B"
            line = out.strip().split("\n")[-1] if out.strip() else ""
            if "meter-sample hits" in line:
                parts = line.split("|")
                hits_str = parts[1].strip() if len(parts) > 1 else ""
                hits_num = int(hits_str.split()[2]) if "hits" in hits_str else 0
                range_str = parts[2].strip() if len(parts) > 2 else ""
                if hits_num >= args.min_hits:
                    results.append((hits_num, v, range_str))
                    print(f"  [HIT] {v}: {hits_num} meter hits | {range_str}")
                else:
                    print(f"  [skip] {v}: {hits_num} meter hits")
            else:
                print(f"  [err]  {v}: {line[:80]}")
        except Exception as e:
            print(f"  [err]  {v}: {e}")

    print(f"\n=== Videos with >={args.min_hits} meter hits ===")
    for hits, name, rng in sorted(results, key=lambda x: -x[0]):
        print(f"  {name}  hits={hits}  {rng}")

if __name__ == "__main__":
    main()
