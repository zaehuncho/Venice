import QtQuick
import QtQuick.Layouts
import OrionNative

Rectangle {
    id: root
    property string title: ""
    property string value: "-"
    property string detail: ""
    property string tone: "neutral"

    readonly property color toneColor: tone === "success" ? Theme.success
                                       : tone === "warning" ? Theme.warning
                                       : tone === "danger" ? Theme.danger
                                       : Theme.borderSoft

    radius: Theme.radiusCard
    color: Theme.bgSidebar
    border.color: tone === "neutral" ? Theme.borderSoft : Qt.rgba(toneColor.r, toneColor.g, toneColor.b, 0.45)
    border.width: 1
    Behavior on border.color { ColorAnimation { duration: Theme.motionSlow } }

    Rectangle {
        visible: root.tone !== "neutral"
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 10
        anchors.bottomMargin: 10
        width: 3
        radius: 1.5
        color: root.toneColor
        Behavior on color { ColorAnimation { duration: Theme.motionSlow } }
    }

    Column {
        anchors.fill: parent
        anchors.margins: 14
        spacing: 6

        Text {
            text: root.title
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            width: parent.width
        }
        Text {
            text: root.value
            color: Theme.textPrimary
            font.family: Theme.fontUi
            font.pixelSize: 24
            font.weight: Font.DemiBold
            elide: Text.ElideRight
            width: parent.width
        }
        Text {
            text: root.detail
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 12
            elide: Text.ElideRight
            width: parent.width
        }
    }
}
