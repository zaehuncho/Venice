#pragma once

#include <chrono>

#include "AppConfig.h"
#include "LeadCalibrationPolicy.h"
#include "ApplicationShutdownPolicy.h"
#include "AutomationEngine.h"
#include "OrionInputClient.h"
#include "OrderedFileLogSink.h"
#include "ControllerDeviceSelector.h"
#include "InputSessionRetryPolicy.h"
#include "LeaseGate.h"
#include "LicenseHeartbeatPolicy.h"   // [CL2-P8-002 2026-09-23]
#include "LatencyRouteAttestationHandshake.h"
#include "LicenseClient.h"
#include "LiveMeterHudPolicy.h"
#include "ManualShotTally.h"
#include "MeterBoxRing.h"
#include "MeterBlindnessLatch.h"   // [RT-MED-04 2026-09-23]
#include "MeterDelayController.h"
#include "MeterDetector.h"
#include "MeterOverlayPolicy.h"
#include "NetworkBridge.h"
#include "PressedOverlayPolicy.h"
#include "RemotePlaySession.h"
#include "RemoteFrameSnapshotStore.h"
#include "ReleaseMarkerDeliveryGate.h"
#include "SecurityManager.h"
#include "ShotIntentPolicy.h"
#include "ShotVerdictTally.h"
#include "SquareOutputWatchdog.h"
#include "UiNotificationPolicy.h"
#include "VeniceNetClient.h"
#include "VirtualController.h"

#include <QtCore/QMutex>
#include <QtCore/QVariantList>
#include <QtCore/QObject>
#include <QtCore/QAbstractNativeEventFilter>
#include <QtCore/QElapsedTimer>   // [CL2-P8-002 2026-09-23]
#include <QtCore/QHash>
#include <QtCore/QRect>
#include <QtCore/QSize>
#include <QtCore/QStringList>
#include <QtCore/QTimer>
#include <QtCore/QUrl>
#include <QtGui/QColor>
#include <QtGui/QImage>
#include <QtQuick/QQuickImageProvider>

#include <array>
#include <atomic>
#include <cmath>
#include <limits>
#include <thread>

namespace orion {

enum class ControllerLifecycleState {
    NoPhysical,
    PhysicalPresentNoLive,
    PhysicalLive,
    VirtualStarting,
    VirtualReady,
    StreamingMirror,
    ControllerFault
};

// Immutable copy of the controller transaction that actually satisfied a
// precise-fire deadline.  The GUI tick can run several milliseconds after the
// worker accepted the release, so its current `output` and OrionInputClient's
// current last packet are not reliable substitutes for this mailbox payload.
struct PreciseFireDeliverySnapshot final {
    ControllerState output{};
    OrionInputPacket pipePacket{};
    bool outputValid = false;
    bool pipePacketValid = false;
    quint64 routeGeneration = 0;
    LatencyControllerRoute route = LatencyControllerRoute::None;
    // Immutable scheduler-vs-delivery timing. commandIssuedMs is the fire
    // authority consumed by AutomationEngine. activeRouteCompleteMs remains
    // separate so a slow local ACK/submit is observable without teaching that
    // delay as scheduler lateness.
    double commandIssuedMs = -1.0;
    double activeRouteCompleteMs = -1.0;
    double activeRouteDurationMs = -1.0;
    OrionInputTransactionTiming pipeTiming{};
    double fireEpochMs = -1.0;
};

// [ORION_TIP_FRAME_NATIVE 2026-09-17] What the post-submit `Release submit:` line needs in order
// to say WHERE ON THE CONSOLE'S OWN FRAME GRID the command actually landed, snapshotted at
// releaseIssued because the shot (and its grid) is reset long before that line is written.
//
// WHY THIS EXISTS. docs/POLL_PHASE_TRACKER.md §10 could prove that the SCHEDULE carried the
// frame-centre offset and that the FIRE did not, but only by reconstructing the grid offline from
// a different log line. One line settles it live: the grid phase of the release and its signed
// distance to the centre it was aimed at, computed from the same GameFramePhase estimate the
// reservation logged.
struct ReleaseFrameGridSnapshot {
    // The shot's own recovered grid, carried whole so the release phase is computed against the
    // SAME estimate the reservation logged rather than a re-derived one.
    orion::game_frame_phase::Estimate grid{};
    QString fireTargetMode;    // "frame_centre" | "instant" | empty (never reached a vision arm)
    double leadMs = -1.0;      // the lead THIS shot's arm spent; the command lands lead ms later
    double alignedFireAtMs = -1.0;    // the instant the armed token was aiming at
    double unalignedFireAtMs = -1.0;  // what the bare tip alone would have armed
};

// Cached WinMM axis ranges (JOYCAPS wXmin/wXmax etc.) so the per-tick WinMM fast path can
// re-read a KNOWN joystick id without re-querying joyGetDevCapsW every tick — and without
// dragging <mmsystem.h> (JOYCAPS) into this header. Filled by the 1/s WinMM discovery scan.
struct WinMmAxisRanges {
    quint32 xMin = 0, xMax = 0;
    quint32 yMin = 0, yMax = 0;
    quint32 zMin = 0, zMax = 0;
    quint32 rMin = 0, rMax = 0;
};

struct RemoteFrameProviderStats {
    quint64 framesSet = 0;
    quint64 imageRequests = 0;
    quint64 snapshotMisses = 0;
    double maxRequestGapMs = 0.0;
};

class RemoteFrameProvider final : public QQuickImageProvider {
public:
    RemoteFrameProvider();

    QImage requestImage(const QString& id, QSize* size, const QSize& requestedSize) override;
    void setFrame(int serial, const QImage& frame);
    void resetFrames(int serial, const QImage& fallback);
    RemoteFrameProviderStats takeWindowStats();
    void resetStats();

private:
    mutable QMutex mutex_;
    RemoteFrameSnapshotStore snapshots_;
    quint64 windowFramesSet_ = 0;
    quint64 windowImageRequests_ = 0;
    quint64 windowSnapshotMisses_ = 0;
    qint64 lastSetSteadyNs_ = 0;
    qint64 lastRequestSteadyNs_ = 0;
    qint64 maxRequestGapNs_ = 0;
};

class OrionRawInputWorker;
class OrionPreciseFireThread;

class OrionAppController final : public QObject, public QAbstractNativeEventFilter {
    Q_OBJECT
    // The dedicated raw-input thread calls back into appendLog/handleRawInputDeviceChange
    // (queued onto the GUI thread).
    friend class OrionRawInputWorker;
    // The sub-tick fire thread submits the engine-committed release output at its exact
    // deadline (controller_/orionInput_ under submitMutex_).
    friend class OrionPreciseFireThread;

    Q_PROPERTY(bool authenticated READ authenticated NOTIFY authChanged)
    Q_PROPERTY(bool authBusy READ authBusy NOTIFY authChanged)
    Q_PROPERTY(QString authMessage READ authMessage NOTIFY authChanged)
    // MOTD (docs/ADMIN_PANEL_V2_CONTRACT.md §5): the owner's notice, carried by the
    // /api/license/check heartbeat and /api/version as `motd {text, level, until}`.
    // RemotePlayPage shows it in the top-centre banner slot while motdVisible is
    // true: text present, not past `until`, and not dismissed. Dismissal lives in
    // memory only (never in settings) and a changed text re-shows the banner.
    Q_PROPERTY(QString motdText READ motdText NOTIFY motdChanged)
    Q_PROPERTY(QString motdLevel READ motdLevel NOTIFY motdChanged)   // info | warn | maint
    Q_PROPERTY(double motdUntil READ motdUntil NOTIFY motdChanged)    // Unix seconds; 0 == no expiry
    Q_PROPERTY(bool motdVisible READ motdVisible NOTIFY motdChanged)
    // [CL2-P8-002 2026-09-23] Plain customer copy while the fire lease blocks
    // automation for a signed-in user (offline, after sleep, server unreachable,
    // PC clock off). Empty when shots are allowed. RemotePlayPage shows it in the
    // top-centre banner slot (outranks the MOTD); TopStatusBar shows "Reconnecting".
    Q_PROPERTY(QString leaseNotice READ leaseNotice NOTIFY leaseNoticeChanged)
    // ── Licence / profile facts (bound by the Sidebar licence strip + flyout) ─────────────────────────────────
    // The backend's `profile` block on /api/activate + every /api/license/check
    // heartbeat (LicenseProfile, LicenseClient.h), refreshed on both. The CURRENT
    // live Lambda does not send one, so every property here reads as its
    // empty/zero default until the new backend ships — the page must render
    // either way. `profileKnown` is the honest "the server actually told us".
    Q_PROPERTY(bool profileKnown READ profileKnown NOTIFY profileChanged)
    Q_PROPERTY(QString profileDiscordId READ profileDiscordId NOTIFY profileChanged)
    Q_PROPERTY(QString profileDiscordName READ profileDiscordName NOTIFY profileChanged)
    Q_PROPERTY(QString profilePlan READ profilePlan NOTIFY profileChanged)
    Q_PROPERTY(double profileExpiryEpochS READ profileExpiryEpochS NOTIFY profileChanged)
    // Whole days remaining, rounded UP (a licence with 4 h left still says "1").
    //   -1 == unknown (no profile yet)      -2 == lifetime (no expiry)
    // profileLifetime is the same fact as a bool so QML can branch without
    // hard-coding the sentinel.
    Q_PROPERTY(int profileDaysLeft READ profileDaysLeft NOTIFY profileChanged)
    Q_PROPERTY(bool profileLifetime READ profileLifetime NOTIFY profileChanged)
    Q_PROPERTY(double profileActivatedEpochS READ profileActivatedEpochS NOTIFY profileChanged)
    Q_PROPERTY(int profileHwidResetsUsed READ profileHwidResetsUsed NOTIFY profileChanged)
    Q_PROPERTY(int profileHwidResetsFreeTotal READ profileHwidResetsFreeTotal NOTIFY profileChanged)
    Q_PROPERTY(int profileHwidResetsFreeRemaining READ profileHwidResetsFreeRemaining NOTIFY profileChanged)
    Q_PROPERTY(int profileHwidPaidCredits READ profileHwidPaidCredits NOTIFY profileChanged)
    // Support identity for the Profile page's "Machine" row. Suffix only — the
    // full fingerprint stays native-side, exactly like licenseKeyMasked.
    Q_PROPERTY(QString machineIdMasked READ machineIdMasked NOTIFY authChanged)
    // Zero-typing activation: a license key delivered via the orion://activate?key=...
    // deep link (protocol handler in main.cpp). AuthGate pre-fills its key field from
    // this; cleared once consumed. Never auto-submits — the user still clicks Unlock.
    Q_PROPERTY(QString pendingActivationKey READ pendingActivationKey NOTIFY pendingActivationKeyChanged)
    // Masked form of the activated key ("••••-••••-••••-A1B2") for support surfaces;
    // the full key never crosses into QML — copyLicenseKey() goes straight to clipboard.
    Q_PROPERTY(QString licenseKeyMasked READ licenseKeyMasked NOTIFY authChanged)
    Q_PROPERTY(QString licenseState READ licenseState NOTIFY statusChanged)
    Q_PROPERTY(QString serverState READ serverState NOTIFY statusChanged)
    Q_PROPERTY(QString updateState READ updateState NOTIFY statusChanged)
    Q_PROPERTY(QString latestVersion READ latestVersion NOTIFY statusChanged)
    Q_PROPERTY(bool updateAvailable READ updateAvailable NOTIFY statusChanged)
    Q_PROPERTY(bool updateMandatory READ updateMandatory NOTIFY statusChanged)
    Q_PROPERTY(bool updateBlocked READ updateBlocked NOTIFY statusChanged)
    Q_PROPERTY(QString updateNotes READ updateNotes NOTIFY statusChanged)
    Q_PROPERTY(bool updaterPresent READ updaterPresent NOTIFY statusChanged)
    // Startup update gate: "checking" -> "offer"/"force" (full-screen gate) -> "clear".
    // Dedicated notify: the root Loader binds to this, and the broad statusChanged
    // fires during page construction (binding loop on the swap).
    Q_PROPERTY(QString updateGatePhase READ updateGatePhase NOTIFY updateGateChanged)
    Q_PROPERTY(QString updateChannel READ updateChannel WRITE setUpdateChannel NOTIFY settingsChanged)
    Q_PROPERTY(bool devBuild READ devBuild CONSTANT)
    Q_PROPERTY(QString securityState READ securityState NOTIFY statusChanged)
    Q_PROPERTY(QString securityDetail READ securityDetail NOTIFY statusChanged)
    Q_PROPERTY(bool securityLockActive READ securityLockActive NOTIFY statusChanged)
    Q_PROPERTY(QString securityLockReason READ securityLockReason NOTIFY statusChanged)
    // [RT-MED-09 2026-09-23] True only while settings.json's signature is missing/invalid in a
    // locked build: the Live page then shows the scoped "Repair settings" banner.
    Q_PROPERTY(bool settingsRepairAvailable READ settingsRepairAvailable NOTIFY statusChanged)
    Q_PROPERTY(QString entitlementState READ entitlementState NOTIFY statusChanged)
    Q_PROPERTY(QString integrityState READ integrityState NOTIFY statusChanged)
    Q_PROPERTY(QString lastSecurityAuditEvent READ lastSecurityAuditEvent NOTIFY statusChanged)
    Q_PROPERTY(QString currentPage READ currentPage WRITE setCurrentPage NOTIFY navigationChanged)
    Q_PROPERTY(QString appVersion READ appVersion CONSTANT)
    // Displayed product identity ("1.0.0 BETA"); version.json / ORION_NATIVE_VERSION
    // stays the updater's source of truth.
    Q_PROPERTY(QString displayVersion READ displayVersion CONSTANT)
    // UI accent color (persisted settings.json launcher_custom_accent); drives the
    // QML Theme singleton.
    Q_PROPERTY(QString accentColor READ accentColor WRITE setAccentColor NOTIFY settingsChanged)
    // Debug page is hidden in production; set ORION_DEBUG=1 to expose it.
    Q_PROPERTY(bool debugUiEnabled READ debugUiEnabled CONSTANT)
    Q_PROPERTY(QString rootDir READ rootDir CONSTANT)
    Q_PROPERTY(QString iconSource READ iconSource CONSTANT)

    Q_PROPERTY(QString remoteState READ remoteState NOTIFY statusChanged)
    Q_PROPERTY(QString remoteStatus READ remoteStatus NOTIFY statusChanged)
    Q_PROPERTY(bool remoteRunning READ remoteRunning NOTIFY statusChanged)
    Q_PROPERTY(QString controllerStatus READ controllerStatus NOTIFY statusChanged)
    Q_PROPERTY(QString controllerInputSource READ controllerInputSource NOTIFY statusChanged)
    Q_PROPERTY(QString controllerDeviceDetail READ controllerDeviceDetail NOTIFY statusChanged)
    Q_PROPERTY(QString controllerHealth READ controllerHealth NOTIFY statusChanged)
    Q_PROPERTY(QString controllerStableDevice READ controllerStableDevice NOTIFY statusChanged)
    Q_PROPERTY(bool controllerIsPhysicalSony READ controllerIsPhysicalSony NOTIFY statusChanged)
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] True when a known Sony pad USB
    // instance still has EnhancedPowerManagementEnabled=1 (the rig-verified
    // cause of the pad dropping off USB after idle until re-plugged). Drives
    // the user-consented one-time fix button; see UsbPadPowerPolicy.h.
    Q_PROPERTY(bool controllerUsbPowerFixAvailable READ controllerUsbPowerFixAvailable NOTIFY statusChanged)
    Q_PROPERTY(double controllerLastInputAgeMs READ controllerLastInputAgeMs NOTIFY statusChanged)
    // [PRESSED OVERLAY 2026-08-08] Raw physical Square held (XINPUT_GAMEPAD_X /
    // raw_button_mask 0x4000), updated synchronously on the SAME input-poll tick
    // that feeds the meter-delay actuator and mints physical shot epochs — never
    // a delayed heartbeat. Drives the local "PRESSED" acknowledgment badge only:
    // purely local display, no console traffic, no shot/fire authority, and NOT
    // part of the meter-box/detection overlay family. Own notify signal so the
    // per-press flip never rides the statusChanged fan-out.
    Q_PROPERTY(bool physicalSquarePressed READ physicalSquarePressed NOTIFY physicalSquarePressedChanged)
    // [ORION_INPUT_DEAD_UX 2026-08-30] The "video alive, input dead" trap, surfaced. In
    // capture-card mode the HDMI feed keeps playing whatever the Chiaki input session does, so a
    // failed promotion / dropped session leaves the game looking perfectly alive while every
    // button press is dead (owner: "sometimes the bot won't shoot even when holding square.
    // That must NOT happen"). These properties drive the unmissable overlay on the live preview.
    // The CORE condition is orion::pressUndeliverable() — the exact predicate the per-press
    // "PRESS UNDELIVERABLE" forensic line evaluates — so the log and the screen cannot disagree.
    //   inputDeadOverlayActive: show the overlay (input undeliverable + live video + the player
    //                           either asked for a session or just pressed into dead input);
    //   inputDeadCritical:      terminal dead state (red, screaming) vs Connecting (amber note);
    //   inputDeadHeadline/Detail: what is happening + what will fix it (retrying / press
    //                           Connect / console waking / recovering input link).
    Q_PROPERTY(bool inputDeadOverlayActive READ inputDeadOverlayActive NOTIFY inputDeliveryStateChanged)
    Q_PROPERTY(bool inputDeadCritical READ inputDeadCritical NOTIFY inputDeliveryStateChanged)
    Q_PROPERTY(QString inputDeadHeadline READ inputDeadHeadline NOTIFY inputDeliveryStateChanged)
    Q_PROPERTY(QString inputDeadDetail READ inputDeadDetail NOTIFY inputDeliveryStateChanged)
    Q_PROPERTY(QString controllerLedStatus READ controllerLedStatus NOTIFY statusChanged)
    Q_PROPERTY(QString chiakiEmbedStatus READ chiakiEmbedStatus NOTIFY statusChanged)
    Q_PROPERTY(bool qmlRenderMode READ qmlRenderMode NOTIFY statusChanged)
    Q_PROPERTY(QString captureSourceHealth READ captureSourceHealth NOTIFY statusChanged)
    // Live capture-card preview active (HDMI feed shown in the dashboard BEFORE Connect, no Chiaki).
    // Kept separate from remoteRunning so the Start button stays enabled while the preview plays.
    Q_PROPERTY(bool capturePreviewActive READ capturePreviewActive NOTIFY statusChanged)
    Q_PROPERTY(QString backendMessage READ backendMessage NOTIFY statusChanged)
    Q_PROPERTY(int frameSerial READ frameSerial NOTIFY frameChanged)
    // Preview-only jitter fix: the RemotePlayPage live Image loads asynchronously
    // by default (worker-thread provider pull, presented when ready) instead of
    // synchronously on the GUI thread per frame. ORION_PREVIEW_ASYNC=0 is a
    // diagnostic escape hatch. Read once in the constructor.
    Q_PROPERTY(bool previewAsync READ previewAsync NOTIFY statusChanged)

    Q_PROPERTY(QString consoleIp READ consoleIp WRITE setConsoleIp NOTIFY settingsChanged)
    Q_PROPERTY(QString remotePlayConsole READ remotePlayConsole WRITE setRemotePlayConsole NOTIFY settingsChanged)
    Q_PROPERTY(QString xboxRemotePlayWindowTitle READ xboxRemotePlayWindowTitle WRITE setXboxRemotePlayWindowTitle NOTIFY settingsChanged)
    // [2026-09-21 beta] One-time "Xbox timing is untested" acknowledgement; gates Xbox session start.
    Q_PROPERTY(bool xboxUntestedAcknowledged READ xboxUntestedAcknowledged WRITE setXboxUntestedAcknowledged NOTIFY settingsChanged)
    Q_PROPERTY(bool streamSetupComplete READ streamSetupComplete NOTIFY settingsChanged)
    // First-run preflight wizard (capture card / Remote Play / test shot) finished or
    // skipped once. AppShell auto-opens the wizard while this is false.
    Q_PROPERTY(bool preflightComplete READ preflightComplete NOTIFY settingsChanged)
    Q_PROPERTY(bool legalAccepted READ legalAccepted NOTIFY settingsChanged)
    Q_PROPERTY(QString videoSource READ videoSource WRITE setVideoSource NOTIFY settingsChanged)
    Q_PROPERTY(int captureCardIndex READ captureCardIndex WRITE setCaptureCardIndex NOTIFY settingsChanged)
    Q_PROPERTY(bool captureCardSelected READ captureCardSelected NOTIFY captureDevicesChanged)
    // [ORION_CAPTURE_FPS 2026-09-14] Capture-card refresh rate. The setter SNAPS to {30, 60, 120}
    // (AppConfigData::snappedCaptureCardFps), so QML can only ever persist a rate the card can be
    // asked for. Reaches the sidecar as ORION_CAPTURE_FPS.
    Q_PROPERTY(int captureCardFps READ captureCardFps WRITE setCaptureCardFps NOTIFY settingsChanged)
    Q_PROPERTY(QStringList captureDeviceList READ captureDeviceList NOTIFY captureDevicesChanged)
    Q_PROPERTY(bool hardwareDecode READ hardwareDecode WRITE setHardwareDecode NOTIFY settingsChanged)
    Q_PROPERTY(QString controllerType READ controllerType WRITE setControllerType NOTIFY settingsChanged)
    Q_PROPERTY(bool autoReconnect READ autoReconnect WRITE setAutoReconnect NOTIFY settingsChanged)
    Q_PROPERTY(QString chiakiPath READ chiakiPath WRITE setChiakiPath NOTIFY settingsChanged)
    Q_PROPERTY(QString profileUser READ profileUser WRITE setProfileUser NOTIFY settingsChanged)
    Q_PROPERTY(QString pin READ pin WRITE setPin NOTIFY settingsChanged)
    Q_PROPERTY(QString redirectUrl READ redirectUrl WRITE setRedirectUrl NOTIFY settingsChanged)

    Q_PROPERTY(QString telemetryConsoleIp READ telemetryConsoleIp NOTIFY telemetryChanged)
    Q_PROPERTY(QString telemetryCourtIp READ telemetryCourtIp NOTIFY telemetryChanged)
    // Screen-safe form of the court endpoint. The court IP is a PUBLIC address:
    // users screenshot and stream this window, and a full public game-server
    // endpoint published that way is an invitation to flood the exact server the
    // user is playing on. The masked form keeps everything that is diagnostically
    // useful (family, provider block, region) and drops only the host identity.
    // The unmasked value stays in telemetryCourtIp and in the diagnostics bundle,
    // which the user exports deliberately rather than leaves on screen.
    Q_PROPERTY(QString telemetryCourtIpMasked READ telemetryCourtIpMasked NOTIFY telemetryChanged)
    // True when the displayed court endpoint is the RTT-verified one rather than
    // the passively observed candidate. These are different claims and the card
    // must not present them identically.
    Q_PROPERTY(bool courtIpVerified READ courtIpVerified NOTIFY telemetryChanged)
    Q_PROPERTY(QString telemetrySync READ telemetrySync NOTIFY telemetryChanged)
    Q_PROPERTY(int inboundPackets READ inboundPackets NOTIFY telemetryChanged)
    Q_PROPERTY(int outboundPackets READ outboundPackets NOTIFY telemetryChanged)
    Q_PROPERTY(double offsetMs READ offsetMs NOTIFY telemetryChanged)
    Q_PROPERTY(double syncAdjustMs READ syncAdjustMs NOTIFY telemetryChanged)
    Q_PROPERTY(double rttMs READ rttMs NOTIFY telemetryChanged)
    Q_PROPERTY(bool rttVerified READ rttVerified NOTIFY telemetryChanged)
    Q_PROPERTY(double jitterMs READ jitterMs NOTIFY telemetryChanged)
    Q_PROPERTY(int consecutivePackets READ consecutivePackets NOTIFY telemetryChanged)
    Q_PROPERTY(double packetIntervalMs READ packetIntervalMs NOTIFY telemetryChanged)
    Q_PROPERTY(int inboundConsecutive READ inboundConsecutive NOTIFY telemetryChanged)
    Q_PROPERTY(int outboundConsecutive READ outboundConsecutive NOTIFY telemetryChanged)
    Q_PROPERTY(double inboundIntervalMs READ inboundIntervalMs NOTIFY telemetryChanged)
    Q_PROPERTY(double outboundIntervalMs READ outboundIntervalMs NOTIFY telemetryChanged)
    Q_PROPERTY(double tickerLatencyMs READ tickerLatencyMs NOTIFY telemetryChanged)
    Q_PROPERTY(bool tickPhaseVerified READ tickPhaseVerified NOTIFY telemetryChanged)
    Q_PROPERTY(bool playingGame READ playingGame NOTIFY telemetryChanged)
    Q_PROPERTY(QString syncSource READ syncSource NOTIFY telemetryChanged)
    // Sidecar-reported capability probe: whether the No-Meter (pose) stack can actually run.
    // The toggle is persisted but its deps (torch/ultralytics + 3 weight files) are deliberately
    // NOT packaged, so enabling it used to fail silently into a swallowed except. QML shows a
    // banner off these. Default true so a sidecar that never reports behaves exactly as before.
    Q_PROPERTY(bool noMeterAvailable READ noMeterAvailable NOTIFY statusChanged)
    Q_PROPERTY(QString noMeterUnavailableReason READ noMeterUnavailableReason NOTIFY statusChanged)
    Q_PROPERTY(double syncConfidence READ syncConfidence NOTIFY telemetryChanged)
    // Passive court discovery is an explicit user opt-in because it may require
    // the local WinDivert service. It is display/diagnostic data only and never
    // becomes release-timing authority.
    Q_PROPERTY(bool packetCaptureEnabled READ packetCaptureEnabled WRITE setPacketCaptureEnabled NOTIFY settingsChanged)
    Q_PROPERTY(bool packetCaptureActive READ packetCaptureActive NOTIFY telemetryChanged)
    Q_PROPERTY(QString rttSyncMode READ rttSyncMode WRITE setRttSyncMode NOTIFY settingsChanged)
    Q_PROPERTY(QString streamBandwidthMode READ streamBandwidthMode WRITE setStreamBandwidthMode NOTIFY settingsChanged)
    Q_PROPERTY(bool streamAudioEnabled READ streamAudioEnabled WRITE setStreamAudioEnabled NOTIFY settingsChanged)
    Q_PROPERTY(QString streamAudioMode READ streamAudioMode WRITE setStreamAudioMode NOTIFY settingsChanged)
    Q_PROPERTY(bool audioToggleBusy READ audioToggleBusy NOTIFY statusChanged)
    Q_PROPERTY(bool controllerLightbarEnabled READ controllerLightbarEnabled WRITE setControllerLightbarEnabled NOTIFY settingsChanged)
    Q_PROPERTY(QString controllerLightbarColor READ controllerLightbarColor WRITE setControllerLightbarColor NOTIFY settingsChanged)
    Q_PROPERTY(QString controllerLightbarMode READ controllerLightbarMode WRITE setControllerLightbarMode NOTIFY settingsChanged)
    Q_PROPERTY(QString controllerLightbarPrimaryColor READ controllerLightbarPrimaryColor WRITE setControllerLightbarPrimaryColor NOTIFY settingsChanged)
    Q_PROPERTY(QString controllerLightbarSecondaryColor READ controllerLightbarSecondaryColor WRITE setControllerLightbarSecondaryColor NOTIFY settingsChanged)
    Q_PROPERTY(double controllerLightbarBrightness READ controllerLightbarBrightness WRITE setControllerLightbarBrightness NOTIFY settingsChanged)
    Q_PROPERTY(double controllerLightbarEffectSpeed READ controllerLightbarEffectSpeed WRITE setControllerLightbarEffectSpeed NOTIFY settingsChanged)
    Q_PROPERTY(double manualSyncAdjustMs READ manualSyncAdjustMs WRITE setManualSyncAdjustMs NOTIFY settingsChanged)
    Q_PROPERTY(double manualOffsetMs READ manualOffsetMs WRITE setManualOffsetMs NOTIFY settingsChanged)
    Q_PROPERTY(double effectiveSyncAdjustMs READ effectiveSyncAdjustMs NOTIFY telemetryChanged)
    // The EXACT millisecond value most recently handed to
    // AutomationEngine::updateNetworkQuality — i.e. the network compensation the
    // release scheduler is actually carrying, fallbacks and all. This is
    // deliberately not effectiveSyncAdjustMs: that one skips the manual-offset
    // and unverified-RTT fallbacks, so a UI reading "applied" off it would be
    // wrong in exactly the cases a user is trying to debug.
    Q_PROPERTY(double appliedNetworkOffsetMs READ appliedNetworkOffsetMs NOTIFY telemetryChanged)
    Q_PROPERTY(double effectiveOffsetMs READ effectiveOffsetMs NOTIFY telemetryChanged)

    Q_PROPERTY(double timingLatencyMs READ timingLatencyMs WRITE setTimingLatencyMs NOTIFY settingsChanged)
    Q_PROPERTY(double releaseThresholdPct READ releaseThresholdPct WRITE setReleaseThresholdPct NOTIFY settingsChanged)
    Q_PROPERTY(double earlyLateOffsetMs READ earlyLateOffsetMs WRITE setEarlyLateOffsetMs NOTIFY settingsChanged)
    Q_PROPERTY(QString tempoInputSource READ tempoInputSource WRITE setTempoInputSource NOTIFY settingsChanged)
    Q_PROPERTY(QString tempoRemapType READ tempoRemapType WRITE setTempoRemapType NOTIFY settingsChanged)
    Q_PROPERTY(double tempoWaitMs READ tempoWaitMs WRITE setTempoWaitMs NOTIFY settingsChanged)
    // [ORION_RHYTHM_FLICK_DELAY 2026-09-14] Rhythm flick trim, -50..+50 ms. POSITIVE fires the
    // right-stick flick LATER, negative earlier, and it applies ONLY to a shot whose release will
    // actually be the flick (AutomationEngine::rhythmFlickReleasePending()). A trim on top of
    // Shot Lead, never a lead of its own.
    Q_PROPERTY(double rhythmFlickDelayMs READ rhythmFlickDelayMs WRITE setRhythmFlickDelayMs NOTIFY settingsChanged)
    Q_PROPERTY(double tempoFallbackMs READ tempoFallbackMs WRITE setTempoFallbackMs NOTIFY settingsChanged)
    Q_PROPERTY(double tempoFlickHoldMs READ tempoFlickHoldMs WRITE setTempoFlickHoldMs NOTIFY settingsChanged)
    Q_PROPERTY(double tempoMinStickHoldMs READ tempoMinStickHoldMs WRITE setTempoMinStickHoldMs NOTIFY settingsChanged)
    // [ORION_TEMPO_RELEASE_STYLE 2026-09-15 owner] Rhythm's release EDGE: "flick" (the opposing
    // full-scale RS deflection every build before this shipped) or "letgo" (the stick driven to
    // neutral at the same instant, so the player keeps holding down and Venice lets go for them).
    // Ignore-unknown on the way in: anything else leaves the current style alone.
    Q_PROPERTY(QString tempoReleaseStyle READ tempoReleaseStyle WRITE setTempoReleaseStyle NOTIFY settingsChanged)

    Q_PROPERTY(QString shotState READ shotState NOTIFY statusChanged)
    Q_PROPERTY(QString activeBotShotType READ activeBotShotType NOTIFY statusChanged)
    // Per-type calibration state for the launcher Calibrate/Lock panel (JSON: phase/locked/clock/greens).
    Q_PROPERTY(QString calibrationSnapshot READ calibrationSnapshot NOTIFY statusChanged)
    // Live HUD values are accepted raw detector measurements only. The engine's
    // ShotContext may legitimately coast/extrapolate through a short occlusion for
    // timing, but the UI must never present that estimate as a camera measurement.
    Q_PROPERTY(bool meterMetricsValid READ meterMetricsValid NOTIFY meterMetricsChanged)
    Q_PROPERTY(double shotFillPct READ shotFillPct NOTIFY meterMetricsChanged)
    Q_PROPERTY(double shotConfidence READ shotConfidence NOTIFY meterMetricsChanged)
    Q_PROPERTY(double shotVelocityPctS READ shotVelocityPctS NOTIFY meterMetricsChanged)
    Q_PROPERTY(double shotHoldMs READ shotHoldMs NOTIFY meterMetricsChanged)
    Q_PROPERTY(double shotEtaMs READ shotEtaMs NOTIFY meterMetricsChanged)
    Q_PROPERTY(double shotEtaToTargetMs READ shotEtaToTargetMs NOTIFY meterMetricsChanged)
    Q_PROPERTY(double botTargetPct READ botTargetPct NOTIFY meterMetricsChanged)
    Q_PROPERTY(double botReleaseLeadMs READ botReleaseLeadMs NOTIFY statusChanged)
    Q_PROPERTY(QString botDecision READ botDecision NOTIFY statusChanged)
    Q_PROPERTY(QString botReleaseReason READ botReleaseReason NOTIFY statusChanged)
    Q_PROPERTY(QString meterProfile READ meterProfile NOTIFY statusChanged)
    Q_PROPERTY(QString meterRejectionReason READ meterRejectionReason NOTIFY statusChanged)
    Q_PROPERTY(QString meterSearchBox READ meterSearchBox NOTIFY statusChanged)
    Q_PROPERTY(QString meterBodyBox READ meterBodyBox NOTIFY statusChanged)
    Q_PROPERTY(QString meterRejectedBox READ meterRejectedBox NOTIFY statusChanged)
    Q_PROPERTY(QString meterFillLine READ meterFillLine NOTIFY meterMetricsChanged)
    Q_PROPERTY(QString meterTargetLine READ meterTargetLine NOTIFY meterMetricsChanged)
    // ===== Live meter-side telemetry HUD (overlay beside the detected meter) =====
    // One batched snapshot, refreshed on the 4 ms input tick and NOTIFIED at most
    // 30 Hz. The lines are pre-formatted in C++ and only rebuilt when the DIGITS
    // that would be rendered actually change, so a steady value costs the QML
    // scene graph nothing: no JS number formatting, no Text relayout, no object
    // churn beside the 60 fps preview. Never bind these to a per-tick signal.
    //
    // The overlay is ALWAYS ON (no user toggle), so this set is deliberately
    // small and fixed: the eye (FILL), the prediction (TIP), the actuation
    // deadline (FIRE), and — 2026-08-06, owner request — the route (COURT) and
    // the link quality (JITTER). The lead is not a row of its own because it is
    // exactly TIP - FIRE and is therefore already on screen. Adding rows here
    // adds steady-state GUI-thread work beside the 60 fps preview for every user,
    // so each one must be an owner-level decision, not a convenience.
    //
    // PERSISTENCE CONTRACT (2026-08-04): the readout is a fixture, not a popup.
    // Every line string is ALWAYS non-empty — when a value cannot be proven the
    // native side publishes the neutral placeholder "--" instead of an empty
    // string, so no row ever collapses and the box never changes size or
    // disappears between shots. `meterHudLive` says whether the TIMING digits
    // are a current measurement (tone/dim only); it is NOT a visibility gate.
    //
    // TWO GATES, NOT ONE (2026-08-04 "the box shows no live values"). The rows
    // do not all become provable at the same instant, so they no longer share a
    // gate:
    //   FILL is a CAMERA MEASUREMENT. It exists for exactly as long as the
    //        detector has a fresh genuine sample — i.e. exactly as long as the
    //        page draws the lock box (`meterHudMeasured`).
    //   TIP/FIRE are ENGINE PREDICTIONS stamped by processHolding. They exist
    //        only while the bot owns a live shot (`meterHudLive`) and are never
    //        synthesised, extrapolated or held over from the previous shot.
    //        FIRE additionally prefers the ARMED precise-fire schedule
    //        (scheduledFireDeadlineMs) whenever a token is armed, so an armed
    //        countdown is never shown as "--"; it dashes only when genuinely
    //        nothing is armed and no owned-shot deadline exists.
    // Gating FILL on ownership too was the defect: measured live, the bot owns
    // ~180 ms of a ~2500 ms meter, so all three rows read "--" for ~93% of the
    // time the box was on screen. FILL now fills that whole window and TIP/FIRE
    // stay honestly blank outside the shot.
    //
    // These lines carry the VALUE ONLY ("62%", "148ms"). The FILL/TIP/FIRE
    // captions are static Text in QML: they never change, so re-publishing them
    // in every string was pure per-update allocation and relayout.
    Q_PROPERTY(bool meterHudLive READ meterHudLive NOTIFY meterHudChanged)
    // True while FILL is a current camera measurement. Tone only: it is what
    // takes the box out of its dimmed resting state, so a live fill is never
    // presented at the opacity that means "nothing here is being proven".
    Q_PROPERTY(bool meterHudMeasured READ meterHudMeasured NOTIFY meterHudChanged)
    Q_PROPERTY(QString meterHudFillLine READ meterHudFillLine NOTIFY meterHudChanged)
    Q_PROPERTY(QString meterHudTipLine READ meterHudTipLine NOTIFY meterHudChanged)
    Q_PROPERTY(QString meterHudFireLine READ meterHudFireLine NOTIFY meterHudChanged)
    // meterHudCourtLine / meterHudJitterLine REMOVED 2026-08-06 (owner: the HUD
    // is three values — the ones the bot uses — "and make it actually LIVE and
    // not static"). They were added earlier the same day; being two rows that
    // essentially never changed, they were most of the static feel. The RTT
    // machinery they displayed (sidecar sampler, NetworkBridge, telemetry_)
    // is untouched — this deleted only the HUD-side mirror and formatting.
    // Tone-only companion, evaluated in C++ so QML never re-derives it.
    // commandLate == the fireAt deadline is already in the past (the exact
    // 2026-08-03 failure).
    Q_PROPERTY(bool meterHudCommandLate READ meterHudCommandLate NOTIFY meterHudChanged)

    // ---- detection overlay appearance (PRESENTATION ONLY) -------------------
    // User-chosen skin for the lock box drawn over the preview. None of these
    // reach the detector, the scheduler, or any release decision — they are read
    // exclusively by QML while painting an overlay on top of an already-decoded
    // frame, so nothing here can move a shot by a microsecond.
    //
    // `meterOverlayColor` / `meterOverlayStyle` / `meterOverlayRgb` are the
    // persisted user choice. `meterOverlayDrawColor` is what QML actually binds
    // the stroke to: it is the chosen colour normally, and the current step of
    // the hue cycle while RGB mode is on. Splitting them means QML never has to
    // ask "is RGB on?" and never runs colour maths of its own.
    Q_PROPERTY(QString meterOverlayColor READ meterOverlayColor
                   WRITE setMeterOverlayColor NOTIFY settingsChanged)
    Q_PROPERTY(QString meterOverlayStyle READ meterOverlayStyle
                   WRITE setMeterOverlayStyle NOTIFY settingsChanged)
    Q_PROPERTY(bool meterOverlayRgb READ meterOverlayRgb
                   WRITE setMeterOverlayRgb NOTIFY settingsChanged)
    // Separate notify signal on purpose. In RGB mode this changes ~4x/second;
    // routing it through settingsChanged would drag every unrelated settings
    // binding in the app along with it four times a second.
    Q_PROPERTY(QString meterOverlayDrawColor READ meterOverlayDrawColor
                   NOTIFY meterOverlayDrawColorChanged)
    Q_PROPERTY(int requestedStreamFps READ requestedStreamFps NOTIFY statusChanged)
    Q_PROPERTY(int captureLoopFps READ captureLoopFps NOTIFY statusChanged)
    Q_PROPERTY(int uniqueFrameFps READ uniqueFrameFps NOTIFY statusChanged)
    Q_PROPERTY(int previewLagFrames READ previewLagFrames NOTIFY statusChanged)
    Q_PROPERTY(double duplicateFramePct READ duplicateFramePct NOTIFY statusChanged)
    Q_PROPERTY(double frameAgeMs READ frameAgeMs NOTIFY statusChanged)
    Q_PROPERTY(QString captureResolution READ captureResolution NOTIFY statusChanged)
    Q_PROPERTY(QString fpsProbeState READ fpsProbeState NOTIFY statusChanged)
    Q_PROPERTY(int meterSearchBoxX READ meterSearchBoxX NOTIFY statusChanged)
    Q_PROPERTY(int meterSearchBoxY READ meterSearchBoxY NOTIFY statusChanged)
    Q_PROPERTY(int meterSearchBoxWidth READ meterSearchBoxWidth NOTIFY statusChanged)
    Q_PROPERTY(int meterSearchBoxHeight READ meterSearchBoxHeight NOTIFY statusChanged)
    Q_PROPERTY(int meterBoxX READ meterBoxX NOTIFY meterBoxChanged)
    Q_PROPERTY(int meterBoxY READ meterBoxY NOTIFY meterBoxChanged)
    Q_PROPERTY(int meterBoxWidth READ meterBoxWidth NOTIFY meterBoxChanged)
    Q_PROPERTY(int meterBoxHeight READ meterBoxHeight NOTIFY meterBoxChanged)
    Q_PROPERTY(bool isShooting READ isShooting NOTIFY statusChanged)
    // Overlay lock gate, driven PURELY by the detector (not the held button). True only while a
    // FRESH, REAL meter detection is on screen; replaces isShooting as the neon-box visibility gate.
    Q_PROPERTY(bool meterConfirmed READ meterConfirmed NOTIFY meterBoxChanged)
    // METER-BLIND SAFETY NET. True after MeterBlindnessLatch::kTripPresses consecutive completed
    // METER presses got NO meter evidence of any kind. Its whole purpose is to catch a
    // MISCONFIGURED or PATCHED meter, so it deliberately derives from NO detector-health field: a
    // detector that cannot see the meter reports meter_present=false perfectly truthfully,
    // forever, and every self-reported health signal agrees with it. Only a CV-INDEPENDENT
    // trigger can raise this: the engine's per-epoch press_unanswered_no_meter terminal (the
    // physical controller edge the meter never answered). [RT-MED-04 2026-09-23] Only an
    // IN-PRESS raw detection resets the streak; the latched state clears after vision-owned shots.
    Q_PROPERTY(bool meterBlindWarning READ meterBlindWarning NOTIFY meterBlindChanged)
    Q_PROPERTY(QString meterBlindHint READ meterBlindHint NOTIFY meterBlindChanged)
    // Reader detector health for the Meter Detection card (PRESENTATION ONLY). Fed by the
    // sidecar's {"event":"detector_health"} line every ~2 s; formatted here into one compact
    // mono line ("Pure CV · 1.2 ms · locked · locks 12 · refused 3") plus the raw provider
    // id. Own low-fanout signal: it moves every couple of seconds and has one subscriber.
    Q_PROPERTY(QString detectorHealthLine READ detectorHealthLine NOTIFY detectorHealthChanged)
    Q_PROPERTY(QString detectorProvider READ detectorProvider NOTIFY detectorHealthChanged)
    Q_PROPERTY(QString shotMode READ shotMode NOTIFY statusChanged)
    Q_PROPERTY(int meterRejectedBoxX READ meterRejectedBoxX NOTIFY meterBoxChanged)
    Q_PROPERTY(int meterRejectedBoxY READ meterRejectedBoxY NOTIFY meterBoxChanged)
    Q_PROPERTY(int meterRejectedBoxWidth READ meterRejectedBoxWidth NOTIFY meterBoxChanged)
    Q_PROPERTY(int meterRejectedBoxHeight READ meterRejectedBoxHeight NOTIFY meterBoxChanged)
    Q_PROPERTY(int liveFrameWidth READ liveFrameWidth NOTIFY frameChanged)
    Q_PROPERTY(int liveFrameHeight READ liveFrameHeight NOTIFY frameChanged)
    Q_PROPERTY(QVariantList poseKeypoints READ poseKeypoints NOTIFY poseOverlayChanged)
    Q_PROPERTY(QVariantList poseBox READ poseBox NOTIFY poseOverlayChanged)
    // Camera-anchor overlay points (full-frame px). poseIndicator is empty
    // when the under-player marker is not detected this frame.
    Q_PROPERTY(QVariantList poseAnchor READ poseAnchor NOTIFY poseOverlayChanged)
    Q_PROPERTY(QVariantList poseLockCenter READ poseLockCenter NOTIFY poseOverlayChanged)
    Q_PROPERTY(QVariantList poseIndicator READ poseIndicator NOTIFY poseOverlayChanged)
    Q_PROPERTY(int shotsAttempted READ shotsAttempted NOTIFY statusChanged)
    Q_PROPERTY(int shotsReleased READ shotsReleased NOTIFY statusChanged)
    Q_PROPERTY(int shotsAborted READ shotsAborted NOTIFY statusChanged)
    // Session verdict tally (graded shots only) for the Live Shot green-rate readout.
    Q_PROPERTY(int sessionVerdicts READ sessionVerdicts NOTIFY statusChanged)
    Q_PROPERTY(int sessionGreens READ sessionGreens NOTIFY statusChanged)
    // User-entered game results, not inferred greens or a calibration input.
    Q_PROPERTY(int manualShotMakes READ manualShotMakes NOTIFY manualShotTallyChanged)
    Q_PROPERTY(int manualShotMisses READ manualShotMisses NOTIFY manualShotTallyChanged)
    Q_PROPERTY(int manualShotTotal READ manualShotTotal NOTIFY manualShotTallyChanged)
    // ═══════════════════════════════════════════════════════════════════════
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] The live shot-verdict tally, read off the
    // GAME'S OWN feedback banner by the sidecar (RemotePlaySession::bannerVerdict) and
    // rolled over the LAST 10 shots by ShotVerdictTally. This is the answer to "can't tell
    // if I found my value or not because sometimes it's green": the owner does not have to
    // count banners any more, and the suggestion line names the direction to move.
    //
    // PRESENTATION ONLY: nothing here reaches the engine, the telemetry snapshot or any
    // timing path. The window is auto-cleared on every committed Release timing / fade trim
    // / Shot Lead change, so the tally always describes the CURRENT value.
    //
    // Its own low-fanout notifier (NOT statusChanged): it changes about once per shot and
    // has exactly two subscribers, the ShotVerdictTally mounts on NoMeterCard and ShotLeadCard.
    Q_PROPERTY(int bannerGreen10 READ bannerGreen10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(int bannerEarly10 READ bannerEarly10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(int bannerLate10 READ bannerLate10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(int bannerOther10 READ bannerOther10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(int bannerCount10 READ bannerCount10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(int bannerContested10 READ bannerContested10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(QString bannerLastTiming READ bannerLastTiming NOTIFY bannerTallyChanged)
    Q_PROPERTY(QString bannerLastCoverage READ bannerLastCoverage NOTIFY bannerTallyChanged)
    // One char per verdict, oldest -> newest (g/e/l/o) for the 10-dot strip.
    Q_PROPERTY(QString bannerPattern10 READ bannerPattern10 NOTIFY bannerTallyChanged)
    Q_PROPERTY(QString bannerSuggestion READ bannerSuggestion NOTIFY bannerTallyChanged)
    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] The bounded closed loop's own two properties.
    // `bannerLeadTrimMs` is the additive term the banner loop has earned for the CURRENT display
    // bucket (Standstill -- the reference type, and the one the owner tunes on), in ms; the card
    // shows one caption under the slider and hides it at 0. Read-only by construction: this is
    // EVIDENCE, and the owner's own slider value is never touched by it.
    Q_PROPERTY(double bannerLeadTrimMs READ bannerLeadTrimMs NOTIFY bannerLeadTrimChanged)
    Q_PROPERTY(bool bannerLeadTrimEnabled READ bannerLeadTrimEnabled
                   NOTIFY bannerLeadTrimChanged)
    // [ORION_LEAD_AUTO_SEED 2026-09-15 owner] The plug-and-play Shot Lead, for the ONE caption
    // under the slider on an install nobody has tuned. `leadAutoSeedActive` is false the moment
    // the owner sets a value of their own (the caption disappears and their number rules);
    // `leadAutoSeedKind` is "measured" once this rig's latency estimator is authoritative and
    // "placeholder" until then, which is the difference between "measured latency 208 ms + 69 ms
    // margin" and "calibrating...". Read-only by construction: the seed is DERIVED, and
    // actuation_lead_ms is never written by it.
    Q_PROPERTY(bool leadAutoSeedActive READ leadAutoSeedActive NOTIFY leadAutoSeedChanged)
    Q_PROPERTY(double leadAutoSeedMs READ leadAutoSeedMs NOTIFY leadAutoSeedChanged)
    Q_PROPERTY(QString leadAutoSeedKind READ leadAutoSeedKind NOTIFY leadAutoSeedChanged)
    Q_PROPERTY(double leadAutoSeedMeasuredMs READ leadAutoSeedMeasuredMs
                   NOTIFY leadAutoSeedChanged)
    Q_PROPERTY(double leadAutoSeedMarginMs READ leadAutoSeedMarginMs
                   NOTIFY leadAutoSeedChanged)
    Q_PROPERTY(bool wifiModeActive READ wifiModeActive NOTIFY telemetryChanged)
    // Defense Mode: physical-pad toggle disarms shot automation (pass-through).
    Q_PROPERTY(bool defenseModeActive READ defenseModeActive NOTIFY statusChanged)
    Q_PROPERTY(QString defenseTriggerButton READ defenseTriggerButton WRITE setDefenseTriggerButton NOTIFY settingsChanged)
    Q_PROPERTY(bool defenseStickAssist READ defenseStickAssist WRITE setDefenseStickAssist NOTIFY settingsChanged)
    Q_PROPERTY(double defenseStickAssistStrength READ defenseStickAssistStrength WRITE setDefenseStickAssistStrength NOTIFY settingsChanged)
    Q_PROPERTY(bool defenseL2HoldAssist READ defenseL2HoldAssist WRITE setDefenseL2HoldAssist NOTIFY settingsChanged)
    Q_PROPERTY(bool defenseSprintAssist READ defenseSprintAssist WRITE setDefenseSprintAssist NOTIFY settingsChanged)
    Q_PROPERTY(QString defenseLightbarColor READ defenseLightbarColor WRITE setDefenseLightbarColor NOTIFY settingsChanged)
    // Per-jumpshot profiles: active name + a JSON snapshot for the picker UI.
    Q_PROPERTY(QString activeProfile READ activeProfile NOTIFY settingsChanged)
    Q_PROPERTY(QString profilesSnapshot READ profilesSnapshot NOTIFY settingsChanged)
    // Safe mode: a repeated watchdog failure disarms automation until the user
    // explicitly exits (or restarts the app). Never re-arms on its own.
    Q_PROPERTY(bool safeModeActive READ safeModeActive NOTIFY statusChanged)
    Q_PROPERTY(QString safeModeReason READ safeModeReason NOTIFY statusChanged)
    Q_PROPERTY(QString logText READ logText NOTIFY logsChanged)
    // Customer Activity feed (engineering telemetry removed). Shares logsChanged:
    // both rings are published on the same 200 ms flush beat.
    Q_PROPERTY(QString activityText READ activityText NOTIFY logsChanged)
    Q_PROPERTY(bool coreActive READ coreActive NOTIFY statusChanged)
    Q_PROPERTY(QString scriptState READ scriptState NOTIFY statusChanged)
    Q_PROPERTY(QString scriptDetail READ scriptDetail NOTIFY statusChanged)
    Q_PROPERTY(QString licenseDetail READ licenseDetail NOTIFY statusChanged)
    Q_PROPERTY(QString timeLeft READ timeLeft NOTIFY statusChanged)
    Q_PROPERTY(QString timeLeftDetail READ timeLeftDetail NOTIFY statusChanged)
    Q_PROPERTY(QString meterRuntimeState READ meterRuntimeState NOTIFY statusChanged)
    Q_PROPERTY(QString holdSource READ holdSource NOTIFY statusChanged)
    Q_PROPERTY(QString lastResult READ lastResult NOTIFY statusChanged)
    Q_PROPERTY(QString outputState READ outputState NOTIFY statusChanged)
    // The one-second clock has its own low-fanout notifier. Sharing the broad
    // statusChanged signal made every status binding re-evaluate once a second
    // even when the underlying runtime state was unchanged.
    Q_PROPERTY(QString sessionTime READ sessionTime NOTIFY sessionTimeChanged)
    Q_PROPERTY(QString blockReason READ blockReason NOTIFY statusChanged)
    Q_PROPERTY(QString statusAge READ statusAge NOTIFY statusAgeChanged)
    Q_PROPERTY(QString netSyncReason READ netSyncReason NOTIFY statusChanged)
    Q_PROPERTY(QString meterStyle READ meterStyle WRITE setMeterStyle NOTIFY settingsChanged)
    // Meter box proposer ("cv" | "yolo") — the Meter Detection card's Detector combo.
    // Exported to the sidecar as ORION_METER_PROPOSER at launch; a change applies to
    // the next sidecar launch (the locator singleton reads it once).
    Q_PROPERTY(QString meterProposer READ meterProposer WRITE setMeterProposer NOTIFY settingsChanged)
    Q_PROPERTY(QString meterColor READ meterColor WRITE setMeterColor NOTIFY settingsChanged)
    Q_PROPERTY(bool meterEnabled READ meterEnabled WRITE setMeterEnabled NOTIFY settingsChanged)
    Q_PROPERTY(bool autoMeterColor READ autoMeterColor WRITE setAutoMeterColor NOTIFY settingsChanged)
    Q_PROPERTY(int detectionConfidencePercent READ detectionConfidencePercent WRITE setDetectionConfidencePercent NOTIFY settingsChanged)
    // Shoot-to-train calibration (no boxing): the user shoots N times while the detector
    // auto-learns the meter colour + green hue. These drive the calibration overlay UI.
    Q_PROPERTY(bool meterCalibrating READ meterCalibrating NOTIFY statusChanged)
    Q_PROPERTY(int meterCalibrationShots READ meterCalibrationShots NOTIFY statusChanged)
    Q_PROPERTY(int meterCalibrationTarget READ meterCalibrationTarget NOTIFY statusChanged)
    Q_PROPERTY(QString meterCalibrationStatus READ meterCalibrationStatus NOTIFY statusChanged)
    // [Track B / B3] ColorCalibrator lifecycle state fed from the sidecar's
    // calibrate_meter_status events: "factory"(seed) | "learning" | "locked" |
    // "provisional" | "relearn" + the date the bands were baked. Drives the
    // MeterConfigPanel state badge (Factory / Learning k/5 / Calibrated / Re-checking).
    Q_PROPERTY(QString meterCalibrationState READ meterCalibrationState NOTIFY statusChanged)
    Q_PROPERTY(QString meterCalibrationLearnedDate READ meterCalibrationLearnedDate NOTIFY statusChanged)
    // Timing-latency calibration is deliberately separate from meter colour training. It
    // exposes only measured estimator state and never grants timing authority by itself.
    Q_PROPERTY(bool latencyCalibrationActive READ latencyCalibrationActive NOTIFY latencyCalibrationChanged)
    Q_PROPERTY(bool latencyCalibrationReady READ latencyCalibrationReady NOTIFY latencyCalibrationChanged)
    Q_PROPERTY(int latencyCalibrationSamples READ latencyCalibrationSamples NOTIFY latencyCalibrationChanged)
    Q_PROPERTY(int latencyCalibrationTarget READ latencyCalibrationTarget CONSTANT)
    Q_PROPERTY(double latencyCalibrationMeasuredLeadMs READ latencyCalibrationMeasuredLeadMs NOTIFY latencyCalibrationChanged)
    Q_PROPERTY(double latencyCalibrationSdMs READ latencyCalibrationSdMs NOTIFY latencyCalibrationChanged)
    Q_PROPERTY(QString latencyCalibrationStatus READ latencyCalibrationStatus NOTIFY latencyCalibrationChanged)
    // Auto = the per-type clock self-tunes from the post-release on-screen meter grade
    // (learning ON); Manual = locked to the slider values (frozen). Inverse of freezeCalibration.
    Q_PROPERTY(bool autoTune READ autoTune WRITE setAutoTune NOTIFY settingsChanged)
    Q_PROPERTY(bool tempoEnabled READ tempoEnabled WRITE setTempoEnabled NOTIFY settingsChanged)
    Q_PROPERTY(QString tempoInputPath READ tempoInputPath WRITE setTempoInputPath NOTIFY settingsChanged)
    Q_PROPERTY(bool noDipEnabled READ noDipEnabled WRITE setNoDipEnabled NOTIFY settingsChanged)
    Q_PROPERTY(double noDipLeadMs READ noDipLeadMs WRITE setNoDipLeadMs NOTIFY settingsChanged)
    // [ORION_USER_LEAD] Shot Lead — the single user-facing timing control. actuationLeadMs is the
    // persisted setting (0 = never configured; writing it marks it user-set for good).
    // actuationLeadMeasuredMs/-Samples are this install's OWN measured end-to-end lead, read off
    // where releases landed; they are evidence shown next to the control, never applied behind it.
    Q_PROPERTY(double actuationLeadMs READ actuationLeadMs WRITE setActuationLeadMs NOTIFY settingsChanged)
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Shot Lead offset for the DELAYED condition —
    // added to actuationLeadMs only while the meter delay is actually applied. 0 (the default)
    // reproduces every build before this property existed. It exists because Meter Delay changes
    // the console's response to the release while leaving the visible meter alone, so a single
    // Shot Lead cannot be right in both conditions — and the condition toggles mid-session under
    // OffenseDefense mode and the D-Pad-Up bypass, so it cannot be a value the user re-types.
    // meterDelayLeadOffsetMaxMs is the largest offset THIS rig can still schedule (the
    // visible-evidence ceiling minus the delay-0 lead); above it every live tip shot aborts.
    Q_PROPERTY(double meterDelayLeadOffsetMs READ meterDelayLeadOffsetMs
                   WRITE setMeterDelayLeadOffsetMs NOTIFY settingsChanged)
    Q_PROPERTY(double meterDelayLeadOffsetMaxMs READ meterDelayLeadOffsetMaxMs
                   NOTIFY settingsChanged)
    // [ORION_METER_DELAY_LEAD_AUTO 2026-09-10] The delay the current Shot Lead can absorb while
    // tip shots stay schedulable (ceiling - lead), and the offset the engine is adding right
    // now (0 = auto follows the applied delay). Presentation only.
    Q_PROPERTY(double meterDelayMaxUsableMs READ meterDelayMaxUsableMs NOTIFY settingsChanged)
    Q_PROPERTY(double meterDelayLeadOffsetAppliedMs READ meterDelayLeadOffsetAppliedMs
                   NOTIFY meterDelayStatusTextChanged)
    // [ORION_LEAD_CALIBRATION] Guided lead calibration. Everything else in the timing stack
    // self-tunes -- the animation constant is learned per jumpshot and persisted -- but the LEAD
    // is this machine's capture-to-screen latency, and capture hardware varies enormously between
    // users (the owner's card is ~160ms of their ~300ms loop). It is the one number with the
    // largest per-machine spread and no way for a user to find it. Without this, a new user's
    // first experience is a bot mistiming every shot with no path to fixing it.
    Q_PROPERTY(bool leadCalibrationActive READ leadCalibrationActive NOTIFY leadCalibrationChanged)
    Q_PROPERTY(bool leadCalibrationLocked READ leadCalibrationLocked NOTIFY leadCalibrationChanged)
    Q_PROPERTY(int leadCalibrationShots READ leadCalibrationShots NOTIFY leadCalibrationChanged)
    Q_PROPERTY(double leadCalibrationLeadMs READ leadCalibrationLeadMs NOTIFY leadCalibrationChanged)
    Q_PROPERTY(double leadCalibrationStepMs READ leadCalibrationStepMs NOTIFY leadCalibrationChanged)
    Q_PROPERTY(QString leadCalibrationHint READ leadCalibrationHint NOTIFY leadCalibrationChanged)
    Q_PROPERTY(bool actuationLeadUserSet READ actuationLeadUserSet NOTIFY settingsChanged)
    Q_PROPERTY(double actuationLeadMinMs READ actuationLeadMinMs CONSTANT)
    Q_PROPERTY(double actuationLeadMaxMs READ actuationLeadMaxMs CONSTANT)
    Q_PROPERTY(double actuationLeadMeasuredMs READ actuationLeadMeasuredMs NOTIFY actuationLeadMeasurementChanged)
    Q_PROPERTY(int actuationLeadMeasuredSamples READ actuationLeadMeasuredSamples NOTIFY actuationLeadMeasurementChanged)
    // [ORION_USER_TIP] Tip Timing — the SECOND user-facing timing control, and the two must
    // never be conflated:
    //   * Shot Lead (above) is the RIG: press -> visible effect latency. Per-machine.
    //   * Tip Timing is the GAME: the equipped jumpshot's animation length (the aim). A
    //     customer with the same jumpshot needs the same value; a different jumpshot needs a
    //     different one, which is why a learner exists for it and not for the lead.
    //
    // `tipTimingMs` is the EFFECTIVE aim in ms — the exact quantity the decision path
    // consumes (effectiveTipPhaseConstantMs; pickup prompt §9.5: never the shipped constant).
    // Derived per read from the PERSISTED learner prior + the LIVE engine constants via the
    // shared frame helpers in AppConfig.h, so the displayed number is the value the next
    // restart runs and the value a locked session holds. While the learner is unlocked and
    // mid-window, the engine's in-memory value can lead this by a few ms until the next
    // full-window persist — that persist re-notifies through tipTimingChanged, so the card
    // tracks the learner at exactly the cadence the learner publishes itself.
    //
    // WRITE = the user chose a value: it is clamped into the learner's own plausibility band,
    // converted to the canonical base-30 physical frame, stored in the learner's persisted
    // slot, and LOCKED (tip_phase_aim_frozen) — a manual value outranks the learner while
    // locked, survives restart, and Reset/unlock hands control back to the learner (which
    // resumes adapting from the manual value rather than jumping — no aim cliff on reset).
    Q_PROPERTY(double tipTimingMs READ tipTimingMs WRITE setTipTimingMs NOTIFY tipTimingChanged)
    Q_PROPERTY(bool tipTimingUserSet READ tipTimingUserSet NOTIFY tipTimingChanged)
    Q_PROPERTY(bool tipTimingLocked READ tipTimingLocked WRITE setTipTimingLocked NOTIFY tipTimingChanged)
    // True while a learned/manual prior exists at all (learning.json carries one); false on a
    // fresh install still running the factory seed. Presentation only.
    Q_PROPERTY(bool tipTimingLearnedActive READ tipTimingLearnedActive NOTIFY tipTimingChanged)
    // The learner's plausibility band expressed in the SAME effective frame as tipTimingMs.
    // NOT CONSTANT: the band shifts with tip_phase_anchor_base20 (the whole constellation
    // moves by +58.3ms), so these re-derive from the live engine config on every notify.
    Q_PROPERTY(double tipTimingMinMs READ tipTimingMinMs NOTIFY tipTimingChanged)
    Q_PROPERTY(double tipTimingMaxMs READ tipTimingMaxMs NOTIFY tipTimingChanged)
    // [ORION_LEAD_CONFLICT 2026-08-08] The Shot Lead x Tip Timing trap, made visible BEFORE the
    // user falls into it. A lead above (tip timing - 30ms scheduling margin) can NEVER be
    // scheduled by the tip-phase path: the command deadline is already behind `now` on the first
    // tick it exists, so every live tip shot aborts live_tip_deadline_missed — the exact
    // 2026-08-08 lost-session signature (Tip Timing 314ms + Shot Lead 320ms, raised further
    // after each bogus LATE). shotLeadMaxUsableMs is the largest schedulable lead under the
    // ACTIVE tip constant, derived from the SAME effective frame the Tip Timing card displays
    // (tipTimingMs), so the two cards can annotate and warn reactively: the banner appears the
    // moment the PAIR conflicts and clears the moment either control fixes it. ADVISORY ONLY —
    // both values are user-set, nothing clamps them, and a silently re-timed release would be
    // worse than the honest abort the engine already performs.
    Q_PROPERTY(double shotLeadMaxUsableMs READ shotLeadMaxUsableMs NOTIFY tipTimingChanged)
    // Shots aborted live_tip_deadline_missed while the conflict held (engine-counted, per
    // session). Feeds the ShotLeadCard conflict banner's "N shots aborted" clause.
    Q_PROPERTY(int shotLeadConflictMisses READ shotLeadConflictMisses NOTIFY leadDiagnosticsChanged)
    // [ORION_LEAD_CONFLICT] The VALIDATED latency posterior last diagnosed as disagreeing with
    // the in-band Shot Lead by more than 3*sd (leadAuthorityDisagreementDiagnosed). -1/-1/0 =
    // never diagnosed. The ShotLeadCard recomputes the 3*sd test reactively against the live
    // slider value, so moving the lead back inside the band hides the badge without waiting for
    // the engine to re-emit. Advisory: nothing adopts the measured value on the user's behalf.
    Q_PROPERTY(double leadAuthorityMs READ leadAuthorityMs NOTIFY leadDiagnosticsChanged)
    Q_PROPERTY(double leadAuthoritySdMs READ leadAuthoritySdMs NOTIFY leadDiagnosticsChanged)
    Q_PROPERTY(int leadAuthoritySamples READ leadAuthoritySamples NOTIFY leadDiagnosticsChanged)
    // [ORION_AIM_FREEZE] What the phase instrument MEASURED this rig's jumpshot at (full-window
    // median), expressed in the SAME effective frame as tipTimingMs. -1 = no full-window
    // measurement yet. Shown on the Tip Timing card beside a diverging manual value; consumed
    // by nothing on the decision path.
    Q_PROPERTY(double tipTimingMeasuredMs READ tipTimingMeasuredMs NOTIFY tipTimingChanged)
    Q_PROPERTY(bool inputTimedEnabled READ inputTimedEnabled WRITE setInputTimedEnabled NOTIFY settingsChanged)
    Q_PROPERTY(bool inputTimedPaused READ inputTimedPaused WRITE setInputTimedPaused NOTIFY settingsChanged)
    Q_PROPERTY(double inputTimedDelayMs READ inputTimedDelayMs WRITE setInputTimedDelayMs NOTIFY settingsChanged)
    Q_PROPERTY(double inputTimedLeadMs READ inputTimedLeadMs WRITE setInputTimedLeadMs NOTIFY settingsChanged)
    Q_PROPERTY(bool inputTimedRhythmEnabled READ inputTimedRhythmEnabled WRITE setInputTimedRhythmEnabled NOTIFY settingsChanged)
    // [ORION_NO_METER_V2 2026-09-14 owner] THE NO METER control: the reference press->release
    // hold in ms (500..800). The card presents it as a 1..100 slider over 500 + 3*(v-1).
    Q_PROPERTY(double noMeterHoldMs READ noMeterHoldMs WRITE setNoMeterHoldMs NOTIFY settingsChanged)
    // [ORION_NO_METER_FADE_TRIM 2026-09-14 owner] The fade-only trim in ms (-60..+60, 0 = the
    // shipped Δ). The card presents it as a -20..+20 slider at 3 ms a step.
    Q_PROPERTY(double noMeterFadeTrimMs READ noMeterFadeTrimMs WRITE setNoMeterFadeTrimMs NOTIFY settingsChanged)
    // [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] The console's input-sampling period (ms)
    // and the snap switch, exposed so the card can show the LIVE frame count under a drag
    // (settingsChanged only fires on commit) using the engine's own arithmetic.
    Q_PROPERTY(double consoleFrameMs READ consoleFrameMs NOTIFY settingsChanged)
    Q_PROPERTY(bool noMeterFrameQuantize READ noMeterFrameQuantize NOTIFY settingsChanged)
    // The COMMITTED Standstill hold as the console will actually see it: whole frames, and the
    // millisecond that count is worth. These are what the card's caption reads between drags.
    Q_PROPERTY(int noMeterHoldFrames READ noMeterHoldFrames NOTIFY settingsChanged)
    Q_PROPERTY(double noMeterHoldSnappedMs READ noMeterHoldSnappedMs NOTIFY settingsChanged)
    // [ORION_NO_METER_VISION_ASSIST 2026-09-14 owner] ON (default): in NO METER mode the blind
    // hold is the DEADLINE and vision owns any release it can actually see. OFF: pure blind.
    Q_PROPERTY(bool noMeterVisionAssist READ noMeterVisionAssist WRITE setNoMeterVisionAssist NOTIFY settingsChanged)
    Q_PROPERTY(bool noMeterEnabled READ noMeterEnabled WRITE setNoMeterEnabled NOTIFY settingsChanged)
    Q_PROPERTY(QString noMeterReleasePoint READ noMeterReleasePoint WRITE setNoMeterReleasePoint NOTIFY settingsChanged)
    Q_PROPERTY(double noMeterBaseOffsetMs READ noMeterBaseOffsetMs WRITE setNoMeterBaseOffsetMs NOTIFY settingsChanged)
    Q_PROPERTY(double noMeterDecodeCompMs READ noMeterDecodeCompMs WRITE setNoMeterDecodeCompMs NOTIFY settingsChanged)
    Q_PROPERTY(double noMeterConfidenceGate READ noMeterConfidenceGate WRITE setNoMeterConfidenceGate NOTIFY settingsChanged)
    Q_PROPERTY(double noMeterPushReleaseWindowMs READ noMeterPushReleaseWindowMs WRITE setNoMeterPushReleaseWindowMs NOTIFY settingsChanged)
    Q_PROPERTY(QString noMeterHandedness READ noMeterHandedness WRITE setNoMeterHandedness NOTIFY settingsChanged)
    // [ORION_METER_DELAY] Customer-facing meter-delay controls (the MeterConfigPanel
    // "Meter Delay" card binds exactly these three names on `orion`). AppConfig is the
    // single source of truth; the setters write through it AND re-push the live values
    // into the running MeterDelayController so a change takes effect without a restart.
    Q_PROPERTY(bool meterDelayEnabled READ meterDelayEnabled WRITE setMeterDelayEnabled NOTIFY settingsChanged)
    Q_PROPERTY(int meterDelayMs READ meterDelayMs WRITE setMeterDelayMs NOTIFY settingsChanged)
    Q_PROPERTY(bool meterDelayBypassOnDefense READ meterDelayBypassOnDefense WRITE setMeterDelayBypassOnDefense NOTIFY settingsChanged)
    // [ORION_METER_DELAY_AVAILABILITY 2026-08-08] Honesty surface for the Meter Delay
    // card. The delay is actuated by the WinDivert packet bridge: on a CURRENT customer
    // install that is the compiled packet_bridge\NexusVisionSvc.exe, which ships in the
    // release package (tools/package_orion_release.py copy_compiled_service) and is
    // registered demand-start + armed by the installer
    // (installer/orion.iss RegisterPacketBridgeService). On a dev rig the nexus_svc.py
    // debug script is the backend instead. The backend can STILL be absent — an older
    // install, a partial package, or an sc-create failure the installer reported and
    // continued past — and in that case the toggle would flip, persist and log while
    // silently doing nothing. meterDelayBackendAvailable reports whether a bridge
    // backend exists on this machine (probed once per launch, through the SAME cache
    // the install guard uses); meterDelayStatusText is the live actuator state for the
    // card and the log census.
    Q_PROPERTY(bool meterDelayBackendAvailable READ meterDelayBackendAvailable NOTIFY meterDelayBackendAvailabilityChanged)
    Q_PROPERTY(QString meterDelayStatusText READ meterDelayStatusText NOTIFY meterDelayStatusTextChanged)
    // [ORION_METER_DELAY_ARM_STATE 2026-08-08] True when the CONNECTED bridge service
    // reports its delay intercept DISARMED (hello `features` array / the per-verb
    // meter_delay_disarmed refusal — see NetworkBridge::MeterDelayServiceState). On a
    // dev rig the bridge can be RUNNING with the intercept disarmed: arming is a
    // deliberate out-of-band operator action (nexus_svc.py arm-gate comment), so the
    // card must be able to say "connected but not applying" instead of "Armed".
    // Rides meterDelayStatusTextChanged with the status line it qualifies.
    Q_PROPERTY(bool meterDelayServiceDisarmed READ meterDelayServiceDisarmed NOTIFY meterDelayStatusTextChanged)
    // [ORION_LEAD_CONFLICT_UI 2026-08-08 task #48] The delay the actuator is applying RIGHT
    // NOW (MeterDelayController::currentDelayMs()) for the ShotLeadUsableMaxIndicator. Rides
    // meterDelayStatusTextChanged, which fires on every applied-value change (each 50 ms ramp
    // tick), so the indicator tracks the ramp live as the user slides the delay.
    Q_PROPERTY(double meterDelayAppliedNowMs READ meterDelayAppliedNowMs NOTIFY meterDelayStatusTextChanged)
    Q_PROPERTY(bool showSkeleton READ showSkeleton WRITE setShowSkeleton NOTIFY settingsChanged)
    Q_PROPERTY(bool showLiveMeterMetrics READ showLiveMeterMetrics WRITE setShowLiveMeterMetrics NOTIFY settingsChanged)
    Q_PROPERTY(QString activeShotType READ activeShotType WRITE setActiveShotType NOTIFY settingsChanged)
    Q_PROPERTY(double minHoldMs READ minHoldMs WRITE setMinHoldMs NOTIFY settingsChanged)
    Q_PROPERTY(double maxHoldMs READ maxHoldMs WRITE setMaxHoldMs NOTIFY settingsChanged)
    Q_PROPERTY(int stableFrames READ stableFrames WRITE setStableFrames NOTIFY settingsChanged)
    Q_PROPERTY(QString visionPipeline READ visionPipeline NOTIFY statusChanged)
    // [ORION_PROBE] Human-readable state of the latency/tick-phase measurement. Rides
    // statusChanged (already emitted on the status tick) rather than adding signal plumbing,
    // so the readout refreshes itself while a run is in flight.
    Q_PROPERTY(QString latencyProbeSummary READ latencyProbeSummary NOTIFY statusChanged)
    Q_PROPERTY(QString settingsSnapshot READ settingsSnapshot NOTIFY settingsChanged)

    Q_PROPERTY(int playerCount READ playerCount NOTIFY telemetryChanged)
    Q_PROPERTY(int legitPlayerCount READ legitPlayerCount NOTIFY telemetryChanged)
    Q_PROPERTY(int cheaterCount READ cheaterCount NOTIFY telemetryChanged)

public:
    explicit OrionAppController(QString rootDir, QObject* parent = nullptr);
    ~OrionAppController() override;

    void setFrameProvider(RemoteFrameProvider* provider);
    bool nativeEventFilter(const QByteArray& eventType, void* message, qintptr* result) override;

    // Kill all chiaki/OrionStream processes. Guarded by chiakiCleanupDone_ so
    // calling it multiple times (destructor + aboutToQuit) is safe.
    void killChiakiProcesses();
    // Root-window close requests cleanup before asking Qt to quit. The plain
    // prepare method is also called by aboutToQuit/destruction as a fail-safe;
    // both are idempotent under re-entry from RemotePlay signals.
    Q_INVOKABLE void requestApplicationShutdown();
    void prepareForApplicationExit();

    [[nodiscard]] bool authenticated() const noexcept { return authenticated_; }
    [[nodiscard]] bool authBusy() const noexcept { return authBusy_; }
    [[nodiscard]] QString authMessage() const noexcept { return authMessage_; }
    [[nodiscard]] QString motdText() const noexcept { return motd_.text; }
    [[nodiscard]] QString motdLevel() const noexcept { return motd_.level; }
    [[nodiscard]] double motdUntil() const noexcept { return static_cast<double>(motd_.untilEpochS); }
    [[nodiscard]] bool motdVisible() const;
    // Hides the current MOTD for this process only; the next notice with a
    // different text shows again.
    Q_INVOKABLE void dismissMotd();
    [[nodiscard]] QString leaseNotice() const { return leaseNotice_; }   // [CL2-P8-002 2026-09-23]
    // ── Profile page accessors (see the Q_PROPERTY block above) ──────────────
    [[nodiscard]] bool profileKnown() const noexcept { return profile_.known; }
    [[nodiscard]] QString profileDiscordId() const noexcept { return profile_.discordUserId; }
    [[nodiscard]] QString profileDiscordName() const noexcept { return profile_.discordUsername; }
    [[nodiscard]] QString profilePlan() const noexcept { return profile_.plan; }
    [[nodiscard]] double profileExpiryEpochS() const noexcept
    {
        return static_cast<double>(profile_.expiryEpochS);
    }
    [[nodiscard]] bool profileLifetime() const noexcept { return profile_.lifetime(); }
    [[nodiscard]] int profileDaysLeft() const noexcept;
    [[nodiscard]] double profileActivatedEpochS() const noexcept
    {
        return static_cast<double>(profile_.activatedAtEpochS);
    }
    [[nodiscard]] int profileHwidResetsUsed() const noexcept { return profile_.hwidResetsUsed; }
    [[nodiscard]] int profileHwidResetsFreeTotal() const noexcept
    {
        return profile_.hwidResetsFreeTotal;
    }
    [[nodiscard]] int profileHwidResetsFreeRemaining() const noexcept
    {
        return profile_.hwidResetsFreeRemaining;
    }
    [[nodiscard]] int profileHwidPaidCredits() const noexcept { return profile_.hwidPaidCredits; }
    [[nodiscard]] QString machineIdMasked() const;
    // Puts the Discord user id on the clipboard (it is the handle support asks
    // for). No-op with a log line when the backend has not sent one.
    Q_INVOKABLE void copyProfileDiscordId();
    // Pure policy shared by profileDaysLeft() and the timeLeft strings, so the
    // hero number on Profile and the "Time left" row can never disagree.
    // nowEpochS == 0 is treated as "no clock"; expiry <= 0 with known == true is
    // LIFETIME. Exposed static for the unit test (the suite never constructs a
    // controller — see MeterDelaySettingsPropertyTests.cpp's header note).
    [[nodiscard]] static int licenseDaysLeft(bool known, qint64 expiryEpochS,
                                             qint64 nowEpochS) noexcept
    {
        if (!known) {
            return -1;
        }
        if (expiryEpochS <= 0) {
            return -2;   // lifetime
        }
        const qint64 remaining = expiryEpochS - nowEpochS;
        if (remaining <= 0) {
            return 0;
        }
        // Round UP: four hours left is still a day the customer can play.
        return static_cast<int>((remaining + 86399) / 86400);
    }
    [[nodiscard]] QString licenseState() const noexcept { return licenseState_; }
    [[nodiscard]] QString serverState() const noexcept { return serverState_; }
    [[nodiscard]] QString updateState() const noexcept { return updateState_; }
    [[nodiscard]] QString latestVersion() const noexcept { return latestVersion_; }
    [[nodiscard]] bool updateAvailable() const noexcept { return updateAvailable_; }
    [[nodiscard]] bool updateMandatory() const noexcept { return updateMandatory_; }
    [[nodiscard]] bool updateBlocked() const noexcept { return updateBlocked_; }
    [[nodiscard]] QString updateNotes() const noexcept { return updateNotes_; }
    [[nodiscard]] bool updaterPresent() const;
    // [ORION_LEAD_CALIBRATION] state; the bisection itself lives in LeadCalibrationPolicy so its
    // sign convention is unit-testable without a UI (the spec records that this sign has been
    // inverted in UI copy before, and inverting it makes the calibration DIVERGE).
    LeadCalibrationState leadCal_{};
    bool leadCalActive_ = false;
    // [RT-MED-01 / CL3-F4-009 2026-09-23] The exact (lead, user_set, route) tuple Cancel restores,
    // and whether any calibration step actually persisted a lead (nothing to undo otherwise).
    ActuationLeadProvenance leadCalStartLead_{};
    bool leadCalWroteLead_ = false;
    // [ORION_UPDATE_NO_LOCKOUT] A failed update must never be worse than no update. These record
    // the target version of an attempt in the per-user data dir, so a ForceUpdate gate that
    // cannot apply (typically: install dir in Program Files, updater launched unelevated) is
    // allowed through on the second pass instead of looping quit->relaunch->quit forever with
    // continueWithoutUpdate() refusing to release the user.
    [[nodiscard]] bool updateAlreadyAttemptedThisVersion() const;
    void noteUpdateAttempt() const;
    void clearUpdateAttempt() const;
    [[nodiscard]] QString updateGatePhase() const noexcept { return updateGatePhase_; }
    [[nodiscard]] QString updateChannel() const noexcept;
    void setUpdateChannel(const QString& channel);
    [[nodiscard]] bool devBuild() const noexcept { return devBuild_; }
    [[nodiscard]] QString securityState() const noexcept { return securityState_; }
    [[nodiscard]] QString securityDetail() const noexcept { return securityDetail_; }
    [[nodiscard]] bool securityLockActive() const noexcept { return securityLockActive_; }
    [[nodiscard]] QString securityLockReason() const noexcept { return securityLockReason_; }
    [[nodiscard]] bool settingsRepairAvailable() const noexcept { return settingsRepairAvailable_; }
    [[nodiscard]] QString entitlementState() const noexcept { return entitlementState_; }
    [[nodiscard]] QString integrityState() const noexcept { return integrityState_; }
    [[nodiscard]] QString lastSecurityAuditEvent() const noexcept { return lastSecurityAuditEvent_; }
    [[nodiscard]] QString currentPage() const noexcept { return currentPage_; }
    [[nodiscard]] QString appVersion() const { return QStringLiteral(ORION_NATIVE_VERSION); }
    [[nodiscard]] QString displayVersion() const { return QStringLiteral(ORION_NATIVE_VERSION); }
    [[nodiscard]] QString accentColor() const noexcept;
    void setAccentColor(const QString& color);
    [[nodiscard]] bool debugUiEnabled() const
    {
#ifdef ORION_PRODUCTION_BUILD
        return false;
#else
        return qEnvironmentVariableIntValue("ORION_DEBUG") == 1;
#endif
    }
    [[nodiscard]] QString rootDir() const noexcept { return rootDir_; }
    [[nodiscard]] QString iconSource() const noexcept { return iconSource_; }

    [[nodiscard]] QString remoteState() const noexcept { return remoteState_; }
    [[nodiscard]] QString remoteStatus() const noexcept { return remoteStatus_; }
    [[nodiscard]] bool remoteRunning() const noexcept { return remoteRunning_; }
    [[nodiscard]] QString controllerStatus() const noexcept { return controllerStatus_; }
    [[nodiscard]] QString controllerInputSource() const noexcept { return controllerInputSource_; }
    [[nodiscard]] QString controllerDeviceDetail() const noexcept { return controllerDeviceDetail_; }
    [[nodiscard]] QString controllerHealth() const noexcept { return controllerHealth_; }
    [[nodiscard]] QString controllerStableDevice() const noexcept { return controllerStableDevice_; }
    [[nodiscard]] bool controllerIsPhysicalSony() const noexcept { return controllerIsPhysicalSony_; }
    [[nodiscard]] bool controllerUsbPowerFixAvailable() const noexcept { return controllerUsbPowerFixAvailable_; }
    [[nodiscard]] double controllerLastInputAgeMs() const noexcept { return controllerLastInputAgeMs_; }
    [[nodiscard]] bool physicalSquarePressed() const noexcept { return pressedOverlayLatch_.held(); }
    [[nodiscard]] bool inputDeadOverlayActive() const noexcept {
        return inputDeadSeverity_ != InputDeadSeverity::None;
    }
    [[nodiscard]] bool inputDeadCritical() const noexcept {
        return inputDeadSeverity_ == InputDeadSeverity::Critical;
    }
    [[nodiscard]] QString inputDeadHeadline() const noexcept { return inputDeadHeadline_; }
    [[nodiscard]] QString inputDeadDetail() const noexcept { return inputDeadDetail_; }
    [[nodiscard]] QString controllerLedStatus() const noexcept { return controllerLedStatus_; }
    [[nodiscard]] QString chiakiEmbedStatus() const noexcept { return chiakiEmbedStatus_; }
    [[nodiscard]] bool qmlRenderMode() const noexcept { return qmlRenderMode_; }
    [[nodiscard]] QString captureSourceHealth() const noexcept { return captureSourceHealth_; }
    [[nodiscard]] bool capturePreviewActive() const noexcept { return capturePreviewActive_; }
    [[nodiscard]] QString backendMessage() const noexcept { return backendMessage_; }
    [[nodiscard]] int frameSerial() const noexcept { return frameSerial_; }
    [[nodiscard]] bool previewAsync() const noexcept { return previewAsync_; }

    [[nodiscard]] QString consoleIp() const noexcept { return config_.data().remotePlayConsoleIp; }
    [[nodiscard]] QString remotePlayConsole() const noexcept { return config_.data().remotePlayConsole; }
    [[nodiscard]] QString xboxRemotePlayWindowTitle() const { return config_.data().xboxRemotePlayWindowTitle; }
    void setXboxRemotePlayWindowTitle(const QString& value);
    [[nodiscard]] bool xboxUntestedAcknowledged() const noexcept { return config_.data().xboxUntestedAcknowledged; }
    void setXboxUntestedAcknowledged(bool value);
    Q_INVOKABLE QStringList xboxRemotePlayWindows() const;
    Q_INVOKABLE void openXboxRemotePlay();
    [[nodiscard]] bool streamSetupComplete() const noexcept { return config_.data().streamSetupComplete; }
    [[nodiscard]] bool preflightComplete() const noexcept { return config_.data().preflightComplete; }
    [[nodiscard]] QString pendingActivationKey() const noexcept { return pendingActivationKey_; }
    [[nodiscard]] QString licenseKeyMasked() const;
    [[nodiscard]] bool legalAccepted() const noexcept {
        return config_.data().legalAcceptedVersion >= AppConfigData::kCurrentLegalVersion;
    }
    [[nodiscard]] QString videoSource() const noexcept { return isXboxRemotePlay(config_.data()) ? QStringLiteral("wgc") : config_.data().videoSource; }
    [[nodiscard]] int captureCardIndex() const noexcept { return config_.data().captureCardIndex; }
    [[nodiscard]] bool captureCardSelected() const noexcept {
        const auto& data = config_.data();
        return data.captureCardIndex >= 0 && data.captureCardIndex < captureDeviceIds_.size()
            && !data.captureCardDeviceId.isEmpty()
            && captureDeviceIds_[data.captureCardIndex] == data.captureCardDeviceId;
    }
    [[nodiscard]] int captureCardFps() const noexcept { return config_.data().captureCardFps; }
    [[nodiscard]] QStringList captureDeviceList() const { return captureDeviceList_; }
    [[nodiscard]] bool hardwareDecode() const noexcept { return config_.data().hardwareDecode; }
    [[nodiscard]] QString controllerType() const noexcept { return config_.data().controllerType; }
    [[nodiscard]] bool autoReconnect() const noexcept { return config_.data().autoReconnect; }
    [[nodiscard]] QString chiakiPath() const noexcept { return config_.data().chiakiPath; }
    [[nodiscard]] QString profileUser() const noexcept { return config_.data().remotePlayProfile; }
    [[nodiscard]] QString pin() const noexcept { return pin_; }
    [[nodiscard]] QString redirectUrl() const noexcept { return redirectUrl_; }

    [[nodiscard]] QString telemetryConsoleIp() const noexcept { return telemetry_.consoleIp; }
    [[nodiscard]] QString telemetryCourtIp() const noexcept {
        return telemetry_.rttTargetVerified && !telemetry_.courtIp.isEmpty()
            ? telemetry_.courtIp : telemetry_.diagnosticCourtIp;
    }
    [[nodiscard]] QString telemetryCourtIpMasked() const;
    [[nodiscard]] bool courtIpVerified() const noexcept {
        return telemetry_.rttTargetVerified && !telemetry_.courtIp.isEmpty();
    }
    [[nodiscard]] QString telemetrySync() const { return telemetry_.syncActive ? QStringLiteral("Active") : QStringLiteral("Waiting"); }
    [[nodiscard]] int inboundPackets() const noexcept { return telemetry_.inboundPackets; }
    [[nodiscard]] int outboundPackets() const noexcept { return telemetry_.outboundPackets; }
    [[nodiscard]] double offsetMs() const noexcept {
        return telemetry_.rttTargetVerified ? telemetry_.offsetMs : 0.0;
    }
    [[nodiscard]] double syncAdjustMs() const noexcept {
        return telemetry_.rttTargetVerified ? telemetry_.syncAdjustMs : 0.0;
    }
    [[nodiscard]] double rttMs() const noexcept {
        return telemetry_.rttTargetVerified ? telemetry_.rttMs : 0.0;
    }
    [[nodiscard]] bool rttVerified() const noexcept { return telemetry_.rttTargetVerified; }
    [[nodiscard]] double jitterMs() const noexcept {
        return telemetry_.rttTargetVerified ? telemetry_.jitterMs : 0.0;
    }
    [[nodiscard]] int consecutivePackets() const noexcept { return telemetry_.consecutivePackets; }
    [[nodiscard]] double packetIntervalMs() const noexcept { return telemetry_.packetIntervalMs; }
    [[nodiscard]] int inboundConsecutive() const noexcept { return telemetry_.inboundConsecutive; }
    [[nodiscard]] int outboundConsecutive() const noexcept { return telemetry_.outboundConsecutive; }
    [[nodiscard]] double inboundIntervalMs() const noexcept { return telemetry_.inboundIntervalMs; }
    [[nodiscard]] double outboundIntervalMs() const noexcept { return telemetry_.outboundIntervalMs; }
    [[nodiscard]] double tickerLatencyMs() const noexcept;
    [[nodiscard]] bool tickPhaseVerified() const noexcept;
    [[nodiscard]] bool playingGame() const noexcept { return telemetry_.playingGame; }
    [[nodiscard]] QString syncSource() const noexcept { return telemetry_.syncSource; }
    [[nodiscard]] bool noMeterAvailable() const { return remotePlay_.noMeterAvailable(); }
    [[nodiscard]] QString noMeterUnavailableReason() const { return remotePlay_.noMeterUnavailableReason(); }
    [[nodiscard]] double syncConfidence() const noexcept { return telemetry_.syncConfidence; }
    // [VENICENET WAVE 1 2026-08-08] The passive-sniffing opt-in flag is deleted;
    // capture diagnostics ride the network feature itself (default ON).
    [[nodiscard]] bool packetCaptureEnabled() const noexcept
    {
        return config_.data().networkEnabled;
    }
    [[nodiscard]] bool packetCaptureActive() const noexcept
    {
        return config_.data().networkEnabled && networkBridge_.connected();
    }
    [[nodiscard]] QString rttSyncMode() const noexcept { return config_.data().rttSyncMode; }
    [[nodiscard]] QString streamBandwidthMode() const noexcept { return config_.data().streamBandwidthMode; }
    [[nodiscard]] bool streamAudioEnabled() const noexcept { return config_.data().streamAudioEnabled; }
    [[nodiscard]] QString streamAudioMode() const noexcept { return config_.data().streamAudioMode; }
    [[nodiscard]] bool audioToggleBusy() const noexcept { return remotePlay_.audioToggleBusy(); }
    [[nodiscard]] bool controllerLightbarEnabled() const noexcept { return config_.data().controllerLightbarEnabled; }
    [[nodiscard]] QString controllerLightbarColor() const noexcept { return config_.data().controllerLightbarColor; }
    [[nodiscard]] QString controllerLightbarMode() const noexcept { return config_.data().controllerLightbarMode; }
    [[nodiscard]] QString controllerLightbarPrimaryColor() const noexcept { return config_.data().controllerLightbarPrimaryColor; }
    [[nodiscard]] QString controllerLightbarSecondaryColor() const noexcept { return config_.data().controllerLightbarSecondaryColor; }
    [[nodiscard]] double controllerLightbarBrightness() const noexcept { return config_.data().controllerLightbarBrightness; }
    [[nodiscard]] double controllerLightbarEffectSpeed() const noexcept { return config_.data().controllerLightbarEffectSpeed; }
    [[nodiscard]] QString meterOverlayColor() const noexcept { return config_.data().meterOverlayColor; }
    [[nodiscard]] QString meterOverlayStyle() const noexcept { return config_.data().meterOverlayStyle; }
    [[nodiscard]] bool meterOverlayRgb() const noexcept { return config_.data().meterOverlayRgb; }
    [[nodiscard]] QString meterOverlayDrawColor() const
    {
        return config_.data().meterOverlayRgb && !overlayRgbColor_.isEmpty()
            ? overlayRgbColor_
            : config_.data().meterOverlayColor;
    }
    [[nodiscard]] double manualSyncAdjustMs() const noexcept { return config_.data().manualSyncAdjustMs; }
    [[nodiscard]] double manualOffsetMs() const noexcept { return config_.data().manualOffsetMs; }
    [[nodiscard]] double effectiveSyncAdjustMs() const noexcept {
        return config_.data().rttSyncMode == QLatin1String("Manual")
            ? config_.data().manualSyncAdjustMs
            : (telemetry_.rttTargetVerified ? telemetry_.syncAdjustMs : 0.0);
    }
    [[nodiscard]] double effectiveOffsetMs() const noexcept {
        return config_.data().rttSyncMode == QLatin1String("Manual")
            ? config_.data().manualOffsetMs
            : (telemetry_.rttTargetVerified ? telemetry_.offsetMs : 0.0);
    }
    // Same call the engine feed uses, so the card cannot drift from the engine.
    [[nodiscard]] double appliedNetworkOffsetMs() const noexcept {
        return networkAutomationOffset();
    }

    [[nodiscard]] double timingLatencyMs() const noexcept { return config_.data().latencyCompensationMs; }
    [[nodiscard]] double releaseThresholdPct() const noexcept { return config_.data().releaseThresholdPct; }
    [[nodiscard]] double earlyLateOffsetMs() const noexcept { return config_.data().earlyLateOffsetMs; }
    [[nodiscard]] QString tempoInputSource() const noexcept { return config_.data().remotePlayInputSource; }
    [[nodiscard]] QString tempoRemapType() const noexcept { return config_.data().tempoRemapType; }
    [[nodiscard]] double tempoWaitMs() const noexcept { return config_.data().tempoWaitMs; }
    [[nodiscard]] double rhythmFlickDelayMs() const noexcept { return config_.data().rhythmFlickDelayMs; }
    [[nodiscard]] double tempoFallbackMs() const noexcept { return config_.data().tempoFallbackTimeoutMs; }
    [[nodiscard]] double tempoFlickHoldMs() const noexcept { return config_.data().tempoFlickHoldMs; }
    [[nodiscard]] QString tempoReleaseStyle() const { return config_.data().tempoReleaseStyle; }
    [[nodiscard]] double tempoMinStickHoldMs() const noexcept { return config_.data().tempoMinStickHoldMs; }

    [[nodiscard]] QString shotState() const noexcept { return shotState_; }
    [[nodiscard]] QString activeBotShotType() const noexcept { return shot_.shotType; }
    [[nodiscard]] bool meterMetricsValid() const noexcept;
    [[nodiscard]] double shotFillPct() const noexcept
    {
        return meterMetricsValid() ? measuredMeterFillPct_ : -1.0;
    }
    // Active-shooting gate: true while a shot is LIVE (armed/holding/timing/releasing). False at
    // Idle/Cooldown, so the overlay lock box is suppressed between shots — a menu/loading screen
    // (no held shot button) never shows a lock even if a stray red+green UI element is on screen.
    [[nodiscard]] bool isShooting() const noexcept {
        return shot_.state != HoldState::Idle && shot_.state != HoldState::Cooldown;
    }
    // Overlay confirmation gate: true only while the AUTHORITATIVE detector has a FRESH, REAL
    // meter lock (a clean raw detection within the last ~120ms — meter_memory/stale echoes
    // excluded). Computed per frame in handleRemoteFrame so the cached bool stays current.
    // Unlike isShooting (true the instant Square is held ~135ms, regardless of whether any
    // meter exists), this can never paint a lock on a stale or distractor bbox during an idle hold.
    [[nodiscard]] bool meterConfirmed() const noexcept { return meterConfirmed_; }
    [[nodiscard]] bool meterBlindWarning() const noexcept { return meterBlindWarning_; }
    // [2026-09-14 owner] Generic. This used to name a colour and offer "the other one" — a
    // Red/Purple choice inherited from 2K26. 2K27's meter is WHITE, the Style picker (Pill vs
    // Straight) is at least as likely to be the mismatch, and the old wording sent the owner to
    // change a setting that was already correct. The card's Style and Color controls sit
    // directly above this line, so naming them is the whole instruction.
    [[nodiscard]] QString meterBlindHint() const
    {
        // [CL2-P9-001 2026-09-23] Say what the bot is DOING first: the old text sent customers into
        // settings that were already correct while shots kept releasing on a timer.
        return QStringLiteral(
            "Meter detection unavailable. Venice is not timing your shots until it sees the meter "
            "again. Check the in-game meter is Arrow2 / White.");
    }
    [[nodiscard]] QString detectorHealthLine() const { return detectorHealthLine_; }
    [[nodiscard]] QString detectorProvider() const { return detectorProvider_; }
    // Shot mode for the overlay readout (TARGET is always the tip; only the label differs).
    [[nodiscard]] QString shotMode() const noexcept {
        if (shot_.mode == ShotMode::TempoSquare || shot_.mode == ShotMode::TempoStick)
            return QStringLiteral("TEMPO");
        if (shot_.mode == ShotMode::GoToStick)
            return QStringLiteral("GO-TO");
        return QStringLiteral("BUTTON");
    }
    [[nodiscard]] double shotConfidence() const noexcept
    {
        return meterMetricsValid() ? measuredMeterConfidence_ : -1.0;
    }
    [[nodiscard]] double shotVelocityPctS() const noexcept
    {
        return meterMetricsValid() ? measuredMeterVelocityPctS_ : 0.0;
    }
    // Actual native bot-ownership duration. The controller stamps the first
    // committed owned-state transition for each arm token, excluding the user's
    // pre-commit physical hold. It ends at the native release trigger. This is a
    // local submit-boundary measurement, not a console acknowledgement.
    [[nodiscard]] double shotHoldMs() const noexcept;
    // Time until the bot submits its release command (latency compensated).
    [[nodiscard]] double shotEtaMs() const noexcept;
    // Time until the measured meter trajectory reaches the active target/tip.
    // No current vision crossing means unavailable (-1), never a fabricated ETA.
    [[nodiscard]] double shotEtaToTargetMs() const noexcept;
    [[nodiscard]] double botTargetPct() const noexcept
    {
        return shot_.armToken != 0 ? shot_.targetPct : -1.0;
    }
    [[nodiscard]] double botReleaseLeadMs() const noexcept
    {
        return shot_.armToken != 0 && std::isfinite(shot_.effectiveLatencyMs)
            && shot_.effectiveLatencyMs >= 0.0 ? shot_.effectiveLatencyMs : -1.0;
    }
    [[nodiscard]] QString botDecision() const noexcept { return shot_.releasePlan; }
    [[nodiscard]] QString botReleaseReason() const noexcept { return shot_.releaseReason; }
    [[nodiscard]] QString meterProfile() const noexcept { return meterProfile_; }
    [[nodiscard]] QString meterRejectionReason() const noexcept { return meterRejectionReason_; }
    [[nodiscard]] QString meterSearchBox() const { return rectSummary(meterSearchBox_); }
    // Diagnostics report the raw authoritative capture-space box, never the filtered/padded
    // presentation rectangle exposed through meterBoxX/Y/Width/Height.
    [[nodiscard]] QString meterBodyBox() const { return rectSummary(meterBoxCapture_); }
    [[nodiscard]] QString meterRejectedBox() const { return rectSummary(meterRejectedBox_); }
    [[nodiscard]] QString meterFillLine() const
    {
        return meterMetricsValid() ? meterFillLine_ : QStringLiteral("--");
    }
    [[nodiscard]] QString meterTargetLine() const
    {
        return meterMetricsValid() ? meterTargetLine_ : QStringLiteral("--");
    }
    // Cached-snapshot reads. Deliberately trivial: all truth gating, clock
    // arithmetic and formatting happened once in refreshLiveMeterTelemetry().
    [[nodiscard]] bool meterHudLive() const noexcept { return liveMeter_.valid; }
    // Cached, not recomputed: meterMetricsValid() is wall-clock-dependent, so
    // reading it here would let the tone flicker between two paints of the same
    // published snapshot. The snapshot's own fill sentinel is the answer.
    [[nodiscard]] bool meterHudMeasured() const noexcept
    {
        return liveMeter_.fillPct >= 0.0;
    }
    [[nodiscard]] QString meterHudFillLine() const { return hudFill_.text; }
    [[nodiscard]] QString meterHudTipLine() const { return hudTip_.text; }
    [[nodiscard]] QString meterHudFireLine() const { return hudFire_.text; }
    [[nodiscard]] bool meterHudCommandLate() const noexcept
    {
        return liveMeter_.commandEtaValid && liveMeter_.commandEtaMs < 0.0;
    }
    [[nodiscard]] int requestedStreamFps() const noexcept { return remotePlay_.requestedFps(); }
    [[nodiscard]] int captureLoopFps() const noexcept { return remotePlay_.captureLoopFps(); }
    [[nodiscard]] int uniqueFrameFps() const noexcept { return remotePlay_.uniqueFrameFps(); }
    [[nodiscard]] int previewLagFrames() const noexcept { return remotePlay_.previewLagFrames(); }
    [[nodiscard]] double duplicateFramePct() const noexcept { return remotePlay_.duplicateFramePct(); }
    [[nodiscard]] double frameAgeMs() const noexcept { return remotePlay_.frameAgeMs(); }
    [[nodiscard]] QString captureResolution() const { return remotePlay_.captureResolution(); }
    [[nodiscard]] QString fpsProbeState() const noexcept { return remotePlay_.fpsProbeState(); }
    [[nodiscard]] int meterSearchBoxX() const noexcept { return meterSearchBox_.x(); }
    [[nodiscard]] int meterSearchBoxY() const noexcept { return meterSearchBox_.y(); }
    [[nodiscard]] int meterSearchBoxWidth() const noexcept { return meterSearchBox_.width(); }
    [[nodiscard]] int meterSearchBoxHeight() const noexcept { return meterSearchBox_.height(); }
    [[nodiscard]] int meterBoxX() const noexcept { return meterBox_.x(); }
    [[nodiscard]] int meterBoxY() const noexcept { return meterBox_.y(); }
    [[nodiscard]] int meterBoxWidth() const noexcept { return meterBox_.width(); }
    [[nodiscard]] int meterBoxHeight() const noexcept { return meterBox_.height(); }
    [[nodiscard]] int meterRejectedBoxX() const noexcept { return meterRejectedBox_.x(); }
    [[nodiscard]] int meterRejectedBoxY() const noexcept { return meterRejectedBox_.y(); }
    [[nodiscard]] int meterRejectedBoxWidth() const noexcept { return meterRejectedBox_.width(); }
    [[nodiscard]] int meterRejectedBoxHeight() const noexcept { return meterRejectedBox_.height(); }
    [[nodiscard]] int liveFrameWidth() const noexcept { return liveFrameWidth_; }
    [[nodiscard]] int liveFrameHeight() const noexcept { return liveFrameHeight_; }
    [[nodiscard]] QVariantList poseKeypoints() const { return poseKeypoints_; }
    [[nodiscard]] QVariantList poseBox() const { return poseBox_; }
    [[nodiscard]] QVariantList poseAnchor() const { return poseAnchor_; }
    [[nodiscard]] QVariantList poseLockCenter() const { return poseLockCenter_; }
    [[nodiscard]] QVariantList poseIndicator() const { return poseIndicator_; }
    [[nodiscard]] int shotsAttempted() const noexcept { return automation_.shotsAttempted(); }
    [[nodiscard]] int shotsReleased() const noexcept { return automation_.shotsReleased(); }
    [[nodiscard]] int shotsAborted() const noexcept { return automation_.shotsAborted(); }
    [[nodiscard]] QString logText() const;
    // [ORION_ACTIVITY_FEED 2026-09-14] The CUSTOMER feed: the same stream with
    // engineering telemetry removed by ui_notifications::shouldEnterActivityRing.
    // logText() above is unchanged (the Debug page still shows the raw ring) and
    // logs/orion_native.log still receives every line either way.
    [[nodiscard]] QString activityText() const;
    [[nodiscard]] bool coreActive() const noexcept { return coreActive_; }
    [[nodiscard]] QString scriptState() const noexcept { return scriptState_; }
    [[nodiscard]] QString scriptDetail() const noexcept { return scriptDetail_; }
    [[nodiscard]] QString licenseDetail() const noexcept { return licenseDetail_; }
    [[nodiscard]] QString timeLeft() const noexcept { return timeLeft_; }
    [[nodiscard]] QString timeLeftDetail() const noexcept { return timeLeftDetail_; }
    [[nodiscard]] QString meterRuntimeState() const noexcept { return meterRuntimeState_; }
    [[nodiscard]] int sessionVerdicts() const noexcept { return sessionVerdicts_; }
    [[nodiscard]] int sessionGreens() const noexcept { return sessionGreens_; }
    [[nodiscard]] int manualShotMakes() const noexcept { return manualShotTally_.makes(); }
    [[nodiscard]] int manualShotMisses() const noexcept { return manualShotTally_.misses(); }
    [[nodiscard]] int manualShotTotal() const noexcept { return manualShotTally_.total(); }
    Q_INVOKABLE void recordManualShotResult(bool made);
    Q_INVOKABLE void undoManualShotResult();
    Q_INVOKABLE void resetManualShotResults();
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14] Live shot-verdict tally readers. Every one of
    // these is a pure read of the rolling window; the rules themselves live in
    // ShotVerdictTally.h so they can be pinned without constructing this controller.
    [[nodiscard]] int bannerGreen10() const noexcept { return bannerTally_.green(); }
    [[nodiscard]] int bannerEarly10() const noexcept { return bannerTally_.early(); }
    [[nodiscard]] int bannerLate10() const noexcept { return bannerTally_.late(); }
    [[nodiscard]] int bannerOther10() const noexcept { return bannerTally_.other(); }
    [[nodiscard]] int bannerCount10() const noexcept { return bannerTally_.count(); }
    [[nodiscard]] int bannerContested10() const noexcept { return bannerTally_.contested(); }
    [[nodiscard]] QString bannerLastTiming() const { return bannerTally_.lastTiming(); }
    [[nodiscard]] QString bannerLastCoverage() const { return bannerTally_.lastCoverage(); }
    [[nodiscard]] QString bannerPattern10() const { return bannerTally_.pattern(); }
    [[nodiscard]] QString bannerSuggestion() const { return bannerTally_.suggestion(); }
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] The caption under the Shot Lead slider. "Standstill" is
    // the display bucket on purpose: it is the reference type the whole blind/trim family is
    // measured against, and it is the type the owner is standing in while he reads the card.
    [[nodiscard]] double bannerLeadTrimMs() const { return bannerLeadTrimMs_; }
    [[nodiscard]] bool bannerLeadTrimEnabled() const noexcept { return bannerLeadTrimEnabled_; }
    // [ORION_LEAD_AUTO_SEED 2026-09-15] The auto-seed caption's five reads. The margin comes from
    // the SETTING (it is a shipped constant, not engine state), the other four from the engine.
    [[nodiscard]] bool leadAutoSeedActive() const noexcept { return leadAutoSeedActive_; }
    [[nodiscard]] double leadAutoSeedMs() const noexcept { return leadAutoSeedMs_; }
    [[nodiscard]] QString leadAutoSeedKind() const { return leadAutoSeedKind_; }
    [[nodiscard]] double leadAutoSeedMeasuredMs() const noexcept { return leadAutoSeedMeasuredMs_; }
    [[nodiscard]] double leadAutoSeedMarginMs() const { return config_.data().aimMarginMs; }
    // The card's Reset link, and the automatic clear behind every committed change to the
    // value being tuned (Release timing, the fade trim, Shot Lead).
    Q_INVOKABLE void resetBannerTally();
    [[nodiscard]] bool wifiModeActive() const { return automation_.wifiModeActive(); }
    [[nodiscard]] bool defenseModeActive() const noexcept { return defenseModeActive_; }
    [[nodiscard]] QString defenseTriggerButton() const { return config_.data().defenseTriggerButton; }
    void setDefenseTriggerButton(const QString& value);
    [[nodiscard]] bool defenseStickAssist() const { return config_.data().defenseStickAssist; }
    void setDefenseStickAssist(bool value);
    [[nodiscard]] double defenseStickAssistStrength() const { return config_.data().defenseStickAssistStrength; }
    void setDefenseStickAssistStrength(double value);
    [[nodiscard]] bool defenseL2HoldAssist() const { return config_.data().defenseL2HoldAssist; }
    void setDefenseL2HoldAssist(bool value);
    [[nodiscard]] bool defenseSprintAssist() const { return config_.data().defenseSprintAssist; }
    void setDefenseSprintAssist(bool value);
    [[nodiscard]] QString defenseLightbarColor() const { return config_.data().defenseLightbarColor; }
    void setDefenseLightbarColor(const QString& value);
    [[nodiscard]] QString activeProfile() const { return config_.data().activeProfile; }
    [[nodiscard]] QString profilesSnapshot() const;
    [[nodiscard]] bool safeModeActive() const noexcept { return safeModeActive_; }
    [[nodiscard]] QString safeModeReason() const noexcept { return safeModeReason_; }
    [[nodiscard]] QString holdSource() const noexcept { return holdSource_; }
    [[nodiscard]] QString lastResult() const noexcept { return lastResult_; }
    [[nodiscard]] QString outputState() const noexcept { return outputState_; }
    [[nodiscard]] QString sessionTime() const noexcept { return sessionTime_; }
    [[nodiscard]] QString blockReason() const noexcept { return blockReason_; }
    [[nodiscard]] QString statusAge() const noexcept { return statusAge_; }
    [[nodiscard]] QString netSyncReason() const noexcept { return netSyncReason_; }
    [[nodiscard]] QString meterStyle() const noexcept { return config_.data().meterStyle; }
    [[nodiscard]] QString meterProposer() const noexcept { return config_.data().meterProposer; }
    [[nodiscard]] QString meterColor() const noexcept { return config_.data().meterColor; }
    [[nodiscard]] bool meterEnabled() const noexcept { return config_.data().meterEnabled; }
    [[nodiscard]] bool autoMeterColor() const noexcept { return config_.data().autoMeterColor; }
    [[nodiscard]] int detectionConfidencePercent() const noexcept { return config_.data().detectionConfidencePercent; }
    [[nodiscard]] bool autoTune() const noexcept { return !config_.data().freezeCalibration; }
    [[nodiscard]] bool tempoEnabled() const noexcept { return config_.data().tempoEnabled || config_.data().tempoRemapEnabled; }
    [[nodiscard]] bool noDipEnabled() const noexcept { return config_.data().noDipEnabled; }
    [[nodiscard]] double noDipLeadMs() const noexcept { return config_.data().noDipLeadMs; }
    [[nodiscard]] double actuationLeadMs() const noexcept { return config_.data().actuationLeadMs; }
    [[nodiscard]] bool actuationLeadUserSet() const noexcept { return config_.data().actuationLeadUserSet; }
    [[nodiscard]] double actuationLeadMinMs() const noexcept { return AppConfigData::kActuationLeadMinMs; }
    [[nodiscard]] double actuationLeadMaxMs() const noexcept { return AppConfigData::kActuationLeadMaxMs; }
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] see the Q_PROPERTY note.
    [[nodiscard]] double meterDelayMaxUsableMs() const noexcept
    {
        const double lead = config_.data().actuationLeadMs;
        if (!std::isfinite(lead) || lead <= 0.0) {
            return 0.0;
        }
        const double v = shotLeadMaxUsableMs() - lead;
        return std::isfinite(v) ? std::max(0.0, v) : 0.0;
    }
    [[nodiscard]] double meterDelayLeadOffsetAppliedMs() const noexcept
    {
        return automation_.appliedMeterDelayLeadOffsetMsForUi();
    }
    [[nodiscard]] double meterDelayLeadOffsetMs() const noexcept
    {
        return config_.data().meterDelayLeadOffsetMs;
    }
    [[nodiscard]] double meterDelayLeadOffsetMinMs() const noexcept
    {
        return AppConfigData::kMeterDelayLeadOffsetMinMs;
    }
    [[nodiscard]] double meterDelayLeadOffsetMaxMs() const noexcept;
    [[nodiscard]] double actuationLeadMeasuredMs() const noexcept { return actuationLeadMeasuredMs_; }
    [[nodiscard]] int actuationLeadMeasuredSamples() const noexcept { return actuationLeadMeasuredSamples_; }
    // [ORION_LEAD_CONFLICT] See the Q_PROPERTY block. Derived per read from tipTimingMs() so the
    // annotation tracks the aim at the cadence the Tip Timing card itself refreshes.
    [[nodiscard]] double shotLeadMaxUsableMs() const;
    [[nodiscard]] int shotLeadConflictMisses() const noexcept { return shotLeadConflictMisses_; }
    // [ORION_LEAD_CONFLICT_UI 2026-08-08 task #48] Applied-delay readout + the indicator's
    // warning line. The Q_INVOKABLE is a thin shim over orion::shotLeadUsableMaxWarningLine
    // (AutomationEngine.h) so the QML binding stays reactive on its ARGUMENT properties
    // while the shipped composition is pinned by shotLeadUsableMaxClampReflectsAppliedDelay.
    [[nodiscard]] double meterDelayAppliedNowMs() const noexcept;
    Q_INVOKABLE QString shotLeadUsableMaxWarning(double leadMs, double usableMaxMs,
                                                 double appliedDelayMs) const;
    [[nodiscard]] double leadAuthorityMs() const noexcept { return leadAuthorityMs_; }
    [[nodiscard]] double leadAuthoritySdMs() const noexcept { return leadAuthoritySdMs_; }
    [[nodiscard]] int leadAuthoritySamples() const noexcept { return leadAuthoritySamples_; }
    [[nodiscard]] bool inputTimedEnabled() const noexcept { return config_.data().inputTimedEnabled; }
    [[nodiscard]] bool inputTimedPaused() const noexcept { return inputTimedPaused_; }
    [[nodiscard]] double inputTimedDelayMs() const noexcept { return config_.data().inputTimedDelayMs; }
    [[nodiscard]] double inputTimedLeadMs() const noexcept { return config_.data().inputTimedLeadMs; }
    [[nodiscard]] bool inputTimedRhythmEnabled() const noexcept { return config_.data().inputTimedRhythmEnabled; }
    void setInputTimedEnabled(bool value);
    void setInputTimedPaused(bool value);
    void setInputTimedDelayMs(double value);
    void setInputTimedLeadMs(double value);
    void setInputTimedRhythmEnabled(bool value);
    // [ORION_NO_METER_V2 2026-09-14] The blind-release reference hold (ms), clamped into
    // [kNoMeterHoldMinMs, kNoMeterHoldMaxMs] on the way in.
    [[nodiscard]] double noMeterHoldMs() const noexcept { return config_.data().noMeterHoldMs; }
    void setNoMeterHoldMs(double value);
    // [ORION_NO_METER_FADE_TRIM 2026-09-14] The fade-only trim (ms), clamped into
    // [kNoMeterFadeTrimMinMs, kNoMeterFadeTrimMaxMs] on the way in.
    [[nodiscard]] double noMeterFadeTrimMs() const noexcept
    {
        return config_.data().noMeterFadeTrimMs;
    }
    void setNoMeterFadeTrimMs(double value);
    // === [ORION_CONSOLE_FRAME_QUANTIZE 2026-09-14 owner] THE CARD'S FRAME READOUT ============
    //
    // The console judges the release on a FRAME, so "650 ms" is really "39 frames" and 641 ms is
    // really "38.46 frames" — a hold that coin-flips between two frames. The card leads with the
    // frame count for exactly that reason, and these are the numbers it reads.
    //
    // THE STANDSTILL hold, deliberately: Standstill's Δ is 0 by construction, so this is H_ref
    // itself on the grid — the quantity the slider under the caption controls. The fades carry
    // their own Δ (and their own trim, whose readout says how many frames IT is worth) and the
    // Rhythm offset belongs to the Rhythm card, so folding either in here would make the caption
    // describe something other than the slider it sits above.
    //
    // They read the PERSISTED settings, i.e. the same source the slider writes. An offline sweep
    // driving ORION_CONSOLE_FRAME_MS moves the engine and not this readout; that is the intended
    // asymmetry for every env override in this app, and the arm line logs the grid it truly used.
    [[nodiscard]] double consoleFrameMs() const noexcept
    {
        return clampedConsoleFrameMs(config_.data().consoleFrameMs);
    }
    [[nodiscard]] bool noMeterFrameQuantize() const noexcept
    {
        return config_.data().noMeterFrameQuantize;
    }
    [[nodiscard]] double noMeterHoldSnappedMs() const noexcept
    {
        // The same order the engine's blindReleaseHold() uses: floor, then snap.
        const double floored = std::max(kBlindReleaseFloorMs, config_.data().noMeterHoldMs);
        return noMeterFrameQuantize() ? snappedConsoleFrameMs(floored, consoleFrameMs())
                                      : floored;
    }
    [[nodiscard]] int noMeterHoldFrames() const noexcept
    {
        return consoleFrameCount(noMeterHoldSnappedMs(), consoleFrameMs());
    }
    // [ORION_NO_METER_VISION_ASSIST 2026-09-14] The hybrid switch. Default TRUE.
    [[nodiscard]] bool noMeterVisionAssist() const noexcept
    {
        return config_.data().noMeterVisionAssist;
    }
    void setNoMeterVisionAssist(bool value);
    [[nodiscard]] bool noMeterEnabled() const noexcept { return config_.data().noMeterEnabled; }
    [[nodiscard]] QString noMeterReleasePoint() const noexcept { return config_.data().noMeterReleasePoint; }
    [[nodiscard]] double noMeterBaseOffsetMs() const noexcept { return config_.data().noMeterBaseOffsetMs; }
    [[nodiscard]] double noMeterDecodeCompMs() const noexcept { return config_.data().noMeterDecodeCompMs; }
    [[nodiscard]] double noMeterConfidenceGate() const noexcept { return config_.data().noMeterConfidenceGate; }
    [[nodiscard]] double noMeterPushReleaseWindowMs() const noexcept { return config_.data().noMeterPushReleaseWindowMs; }
    [[nodiscard]] QString noMeterHandedness() const noexcept { return config_.data().noMeterHandedness; }
    [[nodiscard]] bool meterDelayEnabled() const noexcept { return config_.data().meterDelayEnabled; }
    [[nodiscard]] int meterDelayMs() const noexcept { return config_.data().meterDelayMs; }
    [[nodiscard]] bool meterDelayBypassOnDefense() const noexcept { return config_.data().meterDelayBypassOnDefense; }
    // [ORION_METER_DELAY_AVAILABILITY] See the Q_PROPERTY note. Lazily probes once and
    // caches; safe to call from QML binding evaluation (the probe is one sc.exe query
    // plus two file stats, first call only).
    [[nodiscard]] bool meterDelayBackendAvailable() const;
    [[nodiscard]] QString meterDelayStatusText() const;
    // [ORION_METER_DELAY_ARM_STATE / VENICENET WAVE 2B] The bridge service's own
    // report of its arm state, sourced from the VeniceNet DLL snapshot (the
    // actuation path), never inferred from the actuator's ramp book-keeping. The
    // DLL reports DRIVER_NOT_STARTED when it is connected to a service whose
    // intercept is disarmed (empty hello features / the meter_delay_disarmed
    // refusal) — that is the "connected but not applying" case the card surfaces.
    [[nodiscard]] bool meterDelayServiceDisarmed() const noexcept
    {
        return veniceNet_.available()
            && veniceNet_.driverState() == VENICENET_DRIVER_NOT_STARTED;
    }
    // Pure decision core of the availability probe, exposed for regression tests: the
    // bridge backend is present when the elevated service is installed OR the debug
    // script exists in either launch root. Static so the test suite never has to
    // construct a controller (see MeterDelaySettingsPropertyTests.cpp header).
    [[nodiscard]] static bool packetBridgeBackendPresent(const QString& rootDir,
                                                         const QString& appDir,
                                                         bool serviceInstalled);
    // [ORION_METER_DELAY_LINK 2026-08-08, VENICENET WAVE 1] THE predicate for "should
    // the packet-bridge client run" — shared by the constructor start,
    // ensurePacketBridgeRunning()'s gate and meterDelayStatusText()'s
    // bridgeLinkConfigured input so the three can never disagree. The passive-sniffing
    // opt-in flag is DELETED (owner decision: everything network-side ships on by
    // default), so the link comes up whenever the network feature OR Meter Delay is
    // enabled — both default ON. Meter Delay remains BY ITSELF sufficient, closing the
    // silent-no-op trap where the toggle saved, logged and did NOTHING. Static +
    // argument-driven so MeterDelaySettingsPropertyTests can pin the predicate
    // without a controller.
    [[nodiscard]] static bool packetBridgeLinkConfigured(bool networkEnabled,
                                                         bool meterDelayEnabled) noexcept
    {
        return networkEnabled || meterDelayEnabled;
    }
    // [ORION_METER_DELAY_ARM_STATE] Pure decision core of meterDelayStatusText(),
    // exposed for regression tests (static: the suite never constructs a controller).
    // Precedence is the honesty order: backend absent > user toggle off > the
    // service's link/arm state > the actuator's ramp state. The actuator keeps
    // book-keeping its ramp even when nothing downstream holds a packet, so its
    // "Active — holding" must never outrank a disarmed or disconnected service.
    struct MeterDelayStatusInputs {
        bool backendAvailable = false;
        bool enabled = false;
        NetworkBridge::MeterDelayServiceState serviceState =
            NetworkBridge::MeterDelayServiceState::Unknown;
        bool bridgeConnected = false;
        // packetBridgeLinkConfigured(): networkEnabled || meterDelayEnabled — the
        // same predicate that decides whether the bridge
        // worker is started (constructor / setPacketCaptureEnabled /
        // setMeterDelayEnabled), so the status line and the actual link behaviour
        // can never disagree. With it false the "waiting for connection" state
        // would silently never end, so the status line must say the link is off.
        bool bridgeLinkConfigured = false;
        MeterDelayController::SessionState session = MeterDelayController::SessionState::Idle;
        MeterDelayController::ShotState shot = MeterDelayController::ShotState::Standby;
        double appliedMs = 0.0;
        double targetMs = 0.0;
        QString reason;
        // [ORION_METER_DELAY_ECHO 2026-08-08] What the bridge service itself last
        // reported applying (NetworkBridge::meterDelayServiceEcho). < 0 = no echo
        // received on this connection. Display-only confirmation: when present, the
        // "Active — holding" line quotes it so the card shows the service's own
        // number beside the controller's book-keeping instead of only the latter.
        double serviceAppliedMs = -1.0;
    };
    [[nodiscard]] static QString meterDelayStatusLine(const MeterDelayStatusInputs& in);
    [[nodiscard]] bool showSkeleton() const noexcept { return config_.data().showSkeleton; }
    [[nodiscard]] bool showLiveMeterMetrics() const noexcept { return config_.data().showLiveMeterMetrics; }
    [[nodiscard]] QString activeShotType() const noexcept { return config_.data().activeShotType; }
    [[nodiscard]] double minHoldMs() const noexcept { return config_.data().minimumHoldMs; }
    [[nodiscard]] double maxHoldMs() const noexcept { return config_.data().maximumHoldMs; }
    [[nodiscard]] int stableFrames() const noexcept { return config_.data().stableFrames; }
    [[nodiscard]] QString visionPipeline() const noexcept { return visionPipeline_; }
    [[nodiscard]] QString settingsSnapshot() const noexcept { return settingsSnapshot_; }

    [[nodiscard]] int playerCount() const noexcept { return playerCount_; }
    [[nodiscard]] int legitPlayerCount() const noexcept { return legitPlayerCount_; }
    [[nodiscard]] int cheaterCount() const noexcept { return cheaterCount_; }

    Q_INVOKABLE void authenticate(const QString& key);
    Q_INVOKABLE void checkBackend();
    // Hands off to OrionUpdater.exe (manifest URL + this PID + install dir) and
    // quits so the updater can replace files. Called by the startup update gate
    // (auto-proceed) or an explicit UI action. Refuses in a dev (build-tree)
    // launch so an update can never overwrite a source checkout.
    Q_INVOKABLE void startUpdate();
    // Dismiss the startup update gate without applying. Honored only for an
    // optional update ("offer") or a dev build; a mandatory/blocked gate ignores it.
    Q_INVOKABLE void continueWithoutUpdate();
    // Defense Mode toggle (also fired by the configured physical pad button while
    // a stream is live). Disarms the engine, flips the pill + lightbar color.
    Q_INVOKABLE void toggleDefenseMode();
    // Per-jumpshot profiles: saveProfile captures the current setup (meter style/
    // color, stream mode, network baseline + freeform notes) under `name`;
    // switchProfile applies a profile's setup AND swaps the learning namespace so
    // each build keeps its own calibration.
    Q_INVOKABLE void saveProfile(const QString& name, const QString& notes);
    Q_INVOKABLE void switchProfile(const QString& name);
    Q_INVOKABLE void deleteProfile(const QString& name);
    // Leave safe mode after the user reviewed the failure; clears watchdog
    // counters and re-arms automation (unless Defense Mode holds it disarmed).
    Q_INVOKABLE void exitSafeMode();
    // One-click support bundle: writes a redacted zip to the Desktop and
    // returns its path ("" on failure; reason is in the log).
    Q_INVOKABLE QString exportDiagnostics();
    // [VENICE_PROFILE 2026-08-08] Export/import of venice-profile.json — the customer
    // timing surface (Shot Lead, Tip Timing, Meter Delay, press-anchored constants).
    // Deliberately ONE slot + ONE signal (veniceProfileActionCompleted): action is
    // "export" or "import", fileUrl comes from the Debug page's FileDialog. All policy
    // (allowlist, clamps, never-export-license) lives in VeniceProfile.h; this method
    // only does file I/O plus the learning-before-settings persist ordering.
    Q_INVOKABLE void runVeniceProfileAction(const QString& action, const QUrl& fileUrl);
    // Opens the logs folder in Explorer so the user can grab crash dumps/logs
    // directly when reporting a bug.
    Q_INVOKABLE void openLogsFolder();
    Q_INVOKABLE void detectPs5();
    // First-run onboarding: mark the post-auth Stream Setup screen as completed so the
    // launcher proceeds straight to the app shell on subsequent launches.
    Q_INVOKABLE void markStreamSetupComplete();
    // First-run preflight wizard: mark finished/skipped so it stops auto-opening.
    // (Still re-runnable on demand from the Setup Guide.)
    Q_INVOKABLE void markPreflightComplete();
    // One-click "copy key": puts the activated license key on the clipboard (for
    // support tickets / /hwid_reset). The key itself never transits QML.
    Q_INVOKABLE void copyLicenseKey();
    // One-click "Copy all" for the Activity view: puts the bounded TAIL of
    // the on-disk engineer log (orion_native.log; 256 KiB / 1500 lines,
    // line-aligned — see UiNotificationPolicy.h) on the clipboard so the
    // user can paste it into a support ticket or an AI chat instead of
    // screenshotting the log. Falls back to the 1000-line in-memory ring when
    // the disk log is unreadable. Every copied line runs the sharing
    // redaction pass first; like copyLicenseKey(), the text goes straight to
    // the clipboard from C++. History older than the tail bound is reachable
    // via openLogsFolder().
    Q_INVOKABLE void copyActivityLog();
    // AuthGate consumed the deep-linked key into its field; drop the pending copy.
    Q_INVOKABLE void clearPendingActivationKey();
    // orion://activate?key=... handler (initial argv or a WM_COPYDATA forward from a
    // second instance — see main.cpp). Validates format, then stages the key for
    // AuthGate to pre-fill. No-op with a log line when already authenticated.
    void applyActivationDeepLink(const QString& uri);
    Q_INVOKABLE void acceptLegalAgreement();
    void setVideoSource(const QString& value);
    void setCaptureCardIndex(int value);
    void setCaptureCardFps(int value);
    void setHardwareDecode(bool value);
    void setControllerType(const QString& value);
    void setAutoReconnect(bool value);
    // One-click setup preset: sets bandwidth/render/audio/input/lightbar for a
    // platform/quality in one tap. name = "PlayStation" | "Quality" | "Performance".
    Q_INVOKABLE void applyPreset(const QString& name);
    Q_INVOKABLE void openChiaki();
    Q_INVOKABLE void updateChiakiEmbedRect(int x, int y, int width, int height);
    Q_INVOKABLE void setChiakiEmbedVisible(bool visible);
    Q_INVOKABLE void openPsnLogin();
    Q_INVOKABLE void saveProfileFromRedirect();
    Q_INVOKABLE void refreshProfiles();
    Q_INVOKABLE void registerPin();
    Q_INVOKABLE void connectRemotePlay();
    // Start the live capture-card preview (HDMI feed shown before Connect). Called from the
    // dashboard on load and again after a disconnect; a safe no-op unless the capture-card source
    // is selected and no session is up.
    Q_INVOKABLE void startCapturePreview();
    // QML calls this only after a buffered image:// request reaches Image.Ready.
    // The requested serial joins the visible texture to its exact meter
    // box/visibility/frame-size snapshot. Delayed monotonic frames are valid;
    // duplicate, out-of-order, future, and evicted completions are ignored.
    Q_INVOKABLE void acknowledgeRemoteFramePresented(int serial);
    // Visible QML preview pacing is synchronized to Qt Quick's render clock.
    // When inactive, RemotePlaySession restores its exclusive timer fallback.
    Q_INVOKABLE void setPreviewRenderClockActive(bool active);
    Q_INVOKABLE void advanceRemotePreviewPresentation();
    // synchronous=true runs the blocking sidecar/stream-client teardown inline (used by the
    // internal restart paths that reconnect immediately); the default defers it one event-loop
    // tick so the UI paints "Disconnecting…" before the freeze.
    Q_INVOKABLE void disconnectRemotePlay(bool synchronous = false);
    Q_INVOKABLE void saveRemoteSettings();
    Q_INVOKABLE void saveTimingSettings();
    Q_INVOKABLE double shotTypeOffset(const QString& type) const;
    Q_INVOKABLE void setShotTypeOffset(const QString& type, double value);
    // Per-type shot-mode override: "auto" | "normal" | "tempo" (auto/absent = global tempo toggle).
    Q_INVOKABLE QString shotTypeMode(const QString& type) const;
    Q_INVOKABLE void setShotTypeMode(const QString& type, const QString& mode);
    // Legacy diagnostic compatibility; the shipped Tempo UI has no per-type
    // gesture selector and this value is not runtime actuation authority.
    Q_INVOKABLE QString tempoRemapTypeForShot(const QString& shotType) const;
    // Auto-calibration mode (the feedback-text oracle dials per-type offsets under freeze).
    Q_INVOKABLE bool calibrationMode() const;
    Q_INVOKABLE void setCalibrationMode(bool enabled);
    Q_INVOKABLE void refreshSecurity();
    Q_INVOKABLE void verifyReleaseIntegrityNow();
    Q_INVOKABLE void clearInvalidLocalEntitlement();
    Q_INVOKABLE void signSettings();
    // [RT-MED-09] Scoped customer repair: only acts while the settings signature is invalid;
    // keeps the rejected file aside and saves + signs factory defaults. Never re-signs it.
    Q_INVOKABLE void repairSettings();
    Q_INVOKABLE void connectVirtualController();
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] One-time, USER-CONSENTED fix:
    // writes EnhancedPowerManagementEnabled=0 on every known Sony pad USB
    // instance so hidusb stops idling the pad into the firmware-wedging
    // suspend (rig-verified drop-off-USB cause). Explicit button action only —
    // never called automatically. Takes effect after the pad is re-plugged.
    Q_INVOKABLE void applyControllerUsbPowerFix();
    Q_INVOKABLE void refreshControllerDevices();
    // Enumerate DirectShow video-capture devices (name-ordered, index == cv2 CAP_DSHOW index) so the
    // Stream Setup UI can show a named picker ("Elgato HD60 X") instead of a blind device number.
    Q_INVOKABLE void refreshCaptureDevices();
    Q_INVOKABLE void applyLightbarNow();
    Q_INVOKABLE void openWindowsGameControllersPanel();
    Q_INVOKABLE void clearLogs();
    Q_INVOKABLE void recalibrateShotType(const QString& type);
    Q_INVOKABLE void lockShotType(const QString& type);
    // Train the meter TEMPLATE ANCHOR from the live frame. Selections are frame
    // FRACTIONS (0..1), resolution-independent. Returns "" on success or an error
    // message for the UI. Saves a template PNG + offset into signed settings.json.
    Q_INVOKABLE QString trainMeterAnchor(double anchorXFrac, double anchorYFrac,
                                         double anchorWFrac, double anchorHFrac,
                                         double meterXFrac, double meterYFrac,
                                         double meterWFrac, double meterHFrac);
    Q_INVOKABLE void clearMeterAnchor();
    Q_INVOKABLE bool meterAnchorTrained() const;
    // Shoot-to-train (no boxing): start a calibration window — the user shoots
    // meterCalibrationTarget times while the detector auto-learns the meter colour + green
    // hue, then it locks. Cancel aborts. State is read via the meterCalibration* properties.
    Q_INVOKABLE void startMeterCalibration();
    Q_INVOKABLE void cancelMeterCalibration();
    // [ORION_PROBE] warmup pump-fake latency probes: 8 virtual Square presses (150ms hold,
    // 2.5s apart, tick-phase staggered) while the engine is Idle. Converges the latency
    // oracle before shot #1. User-triggered in a shootable venue (MyCourt/warmup) ONLY —
    // the presses are visible pump fakes in-game. Any physical Square press cancels.
    //
    // Invokable as of the probe wiring: the method has existed since the fused rule was
    // written and had NO caller anywhere, so measuredLatencySdMs_ and tickPhaseConf_ have
    // never left their sentinels on any machine. That left the fused fire path permanently
    // unable to own a shot (fusedOwns requires measuredLeadAuthoritative()) and the tick snap
    // permanently skipped -- both failing silently to factory priors rather than to a warning.
    Q_INVOKABLE void runLatencyProbes();
    [[nodiscard]] QString latencyProbeSummary() const;
    // Diagnostic-only timing probes. Production starts from the signed exact-route model and
    // refines from delivered gameplay shots; these methods are deliberately not QML invokables.
    void startLatencyCalibration();
    void cancelLatencyCalibration();
    [[nodiscard]] bool meterCalibrating() const noexcept { return meterCalibrating_; }
    [[nodiscard]] int meterCalibrationShots() const noexcept { return meterCalShots_; }
    [[nodiscard]] int meterCalibrationTarget() const noexcept { return meterCalTarget_; }
    [[nodiscard]] QString meterCalibrationStatus() const noexcept { return meterCalStatus_; }
    [[nodiscard]] QString meterCalibrationState() const noexcept { return meterCalState_; }
    [[nodiscard]] QString meterCalibrationLearnedDate() const noexcept { return meterCalLearnedDate_; }
    [[nodiscard]] bool latencyCalibrationActive() const noexcept
    {
        return latencyCalibrationActive_;
    }
    [[nodiscard]] bool latencyCalibrationReady() const noexcept
    {
        return latencyCalibrationReady_;
    }
    [[nodiscard]] int latencyCalibrationSamples() const noexcept
    {
        return latencyCalibrationSamples_;
    }
    // L1 measurement plus a distinct in-green L2 validation; refinement continues online.
    [[nodiscard]] int latencyCalibrationTarget() const noexcept { return 2; }
    [[nodiscard]] double latencyCalibrationMeasuredLeadMs() const noexcept
    {
        return latencyCalibrationMeasuredLeadMs_;
    }
    [[nodiscard]] double latencyCalibrationSdMs() const noexcept
    {
        return latencyCalibrationSdMs_;
    }
    [[nodiscard]] QString latencyCalibrationStatus() const noexcept
    {
        return latencyCalibrationStatus_;
    }
    [[nodiscard]] QString calibrationSnapshot() const;
    // Button and Square-triggered Tempo use the same per-shot-type timing bucket.
    [[nodiscard]] QString shotBucketKey(const QString& type) const;

public slots:
    void setCurrentPage(const QString& page);
    void setConsoleIp(const QString& value);
    void setRemotePlayConsole(const QString& value);
    void setChiakiPath(const QString& value);
    void setProfileUser(const QString& value);
    void setPin(const QString& value);
    void setRedirectUrl(const QString& value);
    void setTimingLatencyMs(double value);
    void setReleaseThresholdPct(double value);
    void setEarlyLateOffsetMs(double value);
    void setTempoInputSource(const QString& value);
    void setTempoRemapType(const QString& value);
    void setTempoWaitMs(double value);
    void setRhythmFlickDelayMs(double value);
    void setTempoFallbackMs(double value);
    void setTempoFlickHoldMs(double value);
    void setTempoReleaseStyle(const QString& value);
    void setTempoMinStickHoldMs(double value);
    void setRttSyncMode(const QString& value);
    void setPacketCaptureEnabled(bool value);
    void setStreamBandwidthMode(const QString& value);
    void setStreamAudioEnabled(bool value);
    void setStreamAudioMode(const QString& value);
    void setControllerLightbarEnabled(bool value);
    void setControllerLightbarColor(const QString& value);
    void setControllerLightbarMode(const QString& value);
    void setControllerLightbarPrimaryColor(const QString& value);
    void setControllerLightbarSecondaryColor(const QString& value);
    void setControllerLightbarBrightness(double value);
    void setControllerLightbarEffectSpeed(double value);
    void setMeterOverlayColor(const QString& value);
    void setMeterOverlayStyle(const QString& value);
    void setMeterOverlayRgb(bool value);
    void setManualSyncAdjustMs(double value);
    void setManualOffsetMs(double value);
    void setMeterStyle(const QString& value);
    void setMeterProposer(const QString& value);
    void setMeterColor(const QString& value);
    void setMeterEnabled(bool value);
    void setAutoTune(bool value);
    void setAutoMeterColor(bool value);
    void setDetectionConfidencePercent(int value);
    void setTempoEnabled(bool value);
    [[nodiscard]] QString tempoInputPath() const;
    void setTempoInputPath(const QString& value);
    void setNoDipEnabled(bool value);
    void setNoDipLeadMs(double value);
    // [ORION_USER_LEAD] Writing the Shot Lead is always an explicit USER act — it clamps into
    // [min,max] and latches actuationLeadUserSet, after which the measured seed can never move it.
    void setActuationLeadMs(double value);
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Writing the delayed-condition offset. Clamped
    // into the setting's own signed band; deliberately NOT clamped to the schedulable ceiling —
    // the codebase's standing doctrine for this pair is that an abort is fail-closed while a
    // silently re-timed release is a guaranteed mistime, so an over-ceiling offset aborts loudly
    // through the existing unschedulable path and the UI warns with meterDelayLeadOffsetMaxMs.
    void setMeterDelayLeadOffsetMs(double value);
    // [ORION_LEAD_CALIBRATION] Human-as-oracle: the user reads the game's TIMING banner, which is
    // the only validated outcome signal in this project and the one thing the bot cannot read.
    Q_INVOKABLE void beginLeadCalibration();
    Q_INVOKABLE void cancelLeadCalibration();
    Q_INVOKABLE void reportLeadCalibrationVerdict(const QString& verdict);
    [[nodiscard]] bool leadCalibrationActive() const noexcept { return leadCalActive_; }
    [[nodiscard]] bool leadCalibrationLocked() const noexcept { return leadCal_.locked; }
    [[nodiscard]] int leadCalibrationShots() const noexcept { return leadCal_.shots; }
    [[nodiscard]] double leadCalibrationLeadMs() const noexcept { return leadCal_.leadMs; }
    [[nodiscard]] double leadCalibrationStepMs() const noexcept { return leadCal_.stepMs; }
    [[nodiscard]] QString leadCalibrationHint() const;
    // Single-digit-ms trim buttons: a 300px slider over a 300ms range makes 1ms unhittable, and
    // 1ms is exactly the granularity a user converges at once they are close.
    Q_INVOKABLE void nudgeActuationLeadMs(double deltaMs);
    // Back to "not configured": clears the value AND the user-set latch, so this install's own
    // measurement is free to seed the control again.
    Q_INVOKABLE void resetActuationLead();
    // [ORION_USER_TIP] Tip Timing accessors/actions — see the Q_PROPERTY block for the
    // precedence contract and the frame arithmetic (all conversions go through the shared
    // helpers in AppConfig.h; nothing here re-derives them).
    [[nodiscard]] double tipTimingMs() const;
    [[nodiscard]] bool tipTimingUserSet() const noexcept { return config_.data().tipTimingUserSet; }
    [[nodiscard]] bool tipTimingLocked() const noexcept { return config_.data().tipPhaseAimFrozen; }
    [[nodiscard]] bool tipTimingLearnedActive() const noexcept
    {
        return config_.learning().learnedPhasePhysicalMs > 0.0;
    }
    [[nodiscard]] double tipTimingMinMs() const;
    [[nodiscard]] double tipTimingMaxMs() const;
    // [ORION_AIM_FREEZE] The instrument's own persisted measurement (learning.json
    // measured_phase_physical_ms) in the effective frame; -1 = none. See the Q_PROPERTY note.
    [[nodiscard]] double tipTimingMeasuredMs() const;
    void setTipTimingMs(double effectiveMs);
    void setTipTimingLocked(bool locked);
    Q_INVOKABLE void nudgeTipTimingMs(double deltaMs);
    // Hands the aim back to the learner: clears the lock AND the user-set marker. The current
    // value stays as the learner's prior (continuity — no aim cliff), and the learner is free
    // to move from it on its very next accepted landing.
    Q_INVOKABLE void resetTipTiming();
    void setNoMeterEnabled(bool value);
    void setNoMeterReleasePoint(const QString& value);
    void setNoMeterBaseOffsetMs(double value);
    void setNoMeterDecodeCompMs(double value);
    void setNoMeterConfidenceGate(double value);
    void setNoMeterPushReleaseWindowMs(double value);
    void setNoMeterHandedness(const QString& value);
    // [ORION_METER_DELAY] QML setters for the Meter Delay card. Each writes through
    // AppConfig (persist + settingsChanged via saveConfigSilently) and then re-primes
    // the live actuator via applyMeterDelayRuntimeConfig().
    void setMeterDelayEnabled(bool value);
    void setMeterDelayMs(int value);
    void setMeterDelayBypassOnDefense(bool value);
    void setShowSkeleton(bool value);
    void setShowLiveMeterMetrics(bool value);
    void setActiveShotType(const QString& value);
    void setMinHoldMs(double value);
    void setMaxHoldMs(double value);
    void setStableFrames(int value);

signals:
    void authChanged();
    void leaseNoticeChanged();   // [CL2-P8-002 2026-09-23]
    void motdChanged();
    // Profile block refreshed (activation or heartbeat). Its own low-fanout
    // notifier: the Profile page must not re-evaluate on every broad status tick.
    void profileChanged();
    // A deep-linked activation key arrived/was consumed (orion://activate).
    void pendingActivationKeyChanged();
    void navigationChanged();
    void statusChanged();
    void manualShotTallyChanged();
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14] The live shot-verdict tally moved: one graded
    // banner arrived, or the window was cleared (session start/end, a committed slider
    // change, the card's Reset). Low fan-out on purpose - it fires about once per shot and
    // only the two ShotVerdictTally mounts listen.
    void bannerTallyChanged();
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] The banner loop's trim moved (an application, a reset
    // on a committed Shot Lead change, or the restore at app start). Its own low fan-out
    // notifier for the same reason bannerTallyChanged has one: it fires about once per shot and
    // only one caption listens.
    void bannerLeadTrimChanged();
    // [ORION_LEAD_AUTO_SEED 2026-09-15] Its own low-fanout notifier, like the trim's: it changes
    // about once per shot while an untuned install converges and has exactly one subscriber.
    void leadAutoSeedChanged();
    void leadCalibrationChanged();
    void sessionTimeChanged();
    void statusAgeChanged();
    // Fired only when the startup update gate actually changes phase.
    void updateGateChanged();
    void telemetryChanged();
    void settingsChanged();
    // [ORION_METER_DELAY_AVAILABILITY] Fires at most once per launch, when the packet
    // bridge availability probe first resolves (the result is then cached for the
    // session). Exists so the QML binding is notify-backed rather than a silent
    // one-shot read.
    void meterDelayBackendAvailabilityChanged();
    // Live meter-delay actuator state for the Meter Delay card; re-emitted whenever
    // MeterDelayController::stateTextChanged fires.
    void meterDelayStatusTextChanged();
    void captureDevicesChanged();
    void frameChanged();
    // Dedicated low-fanout notification for measured HUD values. In particular,
    // the expiry timer emits this even when the decoder/frame stream has stalled.
    void meterMetricsChanged();
    // Dedicated, hard-throttled notification for the meter-side telemetry HUD.
    // Kept separate from meterMetricsChanged so the overlay's ~11 bindings never
    // ride the detector/status fan-out, and so turning the overlay off removes
    // its cost from the GUI thread entirely.
    void meterHudChanged();
    // The overlay stroke colour changed. Kept off settingsChanged because RGB
    // mode fires this ~4x/second and settingsChanged re-evaluates every settings
    // binding in the application. This one has exactly two subscribers: the lock
    // box stroke and the telemetry strip's border.
    void meterOverlayDrawColorChanged();
    // Low-fanout notification for the compact timing-calibration card.
    void latencyCalibrationChanged();
    // [ORION_USER_LEAD] the measured end-to-end lead readout moved. Kept off settingsChanged
    // because it updates once per landed shot and has exactly one subscriber (the Shot Lead card).
    void actuationLeadMeasurementChanged();
    // [ORION_USER_TIP] the Tip Timing card's low-fanout notification. Emitted from
    // syncBackendConfig() (covers every settings write, profile switch, and regime flip) and
    // from the learner's own persist handler (phaseConstantUpdated), so the card tracks the
    // learner at the cadence the learner publishes without riding the wide settingsChanged
    // fan-out or any per-tick signal.
    void tipTimingChanged();
    // [ORION_LEAD_CONFLICT 2026-08-08] Low-fanout notifier for the engine-fed lead diagnostics
    // (conflict miss count, validated-authority disagreement stats). The banner VISIBILITY is
    // computed reactively in QML from settingsChanged/tipTimingChanged-notified values; this
    // signal only refreshes the engine-only facts those banners quote.
    void leadDiagnosticsChanged();
    // The meter overlay box moved/cleared — SEPARATE from statusChanged so a per-detection box
    // update repaints only the ~4 box bindings, not the 98-property statusChanged fan-out.
    void meterBoxChanged();
    // Meter-blind safety net toggled. Its own signal (not statusChanged) because it changes at
    // most once every few shots and has exactly one subscriber (the Meter Profile status line).
    void meterBlindChanged();
    // Reader detector health line refreshed (~2 s cadence from the sidecar). One subscriber
    // (the Meter Detection card), so it stays off statusChanged/settingsChanged.
    void detectorHealthChanged();
    void poseOverlayChanged();
    // [PRESSED OVERLAY 2026-08-08] The raw physical Square-held state flipped.
    // Emitted synchronously from the input poll (syncPressedOverlay), on the same
    // tick as the "Physical shot epoch" record. Exactly one subscriber (the
    // PressedBadge on the Live Capture preview), so it stays off statusChanged.
    void physicalSquarePressedChanged();
    // [ORION_INPUT_DEAD_UX 2026-08-30] The input-dead overlay verdict (severity or its
    // headline/detail text) changed. Own signal so a per-press latch flip repaints only the
    // overlay bindings, not the statusChanged fan-out.
    void inputDeliveryStateChanged();
    // [VENICE_PROFILE 2026-08-08] Result line for the Debug page's Venice Profile card
    // (runVeniceProfileAction). Exactly one subscriber; the import's settings fan-out
    // still rides settingsChanged via saveConfigSilently as usual.
    void veniceProfileActionCompleted(bool ok, const QString& summary);
    void logsChanged();

private:
    [[nodiscard]] bool localDevAllowed() const;
    [[nodiscard]] QString stateText(RemotePlayState state) const;
    [[nodiscard]] QString holdStateText(HoldState state) const;
    [[nodiscard]] QString formatHelperError(const QString& operation, const QJsonObject& result) const;
    [[nodiscard]] QString rectSummary(const QRect& rect) const;
    void appendLog(const QString& message);
    // [ORION_ACTIVITY_FEED 2026-09-14] One plain-language customer event. Writes
    // the SAME text to the optional orion_user.log sink (no-op while the
    // ORION_USER_LOG flag is off) and through appendLog, so it lands in the
    // Activity ring and the diagnostic log without inventing a second wording.
    void appendCustomerEvent(const QString& plainText);
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-07] True when a real HID input report
    // has landed in the last 3 s. Distinct from `rawInputPresent_`, which goes
    // true on mere enumeration (pollPhysicalController's findRawInputController
    // path — OrionAppController.cpp:9419). The Connect gates use this instead of
    // `rawInputPresent_` so a pad that enumerated but never sent a HID report
    // can't take Remote Play into a hung "connected but no input" state.
    [[nodiscard]] bool hasRecentRawInput() const noexcept;
    // [ORION_PAD_SILENT_HOLD 2026-09-11] Open the selected pad's HID collection and poll one
    // input report (the D0 resume + firmware nudge the connect gate relies on), without the
    // confirm loop. Returns true when the collection opened; the out-params report both steps.
    bool nudgePhysicalPadOnce(bool* opened, bool* answered);
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Connect-click recovery for an
    // enumerated-but-silent pad (padConnectGateAction ==
    // WakeProbeThenRecheck): open the HID collection (forces a USB
    // selective-suspend resume), issue one best-effort synchronous input
    // report poll, then wait briefly for the dedicated input thread to decode
    // a REAL report. Returns true ONLY once hasRecentRawInput()/
    // physicalPadLive_ hold again — the probe itself never admits, so the
    // dead-pad refusal the 2026-08-07 gate introduced is preserved. Blocking
    // (<= ~700 ms + one HID ioctl); call only from the user-clicked Connect
    // path, never from the 4 ms poll tick.
    [[nodiscard]] bool wakePhysicalPadAndConfirmReport();
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Registry-history scan feeding the
    // absent-pad advice + the consented USB power fix offer (UsbPadPowerPolicy.h):
    // walks HKLM Enum\USB for Sony pad instances (history survives the pad
    // dropping off the bus AND app restarts) and records which instances still
    // carry EnhancedPowerManagementEnabled=1. Cheap (one registry walk); run at
    // startup, on Connect refusal, and after the fix is applied.
    void refreshControllerUsbPowerScan();
    void flushPendingLogs();
    void clearMeterMetrics(bool forceNotify = false);
    void armMeterMetricsExpiry();
    void refreshMeterTargetEtaSnapshot();
    // Rebuilds the batched meter-side HUD snapshot from the ShotContext that was
    // copied earlier in THIS input tick (so every field shares one instant) and
    // emits meterHudChanged() at most once per kMeterHudNotifyIntervalMs.
    // Returns immediately when the overlay is switched off.
    void refreshLiveMeterTelemetry();
    void observeBotOwnership(const ShotContext& context);
    // Meter-blind safety net. Called once per sidecar detection frame with the CV-INDEPENDENT
    // physical shot epoch and whether THIS frame was a genuine raw meter detection.
    void observeMeterBlindness(quint64 physicalShotEpoch, bool genuineRawDetection);
    // [RT-MED-04 2026-09-23] Engine meterPressUnanswered relay: one completed METER press the
    // meter never answered.
    void observeMeterPressUnanswered(quint64 physicalShotEpoch, double holdMs);
    // Mirrors meterBlindLatch_ into the two bools, the engine gate, the log and the signal.
    void applyMeterBlindLatchEvent(MeterBlindnessLatch::Event event);
    // Formats one sidecar {"event":"detector_health"} object into detectorHealthLine_ /
    // detectorProvider_. Presentation only — reads nothing from and writes nothing to the
    // engine.
    void observeDetectorHealth(const QJsonObject& health);
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14] One graded shot off the game's own feedback
    // banner (RemotePlaySession::bannerVerdict). Buckets it into the rolling window and
    // writes one plain Activity line. Presentation only - reads nothing from and writes
    // nothing to the engine.
    // [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] `hasCoverage` is the panel's LAYOUT (did it have
    // a coverage CELL at all), forwarded to the engine's trim; defaulted TRUE so an older sidecar
    // keeps the 2026-09-18 strict coverage gate.
    void observeBannerVerdict(const QString& timing, const QString& timingColor,
                              const QString& coverage, double ncc, qint64 frameEpochMs,
                              int seq, int attributed, qint64 releaseSeq,
                              double releaseDelayMs, bool hasCoverage = true);
    // [ORION_RELEASE_ORACLE_TRIM 2026-09-15] One post-release retraction measurement off the
    // reader (RemotePlaySession::releaseOracle) -- the banner-free input to the SAME bounded Shot
    // Lead trim. Unlike the tally this reaches the engine; see the definition for the fences.
    void observeReleaseOracle(qint64 releaseSeq, double gapPx, const QString& proxy);
    // [ORION_SHOT_RANGE 2026-09-17] One THREE/MID reading off the sidecar
    // (RemotePlaySession::shotRange). Forwarded straight to the engine, which fences it on the
    // live press's own shot-gate epoch; see AutomationEngine::noteShotRange.
    void observeShotRange(qint64 releaseSeq, const QString& range, double conf);
    // Clears the window AND the de-dupe watermark: a new session restarts the sidecar's
    // verdict counter at 1. Called on stream start and on disconnect/error.
    void resetBannerTallyForSession();
    // First update-check result arms (offer/force) or clears the startup gate;
    // later checks only refresh the status pill (no mid-session re-block).
    void resolveUpdateGate(UpdateGateAction action);
    // Silent updater: when a build is available and the session is idle (not
    // streaming), apply it immediately (download → verify → relaunch). Deferred
    // while a stream is live; the periodic re-check retries when idle.
    void maybeAutoApplyUpdate();
    // Defense Mode assists applied to the outgoing pad state (stick response
    // curve, L2-intent hold, sprint hold). Pass-through channels only — never
    // touches the shot button.
    void applyDefenseAssists(ControllerState& output) const;
    // Watchdogs: any trip neutral-submits + disarms the engine FIRST, then
    // attempts one recovery; a repeat within the window escalates to safe mode.
    void onWatchdogTick();
    void tripWatchdog(const QString& reason);
    void enterSafeMode(const QString& reason);
    // Single source of truth for the engine arm gate (defense + safe mode +
    // fresh capture + verified recovery of an escalated direct-input fault).
    void syncEngineArmed();
    // [ORION_DEFENSE_FLAG 2026-08-08] The ONE feed for the meter-delay
    // shot-cycle hint — now recorded diagnostic state only (the hint is fully
    // decoupled from engagement; the D-pad Up manual defense flag is the sole
    // OffenseDefense gate). The engagement-decision log line
    //   Meter delay policy: bypass=<0/1> => <applying|bypassing>
    // moved to the hotkey handler, where the decision actually happens.
    void setMeterDelayShotCycle(bool active);
    // Publish passive route adaptation. This never arms diagnostic calibration
    // or asks the customer to perform setup shots.
    void refreshPassiveLatencyAdaptationStatus(bool routeReady);
    // Revoke any deadline already copied into the precise-fire worker. This is a
    // submit fence, not just engine-state cleanup: it linearizes against the
    // worker's final controller write before watchdog/disconnect paths neutral.
    // Safe to call from the GUI-freeze worker after automation_.setArmed(false).
    void disarmPreciseFire(bool confirmSubmitted = false);
    void fencePreciseFireToken(quint64 token, bool fallbackTakeover);
    void applyPendingPhaseAnchorRefinement();
    void confirmPreciseFire(quint64 token, double actualMs,
                            PreciseFireDeliveryStage stage, uint32_t transportSeq,
                            const PreciseFireDeliverySnapshot& snapshot);
    // Neutral both controller routes under the precise-fire submit fence.
    void neutralizeOwnedInput();
    // A terminal Remote Play error releases console ownership and destroys the
    // desktop-visible virtual target without stopping capture-only preview.
    void releaseFailedRemoteInputRoute();
    [[nodiscard]] bool automationSecurityAllowed() const noexcept;
    void handleRemoteFrame(const QImage& frame, int frameNumber);
    void clearRemotePreviewFrame();
    void pollPhysicalController();
    void registerRawInputController();
    // GUI-thread continuation of a WM_INPUT_DEVICE_CHANGE received on the raw-input
    // worker thread (selection/teardown state lives on the GUI thread).
    void handleRawInputDeviceChange(quintptr deviceHandle, quintptr changeKind,
                                    const QString& devicePath, bool removedActivePhysical);
    void updateLightbarEffect();
    // Schedule one lightbar refresh without performing HID I/O inside the 4 ms
    // controller poll. Solid mode consumes the request once; animated modes keep
    // their effect timer. A request made during a shot remains pending until the
    // first safe idle/cooldown timer tick.
    void requestControllerLightbarRefresh();
    // One step of the detection-overlay hue cycle. Cosmetic, GUI-thread, and
    // deliberately slow: see overlayEffectTimer_ for the cadence argument.
    void updateMeterOverlayEffect();
    // Start the hue cycle only when it can actually be seen (RGB mode on AND a
    // preview is being rendered), stop it otherwise, and snap the stroke back to
    // the user's chosen colour when it stops so the box is never left parked on
    // an arbitrary hue.
    void refreshMeterOverlayEffectTimer();
    // Machine-parseable single-token forms of the shot state / mode for telemetry
    // (holdStateText returns human strings with spaces like "Green Window", which a
    // log parser can't split on whitespace — these never contain spaces).
    [[nodiscard]] static QString holdStateToken(HoldState state);
    [[nodiscard]] static QString shotModeToken(ShotMode mode);
    // Collapse whitespace inside a value (device labels like "PlayStation HID pad")
    // to underscores so every telemetry value is a single space-free token.
    [[nodiscard]] static QString telemetryToken(const QString& raw);
    // Ownership instrumentation: prove whether Orion's virtual release actually held
    // the shot across the WHOLE release pulse. logShotStateTransition emits one line
    // per Idle->Armed->...->Cooldown transition; updateReleaseOwnershipTrace logs a
    // per-tick "Release tick:" line through Releasing/Cooldown and accumulates the
    // window; flushReleaseOwnershipTrace emits the "Release ownership:" summary.
    void logShotStateTransition(const ControllerState& physical, const ControllerState& output);
    void updateReleaseOwnershipTrace(const ControllerState& physical, const ControllerState& output,
                                     bool submitOk, qint64 nowMs);
    void flushReleaseOwnershipTrace();
    // [ORION_OUTPUT_DIVERGENCE 2026-09-14 owner] Evidence only. Compares the engine's final
    // output against the pad's own packet each tick and logs one line when a non-Square button
    // or trigger has disagreed continuously for kOutDivergeMinMs_, and one when it stops.
    void observeOutputDivergence(const ControllerState& physical, const ControllerState& output);
    void applyControllerLightbar(bool force = false);
    void setControllerLedStatus(const QString& status);
    void notifyTelemetryStatusAtHumanCadence(qint64 nowMs);
    void notifyTelemetryPropertiesAtHumanCadence(qint64 nowMs);
    void notifyControllerStatusAtHumanCadence(qint64 nowMs);
    void setCaptureSourceHealth(const QString& value);
    // Every sidecar restart may recreate OrionStream's native window. Extend the
    // containment grace and keep the 60 Hz hunter armed before tearing the old
    // process down, including capture-card input-only sessions.
    void restartSidecarWithWindowContainment();
    // Blocking part of disconnect (sidecar shutdown + stream-client cleanup + virtual-pad
    // unplug + final Disconnected state). Split out so disconnectRemotePlay can run it inline
    // or deferred. See disconnectRemotePlay().
    void finishRemotePlayTeardown();
    void unplugRemoteController();
    void finishMeterCalibration();
    // One monotonic release may contribute at most one user-visible grade. Both the
    // colour-window grade and calibration-only banner verdict route through here so
    // an optional calibration run cannot double-count the same shot.
    void recordSessionGrade(int releaseSeq, const QString& shotType,
                            const QString& verdict, double errorMs,
                            const QString& source);
    bool saveConfigSilently(const AppConfigData& data);
    void persistConfig(const AppConfigData& data, const QString& successMessage);
    void syncBackendConfig();
    // [ORION_METER_DELAY] The ONE mapping from persisted AppConfig to the live
    // MeterDelayController (enabled / manual delay / engage policy). Shared by the
    // constructor priming and the QML setters so a toggle can never take a different
    // runtime path than a restart would.
    void applyMeterDelayRuntimeConfig();
    // [ORION_TEMPO_BRIDGE_LIVE 2026-08-08 task #36] Push the tempo-remap bridge-live
    // inputs (delay engine engaged / service echo applying) into the AutomationEngine.
    void pushTempoRemapBridgeState();
    void applySecurityStatus(const SecurityStatus& status);
    void updateSecurityStatus();
    bool beginSignedSettingsSave();
    void updateRuntimeStatus();
    void ensurePacketBridgeRunning();
    bool startPacketBridgeDebug();
    // [ORION_PACKET_BRIDGE_NAMES 2026-08-08 task #64] The bridge service may be registered
    // as VeniceNetSvc (wave-3 installs) or NexusVisionSvc (legacy). Resolution consumes
    // venicenet::candidateServiceNames() via src/PacketBridgeServiceNames.h — VeniceNetSvc
    // is probed first, the legacy name second, and BOTH must keep working. Returns the
    // first registered name, or empty when neither is installed.
    [[nodiscard]] QString installedPacketBridgeServiceName() const;
    [[nodiscard]] bool packetBridgeServiceInstalled() const;
    // [ORION_METER_DELAY_AVAILABILITY] Single once-per-launch availability probe shared
    // by ensurePacketBridgeRunning()'s install guard and the meterDelayBackendAvailable
    // QML property. const because QML getters must be callable during binding
    // evaluation; the cache members are mutable for exactly this reason. Does NOT log
    // or emit — non-const call sites own those side effects.
    [[nodiscard]] bool probePacketBridgeAvailability() const;
    [[nodiscard]] QJsonObject readStatusJson() const;
    [[nodiscard]] QString tempoInputLabel(const QString& value) const;
    [[nodiscard]] QString textOrFallback(const QJsonObject& obj, const QStringList& keys, const QString& fallback = QStringLiteral("-")) const;
    [[nodiscard]] bool packetBridgeReachable(int timeoutMs = 180) const;
    [[nodiscard]] double networkAutomationOffset() const noexcept;
    [[nodiscard]] double networkAutomationJitter() const noexcept {
        return telemetry_.rttTargetVerified ? telemetry_.jitterMs : 0.0;
    }
    void setControllerLifecycle(ControllerLifecycleState state, const QString& status);

    QString rootDir_;
    QString iconSource_;
    OrderedFileLogSink appLogSink_;
    AppConfig config_;
    SecurityManager security_;
    PeriodicSecurityEvaluator periodicSecurityEvaluator_;
    SecurityEvaluationGenerationFence securityEvaluationFence_;
    LicenseClient licenseClient_;
    AutomationEngine automation_;
    MeterDetector detector_;
    RemotePlaySession remotePlay_;
    VirtualController controller_;
    // Pre-encryption input hook to the patched chiaki-ng (default ON; ORION_INPUT_HOOK=0 disables ->
    // ViGEm sole output). When enabled, the engine output is ALSO written to chiaki's pipe each tick
    // (pre-encryption = jitter-free release); ViGEm stays submitted as the live fallback.
    OrionInputClient orionInput_;
    bool squareOutputWatchdogEnabled_ = false;
    SquareOutputWatchdog squareOutputWatchdog_;
    // Caller holds submitMutex_; transport method also takes its IO fence.
    bool releaseStaleSquareOutputLocked(const QString& reason);
    // Hook write-coalesce: steady-clock microsecond stamp of the last PRECISE FIRE-THREAD pipe
    // write. The GUI tick (4ms cadence) skips its own hook write within this window of a fire so a
    // shot release emits ONE coalesced hook packet, not the fire write + an immediate duplicate
    // tick write. Cuts the release-time write burst that stalled chiaki's takion thread.
    std::atomic<qint64> lastFireHookWriteUs_{0};
    static constexpr qint64 kHookCoalesceUs_ = 3000;   // 3ms
    // Hook liveness heartbeat: last steady-clock ms an "Input hook heartbeat" line was logged (GUI
    // tick only, so no atomicity needed). writeCount/failures otherwise surface only on a release
    // tick, so a mid-session silent fallback to ViGEm would be invisible until the next shot.
    qint64 lastHookHeartbeatMs_ = 0;
    // Require a sustained digital-rest interval before the periodic synchronous liveness ACK.
    // Sticks are deliberately excluded: keeping a stick in motion must not prevent detection
    // of a wedged client-to-OrionStream route after a button/trigger release.
    qint64 hookDigitalRestSinceMs_ = -1;
    // [ORION_INPUT_RELEASE_REPAIR 2026-09-21] Every generated digital release schedules one
    // fresh-sequence MustDeliver route recheck after the primary packet, not a new logical
    // edge or console ACK: the re-assert uses the latest confirmed complete state.
    ControllerState hookReleaseRepairPreviousOutput_{};
    bool hookReleaseRepairHavePreviousOutput_ = false;
    qint64 hookReleaseRepairDueMs_ = -1;
    // [2026-09-22 RED TEAM CL-004] 24 -> 60 ms: the fork spaces a release's redundant copies at
    // +3 ms and +40 ms; a probe inside that window was the transaction that timed out in 2 of 10
    // incidents. 60 clears the echo with margin and is still well inside a human re-press.
    static constexpr qint64 kHookReleaseRepairDelayMs_ = 60;
    // Input-link recovery watchdog. Consecutive 1/s heartbeats seen with a Running
    // stream but no direct hook first trigger three input-child-only recoveries,
    // then one contained full restart after a final grace. A verified pipe write
    // or an explicit user session reset is required to clear terminal fail-closed.
    //
    // CHURN FIX (2026-07-18 live forensics, logs/orion_native.log 04:20-04:23): the input-hook
    // pipe (OrionStream direct-inject) routinely drops after a shot / never connects on some rigs,
    // while the ViGEm/XUSB fallback keeps submitting input AND video keeps flowing from the capture
    // card. The original Patch A restarted the sidecar on input-hook-down ALONE — but the restart
    // never recovered the pipe (the fresh OrionStream's pipe also stayed connected=0, writes=0), so
    // it looped every ~5s: kill+respawn sidecar + a NEW "Orion Stream" window each cycle (the user's
    // "multiple Chiaki windows" churn). An input-pipe drop with a live raw transport is NOT a
    // stream failure, so restarts are gated on transport_age_ms/backend_frozen telemetry.
    // Detector frame/pixel age remains fail-closed automation authority, never process-lifecycle
    // authority because static/loading content is valid.
    int hookDownHeartbeats_ = 0;
    // [CL2-P4-001 / CL2-P5-001 2026-09-22] Input-tick liveness: the 4 ms physical poll runs on the
    // GUI thread; the worst gap since the last heartbeat is logged so a stall is visible.
    std::chrono::steady_clock::time_point inputTickLastAt_{};
    double inputTickGapMaxMs_ = 0.0;
    int hookRecoveryAttempts_ = 0;
    // A contained full restart is the terminal automatic recovery for one user
    // session. Keep this latch across the restart's transient state changes.
    bool hookFullRestartEscalated_ = false;
    // Once terminal recovery begins, video freshness alone must not re-arm the
    // bot. Only a verified direct-pipe reconnect (or a user-started new session)
    // clears this independent fail-closed gate.
    bool inputRouteAwaitingRecovery_ = false;
    // A precise deadline reached the worker but neither authorized output route
    // accepted its release edge. Kept separate from the hook-restart gate so a
    // later active ViGEm fallback may prove recovery without weakening the
    // direct-pipe restart contract. Re-arm also requires three physically
    // neutral Square/RS samples so reset() cannot reinterpret the same hold.
    bool preciseFireDeliveryFault_ = false;
    int preciseFireRecoveryNeutralFrames_ = 0;
    // First-frame shot-intent edges wake the reader but grant no fire authority.
    // The tracker mirrors AutomationEngine's three-UP release lifetime so a
    // transient controller poll dropout cannot mint a second shot epoch mid-hold.
    ShotIntentEdgeTracker shotIntentEdgeTracker_{};
    // [DPAD-UP BYPASS HOTKEY 2026-08-08] Rising-edge latch for the physical
    // D-pad Up meter-delay bypass-on-defense toggle. Passive observer of the
    // same selected-device report the shot-intent tracker consumes; it holds
    // no shot/fire authority and never blocks pass-through. Deliberately NOT
    // reset with the session trackers: the latch mirrors the physical button
    // only, so a hold can never re-emit across a session or engine reset.
    DpadUpEdgeTracker meterDelayBypassHotkeyTracker_{};
    // [PRESSED OVERLAY 2026-08-08] Raw Square-held latch for the local "PRESSED"
    // acknowledgment badge. Passive observer of the same selected-device report
    // the shot-intent tracker consumes; no shot/fire authority, nothing sent to
    // the console. Deliberately raw (no debounce) — release smoothing is the QML
    // fade. Fed false on route loss so a pad unplugged mid-hold cannot leave the
    // badge lit. See PressedOverlayPolicy.h for the full contract.
    SquareHeldLatch pressedOverlayLatch_{};
    SquareUpAuditTracker squareUpAuditTracker_{};
    // Same-tick change detection + emission for the badge. Inline so the input
    // poll's fast path pays one branch when nothing changed.
    void syncPressedOverlay(bool held)
    {
        if (pressedOverlayLatch_.update(held)) {
            emit physicalSquarePressedChanged();
        }
    }
    // Monotonic identity generated synchronously at each physical shot-intent edge.
    // It deliberately survives stream/engine resets so delayed sidecar traffic can
    // never collide with the next shot. Zero is permanently invalid.
    quint64 physicalShotEpochCounter_ = 0;
    // ── [ORION_INPUT_DEAD_UX 2026-08-30] input-session auto-retry + dead-input overlay ─────────
    // See InputSessionRetryPolicy.h for the state machine and the full safety argument
    // (bounded attempts, backoff, cold-vs-warm classification, why it cannot double-spawn).
    // Kill switch: ORION_INPUT_SESSION_AUTORETRY=0 disables scheduling; the overlay is NOT
    // killable (visibility of dead input must never be optional).
    InputSessionRetryPlanner inputRetryPlanner_;
    // True from an accepted user Connect until a user Disconnect / shutdown: "the player asked
    // for a session". Gates both auto-retry and the loud overlay so the pre-Connect preview
    // browse stays quiet.
    bool inputSessionIntentActive_ = false;
    // Set around the retry machinery's own disconnect+connect calls so they are not mistaken
    // for fresh user intent (which would reset the bounded attempt budget mid-loop).
    bool inputRetryInFlightReconnect_ = false;
    // >0 while a retry timer is armed (holds the 1-based attempt number it will fire as).
    int inputRetryPendingAttempt_ = 0;
    // The planner ran out of budget (or was killed by env): the overlay switches from
    // "retrying" to "press Connect".
    bool inputRetryGaveUp_ = false;
    // Wall-clock ms of the last press that landed while input was undeliverable. Latches the
    // overlay on for kUndeliverablePressLatchMs even outside session intent — a press into dead
    // input proves the player believes they are connected. Written on the SAME tick as the
    // "PRESS UNDELIVERABLE" forensic line, from the SAME predicate.
    qint64 lastUndeliverablePressMs_ = 0;
    // Wall-clock ms since Running-with-pipe-down was first observed (0 = healthy). Feeds the
    // overlay's Running-edge debounce (runningPipeDownConfirmed); the log line is undebounced.
    qint64 runningPipeDownSinceMs_ = 0;
    // Cached overlay verdict + text (compared in refreshInputDeliveryState so the NOTIFY only
    // fires on real changes).
    InputDeadSeverity inputDeadSeverity_ = InputDeadSeverity::None;
    QString inputDeadHeadline_;
    QString inputDeadDetail_;
    void refreshInputDeliveryState();
    void handleInputSessionFailure(int failureClass, bool wakeObserved);
    void scheduleInputSessionRetry(const InputSessionRetryDecision& decision,
                                   InputSessionFailureClass cls);
    void fireScheduledInputSessionRetry(quint64 generation, bool coldRestart, int attempt);
    RemoteFrameProvider* frameProvider_ = nullptr;
    // Latest decoded preview frame, cached so the Train button can crop the anchor
    // template + meter region from exactly what the user sees.
    QImage lastRemoteFrame_;
    QTimer refreshTimer_;
    QTimer inputPollTimer_;
    QTimer lightbarEffectTimer_;
    // Detection-overlay hue cycle. 250 ms / CoarseTimer, and it only RUNS while
    // RGB mode is on and a preview is actually being rendered — an idle app, or
    // any user who never turns RGB on, pays literally nothing for this feature.
    //
    // 4 Hz is a deliberate ceiling, not a compromise. This is a real-time shot
    // timer sharing a GUI thread with a 60 fps preview, and a cosmetic hue cycle
    // has no business competing with either. At 4 Hz the cycle is visually
    // indistinguishable from a per-frame one for a slowly-rotating hue, while
    // costing 4 property updates a second instead of 60.
    QTimer overlayEffectTimer_;
    // Current step of that cycle, pre-formatted as "#RRGGBB" so the QML binding
    // assigns a string that is identical whenever the rendered colour has not
    // changed. Empty until the cycle has produced its first step.
    QString overlayRgbColor_;
    double overlayRgbHueDeg_ = 0.0;
    // Mirrors the QML preview render clock, so the cycle can suspend itself when
    // there is no preview on screen to tint.
    bool overlayPreviewRenderActive_ = false;
    ui_notifications::StatusNotificationThrottle telemetryStatusThrottle_;
    ui_notifications::StatusNotificationThrottle telemetryPropertyThrottle_;
    ui_notifications::StatusNotificationThrottle controllerStatusThrottle_;
    ui_notifications::LiveMeterNotificationCadence meterMetricsCadence_;
    // Re-acquires + re-embeds the Chiaki/OrionStream window for the whole life of
    // the stream (window recreation, process restart, late first show). The old
    // one-shot 6 s poll missed any window that appeared after its deadline.
    // remoteRunning_ only flips true on the async stateChanged signal, so the
    // watchdog keeps polling through a startup grace window before it may stop.
    QTimer chiakiEmbedWatchdog_;
    qint64 embedWatchdogGraceUntilMs_ = 0;
    // One-shot guard for the Vulkan→OpenGL embed fallback restart (per app run).
    bool vulkanEmbedFallbackTried_ = false;

    bool authenticated_ = false;
    bool authBusy_ = false;
    bool remoteRunning_ = false;
    bool chiakiCleanupDone_ = false;   // guard: destructor + aboutToQuit both taskkill chiaki
    ApplicationShutdownPhase applicationShutdownPhase_ = ApplicationShutdownPhase::Running;
    bool remotePlayTeardownActive_ = false;
    bool remotePlayTeardownDeferred_ = false;
    bool remotePlayTeardownStopRequested_ = false;
    bool remotePlayTeardownSynchronous_ = false;
    bool captureRefreshInFlight_ = false;
    // Monotonic fence for delayed full-reconnect callbacks. Every accepted
    // Connect/Disconnect intent and every newly scheduled recovery advances it,
    // so an old timer can never tear down a newer manual session.
    quint64 remotePlayLifecycleGeneration_ = 0;
    // Stream startup grace: a sidecar exit within this window after a connect is treated as a
    // warm-up hiccup (chiaki still establishing → black frames) and quietly auto-restarted,
    // instead of counting toward the safe-mode escalation that disarms the bot.
    qint64 streamStartupGraceUntilMs_ = 0;
    int streamStartupRestarts_ = 0;
    // A capture/watchdog fault keeps timing disabled until a newly processed,
    // non-stale frame proves the replacement feed is alive.
    bool captureAwaitingFreshFrame_ = false;
    double lastCaptureProofTsMs_ = -1.0;
    int lastCaptureProofFrameNumber_ = -1;
    // Wall-clock ms of the last connectRemotePlay(); used by the watchdog to detect a
    // connect-time decoder-export stall (the stream is up but live video never flows).
    qint64 streamConnectMs_ = 0;
    // Shoot-to-train calibration state.
    bool meterCalibrating_ = false;
    int meterCalShots_ = 0;
    int meterCalTarget_ = 1;   // one good shot is enough to auto-learn colour + green hue
    QString meterCalStatus_;
    // [Track B / B3] sidecar ColorCalibrator lifecycle mirror. Once a calibrate_meter_status
    // event has been seen the sidecar owns shot counting (sidecarCalAuthoritative_) and the
    // legacy graded-shot increment in the shotOutcomeLearned handler is skipped.
    QString meterCalState_ = QStringLiteral("factory");
    QString meterCalLearnedDate_;
    bool sidecarCalAuthoritative_ = false;
    // Controlled timing-latency calibration mirror (AutomationEngine is the source of truth).
    bool latencyCalibrationActive_ = false;
    bool latencyCalibrationReady_ = false;
    int latencyCalibrationSamples_ = 0;
    double latencyCalibrationMeasuredLeadMs_ = 0.0;
    double latencyCalibrationSdMs_ = 0.0;
    // [ORION_USER_LEAD] this install's measured end-to-end lead (median of recent landings) and
    // how many landings it is built from. Display + seed evidence only; the applied lead is the
    // persisted setting.
    double actuationLeadMeasuredMs_ = 0.0;
    int actuationLeadMeasuredSamples_ = 0;
    // [ORION_LEAD_CONFLICT 2026-08-08] Engine-fed lead diagnostics (see the Q_PROPERTY block).
    // shotLeadConflictMisses_ tracks shots aborted live_tip_deadline_missed while the Shot Lead
    // x Tip Timing pair was structurally unschedulable; the leadAuthority* trio is the last
    // VALIDATED posterior the engine diagnosed as >3*sd from the in-band lead (sticky between
    // diagnoses on purpose — the QML badge re-runs the 3*sd test against the live slider).
    int shotLeadConflictMisses_ = 0;
    double leadAuthorityMs_ = -1.0;
    double leadAuthoritySdMs_ = -1.0;
    int leadAuthoritySamples_ = 0;
    // [ORION_LEAD_CONFLICT] True while the engine's config-time conflict gate would fire for the
    // CURRENT pair — the abort-reason translation in the shotAborted handler uses this so a
    // TRANSIENT live_tip_deadline_missed on a schedulable pair keeps its raw reason.
    [[nodiscard]] bool shotLeadConflictActiveNow() const;
    // [ORION_LEAD_CONFLICT] The lead the decision path is currently consuming, as far as this
    // layer can see it, for the per-outcome telemetry line. Mirrors the in-band half of
    // AutomationEngine::actuationLeadSourceLabel(): a valid in-band value (user or seed) IS the
    // consumed lead; otherwise the engine-internal authority owns it and this layer reports the
    // last authority-published median (actuationLeadMeasuredMs_), or -1 when never published.
    struct ActiveLeadTelemetry {
        double leadMs = -1.0;
        QString source;
    };
    [[nodiscard]] ActiveLeadTelemetry activeLeadForTelemetry() const;
    QString latencyCalibrationStatus_ = QStringLiteral(
        "Waiting for a live controller and video route.");
    // One-shot controller reason consumed by the next engine status signal so cancel/disconnect
    // produces one coherent notification instead of a transient generic "Paused" state.
    QString latencyCalibrationStatusOverride_;
    QString authMessage_ = QStringLiteral("Paste the one-time code from zaeorion.com/connect, then press Unlock.");
    QString licenseState_ = QStringLiteral("Locked");
    QString authLicenseKey_;   // stashed on activation, used by the license heartbeat
    QString pendingActivationKey_;   // orion://activate deep-link key awaiting AuthGate pickup
    // CRIT-1 (docs/SECURITY_REDTEAM.md): the /api/activate session token is kept
    // client-side (it used to be discarded at the activationFinished handler)
    // and the FIRE path is gated on a live server lease via leaseGate_ when
    // ORION_LEASE_GATED_FIRE is set (default OFF until the 07-15 server work).
    QString authToken_;        // opaque session token (server stores its sha256)
    QString authTokenId_;      // token_id of the HMAC(token_id:license_key:expires) record
    qint64 authTokenExpires_ = 0;   // epoch seconds
    LeaseGate leaseGate_;
    // MOTD (contract §5). motd_ is the last STRUCTURED server notice (a transport
    // blip never clears it); motdDismissed_ is in-memory only and resets whenever
    // the text changes; motdExpiryTimer_ hides the banner the moment `until` passes.
    LicenseMotd motd_;
    bool motdDismissed_ = false;
    QTimer motdExpiryTimer_;
    // [CL2-P8-002 2026-09-23] Heartbeat recovery (LicenseHeartbeatPolicy.h).
    // [round 2] heartbeatCoordinator_ owns every decision: 15/30/60 s ladder after
    // failures (else 5 min), the debounce for event-driven heartbeats (resume,
    // network online, lease lapsed) on heartbeatMonotonic_, and the one pending
    // re-run for an event that lands while a request is in flight. The controller
    // only executes HeartbeatActions. None of this touches leaseGate_: only a
    // verified ok server heartbeat refreshes it.
    LicenseHeartbeatCoordinator heartbeatCoordinator_;
    QElapsedTimer heartbeatMonotonic_;
    LeaseNoticeKind leaseNoticeKind_ = LeaseNoticeKind::None;
    QString leaseNotice_;
    QTimer leaseNoticeTimer_;
    void requestImmediateLicenseHeartbeat(const QString& reason);
    void applyHeartbeatAction(const HeartbeatAction& action);
    void refreshLeaseNotice();
    void applyServerMotd(const LicenseMotd& motd);
    // Profile page state (LicenseProfile). Written only by applyLicenseProfile()
    // from an activation or a heartbeat that actually carried a `profile`; a
    // backend without one leaves the last known block intact rather than blanking
    // the page mid-session.
    LicenseProfile profile_;
    void applyLicenseProfile(const LicenseProfile& profile);
    QString serverState_ = QStringLiteral("Venice service");
    QString updateState_ = QStringLiteral("Checking");
    QString updateGatePhase_ = QStringLiteral("checking");
    bool updateGateResolved_ = false; // the gate only arms once, at startup
    bool devBuild_ = false;           // launched from a CMake build tree (dev guard)
    QString latestVersion_;
    bool updateAvailable_ = false;
    bool updateMandatory_ = false;
    bool updateBlocked_ = false;
    QString updateNotes_;
    // [ORION_UPDATE_NO_LOCKOUT 2026-08-08] Per-version latch so the silent
    // path logs its "already attempted, not re-applying" decision ONCE per
    // target version instead of on every update re-check / stream end.
    QString silentUpdateSuppressedLoggedVersion_;
    QString securityState_ = QStringLiteral("Checking");
    QString securityDetail_ = QStringLiteral("Local checks have not run yet.");
    bool securityLockActive_ = false;
    bool settingsRepairAvailable_ = false;
    bool settingsSaveRefusedLogged_ = false;
    bool securityReleaseManifestRequired_ = false;
    QString securityLockReason_ = QStringLiteral("-");
    QString entitlementState_ = QStringLiteral("No cached entitlement");
    QString integrityState_ = QStringLiteral("Unchecked");
    QString lastSecurityAuditEvent_ = QStringLiteral("-");
    QString currentPage_ = QStringLiteral("remotePlay");
    QString remoteState_ = QStringLiteral("Disconnected");
    QStringList captureDeviceList_;   // DirectShow video devices for the capture-card picker
    QStringList captureDeviceIds_;    // index-aligned opaque moniker IDs from the same enumeration
    QString remoteStatus_ = QStringLiteral("Disconnected");
    QString controllerStatus_ = QStringLiteral("No controller plugged in");
    QString controllerInputSource_ = QStringLiteral("None");
    QString controllerDeviceDetail_ = QStringLiteral("No controller device");
    QString controllerHealth_ = QStringLiteral("Physical Sony device not visible to Windows");
    QString controllerStableDevice_ = QStringLiteral("-");
    bool controllerIsPhysicalSony_ = false;
    double controllerLastInputAgeMs_ = -1.0;
    QString controllerLedStatus_ = QStringLiteral("LED waiting for controller");
    QString chiakiEmbedStatus_ = QStringLiteral("Waiting");
    // When true (ORION_QML_RENDER, default on) the decoder-pipe preview is rendered
    // in QML and the chiaki window is parked off-screen instead of embedded in the
    // panel — fixes the embed grow-on-connect, the overlay airspace, and the mouse
    // grab in one move. Set ORION_QML_RENDER=0 to restore the in-panel HWND embed.
    bool qmlRenderMode_ = true;
    // ORION_PREVIEW_ASYNC (default ON): async provider pull for the live preview
    // Image only — decouples per-frame texture preparation from the GUI tick.
    // Preview-only; never touches the reader/engine frame path.
    bool previewAsync_ = true;
    // End-to-end preview observability: source/SHM stats alone cannot prove that
    // QML actually fetched frames. The provider publishes a five-second cadence
    // window used by the smoothness release gate.
    qint64 qmlPreviewStatsAtMs_ = 0;
    quint64 qmlPreviewReadyAcksTotal_ = 0;
    quint64 qmlPreviewReadyAcksWindow_ = 0;
    quint64 qmlPreviewStaleAcksWindow_ = 0;
    int qmlPreviewLastAcknowledgedSerial_ = -1;
    static constexpr qint64 kQmlPreviewStatsIntervalMs_ = 5000;
    // [ORION_ACTIVITY_FEED 2026-09-14] Healthy-cadence twin of the above: the
    // qml_preview_pipeline gauge logs once a minute unless the window recorded a
    // stale presentation ack, which restores the 5 s beat.
    static constexpr qint64 kQmlPreviewStatsHealthyIntervalMs_ = 60000;
    QString captureSourceHealth_ = QStringLiteral("waiting_for_first_frame");
    bool capturePreviewActive_ = false;
    QString backendMessage_ = QStringLiteral("Remote Play helper has not been checked.");
    QString pin_;
    QString redirectUrl_;
    TelemetrySnapshot telemetry_;
    ShotContext shot_;
    QString shotState_ = QStringLiteral("Idle");
    // Last detection-presence value logged, so the presence-transition line in the
    // sidecarDetectionReady handler only fires on a change (not every frame).
    QString lastLoggedPresence_ = QStringLiteral("no_sample_ever");
    // DISPLAY-ONLY box in preview-image pixels.  A bounded native tracker steadies its centre and
    // adds fixed containment guard; AutomationEngine never reads this rectangle.
    QRect meterBox_;
    // Latest computed presentation geometry can run ahead of the texture while
    // QML is asynchronously loading it. Only acknowledgeRemoteFramePresented()
    // copies a keyed snapshot into the public meterBox_/dimension properties.
    QRect meterOverlayComputedBox_;
    QRect meterOverlayComputedRejectedBox_;
    RemoteFrameOverlaySnapshotStore remoteFrameOverlaySnapshots_;
    MeterOverlayPresentationTracker meterOverlayTracker_;
    // Latest authoritative sidecar meter bbox in CAPTURE-frame px (from sidecarDetectionReady).
    // handleRemoteFrame maps this into preview-image px for the overlay — this replaces the old
    // per-frame GUI-thread preview detector (a full OpenCV pass that saturated the GUI thread).
    QRect meterBoxCapture_;
    // FRAME-ID JOIN ring: capture-px meter bbox keyed by the DECODER frame number it was detected on.
    // The preview `frame` stream and each detection carry the same decoder frame seq
    // (orch._last_decoded_frame_number). handleRemoteFrame joins the box for the frame being painted so
    // the lock box glues to its own frame instead of smearing onto a later async-decoded preview image.
    MeterBoxRing meterBoxRing_;
    QSize meterBoxCaptureSize_;
    // Display-only continuity through reader-authorized fill occlusion/reseat.
    // It is keyed to the active physical-shot epoch and can never feed timing.
    MeterOverlayContinuityLease meterOverlayContinuityLease_;
    // Wall-clock ms of the last time EITHER detector saw the meter. The overlay box is
    // held "sticky" for a short window after the last sighting so a 1-frame detector miss
    // mid-shot doesn't flicker the box off (clean box for all shots).
    qint64 lastMeterSeenMs_ = 0;
    // Wall-clock ms of the last FRESH, REAL (raw, non-echo) detection. It feeds
    // the presentation-only meterConfirmed lease; timing and measured HUD values
    // retain their stricter freshness budgets. Distinct from lastMeterSeenMs_,
    // which also advances on stale/echo samples kept for the readout; those must
    // NOT keep the lock box alive during an idle hold.
    qint64 lastRealMeterSeenMs_ = 0;
    // Presentation-only visual evidence clock. Unlike lastRealMeterSeenMs_, it
    // is not renewed by empty post-shot echoes or unstructured idle candidates.
    qint64 lastMeterOverlayVisualSeenMs_ = 0;
    bool meterConfirmed_ = false;
    // ---- meter-blind safety net (see the meterBlindWarning property) ----
    // [RT-MED-04 / CL3-F4-007 / CL3-F8-009 2026-09-23] The whole rule lives in MeterBlindnessLatch
    // (pure, unit-tested): the streak counts completed METER presses the meter never answered
    // (engine signal meterPressUnanswered), only IN-PRESS raw detections reset it, and the latch
    // clears after MeterBlindnessLatch::kRecoveryOwnedShots VISION-owned shots. The two bools
    // below mirror it for the existing properties and the engine gate.
    MeterBlindnessLatch meterBlindLatch_;
    bool meterBlindWarning_ = false;
    // [CL2-P9-001 2026-09-23] Mirrors meterBlindLatch_.unavailable(); drives the engine's
    // detection_unavailable gate.
    bool detectionUnavailable_ = false;
    // ---- reader detector health (Meter Detection card, presentation only) ----
    // Empty until the sidecar's first detector_health line; cleared when the sidecar exits.
    QString detectorHealthLine_;
    QString detectorProvider_;
    // Human-readable visibility notices use genuine raw lock freshness, not
    // AutomationEngine's shot-scoped detectionPresence state.
    bool userMeterVisible_ = false;
    // How long after the last raw detection the lock box stays "confirmed" (visible). Short enough
    // that an idle Square-hold drops the box fast, long enough to bridge a few-frame miss mid-shot.
    static constexpr qint64 kMeterConfirmFreshMs_ = 120;
    // Phase-3: extended confirm window during active shots (isShooting=true). A detection gap
    // during a shot should NOT clear the overlay box — the meter is provably on screen. 300ms
    // matches the sidecar's sticky window at the detection-loss clear path.
    static constexpr qint64 kMeterConfirmShotMs_ = 300;
    // Measured-only HUD snapshot. These fields update exclusively from a
    // genuine, fresh, raw DetectionResult. A coast/memory/stale payload clears
    // meterMetricsCurrent_ instead of exposing a held or extrapolated value.
    bool meterMetricsCurrent_ = false;
    qint64 measuredMeterAtMs_ = 0;
    QTimer meterMetricsExpiryTimer_;
    quint64 measuredMeterSerial_ = 0;
    double measuredMeterFillPct_ = -1.0;
    double measuredMeterConfidence_ = -1.0;
    double measuredMeterVelocityPctS_ = 0.0;
    // Current-frame active-target crossing snapshot. The arm token and target
    // join prevent a green-centre/registration ETA from being shown under a
    // different active target label.
    quint64 measuredEtaSerial_ = 0;
    quint64 measuredEtaArmToken_ = 0;
    double measuredEtaTargetPct_ = -1.0;
    double measuredEtaAtObservationMs_ = -1.0;
    // Native ownership interval for the current arm token. This deliberately
    // starts at the controller-observed takeover transition, never the earlier
    // physical Square edge. Ending at releaseTrigger is local evidence only;
    // Orion does not yet receive a per-shot acknowledgement from the console.
    quint64 botOwnershipArmToken_ = 0;
    double botOwnershipStartedMs_ = -1.0;
    double botOwnershipEndedMs_ = -1.0;
    // ===== Live meter-side telemetry HUD =====
    // 30 Hz. A human reads these digits; anything faster only burns GUI-thread
    // time next to the 60 fps preview and cannot be perceived.
    static constexpr qint64 kMeterHudNotifyIntervalMs_ = 33;
    LiveMeterTelemetry liveMeter_;
    ui_notifications::StatusNotificationThrottle meterHudCadence_{
        kMeterHudNotifyIntervalMs_};
    // One pre-formatted overlay value. `key` is the value quantised to the digits
    // the row actually renders; when it is unchanged the format+allocate is
    // skipped entirely, so a steady readout performs zero work per refresh and
    // QML performs no Text relayout.
    //
    // kAbsentKey is "no proven value", and it renders as the neutral placeholder
    // rather than as an empty string: the row must keep occupying its slot so the
    // overlay's size is constant and it cannot appear to vanish. `text` therefore
    // starts at the placeholder and is never emptied — a boot with no shot yet is
    // already in the correct steady state, which is what lets refresh() keep its
    // zero-work early return.
    struct HudLine {
        static constexpr int kAbsentKey = std::numeric_limits<int>::min();
        QString text = QStringLiteral("--");
        int key = kAbsentKey;

        // True when the caller must (re)build `text` for this key. kAbsentKey is
        // an ordinary key here: demoting a row to the placeholder goes through
        // this same test, so there is exactly one path that changes a row and no
        // separate reset() that could drift from it.
        [[nodiscard]] bool stale(int nextKey) noexcept
        {
            if (nextKey == key) {
                return false;
            }
            key = nextKey;
            return true;
        }
    };
    HudLine hudFill_;
    HudLine hudTip_;
    HudLine hudFire_;
    // Wall-clock deadline until which TIP/FIRE keep the LAST OWNED SHOT's digits
    // instead of demoting to "--".
    //
    // Why this exists: TIP and FIRE only exist while the bot owns a shot, and the
    // bot owns ~180 ms of a ~2500 ms meter. The rows were therefore correct and
    // effectively invisible — the owner reported "no live tip/fire values" across
    // several days of entirely bot-driven shots, because a 180 ms flash is below
    // what an eye reading a number can catch.
    //
    // This holds the REAL decision on screen long enough to read. It never
    // synthesises or extrapolates a value: the digits shown are exactly the ones
    // the engine published for that shot, simply not erased for a moment. That is
    // the distinction the surrounding comments care about — a second predictor
    // beside the meter would be dishonest; a readable one is not.
    qint64 hudShotHoldUntilMs_ = 0;
    // hudJitter_ / hudCourtText_ / hudRouteJitterMs_ / hudRouteRttLive_ (the
    // HUD-only mirror of the RTT engine's route sample) were REMOVED with the
    // COURT/JITTER rows on 2026-08-06 — see the meterHud* Q_PROPERTY comment.
    QRect meterRejectedBox_;
    QRect meterSearchBox_;
    QString meterFillLine_ = QStringLiteral("--");
    QString meterTargetLine_ = QStringLiteral("--");
    QString meterProfile_ = QStringLiteral("-");
    QString meterRejectionReason_ = QStringLiteral("-");
    int liveFrameWidth_ = 0;
    int liveFrameHeight_ = 0;
    QVariantList poseKeypoints_;
    QVariantList poseBox_;
    QVariantList poseAnchor_;
    QVariantList poseLockCenter_;
    QVariantList poseIndicator_;
    int frameSerial_ = 0;
    QStringList logs_;
    // [ORION_ACTIVITY_FEED 2026-09-14] The customer ring. Same cap as logs_, but
    // it only ever receives the lines ui_notifications::shouldEnterActivityRing
    // classifies as human events, so 1000 entries is hours of real activity
    // instead of minutes of telemetry.
    QStringList customerLogs_;
    // [ORION_ACTIVITY_FEED 2026-09-14] Last logged NetworkBridge connection state
    // ("connected - <message>"). connectionChanged fires on every `error` event
    // the diagnostics service answers with, not only on a real transition, so the
    // line is emitted only when this value actually changes.
    QString lastNetworkBridgeStateLine_;
    // appendLog() is hit from hot loops. The GUI owns this short pending batch and the UI ring;
    // flushPendingLogs() transfers it to appLogSink_'s ordered disk worker on the 200 ms beat.
    QStringList pendingLogDiskLines_;
    QTimer logFlushTimer_;
    bool logsDirty_ = false;
    // [ORION_USER_LOG] (fix 5b) SEPARATE human-readable log sink -> logs/orion_user.log
    // (local timestamps, plain-language event lines). Additive only: the diagnostic
    // logs/orion_native.log stream above is untouched and stays byte-identical; while the
    // env flag is off the sink is fully inert (no lines, no file). Flushed on the same
    // 200ms logFlushTimer_ beat as the diagnostic queue.
    UserFacingLog userLog_;
    UserFacingReleaseTracker userReleaseTracker_;
    qint64 sessionStartMs_ = 0;
    // Court-IP lock with hysteresis: 2K alternates between server IPs, so lock the first and
    // require several consecutive sightings of a different one before retargeting (no flip-flop spam).
    bool coreActive_ = false;
    QString scriptState_ = QStringLiteral("Inactive");
    QString scriptDetail_ = QStringLiteral("Waiting for live capture heartbeat");
    QString licenseDetail_ = QStringLiteral("Activation required");
    QString timeLeft_ = QStringLiteral("-");
    QString timeLeftDetail_ = QStringLiteral("No expiry loaded");
    QString meterRuntimeState_ = QStringLiteral("Searching");
    // Session verdict tally + per-type last verdict for the Auto-Calibration card.
    int sessionVerdicts_ = 0;
    int sessionGreens_ = 0;
    int lastSessionGradedReleaseSeq_ = 0;
    ManualShotTally manualShotTally_;
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14] The last 10 graded banners. Fed only by
    // observeBannerVerdict(); read only by the bannerXxx properties above.
    ShotVerdictTally bannerTally_;
    // [ORION_BANNER_LEAD_TRIM 2026-09-15] The published half of the engine's banner trim: the
    // display bucket's value in ms and whether the loop is armed. Written ONLY from the engine's
    // bannerLeadTrimUpdated signal (and from the settings apply, for the flag), so the UI can
    // never disagree with the value the scheduler is actually spending.
    double bannerLeadTrimMs_ = 0.0;
    bool bannerLeadTrimEnabled_ = true;
    // [ORION_LEAD_AUTO_SEED 2026-09-15] The auto-seed caption's published state. Mirrors the
    // engine; never written back to it and never persisted.
    bool leadAutoSeedActive_ = false;
    double leadAutoSeedMs_ = 0.0;
    double leadAutoSeedMeasuredMs_ = 0.0;
    QString leadAutoSeedKind_;
    QHash<QString, QString> lastVerdictByType_;
    // Defense Mode runtime state (settings live in AppConfigData).
    bool defenseModeActive_ = false;
    // [2026-09-14 owner] "for no meter remove the pause timing". The Pause row is gone from
    // NoMeterCard, so nothing on screen could clear a pause — a launch-time `true` here would be
    // NO METER selected, armed, and silently never firing, with no control to find. The property
    // and its setter stay (tests, and any future hotkey), but the ONLY thing that sets it now is
    // an explicit setInputTimedPaused call.
    bool inputTimedPaused_ = false;
    bool prevDefenseTriggerDown_ = false;
    qint64 lastDefenseToggleMs_ = 0;
    // Silent auto-update: periodic background re-check; applies when idle.
    QTimer updateRecheckTimer_;
    // Watchdog + safe mode state.
    QTimer watchdogTimer_;
    QTimer licenseHeartbeatTimer_;   // periodic server re-check -> kills a running session on revoke/expire/killswitch
    bool safeModeActive_ = false;
    QString safeModeReason_;
    int watchdogRecoveryCount_ = 0;        // recoveries inside the rolling window
    qint64 watchdogWindowStartMs_ = 0;     // 5-minute rolling window anchor
    // C1: bounded safe-mode AUTO-recovery (policy in AutomationEngine.h so it is headless-
    // testable). onWatchdogTick feeds it one health observation per tick while safe mode is
    // active; a continuous run of healthy ticks auto-exits safe mode + re-arms, at most
    // kSafeModeAutoRecoverMax_ times per session — beyond that (a genuine crash loop) only
    // the user's manual exitSafeMode() clears it.
    SafeModeRecoveryTracker safeModeRecovery_;
    static constexpr qint64 kSafeModeStabilityWindowMs_ = 30'000;   // 30s of continuous health
    static constexpr int kSafeModeAutoRecoverMax_ = 2;              // per session
    // "Healthy" frame feed for the auto-recovery check: fresh frame arrivals well under the
    // 15s stall threshold the frame-stall watchdog uses (a recovering stream shows tens of ms).
    static constexpr double kSafeModeHealthyFrameAgeMs_ = 2000.0;
    // C4 (2026-07-25): the auto-recovery above can only observe health that something else
    // RESTORES — but the sidecarExited handler bailed out the moment safeModeActive_ was set, so a
    // latch caused by a sidecar crash suppressed the very restart the recovery needed and the
    // stream could never become healthy again. The bot then stayed dead until a manual click.
    // Safe mode now still permits a BOUNDED number of sidecar restarts (automation stays disarmed
    // throughout — a restart only gives the feed a chance to come back). Past the cap a genuine
    // crash loop stops restarting exactly as before, so this cannot become a respawn storm.
    int safeModeSidecarRestarts_ = 0;
    static constexpr int kSafeModeSidecarRestartMax_ = 2;   // per session
    qint64 frameStallSinceMs_ = 0;         // first tick that observed a stalled frame feed
    qint64 lastWatchdogRestartMs_ = 0;
    bool occlusionPauseLogged_ = false;    // logged the "Orion minimized -> feed paused" notice once
    // GUI-freeze watchdog: a worker thread checks this heartbeat; on a >6s
    // freeze it neutral-submits + disarms directly (the GUI can't).
    std::atomic<qint64> guiHeartbeatMs_{0};
    // [SAFE MODE] Epoch-ms deadline before which the freeze watchdog must NOT trip. The disconnect
    // teardown blocks the GUI thread for SECONDS on purpose — the sidecar is given a real graceful
    // shutdown window (kSidecarGracefulShutdownMs) so the PS5 receives a proper Takion/ctrl
    // disconnect instead of watching the transport vanish, then any surviving stream client is
    // reaped. That is a known, bounded block, not a hang; the watchdog read it as a freeze,
    // disarmed automation and latched SAFE MODE (manual recovery) on every NORMAL disconnect.
    // Heartbeat bumps around the block cannot cover it, because the block happens inside a single
    // synchronous call. Deliberately a DEADLINE, not a flag: a genuine wedge inside the teardown
    // still trips once it expires. 0 = no suppression.
    std::atomic<qint64> guiFreezeSuppressUntilMs_{0};
    std::atomic<bool> guiFreezeTripped_{false};
    // [RT-MED-10 2026-09-23] Set on WM_POWERBROADCAST PBT_APMSUSPEND, cleared on resume: the
    // GUI-freeze watchdog stands down while the system is going to / coming back from sleep.
    std::atomic<bool> systemSuspended_{false};
    std::atomic<bool> watchdogThreadStop_{false};
    std::thread guiFreezeThread_;
    QString holdSource_ = QStringLiteral("Square");
    QString lastResult_ = QStringLiteral("-");
    QString outputState_ = QStringLiteral("No Output");
    QString sessionTime_ = QStringLiteral("00:00:00");
    QString blockReason_ = QStringLiteral("-");
    QString statusAge_ = QStringLiteral("-");
    QString netSyncReason_ = QStringLiteral("Waiting");
    QString visionPipeline_ = QStringLiteral("Waiting for status.json...");
    QString settingsSnapshot_;
    bool physicalPadConnected_ = false;
    bool physicalPadLive_ = false;
    bool rawInputRegistered_ = false;
    bool rawInputPresent_ = false;
    // Owns the dedicated raw-input thread (message-only window + its own pump). The
    // rawInput* members below are GUI-thread mirrors refreshed from its snapshot each
    // pollPhysicalController tick; the worker thread never touches them directly.
    OrionRawInputWorker* rawInputWorker_ = nullptr;
    ControllerState rawInputState_;
    qint64 lastRawInputMs_ = 0;
    // Sub-tick release scheduler: the precise fire thread plus the submit mutex that
    // serializes its release write against the GUI tick's submit. The GUI tick re-checks
    // the thread's fired flag UNDER this mutex before submitting, so a held state can
    // never be re-pressed on top of a release the thread already wrote (= pump fake).
    OrionPreciseFireThread* fireThread_ = nullptr;
    QMutex submitMutex_;
#ifdef Q_OS_WIN
    // Process-level Windows timer-throttling opt-out. Startup deliberately
    // defers this while on battery and no stream is active; the Running
    // transition consumes the deferred request exactly once.
    bool timerResolutionThrottleOptOutApplied_ = false;
#endif
    // The GUI thread publishes arms; the GUI-freeze watchdog may revoke one.
    // The fire worker itself is fenced by submitMutex_ + its own mutex.
    std::atomic<quint64> lastArmedFireToken_{0};
    // DIAGNOSTIC (Phase 0A, hook input-read kill): WM_INPUT delivery/parse counters. wm=raw HID
    // messages reaching the handler; classified=device recognized by classifyRawInputController;
    // decoded=decodeSonyReport parsed OK. If a streaming session shows wm climbing but
    // classified/decoded flat, the patched chiaki changed the pad's report format/path so Orion
    // stopped parsing it (-> rawInputState_ stays neutral). Removed after Phase 0B.
    qint64 lastRawInputPresenceCheckMs_ = 0;
    qint64 lastLightbarApplyMs_ = 0;
    // Solid lightbar writes are event-driven. These fields distinguish a real
    // settings/device refresh from the old unconditional 1.5-second rewrite.
    bool lightbarRefreshPending_ = false;
    QColor lastLightbarSentColor_;
    QString lastLightbarSentDevicePath_;
    QString lastLightbarSentDeviceKind_;
    qint64 lastPhysicalSeenMs_ = 0;
    qint64 physicalMissingSinceMs_ = 0;
    // [ORION_METER_DELAY 2026-08-07] Grace before we tear virtual pad down.
    // 2.5s if armed / holding a sequence, 8s if idle — an idle Bluetooth pad
    // that goes to sleep should NOT drop the virtual target and reset session.
    // [ORION_PAD_TEARDOWN_GRACE 2026-08-07] Reduced idle grace 60000 -> 8000 ms;
    // a full minute of silent debugging while the pad "worked yesterday" is the
    // shipping value the round-2 review flagged. 8 s still covers a normal BT
    // reconnect (2-5 s typical); anything longer is a debug session, not a nap.
    qint64 physicalMissingTeardownGraceMs_ = 2500;
    // [ORION_PAD_TEARDOWN_GRACE 2026-08-07] Latch so the "still waiting" warning
    // is emitted at most once per grace period, not every 50 ms poll tick.
    bool physicalMissingGraceWarned_ = false;
    // [ORION_PAD_SILENT_HOLD 2026-09-11] One HID-collection nudge per silence episode.
    bool physicalMissingNudged_ = false;
    QString rawInputLabel_;
    QString rawInputDevicePath_;
    QString activePhysicalDevicePath_;
    QString activePhysicalDeviceKind_;
    QHash<quintptr, QString> rawInputDeviceKinds_;
    // [ORION_PAD_LIVE_INPUT_GATE 2026-08-08] Sony-pad USB registry history:
    // whether any Sony pad instance was EVER enumerated on this machine, and
    // the Device Parameters key paths that still carry
    // EnhancedPowerManagementEnabled=1 (targets for the consented fix).
    bool sonyUsbHistoryKnown_ = false;
    bool controllerUsbPowerFixAvailable_ = false;
    QStringList sonyUsbEpmEnabledParamKeys_;
    ControllerDeviceSelector controllerSelector_;
    ControllerLifecycleState controllerLifecycleState_ = ControllerLifecycleState::NoPhysical;
    QString lastControllerRoute_;
    bool virtualConnectInProgress_ = false;
    int inferredVirtualXinputSlot_ = -1;
    // XInput device removal is asynchronous. Keep the slot of a just-removed
    // ViGEm target quarantined while Windows drains it so the selector cannot
    // mistake Orion's own stale XUSB device for a newly-arrived physical pad.
    int retiredVirtualXinputSlot_ = -1;
    qint64 retiredVirtualXinputSlotUntilMs_ = 0;
    // Desktop UI isolation: controller activity opens a short suppression window
    // for mapped navigation-key/mouse messages while Remote Play is live. The
    // previous state makes this edge-triggered: a held/noisy report cannot lock
    // out the user's real mouse indefinitely.
    qint64 controllerUiGuardUntilMs_ = 0;
    // [ORION_CONTROLLER_UI_ISOLATION 2026-09-19] A stick mapped to the desktop
    // POINTER never produces a press edge, so it never opened the guard above and
    // the cursor walked over Venice lighting up hover states mid-session. This is
    // the level-triggered twin: it stays open while any stick is deflected past
    // kControllerUiStickDeflection and for kControllerUiPointerGuardMs after.
    qint64 controllerUiPointerGuardUntilMs_ = 0;
    // Explicit escape hatch (ORION_CONTROLLER_UI_PASSTHROUGH=1): deliberately
    // controller-driven UI, and the owner's way back to pre-2026-09-19 behaviour.
    bool controllerUiPassthrough_ = false;
    // ORION_CONTROLLER_UI_INJECTED_ISOLATION=0 disables the injected-source leg
    // only, leaving the timing guards intact.
    bool controllerUiInjectedIsolation_ = true;
    bool controllerUiSuppressionLogged_ = false;
    ControllerState previousControllerUiState_{};
    bool directPipeOwnsInput_ = false;
    // Current-session neutral local-delivery proof used only to bind restored timing-cache
    // provenance. Pending and confirmed are intentionally distinct: local acceptance alone is
    // never sidecar echo authority, and reset preserves the process-monotonic generation fence.
    LatencyRouteAttestationHandshake latencyCacheRouteAttestation_;
    // Per-XInput-slot "don't re-probe before this wall-clock ms". XInputGetState on a DISCONNECTED
    // slot stalls hundreds of us-ms; polling all 4 empty slots at 250Hz on the GUI thread blows the
    // frame budget. An empty slot is skipped for ~1s after it reads empty; a connected slot is polled
    // every tick (cheap + needed for live state). [4] == XUSER_MAX_COUNT.
    qint64 xinputSlotNextProbeMs_[4] = {0, 0, 0, 0};
    // Capabilities are device-lifetime metadata, not controller state. Querying
    // them at 250 Hz on the GUI thread added avoidable OS calls beside preview
    // presentation. Any disconnect or PnP change invalidates the cache.
    bool xinputSlotCapabilityKnown_[4] = {false, false, false, false};
    bool xinputSlotNoNavigation_[4] = {false, false, false, false};
    // Set by releaseIssued and consumed by the post-submit telemetry. The delivery stage/source
    // are carried from the precise worker's exact ACK; never infer them from a later pipe write.
    int pendingSubmitSeq_ = -1;
    quint64 pendingSubmitPhysicalShotEpoch_ = 0;
    quint64 pendingSubmitShotAttempt_ = 0;
    quint64 pendingSubmitScheduleToken_ = 0;
    quint64 pendingSubmitRouteGeneration_ = 0;
    LatencyControllerRoute pendingSubmitRoute_ = LatencyControllerRoute::None;
    PreciseFireDeliveryStage confirmedPreciseFireStage_ = PreciseFireDeliveryStage::None;
    quint64 confirmedPreciseFireToken_ = 0;
    uint32_t confirmedPreciseFireTransportSeq_ = 0;
    PreciseFireDeliverySnapshot confirmedPreciseFireSnapshot_{};
    PreciseFireDeliveryStage pendingSubmitDeliveryStage_ = PreciseFireDeliveryStage::None;
    quint64 pendingSubmitFireToken_ = 0;
    uint32_t pendingSubmitTransportSeq_ = 0;
    PreciseFireDeliverySnapshot pendingSubmitSnapshot_{};
    // [ORION_TIP_FRAME_NATIVE 2026-09-17] Set beside pendingSubmitSnapshot_ at releaseIssued and
    // cleared with it on every reset path, so the frame-grid fields on `Release submit:` can
    // never describe a different shot than the rest of that line.
    ReleaseFrameGridSnapshot pendingSubmitFrameGrid_{};
    ReleaseMarkerDeliveryGate releaseMarkerDeliveryGate_;
    // Per-release ownership trace: accumulates physical-vs-output Square across the
    // entire Releasing+Cooldown window so a "Release ownership:" summary can prove
    // whether the bot's virtual release controlled the held shot. ownSeq_ < 0 = idle.
    int ownSeq_ = -1;
    qint64 ownStartMs_ = 0;        // nowMs at the first in-window tick
    qint64 ownLastTms_ = 0;        // t_ms of the last in-window tick (= window duration)
    int ownTicks_ = 0;             // in-window poll ticks observed
    bool ownPhysHeldAll_ = true;   // physical Square held on EVERY in-window tick
    bool ownOutClearedAll_ = true; // output Square cleared on EVERY in-window tick
    bool ownMaxOutSq_ = false;     // output Square was ever set during the window (bug signal)
    qint64 ownPhysReleaseMs_ = -1; // t_ms the user first let go of physical Square (-1 = never)
    bool ownPrevPhysSq_ = false;
    bool ownPrevOutSq_ = false;
    HoldState ownPrevState_ = HoldState::Idle;
    bool ownLogFirst_ = false;     // force a "Release tick:" on the first window tick
    QString ownBackend_;           // authoritative route captured at window start (PIPE/XUSB/DS4)
    QString ownSrc_;               // input source token captured at window start
    QString ownKind_;              // device kind token captured at window start
    // [ORION_OUTPUT_DIVERGENCE 2026-09-14 owner] "buttons sometimes are weird, it seemed like it
    // was holding L2 for me". One slot per watched field, in the order observeOutputDivergence
    // lists them (l2, r2, cross, circle, triangle, l1, r1, l3, r3 — Square excluded, because the
    // release logic legitimately owns it). -1 = agreeing with the pad right now.
    static constexpr qint64 kOutDivergeMinMs_ = 40;
    std::array<qint64, 9> outDivergeSinceMs_{{-1, -1, -1, -1, -1, -1, -1, -1, -1}};
    std::array<qint64, 9> outDivergeLoggedMs_{{-1, -1, -1, -1, -1, -1, -1, -1, -1}};
    std::array<bool, 9> outDivergeReported_{};
    int outDivergeEvents_ = 0;     // reported divergences this session
    // Previous engine shot-state for the "Shot state:" transition logger.
    HoldState prevShotState_ = HoldState::Idle;
    qint64 lastVirtualConnectAttemptMs_ = 0;
    qint64 lastControllerDeviceChangeMs_ = 0;
    quintptr lastControllerDeviceChangeHandle_ = 0;
    quintptr lastControllerDeviceChangeKind_ = 0;
    // WinMM/dinput joystick is a LAST-RESORT fallback that corrupts the shared
    // process heap when polled during device add/remove churn (joyGetPosEx ->
    // dinput!DIHid_BuildHidList -> RtlReAllocateHeap -> c0000374). Only poll it when
    // no RawInput / physical XInput pad is live, never within the device-change
    // cooldown, and at most ~1/sec; cache the last sample so the selector stays
    // stable between throttled polls.
    qint64 lastWinMmPollMs_ = 0;
    qint64 lastWinMmSeenMs_ = 0;
    // Latched true once a modern controller (RawInput/physical XInput) is seen at all this session.
    // While true the WinMM/dinput poll is permanently skipped -- an unplug briefly drops rawInputLive
    // and that window let WinMM enumerate mid-churn -> c0000374 heap corruption (the unplug/replug
    // crash). WinMM stays only a cold-start fallback when no modern pad ever appears.
    bool modernControllerEverSeen_ = false;
    ControllerState lastWinMmState_;
    quint32 lastWinMmJoyId_ = 0;
    QString lastWinMmLabel_;
    bool lastWinMmVirtual_ = false;
    // True when the discovered WinMM device carries Sony's HID VID (054C): a DualSense
    // being served by the LEGACY route, i.e. RawInput should have owned it. Drives the
    // stale-HidHide-cloak warning in the route-decision log.
    bool lastWinMmSonyVid_ = false;
    // Axis ranges of the discovered WinMM device, for the per-tick known-id fast path.
    WinMmAxisRanges lastWinMmRanges_;
    // Diagnostic for the WinMM fallback: last raw JOYINFOEX::dwButtons plus a log budget.
    // The raw mask is logged on change so a live press-test can verify the Sony button
    // order assumed by applyWinMmButtonsToSample (see WinMmButtonMapping.h).
    quint32 lastWinMmRawButtons_ = 0;
    int winMmRawButtonLogBudget_ = 40;
    // Controller isolation test (ORION_FORCE_VIRTUAL_NEUTRAL=1): submit a neutral pad
    // every tick while still reading the physical pad, so any in-game reaction to a
    // held physical Square proves Chiaki reads the DualSense directly (routing leak)
    // rather than Orion's own virtual mirror.
    bool forceVirtualNeutral_ = false;
    bool forceVirtualNeutralLogged_ = false;
    // [ORION_PRESS_DELIVERY_AUDIT 2026-08-30] Edge-dedup key for the post-submit
    // "SQUARE SUPPRESSED" invariant audit (physical Square held while the state
    // submitted to the console carries Square-up). Empty = not suppressed. One
    // log line per attributed reason-window, never per tick.
    QString lastSquareSuppressionLogged_;

    int playerCount_ = 0;
    int legitPlayerCount_ = 0;
    int cheaterCount_ = 0;
    NetworkBridge networkBridge_;
    // [VENICENET WAVE 2B] The meter-delay ACTUATION path. Loads VeniceNet.dll as
    // an IPC client to the packet-bridge service; MeterDelayController's outputs
    // (delay/intercept/possession/policy) feed this instead of NetworkBridge, and
    // the honesty surface + applied-delay echo read its snapshot. NetworkBridge
    // stays for passive diagnostics only. Declared BEFORE meterDelay_ so it
    // OUTLIVES the controller's destructor — the controller's last-resort
    // 0-command / intercept-stop emit must still reach a live DLL client (which
    // additionally zeroes the delay in venicenet_shutdown() as belt-and-braces).
    orion::VeniceNetClient veniceNet_;
    // Shot-aware inbound delay actuator. Declared AFTER networkBridge_/veniceNet_
    // so it is destroyed FIRST: its destructor's last-resort 0-command still
    // reaches a live actuator (the explicit shutdown() in ~OrionAppController is
    // the preferred path; this ordering is the fail-safe).
    // [ORION_METER_DELAY 2026-08-07] Member declared now that the feature ships.
    orion::MeterDelayController meterDelay_;
    qint64 lastPacketBridgeStartAttemptMs_ = 0;
    // Meter-delay intercept churn counter (see the interceptStartRequested log in applyConfig).
    qint64 lastMeterInterceptLogMs_ = 0;
    int meterInterceptCycleCount_ = 0;
    bool packetBridgeStartPending_ = false;
    // [ORION_PACKET_BRIDGE_INSTALL_GUARD] see the note in ensurePacketBridgeRunning().
    // Cached once per launch; prevents 15s-cadence cmd-window flashes on customer
    // installs where neither NexusVisionSvc nor nexus_svc.py exists.
    // mutable: also filled lazily by the const meterDelayBackendAvailable() QML getter
    // via probePacketBridgeAvailability().
    mutable bool packetBridgeAvailabilityChecked_ = false;
    mutable bool packetBridgeAvailable_ = false;
    // One-shot guard for the "packet bridge unavailable" log line (the probe itself is
    // const and cannot log).
    bool packetBridgeUnavailableLogged_ = false;
    // [ORION_METER_DELAY_OBSERVABILITY] Last state line written by the
    // MeterDelayController::stateTextChanged log consumer. Transition-deduped so a
    // 50 ms ramp tick can never spam the log (the applied value changes every tick;
    // the state/reason strings do not).
    QString lastMeterDelayStateLogged_;
    // [ORION_DEFENSE_FLAG 2026-08-08] Mirrors the last shot-cycle value PUSHED
    // to the actuator so the reset paths (engine reset, route loss) stay
    // coherent with the shotStateChanged feed. (Formerly also the dedupe latch
    // for the per-shot engagement-decision log line, which moved to the D-pad
    // hotkey handler when the hint was decoupled from engagement.)
    bool meterDelayShotCycleLastPushed_ = false;
    // [ORION_METER_DELAY_ROUTE_AUTHORITY 2026-08-08] True while the engine is
    // disarmed SOLELY by the meter-delay ramp gate (route authority intact, no
    // attestation revoked). Dedupe for the pair of syncEngineArmed() log lines;
    // cleared on the re-arm edge.
    bool meterDelayRampDisarmLogged_ = false;
    // [ORION_METER_DELAY_ECHO] Last applied-delay echo from the bridge service
    // itself (NetworkBridge::meterDelayServiceEcho, display-only). appliedMs < 0 =
    // no echo on the current connection; both reset when the bridge disconnects so
    // a stale echo can never outlive the service that produced it.
    bool meterDelayEchoActive_ = false;
    double meterDelayEchoAppliedMs_ = -1.0;
    QRect chiakiEmbedRect_;
    bool chiakiEmbedVisible_ = false;
    quintptr chiakiWindowHandle_ = 0;
    quintptr chiakiOriginalParent_ = 0;
    qintptr chiakiOriginalStyle_ = 0;
    qintptr chiakiOriginalExStyle_ = 0;
    // Last physical geometry actually pushed to the embedded child via SetWindowPos.
    // The embed is polled ~10x/sec (QML 250 ms + native watchdog); re-issuing
    // SetWindowPos on the Vulkan child every idle tick thrashes the swapchain and
    // shows up as flicker/black. We skip the call when the target is unchanged.
    QRect lastEmbedAppliedRect_;
    qreal lastEmbedAppliedDpr_ = 0.0;
    int embedRegrabCount_ = 0;
};

// ── Presentation/persistence seams, unit-tested through the controller TU ──────────────────
// (OrionMeterDelaySettingsTests compiles OrionAppController.cpp directly; the suite never
// constructs the controller, so anything that must be testable lives in these free functions.)

// [ORION_LEAD_CONFLICT 2026-08-08] Largest Shot Lead the tip-phase path can schedule under a
// given EFFECTIVE tip-timing constant. UI mirror of AutomationEngine::maxSchedulableTipLeadMs():
// the phase estimate is born with tip_eta ~= constant - decision latency, so a lead within the
// margin of the constant leaves no tick with a future command deadline. The 30ms margin mirrors
// kTipLeadScheduleMarginMs (AutomationEngine.cpp) — keep them in lockstep; the engine constant
// is deliberately private/diagnostic-only, so this is a documented duplicate, not a reference.
// Advisory only: annotation + banner; nothing clamps the user's control to it.
[[nodiscard]] double shotLeadMaxUsableForTipTimingMs(double tipTimingEffectiveMs) noexcept;

// [ORION_LEAD_CONFLICT 2026-08-08] Presentation rewrite for engine diagnostic lines bound for
// the Activity log. Exactly one case today: the frozen self-grader's "Outcome identity:" line
// with grader_truth=0 — the known dead-top LATE-66 artifact, and the exact line the owner read
// as a real grade before dragging Tip Timing / Shot Lead into the unschedulable regime. The
// verdict token is demoted to `verdict=ungraded-artifact(<WORD>)` so the qualifier is read
// BEFORE the trap word; the constant-artifact forensic signal survives (still greppable, still
// countable) and the machine-parseable grader_truth field is untouched. grader_truth=1 lines
// and every other diagnostic pass through byte-identical.
[[nodiscard]] QString presentEngineDiagnosticLine(const QString& line);

// [ORION_AIM_FREEZE 2026-08-08] Persist the phase instrument's own full-window median into the
// measured_phase_physical_ms learning slot (canonical base-30 physical frame — AppConfig's load
// path rejects values outside [200,500], so passing any other frame silently kills the
// restore-time divergence warning). Body of the phaseMeasuredMedianUpdated connect.
void persistMeasuredPhaseMedian(AppConfig& config, double measuredPhysicalMs);

} // namespace orion
