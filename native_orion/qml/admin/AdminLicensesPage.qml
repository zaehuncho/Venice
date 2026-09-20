import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Licenses — POST /api/admin/license (owner) or /api/staff/license (staff),
// body {action, reason, ...} (contract §2) plus the HWID reset policy view (§3).
// Capability gating here is display-only; the server decides.
Item {
    id: page

    property int selectedIndex: -1

    readonly property var rows: admin.licenses
    readonly property var lic: (selectedIndex >= 0 && selectedIndex < rows.length) ? rows[selectedIndex] : admin.license
    readonly property string licKey: lic ? String(lic.license_key || lic.key || "") : ""
    readonly property var history: lic && lic.reset_history ? lic.reset_history : []
    readonly property var flags: lic && lic.flags ? lic.flags : ({})

    // Reads the capabilities PROPERTY so bindings re-evaluate on login; calling
    // the controller's can() method directly would register no dependency.
    function can(name) {
        return admin.capabilities[name] === true
    }

    function field(obj, key, fallback) {
        if (!obj || obj[key] === undefined || obj[key] === null || obj[key] === "")
            return fallback === undefined ? "" : fallback
        return String(obj[key])
    }

    function num(text, fallback) {
        var parsed = parseInt(text, 10)
        return isNaN(parsed) ? fallback : parsed
    }

    function ts(value) {
        var parsed = parseInt(value, 10)
        return isNaN(parsed) || parsed <= 0 ? "-" : admin.formatTs(parsed)
    }

    function statusTone(status) {
        if (status === "revoked" || status === "blacklisted")
            return Theme.danger
        if (status === "frozen" || status === "expired")
            return Theme.warning
        return Theme.success
    }

    function act(action, extra) {
        admin.licenseAction(action, page.licKey, reasonField.text, extra || ({}))
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
                        text: "Licenses"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontDisplay
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: admin.ownerRoutes
                              ? "Owner routes — every action available."
                              : "Staff routes — limited to your role's capabilities."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                    }
                }
            }

            // ---- lookup -------------------------------------------------
            AdminCard {
                Layout.fillWidth: true
                title: "Look up"
                subtitle: "Key, email, Discord ID, or machine ID. Support sees the machine ID masked to the last 6."

                GridLayout {
                    Layout.fillWidth: true
                    columns: 4
                    columnSpacing: 8
                    rowSpacing: 8

                    AdminField {
                        id: keyQuery
                        label: "License key"
                        iconText: "K"
                        placeholderText: "ORION-XXXX-XXXX-XXXX"
                        onAccepted: lookupButton.clicked()
                    }
                    AdminField {
                        id: emailQuery
                        label: "Email"
                        iconText: "@"
                        onAccepted: lookupButton.clicked()
                    }
                    AdminField {
                        id: discordQuery
                        label: "Discord user ID"
                        iconText: "D"
                        onAccepted: lookupButton.clicked()
                    }
                    AdminField {
                        id: machineQuery
                        label: "Machine ID"
                        iconText: "M"
                        onAccepted: lookupButton.clicked()
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        Layout.fillWidth: true
                        text: admin.licenseResult.lookup_mode !== undefined
                              ? "lookup_mode: " + admin.licenseResult.lookup_mode
                              : ""
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontMicro
                        font.family: Theme.fontMono
                    }
                    AdminButton {
                        kind: "ghost"
                        compact: true
                        text: "Clear"
                        onClicked: {
                            keyQuery.text = ""
                            emailQuery.text = ""
                            discordQuery.text = ""
                            machineQuery.text = ""
                            page.selectedIndex = -1
                        }
                    }
                    AdminButton {
                        id: lookupButton
                        kind: "primary"
                        text: "Look up"
                        enabled: !admin.busy && page.can("license.lookup")
                        onClicked: {
                            page.selectedIndex = -1
                            admin.lookupLicense({
                                "key": keyQuery.text,
                                "email": emailQuery.text,
                                "discord_user_id": discordQuery.text,
                                "machine_id": machineQuery.text
                            })
                        }
                    }
                }
            }

            // ---- match picker -------------------------------------------
            AdminCard {
                Layout.fillWidth: true
                visible: page.rows.length > 1
                title: page.rows.length + " matches"
                subtitle: "Pick the license to work on."

                Repeater {
                    model: page.rows
                    delegate: Rectangle {
                        id: matchRow
                        required property var modelData
                        required property int index

                        Layout.fillWidth: true
                        Layout.preferredHeight: 34
                        radius: Theme.radiusControl
                        color: page.selectedIndex === index ? Theme.accentSoft : Theme.bgInset
                        border.color: page.selectedIndex === index ? Theme.accentBorder : Theme.borderSoft
                        border.width: 1

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 10
                            spacing: 10
                            Text {
                                Layout.preferredWidth: 190
                                text: String(matchRow.modelData.license_key || matchRow.modelData.key || "?")
                                color: Theme.textPrimary
                                font.family: Theme.fontMono
                                font.pixelSize: Theme.fontSmall
                                elide: Text.ElideMiddle
                            }
                            Text {
                                Layout.fillWidth: true
                                text: String(matchRow.modelData.email || matchRow.modelData.discord_user_id || "")
                                color: Theme.textSecondary
                                font.pixelSize: Theme.fontSmall
                                elide: Text.ElideRight
                            }
                            AdminPill {
                                label: String(matchRow.modelData.status || "?")
                                tone: page.statusTone(String(matchRow.modelData.status || ""))
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: page.selectedIndex = matchRow.index
                        }
                    }
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                // ---- detail + policy ------------------------------------
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.alignment: Qt.AlignTop
                    spacing: 10

                    AdminCard {
                        Layout.fillWidth: true
                        title: "License"
                        subtitle: page.licKey.length === 0 ? "Nothing selected yet." : ""

                        AdminKeyValue { key: "Key"; value: page.licKey; mono: true; copyable: true }
                        AdminKeyValue {
                            key: "Status"
                            value: page.field(page.lic, "status", "-")
                            valueColor: page.statusTone(page.field(page.lic, "status"))
                        }
                        AdminKeyValue { key: "Plan"; value: page.field(page.lic, "plan") }
                        AdminKeyValue { key: "Email"; value: page.field(page.lic, "email") }
                        AdminKeyValue { key: "Discord"; value: page.field(page.lic, "discord_user_id"); mono: true }
                        AdminKeyValue {
                            key: "Expiry"
                            value: page.field(page.lic, "expiry") === "0"
                                   ? "lifetime"
                                   : page.ts(page.field(page.lic, "expiry"))
                        }
                        AdminKeyValue { key: "Machine"; value: page.field(page.lic, "machine_id"); mono: true }
                        AdminKeyValue { key: "Last check"; value: page.ts(page.field(page.lic, "last_check_at")) }
                        AdminKeyValue { key: "Client version"; value: page.field(page.lic, "client_version"); mono: true }
                        AdminKeyValue { key: "Frozen at"; value: page.ts(page.field(page.lic, "frozen_at")) }
                        AdminKeyValue {
                            key: "Fraud flag"
                            value: page.flags.suspect ? "SUSPECT" : "clean"
                            valueColor: page.flags.suspect ? Theme.logErr : Theme.logOk
                        }
                        AdminKeyValue { key: "Revoke reason"; value: page.field(page.lic, "revoke_reason") }
                    }

                    AdminCard {
                        Layout.fillWidth: true
                        title: "HWID reset policy"
                        subtitle: "Three free resets, then paid credits, then a time deduction (contract §3)."

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            AdminStatTile {
                                label: "Free left"
                                value: String(Math.max(0, page.num(page.field(page.lic, "hwid_free_resets", "3"), 3)
                                                       - page.num(page.field(page.lic, "hwid_resets_used", "0"), 0)))
                                sub: "used " + page.field(page.lic, "hwid_resets_used", "0")
                                     + " of " + page.field(page.lic, "hwid_free_resets", "3")
                            }
                            AdminStatTile {
                                label: "Paid credits"
                                value: page.field(page.lic, "hwid_paid_credits", "0")
                                sub: "granted by the HWID reset product"
                            }
                            AdminStatTile {
                                label: "Locked"
                                value: page.lic && page.lic.hwid_reset_locked ? "YES" : "no"
                                tone: page.lic && page.lic.hwid_reset_locked ? Theme.danger : Theme.textPrimary
                                sub: "last " + page.ts(page.field(page.lic, "last_reset_at"))
                            }
                        }

                        Text {
                            Layout.fillWidth: true
                            text: "Reset history (newest last)"
                            color: Theme.textMuted
                            font.pixelSize: Theme.fontCaption
                            font.weight: Font.DemiBold
                        }
                        Repeater {
                            model: page.history
                            delegate: AdminKeyValue {
                                required property var modelData
                                key: page.ts(modelData.ts)
                                keyWidth: 150
                                value: String(modelData.mode || "?") + "  by " + String(modelData.by || "?")
                                       + "  (was ..." + String(modelData.machine_before_suffix || "?") + ")"
                                mono: true
                            }
                        }
                        Text {
                            visible: page.history.length === 0
                            text: "No resets recorded."
                            color: Theme.textFaint
                            font.pixelSize: Theme.fontSmall
                        }
                    }
                }

                // ---- actions --------------------------------------------
                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.preferredWidth: 1
                    Layout.alignment: Qt.AlignTop
                    spacing: 10

                    AdminCard {
                        Layout.fillWidth: true
                        title: "Actions"
                        subtitle: "Every mutation needs a reason (≤200 chars). It lands in the audit row."

                        AdminReasonField { id: reasonField }

                        Text {
                            Layout.fillWidth: true
                            text: page.licKey.length > 0
                                  ? "Target: " + page.licKey
                                  : "Look a license up first."
                            color: page.licKey.length > 0 ? Theme.textSecondary : Theme.warning
                            font.pixelSize: Theme.fontSmall
                            font.family: page.licKey.length > 0 ? Theme.fontMono : Theme.fontUi
                            elide: Text.ElideMiddle
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            AdminButton {
                                Layout.fillWidth: true
                                kind: "danger"
                                text: "Revoke"
                                visible: page.can("license.revoke")
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                onClicked: page.act("revoke")
                            }
                            AdminButton {
                                Layout.fillWidth: true
                                kind: "secondary"
                                text: "Unrevoke"
                                visible: page.can("license.unrevoke")
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                onClicked: page.act("unrevoke")
                            }
                            AdminButton {
                                Layout.fillWidth: true
                                kind: "secondary"
                                text: "Freeze"
                                visible: page.can("license.freeze")
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                onClicked: page.act("freeze")
                            }
                            AdminButton {
                                Layout.fillWidth: true
                                kind: "secondary"
                                text: "Unfreeze"
                                visible: page.can("license.unfreeze")
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                onClicked: page.act("unfreeze")
                            }
                        }

                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: page.can("license.extend")
                            spacing: 8
                            AdminField {
                                id: extendDays
                                Layout.preferredWidth: 110
                                compact: true
                                label: "Extend by days"
                                iconText: "+"
                                text: "30"
                                inputMethodHints: Qt.ImhDigitsOnly
                            }
                            AdminButton {
                                Layout.alignment: Qt.AlignBottom
                                kind: "secondary"
                                text: "Extend"
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                         && page.num(extendDays.text, 0) > 0
                                onClicked: page.act("extend", { "days": page.num(extendDays.text, 0) })
                            }
                            Item { Layout.fillWidth: true }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: page.can("license.set_plan")
                            spacing: 8
                            AdminField {
                                id: planField
                                Layout.preferredWidth: 160
                                compact: true
                                label: "Set plan"
                                iconText: "P"
                                placeholderText: "standard / pro / lifetime"
                            }
                            AdminButton {
                                Layout.alignment: Qt.AlignBottom
                                kind: "secondary"
                                text: "Set plan"
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                         && planField.text.length > 0
                                onClicked: page.act("set_plan", { "plan": planField.text })
                            }
                            Item { Layout.fillWidth: true }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: page.can("license.transfer")
                            spacing: 8
                            AdminField {
                                id: transferDiscord
                                compact: true
                                label: "Transfer to Discord ID"
                                iconText: "D"
                            }
                            AdminField {
                                id: transferEmail
                                compact: true
                                label: "or email"
                                iconText: "@"
                            }
                            AdminButton {
                                Layout.alignment: Qt.AlignBottom
                                kind: "secondary"
                                text: "Transfer"
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                         && (transferDiscord.text.length > 0 || transferEmail.text.length > 0)
                                onClicked: page.act("transfer", {
                                    "discord_user_id": transferDiscord.text,
                                    "email": transferEmail.text
                                })
                            }
                        }

                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }

                        RowLayout {
                            Layout.fillWidth: true
                            visible: page.can("license.reset_machine")
                            spacing: 8
                            AdminToggle {
                                id: forceReset
                                visible: page.can("license.reset_machine force")
                                label: "force (ignore cooldown, lock and counters)"
                                checked: false
                                onToggled: function(value) { forceReset.checked = value }
                            }
                            Item { Layout.fillWidth: true }
                            AdminButton {
                                kind: forceReset.checked ? "danger" : "secondary"
                                text: "Reset machine"
                                enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                onClicked: page.act("reset_machine", { "force": forceReset.checked })
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            visible: page.can("license.set_reset_policy")
                            spacing: 6
                            Text {
                                text: "Reset policy for this key (owner)"
                                color: Theme.textMuted
                                font.pixelSize: Theme.fontCaption
                                font.weight: Font.DemiBold
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 8
                                AdminField {
                                    id: policyFree
                                    Layout.preferredWidth: 110
                                    compact: true
                                    label: "Free resets"
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    placeholderText: "3"
                                }
                                AdminField {
                                    id: policyPenalty
                                    Layout.preferredWidth: 110
                                    compact: true
                                    label: "Penalty days"
                                    inputMethodHints: Qt.ImhDigitsOnly
                                    placeholderText: "3"
                                }
                                AdminToggle {
                                    id: policyLocked
                                    Layout.alignment: Qt.AlignBottom
                                    label: "locked"
                                    checked: page.lic ? page.lic.hwid_reset_locked === true : false
                                    onToggled: function(value) { policyLocked.checked = value }
                                }
                                AdminButton {
                                    Layout.alignment: Qt.AlignBottom
                                    kind: "secondary"
                                    text: "Apply policy"
                                    enabled: !admin.busy && reasonField.valid && page.licKey.length > 0
                                    onClicked: {
                                        var extra = { "locked": policyLocked.checked }
                                        if (policyFree.text.length > 0)
                                            extra.free_resets = page.num(policyFree.text, 3)
                                        if (policyPenalty.text.length > 0)
                                            extra.penalty_days = page.num(policyPenalty.text, 3)
                                        page.act("set_reset_policy", extra)
                                    }
                                }
                            }
                        }
                    }

                    // ---- create -----------------------------------------
                    AdminCard {
                        Layout.fillWidth: true
                        visible: page.can("license.create")
                        title: "Create licenses"
                        subtitle: "Up to 25 per call; admins are additionally bounded by caps.keys_per_day."

                        GridLayout {
                            Layout.fillWidth: true
                            columns: 3
                            columnSpacing: 8
                            rowSpacing: 8
                            AdminField {
                                id: createPlan
                                compact: true
                                label: "Plan"
                                text: "standard"
                            }
                            AdminField {
                                id: createDays
                                compact: true
                                label: "Days (0 = lifetime)"
                                text: "30"
                                inputMethodHints: Qt.ImhDigitsOnly
                            }
                            AdminField {
                                id: createCount
                                compact: true
                                label: "Count"
                                text: "1"
                                inputMethodHints: Qt.ImhDigitsOnly
                            }
                            AdminField {
                                id: createDiscord
                                compact: true
                                label: "Discord ID (optional)"
                            }
                            AdminField {
                                id: createEmail
                                compact: true
                                label: "Email (optional)"
                            }
                            AdminField {
                                id: createNote
                                compact: true
                                label: "Note (optional)"
                            }
                        }

                        AdminReasonField { id: createReason }

                        AdminButton {
                            Layout.fillWidth: true
                            kind: "primary"
                            text: "Create"
                            enabled: !admin.busy && createReason.valid && createPlan.text.length > 0
                                     && page.num(createCount.text, 0) >= 1 && page.num(createCount.text, 0) <= 25
                            onClicked: admin.createLicenses(createPlan.text,
                                                            page.num(createDays.text, 0),
                                                            page.num(createCount.text, 1),
                                                            createDiscord.text,
                                                            createEmail.text,
                                                            createNote.text,
                                                            createReason.text)
                        }

                        AdminSecretReveal {
                            visible: admin.createdKeys.length > 0
                            title: admin.createdKeys.length + " key(s) created"
                            message: "Copy these now. The panel never shows a full key again."
                            copyAllText: admin.createdKeys.join("\n")
                            entries: admin.createdKeys.map(function (k) { return { label: "", value: String(k) } })
                            onDismissed: admin.clearCreatedKeys()
                        }
                    }

                    // ---- blacklist --------------------------------------
                    AdminCard {
                        Layout.fillWidth: true
                        visible: admin.ownerRoutes
                        title: "Blacklist"
                        subtitle: "Blocks activate / trial / heartbeat with error code `blacklisted` (owner only)."

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            AdminCombo {
                                id: blacklistKind
                                Layout.preferredWidth: 130
                                model: ["machine", "discord"]
                            }
                            AdminField {
                                id: blacklistId
                                compact: true
                                placeholderText: blacklistKind.currentText === "discord" ? "Discord user ID" : "machine ID"
                            }
                        }

                        AdminReasonField { id: blacklistReason }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8
                            AdminButton {
                                Layout.fillWidth: true
                                kind: "danger"
                                text: "Blacklist"
                                enabled: !admin.busy && blacklistReason.valid && blacklistId.text.length > 0
                                onClicked: admin.blacklist(blacklistKind.currentText, blacklistId.text, blacklistReason.text, true)
                            }
                            AdminButton {
                                Layout.fillWidth: true
                                kind: "secondary"
                                text: "Remove"
                                enabled: !admin.busy && blacklistReason.valid && blacklistId.text.length > 0
                                onClicked: admin.blacklist(blacklistKind.currentText, blacklistId.text, blacklistReason.text, false)
                            }
                        }
                    }
                }
            }
        }
    }
}
