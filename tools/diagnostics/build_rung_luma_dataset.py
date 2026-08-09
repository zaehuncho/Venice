#!/usr/bin/env python3
"""Build the RUNG-LUMA meter-locator datasets for the compressed (Chiaki) path.

Takes real live framedump sessions (logs/diagnostics/framedump/<session>/f*_raw.png),
auto-labels them with the PRODUCTION SimpleMeterReader (pristine bbox = GT), re-encodes
the sequence through the real Remote Play bandwidth ladder (reencode_ladder's x264
zerolatency CBR templates, dup mode = realistic per-frame bit budget), decodes, converts
each decoded frame to LUMA (gray replicated x3 — what the compressed path feeds the
locator), and writes:

  TRAIN mode (default)  datasets/meter_rung_luma_v1/<session>__<rung>/
      images/*.jpg  labels/*.txt  train.txt
    positives = frames where the pristine reader held a fresh (non-coast) detection;
    label = the pristine trackbox, normalized (class 0 'meter').
    negatives = a capped ratio of pristine no-meter frames with EMPTY label files.
    Feed these dirs to:  train_meter_detector.py --luma --extra-dataset <dir> ...

  EVAL mode (--eval)    <out>/<session>__<rung>/
      frames/f%05d.png  (EVERY decoded frame, luma)  +  labels.csv
    labels.csv columns pts_s,fill,x,y,w,h,shot_id (the eval_meter_locator --meta schema;
    x,y,w,h in ABSOLUTE eval-frame pixels; fill=nan + shot_id=-1 outside detections)
    -> eval_meter_locator.py --meta <labels.csv> --frames-dir <frames> --rung <name>

  --luma-twin SRCDIR    <out>/  luma copies of an existing labeled color dataset
    (images/ + labels/ hardlinked labels, train.txt) — boosts the gray share of the
    combined training set without new labeling.

The special rung name 'PristineY' skips re-encoding (luma of the original frames) —
isolates gray-domain loss from compression loss in the eval.

Usage (repo root):
  C:/Python314/python.exe tools/diagnostics/build_rung_luma_dataset.py \
      --session session_20260706_190737 --rungs Balanced,UltraLow --max-frames 2500
  C:/Python314/python.exe tools/diagnostics/build_rung_luma_dataset.py \
      --session session_20260704_210801 --rungs PristineY,Performance,Balanced,UltraLow \
      --eval --out logs/diagnostics/rung_luma_eval
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import sys

# repo root must win over the retired tools/diagnostics simple_meter_reader twin
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, os.pardir, os.pardir))
try:
    sys.path.remove(_REPO)
except ValueError:
    pass
sys.path.insert(0, _REPO)

import cv2  # noqa: E402
import numpy as np  # noqa: E402


def _load_reencode_ladder():
    path = os.path.join(_REPO, "tools", "diagnostics", "reencode_ladder.py")
    spec = importlib.util.spec_from_file_location("reencode_ladder_probe", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_RL = _load_reencode_ladder()
MS_PER_IDX = 64.6      # framedump cadence (fit for session_20260704_210801; ~15fps for all)


def to_luma3(bgr):
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return cv2.merge([g, g, g])


def encode_decode(frames, rung, mode, work_dir):
    """reencode_ladder.encode_rung + its decode loop -> [(orig_index, BGR)]."""
    out_mp4 = os.path.join(work_dir, f"_{rung}_{mode}.mp4")
    dup = _RL.encode_rung(frames, rung, mode, out_mp4)
    cap = cv2.VideoCapture(out_mp4)
    decoded = []
    k = 0
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                break
            if k % dup == 0 and len(decoded) < len(frames):
                decoded.append((frames[len(decoded)][0], fr))
            k += 1
    finally:
        cap.release()
    try:
        os.unlink(out_mp4)
    except OSError:
        pass
    return decoded


def shot_ids(pristine_rows, gap=8):
    """Group detected frames into shots: runs of det frames whose index gap <= `gap`
    share a shot_id; everything else is -1."""
    ids = {}
    sid = -1
    last_i = None
    for r in pristine_rows:
        if r["det"]:
            if last_i is None or (r["i"] - last_i) > gap:
                sid += 1
            ids[r["i"]] = sid
            last_i = r["i"]
    return ids


def valid_box(r):
    b = r.get("bbox") or [0, 0, 0, 0]
    return b[2] > 2 and b[3] > 2


def write_train_rung(decoded, pris_by_i, pw, ph, out_dir, neg_ratio=0.33, jpg_q=92):
    img_d = os.path.join(out_dir, "images")
    lbl_d = os.path.join(out_dir, "labels")
    os.makedirs(img_d, exist_ok=True)
    os.makedirs(lbl_d, exist_ok=True)
    kept = []
    n_pos = n_neg = 0
    for i, frame in decoded:
        pr = pris_by_i.get(i)
        if pr is None:
            continue
        pos = pr["det"] and valid_box(pr)
        if not pos and n_neg >= neg_ratio * max(1, n_pos):
            continue
        name = f"f{i:05d}"
        ip = os.path.join(img_d, name + ".jpg")
        cv2.imwrite(ip, to_luma3(frame), [cv2.IMWRITE_JPEG_QUALITY, jpg_q])
        with open(os.path.join(lbl_d, name + ".txt"), "w") as f:
            if pos:
                x, y, w, h = pr["bbox"]
                f.write(f"0 {(x + w / 2) / pw:.6f} {(y + h / 2) / ph:.6f} "
                        f"{w / pw:.6f} {h / ph:.6f}\n")
                n_pos += 1
            else:
                n_neg += 1
        kept.append(ip)
    with open(os.path.join(out_dir, "train.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    return n_pos, n_neg


def write_eval_rung(decoded, pris_by_i, pw, ph, out_dir):
    fr_d = os.path.join(out_dir, "frames")
    os.makedirs(fr_d, exist_ok=True)
    sids = shot_ids([pris_by_i[i] for i, _ in decoded if i in pris_by_i])
    rows = []
    for i, frame in decoded:
        pr = pris_by_i.get(i)
        if pr is None:
            continue
        lum = to_luma3(frame)
        rh, rw = lum.shape[:2]
        cv2.imwrite(os.path.join(fr_d, f"f{i:05d}.png"), lum)
        if pr["det"] and valid_box(pr):
            sx, sy = rw / pw, rh / ph
            x, y, w, h = pr["bbox"]
            rows.append((i * MS_PER_IDX / 1000.0, pr["fill"],
                         x * sx, y * sy, w * sx, h * sy, sids.get(i, -1)))
        else:
            rows.append((i * MS_PER_IDX / 1000.0, float("nan"), 0, 0, 0, 0, -1))
    csv_p = os.path.join(out_dir, "labels.csv")
    with open(csv_p, "w", encoding="utf-8") as f:
        f.write("pts_s,fill,x,y,w,h,shot_id\n")
        for r in rows:
            f.write(f"{r[0]:.4f},{r[1]},{r[2]:.1f},{r[3]:.1f},{r[4]:.1f},{r[5]:.1f},{r[6]}\n")
    n_lab = sum(1 for r in rows if np.isfinite(r[1]))
    return csv_p, n_lab, len(rows)


def build_session(session, rungs, mode, max_frames, out_root, eval_mode, neg_ratio):
    sdir = os.path.join(_REPO, "logs", "diagnostics", "framedump", session)
    frames = _RL.list_frames(sdir, max_frames)
    if not frames:
        raise FileNotFoundError(f"no frames in {sdir}")
    first = cv2.imread(frames[0][1])
    ph, pw = first.shape[:2]
    print(f"[{session}] {len(frames)} frames @ {pw}x{ph}; pristine reader pass ...")

    # single imread pass: run the pristine reader AND drop unreadable/truncated PNGs so the
    # encode list and the decoded-frame alignment only ever see good frames.
    good = []

    def _gen():
        for i, p in frames:
            im = cv2.imread(p)
            if im is None:
                print(f"[{session}] skip unreadable {os.path.basename(p)}")
                continue
            good.append((i, p))
            yield i, im

    pristine = _RL.run_reader(_gen(), MS_PER_IDX)
    frames = good
    pris_by_i = {r["i"]: r for r in pristine}
    n_det = sum(1 for r in pristine if r["det"] and valid_box(r))
    print(f"[{session}] pristine detections with box: {n_det}/{len(frames)}")

    for rung in rungs:
        tag = rung if (eval_mode or rung == "PristineY") else f"{rung}_{mode}"
        out_dir = os.path.join(out_root, f"{session}__{tag}")
        os.makedirs(out_dir, exist_ok=True)
        if rung == "PristineY":
            decoded = [(i, cv2.imread(p)) for i, p in frames]
        else:
            print(f"[{session}/{rung}] encode+decode ({mode}) ...")
            decoded = encode_decode(frames, rung, mode, out_dir)
        if eval_mode:
            csv_p, n_lab, n_all = write_eval_rung(decoded, pris_by_i, pw, ph, out_dir)
            print(f"[{session}/{rung}] EVAL: {n_all} frames, {n_lab} labeled -> {csv_p}")
        else:
            n_pos, n_neg = write_train_rung(decoded, pris_by_i, pw, ph, out_dir,
                                            neg_ratio=neg_ratio)
            print(f"[{session}/{rung}] TRAIN: {n_pos} pos + {n_neg} neg -> {out_dir}")


def build_luma_twin(src, out_dir, stride):
    """Luma copies of an existing labeled color dataset (images/ + labels/)."""
    import glob as _g
    import shutil
    src = src if os.path.isabs(src) else os.path.join(_REPO, src)
    imgs = sorted(sum((_g.glob(os.path.join(src, "images", e))
                       for e in ("*.png", "*.jpg", "*.jpeg")), []))[::max(1, stride)]
    img_d = os.path.join(out_dir, "images")
    lbl_d = os.path.join(out_dir, "labels")
    os.makedirs(img_d, exist_ok=True)
    os.makedirs(lbl_d, exist_ok=True)
    kept = []
    for p in imgs:
        im = cv2.imread(p)
        if im is None:
            continue
        name = os.path.splitext(os.path.basename(p))[0]
        ip = os.path.join(img_d, name + ".jpg")
        cv2.imwrite(ip, to_luma3(im), [cv2.IMWRITE_JPEG_QUALITY, 92])
        lp = os.path.join(src, "labels", name + ".txt")
        dl = os.path.join(lbl_d, name + ".txt")
        if os.path.isfile(lp):
            shutil.copyfile(lp, dl)
        else:
            open(dl, "w").close()
        kept.append(ip)
    with open(os.path.join(out_dir, "train.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")
    print(f"[luma-twin] {len(kept)} images {src} -> {out_dir}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--session", action="append", default=None)
    ap.add_argument("--rungs", default="Balanced,UltraLow",
                    help=f"comma list from {list(_RL.RUNGS)} + PristineY")
    ap.add_argument("--mode", default="dup", choices=["dup", "seq"])
    ap.add_argument("--max-frames", type=int, default=2500)
    ap.add_argument("--out", default=os.path.join("datasets", "meter_rung_luma_v1"))
    ap.add_argument("--eval", action="store_true",
                    help="write eval frames + labels.csv instead of a training dir")
    ap.add_argument("--neg-ratio", type=float, default=0.33)
    ap.add_argument("--luma-twin", default=None, metavar="SRCDIR",
                    help="instead of sessions: luma-copy an existing labeled dataset")
    ap.add_argument("--stride", type=int, default=1, help="luma-twin image stride")
    args = ap.parse_args(argv)

    out_root = args.out if os.path.isabs(args.out) else os.path.join(_REPO, args.out)
    if args.luma_twin:
        build_luma_twin(args.luma_twin, out_root, args.stride)
        return 0
    rungs = [r.strip() for r in args.rungs.split(",") if r.strip()]
    for r in rungs:
        if r != "PristineY" and r not in _RL.RUNGS:
            raise SystemExit(f"unknown rung {r!r}")
    for session in (args.session or ["session_20260704_210801"]):
        build_session(session, rungs, args.mode, args.max_frames, out_root,
                      args.eval, args.neg_ratio)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
