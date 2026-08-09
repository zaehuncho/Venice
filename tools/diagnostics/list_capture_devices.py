#!/usr/bin/env python3
"""Find the right capture_card_index for Orion's Capture-card (HDMI) mode.

cv2.VideoCapture addresses devices by a bare INTEGER index, and that index is NOT stable across machines
(a webcam / OBS-virtual-cam / the HDMI card can land at 0,1,2...). Selecting the wrong index silently grabs
the wrong device (or a black HDCP feed). This probes indices 0..N via the SAME APIs the backend uses
(DSHOW first, then MSMF — see capture_card_backend.py) and reports, for each that opens: resolution, FPS,
and mean brightness (a black/HDCP-blocked feed reads ~0). Pick the index that shows your PS5 feed
(typically 1920x1080 @ ~60 and clearly non-black), then set it in Orion → Stream Setup → Detection video →
Device #.

USAGE:  C:\\Python314\\python.exe tools/diagnostics/list_capture_devices.py [--max 10] [--save]
        --save writes a small JPEG per opened device to the scratchpad so you can eyeball which is the PS5.
"""
import argparse
import os
import sys


def _pnp_names():
    """Best-effort: list Windows imaging/camera device friendly names for cross-reference (order does NOT
    necessarily match the cv2 index, but the names help identify what's plugged in)."""
    if os.name != "nt":
        return []
    try:
        import subprocess
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_PnPEntity | Where-Object { $_.PNPClass -in @('Camera','Image','Media') } "
             "| Select-Object -ExpandProperty Name"],
            capture_output=True, text=True, timeout=15)
        return [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=10, help="highest device index to probe")
    ap.add_argument("--save", action="store_true", help="save a JPEG per opened device for eyeballing")
    args = ap.parse_args()

    try:
        import cv2
    except Exception as exc:
        print(f"opencv not importable ({exc}); run with C:\\Python314\\python.exe")
        return 1
    import numpy as np

    save_dir = None
    if args.save:
        save_dir = os.path.join(os.environ.get("TEMP", "."), "orion_capdev")
        os.makedirs(save_dir, exist_ok=True)

    names = _pnp_names()
    if names:
        print("Windows imaging/camera devices present (names only, not index-ordered):")
        for n in names:
            print(f"    - {n}")
        print()

    apis = [(cv2.CAP_DSHOW, "DSHOW"), (cv2.CAP_MSMF, "MSMF")] if os.name == "nt" else [(cv2.CAP_ANY, "ANY")]
    print(f"Probing indices 0..{args.max - 1} (DSHOW preferred, MSMF fallback):\n")
    found = []
    for idx in range(args.max):
        opened = None
        for api, apiname in apis:
            cap = None
            try:
                cap = cv2.VideoCapture(idx, api)
                if not cap or not cap.isOpened():
                    if cap is not None:
                        cap.release()
                    continue
                # match the backend's MJPG 1080p60 request so the probe reflects real capability
                try:
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
                except Exception:
                    pass
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
                cap.set(cv2.CAP_PROP_FPS, 60)
                ok, frame = cap.read()
                if not ok or frame is None or getattr(frame, "size", 0) == 0:
                    cap.release()
                    continue
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                fps = cap.get(cv2.CAP_PROP_FPS)
                bright = float(np.mean(frame))
                opened = (apiname, w, h, fps, bright)
                if save_dir is not None:
                    cv2.imwrite(os.path.join(save_dir, f"dev{idx}_{apiname}.jpg"), frame)
                cap.release()
                break
            except Exception:
                if cap is not None:
                    try:
                        cap.release()
                    except Exception:
                        pass
        if opened:
            apiname, w, h, fps, bright = opened
            tag = "  <-- likely PS5 HDMI card" if (w >= 1280 and h >= 720 and bright > 12) else ""
            black = "  (BLACK/HDCP? brightness~0)" if bright <= 8 else ""
            print(f"  index {idx}: OPEN via {apiname}  {w}x{h} @ {fps:.0f}fps  brightness={bright:.0f}{tag}{black}")
            found.append((idx, w, h, bright))

    print()
    if not found:
        print("No capture devices opened. Check the card is plugged in, HDCP is stripped, and no other app "
              "(OBS/Chrome) holds it. Then set that index in Orion.")
    else:
        cand = [f for f in found if f[1] >= 1280 and f[2] >= 720 and f[3] > 12]
        best = (cand or found)[0][0]
        print(f"Recommended capture_card_index = {best}")
        print("Set it in Orion: Stream Setup -> Detection video -> Capture card (HDMI) -> Device # =", best)
        if save_dir is not None:
            print(f"Saved preview JPEGs to {save_dir} (open them to confirm which shows your PS5).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
