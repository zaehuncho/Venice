#!/usr/bin/env python3
"""Train the COMPRESSED-STREAM METER READER (MeterReaderNet-Y) — a 1-channel LUMA student CNN
that regresses continuous fill + the green make-window bounds AND arbitrates meter presence from
a LOCATED meter crop's Y (luma) plane alone. This is the compressed-path sibling of the BGR
sub-pixel reader (tools/training/train_meter_reader.py): on a chroma-collapsed / re-encoded feed
the red/green cues are gone, so the reader must lean on luma structure only.

Input : the Y (luma) plane of a meter crop, resized to 128x48 (H x W; the meter is a tall vertical
        bar so H >> W to preserve sub-pixel vertical resolution).
Heads : reg     -> 3 sigmoids [fill_frac, green_top_frac, green_bottom_frac]
        present -> 1 logit (is a real meter present? — the acquisition-arbiter signal)
        log_var -> 1 aleatoric log-variance of fill (heteroscedastic; clamped to [-6, 2]).

Loss  : heteroscedastic (aleatoric) masked loss, see meter_y_loss(). Each masked term is normalised
        by its own mask sum (not the batch size) so a batch with few real-fill / few green rows is
        not silently down-weighted.

MeterReaderNetY (_make_net) and preprocess() below MUST stay BIT-IDENTICAL to meter_reader_y_infer.py.

DATA SCHEMA (npz):
  images [N,128,48,1] uint8   (H=128, W=48, Y plane)
  labels [N,4]        float32 = (fill_frac, green_top_frac, green_bottom_frac, present)
  m_fill [N]          float32   mask: 1 where the fill label is trustworthy
  m_green[N]          float32   mask: 1 where the green-window labels are trustworthy
  rung   [N]          int8      (carried through; not used by the loss)
  split  [N]          uint8     0 = train, 1 = val
  sigma  [N]          float32   OPTIONAL per-label fill noise (pp); default 1.0 when absent -> w=1/sigma^2

USAGE:
  py tools/training/train_meter_reader_y.py \
     --data datasets/meter_reader_y_synth/data.npz \
     --data-real datasets/meter_reader_y_real/data.npz --real-weight 2 --epochs 60
Outputs models/meter_reader_y.pt (state_dict) + models/meter_reader_y.json.
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_PT = os.path.join(ROOT, "models", "meter_reader_y.pt")

INPUT_H = 128
INPUT_W = 48


# --------------------------------------------------------------------------- #
#  model + preprocess  (BIT-IDENTICAL to meter_reader_y_infer.py)
# --------------------------------------------------------------------------- #
def _make_net(torch):
    import torch.nn as nn

    class MeterReaderNetY(nn.Module):
        """1x128x48 luma -> 4 conv blocks (1->16->32->64->96) -> GAP -> FC48 -> 3 heads.
        MaxPool(2) after blocks 1,2,3 only. MUST match meter_reader_y_infer.py."""

        def __init__(self):
            super().__init__()
            self.conv1 = nn.Conv2d(1, 16, 3, padding=1); self.bn1 = nn.BatchNorm2d(16)
            self.conv2 = nn.Conv2d(16, 32, 3, padding=1); self.bn2 = nn.BatchNorm2d(32)
            self.conv3 = nn.Conv2d(32, 64, 3, padding=1); self.bn3 = nn.BatchNorm2d(64)
            self.conv4 = nn.Conv2d(64, 96, 3, padding=1); self.bn4 = nn.BatchNorm2d(96)
            self.pool = nn.MaxPool2d(2)
            self.gap = nn.AdaptiveAvgPool2d(1)
            self.fc1 = nn.Linear(96, 48)
            self.head_reg = nn.Linear(48, 3)      # fill, green_top, green_bottom (sigmoid)
            self.head_present = nn.Linear(48, 1)  # presence logit
            self.head_logvar = nn.Linear(48, 1)   # aleatoric log-variance of fill
            self.relu = nn.ReLU(); self.drop = nn.Dropout(0.2)

        def forward(self, x):
            x = self.pool(self.relu(self.bn1(self.conv1(x))))
            x = self.pool(self.relu(self.bn2(self.conv2(x))))
            x = self.pool(self.relu(self.bn3(self.conv3(x))))
            x = self.relu(self.bn4(self.conv4(x)))
            x = self.gap(x).flatten(1)
            h = self.drop(self.relu(self.fc1(x)))
            reg = torch.sigmoid(self.head_reg(h))
            present_logit = self.head_present(h).squeeze(-1)
            log_var = torch.clamp(self.head_logvar(h).squeeze(-1), -6.0, 2.0)
            return reg, present_logit, log_var

    return MeterReaderNetY()


def preprocess(y_crop, input_h=INPUT_H, input_w=INPUT_W):
    """uint8 Y crop -> 1xHxW float32 in [0,1]. MUST match meter_reader_y_infer.py.

    Accepts [H,W] or [H,W,1] uint8. Resizes with cv2.INTER_AREA only when the crop is not already
    input_h x input_w (cv2 is imported lazily so an already-sized crop needs no cv2)."""
    arr = np.asarray(y_crop)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    if arr.shape[0] != input_h or arr.shape[1] != input_w:
        import cv2
        arr = cv2.resize(arr, (input_w, input_h), interpolation=cv2.INTER_AREA)
    x = arr.astype(np.float32) / 255.0
    return x[np.newaxis, :, :]


# --------------------------------------------------------------------------- #
#  loss  (importable for tests)
# --------------------------------------------------------------------------- #
def meter_y_loss(reg, present_logit, log_var, labels, m_fill, m_green, sigma):
    """Heteroscedastic masked loss. All args are torch tensors on one device:
        reg           [N,3] sigmoid (fill, green_top, green_bottom)
        present_logit [N]   presence logit
        log_var       [N]   aleatoric log-variance of fill (already clamped)
        labels        [N,4] (fill, green_top, green_bottom, present)
        m_fill        [N]   fill mask, m_green [N] green mask, sigma [N] per-label fill noise (pp)

    L = m_fill * w * [0.5*exp(-s)*(fill_hat-fill)^2 + 0.5*s]           (fill, aleatoric)
      + m_green * [(gtop_hat-gtop)^2 + (gbot_hat-gbot)^2]              (green window MSE)
      + 0.5 * BCEWithLogits(present_hat, present)                     (presence)
    with s = log_var, w = 1/sigma^2. Each masked term is divided by its OWN mask sum (min 1), not
    the batch size. Returns (total, parts) where parts holds detached fill/green/present scalars."""
    import torch
    import torch.nn.functional as F

    w = 1.0 / sigma.clamp(min=1e-6) ** 2
    s = log_var
    fill_hat = reg[:, 0]
    fill = labels[:, 0]
    fill_term = m_fill * w * (0.5 * torch.exp(-s) * (fill_hat - fill) ** 2 + 0.5 * s)
    fill_loss = fill_term.sum() / m_fill.sum().clamp(min=1.0)

    green_term = m_green * ((reg[:, 1] - labels[:, 1]) ** 2 + (reg[:, 2] - labels[:, 2]) ** 2)
    green_loss = green_term.sum() / m_green.sum().clamp(min=1.0)

    present_loss = 0.5 * F.binary_cross_entropy_with_logits(present_logit, labels[:, 3])

    total = fill_loss + green_loss + present_loss
    parts = {"fill": fill_loss.detach(), "green": green_loss.detach(), "present": present_loss.detach()}
    return total, parts


# --------------------------------------------------------------------------- #
#  numpy validation metrics
# --------------------------------------------------------------------------- #
def _auc_rank(y_true, y_score):
    """Rank-based (Mann-Whitney U) AUC, numpy only. y_true is 0/1 (present>0.5). nan if one class."""
    y_true = np.asarray(y_true).ravel() > 0.5
    y_score = np.asarray(y_score, dtype=np.float64).ravel()
    n_pos = int(y_true.sum()); n_neg = int((~y_true).sum())
    if n_pos == 0 or n_neg == 0:
        return float("nan")
    order = np.argsort(y_score, kind="mergesort")
    ranks = np.empty(len(y_score), dtype=np.float64)
    ranks[order] = np.arange(1, len(y_score) + 1)
    # average ties so equal scores share a rank (keeps AUC unbiased on flat/constant scores)
    _, inv, counts = np.unique(y_score, return_inverse=True, return_counts=True)
    csum = np.cumsum(counts)
    start = csum - counts
    avg = (start + csum + 1) / 2.0  # mean of the 1-based rank block per unique value
    ranks = avg[inv]
    return (ranks[y_true].sum() - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


# --------------------------------------------------------------------------- #
#  data loading
# --------------------------------------------------------------------------- #
def _load_npz(path):
    d = np.load(path)
    images = d["images"]
    n = len(images)
    labels = d["labels"].astype(np.float32)
    m_fill = d["m_fill"].astype(np.float32)
    m_green = d["m_green"].astype(np.float32)
    split = d["split"].astype(np.uint8)
    sigma = d["sigma"].astype(np.float32) if "sigma" in d else np.ones((n,), np.float32)
    X = np.stack([preprocess(im) for im in images]).astype(np.float32)  # [N,1,128,48]
    return X, labels, m_fill, m_green, sigma, split


def _concat(sources, want_split, real_weight):
    """Concatenate the requested split across sources. Real sources (weight>1) are repeated
    int(round(real_weight)) times on the TRAIN split only (val is never upsampled)."""
    Xs, Ls, Mf, Mg, Sg = [], [], [], [], []
    for (X, labels, m_fill, m_green, sigma, split), w in sources:
        m = split == want_split
        if not m.any():
            continue
        reps = max(1, int(round(w))) if want_split == 0 else 1
        for _ in range(reps):
            Xs.append(X[m]); Ls.append(labels[m]); Mf.append(m_fill[m])
            Mg.append(m_green[m]); Sg.append(sigma[m])
    if not Xs:
        z = np.zeros((0,), np.float32)
        return (np.zeros((0, 1, INPUT_H, INPUT_W), np.float32),
                np.zeros((0, 4), np.float32), z, z, z)
    return (np.concatenate(Xs), np.concatenate(Ls), np.concatenate(Mf),
            np.concatenate(Mg), np.concatenate(Sg))


# --------------------------------------------------------------------------- #
#  training
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", action="append", default=[], help="npz (weight 1); repeatable")
    ap.add_argument("--data-real", action="append", default=[],
                    help="npz upsampled by --real-weight on the train split; repeatable")
    ap.add_argument("--real-weight", type=float, default=2.0)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=256)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--device", default="auto", help="auto|cpu|cuda|0")
    ap.add_argument("--out", default=OUT_PT, help="output .pt path (.json sidecar derived from it)")
    args = ap.parse_args()

    if not args.data and not args.data_real:
        ap.error("provide at least one --data or --data-real npz")

    import torch
    from torch.utils.data import DataLoader, TensorDataset

    device = ("cuda" if torch.cuda.is_available() else "cpu") if args.device == "auto" else args.device

    sources = [(_load_npz(p), 1.0) for p in args.data]
    sources += [(_load_npz(p), args.real_weight) for p in args.data_real]
    trained_on = "+".join([os.path.basename(p) for p in args.data + args.data_real])

    Xtr, Ltr, Mftr, Mgtr, Sgtr = _concat(sources, 0, args.real_weight)
    Xva, Lva, Mfva, Mgva, Sgva = _concat(sources, 1, args.real_weight)
    print(f"train rows={len(Xtr)}  val rows={len(Xva)}  "
          f"(real x{args.real_weight} on {len(args.data_real)} src)")

    def _loader(X, L, Mf, Mg, Sg, shuffle):
        ds = TensorDataset(torch.from_numpy(X), torch.from_numpy(L), torch.from_numpy(Mf),
                           torch.from_numpy(Mg), torch.from_numpy(Sg))
        return DataLoader(ds, batch_size=args.batch, shuffle=shuffle)

    tr = _loader(Xtr, Ltr, Mftr, Mgtr, Sgtr, True)
    va = _loader(Xva, Lva, Mfva, Mgva, Sgva, False)

    net = _make_net(torch).to(device)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, args.epochs))

    out_pt = args.out
    out_json = os.path.splitext(out_pt)[0] + ".json"
    os.makedirs(os.path.dirname(os.path.abspath(out_pt)), exist_ok=True)

    best = float("inf")
    for ep in range(args.epochs):
        net.train(); tl = 0.0; nseen = 0
        for xb, lb, mfb, mgb, sgb in tr:
            xb, lb, mfb, mgb, sgb = (xb.to(device), lb.to(device), mfb.to(device),
                                     mgb.to(device), sgb.to(device))
            opt.zero_grad()
            reg, plogit, lvar = net(xb)
            loss, _ = meter_y_loss(reg, plogit, lvar, lb, mfb, mgb, sgb)
            loss.backward(); opt.step()
            tl += loss.item() * len(xb); nseen += len(xb)
        sched.step()
        tl /= max(1, nseen)

        # ---- validation ----
        net.eval()
        vfill_abs = 0.0; vfill_n = 0.0
        cover_hit = 0.0; cover_n = 0.0
        p_true, p_score = [], []
        with torch.no_grad():
            for xb, lb, mfb, mgb, sgb in va:
                xb, lb, mfb = xb.to(device), lb.to(device), mfb.to(device)
                reg, plogit, lvar = net(xb)
                err = torch.abs(reg[:, 0] - lb[:, 0])
                vfill_abs += (err * mfb).sum().item(); vfill_n += mfb.sum().item()
                sig = torch.exp(0.5 * lvar)
                cover_hit += ((err <= sig).float() * mfb).sum().item(); cover_n += mfb.sum().item()
                p_true.append(lb[:, 3].cpu().numpy())
                p_score.append(torch.sigmoid(plogit).cpu().numpy())
        vmae = (vfill_abs / max(1e-9, vfill_n)) * 100.0
        cover = (cover_hit / cover_n) if cover_n else float("nan")
        auc = _auc_rank(np.concatenate(p_true), np.concatenate(p_score)) if p_true else float("nan")
        print(f"epoch {ep+1}/{args.epochs}  train_loss={tl:.5f}  "
              f"val_fill_MAE={vmae:.3f}pp  present_AUC={auc:.3f}  +-1sigma_cover={cover:.3f}")

        if vmae <= best:
            best = vmae
            torch.save(net.state_dict(), out_pt)

    if not os.path.exists(out_pt):  # degenerate (no val fill rows) -> always persist final weights
        torch.save(net.state_dict(), out_pt)

    meta = {
        "arch": "meter_reader_y_cnn_v1",
        "input": "y_128x48",
        "outputs": ["fill_frac", "green_top_frac", "green_bottom_frac", "present", "log_var"],
        "best_val_fill_mae_pct": float(best),
        "trained_on": trained_on,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    print(f"saved {out_pt} (best_val_fill_MAE={best:.3f}pp) + {out_json}")


if __name__ == "__main__":
    main()
