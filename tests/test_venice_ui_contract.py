from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
QML = ROOT / "native_orion" / "qml"


def source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_venice_brand_and_palette_are_centralized() -> None:
    theme = source("native_orion/qml/Theme.qml")
    assert 'productName: "Venice"' in theme
    assert 'productMark: "VENICE"' in theme
    assert 'brandAccent: "#2D7DFF"' in theme
    assert 'bgShell: "#020509"' in theme
    assert 'bgSidebar: "#050B14"' in theme
    # Detection-box stroke colour, owner-directed four times:
    #   2026-08-04 blue -> violet #CC44FF (sampled from their reference screenshot, hue 285 deg)
    #   2026-08-06 violet -> bright blue #00A8FF (owner: "bright blue meter detection box as the
    #     default"), with a legacy-default migration in AppConfig so the persisted #CC44FF flips
    #     over while any OTHER persisted hex stays an explicit user pick.
    #   2026-08-31 bright blue -> vivid magenta #FF2BD6.
    #   2026-09-14 magenta -> blue #1E90FF (owner: blue, visible).
    # meterLockKeyline survives the changes and is the load-bearing part: it is the dark
    # inner/outer ring that keeps the stroke legible against a floodlit white court, where a
    # bare saturated stroke dissolves -- which is what sank the styling before it existed.
    # It tracks the stroke's hue (dark navy under a blue stroke) so the pair reads as one mark.
    assert 'meterLock: "#1E90FF"' in theme
    assert 'meterLockKeyline: "#06182F"' in theme


def test_launcher_uses_static_venice_presentation() -> None:
    main = source("native_orion/qml/Main.qml")
    assert "VeniceBackdrop {" in main
    assert "text: Theme.productName" in main
    assert "Theme.accent = Theme.brandAccent" in main
    # DELIBERATE CONTRACT CHANGE 2026-08-06: this used to assert `title: "Orion"`,
    # described as a hidden compatibility handle for orion:// forwarding rather than
    # customer chrome. That was wrong about one thing — the window title IS customer
    # chrome: it is what the taskbar, Alt-Tab and Task Manager display, so the app
    # still read "Orion" everywhere outside its own painted UI. Retitled to Venice.
    #
    # The title doubles as the deep-link lookup handle, so main.cpp's FindWindowW
    # must match it exactly; that pairing is asserted in the runtime-contract test
    # below so the two can never drift apart.
    assert 'title: "Venice"' in main

    for page in ("AuthGate.qml", "LegalGate.qml", "StreamSetupGate.qml", "UpdateGatePage.qml"):
        content = source(f"native_orion/qml/pages/{page}")
        assert "VeniceBackdrop {" in content
        assert "StarBackdrop {" not in content

    backdrop = source("native_orion/qml/components/VeniceBackdrop.qml")
    assert "Canvas {" not in backdrop
    assert "Timer {" not in backdrop
    assert "Animation {" not in backdrop


def test_customer_navigation_is_compact_and_compatible() -> None:
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    # [OVERVIEW TAB REMOVED 2026-09-14 owner] ("general", "Overview") used to be the
    # second entry. The page duplicated Setup/Live and was deleted with GeneralPage.qml.
    # [PROFILE TAB REMOVED 2026-09-15 owner: "remove the profile tab, license info and
    # days left should be displayed on the bottom left corner"] ("profile", "Profile")
    # briefly sat between Setup and Updates. Three customer tabs again; the account
    # truth moved into the footer licence strip asserted below.
    for key, label in (
        ("remotePlay", "Live"),
        ("dashboard", "Setup"),
        ("patchNotes", "Updates"),
    ):
        assert f'key: "{key}", label: "{label}"' in sidebar
    assert 'label: "Overview"' not in sidebar
    assert 'label: "Profile"' not in sidebar
    assert 'key: "profile"' not in sidebar
    # Order matters: the build you are running reads last.
    assert (
        sidebar.index('key: "remotePlay"')
        < sidebar.index('key: "dashboard"')
        < sidebar.index('key: "patchNotes"')
    )
    # [ORION_UI_BUBBLES 2026-09-15 owner] The "Quick Start" footer button is gone —
    # see test_footer_bubbles_are_gone_and_the_tour_is_first_launch_only below.
    assert 'text: "Quick Start"' not in sidebar
    assert "controllerIsPhysicalSony" not in sidebar
    assert 'label: "Controller"' not in sidebar
    assert 'label: "Version"' not in sidebar
    # [SIDEBAR BRAND BLOCK REMOVED 2026-09-14 owner] The logo tile plus the
    # "VENICE" / "Precision timing" lockup are gone from the nav rail. The Theme
    # tokens stay (the gates still render them) — see the palette test above.
    assert "Theme.productMark" not in sidebar
    assert "Theme.productTagline" not in sidebar
    assert "orion.iconSource" not in sidebar


def test_meter_controls_are_autonomous_not_a_setup_batch() -> None:
    meter = source("native_orion/qml/components/MeterConfigPanel.qml")
    # [METER DETECTION CARD 2026-09-10 owner] the card was renamed from "Meter Profile".
    assert 'title: "Meter Detection"' in meter
    assert 'subtitle: "Automatic detection on every shot"' in meter
    assert 'text: "Style"' in meter
    assert 'text: "Color"' in meter

    assert "DashboardCombo" in meter
    # The BACKEND still understands "both" (AppConfig normalization, RemotePlaySession,
    # AutomationEngine input_mode), so a settings.json carrying it keeps working -- and the combo
    # must still SHOW it while it is the live value, or such an install would silently read
    # "Square" over a stored "both".
    assert "Calibrate Meter" not in meter
    assert "trainRequested" not in meter
    assert "meterCalibration" not in meter

    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    assert "startMeterCalibration" not in live
    assert 'title: "Live Timing"' not in live
    assert 'label: "Video"' not in live
    assert 'label: "Controller"' not in live
    # 2026-08-06 added a display-only Timing readiness pill (objectName
    # liveTimingStatus) plus a warming-up banner over the capture, because a
    # cold install fails closed and the only readiness surface was on Setup.
    # [2026-09-12 owner] the banner went first ("TIMING WARMING UP — SHOTS STAY MANUAL":
    # with the user lead authoritative from the first press the state is momentary and
    # it read as a fault).
    # [2026-09-14 owner] the pill follows it. With the banner gone the pill pointed at
    # nothing, and "Timing: Route unavailable" is internal vocabulary sitting permanently
    # beside the stream. Both readiness surfaces are now gone from the Live page; the
    # engine property they mirrored (orion.latencyCalibrationReady) is untouched and
    # simply has no QML consumer. The old prohibition this replaced was against a manual
    # timing CONTROL surface, still asserted above (startMeterCalibration / "Live Timing").
    assert 'objectName: "liveTimingStatus"' not in live
    assert 'objectName: "timingWarmupBanner"' not in live
    assert "TIMING WARM-UP BANNER: REMOVED" in live
    assert "LIVE TIMING PILL REMOVED" in live
    assert 'label: "Timing"' not in live
    # [2026-09-14 owner] the connect CTA is plain "Connect" / "Connecting…" / "Connected";
    # the "Enable Bot + Controller" phrasing is gone from every QML surface.
    assert 'text: root.connecting ? "Connecting…"' in live
    assert "Enable Bot" not in live
    assert "Bot + Controller" not in live
    # [2026-09-14 owner] the amber passive-preview chip over the pre-connect capture is
    # deleted; it only restated the Connect button beside it.
    assert "PREVIEW-ONLY BADGE REMOVED" in live
    assert "id: previewOnlyLabel" not in live


def test_meter_style_combo_offers_arrow2_only_after_pill_withdrawal() -> None:
    """[ORION_PILL_REMOVED 2026-09-21 owner]

    History: 2026-09-17 the Style combo offered "Pill (beta)" and a Pill launch was
    routed onto the packaged (Pill-trained) detector (ORION_PILL_YOLO_ROUTE), because
    the CV contour locator proposes a box on 0 of 642 labelled Pill frames. On
    2026-09-21 the owner withdrew Pill from the beta ("remove the pill option").

    The contract now: the combo offers Arrow2 ONLY; a persisted "Pill" is MIGRATED to
    Arrow2 on load (AppConfig) and cannot be set (OrionAppController::setMeterStyle);
    the Pill -> yolo route stays COMPILED (its unit tests still exercise the pure
    function) but is unreachable from any settings file. The combo still WRITES the
    setting and the old `pureCv` pin must not return.
    """
    meter = source("native_orion/qml/components/MeterConfigPanel.qml")

    # Arrow2 is the only offered style: the two-entry list is gone and no label maps
    # to the style name "Pill" any more. (Matched structurally, not as a bare
    # substring -- the withdrawal note in the QML legitimately names the old label.)
    # [ORION_PILL_REMOVED, re-withdrawn 2026-09-23] Arrow2 is the only offered style.
    assert 'readonly property var styleOptions: ["Arrow2"]' in meter
    assert 'styleOptions: ["Arrow2", "Pill (beta)"]' not in meter
    assert "model: meterDetectionCol.styleOptions" in meter
    # Picking WRITES the setting, and the label-to-label guard keeps merely SHOWING
    # the panel from rewriting a persisted style that is not on the list.
    assert "orion.meterStyle = meterDetectionCol.styleValueFor(value)" in meter
    assert "if (value === meterDetectionCol.styleLabelFor(orion.meterStyle))" in meter
    # The single-entry pin and the disabled/dimmed lock are still gone.
    assert "model: meterDetectionCol.pureCv" not in meter
    assert 'value: meterDetectionCol.pureCv ? "Arrow2" : orion.meterStyle' not in meter
    assert "enabled: !meterDetectionCol.pureCv" not in meter
    # The caption keeps its objectName for the panel probes; its text no longer sells Pill.
    assert 'objectName: "meterStyleCaption"' in meter
    assert "Pill: uses the packaged detector" not in meter
    assert 'objectName: "meterStyleCombo"' in meter

    # MIGRATION GUARD: the one shared rule does not accept "pill" (AppConfig.h normalizedMeterStyle).
    header = source("native_orion/src/AppConfig.h")
    assert 'lower == QLatin1String("pill")' not in header

    # The native route half stays compiled and its kill-switch key still round-trips
    # (dormant, not deleted -- a deliberate beta decision, easy to re-enable).
    profile = source("native_orion/src/SidecarReaderProfile.h")
    assert 'kMeterStyleEnvKey[] = "ORION_METER_STYLE"' in profile
    assert 'kPillYoloRouteEnvKey[] = "ORION_PILL_YOLO_ROUTE"' in profile
    assert "METER STYLE: Pill -> proposer=yolo" in profile
    assert "METER STYLE MISMATCH:" in profile
    session = source("native_orion/src/RemotePlaySession.cpp")
    assert "applyPillYoloRoute(env, config_.meterStyle, config_.pillYoloRoute)" in session
    config = source("native_orion/src/AppConfig.cpp")
    assert 'obj.insert(QStringLiteral("pill_yolo_route"), data.pillYoloRoute);' in config
    assert 'cleanBool(obj, "pill_yolo_route", data_.pillYoloRoute)' in config


def test_no_meter_path_is_shelved_out_of_the_live_side_panel() -> None:
    """[ORION_NO_METER_SHELVED 2026-09-15 owner] "Shelve the no meter path, we'll beef that
    up for a later update."

    SHELVED, NOT DELETED, and the difference is the whole contract:
      * the customer has no way in -- the METER / NO METER switch, the NoMeterCard hold slider
        and its `inputTimed: true` RhythmCard are all unmounted from the Live page, and the
        meter-path cards are unconditional because there is no second path to hide them for;
      * the code is untouched -- NoMeterCard.qml and RhythmCard.qml stay in the tree, the blind
        engine still compiles and still runs (the meter path's own blind backstop uses it), and
        the ONE door left is AppConfig::setInputTimedAllowedForTesting() so the engine tests
        keep driving the real path;
      * the fence is on the INSTALL, not only on the UI -- AppConfig forces input_timed_enabled
        false on load AND save, and the controller's setter refuses to turn the mode on. A mode
        that only the UI hides is exactly the state that ships a blind release to a customer who
        never chose one.

    Re-shipping is a remount plus lifting that fence; this test pins both halves so neither can
    drift back on its own.
    """
    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    # The marker, so the next reader finds the decision rather than guessing at an absence.
    assert "[ORION_NO_METER_SHELVED 2026-09-15]" in live
    # The switch is gone, both buttons with it.
    assert 'objectName: "timingModeCard"' not in live
    assert 'objectName: "meterModeButton"' not in live
    assert 'objectName: "noMeterModeButton"' not in live
    assert 'objectName: "timingModeStatusLine"' not in live
    assert 'text: "NO METER"' not in live
    # Nothing on the page may put the app on the blind path.
    assert "orion.inputTimedEnabled = true" not in live
    assert "orion.inputTimedEnabled = false" not in live
    # The NO METER side of the panel is unmounted, and so is the mode property that drove it.
    assert 'objectName: "noMeterCard"' not in live
    assert 'objectName: "noMeterRhythmCard"' not in live
    assert "NoMeterCard {" not in live
    assert "inputTimed: true" not in live
    assert "noMeterMode" not in live.replace("[ORION_NO_METER_SHELVED 2026-09-15] `noMeterMode`", "")
    # The meter path's own cards are unconditional now: a `visible` binding on a mode that
    # cannot be selected is a switch that can never be thrown.
    for card in ("MeterConfigPanel", "ShotLeadCard"):
        block = live[live.index(card + " {"):]
        assert "visible: !root.noMeterMode" not in block[:900], card

    # NOT DELETED. Both components stay in the tree and stay registered with the QML module,
    # so the mode comes back as a remount.
    assert "NoMeterCard" in source("native_orion/CMakeLists.txt")
    assert 'objectName: "noMeterHoldSlider"' in source(
        "native_orion/qml/components/NoMeterCard.qml")
    # RhythmCard keeps its inputTimed variant -- its meter mount is still live inside
    # MeterConfigPanel, and the blind variant is what the mode remounts.
    assert "property bool inputTimed" in source("native_orion/qml/components/RhythmCard.qml")

    # The install-level fence, in the two places that make it true of a restart rather than
    # only of a rendering.
    config = source("native_orion/src/AppConfig.cpp")
    assert "bool g_inputTimedAllowed = false;" in config
    assert "inputTimedAllowed() && cleanBool(obj, \"input_timed_enabled\", false)" in config
    assert "data.inputTimedEnabled && inputTimedAllowed()" in config
    controller = source("native_orion/src/OrionAppController.cpp")
    assert "if (value && !AppConfig::inputTimedAllowed()) {" in controller
    # ...and the one door the tests keep.
    assert "static void setInputTimedAllowedForTesting(bool allowed) noexcept;" in source(
        "native_orion/src/AppConfig.h")


def test_no_meter_card_is_one_hold_slider_in_milliseconds() -> None:
    """[ORION_NO_METER_V2 2026-09-14 owner] ONE control, plus the fade trim, and nothing else.

    [ORION_NO_METER_SHELVED 2026-09-15 owner] The card is SHELVED, so it is no longer mounted on
    the Live page -- but the file stays in the tree and this contract stays live, because the day
    the mode is "beefed up" it comes back as a remount of exactly this card. A shelved component
    whose contract stopped being checked is a component that quietly rots; the assertions below
    are what make the remount a one-line change instead of an archaeology exercise.

    The console sees exactly press->release, so the hold IS the whole control quantity, and the
    MILLISECONDS stay the control quantity here: the slider is 1..100 over 500 + 3*(v-1) ms, so
    its SHORTEST reachable hold is 500 ms, about 110 ms clear of the owner-bracketed pump-fake
    commit threshold (371..389 ms). That is what makes "tune it late and you get a pump fake"
    impossible from the UI, and it is asserted below even though the big readout now shows the
    1..100 step number ("the lead card is too long 1-100 is fine") with the ms as a caption.
    """
    card = source("native_orion/qml/components/NoMeterCard.qml")
    assert 'objectName: "noMeterHoldSlider"' in card
    assert "orion.noMeterHoldMs" in card
    assert 'text: "Release timing"' in card
    # The ms semantics are unchanged: 500 floor, 3 ms a step, 1..100.
    assert "readonly property real msLo: 500" in card
    assert "readonly property real msStep: 3" in card
    assert "from: 1" in card
    assert "to: 100" in card
    # ONE big readout, showing the 1..100 VALUE, with the milliseconds as a small caption.
    assert 'objectName: "noMeterHoldValue"' in card
    assert "text: root.liveValue" in card
    assert 'objectName: "noMeterHoldMsCaption"' in card
    # [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] The caption leads with CONSOLE FRAMES.
    # The console samples the pad once per frame and judges the release on that frame, so a hold
    # that is not a whole number of frames coin-flips between frame N and N+1: 641 ms is 38.46
    # frames ("inconsistent"), 650 ms is 39.00 ("basically perfect"). "39 fr · 650 ms" says that
    # where "641 ms" hid it. The ms stays in the caption because the slider is still a ms
    # quantity and the engine still reads orion.noMeterHoldMs.
    assert 'text: root.shownFrames + " fr · " + Math.round(root.shownSnappedMs) + " ms"' in card
    assert "orion.noMeterHoldFrames" in card
    assert "orion.noMeterHoldSnappedMs" in card
    # The live copies exist ONLY because settingsChanged fires on commit: without them the
    # caption would freeze at the committed frame count for the whole drag.
    assert "orion.consoleFrameMs" in card
    assert "orion.noMeterFrameQuantize" in card
    assert "readonly property int shownFrames" in card
    assert "Math.max(450, root.liveMs)" in card   # floor first, then snap -- the engine's order
    # Rails are the slider's own ends now, not the milliseconds they map to.
    assert 'text: "1"' in card
    assert 'text: "100"' in card
    assert 'text: "500 ms"' not in card
    assert 'text: "797 ms"' not in card
    # One hint line, shortened.
    assert "Shots EARLY → move right. LATE → move left." in card

    # [2026-09-14 owner] "remove all the unnecessary text": the badge, both InfoTip paragraphs
    # and the shot-type note are gone. The fade behaviour they described has a control instead.
    assert "InfoTip {" not in card   # the comment above may still name what was removed
    assert '"PAUSED"' not in card
    assert '"TIMING"' not in card
    assert "noMeterShotTypeNote" not in card
    assert "fades hold longer automatically" not in card

    # [2026-09-14 owner] "for no meter remove the pause timing". With no control on screen a
    # paused NO METER would be armed, silent and untraceable, so the row is gone and nothing in
    # the app may pause the mode on its own (OrionAppController::inputTimedPaused_ defaults
    # false and setInputTimedEnabled no longer writes it).
    assert 'objectName: "noMeterPauseToggle"' not in card
    assert "orion.inputTimedPaused" not in card

    # [ORION_NO_METER_FADE_TRIM 2026-09-14 owner] "fades need work": ONE compact secondary
    # control, -20..+20 at the same 3 ms step, reading out in ms.
    assert 'objectName: "noMeterFadeTrimSlider"' in card
    assert "orion.noMeterFadeTrimMs" in card
    assert 'text: "Fades"' in card
    assert "readonly property real fadeStep: 3" in card
    assert "from: -20" in card
    assert "to: 20" in card
    assert 'objectName: "noMeterFadeTrimValue"' in card
    # [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] ...reading out in FRAMES while the snap is
    # on, and in ms when it is off. The trim ITSELF stays a millisecond quantity either way: only
    # the TOTAL hold is snapped, so the 3 ms step keeps its resolution and a sub-frame nudge can
    # still tip the total onto the next frame.
    assert "readonly property int fadeLiveFrames" in card
    assert 'root.fadeLiveFrames + " fr"' in card
    assert 'root.fadeLiveMs + " ms"' in card

    # The retired lead knob and every trace of its scale are gone from the card.
    assert "orion.inputTimedLeadMs" not in card
    assert "noMeterLeadSlider" not in card
    assert 'title: "No Meter Lead"' not in card   # the comment may still explain the retirement
    assert "higher releases earlier" not in card
    # The Flick timing dial belongs to the meter path; the blind path's Rhythm offset is a
    # fixed +40 ms, so this card must not grow a second RELEASE-timing control.
    assert "rhythmFlickDelayMs" not in card


def test_meter_blind_advisor_is_generic_and_silent_in_no_meter_mode() -> None:
    """[2026-09-14 owner] The detector-blind advisor fired during NO METER sessions with
    `NO METER: 3 shots with no detection - Detection is set to Purple`.

    Two bugs in one line. (1) NO METER releases on a timer and looks at nothing, so "no meter was
    detected" is the DESIGN there, not a fault. (2) The Red/Purple wording is inherited from
    2K26; 2K27's meter is WHITE and the Style picker (Pill vs Straight) is at least as likely to
    be the mismatch, so the old text sent the owner to change a setting that was already right.
    """
    controller_h = source("native_orion/src/OrionAppController.h")
    controller_cpp = source("native_orion/src/OrionAppController.cpp")
    panel = source("native_orion/qml/components/MeterConfigPanel.qml")

    # Generic wording, naming the two controls that sit directly above it on the card.
    # [CL2-P9-001 2026-09-23] The hint now says what the bot is DOING first (not timing shots),
    # then the one setting to check; it still never names a colour the game no longer has.
    assert "Meter detection unavailable. Venice is not timing your shots" in controller_h
    assert "If your in-game shot meter is" not in controller_h
    assert "Detection is set to" not in controller_h

    # Silent for the whole of NO METER, and the streak is reset so the first press back on the
    # meter path does not inherit one earned while the detector was not being asked a question.
    blind = controller_cpp[controller_cpp.index("void OrionAppController::observeMeterBlindness") :]
    blind = blind[: blind.index("void OrionAppController::observeBotOwnership")]
    assert "if (config_.data().inputTimedEnabled) {" in blind
    # [RT-MED-04 2026-09-23] The streak lives in MeterBlindnessLatch; NO METER resets it whole.
    assert "meterBlindLatch_.reset();" in blind
    # ...and the streak is fed by the engine's unanswered-press terminal, with the raw-detection
    # input keyed on the ACTIVE press epoch (idle sightings no longer reset it).
    assert "&AutomationEngine::meterPressUnanswered" in controller_cpp
    assert "observeMeterBlindness(automation_.activePhysicalPressEpoch(), rawMeterVisible);" in controller_cpp
    assert "observeMeterBlindness(shot_.physicalShotEpoch, rawMeterVisible);" not in controller_cpp

    # The log prefix no longer collides with the NO METER timing mode / the engine's blind-release
    # tag, and it says "no meter detected" rather than "no detection".
    assert "METER BLIND: %1 shots with no meter detected" in controller_cpp
    assert "NO METER: %1 shots with no detection" not in controller_cpp

    # And the badge on the card says what happened, not which setting to change. ("NO METER" is
    # now the name of a timing MODE on the same page, so the old badge read as a mode readout.)
    assert '"NO METER DETECTED"' in panel
    assert 'meterBlindWarning ? "NO METER — CHECK COLOR"' not in panel


def test_live_meter_overlay_and_truth_hud_remain_real_and_visible() -> None:
    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    for contract in (
        'objectName: "meterDebugLayer"',
        'objectName: "meterLockBox"',
        'objectName: "liveMeterMetricsOverlay"',
        "orion.meterConfirmed",
        "orion.meterBoxWidth > 0",
        "orion.liveFrameWidth > 0",
        "orion.shotEtaToTargetMs",
        "orion.shotHoldMs",
        "Theme.meterLock",
    ):
        assert contract in live
    assert 'objectName: "meterInfoHud"' not in live
    assert '"AWAITING SHOT"' not in live
    assert ': "--"' not in live[
        live.index('id: liveMeterMetrics') : live.index("// Skeleton overlay")
    ]
    assert "orion.showLiveMeterMetrics" in live
    assert "&& (etaAvailable || holdAvailable)" in live
    assert "root.streamLive || root.previewActive" in live
    meter_layer = live[live.index('id: meterDebugLayer') : live.index(
        "// CONFIRMED-METER GATE", live.index('id: meterDebugLayer')
    )]
    assert "z: 20" in meter_layer
    assert "above every\n                            // non-modal capture HUD chip" in meter_layer


def test_setup_defaults_to_production_setup_and_removes_performance_surface() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")
    assert "sourceComponent: setupDash" in dashboard
    # [2026-09-14 owner] The "Production Setup" card (Console / Video / Reconnect /
    # Timing status pills) is deleted: every pill restated a control in the Connection
    # card directly below it. Setup now contains only the Connection card.
    assert 'title: "Production Setup"' not in dashboard
    assert 'objectName: "passiveTimingStatus"' not in dashboard
    assert 'label: "Timing"' not in dashboard
    assert 'label: "Reconnect"' not in dashboard
    assert 'title: "Connection"' in dashboard
    assert 'title: "Controller lightbar"' not in dashboard
    assert 'title: "Profiles"' not in dashboard
    assert "orion.switchProfile" not in dashboard
    # [ACCOUNT CARD -> PROFILE PAGE 2026-09-14 -> LICENCE STRIP 2026-09-15 owner]
    # Setup is configuration only. Every licence/build binding left Setup for the
    # Profile page, and now lives in the Sidebar footer strip and its flyout.
    assert 'title: "Account"' not in dashboard
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    for account_binding in (
        "orion.licenseKeyMasked",
        "orion.copyLicenseKey()",
        "orion.displayVersion",
        "orion.updateChannel",
        "orion.timeLeft",
    ):
        assert account_binding not in dashboard
    for account_binding in (
        "orion.licenseState",
        "orion.licenseKeyMasked",
        "orion.copyLicenseKey()",
        "orion.displayVersion",
        "orion.updateChannel",
    ):
        assert account_binding in sidebar
    # Setup still GATES on the licence state (accessReady) — it just no longer
    # reports it. That guard is the only licence reference left on the page.
    assert dashboard.count("orion.licenseState") == 2
    assert "dashTab" not in dashboard
    assert 'label: "Network"' not in dashboard
    assert 'label: "Performance"' not in dashboard
    assert "performanceDash" not in dashboard
    assert "Advanced Detection" not in dashboard


def test_network_telemetry_is_removed_from_customer_setup_surface() -> None:
    dashboard = source("native_orion/qml/pages/DashboardPage.qml")
    for removed in (
        'objectName: "networkWaitingState"',
        'objectName: "courtTelemetry"',
        'objectName: "packetCaptureOptIn"',
        "orion.rttMs",
        "orion.jitterMs",
        "orion.inboundPackets",
        "orion.outboundPackets",
    ):
        assert removed not in dashboard


def test_network_capture_toggle_drives_network_enabled_and_refreshes_live_state() -> None:
    # [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in flag
    # (passiveSniffingEnabled / network_packet_capture_opt_in) is DELETED per owner
    # decision: everything network-side ships on by default. The packetCaptureEnabled
    # property survives as the network feature's own switch, and the deleted flag must
    # stay deleted.
    controller = source("native_orion/src/OrionAppController.h")
    implementation = source("native_orion/src/OrionAppController.cpp")

    assert (
        "Q_PROPERTY(bool packetCaptureEnabled READ packetCaptureEnabled "
        "WRITE setPacketCaptureEnabled NOTIFY settingsChanged)"
    ) in controller
    assert "void OrionAppController::setPacketCaptureEnabled(bool value)" in implementation
    assert "data.networkEnabled = value;" in implementation
    assert "networkBridge_.start();" in implementation
    assert "networkBridge_.stop();" in implementation
    # The deleted flag must not creep back into any of its old homes.
    for deleted in ("passiveSniffingEnabled", "network_packet_capture_opt_in"):
        assert deleted not in implementation
        assert deleted not in source("native_orion/src/AppConfig.cpp")
    assert "passiveSniffingEnabled" not in source("native_orion/src/AppConfig.h")
    connection_handler = implementation[
        implementation.index("&NetworkBridge::connectionChanged"):
        implementation.index("networkBridge_.setConsoleIp", implementation.index("&NetworkBridge::connectionChanged"))
    ]
    assert "emit telemetryChanged();" in connection_handler


def test_activity_log_is_copy_pasteable_for_support() -> None:
    # "Logs are not copy-pasteable": users were screenshotting the Activity
    # view to get its text into an AI chat for debugging. Contract: one click
    # copies the BOUNDED TAIL of the on-disk engineer log (the 160-line UI
    # ring covers only seconds of an active session) straight from C++ to the
    # clipboard (the copyLicenseKey idiom), after a sharing redaction pass —
    # the paste target is explicitly outside the app. When the disk log is
    # unreadable the copy falls back to the in-memory ring, so the button
    # never silently no-ops.
    controller = source("native_orion/src/OrionAppController.h")
    implementation = source("native_orion/src/OrionAppController.cpp")
    clipboard_copy = source("native_orion/src/ActivityLogClipboard.h")
    policy = source("native_orion/src/UiNotificationPolicy.h")
    assert "Q_INVOKABLE void copyActivityLog();" in controller
    assert "void OrionAppController::copyActivityLog()" in implementation
    assert "copyActivityLogToClipboard(" in implementation
    assert '/logs/orion_native.log"' in implementation
    assert "readLogTailForSharing(" in clipboard_copy
    assert "activityLogCopyForSharing(" in clipboard_copy  # partial/ring fallback
    assert "clipboard->setText(" in clipboard_copy
    assert "serializeActivityLogForSharing(ring)" in policy
    assert "Disk log incomplete; recent session events follow." in policy
    assert "redactActivityLineForSharing" in policy
    # The tail bounds are named constants, not magic numbers.
    assert "kActivityShareTailMaxBytes = 256 * 1024" in policy
    assert "kActivityShareTailMaxLines = 1500" in policy

    # Both Activity surfaces expose the button: the Live page's collapsed
    # ACTIVITY card and the Debug page's LogViewer. Each also carries the
    # escape hatch for history older than the tail bound: open the logs
    # folder (pre-existing invokable, newly wired).
    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    assert "orion.copyActivityLog()" in live
    assert "orion.openLogsFolder()" in live
    viewer = source("native_orion/qml/components/LogViewer.qml")
    assert "orion.copyActivityLog()" in viewer
    assert "orion.openLogsFolder()" in viewer
    # In-place selection stays per-line on the colour-coded ListView (a flat
    # TextArea would lose severity colouring); Copy all is the primary path.
    assert "selectByMouse: true" in viewer
    assert "readOnly: true" in viewer


def test_meter_delay_controls_are_wired_end_to_end() -> None:
    # SHIP-BLOCKER regression guard: the Meter Delay card binds three names on
    # `orion`. QML resolves a missing property to `undefined` at RUNTIME (no build
    # failure), so when OrionAppController shipped without these Q_PROPERTYs the
    # toggle rendered unchecked and every assignment died with "Cannot assign to
    # non-existent property" — the whole subsystem was untestable from the UI.
    # Native meta-object coverage: native_orion/tests/MeterDelaySettingsPropertyTests.cpp.
    meter = source("native_orion/qml/components/MeterConfigPanel.qml")
    controller = source("native_orion/src/OrionAppController.h")
    implementation = source("native_orion/src/OrionAppController.cpp")

    # [METER DELAY SHELVED 2026-09-12 owner: "port and shelve out meter delay"] The card is gone
    # from the customer panel: no QML binds the three names any more. The BACKEND contract below
    # is unchanged on purpose (a persisted meter_delay_enabled still applies, the D-pad bypass and
    # VeniceNetSvc stay), so the Q_PROPERTY assertions remain the regression guard.
    assert "checked: orion.meterDelayEnabled" not in meter
    assert "orion.meterDelayEnabled = checked" not in meter
    assert "value: orion.meterDelayMs" not in meter
    assert "orion.meterDelayMs = value" not in meter
    assert "checked: orion.meterDelayBypassOnDefense" not in meter
    assert "METER DELAY CARD SHELVED" in meter

    # The leading "\n    " pins the declaration at the start of its line: a plain
    # substring check also matches a commented-out declaration ("// Q_PROPERTY(...)"),
    # i.e. it would pass with the bug present. Verified by falsification 2026-08-08.
    assert (
        "\n    Q_PROPERTY(bool meterDelayEnabled READ meterDelayEnabled "
        "WRITE setMeterDelayEnabled NOTIFY settingsChanged)"
    ) in controller
    assert (
        "\n    Q_PROPERTY(int meterDelayMs READ meterDelayMs "
        "WRITE setMeterDelayMs NOTIFY settingsChanged)"
    ) in controller
    assert (
        "\n    Q_PROPERTY(bool meterDelayBypassOnDefense READ meterDelayBypassOnDefense "
        "WRITE setMeterDelayBypassOnDefense NOTIFY settingsChanged)"
    ) in controller

    # Setters persist through AppConfig (saveConfigSilently emits settingsChanged)
    # AND re-prime the live actuator so a change takes effect without a restart.
    assert "void OrionAppController::setMeterDelayEnabled(bool value)" in implementation
    assert "data.meterDelayEnabled = value;" in implementation
    assert "void OrionAppController::setMeterDelayMs(int value)" in implementation
    assert "data.meterDelayMs = clamped;" in implementation
    assert "void OrionAppController::setMeterDelayBypassOnDefense(bool value)" in implementation
    assert "data.meterDelayBypassOnDefense = value;" in implementation
    # One shared config->runtime mapping: the constructor priming plus all three
    # setters route through applyMeterDelayRuntimeConfig().
    assert implementation.count("applyMeterDelayRuntimeConfig();") >= 4
    # [2026-09-23 owner] Meter Delay is SHELVED: the controller never arms it, whatever the
    # persisted setting says (the installed build engaged 250 ms -> early shots and late-seen
    # meters). Every arm/link/status site goes through meterDelayActive().
    assert "meterDelay_.setEnabled(meterDelayActive(cfg));" in implementation
    assert "constexpr bool kMeterDelayShelved = true;" in implementation
    assert "return !kMeterDelayShelved && d.meterDelayEnabled;" in implementation
    assert "meterDelay_.setEnabled(cfg.meterDelayEnabled);" not in implementation
    assert "meterDelay_.setManualDelayMs(static_cast<double>(cfg.meterDelayMs));" in implementation


def test_internal_orion_runtime_contracts_are_not_rebranded() -> None:
    cmake = source("native_orion/CMakeLists.txt")
    main_cpp = source("native_orion/src/main.cpp")
    remote = source("native_orion/src/RemotePlaySession.cpp")
    updater = source("native_orion/src/updater_main.cpp")

    assert "qt_add_qml_module(OrionNative" in cmake
    assert 'QStringLiteral("orion://")' in main_cpp
    assert 'L"Local\\\\OrionNativeLauncher"' in main_cpp
    # The deep-link lookup title tracks the WINDOW title (customer-facing, now
    # "Venice") rather than the internal slugs around it. These two literals are a
    # matched pair across a C++/QML boundary that no compiler checks, and a mismatch
    # fails silently — forwarding finds nothing and a duplicate instance launches —
    # so both halves are pinned here together.
    assert 'FindWindowW(nullptr, L"Venice")' in main_cpp
    assert 'title: "Venice"' in source("native_orion/qml/Main.qml")
    assert 'QStringLiteral("OrionPreviewFrame_%1")' in remote
    assert "OrionUpdater.exe" in updater
    assert 'QStringLiteral("OrionNative.exe")' in updater


def test_capture_refresh_rate_is_a_customer_control_on_the_capture_card_path() -> None:
    """[2026-09-14 owner] The rate is graded against, so it is a real setting, not a hidden env
    var. It lives inside the capture-card-only block of the stream form, directly under the
    device picker.

    120 Hz is NOT offered. Confirmed live 2026-09-14: the Elgato HD60 X accepts a 1080p120
    request and delivers 60, and the sidecar judged every shot's cadence against 120 for the
    whole session — the owner's random earlies and lates ("i turned it off and i went perfect
    from the field"). A persisted 120 still snaps through captureCardFps for a future card that
    really does deliver it; it simply cannot be picked here, and it READS as 60.
    """
    form = source("native_orion/qml/components/StreamSetupForm.qml")
    assert 'objectName: "captureFpsCombo"' in form
    assert "orion.captureCardFps" in form
    # [RT-MED-05 / P-E 2026-09-23] 30 Hz is preview only (no timing authority below 60 fps).
    assert 'model: ["30 Hz (preview only)", "60 Hz (recommended)"]' in form
    for label in ('"30 Hz (preview only)"', '"60 Hz (recommended)"'):
        assert label in form
    assert '"120 Hz"' not in form
    # Any non-30 value, persisted or env-set, reads as the recommended 60.
    assert 'return fps === 30 ? "30 Hz (preview only)" : "60 Hz (recommended)"' in form
    assert (
        "Shot timing needs 60 Hz. "
        "At 30 Hz Venice shows the picture but does not time shots." in form
    )
    # It must sit inside the capture-card branch, after the device row.
    assert form.index('visible: !form.xboxMode && orion.videoSource === "capture_card"') \
        < form.index("id: ccPicker") < form.index('objectName: "captureFpsCombo"')
    # Sections read Console -> Video source -> Stream quality.
    assert form.index('text: "Console"') \
        < form.index('text: "Video source"') < form.index('text: "Stream quality"')
    # [2026-09-14 owner] the connect CTA wording is "Connect" everywhere.
    assert "Enable Bot" not in form
    assert "Bot + Controller" not in form
    assert "press Connect" in form


def test_tempo_toggle_reveals_only_button_and_stick_selector() -> None:
    card = source("native_orion/qml/components/RhythmCard.qml")
    assert 'objectName: "tempoToggle"' in card
    assert 'checked: root.tempoOn' in card
    assert 'orion.tempoEnabled = value' in card
    assert 'objectName: "releasePathSelector"' in card
    assert 'model: ["Button", "Stick"]' in card
    assert 'visible: root.tempoOn' in card
    assert "onActivated:" in card
    assert "onValueChanged:" not in card
    assert "Qt.binding" in card
    assert "rhythmFlickDelayMs" not in card
    assert "ThemedSlider" not in card
    assert "tempoReleaseStyle" not in card
    assert "orion.inputTimedRhythmEnabled" in card
    assert "orion.tempoInputPath" in card
    assert '"Stick", "Tempo"' not in card


def test_tempo_remap_is_retired_from_the_meter_panel():
    """Owner retired Tempo Remap on 2026-08-26.

    SURFACE removal only: the card and its Square/Stick picker leave the UI, but
    `orion.tempoInputSource` and the engine's tempo handling stay, so a persisted
    setting cannot change behaviour just because a control disappeared.
    """
    meter = source("native_orion/qml/components/MeterConfigPanel.qml")
    assert "tempoCard" not in meter
    assert "tempoInputSourceCombo" not in meter
    assert 'text: "Tempo Remap"' not in meter


def test_licence_strip_replaces_the_profile_tab_in_the_sidebar_footer() -> None:
    """[PROFILE TAB -> LICENCE STRIP 2026-09-15 owner]

    "remove the profile tab, license info and days left should be displayed on the
    bottom left corner or at the top of the page somewhere". The strip sits in the
    Sidebar footer, directly above the session pill, and renders the backend's
    `profile` block, which the launcher parses tolerantly: the CURRENT live Lambda
    sends none, so every number must degrade to an honest placeholder rather than to
    a fabricated zero.
    """
    sidebar = source("native_orion/qml/components/Sidebar.qml")

    # The page, its route and its nav entry are gone.
    assert not (QML / "pages" / "ProfilePage.qml").exists()
    assert "ProfilePage" not in source("native_orion/CMakeLists.txt")
    assert 'key: "profile"' not in sidebar
    shell = source("native_orion/qml/components/AppShell.qml")
    assert "ProfilePage" not in shell
    assert 'orion.currentPage === "profile"' not in shell
    # A persisted current_page="profile" must fall through to Live the way "general"
    # does, so it may not be excluded from the Remote Play fallthrough either.
    assert 'orion.currentPage !== "profile"' not in shell
    # The "user" glyph existed only for that nav entry.
    assert 'icon === "user"' not in source("native_orion/qml/components/NavIcon.qml")
    assert 'icon: "user"' not in sidebar

    # Every Q_PROPERTY the controller exposes for the account surface, by exact name.
    for prop in (
        "orion.profileKnown",
        "orion.profileDiscordId",
        "orion.profileDiscordName",
        "orion.profilePlan",
        "orion.profileDaysLeft",
        "orion.profileLifetime",
        "orion.profileActivatedEpochS",
        "orion.profileHwidResetsFreeTotal",
        "orion.profileHwidResetsFreeRemaining",
        "orion.profileHwidPaidCredits",
    ):
        assert prop in sidebar

    # The strip: two lines, addressable. [ORION_UI_BUBBLES 2026-09-15 owner] It used
    # to sit above a "Session" status pill; that pill is gone and the strip is now
    # the whole footer.
    assert 'objectName: "licenseStrip"' in sidebar
    assert 'objectName: "licenseStripState"' in sidebar
    assert 'objectName: "licenseStripDays"' in sidebar
    assert 'label: "Session"' not in sidebar

    # Line 1 = state + plan. Line 2 = days left, with the honest fallbacks.
    assert '"Active \u00b7 " + plan' in sidebar
    assert 'return "Not activated"' in sidebar
    assert 'return "Trial"' in sidebar
    assert 'days + " days left"' in sidebar
    assert 'return "Expires today"' in sidebar
    assert 'return "Expired"' in sidebar
    assert 'return "Not reported"' in sidebar
    # A lifetime key reads as a word, never as a day count.
    assert 'return "Lifetime"' in sidebar
    # The strip falls back to the controller's own phrasing before it gives up.
    assert "orion.timeLeft" in sidebar
    # Subtle warning tone in the last three days only.
    assert "licenseExpiringSoon" in sidebar
    assert "root.licenseDaysLeft >= 0 && root.licenseDaysLeft <= 3" in sidebar
    assert "Theme.warning" in sidebar

    # The flyout carries what the Profile page used to: key + Copy, Discord ID +
    # Copy, the reset allowance, the activated date, the machine and the build.
    assert 'objectName: "licenseFlyout"' in sidebar
    assert "Popup {" in sidebar
    assert "orion.licenseKeyMasked" in sidebar
    assert "orion.copyLicenseKey()" in sidebar
    assert "orion.copyProfileDiscordId()" in sidebar
    assert "orion.machineIdMasked" in sidebar
    assert "orion.displayVersion" in sidebar
    # The owner's HWID rule, stated once, in plain language.
    assert '" of " + root.licenseFreeTotal + " free remaining"' in sidebar
    assert "free PC resets" in sidebar
    assert "1 credit" in sidebar


def test_footer_bubbles_are_gone_and_the_tour_is_first_launch_only() -> None:
    """[ORION_UI_BUBBLES 2026-09-15 owner]

    "remove the session start bubble and the quick start bubble, quick start should
    just be shown when the customer first launches the UI and rework the profile
    bubble to make it look neater and better".

    The three bubbles were the three items in the Sidebar footer: the "Quick Start"
    button that re-opened the guided tour, the "Session: Offline/Live" StatusPill
    under it, and the licence strip + flyout. The first two are gone; the third was
    redesigned in place (same objectNames, which the C++ smoke tests bind to).
    """
    sidebar = source("native_orion/qml/components/Sidebar.qml")
    shell = source("native_orion/qml/components/AppShell.qml")
    tour = source("native_orion/qml/components/FirstRunTour.qml")

    # ---- bubble 1: the Quick Start footer button and every way to raise it ----
    # (The removal is narrated in a comment in the rail, so match QML, not prose.)
    assert 'text: "Quick Start"' not in sidebar
    assert "quickStart" not in sidebar
    assert "tourRequested" not in sidebar
    assert "onTourRequested" not in shell
    # The nav model may not carry an action entry any more: the delegate is a pure
    # page router, so a stray action row would silently route to a missing page.
    assert 'action: "tour"' not in sidebar
    assert "isAction" not in sidebar
    assert 'icon: "guide"' not in sidebar
    # The tour still spotlights the nav rows, so the anchors stay registered.
    assert 'TourRegistry.register("nav:" + modelData.key' in sidebar
    assert 'TourRegistry.unregister("nav:" + modelData.key' in sidebar

    # ---- the tour is first-launch-only, and says so ----
    # It auto-opens exactly once, gated on the persisted flag, and the flag is
    # written the moment it OPENS. Writing it only on Finish/Skip (the old
    # behaviour) left it false whenever the tour was abandoned, so it re-showed on
    # every later launch -- which is what the owner was seeing.
    assert "if (!orion.preflightComplete) {" in shell
    assert "orion.markPreflightComplete()" in shell
    assert "firstRunTour.start()" in shell
    assert shell.index("orion.markPreflightComplete()") < shell.index("firstRunTour.start()")
    # Nothing else in the shell may raise it -- the rail's signal is gone.
    assert "tourRequested" not in shell
    # finish() still marks it (idempotent), so the flag has two writers, not none.
    assert "orion.markPreflightComplete()" in tour
    assert "reopen this from Quick Start" not in tour

    # ---- bubble 2: the session status pill ----
    assert "StatusPill {" not in sidebar
    assert 'label: "Session"' not in sidebar
    assert "sessionLabel" not in sidebar
    assert "sessionTone" not in sidebar
    assert "orion.remoteState" not in sidebar

    # ---- bubble 3: the licence flyout, rebuilt in the house card style ----
    # Header: a state chip plus the days left as the headline (the owner's
    # "Active - 23 days left", split so neither half elides the other).
    assert "licenseChipText" in sidebar
    assert "licenseToneColor" in sidebar
    assert "licenseToneFill" in sidebar
    assert "root.licenseChipText.toUpperCase()" in sidebar
    # A tidy two-column key/value grid instead of seven bespoke rows.
    assert "component DetailRow: RowLayout {" in sidebar
    for label in ('label: "Key"', 'label: "Discord"',
                  'label: "PC resets"', 'label: "Activated"', 'label: "Machine"',
                  'label: "Build"'):
        assert label in sidebar
    # [2026-09-21 owner] "profile card displaying the user's discord name and days left":
    # the Discord NAME is the card's headline (identity block with an initial tile), no
    # longer a 'label: "Name"' row. The ID stays a copyable row.
    assert 'objectName: "licenseFlyoutDiscordName"' in sidebar
    assert 'label: "Name"' not in sidebar
    assert '"Discord not linked"' in sidebar
    # Inline components may only reference ones declared above them.
    assert sidebar.index("component CopyChip:") < sidebar.index("component DetailRow:")
    # Card.qml's elevation idiom: drop halo + 1px top light catch under the surface.
    assert "Theme.shadowHalo" in sidebar
    assert "Theme.hairlineLight" in sidebar
    # Esc closes it -- CloseOnEscape never fires without active focus -- and so does
    # a press outside the strip.
    assert "focus: true" in sidebar
    assert "Popup.CloseOnEscape | Popup.CloseOnPressOutsideParent" in sidebar
    # Every value elides rather than widening the card past the rail.
    assert "elide: detailRow.valueElide" in sidebar
    # Theme tokens only: not one hard-coded colour anywhere in the rail.
    assert '"#' not in sidebar


def test_activity_feed_is_the_customer_ring_and_raw_log_stays_reachable() -> None:
    """[ACTIVITY FEED 2026-09-14 owner "overall polish"]

    One hour of logs/orion_native.log grew 12,000 lines and the customer Activity
    panel showed all of it. The split lives in ONE place natively
    (ui_notifications::shouldEnterActivityRing, called from appendLog); QML simply
    consumes the two rings.
    """
    header = source("native_orion/src/UiNotificationPolicy.h")
    assert "bool shouldEnterActivityRing(" in header
    assert "bool isCustomerActivityTemplate(" in header
    assert "bool isEngineeringTelemetryLine(" in header
    assert "bool looksLikeCounterLine(" in header

    controller = source("native_orion/src/OrionAppController.cpp")
    # Exactly one production call site: the appendLog chokepoint.
    assert "if (ui_notifications::shouldEnterActivityRing(simplified))" in controller
    assert controller.count("shouldEnterActivityRing(simplified)") == 1
    assert "customerLogs_" in controller
    # The network-bridge repeat is logged on state CHANGES only.
    assert "lastNetworkBridgeStateLine_" in controller

    viewer = source("native_orion/qml/components/LogViewer.qml")
    assert 'property string filterMode: "activity"' in viewer
    assert "property string activityText" in viewer
    # The raw engineering ring is still one tab away, and the on-disk log is still
    # behind "Open folder"; "Copy all" still copies the FULL log for support.
    assert '{ k: "all", t: "All" }' in viewer
    assert "orion.copyActivityLog()" in viewer
    assert "orion.openLogsFolder()" in viewer

    debug_page = source("native_orion/qml/pages/DebugPage.qml")
    assert "text: orion.logText" in debug_page
    assert "activityText: orion.activityText" in debug_page

    live = source("native_orion/qml/pages/RemotePlayPage.qml")
    assert "root.filterLogLines(orion.activityText)" in live


def test_shot_verdict_tally_is_one_component_under_both_tuning_sliders() -> None:
    """[ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] The live shot tally.

    The owner tunes ONE slider against the game's own shot-feedback banner and could not
    keep the score: "can't tell if I found my value or not because sometimes it's green".
    The sidecar now grades that banner with the validated reader and the controller rolls
    the last ten verdicts; this pins the surface that shows them.

    THE contract, in three parts:
      1. ONE component. NoMeterCard and ShotLeadCard mount the same file with the same
         bindings -- whichever timing path is on screen, the owner reads the identical
         tally under the slider he is holding.
      2. The tally is DATA and the suggestion is the ONLY sentence (owner: minimal text).
      3. The counts are READ-ONLY bindings on `orion`; the card's only write is the Reset
         invokable.
    """
    card = source("native_orion/qml/components/ShotVerdictTally.qml")
    for name in ("shotVerdictTally", "shotVerdictHeadline", "shotVerdictSuggestion",
                 "shotVerdictDots", "shotVerdictReset"):
        assert f'objectName: "{name}"' in card, name
    # Every count it renders, on the controller's own low-fanout notifier.
    for binding in ("orion.bannerCount10", "orion.bannerGreen10", "orion.bannerEarly10",
                    "orion.bannerLate10", "orion.bannerOther10", "orion.bannerContested10",
                    "orion.bannerPattern10", "orion.bannerSuggestion",
                    "orion.bannerLastTiming", "orion.bannerLastCoverage"):
        assert binding in card, binding
    # The one write, and the ten-slot strip.
    assert "orion.resetBannerTally()" in card
    assert "model: 10" in card
    # Buckets are coloured by the shared palette, never by a local hex.
    for token in ("Theme.success", "Theme.warning", "Theme.danger"):
        assert token in card, token
    assert "#" not in card.split("ColumnLayout {", 1)[1], "no literal colours in the tally"
    # Headline shape: "last 10: ..." once full, the REAL count before that.
    assert '"last 10: "' in card
    assert '" shots: "' in card
    # It is card-less: it drops into a Card's own column, so it must add no Card of its own.
    assert "Card {" not in card

    # Retired from BOTH tuning cards; backend verdict evidence remains available.
    for mount in ("native_orion/qml/components/NoMeterCard.qml",
                  "native_orion/qml/components/ShotLeadCard.qml"):
        content = source(mount)
        assert "ShotVerdictTally {" not in content, mount

    # Registered with the QML module, or the launcher cannot resolve the type at runtime.
    assert "qml/components/ShotVerdictTally.qml" in source("native_orion/CMakeLists.txt")

    # The native side: the properties QML binds, the invokable it calls, and the automatic
    # clear on every committed change to the value being tuned (otherwise the window
    # straddles a slider move and describes two different settings at once).
    header = source("native_orion/src/OrionAppController.h")
    for prop in ("bannerGreen10", "bannerEarly10", "bannerLate10", "bannerOther10",
                 "bannerCount10", "bannerContested10", "bannerLastTiming",
                 "bannerLastCoverage", "bannerPattern10", "bannerSuggestion"):
        assert f"Q_PROPERTY(int {prop} " in header or f"Q_PROPERTY(QString {prop} " in header, prop
        assert "NOTIFY bannerTallyChanged" in header
    assert "Q_INVOKABLE void resetBannerTally();" in header

    controller = source("native_orion/src/OrionAppController.cpp")
    assert "&RemotePlaySession::bannerVerdict" in controller
    assert "&OrionAppController::observeBannerVerdict" in controller
    # One Activity line per graded shot, in the game's own words.
    assert 'QStringLiteral("Shot: %1")' in controller
    # Auto-reset: one call in each setter for a value the tally describes -- Shot Lead, NO
    # METER Release timing, the fade trim, and the vision-assist switch (which changes WHO times
    # the release, so the shots either side of it are not the same experiment) -- so the count
    # always restarts with the value. Session start/end clears the de-dupe watermark too.
    for setter in ("setActuationLeadMs", "setNoMeterHoldMs", "setNoMeterFadeTrimMs",
                   "setNoMeterVisionAssist"):
        body = controller[controller.index("OrionAppController::" + setter + "("):][:2600]
        assert "resetBannerTally();" in body, setter
    # [RT-MED-01 2026-09-23] +1: calibration Cancel restores the pre-calibration lead WITHOUT
    # setActuationLeadMs (whose clamp/user-set latch was the bug) and so resets the tally itself.
    assert controller.count("    resetBannerTally();") == 5
    assert controller.count("resetBannerTallyForSession();") >= 2

    # And the Activity ring allow-lists it, so no future deny-list entry can swallow the
    # most customer-meaningful line the app writes.
    assert 'QStringLiteral("shot: ")' in source("native_orion/src/UiNotificationPolicy.h")


def test_lead_calibration_cancel_restores_exact_provenance() -> None:
    """[RT-MED-01 / CL3-F4-009 2026-09-23] Cancel restores the exact pre-calibration tuple.

    The old Cancel went through setActuationLeadMs(), which clamps to [150, 800] and latches
    user_set=true, so an Auto install came back as a pinned 150 ms user lead. The native half is
    RemotePlayExecutablePolicyTests::leadCalibrationCancelRestoresExactProvenance; this pins the
    controller wiring (the controller has no native test harness).
    """
    controller = source("native_orion/src/OrionAppController.cpp")
    begin = controller[controller.index("void OrionAppController::beginLeadCalibration()") :]
    begin = begin[: begin.index("void OrionAppController::cancelLeadCalibration()")]
    assert "leadCalStartLead_ = captureActuationLeadProvenance(config_.data());" in begin
    # [2026-09-23 owner] On Auto, the start value is APPLIED (so every verdict grades the lead under
    # test and a lock at the start value persists) - strictly AFTER the exact snapshot, so Cancel
    # still restores Auto verbatim.
    snap = begin.index("leadCalStartLead_ = captureActuationLeadProvenance(config_.data());")
    apply = begin.index("setActuationLeadMs(leadCal_.leadMs);")
    assert snap < apply
    assert "leadAutoSeedMs_" in begin
    cancel = controller[controller.index("void OrionAppController::cancelLeadCalibration()") :]
    cancel = cancel[: cancel.index("void OrionAppController::reportLeadCalibrationVerdict")]
    assert "restoreActuationLeadProvenance(data, leadCalStartLead_)" in cancel
    assert "setActuationLeadMs(restoreMs)" not in cancel
    assert " setActuationLeadMs(" not in cancel.replace("Not setActuationLeadMs()", "")
    assert "restored the lead to %2." in cancel
    assert "leadCalStartLeadMs_" not in controller
