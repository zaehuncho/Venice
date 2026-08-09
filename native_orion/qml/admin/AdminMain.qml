import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: window

    width: 1120
    height: 720
    minimumWidth: 1040
    minimumHeight: 680
    visible: true
    title: admin.ownerMode ? "Orion Owner" : "Orion Staff"
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontBody

    readonly property bool canAct: admin.authenticated && !admin.busy && !admin.securityLockActive
    readonly property color toolAccent: admin.ownerMode ? "#4F8CFF" : "#7C3AED"

    Component.onCompleted: Theme.accent = toolAccent

    component Card: Rectangle {
        color: Theme.bgCard
        radius: Theme.radiusCard
        border.color: Theme.borderSoft
        border.width: 1
        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 1
            radius: parent.radius
            color: Theme.hairlineLight
        }
    }

    component FieldBox: Rectangle {
        id: fieldBox
        property alias text: input.text
        property alias placeholderText: input.placeholderText
        property alias echoMode: input.echoMode
        property alias input: input
        property string iconText: ""
        signal accepted()

        Layout.fillWidth: true
        Layout.preferredHeight: 46
        radius: Theme.radiusControl
        color: Theme.bgField
        border.color: input.activeFocus ? Theme.accentBorder : Theme.borderSoft
        border.width: input.activeFocus ? 2 : 1

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 13
            anchors.rightMargin: 13
            spacing: 10

            Text {
                visible: fieldBox.iconText.length > 0
                text: fieldBox.iconText
                color: input.activeFocus ? Theme.accentBorder : Theme.textFaint
                font.pixelSize: 14
                font.weight: Font.DemiBold
                Layout.alignment: Qt.AlignVCenter
            }

            TextField {
                id: input
                Layout.fillWidth: true
                Layout.fillHeight: true
                verticalAlignment: TextInput.AlignVCenter
                selectByMouse: true
                color: Theme.textPrimary
                placeholderTextColor: Theme.textFaint
                font.family: Theme.fontUi
                font.pixelSize: 13
                background: null
                Keys.onReturnPressed: fieldBox.accepted()
            }
        }
    }

    component DarkComboBox: ComboBox {
        id: combo

        implicitHeight: 40
        font.family: Theme.fontUi
        font.pixelSize: 13
        leftPadding: 14
        rightPadding: 34

        contentItem: Text {
            leftPadding: 14
            rightPadding: 34
            text: combo.displayText
            color: combo.enabled ? Theme.textPrimary : Theme.textFaint
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            font: combo.font
        }

        indicator: Text {
            anchors.right: parent.right
            anchors.rightMargin: 13
            anchors.verticalCenter: parent.verticalCenter
            text: "v"
            color: combo.enabled ? Theme.textMuted : Theme.textFaint
            font.pixelSize: 12
            font.weight: Font.DemiBold
        }

        background: Rectangle {
            radius: Theme.radiusControl
            color: combo.enabled ? Theme.bgField : Theme.bgInset
            border.color: combo.activeFocus || combo.popup.visible ? Theme.accentBorder : Theme.borderSoft
            border.width: combo.activeFocus || combo.popup.visible ? 2 : 1
        }

        delegate: ItemDelegate {
            id: comboDelegate
            required property var modelData
            required property int index

            width: combo.width - 8
            height: 34
            highlighted: combo.highlightedIndex === index

            contentItem: Text {
                text: comboDelegate.modelData
                color: comboDelegate.highlighted ? Theme.textPrimary : Theme.textSecondary
                verticalAlignment: Text.AlignVCenter
                elide: Text.ElideRight
                font.family: Theme.fontUi
                font.pixelSize: 13
            }

            background: Rectangle {
                radius: 8
                color: comboDelegate.highlighted ? Theme.accentSoft : "transparent"
            }
        }

        popup: Popup {
            y: combo.height + 5
            width: combo.width
            implicitHeight: Math.min(contentItem.implicitHeight + 8, 150)
            padding: 4

            contentItem: ListView {
                clip: true
                implicitHeight: contentHeight
                model: combo.popup.visible ? combo.delegateModel : null
                currentIndex: combo.highlightedIndex
                boundsBehavior: Flickable.StopAtBounds
            }

            background: Rectangle {
                color: Theme.bgInset
                radius: Theme.radiusControl
                border.color: Theme.borderStrong
                border.width: 1
            }
        }
    }

    component PrimaryButton: Button {
        id: primaryControl
        implicitHeight: 40
        font.family: Theme.fontUi
        font.pixelSize: 13
        font.weight: Font.DemiBold

        contentItem: Text {
            text: primaryControl.text
            color: primaryControl.enabled ? Theme.textPrimary : Theme.textFaint
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            font: primaryControl.font
        }

        background: Rectangle {
            radius: Theme.radiusControl
            color: primaryControl.enabled
                   ? (primaryControl.down ? Theme.accentPressed : primaryControl.hovered ? Theme.accentHover : Theme.accent)
                   : Theme.bgField
            border.color: primaryControl.enabled ? Theme.accentBorder : Theme.borderSoft
            border.width: 1
        }
    }

    component SecondaryButton: Button {
        id: secondaryControl
        implicitHeight: 40
        font.family: Theme.fontUi
        font.pixelSize: 13
        font.weight: Font.DemiBold

        contentItem: Text {
            text: secondaryControl.text
            color: secondaryControl.enabled ? Theme.textSecondary : Theme.textFaint
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            font: secondaryControl.font
        }

        background: Rectangle {
            radius: Theme.radiusControl
            color: secondaryControl.enabled
                   ? (secondaryControl.down ? Theme.bgField : secondaryControl.hovered ? Theme.bgCardHover : Theme.bgInset)
                   : Theme.bgField
            border.color: secondaryControl.enabled ? Theme.borderStrong : Theme.borderSoft
            border.width: 1
        }
    }

    component DangerButton: Button {
        id: dangerControl
        implicitHeight: 40
        font.family: Theme.fontUi
        font.pixelSize: 13
        font.weight: Font.DemiBold

        contentItem: Text {
            text: dangerControl.text
            color: dangerControl.enabled ? Theme.textPrimary : Theme.textFaint
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            font: dangerControl.font
        }

        background: Rectangle {
            radius: Theme.radiusControl
            color: dangerControl.enabled
                   ? (dangerControl.down ? Qt.darker(Theme.danger, 1.25) : dangerControl.hovered ? "#FF5B5B" : Theme.danger)
                   : Theme.bgField
            border.color: dangerControl.enabled ? "#FF8A8A" : Theme.borderSoft
            border.width: 1
        }
    }

    component StatusPill: Rectangle {
        property string label: ""
        property color tone: Theme.textMuted

        implicitWidth: pillText.implicitWidth + 20
        implicitHeight: 26
        radius: 13
        color: Qt.rgba(tone.r, tone.g, tone.b, 0.12)
        border.color: Qt.rgba(tone.r, tone.g, tone.b, 0.42)
        border.width: 1

        Text {
            id: pillText
            anchors.centerIn: parent
            text: parent.label
            color: parent.tone
            font.pixelSize: 11
            font.weight: Font.DemiBold
        }
    }

    component OrionMark: Rectangle {
        width: 54
        height: 54
        radius: 16
        color: "#0B0F14"
        border.color: Theme.accentBorder
        border.width: 1

        Rectangle {
            anchors.centerIn: parent
            width: 26
            height: 26
            radius: 13
            color: "transparent"
            border.color: Theme.accent
            border.width: 2
        }
        Rectangle {
            anchors.centerIn: parent
            width: 12
            height: 12
            radius: 6
            color: Theme.accent
        }
        Rectangle {
            anchors.centerIn: parent
            width: 5
            height: 5
            radius: 2.5
            color: "#F4F7FA"
        }
    }

    component StarBackdropLocal: Item {
        property real density: 1.0
        property bool interactive: false
        property real constellationRadius: 120
        property real constellationDistance: 72

        Repeater {
            model: Math.round(120 * parent.density)
            Rectangle {
                required property int index
                property int n: index
                width: n % 13 === 0 ? 5 : n % 5 === 0 ? 3 : 2
                height: width
                radius: width / 2
                x: ((n * 73) % Math.max(1, parent.width - 8)) + 4
                y: ((n * 41) % Math.max(1, parent.height - 8)) + 4
                color: n % 17 === 0 ? Theme.accent : "#8EA0BA"
                opacity: n % 17 === 0 ? 0.22 : 0.30
            }
        }

        Canvas {
            anchors.fill: parent
            opacity: 0.20
            onPaint: {
                var ctx = getContext("2d")
                ctx.reset()
                ctx.strokeStyle = Theme.accent
                ctx.lineWidth = 1
                for (var i = 0; i < 8; ++i) {
                    var x = 24 + ((i * 137) % Math.max(1, width - 96))
                    var y = 32 + ((i * 89) % Math.max(1, height - 96))
                    ctx.beginPath()
                    ctx.moveTo(x, y)
                    ctx.lineTo(Math.min(width - 24, x + constellationDistance), Math.min(height - 24, y + constellationRadius / 3))
                    ctx.stroke()
                }
            }
        }
    }

    Rectangle {
        id: shell
        anchors.fill: parent
        radius: 16
        clip: true
        color: Theme.bgShell
        border.color: Theme.borderSoft
        border.width: 1

        StarBackdropLocal {
            anchors.fill: parent
            opacity: 0.78
            interactive: !admin.authenticated
            density: admin.authenticated ? 0.85 : 1.25
            constellationRadius: 120
            constellationDistance: 72
        }

        Rectangle {
            id: titleBar
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 38
            gradient: Gradient {
                orientation: Gradient.Vertical
                GradientStop { position: 0.0; color: "#0A0E15" }
                GradientStop { position: 1.0; color: "#070A10" }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: Theme.hairline
            }

            Row {
                id: brandRow
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                spacing: 9

                Rectangle {
                    width: 18
                    height: 18
                    radius: 4
                    anchors.verticalCenter: parent.verticalCenter
                    gradient: Gradient {
                        orientation: Gradient.Vertical
                        GradientStop { position: 0.0; color: Theme.accent }
                        GradientStop { position: 1.0; color: Qt.darker(Theme.accent, 1.3) }
                    }
                    Text {
                        anchors.centerIn: parent
                        text: "O"
                        color: "#FFFFFF"
                        font.pixelSize: 11
                        font.weight: Font.Bold
                    }
                }
                Text {
                    text: admin.ownerMode ? "Orion Owner" : "Orion Staff"
                    color: Theme.textPrimary
                    font.pixelSize: 13
                    font.weight: Font.DemiBold
                    anchors.verticalCenter: parent.verticalCenter
                }
                StatusPill {
                    label: admin.authenticated ? admin.role : "Locked"
                    tone: admin.authenticated ? Theme.success : Theme.warning
                    anchors.verticalCenter: parent.verticalCenter
                }
            }

            MouseArea {
                anchors.left: brandRow.right
                anchors.right: titleButtons.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                onPressed: window.startSystemMove()
                cursorShape: Qt.SizeAllCursor
            }

            Row {
                id: titleButtons
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 96

                Button {
                    id: minBtn
                    width: 48
                    height: titleBar.height
                    onClicked: window.showMinimized()
                    contentItem: Text {
                        text: "-"
                        color: minBtn.hovered ? "#FFFFFF" : "#D8DEE8"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        font.pixelSize: 16
                    }
                    background: Rectangle { color: minBtn.hovered ? "#1B2433" : "transparent" }
                }

                Button {
                    id: closeBtn
                    width: 48
                    height: titleBar.height
                    onClicked: window.close()
                    contentItem: Text {
                        text: "X"
                        color: closeBtn.hovered ? "#FFFFFF" : "#D8DEE8"
                        horizontalAlignment: Text.AlignHCenter
                        verticalAlignment: Text.AlignVCenter
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                    background: Rectangle { color: closeBtn.hovered ? "#C0392B" : "transparent" }
                }
            }
        }

        Loader {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: titleBar.bottom
            anchors.bottom: parent.bottom
            sourceComponent: admin.authenticated ? dashboardPage : authGatePage
        }
    }

    Component {
        id: authGatePage

        Item {
            id: authRoot

            function submitPrimary() {
                if (admin.busy)
                    return
                if (admin.ownerMode)
                    admin.ownerLogin(ownerSecret.text)
                else
                    admin.staffLogin(staffDiscord.text)
            }

            Card {
                id: authCard
                width: 492
                height: authColumn.implicitHeight + 54
                anchors.centerIn: parent
                color: "#0B1019"
                border.color: Theme.borderStrong

                Rectangle {
                    anchors.fill: parent
                    anchors.margins: -1
                    radius: parent.radius + 1
                    color: "transparent"
                    border.color: Theme.accentGlow
                    border.width: 1
                }

                ColumnLayout {
                    id: authColumn
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 27
                    spacing: 17

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 14

                        OrionMark {}

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: "ORION"
                                color: Theme.textPrimary
                                font.pixelSize: 27
                                font.weight: Font.DemiBold
                            }
                            Text {
                                text: admin.ownerMode ? "Owner authentication required" : "Staff authentication required"
                                color: Theme.textMuted
                                font.pixelSize: 12
                            }
                        }

                        StatusPill {
                            label: admin.securityLockActive ? "Security Lock" : "Online"
                            tone: admin.securityLockActive ? Theme.danger : Theme.success
                            Layout.alignment: Qt.AlignTop
                        }
                    }

                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 4
                        Text {
                            text: admin.ownerMode ? "Owner console locked" : "Staff console locked"
                            color: Theme.textPrimary
                            font.pixelSize: 19
                            font.weight: Font.DemiBold
                        }
                        Text {
                            Layout.fillWidth: true
                            text: admin.ownerMode
                                  ? "Enter the owner secret to unlock privileged license and staff controls."
                                  : "Log in with your registered Discord ID, or enroll once with an owner-issued key."
                            color: Theme.textMuted
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                        }
                    }

                    FieldBox {
                        id: ownerSecret
                        visible: admin.ownerMode
                        placeholderText: "Owner secret"
                        echoMode: TextInput.Password
                        iconText: "#"
                        onAccepted: authRoot.submitPrimary()
                        Component.onCompleted: if (admin.ownerMode) input.forceActiveFocus()
                    }

                    FieldBox {
                        id: staffDiscord
                        visible: !admin.ownerMode
                        placeholderText: "Discord ID"
                        iconText: "@"
                        onAccepted: authRoot.submitPrimary()
                        Component.onCompleted: if (!admin.ownerMode) input.forceActiveFocus()
                    }

                    FieldBox {
                        id: staffEnrollKey
                        visible: !admin.ownerMode
                        placeholderText: "Enrollment key"
                        echoMode: TextInput.Password
                        iconText: "#"
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10

                        PrimaryButton {
                            Layout.fillWidth: true
                            text: admin.busy ? "Verifying..." : admin.ownerMode ? "Unlock" : "Login"
                            enabled: !admin.busy
                            onClicked: authRoot.submitPrimary()
                        }

                        SecondaryButton {
                            visible: !admin.ownerMode
                            Layout.fillWidth: true
                            text: "Enroll"
                            enabled: !admin.busy
                            onClicked: admin.staffEnroll(staffDiscord.text, staffEnrollKey.text)
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        text: admin.statusMessage
                        visible: text.length > 0
                        color: admin.securityLockActive ? Theme.danger : admin.busy ? Theme.textMuted : Theme.warning
                        font.pixelSize: 12
                        wrapMode: Text.WordWrap
                        horizontalAlignment: Text.AlignHCenter
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 44
                        radius: Theme.radiusControl
                        color: Theme.bgInset
                        border.color: Theme.borderSoft
                        border.width: 1

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 13
                            anchors.rightMargin: 13
                            spacing: 8
                            Text {
                                Layout.fillWidth: true
                                text: admin.securityState
                                color: admin.securityLockActive ? Theme.danger : Theme.textFaint
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                            Text {
                                text: "Machine " + admin.machineIdSuffix
                                color: Theme.textFaint
                                font.pixelSize: 11
                                font.family: Theme.fontMono
                            }
                        }
                    }
                }
            }
        }
    }

    Component {
        id: dashboardPage

        Item {
            Component.onCompleted: if (admin.ownerMode) admin.refreshKillSwitch()

            ColumnLayout {
                anchors.fill: parent
                anchors.margins: 16
                spacing: 12

                RowLayout {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 112
                    spacing: 12

                    Card {
                        Layout.fillWidth: true
                        Layout.fillHeight: true

                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 16
                            spacing: 14

                            OrionMark {}

                            ColumnLayout {
                                Layout.fillWidth: true
                                spacing: 4
                                Text {
                                    text: admin.ownerMode ? "Owner Console" : "Staff Console"
                                    color: Theme.textPrimary
                                    font.pixelSize: 22
                                    font.weight: Font.DemiBold
                                }
                                Text {
                                    text: admin.ownerMode
                                          ? "Privileged staff registration, license recovery, and emergency controls."
                                          : "Role-limited license support tools bound to this machine."
                                    color: Theme.textMuted
                                    font.pixelSize: 12
                                }
                            }

                            StatusPill {
                                label: admin.securityLockActive ? "Locked" : "Authenticated"
                                tone: admin.securityLockActive ? Theme.danger : Theme.success
                            }
                            StatusPill {
                                label: admin.role
                                tone: Theme.accent
                            }
                        }
                    }

                    Card {
                        Layout.preferredWidth: 300
                        Layout.fillHeight: true

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 6
                            Text {
                                text: admin.staffName.length ? admin.staffName : (admin.ownerMode ? "Owner" : "Staff")
                                color: Theme.textPrimary
                                font.pixelSize: 16
                                font.weight: Font.DemiBold
                                elide: Text.ElideRight
                            }
                            Text {
                                text: "Machine " + admin.machineIdSuffix
                                color: Theme.textMuted
                                font.pixelSize: 11
                                font.family: Theme.fontMono
                            }
                            RowLayout {
                                Layout.fillWidth: true
                                SecondaryButton {
                                    Layout.fillWidth: true
                                    text: "Check Update"
                                    enabled: !admin.busy
                                    onClicked: admin.checkUpdate()
                                }
                                PrimaryButton {
                                    Layout.fillWidth: true
                                    text: "Apply"
                                    enabled: admin.updateAvailable && !admin.busy
                                    onClicked: admin.startUpdate()
                                }
                            }
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 12

                    ColumnLayout {
                        Layout.preferredWidth: 376
                        Layout.maximumWidth: 398
                        Layout.fillHeight: true
                        spacing: 12

                        Card {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 136
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 13
                                spacing: 6
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        Layout.fillWidth: true
                                        text: "Session"
                                        color: Theme.textPrimary
                                        font.pixelSize: 15
                                        font.weight: Font.DemiBold
                                    }
                                    DangerButton {
                                        text: "Lock"
                                        enabled: admin.authenticated && !admin.busy
                                        onClicked: admin.logout()
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: admin.statusMessage
                                    color: admin.securityLockActive ? Theme.danger : Theme.textSecondary
                                    wrapMode: Text.WordWrap
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: admin.securityState
                                    color: admin.securityLockActive ? Theme.danger : Theme.textMuted
                                    wrapMode: Text.WordWrap
                                    font.pixelSize: 11
                                }
                                Text {
                                    text: admin.updateState + (admin.latestVersion.length ? (" (" + admin.latestVersion + ")") : "")
                                    color: admin.updateAvailable ? Theme.warning : Theme.textMuted
                                    font.pixelSize: 11
                                }
                            }
                        }

                        Card {
                            visible: admin.ownerMode
                            Layout.fillWidth: true
                            Layout.preferredHeight: 176
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 13
                                spacing: 8
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        Layout.fillWidth: true
                                        text: "Killswitch"
                                        color: Theme.textPrimary
                                        font.pixelSize: 15
                                        font.weight: Font.DemiBold
                                    }
                                    StatusPill {
                                        label: admin.killSwitchEngaged ? "Engaged" : "Released"
                                        tone: admin.killSwitchEngaged ? Theme.danger : Theme.success
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    visible: admin.killSwitchReason.length > 0
                                    text: admin.killSwitchReason
                                    color: Theme.textMuted
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                }
                                FieldBox { id: killswitchReasonField; placeholderText: "Reason"; iconText: "R" }
                                RowLayout {
                                    Layout.fillWidth: true
                                    DangerButton {
                                        Layout.fillWidth: true
                                        text: "Engage"
                                        enabled: window.canAct
                                        onClicked: admin.engageKillSwitch(killswitchReasonField.text)
                                    }
                                    PrimaryButton {
                                        Layout.fillWidth: true
                                        text: "Release"
                                        enabled: window.canAct
                                        onClicked: admin.releaseKillSwitch()
                                    }
                                    SecondaryButton {
                                        Layout.fillWidth: true
                                        text: "Refresh"
                                        enabled: window.canAct
                                        onClicked: admin.refreshKillSwitch()
                                    }
                                }
                            }
                        }

                        Card {
                            visible: admin.ownerMode
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 320
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 8
                                Text {
                                    text: "Staff"
                                    color: Theme.textPrimary
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                }
                                ScrollView {
                                    id: staffScroll
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true
                                    contentWidth: availableWidth

                                    ColumnLayout {
                                        width: staffScroll.availableWidth
                                        spacing: 8

                                        FieldBox { id: newStaffDiscord; placeholderText: "Discord ID"; iconText: "@" }
                                        FieldBox { id: newStaffName; placeholderText: "Name"; iconText: "N" }
                                        RowLayout {
                                            Layout.fillWidth: true
                                            DarkComboBox {
                                                id: newStaffRole
                                                Layout.fillWidth: true
                                                model: ["support", "admin", "owner"]
                                            }
                                            PrimaryButton {
                                                Layout.preferredWidth: 102
                                                text: "Create"
                                                enabled: window.canAct
                                                onClicked: admin.createStaff(newStaffDiscord.text, newStaffName.text, newStaffRole.currentText)
                                            }
                                        }
                                        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.hairline }
                                        FieldBox { id: staffTarget; placeholderText: "Staff ID or Discord ID"; iconText: "S" }
                                        FieldBox { id: staffReason; placeholderText: "Reason"; iconText: "R" }
                                        GridLayout {
                                            Layout.fillWidth: true
                                            columns: 2
                                            columnSpacing: 8
                                            rowSpacing: 8
                                            DangerButton {
                                                Layout.fillWidth: true
                                                text: "Disable"
                                                enabled: window.canAct
                                                onClicked: admin.disableStaff(staffTarget.text, staffReason.text)
                                            }
                                            PrimaryButton {
                                                Layout.fillWidth: true
                                                text: "Reset Machine"
                                                enabled: window.canAct
                                                onClicked: admin.resetStaffMachine(staffTarget.text, staffReason.text)
                                            }
                                            SecondaryButton {
                                                Layout.columnSpan: 2
                                                Layout.fillWidth: true
                                                text: "Reissue Enrollment Key"
                                                enabled: window.canAct
                                                onClicked: admin.reissueStaffEnrollment(staffTarget.text, staffReason.text)
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        Item { Layout.fillHeight: true; visible: !admin.ownerMode }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        spacing: 12

                        Card {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 216
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 8
                                Text {
                                    text: "License"
                                    color: Theme.textPrimary
                                    font.pixelSize: 15
                                    font.weight: Font.DemiBold
                                }
                                FieldBox { id: licenseKey; placeholderText: "License key"; iconText: "K" }
                                FieldBox { id: licenseReason; placeholderText: "Reason"; iconText: "R" }
                                RowLayout {
                                    Layout.fillWidth: true
                                    PrimaryButton {
                                        Layout.fillWidth: true
                                        text: "Lookup"
                                        enabled: window.canAct
                                        onClicked: admin.lookupLicense(licenseKey.text)
                                    }
                                    SecondaryButton {
                                        Layout.fillWidth: true
                                        text: "Reset HWID"
                                        enabled: window.canAct
                                        onClicked: admin.resetLicenseHwid(licenseKey.text, licenseReason.text)
                                    }
                                    DangerButton {
                                        Layout.fillWidth: true
                                        text: "Deactivate"
                                        enabled: window.canAct
                                        onClicked: admin.deactivateLicense(licenseKey.text, licenseReason.text)
                                    }
                                }
                            }
                        }

                        Card {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.minimumHeight: 240
                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 14
                                spacing: 8
                                RowLayout {
                                    Layout.fillWidth: true
                                    Text {
                                        Layout.fillWidth: true
                                        text: "Result"
                                        color: Theme.textPrimary
                                        font.pixelSize: 15
                                        font.weight: Font.DemiBold
                                    }
                                    SecondaryButton {
                                        text: "Clear"
                                        enabled: admin.resultText.length > 0
                                        onClicked: admin.clearResult()
                                    }
                                }
                                ScrollView {
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    TextArea {
                                        text: admin.resultText
                                        readOnly: true
                                        wrapMode: TextEdit.Wrap
                                        color: Theme.textSecondary
                                        font.family: Theme.fontMono
                                        font.pixelSize: 11
                                        background: Rectangle {
                                            color: Theme.bgField
                                            border.color: Theme.borderSoft
                                            radius: Theme.radiusControl
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
