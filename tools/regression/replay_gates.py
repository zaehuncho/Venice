#!/usr/bin/env python
"""
replay_gates.py -- OFFLINE detection regression core.

Replays the recorded live framedumps through the PRODUCTION simple_meter_reader.SimpleMeterReader
and computes the quality metrics that the regression gates assert floors on. This is the single
source of truth for both:

  * tools/regression/run_gates.py   (human PASS/FAIL report)
  * tests/test_detection_regression.py  (pytest suite; skips gracefully when a dump is absent)

NO chain / YOLO / GPU is needed: the gates are self-referential to the production reader (that IS
the thing we are protecting). Where a cached serving-chain result exists (chaincache_*.jsonl) an
extra fill-vs-chain gate is added, but it is optional.

Design (mirrors tools/diagnostics/validate_simple_reader.py, but standalone -- tools/diagnostics is
deliberately NOT put on sys.path):
  * SHOTS are found from an UNARMED pass (a run of >= MIN_CORE frames the reader detects, expanded
    over the surrounding detect span up to MAX_GAP, requiring a peak fill >= MIN_PEAK).
  * The production condition is the ARMED pass: the bot KNOWS it is shooting, so set_shot_state(True)
    within each shot span (offline proxy for the live held-trigger / v9-pose arm signal). All gate
    metrics are measured on the ARMED pass -- that is what ships.

Usage (baseline dump):
  C:/Python314/python.exe tools/regression/replay_gates.py --dump
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import time as _time

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import cv2  # noqa: E402
from simple_meter_reader import SimpleMeterReader  # noqa: E402

FRAMEDUMP = os.path.join(REPO, "logs", "diagnostics", "framedump")
CHAINCACHE_DIR = os.path.join(REPO, "logs", "diagnostics", "simple_vs_chain")

# Shot-consensus params (identical to the diagnostics harness).
MIN_CORE = 5
MAX_GAP = 3
MIN_PEAK = 70.0

# A full 2MB-PNG replay of every session is ~30 min wall on this box, and a blind frame PREFIX misses
# the shots (they often start 700-2000 frames in, after the pre-game/warmup). So the gate first does a
# CHEAP coarse locate -- decode every COARSE_STRIDE-th frame to find where the shots are -- then does
# the real 2-pass measurement over a bounded DENSE window (GATE_WINDOW frames) anchored on the first
# shot. This is deterministic (same frames every run), reliably contains shots, and stays ~1-2 min per
# session. Override the window with ORION_GATE_WINDOW; set it to 0 to measure the FULL session.
try:
    GATE_WINDOW = int(os.environ.get("ORION_GATE_WINDOW", "900"))
except ValueError:
    GATE_WINDOW = 900
COARSE_STRIDE = 8
COARSE_FILL = 55.0        # a coarse-scan detection this strong marks a shot region
WINDOW_LEAD = 40          # start the dense window this many frames before the first located shot
# A décor false-lock (the thing we guard against: the reader latching a static red graphic -- City
# kiosk / 2K logo / banner) is a detached off-shot detection that is SUSTAINED and STATIC. It is
# distinguished from a real meter fragment by three tests, ALL required to count as a décor lock:
#   * DETACHED  : > TAIL_GAP frames from every shot span (adjacency => rise/deflate tail, not a lock),
#   * SUSTAINED : >= MIN_CORE frames (a 1-4 frame blip is a boundary fragment, not a lock),
#   * STATIC    : fill range < STATIC_RANGE across the run -- a real meter (even a rejected low-peak
#                 contested shot) RISES/falls; a static graphic reads a ~constant fill.
TAIL_GAP = 8
STATIC_RANGE = 12.0

_FRAME_RE = re.compile(r"f(\d{5})_([01])_raw\.png$")


# --------------------------------------------------------------------------- #
#  Framedump IO
# --------------------------------------------------------------------------- #
def list_frames(session_dir):
    out = []
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = _FRAME_RE.search(os.path.basename(p))
        if m:
            out.append((int(m.group(1)), int(m.group(2)), p))
    out.sort(key=lambda t: (t[0], t[1]))
    return out


def load_chain_cache(session):
    path = os.path.join(CHAINCACHE_DIR, "chaincache_%s.jsonl" % session)
    if not os.path.exists(path):
        return None
    cache = {}
    with open(path) as f:
        for line in f:
            r = json.loads(line)
            cache[(r["idx"], r["flag"])] = r
    return cache


# --------------------------------------------------------------------------- #
#  Reader pass
# --------------------------------------------------------------------------- #
def _run_pass(frames, W, H, armed_set=None, scale=None):
    """Run the production reader over the frames. armed_set = ordinal indices to ARM the shot-gate
    on (None = fully unarmed). scale != None resizes every frame (camera-adapt probe). Returns a list
    of per-frame record dicts."""
    reader = SimpleMeterReader(W if scale is None else int(round(W * scale)),
                               H if scale is None else int(round(H * scale)))
    records = []
    for ordi, (idx, flag, path) in enumerate(frames):
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        if scale is not None:
            bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if armed_set is not None:
            reader.set_shot_state(ordi in armed_set)
        t0 = _time.perf_counter()
        res = reader.detect(bgr, ts=ordi / 60.0)   # deterministic 60fps clock -> real velocity
        dms = (_time.perf_counter() - t0) * 1000.0
        bx = tuple(int(v) for v in (res.bbox or (0, 0, 0, 0)))
        records.append({
            "idx_ord": ordi, "idx": idx, "flag": flag,
            "det": bool(res.detected), "fill": float(res.fill_pct or 0.0),
            "bbox": bx, "top_row": int(getattr(res, "top_pixel_row", -1)),
            "rise": str(res.rise_state or ""),
            "reason": str(getattr(res, "rejection_reason", "") or ""),
            "green_center": float(getattr(res, "green_window_center_pct", -1.0)),
            "ms": dms,
        })
    return records


def _find_shots(records):
    """Simple-only shot spans: a run of >= MIN_CORE detected frames, expanded over the surrounding
    detected span up to MAX_GAP, requiring peak fill >= MIN_PEAK. (No chain: db == de == det.)"""
    n = len(records)
    det = [r["det"] for r in records]
    shots = []
    i = 0
    while i < n:
        if not det[i]:
            i += 1
            continue
        j = i
        while j < n and det[j]:
            j += 1
        if j - i >= MIN_CORE:
            a, b = i, j
            gap = 0
            while a > 0 and (det[a - 1] or gap < MAX_GAP):
                a -= 1
                gap = 0 if det[a] else gap + 1
            gap = 0
            while b < n and (det[b] or gap < MAX_GAP):
                gap = 0 if det[b] else gap + 1
                b += 1
            peak = max((records[k]["fill"] for k in range(a, b) if records[k]["det"]), default=0.0)
            if peak >= MIN_PEAK:
                if shots and a <= shots[-1][1]:
                    shots[-1] = (shots[-1][0], b)
                else:
                    shots.append((a, b))
            i = max(j, b)
        else:
            i = j
    return shots


# --------------------------------------------------------------------------- #
#  Cheap coarse shot-window locate (so the dense window actually contains shots)
# --------------------------------------------------------------------------- #
def _locate_window(frames, W, H, window):
    """Decode every COARSE_STRIDE-th frame with a throwaway reader; return (start, end) into `frames`
    for a contiguous `window`-frame span anchored WINDOW_LEAD frames before the first strong (>=
    COARSE_FILL) detection. Falls back to the prefix [0:window] when nothing strong is found."""
    if window <= 0 or window >= len(frames):
        return 0, len(frames)
    reader = SimpleMeterReader(W, H)
    first = None
    for k in range(0, len(frames), COARSE_STRIDE):
        bgr = cv2.imread(frames[k][2], cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        res = reader.detect(bgr, ts=k / 60.0)
        if res.detected and float(res.fill_pct or 0.0) >= COARSE_FILL:
            first = k
            break
    if first is None:
        return 0, window
    start = max(0, first - WINDOW_LEAD)
    end = min(len(frames), start + window)
    return start, end


# --------------------------------------------------------------------------- #
#  Metrics
# --------------------------------------------------------------------------- #
def measure_session(session, framedump=FRAMEDUMP, window=None):
    """Replay one session's shot window (coarse-located). Returns a metrics dict (or present:False)."""
    if window is None:
        window = GATE_WINDOW
    session_dir = os.path.join(framedump, session)
    frames = list_frames(session_dir)
    if not frames:
        return {"present": False, "session": session}
    total = len(frames)

    b0 = cv2.imread(frames[0][2], cv2.IMREAD_COLOR)
    H, W = b0.shape[:2]
    wstart, wend = _locate_window(frames, W, H, window)
    frames = frames[wstart:wend]

    # PASS 1 unarmed -> find shots.
    unarmed = _run_pass(frames, W, H, armed_set=None)
    shots = _find_shots(unarmed)
    armed_set = set()
    for a, b in shots:
        armed_set.update(range(a, b))

    # PASS 2 armed (production condition) -> all gate metrics.
    armed = _run_pass(frames, W, H, armed_set=armed_set)

    m = {"present": True, "session": session, "frames": len(frames), "frames_total": total,
         "window": [wstart, wend], "W": W, "H": H, "n_shots": len(shots)}

    # within-shot detection + disappearance (max consecutive miss inside a shot).
    tot = drop = maxgap = 0
    for a, b in shots:
        g = 0
        for k in range(a, b):
            tot += 1
            if not armed[k]["det"]:
                drop += 1
                g += 1
                maxgap = max(maxgap, g)
            else:
                g = 0
    m["within_shot_frames"] = tot
    m["within_shot_drop"] = drop
    m["within_shot_detect_rate"] = (1.0 - drop / tot) if tot else 0.0
    m["within_shot_maxgap"] = maxgap

    # off-shot false-lock episodes (see the DETACHED / SUSTAINED / STATIC definition above).
    inshot = np.zeros(len(armed), bool)
    for a, b in shots:
        inshot[a:b] = True

    def _gap_to_shots(k0, k1):
        return min((max(0, a - k1, k0 - b) for a, b in shots), default=10 ** 9)

    decor = decor_frames = non_decor_offshot = 0
    k = 0
    while k < len(armed):
        if (not inshot[k]) and armed[k]["det"]:
            j = k
            while j < len(armed) and (not inshot[j]) and armed[j]["det"]:
                j += 1
            fills = [armed[x]["fill"] for x in range(k, j)]
            frng = max(fills) - min(fills)
            if (_gap_to_shots(k, j - 1) > TAIL_GAP and (j - k) >= MIN_CORE
                    and frng < STATIC_RANGE):
                decor += 1
                decor_frames += (j - k)
            else:
                non_decor_offshot += 1          # rise/deflate tail, boundary fragment, or a rejected
            k = j                               # low-peak (contested) shot -- NOT a décor lock
        else:
            k += 1
    m["offshot_falselock_episodes"] = decor
    m["offshot_falselock_frames"] = decor_frames
    m["offshot_nondecor_episodes"] = non_decor_offshot
    m["offshot_frames"] = int((~inshot).sum())

    # top-clip: box top (bbox[1]) below the red top row -> the column is clipped.
    clips = 0
    for r in armed:
        if r["det"] and r["top_row"] >= 0 and r["bbox"][1] > r["top_row"]:
            clips += 1
    m["top_clips"] = clips

    # mid-rise glitch: >20pp backward drop before the shot peak (deflate/cap not counted).
    glitches = 0
    peaks = []
    for a, b in shots:
        pk = max((armed[k]["fill"] for k in range(a, b) if armed[k]["det"]), default=0.0)
        peaks.append(pk)
        prev = None
        peaked = False
        for k in range(a, b):
            if not armed[k]["det"]:
                prev = None
                continue
            f = armed[k]["fill"]
            if f >= pk - 5:
                peaked = True
            if prev is not None and not peaked and (prev - f) > 20:
                glitches += 1
            prev = f
    m["mid_rise_glitches"] = glitches
    m["median_shot_peak"] = float(np.median(peaks)) if peaks else 0.0

    # within-shot GHOST frames (the mid-shot overlay blink the live user sees): a not-detected
    # OR zero-box emission strictly BETWEEN the first and last GENUINE read of a shot. Genuine
    # read = detected with a real box whose value is a FRESH measurement this frame (held-fill
    # re-emissions -- coast meter_memory / dead_reckoned / held_reseat -- bound nothing: the
    # stale echo coasting ahead of a new shot's first real read is CORRECTLY laundered there).
    # reason 'occl_relock' is exempt: B1's designed one-frame >20pp continuity launder (its own
    # guards -- mid_rise_glitches, within_shot_detect_rate -- already bound it). Baseline HEAD
    # 2026-07-19: 2 on session_20260706_190737 (the A2 coast-steal 'steal_reseat' launder at
    # o=734 + the detected:True zero-box meter_memory frame it strands at o=735 -- a SAME-METER
    # reseat, fill 92.7->91.6, blinking mid-shot).
    _HELD_REASONS = ("meter_memory", "dead_reckoned", "held_reseat")
    ghosts = 0
    for a, b in shots:
        ks = [k for k in range(a, b)
              if armed[k]["det"] and any(armed[k]["bbox"])
              and armed[k]["reason"] not in _HELD_REASONS]
        if len(ks) >= 2:
            for k in range(ks[0] + 1, ks[-1]):
                r = armed[k]
                if r["reason"] == "occl_relock":
                    continue
                if (not r["det"]) or not any(r["bbox"]):
                    ghosts += 1
    m["within_shot_ghost_frames"] = ghosts

    m["read_ms_median"] = float(np.median([r["ms"] for r in armed]))

    # camera-adapt: the single longest shot span still locks when the crop is scaled 1.6x / 0.6x.
    m["camera_adapt"] = {}
    if shots:
        a, b = max(shots, key=lambda s: s[1] - s[0])
        pad = 8
        span = frames[max(0, a - pad):b + pad]
        for tag, sc in (("x1.6", 1.6), ("x0.6", 0.6)):
            rec = _run_pass(span, W, H, armed_set=set(range(len(span))), scale=sc)
            det = sum(1 for r in rec if r["det"])
            m["camera_adapt"][tag] = {"det": det, "n": len(rec),
                                      "rate": det / max(1, len(rec))}

    # optional fill-vs-chain gate (only if a chaincache exists for this session).
    cache = load_chain_cache(session)
    if cache is not None:
        diffs = []
        for r in armed:
            hit = cache.get((r["idx"], r["flag"]))
            if hit and hit.get("chain_det") and r["det"]:
                diffs.append(abs(float(hit["chain_fill"]) - r["fill"]))
        if diffs:
            d = np.array(diffs)
            m["fill_vs_chain"] = {"n": len(d), "mean": float(d.mean()),
                                  "median": float(np.median(d)),
                                  "within5pp": float((d <= 5).mean()),
                                  "within10pp": float((d <= 10).mean())}
    return m


# --------------------------------------------------------------------------- #
#  Gate evaluation (shared by run_gates.py and tests/test_detection_regression.py)
# --------------------------------------------------------------------------- #
FLOORS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gate_floors.json")


def load_floors():
    with open(FLOORS_PATH) as f:
        return json.load(f)


class GateResult:
    __slots__ = ("name", "measured", "floor", "op", "passed", "detail")

    def __init__(self, name, measured, floor, op, detail=""):
        self.name = name
        self.measured = measured
        self.floor = floor
        self.op = op
        self.detail = detail
        if floor is None:
            self.passed = True
        elif op == ">=":
            self.passed = measured >= floor
        elif op == "<=":
            self.passed = measured <= floor
        else:
            raise ValueError(op)

    def __repr__(self):
        v = "%.3f" % self.measured if isinstance(self.measured, float) else str(self.measured)
        fl = ("%.3f" % self.floor if isinstance(self.floor, float) else str(self.floor)) if self.floor is not None else "-"
        return "%s %s=%s (floor %s %s)" % ("PASS" if self.passed else "FAIL", self.name, v, self.op, fl)


def evaluate_session(session, floors=None, m=None):
    """Measure (or accept a pre-measured m) and return (metrics, [GateResult]). Empty list => dump
    absent (caller should skip)."""
    if floors is None:
        floors = load_floors()
    if m is None:
        m = measure_session(session)
    if not m.get("present"):
        return m, []
    fl = dict(floors.get("_default", {}))       # per-session entries OVERRIDE the shared default
    fl.update({k: v for k, v in floors.get(session, {}).items() if not k.startswith("_")})
    g = []
    g.append(GateResult("n_shots", m["n_shots"], fl.get("n_shots_min"), ">="))
    g.append(GateResult("within_shot_detect_rate", m["within_shot_detect_rate"],
                        fl.get("within_shot_detect_rate_min"), ">="))
    g.append(GateResult("within_shot_maxgap", m["within_shot_maxgap"],
                        fl.get("within_shot_maxgap_max"), "<=",
                        "no within-shot disappearance through rise->cap->deflate"))
    g.append(GateResult("offshot_falselock_episodes", m["offshot_falselock_episodes"],
                        fl.get("offshot_falselock_episodes_max"), "<=", "0 decor false-locks off-shot"))
    g.append(GateResult("top_clips", m["top_clips"], fl.get("top_clips_max"), "<=",
                        "box top never below the red top (tip always captured)"))
    g.append(GateResult("mid_rise_glitches", m["mid_rise_glitches"],
                        fl.get("mid_rise_glitches_max"), "<=", "no >20pp backward drop before peak"))
    if fl.get("within_shot_ghost_frames_max") is not None:
        g.append(GateResult("within_shot_ghost_frames", m.get("within_shot_ghost_frames", 0),
                            fl.get("within_shot_ghost_frames_max"), "<=",
                            "no mid-shot detected:False / zero-box blink between genuine reads"))
    g.append(GateResult("median_shot_peak", m["median_shot_peak"],
                        fl.get("median_shot_peak_min"), ">="))
    ca = m.get("camera_adapt", {})
    if "x1.6" in ca:
        g.append(GateResult("camera_adapt_x1.6", ca["x1.6"]["rate"],
                            fl.get("camera_adapt_min"), ">=", "1.6x-scaled crop still locks"))
    if "x0.6" in ca:
        g.append(GateResult("camera_adapt_x0.6", ca["x0.6"]["rate"],
                            fl.get("camera_adapt_min"), ">=", "0.6x-scaled crop still locks"))
    fvc = m.get("fill_vs_chain")
    if fvc and fl.get("fill_vs_chain_within5pp_min") is not None:
        g.append(GateResult("fill_vs_chain_within5pp", fvc["within5pp"],
                            fl.get("fill_vs_chain_within5pp_min"), ">=",
                            "peak/rise fill agrees with the serving chain"))
    return m, g


# Target sessions (task-named). fades_20260704_210801 is the fade reference (has a chaincache).
SESSIONS = [
    "session_20260707_135309",
    "session_20260707_175017",
    "session_20260707_121446",
    "session_20260706_190737",
    "session_20260704_210801",
]


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=SESSIONS)
    ap.add_argument("--dump", action="store_true", help="write baseline JSON to stdout")
    ap.add_argument("--out", default=None, help="write JSON incrementally to this file (per session)")
    args = ap.parse_args()
    out = {}
    for s in args.sessions:
        t0 = _time.perf_counter()
        out[s] = measure_session(s)
        out[s]["_wall_s"] = round(_time.perf_counter() - t0, 1)
        sys.stderr.write("done %s in %.1fs (window %s of %d, %d frames, %d shots)\n" % (
            s, out[s]["_wall_s"], out[s].get("window"), out[s].get("frames_total", 0),
            out[s].get("frames", 0), out[s].get("n_shots", 0)))
        sys.stderr.flush()
        if args.out:                       # persist after EACH session so a kill can't lose everything
            with open(args.out, "w") as f:
                json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
