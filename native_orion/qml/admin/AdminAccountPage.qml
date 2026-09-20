import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// My access — the staff panel's own identity, capabilities and daily usage
// (contract §7). Read from /api/staff/whoami.
Item {
    id: page

    readonly property var caps: admin.caps
    readonly property var usage: admin.usage

    function can(name) {
        return admin.capabilities[name] === true
    }

    function value(obj, key, fallback) {
        if (!obj || obj[key] === undefined || obj[key] === null)
            return fallback === undefined ? "-" : fallback
        return String(obj[key])
    }

    function used(key, capKey) {
        return page.value(usage, key, "0") + " / " + page.value(caps, capKey, "0")
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
                        text: "My access"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontDisplay
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "What this role can do, and what it has already used today."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                    }
                }
                AdminButton {
                    kind: "primary"
                    text: "Refresh"
                    enabled: !admin.busy && admin.authenticated
                    onClicked: admin.refreshWhoami()
                }
            }

            AdminCard {
                Layout.fillWidth: true
                title: "Identity"

                AdminKeyValue { key: "Staff ID"; value: admin.staffId; mono: true; copyable: true }
                AdminKeyValue { key: "Display name"; value: admin.staffName }
                AdminKeyValue { key: "Role"; value: admin.role }
                AdminKeyValue { key: "Machine"; value: admin.machineIdSuffix; mono: true }
                AdminKeyValue { key: "Tool version"; value: admin.appVersion; mono: true }
                AdminKeyValue {
                    key: "Security"
                    value: admin.securityState
                    valueColor: admin.securityLockActive ? Theme.logErr : Theme.logOk
                }
            }

            AdminCard {
                Layout.fillWidth: true
                title: "Caps and usage today"
                subtitle: "Usage day: " + page.value(page.usage, "day", "(not reported)")

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    AdminStatTile {
                        label: "Keys created"
                        value: page.used("keys", "keys_per_day")
                    }
                    AdminStatTile {
                        label: "HWID resets"
                        value: page.used("resets", "resets_per_day")
                    }
                    AdminStatTile {
                        label: "Extend max"
                        value: page.value(page.caps, "extend_max_days", "0") + " d"
                    }
                }
            }

            AdminCard {
                Layout.fillWidth: true
                title: "Capabilities"
                subtitle: "Shown from the role matrix; the server enforces the real gate on every call."

                Repeater {
                    model: [
                        "license.lookup", "license.create", "license.reset_machine",
                        "license.reset_machine force", "license.extend", "license.revoke",
                        "license.unrevoke", "license.freeze", "license.unfreeze",
                        "license.set_plan", "license.transfer", "license.set_reset_policy",
                        "staff.manage", "config.write", "audit.read_all", "audit.read_own"
                    ]
                    delegate: AdminKeyValue {
                        required property var modelData
                        key: String(modelData)
                        keyWidth: 200
                        value: page.can(String(modelData)) ? "allowed" : "denied"
                        valueColor: page.can(String(modelData)) ? Theme.logOk : Theme.textFaint
                    }
                }
            }
        }
    }
}
