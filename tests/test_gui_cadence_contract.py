from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_CPP = ROOT / "native_orion" / "src" / "OrionAppController.cpp"
CONTROLLER_H = ROOT / "native_orion" / "src" / "OrionAppController.h"
MAIN_QML = ROOT / "native_orion" / "qml" / "Main.qml"
VENICE_BACKDROP_QML = ROOT / "native_orion" / "qml" / "components" / "VeniceBackdrop.qml"


def _section(source: str, start_marker: str, end_marker: str) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


def test_solid_lightbar_is_event_driven_and_deduplicated() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    body = _section(
        source,
        "void OrionAppController::applyControllerLightbar(bool force)",
        "void OrionAppController::appendLog",
    )

    assert "lightbarRefreshPending_" in body
    assert "sameSolidWrite" in body
    assert "lastLightbarSentColor_ == color" in body
    assert "lastLightbarSentDevicePath_ == activePhysicalDevicePath_" in body
    assert body.index("if (!force && sameSolidWrite)") < body.index("sendSonyLightbar(")
    assert "lightbarEffectTimer_.stop()" in body
    assert "animated ? 160 : 1500" not in body


def test_lightbar_refreshes_after_device_arrival_and_selected_route_change() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    device_change = _section(
        source,
        "void OrionAppController::handleRawInputDeviceChange(",
        "QString OrionAppController::logText() const",
    )
    poll = _section(
        source,
        "void OrionAppController::pollPhysicalController()",
        "void OrionAppController::updateLightbarEffect()",
    )

    assert "changeKind == GIDC_ARRIVAL" in device_change
    assert "requestControllerLightbarRefresh();" in device_change
    assert "lastLightbarSentDevicePath_.clear();" in device_change
    assert "lightbarRouteChanged" in poll
    assert "requestControllerLightbarRefresh();" in poll


def test_raw_input_enumeration_is_recovery_only_while_worker_is_fresh() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    poll = _section(
        source,
        "void OrionAppController::pollPhysicalController()",
        "void OrionAppController::updateLightbarEffect()",
    )
    device_change = _section(
        source,
        "void OrionAppController::handleRawInputDeviceChange(",
        "QString OrionAppController::logText() const",
    )

    assert "rawSnapshotFresh" in poll
    assert "rawIdentityKnown" in poll
    assert "rawPresenceRefreshRequested" in poll
    assert "rawPresenceStaleOrMissing" in poll
    scan_at = poll.index("findRawInputController(")
    guard_at = poll.index("if (rawPresenceRefreshRequested")
    assert guard_at < scan_at
    assert "lastRawInputPresenceCheckMs_ = 0;" in device_change


def test_runtime_status_notifiers_are_change_driven() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    header = CONTROLLER_H.read_text(encoding="utf-8")
    body = _section(
        source,
        "void OrionAppController::updateRuntimeStatus()",
        "bool OrionAppController::packetBridgeReachable",
    )

    assert "sessionTime READ sessionTime NOTIFY sessionTimeChanged" in header
    assert "statusAge READ statusAge NOTIFY statusAgeChanged" in header
    assert "if (sessionTime_ != previousSessionTime)" in body
    assert "if (statusAge_ != previousStatusAge)" in body
    assert "if (runtimeTelemetryChanged)" in body
    assert "if (runtimeStatusChanged)" in body
    assert body.rstrip().endswith("}")


def test_only_ui_publication_is_throttled_not_detection_authority() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    constructor = _section(
        source,
        "OrionAppController::OrionAppController(QString rootDir, QObject* parent)",
        "OrionAppController::~OrionAppController()",
    )
    detection = _section(
        constructor,
        "&RemotePlaySession::sidecarDetectionReady",
        "&AutomationEngine::learningUpdated",
    )

    assert detection.index("automation_.updateDetection(result)") < detection.index(
        "notifyTelemetryStatusAtHumanCadence(nowMeterMs)"
    )
    assert detection.index("automation_.reevaluateScheduleOnFreshSample()") < detection.index(
        "notifyTelemetryStatusAtHumanCadence(nowMeterMs)"
    )
    assert "telemetryStatusThrottle_.take(nowMs)" in constructor
    assert constructor.count("notifyTelemetryPropertiesAtHumanCadence(") >= 2
    assert "inputPollTimer_.setInterval(4);" in constructor


def test_periodic_diagnostics_do_not_age_out_user_activity_history() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    body = _section(
        source,
        "void OrionAppController::appendLog(const QString& message)",
        "void OrionAppController::flushPendingLogs()",
    )

    classifier_at = body.index("isPeriodicMachineDiagnostic(simplified)")
    user_ring_guard_at = body.index("if (!periodicDiagnostic)")
    user_ring_at = body.index("logs_.append(")
    disk_ring_at = body.index("pendingLogDiskLines_.append(")

    assert classifier_at < user_ring_guard_at < user_ring_at < disk_ring_at
    # Disk/audit persistence must remain unconditional; only the QML-facing
    # ring belongs inside the periodic-diagnostic guard.
    guarded = body[user_ring_guard_at:disk_ring_at]
    assert "pendingLogDiskLines_.append(" not in guarded
    assert "logsDirty_ = true;" in guarded


def test_presenter_deadline_lateness_is_observability_only() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    frame_handler = _section(
        source,
        "void OrionAppController::handleRemoteFrame(",
        "void OrionAppController::registerRawInputController()",
    )

    assert "presenterDeadlineLateMaxMs" in frame_handler
    assert "present_deadline_late_max_ms=" in frame_handler
    assert "stats.maxRequestGapMs - (1000.0 / 60.0)" in frame_handler


def test_authenticated_shell_backdrop_does_not_compete_with_live_video() -> None:
    qml = MAIN_QML.read_text(encoding="utf-8")
    backdrop = VENICE_BACKDROP_QML.read_text(encoding="utf-8")

    assert "VeniceBackdrop {" in qml
    assert "StarBackdrop {" not in qml
    assert "Canvas {" not in backdrop
    assert "Timer {" not in backdrop
    assert "Animation {" not in backdrop
    assert "ParticleSystem {" not in backdrop
