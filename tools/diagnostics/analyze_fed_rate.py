"""Measure how well a recording's detections would FEED the timing engine.

The right question is NOT "are 100% of detected frames fed" — meter_memory frames
carry no new fill, and the 2-3 acquisition frames at each shot start are intentionally
gated (anti-false-positive). What matters for release timing is: within each REAL shot
segment, does the engine get a DENSE-ENOUGH fed signal (short blind gaps) across the
rise to time the release?

Feeds the engine when rejection_reason in {'', 'green_not_found'} (matches
RemotePlayOrchestrator._should_feed_engine). Run:
    python tools/diagnostics/analyze_fed_rate.py [csv_path]
"""
import csv
import sys

FED = {"", "green_not_found"}
GAP_FRAMES = 8   # >=N consecutive non-detected frames ends a shot (matches analyzer)
MIN_SHOT = 5     # ignore 1-2 frame detection blips


def main(path: str) -> None:
    rows = list(csv.DictReader(open(path)))
    shots, cur, miss = [], [], 0
    for r in rows:
        if r["detected"] == "1":
            if miss >= GAP_FRAMES and cur:
                shots.append(cur)
                cur = []
            miss = 0
            cur.append(r)
        else:
            miss += 1
    if cur:
        shots.append(cur)
    real = [s for s in shots if len(s) >= MIN_SHOT]

    print(f"file: {path}")
    print(f"real shots (>= {MIN_SHOT} detected frames): {len(real)}\n")
    print(f"{'shot':>4} {'frames':>6} {'fed%':>5} {'maxgap':>6} {'mem':>4} {'unstab':>6}  fill_range")
    tot_fed = tot = 0
    well_timed = 0
    for i, s in enumerate(real, 1):
        fed_flags = [r["rejection_reason"] in FED for r in s]
        fed = sum(fed_flags)
        # longest run of consecutive UNFED frames inside the shot = worst blind gap
        maxgap = cur_gap = 0
        for f in fed_flags:
            cur_gap = 0 if f else cur_gap + 1
            maxgap = max(maxgap, cur_gap)
        mem = sum(1 for r in s if r["rejection_reason"] == "meter_memory")
        uns = sum(1 for r in s if r["rejection_reason"] == "bbox_unstable")
        fills = [float(r["fill_pct"]) for r in s]
        tot_fed += fed
        tot += len(s)
        # "well timed-able": >=60% fed AND no blind gap longer than 3 frames (~50ms@60fps,
        # which the engine's velocity extrapolation bridges comfortably).
        ok = (fed / len(s) >= 0.60) and (maxgap <= 3)
        well_timed += ok
        flag = "" if ok else "  <-- sparse/gappy"
        print(f"{i:>4} {len(s):>6} {100*fed/len(s):>4.0f}% {maxgap:>6} {mem:>4} {uns:>6}  "
              f"{min(fills):.0f}->{max(fills):.0f}%{flag}")

    print(f"\nwithin-shot fed-rate: {100*tot_fed/max(1,tot):.0f}%  ({tot_fed}/{tot})")
    print(f"shots with a dense, low-gap feed (timing-able): {well_timed}/{len(real)} "
          f"= {100*well_timed/max(1,len(real)):.0f}%")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "logs/diagnostics/orion_recording_FIXED.csv")
