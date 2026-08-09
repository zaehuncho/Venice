#!/usr/bin/env python
"""
replay_simple_reader.py -- drive the REAL SimpleMeterReader over a framedump session,
faithfully replicating the orchestrator's per-frame contract (synthetic 60fps capture
clock via detect(ts=...), the CV self-arm shot-gate, set_shot_state each frame), and
report per-frame + aggregate tracking-quality metrics.

Ground truth available offline:
  * filename flag  fNNNNN_D_raw.png  -> D = the LIVE detector's `detected` for that frame
    (from whatever detector ran live; a presence indicator, not a box oracle).

Metrics (the reported live symptoms -> a number):
  * detect latency (ms): mean / p50 / p95 / max  (the "detect-loop stall")
  * within-shot DISAPPEARANCE: a detected->NOT-detected->detected flicker inside a
    contiguous detected run (blink/early-drop)
  * FROZEN-FILL runs: consecutive detected frames whose reported fill is byte-identical
    (a stuck coast / fake hold)
  * BBOX TELEPORT: consecutive detected frames whose box centre jumps > TELEPORT px
    (fake-lock jumping onto decor)
  * velocity-through-rise: is velocity non-zero on the rising frames

Read-only: writes ONLY under the scratch dir passed via --out (default: stdout only).
Does NOT modify the reader or any source/test.
"""
import argparse
import glob
import json
import os
import re
import sys
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Live defaults (today's sidecar env had NO ORION_READER_* overrides): occl ON, robust
# OFF, scale-adapt ON, tip-enforce ON, armed-hold ON, color-cal OFF, green-stack OFF.
# Pin them so the replay does not inherit a stray shell env.
for _k, _v in {
    "ORION_READER_OCCLUSION": "1",
    "ORION_READER_ROBUST": "0",
    "ORION_READER_SCALE_ADAPT": "1",
    "ORION_READER_TIP_ENFORCE": "1",
    "ORION_READER_ARMED_HOLD": "1",
    "ORION_COLOR_CAL": "0",
    "ORION_GREEN_ZONE_WINDOW": "0",
    "ORION_GREEN_SELF_GRADE": "0",
}.items():
    os.environ.setdefault(_k, _v)

import numpy as np   # noqa: E402
import cv2           # noqa: E402

from simple_meter_reader import SimpleMeterReader   # noqa: E402

_FRAME_RE = re.compile(r"f(\d{5})_([01])_raw\.png$")
_SHOT_GATE_ARM_FRAMES = 120   # orch default (ORION_SHOT_GATE_ARM_FRAMES)
_TELEPORT_PX = 120            # box-centre jump that is not a real meter slide @1080p


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "Red"
    confidence_threshold = 0.32


def list_frames(session_dir):
    out = {}
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out[int(m.group(1))] = (p, int(m.group(2)))
    return out


def run(session_dir, fps=60.0, limit=None, dump_jsonl=None):
    frames = list_frames(session_dir)
    idxs = sorted(frames)
    if limit:
        idxs = idxs[:limit]
    if not idxs:
        print(f"NO FRAMES in {session_dir}", file=sys.stderr)
        return None

    reader = SimpleMeterReader(cfg=_Cfg())
    dt = 1.0 / float(fps)
    t = 100000.0
    deadline_seq = -1

    rows = []
    lat_ms = []
    for seq in idxs:
        path, live_flag = frames[seq]
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            continue
        # CV self-arm contract (orch): armed = seq <= deadline; pushed BEFORE detect().
        armed = seq <= deadline_seq
        reader.set_shot_state(armed, 0.0, armed)
        t0 = _time.perf_counter()
        res = reader.detect(img, ts=t)
        lat = (_time.perf_counter() - t0) * 1000.0
        lat_ms.append(lat)
        detected = bool(getattr(res, "detected", False))
        bbox = tuple(int(v) for v in getattr(res, "bbox", (0, 0, 0, 0)))
        fill = float(getattr(res, "fill_pct", 0.0) or 0.0)
        vel = float(getattr(res, "fill_velocity_pct_s", 0.0) or 0.0)
        rise = str(getattr(res, "rise_state", "") or "")
        reject = str(getattr(res, "rejection_reason", "") or "")
        conf = float(getattr(res, "confidence", 0.0) or 0.0)
        # CV self-arm: refresh deadline on a rising/fast detected frame (orch line ~2175).
        if detected and (rise == "rising" or vel > 40.0):
            deadline_seq = seq + _SHOT_GATE_ARM_FRAMES
        rows.append(dict(seq=seq, live=live_flag, det=int(detected), fill=round(fill, 3),
                         vel=round(vel, 2), rise=rise, reject=reject, conf=round(conf, 3),
                         bbox=bbox, lat=round(lat, 2), armed=int(armed)))
        t += dt

    if dump_jsonl:
        with open(dump_jsonl, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    return _metrics(session_dir, rows, lat_ms)


def _runs_detected(rows):
    """contiguous runs where det==1, as (start_i, end_i) inclusive indices into rows."""
    runs = []
    i = 0
    n = len(rows)
    while i < n:
        if rows[i]["det"] == 1:
            j = i
            while j + 1 < n and rows[j + 1]["det"] == 1:
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def _metrics(session_dir, rows, lat_ms):
    n = len(rows)
    det = [r for r in rows if r["det"] == 1]
    lat = np.array(lat_ms, dtype=np.float64)

    # within-shot disappearance: for each contiguous "meter present" region (bounded by
    # >=STALL no-detect frames on both sides), count interior single/short no-detect gaps.
    STALL = 12  # >=~200ms of no-detect = a real shot boundary, not a flicker
    present = np.array([r["det"] for r in rows], dtype=np.int8)
    # find gaps of consecutive 0s; a gap shorter than STALL that is flanked by detects on
    # BOTH sides is a within-shot disappearance (blink/early-drop).
    blinks = 0
    blink_frames = 0
    i = 0
    while i < n:
        if present[i] == 0:
            j = i
            while j + 1 < n and present[j + 1] == 0:
                j += 1
            gap = j - i + 1
            flanked = i > 0 and j < n - 1 and present[i - 1] == 1 and present[j + 1] == 1
            if flanked and gap < STALL:
                blinks += 1
                blink_frames += gap
            i = j + 1
        else:
            i += 1

    # frozen-fill runs: consecutive detected frames with byte-identical fill (>=FREEZE long)
    FREEZE = 6
    frozen_runs = 0
    frozen_max = 0
    for (a, b) in _runs_detected(rows):
        k = a
        while k <= b:
            m = k
            while m + 1 <= b and rows[m + 1]["fill"] == rows[k]["fill"]:
                m += 1
            runlen = m - k + 1
            if runlen >= FREEZE:
                frozen_runs += 1
                frozen_max = max(frozen_max, runlen)
            k = m + 1

    # bbox teleport: detected->detected consecutive centre jump > TELEPORT
    teleports = 0
    for a, b in zip(rows, rows[1:]):
        if a["det"] and b["det"] and a["bbox"][2] and b["bbox"][2]:
            ax = a["bbox"][0] + a["bbox"][2] * 0.5
            ay = a["bbox"][1] + a["bbox"][3] * 0.5
            bx = b["bbox"][0] + b["bbox"][2] * 0.5
            by = b["bbox"][1] + b["bbox"][3] * 0.5
            if ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 > _TELEPORT_PX:
                teleports += 1

    # velocity through the rise: fraction of rising frames with |vel|>1
    rising = [r for r in det if r["rise"] == "rising"]
    rising_moving = [r for r in rising if abs(r["vel"]) > 1.0]

    # agreement with the live filename flag (presence indicator)
    agree = float(np.mean([r["det"] == r["live"] for r in rows])) if rows else 0.0

    name = os.path.basename(session_dir.rstrip("/\\"))
    out = dict(
        session=name, frames=n, detected=len(det),
        lat_mean=round(float(lat.mean()), 2) if n else 0,
        lat_p50=round(float(np.percentile(lat, 50)), 2) if n else 0,
        lat_p95=round(float(np.percentile(lat, 95)), 2) if n else 0,
        lat_max=round(float(lat.max()), 2) if n else 0,
        within_shot_blinks=blinks, blink_frames=blink_frames,
        frozen_fill_runs=frozen_runs, frozen_fill_max=frozen_max,
        bbox_teleports=teleports,
        rising_frames=len(rising), rising_moving=len(rising_moving),
        live_agreement=round(agree, 4),
    )
    print(json.dumps(out, indent=2))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", required=True, help="framedump session dir (or its name under logs/diagnostics/framedump)")
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--jsonl", default=None, help="write per-frame rows here")
    a = ap.parse_args()
    sd = a.session
    if not os.path.isdir(sd):
        sd = os.path.join(REPO, "logs", "diagnostics", "framedump", a.session)
    run(sd, fps=a.fps, limit=a.limit, dump_jsonl=a.jsonl)


if __name__ == "__main__":
    main()
