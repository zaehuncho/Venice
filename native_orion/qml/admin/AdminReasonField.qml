import QtQuick
import QtQuick.Layouts

// Every mutation in the contract requires a reason (<= 200 chars). Buttons bind
// `enabled` to `valid` so nothing fires without one.
ColumnLayout {
    id: root
    property alias text: field.text
    property string label: "Reason (required)"
    property string placeholderText: "Why this change is being made"
    readonly property string value: field.text.trim()
    readonly property bool valid: admin.reasonValid(field.text)
    readonly property int maxChars: 200

    Layout.fillWidth: true
    spacing: 2

    AdminField {
        id: field
        label: root.label
        iconText: "R"
        maximumLength: root.maxChars
        placeholderText: root.placeholderText
    }

    Text {
        Layout.alignment: Qt.AlignRight
        text: field.text.length + "/" + root.maxChars
        color: root.valid ? Theme.textFaint : field.text.length === 0 ? Theme.textFaint : Theme.warning
        font.pixelSize: Theme.fontMicro
        font.family: Theme.fontMono
    }
}
