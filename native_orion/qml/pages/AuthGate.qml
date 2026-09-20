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
    // True once the user attempted a connection — gates the error card.
    property bool attempted: false

    // Map the raw backend message onto an actionable next step (onboarding.md §4).
    readonly property string errorHint: {
        var m = (orion.authMessage || "").toLowerCase()
        if (/machine|device|another|bound|hwid/.test(m))
            return "This account is linked to another PC. Run /hwid_reset in Discord, then connect this PC again. If that was not you, open a ticket."
        if (/expire/.test(m))
            return "Your trial or subscription has ended. Run /status in Discord or renew on the Venice website."
        if (/discord_signin|required/.test(m))
            return "Sign in with the Discord account that owns your trial or subscription, then use a fresh one-time code."
        if (/invalid|unknown|not found|format/.test(m))
            return "That one-time code is invalid, expired, or already used. Open Connect Discord again for a fresh code."
        if (/timed out|timeout|network|connect|offline|unreach|tls|certificate/.test(m))
            return "Couldn't reach the license server. Check your internet connection and try again in a moment."
        if (/rate limit/.test(m))
            return "Too many attempts in a row — wait a moment, then try the connection once."
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
            radius: cardWrap.radius
            color: Theme.shadowHalo
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

            // ---- One-time connection code ----
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 50
                radius: Theme.radiusControl
                color: Theme.bgField
                border.color: keyField.activeFocus ? Theme.focusRing : Theme.borderSoft
                border.width: keyField.activeFocus ? 2 : 1
                Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }

                TextField {
                    id: keyField
                    anchors.fill: parent
                    anchors.leftMargin: 14
                    anchors.rightMargin: pasteBtn.width + 20
                    verticalAlignment: TextInput.AlignVCenter
                    placeholderText: "One-time Discord connection code"
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

                // One-click paste for the short-lived code shown after Discord sign-in.
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
                        font.pixelSize: Theme.fontSmall
                        font.weight: Font.DemiBold
                        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                    }
                    background: Rectangle {
                        radius: Theme.radiusChip
                        color: pasteBtn.hovered ? Theme.bgCardHover : Theme.bgCard
                        border.color: pasteBtn.hovered ? Theme.borderStrong : Theme.borderSoft
                        border.width: 1
                        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                        Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
                    }
                }
            }

            // Deep-link confirmation: the code came from Discord sign-in.
            RowLayout {
                visible: root.deepLinkFilled
                Layout.fillWidth: true
                Layout.topMargin: -8
                spacing: 7
                Text { text: "✓"; color: Theme.success; font.family: Theme.fontUi; font.pixelSize: Theme.fontSmall; font.weight: Font.Bold }
                Text {
                    Layout.fillWidth: true
                    text: "Code filled from Discord sign-in — press Unlock."
                    color: Theme.success
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontSmall
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

            Button {
                Layout.fillWidth: true
                text: "Connect Discord"
                hoverEnabled: true
                onClicked: Qt.openUrlExternally("https://zaeorion.com/connect")
                contentItem: Text {
                    text: parent.text
                    color: parent.hovered ? Theme.textPrimary : Theme.accent
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontBody
                    font.weight: Font.DemiBold
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: parent.hovered ? Theme.bgCardHover : Theme.bgCard
                    border.color: Theme.borderSoft
                }
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
                        Text { text: "✕"; color: Theme.danger; font.family: Theme.fontUi; font.pixelSize: Theme.fontSmall; font.weight: Font.Bold }
                        Text {
                            Layout.fillWidth: true
                            text: "Activation failed"
                            color: Theme.danger
                            font.family: Theme.fontUi
                            font.pixelSize: Theme.fontBody
                            font.weight: Font.DemiBold
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: orion.authMessage
                        color: Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontSmall
                        lineHeight: 1.25
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        visible: root.errorHint.length > 0
                        text: root.errorHint
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Theme.fontCaption
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
                font.pixelSize: Theme.fontSmall
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                Behavior on color { ColorAnimation { duration: Theme.motionSlow } }
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
                    font.pixelSize: Theme.fontCaption
                }
                Item { Layout.fillWidth: true }
                Rectangle {
                    Layout.preferredWidth: 6
                    Layout.preferredHeight: 6
                    radius: 3
                    Layout.alignment: Qt.AlignVCenter
                    color: root.serverOnline ? Theme.success : Theme.danger
                    Behavior on color { ColorAnimation { duration: Theme.motionSlow } }
                }
                Text {
                    text: root.serverOnline ? "Online" : "Offline"
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontCaption
                }
            }
        }
    }
}
