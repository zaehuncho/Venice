#!/usr/bin/env python3
r"""FROZEN-METER LATENCY ORACLE: detframes.csv  join  orion_native.log  ->  per-shot release-path latency.

After each release the shot meter FREEZES at F_stop. Because the on-screen meter our detector reads is
delayed by the full capture->detect->release->animation-stop->encode->decode loop, the OBSERVED fill keeps
rising after the release COMMAND and only reaches F_stop L ms later. This tool measures L per shot:

  1. Parse the release COMMANDs from orion_native.log ("Release issued: fill F% target T% ..." lines carry
     an ISO wall-clock timestamp == the same wall clock as detframes.csv `wall_ms`).
  2. For each release, take the detframes fill(t) window around it, detect the post-release FREEZE plateau
     (fill flattens at F_stop), and invert the observed rise fill(t) to the instant t* where f(t*)==F_stop.
  3. measured_latency = t* - release_wall_ms.

Emits a per-shot table + a robust (median/IQR) summary -- the supervised label the live
latency_estimator's rolling estimate is validated against.

Run:
  C:\Python314\python.exe tools/timing/oracle_join.py \
      --detframes logs/diagnostics/detframes.csv --log logs/orion_native.log
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import os
import re

import numpy as np

_REL_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z\s+Release issued:\s*fill\s*"
    r"(?P<fill>[-\d.]+)%\s*target\s*(?P<target>[-\d.]+)%"
)


def _iso_to_epoch_ms(ts: str) -> float:
    if "." not in ts:
        ts = ts + ".0"
    dt = _dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=_dt.timezone.utc)
    return dt.timestamp() * 1000.0


def parse_releases(log_path: str):
    """-> list of (wall_ms, fill_at_cmd_pct, target_pct)."""
    out = []
    with open(log_path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            m = _REL_RE.match(line)
            if m:
                try:
                    out.append((_iso_to_epoch_ms(m.group("ts")),
                                float(m.group("fill")), float(m.group("target"))))
                except Exception:
                    pass
    return out


def load_detframes(csv_path: str):
    """-> sorted list of (wall_ms, fill_pct, detected) for detected rows."""
    rows = []
    with open(csv_path, encoding="utf-8", errors="ignore") as fh:
        for r in csv.DictReader(fh):
            try:
                if r.get("detected") != "1":
                    continue
                rows.append((float(r["wall_ms"]), float(r["fill_pct"])))
            except Exception:
                continue
    rows.sort(key=lambda z: z[0])
    return rows


def measure_shot(det, t_rel, f_cmd,
                 pre_ms=120.0, post_ms=1200.0,
                 freeze_win=4, freeze_eps=1.5, min_rise_pct=6.0,
                 lo_ms=10.0, hi_ms=200.0):
    """Frozen-meter oracle for one release. Returns dict or None if no clean freeze was found."""
    win = [(w, f) for (w, f) in det if t_rel - pre_ms <= w <= t_rel + post_ms]
    if len(win) < freeze_win + 2:
        return None
    ws = np.array([w for w, _ in win], float)
    fs = np.array([f for _, f in win], float)
    # find the post-release FREEZE: first run of >=freeze_win consecutive samples (after t_rel) whose
    # spread <= freeze_eps and whose level is above the release fill (the meter has risen + stopped).
    after = np.where(ws >= t_rel)[0]
    f_stop = None
    onset_i = None
    for start in after:
        end = start + freeze_win
        if end > len(fs):
            break
        seg = fs[start:end]
        if (seg.max() - seg.min()) <= freeze_eps and float(np.median(seg)) >= f_cmd + min_rise_pct * 0.0:
            # require the plateau to be a genuine post-rise hold, not a pre-release flat
            if float(np.median(seg)) >= max(f_cmd, fs[:max(1, start)].min() + min_rise_pct):
                f_stop = float(np.median(seg))
                onset_i = int(start)
                break
    if f_stop is None or onset_i is None:
        return None
    t_freeze = float(ws[onset_i])
    # invert the observed rise to t* where f == F_stop: linear fit t = a*f + b over the rising leg
    # below F_stop (censor the plateau). Fall back to the freeze onset if the leg is too short.
    rise_mask = (ws <= t_freeze) & (fs < f_stop - 0.05) & (ws >= t_rel - pre_ms)
    tr, fr = ws[rise_mask], fs[rise_mask]
    t_star = t_freeze
    if len(tr) >= 3 and fr[-1] > fr[0] + 0.2:
        tail = min(len(tr), max(4, freeze_win + 1))
        try:
            a, b = np.polyfit(fr[-tail:], tr[-tail:], 1)
            t_star = float(a * f_stop + b)
        except Exception:
            t_star = t_freeze
    lat = t_star - t_rel
    if not (lo_ms <= lat <= hi_ms):
        return None
    return {"t_rel": t_rel, "f_cmd": f_cmd, "f_stop": f_stop,
            "t_freeze": t_freeze, "t_star": t_star, "latency_ms": lat, "n_win": len(win)}


def main() -> int:
    ap = argparse.ArgumentParser(description="frozen-meter latency oracle (detframes JOIN orion_native.log)")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--detframes", default=os.path.join(root, "logs", "diagnostics", "detframes.csv"))
    ap.add_argument("--log", default=os.path.join(root, "logs", "orion_native.log"))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    releases = parse_releases(args.log)
    det = load_detframes(args.detframes)
    if not det:
        print(f"no detected rows in {args.detframes}"); return 1
    d0, d1 = det[0][0], det[-1][0]
    overlap = [r for r in releases if d0 - 200 <= r[0] <= d1 + 200]
    print(f"detframes: {os.path.basename(args.detframes)}  detected-rows={len(det)}  "
          f"wall[{_dt.datetime.fromtimestamp(d0/1000, _dt.timezone.utc)} .. {_dt.datetime.fromtimestamp(d1/1000, _dt.timezone.utc)}]")
    print(f"log releases total={len(releases)}  overlapping this session={len(overlap)}")
    if not overlap:
        print("NO releases overlap this detframes window -- pass a --log/--detframes pair from the SAME run.")
        return 2

    results = []
    for (t_rel, f_cmd, tgt) in overlap:
        m = measure_shot(det, t_rel, f_cmd)
        if m:
            results.append(m)
            if args.verbose:
                print(f"  rel@{_dt.datetime.fromtimestamp(t_rel/1000, _dt.timezone.utc).strftime('%H:%M:%S.%f')[:-3]} "
                      f"f_cmd={f_cmd:5.1f}% F_stop={m['f_stop']:5.1f}% "
                      f"t*-t_rel={m['latency_ms']:6.1f}ms  (win={m['n_win']})")

    print(f"\nmeasured {len(results)}/{len(overlap)} shots (rest had no clean freeze in-window)")
    if len(results) >= 3:
        lat = np.array([r["latency_ms"] for r in results])
        print(f"  RELEASE-PATH LATENCY  median={np.median(lat):.1f}ms  "
              f"IQR[{np.percentile(lat,25):.1f}-{np.percentile(lat,75):.1f}]  "
              f"mean={lat.mean():.1f}  min={lat.min():.1f}  max={lat.max():.1f}  n={len(lat)}")
        sane = 40.0 <= np.median(lat) <= 100.0
        print(f"  -> median in the expected ~40-100ms release-path band: {'YES' if sane else 'NO'}")
    else:
        print("  too few clean shots for a robust summary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
