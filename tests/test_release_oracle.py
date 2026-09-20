"""RELEASE ORACLE (ORION_RELEASE_ORACLE) -- the settled white-top-to-green-bottom gap.

[2026-09-15] Measured on session_20260915_185359 (29 releases, 27 gradable): ~35-50 ms after
the bar tops out 2K27 RETRACTS the white fill a few pixels and HOLDS it, and that settled gap
between the fill's top row and the green band's BOTTOM row separates the game's own banner
verdict perfectly -- EXCELLENT <= 3 px, LATE/EARLY >= 4 px (settled_fill EXCELLENT 90.06 +-
0.32 vs LATE 85.47 +- 2.79). The reader already measures both landmarks for every frame, so
the whole feature is a subtraction, one ERROR line per release and one append-only key.

IT IS A DIAGNOSTIC. It is taken 300-500 ms AFTER the release command -- long after every
decision the reader feeds -- and these tests pin that it changes no emitted number.
"""
import numpy as np
import pytest

from simple_meter_reader import SimpleMeterReader

FRAME_DT = 1.0 / 60.0
CAP_TOP, CAP_BOT = 464, 470          # green band rows [464, 470) -> bottom row 469
BASE, COL_X, COL_W = 600, 900, 24


class _Cfg:
    meter_style = "Arrow2"
    meter_color = "White"
    confidence_threshold = 0.32


def settle_frame(gap_px=1, green=True):
    """A settled 2K27 White meter whose fill top sits `gap_px` below the green band."""
    f = np.full((1080, 1920, 3), 40, np.uint8)
    top = CAP_BOT - 1 + int(gap_px)
    f[top:BASE, COL_X:COL_X + COL_W] = (250, 252, 253)
    if green:
        f[CAP_TOP:CAP_BOT, COL_X:COL_X + COL_W] = (60, 200, 60)
    return f


def buried_cap_frame(visible_rows=1):
    """A shot that landed AT the tip: the fill has BURIED the band, leaving only the arrow
    head's last few pixels. Measured on session_20260915_185359 seq 4/11/14/22/23 -- the best
    shots in the batch -- where 3-6 green pixels survived and the emitted cap anchor (which
    only looks for green ABOVE the fill) reported no band at all."""
    f = np.full((1080, 1920, 3), 40, np.uint8)
    for k, row in enumerate(range(CAP_TOP, CAP_BOT)):           # a narrowing arrow head
        w = min(COL_W, 2 + 4 * k)
        c0 = COL_X + (COL_W - w) // 2
        f[row, c0:c0 + w] = (60, 200, 60)
    top = CAP_TOP + int(visible_rows)                            # the fill covers the rest
    f[top:BASE, COL_X:COL_X + COL_W] = (250, 252, 253)
    return f


def reader(**env):
    r = SimpleMeterReader(1920, 1080, cfg=_Cfg())
    r.set_shot_state(True, 0.0, True)
    return r


def warm(r, frame, n=5, t=1000.0):
    """Acquire the meter so the settle frames below are read, not searched for."""
    for _ in range(n):
        r.detect(frame, ts=t)
        t += FRAME_DT
    return t


def release_and_settle(r, frame, t, seq=7, hold_ms=620.0):
    """Issue the release marker and feed settle frames past the oracle's window."""
    r.notify_release(seq)
    end = t + hold_ms / 1000.0
    while t <= end:
        r.detect(frame, ts=t)
        t += FRAME_DT
    return t


# --------------------------------------------------------------- the verdict, end to end
@pytest.mark.parametrize("gap,verdict", [(0, "green"), (2, "green"), (4, "miss"), (11, "miss")])
def test_settled_gap_separates_the_verdict(gap, verdict):
    """0/2 px = the game's own EXCELLENT; 4/11 px = its LATE/EARLY. Threshold 3.5 px."""
    f = settle_frame(gap)
    r = reader()
    t = warm(r, f)
    release_and_settle(r, f, t)
    rec = r.last_release_oracle
    assert rec is not None, "a release must always produce a record"
    assert rec["verdict_proxy"] == verdict, rec
    assert rec["gap_px"] == pytest.approx(max(1.0, float(gap)), abs=1.0), rec
    assert rec["seq"] == 7
    assert rec["settled_fill"] > 80.0 and rec["green_bottom_pct"] > 80.0


def test_no_green_band_is_unknown_not_a_miss():
    """An unmeasurable window is not evidence of a miss; it says so."""
    f = settle_frame(4, green=False)
    r = reader()
    t = warm(r, f)
    release_and_settle(r, f, t)
    rec = r.last_release_oracle
    assert rec is not None and rec["verdict_proxy"] == "unknown", rec
    assert rec["gap_px"] == -1.0 and rec["green_bottom_pct"] == -1.0


def test_a_band_buried_under_the_fill_is_still_measured():
    """The emitted cap anchor cannot see this frame; the oracle uses its own landmark pair
    precisely so the shots that land AT the tip are graded instead of dropped."""
    f = buried_cap_frame()
    r = reader()
    t = warm(r, f)
    release_and_settle(r, f, t)
    rec = r.last_release_oracle
    assert rec is not None and rec["verdict_proxy"] == "green", rec
    assert rec["gap_px"] >= 0.0 and rec["gap_px"] <= 3.5, rec


def test_the_gap_is_measured_in_pixels_and_as_a_fraction_of_the_track():
    f = settle_frame(11)
    r = reader()
    t = warm(r, f)
    release_and_settle(r, f, t)
    rec = r.last_release_oracle
    # BASE - CAP_BOT + 1 = 131 fillable rows: 11 px is ~8 % of the track
    assert rec["gap_pct"] == pytest.approx(100.0 * rec["gap_px"] / 131.0, abs=1.0), rec


# --------------------------------------------------------------- the window
def test_one_line_per_release_and_only_after_the_settle_window():
    f = settle_frame(2)
    r = reader()
    t = warm(r, f)
    r.notify_release(3)
    # 250 ms in: still filling, nothing emitted
    end = t + 0.25
    while t <= end:
        r.detect(f, ts=t)
        t += FRAME_DT
    assert r.last_release_oracle is None
    assert r._ro_pending is True
    end = t + 0.35                       # past the 500 ms rail
    while t <= end:
        r.detect(f, ts=t)
        t += FRAME_DT
    assert r._ro_pending is False
    rec = r.last_release_oracle
    assert rec is not None and rec["seq"] == 3 and rec["end_reason"] == "settled"
    assert rec["n"] >= 3


def test_only_the_settled_frames_are_judged():
    """The rise is still running at +100 ms; a sample from there must not reach the median."""
    r = reader()
    rising = settle_frame(40)            # a mid-rise frame: a huge gap
    settled = settle_frame(2)
    t = warm(r, rising)
    r.notify_release(9)
    for _ in range(12):                  # ~200 ms of rise, all before the LO rail
        r.detect(rising, ts=t)
        t += FRAME_DT
    end = t + 0.45
    while t <= end:
        r.detect(settled, ts=t)
        t += FRAME_DT
    rec = r.last_release_oracle
    assert rec is not None and rec["verdict_proxy"] == "green", rec


def test_a_second_release_flushes_the_first():
    f = settle_frame(2)
    r = reader()
    t = warm(r, f)
    r.notify_release(11)
    for _ in range(6):
        r.detect(f, ts=t)
        t += FRAME_DT
    r.notify_release(12)
    assert r.last_release_oracle is not None
    assert r.last_release_oracle["seq"] == 11
    assert r.last_release_oracle["end_reason"] == "superseded"
    assert r._ro_pending is True and r._ro_seq == 12


def test_the_next_press_closes_an_open_window():
    f = settle_frame(2)
    r = reader()
    t = warm(r, f)
    r.notify_release(13)
    for _ in range(4):
        r.detect(f, ts=t)
        t += FRAME_DT
    r.notify_physical_shot_start(41)
    assert r._ro_pending is False
    assert r.last_release_oracle["end_reason"] == "next_press"


def test_a_release_whose_meter_vanished_still_emits():
    """No meter = no sample; the line still has to appear, saying it could not measure."""
    r = reader()
    t = warm(r, settle_frame(2))
    blank = np.full((1080, 1920, 3), 40, np.uint8)
    t = release_and_settle(r, blank, t, seq=17)
    rec = r.last_release_oracle
    assert rec is not None and rec["seq"] == 17 and rec["verdict_proxy"] == "unknown"


# --------------------------------------------------------------- wiring + inertness
def test_the_knob_switches_the_whole_feature_off(monkeypatch):
    monkeypatch.setenv("ORION_RELEASE_ORACLE", "0")
    f = settle_frame(2)
    r = reader()
    assert r._ro_on is False
    t = warm(r, f)
    release_and_settle(r, f, t)
    assert r.last_release_oracle is None and r._ro_pending is False


def test_the_threshold_is_a_knob(monkeypatch):
    monkeypatch.setenv("ORION_RELEASE_ORACLE_GAP_PX", "8.0")
    f = settle_frame(4)
    r = reader()
    assert r._ro_thr_px == pytest.approx(8.0)
    t = warm(r, f)
    release_and_settle(r, f, t)
    assert r.last_release_oracle["verdict_proxy"] == "green"


def test_the_oracle_changes_no_emitted_number():
    """Same frames, oracle on and off: every field the timing stack reads is identical."""
    f = settle_frame(4)
    keys = ("detected", "fill_pct", "raw_fill_pct", "confidence", "bbox", "top_pixel_row",
            "green_window_start_pct", "green_window_end_pct", "green_window_center_pct",
            "eta_ms", "rejection_reason", "rise_state", "fill_velocity_pct_s")

    def run(on):
        import os as _os
        prev = _os.environ.get("ORION_RELEASE_ORACLE")
        _os.environ["ORION_RELEASE_ORACLE"] = "1" if on else "0"
        try:
            r = reader()
        finally:
            if prev is None:
                _os.environ.pop("ORION_RELEASE_ORACLE", None)
            else:
                _os.environ["ORION_RELEASE_ORACLE"] = prev
        t = 1000.0
        out = []
        for i in range(8):
            res = r.detect(f, ts=t)
            t += FRAME_DT
            if i == 3:
                r.notify_release(5)
            out.append(tuple(str(getattr(res, k, None)) for k in keys))
        return out

    assert run(True) == run(False)


def test_release_diagnostic_record_carries_the_gap(monkeypatch):
    """APPEND-ONLY: `oracle_gap_px` joins the reader's own per-release JSON record."""
    monkeypatch.setenv("ORION_GREEN_SELF_GRADE", "1")
    f = settle_frame(2)
    r = reader()
    sink = []
    r.set_green_grade_sink(sink.append)
    # the self-grade window is learned from UN-occluded frames: warm on a mid-rise meter
    # (the band fully exposed) before the fill climbs into it.
    t = warm(r, settle_frame(40), n=8)
    t = release_and_settle(r, f, t, seq=23)
    r._gz_end_shot("lock_drop")
    assert sink, "the release diagnostic must still be emitted"
    rec = sink[-1]
    assert "oracle_gap_px" in rec
    assert rec["oracle_gap_px"] == pytest.approx(r.last_release_oracle["gap_px"])
    for legacy in ("label", "fill_at_release", "peak_fill", "release_seq", "release_proxy"):
        assert legacy in rec, "the record is append-only"


def test_the_line_is_emitted_at_error_level(caplog):
    """The sidecar relays sidecar WARNINGs through ONE 1 s throttle slot the press's own arm
    receipt already owns, so a WARNING here would be dropped on exactly the shots it grades."""
    import logging
    f = settle_frame(2)
    r = reader()
    t = warm(r, f)
    with caplog.at_level(logging.ERROR, logger="simple_reader"):
        release_and_settle(r, f, t, seq=31)
    lines = [rec.getMessage() for rec in caplog.records
             if rec.getMessage().startswith("RELEASE ORACLE:")]
    assert len(lines) == 1, lines
    for key in ("seq=31", "gap_px=", "gap_pct=", "settled_fill=", "green_bottom_pct=",
                "verdict_proxy=green"):
        assert key in lines[0], lines[0]


# --------------------------------------------------- the machine line (native RemotePlaySession)
#
# [ORION_RELEASE_ORACLE_TRIM 2026-09-15 owner] The oracle also leaves the sidecar as ONE JSON
# line on the banner verdict's own stdout channel, because the engine's Shot Lead trim consumes
# it. RemotePlaySession.cpp parses `event == "release_oracle"` and reads release_seq / gap_px /
# gap_pct / settled_fill / green_bottom_pct / verdict_proxy / t_ms; `type` carries the same
# value because that is the schema the C++ side documents. These tests pin BOTH keys, the field
# names, the identity (the SHOT-GATE epoch, which is what the banner verdict is attributed on)
# and the once-per-release cardinality.
import json as _json


def _sink_reader(**kw):
    r = reader(**kw)
    out = []
    r.set_release_oracle_sink(out.append)
    return r, out


def test_one_machine_line_per_release_with_the_agreed_schema():
    f = settle_frame(2)
    r, out = _sink_reader()
    t = warm(r, f)
    r.notify_release(7, physical_epoch=918273645, shot_attempt=5, identity_verified=True)
    end = t + 0.62
    while t <= end:
        r.detect(f, ts=t)
        t += FRAME_DT
    assert len(out) == 1, out
    assert out[0].endswith("\n")
    msg = _json.loads(out[0])
    assert msg["event"] == "release_oracle"       # the native dispatches on `event`
    assert msg["type"] == "release_oracle"        # the schema key, accepted as a fallback
    assert set(msg) == {"type", "event", "release_seq", "gap_px", "gap_pct",
                        "settled_fill", "green_bottom_pct", "verdict_proxy", "t_ms"}
    assert msg["release_seq"] == 918273645        # the PHYSICAL epoch, not the marker seq
    assert msg["verdict_proxy"] == "green"
    assert msg["gap_px"] == pytest.approx(r.last_release_oracle["gap_px"])
    assert msg["settled_fill"] == pytest.approx(r.last_release_oracle["settled_fill"])
    assert msg["t_ms"] > 0.0


def test_a_release_with_no_oracle_window_emits_nothing():
    """_ro_flush on a reader with no open release must not invent a line -- the native would
    attribute it to whatever shot the engine's ring happens to be holding."""
    r, out = _sink_reader()
    r._ro_flush("shot_end")
    r._ro_flush("next_press")
    assert out == []


def test_the_machine_line_is_ungated_by_green_self_grade(monkeypatch):
    """ORION_GREEN_SELF_GRADE owns the green-zone experiment; the owner asked for the oracle
    on every release, so the two must not be wired together."""
    monkeypatch.setenv("ORION_GREEN_SELF_GRADE", "0")
    f = settle_frame(6)
    r, out = _sink_reader()
    t = warm(r, f)
    release_and_settle(r, f, t, seq=12)
    assert len(out) == 1, out
    assert _json.loads(out[0])["verdict_proxy"] == "miss"


def test_a_release_relayed_without_an_epoch_falls_back_to_the_marker_seq():
    """The legacy 1-arg notify_release (and the tests above) carry no shot-gate epoch. Such a
    line will simply never join a banner verdict, which is the honest outcome -- but it must
    still carry a non-zero id rather than 0."""
    f = settle_frame(1)
    r, out = _sink_reader()
    t = warm(r, f)
    release_and_settle(r, f, t, seq=44)
    assert len(out) == 1
    assert _json.loads(out[0])["release_seq"] == 44


def test_the_oracle_switch_turns_the_machine_line_off_too(monkeypatch):
    monkeypatch.setenv("ORION_RELEASE_ORACLE", "0")
    f = settle_frame(2)
    r, out = _sink_reader()
    t = warm(r, f)
    release_and_settle(r, f, t, seq=9)
    assert out == []
    assert r.last_release_oracle is None


def test_the_default_sink_is_the_banner_verdict_s_own_stdout_writer(monkeypatch):
    """Framing is the banner's, byte for byte: one compact JSON object + '\n' written through
    remote_play_orchestrator.emit_stdout_jsonl, which holds the shared IPC lock. The module is
    taken from sys.modules so the reader never imports the orchestrator back."""
    import sys
    import types
    seen = []
    fake = types.ModuleType("remote_play_orchestrator")
    fake.emit_stdout_jsonl = seen.append
    monkeypatch.setitem(sys.modules, "remote_play_orchestrator", fake)
    f = settle_frame(2)
    r = reader()
    t = warm(r, f)
    release_and_settle(r, f, t, seq=5)
    assert len(seen) == 1, seen
    assert seen[0].startswith('{"event":"release_oracle"') and seen[0].endswith("}\n")
