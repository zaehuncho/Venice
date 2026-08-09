"""Per-shot detection timeline + machine verdict (Plan Round 33, Phase C).

Joins detframes.csv (wall_ms epoch) with orion_native.log release lines and
classifies WHY each shot's engine feed starved, with one machine verdict per
shot window:

  NOISE_LATCH            detections dominated by out-of-band masked-medians
                         (winner blob is not the meter; e.g. red antialias
                         slivers under a wrong meter_color, or close-welded
                         speckles) — fills fed to the engine were garbage.
  STARVED_BBOX_UNSTABLE  feed starved by the stability validator (bbox_unstable
                         rejections with acquisition-jump resets).
  STARVED_ROI_NOT_FOUND  feed starved by the colour scan (roi_not_found): split
                         into no_candidates (mask empty) vs gates_rejected
                         (candidates existed but every gate pass failed).
  FED_OK_NATIVE_DROP     sidecar fed fine but the native census accepted little
                         (fresh accepts << fed) — native freshness gates.
  FED_OK                 nothing wrong detection-side in this window.

Usage:
  python tools/diagnostics/shot_detection_timeline.py [--log <path>] [--csv <path>]
         [--since 2026-06-12T19:00] [--lookback-ms 2500] [--shot <seq>]
"""
import argparse
import csv
import os
import re
from datetime import datetime, timezone
from pathlib import Path

REPO = str(Path(__file__).resolve().parents[2])

# Purple mask hue band (style corner-sampled bounds, see _style_bgr_bounds_to_hsv):
# a genuine meter blob's masked-median hue must land inside it.
MASK_HUE_LO, MASK_HUE_HI = 132.0, 167.0

LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3})Z\s+(.*)$")
RE_ISSUED = re.compile(
    r"Release issued: fill ([\d.]+)% .*code=(\S+) .*seq=(\d+) shot=(.+)$")
RE_DETSUM = re.compile(
    r"Release detsummary: seq=(\d+) samples=(\d+) fresh=(\d+) staleMem=(\d+) "
    r"confLow=(\d+) staleFrame=(\d+) nodet=(\d+) firstFreshMs=(-?\d+) "
    r"firstMeterMs=(-?\d+) minFreshFill=(-?[\d.]+) shot=(.+)$")
RE_OUTCOME = re.compile(r"Shot outcome: seq=(\d+) verdict=(\S+) .*shot=(.+)$")


def parse_log(path, since_ms):
    shots = {}  # (ts_ms rounded, seq) keyed by seq occurrence order
    order = []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = LOG_TS.match(line)
        if not m:
            continue
        ts = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f").replace(
            tzinfo=timezone.utc).timestamp() * 1000.0
        if ts < since_ms:
            continue
        body = m.group(2)
        mi = RE_ISSUED.search(body)
        if mi:
            key = (ts, int(mi.group(3)))
            shots[key] = {"ts": ts, "seq": int(mi.group(3)), "fill": float(mi.group(1)),
                          "code": mi.group(2), "shot": mi.group(4).strip()}
            order.append(key)
            continue
        md = RE_DETSUM.search(body)
        if md:
            seq = int(md.group(1))
            for key in reversed(order):
                if key[1] == seq and "samples" not in shots[key]:
                    shots[key].update(samples=int(md.group(2)), fresh=int(md.group(3)),
                                      staleMem=int(md.group(4)), confLow=int(md.group(5)),
                                      staleFrame=int(md.group(6)), nodet=int(md.group(7)),
                                      firstFreshMs=int(md.group(8)), firstMeterMs=int(md.group(9)),
                                      minFreshFill=float(md.group(10)))
                    break
            continue
        mo = RE_OUTCOME.search(body)
        if mo:
            seq = int(mo.group(1))
            for key in reversed(order):
                if key[1] == seq and "verdict" not in shots[key]:
                    shots[key]["verdict"] = mo.group(2)
                    break
    return [shots[k] for k in order]


def rle(seq):
    out = []
    for v in seq:
        if out and out[-1][0] == v:
            out[-1][1] += 1
        else:
            out.append([v, 1])
    return " ".join(f"{v}x{n}" for v, n in out)


def classify(shot, rows):
    det = [r for r in rows if r["detected"] == "1"]
    fed = [r for r in rows if r.get("fed") == "1"]
    n = len(rows)
    if not n:
        return "NO_FRAMES", {}
    rej = [r["rejection_reason"] or ("ok" if r["detected"] == "1" else "?") for r in rows]
    counts = {}
    for v in rej:
        counts[v] = counts.get(v, 0) + 1

    def share(name):
        return counts.get(name, 0) / n

    out_band = [r for r in det if r.get("med_h") not in (None, "", "-1.0")
                and not (MASK_HUE_LO <= float(r["med_h"]) <= MASK_HUE_HI)]
    stats = {
        "frames": n, "detected": len(det), "fed": len(fed),
        "out_of_band_winners": len(out_band),
        "rle": rle(rej),
    }
    # Out-of-band winners dominating detections = the winner is not the meter.
    if det and len(out_band) >= max(2, len(det) // 2):
        return "NOISE_LATCH", stats
    if share("bbox_unstable") >= 0.30:
        return "STARVED_BBOX_UNSTABLE", stats
    if share("roi_not_found") >= 0.30:
        cand = [int(r.get("cand_n") or 0) for r in rows if r["rejection_reason"] == "roi_not_found"]
        sub = "gates_rejected" if cand and (sum(1 for c in cand if c > 0) / len(cand)) > 0.5 else "no_candidates"
        stats["roi_sub"] = sub
        return "STARVED_ROI_NOT_FOUND", stats
    if fed and shot.get("samples") and shot.get("fresh", 0) <= max(2, len(fed) // 5):
        return "FED_OK_NATIVE_DROP", stats
    return "FED_OK", stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default=os.path.join(REPO, "logs", "orion_native.log"))
    ap.add_argument("--csv", default=os.path.join(REPO, "logs", "diagnostics", "detframes.csv"))
    ap.add_argument("--since", default=None, help="UTC ISO floor, e.g. 2026-06-12T19:00")
    ap.add_argument("--lookback-ms", type=float, default=2500.0)
    ap.add_argument("--shot", type=int, default=None, help="print full frame timeline for this seq")
    args = ap.parse_args()

    since_ms = 0.0
    if args.since:
        since_ms = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc).timestamp() * 1000

    shots = parse_log(args.log, since_ms)
    if not shots:
        print("no Release issued lines after --since")
        return 1
    frames = [r for r in csv.DictReader(open(args.csv))
              if float(r["wall_ms"]) >= since_ms - args.lookback_ms]

    print(f"shots: {len(shots)}   frames: {len(frames)}")
    for s in shots:
        lo, hi = s["ts"] - args.lookback_ms, s["ts"] + 300.0
        rows = [r for r in frames if lo <= float(r["wall_ms"]) <= hi]
        verdict, st = classify(s, rows)
        when = datetime.fromtimestamp(s["ts"] / 1000, tz=timezone.utc).strftime("%H:%M:%S")
        print(f"\n[{when}Z seq={s['seq']:>3} {s['shot']:<11}] code={s['code']} "
              f"banner={s.get('verdict', '-')} -> {verdict}")
        print(f"  native: samples={s.get('samples', '-')} fresh={s.get('fresh', '-')} "
              f"staleMem={s.get('staleMem', '-')} firstFreshMs={s.get('firstFreshMs', '-')} "
              f"minFreshFill={s.get('minFreshFill', '-')}")
        print(f"  frames={st.get('frames')} detected={st.get('detected')} fed={st.get('fed')} "
              f"outOfBandWinners={st.get('out_of_band_winners')}"
              + (f" roiSub={st['roi_sub']}" if "roi_sub" in st else ""))
        print(f"  rle: {st.get('rle', '')[:240]}")
        if args.shot == s["seq"]:
            for r in rows:
                t = (float(r["wall_ms"]) - s["ts"]) / 1000.0
                print(f"   {t:+.2f}s det={r['detected']} fill={r['fill_pct']:>6} "
                      f"rej={r['rejection_reason']:<16} fed={r.get('fed')} "
                      f"jump={r.get('stab_jump_px'):>6} gate={r.get('acq_gate_px'):>6} "
                      f"ev={r.get('stab_event'):<14} cand={r.get('cand_n'):>3} "
                      f"med_h={r.get('med_h'):>6}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
