import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

ScrollView {
    id: root
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
    // Same slim thumb as the other pages (this one had the stock grey bar).
    ScrollBar.vertical: ScrollBar {
        id: pageScrollBar
        policy: ScrollBar.AsNeeded
        width: 6
        contentItem: Rectangle {
            radius: 3
            color: pageScrollBar.pressed ? Theme.scrollbarThumbActive : Theme.scrollbarThumb
        }
        background: Rectangle { color: "transparent" }
    }

    ColumnLayout {
        width: root.availableWidth
        spacing: 14

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 68
            radius: Theme.radiusCard
            color: Theme.bgField
            border.color: Theme.borderSoft
            border.width: 1
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 12
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Text {
                        text: "DEBUG"
                        color: Theme.textPrimary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontDisplay
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.0
                    }
                    Text {
                        text: "Compact settings grid | live pipeline row | session log"
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmall
                    }
                }
                StatusPill {
                    label: "Core"
                    statusText: orion.coreActive ? "Fresh" : "Waiting"
                    tone: orion.coreActive ? "success" : "warning"
                }
            }
        }

        // ===== No-Meter (skele) mode ===============================================
        // The C++ side (orion.noMeterEnabled / orion.showSkeleton, both READ/WRITE)
        // and the skeleton canvas in RemotePlayPage.qml have existed since the pose
        // work landed — but no QML ever WROTE either property, so the whole mode was
        // unreachable from the UI and read as a missing feature. This card is that
        // missing control; nothing in C++ had to change to add it.
        //
        // Debug page deliberately: the shipped packager still excludes torch and its
        // models whitelist is empty, so this is an owner/dev test surface. When the
        // sidecar's capability probe says the pose stack is absent, switching the
        // mode on yields a bot that cannot fire a single shot — which is exactly the
        // silent failure the RemotePlayPage banner exists to shout about.
        Card {
            title: "No-Meter Mode (Skele)"
            subtitle: orion.noMeterAvailable
                      ? "Pose-timed release — no shot meter required"
                      : "Pose stack missing on this install — toggling on will not fire"
            Layout.fillWidth: true
            Layout.preferredHeight: 200

            ColumnLayout {
                anchors.fill: parent
                spacing: 10

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2
                        Text {
                            text: "No-Meter Mode"
                            color: Theme.textPrimary
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontBody
                            font.weight: Font.DemiBold
                        }
                        Text {
                            text: "Time the release from body pose instead of the shot meter"
                            color: Theme.textMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontCaption
                        }
                    }
                    DashboardToggle {
                        objectName: "noMeterEnableToggle"
                        checked: orion.noMeterEnabled
                        onToggled: function(next) { orion.noMeterEnabled = next }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2
                        Text {
                            text: "Show Skeleton"
                            color: Theme.textPrimary
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontBody
                            font.weight: Font.DemiBold
                        }
                        Text {
                            // The canvas in RemotePlayPage.qml requires BOTH gates plus a
                            // live stream, so name the dependency rather than letting the
                            // toggle look broken when it draws nothing on its own.
                            text: "Draw the tracked player over Live Capture (needs No-Meter ON)"
                            color: Theme.textMuted
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontCaption
                        }
                    }
                    DashboardToggle {
                        objectName: "showSkeletonToggle"
                        checked: orion.showSkeleton
                        onToggled: function(next) { orion.showSkeleton = next }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    StatusPill {
                        label: "Pose stack"
                        statusText: orion.noMeterAvailable ? "Ready" : "Missing"
                        tone: orion.noMeterAvailable ? "success" : "danger"
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: !orion.noMeterAvailable
                        text: orion.noMeterUnavailableReason
                        color: Theme.danger
                        font.family: Theme.fontMono
                        font.pixelSize: Theme.fontCaption
                        elide: Text.ElideRight
                    }
                }
            }
        }

        Card {
            title: "Security Gate"
            subtitle: "Release integrity, entitlement cache, and automation lock state"
            Layout.fillWidth: true
            // Header ~80 + pills 30 + text 15 + buttons 38 + two 10px gaps. The old
            // 150 was ~30px short, so the button row was being squeezed.
            Layout.preferredHeight: 184

            ColumnLayout {
                anchors.fill: parent
                spacing: 10
                GridLayout {
                    Layout.fillWidth: true
                    columns: 3
                    rowSpacing: 8
                    columnSpacing: 8
                    StatusPill { label: "Lock"; statusText: orion.securityLockActive ? "Locked" : "Open"; tone: orion.securityLockActive ? "danger" : "success"; Layout.fillWidth: true }
                    StatusPill { label: "Integrity"; statusText: orion.integrityState; tone: orion.securityLockActive ? "danger" : "neutral"; Layout.fillWidth: true }
                    StatusPill { label: "Entitlement"; statusText: orion.entitlementState; tone: orion.entitlementState.indexOf("valid") >= 0 ? "success" : "neutral"; Layout.fillWidth: true }
                }
                Text {
                    Layout.fillWidth: true
                    text: "Reason: " + orion.securityLockReason + "   |   Last audit: " + orion.lastSecurityAuditEvent
                    color: Theme.textMuted
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.fontCaption
                    elide: Text.ElideRight
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    PrimaryButton { text: "Verify Integrity"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.verifyReleaseIntegrityNow() }
                    PrimaryButton { text: "Clear Entitlement"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.clearInvalidLocalEntitlement() }
                    PrimaryButton { text: "Refresh Security"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.refreshSecurity() }
                }
            }
        }

        Card {
            title: "Settings Snapshot"
            subtitle: "Active native settings that map to the automation pipeline"
            Layout.fillWidth: true
            Layout.preferredHeight: 236

            TextArea {
                anchors.fill: parent
                text: orion.settingsSnapshot
                readOnly: true
                selectByMouse: true
                color: Theme.textSecondary
                selectedTextColor: Theme.textOnAccent
                selectionColor: Theme.accent
                font.family: Theme.fontMono
                font.pixelSize: Theme.fontSmall
                padding: 12
                wrapMode: TextEdit.NoWrap
                background: Rectangle {
                    color: Theme.bgField
                    radius: Theme.radiusControl
                    border.color: Theme.borderSoft
                    border.width: 1
                }
            }
        }

        Card {
            title: "Vision Pipeline"
            subtitle: "Live CV, meter, and output state"
            Layout.fillWidth: true
            Layout.preferredHeight: 168

            TextArea {
                anchors.fill: parent
                text: orion.visionPipeline
                readOnly: true
                selectByMouse: true
                color: Theme.textSecondary
                selectedTextColor: Theme.textOnAccent
                selectionColor: Theme.accent
                font.family: Theme.fontMono
                font.pixelSize: Theme.fontSmall
                padding: 12
                wrapMode: TextEdit.Wrap
                background: Rectangle {
                    color: Theme.bgField
                    radius: Theme.radiusControl
                    border.color: Theme.borderSoft
                    border.width: 1
                }
            }
        }

        Card {
            title: "Remote Play Instructions"
            subtitle: "Quick setup and capture checks"
            Layout.fillWidth: true
            Layout.preferredHeight: 220

            TextArea {
                anchors.fill: parent
                text: "One-time setup\n" +
                      "1. Install ViGEmBus, then plug the controller into the PC.\n" +
                      "2. Register the PS5 in Chiaki once using the console pairing flow.\n" +
                      "3. Save the PS5 console IP and Chiaki executable path in Remote Play.\n\n" +
                      "Start session\n" +
                      "1. Press Connect Chiaki.\n" +
                      "2. Select the configured console in Chiaki if needed.\n" +
                      "3. The stream should embed into Orion and update Live Capture.\n\n" +
                      "If capture is black\n" +
                      "Keep Orion visible, disconnect/reconnect once, and confirm Chiaki is on the stream window, not setup. Check the Session Log below for sidecar errors."
                readOnly: true
                selectByMouse: true
                color: Theme.textSecondary
                selectedTextColor: Theme.textOnAccent
                selectionColor: Theme.accent
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
                padding: 12
                wrapMode: TextEdit.Wrap
                background: Rectangle {
                    color: Theme.bgField
                    radius: Theme.radiusControl
                    border.color: Theme.borderSoft
                    border.width: 1
                }
            }
        }

        Card {
            title: "Timing Probes"
            // "with the ball" is load-bearing, not decoration: a probe presses Square and measures
            // the meter that follows, so with no ball in hand there is no meter, no rise samples,
            // and all 8 probes expire.
            // The ball requirement is load-bearing and non-obvious: a probe presses Square and
            // measures the meter that follows, so an empty-handed press produces nothing and the
            // probe expires. In venues where possession is not persistent the user must re-acquire
            // between shots, which is what the ~6s spacing is for.
            subtitle: "Keep the BALL in hand — 8 shots, ~6s apart; call for it between shots"
            Layout.fillWidth: true
            // Header ~80 + two mono lines (~30) + 10 + 38px button.
            Layout.preferredHeight: 160

            ColumnLayout {
                anchors.fill: parent
                spacing: 10

                Text {
                    Layout.fillWidth: true
                    // Two lines: actuation latency/jitter, then tick phase. Both say
                    // "NOT MEASURED" until a run lands, which is the state every machine
                    // has shipped in so far.
                    text: orion.latencyProbeSummary
                    color: Theme.textSecondary
                    font.family: Theme.fontMono
                    font.pixelSize: Theme.fontCaption
                    wrapMode: Text.Wrap
                }

                Item { Layout.fillHeight: true }

                PrimaryButton {
                    text: "Run Timing Probes"
                    Layout.fillWidth: true
                    Layout.preferredHeight: Theme.controlHeight
                    // Any physical Square press cancels the run, so a mistimed real shot
                    // aborts cleanly rather than fighting the probe presses.
                    onClicked: orion.runLatencyProbes()
                }
            }
        }


        RowLayout {
            Layout.fillWidth: true
            // 5 buttons × 38 px + 4 × 8 spacing = 222 px, plus ~80 for the card
            // header and margins. 320 leaves a small breathing item at the bottom.
            Layout.preferredHeight: 320
            spacing: 14

            LogViewer {
                // Both feeds: the raw engineering ring (unchanged — the Debug page
                // still shows everything under "All") and the customer Activity
                // feed, which the viewer opens on by default.
                text: orion.logText
                activityText: orion.activityText
                Layout.fillWidth: true
                Layout.fillHeight: true
            }

            Card {
                title: "Actions"
                subtitle: "Diagnostics only"
                Layout.preferredWidth: 300
                Layout.fillHeight: true

                ColumnLayout {
                    anchors.fill: parent
                    spacing: 8
                    PrimaryButton { text: "Check Chiaki"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.checkBackend() }
                    PrimaryButton { text: "Open Chiaki"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.openChiaki() }
                    PrimaryButton { text: "Refresh Security"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.refreshSecurity() }
                    PrimaryButton { text: "Sign Settings"; Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.signSettings() }
                    DangerButton  { text: "Clear Log";       Layout.fillWidth: true; Layout.preferredHeight: Theme.controlHeight; onClicked: orion.clearLogs() }
                    Item { Layout.fillHeight: true }
                }
            }
        }
    }
}
