"""[ORION_STALL_ATTRIB 2026-09-15] The two idle-time stalls the attributor named, at the source.

The attributor caught 58 consumer stalls in one session (p50 50 ms, max 497), every one of
them while the game sat in a menu or a loading screen, in exactly two places:

  * `CaptureCardReader` -> `_isolate_immutable_frame` -> `numpy.array_equal`. The ownership
    copy's verification compared the WHOLE 1080p frame, which costs 2.8 ms and allocates a
    6.2 MB temporary bool array per frame -- ~370 MB/s of churn into the generational GC at
    60 fps. The tear it exists to catch (a driver writing a new frame over the buffer we just
    copied) rewrites whole scanline bands, so a ROW-STRIDED sample sees it; the full compare
    survives as ORION_CAPTURE_VERIFY_STRIDE=1 / ORION_CAPTURE_VERIFY_FULL=1.

  * `meter-yolo` -> `meter_locator_cv._find` on frames that had not changed a byte. A static
    1080p menu frame costs 32 ms of band scan to re-derive an answer that cannot have changed,
    because the pixels did not change.

These tests pin the CONTRACTS of both fixes (the torn copy is still caught; a shot never
reuses anything) and, with the attributor's own synthetic rig, that the same consumer work
that stalls on the old path does not stall on the new one.
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
import time

import numpy as np
import pytest

import capture_card_backend as ccb
import meter_locator_cv as mlc
import stall_attributor as sa


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith(("ORION_CAPTURE_", "ORION_LOCATOR_", "ORION_CV_", "ORION_STALL_")):
            monkeypatch.delenv(key, raising=False)
    yield


def _frame_1080p(seed=3):
    rng = np.random.default_rng(seed)
    return np.ascontiguousarray(
        np.clip(rng.normal(60, 20, (1080, 1920, 3)), 0, 255).astype(np.uint8))


def _menu_frame():
    """A realistic IDLE frame: a dark menu with bright text rows and two tall white columns --
    the decor the band scan has to chew through to answer 'no meter here'."""
    rng = np.random.default_rng(7)
    f = np.clip(rng.normal(38, 6, (1080, 1920, 3)), 0, 255).astype(np.uint8)
    for y in range(180, 900, 60):
        f[y:y + 22, 300:1500] = 236
    for x in (777, 1420):
        f[420:560, x:x + 26] = 240
    return np.ascontiguousarray(f)


def _ms(fn, n=40):
    for _ in range(5):
        fn()
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(out)


# ================================================================== the ownership boundary
def test_the_torn_copy_is_still_caught_by_the_strided_sample(clean_env):
    """The contract the check exists for: a source that changed during the copy is refused."""
    src = _frame_1080p()
    real_array = ccb.np.array

    def _copy_then_overwrite(value, *args, **kwargs):
        out = real_array(value, *args, **kwargs)
        value[:] = (value.astype(np.uint16) + 7).astype(np.uint8)   # a new frame lands
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ccb.np, "array", _copy_then_overwrite)
        owned, reason = ccb._isolate_immutable_frame(src, verify_copy=True)
    assert owned is None and reason == "torn_copy"


def test_a_band_of_rows_rewritten_mid_copy_is_caught(clean_env):
    """A partial tear -- a DMA that got through part of the frame -- is a band of rows, and
    the default stride samples every 8th row, so any band of 8+ rows is seen."""
    src = _frame_1080p()
    real_array = ccb.np.array

    def _copy_then_tear(value, *args, **kwargs):
        out = real_array(value, *args, **kwargs)
        value[300:340] ^= np.uint8(0x55)
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ccb.np, "array", _copy_then_tear)
        owned, reason = ccb._isolate_immutable_frame(src, verify_copy=True)
    assert owned is None and reason == "torn_copy"


def test_the_sample_is_a_sample_and_the_strict_switch_buys_the_proof_back(clean_env):
    """HONEST LIMIT: a strided sample can PROVE a tear, never prove its absence. A one-pixel
    change between two sampled rows is invisible to it -- and ORION_CAPTURE_VERIFY_FULL is
    the switch that re-runs the full compare when the sample matched."""
    src = _frame_1080p()
    real_array = ccb.np.array

    def _copy_then_poke(value, *args, **kwargs):
        out = real_array(value, *args, **kwargs)
        value[301, 17, 1] ^= np.uint8(0xFF)      # row 301: not a multiple of 8
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ccb.np, "array", _copy_then_poke)
        owned, reason = ccb._isolate_immutable_frame(src, verify_copy=True)
        assert reason == "", "a single unsampled pixel is below the default sample's resolution"
        owned2, reason2 = ccb._isolate_immutable_frame(src, verify_copy=True, full=True)
        assert owned2 is None and reason2 == "torn_copy"
        owned3, reason3 = ccb._isolate_immutable_frame(src, verify_copy=True, stride=1)
        assert owned3 is None and reason3 == "torn_copy"
    assert owned is not None and not owned.flags["WRITEABLE"]


def test_the_owned_frame_contract_is_unchanged(clean_env):
    src = _frame_1080p()
    owned, reason = ccb._isolate_immutable_frame(src, verify_copy=True)
    assert reason == ""
    assert owned.flags["OWNDATA"] and owned.flags["C_CONTIGUOUS"]
    assert not owned.flags["WRITEABLE"]
    assert not np.shares_memory(src, owned)
    assert np.array_equal(src, owned)


def test_nothing_downstream_can_mutate_a_published_frame(clean_env):
    """WHO MUTATES: nobody, and nobody can -- which is why the copy is a BUFFER-LIFETIME guard
    and not a mutation guard, and why ORION_CAPTURE_ISOLATE_COPY exists but stays on."""
    owned, _ = ccb._isolate_immutable_frame(_frame_1080p(), verify_copy=True)
    with pytest.raises(ValueError):
        owned[0, 0, 0] = 1


def test_the_copy_can_be_skipped_only_on_purpose(clean_env, monkeypatch):
    src = _frame_1080p()
    monkeypatch.setattr(ccb, "_ISOLATE_COPY", False)
    owned, reason = ccb._isolate_immutable_frame(src, verify_copy=True)
    assert reason == "" and not owned.flags["WRITEABLE"]
    assert np.shares_memory(src, owned), "the point of the knob is that no copy is made"
    # and the shipped default still copies
    monkeypatch.setattr(ccb, "_ISOLATE_COPY", True)
    owned2, _ = ccb._isolate_immutable_frame(src, verify_copy=True)
    assert not np.shares_memory(src, owned2)


def test_the_shipped_defaults_are_the_cheap_ones(clean_env):
    assert ccb._VERIFY_STRIDE == 8
    assert ccb._VERIFY_FULL is False
    assert ccb._ISOLATE_COPY is True


def test_the_strided_verify_is_materially_cheaper_than_the_full_one(clean_env):
    """The measurement behind the change. The comparison itself is timed in isolation (the
    copy it rides on is ~2.3 ms and is NOT what this fix touches): 2.81 ms -> 0.29 ms per
    1080p frame on this machine. The gate is deliberately loose so it fails on a regression,
    not on machine noise."""
    a = _frame_1080p()
    b = np.array(a, copy=True)
    full = _ms(lambda: ccb._frame_sample_matches(a, b, 1))
    strided = _ms(lambda: ccb._frame_sample_matches(a, b, ccb._VERIFY_STRIDE))
    assert strided < 0.4 * full, f"strided={strided:.3f}ms full={full:.3f}ms"
    # ... and the boundary as a whole gets measurably cheaper, copy included.
    whole_full = _ms(lambda: ccb._isolate_immutable_frame(a, verify_copy=True, stride=1))
    whole_new = _ms(lambda: ccb._isolate_immutable_frame(a, verify_copy=True))
    assert whole_new < 0.85 * whole_full, f"new={whole_new:.3f}ms old={whole_full:.3f}ms"


# ================================================= the attributor's own rig, on the real work
def _run_consumer(attrib, work, bursts=6):
    """One fake consumer thread: note_callback(), then a BURST of frame work, repeatedly.
    Returns the STALL: lines the attributor produced."""
    def _consumer():
        for _ in range(bursts):
            attrib.note_callback()
            work()

    th = threading.Thread(target=_consumer, name="fake-capture-consumer", daemon=True)
    th.start()
    th.join(timeout=30.0)
    attrib.drain_once()


def test_the_burst_that_stalls_the_consumer_on_the_full_compare_does_not_on_the_strided_one(
        clean_env, monkeypatch, caplog):
    """The attributor's synthetic rig (see tests/test_stall_attributor.py) pointed at the REAL
    ownership boundary: a consumer that has to isolate a backlog of frames between callbacks.

    The burst size is derived from THIS machine's measured full-compare cost so the test is a
    ratio test, not a wall-clock one: n frames chosen to put the old path ~70 ms past a
    callback (well over the 40 ms stall threshold), then the same n on the new path.
    """
    monkeypatch.setenv("ORION_STALL_ATTRIB", "1")
    src = _frame_1080p()
    full_ms = _ms(lambda: ccb._isolate_immutable_frame(src, verify_copy=True, stride=1), n=20)
    new_ms = _ms(lambda: ccb._isolate_immutable_frame(src, verify_copy=True), n=20)
    if full_ms <= 0.0 or new_ms <= 0.0:
        pytest.skip("cannot time the ownership boundary on this machine")
    n = max(3, int(round(60.0 / full_ms)))
    # The stall threshold is placed BETWEEN the two measured bursts rather than at a fixed
    # wall-clock number, so this stays a RATIO test: a loaded machine slows both bursts and
    # the threshold with them. If the ratio is too small to place a threshold with margin --
    # a machine whose memcpy dominates, or one busy enough that the timings are meaningless --
    # the rig proves nothing and says so instead of flaking.
    gap_ms = 0.5 * n * (full_ms + new_ms)
    if not (n * new_ms * 1.3 < gap_ms < n * full_ms * 0.85):
        pytest.skip(f"no margin to place a stall threshold (full={full_ms:.2f}ms "
                    f"new={new_ms:.2f}ms n={n})")

    def _old():
        for _ in range(n):
            ccb._isolate_immutable_frame(src, verify_copy=True, stride=1)

    def _new():
        for _ in range(n):
            ccb._isolate_immutable_frame(src, verify_copy=True)

    def _stalls(work):
        attrib = sa.StallAttributor(sample_ms=2.0, gap_ms=gap_ms, ring_ms=2000.0, gc_ms=0.0,
                                    poll_ms=5.0, stats_s=0.0)
        attrib.start()
        try:
            with caplog.at_level(logging.WARNING, logger="stall_attrib"):
                caplog.clear()
                _run_consumer(attrib, work)
            return [r.getMessage() for r in caplog.records
                    if r.getMessage().startswith("STALL:")]
        finally:
            attrib.stop()

    old_lines = _stalls(_old)
    new_lines = _stalls(_new)
    assert old_lines, "the old path must actually stall the consumer, or the rig proves nothing"
    assert any("_isolate_immutable_frame" in ln or "array_equal" in ln for ln in old_lines), \
        old_lines
    assert not new_lines, new_lines


# ================================================================== the locator's idle scan
@pytest.fixture
def locator(clean_env, monkeypatch):
    """[SHIP CONFIG 2026-09-17] The idle reuse now ships OFF, so this suite arms it.

    The optimisation and its bounds are unchanged and these tests still own them; what moved is
    only the packaged default (it was in the OFF half of the graded launch line -- an idle-cost
    win with no live hours, and the 09-16 blind run was traced to an unrated short-circuit of
    exactly this kind). tests/test_ship_defaults.py owns the default itself.
    """
    monkeypatch.setenv("ORION_LOCATOR_IDLE_REUSE", "1")
    mlc._pa.ARM.reset()
    loc = mlc.MeterContourLocator()
    try:
        yield loc
    finally:
        mlc._pa.ARM.reset()


def test_a_static_idle_frame_is_scanned_at_most_four_times_a_second(locator):
    """200 frames = 3.33 s of a frozen menu. The bound is the 250 ms re-run, not the frame
    rate, so the scan runs ~14 times instead of 200."""
    f = _menu_frame()
    t = 1000.0
    for _ in range(200):
        locator.detect_box(f, ts=t)
        t += 1.0 / 60.0
    assert locator.stats["calls"] == 200
    assert locator.stats["idle_reuse"] >= 180
    real_scans = locator.stats["calls"] - locator.stats["idle_reuse"]
    assert real_scans <= 16, real_scans          # ceil(3.33 s / 0.25 s) + slack


def test_the_reuse_is_bounded_so_a_wedged_feed_cannot_hide_the_scan(locator):
    f = _menu_frame()
    locator.detect_box(f, ts=1000.0)
    locator.detect_box(f, ts=1000.0 + 0.24)
    assert locator.stats["idle_reuse"] == 1
    locator.detect_box(f, ts=1000.0 + 0.26)      # past ORION_LOCATOR_IDLE_REUSE_MS
    assert locator.stats["idle_reuse"] == 1


def test_an_armed_press_never_reuses_anything(locator):
    """A shot always pays for a real scan on real pixels. This is what makes the whole
    optimisation free: it cannot delay, stale or skip a meter the engine is waiting for."""
    f = _menu_frame()
    for k in range(6):
        locator.detect_box(f, ts=1000.0 + k / 60.0)
    assert locator.stats["idle_reuse"] >= 4
    mlc._pa.ARM.note_press(7, 1000.1, "Standstill")
    before = locator.stats["idle_reuse"]
    for k in range(6):
        locator.detect_box(f, ts=1000.1 + k / 60.0)
    assert locator.stats["idle_reuse"] == before


def test_a_frame_that_changed_is_never_served_a_cached_answer(locator):
    """The signature has to be finer than the smallest change that can matter: a meter grows
    ~4 px per frame, so every 4th row is sampled. A 5-row change must break the signature."""
    f = _menu_frame()
    locator.detect_box(f, ts=1000.0)
    g = f.copy()
    g[500:505, 900:926] = 250
    locator.detect_box(g, ts=1000.0 + 1.0 / 60.0)
    assert locator.stats["idle_reuse"] == 0


def test_reset_and_forget_position_drop_the_cached_answer(locator):
    f = _menu_frame()
    locator.detect_box(f, ts=1000.0)
    locator.reset()
    locator.detect_box(f, ts=1000.0 + 1.0 / 60.0)
    assert locator.stats["idle_reuse"] == 0
    locator.detect_box(f, ts=1000.0 + 2.0 / 60.0)
    assert locator.stats["idle_reuse"] == 1
    locator.forget_position()
    locator.detect_box(f, ts=1000.0 + 3.0 / 60.0)
    assert locator.stats["idle_reuse"] == 1


def test_the_ship_default_scans_every_frame_and_the_switch_arms_the_reuse(clean_env,
                                                                         monkeypatch):
    """[SHIP CONFIG 2026-09-17] Both directions, from the SHIP default (off).

    A packaged install re-runs the real scan on every idle frame -- the 2026-09-15 behaviour,
    byte for byte -- and ORION_LOCATOR_IDLE_REUSE=1 arms the optimisation the tests above pin.
    """
    monkeypatch.delenv("ORION_LOCATOR_IDLE_REUSE", raising=False)
    f = _menu_frame()
    for value, expect_reuse in ((None, False), ("0", False), ("1", True)):
        if value is None:
            monkeypatch.delenv("ORION_LOCATOR_IDLE_REUSE", raising=False)
        else:
            monkeypatch.setenv("ORION_LOCATOR_IDLE_REUSE", value)
        loc = mlc.MeterContourLocator()
        mlc._pa.ARM.reset()
        for k in range(8):
            loc.detect_box(f, ts=1000.0 + k / 60.0)
        if expect_reuse:
            assert loc.stats["idle_reuse"] > 0
        else:
            assert loc.stats["idle_reuse"] == 0
            assert loc.stats["full"] == 8


def test_the_idle_scan_of_a_frozen_frame_is_an_order_of_magnitude_cheaper(locator):
    """32 ms -> 0.7 ms per static 1080p frame on this machine (the signature is the whole
    remaining cost). The gate is loose so it fails on a regression, not on machine noise."""
    f = _menu_frame()
    locator.detect_box(f, ts=1000.0)
    t = 1000.0 + 1.0 / 60.0
    scan = []
    for _ in range(30):
        t0 = time.perf_counter()
        locator.detect_box(f, ts=t)
        scan.append((time.perf_counter() - t0) * 1000.0)
        t += 1.0 / 60.0
    cold = mlc.MeterContourLocator()
    cold_ms = _ms(lambda: cold._detect_scan(f, (0, 0), None, None, f), n=10)
    assert statistics.median(scan) < 0.25 * cold_ms, (statistics.median(scan), cold_ms)
