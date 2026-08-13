import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// [ORION_USER_LEAD] Shot Lead — the one timing control a customer ever touches.
//
// The game already renders a per-shot verdict banner (TIMING: EXCELLENT / EARLY / LATE), so the
// user has a complete feedback loop with no extra instrumentation: read the banner, nudge this,
// done. Everything on this card serves that loop and nothing else.
//
// DIRECTION (verified against AutomationEngine, do not "fix" this without re-reading it):
// the fire deadline is `fireAt = predicted_tip - lead` (AutomationEngine.cpp, both scheduling
// paths), so a LARGER lead fires EARLIER. A shot that landed LATE therefore needs MORE lead.
// The live evidence agrees: at lead 300 shots read EXCELLENT, and when the estimator walked the
// lead down to 266/245 the LATE verdicts came back.
Card {
    id: root
    title: "Shot Lead"
    subtitle: "How far ahead of the meter Venice releases, in milliseconds"
    // 84, not the old 70: a titled+subtitled Card's real overhead is 32 (margins)
    // + ~37 (title/subtitle block) + 12 (header spacing) ≈ 81. At +70 the content
    // column ran ~11px short of its implicitHeight and the layout compressed the
    // rows into each other. +84 covers it with slack.
    Layout.preferredHeight: col.implicitHeight + 84

    readonly property bool configured: orion.actuationLeadMs > 0
    readonly property bool measured: orion.actuationLeadMeasuredMs > 0
    // Slider position while nothing is configured: this install's measurement if it has one,
    // otherwise the midpoint of the allowed range. Deliberately NOT a hard-coded "known good"
    // number — that would be one machine's latency shipped to everybody. Nothing is applied until
    // the user moves the control or the measurement seeds it.
    readonly property real shownLead: configured
                                      ? orion.actuationLeadMs
                                      : (measured ? orion.actuationLeadMeasuredMs
                                                  : 0.5 * (orion.actuationLeadMinMs + orion.actuationLeadMaxMs))

    // [ORION_LEAD_CONFLICT 2026-08-08] The applied lead vs the largest lead the tip-phase path
    // can schedule under the ACTIVE Tip Timing (tipTimingMs - 30ms margin, computed in C++).
    // Reactive on purpose: the banner appears the moment the PAIR conflicts (either control)
    // and clears the moment either control fixes it — no engine round-trip, no stale latch.
    readonly property bool leadConflict: configured
                                         && orion.shotLeadMaxUsableMs > 0
                                         && orion.actuationLeadMs > orion.shotLeadMaxUsableMs
    // [ORION_LEAD_CONFLICT] Validated-posterior disagreement badge: the 3*sd test the engine
    // uses, re-run against the LIVE slider value so moving the lead back inside the band hides
    // it immediately. Stats arrive only after the engine has diagnosed a disagreement once.
    readonly property bool leadDisagrees: configured
                                          && orion.leadAuthorityMs > 0
                                          && orion.leadAuthoritySdMs > 0
                                          && Math.abs(orion.actuationLeadMs - orion.leadAuthorityMs)
                                             > 3 * orion.leadAuthoritySdMs

    ColumnLayout {
        id: col
        anchors.fill: parent
        spacing: 10

        // [ORION_LEAD_CONFLICT 2026-08-08] Honesty banner (idiom shared with MeterConfigPanel's
        // Meter Delay card). This is the warning whose absence cost a whole play session: with
        // Shot Lead above what Tip Timing can schedule, EVERY live tip shot aborts
        // live_tip_deadline_missed and the only feedback was a cryptic reason code plus a bogus
        // LATE verdict that pushed the lead even higher. Persistent while the pair conflicts;
        // never a toast, never just a log line. Advisory only — nothing clamps either control.
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
                text: "Shot Lead " + orion.actuationLeadMs.toFixed(0) + " ms is more than Tip "
                      + "Timing " + orion.tipTimingMs.toFixed(0) + " ms can schedule — live tip "
                      + "shots cannot fire and will abort. Lower Shot Lead to "
                      + orion.shotLeadMaxUsableMs.toFixed(0) + " ms or less, or raise/reset "
                      + "Tip Timing."
                      + (orion.shotLeadConflictMisses > 0
                         ? " " + orion.shotLeadConflictMisses + " shot(s) already aborted this session."
                         : "")
            }
        }

        // [ORION_LEAD_CONFLICT] Second honesty banner: the user's lead vs this rig's VALIDATED
        // latency measurement (2026-08-08: validated 197.1 sd=3.2 vs user 320 and nothing
        // surfaced it). The user's value still wins — this states the disagreement, it never
        // adopts the measurement.
        Rectangle {
            objectName: "shotLeadDisagreementBanner"
            visible: root.leadDisagrees
            Layout.fillWidth: true
            implicitHeight: disagreementText.implicitHeight + 16
            radius: 6
            color: Theme.warningDim
            border.color: Theme.warningBorder
            border.width: 1
            Text {
                id: disagreementText
                anchors.fill: parent
                anchors.margins: 8
                wrapMode: Text.WordWrap
                color: Theme.warning
                font.family: Theme.fontUi
                font.pixelSize: 12
                text: "Shot Lead " + orion.actuationLeadMs.toFixed(0) + " ms disagrees with this "
                      + "rig's validated measurement of " + orion.leadAuthorityMs.toFixed(1)
                      + " ± " + orion.leadAuthoritySdMs.toFixed(1) + " ms (from "
                      + orion.leadAuthoritySamples + " shots). Venice still fires with your "
                      + "value; the measurement is advisory."
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 10

            Text {
                objectName: "shotLeadValue"
                text: root.shownLead.toFixed(0) + " ms"
                color: root.configured ? Theme.textPrimary : Theme.textMuted
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

        // [ORION_LEAD_LAYOUT 2026-08-09] The slider gets a row of its own. This card
        // lives in a 336px panel column (~302px of card content); the old single row
        // spent ~236px on the two 66px buttons, the 54px field and "ms" — leaving the
        // slider a ~66px track over a 100-800ms range ("sliders are tiny"). The
        // micro-adjust controls now sit on their own row below, which also makes the
        // typed field a deliberate target instead of an accidental focus grab.
        ThemedSlider {
            id: leadSlider
            objectName: "shotLeadSlider"
            Layout.fillWidth: true
            // Keeps the track usable if a future sibling crowds this row again:
            // the layout overflows instead of silently crushing the slider.
            Layout.minimumWidth: 240
            from: orion.actuationLeadMinMs
            to: orion.actuationLeadMaxMs
            stepSize: 1
            snapMode: Slider.SnapAlways
            value: root.shownLead
            onMoved: if (!pressed) orion.actuationLeadMs = value
            onPressedChanged: if (!pressed) orion.actuationLeadMs = value
            // Dragging a Slider overwrites `value` imperatively, which breaks the binding
            // above. Without this the handle would stop tracking the setting after the first
            // drag — so Reset, and the +/- buttons, would visibly do nothing.
            Connections {
                target: orion
                function onSettingsChanged() {
                    if (!leadSlider.pressed) {
                        leadSlider.value = root.shownLead
                    }
                }
                function onActuationLeadMeasurementChanged() {
                    if (!leadSlider.pressed) {
                        leadSlider.value = root.shownLead
                    }
                }
            }

            // [ORION_LEAD_CONFLICT 2026-08-08] Max-usable tick: the largest lead the ACTIVE
            // Tip Timing can schedule, drawn on the track so the trap boundary is visible
            // BEFORE the handle crosses it. Marker only — the slider is deliberately NOT
            // clamped to it (both values are user-set; an honest refusal beats a silent
            // re-time).
            Rectangle {
                objectName: "shotLeadMaxUsableTick"
                visible: orion.shotLeadMaxUsableMs > leadSlider.from
                         && orion.shotLeadMaxUsableMs < leadSlider.to
                width: 2
                height: 14
                radius: 1
                color: Theme.warning
                x: leadSlider.leftPadding
                   + ((orion.shotLeadMaxUsableMs - leadSlider.from)
                      / (leadSlider.to - leadSlider.from)) * leadSlider.availableWidth
                   - width / 2
                y: leadSlider.topPadding + leadSlider.availableHeight / 2 - height / 2
            }
        }

        RowLayout {
            Layout.fillWidth: true
            spacing: 8

            // The +/- buttons are not decoration. The useful adjustment granularity here is
            // single-digit ms, and a 300ms range across a few hundred pixels puts one pixel at
            // roughly one millisecond — reachable by luck, not by intent.
            Button {
                id: downButton
                objectName: "shotLeadDownButton"
                text: "− 1 ms"
                implicitWidth: 66
                implicitHeight: 28
                hoverEnabled: true
                font.family: Theme.fontUi
                font.pixelSize: 11
                onClicked: orion.nudgeActuationLeadMs(-1)
                contentItem: Text {
                    text: downButton.text
                    color: Theme.textSecondary
                    font: downButton.font
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: downButton.down ? Theme.bgInset : Theme.bgField
                    border.width: 1
                    border.color: downButton.hovered ? Theme.borderStrong : Theme.borderSoft
                }
            }

            Button {
                id: upButton
                objectName: "shotLeadUpButton"
                text: "+ 1 ms"
                implicitWidth: 66
                implicitHeight: 28
                hoverEnabled: true
                font.family: Theme.fontUi
                font.pixelSize: 11
                onClicked: orion.nudgeActuationLeadMs(1)
                contentItem: Text {
                    text: upButton.text
                    color: Theme.textSecondary
                    font: upButton.font
                    horizontalAlignment: Text.AlignHCenter
                    verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: upButton.down ? Theme.bgInset : Theme.bgField
                    border.width: 1
                    border.color: upButton.hovered ? Theme.borderStrong : Theme.borderSoft
                }
            }

            Item { Layout.fillWidth: true }

            // [ORION_FINE_TUNE 2026-08-08] Exact-value entry (owner request: type the
            // lead directly while sweeping meter-delay values instead of pixel-hunting
            // the slider). Enter or focus-out commits; the C++ setter path clamps to
            // [actuationLeadMinMs, actuationLeadMaxMs]; the binding restore snaps the
            // field to whatever was actually applied.
            TextField {
                id: leadField
                objectName: "shotLeadValueField"
                Layout.preferredWidth: 72
                implicitHeight: 28
                horizontalAlignment: TextInput.AlignRight
                selectByMouse: true
                color: Theme.textPrimary
                font.family: Theme.fontMono
                font.pixelSize: 12
                validator: IntValidator {
                    bottom: Math.round(orion.actuationLeadMinMs)
                    top: Math.round(orion.actuationLeadMaxMs)
                }
                text: root.shownLead.toFixed(0)
                onEditingFinished: {
                    var v = parseInt(text)
                    if (!isNaN(v)) {
                        orion.actuationLeadMs = Math.max(orion.actuationLeadMinMs,
                                                         Math.min(orion.actuationLeadMaxMs, v))
                    }
                    text = Qt.binding(function() { return root.shownLead.toFixed(0) })
                }
                background: Rectangle {
                    radius: Theme.radiusControl
                    color: Theme.bgField
                    border.width: 1
                    border.color: leadField.activeFocus ? Theme.borderStrong : Theme.borderSoft
                }
            }
            Text {
                text: "ms"
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: 11
            }
        }

        // [ORION_LEAD_CONFLICT_UI 2026-08-08 task #48] Compact usable-max readout + soft
        // clamp, judged against the LIVE handle position (root.shownLead) so the red state
        // and the "Clamp to safe" action appear while dragging, before the value commits.
        // Delay-aware: names the applied meter delay eating the runway and the max delay
        // that would make this lead schedulable again.
        ShotLeadUsableMaxIndicator {
            objectName: "shotLeadUsableMaxIndicator"
            Layout.fillWidth: true
            leadMs: leadSlider.pressed ? leadSlider.value : root.shownLead
        }

        // [ORION_LEAD_CONFLICT 2026-08-08] Names the tick above. Shown only while the boundary
        // actually cuts the slider's range (a Tip Timing long enough to schedule every lead has
        // no trap to mark).
        Text {
            objectName: "shotLeadMaxUsableCaption"
            visible: orion.shotLeadMaxUsableMs > 0
                     && orion.shotLeadMaxUsableMs < orion.actuationLeadMaxMs
            Layout.fillWidth: true
            // [ORION_CARD_TRIM 2026-08-13] One line, not three. The mark on the slider already
            // shows WHERE; this only has to say what crossing it costs.
            text: "Above " + orion.shotLeadMaxUsableMs.toFixed(0) + " ms (the slider mark), "
                  + "live tip shots abort."
            color: Theme.textFaint
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }

        // THE line the whole feature rests on. A user who reads only this sentence must be able to
        // act correctly, and the direction in it is the one verified in the header comment above.
        Text {
            objectName: "shotLeadGuidance"
            Layout.fillWidth: true
            // [ORION_CARD_TRIM 2026-08-13] Two lines instead of three. The direction is the whole
            // point of this sentence, so it stays; "a few ms at a time" is implied by ±1 buttons.
            text: "Watch the game's TIMING banner: landing LATE? Raise this. EARLY? Lower it."
            color: Theme.textSecondary
            font.family: Theme.fontUi
            font.pixelSize: 12
            wrapMode: Text.WordWrap
        }

        // TRUTHFUL COPY (2026-08-06). The unmeasured branch used to promise "It fills
        // this in for you after about a dozen shots" unconditionally. That was
        // unreachable by construction on a fresh install: the seed fires from
        // actuationLeadMeasured, which needs BOT releases, which the measured-lead
        // authority gate is refusing until timing is ready — a chicken-and-egg the
        // old copy papered over. The promise is now made only while timing is
        // actually Ready (the bot can release, so measurement can happen); before
        // that, the card says what the user can genuinely do: tune it by hand.
        Text {
            objectName: "shotLeadMeasurement"
            Layout.fillWidth: true
            // [ORION_CARD_TRIM 2026-08-13] Same three states, one line each.
            text: root.measured
                  ? ("Measured " + orion.actuationLeadMeasuredMs.toFixed(0) + " ms over "
                     + orion.actuationLeadMeasuredSamples + " shots"
                     + (orion.actuationLeadUserSet ? " — yours is used instead." : "."))
                  : (orion.latencyCalibrationReady
                     ? "Venice fills this in after about a dozen bot shots."
                     : "Timing still warming up — set this by hand for now.")
            color: Theme.textFaint
            font.family: Theme.fontUi
            font.pixelSize: 11
            wrapMode: Text.WordWrap
        }
    }
}
