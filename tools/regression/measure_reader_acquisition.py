#!/usr/bin/env python
"""
measure_reader_acquisition.py -- OFFLINE A/B for the two reader acquisition flags
(2026-08-06, both default OFF):

  P1  ORION_READER_POST_RELEASE_YIELD   post-release lock yield (force-unlock a spent,
                                        frozen >=90 lock instead of coasting it as
                                        meter_memory; skip the steal display launder)
  P2  ORION_READER_FRESH_RISE_STEPS_ARMED   armed-window N-rising-frames proof count
                                        (3 = shipped behaviour, 2 = one frame faster)

It replays the recorded live framedumps through the PRODUCTION SimpleMeterReader in the
ORCHESTRATOR wiring (require_gameplay_eligibility=True, hardware arm + shot-start +
release-marker relays emulated from the unarmed shot consensus) and reports:

  * the FIRST-FED-FILL distribution per shot (fed = detected AND rejection_reason in
    {"", "green_not_found"} -- the frames the engine's fresh tier accepts), per flag config;
  * the post-release meter_memory tail (fed=0 coast frames between a release and the next
    shot) per flag config -- P1's direct target;
  * the off-shot admission count of the 2-step vs 3-step monotonic-rise rule (P2's
    false-lock risk question, as specified: run the rule over off-shot frames and count).

OFFLINE PROXY CAVEATS (be honest when quoting numbers): the hw-arm windows, shot-start
epochs and release markers are reconstructed from the unarmed pass consensus (arm opens
LEAD_IN frames before the span, stays open across gaps <= BRIDGE_GAP frames to model the
measured back-to-back single-window trace, release fires at the first >=RELEASE_FILL frame).
Live wiring differs per session; use these numbers for DIRECTION and frame counts, not as a
banner-grade outcome instrument.

Usage:
  C:/Python314/python.exe tools/regression/measure_reader_acquisition.py \
      --sessions session_20260804_201020 session_20260804_202151
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time as _time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (HERE, REPO):
    if p not in sys.path:
        sys.path.insert(0, p)

import cv2  # noqa: E402
import replay_gates as RG  # noqa: E402
from simple_meter_reader import SimpleMeterReader  # noqa: E402

LEAD_IN = 6          # arm this many frames before the consensus span (live arm precedes HUD)
TAIL = 40            # keep the window open this long past the span end (release occlusion)
BRIDGE_GAP = 90      # <= this many frames between spans -> ONE continuous hw window
RELEASE_FILL = 92.0  # notify_release at the first armed frame at/above this fill
FED_REASONS = ("", "green_not_found")
HELD_REASONS = ("meter_memory", "dead_reckoned", "held_reseat", "steal_reseat")
FRESH_UP_PP = 0.25   # mirror of ORION_READER_FRESH_RISE_PP default (B5 up/down band)


def _decode(frames):
    for ordi, (idx, flag, path) in enumerate(frames):
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
        if bgr is not None:
            yield ordi, bgr


def _plan_windows(shots, n, bridge_gap=BRIDGE_GAP):
    """Reconstruct the hw-arm timeline from the unarmed consensus: per-frame armed flag,
    {ordinal: epoch} shot starts, and the span->window mapping. Spans closer than
    `bridge_gap` share ONE window (the measured back-to-back trace: no disarm, no new
    shot-start relay between the release and the next attempt). bridge_gap=0 models the
    per-press wiring instead: every span gets its own window, shot-start relay and disarm
    (the reader's arm-edge cold re-acquire runs per shot)."""
    armed = np.zeros(n, bool)
    starts = {}
    epoch = 100
    windows = []          # (w_start, w_end, [span, span, ...])
    for a, b in shots:
        if windows and bridge_gap > 0 and a - windows[-1][1] <= bridge_gap:
            w = windows[-1]
            windows[-1] = (w[0], min(n, b + TAIL), w[2] + [(a, b)])
        else:
            windows.append((max(0, a - LEAD_IN), min(n, b + TAIL), [(a, b)]))
    for (wa, wb, spans) in windows:
        armed[wa:wb] = True
        starts[wa] = epoch
        epoch += 1
    return armed, starts, windows


def run_orchestrated_pass(frames, W, H, armed, starts, env):
    """Production-wiring pass: eligibility gate ON, hw arm + shot-start + release relays."""
    keep = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        reader = SimpleMeterReader(W, H, require_gameplay_eligibility=True)
    finally:
        for k, v in keep.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    records = [None] * len(frames)
    release_seq = 0
    released_this_window = False
    prev_armed = False
    for ordi, bgr in _decode(frames):
        is_armed = bool(armed[ordi])
        if is_armed and not prev_armed:
            released_this_window = False
        if ordi in starts:
            reader.notify_physical_shot_start(starts[ordi])
        reader.set_shot_state(is_armed, 0.0, is_armed)
        res = reader.detect(bgr, ts=ordi / 60.0)
        fill = float(res.fill_pct or 0.0)
        det = bool(res.detected)
        reason = str(getattr(res, "rejection_reason", "") or "")
        if (is_armed and det and not released_this_window
                and reason in FED_REASONS and fill >= RELEASE_FILL):
            release_seq += 1
            reader.notify_release(seq=release_seq)
            released_this_window = True
        records[ordi] = {
            "det": det, "fill": fill, "reason": reason,
            "vel": float(getattr(res, "fill_velocity_pct_s", 0.0) or 0.0),
            "rise": str(res.rise_state or ""),
            "armed": is_armed,
            "released": released_this_window,
        }
        prev_armed = is_armed
    yields_fired = int(getattr(reader, "_pr_yield_n", 0))
    return records, yields_fired


def first_fed_per_span(records, windows):
    """Per consensus span: the fill of the first FED frame (engine-fresh) at/after the
    span's search start. Inside a bridged window a later span's search starts at the
    PREVIOUS span's end (the release tail), which is exactly the back-to-back seam."""
    out = []
    for (wa, wb, spans) in windows:
        for si, (a, b) in enumerate(spans):
            search_from = wa if si == 0 else spans[si - 1][1]
            hit = None
            for k in range(max(search_from, a - LEAD_IN), b):
                r = records[k]
                if r and r["det"] and r["reason"] in FED_REASONS and r["fill"] > 0.5:
                    hit = (k, r["fill"])
                    break
            out.append({"span": [a, b], "back_to_back": si > 0,
                        "first_fed_ord": hit[0] if hit else -1,
                        "first_fed_fill": hit[1] if hit else -1.0})
    return out


def post_release_tail(records, windows):
    """meter_memory / held frames served after a release inside its window (P1's target)."""
    held = fed = 0
    for (wa, wb, spans) in windows:
        for k in range(wa, wb):
            r = records[k]
            if r is None or not r["released"]:
                continue
            if r["det"] and r["reason"] in HELD_REASONS:
                held += 1
            elif r["det"] and r["reason"] in FED_REASONS:
                fed += 1
    return {"held_frames": held, "fed_frames": fed}


def offshot_admissions(records, shots, n_steps):
    """Count off-shot episodes the N-consecutive-rising-frames rule would admit
    (rise run of >=N up-steps > FRESH_UP_PP with velocity > 25 %/s at the Nth), over the
    UNARMED replay -- the task-specified proxy for the 2-vs-3-step false-lock risk.

    The velocity is computed from the RAW fill series at the replay's 60fps clock
    (single-step delta * 60), NOT from the reader's own `rising` label: that label is
    itself gated at the base 3-step rule off-shot, which would make a 2-step census
    circular (nothing 2-step-only could ever be labeled rising)."""
    inshot = np.zeros(len(records), bool)
    for a, b in shots:
        inshot[a:b] = True
    admissions = 0
    run = 0
    prev = None
    prev_ord = None
    admitted_this_run = False
    for k, r in enumerate(records):
        if r is None or not r["det"] or inshot[k] or r["reason"] in HELD_REASONS:
            # conservative proxy: a gap/held frame neither advances nor resets the run
            # (B5 only updates on fresh reads); an in-shot frame ends the off-shot run.
            if inshot[k]:
                run = 0
                prev = None
                prev_ord = None
                admitted_this_run = False
            continue
        f = r["fill"]
        vel = 0.0
        if prev is not None:
            gap = max(1, k - prev_ord)
            vel = (f - prev) * 60.0 / gap
            if f > prev + FRESH_UP_PP:
                run += 1
            elif f < prev - FRESH_UP_PP:
                run = 0
                admitted_this_run = False
        prev = f
        prev_ord = k
        if run >= n_steps and vel > 25.0 and not admitted_this_run:
            admissions += 1
            admitted_this_run = True
    return admissions


def measure(session, window, census_only=False, bridge_gap=BRIDGE_GAP):
    session_dir = os.path.join(RG.FRAMEDUMP, session)
    frames = RG.list_frames(session_dir)
    if not frames:
        return {"present": False, "session": session}
    b0 = cv2.imread(frames[0][2], cv2.IMREAD_COLOR)
    H, W = b0.shape[:2]
    wstart, wend = RG._locate_window(frames, W, H, window)
    frames = frames[wstart:wend]

    # Consensus pass (unarmed, ungated) -> spans + the off-shot admission census.
    unarmed = RG._run_pass(frames, W, H, armed_set=None)
    shots = RG._find_shots(unarmed)
    un_records = [{"det": r["det"], "fill": r["fill"], "reason": r["reason"],
                   "vel": 0.0, "rise": r["rise"], "armed": False, "released": False}
                  for r in unarmed]

    armed, starts, windows = _plan_windows(shots, len(frames), bridge_gap=bridge_gap)

    inshot = np.zeros(len(frames), bool)
    for a, b in shots:
        inshot[a:b] = True
    out = {"present": True, "session": session, "frames": len(frames),
           "n_shots": len(shots), "shots": shots,
           "offshot_frames": int((~inshot).sum()),
           "offshot_det_frames": int(sum(1 for k, r in enumerate(un_records)
                                         if r["det"] and not inshot[k])),
           "offshot_admissions_3step": offshot_admissions(un_records, shots, 3),
           "offshot_admissions_2step": offshot_admissions(un_records, shots, 2),
           "configs": {}}
    if census_only:
        return out

    configs = {
        "base": {},
        "p1_yield": {"ORION_READER_POST_RELEASE_YIELD": "1"},
        "p2_steps2": {"ORION_READER_FRESH_RISE_STEPS_ARMED": "2"},
        "p1p2": {"ORION_READER_POST_RELEASE_YIELD": "1",
                 "ORION_READER_FRESH_RISE_STEPS_ARMED": "2"},
    }
    for name, env in configs.items():
        t0 = _time.perf_counter()
        records, yields_fired = run_orchestrated_pass(frames, W, H, armed, starts, env)
        ff = first_fed_per_span(records, windows)
        fills = [x["first_fed_fill"] for x in ff if x["first_fed_fill"] >= 0.0]
        b2b = [x["first_fed_fill"] for x in ff
               if x["first_fed_fill"] >= 0.0 and x["back_to_back"]]
        det_total = sum(1 for r in records if r and r["det"])
        fed_total = sum(1 for r in records if r and r["det"] and r["reason"] in FED_REASONS)
        out["configs"][name] = {
            "det_frames_total": det_total,
            "fed_frames_total": fed_total,
            "first_fed": ff,
            "first_fed_fill_median": float(np.median(fills)) if fills else -1.0,
            "first_fed_fill_mean": float(np.mean(fills)) if fills else -1.0,
            "first_fed_fill_median_back_to_back": float(np.median(b2b)) if b2b else -1.0,
            "n_back_to_back": len(b2b),
            "post_release_tail": post_release_tail(records, windows),
            "p1_yields_fired": yields_fired,
            "_wall_s": round(_time.perf_counter() - t0, 1),
        }
        sys.stderr.write("  %s/%s done in %.1fs (median first-fed fill %.2f, b2b n=%d)\n" % (
            session, name, out["configs"][name]["_wall_s"],
            out["configs"][name]["first_fed_fill_median"], len(b2b)))
        sys.stderr.flush()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=[
        "session_20260804_201020", "session_20260804_202151"])
    ap.add_argument("--window", type=int, default=RG.GATE_WINDOW)
    ap.add_argument("--census-only", action="store_true",
                    help="only the unarmed pass + the 2-vs-3-step off-shot admission census")
    ap.add_argument("--bridge-gap", type=int, default=BRIDGE_GAP,
                    help="frames between spans below which ONE hw window bridges them "
                         "(0 = per-press windows with shot-start relays)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    out = {}
    for s in args.sessions:
        t0 = _time.perf_counter()
        out[s] = measure(s, args.window, census_only=args.census_only,
                         bridge_gap=args.bridge_gap)
        out[s]["_wall_s"] = round(_time.perf_counter() - t0, 1)
        if args.out:
            with open(args.out, "w") as f:
                json.dump(out, f, indent=2)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
