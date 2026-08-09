import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

// Silent startup update gate. Mounted by Main.qml whenever orion.updateGatePhase
// is not "clear" — BEFORE AuthGate. No prompts, no countdown: if an update can be
// applied it installs immediately; otherwise it fails soft and proceeds.
//   "checking" -> minimal splash while /api/update resolves (fail-soft, watchdogged)
//   "offer"/"force" -> apply now (canApply), or proceed on the current version
Item {
    id: root

    readonly property string phase: orion.updateGatePhase
    readonly property bool checking: phase === "checking"
    readonly property bool forced: phase === "force"
    // A dev (build-tree) launch never auto-applies; an absent updater can't apply.
    readonly property bool canApply: orion.updaterPresent && !orion.devBuild
    // Forced + no updater is the only state that can't proceed (must reinstall).
    readonly property bool stuck: !canApply && forced && !orion.devBuild

    // One-shot: decide and act shortly after the card renders.
    Timer {
        interval: 600
        repeat: false
        running: !root.checking
        onTriggered: {
            if (root.canApply)
                orion.startUpdate()
            else if (orion.devBuild || !root.forced)
                orion.continueWithoutUpdate()
            // else: forced + missing updater -> stay on the reinstall notice
        }
    }

    VeniceBackdrop {
        anchors.fill: parent
        opacity: 0.95
    }

    // ---- Checking splash -------------------------------------------------
    ColumnLayout {
        anchors.centerIn: parent
        spacing: 18
        visible: root.checking

        Item {
            id: checkSpinner
            Layout.alignment: Qt.AlignHCenter
            width: 34; height: 34
            Repeater {
                model: 8
                Rectangle {
                    width: 5; height: 5; radius: 2.5
                    color: "#4F8CFF"
                    opacity: 0.18 + 0.82 * (index / 8)
                    x: checkSpinner.width / 2 - 2.5 + (checkSpinner.width / 2 - 3) * Math.cos(index * Math.PI / 4)
                    y: checkSpinner.height / 2 - 2.5 + (checkSpinner.height / 2 - 3) * Math.sin(index * Math.PI / 4)
                }
            }
            RotationAnimator on rotation {
                from: 0; to: 360; duration: 900; loops: Animation.Infinite; running: root.checking
            }
        }
        Text {
            Layout.alignment: Qt.AlignHCenter
            text: "Checking for updates…"
            color: "#9FB0C3"
            font.family: "Segoe UI Variable"
            font.pixelSize: 14
        }
    }

    // ---- Update card -----------------------------------------------------
    Rectangle {
        id: cardWrap
        width: 540
        height: cardCol.implicitHeight + 56
        anchors.centerIn: parent
        radius: 18
        color: Theme.modalSurface
        border.color: Theme.modalBorder
        border.width: 1
        visible: !root.checking

        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.color: Qt.rgba(79 / 255, 140 / 255, 255 / 255, 0.18)
            border.width: 1
        }

        opacity: 0
        scale: 0.95
        Component.onCompleted: if (visible) entrance.start()
        onVisibleChanged: if (visible) entrance.start()
        ParallelAnimation {
            id: entrance
            NumberAnimation { target: cardWrap; property: "opacity"; from: 0; to: 1; duration: 420; easing.type: Easing.OutCubic }
            NumberAnimation { target: cardWrap; property: "scale"; from: 0.95; to: 1; duration: 520; easing.type: Easing.OutBack; easing.overshoot: 0.7 }
        }

        ColumnLayout {
            id: cardCol
            x: 28
            y: 28
            width: parent.width - 56
            spacing: 16

            // ---- Brand ----
            RowLayout {
                Layout.fillWidth: true
                spacing: 14

                Rectangle {
                    width: 54
                    height: 54
                    radius: 16
                    color: "#0B0F14"
                    border.color: "#4F8CFF"
                    border.width: 1
                    Image {
                        id: logoImg
                        anchors.centerIn: parent
                        source: orion.iconSource
                        width: 34
                        height: 34
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        visible: status === Image.Ready
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "V"
                        color: "#F4F7FA"
                        font.family: "Segoe UI Variable"
                        font.pixelSize: 24
                        font.weight: Font.DemiBold
                        visible: logoImg.status !== Image.Ready
                    }
                }

                ColumnLayout {
                    spacing: 1
                    Text {
                        text: root.stuck ? "Update required" : "Updating Venice"
                        color: "#F4F7FA"
                        font.family: "Segoe UI Variable"
                        font.pixelSize: 22
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: orion.latestVersion.length > 0
                              ? "Venice " + orion.appVersion + "  →  " + orion.latestVersion
                              : "Venice " + orion.appVersion
                        color: "#8A96A8"
                        font.family: "Cascadia Mono"
                        font.pixelSize: 12
                    }
                }

                Item { Layout.fillWidth: true }

                Rectangle {
                    Layout.alignment: Qt.AlignTop
                    radius: 9
                    implicitHeight: 26
                    implicitWidth: channelText.implicitWidth + 20
                    color: "#13202F"
                    border.color: "#2A3A52"
                    border.width: 1
                    Text {
                        id: channelText
                        anchors.centerIn: parent
                        text: orion.updateChannel.toUpperCase()
                        color: "#9AA8BF"
                        font.family: "Segoe UI Variable"
                        font.pixelSize: 11
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.0
                    }
                }
            }

            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: "#1B2738" }

            // ---- Reinstall notice (only when stuck: forced + no updater) ----
            Rectangle {
                Layout.fillWidth: true
                visible: root.stuck
                radius: 10
                implicitHeight: noticeText.implicitHeight + 20
                color: "#2A2010"
                border.color: "#6B521F"
                border.width: 1
                Text {
                    id: noticeText
                    anchors.fill: parent
                    anchors.margins: 10
                    text: "The required Venice update helper is missing, so this update can't be applied. Reinstall Venice from the latest package."
                    color: "#E8C872"
                    font.family: "Segoe UI Variable"
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
            }

            // ---- Release notes ----
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 6
                visible: orion.updateNotes.length > 0 && !root.stuck

                Text {
                    text: "What's new"
                    color: "#8A96A8"
                    font.family: "Segoe UI Variable"
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.0
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: Math.min(notesText.implicitHeight + 24, 170)
                    radius: 10
                    color: "#080C12"
                    border.color: "#1B2738"
                    border.width: 1
                    Flickable {
                        anchors.fill: parent
                        anchors.margins: 12
                        contentHeight: notesText.implicitHeight
                        clip: true
                        Text {
                            id: notesText
                            width: parent.width
                            text: orion.updateNotes
                            color: "#C3CDDB"
                            font.family: "Segoe UI Variable"
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                        }
                    }
                }
            }

            // ---- Status line + indeterminate progress ----
            Text {
                Layout.fillWidth: true
                text: root.stuck ? "Please reinstall to continue."
                      : root.canApply ? "Installing the latest version — Venice will restart automatically."
                      : "Continuing on the current version…"
                color: "#8A96A8"
                font.family: "Segoe UI Variable"
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 6
                radius: 3
                color: "#16202C"
                clip: true
                visible: !root.stuck
                Rectangle {
                    id: shimmer
                    width: parent.width * 0.34
                    height: parent.height
                    radius: 3
                    color: "#4F8CFF"
                    x: -width
                    SequentialAnimation on x {
                        loops: Animation.Infinite
                        running: cardWrap.visible && !root.stuck
                        NumberAnimation { from: -shimmer.width; to: cardWrap.width; duration: 1100; easing.type: Easing.InOutQuad }
                    }
                }
            }

            // ---- Footer ----
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 2
                Text {
                    text: orion.updateState
                    color: "#566273"
                    font.family: "Segoe UI Variable"
                    font.pixelSize: 11
                }
                Item { Layout.fillWidth: true }
                Text {
                    text: "Signed updates · Ed25519 verified"
                    color: "#566273"
                    font.family: "Segoe UI Variable"
                    font.pixelSize: 11
                }
            }
        }
    }
}
