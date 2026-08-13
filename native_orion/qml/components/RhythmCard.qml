import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// [ORION_RHYTHM 2026-08-13] Rhythm — how long Venice holds the right stick UP on the release
// flick, in milliseconds. Sits directly under Shot Lead because the two are the pair a user
// actually tunes: Shot Lead decides WHEN the flick starts, this decides HOW the flick reads.
//
// This control already existed, buried inside the Tempo Remap card's Tuning expander, where it
// was effectively undiscoverable. It also had a dead lower half: AutomationEngine takes
// `std::max(flickHold, releasePulseMs)` and releasePulseMs is a fixed 50.0, so every value under
// 50 executed as 50 with nothing in the UI saying so (this owner had 16 stored and had been
// running 50 the whole time). The config clamp now floors at 50, so what is shown here is what
// the engine runs -- do not lower `from` below 50 without also lowering that floor, or the dead
// zone comes straight back.
Card {
    id: root
    title: "Rhythm"
    subtitle: "How long Venice holds the stick up on the release flick"
    Layout.preferredHeight: col.implicitHeight + 84

    // Only meaningful when the tempo gesture owns the shot. A Button-mode shot never generates
    // an RS-up flick, so the control is shown disabled rather than hidden: hiding it would make
    // the setting look absent instead of inapplicable.
    readonly property bool flickActive: orion.tempoEnabled

    ColumnLayout {
        id: col
        anchors.fill: parent
        spacing: 10

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                text: "Flick"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 12
                Layout.preferredWidth: 46
            }

            Slider {
                id: rhythmSlider
                Layout.fillWidth: true
                enabled: root.flickActive
                // 50 = the engine floor (releasePulseMs). 200 covers the deliberate, slow-push
                // end of rhythm shooting; settings.json still accepts up to 500 for anyone who
                // wants to go further, and a stored value above 200 simply pins the handle --
                // nothing is written back unless the user actually moves it.
                from: 50
                to: 200
                stepSize: 1
                value: Math.max(50, Math.min(200, orion.tempoFlickHoldMs))
                onMoved: if (!pressed) orion.tempoFlickHoldMs = value
                onPressedChanged: if (!pressed) orion.tempoFlickHoldMs = value
            }

            Text {
                text: (rhythmSlider.pressed ? rhythmSlider.value : orion.tempoFlickHoldMs).toFixed(0) + " ms"
                color: Theme.textPrimary
                font.family: Theme.fontMono
                font.pixelSize: 12
                Layout.preferredWidth: 52
                horizontalAlignment: Text.AlignRight
            }
        }

        Text {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
            text: root.flickActive
                  ? "A shorter flick is a snappier push; a longer one is more deliberate. 50 ms is "
                    + "the floor — the console samples the pad on its own cadence, and an edge much "
                    + "under one 60 Hz frame (16.7 ms) can be missed entirely, which shows up as a "
                    + "rare shot that releases late for no visible reason."
                  : "Inactive: Tempo is off, so the shot is released as a button press and no stick "
                    + "flick is generated. Turn Tempo on in Tempo Remap to use this."
        }
    }
}
