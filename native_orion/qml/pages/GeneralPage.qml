import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

ScrollView {
    id: root
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

    readonly property bool streamLive: orion.remoteRunning
                                               || orion.chiakiEmbedStatus === "Embedded"
    readonly property bool setupReady: orion.streamSetupComplete === true
    readonly property bool accessReady: orion.licenseState !== "Locked"
                                                && orion.licenseState !== "Checking"
                                                && !orion.securityLockActive
    readonly property bool launchReady: root.accessReady && root.setupReady
    readonly property string videoPathLabel: orion.videoSource === "capture_card"
                                                     ? "Capture card"
                                                     : "Remote Play"

    component StatRow: RowLayout {
        id: statRow
        property string label: ""
        property string value: ""
        property color valueColor: Theme.textPrimary
        Layout.fillWidth: true
        spacing: 12

        Text {
            text: statRow.label
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 12
            Layout.preferredWidth: 116
        }
        Text {
            text: statRow.value
            color: statRow.valueColor
            font.family: Theme.fontUi
            font.pixelSize: 13
            font.weight: Font.DemiBold
            Layout.fillWidth: true
            horizontalAlignment: Text.AlignRight
            elide: Text.ElideRight
        }
    }

    component SummaryTile: Rectangle {
        id: tile
        property string label: ""
        property string value: ""
        property string detail: ""
        property string tone: "neutral"
        readonly property color toneColor: tone === "success" ? Theme.success
                                                   : tone === "warning" ? Theme.warning
                                                   : tone === "danger" ? Theme.danger
                                                   : Theme.textSecondary
        radius: Theme.radiusControl
        color: Theme.bgInset
        border.color: tone === "neutral" ? Theme.borderSoft : toneColor
        border.width: 1
        Layout.fillWidth: true
        Layout.preferredHeight: 108

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 14
            spacing: 5
            Text {
                text: tile.label.toUpperCase()
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 10
                font.weight: Font.Bold
                font.letterSpacing: 1.1
            }
            Text {
                text: tile.value
                color: tile.toneColor
                font.family: Theme.fontUi
                font.pixelSize: 17
                font.weight: Font.DemiBold
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
            Text {
                text: tile.detail
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
                Layout.fillWidth: true
                elide: Text.ElideRight
            }
        }
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: 14

        Card {
            title: "Overview"
            subtitle: "Account and launch readiness at a glance"
            Layout.fillWidth: true
            Layout.preferredHeight: 136

            RowLayout {
                anchors.fill: parent
                spacing: 14

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 4
                    Text {
                        text: !root.accessReady
                              ? "Access needs attention"
                              : !root.setupReady
                                ? "Finish setup before going Live"
                                : root.streamLive
                                  ? "Venice is Live"
                                  : "Ready for your next session"
                        color: Theme.textPrimary
                        font.family: Theme.fontUi
                        font.pixelSize: 20
                        font.weight: Font.DemiBold
                        Layout.fillWidth: true
                        elide: Text.ElideRight
                    }
                    Text {
                        text: !root.accessReady
                              ? "Resolve the license or security status shown below."
                              : !root.setupReady
                                ? "Confirm the console, video path, and stream policy once."
                                : "Configuration is ready. Open Live when you want Venice to take over the shot timing path."
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                    }
                }

                StatusPill {
                    statusText: !root.accessReady ? "LOCKED"
                                : root.streamLive ? "LIVE"
                                : root.launchReady ? "READY" : "SETUP"
                    tone: !root.accessReady ? "danger"
                          : root.streamLive ? "success"
                          : root.launchReady ? "success" : "warning"
                    animated: root.streamLive
                }

                PrimaryButton {
                    text: root.setupReady ? "Open Live" : "Open Setup"
                    Layout.preferredWidth: 116
                    Layout.preferredHeight: 38
                    enabled: root.accessReady
                    onClicked: orion.currentPage = root.setupReady ? "remotePlay" : "dashboard"
                }
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            SummaryTile {
                label: "Configuration"
                value: root.setupReady ? "Ready" : "Incomplete"
                detail: root.videoPathLabel + " · " + orion.remotePlayConsole
                tone: root.setupReady ? "success" : "warning"
            }
            SummaryTile {
                label: "Session"
                value: root.streamLive ? "Live" : "Standby"
                detail: String(orion.remoteState || "Disconnected")
                tone: root.streamLive ? "success" : "neutral"
            }
        }

        GridLayout {
            Layout.fillWidth: true
            columns: 2
            columnSpacing: 12
            rowSpacing: 12

            Card {
                title: "Account"
                subtitle: "Current Venice entitlement"
                Layout.fillWidth: true
                Layout.preferredHeight: 196

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10
                    StatRow {
                        label: "License"
                        value: orion.licenseState
                        valueColor: root.accessReady ? Theme.success : Theme.danger
                    }
                    StatRow {
                        label: "Time left"
                        value: String(orion.timeLeft || "Not reported")
                    }

                    RowLayout {
                        visible: orion.licenseKeyMasked.length > 0
                        Layout.fillWidth: true
                        spacing: 10
                        Text {
                            text: "License key"
                            color: Theme.textMuted
                            font.family: Theme.fontUi
                            font.pixelSize: 12
                            Layout.preferredWidth: 116
                        }
                        Text {
                            Layout.fillWidth: true
                            text: orion.licenseKeyMasked
                            color: Theme.textPrimary
                            font.family: Theme.fontMono
                            font.pixelSize: 12
                            horizontalAlignment: Text.AlignRight
                            elide: Text.ElideLeft
                        }
                        Button {
                            id: copyKeyButton
                            property bool copied: false
                            implicitHeight: 28
                            implicitWidth: 70
                            hoverEnabled: true
                            onClicked: {
                                orion.copyLicenseKey()
                                copied = true
                                copyReset.restart()
                            }
                            Timer {
                                id: copyReset
                                interval: 1600
                                onTriggered: copyKeyButton.copied = false
                            }
                            contentItem: Text {
                                text: copyKeyButton.copied ? "Copied" : "Copy"
                                color: copyKeyButton.copied ? Theme.success : Theme.textSecondary
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                                font.family: Theme.fontUi
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }
                            background: Rectangle {
                                radius: 7
                                color: copyKeyButton.hovered ? Theme.bgCardHover : Theme.bgField
                                border.color: copyKeyButton.copied
                                              ? Theme.successBorder : Theme.borderSoft
                                border.width: 1
                            }
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Card {
                title: "Application"
                subtitle: "Installed customer build"
                Layout.fillWidth: true
                Layout.preferredHeight: 196

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 10
                    StatRow { label: "Product"; value: Theme.productName }
                    StatRow { label: "Version"; value: orion.displayVersion }
                    StatRow { label: "Update channel"; value: orion.updateChannel }
                    StatRow { label: "Profile"; value: orion.activeProfile }
                    Item { Layout.fillHeight: true }
                }
            }
        }
    }
}
