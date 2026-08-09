import QtQuick
import OrionNative

// Starfield backdrop. Static by default (paints once — used behind the launcher shell
// with no idle cost). When `animated` is true it runs a render loop: stars twinkle +
// flicker and comets streak across — used on the auth gate for a cinematic look.
Canvas {
    id: root
    property bool interactive: false        // cursor-reactive constellation lines in empty areas
    property real constellationRadius: 155  // px: the pointer links to stars within this radius
    property real constellationDistance: 86 // px: two node-stars link when closer than this
    property bool constellations: true      // draw the star-to-star mesh (when animated/interactive)
    property real density: 1.0
    property int seed: 1337
    property bool animated: false
    property bool comets: true       // when animated, also streak comets (twinkle is always on)
    renderStrategy: Canvas.Cooperative

    readonly property color meshTint: Theme.accent
    onMeshTintChanged: requestPaint()

    // Animation state.
    property real _t: 0.0
    property var _stars: null
    property var _comets: []
    property real _spawnTimer: 1.2
    property var _nodes: []
    property real _mx: -1
    property real _my: -1

    Timer {
        interval: 33                     // ~30fps
        running: (root.animated || root.interactive) && root.visible
        repeat: true
        onTriggered: root._advance(0.033)
    }

    // Pointer tracking for the interactive constellation (hover only; clicks pass through).
    MouseArea {
        anchors.fill: parent
        enabled: root.interactive
        hoverEnabled: root.interactive
        acceptedButtons: Qt.NoButton
        onPositionChanged: { root._mx = mouseX; root._my = mouseY; }
        onExited: { root._mx = -1; root._my = -1; }
    }

    function _rng(s) {
        var st = (s >>> 0) || 1;
        return function () { st = (st * 1664525 + 1013904223) >>> 0; return st / 4294967296; };
    }

    function _ensureStars() {
        if (_stars && _stars._w === width && _stars._h === height)
            return;
        var rnd = _rng(seed);
        var n = Math.min(900, Math.round(width * height / 3600 * Math.max(0.2, density)));
        var arr = [];
        for (var i = 0; i < n; ++i) {
            var roll = rnd();
            var r, a;
            if (roll < 0.84) { r = 0.35 + rnd() * 0.7; a = 0.16 + rnd() * 0.4; }
            else if (roll < 0.975) { r = 1.05 + rnd() * 0.7; a = 0.5 + rnd() * 0.4; }
            else { r = 1.8 + rnd() * 1.0; a = 0.82 + rnd() * 0.18; }
            // Slow parallax drift (px/s): a gentle up-and-right float, depth-scaled
            // so the bigger/brighter "near" stars glide faster than the faint "far"
            // ones. This is what makes the field read as alive rather than static.
            var depth = 0.4 + r * 0.5;
            arr.push({
                x: rnd() * width, y: rnd() * height, r: r, a: a,
                phase: rnd() * 6.2832, speed: 0.15 + rnd() * 0.6,
                blue: rnd() < 0.22, bright: roll >= 0.975, flicker: rnd() < 0.12,
                vx: (3.5 + (-2.2 + 4.4 * rnd())) * depth,
                vy: (-2.8 + (-2.2 + 4.4 * rnd())) * depth
            });
        }
        // Constellation nodes: a capped, spatially-spread subset used for the mesh.
        var nodes = [];
        for (var k = 0; k < arr.length && nodes.length < 150; ++k) {
            if (arr[k].r > 0.8) nodes.push(arr[k]);
        }
        arr._w = width; arr._h = height;
        _stars = arr;
        _nodes = nodes;
    }

    function _spawnComet() {
        var fromLeft = Math.random() < 0.5;
        var speed = 320 + Math.random() * 320;          // px/s
        var ang = (18 + Math.random() * 34) * Math.PI / 180;  // shallow downward
        var vx = (fromLeft ? 1 : -1) * speed * Math.cos(ang);
        var vy = speed * Math.sin(ang);
        var x = fromLeft ? -40 + Math.random() * width * 0.4 : width + 40 - Math.random() * width * 0.4;
        _comets.push({ x: x, y: -30 - Math.random() * height * 0.2, vx: vx, vy: vy,
                       len: 90 + Math.random() * 150, life: 0 });
    }

    function _advance(dt) {
        _t += dt;
        // Drift the stars and wrap them around the edges so the field flows
        // continuously without ever emptying.
        if (_stars) {
            for (var si = 0; si < _stars.length; ++si) {
                var st = _stars[si];
                st.x += st.vx * dt;
                st.y += st.vy * dt;
                if (st.x < -2) st.x += width + 4; else if (st.x > width + 2) st.x -= width + 4;
                if (st.y < -2) st.y += height + 4; else if (st.y > height + 2) st.y -= height + 4;
            }
        }
        if (comets) {
            _spawnTimer -= dt;
            if (_spawnTimer <= 0) {
                _spawnComet();
                _spawnTimer = 2.0 + Math.random() * 4.5;     // next comet in 2-6.5s
            }
        }
        var keep = [];
        for (var i = 0; i < _comets.length; ++i) {
            var c = _comets[i];
            c.x += c.vx * dt; c.y += c.vy * dt; c.life += dt;
            if (c.y < height + 60 && c.x > -80 && c.x < width + 80)
                keep.push(c);
        }
        _comets = keep;
        requestPaint();
    }

    function _twinkle(s) {
        var v = 0.30 + 0.70 * (0.5 + 0.5 * Math.sin(_t * s.speed * 6.2832 + s.phase));
        if (s.flicker)
            v *= 0.7 + 0.3 * Math.sin(_t * 7.0 + s.phase * 3.0);   // fast shimmer
        return v;
    }

    onPaint: {
        var ctx = getContext("2d");
        ctx.clearRect(0, 0, width, height);

        // Deep-space depth gradient.
        var g = ctx.createLinearGradient(0, 0, width, height);
        g.addColorStop(0, "rgba(13,19,29,0.95)");
        g.addColorStop(0.55, "rgba(8,11,16,0.92)");
        g.addColorStop(1, "rgba(11,16,24,0.96)");
        ctx.fillStyle = g;
        ctx.fillRect(0, 0, width, height);

        // Faint nebula glow.
        var tr = Math.round(meshTint.r * 255), tg = Math.round(meshTint.g * 255), tb = Math.round(meshTint.b * 255);
        var blobs = [
            { x: width * 0.80, y: height * 0.14, r: Math.max(width, height) * 0.55, c: "rgba(" + tr + "," + tg + "," + tb + ",0.075)" },
            { x: width * 0.12, y: height * 0.88, r: Math.max(width, height) * 0.50, c: "rgba(79,140,255,0.055)" }
        ];
        for (var b = 0; b < blobs.length; ++b) {
            var rg = ctx.createRadialGradient(blobs[b].x, blobs[b].y, 0, blobs[b].x, blobs[b].y, blobs[b].r);
            rg.addColorStop(0, blobs[b].c); rg.addColorStop(1, "rgba(0,0,0,0)");
            ctx.fillStyle = rg; ctx.fillRect(0, 0, width, height);
        }

        _ensureStars();

        // Constellation mesh — faint lines between nearby node stars.
        if (constellations && (animated || interactive) && _nodes && _nodes.length) {
            var cd = constellationDistance, cd2 = cd * cd;
            ctx.lineWidth = 1.0;
            for (var ai = 0; ai < _nodes.length; ++ai) {
                var p = _nodes[ai];
                for (var aj = ai + 1; aj < _nodes.length; ++aj) {
                    var q = _nodes[aj];
                    var ndx = p.x - q.x, ndy = p.y - q.y;
                    var nd2 = ndx * ndx + ndy * ndy;
                    if (nd2 < cd2) {
                        var ma = (1 - Math.sqrt(nd2) / cd) * 0.16;
                        ctx.strokeStyle = "rgba(" + tr + "," + tg + "," + tb + "," + ma.toFixed(3) + ")";
                        ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(q.x, q.y); ctx.stroke();
                    }
                }
            }
        }

        // Cursor constellation — lines from the pointer to nearby stars + a soft glow.
        if (interactive && _mx >= 0 && _nodes) {
            var cr = constellationRadius, cr2 = cr * cr;
            ctx.lineWidth = 1.1;
            for (var ci = 0; ci < _nodes.length; ++ci) {
                var sc = _nodes[ci];
                var pdx = sc.x - _mx, pdy = sc.y - _my;
                var pd2 = pdx * pdx + pdy * pdy;
                if (pd2 < cr2) {
                    var pa = (1 - Math.sqrt(pd2) / cr) * 0.55;
                    ctx.strokeStyle = "rgba(" + tr + "," + tg + "," + tb + "," + pa.toFixed(3) + ")";
                    ctx.beginPath(); ctx.moveTo(_mx, _my); ctx.lineTo(sc.x, sc.y); ctx.stroke();
                }
            }
        }

        for (var i = 0; i < _stars.length; ++i) {
            var s = _stars[i];
            var a = animated ? s.a * _twinkle(s) : s.a;
            if (a <= 0.02) continue;
            var rc = s.blue ? 176 : 230, gc = s.blue ? 206 : 238;
            if (s.bright) {
                var halo = ctx.createRadialGradient(s.x, s.y, 0, s.x, s.y, s.r * 5);
                halo.addColorStop(0, "rgba(" + rc + "," + gc + ",255," + (a * 0.5).toFixed(3) + ")");
                halo.addColorStop(1, "rgba(0,0,0,0)");
                ctx.fillStyle = halo;
                ctx.beginPath(); ctx.arc(s.x, s.y, s.r * 5, 0, 6.2832); ctx.fill();
                if (a > 0.7) {  // sparkle cross on the brightest moments
                    ctx.strokeStyle = "rgba(" + rc + "," + gc + ",255," + (a * 0.6).toFixed(3) + ")";
                    ctx.lineWidth = 0.7;
                    ctx.beginPath();
                    ctx.moveTo(s.x - s.r * 3.4, s.y); ctx.lineTo(s.x + s.r * 3.4, s.y);
                    ctx.moveTo(s.x, s.y - s.r * 3.4); ctx.lineTo(s.x, s.y + s.r * 3.4);
                    ctx.stroke();
                }
            }
            ctx.fillStyle = "rgba(" + rc + "," + gc + ",255," + a.toFixed(3) + ")";
            ctx.beginPath(); ctx.arc(s.x, s.y, s.r, 0, 6.2832); ctx.fill();
        }

        // Comets (animated + comets enabled).
        if (animated && comets) {
            for (var k = 0; k < _comets.length; ++k) {
                var c = _comets[k];
                var spd = Math.hypot(c.vx, c.vy) || 1;
                var tx = c.x - c.vx / spd * c.len, ty = c.y - c.vy / spd * c.len;
                var fade = Math.min(1.0, c.life / 0.25);   // quick fade-in
                var lg = ctx.createLinearGradient(c.x, c.y, tx, ty);
                lg.addColorStop(0, "rgba(224,238,255," + (0.92 * fade).toFixed(3) + ")");
                lg.addColorStop(1, "rgba(120,170,255,0)");
                ctx.strokeStyle = lg; ctx.lineWidth = 2.4; ctx.lineCap = "round";
                ctx.beginPath(); ctx.moveTo(c.x, c.y); ctx.lineTo(tx, ty); ctx.stroke();
                var hg = ctx.createRadialGradient(c.x, c.y, 0, c.x, c.y, 7);
                hg.addColorStop(0, "rgba(244,249,255," + (0.95 * fade).toFixed(3) + ")");
                hg.addColorStop(1, "rgba(120,170,255,0)");
                ctx.fillStyle = hg;
                ctx.beginPath(); ctx.arc(c.x, c.y, 7, 0, 6.2832); ctx.fill();
            }
        }
    }

    onWidthChanged: requestPaint()
    onHeightChanged: requestPaint()
    Component.onCompleted: requestPaint()
}
