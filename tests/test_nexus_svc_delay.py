"""Contract tests for the nexus_svc inbound meter-delay intercept path.

Everything here runs against a fake WinDivert handle — no driver, no elevation,
no real packets.  The properties under test are the ones that make a 165ms
console delay safe to ship:

  * ordering is never violated, in either direction of a delay change;
  * the zero-delay fast path can never overtake a non-empty buffer;
  * the buffer is capped with a no-drop overflow policy;
  * the delay cannot get stuck on (dead-man switch + starvation watchdog);
  * out-of-range / garbage delays are clamped or rejected;
  * unknown verbs are answered with an error instead of silence;
  * the hello/status handshake advertises v3 + the meter_delay feature.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

import nexus_svc


CONSOLE_IP = "192.168.137.100"
COURT_IP = "104.255.104.55"


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _FakePacket:
    """Stands in for a pydivert Packet; only identity/ordering matters here."""

    __slots__ = ("seq",)

    def __init__(self, seq: int) -> None:
        self.seq = seq

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<pkt {self.seq}>"


class _FakeHandle:
    """Records the order and wall-clock time of every re-injection."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.send_times: list[float] = []
        self.closed = False
        self.parked = False
        self._lock = threading.Lock()
        self._inbox: list[_FakePacket] = []
        self._inbox_cv = threading.Condition()

    # -- send side (what the delay buffer calls) --------------------------- #
    def send(self, pkt) -> None:
        if self.closed:
            raise OSError("handle closed")
        with self._lock:
            self.sent.append(pkt.seq)
            self.send_times.append(time.perf_counter())

    # -- recv side (feeds the capture thread) ------------------------------ #
    def feed(self, pkt) -> None:
        with self._inbox_cv:
            self._inbox.append(pkt)
            self._inbox_cv.notify_all()

    def recv(self):
        with self._inbox_cv:
            while not self._inbox and not self.closed and not self.parked:
                self._inbox_cv.wait(0.05)
            if self.closed:
                raise OSError("handle closed")
            if not self._inbox:
                return None          # parked: lets the capture loop re-check stop
            return self._inbox.pop(0)

    def park(self, value: bool = True) -> None:
        """Make recv() return None so the capture thread can retire cleanly."""
        with self._inbox_cv:
            self.parked = value
            self._inbox_cv.notify_all()

    def close(self) -> None:
        self.closed = True
        with self._inbox_cv:
            self._inbox_cv.notify_all()

    def snapshot(self) -> list[int]:
        with self._lock:
            return list(self.sent)


class _FakeWinDivertLoop:
    def __init__(self, console_ip: str = CONSOLE_IP, court_ip: str = COURT_IP) -> None:
        self._console_ip = console_ip
        self._court_ip = court_ip
        self.meter_status_provider = None

    def set_console_ip(self, value):
        self._console_ip = value

    def clear_console_ip(self):
        self._console_ip = ""

    def set_court_ip(self, value):
        self._court_ip = value

    def current_ips(self):
        return self._console_ip, self._court_ip

    def set_meter_status_provider(self, fn):
        self.meter_status_provider = fn


@pytest.fixture
def fake_handle(monkeypatch):
    handle = _FakeHandle()
    monkeypatch.setattr(nexus_svc, "_open_intercept_handle",
                        lambda filter_str: handle)
    return handle


@pytest.fixture
def buf(fake_handle):
    """A started intercept buffer with its background threads suppressed.

    Threads are stopped so the tests drive ``enqueue``/``flush_due``/
    ``watchdog_check`` deterministically; the threads themselves are exercised
    by the end-to-end tests further down.

    The slew limiter is DISABLED here (``slew_ms_per_s=0``) so ``set_delay``
    applies synchronously and these tests can keep asserting on ordering,
    clamping and the fail-safes without threading a ramp through every case.
    The limiter itself is the subject of its own section at the end of the file;
    keeping the two concerns apart is what stops a limiter regression from
    hiding behind an ordering assertion.
    """
    b = nexus_svc._InboundDelayBuffer(slew_ms_per_s=0.0)
    ok, err = b.start(CONSOLE_IP, COURT_IP)
    assert ok, err
    _quiesce_threads(b)
    yield b
    b._running = False


@pytest.fixture
def slew_buf(fake_handle):
    """A started buffer with the shipping slew cap and threads suppressed."""
    b = nexus_svc._InboundDelayBuffer()
    ok, err = b.start(CONSOLE_IP, COURT_IP)
    assert ok, err
    _quiesce_threads(b)
    yield b
    b._running = False


def _quiesce_threads(b: nexus_svc._InboundDelayBuffer) -> None:
    """Retire the capture/flush/watchdog threads without closing the handle."""
    b._stop.set()
    handle = b._handle
    if isinstance(handle, _FakeHandle):
        handle.park(True)
    for t in b._threads:
        t.join(timeout=1.0)
        assert not t.is_alive(), f"{t.name} did not retire"
    b._threads = []
    if isinstance(handle, _FakeHandle):
        handle.park(False)
    b._stop = threading.Event()  # fresh, unset, so nothing else short-circuits


def _authed_handler(hub, wd_loop, delay_buf, monkeypatch):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "test-token")
    handler = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub,
                                       wd_loop, delay_buf)
    handler._handle_cmd({"cmd": "auth", "token": "test-token"})
    _drain(handler)
    return handler


def _drain(handler) -> list[dict]:
    out = []
    while True:
        try:
            out.append(json.loads(handler._q.get_nowait().decode("utf-8")))
        except Exception:
            return out


def _last(handler) -> dict:
    msgs = _drain(handler)
    assert msgs, "expected a response"
    return msgs[-1]


# --------------------------------------------------------------------------- #
# Filter derivation
# --------------------------------------------------------------------------- #
def test_intercept_filter_is_inbound_game_udp_only():
    filt = nexus_svc._build_intercept_filter(CONSOLE_IP, COURT_IP)

    # UDP only: RTT probes are ICMP (ping3) or TCP:80 and can never match.
    assert filt.startswith("ip and udp")
    assert "tcp" not in filt
    assert "icmp" not in filt.lower()
    # Direction is expressed as an address pair, never the inbound/outbound
    # keywords -- NETWORK_FORWARD reports everything as outbound.
    assert "inbound" not in filt and "outbound" not in filt
    # Source pinned to the public court server, destination pinned to the
    # console: Remote Play (console LAN <-> PC LAN) cannot match either clause.
    assert f"ip.SrcAddr == {COURT_IP}" in filt
    assert f"ip.DstAddr == {CONSOLE_IP}" in filt
    assert "30000" in filt and "30020" in filt


def test_intercept_refuses_a_private_court_ip(monkeypatch):
    """A LAN 'court' IP would let the Remote Play stream be delayed."""
    opened = []
    monkeypatch.setattr(nexus_svc, "_open_intercept_handle",
                        lambda f: opened.append(f) or _FakeHandle())
    b = nexus_svc._InboundDelayBuffer()

    ok, err = b.start(CONSOLE_IP, "192.168.137.1")
    assert not ok
    assert err == "intercept_requires_ips"
    assert opened == []
    assert not b.active


def test_sniff_filter_restart_does_not_touch_the_intercept_handle(fake_handle):
    """Bug F: set_console_ip force-closes the SNIFF handle only."""
    hub = nexus_svc._BroadcastHub()
    wd = nexus_svc._WinDivertLoop(hub, threading.Event())

    class _SniffHandle:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    sniff = _SniffHandle()
    wd._sniff_handle = sniff

    b = nexus_svc._InboundDelayBuffer()
    assert b.start(CONSOLE_IP, COURT_IP)[0]
    _quiesce_threads(b)

    wd.set_console_ip("192.168.137.55")

    assert sniff.closed is True          # sniff handle recycled
    assert wd._sniff_handle is None
    assert b.active is True              # intercept untouched
    assert fake_handle.closed is False
    b._running = False


# --------------------------------------------------------------------------- #
# Ordering
# --------------------------------------------------------------------------- #
def test_order_preserved_across_a_delay_increase(buf, fake_handle):
    now = time.perf_counter()

    buf.set_delay(50.0)
    for seq in range(3):
        buf.enqueue(_FakePacket(seq))
    # Delay jumps up mid-flow; the already-buffered packets keep their earlier
    # deadlines and the new ones must still land behind them.
    buf.set_delay(200.0)
    for seq in range(3, 6):
        buf.enqueue(_FakePacket(seq))

    releases = [entry[0] for entry in buf._buffer]
    assert releases == sorted(releases), "release times must stay monotonic"

    buf.flush_due(now + 10.0)
    assert fake_handle.snapshot() == [0, 1, 2, 3, 4, 5]


def test_order_preserved_across_a_delay_decrease(buf, fake_handle):
    buf.set_delay(200.0)
    for seq in range(5):
        buf.enqueue(_FakePacket(seq))
    buf.set_delay(40.0)
    for seq in range(5, 8):
        buf.enqueue(_FakePacket(seq))
    buf.set_delay(0.0)
    for seq in range(8, 11):
        buf.enqueue(_FakePacket(seq))

    releases = [entry[0] for entry in buf._buffer]
    assert releases == sorted(releases)

    buf.flush_due(time.perf_counter() + 10.0)
    assert fake_handle.snapshot() == list(range(11))


def test_zero_delay_fast_path_does_not_overtake_a_non_empty_buffer(buf, fake_handle):
    """Bug A: the reference design sent immediately whenever delay == 0."""
    buf.set_delay(165.0)
    buf.enqueue(_FakePacket(0))
    buf.enqueue(_FakePacket(1))
    assert fake_handle.snapshot() == [], "delayed packets must not be sent yet"

    # Delay collapses to zero while packets are still held.
    buf.set_delay(0.0)
    buf.enqueue(_FakePacket(2))

    # Packet 2 must NOT have jumped the queue.
    assert fake_handle.snapshot() == []
    assert buf.buffer_depth == 3

    buf.flush_due(time.perf_counter() + 10.0)
    assert fake_handle.snapshot() == [0, 1, 2]


def test_zero_delay_fast_path_is_used_when_buffer_is_empty(buf, fake_handle):
    buf.set_delay(0.0)
    buf.enqueue(_FakePacket(7))
    assert fake_handle.snapshot() == [7]
    assert buf.buffer_depth == 0
    assert buf.stats()["passed"] == 1


def test_delay_drop_to_zero_drains_paced_not_bursted(buf, fake_handle):
    """Bug B: set_delay(0) used to dump the whole queue in one shot."""
    buf.set_delay(165.0)
    for seq in range(10):
        buf.enqueue(_FakePacket(seq))

    t0 = time.perf_counter()
    buf.set_delay(0.0)

    releases = [entry[0] for entry in buf._buffer]
    assert releases == sorted(releases)
    # First one goes immediately, the rest are spaced by at least the drain
    # spacing so the console sees a paced catch-up rather than a microburst.
    spacings = [b - a for a, b in zip(releases, releases[1:])]
    assert all(s >= nexus_svc._METER_DRAIN_MIN_SPACING_S * 0.99 for s in spacings)
    span = releases[-1] - releases[0]
    assert span >= 9 * nexus_svc._METER_DRAIN_MIN_SPACING_S * 0.99
    # ...but the whole backlog still clears far faster than the 165ms it would
    # have taken at the old delay.
    assert releases[-1] - t0 < 0.100

    # Nothing has actually gone out yet at t0.
    assert fake_handle.snapshot() == []
    buf.flush_due(releases[0])
    assert fake_handle.snapshot() == [0]
    buf.flush_due(releases[-1])
    assert fake_handle.snapshot() == list(range(10))


# --------------------------------------------------------------------------- #
# Buffer cap
# --------------------------------------------------------------------------- #
def test_buffer_cap_is_enforced_by_early_release_never_by_dropping(buf, fake_handle,
                                                                   monkeypatch):
    monkeypatch.setattr(nexus_svc, "_METER_BUFFER_MAX_PACKETS", 8)
    buf.set_delay(300.0)

    for seq in range(20):
        buf.enqueue(_FakePacket(seq))

    assert buf.buffer_depth == 8, "buffer must be hard-capped"
    early = fake_handle.snapshot()
    assert early == list(range(12)), "oldest packets released early, in order"
    assert buf.stats()["overflow_released"] == 12

    buf.flush_due(time.perf_counter() + 10.0)
    # No packet was ever dropped and ordering survived the overflow.
    assert fake_handle.snapshot() == list(range(20))


def test_teardown_packet_does_not_jump_the_buffered_backlog(buf, fake_handle):
    """A packet received after stop_evt is set must not overtake the backlog.

    ``_capture_loop`` used to re-inject it immediately, which put it ahead of
    everything ``stop()`` was still holding.
    """
    buf.set_delay(300.0)
    for seq in range(5):
        buf.enqueue(_FakePacket(seq))

    # Exactly what _capture_loop does once stop_evt is set.
    buf._stop.set()
    late = _FakePacket(99)
    with buf._lock:
        if buf._buffer:
            buf._buffer.append((buf._buffer[-1][0], time.perf_counter(), late))
            buf._depth = len(buf._buffer)
        else:
            buf._send_locked(late)

    assert fake_handle.snapshot() == [], "nothing may go out ahead of the backlog"
    buf.flush_due(time.perf_counter() + 10.0)
    assert fake_handle.snapshot() == [0, 1, 2, 3, 4, 99]


def test_capture_loop_teardown_preserves_order_end_to_end(fake_handle):
    b = nexus_svc._InboundDelayBuffer()
    assert b.start(CONSOLE_IP, COURT_IP)[0]
    try:
        b.set_delay(300.0)
        for seq in range(5):
            b.enqueue(_FakePacket(seq))
            b.set_delay(300.0)
        # Race the capture thread's stop branch against the stop() drain.
        b._stop.set()
        fake_handle.feed(_FakePacket(99))
        time.sleep(0.05)
    finally:
        b.stop("test")
    sent = fake_handle.snapshot()
    assert sent == sorted(sent), f"teardown reordered the stream: {sent}"
    assert sent[:5] == [0, 1, 2, 3, 4]


# --------------------------------------------------------------------------- #
# Concurrent start
# --------------------------------------------------------------------------- #
def test_concurrent_start_never_opens_two_intercept_handles(monkeypatch):
    """An orphaned non-SNIFF handle blackholes the whole game flow.

    ``start()`` opens the WinDivert handle outside the lock, so two clients
    racing ``start_meter_intercept`` used to open two intercepting handles and
    leak one with no thread left to re-inject from it.
    """
    opened: list[_FakeHandle] = []
    gate = threading.Event()

    def slow_open(_filter):
        handle = _FakeHandle()
        opened.append(handle)
        gate.wait(2.0)
        return handle

    monkeypatch.setattr(nexus_svc, "_open_intercept_handle", slow_open)
    b = nexus_svc._InboundDelayBuffer()
    results: list[tuple[bool, str]] = []
    lock = threading.Lock()

    def go():
        r = b.start(CONSOLE_IP, COURT_IP)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    gate.set()
    for t in threads:
        t.join(timeout=3.0)
        assert not t.is_alive()

    assert len(opened) == 1, f"opened {len(opened)} intercept handles"
    assert sum(1 for ok, _ in results if ok) == 1
    assert len(b._threads) == 3, "duplicate capture/flush/watchdog thread sets"
    b.stop("test")
    assert all(h.closed for h in opened), "an intercept handle was leaked"


def test_buffer_cap_default_is_bounded():
    assert 0 < nexus_svc._METER_BUFFER_MAX_PACKETS <= 4096


# --------------------------------------------------------------------------- #
# Clamping / validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("requested,expected", [
    (-50.0, 0.0),
    (0.0, 0.0),
    (55.0, 55.0),          # ramp intermediate, below the manual floor
    (165.0, 165.0),
    (250.0, 250.0),
    (300.0, 300.0),
    (301.0, 301.0),
    (600.0, 600.0),
    (601.0, 600.0),
    (100000.0, 600.0),
    ("165", 165.0),
])
def test_delay_is_clamped_to_the_hard_safety_range(buf, requested, expected):
    assert buf.set_delay(requested) == expected
    assert buf.current_delay_ms == expected


@pytest.mark.parametrize("bad", [None, "abc", float("nan"), float("inf"),
                                 float("-inf"), [], {}])
def test_non_numeric_delay_is_rejected(buf, bad):
    with pytest.raises(ValueError):
        buf.set_delay(bad)


def test_set_meter_delay_command_clamps_and_acks(monkeypatch, fake_handle):
    hub = nexus_svc._BroadcastHub()
    wd = _FakeWinDivertLoop()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = _authed_handler(hub, wd, delay, monkeypatch)

    handler._handle_cmd({"cmd": "start_meter_intercept"})
    assert _last(handler)["event"] == "ack"
    _quiesce_threads(delay)

    handler._handle_cmd({"cmd": "set_meter_delay", "delay_ms": 5000.0})
    ack = _last(handler)
    assert ack["event"] == "ack"
    # `target_ms` is the accepted TARGET (clamped); `meter_delay_ms` is what is
    # applied right now, which is still 0 because the service has not slewed yet.
    # A client that conflates the two would misreport the delay condition.
    assert ack["target_ms"] == nexus_svc._METER_DELAY_HARD_MAX_MS
    assert ack["meter_delay_ms"] == 0.0
    assert ack["meter_delay_settled"] is False
    assert ack["slew_cap_ms_per_s"] == nexus_svc._METER_MAX_SLEW_MS_PER_S
    assert ack["meter_delay_active"] is True

    handler._handle_cmd({"cmd": "set_meter_delay", "delay_ms": "banana"})
    assert _last(handler) == {"event": "error", "msg": "invalid_delay_ms"}

    delay._running = False


def test_set_meter_delay_before_start_is_refused(monkeypatch):
    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = _authed_handler(hub, _FakeWinDivertLoop(), delay, monkeypatch)

    handler._handle_cmd({"cmd": "set_meter_delay", "delay_ms": 165.0})
    err = _last(handler)
    assert err["event"] == "error"
    assert err["msg"] == "intercept_not_running"
    assert delay.current_delay_ms == 0.0


def test_start_requires_both_ips(monkeypatch):
    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    wd = _FakeWinDivertLoop(console_ip="", court_ip="")
    handler = _authed_handler(hub, wd, delay, monkeypatch)

    handler._handle_cmd({"cmd": "start_meter_intercept"})
    err = _last(handler)
    assert err["event"] == "error"
    assert err["msg"] == "intercept_requires_ips"
    assert not delay.active


def test_start_always_comes_up_at_zero_delay(fake_handle):
    """Opening the handle must never implicitly re-apply a stale delay."""
    b = nexus_svc._InboundDelayBuffer()
    assert b.start(CONSOLE_IP, COURT_IP)[0]
    _quiesce_threads(b)
    b.set_delay(165.0)
    b.stop("test")
    assert b.current_delay_ms == 0.0

    # Simulate any path that could leave a residual value behind while stopped.
    b._delay_ms = 165.0
    handle2 = _FakeHandle()
    saved = nexus_svc._open_intercept_handle
    try:
        nexus_svc._open_intercept_handle = lambda f: handle2
        assert b.start(CONSOLE_IP, COURT_IP)[0]
        _quiesce_threads(b)
        assert b.current_delay_ms == 0.0, "stale delay carried into a new handle"
        assert b.snapshot()["meter_delay_ms"] == 0.0
    finally:
        nexus_svc._open_intercept_handle = saved
        b._running = False


# --------------------------------------------------------------------------- #
# Dead-man switch
# --------------------------------------------------------------------------- #
def test_dead_man_switch_zeroes_and_flushes_when_last_client_disconnects(
        monkeypatch, fake_handle):
    hub = nexus_svc._BroadcastHub()
    wd = _FakeWinDivertLoop()
    # Limiter off: this test is about the dead-man switch releasing a buffer
    # that is already full, not about how the delay got there.
    delay = nexus_svc._InboundDelayBuffer(hub, slew_ms_per_s=0.0)
    handler = _authed_handler(hub, wd, delay, monkeypatch)

    handler._handle_cmd({"cmd": "start_meter_intercept"})
    assert _last(handler)["event"] == "ack"
    _quiesce_threads(delay)
    handler._handle_cmd({"cmd": "set_meter_delay", "delay_ms": 165.0})
    assert _last(handler)["event"] == "ack"

    for seq in range(4):
        delay.enqueue(_FakePacket(seq))
    assert delay.buffer_depth == 4
    assert fake_handle.snapshot() == []

    # Orion dies / NetworkBridge drops -> the client is unregistered.
    hub.remove_client(handler._q)
    handler._on_disconnect()

    assert delay.active is False
    assert delay.current_delay_ms == 0.0
    assert fake_handle.snapshot() == [0, 1, 2, 3], "buffer flushed in order"
    assert fake_handle.closed is True
    assert delay.stats()["dead_man_trips"] == 1


def test_dead_man_does_not_fire_while_another_client_is_authenticated(
        monkeypatch, fake_handle):
    hub = nexus_svc._BroadcastHub()
    wd = _FakeWinDivertLoop()
    delay = nexus_svc._InboundDelayBuffer(hub)
    first = _authed_handler(hub, wd, delay, monkeypatch)
    second = _authed_handler(hub, wd, delay, monkeypatch)

    first._handle_cmd({"cmd": "start_meter_intercept"})
    assert _last(first)["event"] == "ack"
    _quiesce_threads(delay)

    hub.remove_client(first._q)
    first._on_disconnect()

    assert hub.authed_client_count() == 1
    assert delay.active is True

    hub.remove_client(second._q)
    second._on_disconnect()
    assert delay.active is False


def test_unauthenticated_client_is_not_counted(monkeypatch):
    hub = nexus_svc._BroadcastHub()
    authed = threading.Event()
    hub.add_client(authed)
    hub.add_client(threading.Event())
    assert hub.authed_client_count() == 0
    authed.set()
    assert hub.authed_client_count() == 1


# --------------------------------------------------------------------------- #
# Watchdog
# --------------------------------------------------------------------------- #
def test_watchdog_zeroes_delay_on_command_starvation(buf, fake_handle):
    buf.set_delay(165.0)
    for seq in range(3):
        buf.enqueue(_FakePacket(seq))

    now = time.perf_counter()
    # Still being re-asserted -> no trip.
    assert buf.watchdog_check(now + nexus_svc._METER_WATCHDOG_TIMEOUT_S * 0.5) is False
    assert buf.current_delay_ms == 165.0

    starved_at = now + nexus_svc._METER_WATCHDOG_TIMEOUT_S + 0.05
    assert buf.watchdog_check(starved_at) is True
    assert buf.current_delay_ms == 0.0
    assert buf.stats()["watchdog_trips"] == 1

    # The backlog is re-timed for an immediate ordered drain, not dropped.
    buf.flush_due(time.perf_counter() + 10.0)
    assert fake_handle.snapshot() == [0, 1, 2]


def test_watchdog_is_reset_by_each_set_meter_delay(buf):
    buf.set_delay(165.0)
    base = time.perf_counter()
    for _ in range(6):
        buf.set_delay(165.0)          # controller keepalive at ~100ms cadence
        assert buf.watchdog_check(time.perf_counter() + 0.1) is False
    assert buf.current_delay_ms == 165.0
    assert buf.stats()["watchdog_trips"] == 0
    assert time.perf_counter() >= base


def test_watchdog_ignores_a_zero_delay(buf):
    buf.set_delay(0.0)
    assert buf.watchdog_check(time.perf_counter() + 60.0) is False
    assert buf.stats()["watchdog_trips"] == 0


def test_watchdog_thread_fires_end_to_end(fake_handle):
    """A commanded delay that stops being re-asserted must collapse to zero.

    Under the slew model a standing TARGET is just as dangerous as an applied
    delay -- it is a commitment to hold packets that a dead controller can no
    longer withdraw -- so the watchdog arms on either, and must clear both.
    """
    b = nexus_svc._InboundDelayBuffer()
    assert b.start(CONSOLE_IP, COURT_IP)[0]
    try:
        b.set_delay(165.0)
        # One command, then silence. The watchdog timeout is 500ms.
        deadline = time.perf_counter() + 3.0
        while b.stats()["watchdog_trips"] < 1 and time.perf_counter() < deadline:
            time.sleep(0.02)
        assert b.stats()["watchdog_trips"] >= 1, "watchdog thread never fired"
        assert b.current_delay_ms == 0.0
        assert b.target_delay_ms == 0.0, "watchdog left the target standing"
    finally:
        b.stop("test")


def test_watchdog_arms_on_a_target_that_has_not_slewed_in_yet(slew_buf):
    """The gap the target/applied split opens, closed explicitly.

    Right after set_delay the applied delay is still 0 while the target is 165.
    A watchdog that only looked at the applied value would treat that window as
    'nothing armed' and let a dead controller's commitment stand.
    """
    slew_buf.set_delay(165.0)
    assert slew_buf.current_delay_ms == 0.0
    assert slew_buf.target_delay_ms == 165.0
    assert slew_buf.watchdog_check(time.perf_counter() + 60.0) is True
    assert slew_buf.target_delay_ms == 0.0


# --------------------------------------------------------------------------- #
# Command surface / version negotiation
# --------------------------------------------------------------------------- #
def test_unknown_verb_now_returns_an_error(monkeypatch):
    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = _authed_handler(hub, _FakeWinDivertLoop(), delay, monkeypatch)

    handler._handle_cmd({"cmd": "definitely_not_a_verb"})
    err = _last(handler)
    assert err == {"event": "error", "msg": "unknown_command",
                   "cmd": "definitely_not_a_verb"}


def test_known_verbs_are_not_reported_as_unknown(monkeypatch, fake_handle):
    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = _authed_handler(hub, _FakeWinDivertLoop(), delay, monkeypatch)
    try:
        for verb in ("ping", "status", "clear_filter", "start_meter_intercept",
                     "set_meter_delay", "stop_meter_intercept"):
            handler._handle_cmd({"cmd": verb, "delay_ms": 165.0})
            reply = _last(handler)
            assert reply.get("msg") != "unknown_command", verb
            if verb == "start_meter_intercept":
                _quiesce_threads(delay)
    finally:
        delay._running = False


def test_version_negotiation_advertises_v3_and_meter_delay(monkeypatch, fake_handle):
    assert nexus_svc.SVC_VERSION == 3
    # SVC_FEATURES is now populated by _run_service() only when the intercept is
    # ARMED, so a client can distinguish "this bridge will not delay packets"
    # from "this bridge is too old to know the verb". Simulate an armed bridge.
    monkeypatch.setattr(nexus_svc, "SVC_FEATURES", ("meter_delay",))
    monkeypatch.setattr(nexus_svc, "_METER_ARMED", True)
    assert "meter_delay" in nexus_svc.SVC_FEATURES

    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = _authed_handler(hub, _FakeWinDivertLoop(), delay, monkeypatch)

    handler._send({"event": "hello", "version": nexus_svc.SVC_VERSION,
                   "features": list(nexus_svc.SVC_FEATURES)})
    hello = _last(handler)
    assert hello["version"] == 3
    assert "meter_delay" in hello["features"]

    handler._handle_cmd({"cmd": "status"})
    status = _last(handler)
    assert status["event"] == "status"
    assert status["version"] == 3
    assert "meter_delay" in status["features"]
    assert status["meter_delay_active"] is False
    assert status["meter_delay_ms"] == 0.0
    assert status["meter_buffer_depth"] == 0


def test_meter_commands_require_authentication(monkeypatch, fake_handle):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "test-token")
    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub,
                                       _FakeWinDivertLoop(), delay)

    for verb in ("start_meter_intercept", "set_meter_delay",
                 "stop_meter_intercept"):
        handler._handle_cmd({"cmd": verb, "delay_ms": 165.0})
        rejection = _last(handler)
        # The unauthorized payload gained diagnostic fields (reason/token_path/
        # token_mode) in the F1 hardening after this test was written; assert the
        # contract rather than the exact shape.
        assert rejection["event"] == "error"
        assert rejection["msg"] == "unauthorized"
        assert rejection["reason"] == "auth_required"
    assert not delay.active


def test_stop_meter_intercept_flushes_and_closes(monkeypatch, fake_handle):
    hub = nexus_svc._BroadcastHub()
    delay = nexus_svc._InboundDelayBuffer(hub)
    handler = _authed_handler(hub, _FakeWinDivertLoop(), delay, monkeypatch)

    handler._handle_cmd({"cmd": "start_meter_intercept"})
    assert _last(handler)["event"] == "ack"
    _quiesce_threads(delay)
    handler._handle_cmd({"cmd": "set_meter_delay", "delay_ms": 200.0})
    _drain(handler)
    for seq in range(3):
        delay.enqueue(_FakePacket(seq))

    handler._handle_cmd({"cmd": "stop_meter_intercept"})
    ack = _last(handler)
    assert ack["event"] == "ack"
    assert ack["was_active"] is True
    assert ack["meter_delay_active"] is False
    assert fake_handle.snapshot() == [0, 1, 2]
    assert fake_handle.closed is True

    handler._handle_cmd({"cmd": "stop_meter_intercept"})
    assert _last(handler)["was_active"] is False


def test_meter_status_is_attached_to_packet_telemetry(fake_handle):
    hub = nexus_svc._BroadcastHub()
    auth = threading.Event()
    auth.set()
    q = hub.add_client(auth)

    # Limiter off so the telemetry assertion can name an exact applied value.
    delay = nexus_svc._InboundDelayBuffer(hub, slew_ms_per_s=0.0)
    wd = nexus_svc._WinDivertLoop(hub, threading.Event())
    wd.set_meter_status_provider(delay.snapshot)

    class _Pkt:
        protocol = "UDP"
        src_addr = COURT_IP
        dst_addr = CONSOLE_IP
        src_port = 30001
        dst_port = 51000
        raw = b"x" * 40
        is_outbound = False

    assert delay.start(CONSOLE_IP, COURT_IP)[0]
    _quiesce_threads(delay)
    delay.set_delay(165.0)
    delay.enqueue(_FakePacket(0))

    wd._dispatch(_Pkt())
    while True:
        msg = json.loads(q.get_nowait().decode("utf-8"))
        if msg.get("event") == "packet":
            break
    assert msg["meter_delay_active"] is True
    assert msg["meter_delay_ms"] == 165.0
    assert msg["meter_buffer_depth"] == 1
    delay._running = False


def test_meter_delay_state_broadcast_carries_the_client_echo_contract(fake_handle):
    """Pin the {"event":"meter_delay"} state-broadcast shape the Orion client eats.

    NetworkBridge::maybeEmitMeterDelayEcho (restored 2026-08-08) parses exactly
    meter_delay_active (bool) / meter_delay_ms (number) / meter_buffer_depth (int)
    out of this broadcast and out of every delay-verb ack, and the Meter Delay
    card displays the result as the service's own applied-delay statement.
    Renaming or retyping any of these keys silently kills that echo — the card
    would fall back to controller book-keeping without any test failing — so the
    contract is pinned here on the producing side too.
    """
    hub = nexus_svc._BroadcastHub()
    auth = threading.Event()
    auth.set()
    q = hub.add_client(auth)

    delay = nexus_svc._InboundDelayBuffer(hub, slew_ms_per_s=0.0)
    assert delay.start(CONSOLE_IP, COURT_IP)[0]
    _quiesce_threads(delay)

    msg = None
    while True:
        try:
            m = json.loads(q.get_nowait().decode("utf-8"))
        except Exception:
            break
        if m.get("event") == "meter_delay":
            msg = m
    assert msg is not None, "start() must broadcast a meter_delay state event"
    assert msg["state"] == "started"
    assert msg["meter_delay_active"] is True
    assert isinstance(msg["meter_delay_ms"], (int, float))
    assert 0.0 <= float(msg["meter_delay_ms"]) <= 300.0
    assert isinstance(msg["meter_buffer_depth"], int)
    assert msg["meter_buffer_depth"] >= 0
    delay._running = False


# --------------------------------------------------------------------------- #
# End-to-end through the real threads
# --------------------------------------------------------------------------- #
def test_capture_and_flush_threads_deliver_in_order_with_the_delay_applied(
        fake_handle):
    # Limiter off: this asserts the delay is actually applied by the real
    # threads. The ramp that gets there is the subject of the slew section.
    b = nexus_svc._InboundDelayBuffer(slew_ms_per_s=0.0)
    assert b.start(CONSOLE_IP, COURT_IP)[0]
    try:
        b.set_delay(60.0)
        t0 = time.perf_counter()
        for seq in range(6):
            fake_handle.feed(_FakePacket(seq))
            time.sleep(0.005)

        deadline = time.perf_counter() + 2.0
        while len(fake_handle.snapshot()) < 6 and time.perf_counter() < deadline:
            b.set_delay(60.0)          # keepalive, keeps the watchdog quiet
            time.sleep(0.01)

        assert fake_handle.snapshot() == list(range(6))
        # Every packet must have been held for at least most of the delay.
        assert fake_handle.send_times[0] - t0 >= 0.050
    finally:
        b.stop("test")


def test_flush_jitter_is_bounded(fake_handle):
    """Measures actual release jitter through the real flush thread."""
    # Limiter off so every packet sees the same nominal delay and the residuals
    # measure flush jitter alone rather than jitter plus an in-progress ramp.
    b = nexus_svc._InboundDelayBuffer(slew_ms_per_s=0.0)
    assert b.start(CONSOLE_IP, COURT_IP)[0]
    try:
        b.set_delay(50.0)
        expected = []
        for seq in range(20):
            t = time.perf_counter()
            b.enqueue(_FakePacket(seq))
            expected.append(t + 0.050)
            b.set_delay(50.0)
            time.sleep(0.004)

        deadline = time.perf_counter() + 3.0
        while len(fake_handle.snapshot()) < 20 and time.perf_counter() < deadline:
            b.set_delay(50.0)
            time.sleep(0.005)

        assert fake_handle.snapshot() == list(range(20))
        errors = [(actual - want) * 1000.0
                  for actual, want in zip(fake_handle.send_times, expected)]
        assert min(errors) > -1.0, f"released early: {min(errors):.2f}ms"
        # Generous ceiling: the point is that it is single-digit-ms, not that
        # it hits a hard real-time bound on a loaded dev box.
        assert max(errors) < 25.0, f"flush jitter too high: {max(errors):.2f}ms"
    finally:
        b.stop("test")


# --------------------------------------------------------------------------- #
# Slew limiter — the anti-stutter guarantee
# --------------------------------------------------------------------------- #
# WHY THIS SECTION EXISTS
# -----------------------
# The feature was deleted on 2026-08-04 because "holding packets through
# WinDivert stutters the game and cost a batch of tip timing" (968b127f). The
# mechanism was never a mystery once the arithmetic is written down: a packet
# arriving at t leaves at t + D(t), so two packets dt apart leave dt*(1 + D')
# apart and the console receives the server's update stream at 1/(1 + D') of
# its true rate. A CONSTANT delay is harmless (D' = 0, multiplier exactly 1).
# The deleted controller ramped 55ms every 50ms tick, i.e. D' = +1.1, i.e. the
# console saw 48% of the normal update rate for ~150ms immediately before the
# meter rendered.
#
# These tests fix that as a property of the SERVICE, not of client good
# behaviour, so no controller bug, version skew or hostile caller can
# reintroduce it.

def test_shipping_slew_cap_keeps_the_packet_rate_within_ten_percent():
    """The cap is chosen so 1/(1+D') stays inside ordinary jitter."""
    d_prime = nexus_svc._METER_MAX_SLEW_MS_PER_S / 1000.0
    assert 1.0 / (1.0 + d_prime) >= 0.90, "ramp-up starves the console"
    assert 1.0 / (1.0 - d_prime) <= 1.12, "ramp-down bursts the console"


def test_the_configuration_that_got_this_deleted_would_fail_the_bar():
    """Regression guard naming the historical defect in its own units.

    MeterDelayController used kRampUpPerTickMs=55 with kTickMs=50. If anyone
    ever reinstates a slew of that order this test says why it is wrong.
    """
    deleted_ms_per_s = (55.0 / 50.0) * 1000.0          # == 1100 ms/s
    deleted_d_prime = deleted_ms_per_s / 1000.0
    assert 1.0 / (1.0 + deleted_d_prime) < 0.50
    assert deleted_ms_per_s > nexus_svc._METER_MAX_SLEW_MS_PER_S * 10


def test_applied_delay_never_moves_faster_than_the_cap(slew_buf):
    cap = nexus_svc._METER_MAX_SLEW_MS_PER_S
    t = 1_000.0
    slew_buf._last_slew_ts = t
    slew_buf.set_delay(250.0)

    prev = slew_buf.current_delay_ms
    assert prev == 0.0, "set_delay must not apply anything by itself"

    dt = 0.005
    for _ in range(2_000):
        t += dt
        applied = slew_buf.advance_slew(t)
        assert abs(applied - prev) <= cap * dt + 1e-9, "slew cap violated"
        prev = applied
        if applied >= 250.0:
            break
    assert prev == pytest.approx(250.0)
    assert slew_buf.peak_slew_ms_per_s <= cap + 1e-6


def test_ramp_to_the_sweet_spot_takes_the_expected_wall_time(slew_buf):
    """165ms at 100 ms/s is ~1.65s — deliberately too slow for a per-shot ramp.

    This is the load-bearing consequence of the cap: an engagement policy that
    tries to ramp inside a ~300ms pre-meter window physically cannot reach
    target, so the unsafe policy is unreachable rather than merely discouraged.
    """
    t = 1_000.0
    slew_buf._last_slew_ts = t
    slew_buf.set_delay(165.0)
    dt = 0.005
    elapsed = 0.0
    while slew_buf.current_delay_ms < 165.0 and elapsed < 10.0:
        t += dt
        elapsed += dt
        slew_buf.advance_slew(t)
    assert elapsed == pytest.approx(1.65, abs=0.05)
    # And confirm it cannot make it inside a shot's pre-meter budget.
    assert elapsed > 0.300


def test_a_quiet_period_cannot_bank_an_unbounded_step(slew_buf):
    """Token-bucket bound: silence must not buy a burst.

    Without the accumulator cap a client that went quiet for 10s would be
    allowed a 1000ms step in a single call — exactly the cliff the limiter
    exists to prevent.
    """
    t = 1_000.0
    slew_buf._last_slew_ts = t
    slew_buf.set_delay(250.0)
    applied = slew_buf.advance_slew(t + 10.0)
    ceiling = (nexus_svc._METER_MAX_SLEW_MS_PER_S
               * nexus_svc._METER_SLEW_MAX_ACCUM_S)
    assert applied <= ceiling + 1e-9
    assert applied == pytest.approx(ceiling)


def test_slew_is_symmetric_so_ramp_down_cannot_burst(slew_buf):
    """A fast ramp DOWN is a catch-up burst, which stutters just as badly."""
    cap = nexus_svc._METER_MAX_SLEW_MS_PER_S
    t = 1_000.0
    slew_buf._last_slew_ts = t
    slew_buf._delay_ms = 165.0
    slew_buf._target_ms = 165.0
    slew_buf.set_delay(0.0)

    prev = slew_buf.current_delay_ms
    dt = 0.005
    steps = 0
    while prev > 0.0 and steps < 2_000:
        t += dt
        applied = slew_buf.advance_slew(t)
        assert prev - applied <= cap * dt + 1e-9, "ramp-down burst"
        prev = applied
        steps += 1
    assert prev == 0.0


def test_only_the_safety_paths_may_bypass_the_limiter(slew_buf):
    """force_zero is the one sanctioned violation: a stuck delay outranks it."""
    slew_buf._last_slew_ts = 1_000.0
    slew_buf._delay_ms = 165.0
    slew_buf._target_ms = 165.0

    slew_buf.force_zero("test")
    assert slew_buf.current_delay_ms == 0.0
    assert slew_buf.target_delay_ms == 0.0
    assert slew_buf.stats()["emergency_zero"] == 1


def test_set_delay_alone_can_never_change_the_applied_delay(slew_buf):
    """The client expresses INTENT only. This is the whole contract."""
    for value in (165.0, 0.0, 250.0, 100.0):
        before = slew_buf.current_delay_ms
        slew_buf.set_delay(value)
        assert slew_buf.current_delay_ms == before
        assert slew_buf.target_delay_ms == pytest.approx(value)


def test_ordering_still_holds_while_the_delay_is_ramping(slew_buf, fake_handle):
    """The limiter must not be able to reorder packets.

    Release times are kept monotonic, so a rising delay can only push packets
    later and a falling delay is re-paced rather than bursted. Drive a full
    ramp up and back down with traffic flowing throughout.
    """
    t = 1_000.0
    slew_buf._last_slew_ts = t
    seq = 0
    for phase_target in (165.0, 0.0):
        slew_buf.set_delay(phase_target)
        for _ in range(400):
            t += 0.005
            slew_buf.advance_slew(t)
            slew_buf.enqueue(_FakePacket(seq))
            seq += 1
            slew_buf.flush_due()
    slew_buf.flush_due(time.perf_counter() + 10.0)

    sent = fake_handle.snapshot()
    assert sent == sorted(sent), "packets left out of order during a ramp"
    assert len(sent) == seq, "packets were lost during a ramp"


# --------------------------------------------------------------------------- #
# Arm gate — cannot fire unless explicitly armed
# --------------------------------------------------------------------------- #
def test_meter_delay_is_disarmed_by_default(monkeypatch):
    monkeypatch.delenv(nexus_svc._METER_ARM_ENV, raising=False)
    armed, source = nexus_svc._meter_arm_requested(["nexus_svc.py", "debug"])
    assert armed is False
    assert source == ""


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "On"])
def test_env_var_arms_the_intercept(monkeypatch, value):
    monkeypatch.setenv(nexus_svc._METER_ARM_ENV, value)
    armed, source = nexus_svc._meter_arm_requested(["nexus_svc.py", "debug"])
    assert armed is True
    assert source == "env"


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_junk_env_values_do_not_arm(monkeypatch, value):
    monkeypatch.setenv(nexus_svc._METER_ARM_ENV, value)
    armed, _ = nexus_svc._meter_arm_requested(["nexus_svc.py", "debug"])
    assert armed is False


def test_cli_flag_arms_the_intercept(monkeypatch):
    monkeypatch.delenv(nexus_svc._METER_ARM_ENV, raising=False)
    armed, source = nexus_svc._meter_arm_requested(
        ["nexus_svc.py", "debug", nexus_svc._METER_ARM_FLAG])
    assert armed is True
    assert source == "cli"


# --------------------------------------------------------------------------- #
# Compiled-service launch routing — the SCM starts NexusVisionSvc.exe via its
# ImagePath (no verb; the installer bakes --arm-meter-delay into binPath to arm).
# That invocation must run the SCM control dispatcher, never the command parser.
# --------------------------------------------------------------------------- #
def test_scm_launch_routes_to_the_service_dispatcher():
    # Bare launch (historical dev) and the SCM launch of an ARMED service both
    # carry no command verb -> run the dispatcher.
    assert nexus_svc._is_service_run_invocation([]) is True
    assert nexus_svc._is_service_run_invocation([nexus_svc._METER_ARM_FLAG]) is True


@pytest.mark.parametrize("args", [
    ["debug"],
    ["install"],
    ["--startup", "manual", "install"],
    ["remove"],
    ["driver-status"],
    ["debug", nexus_svc._METER_ARM_FLAG],  # a real verb wins even with the arm flag present
])
def test_operator_command_verbs_do_not_route_to_the_dispatcher(args):
    assert nexus_svc._is_service_run_invocation(args) is False


def test_installer_armed_imagepath_both_routes_and_arms(monkeypatch):
    """End-to-end arming contract for the shipped service: the installer registers
    binPath = "NexusVisionSvc.exe --arm-meter-delay". On SCM start the process sees
    argv=[exe, --arm-meter-delay], which (1) routes to the dispatcher and (2) arms
    the intercept. Disarming = the same binPath without the flag."""
    monkeypatch.delenv(nexus_svc._METER_ARM_ENV, raising=False)
    exe = r"C:\Program Files\Venice\packet_bridge\NexusVisionSvc.exe"

    # Armed registration.
    assert nexus_svc._is_service_run_invocation([nexus_svc._METER_ARM_FLAG]) is True
    armed, source = nexus_svc._meter_arm_requested([exe, nexus_svc._METER_ARM_FLAG])
    assert (armed, source) == (True, "cli")

    # Disarmed registration (flag removed from binPath) still runs as a service,
    # and the arm gate reports disarmed -> the honesty surface stays truthful.
    assert nexus_svc._is_service_run_invocation([]) is True
    armed, source = nexus_svc._meter_arm_requested([exe])
    assert (armed, source) == (False, "")


def test_disarmed_bridge_answers_loudly_instead_of_silently(monkeypatch):
    """The trap this codebase keeps falling into, closed.

    manual_meter_delay_ms spent months as an invisible no-op because every gate
    failed quietly. A disarmed bridge must NAME the reason and the remedy.
    """
    hub = nexus_svc._BroadcastHub()
    handler = _authed_handler(hub, _FakeWinDivertLoop(), None, monkeypatch)

    for verb in ("start_meter_intercept", "set_meter_delay",
                 "stop_meter_intercept", "meter_delay_stats"):
        handler._handle_cmd({"cmd": verb, "delay_ms": 165.0})
        reply = _last(handler)
        assert reply["event"] == "error"
        assert reply["msg"] == "meter_delay_disarmed"
        assert reply["cmd"] == verb
        assert nexus_svc._METER_ARM_FLAG in reply["detail"]
        assert nexus_svc._METER_ARM_ENV in reply["detail"]


def test_disarmed_bridge_does_not_advertise_the_feature(monkeypatch):
    """Distinguishable from a v2 bridge that simply lacks the verb."""
    monkeypatch.setattr(nexus_svc, "SVC_FEATURES", ())
    monkeypatch.setattr(nexus_svc, "_METER_ARMED", False)
    hub = nexus_svc._BroadcastHub()
    handler = _authed_handler(hub, _FakeWinDivertLoop(), None, monkeypatch)

    handler._handle_cmd({"cmd": "status"})
    status = _last(handler)
    assert status["version"] == 3, "still a v3 bridge"
    assert "meter_delay" not in status["features"]
    assert status["meter_delay_armed"] is False
    # A disarmed bridge reports no delay state at all, rather than zeros that
    # could be mistaken for an armed-but-idle intercept.
    assert "meter_delay_active" not in status


def test_a_disarmed_bridge_can_never_open_an_intercept_handle(monkeypatch):
    """The structural half of the guarantee: no buffer object exists at all."""
    opened = []
    monkeypatch.setattr(nexus_svc, "_open_intercept_handle",
                        lambda f: opened.append(f))
    hub = nexus_svc._BroadcastHub()
    handler = _authed_handler(hub, _FakeWinDivertLoop(), None, monkeypatch)

    handler._handle_cmd({"cmd": "start_meter_intercept"})
    handler._handle_cmd({"cmd": "set_filter", "console_ip": CONSOLE_IP})
    handler._handle_cmd({"cmd": "set_court_ip", "court_ip": COURT_IP})
    assert opened == [], "a disarmed bridge opened an intercepting handle"


# --------------------------------------------------------------------------- #
# Driver residency — nothing stays loaded after we exit
# --------------------------------------------------------------------------- #
def test_driver_unload_verifies_the_result_rather_than_assuming_it(monkeypatch):
    """unregister() only *requests* a stop; the SCM refuses while handles live."""
    calls = []
    states = iter([True, True, False])
    monkeypatch.setattr(nexus_svc, "_PYDIVERT_OK", True)
    monkeypatch.setattr(nexus_svc, "_windivert_driver_running",
                        lambda: next(states))

    class _FakeWinDivert:
        @staticmethod
        def unregister():
            calls.append("unregister")

    monkeypatch.setattr(nexus_svc, "pydivert",
                        type("_M", (), {"WinDivert": _FakeWinDivert}))
    assert nexus_svc._unload_windivert_driver("test") is True
    assert calls == ["unregister"]


def test_driver_unload_reports_failure_when_the_driver_stays_resident(monkeypatch):
    """A refused unload must be loud, not swallowed.

    Verified on this rig 2026-08-06: `sc query WinDivert` reported RUNNING with
    no Orion process alive. Silence here is how that happens.
    """
    monkeypatch.setattr(nexus_svc, "_PYDIVERT_OK", True)
    monkeypatch.setattr(nexus_svc, "_windivert_driver_running", lambda: True)
    monkeypatch.setattr(nexus_svc, "pydivert",
                        type("_M", (), {"WinDivert": type(
                            "_W", (), {"unregister": staticmethod(lambda: None)})}))
    assert nexus_svc._unload_windivert_driver("test") is False


def test_driver_unload_is_a_noop_when_already_stopped(monkeypatch):
    monkeypatch.setattr(nexus_svc, "_PYDIVERT_OK", True)
    monkeypatch.setattr(nexus_svc, "_windivert_driver_running", lambda: False)
    called = []
    monkeypatch.setattr(nexus_svc, "pydivert",
                        type("_M", (), {"WinDivert": type(
                            "_W", (), {"unregister": staticmethod(
                                lambda: called.append(1))})}))
    assert nexus_svc._unload_windivert_driver("test") is True
    assert called == [], "unregister called against an already-stopped driver"


def test_ramp_down_to_zero_restores_the_zero_delay_fast_path(slew_buf, fake_handle):
    """The applied delay must land EXACTLY on target, not a hair short.

    Regression, found 2026-08-06. perf_counter values are ~1e4, so a 5ms step
    carries ~5e-13 of float error; the accumulated applied delay converges to
    ~1.5e-10 short of target. If the slew merely returns early at that point
    instead of snapping, a ramp back to zero leaves _delay_ms fractionally
    ABOVE zero -- permanently. enqueue()'s zero-delay fast path tests
    `delay_s <= 0.0`, so it never fires again and every packet is buffered and
    rescheduled for the rest of the session, long after the delay is nominally
    off. Silent, and it survives a full ramp cycle.
    """
    t = 1_000.0
    slew_buf._last_slew_ts = t

    slew_buf.set_delay(165.0)
    for _ in range(5_000):
        if slew_buf.current_delay_ms >= 165.0:
            break
        t += 0.005
        slew_buf.advance_slew(t)
    assert slew_buf.current_delay_ms == 165.0, "ramp up never reached target"

    slew_buf.set_delay(0.0)
    for _ in range(5_000):
        if slew_buf.current_delay_ms <= 0.0:
            break
        t += 0.005
        slew_buf.advance_slew(t)
    # Exact equality is the assertion. approx() would hide the whole defect.
    assert slew_buf.current_delay_ms == 0.0, "float residue left above zero"

    before = slew_buf.stats()["passed"]
    slew_buf.enqueue(_FakePacket(4242))
    assert slew_buf.stats()["passed"] == before + 1, "zero-delay fast path did not re-arm"
    assert fake_handle.snapshot()[-1] == 4242
