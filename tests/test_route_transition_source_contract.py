"""Pin the controller call ordering that the headless native route fixture models.

The real controller constructor starts helpers and its destructor sweeps client
images, so the native event-loop fixture does not instantiate that QObject.
This source contract keeps the fixture aligned with the shipped setter.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER = ROOT / "native_orion" / "src" / "OrionAppController.cpp"
QML = ROOT / "native_orion" / "qml" / "components" / "StreamSetupForm.qml"


def _method(name: str, next_name: str) -> str:
    source = CONTROLLER.read_text(encoding="utf-8")
    start = source.index(f"void OrionAppController::{name}(")
    end = source.index(f"void OrionAppController::{next_name}(", start + 1)
    return source[start:end]


def test_active_video_source_refusal_precedes_preview_retire_and_lead_commit():
    body = _method("setVideoSource", "setCaptureCardIndex")
    guard = body.index("videoSourceChangeAllowed(")
    refusal_return = body.index("return;", guard)
    preview_stop = body.index('remotePlay_.stop(QStringLiteral("video_source_change"))')
    lead_commit = body.index("switchActuationLeadVideoSource(data, norm)")
    save = body.index("saveConfigSilently(data)")
    assert guard < refusal_return < preview_stop < lead_commit < save
    assert "RemotePlayState::Connecting" in body[guard:refusal_return]
    assert "RemotePlayState::Running" in body[guard:refusal_return]
    assert "remotePlayTeardownActive_" in body[guard:refusal_return]
    assert "remotePlay_.stopping()" in body[guard:refusal_return]
    assert 'remoteState_ == QLatin1String("Disconnecting")' in body[guard:refusal_return]


def test_device_selector_and_controller_refuse_live_reselection():
    body = _method("setCaptureCardIndex", "setCaptureCardFps")
    guard = body.index("videoSourceChangeAllowed(")
    refusal_return = body.index("return;", guard)
    assign = body.index("data.captureCardIndex = value")
    assert guard < refusal_return < assign
    assert "RemotePlayState::Connecting" in body[guard:refusal_return]
    assert "RemotePlayState::Running" in body[guard:refusal_return]
    qml = QML.read_text(encoding="utf-8")
    combo = qml[qml.index("id: videoSourceCombo") : qml.index("FieldHint {", qml.index("id: videoSourceCombo"))]
    for state in ("Running", "Connecting", "Disconnecting"):
        assert f'orion.remoteState !== "{state}"' in combo
    assert "enabled: orion.captureDeviceList.length > 0 && videoSourceCombo.enabled" in qml


def test_device_reselection_retires_stale_preview_before_persisting_new_identity():
    body = _method("setCaptureCardIndex", "setCaptureCardFps")
    guard = body.index("videoSourceChangeAllowed(")
    refusal_return = body.index("return;", guard)
    selection_checked = body.index("data.captureCardIndex == value && data.captureCardDeviceId == chosenId")
    preview_stop = body.index('remotePlay_.stop(QStringLiteral("capture_device_change"))')
    clear_frame = body.index("clearRemotePreviewFrame()", preview_stop)
    assign_index = body.index("data.captureCardIndex = value")
    assign_identity = body.index("data.captureCardDeviceId = chosenId")
    save = body.index("saveConfigSilently(data)")
    assert guard < refusal_return < selection_checked < preview_stop < clear_frame
    assert clear_frame < assign_index < assign_identity < save
    assert "capturePreviewActive_ || remotePlay_.sidecarPid() != 0" in body[
        selection_checked:preview_stop
    ]
    assert "startCapturePreview(" not in body
    assert "restartSidecar(" not in body
    # In-flight stream/teardown is rejected before the preview-only stop path.
    assert 'remoteState_ == QLatin1String("Disconnecting")' in body[guard:refusal_return]
