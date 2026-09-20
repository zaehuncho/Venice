import QtQuick
import QtQuick.Controls

// kind: "primary" | "secondary" | "danger" | "ghost"
Button {
    id: control

    property string kind: "primary"
    property bool compact: false

    implicitHeight: compact ? 32 : 38
    leftPadding: compact ? 12 : 16
    rightPadding: compact ? 12 : 16
    font.family: Theme.fontUi
    font.pixelSize: compact ? Theme.fontSmall : Theme.fontBody
    font.weight: Font.DemiBold
    hoverEnabled: true

    readonly property bool isPrimary: kind === "primary"
    readonly property bool isDanger: kind === "danger"
    readonly property bool isGhost: kind === "ghost"

    contentItem: Text {
        text: control.text
        color: !control.enabled ? Theme.textFaint
             : (control.isPrimary || control.isDanger) ? Theme.textOnAccent
             : control.isGhost ? Theme.textMuted
             : Theme.textSecondary
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        font: control.font
    }

    background: Rectangle {
        radius: Theme.radiusControl
        color: {
            if (!control.enabled)
                return control.isGhost ? "transparent" : Theme.bgField
            if (control.isPrimary)
                return control.down ? Theme.accentPressed : control.hovered ? Theme.accentHover : Theme.accent
            if (control.isDanger)
                return control.down ? Qt.darker(Theme.danger, 1.25) : control.hovered ? "#FF5B5B" : Theme.danger
            if (control.isGhost)
                return control.down ? Theme.bgField : control.hovered ? Theme.bgCardHover : "transparent"
            return control.down ? Theme.bgField : control.hovered ? Theme.bgCardHover : Theme.bgInset
        }
        border.color: {
            if (!control.enabled)
                return control.isGhost ? "transparent" : Theme.borderSoft
            if (control.isPrimary)
                return Theme.accentBorder
            if (control.isDanger)
                return "#FF8A8A"
            if (control.isGhost)
                return control.hovered ? Theme.borderSoft : "transparent"
            return Theme.borderStrong
        }
        border.width: 1
    }
}
