from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_sidebar_preserves_routes_but_replaces_performance_with_setup() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")

    assert '{ key: "dashboard", label: "Setup", icon: "grid" }' in sidebar
    assert '{ key: "remotePlay", label: "Live", icon: "play" }' in sidebar
    assert '{ key: "patchNotes", label: "Updates", icon: "notes" }' in sidebar
    # [PROFILE TAB REMOVED 2026-09-15 owner] Account truth is the footer strip now.
    assert 'key: "profile"' not in sidebar
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

    # [2026-09-14 owner] "Production Setup" (the Console / Video / Reconnect / Timing
    # status-pill card) is deleted — it restated the Connection form under it.
    assert 'title: "Production Setup"' not in dashboard
    assert 'title: "Connection"' in dashboard
    assert "StreamSetupForm {" in dashboard
    # [2026-09-21] The "Profiles" card left the Dashboard with the 09-14 simplification
    # (profile/account data lives in the Sidebar licence flyout); this contract had kept
    # asserting the removed card and was failing at HEAD.
    assert 'title: "Profiles"' not in dashboard
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


def test_overview_and_profile_pages_are_gone_and_account_lives_in_the_licence_strip() -> None:
    """[2026-09-14/15 owner] The Overview tab is deleted; so is the Profile tab.

    Overview's three cards were Overview (readiness restated from Setup + Live),
    Account, and Application. Only Account was information you could not get anywhere
    else. It briefly landed at the bottom of Setup, then became its own sidebar tab
    (pages/ProfilePage.qml) when the owner asked for a profile section. On 2026-09-15
    the owner removed that tab too -- "license info and days left should be displayed
    on the bottom left corner" -- so the account truth is a permanent strip in the
    Sidebar footer with a details flyout. Both pages stay removed, along with their
    sidebar entries, their AppShell routes, and their CMakeLists QML_FILES lines.
    """
    assert not (ROOT / "native_orion" / "qml" / "pages" / "GeneralPage.qml").exists()
    assert not (ROOT / "native_orion" / "qml" / "pages" / "ProfilePage.qml").exists()

    cmake = source("native_orion/CMakeLists.txt")
    assert "GeneralPage.qml" not in cmake
    assert "ProfilePage.qml" not in cmake

    shell = source("native_orion/qml/components/AppShell.qml")
    assert "GeneralPage" not in shell
    assert "ProfilePage" not in shell
    assert 'orion.currentPage === "general"' not in shell
    assert 'orion.currentPage === "profile"' not in shell
    # An unknown/stale current_page (including a persisted "general" or "profile")
    # must fall through to Remote Play rather than load a null component into a
    # blank panel -- so neither may be excluded from the fallthrough test.
    assert 'orion.currentPage !== "general"' not in shell
    assert 'orion.currentPage !== "profile"' not in shell

    profile_surface = source("native_orion/qml/components/Sidebar.qml")
    for required in (
        "orion.licenseState",
        "orion.licenseKeyMasked",
        "orion.copyLicenseKey()",
        "orion.displayVersion",
        "orion.updateChannel",
        "orion.profileDiscordId",
        "orion.profileDaysLeft",
        "orion.profileHwidResetsFreeRemaining",
    ):
        assert required in profile_surface
    for objname in ("licenseStrip", "licenseStripState", "licenseStripDays",
                    "licenseFlyout"):
        assert f'objectName: "{objname}"' in profile_surface

    dashboard = source("native_orion/qml/pages/DashboardPage.qml")
    assert 'title: "Account"' not in dashboard
    assert "orion.licenseKeyMasked" not in dashboard
    assert "orion.copyLicenseKey()" not in dashboard

    # Live analytics stay on the Live page; neither surface may grow them back.
    for duplicate_live_metric in (
        "uniqueFrameFps",
        "captureResolution",
        "rttMs",
        "jitterMs",
        "shotsReleased",
        "sessionGreens",
        "sessionVerdicts",
    ):
        assert duplicate_live_metric not in dashboard
        assert duplicate_live_metric not in profile_surface

    assert 'label: "Controller"' not in dashboard
    assert "Physical Sony" not in dashboard


def test_sidebar_and_tour_only_name_current_customer_surfaces() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    tour = source("native_orion/qml/components/FirstRunTour.qml")

    assert 'statusText: root.controllerReady ? "Physical Sony"' not in sidebar
    assert 'label: "Controller"' not in sidebar
    # [2026-09-14 owner] the tour used to say "Overview shows readiness, Setup holds
    # stream and profile configuration". Overview no longer exists, and no tour step
    # may route to page "general" any more.
    assert "Overview" not in tour
    assert 'page: "general"' not in tour
    # [2026-09-21] Current tour copy (the 09-14 rewrite): Live / Setup / Updates are the only
    # surfaces named, and Setup is described by what it actually holds today.
    assert "Setup holds your console, video source, and audio settings, and Updates carries release notes." in tour
    assert "Setup keeps your connection and stream settings in one place." in tour
    assert "profiles, and account" not in tour
    assert "Performance shows" not in tour
    assert 'title: "Track your results"' not in tour
    # The connect CTA copy tracks the button.
    assert "Enable Bot" not in tour
    assert "Bot + Controller" not in tour
    assert "Press Connect to start." in tour


def test_video_presets_do_not_silently_revoke_stick_shot_input() -> None:
    controller = source("native_orion/src/OrionAppController.cpp")
    preset = controller[
        controller.index("void OrionAppController::applyPreset"):
        controller.index("void OrionAppController::saveRemoteSettings")
    ]

    assert "remotePlayInputSource" not in preset
    assert "normalizedRemotePlayInputSource(value)" in controller
