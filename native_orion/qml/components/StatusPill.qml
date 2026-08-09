import QtQuick
import QtQuick.Layouts
import OrionNative

Rectangle {
    id: root
    property string label: ""
    property string statusText: ""
    property string tone: "neutral"
    property bool animated: false
    implicitHeight: 30
    implicitWidth: content.implicitWidth + 22
    radius: Theme.radiusPill
    color: tone === "success" ? Theme.successDim : tone === "warning" ? Theme.warningDim : tone === "danger" ? Theme.dangerDim : Theme.bgSidebar
    border.color: tone === "success" ? Theme.success : tone === "warning" ? Theme.warning : tone === "danger" ? Theme.danger : Theme.borderSoft
    border.width: 1

    Behavior on color { ColorAnimation { duration: Theme.motionSlow } }
    Behavior on border.color { ColorAnimation { duration: Theme.motionSlow } }

    RowLayout {
        id: content
        anchors.fill: parent
        anchors.leftMargin: 10
        anchors.rightMargin: 10
        spacing: 7

        Rectangle {
            width: 7
            height: 7
            radius: 4
            color: root.tone === "success" ? Theme.success : root.tone === "warning" ? Theme.warning : root.tone === "danger" ? Theme.danger : Theme.accent
            Behavior on color { ColorAnimation { duration: Theme.motionSlow } }
            SequentialAnimation on opacity {
                running: root.animated && root.tone !== "neutral"
                loops: Animation.Infinite
                alwaysRunToEnd: true
                NumberAnimation { to: 0.45; duration: 1100; easing.type: Easing.InOutQuad }
                NumberAnimation { to: 1.0; duration: 1100; easing.type: Easing.InOutQuad }
            }
        }

        Text {
            text: root.label.length > 0 ? root.label + ": " + root.statusText : root.statusText
            color: Theme.textPrimary
            font.family: Theme.fontUi
            font.pixelSize: 12
            elide: Text.ElideRight
            Layout.maximumWidth: 190
        }
    }
}
