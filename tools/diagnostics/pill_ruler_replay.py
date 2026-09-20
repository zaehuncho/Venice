#!/usr/bin/env python
"""pill_ruler_replay.py -- A/B the Pill LANDMARK ruler against the shipped BOX ruler by
replaying a framedump through the REAL SimpleMeterReader.

Two reader instances are driven over the SAME decoded frames in one pass, differing ONLY by
``ORION_PILL_RULER`` (0 = the shipped box-relative ruler, 1 = pill_fill_ruler.py).  Each has
its own proposer, so the arms are independent end-to-end; the report states on how many
frames their boxes agreed, which is what makes the paired per-rise numbers meaningful.

Reported per detected RISE (a run of frames whose fill climbs from < 15 % to > 60 %):
  * fill velocity in the 36-40 % band   (Arrow2 reads 0.181 pp/ms, IQR 0.003)
  * the 20 % crossing time, and its SHIFT between the two rulers
  * the frozen green_end and its spread  (Arrow2 reads 96.0, p10 95.3 / p90 96.7)

Also usable as the Arrow2 NO-CHANGE proof (``--style Arrow2 --proposer cv``): with a
non-Pill style the hook is unreachable, so the two arms must be byte-identical; the tool
prints the count of frames whose (detected, fill, bbox, green) differ.

READ-ONLY: no git state, no live process, no source edits.  Frame caches land under
--cache-dir (default D:/NexusVision/pill_check/_cache).

USAGE
  .venv/Scripts/python.exe tools/diagnostics/pill_ruler_replay.py \
      --session "D:/NexusVision/framedump/session_20260917_050219" \
      --style Pill --proposer yolo --out D:/NexusVision/pill_check
  .venv/Scripts/python.exe tools/diagnostics/pill_ruler_replay.py \
      --session "D:/NexusVision/framedump/session_20260918_162245" \
      --style Arrow2 --proposer cv --out D:/NexusVision/pill_check
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
import re
import sys
import time as _time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# The 2026-09-16 ship line (docs/ship config), pinned so a stray shell env cannot move
# either arm.  Both arms get the identical set; only ORION_PILL_RULER differs.
LIVE_ENV = {
    "ORION_SIMPLE_READER": "1",
    "ORION_METER_DETECTOR": "0",     # the locator is attached by hand below
    "ORION_READER_ANCHOR": "1",
    "ORION_READER_PCTL_FILL": "1",
    "ORION_READER_TRACK_H_CAP": "1",
    "ORION_METER_SUBPIXEL_SESSION_RULER": "1",
    "ORION_METER_SUBPIXEL_SESSION_PROVISIONAL": "1",
    "ORION_METER_SUBPIXEL_BASE_HOLD": "1",
    "ORION_METER_PARTIAL_OCCLUSION": "1",
    "ORION_READER_BOX_LATCH": "1",
    "ORION_READER_STATIC_ZONE_QUARANTINE": "1",
    "ORION_CV_TIPLESS_ARMED": "1",
    "ORION_READER_GHOST_FORGET_LOCATOR": "0",
    "ORION_READER_FRESH_AFTER_GHOST": "0",
    "ORION_READER_IDLE_PUBLISH_GATE": "0",
    "ORION_LOCATOR_IDLE_REUSE": "0",
    "ORION_COLOR_CAL": "0",
    "ORION_GREEN_SELF_GRADE": "0",
    "ORION_DETDIAG": "0",
}

_FRAME_RE = re.compile(r"ep(\d+)_f(\d+)_([01])_raw\.(?:jpg|png)$")
_SHOT_GATE_ARM_FRAMES = 120


class _Cfg(object):
    meter_style = "Pill"
    meter_color = "White"
    confidence_threshold = 0.32
    park_temporal_enabled = False


# ------------------------------------------------------------------------------- frames
def list_frames(session_dir):
    """-> {frame_idx: (path, episode, live_detected)}"""
    out = {}
    for p in glob.glob(os.path.join(session_dir, "*_raw.jpg")) + \
            glob.glob(os.path.join(session_dir, "*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out[int(m.group(2))] = (p, int(m.group(1)), int(m.group(3)))
    return out


def load_rows(session_dir, limit=None):
    frames = list_frames(session_dir)
    rows = []
    csv_path = os.path.join(session_dir, "frames.csv")
    if os.path.exists(csv_path):
        with open(csv_path, newline="") as f:
            for r in csv.DictReader(f):
                i = int(r["idx"])
                if i not in frames:
                    continue
                p, ep, live = frames[i]
                rows.append({"idx": i, "path": p, "ep": ep, "t": float(r["t_wall"]),
                             "live_det": int(r["detected"]),
                             "live_fill": float(r["fill_pct"]),
                             "live_bbox": (int(r["bbox_x"]), int(r["bbox_y"]),
                                           int(r["bbox_w"]), int(r["bbox_h"]))})
    else:
        for i in sorted(frames):
            p, ep, live = frames[i]
            rows.append({"idx": i, "path": p, "ep": ep, "t": i / 60.0,
                         "live_det": live, "live_fill": 0.0,
                         "live_bbox": (0, 0, 0, 0)})
    rows.sort(key=lambda r: r["idx"])
    if limit:
        rows = rows[:limit]
    return rows


class FrameCache(object):
    """uint8 memmap of the decoded dump; a second run costs no libjpeg time."""

    def __init__(self, session_dir, cache_dir, rows, rebuild=False):
        import numpy as np
        import cv2
        self._np = np
        name = os.path.basename(session_dir.rstrip("/\\")) + "_%d" % len(rows)
        self.dir = os.path.join(cache_dir, name)
        os.makedirs(self.dir, exist_ok=True)
        self.meta_path = os.path.join(self.dir, "index.json")
        self.npy_path = os.path.join(self.dir, "frames_u8.npy")
        meta = None
        if not rebuild and os.path.exists(self.meta_path) and os.path.exists(self.npy_path):
            try:
                meta = json.load(open(self.meta_path))
            except Exception:
                meta = None
        if meta and meta.get("idx") == [r["idx"] for r in rows]:
            self.arr = np.lib.format.open_memmap(self.npy_path, mode="r")
            return
        shape = None
        for r in rows:
            im = cv2.imread(r["path"], cv2.IMREAD_COLOR)
            if im is not None:
                shape = im.shape
                break
        if shape is None:
            raise SystemExit("no decodable frames in %s" % session_dir)
        arr = np.lib.format.open_memmap(self.npy_path, mode="w+", dtype=np.uint8,
                                        shape=(len(rows), shape[0], shape[1], shape[2]))
        t0 = _time.perf_counter()
        for i, r in enumerate(rows):
            im = cv2.imread(r["path"], cv2.IMREAD_COLOR)
            arr[i] = im if (im is not None and im.shape == shape) else 0
            if (i + 1) % 500 == 0:
                sys.stderr.write("  decode %d/%d (%.0fs)\n"
                                 % (i + 1, len(rows), _time.perf_counter() - t0))
        arr.flush()
        del arr
        json.dump({"idx": [r["idx"] for r in rows], "shape": list(shape)},
                  open(self.meta_path, "w"))
        self.arr = np.lib.format.open_memmap(self.npy_path, mode="r")

    def get(self, i):
        return self._np.asarray(self.arr[i])


# ------------------------------------------------------------------------------- reader
def build_reader(style, proposer, conf=0.35):
    from meter_detector_yolo import AsyncMeterLocator
    from simple_meter_reader import SimpleMeterReader
    cfg = _Cfg()
    cfg.meter_style = style
    r = SimpleMeterReader(cfg=cfg)
    if proposer == "yolo":
        import meter_detector_yolo as mdy
        base = mdy.MeterYoloLocator(conf_thres=conf)
    else:
        import meter_locator_cv as mlc
        base = mlc.MeterContourLocator()
    r._meter_detector = AsyncMeterLocator(base=base, sync=True)
    try:
        r.set_active_style(style)
    except Exception:
        pass
    return r


def run_pair_live_box(rows, cache, style):
    """Paired measurement in the LIVE box stream (frames.csv bbox).

    No proposer, no tracking: both arms measure the SAME box the shipped session actually
    served, so the two rulers differ only by pill_fill_ruler.  This is the instrument for
    "what would the live session's fill have read", free of replay box drift.
    """
    for k, v in LIVE_ENV.items():
        os.environ[k] = v
    os.environ["ORION_METER_STYLE"] = style
    os.environ["ORION_PILL_RULER"] = "0"
    r_off = build_reader(style, "cv")
    os.environ["ORION_PILL_RULER"] = "1"
    r_on = build_reader(style, "cv")
    out = []
    for i, row in enumerate(rows):
        bx = row["live_bbox"]
        rec = {"i": i, "idx": row["idx"], "ep": row["ep"], "t": row["t"]}
        if not row["live_det"] or bx[2] <= 0 or bx[3] <= 0:
            for name in ("off", "on"):
                rec[name] = {"det": 0, "fill": 0.0, "vel": 0.0, "bbox": list(bx),
                             "rise": "", "reject": "", "g_end": None, "conf": 0.0}
            rec["ruler"] = None
            out.append(rec)
            continue
        img = cache.get(i)
        for name, rd, flag in (("off", r_off, "0"), ("on", r_on, "1")):
            os.environ["ORION_PILL_RULER"] = flag
            fill, green, top = rd._measure_fill_in_box(img, bx, ts=row["t"])
            rec[name] = {"det": int(top >= 0), "fill": round(float(fill), 3), "vel": 0.0,
                         "bbox": list(bx), "rise": "", "reject": "",
                         "g_end": (None if green is None else round(float(green[1]), 3)),
                         "conf": 0.0}
            if name == "on":
                d = getattr(rd, "_dbg_pill_ruler", None)
                rec["ruler"] = dict(d) if d else None
        out.append(rec)
    return out


def run_pair(rows, cache, style, proposer, arms=("off", "on")):
    """Drive one or two independent readers (ruler off / on) over the same frames.

    With a single arm the tool is a BEFORE/AFTER recorder: point --ref-dir at a pristine
    copy of simple_meter_reader.py and the same command produces the pre-change trace.
    """
    for k, v in LIVE_ENV.items():
        os.environ[k] = v
    os.environ["ORION_METER_PROPOSER"] = proposer
    os.environ["ORION_METER_STYLE"] = style
    readers = []
    for name in arms:
        os.environ["ORION_PILL_RULER"] = "0" if name == "off" else "1"
        readers.append((name, build_reader(style, proposer),
                        "0" if name == "off" else "1"))

    out = []
    dl = {"off": -1, "on": -1}
    t_start = _time.perf_counter()
    for i, row in enumerate(rows):
        img = cache.get(i)
        ts = row["t"]
        rec = {"i": i, "idx": row["idx"], "ep": row["ep"], "t": ts}
        for name, rd, flag in readers:
            armed = row["idx"] <= dl[name]
            rd.set_shot_state(armed, 0.0, armed)
            os.environ["ORION_PILL_RULER"] = flag
            res = rd.detect(img, ts=ts)
            det = bool(getattr(res, "detected", False))
            fill = float(getattr(res, "fill_pct", 0.0) or 0.0)
            vel = float(getattr(res, "fill_velocity_pct_s", 0.0) or 0.0)
            rise = str(getattr(res, "rise_state", "") or "")
            if det and (rise == "rising" or vel > 40.0):
                dl[name] = row["idx"] + _SHOT_GATE_ARM_FRAMES
            bbox = tuple(int(v) for v in (getattr(res, "bbox", (0, 0, 0, 0)) or (0, 0, 0, 0)))
            g_end = float(getattr(res, "green_window_center_pct", -1.0) or -1.0)
            g_w = float(getattr(res, "green_window_width_pct", 0.0) or 0.0)
            rec[name] = {
                "det": int(det), "fill": round(fill, 3), "vel": round(vel, 2),
                "bbox": list(bbox), "rise": rise,
                "reject": str(getattr(res, "rejection_reason", "") or ""),
                "g_end": (round(g_end + 0.5 * g_w, 3) if g_end >= 0 else None),
                "conf": round(float(getattr(res, "confidence", 0.0) or 0.0), 3),
            }
            if name == "on":
                d = getattr(rd, "_dbg_pill_ruler", None)
                rec["ruler"] = dict(d) if d else None
        out.append(rec)
        if (i + 1) % 500 == 0:
            sys.stderr.write("  replay %d/%d (%.0fs)\n"
                             % (i + 1, len(rows), _time.perf_counter() - t_start))
    return out


# ------------------------------------------------------------------------------ scoring
def _rises(recs, arm, lo=15.0, hi=60.0):
    """Runs of detected frames climbing from < lo to > hi.  -> [[rec, ...], ...]"""
    runs, cur = [], []
    for r in recs:
        a = r[arm]
        if a["det"] and a["fill"] > 0.0:
            cur.append(r)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    out = []
    for run in runs:
        f = [r[arm]["fill"] for r in run]
        if min(f) < lo and max(f) > hi:
            out.append(run)
    return out


def _vel_in_band(run, arm, lo=36.0, hi=40.0, pad=6.0):
    """pp/ms from a least-squares line over the frames straddling [lo, hi]."""
    import numpy as np
    pts = [(r["t"], r[arm]["fill"]) for r in run
           if lo - pad <= r[arm]["fill"] <= hi + pad]
    if len(pts) < 3:
        return None
    t = np.array([p[0] for p in pts]) * 1000.0
    y = np.array([p[1] for p in pts])
    if t.max() - t.min() < 20.0:
        return None
    m = np.polyfit(t, y, 1)[0]
    return float(m) if m > 0 else None


def _cross_ms(run, arm, level=20.0):
    """Wall-clock ms of the first upward crossing of `level`, linearly interpolated."""
    prev = None
    for r in run:
        f = r[arm]["fill"]
        if prev is not None and prev[1] < level <= f and f > prev[1]:
            frac = (level - prev[1]) / (f - prev[1])
            return (prev[0] + frac * (r["t"] - prev[0])) * 1000.0
        prev = (r["t"], f)
    return None


def report(recs, style, out_dir, tag):
    import numpy as np

    def pct(a, q):
        return float(np.percentile(a, q)) if len(a) else float("nan")

    n = len(recs)
    same_det = sum(1 for r in recs if r["off"]["det"] == r["on"]["det"])
    same_box = sum(1 for r in recs if r["off"]["bbox"] == r["on"]["bbox"])
    same_fill = sum(1 for r in recs if abs(r["off"]["fill"] - r["on"]["fill"]) < 1e-9)
    same_g = sum(1 for r in recs if r["off"]["g_end"] == r["on"]["g_end"])
    diff = [r for r in recs
            if r["off"]["det"] != r["on"]["det"]
            or r["off"]["bbox"] != r["on"]["bbox"]
            or abs(r["off"]["fill"] - r["on"]["fill"]) > 1e-9
            or r["off"]["g_end"] != r["on"]["g_end"]]
    print("\n=== %s  (%s, %d frames) ===" % (tag, style, n))
    print("arm agreement: detected %d/%d  bbox %d/%d  fill %d/%d  green_end %d/%d"
          % (same_det, n, same_box, n, same_fill, n, same_g, n))
    print("FRAMES THAT DIFFER (det|bbox|fill|green): %d" % len(diff))

    rises_on = _rises(recs, "on")
    rises_off = _rises(recs, "off")
    print("rises: box-ruler arm %d, landmark arm %d" % (len(rises_off), len(rises_on)))

    rows = []
    for run in rises_on:
        idx = set(id(r) for r in run)
        v_on = _vel_in_band(run, "on")
        v_off = _vel_in_band(run, "off")
        c_on = _cross_ms(run, "on")
        c_off = _cross_ms(run, "off")
        g_on = [r["on"]["g_end"] for r in run if r["on"]["g_end"] is not None]
        g_off = [r["off"]["g_end"] for r in run if r["off"]["g_end"] is not None]
        spans = [r["ruler"]["span"] for r in run if r.get("ruler")]
        rows.append({
            "ep": run[0]["ep"], "n": len(run),
            "v_off": v_off, "v_on": v_on,
            "c_off": c_off, "c_on": c_on,
            "shift": (None if (c_on is None or c_off is None) else c_on - c_off),
            "g_off": (float(np.median(g_off)) if g_off else None),
            "g_on": (float(np.median(g_on)) if g_on else None),
            "span": (float(np.median(spans)) if spans else None),
        })

    def col(key):
        return np.array([r[key] for r in rows if r[key] is not None], dtype=float)

    print("\n%-22s %7s %7s %7s %7s %7s" % ("metric", "n", "p25", "p50", "p75", "IQR"))
    for label, key in (("vel 36-40% BOX pp/ms", "v_off"),
                       ("vel 36-40% NEW pp/ms", "v_on"),
                       ("20% crossing shift ms", "shift"),
                       ("green_end BOX", "g_off"),
                       ("green_end NEW", "g_on"),
                       ("latched span px", "span")):
        a = col(key)
        if not len(a):
            print("%-22s %7d" % (label, 0))
            continue
        print("%-22s %7d %7.3f %7.3f %7.3f %7.3f"
              % (label, len(a), pct(a, 25), pct(a, 50), pct(a, 75),
                 pct(a, 75) - pct(a, 25)))
    for label, key in (("green_end BOX", "g_off"), ("green_end NEW", "g_on")):
        a = col(key)
        if len(a):
            print("%-22s p10 %.2f  p90 %.2f  sd %.3f" % (label, pct(a, 10), pct(a, 90),
                                                         float(np.std(a))))

    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "pill_ruler_%s_rises.json" % tag), "w") as f:
            json.dump(rows, f, indent=1)
        with open(os.path.join(out_dir, "pill_ruler_%s_frames.jsonl" % tag), "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        if diff:
            with open(os.path.join(out_dir, "pill_ruler_%s_diff.jsonl" % tag), "w") as f:
                for r in diff[:2000]:
                    f.write(json.dumps(r) + "\n")
        print("\nwrote %s/pill_ruler_%s_*.json*" % (out_dir, tag))
    return rows


def compare(path_a, path_b):
    """BEFORE/AFTER diff of two single-arm traces (the Arrow2 no-change proof)."""
    def load(p):
        out = {}
        with open(p) as f:
            for line in f:
                r = json.loads(line)
                arm = r.get("off") or r.get("on")
                out[r["idx"]] = (arm["det"], arm["fill"], tuple(arm["bbox"]),
                                 arm["g_end"], arm["reject"], arm["vel"])
        return out
    a, b = load(path_a), load(path_b)
    keys = sorted(set(a) | set(b))
    diff = [k for k in keys if a.get(k) != b.get(k)]
    print("BEFORE %s  (%d frames)" % (path_a, len(a)))
    print("AFTER  %s  (%d frames)" % (path_b, len(b)))
    print("frames compared: %d   FRAMES THAT DIFFER: %d" % (len(keys), len(diff)))
    for k in diff[:20]:
        print("  idx=%s before=%s after=%s" % (k, a.get(k), b.get(k)))
    return 0 if not diff else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="")
    ap.add_argument("--single", default="", choices=("", "off", "on"),
                    help="run ONE arm and dump its per-frame trace (before/after proof)")
    ap.add_argument("--ref-dir", default="",
                    help="prepended to sys.path, so a pristine simple_meter_reader.py "
                         "there shadows the repo's (the BEFORE arm)")
    ap.add_argument("--compare", nargs=2, default=None,
                    help="two single-arm traces to diff; nothing is replayed")
    ap.add_argument("--style", default="Pill")
    ap.add_argument("--proposer", default="yolo", choices=("yolo", "cv"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=os.path.join("D:", os.sep, "NexusVision", "pill_check"))
    ap.add_argument("--cache-dir", default=os.path.join("D:", os.sep, "NexusVision",
                                                        "pill_check", "_cache"))
    ap.add_argument("--rebuild-cache", action="store_true")
    ap.add_argument("--boxes", default="replay", choices=("replay", "live"),
                    help="replay = the reader's own proposer/tracking; "
                         "live = the boxes frames.csv recorded in the shipped session")
    ap.add_argument("--tag", default="")
    a = ap.parse_args()
    if a.compare:
        return compare(a.compare[0], a.compare[1])
    if not a.session:
        raise SystemExit("--session is required unless --compare is used")
    if a.ref_dir:
        sys.path.insert(0, os.path.abspath(a.ref_dir))
    rows = load_rows(a.session, a.limit or None)
    if not rows:
        raise SystemExit("no frames")
    os.makedirs(a.cache_dir, exist_ok=True)
    cache = FrameCache(a.session, a.cache_dir, rows, rebuild=a.rebuild_cache)
    tag = a.tag or (os.path.basename(a.session.rstrip("/\\")) + "_" + a.style.lower()
                    + "_" + a.boxes)
    if a.single:
        recs = run_pair(rows, cache, a.style, a.proposer, arms=(a.single,))
        import simple_meter_reader as _smr
        os.makedirs(a.out, exist_ok=True)
        p = os.path.join(a.out, "pill_ruler_%s_single.jsonl" % tag)
        with open(p, "w") as f:
            for r in recs:
                f.write(json.dumps(r) + "\n")
        print("reader module: %s" % _smr.__file__)
        print("wrote %s  (%d frames, arm=%s)" % (p, len(recs), a.single))
        return 0
    if a.boxes == "live":
        recs = run_pair_live_box(rows, cache, a.style)
    else:
        recs = run_pair(rows, cache, a.style, a.proposer)
    report(recs, a.style, a.out, tag)
    return 0


if __name__ == "__main__":
    sys.exit(main())
