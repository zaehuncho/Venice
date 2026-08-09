import QtQuick

// Secondary "ghost" button — transparent with a hairline border.
Item {
    id: root
    property string text: ""
    signal clicked()

    implicitHeight: 42
    implicitWidth: label.implicitWidth + 40

    Rectangle {
        anchors.fill: parent
        radius: 9
        color: "transparent"
        border.width: 1
        border.color: ma.containsMouse ? Theme.dim : Theme.line
        y: ma.pressed ? 1 : 0
    }
    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: ma.containsMouse ? Theme.text : Theme.muted
        font.family: Theme.sans
        font.pixelSize: 14
        font.weight: Font.DemiBold
    }
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
