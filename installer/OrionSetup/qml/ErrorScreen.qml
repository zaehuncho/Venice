import QtQuick

// Screen 4 — ERROR (amber).
Item {
    id: root
    signal retry()
    signal getHelp()

    property string message:
        "The connection dropped while getting the detection engine. Your progress "
        + "is saved — retrying resumes where it stopped."

    Column {
        anchors.fill: parent
        anchors.leftMargin: 40
        anchors.rightMargin: 40
        anchors.topMargin: 34
        anchors.bottomMargin: 26
        spacing: 0

        // amber/red seal with "!"
        Item {
            width: 60; height: 60
            Rectangle {
                anchors.centerIn: parent
                width: 90; height: 90; radius: 45
                gradient: Gradient {
                    GradientStop { position: 0.0; color: Qt.rgba(255/255,96/255,96/255,0.18) }
                    GradientStop { position: 0.7; color: "transparent" }
                }
            }
            Rectangle {
                anchors.fill: parent
                radius: 30
                color: "transparent"
                border.width: 1.5
                border.color: Theme.red
            }
            Text {
                anchors.centerIn: parent
                text: "!"
                color: Theme.red
                font.family: Theme.sans
                font.pixelSize: 30
                font.weight: Font.Bold
            }
        }
        Item { width: 1; height: 20 }

        Text {
            text: "Download interrupted"
            color: Theme.amber
            font.family: Theme.mono
            font.pixelSize: 11
            font.letterSpacing: 2.4
            font.capitalization: Font.AllUppercase
            bottomPadding: 14
        }
        Text {
            text: "Couldn't finish the download."
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 28
            font.weight: Font.DemiBold
            bottomPadding: 8
        }
        Text {
            width: Math.min(parent.width, 440)
            text: root.message
            color: Theme.muted
            font.family: Theme.sans
            font.pixelSize: 15
            lineHeight: 1.5
            wrapMode: Text.WordWrap
        }

        Item { width: 1; height: 24 }

        Row {
            spacing: 12
            PrimaryButton { text: "Retry download"; onClicked: root.retry() }
            GhostButton { text: "Get help"; onClicked: root.getHelp() }
        }
    }
}
