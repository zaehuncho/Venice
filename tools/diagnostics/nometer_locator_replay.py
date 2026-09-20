"""Replay a press window through the REAL MeterContourLocator and print, per frame,
why it did or did not propose a box.

Runs the shipped proposer (ORION_METER_PROPOSER=cv) through the shipped async wrapper
in sync mode, with the press published on player_anchor.ARM exactly as the reader
publishes it live, so the anchored search, the onset window and the tipless path all
see the same state they saw live. The per-frame reason is the DELTA in the locator's
own stats dict (no_tip / shape_short / no_tip_lone / refused_outside / ...).
READ-ONLY.
"""
from __future__ import annotations
import os, sys

os.environ.setdefault("ORION_METER_PROPOSER", "cv")
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, _HERE)

import cv2  # noqa: E402
from nometer_window_forensics import load_records, load_frames_csv, frame_paths  # noqa: E402

import player_anchor as pa                      # noqa: E402
from meter_detector_yolo import AsyncMeterLocator  # noqa: E402
import meter_locator_cv as mlc                  # noqa: E402

INTERESTING = ("no_tip", "no_tip_lone", "shape_short", "shape_irregular", "tip_spill",
               "shape_abstain", "no_outline", "not_lone", "refused_outside",
               "expect_pending", "tipless_pending", "tipless_track", "col_cands",
               "anchor_hit", "anchor_patch_hit", "top_strip", "error")


def replay(ep, recs, fr, t_lo=None, t_hi=None, verbose=True):
    r = recs[ep]; fd = r["framedump"]; press = r["press_ts_ms"] / 1000.0
    base = mlc.MeterContourLocator()
    loc = AsyncMeterLocator(base=base, sync=True)
    base.reset()
    pa.ARM.note_press(ep, press, r["shot_type"] or "")
    prev = dict(base.stats)
    out = []
    for i, p in frame_paths(ep, fd["first_idx"], fd["last_idx"]):
        row = fr.get(i)
        if row is None:
            continue
        ts = float(row["t_wall"]); t = (ts - press) * 1000.0
        if t_lo is not None and t < t_lo:
            continue
        if t_hi is not None and t > t_hi:
            continue
        img = cv2.imread(p)
        if img is None:
            continue
        loc.submit(img, ts)
        found, box, conf, fts = loc.latest()
        d = {k: base.stats[k] - prev.get(k, 0) for k in base.stats
             if base.stats[k] - prev.get(k, 0)}
        prev = dict(base.stats)
        why = ",".join(f"{k}={v}" for k, v in sorted(d.items()) if k in INTERESTING) or "-"
        out.append((i, t, found, box, conf, why))
        if verbose:
            print(f"  f{i} t={t:+8.1f} live_det={row['detected']} propose={'HIT ' if found else 'miss'} "
                  f"box={box} conf={conf:.2f} reason[{why}]")
    return out, base


if __name__ == "__main__":
    recs = load_records(); fr = load_frames_csv(recs)
    for a in (sys.argv[1:] or ["35"]):
        ep = int(a)
        r = recs[ep]
        print(f"\n### epoch {ep} {r['shot_type']} hold={r.get('release_after_press_ms')} "
              f"live onset={r.get('onset_ms')}")
        out, base = replay(ep, recs, fr)
        hits = [o for o in out if o[2]]
        print(f"  proposed on {len(hits)}/{len(out)} frames; first at "
              f"{hits[0][1]:+.1f} ms" if hits else "  proposed on 0 frames")
        print("  final stats:", {k: v for k, v in base.stats.items() if v})
