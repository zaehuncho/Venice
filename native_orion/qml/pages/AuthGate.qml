import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative
import "../components"

// Venice license gate: static navy depth, customer mark, and one clear action.
Item {
    id: root

    readonly property string serverText: orion.serverState || ""
    readonly property bool serverOnline: {
        var s = serverText.toLowerCase()
        return s.length > 0 && !/offline|unreach|error|down|fail|wait/.test(s)
    }
    readonly property bool locked: orion.licenseState === "Locked"
    readonly property bool ready: keyField.text.trim().length > 0 && !orion.authBusy
    // True while the field holds a key delivered by the orion://activate deep link
    // (cleared the moment the user edits the field or unlock starts).
    property bool deepLinkFilled: false
    // True once the user attempted an unlock — gates the error card so the idle
    // "Enter a license key" prompt keeps rendering as plain muted text.
    property bool attempted: false

    // Map the raw backend message onto an actionable next step (onboarding.md §4).
    readonly property string errorHint: {
        var m = (orion.authMessage || "").toLowerCase()
        if (/machine|device|another|bound|hwid/.test(m))
            return "That's the machine lock doing its job. Moving to your own new PC? Run /hwid_reset in the Discord (self-service, once per 24h). Never activated it yourself? Open a ticket — your key may have leaked."
        if (/expire/.test(m))
            return "Your license period has ended. Grab a new tier from the store, or check #pricing in the Discord."
        if (/invalid|unknown|not found|format/.test(m))
            return "Copy the key straight from your Venice bot DM (click the spoiler to reveal, then copy — don't retype it). Bought but no DM? Run /redeem in the Discord with your order email."
        if (/timed out|timeout|network|connect|offline|unreach|tls|certificate/.test(m))
            return "Couldn't reach the license server. Check your internet connection and try again in a moment."
        if (/rate limit/.test(m))
            return "Too many attempts in a row — wait a moment, then press Unlock once."
        return ""
    }

    function tryUnlock() {
        if (orion.authBusy)
            return
        var k = keyField.text
        if (k && k.trim().length > 0) {
            root.attempted = true
            root.deepLinkFilled = false
            orion.authenticate(k.trim())
        }
    }

    // Zero-typing activation: a key arriving over orion://activate?key=... (at
    // launch or forwarded from a second instance) pre-fills the field. Never
    // auto-submits — the user reviews and clicks Unlock.
    function consumePendingKey() {
        if (orion.pendingActivationKey.length === 0)
            return
        keyField.text = orion.pendingActivationKey
        root.deepLinkFilled = true
        orion.clearPendingActivationKey()
    }
    Component.onCompleted: consumePendingKey()
    Connections {
        target: orion
        function onPendingActivationKeyChanged() { root.consumePendingKey() }
    }

    // Animated starfield — twinkling/flickering stars (no comets, per request).
    VeniceBackdrop {
        anchors.fill: parent
        opacity: 0.95
    }

    // Auth card.
    Rectangle {
        id: cardWrap
        width: 460
        height: cardCol.implicitHeight + 56
        anchors.centerIn: parent
        radius: Theme.radiusCard
        color: Theme.modalSurface
        border.color: Theme.modalBorder
        border.width: 1

        Rectangle {
            anchors.fill: parent
            anchors.margins: -1
            radius: parent.radius + 1
            color: "transparent"
            border.color: Theme.accentSoft
            border.width: 1
        }
        Rectangle {
            z: -1
            anchors.fill: parent
            anchors.topMargin: 3
            radius: parent.radius
            color: "#06090E"
            opacity: 0.5
        }

        opacity: 0
        scale: 0.96
        Component.onCompleted: entrance.start()
        ParallelAnimation {
            id: entrance
            NumberAnimation { target: cardWrap; property: "opacity"; from: 0; to: 1; duration: 420; easing.type: Easing.OutCubic }
            NumberAnimation { target: cardWrap; property: "scale"; from: 0.96; to: 1; duration: 520; easing.type: Easing.OutBack; easing.overshoot: 0.6 }
        }

        ColumnLayout {
            id: cardCol
            x: 28
            y: 28
            width: parent.width - 56
            spacing: 18

            // ---- Brand: logo + wordmark ----
            // Borderless + transparent (no disc/tile) so the star's own glow blends
            // into the card + starfield. The asset's bright core sits high-right of its
            // bounding box (long tail to the lower-left), so it's nudged down-left to
            // sit optically centered under the wordmark.
            ColumnLayout {
                Layout.fillWidth: true
                Layout.bottomMargin: 4
                spacing: 6

                // Full-width row so the mark centres on the card's axis (fillWidth +
                // centerIn is reliable here where Layout.alignment was not).
                Item {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 72

                    Image {
                        id: brandLogo
                        anchors.centerIn: parent
                        anchors.horizontalCenterOffset: -3
                        anchors.verticalCenterOffset: 7
                        source: orion.iconSource
                        width: 72; height: 72
                        sourceSize.width: 192
                        sourceSize.height: 192
                        fillMode: Image.PreserveAspectFit
                        smooth: true
                        mipmap: true
                        visible: status === Image.Ready
                        transformOrigin: Item.Center
                        // Slow "breathing" scale so the mark feels alive, not pasted.
                    }
                    // Fallback glyph if the icon asset is ever missing.
                    Text {
                        anchors.centerIn: parent
                        text: "V"
                        visible: brandLogo.status !== Image.Ready
                        color: Theme.accent
                        font.pixelSize: 44
                    }
                }

                Text {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: Theme.productMark
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: 22
                    font.weight: Font.DemiBold
                    font.letterSpacing: 3
                }
            }

            // ---- Key field ----
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 50
                radius: Theme.radiusControl
                color: Theme.bgField
                border.color: keyField.activeFocus ? Theme.focusRing : Theme.borderSoft
                border.width: keyField.activeFocus ? 2 : 1
                Behavior on border.color { ColorAnimation { duration: 160 } }

                TextField {
                    id: keyField
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: pasteBtn.width + 20
                    verticalAlignment: TextInput.AlignVCenter
                    placeholderText: "Enter license key"
                    color: Theme.textPrimary
                    placeholderTextColor: Theme.textFaint
                    font.family: Theme.fontUi
                    font.pixelSize: 15
                    font.letterSpacing: 0.5
                    selectByMouse: true
                    background: null
                    Keys.onReturnPressed: root.tryUnlock()
                    Component.onCompleted: forceActiveFocus()
                    onTextEdited: root.deepLinkFilled = false
                }

                // One-click paste — the key arrives via a Discord DM, so paste is
                // the whole activation flow (typos in 16+ char keys are the enemy).
                Button {
                    id: pasteBtn
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    width: 62
                    height: 32
                    hoverEnabled: true
                    onClicked: {
                        keyField.selectAll()
                        keyField.paste()
                        keyField.forceActiveFocus()
                        root.deepLinkFilled = false
                    }
                    contentItem: Text {
                        text: "Paste"
                        color: pasteBtn.hovered ? Theme.textPrimary : Theme.textSecondary
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                    }
                    background: Rectangle {
                        radius: 7
                        color: pasteBtn.hovered ? Theme.bgCardHover : Theme.bgCard
                        border.color: pasteBtn.hovered ? Theme.borderStrong : Theme.borderSoft
                        border.width: 1
                        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                    }
                }
            }

            // Deep-link confirmation: the key came from the activation link, ready to go.
            RowLayout {
                visible: root.deepLinkFilled
                Layout.fillWidth: true
                Layout.topMargin: -8
                spacing: 7
                Text { text: "✓"; color: Theme.success; font.pixelSize: 12; font.weight: Font.Bold }
                Text {
                    Layout.fillWidth: true
                    text: "Key filled from your activation link — press Unlock."
                    color: Theme.success
                    font.family: Theme.fontUi
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
            }

            // ---- Unlock ----
            PrimaryButton {
                Layout.fillWidth: true
                Layout.preferredHeight: 46
                text: orion.authBusy ? "Verifying license…" : "Unlock"
                enabled: root.ready
                onClicked: root.tryUnlock()
            }

            // ---- Status / error surface ----
            // Failed attempts render as a structured error card (what happened +
            // the exact next step); idle/busy states stay a quiet single line.
            readonly property bool showErrorCard: root.attempted && root.locked && !orion.authBusy
                                                  && orion.authMessage.length > 0

            Rectangle {
                visible: cardCol.showErrorCard
                Layout.fillWidth: true
                Layout.preferredHeight: errorCol.implicitHeight + 24
                radius: Theme.radiusControl
                color: Theme.dangerDim
                border.color: Theme.dangerBorder
                border.width: 1

                ColumnLayout {
                    id: errorCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.leftMargin: 14
                    anchors.rightMargin: 14
                    spacing: 5

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text { text: "✕"; color: Theme.danger; font.pixelSize: 12; font.weight: Font.Bold }
                        Text {
                            Layout.fillWidth: true
                            text: "Activation failed"
                            color: Theme.danger
                            font.family: Theme.fontUi
                            font.pixelSize: 13
                            font.weight: Font.DemiBold
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: orion.authMessage
                        color: Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                        lineHeight: 1.25
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: root.errorHint.length > 0
                        text: root.errorHint
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 11
                        lineHeight: 1.3
                        wrapMode: Text.WordWrap
                    }
                }
            }

            Text {
                Layout.fillWidth: true
                text: orion.authMessage
                visible: text.length > 0 && !cardCol.showErrorCard
                color: orion.authBusy ? Theme.textSecondary : Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 12
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                Behavior on color { ColorAnimation { duration: 200 } }
            }

            // ---- Footer: version + inline server status (no bubble) ----
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 2
                spacing: 7
                Text {
                    text: Theme.productName + " " + orion.displayVersion
                    color: Theme.textFaint
                    font.family: Theme.fontUi
                    font.pixelSize: 11
                }
                Item { Layout.fillWidth: true }
                Rectangle {
                    width: 6; height: 6; radius: 3
                    Layout.alignment: Qt.AlignVCenter
                    color: root.serverOnline ? Theme.success : Theme.danger
                }
                Text {
                    text: root.serverOnline ? "Online" : "Offline"
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: 11
                }
            }
        }
    }
}
