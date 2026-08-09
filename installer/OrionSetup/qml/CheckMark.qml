import QtQuick

// The mockup's check glyph — the SVG path "M20 6 9 17l-5-5" drawn as a stroke.
Canvas {
    id: root
    property color stroke: Theme.green
    property real weight: 2.4
    implicitWidth: 16
    implicitHeight: 16

    onPaint: {
        const ctx = getContext("2d");
        ctx.reset();
        ctx.strokeStyle = root.stroke;
        ctx.lineWidth = root.weight;
        ctx.lineCap = "round";
        ctx.lineJoin = "round";
        // normalise the 24x24 viewBox path to this item's size
        const s = width / 24.0;
        ctx.beginPath();
        ctx.moveTo(20 * s, 6 * s);
        ctx.lineTo(9 * s, 17 * s);
        ctx.lineTo(4 * s, 12 * s);
        ctx.stroke();
    }
    onStrokeChanged: requestPaint()
    onWidthChanged: requestPaint()
    Component.onCompleted: requestPaint()
}
