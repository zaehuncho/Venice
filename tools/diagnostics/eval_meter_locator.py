#!/usr/bin/env python3
"""Validate the trained meter LOCATOR before enabling it live. Two metrics that decide the ship:
  1. RECALL on real meter-present frames (meter_poc/val + meter_real_park/val) — does it FIND the meter?
  2. FALSE-POSITIVE rate on NO-METER distractor frames (the synthetic hard-negatives = real park lobby /
     menu / EA-FC backgrounds with no meter) — does it REJECT random red objects? (the ship blocker)
Also saves montages of detections + any false positives so the behaviour is eyeballable.

USAGE:
  C:\\Python314\\python.exe tools/diagnostics/eval_meter_locator.py [--model models/orion_meter_n.pt]
      [--conf 0.35] [--imgsz 640]
"""
import argparse, glob, os, random
import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.environ.get("TEMP", "."), "meter_locator_eval")


def read_list(p, base):
    if not os.path.isfile(p):
        return []
    out = []
    for ln in open(p, encoding="utf-8"):
        s = ln.strip()
        if s:
            out.append(s if os.path.isabs(s) else os.path.normpath(os.path.join(base, s)))
    return out


def gt_for(img_path):
    lp = img_path.replace(os.sep + "images" + os.sep, os.sep + "labels" + os.sep)
    lp = os.path.splitext(lp)[0] + ".txt"
    boxes = []
    if os.path.isfile(lp):
        for ln in open(lp):
            p = ln.split()
            if len(p) == 5:
                boxes.append(tuple(map(float, p[1:])))   # cx,cy,w,h normalized
    return boxes


def overlaps(pred_xywh, gts, W, H):
    px, py, pw, ph = pred_xywh
    pcx, pcy = px + pw / 2, py + ph / 2
    for (cx, cy, w, h) in gts:
        gx, gy, gw, gh = cx * W, cy * H, w * W, h * H
        # a hit = pred centre within the (padded) GT box OR GT centre within the pred box
        if (abs(pcx - gx) < gw * 0.75 + 20 and abs(pcy - gy) < gh * 0.75 + 20):
            return True
    return False


# --------------------------------------------------------------------------- #
#  --meta: PER-PHASE / PER-RUNG recall (a release-window hole hid behind an
#  aggregate recall number until a live 0/28 batch — so grade each shot phase).
# --------------------------------------------------------------------------- #
PHASES = ("rising-early", "rising-late", "peak", "spent")


def load_meta_csv(path):
    """Load the dual-capture aligner labels.csv (columns pts_s,fill,x,y,w,h,shot_id) into a dict
    of numpy arrays. fill is NaN outside labeled spans; shot_id is -1 outside shots."""
    import csv
    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    def col(name, cast):
        return np.array([cast(r[name]) for r in rows]) if rows else np.array([])

    return {
        "pts_s": col("pts_s", float),
        "fill": col("fill", float),               # "nan" -> np.nan
        "x": col("x", float), "y": col("y", float),
        "w": col("w", float), "h": col("h", float),
        "shot_id": col("shot_id", lambda v: int(float(v))),
    }


def compute_shot_spans(pts_s, fill, shot_id):
    """From aligned per-frame arrays, return {shot_id: (t_start, max_fill, t_max)} for each real
    shot (shot_id >= 0), computed over that shot's LABELED frames (fill not NaN). t_start is the
    span start; t_max is the pts of the shot's max-fill frame."""
    pts_s = np.asarray(pts_s, float)
    fill = np.asarray(fill, float)
    shot_id = np.asarray(shot_id)
    spans = {}
    for sid in np.unique(shot_id):
        if int(sid) < 0:                          # -1 = outside any shot
            continue
        m = (shot_id == sid) & np.isfinite(fill)
        if not m.any():
            continue
        t = pts_s[m]
        f = fill[m]
        imax = int(np.argmax(f))
        spans[int(sid)] = (float(t.min()), float(f[imax]), float(t[imax]))
    return spans


def phase_of(pts_s, fill, shot_span, early_ms=120.0, peak_fill=85.0, spent_drop_pp=8.0):
    """Bucket ONE labeled frame into rising-early / rising-late / peak / spent.

    shot_span = (t_start, max_fill, t_max) for the frame's shot (from compute_shot_spans).
    Precedence (a frame can satisfy several definitions; most-specific wins):
      1. rising-early  first `early_ms` of the shot's labeled span (regardless of fill)
      2. spent         AFTER the max-fill frame AND fill dropped > `spent_drop_pp` from max
      3. peak          fill >= `peak_fill` while the shot is active
      4. rising-late   the rest (fill < peak_fill on the way up)"""
    t_start, max_fill, t_max = shot_span
    if (pts_s - t_start) < (early_ms / 1000.0):
        return "rising-early"
    if pts_s > t_max and (max_fill - fill) > spent_drop_pp:
        return "spent"
    if fill >= peak_fill:
        return "peak"
    return "rising-late"


def bucket_phases(pts_s, fill, shot_id, **kw):
    """Assign every LABELED frame (shot_id >= 0 and fill not NaN) a phase. Returns an object array
    aligned to the inputs; unlabeled / inter-shot frames get "" (empty string)."""
    pts_s = np.asarray(pts_s, float)
    fill = np.asarray(fill, float)
    shot_id = np.asarray(shot_id)
    spans = compute_shot_spans(pts_s, fill, shot_id)   # phase thresholds (**kw) go to phase_of only
    out = np.full(pts_s.shape, "", dtype=object)
    for i in range(pts_s.size):
        sid = int(shot_id[i])
        if sid < 0 or not np.isfinite(fill[i]) or sid not in spans:
            continue
        out[i] = phase_of(float(pts_s[i]), float(fill[i]), spans[sid], **kw)
    return out


def format_meta_report(rung, phase_stats, conf=None, imgsz=None, stubbed=False):
    """Render the per-phase recall table. phase_stats: {phase: (hits, total)}. `rung` (or None)
    prefixes every DATA line so runs for different rungs concatenate into one comparable table.
    Returns a list of lines. OVERALL is summed across phases."""
    tag = rung if rung else "-"
    lines = []
    hdr = "# METER LOCATOR PER-PHASE EVAL"
    if conf is not None or imgsz is not None:
        hdr += f" (conf={conf} imgsz={imgsz})"
    if stubbed:
        hdr += "  [inference STUBBED -- recall is a placeholder]"
    lines.append(hdr)
    lines.append(f"{'rung':<12} {'phase':<13} {'recall':>7}  hit/total")
    tot_hit = tot_tot = 0
    for ph in PHASES:
        hit, total = phase_stats.get(ph, (0, 0))
        tot_hit += hit
        tot_tot += total
        rec = (hit / total * 100.0) if total else 0.0
        lines.append(f"{tag:<12} {ph:<13} {rec:6.1f}%  {hit}/{total}")
    orec = (tot_hit / tot_tot * 100.0) if tot_tot else 0.0
    lines.append(f"{tag:<12} {'OVERALL':<13} {orec:6.1f}%  {tot_hit}/{tot_tot}")
    return lines


def meta_detect_hits(cols, args):
    """Inference hook: return (hits, stubbed).

    `hits` is a bool np.ndarray aligned to cols['pts_s'] where True = the detector's predicted
    box on that frame overlaps the labels.csv GT box; `stubbed` is False once real inference ran.

    labels.csv does NOT carry frame paths, so the caller must pass --frames-dir: a directory of
    f%05d.png frames whose index maps to pts_s via --ms-per-idx (the framedump cadence; the
    build_rung_luma_dataset.py --eval writer uses exactly this naming/pts convention). GT cols
    x,y,w,h are ABSOLUTE pixels in those frames. Without --frames-dir the historical stub
    (all-miss placeholder) is preserved."""
    frames_dir = getattr(args, "frames_dir", None)
    if not frames_dir:
        return None, True
    frames_dir = frames_dir if os.path.isabs(frames_dir) else os.path.join(ROOT, frames_dir)
    model_path = args.model if os.path.isabs(args.model) else os.path.join(ROOT, args.model)
    if not os.path.exists(model_path):
        raise SystemExit(f"model not found: {model_path}")
    from ultralytics import YOLO
    m = YOLO(model_path)
    ms = float(getattr(args, "ms_per_idx", 64.6))
    pts = np.asarray(cols["pts_s"], float)
    fill = np.asarray(cols["fill"], float)
    sid = np.asarray(cols["shot_id"])
    hits = np.zeros(pts.shape, dtype=bool)
    for k in range(pts.size):
        if sid[k] < 0 or not np.isfinite(fill[k]):
            continue                                   # unlabeled frame: never graded
        idx = int(round(pts[k] * 1000.0 / ms))
        fp = os.path.join(frames_dir, f"f{idx:05d}.png")
        im = cv2.imread(fp)
        if im is None:
            continue                                   # missing frame counts as a miss
        H, W = im.shape[:2]
        gx, gy, gw, gh = (float(cols[c][k]) for c in ("x", "y", "w", "h"))
        if gw <= 0 or gh <= 0:
            continue
        gts = [((gx + gw / 2) / W, (gy + gh / 2) / H, gw / W, gh / H)]  # overlaps() convention
        r = m.predict(im, imgsz=args.imgsz, conf=args.conf, verbose=False)[0]
        b = r.boxes
        if b is None or len(b) == 0:
            continue
        for (x1, y1, x2, y2) in b.xyxy.cpu().numpy():
            if overlaps((x1, y1, x2 - x1, y2 - y1), gts, W, H):
                hits[k] = True
                break
    return hits, False


def run_meta(args):
    """--meta mode: bucket the aligner labels.csv into phases and print a per-phase/per-rung recall
    table. Detector inference is a clearly-marked stub (see meta_detect_hits)."""
    meta_path = args.meta if os.path.isabs(args.meta) else os.path.join(ROOT, args.meta)
    if not os.path.isfile(meta_path):
        raise SystemExit(f"--meta labels csv not found: {meta_path}")
    cols = load_meta_csv(meta_path)
    phases = bucket_phases(cols["pts_s"], cols["fill"], cols["shot_id"])
    labeled = phases != ""
    hits, stubbed = meta_detect_hits(cols, args)          # STUB unless real inference is wired
    phase_stats = {}
    for ph in PHASES:
        m = labeled & (phases == ph)
        total = int(m.sum())
        hit = int((np.asarray(hits, bool) & m).sum()) if hits is not None else 0
        phase_stats[ph] = (hit, total)
    print("\n".join(format_meta_report(args.rung, phase_stats,
                                       conf=args.conf, imgsz=args.imgsz, stubbed=stubbed)))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=os.path.join("models", "orion_meter_n.pt"))
    ap.add_argument("--conf", type=float, default=0.35)
    ap.add_argument("--imgsz", type=int, default=768)   # must match training imgsz
    ap.add_argument("--neg", type=int, default=400, help="how many no-meter frames to test for false positives")
    ap.add_argument("--meta", default=None, metavar="LABELS_CSV",
                    help="dual-capture aligner labels.csv -> per-phase (rising-early/rising-late/peak/"
                         "spent) + per-rung recall table instead of the aggregate eval")
    ap.add_argument("--rung", default=None,
                    help="rung name prefixed on every --meta report line so multiple runs concatenate")
    ap.add_argument("--frames-dir", default=None,
                    help="--meta only: dir of f%%05d.png frames (build_rung_luma_dataset --eval) "
                         "to run REAL inference; omitted -> the historical stubbed placeholder")
    ap.add_argument("--ms-per-idx", type=float, default=64.6,
                    help="--meta only: framedump cadence mapping pts_s -> frame index")
    args = ap.parse_args()

    if args.meta:
        return run_meta(args)

    model_path = args.model if os.path.isabs(args.model) else os.path.join(ROOT, args.model)
    if not os.path.exists(model_path):
        raise SystemExit(f"model not found: {model_path} (train it first)")
    os.makedirs(OUT, exist_ok=True)
    from ultralytics import YOLO
    m = YOLO(model_path)

    def predict(img):
        r = m.predict(img, imgsz=args.imgsz, conf=args.conf, verbose=False)[0]
        b = r.boxes
        if b is None or len(b) == 0:
            return []
        return b.xyxy.cpu().numpy()

    # ---- 1) RECALL on meter-present val ----
    val = (read_list(os.path.join(ROOT, "datasets/meter_poc/val.txt"), os.path.join(ROOT, "datasets/meter_poc"))
           + read_list(os.path.join(ROOT, "datasets/meter_real_park/val.txt"), os.path.join(ROOT, "datasets/meter_real_park")))
    hit = miss = 0
    tp_tiles = []
    for ip in val:
        gts = gt_for(ip)
        if not gts:
            continue
        im = cv2.imread(ip)
        if im is None:
            continue
        H, W = im.shape[:2]
        preds = predict(im)
        got = any(overlaps((x1, y1, x2 - x1, y2 - y1), gts, W, H) for (x1, y1, x2, y2) in preds)
        hit += int(got); miss += int(not got)
        if got and len(tp_tiles) < 8:
            vis = im.copy()
            for (x1, y1, x2, y2) in preds:
                cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 255, 0), 2)
            tp_tiles.append(cv2.resize(vis, (480, 270)))
    recall = hit / max(1, hit + miss)

    # ---- 2) FALSE POSITIVES on no-meter distractor frames (synthetic hard-negatives) ----
    # Use the synth VAL split only — training never sees these files, so the FP rate is honest
    # (train negatives would be overfit-blind). Fall back to the full dir if val.txt is missing.
    synth_dir = os.path.join(ROOT, "datasets", "meter_fullframe_synth")
    synth_val = read_list(os.path.join(synth_dir, "val.txt"), synth_dir)
    if not synth_val:
        synth_val = glob.glob(os.path.join(synth_dir, "images", "*.jpg"))
    neg_pool = [f for f in synth_val
                if not gt_for(f)]   # empty label = a real distractor background with NO meter pasted
    random.Random(0).shuffle(neg_pool)
    neg_pool = neg_pool[:args.neg]
    fp = 0
    fp_tiles = []
    for ip in neg_pool:
        im = cv2.imread(ip)
        if im is None:
            continue
        preds = predict(im)
        if len(preds) > 0:
            fp += 1
            if len(fp_tiles) < 8:
                vis = im.copy()
                for (x1, y1, x2, y2) in preds:
                    cv2.rectangle(vis, (int(x1), int(y1)), (int(x2), int(y2)), (0, 0, 255), 2)
                fp_tiles.append(cv2.resize(vis, (480, 270)))
    fp_rate = fp / max(1, len(neg_pool))

    def montage(tiles, name):
        if not tiles:
            return
        rows = [np.hstack(tiles[i:i + 2]) for i in range(0, len(tiles) - len(tiles) % 2, 2)]
        if rows:
            cv2.imwrite(os.path.join(OUT, name), np.vstack(rows))

    montage(tp_tiles, "eval_detections.png")
    montage(fp_tiles, "eval_false_positives.png")
    print(f"\n=== METER LOCATOR EVAL (conf={args.conf} imgsz={args.imgsz}) ===")
    print(f"RECALL   (real meter-present): {recall*100:.1f}%  ({hit}/{hit+miss})")
    print(f"FALSE-POS (no-meter frames)  : {fp_rate*100:.1f}%  ({fp}/{len(neg_pool)})  <- ship blocker: want LOW")
    print(f"montages -> {OUT}")


if __name__ == "__main__":
    main()
