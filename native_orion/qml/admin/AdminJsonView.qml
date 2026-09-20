import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// Read-only monospace JSON/text viewer.
Rectangle {
    id: root
    property string text: ""
    property int minimumHeight: 120

    Layout.fillWidth: true
    implicitHeight: Math.max(minimumHeight, Math.min(360, area.implicitHeight + 16))
    radius: Theme.radiusControl
    color: Theme.bgField
    border.color: Theme.borderSoft
    border.width: 1
    clip: true

    ScrollView {
        anchors.fill: parent
        anchors.margins: 4
        contentWidth: availableWidth
        TextArea {
            id: area
            text: root.text
            readOnly: true
            selectByMouse: true
            wrapMode: TextEdit.Wrap
            color: Theme.textSecondary
            font.family: Theme.fontMono
            font.pixelSize: Theme.fontCaption
            background: null
        }
    }
}
