import QtQuick
import QtQuick.Controls
import OrionNative

Slider {
    id: control
    implicitHeight: 34
    live: true
    hoverEnabled: true
    snapMode: Slider.NoSnap
    wheelEnabled: false

    background: Rectangle {
        x: control.leftPadding
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: control.availableWidth
        height: control.hovered || control.pressed ? 7 : 6
        radius: 3
        color: Theme.borderSoft
        Behavior on height { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }

        Rectangle {
            width: control.visualPosition * parent.width
            height: parent.height
            radius: 3
            color: Theme.accent
            Behavior on color { ColorAnimation { duration: Theme.motionBase } }
        }
    }

    handle: Rectangle {
        x: control.leftPadding + control.visualPosition * (control.availableWidth - width)
        y: control.topPadding + control.availableHeight / 2 - height / 2
        width: control.pressed ? 22 : 20
        height: control.pressed ? 22 : 20
        radius: width / 2
        color: control.pressed ? "#DCEBFF" : "#F4F7FA"
        border.color: control.hovered || control.pressed ? Theme.accentBorder : Theme.accent
        border.width: 2
        Behavior on width { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on height { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
    }
}
