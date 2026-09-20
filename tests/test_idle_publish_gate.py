"""[ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] What the reader is allowed to PUBLISH.

THE COMPLAINT. Between shots the locator locks white columns that are not meters: court
lines, the scoreboard, the shot chart, the owner's own jersey. Measured on the 2026-09-15
session, `DETECTOR HEALTH found=2745` against ~600 frames that actually carried a meter, and
one static column at [777,342,26,110] locked and dropped TWENTY times in 40 s. The engine
ignores them, so it was filed as cosmetic. It is not: the overlay draws every one of those
boxes on the owner's screen, and each one is a lock the next press has to evict
(`STALE LOCK DROPPED AT PRESS`) before it can find the real meter.

THE GATE. Tracking is untouched -- the lock, its ruler and its eviction rules all still run.
Only PUBLICATION (what reaches the engine and the overlay) is gated, on three doors:
    (a) a press is armed,
    (b) the column's fill ROSE >= 3 pp over the last 3 reads of the same column,
    (c) it continues an already-published lock.
The load-bearing test in this file is the first one: a real shot must not lose a single frame.

[SHIP CONFIG 2026-09-17] The gate now ships OFF (it was in the OFF half of the graded launch
line: it is one more publication layer that can refuse a real first read, and the 09-16 blind
run -- six consecutive backstopped presses -- was that failure). The MECHANISM is unchanged and
this suite still owns it, so every test here arms the gate explicitly through the autouse
fixture below instead of relying on a default. `tests/test_ship_defaults.py` owns the default.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader


@pytest.fixture(autouse=True)
def _gate_armed(monkeypatch):
    """This suite is ABOUT the gate, so it arms it; the ship default is off (see the docstring).

    A test that needs the legacy emission still sets "0" in its own body, which wins.
    """
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "1")

FRAME_DT = 1.0 / 60.0
CAP_TOP, CAP_BOT = 464, 470          # the green cap's rows
BASE, COL_X, COL_W = 600, 900, 24    # the white column
FILLABLE = BASE - CAP_BOT


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def meter_frame(fill_pct, x=COL_X):
    """A 2K27 White meter filled to `fill_pct` with its green cap in place."""
    f = np.full((1080, 1920, 3), 40, np.uint8)
    top = BASE - int(round(FILLABLE * max(0.0, min(100.0, fill_pct)) / 100.0))
    f[min(top, BASE - 1):BASE, x:x + COL_W] = (250, 252, 253)
    f[CAP_TOP:CAP_BOT, x:x + COL_W] = (60, 200, 60)
    return f


def reader(**_kw):
    return SimpleMeterReader(1920, 1080, cfg=_Cfg())


def feed(r, frames, *, armed, t=1000.0):
    """Push `frames` at 60 fps with the shot gate held at `armed`. -> list of DetectResult."""
    out = []
    for f in frames:
        r.set_shot_state(bool(armed), 0.0, bool(armed))
        out.append(r.detect(f, ts=t))
        t += FRAME_DT
    return out


def first_published(results):
    for i, res in enumerate(results):
        if res.detected:
            return i
    return -1


# ------------------------------------------------------------------ the load-bearing one
def test_an_armed_press_publishes_on_exactly_the_frame_it_publishes_today(monkeypatch):
    """THE gate's whole licence. A shot is published from its first accepted frame, with no
    history required, so the gate cannot cost a real meter a single frame of its rise."""
    rise = [meter_frame(p) for p in (0, 6, 12, 18, 24, 32, 40, 52, 64, 78, 92, 100)]

    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")
    legacy = feed(reader(), rise, armed=True)
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "1")
    gated = feed(reader(), rise, armed=True)

    assert first_published(legacy) >= 0, "the fixture must produce a detection at all"
    assert first_published(gated) == first_published(legacy)
    assert [r.detected for r in gated] == [r.detected for r in legacy]
    assert [tuple(r.bbox) for r in gated] == [tuple(r.bbox) for r in legacy]


def test_the_switch_arms_the_gate_and_restores_the_legacy_emission(monkeypatch):
    """[SHIP CONFIG 2026-09-17] Both directions of the one switch, from its SHIP default.

    The default itself moved to OFF and is pinned by tests/test_ship_defaults.py; what this
    test owns is that the env still decides, in both directions, on every construction.
    """
    monkeypatch.delenv("ORION_READER_IDLE_PUBLISH_GATE", raising=False)
    assert reader()._idle_pub_gate is False, "ship default"
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "1")
    assert reader()._idle_pub_gate is True
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")
    assert reader()._idle_pub_gate is False


# ------------------------------------------------------------------ (a) armed
def test_an_idle_static_column_is_tracked_but_never_published():
    """The false-lock class, in its live shape: a column that is simply THERE, with no press
    behind it and no growth. The reader may lock it; the engine and the overlay may not see
    it, and every refusal is counted."""
    f = meter_frame(55.0)
    r = reader()
    res = feed(r, [f] * 12, armed=False)
    assert not any(x.detected for x in res), [x.detected for x in res]
    assert all(tuple(x.bbox) == (0, 0, 0, 0) for x in res)
    assert r._det_diag["idle_unpublished"] >= 1
    # ... and the reader still TRACKED it, so the next press inherits the memory.
    assert r.last_fill > 0.0


def test_the_same_column_publishes_the_moment_a_press_arms():
    f = meter_frame(55.0)
    r = reader()
    assert not any(x.detected for x in feed(r, [f] * 6, armed=False))
    armed = feed(r, [f] * 3, armed=True, t=1000.0 + 6 * FRAME_DT)
    assert armed[0].detected, "an armed press publishes on its first frame, with no warm-up"
    assert tuple(armed[0].bbox)[2] > 0


# ------------------------------------------------------------------ (b) the rise
def test_the_owner_shooting_without_the_bot_stays_visible():
    """No press is armed (the owner pressed the button himself), but a REAL meter is rising.
    It must publish, and within a frame or two of the first read -- a meter climbs 3-4 pp per
    60 fps frame, so the 3 pp rule is satisfied on its second read."""
    rise = [meter_frame(p) for p in (18, 24, 31, 38, 46, 55, 64, 74, 85, 96)]
    r = reader()
    res = feed(r, rise, armed=False)
    idx = first_published(res)
    assert idx >= 0, "a rising meter must never be withheld"
    assert idx <= 2, f"published only at read {idx}: the rise rule is too slow"
    assert all(x.detected for x in res[idx:]), "and it stays published for the rest of the rise"


def test_a_jittering_static_column_never_looks_like_a_rise():
    """A false lock's reported fill wanders by well under a point. It must not accumulate
    into the 3 pp the rule asks for."""
    r = reader()
    res = feed(r, [meter_frame(55.0 + 0.4 * (i % 3)) for i in range(24)], armed=False)
    assert not any(x.detected for x in res)


# ------------------------------------------------------------------ (c) continuation
def test_a_published_shot_stays_published_after_the_gate_disarms():
    """The post-release settle: the shot gate closes ~1.2 s after the release while the meter
    is still on screen at its settled fill, not rising. Rule (c) is what keeps it drawn."""
    rise = [meter_frame(p) for p in (18, 26, 36, 48, 62, 76, 90, 98)]
    r = reader()
    armed = feed(r, rise, armed=True)
    assert armed[-1].detected
    t = 1000.0 + len(rise) * FRAME_DT
    settled = feed(r, [meter_frame(98.0)] * 10, armed=False, t=t)
    assert all(x.detected for x in settled), [x.detected for x in settled]


def test_an_unpublished_lock_cannot_bootstrap_itself_through_continuation():
    """(c) extends an ALREADY-PUBLISHED lock. A lock that never passed (a) or (b) has nothing
    to continue, which is what stops an idle false lock from talking its way on screen."""
    r = reader()
    res = feed(r, [meter_frame(55.0)] * 40, armed=False)
    assert not any(x.detected for x in res)
    assert r._idle_pub_box is None


def test_a_different_column_does_not_inherit_the_published_lock():
    """The re-seat case: the published meter vanishes and a static impostor appears 500 px
    away. Same reader, same frame budget -- it is not a continuation of anything."""
    rise = [meter_frame(p) for p in (18, 26, 36, 48, 62, 76)]
    r = reader()
    assert feed(r, rise, armed=True)[-1].detected
    t = 1000.0 + len(rise) * FRAME_DT
    other = feed(r, [meter_frame(55.0, x=320)] * 12, armed=False, t=t)
    assert not other[-1].detected, "a column 580 px away is a different lock"


# ---------------------------------------------------- the SHIPPED wiring, where it matters
def _production_reader():
    """The orchestrator's own construction (remote_play_orchestrator :1683/:1711) plus the
    shipped proposer wiring from run_orion.local.ps1."""
    return SimpleMeterReader(1920, 1080, cfg=_Cfg(), require_gameplay_eligibility=True)


@pytest.fixture
def shipped_detector(monkeypatch):
    for k, v in {"ORION_METER_DETECTOR": "1", "ORION_METER_PROPOSER": "cv",
                 "ORION_METER_DETECTOR_SYNC": "1",
                 "ORION_METER_DETECTOR_MIN_INTERVAL_MS": "0"}.items():
        monkeypatch.setenv(k, v)
    import meter_detector_yolo as mdy
    # the process-wide singletons: a reader built under other env must not be reused
    monkeypatch.setattr(mdy, "_ASYNC", None)
    monkeypatch.setattr(mdy, "_SINGLETON", None)
    yield
    mdy._ASYNC = None
    mdy._SINGLETON = None


def test_the_gameplay_gate_does_not_already_stop_this(shipped_detector, monkeypatch):
    """WHY THE GATE IS NOT REDUNDANT. `require_gameplay_eligibility` closes read() between
    shots, but the DETECTOR-FILL path deliberately runs after it ("a gameplay-ineligible frame
    during the fill is no longer dropped: the detector's meter presence is the proof"), so a
    locator lock on a static white column publishes anyway -- 117 of 120 unarmed frames here.
    That is the box on the owner's overlay and the lock the next press has to evict."""
    f = meter_frame(55.0)
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")
    legacy = feed(_production_reader(), [f] * 120, armed=False)
    assert sum(x.detected for x in legacy) > 100, "the fixture must reproduce the complaint"

    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "1")
    r = _production_reader()
    gated = feed(r, [f] * 120, armed=False)
    assert sum(x.detected for x in gated) == 0
    assert r._det_diag["idle_unpublished"] == sum(1 for x in legacy if x.detected)
