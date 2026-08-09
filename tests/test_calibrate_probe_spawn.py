"""Session-join tests for tools/timing/calibrate_probe_spawn.py (v2).

The v1 tool joined median(ALL probe raws in the file) against the LAST release marker in the
file. On the real rotated 2026-08-06 log that mixed a capture-card session's probes (raw
median 406.0) with a later decoder-route session's converged oracle (134.0) and produced a
~90ms-wrong constant. These tests pin the session-aware behaviour on miniature transcripts
built from REAL log line shapes (native-prefixed sidecar relay lines).

Loaded by file path so the test does not depend on tools/ being an importable package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "calibrate_probe_spawn", ROOT / "tools" / "timing" / "calibrate_probe_spawn.py")
cps = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cps)


def _obs(ts: str, total: float, fixed: float, n: int) -> str:
    return (f"{ts}  Sidecar: 2026-08-05 19:01:00,000 INFO latency_estimator: "
            f"latency observation: seq={n} calibration=0 accepted=1 status=warming reject=- "
            f"total_ms={total:.1f} sigma_ms=5.6 fixed_ms={fixed:.1f} sd_ms=5.5 n={n} corr=0 "
            f"freeze=rise controlled=0 validated=0 target_pct=-1.0 tolerance_pct=-1.0 "
            f"f_stop=88.6 peak=89.0")


def _probe(ts: str, raw: float) -> str:
    return (f"{ts}  Sidecar: 2026-08-05 19:01:43,733 WARNING latency_estimator: "
            f"probe closed: raw={raw:.1f}ms spawn=0.0 rtt=0.0")


def _estimate(ts: str, d: float, probes: int, n_labels: int, total: float) -> str:
    return (f"{ts}  Sidecar: 2026-08-05 19:01:55,734 INFO latency_estimator: "
            f"probe spawn estimate: d_spawn={d:.1f}ms probes={probes} n_labels={n_labels} "
            f"measured_total_ms={total:.1f} (telemetry only)")


def _session(ts: str) -> str:
    return (f"{ts}  New sidecar process generation: timing scope baseline reset; "
            f"fresh neutral route proof required")


_SESSION_A = "\n".join(
    [_session("2026-08-06T00:00:00.000Z")]
    + [_obs(f"2026-08-06T00:01:{28 + i:02d}.100Z", 226.1 - i, 217.5 - i, i + 1)
       for i in range(4)]
    + [_probe(f"2026-08-06T00:02:{i:02d}.500Z", 400.0 + i) for i in range(0, 48, 6)]
)

# A later session on a faster route: converged low totals that must NOT join session A's probes.
_SESSION_B = "\n".join(
    [_session("2026-08-06T09:53:28.005Z")]
    + [_obs(f"2026-08-06T10:30:{i:02d}.000Z", 134.0, 125.7, i + 1) for i in range(6)]
)


def test_single_clean_session_joins_and_recommends(tmp_path, capsys):
    log = tmp_path / "session.log"
    log.write_text(_SESSION_A, encoding="utf-8")
    rc = cps.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    # median raw = 421 (raws 400..442 step 6), oracle = median(226.1,225.1,224.1,223.1)=224.6
    assert '"probe_spawn_offset_ms": 196.4' in out


def test_rotated_log_never_joins_across_sessions(tmp_path, capsys):
    # The v1 failure shape: session B's converged low oracle sits later in the same file.
    log = tmp_path / "rotated.log"
    log.write_text(_SESSION_A + "\n" + _SESSION_B, encoding="utf-8")
    rc = cps.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    # Joined within session A only: identical result to the clean-session case, and nowhere
    # near the ~287 the file-global join would produce against session B's 134ms oracle.
    assert '"probe_spawn_offset_ms": 196.4' in out


def test_estimator_line_disagreement_refuses(tmp_path, capsys):
    log = tmp_path / "disagree.log"
    log.write_text(
        _SESSION_A + "\n"
        + _estimate("2026-08-06T00:02:42.500Z", 150.0, 8, 2, 271.0),
        encoding="utf-8")
    rc = cps.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "REFUSING" in out


def test_probes_without_oracle_says_what_to_do(tmp_path, capsys):
    log = tmp_path / "probes_only.log"
    log.write_text(
        "\n".join(_probe(f"2026-08-06T00:02:{i:02d}.500Z", 406.0) for i in range(0, 18, 6)),
        encoding="utf-8")
    rc = cps.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "real shots" in out


def test_estimator_join_alone_recommends(tmp_path, capsys):
    log = tmp_path / "estimate_only.log"
    log.write_text(
        _estimate("2026-08-06T00:02:42.500Z", 180.9, 16, 2, 225.1), encoding="utf-8")
    rc = cps.main([str(log)])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"probe_spawn_offset_ms": 180.9' in out
