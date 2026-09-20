import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import OrionNative

// Customer controls beside the live feed. Detection and latency learning are
// autonomous; users only identify the visual meter profile and optionally enable
// Tempo remapping. Square remains the safe default; Stick is the explicit
// alternative, and it authorizes qualified raw right-stick shot gestures.
Item {
    id: meterPanel
    property bool streamLive: false
    implicitHeight: panelColumn.implicitHeight

    ColumnLayout {
        id: panelColumn
        width: meterPanel.width
        spacing: 12

        Card {
            title: "Meter Detection"
            subtitle: "Automatic detection on every shot"
            Layout.fillWidth: true
            // Sized from content, the same +84 header overhead every Card in this panel uses:
            // the card gained a Detector row, a locked-style hint and a health line, and
            // the old fixed 180 would crush the rows into each other.
            Layout.preferredHeight: meterDetectionCol.implicitHeight + 84

            ColumnLayout {
                id: meterDetectionCol
                anchors.fill: parent
                spacing: 11

                // [METER DETECTION CARD 2026-09-10] Which proposer hands the reader its meter
                // box. "cv" = the pure-CV landmark locator (Arrow2 geometry only, ~0.5 ms);
                // "yolo" = the shipped ONNX detector (every style in the Style combo). The
                // combo shows labels; the persisted value stays "cv" | "yolo" (the C++ setter
                // normalises). The sidecar reads it ONCE at launch (ORION_METER_PROPOSER), so
                // a change applies to the next preview/stream start, not mid-session.
                // [2026-09-10 owner] YOLO shelved: Pure CV is the only path, the Style stays
                // Arrow2. (SUPERSEDED for the Style combo on 2026-09-17 -- see below.)
                readonly property bool pureCv: true

                // [ORION_PILL_YOLO_ROUTE 2026-09-17] The Style combo is NO LONGER pinned by
                // `pureCv`. Measured on 661 labelled 2K27 park frames (docs/PILL_STYLE_STATUS.md):
                // the pure-CV locator proposes a box on 0 of 642 Pill frames, and the packaged
                // detector reads 661/661 -- so a Pill player on this panel was choosing between
                // "Arrow2" (wrong style) and a hidden persisted Pill that ran blind. Picking Pill
                // here writes meter_style, and the NEXT sidecar launch routes itself onto the
                // detector that can see it (RemotePlaySession + SidecarReaderProfile.h).
                // `pureCv` is KEPT as the card's record of the shipped proposer choice (the
                // Detector combo it also dimmed was removed with the rest of the proposer
                // surface); nothing on this card binds it now that the Style combo is live.
                //
                // The combo trades in LABELS, the setting in style names: "Pill (beta)" persists
                // as "Pill". A persisted style that is neither (a 2K26 "Dial"/"Sword") displays as
                // Arrow2 without being rewritten -- the same display-only contract the lock had.
                readonly property var styleOptions: ["Arrow2", "Pill (beta)"]
                function styleLabelFor(style) {
                    return String(style).trim().toLowerCase() === "pill" ? "Pill (beta)" : "Arrow2"
                }
                function styleValueFor(label) {
                    return label === "Pill (beta)" ? "Pill" : "Arrow2"
                }


                RowLayout {
                    Layout.fillWidth: true
                    spacing: 14

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Text { text: "Style"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 11; font.weight: Font.DemiBold }
                        // The two styles the shipped build can actually READ: Arrow2 on the
                        // pure-CV locator, Pill on the packaged detector (which the launch
                        // routes to automatically when this says Pill). The retired 2K26
                        // entries (Arrow/Dial/Sword) and Straight -- which no proposer has
                        // been measured on -- stay off the list rather than offering a choice
                        // that lands the user on a blind session.
                        DashboardCombo {
                            id: meterStyleCombo
                            objectName: "meterStyleCombo"
                            Layout.fillWidth: true
                            model: meterDetectionCol.styleOptions
                            value: meterDetectionCol.styleLabelFor(orion.meterStyle)
                            onValueChanged: {
                                // Compare LABEL to LABEL, never label-to-style: a persisted
                                // 2K26 value ("Dial"/"Sword"/"Straight") displays as Arrow2,
                                // and comparing it against the style would make merely SHOWING
                                // this panel rewrite it. Only a pick that moves the label off
                                // what the setting already displays as writes the setting.
                                if (value === meterDetectionCol.styleLabelFor(orion.meterStyle))
                                    return
                                orion.meterStyle = meterDetectionCol.styleValueFor(value)
                            }
                            // Same re-sync as the Detector combo: a user pick breaks the
                            // `value` binding, so the combo must be re-pointed at the
                            // persisted style whenever the settings round trip.
                            Connections {
                                target: orion
                                function onSettingsChanged() {
                                    meterStyleCombo.value = meterDetectionCol.styleLabelFor(orion.meterStyle)
                                }
                            }
                        }
                    }

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 6
                        Text { text: "Color"; color: Theme.textMuted; font.family: Theme.fontUi; font.pixelSize: 11; font.weight: Font.DemiBold }
                        // NBA 2K27 ships a WHITE-ONLY meter (Red/Purple retired 2026-08-27). Kept as a
                        // real DashboardCombo so it sizes/aligns EXACTLY like the Style combo beside it
                        // (a custom Rectangle broke the RowLayout width split and made both look buggy),
                        // but White-only + disabled so it reads as locked and can't be changed. meterColor
                        // is forced White in case a stored Red/Purple value survives a 2K26 profile.
                        DashboardCombo {
                            Layout.fillWidth: true
                            model: ["White"]
                            value: "White"
                            enabled: false
                            opacity: 0.55        // greyed -> signals locked / not user-changeable
                            Component.onCompleted: if (orion.meterColor !== "White") orion.meterColor = "White"
                        }
                    }
                }

                // [ORION_PILL_YOLO_ROUTE 2026-09-17] Sits under the Style/Color row rather than
                // inside the Style column so the two combos keep the row height they share today.
                Text {
                    objectName: "meterStyleCaption"
                    Layout.fillWidth: true
                    text: "Pill: uses the packaged detector; timing validation in progress."
                    color: Theme.textMuted
                    wrapMode: Text.WordWrap
                    font.family: Theme.fontUi
                    font.pixelSize: 10
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
                        // Style or Color above does not match the in-game meter.
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
                        // [2026-09-14 owner] was "NO METER — CHECK COLOR". "NO METER" is now the
                        // name of a timing MODE on this page, and the mismatch is as often the
                        // Style (Pill vs Straight) as the Colour.
                        text: orion.meterBlindWarning ? "NO METER DETECTED"
                              : orion.meterConfirmed ? "METER LOCKED"
                              : meterPanel.streamLive ? "WATCHING" : "STANDBY"
                        color: orion.meterBlindWarning ? Theme.danger
                               : orion.meterConfirmed ? Theme.success : Theme.textSecondary
                        font.family: Theme.fontMono
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                    }
                }

                // The actionable half of the warning: the two settings that decide whether the
                // reader can see this meter at all, both of which sit directly above.
                Text {
                    Layout.fillWidth: true
                    visible: orion.meterBlindWarning
                    text: orion.meterBlindHint
                    color: Theme.danger
                    wrapMode: Text.WordWrap
                    font.family: Theme.fontUi
                    font.pixelSize: 10
                }

                // Reader detector health, straight from the sidecar (~2 s cadence):
                // "<Pure CV | YOLO · DirectML> · <infer> ms · <idle|pending|locked> · locks N
                // · <refused|drops> N". Native formats it; this only paints it. "--" until the
                // first report and again after the sidecar exits, so it never quotes a ghost.
                Text {
                    objectName: "detectorHealthLine"
                    Layout.fillWidth: true
                    text: orion.detectorHealthLine && orion.detectorHealthLine.length > 0
                          ? orion.detectorHealthLine : "--"
                    color: Theme.textMuted
                    elide: Text.ElideRight
                    font.family: Theme.fontMono
                    font.pixelSize: 10
                }

                // The "Live ETA / hold" toggle was removed 2026-08-06 (owner-directed) as
                // part of cutting the customer surface down to controls people actually
                // use. The overlay it governed still exists and still renders on its
                // persisted/default state (ON) — only the switch is gone, so this is a UI
                // removal, not a behaviour change. orion.showLiveMeterMetrics is now
                // UI-orphaned on the WRITE side; RemotePlayPage still READS it.

                // NOTE: the compact unboxed meter telemetry near the lock has
                // no toggle. It is drawn on exactly the same condition as the
                // detection lock itself (a fresh confirmed meter) and disappears
                // with it, so there is nothing here to switch independently.
            }
        }

        // [2026-08-26] TEMPO REMAP RETIRED at the owner's request. The card and
        // its Square/Stick input picker are gone from the UI; the backend property
        // (orion.tempoInputSource) and the engine's tempo handling are left intact
        // so nothing that reads a persisted setting changes behaviour -- this is a
        // surface removal, not a feature rip-out.

        // [ORION_RHYTHM_RESTORED 2026-09-11 owner] Rhythm is the last card in this panel.
        RhythmCard {
            objectName: "rhythmCard"
            Layout.fillWidth: true
        }

        // METER DELAY CARD SHELVED (2026-09-12, owner: "port and shelve out meter delay").
        // Measured 09-12: the delay never improved timing (it widens the meter in time but the
        // lead absorbs it 1:1) and it only ever mattered for contested shots, where the miss is
        // the precision floor on a narrower window, not a delay problem. UI removal ONLY: the
        // backend (orion.meterDelayEnabled / meterDelayMs / meterDelayAppliedNowMs, the
        // MeterDelayController, VeniceNetSvc, the D-pad Up bypass hotkey, the profile export
        // keys and the C++ property tests) is untouched, so a persisted meter_delay_enabled
        // still applies exactly as before -- it just has no control on this surface. The
        // shipped default is OFF. Put the card back from git history if it is ever wanted.

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
