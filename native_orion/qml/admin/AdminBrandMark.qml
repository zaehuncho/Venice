import QtQuick

// The Venice mark for the consoles: a V cut into a rounded gradient tile, drawn in QML so it
// needs no asset on disk and scales with `size`. Same shape as the website plan-mark and the
// launcher's window icon, so the owner tool reads as the same product.
Rectangle {
    id: root

    property int size: 40
    property color tone: Theme.accent

    width: size
    height: size
    radius: Math.round(size * 0.28)
    border.color: Qt.rgba(1, 1, 1, 0.16)
    border.width: 1
    gradient: Gradient {
        orientation: Gradient.Vertical
        GradientStop { position: 0.0; color: Qt.lighter(root.tone, 1.12) }
        GradientStop { position: 1.0; color: Qt.darker(root.tone, 1.45) }
    }

    // Glass highlight across the top half.
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 1
        height: Math.max(2, Math.round(root.size * 0.44))
        radius: root.radius
        gradient: Gradient {
            orientation: Gradient.Vertical
            GradientStop { position: 0.0; color: Qt.rgba(1, 1, 1, 0.20) }
            GradientStop { position: 1.0; color: "transparent" }
        }
    }

    Canvas {
        anchors.fill: parent
        antialiasing: true
        onPaint: {
            var ctx = getContext("2d")
            ctx.reset()
            var s = width
            ctx.lineWidth = Math.max(1.5, s * 0.15)
            ctx.lineCap = "round"
            ctx.lineJoin = "round"
            ctx.strokeStyle = "#FFFFFF"
            ctx.beginPath()
            ctx.moveTo(s * 0.27, s * 0.31)
            ctx.lineTo(s * 0.50, s * 0.73)
            ctx.lineTo(s * 0.73, s * 0.31)
            ctx.stroke()
        }
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }
}
