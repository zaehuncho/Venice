import QtQuick
import QtQuick.Controls
import OrionNative

Button {
    id: control
    implicitHeight: 40
    implicitWidth: 128
    hoverEnabled: true
    font.family: Theme.fontUi
    font.pixelSize: 13
    font.weight: Font.DemiBold

    contentItem: Text {
        text: control.text
        color: control.enabled ? "#FFFFFF" : Theme.textFaint
        horizontalAlignment: Text.AlignHCenter
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        font: control.font
    }

    background: Rectangle {
        radius: Theme.radiusControl
        color: control.enabled ? (control.down ? Theme.accentPressed : control.hovered ? Theme.accentHover : Theme.accent) : "#17202C"
        border.color: control.enabled ? (control.hovered ? Qt.lighter(Theme.accent, 1.6) : Theme.accentBorder) : Theme.borderSoft
        border.width: 1
        scale: control.down ? 0.97 : 1.0
        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
        Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
    }
}
