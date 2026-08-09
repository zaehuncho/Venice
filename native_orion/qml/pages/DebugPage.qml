import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "../components"

ScrollView {
    id: root
    clip: true
    ScrollBar.horizontal.policy: ScrollBar.AlwaysOff

    ColumnLayout {
        width: root.availableWidth
        spacing: 14

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 68
            radius: 12
            color: "#05080D"
            border.color: "#263241"
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
                        color: "#F4F7FA"
                        font.family: "Segoe UI Variable"
                        font.pixelSize: 22
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Compact settings grid | live pipeline row | session log"
                        color: "#8A96A8"
                        font.family: "Segoe UI Variable"
                        font.pixelSize: 12
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
                            color: "#F4F7FA"
                            font.family: "Segoe UI Variable"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                        Text {
                            text: "Time the release from body pose instead of the shot meter"
                            color: "#8A96A8"
                            font.family: "Segoe UI Variable"
                            font.pixelSize: 11
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
                            color: "#F4F7FA"
                            font.family: "Segoe UI Variable"
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                        Text {
                            // The canvas in RemotePlayPage.qml requires BOTH gates plus a
                            // live stream, so name the dependency rather than letting the
                            // toggle look broken when it draws nothing on its own.
                            text: "Draw the tracked player over Live Capture (needs No-Meter ON)"
                            color: "#8A96A8"
                            font.family: "Segoe UI Variable"
                            font.pixelSize: 11
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
                        color: "#EF4444"
                        font.family: "Cascadia Mono"
                        font.pixelSize: 11
                        elide: Text.ElideRight
                    }
                }
            }
        }

        Card {
            title: "Security Gate"
            subtitle: "Release integrity, entitlement cache, and automation lock state"
            Layout.fillWidth: true
            Layout.preferredHeight: 150

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
                    color: "#8A96A8"
                    font.family: "Cascadia Mono"
                    font.pixelSize: 11
                    elide: Text.ElideRight
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    PrimaryButton { text: "Verify Integrity"; Layout.fillWidth: true; Layout.preferredHeight: 34; onClicked: orion.verifyReleaseIntegrityNow() }
                    PrimaryButton { text: "Clear Entitlement"; Layout.fillWidth: true; Layout.preferredHeight: 34; onClicked: orion.clearInvalidLocalEntitlement() }
                    PrimaryButton { text: "Refresh Security"; Layout.fillWidth: true; Layout.preferredHeight: 34; onClicked: orion.refreshSecurity() }
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
                color: "#D8DEE8"
                selectedTextColor: "#FFFFFF"
                selectionColor: "#4F8CFF"
                font.family: "Cascadia Mono"
                font.pixelSize: 12
                padding: 12
                wrapMode: TextEdit.NoWrap
                background: Rectangle {
                    color: "#05080D"
                    radius: 10
                    border.color: "#263241"
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
                color: "#D8DEE8"
                selectedTextColor: "#FFFFFF"
                selectionColor: "#4F8CFF"
                font.family: "Cascadia Mono"
                font.pixelSize: 12
                padding: 12
                wrapMode: TextEdit.Wrap
                background: Rectangle {
                    color: "#05080D"
                    radius: 10
                    border.color: "#263241"
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
                color: "#D8DEE8"
                selectedTextColor: "#FFFFFF"
                selectionColor: "#4F8CFF"
                font.family: "Segoe UI Variable"
                font.pixelSize: 12
                padding: 12
                wrapMode: TextEdit.Wrap
                background: Rectangle {
                    color: "#05080D"
                    radius: 10
                    border.color: "#263241"
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
            Layout.preferredHeight: 150

            ColumnLayout {
                anchors.fill: parent
                spacing: 10

                Text {
                    Layout.fillWidth: true
                    // Two lines: actuation latency/jitter, then tick phase. Both say
                    // "NOT MEASURED" until a run lands, which is the state every machine
                    // has shipped in so far.
                    text: orion.latencyProbeSummary
                    color: "#D8DEE8"
                    font.family: "Cascadia Mono"
                    font.pixelSize: 11
                    wrapMode: Text.Wrap
                }

                Item { Layout.fillHeight: true }

                PrimaryButton {
                    text: "Run Timing Probes"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 34
                    // Any physical Square press cancels the run, so a mistimed real shot
                    // aborts cleanly rather than fighting the probe presses.
                    onClicked: orion.runLatencyProbes()
                }
            }
        }

        // [VENICE_PROFILE 2026-08-08] Backup/restore of the tuned timing values as
        // venice-profile.json. All behaviour lives in VeniceProfileCard.qml + the
        // single controller slot (runVeniceProfileAction).
        VeniceProfileCard {
            Layout.fillWidth: true
            Layout.preferredHeight: 170
        }

        RowLayout {
            Layout.fillWidth: true
            // 5 buttons × 36 px + 4 × 8 spacing + 24 padding = ~232 px minimum,
            // plus ~50 for the card title/subtitle. Bumped to 320 so nothing
            // ever clips and there's a small breathing item at the bottom.
            Layout.preferredHeight: 320
            spacing: 14

            LogViewer {
                text: orion.logText
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
                    PrimaryButton { text: "Check Chiaki"; Layout.fillWidth: true; Layout.preferredHeight: 36; onClicked: orion.checkBackend() }
                    PrimaryButton { text: "Open Chiaki"; Layout.fillWidth: true; Layout.preferredHeight: 36; onClicked: orion.openChiaki() }
                    PrimaryButton { text: "Refresh Security"; Layout.fillWidth: true; Layout.preferredHeight: 36; onClicked: orion.refreshSecurity() }
                    PrimaryButton { text: "Sign Settings"; Layout.fillWidth: true; Layout.preferredHeight: 36; onClicked: orion.signSettings() }
                    DangerButton  { text: "Clear Log";       Layout.fillWidth: true; Layout.preferredHeight: 36; onClicked: orion.clearLogs() }
                    Item { Layout.fillHeight: true }
                }
            }
        }
    }
}
