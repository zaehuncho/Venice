#!/usr/bin/env python3
"""Ship-gate evaluation harness for Orion's NBA 2K meter detection.

Gates the retrained YOLO meter LOCATOR (models/orion_meter_n.pt) and the classical park
detector (meter_detector.MeterDetector) before either may drive live timing. Replaces the
leaked-pseudo-label eval in eval_meter_locator.py with:
  * a manual, human-verified val set (datasets/meter_val_manual/val_manual) when available
  * the cleaned pseudo-label val split (clean_val.txt) as a second, separate set
  * a real-negative FP set (val_manual/negatives; UNVETTED fallback: _work/neg_frames)
  * per-x-bin + per-height-tercile recall breakdowns (edge fades are the known weak spot)
  * temporal metrics over the two HELD-OUT raw val clips (lock rate / lock latency /
    phantom rate / fill monotonicity)

MATCHING: label box widths are FAKE/synthetic, so IoU is INVALID. A prediction "hits" a GT
box iff |pred_cx-gt_cx| < 0.75*gt_w+tol AND |pred_cy-gt_cy| < 0.75*gt_h+tol (center-hit,
same rule as eval_meter_locator.py; tol default 20px).

SUBCOMMANDS
  boxes      static YOLO locator eval over manual val + clean_val (+ FP over negatives)
  classical  same metrics driving MeterDetector.detect() (fresh detector per clip,
             frames fed in clip order so the temporal park tracker works) + mean ms
  clips      temporal eval over the raw held-out val clip mp4s (classical always,
             locator too when --model is given). Shot spans derived from the cleaned
             pseudo-label dataset indices (runs with gaps<=5; video_frame = 2*idx).
  gates      boxes+clips for one target, PASS/FAIL against the ship thresholds:
             overall recall>=0.97, edge-bin recall>=0.90, FP<=0.5%,
             per-shot lock>=99%, phantom<=2 episodes/min. Exit 0 only if ALL pass.

Detections are cached (JSON under logs/diagnostics/eval_gates/cache) keyed on the exact
model/detector signature + image/clip list, so re-runs are cheap. --no-cache bypasses.
Disk I/O is kept modest: no frame dumps, sequential video reads only.

PYTHON: C:/Python314/python.exe for `classical` (cv2-only). For `boxes`/`clips --model`/
`gates` use .venv311/Scripts/python.exe -- on 2026-07-02 a torch 2.12.1+cpu install into
C:/Python314 broke its torchvision 0.26.0+cu126 pairing (ultralytics import dies with
"operator torchvision::nms does not exist" and CUDA is gone); .venv311 has a matched
torch 2.12.1+cpu / torchvision 0.27.1+cpu / ultralytics 8.4.83.

USAGE (examples)
  C:/Python314/python.exe tools/diagnostics/eval_detection_gates.py boxes --model models/orion_meter_n.pt
  C:/Python314/python.exe tools/diagnostics/eval_detection_gates.py classical --limit 300
  C:/Python314/python.exe tools/diagnostics/eval_detection_gates.py clips --clip 20260624212347 --max-frames 3000
  C:/Python314/python.exe tools/diagnostics/eval_detection_gates.py gates --model models/orion_meter_n.pt
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

OUT_DIR = os.path.join(ROOT, "logs", "diagnostics", "eval_gates")
CACHE_DIR = os.path.join(OUT_DIR, "cache")

MANUAL_DIR = os.path.join(ROOT, "datasets", "meter_val_manual", "val_manual")
CLEAN_VAL = os.path.join(ROOT, "datasets", "meter_val_manual", "clean_val.txt")
CLEAN_TRAIN = os.path.join(ROOT, "datasets", "meter_val_manual", "clean_train.txt")
FALLBACK_NEG_DIR = os.path.join(ROOT, "datasets", "meter_val_manual", "_work", "neg_frames")
DEFAULT_VIDEOS_DIR = "C:/Users/Administrator/Videos"
VAL_CLIPS = ["20260624212347", "20260624212008"]

# dataset frames were extracted with step 2 at 1280x720 -> video_frame = 2 * dataset_idx
FRAME_STEP = 2
PROC_W, PROC_H = 1280, 720

X_BINS: List[Tuple[float, float]] = [(0.0, 0.15), (0.15, 0.35), (0.35, 0.65), (0.65, 0.85), (0.85, 1.000001)]
EDGE_BIN_IDX = (0, 4)

GATE_RECALL = 0.97
GATE_EDGE_RECALL = 0.90
GATE_FP = 0.005
GATE_LOCK = 0.99
GATE_PHANTOM_PER_MIN = 2.0

_FRAME_RE = re.compile(r"NBA_2K26_(\d+)_v?f(\d+)", re.IGNORECASE)


# --------------------------------------------------------------------------------------
# generic helpers
# --------------------------------------------------------------------------------------

def _die(msg: str) -> None:
    raise SystemExit(f"ERROR: {msg}")


def file_sig(path: str) -> List[int]:
    # list (not tuple): cache descs are compared against their JSON round-trip
    st = os.stat(path)
    return [st.st_size, int(st.st_mtime)]


def read_list(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    out = []
    for ln in open(path, encoding="utf-8"):
        s = ln.strip()
        if s:
            out.append(os.path.normpath(s))
    return out


def list_images(d: str) -> List[str]:
    if not os.path.isdir(d):
        return []
    out = [os.path.join(d, f) for f in sorted(os.listdir(d))
           if f.lower().endswith((".jpg", ".jpeg", ".png"))]
    return out


def gt_boxes_for(img_path: str) -> List[Tuple[float, float, float, float]]:
    """YOLO label boxes (normalized cx,cy,w,h) for an image (labels/ sibling of images/)."""
    p = os.path.normpath(img_path)
    lp = p.replace(os.sep + "images" + os.sep, os.sep + "labels" + os.sep)
    lp = os.path.splitext(lp)[0] + ".txt"
    boxes = []
    if os.path.isfile(lp):
        for ln in open(lp, encoding="utf-8"):
            parts = ln.split()
            if len(parts) == 5:
                boxes.append(tuple(map(float, parts[1:])))
    return boxes


def center_hit(pred_xywh: Sequence[float], gt: Tuple[float, float, float, float],
               W: int, H: int, tol_px: float) -> bool:
    """Center-hit match (label widths are FAKE -> IoU invalid)."""
    px, py, pw, ph = pred_xywh
    pcx, pcy = px + pw / 2.0, py + ph / 2.0
    cx, cy, w, h = gt
    gx, gy, gw, gh = cx * W, cy * H, w * W, h * H
    return abs(pcx - gx) < gw * 0.75 + tol_px and abs(pcy - gy) < gh * 0.75 + tol_px


def parse_clip_frame(path: str) -> Optional[Tuple[str, int]]:
    m = _FRAME_RE.search(os.path.basename(path))
    if not m:
        return None
    return m.group(1), int(m.group(2))


def group_by_clip(images: Sequence[str]) -> Dict[str, List[Tuple[int, str]]]:
    """clip_id -> [(frame_idx, path), ...] sorted by frame idx. Unparseable names go in ''. """
    groups: Dict[str, List[Tuple[int, str]]] = {}
    for i, p in enumerate(images):
        cf = parse_clip_frame(p)
        if cf is None:
            groups.setdefault("", []).append((i, p))
        else:
            groups.setdefault(cf[0], []).append((cf[1], p))
    for k in groups:
        groups[k].sort(key=lambda t: t[0])
    return groups


# --------------------------------------------------------------------------------------
# caching
# --------------------------------------------------------------------------------------

def cache_path(desc: Dict[str, Any]) -> str:
    key = hashlib.sha1(json.dumps(desc, sort_keys=True).encode("utf-8")).hexdigest()[:20]
    return os.path.join(CACHE_DIR, f"{desc.get('kind','x')}_{key}.json")


def cache_load(desc: Dict[str, Any], no_cache: bool) -> Optional[Any]:
    if no_cache:
        return None
    desc = json.loads(json.dumps(desc))   # normalize tuples->lists for the equality check
    p = cache_path(desc)
    if os.path.isfile(p):
        try:
            with open(p, encoding="utf-8") as f:
                blob = json.load(f)
            if blob.get("desc") == desc:
                print(f"  [cache hit] {os.path.basename(p)}")
                return blob["data"]
        except Exception:
            return None
    return None


def cache_save(desc: Dict[str, Any], data: Any) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    p = cache_path(desc)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"desc": desc, "data": data}, f)
    os.replace(tmp, p)


# --------------------------------------------------------------------------------------
# runners
# --------------------------------------------------------------------------------------

class YoloRunner:
    """Thin YOLO wrapper matching MeterLocator semantics (argmax-conf box)."""

    def __init__(self, model_path: str, imgsz: int, conf: float):
        if not os.path.isfile(model_path):
            _die(f"locator model not found: {model_path}")
        self.model_path = os.path.abspath(model_path)
        self.imgsz, self.conf = int(imgsz), float(conf)
        import torch
        from ultralytics import YOLO
        self.device = 0 if torch.cuda.is_available() else "cpu"
        self.model = YOLO(self.model_path)
        self.model.predict(np.zeros((32, 32, 3), np.uint8), imgsz=self.imgsz,
                           conf=self.conf, device=self.device, verbose=False)

    def sig(self) -> Dict[str, Any]:
        return {"model": self.model_path, "model_sig": file_sig(self.model_path),
                "imgsz": self.imgsz, "conf": self.conf}

    def predict(self, img: np.ndarray) -> List[List[float]]:
        r = self.model.predict(img, imgsz=self.imgsz, conf=self.conf,
                               device=self.device, verbose=False)[0]
        b = getattr(r, "boxes", None)
        if b is None or len(b) == 0:
            return []
        xyxy = b.xyxy.cpu().numpy()
        confs = b.conf.cpu().numpy()
        return [[float(x1), float(y1), float(x2 - x1), float(y2 - y1), float(c)]
                for (x1, y1, x2, y2), c in zip(xyxy, confs)]


def build_classical_detector():
    """Construct MeterDetector exactly the way remote_play_orchestrator does for park mode."""
    os.environ["ORION_METER_LOCATOR"] = "0"    # classical must stay pure classical
    from meter_detector import DetectorConfig, MeterDetector
    cfg = DetectorConfig()
    cfg.park_temporal_enabled = True
    cfg.meter_color = "Red"
    cfg.auto_meter_color = False
    styles_dir = os.path.join(ROOT, "meter_styles")
    return MeterDetector(styles_dir, cfg)


def classical_sig() -> Dict[str, Any]:
    return {"detector": "classical_park_red",
            "meter_detector_sig": file_sig(os.path.join(ROOT, "meter_detector.py"))}


# --------------------------------------------------------------------------------------
# static-set evaluation (boxes / classical share this)
# --------------------------------------------------------------------------------------

def run_yolo_on_images(runner: YoloRunner, images: List[str], tag: str,
                       no_cache: bool) -> Dict[str, Dict[str, Any]]:
    desc = {"kind": "yolo_imgs", "tag": tag, **runner.sig(),
            "images_sha": hashlib.sha1("\n".join(images).encode()).hexdigest(), "n": len(images)}
    cached = cache_load(desc, no_cache)
    if cached is not None:
        return cached
    preds: Dict[str, Dict[str, Any]] = {}
    t0 = time.perf_counter()
    for i, ip in enumerate(images):
        im = cv2.imread(ip)
        if im is None:
            preds[ip] = {"wh": None, "p": []}
        else:
            preds[ip] = {"wh": [im.shape[1], im.shape[0]], "p": runner.predict(im)}
        if (i + 1) % 200 == 0:
            print(f"  yolo {tag}: {i+1}/{len(images)} ({time.perf_counter()-t0:.0f}s)")
    cache_save(desc, preds)
    return preds


def run_classical_on_images(images: List[str], tag: str,
                            no_cache: bool) -> Tuple[Dict[str, Dict[str, Any]], float]:
    """Feed images grouped by clip, in frame order, fresh detector per clip.
    Returns preds map (bbox list, 0 or 1 entries) + mean detect() ms."""
    desc = {"kind": "classical_imgs", "tag": tag, **classical_sig(),
            "images_sha": hashlib.sha1("\n".join(images).encode()).hexdigest(), "n": len(images)}
    cached = cache_load(desc, no_cache)
    if cached is not None:
        return cached["preds"], cached["mean_ms"]
    groups = group_by_clip(images)
    preds: Dict[str, Dict[str, Any]] = {}
    total_ms, n_calls = 0.0, 0
    for clip_id, items in sorted(groups.items()):
        det = build_classical_detector()
        for _, ip in items:
            im = cv2.imread(ip)
            if im is None:
                preds[ip] = {"wh": None, "p": []}
                continue
            t0 = time.perf_counter()
            r = det.detect(im)
            ms = (time.perf_counter() - t0) * 1000.0
            total_ms += ms
            n_calls += 1
            wh = [im.shape[1], im.shape[0]]
            if r.detected:
                x, y, w, h = r.bbox
                preds[ip] = {"wh": wh, "p": [[float(x), float(y), float(w), float(h), float(r.confidence)]]}
            else:
                preds[ip] = {"wh": wh, "p": []}
        print(f"  classical clip {clip_id or '<unparsed>'}: {len(items)} frames done")
    mean_ms = total_ms / max(1, n_calls)
    cache_save(desc, {"preds": preds, "mean_ms": mean_ms})
    return preds, mean_ms


def recall_metrics(images: List[str], preds: Dict[str, Dict[str, Any]],
                   tol_px: float) -> Dict[str, Any]:
    """Per-GT-box recall + x-bin + height-tercile breakdown."""
    per_gt = []  # (cx, h, hit)
    n_img_labeled = 0
    for ip in images:
        gts = gt_boxes_for(ip)
        if not gts:
            continue
        n_img_labeled += 1
        ent = preds.get(ip) or {}
        im_preds = ent.get("p", [])
        wh = ent.get("wh") or [PROC_W, PROC_H]
        W, H = int(wh[0]), int(wh[1])
        for gt in gts:
            hit = any(center_hit(p[:4], gt, W, H, tol_px) for p in im_preds)
            per_gt.append((gt[0], gt[3], hit))
    n = len(per_gt)
    hits = sum(1 for g in per_gt if g[2])
    overall = hits / n if n else 0.0

    xbins = []
    for lo, hi in X_BINS:
        sel = [g for g in per_gt if lo <= g[0] < hi]
        xbins.append({"bin": f"[{lo:.2f},{hi if hi <= 1 else 1.0:.2f})", "n": len(sel),
                      "recall": (sum(1 for g in sel if g[2]) / len(sel)) if sel else None})

    heights = sorted(g[1] for g in per_gt)
    terciles = []
    if heights:
        t1 = heights[len(heights) // 3]
        t2 = heights[(2 * len(heights)) // 3]
        edges = [(0.0, t1, "short"), (t1, t2, "mid"), (t2, 10.0, "tall")]
        for lo, hi, name in edges:
            sel = [g for g in per_gt if lo <= g[1] < hi] if name != "tall" else [g for g in per_gt if g[1] >= lo]
            terciles.append({"tercile": name, "h_range": f"[{lo:.3f},{hi:.3f})" if name != "tall" else f">={lo:.3f}",
                             "n": len(sel),
                             "recall": (sum(1 for g in sel if g[2]) / len(sel)) if sel else None})
    return {"n_images_labeled": n_img_labeled, "n_gt": n, "recall": overall,
            "x_bins": xbins, "height_terciles": terciles}


def fp_metrics(neg_images: List[str], preds: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    fp = sum(1 for ip in neg_images if (preds.get(ip) or {}).get("p"))
    return {"n_negatives": len(neg_images), "fp": fp,
            "fp_rate": fp / len(neg_images) if neg_images else None}


def resolve_negatives() -> Tuple[List[str], str, bool]:
    """-> (paths, source_dir, vetted)"""
    d = os.path.join(MANUAL_DIR, "negatives")
    imgs = list_images(d)
    if imgs:
        return imgs, d, True
    imgs = list_images(FALLBACK_NEG_DIR)
    if imgs:
        print(f"WARNING: manual negatives dir missing ({d}); falling back to UNVETTED "
              f"candidates {FALLBACK_NEG_DIR} ({len(imgs)} frames). Re-run once the manual "
              f"val workflow finishes.")
        return imgs, FALLBACK_NEG_DIR, False
    print(f"WARNING: no negatives available ({d} and {FALLBACK_NEG_DIR} both missing/empty) "
          f"-- FP rate cannot be measured.")
    return [], "", False


def resolve_static_sets(limit: int) -> Dict[str, List[str]]:
    """Named image sets for the static evals. Missing sets -> clear warnings."""
    sets: Dict[str, List[str]] = {}
    man_imgs = list_images(os.path.join(MANUAL_DIR, "images"))
    if man_imgs:
        sets["manual_val"] = man_imgs[:limit] if limit else man_imgs
    else:
        print(f"WARNING: manual val set not built yet ({MANUAL_DIR}\\images missing/empty) "
              f"-- it is being assembled by a parallel workflow; skipping that set.")
    clean = read_list(CLEAN_VAL)
    if clean:
        missing = [p for p in clean[:20] if not os.path.isfile(p)]
        if missing:
            print(f"WARNING: clean_val.txt entries missing on disk (e.g. {missing[0]})")
        sets["clean_val"] = clean[:limit] if limit else clean
    else:
        print(f"WARNING: cleaned pseudo val split missing ({CLEAN_VAL}); skipping that set.")
    if not sets:
        _die("no evaluation sets available at all -- nothing to do")
    return sets


# --------------------------------------------------------------------------------------
# pretty tables
# --------------------------------------------------------------------------------------

def _pct(v: Optional[float]) -> str:
    return "   n/a" if v is None else f"{v*100:6.1f}%"


def print_static_table(title: str, results: Dict[str, Dict[str, Any]],
                       neg: Optional[Dict[str, Any]], mean_ms: Optional[float] = None) -> None:
    print(f"\n=== {title} ===")
    for set_name, m in results.items():
        print(f"\n-- set: {set_name}  (images={m['n_images_labeled']}  gt_boxes={m['n_gt']})")
        print(f"   overall recall : {_pct(m['recall'])}")
        print("   x-bin            n     recall")
        for b in m["x_bins"]:
            edge = " <-edge" if b["bin"] in (m["x_bins"][0]["bin"], m["x_bins"][-1]["bin"]) else ""
            print(f"   {b['bin']:<14} {b['n']:>5}  {_pct(b['recall'])}{edge}")
        print("   height-tercile        n     recall")
        for t in m["height_terciles"]:
            print(f"   {t['tercile']:<6}{t['h_range']:<15} {t['n']:>5}  {_pct(t['recall'])}")
    if neg is not None and neg.get("n_negatives"):
        print(f"\n-- negatives ({neg['n_negatives']} frames, src={neg.get('source','?')} "
              f"vetted={neg.get('vetted')})")
        print(f"   FP rate        : {_pct(neg['fp_rate'])}  ({neg['fp']}/{neg['n_negatives']})")
    if mean_ms is not None:
        print(f"\n   mean detect() ms: {mean_ms:.2f}")


# --------------------------------------------------------------------------------------
# clips (temporal) evaluation
# --------------------------------------------------------------------------------------

def spans_for_clip(clip_id: str) -> List[Tuple[int, int]]:
    """Shot spans in VIDEO frames from cleaned pseudo-label dataset indices."""
    idxs = []
    for lst in (CLEAN_VAL, CLEAN_TRAIN):
        for p in read_list(lst):
            cf = parse_clip_frame(p)
            if cf and cf[0] == clip_id:
                idxs.append(cf[1])
    idxs = sorted(set(idxs))
    if not idxs:
        return []
    spans, start, prev = [], idxs[0], idxs[0]
    for i in idxs[1:]:
        if i - prev <= 5:
            prev = i
            continue
        spans.append((start * FRAME_STEP, prev * FRAME_STEP))
        start = prev = i
    spans.append((start * FRAME_STEP, prev * FRAME_STEP))
    return spans


def clip_video_path(videos_dir: str, clip_id: str) -> str:
    return os.path.join(videos_dir, f"NBA 2K26_{clip_id}.mp4")


def run_clip(clip_id: str, videos_dir: str, max_frames: int, runner: Optional[YoloRunner],
             no_cache: bool) -> Dict[str, Any]:
    """Decode the clip once; per-frame classical detect (and locator when runner given).
    Returns raw per-frame records + fps. Cached."""
    vpath = clip_video_path(videos_dir, clip_id)
    if not os.path.isfile(vpath):
        _die(f"val clip video missing: {vpath}")
    desc = {"kind": "clip", "clip": clip_id, "video_sig": file_sig(vpath),
            "max_frames": max_frames, **classical_sig(),
            "locator": runner.sig() if runner else None}
    cached = cache_load(desc, no_cache)
    if cached is not None:
        return cached

    cap = cv2.VideoCapture(vpath)
    if not cap.isOpened():
        _die(f"cannot open video: {vpath}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    det = build_classical_detector()
    frames: List[Dict[str, Any]] = []
    t_start = time.perf_counter()
    n = 0
    while True:
        if max_frames and n >= max_frames:
            break
        ok, frame = cap.read()
        if not ok:
            break
        if frame.shape[1] != PROC_W or frame.shape[0] != PROC_H:
            frame = cv2.resize(frame, (PROC_W, PROC_H), interpolation=cv2.INTER_AREA)
        t0 = time.perf_counter()
        r = det.detect(frame)
        cms = (time.perf_counter() - t0) * 1000.0
        rec: Dict[str, Any] = {"f": n, "cd": int(r.detected), "fill": round(float(r.fill_pct), 2),
                               "conf": round(float(r.confidence), 3), "ms": round(cms, 2)}
        if r.detected:
            rec["bb"] = [int(v) for v in r.bbox]
        if runner is not None:
            lp = runner.predict(frame)
            rec["ld"] = int(bool(lp))
            if lp:
                best = max(lp, key=lambda p: p[4])
                rec["lb"] = [round(v, 1) for v in best[:4]]
        frames.append(rec)
        n += 1
        if n % 500 == 0:
            print(f"  clip {clip_id}: {n} frames ({time.perf_counter()-t_start:.0f}s)")
    cap.release()
    data = {"fps": fps, "n_frames": n, "frames": frames}
    cache_save(desc, data)
    return data


def _detection_episodes(det_frames: List[int], merge_gap: int = 5) -> int:
    if not det_frames:
        return 0
    eps, prev = 1, det_frames[0]
    for f in det_frames[1:]:
        if f - prev > merge_gap:
            eps += 1
        prev = f
    return eps


def clip_metrics(clip_id: str, data: Dict[str, Any], method: str) -> Dict[str, Any]:
    """method: 'cd' (classical) or 'ld' (locator)."""
    fps = data["fps"]
    n = data["n_frames"]
    frames = data["frames"]
    spans_all = spans_for_clip(clip_id)
    if not spans_all:
        print(f"WARNING: no pseudo-label spans found for clip {clip_id} "
              f"(clean_val/clean_train lists) -- lock metrics unavailable.")
    spans = [(s, e) for (s, e) in spans_all if s < n]      # span must start in processed range
    det_frames = [fr["f"] for fr in frames if fr.get(method)]
    det_set = set(det_frames)

    # per-shot lock + latency
    locks, latencies = [], []
    for (s, e) in spans:
        e_c = min(e, n - 1)
        inside = [f for f in range(s, e_c + 1) if f in det_set]
        locks.append(bool(inside))
        if inside:
            latencies.append(inside[0] - s)
    lock_rate = (sum(locks) / len(locks)) if locks else None

    # phantom: detections >2s away from ANY span (full-span list, incl. beyond range)
    guard = int(round(2.0 * fps))
    far_det, far_frames = [], 0
    for f in range(n):
        near = any((s - guard) <= f <= (e + guard) for (s, e) in spans_all)
        if near:
            continue
        far_frames += 1
        if f in det_set:
            far_det.append(f)
    far_minutes = far_frames / fps / 60.0 if fps else 0.0
    episodes = _detection_episodes(far_det)
    phantom_per_min = (episodes / far_minutes) if far_minutes > 0 else None
    phantom_frames_per_min = (len(far_det) / far_minutes) if far_minutes > 0 else None

    out: Dict[str, Any] = {
        "clip": clip_id, "method": "classical" if method == "cd" else "locator",
        "fps": fps, "frames_processed": n,
        "n_spans_total": len(spans_all), "n_spans_evaluated": len(spans),
        "lock_rate": lock_rate,
        "locked": int(sum(locks)) if locks else 0,
        "lock_latency_frames": {
            "median": float(np.median(latencies)) if latencies else None,
            "p90": float(np.percentile(latencies, 90)) if latencies else None,
            "max": int(max(latencies)) if latencies else None,
        },
        "phantom_episodes": episodes, "phantom_det_frames": len(far_det),
        "far_region_minutes": round(far_minutes, 2),
        "phantom_per_min": phantom_per_min,
        "phantom_frames_per_min": phantom_frames_per_min,
    }

    if method == "cd":
        # fill-read sanity: within each span, fraction of non-decreasing consecutive
        # detected-frame fill steps from span start up to the peak-fill frame (tol 3%)
        monos, peaks = [], []
        for (s, e) in spans:
            e_c = min(e, n - 1)
            seq = [(fr["f"], fr["fill"]) for fr in frames
                   if s <= fr["f"] <= e_c and fr.get("cd")]
            if len(seq) < 3:
                continue
            fills = [f for _, f in seq]
            peak_i = int(np.argmax(fills))
            peaks.append(fills[peak_i])
            rise = fills[:peak_i + 1]
            if len(rise) >= 2:
                good = sum(1 for a, b in zip(rise, rise[1:]) if b >= a - 3.0)
                monos.append(good / (len(rise) - 1))
        out["fill_monotonicity_mean"] = float(np.mean(monos)) if monos else None
        out["fill_monotonicity_n_spans"] = len(monos)
        out["fill_peak_mean"] = float(np.mean(peaks)) if peaks else None
        out["mean_detect_ms"] = float(np.mean([fr["ms"] for fr in frames])) if frames else None
    return out


def print_clip_table(rows: List[Dict[str, Any]]) -> None:
    print("\n=== CLIPS (temporal) ===")
    hdr = (f"{'clip':<16}{'method':<11}{'frames':>7}{'spans':>6}{'lock':>8}{'lat_med':>8}"
           f"{'lat_p90':>8}{'phantom/min':>12}{'fill_mono':>10}{'det_ms':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        lat = r["lock_latency_frames"]
        lat_med = f"{lat['median']:.0f}" if lat["median"] is not None else "n/a"
        lat_p90 = f"{lat['p90']:.0f}" if lat["p90"] is not None else "n/a"
        phant = f"{r['phantom_per_min']:.2f}" if r["phantom_per_min"] is not None else "n/a"
        mono = _pct(r["fill_monotonicity_mean"]).strip() if r.get("fill_monotonicity_mean") is not None else "n/a"
        dms = f"{r['mean_detect_ms']:.1f}" if r.get("mean_detect_ms") is not None else "n/a"
        print(f"{r['clip']:<16}{r['method']:<11}{r['frames_processed']:>7}"
              f"{r['n_spans_evaluated']:>6}{_pct(r['lock_rate']):>8}"
              f"{lat_med:>8}{lat_p90:>8}{phant:>12}{mono:>10}{dms:>8}")


# --------------------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------------------

def save_report(name: str, payload: Dict[str, Any]) -> str:
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1)
    print(f"\nJSON -> {p}")
    return p


def cmd_boxes(args) -> Dict[str, Any]:
    model_path = args.model if os.path.isabs(args.model) else os.path.join(ROOT, args.model)
    runner = YoloRunner(model_path, args.imgsz, args.conf)
    sets = resolve_static_sets(args.limit)
    results = {}
    for name, imgs in sets.items():
        print(f"\nrunning locator on set '{name}' ({len(imgs)} images)...")
        preds = run_yolo_on_images(runner, imgs, name, args.no_cache)
        results[name] = recall_metrics(imgs, preds, args.tol_px)
    negs, neg_src, vetted = resolve_negatives()
    neg_metrics = None
    if negs:
        print(f"\nrunning locator on negatives ({len(negs)} images)...")
        npreds = run_yolo_on_images(runner, negs, "negatives", args.no_cache)
        neg_metrics = fp_metrics(negs, npreds)
        neg_metrics.update({"source": neg_src, "vetted": vetted})
    title = f"BOXES locator={os.path.basename(model_path)} imgsz={args.imgsz} conf={args.conf} tol={args.tol_px}px"
    print_static_table(title, results, neg_metrics)
    payload = {"meta": {"cmd": "boxes", **runner.sig(), "tol_px": args.tol_px, "limit": args.limit},
               "sets": results, "negatives": neg_metrics}
    save_report(f"boxes_{os.path.splitext(os.path.basename(model_path))[0]}.json", payload)
    return payload


def cmd_classical(args) -> Dict[str, Any]:
    sets = resolve_static_sets(args.limit)
    results = {}
    mean_mss = []
    for name, imgs in sets.items():
        print(f"\nrunning classical detector on set '{name}' ({len(imgs)} images, grouped by clip)...")
        preds, mean_ms = run_classical_on_images(imgs, name, args.no_cache)
        results[name] = recall_metrics(imgs, preds, args.tol_px)
        results[name]["mean_detect_ms"] = mean_ms
        mean_mss.append(mean_ms)
    negs, neg_src, vetted = resolve_negatives()
    neg_metrics = None
    if negs:
        print(f"\nrunning classical detector on negatives ({len(negs)} images)...")
        npreds, _ = run_classical_on_images(negs, "negatives", args.no_cache)
        neg_metrics = fp_metrics(negs, npreds)
        neg_metrics.update({"source": neg_src, "vetted": vetted})
    title = f"CLASSICAL park detector (Red, park_temporal) tol={args.tol_px}px"
    print_static_table(title, results, neg_metrics,
                       mean_ms=float(np.mean(mean_mss)) if mean_mss else None)
    payload = {"meta": {"cmd": "classical", **classical_sig(), "tol_px": args.tol_px,
                        "limit": args.limit},
               "sets": results, "negatives": neg_metrics}
    save_report("classical_static.json", payload)
    return payload


def cmd_clips(args) -> Dict[str, Any]:
    clip_ids = [args.clip] if args.clip else list(VAL_CLIPS)
    runner = None
    if args.model:
        model_path = args.model if os.path.isabs(args.model) else os.path.join(ROOT, args.model)
        runner = YoloRunner(model_path, args.imgsz, args.conf)
    rows = []
    for cid in clip_ids:
        print(f"\nprocessing clip {cid} (max_frames={args.max_frames or 'all'})...")
        data = run_clip(cid, args.videos_dir, args.max_frames, runner, args.no_cache)
        rows.append(clip_metrics(cid, data, "cd"))
        if runner is not None:
            rows.append(clip_metrics(cid, data, "ld"))
    print_clip_table(rows)
    payload = {"meta": {"cmd": "clips", "clips": clip_ids, "max_frames": args.max_frames,
                        "locator": runner.sig() if runner else None, **classical_sig()},
               "results": rows}
    save_report("clips_" + ("_".join(clip_ids)) + ".json", payload)
    return payload


def _gate(name: str, ok: Optional[bool], detail: str) -> Dict[str, Any]:
    status = "PASS" if ok else ("UNPROVABLE" if ok is None else "FAIL")
    print(f"  [{status:<10}] {name:<28} {detail}")
    return {"gate": name, "status": status, "detail": detail}


def cmd_gates(args) -> Dict[str, Any]:
    target = args.target
    if target == "locator" and not args.model:
        _die("gates --target locator requires --model")
    print(f"\n########## SHIP GATES -- target: {target} ##########")

    # -- static (boxes/classical) --
    if target == "locator":
        static = cmd_boxes(args)
    else:
        static = cmd_classical(args)

    # -- temporal (clips) --
    clips_args = argparse.Namespace(clip=None, videos_dir=args.videos_dir,
                                    max_frames=args.max_frames, model=args.model if target == "locator" else None,
                                    imgsz=args.imgsz, conf=args.conf, no_cache=args.no_cache)
    clips = cmd_clips(clips_args)
    method = "locator" if target == "locator" else "classical"
    trows = [r for r in clips["results"] if r["method"] == method]

    # gate basis set: manual val when available, else clean_val (stated explicitly)
    sets = static["sets"]
    basis = "manual_val" if "manual_val" in sets else "clean_val"
    m = sets[basis]

    print(f"\n=== GATE VERDICTS (recall basis set: {basis}) ===")
    verdicts = []
    verdicts.append(_gate("overall recall >= 0.97",
                          m["recall"] >= GATE_RECALL if m["n_gt"] else None,
                          f"{m['recall']*100:.1f}% on {basis} (n={m['n_gt']})"))
    for i in EDGE_BIN_IDX:
        b = m["x_bins"][i]
        ok = None if b["recall"] is None else (b["recall"] >= GATE_EDGE_RECALL)
        verdicts.append(_gate(f"edge-bin {b['bin']} recall >= 0.90", ok,
                              f"{_pct(b['recall']).strip()} (n={b['n']})"))
    neg = static.get("negatives")
    if neg and neg.get("fp_rate") is not None:
        verdicts.append(_gate("FP rate <= 0.5%", neg["fp_rate"] <= GATE_FP,
                              f"{neg['fp_rate']*100:.2f}% ({neg['fp']}/{neg['n_negatives']}, "
                              f"vetted={neg.get('vetted')})"))
    else:
        verdicts.append(_gate("FP rate <= 0.5%", None, "no negatives available"))

    n_spans = sum(r["n_spans_evaluated"] for r in trows)
    n_locked = sum(r["locked"] for r in trows)
    lock = n_locked / n_spans if n_spans else None
    verdicts.append(_gate("per-shot lock >= 99%", None if lock is None else lock >= GATE_LOCK,
                          f"{_pct(lock).strip()} ({n_locked}/{n_spans} spans over {len(trows)} clips)"))
    phs = [r["phantom_per_min"] for r in trows if r["phantom_per_min"] is not None]
    ph = max(phs) if phs else None
    verdicts.append(_gate("phantom <= 2/min (worst clip)", None if ph is None else ph <= GATE_PHANTOM_PER_MIN,
                          f"{ph:.2f}/min" if ph is not None else "no far-region time processed"))

    all_pass = all(v["status"] == "PASS" for v in verdicts)
    print(f"\n>>> SHIP GATE: {'PASS -- may drive live timing' if all_pass else 'FAIL -- must NOT drive live timing'}")
    payload = {"meta": {"cmd": "gates", "target": target}, "static": static, "clips": clips,
               "verdicts": verdicts, "ship": all_pass}
    save_report(f"gates_{target}.json", payload)
    if not all_pass:
        sys.exit(1)
    return payload


# --------------------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p, model_required=False):
        p.add_argument("--tol-px", type=float, default=20.0, help="center-hit px tolerance")
        p.add_argument("--limit", type=int, default=0, help="cap images per set (0=all)")
        p.add_argument("--no-cache", action="store_true")

    def add_model(p, required):
        p.add_argument("--model", default=(None if not required else os.path.join("models", "orion_meter_n.pt")),
                       required=False, help="YOLO locator .pt")
        p.add_argument("--imgsz", type=int, default=768, help="must match training imgsz")
        p.add_argument("--conf", type=float, default=0.35)

    p = sub.add_parser("boxes", help="static YOLO locator eval")
    add_model(p, required=True)
    p.set_defaults(model=os.path.join("models", "orion_meter_n.pt"))
    add_common(p)
    p.set_defaults(fn=cmd_boxes)

    p = sub.add_parser("classical", help="static classical-detector eval")
    add_common(p)
    p.set_defaults(fn=cmd_classical)

    p = sub.add_parser("clips", help="temporal eval on held-out val clips")
    add_model(p, required=False)
    p.add_argument("--clip", default=None, help=f"single clip id (default: both of {VAL_CLIPS})")
    p.add_argument("--videos-dir", default=DEFAULT_VIDEOS_DIR)
    p.add_argument("--max-frames", type=int, default=0, help="cap frames per clip (0=all)")
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(fn=cmd_clips)

    p = sub.add_parser("gates", help="boxes+clips -> PASS/FAIL ship verdict (exit 0 iff all pass)")
    add_model(p, required=True)
    p.set_defaults(model=os.path.join("models", "orion_meter_n.pt"))
    p.add_argument("--target", choices=["locator", "classical"], default="locator")
    p.add_argument("--videos-dir", default=DEFAULT_VIDEOS_DIR)
    p.add_argument("--max-frames", type=int, default=0)
    add_common(p)
    p.set_defaults(fn=cmd_gates)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
