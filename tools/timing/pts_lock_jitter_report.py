"""Offline proof that the capture-card cadence-lock reduces timestamp jitter.

The capture card carries pts=0, so a frame's timestamp is the wall clock at the instant
cv2.read() RETURNS. The source runs a fixed 60fps cadence, so the TRUE frame times lie on a
regular 16.667ms grid; the read-return instant adds zero-mean scheduling/USB/decode noise. That
noise becomes frame_age_ms variance => release-timing jitter. CadenceLock (capture_card_backend.py)
snaps each stamp back to the grid. This harness measures inter-frame dt jitter (std of dt) BEFORE
vs AFTER the lock — on a synthetic jittery 60fps stream, or on a real session's logged per-frame
timestamps.

Run:  C:\\Python314\\python.exe tools\\timing\\pts_lock_jitter_report.py
      C:\\Python314\\python.exe tools\\timing\\pts_lock_jitter_report.py --fps 60 --jitter-ms 3 --drops 2
      C:\\Python314\\python.exe tools\\timing\\pts_lock_jitter_report.py --stamps-file session_stamps.txt

Trap: tools/ is NOT on sys.path — add the repo root explicitly so `capture_card_backend` imports.
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys

# Repo root on sys.path (tools/timing is two levels down); do NOT rely on tools/diagnostics.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from capture_card_backend import CadenceLock  # noqa: E402


def _synth_stamps(fps=60.0, n=1200, jitter_ms=3.0, drops=0, seed=1234):
    """Generate raw read-return timestamps (ns): a perfect fps grid + gaussian read jitter,
    with `drops` frames randomly skipped (a period missing) to exercise the re-lock guard."""
    import random
    rng = random.Random(seed)
    dt = 1e9 / fps
    drop_idx = set(rng.sample(range(2, n - 2), min(drops, max(0, n - 4)))) if drops else set()
    t_true = 0.0
    stamps, truths = [], []
    for i in range(n):
        t_true += dt
        if i in drop_idx:                     # skipped frame: the grid advances a period with no read
            t_true += dt
        noise = rng.gauss(0.0, jitter_ms * 1e6)
        stamps.append(int(t_true + noise))
        truths.append(int(t_true))
    return stamps, truths


def _read_stamps_file(path):
    """One integer-ns (or float-ms) timestamp per line. Auto-detects ms vs ns by magnitude."""
    vals = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                vals.append(float(line.split(",")[0]))
            except ValueError:
                continue
    if not vals:
        raise SystemExit(f"no timestamps parsed from {path}")
    # Heuristic: perf_counter_ns values are ~1e15+; ms-scale values are far smaller.
    if max(vals) < 1e12:                       # looks like ms
        vals = [v * 1e6 for v in vals]         # -> ns
    return [int(v) for v in vals]


def _dt_std_ms(stamps):
    if len(stamps) < 3:
        return 0.0
    dts = [(stamps[i] - stamps[i - 1]) / 1e6 for i in range(1, len(stamps))]
    return statistics.pstdev(dts), statistics.mean(dts)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--jitter-ms", type=float, default=3.0, help="synthetic read-return jitter std")
    ap.add_argument("--drops", type=int, default=2, help="synthetic dropped frames")
    ap.add_argument("--n", type=int, default=1200)
    ap.add_argument("--stamps-file", type=str, default=None,
                    help="real per-frame timestamps (ns or ms), one per line")
    args = ap.parse_args()

    if args.stamps_file:
        raw = _read_stamps_file(args.stamps_file)
        truths = None
        src = f"file:{args.stamps_file}"
    else:
        raw, truths = _synth_stamps(args.fps, args.n, args.jitter_ms, args.drops)
        src = f"synthetic {args.fps:.2f}fps jitter={args.jitter_ms}ms drops={args.drops} n={args.n}"

    lock = CadenceLock(args.fps)
    locked = [lock.update(t) for t in raw]

    raw_std, raw_mean = _dt_std_ms(raw)
    lock_std, lock_mean = _dt_std_ms(locked)

    print(f"source            : {src}")
    print(f"nominal frame dt  : {1000.0/args.fps:.3f} ms")
    print(f"BEFORE (raw read) : dt mean={raw_mean:.3f} ms  dt std={raw_std:.4f} ms")
    print(f"AFTER  (cadence)  : dt mean={lock_mean:.3f} ms  dt std={lock_std:.4f} ms")
    if raw_std > 0:
        print(f"jitter reduction  : {raw_std/max(lock_std,1e-9):.1f}x  "
              f"({raw_std:.3f} -> {lock_std:.4f} ms std)  re-locks={lock.relocks}")
    if truths is not None:
        # Absolute error vs the true grid: does the lock stay ON the true frame times?
        raw_err = statistics.pstdev([(raw[i] - truths[i]) / 1e6 for i in range(len(raw))])
        lock_err = statistics.pstdev([(locked[i] - truths[i]) / 1e6 for i in range(len(locked))])
        print(f"abs err vs truth  : raw std={raw_err:.4f} ms  locked std={lock_err:.4f} ms")


if __name__ == "__main__":
    main()
