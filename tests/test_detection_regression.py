"""
test_detection_regression.py -- OFFLINE detection regression gate as a pytest module.

Replays the recorded live framedumps through the PRODUCTION simple_meter_reader.SimpleMeterReader
and asserts the achieved detection-quality floors still hold (committed in
tools/regression/gate_floors.json). Any future change that silently regresses within-shot detection,
tip capture, false-lock immunity, fill shape, or camera-adapt makes one of these FAIL.

Each session is SKIPPED (not failed) when its framedump is absent, so CI without the multi-GB dumps
still runs the rest of the suite; where the dumps exist (the dev box) the gates run fully.

Run:  C:/Python314/python.exe -m pytest tests/test_detection_regression.py -v
"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "tools", "regression"))  # replay_gates (NOT tools/diagnostics)

import replay_gates as RG  # noqa: E402

# A full replay reads ~23k * 2MB PNGs twice -> tens of minutes. It runs by default WHERE THE DUMPS
# EXIST (the protect-the-gains contract), but set ORION_SKIP_SLOW_REGRESSION=1 to skip it in a quick
# `pytest` sweep of the whole suite.
_SKIP_SLOW = os.environ.get("ORION_SKIP_SLOW_REGRESSION", "") not in ("", "0", "false", "False")

pytestmark = pytest.mark.slow

# Each session is measured ONCE (a full replay is expensive) and cached across its gate tests.
_CACHE = {}


def _measure(session):
    if session not in _CACHE:
        session_dir = os.path.join(RG.FRAMEDUMP, session)
        if not RG.list_frames(session_dir):
            _CACHE[session] = None
        else:
            _CACHE[session] = RG.measure_session(session)
    return _CACHE[session]


def _gates(session):
    if _SKIP_SLOW:
        pytest.skip("ORION_SKIP_SLOW_REGRESSION set")
    m = _measure(session)
    if m is None or not m.get("present"):
        pytest.skip("framedump absent: %s" % session)
    _, gates = RG.evaluate_session(session, m=m)
    return {g.name: g for g in gates}


def _ids(session):
    m = _measure(session)
    if m is None or not m.get("present"):
        return []
    _, gates = RG.evaluate_session(session, m=m)
    return [g.name for g in gates]


@pytest.mark.parametrize("session", RG.SESSIONS)
def test_session_detection_gates(session):
    """Every detection gate for the session must hold at or above its committed floor."""
    gates = _gates(session)
    failures = [repr(g) for g in gates.values() if not g.passed]
    assert not failures, "regressed detection gates in %s:\n  %s" % (session, "\n  ".join(failures))


def test_at_least_one_session_present():
    """Guard: on the dev box (where the dumps live) the suite must actually exercise a session, so a
    silently-empty framedump tree can't turn the whole gate into a no-op green."""
    present = [s for s in RG.SESSIONS if RG.list_frames(os.path.join(RG.FRAMEDUMP, s))]
    if not present:
        pytest.skip("no framedumps present on this machine (CI without dumps)")
    assert present
