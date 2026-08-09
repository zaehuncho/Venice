import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Customer controls beside the live feed. Detection and latency learning are
// autonomous; users only identify the visual meter profile and optionally enable
// Tempo remapping. Square remains the safe default; Stick and Both are explicit
// choices because they also authorize qualified raw right-stick shot gestures.
Item {
    id: meterPanel
    property bool streamLive: false
    implicitHeight: panelColumn.implicitHeight

    ColumnLayout {
        id: panelColumn
        width: meterPanel.width
        spacing: 12

        Card {
            title: "Meter Profile"
            subtitle: "Automatic detection on every shot"
            Layout.fillWidth: true
            Layout.preferredHeight: 180

            ColumnLayout {
                anchors.fill: parent
                spacing: 11

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Text { text: "Style"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 11; font.weight: Font.DemiBold }
                        DashboardCombo {
                            Layout.fillWidth: true
                            model: ["Arrow", "Arrow2", "Dial", "Pill", "Straight", "Sword"]
                            value: orion.meterStyle
                            onValueChanged: orion.meterStyle = value
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Text { text: "Color"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 11; font.weight: Font.DemiBold }
                        DashboardCombo {
                            Layout.fillWidth: true
                            // Red and Purple are the only bar colours the reader can actually
                            // detect. White/Yellow (and Orange/Cyan) were selectable here but
                            // had no reader support at all: picking one changed the HUD label,
                            // left every mask red, and detected nothing with no explanation.
                            // Nothing unsupported may be selectable.
                            model: ["Red", "Purple"]
                            value: orion.meterColor
                            onValueChanged: orion.meterColor = value
                        }
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    spacing: 8
                    Rectangle {
                        Layout.preferredWidth: 7
                        Layout.preferredHeight: 7
                        radius: 4
                        // meterBlindWarning is the HIGHEST-priority state: several whole shots
                        // were taken and the detector saw nothing, which almost always means the
                        // Color above does not match the in-game meter.
                        color: orion.meterBlindWarning ? Theme.danger
                               : orion.meterConfirmed ? Theme.success
                               : meterPanel.streamLive ? Theme.accent : Theme.textFaint
                    }
                    Text {
                        text: "AUTO DETECTION"
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.0
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: orion.meterBlindWarning ? "NO METER — CHECK COLOR"
                              : orion.meterConfirmed ? "METER LOCKED"
                              : meterPanel.streamLive ? "WATCHING" : "STANDBY"
                        color: orion.meterBlindWarning ? Theme.danger
                               : orion.meterConfirmed ? Theme.success : Theme.textSecondary
                        font.family: Theme.fontMono
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                    }
                }

                // The actionable half of the warning: which colour is set, and which to try.
                Text {
                    Layout.fillWidth: true
                    visible: orion.meterBlindWarning
                    text: orion.meterBlindHint
                    color: Theme.danger
                    wrapMode: Text.WordWrap
                    font.family: Theme.fontUi
                    font.pixelSize: 10
                }

                // The "Live ETA / hold" toggle was removed 2026-08-06 (owner-directed) as
                // part of cutting the customer surface down to controls people actually
                // use. The overlay it governed still exists and still renders on its
                // persisted/default state (ON) — only the switch is gone, so this is a UI
                // removal, not a behaviour change. orion.showLiveMeterMetrics is now
                // UI-orphaned on the WRITE side; RemotePlayPage still READS it.

                // NOTE: the meter telemetry box beside the lock has no toggle.
                // It is drawn on exactly the same condition as the detection
                // box itself (a fresh confirmed meter lock) and disappears with
                // it, so there is nothing here to switch: turning it off would
                // mean turning the detection box off.
            }
        }

        Card {
            id: tempoCard
            Layout.fillWidth: true
            // Content-derived. +32 is this Card's own margins — the header row below
            // is drawn by the card itself, so the base Card contributes no title
            // block (the old +70 was sized for one and left ~38px of dead space).
            // The ENABLE toggle still never resizes the card: the Input row disables
            // rather than hides. The rarely-touched tuning controls (Auto/Manual +
            // manual sliders) sit behind the Tuning expander, so the resting card is
            // two rows tall; expanding is an explicit user action and may resize it
            // (same class of change as the accepted Auto→Manual slider reveal).
            Layout.preferredHeight: tempoCol.implicitHeight + 32

            // Collapsed by default: Auto is the shipped default and Manual tuning
            // is a rare, deliberate act. The header still names the active mode.
            property bool tuningExpanded: false

            ColumnLayout {
                id: tempoCol
                anchors.fill: parent
                spacing: 8

                RowLayout {
                    Layout.fillWidth: true
                    Text { text: "Tempo Remap"; color: Theme.textPrimary; font.family: Theme.fontUi; font.pixelSize: 13; font.weight: Font.DemiBold }
                    Item { Layout.fillWidth: true }
                    DashboardToggle {
                        checked: orion.tempoEnabled
                        onToggled: orion.tempoEnabled = checked
                    }
                }

                RowLayout {
                    // Laid out unconditionally so the enable toggle cannot resize the card;
                    // disabled (not hidden) when Tempo is off, which also keeps the current
                    // Input selection readable instead of making the user toggle Tempo on
                    // just to see what it is set to.
                    enabled: orion.tempoEnabled
                    opacity: orion.tempoEnabled ? 1.0 : 0.4
                    Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
                    Layout.fillWidth: true
                    spacing: 8
                    Text { text: "Input"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 12; Layout.preferredWidth: 62 }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Repeater {
                            model: [
                                { l: "Square", v: "square" },
                                { l: "Stick", v: "stick" },
                                { l: "Both", v: "both" }
                            ]
                            delegate: Rectangle {
                                required property var modelData
                                Layout.fillWidth: true
                                Layout.preferredHeight: 28
                                radius: 8
                                color: orion.tempoInputSource === modelData.v ? Theme.accentSoft : Theme.bgField
                                border.color: orion.tempoInputSource === modelData.v ? Theme.accent : Theme.borderSoft
                                border.width: 1
                                Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                                Text {
                                    anchors.centerIn: parent
                                    text: modelData.l
                                    color: orion.tempoInputSource === modelData.v ? Theme.textPrimary : Theme.textMuted
                                    font.family: Theme.fontUi
                                    font.pixelSize: 12
                                    font.weight: orion.tempoInputSource === modelData.v ? Font.DemiBold : Font.Normal
                                }
                                MouseArea { anchors.fill: parent; onClicked: orion.tempoInputSource = modelData.v }
                            }
                        }
                    }
                    InfoTip { text: "Both keeps Square remapped and adds Stick and Go-To shot gestures." }
                }

                // Tuning expander header: always present so the active mode stays
                // readable at a glance; the controls only exist while open.
                RowLayout {
                    enabled: orion.tempoEnabled
                    opacity: orion.tempoEnabled ? 1.0 : 0.4
                    Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
                    Layout.fillWidth: true
                    spacing: 8
                    Text { text: "Tuning"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 12; Layout.preferredWidth: 62 }
                    Text {
                        text: orion.autoTune ? "Auto" : "Manual"
                        color: Theme.textSecondary
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                        font.weight: Font.DemiBold
                    }
                    Item { Layout.fillWidth: true }
                    Text {
                        text: tempoCard.tuningExpanded ? "−" : "+"
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: 14
                    }
                    TapHandler { onTapped: tempoCard.tuningExpanded = !tempoCard.tuningExpanded }
                }

                RowLayout {
                    visible: tempoCard.tuningExpanded
                    enabled: orion.tempoEnabled
                    opacity: orion.tempoEnabled ? 1.0 : 0.4
                    Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
                    Layout.fillWidth: true
                    spacing: 6
                    Repeater {
                        model: [ { l: "Auto", v: true }, { l: "Manual", v: false } ]
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 28
                            radius: 8
                            color: orion.autoTune === modelData.v ? Theme.accentSoft : Theme.bgField
                            border.color: orion.autoTune === modelData.v ? Theme.accent : Theme.borderSoft
                            border.width: 1
                            Behavior on color { ColorAnimation { duration: Theme.motionFast } }
                            Text {
                                anchors.centerIn: parent
                                text: modelData.l
                                color: orion.autoTune === modelData.v ? Theme.textPrimary : Theme.textMuted
                                font.family: Theme.fontUi
                                font.pixelSize: 12
                                font.weight: orion.autoTune === modelData.v ? Font.DemiBold : Font.Normal
                            }
                            MouseArea { anchors.fill: parent; onClicked: orion.autoTune = modelData.v }
                        }
                    }
                    InfoTip { text: "Auto learns from the live meter each shot. Manual freezes the values below." }
                }

                RowLayout {
                    visible: tempoCard.tuningExpanded && orion.tempoEnabled && !orion.autoTune
                    Layout.fillWidth: true
                    spacing: 10
                    Text { text: "Tempo"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 12; Layout.preferredWidth: 70 }
                    ThemedSlider {
                        id: tempoSlider
                        Layout.fillWidth: true
                        from: 16
                        to: 250
                        value: orion.tempoFlickHoldMs
                        onMoved: if (!pressed) orion.tempoFlickHoldMs = value
                        onPressedChanged: if (!pressed) orion.tempoFlickHoldMs = value
                    }
                    Text { text: (tempoSlider.pressed ? tempoSlider.value : orion.tempoFlickHoldMs).toFixed(0) + " ms"; color: Theme.textPrimary; font.family: Theme.fontMono; font.pixelSize: 12; Layout.preferredWidth: 52; horizontalAlignment: Text.AlignRight }
                }

                RowLayout {
                    visible: tempoCard.tuningExpanded && orion.tempoEnabled && !orion.autoTune
                    Layout.fillWidth: true
                    spacing: 10
                    Text { text: "Rhythm"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 12; Layout.preferredWidth: 70 }
                    ThemedSlider {
                        id: rhythmSlider
                        Layout.fillWidth: true
                        from: 0
                        to: 300
                        value: orion.tempoMinStickHoldMs
                        onMoved: if (!pressed) orion.tempoMinStickHoldMs = value
                        onPressedChanged: if (!pressed) orion.tempoMinStickHoldMs = value
                    }
                    Text { text: (rhythmSlider.pressed ? rhythmSlider.value : orion.tempoMinStickHoldMs).toFixed(0) + " ms"; color: Theme.textPrimary; font.family: Theme.fontMono; font.pixelSize: 12; Layout.preferredWidth: 52; horizontalAlignment: Text.AlignRight }
                }
            }
        }

        Card {
            title: "Meter Delay"
            subtitle: "Adds inbound network delay so the shot meter reads cleaner"
            Layout.fillWidth: true
            // 84, not the old 70: a titled+subtitled Card's real overhead is
            // 32 (margins) + ~37 (title/subtitle block) + 12 (header spacing) ≈ 81.
            // At +70 the content column ran ~11px short of its implicitHeight, so
            // the layout shaved every row a pixel or two and adjacent text visually
            // collided ("glitchy"). +84 covers the overhead with slack; surplus just
            // rests at the card bottom.
            Layout.preferredHeight: meterDelayCol.implicitHeight + 84

            ColumnLayout {
                id: meterDelayCol
                anchors.fill: parent
                spacing: 10

                // [ORION_METER_DELAY_AVAILABILITY 2026-08-08] Honesty banner. The delay
                // is actuated by the WinDivert packet bridge; current installers ship
                // and register it (packet_bridge\NexusVisionSvc.exe -> NexusVisionSvc,
                // installer/orion.iss), but older installs, partial packages and failed
                // service registrations have no backend — and without this banner the
                // toggle flips and persists while nothing downstream can ever engage
                // (the ship-blocker). The toggle stays operable so the preference
                // survives, but the card can never silently lie about a headline
                // feature.
                // [ORION_METER_DELAY_ARM_STATE 2026-08-08] Same banner, second lie: on a
                // dev rig the bridge can be RUNNING with the intercept DISARMED (nexus_svc
                // ships disarmed; arming is an out-of-band operator action). The service
                // reports that itself, and the card must show it instead of "Armed".
                Rectangle {
                    visible: !orion.meterDelayBackendAvailable
                             || (orion.meterDelayEnabled && orion.meterDelayServiceDisarmed)
                    Layout.fillWidth: true
                    implicitHeight: meterDelayUnavailableText.implicitHeight + 16
                    radius: 6
                    color: Theme.warningDim
                    border.color: Theme.warningBorder
                    border.width: 1
                    Text {
                        id: meterDelayUnavailableText
                        anchors.fill: parent
                        anchors.margins: 8
                        wrapMode: Text.WordWrap
                        color: Theme.warning
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                        text: !orion.meterDelayBackendAvailable
                              ? ("Not available on this install — the network-delay service "
                                 + "is missing, so Meter Delay has no effect. Re-run the "
                                 + "Venice installer as administrator to add it.")
                              : ("The network-delay service is running but DISARMED, so "
                                 + "Meter Delay is not being applied. Restart the bridge "
                                 + "armed (--arm-meter-delay).")
                    }
                }

                RowLayout {
                    Layout.fillWidth: true
                    Text {
                        // Just "Enable": the card's title + subtitle already name the
                        // feature; repeating "Meter Delay" here taught nothing.
                        text: "Enable"
                        color: Theme.textPrimary
                        font.family: Theme.fontUi
                        font.pixelSize: 13
                        font.weight: Font.DemiBold
                    }
                    Item { Layout.fillWidth: true }
                    DashboardToggle {
                        checked: orion.meterDelayEnabled
                        onToggled: orion.meterDelayEnabled = checked
                    }
                }

                // Live actuator state (Idle / Armed / Engaging / Active — holding N ms).
                // Doubles as the second, always-current honesty line when the backend is
                // unavailable or the connected service reports the intercept disarmed.
                Text {
                    visible: orion.meterDelayEnabled || !orion.meterDelayBackendAvailable
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    color: (orion.meterDelayBackendAvailable && !orion.meterDelayServiceDisarmed)
                           ? Theme.textMuted : Theme.warning
                    font.family: Theme.fontMono
                    font.pixelSize: 11
                    text: orion.meterDelayStatusText
                }

                // [ORION_METER_DELAY_LAYOUT 2026-08-09] Two rows, not one. This panel
                // column is 336px wide (RemotePlayPage's setup Loader) and card
                // margins leave ~302px of content; the old single row spent ~266px on
                // the label, two 44px buttons, the 54px field, "ms", the InfoTip and
                // six 8px gaps — the slider got the ~36px that remained, a nub
                // narrower than two handle widths ("sliders are tiny"). Row 1 now
                // gives the slider the whole width; row 2 carries the micro-adjust
                // controls, which also makes typed entry a deliberate act instead of
                // an accidental focus grab beside the handle. The enabled/opacity
                // dimming moved up to this column so both rows fade together.
                ColumnLayout {
                    enabled: orion.meterDelayEnabled
                    opacity: orion.meterDelayEnabled ? 1.0 : 0.4
                    Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
                    Layout.fillWidth: true
                    spacing: 6

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Text {
                            text: "Delay"
                            color: Theme.textMuted
                            font.family: Theme.fontUi
                            font.pixelSize: 12
                            Layout.preferredWidth: 42
                        }
                        // [ORION_METER_DELAY_RANGE 2026-08-08] 200-300 step 5 -> 100-600
                        // step 1 (owner: find the empirical physics ceiling, fine-tune
                        // against the other timing knobs). The +/- buttons and the
                        // exact-value field (row below) mirror ShotLeadCard's
                        // micro-adjust idiom: 500 ms across a few hundred pixels puts
                        // one pixel at ~2 ms, so 1 ms precision is unreachable by drag
                        // alone.
                        ThemedSlider {
                            id: meterDelaySlider
                            Layout.fillWidth: true
                            // The whole point of the two-row split: the track must stay
                            // usable. If some future sibling crowds this row again, the
                            // layout overflows instead of silently crushing the slider.
                            Layout.minimumWidth: 240
                            from: 100
                            to: 600
                            stepSize: 1
                            snapMode: Slider.SnapAlways
                            value: orion.meterDelayMs
                            onMoved: if (!pressed) orion.meterDelayMs = value
                            onPressedChanged: if (!pressed) orion.meterDelayMs = value
                            // Dragging a Slider overwrites `value` imperatively, which
                            // breaks the binding above — without this re-sync the handle
                            // stops tracking after the first drag, so the +/- buttons and
                            // the typed field would visibly do nothing (same fix as
                            // ShotLeadCard's slider).
                            Connections {
                                target: orion
                                function onSettingsChanged() {
                                    if (!meterDelaySlider.pressed) {
                                        meterDelaySlider.value = orion.meterDelayMs
                                    }
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8
                        Button {
                            id: meterDelayDown
                            objectName: "meterDelayDownButton"
                            text: "− 1"
                            implicitWidth: 44
                            implicitHeight: 28
                            hoverEnabled: true
                            font.family: Theme.fontUi
                            font.pixelSize: 11
                            onClicked: orion.meterDelayMs = orion.meterDelayMs - 1
                            contentItem: Text {
                                text: meterDelayDown.text
                                color: Theme.textSecondary
                                font: meterDelayDown.font
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: Theme.radiusControl
                                color: meterDelayDown.down ? Theme.bgInset : Theme.bgField
                                border.width: 1
                                border.color: meterDelayDown.hovered ? Theme.borderStrong : Theme.borderSoft
                            }
                        }
                        Button {
                            id: meterDelayUp
                            objectName: "meterDelayUpButton"
                            text: "+ 1"
                            implicitWidth: 44
                            implicitHeight: 28
                            hoverEnabled: true
                            font.family: Theme.fontUi
                            font.pixelSize: 11
                            onClicked: orion.meterDelayMs = orion.meterDelayMs + 1
                            contentItem: Text {
                                text: meterDelayUp.text
                                color: Theme.textSecondary
                                font: meterDelayUp.font
                                horizontalAlignment: Text.AlignHCenter
                                verticalAlignment: Text.AlignVCenter
                            }
                            background: Rectangle {
                                radius: Theme.radiusControl
                                color: meterDelayUp.down ? Theme.bgInset : Theme.bgField
                                border.width: 1
                                border.color: meterDelayUp.hovered ? Theme.borderStrong : Theme.borderSoft
                            }
                        }
                        Item { Layout.fillWidth: true }
                        // Exact-value entry: type a number, Enter or focus-out commits.
                        // The C++ setter clamps into [100, 600]; the binding restore
                        // makes the field snap back to the applied value either way.
                        TextField {
                            id: meterDelayField
                            objectName: "meterDelayValueField"
                            Layout.preferredWidth: 72
                            implicitHeight: 28
                            horizontalAlignment: TextInput.AlignRight
                            selectByMouse: true
                            color: Theme.textPrimary
                            font.family: Theme.fontMono
                            font.pixelSize: 12
                            validator: IntValidator { bottom: 100; top: 600 }
                            text: orion.meterDelayMs
                            onEditingFinished: {
                                var v = parseInt(text)
                                if (!isNaN(v)) {
                                    orion.meterDelayMs = Math.max(100, Math.min(600, v))
                                }
                                text = Qt.binding(function() { return orion.meterDelayMs })
                            }
                            background: Rectangle {
                                radius: Theme.radiusControl
                                color: Theme.bgField
                                border.width: 1
                                border.color: meterDelayField.activeFocus ? Theme.borderStrong : Theme.borderSoft
                            }
                        }
                        Text {
                            text: "ms"
                            color: Theme.textMuted
                            font.family: Theme.fontMono
                            font.pixelSize: 11
                        }
                        InfoTip { text: "Higher = cleaner meter but more input lag. 100-600 ms, 1 ms steps." }
                    }
                }

                // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Shot Lead offset for the DELAYED
                // condition, shown WHERE THE DELAY IS SET because it is a property of the
                // delay, not of the Shot Lead. Meter Delay holds the game-server -> console
                // packet flow: it does not delay the video and it does not delay the release
                // command, but it does change how the console responds to that release, so the
                // Shot Lead calibrated with the delay off is wrong with it on. The 2026-08-09
                // session is the whole argument: delay 175 with the delay-0 lead of 295 put 8
                // of 8 graded landings outside the good band, while the same lead with the
                // delay off held settled_fill 95-97. This offset is added ONLY while the delay
                // is actually applied, so it also survives OffenseDefense mode and the D-pad-Up
                // bypass flipping the condition mid-session — which a hand-retyped Shot Lead
                // cannot. 0 (the default) reproduces the pre-keying build exactly.
                ColumnLayout {
                    objectName: "meterDelayLeadOffsetRow"
                    enabled: orion.meterDelayEnabled
                    opacity: orion.meterDelayEnabled ? 1.0 : 0.4
                    Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
                    Layout.fillWidth: true
                    spacing: 4
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            text: "Shot Lead offset while delayed"
                            color: Theme.textPrimary
                            font.family: Theme.fontUi
                            font.pixelSize: 12
                        }
                        InfoTip {
                            text: "Added to Shot Lead ONLY while the meter delay is applied. "
                                  + "The delay changes how the console answers the release, so "
                                  + "the lead you tuned with the delay off is wrong with it on. "
                                  + "Tune it the same way as Shot Lead: read the game's TIMING "
                                  + "banner and nudge until it stops saying late/early. "
                                  + "0 = the delay-off lead is used unchanged."
                        }
                        Item { Layout.fillWidth: true }
                        TextField {
                            id: meterDelayLeadOffsetField
                            objectName: "meterDelayLeadOffsetField"
                            Layout.preferredWidth: 72
                            implicitHeight: 28
                            horizontalAlignment: TextInput.AlignRight
                            selectByMouse: true
                            color: Theme.textPrimary
                            font.family: Theme.fontMono
                            font.pixelSize: 12
                            validator: IntValidator { bottom: -200; top: 400 }
                            text: orion.meterDelayLeadOffsetMs
                            onEditingFinished: {
                                var v = parseInt(text)
                                if (!isNaN(v)) {
                                    orion.meterDelayLeadOffsetMs =
                                        Math.max(-200, Math.min(400, v))
                                }
                                text = Qt.binding(function() {
                                    return orion.meterDelayLeadOffsetMs
                                })
                            }
                            background: Rectangle {
                                radius: Theme.radiusControl
                                color: Theme.bgField
                                border.width: 1
                                border.color: meterDelayLeadOffsetField.activeFocus
                                              ? Theme.borderStrong : Theme.borderSoft
                            }
                        }
                        Text {
                            text: "ms"
                            color: Theme.textMuted
                            font.family: Theme.fontMono
                            font.pixelSize: 11
                        }
                    }
                    // The ceiling this rig can actually schedule. Above it every live tip shot
                    // aborts (fail-closed), which is the honest failure but a baffling one
                    // without the number, so the number is always on screen.
                    Text {
                        Layout.fillWidth: true
                        wrapMode: Text.WordWrap
                        font.family: Theme.fontUi
                        font.pixelSize: 11
                        color: orion.meterDelayLeadOffsetMs > orion.meterDelayLeadOffsetMaxMs
                               ? Theme.danger : Theme.textMuted
                        text: orion.meterDelayLeadOffsetMaxMs > 0
                              ? (orion.meterDelayLeadOffsetMs > orion.meterDelayLeadOffsetMaxMs
                                 ? "Above this rig's schedulable max (+"
                                   + Math.round(orion.meterDelayLeadOffsetMaxMs)
                                   + " ms) — live tip shots will abort while the delay is on."
                                 : "Schedulable up to +"
                                   + Math.round(orion.meterDelayLeadOffsetMaxMs)
                                   + " ms on this rig (Tip Timing ceiling minus your Shot Lead).")
                              : "Shot Lead already sits at the Tip Timing ceiling — no positive "
                                + "offset is schedulable. Lower Shot Lead or raise Tip Timing."
                    }
                }

                // [ORION_LEAD_CONFLICT_UI 2026-08-08 task #48] The Shot Lead ceiling, shown
                // WHERE THE DELAY IS SET: the delay consumes the same visible runway the
                // lead schedules against, so owners sweeping the delay slider see the
                // applied delay and the "shots will abort" verdict change here, live (the
                // applied value tracks the slider through the 100 ms/s ramp). Judges the
                // COMMITTED Shot Lead setting; the Shot Lead card's copy judges its own
                // slider handle.
                ShotLeadUsableMaxIndicator {
                    objectName: "meterDelayShotLeadUsableMaxIndicator"
                    Layout.fillWidth: true
                    visible: orion.meterDelayEnabled && orion.shotLeadMaxUsableMs > 0
                    leadMs: orion.actuationLeadMs
                }

                // [ORION_DEFENSE_FLAG 2026-08-08] Option B master switch. Defense
                // bypass is now MANUAL: D-pad Up (in a connected session) toggles a
                // runtime defense flag — flag ON ramps the delay to 0 while the user
                // defends, flag OFF re-applies it. This persisted toggle only decides
                // whether that hotkey exists at all; unchecked = hotkey inert, delay
                // always applied. The runtime flag itself is deliberately NOT
                // persisted (every session starts with the delay applied), which is
                // why this stayed a setting instead of being deleted (Option A) —
                // that and MeterDelaySettingsPropertyTests pinning the property.
                RowLayout {
                    enabled: orion.meterDelayEnabled
                    opacity: orion.meterDelayEnabled ? 1.0 : 0.4
                    Behavior on opacity { NumberAnimation { duration: Theme.motionFast } }
                    Layout.fillWidth: true
                    Text {
                        text: "Defense bypass (D-pad Up)"
                        color: Theme.textPrimary
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                    }
                    InfoTip {
                        text: "Press D-pad Up in-game to toggle defense mode: ON pauses "
                              + "the delay (no input lag while defending), OFF re-applies it. "
                              + "Always starts OFF (delay active) each session. "
                              + "Untick to disable the hotkey entirely."
                    }
                    Item { Layout.fillWidth: true }
                    DashboardToggle {
                        checked: orion.meterDelayBypassOnDefense
                        onToggled: orion.meterDelayBypassOnDefense = checked
                    }
                }
            }
        }

        // NETWORK CARD REMOVED ENTIRELY (2026-08-06, owner: "the network card
        // it's useless"). It showed COURT IP / RTT / JITTER / PACKETS IN / OUT
        // plus the court-discovery opt-in toggle. UI removal ONLY: the
        // RTT/network plumbing underneath (NetworkBridge, the sidecar's court
        // sampler, telemetry properties, Export Diagnostics) is consumed by the
        // estimator and diagnostics and is untouched. Consequences to know:
        //   * orion.packetCaptureEnabled lost its ONLY UI control — court
        //     discovery now simply runs at its persisted/default state (the
        //     passive-sniffing opt-in flag was DELETED in the 2026-08-08
        //     VeniceNet wave-1 cleanup; the property now rides networkEnabled,
        //     default ON, and the bridge runs whenever network or Meter Delay
        //     is enabled).
        //   * telemetryCourtIp/-Masked, courtIpVerified, rttVerified, rttMs,
        //     jitterMs, inboundPackets, outboundPackets have no QML consumers
        //     left on this panel (diagnostics export still carries them).
    }
}
