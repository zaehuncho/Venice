from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_orion" / "src"
CLASSIFIER = NATIVE / "PassiveCourtFlowClassifier.h"
BRIDGE = NATIVE / "NetworkBridge.cpp"
CONTROLLER = NATIVE / "OrionAppController.cpp"
CONTROLLER_HEADER = NATIVE / "OrionAppController.h"
DASHBOARD = ROOT / "native_orion" / "qml" / "pages" / "DashboardPage.qml"


def _between(source: str, start: str, end: str) -> str:
    begin = source.index(start)
    return source[begin : source.index(end, begin + len(start))]


def test_classifier_is_strict_sustained_bidirectional_udp_only() -> None:
    source = CLASSIFIER.read_text(encoding="utf-8")

    assert "Transport::Udp" in source
    assert "kCourtPortMin = 30000" in source
    assert "kCourtPortMax = 30099" in source
    assert "kMinimumPackets = 12" in source
    assert "kMinimumPacketsPerDirection = 3" in source
    assert "kMinimumObservationSpanMs" in source
    assert "kMinimumPacketsPerSecond" in source
    assert "kStaleAfterMs" in source
    assert "Ipv4Kind::PrivateLan" in source
    assert "Ipv4Kind::Public" in source
    assert "inbound >= kMinimumPacketsPerDirection" in source
    assert "outbound >= kMinimumPacketsPerDirection" in source


def test_bridge_uses_local_clock_console_identity_and_display_only_fields() -> None:
    source = BRIDGE.read_text(encoding="utf-8")
    packet_handler = _between(
        source,
        'if (event == QLatin1String("packet"))',
        '} else if (event == QLatin1String("ready"))',
    )
    publisher = _between(
        source,
        "void NetworkBridge::applyPassiveSnapshot(",
        "void NetworkBridge::checkIdleTimeout(",
    )
    stop = _between(
        source,
        "void NetworkBridge::stop()",
        "bool NetworkBridge::sendLine(",
    )
    reset = _between(
        source,
        "void NetworkBridge::resetTelemetryState()",
        "void NetworkBridge::applyPassiveSnapshot(",
    )

    assert 'proto != QLatin1String("UDP")' in packet_handler
    assert "monotonicNowMs()" in packet_handler
    assert "srcIp != consoleIp_ && dstIp != consoleIp_" in packet_handler
    assert "PassiveCourtFlowClassifier::Transport::Udp" in packet_handler

    assert "telemetry_.diagnosticCourtIp" in publisher
    assert "telemetry_.courtIp.clear();" in publisher
    assert "telemetry_.rttTargetVerified = false;" in publisher
    assert "telemetry_.syncActive = false;" in publisher
    assert "telemetry_.offsetMs = 0.0;" in publisher
    assert "telemetry_.syncAdjustMs = 0.0;" in publisher
    assert "telemetry_.tickerLatencyMs = 0.0;" in publisher
    assert "display only" in publisher
    assert "resetTelemetryState();" in stop
    assert "telemetry_.diagnosticCourtIp.clear();" in reset
    assert "passiveCourtFlow_.reset();" in reset

    assert "remotePlay_" not in source
    assert "observePacket(" not in source
    assert "setCourtIp(" not in source


def test_remote_telemetry_preserves_only_diagnostic_identity_from_bridge() -> None:
    source = CONTROLLER.read_text(encoding="utf-8")
    remote_merge = _between(
        source,
        "&RemotePlaySession::telemetryReady",
        "&RemotePlaySession::poseLandmarkReady",
    )
    bridge_merge = _between(
        source,
        "&NetworkBridge::telemetryUpdated",
        "&NetworkBridge::playerCountChanged",
    )
    bridge_connection = _between(
        source,
        "&NetworkBridge::connectionChanged",
        "networkBridge_.setConsoleIp",
    )
    opt_out = _between(
        source,
        "void OrionAppController::setPacketCaptureEnabled",
        "void OrionAppController::setRemotePlayConsole",
    )

    assert "telemetry_ = rpTelemetry;" in remote_merge
    assert "telemetry_.diagnosticCourtIp = observed.diagnosticCourtIp;" in remote_merge
    assert "telemetry_.rttMs = observed.rttMs" not in remote_merge
    assert "telemetry_.offsetMs = observed.offsetMs" not in remote_merge
    assert "telemetry_.syncAdjustMs = observed.syncAdjustMs" not in remote_merge

    assert "previous.rttTargetVerified" in bridge_merge
    assert "remotePlay_.clearCourtTarget()" not in bridge_merge
    assert "remotePlay_.clearCourtTarget()" not in bridge_connection
    assert "remotePlay_.clearCourtTarget()" not in opt_out


def test_display_fallback_cannot_unlock_timing_accessors() -> None:
    header = CONTROLLER_HEADER.read_text(encoding="utf-8")
    implementation = CONTROLLER.read_text(encoding="utf-8")
    getter = _between(
        header,
        "[[nodiscard]] QString telemetryCourtIp()",
        "[[nodiscard]] QString telemetrySync()",
    )
    automation_offset = _between(
        implementation,
        "double OrionAppController::networkAutomationOffset()",
        "} // namespace orion",
    )

    assert "telemetry_.rttTargetVerified" in getter
    assert "telemetry_.diagnosticCourtIp" in getter
    assert "if (!telemetry_.rttTargetVerified)" in automation_offset
    assert "return 0.0;" in automation_offset


def test_customer_dashboard_omits_network_telemetry_surface() -> None:
    source = DASHBOARD.read_text(encoding="utf-8")

    # Passive-flow diagnostics remain internally fail-closed, but the Venice
    # customer dashboard deliberately does not expose the retired Network page.
    for retired_network_copy in (
        "Passively observed UDP flow; diagnostic only",
        'label: "Timing authority"',
        "Passive capture does not measure RTT",
        "Passive capture has no timing authority",
        "orion.rttMs.toFixed(1)",
    ):
        assert retired_network_copy not in source

    # [2026-09-14 owner] The Timing readiness pill (objectName passiveTimingStatus) went
    # with the whole "Production Setup" status card: every pill in it restated a control
    # in the Connection form directly below. Timing adaptation is still passive and
    # engine-side; it simply has no customer-facing chrome.
    assert 'objectName: "passiveTimingStatus"' not in source
    assert 'label: "Timing"' not in source
