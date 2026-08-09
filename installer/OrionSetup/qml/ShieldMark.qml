import QtQuick

// A small security shield with an inner check — the trust affordance. Drawn on a
// 24x24 viewBox so it scales crisply at any size. `filled` tints the body faintly
// (verified/on state); otherwise it renders as a clean outline (neutral state).
Canvas {
    id: root
    property color stroke: Theme.green
    property color fill: "transparent"
    property real weight: 2.0
    property bool showCheck: true
    implicitWidth: 16
    implicitHeight: 18

    onPaint: {
        const ctx = getContext("2d");
        ctx.reset();
        const sx = width / 24.0;
        const sy = height / 24.0;

        // shield silhouette: top shoulders, tapering to a rounded point
        ctx.beginPath();
        ctx.moveTo(12 * sx, 2 * sy);
        ctx.lineTo(20 * sx, 5 * sy);
        ctx.lineTo(20 * sx, 11 * sy);
        ctx.bezierCurveTo(20 * sx, 16.5 * sy, 16.5 * sx, 20.5 * sy,
                          12 * sx, 22 * sy);
        ctx.bezierCurveTo(7.5 * sx, 20.5 * sy, 4 * sx, 16.5 * sy,
                          4 * sx, 11 * sy);
        ctx.lineTo(4 * sx, 5 * sy);
        ctx.closePath();

        if (root.fill != "transparent") {
            ctx.fillStyle = root.fill;
            ctx.fill();
        }
        ctx.strokeStyle = root.stroke;
        ctx.lineWidth = root.weight;
        ctx.lineJoin = "round";
        ctx.stroke();

        // inner check
        if (root.showCheck) {
            ctx.beginPath();
            ctx.moveTo(8.5 * sx, 12 * sy);
            ctx.lineTo(11 * sx, 14.5 * sy);
            ctx.lineTo(15.5 * sx, 9 * sy);
            ctx.lineWidth = root.weight;
            ctx.lineCap = "round";
            ctx.lineJoin = "round";
            ctx.stroke();
        }
    }
    onStrokeChanged: requestPaint()
    onFillChanged: requestPaint()
    onShowCheckChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    Component.onCompleted: requestPaint()
}
