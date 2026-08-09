"""Presentation guarantees for the source-paced live preview."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "native_orion" / "qml" / "pages" / "RemotePlayPage.qml"
CMAKE = ROOT / "native_orion" / "CMakeLists.txt"
CONTROLLER_CPP = ROOT / "native_orion" / "src" / "OrionAppController.cpp"
CONTROLLER_H = ROOT / "native_orion" / "src" / "OrionAppController.h"
REMOTE_SESSION_CPP = ROOT / "native_orion" / "src" / "RemotePlaySession.cpp"
ORION_TYPES_H = ROOT / "native_orion" / "src" / "OrionTypes.h"


def test_async_live_preview_double_buffers_without_canceling_visible_frame():
    qml = PAGE.read_text(encoding="utf-8")

    source_at = qml.index("id: previewImageA")
    image_block = qml[source_at : qml.index("// Idle placeholder", source_at)]

    assert "id: previewImageA" in image_block
    assert "id: previewImageB" in image_block
    assert image_block.count("cache: false") == 2
    assert image_block.count("asynchronous: orion.previewAsync") == 2
    assert image_block.count("retainWhileLoading: orion.previewAsync") == 2
    assert 'source: "image://remote/live/" + orion.frameSerial' not in qml
    assert "function onFrameChanged() { root.queuePreviewSerial(orion.frameSerial) }" in qml
    assert "root.previewLoadingSlot = slot" in qml
    assert 'image.source = "image://remote/live/" + serial' in qml
    assert "orion.acknowledgeRemoteFramePresented(serial)" in qml


def test_visible_preview_uses_one_exclusive_render_clock_pump_per_tick():
    qml = PAGE.read_text(encoding="utf-8")

    queue_at = qml.index("function queuePreviewSerial(serial)")
    queue = qml[queue_at : qml.index("function pumpPreviewSerial()", queue_at)]
    ready_at = qml.index("function previewLoaderStatusChanged")
    ready = qml[ready_at : qml.index("FrameAnimation {", ready_at)]
    clock_at = qml.index("id: previewRenderClock")
    clock = qml[clock_at : qml.index("ColumnLayout {", clock_at)]

    assert "root.pumpPreviewSerial()" not in queue
    assert "Qt.callLater(root.pumpPreviewSerial)" not in ready
    assert "root.visible && orion.qmlRenderMode" in clock
    assert "orion.setPreviewRenderClockActive(running)" in clock
    assert clock.index("orion.advanceRemotePreviewPresentation()") < clock.index(
        "root.pumpPreviewSerial()"
    )
    assert "Component.onDestruction: orion.setPreviewRenderClockActive(false)" in clock


def test_native_build_requires_qt_version_that_supports_frame_retention():
    cmake = CMAKE.read_text(encoding="utf-8")

    assert "find_package(Qt6 6.8 REQUIRED COMPONENTS Core Gui Widgets Network Qml Quick QuickControls2)" in cmake
    assert "find_package(Qt6 6.8 REQUIRED COMPONENTS Test)" in cmake
    assert "find_package(Qt6 6.5" not in cmake


def test_qml_line_height_uses_qt_68_compatible_default_mode():
    """Qt 6.8's instantiated controls reject the newer lineHeightMode property."""
    qml_root = ROOT / "native_orion" / "qml"
    offenders = [
        path.relative_to(qml_root).as_posix()
        for path in qml_root.rglob("*.qml")
        if "lineHeightMode:" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

    # The live activity pane intentionally uses a virtualized ListView, not a
    # QTextDocument-backed TextArea. This also avoids the Qt 6.8 TextArea
    # line-height compatibility surface entirely.
    remote_page = PAGE.read_text(encoding="utf-8")
    assert "TextArea {" not in remote_page


def test_live_activity_log_is_bounded_incremental_and_excludes_periodic_telemetry():
    """Log rendering must not rebuild a large text document on the video GUI thread."""
    qml = PAGE.read_text(encoding="utf-8")

    assert "readonly property int captureLogLimit: 36" in qml
    assert "function syncCaptureLogModel()" in qml
    assert "ListModel {" in qml
    assert 'id: captureLogModel' in qml
    assert 'objectName: "captureLogModel"' in qml
    assert "model: captureLogModel" in qml
    assert "captureLogModel.remove(0, removeCount)" in qml
    assert 'captureLogModel.append({ "line": next[k] })' in qml
    assert "qml_preview_pipeline|preview_pipeline:" in qml
    assert "preview_stats:|capture health:|shm preview frame read:" in qml
    assert "detection presence:" in qml
    assert "sidecar:|detector frame rejected:" in qml
    assert "release (attribution|timing|detsummary|freshness|vision|tempo|window diagnostic)" in qml
    assert "function activityTone(line)" in qml
    assert "readonly property string captureLog" not in qml


def test_preview_log_measures_final_ready_acknowledgements():
    cpp = CONTROLLER_CPP.read_text(encoding="utf-8")

    assert "qmlPreviewLastAcknowledgedSerial_" in cpp
    assert "++qmlPreviewReadyAcksWindow_" in cpp
    assert "++qmlPreviewStaleAcksWindow_" in cpp
    assert "ready_ack_fps=%8" in cpp
    assert "ready_ack_total=%9" in cpp
    assert "stale_ack=%10" in cpp
    assert "serial != frameSerial_" not in cpp
    assert "classifyRemoteFrameAck(" in cpp


def test_detector_loss_does_not_invalidate_in_flight_preview_snapshots():
    """No-meter HUD clearing must not cancel valid async frame/overlay joins."""
    cpp = CONTROLLER_CPP.read_text(encoding="utf-8")

    detector_loss_at = cpp.index("const bool hardOverlayFailure")
    detector_loss = cpp[
        detector_loss_at : cpp.index(
            "notifyTelemetryStatusAtHumanCadence(nowMeterMs)", detector_loss_at
        )
    ]
    assert "meterOverlayComputedBox_ = {};" in detector_loss
    assert "remoteFrameOverlaySnapshots_.clear();" not in detector_loss
    assert "clearUnacknowledgedFromSourceFrame(" in detector_loss

    # History is still erased on true stream lifecycle boundaries.
    assert cpp.count("remoteFrameOverlaySnapshots_.clear();") >= 2


def test_meter_telemetry_is_unboxed_without_mutating_detector_box_geometry():
    """Fixed-corner live text cannot cover or resize the exact detector truth."""
    qml = PAGE.read_text(encoding="utf-8")

    layer_at = qml.index("id: meterDebugLayer")
    layer = qml[layer_at : qml.index("// Skeleton overlay", layer_at)]
    hud_at = layer.index("id: liveMeterMetrics")
    hud = layer[hud_at :]

    assert 'objectName: "liveMeterMetricsOverlay"' in hud
    assert 'objectName: "meterInfoHud"' not in qml
    assert "anchors.right: parent.right" in hud
    assert "anchors.bottom: parent.bottom" in hud
    assert "opacity: 0.82" in hud
    assert "Rectangle {" not in hud
    assert 'objectName: "meterEtaMetric"' in hud
    assert 'objectName: "meterHoldMetric"' in hud
    assert '"ETA  " +' in hud
    assert '"HOLD " +' in hud
    assert ': "--"' not in hud
    assert "readonly property bool etaAvailable: etaMs >= 0" in hud
    assert "readonly property bool holdAvailable: holdMs >= 0" in hud
    assert "orion.showLiveMeterMetrics" in hud
    assert "&& (etaAvailable || holdAvailable)" in hud
    assert '"  ·  TIP "' not in hud

    # Display-only compaction must never resize or normalize the detector's actual lock.
    assert "readonly property real boxW: orion.meterBoxWidth * drawScale" in layer
    assert "readonly property real boxH: orion.meterBoxHeight * drawScale" in layer


def test_meter_overlay_is_post_capture_and_has_no_qml_frame_tween():
    """QML composes after the immutable frame handoff; it cannot enter bot pixels."""
    qml = PAGE.read_text(encoding="utf-8")
    cpp = CONTROLLER_CPP.read_text(encoding="utf-8")
    header = CONTROLLER_H.read_text(encoding="utf-8")

    image_at = qml.index("id: previewImageA")
    layer_at = qml.index("id: meterDebugLayer")
    layer = qml[layer_at : qml.index("// Skeleton overlay", layer_at)]
    assert image_at < layer_at
    assert "Behavior on x" not in layer
    assert "Behavior on y" not in layer
    assert "Behavior on width" not in layer
    assert "Behavior on height" not in layer
    assert "grabToImage" not in layer and "ShaderEffectSource" not in layer

    handler_at = cpp.index("void OrionAppController::handleRemoteFrame")
    handler = cpp[handler_at : cpp.index("void OrionAppController::", handler_at + 10)]
    assert handler.index("frameProvider_->setFrame(nextFrameSerial, frame)") < handler.index(
        "meterOverlayTracker_.update"
    )
    assert handler.index("remoteFrameOverlaySnapshots_.publish") < handler.index(
        "frameSerial_ = nextFrameSerial"
    )
    assert "meterBox_ = newBox" not in handler
    assert "meterOverlayComputedBox_ = newBox" in handler
    assert "rawMappedBox" in handler
    assert "frame.convertToFormat(" not in handler
    assert "QPainter" not in handler

    # Diagnostics retain raw capture truth; QML getters alone expose the display tracker.
    assert "return rectSummary(meterBoxCapture_);" in header
    assert "return meterBox_.x();" in header


def test_meter_overlay_uses_bbox_snapshot_dimensions_not_racing_capture_telemetry():
    """bbox and bbox_wh must remain one coordinate-space identity through native mapping."""
    session = REMOTE_SESSION_CPP.read_text(encoding="utf-8")
    controller = CONTROLLER_CPP.read_text(encoding="utf-8")
    types = ORION_TYPES_H.read_text(encoding="utf-8")

    assert "int bboxFrameWidth = 0;" in types
    assert "int bboxFrameHeight = 0;" in types
    assert 'msg.value(QStringLiteral("bbox_wh")).toArray()' in session
    assert "sidecarResult.bboxFrameWidth = bboxFrameWidth;" in session
    assert "sidecarResult.bboxFrameHeight = bboxFrameHeight;" in session

    detection_at = controller.index(
        "if (result.width > 0 && result.height > 0)"
    )
    detection = controller[detection_at : controller.index("lastResult_", detection_at)]
    assert "QSize captureSize(result.bboxFrameWidth, result.bboxFrameHeight);" in detection
    assert detection.index("meterBoxCaptureSize_ = captureSize") < detection.index(
        "meterBoxRing_.record"
    )

    handler_at = controller.index("void OrionAppController::handleRemoteFrame")
    handler = controller[handler_at : controller.index(
        "void OrionAppController::", handler_at + 10
    )]
    assert "meterBoxCaptureSize_.isValid()" in handler
    assert "? meterBoxCaptureSize_ : reportedCaptureSize" in handler
    assert "meterBoxCaptureSize_ != captureSize" not in handler


def test_meter_overlay_is_visible_on_capture_card_preview_without_chiaki_session():
    """Passive capture preview has authoritative detector frames and cannot cover the overlay."""
    qml = PAGE.read_text(encoding="utf-8")

    layer_at = qml.index("id: meterDebugLayer")
    layer = qml[layer_at : qml.index("// Skeleton overlay", layer_at)]
    assert "visible: (root.streamLive || root.previewActive)" in layer
    assert "&& orion.meterConfirmed" in layer
    assert "z: 20" in layer

    # Both double-buffered preview textures use at most z=2. A default-z overlay would be
    # logically visible but still paint behind the video, reproducing the live failure.
    preview_at = qml.index("id: previewImageA")
    preview = qml[preview_at:layer_at]
    assert "z: root.previewFrontSlot === 0 ? 2 : 1" in preview
    assert "z: root.previewFrontSlot === 1 ? 2 : 1" in preview
