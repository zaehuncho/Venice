"""What a RELAXED gate-4 width floor would cost on NON-Pill footage.

READ-ONLY.  Walks an arbitrary frame directory (a framedump session, a press-window
dump -- any *.png / *.jpg, sorted) through the shipped CV proposer twice: once with the
shipped constants and once with the relaxation under test, and reports how many frames
each arm proposed a box on.

On an Arrow2 session this is a FALSE-LOCK census: every extra proposal the relaxed arm
takes on a frame the shipped arm refused is a box the shipped gates were refusing on
purpose (the owner's white jersey, the scoreboard, a court line).  It is the evidence
for keying any relaxation on `meter_style=pill` rather than shipping it globally.

Usage:
  python tools/diagnostics/pill_falselock_probe.py --dir D:\\NexusVision\\framedump\\session_20260912_201355 \\
      --stride 7 --limit 800 --relax ORION_CV_COL_W_MIN=3
"""
from __future__ import annotations
import argparse, collections, glob, os, subprocess, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))


def run_arm(frames, relax):
    """Child process: a fresh interpreter so the locator reads the relaxed env at import."""
    code = (
        "import os,sys,json;sys.path.insert(0,r'%s');"
        "os.environ['ORION_METER_PROPOSER']='cv';"
        "import cv2;import meter_locator_cv as m;"
        "loc=m.MeterContourLocator();"
        "hits=[];\n"
        "for i,p in enumerate(json.load(sys.stdin)):\n"
        "    im=cv2.imread(p)\n"
        "    if im is None: continue\n"
        "    b=loc.detect_box(im, i/60.0)\n"
        "    hits.append(1 if b else 0)\n"
        "print(json.dumps({'n':len(hits),'hits':sum(hits),"
        "'stats':{k:v for k,v in loc.stats.items() if v}}))" % REPO
    )
    env = dict(os.environ)
    env.update(relax)
    import json
    py = os.path.join(REPO, ".venv", "Scripts", "python.exe")
    if not os.path.exists(py):
        py = sys.executable
    out = subprocess.run([py, "-c", code], input=json.dumps(frames), env=env,
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stderr[-2000:])
        return None
    return json.loads(out.stdout.strip().splitlines()[-1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--stride", type=int, default=7)
    ap.add_argument("--limit", type=int, default=800)
    ap.add_argument("--relax", default="ORION_CV_COL_W_MIN=3")
    a = ap.parse_args()
    frames = sorted(glob.glob(os.path.join(a.dir, "*.png"))
                    + glob.glob(os.path.join(a.dir, "*.jpg")))[:: a.stride][: a.limit]
    if not frames:
        print("no frames under", a.dir)
        return 2
    relax = {}
    for kv in a.relax.split(","):
        if "=" in kv:
            k, v = kv.split("=", 1)
            relax[k.strip()] = v.strip()
    print("%d frames from %s" % (len(frames), a.dir))
    base = run_arm(frames, {})
    rel = run_arm(frames, relax)
    for name, res in (("SHIPPED", base), ("RELAXED %s" % relax, rel)):
        if res is None:
            print(name, "-> failed")
            continue
        print("%-34s proposals %4d / %4d (%.1f%%)"
              % (name, res["hits"], res["n"], 100.0 * res["hits"] / max(1, res["n"])))
        print("    stats:", res["stats"])
    if base and rel:
        print("DELTA proposals: %+d" % (rel["hits"] - base["hits"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
