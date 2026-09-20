"""hud_3pt_icon.py -- does NBA 2K27's three-point possession icon mark the release?

Read-only diagnostic.  The competitor's "no meter / icon" mode is claimed to read the
black disc with a white bold "3" the game draws next to the ball handler's nameplate while
he holds the ball behind the arc.  This script measures, on the owner's own framedump,

  * where that icon is (it is NOT free-floating: it is the left cell of the ball handler's
    nameplate, immediately left of the PlayStation-logo disc and the gamertag),
  * when it appears and disappears relative to the owner's PRESS and to the RELEASE,
  * how tight that offset is across shots and shot types.

Anchor strategy (this is the part a live reader would reuse):
    1. multi-scale TM_CCOEFF_NORMED for the PS-logo disc (a constant, high-contrast,
       light disc with a dark glyph) over the lower 3/4 of the frame;
    2. confirm it is the OWNER's plate by correlating the gamertag text immediately to
       its right (other players in the Theater have nameplates too);
    3. read the "3" cell as a circle one disc-pitch (25 px at scale 1.0) to the LEFT of
       the PS disc centre: the icon is present iff that circle is mostly near-black with a
       bright glyph inside; absent it shows court wood (all bright).

Usage:
    .venv/Scripts/python.exe tools/diagnostics/hud_3pt_icon.py            # scan + summarise
    .venv/Scripts/python.exe tools/diagnostics/hud_3pt_icon.py --montage  # + montages
Inputs : logs/diagnostics/hud_landmark_study/shots_table.csv (hud_landmark_shots.py)
Outputs: logs/diagnostics/hud_landmark_study/icon_frames.csv, icon_shots.csv,
         tmpl_*.png, montage/*.png
"""
from __future__ import annotations

import argparse
import bisect
import csv
import os
import sys
from concurrent.futures import ProcessPoolExecutor

import cv2
import numpy as np

DUMP = r"D:\NexusVision\framedump\session_20260912_201355"
OUT = os.path.join("logs", "diagnostics", "hud_landmark_study")
# reference frame the templates are cut from (owner nameplate, icon present, clean wood bg)
REF_IDX = 1105
REF_PS = (940, 546, 27, 27)      # x, y, w, h of the PlayStation-logo disc
REF_TX = (968, 546, 92, 26)      # the gamertag text immediately right of it
PITCH = 25.0                      # px from the PS disc centre to the "3" disc centre, scale 1
SCALES = [0.70, 0.80, 0.90, 0.95, 1.00, 1.05, 1.15, 1.30]
Y0, Y1 = 150, 716                 # nameplate search band
PRE_S, POST_S = 1.5, 3.0          # window around the press

PS_MIN = 0.60                     # PS-disc correlation gate
TX_MIN = 0.35                     # gamertag correlation gate (confirms it is the owner)
DARK_T, BRIGHT_T = 75, 165


def frame_path(idx: int) -> str | None:
    for d in (0, 1):
        p = os.path.join(DUMP, "f%05d_%d_raw.png" % (idx, d))
        if os.path.exists(p):
            return p
    return None


_T = {}


def templates():
    if not _T:
        ref = cv2.imread(frame_path(REF_IDX))
        x, y, w, h = REF_PS
        _T["ps"] = cv2.cvtColor(ref[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
        x, y, w, h = REF_TX
        _T["tx"] = cv2.cvtColor(ref[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY).astype(np.float32)
    return _T


def probe(img):
    """-> dict(ps, tx, scale, cx, cy, dark, bright, present)"""
    T = templates()
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    band = g[Y0:Y1]
    best = None
    for s in SCALES:
        tp = cv2.resize(T["ps"], None, fx=s, fy=s) if s != 1.0 else T["ps"]
        tt = cv2.resize(T["tx"], None, fx=s, fy=s) if s != 1.0 else T["tx"]
        if tp.shape[0] >= band.shape[0] or tp.shape[1] >= band.shape[1]:
            continue
        r = cv2.matchTemplate(band, tp, cv2.TM_CCOEFF_NORMED)
        # top-3 peaks, 30 px apart
        flat = r.copy()
        for _ in range(3):
            m = float(flat.max())
            if m < PS_MIN * 0.6:
                break
            ly, lx = np.unravel_index(flat.argmax(), flat.shape)
            flat[max(0, ly - 20):ly + 20, max(0, lx - 30):lx + 30] = -9
            px, py = int(lx), int(ly) + Y0
            # gamertag immediately to the right
            x0, y0 = px + tp.shape[1] + 1, py
            reg = g[max(0, y0 - 6):y0 + tt.shape[0] + 6, max(0, x0 - 8):x0 + tt.shape[1] + 8]
            ts = -9.0
            if reg.shape[0] > tt.shape[0] and reg.shape[1] > tt.shape[1]:
                ts = float(cv2.matchTemplate(reg, tt, cv2.TM_CCOEFF_NORMED).max())
            score = m + ts
            if best is None or score > best[0]:
                best = (score, m, ts, s, px + tp.shape[1] / 2.0, py + tp.shape[0] / 2.0)
    if best is None:
        return dict(ps=-9.0, tx=-9.0, scale=0.0, cx=-1, cy=-1, dark=-1.0, bright=-1.0,
                    present=-1)
    _, ps, tx, s, cx, cy = best
    sx, sy = cx - PITCH * s, cy
    rr = max(4, int(round(9 * s)))
    y1, y2 = int(round(sy - rr)), int(round(sy + rr))
    x1, x2 = int(round(sx - rr)), int(round(sx + rr))
    dark = bright = -1.0
    present = -1
    if 0 <= x1 and 0 <= y1 and y2 <= img.shape[0] and x2 <= img.shape[1]:
        slot = cv2.cvtColor(img[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
        if slot.size:
            dark = float((slot < DARK_T).mean())
            bright = float((slot > BRIGHT_T).mean())
    if ps >= PS_MIN and tx >= TX_MIN and dark >= 0:
        present = 1 if (dark >= 0.28 and bright >= 0.12) else 0
    return dict(ps=round(ps, 3), tx=round(tx, 3), scale=s, cx=round(cx, 1), cy=round(cy, 1),
                dark=round(dark, 3), bright=round(bright, 3), present=present)


def _work(job):
    idx, = job
    p = frame_path(idx)
    if p is None:
        return idx, None
    img = cv2.imread(p)
    if img is None:
        return idx, None
    return idx, probe(img)


def read_frames():
    rows = []
    with open(os.path.join(DUMP, "frames.csv"), newline="") as f:
        for r in csv.DictReader(f):
            rows.append(dict(idx=int(r["idx"]), t=float(r["t_wall"]), det=int(r["detected"]),
                             fill=float(r["fill_pct"]), bx=int(r["bbox_x"]),
                             by=int(r["bbox_y"]), bw=int(r["bbox_w"]), bh=int(r["bbox_h"])))
    rows.sort(key=lambda r: r["t"])
    return rows


def read_shots():
    with open(os.path.join(OUT, "shots_table.csv"), newline="") as f:
        return list(csv.DictReader(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--montage", action="store_true")
    ap.add_argument("--resummarise", action="store_true",
                    help="skip the scan, re-read icon_frames.csv")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    if a.resummarise:
        rows = list(csv.DictReader(open(os.path.join(OUT, "icon_frames.csv"), newline="")))
        for r in rows:
            for k in ("dt_press_ms", "cx", "cy", "ps", "tx", "dark", "bright", "scale"):
                r[k] = float(r[k])
            for k in ("present", "idx", "meter_det", "meter_x", "meter_y"):
                r[k] = int(float(r[k]))
        summarise(rows, read_shots())
        if a.montage:
            montage(rows, read_shots())
        return
    T = templates()
    cv2.imwrite(os.path.join(OUT, "tmpl_ps.png"), cv2.resize(T["ps"], None, fx=6, fy=6,
                interpolation=cv2.INTER_NEAREST).astype(np.uint8))

    frames = read_frames()
    walls = [f["t"] for f in frames]
    shots = read_shots()

    need = set()
    per_shot = {}
    for s in shots:
        pt = float(s["press_wall"])
        i0 = bisect.bisect_left(walls, pt - PRE_S)
        i1 = bisect.bisect_right(walls, pt + POST_S)
        ids = [frames[i]["idx"] for i in range(i0, i1)]
        per_shot[s["epoch"]] = ids
        need.update(ids)
    need = sorted(need)
    print(f"{len(shots)} presses, {len(need)} distinct frames to scan", flush=True)

    res = {}
    with ProcessPoolExecutor(max_workers=a.workers) as ex:
        for n, (idx, r) in enumerate(ex.map(_work, [(i,) for i in need], chunksize=8)):
            if r:
                res[idx] = r
            if n % 500 == 0:
                print(f"  {n}/{len(need)}", flush=True)

    byidx = {f["idx"]: f for f in frames}
    rows = []
    for s in shots:
        pt = float(s["press_wall"])
        rel = float(s["release_wall"]) if s["release_wall"] else None
        for idx in per_shot[s["epoch"]]:
            r = res.get(idx)
            if not r:
                continue
            f = byidx[idx]
            rows.append(dict(epoch=s["epoch"], shot_type=s["shot_type"],
                             release_src=s["release_src"], hold_ms=s["hold_ms"],
                             banner=s["banner"], idx=idx,
                             dt_press_ms=round((f["t"] - pt) * 1000.0, 1),
                             dt_rel_ms=(round((f["t"] - rel) * 1000.0, 1) if rel else ""),
                             meter_det=f["det"], meter_fill=f["fill"],
                             meter_x=f["bx"], meter_y=f["by"], **r))
    p = os.path.join(OUT, "icon_frames.csv")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(p, len(rows), "rows")

    summarise(rows, shots)
    if a.montage:
        montage(rows, shots)


def _med(v):
    v = sorted(v)
    n = len(v)
    return 0.0 if not n else (v[n // 2] if n % 2 else 0.5 * (v[n // 2 - 1] + v[n // 2]))


def _rmad(v):
    m = _med(v)
    return _med([abs(x - m) for x in v])


def _sd(v):
    if len(v) < 2:
        return 0.0
    m = sum(v) / len(v)
    return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5


def summarise(rows, shots):
    by = {}
    for r in rows:
        by.setdefault(r["epoch"], []).append(r)
    out = []
    for s in shots:
        rs = sorted(by.get(s["epoch"], []), key=lambda r: r["dt_press_ms"])
        vis = [r for r in rs if r["present"] == 1]
        seen = [r for r in rs if r["present"] in (0, 1)]
        # OFF edge: the last ON frame in [-0.2 s, +2.5 s] whose NEXT readable frame is OFF
        last_on = first_off = None
        for i, r in enumerate(rs):
            if r["present"] != 1 or r["dt_press_ms"] > 2500:
                continue
            nxt = next((q for q in rs[i + 1:] if q["present"] in (0, 1)), None)
            if nxt is not None and nxt["present"] == 0:
                last_on, first_off = r, nxt
        # ON edge (the catch): the first OFF->ON transition in the window
        on_prev = on_first = None
        for i, r in enumerate(rs):
            if r["present"] != 0:
                continue
            nxt = next((q for q in rs[i + 1:] if q["present"] in (0, 1)), None)
            if nxt is not None and nxt["present"] == 1:
                on_prev, on_first = r, nxt
                break
        mid = relmid = gap = None
        clean = 0
        if last_on is not None:
            j = rs.index(last_on)
            k = rs.index(first_off)
            gap = first_off["dt_press_ms"] - last_on["dt_press_ms"]
            clean = 1 if (k == j + 1 and gap <= 220.0) else 0
            mid = 0.5 * (last_on["dt_press_ms"] + first_off["dt_press_ms"])
            if last_on["dt_rel_ms"] != "":
                relmid = 0.5 * (float(last_on["dt_rel_ms"]) + float(first_off["dt_rel_ms"]))
        # position of the icon relative to the detected meter box, when both are up
        dx = [r["cx"] - PITCH * r["scale"] - r["meter_x"] for r in vis if r["meter_det"] == 1]
        dy = [r["cy"] - r["meter_y"] for r in vis if r["meter_det"] == 1]
        out.append(dict(
            epoch=s["epoch"], shot_type=s["shot_type"], release_src=s["release_src"],
            hold_ms=s["hold_ms"], banner=s["banner"], coverage=s["coverage"],
            n_frames=len(rs), n_plate=len(seen), n_icon_on=len(vis),
            icon_on_at_press=next((r["present"] for r in rs
                                   if -170 <= r["dt_press_ms"] <= 40 and r["present"] >= 0), ""),
            last_on_ms=(last_on["dt_press_ms"] if last_on else ""),
            first_off_ms=(first_off["dt_press_ms"] if first_off else ""),
            off_mid_press_ms=(round(mid, 1) if mid is not None else ""),
            off_mid_rel_ms=(round(relmid, 1) if relmid is not None else ""),
            gap_ms=(round(gap, 1) if gap is not None else ""),
            clean=clean,
            on_edge_mid_press_ms=(round(0.5 * (on_prev["dt_press_ms"] +
                                               on_first["dt_press_ms"]), 1)
                                  if on_first is not None else ""),
            scale=(round(_med([r["scale"] for r in seen]), 2) if seen else ""),
            d_meter_x=(round(_med(dx)) if dx else ""),
            d_meter_y=(round(_med(dy)) if dy else ""),
        ))
    p = os.path.join(OUT, "icon_shots.csv")
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)
    print(p, len(out), "shots")
    report(out)


def report(out):
    eng = [r for r in out if r["release_src"] == "engine"]
    clean = [r for r in eng if r["clean"] == 1 and r["off_mid_rel_ms"] != ""]
    print("\n=== 3PT icon OFF edge (engine-released shots, precise release epoch) ===")
    print(f"engine shots {len(eng)}, with a clean single-frame OFF transition {len(clean)}")
    P = [float(r["off_mid_press_ms"]) for r in clean]
    R = [float(r["off_mid_rel_ms"]) for r in clean]
    H = [float(r["hold_ms"]) for r in clean]
    for name, v in (("icon_off - PRESS", P), ("icon_off - RELEASE", R), ("hold", H)):
        print("  %-20s n=%2d med %7.1f  rMAD %5.1f  sd %6.1f  min %7.1f  max %7.1f"
              % (name, len(v), _med(v), _rmad(v), _sd(v), min(v), max(v)))
    if len(P) > 2:
        def corr(a, b):
            ma, mb = sum(a) / len(a), sum(b) / len(b)
            num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
            den = (sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b)) ** 0.5
            return num / den if den else 0.0
        print("  corr(off-press, hold) = %+.3f   corr(off-release, hold) = %+.3f"
              % (corr(P, H), corr(R, H)))
    g = {}
    for r in clean:
        g.setdefault(r["shot_type"], []).append(float(r["off_mid_rel_ms"]))
    print("  per type (icon_off - release):")
    for k, v in sorted(g.items()):
        print("    %-12s n=%2d med %6.1f rMAD %5.1f  %s"
              % (k, len(v), _med(v), _rmad(v), [round(x) for x in sorted(v)]))
    dx = [float(r["d_meter_x"]) for r in out if r["d_meter_x"] != ""]
    dy = [float(r["d_meter_y"]) for r in out if r["d_meter_y"] != ""]
    if dx:
        print("\n  icon centre relative to the detected METER BOX top-left (px, 720p):")
        print("    dx med %+.0f  (p10 %+.0f p90 %+.0f)   dy med %+.0f  (p10 %+.0f p90 %+.0f)"
              % (_med(dx), sorted(dx)[len(dx) // 10], sorted(dx)[9 * len(dx) // 10],
                 _med(dy), sorted(dy)[len(dy) // 10], sorted(dy)[9 * len(dy) // 10]))


def montage(rows, shots, n=6):
    md = os.path.join(OUT, "montage")
    os.makedirs(md, exist_ok=True)
    by = {}
    for r in rows:
        by.setdefault(r["epoch"], []).append(r)
    # one full-frame locator figure: where the icon lives relative to the player + meter
    for r in rows:
        if r["present"] == 1 and r["meter_det"] == 1 and 200 <= r["dt_press_ms"] <= 1400:
            img = cv2.imread(frame_path(r["idx"]))
            s = r["scale"]
            ix, iy = int(r["cx"] - PITCH * s), int(r["cy"])
            rr = int(round(11 * s))
            cv2.rectangle(img, (ix - rr, iy - rr), (ix + rr, iy + rr), (0, 0, 255), 2)
            cv2.rectangle(img, (int(r["cx"]) - rr, iy - rr), (int(r["cx"]) + rr, iy + rr),
                          (0, 255, 255), 2)
            cv2.putText(img, "3PT icon", (ix - 60, iy + rr + 20), cv2.FONT_HERSHEY_SIMPLEX,
                        0.5, (0, 0, 255), 2)
            cv2.putText(img, "PS-disc anchor", (int(r["cx"]) - 20, iy - rr - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 2)
            cv2.line(img, (r["meter_x"], r["meter_y"]), (ix, iy), (255, 128, 0), 1)
            cv2.putText(img, "meter box", (r["meter_x"] - 10, r["meter_y"] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 128, 0), 2)
            cv2.imwrite(os.path.join(md, "icon_locator.png"), img)
            break
    done = 0
    seen_types = set()
    for pref in (True, False):
        for s in shots:
            if s["release_src"] != "engine" or done >= n:
                continue
            if pref and s["shot_type"] in seen_types:
                continue
            rs = sorted(by.get(s["epoch"], []), key=lambda r: r["dt_press_ms"])
            last = after = None
            for i, r in enumerate(rs):
                if r["present"] != 1 or r["dt_press_ms"] > 2500:
                    continue
                if i + 1 < len(rs) and rs[i + 1]["present"] == 0 and \
                        rs[i + 1]["dt_press_ms"] - r["dt_press_ms"] <= 220:
                    last, after = r, rs[i + 1]
            if last is None:
                continue
            pick = [r for r in rs if r["dt_press_ms"] < last["dt_press_ms"]][-1:] + [last, after]
            tiles = []
            for r in pick:
                img = cv2.imread(frame_path(r["idx"]))
                cx, cy = int(r["cx"]), int(r["cy"])
                c = img[max(0, cy - 34):cy + 34, max(0, cx - 80):cx + 120].copy()
                c = cv2.resize(c, (600, 204), interpolation=cv2.INTER_NEAREST)
                cv2.putText(c, "p%+dms r%sms %s" % (int(r["dt_press_ms"]), r["dt_rel_ms"],
                            "ICON" if r["present"] == 1 else "gone"), (6, 24),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                tiles.append(c)
            path = os.path.join(md, "icon_ep%s_%s.png" % (s["epoch"],
                                s["shot_type"].replace(" ", "")))
            cv2.imwrite(path, np.vstack(tiles))
            print("montage", path, s["shot_type"], "hold", s["hold_ms"])
            seen_types.add(s["shot_type"])
            done += 1


if __name__ == "__main__":
    sys.exit(main())
