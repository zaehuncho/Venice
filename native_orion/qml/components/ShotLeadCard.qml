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
// LATE -> raise.
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
    readonly property bool leadDisagrees: configured
                                          && orion.leadAuthorityMs > 0
                                          && orion.leadAuthoritySdMs > 0
                                          && Math.abs(orion.actuationLeadMs - orion.leadAuthorityMs)
                                             > 3 * orion.leadAuthoritySdMs

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
                text: "Shot Lead " + root.shownValue + " is more than Tip Timing can schedule — "
                      + "live tip shots cannot fire and will abort. Lower it to "
                      + root.valueFromMs(orion.shotLeadMaxUsableMs) + " or less, or raise/reset Tip Timing."
                      + (orion.shotLeadConflictMisses > 0
                         ? " " + orion.shotLeadConflictMisses + " shot(s) already aborted this session."
                         : "")
            }
        }

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
                text: "Shot Lead " + root.shownValue + " disagrees with this rig's validated "
                      + "measurement of " + root.valueFromMs(orion.leadAuthorityMs) + " (from "
                      + orion.leadAuthoritySamples + " shots). Venice still fires with your value; "
                      + "the measurement is advisory."
            }
        }

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
            Layout.minimumWidth: 240
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
            Item { Layout.fillWidth: true }
            Text {
                objectName: "shotLeadDirectionHint"
                text: "Shots landing EARLY → lower it · LATE → raise it"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 11
            }
            InfoTip {
                text: "Shot Lead is how far ahead of the meter Venice releases. Read the game's "
                      + "TIMING banner over a few shots: if it says EARLY, the release came too "
                      + "soon — lower the value. If it says LATE, raise it. Move by 2 to 4 at a "
                      + "time; the sweet spot is where EARLY and LATE are equally rare."
            }
            Item { Layout.fillWidth: true }
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
            text: "Auto-trim from game feedback: "
                  + (orion.bannerLeadTrimMs > 0 ? "+" : "−")
                  + Math.abs(Math.round(orion.bannerLeadTrimMs)) + " ms"
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
                  ? "Auto: measured latency " + Math.round(orion.leadAutoSeedMeasuredMs)
                    + " ms + " + Math.round(orion.leadAutoSeedMarginMs) + " ms margin = "
                    + Math.round(orion.leadAutoSeedMs) + " ms"
                  : "Auto: calibrating… using " + Math.round(orion.leadAutoSeedMs)
                    + " ms until your latency is measured"
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
        }

    }
}
