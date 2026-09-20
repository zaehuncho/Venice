import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Dashboard — GET /api/admin/metrics (contract §6). Owner routes only.
Item {
    id: page

    property bool loaded: false

    readonly property var m: admin.metrics
    readonly property var licenses: m && m.licenses ? m.licenses : ({})
    readonly property var trials: m && m.trials ? m.trials : ({})
    readonly property var activations: m && m.activations ? m.activations : ({})
    readonly property var resets: m && m.resets ? m.resets : ({})
    readonly property var staff: m && m.staff ? m.staff : ({})

    function value(obj, key) {
        if (!obj || obj[key] === undefined || obj[key] === null)
            return "-"
        return String(obj[key])
    }

    function pairs(obj) {
        var out = []
        if (!obj)
            return out
        var keys = Object.keys(obj)
        keys.sort()
        for (var i = 0; i < keys.length; ++i)
            out.push({ name: keys[i], count: String(obj[keys[i]]) })
        return out
    }

    function reload() {
        admin.refreshMetrics()
        admin.refreshConfig()
        loaded = true
    }

    onVisibleChanged: {
        if (visible && !loaded && admin.ownerRoutes)
            reload()
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
                        text: "Dashboard"
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontDisplay
                        font.weight: Font.DemiBold
                    }
                    Text {
                        text: "Scanned live on request (owner-only, rate limited 6/min)."
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontSmall
                    }
                }
                AdminPill {
                    visible: admin.killSwitchEngaged
                    label: "kill switch engaged"
                    tone: Theme.danger
                }
                AdminButton {
                    kind: "primary"
                    text: "Refresh"
                    enabled: !admin.busy && admin.ownerRoutes
                    onClicked: page.reload()
                }
            }

            Text {
                Layout.fillWidth: true
                visible: !admin.ownerRoutes
                text: "Metrics require owner credentials."
                color: Theme.warning
                font.pixelSize: Theme.fontSmall
            }

            GridLayout {
                Layout.fillWidth: true
                columns: 4
                columnSpacing: 10
                rowSpacing: 10

                AdminStatTile {
                    label: "Active licenses"
                    value: page.value(page.licenses, "active")
                    tone: Theme.success
                    sub: "frozen " + page.value(page.licenses, "frozen") + " · revoked " + page.value(page.licenses, "revoked")
                }
                AdminStatTile {
                    label: "Online now"
                    value: page.value(page.m, "online_now")
                    sub: "heartbeat within 15 min"
                }
                AdminStatTile {
                    label: "Activations 24h"
                    value: page.value(page.activations, "24h")
                    sub: "7d " + page.value(page.activations, "7d")
                }
                AdminStatTile {
                    label: "Fraud flagged"
                    value: page.value(page.m, "fraud_flagged")
                    tone: page.value(page.m, "fraud_flagged") === "0" ? Theme.textPrimary : Theme.warning
                    sub: "flags.suspect = true"
                }
                AdminStatTile {
                    label: "Expired"
                    value: page.value(page.licenses, "expired")
                    sub: "lifetime keys excluded"
                }
                AdminStatTile {
                    label: "Trials active"
                    value: page.value(page.trials, "active")
                    sub: "claimed 7d " + page.value(page.trials, "claimed_7d")
                         + " · converted 30d " + page.value(page.trials, "converted_30d")
                }
                AdminStatTile {
                    label: "HWID resets 24h"
                    value: page.value(page.resets, "24h")
                    sub: "7d " + page.value(page.resets, "7d")
                         + " · paid " + page.value(page.resets, "paid_7d")
                         + " · deduct " + page.value(page.resets, "deduct_7d")
                }
                AdminStatTile {
                    label: "Staff"
                    value: page.value(page.staff, "active")
                    sub: "actions 24h " + page.value(page.staff, "actions_24h")
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 10

                AdminCard {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Licenses by plan"

                    Repeater {
                        model: page.pairs(page.licenses.by_plan)
                        delegate: AdminKeyValue {
                            required property var modelData
                            key: modelData.name
                            value: modelData.count
                        }
                    }
                    Text {
                        visible: page.pairs(page.licenses.by_plan).length === 0
                        text: "No plan breakdown yet."
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontSmall
                    }
                }

                AdminCard {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Client versions seen"

                    Repeater {
                        model: page.pairs(page.m ? page.m.versions : null)
                        delegate: AdminKeyValue {
                            required property var modelData
                            key: modelData.name
                            value: modelData.count
                            mono: true
                        }
                    }
                    Text {
                        visible: page.pairs(page.m ? page.m.versions : null).length === 0
                        text: "No heartbeat versions recorded yet."
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontSmall
                    }
                }
            }

            AdminCard {
                Layout.fillWidth: true
                title: "Service state"
                subtitle: "From GET /api/admin/config. Change it on the Config screen."

                AdminKeyValue {
                    key: "Global kill"
                    value: admin.killSwitchEngaged ? "ENGAGED" : "released"
                    valueColor: admin.killSwitchEngaged ? Theme.logErr : Theme.logOk
                }
                AdminKeyValue {
                    key: "Kill reason"
                    value: admin.killSwitchReason
                }
                AdminKeyValue {
                    key: "Min client version"
                    value: admin.config.min_client_version !== undefined ? String(admin.config.min_client_version) : ""
                    mono: true
                }
                AdminKeyValue {
                    key: "Owner TOTP"
                    value: admin.totpRequired ? "required" : "not enrolled"
                    valueColor: admin.totpRequired ? Theme.logOk : Theme.warning
                }
            }
        }
    }
}
