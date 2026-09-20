import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

// Post-auth legal agreement + rules. Shown once (until orion.legalAccepted) before the
// stream-setup gate / app. The user must accept to continue; Decline quits.
Item {
    id: root

    VeniceBackdrop {
        anchors.fill: parent
        opacity: 0.95
    }

    Rectangle {
        id: cardWrap
        width: 560
        height: Math.min(parent.height - 72, 620)
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
            radius: cardWrap.radius
            color: Theme.shadowHalo
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
            spacing: 14

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    text: "Legal Agreement & Rules"
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontHeading
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: "Please read and accept before using Venice."
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmall
                    wrapMode: Text.WordWrap
                }
            }

            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }

            ScrollView {
                id: legalScroll
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical: ScrollBar {
                    id: legalScrollBar
                    policy: ScrollBar.AsNeeded
                    width: 6
                    contentItem: Rectangle { radius: 3; color: legalScrollBar.pressed ? Theme.scrollbarThumbActive : Theme.scrollbarThumb }
                    background: Rectangle { color: "transparent" }
                }

                ColumnLayout {
                    width: legalScroll.availableWidth
                    spacing: 12

                    Text {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontBody
                        lineHeight: 1.25
                        textFormat: Text.StyledText
                        text: "<b style='color:" + Theme.textPrimary + "'>Disclaimer</b><br>" +
                              "• Venice is provided \"as is\", without warranty of any kind. You use it entirely at your own risk.<br>" +
                              "• Venice is <b>not</b> affiliated with, endorsed by, or sponsored by Take-Two Interactive, 2K, Visual Concepts, Sony Interactive Entertainment, PlayStation, or the NBA. All trademarks belong to their respective owners.<br>" +
                              "• Using automation or third-party tools with online games may violate the game's and platform's Terms of Service and can result in warnings, suspension, or a permanent ban of your account. You accept this risk.<br>" +
                              "• The developers are not liable for any damages, account actions, hardware issues, or losses arising from use of this software."
                    }
                    Text {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontBody
                        lineHeight: 1.25
                        textFormat: Text.StyledText
                        text: "<b style='color:" + Theme.textPrimary + "'>Rules of Use</b><br>" +
                              "• For personal use only. Do not resell, redistribute, or share your license key.<br>" +
                              "• Do not use Venice to harass, defraud, or harm other players.<br>" +
                              "• You are solely responsible for complying with all applicable laws and the terms of any service you use it with.<br>" +
                              "• Do not reverse-engineer, tamper with, or attempt to bypass the licensing or security."
                    }
                    Text {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontCaption
                        lineHeight: 1.2
                        text: "By clicking \"I Agree\" you confirm you have read, understood, and accept this agreement and the rules above."
                    }
                }
            }

            // 46px matches the Unlock / Continue CTAs on the other gates.
            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                DangerButton {
                    text: "Decline & Exit"
                    implicitWidth: 130
                    implicitHeight: 46
                    onClicked: Qt.quit()
                }
                Item { Layout.fillWidth: true }
                PrimaryButton {
                    text: "I Agree"
                    implicitWidth: 160
                    implicitHeight: 46
                    onClicked: orion.acceptLegalAgreement()
                }
            }
        }
    }
}
