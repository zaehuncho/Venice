#!/usr/bin/env python3
"""Head-to-head pose model eval: NEW (yolo26n fine-tune) vs SHIPPED (orion_pose2k_n_v2).

Runs on RUNTIME-DOMAIN framedump frames (real capture-card 1280x720 PNGs from
D:\\VeniceArchive\\framedump) which NEITHER model trained on. Deliberately does NOT lead
with the pseudo-label val split: those labels come from the yolo26x teacher that NEW was
distilled from, so val mAP measures teacher-copying, not quality (see `biasedval`).

Subcommands:
  sweep     both models over windows of CONSECUTIVE frames; measures detection rate,
            keypoint confidence, frame-to-frame jitter (median/p95 px, co-tracked pairs =
            same frame AND same player for both models), dropped-detection count, and
            cross-model spatial disagreement per keypoint group (NEW's tracked player vs
            SHIPPED's best-overlapping detection, so tracker choice cannot skew it).
  render    side-by-side skeleton overlays for the frames where the models disagree most
            on the shooting-arm joints (human adjudication set).
  latency   per-frame predict latency for both models, full 1280x720 @640 and player
            crop @256, same conditions for both.
  biasedval Ultralytics mAP of both models on a subsample of the teacher-labeled val
            split. REPORTED ONLY AS THE BIASED NUMBER - never the headline.

Read-only w.r.t. models and framedumps. Outputs land under
logs/diagnostics/pose_eval_headtohead_20260809/ (gitignored via logs/).

Usage (system Python312 has CUDA torch + ultralytics):
  python tools/training/pose_headtohead_eval.py sweep
  python tools/training/pose_headtohead_eval.py render
  python tools/training/pose_headtohead_eval.py latency
  python tools/training/pose_headtohead_eval.py biasedval --n 400 --scratch <dir>
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import statistics
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
NEW_PT = os.path.join(ROOT, "logs", "diagnostics", "pose_train", "pose2k_n_eff_phase2",
                      "weights", "best.pt")
SHIPPED_PT = os.path.join(ROOT, "models", "orion_pose2k_n_v2.pt")
OUT_DIR = os.path.join(ROOT, "logs", "diagnostics", "pose_eval_headtohead_20260809")

# Windows of consecutive frames, distinct contexts (verified by eye):
#   0 crowded Theater game (normal avatars, multi-person)
#   1 practice gym, normal human avatar
#   2 practice gym, gold statue avatar (edge-case skin)
DEFAULT_WINDOWS = [
    r"D:\VeniceArchive\framedump\session_20260804_201020:2000:250",
    r"D:\VeniceArchive\framedump\session_20260804_202151:1000:250",
    r"D:\VeniceArchive\framedump\session_20260806_092247:3000:250",
]

CONF_PREDICT = 0.10   # floor passed to predict; detection rate reported at 0.25 too
CONF_DET = 0.25       # a "detection" for rate/drop purposes
KPT_VIS = 0.30        # keypoint counted only when both sides exceed this
IOU_TRACK = 0.10      # track continuation threshold
IOU_XMODEL = 0.30     # cross-model same-person match threshold

GROUPS = {
    "head": [0, 1, 2, 3, 4],
    "shoulders": [5, 6],
    "elbows": [7, 8],
    "wrists": [9, 10],
    "hips": [11, 12],
    "knees": [13, 14],
    "ankles": [15, 16],
}
ARM = [5, 6, 7, 8, 9, 10]  # shoulders+elbows+wrists = what release timing reads

SKELETON = [(0, 1), (0, 2), (1, 3), (2, 4), (5, 6), (5, 7), (7, 9), (6, 8), (8, 10),
            (5, 11), (6, 12), (11, 12), (11, 13), (13, 15), (12, 14), (14, 16)]


def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def load_models(new_pt, shipped_pt):
    os.environ.setdefault("YOLO_VERBOSE", "False")
    from ultralytics import YOLO
    return {"NEW": YOLO(new_pt), "SHIPPED": YOLO(shipped_pt)}


def frame_paths(window):
    """(frame_number, path) pairs; suffix is _0_raw or _1_raw depending on the dump slot."""
    d, start, count = window.rsplit(":", 2)
    start, count = int(start), int(count)
    out = []
    for i in range(start, start + count):
        g = sorted(glob.glob(os.path.join(d, f"f{i:05d}_*_raw.png")))
        if g:
            out.append((i, g[0]))
    return out


def detect(model, img):
    """All detections >= CONF_PREDICT as (box[4], conf, kpts[17,3]), conf-descending, cap 8."""
    r = model.predict(img, verbose=False, conf=CONF_PREDICT, imgsz=640, device=0)[0]
    dets = []
    if r.boxes is not None and len(r.boxes):
        boxes = r.boxes.xyxy.cpu().numpy()
        confs = r.boxes.conf.cpu().numpy()
        kpts = r.keypoints.data.cpu().numpy()  # (N,17,3)
        order = np.argsort(-confs)[:8]
        for j in order:
            dets.append((boxes[j].astype(float), float(confs[j]), kpts[j].astype(float)))
    return dets


def pct(vals, q):
    return float(np.percentile(np.asarray(vals, dtype=float), q)) if len(vals) else float("nan")


# ==============================================================================================
# sweep
# ==============================================================================================

def cmd_sweep(args):
    import cv2
    models = load_models(args.new, args.shipped)
    windows = args.windows
    os.makedirs(OUT_DIR, exist_ok=True)

    mnames = list(models)
    rec = {m: {"valid": [], "matched": [], "box": [], "conf": [], "kpts": [],
               "ndet": [], "topconf": []} for m in mnames}
    alldets = {m: [] for m in mnames}   # per frame: list of (box, conf, kpts)
    paths_all, window_id, frame_no = [], [], []

    t0 = time.time()
    for wi, w in enumerate(windows):
        pairs = frame_paths(w)
        print(f"window {wi}: {w} -> {len(pairs)} frames")
        track = {m: None for m in mnames}
        for fno, p in pairs:
            img = cv2.imread(p)
            if img is None:
                continue
            paths_all.append(p)
            window_id.append(wi)
            frame_no.append(fno)
            for mname, model in models.items():
                dets = detect(model, img)
                alldets[mname].append(dets)
                R = rec[mname]
                R["ndet"].append(sum(1 for d in dets if d[1] >= CONF_DET))
                R["topconf"].append(dets[0][1] if dets else 0.0)
                tb = track[mname]
                chosen, matched = None, False
                if dets:
                    if tb is not None:
                        ious = [iou(tb, d[0]) for d in dets]
                        j = int(np.argmax(ious))
                        if ious[j] >= IOU_TRACK:
                            chosen, matched = dets[j], True
                    if chosen is None:
                        chosen = dets[0]  # (re)acquire highest conf
                if chosen is not None:
                    track[mname] = chosen[0]
                    R["valid"].append(True)
                    R["matched"].append(matched)
                    R["box"].append(chosen[0])
                    R["conf"].append(chosen[1])
                    R["kpts"].append(chosen[2])
                else:
                    track[mname] = None
                    R["valid"].append(False)
                    R["matched"].append(False)
                    R["box"].append(np.zeros(4))
                    R["conf"].append(0.0)
                    R["kpts"].append(np.zeros((17, 3)))

    n = len(paths_all)
    print(f"swept {n} frames x {len(models)} models in {time.time() - t0:.0f}s")

    for m in rec:
        for k in ("valid", "matched", "box", "conf", "kpts", "ndet", "topconf"):
            rec[m][k] = np.asarray(rec[m][k])
    window_id = np.asarray(window_id)
    frame_no = np.asarray(frame_no)

    def consecutive(i):
        return window_id[i] == window_id[i - 1] and frame_no[i] == frame_no[i - 1] + 1

    stats = {"n_frames": n, "windows": windows, "conf_det": CONF_DET, "kpt_vis": KPT_VIS,
             "per_model": {}, "cross_model": {}, "per_window": {}}

    # ---- per-model: detection rate, kpt conf, jitter (own track), drops ----------------------
    for m in mnames:
        R = rec[m]
        det_rate_25 = float(np.mean(R["ndet"] >= 1))
        mean_top = float(np.mean(R["topconf"][R["topconf"] > 0])) if np.any(R["topconf"] > 0) else 0.0

        kptconf = {}
        vk = R["kpts"][R["valid"]]
        for g, idx in GROUPS.items():
            kptconf[g] = float(np.mean(vk[:, idx, 2])) if len(vk) else float("nan")

        jit_all, jit_grp, jit_box = [], {g: [] for g in GROUPS}, []
        for i in range(1, n):
            if not consecutive(i):
                continue
            if not (R["valid"][i] and R["valid"][i - 1] and R["matched"][i]):
                continue
            k0, k1 = R["kpts"][i - 1], R["kpts"][i]
            vis = (k0[:, 2] > KPT_VIS) & (k1[:, 2] > KPT_VIS)
            d = np.linalg.norm(k1[:, :2] - k0[:, :2], axis=1)
            jit_all.extend(d[vis].tolist())
            for g, idx in GROUPS.items():
                jit_grp[g].extend(d[[j for j in idx if vis[j]]].tolist())
            c0 = (R["box"][i - 1][:2] + R["box"][i - 1][2:]) / 2
            c1 = (R["box"][i][:2] + R["box"][i][2:]) / 2
            jit_box.append(float(np.linalg.norm(c1 - c0)))

        # drops: player tracked at i-1 vanishes from ALL detections at i, back at i+1
        drops = []
        for i in range(1, n - 1):
            if not (consecutive(i) and consecutive(i + 1)):
                continue
            if not R["valid"][i - 1]:
                continue
            b = R["box"][i - 1]
            here = any(iou(b, d[0]) >= IOU_TRACK for d in alldets[m][i])
            back = any(iou(b, d[0]) >= 0.2 for d in alldets[m][i + 1])
            if not here and back:
                drops.append(os.path.basename(paths_all[i]))

        stats["per_model"][m] = {
            "det_rate_conf25": round(det_rate_25, 4),
            "mean_top_conf": round(mean_top, 4),
            "kpt_conf_by_group": {g: round(v, 4) for g, v in kptconf.items()},
            "jitter_own_track_px": {
                "all_median": round(pct(jit_all, 50), 2), "all_p95": round(pct(jit_all, 95), 2),
                "wrists_median": round(pct(jit_grp["wrists"], 50), 2),
                "wrists_p95": round(pct(jit_grp["wrists"], 95), 2),
                "elbows_median": round(pct(jit_grp["elbows"], 50), 2),
                "elbows_p95": round(pct(jit_grp["elbows"], 95), 2),
                "box_center_median": round(pct(jit_box, 50), 2),
                "box_center_p95": round(pct(jit_box, 95), 2),
                "n_pairs": len(jit_box),
            },
            "dropped_detections": {"count": len(drops), "frames": drops[:40]},
        }

    # ---- cross-model: NEW's tracked player vs SHIPPED's best-overlapping DETECTION -----------
    A = rec["NEW"]
    xm_valid = np.zeros(n, bool)
    xm_shp_box = np.zeros((n, 4))
    xm_shp_kpts = np.zeros((n, 17, 3))
    for i in range(n):
        if not A["valid"][i]:
            continue
        cands = alldets["SHIPPED"][i]
        if not cands:
            continue
        ious = [iou(A["box"][i], d[0]) for d in cands]
        j = int(np.argmax(ious))
        if ious[j] >= IOU_XMODEL:
            xm_valid[i] = True
            xm_shp_box[i] = cands[j][0]
            xm_shp_kpts[i] = cands[j][2]

    # reverse coverage: does NEW see the player SHIPPED tracks?
    Bv = rec["SHIPPED"]
    rev_match_arr = np.zeros(n, bool)
    rev_valid = np.zeros(n, bool)
    for i in range(n):
        if not Bv["valid"][i]:
            continue
        rev_valid[i] = True
        cands = alldets["NEW"][i]
        if cands and max(iou(Bv["box"][i], d[0]) for d in cands) >= IOU_XMODEL:
            rev_match_arr[i] = True
    rev_match, rev_total = int(rev_match_arr.sum()), int(rev_valid.sum())

    dist_grp, dist_all, per_frame_arm = {g: [] for g in GROUPS}, [], []
    arm_by_frame = np.full(n, np.nan)
    for i in range(n):
        if not xm_valid[i]:
            continue
        ka, kb = A["kpts"][i], xm_shp_kpts[i]
        vis = (ka[:, 2] > KPT_VIS) & (kb[:, 2] > KPT_VIS)
        d = np.linalg.norm(ka[:, :2] - kb[:, :2], axis=1)
        dist_all.extend(d[vis].tolist())
        for g, idx in GROUPS.items():
            dist_grp[g].extend(d[[j for j in idx if vis[j]]].tolist())
        arm_vis = [j for j in ARM if vis[j]]
        if len(arm_vis) >= 3:
            per_frame_arm.append((float(np.mean(d[arm_vis])), i))
            arm_by_frame[i] = per_frame_arm[-1][0]

    # co-tracked jitter: SAME (frame,player) pairs for both models
    co_pairs = 0
    co_jit = {m: {"all": [], "wrists": []} for m in mnames}
    for i in range(1, n):
        if not consecutive(i):
            continue
        if not (xm_valid[i] and xm_valid[i - 1] and A["matched"][i]):
            continue
        if iou(xm_shp_box[i - 1], xm_shp_box[i]) < IOU_TRACK:
            continue
        co_pairs += 1
        for m, k0, k1 in (("NEW", A["kpts"][i - 1], A["kpts"][i]),
                          ("SHIPPED", xm_shp_kpts[i - 1], xm_shp_kpts[i])):
            vis = (k0[:, 2] > KPT_VIS) & (k1[:, 2] > KPT_VIS)
            d = np.linalg.norm(k1[:, :2] - k0[:, :2], axis=1)
            co_jit[m]["all"].extend(d[vis].tolist())
            co_jit[m]["wrists"].extend(d[[j for j in GROUPS["wrists"] if vis[j]]].tolist())

    box_h = [float(b[3] - b[1]) for b, v in zip(A["box"], A["valid"]) if v]
    stats["cross_model"] = {
        "frames_new_tracked": int(A["valid"].sum()),
        "frames_same_player_matched": int(xm_valid.sum()),
        "shipped_finds_news_player_rate": round(float(xm_valid.sum()) / max(1, int(A["valid"].sum())), 4),
        "new_finds_shippeds_player_rate": round(rev_match / max(1, rev_total), 4),
        "mean_tracked_box_height_px": round(float(np.mean(box_h)) if box_h else 0.0, 1),
        "kpt_dist_px": {
            "all_median": round(pct(dist_all, 50), 2), "all_p95": round(pct(dist_all, 95), 2),
            **{f"{g}_median": round(pct(dist_grp[g], 50), 2) for g in GROUPS},
            **{f"{g}_p95": round(pct(dist_grp[g], 95), 2) for g in GROUPS},
        },
        "arm_disagreement_median_px": round(pct([a for a, _ in per_frame_arm], 50), 2),
        "arm_disagreement_p95_px": round(pct([a for a, _ in per_frame_arm], 95), 2),
        "co_tracked_jitter_px": {
            "n_pairs": co_pairs,
            **{f"{m}_all_median": round(pct(co_jit[m]["all"], 50), 2) for m in mnames},
            **{f"{m}_all_p95": round(pct(co_jit[m]["all"], 95), 2) for m in mnames},
            **{f"{m}_wrists_median": round(pct(co_jit[m]["wrists"], 50), 2) for m in mnames},
            **{f"{m}_wrists_p95": round(pct(co_jit[m]["wrists"], 95), 2) for m in mnames},
        },
    }

    # ---- per-window detection + agreement breakdown ------------------------------------------
    for wi in sorted(set(window_id.tolist())):
        sel = window_id == wi
        arm_w = [a for a, i in per_frame_arm if window_id[i] == wi]
        stats["per_window"][str(wi)] = {
            "frames": int(sel.sum()),
            "NEW_det_rate": round(float(np.mean(rec["NEW"]["ndet"][sel] >= 1)), 4),
            "SHIPPED_det_rate": round(float(np.mean(rec["SHIPPED"]["ndet"][sel] >= 1)), 4),
            "same_player_match_rate": round(float(xm_valid[sel].sum()) / max(1, int(A["valid"][sel].sum())), 4),
            "new_finds_shippeds_player_rate": round(
                float(rev_match_arr[sel].sum()) / max(1, int(rev_valid[sel].sum())), 4),
            "arm_disagreement_median_px": round(pct(arm_w, 50), 2),
        }

    per_frame_arm.sort(reverse=True)
    np.savez_compressed(
        os.path.join(OUT_DIR, "sweep_data.npz"),
        window_id=window_id, frame_no=frame_no,
        new_valid=A["valid"], new_box=A["box"], new_kpts=A["kpts"], new_conf=A["conf"],
        shp_valid=rec["SHIPPED"]["valid"], shp_box=rec["SHIPPED"]["box"],
        shp_kpts=rec["SHIPPED"]["kpts"], shp_conf=rec["SHIPPED"]["conf"],
        xm_valid=xm_valid, xm_shp_box=xm_shp_box, xm_shp_kpts=xm_shp_kpts,
        rev_match=rev_match_arr, rev_valid=rev_valid,
        new_ndet=rec["NEW"]["ndet"], shp_ndet=rec["SHIPPED"]["ndet"],
        arm_by_frame=arm_by_frame,
        top_disagree_idx=np.asarray([i for _, i in per_frame_arm[:40]], dtype=int),
        top_disagree_px=np.asarray([a for a, _ in per_frame_arm[:40]], dtype=float),
    )
    with open(os.path.join(OUT_DIR, "sweep_frames.json"), "w", encoding="utf-8") as fh:
        json.dump(paths_all, fh)
    with open(os.path.join(OUT_DIR, "sweep_stats.json"), "w", encoding="utf-8") as fh:
        json.dump(stats, fh, indent=2)
    print(json.dumps(stats, indent=2))
    print(f"\nwrote {OUT_DIR}\\sweep_stats.json")
    return 0


# ==============================================================================================
# render
# ==============================================================================================

def draw_skel(img, kpts, color):
    import cv2
    for a, b in SKELETON:
        if kpts[a, 2] > KPT_VIS and kpts[b, 2] > KPT_VIS:
            cv2.line(img, (int(kpts[a, 0]), int(kpts[a, 1])),
                     (int(kpts[b, 0]), int(kpts[b, 1])), color, 2, cv2.LINE_AA)
    for j in range(17):
        if kpts[j, 2] > KPT_VIS:
            r = 5 if j in (9, 10) else 3  # wrists bigger
            cv2.circle(img, (int(kpts[j, 0]), int(kpts[j, 1])), r, color, -1, cv2.LINE_AA)


def cmd_render(args):
    import cv2
    data = np.load(os.path.join(OUT_DIR, "sweep_data.npz"))
    with open(os.path.join(OUT_DIR, "sweep_frames.json"), "r", encoding="utf-8") as fh:
        paths = json.load(fh)
    out = os.path.join(OUT_DIR, "side_by_side")
    os.makedirs(out, exist_ok=True)
    # top-K PER WINDOW so one divergent context cannot monopolize the adjudication set
    arm = data["arm_by_frame"]
    wid = data["window_id"]
    picks = []
    for w in sorted(set(wid.tolist())):
        cand = [(arm[i], i) for i in range(len(arm)) if wid[i] == w and np.isfinite(arm[i])]
        cand.sort(reverse=True)
        picks.extend(cand[:args.per_window])
    picks.sort(reverse=True)
    idxs = [i for _, i in picks]
    dpx = [a for a, _ in picks]
    made = []
    for rank, (i, dv) in enumerate(zip(idxs, dpx)):
        img = cv2.imread(paths[i])
        if img is None:
            continue
        nb, sb = data["new_box"][i], data["xm_shp_box"][i]
        u = [min(nb[0], sb[0]), min(nb[1], sb[1]), max(nb[2], sb[2]), max(nb[3], sb[3])]
        w, h = u[2] - u[0], u[3] - u[1]
        mx, my = 0.35 * w + 20, 0.25 * h + 20
        x1, y1 = max(0, int(u[0] - mx)), max(0, int(u[1] - my))
        x2, y2 = min(img.shape[1], int(u[2] + mx)), min(img.shape[0], int(u[3] + my))
        crop = img[y1:y2, x1:x2]
        scale = max(1.0, 460.0 / max(1, crop.shape[0]))
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

        def panel(kpts, box, color, label):
            p = crop.copy()
            k = kpts.copy()
            k[:, 0] = (k[:, 0] - x1) * scale
            k[:, 1] = (k[:, 1] - y1) * scale
            draw_skel(p, k, color)
            bx = ((box - [x1, y1, x1, y1]) * scale).astype(int)
            cv2.rectangle(p, (bx[0], bx[1]), (bx[2], bx[3]), color, 1)
            cv2.putText(p, label, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
            cv2.putText(p, label, (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
            return p

        left = panel(data["new_kpts"][i], data["new_box"][i], (80, 220, 80), "NEW (yolo26n)")
        right = panel(data["xm_shp_kpts"][i], data["xm_shp_box"][i], (60, 140, 255), "SHIPPED (v2)")
        divider = np.full((left.shape[0], 8, 3), 255, np.uint8)
        combo = np.hstack([left, divider, right])
        strip = np.full((36, combo.shape[1], 3), 30, np.uint8)
        tag = f"{os.path.relpath(paths[i], r'D:\VeniceArchive\framedump')}  arm-disagreement {dv:.1f}px"
        cv2.putText(strip, tag, (8, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
        combo = np.vstack([strip, combo])
        fp = os.path.join(out, f"disagree_{rank:02d}_{dv:.0f}px.png")
        cv2.imwrite(fp, combo)
        made.append(fp)
    print("\n".join(made))
    return 0


def cmd_misses(args):
    """Render frames where NEW could not find the player SHIPPED tracks (rev_match False),
    and frames where NEW had no detection at all. SHIPPED skeleton orange; NEW's own tracked
    box (possibly a different player) green. No model needed - uses sweep data."""
    import cv2
    data = np.load(os.path.join(OUT_DIR, "sweep_data.npz"))
    with open(os.path.join(OUT_DIR, "sweep_frames.json"), "r", encoding="utf-8") as fh:
        paths = json.load(fh)
    out = os.path.join(OUT_DIR, "new_misses")
    os.makedirs(out, exist_ok=True)
    n = len(paths)
    rev_miss = [i for i in range(n) if data["rev_valid"][i] and not data["rev_match"][i]]
    blind = [i for i in range(n) if data["new_ndet"][i] == 0]
    made = []
    for label, idxs in (("revmiss", rev_miss[::max(1, len(rev_miss) // args.per_kind)][:args.per_kind]),
                        ("blind", blind[:args.per_kind])):
        for i in idxs:
            img = cv2.imread(paths[i])
            if img is None:
                continue
            if data["shp_valid"][i]:
                draw_skel(img, data["shp_kpts"][i], (60, 140, 255))
                b = data["shp_box"][i].astype(int)
                cv2.rectangle(img, (b[0], b[1]), (b[2], b[3]), (60, 140, 255), 2)
                cv2.putText(img, "SHIPPED track", (b[0], max(20, b[1] - 8)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (60, 140, 255), 2)
            if data["new_valid"][i]:
                b = data["new_box"][i].astype(int)
                cv2.rectangle(img, (b[0], b[1]), (b[2], b[3]), (80, 220, 80), 2)
                cv2.putText(img, "NEW track", (b[0], min(img.shape[0] - 8, b[3] + 20)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (80, 220, 80), 2)
            tag = os.path.relpath(paths[i], r"D:\VeniceArchive\framedump")
            cv2.putText(img, f"{label}: {tag}", (10, img.shape[0] - 12),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
            fp = os.path.join(out, f"{label}_{i:04d}.png")
            cv2.imwrite(fp, img)
            made.append(fp)
    print(f"rev_miss frames total: {len(rev_miss)}  blind frames total: {len(blind)}")
    print("\n".join(made))
    return 0


# ==============================================================================================
# latency
# ==============================================================================================

def cmd_latency(args):
    import cv2
    models = load_models(args.new, args.shipped)
    img = cv2.imread(args.frame)
    if img is None:
        print(f"cannot read {args.frame}")
        return 2
    results = {}
    for mname, model in models.items():
        for _ in range(10):
            model.predict(img, verbose=False, conf=CONF_PREDICT, imgsz=640, device=0)
        ts = []
        for _ in range(args.reps):
            t = time.perf_counter()
            model.predict(img, verbose=False, conf=CONF_PREDICT, imgsz=640, device=0)
            ts.append((time.perf_counter() - t) * 1000)
        full = {"median_ms": round(statistics.median(ts), 1), "p95_ms": round(pct(ts, 95), 1)}

        dets = detect(model, img)
        crop_stats = None
        if dets:
            b = dets[0][0]
            w, h = b[2] - b[0], b[3] - b[1]
            x1, y1 = max(0, int(b[0] - 0.2 * w)), max(0, int(b[1] - 0.2 * h))
            x2, y2 = min(img.shape[1], int(b[2] + 0.2 * w)), min(img.shape[0], int(b[3] + 0.2 * h))
            crop = img[y1:y2, x1:x2]
            for _ in range(10):
                model.predict(crop, verbose=False, conf=CONF_PREDICT, imgsz=256, device=0)
            ts = []
            for _ in range(args.reps):
                t = time.perf_counter()
                model.predict(crop, verbose=False, conf=CONF_PREDICT, imgsz=256, device=0)
                ts.append((time.perf_counter() - t) * 1000)
            crop_stats = {"median_ms": round(statistics.median(ts), 1),
                          "p95_ms": round(pct(ts, 95), 1),
                          "crop_px": f"{crop.shape[1]}x{crop.shape[0]}"}
        results[mname] = {"full_1280x720_imgsz640": full, "crop_imgsz256": crop_stats}
    print(json.dumps(results, indent=2))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "latency.json"), "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    return 0


# ==============================================================================================
# biasedval (reported only as the biased number)
# ==============================================================================================

def cmd_biasedval(args):
    import shutil
    src = r"C:\VeniceTraining\no_meter_prep\pose_ds"
    imgs = sorted(glob.glob(os.path.join(src, "images", "val", "*")))
    step = max(1, len(imgs) // args.n)
    pick = imgs[::step][:args.n]
    ds = os.path.join(args.scratch, "biasedval_ds")
    for sub in ("images/val", "labels/val"):
        os.makedirs(os.path.join(ds, sub.replace("/", os.sep)), exist_ok=True)
    n_ok = 0
    for ip in pick:
        stem = os.path.splitext(os.path.basename(ip))[0]
        lp = os.path.join(src, "labels", "val", stem + ".txt")
        if not os.path.isfile(lp):
            continue
        shutil.copy2(ip, os.path.join(ds, "images", "val"))
        shutil.copy2(lp, os.path.join(ds, "labels", "val"))
        n_ok += 1
    yaml = os.path.join(ds, "ds.yaml")
    with open(yaml, "w", encoding="utf-8") as fh:
        fh.write(f"path: {ds}\ntrain: images/val\nval: images/val\n"
                 "kpt_shape: [17, 3]\n"
                 "flip_idx: [0, 2, 1, 4, 3, 6, 5, 8, 7, 10, 9, 12, 11, 14, 13, 16, 15]\n"
                 "names:\n  0: person\n")
    print(f"biased val subset: {n_ok} teacher-labeled frames")
    models = load_models(args.new, args.shipped)
    out = {}
    for mname, model in models.items():
        r = model.val(data=yaml, imgsz=640, batch=8, device=0, verbose=False, plots=False,
                      project=args.scratch, name=f"biasedval_{mname}", exist_ok=True)
        out[mname] = {"pose_mAP50": round(float(r.pose.map50), 4),
                      "pose_mAP50_95": round(float(r.pose.map), 4),
                      "box_mAP50": round(float(r.box.map50), 4)}
    print("\nBIASED (teacher-labeled val; NEW distilled from this teacher -- do not headline):")
    print(json.dumps(out, indent=2))
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "biasedval.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--new", default=NEW_PT)
    ap.add_argument("--shipped", default=SHIPPED_PT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("sweep")
    sp.add_argument("--windows", nargs="+", default=DEFAULT_WINDOWS,
                    help="dir:start:count windows of consecutive frames")
    rp = sub.add_parser("render")
    rp.add_argument("--per-window", type=int, default=4)
    mp = sub.add_parser("misses")
    mp.add_argument("--per-kind", type=int, default=6)
    lp = sub.add_parser("latency")
    lp.add_argument("--frame",
                    default=r"D:\VeniceArchive\framedump\session_20260804_202151\f01200_0_raw.png")
    lp.add_argument("--reps", type=int, default=60)
    bp = sub.add_parser("biasedval")
    bp.add_argument("--n", type=int, default=400)
    bp.add_argument("--scratch", required=True)
    args = ap.parse_args()
    return {"sweep": cmd_sweep, "render": cmd_render, "misses": cmd_misses,
            "latency": cmd_latency, "biasedval": cmd_biasedval}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
