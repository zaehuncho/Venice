import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Shared by the first-run gate and Setup page. Keep every control bound to the
// existing Orion backend contract; this surface only simplifies presentation.
ColumnLayout {
    id: form
    property bool showSetupHelp: false
    spacing: 12

    component SectionLabel: Text {
        color: Theme.accentBorder
        font.family: Theme.fontUi
        font.pixelSize: 10
        font.weight: Font.Black
        font.letterSpacing: 1.4
        font.capitalization: Font.AllUppercase
    }

    component FieldHint: Text {
        color: Theme.textMuted
        font.family: Theme.fontUi
        font.pixelSize: 11
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: routeSummary.implicitHeight + 20
        radius: Theme.radiusControl
        color: Theme.bgInset
        border.color: Theme.borderSoft
        border.width: 1

        RowLayout {
            id: routeSummary
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: 10
            spacing: 10

            Rectangle {
                Layout.preferredWidth: 8
                Layout.preferredHeight: 8
                radius: 4
                color: Theme.success
            }
            Text {
                Layout.fillWidth: true
                text: "Controller routing is automatic. Choose the console and clean video source Venice should use."
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: 12
                wrapMode: Text.WordWrap
            }
        }
    }

    SectionLabel { text: "Console connection" }

    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        DashboardCombo {
            Layout.preferredWidth: 118
            model: ["PS5", "Xbox"]
            value: orion.remotePlayConsole
            onValueChanged: {
                if (orion.remotePlayConsole !== value)
                    orion.remotePlayConsole = value
            }
        }

        TextField {
            id: consoleField
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            text: orion.consoleIp
            placeholderText: orion.remotePlayConsole === "PS5"
                             ? "Console IP (leave empty for discovery)"
                             : "Console IP"
            color: Theme.textPrimary
            placeholderTextColor: Theme.textFaint
            selectByMouse: true
            onTextEdited: orion.consoleIp = text
            background: Rectangle {
                radius: 10
                color: Theme.bgField
                border.color: consoleField.activeFocus ? Theme.focusRing : Theme.borderSoft
                border.width: 1
            }
        }

        PrimaryButton {
            text: "Detect"
            Layout.preferredWidth: 78
            Layout.preferredHeight: 38
            enabled: orion.remotePlayConsole === "PS5"
            onClicked: orion.detectPs5()
        }
    }

    FieldHint {
        text: orion.remotePlayConsole === "Xbox"
              ? "Xbox uses the official Xbox app with the virtual controller and window capture."
              : "Discovery is usually enough. Enter a fixed console IP only when discovery is unavailable."
    }

    SectionLabel { text: "Detection video" }

    DashboardCombo {
        Layout.fillWidth: true
        model: ["Remote Play stream", "Capture card (HDMI)"]
        value: orion.videoSource === "capture_card"
               ? "Capture card (HDMI)" : "Remote Play stream"
        onValueChanged: {
            orion.videoSource = value === "Capture card (HDMI)" ? "capture_card" : "decoder"
        }
    }

    FieldHint {
        visible: orion.videoSource !== "capture_card"
        text: "Remote Play provides video and controller transport with no capture card required."
    }

    ColumnLayout {
        Layout.fillWidth: true
        visible: orion.videoSource === "capture_card"
        spacing: 7
        Component.onCompleted: orion.refreshCaptureDevices()
        onVisibleChanged: if (visible) orion.refreshCaptureDevices()

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            ComboBox {
                id: ccPicker
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                enabled: orion.captureDeviceList.length > 0
                model: orion.captureDeviceList.length > 0
                       ? orion.captureDeviceList
                       : ["No capture device detected"]
                currentIndex: orion.captureCardIndex >= 0
                              && orion.captureCardIndex < orion.captureDeviceList.length
                              ? orion.captureCardIndex : 0
                onActivated: {
                    if (orion.captureDeviceList.length > 0)
                        orion.captureCardIndex = currentIndex
                }
                contentItem: Text {
                    text: ccPicker.displayText
                    color: Theme.textPrimary
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 12
                    rightPadding: 28
                    font.pixelSize: 13
                    font.family: Theme.fontUi
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: 10
                    color: Theme.bgField
                    border.color: ccPicker.activeFocus ? Theme.focusRing : Theme.borderSoft
                    border.width: 1
                }
            }

            PrimaryButton {
                text: "Refresh"
                Layout.preferredWidth: 84
                Layout.preferredHeight: 38
                onClicked: orion.refreshCaptureDevices()
            }
        }

        FieldHint {
            text: "Use a clean HDMI feed, disable HDCP, and close other capture applications before starting Live."
        }
    }

    SectionLabel { text: "Stream policy" }

    GridLayout {
        Layout.fillWidth: true
        columns: 2
        columnSpacing: 10
        rowSpacing: 8

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            readonly property var qualityModel: [
                { key: "Quality",      label: "Quality · 1080p60 · 12 Mbps" },
                { key: "Performance",  label: "Competitive · 720p60 · 12 Mbps" },
                { key: "Balanced",     label: "Balanced · 720p60 · 4 Mbps" },
                { key: "LowBandwidth", label: "Low · 540p60 · Manual preview" },
                { key: "UltraLow",     label: "Ultra-Low · 360p30 · Manual preview" }
            ]

            Text {
                text: "Quality preset"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
            }
            DashboardCombo {
                id: qualityCombo
                Layout.fillWidth: true
                model: parent.qualityModel.map(function(item) { return item.label })
                value: {
                    for (var i = 0; i < parent.qualityModel.length; ++i) {
                        if (parent.qualityModel[i].key === orion.streamBandwidthMode)
                            return parent.qualityModel[i].label
                    }
                    return parent.qualityModel[1].label
                }
                onValueChanged: {
                    for (var i = 0; i < parent.qualityModel.length; ++i) {
                        if (parent.qualityModel[i].label === value
                                && orion.streamBandwidthMode !== parent.qualityModel[i].key) {
                            orion.streamBandwidthMode = parent.qualityModel[i].key
                            return
                        }
                    }
                }
            }
        }

        ColumnLayout {
            Layout.fillWidth: true
            spacing: 5
            Text {
                text: "Recovery"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                radius: 10
                color: Theme.bgField
                border.color: Theme.borderSoft
                border.width: 1

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 8
                    Text {
                        text: "Auto-reconnect"
                        color: Theme.textPrimary
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                        Layout.fillWidth: true
                    }
                    DashboardToggle {
                        checked: orion.autoReconnect
                        onToggled: orion.autoReconnect = checked
                    }
                }
            }
        }
    }

    FieldHint {
        text: "Competitive, Balanced, and Quality keep the 720p minimum required for shot automation."
    }

    SectionLabel { text: "Audio" }

    RowLayout {
        Layout.fillWidth: true
        spacing: 10

        Switch {
            id: audioSwitch
            checked: orion.streamAudioEnabled
            enabled: !orion.audioToggleBusy
            onToggled: orion.streamAudioEnabled = checked
        }

        Text {
            text: orion.audioToggleBusy
                  ? "Applying..."
                  : orion.streamAudioEnabled ? "Audio on" : "Muted"
            color: orion.streamAudioEnabled ? Theme.success : Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 12
            Layout.preferredWidth: 76
        }

        ComboBox {
            id: audioModeCombo
            visible: orion.streamAudioEnabled
            Layout.fillWidth: true
            Layout.preferredHeight: 38
            enabled: !orion.audioToggleBusy
            model: [
                { key: "Off",        label: "Off" },
                { key: "Standard",   label: "Standard latency" },
                { key: "Stabilized", label: "Stabilized for crackle" }
            ]
            textRole: "label"
            valueRole: "key"
            currentIndex: {
                switch (orion.streamAudioMode) {
                case "Off": return 0
                case "Stabilized": return 2
                default: return 1
                }
            }
            onActivated: orion.streamAudioMode = model[currentIndex].key
            contentItem: Text {
                text: audioModeCombo.displayText
                color: Theme.textPrimary
                verticalAlignment: Text.AlignVCenter
                leftPadding: 12
                rightPadding: 28
                font.family: Theme.fontUi
                font.pixelSize: 13
                elide: Text.ElideRight
            }
            background: Rectangle {
                radius: 10
                color: Theme.bgField
                border.color: audioModeCombo.activeFocus ? Theme.focusRing : Theme.borderSoft
                border.width: 1
            }
        }
    }

    Rectangle {
        visible: form.showSetupHelp
        Layout.fillWidth: true
        Layout.preferredHeight: firstRunText.implicitHeight + 24
        radius: Theme.radiusControl
        color: Theme.accentFaint
        border.color: Theme.borderStrong
        border.width: 1

        Text {
            id: firstRunText
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: 12
            text: "Pairing this PC for the first time? Open the bundled Remote Play client, register the console, then return to Venice and press Enable Bot + Controller."
            color: Theme.textSecondary
            wrapMode: Text.Wrap
            lineHeight: 1.12
            font.family: Theme.fontUi
            font.pixelSize: 12
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: orion.openChiaki()
        }
    }
}
