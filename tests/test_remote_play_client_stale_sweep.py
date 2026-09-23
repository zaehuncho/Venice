"""First-connect stall: the 3s `stale_sweep` was never the sweep (2026-09-14).

Every first connect of an app run since 09-13 logged

    start_stream timing: total=4680ms | stale_sweep=3020ms prelaunch_scan=5ms
    binary_resolve=0ms probe=1ms wake_check=1ms identity=6ms spawn=2ms
    launch_total=12ms client_boot=990ms handshake=632ms ready_wait=1622ms

with, 2.4s into that window,

    ERROR RemotePlayClient: CONSOLE ADDRESS DRIFT: configured 192.168.137.126 is
    silent; discovery found the console at 192.168.137.81 (ready)

`ensure_running()` took ONE perf-counter mark at function entry and did not reset
it before `terminate_chiaki_processes()`, so the mark spanned the whole
`_promote_standby()` attempt — and inside it `_resolve_console_host()`, which
pays 0.4s + 0.8s + 1.2s of real socket timeouts whenever the configured console
ip is silent. The measured sweep with nothing running is ~12ms.

These tests pin the four fixes:
  1. stale_sweep_ms times ONLY the sweep; the standby attempt and the console
     host resolution are their own stages.
  2. The sweep spawns nothing when the snapshot says nothing is running, and
     kills by PID in-process when something is.
  3. The spawning taskkill/tasklist sweep survives only as the fallback.
  4. A drifted console retargets the standby promote instead of throwing the
     pre-booted client away, and the drift resolution is prewarmed off the
     connect path.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import remote_play_client


@pytest.fixture(autouse=True)
def _clean_caches():
    remote_play_client._PROBE_CACHE.clear()
    remote_play_client.reset_console_host_prewarm()
    yield
    remote_play_client._PROBE_CACHE.clear()
    remote_play_client.reset_console_host_prewarm()


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


# ---------------------------------------------------------------------------
# 1. the sweep itself
# ---------------------------------------------------------------------------

def test_sweep_with_nothing_running_spawns_nothing_and_reports_it(monkeypatch):
    calls = _record_spawns(monkeypatch)
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _n: [])

    report = remote_play_client.terminate_chiaki_processes()

    assert calls == []
    assert report["found"] == [] and report["killed"] == []
    assert report["fallback"] is False
    assert report["ms"] < 250.0
    assert remote_play_client.last_sweep_report()["found"] == []


def test_found_pid_is_terminated_in_process_within_budget(monkeypatch):
    calls = _record_spawns(monkeypatch)
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)
    snapshots = [[(9001, "orionstream.exe"), (9002, "chiaki.exe")], []]
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _n: snapshots.pop(0))
    killed_with = []

    def fake_kill(pids, wait_s=1.5):
        killed_with.append((list(pids), wait_s))
        return [pid for pid, _name in pids], []

    monkeypatch.setattr(remote_play_client, "_terminate_pids_in_process", fake_kill)

    report = remote_play_client.terminate_chiaki_processes()

    assert calls == []                               # no taskkill.exe, no tasklist.exe
    assert killed_with[0][0] == [(9001, "orionstream.exe"), (9002, "chiaki.exe")]
    assert report["killed"] == [9001, 9002]
    assert report["found"] == ["orionstream.exe:9001", "chiaki.exe:9002"]
    assert report["fallback"] is False
    assert report["ms"] < 500.0


def test_fallback_spawns_only_when_the_in_process_kill_fails(monkeypatch):
    calls = _record_spawns(monkeypatch)
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _s: None)
    # The image never clears, so both in-process rounds fail.
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _n: [(9001, "orionstream.exe")])
    monkeypatch.setattr(remote_play_client, "_terminate_pids_in_process",
                        lambda pids, wait_s=1.5: ([], [p for p, _n in pids]))

    report = remote_play_client.terminate_chiaki_processes()

    assert report["fallback"] is True
    taskkills = [c for c in calls if c and "taskkill.exe" in c[0]]
    assert taskkills, "the legacy sweep must still back the pipe-freeing guarantee"
    # Scoped to the image that is actually running, exactly as before.
    assert {c[2] for c in taskkills} == {"OrionStream.exe"}


def test_snapshot_failure_goes_straight_to_the_legacy_sweep(monkeypatch):
    calls = _record_spawns(monkeypatch)
    monkeypatch.setattr(remote_play_client.os, "name", "nt", raising=False)
    monkeypatch.setattr(remote_play_client, "_running_image_pids", lambda _n: None)

    report = remote_play_client.terminate_chiaki_processes()

    assert report["fallback"] is True
    assert {c[2] for c in calls if c and "taskkill.exe" in c[0]} == set(
        remote_play_client._CHIAKI_IMAGE_NAMES)


@pytest.mark.skipif(os.name != "nt", reason="Win32 process kill")
def test_in_process_kill_never_touches_this_process():
    killed, failed = remote_play_client._terminate_pids_in_process(
        [(os.getpid(), "orionstream.exe"), (4, "orionstream.exe"),
         (0, "orionstream.exe")], wait_s=0.1)
    assert killed == [] and failed == []


@pytest.mark.skipif(os.name != "nt", reason="toolhelp snapshot is Windows-only")
def test_real_snapshot_and_sweep_are_cheap_with_nothing_to_kill():
    import time
    running = remote_play_client._running_image_pids(remote_play_client._CHIAKI_IMAGE_NAMES)
    assert running is not None
    # [CL3-F3-001 2026-09-23] Check BEFORE sweeping. This test used to call the REAL sweep first
    # and skip afterwards, so a suite run during a play session TerminateProcess(..., 1)'d the
    # owner's live OrionStream: the 2026-09-23T02:28:59Z "mid-game disconnect" was this test.
    if running:
        pytest.skip(f"a chiaki-family process is running here: {sorted(running)}")
    started = time.perf_counter()
    report = remote_play_client.terminate_chiaki_processes()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if report["found"]:
        pytest.skip(f"a chiaki-family process started during the sweep: {report['found']}")
    assert report["fallback"] is False
    assert elapsed_ms < 250.0, f"sweep cost {elapsed_ms:.0f}ms with nothing to kill"


# ---------------------------------------------------------------------------
# 2. the mis-attribution that hid it
# ---------------------------------------------------------------------------

def test_standby_attempt_is_not_charged_to_the_stale_sweep(monkeypatch, tmp_path):
    """The exact 09-13 regression: a slow _promote_standby() must not appear as
    stale_sweep. Before the fix this test recorded stale_sweep >= 300ms."""
    monkeypatch.setenv("ORION_INPUT_HOOK", "1")
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    monkeypatch.setenv("ORION_PS5_DISCOVER_DRIFT", "0")

    clock = {"t": 0.0}
    monkeypatch.setattr(remote_play_client.time, "perf_counter",
                        lambda: clock["t"])
    monkeypatch.setattr(remote_play_client.time, "sleep", lambda _s: None)

    def slow_standby(_stages):
        clock["t"] += 3.0        # the console-drift discovery scan
        return None

    monkeypatch.setattr(
        remote_play_client.RemotePlayClientManager, "_promote_standby",
        lambda self, stages: slow_standby(stages))
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes",
                        lambda: {"found": [], "killed": [], "failed": [],
                                 "fallback": False, "ms": 0.0})
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (0, ""))
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process", lambda _p: None)

    cfg = remote_play_client.RemotePlayClientConfig(
        console_ip="192.0.2.9", require_session_ready=True, close_on_stop=True,
        session_log_dir=str(tmp_path), wait_timeout_s=5.0)
    manager = remote_play_client.RemotePlayClientManager(cfg)
    monkeypatch.setattr(
        manager, "_launch",
        lambda _mode: remote_play_client.RemotePlayClientStatus(
            ok=True, mode="chiaki", path="OrionStream.exe", pid=1,
            message="Started chiaki"))
    monkeypatch.setattr(manager._session_tracker, "poll",
                        lambda: ("ready", str(tmp_path / "s.log")))

    status = manager.ensure_running()
    stages = manager.last_stage_timings

    assert status.ok
    assert stages["standby_attempt_ms"] == pytest.approx(3000.0, abs=1.0)
    assert stages["stale_sweep_ms"] < 1.0, stages
    assert "standby_attempt=3000ms" in manager.stage_summary()


def test_host_resolution_is_its_own_stage(monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_PS5_DISCOVER_DRIFT", "1")
    clock = {"t": 0.0}
    monkeypatch.setattr(remote_play_client.time, "perf_counter", lambda: clock["t"])

    def slow_lookup(host):
        clock["t"] += 2400.0
        return "192.0.2.77"

    monkeypatch.setattr(remote_play_client, "_console_host_lookup", slow_lookup)
    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip="192.0.2.9"))
    manager.last_stage_timings = {}

    assert manager._resolve_console_host("192.0.2.9") == "192.0.2.77"
    assert manager.last_stage_timings["host_resolve_ms"] == pytest.approx(
        2400_000.0, rel=0.01)
    assert manager._console_host_adopted == "192.0.2.77"


# ---------------------------------------------------------------------------
# 3. the prewarm that takes the cost off the connect path
# ---------------------------------------------------------------------------

def test_prewarmed_address_serves_the_connect_without_a_scan(monkeypatch):
    lookups = []

    def lookup(host):
        lookups.append(host)
        return "192.0.2.77"

    monkeypatch.setattr(remote_play_client, "_console_host_lookup", lookup)
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "started"
    for _ in range(200):
        if remote_play_client._console_host_prewarm_get("192.0.2.9"):
            break
        import time as _t
        _t.sleep(0.01)

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(console_ip="192.0.2.9"))
    assert manager._resolve_console_host("192.0.2.9") == "192.0.2.77"
    assert lookups == ["192.0.2.9"], "the connect must not re-run the scan"
    assert manager._console_host_adopted == "192.0.2.77"


def test_prewarm_is_single_flight_and_respects_the_kill_switch(monkeypatch):
    monkeypatch.setenv("ORION_PS5_DISCOVER_DRIFT", "0")
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "disabled"
    monkeypatch.delenv("ORION_PS5_DISCOVER_DRIFT", raising=False)
    assert remote_play_client.prewarm_console_host("") == "no-host"

    monkeypatch.setattr(remote_play_client, "_console_host_lookup",
                        lambda host: host)
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "started"
    for _ in range(200):
        if remote_play_client._console_host_prewarm_get("192.0.2.9"):
            break
        import time as _t
        _t.sleep(0.01)
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "fresh"


# ---------------------------------------------------------------------------
# 3b. [ORION_CONNECT_LATENCY 2026-09-19] the prewarm TTL race
#
# The prewarm only ever re-resolved AFTER its entry expired, so the cache was
# empty from expiry until the next 30 s tick finished its ~2.4 s lookup — and a
# Connect landing there paid that lookup synchronously. Measured live: 2 of 10
# connects on this build, host_resolve=2422 ms and 2440 ms, turning a 1.3 s
# connect into 3.3-3.5 s.
# ---------------------------------------------------------------------------

def _wait_for_prewarm(host, timeout_s=2.0):
    import time as _t
    deadline = _t.monotonic() + timeout_s
    while _t.monotonic() < deadline:
        if remote_play_client._console_host_prewarm_get(host):
            return True
        _t.sleep(0.005)
    return False


def test_prewarm_refreshes_before_the_entry_expires(monkeypatch):
    # REGRESSION: with the old `is not None -> "fresh"` short-circuit this call
    # returned "fresh" and started nothing, so the entry was allowed to run all
    # the way to expiry with no refresh in flight.
    lookups = []
    monkeypatch.setattr(remote_play_client, "_console_host_lookup",
                        lambda host: lookups.append(host) or "192.0.2.77")
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "started"
    assert _wait_for_prewarm("192.0.2.9")
    assert len(lookups) == 1

    # Age the entry past the refresh threshold but keep it well inside the TTL.
    with remote_play_client._console_host_prewarm_lock:
        resolved, stamped = remote_play_client._console_host_prewarm["192.0.2.9"]
        remote_play_client._console_host_prewarm["192.0.2.9"] = (
            resolved,
            stamped - (remote_play_client._CONSOLE_HOST_PREWARM_REFRESH_AFTER_S + 1.0),
        )

    # Still servable — a connect in this window must NOT pay a lookup …
    assert remote_play_client._console_host_prewarm_get("192.0.2.9") == "192.0.2.77"
    # … and the prewarm re-resolves it anyway so the cache never empties.
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "refreshing"
    import time as _t
    deadline = _t.monotonic() + 2.0
    while _t.monotonic() < deadline and len(lookups) < 2:
        _t.sleep(0.005)
    assert len(lookups) == 2, "the refresh must actually run a new lookup"
    assert remote_play_client._console_host_prewarm_get("192.0.2.9") == "192.0.2.77"


def test_refresh_threshold_leaves_room_for_the_lookup_before_the_ttl():
    # The contract behind the numbers: the refresh must start far enough before
    # expiry that a ~2.4 s lookup lands first, and far enough after the
    # orchestrator's 30 s prewarm tick that a tick is not a refresh every time.
    tick_s = 30.0
    worst_case_lookup_s = 5.0
    assert remote_play_client._CONSOLE_HOST_PREWARM_REFRESH_AFTER_S > tick_s
    assert (remote_play_client._CONSOLE_HOST_PREWARM_REFRESH_AFTER_S
            + tick_s + worst_case_lookup_s
            < remote_play_client._CONSOLE_HOST_PREWARM_TTL_S)


def test_a_cold_cache_still_reports_started_not_refreshing(monkeypatch):
    # "started" is the honest token for "a Connect right now still pays the
    # lookup"; only an entry that is still servable may report "refreshing".
    monkeypatch.setattr(remote_play_client, "_console_host_lookup",
                        lambda host: host)
    assert remote_play_client._console_host_prewarm_age("192.0.2.9") is None
    assert remote_play_client.prewarm_console_host("192.0.2.9") == "started"


# ---------------------------------------------------------------------------
# 4. a drifted console keeps its pre-booted standby
# ---------------------------------------------------------------------------

class _StandbyChild:
    def __init__(self, pid=4321):
        self.pid = pid
        self._handle = pid + 100_000
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


def test_drifted_console_retargets_the_standby_instead_of_discarding_it(
        monkeypatch, tmp_path):
    monkeypatch.setenv("ORION_INPUT_HOOK", "1")
    monkeypatch.setenv("ORION_PS5_WAKE", "0")
    monkeypatch.delenv("ORION_STANDBY_CLIENT", raising=False)

    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"standby-client")
    pool = remote_play_client.StandbyClientPool()
    child = _StandbyChild()
    pool._client = remote_play_client.StandbyClient(
        process=child, pipe_name="orion_standby_test",
        pipe_path="\\\\.\\pipe\\orion_standby_test",
        executable_path=str(executable), nickname="registered-console",
        host="192.168.137.126", disable_video=False,
        identity_sha256="", identity_size=-1, spawned_at=0.0)

    monkeypatch.setattr(remote_play_client, "find_chiaki_binary",
                        lambda _explicit="": str(executable))
    monkeypatch.setattr(
        remote_play_client, "_probe_chiaki_client",
        lambda _path: remote_play_client.ChiakiClientProbe(
            "registered-console", True, ""))
    monkeypatch.setattr(remote_play_client, "get_standby_pool", lambda: pool)
    monkeypatch.setattr(remote_play_client, "find_remote_play_window",
                        lambda _title="": (0, ""))
    monkeypatch.setattr(remote_play_client, "windows_process_creation_time_100ns",
                        lambda handle: int(handle or 0) * 10)
    monkeypatch.setattr(remote_play_client, "optimize_chiaki_process", lambda _p: None)
    monkeypatch.setattr(remote_play_client, "_standby_pipe_exists", lambda _p: True)
    monkeypatch.setattr(remote_play_client, "_running_image_pids",
                        lambda _t: [(child.pid, "orionstream.exe")])
    # The console moved .126 -> .81 (live, 2026-09-13).
    monkeypatch.setattr(remote_play_client, "_console_host_lookup",
                        lambda host: "192.168.137.81")
    sweeps = []
    monkeypatch.setattr(remote_play_client, "terminate_chiaki_processes",
                        lambda: sweeps.append("sweep") or {})

    commands = []

    def transact(_pipe, command, timeout_s=2.0, **_cancel):
        del timeout_s
        commands.append(command)
        if command == "ping":
            return "ok standby"
        if command.startswith("open "):
            log = tmp_path / "chiaki_session_2026-01-01_00-00-09.log"
            log.write_bytes(b"Starting session request\n"
                            + remote_play_client._CHIAKI_SESSION_READY_MARKER + b"\n")
            return "ok opening"
        return "err unknown"

    monkeypatch.setattr(remote_play_client, "_standby_pipe_transact", transact)

    manager = remote_play_client.RemotePlayClientManager(
        remote_play_client.RemotePlayClientConfig(
            console_ip="192.168.137.126", close_on_stop=True,
            require_session_ready=True, session_log_dir=str(tmp_path),
            wait_timeout_s=3.0))
    status = manager.ensure_running()

    assert status.ok and status.session_ready
    assert status.pid == child.pid
    # The promote carries the ADOPTED address, which the fork's standby protocol
    # documents as winning over the spawn-time one.
    assert commands == ["ping", "open 192.168.137.81"]
    assert sweeps == [], "a promoted standby must not trigger the broad sweep"
    assert manager.last_stage_timings.get("standby") == 1
