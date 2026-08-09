import QtQuick

// A pre-checked requirement chip (green check + label) from the WELCOME screen.
Rectangle {
    id: root
    property string text: ""
    radius: 999
    color: Theme.raised
    border.width: 1
    border.color: Theme.lineSoft
    implicitHeight: 30
    implicitWidth: row.implicitWidth + 24

    Row {
        id: row
        anchors.centerIn: parent
        spacing: 7
        CheckMark {
            anchors.verticalCenter: parent.verticalCenter
            width: 13; height: 13
            stroke: Theme.green
            weight: 3
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.text
            color: Theme.muted
            font.family: Theme.sans
            font.pixelSize: 13
        }
    }
}
