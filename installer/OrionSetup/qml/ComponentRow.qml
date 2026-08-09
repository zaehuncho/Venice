import QtQuick

// One row of the component checklist: state icon + label + size.
// state: "pending" | "active" | "done" | "fail"
Item {
    id: root
    property string label: ""
    property string size: ""
    property string state: "pending"
    property bool verified: false      // SHA-256 verified — shows the trust badge
    property bool last: false
    property bool reduceMotion: Theme.reduceMotion

    implicitHeight: 37

    // hairline separator
    Rectangle {
        visible: !root.last
        anchors.bottom: parent.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        height: 1
        color: Theme.lineSoft
    }

    Row {
        anchors.verticalCenter: parent.verticalCenter
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.leftMargin: 4
        anchors.rightMargin: 4
        spacing: 12

        // ── state icon ──
        Item {
            id: icon
            width: 20; height: 20
            anchors.verticalCenter: parent.verticalCenter

            // ring
            Rectangle {
                anchors.fill: parent
                radius: 10
                color: root.state === "done" ? Theme.green : "transparent"
                border.width: 1.5
                border.color: {
                    switch (root.state) {
                    case "active": return Theme.green;
                    case "done":   return Theme.green;
                    case "fail":   return Theme.red;
                    default:       return Theme.line;
                    }
                }
            }

            // spinner (active)
            Canvas {
                id: spinner
                anchors.fill: parent
                anchors.margins: -1.5
                visible: root.state === "active"
                property real angle: 0
                onPaint: {
                    const ctx = getContext("2d");
                    ctx.reset();
                    const r = width / 2 - 1.5;
                    ctx.translate(width / 2, height / 2);
                    ctx.rotate(angle);
                    ctx.beginPath();
                    ctx.arc(0, 0, r, -Math.PI / 2, 0);   // quarter arc
                    ctx.strokeStyle = Theme.green;
                    ctx.lineWidth = 1.5;
                    ctx.lineCap = "round";
                    ctx.stroke();
                }
                NumberAnimation on angle {
                    running: root.state === "active" && !root.reduceMotion
                    from: 0; to: 2 * Math.PI
                    duration: 800; loops: Animation.Infinite
                }
                onAngleChanged: requestPaint()
            }

            // check (done)
            CheckMark {
                anchors.centerIn: parent
                width: 12; height: 12
                visible: root.state === "done"
                stroke: "#04160B"
                weight: 3.4
            }

            // ! (fail)
            Text {
                anchors.centerIn: parent
                visible: root.state === "fail"
                text: "!"
                color: Theme.red
                font.family: Theme.sans
                font.pixelSize: 13
                font.weight: Font.Bold
            }
        }

        // ── label ──
        Text {
            // Row spacing is 12 between each *visible* child: icon·label·size = 24;
            // add the badge column (width + one more gap) only when it shows.
            width: parent.width - icon.width - sizeText.width - 24
                   - (root.verified ? badge.width + 12 : 0)
            anchors.verticalCenter: parent.verticalCenter
            text: root.label
            elide: Text.ElideRight
            color: (root.state === "active" || root.state === "done")
                   ? Theme.text : Theme.muted
            font.family: Theme.sans
            font.pixelSize: 14
        }

        // ── "Verified ✓" trust badge (once SHA-256 passes) ──
        Row {
            id: badge
            anchors.verticalCenter: parent.verticalCenter
            spacing: 4
            visible: root.verified
            opacity: root.verified ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 220 } }
            ShieldMark {
                anchors.verticalCenter: parent.verticalCenter
                width: 11; height: 13
                stroke: Theme.green
                fill: Qt.rgba(51/255, 222/255, 118/255, 0.14)
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "Verified"
                color: Theme.green
                font.family: Theme.mono
                font.pixelSize: 11
                font.letterSpacing: 0.3
            }
        }

        // ── size ──
        Text {
            id: sizeText
            anchors.verticalCenter: parent.verticalCenter
            text: root.size
            color: Theme.dim
            font.family: Theme.mono
            font.pixelSize: 12
        }
    }
}
