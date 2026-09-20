import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Labelled single-line text field with the admin tool's dark styling.
ColumnLayout {
    id: root

    property string label: ""
    property string iconText: ""
    property string hint: ""
    property alias text: input.text
    property alias placeholderText: input.placeholderText
    property alias echoMode: input.echoMode
    property alias readOnly: input.readOnly
    property alias maximumLength: input.maximumLength
    property alias validator: input.validator
    property alias inputMethodHints: input.inputMethodHints
    property alias input: input
    property bool compact: false
    signal accepted()

    Layout.fillWidth: true
    spacing: 4

    Text {
        visible: root.label.length > 0
        text: root.label
        color: Theme.textMuted
        font.pixelSize: Theme.fontCaption
        font.weight: Font.DemiBold
    }

    Rectangle {
        Layout.fillWidth: true
        Layout.preferredHeight: root.compact ? 34 : 40
        radius: Theme.radiusControl
        color: input.readOnly ? Theme.bgInset : Theme.bgField
        border.color: input.activeFocus ? Theme.accentBorder : Theme.borderSoft
        border.width: input.activeFocus ? 2 : 1

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: 12
            anchors.rightMargin: 12
            spacing: 8

            Text {
                visible: root.iconText.length > 0
                text: root.iconText
                color: input.activeFocus ? Theme.accentBorder : Theme.textFaint
                font.pixelSize: 12
                font.weight: Font.DemiBold
                font.family: Theme.fontMono
                Layout.alignment: Qt.AlignVCenter
            }

            TextField {
                id: input
                Layout.fillWidth: true
                Layout.fillHeight: true
                verticalAlignment: TextInput.AlignVCenter
                selectByMouse: true
                color: readOnly ? Theme.textSecondary : Theme.textPrimary
                placeholderTextColor: Theme.textFaint
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontBody
                background: null
                Keys.onReturnPressed: root.accepted()
                Keys.onEnterPressed: root.accepted()
            }
        }
    }

    Text {
        visible: root.hint.length > 0
        Layout.fillWidth: true
        text: root.hint
        color: Theme.textFaint
        font.pixelSize: Theme.fontMicro
        wrapMode: Text.WordWrap
    }
}
