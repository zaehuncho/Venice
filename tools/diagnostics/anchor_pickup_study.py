#!/usr/bin/env python
"""anchor_pickup_study.py -- does the nameplate anchor make the meter PICK UP earlier?

Read-only offline A/B for [ORION_PLAYER_ANCHOR] / [ORION_ANCHORED_SEARCH] /
[ORION_EXPECTATION_WINDOW]. It replays a framedump straight through
``meter_locator_cv.MeterContourLocator`` -- the same proposer the sidecar runs with
ORION_METER_PROPOSER=cv -- on the dump's OWN wall clock, with the press window published
exactly the way simple_meter_reader publishes it live, and reports per shot:

    first_sight_fill            the fill (% of the 100 px landmark gap) of the FIRST box
                                the locator accepts after the press -- the owner's
                                "late read" complaint, in a number
    first_sight_ms_after_press  how long after the press that took
    no_sight                    shots where nothing was ever accepted (roi_not_found)
    outside_patch_accepts       accepted boxes a CONFIDENT anchor does not contain -- the
                                false-lock proxy; must be 0 with the anchor allowed to refuse
    detect ms / anchor ms       p50 / p90 / p99 / max against the 16.7 ms frame budget

Usage:
  py tools/diagnostics/anchor_pickup_study.py --dump D:/NexusVision/framedump/session_20260912_201355 \
        --presses logs/diagnostics/farshot_study/presses.csv --mode off
  py tools/diagnostics/anchor_pickup_study.py --dump <dir> --mode on --min-h-armed 8
  py tools/diagnostics/anchor_pickup_study.py --dump <dir> --mode both --json out.json

Writes nothing unless --json is given. Never touches the running app, settings or D:.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
import time
from typing import Dict, List, Optional

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import cv2                       # noqa: E402
import numpy as np               # noqa: E402


# --------------------------------------------------------------------------- #
#  corpus
# --------------------------------------------------------------------------- #
def load_frames(dump: str) -> List[dict]:
    """-> [{idx, t, path, live_det, live_fill, live_box}] ordered by wall time."""
    rows: Dict[int, dict] = {}
    csv_path = os.path.join(dump, "frames.csv")
    if os.path.exists(csv_path):
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                i = int(r["idx"])
                rows[i] = dict(idx=i, t=float(r["t_wall"]), live_det=int(r["detected"]),
                               live_fill=float(r["fill_pct"]),
                               live_box=(int(r["bbox_x"]), int(r["bbox_y"]),
                                         int(r["bbox_w"]), int(r["bbox_h"])))
    out = []
    for name in os.listdir(dump):
        # the 09-12 continuous dump wrote f<idx>_<det>_raw.png; the 09-17/09-18
        # press-window dumps write ep<epoch>_f<idx>_<det>_raw.jpg at 60 fps
        m = (re.match(r"f(\d+)_(\d)_raw\.png$", name)
             or re.match(r"ep\d+_f(\d+)_(\d)_raw\.jpg$", name))
        if not m:
            continue
        i = int(m.group(1))
        r = rows.get(i)
        if r is None:
            r = dict(idx=i, t=float(i) / 6.7, live_det=int(m.group(2)),
                     live_fill=-1.0, live_box=(0, 0, 0, 0))
        r = dict(r)
        r["path"] = os.path.join(dump, name)
        out.append(r)
    out.sort(key=lambda r: (r["t"], r["idx"]))
    return out


def load_presses(path: Optional[str], frames: List[dict]) -> List[dict]:
    """Real presses when a press table exists, else an EPISODE PROXY.

    The two online dumps have no press table (no detframes CSV covers them), so a shot is
    taken to start 300 ms before the first frame of each run of live-detected frames that
    is separated from the previous run by >= 0.7 s. That proxy is good enough for the
    metrics this tool reports -- they are all measured relative to the press, and the proxy
    biases OFF and ON identically -- but it is NOT a press and is labelled as such.
    """
    if path and path.endswith(".jsonl") and os.path.exists(path):
        # [2026-09-19] The 09-17/09-18 press dumps have no presses.csv; the session's own
        # shot-record JSONL carries press_ts_ms on the same wall clock as frames.csv.
        out = []
        with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    out.append(dict(epoch=int(rec["epoch"]),
                                    t=float(rec["press_ts_ms"]) / 1000.0,
                                    shot_type=(rec.get("shot_type") or "").strip(),
                                    kind="RECORD", proxy=False))
                except (KeyError, TypeError, ValueError):
                    continue
        out.sort(key=lambda r: r["t"])
        return out
    if path and os.path.exists(path):
        out = []
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                try:
                    out.append(dict(epoch=int(r["epoch"]), t=float(r["press_wall"]),
                                    shot_type=(r.get("shot_type") or "").strip(),
                                    kind=(r.get("kind") or "").strip(), proxy=False))
                except (KeyError, TypeError, ValueError):
                    continue
        out.sort(key=lambda r: r["t"])
        return out
    out = []
    prev_t = -1.0e9
    ep = 0
    for fr in frames:
        if not fr["live_det"]:
            continue
        if fr["t"] - prev_t >= 0.7:
            ep += 1
            out.append(dict(epoch=ep, t=fr["t"] - 0.30, shot_type="", kind="PROXY",
                            proxy=True))
        prev_t = fr["t"]
    return out


# --------------------------------------------------------------------------- #
#  stats
# --------------------------------------------------------------------------- #
def q(v, p):
    v = sorted(v)
    if not v:
        return float("nan")
    k = (len(v) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


# --------------------------------------------------------------------------- #
#  one replay
# --------------------------------------------------------------------------- #
def run(dump: str, presses_csv: Optional[str], mode: str, args) -> dict:
    """mode 'off' = today's locator (the anchor still runs in SHADOW so the two arms are
    scored against the same patches); mode 'on' = the anchor drives the search."""
    on = (mode == "on")
    os.environ["ORION_PLAYER_ANCHOR"] = "1" if on else "0"
    os.environ["ORION_ANCHORED_SEARCH"] = "1" if on else "0"
    os.environ["ORION_EXPECTATION_WINDOW"] = "1" if (on and args.expect) else "0"
    os.environ["ORION_CV_SHAPE_MIN_H_ARMED"] = str(args.min_h_armed if on else 14.0)
    os.environ["ORION_CV_COL_W_MIN_ARMED"] = str(args.w_min_armed if on else 8.0)
    os.environ["ORION_EXPECT_PAIR_MS"] = str(args.pair_ms)
    os.environ["ORION_METER_TOP_STRIP"] = "1" if args.top_strip else "0"

    for m in ("meter_locator_cv", "player_anchor"):
        sys.modules.pop(m, None)
    import player_anchor as pa           # noqa: E402  (re-imported per arm on purpose)
    import meter_locator_cv as mlc       # noqa: E402

    loc = mlc.MeterContourLocator()
    shadow = pa.PlayerAnchor()           # scores the OFF arm against the same geometry

    frames = load_frames(dump)
    if args.limit:
        frames = frames[:args.limit]
    presses = load_presses(presses_csv, frames)
    t0 = frames[0]["t"] if frames else 0.0
    arm_s = args.arm_s

    per_shot: Dict[int, dict] = {}
    det_ms: List[float] = []
    anc_ms: List[float] = []
    outside = 0
    accepts = 0
    pi = 0
    cur = None
    warm = False
    # PER-FRAME low-fill recall. Every framedump in the corpus runs at ~7 fps while the
    # meter fills in ~500 ms, so a per-SHOT first sight cannot resolve the 14->8 px floor
    # (one frame gap = 150 ms = ~36 pp of fill). What the corpora CAN answer is: given a
    # frame on which a meter demonstrably exists at fill F, does this arm accept it? The
    # live framedump's own (detected, fill_pct, bbox) is the oracle, restricted to boxes
    # with the meter's measured geometry so the production false locks of that night are
    # not counted as meters.
    BUCKETS = ((0, 10), (10, 15), (15, 20), (20, 25), (25, 40), (40, 101))
    b_tot = [0] * len(BUCKETS)
    b_acc = [0] * len(BUCKETS)
    patch_seen = patch_hit = 0
    for fr in frames:
        t = fr["t"] - t0
        while pi < len(presses) and (presses[pi]["t"] - t0) <= t:
            cur = presses[pi]
            pa.ARM.note_press(cur["epoch"], t, cur["shot_type"])
            pa.ANCHOR.reset(keep_identity=True)
            shadow.reset(keep_identity=True)
            per_shot.setdefault(cur["epoch"],
                                dict(epoch=cur["epoch"], shot_type=cur["shot_type"],
                                     kind=cur["kind"], press_t=presses[pi]["t"] - t0,
                                     fill=-1.0, ms=-1.0, anchor=0, conf=0.0,
                                     n_acc=0, outside=0))
            pi += 1
        if cur is not None and (t - (cur["t"] - t0)) > arm_s:
            pa.ARM.note_release(t)
            cur = None
        img = cv2.imread(fr["path"])
        if img is None:
            continue
        if not warm:                      # one untimed call: OpenCV kernel/thread warm-up
            loc.detect_box(img, ts=t - 1.0e-3)
            loc.reset()
            warm = True
        armed = cur is not None
        anchor = None
        if not on and armed:
            anchor = shadow.update(img, ts=t, armed=True)
            anc_ms.append(float(shadow.last_ms))
        ta = time.perf_counter()
        box = loc.detect_box(img, ts=t)
        det_ms.append((time.perf_counter() - ta) * 1000.0)
        if on and armed:
            anchor = loc._anchor
            anc_ms.append(float(loc.anchor_ms))
        # ---- per-frame low-fill recall + patch containment ------------------
        lb = fr["live_box"]
        oracle = (fr["live_det"] and 24 <= lb[2] <= 30 and 96 <= lb[3] <= 132
                  and fr["live_fill"] > 0.0)
        if oracle:
            bi = next((k for k, (a2, b2) in enumerate(BUCKETS)
                       if a2 <= fr["live_fill"] < b2), None)
            if bi is not None:
                b_tot[bi] += 1
                if box is not None and abs((box[0] + box[2] * 0.5) - (lb[0] + lb[2] * 0.5)) <= 20 \
                        and abs((box[1] + box[3]) - (lb[1] + lb[3])) <= 24:
                    b_acc[bi] += 1
            if anchor is not None:
                patch_seen += 1
                patch_hit += int(anchor.contains_box(*lb))
        if box is None:
            continue
        accepts += 1
        if not on and anchor is not None and len(box) > 4:
            # Teach the SHADOW anchor exactly what the live one is taught, so the OFF arm's
            # "outside_patch_accepts" is measured against an anchor that can actually reach
            # the confidence threshold. Without this the OFF column reads 0 because no
            # identity was ever learned -- not because nothing was accepted outside.
            shadow.note_meter(img, box, anchor, float(box[4]) >= 0.90)
        if anchor is not None and pa.PlayerAnchor.refuse_ok(anchor):
            if not anchor.contains_box(box[0], box[1], box[2], box[3]):
                outside += 1
                if cur is not None:
                    per_shot[cur["epoch"]]["outside"] += 1
        if cur is None:
            continue
        sh = per_shot[cur["epoch"]]
        sh["n_acc"] += 1
        if sh["fill"] < 0.0:
            col = getattr(loc, "_find_col", None)
            gap = loc.tip_gap * (img.shape[0] / 720.0)
            sh["fill"] = round(100.0 * col[0] / max(1.0, gap), 1) if col else -1.0
            sh["ms"] = round((t - (cur["t"] - t0)) * 1000.0, 1)
            sh["anchor"] = int(anchor is not None)
            sh["conf"] = round(float(anchor.conf), 3) if anchor is not None else 0.0

    shots = list(per_shot.values())
    seen = [s for s in shots if s["fill"] >= 0.0]
    fills = [s["fill"] for s in seen]
    lat = [s["ms"] for s in seen]
    return dict(
        mode=mode, dump=os.path.basename(dump.rstrip("\\/")), frames=len(frames),
        shots=len(shots), proxy=bool(presses and presses[0]["proxy"]),
        seen=len(seen), no_sight=len(shots) - len(seen),
        fill_p50=round(q(fills, .50), 1) if fills else None,
        fill_p90=round(q(fills, .90), 1) if fills else None,
        ms_p50=round(q(lat, .50), 1) if lat else None,
        ms_p90=round(q(lat, .90), 1) if lat else None,
        accepts=accepts, outside_patch_accepts=outside,
        det_p50=round(q(det_ms, .50), 2), det_p99=round(q(det_ms, .99), 2),
        det_max=round(max(det_ms), 2) if det_ms else None,
        anc_p50=round(q(anc_ms, .50), 3) if anc_ms else None,
        anc_p99=round(q(anc_ms, .99), 3) if anc_ms else None,
        anc_max=round(max(anc_ms), 3) if anc_ms else None,
        loc_stats={k: v for k, v in loc.stats.items() if v},
        anchor_stats={k: v for k, v in (pa.ANCHOR.stats if on else shadow.stats).items() if v},
        buckets=[dict(lo=a2, hi=b2, n=b_tot[k], acc=b_acc[k],
                      rate=(round(b_acc[k] / b_tot[k], 3) if b_tot[k] else None))
                 for k, (a2, b2) in enumerate(BUCKETS)],
        patch_seen=patch_seen, patch_hit=patch_hit,
        patch_rate=round(patch_hit / patch_seen, 3) if patch_seen else None,
        shots_detail=shots,
    )


HEAD = ("mode  frames shots seen nosight fill_p50 fill_p90  ms_p50  ms_p90 "
        "outside det_p50 det_p99 det_max anc_p50 anc_p99 anc_max")


def line(r):
    def f(v, w, d=1):
        return ("%*.*f" % (w, d, v)) if isinstance(v, (int, float)) and v is not None else "%*s" % (w, "-")
    return ("%-5s %6d %5d %4d %7d %8s %8s %7s %7s %7d %7s %7s %7s %7s %7s %7s" % (
        r["mode"], r["frames"], r["shots"], r["seen"], r["no_sight"],
        f(r["fill_p50"], 8), f(r["fill_p90"], 8), f(r["ms_p50"], 7), f(r["ms_p90"], 7),
        r["outside_patch_accepts"], f(r["det_p50"], 7, 2), f(r["det_p99"], 7, 2),
        f(r["det_max"], 7, 2), f(r["anc_p50"], 7, 2), f(r["anc_p99"], 7, 2),
        f(r["anc_max"], 7, 2)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump", required=True)
    ap.add_argument("--presses", default=None)
    ap.add_argument("--proxy-presses", action="store_true",
                    help="ignore --presses and use the episode proxy (the press tables only "
                         "cover a fraction of the shots in a long dump)")
    ap.add_argument("--mode", default="both", choices=("off", "on", "both"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--min-h-armed", type=float, default=8.0)
    ap.add_argument("--w-min-armed", type=float, default=6.0)
    ap.add_argument("--pair-ms", type=float, default=60.0)
    ap.add_argument("--arm-s", type=float, default=2.0,
                    help="how long a press keeps the reader armed (the live gate is longer;"
                         " the meter's whole life is <1.5 s)")
    ap.add_argument("--expect", type=int, default=1)
    ap.add_argument("--top-strip", type=int, default=0)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    if a.proxy_presses:
        a.presses = None
    modes = ("off", "on") if a.mode == "both" else (a.mode,)
    out = []
    print(HEAD, flush=True)
    for m in modes:
        r = run(a.dump, a.presses, m, a)
        out.append(r)
        print(line(r), flush=True)
    for r in out:
        print("\n[%s] locator %s" % (r["mode"], r["loc_stats"]))
        print("[%s] anchor  %s" % (r["mode"], r["anchor_stats"]))
        print("[%s] patch contains the live meter box: %s/%s = %s"
              % (r["mode"], r["patch_hit"], r["patch_seen"], r["patch_rate"]))
        print("[%s] per-frame recall by live fill:" % r["mode"])
        for b in r["buckets"]:
            print("        fill %3d-%3d  n=%4d  accepted=%4d  %s"
                  % (b["lo"], b["hi"], b["n"], b["acc"],
                     "-" if b["rate"] is None else "%.3f" % b["rate"]))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)
        print("\nwrote", a.json)


if __name__ == "__main__":
    main()
