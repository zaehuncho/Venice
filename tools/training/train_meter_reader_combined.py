#!/usr/bin/env python3
"""Train the sub-pixel METER READER on SYNTHETIC + REAL crops with a masked loss (the
sim-to-real fix). The synthetic-only model over-reads real fill by ~+20-25% because the
synthetic renderer paints a LIGHT-grey filled track while the real meter has a DARK empty
track (see extract_meter_reader_real_data.py). Here:

  * SYNTHETIC samples (perfect sub-pixel labels)   -> MSE over all 3 outputs
    [fill_frac, green_top_frac, green_bottom_frac] : sub-pixel smoothness + full coverage.
  * REAL samples (row-count fill label, fill_only=1): MSE over the FILL output ONLY -> pins
    the real appearance + absolute calibration. Green is masked out (real green labels are
    unreliable AND the serving path consumes only fill_pct).

REAL crops are upsampled (--real-weight) so they aren't swamped by the larger synthetic set.

MeterReaderNet + preprocess() are IMPORTED from tools/training/train_meter_reader.py, so they
remain BIT-IDENTICAL to meter_reader_infer.py. Outputs models/meter_reader.pt + .json.

USAGE:
  py tools/training/train_meter_reader_combined.py \
     --synth datasets/meter_reader_synth/meter_reader_data.npz \
     --real  datasets/meter_reader_real_162915/meter_reader_real.npz \
     --init  models/meter_reader.pt --epochs 40 --real-weight 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(_HERE))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
import train_meter_reader as T  # _make_net + preprocess (bit-identical to meter_reader_infer)  # noqa: E402

OUT_PT = os.path.join(ROOT, "models", "meter_reader.pt")
OUT_JSON = os.path.join(ROOT, "models", "meter_reader.json")


def _load(npz, input_h, input_w):
    d = np.load(npz)
    images, labels, split = d["images"], d["labels"], d["split"]
    fo = d["fill_only"] if "fill_only" in d else np.zeros((len(images),), np.uint8)
    X = np.stack([T.preprocess(im, input_h, input_w) for im in images]).astype(np.float32)
    return X, labels.astype(np.float32), split.astype(np.uint8), fo.astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--synth", default=os.path.join(ROOT, "datasets", "meter_reader_synth", "meter_reader_data.npz"))
    ap.add_argument("--real", required=True)
    ap.add_argument("--real-weight", type=float, default=4.0, help="upsample factor for real train rows")
    ap.add_argument("--init", default="", help="warm-start weights (e.g. the synthetic model)")
    ap.add_argument("--epochs", type=int, default=40)
    ap.add_argument("--device", default="auto")
    ap.add_argument("--batch", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--input-h", type=int, default=96)
    ap.add_argument("--input-w", type=int, default=32)
    args = ap.parse_args()

    import torch
    from torch.utils.data import DataLoader, TensorDataset

    device = ("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device
    ih, iw = args.input_h, args.input_w

    Xs, Ys, Ss, Fs = _load(args.synth, ih, iw)
    Xr, Yr, Sr, Fr = _load(args.real, ih, iw)
    print(f"synth: {len(Xs)} ({(Ss==0).sum()} tr / {(Ss==1).sum()} va)   "
          f"real: {len(Xr)} ({(Sr==0).sum()} tr / {(Sr==1).sum()} va)")

    # weights: synth=1, real=real_weight (only affects the sampler via repetition on train)
    def build(mask_split):
        Xl, Yl, Fl, Wl = [], [], [], []
        for X, Y, S, F, w in ((Xs, Ys, Ss, Fs, 1.0), (Xr, Yr, Sr, Fr, args.real_weight)):
            m = S == mask_split
            reps = int(round(w)) if mask_split == 0 else 1
            for _ in range(max(1, reps)):
                Xl.append(X[m]); Yl.append(Y[m]); Fl.append(F[m])
                Wl.append(np.full((int(m.sum()),), w if mask_split == 0 else 1.0, np.float32))
        return (np.concatenate(Xl), np.concatenate(Yl), np.concatenate(Fl), np.concatenate(Wl))

    Xtr, Ytr, Ftr, _ = build(0)
    Xva, Yva, Fva, _ = build(1)

    xt = torch.from_numpy(Xtr); yt = torch.from_numpy(Ytr); ft = torch.from_numpy(Ftr)
    xv = torch.from_numpy(Xva); yv = torch.from_numpy(Yva); fv = torch.from_numpy(Fva)
    tr = DataLoader(TensorDataset(xt, yt, ft), batch_size=args.batch, shuffle=True)
    va = DataLoader(TensorDataset(xv, yv, fv), batch_size=args.batch, shuffle=False)

    net = T._make_net(torch).to(device)
    if args.init and os.path.exists(args.init):
        net.load_state_dict(torch.load(args.init, map_location=device, weights_only=True))
        print(f"warm-started from {args.init}")
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)

    def masked_loss(pred, y, fo):
        # fill (col 0): always. green (cols 1,2): only where fill_only==0 (synthetic).
        fill_l = (pred[:, 0] - y[:, 0]) ** 2
        gmask = (1.0 - fo)
        green_l = ((pred[:, 1:] - y[:, 1:]) ** 2).mean(dim=1) * gmask
        denom = gmask.sum().clamp(min=1.0)
        return fill_l.mean() + green_l.sum() / denom

    os.makedirs(os.path.dirname(OUT_PT), exist_ok=True)
    best = float("inf")
    for ep in range(args.epochs):
        net.train(); tl = 0.0; n = 0
        for xb, yb, fb in tr:
            xb, yb, fb = xb.to(device), yb.to(device), fb.to(device)
            opt.zero_grad()
            loss = masked_loss(net(xb), yb, fb)
            loss.backward(); opt.step()
            tl += loss.item() * len(xb); n += len(xb)
        tl /= max(1, n)

        net.eval(); vfill = 0.0; nv = 0
        with torch.no_grad():
            for xb, yb, fb in va:
                xb, yb, fb = xb.to(device), yb.to(device), fb.to(device)
                p = net(xb)
                vfill += torch.abs(p[:, 0] - yb[:, 0]).sum().item(); nv += len(xb)
        vfill = (vfill / max(1, nv)) * 100.0
        print(f"epoch {ep+1}/{args.epochs}  train_loss={tl:.5f}  val_fill_MAE={vfill:.3f}%")
        if vfill <= best:
            best = vfill
            torch.save(net.state_dict(), OUT_PT)

    if not os.path.exists(OUT_PT):
        torch.save(net.state_dict(), OUT_PT)
    meta = {"input_h": ih, "input_w": iw, "normalize": "div255",
            "outputs": ["fill_frac", "green_top_frac", "green_bottom_frac"],
            "arch": "meter_reader_cnn_v1", "best_val_fill_mae_pct": float(best),
            "trained_on": "synth+real_masked"}
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"saved {OUT_PT} (best_val_fill_MAE={best:.3f}%) + {OUT_JSON}")


if __name__ == "__main__":
    main()
