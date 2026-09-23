import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Shell for OrionOwner.exe / OrionStaff.exe (contract §7). The window owns the
// chrome, the sidebar, and the status strip; every screen lives in its own file
// and talks to the `admin` context property (AdminToolController).
ApplicationWindow {
    id: window

    width: 1280
    height: 840
    minimumWidth: 1080
    minimumHeight: 700
    visible: true
    title: admin.ownerMode ? "Venice Owner" : "Venice Staff"
    flags: Qt.Window | Qt.FramelessWindowHint
    color: "transparent"
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontBody

    // Owner = the Venice blue the launcher and site use; staff keeps violet so the two consoles
    // are never mistaken for each other at a glance.
    readonly property color toolAccent: admin.ownerMode ? Theme.brandAccent : "#7C3AED"
    readonly property bool canAct: admin.authenticated && !admin.busy && !admin.securityLockActive
    property string page: admin.ownerMode ? "dashboard" : "licenses"
    property bool showRaw: false

    Component.onCompleted: Theme.accent = toolAccent

    // Owner and staff tools share this shell; the nav is the only split.
    readonly property var navItems: admin.ownerMode
        ? [
            { key: "dashboard", label: "Dashboard", glyph: "D" },
            { key: "licenses", label: "Licenses", glyph: "L" },
            { key: "staff", label: "Staff", glyph: "S" },
            { key: "audit", label: "Audit", glyph: "A" },
            { key: "config", label: "Config", glyph: "C" }
          ]
        : [
            { key: "licenses", label: "Licenses", glyph: "L" },
            { key: "audit", label: "My audit", glyph: "A" },
            { key: "account", label: "My access", glyph: "M" }
          ]

    function pageIndex(key) {
        switch (key) {
        case "dashboard": return 0
        case "licenses": return 1
        case "staff": return 2
        case "audit": return 3
        case "config": return 4
        case "account": return 5
        }
        return 1
    }

    Rectangle {
        id: shell
        anchors.fill: parent
        radius: 16
        clip: true
        color: Theme.bgShell
        border.color: Theme.borderSoft
        border.width: 1

        // ---- title bar -------------------------------------------------
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

                AdminBrandMark {
                    size: 20
                    anchors.verticalCenter: parent.verticalCenter
                }
                Text {
                    text: Theme.productName
                    color: Theme.textPrimary
                    font.pixelSize: 13
                    font.weight: Font.Bold
                    font.letterSpacing: -0.2
                    anchors.verticalCenter: parent.verticalCenter
                }
                AdminPill {
                    anchors.verticalCenter: parent.verticalCenter
                    label: admin.ownerMode ? "Owner" : "Staff"
                    tone: Theme.accent
                }
                AdminPill {
                    anchors.verticalCenter: parent.verticalCenter
                    label: admin.authenticated ? (admin.role.length > 0 ? admin.role : "signed in") : "locked"
                    tone: admin.authenticated ? Theme.success : Theme.warning
                }
                AdminPill {
                    anchors.verticalCenter: parent.verticalCenter
                    visible: admin.authenticated && admin.ownerRoutes
                    label: "owner routes"
                    tone: Theme.accent
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

        // ---- login gate ------------------------------------------------
        AdminLoginPane {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: titleBar.bottom
            anchors.bottom: parent.bottom
            visible: !admin.authenticated
        }

        // ---- authenticated body ---------------------------------------
        Item {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: titleBar.bottom
            anchors.bottom: parent.bottom
            visible: admin.authenticated

            Rectangle {
                id: sidebar
                anchors.left: parent.left
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                width: 196
                color: Theme.bgSidebar
                border.color: "transparent"

                Rectangle {
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.bottom: parent.bottom
                    width: 1
                    color: Theme.hairline
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 6

                    Text {
                        text: "SIGNED IN AS"
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontMicro
                        font.weight: Font.Bold
                        font.letterSpacing: 1.2
                    }
                    Text {
                        Layout.fillWidth: true
                        text: admin.staffName.length > 0 ? admin.staffName : (admin.ownerMode ? "Owner" : "Staff")
                        color: Theme.textPrimary
                        font.pixelSize: Theme.fontTitle
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }
                    Text {
                        Layout.fillWidth: true
                        text: admin.staffId.length > 0 ? admin.staffId : "break-glass session"
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontMicro
                        font.family: Theme.fontMono
                        elide: Text.ElideMiddle
                    }

                    Item { Layout.preferredHeight: 8 }

                    Repeater {
                        model: window.navItems
                        delegate: Rectangle {
                            id: navRow
                            required property var modelData
                            readonly property bool current: window.page === modelData.key

                            Layout.fillWidth: true
                            Layout.preferredHeight: 40
                            radius: Theme.radiusControl
                            color: current ? Theme.accentSoft : navMouse.containsMouse ? Theme.bgCardHover : "transparent"
                            border.color: current ? Theme.accentBorder : "transparent"
                            border.width: 1

                            Rectangle {
                                anchors.left: parent.left
                                anchors.verticalCenter: parent.verticalCenter
                                width: 3
                                height: 18
                                radius: 1.5
                                color: Theme.accent
                                visible: navRow.current
                            }

                            RowLayout {
                                anchors.fill: parent
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                spacing: 10
                                Rectangle {
                                    Layout.preferredWidth: 26
                                    Layout.preferredHeight: 26
                                    radius: 7
                                    color: navRow.current ? Theme.accent : Theme.iconTileBg
                                    border.color: navRow.current ? Theme.accentBorder : Theme.borderSoft
                                    border.width: 1
                                    Text {
                                        anchors.centerIn: parent
                                        text: navRow.modelData.glyph
                                        color: navRow.current ? Theme.textOnAccent : Theme.textMuted
                                        font.pixelSize: Theme.fontCaption
                                        font.weight: Font.Bold
                                    }
                                }
                                Text {
                                    Layout.fillWidth: true
                                    text: navRow.modelData.label
                                    color: navRow.current ? Theme.textPrimary : Theme.textSecondary
                                    font.pixelSize: Theme.fontBody
                                    elide: Text.ElideRight
                                }
                            }

                            MouseArea {
                                id: navMouse
                                anchors.fill: parent
                                hoverEnabled: true
                                cursorShape: Qt.PointingHandCursor
                                onClicked: window.page = navRow.modelData.key
                            }
                        }
                    }

                    Item { Layout.fillHeight: true }

                    AdminPill {
                        Layout.alignment: Qt.AlignLeft
                        visible: admin.killSwitchEngaged
                        label: "KILL SWITCH ON"
                        tone: Theme.danger
                    }

                    Text {
                        Layout.fillWidth: true
                        text: admin.securityState
                        color: admin.securityLockActive ? Theme.danger : Theme.textFaint
                        font.pixelSize: Theme.fontMicro
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "machine " + admin.machineIdSuffix
                        color: Theme.textFaint
                        font.pixelSize: Theme.fontMicro
                        font.family: Theme.fontMono
                        elide: Text.ElideMiddle
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "v" + admin.appVersion + "  " + admin.updateState
                        color: admin.updateAvailable ? Theme.warning : Theme.textFaint
                        font.pixelSize: Theme.fontMicro
                        elide: Text.ElideRight
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        AdminButton {
                            Layout.fillWidth: true
                            kind: "ghost"
                            compact: true
                            text: admin.updateAvailable ? "Update" : "Check"
                            onClicked: admin.updateAvailable ? admin.startUpdate() : admin.checkUpdate()
                        }
                        AdminButton {
                            Layout.fillWidth: true
                            kind: "secondary"
                            compact: true
                            text: "Sign out"
                            onClicked: admin.logout()
                        }
                    }
                }
            }

            ColumnLayout {
                anchors.left: sidebar.right
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.bottom: parent.bottom
                anchors.margins: 14
                spacing: 10

                StackLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    currentIndex: window.pageIndex(window.page)

                    AdminDashboardPage {}
                    AdminLicensesPage {}
                    AdminStaffPage {}
                    AdminAuditPage {}
                    AdminConfigPage {}
                    AdminAccountPage {}
                }

                // ---- status strip -----------------------------------
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: statusColumn.implicitHeight + 16
                    radius: Theme.radiusControl
                    color: Theme.bgCard
                    border.color: admin.statusIsError ? Theme.dangerBorder : Theme.borderSoft
                    border.width: 1

                    ColumnLayout {
                        id: statusColumn
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                Layout.preferredWidth: 8
                                Layout.preferredHeight: 8
                                radius: 4
                                color: admin.busy ? Theme.warning : admin.statusIsError ? Theme.danger : Theme.success
                            }
                            Text {
                                Layout.fillWidth: true
                                text: admin.busy
                                      ? "Working..."
                                      : (admin.statusMessage.length > 0 ? admin.statusMessage : "Ready.")
                                color: admin.statusIsError ? Theme.logErr : Theme.textSecondary
                                font.pixelSize: Theme.fontSmall
                                elide: Text.ElideRight
                            }
                            AdminButton {
                                kind: "ghost"
                                compact: true
                                text: window.showRaw ? "Hide response" : "Raw response"
                                onClicked: window.showRaw = !window.showRaw
                            }
                            AdminButton {
                                kind: "ghost"
                                compact: true
                                visible: admin.resultText.length > 0
                                text: "Clear"
                                onClicked: admin.clearResult()
                            }
                        }

                        AdminJsonView {
                            visible: window.showRaw
                            minimumHeight: 150
                            text: admin.resultText.length > 0 ? admin.resultText : "(no response yet)"
                        }
                    }
                }
            }
        }
    }
}
