import QtQuick
import QtQuick.Window

// Frameless dark installer window. Custom titlebar (drag + min/close), a stage
// that cross-fades between the four screens, and a stepper-dots footer.
Window {
    id: win
    width: 600
    height: 508
    minimumWidth: 600; maximumWidth: 600
    minimumHeight: 508; maximumHeight: 508
    visible: true
    color: "transparent"
    flags: Qt.Window | Qt.FramelessWindowHint
    title: "Orion Installer"

    // "welcome" | "progress" | "finish" | "error"
    property string current: "welcome"

    function stepFor(name) {
        if (name === "welcome") return 0;
        if (name === "finish")  return 2;
        return 1;                 // progress + error
    }

    // ── window frame (rounded, hairline, radial-tinted background) ──
    Rectangle {
        id: frame
        anchors.fill: parent
        radius: Theme.radius
        color: Theme.surface
        border.width: 1
        border.color: Theme.line
        clip: true

        // radial top glow + vertical surface gradient
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#0F1613" }
                GradientStop { position: 1.0; color: "#0B110E" }
            }
        }
        Rectangle {
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            width: parent.width * 1.2
            height: parent.height * 0.6
            radius: width / 2
            gradient: Gradient {
                GradientStop { position: 0.0; color: Qt.rgba(13/255,26/255,18/255,0.9) }
                GradientStop { position: 1.0; color: "transparent" }
            }
        }

        Column {
            anchors.fill: parent

            // ── titlebar ──
            Item {
                id: titlebar
                width: parent.width
                height: 48

                MouseArea {
                    anchors.fill: parent
                    onPressed: win.startSystemMove()
                }

                Rectangle {   // bottom hairline
                    anchors.bottom: parent.bottom
                    width: parent.width; height: 1
                    color: Theme.lineSoft
                }

                Row {
                    anchors.left: parent.left
                    anchors.leftMargin: 18
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 10
                    TipMark {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 12; height: 11
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "VENICE"
                        color: Theme.text
                        font.family: Theme.sans
                        font.pixelSize: 13
                        font.weight: Font.Medium
                        font.letterSpacing: 5.5
                    }
                }

                Row {
                    anchors.right: parent.right
                    anchors.rightMargin: 14
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 10
                    WinButton { glyph: "–"; onClicked: win.showMinimized() }
                    WinButton { glyph: "✕"; onClicked: win.close() }
                }
            }

            // ── stage ──
            Item {
                id: stage
                width: parent.width
                height: parent.height - titlebar.height - footer.height

                WelcomeScreen {
                    id: welcome
                    anchors.fill: parent
                    property bool active: win.current === "welcome"
                    opacity: active ? 1 : 0
                    visible: opacity > 0.01
                    enabled: active
                    y: active ? 0 : 6
                    Behavior on opacity { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    Behavior on y { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    onInstall: {
                        progress.resetStates();
                        progress.perfect = false;
                        progress.pct = 0; progress.mbNow = 0;
                        win.current = "progress";
                        installer.startInstall();
                    }
                    onCancel: win.close()
                }

                ProgressScreen {
                    id: progress
                    anchors.fill: parent
                    property bool active: win.current === "progress"
                    opacity: active ? 1 : 0
                    visible: opacity > 0.01
                    enabled: active
                    y: active ? 0 : 6
                    Behavior on opacity { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    Behavior on y { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                }

                FinishScreen {
                    id: finish
                    anchors.fill: parent
                    property bool active: win.current === "finish"
                    opacity: active ? 1 : 0
                    visible: opacity > 0.01
                    enabled: active
                    y: active ? 0 : 6
                    Behavior on opacity { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    Behavior on y { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    onLaunch: { installer.launchOrion(); win.close(); }
                    onSetupGuide: installer.openSetupGuide()
                    onDiscord: installer.openDiscord()
                }

                ErrorScreen {
                    id: error
                    anchors.fill: parent
                    property bool active: win.current === "error"
                    opacity: active ? 1 : 0
                    visible: opacity > 0.01
                    enabled: active
                    y: active ? 0 : 6
                    Behavior on opacity { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    Behavior on y { NumberAnimation { duration: 380; easing.type: Easing.InOutQuad } }
                    onRetry: { win.current = "progress"; installer.retry(); }
                    onGetHelp: installer.openSetupGuide()
                }
            }

            // ── footer: stepper dots + version meta ──
            Item {
                id: footer
                width: parent.width
                height: 44

                Rectangle {   // top hairline
                    anchors.top: parent.top
                    width: parent.width; height: 1
                    color: Theme.lineSoft
                }
                Rectangle {
                    anchors.fill: parent
                    color: Qt.rgba(0, 0, 0, 0.2)
                }

                Row {
                    anchors.left: parent.left
                    anchors.leftMargin: 18
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 7
                    Repeater {
                        model: 3
                        Rectangle {
                            property bool on: index <= win.stepFor(win.current)
                            width: on ? 18 : 6
                            height: 6
                            radius: on ? 3 : 3
                            color: on ? Theme.green : Theme.line
                            Behavior on width { NumberAnimation { duration: 300 } }
                        }
                    }
                }

                // Truthful signed-state: a shield + the real Authenticode result
                // of THIS OrionSetup.exe. Green shield + "Signed ✓ <signer>" only
                // when genuinely signed; a neutral outline + "Unsigned build"
                // otherwise. Never claims signed when it isn't.
                Row {
                    anchors.right: parent.right
                    anchors.rightMargin: 18
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6
                    ShieldMark {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 12; height: 14
                        stroke: installer.selfSigned ? Theme.green : Theme.dim
                        fill: installer.selfSigned
                              ? Qt.rgba(51/255, 222/255, 118/255, 0.12) : "transparent"
                        showCheck: installer.selfSigned
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: {
                            var v = "v" + installer.version + " · ";
                            if (installer.selfSigned)
                                return v + "Signed ✓" + (installer.signerName.length
                                       ? " " + installer.signerName : "");
                            return v + "Unsigned build";
                        }
                        color: installer.selfSigned ? Theme.muted : Theme.dim
                        font.family: Theme.mono
                        font.pixelSize: 11
                        font.letterSpacing: 0.4
                    }
                }
            }
        }
    }

    // ── backend wiring ──
    Connections {
        target: installer

        function onProgress(pct, mbNow, mbTotal, bytesPerSec, etaSec) {
            progress.pct = pct;
            progress.mbNow = mbNow;
            progress.speed = bytesPerSec;
            progress.etaSec = etaSec;
        }
        function onComponentState(index, state) { progress.setItemState(index, state); }
        function onComponentVerified(index)      { progress.setItemVerified(index); }
        function onPhase(text)      { progress.phaseText = text; }
        function onStatusLine(text) { progress.statusText = text; }
        function onSubLine(text)    { progress.subText = text; }

        function onFinished() {
            progress.pct = 100;
            progress.perfect = true;
            progress.subText = "Windows may ask permission for the drivers — click Yes.";
            // Carry the honest verification tally to the Finish screen.
            finish.allVerified = progress.allVerified();
            finishDelay.start();
        }
        function onFailed(message) {
            error.message = message;
            win.current = "error";
        }
    }

    Timer {
        id: finishDelay
        interval: Theme.reduceMotion ? 200 : 900
        onTriggered: { win.current = "finish"; finish.shown = true; }
    }
}
