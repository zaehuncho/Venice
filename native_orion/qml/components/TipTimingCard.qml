import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// [ORION_USER_TIP] Tip Timing — the jumpshot-animation control, companion to ShotLeadCard.
//
// THE TWO QUANTITIES, and why this card exists at all (do not merge it into Shot Lead):
//   * Shot Lead = the RIG. How long this machine's press takes to become a visible effect.
//     Measured per-rig; a new capture card changes it, a new jumpshot does not.
//   * Tip Timing = the GAME. The equipped jumpshot's animation length — the AIM. A different
//     jumpshot needs a different value; the same jumpshot needs the same value on any rig.
//     Venice LEARNS this from real landings (recordPhaseConstantSample), which the lead has
//     no learner for. Until this card existed the only way to adjust it was hand-editing
//     learning.json, which no customer can do.
//
// THE NUMBER SHOWN IS THE EFFECTIVE AIM (pickup prompt §9.5) — the exact ms the decision
// path consumes — not the shipped constant and not the raw learner state. All frame
// arithmetic (canonical base-30 storage, the base-20 anchor shift, the aim-preservation
// offset) lives in C++ behind orion.tipTimingMs; this file never converts anything.
//
// DIRECTION (verified against AutomationEngine, mirror-image of Shot Lead's rule):
// fireAt = anchor + tipTiming - lead, so a LARGER value releases LATER. A shot whose
// banner reads EARLY therefore needs a LATER tip timing, and vice versa. Note this is the
// OPPOSITE sign convention from Shot Lead ("landing LATE? raise the lead") — which is
// exactly why the buttons here are labelled Earlier/Later instead of +/−.
//
// STEP SIZES 1ms / 5ms, from measurement: the counted-batch landing spread is ~6-12ms sd,
// so sub-ms steps would be tuning inside the noise floor (removed as an option), 1ms is the
// finest step a banner-driven walk can resolve, and 5ms covers most of one sigma per press
// for coarse correction after a jumpshot change.
//
// DELIBERATELY NO SLIDER. Shot Lead's slider is safe because the lead is re-measurable;
// the aim is not — a stray drag here is tens of ms of guaranteed miss on every shot until
// noticed, and the useful adjustment is single-digit ms anyway. Buttons only.
//
// PRECEDENCE (the actuation_lead_user_set pattern, implemented via the existing aim
// freeze — no second mechanism):
//   * Nudging a value LOCKS it (tip_phase_aim_frozen): your value wins over the learner
//     while locked, and it survives restart (stored in the learner's own persisted slot).
//   * The Hold toggle locks/unlocks the CURRENT value without moving it.
//   * Reset (and unlocking) hands control back to the learner, which keeps adapting FROM
//     the current value — a smooth handover, never an instant re-aim.
Card {
    id: root
    title: "Tip Timing"
    // Kept SHORT deliberately: Card elides its subtitle to one line, so a longer
    // string silently truncates mid-word instead of wrapping (it was rendering as
    // "...your jumpshot's animatio…"). Detail belongs in the InfoTip, not here.
    subtitle: "Where Venice releases in your jumpshot"
    // +80 = Card header (title + subtitle + 16px margins + 12px gap).
    Layout.preferredHeight: col.implicitHeight + 80

    // Typed intermediates so every binding below is total: with a partial test stub the
    // raw properties coerce to NaN/false instead of throwing on toFixed().
    readonly property real shownTiming: orion.tipTimingMs
    readonly property bool locked: orion.tipTimingLocked
    readonly property bool userSet: orion.tipTimingUserSet
    readonly property bool learned: orion.tipTimingLearnedActive
    readonly property real bandLo: orion.tipTimingMinMs
    readonly property real bandHi: orion.tipTimingMaxMs

    // [ORION_AIM_FREEZE 2026-08-08] The instrument's own full-window measurement (effective
    // frame, from measured_phase_physical_ms) vs the value the shots are actually timed to.
    // 20ms mirrors the engine's kTipTimingDivergenceWarnMs (~4 sigma of the median's standard
    // error — a real animation mismatch, not instrument noise). Only meaningful while a
    // manual/locked value outranks the learner; unlocked, the learner converges onto the
    // measurement by itself.
    readonly property bool measuredDiverges: (userSet || locked)
                                             && orion.tipTimingMeasuredMs > 0
                                             && Math.abs(orion.tipTimingMeasuredMs - shownTiming) > 20

    ColumnLayout {
        id: col
        anchors.fill: parent
        spacing: 10

        // [ORION_AIM_FREEZE 2026-08-08] Honesty banner (idiom shared with ShotLeadCard /
        // MeterConfigPanel): the rig MEASURED the jumpshot at one value while the manual lock
        // holds another. 2026-08-08: manual 240 persisted every session while the live median
        // measured 310 — a 70ms disagreement no surface ever showed, and half of the pair that
        // made shots unschedulable. States the measurement; never adopts it.
        Rectangle {
            objectName: "tipTimingDivergenceBanner"
            visible: root.measuredDiverges
            Layout.fillWidth: true
            implicitHeight: divergenceText.implicitHeight + 16
            radius: Theme.radiusChip
            color: Theme.warningDim
            border.color: Theme.warningBorder
            border.width: 1
            Text {
                id: divergenceText
                anchors.fill: parent
                anchors.margins: 8
                wrapMode: Text.WordWrap
                color: Theme.warning
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
                text: "Venice measured your jumpshot at " + orion.tipTimingMeasuredMs.toFixed(0)
                      + " ms; shots are timed to your value of " + root.shownTiming.toFixed(0)
                      + " ms. Your value stays in charge — press Reset to use the measured "
                      + "animation instead."
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                objectName: "tipTimingValue"
                // One decimal, because the stored value genuinely carries halves (435.5 on
                // the reference rig) and a whole-ms readout would show a value the user
                // typed as changed when it is not.
                text: root.shownTiming.toFixed(1) + " ms"
                color: Theme.textPrimary
                font.family: Theme.fontMono
                font.pixelSize: 22
                font.weight: Font.DemiBold
            }

            // Learned-or-manual, stated explicitly — the value alone cannot tell the user
            // who controls it, and that ambiguity is how aims get double-corrected.
            Rectangle {
                objectName: "tipTimingStatePill"
                radius: Theme.radiusPill
                implicitHeight: 22
                implicitWidth: stateLabel.implicitWidth + 20
                color: root.userSet ? Theme.accentSoft
                       : root.locked ? Theme.accentSoft
                       : root.learned ? Theme.successDim : "transparent"
                border.width: 1
                border.color: root.userSet || root.locked ? Theme.accentBorder
                              : root.learned ? Theme.successBorder : Theme.borderSoft
                Text {
                    id: stateLabel
                    anchors.centerIn: parent
                    // ONE WORD where possible. The side panel is ~330px and this row also
                    // carries a 22px value and the reset action; the old sentence-length
                    // pills ("Locked for this session") pushed the row past the card edge
                    // and clipped the reset action off-screen. The distinction between
                    // user-set and session-locked is preserved, just tersely.
                    text: root.userSet ? "Your value"
                          : root.locked ? "Locked"
                          : root.learned ? "Learned"
                          : "Learning"
                    color: root.userSet || root.locked ? Theme.textPrimary
                           : root.learned ? Theme.success : Theme.textFaint
                    font.family: Theme.fontUi
                    font.pixelSize: Theme.fontCaption
                    font.weight: Font.DemiBold
                }
            }

            Item { Layout.fillWidth: true }

            Text {
                objectName: "tipTimingResetAction"
                visible: root.locked || root.userSet
                text: "Reset"
                color: resetHover.hovered ? Theme.accentHover : Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaption
                font.underline: resetHover.hovered
                HoverHandler { id: resetHover; cursorShape: Qt.PointingHandCursor }
                TapHandler { onTapped: orion.resetTipTiming() }
            }
        }

        // Earlier / Later nudges. Component-local button style copied from ShotLeadCard's
        // +/- buttons so the two timing cards read as one family.
        component NudgeButton: Button {
            id: nudge
            property real deltaMs: 0
            // FLEXIBLE, not fixed. A fixed 58px each put the row at roughly
            //   "Earlier"(42) + 4x58(232) + "Later"(32) + 5 gaps(40) = 346px
            // inside a ~300px usable card, so the row overflowed and the trailing
            // control was clipped off the panel edge. Filling the available width
            // makes the group fit whatever the panel is, at any font scale.
            Layout.fillWidth: true
            Layout.minimumWidth: 34
            Layout.preferredWidth: 52
            implicitHeight: 28
            hoverEnabled: true
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontCaption
            onClicked: orion.nudgeTipTimingMs(deltaMs)
            contentItem: Text {
                text: nudge.text
                color: nudge.hovered ? Theme.textPrimary : Theme.textSecondary
                font: nudge.font
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                Behavior on color { ColorAnimation { duration: Theme.motionFast } }
            }
            background: Rectangle {
                radius: Theme.radiusControl
                color: nudge.down ? Theme.bgInset : Theme.bgField
                border.width: 1
                border.color: nudge.hovered ? Theme.borderStrong : Theme.borderSoft
                Behavior on border.color { ColorAnimation { duration: Theme.motionFast } }
            }
        }

        // The Earlier/Later labels sit ABOVE the buttons rather than beside them, so the
        // button row owns the full card width. Inline labels were what pushed the row past
        // the panel edge, and they cost width that the controls themselves need.
        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                text: "← Earlier"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontMicro
                font.weight: Font.DemiBold
            }
            Item { Layout.fillWidth: true }
            Text {
                text: "Later →"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontMicro
                font.weight: Font.DemiBold
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 6
            NudgeButton { objectName: "tipTimingEarlierCoarse"; text: "− 5"; deltaMs: -5 }
            NudgeButton { objectName: "tipTimingEarlierFine"; text: "− 1"; deltaMs: -1 }
            NudgeButton { objectName: "tipTimingLaterFine"; text: "+ 1"; deltaMs: 1 }
            NudgeButton { objectName: "tipTimingLaterCoarse"; text: "+ 5"; deltaMs: 5 }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8
            Text {
                text: "Hold steady"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontCaption
            }
            InfoTip { text: "Locked: your current value is used for every shot and Venice's learner cannot move it, this session or after a restart. Unlocked: Venice keeps refining the value from your landings — required after changing your jumpshot, since a different animation needs a different timing." }
            Item { Layout.fillWidth: true }
            DashboardToggle {
                objectName: "tipTimingLockToggle"
                checked: root.locked
                onToggled: orion.tipTimingLocked = checked
            }
        }

        // THE line the whole card rests on — a user reading only this must act correctly.
        // Sign verified against the engine (see header): EARLY banner -> Later, LATE -> Earlier.
        //
        // Condensed 2026-08-06: the card ran taller than the side panel could show, so the
        // owner could not see its own controls. The DIRECTION RULE is the irreducible part
        // and stays inline; the jumpshot-change caveat and the band rationale moved into
        // the InfoTip beside it. Nothing was deleted, only relocated.
        RowLayout {
            Layout.fillWidth: true
            spacing: 6

            Text {
                objectName: "tipTimingGuidance"
                Layout.fillWidth: true
                // [ORION_CARD_TRIM 2026-08-13] Was a forced two-liner via \n; the sentence fits
                // one row and the card is tall enough already.
                text: "Watch the TIMING banner: EARLY → Later, LATE → Earlier."
                color: Theme.textSecondary
                font.family: Theme.fontUi
                font.pixelSize: Theme.fontSmall
                wrapMode: Text.WordWrap
            }

            InfoTip {
                objectName: "tipTimingGuidanceTip"
                text: "This value tracks your JUMPSHOT'S ANIMATION, so re-learn or re-tune it "
                      + "whenever you equip a different jumpshot.\n\n"
                      + "Adjustments stay inside " + root.bandLo.toFixed(0) + "–"
                      + root.bandHi.toFixed(0) + " ms — the range real jumpshot animations "
                      + "measure. Values outside it are release-killing typos, so they are "
                      + "clamped to the nearest edge."
            }
        }
    }
}
