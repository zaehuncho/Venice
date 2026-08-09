import QtQuick
import QtQuick.Layouts
import OrionNative

Rectangle {
    id: root
    property string title: Theme.productMark
    property string subtitle: Theme.productTagline
    radius: Theme.radiusCard
    color: Theme.bgSidebar
    border.color: Theme.borderSoft
    border.width: 1
    clip: true

    VeniceBackdrop {
        anchors.fill: parent
        opacity: 0.8
    }

    Rectangle {
        anchors.fill: parent
        color: "transparent"
        border.color: Theme.accentFaint
        border.width: 1
        radius: parent.radius
    }

    RowLayout {
        anchors.centerIn: parent
        spacing: 14

        Rectangle {
            width: 58
            height: 58
            radius: 18
            // Borderless + transparent so the star blends into the banner's
            // starfield instead of sitting in a bordered tile.
            color: "transparent"
            border.width: 0

            Image {
                id: iconImage
                anchors.centerIn: parent
                source: orion.iconSource
                width: 44
                height: 44
                sourceSize.width: 128
                sourceSize.height: 128
                fillMode: Image.PreserveAspectFit
                smooth: true
                mipmap: true
                visible: status === Image.Ready
            }
            Text {
                anchors.centerIn: parent
                text: "V"
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: 25
                font.weight: Font.DemiBold
                visible: iconImage.status !== Image.Ready
            }
        }

        ColumnLayout {
            spacing: 2
            Text {
                text: root.title
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: 28
                font.weight: Font.DemiBold
                font.letterSpacing: 1.5
            }
            Text {
                text: root.subtitle
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 13
            }
        }
    }
}
