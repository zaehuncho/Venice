"""Manual results stay separate from detector estimates and timing calibration."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "native_orion/qml/pages/RemotePlayPage.qml"
HEADER = ROOT / "native_orion/src/OrionAppController.h"
SOURCE = ROOT / "native_orion/src/OrionAppController.cpp"


def test_manual_results_ui_is_retired_from_the_live_page():
    """Owner 2026-09-08: the Make/Miss counter is gone from the Live page. The backend tally stays
    (inert without a caller) so no controller surface changed; only the QML must be clean."""
    page = PAGE.read_text(encoding="utf-8")
    for retired in ('manualShotTally', 'resetManualResultsDialog', 'TallyChoice', 'TallyAction',
                    'orion.manualShotMakes', 'orion.manualShotMisses', 'orion.manualShotTotal',
                    'orion.recordManualShotResult', 'orion.undoManualShotResult',
                    'orion.resetManualShotResults', '"MANUAL RESULTS'):
        assert retired not in page, retired
    # the action row still sits directly under the preview
    assert 'id: startStreamBtn' in page


def test_manual_tally_methods_do_not_teach_or_change_controller_timing():
    source = SOURCE.read_text(encoding="utf-8")
    methods = source.split('void OrionAppController::recordManualShotResult', 1)[1].split(
        'void OrionAppController::observeBannerVerdict', 1)[0]
    for forbidden in ('automation_', 'config_', 'saveConfig', 'recordSessionGrade(',
                      'reportLeadCalibrationVerdict(', 'learnFromOutcome(',
                      'sessionGreens_', 'sessionVerdicts_'):
        assert forbidden not in methods
    assert methods.count('emit manualShotTallyChanged();') == 3
    assert methods.count('source=user') == 3


def test_manual_counts_have_independent_notify_and_survive_stream_reconnect():
    header = HEADER.read_text(encoding="utf-8")
    for name in ('manualShotMakes', 'manualShotMisses', 'manualShotTotal'):
        assert f'Q_PROPERTY(int {name} READ {name} NOTIFY manualShotTallyChanged)' in header
    assert 'ManualShotTally manualShotTally_;' in header
    source = SOURCE.read_text(encoding="utf-8")
    # Reset is exclusively the explicit user's action, not a connect/stream event.
    assert source.count('manualShotTally_.reset()') == 1
    assert 'void OrionAppController::resetManualShotResults()' in source
