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
    teardown = _function(
        source,
        "void OrionAppController::finishRemotePlayTeardown()",
        "void OrionAppController::runLatencyProbes",
    )
    connect = _function(
        source,
        "void OrionAppController::connectRemotePlay()",
        "void OrionAppController::disconnectRemotePlay",
    )

    assert "orionInput_.resetConnection();" in failed_route
    assert "orionInput_.resetConnection();" in contained_restart
    assert "orionInput_.resetConnection();" in teardown
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
    send = _function(
        source,
        "InputRouteWriteResult OrionInputClient::sendDetailed(",
        "#else\nvoid OrionInputClient::closePipe()",
    )

    timeout_branch = send.index("CancelIoEx(pipe_, &ov);")
    drain = send.index(
        "WaitForSingleObject(writeEvent_, INFINITE) == WAIT_OBJECT_0",
        timeout_branch,
    )
    terminal_result = send.index(
        "GetOverlappedResult(pipe_, &ov, &written, FALSE)",
        drain,
    )
    failure_classification = send.index("if(!writeOk)", terminal_result)

    # The buffer remains alive until cancellation is drained. If the write won the
    # timeout/cancel race, its exact terminal completion is authoritative; aborted,
    # partial, and failed writes still flow to the ambiguous fail-closed result.
    assert timeout_branch < drain < terminal_result < failure_classification
    assert "&& written == sizeof(p);" in send[terminal_result:failure_classification]


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
