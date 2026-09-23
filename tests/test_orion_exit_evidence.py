"""A3 parent-owned process termination and reconnect evidence fixtures."""

import json
from pathlib import Path

import orion_exit_evidence as evidence
import remote_play_client as rpc


def _events(log_dir: Path):
    return [json.loads(line) for line in (log_dir / "orion_parent_termination.jsonl").read_text().splitlines()]


def test_fake_child_exit_matrix_has_one_observed_classification(tmp_path):
    cases = (
        ("clean", 0, "Orion process_exit code=0", True, False, "clean"),
        ("fault", 1, "exit_path=orion_bridge_fatal owner_pipe_disconnected", False, False, "child_fault"),
        ("fault_zero", 0, "exit_path=orion_bridge_fatal owner_pipe_disconnected\nOrion process_exit code=0", False, False, "child_fault"),
        ("forced", 1, "exit_path=orion_bridge_fatal owner_pipe_disconnected", True, True, "forced_by_parent"),
        ("unknown", 1, "ordinary input ACK", False, False, "external_or_runtime_unknown"),
    )
    for name, code, child_line, graceful, forced, expected in cases:
        folder = tmp_path / name
        folder.mkdir()
        child_log = folder / "chiaki_session_fixture.log"
        child_log.write_text(child_line + "\n")
        trace = evidence.SessionExitEvidence(folder, pid=123, creation_time_100ns=456,
                                             session_generation=7, session_log_path=child_log)
        if graceful:
            trace.request("launcher", "stop", forced=False, requested_exit_code=0)
            trace.request_result(True)
        if forced:
            trace.request("launcher", "graceful_timeout", forced=True, requested_exit_code=1)
            trace.request_result(True)
        assert trace.observe(code) == expected
        assert trace.observe(code) == expected
        observed = [event for event in _events(folder) if event["event"] == "observed_exit"]
        assert len(observed) == 1
        assert observed[0]["classification"] == expected
        assert observed[0]["pid"] == 123
        assert observed[0]["creation_time_100ns"] == 456
        assert observed[0]["session_generation"] == 7
        if expected != "clean":
            bundles = list((folder / "incidents").glob("*/manifest.json"))
            assert len(bundles) == 1
            manifest = json.loads(bundles[0].read_text())
            assert manifest["classification"] == expected
            assert manifest["files"]["session.log"]["sha256"]


def test_request_is_durable_before_force_and_missing_final_line_is_not_clean(tmp_path):
    child_log = tmp_path / "chiaki_session_fixture.log"
    child_log.write_text("owner pipe disconnected\n")
    trace = evidence.SessionExitEvidence(tmp_path, pid=60604, creation_time_100ns=99,
                                         session_generation=2, session_log_path=child_log)
    trace.request("launcher", "deadline_reaper", forced=True, requested_exit_code=1)
    assert _events(tmp_path)[-1]["event"] == "termination_request"
    assert _events(tmp_path)[-1]["call_result"] == "pending"
    trace.request_result(True)
    assert trace.observe(1) == "forced_by_parent"
    assert not any("process_exit" in line for line in child_log.read_text().splitlines())


def test_abnormal_bundle_survives_more_than_six_retry_logs_and_is_bounded(tmp_path):
    first = tmp_path / "chiaki_session_first.log"
    first.write_bytes(b"X" * (evidence.MAX_SESSION_LOG_BYTES + 32) + b"\nincident tail\n")
    trace = evidence.SessionExitEvidence(tmp_path, pid=1, creation_time_100ns=2,
                                         session_generation=3, session_log_path=first)
    assert trace.observe(1) == "external_or_runtime_unknown"
    bundle = next((tmp_path / "incidents").glob("*/session.log"))
    assert bundle.read_bytes().endswith(b"incident tail\n")
    assert bundle.stat().st_size <= evidence.MAX_SESSION_LOG_BYTES
    first.unlink()
    for index in range(9):
        (tmp_path / f"chiaki_session_retry_{index}.log").write_text("retry\n")
    assert bundle.exists()
    assert (bundle.parent / "manifest.json").exists()


def test_missing_or_unwritable_bundle_store_keeps_exit_classification(tmp_path, monkeypatch):
    trace = evidence.SessionExitEvidence(tmp_path, pid=1, creation_time_100ns=2,
                                         session_generation=3,
                                         session_log_path=tmp_path / "missing.log")
    assert trace.observe(1) == "external_or_runtime_unknown"
    assert _events(tmp_path)[-1]["classification"] == "external_or_runtime_unknown"
    other = evidence.SessionExitEvidence(tmp_path / "other", pid=2,
                                         creation_time_100ns=3, session_generation=4)
    monkeypatch.setattr(evidence, "_write_incident_bundle", lambda *a, **kw: (_ for _ in ()).throw(OSError("full")))
    assert other.observe(1) == "external_or_runtime_unknown"


def test_manager_records_identity_before_forced_child_kill(tmp_path, monkeypatch):
    seen = []
    class Child:
        pid = 60604
        returncode = None
        def poll(self):
            return self.returncode
        def terminate(self):
            seen.append(_events(tmp_path)[-1])
            self.returncode = 1
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(session_log_dir=str(tmp_path)))
    manager._process = Child()
    manager._owned_launch_generation = 8
    manager._owned_creation_time_100ns = 123456
    monkeypatch.setattr(rpc, "terminate_chiaki_processes", lambda: {})
    monkeypatch.setattr(manager, "_post_wm_close", lambda _: False)
    manager.stop()
    assert len(seen) == 1
    assert seen[0]["event"] == "termination_request"
    assert seen[0]["forced"] is True
    assert seen[0]["pid"] == 60604
    assert seen[0]["creation_time_100ns"] == 123456
    assert seen[0]["session_generation"] == 8
    assert _events(tmp_path)[-1]["classification"] == "forced_by_parent"


def test_manager_pins_unexpected_exit_before_reconnect(tmp_path):
    child_log = tmp_path / "chiaki_session_first.log"
    child_log.write_text("exit_path=orion_bridge_fatal owner_pipe_disconnected\n")
    class Child:
        pid = 60604
        returncode = 1
        def poll(self):
            return self.returncode
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(session_log_dir=str(tmp_path)))
    manager._process = Child()
    manager._owned_launch_generation = 8
    manager._owned_creation_time_100ns = 123456
    manager._status.session_log_path = str(child_log)
    assert not manager.is_running()
    assert len(list((tmp_path / "incidents").glob("*/session.log"))) == 1


def test_normal_close_pipe_drop_without_child_final_line_is_clean(tmp_path, monkeypatch):
    child_log = tmp_path / "chiaki_session_normal_close.log"
    child_log.write_text("exit_path=orion_bridge_fatal owner_pipe_disconnected\n")
    class Child:
        pid = 70
        returncode = None
        forced = False
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
        def terminate(self):
            self.forced = True
    child = Child()
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(session_log_dir=str(tmp_path)))
    manager._process = child
    manager._owned_launch_generation = 4
    manager._owned_creation_time_100ns = 50
    manager._status.hwnd = 99
    manager._status.session_log_path = str(child_log)
    monkeypatch.setattr(manager, "_post_wm_close", lambda _: True)
    monkeypatch.setattr(rpc, "terminate_chiaki_processes", lambda: {})
    manager.stop()
    assert not child.forced
    assert _events(tmp_path)[-1]["classification"] == "clean"
    assert not (tmp_path / "incidents").exists()


def test_unbound_stale_sweep_records_identity_without_fake_incident(tmp_path):
    trace = evidence.SessionExitEvidence(tmp_path, pid=99, creation_time_100ns=111,
                                         session_generation=0, pin_abnormal=False,
                                         persist_unbound=True)
    trace.request("launcher", "stale_client_sweep", forced=True, requested_exit_code=1)
    trace.request_result(True)
    assert trace.observe(1) == "forced_by_parent"
    assert _events(tmp_path)[-1]["creation_time_100ns"] == 111
    assert not (tmp_path / "incidents").exists()


def test_launch_identity_failure_records_before_killing_spawned_child(tmp_path, monkeypatch):
    seen = []
    class Child:
        pid = 77
        _handle = None
        def terminate(self):
            seen.append(_events(tmp_path)[-1])
    manager = rpc.RemotePlayClientManager(rpc.RemotePlayClientConfig(session_log_dir=str(tmp_path)))
    monkeypatch.setattr(rpc, "chiaki_controller_env", lambda **kw: {})
    monkeypatch.setattr(rpc.subprocess, "Popen", lambda *a, **kw: Child())
    monkeypatch.setattr(rpc, "canonical_executable_path", lambda _: "")
    monkeypatch.setattr(rpc, "windows_process_creation_time_100ns", lambda _: 222)
    result = manager._launch_process(["fixture"], "chiaki", "fixture.exe")
    assert not result.ok
    assert len(seen) == 1
    assert seen[0]["event"] == "termination_request"
    assert seen[0]["session_generation"] == 1
    assert seen[0]["creation_time_100ns"] == 222


def test_parent_ledger_rotates_with_bounded_size(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "_MAX_LEDGER_BYTES", 180)
    trace = evidence.SessionExitEvidence(tmp_path, pid=9, creation_time_100ns=10,
                                         session_generation=11)
    for _ in range(3):
        trace.request("launcher", "stop", forced=False, requested_exit_code=0)
    assert (tmp_path / "orion_parent_termination.jsonl.1").exists()
    assert (tmp_path / "orion_parent_termination.jsonl").exists()
