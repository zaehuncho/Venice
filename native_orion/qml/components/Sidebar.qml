import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

Rectangle {
    id: root
    color: Theme.bgSidebar
    radius: Theme.radiusCard
    border.color: Theme.borderSoft
    border.width: 1

    // Raised when the "Setup Guide" entry is clicked — it opens the first-run coach-
    // mark tour overlay (FirstRunTour) rather than navigating to a page. AppShell
    // owns the tour and connects to this.
    signal tourRequested()

    readonly property string sessionState: String(orion.remoteState || "Disconnected")
    readonly property string sessionLabel: sessionState === "Running" ? "Live"
                                           : sessionState === "Connecting" ? "Connecting"
                                           : sessionState === "Disconnecting" ? "Closing"
                                           : sessionState === "Error" ? "Attention"
                                           : "Offline"
    readonly property string sessionTone: sessionState === "Running" ? "success"
                                          : sessionState === "Connecting"
                                            || sessionState === "Disconnecting" ? "warning"
                                          : sessionState === "Error" ? "danger"
                                          : "neutral"
    // Debug stays hidden in production builds. Quick Start is a footer action;
    // "Setup Guide" entry carries action:"tour" so it launches the guided tour
    // overlay instead of switching pages (it is not a tab you land on) — and it
    // only exists until first-run setup finishes (orion.preflightComplete flips
    // via markPreflightComplete(), NOTIFYing settingsChanged so the list rebuilds).
    readonly property var pages: {
        var list = [
            { key: "remotePlay", label: "Live", icon: "play" },
            { key: "general", label: "Overview", icon: "home" },
            { key: "dashboard", label: "Setup", icon: "grid" },
            { key: "patchNotes", label: "Updates", icon: "notes" }
        ]
        if (orion.debugUiEnabled)
            list.push({ key: "debug", label: "Debug", icon: "gear" })
        return list
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 16

        RowLayout {
            spacing: 12
            Layout.fillWidth: true
            Layout.preferredHeight: 54

            // Logo mark in a rounded tile so it reads clearly at any icon size,
            // with a crisp "O" fallback if the icon asset fails to load.
            Rectangle {
                width: 44
                height: 44
                radius: 12
                // Borderless + faint so the star + its glow blend into the sidebar
                // panel instead of reading as a bordered badge.
                color: Theme.iconTileBg
                border.color: Theme.borderSoft
                border.width: 1

                // Faint accent glow behind the mark.
                Rectangle {
                    anchors.fill: parent
                    radius: parent.radius
                    color: Theme.accentFaint
                }

                Image {
                    id: brandIcon
                    anchors.centerIn: parent
                    source: orion.iconSource
                    width: 30
                    height: 30
                    sourceSize.width: 64
                    sourceSize.height: 64
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                    visible: status === Image.Ready
                }
                Text {
                    anchors.centerIn: parent
                    text: "V"
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: 22
                    font.weight: Font.Bold
                    visible: brandIcon.status !== Image.Ready
                }
            }
            ColumnLayout {
                spacing: 1
                Text {
                    text: Theme.productMark
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: 19
                    font.weight: Font.Bold
                    font.letterSpacing: 2.5
                }
                Text {
                    text: Theme.productTagline
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: 10
                    font.letterSpacing: 0.5
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            height: 1
            color: Theme.hairline
        }

        Repeater {
            model: root.pages
            delegate: Button {
                id: nav
                required property var modelData
                readonly property bool isAction: modelData.action === "tour"
                // The tour launcher never becomes the "current page", so it never
                // shows the active accent — it reads as an action, not a tab.
                readonly property bool active: !isAction && orion.currentPage === modelData.key
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                text: modelData.label
                hoverEnabled: true
                onClicked: {
                    if (nav.isAction)
                        root.tourRequested()
                    else
                        orion.currentPage = modelData.key
                }

                // Anchor for the first-run tour spotlight (e.g. "nav:remotePlay").
                Component.onCompleted: TourRegistry.register("nav:" + modelData.key, nav)
                Component.onDestruction: TourRegistry.unregister("nav:" + modelData.key, nav)

                contentItem: RowLayout {
                    spacing: 10

                    // Active-page accent indicator.
                    Rectangle {
                        Layout.preferredWidth: 3
                        Layout.preferredHeight: 18
                        Layout.leftMargin: 2
                        radius: 1.5
                        color: nav.active ? Theme.accent : "transparent"
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }
                    NavIcon {
                        icon: nav.modelData.icon
                        color: nav.active ? Theme.accentBorder : Theme.textFaint
                        width: 18
                        height: 18
                        Layout.preferredWidth: 18
                        Layout.preferredHeight: 18
                        Layout.alignment: Qt.AlignVCenter
                    }
                    Text {
                        text: nav.text
                        color: nav.active ? Theme.textPrimary : nav.hovered ? Theme.textSecondary : Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontBody
                        font.weight: nav.active ? Font.DemiBold : Font.Normal
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    }
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: nav.active ? Theme.accentSoft : nav.hovered ? Theme.bgCard : "transparent"
                    border.color: nav.active ? Theme.borderStrong : "transparent"
                    border.width: 1
                    Behavior on color { ColorAnimation { duration: Theme.motionBase } }
                    Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
                }
            }
        }

        Item { Layout.fillHeight: true }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 8

            Button {
                id: quickStart
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                text: "Quick Start"
                hoverEnabled: true
                onClicked: root.tourRequested()
                contentItem: RowLayout {
                    spacing: 9
                    NavIcon {
                        icon: "guide"
                        color: quickStart.hovered ? Theme.accentBorder : Theme.textFaint
                        width: 16
                        height: 16
                        Layout.preferredWidth: 16
                        Layout.preferredHeight: 16
                    }
                    Text {
                        Layout.fillWidth: true
                        text: quickStart.text
                        color: quickStart.hovered ? Theme.textSecondary : Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                    }
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: quickStart.hovered ? Theme.bgCard : "transparent"
                    border.color: Theme.borderSoft
                    border.width: 1
                }
            }

            StatusPill {
                Layout.fillWidth: true
                label: "Session"
                statusText: root.sessionLabel
                tone: root.sessionTone
            }

        }
    }
}
