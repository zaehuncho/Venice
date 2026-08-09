import QtQuick

// Screen 3 — FINISH.
Item {
    id: root
    signal launch()
    signal setupGuide()
    signal discord()

    property bool shown: false
    property bool allVerified: false   // set from the progress tally at finish

    Column {
        anchors.fill: parent
        anchors.leftMargin: 40
        anchors.rightMargin: 40
        anchors.topMargin: 34
        anchors.bottomMargin: 26
        spacing: 0

        // perfect-green seal / check
        Item {
            id: seal
            width: 60; height: 60
            scale: root.shown ? 1 : 0.4
            opacity: root.shown ? 1 : 0
            Behavior on scale {
                enabled: !Theme.reduceMotion
                NumberAnimation { duration: 500; easing.type: Easing.OutBack }
            }
            Behavior on opacity { NumberAnimation { duration: 300 } }

            Rectangle {   // glow disc
                anchors.centerIn: parent
                width: 90; height: 90; radius: 45
                gradient: Gradient {
                    GradientStop { position: 0.0; color: Qt.rgba(147/255,255/255,173/255,0.25) }
                    GradientStop { position: 0.7; color: "transparent" }
                }
            }
            Rectangle {
                anchors.fill: parent
                radius: 30
                color: "transparent"
                border.width: 1.5
                border.color: Theme.perfect
            }
            CheckMark {
                anchors.centerIn: parent
                width: 30; height: 30
                stroke: Theme.perfect
                weight: 2.4
            }
        }
        Item { width: 1; height: 20 }

        Text {
            text: "Installed"
            color: Theme.green
            font.family: Theme.mono
            font.pixelSize: 11
            font.letterSpacing: 2.4
            font.capitalization: Font.AllUppercase
            bottomPadding: 14
        }
        Text {
            text: "You're in."
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 28
            font.weight: Font.DemiBold
            bottomPadding: 8
        }
        Text {
            width: Math.min(parent.width, 420)
            text: "Orion is ready. Your first 10–15 shots in Practice calibrate it to "
                  + "your jumper — that's expected, not a miss."
            color: Theme.muted
            font.family: Theme.sans
            font.pixelSize: 15
            lineHeight: 1.5
            wrapMode: Text.WordWrap
        }

        Item { width: 1; height: 14 }

        Column {
            spacing: 9
            Repeater {
                model: [
                    "Launch, connect Remote Play, and hold your shot button.",
                    "The first-run check confirms your capture & controller."
                ]
                Row {
                    spacing: 10
                    CheckMark {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 15; height: 15
                        stroke: Theme.green
                        weight: 2.4
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: modelData
                        color: Theme.muted
                        font.family: Theme.sans
                        font.pixelSize: 14
                    }
                }
            }
        }

        Item { width: 1; height: 18 }

        // honest security line — reflects the real verification + signed state
        Row {
            spacing: 8
            ShieldMark {
                anchors.verticalCenter: parent.verticalCenter
                width: 14; height: 16
                stroke: root.allVerified ? Theme.green : Theme.dim
                fill: root.allVerified
                      ? Qt.rgba(51/255, 222/255, 118/255, 0.14) : "transparent"
                showCheck: root.allVerified
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: {
                    var files = root.allVerified
                        ? "Every file verified" : "Signature-checked drivers";
                    var sig = installer.selfSigned
                        ? "signed installer" : "dev build (unsigned)";
                    return files + " · " + sig;
                }
                color: root.allVerified ? Theme.muted : Theme.dim
                font.family: Theme.mono
                font.pixelSize: 12
                font.letterSpacing: 0.3
            }
        }

        Item { width: 1; height: 18 }

        Row {
            spacing: 12
            PrimaryButton { text: "Launch Orion"; onClicked: root.launch() }
            GhostButton { text: "Setup guide"; onClicked: root.setupGuide() }
            GhostButton { text: "Join Discord"; onClicked: root.discord() }
        }
    }
}
