#!/usr/bin/env python3
"""Run eval_skele_pipeline.py across all 6 clips with multiple env-gate combos.

Outputs a summary table comparing baseline vs velocity-peak vs gather+vel-peak.

Usage:
    C:\\Python314\\python.exe tools\\diagnostics\\ab_eval_all.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import json

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CLIPS = [
    ("NBA 2K26_20260324195410.mp4", 3000, 3760, "Right", "Purple"),
    ("NBA 2K26_20260618064057.mp4", 2500, 1000, "Right", "Purple"),
    ("NBA 2K26_20260624212008.mp4", 3000, 150, "Right", "Red"),
    ("NBA 2K26_20260624212347.mp4", 3000, 2880, "Right", "Red"),
    ("NBA 2K26_20260624212700.mp4", 3000, 6240, "Right", "Red"),
    ("NBA 2K26_20260624213055.mp4", 3000, 7860, "Right", "Red"),
]

COMBOS = [
    ("baseline", {}),
    ("vel_peak", {"ORION_POSE_VEL_PEAK_PUSH": "1"}),
    ("gather+vel", {"ORION_POSE_REQUIRE_GATHER": "1", "ORION_POSE_VEL_PEAK_PUSH": "1"}),
]

PY = r"C:\Python314\python.exe"
EVAL = os.path.join(ROOT, "tools", "diagnostics", "eval_skele_pipeline.py")
VIDEOS = r"C:\Users\Administrator\Videos"


def run_eval(clip, count, start, handed, meter_color, env_extra):
    env = os.environ.copy()
    env["METER_COLOR"] = meter_color
    env.update(env_extra)
    video = os.path.join(VIDEOS, clip)
    cmd = [PY, EVAL, video, str(count), str(start), handed]
    r = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=600, cwd=ROOT)
    out = r.stdout + r.stderr
    # Parse key metrics
    lines = out.strip().split("\n")
    result = {"raw": out}
    for line in lines:
        if "player-lock ON SHOT FRAMES" in line:
            parts = line.split(":")[-1].strip()
            result["lock_shot"] = parts
        elif "real shots" in line:
            result["shots_line"] = line.strip()
        elif "push->meter offset" in line:
            result["push_std_line"] = line.strip()
        elif "release->meter offset" in line:
            result["rel_std_line"] = line.strip()
        elif "raw landmarks" in line:
            result["raw_line"] = line.strip()
    return result


def main():
    results = {}
    for combo_name, env_extra in COMBOS:
        print(f"\n{'='*60}")
        print(f"  COMBO: {combo_name}  (env: {env_extra})")
        print(f"{'='*60}")
        results[combo_name] = []
        for clip, count, start, handed, mcolor in CLIPS:
            tag = clip[:20]
            print(f"\n  [{tag}] {combo_name} ...", end=" ", flush=True)
            try:
                r = run_eval(clip, count, start, handed, mcolor, env_extra)
                results[combo_name].append(r)
                sl = r.get("shots_line", "?")
                pl = r.get("push_std_line", "?")
                ll = r.get("lock_shot", "?")
                print(f"lock={ll} | {sl}")
                print(f"    {pl}")
            except Exception as e:
                print(f"ERROR: {e}")
                results[combo_name].append({"error": str(e)})

    # Summary table
    print(f"\n\n{'='*80}")
    print("  SUMMARY TABLE")
    print(f"{'='*80}")
    print(f"{'Clip':<22} {'Combo':<14} {'Lock%':<8} {'Shots':<6} {'Push%':<8} {'Rel%':<8} {'Pair%':<8} {'PushSTD':<10} {'RelSTD':<10}")
    print("-" * 80)
    for combo_name, _ in COMBOS:
        for i, r in enumerate(results[combo_name]):
            clip = CLIPS[i][0][:20]
            if "error" in r:
                print(f"{clip:<22} {combo_name:<14} ERROR")
                continue
            sl = r.get("shots_line", "")
            ll = r.get("lock_shot", "")
            pl = r.get("push_std_line", "")
            rl = r.get("rel_std_line", "")
            # Parse
            import re
            lock_m = re.search(r'(\d+/\d+).*?(\d+)%', ll)
            lock_str = f"{lock_m.group(2)}%" if lock_m else "?"
            shots_m = re.search(r'(\d+)\s*\|.*?(\d+)/(\d+).*?(\d+)%.*?(\d+)/(\d+).*?(\d+)%.*?(\d+)/(\d+).*?(\d+)%', sl)
            if shots_m:
                n_shots = shots_m.group(1)
                push_pct = f"{shots_m.group(4)}%"
                rel_pct = f"{shots_m.group(7)}%"
                pair_pct = f"{shots_m.group(10)}%"
            else:
                n_shots = push_pct = rel_pct = pair_pct = "?"
            push_std_m = re.search(r'STD\s+(\d+)ms', pl)
            push_std = f"{push_std_m.group(1)}ms" if push_std_m else "N/A"
            rel_std_m = re.search(r'STD\s+(\d+)ms', rl)
            rel_std = f"{rel_std_m.group(1)}ms" if rel_std_m else "N/A"
            print(f"{clip:<22} {combo_name:<14} {lock_str:<8} {n_shots:<6} {push_pct:<8} {rel_pct:<8} {pair_pct:<8} {push_std:<10} {rel_std:<10}")

    # Save full results
    out_path = os.path.join(ROOT, "logs", "diagnostics", "ab_eval_results.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nFull results saved to {out_path}")


if __name__ == "__main__":
    main()
