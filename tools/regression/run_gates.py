#!/usr/bin/env python
"""
run_gates.py -- OFFLINE detection + timing regression gate (human report).

Replays the recorded live framedumps through the PRODUCTION simple_meter_reader.SimpleMeterReader
and prints PASS/FAIL per gate with the MEASURED value vs the committed FLOOR. This is the guard that
locks in the detection quality achieved on HEAD so a future change that silently regresses it FAILS.

The native TIMING gates live in the QtTest suite OrionNativeTests (native_orion/tests/
AutomationEngineTests.cpp); this runner prints how to run them and (best-effort) their totals if the
built exe is present. See docs/REGRESSION_GATES.md for the full gate catalogue + thresholds.

Usage:
  C:/Python314/python.exe tools/regression/run_gates.py
  C:/Python314/python.exe tools/regression/run_gates.py --sessions session_20260704_210801
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import replay_gates as RG  # noqa: E402

_GREEN = "\033[32m"
_RED = "\033[31m"
_DIM = "\033[2m"
_RST = "\033[0m"


def _c(txt, col):
    return col + txt + _RST if sys.stdout.isatty() else txt


def _fmt(v):
    return "%.3f" % v if isinstance(v, float) else str(v)


def _cmake_cache_value(build_dir, key):
    """Read one typed CMakeCache value without assuming a user-specific SDK path."""
    cache = Path(build_dir) / "CMakeCache.txt"
    if not cache.exists():
        return ""
    prefix = key + ":"
    for line in cache.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(prefix) and "=" in line:
            return line.split("=", 1)[1].strip()
    return ""


def run_detection(sessions):
    floors = RG.load_floors()
    total = passed = skipped = 0
    any_fail = False
    for s in sessions:
        t0 = time.perf_counter()
        m, gates = RG.evaluate_session(s, floors=floors)
        dt = time.perf_counter() - t0
        if not gates:
            print(_c("SKIP", _DIM) + "  %s  (framedump absent)" % s)
            skipped += 1
            continue
        win = m.get("window", [0, 0])
        print("\n=== DETECTION: %s  (shot-window %d-%d of %d, %d frames, %d shots, %.0fs) ===" % (
            s, win[0], win[1], m.get("frames_total", 0), m.get("frames", 0), m.get("n_shots", 0), dt))
        for g in gates:
            total += 1
            if g.passed:
                passed += 1
            else:
                any_fail = True
            tag = _c("PASS", _GREEN) if g.passed else _c("FAIL", _RED)
            floor = "-" if g.floor is None else "%s %s" % (g.op, _fmt(g.floor))
            line = "  %s  %-28s %10s   floor %-14s" % (tag, g.name, _fmt(g.measured), floor)
            if g.detail:
                line += _c("  # " + g.detail, _DIM)
            print(line)
    print("\nDETECTION GATES: %d/%d passed, %d session(s) skipped" % (passed, total, skipped))
    return any_fail, (total > 0)


def report_timing():
    exe = os.path.join(RG.REPO, "native_orion", "build", "Release", "OrionNativeTests.exe")
    print("\n=== TIMING GATES (native QtTest: OrionNativeTests) ===")
    invariants = [
        ("fade -> per-type clock (not 566/250 global)", "fadeUsesPerTypeClockWhileStandstillUsesGlobal"),
        ("standstill -> global clock", "fadeUsesPerTypeClockWhileStandstillUsesGlobal / autonomousVisionFiresOnGlobalClock"),
        ("Go-To meter-anchored + ~1600ms blind floor", "gotoUsesMeterAnchoredClockNotGlobalHoldClock"),
        ("Go-To no-meter blind fire at the cap", "gotoNoMeterBlindFires / gotoStalePhantomBlindFiresAtNoMeterCap"),
        ("fade safety-release at make-window floor", "fadeSafetyReleasesAtMakeWindowFloor"),
        ("fade targets green-window ENTRY", "fadeTargetsGreenWindowEntryNotCenter"),
        ("grader/artifact freeze on railed streak", "clockRunawayGuardFreezesOnRailedStreak / postReleaseMovingMeterIsNotGraded"),
        ("grader dials GLOBAL latency, not per-type", "autonomousGraderDialsGlobalLatencyNotPerType"),
        ("overlay frame-id ring join", "meterBoxRingJoinsBboxToItsOwnFrame"),
        ("blind-fire suppression (abort not blind release)", "noMeterAbortsInsteadOfBlindRelease / gotoLowFillSuppressesMaxHoldDump"),
    ]
    for desc, test in invariants:
        print("  %-46s -> %s" % (desc, test))
    if os.path.exists(exe):
        env = dict(os.environ)
        build_dir = os.path.dirname(os.path.dirname(exe))
        dll_dirs = [os.path.dirname(exe)]
        qt_dir = _cmake_cache_value(build_dir, "Qt6_DIR")
        if qt_dir:
            dll_dirs.append(str(Path(qt_dir).resolve().parents[2] / "bin"))
        opencv_dir = _cmake_cache_value(build_dir, "OpenCV_DIR")
        if opencv_dir:
            dll_dirs.append(str(Path(opencv_dir).resolve() / "x64" / "vc16" / "bin"))
        dll_dirs = [p for p in dll_dirs if os.path.isdir(p)]
        env["PATH"] = os.pathsep.join(dll_dirs + [env.get("PATH", "")])
        try:
            # QtTest routes its log to OutputDebugString when no console is attached (a captured pipe
            # sees nothing), so force plain-text output to a file with `-o file,txt` and read it back.
            import tempfile
            fd, tf = tempfile.mkstemp(prefix="orion_native_gate_", suffix=".txt")
            os.close(fd)
            os.unlink(tf)  # QtTest creates its own output file; never reuse stale gate output.
            proc = subprocess.run([exe, "-o", tf + ",txt"], capture_output=True, text=True, env=env,
                                  timeout=300, cwd=os.path.dirname(exe))
            out = open(tf, encoding="utf-8", errors="replace").read() if os.path.exists(tf) else ""
            tot = [ln for ln in out.splitlines() if ln.startswith("Totals:")]
            fails = [ln for ln in out.splitlines() if ln.startswith("FAIL")]
            print("\n  built exe: %s" % exe)
            for ln in fails:
                print("  " + _c(ln, _RED))
            gate_failed = proc.returncode != 0 or bool(fails) or not tot
            print("  " + (_c(tot[0], _GREEN if not gate_failed else _RED)
                           if tot else _c("FAIL: no QtTest totals line", _RED)))
            if proc.returncode != 0:
                print("  " + _c("FAIL: OrionNativeTests exited %d" % proc.returncode, _RED))
            return gate_failed
        except Exception as e:  # pragma: no cover
            print("\n  (could not run built exe: %s)" % e)
            return True
        finally:
            if "tf" in locals() and os.path.exists(tf):
                os.unlink(tf)
    else:
        print("\n  built exe not found -> build + run with:")
        print("    cmake --build native_orion/build --target OrionNativeTests --config Release")
        print("    (add the Qt bin from native_orion/build/CMakeCache.txt to PATH, then run "
              "native_orion/build/Release/OrionNativeTests.exe)")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", nargs="*", default=RG.SESSIONS)
    ap.add_argument("--no-timing", action="store_true", help="skip the native timing report")
    args = ap.parse_args()

    det_fail, det_ran = run_detection(args.sessions)
    tim_fail = False if args.no_timing else report_timing()

    print("\n" + "=" * 64)
    if not det_ran:
        print(_c("FAIL: no detection session was available; a skipped replay is not a pass", _RED))
    ok = det_ran and not det_fail and not tim_fail
    print(_c("ALL GATES PASS" if ok else "GATES FAILED", _GREEN if ok else _RED))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
