#!/usr/bin/env python3
"""Replay real captured frames (or a frame dir) through meter_detector.MeterDetector
and report, per detection episode, WHERE the live timing feed is won or lost.

This is the measurement tool behind the detector-freshness work. It reconstructs the
detector's temporal state by feeding frames in order, then segments the run into
"episodes" (a shot's meter lifetime) and reports the metric that actually drives the
native timing engine:

    fed%  = fraction of episode frames the engine would treat as a FRESH live sample
            (orchestrator._should_feed_engine: detected AND bbox valid AND
             rejection_reason in {'', 'green_not_found'}). Everything else
             (meter_memory echo, bbox_unstable, roi_not_found) is stale_or_memory on
             the native side and earns no timing trust.

A fade/Go-To whose fed% is low is exactly the "fresh=0 staleMem=high" shot in the
native "Release detsummary:" line — the engine is flying open-loop on the learned
clock instead of a live trajectory.

Usage:
    python tools/diagnostics/replay_detector.py --frames logs/diagnostics/framedump
    python tools/diagnostics/replay_detector.py --frames <dir> --color Purple --style Arrow2

Frames are sorted by the integer in their filename (f00000_0_raw.png -> 0). Pass a glob
with --glob to match a different naming scheme.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from typing import Dict, List, Optional

# Repo root so `import meter_detector` works no matter where this is run from.
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from meter_detector import MeterDetector, load_detector_config  # noqa: E402

# Same definition the orchestrator's feed gate uses (remote_play_orchestrator
# ._should_feed_engine): only these reach the engine as a fresh live sample.
FED_REASONS = {"", "green_not_found"}


def _frame_index(path: str) -> int:
    m = re.search(r"(\d+)", os.path.basename(path))
    return int(m.group(1)) if m else 0


def _load_frames(frames_dir: str, pattern: str, limit: int) -> List[str]:
    paths = sorted(glob.glob(os.path.join(frames_dir, pattern)), key=_frame_index)
    if limit > 0:
        paths = paths[:limit]
    return paths


def _new_detector(color: str, style: Optional[str]) -> MeterDetector:
    cfg = load_detector_config()
    cfg.meter_color = color
    styles_dir = os.path.join(_REPO, "meter_styles")
    det = MeterDetector(styles_dir, cfg)
    if style:
        det.set_active_style(style)
    return det


class _FrameRec:
    __slots__ = ("i", "det", "fill", "conf", "cx", "cy", "w", "h", "rej",
                 "gc", "zone", "event", "fed", "mem", "sig")

    def __init__(self, i, res, dbg, sig=None):
        self.i = i
        self.det = bool(res.detected)
        self.fill = float(res.fill_pct)
        self.conf = float(res.confidence)
        bx, by, bw, bh = res.bbox
        self.w, self.h = int(bw), int(bh)
        self.cx = float(bx) + bw * 0.5 if bw > 0 else -1.0
        self.cy = float(by) + bh * 0.5 if bh > 0 else -1.0
        self.rej = str(res.rejection_reason or "")
        self.gc = float(res.green_window_center_pct)
        self.zone = str(dbg.get("zone", ""))
        self.event = str(dbg.get("stab_event", ""))
        self.mem = int(dbg.get("mem_left", 0))
        self.fed = bool(self.det and bw > 0 and bh > 0 and self.rej in FED_REASONS)
        # 8x8 gray signature for cheap duplicate detection (the ~30fps export cap
        # emits byte-identical frames; a "missed" frame that is a dup of a fed
        # neighbour costs the engine nothing — it already has that sample fresh).
        self.sig = sig


def _frame_sig(frame) -> "np.ndarray":
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return cv2.resize(g, (8, 8), interpolation=cv2.INTER_AREA).astype(np.float32).flatten()


def _sig_dup(a, b, tol: float = 2.5) -> bool:
    if a is None or b is None:
        return False
    return float(np.mean(np.abs(a - b))) < tol


def replay(frame_paths: List[str], color: str, style: Optional[str]) -> List[_FrameRec]:
    det = _new_detector(color, style)
    recs: List[_FrameRec] = []
    for p in frame_paths:
        frame = cv2.imread(p)
        if frame is None:
            continue
        res = det.detect(frame)
        recs.append(_FrameRec(_frame_index(p), res, det.last_debug or {}, _frame_sig(frame)))
    return recs


def _episodes(recs: List[_FrameRec], gap: int = 6) -> List[List[_FrameRec]]:
    """Split into episodes separated by >=gap consecutive fully-absent frames
    (no detection and no memory echo). Each episode ~ one shot's meter lifetime."""
    eps: List[List[_FrameRec]] = []
    cur: List[_FrameRec] = []
    absent = 0
    for r in recs:
        present = r.det or r.rej == "meter_memory"
        if present:
            if absent >= gap and cur:
                eps.append(cur)
                cur = []
            absent = 0
            cur.append(r)
        else:
            absent += 1
            if cur and absent < gap:
                cur.append(r)
    if cur:
        eps.append(cur)
    return [e for e in eps if len(e) >= 3]


def _pct(n: int, d: int) -> float:
    return 100.0 * n / d if d else 0.0


def summarize(recs: List[_FrameRec]) -> None:
    n = len(recs)
    if n == 0:
        print("no frames decoded")
        return
    fed = sum(1 for r in recs if r.fed)
    det = sum(1 for r in recs if r.det)
    lc = [r for r in recs if r.zone == "locked_crop"]
    lc_miss = sum(1 for r in lc if not r.fed)
    print(f"\n=== OVERALL ({n} frames) ===")
    print(f"  detected:        {det:6} ({_pct(det,n):5.1f}%)")
    print(f"  fed (fresh):     {fed:6} ({_pct(fed,n):5.1f}%)   <- native fresh-accept equivalent")
    print(f"  locked_crop:     {len(lc):6}   miss/stale within it: {lc_miss} ({_pct(lc_miss,max(1,len(lc))):.0f}%)")
    zones: Dict[str, int] = {}
    for r in recs:
        zones[r.zone] = zones.get(r.zone, 0) + 1
    print("  zones:           " + ", ".join(f"{k}={v}" for k, v in sorted(zones.items(), key=lambda x: -x[1])))

    eps = _episodes(recs)
    print(f"\n=== {len(eps)} EPISODES (shot meter lifetimes) ===")
    print("  ep  frames  fed%   fresh   maxJump  fillMin..Max  green%  zonesUsed")
    fed_pcts = []
    for k, e in enumerate(eps):
        m = len(e)
        ef = sum(1 for r in e if r.fed)
        fed_pcts.append(_pct(ef, m))
        # max per-frame center jump on FED frames (bbox path continuity).
        fed_pts = [(r.cx, r.cy) for r in e if r.fed and r.cx >= 0]
        maxjump = 0.0
        for a, b in zip(fed_pts, fed_pts[1:]):
            maxjump = max(maxjump, float(np.hypot(b[0] - a[0], b[1] - a[1])))
        fills = [r.fill for r in e if r.fed]
        fmin = min(fills) if fills else 0.0
        fmax = max(fills) if fills else 0.0
        greenp = _pct(sum(1 for r in e if r.fed and r.gc >= 0), max(1, ef))
        ezones = "/".join(sorted({r.zone for r in e if r.zone}))
        flag = "  <-- STARVED" if _pct(ef, m) < 60 else ""
        print(f"  {k:2}  {m:5}   {_pct(ef,m):5.1f}  {ef:2}/{m:<3}  {maxjump:6.0f}   {fmin:5.1f}..{fmax:5.1f}  {greenp:5.0f}  {ezones}{flag}")
    if fed_pcts:
        fed_pcts.sort()
        med = fed_pcts[len(fed_pcts) // 2]
        print(f"\n  median episode fed%: {med:.1f}   episodes <60% fed: "
              f"{sum(1 for p in fed_pcts if p < 60)}/{len(fed_pcts)}")


def rise_report(recs: List[_FrameRec]) -> None:
    """Decompose the RISE-PHASE misses (frames from meter-appear up to peak fill, the
    timing-critical window) into the only three buckets that matter:

      recoverable  - a meter was on screen (a FED frame sits within +/-2 frames) yet THIS
                     frame was rejected. The real target: a gate threw away a usable read.
      dup          - a missed frame that is a pixel-duplicate of an adjacent fed frame
                     (export-cap echo). Harmless: the engine already holds that sample.
      isolated     - no fed frame within +/-2 (meter absent/occluded/not-yet-drawn).
                     Largely irreducible — you cannot detect what is not on screen.

    Only the `recoverable` bucket is worth chasing toward 100%; its dominant
    rejection_reason names the gate to fix.
    """
    eps = _episodes(recs)
    rise_total = rise_fed = 0
    miss_reason: Dict[str, int] = {}
    rec_reason: Dict[str, int] = {}       # reason breakdown WITHIN recoverable bucket
    rec_zone: Dict[str, int] = {}
    n_recoverable = n_dup = n_isolated = 0
    for e in eps:
        # Peak fill index: argmax over frames that carried a fill (fed or detected).
        best_i, best_fill = -1, -1.0
        for idx, r in enumerate(e):
            f = r.fill if (r.fed or r.det) else -1.0
            if f > best_fill:
                best_fill, best_i = f, idx
        if best_i < 0:
            continue
        for idx in range(best_i + 1):          # rise = episode start .. peak (inclusive)
            r = e[idx]
            rise_total += 1
            if r.fed:
                rise_fed += 1
                continue
            reason = r.rej or ("no_match" if not r.det else "accepted?")
            miss_reason[reason] = miss_reason.get(reason, 0) + 1
            lo, hi = max(0, idx - 2), min(len(e), idx + 3)
            bracket = [e[j] for j in range(lo, hi) if j != idx and e[j].fed]
            if not bracket:
                n_isolated += 1
                continue
            if any(_sig_dup(r.sig, b.sig) for b in bracket):
                n_dup += 1
                continue
            n_recoverable += 1
            rec_reason[reason] = rec_reason.get(reason, 0) + 1
            rec_zone[r.zone or "none"] = rec_zone.get(r.zone or "none", 0) + 1

    miss = rise_total - rise_fed
    print(f"\n=== RISE-PHASE FRESHNESS ({len(eps)} episodes) ===")
    print(f"  rise frames:     {rise_total:6}")
    print(f"  fed (fresh):     {rise_fed:6} ({_pct(rise_fed, rise_total):5.1f}%)")
    print(f"  missed:          {miss:6} ({_pct(miss, rise_total):5.1f}%)")
    if miss:
        print("  miss by reason:  " + ", ".join(f"{k}={v}" for k, v in
                                                 sorted(miss_reason.items(), key=lambda x: -x[1])))
        print(f"\n  --- miss decomposition ---")
        print(f"  recoverable:     {n_recoverable:6} ({_pct(n_recoverable, miss):5.1f}% of miss)  "
              f"<- the real target (meter on screen, gate rejected it)")
        print(f"  dup (harmless):  {n_dup:6} ({_pct(n_dup, miss):5.1f}% of miss)")
        print(f"  isolated/absent: {n_isolated:6} ({_pct(n_isolated, miss):5.1f}% of miss)")
        # Effective ceiling: fed + the harmless dups (engine is covered on those).
        covered = rise_fed + n_dup
        print(f"\n  effective fresh ceiling (fed + dups covered): "
              f"{_pct(covered, rise_total):.1f}%   "
              f"reachable if all recoverable fixed: {_pct(covered + n_recoverable, rise_total):.1f}%")
        if n_recoverable:
            print("  recoverable by reason: " + ", ".join(f"{k}={v}" for k, v in
                                                          sorted(rec_reason.items(), key=lambda x: -x[1])))
            print("  recoverable by zone:   " + ", ".join(f"{k}={v}" for k, v in
                                                          sorted(rec_zone.items(), key=lambda x: -x[1])))


def sim_memory_trust(recs: List[_FrameRec], max_frames: int = 1, slack: float = 2.0,
                     max_fill: float = 85.0) -> None:
    """Offline PREDICTION of the native memory-trust experiment (settings memory_trust_enabled).

    Applies the engine's promotion rule to the replay: a rise-phase meter_memory echo within
    <= max_frames captured frames of the last GENUINE fed accept (and whose held fill stays
    within the rise band, min_fed_fill - slack) is promoted to fresh-equivalent. Echoes do NOT
    advance the anchor (faithful to the native rule: lastFreshAcceptMs only moves on a genuine
    accept), so a run of echoes converts only the frame(s) inside the window after a real accept.

    Reports rise-fed% before -> after, how many frames convert, and how many land near the tip
    (>=90% fill) -- the EARLY-risk surface (a held value trusted as fresh right where the real
    meter is sweeping through the green window). This is a PREDICTION, not proof; confirm on a
    live batch via the memTrusted= field on the "Release freshness:" log line. max_frames=1 is
    the native ~45ms default at 22-30fps; run 2 as well to bracket the ms<->frame uncertainty.
    """
    eps = _episodes(recs)
    rise_total = rise_fed = converted = tip_blocked = 0
    buckets = {"<70": 0, "70-90": 0, ">=90 (tip-risk)": 0}
    for e in eps:
        best_i, best_fill = -1, -1.0
        for idx, r in enumerate(e):
            f = r.fill if (r.fed or r.det) else -1.0
            if f > best_fill:
                best_fill, best_i = f, idx
        if best_i < 0:
            continue
        last_gen_i: Optional[int] = None
        min_fed_fill = float("inf")
        for idx in range(best_i + 1):
            r = e[idx]
            rise_total += 1
            if r.fed:
                rise_fed += 1
                last_gen_i = r.i
                if r.fill > 0:
                    min_fed_fill = min(min_fed_fill, r.fill)
                continue
            if r.rej == "meter_memory" and last_gen_i is not None:
                gap = r.i - last_gen_i                       # captured frames since last genuine
                if 0 < gap <= max_frames and r.fill >= min_fed_fill - slack:
                    # TIP-GUARD (native memoryTrustMaxFillPct): never promote a held echo at/above
                    # max_fill -- near the green band a stale held value is the mistimed-release risk.
                    if r.fill >= max_fill:
                        tip_blocked += 1
                        continue
                    converted += 1
                    if r.fill >= 90.0:
                        buckets[">=90 (tip-risk)"] += 1
                    elif r.fill >= 70.0:
                        buckets["70-90"] += 1
                    else:
                        buckets["<70"] += 1
                    # echoes do NOT advance last_gen_i (the anchor stays on the genuine accept)
    before = _pct(rise_fed, rise_total)
    after = _pct(rise_fed + converted, rise_total)
    print(f"\n=== MEMORY-TRUST OFFLINE SIM (max_frames={max_frames}, slack={slack}%, tip-guard max_fill={max_fill:.0f}%) ===")
    print(f"  rise fed%:       {before:5.1f}%  ->  {after:5.1f}%   (+{converted} frames)")
    if converted or tip_blocked:
        print("  converted fill:  " + ", ".join(f"{k}={v}" for k, v in buckets.items()))
        print(f"  tip-guard blocked (>= {max_fill:.0f}% held, near green): {tip_blocked}"
              f"  <- the early-shot risk the guard removes")
    else:
        print("  no frames converted -> predicted live no-op (memTrusted would stay ~0)")


def main() -> int:
    ap = argparse.ArgumentParser(description="Replay frames through MeterDetector and report per-episode freshness")
    ap.add_argument("--frames", required=True, help="directory of frame images")
    ap.add_argument("--glob", default="*_raw.png", help="filename glob (default *_raw.png)")
    ap.add_argument("--color", default="Purple")
    ap.add_argument("--style", default="Arrow2")
    ap.add_argument("--limit", type=int, default=0, help="max frames (0 = all)")
    ap.add_argument("--episode-dump", type=int, default=-1, help="print per-frame trace of episode index N")
    ap.add_argument("--rise-report", action="store_true",
                    help="decompose rise-phase (appear->peak) misses into recoverable/dup/isolated")
    ap.add_argument("--sim-memory-trust", action="store_true",
                    help="offline-predict the native memory-trust gain on the rise phase")
    ap.add_argument("--mem-trust-max-frames", type=int, default=1,
                    help="memory-trust sim: max captured-frame gap to promote an echo (default 1)")
    ap.add_argument("--mem-trust-max-fill", type=float, default=85.0,
                    help="memory-trust tip-guard: never promote a held echo at/above this fill%% (default 85)")
    args = ap.parse_args()

    paths = _load_frames(args.frames, args.glob, args.limit)
    if not paths:
        print(f"no frames matched {args.glob} in {args.frames}")
        return 2
    print(f"replaying {len(paths)} frames from {args.frames} (color={args.color} style={args.style})")
    recs = replay(paths, args.color, args.style or None)
    summarize(recs)
    if args.rise_report:
        rise_report(recs)
    if args.sim_memory_trust:
        sim_memory_trust(recs, args.mem_trust_max_frames, max_fill=args.mem_trust_max_fill)

    if args.episode_dump >= 0:
        eps = _episodes(recs)
        if args.episode_dump < len(eps):
            print(f"\n--- episode {args.episode_dump} per-frame trace ---")
            print("   i  det fill  conf   cx   cy  rej             zone         event")
            for r in eps[args.episode_dump]:
                print(f"  {r.i:4} {int(r.det)}  {r.fill:5.1f} {r.conf:.2f} {r.cx:4.0f} {r.cy:4.0f} "
                      f"{r.rej:<15} {r.zone:<12} {r.event}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
