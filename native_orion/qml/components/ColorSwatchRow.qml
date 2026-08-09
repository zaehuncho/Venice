import QtQuick
import OrionNative

// A fixed row of colour presets with a single selected entry.
//
// WHY PRESETS AND NOT A PICKER: both places this is used write a colour that has
// to survive a hostile background — the lock box sits on a floodlit white court
// beside a saturated red meter, and the lightbar is read from across a room. An
// arbitrary hex picker lets a user choose #101014 and then report the overlay as
// broken. Every preset here is a hue that stays legible in those conditions, so
// the control cannot produce an unusable result.
//
// CONTROLLED COMPONENT: `value` is bound by the parent to the persisted native
// property and is NEVER written here. Clicking emits `picked` and the parent
// performs the write; the binding then repaints the selection. Mutating `value`
// locally would break that binding and desync the row from the real setting —
// the same rule DashboardToggle documents.
Row {
    id: root

    // ["#RRGGBB", ...] — the swatches to offer, in display order.
    property var colors: []
    // Currently selected colour. Compared case-insensitively because native
    // normalises to upper case while QML literals are usually written lower.
    property string value: ""
    property real swatchSize: 26
    signal picked(string color)

    spacing: 8

    Repeater {
        model: root.colors

        delegate: Rectangle {
            required property string modelData

            width: root.swatchSize
            height: root.swatchSize
            radius: 6
            color: modelData

            readonly property bool selected:
                root.value.toUpperCase() === modelData.toUpperCase()

            // The selection ring is drawn in the swatch's OWN colour on the
            // outside and white on the inside. A single white ring vanished on
            // the pale swatches and a single dark ring vanished on the dark
            // ones; one of each is legible against every entry in the row.
            border.color: selected ? "#FFFFFF" : Theme.borderSoft
            border.width: selected ? 2 : 1

            Rectangle {
                visible: parent.selected
                anchors.fill: parent
                anchors.margins: -3
                radius: parent.radius + 3
                color: "transparent"
                border.color: parent.color
                border.width: 2
            }

            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: root.picked(parent.modelData)
            }
        }
    }
}
