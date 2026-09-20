"""Warm-promote connect-latency fixes (2026-08-29).

Measured live: pressing Connect with a warm preview took 5.28s to input-ready, of
which ~1.0s was OUR prep — dominated by an unconditional 4x-taskkill + 4x-tasklist
stale sweep (~920ms measured with nothing running) plus a per-connect `chiaki list`
subprocess probe (~75ms). These tests pin the fixes:

  1. terminate_chiaki_processes() answers "anything to kill?" with ONE in-process
     toolhelp snapshot and returns immediately when the answer is no; a failed
     snapshot falls back to the legacy spawning sweep (the fixed-named-pipe-freeing
     guarantee must never rest on a failed probe).
  2. The `chiaki list` probe result is cached, keyed by a fingerprint of the exact
     client install (exe path/size/mtime + DLL set) and the registered-hosts
     registry stamp; failures and empty nicknames are never cached.
  3. ensure_running() records a factual per-stage timing breakdown.
  4. The session-ready readiness poll no longer runs the EnumWindows scan every
     25ms iteration (the window is bookkeeping there, not the readiness authority).
"""
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote_play_client


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    remote_play_client._PROBE_CACHE.clear()
    yield
    remote_play_client._PROBE_CACHE.clear()


# ---------------------------------------------------------------------------
# 1. stale sweep fast path
# ---------------------------------------------------------------------------

def _record_spawns(monkeypatch):
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))

        class _R:
            stdout = ""
            returncode = 0
        return _R()

    monkeypatch.setattr(remote_play_client.subprocess, "run", fake_run)
    return calls


def test_sweep_spawns_nothing_when_no_target_processes_run(monkeypatch):
    calls = _record_spawns(monkeypatch)
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _names: [])
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)

    started = time.perf_counter()
    remote_play_client.terminate_chiaki_processes()
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    assert calls == []                     # no taskkill/tasklist subprocess at all
    assert elapsed_ms < 250.0              # the ~920ms sweep is gone


def test_sweep_falls_back_to_legacy_spawns_when_snapshot_unavailable(monkeypatch):
    calls = _record_spawns(monkeypatch)
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _names: None)
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)

    remote_play_client.terminate_chiaki_processes()

    taskkills = [c for c in calls if c and "taskkill.exe" in c[0]]
    killed = {c[2] for c in taskkills}
    # Fail-safe: with no probe answer, every image name is swept exactly as before.
    assert killed == set(remote_play_client._CHIAKI_IMAGE_NAMES)
    # And the legacy tasklist verification ran (fake stdout says nothing is running).
    assert any(c and "tasklist.exe" in c[0] for c in calls)


def test_sweep_kills_only_the_images_actually_running(monkeypatch):
    calls = _record_spawns(monkeypatch)
    snapshots = [[(4242, "orionstream.exe")], []]
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _names: snapshots.pop(0))
    monkeypatch.setattr(remote_play_client, "_terminate_pids_in_process",
                        lambda pids, wait_s=1.5: ([p for p, _n in pids], []))
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _s: None)

    report = remote_play_client.terminate_chiaki_processes()

    # 2026-09-14: the kill is in-process and PID-scoped; nothing is spawned.
    assert calls == []
    assert report["found"] == ["orionstream.exe:4242"]
    assert report["killed"] == [4242]
    assert report["fallback"] is False


@pytest.mark.skipif(os.name != "nt", reason="toolhelp snapshot is Windows-only")
def test_running_image_names_real_snapshot_finds_this_python():
    me = os.path.basename(sys.executable)
    found = remote_play_client._running_image_names((me, "OrionStream.exe"))
    assert found is not None, "snapshot API should be available on Windows"
    assert me.lower() in found
    # And a clean answer for the chiaki set (nothing should be running in CI).
    chiaki = remote_play_client._running_image_names(remote_play_client._CHIAKI_IMAGE_NAMES)
    assert chiaki is not None


# ---------------------------------------------------------------------------
# 2. probe cache
# ---------------------------------------------------------------------------

def _make_client_dir(tmp_path):
    exe = tmp_path / "OrionStream.exe"
    exe.write_bytes(b"fake exe")
    (tmp_path / "Qt6Core.dll").write_bytes(b"fake dll")
    return str(exe)


def _count_probes(monkeypatch, probe):
    calls = []
    monkeypatch.setattr(remote_play_client, "_probe_chiaki_client",
                        lambda path: calls.append(path) or probe)
    return calls


def test_probe_cache_reuses_successful_probe(monkeypatch, tmp_path):
    exe = _make_client_dir(tmp_path)
    monkeypatch.setattr(remote_play_client, "_registered_hosts_stamp", lambda: 42)
    good = remote_play_client.ChiakiClientProbe("Isaiahs PS5", True, "")
    calls = _count_probes(monkeypatch, good)

    first = remote_play_client._probe_chiaki_client_cached(exe)
    second = remote_play_client._probe_chiaki_client_cached(exe)

    assert first == good and second == good
    assert len(calls) == 1                 # second connect never spawned the client


def test_probe_cache_invalidated_by_exe_change(monkeypatch, tmp_path):
    exe = _make_client_dir(tmp_path)
    monkeypatch.setattr(remote_play_client, "_registered_hosts_stamp", lambda: 42)
    good = remote_play_client.ChiakiClientProbe("Isaiahs PS5", True, "")
    calls = _count_probes(monkeypatch, good)

    remote_play_client._probe_chiaki_client_cached(exe)
    with open(exe, "ab") as fh:            # rebuild: size/mtime change
        fh.write(b"!")
    remote_play_client._probe_chiaki_client_cached(exe)

    assert len(calls) == 2


def test_probe_cache_invalidated_by_dll_change(monkeypatch, tmp_path):
    # The 2026-08-13 incident class: fresh exe, stale/changed DLL beside it.
    exe = _make_client_dir(tmp_path)
    monkeypatch.setattr(remote_play_client, "_registered_hosts_stamp", lambda: 42)
    good = remote_play_client.ChiakiClientProbe("Isaiahs PS5", True, "")
    calls = _count_probes(monkeypatch, good)

    remote_play_client._probe_chiaki_client_cached(exe)
    (tmp_path / "avcodec-61.dll").write_bytes(b"new dll")
    remote_play_client._probe_chiaki_client_cached(exe)

    assert len(calls) == 2


def test_probe_cache_invalidated_by_registration_change(monkeypatch, tmp_path):
    exe = _make_client_dir(tmp_path)
    stamps = iter([42, 42, 43])
    monkeypatch.setattr(remote_play_client, "_registered_hosts_stamp",
                        lambda: next(stamps))
    good = remote_play_client.ChiakiClientProbe("Isaiahs PS5", True, "")
    calls = _count_probes(monkeypatch, good)

    remote_play_client._probe_chiaki_client_cached(exe)   # stamp 42 -> cached
    remote_play_client._probe_chiaki_client_cached(exe)   # stamp 42 -> hit
    remote_play_client._probe_chiaki_client_cached(exe)   # stamp 43 -> re-probe

    assert len(calls) == 2


def test_probe_cache_never_caches_failures_or_empty_nicknames(monkeypatch, tmp_path):
    exe = _make_client_dir(tmp_path)
    monkeypatch.setattr(remote_play_client, "_registered_hosts_stamp", lambda: 42)

    dead = remote_play_client.ChiakiClientProbe("", False, "loader failure")
    calls = _count_probes(monkeypatch, dead)
    remote_play_client._probe_chiaki_client_cached(exe)
    remote_play_client._probe_chiaki_client_cached(exe)
    assert len(calls) == 2                 # failure re-probed every time

    empty = remote_play_client.ChiakiClientProbe("", True, "")
    calls = _count_probes(monkeypatch, empty)
    remote_play_client._probe_chiaki_client_cached(exe)
    remote_play_client._probe_chiaki_client_cached(exe)
    assert len(calls) == 2                 # nothing-registered re-probed every time


def test_probe_cache_disabled_for_unknown_paths(monkeypatch):
    good = remote_play_client.ChiakiClientProbe("Isaiahs PS5", True, "")
    calls = _count_probes(monkeypatch, good)
    remote_play_client._probe_chiaki_client_cached("Z:/does/not/exist/OrionStream.exe")
    remote_play_client._probe_chiaki_client_cached("Z:/does/not/exist/OrionStream.exe")
    assert len(calls) == 2                 # unstat-able path: never cached


# ---------------------------------------------------------------------------
# 3 + 4. stage timings and the throttled readiness-poll window scan
# ---------------------------------------------------------------------------

def _ready_manager(monkeypatch, tmp_path, waiting_polls):
    """Manager whose tracker reports 'waiting' N times then latches 'ready'."""
    monkeypatch.setenv("ORION_INPUT_HOOK", "1")
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    sweep_calls = []
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes",
                        lambda: sweep_calls.append(1))
    scan_calls = []

    def fake_scan(_title=""):
        scan_calls.append(1)
        return (777, "Orion Stream") if len(scan_calls) > 1 else (0, "")

    monkeypatch.setattr(remote_play_client, "find_remote_play_window", fake_scan)
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process", lambda _pid: None)

    cfg = remote_play_client.RemotePlayClientConfig(
        console_ip="192.0.2.9", require_session_ready=True,
        session_log_dir=str(tmp_path), wait_timeout_s=8.0)
    manager = remote_play_client.RemotePlayClientManager(cfg)

    state = {"polls": 0}

    def fake_poll():
        state["polls"] += 1
        if state["polls"] > waiting_polls:
            return "ready", str(tmp_path / "chiaki_session_x.log")
        return "waiting", (str(tmp_path / "chiaki_session_x.log")
                           if state["polls"] > 2 else "")

    monkeypatch.setattr(manager._session_tracker, "poll", fake_poll)
    monkeypatch.setattr(
        manager, "_launch",
        lambda _mode: remote_play_client.RemotePlayClientStatus(
            ok=True, mode="chiaki", path="OrionStream.exe", pid=4242,
            message="Started chiaki"))
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _s: None)
    return manager, scan_calls, state


def test_stage_timings_recorded_and_summary_readable(monkeypatch, tmp_path):
    manager, _scans, _state = _ready_manager(monkeypatch, tmp_path, waiting_polls=5)
    status = manager.ensure_running()

    assert status.ok and status.session_ready
    stages = manager.last_stage_timings
    for key in ("stale_sweep_ms", "prelaunch_scan_ms", "launch_total_ms",
                "client_boot_ms", "handshake_ms", "ready_wait_ms"):
        assert key in stages, f"missing stage {key}"
        assert stages[key] >= 0.0
    summary = manager.stage_summary()
    assert "stale_sweep=" in summary and "ready_wait=" in summary


def test_session_ready_poll_throttles_window_scan(monkeypatch, tmp_path):
    # 400 waiting polls with sleep no-oped: wall time stays far below the 100ms
    # scan interval, so the readiness loop must run the EnumWindows scan only a
    # handful of times while the marker poll runs every iteration.
    manager, scan_calls, state = _ready_manager(monkeypatch, tmp_path,
                                                waiting_polls=400)
    status = manager.ensure_running()

    assert status.ok and status.session_ready
    assert state["polls"] >= 400
    # prelaunch scan + first loop scan + the final status.hwnd rescue scan,
    # plus at most a few 100ms-interval rescans if the box stalls.
    assert len(scan_calls) <= 10, f"window scan ran {len(scan_calls)} times"
    assert status.hwnd == 777              # rescue scan still populated the hwnd


def test_window_only_mode_keeps_per_iteration_scan(monkeypatch, tmp_path):
    monkeypatch.delenv("ORION_INPUT_HOOK", raising=False)
    monkeypatch.delenv("ORION_FRAME_PIPE", raising=False)
    scan_results = iter([(0, "")] * 3 + [(555, "Chiaki")] * 50)
    scans = []

    def fake_scan(_title=""):
        scans.append(1)
        return next(scan_results)

    monkeypatch.setattr(remote_play_client, "find_remote_play_window", fake_scan)
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process", lambda _pid: None)
    cfg = remote_play_client.RemotePlayClientConfig(
        require_session_ready=False, session_log_dir=str(tmp_path),
        wait_timeout_s=5.0)
    manager = remote_play_client.RemotePlayClientManager(cfg)
    monkeypatch.setattr(
        manager, "_launch",
        lambda _mode: remote_play_client.RemotePlayClientStatus(
            ok=True, mode="chiaki", path="chiaki.exe", pid=1,
            message="Started chiaki"))
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _s: None)

    status = manager.ensure_running()
    # Window IS the readiness signal here: found on the 4th scan (3 waiting
    # iterations were not throttled away).
    assert status.ok and status.hwnd == 555
    assert len(scans) >= 4


# ---------------------------------------------------------------------------
# sidecar surfaces the timing line
# ---------------------------------------------------------------------------

def _load_sidecar():
    import importlib.util
    sc = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "native_orion", "backend", "autogreen_sidecar.py")
    spec = importlib.util.spec_from_file_location("_autogreen_sidecar_latency_ut", sc)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_start_stream_logs_factual_stage_timing(monkeypatch):
    mod = _load_sidecar()
    events = []
    logs = []
    monkeypatch.setattr(mod, "_emit", lambda payload: events.append(payload))
    monkeypatch.setattr(mod, "_log", lambda msg, *a, **kw: logs.append(str(msg)))

    class _Orch:
        last_promotion_stage_summary = (
            "stale_sweep=2ms probe=0ms identity=6ms spawn=2ms "
            "client_boot=1005ms handshake=591ms ready_wait=1650ms")

        def promote_to_stream(self, console_ip=None, console_identity=None):
            return True

        def input_link_ready(self):
            return True

    assert mod._handle_start_stream(_Orch(), {"console_ip": "1.2.3.4"}) is True
    timing_lines = [line for line in logs if line.startswith("start_stream timing:")]
    assert len(timing_lines) == 1
    assert "total=" in timing_lines[0]
    assert "handshake=591ms" in timing_lines[0]
    # The verdict is still the explicit contract — timing is purely informational.
    assert events[-1]["event"] == "started" and events[-1]["input_ready"] is True
