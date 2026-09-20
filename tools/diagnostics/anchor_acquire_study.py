#!/usr/bin/env python
"""anchor_acquire_study.py -- WHY does player_anchor fail to pick up the owner's plate?

[ORION_ANCHOR_ACQUIRE 2026-09-17] The 09-17 night sessions logged ``anchor=1`` on 2 of 360
pickup lines, so every relaxed armed floor (8 px / 6 px), the range classifier and the pose
lock had nothing to key on.  The plate ``[3][PS][trimuzis]`` is plainly visible on those
frames, so the failure is in ACQUISITION, not in the corpus.  This tool measures the
acquisition itself, per press, against a ground truth that does not come from the anchor:

    ORACLE PLATE   a wide multi-scale PS-disc sweep (0.45..1.20, tight template) in the
                   window the LIVE meter box predicts, confirmed by the gamertag crop to
                   its right.  The live box comes from frames.csv, which the production
                   detector wrote -- it is not this module's opinion.
    SHIPPED ARM    player_anchor.PlayerAnchor replayed frame by frame on the dump's own
                   clock with ARM armed, exactly as the locator drives it live.

and reports, per press: was the plate found, how long after the press, the correlation peak
and the scale it peaked at, the coarse-peak rank of the true plate, whether the gamertag
matched -- plus the failure-reason histogram:

    no_plate        the oracle finds no plate in the window at all (off-screen / not drawn)
    scale           the plate IS there, but its size is outside PlayerAnchor.SCALES_ACQ
    below_ps_min    the right scale is swept and the peak is still under ORION_ANCHOR_PS_MIN
    not_in_topk     the plate's coarse peak is not among the ones the acquisition refines
    identity        a plate was locked, but not the owner's
    rate_limited    ORION_ANCHOR_ACQ_MS never let an acquisition run inside the window

Usage:
  py tools/diagnostics/anchor_acquire_study.py --session session_20260917_030758
  py tools/diagnostics/anchor_acquire_study.py --session 0912 --json out.json
  py tools/diagnostics/anchor_acquire_study.py --session all --arm on|off

Read-only.  Writes nothing unless --json is given; never touches the running app.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

import cv2                        # noqa: E402
import numpy as np                # noqa: E402

FRAMEDUMP = os.environ.get("ORION_STUDY_FRAMEDUMP", r"D:\NexusVision\framedump")
RECORDS = os.environ.get("ORION_STUDY_RECORDS", r"D:\NexusVision\shot_records")
DUMP_0912 = os.path.join(FRAMEDUMP, "session_20260912_201355")
PRESSES_0912 = os.path.join(_REPO, "logs", "diagnostics", "farshot_study", "presses.csv")

# The oracle sweeps far wider than the shipped acquisition: the plate is a PERSPECTIVE
# element, not a fixed-size billboard, so its disc runs ~11 px (a far corner three) to
# ~30 px (a close-range drive) on the same court.
WIDE_SCALES = tuple(round(0.45 + 0.05 * i, 3) for i in range(16))     # 0.45 .. 1.20


# --------------------------------------------------------------------------- #
#  corpus
# --------------------------------------------------------------------------- #
def _frames_csv(path: str) -> List[dict]:
    rows = []
    with open(path, newline="") as f:
        for r in csv.DictReader(f):
            try:
                rows.append(dict(idx=int(r["idx"]), t=float(r["t_wall"]),
                                 det=int(r["detected"]), fill=float(r["fill_pct"]),
                                 conf=float(r.get("conf") or 0.0),
                                 box=(int(r["bbox_x"]), int(r["bbox_y"]),
                                      int(r["bbox_w"]), int(r["bbox_h"]))))
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def index_press_dump(root: str = FRAMEDUMP) -> Dict[tuple, list]:
    """-> {(epoch, idx): [file records]} for the 09-17 press-window dumps.

    Sessions 030402 and 030758 share this directory and their idx ranges COLLIDE, so a
    key can hold two files.  The jpg mtime equals the frame's t_wall, which is what
    ``press_windows`` uses to give each file back to the session that wrote it.
    """
    by_idx: Dict[int, List[dict]] = {}
    for r in _frames_csv(os.path.join(root, "frames.csv")):
        by_idx.setdefault(r["idx"], []).append(r)
    out: Dict[tuple, list] = {}
    pat = re.compile(r"ep(\d+)_f(\d+)_(\d)_raw\.jpg$")
    for name in os.listdir(root):
        m = pat.match(name)
        if not m:
            continue
        ep, idx, dflag = int(m.group(1)), int(m.group(2)), int(m.group(3))
        path = os.path.join(root, name)
        mt = os.path.getmtime(path)
        cands = [r for r in by_idx.get(idx, ()) if r["det"] == dflag] or by_idx.get(idx, [])
        best = min(cands, key=lambda r: abs(r["t"] - mt)) if cands else None
        out.setdefault((ep, idx), []).append(dict(
            path=path, mtime=mt,
            t=(best["t"] if best else mt), det=(best["det"] if best else dflag),
            fill=(best["fill"] if best else -1.0), conf=(best["conf"] if best else 0.0),
            box=(best["box"] if best else (0, 0, 0, 0))))
    return out


def load_records(session: str) -> List[dict]:
    out = []
    with open(os.path.join(RECORDS, session + ".jsonl"), "r",
              encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    out.sort(key=lambda d: d.get("seq") or 0)
    return out


def press_windows(session: str, files: Dict[tuple, list], tol_s: float = 6.0) -> List[dict]:
    # [2026-09-19] The dump's own file names carry the epoch, so a record whose
    # `framedump` block never got attached (the recorder closed it before the dump window
    # ended -- 17/18 of session_20260917_142445, 19/36 of session_20260918_135725) is
    # still measurable: its frames are exactly the ep<N>_* files on disk. Without this
    # fallback four of the five 09-17/09-18 dumps score on a handful of presses.
    by_ep: Dict[int, List[int]] = {}
    for (ep, idx) in files:
        by_ep.setdefault(ep, []).append(idx)
    out = []
    for rec in load_records(session):
        fd = rec.get("framedump") or {}
        lo, hi = fd.get("first_idx"), fd.get("last_idx")
        press_t = float(rec["press_ts_ms"]) / 1000.0
        ep = int(rec["epoch"])
        if lo is None or hi is None:
            own = by_ep.get(ep) or []
            if not own:
                continue
            lo, hi = min(own), max(own)
        frs = []
        for idx in range(int(lo), int(hi) + 1):
            for f in files.get((ep, idx), ()):
                if not (press_t - tol_s <= f["mtime"] <= press_t + tol_s):
                    continue
                frs.append(dict(idx=idx, path=f["path"], t=f["t"], det=f["det"],
                                fill=f["fill"], conf=f["conf"], box=f["box"],
                                ms=(f["t"] - press_t) * 1000.0))
        frs.sort(key=lambda r: r["t"])
        out.append(dict(session=session, seq=rec.get("seq"), epoch=ep, press_t=press_t,
                        shot_type=rec.get("shot_type") or "",
                        banner=((rec.get("banner") or {}).get("timing")),
                        frames=frs))
    return [s for s in out if s["frames"]]


def press_windows_0912(max_shots: int = 0) -> List[dict]:
    """The 09-12 continuous dump, cut into press windows by its own press table."""
    rows = {r["idx"]: r for r in _frames_csv(os.path.join(DUMP_0912, "frames.csv"))}
    names = {}
    for name in os.listdir(DUMP_0912):
        m = re.match(r"f(\d+)_(\d)_raw\.png$", name)
        if m:
            names[int(m.group(1))] = os.path.join(DUMP_0912, name)
    out = []
    with open(PRESSES_0912, newline="") as f:
        for r in csv.DictReader(f):
            try:
                press_t = float(r["press_wall"])
                lo, hi = int(r["first_idx"]), int(r["last_idx"])
            except (KeyError, TypeError, ValueError):
                continue
            frs = []
            # the press table's own window is only ~10 frames; widen it to the same
            # -200..+1100 ms the 09-17 press dumps carry so the two are comparable
            for idx in range(lo - 4, hi + 9):
                p = names.get(idx)
                rr = rows.get(idx)
                if p is None or rr is None:
                    continue
                frs.append(dict(idx=idx, path=p, t=rr["t"], det=rr["det"],
                                fill=rr["fill"], conf=rr["conf"], box=rr["box"],
                                ms=(rr["t"] - press_t) * 1000.0))
            frs.sort(key=lambda x: x["t"])
            if frs:
                out.append(dict(session="session_20260912_201355", seq=int(r["epoch"]),
                                epoch=int(r["epoch"]), press_t=press_t,
                                shot_type=(r.get("shot_type") or "").strip(),
                                banner=None, frames=frs))
    if max_shots:
        out = out[:max_shots]
    return out


# --------------------------------------------------------------------------- #
#  templates
# --------------------------------------------------------------------------- #
def disc_template(crop: int = 0):
    """The shipped PS disc, optionally cropped to its inscribed square.

    The shipped 27x27 carries ~4 px of the 09-12 court's BLOND WOOD in every corner.
    TM_CCOEFF_NORMED does not forgive that on a dark blue practice court: the same plate
    that scores 0.92 with the wood cropped away scores 0.62 with it left in.
    """
    import player_anchor as pa
    t = pa.PlayerAnchor()._template()
    if t is None:
        raise SystemExit("PS template failed to decode")
    if crop and crop < t.shape[0]:
        o = (t.shape[0] - crop) // 2
        t = t[o:o + crop, o:o + crop].copy()
    return t


def _gray(img, x0, y0, x1, y1):
    H, W = img.shape[:2]
    x0 = int(max(0, min(W, x0))); x1 = int(max(0, min(W, x1)))
    y0 = int(max(0, min(H, y0))); y1 = int(max(0, min(H, y1)))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None, 0, 0
    return cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY), x0, y0


def _scaled(t, m):
    if abs(m - 1.0) < 1e-3:
        return t
    o = cv2.resize(t, None, fx=m, fy=m, interpolation=cv2.INTER_AREA)
    return None if (o.shape[0] < 5 or o.shape[1] < 5) else o


def sweep(img, tmpl, cx, cy, pad_x, pad_y, scales) -> Optional[dict]:
    """Best multi-scale disc peak in a window. -> {ps, x, y, scale} or None."""
    best = None
    for m in scales:
        t = _scaled(tmpl, m)
        if t is None:
            continue
        th, tw = t.shape[:2]
        reg, rx, ry = _gray(img, cx - pad_x - tw, cy - pad_y - th,
                            cx + pad_x + tw, cy + pad_y + th)
        if reg is None or reg.shape[0] <= th or reg.shape[1] <= tw:
            continue
        _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED))
        if best is None or mx > best["ps"]:
            best = dict(ps=float(mx), x=rx + loc[0] + tw * 0.5,
                        y=ry + loc[1] + th * 0.5, scale=float(m))
    return best


def band_peaks(img, tmpl, scales, topk=8, y0f=0.35, y1f=0.99) -> List[dict]:
    """Top-k disc peaks over the whole nameplate band, best-over-scale at each spot."""
    H, W = img.shape[:2]
    by0, by1 = int(y0f * H), int(y1f * H)
    band = cv2.cvtColor(img[by0:by1], cv2.COLOR_BGR2GRAY)
    acc = None
    accs = None
    for m in scales:
        t = _scaled(tmpl, m)
        if t is None or t.shape[0] >= band.shape[0] or t.shape[1] >= band.shape[1]:
            continue
        th, tw = t.shape[:2]
        r = cv2.matchTemplate(band, t, cv2.TM_CCOEFF_NORMED)
        full = np.full(band.shape, -9.0, np.float32)
        full[th // 2:th // 2 + r.shape[0], tw // 2:tw // 2 + r.shape[1]] = r
        if acc is None:
            acc = full
            accs = np.full(band.shape, m, np.float32)
        else:
            better = full > acc
            acc = np.where(better, full, acc)
            accs = np.where(better, m, accs)
    if acc is None:
        return []
    out = []
    work = acc.copy()
    for _ in range(topk):
        _, mx, _, loc = cv2.minMaxLoc(work)
        if mx <= -8.0:
            break
        out.append(dict(ps=float(mx), x=float(loc[0]), y=float(loc[1] + by0),
                        scale=float(accs[loc[1], loc[0]])))
        cv2.rectangle(work, (loc[0] - 14, loc[1] - 12), (loc[0] + 14, loc[1] + 12), -9.0, -1)
    return out


def tag_crop(img, ix, iy, sc, shape=(26, 92)):
    """The gamertag immediately right of a disc at (ix, iy), normalised to `shape`."""
    H, W = img.shape[:2]
    th, tw = shape
    x0 = int(round(ix + 27.0 * sc * 0.5 + 1)); y0 = int(round(iy - (th * 0.5) * sc))
    x1 = int(round(x0 + tw * sc)); y1 = int(round(y0 + th * sc))
    if x0 < 0 or y0 < 0 or x1 > W or y1 > H or x1 - x0 < 16 or y1 - y0 < 8:
        return None
    g = cv2.cvtColor(img[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    if float(g.std()) < 12.0:
        return None
    return cv2.resize(g, (tw, th), interpolation=cv2.INTER_AREA)


def tag_score(img, ix, iy, sc, tag) -> float:
    if tag is None:
        return -9.0
    c = tag_crop(img, ix, iy, sc)
    if c is None:
        return -9.0
    return float(cv2.matchTemplate(c, tag, cv2.TM_CCOEFF_NORMED).max())


# --------------------------------------------------------------------------- #
#  the oracle: the plate the LIVE meter box confirms
# --------------------------------------------------------------------------- #
def plausible_box(fr, s=1.0) -> bool:
    b = fr["box"]
    return bool(fr["det"] and 20 * s <= b[2] <= 42 * s and 88 * s <= b[3] <= 155 * s
                and fr["fill"] > 8.0)


def oracle_plate(img, box, tmpl, ps_min=0.62, scales=WIDE_SCALES) -> Optional[dict]:
    """The owner's plate, found where a CONFIRMED meter box says it must be.

    The y prior is deliberately loose (+20..+260 px below the box bottom): the icon->box
    offset is NOT a constant, it grows with the plate's own scale (the meter is a
    fixed-size HUD panel; the plate rides at the player's feet).
    """
    bx, by, bw, bh = box[:4]
    cx = bx + bw * 0.5 - 22.0
    cy = by + bh + 140.0
    hit = sweep(img, tmpl, cx, cy, 78.0, 128.0, scales)
    if hit is None or hit["ps"] < ps_min:
        return None
    return hit


def oracle_track(frames: List[dict], tmpl, args):
    """Where the OWNER's plate is on every frame of a press window.

    Seeded, not searched: the seed is the plate a CONFIRMED meter box points at (that box
    is the production detector's, not this module's), and the track is then propagated
    backwards and forwards from the seed with a local wide-scale sweep.  Propagating from
    a confirmed seed is what makes the press-time position trustworthy -- at the press the
    meter does not exist yet, so nothing else can vouch for a plate there.
    """
    seed = None
    seed_i = -1
    for i, fr in enumerate(frames):
        if not plausible_box(fr):
            continue
        img = cv2.imread(fr["path"])
        if img is None:
            continue
        hit = oracle_plate(img, fr["box"], tmpl, args.oracle_ps)
        if hit is None:
            continue
        b = fr["box"]
        seed = dict(hit, ms=fr["ms"], idx=fr["idx"], path=fr["path"],
                    dx=hit["x"] - (b[0] + b[2] * 0.5), dy=hit["y"] - (b[1] + b[3]))
        seed_i = i
        break
    if seed is None:
        return {}, None
    track = {seed["idx"]: seed}
    for step in (-1, 1):
        x, y, sc = seed["x"], seed["y"], seed["scale"]
        i = seed_i + step
        misses = 0
        while 0 <= i < len(frames):
            fr = frames[i]
            img = cv2.imread(fr["path"])
            if img is None:
                break
            near = tuple(m for m in WIDE_SCALES if abs(m - sc) <= 0.16) or WIDE_SCALES
            hit = sweep(img, tmpl, x, y, 26.0, 22.0, near)
            if hit is None or hit["ps"] < args.track_ps:
                misses += 1
                if misses > args.track_gap:
                    break
            else:
                misses = 0
                x, y, sc = hit["x"], hit["y"], hit["scale"]
                track[fr["idx"]] = dict(hit, ms=fr["ms"], idx=fr["idx"], path=fr["path"],
                                        dx=0.0, dy=0.0)
            i += step
    return track, seed


# --------------------------------------------------------------------------- #
#  per-press measurement
# --------------------------------------------------------------------------- #
def measure_session(shots: List[dict], tmpl, args) -> List[dict]:
    """Replay a whole session through ONE anchor, the way the sidecar runs it.

    The anchor is a process singleton live: it keeps the owner's gamertag and the
    per-session offsets across presses, and the locator teaches it on every confirmed
    meter (meter_locator_cv gate 10 -> ANCHOR.note_meter).  Measuring it with a fresh
    instance per press would score a cold start that never happens after shot one.
    """
    import player_anchor as pa
    pa.reset_all(keep_identity=False)
    A = pa.ANCHOR
    rows = []
    for sh in shots:
        A.reset(keep_identity=True)
        rows.append(measure_press(sh, tmpl, args, anchor=A))
    return rows


def measure_press(sh: dict, tmpl, args, anchor=None) -> dict:
    """-> one row: oracle truth + the shipped arm's behaviour on this press."""
    import player_anchor as pa

    anchor_mod = pa
    A = anchor if anchor is not None else pa.PlayerAnchor()
    pa.ARM.reset()
    pa.ARM.note_press(sh["epoch"], sh["frames"][0]["t"], sh["shot_type"])

    row = dict(session=sh["session"], seq=sh["seq"], epoch=sh["epoch"],
               shot_type=sh["shot_type"], banner=sh["banner"], n_frames=len(sh["frames"]),
               oracle_ms=None, oracle_ps=None, oracle_scale=None, oracle_x=None,
               oracle_y=None, oracle_tx=None, oracle_rank=None, oracle_n=0,
               ship_ms=None, ship_ps=None, ship_scale=None, ship_ok=None,
               ship_conf=None, ship_dist=None, ship_scale_ps=None, ship_first_ms=None,
               reason=None, dx=None, dy=None, oracle_first_ms=None,
               oracle_early=0)

    # ---- 1) ORACLE TRACK: where the owner's plate is on EVERY frame ------------
    track, seed = oracle_track(sh["frames"], tmpl, args)
    # A seed that will not PROPAGATE is not a plate: a lone 0.62 peak at the ladder's floor
    # is a court texture that happened to sit where the box predicts. Ground truth has to
    # survive its own neighbours or the failure histogram is measuring noise.
    if len(track) < args.track_min:
        track, seed = {}, None
    row["oracle_n"] = len(track)
    if seed is not None:
        row.update(oracle_ms=round(seed["ms"], 1), oracle_ps=round(seed["ps"], 3),
                   oracle_scale=seed["scale"], oracle_x=round(seed["x"], 1),
                   oracle_y=round(seed["y"], 1),
                   dx=round(seed["dx"], 1), dy=round(seed["dy"], 1))
        early = [t for i, t in track.items() if t["ms"] <= args.lock_ms]
        row["oracle_first_ms"] = round(min(t["ms"] for t in track.values()), 1)
        row["oracle_early"] = len(early)

    def near_oracle(a, fr):
        """Is this anchor on the OWNER's plate ON THIS FRAME? None = no ground truth."""
        o = track.get(fr["idx"])
        if o is None:
            return None, None
        d = math.hypot(a.icon_x - o["x"], a.icon_y - o["y"])
        return d, bool(d <= args.lock_px)

    # ---- 2) the shipped arm, frame by frame on the dump's own clock -----------
    #  The whole window is replayed, not just the scoring window: the locator teaches the
    #  anchor on every confirmed meter (note_meter), and that teaching is what the NEXT
    #  press's acquisition depends on.  Only the first CORRECT lock inside --window-ms is
    #  scored -- an anchor that opens on somebody else's plate and settles on the owner's
    #  20 ms later has done its job; one that never settles has not.
    first = None
    attempted = 0
    for fr in sh["frames"]:
        img = cv2.imread(fr["path"])
        if img is None:
            continue
        before = A.stats["acq"]
        a = A.update(img, ts=fr["t"], armed=True)
        if fr["ms"] <= args.window_ms:
            attempted += int(A.stats["acq"] > before)
            if a is not None:
                if row["ship_first_ms"] is None:
                    row["ship_first_ms"] = round(fr["ms"], 1)
                d, ok = near_oracle(a, fr)
                if first is None and ok:
                    first = (fr, a, d)
        # teach exactly what meter_locator_cv gate 10 teaches: a box the shipped gates
        # accepted with a confirmed green tip (conf >= 0.90) and a full-height judgement
        if plausible_box(fr) and fr["conf"] >= 0.90 and fr["fill"] >= 20.0:
            A.note_meter(img, fr["box"], a, True)
    if first is not None:
        fr, a, d = first
        row.update(ship_ms=round(fr["ms"], 1), ship_ps=round(a.ps, 3),
                   ship_scale=round(a.scale, 3), ship_conf=round(a.conf, 3),
                   ship_dist=round(d, 1), ship_ok=True)
    elif track:
        row["ship_ok"] = False

    # ---- 3) failure reason, against the LIVE configuration --------------------
    if row["ship_ok"]:
        row["reason"] = "ok"
    elif not track:
        row["reason"] = "no_plate"
    else:
        o = min(track.values(), key=lambda z: abs(z["ms"]))
        img = cv2.imread(o["path"])
        mt = A._match_tmpl()
        ps_min = A._ps_floor()
        cfg_sc = sweep(img, mt, o["x"], o["y"], 12.0, 12.0, A._scales())
        row["ship_scale_ps"] = round(cfg_sc["ps"], 3) if cfg_sc else None
        wide = sweep(img, mt, o["x"], o["y"], 12.0, 12.0, WIDE_SCALES)
        wide_ps = wide["ps"] if wide else -9.0
        if attempted == 0:
            row["reason"] = "rate_limited"
        elif cfg_sc is None or cfg_sc["ps"] < ps_min:
            row["reason"] = "scale" if wide_ps >= ps_min else "below_ps_min"
        else:
            # the right scale IS swept and clears the floor -> the coarse ranking or the
            # identity dropped it.  Rank the plate among the coarse peaks the live
            # acquisition would refine.
            rank = coarse_rank_live(img, A, o)
            row["oracle_rank"] = rank
            topk = int(float(os.environ.get("ORION_ANCHOR_COARSE_TOPK") or 8))
            row["reason"] = ("not_in_topk" if (rank is None or rank >= topk)
                             else ("identity" if A._tag is not None else "wrong_plate"))
    return row


def coarse_rank_live(img, A, o, topn=16) -> Optional[int]:
    """Rank of the oracle plate among the coarse peaks THIS anchor would refine."""
    H, W = img.shape[:2]
    s = H / 720.0
    by0, by1 = int(A.BAND_Y0_F * H), int(min(H, A.BAND_Y1_F * H))
    band = img[by0:by1]
    div = max(1, int(float(os.environ.get("ORION_ANCHOR_COARSE_DIV") or 2)))
    gb = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gs = gb if div == 1 else cv2.resize(gb, (gb.shape[1] // div, gb.shape[0] // div),
                                        interpolation=cv2.INTER_AREA)
    fx = gs.shape[1] / float(W)
    fy = gs.shape[0] / float(band.shape[0])
    flat = A._coarse_map(gs, A._match_tmpl(), s, fx, fy, by0, A._scales())
    if flat is None:
        return None
    sx = max(4, int(round(24.0 * s * fx)))
    sy = max(4, int(round(20.0 * s * fx)))
    for k in range(topn):
        _, mx, _, loc = cv2.minMaxLoc(flat)
        if mx <= -8.0:
            break
        px = loc[0] / fx
        py = loc[1] / fy + by0
        if math.hypot(px - o["x"], py - o["y"]) <= 34.0:
            return k
        cv2.rectangle(flat, (loc[0] - sx, loc[1] - sy), (loc[0] + sx, loc[1] + sy), -9.0, -1)
    return None


# --------------------------------------------------------------------------- #
def refusal_study(args) -> dict:
    """The 09-15 FALSE-LOCK number, re-measured: does a confident anchor still refuse the
    out-of-shot junk?

    09-15 (docs/HANDOFF_2026-09-14_UI_POLISH.md): "plate present at the meter offset: real
    shots 0.60-0.73 vs out-of-shot junk 0.01-0.09 (~10x separation) -> a confident anchor
    refuses 91-96 % of the false-lock population; 0 outside-patch accepts on all three
    corpora."  Junk here is the same population: a box the LIVE detector accepted on a
    frame more than --junk-gap-s away from any press in the session's press table.
    """
    import player_anchor as pa
    rows = {r["idx"]: r for r in _frames_csv(os.path.join(DUMP_0912, "frames.csv"))}
    names = {}
    for name in os.listdir(DUMP_0912):
        m = re.match(r"f(\d+)_(\d)_raw\.png$", name)
        if m:
            names[int(m.group(1))] = os.path.join(DUMP_0912, name)
    presses = []
    with open(PRESSES_0912, newline="") as f:
        for r in csv.DictReader(f):
            try:
                presses.append(float(r["press_wall"]))
            except (KeyError, TypeError, ValueError):
                pass
    presses.sort()
    order = sorted(rows.values(), key=lambda r: (r["t"], r["idx"]))
    if args.limit:
        order = order[:args.limit]
    pa.reset_all(keep_identity=False)
    A = pa.ANCHOR
    import bisect
    gap = args.junk_gap_s
    arm_s = 2.5
    out = dict(junk=0, junk_in_patch=0, junk_in_patch_conf=0,
               real=0, real_in_patch=0, frames=0, confident=0)
    for r in order:
        p = names.get(r["idx"])
        if p is None:
            continue
        i = bisect.bisect_left(presses, r["t"])
        near = min([abs(r["t"] - presses[j]) for j in (i - 1, i)
                    if 0 <= j < len(presses)] or [9e9])
        after = min([r["t"] - presses[j] for j in (i - 1, i)
                     if 0 <= j < len(presses) and presses[j] <= r["t"]] or [9e9])
        armed = 0.0 <= after <= arm_s
        img = cv2.imread(p)
        if img is None:
            continue
        out["frames"] += 1
        if armed:
            pa.ARM.note_press(1, r["t"], "")
        else:
            pa.ARM.note_release(0, r["t"])
        a = A.update(img, ts=r["t"], armed=True)
        if a is not None and pa.PlayerAnchor.refuse_ok(a):
            out["confident"] += 1
        if r["det"] and r["box"][2] > 0 and r["box"][3] > 0:
            inside = bool(a is not None and a.contains_box(*r["box"]))
            if near > gap:
                out["junk"] += 1
                out["junk_in_patch"] += int(inside)
                out["junk_in_patch_conf"] += int(inside and a is not None
                                                 and pa.PlayerAnchor.refuse_ok(a))
            elif armed:
                out["real"] += 1
                out["real_in_patch"] += int(inside)
        if plausible_box(r) and r["conf"] >= 0.90 and r["fill"] >= 20.0:
            A.note_meter(img, r["box"], a, True)
    out["junk_refused_pct"] = round(
        100.0 * (1.0 - out["junk_in_patch"] / max(1, out["junk"])), 1)
    out["junk_refused_conf_pct"] = round(
        100.0 * (1.0 - out["junk_in_patch_conf"] / max(1, out["junk"])), 1)
    out["real_in_patch_pct"] = round(100.0 * out["real_in_patch"] / max(1, out["real"]), 1)
    return out


def q(v, p):
    v = sorted(x for x in v if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return v[lo] if lo == hi else v[lo] + (v[hi] - v[lo]) * (k - lo)


def summarise(rows: List[dict], label: str, lock_ms: float) -> dict:
    n = len(rows)
    plate = [r for r in rows if r["oracle_n"]]
    early = [r for r in rows if r.get("oracle_early")]
    locked = [r for r in rows if r["ship_ok"]]
    fast = [r for r in locked if r["ship_ms"] is not None and r["ship_ms"] <= lock_ms]
    hist: Dict[str, int] = {}
    for r in rows:
        hist[r["reason"] or "?"] = hist.get(r["reason"] or "?", 0) + 1
    return dict(label=label, presses=n, plate_visible=len(plate),
                plate_at_press=len(early),
                locked=len(locked), locked_pct=round(100.0 * len(locked) / max(1, n), 1),
                locked_fast=len(fast),
                locked_fast_pct=round(100.0 * len(fast) / max(1, n), 1),
                lock_of_visible_pct=round(100.0 * len(locked) / max(1, len(plate)), 1),
                ship_ms_p50=q([r["ship_ms"] for r in locked], .5),
                lock_of_early_pct=round(100.0 * len([r for r in early if r["ship_ok"]])
                                        / max(1, len(early)), 1),
                fast_of_early_pct=round(100.0 * len([r for r in early if r["ship_ok"]
                                        and r["ship_ms"] is not None
                                        and r["ship_ms"] <= lock_ms])
                                        / max(1, len(early)), 1),
                oracle_ps_p50=q([r["oracle_ps"] for r in plate], .5),
                oracle_scale_p10=q([r["oracle_scale"] for r in plate], .1),
                oracle_scale_p50=q([r["oracle_scale"] for r in plate], .5),
                oracle_scale_p90=q([r["oracle_scale"] for r in plate], .9),
                dy_p10=q([r["dy"] for r in plate], .1), dy_p50=q([r["dy"] for r in plate], .5),
                dy_p90=q([r["dy"] for r in plate], .9),
                dx_p50=q([r["dx"] for r in plate], .5),
                reasons=hist)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", default="all",
                    help="session_20260917_030402 | session_20260917_030758 | 0912 | all")
    ap.add_argument("--window-ms", type=float, default=600.0,
                    help="how far past the press the shipped arm is given to lock")
    ap.add_argument("--lock-ms", type=float, default=150.0,
                    help="the 'fast enough' bar reported alongside the raw lock rate")
    ap.add_argument("--lock-px", type=float, default=40.0)
    ap.add_argument("--oracle-ps", type=float, default=0.68)
    ap.add_argument("--oracle-crop", type=int, default=19)
    ap.add_argument("--track-ps", type=float, default=0.58,
                    help="oracle-track propagation floor (wide ladder, local window)")
    ap.add_argument("--track-gap", type=int, default=3,
                    help="consecutive missed frames before the oracle track stops")
    ap.add_argument("--track-min", type=int, default=5,
                    help="a track shorter than this is a spurious seed, not a plate")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--refusal", action="store_true",
                    help="re-measure the 09-15 false-lock refusal instead of the lock rate")
    ap.add_argument("--junk-gap-s", type=float, default=2.5)
    ap.add_argument("--json", default=None)
    a = ap.parse_args()

    if a.refusal:
        r = refusal_study(a)
        print(json.dumps(r, indent=1, sort_keys=True))
        print("\njunk boxes %d  in a patch %d  in a CONFIDENT patch %d"
              % (r["junk"], r["junk_in_patch"], r["junk_in_patch_conf"]))
        print("refused %.1f %% (confident-only %.1f %%)   real in-patch %.1f %%"
              % (r["junk_refused_pct"], r["junk_refused_conf_pct"], r["real_in_patch_pct"]))
        if a.json:
            with open(a.json, "w") as f:
                json.dump(r, f, indent=1)
        return

    sessions = ([s for s in a.session.split(",") if s] if a.session != "all"
                else ["session_20260917_030402", "session_20260917_030758", "0912"])
    tmpl = disc_template(a.oracle_crop)
    files = None
    out = []
    for sess in sessions:
        if sess == "0912":
            shots = press_windows_0912()
        else:
            # [2026-09-19] The 09-17/09-18 press dumps live in a PER-SESSION directory
            # (framedump/<session>/frames.csv + ep*_f*_0_raw.jpg); the 09-17 03:xx pair
            # shared the framedump root. Index whichever this session actually wrote.
            sdir = os.path.join(FRAMEDUMP, sess)
            if os.path.isfile(os.path.join(sdir, "frames.csv")):
                shots = press_windows(sess, index_press_dump(sdir))
            else:
                if files is None:
                    files = index_press_dump()
                shots = press_windows(sess, files)
        if a.limit:
            shots = shots[:a.limit]
        rows = measure_session(shots, tmpl, a)
        s = summarise(rows, sess, a.lock_ms)
        s["rows"] = rows
        out.append(s)
        print("\n=== %s  presses=%d  plate_visible=%d  plate_at_press=%d  "
              "locked=%d (%.1f%%)  within %.0f ms=%d (%.1f%%)"
              % (sess, s["presses"], s["plate_visible"], s["plate_at_press"],
                 s["locked"], s["locked_pct"], a.lock_ms, s["locked_fast"],
                 s["locked_fast_pct"]), flush=True)
        print("    of the presses whose plate IS on screen within %.0f ms of the press: "
              "locked %.1f %%, locked in time %.1f %%"
              % (a.lock_ms, s["lock_of_early_pct"], s["fast_of_early_pct"]))
        print("    oracle ps p50=%s  scale p10/p50/p90=%s/%s/%s  dx p50=%s  dy p10/p50/p90=%s/%s/%s"
              % (s["oracle_ps_p50"], s["oracle_scale_p10"], s["oracle_scale_p50"],
                 s["oracle_scale_p90"], s["dx_p50"], s["dy_p10"], s["dy_p50"], s["dy_p90"]))
        print("    reasons: %s" % json.dumps(s["reasons"], sort_keys=True))
    if a.json:
        with open(a.json, "w") as f:
            json.dump(out, f, indent=1)
        print("\nwrote", a.json)


if __name__ == "__main__":
    main()
