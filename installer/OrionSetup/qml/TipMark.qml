import QtQuick

// The Orion mark: a small green upward "meter tip" triangle with a soft glow.
Item {
    id: root
    property color color: Theme.green
    property bool glow: true
    implicitWidth: 12
    implicitHeight: 11

    // soft glow behind the triangle (approximates the CSS drop-shadow)
    Rectangle {
        visible: root.glow
        anchors.centerIn: parent
        width: parent.width * 1.6
        height: parent.height * 1.6
        radius: width / 2
        color: Theme.glow
        opacity: 0.6
    }

    Canvas {
        id: cv
        anchors.fill: parent
        onPaint: {
            const ctx = getContext("2d");
            ctx.reset();
            ctx.beginPath();
            ctx.moveTo(width / 2, 0);          // apex
            ctx.lineTo(width, height);         // bottom-right
            ctx.lineTo(0, height);             // bottom-left
            ctx.closePath();
            ctx.fillStyle = root.color;
            ctx.fill();
        }
        Component.onCompleted: requestPaint()
    }
    onColorChanged: cv.requestPaint()
}
