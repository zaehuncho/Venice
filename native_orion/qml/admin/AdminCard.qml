import QtQuick
import QtQuick.Layouts

// Card surface for the admin tools. Children land in the inner ColumnLayout.
Rectangle {
    id: root

    property string title: ""
    property string subtitle: ""
    property int padding: 14
    property int contentSpacing: 8
    property alias headerRow: headerActions.data
    default property alias content: body.data

    radius: Theme.radiusCard
    border.color: Theme.borderSoft
    border.width: 1
    // Same surface as the launcher's cards: a little lighter at the top so stacked cards read
    // as panels rather than flat boxes.
    gradient: Gradient {
        orientation: Gradient.Vertical
        GradientStop { position: 0.0; color: Theme.bgCardTop }
        GradientStop { position: 1.0; color: Theme.bgCardBottom }
    }
    implicitHeight: column.implicitHeight + padding * 2
    implicitWidth: 240

    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        height: 1
        radius: parent.radius
        color: Theme.hairlineLight
    }

    ColumnLayout {
        id: column
        anchors.fill: parent
        anchors.margins: root.padding
        spacing: root.contentSpacing

        RowLayout {
            Layout.fillWidth: true
            visible: root.title.length > 0 || headerActions.children.length > 0
            spacing: 8
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Text {
                    visible: root.title.length > 0
                    text: root.title
                    color: Theme.textPrimary
                    font.pixelSize: Theme.fontTitle
                    font.weight: Font.DemiBold
                }
                Text {
                    visible: root.subtitle.length > 0
                    Layout.fillWidth: true
                    text: root.subtitle
                    color: Theme.textMuted
                    font.pixelSize: Theme.fontCaption
                    wrapMode: Text.WordWrap
                }
            }
            RowLayout {
                id: headerActions
                spacing: 6
            }
        }

        ColumnLayout {
            id: body
            Layout.fillWidth: true
            Layout.fillHeight: true
            spacing: root.contentSpacing
        }
    }
}
