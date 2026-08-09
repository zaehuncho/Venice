import QtQuick

// THE HERO — download progress rendered as a shot meter. A horizontal fill bar
// with faint tick marks and a green "zone" at the far right; the fill rises and
// at 100% the whole bar FLASHES perfect-green with a glow.
Item {
    id: root
    property real value: 0          // 0..100
    property bool perfect: false    // 100% perfect-flash state
    property bool reduceMotion: Theme.reduceMotion
    implicitHeight: 34

    // perfect-flash outer glow
    Rectangle {
        anchors.fill: meter
        anchors.margins: -6
        radius: 12
        color: "transparent"
        border.width: 1
        border.color: Theme.perfect
        opacity: root.perfect ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 220 } }
    }

    Rectangle {
        id: meter
        anchors.fill: parent
        radius: 8
        color: Theme.raised
        border.width: 1
        border.color: root.perfect ? Theme.perfect : Theme.line
        clip: true

        // inner top shadow
        Rectangle {
            anchors.fill: parent
            radius: parent.radius
            gradient: Gradient {
                GradientStop { position: 0.0; color: Qt.rgba(0,0,0,0.35) }
                GradientStop { position: 0.25; color: "transparent" }
            }
        }

        // faint tick marks
        Row {
            anchors.fill: parent
            Repeater {
                model: Math.floor(meter.width / 24)
                Item {
                    width: 24; height: meter.height
                    Rectangle {
                        anchors.right: parent.right
                        width: 1; height: parent.height
                        color: Qt.rgba(1,1,1,0.04)
                    }
                }
            }
        }

        // green "zone" at the far right
        Rectangle {
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            width: parent.width * 0.08
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: "transparent" }
                GradientStop { position: 1.0; color: Qt.rgba(51/255,222/255,118/255,0.14) }
            }
            Rectangle {   // dashed-ish left edge
                anchors.left: parent.left
                width: 1; height: parent.height
                color: Qt.rgba(51/255,222/255,118/255,0.4)
            }
        }

        // the rising fill
        Rectangle {
            id: fill
            anchors.top: parent.top
            anchors.bottom: parent.bottom
            anchors.left: parent.left
            width: parent.width * Math.max(0, Math.min(100, root.value)) / 100
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: root.perfect ? Theme.green : "#1F9E52" }
                GradientStop { position: 1.0; color: root.perfect ? Theme.perfect : Theme.green }
            }
            Behavior on width {
                enabled: !root.reduceMotion
                NumberAnimation { duration: 250; easing.type: Easing.Linear }
            }
        }

        // fill glow overlay
        Rectangle {
            anchors.fill: fill
            radius: 4
            color: "transparent"
            border.width: 2
            border.color: Theme.glow
            opacity: 0.5
            visible: fill.width > 2
        }
    }

    // the bright "tip" marker riding the leading edge of the fill
    Rectangle {
        id: tipbar
        width: 2
        y: -2
        height: meter.height + 4
        x: meter.width * Math.max(0, Math.min(100, root.value)) / 100 - width / 2
        color: Theme.greenBright
        Behavior on x {
            enabled: !root.reduceMotion
            NumberAnimation { duration: 250; easing.type: Easing.Linear }
        }
        Rectangle {   // glow
            anchors.centerIn: parent
            width: 8; height: parent.height + 6
            radius: 4
            color: Theme.glow
            opacity: 0.6
            z: -1
        }
    }
}
