#!/usr/bin/env python3
"""Train the No-Meter Mode cue temporal model (1D TCN, two heads) from release-cue labels.

INSURANCE STAGE: assume the heuristic cue IQRs from prepare_release_cue_labels.py miss the
green window -- this learned model predicts release timing straight from pose sequences.
See docs/NO_METER_MODE_TRAINING.md Stage 5.

Input: release_cue_labels.json (prepare_release_cue_labels.py, 2026-08-08 schema or newer:
per-cue `video_frame`, per-shot `handedness` + `player_bbox_at_release` -- older JSONs fail
loud with a regenerate message) plus the SOURCE VIDEOS it references (the "path" field per
shot). The pose dataset's YOLO frames are ~12fps decorrelated samples -- useless for 60fps
windows -- so sequences are re-extracted from the videos (YOLO pose, release-bbox-anchored
player lock; temporal-nearest only as the DEBUG-logged compat path for labels without the
bbox) and cached to a .npz NEXT TO the labels JSON; re-runs skip the OpenCV re-read entirely.

Per accepted shot, N windows are cut ending at release-minus-jitter (jitter in
[0, window//2] frames) so training sees varied "how far from release" contexts, matching
how the model is queried at runtime. Targets per window:
  timing head: frames-until-release from the window's last frame (MSE, what the bot uses)
  cue head:    cue class at the last frame within +-cue-tol frames, else "none" (CE)
Windows where > --max-arm-missing of frames have a zero-visibility shooting-arm joint
(wrist/elbow/shoulder, handedness side) are rejected.

Split: owner-only validation at VIDEO granularity (the split_train_val.py invariant, fail
closed: youtube/unknown-source shots can NEVER enter val; empty val aborts, do not relax).

Loss = 0.6*timing_mse + 0.4*cue_ce. AdamW lr 3e-4, cosine schedule. Early stop + model
selection on VAL TIMING MAE (2026-08-09 fix: the previous criterion -- val green-window hit
rate -- selected a DEGENERATE CONSTANT predictor: ~1/3 of windows have offset 0, so constant
"0 frames" scores ~25-30% hit rate by construction). A near-constant predictor (val pred std
under max(5% of target std, 0.25 frames)) is REFUSED as best regardless of its metrics, and
`green_hit_rate_offset2plus` (hit rate on windows with true offset >= 2 frames) is reported
as the constant-predictor-immune view of the hit rate. Cue CE is class-weighted by inverse
train frequency (--no-class-weights restores unweighted).

Outputs in --out: cue_temporal_model.pt + cue_temporal_model_meta.json (the runtime feature
contract). ONNX export is separate: tools/training/export_cue_temporal_onnx.py.

Usage:
  python tools/training/train_cue_temporal_model.py \
      --labels logs/diagnostics/no_meter_prep/release_cue_labels.json \
      [--out logs/diagnostics/cue_temporal] [--window 60] [--per-shot 4] [--epochs 50] \
      [--batch 128] [--lr 3e-4] [--val-frac 0.2] [--handedness Right] [--cue-tol 3] \
      [--pose-model models/orion_pose2k_n_v2.pt] [--seed 1234] [--patience 8]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from cue_temporal_model import (  # noqa: E402  (numpy-only imports; torch stays optional)
    ARM_ELBOW, ARM_SHOULDER, ARM_WRIST, CUE_CLASSES, DEFAULT_CHANNELS, DEFAULT_DILATIONS,
    DEFAULT_DROPOUT, DEFAULT_KERNEL, INPUT_DIM, arch_dict, build_model, count_params,
    normalization_meta, normalize_frames,
)

# nearest cue wins; ties broken toward the LATER (more actionable) cue in the shot motion
CUE_PRIORITY = {"release": 0, "flick": 1, "push": 2, "jump": 3, "set_point": 4}
GREEN_WINDOW_MS = 30.0  # "hit" = predicted release within +-30ms of ground truth

REGEN_HINT = "regenerate labels with the updated prepare_release_cue_labels.py"
log = logging.getLogger(__name__)


# ==============================================================================================
# CUDA preflight (task #69: fail EARLY and LOUD, never silently 8fps / CPU-crawl)
# ==============================================================================================

def preflight_cuda_torch():
    """Abort with the exact fix commands unless CUDA torch is importable and live."""
    py = sys.executable
    fix = (
        "\n"
        "FIX (run in the venv/python you will train with):\n"
        f'  "{py}" -m pip uninstall -y torch torchvision\n'
        f'  "{py}" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu126\n'
        f'  "{py}" -m pip install ultralytics\n'
        "\n"
        "Never train on CPU torch: CPU inference was the 2026-07-06 0/28 batch root cause\n"
        "(8fps detector). This rig's system torch may be the +cpu build -- 'import torch\n"
        "works' is NOT the test; torch.cuda.is_available() is.\n"
    )
    try:
        import torch  # noqa: F401
    except Exception as exc:
        print(f"PREFLIGHT FAILED: torch is not importable ({exc}){fix}")
        return False
    import torch
    if not torch.cuda.is_available():
        built = getattr(torch.version, "cuda", None)
        print(f"PREFLIGHT FAILED: torch {torch.__version__} has no CUDA "
              f"(torch.cuda.is_available()=False, built-against-cuda={built}).{fix}")
        return False
    print(f"preflight OK: torch {torch.__version__} CUDA={torch.version.cuda} "
          f"device={torch.cuda.get_device_name(0)}")
    return True


# ==============================================================================================
# Labels + owner-only split
# ==============================================================================================

def load_shots(labels_path):
    with open(labels_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    shots = [s for s in data.get("shots", []) if "release" in s.get("cues", {})]
    dropped = len(data.get("shots", [])) - len(shots)
    if dropped:
        print(f"labels: dropped {dropped} shots without a release cue")
    # Fail LOUD on the pre-2026-08-08 schema: video_frame is the raw-video anchor; quietly
    # reconstructing it as frame*step would hide schema drift (labels bug #1).
    stale = [shot_key(s) for s in shots
             if "release_video_frame" not in s
             or any("video_frame" not in c for c in s["cues"].values())]
    if stale:
        print(f"ERROR: {len(stale)}/{len(shots)} shots lack video_frame/release_video_frame "
              f"(first: {stale[0]}) -- old labels schema; {REGEN_HINT}")
        raise SystemExit(2)
    return shots, data


def shot_key(s):
    return f'{s["video"]}#{s["shot_idx"]}'


def shot_handedness(s, flag_handedness):
    """Labels bug #2 chain: per-shot labels field -> --handedness flag -> 'right'.

    Which source fired is DEBUG-logged per shot (run with --debug to see it)."""
    h = s.get("handedness")
    if h:
        log.debug("handedness %s: '%s' (labels field)", shot_key(s), h)
        return str(h)
    if str(flag_handedness or "").strip():
        log.debug("handedness %s: '%s' (--handedness flag fallback -- labels lack the field)",
                  shot_key(s), flag_handedness)
        return str(flag_handedness)
    log.debug("handedness %s: 'right' (hard default -- no labels field, no flag)", shot_key(s))
    return "right"


def build_split(shots, val_frac, seed, allow=("owner",)):
    """Video-granularity owner-only split. Returns (train, val, contaminated)."""
    allow = set(allow)
    groups = {}
    for s in shots:
        groups.setdefault(s["video"], []).append(s)
    eligible = sorted(v for v in groups
                      if all(s.get("source", "unknown") in allow for s in groups[v]))
    rng = random.Random(seed)
    rng.shuffle(eligible)
    target = int(round(len(shots) * val_frac))
    val_vids, acc = set(), 0
    for v in eligible:
        if acc >= target:
            break
        val_vids.add(v)
        acc += len(groups[v])
    train, val = [], []
    for s in shots:
        # belt & braces: a non-allowlisted source can never land in val
        if s["video"] in val_vids and s.get("source", "unknown") in allow:
            val.append(s)
        else:
            train.append(s)
    contaminated = [s for s in val if s.get("source", "unknown") not in allow]
    return train, val, contaminated


# ==============================================================================================
# Sequence cache (.npz next to the labels JSON -- re-runs skip the OpenCV re-read)
# ==============================================================================================

def cache_path_for(labels_path):
    d, b = os.path.split(os.path.abspath(labels_path))
    return os.path.join(d, os.path.splitext(b)[0].replace(" ", "_") + "_seq_cache.npz")


def load_cache(path):
    if not os.path.isfile(path):
        return {}
    try:
        z = np.load(path, allow_pickle=False)
        meta = json.loads(str(z["meta"][()]))
        return {m["key"]: {"kpts": z[m["arr"]], "start": int(m["start"]), "rel": int(m["rel"]),
                           "step": int(m["step"]), "fps": float(m["fps"]), "path": m["path"]}
                for m in meta}
    except Exception as exc:
        print(f"WARNING: unreadable sequence cache {path} ({exc}) -- re-extracting")
        return {}


def save_cache(path, cache):
    arrays, meta = {}, []
    for i, key in enumerate(sorted(cache)):
        ent = cache[key]
        name = f"k{i}"
        arrays[name] = np.asarray(ent["kpts"], dtype=np.float32)
        meta.append({"key": key, "arr": name, "start": int(ent["start"]), "rel": int(ent["rel"]),
                     "step": int(ent["step"]), "fps": float(ent["fps"]), "path": ent["path"]})
    np.savez_compressed(path, meta=np.array(json.dumps(meta)), **arrays)
    print(f"sequence cache: {len(meta)} shots -> {path}")


def _extract_video(path, tasks, pose, step):
    """Extract (span,17,3) pixel keypoints per task from one video, single sequential pass.

    tasks: [(key, start_idx, rel_idx, rel_video_frame, player_bbox_at_release)] with
    start/rel in SAMPLED-index space. The raw video frame for sampled index j is ANCHORED
    at the label's rel_video_frame and spaced by the per-shot stride:
    raw = rel_video_frame - (rel_idx - j) * step (labels bug #1 fix: the seek no longer
    guesses raw = index * step).

    Player lock: when the labels carry player_bbox_at_release ([x, y, w, h] raw pixels),
    the lock walks BACKWARD from the release frame seeded on that bbox center --
    deterministic, and it reproduces the label-time meter-nearest selection (labels
    bug #3 fix). Labels without the field fall back to the old temporal-nearest forward
    lock (highest-confidence seed), DEBUG-logged compat path.
    """
    import cv2

    raw_needed = set()
    for _key, s0, s1, rel_video, _pbb in tasks:
        for j in range(s0, s1 + 1):
            raw = rel_video - (s1 - j) * step
            if raw >= 0:
                raw_needed.add(raw)
    if not raw_needed:
        return {}
    last_raw = max(raw_needed)
    cap = cv2.VideoCapture(path)
    dets = {}

    def _flush(buf):
        if not buf:
            return
        for (fi, _), r in zip(buf, pose.predict([f for _, f in buf], verbose=False, conf=0.10)):
            if r.boxes is not None and len(r.boxes) and r.keypoints is not None:
                dets[fi] = (r.boxes.xywh.cpu().numpy(),
                            r.boxes.conf.cpu().numpy(),
                            r.keypoints.data.cpu().numpy())

    buf = []
    i = 0
    while i <= last_raw:
        if not cap.grab():
            break
        if i in raw_needed:
            ok, fr = cap.retrieve()
            if not ok:
                break
            buf.append((i, fr))
            if len(buf) >= 16:
                _flush(buf)
                buf = []
        i += 1
    _flush(buf)
    cap.release()

    out = {}
    for key, s0, s1, rel_video, pbb in tasks:
        span = s1 - s0 + 1
        kp = np.zeros((span, 17, 3), dtype=np.float32)  # missing detection = conf-0 row
        raws = [rel_video - (span - 1 - li) * step for li in range(span)]
        if pbb is not None and len(pbb) >= 4:
            # deterministic lock: seed on the labeled release bbox center, walk backward
            prev = (float(pbb[0]) + float(pbb[2]) / 2.0, float(pbb[1]) + float(pbb[3]) / 2.0)
            order = range(span - 1, -1, -1)
        else:
            log.debug("shot %s: labels lack player_bbox_at_release -- temporal-nearest "
                      "forward lock (old-schema compat path)", key)
            prev = None
            order = range(span)
        for li in order:
            d = dets.get(raws[li])
            if d is None:
                continue
            xywh, conf, kps = d
            if prev is None:
                b = int(np.argmax(conf))
            else:
                b = int(np.argmin((xywh[:, 0] - prev[0]) ** 2 + (xywh[:, 1] - prev[1]) ** 2))
            prev = (float(xywh[b, 0]), float(xywh[b, 1]))
            kp[li] = kps[b][:17]
        out[key] = kp
    return out


def ensure_cache(shots, cache, span, pose_model_path, cache_file):
    """Extract any shot spans missing from the cache; persist the cache when it changed."""
    missing = []
    for s in shots:
        key = shot_key(s)
        rel = int(s["cues"]["release"]["frame"])              # sampled-trajectory index
        rel_video = int(s["cues"]["release"]["video_frame"])  # raw video frame (validated)
        start = max(0, rel - span + 1)
        ent = cache.get(key)
        if (ent and ent.get("path") == s["path"] and ent["rel"] == rel
                and ent["start"] <= start):
            continue
        missing.append((s, key, start, rel, rel_video, s.get("player_bbox_at_release")))
    if not missing:
        print(f"sequence cache: all {len(shots)} shots present -> no video re-read")
        return cache

    try:
        import cv2  # noqa: F401
        from ultralytics import YOLO
    except Exception as exc:
        print(f"sequence extraction needs opencv + ultralytics: {exc}")
        print("install per the preflight fix above (same python), or supply a complete cache")
        raise SystemExit(2)

    mp = pose_model_path if os.path.isabs(pose_model_path) else os.path.join(ROOT, pose_model_path)
    if not os.path.isfile(mp):
        print(f"pose model not found at {mp}; falling back to yolov8x-pose.pt")
        mp = "yolov8x-pose.pt"
    print(f"extracting {len(missing)} shot sequences (pose model: {mp}) ...")
    pose = YOLO(mp)

    by_path = {}
    for item in missing:
        by_path.setdefault(item[0]["path"], []).append(item)
    t0 = time.time()
    n_done = 0
    for path, items in by_path.items():
        if not os.path.isfile(path):
            print(f"  skip (video not found): {path}")
            continue
        step = int(items[0][0].get("step", 1)) or 1
        got = _extract_video(path, [(k, s0, s1, rv, pbb) for _s, k, s0, s1, rv, pbb in items],
                             pose, step)
        for s, key, start, rel, _rv, _pbb in items:
            if key in got:
                cache[key] = {"kpts": got[key], "start": start, "rel": rel, "step": step,
                              "fps": float(s.get("fps", 60.0)), "path": s["path"]}
                n_done += 1
        print(f"  {os.path.basename(path):40.40} {len(got):>4} shots  ({time.time() - t0:.0f}s)")
    if n_done:
        save_cache(cache_file, cache)
    return cache


# ==============================================================================================
# Window dataset
# ==============================================================================================

def build_dataset(shots, cache, window, per_shot, seed, handedness, cue_tol, max_arm_missing):
    """Cut jittered windows from cached shot spans ->
    (X, y_frames, y_cls, cue_mask, mspf, vids, stats).

    X (M, 51, window) float32 channels-first; y_frames = frames-until-release from the
    window's last frame; y_cls = CUE_CLASSES index at the last frame; mspf = ms-per-frame
    of the source clip (frames -> ms conversion is per-clip: 1000 * step / fps).

    cue_mask (M,) bool: True where y_cls is real supervision. Two-tier labels
    (2026-08-09): tier "A" shots carry ONLY the meter-anchored release -- their windows
    train the timing head always, but contribute to the cue CE only when the window end
    sits within cue_tol of the release (the one cue tier A actually knows). Everything
    else would fabricate "none" negatives, so it is masked out of the CE. Tier "AB"
    (and legacy label files without the field) supervise the cue head on every window.
    """
    rng = random.Random(seed)
    arm_v = [ARM_SHOULDER * 3 + 2, ARM_ELBOW * 3 + 2, ARM_WRIST * 3 + 2]
    X, y_t, y_c, y_m, mspf, vids = [], [], [], [], [], []
    stats = {"rejected_arm": 0, "rejected_short": 0, "shots_missing_cache": 0}
    max_j = max(window // 2, 1)
    for s in shots:
        ent = cache.get(shot_key(s))
        if ent is None:
            stats["shots_missing_cache"] += 1
            continue
        tier = str(s.get("tier", "AB"))
        mirror = shot_handedness(s, handedness).strip().lower().startswith("l")
        feats = normalize_frames(ent["kpts"], mirror=mirror)
        rel_local = ent["rel"] - ent["start"]
        step_s = int(s.get("step", ent["step"])) or 1
        ms_per_frame = 1000.0 * step_s / max(float(s.get("fps", ent["fps"])), 1.0)
        rel_video = int(s["cues"]["release"]["video_frame"])
        cue_local = {}
        for name, cue in s["cues"].items():
            # video_frame is the authoritative anchor (sampled `frame` stays in the JSON
            # for step-by-step trajectory tracking); local index from the release delta
            li = rel_local - int(round((rel_video - int(cue["video_frame"])) / step_s))
            if 0 <= li <= rel_local:
                cue_local[name] = li
        offs = {0}
        while len(offs) < per_shot and len(offs) < max_j + 1:
            offs.add(rng.randint(0, max_j))
        for off in sorted(offs):
            end = rel_local - off
            start = end - window + 1
            if start < 0:
                stats["rejected_short"] += 1
                continue
            w = feats[start:end + 1]
            bad_frac = float(np.mean(np.any(w[:, arm_v] <= 0.0, axis=1)))
            if bad_frac > max_arm_missing:
                stats["rejected_arm"] += 1
                continue
            best = None
            for name, li in cue_local.items():
                d = abs(li - end)
                if d <= cue_tol and (best is None or (d, CUE_PRIORITY[name]) < best[:2]):
                    best = (d, CUE_PRIORITY[name], name)
            y_c.append(CUE_CLASSES.index(best[2]) if best else CUE_CLASSES.index("none"))
            y_m.append(tier == "AB" or best is not None)
            X.append(w.T)
            y_t.append(float(off))
            mspf.append(ms_per_frame)
            vids.append(s["video"])
    if not X:
        return (np.zeros((0, INPUT_DIM, window), np.float32), np.zeros(0, np.float32),
                np.zeros(0, np.int64), np.zeros(0, bool), np.zeros(0, np.float32), [], stats)
    return (np.stack(X).astype(np.float32), np.asarray(y_t, np.float32),
            np.asarray(y_c, np.int64), np.asarray(y_m, bool),
            np.asarray(mspf, np.float32), vids, stats)


def compute_metrics(pred_frames, true_frames, mspf, pred_cls, true_cls, cue_mask=None):
    pred_frames = np.asarray(pred_frames)
    true_frames = np.asarray(true_frames)
    err_ms = (pred_frames - true_frames) * np.asarray(mspf)
    # cue metrics only where the class target is real supervision (two-tier masking)
    if cue_mask is None:
        cue_mask = np.ones(len(pred_cls), dtype=bool)
    pc, tc = np.asarray(pred_cls)[cue_mask], np.asarray(true_cls)[cue_mask]
    f1 = {}
    for i, name in enumerate(CUE_CLASSES):
        tp = int(np.sum((pc == i) & (tc == i)))
        fp = int(np.sum((pc == i) & (tc != i)))
        fn = int(np.sum((pc != i) & (tc == i)))
        f1[name] = round(2 * tp / max(2 * tp + fp + fn, 1), 3)
    # Constant-predictor-immune view: the offset-0 spike (~1/3 of windows) lets a constant
    # "0 frames" output score ~30% raw hit rate; windows with true offset >= 2 frames cannot
    # be hit that way, so this rate collapses to ~0 for any degenerate predictor.
    nz = true_frames >= 2.0
    hit_nz = float(np.mean(np.abs(err_ms[nz]) <= GREEN_WINDOW_MS)) if nz.any() else float("nan")
    return {"timing_mae_ms": round(float(np.mean(np.abs(err_ms))), 1),
            "green_hit_rate": round(float(np.mean(np.abs(err_ms) <= GREEN_WINDOW_MS)), 4),
            "green_hit_rate_offset2plus": round(hit_nz, 4),
            "pred_std_frames": round(float(np.std(pred_frames)), 3),
            "target_std_frames": round(float(np.std(true_frames)), 3),
            "cue_f1": f1,
            "cue_acc": round(float(np.mean(pc == tc)), 4) if len(pc) else float("nan"),
            "cue_windows": int(cue_mask.sum())}


def is_degenerate(metrics):
    """True when the timing head is a near-constant output (the 2026-08-09 artifact).

    Guard: val prediction std under max(5% of target std, 0.25 frames). Such an epoch is
    NEVER selectable as best no matter what its hit rate says.
    """
    return metrics["pred_std_frames"] < max(0.05 * metrics["target_std_frames"], 0.25)


def predict_in_batches(model, torch, X, device, batch=256):
    preds_t, preds_c = [], []
    with torch.no_grad():
        for i in range(0, len(X), batch):
            xb = torch.from_numpy(X[i:i + batch]).to(device)
            t, c = model(xb)
            preds_t.append(t.cpu().numpy())
            preds_c.append(c.argmax(dim=1).cpu().numpy())
    return np.concatenate(preds_t), np.concatenate(preds_c)


# ==============================================================================================
# Main
# ==============================================================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--labels", default=os.path.join("logs", "diagnostics", "no_meter_prep",
                                                     "release_cue_labels.json"))
    ap.add_argument("--out", default=os.path.join("logs", "diagnostics", "cue_temporal"))
    ap.add_argument("--window", type=int, default=60, help="sequence length in frames (~1s @ 60fps)")
    ap.add_argument("--per-shot", type=int, default=4, help="jittered windows per shot")
    ap.add_argument("--cue-tol", type=int, default=3, help="cue-class match tolerance, frames")
    ap.add_argument("--max-arm-missing", type=float, default=0.30,
                    help="reject a window if > this fraction of frames miss a shooting-arm joint")
    ap.add_argument("--handedness", default="Right",
                    help="fallback when a shot record has no handedness field "
                         "(pre-2026-08-08 labels; the per-shot field wins when present)")
    ap.add_argument("--pose-model", default=os.path.join("models", "orion_pose2k_n_v3.pt"))
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--w-timing", type=float, default=0.6)
    ap.add_argument("--w-cue", type=float, default=0.4)
    ap.add_argument("--val-frac", type=float, default=0.2)
    ap.add_argument("--patience", type=int, default=8, help="early-stop patience on val MAE")
    ap.add_argument("--no-class-weights", action="store_true",
                    help="disable inverse-frequency class weighting of the cue CE loss")
    ap.add_argument("--seed", type=int, default=1234)
    ap.add_argument("--debug", action="store_true",
                    help="DEBUG logging (per-shot handedness/player-lock fallback provenance)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(levelname)s %(message)s")
    os.environ.setdefault("YOLO_VERBOSE", "False")

    # ---- Stage 0: CUDA preflight (ALWAYS first; task #69) ------------------------------------
    if not preflight_cuda_torch():
        return 2
    import torch
    from torch import nn as tnn

    labels = args.labels if os.path.isabs(args.labels) else os.path.join(ROOT, args.labels)
    if not os.path.isfile(labels):
        print(f"labels JSON not found: {labels} (run tools/training/no_meter_mode_prep.py "
              "or prepare_release_cue_labels.py first)")
        return 2
    out_dir = args.out if os.path.isabs(args.out) else os.path.join(ROOT, args.out)
    os.makedirs(out_dir, exist_ok=True)

    shots, _data = load_shots(labels)
    if not shots:
        print("no usable shots in the labels JSON")
        return 1

    # ---- owner-only split (fail closed, split_train_val.py invariant) -------------------------
    train_shots, val_shots, contaminated = build_split(shots, args.val_frac, args.seed)
    print(f"split: {len(train_shots)} train / {len(val_shots)} val shots "
          f"(video granularity, owner-only val)")
    if contaminated:
        print(f"ERROR: val contains {len(contaminated)} non-owner shots -- this is a bug, aborting")
        return 3
    if not val_shots:
        print("ERROR: val is EMPTY -- no owner-source shots available for validation. "
              "Label owner footage first; do NOT relax the allowlist to youtube.")
        return 1

    # ---- sequences (cache-first; extraction only on cache miss) -------------------------------
    span = args.window + max(args.window // 2, 1)
    cache_file = cache_path_for(labels)
    cache = load_cache(cache_file)
    cache = ensure_cache(shots, cache, span, args.pose_model, cache_file)

    Xtr, ttr, ctr, ktr, mtr, _vtr, st_tr = build_dataset(
        train_shots, cache, args.window, args.per_shot, args.seed,
        args.handedness, args.cue_tol, args.max_arm_missing)
    Xva, tva, cva, kva, mva, vva, st_va = build_dataset(
        val_shots, cache, args.window, args.per_shot, args.seed + 1,
        args.handedness, args.cue_tol, args.max_arm_missing)
    print(f"windows: {len(Xtr)} train / {len(Xva)} val  "
          f"(cue-supervised {int(ktr.sum())} / {int(kva.sum())}; "
          f"rejected arm-occluded {st_tr['rejected_arm'] + st_va['rejected_arm']}, "
          f"too-early {st_tr['rejected_short'] + st_va['rejected_short']}, "
          f"missing-cache shots {st_tr['shots_missing_cache'] + st_va['shots_missing_cache']})")
    if not len(Xtr) or not len(Xva):
        print("ERROR: empty train or val window set after rejection -- nothing to train on")
        return 1
    counts = {CUE_CLASSES[i]: int(np.sum(ctr[ktr] == i)) for i in range(len(CUE_CLASSES))}
    print(f"train cue classes (masked-in windows only): {counts}")

    # ---- model + optimization -----------------------------------------------------------------
    device = torch.device("cuda")
    model = build_model().to(device)  # default arch; overrides live in cue_temporal_model.py
    n_params = count_params(model)
    print(f"model: {n_params:,} params "
          f"(channels={list(DEFAULT_CHANNELS)}, kernel={DEFAULT_KERNEL}, "
          f"dilations={list(DEFAULT_DILATIONS)}, dropout={DEFAULT_DROPOUT})")
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs)
    if args.no_class_weights:
        ce = tnn.CrossEntropyLoss()
    else:
        # inverse-frequency weights, normalized to mean 1 so the 0.6/0.4 loss split holds;
        # capped at 10x mean so a near-empty class cannot dominate/destabilize the CE
        freq = np.array([max(counts[c], 1) for c in CUE_CLASSES], dtype=np.float64)
        w = (1.0 / freq)
        w = np.minimum(w / w.mean(), 10.0)
        w = w / w.mean()
        print(f"cue class weights: {dict(zip(CUE_CLASSES, [round(float(x), 2) for x in w]))}")
        ce = tnn.CrossEntropyLoss(weight=torch.tensor(w, dtype=torch.float32, device=device))
    mse = tnn.MSELoss()

    pt_path = os.path.join(out_dir, "cue_temporal_model.pt")
    meta_path = os.path.join(out_dir, "cue_temporal_model_meta.json")
    config = dict(arch_dict(), window=args.window, labels=os.path.abspath(labels),
                  seed=args.seed, val_frac=args.val_frac, per_shot=args.per_shot,
                  cue_tol=args.cue_tol, handedness=args.handedness,
                  max_arm_missing=args.max_arm_missing)

    best = {"timing_mae_ms": float("inf"), "green_hit_rate": -1.0}
    best_epoch = -1
    stale = 0
    n = len(Xtr)
    torch.manual_seed(args.seed)
    t0 = time.time()
    for epoch in range(args.epochs):
        model.train()
        perm = np.random.default_rng(args.seed + epoch).permutation(n)
        ep_loss = 0.0
        for i in range(0, n, args.batch):
            idx = perm[i:i + args.batch]
            xb = torch.from_numpy(Xtr[idx]).to(device)
            tb = torch.from_numpy(ttr[idx]).to(device)
            cb = torch.from_numpy(ctr[idx]).to(device)
            kb = torch.from_numpy(ktr[idx]).to(device)
            pred_t, logits = model(xb)
            # timing MSE in window-normalized units so 0.6/0.4 weighting is meaningful vs CE;
            # cue CE only where the class target is real supervision (two-tier mask)
            loss = args.w_timing * mse(pred_t / args.window, tb / args.window)
            if bool(kb.any()):
                loss = loss + args.w_cue * ce(logits[kb], cb[kb])
            opt.zero_grad()
            loss.backward()
            opt.step()
            ep_loss += float(loss.detach()) * len(idx)
        sched.step()

        model.eval()
        pt, pc = predict_in_batches(model, torch, Xva, device)
        m = compute_metrics(pt, tva, mva, pc, cva, kva)
        degen = is_degenerate(m)
        # Selection = val timing MAE; a near-constant predictor is NEVER selectable (the
        # 2026-08-09 artifact: hit-rate selection chose a constant "0 frames" output).
        improved = (not degen) and m["timing_mae_ms"] < best["timing_mae_ms"]
        hit_nz = m["green_hit_rate_offset2plus"]
        print(f"epoch {epoch + 1:3d}/{args.epochs}  loss {ep_loss / n:.4f}  "
              f"val MAE {m['timing_mae_ms']:6.1f}ms  hit {100 * m['green_hit_rate']:5.1f}%  "
              f"hit(off>=2) {100 * hit_nz:5.1f}%  pred-std {m['pred_std_frames']:5.2f}f  "
              f"cue-acc {100 * m['cue_acc']:5.1f}%  "
              f"{'DEGENERATE' if degen else ''}{'*' if improved else ''}")
        if improved:
            best = m
            best_epoch = epoch + 1
            stale = 0
            torch.save({"model_state": model.state_dict(), "config": config,
                        "metrics_val": best, "epoch": best_epoch}, pt_path)
        else:
            stale += 1
            if stale >= args.patience:
                print(f"early stop: no val MAE improvement in {args.patience} epochs")
                break

    if best_epoch < 0:
        print("\nERROR: every epoch was DEGENERATE (near-constant timing head) -- "
              "no checkpoint saved; do not ship anything from this run")
        return 1

    # ---- meta (the runtime contract) ----------------------------------------------------------
    meta = {
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window": args.window,
        "input_dim": INPUT_DIM,
        "input_layout": "(N, 51, T) float32 channels-first; per-joint [x, y, v] x 17 COCO joints",
        "classes": list(CUE_CLASSES),
        "outputs": {"frames_until_release": "(N,) float, frames from the window's last frame",
                    "cue_logits": f"(N, {len(CUE_CLASSES)}) float, order = classes"},
        "frames_to_ms": "ms = frames * 1000 * step / fps of the source clip (~16.7ms @ 60fps)",
        "green_window_ms": GREEN_WINDOW_MS,
        "normalization": normalization_meta(),
        "arch": dict(arch_dict(), params=n_params),
        "training": {"labels": os.path.abspath(labels), "seed": args.seed,
                     "val_frac": args.val_frac, "per_shot": args.per_shot,
                     "cue_tol": args.cue_tol, "handedness_default": args.handedness,
                     "max_arm_missing": args.max_arm_missing, "epochs_max": args.epochs,
                     "best_epoch": best_epoch, "lr": args.lr, "batch": args.batch,
                     "loss": f"{args.w_timing}*timing_mse + {args.w_cue}*cue_ce",
                     "class_weighted_ce": not args.no_class_weights,
                     "selection": "val timing MAE, degenerate (near-constant) epochs refused",
                     "train_windows": int(len(Xtr)), "val_windows": int(len(Xva)),
                     "train_cue_counts": counts,
                     "wall_clock_s": round(time.time() - t0, 1)},
        "metrics_val_best": best,
    }
    with open(meta_path, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2)

    print(f"\nDONE: best epoch {best_epoch} -- val MAE {best.get('timing_mae_ms')}ms, "
          f"green-window hit {100 * best.get('green_hit_rate', 0):.1f}%")
    print(f"weights: {pt_path}\nmeta:    {meta_path}")
    print("next:")
    print(f"  python tools/training/eval_cue_temporal_model.py --checkpoint \"{pt_path}\"")
    print(f"  python tools/training/export_cue_temporal_onnx.py --checkpoint \"{pt_path}\"")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
