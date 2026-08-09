import QtQuick
import QtQuick.Controls
import OrionNative

Item {
    id: root
    property bool checked: false
    signal toggled(bool checked)

    implicitWidth: 48
    implicitHeight: 26

    Rectangle {
        anchors.fill: parent
        radius: height / 2
        color: root.checked ? Theme.accent : Theme.bgInset
        border.color: root.checked ? Theme.accentBorder : Theme.borderSoft
        border.width: 1

        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
        Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
    }

    Rectangle {
        width: 20
        height: 20
        radius: 10
        x: root.checked ? root.width - width - 4 : 4
        y: 3
        color: root.checked ? "#FFFFFF" : Theme.textMuted
        Behavior on x { NumberAnimation { duration: 130; easing.type: Easing.OutCubic } }
        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
    }

    // Controlled component: do NOT mutate `checked` here. Parents bind
    // `checked: orion.<prop>`; mutating it locally would BREAK that binding and
    // desync the toggle from the real state (the glitchy-toggle bug). Emit the
    // requested value only — the parent flips the property and the binding redraws.
    MouseArea {
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.toggled(!root.checked)
    }
}
