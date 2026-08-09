#pragma once

#include <QtCore/QMetaType>
#include <QtCore/QString>

namespace orion {

enum class ShotMode {
    TempoSquare,
    TempoStick,
    GoToStick,
    ButtonShot
};

enum class HoldState {
    Idle,
    Armed,
    Holding,
    GreenWindow,
    Releasing,
    PumpFake,
    Cooldown
};

enum class RemotePlayState {
    Disconnected,
    Connecting,
    Running,
    Error
};

// Exact local controller backend bound to a restored timing-cache attestation. This enum is
// provenance metadata only and is never used as a release/shot command.
enum class LatencyControllerRoute : uint8_t {
    None = 0,
    Pipe,
    VigemDs4,
    VigemXusb
};

inline constexpr uint16_t XINPUT_GAMEPAD_DPAD_UP = 0x0001;
inline constexpr uint16_t XINPUT_GAMEPAD_DPAD_DOWN = 0x0002;
inline constexpr uint16_t XINPUT_GAMEPAD_DPAD_LEFT = 0x0004;
inline constexpr uint16_t XINPUT_GAMEPAD_DPAD_RIGHT = 0x0008;
inline constexpr uint16_t XINPUT_GAMEPAD_START = 0x0010;
inline constexpr uint16_t XINPUT_GAMEPAD_BACK = 0x0020;
inline constexpr uint16_t XINPUT_GAMEPAD_LEFT_THUMB = 0x0040;
inline constexpr uint16_t XINPUT_GAMEPAD_RIGHT_THUMB = 0x0080;
inline constexpr uint16_t XINPUT_GAMEPAD_LEFT_SHOULDER = 0x0100;
inline constexpr uint16_t XINPUT_GAMEPAD_RIGHT_SHOULDER = 0x0200;
inline constexpr uint16_t XINPUT_GAMEPAD_GUIDE = 0x0400;
inline constexpr uint16_t XINPUT_GAMEPAD_A = 0x1000;
inline constexpr uint16_t XINPUT_GAMEPAD_B = 0x2000;
inline constexpr uint16_t XINPUT_GAMEPAD_X = 0x4000;
inline constexpr uint16_t XINPUT_GAMEPAD_Y = 0x8000;

struct ControllerState {
    uint16_t buttons = 0;
    int dpad = 8;
    int leftStickX = 0;
    int leftStickY = 0;
    int rightStickX = 0;
    int rightStickY = 0;
    uint8_t l2 = 0;
    uint8_t r2 = 0;
    bool touchpad = false;
    bool lightbarSet = false;
    uint8_t lightbarR = 124;
    uint8_t lightbarG = 58;
    uint8_t lightbarB = 237;

    [[nodiscard]] ControllerState clone() const { return *this; }

    bool square() const { return (buttons & XINPUT_GAMEPAD_X) != 0; }
    bool cross() const { return (buttons & XINPUT_GAMEPAD_A) != 0; }
    bool circle() const { return (buttons & XINPUT_GAMEPAD_B) != 0; }
    bool triangle() const { return (buttons & XINPUT_GAMEPAD_Y) != 0; }
    bool l1() const { return (buttons & XINPUT_GAMEPAD_LEFT_SHOULDER) != 0; }
    bool r1() const { return (buttons & XINPUT_GAMEPAD_RIGHT_SHOULDER) != 0; }
    bool options() const { return (buttons & XINPUT_GAMEPAD_START) != 0; }
    bool create() const { return (buttons & XINPUT_GAMEPAD_BACK) != 0; }
    bool ps() const { return (buttons & XINPUT_GAMEPAD_GUIDE) != 0; }
    bool rightStickUp(double threshold = 50.0) const { return rightStickY <= -threshold; }
    bool rightStickDown(double threshold = 50.0) const { return rightStickY >= threshold; }
};

struct DetectionResult {
    bool detected = false;
    bool staleFrame = false;
    bool ghostFrame = false;
    QString style;
    QString profileName;
    QString colorName = QStringLiteral("Purple");
    QString detectorSource = QStringLiteral("native-bgr");
    QString rejectionReason;
    int x = 0;
    int y = 0;
    int width = 0;
    int height = 0;
    // Coordinate space of x/y/width/height. The sidecar stamps this as
    // bbox_wh on the same immutable detector snapshot as the bbox; using the
    // independently advancing capture_width/height telemetry can otherwise
    // clamp a valid box away during a source/resolution handoff.
    int bboxFrameWidth = 0;
    int bboxFrameHeight = 0;
    int searchX = 0;
    int searchY = 0;
    int searchWidth = 0;
    int searchHeight = 0;
    int rejectedX = 0;
    int rejectedY = 0;
    int rejectedWidth = 0;
    int rejectedHeight = 0;
    double fillPct = 0.0;
    double confidence = 0.0;
    int consecutiveFrames = 0;
    double velocityPctS = 0.0;
    double accelerationPctS2 = 0.0;
    double predictionPct = 0.0;
    double targetPct = 96.0;
    double profileTimingMs = 0.0;
    double frameAgeMs = 0.0;
    int candidateCount = 0;
    bool releaseReady = false;
    bool velocityStable = false;
    int greenClusterPx = 0;
    double greenStartPct = -1.0;
    double greenEndPct = -1.0;
    double greenCenterPct = -1.0;
    double greenWidthPct = 0.0;
    double greenConfidence = 0.0;
    double etaToGreenMs = -1.0;
    bool roiLocked = false;
    // True only when the production reader proved connected meter-specific structure in the
    // CURRENT physical hardware-shot epoch. Temporal rise is insufficient: menus/loading bars
    // can animate. Older sidecars omit this and retain the fail-closed false default.
    bool gameplayStructureVerified = false;
    // Exact physical-shot epoch in which gameplayStructureVerified was proved. Transported
    // as a canonical decimal string across JSON so all 64 bits remain exact. Zero means the
    // proof came from an untokenized/local arm and can never authorize automatic calibration.
    quint64 gameplayStructureEpoch = 0;
    // === Tip-timing IPC contract (Python sidecar per-frame payload fields) ===
    // [ORION_MEASURED_LEAD] Observed/shadow posterior. It may move after any accepted label and is
    // never sufficient release authority on its own. 0 = not provided (older sidecar) -> inert.
    double measuredLatencyMs = 0.0;
    // Atomic actuation contract published by the estimator: "factory" uses the immutable packaged
    // route prior, "validated" uses the causally validated posterior, and "none" fails closed.
    // Older sidecars omit these fields and therefore cannot authorize autonomous timing.
    QString measuredLatencyAuthorityKind = QStringLiteral("none");
    double measuredLatencyAuthorityMs = 0.0;
    double measuredLatencyAuthoritySdMs = 0.0;
    // Confidence [0,1] and label count of the measured-latency oracle (measured_latency_conf /
    // measured_latency_n). The point value alone can't tell a 1-sample boot guess from a converged
    // 12-label median; the PRIOR->POSTERIOR blend weights the vision posterior by this confidence.
    // 0/0 = not provided (older sidecar) -> the blend degrades to the point value / pure clock.
      double measuredLatencyConf = 0.0;
      int measuredLatencyN = 0;
      // L1 provenance for two-stage causal calibration. This permits only an in-green validation
      // schedule; it is never tip-release authority by itself.
      bool measuredLatencyControlledAnchor = false;
      // True only after a distinct delivery-confirmed validation release both agreed with L1 and
      // stopped inside its planned green residual. Ordinary or L1-only telemetry stays inert.
      bool measuredLatencyProvisional = false;
      // A cached total posterior remains provisional until two independent current-session
      // attestations succeed: sidecar video-route/capture health, then native neutral controller
      // delivery. Older sidecars omit both and therefore cannot enter restored authority.
    bool measuredLatencyRestored = false;
    bool measuredLatencyVideoRouteAttested = false;
    // Weak, release-manifested route model for zero-setup first-shot ownership. It is never a
    // measured label and is actuatable only with an exact current controller-route token.
    bool measuredLatencyFactoryPrior = false;
    QString measuredLatencyPriorSource;
    QString measuredLatencyModelVersion;
      // Native-issued neutral-delivery generation echoed by the sidecar only after its exact
      // latency scope has been re-keyed. Zero/missing is fail-closed. This is provenance only;
      // it is never a release sequence, shot identity, or estimator observation.
      quint64 measuredLatencyAttestationGeneration = 0;
      LatencyControllerRoute measuredLatencyDeliveryRoute = LatencyControllerRoute::None;
      // Sidecar-process-monotonic identity of the exact latency estimator/source scope.
      // Every effective capture source or mode replacement increments it, even when the
      // route string is unchanged. Canonical decimal-string IPC; zero/missing cannot bind
      // a controller proof and never grants autonomous timing authority.
      quint64 measuredLatencyScopeEpoch = 0;
    // [ORION_REG_FUSION] reg_tip_ms + reg_conf: registration-based time-to-TIP estimate + confidence,
    // flat/accurate at the FAR horizon (commit >~200ms out). -1/0 = not provided / no registration lock.
    double regTipMs = -1.0;
    double regConf = 0.0;
    // Predictor payload is valid only when regSeq equals this detection's frame_count. Absolute
    // timestamps use the same capture-epoch clock as captureTsMs; native also checks that
    // (tip-sample) agrees with regTipMs before accepting the forecast.
    int regSeq = -1;
    double regTipCaptureMs = 0.0;
    double regSampleCaptureMs = 0.0;
    double regSigmaMs = -1.0;
    QString regModelId;
    QString regModelVersion;
    QString regFitMethod;
    // Registration fit-quality companions (Phase-1 fusion sigma): unweighted residual RMS in
    // fill-% + uncensored sample count of the online fit. -1/0 = no fit this frame.
    double regRmsePp = -1.0;
    int regN = 0;
    // POST-HOC full-shot refit tip label (once per completed shot; capture-epoch ms). The clock
    // prior's EMA teacher — never the frozen self-grade. 0/0/0 = none yet / older sidecar.
    int posthocN = 0;
    double posthocTipMs = 0.0;
    double posthocConf = 0.0;
    // Oracle v2 posterior components: the FIXED latency (rtt + tick-wait stripped) and its
    // posterior sd. The engine reconstitutes lead = l_fixed + rtt_now + tick_wait at consumption.
    double measuredLFixedMs = 0.0;
    double measuredLatencySdMs = 0.0;
    // [Phase-2 A2(c)] console INPUT-tick phase from the warmup probe run's sawtooth fit
    // (tick_phase_ms = the input-sample EDGE phase, press-epoch ms modulo 16.67; conf decays
    // sidecar-side, halved per 10 min since the probe run). Distinct from the rtt engine's
    // network tick. -1/0/-1 = not provided (no probe run / older sidecar) -> the engine's
    // earlier-only fused tick snap stays fully disengaged (byte-identical behaviour).
    double tickPhaseMs = -1.0;
    double tickPhaseConf = 0.0;
    double tickPhaseSdMs = -1.0;
    // Detector-only release-window diagnostic: where the release COMMAND landed relative to the
    // colour-derived meter window. This is not an NBA 2K make/miss outcome and must not feed
    // session accuracy, gameplay learning, or bandit tuning. label: 0 = command early,
    // 1 = command in detected window, 2 = command over; -1 = absent. releaseSeq is the immutable
    // native id captured by SimpleMeterReader.notify_release(seq); proxy or unmatched records are
    // rejected before use. Older sidecars retain these fail-closed defaults.
    int greenGradeLabel = -1;
    int greenGradeSeq = -1;
    int greenGradeReleaseSeq = 0;
    // Exact ownership identity copied from the delivered native marker.  These
    // travel as canonical decimal strings on the JSON wire so uint64 values are
    // never rounded. Zero is the fail-closed legacy/missing sentinel.
    quint64 greenGradePhysicalShotEpoch = 0;
    quint64 greenGradeShotAttempt = 0;
    bool greenGradeReleaseProxy = true;
    double greenGradeWindowConfidence = 0.0;
    double greenGradeStartPct = -1.0;
    double greenGradeFillAtRelease = -1.0;
    // Raw source/publication epoch of this unique frame. It is the stable
    // same-frame identity stamp, not the decoder measurement timeline.
    double captureTsMs = 0.0;
    // Canonical epoch of these pixels for timing math. Decoder mode maps PTS onto
    // epoch; capture-card mode equals captureTsMs. The sampler, registration, and
    // frame-age lease must all use this one domain. 0 = older sidecar/no authority.
    double measurementCaptureTsMs = 0.0;
    // [A0] Reader stage for this frame: track / track_green / acquire / coast / no_meter.
    // Distinguishes a fresh read from a coasted (held/dead-reckoned) sample without string-
    // matching rejectionReason. Empty = older sidecar / legacy chain detector.
    QString stage;
    // [FRAME-ID JOIN] Decoder frame number (orch._last_decoded_frame_number) of the frame this
    // detection was computed on — the SAME counter the preview `frame` stream stamps each JPEG with.
    // The overlay uses it to composite the lock box on the exact preview frame it was detected on
    // (fixes the drift/flicker from painting the latest bbox onto a later async-decoded image).
    // NOTE: this is the decoder wire seq, NOT the unique-frame dedup key `frame_count`; the two are
    // different counters, only the decoder number is shared with the preview stream. -1 = older
    // sidecar that doesn't emit `frame_number` in telemetry -> the overlay falls back to latest-box.
    int frameNumber = -1;
};

struct TelemetrySnapshot {
    QString consoleIp;
    QString courtIp;
    // A public endpoint inferred only from sustained passive UDP flow. This is
    // display data, never RTT/tick/automation authority. courtIp remains the
    // sidecar's verified timing target; keeping the fields distinct prevents a
    // diagnostic observation from silently becoming a release offset source.
    QString diagnosticCourtIp;
    QString syncSource = QStringLiteral("Waiting");
    bool syncActive = false;
    double syncConfidence = 0.0;
    int inboundPackets = 0;
    int outboundPackets = 0;
    double offsetMs = 0.0;
    double syncAdjustMs = 0.0;
    double rttMs = 0.0;
    // True only when rttMs was measured against a positively identified public
    // court endpoint. A console/gateway LAN ping is useful diagnostics, but it
    // is not game-path RTT and must never drive release timing or be presented
    // as a network round-trip.
    bool rttTargetVerified = false;
    double jitterMs = 0.0;
    int consecutivePackets = 0;
    double packetIntervalMs = 0.0;
    int inboundConsecutive = 0;
    int outboundConsecutive = 0;
    double inboundIntervalMs = 0.0;
    double outboundIntervalMs = 0.0;
    double tickerLatencyMs = 0.0;
    bool tickPhaseVerified = false;
    double tickPhaseConfidence = 0.0;
    qint64 tickPhaseObservedEpochMs = 0;
    bool playingGame = false;
};

// Batched meter-side HUD telemetry for the live overlay beside the detected
// meter. Every field is a COPY of an already-authoritative engine value taken
// on the same 4 ms input tick that produced it; nothing here is recomputed,
// extrapolated, or re-derived for presentation. The overlay reads only this
// struct, so the whole readout is one snapshot of one instant rather than a
// set of independently-aged bindings.
//
// TRUTH BOUNDARY: negative / false / empty is the fail-closed "not available"
// sentinel and the overlay HIDES that line. A placeholder value is never shown.
// Consumers must not substitute a plausible number for an absent one.
struct LiveMeterTelemetry {
    // False whenever no bot-owned shot with a fresh genuine detector sample
    // exists. The entire overlay is hidden; individual lines are additionally
    // gated by their own sentinels.
    bool valid = false;

    // Genuine raw detector fill for this snapshot (OrionAppController's
    // measuredMeterFillPct_, i.e. the same freshness/arm-token-gated value the
    // existing live card publishes). ShotContext::fillPct may legitimately
    // coast through an occlusion for timing; the HUD never presents a coasted
    // estimate as a camera measurement. -1 = unavailable.
    double fillPct = -1.0;
    // ShotContext::releaseVelocityPctMs — the ENGINE SAMPLER slope in %/ms that
    // the predictive paths actually use. Deliberately not the detector's
    // velocityPctS. 0 with fillVelocityValid=false means unavailable.
    double fillVelocityPctMs = 0.0;
    bool fillVelocityValid = false;

    // Canonical tip decision: tipAbsMs - now, from ShotContext's
    // releaseCrossingEtaMs stamped by the same tick. NOT a QML timer and not a
    // green-centre or registration substitute. -1 = no usable crossing.
    double tipEtaMs = -1.0;
    // Command deadline: ShotContext::tipPredictionDeadlineMs (absolute engine
    // clock fireAtMs = tipAbs - lead) minus now. May be NEGATIVE — a deadline
    // already in the past is exactly the failure the 2026-08-03 session hit and
    // must stay visible rather than being clamped to zero.
    double commandEtaMs = 0.0;
    bool commandEtaValid = false;
    // True while a precise-fire token is actually armed for this deadline
    // (AutomationEngine::scheduledFireToken() != 0). This is the schedule, not
    // the reservation; see reservationState below.
    bool commandScheduled = false;

    // Actuation lead authority in force for this snapshot.
    double leadMs = -1.0;
    QString leadKind;               // tipPredictionAuthorityKind: factory | validated | none
    double leadSigmaMs = -1.0;      // tipPredictionLeadSigmaMs
    // Combined predictor sigma (tipPredictionSigmaMs). -1 = unavailable.
    double predictorSigmaMs = -1.0;

    // tipPredictionSource (sampler / registration / fused / none) and the age of
    // the frame this snapshot was computed from. -1 = unavailable.
    QString predictorSource;
    double frameAgeMs = -1.0;

    // AutonomousTipReservation disposition + refinement count. AutomationEngine
    // currently keeps tipReservation_ private with no accessor, so these stay at
    // their fail-closed defaults and the reservation line is hidden. They are
    // declared here so wiring an accessor is a pure engine-side change.
    QString reservationState;
    int reservationUpdates = -1;
};

} // namespace orion

Q_DECLARE_METATYPE(orion::DetectionResult)
Q_DECLARE_METATYPE(orion::TelemetrySnapshot)
Q_DECLARE_METATYPE(orion::LiveMeterTelemetry)
