import QtQuick
import OrionNative

// Compact truth-only readout attached to the frame-joined meter lock.
//
// The page owns visibility and placement; this component owns no timer, no
// prediction, no smoothing and no animation. All three values are already
// formatted by OrionAppController from one native telemetry snapshot. "--" is
// therefore an honest unavailable value, never a client-side fallback.
//
// The visual intentionally has no card, fill or border. A dense monospace stack
// with a dark glyph outline remains readable on white/red courts while leaving
// the moving game image open. Captions use the same vivid accent as the meter
// lock, so the text and outline read as one instrument without another box.
Item {
    id: root

    property bool measured: false
    property bool live: false
    property bool commandLate: false
    property color accentColor: Theme.meterLock

    // Pin the footprint to the widest shipped value. Centering an implicit
    // width above the lock made the whole label step sideways whenever TIP or
    // FIRE gained/lost a digit even though the meter box itself was stable.
    implicitWidth: 76
    implicitHeight: stack.implicitHeight
    width: implicitWidth
    height: implicitHeight
    opacity: (root.measured || root.live) ? 1.0 : 0.68

    component Field: Item {
        id: field
        property string caption: ""
        property string value: "--"
        property color valueColor: Theme.textPrimary
        property color captionColor: Theme.meterLock

        // Clean readout (2026-09-10): captions and values share one size and,
        // by default, one colour, so the three rows read as one label.
        readonly property real captionColumn: 30
        readonly property real captionGap: 2

        implicitWidth: captionColumn + captionGap + valueText.implicitWidth
        implicitHeight: Math.max(captionText.implicitHeight,
                                 valueText.implicitHeight)

        Text {
            id: captionText
            anchors.left: parent.left
            anchors.verticalCenter: parent.verticalCenter
            width: field.captionColumn
            text: field.caption
            color: field.captionColor
            style: Text.Outline
            styleColor: "#F2050810"
            font.family: Theme.fontMono
            font.pixelSize: 10
            font.weight: Font.Bold
            font.letterSpacing: 0.2
        }

        Text {
            id: valueText
            anchors.left: parent.left
            anchors.leftMargin: field.captionColumn + field.captionGap
            anchors.verticalCenter: parent.verticalCenter
            text: field.value
            color: field.valueColor
            style: Text.Outline
            styleColor: "#F2050810"
            font.family: Theme.fontMono
            font.pixelSize: 10
            font.weight: Font.Bold
        }
    }

    Column {
        id: stack
        anchors.left: parent.left
        anchors.top: parent.top
        spacing: 0

        Field {
            objectName: "meterHudFillRow"
            caption: "FILL"
            captionColor: root.accentColor
            value: orion.meterHudFillLine
            valueColor: root.measured ? root.accentColor : Theme.textMuted
        }

        Field {
            objectName: "meterHudTipRow"
            caption: "TIP"
            captionColor: root.accentColor
            value: orion.meterHudTipLine
            valueColor: root.live ? root.accentColor : Theme.textMuted
        }

        Field {
            objectName: "meterHudFireRow"
            caption: "FIRE"
            captionColor: root.accentColor
            value: orion.meterHudFireLine
            valueColor: !root.live ? Theme.textMuted
                        : root.commandLate ? Theme.danger : root.accentColor
        }
    }
}
