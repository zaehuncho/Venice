import QtQuick
import QtQuick.Controls

ComboBox {
    id: combo

    implicitHeight: 38
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontBody
    leftPadding: 12
    rightPadding: 32

    contentItem: Text {
        leftPadding: 12
        rightPadding: 32
        text: combo.displayText
        color: combo.enabled ? Theme.textPrimary : Theme.textFaint
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        font: combo.font
    }

    indicator: Text {
        anchors.right: parent.right
        anchors.rightMargin: 12
        anchors.verticalCenter: parent.verticalCenter
        text: "v"
        color: combo.enabled ? Theme.textMuted : Theme.textFaint
        font.pixelSize: 11
        font.weight: Font.DemiBold
    }

    background: Rectangle {
        radius: Theme.radiusControl
        color: combo.enabled ? Theme.bgField : Theme.bgInset
        border.color: combo.activeFocus || combo.popup.visible ? Theme.accentBorder : Theme.borderSoft
        border.width: combo.activeFocus || combo.popup.visible ? 2 : 1
    }

    delegate: ItemDelegate {
        id: comboDelegate
        required property var modelData
        required property int index

        width: combo.width - 8
        height: 32
        highlighted: combo.highlightedIndex === index

        contentItem: Text {
            text: String(comboDelegate.modelData)
            color: comboDelegate.highlighted ? Theme.textPrimary : Theme.textSecondary
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontBody
        }

        background: Rectangle {
            radius: 8
            color: comboDelegate.highlighted ? Theme.accentSoft : "transparent"
        }
    }

    popup: Popup {
        y: combo.height + 4
        width: combo.width
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 220)
        padding: 4

        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: combo.popup.visible ? combo.delegateModel : null
            currentIndex: combo.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds
        }

        background: Rectangle {
            color: Theme.bgInset
            radius: Theme.radiusControl
            border.color: Theme.borderStrong
            border.width: 1
        }
    }
}
