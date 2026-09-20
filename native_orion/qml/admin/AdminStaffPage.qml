import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Staff — POST /api/admin/staff {action, ...} (contract §2, owner only).
// `create` and `reissue_enrollment` return the one-time enroll key; it is shown
// once here and cleared from the controller on dismiss.
Item {
    id: page

    property bool loaded: false
    property string openStaffId: ""

    function num(text, fallback) {
        var parsed = parseInt(text, 10)
        return isNaN(parsed) ? fallback : parsed
    }

    function ts(value) {
        var parsed = parseInt(value, 10)
        return isNaN(parsed) || parsed <= 0 ? "-" : admin.formatTs(parsed)
    }

    function capsOf(member) {
        return member && member.caps ? member.caps : ({})
    }

    function usageOf(member) {
        return member && member.usage ? member.usage : ({})
    }

    onVisibleChanged: {
        if (visible && !loaded && admin.ownerRoutes) {
            admin.refreshStaff()
            loaded = true
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
                        text: "Staff"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontDisplay
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Roles and per-staff caps are what the audit trail stands on."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                    }
                }
                AdminButton {
                    kind: "primary"
                    text: "Refresh"
                    enabled: !admin.busy && admin.ownerRoutes
                    onClicked: admin.refreshStaff()
                }
            }

            Text {
                Layout.fillWidth: true
                visible: !admin.ownerRoutes
                text: "Staff management requires owner credentials."
                color: Theme.warning
                font.pixelSize: Theme.fontSmall
            }

            AdminSecretReveal {
                visible: admin.enrollSecret.enroll_key !== undefined
                         && String(admin.enrollSecret.enroll_key).length > 0
                title: "One-time enrollment key"
                message: "Hand this to the staff member over a private channel. Only its salted hash is stored, so it cannot be shown again — reissue if it is lost."
                copyAllText: "staff_id: " + String(admin.enrollSecret.staff_id || "")
                             + "\nenroll_key: " + String(admin.enrollSecret.enroll_key || "")
                entries: [
                    { label: "Staff ID", value: String(admin.enrollSecret.staff_id || "") },
                    { label: "Enroll key", value: String(admin.enrollSecret.enroll_key || "") }
                ]
                onDismissed: admin.clearEnrollSecret()
            }

            // ---- create --------------------------------------------------
            AdminCard {
                Layout.fillWidth: true
                visible: admin.ownerRoutes
                title: "Create staff"
                subtitle: "Creates the row and returns the enroll key once. Caps bound the member's daily actions."

                GridLayout {
                    Layout.fillWidth: true
                    columns: 5
                    columnSpacing: 8
                    rowSpacing: 8

                    AdminField {
                        id: newDiscord
                        compact: true
                        label: "Discord user ID"
                        iconText: "D"
                    }
                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text {
                            text: "Role"
                            color: Theme.textMuted
                            font.pixelSize: Theme.fontCaption
                            font.weight: Font.DemiBold
                        }
                        AdminCombo {
                            id: newRole
                            Layout.fillWidth: true
                            implicitHeight: 34
                            model: ["support", "admin", "owner"]
                        }
                    }
                    AdminField {
                        id: newKeysPerDay
                        compact: true
                        label: "keys_per_day"
                        text: "0"
                        inputMethodHints: Qt.ImhDigitsOnly
                    }
                    AdminField {
                        id: newResetsPerDay
                        compact: true
                        label: "resets_per_day"
                        text: "5"
                        inputMethodHints: Qt.ImhDigitsOnly
                    }
                    AdminField {
                        id: newExtendMax
                        compact: true
                        label: "extend_max_days"
                        text: "0"
                        inputMethodHints: Qt.ImhDigitsOnly
                    }
                }

                AdminReasonField { id: createReason }

                AdminButton {
                    Layout.fillWidth: true
                    kind: "primary"
                    text: "Create staff"
                    enabled: !admin.busy && admin.ownerRoutes && createReason.valid && newDiscord.text.length > 0
                    onClicked: admin.createStaff(newDiscord.text, newRole.currentText, {
                        "keys_per_day": page.num(newKeysPerDay.text, 0),
                        "resets_per_day": page.num(newResetsPerDay.text, 0),
                        "extend_max_days": page.num(newExtendMax.text, 0)
                    }, createReason.text)
                }
            }

            // ---- roster --------------------------------------------------
            AdminCard {
                Layout.fillWidth: true
                title: admin.staffList.length + " staff row(s)"
                subtitle: "Click a row to open its actions."

                Repeater {
                    model: admin.staffList
                    delegate: Rectangle {
                        id: staffRow
                        required property var modelData

                        readonly property string sid: String(modelData.staff_id || "")
                        readonly property bool disabled: modelData.disabled === true
                        readonly property bool open: page.openStaffId === sid && sid.length > 0

                        Layout.fillWidth: true
                        Layout.preferredHeight: rowColumn.implicitHeight + 20
                        radius: Theme.radiusControl
                        color: open ? Theme.bgCardHover : Theme.bgInset
                        border.color: open ? Theme.accentBorder : Theme.borderSoft
                        border.width: 1

                        ColumnLayout {
                            id: rowColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 10
                            spacing: 8

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 10

                                Text {
                                    Layout.preferredWidth: 150
                                    text: staffRow.sid
                                    color: Theme.textPrimary
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.fontSmall
                                    elide: Text.ElideMiddle
                                }
                                Text {
                                    Layout.preferredWidth: 150
                                    text: String(staffRow.modelData.discord_user_id || "-")
                                    color: Theme.textSecondary
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.fontSmall
                                    elide: Text.ElideRight
                                }
                                AdminPill {
                                    label: String(staffRow.modelData.role || "?")
                                    tone: String(staffRow.modelData.role) === "owner" ? Theme.accent
                                        : String(staffRow.modelData.role) === "admin" ? Theme.logInfo
                                        : Theme.textMuted
                                }
                                AdminPill {
                                    label: staffRow.disabled ? "disabled" : "enabled"
                                    tone: staffRow.disabled ? Theme.danger : Theme.success
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "today: keys " + String(page.usageOf(staffRow.modelData).keys || 0)
                                          + " / " + String(page.capsOf(staffRow.modelData).keys_per_day || 0)
                                          + "   resets " + String(page.usageOf(staffRow.modelData).resets || 0)
                                          + " / " + String(page.capsOf(staffRow.modelData).resets_per_day || 0)
                                          + "   extend max " + String(page.capsOf(staffRow.modelData).extend_max_days || 0) + "d"
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.fontCaption
                                    elide: Text.ElideRight
                                }
                                AdminButton {
                                    kind: "ghost"
                                    compact: true
                                    text: staffRow.open ? "Close" : "Actions"
                                    onClicked: page.openStaffId = staffRow.open ? "" : staffRow.sid
                                }
                            }

                            RowLayout {
                                Layout.fillWidth: true
                                visible: staffRow.open
                                spacing: 16
                                Text {
                                    text: "machine " + (String(staffRow.modelData.machine_id || "").length > 0
                                                        ? String(staffRow.modelData.machine_id).slice(-10)
                                                        : "(unbound)")
                                    color: Theme.textFaint
                                    font.pixelSize: Theme.fontMicro
                                    font.family: Theme.fontMono
                                }
                                Text {
                                    text: "created " + page.ts(staffRow.modelData.created_at)
                                          + " by " + String(staffRow.modelData.created_by || "?")
                                    color: Theme.textFaint
                                    font.pixelSize: Theme.fontMicro
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: "last login " + page.ts(staffRow.modelData.last_login_at)
                                    color: Theme.textFaint
                                    font.pixelSize: Theme.fontMicro
                                    elide: Text.ElideRight
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                visible: staffRow.open
                                spacing: 8

                                AdminReasonField { id: rowReason }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    AdminButton {
                                        kind: staffRow.disabled ? "secondary" : "danger"
                                        compact: true
                                        text: staffRow.disabled ? "Enable" : "Disable"
                                        enabled: !admin.busy && rowReason.valid
                                        onClicked: admin.staffAction(staffRow.disabled ? "enable" : "disable",
                                                                     staffRow.sid, rowReason.text, ({}))
                                    }
                                    AdminButton {
                                        kind: "secondary"
                                        compact: true
                                        text: "Reset machine"
                                        enabled: !admin.busy && rowReason.valid
                                        onClicked: admin.staffAction("reset_machine", staffRow.sid, rowReason.text, ({}))
                                    }
                                    AdminButton {
                                        kind: "secondary"
                                        compact: true
                                        text: "Reissue enroll key"
                                        enabled: !admin.busy && rowReason.valid
                                        onClicked: admin.staffAction("reissue_enrollment", staffRow.sid, rowReason.text, ({}))
                                    }
                                    Item { Layout.fillWidth: true }
                                }

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 8
                                    AdminCombo {
                                        id: rowRole
                                        Layout.preferredWidth: 130
                                        implicitHeight: 32
                                        model: ["support", "admin", "owner"]
                                    }
                                    AdminButton {
                                        kind: "secondary"
                                        compact: true
                                        text: "Set role"
                                        enabled: !admin.busy && rowReason.valid
                                        onClicked: admin.staffAction("set_role", staffRow.sid, rowReason.text,
                                                                     { "role": rowRole.currentText })
                                    }
                                    AdminField {
                                        id: rowKeys
                                        Layout.preferredWidth: 96
                                        compact: true
                                        label: "keys/day"
                                        text: String(page.capsOf(staffRow.modelData).keys_per_day || 0)
                                        inputMethodHints: Qt.ImhDigitsOnly
                                    }
                                    AdminField {
                                        id: rowResets
                                        Layout.preferredWidth: 96
                                        compact: true
                                        label: "resets/day"
                                        text: String(page.capsOf(staffRow.modelData).resets_per_day || 0)
                                        inputMethodHints: Qt.ImhDigitsOnly
                                    }
                                    AdminField {
                                        id: rowExtend
                                        Layout.preferredWidth: 110
                                        compact: true
                                        label: "extend max days"
                                        text: String(page.capsOf(staffRow.modelData).extend_max_days || 0)
                                        inputMethodHints: Qt.ImhDigitsOnly
                                    }
                                    AdminButton {
                                        Layout.alignment: Qt.AlignBottom
                                        kind: "secondary"
                                        compact: true
                                        text: "Set caps"
                                        enabled: !admin.busy && rowReason.valid
                                        onClicked: admin.staffAction("set_caps", staffRow.sid, rowReason.text, {
                                            "caps": {
                                                "keys_per_day": page.num(rowKeys.text, 0),
                                                "resets_per_day": page.num(rowResets.text, 0),
                                                "extend_max_days": page.num(rowExtend.text, 0)
                                            }
                                        })
                                    }
                                }
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            enabled: !staffRow.open
                            cursorShape: Qt.PointingHandCursor
                            onClicked: page.openStaffId = staffRow.sid
                        }
                    }
                }

                Text {
                    visible: admin.staffList.length === 0
                    text: "No staff rows loaded."
                    color: Theme.textFaint
                    font.pixelSize: Theme.fontSmall
                }
            }
        }
    }
}
