import QtQuick
import QtQuick.Layouts
import OrionNative

// First-run guided tour — a coach-mark / spotlight overlay that sits ON TOP of the
// real launcher and walks a new user through the actual controls in place (sidebar,
// Connect, live capture, meter panel, shot type) rather than a separate screen.
//
//   * Modal-but-not-trapping: the dim scrim blocks stray clicks OUTSIDE the spotlight,
//     but the spotlight hole is a real cut-out (no overlay item there) so the
//     highlighted control stays live and clickable. Esc / Skip / Finish always exit
//     and hand focus back to the app.
//   * Robust: a re-measure timer maps each step's registered target (TourRegistry)
//     into overlay space every frame, so the spotlight tracks layout/scroll/page
//     fades. If a target is missing or hidden the step degrades to a centered caption
//     instead of breaking.
//   * The final step folds in the old Preflight checks as a LIVE readiness list
//     (capture / Remote Play / meter lock) reusing the same controller properties the
//     real session runs on — so the standalone Preflight page isn't needed.
//
// Persistence: start() is driven by orion.preflightComplete (the first-run "seen"
// flag).
//
// [ORION_UI_BUBBLES 2026-09-15 owner: "quick start should just be shown when the
// customer first launches the UI"] AppShell.qml now marks the flag the moment it
// auto-opens this, not when the user reaches Finish/Skip — an abandoned tour used
// to leave the flag false and re-show on every later launch. finish() still marks
// it (idempotent), and the sidebar's "Quick Start" re-open button is gone: the ONLY
// way this appears is the customer's first launch.
Item {
    id: tour
    anchors.fill: parent
    z: 4000
    visible: running

    property bool running: false
    property int index: 0

    // Padding baked around the spotlight hole so the highlighted control isn't
    // clipped tight to its own border.
    readonly property int holePad: 8

    // Each step: title/body caption + optional target (TourRegistry key) + optional
    // page to switch to first. kind "ready" swaps the body for the live readiness list.
    readonly property var steps: [
        {
            page: "remotePlay", target: "",
            title: "Welcome to Venice",
            // [ORION_UI_BUBBLES 2026-09-15] No longer points at a "Quick Start"
            // button — that footer action is gone and this runs on first launch only.
            body: "A quick tour of the launcher — where to connect, match your meter profile, and check readiness. It only shows on your first launch, and you can skip any time."
        },
        {
            page: "remotePlay", target: "nav:remotePlay",
            title: "Your navigation",
            body: "Live is where you connect and play. Setup holds your console, video source, and audio settings, and Updates carries release notes."
        },
        {
            page: "remotePlay", target: "rp:connect",
            title: "Connect to your PS5",
            body: "Connect brings the live feed up and hands Venice your controller. Make sure Remote Play is enabled on the PS5 first."
        },
        {
            page: "remotePlay", target: "rp:controllerFix", kind: "controller",
            title: "Controller Setup",
            body: "Plug your controller into the PC over USB. Windows' default USB power setting can silently drop the pad off USB after the PC sits idle — if your pad carries that risk, apply the one-time fix below. The same button stays available on the Live page (it can reappear for a new USB port)."
        },
        {
            page: "remotePlay", target: "rp:capture",
            title: "The live capture",
            body: "Your PS5 picture shows here once connected. If it stays black, HDCP is still on — turn it off on the PS5: Settings → System → HDMI → Enable HDCP (off)."
        },
        {
            page: "remotePlay", target: "rp:meter",
            title: "Match your meter",
            // TRUTHFUL COPY (2026-08-06): a fresh install does have a warm-up. The
            // 2026-09-14 rewrite drops the pointer to the "Timing" pill and the
            // warming-up banner — both surfaces are gone — without going back to the
            // old lie that there is no warm-up at all.
            body: "Pick the Style and Color that match the in-game meter. Detection adapts on its own while you play, and Venice keeps refining its timing on your setup over your first shots."
        },
        {
            page: "remotePlay", target: "rp:shotType",
            title: "Shot type",
            body: "This chip shows the shot type Venice is timing right now. Each type keeps its own live timing model and refines itself while you play."
        },
        {
            page: "remotePlay", target: "", kind: "ready",
            title: "Ready check",
            body: "A quick pre-game look at the three things that must work. Green across the board means your first real game will just work — no mid-game surprises."
        },
        {
            page: "dashboard", target: "nav:dashboard",
            title: "Review your setup",
            body: "Setup keeps your connection and stream settings in one place. That's the tour — press Finish and jump into a game."
        }
    ]

    readonly property var step: steps[Math.max(0, Math.min(index, steps.length - 1))]
    readonly property bool isFirst: index <= 0
    readonly property bool isLast: index >= steps.length - 1

    // ---- live spotlight geometry (overlay-space) --------------------------------
    property bool hasTarget: false
    property real holeX: 0
    property real holeY: 0
    property real holeW: 0
    property real holeH: 0

    // ---- readiness (folded-in preflight) checks ---------------------------------
    readonly property bool captureCardSource: orion.videoSource === "capture_card"
    readonly property bool capturePass: captureCardSource ? (orion.captureDeviceList.length > 0) : true
    readonly property bool streamLive: orion.remoteRunning || orion.chiakiEmbedStatus === "Embedded"
    readonly property bool framesFlowing: orion.captureSourceHealth === "frame_feed_active" || orion.uniqueFrameFps > 0
    readonly property bool remotePass: streamLive && framesFlowing
    // Latched once the detector confirms a meter during this tour so the row stays
    // green after the meter disappears post-release.
    property bool meterSeen: false

    Connections {
        target: orion
        enabled: tour.running
        function onMeterBoxChanged() {
            if (orion.meterConfirmed && orion.meterBoxWidth > 0)
                tour.meterSeen = true
        }
    }

    // ---- inline components (declared at the file root, per QML rules) ------------

    // Dimming panel that eats clicks so the UI outside the spotlight is inert. Four
    // of these border the hole; the hole itself carries no overlay, so the
    // highlighted control stays live and clickable.
    component Scrim: Rectangle {
        color: Theme.overlayScrim
        Behavior on x { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on y { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on width { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on height { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        MouseArea { anchors.fill: parent; hoverEnabled: true; onClicked: {} }
    }

    // One live readiness row (folded-in preflight check).
    component ReadyRow: RowLayout {
        id: rowRoot
        property string rowLabel: ""
        property string rowDetail: ""
        property bool ok: false
        Layout.fillWidth: true
        spacing: 9
        Rectangle {
            Layout.preferredWidth: 18
            Layout.preferredHeight: 18
            radius: 9
            color: rowRoot.ok ? Theme.successDim : Theme.accentSoft
            border.color: rowRoot.ok ? Theme.success : Theme.borderStrong
            border.width: 1
            Text {
                anchors.centerIn: parent
                text: rowRoot.ok ? "✓" : "…"
                color: rowRoot.ok ? Theme.success : Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
                font.weight: Font.Bold
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 0
            Text {
                text: rowRoot.rowLabel
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                text: rowRoot.rowDetail
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaption
                wrapMode: Text.WordWrap
            }
        }
    }

    // Self-contained button so the overlay never depends on page-owned styles.
    component TourButton: Rectangle {
        id: btn
        property string label: ""
        property bool primary: false
        property bool subtle: false
        signal clicked()
        implicitWidth: Math.max(primary ? 88 : 66, btnText.implicitWidth + 26)
        implicitHeight: 34
        radius: Theme.radiusControl
        color: primary ? (mouse.containsMouse ? Theme.accentHover : Theme.accent)
               : subtle ? (mouse.containsMouse ? Theme.bgCardHover : "transparent")
               : (mouse.containsMouse ? Theme.bgCardHover : Theme.bgCard)
        border.color: primary ? "transparent" : Theme.borderSoft
        border.width: primary ? 0 : 1
        Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        Text {
            id: btnText
            anchors.centerIn: parent
            text: btn.label
            color: btn.primary ? Theme.textOnAccent : (mouse.containsMouse ? Theme.textPrimary : Theme.textSecondary)
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontBody
            font.weight: btn.primary ? Font.DemiBold : Font.Normal
            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
        }
        MouseArea {
            id: mouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: btn.clicked()
        }
    }

    // ---- lifecycle --------------------------------------------------------------
    function start() {
        meterSeen = false
        index = 0
        running = true
        applyStepPage()
        remeasure()
        forceActiveFocus()
    }

    function finish() {
        running = false
        orion.markPreflightComplete()
    }

    function goTo(i) {
        if (i < 0 || i >= steps.length)
            return
        index = i
        applyStepPage()
        remeasure()
    }

    function next() {
        if (isLast)
            finish()
        else
            goTo(index + 1)
    }

    function back() {
        if (!isFirst)
            goTo(index - 1)
    }

    function applyStepPage() {
        var p = step.page
        if (p && p.length > 0 && orion.currentPage !== p)
            orion.currentPage = p
    }

    // Resolve the current target through the registry and map its rect into overlay
    // space. Missing/hidden/zero-size -> no spotlight (caption centers). Keeps correct
    // if the anchor mounts after we open (the timer keeps re-measuring).
    function remeasure() {
        if (!running)
            return
        var key = step.target
        var it = key && key.length > 0 ? TourRegistry.item(key) : null
        if (it && it.visible && it.width > 0 && it.height > 0) {
            var p = it.mapToItem(tour, 0, 0)
            var x = p.x - holePad
            var y = p.y - holePad
            var w = it.width + holePad * 2
            var h = it.height + holePad * 2
            // Reject if it maps entirely off-screen (e.g. mid page-fade).
            if (x + w > 0 && y + h > 0 && x < tour.width && y < tour.height) {
                holeX = Math.max(0, x)
                holeY = Math.max(0, y)
                holeW = Math.min(w, tour.width - holeX)
                holeH = Math.min(h, tour.height - holeY)
                hasTarget = true
                return
            }
        }
        hasTarget = false
    }

    onIndexChanged: remeasure()

    // Continuous re-measure so the spotlight tracks page fades, scroll and layout.
    Timer {
        running: tour.running
        interval: 60
        repeat: true
        onTriggered: tour.remeasure()
    }

    // ---- keyboard ---------------------------------------------------------------
    focus: running
    Keys.onPressed: function (e) {
        switch (e.key) {
        case Qt.Key_Escape:
            tour.finish(); e.accepted = true; break
        case Qt.Key_Right:
        case Qt.Key_Down:
        case Qt.Key_Return:
        case Qt.Key_Enter:
        case Qt.Key_Space:
            tour.next(); e.accepted = true; break
        case Qt.Key_Left:
        case Qt.Key_Up:
            tour.back(); e.accepted = true; break
        }
    }

    // ---- dimming scrim (4 rects around the hole so the hole is a real cut-out) ---
    // No target -> one full scrim.
    Scrim {
        anchors.fill: parent
        visible: !tour.hasTarget
    }
    // Target -> four rects bordering the hole.
    Scrim { visible: tour.hasTarget; x: 0; y: 0; width: tour.width; height: tour.holeY }
    Scrim { visible: tour.hasTarget; x: 0; y: tour.holeY + tour.holeH; width: tour.width; height: Math.max(0, tour.height - (tour.holeY + tour.holeH)) }
    Scrim { visible: tour.hasTarget; x: 0; y: tour.holeY; width: tour.holeX; height: tour.holeH }
    Scrim { visible: tour.hasTarget; x: tour.holeX + tour.holeW; y: tour.holeY; width: Math.max(0, tour.width - (tour.holeX + tour.holeW)); height: tour.holeH }

    // ---- spotlight ring ---------------------------------------------------------
    Rectangle {
        visible: tour.hasTarget
        x: tour.holeX - 3
        y: tour.holeY - 3
        width: tour.holeW + 6
        height: tour.holeH + 6
        radius: Theme.radiusControl + 3
        color: "transparent"
        border.color: Theme.accent
        border.width: 2
        Behavior on x { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on y { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on width { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }
        Behavior on height { NumberAnimation { duration: Theme.motionFast; easing.type: Easing.OutCubic } }

        // Soft accent glow just outside the ring.
        Rectangle {
            anchors.fill: parent
            anchors.margins: -3
            radius: parent.radius + 3
            color: "transparent"
            border.color: Theme.accentGlow
            border.width: 3
            z: -1
        }
    }

    // ---- caption card -----------------------------------------------------------
    Rectangle {
        id: caption
        width: 340
        implicitHeight: captionCol.implicitHeight + 32
        height: implicitHeight
        radius: Theme.radiusCard
        color: Theme.modalSurface
        border.color: Theme.modalBorder
        border.width: 1

        // Horizontal: aligned near the target's centre, clamped on-screen; centered
        // when there's no target.
        x: {
            if (!tour.hasTarget)
                return (tour.width - width) / 2
            var want = tour.holeX + tour.holeW / 2 - width / 2
            return Math.max(16, Math.min(want, tour.width - width - 16))
        }
        // Vertical: below the target if it fits, else above, else centered.
        y: {
            if (!tour.hasTarget)
                return (tour.height - height) / 2
            var below = tour.holeY + tour.holeH + 16
            if (below + height <= tour.height - 16)
                return below
            var above = tour.holeY - height - 16
            if (above >= 16)
                return above
            return Math.max(16, (tour.height - height) / 2)
        }
        Behavior on x { NumberAnimation { duration: Theme.motionBase; easing.type: Easing.OutCubic } }
        Behavior on y { NumberAnimation { duration: Theme.motionBase; easing.type: Easing.OutCubic } }

        ColumnLayout {
            id: captionCol
            anchors.fill: parent
            anchors.margins: 16
            spacing: 10

            // Step counter + progress dots.
            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                Text {
                    text: "STEP " + (tour.index + 1) + " OF " + tour.steps.length
                    color: Theme.accentBorder
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontMicro
                    font.weight: Font.Bold
                    font.letterSpacing: 1.2
                }
                Item { Layout.fillWidth: true }
                Row {
                    spacing: 4
                    Repeater {
                        model: tour.steps.length
                        delegate: Rectangle {
                            required property int index
                            width: index === tour.index ? 16 : 6
                            height: 6
                            radius: 3
                            color: index === tour.index ? Theme.accent
                                   : index < tour.index ? Theme.accentBorder : Theme.borderStrong
                            Behavior on width { NumberAnimation { duration: Theme.motionFast } }
                            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                        }
                    }
                }
            }

            Text {
                Layout.fillWidth: true
                text: tour.step.title
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontTitle
                font.weight: Font.DemiBold
                wrapMode: Text.WordWrap
            }

            Text {
                Layout.fillWidth: true
                text: tour.step.body
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontBody
                lineHeight: 1.32
                wrapMode: Text.WordWrap
            }

            // ---- live readiness list (folded-in preflight) ----
            ColumnLayout {
                Layout.fillWidth: true
                Layout.topMargin: 2
                visible: tour.step.kind === "ready"
                spacing: 7

                ReadyRow {
                    rowLabel: "Capture device"
                    ok: tour.capturePass
                    rowDetail: !tour.captureCardSource ? "Decoder source — no capture card needed."
                               : tour.capturePass ? (orion.captureDeviceList.length + " device(s) found.")
                               : "No capture card found — check USB 3.0 + HDCP off."
                }
                ReadyRow {
                    rowLabel: "Remote Play"
                    ok: tour.remotePass
                    rowDetail: tour.remotePass ? ("Streaming — " + orion.uniqueFrameFps + " fps.")
                               : (orion.consoleIp.length === 0 ? "Set your console IP, then press Connect."
                                                               : "Press Connect to start.")
                }
                ReadyRow {
                    rowLabel: "Meter lock"
                    ok: tour.meterSeen
                    rowDetail: tour.meterSeen ? "Detector locked your meter — you're set."
                               : "Take one shot—Venice will draw a live box around the meter."
                }
            }

            // ---- controller setup (one-time USB power fix) ----
            // Primary discovery for the pad-drops-off-USB fix lives HERE in the
            // setup flow; the Live page keeps its own button for repeat use
            // (a new USB port re-surfaces the risky Windows default). The step
            // also spotlights the real Live-page button when the fix is
            // applicable; either control applies the same one-time fix.
            ColumnLayout {
                Layout.fillWidth: true
                Layout.topMargin: 2
                visible: tour.step.kind === "controller"
                spacing: 7

                ReadyRow {
                    rowLabel: "Controller USB power"
                    ok: !orion.controllerUsbPowerFixAvailable
                    rowDetail: orion.controllerUsbPowerFixAvailable
                               ? "Windows' risky default detected — the pad can drop off USB after the PC idles."
                               : "No risky USB power setting detected — nothing to fix."
                }
                TourButton {
                    visible: orion.controllerUsbPowerFixAvailable
                    label: "Fix controller USB power"
                    primary: true
                    onClicked: orion.applyControllerUsbPowerFix()
                }
            }

            // ---- controls ----
            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 4
                spacing: 8

                TourButton {
                    label: "Skip"
                    subtle: true
                    visible: !tour.isLast
                    onClicked: tour.finish()
                }
                Item { Layout.fillWidth: true }
                TourButton {
                    label: "Back"
                    subtle: true
                    visible: !tour.isFirst
                    onClicked: tour.back()
                }
                TourButton {
                    label: tour.isLast ? "Finish" : "Next"
                    primary: true
                    onClicked: tour.next()
                }
            }
        }
    }
}
