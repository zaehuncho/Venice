import QtQuick

// Circular hue/saturation picker shared by the Appearance card and the Lightbar
// Studio. Painted with Canvas.Image + Immediate so it renders reliably inside
// Loaders/ScrollViews/Popups (the FBO render target silently skipped the first
// paint there, which is why the wheel showed up blank).
Item {
    id: root
    implicitWidth: 160
    implicitHeight: 160

    // Currently selected color (drives the indicator dot). Callers bind this to
    // their persisted setting; picking emits colorPicked without writing back.
    property color selectedColor: "#2563EB"
    signal colorPicked(string hex)

    readonly property real wheelRadius: Math.min(width, height) / 2 - 3

    Canvas {
        id: wheel
        anchors.fill: parent
        renderTarget: Canvas.Image
        renderStrategy: Canvas.Immediate

        onPaint: {
            var ctx = getContext("2d")
            var w = width
            var h = height
            if (w <= 0 || h <= 0)
                return
            var cx = w / 2
            var cy = h / 2
            var r = Math.min(cx, cy)
            ctx.clearRect(0, 0, w, h)
            var image = ctx.createImageData(w, h)
            for (var y = 0; y < h; y++) {
                for (var x = 0; x < w; x++) {
                    var dx = x - cx
                    var dy = y - cy
                    var d = Math.sqrt(dx * dx + dy * dy)
                    var idx = (y * w + x) * 4
                    if (d <= r - 3) {
                        var hue = Math.atan2(dy, dx) / (Math.PI * 2)
                        if (hue < 0)
                            hue += 1
                        var c = Qt.hsva(hue, d / r, 1, 1)
                        image.data[idx] = Math.round(c.r * 255)
                        image.data[idx + 1] = Math.round(c.g * 255)
                        image.data[idx + 2] = Math.round(c.b * 255)
                        image.data[idx + 3] = 255
                    } else {
                        image.data[idx + 3] = 0
                    }
                }
            }
            ctx.putImageData(image, 0, 0)
        }

        // The FBO path could drop the initial paint when the wheel lived inside a
        // Loader/Popup that wasn't visible yet — repaint on every (re)appearance.
        onVisibleChanged: if (visible) requestPaint()
        Component.onCompleted: requestPaint()
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
    }

    // Selection indicator: place the dot at the selected color's hue/sat position.
    Rectangle {
        readonly property real selHue: root.selectedColor.hsvHue < 0 ? 0 : root.selectedColor.hsvHue
        readonly property real selSat: root.selectedColor.hsvSaturation
        x: root.width / 2 + Math.cos(selHue * Math.PI * 2) * selSat * root.wheelRadius - width / 2
        y: root.height / 2 + Math.sin(selHue * Math.PI * 2) * selSat * root.wheelRadius - height / 2
        width: 14
        height: 14
        radius: 7
        color: "transparent"
        border.color: "#FFFFFF"
        border.width: 2

        Rectangle {
            anchors.centerIn: parent
            width: 8
            height: 8
            radius: 4
            color: root.selectedColor
            border.color: "#05080D"
            border.width: 1
        }
    }

    MouseArea {
        anchors.fill: parent
        cursorShape: Qt.CrossCursor
        function pick(mx, my) {
            var cx = root.width / 2
            var cy = root.height / 2
            var dx = mx - cx
            var dy = my - cy
            var radius = Math.min(cx, cy) - 4
            var dist = Math.sqrt(dx * dx + dy * dy)
            if (dist > radius) {
                // Clamp drags that wander off the rim to the rim color instead of
                // ignoring them — feels like a real picker, not a dead zone.
                dist = radius
            }
            var hue = Math.atan2(dy, dx) / (Math.PI * 2)
            if (hue < 0)
                hue += 1
            var sat = Math.max(0, Math.min(1, dist / radius))
            var c = Qt.hsva(hue, sat, 1, 1)
            function hex2(v) {
                var n = Math.max(0, Math.min(255, Math.round(v * 255)))
                return ("0" + n.toString(16)).slice(-2).toUpperCase()
            }
            root.colorPicked("#" + hex2(c.r) + hex2(c.g) + hex2(c.b))
        }
        onPressed: function(mouse) { pick(mouse.x, mouse.y) }
        onPositionChanged: function(mouse) { if (pressed) pick(mouse.x, mouse.y) }
    }
}
