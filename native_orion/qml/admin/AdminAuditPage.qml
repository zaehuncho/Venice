import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Audit — GET /api/admin/audit (owner, filterable) or GET /api/staff/audit
// (own rows only), cursor-paged (contract §4).
Item {
    id: page

    property bool loaded: false
    property string openId: ""

    readonly property bool ownerView: admin.ownerRoutes

    function filters() {
        if (!ownerView)
            return ({})
        return {
            "since": sinceField.text,
            "until": untilField.text,
            "actor": actorField.text,
            "action": actionField.text,
            "target": targetField.text
        }
    }

    function limit() {
        var parsed = parseInt(limitField.text, 10)
        return isNaN(parsed) ? 50 : Math.max(1, Math.min(200, parsed))
    }

    function load() {
        admin.clearAudit()
        admin.fetchAudit(filters(), "", limit())
        loaded = true
    }

    function tone(result) {
        return (!result || result === "ok") ? Theme.logOk : Theme.logErr
    }

    onVisibleChanged: {
        if (visible && !loaded && admin.authenticated)
            load()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Text {
                    text: page.ownerView ? "Audit" : "My audit"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontDisplay
                    font.weight: Font.DemiBold
                }
                Text {
                    text: page.ownerView
                          ? "Every mutation, with the actor that made it."
                          : "Rows recorded against your staff identity."
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSmall
                }
            }
            AdminButton {
                kind: "primary"
                text: "Load"
                enabled: !admin.busy && admin.authenticated
                onClicked: page.load()
            }
        }

        AdminCard {
            Layout.fillWidth: true
            visible: page.ownerView
            title: "Filters"
            subtitle: "since / until are Unix seconds; actor, action and target match the audit row fields."

            GridLayout {
                Layout.fillWidth: true
                columns: 6
                columnSpacing: 8
                rowSpacing: 8

                AdminField {
                    id: sinceField
                    compact: true
                    label: "since (unix)"
                    inputMethodHints: Qt.ImhDigitsOnly
                }
                AdminField {
                    id: untilField
                    compact: true
                    label: "until (unix)"
                    inputMethodHints: Qt.ImhDigitsOnly
                }
                AdminField {
                    id: actorField
                    compact: true
                    label: "actor"
                    placeholderText: "staff_id / owner"
                }
                AdminField {
                    id: actionField
                    compact: true
                    label: "action"
                    placeholderText: "license.revoke"
                }
                AdminField {
                    id: targetField
                    compact: true
                    label: "target"
                    placeholderText: "key suffix / staff_id"
                }
                AdminField {
                    id: limitField
                    compact: true
                    label: "limit (≤200)"
                    text: "50"
                    inputMethodHints: Qt.ImhDigitsOnly
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            radius: Theme.radiusCard
            color: Theme.bgCard
            border.color: Theme.borderSoft
            border.width: 1

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 12
                spacing: 8

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Text {
                        Layout.fillWidth: true
                        text: admin.auditRows.length + " row(s)"
                                + (admin.auditCursor.length > 0 ? " · more available" : "")
                        color: Theme.textMuted
                        font.pixelSize: Theme.fontCaption
                        font.weight: Font.DemiBold
                    }
                    AdminButton {
                        kind: "ghost"
                        compact: true
                        text: "Clear"
                        enabled: admin.auditRows.length > 0
                        onClicked: admin.clearAudit()
                    }
                    AdminButton {
                        kind: "secondary"
                        compact: true
                        text: "Load more"
                        enabled: !admin.busy && admin.auditCursor.length > 0
                        onClicked: admin.fetchAudit(page.filters(), admin.auditCursor, page.limit())
                    }
                }

                ListView {
                    id: list
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    clip: true
                    spacing: 4
                    model: admin.auditRows
                    boundsBehavior: Flickable.StopAtBounds
                    ScrollBar.vertical: ScrollBar {}

                    delegate: Rectangle {
                        id: auditRow
                        required property var modelData
                        required property int index

                        readonly property string rowId: String(modelData.audit_id || index)
                        readonly property bool open: page.openId === rowId

                        width: ListView.view.width
                        height: rowColumn.implicitHeight + 16
                        radius: Theme.radiusControl
                        color: open ? Theme.bgCardHover : "transparent"
                        border.color: open ? Theme.borderStrong : "transparent"
                        border.width: 1

                        ColumnLayout {
                            id: rowColumn
                            anchors.left: parent.left
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.margins: 8
                            spacing: 6

                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 10
                                Text {
                                    Layout.preferredWidth: 150
                                    text: admin.formatTs(parseInt(auditRow.modelData.ts, 10) || 0)
                                    color: Theme.textFaint
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.fontCaption
                                }
                                Text {
                                    Layout.preferredWidth: 170
                                    text: String(auditRow.modelData.actor_type || "?") + ":"
                                          + String(auditRow.modelData.actor_id || "?")
                                    color: Theme.textSecondary
                                    font.pixelSize: Theme.fontCaption
                                    elide: Text.ElideMiddle
                                }
                                Text {
                                    Layout.preferredWidth: 160
                                    text: String(auditRow.modelData.action || "?")
                                    color: Theme.textPrimary
                                    font.pixelSize: Theme.fontCaption
                                    font.weight: Font.DemiBold
                                    elide: Text.ElideRight
                                }
                                Text {
                                    Layout.preferredWidth: 130
                                    text: String(auditRow.modelData.target_type || "") + " "
                                          + String(auditRow.modelData.target || "")
                                    color: Theme.textSecondary
                                    font.family: Theme.fontMono
                                    font.pixelSize: Theme.fontCaption
                                    elide: Text.ElideMiddle
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: String(auditRow.modelData.reason || "")
                                    color: Theme.textMuted
                                    font.pixelSize: Theme.fontCaption
                                    elide: Text.ElideRight
                                }
                                AdminPill {
                                    label: String(auditRow.modelData.result || "ok")
                                    tone: page.tone(String(auditRow.modelData.result || "ok"))
                                }
                            }

                            ColumnLayout {
                                Layout.fillWidth: true
                                visible: auditRow.open
                                spacing: 4
                                AdminKeyValue { key: "audit_id"; value: auditRow.rowId; mono: true }
                                AdminKeyValue { key: "role"; value: String(auditRow.modelData.role || "") }
                                AdminKeyValue { key: "ip"; value: String(auditRow.modelData.ip || ""); mono: true }
                                AdminKeyValue { key: "day"; value: String(auditRow.modelData.day || ""); mono: true }
                                AdminJsonView {
                                    minimumHeight: 80
                                    text: JSON.stringify(auditRow.modelData.details || {}, null, 2)
                                }
                            }
                        }

                        MouseArea {
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: page.openId = auditRow.open ? "" : auditRow.rowId
                            z: -1
                        }
                    }
                }

                Text {
                    Layout.fillWidth: true
                    visible: admin.auditRows.length === 0
                    text: "No audit rows loaded."
                    color: Theme.textFaint
                    font.pixelSize: Theme.fontSmall
                }
            }
        }
    }
}
