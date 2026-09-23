import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import OrionNative

// Minimal top bar: a single connectivity/update indicator on the right, plus a
// rare Safe Mode override on the left. License, security, and patch detail live
// on the General page; updates apply silently in the background (no prompt).
Rectangle {
    id: root
    radius: Theme.radiusCard
    color: Theme.bgSidebar
    border.color: Theme.borderSoft
    border.width: 1

    // Online/offline derives from backend reachability + license validity.
    readonly property bool serverOnline: orion.updateState.toLowerCase().indexOf("offline") < 0
                                         && orion.licenseState !== "Locked"

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 16
        anchors.rightMargin: 16
        spacing: 10

        // Safe mode: a repeated watchdog failure parked automation. Click to review/exit.
        StatusPill {
            visible: orion.safeModeActive
            statusText: "SAFE MODE"
            tone: "danger"
            animated: true
            MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                onClicked: safeModeDialog.open()
            }
        }

        Item { Layout.fillWidth: true }

        // Single status indicator: Online normally, Update when one is pending
        // (it applies itself), Offline if the backend/license is unreachable.
        // [CL2-P8-002 2026-09-23] "Reconnecting" while the licence lease blocks shots
        // (the RemotePlay banner carries the full sentence), instead of a green Online.
        // [CL3-F8-003 2026-09-23] NOTE: AppShell mounts this whole bar only while safe mode is
        // on, so this pill is visible only then. The lease state's customer surface is the Live
        // page's SHOTS PAUSED banner, not this pill.
        StatusPill {
            readonly property bool leaseBlocked: String(orion.leaseNotice || "").length > 0
            statusText: orion.updateAvailable ? "Update"
                        : leaseBlocked ? "Reconnecting"
                        : root.serverOnline ? "Online"
                        : "Offline"
            tone: orion.updateAvailable ? "warning"
                  : leaseBlocked ? "warning"
                  : root.serverOnline ? "success"
                  : "danger"
        }
    }

    Dialog {
        id: safeModeDialog
        modal: true
        anchors.centerIn: Overlay.overlay
        width: 420
        padding: 20
        title: "Safe mode"

        background: Rectangle {
            radius: 14
            color: Theme.modalSurface
            border.color: Theme.modalBorder
            border.width: 1
        }

        contentItem: ColumnLayout {
            spacing: 12

            Text {
                Layout.fillWidth: true
                wrapMode: Text.WordWrap
                color: Theme.textPrimary
                font.family: "Segoe UI Variable"
                font.pixelSize: 13
                // [CL3-F8-003 2026-09-23] Must promise the same thing as the "SAFE MODE:" Activity
                // line (OrionAppController::enterSafeMode): it auto-recovers on a steady stream.
                text: "Shots are paused because something kept failing. Venice usually turns them back on by itself once the stream has been steady for 30 seconds, or press Exit safe mode.\n\nReason: " + orion.safeModeReason
            }

            RowLayout {
                Layout.alignment: Qt.AlignRight
                spacing: 10

                PrimaryButton {
                    text: "Export diagnostics"
                    implicitHeight: 34
                    implicitWidth: 160
                    onClicked: orion.exportDiagnostics()
                }
                PrimaryButton {
                    text: "Exit safe mode"
                    implicitHeight: 34
                    implicitWidth: 140
                    onClicked: {
                        orion.exitSafeMode()
                        safeModeDialog.close()
                    }
                }
            }
        }
    }
}
