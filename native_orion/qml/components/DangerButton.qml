import QtQuick
import QtQuick.Controls
import OrionNative

Button {
    id: control
    implicitHeight: 40
    implicitWidth: 128
    hoverEnabled: true
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontBody
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
        color: control.enabled ? (control.down ? "#B91C1C" : control.hovered ? "#F05252" : Theme.danger) : "#17202C"
        border.color: control.enabled ? (control.hovered ? "#FCA5A5" : "#F87171") : Theme.borderSoft
        border.width: 1
        scale: control.down ? 0.97 : 1.0
        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
        Behavior on scale { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
    }
}
