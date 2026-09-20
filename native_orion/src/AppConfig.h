#pragma once

#include "OrionExports.h"

#include <QtCore/QJsonArray>
#include <QtCore/QJsonObject>
#include <QtCore/QJsonValue>
#include <QtCore/QList>
#include <QtCore/QMap>
#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtCore/QStringList>

#include <algorithm>
#include <cmath>

namespace orion {

// Canonical input-ownership selector shared by persisted settings and the QML
// controller API. Only these three modes are meaningful to AutomationEngine:
// Square/Button, raw right-stick, or both. Unknown/empty values fail safely to
// Square unless the caller supplies another valid canonical fallback.
inline QString normalizedRemotePlayInputSource(
    QString value, QString fallback = QStringLiteral("square"))
{
    const auto canonical = [](QString candidate) -> QString {
        const QString input = candidate.trimmed().toLower();
        if (input == QLatin1String("square") || input == QLatin1String("square_only")
            || input == QLatin1String("button")) {
            return QStringLiteral("square");
        }
        if (input == QLatin1String("stick") || input == QLatin1String("stick_only")
            || input == QLatin1String("right stick")
            || input == QLatin1String("right_stick")) {
            return QStringLiteral("stick");
        }
        if (input == QLatin1String("both") || input == QLatin1String("auto")) {
            return QStringLiteral("both");
        }
        return {};
    };

    const QString normalized = canonical(value);
    if (!normalized.isEmpty()) {
        return normalized;
    }
    const QString normalizedFallback = canonical(fallback);
    return normalizedFallback.isEmpty() ? QStringLiteral("square") : normalizedFallback;
}

// Canonical meter box-proposer selector shared by persisted settings, the QML
// controller API and the sidecar environment (ORION_METER_PROPOSER, read once
// in meter_detector_yolo.get_locator()). "cv" = the pure-CV landmark locator
// (meter_locator_cv.py, Arrow2 geometry only); "yolo" = the shipped ONNX
// detector (every style in the Style combo). Anything else fails safely to
// the certified default, "cv".
inline QString normalizedMeterProposer(const QString& value)
{
    const QString input = value.trimmed().toLower();
    if (input == QLatin1String("yolo") || input == QLatin1String("onnx")) {
        return QStringLiteral("yolo");
    }
    return QStringLiteral("cv");
}

// [ORION_CAPTURE_FPS 2026-09-14] Capture-card refresh rate, snapped to the only three rates an
// Elgato-class HDMI card negotiates at 1080p. Shared by the settings loader, the QML controller
// setter, and the sidecar environment (ORION_CAPTURE_FPS -> CaptureCardBackend(fps=...), which
// derives CadenceLock, the health floor and the nominal frame period from it).
//
// SNAP, never clamp: a stored/typed 45 is not "between" two supported modes, it is a mode the
// driver does not have, and CAP_PROP_FPS set() is advisory — asking for 45 silently yields
// whatever the card feels like and the cadence model would then be modelling a grid that does not
// exist. Nearest-allowed keeps stored == requested == what the card was actually asked for.
// Ties (e.g. 45, exactly between 30 and 60) resolve to the LOWER rate, which is the safer request
// on a bandwidth-limited USB card.
inline int snappedCaptureCardFps(int value) noexcept
{
    constexpr int kAllowed[] = {30, 60, 120};
    int best = 60;
    int bestDistance = -1;
    for (const int allowed : kAllowed) {
        const int distance = value > allowed ? value - allowed : allowed - value;
        if (bestDistance < 0 || distance < bestDistance) {
            bestDistance = distance;
            best = allowed;
        }
    }
    return best;
}

// === [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] THE CONSOLE'S INPUT GRID ===============
//
// The console samples the controller ONCE PER FRAME and judges the release on that frame. At
// 60 Hz that grid is 1000/60 ms wide and the green window is worth ~16 ms of hold — i.e. ONE
// frame. A blind hold that is not a whole number of frames therefore lands the release ON a
// frame boundary and coin-flips between frame N and N+1: the owner's 641 ms (38.46 frames) read
// as "inconsistent", 650 ms (39.00 frames) as "basically perfect". Quantizing every blind hold
// to the grid removes that coin flip by construction.
//
// THE DEFAULT IS 1000/60, NOT THE TYPED 16.6667. They differ by 33 ns a frame, which no timing
// path could ever see — but the EXACTNESS is load-bearing for the arithmetic the owner reads:
// 39 frames is 650.0 ms and the 450 ms pump-fake floor is 27 frames exactly only when the period
// is the real 1000/60. A stored 16.6667 is honoured verbatim (it is within a rounding of the
// same grid); the compiled default is the exact period so the shipped numbers are the clean ones.
inline constexpr double kConsoleFrameMsDefault = 1000.0 / 60.0;   // 16.6667 ms, written exactly
// The blind-release floor, shared. AutomationEngine::kBlindReleaseMinHoldMs is the engine-side
// name for this same number and the argument for it lives there (a Square release before the
// shot commits is a PUMP FAKE; the owner bracketed the commit threshold at 371..389 ms on
// 2026-09-13, so 450 carries ~60 ms of margin). It sits here because the card's frame readout
// has to floor exactly as the law does, and a second literal 450 in the controller would be a
// copy that could drift away from the one that matters. 450 is 27 frames at 1000/60 exactly.
inline constexpr double kBlindReleaseFloorMs = 450.0;
inline constexpr double kConsoleFrameMsMin = 8.0;                 // 125 Hz — absurdly fast, but
inline constexpr double kConsoleFrameMsMax = 40.0;                // 25 Hz — ...both are guards,
                                                                  // not modes: 30/60/120 Hz all
                                                                  // sit inside this band.

// The frame period, defended against every way a double can arrive broken. Shared by the
// settings loader/saver, the engine's applyConfig (plus its ORION_CONSOLE_FRAME_MS override) and
// the QML controller, so file, environment and UI cannot disagree about the grid.
inline double clampedConsoleFrameMs(double value) noexcept
{
    if (!std::isfinite(value) || value <= 0.0) {
        return kConsoleFrameMsDefault;
    }
    return std::clamp(value, kConsoleFrameMsMin, kConsoleFrameMsMax);
}

// [ORION_LEAD_OFFSET_BY_TYPE 2026-09-16 owner] The per-shot-type Shot Lead offset's band. See
// AppConfigData::leadOffsetLeftFadeMs for the measurement. +-40 ms is the whole correction a
// per-type term may ever be worth: the measured fade lateness is 7-10 ms, and a term that could
// travel further than the Shot Lead's own tuning range would be a second lead control.
inline constexpr double kLeadOffsetMinMs = -40.0;
inline constexpr double kLeadOffsetMaxMs = 40.0;

// The offset, defended against every way a double can arrive broken. Shared by the settings
// saver/loader and the engine's applyConfig (plus its two env overrides), so file, environment
// and UI cannot disagree about what a per-type offset of "500" means.
inline double clampedLeadOffsetMs(double value, double fallback) noexcept
{
    const double raw = std::isfinite(value) ? value : fallback;
    if (!std::isfinite(raw)) {
        return 0.0;
    }
    return std::clamp(raw, kLeadOffsetMinMs, kLeadOffsetMaxMs);
}

// How many whole console frames a millisecond hold is, ROUND HALF UP. Half-up rather than
// banker's rounding because the tie is a real case (a hold sitting exactly on a boundary is
// precisely the coin flip being removed) and "the later frame" is the safe side: 2K27's green
// window has an INVARIANT top and a moving bottom, so rounding up lands inside the window and
// rounding down can fall out of the bottom of it.
inline int consoleFrameCount(double ms, double frameMs) noexcept
{
    const double period = clampedConsoleFrameMs(frameMs);
    if (!std::isfinite(ms)) {
        return 0;
    }
    return static_cast<int>(std::floor(ms / period + 0.5));
}

// The hold snapped onto the grid: consoleFrameCount() frames, expressed in ms.
//
// The result is rounded to the NEAREST MICROSECOND. That is not cosmetic. frames * period is a
// floating-point product whose last bit lands either side of the exact answer, and this number
// becomes a DEADLINE compared against a clock with `now < deadline`: one ulp above 650.0 means a
// release scheduled for exactly 650.0 ms does not fire on that tick. A microsecond is four
// orders of magnitude finer than the 4 ms tick and than any quantity this engine measures, so
// the rounding is free — and it makes 39 frames exactly 650.0 ms and the floor exactly 450.0.
inline double snappedConsoleFrameMs(double ms, double frameMs) noexcept
{
    const double period = clampedConsoleFrameMs(frameMs);
    if (!std::isfinite(ms)) {
        return ms;
    }
    const double snapped = static_cast<double>(consoleFrameCount(ms, period)) * period;
    return std::round(snapped * 1000.0) / 1000.0;
}

struct AppConfigData {
    QString launcherTheme = QStringLiteral("obsidian");
    QString customAccent = QStringLiteral("#2563EB");
    // Release ring the client checks /api/update against: dev | internal | beta | stable.
    // 2026-07-25 SHIPPING RECONCILIATION: was "beta". A paid build ships on the GA ring —
    // stable is the terminal ring of the documented promotion flow dev->internal->beta->stable
    // (docs/SERVER_HANDOFF.md:289) and the only ring that "receives builds that passed the live +
    // replay gates". All four values are accepted client-side (loadSettingsObject + setUpdateChannel)
    // and AdminToolController already checks "stable". NOTE for ops: the live Lambda's
    // GET /api/update IGNORES the channel query and serves ONE manifest (backend/lambda_function.py
    // :522 record_id="current"; docs/SERVER_HANDOFF.md:251 "no per-ring manifests yet"), so this is
    // behaviour-neutral TODAY — but when per-ring manifests land a "stable" manifest MUST be
    // published or shipped clients stop seeing updates (fail-soft: they keep running).
    QString updateChannel = QStringLiteral("stable");
    // settings.json is excluded from the installer, so these values define a
    // clean customer's first detection session. The certified 2K27 locator and
    // current rig use the white Arrow2 meter; retaining the retired Red default
    // makes the learned box succeed while the fill mask reads the wrong colour.
    QString meterColor = QStringLiteral("White");
    QString meterStyle = QStringLiteral("Arrow2");
    // Which proposer hands SimpleMeterReader its meter box (the Live page's
    // "Meter Detection" card): "cv" | "yolo", see normalizedMeterProposer().
    // Reaches the sidecar as ORION_METER_PROPOSER at launch and is read once
    // when the locator singleton is built, so a change applies to the NEXT
    // sidecar launch, never mid-session.
    QString meterProposer = QStringLiteral("cv");
    // [ORION_PILL_YOLO_ROUTE 2026-09-17] The kill switch on the style->proposer
    // route (orion::applyPillYoloRoute, SidecarReaderProfile.h). Default TRUE:
    // when meterStyle is "Pill" the sidecar launches with ORION_METER_PROPOSER=
    // yolo whatever meterProposer says, because the CV contour locator proposes
    // a box on 0/642 measured Pill frames while the packaged ONNX net reads
    // 661/661. Set false (or ORION_PILL_YOLO_ROUTE=0) to launch the persisted
    // proposer unchanged -- the pre-route environment, byte for byte, with a
    // "METER STYLE MISMATCH:" line in the launch log if that leaves the reader
    // blind. A non-Pill style never consults this key at all.
    bool pillYoloRoute = true;
    QString remotePlayClientMode = QStringLiteral("chiaki");
    // PS5 uses the existing Chiaki route. Xbox attaches to a selected Microsoft
    // client window via WGC and uses X360 ViGEm, without owning the client.
    // Switching consoles preserves PS5's capture settings and separates leads.
    QString remotePlayConsole = QStringLiteral("PS5");
    QString xboxRemotePlayWindowTitle;
    // First-run onboarding: false until the user finishes the post-auth Stream Setup
    // screen once; afterward stream setup is edited on the Dashboard.
    bool streamSetupComplete = false;
    // First-run preflight wizard: false until the user finishes (or skips) the
    // capture-card / Remote Play / test-shot check flow once. Re-runnable any time
    // from the Setup Guide; this flag only controls the automatic first-run launch.
    bool preflightComplete = false;
    // Legal agreement + rules acceptance (post-auth gate). Stores the TERMS VERSION the
    // user last accepted (0 = never accepted). The gate re-shows whenever the accepted
    // version is below kCurrentLegalVersion, so bumping that constant when the agreement
    // text changes forces everyone to re-read + re-accept the new terms.
    static constexpr int kCurrentLegalVersion = 1;
    int legalAcceptedVersion = 0;
    // Detection video source: "capture_card" (an HDMI capture device read via cv2.VideoCapture
    // — a clean, low-latency 1080p60 path; DEFAULT while we harden the HDMI path)
    // or "decoder" (the remote-play decoder pipe — no extra hardware). Drives the sidecar's
    // ORION_CAPTURE_CARD env and, in capture-card mode, keeps the chiaki window hidden (input-only).
    QString videoSource = QStringLiteral("capture_card");
    int captureCardIndex = 0;
    // [ORION_CAPTURE_FPS 2026-09-14] The rate the capture card is ASKED for, in fps.
    // {30, 60, 120} only (see snappedCaptureCardFps above); 60 is the shipped default and the
    // rate every previous build hard-coded, so an install that never touches this is unchanged.
    // Passed to the sidecar as ORION_CAPTURE_FPS and consumed by CaptureCardBackend(fps=...).
    // What the driver actually NEGOTIATES may differ — that is reported separately in the
    // "Capture health: ... cap_mode=" line, next to the requested value.
    int captureCardFps = 60;
    // Hardware video decode (chiaki hw_decoder=d3d11va). DEFAULT OFF — live 2026-06-24: enabling
    // it BROKE the decoder-pipe export ("Failed to push frame: Invalid data" in the OrionStream
    // d3d11 hw->sw readback path), so no frames flowed -> decoder-export stall -> connect/reconnect
    // loop + the chiaki window popped out. Software FFmpeg decode keeps the export working (CPU
    // frames, no readback). The ~38fps software cap (CPU contention) is the trade, but a working
    // stream beats a broken one. Re-enabling hardware decode needs the OrionStream d3d11 readback
    // fixed first (chiaki-ng fork); the toggle is left for that future A/B.
    // Kept only for settings-file compatibility. Production UI does not expose
    // this unsupported split toggle, and RemotePlaySession ignores the persisted
    // value for both hw_decoder and use_zero_copy. ORION_HW_DECODE=1 is the one
    // explicit diagnostic opt-in and enables both halves atomically.
    bool hardwareDecode = false;
    // Virtual pad target: "X360" (default — chiaki maps it to PS5) or "DS4" (DualShock).
    // Applied on the next stream connect.
    QString controllerType = QStringLiteral("X360");
    // Auto-reconnect: when the stream drops mid-session (detection sidecar exits), fully
    // reconnect instead of going to safe mode. Off by default.
    bool autoReconnect = false;
    QString remotePlayWindowTitle = QStringLiteral("Chiaki");
    QString chiakiPath;
    QString remotePlayConsoleIp;
    QString remotePlayProfile;
    // Input ownership: "square" (safe default), "stick", or "both". The
    // explicit `both` route enables Button/TempoSquare plus qualified raw
    // TempoStick and Go-To gestures without changing the default for users.
    QString remotePlayInputSource = QStringLiteral("square");
    // Default Performance (720p60/12 Mbps), not Balanced (4 Mbps): the compressed-path error
    // budget puts 4 Mbps exactly at the CV-vs-transport parity point — on a wired LAN the
    // 12 Mbps rung is worth ~+9pp green for no bandwidth reason not to.
    // 2026-07-25 SHIPPING RECONCILIATION — KEPT Performance, intentionally different from the rig
    // (settings.json stream_bandwidth_mode=Balanced, which is simply STALE: it predates the flip
    // recorded in docs/COMPRESSED_READER_PICKUP_PROMPT.md:22 "default rung Balanced->Performance").
    // Both rungs are 720p60 h264 — the only delta is 12 vs 4 Mbps — and the measured P(made) ceiling
    // table (docs/COMPRESSED_PATH_DESIGN.md:36) puts Performance at 82-88% vs Balanced 68-78%
    // ("acquire-blind chroma + green-tip death"). Downgrading to match the rig would ship the worse rung.
    QString streamBandwidthMode = QStringLiteral("Performance");  // Quality | Performance | Balanced | LowBandwidth | UltraLow | Experimental120 | Experimental240
    // Chiaki render backend: vulkan (+zero-copy) is the proven low-display-latency
    // pair; opengl is the embed-safe fallback the watchdog flips to (and persists)
    // if the Vulkan surface won't reparent into the capture panel.
    QString streamRenderBackend = QStringLiteral("vulkan"); // vulkan | opengl
    bool streamAudioEnabled = false;
    QString streamAudioMode = QStringLiteral("Off"); // Off | Standard | Stabilized
    bool controllerLightbarEnabled = false;
    QString controllerLightbarColor = QStringLiteral("#2563EB");
    QString controllerLightbarMode = QStringLiteral("Solid"); // Solid | Pulse | Strobe | Rainbow
    QString controllerLightbarPrimaryColor = QStringLiteral("#2563EB");
    QString controllerLightbarSecondaryColor = QStringLiteral("#4F8CFF");
    double controllerLightbarBrightness = 1.0;
    double controllerLightbarEffectSpeed = 1.0;
    // ---- detection overlay appearance (presentation only) --------------------
    // These three never reach the detector, the scheduler, or any timing path.
    // They style the lock box that is drawn OVER the preview, nothing else.
    //
    // The default is vivid MAGENTA (owner directive 2026-08-31). The 1px dark
    // keylines that bracket the stroke keep it legible over both the white court
    // and the meter fill without widening or obscuring the detected geometry.
    //
    // The default lives HERE as data — QML binds `meterOverlayDrawColor` and
    // never hardcodes a hue — so future restyles are a constant change, not a
    // code change.
    //
    // Since 2026-08-06 the colour is ALSO not user-customisable: the owner
    // removed the picker, and the settings
    // loader pins every persisted meter_overlay_color / meter_overlay_rgb back
    // to this default (see AppConfig::loadSettingsObject). The property
    // plumbing (meterOverlayColor / meterOverlayRgb / meterOverlayDrawColor)
    // stays because the draw path, the HUD accent, and the overlay tests all
    // ride it — only the ability to change the value was removed.
    //
    // Colour history (each entry is a one-constant change; the loader pin below
    // migrates every existing install on its next launch):
    //   pre-2026-08-06  #CC44FF  reference violet (kMeterOverlayLegacyDefaultColor)
    //   2026-08-06      #FF2BD6  magenta
    //   2026-09-14      #1E90FF  magenta -> blue #1E90FF, owner: blue, visible on
    //                            light and dark. Theme.meterLock carries the same hex.
    static constexpr const char* kMeterOverlayDefaultColor = "#1E90FF";
    // The pre-2026-08-06 factory default (the reference violet). Kept as a
    // named constant for the tests that pin the migration history; the loader
    // no longer needs to single it out because it now pins EVERY persisted
    // colour to the default above.
    static constexpr const char* kMeterOverlayLegacyDefaultColor = "#CC44FF";
    QString meterOverlayColor = QLatin1String(kMeterOverlayDefaultColor);
    // Solid: full 2px rectangle (the reference look).
    // Brackets: corner brackets only — leaves the meter's long edges completely
    //           unobstructed, which matters when the meter is small on screen.
    // Hairline: 1px rectangle for players who want the box nearly invisible.
    QString meterOverlayStyle = QStringLiteral("Clean"); // Clean | Solid | Brackets | Hairline
    // Slow hue cycle. Cosmetic only, and deliberately driven by a 4 Hz coarse
    // timer rather than a per-frame animation — see OrionAppController's
    // overlayEffectTimer_. It is suspended entirely whenever no preview is being
    // rendered, so an idle app does zero work for it.
    bool meterOverlayRgb = false;
    QString rttSyncMode = QStringLiteral("Auto");
    double manualSyncAdjustMs = 0.0;
    double manualOffsetMs = 0.0;
    // Defense Mode: a physical pad button toggles automation fully off (Square
    // passes through untouched) plus optional defensive assists. Lightbar flips
    // to defenseLightbarColor while active.
    QString defenseTriggerButton = QStringLiteral("dpad_up"); // dpad_up | dpad_down | dpad_left | dpad_right
    bool defenseStickAssist = false;        // faster lateral first-step response curve
    double defenseStickAssistStrength = 0.5; // 0..1 curve strength
    bool defenseL2HoldAssist = false;       // commit an L2 tap to a full hold
    bool defenseSprintAssist = false;       // commit R2 to full sprint while pressed
    QString defenseLightbarColor = QStringLiteral("#EF4444");
    // Per-jumpshot profile namespace: "Default" keeps the historical learning.json;
    // any other profile reads/writes learning.<slug>.json so switching builds never
    // trashes another build's calibration. Profile metadata lives in settings.json
    // under "profiles" (held in AppConfig::profiles_).
    QString activeProfile = QStringLiteral("Default");
    QString greenWindowTargetMode = QStringLiteral("tip");
    // Deterministic feedforward-clock anchor: "meter_appear" (release timed from when the
    // meter surfaces — precise on fast shots) or "hold_start" (timed from the hold begin).
    // Runtime knob for the live A/B; see RemapConfig::feedforwardAnchor.
    QString feedforwardAnchor = QStringLiteral("meter_appear");
    // Anchor-validity gates for the meter-appear clock (T2 carryover rejection); see
    // RemapConfig::anchorMaxFirstFillPct / anchorRiseMinPct. Live-tunable.
    double anchorMaxFirstFillPct = 40.0;
    double anchorRiseMinPct = 3.0;
    // Candidate-A decision-budget flag (2026-08-05, default OFF): strict autonomous
    // ownership grants on the 2nd unique rising frame instead of the 3rd, recovering
    // one detector frame (~17ms) of the 93ms anchor->deadline budget WHEN the rise
    // proof (anchorRiseMinPct) already passes at sample 2 — offline replay of 369
    // real shot episodes measured that at 58% of budget-critical shots, with zero
    // additional false-lock admissions (tools/timing/validate_ownership_proof.py).
    // Do not pair with a raised anchorRiseMinPct: 4.0pp re-creates the 3rd-frame
    // wait (median saving 0ms) and was measured to buy nothing on false locks.
    bool ownershipProofTwoFrame = false;
    // Candidate-B decision-budget flag (2026-08-05, default OFF): move the tip-phase base
    // anchor 30 -> 20, widening the anchor->deadline decision budget 93 -> ~151ms. ONE flag
    // moves the whole coupled constellation (constant/seed/learn-band shift by the measured
    // 58.3ms, ladder count 2 -> 4, per-rung measured dating offsets) -- see
    // RemapConfig::anchorBase20. learning.json stays canonically base-30 either way.
    bool tipPhaseAnchorBase20 = false;
    // [ORION_ANCHOR_CONSENSUS] Refine a witnessed base-20 phase date once the same
    // shot has independently crossed 25 and 30. The three observed crossings are
    // mapped onto the base-20 clock and median-combined; no curve extrapolation or
    // outcome feedback is involved. Default OFF until a counted live A/B passes.
    bool tipPhaseAnchorConsensus = false;
    // [ORION_TIP_PHASE_FIRST_SIGHT] (2026-09-14, default ON): when the meter's FIRST genuine
    // accept for the shot already sits above every ladder rung the phase model could still
    // witness, anchor the phase model on that first sample instead of standing down onto the
    // sampler. Measured tonight: one shot whose first accepted fill was 46.41 fell to
    // source=sampler (sigma 23.6 vs the phase member's ~13) and died live_tip_deadline_missed
    // at lateness 41.6 ms, while the same session's other 16 reservations were source=phase
    // with a 354 ms median runway. On courts/lighting where the first accept lands at ~22 %
    // the base-20 anchor is still caught; at 46 % nothing below is reachable ever again.
    // Set false (or ORION_TIP_PHASE_FIRST_SIGHT=0) to restore the sampler fallback exactly.
    bool tipPhaseFirstSightAnchor = true;
    // [ORION_TYPE_TRIM] (2026-08-06, default OFF): per-shot-type animation trim for the
    // tip-phase member. The engine applies ONE pooled animation constant to every shot type,
    // but the measured anchor->stop animation differs by type: across three flag configs
    // (base30+snap / base20+snap / base20+subframe, 500 PHASE SAMPLE observations,
    // logs/orion_native.log{,.1}) Right-Fade-labelled shots dated -6.5/-6.4/-5.5 ms vs
    // Standstill and Left Fade -1.6/-5.8/-4.1 ms. The trim is added to the tip decision AND
    // subtracted from the learner's observation (symmetric), so the pooled learner keeps
    // estimating the Standstill-scale base and cannot fight the trim. Values are clamped to
    // single-digit ms (+/-9) on read; keys are exact classifyShotType labels. Deliberately NOT
    // routed through shot_type_offsets (legacy feedforward-path trim with a shared +/-150 cap
    // already carrying live learned values). CAVEAT the defaults ship on: the historical
    // fade-labelled samples are stick-deflected presses, not confirmed deliberate fadeaways --
    // flip tip_phase_type_trim_enabled only after a dedicated counted fade batch confirms.
    bool tipPhaseTypeTrimEnabled = false;
    QMap<QString, double> tipPhaseTypeTrimMs {
        {QStringLiteral("Left Fade"), -4.0},
        {QStringLiteral("Right Fade"), -6.0},
    };
    // [ORION_STOP_CORROBORATE] (2026-08-06, default OFF): require two accepted samples before
    // re-dating the end-of-rise stop, so a single-frame fill spike above a settled plateau
    // cannot be learned as a long animation. Measurement/learning only — see
    // RemapConfig::stopReopenCorroborate.
    bool stopReopenCorroborate = false;
    // [ORION_STOP_SUBFRAME] (2026-08-06, default OFF): date the end-of-rise stop SUB-FRAME by
    // intersecting a fit of the last rising samples with the settled plateau, instead of
    // snapping to the capture frame that tripped the step threshold. The anchor crossing is
    // already interpolated (crossing noise 0.8-0.9ms) while the snapped stop carries ~6x that;
    // that noise feeds the phase learner, which sets the aim. Measurement/learning only — see
    // RemapConfig::stopDatingSubframe. Composes with stop_reopen_corroborate (the refinement
    // runs around whatever snapped stop the corroboration logic committed).
    bool stopDatingSubframe = false;
    // [ORION_PHASE_VETO_DIRECTIONAL] (2026-08-06, default OFF): make the live-meter corroboration
    // veto one-directional so a sampler running its own measured early bias can no longer demote a
    // healthy dated phase member. Measured cause of 16 no-fire aborts + 3 counted earlies.
    // 2026-08-08: BOTH activation paths were attempted and withdrawn the same day. The compiled
    // default flip and the settings_version v1 migration each turned this ON, and with the flag
    // ON, 12 autonomous tip-decision pins in AutomationEngineTests fail under the then-in-flight
    // engine -- shots stuck at waiting_for_live_tip_deadline with deadline=-1, plus the
    // tempo/goto LeavesButtonShotBitIdentical contracts -- confirmed by an A/B/A rebuild on this
    // flag alone. That is the identical no-fire class this flag exists to remove, so the risk is
    // asymmetric (OFF = the bounded, measured 16+3; ON = possibly unbounded no-release) and the
    // flag ships OFF everywhere. Re-enable (default OR migration) only after the AutomationEngine
    // owner adjudicates the 12 pins AND a counted live batch confirms; full history in
    // AppConfig::settingsMigrations().
    bool phaseVetoDirectional = false;
    // [ORION_TIP_FRAME_NATIVE 2026-09-15] Fire on the CONSOLE'S FRAME GRID rather than at a
    // random phase inside it, and date the anchor crossing to the game frame it happened in.
    // Default TRUE. The console samples the pad once per frame, so a release quantises onto that
    // grid; a frame-CENTRED target carries the full +-8.3 ms of margin before jitter pushes it
    // into the neighbouring frame, where a uniformly-phased target carries none. It moves the
    // mean release by at most half a frame, once, which the banner shows and Shot Lead absorbs.
    // Set false (or ORION_TIP_FRAME_NATIVE=0) to restore the instant target and the straddling-
    // pair anchor interpolation byte-for-byte. Full contract on RemapConfig::tipFrameNative.
    bool tipFrameNative = true;
    // [ORION_AIM_FREEZE] (2026-08-06, default OFF): hold the learned aim constant still for the
    // session. Measured: the aim walked 8.2ms across one 70-release batch (landing sd 10.5ms), and
    // that session's second half measured worse than its first. Demo/batch tool — freeze a
    // known-good aim; clear it when the equipped jumpshot changes.
    bool tipPhaseAimFrozen = false;
    // [ORION_AIM_AUTOUNLOCK 2026-08-13] When the locked aim above is contradicted by this rig's
    // own full-window instrument for ten consecutive windows, hand the value back to the learner
    // instead of only logging it. Was default ON from 2026-08-13 because the warn-only path is
    // invisible in practice (fifteen divergence lines, none acted on).
    //
    // [ORION_FREEZE_RIDES_RELEASE 2026-09-01] Default OFF. The instrument the unlock trusts is
    // anchor-to-FREEZE, and the freeze rides the release (recordPhaseConstantSample: "the meter
    // freezes when the game registers the press"), so it measures where the bot LANDED, not
    // where the animation's tip is. Handing that number to the learner moves the aim in the
    // WRONG direction: a freeze arriving sooner than the calibrated aim is consistent with an
    // early landing, and the learner's response is to predict the tip earlier and fire earlier
    // still. The 2026-09-01 logs repeatedly estimated roughly a 20 ms early shift, but lacked
    // human banner labels and cannot establish its exact size. The structural sign error is
    // sufficient to make this opt-in only; use it only where freeze has independently been shown
    // to track the tip rather than the release. See
    // AutomationEngine::maybeWarnTipTimingDivergence for the corrected operator advice.
    bool tipTimingAutoUnlockEnabled = false;
    // [ORION_SESSION_LEAD_PROBE 2026-09-01] Per-session lead correction from the session's own
    // anchor->freeze timing (settings session_lead_probe, default OFF). Measured need: at one
    // Shot Lead the Standstill landing median moved 95.2 / 97.1 / 96.5 / 95.6 across four
    // consecutive sessions while the anchor->freeze median moved 322 / 342 / 335 / 331 with it --
    // a per-connect registration delay, i.e. a constant the lead should absorb. The probe takes
    // the median of the first sessionLeadProbeMinSamples accepted phase samples, compares it to
    // the timing under which the current Shot Lead was calibrated (LearningData::
    // leadReferencePhysicalMs, captured automatically the first time a lead value is used) and
    // shifts the lead ONCE by gain * difference, bounded, then holds for the session. It reads a
    // latency, never a landing grade, and never moves the aim constant.
    bool sessionLeadProbeEnabled = false;
    // [ORION_SOURCE_STEAL_GUARD 2026-08-13] A candidate decision whose sigma is materially worse
    // than the armed token's may not replace it (default ON). The measured rare-late mechanism:
    // fallback arms that evicted a phase token in the final ~100 ms landed bad 59-75% vs phase
    // 14.7% (n=314, 2026-08-13). Replacement-only -- fallback members still arm freely when
    // nothing is armed.
    bool tipSourceStealGuardEnabled = true;
    // Rung-imminent hold flag (2026-08-06, default OFF): extend the [ORION_TIP_PHASE_IMMINENT]
    // hold so it also waits for the next LADDER RUNG, not only the base anchor. Measured need
    // (logs 2026-08-04..06): 27 of 62 rejected_missed aborts were FIRST-TICK sampler kills
    // (reservation_updates=1, source=sampler) on shots whose fill sat in the un-witnessed gap
    // below a rung -- 19 of them within ~5pp of the next rung, i.e. 1-2 frames from a phase
    // crossing. Under tip_phase_anchor_base20 this class DOMINATED the first live batch's
    // residue (4 of 6 misses): a meter first seen at ~22 fill is ABOVE the 20 base anchor, so
    // the base-only hold declines and the +61-92ms-early-biased sampler aborts the shot on its
    // first decision tick. See RemapConfig::tipPhaseRungImminentHold for bounds and risks.
    bool tipPhaseRungImminentHold = false;
    // Tempo tip-parity flag (2026-08-06, default OFF): give TempoStick the same live-meter
    // tip-timing standing ButtonShot/TempoSquare already have on the canonical autonomous
    // path. Two coupled changes, both engine-side and both fail-closed (see
    // RemapConfig::tempoTipParity): (1) an evidence-promoted TempoStick enters Holding
    // directly instead of the Armed detection-confirm dwell (the dwell re-proves what the
    // 3-frame ownership proof just proved and is a release blackout — the same reasoning
    // that moved TempoSquare direct-to-Holding on 2026-07-24), and (2) TempoStick joins the
    // owned-square bounded-hold semantics (phase-anchor-imminent hold, contested-runway
    // hold, lease continuation across detector blinks, measured-lead-loss grace) instead of
    // aborting where ButtonShot would hold. The tempo gesture semantics (movement
    // transaction, stick intent dwells, RS flick release) are untouched. OFF = bit-identical
    // to today for every mode; ON changes TempoStick only.
    bool tempoTipParity = false;
    // Go-To tip-parity flag (2026-08-06, default OFF): give GoToStick the same owned bounded-hold
    // standing ButtonShot/TempoSquare already have on the canonical autonomous path (see
    // RemapConfig::gotoTipParity). GoToStick already promotes direct to Holding, so unlike
    // tempo_tip_parity this is ONE change: GoToStick joins the ownedHold predicate in
    // processAutonomousLiveMeterHolding (measured-lead-loss grace, detector-blink lease
    // continuation, unresolved-trajectory hold, phase-anchor-imminent hold, contested-runway
    // hold, slow-meter defer) plus its subtick mirror, instead of aborting where ButtonShot
    // would hold. Fail-closed untouched: every hold keeps the absolute maxHoldMs ceiling and
    // no branch gains a release path. OFF = bit-identical for every mode; ON changes GoToStick only.
    bool gotoTipParity = false;
    // [ORION_USER_LEAD_AUTHORITY] (2026-08-06, default OFF): a USER-SET actuation_lead_ms may
    // satisfy the measured-lead readiness gate on its own. Motivation (measured, sessions
    // 2026-08-06 21:52Z/22:23Z + cold-install audit): measuredLeadForActuationMs() RETURNS the
    // user's in-band lead and DISCARDS the estimator's value, yet measuredLeadAuthoritative()
    // still demanded a validated/n>=6/sd<=3.3 (or structurally-matching factory) posterior —
    // a gate on a quantity the fire path throws away. A cold session therefore refused every
    // press (`waiting_for_latency_calibration`) until a manual probe run converged, twice in a
    // row on launch-rehearsal day. With this ON, readiness additionally accepts:
    //   user lead set + in band  AND  live telemetry present + fresh  AND  the exact
    //   controller-route attestation echo (scope epoch + generation + delivery route).
    // The channel-liveness half of the old gate is retained IN FULL; only the posterior-shape
    // half is waived, and only for the lead the engine would have fired with anyway. See
    // RemapConfig::userLeadSatisfiesAuthority for the fail-closed argument.
    // [ORION_USER_LEAD_AUTHORITY 2026-08-07] Default-on so packaged installs can actually
    // fire under a user-typed lead — with default-off, the "Adapting" state after any
    // sidecar reconnect became terminal (documented deadlock at AutomationEngine.cpp:9601+).
    // Every clause is retained (in-band value, live telemetry, exact route echo); this
    // flag only decides which authority is TRIED FIRST, never lowers the bar.
    bool userLeadSatisfiesAuthority = true;
    // [ORION_TEMPO_FADE_MIRROR] (2026-08-06, default ON): run a FADE as the mirrored Tempo
    // gesture — gather RS-UP, release flick DOWN — instead of the gather-DOWN/flick-UP used by
    // every other shot type. Fixes a reproducible step-back-instead-of-fade that appears ONLY
    // with Tempo enabled: Pro Stick DOWN while a movement direction is held is the game's
    // escape/step-back input, so the old unconditional gesture was literally asking for a
    // step-back. Matches how all working Cronus Zen scripts do it. See ShotReleasePolicy.h for
    // the full evidence. Default ON so it is exercised rather than becoming a dead flag; set
    // "tempo_fade_mirror_gesture": false to revert in one line if a live batch disagrees.
    bool tempoFadeMirrorGesture = true;
    // === [ORION_TEMPO_RELEASE_STYLE 2026-09-15 owner] RHYTHM'S RELEASE EDGE =================
    //
    // "flick" (default, and every build before this key existed): the owned RS gather is ended
    // with one full-scale OPPOSING flick — gather down, flick up (-127); a mirrored fade gathers
    // up and flicks down (+127) — held for max(tempoFlickHoldMs, releasePulseMs) and then
    // neutralised. That is the direction-change edge the game reads as a shot release.
    //
    // "letgo": at the SAME release-decision instant the stick is driven to NEUTRAL (0,0) and held
    // there for the same pulse window instead of flicking the other way. The player keeps holding
    // the stick down and Venice performs the let-go for them, so the console sees a clean
    // stick-RELEASE edge rather than an opposing deflection. Everything else about the shot is
    // identical: the pull-down arm, the blind hold law, the vision timing, the hold band, the
    // backstop, the trims and every learner — the release EDGE INSTANT is the same instant, only
    // the packet differs. The existing ReleasedUntilPhysicalEnd drain and three-neutral-poll
    // rearm fencing apply unchanged, which is what keeps the still-held physical stick from
    // re-arming a shot after the let-go.
    //
    // Unknown values are IGNORED (the shipped "flick" stands) rather than rejected or guessed:
    // an unrecognised release style must never silently disable the release edge. Env
    // ORION_TEMPO_RELEASE_STYLE follows the same ignore-unknown policy.
    QString tempoReleaseStyle = QStringLiteral("flick");
    // [ORION_PROBE_CACHE_AUTHORITY] (2026-08-06, default OFF): accept the probe-persisted lead
    // cache's replacement seed source ("probe_cache:self_measured") in the factory-prior
    // authority contract. Pairs with sidecar env ORION_PROBE_PRIOR_AUTHORITY — armed alone on
    // either side it does nothing. Without both, a restored probe cache is a Bayesian warm
    // start only (measured 2026-08-06: the unconditional replacement source failed the
    // venice-e2e-route-prior: prefix check and silently destroyed cold-start factory
    // authority, benching every session-start press). See RemapConfig::probeCachePriorAuthority.
    bool probeCachePriorAuthority = false;
    // [ORION_PROBE_COUNT] Warmup probe-run length (default 16 = today's behaviour). Sizing
    // arithmetic (2026-08-06): the published posterior sd behaves like robust_sd/sqrt(n_accepted)
    // against the 3.3ms converged gate; measured probe-raw robust_sd is 10.6-13.9ms and ~2-3 of
    // 16 probes drop, so a 16-run lands at sd ~2.6-3.5 — a coin flip at the gate (the owner
    // needed TWO manual runs). 24 probes -> ~20-21 accepted -> converges for the whole measured
    // robust_sd range with margin (needs robust_sd <= 3.3*sqrt(20) ~ 14.8). Clamped 4..48.
    // [ORION_PROBE_COUNT 2026-08-07] 16 → 24. Per this file's own comment on this field,
    // 16 lands sd ~2.6-3.5ms vs the 3.3 convergence gate = coin flip. Owner needed two
    // runs to converge on the 2026-08-06 test. 24 converges reliably in one run and adds
    // ~5s to the pre-fire calibration window — a one-time cost per session.
    int latencyProbeCount = 24;
    // Tip gate (T4): defer a DUE feedforward clock to the predicted tip crossing when vision
    // is healthy (see RemapConfig::tipGateEnabled / tipGateCapMs). tip_gate_enabled=false is
    // the no-rebuild rollback to pre-T4 clock-fires-on-reach behaviour.
    bool tipGateEnabled = true;
    // 2026-07-25 SHIPPING RECONCILIATION: 100 -> 300, the proven live rig value
    // (settings.json tip_gate_cap_ms=300). Nothing else compensates for the old default — unlike
    // autonomous_vision there is no env force — so a fresh install genuinely ran a 3x narrower
    // gate than the tuned rig, i.e. the clock preempted crossings the rig lets vision own. The cap
    // is a LATENESS BOUND only (release can never land later than clockDue+cap) and 300 is inside
    // the loader clamp (0..400, AppConfig.cpp:713).
    double tipGateCapMs = 300.0;
    // EXPERIMENT (default OFF): native memory-trust A/B. Promote a <=1-frame-old,
    // trajectory-consistent detector meter_memory echo to fresh-equivalent for the engine
    // freshness gate only, so a single-frame wide-zone detection blink does not drop the
    // engine to stale. The age bound is measured from the last GENUINE accept (never chained
    // through echoes) so a real multi-frame dropout still lapses to stale. A normal persisted
    // knob (NOT force-reset on load like the tempo flags) so it can be A/B'd live; see
    // RemapConfig::memoryTrustEnabled.
    bool memoryTrustEnabled = false;
    double memoryTrustMaxAgeMs = 45.0;     // ~1 frame at 22-30fps; tight on purpose
    double memoryTrustFillSlackPct = 2.0;  // held fill must stay within this of the rise band
    double memoryTrustMaxFillPct = 85.0;   // tip-guard: never trust a held echo at/above this fill%
    // Fast green window confirmation: confirm on the first valid green reading instead of
    // requiring 3 readings within 3% tolerance. For very fast meter styles (Arrow2 Purple).
    bool greenConfirmFastPath = false;
    bool meterEnabled = true;
    // [ORION_METER_DELAY 2026-08-07] Inbound network delay actuator — holds the console's
    // inbound game UDP via nexus_svc (WinDivert) so the shot meter decouples from the
    // animation for a cleaner read. NOT a controller-input delay — the customer's presses
    // reach the game with normal latency; only the RESPONSE (meter render) is delayed.
    //
    // The trade named honestly: OffenseDefense (bypass_on_defense=true) ramps the delay
    // to 0 whenever Defense Mode is on, so the customer isn't reactive-lagged on defense.
    // The bot side of this pays a lead-poisoning cost during the ramp (learned_latency_ms
    // is a frozen scalar), which is why the release predicate ALSO gates on
    // MeterDelayController::conditionSettled() — no shot fires while the delay is moving.
    bool meterDelayEnabled = true;
    int meterDelayMs = 250;  // clamped into [kMeterDelayMinMs, kMeterDelayMaxMs] on load and on the QML setter
    bool meterDelayBypassOnDefense = true;
    // Clamp band for meterDelayMs — shared by the AppConfig load path and
    // OrionAppController::setMeterDelayMs so the two can never drift.
    // [ORION_METER_DELAY_RANGE 2026-08-08] Widened 200-300 → 100-600 (owner
    // request: find the empirical physics ceiling). Raised IN LOCKSTEP with
    // MeterDelayController::kManualMaxMs/kHardMaxMs (600), VENICENET_MAX_DELAY_MS
    // (venicenet/VeniceNet.h), the service MeterDelayIntercept caps and
    // nexus_svc.py _METER_DELAY_HARD_MAX_MS. An already-running bridge service
    // keeps enforcing the cap it started with until restarted.
    static constexpr int kMeterDelayMinMs = 100;
    static constexpr int kMeterDelayMaxMs = 600;
    // Banner-calibrate workflow: when true, the bot FREEZES its post-release self-learning grader
    // (learnFromOutcome). The per-type clock/offset then hold at their learning.json values instead
    // of being adjusted by the meter grader -- which can't tell green from late at the dead-top (both
    // peak ~100 then recede), false-LATEs, and creeps the clock early. Calibration is done from the
    // TIMING banner (offline) and the dialed-in clocks ship open-loop. Default false (learning on).
    bool freezeCalibration = false;
    // Banner calibration: grade each calibration shot from the on-screen TIMING banner (green word =
    // on-target) instead of the post-release meter self-grade, which false-LATEs at the dead-top.
    // Reliable for CALIBRATION only (view latency is irrelevant for a finished shot); live timing
    // never reads the banner. Fed live from the capture pipeline. Default OFF pending a live
    // crop/band-check on the packaged path; when enabled the engine auto-falls back to the meter
    // self-grade if the banner stays silent (see RemapConfig::bannerUnclearFallbackShots).
    bool bannerCalibration = false;
    bool tempoEnabled = false;
    bool tempoRemapEnabled = false;
    bool tempoFlickEnabled = false;
    // Tempo remap output gesture: "button" (held-Square autogreen) or "stick" (right-stick
    // gather -> flick at the meter tip; best for Go-To). Stage-3 scaffold: UI + plumbing in
    // place; the engine stick-flick timing is tuned from a live batch before it drives output.
    QString tempoRemapType = QStringLiteral("button");
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] #88. Tempo remap consumes Square, which also disables
    // the steal. A stick click becomes a real Square that the remap never touches. Default R3:
    // L3 is turbo in NBA 2K, so binding Square there would steal on every sprint. See
    // RemapConfig::squarePassthroughEnabled for the full rationale.
    bool squarePassthroughEnabled = true;
    QString squarePassthroughButton = QStringLiteral("r3");   // "r3" | "l3" | "none"
    // === [ORION_SPRINT_RELEASE_ON_SQUARE 2026-09-16 owner] ==================================
    // "next press does nothing, the Square button is stuck and you can't shoot, so I have to
    // manually tempo with my stick."
    //
    // THE MEASUREMENT (2026-09-16 03:23:51-54Z, and an earlier session with the same shape).
    // All five of the long dead presses -- held 923-980 ms, NO meter ever drawn, no shot ever
    // started by the game -- share one signature at the Square-down edge:
    //
    //     the left stick PINNED (|ls| >= 100, e.g. ls=(127,35))  AND  R2 = 255 (sprint)
    //
    // Everything Venice owns was healthy on those presses: the fork ACCEPTED the Square-down
    // every time (`square_bit=1 delivery_stage=local_udp_accepted`), both physical edges
    // propagated, and a right-stick pull-down/up 0.4 s later produced a meter within 486 ms --
    // so the console was alive and listening. NBA 2K27 simply DOES NOT START A JUMP SHOT on
    // Square while the player is at full sprint with the ball; the Pro Stick does, which is
    // exactly why the owner's manual stick tempo always works when the button appears "stuck".
    // Answered presses with R2 held do exist (26 % of 312) -- but at lower stick magnitudes,
    // i.e. moving pull-ups, which the engine already types as fades from the stick lean.
    //
    // THE FIX. At the physical Square-DOWN edge, if the physical R2 is at or above
    // sprintReleaseR2Threshold, the OUTPUT R2 is forced to 0 for the whole of that press. The
    // console then sees "sprint let go, Square pressed" = a pull-up jumper, with a meter. Sticks,
    // every other button and every other byte pass through untouched, and R2 is restored the
    // moment the press physically ends.
    //
    // NOT gated on stick magnitude, deliberately: a full-speed press IS the failing case, and the
    // magnitude cut would only re-introduce the dead band this exists to remove. False positives
    // cost the player a fraction of a second of sprint on a shot they were taking anyway.
    // Env ORION_SPRINT_RELEASE_ON_SQUARE=0/1 on the ignore-don't-clamp policy every other boolean
    // here uses; 0 restores the 2026-09-16 output byte-for-byte.
    //
    // ***** REFUTED LIVE 2026-09-16 23:04-23:05 -- DEFAULT FLIPPED TO FALSE. *****
    // 52 presses with this ON: EVERY press that carried R2 went dead -- 9 long holds (806-1105 ms)
    // and ~30 frantic taps, no meter, no shot -- including one at ls=(14,14), which is an ordinary
    // moving pull-up and not a sprint at all. Standstill presses were unaffected. The base rates
    // WITHOUT the feature refute the premise outright: presses with phys R2 >= 200 AND |ls| >= 100
    // shoot 81 answered / 4 dead-long / 4 backstop, i.e. 91 % of full-sprint presses DO start a
    // jumper. Sprinting does not block Square.
    //
    // What blocks it is an R2 *RELEASE* landing in the same game frame as the Square press. This
    // feature manufactured exactly that on every single press (output R2 255 -> 0 on the Square
    // edge) and so drove the failure rate to 100 %; the player's own thumb does it on ~9 % of
    // sprint pull-ups, which is the original "stuck Square". See squarePressR2HoldMs below for the
    // corrected fix, which holds the trigger THROUGH the press instead of dropping it.
    //
    // The code and its tests are kept (the mechanism is worth being able to re-run on demand);
    // only the pinned default moved. ORION_SPRINT_RELEASE_ON_SQUARE=1 re-arms it.
    //
    // ***** 2026-09-17: A DEFAULT WAS NOT ENOUGH -- THE KEY IS NOW FENCED. *****
    // The flip above moved the COMPILED default, and a compiled default only answers for a file
    // that has no answer of its own. The owner's settings.json still carried the `true` the
    // 09-16 evening session had persisted; the file won, and the 09-17 acceptance session lost
    // every fade to it (9 firings, 0 fades answered). A behaviour measured to kill 100 % of
    // sprint shots must not be re-enableable by a stale file, so AppConfig::load() and
    // AppConfig::save() now force this key false: a persisted true is read (it still has to
    // round-trip), logged once, and ANDed away. ORION_SPRINT_RELEASE_ON_SQUARE=1 is the ONLY
    // door left -- it lifts the fence AND arms the engine's own override, so a dev A/B runs both
    // halves off one env var. See AppConfig::sprintReleaseAllowed().
    bool sprintReleaseOnSquare = false;
    // The DEEP-HOLD cut, in raw trigger units, shared by sprintReleaseOnSquare and
    // squarePressR2HoldMs -- both ask the same question ("was the trigger held down when Square
    // went down?") and one answer keeps them from disagreeing about the same press. 200 of 255 is
    // deep hold: the owner's dead presses all read a saturated 255, while an incidental brush of
    // R2 (or a trigger resting against its spring) never gets near it. CLAMPED into 64..255 on
    // every route -- 64 is already past any resting-trigger noise and 255 means "only a saturated
    // trigger", so no route can install a threshold that shapes an ordinary walk.
    int sprintReleaseR2Threshold = 200;
    static constexpr int kSprintReleaseR2ThresholdMin = 64;
    static constexpr int kSprintReleaseR2ThresholdMax = 255;
    // === [ORION_SQUARE_PRESS_R2_HOLD 2026-09-16 owner] ======================================
    // THE CORRECTED FIX for the "stuck Square". The refutation above leaves one mechanism
    // standing: a press dies when the physical R2 goes UP in the same game frame as the Square
    // going DOWN. The console then receives, inside one 16.7 ms frame, both "let go of sprint"
    // and "shoot" -- and 2K27 answers the trigger edge, not the button.
    //
    // So do the opposite of the refuted feature: at the physical Square-DOWN edge, if the physical
    // R2 is at or above sprintReleaseR2Threshold, LATCH the OUTPUT R2 at that edge value for this
    // many milliseconds. A physical R2 release inside the window is simply not forwarded; when the
    // window ends the output follows the physical trigger again (so the player still lets go, just
    // 50 ms after the button instead of in the same frame as it). Nothing else in the packet is
    // touched, and R2 is never touched at all when Square is not pressed, when the trigger was not
    // deep-held at the edge, on a stick-armed press, or in NO METER.
    //
    // 50 ms is three game frames: comfortably past the frame the press landed in, and short enough
    // that a player who meant to stop sprinting still stops within a fifth of a second.
    // 0 = inert (output byte-identical to the 2026-09-16 build). Env
    // ORION_SQUARE_PRESS_R2_HOLD_MS on the ignore-non-numeric / clamp-numeric policy.
    double squarePressR2HoldMs = 50.0;
    static constexpr double kSquarePressR2HoldMinMs = 0.0;
    static constexpr double kSquarePressR2HoldMaxMs = 150.0;
    // [ORION_SETTLE_SMOOTH_MOTION 2026-08-13] Grade a smoothly-TRANSLATING settled marker. Shipped
    // 2026-08-11 as a compile-time constant with a commit message claiming it was live-A/B-able
    // ("ORION_SETTLE_SMOOTH_MOTION=1"); it never was -- there was no env read, no settings key and
    // no applyConfig line, so enabling it meant editing source and rebuilding. Second-opinion
    // review 2026-08-13 caught the discrepancy. Wired here so the Go-To A/B can actually be run.
    //
    // STILL DEFAULT OFF, and that is not timidity: with the bbox gate relaxed, fill-freeze becomes
    // the sole discriminator, and AutomationEngineTests::settleGradingRequiresBboxMotionSignal
    // guards a false-EXCELLENT bug that ALREADY SHIPPED once (DetectionResult built with no x/y/w/h
    // -> all bbox motion read as 0 -> sliding fades looked settled). Turning this on trades a known
    // measurement gap for a known correctness risk; it is a diagnostic session flag, not a default.
    bool meterSettleAllowSmoothMotion = false;
    // Per-type shot-mode override: type -> "auto"|"normal"|"tempo" (auto/absent = global tempo).
    // Lets Standstill stay a normal Square while fades use the tempo gesture, etc.
    QMap<QString, QString> shotTypeModeOverride;
    // Per-type tempo flick-edge hold (ms); absent -> global tempoFlickHoldMs.
    QMap<QString, double> shotTypeFlickHoldMs;
    // Layer B: once a tempo/fade type's clock is armed, the deterministic open-loop clock owns the
    // release (vision = seed + abort-safety). DEMOTED to false (do #3) so tempo times like the button
    // (open-loop only fills the invisible-meter / view-latency gap, reachability-capped). true restores
    // the old vision-vs-clock race.
    bool tempoOpenLoopPrimary = false;
    // Reachability slack (track-%) for the open-loop feedforward fire (do #1). See
    // RemapConfig::ffReachabilitySlackPct. Live-tunable; default matches the engine knob.
    double ffReachabilitySlackPct = 12.0;
    // Layer A safety: left-stick deflection (raw units) + consecutive frames required to cancel a
    // Go-To/Tempo before the flick commits.
    double lsCancelThresholdPct = 65.0;
    int lsCancelFrames = 3;
    // Layer C: auto-calibration mode. The reliable feedback-text oracle may dial the per-type offset
    // (ACQUIRE->LOCK) even under freezeCalibration; the unreliable meter self-grade stays frozen.
    bool calibrationMode = false;
    // HYBRID GLOBAL PHASE CLOCK (settings autonomous_vision, default OFF). When ON the engine times
    // every shot type on ONE global meter-appear->tip clock + ONE slow global latency correction
    // (learning.json globalAppearToTipMs / learnedLatencyMs), with live vision applying only a small
    // bounded phase nudge (not 110ms extrapolation, not release authority). OFF = the proven per-type
    // clock/offset path. A normal persisted knob (NOT force-reset on load) so it can be A/B'd live;
    // the per-type path stays the fallback until the global model beats it offline AND live.
    // See RemapConfig::autonomousVision.
    // 2026-07-25 SHIPPING RECONCILIATION — KEPT FALSE even though the rig runs autonomous_vision=true,
    // because a fresh install ALREADY runs the autonomous path: OrionAppController's constructor
    // unconditionally qputenv's ORION_AUTONOMOUS_VISION=1 before the first syncBackendConfig()
    // (OrionAppController.cpp:1117-1125, re-affirmed by the envDefaultOn block at :1191), and
    // AutomationEngine::applyConfig ORs that env in (`settings.autonomousVision || IsSet(env)`,
    // AutomationEngine.cpp:1079-1080). The OFF default is the documented PREMISE of that force
    // ("Keeps a stale settings.json from dropping the launch build back to the per-type path"), and
    // it is what keeps a default-constructed AppConfigData on the safe per-type path — 166 native
    // tests build one and only 25 opt into autonomous. Flipping it here would silently re-path ~141
    // tests for zero production behaviour change. Only opt-out (=0) is honoured, as designed.
    bool autonomousVision = false;
    // SHADOW MODE (autonomous_vision_shadow): COMPUTE + LOG the autonomous release deadline without
    // controlling output, for offline/live A/B against the actual release. See RemapConfig.
    bool autonomousVisionShadow = false;
    // === Tip-timing improvement flags (2026-07 W-series; each defaults OFF = CURRENT behavior) ===
    // A/B-able via settings.json OR the matching env var. See RemapConfig for the full semantics.
    //   measured_lead        -> ORION_MEASURED_LEAD  : measured_latency_ms as the lead base (~70ms).
    //   tick_lock            -> ORION_TICK_LOCK       : phase-align the fire deadline mid-console-tick.
    //   reg_fusion           -> ORION_REG_FUSION      : reg_tip_ms as the far-horizon predictor member.
    //   lead_learner_vision_gate -> ORION_LEAD_VISION_GATE : gate the lead learner on fire-time vision.
    bool measuredLeadEnabled = false;
    bool tickLockEnabled = false;
    bool regFusionEnabled = false;
    bool leadLearnerVisionGate = false;
    //   fused_fire           -> ORION_FUSED_FIRE    : Phase-1 fused t_tip posterior fire AUTHORITY
    //                           (new ladder: fused primary, feedforward backstop). Default OFF.
    //   fused_shadow (default ON) : compute + log the fused decision per shot ("FusedShadow:")
    //                           without controlling output — the live A/B before the flip.
    bool fusedFireEnabled = false;
    bool fusedShadowEnabled = true;
    //   grade_v2             -> ORION_GRADE_V2       : Phase-2 trajectory grading (A3). Replaces the
    //                           settled-fill post-release rules with G/R/D trajectory classification and
    //                           becomes the SOLE writer of shotTypeLearnedOffsetMs (legacy learners gated
    //                           off). Default OFF — the user gate: closed loop only after offline
    //                           validation on recorded framedumps (user decision 2026-07-06 №2).
    bool gradeV2Enabled = false;
    //   blind_fire_suppress  -> ORION_BLIND_SUPPRESS  : suppress a feedforward fire on a rejected/no-fill
    //                           frame that is off-schedule (RC-3/C3 blind-fire guard). See RemapConfig.
    bool blindFireSuppressEnabled = false;
    double blindFireSchedTolMs = 50.0;
    //   prior_posterior_blend -> ORION_PRIOR_POSTERIOR : fuse the clock prior with the vision posterior by
    //                           inverse variance instead of the hard feedforward preempt (C2). See RemapConfig.
    bool priorPosteriorBlendEnabled = false;
    // === Ceiling stack flags (2026-07 perfect-green build; each defaults OFF = current behavior) ===
    //   plateau_aim      -> ORION_PLATEAU_AIM     : H6 aim the cap-hold plateau time-center (green=make).
    //   template_arrival -> ORION_TEMPLATE_ARRIVAL: H4 template-matched green-arrival prior (t@96).
    //   press_t0         -> ORION_PRESS_T0        : H5 press-anchored template select (press is native).
    //   bandit_lead      -> ORION_BANDIT_LEAD     : warmup-only bandit lead micro-calibration.
    bool plateauAimEnabled = false;
    bool templateArrivalEnabled = false;
    bool pressT0Enabled = false;
    bool banditLeadEnabled = false;
    // === [ORION_USER_LEAD] Shot Lead — the ONE user-facing timing control ==================
    // The actuation lead in ms: how far AHEAD of the predicted meter tip the release command is
    // submitted, to cover this install's end-to-end latency (capture -> detect -> USB/ViGEm ->
    // console input tick). A LARGER lead fires EARLIER.
    //
    // This exists because the engine's own measured authority is not trustworthy as a shipped
    // default: latency_estimator.py timestamps the meter's first UPWARD crossing of f_stop, but
    // the meter overshoots to ~97-100% and only then recedes, so the label reads ~70-80ms SHORT
    // and the posterior walks the lead DOWN over a session (see RemapConfig::autonomousLeadBiasMs
    // for the full derivation and the live banner evidence). The user has a complete, zero-setup
    // feedback loop the engine does not: the game renders a per-shot TIMING banner. They read it
    // and nudge this control; from then on the install is plug-and-play.
    //
    // 0 = NOT CONFIGURED. A fresh install therefore behaves EXACTLY as before (raw measured
    // authority + any dev floor/bias) until either the landing measurement seeds it or the user
    // moves it. It is deliberately NOT defaulted to the value that works on the dev rig — that
    // number is a property of one machine's capture card, PC, and LAN.
    //
    // RANGE {0} u [150, 800]:
    //   * Floor 150: end-to-end on ANY install is capture exposure + encode/decode + detection +
    //     virtual-pad write + a 16.7ms console input tick. Even a direct-HDMI, low-latency rig
    //     cannot get under ~100ms; 150 leaves room below every plausible install while making a
    //     fat-fingered 15 impossible.
    //   * Ceiling 800 (was 450 until 2026-08-08): the old ceiling assumed the lead only covers
    //     transport latency, but the injected Meter Delay ADDS to the total the banner reports —
    //     the owner runs 250ms meter delay on a ~290ms baseline and needs ~540ms, which the 450
    //     cap made unreachable (the slider was pinned). 800 = the 300ms meter-delay hard max
    //     (MeterDelayController::kHardMaxMs) on top of a ~500ms worst-case transport lead.
    //   * The (0, 500] hard gate on the latency AUTHORITY (measuredLeadForActuationMs's env
    //     envelope, latencyAuthorityContractValid) is NOT widened and does not conflict: the
    //     user lead REPLACES the authority and is validated only against this band — it never
    //     routes through the authority's own <=500 clauses (those bind the estimator posterior
    //     and factory prior, which remain transport-only measurements).
    double actuationLeadMs = 0.0;
    // [ORION_PROBE] How long the warmup probe holds virtual Square. Tunable from settings.json
    // ON PURPOSE: the value that actually spawns a shot meter is a property of the GAME and the
    // player's build, not of this code, and it cost three rebuild-and-relaunch cycles to discover
    // that 150 and 400 both fail on the owner's setup. 0 or absent -> the engine default.
    // A probe must produce a real jump shot: too short and the game renders no meter at all, so
    // the estimator collects zero rise samples and every probe expires.
    double probePressMs = 0.0;
    // [ORION_PROBE] Gap between probe presses. Tunable for the same reason as probePressMs: how
    // long it takes to get the ball back is a property of the venue and the game mode, not of this
    // code. 0 or absent -> the engine default.
    double probeGapMs = 0.0;
    // [ORION_PROBE] Delay from the probe's call-for-ball (Cross) to its shoot press (Square).
    // Must cover the pass actually reaching the player; too short and the probe shoots empty
    // handed, which is the failure this whole mechanism exists to remove. 0 -> engine default.
    double probeCallLeadMs = 0.0;
    // [ORION_GREEN_CENTER] How far from the green window's LATE edge toward its centre to aim.
    // 0 = today's behaviour (aim at the edge, which measurement showed happens on 130/130
    // releases); 1 = the exact centre. Sweepable from settings.json so a counted-green A/B needs
    // no rebuild. See RemapConfig::autonomousGreenCenterFrac for the measurement behind it.
    double autonomousGreenCenterFrac = 0.0;
    // Ceiling on the resulting shift in ms, whatever the window and slope imply.
    double autonomousGreenCenterMaxMs = 40.0;
    // true once the USER moved the control. The measured seed then never overwrites it, for the
    // life of the install, unless they explicitly reset back to measured.
    bool actuationLeadUserSet = false;
    static constexpr double kActuationLeadMinMs = 150.0;
    static constexpr double kActuationLeadMaxMs = 800.0;
    // === [ORION_LEAD_BY_SOURCE 2026-09-14] the Shot Lead, remembered PER VIDEO ROUTE ========
    //
    // WHY. actuationLeadMs is ONE number that absorbs the whole end-to-end transport, and the
    // biggest single term in it is the video route: a capture card's exposure+encode+USB path
    // and Remote Play's network+decoder path are different pipelines with different delays
    // (2026-09-14 live data: the decoder route wants roughly 30 ms LESS). videoSource switches
    // between them at a click, and until this stash existed the lead calibrated on one route
    // silently carried to the other — the user re-tuned from the banner every time they
    // switched, or (worse) did not notice and blamed the bot.
    //
    // The latency posterior and the factory prior were already route-scoped on the sidecar
    // side; the USER-SET lead — the one number that actually fires the release, because
    // measuredLeadForActuationMs lets it REPLACE the authority — was not. This closes that gap
    // and nothing else: the engine's lead semantics are untouched, it still consumes exactly
    // one actuationLeadMs.
    //
    // KEYS are exactly the two canonical videoSource values, "capture_card" and "decoder"
    // (actuationLeadSourceKey normalises anything else). An ABSENT entry means "that route was
    // never configured" — identical to actuationLeadMs == 0, so switching to a fresh route
    // lands in the existing not-configured behaviour (the card prompts, the measured seed may
    // run once authority exists) rather than inheriting a number measured on the other pipe.
    //
    // INVARIANT, enforced in AppConfig::save() (the chokepoint every write path goes through)
    // and on load: the entry under the CURRENT videoSource always mirrors the live pair, and a
    // not-configured live pair carries NO entry. So the stash can never disagree with the live
    // value, and "absent" always means one thing.
    QMap<QString, double> actuationLeadBySourceMs;
    QMap<QString, bool> actuationLeadUserSetBySource;
    // === [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Shot Lead offset for the DELAYED condition =
    // Added to actuationLeadMs ONLY while the meter delay is actually applied
    // (AutomationEngine::meterDelayAppliedMs_ > 0). 0 (the default) is byte-identical to every
    // build before this field existed, at every delay value.
    //
    // WHY IT EXISTS. Meter Delay is a NETWORK actuator: VeniceNetSvc holds the game-server ->
    // console UDP flow (MeterDelayIntercept::buildInterceptFilter is literally
    // "ip and udp and ip.SrcAddr == <courtIp> and ip.DstAddr == <consoleIp>" on
    // WINDIVERT_LAYER_NETWORK_FORWARD, with courtIp validated as PUBLIC). It therefore does NOT
    // delay the Remote Play video (that flow is console -> PC on the local ICS segment and never
    // matches the filter) and it does NOT delay the release command (PC -> console, also local).
    // What it demonstrably DOES change is the console's response to the release: on the
    // 2026-08-09 corpus the meter's own geometry is untouched under delay — PHASE SAMPLE
    // anchor->stop 355-380 ms with delay vs 332-372 ms without, Release-attribution vel
    // 0.166-0.176 vs 0.170-0.210 — yet 59 of 60 graded delay-on landings sit outside the good
    // band while the delay-off landings hold settled_fill 95-97. The predictor is right and the
    // ACTUATION LEAD is wrong; MeterDelayController.h's SAFETY note predicted exactly this
    // ("the effective actuation lead can legitimately differ between delay-on and delay-off")
    // and explicitly left the keying unimplemented.
    //
    // WHY IT IS AN OPERATOR CONTROL AND NOT A FORMULA. The obvious formula (lead += appliedDelay)
    // is REFUTED by the mechanism above: nothing in the measurement path is shifted by D, so
    // there is no reason for the correction to equal D, and the corpus agrees — the single
    // good delay-on landing (2026-08-08 04:19:00, applied 210 ms, Shot Lead 385 ms,
    // settled_fill 95.75 / peak 97.47) needed roughly +118 ms, not +210. One good landing is not
    // a curve, so this ships as a value the operator calibrates from the game's own TIMING
    // banner — the same loop that produced actuationLeadMs — and the engine supplies the one
    // thing the operator cannot compute by hand: the ceiling
    // (AutomationEngine::maxMeterDelayLeadOffsetMs()).
    //
    // RANGE [-200, +400]: signed because the correction's direction is an empirical property of
    // the console under load, and bounded well inside the (base + offset) <= 800 envelope the
    // Shot Lead band already enforces. Out-of-band values are clamped on load, exactly like
    // every other numeric setting here.
    double meterDelayLeadOffsetMs = 0.0;
    static constexpr double kMeterDelayLeadOffsetMinMs = -200.0;
    static constexpr double kMeterDelayLeadOffsetMaxMs = 400.0;
    // === [ORION_USER_TIP] Tip Timing — the OTHER user-facing timing control ================
    // true once the USER explicitly chose the tip-timing (jumpshot-animation) value via the
    // Tip Timing card. The card writes the chosen value into the SAME canonical slot the
    // learner persists (learning.json learned_phase_physical_ms, canonical base-30 PHYSICAL
    // frame) and latches tip_phase_aim_frozen ON, so the manual value outranks the learner
    // exactly while locked — the precedence the freeze feature already implements — and
    // survives restart through the same restore path the learner uses (which is what makes
    // the base-20 translation impossible to get wrong twice). This flag is PRESENTATION +
    // RESET state only: it distinguishes "the user typed this" from "the user locked what
    // the learner found", and reset/unlock clears it. It deliberately does NOT gate the
    // engine — the freeze flag does — so no second precedence mechanism exists.
    bool tipTimingUserSet = false;
    // === [ORION_PRESS_ANCHOR] Press-anchored tip predictor (2026-08-08 scaffold) ============
    // The ONLY tip source not dated from the DELAYED video: the raw Square press is captured by
    // the input hook at true wall time (writes to the PS5 undelayed), so press + learned
    // press->real-tip is schedulable at ANY applied meter delay — the task #51 blocker that
    // visibleEvidenceLeadStarvedByMeterDelay() fail-closes on for every in-video source.
    //
    // TWO-PHASE SHIP. Phase A (always on): every completed shot emits one
    // "PRESS-TIP OBSERVATION:" line and, for H/M-confidence landings, folds the observation
    // into the per-shot-type windowed median/MAD persisted below. Phase B (THIS FLAG, default
    // OFF): the predictor member itself. DO NOT flip the default; the owner arms it once the
    // Aug 9-17 data is in (flip press_anchored_predictor_enabled in settings.json).
    // [ORION_PRESS_ANCHOR_BOOTSTRAP 2026-08-09] One flag-independent exception lives in the
    // engine: in the delay-starved regime (applied meter delay > 0 AND lead > usable max) the
    // member runs regardless of this flag and may fire on the measured factory priors below
    // (labelled press_anchored_boot; Go-To's unmeasured placeholder excluded), because in
    // that regime the strict gate means no delayed shot can ever fire and therefore no
    // observation can ever be collected. Delay-0 behaviour is unchanged by that exception.
    bool pressAnchoredPredictorEnabled = false;
    // The predictor ABSTAINS for a shot type until its accumulated observation weight reaches
    // this floor — a factory prior alone never schedules a fire.
    int pressAnchoredPredictorMinSamples = 30;
    // Per-shot-type learned press->real-tip (WALL-time ms, video latency + applied delay
    // stripped). Seeded with FACTORY PRIORS measured 2026-08-08 from the production log
    // (press "Physical shot epoch" ts -> phase-primary tip estimate, minus per-shot l_fixed,
    // delay 0): Standstill n=19 median 657, No Dip n=3 median 446, Right Fade n=2 median 891,
    // Left Fade n=1 median 1047; Go-To unmeasured (placeholder from its long wind-up). The
    // learner overwrites these on each accepted observation (windowed median, last ~100).
    // [ORION_PRESS_PRIOR_REFRESH 2026-08-10] Re-seeded from 128 delay-0 PRESS-TIP
    // OBSERVATIONs in the production log (medians; applied_delay_ms==0 only, so the
    // "delay stripped" definition above still holds). The 2026-08-08 seeds came from
    // n=19/3/2/1 and two of them were badly wrong:
    //
    //   shot          n   measured median   old prior   delta
    //   Standstill   75             662.7       660.0      +3   <- old seed was excellent
    //   Right Fade   22             911.2       890.0     +21
    //   Left Fade    21             870.8      1050.0    -179   <- old seed was n=1
    //   Go-To        10            1928.4      1000.0    +928   <- old seed was a guess
    //   No Dip        0                 -       450.0       -   <- no new data, left alone
    //
    // Go-To's real wind-up is nearly 2s, not 1s. Its bootstrap exclusion above is
    // DELIBERATELY LEFT IN PLACE: n=10 is thin and un-excluding it would change shot-path
    // behaviour in the delay-starved regime. Revisit once the learner has real weight.
    //
    // KEY NAMING TRAP: this map uses SPACES ("Left Fade"), while the log's shot_type field
    // uses UNDERSCORES ("Left_Fade"). Same shot types, different convention per subsystem —
    // any lookup built from log text must translate or it silently misses every entry.
    QMap<QString, double> pressAnchoredTipMs {
        {QStringLiteral("Standstill"), 663.0},
        {QStringLiteral("Left Fade"), 871.0},
        {QStringLiteral("Right Fade"), 911.0},
        {QStringLiteral("No Dip"), 450.0},
        {QStringLiteral("Go-To"), 1928.0},
    };
    // Per-type sigma (ms; windowed-MAD-derived, honest fusion weight). Absent type -> the
    // engine's wide factory sigma (60ms) so a young learner is weighted low, never trusted.
    QMap<QString, double> pressAnchoredTipSigmaMs;
    // Per-type accumulated observation WEIGHT (H=1.0, M=0.5; capped at the 100-sample window).
    // Persisted so the min-samples gate accumulates across sessions instead of resetting.
    QMap<QString, double> pressAnchoredTipN;
    bool noDipEnabled = true;   // default-ON: no-dip is the default shot (engine also forces this on)
    // No-Dip shot: a no-dip jumpshot skips the gather/dip animation, so the meter rises faster
    // and the release lands earlier. When on, the engine adds noDipLeadMs to the release lead
    // (live-tuned; default 0 = no behaviour change until dialled in). Uses the
    // "skip-gather / earliest possible release" technique.
    double noDipLeadMs = 0.0;
    // Explicit input-timed mode; unrelated to the legacy pose-only lab mode.
    bool inputTimedEnabled = false;
    // [ORION_NO_METER_V2 2026-09-14] RETIRED FROM THE TIMING MATH, tolerated on load/save so an
    // existing settings.json still round-trips. Both were vision-path quantities on a path with
    // no vision (docs/NO_METER_V2_DESIGN.md §1): the hold is now noMeterHoldMs + Δ(type) + R(mode)
    // and nothing subtracts a lead or falls back to a flat delay. Nothing reads these two.
    double inputTimedDelayMs = 500.0;
    double inputTimedLeadMs = 272.0;
    bool inputTimedRhythmEnabled = false;
    // [ORION_NO_METER_V2 2026-09-14 owner] THE blind-release reference hold, in milliseconds:
    // the Standstill press->release hold the console sees. Press-down and the release travel the
    // same pipe, so the command latency cancels and the hold IS the whole control quantity
    // (docs/NO_METER_V2_DESIGN.md §1). 650 = the measured Standstill hold on this rig
    // (651.3 ms over 238 ButtonShot vision shots, rMAD 20.8). The UI slider is 1..100 over
    // 500 + 3*(v-1) ms, so the band below is exactly the slider's reach; the 500 ms floor sits
    // ~110 ms above the highest plausible pump-fake commit threshold (owner bracket 371..389 ms),
    // which is what makes "tune it late and you get a pump fake" impossible by construction.
    // Env override ORION_NO_METER_HOLD_MS for offline sweeps.
    double noMeterHoldMs = 650.0;
    static constexpr double kNoMeterHoldMinMs = 500.0;
    static constexpr double kNoMeterHoldMaxMs = 800.0;
    // [ORION_NO_METER_FADE_TRIM 2026-09-14 owner] "standstill shots are basically perfect but
    // fades need work". ONE trim, in milliseconds, added to BOTH Left Fade and Right Fade Δ in
    // blindReleaseHold() — never to Standstill, which the owner reports as already correct, and
    // never to a type the blind law does not fade-correct. The UI is a −20..+20 slider at 3 ms a
    // step (the same step as Release timing), so the band below is exactly the slider's reach.
    // Env override ORION_NO_METER_FADE_TRIM_MS for offline sweeps.
    double noMeterFadeTrimMs = 0.0;
    static constexpr double kNoMeterFadeTrimMinMs = -60.0;
    static constexpr double kNoMeterFadeTrimMaxMs = 60.0;
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] The console's input-sampling period in ms
    // — the grid every blind hold is snapped onto (see kConsoleFrameMsDefault above for why the
    // default is the exact 1000/60 rather than the typed 16.6667). It is a property of the
    // CONSOLE, not of our capture card: captureCardFps is the rate we ASK the Elgato for and has
    // nothing to say about how often the PS5 reads the pad. Env override ORION_CONSOLE_FRAME_MS.
    double consoleFrameMs = kConsoleFrameMsDefault;
    static constexpr double kConsoleFrameMinMs = kConsoleFrameMsMin;
    static constexpr double kConsoleFrameMaxMs = kConsoleFrameMsMax;
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] Snap every blind hold to a whole number of
    // console frames. DEFAULT ON: a hold that is not a whole number of frames lands the release
    // on a frame boundary and coin-flips between frame N and N+1, which is exactly the
    // inconsistency the owner reported at 641 ms (38.46 frames) and did not report at 650
    // (39.00). OFF restores the unquantized law bit-for-bit — including the legacy 40 ms Rhythm
    // offset — so the two can be A/B'd without a rebuild.
    // Env override ORION_NO_METER_FRAME_QUANTIZE=0/1.
    bool noMeterFrameQuantize = true;
    // [ORION_NO_METER_VISION_ASSIST 2026-09-14 owner] "the no meter part is amazing but
    // inconsistent ... fades are janky". A blind hold CANNOT be consistent on a fade by
    // construction: the vision-path hold rMAD is 21 ms on Standstill but 48-50 ms on the fades,
    // against a green window worth ~16 ms of hold. The meter, when it is visible, graded 38/41
    // EXCELLENT over the same corpus.
    //
    // ON (the default) the blind hold becomes a DEADLINE rather than the decision: the vision
    // path runs inside a NO METER shot with the same fences, the same ownership proof and the
    // same Shot Lead the meter path uses, and whenever it can see the meter it owns the release.
    // OFF restores pure-blind NO METER bit-for-bit, so the owner can A/B the two.
    //
    // When the in-game meter is genuinely OFF no candidate ever appears, so this is inert and
    // every shot is the blind hold — which is the whole point of NO METER mode.
    // Env override ORION_NO_METER_VISION_ASSIST=0/1.
    bool noMeterVisionAssist = true;
    // [ORION_LATE_FIRE_TOLERANCE 2026-09-14 owner] How late the vision path may still FIRE rather
    // than abort, in ms. 0 = the old behaviour (any missed command deadline aborts).
    //
    // THE LOSS. Tonight's log, six wide-open shots in one session:
    //   TIP DEADLINE DECISION: disposition=rejected_missed source=phase tip_eta_ms=257..282
    //   command_eta_ms=-4..-20 lateness_ms=4.4 / 16.3 / 16.4 / 18.4 / 20.2 / 22.5
    //   lead_kind=validated lead_ms=274..286 ... reservation_disposition=unschedulable_lead
    //   -> Shot automation aborted: live_tip_deadline_missed
    // Every one was an owned shot with a validated lead whose reservation arrived 4-22 ms too
    // late for that lead. The abort does NOT make the shot on time — it hands the button back and
    // the owner releases manually, hundreds of ms later. A release 16 ms late is a worse shot; an
    // abort is no shot. The fail-closed rule this softens ("never disguise a guaranteed overtime
    // packet as a successful live-tip edge") is about HONESTY, and it is kept: the packet is
    // emitted, logged as fired_late with its exact lateness, counted, and excluded from every
    // learner that would otherwise read it as an on-time landing.
    //
    // 24 ms ≈ 1.5 capture frames at 60 fps, and ~1.5 green windows of hold (the measured window
    // is ~16 ms of hold on this rig) — far enough to cover the whole observed miss distribution,
    // near enough that a fired_late shot is still a plausible shot rather than a giveaway.
    // Env override ORION_LATE_FIRE_TOLERANCE_MS for offline sweeps.
    double lateFireToleranceMs = 24.0;
    static constexpr double kLateFireToleranceMinMs = 0.0;
    static constexpr double kLateFireToleranceMaxMs = 40.0;
    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] (default TRUE) The bounded closed loop from the
    // game's own TIMING banner back onto the Shot Lead. See BannerLeadTrim.h for the full
    // argument; the short version is the 2026-09-15 measurement: the SAME lead of 274 graded
    // 7 LATE / 2 EARLY / 7 EXCELLENT in a rapid-fire drill and 9 EXCELLENT / 1 EARLY / 2 LATE in
    // a 5v5 game an hour earlier, with IDENTICAL holds and freeze fills (641-654 ms, freeze peak
    // 90.7 vs 90.8) on the LATE and the EXCELLENT shots. The landing sits on the top edge of a
    // ~1-frame window and flips with the frame phase, so the right lead is context-dependent by
    // about +-10 ms and only the banner can say which context this is.
    //
    // It NEVER writes the user's slider: the trim is a separate, clamped, decaying additive term
    // (effective lead = user lead + trim) shown next to the value, and it resets the moment the
    // owner moves the slider themselves. Env override ORION_BANNER_LEAD_TRIM=0/1, ignore-don't-
    // clamp exactly like late_fire_tolerance_ms's neighbours: only an exact "0" or "1" is
    // honoured, so a typo leaves the setting alone.
    bool bannerLeadTrim = true;
    // Per-verdict step. 3 ms is a fifth of the clamp and about a fifth of a console frame pair:
    // small enough that a mis-attributed banner costs one barely-perceptible step, large enough
    // that a genuine two-verdict run closes a 1-frame miss inside a short possession run.
    double bannerTrimStepMs = 3.0;
    static constexpr double kBannerTrimStepMinMs = 0.0;
    static constexpr double kBannerTrimStepMaxMs = 10.0;
    // The clamp. +-15 ms is the measured context spread (+-10 ms) plus one step of headroom; a
    // loop that can travel further than the thing it is correcting is a second, unaccountable
    // lead control, which is exactly what this must not become.
    double bannerTrimMaxMs = 15.0;
    static constexpr double kBannerTrimMaxMinMs = 0.0;
    static constexpr double kBannerTrimMaxMaxMs = 40.0;
    // [ORION_BANNER_TRIM_HOLD 2026-09-16 owner] How many CONSECUTIVE EXCELLENT/GREEN verdicts a
    // bucket must show before the trim starts idling back toward the slider. Until today an
    // EXCELLENT decayed it 1 ms immediately; the 2026-09-16 20:43-20:45 session (22 shots) shows
    // why that was wrong -- lates pushed the Standstill trim to +9 (effective 278), two
    // EXCELLENTs walked it back down, and after a slider move seven EXCELLENTs at -3 (271) walked
    // it to 274 and straight into two EARLIES. A trim that is producing EXCELLENT is by
    // definition the right trim, so it now HOLDS; the decay exists only so a trim that is merely
    // TOLERATED cannot live forever inside one session. 12 is about four possessions at this
    // owner's cadence. Band 4..60: below four a single good run would still retire a working
    // value, above sixty the decay could never fire in a real session. Env override
    // ORION_BANNER_TRIM_HOLD_SHOTS=<int> (clamped; a non-numeric value is ignored rather than
    // guessed). The coarse relaxations are untouched: a restart still halves the trim and the
    // owner's own slider move still clears it.
    int bannerTrimHoldShots = 12;
    static constexpr int kBannerTrimHoldShotsMin = 4;
    static constexpr int kBannerTrimHoldShotsMax = 60;
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16 owner] (default TRUE) Key the trim by (shot type,
    // TEMPO) rather than by shot type alone. The 2026-09-16 21:00 session is the measurement: the
    // owner's shot tempo changed, the meter's onset moved from 500-578 ms after the press to
    // 467-480, and the game's window moved with it -- 602 ms graded EARLY while 613 and 616
    // graded LATE inside the same four shots. Those are different animation classes, and one trim
    // pushed between them is what produced the alternating +3/-3 the same session shows. Tempo is
    // read from the meter's onset alone (no new instrument, no new sample); see BannerLeadTrim.h
    // for the cut points and the offline validation behind them. Env override
    // ORION_BANNER_TRIM_TEMPO=0/1 on the same ignore-don't-clamp policy as banner_lead_trim: 0
    // collapses every key back to the bare type bucket, which is byte-identical to the
    // 2026-09-16 build including its persistence rules.
    bool bannerTrimTempoBuckets = true;
    // [ORION_BANNER_TRIM_RANGE 2026-09-17 owner] (default TRUE) Give FADES a THIRD key dimension,
    // the shot's RANGE (three | mid), so the trim is keyed "Left Fade/normal/mid".
    //
    // THE ASK, verbatim: "middy fades should perform just as standstill shots (slightly bigger
    // window); I'm fine with 50-60 % for three-point fades." The 2026-09-16 night graded 13
    // EXCELLENT / 8 LATE / 3 EARLY on fades sharing ONE bucket per tempo, with several earlies
    // "filled right before the green window" -- the same signature that forced the tempo split.
    //
    // Standstill is UNTOUCHED (it keeps "Standstill/normal"): the owner's standstills are already
    // where he wants them and splitting a bucket that produces EXCELLENT halves its evidence for
    // nothing. An UNKNOWN range -- which is what the sidecar reports whenever it cannot read the
    // nameplate "3" cell, and on today's court that is most presses -- files in the SAME
    // (type, tempo) key the 2026-09-16 build used, so an install where the signal never arrives
    // is byte-identical to today. Env ORION_BANNER_TRIM_RANGE=0/1 on the ignore-don't-clamp
    // policy: 0 collapses every key back to (type, tempo) exactly.
    bool bannerTrimRangeBuckets = true;
    // [ORION_BANNER_COVERAGE_ABSENT 2026-09-19 owner] (default TRUE) A panel with NO COVERAGE
    // CELL calibrates the trim, as an open shot does.
    //
    // WHY. On 2026-09-18 the trim gained a coverage gate: only an explicit OPEN / WIDE OPEN panel
    // may calibrate, because a contested shot has a shrunken window and its timing error is not
    // evidence about the open-shot lead. That is right, and it stays. But the game's 2-cell
    // TIMING | DISTANCE panel -- the one a drill or any no-defender context paints -- has no
    // coverage cell AT ALL, and it was 98 of the 281 graded releases across those two sessions.
    // Excluding it throws away a third of the instrument for a reason that does not apply to it:
    // there is no defender to shrink the window, which is the definition of open.
    //
    // WHAT IT IS NOT. It is not a relaxation of the gate. A panel that HAS a coverage cell is
    // judged on its word exactly as it was -- an unreadable cell, a blank word and every contest
    // word are still COVERAGE_EXCLUDED -- and the sidecar reports the layout separately from the
    // content (banner_verdict_live.py `has_coverage`), so the two cases can never be confused.
    // An older sidecar that does not send the field reads back as has_coverage=TRUE, which is
    // today's strict behaviour byte-for-byte. Env ORION_BANNER_TRIM_ABSENT_COVERAGE_OPEN=0/1 on
    // the ignore-don't-clamp policy; 0 restores the 2026-09-18 gate exactly.
    bool bannerTrimAbsentCoverageOpen = true;
    // == [ORION_BANNER_TRIM_BIAS 2026-09-19 owner] THE INTEGRATOR'S TWO LIMITS =============
    // The 2026-09-18 sessions graded OPEN/WIDE OPEN standstills 76 EXCELLENT / 18 LATE / 2 EARLY
    // -- a persistent nine-to-one late bias -- and the trim stepped 0 to 2 times per session,
    // because its streak rule needs two CONSECUTIVE agreeing verdicts and at a 79 % green rate
    // consecutive lates are rare. Each bucket therefore also keeps a sliding window of its last
    // `banner_trim_bias_window` CALIBRATING verdicts (EXCELLENT / LATE / EARLY), and a NET margin
    // of `banner_trim_bias_votes` buys exactly one step, after which the window is cleared.
    //
    // See BannerLeadTrim.h for the rule and for why a net vote needs no alternation guard. The
    // clamp, the hold, the idle decay and the slider reset are untouched.
    //
    // 12 is about four possessions at this owner's cadence. Band 2..60: below two a window cannot
    // hold a margin of three at all, above sixty it would let a lead from ten minutes ago vote on
    // this possession. Env ORION_BANNER_TRIM_BIAS_WINDOW=<int> (clamped; a non-numeric value is
    // ignored rather than guessed).
    int bannerTrimBiasWindow = 12;
    static constexpr int kBannerTrimBiasWindowMin = 2;
    static constexpr int kBannerTrimBiasWindowMax = 60;
    // 3 of 12 is the smallest margin an ALTERNATION can never reach (E,L,E,L nets 0 or +-1) and a
    // one-sided 19 % stream does. Band 0..20, and 0 is the KILL SWITCH -- it disables the
    // integrator and restores the 2026-09-17 loop byte-for-byte, which is why the floor is 0 and
    // not 1. Env ORION_BANNER_TRIM_BIAS_VOTES=<int> (clamped).
    //
    // [2026-09-19] SHIPS AT 0 (off). The integrator is built, tested and ready, but the forensics
    // that motivated it REFUTED its premise: the Sept-18 18-LATE/2-EARLY imbalance is not an aim
    // bias. A kernel fit over the clean onset band (500-535 ms, n=202) puts the LATE/EARLY
    // crossing at hold 641 ms against a population median of 646 -- the lead is already within
    // ~5 ms of optimum, and shifting it 3/6/9 ms trades 2.7/5.2/7.4 LATEs for 2.6/5.6/9.1 EARLIEs
    // (net +0.2/-0.4/-1.7 shots; the optimum is +2 ms, worth 0.1 pp). The imbalance comes from
    // shots a lead cannot fix: an ownership-proof pickup stall (9 shots, 67 % LATE) and meters
    // drawn late (13 shots, 31 % LATE). Feeding those votes to this loop would spend the whole
    // +-15 ms clamp and convert EXCELLENTs into EARLIEs. Turn it on only for a bucket measured to
    // carry a genuine one-sided bias with the mechanical classes already excluded.
    int bannerTrimBiasVotes = 0;
    static constexpr int kBannerTrimBiasVotesMin = 0;
    static constexpr int kBannerTrimBiasVotesMax = 20;
    // == [ORION_LEAD_OFFSET_BY_TYPE 2026-09-16 owner] ====================================
    // A FIXED per-shot-type addition to the Shot Lead. One slider, one number, every shot type
    // -- and the owner's own read-out of the 2026-09-16 15:00 baseline session says that is one
    // number too few:
    //
    //   "standstills basically perfect, all the fades I got were late"
    //
    // THE MEASUREMENT (same session, the reader's post-release RETRACTION ORACLE, which agreed
    // with the game's own TIMING banner on 35 of 36 graded shots that night):
    //
    //   Standstill white-top -> green-bottom gaps : 0 0 0 0 0 0.5 0.5 2.0 px   (8 of 8 green)
    //   Fade gaps                                 : 1.0 2.0 5.0 6.0 8.5 9.0 9.0 px
    //                                               (5 of 7 a miss, every one LATE by the banner)
    //
    // At the measured ~0.9 px/ms retraction rate those fade gaps are releases landing ~7-10 ms
    // LATE while the standstills land on the tip. The 2026-09-13 session measured the same thing
    // from the other side (fades releasing "8-10 ms faster" than the standstill law expects).
    //
    // WHY A NEW TERM RATHER THAN AN EXISTING ONE. The Shot Lead the owner sets applies
    // IDENTICALLY to every type on the path it rides: shotTypeOffsetMs() feeds only the modelled
    // chain and shotTypeLatencyMs() only the measured-authority path, so neither of them reaches
    // the value a tuned install actually flies. There is no per-type term on the USER lead path
    // at all -- that is the gap this closes.
    //
    // WHAT IT IS NOT. It is not a learner and it never writes the slider: it is a fixed, clamped,
    // per-bucket addition on top of whatever base the shot is already flying (user value or auto
    // seed), keyed on the SAME buckets BannerLeadTrim uses, so the constant offset and the closed
    // loop can never disagree about what a fade is. Positive = a LARGER lead = fire EARLIER,
    // which is the direction a late landing asks for.
    //
    // Defaults: 8 ms on both fades (the middle of the measured 7-10 ms lateness), 0 on the
    // Standstill and Other buckets -- so a standstill install is bit-for-bit the 2026-09-16
    // baseline. Clamped -40..40 on every route: a term that could travel further than the Shot
    // Lead's own tuning range would be a second lead control, which is exactly what this is not.
    // Env: ORION_LEAD_OFFSET_FADE_MS=<ms> sets BOTH fades (numeric, clamped; a non-numeric value
    // is ignored rather than guessed) and ORION_LEAD_OFFSET_BY_TYPE=0 zeroes all four, which
    // restores the 2026-09-16 baseline lead byte-for-byte on every type.
    double leadOffsetLeftFadeMs = 8.0;
    double leadOffsetRightFadeMs = 8.0;
    double leadOffsetStandstillMs = 0.0;
    double leadOffsetOtherMs = 0.0;
    // [ORION_LEAD_OFFSET_FADE_MID 2026-09-17 owner] The MID-RANGE fade's own offset, used
    // INSTEAD of the +-8 above whenever the live press's range reads `mid`. A three-point fade
    // and an unknown range both keep the 8.
    //
    // WHY 6 AND NOT 8. The 8 was measured on the 2026-09-16 fade population as a whole, which is
    // dominated by three-point fades. The owner's ask is that a mid-range fade "perform just as
    // standstill shots (slightly bigger window)" -- a shorter animation into a wider window, i.e.
    // less of the late-landing correction the 8 exists to remove, not none of it. 6 is that step
    // toward the Standstill bucket's 0 while staying inside the measured band; it is a starting
    // value for the closed loop (which now has its own `Left Fade/normal/mid` bucket to move),
    // not a second measurement. Clamped -40..40 on every route exactly like the other four.
    // Env ORION_LEAD_OFFSET_FADE_MID_MS=<ms> (numeric, clamped; non-numeric is ignored), and
    // ORION_LEAD_OFFSET_BY_TYPE=0 zeroes it with the rest.
    double leadOffsetFadeMidMs = 6.0;
    static constexpr double kLeadOffsetByTypeMinMs = kLeadOffsetMinMs;
    static constexpr double kLeadOffsetByTypeMaxMs = kLeadOffsetMaxMs;
    // [ORION_LEAD_AUTO_SEED 2026-09-15 owner] (default TRUE) "How will every user find their tip
    // timing lead... I'm trying to get it as plug and play as possible."
    //
    // THE MEASUREMENT THIS IS BUILT ON. The owner's rig runs a USER-SET Shot Lead of 274
    // (actuation_lead_user_set=true) while its own latency estimator reports fixed_ms ~202-211 /
    // total_ms ~210-220. The ~62 ms difference is NOT transport: it is the game-side AIM MARGIN,
    // i.e. where the release has to sit relative to the meter tip for the landing to grade
    // EXCELLENT. Transport is per-rig and the estimator measures it; the aim margin is a property
    // of 2K27's shot window and is therefore the SAME on every rig.
    //
    // WHAT WENT WRONG WITHOUT IT. A fresh install has actuation_lead_ms == 0 ("never configured")
    // and falls through to the pure measured/learned authority path, which is transport ALONE --
    // ~62 ms short of the lead that actually lands. Every shot on an untuned install is therefore
    // late, and (since 2026-09-15) the banner trim rides the user lead only, so such an install
    // never even earns the closed loop that would have found the difference.
    //
    // WHAT THIS IS NOT. It never writes actuation_lead_ms: the slider stays "Auto" and the
    // moment the owner moves it their value wins exactly as it does today. It is a stand-in for
    // the number they would otherwise have had to discover by hand, consumed through the very
    // same code path (session trim, banner trim, meter-delay offset and Rhythm delay all apply
    // identically), so an auto-seeded install is a tuned install minus the tuning session.
    //
    // Env override ORION_LEAD_AUTO_SEED=0/1, ignore-don't-clamp exactly like banner_lead_trim:
    // only an exact "0" or "1" is honoured, so a typo leaves the setting alone.
    bool leadAutoSeed = true;
    // The factory constant, shipped from the owner's rig: user lead 269 - measured ~200 = ~69.
    // [2026-09-16 owner] Re-measured by hand once the estimator's retraction artefact was fixed:
    // slider 269 against fixed_ms ~200 graded 22 EXCELLENT / 3 LATE / 2 EARLY, 12 EXCELLENT in a
    // row across standstills AND fades -- so the margin ships at 69 and the placeholder at 269.
    // Clamped 0..150 -- 0 restores the pure authority lead (today's behaviour for an untuned
    // install) and 150 is already larger than any plausible shot window, so a hand-edited file
    // cannot install an aim margin that is really a second Shot Lead.
    double aimMarginMs = 69.0;
    static constexpr double kAimMarginMinMs = 0.0;
    static constexpr double kAimMarginMaxMs = 150.0;
    // The lead used BEFORE this rig's estimator is authoritative -- the owner's own converged
    // value, which is the single best guess available about a rig nobody has measured yet.
    // Clamped to the Shot Lead's own 150..400 ms display band so the placeholder is always a
    // value the slider itself could have produced.
    double leadFactoryPlaceholderMs = 269.0;
    static constexpr double kLeadFactoryPlaceholderMinMs = 150.0;
    static constexpr double kLeadFactoryPlaceholderMaxMs = 400.0;
    // [ORION_OWNED_METER_NEVER_ABORTS 2026-09-14 owner] (default TRUE) "Remove aborts as a whole
    // so that when there's a genuine meter on the screen it always picks it up." An OWNED shot
    // with a validated lead whose command deadline has passed fires IMMEDIATELY, at ANY lateness,
    // instead of dying live_tip_deadline_missed. The tolerance above is then only a LABEL
    // boundary: a release past it is logged beyond_tolerance=1 and counted separately, so the
    // cost of the change is measurable from a returned log instead of invisible.
    //
    // The argument is a comparison, not a threshold: an abort does not recover the lateness, it
    // hands the button back and the owner releases manually hundreds of ms later. Every SAFETY
    // refusal is untouched -- wrong vision epoch, an expired or uncovered token, a confirmed or
    // in-flight submit, a calibration probe, an unowned shot, an absent lead authority -- and a
    // fired_late release still teaches no learner. Set false (or ORION_OWNED_METER_NEVER_ABORTS=0)
    // for the bounded 24 ms tolerance exactly as it shipped on 2026-09-14.
    bool ownedMeterNeverAborts = true;
    // [ORION_OWNERSHIP_PROOF_LENIENCY 2026-09-14 owner] (default TRUE) The other half of "when
    // there's a genuine meter on the screen it always picks it up". The strict ownership proof
    // restarts its whole sample count when the detector box's SHAPE drifts past the continuity
    // bound, and near the deadline that restart is the shot: the episode never re-accumulates
    // three rising frames and the press dies ownership_proof_incomplete.
    //
    // With this on, a restart caused by a GEOMETRY break of a SAME-candidate, identity-advancing,
    // still-rising episode is forgiven inside kProofLeniencyHorizonMs of the arming deadline:
    // ownership is granted on the samples in hand (never fewer than 2 consecutive post-break
    // frames) and the shot's predictor sigma is widened by 4 ms per geometry break, so a box that
    // wobbled repeatedly buys proportionally less trust.
    //
    // A break caused by a DIFFERENT candidate, a sample gap, an >8 pp cliff or a descent below
    // the episode anchor is NEVER forgiven -- those are the 2026-09-12 false-lock signatures
    // (jerseys, scoreboard and shot chart taken as meters) and the whole shape gate exists for
    // them. Set false (or ORION_OWNERSHIP_PROOF_LENIENCY=0) for today's refusal exactly.
    bool ownershipProofLeniency = true;
    // [ORION_METER_BLIND_BACKSTOP 2026-09-14 owner] (default TRUE) THE METER PATH'S BLIND
    // BACKSTOP. "aborts on wide-open shots" is a ship blocker, and the 2026-09-14 session's three
    // aborts were all the same shape: a press the meter path could never schedule (a meter the
    // reader never picked up during the shot window, a late-seen fade with ~150 ms of runway
    // against a 274 ms lead, a lost detector authority after a rollback). The blind hold
    // NO METER already computes — max(450, H_ref + Δ(type) + R(mode)) — becomes the meter path's
    // DEADLINE too, with EXACTLY the NO METER hybrid's precedence:
    //
    //   1. vision owns/arms a release  -> vision wins, the backstop is cancelled (structural:
    //      a promoted press leaves HoldState::Idle and the backstop site is unreachable)
    //   2. no candidate by the deadline -> fire blind at the deadline
    //   3. a candidate is visible and RISING -> defer, bounded by kNoMeterVisionDeferMaxMs
    //   4. an OWNED shot never falls to the backstop, so every never-aborts / late-fire /
    //      ownership-leniency rule keeps precedence over it.
    //
    // This REPLACES the retired press-anchored fallback's firing rule (see
    // pressAnchoredFallbackEnabled below), whose flaw was that it fired mid-fill on a
    // visible-but-unowned meter — the deferral is precisely the fix for that.
    //
    // Set false (or env ORION_METER_BLIND_BACKSTOP=0) to restore the abort-on-a-meterless-press
    // behaviour (`press_unanswered_no_meter`) byte-for-byte. There is deliberately no UI for it.
    bool meterBlindBackstop = true;
    // [ORION_METER_BACKSTOP_GRACE 2026-09-15 owner] (default 100 ms) HOW LONG THE BACKSTOP WAITS
    // PAST THE LAW BEFORE IT ANSWERS THE PRESS ITSELF. The METER path only — NO METER's own blind
    // hold is untouched, because there the hold IS the aim, not a backstop.
    //
    // THE MEASUREMENT (session 2026-09-15 20:48-20:51Z, 37 presses). On the 26 presses vision
    // owned, the engine's FIRST meter sample landed 509-586 ms after the press and the ownership
    // proof completed 537-620 ms after it (fades: 749-899 ms against a ~950 ms deadline). The
    // backstop's deadline for a Standstill is press + 650. Vision therefore finishes only
    // 30-55 ms inside the law, and the nine presses that fired `no_candidate_by_deadline` were
    // not meterless — they were pickups a few frames slower than that margin. The shared
    // deferral cannot rescue them either: it only opens for a candidate already RISING within
    // kBlindPreArmHorizonMs (24 ms) of the deadline, and it opened ZERO times in that session,
    // because at the deadline those presses had no accepted sample AT ALL yet.
    //
    // THE TRADE, stated as the comparison it is. A blind release is a coin flip (the banner
    // graded the session's blind releases 3 EXCELLENT / 3 EARLY; the vision-owned shots went
    // 15 EXCELLENT / 8 EARLY / 0 LATE). A press that is genuinely meterless — the far shot, the
    // beyond-half-court attempt — loses 100 ms of lateness on a release that was already blind.
    // Paying that to hand every merely-late pickup back to vision is the right side of the trade,
    // and it is the whole owner complaint ("sometimes on fades or regular standstill the meter
    // doesn't detect").
    //
    // 0 restores the 2026-09-14 deadline exactly (press + the law, byte for byte). The 400 ms
    // ceiling is a safety property, not a taste: past it the backstop would outlive the shot
    // animation it is meant to answer. Env ORION_METER_BACKSTOP_GRACE_MS for offline sweeps,
    // clamped identically, so no route — file, setting or environment — can install a margin the
    // band does not allow.
    double meterBackstopGraceMs = 100.0;
    static constexpr double kMeterBackstopGraceMinMs = 0.0;
    static constexpr double kMeterBackstopGraceMaxMs = 400.0;
    // [ORION_METER_BACKSTOP_GRACE_FADE 2026-09-15 owner] THE FADE'S OWN GRACE. Used INSTEAD of
    // the value above whenever the backstop's shot type is "Left Fade" or "Right Fade".
    //
    // THE MEASUREMENT (live drill 2026-09-15 20:36-20:39Z). A fade's meter is first SEEN
    // 750-950 ms after the press -- Right Fade 800 / 800 / 850 / 950 / 898, Left Fade 750 / 850 --
    // and its ownership proof completes another ~30-60 ms after that. The fade's own backstop
    // deadline is law ~933-950 + the 100 ms Standstill grace = ~1033-1050, which leaves a margin
    // of only 30-100 ms, and 2 of 11 fades in that drill fired the backstop. That is the same
    // failure the Standstill grace was created for, one animation later: those presses were not
    // meterless, they were pickups a few frames slower than the margin, and a blind release at
    // the law is a coin flip where vision is not.
    //
    // 220 ms is the Standstill grace scaled by the gap it has to cover: the worst measured fade
    // sight (950) plus proof (~60) is ~1010 against a law of ~933, i.e. ~77 ms of exposure, and
    // 220 leaves the same kind of headroom over that as 100 leaves over the Standstill case.
    // 0 restores the single-grace behaviour exactly (the fade then uses meter_backstop_grace_ms).
    // Env ORION_METER_BACKSTOP_GRACE_FADE_MS, clamped identically on every route.
    double meterBackstopGraceFadeMs = 220.0;
    static constexpr double kMeterBackstopGraceFadeMinMs = 0.0;
    static constexpr double kMeterBackstopGraceFadeMaxMs = 600.0;
    // [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-16 owner] THE GRACE IS FOR A LATE PICKUP, NOT FOR
    // A PRESS THE READER NEVER PROPOSED ANYTHING FOR. How long BEFORE the law the engine asks
    // "has any meter candidate at all been observed for this press yet?"; on a NO, that press's
    // deadline collapses to press + the law — no grace, and no fade grace either.
    //
    // THE LOSS (owner, live): "next press does nothing, square button is stuck, you can't shoot,
    // so I have to manually tempo with my stick to release the ball". Across ~300 presses, 7 real
    // holds (400-980 ms, ordinary spacing, the Square-down edge accepted by the fork within
    // 0.5 ms) produced NO meter candidate AT ALL for the whole hold — the reader never proposed
    // anything (`PICKUP first_sight_fill=-1`) — and the owner let go by hand before the blind
    // deadline (Standstill 650 + 100 = 750; Right Fade ~950 + 220 = 1170). The grace exists for a
    // pickup that is merely LATE (a meter first seen 550-630 ms after the press must still reach
    // vision). On a press where nothing has been seen at all by law - this probe, it buys nothing
    // and prolongs a hold the player already feels as a stuck button.
    //
    // THE COLLAPSE IS UNDONE the moment a candidate appears. The question is asked on every tick
    // from the probe instant onward, so a meter that surfaces between the probe and the law puts
    // the press straight back on the normal grace/deferral rules — which is exactly the 550-630 ms
    // pickup band the grace was measured for. What it costs is a pickup first seen AFTER the law:
    // that press is now answered at the law instead of waiting the grace out.
    //
    // 0 = the feature OFF (the 2026-09-15 deadline, byte for byte). The 300 ms ceiling is a
    // safety property: a probe wider than that would ask its question before a normal meter has
    // had any chance to be drawn (the owner's own vision pickups land 509-586 ms into a 650 ms
    // law). Env ORION_METER_BACKSTOP_NEVER_SEEN_PROBE_MS, clamped identically on every route.
    //
    // == [ORION_METER_BACKSTOP_NEVER_SEEN 2026-09-17 owner] SHIPS AT 0: THE COLLAPSE IS OFF ====
    //
    // The 100 ms probe was measured against presses that never produced a meter AT ALL. The
    // 2026-09-17 press-window dump (D:\NexusVision\nometer_check\) shows the other population it
    // also catches, and that one it gets WRONG: the session's two Standstill blind fires were
    // LATE GATHERS, not dead presses. The game drew the meter track at press+614 and press+617 ms
    // (a normal gather on this rig is ~407) and the collapsed deadline fired at +653 -- 19 and
    // 39 ms BEFORE any fill existed to time against. With the probe at 0 the Standstill grace of
    // 100 ms stands, the deadline is 750, and ep35 (first fill read at +725) is caught outright
    // while ep50 (+756) is marginal.
    //
    // THE TRADE, stated plainly. At 0 a press that really never gathers waits ~100 ms longer
    // before the blind fire -- and it was never going to get a meter at any deadline, so all it
    // costs is feel. At 100 a press that gathers LATE is fired blind while its own meter is on
    // its way, which costs the shot. A blind fire on a real late gather is the more expensive
    // error, so the default is 0 and the mechanism stays in the build for the owner to re-arm
    // (settings key, or ORION_METER_BACKSTOP_NEVER_SEEN_PROBE_MS) if a stuck-Square session
    // returns.
    // ==========================================================================================
    double meterBackstopNeverSeenProbeMs = 0.0;
    static constexpr double kMeterBackstopNeverSeenProbeMinMs = 0.0;
    static constexpr double kMeterBackstopNeverSeenProbeMaxMs = 300.0;
    // [ORION_METER_BACKSTOP_NEVER_SEEN_FADE 2026-09-16 owner] THE FADE'S OWN PROBE, and it ships
    // at 0 -- FADES ARE EXCLUDED FROM THE COLLAPSE. Used INSTEAD of the value above whenever the
    // backstop's (re-typed) shot type is "Left Fade" or "Right Fade".
    //
    // THE MEASUREMENT that forces the split. The slow-fade tempo class has its meter first SEEN
    // 1000-1051 ms after the press -- session 14, epochs 16 and 17: first sight 1049 / 1051, holds
    // 1180 / 1190, both graded EXCELLENT by the game's own banner, and both of them VISION shots
    // that only existed because the 220 ms fade grace kept the press alive. That first sight is
    // AFTER the fade law (~933-954), so a collapse would have answered those two blind at the law
    // and turned an EXCELLENT into an EARLY. The Standstill case has no such tail: its pickups
    // land 434-630 ms into a 650 ms law, i.e. always before it, which is why the collapse is safe
    // there and stays on by default.
    //
    // 0 IS EXCLUSION, NOT A FALLBACK. This is deliberately the opposite convention to
    // meterBackstopGraceFadeMs (where 0 means "use the Standstill number"): there the fallback
    // preserved a margin, here it would install exactly the behaviour that costs the two measured
    // fades their shots. A fade is collapsed only when this key is set above 0 on purpose.
    // The 400 ms ceiling matches the fade grace's own scale. Env
    // ORION_METER_BACKSTOP_NEVER_SEEN_PROBE_FADE_MS, clamped identically on every route.
    double meterBackstopNeverSeenProbeFadeMs = 0.0;
    static constexpr double kMeterBackstopNeverSeenProbeFadeMinMs = 0.0;
    static constexpr double kMeterBackstopNeverSeenProbeFadeMaxMs = 400.0;
    // === [ORION_VISION_HOLD_BAND 2026-09-15 owner] THE VISION RELEASE'S HOLD BAND ============
    //
    // THE MEASUREMENT, and it is not about the meter. Across two live sessions and one
    // frame-dump forensics pass the game's LATE verdicts line up with the HOLD (press->release),
    // not with anything visible in the meter:
    //
    //   session 23:51-23:54Z, 42 presses, banner 22 EXCELLENT / 15 LATE / 3 EARLY, lead 274-299
    //     Standstill EXCELLENT holds 622-700 (median ~648)
    //     Standstill LATE      holds 705, 719, 719, 726, 727, 733, 775, 1983
    //     Standstill EARLY     holds 608, 632
    //   drill 20:36-20:39Z, fades
    //     EXCELLENT holds 945-987   vs   LATE holds 1018-1099   (fade law 933-950)
    //
    // and the frame forensics say why: on a late shot the meter was DRAWN later after the press
    // (press->meter-zero 552 ms vs 508 ms) while the game's own window did NOT move with it. The
    // game grades against the press and the animation; the drawn meter's onset lags it by a
    // variable 0-130 ms, and vision -- which is tracking the drawn meter -- releases late by
    // exactly that lag.
    //
    // THE LAW ALREADY EXISTS. blindReleaseHold(type, rhythm) is the press-anchored truth for this
    // rig, per shot type: Standstill 650 ms (the owner's own measured 647 snapped to 39 console
    // frames), fades 933-950 via the learned/table delta. The owner's NO METER standstills were
    // "basically perfect" flying on exactly that number. So this is not a new estimator; it is a
    // BAND around a law we already trust, inside which vision keeps every millisecond of its
    // advantage and outside which it is refused.
    //
    // WHAT IT IS NOT. It does not move the prediction: decision.tipAbsMs, every learner input and
    // every sigma are untouched, so the predictors keep seeing what vision actually believed. It
    // clamps ONE thing -- the instant the release is issued at -- into
    // [press + law - band, press + law + band]. It is not the METER BACKSTOP either: that answers
    // a press with NO candidate at all and is unaffected.
    //
    // 40 ms for a Standstill is the measured separation: EXCELLENT tops out at 700 (law + 50)
    // with the median at 648, and every LATE but the coin-flips sat at 705+ (law + 55). A band of
    // 40 keeps the whole EXCELLENT mass and refuses the late tail. 0 = OFF (kill switch), and the
    // 200 ms ceiling is a safety property: past it the band cannot bind on any real hold.
    // Env ORION_VISION_HOLD_BAND_MS, clamped identically on every route.
    //
    // [SHIP CONFIG 2026-09-17] DEFAULT 0 = OFF. REFUTED LIVE on 2026-09-16 21:00-21:01: the
    // game's window had moved with a faster animation tempo (first sight 434-450 ms, natural
    // holds 583-602 against a window of ~608 +- 5), and the band clamped 4 of 5 presses and was
    // WRONG on all four (746->696 EARLY, 583->616 LATE, 596->613 LATE, 602 untouched EARLY). The
    // band is anchored on a law that does not move with tempo, so it fights the per-tempo trim
    // instead of helping it. The mechanism, its tests and its env override are KEPT so it can be
    // re-armed for an A/B the moment a tempo-aware law exists -- only the shipped default moved.
    double visionHoldBandMs = 0.0;
    static constexpr double kVisionHoldBandMinMs = 0.0;
    static constexpr double kVisionHoldBandMaxMs = 200.0;
    // [ORION_VISION_HOLD_BAND 2026-09-15 owner] THE FADE'S OWN, WIDER BAND. Used INSTEAD of the
    // value above whenever the press's shot type is "Left Fade" or "Right Fade". A fade's gather
    // carries legitimate variance a standing shot does not: the drill's EXCELLENT fade holds
    // spread 945-987 (42 ms) against a law of 933-950, i.e. up to law + 54, while its LATE holds
    // began at 1018 (law + 68). 60 ms admits the whole measured EXCELLENT spread and still
    // refuses the late tail. 0 = OFF for fades only (a fade then flies unclamped, NOT on the
    // Standstill band -- a narrower band on a wider-variance type would refuse good shots).
    // Env ORION_VISION_HOLD_BAND_FADE_MS, clamped identically on every route.
    //
    // [SHIP CONFIG 2026-09-17] DEFAULT 0 = OFF for fades too, for the reason above: the band
    // was refuted on the 09-16 21:00 session and the ship build flies unclamped on every type.
    double visionHoldBandFadeMs = 0.0;
    static constexpr double kVisionHoldBandFadeMinMs = 0.0;
    static constexpr double kVisionHoldBandFadeMaxMs = 300.0;
    // [ORION_PRESS_ANCHORED_FALLBACK 2026-09-14 — RETIRED 2026-09-14 by ORION_METER_BLIND_BACKSTOP]
    // The old meter-path fallback fired at press + the blind hold with NO deferral, so a meter
    // that was visible but not yet owned was answered mid-fill — which is why it shipped OFF.
    // Its firing rule, its env override and its block-reason vocabulary are DELETED; the key is
    // retained here only so an existing settings.json carrying it still loads and round-trips
    // unchanged. Nothing reads this field.
    bool pressAnchoredFallbackEnabled = false;
    bool noMeterEnabled = false;
    QString noMeterReleasePoint = QStringLiteral("Push");
    double noMeterBaseOffsetMs = 83.0;
    double noMeterDecodeCompMs = 7.5;
    double noMeterConfidenceGate = 0.70;
    double noMeterPushReleaseWindowMs = 200.0;
    // Player handedness selects the shooting wrist for pose timing (Right→right wrist).
    QString noMeterHandedness = QStringLiteral("Right");
    QMap<QString, double> noMeterShotTypeOffsets;
    // Skele-mode overlay: draw the live pose skeleton + lock box on the capture (debug the lock).
    bool showSkeleton = true;
    // Customer-facing live ETA/HOLD telemetry. Presentation only: values remain
    // truth-gated by the native per-shot arm token and freshness checks.
    bool showLiveMeterMetrics = true;
    // NOTE: the meter-side telemetry HUD (FILL / TIP / FIRE beside the detected
    // meter) deliberately has NO setting. It is always on and gated only by a
    // live bot-owned shot, so a persisted flag here would be a lie.
    // Hold-square autogreen: per-shot-type release offsets (ms). The shot type
    // is auto-classified from the live controller input at release time (L2 /
    // stick signatures); the detected type's offset is added to release lead.
    // activeShotType is only a fallback before the first shot is classified.
    QString activeShotType = QStringLiteral("Standstill");
    QMap<QString, double> shotTypeOffsets {
        {QStringLiteral("Standstill"), 0.0},
        {QStringLiteral("Fade"), 0.0},
        {QStringLiteral("Left Fade"), 0.0},
        {QStringLiteral("Right Fade"), 0.0},
        {QStringLiteral("Go-To"), 0.0},
        {QStringLiteral("No Dip"), 0.0},
        {QStringLiteral("Post Fade"), 0.0},
        {QStringLiteral("Post Hook"), 0.0},
        {QStringLiteral("Post Go-To"), 0.0},
    };
    // [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in flag (field + settings
    // key) is DELETED, not defaulted:
    // the owner retired the passive-sniffing legacy gate and everything network-side ships ON.
    // The bridge/DLL link predicate is OrionAppController::packetBridgeLinkConfigured():
    // networkEnabled || meterDelayEnabled, both default true. Legacy settings.json files that
    // still carry the old opt-in key are deliberately ignored on load.
    // Still NOT timing authority: the bridge is ServiceIdentityTrust::Unverified and its samples
    // are compiled out of the timing feed in production builds, so bot timing never depends on it.
    // MUST degrade gracefully: the kernel driver needs elevation, and some AV/EDR flags WinDivert.
    // ensurePacketBridgeRunning() falls back to a local process and then logs; nothing on the shot
    // path may block on it.
    bool networkEnabled = true;
    // 2026-07-25 SHIPPING RECONCILIATION: true -> false. Deliberately NOT matched to the rig's
    // cuda_enabled=true, because the flag is inert on the shipped path and true is a false promise:
    // its ONLY readers are meter_detector.py's _CudaMaskBackend (the RETIRED legacy detector chain —
    // remote_play_orchestrator.py:677-685 refuses to build it) and remote_play_cv.py's RemotePlayCV
    // (not constructed by the orchestrator, which imports only RemotePlayCVConfig/GreenWindowAnalyzer).
    // The shipped readers (SimpleMeterReader / CompressedMeterReader) never look at it, no QML
    // surfaces it, and the packaged bundle ships neither torch nor a CUDA-built OpenCV wheel, so
    // _CudaMaskBackend's opportunistic probe would fall back to CPU anyway. false = the honest value.
    bool cudaEnabled = false;
    double latencyCompensationMs = 45.0;
    double releaseThresholdPct = 96.0;
    double fixedHoldMs = 650.0;
    double earlyLateOffsetMs = 0.0;
    double tempoWaitMs = 0.0;
    // [ORION_RHYTHM_FLICK_DELAY 2026-09-14] Rhythm/Tempo flick trim, in ms.
    //
    // With Rhythm on, the release is a right-stick FLICK (ShotMode::TempoSquare) instead of a
    // Square tap, and the stick gesture's own travel makes the effective release land slightly
    // differently from the tap path. Customers who read EARLY/LATE on the game's own TIMING
    // banner while Rhythm is on need a trim that moves only the flick.
    //
    // POSITIVE = the flick fires LATER by this many ms; NEGATIVE = earlier. The engine applies it
    // by SUBTRACTING it from the actuation lead (fire = tip - lead, so a smaller lead fires
    // later), and only for a shot whose release will actually be the stick flick — see
    // AutomationEngine::rhythmFlickReleasePending().
    //
    // It is a TRIM ON TOP of the Shot Lead, never a lead of its own: the zero-authority path in
    // measuredLeadForActuationMs() still returns 0, so this can no more manufacture a lead than
    // the meter-delay offset can. Band -50..+50; 0 (the default) is byte-identical to the
    // pre-2026-09-14 behaviour.
    double rhythmFlickDelayMs = 0.0;
    static constexpr double kRhythmFlickDelayMinMs = -50.0;
    static constexpr double kRhythmFlickDelayMaxMs = 50.0;
    // Built-in, pre-tuned tempo timing (no user sliders by design — see MeterConfigPanel).
    // Reference: Tempo 66ms (flick), Rhythm Timer 92ms (gather). Timer 134ms is the meter
    // release clock, which Orion owns per-type/self-calibrating (not a tempo constant).
    double tempoFlickHoldMs = 66.0;        // "Tempo" flick
    double tempoMinStickHoldMs = 92.0;     // "Rhythm Timer" (gather hold)
    double tempoFallbackTimeoutMs = 650.0;
    double minimumHoldMs = 100.0;
    double maximumHoldMs = 1000.0;
    int detectionConfidencePercent = 50;
    int stableFrames = 3;
    // Auto meter-COLOUR: when on, the detector scans + locks the meter colour itself
    // (prevents the wrong-meter_color footgun). Consumed by meter_detector.py.
    bool autoMeterColor = false;
    // Trained TEMPLATE ANCHOR (meter LOCATION) written by the in-launcher Train
    // button and consumed by meter_detector.py's _TemplateAnchor. Stored verbatim
    // (like profiles_) so the nested shape the Python loader expects survives a
    // launcher save: { enabled, template_path, search_band{x0,y0,x1,y1},
    // meter_offset{dx,dy,w,h}, match_confidence_min, ref_wh[W,H] }.
    QJsonObject meterTemplateAnchor;
    // Optional trained green-window HSV (OpenCV H 0-179) — [h,s,v] low/high, or empty.
    QJsonArray meterTrainedGreenHsvLow;
    QJsonArray meterTrainedGreenHsvHigh;
};

// Pure autogreen-sidecar policy: whether the sidecar should auto-launch the Chiaki Remote Play
// client. In live-capture PREVIEW mode the Elgato HD60 X is the video source and Chiaki must NOT
// be launched (no Remote Play, no input hook), so the capture-card feed can show in the dashboard
// BEFORE the user presses Connect — and Connect can later hand the card over to the full pipeline.
// A normal (non-preview) session launches Chiaki. Header-only + Qt-free so it is unit-testable
// headlessly (see AutomationEngineTests); lives here because AppConfig.h is the shared Qt-Core
// header both RemotePlaySession and the Core-only test target can see.
[[nodiscard]] inline bool sidecarShouldAutoLaunchClient(bool previewMode) noexcept
{
    return !previewMode;
}

// [ORION_NO_METER_V2 2026-09-14] One shot type's own press->release HOLD as the VISION path
// measured it — the blind path's only ground truth (docs/NO_METER_V2_DESIGN.md §3, "Learn the
// hold, not the tip"). Stored as a robust running median plus the evidence count behind it, so
// the blind law can ask "do I have >= 8 of this owner's own holds?" before preferring it to the
// shipped table. Persisted in learning.json under no_meter_hold_by_type.
struct NoMeterHoldRecord {
    double medianMs = 0.0;
    int n = 0;
};

struct LearningData {
    int version = 0;
    double emaFillPerFrame = 0.0;
    double emaGreenRatio = 0.0;
    double biasPct = 0.0;
    double openNudgePct = 0.0;
    double lightNudgePct = 0.0;
    double moderateNudgePct = 0.0;
    double heavyNudgePct = 0.0;
    double smotheredNudgePct = 0.0;
    // [ORION_PHASE_COLD_START] The learned PHYSICAL animation constant (ms from the anchor
    // crossing to the meter stopping). Persisted so the cold start happens ONCE for a given
    // jumpshot rather than once per launch: the constant is a property of the equipped animation,
    // and re-measuring it from the seed every session means every session opens with the same
    // rough patch the owner hit when testing on a build with different jumpshots.
    // -1 = never measured, so the seed carries the session exactly as before.
    double learnedPhasePhysicalMs = -1.0;
    // [ORION_AIM_FREEZE] What the phase instrument last MEASURED (full-window median, canonical
    // base-30 physical ms), regardless of what the aim consumes. Distinct from
    // learnedPhasePhysicalMs, which the Tip Timing card overwrites with the MANUAL value while
    // tip_phase_aim_frozen holds -- that overwrite is the card's persistence contract, but it
    // also meant a frozen session discarded its own measurement at the last instruction
    // (2026-08-08: manual 240 persisted every session while the live median measured 310.2,
    // so the 70 ms disagreement was invisible across restarts). This slot keeps the
    // measurement; nothing on the decision path consumes it -- restore uses it only to warn.
    // -1 = never measured.
    double measuredPhasePhysicalMs = -1.0;
    // [ORION_SESSION_LEAD_PROBE] The engine-units anchor->freeze median measured the first time
    // the current Shot Lead ran a full probe window, and the lead it was captured under. A
    // different lead invalidates the pair (the engine recaptures). -1 = never captured.
    double leadReferencePhysicalMs = -1.0;
    double leadReferenceLeadMs = -1.0;
    QMap<QString, double> shotTypeLearnedOffsetMs;
    // Per-shot-type learned hold-start->release time (ms) — the deterministic meter
    // animation clock. Lets the bot feed-forward time a shot whose green window is a
    // contested/invisible sliver. Each shot type (animation) gets its own value.
    QMap<QString, double> shotTypeFeedforwardMs;
    // Per-shot-type learned firstMeterSeen->release time (ms) — the meter-appear-anchored
    // clock (the alternative anchor to shotTypeFeedforwardMs; see RemapConfig::feedforwardAnchor).
    QMap<QString, double> shotTypeMeterToReleaseMs;
    // Per-shot-type calibration phase (0 = Acquire, 1 = Lock). Persisted so a dialed-in type
    // restarts LOCKED (micro-trim only) instead of re-hunting a converged baseline.
    QMap<QString, int> shotTypeCalPhase;
    // Network-offset baseline (ms) captured when the type LOCKED — the delta-compensation
    // reference: per shot the live offset is clamped to within a bounded delta of this, so
    // a locked clock follows network drift without relearning.
    QMap<QString, double> shotTypeRttBaselineMs;
    // Per-shot-type meter velocity prior (%/ms), EMA of vision-timed releases. Live
    // velocity estimates are sanity-banded against it (rejects detector blips).
    QMap<QString, double> shotTypeVelocityPriorPctMs;
    // HYBRID GLOBAL PHASE CLOCK self-learned globals (autonomous_vision path). All three are learned
    // SLOWLY and ONLY from clean, vision-timed, non-fallback releases.
    //   globalAppearToTipMs  = canonical firstMeterSeen->tip duration, shared across ALL shot types
    //                          (the type-invariant rise; 0 = unseeded -> seeds from the first clean shot).
    //   globalHoldToReleaseMs = hold-start->release fallback clock for an invisible/contested meter.
    //   learnedLatencyMs     = ONE global latency+center-bias correction (subtracted from the lead);
    //                          adapts slowly, bounded, divergence-guarded.
    double globalAppearToTipMs = 0.0;
    double globalHoldToReleaseMs = 0.0;
    double learnedLatencyMs = 0.0;
    // [ORION_PROBE] press->meter-appear game constant (D_spawn) for the warmup pump-fake
    // probes: probe label = (press->appear) - this. 0 = uncalibrated -> latency_estimator.py
    // _close_probe drops EVERY probe as probe_uncalibrated, so no probe latency label can ever
    // form. A clean install has no learning.json (and learning.json is release-forbidden:
    // tools/release_filter_policy.py), so this compiled default is the ONLY way the constant
    // reaches a customer — shipping it as a data file would mean shipping a learning.json whose
    // explicit 0 in any other key context could clobber compiled defaults, the exact failure
    // this fixes. It is a GAME-side constant, not a rig property (the same argument
    // probePressMs/probeGapMs/probeCallLeadMs make above): measured twice independently on the
    // reference rig — 178.2 (2026-08-06, 16-probe run joined against the passive posterior) and
    // 180.9 (2026-08-05 session, tools/timing/calibrate_probe_spawn.py) — agreeing within the
    // probe label sigma (8.0ms). A per-user learning.json value still overrides this on load.
    double probeSpawnOffsetMs = 178.2;
    // [ORION_FUSED_FIRE] per-bucket appear->TIP anchor clocks (the fused posterior's Source-1
    // prior), EMA-taught by the sidecar's post-hoc full-shot registration labels — NEVER by
    // the frozen self-grade. Empty bucket -> the engine's fusedAppearToTipSeedMs (380, M2).
    QMap<QString, double> shotTypeAppearToTipMs;
    // [ORION_MEASURED_LEAD] one-shot re-baseline latch: true once the per-type/global clocks
    // have been shifted by (measured lead - old learned lead) at the first measured-lead
    // engagement. Without this the EMA'd clocks (which absorbed the old ~6ms lead) would fire
    // ~69ms early for the 15-30 shots the EMAs need to re-converge.
    bool leadRebaselined = false;
    double globalRiseVelocityPctMs = 0.226;   // type-invariant rise rate (the shadow model's vg)
    // Per-shot-type latency residual (autonomous vision). Persists across restarts so
    // type-specific corrections survive. 0 = no residual (uses global only).
    QMap<QString, double> shotTypeLatencyMs;
    // [ORION_NO_METER_V2 2026-09-14] Per-shot-type press->release HOLD measured on the VISION
    // path's own graded landings. The blind release law reads it as a DIFFERENCE
    // (median(type) - median("Standstill")), which is why every constant latency cancels out of
    // it and why it is usable the moment both types carry >= 8 samples. Empty = use the shipped
    // Δ table.
    QMap<QString, NoMeterHoldRecord> noMeterHoldByType;
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] The banner loop's per-shot-type additive trim, in ms.
    // Keyed by the BannerLeadTrim bucket ("Standstill" / "Left Fade" / "Right Fade" / "Other").
    // Decayed 50 % at every app start (contexts change between sessions) and schema-guarded on
    // load exactly as no_meter_hold_by_type is: a non-finite or out-of-band entry is dropped, so
    // a corrupt file degrades to "no trim" and never to a lead offset no setting could produce.
    QMap<QString, double> bannerLeadTrimByType;
};

// [ORION_USER_LEAD] May this install's MEASURED end-to-end lead be written into the user-facing
// Shot Lead? A named predicate rather than an inline condition because it IS the product promise
// ("once you move it, your value wins and is never silently overwritten") and therefore has to be
// directly testable.
//
// Two independent locks, both required:
//   * actuationLeadUserSet — the user has moved the control at some point. Permanent until they
//     explicitly reset. This is the promise itself.
//   * actuationLeadMs > 0 — a value is already installed. This makes the seed a ONE-TIME event
//     even before anyone touches the slider: the number under the control must not shift while
//     the user is reading the game's banner and drawing conclusions about the value they see.
inline bool actuationLeadAcceptsMeasuredSeed(const AppConfigData& data) noexcept
{
    return !data.actuationLeadUserSet && !(data.actuationLeadMs > 0.0);
}

// === [ORION_LEAD_BY_SOURCE 2026-09-14] per-video-route Shot Lead: the whole mechanism ======
//
// Free functions rather than methods so the switch is a PURE transform on AppConfigData that
// the test suite can drive without constructing the controller (which spawns the packet bridge
// and taskkills chiaki images on teardown — see the MeterDelaySettings test header).

// The two canonical route keys, normalised exactly the way AppConfig::loadSettingsObject
// normalises video_source, so the stash key and the live videoSource can never drift apart.
inline bool isXboxRemotePlay(const AppConfigData& data)
{
    return data.remotePlayConsole == QLatin1String("Xbox");
}

inline QString actuationLeadSourceKey(const QString& videoSource)
{
    const QString v = videoSource.trimmed().toLower();
    return (v == QLatin1String("capture_card") || v == QLatin1String("capture card")
            || v == QLatin1String("capturecard"))
        ? QStringLiteral("capture_card")
        : QStringLiteral("decoder");
}

inline QString actuationLeadRouteKey(const AppConfigData& data)
{
    return isXboxRemotePlay(data) ? QStringLiteral("xbox_wgc")
                                  : actuationLeadSourceKey(data.videoSource);
}

// The accepted set is {0} u [min, max]: 0 = not configured, an in-between value is clamped UP
// into the band. Identical policy to the actuation_lead_ms load rule, factored out so the
// stash cannot acquire a second, subtly different one.
inline double cleanActuationLeadMs(double raw) noexcept
{
    if (!std::isfinite(raw)) {
        return 0.0;
    }
    double v = std::clamp(raw, 0.0, AppConfigData::kActuationLeadMaxMs);
    if (v > 0.0 && v < AppConfigData::kActuationLeadMinMs) {
        v = AppConfigData::kActuationLeadMinMs;
    }
    return v;
}

// "Configured" is what an entry's PRESENCE means. userSet is included for completeness; in
// practice it can only be true alongside a banded lead (setActuationLeadMs clamps to >= min).
inline bool actuationLeadIsConfigured(double leadMs, bool userSet) noexcept
{
    return leadMs > 0.0 || userSet;
}

// Copy the LIVE pair into the stash under the CURRENT route. A not-configured live pair REMOVES
// the entry, which is what keeps "absent == not configured" a bijection instead of two ways of
// spelling the same state. Called from AppConfig::save(), so every write path to the live lead
// (the setter, the nudge, the lead-calibration steps, the measured seed, the reset, a Venice
// profile import) mirrors without having to remember to.
inline void mirrorActuationLeadIntoSourceStash(AppConfigData& data)
{
    const QString key = actuationLeadRouteKey(data);
    if (!actuationLeadIsConfigured(data.actuationLeadMs, data.actuationLeadUserSet)) {
        data.actuationLeadBySourceMs.remove(key);
        data.actuationLeadUserSetBySource.remove(key);
        return;
    }
    data.actuationLeadBySourceMs.insert(key, data.actuationLeadMs);
    data.actuationLeadUserSetBySource.insert(key, data.actuationLeadUserSet);
}

// What one route switch did, so the caller can log it without re-deriving anything.
struct ActuationLeadSourceSwitch
{
    QString fromSource;
    QString toSource;
    double previousLeadMs = 0.0;     // the live lead that belonged to fromSource
    bool previousUserSet = false;
    double restoredLeadMs = 0.0;     // the live lead now, i.e. toSource's stash (0 = none)
    bool restoredUserSet = false;
    bool changed = false;            // false when the route did not actually change
};

// Console changes must not inherit PS5 calibration or overwrite its saved lead.
inline void switchRemotePlayConsole(AppConfigData& data, const QString& console)
{
    if (data.remotePlayConsole == console)
        return;
    mirrorActuationLeadIntoSourceStash(data);
    data.remotePlayConsole = console;
    const QString key = actuationLeadRouteKey(data);
    data.actuationLeadMs = cleanActuationLeadMs(data.actuationLeadBySourceMs.value(key, 0.0));
    data.actuationLeadUserSet = data.actuationLeadUserSetBySource.value(key, false);
    mirrorActuationLeadIntoSourceStash(data);
}

// THE switch: stash the live pair under the route being left, then restore the route being
// entered. An unconfigured destination restores (0, false) — the pre-existing not-configured
// behaviour, deliberately NOT the other route's number.
inline ActuationLeadSourceSwitch switchActuationLeadVideoSource(AppConfigData& data,
                                                                const QString& nextSource)
{
    ActuationLeadSourceSwitch result;
    result.fromSource = actuationLeadSourceKey(data.videoSource);
    result.toSource = actuationLeadSourceKey(nextSource);
    result.previousLeadMs = data.actuationLeadMs;
    result.previousUserSet = data.actuationLeadUserSet;
    // Mirror FIRST and unconditionally: whatever happens next, the live pair belongs to the
    // route we are standing on right now.
    mirrorActuationLeadIntoSourceStash(data);
    if (result.fromSource == result.toSource) {
        result.restoredLeadMs = data.actuationLeadMs;
        result.restoredUserSet = data.actuationLeadUserSet;
        data.videoSource = result.toSource;
        return result;   // changed stays false; the live lead is untouched
    }
    double lead = cleanActuationLeadMs(data.actuationLeadBySourceMs.value(result.toSource, 0.0));
    bool userSet = data.actuationLeadUserSetBySource.value(result.toSource, false);
    if (!actuationLeadIsConfigured(lead, userSet)) {
        lead = 0.0;
        userSet = false;
    }
    data.videoSource = result.toSource;
    data.actuationLeadMs = lead;
    data.actuationLeadUserSet = userSet;
    mirrorActuationLeadIntoSourceStash(data);   // holds the invariant on the NEW route too
    result.restoredLeadMs = lead;
    result.restoredUserSet = userSet;
    result.changed = true;
    return result;
}

// Log/UI wording for one side of a switch.
inline QString actuationLeadDescription(double leadMs, bool userSet)
{
    if (!(leadMs > 0.0)) {
        return QStringLiteral("not configured");
    }
    return QStringLiteral("%1 ms (%2)")
        .arg(leadMs, 0, 'f', 0)
        .arg(userSet ? QStringLiteral("user") : QStringLiteral("measured"));
}

// === [ORION_USER_TIP] Tip Timing frame conversions =====================================
//
// THE TWO FRAMES, and why exactly one identity converts between them. The Tip Timing card
// presents and edits the EFFECTIVE aim — the number the decision path actually consumes:
//
//   effective = activePhysicalPrior + (tipPhaseConstantMs - tipPhaseSeedPhysicalMs)
//   activePhysicalPrior = canonicalPhysical + phasePriorShift   (base-20: +58.3, else +0)
//
// while learning.json stores the prior CANONICALLY (base-30 PHYSICAL frame), so a regime
// flip can never corrupt it (see AutomationEngine::phasePriorShiftMs). Because the engine's
// active seed is defined as (defaultSeed + shift) and the active constant as
// (defaultConstant + shift), the aim offset (constant - seed) is shift-invariant and the
// two-step conversion collapses to ONE subtraction with no separate shift term:
//
//   canonical = effective - activeConstant + defaultSeed
//   effective = canonical + activeConstant - defaultSeed
//
// where activeConstant is the LIVE engine value (RemapConfig::tipPhaseConstantMs after
// applyConfig — 393.0 base-30, 451.3 base-20) and defaultSeed is the COMPILED default
// (RemapConfig{}.tipPhaseSeedPhysicalMs = 319.0), never the live seed. Worked example, the
// owner's rig 2026-08-06 (base-20): effective 435.5 -> canonical 435.5 - 451.3 + 319.0 =
// 303.2, which is exactly the learning.json value in flight that night.
//
// TRAPS THIS ENCODES (do not "simplify" either):
//   * Using the LIVE seed instead of the compiled default double-counts the base-20 shift
//     (the live seed already carries +58.3) and silently re-aims by 58.3ms.
//   * Quoting the shipped constant as the aim is the §9.5 mistake — the effective value is
//     constant-plus-learner, and only this pair of functions round-trips it exactly.
//
// Pure double arithmetic so the identity is unit-testable headlessly (AutomationEngineTests)
// against the engine's own restore path — the controller and the tests MUST share these
// functions rather than re-deriving the arithmetic in two places.
[[nodiscard]] inline double tipTimingCanonicalFromEffective(
    double effectiveMs, double activeTipConstantMs, double defaultSeedPhysicalMs) noexcept
{
    return effectiveMs - activeTipConstantMs + defaultSeedPhysicalMs;
}

[[nodiscard]] inline double tipTimingEffectiveFromCanonical(
    double canonicalMs, double activeTipConstantMs, double defaultSeedPhysicalMs) noexcept
{
    return canonicalMs + activeTipConstantMs - defaultSeedPhysicalMs;
}

class ORION_COMMON_API AppConfig final : public QObject {
    Q_OBJECT
public:
    explicit AppConfig(QString rootDir, QObject* parent = nullptr);

    [[nodiscard]] const AppConfigData& data() const noexcept { return data_; }
    [[nodiscard]] const LearningData& learning() const noexcept { return learning_; }
    [[nodiscard]] QString rootDir() const noexcept { return rootDir_; }
    [[nodiscard]] QString settingsPath() const;
    // Active profile's learning file: learning.json for "Default", else
    // learning.<slug>.json (see profileLearningSlug).
    [[nodiscard]] QString learningPath() const;
    [[nodiscard]] static QString profileLearningSlug(const QString& profileName);

    // === Settings-file schema versioning + migration (2026-08-08) ==================
    //
    // THE DEFECT THIS EXISTS TO FIX. load() reads every key as cleanX(obj, key,
    // codeDefault) -- the on-disk value always wins -- and save() writes every key
    // back. So the first save() an install ever performs freezes THAT build's
    // defaults into settings.json, and a default flipped in code afterwards is
    // permanently invisible to that install: the file says the old value, and
    // nothing can tell "the user chose this" from "save() wrote the default back".
    // (Live example, owner's install 2026-08-08: user_lead_satisfies_authority=false
    // and latency_probe_count=16 on disk, both defaults flipped in code 2026-08-07.)
    //
    // THE MECHANISM. settings_version stamps the schema/default generation that last
    // wrote the file. Absent or malformed = version 0 = every file written before
    // versioning existed (a corrupt stamp degrades to "pre-versioning", never to a
    // wipe). save() always stamps kSettingsVersion. load() compares the on-disk
    // stamp against kSettingsVersion and, when older, applies the registered
    // migrations in order to the raw JSON before parsing, then persists once -- so a
    // migration runs exactly once per install per version step, and a user who flips
    // a migrated key back afterwards is never re-migrated (the stamp is already
    // current). A file stamped current or NEWER (written by newer code) is passed
    // through untouched.
    //
    // MIGRATIONS ARE AN EXPLICIT ALLOWLIST, NEVER A BLANKET RE-DEFAULT. A rule names
    // exactly ONE key, the version it lands in, the OLD default it replaces, the
    // value it installs, and (when one exists) the *_user_set companion that vetoes
    // it. Structurally it cannot re-default wholesale: the runner writes only
    // rule.key, only when the current value is absent or still equal to the old
    // default, and never when the companion says the user chose the value. For the
    // many keys with NO companion we cannot distinguish "user chose this" from
    // "save() wrote the default back" -- that ambiguity is the original defect -- so
    // registering a migration for such a key is a deliberate judgement that the old
    // value is a correctness bug for everyone, made one key at a time in
    // settingsMigrations() with the evidence cited. When in doubt, do not migrate.
    //
    // Bump kSettingsVersion by ONE per new migration batch and register the rules
    // under that version; never renumber or edit a shipped rule.
    // v2 (2026-09-01): retire the legacy auto-persisted
    // tip_timing_auto_unlock=true default.  The anchor-to-freeze instrument measures
    // the release landing, so that value can walk an already-early aim earlier.
    static constexpr int kSettingsVersion = 2;

    struct SettingsMigration {
        int targetVersion;        // the settings_version this rule lands in
        QString key;              // the ONE key this rule may write
        QJsonValue oldDefault;    // applies only when the key is absent or equals this
        QJsonValue newValue;      // the value the rule installs
        QString userSetCompanion; // *_user_set key that vetoes the rule; empty = none exists
        QString rationale;        // one-line justification (logged when applied)
    };
    // The shipped registry (the allowlist). See the doc block above.
    [[nodiscard]] static const QList<SettingsMigration>& settingsMigrations();
    // Defensive stamp parse: absent, non-numeric, negative or non-finite all
    // degrade to 0 (pre-versioning). Truncates fractional stamps downward -- a
    // re-run migration is idempotent, a skipped one is a silent hole.
    [[nodiscard]] static int settingsVersionOf(const QJsonObject& obj);
    // Applies every rule newer than fromVersion (and not vetoed) to the raw JSON
    // object; returns the keys actually rewritten. Public and registry-injectable so
    // the veto/allowlist contract is directly testable without shipping a synthetic
    // migration rule.
    static QStringList applySettingsMigrations(QJsonObject& obj, int fromVersion,
                                               const QList<SettingsMigration>& rules);

    bool load();
    bool save(const AppConfigData& data, QString* error = nullptr);
    // [ORION_NO_METER_V2 2026-09-14 owner] NO METER is IN the shipped product again (the
    // 2026-09-13 removal was of a broken control law, not of the feature), so this defaults to
    // TRUE and nothing on the load/save path or in the controller consults it any more. The
    // pair survives as a one-call kill switch — AppConfig lives in a SHARED library, so a
    // compile define on one target cannot reach it and a runtime flag is the only mechanism
    // that works — but it is deliberately unwired: a mode the page offers must not also be
    // silently revocable from a static.
    static bool inputTimedAllowed() noexcept;
    // [ORION_NO_METER_SHELVED 2026-09-15] THE ONE DOOR THE TESTS KEEP. The shipped answer is
    // false (see the fence in AppConfig.cpp); a test that needs to drive the blind path through
    // the real load/save surface flips this on, and MUST flip it back -- it is process-wide.
    static void setInputTimedAllowedForTesting(bool allowed) noexcept;
    // [ORION_SPRINT_RELEASE_FENCED 2026-09-17 owner] The sprint-release fence, built on the same
    // shape as the pair above because it failed in the same way: a persisted `true` beat a
    // flipped default and cost a session. False in every shipped build -- load() and save() then
    // force sprint_release_on_square false -- unless ORION_SPRINT_RELEASE_ON_SQUARE is exactly
    // "1", which is the one documented door and is a DEV A/B door: it also arms the engine's own
    // override, so both halves of the feature come back together or not at all.
    static bool sprintReleaseAllowed();
    // [ORION_SPRINT_RELEASE_FENCED 2026-09-17] THE DOOR THE TESTS KEEP, exactly like
    // setInputTimedAllowedForTesting: the six sprint tests drive the shaping itself, and the
    // round-trip test drives the real load/save surface. Process-wide -- a test that flips this
    // on MUST flip it back.
    static void setSprintReleaseAllowedForTesting(bool allowed) noexcept;
    bool saveLearning(const LearningData& data, QString* error = nullptr);
    // Re-read the active profile's learning file (fresh defaults when missing).
    // Called after a profile switch so the engine warm-starts that profile's clocks.
    void reloadLearning();

    // Profile metadata blob persisted verbatim in settings.json ("profiles" key).
    [[nodiscard]] QJsonObject profiles() const { return profiles_; }
    void setProfiles(const QJsonObject& profiles) { profiles_ = profiles; }

signals:
    void configChanged(orion::AppConfigData data);

private:
    static QString cleanText(const QJsonObject& obj, const char* key, QString fallback, qsizetype maxLen);
    static double cleanDouble(const QJsonObject& obj, const char* key, double fallback, double lo, double hi);
    static int cleanInt(const QJsonObject& obj, const char* key, int fallback, int lo, int hi);
    static bool cleanBool(const QJsonObject& obj, const char* key, bool fallback);

    void loadSettingsObject(const QJsonObject& obj);
    void loadLearningObject(const QJsonObject& obj);

    QString rootDir_;
    AppConfigData data_;
    LearningData learning_;
    QJsonObject profiles_;
};

} // namespace orion

// [ORION_NO_METER_V2 2026-09-14] The hold learner's record travels through a signal
// (AutomationEngine::noMeterHoldLearned) on its way to learning.json, so the metatype has to be
// declared for it exactly as any other signal payload.
Q_DECLARE_METATYPE(orion::NoMeterHoldRecord)
