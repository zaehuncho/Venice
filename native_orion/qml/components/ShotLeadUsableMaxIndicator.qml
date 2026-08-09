import QtQuick
import QtQuick.Layouts
import OrionNative

// [ORION_LEAD_CONFLICT_UI 2026-08-08 task #48] Compact usable-max readout + SOFT clamp.
//
// The trap this closes: with meter delay engaged the correct Shot Lead grows to loop+delay,
// but the largest lead the tip path can schedule (usable max = Tip Timing - 30 ms margin)
// does NOT grow with it — every tip estimate is dated from the DELAYED video, so the visible
// runway stays the tip constant (verified 2026-08-08; extending usable max by the delay was
// refuted). A lead above usable max makes every tip shot abort, and before this indicator the
// only surface was a log line plus a banner the user had to connect to the delay themselves.
//
// SOFT clamp by deliberate choice (option b): the slider still goes above usable max — the
// owner wants the real ceiling visible while sweeping delay values — but the moment the pair
// conflicts this turns red, says exactly why with the delay named, and offers a one-click
// "Clamp to safe". Nothing here ever moves the control on its own.
//
// The warning sentence comes from C++ (orion.shotLeadUsableMaxWarning ->
// orion::shotLeadUsableMaxWarningLine) so tests pin the shipped composition; the binding
// stays reactive because its ARGUMENTS are notifying properties (lead / usable max / the
// live applied delay, which updates every 50 ms ramp tick via meterDelayStatusTextChanged).
ColumnLayout {
    id: root

    // The lead this indicator judges. Callers with a live slider pass their shown value so
    // the verdict tracks the handle; defaults to the applied setting.
    property real leadMs: orion.actuationLeadMs

    readonly property real usableMaxMs: orion.shotLeadMaxUsableMs
    readonly property real appliedDelayMs: Math.max(0, orion.meterDelayAppliedNowMs)
    readonly property string warningText:
        orion.shotLeadUsableMaxWarning(root.leadMs, root.usableMaxMs, root.appliedDelayMs)
    readonly property bool aboveUsableMax: warningText.length > 0

    visible: usableMaxMs > 0
    spacing: 2

    RowLayout {
        Layout.fillWidth: true
        spacing: 8

        Text {
            objectName: "shotLeadUsableMaxReadout"
            // fillWidth + elide, not a fixed-width label beside a spacer: the
            // " · meter delay N ms applied" suffix appears and lengthens live while
            // the ramp runs, and with "Clamp to safe (...)" also visible the pair
            // could overflow the ~302px card content width. Eliding the readout
            // keeps the clamp action reachable at the row's right edge no matter
            // what the live suffix does.
            Layout.fillWidth: true
            elide: Text.ElideRight
            text: "Usable max " + root.usableMaxMs.toFixed(0) + " ms"
                  + (root.appliedDelayMs > 0
                     ? " · meter delay " + root.appliedDelayMs.toFixed(0) + " ms applied"
                     : "")
            color: root.aboveUsableMax ? Theme.danger : Theme.textFaint
            font.family: Theme.fontUi
            font.pixelSize: 11
            font.weight: root.aboveUsableMax ? Font.DemiBold : Font.Normal
        }

        Text {
            objectName: "shotLeadClampToSafeAction"
            visible: root.aboveUsableMax
            text: "Clamp to safe (" + root.usableMaxMs.toFixed(0) + " ms)"
            color: clampHover.hovered ? Theme.accentHover : Theme.danger
            font.family: Theme.fontUi
            font.pixelSize: 11
            font.underline: clampHover.hovered
            HoverHandler { id: clampHover; cursorShape: Qt.PointingHandCursor }
            TapHandler {
                onTapped: orion.actuationLeadMs = Math.max(
                              orion.actuationLeadMinMs,
                              Math.min(orion.actuationLeadMaxMs, root.usableMaxMs))
            }
        }
    }

    // [ORION_LEAD_CONFLICT_UI 2026-08-09] Reserved warning slot. The red line's
    // visibility tracks the LIVE applied delay, which updates every 50ms ramp tick
    // (and, in ShotLeadCard, the slider handle mid-drag) — when the line inserted
    // and removed itself from the layout, the whole embedding card grew and shrank
    // at that cadence and every sibling card below reflowed ("cards are glitchy").
    // The slot now ALWAYS occupies the tallest height the C++ composer can produce
    // at this width, measured by the two invisible probes below (one per sentence
    // branch: with-delay and no-delay), so state flips repaint the line without
    // ever resizing the card. The probes call the same shipped composer with
    // ceiling arguments (lead above max at the 600ms delay cap) rather than a
    // hand-copied string, so the reservation cannot drift from the real wording.
    Item {
        objectName: "shotLeadUsableMaxWarningSlot"
        Layout.fillWidth: true
        Layout.preferredHeight: Math.max(warningMeasureDelay.implicitHeight,
                                         warningMeasureNoDelay.implicitHeight)
        Layout.minimumHeight: Math.max(warningMeasureDelay.implicitHeight,
                                       warningMeasureNoDelay.implicitHeight)

        // Probe width falls back to a plausible card-content width during the first
        // layout pass: at width 0 a wrapping Text measures one word per line and the
        // reserved height would flash tens of lines tall before settling.
        Text {
            id: warningMeasureDelay
            visible: false
            width: parent.width > 0 ? parent.width : 300
            text: orion.shotLeadUsableMaxWarning(root.usableMaxMs + 1, root.usableMaxMs, 600)
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
        Text {
            id: warningMeasureNoDelay
            visible: false
            width: parent.width > 0 ? parent.width : 300
            text: orion.shotLeadUsableMaxWarning(root.usableMaxMs + 1, root.usableMaxMs, 0)
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }

        Text {
            objectName: "shotLeadUsableMaxWarning"
            visible: root.aboveUsableMax
            width: parent.width
            text: root.warningText
            color: Theme.danger
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
    }
}
