import QtQuick

Rectangle {
    id: root
    property string label: ""
    property color tone: Theme.textMuted

    implicitWidth: pillText.implicitWidth + 18
    implicitHeight: 24
    radius: 12
    color: Qt.rgba(tone.r, tone.g, tone.b, 0.12)
    border.color: Qt.rgba(tone.r, tone.g, tone.b, 0.42)
    border.width: 1

    Text {
        id: pillText
        anchors.centerIn: parent
        text: root.label
        color: root.tone
        font.pixelSize: Theme.fontCaption
        font.weight: Font.DemiBold
    }
}
