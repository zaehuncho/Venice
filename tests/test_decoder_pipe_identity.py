from __future__ import annotations

import os
import threading
import uuid

import numpy as np
import pytest

import remote_play_orchestrator as rpo
from chiaki_backend import FrameData, OrionFramePipeBackend
from decoder_pipe_identity import (
    ProducerObservation,
    WindowsNamedPipeServerIdentityApi,
    expectation_from_owned_process,
    producer_identity_reason,
    stable_executable_snapshot,
)


class _IdentityApi:
    def __init__(self, observation=None, inspect_reason="", lease_reason=""):
        self.observation = observation
        self.inspect_reason_value = inspect_reason
        self.lease_reason_value = lease_reason
        self.closed = []

    def inspect(self, _pipe_handle):
        return self.observation, self.inspect_reason_value

    def lease_reason(self, _observation):
        return self.lease_reason_value

    def close(self, observation):
        if observation is not None:
            self.closed.append(observation)


def _owned(snapshot, *, pid=4200, generation=1, creation=9001, path=None,
           process_handle=None):
    return {
        "pid": pid,
        "launch_generation": generation,
        "creation_time_100ns": creation,
        "path": snapshot.path if path is None else path,
        "process_handle": object() if process_handle is None else process_handle,
    }


def _observation(snapshot, *, pid=4200, creation=9001, path=None, size=None, sha=None):
    return ProducerObservation(
        pid=pid,
        creation_time_100ns=creation,
        path=snapshot.path if path is None else path,
        size=snapshot.size if size is None else size,
        sha256=snapshot.sha256 if sha is None else sha,
        process_handle=object(),
    )


def _backend(snapshot, owned, api):
    return OrionFramePipeBackend(
        expected_producer_path=snapshot.path,
        expected_producer_size=snapshot.size,
        expected_producer_sha256=snapshot.sha256,
        owned_process_identity_provider=lambda: owned,
        producer_identity_api=api,
    )


def test_exact_owned_server_pid_path_hash_and_creation_is_verified(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"exact-shipped-image")
    snapshot = stable_executable_snapshot(str(executable))
    api = _IdentityApi(_observation(snapshot))
    backend = _backend(snapshot, _owned(snapshot), api)
    backend._handle = object()

    backend._bind_pipe_producer()

    status = backend.producer_identity_status()
    assert status["verified"] is True
    assert status["pid"] == 4200
    assert status["launch_generation"] == 1
    assert backend._producer_lease_reason_now() == ""


@pytest.mark.skipif(os.name != "nt", reason="Windows named-pipe identity API")
def test_real_client_handle_reports_server_process_not_client_process():
    win32file = pytest.importorskip("win32file")
    win32pipe = pytest.importorskip("win32pipe")
    pipe_name = rf"\\.\pipe\orion_identity_test_{uuid.uuid4().hex}"
    server = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_OUTBOUND,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
        1,
        4096,
        4096,
        0,
        None,
    )
    connected = threading.Event()

    def accept():
        try:
            win32pipe.ConnectNamedPipe(server, None)
        except Exception as exc:
            # ERROR_PIPE_CONNECTED is the ordinary client-won-the-race result.
            if getattr(exc, "winerror", None) != 535:
                raise
        finally:
            connected.set()

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    client = win32file.CreateFile(
        pipe_name, win32file.GENERIC_READ, 0, None,
        win32file.OPEN_EXISTING, 0, None)
    try:
        assert connected.wait(1.0)
        api = WindowsNamedPipeServerIdentityApi()
        observation, reason = api.inspect(client)
        assert reason == ""
        assert observation is not None
        assert observation.pid == os.getpid()
        # A venv's sys.executable may be a launcher shim while the process image
        # is the base interpreter. PID direction is the contract under test here.
        assert os.path.basename(observation.path).lower() == "python.exe"
        assert stable_executable_snapshot(observation.path).valid
        assert api.lease_reason(observation) == ""
        api.close(observation)
    finally:
        win32file.CloseHandle(client)
        try:
            win32pipe.DisconnectNamedPipe(server)
        except Exception:
            pass
        win32file.CloseHandle(server)
        thread.join(timeout=1.0)


def test_wrong_pid_path_or_hash_never_verifies(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    other = tmp_path / "OtherStream.exe"
    executable.write_bytes(b"expected")
    other.write_bytes(b"other")
    expected = stable_executable_snapshot(str(executable))
    other_snapshot = stable_executable_snapshot(str(other))
    expectation, reason = expectation_from_owned_process(
        _owned(expected), expected.path, expected.size, expected.sha256)
    assert reason == ""

    cases = [
        (_observation(expected, pid=4201), "server_pid_mismatch"),
        (_observation(expected, creation=9002), "server_creation_time_mismatch"),
        (_observation(expected, path=other_snapshot.path), "server_path_mismatch"),
        (_observation(expected, sha="0" * 64), "server_hash_mismatch"),
    ]
    for observed, expected_reason in cases:
        assert producer_identity_reason(expectation, observed) == expected_reason
        backend = _backend(expected, _owned(expected), _IdentityApi(observed))
        backend._handle = object()
        backend._bind_pipe_producer()
        status = backend.producer_identity_status()
        assert status["verified"] is False
        assert status["reason"] == expected_reason


def test_missing_server_pid_api_is_unverified_but_pipe_can_remain_cold(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"expected")
    snapshot = stable_executable_snapshot(str(executable))
    backend = _backend(
        snapshot, _owned(snapshot),
        _IdentityApi(None, inspect_reason="server_pid_api_missing"),
    )
    backend._handle = object()

    backend._bind_pipe_producer()

    status = backend.producer_identity_status()
    assert status == {
        "state": "unverified",
        "reason": "server_pid_api_missing",
        "verified": False,
        "pid": 0,
        "launch_generation": 0,
    }
    # Identity failure is intentionally not transport teardown. The reader owns
    # the still-open handle and may publish frames after timing becomes unscoped.
    assert backend._handle is not None


def test_stale_or_reused_process_identity_revokes_verified_lease(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"expected")
    snapshot = stable_executable_snapshot(str(executable))
    owned = _owned(snapshot)
    api = _IdentityApi(_observation(snapshot), lease_reason="server_process_reused")
    backend = _backend(snapshot, owned, api)
    backend._handle = object()
    backend._bind_pipe_producer()
    assert backend.producer_identity_status()["verified"] is True
    assert backend._producer_lease_reason_now() == "server_process_reused"

    api.lease_reason_value = ""
    owned["launch_generation"] = 2
    assert backend._producer_lease_reason_now() == "owned_launch_generation_changed"


def test_same_size_image_tamper_changes_content_identity(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"AAAA")
    first = stable_executable_snapshot(str(executable))
    executable.write_bytes(b"BBBB")
    second = stable_executable_snapshot(str(executable))
    assert first.valid and second.valid
    assert first.size == second.size
    assert first.sha256 != second.sha256


def test_wire_geometry_or_format_transition_advances_source_generation():
    backend = OrionFramePipeBackend(producer_identity_api=_IdentityApi())
    backend._source_generation = 4
    assert backend._accept_wire_mode(1920, 1080, 0) is False
    assert backend._source_generation == 4
    assert backend._accept_wire_mode(1920, 1080, 0) is False
    assert backend._source_generation == 4
    assert backend._accept_wire_mode(1280, 720, 0) is True
    assert backend._source_generation == 5
    assert backend._accept_wire_mode(1280, 720, 1) is True
    assert backend._source_generation == 6


class _BackendStatus:
    def __init__(self, state="verified", reason="", pid=4200, generation=1):
        self.state = state
        self.reason = reason
        self.pid = pid
        self.generation = generation

    def producer_identity_status(self):
        return {
            "state": self.state,
            "reason": self.reason,
            "verified": self.state == "verified",
            "pid": self.pid,
            "launch_generation": self.generation,
        }


def _decoder_config(snapshot):
    return rpo.OrchestratorConfig(
        frame_source="decoder",
        auto_launch_client=False,
        virtual_controller=False,
        console_identity="registered-host-sha256-v1:" + "1" * 64,
        controller_route="pipe",
        chiaki_path=snapshot.path,
        chiaki_identity_size=snapshot.size,
        chiaki_identity_sha256=snapshot.sha256,
    )


def _decoder_frame(*, width=1920, height=1080, fmt=0, verified=True):
    return FrameData(
        frame=np.zeros((height, width, 3), dtype=np.uint8),
        producer_identity_verified=verified,
        producer_process_id=4200,
        producer_launch_generation=1,
        decoder_width=width,
        decoder_height=height,
        decoder_format=fmt,
    )


def test_unverified_decoder_permanently_moves_timing_to_unscoped_cold(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"expected")
    snapshot = stable_executable_snapshot(str(executable))
    orch = rpo.RemotePlayOrchestrator(_decoder_config(snapshot))
    prior = orch._latency_estimator
    backend = _BackendStatus(state="unverified", reason="server_pid_mismatch")

    assert orch._guard_decoder_latency_route(
        backend=backend, frame_data=_decoder_frame(verified=False)) is False
    assert orch._decoder_warm_cache_revoked is True
    assert orch._latency_route_scope_value == ""
    assert orch._latency_controller_attestation_generation == 0
    assert orch._latency_estimator is not prior

    # A later exact producer may still supply cold live frames, but this sidecar
    # process can never resurrect cache authority after the contradiction.
    backend.state = "verified"
    assert orch._guard_decoder_latency_route(
        backend=backend, frame_data=_decoder_frame()) is False
    assert orch._latency_route_scope_value == ""


def test_actual_wire_mode_enters_scope_and_live_change_resets_without_restore(tmp_path):
    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"expected")
    snapshot = stable_executable_snapshot(str(executable))
    orch = rpo.RemotePlayOrchestrator(_decoder_config(snapshot))
    backend = _BackendStatus()

    assert orch._guard_decoder_latency_route(
        backend=backend, frame_data=_decoder_frame()) is True
    first_scope = orch._latency_route_scope_value
    assert "wire=orf2:1920x1080:nv12" in first_scope
    first_estimator = orch._latency_estimator
    orch._latency_controller_attestation_generation = 55
    orch._latency_controller_attestation_scope = first_scope
    orch._latency_controller_attestation_route = "pipe"

    old_integrity_generation = orch._frame_integrity_generation
    assert orch._guard_decoder_latency_route(
        backend=backend,
        frame_data=_decoder_frame(width=1280, height=720, fmt=1),
    ) is True
    assert "wire=orf2:1280x720:i420" in orch._latency_route_scope_value
    assert orch._latency_route_scope_value != first_scope
    assert orch._latency_estimator is not first_estimator
    assert getattr(orch._latency_estimator, "restored_from_cache", False) is False
    assert orch._latency_controller_attestation_generation == 0
    assert orch._latency_controller_attestation_scope == ""
    assert orch._latency_controller_attestation_route == ""
    assert orch._frame_integrity_generation > old_integrity_generation


def test_same_route_attestation_retries_transient_estimator_load_failure(
        monkeypatch, tmp_path):
    import latency_estimator as latency_module

    executable = tmp_path / "OrionStream.exe"
    executable.write_bytes(b"expected")
    snapshot = stable_executable_snapshot(str(executable))
    config = _decoder_config(snapshot)
    config.decoder_mode = "orf2:1920x1080:nv12"
    orch = rpo.RemotePlayOrchestrator(config)
    scope = rpo._latency_route_scope(config)
    assert scope
    orch._latency_route_scope_value = scope
    orch._latency_estimator = None
    replacement = object()
    loads = []

    def load(route_scope="", **_kwargs):
        loads.append(route_scope)
        return None if len(loads) == 1 else replacement

    monkeypatch.setattr(latency_module, "try_load", load)
    orch._rekey_latency_route("first_transient_failure")
    assert orch._latency_estimator is None

    assert orch.attest_controller_latency_route("pipe", 88) is True
    assert orch._latency_estimator is replacement
    assert loads == [scope, scope]
