"""Compare the three window-capture tiers against a live Chiaki window.

Non-gameplay diagnostic for the live `roi_not_found` dropouts. The orchestrator's
`_capture_window` accepts the first non-(whole-frame-)black tier
(GDI BitBlt -> PrintWindow PW_RENDERFULLCONTENT -> desktop screen-region). The
12:53 batch showed the detector failing live (roi_not_found) while an OBS recording
of the SAME screen detects the meter, with `uniqfps` still high — i.e. the captured
frame content differs from the true screen. This probe grabs the Chiaki window via
EACH tier at the same instant and saves them side by side so we can SEE which tier is
black / stale / video-less and which carries the real (composited) video — without a
gameplay batch.

Run while Orion is streaming (idle is fine; trigger a shot or two if you want a meter
in frame):

    python tools/diagnostics/capture_tier_probe.py --frames 40 --interval 0.25

Output: logs/diagnostics/tierprobe/tier_<NNN>.png (gdi | printwindow | screen_region
side by side, labelled with black/size/core status) + a printed per-tier summary.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from remote_play_orchestrator import RemotePlayOrchestrator as O  # noqa: E402

TIERS = ("gdi", "printwindow", "screen_region")


def _find_window(title_substr: str):
    """Return the HWND of the Chiaki stream window (reuses the client finder when
    available, else enumerates by title)."""
    try:
        from remote_play_client import find_remote_play_window
        hwnd, name = find_remote_play_window(title_substr)
        if hwnd:
            print(f"window: {name} (hwnd={hwnd})")
            return hwnd
    except Exception:
        pass
    try:
        import win32gui
        found = []

        def cb(h, _):
            if not win32gui.IsWindowVisible(h):
                return True
            t = win32gui.GetWindowText(h) or ""
            tl = t.lower()
            if "chiaki" in tl and "orion" not in tl:
                found.append((h, t))
            return True

        win32gui.EnumWindows(cb, None)
        if found:
            print(f"window: {found[0][1]} (hwnd={found[0][0]})")
            return found[0][0]
    except Exception as exc:
        print(f"window enumeration failed: {exc}", file=sys.stderr)
    return None


def _grab(tier: str, hwnd):
    fn = {
        "gdi": O._capture_window_gdi,
        "printwindow": O._capture_window_printwindow,
        "screen_region": O._capture_window_screen_region,
    }[tier]
    try:
        return fn(hwnd)
    except Exception:
        return None


def _label(cv2, img, text):
    import numpy as np
    if img is None:
        panel = np.zeros((360, 480, 3), dtype=np.uint8)
    else:
        h, w = img.shape[:2]
        scale = 480.0 / max(1, w)
        panel = cv2.resize(img, (480, max(1, int(h * scale))))
    cv2.rectangle(panel, (0, 0), (panel.shape[1] - 1, 18), (0, 0, 0), -1)
    cv2.putText(panel, text, (3, 14), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1, cv2.LINE_AA)
    return panel


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--title", default="chiaki", help="window title substring to find")
    ap.add_argument("--hwnd", type=int, default=0, help="explicit HWND (skips the finder)")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--out", default=str(ROOT / "logs" / "diagnostics" / "tierprobe"))
    args = ap.parse_args()

    try:
        import cv2
        import numpy as np
    except Exception as exc:
        print(f"opencv/numpy import failed: {exc}", file=sys.stderr)
        return 3

    hwnd = args.hwnd or _find_window(args.title)
    if not hwnd:
        print("no Chiaki window found — is Orion streaming and un-minimized?", file=sys.stderr)
        return 4

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stats = {t: {"none": 0, "black": 0, "core_black": 0, "ok": 0} for t in TIERS}
    last_hash = {t: None for t in TIERS}
    static_changes = {t: 0 for t in TIERS}

    for i in range(args.frames):
        grabs = {t: _grab(t, hwnd) for t in TIERS}
        panels = []
        for t in TIERS:
            img = grabs[t]
            s = stats[t]
            if img is None:
                s["none"] += 1
                panels.append(_label(cv2, None, f"{t}: NONE"))
                continue
            whole_black = O._frame_is_probably_black(img)
            core_black, core_hash = O._video_region_status(img)
            if core_hash is not None and core_hash != last_hash[t]:
                static_changes[t] += 1
            last_hash[t] = core_hash
            if whole_black:
                s["black"] += 1
            elif core_black:
                s["core_black"] += 1
            else:
                s["ok"] += 1
            h, w = img.shape[:2]
            tag = "BLACK" if whole_black else ("CORE_BLACK" if core_black else "ok")
            panels.append(_label(cv2, img, f"{t}: {w}x{h} {tag}"))
        # Pad panels to equal height, stack horizontally, save.
        maxh = max(p.shape[0] for p in panels)
        padded = [cv2.copyMakeBorder(p, 0, maxh - p.shape[0], 0, 0, cv2.BORDER_CONSTANT, value=(0, 0, 0)) for p in panels]
        combo = np.hstack(padded)
        cv2.imwrite(str(out_dir / f"tier_{i:03d}.png"), combo)
        time.sleep(max(0.0, args.interval))

    print(f"\n=== tier summary over {args.frames} grabs (hwnd={hwnd}) ===")
    for t in TIERS:
        s = stats[t]
        print(f"  {t:<13} ok={s['ok']:<3} core_black={s['core_black']:<3} "
              f"whole_black={s['black']:<3} none={s['none']:<3} "
              f"distinct_core_frames={static_changes[t]}")
    print(f"\nside-by-side frames -> {out_dir}")
    print("Read a few tier_*.png: if gdi/printwindow show BLACK/CORE_BLACK or a STALE\n"
          "video while screen_region shows the live game, the fix is to prefer/validate\n"
          "the screen_region (composited) tier when the window is on-screen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
