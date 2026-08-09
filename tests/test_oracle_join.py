"""detframes JOIN orion_native.log frozen-meter oracle: the release-line parser and the per-shot
freeze inversion must recover an injected latency from a synthetic detframes stream."""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "timing"))
import oracle_join as oj


def test_parse_release_line():
    rels = oj._REL_RE.match(
        "2026-07-06T09:15:41.032Z  Release issued: fill 57.9% target 93.9% (green 89.5)"
    )
    assert rels is not None
    assert abs(float(rels.group("fill")) - 57.9) < 1e-6
    ms = oj._iso_to_epoch_ms("2026-07-06T09:15:41.032")
    assert ms > 0
    # sub-second precision preserved
    assert abs((ms / 1000.0) % 1.0 - 0.032) < 1e-3


def test_measure_shot_recovers_latency():
    # build a synthetic detected fill stream: rise crossing F_stop=88 at t_rel+65ms, then a freeze.
    base = 1_700_000_000_000.0
    t_rel = base + 500.0
    latency = 65.0
    f_stop = 88.0
    slope = (f_stop - 20.0) / 180.0
    t_cross = t_rel + latency
    det = []
    t = base
    while t <= t_cross + 300.0:
        f = 20.0 + slope * (t - (t_cross - (f_stop - 20.0) / slope))
        det.append((t, min(f, f_stop)))
        t += 16.0
    m = oj.measure_shot(det, t_rel, f_cmd=45.0)
    assert m is not None
    assert abs(m["latency_ms"] - latency) <= 25.0, m["latency_ms"]
    assert abs(m["f_stop"] - f_stop) <= 2.0


def test_measure_shot_none_without_freeze():
    # a pure monotonic rise with no plateau -> no clean freeze
    base = 1_700_000_000_000.0
    det = [(base + i * 16.0, 20.0 + i * 2.0) for i in range(30)]
    m = oj.measure_shot(det, base + 100.0, f_cmd=30.0)
    assert m is None
