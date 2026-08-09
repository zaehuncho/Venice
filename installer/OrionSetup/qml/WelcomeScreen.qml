import QtQuick

// Screen 1 — WELCOME.
Item {
    id: root
    signal install()
    signal cancel()

    Column {
        anchors.fill: parent
        anchors.leftMargin: 40
        anchors.rightMargin: 40
        anchors.topMargin: 34
        anchors.bottomMargin: 26
        spacing: 0

        // hero mark: tip triangle + wide-spaced ORION wordmark
        Row {
            spacing: 14
            bottomPadding: 22
            TipMark {
                anchors.verticalCenter: parent.verticalCenter
                width: 18; height: 16
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "VENICE"
                color: Theme.text
                font.family: Theme.sans
                font.pixelSize: 22
                font.letterSpacing: 11
            }
        }

        // eyebrow + inline trust shield (secure-installer cue near the title)
        Row {
            spacing: 7
            bottomPadding: 14
            ShieldMark {
                anchors.verticalCenter: parent.verticalCenter
                width: 11; height: 13
                stroke: Theme.green
                fill: Qt.rgba(51/255, 222/255, 118/255, 0.14)
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Installer · Secure"
                color: Theme.green
                font.family: Theme.mono
                font.pixelSize: 11
                font.letterSpacing: 2.4
                font.capitalization: Font.AllUppercase
            }
        }

        Text {
            width: parent.width
            text: "Perfect-green shot timing for NBA 2K."
            color: Theme.text
            font.family: Theme.sans
            font.pixelSize: 28
            font.weight: Font.DemiBold
            lineHeight: 1.12
            wrapMode: Text.WordWrap
            bottomPadding: 8
        }

        Text {
            width: Math.min(parent.width, 400)
            text: "This downloads Orion and the two controller drivers it needs — "
                  + "ViGEmBus and HidHide. About a minute on a normal connection."
            color: Theme.muted
            font.family: Theme.sans
            font.pixelSize: 15
            lineHeight: 1.5
            wrapMode: Text.WordWrap
        }

        Item { width: 1; height: 22 }   // flex spacer stand-in

        // requirement chips (pre-checked)
        Flow {
            width: parent.width
            spacing: 8
            bottomPadding: 20
            Chip { text: "Windows 11 / 10 · 64-bit" }
            Chip { text: "800 MB free" }
            Chip { text: "Administrator" }
        }

        // honest wired-ethernet note
        Row {
            width: Math.min(parent.width, 460)
            spacing: 9
            bottomPadding: 22
            // info glyph
            Item {
                width: 15; height: 15
                anchors.top: parent.top
                anchors.topMargin: 1
                Rectangle {
                    anchors.fill: parent
                    radius: width / 2
                    color: "transparent"
                    border.width: 1.4
                    border.color: Theme.dim
                }
                Text {
                    anchors.centerIn: parent
                    text: "i"
                    color: Theme.dim
                    font.family: Theme.sans
                    font.pixelSize: 9
                    font.weight: Font.Bold
                }
            }
            Text {
                width: parent.width - 24
                textFormat: Text.StyledText
                text: "<b style='color:#8CA398'>Wired Ethernet strongly recommended.</b> "
                      + "Orion reads your Remote Play feed and is exactly as fast as it — "
                      + "a clean wired link is clean greens."
                color: Theme.dim
                font.family: Theme.sans
                font.pixelSize: 13
                lineHeight: 1.45
                wrapMode: Text.WordWrap
            }
        }

        // actions
        Row {
            spacing: 12
            PrimaryButton {
                text: "Install Orion   ·   " + Math.round(installer.totalMb) + " MB"
                onClicked: root.install()
            }
            GhostButton {
                text: "Cancel"
                onClicked: root.cancel()
            }
        }
    }
}
