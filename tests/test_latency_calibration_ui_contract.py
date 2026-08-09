"""Static contract for passive, zero-setup timing adaptation.

The exact-route model supplies first-shot authority and confirmed delivered shots refine it.
Diagnostic probes remain callable from native tests, but are not exposed as customer QML actions.
"""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "native_orion" / "src" / "OrionAppController.h"
SOURCE = ROOT / "native_orion" / "src" / "OrionAppController.cpp"
LIVE_PAGE = ROOT / "native_orion" / "qml" / "pages" / "RemotePlayPage.qml"
SETUP_PAGE = ROOT / "native_orion" / "qml" / "pages" / "DashboardPage.qml"


def test_timing_adapts_passively_and_has_no_manual_customer_workflow():
    live_qml = LIVE_PAGE.read_text(encoding="utf-8")
    setup_qml = SETUP_PAGE.read_text(encoding="utf-8")

    for name in (
        "latencyCalibrationActive",
        "latencyCalibrationMeasuredLeadMs",
        "latencyCalibrationSdMs",
        "latencyCalibrationStatus",
    ):
        assert name not in live_qml

    assert 'objectName: "liveMeterMetricsOverlay"' in live_qml
    assert 'objectName: "meterInfoHud"' not in live_qml
    assert '"ETA  " + liveMeterMetrics.etaMs.toFixed(0) + " ms"' in live_qml
    assert '"HOLD " + liveMeterMetrics.holdMs.toFixed(0) + " ms"' in live_qml
    assert "orion.showLiveMeterMetrics" in live_qml
    assert 'text: "FILL"' not in live_qml
    assert "orion.shotEtaToTargetMs" in live_qml
    assert "orion.shotHoldMs" in live_qml

    assert 'objectName: "passiveTimingStatus"' in setup_qml
    assert 'statusText: orion.latencyCalibrationReady' in setup_qml
    assert '"Adapting"' in setup_qml
    assert '"Route unavailable"' in setup_qml
    for removed in (
        'objectName: "timingSetupCard"',
        'objectName: "timingSetupAction"',
        "startLatencyCalibration()",
        "cancelLatencyCalibration()",
        "two controlled stages",
    ):
        assert removed not in setup_qml


def test_controller_keeps_diagnostics_internal_and_never_prompts_for_setup():
    header = HEADER.read_text(encoding="utf-8")
    source = SOURCE.read_text(encoding="utf-8")

    assert "void startLatencyCalibration();" in header
    assert "void cancelLatencyCalibration();" in header
    assert "Q_INVOKABLE void startLatencyCalibration();" not in header
    assert "Q_INVOKABLE void cancelLatencyCalibration();" not in header
    assert "AutomationEngine::latencyCalibrationStatusChanged" in source
    assert "refreshPassiveLatencyAdaptationStatus" in source
    assert "setAutomaticLatencyCalibrationMode(true)" not in source
    assert "Timing setup required" not in source
    assert "Open Setup" not in source
    assert "Verifying automatic timing authority" in source
