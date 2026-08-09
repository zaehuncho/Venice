import QtQuick

// The bright green primary action button from the mockup.
Item {
    id: root
    property string text: ""
    signal clicked()

    implicitHeight: 42
    implicitWidth: label.implicitWidth + 40

    Rectangle {
        id: bg
        anchors.fill: parent
        radius: 9
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.greenBright }
            GradientStop { position: 1.0; color: Theme.green }
        }
        opacity: ma.pressed ? 0.92 : (ma.containsMouse ? 1.0 : 0.96)
        y: ma.pressed ? 1 : 0

        // soft outer shadow / glow
        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.width: 1
            border.color: Qt.rgba(0, 0, 0, 0.4)
            z: -1
        }
    }

    Text {
        id: label
        anchors.centerIn: parent
        text: root.text
        color: "#04160B"
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
