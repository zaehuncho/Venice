import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Login gate. Owner: admin secret (+ TOTP when enrolled, contract §5). Staff:
// one-time enrollment on this machine, then machine-bound login (§2).
// Nothing is persisted: the controller holds credentials in memory only.
Item {
    id: root

    readonly property bool locked: admin.securityLockActive

    // Owner-role staff rows need a fresh owner code at sign-in while owner TOTP is
    // required (rc1 RT-CRIT-01). Blank for support/admin: nothing is sent. The
    // field is wiped as soon as the controller has it.
    function submitStaff() {
        if (admin.busy || staffIdField.text.length === 0)
            return
        const code = ownerCodeField.text
        ownerCodeField.text = ""
        admin.staffLogin(staffIdField.text, code)
    }

    function submitOwner() {
        if (admin.busy)
            return
        admin.ownerLogin(secretField.text, totpField.text)
        secretField.text = ""
    }

    // Two soft accent discs behind the card: the only decoration on the gate, and it is the
    // brand colour doing the work rather than a picture.
    Rectangle {
        anchors.centerIn: parent
        anchors.horizontalCenterOffset: -140
        anchors.verticalCenterOffset: -60
        width: 640; height: 640; radius: 320
        color: Theme.accent
        opacity: 0.075
    }
    Rectangle {
        anchors.centerIn: parent
        anchors.horizontalCenterOffset: 180
        anchors.verticalCenterOffset: 120
        width: 420; height: 420; radius: 210
        color: Theme.accent
        opacity: 0.05
    }

    AdminCard {
        id: card
        width: 480
        anchors.centerIn: parent
        padding: 26
        contentSpacing: 14
        border.color: Theme.borderStrong

        RowLayout {
            Layout.fillWidth: true
            spacing: 16

            AdminBrandMark {
                Layout.alignment: Qt.AlignTop
                size: 58
            }

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 3
                Text {
                    text: admin.ownerMode ? "OWNER CONSOLE" : "STAFF CONSOLE"
                    color: Theme.accentBorder
                    font.pixelSize: Theme.fontMicro
                    font.weight: Font.Bold
                    font.letterSpacing: 1.6
                }
                Text {
                    text: Theme.productName
                    color: Theme.textPrimary
                    font.pixelSize: 28
                    font.weight: Font.Bold
                    font.letterSpacing: -0.6
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
                label: "Admin secret"
                iconText: "*"
                echoMode: TextInput.Password
                placeholderText: "paste the secret value"
                hint: "The value of SSM /orion/admin_secret - the secret itself, not the aws command."
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
                onAccepted: root.submitStaff()
            }

            AdminField {
                id: ownerCodeField
                label: admin.staffLoginNeedsCode ? "Owner code (required for this account)" : "Owner code (owner accounts only)"
                iconText: "#"
                maximumLength: 8
                inputMethodHints: Qt.ImhDigitsOnly
                placeholderText: admin.staffLoginNeedsCode ? "fresh 6-digit code" : "leave blank unless you are an owner"
                hint: admin.staffLoginNeedsCode
                      ? "Enter the current code from the owner authenticator. Each code works once."
                      : "Owner accounts need a fresh code from the owner authenticator. It is not saved."
                onAccepted: root.submitStaff()
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
                    onClicked: root.submitStaff()
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
