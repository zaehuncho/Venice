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
