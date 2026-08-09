#!/usr/bin/env python3
"""Is a press-anchored, meter-free release viable? Measure the spread offline.

THE QUESTION
============
With the in-game shot meter HIDDEN (2K's no-meter shooting boost), the bot has
nothing to read. The only zero-latency signal left is the player's own physical
Square press: it is polled PC-side on a 4ms Qt::PreciseTimer and logged as
"Physical shot epoch:", so unlike anything seen through the capture card it
carries no video latency at all.

An open-loop release then means: fire at  t_press + D.

That works if and only if D is STABLE. This script measures how stable, using
data already on disk. No rig, no live session, no code changes.

WHAT IS MEASURED
================
For each physical shot epoch, the meter trajectory that follows it is pulled out
of detframes*.csv and the TIP (peak fill) is located. The tip is the right
anchor because it is what the shipped system already aims at
(targetMode=meter_tip_phase) and, unlike the green window, it is directly
observable -- greenConfirmed is 1 in 0 of 352 release attributions on this rig,
so green cannot serve as ground truth.

        D = t_tip - t_press          (per shot, bucketed by shot type)

THE BAR
=======
Open-loop has no feedback term, so the whole error budget is D's spread. The
green window is as narrow as 2.8pp and the meter runs at ~0.18 %/ms, so the
window is roughly 15ms wide and a centred aim can afford about +/-8ms.

    sigma <= ~5ms   -> open-loop is comfortably viable
    sigma ~ 5-10ms  -> viable only with per-bucket D and a tight bucket key
    sigma >= ~15ms  -> dead; the spread alone exceeds the green window

This script does not decide anything on its own -- it reports the number that
decides it, per bucket, with the sample counts needed to judge whether the
number means anything.

USAGE
=====
    python tools/timing/no_meter_feasibility.py
    python tools/timing/no_meter_feasibility.py --log logs/orion_native.log
"""
from __future__ import annotations

import argparse
import csv
import datetime
import glob
import os
import re
import statistics
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A shot's meter must appear within this long after the press, and the tip is
# searched inside it. The meter first renders ~300ms after the physical edge
# (measured, 2026-08-03 capture-card session); 3s is generous headroom.
SEARCH_WINDOW_MS = 3000.0
# A trajectory has to actually rise to count as a shot rather than a stray
# detection sitting at a flat value.
MIN_RISE_PCT = 25.0
# ...and needs enough detected frames for the peak to be meaningful.
MIN_FRAMES = 8

EPOCH_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+Physical shot epoch: epoch=(?P<epoch>\d+)')
RELEASE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T[\d:.]+Z)\s+Release attribution: seq=(?P<seq>\d+).*?'
    r'vel=(?P<vel>[-\d.]+).*?shot=(?P<shot>\w+)')


def iso_to_ms(ts: str) -> float:
    return datetime.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=datetime.timezone.utc).timestamp() * 1000.0


def parse_log(path: str):
    """-> (epochs, releases); each a list of dicts with wall_ms."""
    epochs, releases = [], []
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    for line in text.splitlines():
        m = EPOCH_RE.match(line)
        if m:
            epochs.append({"wall_ms": iso_to_ms(m.group("ts")),
                           "epoch": int(m.group("epoch"))})
            continue
        m = RELEASE_RE.match(line)
        if m:
            releases.append({"wall_ms": iso_to_ms(m.group("ts")),
                             "seq": int(m.group("seq")),
                             "shot": m.group("shot"),
                             "vel": float(m.group("vel"))})
    return epochs, releases


def load_detframes(paths):
    """-> sorted [(wall_ms, fill_pct, source)] for DETECTED rows only."""
    rows = []
    for p in paths:
        name = os.path.basename(p)
        with open(p, newline="", encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                try:
                    if str(r.get("detected", "")).strip() not in ("1", "true", "True"):
                        continue
                    wall = float(r["wall_ms"])
                    fill = float(r["fill_pct"])
                except (TypeError, ValueError, KeyError):
                    continue
                if wall <= 0 or fill < 0:
                    continue
                rows.append((wall, fill, name))
    rows.sort(key=lambda x: x[0])
    return rows


def slice_window(det, t0: float, t1: float):
    import bisect
    lo = bisect.bisect_left(det, (t0, -1e9, ""))
    hi = bisect.bisect_right(det, (t1, 1e9, "퟿"))
    return det[lo:hi]


# ── Trajectory isolation ────────────────────────────────────────────────────
# The first version of this script took max(fill) over the whole 3s window. That
# does not measure D, it measures the WINDOW: a 3s span can contain several
# shots, and merging 25 detframes files into one timeline let a window straddle
# two captures. It produced sigma=713ms with range=2..2962 -- a D of 2ms is
# physically impossible (the meter does not render until ~300ms after the press)
# and 2962 is the window edge, i.e. the "distribution" was the window itself.
#
# A shot's meter is instead isolated as a CONTIGUOUS run of detected frames:
#   * no gap longer than MAX_GAP_MS (a gap means the meter left the screen);
#   * beginning inside PLAUSIBLE_ONSET_MS of the press (the meter renders
#     ~300ms after the physical edge -- anything later belongs to another shot);
#   * rising at least MIN_RISE_PCT within that run;
#   * and never crossing a detframes file boundary.
MAX_GAP_MS = 150.0
PLAUSIBLE_ONSET_MS = (100.0, 1200.0)
# Physically plausible press -> tip. Anything outside is an extraction failure,
# not a real shot, and is COUNTED rather than silently dropped: if most shots
# land outside, the extraction is broken and the sigma below is meaningless.
PLAUSIBLE_D_MS = (200.0, 2000.0)


def _first_run(win):
    """First contiguous detected run in the window, as a list of (t, fill)."""
    run = []
    for t, f, _src in win:
        if run and t - run[-1][0] > MAX_GAP_MS:
            break
        run.append((t, f))
    return run


def measure(epochs, releases, det):
    """-> (measurements, rejection counts)."""
    out = []
    rej = defaultdict(int)
    rel_sorted = sorted(releases, key=lambda r: r["wall_ms"])
    rel_times = [r["wall_ms"] for r in rel_sorted]
    import bisect

    for e in epochs:
        t0 = e["wall_ms"]
        win = slice_window(det, t0, t0 + SEARCH_WINDOW_MS)
        if not win:
            rej["no detected frames after the press"] += 1
            continue
        # One capture file only: a window that straddles two captures is a
        # splice, not a shot.
        source = win[0][2]
        win = [w for w in win if w[2] == source]

        run = _first_run(win)
        if len(run) < MIN_FRAMES:
            rej["contiguous run too short"] += 1
            continue
        onset = run[0][0] - t0
        if not (PLAUSIBLE_ONSET_MS[0] <= onset <= PLAUSIBLE_ONSET_MS[1]):
            rej["meter onset implausible vs the press"] += 1
            continue
        fills = [f for _, f in run]
        if max(fills) - min(fills) < MIN_RISE_PCT:
            rej["run never rises (not a shot)"] += 1
            continue

        peak = max(fills)
        # FIRST frame achieving the peak: a flat tail after the tip is a
        # stop-detector artifact, not a later tip (see the 2026-08-06 animation
        # tail decomposition), and taking the last frame would import that tail
        # straight into the spread.
        t_tip = next(t for t, f in run if f >= peak - 1e-9)
        d_ms = t_tip - t0
        if not (PLAUSIBLE_D_MS[0] <= d_ms <= PLAUSIBLE_D_MS[1]):
            rej["press->tip outside physical bounds"] += 1
            continue

        idx = bisect.bisect_left(rel_times, t0)
        shot = "?"
        vel = float("nan")
        if idx < len(rel_sorted) and rel_sorted[idx]["wall_ms"] - t0 <= SEARCH_WINDOW_MS:
            shot = rel_sorted[idx]["shot"]
            vel = rel_sorted[idx]["vel"]

        out.append({"epoch": e["epoch"], "d_ms": d_ms, "peak": peak,
                    "onset_ms": onset, "shot": shot, "vel": vel,
                    "frames": len(run), "source": source})
    return out, rej


def describe(name: str, values, extra: str = "") -> str:
    if len(values) < 2:
        return f"  {name:<22} n={len(values):<4} (too few to characterise)"
    med = statistics.median(values)
    sd = statistics.pstdev(values)
    lo, hi = min(values), max(values)
    srt = sorted(values)
    p10 = srt[int(0.10 * (len(srt) - 1))]
    p90 = srt[int(0.90 * (len(srt) - 1))]
    return (f"  {name:<22} n={len(values):<4} median={med:7.1f}ms  "
            f"sigma={sd:6.1f}ms  p10-p90={p10:6.1f}..{p90:<7.1f} "
            f"range={lo:.0f}..{hi:.0f}{extra}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=os.path.join(REPO, "logs", "orion_native.log"))
    ap.add_argument("--detframes", default=os.path.join(REPO, "logs", "diagnostics"))
    ap.add_argument("--min-bucket", type=int, default=5)
    args = ap.parse_args()

    print("=" * 78)
    print("  PRESS-ANCHORED (NO-METER) FEASIBILITY")
    print("  D = t_meter_tip - t_physical_square_press")
    print("=" * 78)

    epochs, releases = parse_log(args.log)
    print(f"\n  physical shot epochs : {len(epochs)}")
    print(f"  release attributions : {len(releases)}")

    paths = sorted(glob.glob(os.path.join(args.detframes, "detframes*.csv")))
    if not paths:
        print("  NO detframes found", file=sys.stderr)
        return 1
    det = load_detframes(paths)
    print(f"  detframes files      : {len(paths)}")
    print(f"  detected frames      : {len(det)}")

    shots, rej = measure(epochs, releases, det)
    if rej:
        print("\n  -- Presses rejected (extraction accounting) ----------------------")
        for k, v in sorted(rej.items(), key=lambda kv: -kv[1]):
            print(f"    {v:>4}  {k}")
    print(f"\n  presses with an ISOLATED meter trajectory : {len(shots)}"
          f"  ({100.0 * len(shots) / max(1, len(epochs)):.0f}% of presses)")
    if len(shots) < 2:
        print("\n  Not enough matched shots to say anything. Stop here.")
        return 1

    all_d = [s["d_ms"] for s in shots]
    print("\n  -- Pooled ---------------------------------------------------------")
    print(describe("ALL shots", all_d))

    by_shot = defaultdict(list)
    for s in shots:
        by_shot[s["shot"]].append(s["d_ms"])
    print("\n  -- By shot type (the bucket key an open-loop D would use) ---------")
    for shot, vals in sorted(by_shot.items(), key=lambda kv: -len(kv[1])):
        if len(vals) >= args.min_bucket:
            print(describe(shot, vals))
    small = {k: v for k, v in by_shot.items() if len(v) < args.min_bucket}
    if small:
        print(f"  (buckets below n={args.min_bucket}, not characterised: "
              f"{', '.join(f'{k}={len(v)}' for k, v in small.items())})")

    by_src = defaultdict(list)
    for s in shots:
        by_src[s["source"]].append(s["d_ms"])
    if len(by_src) > 1:
        print("\n  -- By capture file (a split here means sessions are not "
              "interchangeable) --")
        for src, vals in sorted(by_src.items(), key=lambda kv: -len(kv[1]))[:6]:
            if len(vals) >= args.min_bucket:
                print(describe(src[:22], vals))

    # -- Verdict ------------------------------------------------------------
    print("\n  -- Verdict --------------------------------------------------------")
    best = None
    for shot, vals in by_shot.items():
        if len(vals) >= args.min_bucket:
            sd = statistics.pstdev(vals)
            if best is None or sd < best[1]:
                best = (shot, sd, len(vals))
    pooled_sd = statistics.pstdev(all_d)
    print(f"  pooled sigma                  : {pooled_sd:.1f} ms")
    if best:
        print(f"  best single bucket            : {best[0]} "
              f"sigma={best[1]:.1f} ms (n={best[2]})")
    print("  green window is ~15 ms wide (2.8pp at ~0.18 %/ms) => budget ~ +/-8 ms")
    ref = best[1] if best else pooled_sd
    if ref <= 5.0:
        print("  => VIABLE. Spread fits well inside the window.")
    elif ref <= 10.0:
        print("  => MARGINAL. Only with per-bucket D and a tighter bucket key.")
    else:
        print("  => NOT VIABLE as measured. The spread alone exceeds the green")
        print("     window, so an open-loop release would miss on spread even")
        print("     with a perfect D. Tighten the bucket key or drop the idea.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
