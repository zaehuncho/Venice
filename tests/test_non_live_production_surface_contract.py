from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_sidebar_preserves_routes_but_replaces_performance_with_setup() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")

    assert '{ key: "dashboard", label: "Setup", icon: "grid" }' in sidebar
    assert '{ key: "remotePlay", label: "Live", icon: "play" }' in sidebar
    assert 'label: "Performance"' not in sidebar


def test_dashboard_is_setup_only_and_defaults_to_setup() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")

    assert "sourceComponent: setupDash" in dashboard
    assert "dashTab" not in dashboard
    assert "networkDash" not in dashboard

    for removed in (
        "performanceDash",
        'label: "Performance"',
        'label: "Network"',
        'title: "Make Rate"',
        'title: "Shots"',
        "sessionGreens",
        "sessionVerdicts",
        "shotsAttempted",
        "shotsReleased",
        "shotsAborted",
    ):
        assert removed not in dashboard


def test_setup_keeps_backend_contracts_and_has_no_customer_performance_label() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")
    form = source("native_orion/qml/components/StreamSetupForm.qml")

    assert 'title: "Production Setup"' in dashboard
    assert "StreamSetupForm {" in dashboard
    assert 'title: "Profiles"' in dashboard
    assert "Advanced Detection" not in dashboard

    for binding in (
        "orion.remotePlayConsole",
        "orion.consoleIp",
        "orion.detectPs5()",
        "orion.videoSource",
        "orion.captureCardIndex",
        "orion.streamBandwidthMode",
        "orion.autoReconnect",
        "orion.streamAudioEnabled",
        "orion.streamAudioMode",
        "orion.openChiaki()",
    ):
        assert binding in form

    # Preserve the backend preset key while presenting a customer-facing name.
    assert '{ key: "Performance",  label: "Competitive · 720p60 · 12 Mbps" }' in form
    assert 'label: "Performance"' not in form


def test_network_numbers_are_absent_from_the_customer_dashboard() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")

    for removed in (
        'objectName: "networkWaitingState"',
        'objectName: "courtTelemetry"',
        "courtDetected",
        "orion.rttMs",
        "orion.jitterMs",
        "orion.effectiveSyncAdjustMs",
        "orion.inboundPackets",
        "orion.outboundPackets",
        "orion.tickPhaseVerified",
        "orion.tickerLatencyMs",
    ):
        assert removed not in dashboard


def test_overview_is_readiness_and_account_summary_without_live_analytics() -> None:
    overview = source("native_orion/qml/pages/GeneralPage.qml")

    for required in (
        'title: "Overview"',
        'title: "Account"',
        'title: "Application"',
        'label: "Configuration"',
        'label: "Session"',
        "orion.licenseKeyMasked",
        "orion.displayVersion",
        "orion.updateChannel",
    ):
        assert required in overview

    for duplicate_live_metric in (
        "uniqueFrameFps",
        "captureResolution",
        "rttMs",
        "jitterMs",
        "shotsReleased",
        "sessionGreens",
        "sessionVerdicts",
    ):
        assert duplicate_live_metric not in overview

    assert 'label: "Controller"' not in overview
    assert "Physical Sony" not in overview


def test_sidebar_and_tour_only_name_current_customer_surfaces() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    tour = source("native_orion/qml/components/FirstRunTour.qml")

    assert 'statusText: root.controllerReady ? "Physical Sony"' not in sidebar
    assert 'label: "Controller"' not in sidebar
    assert "Overview shows readiness" in tour
    assert "Setup holds stream and profile configuration" in tour
    assert "Setup keeps your source, controller" in tour
    assert "Performance shows" not in tour
    assert 'title: "Track your results"' not in tour


def test_video_presets_do_not_silently_revoke_stick_shot_input() -> None:
    controller = source("native_orion/src/OrionAppController.cpp")
    preset = controller[
        controller.index("void OrionAppController::applyPreset"):
        controller.index("void OrionAppController::saveRemoteSettings")
    ]

    assert "remotePlayInputSource" not in preset
    assert "normalizedRemotePlayInputSource(value)" in controller
