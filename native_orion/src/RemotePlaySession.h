#pragma once

#include "AppConfig.h"
#include "AsyncProcessRetirer.h"
#include "FrameDecoder.h"
#include "OrionExports.h"
#include "OrionTypes.h"
#include "PoseArmProtocol.h"
#include "PreviewPresentationBuffer.h"
#include "PreviewFrameChunks.h"
#include "SharedMemoryFramePump.h"
#include "VideoInputDeviceEnumeration.h"

#include <QtCore/QObject>
#include <QtCore/QChronoTimer>
#include <QtCore/QElapsedTimer>

#include "SidecarWatchdog.h"

#include <algorithm>
#include <chrono>
#include <QtCore/QJsonObject>
#include <QtCore/QProcess>
#include <QtCore/QStringList>
#include <QtCore/QTimer>
#include <QtCore/QVariantList>
#include <QtGui/QImage>

#include <vector>

namespace orion {

class ORION_REMOTEPLAY_API RemotePlaySession final : public QObject {
    Q_OBJECT
public:
    explicit RemotePlaySession(QObject* parent = nullptr);
    ~RemotePlaySession() override;

    void setRootDir(QString rootDir);
    void applyConfig(const AppConfigData& config);
    enum class BandwidthMode {
        Quality = 0,        // 1080p / 60 / H265 / default bitrate (chiaki defaults)
        Performance = 1,    // 1080p / 60 / H264 / 15 Mbps (lower decode overhead, best for mid-tier hardware)
        Balanced = 2,       // 720p  / 60 / H264 / 6 Mbps  (good for normal Wi-Fi)
        LowBandwidth = 3,   // 540p  / 30 / H264 / 3 Mbps  (poor Wi-Fi, large audio buffer)
        UltraLow = 4,       // 360p  / 30 / H264 / 1.5 Mbps (last-resort bare-minimum)
        Experimental120 = 5,
        Experimental240 = 6
    };
    Q_ENUM(BandwidthMode)

    void start();
    void stop(const QString& initiator = QStringLiteral("requested_stop"));
    void waitForStopped(); // application exit / explicitly synchronous internal recovery only
    [[nodiscard]] bool stopping() const noexcept { return !retiringSidecar_.isNull(); }
    // [ORION_CONNECT_LATENCY 2026-09-19] Start the connect stage clock at the USER's
    // click, not at start(). Everything before start() -- settings save, virtual-pad
    // creation, bandwidth preset, window containment -- was completely dark in the
    // logs (the 09-19 forensics could measure stages a1..a4 only by differencing
    // unrelated lines, and "click -> handler entry" not at all). One call from
    // OrionAppController::connectRemotePlay() makes every later stamp relative to the
    // press. Safe to call more than once; a call with no following start() just
    // leaves a stale timer that the next start() re-reads or ignores.
    void beginConnectStopwatch();
    // Live capture-card preview BEFORE Connect: open the HDMI card + stream preview frames to the
    // QML panel WITHOUT launching Chiaki, the input hook, or the virtual pad. Only meaningful for
    // the capture-card source; a no-op otherwise or when a session is already up. start() then hands
    // the device over to the full pipeline (no double-open of the single-open Elgato).
    void startCapturePreview();
    void checkBackend();
    void openChiakiClient();
    void discoverPs5();
    void applyBandwidthMode(BandwidthMode mode);
    void setAudioEnabled(bool enabled);
    void setAudioMode(const QString& mode);
    void requestOauthUrl();
    void addProfileFromRedirect(const QString& redirectUrl);
    void refreshProfiles();
    void registerProfile(const QString& user, const QString& pin);
    void testSession(const QString& user);
    void triggerGotoShot();
    // No-meter (skele): tell the sidecar to arm the pose zero-cross search on shot-begin. The native
    // owns the controller in the live virtual_controller=False path, so the arming originates here.
    void armPose(quint64 armToken);
    // Wake the meter reader on the first physical Square/vertical-stick edge,
    // before the automation hold-to-own debounce completes.  This carries no
    // release authority; beginShot's tokenized pose_arm remains authoritative.
    // [ORION_SHOT_GATE_TYPE 2026-09-15] `shotType` is the engine classifier's own label for
    // this press ("Standstill" / "Left Fade" / ... , empty when the edge carried none) and
    // narrows the sidecar's meter-onset expectation window from the union to one type;
    // `rhythm` says whether this configuration releases with the Rhythm flick offset. Both
    // are additive: an empty type keeps the sidecar on the union window, exactly as before.
    // A re-send with the SAME epoch and source="type_upgrade" refreshes the type mid-press
    // (the blind 200 ms grace can re-type a Standstill into a fade) WITHOUT moving the press.
    void armMeterGate(const QString& source, quint64 physicalShotEpoch,
                      const QString& shotType = QString(), bool rhythm = false,
                      double pressWallMsEpoch = 0.0);
    // [ORION_SHOT_GATE_RELEASE 2026-09-15] Close the press window the arm opened. `releaseMs`
    // is EPOCH ms (the sidecar's own fill-sample clock), matching sendReleaseMarker's wall_ms
    // contract. Sent on EVERY release path (vision, blind NO METER, METER BACKSTOP).
    void sendShotGateRelease(quint64 physicalShotEpoch, double releaseWallMsEpoch);
    // Close the press window for a press that ended WITHOUT a bot release: the player let go
    // (tap / pump fake) or the engine aborted. `reason` is one snake_case token.
    void sendShotGateDisarm(quint64 physicalShotEpoch, const QString& reason);
    // Recover only the Chiaki input process/pipe while preserving a healthy
    // capture-card + detector sidecar.
    bool recoverInputLink();
    // RC-3: push a release COMMAND marker (seq + wall-clock epoch ms) to the sidecar's frozen-meter
    // latency oracle. The native engine owns the controller in the live path, so it is the only place
    // the release timestamp exists. wallMs MUST be epoch ms (QDateTime::currentMSecsSinceEpoch),
    // matching the sidecar's fill-sample clock (time.time()*1000) so the oracle subtraction is exact.
    void sendReleaseMarker(int seq, double wallMs, bool calibration = false,
                           double validationTargetPct = -1.0,
                           double validationTolerancePct = -1.0,
                           quint64 physicalShotEpoch = 0,
                           quint64 shotAttempt = 0);
    // [ORION_PROBE] push a warmup pump-fake probe PRESS marker (seq + epoch ms + the
    // configured spawn offset) to the sidecar's latency estimator. Same clock contract as
    // sendReleaseMarker.
    void sendProbeMarker(int seq, double wallMs, double spawnOffsetMs);
    // Distinct neutral local-delivery proof for route-scoped latency cache eligibility. It carries
    // no release identity/timestamp and cannot enter the frozen-meter oracle label path.
    void attestLatencyControllerRoute(const QString& deliveryRoute,
                                      quint64 attestationGeneration);
    // Shoot-to-train: tell the detector sidecar to start/finish/cancel a calibration window
    // during which it auto-learns the meter colour + green hue from the user's live shots.
    void calibrateMeter(const QString& phase, int count);
    // Watchdog recovery: relaunch the detection sidecar after a crash/stall
    // (stop is a no-op when it is already gone).
    void restartSidecar();
    void setCourtIp(const QString& ip);
    void clearCourtTarget();
    // The visible QML preview can pace its display-only queue from Qt Quick's
    // render clock. This is exclusive with the fallback QChronoTimer; detector
    // and automation frames never enter either presentation path.
    void setPreviewRenderClockActive(bool active);
    void presentNextPreviewFrameOnRenderTick();
    // Returns the current sidecar QProcess PID (0 if none / not running).
    // Used by OrionAppController::killChiakiProcesses() to kill orphaned
    // sidecar processes on exit so they don't hold the capture card.
    [[nodiscard]] qint64 sidecarPid() const noexcept {
        return (sidecarProcess_ && sidecarProcess_->state() != QProcess::NotRunning)
                   ? static_cast<qint64>(sidecarProcess_->processId()) : 0;
    }
    void observePacket(const QString& srcIp, const QString& dstIp, int srcPort, int dstPort, int size, double ts);

    [[nodiscard]] RemotePlayState state() const noexcept { return state_; }
    // Input-only recovery is an authority gate inside an otherwise-live stream
    // generation. It must not masquerade as a cold Connecting transition.
    [[nodiscard]] bool inputRecoveryPending() const noexcept { return inputRecoveryPending_; }
    [[nodiscard]] QString statusText() const noexcept { return statusText_; }
    [[nodiscard]] TelemetrySnapshot telemetry() const noexcept { return telemetry_; }
    [[nodiscard]] int sidecarFps() const noexcept { return sidecarFps_; }
    [[nodiscard]] int requestedFps() const noexcept { return requestedFps_; }
    [[nodiscard]] int captureLoopFps() const noexcept { return captureLoopFps_; }
    [[nodiscard]] int uniqueFrameFps() const noexcept { return uniqueFrameFps_; }
    [[nodiscard]] double duplicateFramePct() const noexcept { return duplicateFramePct_; }
    // Detector-authority age. It can climb while capture transport is still alive when a
    // dark/malformed/loading-screen frame is deliberately rejected from the bot's input.
    // [CL-006 Codex final gate 2026-09-22] Each age below is the value carried by the LAST
    // telemetry record. The sidecar emits telemetry at >= 60 Hz, so a still-running sidecar that
    // has gone silent used to leave these frozen at their last (fresh-looking) values. Once
    // telemetry has been silent for more than kTelemetrySilenceStaleMs, every age reports at least
    // the silence itself: detection fails closed on frame/pixel age, and the stream watchdog's
    // transport-age restart (kStreamTransportRestartAgeMs) fires on a silent sidecar exactly as it
    // does on a stalled capture. MISSING telemetry before the first record ages nothing.
    [[nodiscard]] qint64 telemetrySilentMs() const noexcept {
        const auto now = std::chrono::steady_clock::now();
        const auto ms = [&](std::chrono::steady_clock::time_point t) {
            return static_cast<qint64>(
                std::chrono::duration_cast<std::chrono::milliseconds>(now - t).count());
        };
        return sidecarTelemetrySilenceMs(haveTelemetry_, haveTelemetry_ ? ms(lastTelemetryAt_) : 0,
                                         telemetryArmed_ ? ms(telemetryArmedAt_) : -1);
    }
    [[nodiscard]] double frameAgeMs() const noexcept { return silenceAged(frameAgeMs_); }
    // Time since the last UNIQUE (non-duplicate), detector-eligible frame. This is an automation
    // freshness signal, not proof that the capture transport/process died.
    [[nodiscard]] double pixelAgeMs() const noexcept { return silenceAged(pixelAgeMs_); }
    // Time since any raw frame was successfully delivered by the active capture backend. This
    // remains fresh through static or dark loading screens and is the process-liveness clock.
    [[nodiscard]] double transportAgeMs() const noexcept { return silenceAged(transportAgeMs_); }
    // Diagnostic-only backend work between the immutable capture/source stamp and
    // publication into Python's latest-wins FrameRingBuffer. Never timing authority.
    [[nodiscard]] double capturePublicationAgeMs() const noexcept {
        return capturePublicationAgeMs_;
    }
    [[nodiscard]] bool backendFrozen() const noexcept { return backendFrozen_; }
    [[nodiscard]] int captureWidth() const noexcept { return captureWidth_; }
    [[nodiscard]] int captureHeight() const noexcept { return captureHeight_; }
    [[nodiscard]] QString captureResolution() const {
        // ASCII-only on purpose: this header has no UTF-8 BOM and the build sets no
        // /utf-8, so a non-ASCII literal (x / dot / dash) would mojibake under a
        // non-UTF-8 active code page. e.g. "1920x1080 - decoder".
        if (captureWidth_ <= 0 || captureHeight_ <= 0) {
            return QStringLiteral("-");  // no frame yet
        }
        QString res = QStringLiteral("%1x%2").arg(captureWidth_).arg(captureHeight_);
        if (!captureTier_.isEmpty()) {
            res += QStringLiteral(" - ") + captureTier_;
        }
        return res;
    }
    [[nodiscard]] QString fpsProbeState() const noexcept { return fpsProbeState_; }
    // How many DECODER frames the preview image currently on screen is behind the newest frame the
    // detector reported on. [E4] Both terms are now the sidecar's `frame_number` (orchestrator
    // `_last_decoded_frame_number`) - the preview stream stamps every JPEG with it and the telemetry
    // payload carries the detected frame's value in the same counter. It used to subtract
    // `frame_count` (the monotonic UNIQUE-frame dedup key, incremented only when the pixels change)
    // minus `frame_number` (incremented on every decode), i.e. two unrelated counters with unrelated
    // origins and rates, so the number shown on two pages of the UI was meaningless - it read as a
    // large positive "lag" purely because the two counters had drifted apart.
    [[nodiscard]] int previewLagFrames() const noexcept { return previewLag_; }
    // Active capture tier ("decoder" once the chiaki frame-export pipe is flowing; "window"/
    // GDI fallback, "wgc", "capture_card", or "" before the first frame). The watchdog uses
    // this to tell a real decoder feed from the GDI black-frame fallback during a stall.
    [[nodiscard]] QString captureTier() const noexcept { return captureTier_; }
    [[nodiscard]] double shotFillPct() const noexcept { return shotFillPct_; }
    [[nodiscard]] double shotConfidence() const noexcept { return shotConfidence_; }
    [[nodiscard]] QString shotState() const noexcept { return shotState_; }
    [[nodiscard]] QString algorithmText() const noexcept { return algorithmText_; }
    [[nodiscard]] bool audioToggleBusy() const noexcept { return audioToggleBusy_; }
    // FIX 2 ("No Meter" ships broken): is the pose/skele path actually usable on THIS install?
    // The sidecar probes pose_timing/ultralytics/torch + the three models/*.pt weights at startup
    // and reports the verdict via {"event":"capabilities"}. Defaults to TRUE ("assume available")
    // so a sidecar that never reports it (older build) behaves exactly as before.
    [[nodiscard]] bool noMeterAvailable() const noexcept { return noMeterAvailable_; }
    [[nodiscard]] QString noMeterUnavailableReason() const noexcept { return noMeterUnavailableReason_; }

signals:
    void stateChanged(orion::RemotePlayState state, QString status);
    // frameNumber: the sidecar decoder frame seq this preview image belongs to (FRAME-ID JOIN key;
    // -1 for placeholder frames). Lets the overlay composite the bbox on the exact frame it was
    // detected on. The frameDecoder_->decoded(QImage,int) signal is relayed straight to this.
    void frameReady(QImage frame, int frameNumber);
    void telemetryReady(orion::TelemetrySnapshot telemetry);
    void helperResult(QString operation, QJsonObject result);
    void setupMessage(QString message);
    void sidecarStatsChanged();
    // Emitted only after QProcess confirms that a newly-created sidecar process
    // started. Consumers may use this edge to reset process-local provenance
    // namespaces; an attempted/failed launch never emits it.
    void sidecarProcessGenerationStarted();
    void sidecarStopFinished();
    void sidecarDetectionReady(orion::DetectionResult result);
    // Direct receipt for the neutral controller-route command. This closes only the local
    // native-to-sidecar command handshake; latency telemetry must still echo the same generation
    // and route before any restored/model timing value becomes authoritative.
    void latencyRouteAttestationAck(bool accepted, QString deliveryRoute,
                                    quint64 attestationGeneration,
                                    quint64 scopeEpoch, QString scopeDigest,
                                    QString reason);
    // Pose-based landmark from the sidecar (no-meter mode). kind = "push" or "release".
    // frame_seq correlates to the sidecar's frame serial for staleness rejection.
    // Clock alignment: timing is relative to engine clock at message arrival,
    // compensated by noMeterDecodeCompMs. frame_seq is for staleness rejection only.
    void poseLandmarkReady(QString kind, int frameSeq, double confidence,
                           quint64 armToken);
    // Per-frame locked-player skeleton overlay: 17 COCO keypoints ([x,y,conf]) + box [x1,y1,x2,y2]
    // in full-frame pixel coords. Empty lists = no lock (clear the overlay).
    // Camera-anchor extras (all full-frame px, empty when absent):
    //   anchor      = [x,y] fixed bottom-center screen point
    //   lockCenter  = [x,y] center of the locked player's box
    //   indicator   = [x,y] under-player user-indicator marker, or EMPTY when not detected this frame
    void poseOverlayReady(QVariantList keypoints, QVariantList box,
                          QVariantList anchor, QVariantList lockCenter, QVariantList indicator);
    // [Track B / B3] Colour-calibration lifecycle status from the sidecar's ColorCalibrator
    // ({"event":"calibrate_meter_status",...}): per-shot progress + state machine
    // (seed|learning|locked|provisional|relearn). Feeds the meterCalibration* properties and
    // the MeterConfigPanel state badge. calibrating = a user calibrate_meter window is open.
    void meterCalibrationStatusReady(int shotsDone, int shotsNeeded, QString state,
                                     QString learnedDate, bool calibrating);
    // Reader detector health from the sidecar ({"event":"detector_health",...}, ~every 2 s
    // while a reader exists): provider ("cv-contour" or an ONNX provider name), infer_ms,
    // state (idle|pending|locked), calls/found/locks/drops/hot_submit/reseat_x and the
    // cv_* gate counters when the pure-CV proposer is active. Feeds ONLY the Meter
    // Detection card's status line (detectorHealthLine / detectorProvider) — never the
    // engine or any timing path.
    void detectorHealthReady(QJsonObject health);
    // [ORION_BANNER_VERDICT_LIVE 2026-09-14] One graded shot from the GAME'S OWN feedback
    // panel ({"event":"banner_verdict",...} from banner_verdict_live.py). Exactly one signal
    // per banner APPEARANCE, ~0.5-1.5 s after the release; a banner that stays up does not
    // re-fire. PRESENTATION ONLY - the owner's live tuning tally, never an engine input.
    //   timing       one of the grader's template words, uppercase, never empty and never
    //                "UNKNOWN": EXCELLENT | LATE | EARLY (the library holds exactly these
    //                three timing words today; treat any other word as "not green" rather
    //                than assuming a closed set).
    //   timingColor  "green" | "red" | "yellow" | "white" — the panel's own cell colour.
    //                GREEN (i.e. a made-by-timing shot) is the WORD, not the colour: the
    //                grader's green words are EXCELLENT and PERFECT.
    //   coverage     WIDE OPEN | OPEN | SEMI-OPEN | BOTHERED | LIGHT CONTEST | SOLID CONTEST
    //                | HEAVY CONTEST | SMOTHERED, or EMPTY when the panel showed no coverage
    //                cell (a 2-cell TIMING|DISTANCE panel).
    //   ncc          template match score for the timing word, always >= 0.90 by construction.
    //   frameEpochMs wall-clock ms of the frame the verdict was read from.
    //   seq          1-based verdict counter for this session (gap-free; use it to de-dupe).
    //   attributed   1 when the sidecar bound this panel to a BOT release, 0 otherwise (a replay
    //                screen, or a shot the owner took by hand). [ORION_BANNER_LEAD_TRIM
    //                2026-09-15] Only an attributed verdict may move the Shot Lead trim; an
    //                older sidecar that omits the field reads back as 0, which is fail-closed.
    //   releaseSeq   the SHOT-GATE EPOCH of that release -- the physical shot epoch this engine
    //                sent on shot_gate_release, which is the id the reader's release feed is
    //                keyed on. NOT ShotContext::releaseSeq, and -1 when unattributed.
    //   releaseDelayMs  panel-appearance minus release, in ms (-1 when unattributed). Diagnostic.
    //   hasCoverage  [ORION_BANNER_COVERAGE_ABSENT 2026-09-19] whether the panel HAD a coverage
    //                cell at all -- LAYOUT, not content. The game's 2-cell TIMING | DISTANCE
    //                panel has none (a drill, or any no-defender context), and it was 98 of the
    //                281 graded releases across the 2026-09-18 sessions. `coverage` alone cannot
    //                say which of two opposite things an empty word means:
    //                  hasCoverage=true,  coverage="" -> the cell exists and was unreadable
    //                  hasCoverage=false, coverage="" -> there is no cell and never will be
    //                An older sidecar that omits the field reads back TRUE, which keeps the
    //                2026-09-18 strict coverage gate byte-for-byte.
    // [ORION_ONSET_FF 2026-09-21] The sidecar's RTT sampler locked onto a DIFFERENT public court
    // (or its first one). Emitted once per change, from the same snapshot the telemetry reads.
    void courtChanged(QString courtIp);
    void bannerVerdict(QString timing, QString timingColor, QString coverage,
                       double ncc, qint64 frameEpochMs, int seq,
                       int attributed, qint64 releaseSeq, double releaseDelayMs,
                       bool hasCoverage);
    // [ORION_RELEASE_ORACLE_TRIM 2026-09-15 owner] The reader's post-release RETRACTION oracle,
    // ~300-500 ms after each release -- the banner-free input to the same Shot Lead trim.
    //   releaseSeq  the SHOT-GATE EPOCH (identical keying to bannerVerdict's), -1 when the
    //               sidecar could not attribute the measurement.
    //   gapPx       white-top -> green-bottom retraction gap in pixels. UNSIGNED: it is the
    //               DISTANCE from the green window and says nothing about which side. <= 3 px
    //               was EXCELLENT and >= 4 px a miss on 27/27 of the 09-15 framedump shots.
    //               NaN when the reader could not measure it.
    //   proxy       the reader's own word: "green" | "miss" | "unknown". Anything else, and
    //               "unknown" itself, is dropped by the engine rather than guessed at.
    void releaseOracle(qint64 releaseSeq, double gapPx, QString proxy);
    // [ORION_SHOT_RANGE 2026-09-17 owner] THREE or MID for one press, read off the ball
    // handler's nameplate "3" cell 40-120 ms after the Square edge (sidecar shot_range.py).
    //   releaseSeq  the SHOT-GATE EPOCH, the same key the banner and the oracle are attributed
    //               on -- NOT ShotContext::releaseSeq. -1 when the sidecar had no press.
    //   range       "three" | "mid" | "unknown". Anything else, and "unknown" itself, leaves the
    //               engine's reading where it was: the sidecar declining to answer must never
    //               CLEAR a range it already delivered for this press.
    //   conf        0..1, the agreement of the 2-3 sampled frames. Diagnostic: the engine keys
    //               on the word, never on the number.
    void shotRange(qint64 releaseSeq, QString range, double conf);
    void audioToggleBusyChanged(bool busy);
    // The detection sidecar exited. Emitted after the terminal stateChanged
    // notification; whileStreaming is the immutable process-generation verdict
    // that a live session lost its detector unexpectedly. It is false for user
    // teardown and intentional recovery restart.
    void sidecarExited(bool whileStreaming);
    // Live capture-card preview turned on/off (the pre-Connect HDMI feed). Drives the
    // controller's capturePreviewActive property so the QML panel shows the card feed while
    // the session is still Disconnected. Not emitted for the full streaming session.
    void previewActiveChanged(bool active);
    // [ORION_INPUT_DEAD_UX 2026-08-30] Terminal input-session failure, classified. Emitted AFTER
    // the terminal stateChanged(Error/…) and any warm-preview restore, exactly once per failed
    // promotion / input-recovery attempt, so OrionAppController can drive the bounded auto-retry
    // and the input-dead overlay from a settled state. failureClass carries
    // orion::InputSessionFailureClass (int on the wire; see InputSessionRetryPolicy.h for the
    // per-class retry safety argument). wakeObserved = a rest-mode console wake was in progress
    // for this attempt (the retry planner then waits out the boot instead of fighting it).
    // This signal classifies EXISTING failures only — it never adjudicates readiness and cannot
    // move the session state.
    void inputSessionFailure(int failureClass, bool wakeObserved);
    // Synchronous edge emitted after the in-place recovery command is accepted by
    // the existing sidecar. Consumers must revoke input/fire authority while
    // retaining the current capture, detector, and learned-timing generation.
    void inputRecoveryStarted();
    // FIX 2: the sidecar reported what this install can actually do ({"event":"capabilities"}).
    // `noMeterAvailable` is false when the pose/skele dependencies (ultralytics/torch) or the
    // three models/*.pt weights are not present — i.e. selecting "No Meter" would produce a bot
    // that silently never fires. `reason` is a human-readable list of what is missing.
    // INTEGRATOR: relay this to a Q_PROPERTY(bool noMeterAvailable) on OrionAppController so the
    // QML gate in RemotePlayPage.qml can see it (see the report / RemotePlayPage.qml comment).
    void sidecarCapabilitiesChanged(bool noMeterAvailable, QString reason);

private:
    friend class RemotePlaySessionTestAccess;
    void setState(RemotePlayState state, QString status);
    void emitPlaceholderFrame();
    void runHelper(const QString& operation, const QStringList& args);
    [[nodiscard]] QString pythonExecutable() const;
    [[nodiscard]] QString helperPath() const;
    // Persist a safe proof of the exact Remote Play client image selected for
    // this launch/recovery. Production callers fail closed if the image cannot
    // be read and hashed.
    bool reportRemotePlayExecutableIdentity(const QString& path, const QString& context);

    void startSidecar();
    void stopSidecar();
    void scheduleSidecarStart(int delayMs);
    void prepareSidecarCaptureInventory();
    // Connect fast-path: promote the already-running warm PREVIEW sidecar (capture card + detector
    // live) straight into a full streaming session by COMMANDING it to launch Chiaki/input over
    // stdin, instead of tearing it down and cold-rebuilding a new process. Only valid when the warm
    // source matches the connect source (capture-card, Chiaki input-only) — see
    // shouldReuseWarmSidecarForConnect. Keeps the card feed + detector flowing uninterrupted.
    void promoteWarmPreviewToStream();
    // Return ownership of an uninterrupted capture-card sidecar to preview mode after the Chiaki
    // input promotion fails. This preserves detector/video continuity while keeping Error visible
    // and automation without route authority.
    void restoreWarmPreviewAfterPromotionFailure();
    void onSidecarStdout();
    void onSidecarStderr();
    void handleSidecarMessage(const QJsonObject& msg);
    // [D3] Fast path for the sidecar's preview {"event":"frame"} line: pulls `jpeg_b64` and
    // `frame_number` out of the raw line by byte offset and hands the base64 straight to the
    // FrameDecoder worker, with NO QJsonDocument parse. The line is 200-595 KB during gameplay and
    // arrives ~56x/sec; parsing it on the GUI thread inflated the base64 into a UTF-16 QString
    // (2x the bytes) and then copied it again out of the document, all in competition with QML
    // paint. `data`/`len` point INTO the caller's receive buffer and nothing is retained past the
    // call. Returns false when the line is not the expected shape, so the caller can fall back to
    // the ordinary full parse (behaviour then is byte-identical to the old path).
    bool submitPreviewFrameLine(const char* data, qsizetype len);
    // Allocation-free fast path for the sidecar's fixed-order frame_chunk
    // records.  Failed parses fall back to QJsonDocument for compatibility.
    bool submitPreviewFrameChunkLine(const char* data, qsizetype len);
    void submitPreviewPayload(QByteArray jpegBase64, int frameNumber);
    // Display-only pacing. Detector/source frames never enter this queue; QImage
    // and decoder frame id stay paired so overlay joins remain exact.
    void queuePreviewFrame(QImage image, int frameNumber);
    void presentNextPreviewFrame();
    void presentNextPreviewFrameAt(qint64 nowNs, bool renderClockTick);
    void resetPreviewPresentation(bool acceptFrames);
    void updatePreviewPresentationCadence(bool resetRateClass = false);
    void scheduleNextPreviewPresentation(qint64 nowNs);
    // Display-only SHM lifecycle. A monotonically increasing source epoch
    // prevents an in-flight read from an exited/restarted sidecar from entering
    // the new process's frame namespace.
    void beginShmSourceEpoch();
    void retireShmSourceEpoch();
    void resetShmPresentationTimingDiagnostics();
    void handleShmPumpBatch(const SharedMemoryFramePumpBatch& batch);
    // Coordinate the one-way display transport handoff without restarting the
    // sidecar/capture source. Producer-initiated handoffs are accepted only
    // when their per-session ready-event name matches the current SHM epoch.
    bool switchPreviewToJpegFallback(const QString& reason,
                                     int firstJpegFrameNumber,
                                     bool requestSidecar);
    void handlePreviewTransportHandoff(const QJsonObject& msg);
    // [D3] Discard the pending receive buffer if an UNTERMINATED line has grown past
    // kSidecarBufferMaxBytes, so a sidecar that stops emitting newlines (or a corrupted stream)
    // cannot grow it without limit. Complete lines are always consumed before this runs, so the
    // only thing this can ever drop is a partial line; the stream resynchronises at the next '\n'.
    void boundSidecarBuffer();
    // Returns false when the command could NOT be handed to the sidecar (process gone / write
    // refused). Was void, so a command written into the void was indistinguishable from one that
    // was executed — which is how a `start_stream` that never reached anything could still end in
    // "Autogreen running" (FIX 1). Existing call sites that ignore the result are unaffected.
    bool sendSidecarCommand(const QJsonObject& cmd);
    // Rolling stderr tail (T5 observability): every non-empty stderr line is recorded
    // (trimmed to 300 chars, last kSidecarStderrTailMax kept) so a sidecar death can be
    // explained from orion_native.log even when the traceback matched no relay filter.
    void recordSidecarStderrLine(const QByteArray& line);
    void pushRemapUpdate();
    [[nodiscard]] QString sidecarScriptPath() const;
    [[nodiscard]] QByteArray buildSidecarConfig() const;
    void scheduleAudioApply(bool muted);
    void cancelAudioApply();
    void setAudioBusy(bool busy);
#ifdef Q_OS_WIN
    void assignSidecarToJob(QProcess* process);
#endif

    AppConfigData config_;
    RemotePlayState state_ = RemotePlayState::Disconnected;
    QString statusText_ = QStringLiteral("Disconnected");
    TelemetrySnapshot telemetry_;
    QString lastCourtIp_;   // [ORION_ONSET_FF] last court the sampler reported
    QTimer frameTimer_;
    int frameCounter_ = 0;
    QProcess* helperProcess_ = nullptr;
    QProcess* sidecarProcess_ = nullptr;
    QPointer<AsyncProcessRetirer> retiringSidecar_;
    bool captureInventoryPending_ = false;
    bool captureInventoryPrepared_ = false;
    bool captureInventoryStartRequested_ = false;
    quint64 captureInventoryStartGeneration_ = 0;
    bool sidecarStartAfterStop_ = false;
    quint64 sidecarStartAfterStopGeneration_ = 0;
    int sidecarStartAfterStopDelayMs_ = 0;
    // Off-GUI-thread preview JPEG decoder. Owns a dedicated worker thread with a 1-deep drop-oldest
    // mailbox; its decoded(QImage) signal is relayed to frameReady on the GUI thread so the render
    // thread never blocks on base64/JPEG decode. Child QObject: destroyed (and its thread joined) with
    // this session. See handleSidecarMessage's "frame" branch.
    FrameDecoder* frameDecoder_ = nullptr;
    // [ORION_DISCONNECT_AUDIT 2026-09-19] F7: bumped by start() and stop() ONLY -- the
    // two places where the user changes what this session is supposed to be doing. It
    // exists so a deferred lifecycle timer can tell "the intent that armed me" from
    // "a later intent that happens to leave the same state_". Deliberately separate
    // from streamPromoteGeneration_/inputRecoveryGeneration_, which are also bumped by
    // process-level events (QProcess::finished, startSidecar) and would therefore
    // cancel a handoff that is legitimately waiting for exactly that process to die.
    quint64 sessionIntentGeneration_ = 0;
    // [ORION_CONNECT_LATENCY 2026-09-19] Connect stage clock. Started at the user's
    // click (beginConnectStopwatch) and read at each stage boundary so the native
    // half of the connect is measurable straight from orion_native.log instead of by
    // differencing unrelated lines. Diagnostic only: nothing branches on it.
    QElapsedTimer connectStopwatch_;
    bool connectStopwatchArmed_ = false;
    QChronoTimer previewPresentationTimer_;
    QElapsedTimer previewPresentationClock_;
    PreviewPresentationBuffer previewPresentationBuffer_;
    PreviewPresentationBuffer::SourceRateClass previewPresentationRateClass_;
    PreviewPresentationBuffer::AdaptiveCadence previewPresentationCadence_;
    bool previewPresentationAccepting_ = false;
    bool previewPresentationPrimed_ = false;
    bool previewPresentationRecovering_ = false;
    bool previewPresentationEverPresented_ = false;
    bool previewPresentationRenderClockActive_ = false;
    qint64 previewPresentationPrimeStartedNs_ = 0;
    qint64 previewPresentationPeriodNs_ = 0;
    qint64 previewPresentationNextDeadlineNs_ = 0;
    qint64 previewPresentationLastRenderTickNs_ = 0;
    qint64 previewPresentationRenderTickPeriodNs_ = 0;
    qint64 previewPresentationLastFrameNs_ = 0;
    qint64 previewPresentationWindowMaxGapNs_ = 0;
    qint64 previewPresentationWindowMaxRenderTickGapNs_ = 0;
    quint64 previewPresentationInputFrames_ = 0;
    quint64 previewPresentationFrames_ = 0;
    quint64 previewPresentationDroppedFrames_ = 0;
    quint64 previewPresentationUnderflows_ = 0;
    std::size_t previewPresentationMaxDepth_ = 0;
    // Preferred display transport. The persistent worker owns mapping/mutex
    // reads and the 2.64 MiB owning copy off the GUI thread, with one pending
    // notification + one ready image + one queued GUI wake-up at most.
    // Detector pixels never pass through it; JPEG remains an in-place fallback.
    SharedMemoryFramePump shmPump_;
    quint64 shmSourceEpoch_ = 0;
    SharedMemoryTransportNames shmTransportNames_;
    bool shmSourceActive_ = false;
    bool shmReaderOpen_ = false;
    int shmOpenFailures_ = 0;
    int shmReadFailures_ = 0;
    quint64 shmFramesRead_ = 0;
    quint64 lastShmFramesReadCount_ = 0;
    quint64 shmEventWaitTimeouts_ = 0;
    quint64 shmEventWaitFailures_ = 0;
    quint64 shmEventGenerationProbes_ = 0;
    quint64 shmEventNotificationLosses_ = 0;
    quint64 shmReadyFrameReplaced_ = 0;
    quint64 shmDeliveryScheduleFailures_ = 0;
    // Producer-QPC -> pump-copy -> GUI-dispatch diagnostics. Values are accepted
    // only when all three stamps are ordered and within the bounded window.
    double shmSourceToReadAgeMs_ = 0.0;
    double shmReadToDispatchAgeMs_ = 0.0;
    double shmSourceToDispatchAgeMs_ = 0.0;
    double shmSourceToReadWindowMaxMs_ = 0.0;
    double shmReadToDispatchWindowMaxMs_ = 0.0;
    double shmSourceToDispatchWindowMaxMs_ = 0.0;
    quint64 shmPresentationTimestampRejects_ = 0;
    bool shmFallbackRequested_ = false;
    int jpegFallbackFirstFrameNumber_ = 0;
#ifdef Q_OS_WIN
    // A KILL_ON_JOB_CLOSE job prevents an app crash from leaving a Python
    // sidecar (and its capture-card handle) behind.
    void* sidecarJob_ = nullptr;
#endif
    // True while WE are tearing the sidecar down on purpose (restartSidecar). The
    // resulting QProcess::finished must report sidecarExited(false) so the
    // controller's crash counter does not escalate an orchestrated recovery
    // restart straight into safe mode (a single stall would otherwise count twice).
    bool intentionalSidecarRestart_ = false;
    // True between restartSidecar() arming its deferred respawn and that respawn actually firing.
    // The respawn is deferred by the capture-card release beat (up to 3s), and a user disconnect
    // landing inside that window used to be silently undone: the timer fired anyway, spawned a
    // fresh sidecar, which relaunched chiaki -> the PS5 reconnected unbidden, while also racing
    // startCapturePreview() for the single-open Elgato. stop() clears this so the pending respawn
    // stands down. NOTE: the obvious guard (state_ == Running/Connecting, as used by the warm-
    // preview handoff timer in start()) does NOT work here — restartSidecar()'s own stopSidecar()
    // makes QProcess::finished fire, which sets state_ to Disconnected before the beat elapses, so
    // a state check would block EVERY watchdog restart. This flag records the intent instead.
    bool sidecarRestartPending_ = false;
    // Live capture-card preview mode: the sidecar was started WITHOUT Chiaki (auto_launch_client
    // off) to show the HDMI feed before Connect. Cleared the moment Connect upgrades to the full
    // pipeline (which hands the single-open Elgato over via a paced stop+restart).
    bool previewMode_ = false;
    // Immutable identity of the process actually holding the preview device.
    // applyConfig() may change the requested settings while that process lives.
    CaptureSidecarLaunchIdentity activeSidecarLaunchIdentity_;
    quint64 activeSidecarSessionGeneration_ = 0;
    qint64 activeSidecarPid_ = 0;
    quint64 activeSidecarCreationTime100ns_ = 0;
    QString sidecarStopInitiator_ = QStringLiteral("launcher_stop");
    // --- FIX 1: warm-preview -> stream promotion result tracking -------------------------------
    // A compliant sidecar brackets the promotion: {"event":"stream_promote","state":"begin"} on
    // receipt, then either {"event":"started"} (Chiaki/input up) or {"event":"error"} (it is NOT,
    // so no input can reach the console). streamPromoteAcked_ records that the RUNNING sidecar
    // speaks this protocol. It is a PROGRESS signal, not the safety gate: a missing `begin` is no
    // longer fatal (it was, and it false-failed live sidecars -- see the note at the
    // kStreamPromoteFallbackMs timer). Legacy process-liveness still may never promote to Running,
    // because sidecarStartedHasInputAuthority() requires an explicit input_ready on `started`.
    bool streamPromotePending_ = false;
    bool streamPromoteAcked_ = false;
    // >0 once the sidecar reported a rest-mode wake in progress: the original 20s deadline
    // timer stands down and a second timer of this length (sidecar budget + headroom) fires
    // instead. Reset with streamPromoteAcked_.
    int streamPromoteDeadlineExtendMs_ = 0;
    void fireStreamPromoteDeadline(quint64 generation, int deadlineMs);
    // True only while Connect is upgrading an already-live capture-card preview. A failed verdict
    // may restore that exact sidecar to preview ownership instead of tearing down/reopening Elgato.
    bool streamPromoteFromWarmPreview_ = false;
    // Set when this sidecar generation's promotion failed/timed out. A late
    // `started` from the same blocking Python call cannot resurrect Running;
    // startSidecar clears it for a genuinely new process generation.
    bool rejectLateSidecarStarted_ = false;
    // Bumped per promotion so a deferred fallback/deadline timer from an earlier connect can never
    // act on a later session (the same hazard the sidecarRestartPending_ guard exists for).
    quint64 streamPromoteGeneration_ = 0;
    // Progress-note beat and hard verdict deadline. Python's streaminfo proof has a 15 s budget;
    // the native deadline leaves five seconds for cleanup + the final JSON verdict and is the ONLY
    // fatal bound if the sidecar thread/stdout wedges. The 2.5 s beat merely refreshes the
    // Connecting message -- it must never adjudicate, because ack latency is not ack absence.
    static constexpr int kStreamPromoteFallbackMs = 2500;
    static constexpr int kStreamPromoteDeadlineMs = 20000;
    // Input-only recovery has its own generation and native deadline. Without this, a blocked
    // Python/Chiaki retry could leave route authority pending indefinitely, and a late ready event
    // could revive a session after timeout/disconnect. The outer stream remains Running so the
    // capture/detector/timing generation is retained; input authority is gated independently.
    bool inputRecoveryPending_ = false;
    bool rejectLateInputRecoveryReady_ = false;
    quint64 inputRecoveryGeneration_ = 0;
    // FIX 2: last {"event":"capabilities"} verdict. True by default = "assume available" so a
    // sidecar that never reports capabilities preserves today's behaviour exactly.
    bool noMeterAvailable_ = true;
    QString noMeterUnavailableReason_;
    QByteArray sidecarBuffer_;
    PreviewFrameChunkAssembler previewChunks_;
    // [D3] Hard cap on the UNTERMINATED tail of sidecarBuffer_. A single preview line can legitimately
    // reach ~1.5 MB (1920 px wide at quality 98, base64-inflated), so this leaves several whole lines
    // of slack while still bounding a stuck/garbage stream. See boundSidecarBuffer().
    static constexpr qsizetype kSidecarBufferMaxBytes = 8 * 1024 * 1024;
    qint64 lastSidecarBufferOverflowMs_ = 0;
    // [D3] True while onSidecarStdout is dispatching parsed messages. A handler can emit signals that
    // reach QML, and (in principle) spin a nested event loop that re-enters this slot; the re-entrant
    // call then only appends its bytes and returns, and the outer call re-scans the buffer when its
    // current pass finishes. That keeps dispatch strictly FIFO and keeps the pointers the dispatch
    // loop holds into its detached snapshot valid for the whole pass.
    bool sidecarDispatching_ = false;
    // Throttle for relaying sidecar WARNING lines to the setup log (errors are
    // never throttled). Keeps a noisy warning from flooding orion_native.log.
    qint64 lastSidecarWarnLogMs_ = 0;
    static constexpr qint64 kSidecarWarnThrottleMs = 1000;
    // [ORION_AUTHORITY_DEATH 2026-08-10] The raw revocation lines are precise but
    // unreadable to a customer ("actual route is not configured DirectShow index 0").
    // The revocation is one-way for the sidecar's life (see remote_play_orchestrator
    // _guard_capture_latency_route: the already_revoked branch never re-tests the
    // route, and the empty scope independently fails the authority check), so the bot
    // is dead until a full restart and nothing else says so. Announce the consequence
    // and the remedy in plain language, exactly once per session so it cannot flood.
    bool authorityDeathAnnounced_ = false;
    // Last N sidecar stderr lines; dumped (bypassing the WARNING throttle) when the
    // sidecar process exits so the final traceback is never silently lost (T5).
    QStringList sidecarStderrTail_;
    static constexpr int kSidecarStderrTailMax = 10;
    // Track W: last-good atomic name + stable-ID inventory. A timeout reuses its
    // names only for webcam-safe probing and strips every cached ID; a swapped
    // card at the same index therefore cannot inherit timing authority.
    VideoInputDeviceInventory cachedVideoDeviceInventory_;
    // Memoized result of pythonExecutable() — the cv2-import probe spawns a python process with
    // waitForFinished(8000) per candidate on the GUI thread, and it was recomputed on every
    // sidecar (re)start. Resolve once. mutable so the const getter can populate it.
    mutable QString cachedPythonExe_;
    QString rootDir_;
    int sidecarFps_ = 0;
    int requestedFps_ = 60;
    int captureLoopFps_ = 0;
    int uniqueFrameFps_ = 0;
    int lastSidecarDetectionFrameCount_ = -1;
    // Opt-in observation only: no timestamp here is release authority.
    quint64 meterPipeTraceEpoch_ = 0;
    qint64 meterPipeTraceStartMs_ = 0;
    int meterPipeTraceSamples_ = 0;
    // [E4] Last `frame_number` seen in a telemetry payload = the DECODER wire seq of the frame the
    // detector last reported on. This is the counter the preview `frame` stream stamps each JPEG
    // with, so it is the only one previewLag_ can legitimately be subtracted from (frame_count, the
    // unique-frame dedup key it used to use, is a different counter that cannot be joined against
    // the preview stream - see the comment on the telemetry `frame_number` parse). -1 until a
    // sidecar that reports the field has been seen, which keeps previewLag_ at 0.
    int lastSidecarDetectionFrameNumber_ = -1;
    int previewLag_ = 0;
    int captureWidth_ = 0;
    int captureHeight_ = 0;
    QString captureTier_;
    double duplicateFramePct_ = 0.0;
    // FIX 3: the sidecar's cumulative count of preview frames LOST to a fault (stash / resize /
    // JPEG encode), as opposed to the deliberate latest-wins shed. It used to be zero-visibility:
    // a detector or encode fault became an invisible dropped frame with no log line anywhere.
    // Reported here (throttled, only when it actually grows) so "the video is choppy" has a number.
    int previewDroppedFrames_ = 0;
    qint64 lastPreviewDropLogMs_ = 0;
    static constexpr qint64 kPreviewDropLogThrottleMs = 10000;
    // Native preview receive/decode observability. The sidecar can report a
    // healthy capture while the stdout transport or JPEG mailbox is shedding;
    // these deltas identify that display-only bottleneck directly.
    qint64 lastPreviewPipelineStatsMs_ = 0;
    quint64 lastPreviewSubmittedCount_ = 0;
    quint64 lastPreviewDecodedCount_ = 0;
    quint64 lastPreviewPresentedCount_ = 0;
    quint64 lastPreviewMailboxDropCount_ = 0;
    quint64 lastPreviewPresentationDropCount_ = 0;
    quint64 lastPreviewDisplayPresentedCount_ = 0;
    quint64 lastPreviewDisplayDroppedCount_ = 0;
    quint64 lastPreviewDisplayUnderflowCount_ = 0;
    static constexpr qint64 kPreviewPipelineStatsIntervalMs = 5000;
    double frameAgeMs_ = 0.0;
    // Time since the last UNIQUE (non-duplicate), detector-eligible frame.
    double pixelAgeMs_ = 0.0;
    // [CL-006] Monotonic receipt time of the last sidecar telemetry record.
    bool haveTelemetry_ = false;
    std::chrono::steady_clock::time_point lastTelemetryAt_{};
    // [CL-006 r2] Armed at every sidecar (re)start, so a sidecar that never reports is caught too.
    bool telemetryArmed_ = false;
    std::chrono::steady_clock::time_point telemetryArmedAt_{};
    [[nodiscard]] double silenceAged(double stored) const noexcept {
        return silenceAgedHealthMs(stored, telemetrySilentMs());
    }
    // Independent raw capture-transport liveness; does not revoke detector fail-closed behavior.
    double transportAgeMs_ = 0.0;
    double capturePublicationAgeMs_ = 0.0;
    // STAGE SPLIT: sidecar dispatch (_emit, autogreen_sidecar.py) -> this receipt. The
    // capture->decision staleness was one opaque number, so when two sessions ran p50
    // 31-36ms (+19ms vs baseline) there was no way to attribute it to sidecar-side work
    // or to the stdout transport. capture->emit is (emit_ts_ms - measurement_capture_ts_ms);
    // this member is the other half. Diagnostic ONLY -- it feeds no gate and no release
    // decision, so a missing/garbage stamp from an older sidecar can never affect timing.
    double sidecarEmitTransitMs_ = -1.0;
    std::vector<double> emitTransitSamples_;
    qint64 lastEmitTransitLogMs_ = 0;
    static constexpr qint64 kEmitTransitLogIntervalMs = 5000;
    bool backendFrozen_ = false;
    QString fpsProbeState_ = QStringLiteral("stable-60");
    double shotFillPct_ = 0.0;
    double shotConfidence_ = 0.0;
    QString shotState_ = QStringLiteral("Idle");
    QString algorithmText_ = QStringLiteral("Waiting for autogreen telemetry...");
    // Coalesce the human-readable diagnostic overlay (algorithmText_) rebuild to ~60 Hz. Under the
    // event-driven sidecar feed the meter JSON can arrive well above 60 Hz; the ~30-arg string build
    // is diagnostic-only and, when several telemetry lines are buffered in one readyRead burst, its
    // per-message cost delays the NEXT (newer) sample's sidecarDetectionReady emit. Detection stays
    // per-message (emit above the throttle); only this readout is coalesced. 0 -> first build runs.
    qint64 lastAlgorithmTextBuildMs_ = 0;
    static constexpr qint64 kAlgorithmTextThrottleMs = 16;
    QTimer audioRetryTimer_;
    int audioRetryAttempts_ = 0;
    bool pendingAudioMuted_ = false;
    bool audioToggleBusy_ = false;
};

} // namespace orion

Q_DECLARE_METATYPE(orion::RemotePlayState)
