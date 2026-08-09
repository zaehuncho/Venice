from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "native_orion" / "qml"


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_venice_brand_and_palette_are_centralized() -> None:
    theme = source("native_orion/qml/Theme.qml")
    assert 'productName: "Venice"' in theme
    assert 'productMark: "VENICE"' in theme
    assert 'brandAccent: "#2D7DFF"' in theme
    assert 'bgShell: "#020509"' in theme
    assert 'bgSidebar: "#050B14"' in theme
    # Detection-box stroke colour, owner-directed twice:
    #   2026-08-04 blue -> violet #CC44FF (sampled from their reference screenshot, hue 285 deg)
    #   2026-08-06 violet -> bright blue #00A8FF (owner: "bright blue meter detection box as the
    #     default"), with a legacy-default migration in AppConfig so the persisted #CC44FF flips
    #     over while any OTHER persisted hex stays an explicit user pick.
    # meterLockKeyline survives both changes and is the load-bearing part: it is the dark
    # inner/outer ring that keeps the stroke legible against a floodlit white court, where a
    # bare saturated stroke dissolves -- which is what sank the styling before it existed.
    assert 'meterLock: "#00A8FF"' in theme
    assert 'meterLockKeyline:' in theme


def test_launcher_uses_static_venice_presentation() -> None:
    main = source("native_orion/qml/Main.qml")
    assert "VeniceBackdrop {" in main
    assert "text: Theme.productName" in main
    assert "Theme.accent = Theme.brandAccent" in main
    # DELIBERATE CONTRACT CHANGE 2026-08-06: this used to assert `title: "Orion"`,
    # described as a hidden compatibility handle for orion:// forwarding rather than
    # customer chrome. That was wrong about one thing — the window title IS customer
    # chrome: it is what the taskbar, Alt-Tab and Task Manager display, so the app
    # still read "Orion" everywhere outside its own painted UI. Retitled to Venice.
    #
    # The title doubles as the deep-link lookup handle, so main.cpp's FindWindowW
    # must match it exactly; that pairing is asserted in the runtime-contract test
    # below so the two can never drift apart.
    assert 'title: "Venice"' in main

    for page in ("AuthGate.qml", "LegalGate.qml", "StreamSetupGate.qml", "UpdateGatePage.qml"):
        content = source(f"native_orion/qml/pages/{page}")
        assert "VeniceBackdrop {" in content
        assert "StarBackdrop {" not in content

    backdrop = source("native_orion/qml/components/VeniceBackdrop.qml")
    assert "Canvas {" not in backdrop
    assert "Timer {" not in backdrop
    assert "Animation {" not in backdrop


def test_customer_navigation_is_compact_and_compatible() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    for key, label in (
        ("remotePlay", "Live"),
        ("general", "Overview"),
        ("dashboard", "Setup"),
        ("patchNotes", "Updates"),
    ):
        assert f'key: "{key}", label: "{label}"' in sidebar
    assert 'text: "Quick Start"' in sidebar
    assert "controllerIsPhysicalSony" not in sidebar
    assert 'label: "Controller"' not in sidebar
    assert 'label: "Version"' not in sidebar


def test_meter_controls_are_autonomous_not_a_setup_batch() -> None:
    meter = source("native_orion/qml/components/MeterConfigPanel.qml")
    assert 'title: "Meter Profile"' in meter
    assert 'subtitle: "Automatic detection on every shot"' in meter
    assert 'text: "Style"' in meter
    assert 'text: "Color"' in meter
    assert 'text: "Tempo Remap"' in meter
    assert 'text: "Input"' in meter
    assert '{ l: "Square", v: "square" }' in meter
    assert '{ l: "Stick", v: "stick" }' in meter
    assert '{ l: "Both", v: "both" }' in meter
    assert "orion.tempoInputSource = modelData.v" in meter
    assert "Calibrate Meter" not in meter
    assert "trainRequested" not in meter
    assert "meterCalibration" not in meter

    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    assert "startMeterCalibration" not in live
    assert 'title: "Live Timing"' not in live
    assert 'label: "Video"' not in live
    assert 'label: "Controller"' not in live
    # DELIBERATE CONTRACT CHANGE 2026-08-06: this file used to assert
    # `label: "Timing"` NOT in the Live page (the old "Live Timing" card was
    # removed in the Venice cleanup and readiness lived on Setup only). The
    # cold-install audit reversed that: the engine fails closed until
    # measured-lead authority exists, every press passes through silently, and
    # the ONLY readiness surface was a pill on the Setup page — so a warming-up
    # bot on the Live page read as a broken product. The Live page now carries
    # a display-only Timing pill (objectName liveTimingStatus) mirroring the
    # Setup pill's bindings, plus a warming-up banner over the capture. Both
    # are readiness surfaces, not controls — the old prohibition was against a
    # manual timing CONTROL surface, which is still asserted above
    # (startMeterCalibration / "Live Timing").
    assert 'objectName: "liveTimingStatus"' in live
    assert 'objectName: "timingWarmupBanner"' in live
    assert "orion.latencyCalibrationReady" in live


def test_live_meter_overlay_and_truth_hud_remain_real_and_visible() -> None:
    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    for contract in (
        'objectName: "meterDebugLayer"',
        'objectName: "meterLockBox"',
        'objectName: "liveMeterMetricsOverlay"',
        "orion.meterConfirmed",
        "orion.meterBoxWidth > 0",
        "orion.liveFrameWidth > 0",
        "orion.shotEtaToTargetMs",
        "orion.shotHoldMs",
        "Theme.meterLock",
    ):
        assert contract in live
    assert 'objectName: "meterInfoHud"' not in live
    assert '"AWAITING SHOT"' not in live
    assert ': "--"' not in live[
        live.index('id: liveMeterMetrics') : live.index("// Skeleton overlay")
    ]
    assert "orion.showLiveMeterMetrics" in live
    assert "&& (etaAvailable || holdAvailable)" in live
    assert "root.streamLive || root.previewActive" in live
    meter_layer = live[live.index('id: meterDebugLayer') : live.index(
        "// CONFIRMED-METER GATE", live.index('id: meterDebugLayer')
    )]
    assert "z: 20" in meter_layer
    assert "above every\n                            // non-modal capture HUD chip" in meter_layer


def test_setup_defaults_to_production_setup_and_removes_performance_surface() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")
    assert "sourceComponent: setupDash" in dashboard
    assert 'title: "Production Setup"' in dashboard
    assert "dashTab" not in dashboard
    assert 'label: "Network"' not in dashboard
    assert 'label: "Performance"' not in dashboard
    assert "performanceDash" not in dashboard
    assert "Advanced Detection" not in dashboard


def test_network_telemetry_is_removed_from_customer_setup_surface() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")
    for removed in (
        'objectName: "networkWaitingState"',
        'objectName: "courtTelemetry"',
        'objectName: "packetCaptureOptIn"',
        "orion.rttMs",
        "orion.jitterMs",
        "orion.inboundPackets",
        "orion.outboundPackets",
    ):
        assert removed not in dashboard


def test_network_capture_toggle_drives_network_enabled_and_refreshes_live_state() -> None:
    # [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in flag
    # (passiveSniffingEnabled / network_packet_capture_opt_in) is DELETED per owner
    # decision: everything network-side ships on by default. The packetCaptureEnabled
    # property survives as the network feature's own switch, and the deleted flag must
    # stay deleted.
    controller = source("native_orion/src/OrionAppController.h")
    implementation = source("native_orion/src/OrionAppController.cpp")

    assert (
        "Q_PROPERTY(bool packetCaptureEnabled READ packetCaptureEnabled "
        "WRITE setPacketCaptureEnabled NOTIFY settingsChanged)"
    ) in controller
    assert "void OrionAppController::setPacketCaptureEnabled(bool value)" in implementation
    assert "data.networkEnabled = value;" in implementation
    assert "networkBridge_.start();" in implementation
    assert "networkBridge_.stop();" in implementation
    # The deleted flag must not creep back into any of its old homes.
    for deleted in ("passiveSniffingEnabled", "network_packet_capture_opt_in"):
        assert deleted not in implementation
        assert deleted not in source("native_orion/src/AppConfig.cpp")
    assert "passiveSniffingEnabled" not in source("native_orion/src/AppConfig.h")
    connection_handler = implementation[
        implementation.index("&NetworkBridge::connectionChanged"):
        implementation.index("networkBridge_.setConsoleIp", implementation.index("&NetworkBridge::connectionChanged"))
    ]
    assert "emit telemetryChanged();" in connection_handler


def test_activity_log_is_copy_pasteable_for_support() -> None:
    # "Logs are not copy-pasteable": users were screenshotting the Activity
    # view to get its text into an AI chat for debugging. Contract: one click
    # copies the BOUNDED TAIL of the on-disk engineer log (the 160-line UI
    # ring covers only seconds of an active session) straight from C++ to the
    # clipboard (the copyLicenseKey idiom), after a sharing redaction pass —
    # the paste target is explicitly outside the app. When the disk log is
    # unreadable the copy falls back to the in-memory ring, so the button
    # never silently no-ops.
    controller = source("native_orion/src/OrionAppController.h")
    implementation = source("native_orion/src/OrionAppController.cpp")
    policy = source("native_orion/src/UiNotificationPolicy.h")
    assert "Q_INVOKABLE void copyActivityLog();" in controller
    assert "void OrionAppController::copyActivityLog()" in implementation
    assert "readLogTailForSharing" in implementation
    assert '/logs/orion_native.log"' in implementation
    assert "serializeActivityLogForSharing(logs_)" in implementation  # ring fallback
    assert "redactActivityLineForSharing" in policy
    # The tail bounds are named constants, not magic numbers.
    assert "kActivityShareTailMaxBytes = 256 * 1024" in policy
    assert "kActivityShareTailMaxLines = 1500" in policy

    # Both Activity surfaces expose the button: the Live page's collapsed
    # ACTIVITY card and the Debug page's LogViewer. Each also carries the
    # escape hatch for history older than the tail bound: open the logs
    # folder (pre-existing invokable, newly wired).
    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    assert "orion.copyActivityLog()" in live
    assert "orion.openLogsFolder()" in live
    viewer = source("native_orion/qml/components/LogViewer.qml")
    assert "orion.copyActivityLog()" in viewer
    assert "orion.openLogsFolder()" in viewer
    # In-place selection stays per-line on the colour-coded ListView (a flat
    # TextArea would lose severity colouring); Copy all is the primary path.
    assert "selectByMouse: true" in viewer
    assert "readOnly: true" in viewer


def test_meter_delay_controls_are_wired_end_to_end() -> None:
    # SHIP-BLOCKER regression guard: the Meter Delay card binds three names on
    # `orion`. QML resolves a missing property to `undefined` at RUNTIME (no build
    # failure), so when OrionAppController shipped without these Q_PROPERTYs the
    # toggle rendered unchecked and every assignment died with "Cannot assign to
    # non-existent property" — the whole subsystem was untestable from the UI.
    # Native meta-object coverage: native_orion/tests/MeterDelaySettingsPropertyTests.cpp.
    meter = source("native_orion/qml/components/MeterConfigPanel.qml")
    controller = source("native_orion/src/OrionAppController.h")
    implementation = source("native_orion/src/OrionAppController.cpp")

    assert "checked: orion.meterDelayEnabled" in meter
    assert "orion.meterDelayEnabled = checked" in meter
    assert "value: orion.meterDelayMs" in meter
    assert "orion.meterDelayMs = value" in meter
    assert "checked: orion.meterDelayBypassOnDefense" in meter
    assert "orion.meterDelayBypassOnDefense = checked" in meter

    # The leading "\n    " pins the declaration at the start of its line: a plain
    # substring check also matches a commented-out declaration ("// Q_PROPERTY(...)"),
    # i.e. it would pass with the bug present. Verified by falsification 2026-08-08.
    assert (
        "\n    Q_PROPERTY(bool meterDelayEnabled READ meterDelayEnabled "
        "WRITE setMeterDelayEnabled NOTIFY settingsChanged)"
    ) in controller
    assert (
        "\n    Q_PROPERTY(int meterDelayMs READ meterDelayMs "
        "WRITE setMeterDelayMs NOTIFY settingsChanged)"
    ) in controller
    assert (
        "\n    Q_PROPERTY(bool meterDelayBypassOnDefense READ meterDelayBypassOnDefense "
        "WRITE setMeterDelayBypassOnDefense NOTIFY settingsChanged)"
    ) in controller

    # Setters persist through AppConfig (saveConfigSilently emits settingsChanged)
    # AND re-prime the live actuator so a change takes effect without a restart.
    assert "void OrionAppController::setMeterDelayEnabled(bool value)" in implementation
    assert "data.meterDelayEnabled = value;" in implementation
    assert "void OrionAppController::setMeterDelayMs(int value)" in implementation
    assert "data.meterDelayMs = clamped;" in implementation
    assert "void OrionAppController::setMeterDelayBypassOnDefense(bool value)" in implementation
    assert "data.meterDelayBypassOnDefense = value;" in implementation
    # One shared config->runtime mapping: the constructor priming plus all three
    # setters route through applyMeterDelayRuntimeConfig().
    assert implementation.count("applyMeterDelayRuntimeConfig();") >= 4
    assert "meterDelay_.setEnabled(cfg.meterDelayEnabled);" in implementation
    assert "meterDelay_.setManualDelayMs(static_cast<double>(cfg.meterDelayMs));" in implementation


def test_internal_orion_runtime_contracts_are_not_rebranded() -> None:
    cmake = source("native_orion/CMakeLists.txt")
    main_cpp = source("native_orion/src/main.cpp")
    remote = source("native_orion/src/RemotePlaySession.cpp")
    updater = source("native_orion/src/updater_main.cpp")

    assert "qt_add_qml_module(OrionNative" in cmake
    assert 'QStringLiteral("orion://")' in main_cpp
    assert 'L"Local\\\\OrionNativeLauncher"' in main_cpp
    # The deep-link lookup title tracks the WINDOW title (customer-facing, now
    # "Venice") rather than the internal slugs around it. These two literals are a
    # matched pair across a C++/QML boundary that no compiler checks, and a mismatch
    # fails silently — forwarding finds nothing and a duplicate instance launches —
    # so both halves are pinned here together.
    assert 'FindWindowW(nullptr, L"Venice")' in main_cpp
    assert 'title: "Venice"' in source("native_orion/qml/Main.qml")
    assert 'QStringLiteral("OrionPreviewFrame_%1")' in remote
    assert "OrionUpdater.exe" in updater
    assert 'QStringLiteral("OrionNative.exe")' in updater
