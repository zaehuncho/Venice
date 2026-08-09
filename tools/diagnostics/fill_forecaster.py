#!/usr/bin/env python3
"""Fill-trajectory forecaster (model #3) — the PRECISION lever for the shot-meter release.

WHY: the green make-window is ~30-40ms wide in time but observe->decide latency is ~55ms, so a REACTIVE
release can't catch it (proven: ~27% in-window at a 110ms lead). You must FORECAST: given the fill curve so
far, predict "ms until the fill reaches the tip (the green window at the top)". The engine then subtracts the
measured total latency to pick the fire instant. This is complementary to the POSE wrist-Y zero-crossing model
(temporal_release_predict_v2.py, release_predictor_v2.pt) — that reads the shooter's arm; THIS reads the meter.

SELF-SUPERVISED: no human labels, no flaky early/late self-grade. For any recorded shot the input is the fill
curve up to time t and the label is the ACTUAL frames until the fill peaked (the tip), read straight from the
same recording's FUTURE frames. Every fed meter in the detframes*.csv logs (and every shot clip) is free data.

TARGET = frames-to-tip (tip = the per-shot peak-fill frame ~= the green window at the top; timing the peak =
greens, per the tip-target success model). Stable + unambiguous, unlike the contested green sliver.

DATA sources (both -> the same uniform-60fps resampled per-shot rise):
  (A) detframes*.csv  — the LIVE detector's per-frame fill/green log (abundant, real distribution). PRIMARY.
  (B) video clips     — re-run MeterDetector over the offline shot clips (clean, controlled). SUPPLEMENT.

USAGE:
  C:\\Python314\\python.exe tools/diagnostics/fill_forecaster.py data      # extract + report shot/sample counts
  C:\\Python314\\python.exe tools/diagnostics/fill_forecaster.py train     # train + leave-one-out eval
  C:\\Python314\\python.exe tools/diagnostics/fill_forecaster.py eval <detframes.csv>
Outputs models/fill_forecaster/fill_forecaster.pt. Export ONNX for the sidecar/native runtime later.
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import sys

import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

WINDOW = 12          # causal history fed to the model (~200ms at 60fps)
FPS = 60.0
MS_PER_FRAME = 1000.0 / FPS
MAX_HORIZON = 40     # only label frames within ~660ms of the tip (before that the fill is too flat to forecast)
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

MODEL_DIR = os.path.join(ROOT, "models", "fill_forecaster")
MODEL_PATH = os.path.join(MODEL_DIR, "fill_forecaster.pt")
META_PATH = os.path.join(MODEL_DIR, "fill_forecaster.json")


def _save_meta(bias_frames):
    import json
    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(dict(window=WINDOW, fps=FPS, n_features=4, bias_frames=float(bias_frames),
                       target="frames_to_tip"), f, indent=2)

# The live logs to mine. Recent, large, current-detector CSVs (older Jun-14 ones used a different color/path).
DEFAULT_CSV_GLOB = os.path.join(ROOT, "logs", "diagnostics", "detframes*.csv")


# ---------------------------------------------------------------------------------------------------------------
# Shot segmentation + resampling: turn a raw per-frame (t_ms, fill, green, detected) stream into clean per-shot
# rises on a uniform 60fps grid, each with its tip (peak) frame.
# ---------------------------------------------------------------------------------------------------------------
def _segment_stream(t_ms, fill, green, detected):
    """Yield per-shot dicts {t, fill, green, tip} (uniform 60fps grid) from one raw stream.

    A valid shot = a run of DETECTED frames with a small inter-frame time gap where the fill RISES from low
    (<45%) to a peak (>72%) over a plausible 80-700ms, i.e. a real meter climb (rejects static false-locks and
    HUD blobs, which don't rise)."""
    shots = []
    n = len(t_ms)
    i = 0
    while i < n:
        if not detected[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and detected[j + 1] and (t_ms[j + 1] - t_ms[j]) < 200.0:
            j += 1
        # segment [i, j]
        seg_t = t_ms[i:j + 1]
        seg_f = fill[i:j + 1]
        seg_g = green[i:j + 1]
        if len(seg_t) >= 5:
            fmin = float(np.min(seg_f))
            tip_rel = int(np.argmax(seg_f))          # first peak
            fpk = float(seg_f[tip_rel])
            tmin_rel = int(np.argmin(seg_f[:tip_rel + 1])) if tip_rel > 0 else 0
            rise_ms = seg_t[tip_rel] - seg_t[tmin_rel]
            if fmin < 45.0 and fpk > 72.0 and tip_rel > tmin_rel and 80.0 <= rise_ms <= 700.0:
                shot = _resample_shot(seg_t, seg_f, seg_g)
                if shot is not None:
                    shots.append(shot)
        i = j + 1
    return shots


def _resample_shot(seg_t, seg_f, seg_g):
    """Resample a raw shot segment onto a uniform 60fps grid; recompute the tip on the grid. Keep from the rise
    start through a few frames past the tip."""
    seg_t = np.asarray(seg_t, float)
    seg_f = np.asarray(seg_f, float)
    seg_g = np.asarray(seg_g, float)
    t0, t1 = seg_t[0], seg_t[-1]
    if t1 - t0 < 80.0:
        return None
    grid = np.arange(t0, t1 + 1e-3, MS_PER_FRAME)
    if len(grid) < WINDOW + 2:
        return None
    fg = np.interp(grid, seg_t, seg_f)
    gg = np.interp(grid, seg_t, seg_g)
    tip = int(np.argmax(fg))
    if tip < 3:
        return None
    end = min(len(grid), tip + 4)                    # keep a little past the tip
    return dict(t=grid[:end], fill=fg[:end], green=gg[:end], tip=tip)


# ---------------------------------------------------------------------------------------------------------------
# Feature builder (causal window ending at index i). Kept in PHYSICAL units (absolute fill level matters — a
# meter at 85% is near the tip, at 30% is far — so NOT per-window normalized like the pose model).
# ---------------------------------------------------------------------------------------------------------------
def build_features(fill, green, i, window=WINDOW):
    seg_f = fill[i - window:i] / 100.0               # absolute fill 0..1
    seg_g = green[i - window:i] / 100.0
    vel = np.gradient(seg_f) * 10.0                  # scaled to ~fill magnitude
    acc = np.gradient(vel) * 10.0
    gap = np.clip(seg_g - seg_f, -0.2, 1.0)          # distance below the green window (-> 0 at the tip)
    feats = np.stack([seg_f, vel, acc, gap], axis=0).astype(np.float32)
    return feats


class FillForecastNet(nn.Module):
    """Small 1D-CNN over the fill history -> frames-to-tip (log-scale). Same backbone family as the proven pose
    predictor, sized for the shorter fill window."""

    def __init__(self, window=WINDOW, n_features=4):
        super().__init__()
        self.conv1 = nn.Conv1d(n_features, 32, 5, padding=2)
        self.bn1 = nn.BatchNorm1d(32)
        self.conv2 = nn.Conv1d(32, 64, 3, padding=1)
        self.bn2 = nn.BatchNorm1d(64)
        self.conv3 = nn.Conv1d(64, 64, 3, padding=1)
        self.bn3 = nn.BatchNorm1d(64)
        self.gap = nn.AdaptiveAvgPool1d(1)
        self.fc1 = nn.Linear(64, 32)
        self.fc2 = nn.Linear(32, 1)
        self.relu = nn.ReLU()
        self.drop = nn.Dropout(0.2)

    def forward(self, x):
        x = self.relu(self.bn1(self.conv1(x)))
        x = self.relu(self.bn2(self.conv2(x)))
        x = self.relu(self.bn3(self.conv3(x)))
        x = self.gap(x).squeeze(-1)
        x = self.drop(x)
        x = self.relu(self.fc1(x))
        return self.fc2(x).squeeze(-1)


class FillDataset(Dataset):
    def __init__(self, shots, augment=True):
        self.samples = []                            # (fill, green, i, label_frames)
        self.augment = augment
        for s in shots:
            fill, green, tip = s["fill"], s["green"], s["tip"]
            for i in range(WINDOW, tip + 1):
                h = tip - i                          # frames to tip
                if h > MAX_HORIZON:
                    continue
                self.samples.append((fill, green, i, float(h)))

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        fill, green, i, label = self.samples[idx]
        feats = build_features(fill, green, i)
        if self.augment and np.random.random() < 0.5:
            feats = feats + np.random.randn(*feats.shape).astype(np.float32) * 0.03
        target = np.log1p(label)                     # frames-to-tip >= 0 -> plain log1p
        return (torch.from_numpy(feats),
                torch.tensor(target, dtype=torch.float32),
                torch.tensor(label, dtype=torch.float32))


# ---------------------------------------------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------------------------------------------
def load_csv_shots(csv_glob=DEFAULT_CSV_GLOB):
    shots, per_file = [], []
    for path in sorted(glob.glob(csv_glob)):
        t_ms, fill, green, det = [], [], [], []
        try:
            with open(path, newline="", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    try:
                        t_ms.append(float(row["t_ms"]))
                        fill.append(float(row["fill_pct"]))
                        green.append(float(row.get("green_center_pct", 96.0)))
                        det.append(int(float(row["detected"])))
                    except (ValueError, KeyError):
                        continue
        except OSError:
            continue
        if len(t_ms) < 20:
            continue
        s = _segment_stream(np.array(t_ms), np.array(fill), np.array(green), np.array(det))
        per_file.append((os.path.basename(path), len(s)))
        shots.extend(s)
    return shots, per_file


def load_video_shots():
    """Supplement: re-run MeterDetector over the offline shot clips -> fill trajectories (clean, controlled)."""
    import cv2
    from meter_detector import MeterDetector, load_detector_config
    CLIPS = [
        ("NBA 2K26_20260624212008.mp4", 3000, 150, "Red"),
        ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Red"),
        ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Red"),
        ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Red"),
        ("NBA 2K26_20260521032018.mp4", 3000, 11940, "Red"),
    ]
    VIDEOS = r"C:\Users\Administrator\Videos"
    shots = []
    for name, count, start, color in CLIPS:
        path = os.path.join(VIDEOS, name)
        if not os.path.exists(path):
            continue
        cfg = load_detector_config(os.path.join(ROOT, "settings.json"))
        cfg.meter_style = "Arrow2"; cfg.meter_color = color; cfg.auto_meter_color = False
        det = MeterDetector(os.path.join(ROOT, "meter_styles"), cfg)
        det.set_active_style("Arrow2")
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
        cap.set(cv2.CAP_PROP_POS_FRAMES, start)
        t_ms, fill, green, dd = [], [], [], []
        n = 0
        while n < count:
            ok, frame = cap.read()
            if not ok:
                break
            r = det.detect(frame)
            fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
            t_ms.append((start + n) / fps * 1000.0)
            fill.append(float(r.fill_pct) if fed else 0.0)
            green.append(float(getattr(r, "green_window_center_pct", 96.0)) if fed else 96.0)
            dd.append(1 if fed else 0)
            n += 1
        cap.release()
        shots.extend(_segment_stream(np.array(t_ms), np.array(fill), np.array(green), np.array(dd)))
    return shots


# ---------------------------------------------------------------------------------------------------------------
def _in_window_report(offsets, tag):
    if len(offsets) < 5:
        print(f"  {tag}: too few ({len(offsets)})")
        return
    arr = np.array(offsets, float)
    med = np.median(arr)
    iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
    w40 = int(np.sum(np.abs(arr - med) <= 40))
    w80 = int(np.sum(np.abs(arr - med) <= 80))
    print(f"  {tag}: n={len(arr)} median={med:+.1f}ms IQR={iqr:.1f}ms "
          f"±40ms={w40}/{len(arr)} ±80ms={w80}/{len(arr)}")


def _horizon_eval(model, shots):
    print(f"\n  Open-loop error by horizon (predict frames-to-tip, h frames before the tip):")
    print(f"  {'Horizon':>10} {'n':>5} {'MAE':>9} {'IQR':>9} {'±40ms':>9} {'±80ms':>9}")
    print("  " + "-" * 58)
    for h in [1, 2, 3, 5, 8, 10, 15, 20]:
        errs = []
        for s in shots:
            i = s["tip"] - h
            if i < WINDOW:
                continue
            feats = build_features(s["fill"], s["green"], i)
            with torch.no_grad():
                p = model(torch.from_numpy(feats).unsqueeze(0).to(DEVICE))
                pf = float(torch.expm1(torch.clamp(p, min=0)).item())
            errs.append((pf - h) * MS_PER_FRAME)
        if len(errs) >= 5:
            e = np.array(errs)
            mae = np.mean(np.abs(e)); med = np.median(e)
            iqr = np.percentile(e, 75) - np.percentile(e, 25)
            w40 = int(np.sum(np.abs(e - med) <= 40)); w80 = int(np.sum(np.abs(e - med) <= 80))
            print(f"  {h:3d} frames {len(errs):>6} {mae:>+7.1f}ms {iqr:>7.1f}ms "
                  f"{f'{w40}/{len(errs)}':>9} {f'{w80}/{len(errs)}':>9}")


def _measure_bias(model, shots):
    """The near-tip systematic bias (median over-prediction at h=2 frames), which the engine subtracts as a
    calibration constant. Returned in FRAMES."""
    errs = []
    for s in shots:
        i = s["tip"] - 2
        if i < WINDOW:
            continue
        feats = build_features(s["fill"], s["green"], i)
        with torch.no_grad():
            pf = float(torch.expm1(torch.clamp(model(torch.from_numpy(feats).unsqueeze(0).to(DEVICE)), min=0)).item())
        errs.append(pf - 2)
    return float(np.median(errs)) if errs else 0.0


def _closedloop_eval(model, shots, bias_frames=0.0):
    """Simulate the real trigger: fire when the (bias-corrected) predicted frames-to-tip first drops to the
    latency lead L; report the scatter of the actual (fire - tip) offset. Sweep L so we see the whole latency
    budget. If the prediction never reaches L (fast/steep shot) fire at its minimum. This is the release-timing
    scatter the engine inherits from the forecaster."""
    print(f"\n  Closed-loop trigger (bias-corr {bias_frames*MS_PER_FRAME:+.0f}ms), fire at latency lead L:")
    for L in [3, 5, 7, 9]:
        offs = []
        for s in shots:
            fired = None
            best_pf, best_i = 1e9, None
            for i in range(WINDOW, s["tip"] + 4):
                feats = build_features(s["fill"], s["green"], i)
                with torch.no_grad():
                    pf = float(torch.expm1(torch.clamp(model(torch.from_numpy(feats).unsqueeze(0).to(DEVICE)), min=0)).item())
                pf -= bias_frames
                if pf < best_pf:
                    best_pf, best_i = pf, i
                if pf <= L:
                    fired = i
                    break
            if fired is None:
                fired = best_i
            if fired is not None:
                # ideal fire = tip - L; report deviation from that ideal (0 = perfectly on the lead)
                offs.append(((fired - (s["tip"] - L))) * MS_PER_FRAME)
        _in_window_report(offs, f"L={L}f ({L*MS_PER_FRAME:.0f}ms lead)")


def train(use_video=False):
    print("FILL-TRAJECTORY FORECASTER — training")
    print(f"Device: {DEVICE}")
    print("=" * 70)
    shots, per_file = load_csv_shots()
    print("  CSV shots per file:")
    for name, k in per_file:
        if k:
            print(f"    {k:4d}  {name}")
    if use_video:
        v = load_video_shots()
        print(f"  video clips: +{len(v)} shots")
        shots.extend(v)
    print(f"  TOTAL shots: {len(shots)}")
    if len(shots) < 20:
        print("  [error] too few shots — widen the CSV glob or add --video")
        return

    rng = np.random.RandomState(0)
    idx = rng.permutation(len(shots))
    nv = max(4, int(len(shots) * 0.15))
    val_shots = [shots[i] for i in idx[:nv]]
    tr_shots = [shots[i] for i in idx[nv:]]

    tr_ds = FillDataset(tr_shots, augment=True)
    val_ds = FillDataset(val_shots, augment=False)
    print(f"  samples: {len(tr_ds)} train / {len(val_ds)} val  ({len(tr_shots)}/{len(val_shots)} shots)")
    if len(tr_ds) < 100:
        print("  [error] too few samples")
        return

    coll = lambda b: (torch.stack([x[0] for x in b]), torch.stack([x[1] for x in b]),
                      torch.stack([x[2] for x in b]))
    tl = DataLoader(tr_ds, batch_size=128, shuffle=True, num_workers=0, collate_fn=coll)
    vl = DataLoader(val_ds, batch_size=128, shuffle=False, num_workers=0, collate_fn=coll)

    net = FillForecastNet().to(DEVICE)
    opt = torch.optim.Adam(net.parameters(), lr=1e-3, weight_decay=1e-4)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=60)
    crit = nn.SmoothL1Loss(beta=0.5)

    os.makedirs(MODEL_DIR, exist_ok=True)
    best = 1e9
    epochs = 60
    for ep in range(epochs):
        net.train()
        for feats, tgt, _ in tl:
            feats, tgt = feats.to(DEVICE), tgt.to(DEVICE)
            opt.zero_grad()
            loss = crit(net(feats), tgt)
            loss.backward(); opt.step()
        sch.step()
        net.eval()
        vmae = 0.0; nb = 0
        with torch.no_grad():
            for feats, tgt, lab in vl:
                feats = feats.to(DEVICE)
                pf = torch.expm1(torch.clamp(net(feats).cpu(), min=0))
                vmae += torch.abs(pf - lab).mean().item(); nb += 1
        vmae /= max(1, nb)
        if (ep + 1) % 10 == 0 or ep == 0:
            print(f"    ep{ep+1:3d}: val_MAE={vmae:.2f} frames ({vmae*MS_PER_FRAME:.0f}ms)")
        if vmae < best:
            best = vmae
            torch.save(net.state_dict(), MODEL_PATH)
    print(f"\n  best val_MAE={best:.2f} frames ({best*MS_PER_FRAME:.0f}ms)  saved {MODEL_PATH}")

    net.load_state_dict(torch.load(MODEL_PATH, weights_only=True)); net.eval()
    bias = _measure_bias(net, tr_shots)                # calibrate on TRAIN, apply to VAL (no leakage)
    print(f"\n  near-tip bias (from train) = {bias:+.2f} frames ({bias*MS_PER_FRAME:+.0f}ms) -> engine subtracts this")
    _save_meta(bias)
    _horizon_eval(net, val_shots)
    _closedloop_eval(net, val_shots, bias_frames=bias)


def evaluate(csv_path):
    if not os.path.exists(MODEL_PATH):
        print(f"  [error] no model at {MODEL_PATH}; train first")
        return
    net = FillForecastNet().to(DEVICE)
    net.load_state_dict(torch.load(MODEL_PATH, weights_only=True)); net.eval()
    shots, _ = load_csv_shots(csv_path)
    print(f"  {csv_path}: {len(shots)} shots")
    if not shots:
        return
    _horizon_eval(net, shots)
    _closedloop_eval(net, shots)


def data_report(use_video=False):
    shots, per_file = load_csv_shots()
    print("Shot extraction report")
    print("=" * 70)
    tot = 0
    for name, k in per_file:
        print(f"  {k:4d} shots  {name}")
        tot += k
    print(f"  --- CSV total: {tot} shots")
    if use_video:
        v = load_video_shots()
        print(f"  video clips: {len(v)} shots")
        shots.extend(v)
    if shots:
        rises = [s["tip"] for s in shots]
        durs = [(s["t"][s["tip"]] - s["t"][0]) for s in shots]
        pk = [float(s["fill"][s["tip"]]) for s in shots]
        ds = FillDataset(shots, augment=False)
        print(f"\n  shots={len(shots)}  training_samples={len(ds)}")
        print(f"  tip frame-idx: median={np.median(rises):.0f}  rise dur ms: median={np.median(durs):.0f} "
              f"[{np.min(durs):.0f}..{np.max(durs):.0f}]")
        print(f"  peak fill%: median={np.median(pk):.0f} [{np.min(pk):.0f}..{np.max(pk):.0f}]")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode")
    d = sub.add_parser("data"); d.add_argument("--video", action="store_true")
    t = sub.add_parser("train"); t.add_argument("--video", action="store_true")
    e = sub.add_parser("eval"); e.add_argument("csv")
    args = ap.parse_args()
    if args.mode == "data":
        data_report(args.video)
    elif args.mode == "train":
        train(args.video)
    elif args.mode == "eval":
        evaluate(args.csv)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
