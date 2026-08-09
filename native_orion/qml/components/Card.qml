import QtQuick
import QtQuick.Layouts
import OrionNative

// Card system base: consistent radius/elevation/spacing/typography, optional
// status accent. `tone` ("neutral" | "accent" | "success" | "warning" | "danger")
// drives a slim left accent bar + border tint so status reads at a glance.
Rectangle {
    id: root
    property string title: ""
    property string subtitle: ""
    property string tone: "neutral"
    property bool interactive: false
    default property alias content: body.data

    readonly property color toneColor: tone === "accent" ? Theme.accent
                                       : tone === "success" ? Theme.success
                                       : tone === "warning" ? Theme.warning
                                       : tone === "danger" ? Theme.danger
                                       : Theme.borderSoft

    radius: Theme.radiusCard
    color: "transparent"
    border.color: tone !== "neutral" ? Qt.rgba(toneColor.r, toneColor.g, toneColor.b, 0.45)
                  : root.interactive && hoverHandler.hovered ? Theme.borderStrong : Theme.borderSoft
    border.width: 1

    Behavior on border.color { ColorAnimation { duration: Theme.motionBase; easing.type: Easing.OutCubic } }

    // Soft elevation: a one-pixel darker halo below the card (no effects module).
    Rectangle {
        z: -1
        anchors.fill: parent
        anchors.topMargin: 2
        anchors.leftMargin: 1
        anchors.rightMargin: -1
        anchors.bottomMargin: -2
        radius: parent.radius + 1
        color: "#06090E"
        opacity: 0.55
    }

    // Surface: subtle top-lit vertical gradient instead of a flat fill.
    Rectangle {
        anchors.fill: parent
        anchors.margins: 1
        radius: parent.radius - 1
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: root.interactive && hoverHandler.hovered ? Theme.bgCardHover : Theme.bgCardTop }
            GradientStop { position: 1.0; color: Theme.bgCardBottom }
        }
    }

    // 1px light catch along the top edge — gives the card physical depth.
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: parent.radius
        anchors.rightMargin: parent.radius
        anchors.topMargin: 1
        height: 1
        color: Theme.hairlineLight
    }

    // Status accent bar.
    Rectangle {
        visible: root.tone !== "neutral"
        anchors.left: parent.left
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 10
        anchors.bottomMargin: 10
        anchors.leftMargin: 0
        width: 3
        radius: 1.5
        color: root.toneColor
        Behavior on color { ColorAnimation { duration: Theme.motionBase } }
    }

    // HoverHandler doesn't block clicks the way MouseArea would, so children
    // (combos, sliders, buttons) still receive their events normally.
    HoverHandler {
        id: hoverHandler
        enabled: root.interactive
        acceptedDevices: PointerDevice.Mouse | PointerDevice.TouchPad
    }

    ColumnLayout {
        anchors.fill: parent
        anchors.margins: 16
        spacing: 12

        ColumnLayout {
            spacing: 3
            visible: root.title.length > 0 || root.subtitle.length > 0
            Layout.fillWidth: true

            Text {
                text: root.title
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: 13
                font.weight: Font.DemiBold
                visible: root.title.length > 0
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
            Text {
                text: root.subtitle
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 12
                visible: root.subtitle.length > 0
                elide: Text.ElideRight
                Layout.fillWidth: true
            }
        }

        Item {
            id: body
            Layout.fillWidth: true
            Layout.fillHeight: true
        }
    }
}
