"""PS5 rest-mode wakeup: protocol formatting, registry parsing, launch gating.

The CLI direct-stream launch (OrionStream.exe stream <nick> <host>) never wakes
a sleeping console — chiaki-ng's stream path skips discovery entirely. These
tests pin the Python-side replacement: the exact chiaki WAKEUP datagram, the
QSettings registry round-trip for the regist key, and the fail-open launch gate
in RemotePlayClientManager.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ps5_wake
import remote_play_client


# --- discovery response parsing -------------------------------------------------

def test_probe_parse_ready():
    assert ps5_wake._parse_probe_response(b"HTTP/1.1 200 Ok\nhost-id:abc\n") == "ready"


def test_probe_parse_standby():
    assert ps5_wake._parse_probe_response(
        b"HTTP/1.1 620 Server Standby\nhost-id:abc\n") == "standby"


def test_probe_parse_garbage_is_none():
    assert ps5_wake._parse_probe_response(b"not a discovery response") is None


def test_probe_parse_unknown_status_is_conservatively_ready():
    # An answering console with an unexpected status must fall through to the
    # normal launch, never to a wakeup or a fail-fast.
    assert ps5_wake._parse_probe_response(b"HTTP/1.1 540 Whatever\n") == "ready"


def test_probe_parse_anchors_at_start_and_reads_full_digit_run():
    # chiaki anchors "HTTP/1.1 " at offset 0 and strtol()s all digits.
    assert ps5_wake._parse_probe_response(b"junk HTTP/1.1 620 Standby\n") is None
    assert ps5_wake._parse_probe_response(b"HTTP/1.1 6200 Weird\n") == "ready"


# --- QSettings QByteArray decoding ----------------------------------------------

def test_decode_utf16_bytearray_wrapper():
    # QSettings' Windows backend stores QByteArray("cafe1234" + 8 NULs) as the
    # UTF-16LE string "@ByteArray(<16 raw bytes>)".
    inner = "cafe1234" + "\x00" * 8
    blob = f"@ByteArray({inner})".encode("utf-16-le")
    assert ps5_wake._decode_qsettings_bytearray(blob) == inner.encode("latin-1")


def test_decode_plain_reg_binary_passthrough():
    raw = b"cafe1234" + b"\x00" * 8
    assert ps5_wake._decode_qsettings_bytearray(raw) == raw


def test_decode_string_form():
    assert ps5_wake._decode_qsettings_bytearray(
        "@ByteArray(deadbeef\x00\x00)") == b"deadbeef\x00\x00"


def test_decode_none_and_odd_types():
    assert ps5_wake._decode_qsettings_bytearray(None) == b""
    assert ps5_wake._decode_qsettings_bytearray(12345) == b""


# --- regist key extraction ------------------------------------------------------

def test_extract_key_truncates_at_nul():
    assert ps5_wake._extract_regist_key(b"cafe1234\x00\x00\x00") == "cafe1234"


def test_extract_key_rejects_empty_overlong_and_nonhex():
    assert ps5_wake._extract_regist_key(b"\x00" * 16) is None
    assert ps5_wake._extract_regist_key(b"cafe12345") is None      # 9 chars
    assert ps5_wake._extract_regist_key(b"nothexxx") is None


# --- wakeup packet --------------------------------------------------------------

def test_wakeup_packet_matches_chiaki_format():
    pkt = ps5_wake.build_wakeup_packet("deadbeef", ps5=True)
    # Trailing NUL included: chiaki_discovery_send transmits length + 1.
    assert pkt == (
        b"WAKEUP * HTTP/1.1\n"
        b"client-type:vr\n"
        b"auth-type:R\n"
        b"model:w\n"
        b"app-type:r\n"
        b"user-credential:3735928559\n"          # 0xdeadbeef in decimal
        b"device-discovery-protocol-version:00030010\n"
        b"\x00"
    )


def test_wakeup_packet_ps4_version():
    pkt = ps5_wake.build_wakeup_packet("1a2b", ps5=False)
    assert b"device-discovery-protocol-version:00020020\n" in pkt
    assert b"user-credential:6699\n" in pkt      # 0x1a2b


def test_wakeup_packet_rejects_bad_keys():
    assert ps5_wake.build_wakeup_packet("", True) is None
    assert ps5_wake.build_wakeup_packet("zzzz", True) is None
    assert ps5_wake.build_wakeup_packet("123456789", True) is None


# --- launch gate ----------------------------------------------------------------

class _FakeWake(types.ModuleType):
    """Injectable stand-in for the ps5_wake module inside the launch gate."""

    def __init__(self, state, key="cafe1234", port_opens=True,
                 send_ok=True, probe_raises=False, port_open_now=None):
        super().__init__("ps5_wake")
        self._state = state
        # Explicit "port is open right now" override; default derives it from the state.
        self._port_open_now = port_open_now
        self._key = key
        self._port_opens = port_opens
        self._send_ok = send_ok
        self._probe_raises = probe_raises
        self.wakeups_sent = []
        self.port_budgets = []

    def is_ip_literal(self, host):
        return ps5_wake.is_ip_literal(host)

    def probe_console_state(self, host, timeout_s=2.5):
        if self._probe_raises:
            raise OSError("no sockets for you")
        return ps5_wake.ConsoleProbe(self._state, 9302, "test")

    def read_chiaki_regist_key(self, nickname=None):
        return self._key, True

    def send_wakeup(self, host, regist_key, ps5=True):
        self.wakeups_sent.append((host, regist_key, ps5))
        return self._send_ok

    def wait_for_session_port(self, host, budget_s, **_kw):
        self.port_budgets.append(float(budget_s))
        return self._port_opens

    def session_port_open(self, host, timeout_s=0.6):
        # "Is the port open RIGHT NOW" (the pre-wake TCP fast-path) is true only for an
        # awake console; `_port_opens` models whether it opens AFTER a wakeup.
        if self._port_open_now is not None:
            return bool(self._port_open_now)
        return self._state == "ready"

    # ---- console address drift (discover_consoles / subnet_broadcast) ----
    discovered = ()          # what a broadcast finds; set per test
    broadcasts = None        # broadcast addresses the resolver asked for

    def subnet_broadcast(self, host):
        return ps5_wake.subnet_broadcast(host)

    def discover_consoles(self, broadcast, timeout_s=1.2):
        if self.broadcasts is None:
            self.broadcasts = []
        self.broadcasts.append(broadcast)
        return [ps5_wake.DiscoveredConsole(h, s, 9302) for h, s in self.discovered]


def _resolve(manager, fake, monkeypatch, host="192.0.2.10"):
    monkeypatch.setitem(sys.modules, "ps5_wake", fake)
    return manager._resolve_console_host(host)


def test_drift_silent_host_with_one_answering_console_is_adopted(manager, monkeypatch):
    fake = _FakeWake("no_answer")
    fake.discovered = (("192.0.2.77", "ready"),)
    assert _resolve(manager, fake, monkeypatch) == "192.0.2.77"
    assert fake.broadcasts == ["192.0.2.255"]
    assert manager._console_host_adopted == "192.0.2.77"


def test_drift_never_guesses_between_two_consoles(manager, monkeypatch):
    fake = _FakeWake("no_answer")
    fake.discovered = (("192.0.2.77", "ready"), ("192.0.2.78", "standby"))
    assert _resolve(manager, fake, monkeypatch) == "192.0.2.10"
    assert manager._console_host_adopted == ""


def test_drift_nothing_found_keeps_the_configured_host(manager, monkeypatch):
    fake = _FakeWake("no_answer")
    fake.discovered = ()
    assert _resolve(manager, fake, monkeypatch) == "192.0.2.10"


def test_drift_answering_host_is_never_touched(manager, monkeypatch):
    # session port open: no discovery at all
    fake = _FakeWake("ready")
    fake.discovered = (("192.0.2.77", "ready"),)
    assert _resolve(manager, fake, monkeypatch) == "192.0.2.10"
    assert fake.broadcasts is None
    # port closed but discovery answers (rest mode): the wake path owns it, no swap
    fake = _FakeWake("standby")
    fake.discovered = (("192.0.2.77", "ready"),)
    assert _resolve(manager, fake, monkeypatch) == "192.0.2.10"
    assert fake.broadcasts is None


def test_drift_discovery_can_be_disabled(manager, monkeypatch):
    monkeypatch.setenv("ORION_PS5_DISCOVER_DRIFT", "0")
    fake = _FakeWake("no_answer")
    fake.discovered = (("192.0.2.77", "ready"),)
    assert _resolve(manager, fake, monkeypatch) == "192.0.2.10"


def test_subnet_broadcast_is_the_slash24_directed_broadcast():
    assert ps5_wake.subnet_broadcast("192.168.137.126") == "192.168.137.255"
    assert ps5_wake.subnet_broadcast("ps5.local") == ""
    assert ps5_wake.subnet_broadcast("") == ""


@pytest.fixture
def manager():
    return remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig())


def _gate(manager, fake, monkeypatch):
    monkeypatch.setitem(sys.modules, "ps5_wake", fake)
    return manager._ensure_console_awake("Test PS5", "192.0.2.10", "chiaki.exe")


def test_gate_ready_console_proceeds(manager, monkeypatch):
    fake = _FakeWake("ready")
    assert _gate(manager, fake, monkeypatch) is None
    assert fake.wakeups_sent == []
    assert manager._last_console_probe == "ready"


def test_gate_no_answer_still_launches_but_wakes_first(manager, monkeypatch):
    # A firewall eating UDP replies must not change launch behavior (launch proceeds and
    # the failure path adds evidence). Since 2026-08-29 the wakeup is sent BEFORE the
    # probe: it is a no-op to an awake console and inert to an off one, and it means a
    # resting console behind such a firewall is already booting for the next Connect.
    fake = _FakeWake("no_answer")
    assert _gate(manager, fake, monkeypatch) is None
    assert fake.wakeups_sent == [("192.0.2.10", "cafe1234", True)]


def test_gate_standby_wakes_and_proceeds_when_port_opens(manager, monkeypatch):
    fake = _FakeWake("standby", port_opens=True)
    assert _gate(manager, fake, monkeypatch) is None
    assert fake.wakeups_sent == [("192.0.2.10", "cafe1234", True)]
    assert manager._last_console_probe == "ready"


def test_gate_standby_slow_wake_fails_fast_with_honest_message(manager, monkeypatch):
    fake = _FakeWake("standby", port_opens=False)
    status = _gate(manager, fake, monkeypatch)
    assert status is not None and status.ok is False
    assert "rest mode" in status.message
    assert "Connect again" in status.message
    assert fake.wakeups_sent  # the wakeup WAS sent before failing fast


def test_gate_standby_without_key_fails_fast_with_manual_instruction(
        manager, monkeypatch):
    fake = _FakeWake("standby", key=None)
    status = _gate(manager, fake, monkeypatch)
    assert status is not None and status.ok is False
    assert "manually" in status.message
    assert fake.wakeups_sent == []


def test_gate_wake_first_then_short_probe(manager, monkeypatch):
    # 2026-08-29: the wakeup packet is sent BEFORE the discovery probe (the console's
    # ~10-25s boot starts at +0.4s instead of after a 2.5s probe), and the probe budget
    # shrinks to 1.0s because it now only decides messaging.
    fake = _FakeWake("standby", port_opens=True)
    calls = []
    real_probe = fake.probe_console_state
    def probe(host, timeout_s=2.5):
        calls.append(("probe", timeout_s, list(fake.wakeups_sent)))
        return real_probe(host, timeout_s=timeout_s)
    fake.probe_console_state = probe
    assert _gate(manager, fake, monkeypatch) is None
    assert calls and calls[0][1] == 1.0                 # short probe
    assert calls[0][2] == [("192.0.2.10", "cafe1234", True)]   # wake already sent when probed


def test_gate_standby_budget_is_deadline_sized_without_host_extension(manager, monkeypatch):
    fake = _FakeWake("standby", port_opens=True)
    assert _gate(manager, fake, monkeypatch) is None
    # wait_timeout_s 18 (native 20s deadline) - ~5s spent/still needed -> 13s, never the old 9s
    assert fake.port_budgets == [13.0]


def test_gate_standby_reports_budget_to_host_and_waits_full_boot(monkeypatch):
    # With a host callback the native extends its deadline, so cover the documented
    # 15-25s rest-mode boot instead of failing at 9s and demanding "Connect again".
    seen = []
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(on_console_waking=seen.append))
    fake = _FakeWake("standby", port_opens=True)
    assert _gate(manager, fake, monkeypatch) is None
    assert seen == [25.0]
    assert fake.port_budgets == [25.0]


def test_gate_probe_exception_fails_open(manager, monkeypatch):
    fake = _FakeWake("standby", probe_raises=True)
    assert _gate(manager, fake, monkeypatch) is None


def test_gate_env_kill_switch(manager, monkeypatch):
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    fake = _FakeWake("standby")
    assert _gate(manager, fake, monkeypatch) is None
    assert fake.wakeups_sent == []


def test_gate_suppressed_for_recovery_launches(monkeypatch):
    # console_wake_allowed=False (mid-session input-link recovery) must skip
    # ALL wake machinery: its 17s/20s watchdog arithmetic predates these
    # probes, and the user may have rested the console deliberately.
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_wake_allowed=False))
    fake = _FakeWake("standby")
    assert _gate(manager, fake, monkeypatch) is None
    assert fake.wakeups_sent == []


def test_gate_skips_hostname_targets(manager, monkeypatch):
    # A hostname would drag synchronous DNS into every probe send, unbounded
    # by the probe budget. Only IP literals are probed.
    fake = _FakeWake("standby")
    monkeypatch.setitem(sys.modules, "ps5_wake", fake)
    assert manager._ensure_console_awake(
        "Test PS5", "my-ps5.local", "chiaki.exe") is None
    assert fake.wakeups_sent == []


# --- ip literal helper ----------------------------------------------------------

def test_is_ip_literal():
    assert ps5_wake.is_ip_literal("192.168.137.100")
    assert ps5_wake.is_ip_literal("fe80::1")
    assert not ps5_wake.is_ip_literal("my-ps5.local")
    assert not ps5_wake.is_ip_literal("")
    assert not ps5_wake.is_ip_literal(None)


# --- prep-anchored readiness deadline -------------------------------------------

def _deadline_manager(wait_timeout_s=15.0):
    return remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(wait_timeout_s=wait_timeout_s))


def test_deadline_fast_prep_charges_prep_to_the_poll():
    manager = _deadline_manager()
    manager._prep_started = 1000.0
    # 2s of prep: the poll ends at entry+15, not launch+15.
    assert manager._readiness_deadline(1002.0) == pytest.approx(1015.0)


def test_deadline_slow_prep_keeps_the_5s_floor():
    manager = _deadline_manager()
    manager._prep_started = 1000.0
    # 12s of prep (rest-mode wake): the woken console still gets a 5s poll.
    assert manager._readiness_deadline(1012.0) == pytest.approx(1017.0)


def test_deadline_never_exceeds_unanchored_budget():
    manager = _deadline_manager()
    manager._prep_started = 2000.0   # clock skew / future stamp
    assert manager._readiness_deadline(1000.0) == pytest.approx(1015.0)


def test_deadline_without_prep_stamp_matches_legacy():
    manager = _deadline_manager()
    if hasattr(manager, "_prep_started"):
        del manager._prep_started
    assert manager._readiness_deadline(1000.0) == pytest.approx(1015.0)


# --- failure-path evidence ------------------------------------------------------

def test_failure_evidence_names_rest_mode(monkeypatch):
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip="192.0.2.10"))
    monkeypatch.setitem(sys.modules, "ps5_wake", _FakeWake("standby"))
    assert "REST MODE" in manager._console_failure_evidence()


def test_failure_evidence_names_powered_off(monkeypatch):
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip="192.0.2.10"))
    monkeypatch.setitem(
        sys.modules, "ps5_wake", _FakeWake("no_answer", port_opens=False))
    assert "powered off" in manager._console_failure_evidence()


def test_failure_evidence_silent_when_console_is_up(monkeypatch):
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip="192.0.2.10"))
    monkeypatch.setitem(
        sys.modules, "ps5_wake", _FakeWake("no_answer", port_open_now=True))
    assert manager._console_failure_evidence() == ""


def test_failure_evidence_empty_without_console_ip():
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip=""))
    assert manager._console_failure_evidence() == ""


def test_failure_evidence_suppressed_for_recovery_launches(monkeypatch):
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            console_ip="192.0.2.10", console_wake_allowed=False))
    monkeypatch.setitem(sys.modules, "ps5_wake", _FakeWake("standby"))
    assert manager._console_failure_evidence() == ""
