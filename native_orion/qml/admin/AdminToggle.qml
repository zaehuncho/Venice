import QtQuick
import QtQuick.Layouts

// Controlled toggle: parents bind `checked` and react to toggled(); the toggle
// never mutates its own state so the binding to the source of truth survives.
RowLayout {
    id: root
    property bool checked: false
    property string label: ""
    signal toggled(bool checked)

    spacing: 10

    Item {
        Layout.preferredWidth: 44
        Layout.preferredHeight: 24
        opacity: root.enabled ? 1.0 : 0.5

        Rectangle {
            anchors.fill: parent
            radius: height / 2
            color: root.checked ? Theme.accent : Theme.bgInset
            border.color: root.checked ? Theme.accentBorder : Theme.borderSoft
            border.width: 1
            Behavior on color { ColorAnimation { duration: Theme.motionBase } }
        }

        Rectangle {
            width: 18
            height: 18
            radius: 9
            x: root.checked ? parent.width - width - 3 : 3
            y: 3
            color: root.checked ? "#FFFFFF" : Theme.textMuted
            Behavior on x { NumberAnimation { duration: 130; easing.type: Easing.OutCubic } }
        }

        MouseArea {
            anchors.fill: parent
            enabled: root.enabled
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: root.toggled(!root.checked)
        }
    }

    Text {
        visible: root.label.length > 0
        Layout.fillWidth: true
        text: root.label
        color: root.enabled ? Theme.textSecondary : Theme.textFaint
        font.pixelSize: Theme.fontBody
        wrapMode: Text.WordWrap
    }
}
