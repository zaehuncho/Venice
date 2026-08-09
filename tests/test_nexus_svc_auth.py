import io
import json
import os
import threading

import pytest

import nexus_svc


class _FakeWinDivertLoop:
    def __init__(self):
        self.console_ips = []
        self.court_ips = []

    def set_console_ip(self, value):
        self.console_ips.append(value)

    def clear_console_ip(self):
        self.console_ips.append("")

    def set_court_ip(self, value):
        self.court_ips.append(value)


class _ChunkSocket:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def settimeout(self, _timeout):
        pass

    def recv(self, _size):
        return self._chunks.pop(0) if self._chunks else b""


class _CloseTracker:
    def __init__(self):
        self.close_calls = 0

    def close(self):
        self.close_calls += 1


def test_packet_broadcast_is_blocked_until_client_authenticates():
    hub = nexus_svc._BroadcastHub()
    auth_ready = threading.Event()
    client_queue = hub.add_client(auth_ready)

    hub.broadcast({"event": "packet", "size": 123})
    assert client_queue.empty()

    auth_ready.set()
    hub.broadcast({"event": "packet", "size": 456})
    payload = json.loads(client_queue.get_nowait().decode("utf-8"))
    assert payload == {"event": "packet", "size": 456}

    hub.remove_client(client_queue)
    hub.broadcast({"event": "packet", "size": 789})
    assert client_queue.empty()


def test_only_valid_token_opens_broadcast_gate(monkeypatch):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "test-token")
    hub = nexus_svc._BroadcastHub()
    handler = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub, object())

    handler._handle_cmd({"cmd": "auth", "token": "wrong"})
    rejected = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert rejected["event"] == "error"
    assert not handler._authed
    assert not handler._auth_ready.is_set()

    handler._handle_cmd({"cmd": "auth", "token": "test-token"})
    accepted = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert accepted == {"event": "ack", "cmd": "auth"}
    assert handler._authed
    assert handler._auth_ready.is_set()


def test_empty_service_token_fails_closed(monkeypatch):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "")
    hub = nexus_svc._BroadcastHub()
    handler = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub, object())

    handler._handle_cmd({"cmd": "auth", "token": ""})
    response = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert response["event"] == "error"
    assert not handler._authed
    assert not handler._auth_ready.is_set()


def test_authenticated_filter_commands_reject_expression_injection(monkeypatch):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "test-token")
    hub = nexus_svc._BroadcastHub()
    wd_loop = _FakeWinDivertLoop()
    handler = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub, wd_loop)
    handler._handle_cmd({"cmd": "auth", "token": "test-token"})
    handler._q.get_nowait()

    handler._handle_cmd({
        "cmd": "set_filter",
        "console_ip": "192.168.1.20 or true",
    })
    rejected = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert rejected == {"event": "error", "msg": "invalid_console_ip"}
    assert wd_loop.console_ips == []

    handler._handle_cmd({"cmd": "set_filter", "console_ip": "192.168.001.020"})
    rejected_noncanonical = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert rejected_noncanonical["msg"] == "invalid_console_ip"
    assert wd_loop.console_ips == []

    handler._handle_cmd({"cmd": "set_filter", "console_ip": "192.168.1.20"})
    accepted = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert accepted["event"] == "ack"
    assert wd_loop.console_ips == ["192.168.1.20"]


def test_court_hint_requires_public_ipv4(monkeypatch):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "test-token")
    hub = nexus_svc._BroadcastHub()
    wd_loop = _FakeWinDivertLoop()
    handler = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub, wd_loop)
    handler._handle_cmd({"cmd": "auth", "token": "test-token"})
    handler._q.get_nowait()

    handler._handle_cmd({"cmd": "set_court_ip", "court_ip": "192.168.1.20"})
    rejected = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert rejected["msg"] == "invalid_court_ip"
    assert wd_loop.court_ips == []

    handler._handle_cmd({"cmd": "set_court_ip", "court_ip": "45.79.123.45"})
    accepted = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert accepted["event"] == "ack"
    assert wd_loop.court_ips == ["45.79.123.45"]


def test_non_object_and_oversized_commands_fail_closed(monkeypatch):
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "test-token")
    hub = nexus_svc._BroadcastHub()
    handler = nexus_svc._ClientHandler(
        _ChunkSocket([b"x" * (nexus_svc._MAX_COMMAND_BUFFER_BYTES + 1)]),
        ("127.0.0.1", 1), hub, _FakeWinDivertLoop())

    handler._handle_cmd(["auth", "test-token"])
    invalid = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert invalid["msg"] == "invalid_command"
    assert not handler._authed

    handler._reader()
    oversized = json.loads(handler._q.get_nowait().decode("utf-8"))
    assert oversized["msg"] == "command_too_large"


# ---------------------------------------------------------------------------------------------
# Token publication.
#
# Regression cover for "the Network telemetry card is permanently empty". Packet capture ships
# enabled by default, so the bridge is normally started by the app as an ORDINARY UNELEVATED USER
# ("debug" mode). Both modes used to publish to %PROGRAMDATA%, which is world-readable and so has
# to have its DACL rewritten to be safe. Once an elevated run had done that, the file was no
# longer writable by the unelevated user: every later debug run failed to write it, fell into the
# fail-closed path with an empty token, and left the old file on disk for the client to read. The
# result was a permanent "Packet bridge authentication rejected" with no other symptom.
# ---------------------------------------------------------------------------------------------

def _pin_token_globals(monkeypatch):
    """Let monkeypatch restore the module globals that _init_bridge_token() reassigns."""
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "")
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN_ACTIVE_PATH", "")
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN_MODE", "")


def _redirect_token_paths(monkeypatch, tmp_path):
    programdata = tmp_path / "ProgramData" / "NexusVision" / "nexus_bridge.token"
    localappdata = tmp_path / "LocalAppData" / "NexusVision" / "nexus_bridge.token"
    monkeypatch.setattr(nexus_svc, "_programdata_token_path", lambda: str(programdata))
    monkeypatch.setattr(nexus_svc, "_localappdata_token_path", lambda: str(localappdata))
    return programdata, localappdata


def test_debug_mode_publishes_a_usable_token_without_elevation(tmp_path, monkeypatch):
    """An unelevated bridge must end up with a real token, in the per-user private location.

    This exercises the ACL hardening for real: %LOCALAPPDATA% is already per-user private, and
    because the process creates the file it owns it, so WRITE_DAC is implicit and the DACL can be
    pinned to owner-only with no privileged operation at all.
    """
    _pin_token_globals(monkeypatch)
    programdata, localappdata = _redirect_token_paths(monkeypatch, tmp_path)

    nexus_svc._init_bridge_token("debug")

    assert nexus_svc._BRIDGE_TOKEN, "an unelevated bridge must still publish a token"
    assert nexus_svc._BRIDGE_TOKEN_ACTIVE_PATH == str(localappdata)
    assert nexus_svc._BRIDGE_TOKEN_MODE == "debug"
    # The whole point: the unelevated path must not depend on ProgramData in any way.
    assert not programdata.exists()

    record = json.loads(localappdata.read_text(encoding="utf-8"))
    assert record["token"] == nexus_svc._BRIDGE_TOKEN
    assert record["mode"] == "debug"
    # pid + start stamp are what make a leftover file detectable as stale instead of silently
    # poisoning auth.
    assert record["pid"] == os.getpid()
    assert record["started_ms"] > 0

    # The token is only as good as its ACL; prove the file really is locked down, not merely
    # assumed to be private by inheritance. Anyone beyond SYSTEM, Administrators and this very
    # account -- notably the inherited "Users" ACE -- makes _verify_token_acl raise.
    import win32security

    nexus_svc._verify_token_acl(str(localappdata), [
        win32security.ConvertStringSidToSid("S-1-5-18"),
        win32security.ConvertStringSidToSid("S-1-5-32-544"),
        nexus_svc._current_user_sid(),
    ])
    # INTERACTIVE is deliberately NOT granted in debug mode: writer and reader are the same
    # account, so this DACL is strictly tighter than the service-mode one.
    with pytest.raises(PermissionError):
        nexus_svc._verify_token_acl(str(localappdata), [
            win32security.ConvertStringSidToSid("S-1-5-18"),
            win32security.ConvertStringSidToSid("S-1-5-32-544"),
            win32security.ConvertStringSidToSid("S-1-5-4"),
        ])


def test_service_mode_publishes_to_programdata_with_interactive_read(tmp_path, monkeypatch):
    """LocalSystem writes ProgramData and must grant INTERACTIVE read, or the app cannot read it."""
    _pin_token_globals(monkeypatch)
    programdata, localappdata = _redirect_token_paths(monkeypatch, tmp_path)

    hardened = []
    monkeypatch.setattr(
        nexus_svc, "_harden_token_acl",
        lambda path, *, allow_interactive_read: hardened.append((path, allow_interactive_read)))

    nexus_svc._init_bridge_token("service")

    assert nexus_svc._BRIDGE_TOKEN_ACTIVE_PATH == str(programdata)
    assert hardened == [(str(programdata), True)]
    # LocalSystem's own LOCALAPPDATA is under systemprofile and unreadable by the signed-in user,
    # so service mode must never fall back to it.
    assert not localappdata.exists()


def test_stale_hardened_programdata_token_no_longer_blocks_an_unelevated_bridge(
        tmp_path, monkeypatch):
    """The shipped failure, reproduced end to end and then shown fixed.

    A previous ELEVATED run leaves a token in ProgramData that the signed-in user can read but not
    rewrite. The old code targeted only that file, so this is precisely the state in which the
    bridge published nothing and rejected every client for ten hours.
    """
    _pin_token_globals(monkeypatch)
    programdata, localappdata = _redirect_token_paths(monkeypatch, tmp_path)

    programdata.parent.mkdir(parents=True)
    programdata.write_text(
        '{"v":1,"token":"stale-from-an-elevated-run","pid":4242,"started_ms":1}',
        encoding="utf-8")
    # Service-mode ACL: readable by INTERACTIVE, writable only by SYSTEM/Administrators.
    nexus_svc._harden_token_acl(str(programdata), allow_interactive_read=True)
    try:
        # Precondition: this test is meaningless unless the file is genuinely unwritable here.
        with pytest.raises(PermissionError):
            io.open(str(programdata), "w", encoding="utf-8").close()

        nexus_svc._init_bridge_token("debug")

        assert nexus_svc._BRIDGE_TOKEN, "an unwritable ProgramData file must not disable auth"
        assert nexus_svc._BRIDGE_TOKEN_ACTIVE_PATH == str(localappdata)
        # The stale file survives (we cannot delete it either) but it is not this session's token,
        # which is exactly why the client checks the recorded pid before using one.
        stale = json.loads(programdata.read_text(encoding="utf-8"))
        assert stale["token"] == "stale-from-an-elevated-run"
        assert stale["token"] != nexus_svc._BRIDGE_TOKEN
    finally:
        # Hand write/delete access back so the tmp_path teardown cannot fail.
        nexus_svc._harden_token_acl(str(programdata), allow_interactive_read=False)


def test_token_publication_failure_still_fails_closed(tmp_path, monkeypatch):
    """An unprotected bearer token is equivalent to no authentication: never degrade to one."""
    _pin_token_globals(monkeypatch)
    _redirect_token_paths(monkeypatch, tmp_path)

    def _refuse(path, record, *, allow_interactive_read):
        raise PermissionError(13, "Permission denied", path)

    monkeypatch.setattr(nexus_svc, "_write_bridge_token_file", _refuse)

    nexus_svc._init_bridge_token("debug")

    assert nexus_svc._BRIDGE_TOKEN == ""
    assert nexus_svc._BRIDGE_TOKEN_ACTIVE_PATH == ""


def test_unprotected_acl_is_rejected_and_the_token_file_removed(tmp_path, monkeypatch):
    """If hardening cannot be verified the file must not be left lying around."""
    _pin_token_globals(monkeypatch)
    _, localappdata = _redirect_token_paths(monkeypatch, tmp_path)

    monkeypatch.setattr(
        nexus_svc, "_harden_token_acl",
        lambda path, *, allow_interactive_read: (_ for _ in ()).throw(
            PermissionError("token DACL is not protected")))

    nexus_svc._init_bridge_token("debug")

    assert nexus_svc._BRIDGE_TOKEN == ""
    assert not localappdata.exists(), "a token that could not be secured must be deleted"


def test_rejected_auth_says_why_instead_of_just_unauthorized(monkeypatch):
    """'unauthorized' alone cost a ten-hour investigation; each distinguishable cause must say so."""
    hub = nexus_svc._BroadcastHub()

    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "")
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN_ACTIVE_PATH", "")
    starved = nexus_svc._ClientHandler(object(), ("127.0.0.1", 1), hub, object())
    starved._handle_cmd({"cmd": "auth", "token": "anything"})
    response = json.loads(starved._q.get_nowait().decode("utf-8"))
    assert response["msg"] == "unauthorized"
    assert response["reason"] == "no_server_token"

    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN", "fresh-token")
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN_ACTIVE_PATH", r"C:\somewhere\nexus_bridge.token")
    monkeypatch.setattr(nexus_svc, "_BRIDGE_TOKEN_MODE", "debug")

    mismatched = nexus_svc._ClientHandler(object(), ("127.0.0.1", 2), hub, object())
    mismatched._handle_cmd({"cmd": "auth", "token": "yesterdays-token"})
    response = json.loads(mismatched._q.get_nowait().decode("utf-8"))
    assert response["reason"] == "token_mismatch"
    # The client prints this path, which is how the next person finds the right file immediately.
    assert response["token_path"] == r"C:\somewhere\nexus_bridge.token"
    assert response["token_mode"] == "debug"

    premature = nexus_svc._ClientHandler(object(), ("127.0.0.1", 3), hub, object())
    premature._handle_cmd({"cmd": "status"})
    response = json.loads(premature._q.get_nowait().decode("utf-8"))
    assert response["reason"] == "auth_required"

    # A correct token still authenticates, and the added reasons did not open a bypass.
    good = nexus_svc._ClientHandler(object(), ("127.0.0.1", 4), hub, object())
    good._handle_cmd({"cmd": "auth", "token": "fresh-token"})
    assert json.loads(good._q.get_nowait().decode("utf-8")) == {"event": "ack", "cmd": "auth"}
    assert good._authed


def test_service_stop_interrupts_active_windivert_receive():
    stop_evt = threading.Event()
    loop = nexus_svc._WinDivertLoop(nexus_svc._BroadcastHub(), stop_evt)
    handle = _CloseTracker()
    loop._sniff_handle = handle

    loop.stop()

    assert stop_evt.is_set()
    assert loop._restart_evt.is_set()
    assert loop._sniff_handle is None
    assert handle.close_calls == 1
