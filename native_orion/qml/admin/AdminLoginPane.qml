import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Login gate. Owner: admin secret (+ TOTP when enrolled, contract §5). Staff:
// one-time enrollment on this machine, then machine-bound login (§2).
// Nothing is persisted: the controller holds credentials in memory only.
Item {
    id: root

    readonly property bool locked: admin.securityLockActive

    function submitOwner() {
        if (admin.busy)
            return
        admin.ownerLogin(secretField.text, totpField.text)
        secretField.text = ""
    }

    AdminCard {
        id: card
        width: 520
        anchors.centerIn: parent
        padding: 24
        contentSpacing: 14
        color: "#0B1019"
        border.color: Theme.borderStrong

        RowLayout {
            Layout.fillWidth: true
            spacing: 14

            Rectangle {
                Layout.preferredWidth: 52
                Layout.preferredHeight: 52
                radius: 16
                color: "#0B0F14"
                border.color: Theme.accentBorder
                border.width: 1
                Rectangle {
                    anchors.centerIn: parent
                    width: 24
                    height: 24
                    radius: 12
                    color: "transparent"
                    border.color: Theme.accent
                    border.width: 2
                }
                Rectangle {
                    anchors.centerIn: parent
                    width: 11
                    height: 11
                    radius: 5.5
                    color: Theme.accent
                }
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Text {
                    text: admin.ownerMode ? "Orion Owner" : "Orion Staff"
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontHeading
                    font.weight: Font.DemiBold
                }
                Text {
                    Layout.fillWidth: true
                    text: admin.ownerMode
                          ? "Break-glass owner console. The secret is never stored on disk."
                          : "Machine-bound staff console. Enrol once, then sign in."
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontSmall
                    wrapMode: Text.WordWrap
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 1
            color: Theme.hairline
        }

        // ---- owner -----------------------------------------------------
        ColumnLayout {
            Layout.fillWidth: true
            visible: admin.ownerMode
            spacing: 12

            AdminField {
                id: secretField
                label: "Admin secret (SSM /orion/admin_secret)"
                iconText: "*"
                echoMode: TextInput.Password
                placeholderText: "paste the secret value, not the aws command"
                onAccepted: root.submitOwner()
            }

            AdminField {
                id: totpField
                label: "TOTP code"
                iconText: "#"
                maximumLength: 8
                inputMethodHints: Qt.ImhDigitsOnly
                placeholderText: admin.totpRequired ? "required - 6 digits" : "only if TOTP is enrolled"
                hint: admin.totpRequired
                      ? "This owner account requires a code (owner_totp_required = true)."
                      : "Leave blank until TOTP is enrolled on the Config screen."
                onAccepted: root.submitOwner()
            }

            AdminButton {
                Layout.fillWidth: true
                kind: "primary"
                text: admin.busy ? "Signing in..." : "Sign in"
                enabled: !admin.busy && !root.locked && secretField.text.length > 0
                onClicked: root.submitOwner()
            }
        }

        // ---- staff -----------------------------------------------------
        ColumnLayout {
            Layout.fillWidth: true
            visible: !admin.ownerMode
            spacing: 12

            AdminField {
                id: staffIdField
                label: "Staff ID"
                iconText: "S"
                placeholderText: "staff_xxxxxxxx (from the owner)"
                onAccepted: admin.staffLogin(staffIdField.text)
            }

            AdminField {
                id: enrollField
                label: "One-time enrollment key"
                iconText: "K"
                echoMode: TextInput.Password
                placeholderText: "only needed the first time on this machine"
                hint: "The owner issues this once per staff row; it is hashed server-side and cannot be shown again."
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                AdminButton {
                    Layout.fillWidth: true
                    kind: "secondary"
                    text: "Enrol this machine"
                    enabled: !admin.busy && !root.locked && staffIdField.text.length > 0 && enrollField.text.length > 0
                    onClicked: {
                        admin.staffEnroll(staffIdField.text, enrollField.text)
                        enrollField.text = ""
                    }
                }
                AdminButton {
                    Layout.fillWidth: true
                    kind: "primary"
                    text: admin.busy ? "Signing in..." : "Sign in"
                    enabled: !admin.busy && !root.locked && staffIdField.text.length > 0
                    onClicked: admin.staffLogin(staffIdField.text)
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: statusText.implicitHeight + 18
            radius: Theme.radiusControl
            visible: admin.statusMessage.length > 0 || root.locked
            color: admin.statusIsError || root.locked ? Theme.dangerDim : Theme.bgInset
            border.color: admin.statusIsError || root.locked ? Theme.dangerBorder : Theme.borderSoft
            border.width: 1

            Text {
                id: statusText
                anchors.fill: parent
                anchors.margins: 9
                text: root.locked ? admin.securityState : admin.statusMessage
                color: admin.statusIsError || root.locked ? Theme.logErr : Theme.textSecondary
                font.pixelSize: Theme.fontSmall
                wrapMode: Text.WordWrap
                verticalAlignment: Text.AlignVCenter
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                Layout.fillWidth: true
                text: "machine " + admin.machineIdSuffix + "  ·  v" + admin.appVersion
                color: Theme.textFaint
                font.pixelSize: Theme.fontMicro
                font.family: Theme.fontMono
                elide: Text.ElideMiddle
            }
            AdminButton {
                kind: "ghost"
                compact: true
                text: "Re-check security"
                onClicked: admin.refreshSecurity()
            }
        }
    }
}
