#!/usr/bin/env python
"""
replay_video.py -- run the PRODUCTION reader over a plain video clip (mp4/mkv), frame
by frame, and report detection quality + drawn-box geometry fidelity.

Purpose: validation on lighting regimes we have NO framedump for (e.g. the neon park
clip C:/Users/aaron/Videos/RED_PARK_NEON.mp4 -- saturated coloured lighting, the
hardest case for a red-meter reader). This changes NOTHING about the reader; it only
measures it. OpenCV decodes the clip (no ffmpeg-on-PATH dependency).

Method (mirrors tools/regression/replay_gates.py so numbers are comparable):
  * PASS 1 unarmed over the WHOLE clip -> find shot spans (same MIN_CORE / MAX_GAP /
    MIN_PEAK consensus as the gates).
  * PASS 2 armed inside those spans (production condition) once per requested
    ORION_READER_BOX_TIGHT mode -> all metrics; the per-frame detected/fill/reason
    triples must be byte-identical across modes (display-only assertion, now proven
    on this clip's lighting too).
Metrics reported per mode:
  * within-shot detection rate / max consecutive miss / drop runs (with rejection
    reasons -- the honest "did the neon break red segmentation" number),
  * off-shot false-lock episodes (same DETACHED/SUSTAINED/STATIC decor definition
    as the gates) + non-decor off-shot episodes,
  * mid-rise glitches (>20pp backward drop before peak) + per-shot peak fill,
  * top-clips (box top below the measured red top row),
  * drawn-box geometry fidelity vs the reader's own raw red-column ground truth
    (box_fidelity._fidelity: width/height ratios, centring, containment, per-frame
    size stability),
  * fresh vs held/coast frame mix inside shots (a reader surviving on coast frames
    in this lighting is a real finding even when detect rate looks fine).

Optionally (--dump-misses DIR) writes every within-shot MISSED frame plus the first
frame of each false-lock episode as PNGs for eyeballing; (--annotate DIR, --annotate-stride N)
writes armed-pass frames with the reported box drawn.

Usage:
  python tools/overlay/replay_video.py C:/Users/aaron/Videos/RED_PARK_NEON.mp4
  python tools/overlay/replay_video.py clip.mp4 --modes 0 2 --dump-misses out/
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as _time

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO not in sys.path:
    sys.path.insert(0, REPO)
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import cv2  # noqa: E402

sys.path.insert(0, os.path.join(REPO, "tools", "regression"))
import replay_gates as rg          # noqa: E402  (shot consensus + decor params)
import box_fidelity as bf          # noqa: E402  (_fidelity aggregation)

FLAG = "ORION_READER_BOX_TIGHT"


def _iter_frames(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit("cannot open video: %s" % path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    idx = 0
    while True:
        ok, bgr = cap.read()
        if not ok:
            break
        yield idx, idx / fps, bgr
        idx += 1
    cap.release()


def _video_meta(path):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise SystemExit("cannot open video: %s" % path)
    meta = {"W": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "H": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": round(float(cap.get(cv2.CAP_PROP_FPS) or 30.0), 3),
            "n_frames_hdr": int(cap.get(cv2.CAP_PROP_FRAME_COUNT))}
    cap.release()
    return meta


def _run_pass(path, W, H, armed_set, mode, annotate=None, annotate_stride=10):
    """One full decode of the clip through a fresh production reader with
    ORION_READER_BOX_TIGHT forced to `mode`. Returns per-frame records shaped for
    box_fidelity._fidelity (ord/det/fill/reason/bbox/bar/top_row/track)."""
    os.environ[FLAG] = str(int(mode))
    from simple_meter_reader import SimpleMeterReader
    reader = SimpleMeterReader(W, H)
    assert reader._box_tight == int(mode), "flag did not take (env read at __init__)"
    records = []
    for idx, ts, bgr in _iter_frames(path):
        if armed_set is not None:
            reader.set_shot_state(idx in armed_set)
        res = reader.detect(bgr, ts=ts)
        src = reader._tight_src        # fresh-red observability twin (mode-independent)
        rec = {
            "ord": idx,
            "det": bool(res.detected),
            "fill": float(res.fill_pct or 0.0),
            "reason": str(getattr(res, "rejection_reason", "") or ""),
            "bbox": tuple(int(v) for v in (res.bbox or (0, 0, 0, 0))),
            "bar": (tuple(int(v) for v in src[0]) if src is not None else None),
            "top_row": (int(src[1]) if src is not None else -1),
            "track": (tuple(int(v) for v in src[2])
                      if src is not None and src[2] is not None else None),
            "res_top_row": int(getattr(res, "top_pixel_row", -1)),
        }
        records.append(rec)
        if annotate is not None and idx % max(1, annotate_stride) == 0:
            im = bgr.copy()
            x, y, w, h = rec["bbox"]
            if rec["det"] and w > 0 and h > 0:
                cv2.rectangle(im, (x, y), (x + w, y + h), (255, 0, 255), 2)
                cv2.putText(im, "%.1f%%" % rec["fill"], (x + w + 6, y + 14),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)
            cv2.imwrite(os.path.join(annotate, "f%05d_m%d.png" % (idx, mode)), im)
    return records


def _episodes(records, shots):
    """Off-shot detection episodes, split decor false-locks (DETACHED+SUSTAINED+STATIC,
    same definition/params as replay_gates) from non-decor tails/fragments."""
    inshot = np.zeros(len(records), bool)
    for a, b in shots:
        inshot[a:b] = True

    def _gap_to_shots(k0, k1):
        return min((max(0, a - k1, k0 - b) for a, b in shots), default=10 ** 9)

    decor, nondecor = [], []
    k = 0
    while k < len(records):
        if (not inshot[k]) and records[k]["det"]:
            j = k
            while j < len(records) and (not inshot[j]) and records[j]["det"]:
                j += 1
            fills = [records[x]["fill"] for x in range(k, j)]
            frng = max(fills) - min(fills)
            ep = {"start": k, "end": j, "n": j - k,
                  "fill_min": round(min(fills), 1), "fill_max": round(max(fills), 1)}
            if (_gap_to_shots(k, j - 1) > rg.TAIL_GAP and (j - k) >= rg.MIN_CORE
                    and frng < rg.STATIC_RANGE):
                decor.append(ep)
            else:
                nondecor.append(ep)
            k = j
        else:
            k += 1
    return decor, nondecor, int((~inshot).sum())


def _shot_quality(records, shots):
    """Within-shot detection + continuity (mirrors replay_gates.measure_session)."""
    tot = drop = maxgap = 0
    miss_frames = []
    for a, b in shots:
        g = 0
        for k in range(a, b):
            tot += 1
            if not records[k]["det"]:
                drop += 1
                g += 1
                maxgap = max(maxgap, g)
                miss_frames.append(k)
            else:
                g = 0
    glitches = []
    peaks = []
    fresh = held = 0
    held_reasons = {}
    for a, b in shots:
        pk = max((records[k]["fill"] for k in range(a, b) if records[k]["det"]),
                 default=0.0)
        peaks.append(round(pk, 1))
        prev = None
        peaked = False
        for k in range(a, b):
            r = records[k]
            if r["det"]:
                if r["bar"] is not None:
                    fresh += 1
                else:
                    held += 1
                    held_reasons[r["reason"]] = held_reasons.get(r["reason"], 0) + 1
            if not r["det"]:
                prev = None
                continue
            f = r["fill"]
            if f >= pk - 5:
                peaked = True
            if prev is not None and not peaked and (prev - f) > 20:
                glitches.append({"ord": k, "drop_pp": round(prev - f, 1)})
            prev = f
    clips = sum(1 for r in records
                if r["det"] and r["res_top_row"] >= 0 and r["bbox"][1] > r["res_top_row"])
    return {
        "within_shot_frames": tot,
        "within_shot_drop": drop,
        "within_shot_detect_rate": round(1.0 - drop / tot, 4) if tot else None,
        "within_shot_maxgap": maxgap,
        "miss_frames": miss_frames,
        "mid_rise_glitches": glitches,
        "shot_peaks": peaks,
        "top_clips": clips,
        "fresh_frames_in_shots": fresh,
        "held_frames_in_shots": held,
        "held_reasons": held_reasons,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--modes", nargs="*", type=int, default=[0, 2])
    ap.add_argument("--dump-misses", default=None,
                    help="dir: write within-shot missed frames + first frame of each "
                         "decor episode as PNGs")
    ap.add_argument("--annotate", default=None,
                    help="dir: write armed-pass frames with the reported box drawn "
                         "(last mode listed)")
    ap.add_argument("--annotate-stride", type=int, default=10)
    args = ap.parse_args()

    meta = _video_meta(args.video)
    W, H = meta["W"], meta["H"]
    out = {"video": args.video, **meta}

    # PASS 1: unarmed, whole clip -> shot consensus (mode irrelevant: display-only,
    # but pin 0 for determinism).
    t0 = _time.perf_counter()
    unarmed = _run_pass(args.video, W, H, armed_set=None, mode=0)
    out["n_frames_decoded"] = len(unarmed)
    shots = rg._find_shots([{"det": r["det"], "fill": r["fill"]} for r in unarmed])
    out["shots"] = [[a, b] for a, b in shots]
    armed_set = set()
    for a, b in shots:
        armed_set.update(range(a, b))

    # PASS 2 per mode: armed inside shots.
    passes = {}
    for m in args.modes:
        ann = None
        if args.annotate and m == args.modes[-1]:
            ann = args.annotate
            os.makedirs(ann, exist_ok=True)
        passes[m] = _run_pass(args.video, W, H, armed_set, m,
                              annotate=ann, annotate_stride=args.annotate_stride)

    # DISPLAY-ONLY assertion across modes on THIS clip's lighting.
    base = passes[args.modes[0]]
    mismatch = 0
    for m in args.modes[1:]:
        for rb, rt in zip(base, passes[m]):
            if rb["det"] != rt["det"] or repr(rb["fill"]) != repr(rt["fill"]) \
                    or rb["reason"] != rt["reason"]:
                mismatch += 1
    out["fill_or_detect_mismatch_frames"] = mismatch

    for m in args.modes:
        rec = passes[m]
        q = _shot_quality(rec, shots)
        decor, nondecor, offshot_frames = _episodes(rec, shots)
        entry = {
            **{k: v for k, v in q.items() if k != "miss_frames"},
            "miss_frames": q["miss_frames"][:50],
            "offshot_falselock_episodes": len(decor),
            "offshot_falselock_detail": decor,
            "offshot_nondecor_episodes": len(nondecor),
            "offshot_nondecor_detail": nondecor[:20],
            "offshot_frames": offshot_frames,
            "geometry": bf._fidelity(rec),
        }
        out["mode%d" % m] = entry

        if args.dump_misses and m == args.modes[-1]:
            os.makedirs(args.dump_misses, exist_ok=True)
            want = set(q["miss_frames"]) | {d["start"] for d in decor}
            for idx, ts, bgr in _iter_frames(args.video):
                if idx in want:
                    tag = "miss" if idx in set(q["miss_frames"]) else "decor"
                    cv2.imwrite(os.path.join(args.dump_misses,
                                             "f%05d_%s.png" % (idx, tag)), bgr)

    out["_wall_s"] = round(_time.perf_counter() - t0, 1)
    print(json.dumps(out, indent=2))
    return 0 if mismatch == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
