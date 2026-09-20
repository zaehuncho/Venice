#!/usr/bin/env python
"""Fine-tune the shipped 2K27 meter detector on the LOW-FILL hard set (2026-09-03).

Recipe: best.pt-as-init (NOT resume=True), the project's established fine-tune pattern
(finetune_meter_pill.py / pose_finetune_efficient.py), starting from the SHIPPED
meter2k27_n3_pill/weights/best.pt, on the original combo data (Arrow2 + Pill, so nothing
is forgotten) PLUS the mined low-fill hard set (tools/training/mine_lowfill_hardset.py),
with the same gentle-AdamW/no-pose-augment schedule at an even lower lr0 (0.0002): this
is a small corrective fine-tune of a converged model, not a fresh train.

The shipped model dir (meter2k27_n3_pill) is never written to: output goes to a NEW run
directory meter2k27_n4_lowfill, and the ONNX is exported with the SAME settings as the
shipped one (imgsz 960, FP16 via quantize=16, simplify, static batch 1) so it is a
drop-in for meter_detector_yolo.MeterYoloLocator, which takes imgsz from the model.
"""
from __future__ import annotations
import os, sys, argparse, shutil, tempfile
from pathlib import Path

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROJECT = os.path.join(REPO, "runs", "detect", "logs", "diagnostics", "meter_train")


def export_best(best, sizes):
    """Stage every resolution before publishing; preserve exports on failure.

    Ultralytics names an export after the input checkpoint, regardless of imgsz.
    Exporting the same best.pt twice in-place overwrites best.onnx before the
    second output is renamed. Isolated checkpoint copies keep those temporary
    filenames away from existing exports and from one another.
    """
    from ultralytics import YOLO
    best = Path(best).resolve()
    sizes = list(dict.fromkeys(int(sz) for sz in sizes))
    if not sizes or any(sz <= 0 for sz in sizes):
        raise ValueError("export sizes must be positive")
    root = Path(tempfile.mkdtemp(prefix=".lowfill-export-", dir=best.parent)).resolve()
    if root.parent != best.parent or not root.name.startswith(".lowfill-export-"):
        raise RuntimeError(f"unexpected export staging directory: {root}")
    keep_recovery = False
    try:
        pending = []
        for sz in sizes:
            stage = root / str(sz)
            stage.mkdir()
            checkpoint = stage / best.name
            shutil.copy2(best, checkpoint)
            path = Path(YOLO(str(checkpoint)).export(
                format="onnx", imgsz=sz, quantize=16, simplify=True,
                dynamic=False, batch=1))
            if not path.is_file():
                raise FileNotFoundError(f"export did not produce a file: {path}")
            dst = best.parent / ("best.onnx" if sz == 960 else f"best_{sz}.onnx")
            backup = root / (dst.name + ".previous")
            if dst.exists():
                shutil.copy2(dst, backup)
            pending.append((path, dst, backup))
        # All model exports have succeeded. Roll back earlier promotions if a
        # subsequent filesystem replacement fails (for example a locked file).
        published = []
        try:
            for path, dst, backup in pending:
                os.replace(path, dst)
                published.append((dst, backup))
        except Exception as publish_error:
            rollback_errors = []
            for dst, backup in reversed(published):
                try:
                    if backup.exists():
                        os.replace(backup, dst)
                    else:
                        dst.unlink()
                except Exception as rollback_error:
                    rollback_errors.append(f"{dst}: {rollback_error!r}")
            if rollback_errors:
                # Retain the original-byte backups if even their restoration
                # fails. Never let automatic temporary-directory cleanup erase
                # the only recovery copy. This is exception recovery, not a
                # cross-file atomic transaction or process-crash guarantee.
                keep_recovery = True
                raise RuntimeError(
                    f"export publication failed; rollback incomplete; "
                    f"retained recovery directory: {root}; "
                    + "; ".join(rollback_errors)) from publish_error
            raise
        return [str(dst) for _, dst, _ in pending]
    finally:
        if not keep_recovery:
            shutil.rmtree(root)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(REPO, "datasets", "meter2k27_combo_lowfill.yaml"))
    ap.add_argument("--base", default=os.path.join(PROJECT, "meter2k27_n3_pill", "weights", "best.pt"))
    ap.add_argument("--epochs", type=int, default=30)
    ap.add_argument("--imgsz", type=int, default=1280)   # matches n2/n3 training
    ap.add_argument("--batch", type=int, default=8)      # live app shares the GPU -> keep moderate
    ap.add_argument("--lr0", type=float, default=0.0002)
    ap.add_argument("--name", default="meter2k27_n4_lowfill")
    ap.add_argument("--export-imgsz", type=int, nargs="+", default=[960, 1280])
    ap.add_argument("--skip-train", action="store_true", help="only (re-)export best.pt")
    a = ap.parse_args()
    assert "n3_pill" not in a.name, "never write into the shipped run dir"
    from ultralytics import YOLO
    out = os.path.join(PROJECT, a.name)
    if not a.skip_train:
        m = YOLO(a.base)
        m.train(
            data=a.data, epochs=a.epochs, imgsz=a.imgsz, batch=a.batch,
            project=PROJECT, name=a.name, exist_ok=True,
            patience=12, cache=False, workers=4, seed=1234,
            optimizer="AdamW", lr0=a.lr0, lrf=0.05, warmup_epochs=0.5, cos_lr=True,
            # HUD element: always upright, never mirrored -> no pose augments.
            degrees=0.0, shear=0.0, perspective=0.0, flipud=0.0, fliplr=0.0,
            mosaic=0.3, scale=0.4, translate=0.1,
            # arena lighting / court colour vary hugely between venues.
            hsv_h=0.015, hsv_s=0.5, hsv_v=0.4,
        )
    best = os.path.join(out, "weights", "best.pt")
    assert os.path.exists(best), best
    # Keep 960/1280 exports distinct and do not touch existing artifacts until
    # every requested export has completed successfully.
    for dst in export_best(best, a.export_imgsz):
        print("exported:", dst, flush=True)
    print("best:", best)


if __name__ == "__main__":
    sys.exit(main())
