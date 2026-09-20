import QtQuick
import QtQuick.Layouts

// Dashboard stat tile: eyebrow label, big value, optional sub-line.
Rectangle {
    id: root
    property string label: ""
    property string value: "-"
    property string sub: ""
    property color tone: Theme.textPrimary

    Layout.fillWidth: true
    Layout.preferredHeight: 84
    radius: Theme.radiusControl
    color: Theme.bgInset
    border.color: Theme.borderSoft
    border.width: 1

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 2
        Text {
            text: root.label.toUpperCase()
            color: Theme.textFaint
            font.pixelSize: Theme.fontMicro
            font.weight: Font.DemiBold
            font.letterSpacing: 0.6
        }
        Text {
            text: root.value
            color: root.tone
            font.pixelSize: 24
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
        Text {
            visible: root.sub.length > 0
            text: root.sub
            color: Theme.textMuted
            font.pixelSize: Theme.fontCaption
            elide: Text.ElideRight
            Layout.fillWidth: true
        }
    }
}
