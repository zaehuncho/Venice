from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "native_orion" / "src" / "OrionAppController.cpp"
INPUT_CLIENT = ROOT / "native_orion" / "src" / "OrionInputClient.cpp"


def _function(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start + len(signature))
    return source[start:end]


def test_unexpected_sidecar_exit_uses_exit_generation_not_terminal_state() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    start = source.index("&RemotePlaySession::sidecarExited")
    end = source.index("watchdogTimer_.setTimerType", start)
    handler = source[start:end]

    assert "shouldRecoverUnexpectedSidecarExit(" in handler
    assert "remotePlayTeardownActive_" in handler
    assert "applicationShutdownActive(applicationShutdownPhase_)" in handler
    assert "!remoteRunning_" not in handler


def test_direct_input_pipe_cache_is_reset_at_every_process_generation_boundary() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")

    failed_route = _function(
        source,
        "void OrionAppController::releaseFailedRemoteInputRoute()",
        "void OrionAppController::tripWatchdog",
    )
    contained_restart = _function(
        source,
        "void OrionAppController::restartSidecarWithWindowContainment()",
        "void OrionAppController::setChiakiEmbedVisible",
    )
    # [2026-09-21] The disconnect-side reset moved EARLIER than teardown: disconnectRemotePlay()
    # calls unplugRemoteController(), which resets the pipe "immediately, before asynchronous
    # process cleanup", so a generation boundary is crossed with the cache already cleared
    # rather than after the async teardown finishes. This contract used to search
    # finishRemotePlayTeardown() alone and was failing at HEAD; it now follows the reset.
    unplug = _function(
        source,
        "void OrionAppController::unplugRemoteController()",
        "void OrionAppController::finishRemotePlayTeardown",
    )
    disconnect = _function(
        source,
        "void OrionAppController::disconnectRemotePlay(",
        "void OrionAppController::unplugRemoteController",
    )
    connect = _function(
        source,
        "void OrionAppController::connectRemotePlay()",
        "void OrionAppController::disconnectRemotePlay",
    )

    assert "orionInput_.resetConnection();" in failed_route
    assert "orionInput_.resetConnection();" in contained_restart
    assert "orionInput_.resetConnection();" in unplug
    assert "unplugRemoteController();" in disconnect
    assert "orionInput_.resetConnection();" in connect


def test_input_client_reset_forces_a_fresh_seed_without_resetting_sequence() -> None:
    source = INPUT_CLIENT.read_text(encoding="utf-8")
    reset = _function(
        source,
        "void OrionInputClient::resetConnection()",
        "OrionInputPacket OrionInputClient::lastSent()",
    )

    assert "closePipe();" in reset
    assert "haveLast_ = false;" in reset
    assert "last_ = {};" in reset
    assert "lastConnectAttemptMs_ = -1.0;" in reset
    assert "seq_" not in reset


def test_pipe_write_timeout_rechecks_exact_boundary_completion_after_cancel_drain() -> None:
    source = INPUT_CLIENT.read_text(encoding="utf-8")
    operation = _function(
        source,
        "PipeIoOutcome runOwnedPipeIo(",
        "CheckedElapsedMicros qpcElapsedUs(",
    )
    send = _function(
        source,
        "InputRouteWriteResult OrionInputClient::transmitLocked(",
        "void OrionInputClient::sendAbandonLocked(",
    )
    abandon = _function(
        source,
        "void OrionInputClient::sendAbandonLocked(",
        "#else\nvoid OrionInputClient::closePipe()",
    )

    # A timed-out caller cannot wait for cancellation while its stack buffer is
    # still in use by the kernel. The operation owns all pending storage, and a
    # separate reaper handles the incomplete branch. A completion that wins the
    # timeout/cancel race is inspected without another blocking wait.
    assert "std::make_unique<OwnedPipeIo>()" in operation
    assert "DuplicateHandle(" in operation
    deadline = operation.index("WaitForSingleObject(op->event, timeoutMs)")
    cancel = operation.index("CancelIoEx(op->pipe, &op->overlapped)", deadline)
    boundary = operation.index("WaitForSingleObject(op->event, 0)", cancel)
    defer = operation.index("reapPendingPipeIo(std::move(op));", boundary)
    terminal = operation.index("GetOverlappedResult(op->pipe, &op->overlapped, &outcome.bytes, FALSE)", defer)
    assert deadline < cancel < boundary < defer < terminal
    assert "GetOverlappedResult(op->pipe, &op->overlapped, &outcome.bytes, TRUE)" not in operation
    assert "WaitForSingleObject(op->event, INFINITE)" not in operation

    write = send.index("runOwnedPipeIo(pipe_, false, &p, kWriteTimeoutMs)")
    full_write = send.index("io.success && written == sizeof(p)", write)
    failure = send.index("if(!writeOk)", full_write)
    assert write < full_write < failure
    assert "inputRouteWriteFailureResult(routeWasOwned, writeMayHaveBeenAccepted)" in send[failure:]
    assert "runOwnedPipeIo(pipe_, false, &abandon, kWriteTimeoutMs)" in abandon
    assert "if(io.success && io.bytes == sizeof(abandon))" in abandon


def test_delayed_auto_reconnect_is_fenced_to_its_lifecycle_generation() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    handler_start = source.index("&RemotePlaySession::sidecarExited")
    handler_end = source.index("watchdogTimer_.setTimerType", handler_start)
    handler = source[handler_start:handler_end]
    connect = _function(
        source,
        "void OrionAppController::connectRemotePlay()",
        "void OrionAppController::disconnectRemotePlay",
    )
    disconnect = _function(
        source,
        "void OrionAppController::disconnectRemotePlay(bool synchronous)",
        "void OrionAppController::finishRemotePlayTeardown()",
    )

    assert "const quint64 reconnectGeneration = ++remotePlayLifecycleGeneration_;" in handler
    assert "[this, reconnectGeneration]" in handler
    assert "delayedAutoReconnectStillCurrent(" in handler
    assert "reconnectGeneration," in handler
    assert "remotePlayLifecycleGeneration_," in handler
    assert "++remotePlayLifecycleGeneration_;" in connect
    assert "++remotePlayLifecycleGeneration_;" in disconnect
