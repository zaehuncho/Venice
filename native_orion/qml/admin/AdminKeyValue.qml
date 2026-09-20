import QtQuick
import QtQuick.Layouts

// Label / value detail row. `copyable` adds a Copy button for the full value.
RowLayout {
    id: root
    property string key: ""
    property string value: ""
    property bool mono: false
    property bool copyable: false
    property color valueColor: Theme.textPrimary
    property int keyWidth: 130

    Layout.fillWidth: true
    spacing: 8

    Text {
        Layout.preferredWidth: root.keyWidth
        text: root.key
        color: Theme.textMuted
        font.pixelSize: Theme.fontCaption
        font.weight: Font.DemiBold
        elide: Text.ElideRight
    }
    Text {
        Layout.fillWidth: true
        text: root.value.length > 0 ? root.value : "-"
        color: root.value.length > 0 ? root.valueColor : Theme.textFaint
        font.pixelSize: Theme.fontSmall
        font.family: root.mono ? Theme.fontMono : Theme.fontUi
        elide: Text.ElideMiddle
        wrapMode: Text.NoWrap
    }
    AdminButton {
        visible: root.copyable && root.value.length > 0
        kind: "ghost"
        compact: true
        text: "Copy"
        onClicked: admin.copyToClipboard(root.value)
    }
}
