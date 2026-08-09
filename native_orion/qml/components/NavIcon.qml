import QtQuick

// Crisp, recolorable nav icons drawn with Canvas (no external icon module). Replaces
// the playful unicode glyphs. `icon` selects the glyph; `color` tints it (bind to Theme).
Canvas {
    id: ico
    property string icon: ""
    property color color: "#FFFFFF"
    width: 18
    height: 18
    renderStrategy: Canvas.Cooperative

    onColorChanged: requestPaint()
    onIconChanged: requestPaint()
    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    Component.onCompleted: requestPaint()

    function _rr(c, x, y, w, h, r) {
        c.beginPath();
        c.moveTo(x + r, y);
        c.arcTo(x + w, y, x + w, y + h, r);
        c.arcTo(x + w, y + h, x, y + h, r);
        c.arcTo(x, y + h, x, y, r);
        c.arcTo(x, y, x + w, y, r);
        c.closePath();
    }

    onPaint: {
        var c = getContext("2d");
        c.clearRect(0, 0, width, height);
        var w = width, h = height, s = Math.min(w, h);
        c.strokeStyle = color;
        c.fillStyle = color;
        c.lineWidth = Math.max(1.4, s * 0.085);
        c.lineJoin = "round";
        c.lineCap = "round";
        var pad = s * 0.16;

        if (icon === "home") {
            c.beginPath();
            c.moveTo(pad, h * 0.52);
            c.lineTo(w / 2, pad);
            c.lineTo(w - pad, h * 0.52);
            c.stroke();
            c.beginPath();
            c.moveTo(w * 0.27, h * 0.47);
            c.lineTo(w * 0.27, h - pad);
            c.lineTo(w * 0.73, h - pad);
            c.lineTo(w * 0.73, h * 0.47);
            c.stroke();
        } else if (icon === "play") {
            c.beginPath();
            c.moveTo(w * 0.36, h * 0.26);
            c.lineTo(w * 0.74, h * 0.5);
            c.lineTo(w * 0.36, h * 0.74);
            c.closePath();
            c.fill();
        } else if (icon === "grid") {
            var g = s * 0.30, gap = s * 0.13;
            var ox = (w - (2 * g + gap)) / 2, oy = (h - (2 * g + gap)) / 2;
            for (var i = 0; i < 2; i++)
                for (var j = 0; j < 2; j++) {
                    _rr(c, ox + i * (g + gap), oy + j * (g + gap), g, g, s * 0.05);
                    c.fill();
                }
        } else if (icon === "notes") {
            var x0 = w * 0.25, x1 = w * 0.75;
            for (var k = 0; k < 3; k++) {
                var ly = h * 0.33 + k * (h * 0.17);
                c.beginPath();
                c.moveTo(x0, ly);
                c.lineTo(k === 2 ? w * 0.6 : x1, ly);
                c.stroke();
            }
        } else if (icon === "guide") {
            // Open book / guide: two facing pages with a center spine.
            var bx = w * 0.16, bw = w * 0.68;
            var by = h * 0.26, bh = h * 0.46;
            var mx = w * 0.5;
            // Left + right page outlines meeting at the spine, with a slight top arc.
            c.beginPath();
            c.moveTo(mx, by + h * 0.03);
            c.lineTo(bx, by);
            c.lineTo(bx, by + bh);
            c.lineTo(mx, by + bh - h * 0.03);
            c.closePath();
            c.stroke();
            c.beginPath();
            c.moveTo(mx, by + h * 0.03);
            c.lineTo(bx + bw, by);
            c.lineTo(bx + bw, by + bh);
            c.lineTo(mx, by + bh - h * 0.03);
            c.closePath();
            c.stroke();
        } else if (icon === "gear") {
            var cx = w / 2, cy = h / 2, R = s * 0.27;
            for (var t = 0; t < 8; t++) {
                var a = t * Math.PI / 4;
                c.beginPath();
                c.moveTo(cx + Math.cos(a) * R, cy + Math.sin(a) * R);
                c.lineTo(cx + Math.cos(a) * (R + s * 0.11), cy + Math.sin(a) * (R + s * 0.11));
                c.stroke();
            }
            c.beginPath();
            c.arc(cx, cy, R, 0, 6.2832);
            c.stroke();
            c.beginPath();
            c.arc(cx, cy, s * 0.10, 0, 6.2832);
            c.stroke();
        }
    }
}
