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
    Layout.preferredHeight: 88
    radius: Theme.radiusControl
    border.color: Theme.borderSoft
    border.width: 1
    gradient: Gradient {
        orientation: Gradient.Vertical
        GradientStop { position: 0.0; color: Theme.bgCardTop }
        GradientStop { position: 1.0; color: Theme.bgInset }
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 2
        RowLayout {
            spacing: 6
            Rectangle {
                Layout.preferredWidth: 6
                Layout.preferredHeight: 6
                radius: 3
                color: Qt.colorEqual(root.tone, Theme.textPrimary) ? Theme.accent : root.tone
            }
            Text {
                text: root.label.toUpperCase()
                color: Theme.textFaint
                font.pixelSize: Theme.fontMicro
                font.weight: Font.Bold
                font.letterSpacing: 0.8
            }
        }
        Text {
            text: root.value
            color: root.tone
            font.pixelSize: 26
            font.weight: Font.Bold
            font.letterSpacing: -0.5
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
