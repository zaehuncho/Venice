import QtQuick
import OrionNative

// [PRESSED OVERLAY 2026-08-08] Local "PRESSED" input-acknowledgment badge
// (owner request). The input hook forwards a physical Square press to the PS5
// undelayed, but the visible meter feedback lags by V + meter_delay ms. This
// badge lights the INSTANT the press is registered (orion.physicalSquarePressed
// flips on the same input-poll tick as the "Physical shot epoch" record),
// decoupling perceived responsiveness from video-pipeline latency.
//
// Purely local display: nothing is sent to the console, and this is a UI
// element — NOT part of the meter-box/detection overlay family (BOX_PREDICT /
// BOX_TIGHT never touch it). It coexists with the LIVE / PREVIEW ONLY badges.
//
// Motion contract: the rising edge is deliberately UNANIMATED (opacity snaps on
// the tick the press lands); only the release fades, over ~150 ms.
Rectangle {
    id: badge

    // Bind to orion.physicalSquarePressed at the instantiation site.
    property bool pressed: false
    // Owner-tunable steady-state visibility while held (see instantiation site).
    property real shownOpacity: 0.92

    implicitWidth: Math.max(80, pressedRow.implicitWidth + 24)
    implicitHeight: 26
    radius: height / 2
    // Same scrim family as the LIVE badge so the badges read as one system;
    // the accent does the talking via border, dot, and text.
    color: "#CC05080D"
    border.color: Theme.accentBorder
    border.width: 1
    opacity: 0
    visible: opacity > 0.001

    onPressedChanged: {
        if (badge.pressed) {
            // Press: instant. Stop any in-flight fade and snap on.
            releaseFade.stop()
            badge.opacity = badge.shownOpacity
        } else {
            // Release: ~150 ms fade-out.
            releaseFade.restart()
        }
    }

    NumberAnimation {
        id: releaseFade
        target: badge
        property: "opacity"
        to: 0
        duration: 150
        easing.type: Easing.OutQuad
    }

    Row {
        id: pressedRow
        anchors.centerIn: parent
        spacing: 6

        Rectangle {
            width: 8
            height: 8
            radius: 4
            anchors.verticalCenter: parent.verticalCenter
            color: Theme.accent
        }

        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: "PRESSED"
            color: Theme.textPrimary
            font.family: Theme.fontUi
            font.pixelSize: 10
            font.weight: Font.Bold
            font.letterSpacing: 1.2
        }
    }
}
