"""[ORION_STALL_ATTRIB 2026-09-15] Tests for the consumer-stall attributor.

Synthetic throughout: a fake consumer thread that deliberately stops calling back for 60 ms
while a second, NAMED thread burns the GIL through the gap. The attributor has to name that
thread and the function it was in -- naming "some thread" is what every previous guess at
this problem already did.

No capture card, no orchestrator, no sleep longer than the stall under test.
"""
from __future__ import annotations

import gc
import logging
import os
import threading
import time

import pytest

import stall_attributor as sa


@pytest.fixture
def clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("ORION_STALL_"):
            monkeypatch.delenv(key, raising=False)
    yield


@pytest.fixture
def attrib_on(clean_env, monkeypatch):
    """The knob DEFAULTS TO 0 (see stall_attributor's COST note), so every behaviour test
    has to turn it on explicitly -- which is also the gating regression test."""
    monkeypatch.setenv("ORION_STALL_ATTRIB", "1")
    yield


@pytest.fixture
def attrib(attrib_on):
    """A private attributor (never the process singleton) with a fast sampler."""
    a = sa.StallAttributor(sample_ms=2.0, gap_ms=40.0, ring_ms=2000.0, gc_ms=0.0,
                           poll_ms=5.0, stats_s=0.0)
    a.start()
    try:
        yield a
    finally:
        a.stop()


# ------------------------------------------------------------------ the hot path
def test_note_callback_is_inert_before_the_first_gap(attrib):
    attrib.note_callback()
    attrib.note_callback()
    assert not attrib._events
    assert attrib.stats()["callbacks"] == 2


def test_a_short_gap_is_not_a_stall(attrib):
    attrib.note_callback()
    time.sleep(0.005)
    attrib.note_callback()
    assert attrib.drain_once() == 0


# ------------------------------------------------------------------ attribution
def _hog_the_gil(stop_at):
    """A named function on a named thread, burning CPU without releasing the GIL for long."""
    x = 0
    while time.perf_counter() < stop_at:
        for _ in range(2000):
            x += 1
    return x


def test_a_synthetic_60ms_stall_names_the_thread_and_the_function(attrib, caplog):
    """THE test. One fake consumer callback, a 60 ms hole, and a named worker inside it."""
    stop_at = time.perf_counter() + 0.060
    hog = threading.Thread(target=_hog_the_gil, args=(stop_at,), name="gil-hog", daemon=True)

    consumed = []

    def _consumer():
        attrib.note_callback()          # the callback BEFORE the gap
        hog.start()
        hog.join()                      # ... 60 ms with no callback at all ...
        attrib.note_callback()          # the callback that closes it
        consumed.append(True)

    th = threading.Thread(target=_consumer, name="fake-consumer", daemon=True)
    with caplog.at_level(logging.WARNING, logger="stall_attrib"):
        th.start()
        th.join(timeout=5.0)
        assert consumed, "the fake consumer never ran"
        attrib.drain_once()

    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STALL:")]
    assert len(lines) == 1, lines
    line = lines[0]
    assert "gap_ms=" in line and "thread=" in line and "where=" in line
    gap = float(line.split("gap_ms=")[1].split()[0])
    assert 55.0 <= gap <= 400.0, line
    assert "thread=gil-hog" in line, line
    assert "_hog_the_gil" in line, line          # where= carries the function, not just a file
    assert "cause=in_process" in line, line      # the process was burning CPU, not starved


def test_the_stall_line_fits_the_native_relay_trim(attrib, caplog):
    """RemotePlaySession trims a relayed sidecar line to 300 chars and the logging prefix
    eats ~50 of them, so a line that runs long loses its own tail (the `Capture health` line
    has been losing its last ~120 characters since it was written). Keep this one short."""
    stop_at = time.perf_counter() + 0.060
    hog = threading.Thread(target=_hog_the_gil, args=(stop_at,), name="gil-hog", daemon=True)
    attrib.note_callback()
    hog.start()
    hog.join()
    attrib.note_callback()
    with caplog.at_level(logging.WARNING, logger="stall_attrib"):
        attrib.drain_once()
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STALL:")][0]
    assert len(line) <= 240, (len(line), line)


def test_a_stall_with_no_cpu_burned_is_reported_as_starved(attrib, caplog):
    """cause= is the whole point of recording process_time beside the wall clock: a gap the
    sidecar did not cause must not be attributed to the sidecar. Sleeping is the closest a
    test can get to being descheduled -- no CPU is consumed either way."""
    attrib.note_callback()
    time.sleep(0.080)
    attrib.note_callback()
    with caplog.at_level(logging.WARNING, logger="stall_attrib"):
        attrib.drain_once()
    line = [r.getMessage() for r in caplog.records if r.getMessage().startswith("STALL:")][0]
    assert "cause=starved" in line, line
    assert "cpu_frac=" in line and "cover=" in line


# ------------------------------------------------------------------ gc
def test_a_heavy_collection_logs_a_gc_line(attrib_on, caplog):
    a = sa.StallAttributor(sample_ms=2.0, gap_ms=40.0, ring_ms=500.0,
                           gc_ms=0.0, poll_ms=5.0, stats_s=0.0)
    a.start()
    try:
        # A real cyclic-garbage heap, so the collection has actual work to do.
        junk = []
        for _ in range(4000):
            d = {}
            d["self"] = d
            junk.append(d)
        del junk
        with caplog.at_level(logging.WARNING, logger="stall_attrib"):
            gc.collect(2)
            # The watchdog may take the queued gen-2 event before this thread's
            # explicit drain. Its log call then races our caplog snapshot, so
            # wait for the observable record rather than assuming one drain is
            # a synchronous logging barrier across both threads.
            deadline = time.perf_counter() + 1.0
            while time.perf_counter() < deadline:
                a.drain_once()
                if any("gen=2" in r.getMessage() for r in caplog.records
                       if r.getMessage().startswith("GC:")):
                    break
                time.sleep(0.005)
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("GC:")]
        assert lines, [r.getMessage() for r in caplog.records]
        # gc_ms=0.0 here, so incidental gen-0/1 sweeps log too; the gen-2 one is the subject.
        gen2 = [m for m in lines if "gen=2" in m]
        assert gen2, lines
        assert "ms=" in gen2[-1] and "collected=" in gen2[-1]
        assert a.stats()["gc2"] >= 1
    finally:
        a.stop()


def test_gc_threshold_keeps_fast_collections_quiet(attrib_on, caplog):
    a = sa.StallAttributor(sample_ms=2.0, gap_ms=40.0, ring_ms=500.0,
                           gc_ms=10_000.0, poll_ms=5.0, stats_s=0.0)
    a.start()
    try:
        with caplog.at_level(logging.WARNING, logger="stall_attrib"):
            gc.collect(2)
            a.drain_once()
        assert not [r for r in caplog.records if r.getMessage().startswith("GC:")]
        assert a.stats()["gc2"] >= 1          # still COUNTED for the 5 s line
    finally:
        a.stop()


def test_gc_callback_is_removed_on_stop(attrib_on):
    before = len(gc.callbacks)
    a = sa.StallAttributor(sample_ms=5.0, stats_s=0.0)
    a.start()
    assert len(gc.callbacks) == before + 1
    a.stop()
    assert len(gc.callbacks) == before


# ------------------------------------------------------------------ cost + gating
def test_sampler_cost_p99_is_inside_budget(attrib_on):
    """Per-sample cost: p99 must stay under 0.3 ms, and the CPU share is MEASURED here.

    Measured 2026-09-15 at the shipped 5 ms period with 19 live threads: p50 0.017 ms,
    p99 0.051-0.086 ms, mean ~0.022 ms -> 200 samples/s = ~0.45 % of one core. p99 passes the
    budget; the CPU share does NOT (0.2 % was the allowance), which is exactly why
    ORION_STALL_ATTRIB defaults to 0 -- see test_the_default_is_off_because_of_that_cost.
    The ceiling asserted here is a 3x regression guard, not the ship budget.
    """
    keep = [threading.Thread(target=lambda e=threading.Event(): e.wait(2.5),
                             name="noise-%d" % i, daemon=True) for i in range(6)]
    for t in keep:
        t.start()
    # LIVE CONDITIONS. _capture_loop raises the Windows timer resolution to 1 ms before the
    # attributor starts; without it Event.wait(0.005) rounds up to the ~15.6 ms system tick,
    # the sampler runs at ~65 Hz instead of 200 Hz and every wake finds cold caches -- which
    # measures a machine the sidecar never runs on, in BOTH directions at once.
    _winmm = None
    try:
        import ctypes
        _winmm = ctypes.windll.winmm
        _winmm.timeBeginPeriod(1)
    except Exception:
        _winmm = None
    a = sa.StallAttributor(sample_ms=5.0, stats_s=0.0)
    a.start()
    try:
        time.sleep(2.0)
        cost = a.sampler_cost()
    finally:
        a.stop()
        if _winmm is not None:
            try:
                _winmm.timeEndPeriod(1)
            except Exception:
                pass
    assert cost["n"] >= 100, cost
    assert cost["p99_ms"] < 0.3, cost
    assert cost["cpu_pct"] < 1.5, cost


def test_a_twenty_millisecond_period_is_inside_the_cpu_budget(attrib_on):
    """The always-on setting the module documents: ORION_STALL_SAMPLE_MS=20 measures ~0.12 %
    of a core, i.e. inside the 0.2 % ship budget, at the price of 2 samples per 40 ms gap."""
    a = sa.StallAttributor(sample_ms=20.0, stats_s=0.0)
    a.start()
    try:
        time.sleep(1.0)
        cost = a.sampler_cost()
    finally:
        a.stop()
    assert cost["n"] >= 20, cost
    assert cost["cpu_pct"] < 0.5, cost          # 3x regression guard around the ~0.12 % measured


def test_the_default_is_off_because_of_that_cost(clean_env):
    """THE GATING DECISION, pinned. The instrument is worth having and it is NOT free: at the
    5 ms period it costs roughly twice the 0.2 % of a core it was allowed. Anyone who flips
    this default has to change this test and re-measure first."""
    assert sa.enabled() is False


def test_the_switch_can_be_turned_on_without_a_restart(clean_env, monkeypatch):
    monkeypatch.setenv("ORION_STALL_ATTRIB", "1")
    assert sa.enabled() is True


def test_the_master_switch_keeps_every_thread_off(clean_env, monkeypatch):
    """Off means OFF: no sampler, no watchdog, no gc hook, and the hot path does not even
    read the clock -- note_callback() returns on a single latched-bool test."""
    monkeypatch.setenv("ORION_STALL_ATTRIB", "0")
    before = len(gc.callbacks)
    a = sa.StallAttributor(sample_ms=2.0, stats_s=0.0)
    a.start()
    try:
        assert a._sampler is None and a._watchdog is None
        assert len(gc.callbacks) == before
        a.note_callback()
        time.sleep(0.060)
        a.note_callback()
        assert not a._events                # nothing recorded at all
        assert a.stats()["callbacks"] == 0
        assert a.drain_once() == 0
    finally:
        a.stop()


def test_stats_carry_the_gen2_count_for_the_five_second_line(attrib_on):
    """remote_play_orchestrator reads exactly these keys into its 5 s `Capture health` line
    (gc2=/gcms=/stall=). The canonical home is the sidecar's own `preview_stats`, which owns
    callback_gap_ms; native_orion/** is out of scope for this change, so the keys are exposed
    here for a one-field addition there."""
    a = sa.StallAttributor(sample_ms=5.0, gc_ms=10_000.0, stats_s=0.0)
    a.start()
    try:
        n0 = a.stats()["gc2"]
        gc.collect(2)
        gc.collect(2)
        s = a.stats()
        assert s["gc2"] >= n0 + 2
        for key in ("gc2", "gc_worst_ms", "stalls", "worst_gap_ms",
                    "callbacks", "samples", "sampler_p99_ms", "sampler_cpu_pct"):
            assert key in s
    finally:
        a.stop()


def test_summary_line_reports_deltas_and_sampler_cost(attrib_on, caplog):
    a = sa.StallAttributor(sample_ms=2.0, gap_ms=40.0, gc_ms=10_000.0,
                           poll_ms=5.0, stats_s=0.0)
    a.start()
    try:
        a.note_callback()
        time.sleep(0.060)
        a.note_callback()
        a.drain_once()
        with caplog.at_level(logging.WARNING, logger="stall_attrib"):
            a.log_stats()
        line = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("STALL STATS:")][0]
        assert "stalls=1" in line, line
        assert "gc2=" in line and "sampler_p99_ms=" in line and "sampler_cpu_pct=" in line
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="stall_attrib"):
            a.log_stats()                       # a second window: the delta is back to zero
        line = [r.getMessage() for r in caplog.records
                if r.getMessage().startswith("STALL STATS:")][0]
        assert "stalls=0" in line, line
    finally:
        a.stop()


def test_the_log_level_defaults_to_error_so_the_relay_cannot_throttle_it(clean_env,
                                                                        monkeypatch, caplog):
    """Same relay, same lesson as the PICKUP: line -- sidecar WARNING lines share one global
    1000 ms slot (RemotePlaySession.h:530 kSidecarWarnThrottleMs) and stalls arrive in bursts."""
    assert sa._level() == logging.ERROR
    monkeypatch.setenv("ORION_STALL_LOG_LEVEL", "warning")
    assert sa._level() == logging.WARNING


def test_the_module_singleton_is_one_object(clean_env):
    assert sa.get() is sa.get()
    sa.note_callback()                          # never raises, even unstarted
