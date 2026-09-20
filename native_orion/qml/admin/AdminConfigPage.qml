import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Config — GET/POST /api/admin/config (contract §5). Owner only. Each card
// sends only its own keys plus the reason, so two operators editing different
// cards never clobber each other.
Item {
    id: page

    property bool loaded: false
    property bool seeded: false

    readonly property var cfg: admin.config
    readonly property var motd: cfg && cfg.motd ? cfg.motd : ({})
    readonly property var policy: cfg && cfg.reset_policy_defaults ? cfg.reset_policy_defaults : ({})
    readonly property var fraud: cfg && cfg.fraud_thresholds ? cfg.fraud_thresholds : ({})
    readonly property var alerts: cfg && cfg.alerts ? cfg.alerts : ({})

    function str(obj, key, fallback) {
        if (!obj || obj[key] === undefined || obj[key] === null)
            return fallback === undefined ? "" : fallback
        return String(obj[key])
    }

    function num(text, fallback) {
        var parsed = parseInt(text, 10)
        return isNaN(parsed) ? fallback : parsed
    }

    function listText(value) {
        if (!value || value.length === undefined)
            return ""
        var out = []
        for (var i = 0; i < value.length; ++i)
            out.push(String(value[i]))
        return out.join(", ")
    }

    function parseList(text) {
        var out = []
        var parts = String(text).split(",")
        for (var i = 0; i < parts.length; ++i) {
            var trimmed = parts[i].trim()
            if (trimmed.length > 0)
                out.push(trimmed)
        }
        return out
    }

    function seedAll() {
        minVersion.text = str(cfg, "min_client_version")
        blockedVersions.text = listText(cfg ? cfg.blocked_versions : null)
        motdText.text = str(motd, "text")
        motdUntil.text = str(motd, "until")
        policyFree.text = str(policy, "free_resets", "3")
        policyPenalty.text = str(policy, "penalty_days", "3")
        policyCooldown.text = str(policy, "cooldown_s", "86400")
        fraudMachines.text = str(fraud, "machines_30d", "3")
        fraudResets.text = str(fraud, "resets_30d", "4")
        alertOwner.text = str(alerts, "owner_discord_user_id")
        alertEvents.text = listText(alerts ? alerts.events : null)
        ipAllowlist.text = listText(cfg ? cfg.owner_ip_allowlist : null)
        seeded = true
    }

    function reload() {
        admin.refreshConfig()
        loaded = true
    }

    onVisibleChanged: {
        if (visible && !loaded && admin.ownerRoutes)
            reload()
    }

    Connections {
        target: admin
        function onDataChanged() {
            // Seed the editors from the first config read; after that the
            // operator's typing wins until they hit "Reload fields".
            if (!page.seeded && page.cfg && Object.keys(page.cfg).length > 0)
                page.seedAll()
        }
    }

    ScrollView {
        id: scroll
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        ColumnLayout {
            width: scroll.availableWidth
            spacing: 10

            RowLayout {
                Layout.fillWidth: true
                spacing: 10
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 2
                    Text {
                        text: "Config"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontDisplay
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Kill switch, version gate, MOTD, reset policy, fraud, alerts and owner hardening."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                    }
                }
                AdminButton {
                    kind: "ghost"
                    text: "Reload fields"
                    enabled: !admin.busy && admin.ownerRoutes
                    onClicked: {
                        page.seeded = false
                        page.reload()
                    }
                }
            }

            Text {
                Layout.fillWidth: true
                visible: !admin.ownerRoutes
                text: "Config changes require owner credentials."
                color: Theme.warning
                font.pixelSize: Theme.fontSmall
            }

            // ---- kill switch ---------------------------------------------
            AdminCard {
                Layout.fillWidth: true
                title: "Global kill switch"
                subtitle: "Disables activation, trials and heartbeats service-wide. Owner-only (the bot route is read-only)."

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    AdminPill {
                        label: admin.killSwitchEngaged ? "ENGAGED" : "released"
                        tone: admin.killSwitchEngaged ? Theme.danger : Theme.success
                    }
                    Text {
                        Layout.fillWidth: true
                        text: admin.killSwitchReason.length > 0 ? admin.killSwitchReason : ""
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                        elide: Text.ElideRight
                    }
                }

                AdminReasonField { id: killReason }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    AdminButton {
                        Layout.fillWidth: true
                        kind: "danger"
                        text: "Engage kill switch"
                        enabled: !admin.busy && admin.ownerRoutes && killReason.valid && !admin.killSwitchEngaged
                        onClicked: admin.setGlobalKill(true, killReason.text)
                    }
                    AdminButton {
                        Layout.fillWidth: true
                        kind: "secondary"
                        text: "Release"
                        enabled: !admin.busy && admin.ownerRoutes && killReason.valid && admin.killSwitchEngaged
                        onClicked: admin.setGlobalKill(false, killReason.text)
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                // ---- version gate ----------------------------------------
                AdminCard {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Version gate"
                    subtitle: "Clients below the minimum, or on a blocked version, get `version_blocked`."

                    AdminField {
                        id: minVersion
                        compact: true
                        label: "min_client_version"
                        placeholderText: "1.4.2"
                    }
                    AdminField {
                        id: blockedVersions
                        compact: true
                        label: "blocked_versions (comma separated)"
                        placeholderText: "1.3.0, 1.3.1"
                    }
                    AdminReasonField { id: versionReason }
                    AdminButton {
                        Layout.fillWidth: true
                        kind: "primary"
                        text: "Apply version gate"
                        enabled: !admin.busy && admin.ownerRoutes && versionReason.valid
                        onClicked: admin.updateConfig({
                            "min_client_version": minVersion.text.trim(),
                            "blocked_versions": page.parseList(blockedVersions.text)
                        }, versionReason.text)
                    }
                }

                // ---- MOTD ------------------------------------------------
                AdminCard {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "MOTD"
                    subtitle: "Shown by the launcher while `until` is in the future. Clear the text to remove it."

                    AdminField {
                        id: motdText
                        compact: true
                        label: "Message"
                        maximumLength: 240
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        ColumnLayout {
                            Layout.preferredWidth: 140
                            spacing: 4
                            Text {
                                text: "Level"
                                color: Theme.textMuted
                                font.pixelSize: Theme.fontCaption
                                font.weight: Font.DemiBold
                            }
                            AdminCombo {
                                id: motdLevel
                                Layout.fillWidth: true
                                implicitHeight: 34
                                model: ["info", "warn", "maint"]
                            }
                        }
                        AdminField {
                            id: motdUntil
                            compact: true
                            label: "until (unix seconds)"
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                    }
                    AdminReasonField { id: motdReason }
                    AdminButton {
                        Layout.fillWidth: true
                        kind: "primary"
                        text: "Apply MOTD"
                        enabled: !admin.busy && admin.ownerRoutes && motdReason.valid
                        onClicked: admin.updateConfig({
                            "motd": {
                                "text": motdText.text.trim(),
                                "level": motdLevel.currentText,
                                "until": page.num(motdUntil.text, 0)
                            }
                        }, motdReason.text)
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                // ---- reset policy defaults -------------------------------
                AdminCard {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "HWID reset policy defaults"
                    subtitle: "Free resets, then paid credits, then a time deduction. Per-key overrides live on the Licenses screen."

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        AdminField {
                            id: policyFree
                            compact: true
                            label: "free_resets"
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                        AdminField {
                            id: policyPenalty
                            compact: true
                            label: "penalty_days"
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                        AdminField {
                            id: policyCooldown
                            compact: true
                            label: "cooldown_s"
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                    }
                    AdminToggle {
                        id: policySelfService
                        label: "self_service (customers may reset from Discord)"
                        checked: page.policy.self_service !== false
                        onToggled: function (value) { policySelfService.checked = value }
                    }
                    AdminReasonField { id: policyReason }
                    AdminButton {
                        Layout.fillWidth: true
                        kind: "primary"
                        text: "Apply policy defaults"
                        enabled: !admin.busy && admin.ownerRoutes && policyReason.valid
                        onClicked: admin.updateConfig({
                            "reset_policy_defaults": {
                                "free_resets": page.num(policyFree.text, 3),
                                "penalty_days": page.num(policyPenalty.text, 3),
                                "cooldown_s": page.num(policyCooldown.text, 86400),
                                "self_service": policySelfService.checked
                            }
                        }, policyReason.text)
                    }
                }

                // ---- fraud + alerts --------------------------------------
                AdminCard {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Fraud thresholds and alerts"
                    subtitle: "Crossing a threshold flags the key and DMs the owner; it never kills the key."

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        AdminField {
                            id: fraudMachines
                            compact: true
                            label: "machines_30d"
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                        AdminField {
                            id: fraudResets
                            compact: true
                            label: "resets_30d"
                            inputMethodHints: Qt.ImhDigitsOnly
                        }
                    }
                    AdminField {
                        id: alertOwner
                        compact: true
                        label: "alerts.owner_discord_user_id"
                    }
                    AdminField {
                        id: alertEvents
                        compact: true
                        label: "alerts.events (comma separated)"
                        placeholderText: "staff.create, license.revoke, config.*"
                    }
                    AdminReasonField { id: fraudReason }
                    AdminButton {
                        Layout.fillWidth: true
                        kind: "primary"
                        text: "Apply fraud + alerts"
                        enabled: !admin.busy && admin.ownerRoutes && fraudReason.valid
                        onClicked: admin.updateConfig({
                            "fraud_thresholds": {
                                "machines_30d": page.num(fraudMachines.text, 3),
                                "resets_30d": page.num(fraudResets.text, 4)
                            },
                            "alerts": {
                                "owner_discord_user_id": alertOwner.text.trim(),
                                "events": page.parseList(alertEvents.text)
                            }
                        }, fraudReason.text)
                    }
                }
            }

            // ---- owner hardening -----------------------------------------
            AdminCard {
                Layout.fillWidth: true
                title: "Owner hardening"
                subtitle: "TOTP on the admin secret, secret rotation, and the IP allowlist for /api/admin/*."

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 10
                    AdminPill {
                        label: admin.totpRequired ? "TOTP required" : "TOTP off"
                        tone: admin.totpRequired ? Theme.success : Theme.warning
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Enrol, scan the URI, then confirm a code — confirming turns the requirement on."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                        wrapMode: Text.WordWrap
                    }
                }

                AdminReasonField { id: hardeningReason }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    AdminButton {
                        kind: "secondary"
                        text: "Enrol TOTP"
                        enabled: !admin.busy && admin.ownerRoutes && hardeningReason.valid
                        onClicked: admin.totpEnroll(hardeningReason.text)
                    }
                    AdminField {
                        id: totpCode
                        Layout.preferredWidth: 140
                        compact: true
                        label: "6-digit code"
                        maximumLength: 8
                        inputMethodHints: Qt.ImhDigitsOnly
                    }
                    AdminButton {
                        Layout.alignment: Qt.AlignBottom
                        kind: "primary"
                        text: "Confirm code"
                        enabled: !admin.busy && admin.ownerRoutes && hardeningReason.valid && totpCode.text.length >= 6
                        onClicked: {
                            admin.totpConfirm(totpCode.text, hardeningReason.text)
                            totpCode.text = ""
                        }
                    }
                    Item { Layout.fillWidth: true }
                    AdminButton {
                        Layout.alignment: Qt.AlignBottom
                        kind: "danger"
                        text: "Rotate admin secret"
                        enabled: !admin.busy && admin.ownerRoutes && hardeningReason.valid
                        onClicked: admin.rotateAdminSecret(hardeningReason.text)
                    }
                }

                AdminSecretReveal {
                    visible: String(admin.totpEnrollment.otpauth_uri || admin.totpEnrollment.secret || "").length > 0
                    title: "TOTP enrollment (shown once)"
                    message: "Add the URI to the authenticator now, then confirm a code above. The secret is not shown again."
                    copyAllText: String(admin.totpEnrollment.otpauth_uri || "")
                    entries: [
                        { label: "otpauth URI", value: String(admin.totpEnrollment.otpauth_uri || "") },
                        { label: "Secret", value: String(admin.totpEnrollment.secret || "") }
                    ]
                    onDismissed: admin.clearTotpEnrollment()
                }

                AdminSecretReveal {
                    visible: admin.rotatedSecret.length > 0
                    title: "New admin secret (shown once)"
                    message: "Store it in the password manager now. This session already switched to it; the old secret is dead."
                    copyAllText: admin.rotatedSecret
                    entries: [ { label: "Admin secret", value: admin.rotatedSecret } ]
                    onDismissed: admin.clearRotatedSecret()
                }

                Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }

                AdminField {
                    id: ipAllowlist
                    compact: true
                    label: "owner_ip_allowlist (CIDRs, comma separated)"
                    placeholderText: "leave empty to allow any IP"
                    hint: "When non-empty, /api/admin/* from any other address is refused with `ip_not_allowed`."
                }
                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    AdminToggle {
                        id: totpRequiredToggle
                        label: "owner_totp_required"
                        checked: admin.totpRequired
                        onToggled: function (value) { totpRequiredToggle.checked = value }
                    }
                    Item { Layout.fillWidth: true }
                    AdminButton {
                        kind: "primary"
                        text: "Apply hardening"
                        enabled: !admin.busy && admin.ownerRoutes && hardeningReason.valid
                        onClicked: admin.updateConfig({
                            "owner_totp_required": totpRequiredToggle.checked,
                            "owner_ip_allowlist": page.parseList(ipAllowlist.text)
                        }, hardeningReason.text)
                    }
                }
            }
        }
    }
}
