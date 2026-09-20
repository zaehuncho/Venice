import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// NO METER: the release is timed from your press and nothing else. Venice holds Square for a
// DURATION and lets go; the console sees exactly that duration, because the press and the
// release travel the same pipe and the connection latency cancels out of both ends.
//
// [ORION_NO_METER_V2 2026-09-14 owner] ONE control. hold = Release timing + per-shot-type offset
// + Rhythm offset, never below 450 ms. The slider is 1..100 over 500..797 ms (3 ms a step), so
// its SHORTEST reachable hold is 500 ms — about 110 ms clear of the shortest hold that has ever
// pump-faked. The MILLISECONDS are still the control quantity the engine reads
// (orion.noMeterHoldMs); only the display leads with the 1..100 step number.
//
// [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] ...and the caption under that number is in
// CONSOLE FRAMES ("39 fr · 650 ms"), because the console samples the pad once per frame and
// judges the release on that frame. The engine snaps the whole hold onto that grid, so the
// caption reports the frame count the console will actually count -- the difference between
// 641 ms (38.46 frames, a coin flip between two frames) and 650 ms (39.00, "basically perfect").
//
// [2026-09-14 owner] "the lead card is too long 1-100 is fine" / "remove all the unnecessary
// text" / "for no meter remove the pause timing". What is left is the whole card: one number,
// one slider, one sentence saying which way to move it, and the fade trim. Gone: the TIMING /
// PAUSED badge (the mode's own METER / NO METER switch already says which path is live), both
// InfoTip paragraphs, the shot-type note (the fade behaviour has a control of its own now), and
// the Pause row.
Card {
    id: root
    title: "No Meter"
    subtitle: "Venice releases on a timer, not the meter"
    // Content-sized: nothing here is optional, so the card is exactly as tall as its column.
    Layout.preferredHeight: col.implicitHeight + 84

    // 1..100 <-> 500..797 ms. 51 -> 650 ms, the measured standstill hold on the reference rig.
    readonly property real msLo: 500
    readonly property real msStep: 3
    function msFromValue(v) { return root.msLo + (Math.max(1, Math.min(100, v)) - 1) * root.msStep }
    function valueFromMs(ms) {
        var n = Number(ms)
        if (!isFinite(n))
            n = 650
        return Math.max(1, Math.min(100, Math.round((n - root.msLo) / root.msStep) + 1))
    }
    readonly property int shownValue: valueFromMs(orion.noMeterHoldMs)
    readonly property int liveValue: holdSlider.pressed ? Math.round(holdSlider.value) : shownValue
    readonly property int liveMs: Math.round(msFromValue(liveValue))

    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] THE CAPTION IS IN FRAMES, because the
    // console is. It samples the pad once per frame and judges the release on that frame, so a
    // hold that is not a whole number of frames lands ON a boundary and coin-flips between frame
    // N and N+1 -- 641 ms is 38.46 frames ("inconsistent"), 650 ms is 39.00 ("basically
    // perfect"). Reading "39 fr" says at a glance what "650 ms" hides.
    //
    // Between drags these come from the controller, which computes them with the ENGINE's own
    // arithmetic (floor at the pump-fake line, then snap) -- one law, not a QML re-derivation of
    // it. The live copies below exist only because settingsChanged fires on COMMIT: without them
    // the caption would freeze at the old frame count for the whole drag.
    readonly property real frameMs: {
        var f = Number(orion.consoleFrameMs)
        return (isFinite(f) && f > 0) ? f : 16.6667
    }
    readonly property bool frameQuantize: orion.noMeterFrameQuantize === true
    readonly property real liveSnappedMs: {
        var floored = Math.max(450, root.liveMs)
        if (!root.frameQuantize)
            return floored
        return Math.round(floored / root.frameMs) * root.frameMs
    }
    readonly property int shownFrames: holdSlider.pressed
        ? Math.round(root.liveSnappedMs / root.frameMs)
        : orion.noMeterHoldFrames
    readonly property real shownSnappedMs: holdSlider.pressed ? root.liveSnappedMs
                                                             : orion.noMeterHoldSnappedMs

    // [ORION_NO_METER_FADE_TRIM 2026-09-14 owner] "fades need work". -20..+20 at 3 ms a step
    // (the SAME step as Release timing, so one click means one thing on this card), applied to
    // both fades and to nothing else.
    readonly property real fadeStep: 3
    function fadeMsFromValue(v) { return Math.max(-20, Math.min(20, Math.round(v))) * root.fadeStep }
    function fadeValueFromMs(ms) {
        var n = Number(ms)
        if (!isFinite(n))
            n = 0
        return Math.max(-20, Math.min(20, Math.round(n / root.fadeStep)))
    }
    readonly property int fadeShownValue: fadeValueFromMs(orion.noMeterFadeTrimMs)
    readonly property int fadeLiveValue: fadeSlider.pressed ? Math.round(fadeSlider.value)
                                                            : fadeShownValue
    readonly property int fadeLiveMs: Math.round(fadeMsFromValue(fadeLiveValue))
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] The TRIM stays a millisecond quantity --
    // it is added to the fade delta and only the TOTAL is snapped, so the 3 ms step keeps its
    // resolution and a sub-frame nudge can still tip the total onto the next frame. What the
    // owner reads, though, is frames, because frames are what the console counts.
    readonly property int fadeLiveFrames: Math.round(root.fadeLiveMs / root.frameMs)

    ColumnLayout {
        id: col
        anchors.fill: parent
        spacing: 8

        // ONE readout: the 1..100 value big, the millisecond it means as a small caption beside
        // it. The slider is what the owner moves; the ms is what the engine holds.
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                text: "Release timing"
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
            Text {
                objectName: "noMeterHoldValue"
                text: root.liveValue
                color: holdSlider.pressed ? Theme.accent : Theme.textPrimary
                font.family: Theme.fontMono
                font.pixelSize: 24
                font.weight: Font.DemiBold
            }
            Text {
                objectName: "noMeterHoldMsCaption"
                text: root.shownFrames + " fr · " + Math.round(root.shownSnappedMs) + " ms"
                color: Theme.textFaint
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
        }

        ThemedSlider {
            id: holdSlider
            objectName: "noMeterHoldSlider"
            Layout.fillWidth: true
            Layout.minimumWidth: 240
            from: 1
            to: 100
            stepSize: 1
            snapMode: Slider.SnapAlways
            value: root.shownValue
            // Commit on release and on keyboard/step changes, never mid-drag: the same idiom
            // Shot Lead uses, so one drag is one settings write.
            onPressedChanged: if (!pressed) orion.noMeterHoldMs = root.msFromValue(value)
            onMoved: if (!pressed) orion.noMeterHoldMs = root.msFromValue(value)
            Connections {
                target: orion
                function onSettingsChanged() {
                    if (!holdSlider.pressed) {
                        holdSlider.value = root.shownValue
                    }
                }
            }
        }

        // Rails on their own slim row: the hint used to sit BETWEEN the rails and pushed the
        // column wider than the card (every wrapped note below then wrapped at that width and
        // clipped at the card edge -- seen on the 2026-09-14 relaunch).
        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Text {
                text: "1"
                color: Theme.textFaint
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
            Item { Layout.fillWidth: true }
            Text {
                text: "100"
                color: Theme.textFaint
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
        }

        Text {
            objectName: "noMeterDirectionHint"
            Layout.fillWidth: true
            text: "Shots EARLY → move right. LATE → move left."
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }

        // ---- Fades -----------------------------------------------------------------------
        // [2026-09-14 owner] "standstill shots are basically perfect but fades need work". The
        // engine already holds a fade ~280-300 ms longer than a standstill from a measured
        // table; this trims that one number for this rig, both directions at once.
        RowLayout {
            Layout.fillWidth: true
            Layout.topMargin: 4
            spacing: 8
            Text {
                text: "Fades"
                color: Theme.textPrimary
                font.family: Theme.fontUi
                font.pixelSize: 12
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
            Text {
                objectName: "noMeterFadeTrimValue"
                text: root.frameQuantize
                      ? ((root.fadeLiveFrames > 0 ? "+" : "") + root.fadeLiveFrames + " fr")
                      : ((root.fadeLiveMs > 0 ? "+" : "") + root.fadeLiveMs + " ms")
                color: fadeSlider.pressed ? Theme.accent
                       : root.fadeLiveMs === 0 ? Theme.textFaint : Theme.textSecondary
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
        }

        ThemedSlider {
            id: fadeSlider
            objectName: "noMeterFadeTrimSlider"
            Layout.fillWidth: true
            Layout.minimumWidth: 240
            from: -20
            to: 20
            stepSize: 1
            snapMode: Slider.SnapAlways
            value: root.fadeShownValue
            onPressedChanged: if (!pressed) orion.noMeterFadeTrimMs = root.fadeMsFromValue(value)
            onMoved: if (!pressed) orion.noMeterFadeTrimMs = root.fadeMsFromValue(value)
            Connections {
                target: orion
                function onSettingsChanged() {
                    if (!fadeSlider.pressed) {
                        fadeSlider.value = root.fadeShownValue
                    }
                }
            }
        }

    }
}
