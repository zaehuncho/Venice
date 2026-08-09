import QtQuick
import QtQuick.Controls
import OrionNative

Item {
    id: root
    property string text: ""
    width: 16
    height: 16

    Rectangle {
        anchors.centerIn: parent
        width: 14
        height: 14
        radius: 7
        color: Theme.modalSurface
        border.color: Theme.accent
        border.width: 1

        Text {
            anchors.centerIn: parent
            text: "i"
            color: Theme.accentBorder
            font.family: Theme.fontUi
            font.pixelSize: 10
            font.weight: Font.DemiBold
        }
    }

    MouseArea {
        id: hover
        anchors.fill: parent
        hoverEnabled: true
        cursorShape: Qt.WhatsThisCursor
    }

    ToolTip {
        id: tip
        visible: hover.containsMouse && root.text.length > 0
        delay: 260
        timeout: 8000
        text: root.text
        y: root.height + 8
        padding: 0

        contentItem: Text {
            text: tip.text
            color: Theme.textPrimary
            font.family: Theme.fontUi
            font.pixelSize: 12
            wrapMode: Text.WordWrap
            lineHeight: 1.12
            width: Math.min(320, implicitWidth)
            leftPadding: 11
            rightPadding: 11
            topPadding: 8
            bottomPadding: 9
        }

        background: Rectangle {
            radius: 8
            color: Theme.modalSurface
            border.color: Theme.borderStrong
            border.width: 1
        }

        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 120; easing.type: Easing.OutCubic }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 90; easing.type: Easing.InCubic }
        }
    }
}
