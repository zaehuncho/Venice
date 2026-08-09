import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

// Post-auth first-run screen: configure the stream once, then Continue into the app.
// Shown only while orion.streamSetupComplete is false (see Main.qml). Reuses the shared
// StreamSetupForm so the fields match the Dashboard Stream Setup tab exactly.
Item {
    id: root

    VeniceBackdrop {
        anchors.fill: parent
        opacity: 0.95
    }

    Rectangle {
        id: cardWrap
        width: 480
        // Fixed-cap height (NOT derived from cardCol.implicitHeight): cardCol holds a
        // Layout.fillHeight ScrollView, so a content-derived height here would form a
        // layout feedback loop (thrash → stall → crash). Parent-derived only.
        height: Math.min(parent.height - 80, 640)
        anchors.centerIn: parent
        radius: Theme.radiusCard
        color: Theme.modalSurface
        border.color: Theme.modalBorder
        border.width: 1

        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.color: Theme.accentSoft
            border.width: 1
        }
        Rectangle {
            z: -1
            anchors.fill: parent
            anchors.topMargin: 3
            radius: parent.radius
            color: "#06090E"
            opacity: 0.5
        }

        opacity: 0
        scale: 0.96
        Component.onCompleted: entrance.start()
        ParallelAnimation {
            id: entrance
            NumberAnimation { target: cardWrap; property: "opacity"; from: 0; to: 1; duration: 420; easing.type: Easing.OutCubic }
            NumberAnimation { target: cardWrap; property: "scale"; from: 0.96; to: 1; duration: 520; easing.type: Easing.OutBack; easing.overshoot: 0.6 }
        }

        ColumnLayout {
            id: cardCol
            x: 28
            y: 24
            width: parent.width - 56
            height: parent.height - 48
            spacing: 16

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    text: "Stream setup"
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: 20
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: "Set up your console connection once. You can change this later on the Dashboard."
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
            }

            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }

            ScrollView {
                id: formScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical: ScrollBar {
                    policy: ScrollBar.AsNeeded
                    width: 6
                    contentItem: Rectangle { radius: 3; color: parent.pressed ? Theme.scrollbarThumbActive : Theme.scrollbarThumb }
                    background: Rectangle { color: "transparent" }
                }

                StreamSetupForm {
                    width: formScroll.availableWidth
                    showSetupHelp: true
                }
            }

            PrimaryButton {
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                text: "Continue to Venice"
                onClicked: orion.markStreamSetupComplete()
            }
        }
    }
}
