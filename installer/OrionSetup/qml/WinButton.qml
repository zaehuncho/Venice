import QtQuick

// A small circular titlebar button (minimize / close).
Rectangle {
    id: root
    property string glyph: ""
    signal clicked()

    width: 14; height: 14
    radius: 7
    color: Theme.raised2
    border.width: 1
    border.color: Theme.line

    Text {
        anchors.centerIn: parent
        anchors.verticalCenterOffset: -0.5
        text: root.glyph
        color: ma.containsMouse ? Theme.text : Theme.dim
        font.family: Theme.sans
        font.pixelSize: 10
    }
    MouseArea {
        id: ma
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.PointingHandCursor
        onClicked: root.clicked()
    }
}
