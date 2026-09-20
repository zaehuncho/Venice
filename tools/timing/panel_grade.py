#!/usr/bin/env python
"""Grade a framedump against the GAME'S OWN feedback panel. One command, no eyeballing.

WHY THIS EXISTS
---------------
`tools/timing/banner_reader.py scan` returns ZERO events on this rig's court (verified
2026-09-12 on 6,542 frames across two sessions). Two reasons:
  * it only finds the 1-cell TIMING box, and this court shows a 3-cell
    TIMING|COVERAGE|DISTANCE panel; and
  * with the Rhythm/Tempo toggle on, every bot shot is mode=TempoSquare and the game
    shows a RHYTHM panel instead of a TIMING one.
The game's panel is the ONLY validated timing instrument in this project -- every
engine-side number has been caught being circular or inverted (anchor->tip rides our own
release; the self-grade reads EARLY while the game says LATE; peak_fill / settled_fill /
travel_pp correlate NEGATIVELY with real severity). So grading has to be fast, or it does
not happen and we tune blind.

HOW IT WORKS
------------
1. CELLS  Per frame, split the panel strip into its cells. The panel is one grey frame
   (V~155) with vertical divider lines; the "active" cells get a coloured border drawn on
   top. Rows: the pair of long bright rows 12..34 px apart that share the most columns is
   the frame's top/bottom; columns lit over >=85 % of those rows are the dividers; each span
   between dividers with a dark plate inside is one cell. Layouts seen on this rig:
     Rhythm ON :  RHYTHM | DISTANCE                 (2 cells, one coloured)
     Rhythm OFF:  TIMING | COVERAGE | DISTANCE      (3 cells)  or  TIMING | COVERAGE (2)
   The first cell is always the timing verdict; a second cell is COVERAGE if there are
   three cells or if it is coloured (a white second cell of a 2-cell panel is DISTANCE).
2. SIGNATURE  Per frame, the dominant saturated hue in the strip (green / red / yellow),
   a white-text score, a darkness score, and the per-cell colour tuple.
3. EVENTS  A maximal run of frames with the panel up, a stable colour and a stable cell
   layout is one verdict. The white flash the game plays between panels splits adjacent
   events; the layout key splits an all-white panel from the plain court around it.
4. WORD  Each cell's verdict word is fingerprinted INSIDE its own border: the saturated
   pixels for a coloured cell (identical to the single-cell fingerprint this tool was
   validated with), the white value line for a white cell. Masks are matched (NCC)
   against a stored TEMPLATE LIBRARY. Templates are labelled ONCE by a human and then
   reused forever. An unlabelled cluster never guesses: it is reported as UNKNOWN.
5. JOIN  Events are matched to engine releases by wall time. Panel onset runs +0.5..1.5 s
   after the release (green ~0.5-0.9 s, red ~1.2-1.5 s on the owner's court; 1.0-1.8 s on
   the Rhythm court), so the lag window is deliberately wide.

USAGE
-----
  grade (normal):   python tools/timing/panel_grade.py <session_dir>
  label new words:  python tools/timing/panel_grade.py <session_dir> --label
  seed the library: python tools/timing/panel_grade.py <session_dir> --label --seed labels.json

`--label` writes review/clusters.png (one exemplar per unmatched cluster, the cell boxed and
annotated with its role) and a labels.template.json stub. Fill the stub in, re-run with
--seed, and those words are known from then on. `--review DIR` moves the review output.
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import os
import re
import sys
from collections import Counter

import cv2
import numpy as np

# Panel strip in 1280x720 capture coordinates. The strip is fixed HUD furniture -- it does
# not scale with the camera -- so these are constants, not a search.
Y0, Y1, X0, X1 = 12, 52, 430, 870

HERE = os.path.dirname(os.path.abspath(__file__))
LIB_PATH = os.path.join(HERE, "panel_templates.npz")

# A saturated pixel: the verdict text and the panel accent. Measured on real panels.
S_MIN, V_MIN = 110, 110
# Hue bands (OpenCV 0..179).
GREEN = (40, 90)
YELLOW = (18, 36)
RED_LO, RED_HI = 10, 168
# Per-cell colour bands. The orange CONTEST text sits at hue 16-23, so the cell classifier
# folds orange into yellow; the cell's border carries the same colour as its text anyway.
CELL_GREEN = (40, 90)
CELL_YELLOW = (12, 40)
CELL_RED_LO, CELL_RED_HI = 8, 168
CELL_COLOR_PX_MIN = 80     # border + text of one cell; the border alone is ~500 px

PANEL_DARK_MIN = 1500      # the panel's own dark backing plate
FLASH_WHITE_MIN = 1500     # the white wipe the game plays between panels
COLOR_PX_MIN = 250         # enough coloured text to call a class
NCC_MATCH = 0.90           # template acceptance; clustering uses the same bar
MASK_W, MASK_H = 240, 28   # normalised word-mask size
BORDER_PAD = 5             # step inside the cell's coloured border before reading the word
VALUE_PAD = 3              # vertical step inside the border: keeps the value line's last row

# Cell geometry (720p). The grey frame and dividers measure V 150-160, S ~0.
FRAME_V_MIN = 140          # grey frame / divider line
WHITE_TEXT_V = 190         # white verdict text (the legacy white-cell threshold)
CELL_MIN_W = 70            # narrowest cell (they are ~118 px on this rig)
CELL_MIN_H, CELL_MAX_H = 12, 34   # cell height ~28 px
LINE_FRAC = 0.85           # a divider column is lit over this fraction of the cell's rows
FRAME_FRAC = 0.90          # a cell's top/bottom border is lit over this fraction of its width

COVERAGE_WORDS = ("WIDE OPEN", "OPEN", "SEMI-OPEN", "BOTHERED", "LIGHT CONTEST",
                  "SOLID CONTEST", "HEAVY CONTEST", "SMOTHERED")      # open -> smothered
GREEN_WORDS = ("EXCELLENT", "PERFECT")

# Panel onset after the release. 0.51 s is the earliest green seen on the owner's court
# (session_20260912_201355); 2.0 s the latest on the Rhythm court.
LAG_MIN_S, LAG_MAX_S = 0.4, 2.4


# --------------------------------------------------------------------------- frames
def load_frames(session: str):
    fp = os.path.join(session, "frames.csv")
    if not os.path.isfile(fp):
        sys.exit(f"no frames.csv in {session}")
    rows = list(csv.DictReader(open(fp, newline="")))
    if not rows:
        sys.exit("frames.csv is empty")
    return rows


def frame_path(session: str, row) -> str:
    """The dump names a frame by its sidecar detection flag; some dumps carry the other flag
    for a few frames, so fall back to whichever file exists."""
    idx = int(row["idx"])
    p = os.path.join(session, "f%05d_%s_raw.png" % (idx, row["detected"]))
    if os.path.isfile(p):
        return p
    for flag in ("0", "1"):
        q = os.path.join(session, "f%05d_%s_raw.png" % (idx, flag))
        if os.path.isfile(q):
            return q
    return p


def strip_from_frame(im):
    """The panel strip of one full BGR frame (a VIEW, not a copy), or None.

    Split out of strip_of() so the LIVE reader (banner_verdict_live.py) can hand this the
    detector's in-memory frame and get byte-identical pixels to the offline path. The disk
    path below is unchanged: imread -> this.
    """
    if im is None:
        return None
    h, w = im.shape[:2]
    if (w, h) != (1280, 720):                      # normalise so the constants hold
        im = cv2.resize(im, (1280, 720), interpolation=cv2.INTER_AREA)
    return im[Y0:Y1, X0:X1]


def strip_of(session: str, row):
    return strip_from_frame(cv2.imread(frame_path(session, row)))


def planes(strip):
    hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0].astype(int), hsv[..., 1].astype(int), hsv[..., 2].astype(int)
    return h, s, v, (s > S_MIN) & (v > V_MIN)


def signature(strip):
    h, s, v, strong = planes(strip)
    return dict(
        green=int(np.sum(strong & (h >= GREEN[0]) & (h <= GREEN[1]))),
        red=int(np.sum(strong & ((h <= RED_LO) | (h >= RED_HI)))),
        yellow=int(np.sum(strong & (h >= YELLOW[0]) & (h <= YELLOW[1]))),
        white=int(np.sum((s < 45) & (v > 205))),
        dark=int(np.sum(v < 45)),
        cells=tuple(c["color"] for c in find_cells(strip)),
    )


# --------------------------------------------------------------------------- cells
def _runs(b):
    """[(start, end)] of the True runs of a 1-D bool array."""
    b = np.asarray(b, bool)
    if not b.any():
        return []
    d = np.diff(np.concatenate(([0], b.astype(np.int8), [0])))
    return list(zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist()))


def _cell_color(h, sat, x0, x1, y0, y1) -> str:
    hh = h[y0:y1, x0:x1][sat[y0:y1, x0:x1]]
    if hh.size == 0:
        return "white"
    g = int(np.sum((hh >= CELL_GREEN[0]) & (hh <= CELL_GREEN[1])))
    r = int(np.sum((hh <= CELL_RED_LO) | (hh >= CELL_RED_HI)))
    y = int(np.sum((hh >= CELL_YELLOW[0]) & (hh <= CELL_YELLOW[1])))
    n, name = max((g, "green"), (r, "red"), (y, "yellow"))
    return name if n >= CELL_COLOR_PX_MIN else "white"


def find_cells(strip):
    """Split the panel strip into its cells, left to right.

    Each cell is {x0, x1, y0, y1, color} in strip coordinates (x1/y1 exclusive), the box
    INCLUDING its border, so a coloured cell's box equals the saturated bounding box the
    single-cell fingerprint used. Returns [] when no panel is up.
    """
    h, s, v, sat = planes(strip)
    frame = (s < 60) & (v > FRAME_V_MIN)
    b = cv2.morphologyEx((sat | frame).astype(np.uint8), cv2.MORPH_CLOSE,
                         np.ones((1, 3), np.uint8)).astype(bool)
    H, W = b.shape
    cand = [y for y in range(H)
            if max((e - a for a, e in _runs(b[y])), default=0) >= CELL_MIN_W]
    best = None
    for y0 in cand:
        for y1 in cand:
            hgt = y1 - y0
            if hgt < CELL_MIN_H or hgt > CELL_MAX_H:
                continue
            top = b[y0] | b[min(y0 + 1, H - 1)]
            bot = b[y1] | b[max(y1 - 1, 0)]
            cover = int(np.sum(top & bot))
            # Widest shared span wins; a tie goes to the taller pair so a 2-px coloured
            # border keeps its second line inside the box (legacy-identical crops).
            if best is None or (cover, hgt) > (best[0], best[3]):
                best = (cover, y0, y1, hgt)
    if best is None or best[0] < CELL_MIN_W:
        return []
    _, y0, y1, _ = best
    top = b[y0] | b[min(y0 + 1, H - 1)]
    bot = b[y1] | b[max(y1 - 1, 0)]
    lines = _runs(b[y0:y1 + 1].mean(axis=0) >= LINE_FRAC)
    cells = []
    for (l0, l1), (r0, r1) in zip(lines, lines[1:]):
        x0, x1 = l0, r1
        if x1 - x0 < CELL_MIN_W:
            continue
        if top[x0:x1].mean() < FRAME_FRAC or bot[x0:x1].mean() < FRAME_FRAC:
            continue
        # The cell sits on the game's own DARK backing plate. A wooden court floor or a
        # bright HUD block also draws long bright lines, but it is BRIGHT inside the box.
        iv = v[y0 + BORDER_PAD:y1 + 1 - BORDER_PAD, x0 + BORDER_PAD:x1 - BORDER_PAD]
        if iv.size == 0 or float(np.mean(iv < 70)) < 0.45:
            continue
        cells.append(dict(x0=int(x0), x1=int(x1), y0=int(y0), y1=int(y1 + 1),
                          color=_cell_color(h, sat, x0, x1, y0, y1 + 1)))
    return cells


def cell_word_mask(strip, cell):
    """Binary fingerprint of one cell's verdict WORD.

    Coloured cell: the saturated pixels inside the border. The label line (TIMING / RHYTHM /
    COVERAGE) is white, so only the value survives -- the same fingerprint the single-cell
    path produced, so the existing templates keep matching. White cell: the white pixels of
    the LOWER text line only (the label sits above an empty gap row).
    """
    h, s, v, sat = planes(strip)
    ix0, ix1 = cell["x0"] + BORDER_PAD, cell["x1"] - BORDER_PAD
    iy0, iy1 = cell["y0"] + VALUE_PAD, cell["y1"] - VALUE_PAD
    if iy1 - iy0 < 6 or ix1 - ix0 < 20:
        return None
    m = sat[iy0:iy1, ix0:ix1]
    if int(m.sum()) < 25:
        m = ((s < 60) & (v > WHITE_TEXT_V))[iy0:iy1, ix0:ix1]
    groups = [(a, e) for a, e in _runs(m.sum(axis=1) > 0) if e - a >= 5]
    if not groups:
        return None
    a, e = groups[-1]
    val = m[a:e]
    ys, xs = np.nonzero(val)
    if len(xs) < 25:
        return None
    crop = val[ys.min():ys.max() + 1, xs.min():xs.max() + 1].astype(np.uint8) * 255
    if crop.shape[1] < 20:
        return None
    return cv2.resize(crop, (MASK_W, MASK_H), interpolation=cv2.INTER_AREA)


def ncc(a, b) -> float:
    r = cv2.matchTemplate(a.astype(np.float32), b.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    return float(r[0, 0])


def cell_roles(cells):
    """Name the cells by position: [timing] / [timing, distance|coverage] /
    [timing, coverage, distance]. match_event corrects a first cell that reads a coverage
    word (the game sometimes shows COVERAGE alone)."""
    if not cells:
        return []
    roles = ["timing"]
    if len(cells) >= 3:
        roles += ["coverage", "distance"] + ["extra"] * (len(cells) - 3)
    elif len(cells) == 2:
        roles.append("coverage" if cells[1]["color"] != "white" else "distance")
    return roles


def read_cells(strip):
    """[{role, color, box, mask}] for every cell of the strip; [] when no panel is up."""
    cells = find_cells(strip)
    out = []
    for c, role in zip(cells, cell_roles(cells)):
        out.append(dict(role=role, color=c["color"], box=(c["x0"], c["y0"], c["x1"], c["y1"]),
                        mask=cell_word_mask(strip, c) if role != "distance" else None))
    return out


# --------------------------------------------------------------------------- events
def scan(session: str, frames):
    sig, keep = [], []
    for r in frames:
        st = strip_of(session, r)
        if st is None:
            continue
        sig.append(signature(st))
        keep.append(r)
    return keep, sig


def classify(s) -> str:
    if s["green"] > COLOR_PX_MIN:
        return "green"
    if s["red"] > COLOR_PX_MIN:
        return "red"
    if s["yellow"] > COLOR_PX_MIN:
        return "yellow"
    return "white"


def events_from(frames, sig):
    panel = [s["dark"] > PANEL_DARK_MIN for s in sig]
    flash = [(s["white"] > FLASH_WHITE_MIN) and (s["dark"] < 800) for s in sig]
    out, i, n = [], 0, len(sig)
    while i < n:
        if not panel[i]:
            i += 1
            continue
        j, c, lay = i, classify(sig[i]), sig[i]["cells"]
        while (j + 1 < n and panel[j + 1] and classify(sig[j + 1]) == c
               and sig[j + 1]["cells"] == lay and not flash[j + 1]):
            j += 1
        # A "white" run is USUALLY the inter-panel flash or plain court -- but a genuine
        # white-SEVERITY verdict is also unsaturated, and dropping the class outright lost 3 of
        # 14 lates against the hand grade. Keep white runs; the cell layout in the run key
        # separates an all-white panel from the dark court around it, and junk is reported
        # as UNKNOWN rather than silently scored.
        if j - i + 1 >= 2:                       # a 1-frame flicker is not a verdict
            out.append(dict(i0=i, i1=j, color=c, layout=lay,
                            t0=float(frames[i]["t_wall"]), t1=float(frames[j]["t_wall"]),
                            n=j - i + 1))
        i = j + 1
    return out


# --------------------------------------------------------------------------- engine
ISO = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3})Z")
NUM = re.compile(r"([a-zA-Z_][a-zA-Z_0-9]*)=(-?\d+(?:\.\d+)?)")


def engine_releases(log_paths, t_lo: float, t_hi: float):
    """Release-timing lines inside the dump's wall-time window, on the unix clock."""
    out, lead, dev = [], float("nan"), {}
    for p in log_paths:
        if not os.path.isfile(p):
            continue
        for line in open(p, encoding="utf-8", errors="replace"):
            m = ISO.match(line)
            if not m:
                continue
            lm = re.search(r"lead_ms=([\d.]+)", line)
            if lm:
                lead = float(lm.group(1))
            if "Release devoffset:" in line:
                # [ORION_DEV_FIRE_OFFSET_SWEEP] one seq-paired line per release while the sweep
                # hook is armed: "Release devoffset: seq=N applied_ms=X scheduled=1 ...". Keyed by
                # seq so a sweep session grades itself (green rate BY OFFSET, below).
                dd = dict(NUM.findall(line.split("Release devoffset:", 1)[1]))
                if "seq" in dd:
                    dev[int(float(dd["seq"]))] = float(dd.get("applied_ms", "nan"))
                continue
            if "Release timing:" not in line:
                continue
            ts = _dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S.%f").replace(
                tzinfo=_dt.timezone.utc).timestamp()
            if not (t_lo - 5 <= ts <= t_hi + 5):
                continue
            d = dict(NUM.findall(line.split("Release timing:", 1)[1]))
            sm = re.search(r"shot=(.+?)(?: pressTrimMs|$)", line.split("Release timing:", 1)[1])
            out.append(dict(t_rel=ts, seq=int(float(d.get("seq", -1))), lead_ms=lead,
                            fillAtRel=float(d.get("fillAtRel", "nan")),
                            holdToRelMs=float(d.get("holdToRelMs", "nan")),
                            shot=(sm.group(1).strip() if sm else "?"), dev_ms=float("nan")))
    for r in out:
        r["dev_ms"] = dev.get(r["seq"], float("nan"))
    out.sort(key=lambda r: r["t_rel"])
    return out


# --------------------------------------------------------------------------- library
def load_library():
    if not os.path.isfile(LIB_PATH):
        return [], []
    z = np.load(LIB_PATH, allow_pickle=True)
    return list(z["masks"]), [str(x) for x in z["labels"]]


def save_library(masks, labels):
    np.savez_compressed(LIB_PATH, masks=np.array(masks), labels=np.array(labels, dtype=object))


def match_word(mask, masks, labels):
    if mask is None or not masks:
        return "UNKNOWN", 0.0
    best, bi = -1.0, -1
    for k, t in enumerate(masks):
        sc = ncc(mask, t)
        if sc > best:
            best, bi = sc, k
    return (labels[bi], best) if best >= NCC_MATCH else ("UNKNOWN", best)


def match_event(e, masks, labels):
    """Fill e[word]/e[score]/e[timing_color] and e[coverage]/e[cov_score] from the cells.

    word is UNKNOWN for an unmatched timing cell and "" when the panel has no timing cell
    (a lone COVERAGE cell reads a coverage word in first position: it is re-roled, never
    scored as timing)."""
    e["word"], e["score"], e["timing_color"] = "", 0.0, e["color"]
    e["coverage"], e["cov_score"] = "", 0.0
    for c in e.get("cells", []):
        c["word"], c["score"] = match_word(c["mask"], masks, labels)
        if c["role"] == "timing" and c["word"] in COVERAGE_WORDS:
            c["role"] = "coverage"
        if c["role"] == "timing":
            e["word"], e["score"], e["timing_color"] = c["word"], c["score"], c["color"]
        elif c["role"] == "coverage" and not e["coverage"]:
            e["coverage"], e["cov_score"] = c["word"], c["score"]


def is_green(word: str) -> bool:
    return word in GREEN_WORDS


# --------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session")
    ap.add_argument("--log", action="append", default=None,
                    help="engine log(s); default logs/orion_native.log(.1)")
    ap.add_argument("--out", default=None, help="output CSV (default <session>/panel_grade.csv)")
    ap.add_argument("--label", action="store_true",
                    help="write review/clusters.png + labels.template.json for unmatched words")
    ap.add_argument("--seed", default=None,
                    help="labels JSON {cluster_index: WORD} to fold into the template library")
    ap.add_argument("--review", default=None,
                    help="directory for --label output (default <session>/review)")
    a = ap.parse_args()

    session = a.session.rstrip("\\/")
    frames = load_frames(session)
    print(f"session {os.path.basename(session)}: {len(frames)} frames listed")

    frames, sig = scan(session, frames)
    print(f"  read {len(frames)} frames from disk")
    evs = events_from(frames, sig)
    print(f"  panel events: {len(evs)}  {dict(Counter(e['color'] for e in evs))}"
          f"   with cells: {sum(1 for e in evs if e['layout'])}"
          f"  layouts {dict(Counter(len(e['layout']) for e in evs if e['layout']))}")
    if not evs:
        print("  no panel events -- is this a menu-only dump?")
        return 1

    t_lo = min(float(f["t_wall"]) for f in frames)
    t_hi = max(float(f["t_wall"]) for f in frames)
    logs = a.log or ["logs/orion_native.log", "logs/orion_native.log.1"]
    rel = engine_releases(logs, t_lo, t_hi)
    print(f"  engine releases in window: {len(rel)}")

    # word masks per event, from the middle of its run (the panel is fully drawn by then)
    for e in evs:
        e["cells"] = []
        if not e["layout"]:
            continue                                   # court / flash: nothing to read
        for cand in ((e["i0"] + e["i1"]) // 2, (e["i0"] + e["i1"]) // 2 + 1, e["i1"], e["i0"]):
            if 0 <= cand < len(frames):
                st = strip_of(session, frames[cand])
                if st is None:
                    continue
                cells = read_cells(st)
                if cells and cells[0]["mask"] is not None:
                    e["cells"], e["mask_idx"] = cells, int(frames[cand]["idx"])
                    break

    masks, labels = load_library()
    print(f"  template library: {len(masks)} labelled words")
    for e in evs:
        match_event(e, masks, labels)

    # SEED FIRST, then report what is still unknown: --label writes the stub that
    # --seed reads, so the other order silently wipes the labels just typed in.
    if a.seed:
        lab = json.load(open(a.seed))
        cm = np.load(os.path.join(os.path.dirname(a.seed), "cluster_masks.npz"))["masks"]
        added = 0
        for k, word in lab.items():
            word = (word or "").strip().upper()
            if not word:
                continue
            masks.append(cm[int(k)])
            labels.append(word)
            added += 1
        if added:
            save_library(masks, labels)
            print(f"  seeded {added} template(s) -> {LIB_PATH} ({len(masks)} total)")
            for e in evs:
                match_event(e, masks, labels)

    # every (event, cell) still unknown
    unknown = [(e, c) for e in evs for c in e["cells"]
               if c["mask"] is not None and c["word"] == "UNKNOWN"]
    if a.label and unknown:
        review = a.review or os.path.join(session, "review")
        os.makedirs(review, exist_ok=True)
        clusters = []
        for e, c in unknown:
            hit = next((k for k in clusters if ncc(c["mask"], k["mask"]) > NCC_MATCH), None)
            if hit:
                hit["members"].append((e, c))
            else:
                clusters.append(dict(mask=c["mask"], members=[(e, c)], ex=(e, c)))
        tiles = []
        for k, cl in enumerate(clusters):
            e, c = cl["ex"]
            st = strip_of(session, frames[(e["i0"] + e["i1"]) // 2]).copy()
            x0, y0, x1, y1 = c["box"]
            cv2.rectangle(st, (x0 - 1, y0 - 1), (x1, y1), (255, 0, 255), 1)
            big = cv2.resize(st, (st.shape[1] * 2, st.shape[0] * 2), interpolation=cv2.INTER_CUBIC)
            lab = np.zeros((big.shape[0], 240, 3), np.uint8)
            cv2.putText(lab, "CL%d %s %s" % (k, c["role"], c["color"]), (4, 26),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 1)
            cv2.putText(lab, "n=%d f%d" % (len(cl["members"]), e.get("mask_idx", -1)), (4, 56),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)
            tiles.append(np.hstack([lab, big]))
        if tiles:
            cv2.imwrite(os.path.join(review, "clusters.png"), np.vstack(tiles))
            stub_path = os.path.join(review, "labels.template.json")
            prior = {}
            if os.path.isfile(stub_path):
                try:
                    prior = json.load(open(stub_path))
                except (ValueError, OSError):
                    prior = {}
            if not any((v or "").strip() for v in prior.values()):
                json.dump({str(k): "" for k in range(len(clusters))},
                          open(stub_path, "w"), indent=2)
            else:
                print("  (kept your filled-in labels.template.json)")
            np.savez_compressed(os.path.join(review, "cluster_masks.npz"),
                                masks=np.array([c["mask"] for c in clusters]))
            print(f"\n  {len(clusters)} UNKNOWN word cluster(s) -> {review}/clusters.png")
            print(f"  fill {review}/labels.template.json (e.g. \"0\": \"EXCELLENT\") then re-run with")
            print(f"    --label --seed {review}/labels.template.json")

    # join: each event takes the most recent release inside the lag window
    for e in evs:
        cands = [r for r in rel if LAG_MIN_S <= (e["t0"] - r["t_rel"]) <= LAG_MAX_S]
        e["rel"] = cands[-1] if cands else None

    out = a.out or os.path.join(session, "panel_grade.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ev", "t0", "color", "word", "ncc", "frames", "seq", "shot", "lead_ms",
                    "fillAtRel", "holdToRelMs", "lag_s", "mask_frame",
                    "timing_color", "coverage", "cov_ncc", "cells", "dev_ms"])
        for k, e in enumerate(evs):
            r = e["rel"]
            w.writerow([k, "%.3f" % e["t0"], e["color"], e["word"], "%.3f" % e["score"], e["n"],
                        r["seq"] if r else "", r["shot"] if r else "",
                        r["lead_ms"] if r else "", r["fillAtRel"] if r else "",
                        r["holdToRelMs"] if r else "",
                        "%.2f" % (e["t0"] - r["t_rel"]) if r else "", e.get("mask_idx", ""),
                        e["timing_color"], e["coverage"], "%.3f" % e["cov_score"],
                        "|".join(e["layout"]),
                        ("%.2f" % r["dev_ms"]) if (r and r["dev_ms"] == r["dev_ms"]) else ""])

    joined = [e for e in evs if e["rel"]]
    known = [e for e in joined if e["word"] not in ("", "UNKNOWN")]
    print(f"\n  joined to a release: {len(joined)}/{len(evs)}   word known: {len(known)}")

    # per shot: one row per release, the best event in its lag window (a known timing word
    # first, then any event that showed cells, longest run breaking ties)
    shots = []
    for r in rel:
        if not (t_lo - LAG_MAX_S <= r["t_rel"] <= t_hi - LAG_MIN_S):
            continue
        mine = [e for e in evs if e["rel"] is r]
        mine.sort(key=lambda e: (e["word"] not in ("", "UNKNOWN"), bool(e["cells"]), e["n"]),
                  reverse=True)
        shots.append((r, mine[0] if mine else None))
    if shots:
        print("\n  PER SHOT (release -> panel)")
        print("  %4s %-12s %-7s %-10s %-14s %5s %6s %6s" % (
            "seq", "shot", "colour", "timing", "coverage", "lag", "ncc", "cov"))
        for r, e in shots:
            if e is None:
                print("  %4d %-12s %-7s %-10s %-14s %5s" % (r["seq"], r["shot"], "-", "(no panel)", "", ""))
                continue
            print("  %4d %-12s %-7s %-10s %-14s %5.2f %6.3f %6.3f" % (
                r["seq"], r["shot"], e["timing_color"], e["word"] or "(none)", e["coverage"],
                e["t0"] - r["t_rel"], e["score"], e["cov_score"]))
        graded = [e for _, e in shots if e is not None and e["word"] not in ("", "UNKNOWN")]
        if graded:
            g = sum(1 for e in graded if is_green(e["word"]))
            print(f"\n  SHOTS: {len(shots)} released, {len(graded)} graded, "
                  f"GREEN {g}/{len(graded)} = {100.0 * g / len(graded):.0f}%")
            misses = Counter((e["timing_color"], e["word"]) for e in graded if not is_green(e["word"]))
            if misses:
                print("  misses: " + "  ".join(f"{c} {w}={n}" for (c, w), n in misses.most_common()))
            cov = {}
            for e in graded:
                if e["coverage"]:
                    cov.setdefault(e["coverage"], []).append(e)
            if cov:
                print("  by coverage:")
                order = ["WIDE OPEN", "OPEN", "SEMI-OPEN", "BOTHERED", "LIGHT CONTEST", "SOLID CONTEST"]
                for c in sorted(cov, key=lambda x: (order.index(x) if x in order else 99, x)):
                    v = cov[c]
                    gg = sum(1 for e in v if is_green(e["word"]))
                    rest = Counter(f"{e['timing_color']} {e['word']}" for e in v if not is_green(e["word"]))
                    print(f"    {c:<14} green {gg:>2}/{len(v):<2} = {100.0 * gg / len(v):3.0f}%"
                          f"   {dict(rest) if rest else ''}")
                op = [e for c in ("WIDE OPEN", "OPEN") for e in cov.get(c, [])]
                ct = [e for c in ("BOTHERED", "LIGHT CONTEST", "SOLID CONTEST") for e in cov.get(c, [])]
                if op and ct:
                    go = sum(1 for e in op if is_green(e["word"]))
                    gc = sum(1 for e in ct if is_green(e["word"]))
                    print(f"    open {go}/{len(op)} = {100.0 * go / len(op):.0f}%   "
                          f"contested {gc}/{len(ct)} = {100.0 * gc / len(ct):.0f}%")

    if known:
        print("\n  VERDICTS (events): " + "  ".join(f"{k}={v}" for k, v in
                                                  Counter(e["word"] for e in known).most_common()))
        g = sum(1 for e in known if is_green(e["word"]))
        print(f"  GREEN RATE: {g}/{len(known)} = {100.0 * g / len(known):.0f}%")
        byl = {}
        for e in known:
            byl.setdefault(e["rel"]["lead_ms"], []).append(e["word"])
        if len(byl) > 1:
            print("  by lead:")
            for L in sorted(byl):
                v = byl[L]
                gg = sum(1 for x in v if is_green(x))
                print(f"    lead {L:>6}  n={len(v):>3}  green {gg:>3} = {100.0 * gg / len(v):3.0f}%"
                      f"   {dict(Counter(x for x in v if not is_green(x)))}")
        byd = {}
        for e in known:
            dv = e["rel"]["dev_ms"]
            if dv == dv:
                byd.setdefault(round(dv, 1), []).append(e)
        if byd:
            # ORION_DEV_FIRE_OFFSET_SWEEP response curve: negative = fired EARLIER than the
            # shipped aim. Read it as: the offset where LATE and EARLY counts balance (weighted
            # by the game's asymmetry: a late is a certain miss, an early inside the window is
            # not) is the aim to set with the Tip Timing card.
            print("  by fire offset (sweep):")
            for dv in sorted(byd):
                v = byd[dv]
                gg = sum(1 for x in v if is_green(x["word"]))
                nl = sum(1 for x in v if x["word"] == "LATE")
                ne = sum(1 for x in v if x["word"] == "EARLY")
                print(f"    offset {dv:>+6.1f} ms  n={len(v):>3}  green {gg:>3} = {100.0 * gg / len(v):3.0f}%"
                      f"   late {nl}  early {ne}")
        byt = {}
        for e in known:
            byt.setdefault(e["rel"]["shot"], []).append(e["word"])
        if len(byt) > 1:
            print("  by shot type:")
            for t in sorted(byt, key=lambda x: -len(byt[x])):
                v = byt[t]
                gg = sum(1 for x in v if is_green(x))
                print(f"    {t:<14} n={len(v):>3}  green {gg:>3} = {100.0 * gg / len(v):3.0f}%"
                      f"   {dict(Counter(x for x in v if not is_green(x)))}")
    print(f"\n  wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
