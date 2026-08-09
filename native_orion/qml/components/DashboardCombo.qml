import QtQuick
import QtQuick.Controls
import OrionNative

ComboBox {
    id: control
    property string value: ""
    implicitHeight: 36
    implicitWidth: 180
    hoverEnabled: true

    function valueIndex() {
        for (var i = 0; i < control.count; ++i) {
            if (control.textAt(i) === control.value)
                return i
        }
        return control.count > 0 ? 0 : -1
    }

    currentIndex: valueIndex()
    onActivated: function(index) {
        var next = control.textAt(index)
        if (next && next.length > 0)
            control.value = next
    }

    contentItem: Text {
        text: control.value && control.value.length > 0 ? control.value : control.currentText
        color: Theme.textPrimary
        verticalAlignment: Text.AlignVCenter
        leftPadding: 12
        rightPadding: 28
        elide: Text.ElideRight
        font.family: Theme.fontUi
        font.pixelSize: Theme.fontBody
    }

    background: Rectangle {
        radius: Theme.radiusControl - 1
        color: Theme.bgField
        border.color: control.hovered || control.activeFocus ? Theme.accentBorder : Theme.borderSoft
        border.width: 1
        Behavior on border.color { ColorAnimation { duration: Theme.motionBase } }
    }

    indicator: Text {
        x: control.width - width - 12
        y: (control.height - height) / 2
        text: "⌄"
        color: control.hovered ? Theme.textSecondary : Theme.textMuted
        font.pixelSize: 13
        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
    }

    popup: Popup {
        y: control.height + 4
        width: control.width
        implicitHeight: contentItem.implicitHeight + 8
        padding: 4
        background: Rectangle {
            color: "#0E1521"
            radius: Theme.radiusControl
            border.color: Theme.borderSoft
            border.width: 1
        }
        contentItem: ListView {
            clip: true
            implicitHeight: Math.min(contentHeight, 220)
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
    }

    delegate: ItemDelegate {
        width: control.width - 8
        height: 34
        contentItem: Text {
            text: modelData
            color: highlighted ? "#FFFFFF" : Theme.textSecondary
            verticalAlignment: Text.AlignVCenter
            leftPadding: 10
            font.family: Theme.fontUi
            font.pixelSize: 12
        }
        background: Rectangle {
            color: highlighted ? Theme.accent : (control.currentIndex === index ? Theme.accentSoft : "transparent")
            radius: 7
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }
    }
}
