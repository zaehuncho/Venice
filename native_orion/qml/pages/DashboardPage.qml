import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

Item {
    id: root

    readonly property string videoPathLabel: orion.videoSource === "capture_card"
                                             ? "Capture card"
                                             : "Remote Play"
    property var profiles: {
        try { return JSON.parse(orion.profilesSnapshot) } catch (e) { return ({ profiles: [] }) }
    }

    Loader {
        anchors.fill: parent
        sourceComponent: setupDash
    }

    // =====================================================================
    // SETUP -- concise production configuration on the existing meter key.
    // =====================================================================
    Component {
        id: setupDash

        ScrollView {
            id: setupScroll
            clip: true
            ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
            ScrollBar.vertical: ScrollBar {
                id: setupScrollBar
                policy: ScrollBar.AsNeeded
                width: 6
                contentItem: Rectangle {
                    radius: 3
                    color: setupScrollBar.pressed
                           ? Theme.scrollbarThumbActive : Theme.scrollbarThumb
                }
                background: Rectangle { color: "transparent" }
            }

            ColumnLayout {
                width: setupScroll.availableWidth
                spacing: 12

                Card {
                    title: "Production Setup"
                    subtitle: "The connection settings Venice uses for every session"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 108

                    RowLayout {
                        anchors.fill: parent
                        spacing: 10
                        StatusPill {
                            label: "Console"
                            statusText: orion.remotePlayConsole
                            tone: "neutral"
                            Layout.fillWidth: true
                        }
                        StatusPill {
                            label: "Video"
                            statusText: root.videoPathLabel
                            tone: "neutral"
                            Layout.fillWidth: true
                        }
                        StatusPill {
                            label: "Reconnect"
                            statusText: orion.autoReconnect ? "On" : "Off"
                            tone: orion.autoReconnect ? "success" : "neutral"
                            Layout.fillWidth: true
                        }
                        StatusPill {
                            objectName: "passiveTimingStatus"
                            label: "Timing"
                            statusText: orion.latencyCalibrationReady
                                        ? "Ready"
                                        : orion.remoteState === "Running"
                                          ? "Adapting" : "Route unavailable"
                            tone: orion.latencyCalibrationReady
                                  ? "success"
                                  : orion.remoteState === "Running"
                                    ? "accent" : "warning"
                            Layout.fillWidth: true
                        }
                    }
                }

                // [ORION_USER_LEAD] Shot Lead used to sit here, under the Timing status
                // pill. It moved to the LIVE page's side panel (2026-08-04, user:
                // "detection box and lead should be in the live tab under network")
                // because it is tuned against the game's own TIMING banner: the card
                // is useless anywhere the user cannot see the shot land, and asking
                // them to leave the stream to nudge it by 1 ms was the wrong loop.
                // The Timing status pill above stays here — it is readiness, not a
                // control. Since 2026-08-06 it is ALSO mirrored on the Live page
                // (objectName liveTimingStatus + the warming-up banner over the
                // capture): the cold-install audit found the benched fail-closed
                // state rendered nowhere the user actually looks, so a warming-up
                // bot read as a broken product. See RemotePlayPage.qml -> setupPanel.

                Card {
                    title: "Connection"
                    subtitle: "Console, video path, stream quality, and audio"
                    Layout.fillWidth: true
                    Layout.preferredHeight: streamForm.implicitHeight + 70

                    ColumnLayout {
                        anchors.fill: parent
                        StreamSetupForm {
                            id: streamForm
                            Layout.fillWidth: true
                        }
                    }
                }

                // =========================================================
                // APPEARANCE — purely cosmetic, and deliberately grouped away
                // from the timing settings above so nobody reads these as
                // things that affect how a shot is released. Nothing in the
                // card below is consumed by the detector, the scheduler, or the
                // input route.
                //
                // "Detection Box" used to be the first card in this group. It
                // moved to the LIVE page's side panel (2026-08-04, user:
                // "detection box and lead should be in the live tab under
                // network"): it is the one appearance setting with a live
                // preview available — the lock is being drawn a few hundred
                // pixels away — so tuning it from a page with no video was
                // guesswork. See RemotePlayPage.qml -> setupPanel. The lightbar
                // stays because its preview is a physical pad, not the screen.
                // =========================================================
                Card {
                    title: "Controller Lightbar"
                    subtitle: "Colour your DualSense while Venice runs"
                    Layout.fillWidth: true
                    Layout.preferredHeight: lightbarCardCol.implicitHeight + 70

                    ColumnLayout {
                        id: lightbarCardCol
                        anchors.fill: parent
                        spacing: 12

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12

                            Text {
                                text: "Enable"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                Layout.preferredWidth: 62
                            }
                            DashboardToggle {
                                objectName: "lightbarEnableToggle"
                                checked: orion.controllerLightbarEnabled
                                onToggled: function(next) { orion.controllerLightbarEnabled = next }
                            }
                            Text {
                                // The honest status string straight from native.
                                // It reports "waiting for controller", "requires
                                // a USB connection", or the colour it wrote —
                                // this card must never imply a write happened
                                // when the route was unavailable.
                                objectName: "lightbarStatusText"
                                Layout.fillWidth: true
                                text: orion.controllerLedStatus
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12
                            enabled: orion.controllerLightbarEnabled
                            opacity: enabled ? 1.0 : 0.45

                            Text {
                                text: "Colour"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                Layout.preferredWidth: 62
                            }
                            ColorSwatchRow {
                                objectName: "lightbarColorSwatches"
                                Layout.fillWidth: true
                                colors: ["#2563EB", "#CC44FF", "#22E0FF", "#3DFF7A",
                                         "#FFD023", "#FF4D6D", "#FFFFFF"]
                                value: orion.controllerLightbarPrimaryColor
                                onPicked: function(color) { orion.controllerLightbarPrimaryColor = color }
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12
                            enabled: orion.controllerLightbarEnabled
                            opacity: enabled ? 1.0 : 0.45

                            Text {
                                text: "Effect"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                Layout.preferredWidth: 62
                            }
                            DashboardCombo {
                                objectName: "lightbarModeCombo"
                                Layout.preferredWidth: 150
                                model: ["Solid", "Pulse", "Strobe", "Rainbow"]
                                value: orion.controllerLightbarMode
                                onValueChanged: {
                                    if (value !== orion.controllerLightbarMode)
                                        orion.controllerLightbarMode = value
                                }
                            }
                            Text {
                                visible: orion.controllerLightbarMode === "Pulse"
                                text: "Fades to"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 11
                            }
                            ColorSwatchRow {
                                objectName: "lightbarSecondarySwatches"
                                visible: orion.controllerLightbarMode === "Pulse"
                                swatchSize: 22
                                colors: ["#000000", "#4F8CFF", "#CC44FF", "#FFFFFF"]
                                value: orion.controllerLightbarSecondaryColor
                                onPicked: function(color) { orion.controllerLightbarSecondaryColor = color }
                            }
                            Item { Layout.fillWidth: true }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 12
                            enabled: orion.controllerLightbarEnabled
                            opacity: enabled ? 1.0 : 0.45

                            Text {
                                text: "Bright"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                Layout.preferredWidth: 62
                            }
                            ThemedSlider {
                                id: lightbarSlider
                                objectName: "lightbarBrightnessSlider"
                                Layout.fillWidth: true
                                from: 0.05
                                to: 1.0
                                value: orion.controllerLightbarBrightness
                                onMoved: if (!pressed) orion.controllerLightbarBrightness = value
                                onPressedChanged: if (!pressed) orion.controllerLightbarBrightness = value
                            }
                            Text {
                                text: Math.round((lightbarSlider.pressed ? lightbarSlider.value : orion.controllerLightbarBrightness) * 100) + "%"
                                color: Theme.textSecondary
                                font.family: Theme.fontMono
                                font.pixelSize: 11
                                Layout.preferredWidth: 38
                                horizontalAlignment: Text.AlignRight
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: "USB only — a Bluetooth pad cannot be driven from here. "
                                  + "Venice never writes to the pad while a shot is being "
                                  + "timed; a change made mid-shot lands the moment it ends."
                            color: Theme.textFaint
                            font.family: Theme.fontUi
                            font.pixelSize: 11
                            wrapMode: Text.WordWrap
                        }
                    }
                }

                Card {
                    title: "Profiles"
                    subtitle: "Save one trusted configuration per jumpshot"
                    Layout.fillWidth: true
                    Layout.preferredHeight: 158

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10
                            Text {
                                text: "Active"
                                color: Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                font.weight: Font.DemiBold
                                Layout.preferredWidth: 48
                            }
                            DashboardCombo {
                                Layout.fillWidth: true
                                model: {
                                    var names = []
                                    var list = root.profiles.profiles || []
                                    for (var i = 0; i < list.length; ++i)
                                        names.push(list[i].name)
                                    if (names.indexOf("Default") < 0)
                                        names.unshift("Default")
                                    return names
                                }
                                value: orion.activeProfile
                                onValueChanged: {
                                    if (value !== orion.activeProfile)
                                        orion.switchProfile(value)
                                }
                            }
                            DangerButton {
                                text: "Delete"
                                implicitWidth: 88
                                implicitHeight: 36
                                visible: orion.activeProfile !== "Default"
                                onClicked: orion.deleteProfile(orion.activeProfile)
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10
                            TextField {
                                id: profileNameField
                                Layout.fillWidth: true
                                placeholderText: "New profile name"
                                color: Theme.textPrimary
                                placeholderTextColor: Theme.textFaint
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                selectByMouse: true
                                background: Rectangle {
                                    radius: 10
                                    color: Theme.bgField
                                    border.color: profileNameField.activeFocus
                                                  ? Theme.focusRing : Theme.borderSoft
                                    border.width: 1
                                }
                            }
                            PrimaryButton {
                                text: "Save profile"
                                implicitWidth: 118
                                implicitHeight: 36
                                enabled: profileNameField.text.trim().length > 0
                                onClicked: {
                                    orion.saveProfile(profileNameField.text.trim(), "")
                                    profileNameField.text = ""
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
