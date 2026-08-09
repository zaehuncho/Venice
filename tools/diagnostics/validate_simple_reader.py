#!/usr/bin/env python
"""
validate_simple_reader.py -- confirm the PRODUCTION simple_meter_reader.SimpleMeterReader
reproduces the prototype's head-to-head result on the real framedump sessions.

Reuses the prototype harness's proven comparison logic (list_frames / load_chain_cache /
build_gt / true_shots / _report) verbatim -- only the reader under test changes from the
prototype's inline class to the promoted production class. Uses the CACHED chain results
(chaincache_*.jsonl) so no YOLO model / GPU is needed.

Usage:
  C:/Python314/python.exe tools/diagnostics/validate_simple_reader.py
"""
import argparse
import importlib.util
import os
import sys
import time as _time

import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# Load the PROTOTYPE module by file path (its basename collides with the root production module,
# so a plain import would shadow one of them). We reuse only its harness helpers.
_proto_path = os.path.join(HERE, "simple_meter_reader.py")
_spec = importlib.util.spec_from_file_location("_proto_simple_meter_reader", _proto_path)
proto = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proto)

# Production reader under test (repo root).
from simple_meter_reader import SimpleMeterReader


def _pass(frames, cache, W, H, armed_set=None):
    """Run the production reader over the frames. armed_set = set of idx_ord to ARM the shot-gate on
    (proxy for 'the bot is shooting'); None = fully unarmed. Returns (records, simple_ms, green_pop)."""
    from collections import Counter
    reader = SimpleMeterReader(W, H)
    records = []
    simple_ms = 0.0
    green_pop = 0
    stage_ms = Counter(); stage_n = Counter()
    for ordi, (idx, flag, path) in enumerate(frames):
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is None:
            continue
        if armed_set is not None:
            reader.set_shot_state(ordi in armed_set)
        c = {"chain_det": False, "chain_fill": 0.0, "chain_bbox": [0, 0, 0, 0],
             "chain_rise": "", "chain_conf": 0.0, "chain_rej": ""}
        if cache is not None:
            hit = cache.get((idx, flag))
            if hit:
                c = {k: hit[k] for k in c}
        t0 = _time.perf_counter()
        s = reader.read(bgr, ts=ordi / 60.0)               # deterministic 60fps clock -> real velocity
        dms = (_time.perf_counter() - t0) * 1000.0
        simple_ms += dms; stage_ms[s["stage"]] += dms; stage_n[s["stage"]] += 1
        if s.get("green") is not None:
            green_pop += 1
        records.append({"idx_ord": ordi, "idx": idx, "flag": flag, **c,
                        "simple_det": s["detected"], "simple_fill": s["fill"],
                        "simple_coarse": s["fill_coarse"], "simple_bbox": s["bbox"],
                        "simple_stage": s["stage"], "simple_ms": round(dms, 3)})
    return records, simple_ms, green_pop, {k: "%.3fms x%d" % (stage_ms[k] / max(1, stage_n[k]), stage_n[k]) for k in stage_n}


def _within_shot_dropout(records, shots):
    tot = drop = 0
    for a, b in shots:
        for k in range(a, b):
            tot += 1
            if not records[k]["simple_det"]:
                drop += 1
    return drop, tot


def run_session(session_dir, out_dir):
    frames = proto.list_frames(session_dir)
    sess = os.path.basename(session_dir)
    cache = proto.load_chain_cache(os.path.join(out_dir, "chaincache_%s.jsonl" % sess))
    print("\n#################### PRODUCTION READER: %s ####################" % sess)
    print("session=%s frames=%d chain_cache=%s" % (
        sess, len(frames), "yes(%d)" % len(cache) if cache else "MISSING"))
    if not frames:
        print("  no frames -> skip"); return
    b0 = cv2.imread(frames[0][2], cv2.IMREAD_COLOR)
    H, W = b0.shape[:2]

    # PASS 1: UNARMED (the offline head-to-head baseline; reproduces the prototype).
    records, simple_ms, green_pop, stage = _pass(frames, cache, W, H, armed_set=None)
    print("stage cost:", stage)
    jp = os.path.join(out_dir, "prod_hh_%s.jsonl" % sess)
    import json
    with open(jp, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    proto._report(records, sess, 0.0, simple_ms, cache is not None, jp)

    # PASS 2: ARMED within the consensus true-shot spans (offline proxy for the live bot arm signal).
    shots = proto.true_shots(records)
    armed_set = set()
    for a, b in shots:
        armed_set.update(range(a, b))
    records2, _ms2, green_pop2, _stage2 = _pass(frames, cache, W, H, armed_set=armed_set)
    d1, t1 = _within_shot_dropout(records, shots)
    d2, t2 = _within_shot_dropout(records2, shots)
    print("\n[SHOT-GATE ARMED A/B]  (armed within the %d consensus true-shot spans)" % len(shots))
    print("  within-shot dropout: UNARMED %d/%d (%.1f%%) -> ARMED %d/%d (%.1f%%)" % (
        d1, t1, 100.0 * d1 / max(1, t1), d2, t2, 100.0 * d2 / max(1, t2)))
    det1 = sum(r["simple_det"] for r in records)
    det2 = sum(r["simple_det"] for r in records2)
    print("  total detections:   UNARMED %d -> ARMED %d" % (det1, det2))
    print("  GREEN-WINDOW populated frames: UNARMED %d -> ARMED %d (of %d)" % (
        green_pop, green_pop2, len(records)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=[
        "session_20260704_210801", "session_20260706_162326"])
    ap.add_argument("--framedump", default=os.path.join(REPO, "logs", "diagnostics", "framedump"))
    ap.add_argument("--out", default=os.path.join(REPO, "logs", "diagnostics", "simple_vs_chain"))
    args = ap.parse_args()
    for s in args.sessions:
        run_session(os.path.join(args.framedump, s), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
