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
    // 2026-07-25 SHIPPING RECONCILIATION: settings.json is gitignored + excluded from the installer,
    // so these defaults ARE the customer's first run. Reconciled to the proven live rig
    // (settings.json meter_color=Red / meter_style=Arrow2). Corroboration for Red: the sidecar
    // orchestrator force-pins meter_color=Red whenever the park temporal locator is on (default on,
    // remote_play_orchestrator.py:595-604), the shipped SimpleMeterReader bakes RED bands
    // (simple_meter_reader.py:228), and RemotePlaySession.cpp:1639-1644 documents that a forced
    // "Purple" mask is THE bug ("the user's meter is RED").
    QString meterColor = QStringLiteral("Red");
    QString meterStyle = QStringLiteral("Arrow2");
    QString remotePlayClientMode = QStringLiteral("chiaki");
    // Target console family: "PS5" (chiaki Remote Play + DualSense ViGEm, the proven
    // path) or "Xbox" (capture the official Xbox app window via WGC + drive an X360
    // ViGEm pad — no open client to embed). Default PS5; selecting Xbox flips the
    // controller backend + capture source. See [[nexusvision-detection-capture-sprint]].
    QString remotePlayConsole = QStringLiteral("PS5");
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
    // The default is a BRIGHT BLUE (owner directive 2026-08-06, replacing the
    // original reference violet-magenta). Blue sits ~210 degrees off the
    // meter's saturated red, so the stroke can never blend into the bar it is
    // tracing; against a floodlit white court a bright blue alone is lower
    // contrast than the old violet was, and the 1px dark keylines that bracket
    // the stroke (Theme.meterLockKeyline) are what carry that edge.
    //
    // The default lives HERE as data — QML binds `meterOverlayDrawColor` and
    // never hardcodes a hue — so future restyles are a constant change, not a
    // code change.
    //
    // Since 2026-08-06 the colour is ALSO not user-customisable: the owner
    // removed the picker ("remove the custom detection box completely, have a
    // bright blue meter detection box as the default"), and the settings
    // loader pins every persisted meter_overlay_color / meter_overlay_rgb back
    // to this default (see AppConfig::loadSettingsObject). The property
    // plumbing (meterOverlayColor / meterOverlayRgb / meterOverlayDrawColor)
    // stays because the draw path, the HUD accent, and the overlay tests all
    // ride it — only the ability to change the value was removed.
    static constexpr const char* kMeterOverlayDefaultColor = "#00A8FF";
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
    QString meterOverlayStyle = QStringLiteral("Solid"); // Solid | Brackets | Hairline
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
    // [ORION_AIM_FREEZE] (2026-08-06, default OFF): hold the learned aim constant still for the
    // session. Measured: the aim walked 8.2ms across one 70-release batch (landing sd 10.5ms), and
    // that session's second half measured worse than its first. Demo/batch tool — freeze a
    // known-good aim; clear it when the equipped jumpshot changes.
    bool tipPhaseAimFrozen = false;
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
    static constexpr int kSettingsVersion = 1;

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
