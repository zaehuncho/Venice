import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Shot Lead: one 1..100 knob. Higher fires earlier.
//
// [2026-09-10 owner] The value is 1..100 ONLY. Underneath it is still the engine's actuation
// lead in milliseconds (settings.json actuation_lead_ms, 150..400 ms on the 1..100 scale, ~2.5 ms
// per step); nothing in the timing stack changed, only what the user sees. The direction hint at
// the bottom is the whole calibration procedure: read the game's TIMING banner, EARLY -> lower,
// LATE -> raise -- but only on a consistent bias over 20+ shots. [2026-09-22 RED TEAM GMC-011]
// Online late streaks are connection variance; chasing them resets the learned baseline
// (discord_launch/launch_embeds/timing_expectations.json says the same).
Card {
    id: root
    title: "Shot Lead"
    // [ORION_LEAD_BY_SOURCE 2026-09-14] The value belongs to ONE video route: the capture card
    // and Remote Play are different pipelines with different end-to-end delays, so the lead is
    // stashed per route and swapped when the user switches (AppConfigData::actuationLeadBySourceMs).
    // Naming the route here is what stops "my Shot Lead changed by itself" — it did not; the
    // user is now looking at the other route's number.
    subtitle: (orion.videoSource === "capture_card" ? "Capture card" : "Remote Play")
              + " · 1 to 100 · higher releases earlier"
    Layout.preferredHeight: col.implicitHeight + 84

    // 1..100 <-> 150..400 ms
    readonly property real msLo: 150
    readonly property real msHi: 400
    function msFromValue(v) { return Math.round(msLo + (Math.max(1, Math.min(100, v)) - 1) * (msHi - msLo) / 99) }
    function valueFromMs(ms) { return Math.max(1, Math.min(100, Math.round((ms - msLo) * 99 / (msHi - msLo)) + 1)) }

    readonly property bool configured: orion.actuationLeadMs > 0
    readonly property bool measured: orion.actuationLeadMeasuredMs > 0
    readonly property real shownLeadMs: configured
                                        ? orion.actuationLeadMs
                                        : (measured ? orion.actuationLeadMeasuredMs : 0.5 * (msLo + msHi))
    readonly property int shownValue: valueFromMs(shownLeadMs)
    // What the big number shows: the handle while it is held, the committed value otherwise.
    readonly property int liveValue: leadSlider.pressed ? Math.round(leadSlider.value) : shownValue
    readonly property bool leadConflict: configured
                                         && orion.shotLeadMaxUsableMs > 0
                                         && orion.actuationLeadMs > orion.shotLeadMaxUsableMs

    ColumnLayout {
        id: col
        anchors.fill: parent
        spacing: 8

        Rectangle {
            objectName: "shotLeadConflictBanner"
            visible: root.leadConflict
            Layout.fillWidth: true
            implicitHeight: conflictText.implicitHeight + 16
            radius: 6
            color: Theme.warningDim
            border.color: Theme.warningBorder
            border.width: 1
            Text {
                id: conflictText
                anchors.fill: parent
                anchors.margins: 8
                wrapMode: Text.WordWrap
                color: Theme.warning
                font.family: Theme.fontUi
                font.pixelSize: 12
                // [2026-09-23 copy] No engine vocabulary: Tip Timing is not on the customer screen.
                text: "Shot Lead " + root.shownValue + " is too high — Venice can't release in "
                      + "time, so shots are skipped. Lower it to "
                      + root.valueFromMs(orion.shotLeadMaxUsableMs) + " or less."
                      + (orion.shotLeadConflictMisses > 0
                         ? " " + orion.shotLeadConflictMisses + " shot(s) skipped this session."
                         : "")
            }
        }

        // [2026-09-23 owner] The "Shot Lead N disagrees with this rig's validated measurement"
        // banner is REMOVED. It was advisory only (Venice always fires with the customer's value),
        // it fired on as few as 6 shots, and it told customers their number was wrong right after
        // Calibrate my lead had set it -- the exact "is it broken?" moment we design against.
        // orion.leadAuthorityMs stays engine-side for diagnostics.

        RowLayout {
            Layout.fillWidth: true
            spacing: 10
            Text {
                objectName: "shotLeadValue"
                text: root.liveValue
                color: leadSlider.pressed ? Theme.accent
                       : root.configured ? Theme.textPrimary : Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: 22
                font.weight: Font.DemiBold
            }
            Rectangle {
                objectName: "shotLeadStatePill"
                radius: Theme.radiusPill
                implicitHeight: 22
                implicitWidth: stateLabel.implicitWidth + 20
                color: orion.actuationLeadUserSet ? Theme.accentSoft
                       : root.configured ? Theme.successDim : "transparent"
                border.width: 1
                border.color: orion.actuationLeadUserSet ? Theme.accentBorder
                              : root.configured ? Theme.successBorder : Theme.borderSoft
                Text {
                    id: stateLabel
                    anchors.centerIn: parent
                    text: orion.actuationLeadUserSet ? "Your setting"
                          : root.configured ? "Measured on your setup" : "Not set yet"
                    color: orion.actuationLeadUserSet ? Theme.textPrimary
                           : root.configured ? Theme.success : Theme.textFaint
                    font.family: Theme.fontUi
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                }
            }
            Item { Layout.fillWidth: true }
            Text {
                objectName: "shotLeadResetAction"
                visible: root.configured || orion.actuationLeadUserSet
                text: "Reset"
                color: resetHover.hovered ? Theme.accentHover : Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
                font.underline: resetHover.hovered
                HoverHandler { id: resetHover; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: orion.resetActuationLead() }
            }
        }

        ThemedSlider {
            id: leadSlider
            objectName: "shotLeadSlider"
            Layout.fillWidth: true
            Layout.minimumWidth: 160
            from: 1
            to: 100
            stepSize: 1
            snapMode: Slider.SnapAlways
            value: root.shownValue
            onPressedChanged: if (!pressed) orion.actuationLeadMs = root.msFromValue(value)
            onMoved: if (!pressed) orion.actuationLeadMs = root.msFromValue(value)
            Connections {
                target: orion
                function onSettingsChanged() {
                    if (!leadSlider.pressed) {
                        leadSlider.value = root.shownValue
                    }
                }
                function onActuationLeadMeasurementChanged() {
                    if (!leadSlider.pressed) {
                        leadSlider.value = root.shownValue
                    }
                }
            }

            // Live readout riding the handle while it is held.
            Rectangle {
                visible: leadSlider.pressed
                radius: Theme.radiusChip
                color: Theme.bgInset
                border.width: 1
                border.color: Theme.accentBorder
                implicitWidth: dragLabel.implicitWidth + 12
                implicitHeight: dragLabel.implicitHeight + 6
                x: Math.max(0, Math.min(leadSlider.width - width,
                        leadSlider.leftPadding + leadSlider.visualPosition * leadSlider.availableWidth - width / 2))
                y: -height - 4
                Text {
                    id: dragLabel
                    anchors.centerIn: parent
                    text: Math.round(leadSlider.value)
                    color: Theme.textPrimary
                    font.family: Theme.fontMono
                    font.pixelSize: 11
                    font.weight: Font.DemiBold
                }
            }
            // The highest value Tip Timing can still schedule.
            Rectangle {
                objectName: "shotLeadMaxUsableTick"
                visible: orion.shotLeadMaxUsableMs > root.msLo && orion.shotLeadMaxUsableMs < root.msHi
                width: 2
                height: 14
                radius: 1
                color: Theme.warning
                x: leadSlider.leftPadding
                   + ((root.valueFromMs(orion.shotLeadMaxUsableMs) - leadSlider.from)
                      / (leadSlider.to - leadSlider.from)) * leadSlider.availableWidth
                   - width / 2
                y: leadSlider.topPadding + leadSlider.availableHeight / 2 - height / 2
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            Text {
                text: "1"
                color: Theme.textFaint
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
            // [2026-09-23 owner] Wraps: a fixed-width line here set the whole card's minimum
            // width, so the layout overflowed the side panel and cut every line on the right.
            Text {
                objectName: "shotLeadDirectionHint"
                Layout.fillWidth: true
                Layout.minimumWidth: 0
                wrapMode: Text.WordWrap
                horizontalAlignment: Text.AlignHCenter
                text: "Consistently EARLY over 20+ shots → lower · LATE → raise"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
            }
            InfoTip {
                text: "Shot Lead is how far ahead of the meter Venice releases. Change it only if "
                      + "the game's TIMING banner is consistently EARLY or LATE over many shots "
                      + "(20 or more), never after one or two. Online, a short run of LATEs is "
                      + "normal connection variance, and moving Shot Lead resets what Venice has "
                      + "learned. Consistently EARLY: lower it. Consistently LATE: raise it. Move "
                      + "by 2 to 4 at a time."
            }
            Text {
                text: "100"
                color: Theme.textFaint
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
        }

        // ---- The auto-trim caption -----------------------------------------------------
        // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] The closed loop's ONE line of UI. The
        // slider above is still entirely the owner's: this says what the game's own TIMING
        // banner has added on top of it since the last time they moved it, and nothing else.
        // Hidden at 0 so a card with no correction running stays exactly as it was.
        Text {
            objectName: "shotLeadAutoTrimCaption"
            visible: orion.bannerLeadTrimEnabled && Math.round(orion.bannerLeadTrimMs) !== 0
            Layout.fillWidth: true
            Layout.topMargin: 2
            elide: Text.ElideRight
            // [2026-09-23 copy] In the slider's own 1..100 units, never milliseconds.
            text: "Auto-adjusting from game feedback: "
                  + (orion.bannerLeadTrimMs > 0 ? "+" : "−")
                  + Math.max(1, Math.round(Math.abs(orion.bannerLeadTrimMs) * 99 / (root.msHi - root.msLo)))
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
        }

        // ---- The auto-seed caption -----------------------------------------------------
        // [ORION_LEAD_AUTO_SEED 2026-09-15 owner] "How will every user find their tip timing
        // lead... I'm trying to get it as plug and play as possible." On an install nobody has
        // tuned, Venice flies this rig's own measured latency plus the shipped game-side aim
        // margin -- and SAYS SO, because a number that moves on its own with no explanation is
        // how a user concludes the app is broken. It disappears the instant they set a value of
        // their own: from then on the slider above is the whole answer.
        Text {
            objectName: "shotLeadAutoSeedCaption"
            visible: orion.leadAutoSeedActive && orion.leadAutoSeedMs > 0
            Layout.fillWidth: true
            Layout.topMargin: 2
            elide: Text.ElideRight
            text: orion.leadAutoSeedKind === "measured"
                  ? "Auto: set to " + root.valueFromMs(orion.leadAutoSeedMs) + " from your measured setup"
                  : "Auto: calibrating… using " + root.valueFromMs(orion.leadAutoSeedMs)
                    + " until your setup is measured"
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
        }

        // ---- Guided calibration --------------------------------------------------------
        // [ORION_LEAD_CALIBRATION 2026-09-21 owner] "figure out how the customers get their own
        // lead". The bisection has lived in the controller since 08-04 (LeadCalibrationPolicy.h,
        // begin/report/cancel below) but never had a screen. This is it: the customer takes an
        // open shot, reads the game's own TIMING banner -- the one oracle that has ever tracked
        // reality on this project -- and taps what it said. Ten to fifteen shots lock the lead;
        // every step is persisted, so closing the app mid-way keeps the progress.
        Rectangle {
            objectName: "shotLeadCalibrationPanel"
            Layout.fillWidth: true
            Layout.topMargin: 10
            implicitHeight: calCol.implicitHeight + 20
            radius: 8
            color: orion.leadCalibrationActive ? Theme.accentSoft : "transparent"
            border.width: orion.leadCalibrationActive ? 1 : 0
            border.color: Theme.accentBorder

            ColumnLayout {
                id: calCol
                anchors.fill: parent
                anchors.margins: 10
                spacing: 8

                // Idle: one button, and an honest line about why a new install should press it.
                RowLayout {
                    visible: !orion.leadCalibrationActive
                    Layout.fillWidth: true
                    spacing: 10
                    PrimaryButton {
                        objectName: "shotLeadCalibrateButton"
                        text: "Calibrate my lead"
                        enabled: orion.remoteRunning
                        onClicked: orion.beginLeadCalibration()
                    }
                    Text {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        text: !orion.remoteRunning
                              ? "Start a session first, then calibrate in shoot-around."
                              : (!root.configured && !orion.actuationLeadUserSet)
                                ? "Not set yet — 10 to 15 open shots find this rig's own number."
                                : "Re-run any time you change capture hardware or your TV."
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 11
                    }
                }

                // Active: the instruction, three verdict buttons, skip / cancel, done when locked.
                Text {
                    objectName: "shotLeadCalibrationHint"
                    visible: orion.leadCalibrationActive
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    text: orion.leadCalibrationHint
                    color: Theme.textPrimary
                    font.family: Theme.fontUi
                    font.pixelSize: 12
                }
                RowLayout {
                    visible: orion.leadCalibrationActive && !orion.leadCalibrationLocked
                    Layout.fillWidth: true
                    spacing: 8
                    // Labelled by what the BANNER said. The direction the lead moves is the
                    // policy's business (early -> smaller lead), never this screen's.
                    PrimaryButton {
                        objectName: "shotLeadVerdictEarly"
                        Layout.fillWidth: true
                        text: "EARLY"
                        onClicked: orion.reportLeadCalibrationVerdict("early")
                    }
                    PrimaryButton {
                        objectName: "shotLeadVerdictGood"
                        Layout.fillWidth: true
                        text: "GOOD"
                        onClicked: orion.reportLeadCalibrationVerdict("good")
                    }
                    PrimaryButton {
                        objectName: "shotLeadVerdictLate"
                        Layout.fillWidth: true
                        text: "LATE"
                        onClicked: orion.reportLeadCalibrationVerdict("late")
                    }
                }
                RowLayout {
                    visible: orion.leadCalibrationActive
                    Layout.fillWidth: true
                    spacing: 16
                    Text {
                        objectName: "shotLeadVerdictSkip"
                        visible: !orion.leadCalibrationLocked
                        text: "Skip — I missed the banner"
                        color: skipHover.hovered ? Theme.accentHover : Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 11
                        HoverHandler { id: skipHover }
                        TapHandler { onTapped: orion.reportLeadCalibrationVerdict("skip") }
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        objectName: "shotLeadCalibrationCancel"
                        visible: !orion.leadCalibrationLocked
                        text: "Cancel"
                        color: cancelHover.hovered ? Theme.accentHover : Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 11
                        HoverHandler { id: cancelHover }
                        TapHandler { onTapped: orion.cancelLeadCalibration() }
                    }
                    PrimaryButton {
                        objectName: "shotLeadCalibrationDone"
                        visible: orion.leadCalibrationLocked
                        text: "Done"
                        onClicked: orion.cancelLeadCalibration()
                    }
                }
            }
        }
    }
}
