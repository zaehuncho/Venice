import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Shared by the first-run gate and Setup page. Keep every control bound to the
// existing Orion backend contract; this surface only simplifies presentation.
//
// Three sections, in the order a new install is configured:
//   Console  ->  Video source  ->  Stream quality
// Every label uses SectionLabel, every explanation uses FieldHint, and every row
// control is Theme.controlHeight tall so mixed rows stay level.
ColumnLayout {
    id: form
    property bool showSetupHelp: false
    readonly property bool xboxMode: orion.remotePlayConsole === "Xbox"
    readonly property bool routeIdle: !orion.remoteRunning && orion.remoteState !== "Connecting"
    property var xboxWindows: []
    function refreshXboxWindows() { xboxWindows = orion.xboxRemotePlayWindows() }
    spacing: 12

    // Width of the inline label column on rows that carry one, so the controls
    // beside them line up down the form.
    readonly property int labelColumn: 82

    // [ORION_CAPTURE_FPS_60_ONLY 2026-09-14 owner] 120 Hz is NOT offered. The Elgato HD60 X
    // accepts a 1080p120 request and delivers 60; the sidecar then judged every shot's cadence
    // against 120 for the whole session, which is what the owner was living with ("i turned it
    // off and i went perfect from the field"). Any persisted or env-set value that is not 30
    // therefore READS as 60 here — including a 120 a future card legitimately supports, which
    // stays settable through settings.json (captureCardFps still snaps 120) but is not a choice
    // this picker can make by accident.
    function fpsLabel(fps) {
        return fps === 30 ? "30 Hz" : "60 Hz (recommended)"
    }

    component SectionLabel: Text {
        color: Theme.accentBorder
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontMicro
        font.weight: Font.Black
        font.letterSpacing: 1.4
        font.capitalization: Font.AllUppercase
        Layout.topMargin: 2
    }

    component FieldHint: Text {
        color: Theme.textMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontCaption
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
    }

    component RowLabel: Text {
        color: Theme.textMuted
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontSmall
        font.weight: Font.DemiBold
        Layout.preferredWidth: form.labelColumn
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
                text: form.xboxMode
                    ? "Choose the app window running your Xbox stream. Venice keeps your PS5 setup separate."
                    : "Your controller is set up automatically. Pick the console and the clean video feed Venice should watch."
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
                wrapMode: Text.WordWrap
            }
        }
    }

    // ---- Console ------------------------------------------------------------
    SectionLabel { text: "Console" }

    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        DashboardCombo {
            id: consoleCombo
            Layout.preferredWidth: 118
            // [COPY-FIX 2026-09-23 CW-14/EA-33] Xbox is untested: customer builds list PS5
            // only. Dev builds keep Xbox, and an install that already has Xbox selected keeps
            // the entry so the combo never shows a value missing from its list (switching to
            // PS5 then removes it).
            model: (orion.debugUiEnabled === true || orion.remotePlayConsole === "Xbox")
                   ? ["PS5", "Xbox"] : ["PS5"]
            enabled: form.routeIdle
            value: orion.remotePlayConsole
            onActivated: function(index) {
                orion.remotePlayConsole = textAt(index)
                value = Qt.binding(function() { return orion.remotePlayConsole })
            }
            // A user pick breaks the `value` binding, so the combo would go stale
            // if the console changed elsewhere (profile switch, first-run gate).
            Connections {
                target: orion
                function onSettingsChanged() { consoleCombo.value = orion.remotePlayConsole }
            }
        }

        TextField {
            id: consoleField
            visible: !form.xboxMode
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.controlHeight
            leftPadding: 12
            rightPadding: 12
            text: orion.consoleIp
            placeholderText: orion.remotePlayConsole === "PS5"
                             ? "Console IP (leave empty for discovery)"
                             : "Console IP"
            color: Theme.textPrimary
            placeholderTextColor: Theme.textFaint
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontBody
            selectByMouse: true
            onTextEdited: orion.consoleIp = text
            background: Rectangle {
                radius: Theme.radiusControl
                color: Theme.bgField
                border.color: consoleField.activeFocus ? Theme.focusRing : Theme.borderSoft
                border.width: 1
                Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
            }
        }

        PrimaryButton {
            text: "Detect"
            visible: !form.xboxMode
            Layout.preferredWidth: 78
            Layout.preferredHeight: Theme.controlHeight
            enabled: orion.remotePlayConsole === "PS5"
            onClicked: orion.detectPs5()
        }
    }

    FieldHint {
        text: orion.remotePlayConsole === "Xbox"
              ? "Xbox Remote Play — EXPERIMENTAL. No Xbox console has been measured yet, so shot timing on Xbox is untested and not supported. Start Remote Play in the Xbox Windows app, then select its window below."
              : "Discovery is usually enough. Enter a fixed console IP only when discovery is unavailable."
    }

    ColumnLayout {
        objectName: "xboxRemotePlaySection"
        visible: form.xboxMode
        Layout.fillWidth: true
        spacing: 8
        Component.onCompleted: if (form.xboxMode) form.refreshXboxWindows()
        onVisibleChanged: if (visible) form.refreshXboxWindows()
        SectionLabel { text: "Xbox Remote Play" }
        // [2026-09-21 beta] Untested-path acknowledgement. Persisted (orion.xboxUntestedAcknowledged);
        // the session refuses to start on Xbox until it is on (RemotePlaySession::start).
        RowLayout {
            objectName: "xboxUntestedAckRow"
            Layout.fillWidth: true
            spacing: 10
            Text {
                Layout.fillWidth: true
                text: "I understand Xbox support is experimental and untested: no console has been measured and shot timing is not supported on Xbox."
                color: Theme.textPrimary
                wrapMode: Text.WordWrap
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
            }
            DashboardToggle {
                objectName: "xboxUntestedAckToggle"
                checked: orion.xboxUntestedAcknowledged
                onToggled: orion.xboxUntestedAcknowledged = checked
            }
        }
        RowLayout {
            Layout.fillWidth: true
            PrimaryButton {
                text: "Open Xbox app"
                enabled: orion.xboxUntestedAcknowledged
                onClicked: orion.openXboxRemotePlay()
            }
            PrimaryButton {
                text: "Refresh windows"
                enabled: orion.xboxUntestedAcknowledged
                onClicked: form.refreshXboxWindows()
            }
        }
        DashboardCombo {
            id: xboxWindowPicker
            objectName: "xboxWindowPicker"
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.controlHeight
            enabled: form.routeIdle && orion.xboxUntestedAcknowledged && form.xboxWindows.length > 0
            model: form.xboxWindows
            value: orion.xboxRemotePlayWindowTitle || "Select the Remote Play window"
            onActivated: function(index) {
                orion.xboxRemotePlayWindowTitle = textAt(index)
                value = Qt.binding(function() {
                    return orion.xboxRemotePlayWindowTitle || "Select the Remote Play window"
                })
            }
        }
        FieldHint {
            // [COPY-FIX 2026-09-23 NEW-A9] Plain words, no pipeline jargon.
            text: "Venice watches the Xbox app window and sends input through a virtual controller. Keep the window visible at a fixed size. Disconnecting Venice leaves the Xbox app open."
        }
        FieldHint {
            text: "Make sure the Xbox app sees only Venice's virtual controller, not your physical pad as well. Xbox has its own Shot Lead; PS5 calibration is preserved. Xbox timing is untested: no accuracy is claimed, and support cannot troubleshoot Xbox timing yet."
        }
    }

    // ---- Video source -------------------------------------------------------
    SectionLabel { text: "Video source"; visible: !form.xboxMode }

    DashboardCombo {
        id: videoSourceCombo
        visible: !form.xboxMode
        enabled: orion.remoteState !== "Running" && orion.remoteState !== "Connecting"
                 && orion.remoteState !== "Disconnecting"
        Layout.fillWidth: true
        model: ["Remote Play stream", "Capture card (HDMI)"]
        value: orion.videoSource === "capture_card"
               ? "Capture card (HDMI)" : "Remote Play stream"
        onValueChanged: {
            var next = value === "Capture card (HDMI)" ? "capture_card" : "decoder"
            if (orion.videoSource !== next)
                orion.videoSource = next
        }
        Connections {
            target: orion
            function onSettingsChanged() {
                videoSourceCombo.value = orion.videoSource === "capture_card"
                                         ? "Capture card (HDMI)" : "Remote Play stream"
            }
        }
    }

    FieldHint {
        visible: !form.xboxMode && orion.videoSource !== "capture_card"
        text: "Remote Play carries video and the controller with no capture card required."
    }

    FieldHint {
        visible: !form.xboxMode && !videoSourceCombo.enabled
        text: "Disconnect before changing the video source or its Shot Lead."
    }

    ColumnLayout {
        Layout.fillWidth: true
        visible: !form.xboxMode && orion.videoSource === "capture_card"
        spacing: 8
        Component.onCompleted: orion.refreshCaptureDevices()
        onVisibleChanged: if (visible) orion.refreshCaptureDevices()

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            RowLabel { text: "Device" }

            ComboBox {
                id: ccPicker
                Layout.fillWidth: true
                Layout.preferredHeight: Theme.controlHeight
                enabled: orion.captureDeviceList.length > 0 && videoSourceCombo.enabled
                model: orion.captureDeviceList.length > 0
                       ? orion.captureDeviceList
                       : ["No capture device detected"]
                currentIndex: orion.captureCardSelected && orion.captureCardIndex >= 0
                              && orion.captureCardIndex < orion.captureDeviceList.length
                              ? orion.captureCardIndex : -1
                onActivated: {
                    if (orion.captureDeviceList.length > 0)
                        orion.captureCardIndex = currentIndex
                }
                contentItem: Text {
                    text: ccPicker.currentIndex < 0 ? "Choose a capture device" : ccPicker.displayText
                    color: Theme.textPrimary
                    verticalAlignment: Text.AlignVCenter
                    leftPadding: 12
                    rightPadding: 28
                    font.pixelSize: Theme.fontBody
                    font.family: Theme.fontUi
                    elide: Text.ElideRight
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: Theme.bgField
                    border.color: ccPicker.activeFocus ? Theme.focusRing : Theme.borderSoft
                    border.width: 1
                    Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
                }
            }

            PrimaryButton {
                text: "Refresh"
                Layout.preferredWidth: 84
                Layout.preferredHeight: Theme.controlHeight
                onClicked: orion.refreshCaptureDevices()
            }
        }

        // [CL2-P3-002 2026-09-23] Any card works if its feed measures steady; say what that
        // takes, and where the reason appears when it does not.
        FieldHint {
            text: "Pick your capture card (not a webcam). Set it to 1080p60 or 720p60 on a USB 3.0 port, turn HDCP off, and close other capture apps before you connect. If the feed is too slow or busy, the Activity feed says why."
        }

        // Capture refresh rate. The card is opened at this rate on the next
        // connect; a live session keeps the rate it started with.
        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            RowLabel { text: "Refresh rate" }

            DashboardCombo {
                id: captureFpsCombo
                objectName: "captureFpsCombo"
                Layout.preferredWidth: 200
                model: ["30 Hz", "60 Hz (recommended)"]
                value: form.fpsLabel(orion.captureCardFps)
                onValueChanged: {
                    var next = value === "30 Hz" ? 30 : 60
                    if (orion.captureCardFps !== next)
                        orion.captureCardFps = next
                }
                Connections {
                    target: orion
                    function onSettingsChanged() {
                        captureFpsCombo.value = form.fpsLabel(orion.captureCardFps)
                    }
                }
            }

            Item { Layout.fillWidth: true }
        }

        FieldHint {
            text: "60 Hz is the meter's native cadence. Only choose 30 Hz if your capture card cannot hold 60."
        }
    }

    // ---- Stream quality -----------------------------------------------------
    SectionLabel { text: "Stream quality"; visible: !form.xboxMode }

    GridLayout {
        visible: !form.xboxMode
        Layout.fillWidth: true
        columns: 2
        columnSpacing: 10
        rowSpacing: 8

        ColumnLayout {
            id: qualityGroup
            Layout.fillWidth: true
            spacing: 5
            readonly property var qualityModel: [
                { key: "Quality",      label: "Quality · 1080p60 · 12 Mbps" },
                { key: "Performance",  label: "Competitive · 720p60 · 12 Mbps" },
                { key: "Balanced",     label: "Balanced · 720p60 · 4 Mbps" },
                { key: "LowBandwidth", label: "Low · 540p60 · Manual preview" },
                { key: "UltraLow",     label: "Ultra-Low · 360p30 · Manual preview" }
            ]

            function labelForMode(mode) {
                for (var i = 0; i < qualityModel.length; ++i) {
                    if (qualityModel[i].key === mode)
                        return qualityModel[i].label
                }
                return qualityModel[1].label
            }

            Text {
                text: "Quality preset"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaption
            }
            DashboardCombo {
                id: qualityCombo
                Layout.fillWidth: true
                // Was `parent.qualityModel` — correct only because a ColumnLayout
                // child's parent IS the layout. Named explicitly so a wrapper item
                // can never silently turn these into undefined lookups.
                model: qualityGroup.qualityModel.map(function(item) { return item.label })
                value: qualityGroup.labelForMode(orion.streamBandwidthMode)
                onValueChanged: {
                    for (var i = 0; i < qualityGroup.qualityModel.length; ++i) {
                        if (qualityGroup.qualityModel[i].label === value
                                && orion.streamBandwidthMode !== qualityGroup.qualityModel[i].key) {
                            orion.streamBandwidthMode = qualityGroup.qualityModel[i].key
                            return
                        }
                    }
                }
                Connections {
                    target: orion
                    function onSettingsChanged() {
                        qualityCombo.value = qualityGroup.labelForMode(orion.streamBandwidthMode)
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
                font.pixelSize: Theme.fontCaption
            }
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: Theme.controlHeight
                radius: Theme.radiusControl
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
                        font.pixelSize: Theme.fontSmall
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
        visible: !form.xboxMode
    }

    RowLayout {
        visible: !form.xboxMode
        Layout.fillWidth: true
        Layout.topMargin: 2
        spacing: 10

        Switch {
            id: audioSwitch
            checked: orion.streamAudioEnabled
            enabled: !orion.audioToggleBusy
            onToggled: orion.streamAudioEnabled = checked
        }

        Text {
            text: orion.audioToggleBusy
                  ? "Applying…"
                  : orion.streamAudioEnabled ? "Audio on" : "Muted"
            color: orion.streamAudioEnabled ? Theme.success : Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontSmall
            Layout.preferredWidth: 76
        }

        ComboBox {
            id: audioModeCombo
            visible: orion.streamAudioEnabled
            Layout.fillWidth: true
            Layout.preferredHeight: Theme.controlHeight
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
                font.pixelSize: Theme.fontBody
                elide: Text.ElideRight
            }
            background: Rectangle {
                radius: Theme.radiusControl
                color: Theme.bgField
                border.color: audioModeCombo.activeFocus ? Theme.focusRing : Theme.borderSoft
                border.width: 1
                Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
            }
        }
    }

    Rectangle {
        visible: form.showSetupHelp && !form.xboxMode
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
            text: "Pairing this PC for the first time? Open the bundled Remote Play client, register the console, then come back to Venice and press Connect."
            color: Theme.textSecondary
            wrapMode: Text.Wrap
            lineHeight: 1.12
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontSmall
        }
        MouseArea {
            anchors.fill: parent
            cursorShape: Qt.PointingHandCursor
            onClicked: orion.openChiaki()
        }
    }
}
