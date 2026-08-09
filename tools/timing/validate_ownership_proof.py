"""Offline-validate the pending-meter ownership proof variants on recorded frames.

    python tools/timing/validate_ownership_proof.py logs/diagnostics/detframes*.csv

RUN THIS BEFORE SHIPPING ANY CHANGE TO requiredSamples OR anchorRiseMinPct
(Candidate A of the 93ms-decision-budget work). It replays the episode state
machine of AutomationEngine::recordPendingMeterOwnershipSample() over real
per-frame reader output and answers, per variant:

  1. time-to-ownership on real shot episodes (and the paired saving vs the
     shipped 3-frame / 3.0pp proof),
  2. WHICH gate is binding at sample 2 -- the sample count or the rise proof.
     At the measured meter velocity (~5.4-5.8 ms/pp => ~3pp per 60fps frame)
     the rise sits exactly on the 3.0pp gate at sample 2, so a "2-frame proof
     with a stricter 4.0pp rise" can silently degenerate back into a 3-frame
     proof and recover nothing,
  3. false-lock admission: episodes each variant would grant that never later
     behave like a real meter (peak fill stays low).

The state machine mirrors AutomationEngine.cpp:3656-3932 (strict path):
  - an episode opens only on a genuine frame at/below anchorMaxFirstFillPct,
  - a repeated frame identity within the 100ms gap is ignored, not a break,
  - breaks: candidate/gap, >8pp drop, descent below first-2.5pp (re-anchor),
    identity stall past the gap, geometry discontinuity (IoU/scale/aspect),
  - grant = sampleCount >= N AND fill - first.fill >= R  (inputQualified is
    unconditionally true on the ButtonShot path, AutomationEngine.cpp:3605).

Fidelity limits (why this stays offline-only evidence): gestureStartMs /
physical-epoch / gameplay-structure proofs are not in the CSV, so every
genuine frame is treated as press-current. That is CONSERVATIVE for the
false-lock census (real epoch gating can only remove admissions, not add
them) and neutral for the timing deltas.
"""
import csv
import glob
import sys

import numpy as np

# AutomationEngine.cpp / AppConfig.h shipped values
ANCHOR_MAX_FIRST_FILL_PCT = 40.0
MAX_GAP_MS = 100.0                 # kPendingMeterOwnershipMaxGapMs
STALE_SPREAD_PCT = 2.5             # kStaleMeterMaxSpreadPct
SHARP_DROP_PP = 8.0
CONFIDENCE_GATE = 0.32
IOU_MIN = 0.10
MAX_DIM_SCALE = 1.50
MAX_ASPECT_SCALE = 1.25

# (name, requiredSamples, anchorRiseMinPct)
VARIANTS = [
    ("baseline 3f/3.0pp", 3, 3.0),
    ("cand-A  2f/3.0pp", 2, 3.0),
    ("cand-A' 2f/4.0pp", 2, 4.0),
    ("info    2f/2.6pp", 2, 2.6),  # 0.1pp above the 2.5pp static band -- info only
]

REAL_SHOT_PEAK_PCT = 45.0          # episode that later reaches this = real meter rise


def load(path):
    rows = []
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                row = {
                    "t": float(r.get("t_ms") or 0.0),
                    "detected": int(r.get("detected", 0) or 0),
                    "fed": int(r.get("fed", 1) or 0),
                    "fill": float(r.get("fill_pct") or 0.0),
                    "conf": float(r.get("confidence") or 0.0),
                    "x": int(float(r.get("x") or 0)),
                    "y": int(float(r.get("y") or 0)),
                    "w": int(float(r.get("w") or 0)),
                    "h": int(float(r.get("h") or 0)),
                    "rej": (r.get("rejection_reason") or "").strip(),
                    "frame_no": int(float(r.get("frame_no") or -1)),
                    "stale": int(r.get("stale", 0) or 0),
                }
            except (TypeError, ValueError):
                continue
            if row["t"] <= 0.0:
                continue
            rows.append(row)
    rows.sort(key=lambda p: p["t"])
    return rows


def genuine(row):
    """CSV projection of the genuineCurrent gate (press/epoch terms unavailable)."""
    return (
        row["detected"] == 1
        and row["fed"] == 1
        and row["rej"] in ("", "green_not_found")
        and row["stale"] == 0
        and row["conf"] >= CONFIDENCE_GATE
        and 0.0 < row["fill"] <= 100.0
        and row["w"] > 0
        and row["h"] > 0
    )


def geometry_continuous(a, b):
    left = max(b["x"], a["x"])
    top = max(b["y"], a["y"])
    right = min(b["x"] + b["w"], a["x"] + a["w"])
    bottom = min(b["y"] + b["h"], a["y"] + a["h"])
    inter = max(0, right - left) * max(0, bottom - top)
    union = b["w"] * b["h"] + a["w"] * a["h"] - inter
    if union <= 0:
        return False
    ws = b["w"] / a["w"]
    hs = b["h"] / a["h"]
    aspect = (b["w"] / b["h"]) / (a["w"] / a["h"])
    return (
        inter / union >= IOU_MIN
        and 1.0 / MAX_DIM_SCALE <= ws <= MAX_DIM_SCALE
        and 1.0 / MAX_DIM_SCALE <= hs <= MAX_DIM_SCALE
        and 1.0 / MAX_ASPECT_SCALE <= aspect <= MAX_ASPECT_SCALE
    )


def episodes(rows):
    """Replay the episode machine once; grants are evaluated per-variant later.

    Yields dicts with the genuine sample list of each episode (post identity /
    geometry / re-anchor rules), matching what sampleCount would count.
    """
    out = []
    cur = None  # {"samples": [row...], }

    def close():
        nonlocal cur
        if cur is not None and cur["samples"]:
            out.append(cur)
        cur = None

    for row in rows:
        if not genuine(row):
            # Non-genuine frames contribute nothing; blink-preservation vs
            # episode teardown both leave the sample list intact, and a long
            # outage is caught by the gap rule on the next genuine frame.
            continue
        if cur is None:
            if row["fill"] > ANCHOR_MAX_FIRST_FILL_PCT + 1e-6:
                continue  # high-fill first sight: censused, no episode
            cur = {"samples": [row]}
            continue
        last = cur["samples"][-1]
        first = cur["samples"][0]
        gap = row["t"] - last["t"]
        identity_advanced = row["frame_no"] > last["frame_no"] >= 0
        if not identity_advanced and gap <= MAX_GAP_MS:
            continue  # repeated payload: ignored, episode survives
        geom_ok = geometry_continuous(last, row)
        descended = row["fill"] < first["fill"] - STALE_SPREAD_PCT
        broken = (
            gap > MAX_GAP_MS
            or row["fill"] < last["fill"] - SHARP_DROP_PP
            or descended
            or not identity_advanced
            or not geom_ok
        )
        if broken:
            close()
            if row["fill"] <= ANCHOR_MAX_FIRST_FILL_PCT + 1e-6:
                cur = {"samples": [row]}
            continue
        cur["samples"].append(row)
    close()
    return out


def grant_time(ep, required_samples, rise_min_pct):
    """First sample index/time at which the proof passes, or None."""
    first_fill = ep["samples"][0]["fill"]
    for i, s in enumerate(ep["samples"]):
        n = i + 1
        if n >= required_samples and s["fill"] - first_fill >= rise_min_pct:
            return i, s["t"]
    return None, None


def main(patterns):
    paths = []
    for p in patterns:
        paths.extend(sorted(glob.glob(p)))
    if not paths:
        print("no input files matched")
        return

    all_eps = []
    for p in paths:
        eps = episodes(load(p))
        for e in eps:
            e["file"] = p
        all_eps.append((p, eps))

    shots, nonshots = [], []
    for _, eps in all_eps:
        for e in eps:
            peak = max(s["fill"] for s in e["samples"])
            (shots if peak >= REAL_SHOT_PEAK_PCT else nonshots).append(e)

    print("files: %d   episodes: %d   real-shot episodes (peak>=%.0f): %d   other: %d"
          % (len(paths), sum(len(e) for _, e in all_eps),
             REAL_SHOT_PEAK_PCT, len(shots), len(nonshots)))

    # detector cadence inside real shot episodes -- the worth of one dead frame
    gaps = [b["t"] - a["t"]
            for e in shots for a, b in zip(e["samples"], e["samples"][1:])]
    if gaps:
        g = np.array(gaps)
        print("intra-episode frame gap on shots: median %.1fms  p10 %.1f  p90 %.1f  (n=%d)"
              % (np.median(g), np.percentile(g, 10), np.percentile(g, 90), len(g)))

    # rise available at sample 2 -- which gate binds a 2-frame proof.
    # Segment by first-sight fill: episodes opening at [30,40] are the No-Dip /
    # budget-critical population (first seen past the 30% anchor, pickup section 3),
    # and the meter is SLOWER there, so the rise gate binds harder exactly where
    # the deadline pressure is.
    def rise2_report(label, eps):
        r = np.array([e["samples"][1]["fill"] - e["samples"][0]["fill"]
                      for e in eps if len(e["samples"]) >= 2])
        if not len(r):
            print("%s: none" % label)
            return
        print("%s (n=%d): rise@2 median %.2fpp  p10 %.2f  p90 %.2f;"
              " >=2.6: %.0f%%  >=3.0: %.0f%%  >=4.0: %.0f%%"
              % (label, len(r), np.median(r), np.percentile(r, 10), np.percentile(r, 90),
                 100.0 * (r >= 2.6).mean(), 100.0 * (r >= 3.0).mean(),
                 100.0 * (r >= 4.0).mean()))

    rise2_report("all shots", shots)
    rise2_report("budget-critical (first sight 30-40 fill)",
                 [e for e in shots if 30.0 <= e["samples"][0]["fill"] <= 40.0])
    rise2_report("early-sight (first sight < 30 fill)",
                 [e for e in shots if e["samples"][0]["fill"] < 30.0])
    print()

    base_name, base_n, base_r = VARIANTS[0]
    base_times = {}
    for e in shots:
        _, t = grant_time(e, base_n, base_r)
        base_times[id(e)] = t

    def variant_table(label, shot_eps):
        print("-- %s (n=%d) --" % (label, len(shot_eps)))
        print("%-20s %13s %14s %30s %11s %15s" % (
            "variant", "granted", "t->grant med",
            "saving vs baseline p10/med/mean/p90", "% @sample2", "non-shot grants"))
        for name, n_req, r_min in VARIANTS:
            times, savings = [], []
            granted = at2 = 0
            for e in shot_eps:
                i, t = grant_time(e, n_req, r_min)
                if t is None:
                    continue
                granted += 1
                at2 += 1 if i == 1 else 0
                times.append(t - e["samples"][0]["t"])
                bt = base_times[id(e)]
                if bt is not None:
                    savings.append(bt - t)
            fl = sum(1 for e in nonshots if grant_time(e, n_req, r_min)[1] is not None)
            t_med = np.median(times) if times else float("nan")
            if savings:
                s = np.array(savings)
                s_txt = "%.1f / %.1f / %.1f / %.1f ms" % (
                    np.percentile(s, 10), np.median(s), s.mean(), np.percentile(s, 90))
            else:
                s_txt = "-"
            print("%-20s %9d/%-3d %11.1fms %32s %10.0f%% %12d" % (
                name, granted, len(shot_eps), t_med, s_txt,
                100.0 * at2 / max(1, granted), fl))
        print()

    variant_table("all shots", shots)
    variant_table("budget-critical (first sight 30-40 fill)",
                  [e for e in shots if 30.0 <= e["samples"][0]["fill"] <= 40.0])

    print()
    print("Interpretation: if 'cand-A' 2f/4.0pp' saves ~0ms, the stricter rise gate")
    print("has re-created the 3rd-frame wait and the compensation defeats the fix.")
    print("Non-shot grants are a PROXY for false-lock admission (no press/epoch")
    print("context offline); compare variants against the baseline column, not zero.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["logs/diagnostics/detframes_2026080*.csv"])
