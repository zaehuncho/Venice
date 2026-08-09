#!/usr/bin/env python3
"""Bench harness for the inbound meter delay — proves the anti-stutter property.

WHY THIS EXISTS
===============
The inbound meter delay was deleted on 2026-08-04 (968b127f) because "holding
packets through WinDivert stutters the game and cost a batch of tip timing".
The stutter was never measured — it was observed, attributed and the feature was
pulled. This harness makes the property measurable OFFLINE, with no driver, no
elevation, no console and no game, so a change can be judged before it ever
touches a live session.

THE PHYSICS, IN ONE LINE
========================
A packet arriving at t is released at t + D(t). Two packets dt apart therefore
leave dt*(1 + D') apart, so the console receives the server's update stream at

        rate multiplier = 1 / (1 + D')

A CONSTANT delay is completely benign: D' = 0, multiplier exactly 1.0, the
stream is bit-identical and merely time-shifted. Every bit of the damage lives
in the TRANSITIONS. The deleted controller ramped 55ms per 50ms tick, i.e.
D' = +1.1, i.e. the console saw 48% of the normal update rate for ~150ms
immediately before the meter rendered — and then a 167% catch-up burst on the
way back down, once per shot.

WHAT THIS MEASURES
==================
Offline mode drives the REAL `_InboundDelayBuffer` from nexus_svc against a
virtual clock and a synthetic packet source at a chosen cadence, then computes
the observed rate multiplier from the release timestamps. Virtual time is what
makes the result exact rather than an estimate fighting ~1ms of Windows
scheduler jitter, which at a 15.6ms packet cadence is 6% — the same order as
the effect being measured.

USAGE
=====
    python tools/meter_delay_bench.py                    # offline, shipping cap
    python tools/meter_delay_bench.py --compare          # vs the deleted config
    python tools/meter_delay_bench.py --slew 250         # try another cap
    python tools/meter_delay_bench.py --live             # against a running bridge

Exit code is 0 when every profile passes its bar, 1 otherwise, so this is usable
as a gate.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nexus_svc  # noqa: E402


CONSOLE_IP = "192.168.137.100"
COURT_IP = "104.255.104.55"

# The bar. 0.90 == the console never drops below 90% of its normal server-update
# rate, which is the same order as ordinary network jitter.
RATE_FLOOR = 0.90
RATE_CEILING = 1.12


class _Pkt:
    __slots__ = ("seq",)

    def __init__(self, seq: int) -> None:
        self.seq = seq


class _RecordingHandle:
    """Captures (seq, virtual release time) for every re-injection."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self.released: list[tuple[int, float]] = []

    def send(self, pkt) -> None:
        self.released.append((pkt.seq, self._clock()))

    def recv(self):          # never used: the bench drives enqueue() directly
        return None

    def close(self) -> None:
        pass


class _VirtualClock:
    def __init__(self) -> None:
        self.t = 1_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# Packets are released on flush ticks, so release timestamps are quantised to
# the tick. Comparing CONSECUTIVE pairs therefore measures the quantiser, not
# the delay: at a 15.6ms cadence and a 0.5ms tick that is +-3% of noise on every
# gap, the same order as the ~9% effect under test, and it produces nonsense
# outliers whenever two packets land in one tick. Measure across a window
# instead — which is also the honest perceptual unit, since the console smooths
# over several updates rather than reacting to a single inter-arrival gap.
RATE_WINDOW_PACKETS = 8


def run_profile(*, slew_ms_per_s: float, target_ms: float, packet_hz: float,
                hold_s: float, step_s: float = 0.0002,
                preset_ms: float | None = None) -> dict:
    """Drive one full engage/hold/disengage cycle and measure the rate.

    Returns a dict of measurements. The buffer under test is the real shipping
    object; only the clock and the handle are substituted.
    """
    clock = _VirtualClock()
    handle = _RecordingHandle(clock)

    real_open = nexus_svc._open_intercept_handle
    nexus_svc._open_intercept_handle = lambda filter_str: handle
    try:
        buf = nexus_svc._InboundDelayBuffer(slew_ms_per_s=slew_ms_per_s,
                                            clock=clock)
        ok, err = buf.start(CONSOLE_IP, COURT_IP)
        if not ok:
            raise RuntimeError(f"intercept start failed: {err}")
        # The bench owns the loop; retire the real threads.
        buf._stop.set()
        for t in buf._threads:
            t.join(timeout=1.0)
        buf._threads = []
    finally:
        nexus_svc._open_intercept_handle = real_open

    packet_period = 1.0 / packet_hz
    arrivals: dict[int, float] = {}
    seq = 0
    next_packet_at = clock()

    ramp_s = (target_ms / slew_ms_per_s) if slew_ms_per_s > 0 else 0.0

    if preset_ms is not None:
        # Constant-delay profile: start ALREADY at target so the run contains no
        # transition at all. This is the control case — it is what the always-on
        # engagement policy looks like once engaged, and D' is identically zero.
        buf._target_ms = preset_ms
        buf._delay_ms = preset_ms
        ramp_s = 0.0
        phases = [("hold", preset_ms, hold_s)]
    else:
        phases = [
            ("engage", target_ms, max(ramp_s * 1.4, 0.2)),
            ("hold", target_ms, hold_s),
            ("disengage", 0.0, max(ramp_s * 1.4, 0.2)),
        ]

    marks: list[tuple[str, float]] = []
    for name, cmd_target, duration in phases:
        marks.append((name, clock()))
        buf.set_delay(cmd_target)
        end_at = clock() + duration
        while clock() < end_at:
            clock.advance(step_s)
            # Re-assert the target the way a live controller's keepalive would,
            # so the starvation watchdog stays quiet.
            buf.set_delay(cmd_target)
            buf.advance_slew(clock())
            while next_packet_at <= clock():
                arrivals[seq] = next_packet_at
                buf.enqueue(_Pkt(seq))
                seq += 1
                next_packet_at += packet_period
            buf.flush_due(clock())

    # Drain whatever is still held. Everything released from here on leaves at
    # the SAME virtual timestamp, so those pairs have a zero output span and
    # would otherwise produce garbage windows (a profile with no disengage phase
    # ends with a full buffer, and the collapse read as a 0.098 multiplier).
    # Measure only what the run itself released.
    measured_count = len(handle.released)
    clock.advance(target_ms / 1000.0 + 1.0)
    buf.flush_due(clock())

    released = handle.released[:measured_count]
    if len(released) < 10:
        raise RuntimeError(f"only {len(released)} packets released — bad profile")

    # Ordering is checked against EVERY packet including the drain — a reorder
    # during teardown is still a reorder.
    all_order = [s for s, _ in handle.released]
    in_order = all_order == sorted(all_order)
    total_released = len(handle.released)

    # Windowed rate multiplier: input span / output span across
    # RATE_WINDOW_PACKETS consecutive releases. See the note on the constant.
    window = min(RATE_WINDOW_PACKETS, max(2, len(released) // 4))
    values: list[float] = []
    for i in range(window, len(released)):
        seq_a, out_a = released[i - window]
        seq_b, out_b = released[i]
        in_gap = arrivals[seq_b] - arrivals[seq_a]
        out_gap = out_b - out_a
        if in_gap <= 0 or out_gap <= 0:
            continue
        values.append(in_gap / out_gap)
    if not values:
        raise RuntimeError("no usable rate windows")
    stats = buf.stats()

    return {
        "slew_ms_per_s": slew_ms_per_s,
        "target_ms": target_ms,
        "packet_hz": packet_hz,
        "packets": total_released,
        "measured": len(released),
        "in_order": in_order,
        "lost": seq - total_released,
        "rate_min": min(values),
        "rate_max": max(values),
        "rate_median": statistics.median(values),
        "ramp_seconds": ramp_s,
        "peak_slew_ms_per_s": stats["peak_slew_ms_per_s"],
        "overflow_released": stats["overflow_released"],
        "marks": marks,
        "passed": (min(values) >= RATE_FLOOR and max(values) <= RATE_CEILING
                   and in_order and seq == total_released),
    }


def _print_profile(label: str, r: dict) -> None:
    verdict = "PASS" if r["passed"] else "FAIL"
    print(f"\n  {label}")
    print(f"    slew cap ............ {r['slew_ms_per_s']:.0f} ms/s "
          f"(D' = {r['slew_ms_per_s'] / 1000.0:.3f})")
    print(f"    ramp 0 -> {r['target_ms']:.0f}ms ...... {r['ramp_seconds']:.2f} s")
    print(f"    packets ............. {r['packets']} released, "
          f"{r['measured']} measured  "
          f"(lost {r['lost']}, in order: {r['in_order']})")
    print(f"    rate multiplier ..... min {r['rate_min']:.3f}   "
          f"median {r['rate_median']:.3f}   max {r['rate_max']:.3f}")
    print(f"    peak applied slew ... {r['peak_slew_ms_per_s']:.1f} ms/s")
    print(f"    console sees ........ {r['rate_min'] * 100:.1f}% of normal "
          f"update rate at worst")
    print(f"    verdict ............. {verdict} "
          f"(bar: {RATE_FLOOR:.2f} <= rate <= {RATE_CEILING:.2f})")


def run_offline(args) -> int:
    print("=" * 72)
    print("  INBOUND METER DELAY — OFFLINE BENCH")
    print("  real _InboundDelayBuffer, virtual clock, synthetic packet source")
    print("=" * 72)

    profiles: list[tuple[str, float]] = [
        (f"shipping cap ({args.slew:.0f} ms/s)", args.slew),
    ]
    if args.compare:
        # The configuration that got the feature deleted, in its own units:
        # MeterDelayController kRampUpPerTickMs=55 / kTickMs=50.
        profiles.append(("DELETED config (55ms per 50ms tick)", 1100.0))
        profiles.append(("constant delay, no transition", 0.0))

    results = []
    for label, slew in profiles:
        if slew <= 0.0:
            # The control case: a delay that never changes has D' = 0 and is
            # provably harmless. This is the whole argument for the always-on
            # engagement policy — once engaged there is no transition to cost
            # anything, and the console's stream is bit-identical, time-shifted.
            r = run_profile(slew_ms_per_s=nexus_svc._METER_MAX_SLEW_MS_PER_S,
                            target_ms=args.target, packet_hz=args.rate,
                            hold_s=args.hold, preset_ms=args.target)
            r["slew_ms_per_s"] = 0.0
        else:
            r = run_profile(slew_ms_per_s=slew, target_ms=args.target,
                            packet_hz=args.rate, hold_s=args.hold)
        results.append((label, r))
        _print_profile(label, r)

    print()
    ok = all(r["passed"] for label, r in results if "DELETED" not in label)
    if args.compare:
        print("  Interpretation: the DELETED row is expected to FAIL — it is the")
        print("  historical defect reproduced, not a regression in this build.")
    print()
    return 0 if ok else 1


def _bridge_token() -> str | None:
    for path in (nexus_svc._localappdata_token_path(),
                 nexus_svc._programdata_token_path()):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        try:
            return str(json.loads(raw).get("token", ""))
        except Exception:
            return raw
    return None


def run_live(args) -> int:
    """Command a ramp against a running, ARMED bridge and read back its stats."""
    token = _bridge_token()
    if not token:
        print("ERROR: no bridge token found. Is nexus_svc running?")
        return 1

    sock = socket.create_connection((nexus_svc.LISTEN_HOST, nexus_svc.LISTEN_PORT),
                                    timeout=5.0)
    fh = sock.makefile("rwb")

    def send(obj):
        fh.write((json.dumps(obj) + "\n").encode()); fh.flush()

    def read_until(cmd, limit=200):
        for _ in range(limit):
            line = fh.readline()
            if not line:
                return None
            msg = json.loads(line)
            if msg.get("cmd") == cmd or msg.get("event") == "error":
                return msg
        return None

    send({"cmd": "auth", "token": token})
    if (read_until("auth") or {}).get("event") != "ack":
        print("ERROR: bridge auth failed")
        return 1

    send({"cmd": "status"})
    status = read_until("status") or {}
    print(f"  bridge v{status.get('version')} features={status.get('features')} "
          f"armed={status.get('meter_delay_armed')}")
    if not status.get("meter_delay_armed"):
        print("\nERROR: the bridge is DISARMED — this is the shipping default.")
        print(f"       Restart it with {nexus_svc._METER_ARM_FLAG} or "
              f"{nexus_svc._METER_ARM_ENV}=1.")
        return 1

    send({"cmd": "meter_delay_stats"})
    stats = read_until("meter_delay_stats") or {}
    print(f"  stats: {json.dumps(stats.get('stats', {}), indent=2)}")
    sock.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slew", type=float,
                    default=nexus_svc._METER_MAX_SLEW_MS_PER_S,
                    help="slew cap in ms/s (default: the shipping value)")
    ap.add_argument("--target", type=float, default=165.0,
                    help="delay target in ms (default: the 165ms sweet spot)")
    ap.add_argument("--rate", type=float, default=64.0,
                    help="synthetic packet cadence in Hz (default: 64)")
    ap.add_argument("--hold", type=float, default=1.0,
                    help="seconds to hold at target (default: 1.0)")
    ap.add_argument("--compare", action="store_true",
                    help="also run the deleted config and a constant delay")
    ap.add_argument("--live", action="store_true",
                    help="query a running armed bridge instead of running offline")
    args = ap.parse_args()
    return run_live(args) if args.live else run_offline(args)


if __name__ == "__main__":
    sys.exit(main())
