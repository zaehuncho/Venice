import QtQuick
import QtQuick.Layouts

// One-time secret display (enroll keys, created license keys, TOTP secret,
// rotated admin secret). Each entry has a Copy button; Dismiss clears it from
// the controller so it cannot be re-read later in the session.
Rectangle {
    id: root

    property string title: "Shown once"
    property string message: "Copy this now. It is not stored and will not be shown again."
    // [{label: "", value: ""}]
    property var entries: []
    property string copyAllText: ""
    signal dismissed()

    Layout.fillWidth: true
    implicitHeight: column.implicitHeight + 28
    radius: Theme.radiusCard
    color: Theme.warningDim
    border.color: Theme.warningBorder
    border.width: 1

    ColumnLayout {
        id: column
        anchors.fill: parent
        anchors.margins: 14
        spacing: 8

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                Layout.fillWidth: true
                text: root.title
                color: Theme.warning
                font.pixelSize: Theme.fontTitle
                font.weight: Font.DemiBold
            }
            AdminButton {
                visible: root.copyAllText.length > 0
                kind: "secondary"
                compact: true
                text: "Copy all"
                onClicked: admin.copyToClipboard(root.copyAllText)
            }
            AdminButton {
                kind: "ghost"
                compact: true
                text: "Dismiss"
                onClicked: root.dismissed()
            }
        }

        Text {
            Layout.fillWidth: true
            text: root.message
            color: Theme.textSecondary
            font.pixelSize: Theme.fontSmall
            wrapMode: Text.WordWrap
        }

        Repeater {
            model: root.entries
            delegate: RowLayout {
                required property var modelData
                Layout.fillWidth: true
                spacing: 8
                Text {
                    visible: String(modelData.label || "").length > 0
                    Layout.preferredWidth: 110
                    text: String(modelData.label || "")
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontCaption
                    font.weight: Font.DemiBold
                    elide: Text.ElideRight
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 34
                    radius: Theme.radiusControl
                    color: Theme.bgField
                    border.color: Theme.borderSoft
                    border.width: 1
                    TextInput {
                        anchors.fill: parent
                        anchors.leftMargin: 10
                        anchors.rightMargin: 10
                        verticalAlignment: TextInput.AlignVCenter
                        text: String(modelData.value || "")
                        readOnly: true
                        selectByMouse: true
                        color: Theme.textPrimary
                        font.family: Theme.fontMono
                        font.pixelSize: Theme.fontSmall
                        clip: true
                    }
                }
                AdminButton {
                    kind: "primary"
                    compact: true
                    text: "Copy"
                    onClicked: admin.copyToClipboard(String(modelData.value || ""))
                }
            }
        }
    }
}
