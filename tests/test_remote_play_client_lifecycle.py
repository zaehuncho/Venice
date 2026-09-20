import remote_play_client
from decoder_pipe_identity import stable_executable_snapshot


class _Process:
    def __init__(self, returncode):
        self.returncode = returncode
        self.pid = 4242
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def test_disable_video_launch_env_is_explicit_and_route_scoped(monkeypatch):
    monkeypatch.setenv("CHIAKI_ORION_DISABLE_VIDEO", "stale-parent-value")

    capture_env = remote_play_client.chiaki_controller_env(disable_video=True)
    decoder_env = remote_play_client.chiaki_controller_env(disable_video=False)

    assert capture_env["CHIAKI_ORION_DISABLE_VIDEO"] == "1"
    assert "CHIAKI_ORION_DISABLE_VIDEO" not in decoder_env


def test_manager_liveness_tracks_owned_process_exit():
    manager = remote_play_client.RemotePlayClientManager()
    manager._process = _Process(None)
    assert manager.is_running() is True
    manager._process.returncode = 0
    assert manager.is_running() is False


def test_hook_enabled_launch_cleans_stale_clients_before_window_scan(monkeypatch):
    calls = []

    monkeypatch.setenv("ORION_FRAME_PIPE", "1")
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: calls.append("cleanup"))
    monkeypatch.setattr(remote_play_client, "find_remote_play_window", lambda _title="": calls.append("scan") or (1234, "Orion Stream"))

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(close_on_stop=True)
    )

    status = manager.ensure_running()

    assert status.ok is True
    assert status.mode == "existing"
    assert calls == ["cleanup", "scan"]


def test_hook_enabled_launch_does_not_cleanup_when_close_on_stop_disabled(monkeypatch):
    calls = []

    monkeypatch.setenv("ORION_INPUT_HOOK", "1")
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: calls.append("cleanup"))
    monkeypatch.setattr(remote_play_client, "find_remote_play_window", lambda _title="": calls.append("scan") or (1234, "Orion Stream"))

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(close_on_stop=False)
    )

    status = manager.ensure_running()

    assert status.ok is True
    assert calls == ["scan"]


def test_hook_enabled_launch_performs_only_one_stale_client_sweep(monkeypatch, tmp_path):
    # This test is about the stale-client sweep, not the console wake gate; keep the real
    # ps5_wake module (UDP probe / wakeup packet) off the network with its kill switch.
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    calls = []
    monkeypatch.setenv("ORION_INPUT_HOOK", "1")
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes",
                        lambda: calls.append("cleanup"))
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (0, ""))
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(tmp_path / "OrionStream.exe"))
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe("registered-console", True, ""))

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            console_ip="1.2.3.4",
            close_on_stop=True,
            require_session_ready=True,
            session_log_dir=str(tmp_path),
        )
    )
    monkeypatch.setattr(manager, "_launch_process", lambda _cmd, mode, path:
                        remote_play_client.RemotePlayClientStatus(
                            ok=True, mode=mode, path=path, pid=42))
    monkeypatch.setattr(manager._session_tracker, "poll",
                        lambda: ("ready", str(tmp_path / "session.log")))

    status = manager.ensure_running()

    assert status.ok is True
    assert calls == ["cleanup"]


def _fast_clock(monkeypatch):
    clock = {"now": 0.0}

    def now():
        clock["now"] += 0.5
        return clock["now"]

    monkeypatch.setattr(remote_play_client.time, "time", now)
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _seconds: None)
    return clock


def test_session_ready_requires_fresh_marker_even_when_window_and_pipe_child_live(
        monkeypatch, tmp_path):
    old = tmp_path / "chiaki_session_2026-01-01_00-00-00.log"
    old.write_bytes(remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
    _fast_clock(monkeypatch)
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (1234, "Orion Stream"))

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            close_on_stop=False,
            require_session_ready=True,
            session_log_dir=str(tmp_path),
            wait_timeout_s=3.0,
        )
    )
    manager._process = _Process(None)
    monkeypatch.setattr(manager, "_launch", lambda _mode: remote_play_client.RemotePlayClientStatus(
        ok=True, mode="chiaki", pid=77, message="Started chiaki"))

    status = manager.ensure_running()

    assert status.ok is False
    assert status.session_ready is False
    assert "no fresh console-session readiness marker" in status.message


def test_session_ready_accepts_current_child_marker_without_requiring_window(
        monkeypatch, tmp_path):
    optimized = []
    _fast_clock(monkeypatch)
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (0, ""))
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            close_on_stop=False,
            require_session_ready=True,
            session_log_dir=str(tmp_path),
            wait_timeout_s=3.0,
        )
    )
    manager._process = _Process(None)
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process",
                        lambda pid: optimized.append(pid))

    def launch(_mode):
        current = tmp_path / "chiaki_session_2026-01-01_00-00-01.log"
        current.write_bytes(b"Starting session request\n"
                            + remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
        return remote_play_client.RemotePlayClientStatus(
            ok=True, mode="chiaki", pid=78, message="Started chiaki")

    monkeypatch.setattr(manager, "_launch", launch)

    status = manager.ensure_running()

    assert status.ok is True
    assert status.session_ready is True
    assert status.hwnd == 0
    assert manager.is_session_ready() is True
    assert optimized == [manager._process.pid]


def test_readiness_gated_launch_defers_high_priority_until_ready(monkeypatch, tmp_path):
    optimized = []
    launched = []

    class _Popen:
        pid = 5150

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(remote_play_client.subprocess, "Popen",
                        lambda *args, **kwargs: launched.append((args, kwargs)) or _Popen())
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process",
                        lambda pid: optimized.append(pid))

    gated = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(require_session_ready=True)
    )
    status = gated._launch_process([str(tmp_path / "OrionStream.exe")],
                                   "chiaki", str(tmp_path / "OrionStream.exe"))

    assert status.ok is True
    assert launched
    assert optimized == []

    immediate = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(require_session_ready=False)
    )
    status = immediate._launch_process([str(tmp_path / "OrionStream.exe")],
                                       "chiaki", str(tmp_path / "OrionStream.exe"))

    assert status.ok is True
    assert optimized == [5150]


def test_owned_process_identity_is_exact_and_generation_scoped(monkeypatch, tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"decoder")
    children = []

    class _Child:
        def __init__(self, pid):
            self.pid = pid
            self._handle = pid + 100_000
            self.returncode = None

        def poll(self):
            return self.returncode

    def popen(*_args, **_kwargs):
        child = _Child(6000 + len(children))
        children.append(child)
        return child

    monkeypatch.setattr(remote_play_client.subprocess, "Popen", popen)
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process", lambda _pid: None)
    monkeypatch.setattr(
        remote_play_client, "windows_process_creation_time_100ns",
        lambda handle: int(handle) * 10)
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(require_session_ready=True)
    )

    assert manager._launch_process(
        [str(executable)], "chiaki", str(executable)).ok
    first = manager.owned_process_identity()
    assert first["pid"] == 6000
    assert first["launch_generation"] == 1
    assert first["creation_time_100ns"] == 1_060_000
    assert first["process_handle"] == 106_000
    assert first["path"] == remote_play_client.canonical_executable_path(str(executable))

    children[-1].returncode = 0
    assert manager.owned_process_identity() == {}
    manager._process = None
    assert manager._launch_process(
        [str(executable)], "chiaki", str(executable)).ok
    second = manager.owned_process_identity()
    assert second["pid"] == 6001
    assert second["launch_generation"] == 2
    assert second["creation_time_100ns"] == 1_060_010


def test_native_image_expectation_is_locked_and_checked_before_spawn(monkeypatch, tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"native-selected-image")
    snapshot = stable_executable_snapshot(str(executable))
    spawned = []

    class _Child:
        pid = 7000

        @staticmethod
        def poll():
            return None

    monkeypatch.setattr(
        remote_play_client.subprocess, "Popen",
        lambda *_args, **_kwargs: spawned.append(True) or _Child())
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            require_session_ready=True,
            chiaki_identity_size=snapshot.size,
            chiaki_identity_sha256=snapshot.sha256,
        )
    )
    assert manager._launch_process(
        [str(executable)], "chiaki", str(executable)).ok
    assert spawned == [True]

    executable.write_bytes(b"tampered-image-bytes")
    rejected = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            require_session_ready=True,
            chiaki_identity_size=snapshot.size,
            chiaki_identity_sha256=snapshot.sha256,
        )
    )
    status = rejected._launch_process(
        [str(executable)], "chiaki", str(executable))
    assert status.ok is False
    assert "identity mismatch" in status.message
    assert spawned == [True]


def test_fresh_session_quit_fails_immediately_and_reaps_owned_child(
        monkeypatch, tmp_path):
    clock = _fast_clock(monkeypatch)
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (1234, "Orion Stream"))
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            close_on_stop=True,
            require_session_ready=True,
            session_log_dir=str(tmp_path),
            # The regression was a full 40-second Connecting wait despite an
            # authoritative quit marker at ~5 seconds.
            wait_timeout_s=40.0,
        )
    )
    child = _Process(None)
    manager._process = child

    def launch(_mode):
        current = tmp_path / "chiaki_session_2026-01-01_00-00-05.log"
        current.write_bytes(b"Starting session request\nSession has quit\n")
        return remote_play_client.RemotePlayClientStatus(
            ok=True, mode="chiaki", pid=child.pid, message="Started chiaki")

    monkeypatch.setattr(manager, "_launch", launch)

    status = manager.ensure_running()

    assert status.ok is False
    assert status.session_ready is False
    assert "current Chiaki session ended before becoming ready" in status.message
    assert child.terminated is True
    assert manager._process is None
    assert manager._failed_start_reaped is True
    assert clock["now"] < 5.0


def test_restart_cannot_reuse_old_ready_marker_and_quit_revokes_authority(tmp_path):
    first = tmp_path / "chiaki_session_2026-01-01_00-00-01.log"
    first.write_bytes(remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
    stale_baseline = remote_play_client._snapshot_session_logs(str(tmp_path))
    assert remote_play_client._fresh_session_log_state(
        str(tmp_path), stale_baseline)[0] == "waiting"

    second = tmp_path / "chiaki_session_2026-01-01_00-00-02.log"
    second.write_bytes(b"Starting session request\n"
                       + remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
    assert remote_play_client._fresh_session_log_state(
        str(tmp_path), stale_baseline)[0] == "ready"

    with second.open("ab") as handle:
        handle.write(b"Session has quit\n")
    assert remote_play_client._fresh_session_log_state(
        str(tmp_path), stale_baseline)[0] == "ended"

    restart_baseline = remote_play_client._snapshot_session_logs(str(tmp_path))
    third = tmp_path / "chiaki_session_2026-01-01_00-00-03.log"
    third.write_bytes(b"Starting session request\n")
    assert remote_play_client._fresh_session_log_state(
        str(tmp_path), restart_baseline)[0] == "waiting"
    with third.open("ab") as handle:
        handle.write(remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
    assert remote_play_client._fresh_session_log_state(
        str(tmp_path), restart_baseline)[0] == "ready"


def test_latched_readiness_survives_large_log_then_later_end_revokes(tmp_path):
    baseline = remote_play_client._snapshot_session_logs(str(tmp_path))
    current = tmp_path / "chiaki_session_2026-01-01_00-00-04.log"
    current.write_bytes(remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
    tracker = remote_play_client._SessionReadinessTracker(str(tmp_path), baseline)

    assert tracker.poll()[0] == "ready"

    # More than the old 2 MiB stateless tail: the initial marker is no longer
    # present in any plausible tail window, but the incremental latch remains.
    with current.open("ab") as handle:
        handle.write(b"diagnostic noise\n" * 150_000)
    assert current.stat().st_size > 2 * 1024 * 1024
    assert tracker.poll()[0] == "ready"

    with current.open("ab") as handle:
        handle.write(b"StreamConnection is disconnecting\n")
    assert tracker.poll()[0] == "ended"


def test_live_ready_status_is_revoked_when_current_session_quits(tmp_path):
    current = tmp_path / "chiaki_session_2026-01-01_00-00-06.log"
    baseline = remote_play_client._snapshot_session_logs(str(tmp_path))
    current.write_bytes(remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            close_on_stop=False,
            require_session_ready=True,
            session_log_dir=str(tmp_path),
        )
    )
    manager._session_tracker.reset(baseline)
    manager._process = _Process(None)
    manager._status = remote_play_client.RemotePlayClientStatus(
        ok=True, session_ready=True)
    assert manager.is_session_ready() is True

    with current.open("ab") as handle:
        handle.write(b"Session has quit\n")

    assert manager.is_session_ready() is False
    assert manager.status.session_ready is False


# --- [ORION_CLIENT_LAUNCHABILITY 2026-08-13] -------------------------------------------
# Regression cover for the 2026-08-13 rig failure: a rebuilt OrionStream.exe linked
# against FFmpeg 8 while the deploy folder shipped FFmpeg 7, so the client exited
# 0xC0000135 (STATUS_DLL_NOT_FOUND) in the Windows loader with EMPTY stdout. The old
# probe returned "" for that, identical to "no console registered", and the caller
# logged a registration problem and opened the lobby. The console registration was
# intact throughout. These pin the two states apart.

class _CompletedProbe:
    def __init__(self, returncode, stdout=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _probe_with(monkeypatch, returncode, stdout=""):
    monkeypatch.setattr(
        remote_play_client.subprocess, "run",
        lambda *a, **k: _CompletedProbe(returncode, stdout))
    return remote_play_client._probe_chiaki_client("OrionStream.exe")


def test_probe_reports_registered_console(monkeypatch):
    probe = _probe_with(monkeypatch, 0, "Host: Isaiahs PS5 \n")
    assert probe.nickname == "Isaiahs PS5"
    assert probe.client_ran is True
    assert probe.detail == ""


def test_probe_clean_exit_with_no_host_means_nothing_registered(monkeypatch):
    probe = _probe_with(monkeypatch, 0, "")
    assert probe.nickname == ""
    # The client RAN. An empty list here is real information, and the lobby fallback
    # (pair a console manually) is the correct response.
    assert probe.client_ran is True


def test_probe_loader_failure_is_not_reported_as_missing_registration(monkeypatch):
    # 0xC0000135 as Python surfaces it: the exact code seen on the rig.
    probe = _probe_with(monkeypatch, 3221225781, "")
    assert probe.nickname == ""
    assert probe.client_ran is False
    assert "STATUS_DLL_NOT_FOUND" in probe.detail
    assert "cannot start" in probe.detail


def test_probe_unmapped_crash_still_counts_as_unrunnable(monkeypatch):
    probe = _probe_with(monkeypatch, 0xC0000374 - (1 << 32), "")  # heap corruption
    assert probe.client_ran is False
    assert "0xC0000374" in probe.detail


def test_probe_timeout_keeps_the_historical_lobby_fallback(monkeypatch):
    def _boom(*a, **k):
        raise remote_play_client.subprocess.TimeoutExpired(cmd="list", timeout=4.0)
    monkeypatch.setattr(remote_play_client.subprocess, "run", _boom)
    probe = remote_play_client._probe_chiaki_client("OrionStream.exe")
    # A slow machine must NOT be newly hard-blocked; only unambiguous loader deaths are.
    assert probe.client_ran is True
    assert probe.nickname == ""


def test_launch_refuses_and_explains_when_client_cannot_run(monkeypatch, tmp_path):
    spawned = []
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: None)
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(tmp_path / "OrionStream.exe"))
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe(
            "", False, "the Remote Play client cannot start: a DLL it needs is missing "
                       "from the client folder (STATUS_DLL_NOT_FOUND) [exit 0xC0000135]"))
    monkeypatch.setattr(remote_play_client.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a) or _Process(None))

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip="1.2.3.4"))
    status = manager._launch("chiaki")

    assert status.ok is False
    # Names the real fault and explicitly clears the console/registration of blame.
    assert "STATUS_DLL_NOT_FOUND" in status.message
    assert "not a console" in status.message
    # And it must not burn the readiness deadline launching a client that cannot run.
    assert spawned == []


# ---------------------------------------------------------------------------
# Standby client pool - "pre-booted client, deferred session"
# ---------------------------------------------------------------------------

def _fast_full_clock(monkeypatch):
    """time.time + time.monotonic advance together; sleep is a no-op."""
    clock = {"now": 0.0}

    def now():
        clock["now"] += 0.5
        return clock["now"]

    monkeypatch.setattr(remote_play_client.time, "time", now)
    monkeypatch.setattr(remote_play_client.time, "monotonic", now)
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _seconds: None)
    return clock


# Content for fake STANDBY-CAPABLE client executables: the pool's spawn-free
# capability sniff requires the control-pipe marker (a real pre-standby binary
# lacks it and must never be spawned with --standby — it would raise a modal
# parser-error dialog and linger, verified 2026-08-30 on the real old binary).
_MARKED_CLIENT_BYTES = (
    b"MZ fake orion client \x00"
    + remote_play_client._STANDBY_CAPABILITY_MARKER
    + b"\x00 tail")


class _StandbyChild:
    def __init__(self, pid=4321):
        self.pid = pid
        self._handle = pid + 100_000
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 1

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        if self.returncode is None:
            raise remote_play_client.subprocess.TimeoutExpired("standby", 0)
        return self.returncode


def _pooled_standby(pool, tmp_path, *, nickname="registered-console", pid=4321,
                    disable_video=False, sha="", size=-1):
    executable = tmp_path / "OrionStream.exe"
    if not executable.exists():
        executable.write_bytes(b"standby-client")
    child = _StandbyChild(pid)
    client = remote_play_client.StandbyClient(
        process=child,
        pipe_name="orion_standby_test",
        pipe_path="\\\\.\\pipe\\orion_standby_test",
        executable_path=str(executable),
        nickname=nickname,
        host="1.2.3.4",
        disable_video=disable_video,
        identity_sha256=sha,
        identity_size=size,
        spawned_at=0.0,
    )
    pool._client = client
    return client


def _standby_manager(tmp_path, **overrides):
    cfg = dict(
        console_ip="1.2.3.4",
        close_on_stop=True,
        require_session_ready=True,
        session_log_dir=str(tmp_path),
        wait_timeout_s=3.0,
    )
    cfg.update(overrides)
    return remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(**cfg))


def _wire_standby_env(monkeypatch, tmp_path, pool, *, nickname="registered-console"):
    monkeypatch.setenv("ORION_INPUT_HOOK", "1")
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    if not executable.exists():
        executable.write_bytes(b"standby-client")
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe(nickname, True, ""))
    monkeypatch.setattr(remote_play_client, "get_standby_pool", lambda: pool)
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (0, ""))
    monkeypatch.setattr(remote_play_client, "windows_process_creation_time_100ns",
                        lambda handle: int(handle or 0) * 10)
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process", lambda _pid: None)


def test_standby_promote_skips_sweep_and_reuses_prebooted_child(monkeypatch, tmp_path):
    _fast_full_clock(monkeypatch)
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)
    _wire_standby_env(monkeypatch, tmp_path, pool)
    sweeps = []
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes",
                        lambda: sweeps.append("sweep"))
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(client.process.pid, "orionstream.exe")])
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    commands = []

    def transact(_pipe, command, timeout_s=2.0, **_cancel):
        del timeout_s
        commands.append(command)
        if command == "ping":
            return "ok standby"
        if command.startswith("open "):
            # The promoted child opens its session -> a FRESH launch-scoped log.
            log = tmp_path / "chiaki_session_2026-01-01_00-00-09.log"
            log.write_bytes(b"Starting session request\n"
                            + remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
            return "ok opening"
        return "err unknown"

    monkeypatch.setattr(remote_play_client, "_standby_pipe_transact", transact)

    manager = _standby_manager(tmp_path)
    status = manager.ensure_running()

    assert status.ok is True
    assert status.session_ready is True
    assert status.pid == client.process.pid
    # The promote must carry the CURRENT console ip and be preceded by the ping proof.
    assert commands == ["ping", "open 1.2.3.4"]
    # No broad taskkill sweep ran: the scoped sole-pid check stood in for it.
    assert sweeps == []
    # The pool no longer owns the child; the manager does, generation-scoped.
    assert pool.state() == "absent"
    identity = manager.owned_process_identity()
    assert identity["pid"] == client.process.pid
    assert identity["launch_generation"] == 1
    assert manager.last_stage_timings.get("standby") == 1
    assert "standby_promote_ms" in manager.last_stage_timings


def test_standby_promote_tightens_readiness_poll_quantum(monkeypatch, tmp_path):
    """The promote path polls the (already-launched) marker at 10ms; the cold
    path keeps the measured 25ms quantum (2026-08-12 decision) untouched."""
    _fast_full_clock(monkeypatch)
    sleeps = []
    monkeypatch.setattr(remote_play_client.time, "sleep",
                        lambda s: sleeps.append(round(float(s), 3)))
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)
    _wire_standby_env(monkeypatch, tmp_path, pool)
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: None)
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(client.process.pid, "orionstream.exe")])
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)

    def transact(_pipe, command, timeout_s=2.0, **_cancel):
        del timeout_s
        if command == "ping":
            return "ok standby"
        if command.startswith("open "):
            # Session opens but the readiness marker never lands: the loop must
            # keep polling (at the tightened quantum) until the deadline.
            (tmp_path / "chiaki_session_2026-01-01_00-00-09.log").write_bytes(
                b"Starting session request\n")
            return "ok opening"
        return "err unknown"

    monkeypatch.setattr(remote_play_client, "_standby_pipe_transact", transact)
    manager = _standby_manager(tmp_path)
    status = manager.ensure_running()
    assert status.ok is False  # marker never landed; only the cadence is under test
    loop_sleeps = [s for s in sleeps if s in (0.01, 0.025)]
    assert loop_sleeps and set(loop_sleeps) == {0.01}

    # Cold path (no standby available): the 25ms quantum stands.
    sleeps.clear()
    empty_pool = remote_play_client.StandbyClientPool()
    monkeypatch.setattr(remote_play_client, "get_standby_pool", lambda: empty_pool)
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: [])
    cold = _standby_manager(tmp_path)
    monkeypatch.setattr(cold, "_launch", lambda _mode:
                        remote_play_client.RemotePlayClientStatus(
                            ok=True, mode="chiaki", pid=7777, message="launched"))
    status = cold.ensure_running()
    assert status.ok is False
    loop_sleeps = [s for s in sleeps if s in (0.01, 0.025)]
    assert loop_sleeps and set(loop_sleeps) == {0.025}


def test_standby_open_refusal_falls_back_to_cold_spawn(monkeypatch, tmp_path):
    _fast_full_clock(monkeypatch)
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)
    _wire_standby_env(monkeypatch, tmp_path, pool)
    sweeps = []
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes",
                        lambda: sweeps.append("sweep"))
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(client.process.pid, "orionstream.exe")])
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    monkeypatch.setattr(
        remote_play_client, "_standby_pipe_transact",
        lambda _pipe, command, timeout_s=2.0, **_cancel:
            "ok standby" if command == "ping" else "err open-failed")

    manager = _standby_manager(tmp_path)

    def cold_launch(_mode):
        log = tmp_path / "chiaki_session_2026-01-01_00-00-10.log"
        log.write_bytes(remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
        manager._process = _StandbyChild(pid=9000)
        return remote_play_client.RemotePlayClientStatus(
            ok=True, mode="chiaki", pid=9000, message="Started chiaki")

    monkeypatch.setattr(manager, "_launch", cold_launch)
    status = manager.ensure_running()

    assert status.ok is True
    assert status.pid == 9000
    # The refused standby was killed and the cold path swept before spawning.
    assert client.process.terminated or client.process.killed
    assert sweeps == ["sweep"]
    assert "standby" not in manager.last_stage_timings


def test_standby_falls_back_when_other_chiaki_processes_are_running(monkeypatch, tmp_path):
    _fast_full_clock(monkeypatch)
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)
    _wire_standby_env(monkeypatch, tmp_path, pool)
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: None)
    monkeypatch.setattr(
        remote_play_client, "_running_image_pids",
        lambda _targets: [(client.process.pid, "orionstream.exe"), (777, "chiaki.exe")])
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    monkeypatch.setattr(
        remote_play_client, "_standby_pipe_transact",
        lambda _pipe, command, timeout_s=2.0, **_cancel: "ok standby" if command == "ping" else "ok opening")

    manager = _standby_manager(tmp_path)
    launched = []
    monkeypatch.setattr(manager, "_launch", lambda _mode: launched.append(1) or
                        remote_play_client.RemotePlayClientStatus(
                            ok=False, mode="chiaki", message="cold path reached"))
    status = manager.ensure_running()

    assert status.ok is False
    assert launched == [1]
    # A stale sibling means the standby is NOT trusted: killed, cold path owns cleanup.
    assert client.process.terminated or client.process.killed


def test_standby_not_ready_within_claim_budget_falls_back(monkeypatch, tmp_path):
    _fast_full_clock(monkeypatch)
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)
    _wire_standby_env(monkeypatch, tmp_path, pool)
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: None)
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(client.process.pid, "orionstream.exe")])
    # Pool state() must see it alive-but-booting, claim must then time out.
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: False)

    manager = _standby_manager(tmp_path)
    launched = []
    monkeypatch.setattr(manager, "_launch", lambda _mode: launched.append(1) or
                        remote_play_client.RemotePlayClientStatus(
                            ok=False, mode="chiaki", message="cold path reached"))
    status = manager.ensure_running()

    assert status.ok is False
    assert launched == [1]
    assert client.process.terminated or client.process.killed
    assert pool.state() == "absent"


def test_standby_claim_rejects_nickname_and_route_changes(monkeypatch, tmp_path):
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    monkeypatch.setattr(remote_play_client, "_standby_pipe_transact",
                        lambda *_a, **_k: "ok standby")
    executable = str(tmp_path / "OrionStream.exe")

    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path, nickname="console-A")
    assert pool.claim(nickname="console-B", disable_video=False,
                      executable_path=executable) is None
    assert client.process.terminated

    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path, disable_video=False)
    assert pool.claim(nickname="registered-console", disable_video=True,
                      executable_path=executable) is None
    assert client.process.terminated

    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path, sha="a" * 64, size=14)
    assert pool.claim(nickname="registered-console", disable_video=False,
                      executable_path=executable,
                      identity_sha256="b" * 64, identity_size=14) is None
    assert client.process.terminated

    # And an exact match hands the client over, emptying the pool.
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path, sha="a" * 64, size=14)
    claimed = pool.claim(nickname="registered-console", disable_video=False,
                         executable_path=executable,
                         identity_sha256="a" * 64, identity_size=14)
    assert claimed is client
    assert pool.state() == "absent"


def test_standby_pool_refuses_to_spawn_beside_other_clients(monkeypatch, tmp_path):
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"standby-client")
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(31337, "chiaki.exe")])
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "busy"
    # An unanswerable snapshot is also a refusal (fail-safe, never spawn blind).
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: None)
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "busy"


def test_standby_pool_spawn_wires_flag_env_and_hidden_window(monkeypatch, tmp_path):
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(_MARKED_CLIENT_BYTES)
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: [])
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe("registered-console", True, ""))
    spawns = []

    def popen(cmd, cwd=None, env=None, startupinfo=None, **_kwargs):
        spawns.append((cmd, env, startupinfo))
        return _StandbyChild(5555)

    monkeypatch.setattr(remote_play_client.subprocess, "Popen", popen)
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4", disable_video=True) == "spawned"
    assert pool.ensure_spawned(console_ip="1.2.3.4", disable_video=True) == "present"
    assert len(spawns) == 1
    cmd, env, startupinfo = spawns[0]
    # Options MUST precede positionals (ParseAsPositionalArguments). A pre-standby
    # client would NOT exit on the unknown option (it raises a modal parser-error
    # dialog and lingers — verified 2026-08-30); the capability sniff keeps one
    # from ever being spawned, so reaching Popen here implies a marked binary.
    assert cmd[1] == "--standby"
    assert cmd[2:] == ["stream", "registered-console", "1.2.3.4"]
    assert env["CHIAKI_ORION_STANDBY_PIPE"].startswith("orion_standby_")
    assert "SDL_GAMECONTROLLER_IGNORE_DEVICES" in env
    assert env["CHIAKI_ORION_DISABLE_VIDEO"] == "1"
    assert startupinfo is not None and startupinfo.wShowWindow == 0


def test_standby_pool_marks_unsupported_binary_with_ttl(monkeypatch, tmp_path):
    clock = {"now": 1000.0}
    monkeypatch.setattr(remote_play_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    # Marker-bearing so the sniff passes: this latch is the defence for a
    # STANDBY-CAPABLE binary that still dies young with an error (missing DLL,
    # loader failure) — the marker-less pre-standby case never reaches Popen.
    executable.write_bytes(_MARKED_CLIENT_BYTES)
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: [])
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe("registered-console", True, ""))
    children = []

    def popen(*_a, **_k):
        child = _StandbyChild(6000 + len(children))
        children.append(child)
        return child

    monkeypatch.setattr(remote_play_client.subprocess, "Popen", popen)
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"
    # The doomed client exits 1 almost immediately, before ever becoming ready.
    clock["now"] += 1.0
    children[0].returncode = 1
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "unsupported"
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "unsupported"
    assert len(children) == 1
    # The verdict EXPIRES so a false latch (e.g. a sweep-killed young standby,
    # whose victims also exit 1) self-heals without a binary change.
    clock["now"] += remote_play_client._STANDBY_UNSUPPORTED_TTL_S + 1.0
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"
    assert len(children) == 2


def test_standby_pool_does_not_mark_unsupported_after_ready_was_seen(monkeypatch, tmp_path):
    clock = {"now": 1000.0}
    monkeypatch.setattr(remote_play_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(_MARKED_CLIENT_BYTES)
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: [])
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe("registered-console", True, ""))
    children = []

    def popen(*_a, **_k):
        child = _StandbyChild(6100 + len(children))
        children.append(child)
        return child

    monkeypatch.setattr(remote_play_client.subprocess, "Popen", popen)
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"
    assert pool.state() == "ready"  # marks ready_seen
    # A young READY standby killed by a broad sweep exits 1 too - that must NOT
    # latch the binary as unsupported.
    clock["now"] += 2.0
    children[0].returncode = 1
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"
    assert len(children) == 2


def test_standby_pool_never_spawns_at_a_pre_standby_binary(monkeypatch, tmp_path):
    """A binary without the control-pipe marker must never be spawned with
    --standby: the real pre-standby client raises a MODAL parser-error dialog
    (class #32770, title "Chiaki") and lingers un-exited on the user's desktop
    instead of failing with an exit code (measured 2026-08-30). The sniff is
    the only layer that prevents the dialog, so it must fire BEFORE Popen."""
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"old client without the standby control pipe")
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: [])
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe("registered-console", True, ""))

    def popen(*_a, **_k):
        raise AssertionError("--standby must never be spawned at a pre-standby binary")

    monkeypatch.setattr(remote_play_client.subprocess, "Popen", popen)
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "no-standby-support"
    # Cached by fingerprint: the second refusal must not rescan or spawn.
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "no-standby-support"

    # Replacing the binary with a standby-capable one (new size/mtime => new
    # fingerprint) self-heals without a process restart.
    executable.write_bytes(_MARKED_CLIENT_BYTES)
    monkeypatch.setattr(remote_play_client.subprocess, "Popen",
                        lambda *a, **k: _StandbyChild(6200))
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"


def test_standby_pool_busy_refusal_wins_over_capability_sniff(monkeypatch, tmp_path):
    """Beside another chiaki-family process the pool must answer "busy" without
    doing capability I/O: the refusal order is part of the contract (the sniff
    only runs when a spawn was otherwise due)."""
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"markerless, but that must not matter here")
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(31337, "chiaki.exe")])
    pool = remote_play_client.StandbyClientPool()
    sniffs = []
    monkeypatch.setattr(pool, "_binary_supports_standby",
                        lambda *_a, **_k: sniffs.append(1) or True)
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "busy"
    assert not sniffs


def test_standby_pool_boot_deadline_kills_and_latches_a_wedged_client(monkeypatch, tmp_path):
    """A client that passes the sniff but never creates its control pipe (e.g.
    wedged behind a dialog or a hung boot) is killed at the boot deadline and
    its binary latched unsupported (TTL), so the prewarm loop stops feeding a
    wedge. The claim path's own 2s budget already covers the Connect side."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(remote_play_client.time, "monotonic", lambda: clock["now"])
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(_MARKED_CLIENT_BYTES)
    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _targets: [])
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe("registered-console", True, ""))
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: False)
    children = []

    def popen(*_a, **_k):
        child = _StandbyChild(6300 + len(children))
        children.append(child)
        return child

    monkeypatch.setattr(remote_play_client.subprocess, "Popen", popen)
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"
    # Inside the deadline the client is merely "booting" — no kill, no latch.
    clock["now"] += remote_play_client._STANDBY_BOOT_DEADLINE_S - 1.0
    assert pool.state() == "booting"
    assert children[0].returncode is None
    # Past the deadline: killed, pool empty, binary latched with the TTL.
    clock["now"] += 2.0
    assert pool.state() == "absent"
    assert children[0].terminated or children[0].killed
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "unsupported"
    assert len(children) == 1
    clock["now"] += remote_play_client._STANDBY_UNSUPPORTED_TTL_S + 1.0
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "spawned"
    assert len(children) == 2


def test_standby_disabled_env_keeps_cold_path_untouched(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_STANDBY_CLIENT", "0")
    pool = remote_play_client.StandbyClientPool()
    assert pool.ensure_spawned(console_ip="1.2.3.4") == "disabled"

    _fast_full_clock(monkeypatch)
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (0, ""))
    claimed = []
    monkeypatch.setattr(pool, "claim", lambda **kwargs: claimed.append(kwargs) or None)
    monkeypatch.setattr(remote_play_client, "get_standby_pool", lambda: pool)
    manager = _standby_manager(tmp_path, close_on_stop=False)
    monkeypatch.setattr(manager, "_launch", lambda _mode: remote_play_client.RemotePlayClientStatus(
        ok=False, mode="chiaki", message="cold path reached"))
    status = manager.ensure_running()
    assert status.ok is False
    assert claimed == []


def test_standby_pool_shutdown_quits_then_kills(monkeypatch, tmp_path):
    commands = []

    def transact(_pipe, command, timeout_s=2.0, **_cancel):
        del timeout_s
        commands.append(command)
        return "ok quitting"

    monkeypatch.setattr(remote_play_client, "_standby_pipe_transact", transact)
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)

    def wait(timeout=None):
        del timeout
        # Simulates the clean exit after the quit command.
        client.process.returncode = 0
        return 0

    client.process.wait = wait
    pool.shutdown()
    assert commands == ["quit"]
    assert client.process.poll() == 0
    assert pool.state() == "absent"


def test_standby_wake_refusal_is_terminal_and_discards_standby(monkeypatch, tmp_path):
    _fast_full_clock(monkeypatch)
    pool = remote_play_client.StandbyClientPool()
    client = _pooled_standby(pool, tmp_path)
    _wire_standby_env(monkeypatch, tmp_path, pool)
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes", lambda: None)
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _targets: [(client.process.pid, "orionstream.exe")])
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    opens = []
    monkeypatch.setattr(
        remote_play_client, "_standby_pipe_transact",
        lambda _pipe, command, timeout_s=2.0, **_cancel:
            opens.append(command) or ("ok standby" if command == "ping" else "ok opening"))

    manager = _standby_manager(tmp_path)
    refusal = remote_play_client.RemotePlayClientStatus(
        ok=False, mode="chiaki",
        message="The PS5 is in rest mode ... press Connect again in a moment")
    monkeypatch.setattr(manager, "_ensure_console_awake",
                        lambda _nick, _host, _path: refusal)
    launched = []
    monkeypatch.setattr(manager, "_launch", lambda _mode: launched.append(1))

    status = manager.ensure_running()

    # Terminal refusal: identical verdict to the cold path, no open ever sent,
    # no cold spawn attempted, and the standby did not linger for the sweep.
    assert status is refusal
    assert launched == []
    assert opens == ["ping"]
    assert client.process.terminated or client.process.killed
