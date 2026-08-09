#!/usr/bin/env python
"""
banner_reader.py -- OFFLINE ground-truth grader: read the game's own TIMING banner
from recorded frames and emit one graded row per shot.

WHY THIS EXISTS
  The game's post-shot TIMING banner (EXCELLENT / EARLY / LATE, colour-coded by
  severity) is the only outcome instrument that has ever tracked reality. Every
  log-derived fill metric (settled_fill / peak / f_stop / travel_pp) is refuted-
  circular, and the freeze oracle was causally falsified. This tool reads the banner
  OFFLINE from recorded frames so measurements stop depending on hand-counted
  batches.

HARD BOUNDARY (owner directive)
  Banner-driven CLOSED-LOOP grading is FORBIDDEN. This file is analysis tooling
  only: it is never imported by the engine/orchestrator/sidecar, it reads recorded
  artifacts (framedump PNGs, mp4s, orion_native.log) and writes CSVs. Do not add
  any code path by which a banner read can reach the engine.

WHERE THE BANNER LIVES (measured 2026-08-06 on the 2026-08-04 framedumps)
  NBA 2K26 renders a post-shot feedback panel top-centre of the frame at 720p:
  a "TIMING" label (white, ~80x16 px at 1280x720) with the verdict word directly
  beneath it. Three layouts were observed (4-cell TEMPO/COVERAGE/TIMING/DISTANCE,
  3-cell TIMING/COVERAGE/DISTANCE, 2-cell TIMING/DISTANCE); in every layout the
  verdict sits ~14-36 px below the label's top-left. The verdict value fades in
  0.5-2.2 s after the release (court-dependent, see pitfalls) and persists
  >= 1.2 s (on some courts until the next shot). Banners appear for EVERY shot
  on the court (incl. other players), so joining to engine releases must be
  time-gated, and unmatched banner events are kept (release=NA): they are other
  players' shots OR the owner's pass-through presses on missed-deadline shots --
  the latter are exactly the lates being hunted.

  Verdict rendering: word + colour. Measured text HSV medians:
    EXCELLENT  H=66  S=143 (green)      LATE  H=32 S=240 (yellow = mild)
    EARLY/LATE H~0/179 S=200 (red = severe)   SLIGHTLY LATE: white (S low)
  The word alone is EARLY/LATE/EXCELLENT/SLIGHTLY LATE...; the colour carries
  severity (white=slight, yellow, red). Both are reported. Coloured text is
  bright AND saturated (V>=170, S>=100); white verdicts need a separate
  low-saturation mask (see WHITE_V/WHITE_S) -- a plain V threshold fails on
  bright scenes behind the translucent panel.

  MEASURED PITFALLS built into this reader (2026-08-06):
   * The "TEMPO" label false-matches the TIMING template at ~0.60-0.65 NCC, and
     TEMPO-topped panels exist with NO timing cell at all. Every accepted label
     must beat the TEMPO template at the same location by TEMPO_REJECT_MARGIN.
   * On some courts the panel skeleton (labels, no values) persists between
     shots; the verdict VALUE fades out (~0.5-0.8 s label loss or empty value
     cell) and the next verdict fades in. Events are therefore segmented runs
     of a STABLE value mask, not runs of label visibility.
   * The release->verdict-onset delay is NOT constant: measured 0.5-0.9 s on
     park courts, ~1.9-2.2 s on the persistent-panel court. The join uses a
     [0.2, 3.5] s window with FIFO assignment, after per-session clock
     calibration (framedump PNG mtimes lag capture by a writer-queue delay that
     differs per session; calibrated against reservation_created log lines).

USAGE
  # 1) scan recorded frames -> banner events CSV (+ crops for hand-labelling)
  python tools/timing/banner_reader.py scan --session logs/diagnostics/framedump/session_20260804_201020 \
      --out out/events_201020.csv --crops out/crops
  python tools/timing/banner_reader.py scan --video "C:/Users/aaron/Videos/RED_PARK_NEON.mp4" \
      --out out/events_redpark.csv --crops out/crops --stride 2

  # 2) validate against hand labels (label CSV: event_id,word,color)
  python tools/timing/banner_reader.py validate --events out/events_holdout.csv --labels out/labels.csv

  # 3) join graded events to engine per-shot records
  python tools/timing/banner_reader.py join --events out/events_201020.csv \
      --log logs/orion_native.log.1 --out out/joined.csv

  # 4) first analysis: green-window width vs banner verdict
  python tools/timing/banner_reader.py analyze --joined out/joined.csv

Templates live in tools/timing/banner_templates/ (timing_label.png + word_*.png).
Word templates are binary masks harvested from a TRAIN split of the corpus; the
validate step must only ever be quoted against events NOT used for templates.

CAPTURE SETTINGS FOR A COUNTED BATCH (measured 2026-08-06 by decimating the
2026-08-04 framedump sessions):
  ORION_FRAMEDUMP_INTERVAL=0.2 (5 fps) is SUFFICIENT on park/theater courts
  (12/12 shots, identical verdicts, ~20 frames per banner) but MERGES
  consecutive same-verdict shots on persistent-panel courts (7 events vs 15:
  the between-shot fade can be shorter than the 0.4 s label-loss window that
  5 fps needs to split). Use ORION_FRAMEDUMP_INTERVAL=0.1 -- it is safe on both
  court types and costs ~2.4 GB per 5-minute batch. Always start with a FRESH
  ORION_FRAMEDUMP_DIR: a sidecar respawn restarts f00000 and overwrites the
  previous run in place (both 2026-08-04 splices came from this).
"""
import argparse
import csv
import datetime
import glob
import math
import os
import re
import sys

import numpy as np
import cv2

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(HERE, "banner_templates")

# ---------------------------------------------------------------- geometry (720p)
NORM_H = 720                 # all frames normalised to this height before matching
STRIP_Y = (0, 150)           # search strip for the TIMING label
STRIP_X = (300, 1000)
LABEL_THR = 0.60             # NCC accept for the label (measured: pos >=0.75, bg <=0.44)
LABEL_THR_WINDOWED = 0.55    # calibrated windowed-capture mode only: the preview
                             # banner is upscaled soft text (peaks 0.59-0.63) and
                             # the search strip is a tight 320x80 window, so the
                             # false-label surface is tiny and the value gates
                             # (mask px, centroid, colour, word NCC) stack behind
TEMPO_REJECT_MARGIN = 0.05   # TIMING score must beat TEMPO score here by this much
CELL_DY = (14, 36)           # value cell rows relative to label top
CELL_DX = 140                # value cell half-width around label centre
LABEL_W = 80                 # label template nominal size at 720p
COL_GAP_PX = 25              # column gap that splits distinct text groups
CENTROID_MAX_DX = 70         # verdict text is centred under the label (rejects LED clock etc.)
MIN_MASK_PX = 60             # min text pixels for a valid verdict read
V_MIN, S_MIN = 170, 100      # coloured text = bright AND saturated (measured)
WHITE_V, WHITE_S = 175, 50   # white verdicts (SLIGHTLY LATE measured V~191 S~0;
                             # court bleed through the panel edge is V>=175 S~203)
WORD_H = 14                  # canonical word-mask box for NCC. FIXED box, not
WORD_W = 110                 # aspect-preserving: the tight bbox height is noisy
                             # (9 vs 10 rows) and aspect scaling misaligned the
                             # glyph columns enough to collapse same-word NCC.
WORD_THR = 0.55              # min NCC to accept a word class
EVENT_GAP_S = 0.8            # a time gap beyond this always closes an event
SIG_SPLIT_NCC = 0.60         # value-mask NCC below this (2x in a row) splits events
LABEL_Y_JUMP = 6             # label row jump beyond this splits events (layout change)

# severity buckets from median hue (OpenCV H in 0..179, red wrapped to ~0)
HUE_BUCKETS = (("red", -999, 9), ("orange", 9, 21), ("yellow", 21, 41),
               ("green", 41, 95), ("other", 95, 999))


def hue_bucket(h):
    if h >= 170:
        h -= 180
    for name, lo, hi in HUE_BUCKETS:
        if lo <= h < hi:
            return name
    return "other"


# ---------------------------------------------------------------- template loading
def load_label_template():
    p = os.path.join(TEMPLATE_DIR, "timing_label.png")
    t = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
    if t is None:
        raise SystemExit(f"missing label template: {p}")
    return t


def load_tempo_template():
    p = os.path.join(TEMPLATE_DIR, "tempo_label.png")
    return cv2.imread(p, cv2.IMREAD_GRAYSCALE)   # optional negative template


def load_word_templates():
    """word templates: banner_templates/word_<WORD>[_n].png, binary masks."""
    out = {}
    for p in sorted(glob.glob(os.path.join(TEMPLATE_DIR, "word_*.png"))):
        name = os.path.basename(p)[5:-4]
        word = re.sub(r"_\d+$", "", name).upper()
        m = cv2.imread(p, cv2.IMREAD_GRAYSCALE)
        if m is None:
            continue
        out.setdefault(word, []).append(m)
    return out


# ---------------------------------------------------------------- core per-frame read
class FrameRead(object):
    __slots__ = ("label_score", "label_xy", "mask", "mask_px", "med_hue", "med_sat",
                 "word", "word_score", "word_margin", "color")

    def __init__(self):
        self.label_score = 0.0
        self.label_xy = (-1, -1)
        self.mask = None
        self.mask_px = 0
        self.med_hue = -1.0
        self.med_sat = -1.0
        self.word = ""
        self.word_score = 0.0
        self.word_margin = 0.0
        self.color = ""


def normalise(frame):
    h, w = frame.shape[:2]
    if h != NORM_H:
        frame = cv2.resize(frame, (int(round(w * NORM_H / float(h))), NORM_H),
                           interpolation=cv2.INTER_AREA)
    return frame


def read_frame(frame, label_tmpl, word_tmpls, tempo_tmpl=None, prescale=1.0, strip=None):
    """Run the banner read on one BGR frame. Returns FrameRead (label_score
    below threshold => no banner).

    prescale/strip support windowed captures (e.g. the Venice-app Screen
    Recordings, where the game sits in a preview panel at ~0.54 native scale):
    the 720p-normalised frame is upscaled by `prescale` so the banner reaches
    template scale, and `strip` = (x0, x1, y0, y1) overrides the default label
    search window in the SCALED frame. Defaults reproduce the fixed-scale
    behaviour bit-for-bit."""
    r = FrameRead()
    frame = normalise(frame)
    if prescale != 1.0:
        frame = cv2.resize(frame, (int(round(frame.shape[1] * prescale)),
                                   int(round(frame.shape[0] * prescale))),
                           interpolation=cv2.INTER_CUBIC)
    windowed = strip is not None
    if windowed:
        x0, x1, y0, y1 = strip
        x0 = max(0, min(x0, frame.shape[1] - 1))
        x1 = min(x1, frame.shape[1])
        y0 = max(0, min(y0, frame.shape[0] - 1))
        y1 = min(y1, frame.shape[0])
    else:
        y0, y1 = STRIP_Y
        x0, x1 = STRIP_X
        x1 = min(x1, frame.shape[1])
    strip = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2GRAY)
    if strip.shape[0] < label_tmpl.shape[0] or strip.shape[1] < label_tmpl.shape[1]:
        return r
    res = cv2.matchTemplate(strip, label_tmpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(res)
    r.label_score = float(mx)
    thr = LABEL_THR_WINDOWED if windowed else LABEL_THR
    if mx < thr:
        return r
    # TEMPO rejection: TEMPO-topped panels (no timing cell at all) false-match the
    # TIMING template at ~0.60-0.65. Require TIMING to beat TEMPO at this location.
    if tempo_tmpl is not None and tempo_tmpl.shape == label_tmpl.shape:
        res_t = cv2.matchTemplate(strip, tempo_tmpl, cv2.TM_CCOEFF_NORMED)
        if mx < float(res_t[loc[1], loc[0]]) + TEMPO_REJECT_MARGIN:
            r.label_score = 0.0
            return r
    lx, ly = x0 + loc[0], y0 + loc[1]
    r.label_xy = (lx, ly)

    got = extract_value_mask(frame, lx, ly)
    if got is None:
        return r
    r.mask, r.mask_px, r.med_hue, r.med_sat, r.color = got
    if word_tmpls:
        r.word, r.word_score, r.word_margin = classify_word(r.mask, word_tmpls)
    return r


def extract_value_mask(frame, lx, ly):
    """Extract the verdict-text mask from the value cell under a TIMING label at
    (lx, ly) in a 720p-normalised BGR image (also works on a panel crop as long
    as the label position within it is known). Returns
    (tight_mask, mask_px, med_hue, med_sat, color) or None."""
    cx = lx + LABEL_W // 2
    cy0, cy1 = ly + CELL_DY[0], ly + CELL_DY[1]
    cx0, cx1 = max(0, cx - CELL_DX), min(frame.shape[1], cx + CELL_DX)
    cell = frame[cy0:cy1, cx0:cx1]
    if cell.size == 0:
        return None
    hsv = cv2.cvtColor(cell, cv2.COLOR_BGR2HSV)
    H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    # coloured verdicts are saturated; white verdicts (SLIGHTLY LATE) need the
    # second term -- backgrounds seen through the translucent panel stay dimmer
    mask = (((V >= V_MIN) & (S >= S_MIN)) |
            ((V >= WHITE_V) & (S <= WHITE_S))).astype(np.uint8)

    # keep the column-group nearest the label centre (rejects DISTANCE bleed-through)
    colsum = mask.sum(axis=0)
    on = np.flatnonzero(colsum > 0)
    if on.size == 0:
        return None
    splits = np.flatnonzero(np.diff(on) > COL_GAP_PX)
    groups = np.split(on, splits + 1)
    centre = mask.shape[1] / 2.0
    grp = min(groups, key=lambda g: abs((g[0] + g[-1]) / 2.0 - centre))
    # the verdict is centred under the label; anything further out is bleed-through
    # (LED shot clock, DISTANCE digits, buildings)
    if abs((grp[0] + grp[-1]) / 2.0 - centre) > CENTROID_MAX_DX:
        return None
    gm = np.zeros_like(mask)
    gm[:, grp[0]:grp[-1] + 1] = mask[:, grp[0]:grp[-1] + 1]
    # row-trim to the dominant text band: band edges / LED decor below the value
    # can attach a detached blob that stretches the tight bbox vertically and
    # squashes the glyphs at canonicalisation (measured: 19-row bbox for 9-row text)
    rowsum = gm.sum(axis=1)
    if rowsum.max() > 0:
        thr_r = 0.25 * rowsum.max()
        peak = int(np.argmax(rowsum))
        r0 = peak
        while r0 > 0 and rowsum[r0 - 1] >= thr_r:
            r0 -= 1
        r1 = peak
        while r1 < len(rowsum) - 1 and rowsum[r1 + 1] >= thr_r:
            r1 += 1
        gm[:r0, :] = 0
        gm[r1 + 1:, :] = 0
    ys, xs = np.nonzero(gm)
    if ys.size < MIN_MASK_PX or (ys.max() - ys.min()) < 7 or (xs.max() - xs.min()) < 18:
        return None
    tight = gm[ys.min():ys.max() + 1, xs.min():xs.max() + 1] * 255
    sel = gm.astype(bool)
    hsel = H[sel].astype(int)
    # unwrap red (h near 179 == h near 0) before the median
    hsel = np.where(hsel >= 170, hsel - 180, hsel)
    med_hue = float(np.median(hsel))
    med_sat = float(np.median(S[sel]))
    color = "white" if med_sat <= 80 else hue_bucket(med_hue)
    return tight, int(ys.size), med_hue, med_sat, color


def _canon(mask):
    return cv2.resize(mask, (WORD_W, WORD_H), interpolation=cv2.INTER_AREA)


def classify_word(tight_mask, word_tmpls):
    """NCC of the canonicalised mask against each word template (small padded
    search absorbs +-3 px of residual misalignment). Returns (word, best, margin)."""
    q = cv2.copyMakeBorder(_canon(tight_mask), 3, 3, 6, 6, cv2.BORDER_CONSTANT, value=0)
    scores = {}
    for word, tmpls in word_tmpls.items():
        best = 0.0
        for t in tmpls:
            tc = _canon(t)
            res = cv2.matchTemplate(q, tc, cv2.TM_CCOEFF_NORMED)
            best = max(best, float(res.max()))
        scores[word] = best
    if not scores:
        return "", 0.0, 0.0
    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    word, s = ranked[0]
    margin = s - (ranked[1][1] if len(ranked) > 1 else 0.0)
    if s < WORD_THR:
        return "UNKNOWN", s, margin
    return word, s, margin


# ---------------------------------------------------------------- frame sources
def iter_session(session_dir):
    """Yield (frame_idx, wall_dt_local, path) for a framedump session, mtime-ordered
    coherently: sessions can be two spliced runs (index wrap overwrote the head --
    seen in session_20260804_201020 at idx 141). We order by mtime, which restores
    each run's internal order; the caller sees a monotone timeline."""
    rows = []
    for p in glob.glob(os.path.join(session_dir, "f*_raw.png")):
        m = re.search(r"f(\d+)_([01])_raw\.png$", os.path.basename(p))
        if not m:
            continue
        rows.append((os.path.getmtime(p), int(m.group(1)), p))
    rows.sort()
    for mt, idx, p in rows:
        yield idx, datetime.datetime.fromtimestamp(mt), p


def calibrate_video_scale(path, label_tmpl, tempo_tmpl, samples=50):
    """For windowed captures (game inside an app preview): find the prescale
    factor and label search window that bring the banner to template scale.
    Samples frames across the video, tries a scale ladder with a full-frame
    label search, and returns (prescale, strip) from the modal high-scoring
    hit -- or None if the plain fixed-scale path already works / no banner."""
    SCALES = (1.0, 1.15, 1.3, 1.5, 1.7, 1.9, 2.1, 2.3)
    cap = cv2.VideoCapture(path)
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    hits = []
    fixed_hits = 0
    for fi in range(0, n, max(1, n // samples)):
        cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ok, frame = cap.read()
        if not ok:
            continue
        base = normalise(frame)
        best = None
        for s in SCALES:
            f = base if s == 1.0 else cv2.resize(
                base, (int(round(base.shape[1] * s)), int(round(base.shape[0] * s))),
                interpolation=cv2.INTER_CUBIC)
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            if g.shape[0] < label_tmpl.shape[0] or g.shape[1] < label_tmpl.shape[1]:
                continue
            res = cv2.matchTemplate(g, label_tmpl, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(res)
            if tempo_tmpl is not None and tempo_tmpl.shape == label_tmpl.shape:
                res_t = cv2.matchTemplate(g, tempo_tmpl, cv2.TM_CCOEFF_NORMED)
                if mx < float(res_t[loc[1], loc[0]]) + TEMPO_REJECT_MARGIN:
                    continue
            if best is None or mx > best[0]:
                best = (mx, s, loc[0], loc[1])
        if best and best[0] >= 0.55:
            hits.append(best)
            if best[1] == 1.0 and best[0] >= LABEL_THR:
                fixed_hits += 1
    cap.release()
    if len(hits) < 5:
        return None
    if fixed_hits >= max(2, len(hits) // 2):
        return None            # fixed-scale path already works -- do not touch it
    # modal scale, then median position at that scale
    from collections import Counter
    s_star = Counter(h[1] for h in hits).most_common(1)[0][0]
    xs = sorted(h[2] for h in hits if h[1] == s_star)
    ys = sorted(h[3] for h in hits if h[1] == s_star)
    x, y = xs[len(xs) // 2], ys[len(ys) // 2]
    strip = (x - 160, x + 160 + LABEL_W, y - 40, y + 40 + 16)
    return s_star, strip


def iter_video(path, stride):
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    fi = -1
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        fi += 1
        if fi % stride:
            continue
        yield fi, fi / fps, frame
    cap.release()


# ---------------------------------------------------------------- scan -> events
EVENT_FIELDS = ["event_id", "source", "onset_frame", "onset_time", "end_time",
                "n_frames", "word", "word_score", "word_margin", "color",
                "med_hue", "label_score", "label_x", "label_y", "crop_path"]


def mask_ncc(a, b):
    """NCC of two canonicalised binary masks (order-independent)."""
    if a is None or b is None:
        return 0.0
    ca = cv2.copyMakeBorder(_canon(a), 2, 2, 4, 4, cv2.BORDER_CONSTANT, value=0)
    return float(cv2.matchTemplate(ca, _canon(b), cv2.TM_CCOEFF_NORMED).max())


class _RunSegmenter(object):
    """Streaming event segmentation. A banner EVENT is a maximal run of frames
    with a STABLE verdict value mask -- label visibility alone is not enough
    (some courts keep the panel skeleton up between shots). Splits on:
    value-mask change (debounced), label-row jump (layout change), label/value
    loss for >=2 scanned frames, or a wall-time gap."""

    def __init__(self):
        self.runs = []
        self.cur = []
        self.ref = None
        self.miss = 0
        self.disagree = 0

    @staticmethod
    def _t(h):
        t = h[1]
        return t.timestamp() if isinstance(t, datetime.datetime) else t

    def _close(self):
        if self.cur:
            self.runs.append((self.cur, self.ref))
        self.cur, self.ref, self.disagree = [], None, 0

    def feed(self, idx, t, fr, panel):
        valid = fr.mask is not None
        if not valid:
            self.miss += 1
            if self.miss >= 2:
                self._close()
            return
        self.miss = 0
        h = (idx, t, fr, panel)
        if self.cur:
            if (self._t(h) - self._t(self.cur[-1])) > EVENT_GAP_S or \
                    abs(fr.label_xy[1] - self.cur[-1][2].label_xy[1]) > LABEL_Y_JUMP:
                self._close()
        if self.cur:
            if mask_ncc(self.ref, fr.mask) < SIG_SPLIT_NCC:
                self.disagree += 1
                if self.disagree >= 2:
                    self._close()
                    self.cur = [h]
                    self.ref = fr.mask
                return
            self.disagree = 0
            self.cur.append(h)
        else:
            self.cur = [h]
            self.ref = fr.mask

    def finish(self):
        """Close and return runs, merging blip-splits: an overlay/occlusion flicker
        can wipe the label or value for a frame or two mid-banner. A real fade to
        the NEXT shot's verdict keeps the label lost ~0.9 s (measured), so only
        gaps <= 0.5 s with the SAME stable mask are merged."""
        self._close()
        merged = []
        for frames, ref in self.runs:
            if merged:
                pframes, pref = merged[-1]
                gap = self._t(frames[0]) - self._t(pframes[-1])
                same_row = abs(frames[0][2].label_xy[1] - pframes[-1][2].label_xy[1]) <= LABEL_Y_JUMP
                if gap <= 0.5 and same_row and mask_ncc(pref, ref) >= 0.8:
                    merged[-1] = (pframes + frames, pref)
                    continue
            merged.append((frames, ref))
        return [frames for frames, _ in merged]


def build_event_rows(runs, source, crops_dir, min_frames):
    rows = []
    for ev in runs:
        if len(ev) < min_frames:
            continue
        votes = {}
        for _, _, fr, _ in ev:
            if fr.word:
                votes[fr.word] = votes.get(fr.word, 0.0) + max(fr.word_score, 1e-3)
        word = max(votes, key=votes.get) if votes else ""
        best = max((fr for _, _, fr, _ in ev),
                   key=lambda f: f.word_score if f.word == word else f.label_score)
        hues = [fr.med_hue for _, _, fr, _ in ev if fr.med_hue > -900]
        med_hue = float(np.median(hues)) if hues else -1.0
        cvotes = {}
        for _, _, fr, _ in ev:
            if fr.color:
                cvotes[fr.color] = cvotes.get(fr.color, 0) + 1
        color = max(cvotes, key=cvotes.get) if cvotes else ""
        onset, end = ev[0], ev[-1]
        # crop from the stablest frame (max text pixels), not the fading onset
        stable = max(ev, key=lambda h: h[2].mask_px)
        eid = f"{source}#{onset[0]:06d}"
        crop_path = ""
        if crops_dir:
            os.makedirs(crops_dir, exist_ok=True)
            crop_path = os.path.join(crops_dir, eid.replace("/", "_").replace("#", "_") + ".png")
            cv2.imwrite(crop_path, stable[3])
        fmt = (lambda t: t.isoformat() if isinstance(t, datetime.datetime) else f"{t:.3f}")
        rows.append({
            "event_id": eid, "source": source,
            "onset_frame": onset[0], "onset_time": fmt(onset[1]), "end_time": fmt(end[1]),
            "n_frames": len(ev), "word": word,
            "word_score": round(best.word_score, 3), "word_margin": round(best.word_margin, 3),
            "color": color, "med_hue": round(med_hue, 1),
            "label_score": round(max(fr.label_score for _, _, fr, _ in ev), 3),
            "label_x": best.label_xy[0], "label_y": best.label_xy[1],
            "crop_path": crop_path,
        })
    return rows


def cmd_scan(args):
    label_tmpl = load_label_template()
    tempo_tmpl = load_tempo_template()
    word_tmpls = load_word_templates()
    if not word_tmpls:
        print("NOTE: no word templates in banner_templates/ -- events will carry "
              "masks/colour only (harvest mode).")
    seg = _RunSegmenter()
    n_seen = n_hit = 0
    prescale, strip = 1.0, None
    if args.session:
        source = os.path.basename(os.path.normpath(args.session))
        it = ((idx, t, cv2.imread(p)) for idx, t, p in iter_session(args.session))
    else:
        source = os.path.splitext(os.path.basename(args.video))[0]
        it = iter_video(args.video, args.stride)
        if getattr(args, "auto_scale", False):
            cal = calibrate_video_scale(args.video, label_tmpl, tempo_tmpl)
            if cal:
                prescale, strip = cal
                print(f"auto-scale: banner found at {prescale:.2f}x, "
                      f"search window {strip} (windowed capture)")
            else:
                print("auto-scale: fixed-scale path is fine (or no banner found)")
    for idx, t, frame in it:
        if frame is None:
            continue
        n_seen += 1
        fr = read_frame(frame, label_tmpl, word_tmpls, tempo_tmpl,
                        prescale=prescale, strip=strip)
        panel = None
        if fr.mask is not None:
            n_hit += 1
            nf = normalise(frame)
            if prescale != 1.0:   # label_xy lives in the prescaled frame
                nf = cv2.resize(nf, (int(round(nf.shape[1] * prescale)),
                                     int(round(nf.shape[0] * prescale))),
                                interpolation=cv2.INTER_CUBIC)
            lx, ly = fr.label_xy
            panel = nf[max(0, ly - 15):ly + 45, max(0, lx - 210):lx + 290].copy()
        seg.feed(idx, t, fr, panel)
    rows = build_event_rows(seg.finish(), source, args.crops, args.min_frames)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EVENT_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{source}: {n_seen} frames scanned, {n_hit} banner frames, "
          f"{len(rows)} events -> {args.out}")
    for r in rows:
        print(f"  {r['event_id']}  word={r['word'] or '?':10s} color={r['color']:7s} "
              f"score={r['word_score']} n={r['n_frames']} t={r['onset_time']}")


# ---------------------------------------------------------------- labelsheet
def cmd_labelsheet(args):
    """Numbered contact sheets of event crops for BLIND hand-labelling (no reader
    output is printed on the sheet), plus a label-CSV skeleton to fill in."""
    events = []
    for evf in args.events:
        with open(evf, newline="") as f:
            events.extend(csv.DictReader(f))
    os.makedirs(args.out_dir, exist_ok=True)
    per = args.per_sheet
    skel = os.path.join(args.out_dir, "labels_skeleton.csv")
    with open(skel, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["event_id", "word", "color"])
        for ev in events:
            w.writerow([ev["event_id"], "", ""])
    n_sheet = 0
    for i in range(0, len(events), per):
        tiles = []
        for j, ev in enumerate(events[i:i + per]):
            tile = np.zeros((90, 520, 3), np.uint8)
            im = cv2.imread(ev["crop_path"]) if ev["crop_path"] else None
            if im is not None:
                h, w2 = im.shape[:2]
                tile[:min(60, h), :min(500, w2)] = im[:min(60, h), :min(500, w2)]
            cv2.putText(tile, f"[{i + j:03d}] {ev['event_id'][-28:]}", (5, 84),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 255, 255), 1)
            tiles.append(tile)
        rows = [np.hstack(tiles[k:k + args.cols]) for k in range(0, len(tiles), args.cols)]
        W = max(r.shape[1] for r in rows)
        rows = [np.pad(r, ((0, 0), (0, W - r.shape[1]), (0, 0))) for r in rows]
        out = os.path.join(args.out_dir, f"sheet_{n_sheet:02d}.png")
        cv2.imwrite(out, np.vstack(rows))
        n_sheet += 1
    print(f"{len(events)} events -> {n_sheet} sheets + {skel}")


# ---------------------------------------------------------------- validate
def cmd_validate(args):
    truth = {}
    with open(args.labels, newline="") as f:
        for row in csv.DictReader(f):
            truth[row["event_id"]] = (row["word"].strip().upper(),
                                      row.get("color", "").strip().lower())
    pred = {}
    for evf in args.events:
        with open(evf, newline="") as f:
            for row in csv.DictReader(f):
                pred[row["event_id"]] = (row["word"].strip().upper(),
                                         row["color"].strip().lower())
    keys = [k for k in truth if k in pred]
    missing = [k for k in truth if k not in pred]
    words = sorted({truth[k][0] for k in keys} | {pred[k][0] for k in keys})
    conf = {t: {p: 0 for p in words} for t in words}
    col_ok = col_n = 0
    for k in keys:
        conf[truth[k][0]][pred[k][0]] += 1
        if truth[k][1]:
            col_n += 1
            col_ok += (truth[k][1] == pred[k][1])
    print(f"n={len(keys)} labelled events matched ({len(missing)} labels had no event -- "
          f"reader MISSED those shots; they count against recall)")
    wcol = max(10, max(len(w) for w in words) + 1)
    print("word confusion (rows=truth, cols=pred):")
    print(" " * wcol + "".join(f"{w:>{wcol}}" for w in words))
    for t in words:
        print(f"{t:>{wcol}}" + "".join(f"{conf[t][p]:>{wcol}}" for p in words))
    print("\nper-class precision/recall (word):")
    for w in words:
        tp = conf[w][w]
        fn = sum(conf[w].values()) - tp
        fp = sum(conf[t][w] for t in words) - tp
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / (tp + fn) if tp + fn else float("nan")
        print(f"  {w:12s} precision={prec:.3f} recall={rec:.3f} (tp={tp} fp={fp} fn={fn})")
    if col_n:
        print(f"\ncolour bucket agreement: {col_ok}/{col_n} = {col_ok / col_n:.3f}")
    if missing:
        print("labels with no reader event (missed banners):")
        for k in missing:
            print("   ", k, truth[k])


# ---------------------------------------------------------------- join
RE_RELEASE = re.compile(
    r"^(\S+)\s+Release issued: fill ([\d.]+)% target [\d.]+% \(green (-?[\d.]+)-(-?[\d.]+)\)"
    r".*?reason=(\S+).*?code=(\S+).*?src=(\S+) conf=([\d.]+) age=(\d+)ms offset=([\d.-]+)ms "
    r"seq=(\d+) shot=(.+?)\s*$")
RE_TIMING = re.compile(
    r"^(\S+)\s+Release timing: seq=(\d+) code=\S+ anchorAppearMs=(-?\d+) appearToRelMs=(-?\d+) "
    r"holdToRelMs=(-?\d+) plannedClockMs=(-?\d+) fillAtRel=([\d.]+) peakFill=([\d.]+)")
RE_RESV = re.compile(
    r"^(\S+)\s+TIP RESERVATION: disposition=\S+ source=(\S+) tip_eta_ms=([\d.-]+) .*?"
    r"fill_pct=([\d.]+) frame_age_ms=([\d.]+) .*?phase_primary=(\d) phase_anchor_pct=([\d.-]+)")
RE_PHASE = re.compile(
    r"^(\S+)\s+PHASE SAMPLE: raw_ms=([\d.-]+) normalized_ms=([\d.-]+) anchor_pct=([\d.]+) "
    r"accepted=(\d) .*?shot_type=(.+?)\s*$")
RE_SELF = re.compile(
    r"^(\S+)\s+Self-grade diagnostic \(not timing truth\): seq=(\d+) verdict=(\S+) errorMs=([\d.-]+)")
RE_MISS = re.compile(
    r"^(\S+)\s+TIP DEADLINE DECISION.*?disposition=rejected_missed.*?source=(\S+).*?"
    r"tip_eta_ms=(-?[\d.]+).*?lateness_ms=(-?[\d.]+)")


def _ts(s):
    return datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))


def parse_log(paths):
    rel, resv, phase, self_g, timing, misses = [], [], [], [], [], []
    for path in paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RE_RELEASE.match(line)
                if m:
                    rel.append({"t": _ts(m.group(1)), "fill": float(m.group(2)),
                                "green_a": float(m.group(3)), "green_b": float(m.group(4)),
                                "reason": m.group(5), "code": m.group(6), "src": m.group(7),
                                "conf": float(m.group(8)), "age_ms": int(m.group(9)),
                                "offset_ms": float(m.group(10)), "seq": int(m.group(11)),
                                "shot": m.group(12)})
                    continue
                m = RE_TIMING.match(line)
                if m:
                    timing.append({"t": _ts(m.group(1)), "seq": int(m.group(2)),
                                   "appearToRelMs": int(m.group(4)), "holdToRelMs": int(m.group(5)),
                                   "fillAtRel": float(m.group(7)), "peakFill": float(m.group(8))})
                    continue
                m = RE_RESV.match(line)
                if m:
                    resv.append({"t": _ts(m.group(1)), "source": m.group(2),
                                 "tip_eta_ms": float(m.group(3)), "fill_pct": float(m.group(4)),
                                 "frame_age_ms": float(m.group(5)), "phase_primary": int(m.group(6)),
                                 "anchor_pct": float(m.group(7))})
                    continue
                m = RE_PHASE.match(line)
                if m:
                    phase.append({"t": _ts(m.group(1)), "normalized_ms": float(m.group(3)),
                                  "anchor_pct": float(m.group(4)), "accepted": int(m.group(5)),
                                  "shot_type": m.group(6)})
                    continue
                m = RE_SELF.match(line)
                if m:
                    self_g.append({"t": _ts(m.group(1)), "seq": int(m.group(2)),
                                   "verdict": m.group(3), "errorMs": float(m.group(4))})
                    continue
                m = RE_MISS.match(line)
                if m:
                    misses.append({"t": _ts(m.group(1)), "src": m.group(2),
                                   "tip_eta": float(m.group(3)), "lateness": float(m.group(4))})
    return rel, resv, phase, self_g, timing, misses


JOIN_FIELDS = ["event_id", "banner_onset_utc", "word", "color", "word_score", "n_frames",
               "release_utc", "rel_seq", "shot", "fill_at_release", "green_a", "green_b",
               "green_width_pp", "green_floor_clamped", "reason", "src", "frame_age_ms_at_rel",
               "offset_ms", "resv_source", "resv_anchor_pct", "resv_phase_primary",
               "resv_frame_age_ms", "phase_normalized_ms", "phase_accepted",
               "appearToRelMs", "holdToRelMs", "peakFill", "self_grade", "self_grade_errorMs",
               "delta_banner_s", "join_status", "nofire_lateness_ms"]

# verdict-value onset relative to the release: measured 0.5-0.9 s on park courts,
# 1.9-2.2 s on the persistent-panel court -- NOT a constant. FIFO assignment
# inside this window handles pipelined shots at the tightest observed cadence.
JOIN_MIN_S, JOIN_MAX_S = 0.2, 3.5

RE_CREATED = re.compile(
    r"^(\S+)\s+TIP RESERVATION: disposition=reservation_created")


def calibrate_session_lag(session_dir, log_paths):
    """Framedump PNG mtimes lag capture by a writer-queue delay that differs per
    session. Anchor: the first detected=1 frame of each meter episode (filename
    flag) vs the engine's reservation_created line. Returns median lag seconds
    (subtract from mtimes to get engine-clock wall time), or None."""
    created = []
    for path in log_paths:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RE_CREATED.match(line)
                if m:
                    created.append(_ts(m.group(1)))
    starts = []
    prev_det = 0
    for idx, t, p in iter_session(session_dir):
        det = 1 if re.search(r"_1_raw\.png$", p) else 0
        if det and not prev_det:
            starts.append(t.astimezone().astimezone(datetime.timezone.utc))
        prev_det = det
    lags = []
    for s in starts:
        near = min(created, key=lambda c: abs((s - c).total_seconds()), default=None)
        if near is not None and abs((s - near).total_seconds()) <= 3.0:
            lags.append((s - near).total_seconds())
    if not lags:
        return None
    return float(np.median(lags))


def cmd_join(args):
    lag_by_source = {}
    for sd in (args.calib_session or []):
        src = os.path.basename(os.path.normpath(sd))
        lag = calibrate_session_lag(sd, args.log)
        if lag is None:
            print(f"calibration: {src}: no episode/reservation pairs found -- lag 0 assumed")
        else:
            print(f"calibration: {src}: framedump mtime lags engine clock by {lag:+.3f} s")
            lag_by_source[src] = lag
    events = []
    for evf in args.events:
        with open(evf, newline="") as f:
            for row in csv.DictReader(f):
                t = row["onset_time"]
                try:
                    dt = datetime.datetime.fromisoformat(t)
                except ValueError:
                    print(f"  SKIP {row['event_id']}: onset_time {t!r} is not a wall time "
                          f"(video scans cannot be joined)")
                    continue
                if dt.tzinfo is None:
                    dt = dt.astimezone()   # frame mtimes are machine-local
                et = dt.astimezone(datetime.timezone.utc)
                lag = lag_by_source.get(row["source"], 0.0)
                events.append((et - datetime.timedelta(seconds=lag), row))
    events.sort(key=lambda e: e[0])
    rel, resv, phase, self_g, timing, misses = parse_log(args.log)
    rel.sort(key=lambda r: r["t"])

    def nearest_before(items, t, within_s):
        best = None
        for it in items:
            d = (t - it["t"]).total_seconds()
            if 0 <= d <= within_s and (best is None or d < best[0]):
                best = (d, it)
        return best[1] if best else None

    def nearest_after(items, t, within_s):
        best = None
        for it in items:
            d = (it["t"] - t).total_seconds()
            if 0 <= d <= within_s and (best is None or d < best[0]):
                best = (d, it)
        return best[1] if best else None

    rows = []
    used_rel = set()
    for et, ev in events:
        row = {k: "" for k in JOIN_FIELDS}
        row.update({"event_id": ev["event_id"], "banner_onset_utc": et.isoformat(),
                    "word": ev["word"], "color": ev["color"],
                    "word_score": ev["word_score"], "n_frames": ev["n_frames"]})
        # transition fragments (fade junk, partially-rendered values) must not
        # steal a release from the real banner that follows them. A genuinely new
        # verdict word would still show up in the events CSV / labelsheet for
        # template addition -- it just will not join until a template exists.
        if ev["word"] in ("", "UNKNOWN") and float(ev["word_score"] or 0) < 0.45:
            row["join_status"] = "low_confidence_skipped"
            rows.append(row)
            continue
        # FIFO: the OLDEST unclaimed release in the window owns this banner
        # (verdicts arrive in shot order; delta varies 0.5-2.2 s by court)
        cand = [r for r in rel
                if JOIN_MIN_S <= (et - r["t"]).total_seconds() <= JOIN_MAX_S
                and id(r) not in used_rel]
        if cand:
            r = cand[0]
            used_rel.add(id(r))
            ga, gb = r["green_a"], r["green_b"]
            width = (gb - ga) if (ga >= 0 and gb >= 0) else ""
            row.update({
                "release_utc": r["t"].isoformat(), "rel_seq": r["seq"], "shot": r["shot"],
                "fill_at_release": r["fill"], "green_a": ga, "green_b": gb,
                "green_width_pp": width,
                "green_floor_clamped": int(abs(ga - 98.0) < 0.05) if ga >= 0 else "",
                "reason": r["reason"], "src": r["src"], "frame_age_ms_at_rel": r["age_ms"],
                "offset_ms": r["offset_ms"],
                "delta_banner_s": round((et - r["t"]).total_seconds(), 3),
                "join_status": "matched",
            })
            rv = nearest_before(resv, r["t"], 2.0)
            if rv:
                row.update({"resv_source": rv["source"], "resv_anchor_pct": rv["anchor_pct"],
                            "resv_phase_primary": rv["phase_primary"],
                            "resv_frame_age_ms": rv["frame_age_ms"]})
            ph = nearest_after(phase, r["t"], 2.5)
            if ph:
                row.update({"phase_normalized_ms": ph["normalized_ms"],
                            "phase_accepted": ph["accepted"]})
            tm = nearest_after(timing, r["t"] - datetime.timedelta(seconds=0.2), 1.0)
            if tm and tm["seq"] == r["seq"]:
                row.update({"appearToRelMs": tm["appearToRelMs"], "holdToRelMs": tm["holdToRelMs"],
                            "peakFill": tm["peakFill"]})
            sg = nearest_after(self_g, r["t"], 4.0)
            if sg and sg["seq"] == r["seq"]:
                row.update({"self_grade": sg["verdict"], "self_grade_errorMs": sg["errorMs"]})
        else:
            row["join_status"] = "no_release"
            ms = nearest_before(misses, et, 6.0)
            if ms:
                row["join_status"] = "no_fire_missed_deadline"
                row["nofire_lateness_ms"] = ms["lateness"]
        rows.append(row)

    # unmatched releases are only meaningful where frames were actually scanned:
    # keep them within a minute of the observed banner-event span
    if events:
        lo = min(et for et, _ in events) - datetime.timedelta(seconds=60)
        hi = max(et for et, _ in events) + datetime.timedelta(seconds=60)
    else:
        lo = hi = None
    for r in rel:
        if lo is not None and not (lo <= r["t"] <= hi):
            continue
        if id(r) not in used_rel:
            row = {k: "" for k in JOIN_FIELDS}
            ga, gb = r["green_a"], r["green_b"]
            row.update({"event_id": "", "word": "", "join_status": "release_without_banner",
                        "release_utc": r["t"].isoformat(), "rel_seq": r["seq"], "shot": r["shot"],
                        "fill_at_release": r["fill"], "green_a": ga, "green_b": gb,
                        "green_width_pp": (gb - ga) if (ga >= 0 and gb >= 0) else "",
                        "green_floor_clamped": int(abs(ga - 98.0) < 0.05) if ga >= 0 else "",
                        "reason": r["reason"], "src": r["src"], "frame_age_ms_at_rel": r["age_ms"]})
            rows.append(row)

    rows.sort(key=lambda r: r["release_utc"] or r["banner_onset_utc"])
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=JOIN_FIELDS)
        w.writeheader()
        w.writerows(rows)
    n_m = sum(1 for r in rows if r["join_status"] == "matched")
    print(f"{len(rows)} rows -> {args.out}  (matched={n_m}, "
          f"no_release={sum(1 for r in rows if r['join_status'] == 'no_release')}, "
          f"no_fire={sum(1 for r in rows if r['join_status'] == 'no_fire_missed_deadline')}, "
          f"release_without_banner={sum(1 for r in rows if r['join_status'] == 'release_without_banner')})")


# ---------------------------------------------------------------- analyze
def cmd_analyze(args):
    rows = []
    for jf in args.joined:
        with open(jf, newline="") as f:
            rows.extend(r for r in csv.DictReader(f) if r["join_status"] == "matched")
    graded = [r for r in rows if r["word"] in ("EXCELLENT", "EARLY", "LATE")
              and r["green_width_pp"] not in ("", None)]
    print(f"matched banner+release rows: {len(rows)}; with verdict AND real green window: {len(graded)}")
    if not graded:
        print("NO rows with both a banner verdict and a real green window -- the "
              "width-vs-miss question cannot be answered from this input.")
        return
    narrow_thr = args.narrow_pp
    a = b = c = d = 0   # a: narrow&miss  b: narrow&green  c: wide&miss  d: wide&green
    for r in graded:
        w = float(r["green_width_pp"])
        green = (r["word"] == "EXCELLENT")
        if w <= narrow_thr:
            b += green
            a += (not green)
        else:
            d += green
            c += (not green)
    print(f"\ncontingency (narrow = width <= {narrow_thr}pp):")
    print(f"                 miss   green")
    print(f"  narrow      {a:6d}  {b:6d}")
    print(f"  wide        {c:6d}  {d:6d}")
    from scipy.stats import fisher_exact, chi2_contingency
    tbl = [[a, b], [c, d]]
    if min(a + b, c + d) > 0:
        odds, p = fisher_exact(tbl)
        print(f"  Fisher exact: odds={odds:.3f} p={p:.4f}")
        if all(x >= 5 for x in (a, b, c, d)):
            chi2, pc, _, _ = chi2_contingency(tbl)
            print(f"  chi-square:   chi2={chi2:.3f} p={pc:.4f}")
        else:
            print("  chi-square omitted (expected cell < 5); Fisher is the valid test")
    clamped = [r for r in graded if r["green_floor_clamped"] == "1"]
    print(f"\ngreen_start pinned at 98.00 floor on {len(clamped)}/{len(graded)} rows "
          f"({100.0 * len(clamped) / len(graded):.0f}%) -- clamp-artifact context for the widths")

    both = [r for r in rows if r["self_grade"] and r["word"]]
    if both:
        agree = sum(1 for r in both if r["self_grade"].upper() == r["word"].upper())
        print(f"\nengine self-grade vs game banner (diagnostic; the self-grade is NOT truth):"
              f" agree {agree}/{len(both)}")
        for r in both:
            flag = "" if r["self_grade"].upper() == r["word"].upper() else "   <-- disagree"
            print(f"  seq={r['rel_seq']:>3} banner={r['word']:14s}({r['color']}) "
                  f"self={r['self_grade']}{flag}")
    print("\nindependence note: the regressor (green window width, engine reader at release)"
          "\nand the response (banner verdict, game HUD pixels ~3 s later) come from different"
          "\ninstruments; neither is computed from the other, so the algebraic-circularity"
          "\nfailure mode (B = f(phi) regressed on phi) does not apply here.")


# ---------------------------------------------------------------- review
def cmd_review(args):
    """Owner-verification table: one row per shot, sorted by reader confidence
    ASCENDING (least-sure rows on top, where a human reviewer actually looks),
    with a BET flag on every row the reader would stake a count on. If the table
    is large, emits the lowest-confidence rows plus a random spot-check sample of
    high-confidence ones (a reviewer who only sees uncertain rows cannot catch
    confident-but-wrong errors)."""
    import random
    rows = []
    for jf in args.joined:
        with open(jf, newline="") as f:
            rows.extend(csv.DictReader(f))
    banner_rows = [r for r in rows if r["word"]]
    rel_only = [r for r in rows if not r["word"] and r["release_utc"]]

    def conf(r):
        try:
            return float(r["word_score"] or 0)
        except ValueError:
            return 0.0

    def bet(r):
        if r["word"] in ("", "UNKNOWN") or conf(r) < 0.9:
            return "NO-BET"
        if int(r["n_frames"] or 0) < 5:
            return "NO-BET"
        if r["join_status"] == "matched":
            d = float(r["delta_banner_s"] or 0)
            if not (0.3 <= d <= 3.2):
                return "NO-BET"
        return "bet"

    banner_rows.sort(key=conf)
    if len(banner_rows) > args.max_rows:
        low = banner_rows[:args.max_rows - 20]
        high = banner_rows[args.max_rows - 20:]
        random.seed(20260806)
        banner_rows = low + sorted(random.sample(high, 20), key=conf)
        note = (f"(showing {len(low)} lowest-confidence rows + random spot-check "
                f"of 20 high-confidence rows out of {len(rows)})")
    else:
        note = f"(all {len(banner_rows)} banner rows)"

    cols = ["flag", "conf", "verdict", "color", "seq", "banner_utc", "join_status",
            "n_frames", "norm_ms", "green_win_pp", "fill", "rung", "shot"]

    def to_row(r):
        return {
            "flag": bet(r), "conf": f"{conf(r):.2f}",
            "verdict": r["word"] or "-", "color": r["color"] or "-",
            "seq": r["rel_seq"] or "-",
            "banner_utc": (r["banner_onset_utc"] or r["release_utc"] or "")[11:19],
            "join_status": r["join_status"],
            "n_frames": r["n_frames"] or "-",
            "norm_ms": r["phase_normalized_ms"] or "-",
            "green_win_pp": r["green_width_pp"][:4] if r["green_width_pp"] else "-",
            "fill": r["fill_at_release"] or "-",
            "rung": r["resv_anchor_pct"] or "-",
            "shot": r["shot"] or "-",
        }

    out_rows = [to_row(r) for r in banner_rows]
    tail_rows = [to_row(r) for r in rel_only]
    with open(args.out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows + tail_rows)
    widths = {c: max(len(c), *(len(str(r[c])) for r in out_rows + tail_rows)) for c in cols}
    lines = [f"BANNER VERDICTS, least-confident first {note}",
             "  ".join(c.ljust(widths[c]) for c in cols),
             "  ".join("-" * widths[c] for c in cols)]
    lines += ["  ".join(str(r[c]).ljust(widths[c]) for c in cols) for r in out_rows]
    if tail_rows:
        lines += ["", "ENGINE RELEASES WITH NO BANNER READ (frames absent or refused):"]
        lines += ["  ".join(str(r[c]).ljust(widths[c]) for c in cols) for r in tail_rows]
    txt = "\n".join(lines) + "\n"
    with open(args.out_txt, "w") as f:
        f.write(txt)
    print(txt)
    print(f"-> {args.out_csv}\n-> {args.out_txt}")


# ---------------------------------------------------------------- corrections loop
CORR_FIELDS = ["source", "event_id", "time", "verdict", "color", "confidence",
               "n_frames", "crop", "corrected_word", "corrected_color", "note"]


def cmd_corrections(args):
    """Owner labelling loop, step 1: emit a correctable CSV (lowest confidence
    first, plus a random high-confidence spot-check) with per-event crop images
    copied next to it, so the owner can eyeball pixels without scrubbing video.
    He fills corrected_word/corrected_color ONLY on rows the reader got wrong
    (blank = reader was right). Step 2 is `refit --corrections <file>`."""
    import random, shutil
    events = []
    for evf in args.events:
        with open(evf, newline="") as f:
            events.extend(csv.DictReader(f))
    crops_out = os.path.join(os.path.dirname(os.path.abspath(args.out)), "crops")
    os.makedirs(crops_out, exist_ok=True)

    def conf(r):
        try:
            return float(r["word_score"] or 0)
        except ValueError:
            return 0.0

    events.sort(key=conf)
    if args.max_rows and len(events) > args.max_rows:
        low = events[:args.max_rows - args.spot_check]
        high = events[args.max_rows - args.spot_check:]
        random.seed(20260806)
        events = low + sorted(random.sample(high, min(args.spot_check, len(high))), key=conf)
    rows = []
    for ev in events:
        crop_rel = ""
        if ev["crop_path"] and os.path.exists(ev["crop_path"]):
            base = os.path.basename(ev["crop_path"])
            dst = os.path.join(crops_out, base)
            if not os.path.exists(dst):
                shutil.copy2(ev["crop_path"], dst)
            crop_rel = os.path.join("crops", base)
        rows.append({
            "source": ev["source"], "event_id": ev["event_id"],
            "time": ev["onset_time"], "verdict": ev["word"] or "UNREAD",
            "color": ev["color"], "confidence": ev["word_score"],
            "n_frames": ev["n_frames"], "crop": crop_rel,
            "corrected_word": "", "corrected_color": "", "note": "",
        })
    with open(args.out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CORR_FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} rows -> {args.out} (crops in {crops_out})")
    print("Owner instructions: fill corrected_word (and corrected_color if wrong) ONLY on")
    print("rows where the reader's verdict is wrong; leave correct rows blank. Then run:")
    print(f"  python tools/timing/banner_reader.py refit --corrections {args.out}")


def cmd_refit(args):
    """Owner labelling loop, step 2: ingest the corrected CSV. Every corrected row
    contributes its crop's value mask as a NEW word template (so the reader learns
    the owner's label), then the whole corrections file is re-classified and the
    before/after agreement is printed. Templates land in banner_templates/ as
    word_<WORD>_r<n>.png; delete those files to undo a refit."""
    label_tmpl = load_label_template()
    with open(args.corrections, newline="") as f:
        rows = list(csv.DictReader(f))
    base_dir = os.path.dirname(os.path.abspath(args.corrections))
    corrected = [r for r in rows if r["corrected_word"].strip()]
    print(f"{len(rows)} rows, {len(corrected)} corrections")
    added = 0
    for r in corrected:
        word = r["corrected_word"].strip().upper().replace(" ", "_")
        crop_p = os.path.join(base_dir, r["crop"]) if r["crop"] else ""
        crop = cv2.imread(crop_p) if crop_p else None
        if crop is None:
            print(f"  SKIP {r['event_id']}: crop missing ({r['crop']})")
            continue
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if g.shape[0] < label_tmpl.shape[0] or g.shape[1] < label_tmpl.shape[1]:
            continue
        res = cv2.matchTemplate(g, label_tmpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx < LABEL_THR:
            print(f"  SKIP {r['event_id']}: no label in crop (NCC {mx:.2f})")
            continue
        got = extract_value_mask(crop, loc[0], loc[1])
        if got is None:
            print(f"  SKIP {r['event_id']}: no value mask in crop")
            continue
        n = 0
        while os.path.exists(os.path.join(TEMPLATE_DIR, f"word_{word}_r{n}.png")):
            n += 1
        cv2.imwrite(os.path.join(TEMPLATE_DIR, f"word_{word}_r{n}.png"), got[0])
        added += 1
        print(f"  + template word_{word}_r{n}.png from {r['event_id']}")
    word_tmpls = load_word_templates()
    ok_before = ok_after = n_eval = 0
    for r in rows:
        truth = (r["corrected_word"].strip() or r["verdict"]).upper().replace(" ", "_")
        if truth in ("", "UNREAD", "UNKNOWN"):
            continue
        crop_p = os.path.join(base_dir, r["crop"]) if r["crop"] else ""
        crop = cv2.imread(crop_p) if crop_p else None
        if crop is None:
            continue
        g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        res = cv2.matchTemplate(g, label_tmpl, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(res)
        if mx < LABEL_THR:
            continue
        got = extract_value_mask(crop, loc[0], loc[1])
        if got is None:
            continue
        word, score, _ = classify_word(got[0], word_tmpls)
        n_eval += 1
        ok_before += (r["verdict"].upper().replace(" ", "_") == truth)
        ok_after += (word == truth)
    print(f"\n{added} templates added; crop-level agreement with owner labels: "
          f"before {ok_before}/{n_eval}, after {ok_after}/{n_eval}")
    print("Re-run `scan` on new sessions to use the refit templates.")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="scan frames -> banner events CSV")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--session", help="framedump session dir (PNG frames, mtime = wall time)")
    g.add_argument("--video", help="mp4 recording (no wall time -> validation corpus only)")
    s.add_argument("--stride", type=int, default=2, help="video frame stride (default 2)")
    s.add_argument("--out", required=True)
    s.add_argument("--crops", help="dir for per-event panel crops (hand-labelling)")
    s.add_argument("--min-frames", type=int, default=2,
                   help="drop events seen on fewer frames (default 2; use 1 for sparse dumps)")
    s.add_argument("--auto-scale", action="store_true",
                   help="video only: calibrate for windowed captures (game inside an "
                        "app preview at non-native scale); no-op when fixed scale works")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("labelsheet", help="numbered crop sheets for blind hand-labelling")
    s.add_argument("--events", nargs="+", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--per-sheet", type=int, default=24)
    s.add_argument("--cols", type=int, default=3)
    s.set_defaults(fn=cmd_labelsheet)

    s = sub.add_parser("validate", help="confusion matrix vs hand labels")
    s.add_argument("--events", nargs="+", required=True)
    s.add_argument("--labels", required=True, help="CSV: event_id,word[,color]")
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("join", help="join banner events to engine per-shot records")
    s.add_argument("--events", nargs="+", required=True)
    s.add_argument("--log", nargs="+", required=True, help="orion_native.log path(s)")
    s.add_argument("--calib-session", nargs="*", default=[],
                   help="framedump session dir(s) for per-session mtime-lag calibration")
    s.add_argument("--out", required=True)
    s.set_defaults(fn=cmd_join)

    s = sub.add_parser("analyze", help="green-window width vs banner verdict")
    s.add_argument("--joined", nargs="+", required=True)
    s.add_argument("--narrow-pp", type=float, default=3.0)
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("review", help="owner-verification table (least-confident first)")
    s.add_argument("--joined", nargs="+", required=True)
    s.add_argument("--out-csv", required=True)
    s.add_argument("--out-txt", required=True)
    s.add_argument("--max-rows", type=int, default=50)
    s.set_defaults(fn=cmd_review)

    s = sub.add_parser("corrections", help="emit owner-correctable CSV + crops")
    s.add_argument("--events", nargs="+", required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--max-rows", type=int, default=0, help="0 = all rows")
    s.add_argument("--spot-check", type=int, default=20)
    s.set_defaults(fn=cmd_corrections)

    s = sub.add_parser("refit", help="ingest owner corrections -> new templates + re-report")
    s.add_argument("--corrections", required=True)
    s.set_defaults(fn=cmd_refit)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
