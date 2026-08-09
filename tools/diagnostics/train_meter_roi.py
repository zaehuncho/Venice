#!/usr/bin/env python3
"""Train the multi-head ROI reader (model #2): given the meter ROI crop the classical locator hands it, predict
present + notch_row + fill_top_row + green_top/bottom_row + fill_pct in one cheap pass. This is the precision
lever for the CONTESTED-GREEN sliver the classical green-scan misses. Trained on synth_meter_gen.py output;
fine-tune later on real meter_autolabel crops for the sim-to-real gap.

USAGE:
  python tools/diagnostics/train_meter_roi.py --data datasets/meter_roi_synth --epochs 18 --out models/meter_roi
Outputs models/meter_roi/meter_roi.pt (+ best val metrics). Export to ONNX for the C++/sidecar runtime later.
"""
import argparse, json, os, random
import cv2
import numpy as np
import torch
import torch.nn as nn

H, W = 160, 64   # ROI crop size (the locator normalizes to this)


class DS(torch.utils.data.Dataset):
    def __init__(self, root, rows):
        self.root = root; self.rows = rows
    def __len__(self):
        return len(self.rows)
    def __getitem__(self, i):
        r = self.rows[i]
        img = cv2.imread(os.path.join(self.root, r["file"]))
        if img is None:
            img = np.zeros((H, W, 3), np.uint8)
        if img.shape[:2] != (H, W):
            img = cv2.resize(img, (W, H))
        x = torch.from_numpy(img[:, :, ::-1].copy()).permute(2, 0, 1).float() / 255.0   # BGR->RGB, CHW
        present = float(r["present"])
        rows = torch.tensor([max(0.0, r["notch_row"]), max(0.0, r["fill_top_row"]),
                             max(0.0, r["green_top_row"]), max(0.0, r["green_bottom_row"])], dtype=torch.float32)
        fill = torch.tensor([max(0.0, r["fill_pct"]) / 100.0], dtype=torch.float32)
        return x, torch.tensor([present]), rows, fill


class MeterROINet(nn.Module):
    """Small conv net; FLATTEN (not global-pool) so the vertical position needed for row regression survives."""
    def __init__(self):
        super().__init__()
        def blk(i, o):
            return nn.Sequential(nn.Conv2d(i, o, 3, 2, 1), nn.BatchNorm2d(o), nn.ReLU(inplace=True))
        self.feat = nn.Sequential(blk(3, 16), blk(16, 32), blk(32, 64), blk(64, 64))   # 160x64 -> 10x4
        fdim = 64 * (H // 16) * (W // 16)
        self.drop = nn.Dropout(0.2)
        self.present = nn.Linear(fdim, 1)
        self.rows = nn.Linear(fdim, 4)
        self.fill = nn.Linear(fdim, 1)
    def forward(self, x):
        f = self.drop(torch.flatten(self.feat(x), 1))
        return self.present(f), torch.sigmoid(self.rows(f)), torch.sigmoid(self.fill(f))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="datasets/meter_roi_synth")
    ap.add_argument("--epochs", type=int, default=18)
    ap.add_argument("--bs", type=int, default=128)
    ap.add_argument("--out", default="models/meter_roi")
    ap.add_argument("--val-frac", type=float, default=0.1)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(os.path.join(args.data, "labels.jsonl"), encoding="utf-8")]
    random.Random(0).shuffle(rows)
    nv = int(len(rows) * args.val_frac)
    val, tr = rows[:nv], rows[nv:]
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tl = torch.utils.data.DataLoader(DS(args.data, tr), batch_size=args.bs, shuffle=True, num_workers=0)
    vl = torch.utils.data.DataLoader(DS(args.data, val), batch_size=args.bs, num_workers=0)
    net = MeterROINet().to(dev)
    opt = torch.optim.Adam(net.parameters(), 2e-3)
    sch = torch.optim.lr_scheduler.CosineAnnealingLR(opt, args.epochs)
    bce = nn.BCEWithLogitsLoss(); l1 = nn.L1Loss(reduction="none")

    best = 1e9
    os.makedirs(args.out, exist_ok=True)
    for ep in range(args.epochs):
        net.train()
        for x, p, r, fi in tl:
            x, p, r, fi = x.to(dev), p.to(dev), r.to(dev), fi.to(dev)
            pl, rp, fp = net(x)
            loss = bce(pl, p) + (p * l1(rp, r).mean(1, keepdim=True)).mean() * 4 + (p * l1(fp, fi)).mean() * 4
            opt.zero_grad(); loss.backward(); opt.step()
        sch.step()
        # --- validate ---
        net.eval(); pacc = 0; nrow = 0; row_mae = np.zeros(4); fill_mae = 0.0; npos = 0
        with torch.no_grad():
            for x, p, r, fi in vl:
                x = x.to(dev); pl, rp, fp = net(x)
                pp = (torch.sigmoid(pl).cpu() > 0.5).float()
                pacc += (pp == p).sum().item(); nrow += len(p)
                m = (p.squeeze(1) > 0.5)
                if m.any():
                    row_mae += (l1(rp.cpu()[m], r[m]).mean(0).numpy()) * len(r[m]); npos += m.sum().item()
                    fill_mae += l1(fp.cpu()[m], fi[m]).sum().item()
        rm = row_mae / max(1, npos); fm = fill_mae / max(1, npos)
        # report row MAE in PIXELS (xH) and fill MAE in PERCENT (x100)
        score = rm.mean() * H + fm * 100
        print(f"ep{ep:02d} present_acc={pacc/nrow:.3f}  rowMAE_px[notch,fill,gT,gB]="
              f"[{rm[0]*H:.1f},{rm[1]*H:.1f},{rm[2]*H:.1f},{rm[3]*H:.1f}]  fillMAE={fm*100:.1f}%")
        if score < best:
            best = score
            torch.save(net.state_dict(), os.path.join(args.out, "meter_roi.pt"))
    print(f"best score={best:.2f}  saved {args.out}/meter_roi.pt")


if __name__ == "__main__":
    main()
