"""READ-RESCUE (ORION_METER_READ_RESCUE): same-frame box reseat when the held box
cannot measure any fill run.

Measured failure (session_20260830_112306): during camera pan bursts the served box
lags the meter 12-44px (every serving mechanism is bounded below that error by
design) and the fill read hard-zeroes past ~12px of box offset. The rescue queues
one throttled latest-frame priority request on the detector worker. Its next unique
result passes the SHIPPED lifecycle policy (teleports still refused) and hard-reseats
serving without ever running ORT on the capture callback.

These tests drive detect() with a scripted fake locator (the async detector's
interface) and lock in: trigger-on-failure only, no inline detect_now call, honest
zero on no-find, deferred reseat, throttle, teleport refusal, and flag-off inertness.
"""
import numpy as np
import pytest

import simple_meter_reader as smr
from simple_meter_reader import SimpleMeterReader


# [ORION_READER_IDLE_PUBLISH_GATE 2026-09-15] The fixtures below feed a meter with NO press
# armed and (mostly) a constant fill -- byte for byte the shape the reader's idle publication
# gate now withholds from the engine and the overlay (see SimpleMeterReader._idle_publish_ok).
# The gate is a PUBLICATION policy with its own suite (tests/test_idle_publish_gate.py); these
# tests are about what the reader MEASURES, so the gate is switched off here and they keep
# measuring it.
@pytest.fixture(autouse=True)
def _idle_publish_gate_off(monkeypatch):
    monkeypatch.setenv("ORION_READER_IDLE_PUBLISH_GATE", "0")


BH, BW = 107, 24
METER_X, METER_Y = 600, 300
FILL_EDGE = 40          # rows from box top -> fill ~62%


def _meter_patch(fill_edge_row=FILL_EDGE):
    """Simplified 2K27 white-meter patch: saturated empty track above the edge,
    bright-neutral fill below it, base shelf, dark outline columns."""
    p = np.zeros((BH, BW, 3), np.uint8)
    p[:] = (30, 70, 140)                        # empty track (V=140, saturated)
    p[fill_edge_row:100] = (250, 252, 253)      # white fill core
    p[100:103] = (168, 170, 171)                # base shelf
    p[103:] = (100, 105, 110)
    p[:, :3] = (60, 60, 65)                     # outline columns
    p[:, BW - 3:] = (60, 60, 65)
    p[2:4, 4:BW - 4] = (60, 200, 60)            # green make-window cap
    return p


def _frame():
    f = np.full((720, 1280, 3), 35, np.uint8)
    f[METER_Y:METER_Y + BH, METER_X:METER_X + BW] = _meter_patch()
    return f


TRUE_BOX = (METER_X, METER_Y, BW, BH)


class FakeLoc:
    """Scripted stand-in for AsyncMeterLocator: latest() serves what the test says;
    submit_priority() publishes its scripted truth on the next submit/read callback,
    modelling a fast asynchronous worker without sleeps."""

    def __init__(self):
        self.ok = True
        self.provider = "fake"
        self.infer_ms = 0.0
        self.res = (False, None, 0.0, -1.0)
        self.truth = None          # priority worker answer (None -> no find)
        self.pending = None
        self.priority_calls = 0
        self.now_calls = 0

    def submit(self, frame, ts):
        if self.pending is not None:
            self.res = self.pending
            self.pending = None

    def submit_priority(self, frame, ts):
        self.priority_calls += 1
        if self.truth is None:
            self.pending = (False, None, 0.0, float(ts))
        else:
            self.pending = (True, tuple(self.truth), 0.9, float(ts))

    def latest(self):
        return self.res

    def detect_now(self, frame, ts):
        self.now_calls += 1
        raise AssertionError("capture callback must not run detector inference")


def _locked_reader(monkeypatch, **env):
    """Reader with a fake locator holding a LOCK on the true meter box."""
    monkeypatch.setenv("ORION_METER_DETECTOR", "0")   # constructor must not load YOLO
    monkeypatch.setenv("ORION_METER_DETECTOR_TRACK", "0")  # deterministic: no NCC moves
    monkeypatch.setenv("ORION_METER_DETECTOR_SYNC_ACQUIRE", "0")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    r = SimpleMeterReader(1280, 720)
    fake = FakeLoc()
    r._meter_detector = fake
    fr = _frame()
    # two unique fresh on-meter results -> lifecycle ACQUIRE streak -> locked
    fake.res = (True, TRUE_BOX, 0.9, 0.98)
    res = r.detect(fr, ts=1.00)
    fake.res = (True, TRUE_BOX, 0.9, 1.00)
    res = r.detect(fr, ts=1.02)
    assert r._det_state == "locked"
    assert res.detected and res.fill_pct > 30.0
    fake.now_calls = 0
    return r, fake, fr


def test_rescue_reseats_offset_box(monkeypatch):
    """Sustained fresh-but-stale detections 30px off the meter (a pan lag) walk the
    served box off the ribbon under the emit slew until the read zeroes; the rescue's
    priority result reseats the box on the following callback. The trigger frame is
    an honest zero, then serving recovers without an inline inference stall."""
    r, fake, fr = _locked_reader(monkeypatch)
    stale = (METER_X - 30, METER_Y, BW, BH)
    fake.truth = TRUE_BOX
    fills = []
    for k in range(5):
        ts = 1.04 + 0.02 * k
        fake.res = (True, stale, 0.9, ts - 0.02)   # fresh by TTL, computed frames ago
        res = r.detect(fr, ts=ts)
        fills.append(res.fill_pct)
    assert fake.priority_calls >= 1                # the drift crossed the read cliff
    assert fake.now_calls == 0
    assert 0.0 in fills                            # trigger frame stays honest
    assert any(fills[i] == 0.0 and fills[i + 1] > 30.0
               for i in range(len(fills) - 1)), fills  # next worker result recovered
    assert r._det_diag["rescue_seat"] >= 1


def test_no_rescue_when_read_succeeds(monkeypatch):
    """A readable held box must never pay for an inference."""
    r, fake, fr = _locked_reader(monkeypatch)
    fake.res = (True, (METER_X - 4, METER_Y, BW, BH), 0.9, 1.02)  # inside tolerance
    fake.truth = TRUE_BOX
    res = r.detect(fr, ts=1.04)
    assert res.fill_pct > 30.0
    assert fake.now_calls == 0
    assert fake.priority_calls == 0
    assert r._det_diag["rescue"] == 0


def test_honest_zero_when_meter_truly_gone(monkeypatch):
    """Worker request finds nothing -> the honest zero stands, box not moved."""
    r, fake, fr = _locked_reader(monkeypatch)
    blank = np.full((720, 1280, 3), 35, np.uint8)   # meter vanished
    fake.res = (True, TRUE_BOX, 0.9, 1.02)
    fake.truth = None
    res = r.detect(blank, ts=1.04)
    assert fake.priority_calls == 1
    assert fake.now_calls == 0
    assert not res.detected                  # retained box is not a white-edge measurement
    assert res.fill_pct == 0.0
    assert tuple(r._det_active_box) == TRUE_BOX
    assert r._det_diag["rescue_seat"] == 0


def test_rescue_throttled(monkeypatch):
    """Two failing frames inside RESCUE_GAP_MS cost exactly one inference."""
    r, fake, fr = _locked_reader(monkeypatch, ORION_METER_RESCUE_GAP_MS="1000")
    blank = np.full((720, 1280, 3), 35, np.uint8)
    fake.res = (True, TRUE_BOX, 0.9, 1.02)
    fake.truth = None
    r.detect(blank, ts=1.04)
    fake.res = (True, TRUE_BOX, 0.9, 1.04)
    r.detect(blank, ts=1.06)
    assert fake.priority_calls == 1
    assert fake.now_calls == 0


def test_teleport_rescue_refused(monkeypatch):
    """A same-frame proposal far outside the lifecycle jump gate must NOT move the
    lock (shipped teleport policy) -- the dropout stays an honest zero."""
    r, fake, fr = _locked_reader(monkeypatch)
    blank = np.full((720, 1280, 3), 35, np.uint8)
    far = np.full((720, 1280, 3), 35, np.uint8)
    far[METER_Y:METER_Y + BH, 40:40 + BW] = _meter_patch()   # 560px away
    fake.res = (True, TRUE_BOX, 0.9, 1.02)
    fake.truth = (40, METER_Y, BW, BH)
    res = r.detect(far, ts=1.04)
    assert fake.priority_calls == 1
    assert fake.now_calls == 0
    assert res.fill_pct == 0.0
    assert tuple(r._det_active_box) == TRUE_BOX       # lock did not teleport
    assert r._det_diag["rescue_seat"] == 0


def test_flag_off_is_inert(monkeypatch):
    """ORION_METER_READ_RESCUE=0 restores the shipped dropout behaviour: the same
    sustained stale drift zeroes the read and nothing rescues it."""
    r, fake, fr = _locked_reader(monkeypatch, ORION_METER_READ_RESCUE="0")
    stale = (METER_X - 30, METER_Y, BW, BH)
    fake.truth = TRUE_BOX
    fills = []
    for k in range(5):
        ts = 1.04 + 0.02 * k
        fake.res = (True, stale, 0.9, ts - 0.02)
        res = r.detect(fr, ts=ts)
        fills.append(res.fill_pct)
    assert fake.now_calls == 0
    assert fake.priority_calls == 0
    assert min(fills) == 0.0, fills                # the shipped dropout reproduces
