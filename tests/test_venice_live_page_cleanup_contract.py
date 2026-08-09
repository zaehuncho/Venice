"""Focused contracts for the minimal Venice Live page."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "native_orion" / "qml" / "pages" / "RemotePlayPage.qml"
PANEL = ROOT / "native_orion" / "qml" / "components" / "MeterConfigPanel.qml"
CONFIG_H = ROOT / "native_orion" / "src" / "AppConfig.h"
CONFIG_CPP = ROOT / "native_orion" / "src" / "AppConfig.cpp"
CONTROLLER_H = ROOT / "native_orion" / "src" / "OrionAppController.h"


def live_page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_meter_truth_hud_is_transparent_fixed_and_never_attached_to_meter() -> None:
    qml = live_page()
    hud_at = qml.index("id: liveMeterMetrics")
    hud = qml[hud_at : qml.index("// Skeleton overlay", hud_at)]

    assert 'objectName: "liveMeterMetricsOverlay"' in hud
    assert 'objectName: "meterInfoHud"' not in qml
    assert "anchors.right: parent.right" in hud
    assert "anchors.bottom: parent.bottom" in hud
    assert "opacity: 0.82" in hud
    assert "Rectangle {" not in hud
    assert "orion.showLiveMeterMetrics" in hud
    for live_value in (
        "orion.shotEtaToTargetMs",
        "orion.shotHoldMs",
    ):
        assert live_value in hud
    assert ': "--"' not in hud
    assert '"AWAITING SHOT"' not in hud
    assert '"METER LOCKED"' not in hud
    assert "readonly property bool etaAvailable: etaMs >= 0" in hud
    assert "readonly property bool holdAvailable: holdMs >= 0" in hud
    assert "&& (etaAvailable || holdAvailable)" in hud


def test_live_metric_toggle_is_persisted_default_on() -> None:
    panel = PANEL.read_text(encoding="utf-8")
    config_h = CONFIG_H.read_text(encoding="utf-8")
    config_cpp = CONFIG_CPP.read_text(encoding="utf-8")
    controller_h = CONTROLLER_H.read_text(encoding="utf-8")

    # History, because this control has moved twice and the reasons differ:
    #   2026-08-04 removed. 2026-08-05 RESTORED — but the revert was not a judgement on
    #     the removal: a layout change shipped in the same commit scrambled the cards, so
    #     the whole UI commit was backed out at the owner's request and this came with it.
    #   2026-08-06 removed again, deliberately and on its own, as part of cutting the
    #     customer surface to controls people actually use.
    # The SETTING is untouched — the overlay still reads showLiveMeterMetrics and still
    # defaults ON, so this is a UI removal with no behaviour change. Those assertions stay
    # below precisely so a future cleanup cannot quietly delete the setting too and turn
    # the overlay off for everyone.
    assert 'objectName: "liveMeterMetricsToggle"' not in panel
    assert "bool showLiveMeterMetrics = true;" in config_h
    assert 'QStringLiteral("show_live_meter_metrics")' in config_cpp
    assert 'obj, "show_live_meter_metrics", data_.showLiveMeterMetrics' in config_cpp
    assert "Q_PROPERTY(bool showLiveMeterMetrics" in controller_h


def test_live_page_removes_redundant_status_and_timing_cards() -> None:
    qml = live_page()

    # DELIBERATE CONTRACT CHANGE 2026-08-06: this test used to ban StatusPill
    # (and label: "Timing") from the Live page outright — the old cleanup that
    # removed the redundant Video/Controller/Timing status cards. The
    # cold-install audit reversed it for TIMING ONLY: the engine fails closed
    # until measured-lead authority exists, every press passes through
    # silently, and the only readiness surface was on the Setup page — so a
    # warming-up bot on the Live page read as a broken product. Exactly ONE
    # pill is now allowed back: the display-only Timing readiness pill
    # (objectName liveTimingStatus, bound to latencyCalibrationReady only).
    # Everything else this test removed stays removed, and the pill count is
    # pinned to 1 so the old status-card sprawl cannot quietly regrow.
    assert qml.count("StatusPill {") == 1
    assert 'objectName: "liveTimingStatus"' in qml
    assert 'label: "Timing"' in qml
    for removed_status in (
        'label: "Video"',
        'label: "Controller"',
    ):
        assert removed_status not in qml

    for removed_timing_card_content in (
        'id: latencyCalibrationCard',
        'id: latencyCalBody',
        'title: "Live Timing"',
        'text: "LIVE ADAPTATION"',
        'text: "Measured lead"',
    ):
        assert removed_timing_card_content not in qml


def test_live_cleanup_preserves_detector_and_stream_controls() -> None:
    qml = live_page()
    layer_at = qml.index("id: meterDebugLayer")
    layer = qml[layer_at : qml.index("// Skeleton overlay", layer_at)]

    for detector_contract in (
        'objectName: "meterDebugLayer"',
        'objectName: "meterLockBox"',
        "visible: (root.streamLive || root.previewActive)",
        "&& orion.meterConfirmed",
        "readonly property real boxX: drawX + orion.meterBoxX * drawScale",
        "readonly property real boxY: drawY + orion.meterBoxY * drawScale",
        "readonly property real boxW: orion.meterBoxWidth * drawScale",
        "readonly property real boxH: orion.meterBoxHeight * drawScale",
    ):
        assert detector_contract in layer

    # Keep the layer's explicit stacking contract without pinning this cleanup
    # test to a particular z value owned by the overlay safety tests.
    assert "\n                            z: " in layer
    for control_contract in (
        "onClicked: orion.connectRemotePlay()",
        "onClicked: orion.disconnectRemotePlay()",
        "orion.startCapturePreview()",
    ):
        assert control_contract in qml
