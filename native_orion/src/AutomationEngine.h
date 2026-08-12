#pragma once

#include "AppConfig.h"
#include "OrionExports.h"
#include "OrionTypes.h"

#include <QtCore/QDateTime>
#include <QtCore/QElapsedTimer>
#include <QtCore/QHash>
#include <QtCore/QMap>
#include <QtCore/QObject>
#include <QtCore/QPair>
#include <QtCore/QQueue>
#include <QtCore/QStringList>
#include <QtCore/QVector>

#include <algorithm>
#include <array>
#include <atomic>
#include <cmath>
#include <deque>
#include <limits>

class AutomationEngineTests;

namespace orion {

// [ORION_USER_LOG] appendUserLog (fix 5b, default OFF -> byte-identical). A SEPARATE
// human-readable log stream (logs/orion_user.log): readable LOCAL timestamps + short
// plain-language event lines (Connected / Meter detected / Shot released / verdict / RTT),
// written ALONGSIDE — never instead of — the engineer-oriented logs/orion_native.log. The
// diagnostic log stays byte-identical (other tooling parses it): this sink buffers its own
// lines and writes ONLY its own file; while disabled every call is a no-op and no file is
// ever created. OrionAppController owns the instance and appends at its event-edge sites;
// the class lives HERE (AutomationCore) so OrionNativeTests can link it without pulling the
// GUI controller. Enabled by the ORION_USER_LOG env var (no settings field yet — AppConfig
// is untouched in this wave; plumb settings.append_user_log when AppConfig next changes).
class ORION_AUTOMATION_API UserFacingLog {
public:
    // ORION_USER_LOG truthiness: set AND not one of "0"/"false"/"off"/"no" (case-insensitive).
    [[nodiscard]] static bool envEnabled();
    void setEnabled(bool on) { enabled_ = on; }
    [[nodiscard]] bool enabled() const { return enabled_; }
    // Queue one plain-language line, timestamped NOW in LOCAL time. No-op while disabled.
    void append(const QString& plainText);
    // Append the queued lines to <logsDirPath>/orion_user.log (creating the directory/file on
    // first write) and clear the queue. Returns true when lines were actually written. Touches
    // no other file; disabled/empty sinks return false without any filesystem access.
    bool flushTo(const QString& logsDirPath);
    [[nodiscard]] const QStringList& pendingLines() const { return pending_; }
    // "yyyy-MM-dd HH:mm:ss  <text>" — a readable LOCAL wall-clock stamp (the diagnostic log
    // uses UTC ISO), two-space separated from the simplified plain text.
    [[nodiscard]] static QString formatLine(const QDateTime& localTime, const QString& plainText);
    [[nodiscard]] static QString fileName();   // "orion_user.log"
private:
    bool enabled_ = false;
    QStringList pending_;
};

// User-facing release truth is a two-phase event. AutomationEngine can decide
// the timing and emit releaseIssued, but only the local controller transport can
// confirm that the exact release output was accepted locally. That confirmation
// is not console/game receipt. Keeping the text private inside this tracker makes
// it unavailable at the issuance phase.
enum class UserReleaseFailureReason {
    TransportNotConfirmed,
    VirtualControllerDisconnected,
    ControllerRouteRevoked,
    RemotePlayDisconnected,
    PreciseWriteFailed,
};

class ORION_AUTOMATION_API UserFacingReleaseTracker final {
public:
    void stage(int releaseSeq, double fillPct, double targetPct,
               const QString& shotType, bool rttVerified, double rttMs);
    [[nodiscard]] QString confirm(int releaseSeq);
    [[nodiscard]] QString fail(int releaseSeq, UserReleaseFailureReason reason);
    [[nodiscard]] QString failPending(UserReleaseFailureReason reason);
    [[nodiscard]] static QString failureNotice(UserReleaseFailureReason reason);
    [[nodiscard]] bool pending() const noexcept { return pendingSeq_ >= 0; }
    [[nodiscard]] int pendingSeq() const noexcept { return pendingSeq_; }

private:
    void clear();

    int pendingSeq_ = -1;
    QString confirmedText_;
};

enum class UserMeterVisibilityNotice {
    None,
    Detected,
    NoLongerVisible,
};

// Plain-language visibility follows the same genuine-raw freshness evidence as
// the lock box. Engine-only states such as "accepted" and "idle_overlay" are
// deliberately absent: releasing a shot cannot manufacture a visibility loss.
[[nodiscard]] inline bool userMeterVisibleFromRawEvidence(
    bool genuineRawDetection, qint64 lastGenuineSeenMs, qint64 nowMs,
    qint64 freshnessMs) noexcept
{
    if (genuineRawDetection) {
        return true;
    }
    return lastGenuineSeenMs > 0 && nowMs >= lastGenuineSeenMs
        && freshnessMs > 0 && nowMs - lastGenuineSeenMs < freshnessMs;
}

[[nodiscard]] inline UserMeterVisibilityNotice userMeterVisibilityNotice(
    bool wasVisible, bool isVisible) noexcept
{
    if (wasVisible == isVisible) {
        return UserMeterVisibilityNotice::None;
    }
    return isVisible ? UserMeterVisibilityNotice::Detected
                     : UserMeterVisibilityNotice::NoLongerVisible;
}

struct RemapConfig {
    bool enabled = true;
    QString inputMode = QStringLiteral("square_only");
    // Tempo Shot is an output remap layered on the physical Square trigger. The
    // Square edge, ownership gate, timing model, and detector authority are shared
    // with ButtonShot; only the owned output becomes an RS gather/flick gesture.
    bool tempoRemapEnabled = false;
    // Legacy persisted compatibility. Runtime Tempo behavior is controlled by
    // tempoRemapEnabled and always remaps the Square-owned shot to gather/flick.
    QString tempoRemapType = QStringLiteral("button");
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] #88. Tempo remap CONSUMES Square: every owned path
    // clears XINPUT_GAMEPAD_X and drives the right stick instead (21 clear sites across 12
    // functions). That is the remap working as designed, but it also means Square's OTHER job --
    // the steal -- becomes unreachable while Tempo is on.
    //
    // The 2026-08-11 attempt (73f8fcb) tried to tell a "tap" from a "hold" and replay the tap the
    // remap had swallowed. That has to GUESS, and it guessed in the Python remap layer, which
    // cannot reach the console on a capture-card rig at all. Reopened.
    //
    // Give Square a SECOND HOME instead: a stick click emits a real Square and is exempt from the
    // remap. No timing heuristic, no replay window, nothing to race. Applied once on the FINAL
    // output in process(), so it cannot be undone by a later clear site.
    //
    // R3 is the default and L3 is deliberately NOT: L3 is turbo in NBA 2K, so binding Square there
    // fires a steal every time the player sprints. R3 is unused by this engine (grep
    // XINPUT_GAMEPAD_RIGHT_THUMB -- read in WinMmButtonMapping/OrionAppController, never acted on).
    bool squarePassthroughEnabled = true;
    // "r3" | "l3" | "none". Unrecognised values behave as "none" (feature inert, never a crash).
    QString squarePassthroughButton = QStringLiteral("r3");
    // No-Dip shot mode: a no-dip jumpshot skips the gather/dip, so it releases earlier. When on,
    // add noDipLeadMs to the release lead (live-tuned; default 0 = inert).
    bool noDipEnabled = false;
    double noDipLeadMs = 0.0;
    double tempoWaitMs = 0.0;
    double tempoFlickHoldMs = 50.0;
    double tempoFallbackTimeoutMs = 650.0;
    double tempoMinStickHoldMs = 0.0;
    double minHoldMs = 90.0;
    double maxHoldMs = 1200.0;
    double fixedHoldMs = 650.0;
    double releasePulseMs = 50.0;
    double earlyLateOffsetMs = 0.0;
    QString greenWindowTarget = QStringLiteral("tip");
    double fallbackTargetPct = 96.0;
    bool greenWindowPriority = true;
    double stickDownThreshold = 50.0;
    double stickUpThreshold = 50.0;
    bool gotoEnabled = false;
    double gotoFlickHoldMs = 50.0;
    int gotoArmFrames = 2;
    double confidenceGate = 0.32;
    int stableFramesRequired = 3;
    // A detection sample older than this (capture->detect latency) can't seed a
    // NEW velocity point, but must NOT tear down the confirmed green window or
    // last-known fill. Brief stream/age jitter under this bound is still usable.
    // (Was a hard 180ms reject that zeroed meterDetected → every shot rode to 100%.)
    double staleFrameMaxMs = 400.0;
    // How long a detection sample stays "fresh" enough to drive a release after
    // the last telemetry update arrived (native receipt clock). Widened from the
    // old 160ms so ~30Hz sidecar telemetry + GC/jitter bursts don't flap it off.
    double meterFreshWindowMs = 180.0;  // tightened from 220 — less stale data at 60fps
    // Release authority is intentionally much shorter than display freshness and
    // includes capture age at arrival. At 50ms a 60fps detector gets roughly
    // 2-3 source frames of interpolation, but a capture gap cannot inherit a
    // 180ms display lease and fire from an old image.
    double strictReleaseMaxSourceAgeMs = 50.0;
    // EXPERIMENT (default OFF, settings memory_trust_enabled): treat a very-recent
    // (<= memoryTrustMaxAgeMs since the last GENUINE accept) detector meter_memory echo as
    // fresh-equivalent for lastSampleFreshAccept ONLY — bridges a single-frame wide-zone
    // detection blink without feeding the velocity sampler or loosening any detector gate.
    // The bound is anchored to the last genuine accept (echoes never advance it), so a real
    // multi-frame dropout still goes stale. Converts the recoverable wide-zone meter_memory
    // misses measured by tools/diagnostics/replay_detector.py --rise-report.
    bool memoryTrustEnabled = false;
    double memoryTrustMaxAgeMs = 45.0;
    double memoryTrustFillSlackPct = 2.0;
    // TIP-GUARD: never promote a meter_memory echo whose held fill is at/above this %% — near the
    // green window a STALE held value is the early/mistimed-release risk (offline sim: 84 of 346
    // promoted frames were >=90% fill near green). Below it (the rising body) a 1-frame-old echo is
    // safe (monotonic rise). Keeps the freshness gain where it's safe; requires a FRESH read at the
    // tip. Default 85 leaves a margin below the ~92-100% green band.
    double memoryTrustMaxFillPct = 85.0;
    double controllerChainMs = 8.0;
    // Remote-Play pipeline lead (ms): the encode -> network -> decode -> display
    // latency on the DOWNSTREAM side plus input encode/poll on the UPSTREAM side
    // that the bot cannot directly measure (frameAge only covers capture->detect).
    // Added to the release lead so the press lands in the green window instead of
    // arriving ~a pipeline-latency LATE. ~55ms is a typical Remote-Play figure;
    // tune with early_late_offset_ms (raise if still late, lower if now early).
    double remotePlayPipelineMs = 55.0;
    // Capture-staleness lead (ms): shot_.frameAgeMs is how OLD the meter sample is
    // (capture -> engine). The predictor extrapolates from that already-N-ms-stale
    // fill, so the lead must include it — the meter kept rising during those N ms.
    // Folded into effectiveLatency but CLAMPED to this cap so one momentary huge frame
    // age (a capture hitch / dropout) can't massively over-lead and fire early.
    double captureAgeLeadCapMs = 20.0;  // tightened from 25 — less over-lead on frame drops
    double learningBiasPct = 0.0;
    // Banner-calibrate: when true, FREEZE the post-release self-learning grader (learnFromOutcome) so
    // the per-type clock/offset hold at their learning.json values. The meter grader can't tell green
    // from late at the dead-top (both peak ~100 then recede) and false-LATEs, creeping the clock; the
    // banner-dialed clocks then ship open-loop. Set from settings.freeze_calibration / ORION_FREEZE_CAL.
    bool calibrationFrozen = true;
    // Banner-calibrate (settings.banner_calibration): grade each shot from the on-screen TIMING banner
    // (green=on-target / red=off) rather than the meter self-grade, which false-LATEs at the dead-top.
    // Calibration ONLY -- live timing never reads the banner.
    bool bannerCalibration = false;
    double squareHoldArmMs = 75.0;
    // Legacy compatibility value. TempoSquare deliberately uses squareHoldArmMs;
    // a remap toggle must never select a different ownership/timing authority.
    double tempoSquareHoldArmMs = 28.0;
    double stickHoldArmMs = 135.0;
    double gotoHoldArmMs = 120.0;
    double standstillCommitMinMs = 140.0;
    double fadeCommitMinMs = 360.0;
    double movingCommitMinMs = 360.0;
    double gotoCommitMinMs = 360.0;
    // [ORION_GOTO_CLOCK] Floor for the Go-To hold-start deadline. The deadline
    // still requires a validated anchor and a current genuine detector frame.
    double gotoBlindFireFloorMs = 1600.0;
    // Go-To shots animate longer than normal shots, so they get a higher base
    // interval for the 3x post-meter physical recovery bound.
    double gotoMaxHoldMs = 1400.0;
    // When a shot is HELD but the meter never surfaces, wait up to this cap
    // (mode-aware), then abort automation and return the physical input unchanged. Go-To /
    // half-court / contested animations can take several seconds to surface the
    // meter, so they get a much longer cap than button shots. These are measured
    // from hold start; the post-meter ceilings above are measured from when the
    // meter actually appeared.
    double gotoNoMeterAbortMs = 6000.0;
    // A1 (2026-07-25): raised 1500 -> 2800. The live Go-To shots acquired their meter at
    // firstMeterMs = 1424 / 1535 / 1445 — i.e. the REAL meter-acquire distribution straddles the
    // old 1500ms button cap. 2800 covers that distribution with margin; if no
    // meter arrives, automation relinquishes ownership rather than fabricating a release.
    double buttonNoMeterAbortMs = 2800.0;
    // [ORION_GOTO_METER_WAIT] (fix 2b, default OFF -> byte-identical). Go-To animations vary in
    // length, so a Go-To must WAIT — holding the RS-up input — until a REAL meter is on screen
    // and then time the release off the live rise (the meter/vision paths). When ON:
    //   * the hold-start feedforward clock cannot fire a meterless Go-To, and
    //   * the no-meter abort may wait up to gotoMeterWaitCapMs before returning control.
    // Once a meter genuinely appears, everything times exactly as before (meter-appear clock +
    // green/predictive/reactive vision paths + the post-meter gotoMaxHoldMs ceiling). Enabled by
    // the ORION_GOTO_METER_WAIT env var in applyConfig (no settings field yet — AppConfig is
    // untouched in this wave; plumb settings.goto_meter_wait when AppConfig next changes).
    bool gotoMeterWait = false;
    double gotoMeterWaitCapMs = 15000.0;
    // Compile-time-gated lab-only pose timing. Production normalizes this false.
    bool noMeterEnabled = false;
    QString noMeterReleasePoint = QStringLiteral("Push");
    // Default from offline validation: push→meter-tip median = 83ms across 41 shots
    // (MAD-cleaned stdev = 19ms = STRONG). The engine fires at pushArrival + baseOffset - decodeComp.
    double noMeterBaseOffsetMs = 83.0;
    double noMeterDecodeCompMs = 7.5;
    // Minimum pose landmark confidence to accept (0-1). Landmarks below this are rejected.
    double noMeterConfidenceGate = 0.70;
    // If push fires but no release arrives within this window (ms), abort instead of blind fire.
    double noMeterPushReleaseWindowMs = 200.0;
    // Per-shot-type no-meter offsets (ms), added to baseOffset. Same 8 types as meter mode.
    QMap<QString, double> noMeterShotTypeOffsets;
    // Go-To max-hold release floor. A detection dropout freezes the fill low (peak
    // stops advancing -> "not developing"), which previously let max_hold_safety dump
    // the Go-To at 13-42% while the REAL meter was near the top (confirmed by 10:28
    // video correlation: log_fill 13-27% vs real peak 92-100%). Below this fill a
    // Go-To NEVER takes an elapsed-only release: it keeps holding for a fresh/recovered meter
    // and, if none surfaces by the absolute hard cap, ABORTS. Set high
    // enough to reject dropout-frozen low fills, low enough that a genuinely contested
    // Go-To whose green sits low still releases via the predictive/green paths (those
    // are unaffected by this floor — it only gates the legacy elapsed-time candidate).
    double gotoMinReleaseFillPct = 55.0;
    // Non-Go-To (Square/fade) low-fill dump floor. Neither the 650ms tempo timeout nor
    // the maxHoldMs ceiling may release a Square/fade below this fill — a meter stalled
    // below it keeps holding (it may still recover/rise) until the ABSOLUTE hard cap
    // (postMeterCeiling*3) aborts automation. Stops premature low dumps (live: a Left Fade
    // dumped at 49% via timeout_fallback) without ever leaving the user's shoot press
    // under automation control. The predictive/green/reactive paths are unaffected.
    double nonGotoMinReleaseFillPct = 70.0;
    // Reactive-release "the meter actually rose this shot" gate. A non-Go-To reactive
    // release (fill already >= target) is only allowed if THIS shot observed a FRESH meter
    // sample at least this many %% BELOW the target — i.e. we watched the meter climb INTO
    // the target. A meter that is already at/above target from the first sample is a
    // lingering meter from the previous shot still on screen, or a weak false-positive
    // (live: conf~0.50 fill=100 with no green) — NOT this shot's own timed meter. Firing
    // reactively on it makes a Standstill release ~150ms into the hold, ignoring the real
    // meter ("doesn't time at all"). The predictive/green paths are unaffected (a real
    // rising meter fires those before target); this only gates the at-target last resort.
    double reactiveRiseMarginPct = 25.0;
    // Strict Go-To arming: RS-up must be vertical-DOMINANT — |rightStickX| may not
    // exceed this fraction of |rightStickY| — so a diagonal/left/right RS (a dribble
    // move) does not arm Go-To. Lower = stricter (more purely vertical).
    double gotoLateralMaxRatio = 0.5;
    // Predictive-release reachability gate. The further BELOW the target a
    // predictive release fires, the more it must trust an uncertain velocity AND
    // latency extrapolated across the meter's non-linear top (it DECELERATES near
    // full, then the value caps at 100 and recedes). A fixed lead error therefore
    // lands a FAST meter much further below green than a slow one — exactly the
    // live signature: a standstill (fast fill) releases ~57% and misses low, while
    // the slower right fade releases ~82% and greens. Capping how far below target
    // a predictive release may fire makes a fast meter WAIT until it is near the
    // top, where it caps into the green band regardless of the exact latency —
    // shrinking the per-shot-type spread. Slow meters (small velocity*lead) stay
    // unaffected. The effective ceiling is maxPredictiveRisePct + reachabilitySlackPct
    // (was a hardcoded 45 + 12 = 57). Tune UP toward 45/12 if shots start landing
    // LATE/over the green; tune DOWN if fast shots still land short of green.
    double maxPredictiveRisePct = 25.0;
    double reachabilitySlackPct = 6.0;
    // === A3 (2026-07-25) REACHABILITY HORIZON =========================================
    // The reach test above asks "is the tip within ONE LEAD's worth of rise?" — but that is the
    // wrong question. It abandons a shot that demonstrably WILL reach the tip, just not within the
    // next `effectiveLatency` ms: live seq 9/12/19 had healthy velocity and a valid forward
    // crossing (crossingEta 210/355/143 ms) yet reported withinReach=0, so the vision rungs never
    // opened and the blind feedforward clock owned the release. Widen `expectedRisePct` from the
    // one-lead window to the rise the meter will genuinely make between now and its own predicted
    // crossing, bounded by BOTH a time horizon and a rise ceiling. The gate is NOT disabled: the
    // (now lead-aware) fill floor still decides how far below target a release may fire, and the
    // release itself still only happens at `crossing - effectiveLatency`, so opening the rung
    // earlier can never fire earlier — it only stops the shot being handed to the blind clock.
    double reachabilityCrossingHorizonMs = 400.0;   // crossings further out than this don't count
    double reachabilityMaxRisePct = 70.0;           // ceiling on the horizon-implied rise (pp)
    // Reachability cap on the OPEN-LOOP feedforward fire (do #1, the chief early-fire fix). The
    // feedforward fires AHEAD on the learned clock to beat view latency, so a TRUSTWORTHY fresh fill
    // usually still reads below the target at fire time (the reading lags the real rise). But when the
    // learned clock drifts SHORT it would fire no matter how LOW the fill is. The fire is allowed only
    // while a fresh fill is within one MAX lead's worth of rise of the target (the most-lenient vision
    // reach = nonGotoMaxPredictiveRisePct/maxPredictiveRisePct) PLUS this slack. Larger than the vision
    // reachabilitySlackPct on purpose: the feedforward intentionally fires early, so the slack keeps the
    // legitimate view-latency fire (a fast meter reading mid-rise) while still suppressing a gross early
    // fire at a genuinely low fill. An invisible/contested meter never receives clock authority.
    double ffReachabilitySlackPct = 12.0;
    // NON-Go-To (Standstill/fade) predictive reachability clamp — SEPARATE from the Go-To
    // maxPredictiveRisePct (kept conservative at 25). The +80 standstill A/B proved the 25/6
    // ceiling PINS the release at target-31, which lands LATE across the Remote-Play round
    // trip (every -80 shot LATE; +80 cap-pinned at target-31, only the easiest shot greened).
    // A higher non-Go-To clamp lets a Standstill release earlier — down to ~target-(this+slack)
    // ≈ target-45 — so the press lands in the green; Go-To stays on the conservative clamp.
    // Start at 39 (≈target-45); widen only after a live standstill batch proves it. A noisy
    // fast meter is still bounded by this AND the hard floor below.
    double nonGotoMaxPredictiveRisePct = 39.0;
    // Absolute hard floor for a non-Go-To PREDICTIVE/green release: never fire one below this
    // fill no matter how high the clamp or velocity — guards a noisy fast meter (or a low
    // green target) from dumping at absurd low fill. The blind safety dumps are gated
    // separately by nonGotoMinReleaseFillPct; this gates only the predictive/green paths.
    double nonGotoMinPredictiveFillPct = 42.0;
    // AUTONOMOUS TIP-VISION fire-near-tip floor (non-Go-To, autonomous_vision path only). Near the
    // tip the crossing predictor's remaining-ms IQR is ~10-15ms vs 40-70ms mid-rise, so the
    // tip-vision design only lets a predictive release fire once the fill is within one tip's-worth
    // of the top. Replaces the per-type nonGotoMinPredictiveFillPct (42) when globalApplies; the
    // per-type fallback keeps its 42 floor. Not settings-plumbed — an internal tuning constant.
    // C1 (2026-07-25) -- THE LEAD CAP. A FIXED fill floor silently CAPS the expressible lead: to
    // express a lead L at rise rate v the release must fire at `target - v*L`, so a floor F forbids
    // any lead longer than `(target - F)/v`. Live (target 99.6, v~0.25 %/ms) the 88 floor capped
    // every autonomous release at ~46ms of lead -- the bot was structurally LATE on every vision
    // shot with learned_latency_ms=75, and the release fills clustered at 88.9/90.1/90.7/91.3/91.9
    // (pinned against the floor, not spread by sampling). The floor's real intent is "don't fire
    // while the crossing prediction is unreliable"; that reliability gate already exists elsewhere
    // (the release only fires at crossing - effectiveLatency, i.e. once the tip is one lead away).
    // So the EFFECTIVE floor is now LEAD-AWARE:
    //     max(tipFireAbsMinFillPct, min(tipFireMinFillPct, target - v*effectiveLatency - slack))
    // applied in processHolding AND its reevaluateScheduleOnFreshSample mirror.
    // Slack (pp) BELOW the lead-implied fire fill. The fire lands exactly at target - v*L, so a
    // floor sitting exactly there races the sampler's own quantisation and re-pins the release a
    // tick late. Small on purpose: pure floor headroom, never a licence to fire early (the crossing
    // deadline still decides WHEN).
    double tipFireLeadSlackPct = 2.0;
    // Absolute sanity floor for the lead-aware lowering: a garbage velocity (torn frame / detector
    // blip) makes target - v*L wildly negative, which would otherwise open the predictive rung at
    // any fill. Deliberately EQUAL to nonGotoMinPredictiveFillPct so the per-type fallback path is
    // provably byte-identical (max(42, min(42, x)) == 42 for every x).
    double tipFireAbsMinFillPct = 42.0;
    double tipFireMinFillPct = 88.0;
    // [ORION_FADE_SAFETY] Fade-only overshoot bound. A fade fires on its per-type clock (see the
    // clock exemption): if that clock runs a touch LATE and none of the predictive/green paths have
    // fired yet, releasing at/above this fill (or the confirmed green ENTRY, whichever is LOWER)
    // beats a late miss — 2K's made-shot window is more forgiving slightly-early than late. Fades
    // ONLY; standstill/other types already land and keep their normal paths. An internal constant.
    double fadeSafetyFillPct = 92.0;
    // A green window at/above this width (track-%) is "comfortably visible" -> aim
    // for its CENTER. Narrower (miniscule) windows -> aim for the tip ("fill up").
    double visibleGreenWidthPct = 8.0;
    // Target when NO green window is present (contested): time the meter fully to
    // the top ("the make point").
    double fullMeterTargetPct = 100.0;
    // DEAD-TOP targeting: aim the green window's TOP EDGE (the contest-invariant make-point) for
    // ALL shot types, pulled DOWN by this margin (track-%). 0 = dead on the top edge. Raise a hair
    // (~2-3) for late-safety while a type is still converging, then ratchet back toward 0. Replaces
    // the old width-based center/tip split (visibleGreenWidthPct, now legacy).
    double meterTipMarginPct = 0.0;
    // Separate late-safety margin for fade shot types (Left Fade, Right Fade, Post Fade).
    // Fades have a longer animation and MAY benefit from a small late-safety pull below the
    // green window's top edge. DEFAULT 0.0 = dead top, byte-identical to meterTipMarginPct:
    // the 2.0 default shipped unmeasured and silently moved every green-confirmed fade target
    // 2pp early. Kept as an opt-in lever; raise it only with a live A/B behind it.
    double tipMarginFadePct = 0.0;
    // Fast green window confirmation: when true, confirm on the first valid green reading
    // (skip the 3-reading minimum). For very fast meter styles like Arrow2 Purple where
    // the green window is only ~30-40ms. Default false — opt in per meter style.
    bool greenConfirmFastPath = false;
    // Post-shot cooldown before another shot can arm. Go-To gets a longer cooldown:
    // the signature animation + gather needs to fully reset before the next go-to,
    // otherwise a still-held / quickly re-pushed stick re-triggers into a broken
    // animation. (The cooldown state also blocks re-arming, see processCooldown.)
    double cooldownMs = 200.0;
    double gotoCooldownMs = 650.0;
    double pumpFakePulseMs = 42.0;
    double movingSquareThreshold = 42.0;
    QMap<QString, double> shotTypeOffsets;
    QMap<QString, double> shotTypeLearnedOffsetMs;
    // Feedforward: per-shot-type learned hold-start->release time (the deterministic
    // meter animation clock). Lets the bot time a shot whose green window is a
    // contested/invisible sliver, on the clock, when vision can't.
    QMap<QString, double> shotTypeFeedforwardMs;
    double feedforwardGain = 0.25;   // EMA gain when learning the per-type clock
    double feedforwardMinMs = 150.0; // ignore implausibly short learned/used hold-start times
    // Which deterministic-clock ANCHOR the feedforward release times from:
    //   "hold_start"   -> release at holdStartMs + shotTypeFeedforwardMs  (jitter-free clock,
    //                     but carries the variable gather/animation-start lever arm — worse on
    //                     fast meters whose green window passes in less time).
    //   "meter_appear" -> release at firstMeterSeenMs + shotTypeMeterToReleaseMs (the FIXED
    //                     animation segment meter-appears->tip; far less shot-to-shot variance,
    //                     so a fast Standstill/Fade hits the tiny window as reliably as a slow
    //                     Right Fade). Relies on a clean low-latency feed. Falls back to the
    //                     hold-start clock when the meter never surfaces (invisible/contested).
    // Runtime knob (settings.json feedforward_anchor) so the two can be A/B'd live without a
    // rebuild; both clocks are kept warm (seeded in triggerRelease) regardless of the choice.
    QString feedforwardAnchor = QStringLiteral("meter_appear");
    // === Anchor validity (T2 carryover rejection; settings anchor_max_first_fill_pct /
    // anchor_rise_min_pct) ===
    // The meter-appear CLOCK may only anchor to a VALIDATED genuine-accept episode (see
    // ShotContext::anchorValidMs): first sight at/below anchorMaxFirstFillPct with a
    // non-descending confirm sample, OR a genuine rise of >= anchorRiseMinPct across the
    // episode. A lingering previous-shot meter (static/deflating ~100 fill panning with
    // the camera) is a REAL raw detection, so only this trajectory test can reject it.
    double anchorMaxFirstFillPct = 40.0;
    double anchorRiseMinPct = 3.0;
    // Candidate-A decision-budget flag (settings ownership_proof_two_frame, default OFF):
    // strict autonomous ownership needs 2 unique rising frames instead of 3. The rise
    // proof (anchorRiseMinPct) is unchanged and remains the false-lock discriminator.
    bool ownershipProofTwoFrame = false;
    // Tempo tip-parity flag (settings tempo_tip_parity, default OFF). TempoStick is the one
    // live-meter mode still carrying two pre-parity divergences on the CANONICAL autonomous
    // path (measured 2026-08-06; TempoSquare already releases path=live_meter_tip with the
    // full owned-square hold set — 10/10 recent tempo releases):
    //   (1) beginShot routes an evidence-promoted TempoStick through HoldState::Armed, a
    //       detection-confirm dwell with NO release path. Under autonomous vision ownership
    //       is only ever granted by the strict 3-frame rising proof, so the dwell re-proves
    //       weaker evidence than what promotion just required, while consuming ~1-3 detector
    //       frames of the measured 93ms anchor->deadline decision budget as a release
    //       blackout. This is precisely the 2026-07-24 TempoSquare finding.
    //   (2) processAutonomousLiveMeterHolding narrows every bounded hold to owned SQUARE:
    //       on measured-lead loss, an unresolved trajectory, a contested far-disagreeing
    //       estimate with proven runway, or one-frame-away phase-anchor imminence, a
    //       TempoStick ABORTS (or releases on the +61-92ms-early-biased sampler) where
    //       ButtonShot/TempoSquare hold within the same maxHoldMs ceiling.
    // With the flag ON both close: TempoStick promotes direct to Holding and joins the
    // owned-hold predicate. Fail-closed is untouched — every hold keeps its existing
    // absolute ceiling and no branch gains a release path that ButtonShot does not have.
    // The tempo movement transaction, stick intent dwells (stickHoldArmMs /
    // tempoMinStickHoldMs), ownership gesture gates, and the RS-up flick release output are
    // deliberately NOT changed: parity is the tip-timing pipeline, not the gesture.
    // The legacy (non-autonomous) TempoStick lane keeps its Armed dwell in all cases —
    // there ownership can begin without meter proof, which is what Armed actually verifies.
    bool tempoTipParity = false;
    // Go-To tip-parity flag (settings goto_tip_parity, default OFF). GoToStick is excluded
    // from every bounded-hold site ButtonShot enjoys in processAutonomousLiveMeterHolding —
    // the measured-lead-loss grace, the detector-blink lease continuation, the
    // unresolved-trajectory hold, the phase-anchor-imminent hold, the contested-runway hold
    // and the slow-meter defer — because they all gate on ownedSquare. Nothing in that
    // ladder is Square-specific (see the [ORION_TEMPO_PARITY] note at the ownedHold
    // definition): a Go-To's owned output is the same bot-held gesture (RS-up) with its own
    // maxHold ceiling and the same fail-closed teardown, yet in a recoverable state it
    // ABORTS (or releases on the +61-92ms-early-biased sampler) where a Square shot would
    // hold and recover. Live census 2026-08-04..06: Go-To was 5 of the 16 dated no-fire
    // aborts. With the flag ON GoToStick joins the ownedHold predicate (tick + subtick
    // mirror); beginShot needs no change — GoToStick already promotes direct to Holding.
    // Fail-closed untouched: every hold keeps its existing absolute maxHoldMs ceiling and
    // relock grace, no branch gains a release path, no token is created or moved earlier.
    // OFF = bit-identical to today for every mode; ON changes GoToStick only.
    bool gotoTipParity = false;
    // === Forward-crossing authority (T4; settings tip_gate_enabled) ====================
    // While a genuine >=3-sample rising trajectory has a credible future target
    // crossing whose latency-compensated fire ETA is within the rolling
    // tipGateCapMs window, vision owns timing and raw feedforward may neither fire nor schedule.
    // A fresh accepted flat/receding/no-crossing sample returns ownership to the
    // clock backstop. The cap is relative to NOW, not the raw clock deadline;
    // the old clock-relative cap was the overdue/scheduler bypass this removes.
    // tipGateEnabled=false restores the legacy raw-clock-primary behavior.
    bool tipGateEnabled = true;
    double tipGateCapMs = 100.0;
    // Per-shot-type learned firstMeterSeen->release time (ms) — the meter-appear-anchored clock.
    // Learned alongside shotTypeFeedforwardMs so the anchor toggle is a clean flip.
    QMap<QString, double> shotTypeMeterToReleaseMs;
    double meterClockMinMs = 80.0;   // ignore implausibly short meter-appear->release times
    // Clamp on the NET per-type offset (shot_type_offsets + learned) applied to BOTH the vision
    // lead (effectiveLatency) and the feedforward clock (meterThresh/holdThresh). Bounds an
    // over-dialed / stale slider (live: Right Fade sat at the +/-250 UI rail) so it can't push the
    // release outside a plausible window around the learned clock. With the wind-up-invariant
    // meter-appear anchor a small trim suffices, so the effect is bounded to +/-offsetCapMs.
    double offsetCapMs = 150.0;
    // === HYBRID GLOBAL PHASE CLOCK (settings autonomous_vision) ========================
    // ONE global meter-appear->tip clock + ONE slow global latency correction, shared across ALL
    // shot types (the type-invariant rise), with live vision applying only a small BOUNDED phase
    // nudge. Replaces the 11 per-type clocks/offsets when autonomousVision is true. The per-type
    // path is the fallback; this must beat it offline AND live before it becomes the default.
    // Default OFF: a default-constructed engine must NOT silently run the experimental global
    // clock. applyConfig overwrites this from settings.json in production, so only tests /
    // default-constructed engines see the default (T3: the live value is settings-driven).
    bool autonomousVision = false;
    double globalAppearToTipMs = 0.0;     // learned firstMeterSeen->tip (0 = unseeded -> seeds clean)
    double globalHoldToReleaseMs = 0.0;   // hold-start->release fallback (invisible/contested meter)
    double learnedLatencyMs = 0.0;        // ONE global latency+center-bias correction, subtracted from lead
    // Per-shot-type latency residual (autonomous vision): augments the global learnedLatencyMs
    // with a type-specific correction. Different shot types have different meter rise profiles
    // (Standstill fills fast, Fade fills slow), so a single global correction under-corrects
    // slow types and over-corrects fast ones. This map is dialed by the post-release grader
    // alongside the global, with the same slow gain + clamp. The effective latency is:
    //   learnedLatencyMs + shotTypeLatencyMs[type]
    QMap<QString, double> shotTypeLatencyMs;
    double shotTypeLatencyGain = 0.08;    // per-type EMA gain (slower than global — less data per type)
    double shotTypeLatencyClampMs = 50.0; // per-type correction bound (smaller than global — residual only)
    double globalRiseVelocityPctMs = 0.226;  // type-invariant rise rate (measured); the shadow model's vg
    // SHADOW MODE (settings autonomous_vision_shadow): the engine COMPUTES the global-velocity
    // phase-aligned autonomous release deadline each shot and LOGS it on "Shadow timing:" WITHOUT
    // controlling output, so it can be A/B'd against the actual release + verdict before it ever
    // takes control. Independent of autonomousVision (which would actually flip control). Default OFF.
    bool autonomousVisionShadow = false;
    // Slow, bounded, clean-only learners (never trained by fallback/timeout/contested releases).
    double globalLatencyGain = 0.10;      // EMA gain for learnedLatencyMs (slow on purpose)
    double globalLatencyClampMs = 100.0;  // |learnedLatencyMs| bound (tightened from 130)
    double globalClockGain = 0.12;        // EMA gain — lowered from 0.15 for slower, more stable convergence
    // Live vision applies at most this absolute nudge (ms) to the global clock fire — green-confirmed
    // near the top only. NOT 110ms extrapolation, NOT release authority.
    double visionNudgeClampMs = 18.0;  // tightened from 25 — less noise authority
    // Velocity-fit plateau de-weight: samples at/above this fill are dropped from the live velocity
    // estimate (the meter caps/flattens at ~98-100% where green sits; including them collapses the
    // slope and corrupts any extrapolation). Diagnostic/secondary in the hybrid (the clock leads),
    // but keeps the bounded vision nudge honest.
    double velocityPlateauKneePct = 92.0;
    // === Tip-timing improvement flags (2026-07 W-series) ==============================
    // Each defaults OFF so a default-constructed / stale-settings engine keeps CURRENT behavior; every
    // one is A/B-able via settings.json OR the named env var (applyConfig ORs them, like autonomousVision).
    //
    // [ORION_MEASURED_LEAD] Use the sidecar's live-measured release-path latency (measured_latency_ms;
    // ~70ms true round-trip) as the lead BASE instead of the under-compensating learnedLatencyMs(+residual).
    // The engine models only ~13ms today -> it fires ~50ms LATE; the measured value fires on the tip. The
    // frame-age staleness term is still added. It engages only for a fresh, independently
    // converged estimator epoch, or one explicitly controlled bounded warm-start label; an older
    // sidecar that omits the provenance bit stays on the stricter N>=6 path.
    bool measuredLeadEnabled = false;
    // Receipt-clock lease for the measured-latency snapshot. The sidecar republishes the
    // estimator on every detector payload once it exists, so an older value means the
    // telemetry/source path stalled and must not retain release authority indefinitely.
    double measuredLeadFreshnessMs = 250.0;
    // [ORION_TICK_LOCK] Phase-align the ABSOLUTE fire deadline to the 60Hz console sample tick so the
    // release ARRIVES mid-tick (maximally far from the ±tick/2 quantization edges) instead of scattering
    // 0-16.7ms on the grid. Uses the already-surfaced next-tick eta telemetry; see tickAlignedFireDeadlineMs.
    // Applied at the arm site (OrionAppController) — telemetry-only until now.
    bool tickLockEnabled = false;
    double consoleTickIntervalMs = 1000.0 / 60.0;   // 60Hz console sample tick (~16.67ms)
    // [ORION_REG_FUSION] Fuse the sidecar's registration time-to-TIP (reg_tip_ms; flat/accurate at the FAR
    // horizon where the quadratic sampler blows up) for the far horizon (commit >= regFusionFarHorizonMs out,
    // OR fill below regFusionNearFillPct), keeping the live sampler for the near-tip window. Mirrors the
    // offline FUSION handoff. Gated on reg_conf >= regFusionMinConf.
    bool regFusionEnabled = false;
    double regFusionMinConf = 0.5;
    double regFusionFarHorizonMs = 200.0;   // registration owns the >= 200ms-out horizon
    double regFusionNearFillPct = 55.0;     // ...and the low-fill (early) horizon
    // [ORION_LEAD_VISION_GATE] Gate the autonomous global-lead learner (learnFromOutcome) on FIRE-TIME
    // vision confidence (vision-timed release code + fresh vision + lock quality) instead of only the
    // post-hoc grade streaks, so a blind/low-confidence outcome cannot move the lead. Hardens the learner
    // beyond the divergence guard.
    bool leadLearnerVisionGate = false;
    double leadLearnVisionConfGate = 0.5;   // min fire-time detector confidence to trust the outcome
    // [ORION_BLIND_SUPPRESS] RC-3/C3 legacy defense-in-depth diagnostic. The central release-authority
    // gate now rejects every non-genuine current frame before a feedforward fire can reach this check.
    // Keep the settings for telemetry/config compatibility; the env var force-enables the diagnostic.
    bool blindFireSuppressEnabled = false;
    double blindFireSchedTolMs = 50.0;
    // [ORION_PRIOR_POSTERIOR] C2: replace the hard feedforward-preempt (which makes fast meters fire on the
    // clock and never apply the measured-lead/vision stack) with an inverse-variance BLEND — the clock
    // deadline is the PRIOR (variance = per-type historical spread), the vision crossing-minus-latency is
    // the POSTERIOR (variance grows as detection confidence + freshness fall). The fused fire degrades
    // smoothly to pure clock as vision confidence -> 0 and converges to the vision crossing as its variance
    // -> 0. Default OFF -> the hard feedforward preempt is byte-identical to today.
    bool priorPosteriorBlendEnabled = false;
    double blendClockPriorSdMs = 45.0;   // prior (clock) std-dev when no per-type residual spread is known
    double blendVisionBaseSdMs = 20.0;   // vision posterior std-dev at full confidence + fresh meter
    double blendVisionMaxSdMs = 400.0;   // vision posterior std-dev as confidence/freshness -> 0 (~pure clock)
    // === Ceiling stack (2026-07 perfect-green build). Every flag defaults OFF -> the engine is
    // === byte-identical until flipped; each is settings-driven OR forced by its env var. =========
    // [ORION_PLATEAU_AIM] H6: GREEN = MAKE (user-confirmed binary) and the cap-hold PLATEAU
    // (~50ms at fill=100) sits inside the green band, so aim the release at the plateau
    // TIME-CENTER — t(fill=100) + ½·plateauCapHoldMs — instead of the green-center / entry /
    // dead-top estimates. Engages only off a CONFIRMED green tracker (low-σ confirmed-green,
    // never a far-horizon ML-ETA alone): target := the apex (100) and the predicted crossing is
    // shifted +½·plateauCapHoldMs. Fade-class especially (+5-6pp modeled on moving shots).
    // LIVE HINGE (07-15): does releasing during the visual plateau register GREEN in-game?
    bool plateauAimEnabled = false;
    double plateauCapHoldMs = 50.0;      // measured visual cap-hold duration (live-confirm 07-15)
    // [ORION_TEMPLATE_ARRIVAL] H4: the shot animation is a deterministic replay when measured as
    // TIME-AT-FILL crossings (not fill-at-time). Match the early crossing-vector to k cluster-mean
    // templates learned online per timing bucket, predict the green-arrival (t@96) from the
    // matched template anchored at the LATEST observed crossing, and BLEND it as a prior with the
    // live sampler crossing (inverse-variance). Offline LOO ~14-16ms σ from the first ~120-160ms
    // vs ~35ms for the reactive parametric fit; decides 130-180ms earlier.
    bool templateArrivalEnabled = false;
    int templateArrivalK = 4;            // max templates per timing bucket (k≈3-4 clusters)
    double templateArrivalSdMs = 16.0;   // template prior σ (offline LOO 14-16ms)
    double templateLiveSdMs = 25.0;      // live sampler crossing σ at the blend point
    int templateMatchMinFills = 4;       // min crossed grid fills before a match may engage
    // [ORION_PRESS_T0] H5: the user's shoot-press timestamp is ALREADY native — the engine reads
    // the physical pad via RawInput and stamps squareHoldStartMs_/holdStartMs on its own clock
    // (nothing crosses the Chiaki fork; verified 2026-07-09). Flag ON additionally aligns the H4
    // template match on the press→first-crossing interval (earlier SELECT + pre-arm) instead of
    // the crossing-shape alone.
    bool pressT0Enabled = false;
    // [ORION_BANDIT_LEAD] legacy/offline experiment settings. Live release-window diagnostics are
    // detector telemetry, not gameplay rewards, so AutomationEngine never explores, applies, or
    // teaches this tuner. The class remains available to historical offline/unit experiments only.
    bool banditLeadEnabled = false;
    int banditMinPullsPerArm = 3;
    int banditMaxShots = 25;
    // === Per-type ACQUIRE->LOCK calibration controller =================================
    // The user's "find an offset that's super close, then make only minuscule adjustments"
    // model. Each shot type starts in ACQUIRE (the annealed gain converges the offset+clock
    // fast). After calLockAfterGreens consecutive EXCELLENT (in-green) outcomes it LOCKS: the
    // baseline is frozen and only a micro-trim of at most calLockMaxTrimMs is applied per shot,
    // so a dialed-in type stops hunting around the ~30-40ms window. A run of calUnlockAfterMisses
    // off-target outcomes (a new build / contest change) drops it back to ACQUIRE for a fast
    // recalibrate. Phase persists per type (LearningData::shotTypeCalPhase) so a dialed-in type
    // restarts LOCKED. 0 = Acquire, 1 = Lock.
    QMap<QString, int> shotTypeCalPhase;
    int calLockAfterGreens = 3;
    int calUnlockAfterMisses = 3;
    double calLockMaxTrimMs = 4.0;
    // Divergence guard: stop integrating a per-type clock once its verdict stays RAILED at
    // meterMaxErrorMs with the SAME sign for this many shots in a row without ever reaching green
    // (so a degraded/ambiguous signal can't pin the clock at a clamp). Conservative — a normal
    // ACQUIRE converges (greens, or the error un-rails as it nears green) far sooner than this.
    int calDivergenceGuardShots = 10;
    // Per-shot-type count of HUD-outcome learning updates THIS session. The outcome
    // offset uses an ANNEALING gain — large for the first few shots of a bucket (fast
    // from-scratch calibration) decaying to a small stable gain (no oscillation once the
    // bucket has converged). Seeded high in applyConfig for any type that already has a
    // persisted offset, so a restored/calibrated bucket starts at the stable gain.
    QMap<QString, int> shotTypeLearnCount;
    double outcomeGainInitial = 0.6;
    double outcomeGainStable  = 0.3;
    double learnAnnealTau     = 2.0;

    // Post-release shot-meter calibration. The bot's OWN shot meter (only the user's is ever
    // drawn) FREEZES at the release point next to the player for ~2s and then VANISHES — it does
    // NOT recede (user-confirmed from recordings). So we read that FROZEN release marker after a
    // release and grade it against the green WINDOW:
    //   frozen fill BELOW the green window  -> EARLY -> negative errorMs -> LENGTHEN the clock
    //   settled fill INSIDE green / within deadband        -> EXCELLENT -> ~0       -> HOLD
    //   settled fill ABOVE the green window                 -> LATE (overshoot)     -> SHORTEN
    //   settled BELOW green BUT the peak REACHED green       -> LATE (deflate)       -> SHORTEN
    //   settled BELOW green AND the peak fell short of green -> EARLY                -> LENGTHEN
    // The post-release meter ENCODES the outcome (recording-confirmed 2026-06-07): a made/EXCELLENT
    // shot HOLDS the meter high near green (~92%); a LATE shot rises THROUGH green then DEFLATES to
    // ~52%. The PEAK gate is the discriminator — it separates a deflate (peak reached green -> LATE)
    // from a genuine early shot (peak never reached green -> EARLY), so this does NOT repeat the old
    // ungated recede>=N -> LATE rule that mislabeled shots and railed the lead. The detector reads
    // fill ACCURATELY; the ~52% is a real deflate, not a false-lock. meterRecedeLatePct floors the
    // deflate-LATE step; EXCELLENT is checked FIRST so a held-high made shot never hits the deflate branch.
    double postReleaseWindowMs = 1200.0;
    double meterMsPerPct = 5.5;           // fill% -> clock ms (Arrow2 rises ~0.18%/ms)
    double meterGreenWindowTolPct = 3.0;  // settled fill within this of the DETECTED green window [gs,ge] == on-target (EXCELLENT). Window-RELATIVE, not a center deadband: a contested fade's green window shrinks to a sliver near the top (live ~[98,100]); a wide +/-7% center deadband false-stamped a settled-~7%-below-window fade EXCELLENT -> false lock. Validated vs user labels 2026-06-08.
    double meterMaxErrorMs = 90.0;        // per-read clock-error clamp. Tightened 160->90: the fill->ms
                                          // mapping OVER-estimates the timing error near the top (the meter
                                          // decelerates, so a small time error = a large fill shortfall),
                                          // so a railed 160 over-corrects in BOTH directions and bang-bangs
                                          // EARLY<->LATE. 90 keeps a healthy cold-start step (x annealed gain)
                                          // while damping the oscillation; pairs with the FIXED deflate nudge.
    double meterRecedeLatePct = 12.0;     // deflate-LATE shorten NUDGE in fill% (x meterMsPerPct -> ms). The recede magnitude is UNINFORMATIVE (any overtime snaps the marker to ~the same low rest point), so a deflate applies this FIXED bounded nudge, NOT a recede-scaled step that rails the clock and bang-bangs around green.
    double meterMinConfidence = 0.45;
    int meterMinSettledFrames = 3;        // a FADE meter is only briefly visible before it slides off
    double meterGreenMinStartPct = 70.0;  // green must be a near-top chevron, not a wide junk band
    double meterGreenMaxWidthPct = 35.0;
    double meterMinPeakFillPct = 25.0;    // reject only an idle/empty bar; an EARLY shot (~33%) must still grade
    // Meter-settle gating (fixes false EXCELLENT on MOVING shots — fades/Go-To). The post-release meter
    // is only graded off a SETTLED frozen marker: a run of >= meterMinSettledFrames clean frames whose
    // bbox stays within meterSettleMaxMovePx of the prior frame (the meter stopped sliding = player
    // landed) AND whose fill spans <= meterSettleFillTolPct. Until that exists the capture keeps running
    // (re-polled every meterSettlePollMs) up to postReleaseMaxWindowMs; if it never settles the shot is
    // NOT graded (a moving read can't false-grade). Standstill is static throughout so it grades promptly.
    double postReleaseMaxWindowMs = 3000.0;
    double meterSettleMaxMovePx = 12.0;
    double meterSettleFillTolPct = 8.0;
    double meterSettlePollMs = 150.0;
    // === [ORION_SETTLE_SMOOTH_MOTION 2026-08-11] Grade a SMOOTHLY TRANSLATING settled marker ====
    // The static-bbox rule above is a guard against false EXCELLENT on moving shots, and it works.
    // But it is a proxy: what actually makes a moving read untrustworthy is the box jumping
    // (re-acquisition onto decor / another player), not the box travelling. On a Go-To the player
    // is running, so the marker rides across the screen at near-constant velocity while its FILL is
    // already frozen -- a genuine settle that the per-frame |step| <= meterSettleMaxMovePx test can
    // never see, because every step breaks the run and yields length-1 runs.
    //
    // MEASURED 2026-08-11 (session 29, 8 Go-To shots): 5 consecutive landings came back
    // reason=no_settled_run settled_n=0 with samples_n=180-182 and peak_fill 97-100 -- the meter was
    // seen for the whole window and did fill; only the STATIC test failed. meter_jump on those five
    // was 0.0104-0.0217 vs ~0.0045 on a graded Standstill. Cost is not just the missing grade:
    // an ungraded landing emits no PRESS-TIP observation, so Go-To's learner is starved by exactly
    // the shots it needs (its press-anchored weight is stuck at 34 while Standstill caps at 100).
    //
    // This path is deliberately STRICTER than the static one, so admitting it cannot be a net
    // loosening: a run that used it needs meterSettleMotionMinFrames (more frames) and only half
    // meterSettleFillTolPct (tighter fill agreement). A step qualifies as smooth when it is both
    // bounded in absolute travel (rejects a teleport onto another object) and consistent with the
    // previous step (rejects erratic/jittering boxes). The FIRST step of a run has no previous step
    // to compare against, so it is admitted on the travel bound alone -- that is what lets a
    // continuously-moving meter bootstrap a run at all.
    //
    // DEFAULT OFF: it changes which shots GRADE, and therefore what reaches the learner. Validate
    // offline on real Go-To captures (replay framedump harness) before flipping it.
    bool   meterSettleAllowSmoothMotion = false;
    double meterSettleMaxTravelPx = 48.0;   // per-frame ceiling; beyond this it is not the same marker
    double meterSettleMaxAccelPx = 6.0;     // |step - prevStep| ceiling; half of maxMove, on purpose
    int    meterSettleMotionMinFrames = 6;  // 2x meterMinSettledFrames: motion runs must be longer
    // [ORION_SETTLE_SMOOTH_MOTION fix-2 2026-08-11] The fill-tolerance tail is NOT a freeze test.
    // It is band-membership against the run's LAST fill, which makes it a fill-RATE gate: over
    // meterSettleMotionMinFrames-1 = 5 intervals, any rise up to meterSettleFillTolPct*0.5 / 5 =
    // 0.8 pp/frame threads it while still climbing the whole way (traced counterexample:
    // 90.0 -> 94.9 at 0.7 pp/frame reads as a 6-frame "settle"). A real 2K meter's deceleration
    // knee passes through that band on every shot, so the original claim that the tail separates a
    // settle from a mid-flight read was simply WRONG -- it only rejects a STEEP rise.
    // NET DRIFT is the property actually wanted: a frozen marker ends where it started. Applied to
    // motion runs only, so the static path keeps its existing behaviour byte-for-byte.
    double meterSettleMotionMaxNetDriftPct = 1.5;
    // === [ORION_GRADE_V2] Phase-2 grading v2 (plan A3 + Part-0 M4/M5 gates) ==================
    // Trajectory classification of the post-release meter replaces the settled-fill rules:
    //   Case G  rise -> freeze INSIDE green                     -> EXCELLENT, err 0.
    //   Case R  rise -> freeze BELOW green (peak<=F_stop+1.5pp) -> EARLY with magnitude from the
    //           LOCAL rise slope at the freeze (margin-fixed linear — Part-0 M5 measured it
    //           strictly better than the template inverse), err clamped [-40, 0].
    //   Case D  rise -> cap>=97 -> deflate -> freeze            -> LATE, DIRECTION-ONLY +10ms
    //           (Part-0 M4 PROVED F_stop snaps to a per-type rest constant — never a magnitude).
    //   Dead zone err in (-10,+6)ms (and a <=1.5pp peak-to-stop at the cap): grade reported,
    //   NO learning update. Under the flag grading v2 is the SOLE writer of
    //   shotTypeLearnedOffsetMs (offset += 0.25*clamp(err,±40), drift bounded ±120ms/bucket vs
    //   session start); the legacy learnFromOutcome/ACQUIRE->LOCK controller is gated off.
    // Default OFF = the user gate (closed loop only after offline framedump validation).
    bool gradeV2Enabled = false;
    double gradeV2Gain = 0.25;
    double gradeV2ErrClampMs = 40.0;
    double gradeV2DriftBoundMs = 120.0;
    double gradeV2DeadEarlyMs = -10.0;      // dead zone (open interval) lower edge
    double gradeV2DeadLateMs = 6.0;         //  ... upper edge
    double gradeV2DirOnlyLateMs = 10.0;     // Case D direction-only step
    double gradeV2DirOnlyEarlyMs = -10.0;   // Case R fallback when the local fit is unusable

    // === Network sample-and-hold + delta compensation (wifi robustness) ===============
    // The network offset is SAMPLED at shot start and HELD for the whole shot (a mid-hold
    // RTT spike must never move an already-computed deadline). On top of the Kalman
    // outlier gate, the per-shot offset is clamped to within rttDeltaClampMs of the
    // baseline captured when the type LOCKED (shotTypeRttBaselineMs, persisted) — the
    // locked clock was calibrated against that baseline, so only bounded drift is
    // followed without relearning; a spike beyond the clamp is treated as transient.
    // Wifi mode engages automatically when the measured jitter EMA exceeds
    // wifiJitterThresholdMs: a tighter delta clamp and a stricter vision-freshness gate
    // (meterFreshWindowMs * wifiFreshnessFactor) so stale frames can't drive a
    // predictive release. No user toggle.
    double rttDeltaClampMs = 10.0;
    double wifiRttDeltaClampMs = 5.0;
    double wifiJitterThresholdMs = 4.0;
    double wifiFreshnessFactor = 0.5;  // tightened from 0.6 — stricter freshness on wifi
    QMap<QString, double> shotTypeRttBaselineMs;

    // Banner grader auto-fallback: when bannerCalibration is on but the banner reader
    // delivers NO verdict (UNCLEAR / not running) for this many consecutive graded
    // releases, the engine falls back to applying the stored meter self-grade at the
    // pairing deadline so calibration never stalls. Any banner verdict resets the streak
    // and restores banner authority.
    int bannerUnclearFallbackShots = 3;

    // === Sub-tick release scheduler ====================================================
    // The engine runs on a 4ms tick, so a deadline-based release fires 0-4ms late on the
    // grid. When a release deadline lands within schedulerHorizonMs of now, the engine
    // exposes it (scheduledFireDeadlineMs) instead of waiting; the controller's precise
    // fire thread submits the release output at the exact deadline and confirms back via
    // confirmScheduledFire. If no confirm arrives within schedulerGraceMs past the
    // deadline (no virtual pad / scheduler unavailable), the engine fires in-tick as before.
    bool subTickScheduler = true;
    double schedulerHorizonMs = 6.0;
    double schedulerGraceMs = 8.0;

    // === [ORION_FUSED_FIRE] Phase-1 fused t_tip posterior (plan async-whistling-pelican) =====
    // ONE per-shot Gaussian posterior over the tip time, fused per fresh sample from the
    // per-type appear->tip clock prior (anchor), the registration fit, and the near-tip
    // sampler crossing — inverse-variance with chi^2 gating. Fire at
    //   t_fire = mu_tip - delta_aim - lead,  delta_aim = clamp(aimWFrac*W + aimSigmaK*sigma_land,
    //                                                          aimMin, aimMaxWFrac*W)
    // aimed EARLY of the tip inside the measured green band (asymmetric loss: past-tip = miss).
    // fusedFireEnabled = fire AUTHORITY (new ladder: fused primary; feedforward demoted to
    // backstop; T4 tip-gate / prior-posterior blend / consensus / ML-ETA rungs skipped — their
    // jobs live inside the fusion). Default OFF => byte-identical ladder.
    // fusedShadowEnabled = compute + log-only ("FusedShadow:" per shot), safe always-on.
    bool fusedFireEnabled = false;
    bool fusedShadowEnabled = true;
    double fusedSigma0Ms = 45.0;            // anchor sd until per-type MAD is learned
    double fusedProcessNoiseMs = 5.0;       // q per NEW-information sample (raw fill stepped)
    double fusedSigmaFloorMs = 8.0;         // sigma floor inside delta_aim (correlation control)
    double fusedPeakInflateMs = 15.0;       // var inflation at rise->peak regime change
    double fusedSamplerMaxHorizonMs = 250.0;// sampler ignored beyond this horizon (quad blowup)
    int    fusedSamplerMinN = 4;
    double fusedRegMinConf = 0.35;          // reg hard-gated OUT below this (drop, don't clamp)
    double fusedRegSigmaBaseMs = 45.0;      // Part-0 M2 MEASURED far-horizon error (recalibrate on
    double fusedRegSigmaSlopeMsPerMs = 0.05;//  60fps current-reader sessions before tightening)
    double fusedRegRmseSigmaMsPerPp = 6.0;  // + per pp of unweighted fit residual
    double fusedAppearToTipSeedMs = 380.0;  // Part-0 M2: median appear->tip 388ms (not 261 = appear->releaseCmd)
    double fusedWTimeDefaultMs = 30.0;      // green-band time width seed (M3: SS ~12pp, moving ~2pp)
    double fusedAimWFrac = 0.35;
    double fusedAimSigmaK = 0.8;
    double fusedAimMinMs = 10.0;
    double fusedAimMaxWFrac = 0.6;
    double fusedConvergedSigmaMs = 30.0;    // may schedule once sigma <= this
    double fusedBackstopSigmaMs = 45.0;     // FF backstop takes over above this at its due time
    // Autonomous live-tip authority rejects grossly uncertain predictions instead of presenting
    // them as precise. Includes predictor, route-latency, and console-tick uncertainty.
    // Combined sigma ceiling for tip prediction validity. With the factory prior's
    // lead_sd=28.7 and tick_sd=4.8, a 65ms cap left only 58.1ms of predictor sigma
    // budget — razor-thin, causing validity to flap on every frame and killing
    // promoted tokens. 75ms gives a 69ms predictor budget: comfortable for the fused
    // predictor (~45-55ms near tip) while still rejecting genuinely bad predictions.
    double autonomousTipMaxCombinedSigmaMs = 75.0;
    // Lower bound on the actuation lead, in ms. 0 = disabled (the measured authority is used
    // verbatim, the historical behaviour). Set via settings or ORION_LEAD_FLOOR_MS.
    //
    // WHY THIS EXISTS (2026-08-03): the sidecar latency label is biased SHORT, so the posterior
    // walks the lead DOWN and re-introduces late releases. latency_estimator.py:1484-1489 fits
    // time-vs-fill over the RISING tail and extrapolates to f_stop, i.e. it labels the instant the
    // meter FIRST crossed the settle value going UP. But the meter overshoots to ~97-100% and only
    // then recedes to f_stop, so the shot locks LATER than that upward crossing and the label reads
    // short by roughly the rise-to-peak time (~50ms measured). Live evidence, graded off the game's
    // own TIMING banner: at lead 300 shots read EXCELLENT; the estimator then walked the lead to
    // 266/245 and LATE returned. This floor is a GUARD, not the root fix -- it stops the posterior
    // destroying a known-good lead while the label definition is corrected against the banner.
    //
    // Deliberately does NOT manufacture a lead: it only raises an ALREADY-VALID authority, so a
    // shot with no measured authority stays unschedulable and fail-closed exactly as before.
    double autonomousLeadFloorMs = 0.0;

    // Systematic correction added to the measured latency authority, in ms.
    //
    // The label pipeline under-reports latency by a STRUCTURAL amount, not a random one.
    // latency_estimator.py fits the RISING tail and extrapolates to f_stop, so it timestamps the
    // instant the meter FIRST crossed the settle value going UP -- but the meter overshoots to
    // ~97-100% and only then recedes to f_stop, so the shot actually locks later. The size of that
    // gap is set by 2K's meter overshoot-and-settle animation, which is a property of the GAME,
    // not of any particular capture card, PC, or network.
    //
    // That is what makes this portable where a hard-pinned lead is not. Each install's estimator
    // converges to ITS OWN latency; adding the same method-level correction lands every install on
    // its own correct lead. Measured here 2026-08-03: the estimator converged to fixed_ms=227.6
    // (n=7, sigma 5.5) while the banner-verified good lead was 300 -> bias ~72ms. Cross-check: the
    // independently built factory prior reads 218.5, i.e. within ~9ms of the learned value, so both
    // paths share the same short bias and the same correction fixes both.
    //
    // DEFAULT 0 (off) -- deliberately NOT defaulted on. Defaulting it to 72 broke 8 engine tests,
    // and the informative ones were behavioural rather than cosmetic: scenarios with a genuinely
    // SHORT latency (~70ms) became unschedulable, because +72ms pushed the deadline past the
    // meter window entirely (live_tip_deadline_missed, no release). An additive constant tuned in
    // a ~230ms-latency regime is not safe to impose on a low-latency install sight unseen.
    //
    // So this ships OFF and is opted into per rig via ORION_LEAD_BIAS_MS while the bias is
    // validated on a SECOND machine. The permanent, genuinely portable fix is upstream: correct
    // the label in latency_estimator.py to timestamp the shot LOCK rather than the meter's first
    // upward crossing of f_stop, after which no engine-side correction is needed at all.
    double autonomousLeadBiasMs = 0.0;

    // === [ORION_USER_LEAD] the shipped, user-tuned actuation lead ==============================
    // Mirrors AppConfigData::actuationLeadMs (see there for the range derivation). When set it IS
    // the actuation lead: it REPLACES the measured authority's magnitude rather than nudging it,
    // because the whole point is that the user's banner-read value is deterministic and portable
    // while the label-derived posterior drifts. 0 / out-of-band = not configured -> the existing
    // authority + bias + floor path runs verbatim.
    //
    // FAIL-CLOSED IS UNCHANGED. This is applied strictly INSIDE measuredLeadForActuationMs()'s
    // "authority exists" branch: a shot with no valid measured authority still returns 0 and stays
    // unschedulable. This setting can only re-scale an already-valid lead; it can never
    // manufacture authority.
    double userActuationLeadMs = 0.0;
    bool userActuationLeadSet = false;   // the USER moved it (blocks the measured auto-seed)
    // === [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] the delay-condition lead offset ===========
    // Mirrors AppConfigData::meterDelayLeadOffsetMs. Added to the consumed actuation lead ONLY
    // while an applied meter delay is in force (meterDelayAppliedMs_ > 0). 0 (the default) keeps
    // every path byte-identical to the pre-keying engine, at every delay value.
    //
    // WHY THIS HAS TO EXIST: MeterDelayController.h's SAFETY note already states the contract —
    // "The effective actuation lead can legitimately differ between delay-on and delay-off, so
    // latency posteriors learned in one condition must not be applied blind in the other. This
    // class does NOT implement that keying." Nothing else implemented it either: before this
    // field measuredLeadForActuationMs() returned the operator's single Shot Lead verbatim in
    // BOTH conditions, so the moment the delay engaged the engine kept firing with the delay-0
    // calibration and was mistimed with no diagnostic of any kind (see the uncalibrated warning
    // in AutomationEngine.cpp for the 2026-08-09 evidence).
    //
    // ONE OFFSET, NOT A CURVE. The 2026-08-09 corpus does not contain enough good delay-on
    // landings to fit lead-vs-delay (one graded landing in the 95+ band out of 60 delay-on
    // landings), so this deliberately does NOT try to derive the offset from the applied delay.
    // It is an operator control keyed to "a delay is applied", nothing more.
    double meterDelayLeadOffsetMs = 0.0;
    // ORION_LEAD_FLOOR_MS / ORION_LEAD_BIAS_MS were present and in-envelope on this apply. The dev
    // override deliberately BEATS the user setting so a live sweep keeps behaving exactly as it
    // does today; without this flag the setting (which replaces the lead outright) would silently
    // out-rank the floor the sweep is trying to install.
    bool leadOverrideFromEnv = false;
    // Landings required before the measured median is offered as a seed. sd of the per-landing
    // lead measured 17.2ms on the reference rig, so the standard error of the median at n=12 is
    // ~5ms — well inside the ~30ms green window (fusedWTimeDefaultMs) and reached in about two
    // minutes of play. n=5 would be twice as noisy and let one bad landing carry 20% of the
    // answer; n=25 is a better estimate the user has to wait far too long for a starting point.
    int actuationLeadSeedMinSamples = 12;
    // Sanity band for BOTH the accepted setting and each landing-derived sample. Mirrors
    // AppConfigData::kActuationLeadMin/MaxMs; a sample outside it is a bad read, not a lead.
    double actuationLeadMinMs = 150.0;
    double actuationLeadMaxMs = 800.0;
    // === [ORION_GREEN_CENTER] aim at the MIDDLE of the green window, not its upper edge ========
    // MEASURED 2026-08-05, 130 consecutive live releases: shot_.targetPct was 100.00 on every
    // one, greenEndPct was 100.00 on every one, and the aim's position within the window was
    // 1.000 at p10, p50 AND p90. The bot aims at the exact LATE boundary of the window on every
    // shot, with no variance.
    //
    // That caps the green rate at ~50% before precision enters the argument: a symmetric release
    // error centred on the upper edge puts half its mass past the edge, which is LATE by
    // definition. It is not a predictor problem -- a PERFECT predictor aimed at the edge still
    // returns 50%. The measured window is 8.9pp median (~48ms at the observed 0.185pp/ms), so
    // centring converts the SAME sigma into a two-sided margin:
    //     sigma 12ms, aim at edge   -> ~50%
    //     sigma 12ms, aim at centre -> ~96%
    //
    // Expressed as a fraction of the half-window so it can be swept: 0.0 = today's edge aim
    // (byte-identical, this whole path is skipped), 1.0 = the exact centre. The offset is applied
    // as a TIME shift on the fire deadline rather than by re-targeting predictCrossing(), so no
    // predictor's fit, sigma or fusion weight moves -- only when the command is issued.
    //
    // DEFAULT 0 (off). Window width is NOT constant: p10 2.0pp (~11ms) vs p90 10.3pp (~56ms), so
    // a shift that centres a wide window can push a narrow one out the EARLY side. The offset is
    // therefore derived per shot from THIS shot's own tracked window and slope, and clamped, and
    // it stays opt-in until a counted-green batch says it wins.
    double autonomousGreenCenterFrac = 0.0;
    // Hard ceiling on the shift regardless of what the window/slope claim. A bogus greenStart or
    // a near-zero slope must not be able to walk the deadline arbitrarily early; past this the
    // offset saturates rather than growing.
    double autonomousGreenCenterMaxMs = 40.0;
    double fusedSchedulerHorizonMs = 30.0;  // wider commit horizon for the fused deadline
    double fusedCommitLockMs = 22.0;        // inside this the scheduled fire is final
    double fusedRescheduleMinDeltaMs = 1.5; // don't churn the fire thread below this
    // === [Phase-2 A2(c)] earlier-only console-tick snap on the FUSED fire deadline ==========
    // The game samples input at 60Hz: a press landing mid-tick WAITS, adding uniform 0-16.7ms.
    // With a probe-run tick-phase lock (tick_phase_conf >= min AND sd <= max) the fused tFire
    // snaps EARLIER-ONLY to the latest instant whose press arrives epsilon before an input-tick
    // edge — never later than the unsnapped deadline (the legacy tickAlignedFireDeadlineMs
    // centers mid-tick and can move +8.3ms LATE; it stays untouched for the legacy path).
    // Self-gating on phase-telemetry presence: no tick_phase fields -> byte-identical, no flag.
    double fusedTickSnapEpsilonMs = 5.5;    // press arrives this early of the edge (P(slip)<=1.3%)
    double fusedTickSnapMinConf = 0.5;      // sidecar tick_phase_conf engage floor
    double fusedTickSnapMaxSdMs = 1.2;      // sidecar tick_phase_sd engage ceiling
    double fusedTickSigmaSnappedMs = 1.8;   // sigma_tick in sigma_land while snapped (else 4.8)
    // [ORION_MEASURED_LEAD] one-shot clock re-baseline latch (mirrors LearningData; see
    // rebaselineLeadClocks): the EMA-dialed clocks absorbed the OLD ~6ms lead — flipping the
    // base to the measured ~75ms must shift them once by the delta or every path fires early.
    bool leadRebaselined = false;
    QMap<QString, double> shotTypeAppearToTipMs; // per-bucket anchor clocks (session-learned from
    QMap<QString, double> shotTypeWTimeMs;       //  posthoc labels / green-band widths; v4 fields)

    // === [ORION_PROBE] warmup pump-fake latency probes (plan A2.b, user: warmup ONLY) ========
    // A probe = a virtual Square press held probePressMs then released (a pump fake): the game
    // spawns the meter on the press, the sidecar back-extrapolates the meter-appear instant
    // through the registration template, and (press -> appear) - probeSpawnOffsetMs feeds the
    // SAME L_fixed posterior as the passive oracle at sigma ~8ms — converged before shot #1.
    // Presses are staggered probeStaggerMs apart on the 16.7ms console tick so one run also
    // yields the absolute input-tick phase (Phase 2 consumes it). User-triggered (warmup only);
    // any physical Square press cancels the run instantly.
    double probeSpawnOffsetMs = 0.0;   // press->meter-appear game constant; 0 = uncalibrated
                                       // (sidecar then only logs probe_raw for offline D_spawn calibration)
    // The probe needs 3 rising samples below 45% fill, so the game must actually RENDER a shot
    // meter -- a flash is not enough. Measured on the owner's setup 2026-08-05: 150ms produced no
    // meter (8/8 expired), and neither did 400ms (8/8 expired, player visibly shot each time)
    // while a manual hold in the same spot produced a meter every time. The discriminator is hold
    // length, so this default mimics a human hold rather than a tap.
    //
    // Overridable from settings.json via probe_press_ms, because the threshold is a property of
    // the game and the player's build -- not of this code -- and each guess otherwise costs a
    // rebuild plus a relaunch. Stays inside the estimator's 2000ms probe expiry and the 2500ms
    // inter-probe gap.
    double probePressMs = 900.0;
    // Gap between probe presses. 2500 assumed the player simply stands there holding a ball, which
    // is false in every venue where possession is not persistent: measured 2026-08-05, the owner
    // has to press X to call for the ball after each shot, so on a 2.5s metronome most presses
    // fired at an empty-handed player, produced no shot, and expired with rise_samples=0. The
    // probe was timing a ball that was not there.
    //
    // 6000 leaves room for the shot animation, the rebound, and the call-for-ball round trip.
    // Cross/X does NOT cancel a run (only physical Square does), so the user can re-acquire the
    // ball freely between presses. Overridable from settings.json via probe_gap_ms.
    double probeGapMs = 6000.0;
    // [ORION_PROBE] Call for the ball before each probe, instead of requiring the operator to do
    // it by hand between presses.
    //
    // A probe measures the meter that follows a Square press, so an empty-handed press measures
    // nothing. In venues without persistent possession the ball has to be re-acquired every shot,
    // and a fixed-cadence probe run cannot know whether it arrived: measured 2026-08-05 at a 6s
    // gap, 1 of 8 presses caught the ball. Pressing Cross (PS_CROSS via XINPUT_GAMEPAD_A, see
    // OrionInputClient.cpp) makes the run self-sufficient and removes the operator from the loop.
    //
    // The stagger still lands on the SHOOT press, not the call, so the run continues to sweep the
    // console input tick exactly as before.
    double probeCallForBallMs = 120.0;   // how long Cross is held (a tap, not a hold)
    double probeCallLeadMs = 2000.0;     // call -> shoot; must cover the pass travelling to you
    double probeStaggerMs = 1000.0 / 60.0 / 8.0;   // 8 presses sweep the console tick phase

    // === Item 1: Nonlinear meter-top deceleration correction ==========================
    // The meter decelerates near the top (fill caps at ~100%). The ORIGINAL premise was that
    // linear extrapolation over-estimates the time to target, so shortening msUntilTarget above
    // decelKneePct would recover a late release.
    // WRONG SIGN (2026-07-24): deceleration makes the meter take LONGER to cover the remaining
    // gap, so a linear fit under-estimates time-to-tip and is already EARLY-biased — the engine
    // says so itself where it taxes a linear crossing above the knee (`!cf.usedQuad && fillPct >
    // decelKneePct -> sigma *= 2`, AutomationEngine.cpp). Subtracting `gap*f/v` there pushed the
    // release EARLIER on exactly the frames that were already early. Worse, decelKneePct (88)
    // equals tipFireMinFillPct (88), so under autonomous tip-vision the correction fired on
    // EVERY predictive release rather than on a rare tail.
    // Default is now 0.0 (inert): TemporalSampler::predictCrossing's quadratic already models
    // the top-of-meter curvature, and its own chi^2 gate decides when the quadratic is trusted.
    // The knee/factor plumbing is kept so a live experiment can re-enable it (with the correct
    // sign) without a code change.
    double decelKneePct = 88.0;         // start correcting above this fill
    double decelCorrectionFactor = 0.0;  // fraction of remaining gap to subtract (0=off, 1=full)

    // === [ORION_HORIZON_DEBIAS] Horizon-linearisation of the sampler crossing ==========
    // MEASURED (2026-08-04, replay of TemporalSampler over 47 clean live rises from
    // logs/diagnostics/detframes_20260803_*.csv + logs/batch_20260803/detframes_batch.csv,
    // 1142 decision points): the weighted-linear crossing does not scatter about the truth,
    // it runs LATE, and the lateness grows with how far out the prediction is made:
    //
    //     predicted horizon    MEDIAN residual (bias)    rMAD (spread)
    //       100-200ms                 +15.4ms               13.0ms
    //       200-300ms                 +38.7ms               17.7ms
    //       300-400ms                 +68.4ms               20.6ms
    //       400-500ms                 +88.6ms               23.2ms
    //
    // Slope ~ +0.22ms of lateness per ms of horizon. The rMAD column reproduces the
    // 2026-08-03 recalibration's committed numbers (15.9/16.7/22.0) almost exactly, which
    // confirms both analyses measure the same quantity — that recalibration reported only
    // the spread, so the median column above was never acted on. The cause is physical: the
    // meter EASES IN (velocity 0.164 %/ms at fill 20-30 rising to 0.26 %/ms at fill 85-90,
    // +58%), so a straight line fitted mid-rise and extrapolated to the cap always arrives
    // late, and the further it extrapolates the later it arrives.
    //
    // A constant lead can only cancel this at ONE horizon. That was tolerable while commits
    // were pinned near a single horizon; it is not once the authority lease lets tokens arm
    // across a wide horizon range, because then the uncancelled part varies shot to shot.
    //
    // The correction below is deliberately MEAN-NEUTRAL: it subtracts k*(h - lead), pivoting on
    // the engine's OWN measured lead. The command is submitted at fireAt = tip - lead, so at the
    // decision that actually fires the remaining horizon IS the lead and the correction is
    // identically zero — the average aim point, and therefore the validity of the already-learned
    // lead, is unchanged. Only the horizon-dependent spread is removed. The pivot is a live
    // per-machine measurement, never a constant, so this carries no hardware assumption.
    // Setting k = 0, or the flag false, is byte-identical to the pre-2026-08-04 behaviour.
    //
    // DEFAULT OFF, deliberately. The pivot is not yet known: the engine schedules a fire ~43ms
    // BEFORE it executes (see the closed-loop evidence in canonicalAutonomousTipDecision), so
    // the operative horizon is lead + scheduling-lookahead — and that lookahead is precisely
    // what the rolling authority lease just changed. Offline replay cannot measure it because
    // replay has no scheduler. One live batch carrying the new "Release landing:" line pins it;
    // flipping this flag is then a one-line change with the measurement already in place.
    bool   samplerHorizonDebiasEnabled = false;
    double samplerHorizonBiasMsPerMs = 0.22;   // measured slope; 0.0 == feature off
    double samplerHorizonDebiasMaxMs = 45.0;   // clamp |correction| (guards a wild fit)

    // === [ORION_TIP_PHASE] Animation-phase tip LOOKUP (replaces the extrapolation) =====
    //
    // THE MEASUREMENT (2026-08-04, independently re-derived from
    // logs/diagnostics/detframes_20260804_074153.csv + _091503.csv; 89 meter rises, 61 of
    // them carrying a release with fill_at_release >= 40):
    //
    //   time from the meter's first upward crossing of 30% fill to the end of its rise
    //     median 319.8 ms   rMAD 9.6 ms   sd 15.0 ms   p10-p90 301-332
    //   the SHIPPED sampler over the same landings ((crossingEta at submit) - measured stop)
    //     median +92.6 ms   rMAD 18.4 ms  sd 21.5 ms
    //
    // So the lookup is ~1.9x tighter in rMAD. The reason is structural, not a better fit:
    // TemporalSampler EXTRAPOLATES a curve forward to a crossing it has not seen, and the
    // meter eases in, so the projection lands late by an amount that depends on the curve.
    // This predictor extrapolates NOTHING. It dates one event that already happened (the
    // 30% crossing) and adds a constant. That is why it is horizon-FLAT where the sampler
    // is not -- measured at five anchors on the same 61 rises:
    //     anchor 20% -> 380.8 (rMAD 10.3)   35% -> 293.7 (rMAD 11.1)
    //     anchor 25% -> 350.8 (rMAD 12.2)   40% -> 266.8 (rMAD 11.0)
    //     anchor 30% -> 319.8 (rMAD  9.6)   50% -> 214.8 (rMAD 11.8)
    // The median tracks the anchor (it is a different point on one curve) but the SPREAD
    // does not move. A predictor whose error grew with horizon could not do that.
    //
    // WHY THIS IS NOT ANOTHER CIRCULAR METRIC (four have already died on this project).
    // The constant differences two DETECTOR-CLOCK frame times: the frame where fill first
    // crossed 30%, and the frame where the rise ended. It never reads lead_ms, tip_eta_ms,
    // command_eta_ms, peak_fill or fill_at_release, so the 300 ms lead knob has no path to
    // it. That the meter's STOP is animation-set rather than command-set at fill>=40 is
    // separately measurable, and is: regressing plateau fill on fill_at_release gives slope
    // +0.013 +-0.247 for fill>=40 (flat -- the animation decides) against +1.607 +-1.605
    // for fill<40 (the command decides). Honest residual: regressing the CONSTANT itself on
    // fill_at_release leaves +1.32 +-1.12 ms/pp, i.e. ~5 ms across the whole observed
    // 40-54 pp range. Not identically zero, but an order of magnitude below the +92.6 ms
    // sampler bias it replaces, and it is never exercised while the aim is held fixed (see
    // tipPhaseSeedPhysicalMs). Extrapolating this predictor far outside the observed
    // release band is NOT supported by the measurement.
    bool   tipPhaseEnabled = true;
    // Fill (%) whose first upward crossing dates the shot. 30 is the measured optimum but
    // the spread is flat from 20-50, so this is a safe knob.
    double tipPhaseAnchorPct = 30.0;
    // [ORION_TIP_PHASE_LADDER] FALLBACK anchors for a meter first seen ABOVE tipPhaseAnchorPct.
    //
    // THE PROBLEM, measured across the 2026-08-04 batches. Grouping every shot by the fill at
    // which its reservation was promoted gives a clean, non-overlapping split:
    //
    //     Left Fade   n=6   median 14.1   range 11.9-15.9
    //     Right Fade  n=3   median 19.8   range 18.5-21.5
    //     Standstill  n=15  median 21.0   range 15.9-27.8
    //     Go-To       n=6   median 22.2   range 13.8-24.7
    //     No Dip      n=3   median 35.1   range 32.4-37.3   <-- ENTIRELY above the 30 anchor
    //
    // No Dip's meter is never observed below 30, so its 30% crossing has already happened by the
    // time the engine sees it, no straddling pair can exist, and notePhaseAnchorSample correctly
    // records nothing. The shot then falls back to the registration/sampler chain carrying the
    // +61-92 ms bias the phase member exists to replace. Measured consequence: of shots that
    // reached a release, phase drove 20/21 Standstill, 14/15 Left Fade, 7/7 Right Fade and 9/9
    // Go-To -- but only 2 of 4 No Dip.
    //
    // THE FIX IS STILL AN OBSERVED CROSSING, NOT AN INFERRED ONE. A meter first seen at 35 has
    // genuinely not crossed 40 yet, so the 40 crossing can be witnessed with a real straddling
    // pair under exactly the same rules. Nothing is guessed and the fail-closed direction is
    // unchanged: a shot that straddles no level in the ladder still records no anchor.
    //
    // The LOWEST straddled level wins, so a meter first seen below 30 anchors at 30 exactly as
    // before -- every non-No-Dip shot is byte-identical. Only a meter that MISSED 30 falls
    // through to 35, then 40.
    double tipPhaseAnchorLadderStepPct = 5.0;
    int    tipPhaseAnchorLadderCount = 2;      // -> {35, 40}; 0 disables the ladder entirely
    // How the constant shrinks per pp of extra anchor. A later anchor dates the shot later in
    // the same animation, so less of it remains.
    //
    // MEASURED TWICE, on two independent sessions, by the detector's own rise labels:
    //           anchor 25      30      35      40     slope(30->40)
    //   batch 4     417.4   388.4   362.8   336.5     -5.19 ms/pp   (n=36)
    //   batch 5     422.1   391.0   366.6   338.2     -5.28 ms/pp   (n=22)
    // The two sessions agree to 0.1 ms/pp on the slope and to <=2.6 ms on every absolute level.
    // The stop criterion used offline sits ~69 ms later than the engine's, but that offset is
    // common to both endpoints of a difference, so it cancels out of the slope exactly.
    double tipPhaseConstantSlopeMsPerPct = -5.235;   // mean of the two measured slopes
    // [ORION_STOP_CORROBORATE] Require a SECOND accepted sample before a stop re-open is
    // committed (settings stop_reopen_corroborate, default OFF). Measurement/learning only —
    // it can never fire, schedule, or move an armed token. See the long note at the use site:
    // three single-frame spikes in the 2026-08-06 counted batch were learned as 409-446ms
    // animations and biased the aim late.
    bool stopReopenCorroborate = false;
    // [ORION_STOP_SUBFRAME] Date the end-of-rise stop SUB-FRAME (settings stop_dating_subframe,
    // default OFF). The snapped stop takes the wall time of the capture sample that tripped the
    // step threshold, so where the freeze truly ended within the surrounding frame interval is
    // discarded — split-half analysis measured that dating noise at ~6x the interpolated anchor
    // crossing's, and it feeds the phase learner, which sets the aim. When ON, the measurement
    // (recordPhaseConstantSample only — never scheduling/fire) re-dates the stop at the
    // intersection of a line fit through the last rising samples with the settled plateau level,
    // clamped to the observed straddle [last-below-plateau sample, first-at-plateau sample], then
    // RE-CENTRED by kStopSubframeRecenterMs so the learned quantity stays commensurable with the
    // seed/constant calibrated under frame-snapped dating (i.e. the flag removes variance, not the
    // mean — flipping it must not re-aim the bot). Falls back to the snapped date whenever the fit
    // is not defensible (too few rising samples, implausible slope, no plateau).
    bool stopDatingSubframe = false;
    // [ORION_PHASE_VETO_DIRECTIONAL] Make the live-meter corroboration veto one-directional
    // (settings phase_veto_directional, default OFF): keep it when the sampler says the crossing
    // is LATER than the phase tip (the late hazard it was written for), drop it when the sampler
    // says EARLIER (its own measured +61-92ms bias direction). See the long note at the use site:
    // this one line accounts for 16 measured no-fire aborts AND 3 counted earlies.
    bool phaseVetoDirectional = false;
    // [ORION_AIM_FREEZE] Hold the learned aim constant still for the session (settings
    // tip_phase_aim_frozen, default OFF). Measured cause of within-session degradation: the aim
    // walked 8.2ms across one 70-release batch while landing sd was 10.5ms. See the use site.
    bool tipPhaseAimFrozen = false;
    // [ORION_ANCHOR_BASE20] Candidate B of the 93ms decision-budget work (settings
    // tip_phase_anchor_base20, default OFF): move the base anchor 30 -> 20, widening the
    // anchor->deadline decision budget by the measured 20->30 animation time (58.3ms,
    // tools/timing/measure_anchor_slope.py, n=21 -- NOT the 52.35ms the shipped slope
    // implies). When ON, applyConfig() moves the whole coupled constellation together
    // (constant, seed, learn band, ladder count) and tipPhaseLevelAdjustmentMs() switches
    // to measured per-rung secant offsets, because the single -5.235 slope mis-dates the
    // 35/40 rungs by ~6-8ms LATE from a base of 20. learning.json stays canonically
    // expressed at base 30 (translated on restore/persist), so flipping this flag in
    // either direction can never corrupt a learned prior.
    bool anchorBase20 = false;
    // === [ORION_USER_LEAD_AUTHORITY] (settings user_lead_satisfies_authority, default OFF) ===============
    // A USER-SET, in-band actuation lead may satisfy measuredLeadAuthoritative() when the
    // estimator posterior cannot.
    //
    // WHY (measured 2026-08-06, sessions 21:52Z/22:23Z, plus the cold-install audit): the fire
    // path — measuredLeadForActuationMs() — RETURNS config_.userActuationLeadMs whenever it is
    // set and in band, discarding the estimator's value outright. Yet the readiness gate still
    // demanded a validated (n>=6, sd<=3.3) or structurally-matching factory posterior: a gate on
    // a quantity the fire path throws away. A cold session with a hand-tuned lead therefore
    // refused every press (waiting_for_latency_calibration) until a manual probe run converged —
    // 8/8 and 7/7 presses benched in the two rehearsal sessions, and a clean customer install
    // cannot run a probe at all (Debug page is compiled out under ORION_PRODUCTION_BUILD).
    //
    // FAIL-CLOSED ARGUMENT (each clause of the old gate, and what happens to it):
    //   KEPT IN FULL — measuredLeadEnabled, clock compatibility, live telemetry present, the
    //     freshness window, and the EXACT controller-route attestation echo (scope epoch +
    //     generation + delivery route). No release can happen with a dead sidecar, a stale
    //     telemetry stream, or an unproven/changed controller route, exactly as before.
    //   KEPT UNTOUCHED — everything downstream: meter ownership proof, tip-decision validity
    //     (sigma caps), frozen-lease arming, absolute max-hold. This flag adds no release path;
    //     it only lets the engine ARM with the same lead value every driven shot already uses.
    //   WAIVED — only the estimator-posterior SHAPE requirements (kind/n/sd/prior-source), and
    //     only when the OWNER explicitly set actuation_lead_ms (actuationLeadUserSet) inside
    //     [actuationLeadMinMs, actuationLeadMaxMs]. An unset or out-of-band lead changes nothing.
    //   The env sweep overrides (ORION_LEAD_FLOOR/BIAS_MS) disable this path entirely, because
    //     under them measuredLeadForActuationMs() would NOT return the user value — readiness
    //     must never be granted on a lead the fire path would not actually use.
    // Scheduling sigma: with no posterior there is no lead sd; effectiveLeadSdMs() then reports
    // the 6.0ms factory-envelope floor — the same sd every working factory-prior session
    // (e.g. 2026-08-06 13:55Z, lead_sd_ms=6.000) scheduled with, so combined-sigma gating is
    // bit-identical to the proven cold-start behaviour, not loosened.
    bool userLeadSatisfiesAuthority = false;
    // [ORION_TEMPO_FADE_MIRROR] see AppConfig.h / ShotReleasePolicy.h. Fades run the mirrored
    // Tempo gesture (gather UP, flick DOWN); anything else keeps gather DOWN / flick UP.
    bool tempoFadeMirrorGesture = true;
    // [ORION_PROBE_CACHE_AUTHORITY] (settings probe_cache_prior_authority, default OFF): the
    // factory-prior authority contract additionally accepts prior source
    // "probe_cache:self_measured" — the probe-persisted, DPAPI-bound, route-scope-digest-keyed
    // lead cache restored by latency_estimator._restore_probe_prior() under
    // ORION_PROBE_PRIOR_AUTHORITY. Route safety is NOT weakened: the tuple still has to pass
    // every other factoryPriorStructured clause (factory kind, N<6, sd in [6,100], model
    // version) plus the exact attestation-generation/delivery-route echo, and the cache file
    // itself is selected by a digest of the full route scope (console identity, controller
    // route, device moniker, negotiated mode) so a route change restores nothing at all.
    bool probeCachePriorAuthority = false;
    // [ORION_PROBE_COUNT] Warmup probe-run length (settings latency_probe_count, default 16 =
    // today's behaviour; see AppConfigData::latencyProbeCount for the sizing arithmetic).
    int latencyProbeCount = 24;  // see AppConfig.h — bumped 16 → 24 for reliable convergence
    // [ORION_TIP_PHASE_IMMINENT] How far BELOW the anchor still counts as "the phase member is
    // about to exist", so a non-phase estimate must not release yet.
    //
    // 6.0 pp is ~1-2 frames of rise: the meter climbs 0.18-0.23 %/pp-per-ms, so 6 pp is 26-33 ms
    // at 60 fps. Sized from the live miss it exists to prevent -- a sampler release at fill 29.3
    // when the anchor was ~3 ms away -- and deliberately kept narrow: a meter STALLED well below
    // the anchor never enters the band, so it aborts on the existing timeout exactly as before
    // rather than being held for a crossing that is not coming.
    //
    // Blast radius, measured over the 54 shots of the two batches: exactly ONE release sits in
    // this band. 52 of the other 53 were already phase-driven and cannot reach this branch at all.
    // 0.0 disables the hold.
    double tipPhaseImminentHoldBandPct = 6.0;
    // [ORION_RUNG_IMMINENT] (settings tip_phase_rung_imminent_hold, default OFF) Extend the
    // imminent hold's target from the base anchor alone to the LOWEST LADDER RUNG STRICTLY
    // ABOVE the current fill, using the same band, the same rising-only refusal, and the same
    // bounded hold path.
    //
    // WHY (measured, logs 2026-08-04..06): 27 of 62 rejected_missed aborts were first-tick
    // sampler kills -- reservation_updates=1, source=sampler, no witnessed crossing -- and 19
    // of those sat within ~5pp BELOW a rung, i.e. 1-2 frames from a phase crossing that would
    // have replaced the +61-92ms-early-biased sampler estimate whose tip_eta < lead is what
    // aborted the shot. Under anchorBase20 this class was 4 of the first live batch's 6
    // misses: a meter first seen at ~22 fill is ABOVE the 20 base anchor, so the base-only
    // predicate declines (belowBy < 0) and the sampler kills the shot on its first decision
    // tick -- protection the base-30 regime USED to give that same shot (22 is 8pp below 30).
    //
    // Fail-closed shape is identical to the base hold: every condition is a refusal, the hold
    // creates no token and no command, it stands only while RISING toward a witnessable rung,
    // and it remains under the absolute maxHoldMs ceiling. A fill above the TOP rung has no
    // witnessable crossing left, so it falls through to today's behaviour untouched.
    //
    // KNOWN RISK (why this ships OFF pending its own counted batch): under base20 the rung
    // gaps (5pp) are narrower than the band (6pp), so the corridors are contiguous 14..40pp;
    // a rising meter whose rung crossings repeatedly fail to be witnessed (dropped straddle
    // frames) can be held across several rungs and reach the sampler fallback with less
    // runway than it has today. Bounded by rising-only + maxHoldMs; the alternative today is
    // an instant first-tick abort, but the counted batch must confirm the trade.
    bool tipPhaseRungImminentHold = false;
    // HOW FAR PAST THE ANCHOR the upper sample of the straddling pair may sit and still date
    // the shot. This is the anchor's PROVENANCE gate, and it is the one that was missing.
    //
    // notePhaseAnchorSample already refuses to date a meter first seen above the anchor, on
    // the grounds that a lingering previous-shot meter is a genuine detection that appears
    // high. That guard has a hole: a carryover does not have to be seen only above the anchor
    // to fool it, it only has to be seen ONCE below and once above. A re-lock that reports
    // 28.7% and then 50.4% forms a textbook straddling pair, and the interpolation between
    // them produces an anchor for an animation that never happened.
    //
    // MEASURED, on the 2026-08-04 batch (26 attempts, first live batch with this member).
    // Across the 21 shots whose anchor was dated from a real rise, the fill at the tick the
    // phase member first became primary ran 30.06-33.60 -- i.e. the upper straddle sample sat
    // at most 3.60 pp above the 30.0 anchor, which is exactly one 60 fps frame at the meter's
    // measured 0.205-0.222 %/ms. The single shot that failed (physical_epoch=38, graded EARLY
    // at -90.0 ms, peak_fill 51.36 against a 93.46-100 green) became primary at 50.43 -- 20.43
    // pp past the anchor, five times outside the entire healthy range, on a meter the detector
    // had been reporting parked at ~50% for the 2.2 s since the previous shot aborted. It also
    // took ownership 42.7 ms after the press against 416-1795 ms for every other attempt in the
    // batch, which is the same carryover seen from the other side.
    //
    // 12.0 pp, and the separation is the point: it is 3.3x the largest healthy overshoot and
    // 0.59x the failure. The upper bound is set by dropped frames rather than by fitting the
    // failure -- three consecutive dropped frames (a 50 ms straddle at 0.21 %/ms) is 10.5 pp,
    // so 12.0 still dates an anchor across a detection hiccup that a pp-per-frame rule would
    // throw away. Deliberately NOT expressed as a rate: a rate gate has to be told the frame
    // cadence, and the cadence is exactly what is unreliable in the case being caught.
    //
    // WITHDRAW-ONLY. Failing this records no anchor, which puts the shot in the same state as
    // one whose crossing was never observed: the phase member stands down and the pre-existing
    // registration/sampler chain runs unchanged. It cannot move a deadline, extend one, or
    // manufacture a command. A later CLEAN straddle in the same shot (the meter dips back below
    // the anchor and re-crosses properly) is still dated, so this only ever removes evidence
    // the engine should not have trusted.
    double tipPhaseAnchorMaxOvershootPct = 12.0;
    // TOTAL constant added to the anchor. Deliberately NOT the physical 318/319.8: see
    // tipPhaseSeedPhysicalMs for why, and for the arithmetic that keeps the aim unchanged.
    // 387.0, not 379.0. 379 was the design's arithmetic (318 physical + the audit's +61.0
    // sampler bias), but the MEASURED landing says otherwise: with 379 and a 300 ms lead the
    // command lands at anchor+79.0, while today's shots land at anchor+87.0 -- an 8 ms EARLY
    // shift, which is not the mean-neutrality this change was predicated on. The user is
    // separately seeing the game's own banner read EARLY (median peak_fill 98.17 against a
    // 100 target), so shifting a further 8 ms early compounds the one error we can actually
    // observe. 387 lands the command exactly where it lands today, which is the whole design
    // constraint: move the SPREAD (rMAD 18.4 -> 9.6) and leave the AIM alone.
    // 392.0. This number was walked to live, against the game's own banner, because that is the
    // only instrument here that has ever tracked reality:
    //
    //   387  -> owner reports landing JUST SHORT of the tip, then a later batch reads roughly
    //           balanced ("early early late slightly late"). Slightly early-leaning.
    //   399  -> "mostly lates, a few earlies". Clearly overshot.
    //   392  -> midpoint, deliberately biased to the EARLY side of the late-onset.
    //
    // WHY THE OFFLINE MEASUREMENT DID NOT SETTLE IT. Replaying the detector's own rises put the
    // animation constant at 402.0 ms at the 30% anchor (n=36, rMAD 11.0), and acting on it
    // overshot by a clear margin. The offline "stop" is the first attainment of peak fill, which
    // sits later than the engine's internal stop detector -- so the two numbers are not the same
    // quantity and 402 was never directly comparable to what this constant expresses. The
    // engine's OWN learner corroborates: across the 399 batch it pulled the effective constant
    // down toward ~393, not up toward 402.
    //
    // So the offline number gave the right DIRECTION (the old 387 really was short) and the wrong
    // MAGNITUDE. Direction from measurement, magnitude from the banner.
    //
    // BIASED EARLY ON PURPOSE. green_end is always 100.0, so the green window sits BELOW the tip:
    // an early release still lands inside it while a late one falls outside and the banner caps
    // and deflates. Where the evidence brackets a range, sit on the early edge of it.
    //
    // Raising the CONSTANT while leaving tipPhaseSeedPhysicalMs at 318 is how an aim change is
    // expressed: effectiveTipPhaseConstantMs() = learnedPhysical + (constant - seedPhysical), so
    // the aim offset is 74 and the learner keeps converging on the physical term untouched.
    // 393.0. Read in EFFECTIVE terms (learnedPhysical + aim offset), which is what the bot
    // actually fires on, the owner's live banner walk reads:
    //     385.8 -> "a little worse, more earlies"      (this session, seed mismatch)
    //     393-394.5 -> good                            (the batch we called 392)
    //     ~399 -> "mostly lates"
    // so the target is ~393 effective. With the seed now aligned at 312 the aim offset is 81, and
    // 393 is what BOTH a fresh install and a converged one run.
    // 2026-08-05: 393.0 -> 390.0. The 393 was set from a RECONSTRUCTION of what the well-received
    // batches ran at ("393-394.5 effective"); the PHASE SAMPLE data those batches actually logged
    // says 390.87 (n=103, anchor_pct=30). The reconstruction was wrong and the bot was aimed
    // ~2.8 ms late. Measure the aim from effective_const_ms in PHASE SAMPLE -- never reconstruct
    // it from commit history, and never from learning.json (that is learner STATE, which is how
    // the 318->312 seed error happened).
    //
    // WHY IT ONLY SURFACED NOW: across the same sessions the effective-aim sd fell 4.53 -> 0.97.
    // While the predictor still wandered, a good share of shots landed near 390 by luck and the
    // bias was masked; once the spread tightened, every shot carried the full +2.8 ms. Tightening
    // variance EXPOSES bias -- it does not create it. Expect this whenever spread drops sharply.
    //
    // 2026-08-05 (second correction): 390.0 -> 392.0. The 390 was set when learned_phase_physical
    // sat at 319.56; it then drifted to ~317.9, so the EFFECTIVE aim landed at 388.90 (n=63,
    // measured) -- ~2.0 ms EARLY of the 390.87 target rather than on it. The owner's report
    // matched exactly: all-lates became "a few lates and earlies", i.e. centred but biased early.
    // +2.0 puts effective back on 390.9.
    //
    // The recurring lesson: the constant is only half the aim. Whenever the learner moves, the
    // effective aim moves with it, so ALWAYS re-read learning.json before and after a change --
    // never assume a constant delta produces an equal effective delta.
    //
    // 2026-08-05 (REVERTED to 393.0). Both corrections above chased a ~2ms aim offset against
    // log-derived fill metrics, which is the class of instrument this repo has already refuted
    // three times (settled_fill, peak, f_stop, travel_pp). Neither 390 nor 392 was ever graded on
    // a counted outcome, and the 390 step demonstrably aimed the bot 2.8ms LATE before being
    // corrected again -- two adjustments, no evidence, in opposite directions.
    //
    // The same night's measurement makes the scale plain: shot_.targetPct was 100.00 and the aim
    // sat at position 1.000 of the green window on 130/130 releases, i.e. on its LATE EDGE. That
    // geometry caps the green rate near 50% no matter what this constant is, and the window is
    // ~48ms wide. A 1-2ms argument here is rounding error against a ~24ms mis-aim; chasing it
    // first is how the real one stayed invisible. See RemapConfig::autonomousGreenCenterFrac.
    //
    // Back to the value the tests encode and derive (393 = 319 physical + 74 aim offset). Re-tune
    // it only against counted greens, and only after the window-centring question is settled.
    double tipPhaseConstantMs = 393.0;
    // THE PHYSICAL PART of tipPhaseConstantMs, and the reason the two are separate fields.
    //
    // (tipPhaseConstantMs - tipPhaseSeedPhysicalMs) = 61.0 ms is an AIM-PRESERVATION OFFSET,
    // not physics. It exists so that swapping predictors does not silently re-aim the bot:
    // the shipped sampler runs LATE by ~+61..+92 ms and the installed lead was tuned against
    // that biased predictor, so dropping in an unbiased one would fire the whole session
    // early by exactly that bias. Shipping 318+61 makes the firing decision unchanged by
    // construction and moves only the spread -- which is the entire point of the change.
    //
    // THE LEARNER MUST NOT EAT THIS OFFSET. The running median of (stop - anchor) converges
    // on the PHYSICAL ~320, so a learner that wrote its answer straight into
    // tipPhaseConstantMs would walk the aim 61 ms earlier over its first ten landings -- a
    // large silent re-aim wearing the clothes of a calibration. effectiveTipPhaseConstantMs()
    // therefore learns the physical term only and re-adds this offset unchanged. Zeroing the
    // offset (setting this equal to tipPhaseConstantMs) is the ONE-LINE way to later take the
    // 61 ms out on purpose, together with a lead re-tune, as a decision of its own.
    // 312.0, from 318.0 on 2026-08-05. THE SEED MUST MATCH WHAT THE LEARNER ACTUALLY CONVERGES
    // TO, or the shipped constant stops meaning what it says.
    //
    // effectiveTipPhaseConstantMs() = learnedPhysical + (tipPhaseConstantMs - seedPhysical), so
    // the shipped constant is only the aim WHILE learnedPhysical == seedPhysical. It does not:
    // this rig's learner converged to 311.8 and persisted it, so the session ran at
    // 311.8 + (392 - 318) = 385.8 ms -- about 6 ms below the 392 we had tuned to, and right next
    // to the 387 the owner had already called "just short". The batch duly read "a little worse,
    // more earlies".
    //
    // It was wrong all along; persistence only made it VISIBLE. Before the constant was saved
    // across sessions, learnedPhysical drifted, so "392" was never the same aim twice and the
    // whole 387 -> 399 -> 392 walk was measured against a moving target.
    //
    // Aligning the seed with the converged value makes the two paths agree: a fresh install with
    // no learning.json runs exactly tipPhaseConstantMs, and a converged one runs
    // learnedPhysical + the same offset, which is now the same number. Read the aim in EFFECTIVE
    // terms from here on -- that is the quantity the bot actually fires on.
    // 319.0 -- the MEASURED animation median, n=36 in one session, rMAD 4.8.
    //
    // History worth keeping, because I got this wrong once in each direction. It shipped at 318,
    // which was very nearly right. On 2026-08-05 I moved it to 312 to match a PERSISTED value of
    // 311.8 -- but that persisted number was stale state from an earlier configuration, not a
    // measurement of the animation. Lowering the seed raised the aim offset from 74 to 81, so as
    // the learner correctly converged toward the real ~319 the effective aim WALKED LATE: measured
    // 392.7 at the start of the next session and 402.1 by the end, a +9.4 ms drift, and the owner
    // duly reported "a few lates".
    //
    // The lesson: this is a MEASUREMENT of the animation, so set it from PHASE SAMPLE medians,
    // never from whatever happens to be sitting in learning.json. A persisted value is the
    // learner's state, and the learner's state can be stale, mid-convergence, or left over from a
    // different configuration entirely.
    //
    // With the seed at the converged median the aim offset is 393 - 319 = 74 -- the value it had
    // originally -- and a learner converging to 319 moves the effective aim NOWHERE. That is the
    // property that matters: no within-session drift.
    double tipPhaseSeedPhysicalMs = 319.0;
    // Fixed, because it MEASURED fixed: rMAD 9.6-13.4 at every anchor from 20% to 50%. A
    // horizon-dependent sigma here would be modelling a dependence the data does not show.
    // Chosen at the audit's 13.0 (between the measured rMAD 9.6 and sd 15.0 -- the sd is the
    // one inflated by a handful of two-stage rises, so this is the conservative middle).
    double tipPhaseSigmaMs = 13.0;
    // Per-session learning of the PHYSICAL term. Median (never mean) of the last N landings:
    // one mis-read stop moves a mean by sample/N and a median by nothing. SE of the median at
    // N=10 with rMAD 8.7-9.6 is ~3.4 ms, which is why the window is 10 and why no median is
    // published before the window is full.
    // 20, raised from 10 on 2026-08-05. MEASURED: replaying the real PHASE SAMPLE stream (n=112
    // across 4 sessions) through an engine-exact simulation, the shipped 10-sample window was
    // INJECTING 26% of the core spread -- robust SD 7.26 (learner frozen) vs 9.12 (shipped). It
    // was chasing noise. Widening to 20 recovers most of that (9.12 -> 7.89, -13%) and raises the
    // in-window hit rate at a 15 ms window from 61.6% to 67.0%. It also calms the per-landing aim
    // excursion: median jump 0.60 -> 0.35 ms, p90 2.35 -> 1.15, max 6.70 -> 3.53.
    //
    // Freezing the learner entirely scores slightly better at a 15 ms window (+0.9 pp) but WORSE
    // at 20 ms, and forfeits the per-jumpshot adaptation that is the whole point -- confirmed
    // live, the bot adapted to a different build with a different jumpshot and release speed.
    // Widening keeps the adaptation and takes most of the variance back.
    //
    // COST, and it is real: the cold-start shrink at recordPhaseConstantSample() is n/window, so
    // at n=3 the estimate now takes 15% of the correction instead of 30%. That is mitigated by
    // shrinking from the PERSISTED prior rather than the seed (same commit) -- with persistence
    // working, a returning session is not cold at all.
    int    tipPhaseLearnWindow = 20;
    // [ORION_PHASE_COLD_START] Fewest landings before the learner will move the constant at all.
    // Below this, one or two samples cannot distinguish a genuinely different animation from
    // ordinary shot-to-shot spread, so the seed stands. From here to tipPhaseLearnWindow the
    // estimate shrinks n/window of the way toward the observed median rather than waiting for a
    // full window and then jumping -- see recordPhaseConstantSample(). 0 or 1 disables the
    // partial-window behaviour and restores the old wait-for-ten cliff exactly.
    int    tipPhaseLearnMinSamples = 3;
    // Landing observations outside this band are DROPPED, never repaired. The observed range
    // was 281-383 ms; this band is that with margin, and anything outside it is a mis-read
    // stop (a two-stage rise, a dropout, a carryover) rather than a slow animation.
    double tipPhaseLearnMinMs = 240.0;
    double tipPhaseLearnMaxMs = 430.0;
    // [ORION_TYPE_TRIM] (settings tip_phase_type_trim_enabled / tip_phase_type_trim, default
    // OFF): signed per-shot-type ms added to the tip-phase constant at the decision AND
    // subtracted from the landing observation before the learner's band check. The pooled
    // learner therefore keeps estimating the base (Standstill-scale) animation; the trim
    // expresses the measured per-type difference (fade animations date ~4-6 ms SHORTER than
    // the pooled constant) without the learner fighting it. Keyed on exact classifyShotType
    // labels; missing key = 0; values clamped to +/-kTipPhaseTypeTrimCapMs at the accessor.
    bool tipPhaseTypeTrimEnabled = false;
    QMap<QString, double> tipPhaseTypeTrims;
    // End-of-rise detector, and these two numbers ARE the estimator -- the offline sweep is
    // in the block comment above recordPhaseConstantSample(). The stop is the last sample
    // that pushed the running max up by more than tipPhaseStopStepPct, confirmed by
    // tipPhaseStopQuietMs of no further growth. The meter dithers ~1 pp at its plateau and
    // creeps asymptotically into it, so a smaller step chases the dither (step 0.75 pp ->
    // rMAD 24.9) and a longer quiet window swallows a genuine second rise (quiet 250 ms ->
    // sd 115). 2.0 pp / 100 ms was the flat optimum of that sweep.
    double tipPhaseStopStepPct = 2.0;
    double tipPhaseStopQuietMs = 100.0;
    // CORROBORATION, and the reason this predictor cannot go guaranteed-late.
    //
    // The constant is a calibrated property of ONE animation. Handed a meter that is not that
    // animation -- a different game mode, a different frame rate, a synthetic rise -- an
    // uncorroborated lookup is confidently wrong in BOTH directions: a faster meter tops out
    // while the lookup still claims 150 ms of runway (a held, guaranteed-late release), a slower
    // one fires the lookup long before the meter arrives. Neither is acceptable, so the live
    // meter gets a veto: the phase member is admitted only when the sampler's own crossing is
    // statistically compatible with it, using the same disagreement^2 <= k^2 * (varA + varB)
    // shape the reg/sampler fusion above already uses.
    //
    // 3.0 measured, not chosen: over 221 real reservation updates paired to their landings the
    // disagreement (sampler crossing - phase tip) runs median +19.6 ms rMAD 21.4 at fill>=30 and
    // +8.4 ms rMAD 16.4 in the firing band -- the two predictors normally agree to well inside a
    // sigma, because the phase constant carries the same aim offset the biased sampler does. At
    // k=3 the gate admits 99.2% of fill>=30 updates and 98.3% of firing-band ones; at k=2 it
    // needlessly drops 5-9%. It only ever WITHDRAWS the member, falling back to the pre-existing
    // chain, so a wrong tolerance costs accuracy and never safety.
    double tipPhaseCorroborationSigmas = 3.0;
    // [ORION_SLOW_METER_DEFER] (default OFF -> bit-identical) Refuse to ARM -- or to adopt via
    // reschedule -- an autonomous live-tip precise-fire token whose deadline UNDERCUTS the phase
    // member's own deadline (phaseTipAbsMs - lead) by more than slowMeterDeferUndercutMs, while
    // the phase member is present and horizon-plausible on the SAME decision. DEFER, never fire:
    // the shot keeps holding and the ordinary tick re-evaluation arms at the phase deadline when
    // the transient passes, or the existing deadline-miss/abort machinery returns pass-through.
    //
    // MEASURED, 2026-08-06 (120 releases, two sessions, hand-counted by the owner: 3 earlies).
    // All three earlies (A#52, B#7, B#8) share one anatomy: 46-70ms after the meter appeared, on
    // an n=4-5 sample fit, the sampler's crossing swung ~90-190ms EARLIER within 8-13ms (seq 52:
    // 490.9 -> 304.6 in 8ms), tripped the tipPhaseCorroborationSigmas veto -- which read the
    // swing as "phase contradicted by the live meter" and DEMOTED the phase member -- and the
    // now-primary sampler armed/re-armed the token at command_eta 4.6/24.9/27.9ms while the
    // phase member simultaneously said command_eta 98-132ms. One frame later the sampler swung
    // back (or lost authority) and phase re-primaried, but the armed token was inside the
    // one-frame irreplaceable window and fired ~90-116ms early in meter phase (fill at release
    // 22-25 vs the normal 33-44). Per-frame capture (detframes_20260806_051100.csv) proves the
    // METER WAS NOT SLOW: it rose at the normal 0.18-0.27 pp/ms on every frame pair but one
    // near-flat stall pair -- the "slow velocity" (0.056-0.069 pp/ms) stamped at fire is the
    // degraded few-sample fit, not the animation. Counterfactually, firing at the phase deadline
    // would have released at fill ~44 on all three: normal.
    //
    // THE THRESHOLD, 45.0, from the arm-time undercut census of all 120 releases:
    //   110 phase-present normal arms:  undercut -1.2 .. +0.9 ms   (max +0.9)
    //     3 slow-regime arms:           undercut +88.7 / +95.1 / +119.9 ms
    //     7 arms with phase absent:     guard structurally inert
    // 45.0 is the midpoint of the (0.9, 88.7) gap: ~50x the normal population's maximum, ~half
    // the slow population's minimum, >2.5 detector frame gaps (so ordinary frame-to-frame
    // estimate drift cannot reach it), and above the shelved green-centring cap (40ms) so that
    // feature can never trip the guard against a phase-primary deadline. Every threshold from
    // 30 to 88 selects exactly the same 3-of-120 shots -- the choice is not delicate.
    //
    // FAIL-CLOSED both ways: the guard only refuses to create/adopt an EARLIER deadline; it
    // never moves an armed token, never fires, and stands down entirely when the phase member
    // is absent (below-anchor sampler arms are untouched). Its failure direction is by
    // construction "later, or no fire": a genuinely-faster-than-constant animation would be
    // deferred into the existing bounded abort (pass-through) instead of being rescued by the
    // +61-92ms-early-biased sampler -- the rescue path that produced all three counted earlies.
    bool   slowMeterDeferEnabled = false;
    double slowMeterDeferUndercutMs = 45.0;

    // === [ORION_PRESS_ANCHOR] Press-anchored tip predictor (Phase B scaffold) =========
    // The FOURTH member, and the only one whose anchor is not the delayed video: the raw
    // Square press wall time (input hook edge, captured in setPhysicalShotEpoch on the same
    // GUI tick that logs "Physical shot epoch") plus a per-shot-type LEARNED press->real-tip
    // constant. Reported in VISIBLE time (real tip + V + applied delay) so the existing
    // schedule-fire path consumes it unchanged:
    //     tip_abs_visible = press_wall + press_to_tip[type] + V + applied_delay
    // where V is the video-pipeline latency estimate (sidecar l_fixed when present, else the
    // full loop authority as a degraded stand-in — see pressAnchoredVideoLatencyMs()). A
    // constant error in V cancels between the learner (which subtracts it from the observed
    // stop) and the predictor (which adds it back), as long as both use the same estimator.
    //
    // DEFAULT OFF (settings press_anchored_predictor_enabled) and doubly gated: the member
    // abstains for a shot type until its accumulated observation weight reaches
    // pressAnchoredMinSamples, so a factory prior alone can never schedule a fire — with ONE
    // owner-directed exception ([ORION_PRESS_ANCHOR_BOOTSTRAP] 2026-08-09): in the
    // delay-starved regime (applied delay > 0 AND lead > usable max, i.e. every in-video
    // member withdrawn) the member runs unflagged and a MEASURED factory prior may fire at
    // the factory sigma, labelled press_anchored_boot, because the alternative there is
    // structurally no shot ever and therefore no observations ever. When it IS armed and
    // valid it only ever ADOPTS an otherwise-invalid decision (including one withdrawn by
    // the meter-delay starvation clause — its whole reason to exist); it never outvotes a
    // valid visible-evidence decision, so the delay-0 path is untouched.
    bool   pressAnchoredEnabled = false;
    int    pressAnchoredMinSamples = 30;
    // Per-type learned press->real-tip (wall ms) / sigma / accumulated weight. Seeded from
    // AppConfigData's factory priors; the windowed learner (recordPressToTipObservation)
    // refines them and they persist via pressAnchoredCalibrationUpdated -> settings.json.
    QMap<QString, double> pressAnchoredTipMs;
    QMap<QString, double> pressAnchoredTipSigmaMs;
    QMap<QString, double> pressAnchoredTipN;

    // === Item 2: Dual-signal consensus release ========================================
    // When two independent release signals (green crossing + velocity crossing, or velocity +
    // feedforward clock) agree within consensusWindowMs, fire at the EARLIER one — the later
    // is already late. Boosts confidence and reduces jitter-induced timing scatter.
    bool consensusReleaseEnabled = true;
    double consensusWindowMs = 6.0;     // max gap between two signals to count as consensus

    // === Item 3: Frame-age-weighted velocity sampling ================================
    // TemporalSampler already uses exponentially-weighted least-squares by sample recency.
    // This adds a time-based decay on top: older samples (by ms, not just by index) weigh less.
    double velocityDecayMs = 120.0;     // exponential decay constant for age weighting

    // === Item 6: Meter-memory fill extrapolation ======================================
    // When the meter goes to meter_memory (occluded), extrapolate the held fill using the
    // current velocity instead of freezing it. The meter keeps rising during occlusion.
    bool memoryExtrapolationEnabled = true;
    double memoryExtrapolationClampPct = 100.0; // cap extrapolated fill

    // === Item 7: Shot-type-aware target adjustment ====================================
    // Per-type target offset (track-%). Different shot types have slightly different green
    // window positions. Learned from post-release grade: consistently early -> lower target,
    // consistently late -> raise it. Bounded and slow.
    QMap<QString, double> shotTypeTargetOffsetPct;
    double targetOffsetGain = 0.03;     // EMA gain per shot (very slow — target is sensitive)
    double targetOffsetClampPct = 3.0;  // max ±offset from baseline target

    // === Item 8: Jitter-predictive release guard ======================================
    // When jitter is high, require higher confidence before firing predictively. The crossing
    // prediction is less reliable under jitter, so a higher floor prevents false-early fires.
    double jitterPredictiveFloor = 0.65; // confidence floor when jitter > jitterFloorTrigger
    double jitterFloorTrigger = 4.0;     // ms of jitter EMA to engage the higher floor

    // === Item 9: Kalman filter for fill prediction ====================================
    // 2-state Kalman (position=fill, velocity=rate) for smoother, more predictive fill
    // estimation that naturally handles acceleration/deceleration near the top.
    bool kalmanFillEnabled = true;
    double kalmanFillProcessNoise = 0.5;   // Q: process noise (meter rise variability)
    double kalmanFillMeasurementNoise = 2.0; // R: measurement noise (detection jitter)

    // === Item 10: Bayesian shot-type classification ===================================
    // Auto-detect shot type from meter rise profile (fast=Standstill, slow=Fade) using
    // the first N frames of meter rise. Falls back to user-input pattern if uncertain.
    bool bayesianShotTypeEnabled = false; // OFF by default — needs live validation
    double standstillRiseThreshold = 0.25; // pct/ms — above this = fast type (Standstill)
    double fadeRiseThreshold = 0.12;       // pct/ms — below this = slow type (Fade)

    // === Per-type velocity priors ======================================================
    // EMA of the sampler velocity at vision-timed releases, persisted per type. Live
    // velocity estimates outside [prior*lo, prior*hi] are clamped to the band for the
    // predictive computations — rejects detector blips / wifi torn frames without
    // discarding the sample.
    QMap<QString, double> shotTypeVelocityPriorPctMs;
    double velocityPriorGain = 0.2;
    double velocityPriorLoFactor = 0.5;
    double velocityPriorHiFactor = 1.6;

    // === Legacy Tempo settings + output shaping =======================================
    // Retained for settings compatibility only. The global Tempo toggle is the sole
    // Square-remap authority; stale per-type values cannot override it.
    QMap<QString, QString> shotTypeModeOverride;
    // Per-type tempo flick-edge hold (ms). Absent -> the global tempoFlickHoldMs. The flick edge
    // decides which frame the game registers the release, so it is calibratable per type.
    QMap<QString, double> shotTypeFlickHoldMs;
    // Legacy serialized setting. TempoSquare is an output remap and cannot opt
    // into a different open-loop release path.
    bool tempoOpenLoopPrimary = false;
    // Legacy serialized setting. Timing uses the canonical plain shot-type
    // bucket in both Button and Tempo, so this flag has no authority.
    bool tempoOwnClockOnly = false;
    // Go-To-only left-stick cancel. Square-triggered Tempo shares ButtonShot's
    // non-cancellable ownership contract.
    double lsCancelThresholdPct = 65.0;
    int lsCancelFrames = 3;
    // === Auto-calibration (Layer C) ===================================================
    // When calibrationMode is on, a reliable SIGNED verdict (the on-screen feedback-text oracle,
    // fed via updateShotOutcome) is allowed to drive learnFromOutcome even while calibrationFrozen
    // (the FREEZE protects only against the unreliable meter self-grade, not the oracle). The
    // ACQUIRE->LOCK controller then converges + LOCKs the per-type offset automatically.
    bool calibrationMode = false;
};

struct ShotContext {
    ShotMode mode = ShotMode::ButtonShot;
    HoldState state = HoldState::Idle;
    // Monotonic per-process identity for the physical shot that opened this
    // context. It is transported as a decimal string across the sidecar JSON
    // boundary so a delayed pose landmark cannot authorize a later shot.
    quint64 armToken = 0;
    double armTimestampMs = 0.0;
    double holdStartMs = 0.0;
    double releaseTriggerMs = 0.0;
    double releaseTriggerPtsMs = 0.0;  // Orion 13.1: PTS-corrected release time (now - frameAgeMs)
    double releaseCompleteMs = 0.0;
    double cooldownEndMs = 0.0;
    double fillPct = 0.0;
    double confidence = 0.0;
    double velocityPctS = 0.0;
    double greenStartPct = -1.0;
    double greenEndPct = -1.0;
    double greenCenterPct = -1.0;
    double etaToGreenMs = -1.0;
    // [ORION_REG_FUSION] Sidecar registration far-horizon time-to-TIP estimate (ms) + its confidence,
    // copied per-frame from the DetectionResult. -1/0 when the sidecar omits them (older sidecar / no
    // registration lock). Consumed by the crossing fusion (far horizon) in processHolding + the mirror.
    double regTipMs = -1.0;
    double regConf = 0.0;
    int regSeq = -1;
    double regSigmaMs = -1.0;
    QString regModelId;
    QString regModelVersion;
    // [ORION_FUSED_FIRE] registration fit-quality companions (unweighted residual RMS pp +
    // uncensored sample count) — the fused posterior's sigma_reg inputs. -1/0 = no fit.
    double regRmsePp = -1.0;
    int regN = 0;
    // Absolute engine-clock registration tip estimate (capture-aligned sample + reg_tip_ms),
    // stamped in updateDetection so decoder/IPC arrival jitter cannot slide the forecast.
    double regTipAbsMs = -1.0;
    QString tipPredictionSource = QStringLiteral("none");
    double tipPredictionSigmaMs = -1.0;
    // Exact lead-authority and deadline decision used by autonomous tip timing.
    // Lateness is max(0, decision_clock - deadline); a positive value means the
    // prediction arrived too late and must not be relabeled as a tip release.
    QString tipPredictionAuthorityKind = QStringLiteral("none");
    double tipPredictionLeadMs = -1.0;
    double tipPredictionLeadSigmaMs = -1.0;
    double tipPredictionDeadlineMs = -1.0;
    double tipPredictionDeadlineLatenessMs = 0.0;
    // [ORION_FUSED_FIRE] per-shot fused posterior diagnostics (stamped every Holding tick the
    // estimator is anchored; logged on the FusedShadow line at shot end).
    double fusedMuTipMs = -1.0;
    double fusedSigmaMs = -1.0;
    double fusedShadowFireMs = -1.0;      // engine ms the fused rule WOULD have fired (-1 = never)
    double fusedShadowFillPct = -1.0;
    double accelerationPctS2 = 0.0;
    double frameAgeMs = 0.0;
    double holdStartFrameAgeMs = 0.0;    // Orion 13.1: frame age when hold started
    double firstMeterFrameAgeMs = 0.0;   // Orion 13.1: frame age when meter first seen
    int consecutiveFrames = 0;
    double networkOffsetMs = 0.0;
    QString syncSource = QStringLiteral("Waiting");
    double syncConfidence = 0.0;
    int holdFrames = 0;
    double lastDetectionMs = 0.0;
    bool meterDetected = false;
    // Time (engine clock) the meter's PEAK fill last increased. Used to tell a
    // still-rising meter (peak advancing) from a stalled one, robustly — peak is
    // monotonic so it ignores per-frame fill noise from a small/low-res capture.
    double peakFillMs = 0.0;
    // Latched true the first time THIS shot observes a real, rising, confident
    // meter. The hold is gated on this (the meter actually appearing), not on a
    // green window — a contested shot may never show green. firstMeterSeenMs marks
    // when it appeared so the post-meter timing window is measured from there.
    bool meterSeenThisShot = false;
    double firstMeterSeenMs = -1.0;
    // === Anchor validity (T2 carryover rejection) ===
    // A meter-appear CLOCK anchor is only trusted once the genuine-accept stream proves it
    // belongs to THIS shot's rising meter, not a lingering previous-shot meter panning with
    // the camera (a REAL raw detection, so genuine-vs-echo cannot reject it). Episode
    // tracking lives in updateDetection: a re-lock restarts the trajectory on an explicit
    // acquire after coast or on a >50ms genuine-sample gap; anchor candidate episodes also restart
    // on a >300ms gap or a >8% fill drop. It VALIDATES (anchorValidMs = episode first-sight time, backdated
    // so the confirmation sample adds no timing error) when the first sight was low-fill
    // (<= anchorMaxFirstFillPct) and the next genuine sample did not descend, OR the fill
    // genuinely rose >= anchorRiseMinPct across the episode. Learned clocks and clock
    // learners consume validity; physical recovery remains separately bounded.
    // -1 = never validated this shot.
    double anchorCandFirstMs = -1.0;
    double anchorCandFirstFill = 0.0;
    double anchorCandFirstFrameAgeMs = 0.0;
    double anchorCandLastMs = -1.0;
    double anchorCandLastFill = 0.0;
    double anchorValidMs = -1.0;
    // DIAGNOSTIC (T1 "Release vision:" line): first engine-clock ms a DUE feedforward clock
    // was held back by the reachability gate this shot (-1 = never held); the log line
    // derives reachHeldMs = releaseTriggerMs - reachHoldStartMs.
    double reachHoldStartMs = -1.0;
    // DIAGNOSTIC (T4 tip gate): how long past its deadline the due feedforward clock had
    // been deferred toward the predicted vision crossing at the last defer tick (0 = never).
    double tipGateDeferMs = 0.0;
    // T4 tip-gate health ring: engine-clock timestamps of the last few GENUINE fresh
    // accepts (pushed in updateDetection), so the gate can require CONTINUOUS fresh tracking
    // RIGHT NOW (>= 3 accepts within 150ms) before it defers the clock — a shot that only
    // just re-acquired, or whose tracking is intermittent, does not qualify. Reset per shot.
    std::array<double, 8> freshAcceptRingMs{{-1, -1, -1, -1, -1, -1, -1, -1}};
    int freshAcceptRingPos = 0;
    void pushFreshAccept(double tsMs)
    {
        freshAcceptRingMs[static_cast<std::size_t>(freshAcceptRingPos)] = tsMs;
        freshAcceptRingPos = (freshAcceptRingPos + 1)
            % static_cast<int>(freshAcceptRingMs.size());
    }
    [[nodiscard]] int countFreshAcceptsWithin(double now, double windowMs) const
    {
        int n = 0;
        for (double t : freshAcceptRingMs) {
            if (t >= 0.0 && (now - t) <= windowMs) {
                ++n;
            }
        }
        return n;
    }
    // DIAGNOSTIC (T1): stamped at triggerRelease — the last sample was a genuine fresh
    // accept AND still inside the (wifi-scaled) freshness window at the release instant.
    bool visionFreshAtRelease = false;
    double peakFillPct = 0.0;
    // === [ORION_TIP_PHASE] the shot's animation clock ================================
    // CAPTURE-clock time this shot's fill first crossed config_.tipPhaseAnchorPct on the way
    // UP, linearly interpolated between the straddling genuine samples so the ~16 ms frame
    // grid adds no quantisation. -1 = the crossing was never OBSERVED this shot, which is the
    // fail-closed case: a meter first seen already above the anchor (a carryover, a late
    // acquire) cannot be dated, so the phase predictor stands down and the sampler carries the
    // shot exactly as before. Reset by beginShot's ShotContext{} and, explicitly, by the
    // detector-relock discontinuity that resets peakFillPct -- a re-lock starts a NEW
    // trajectory episode, and dating the new animation from the old episode's crossing would
    // be the single worst failure mode this predictor has.
    double fillPhaseAnchorMs = -1.0;
    // [ORION_TIP_PHASE_LADDER] WHICH anchor level fillPhaseAnchorMs actually dates. Negative
    // until a crossing is observed. Carried per-shot rather than read from config because the
    // ladder means two shots in the same session can legitimately be dated at different levels,
    // and the constant that pairs with the anchor differs accordingly.
    double fillPhaseAnchorLevelPct = -1.0;
    // Previous genuine (fill, capture-time) pair, kept solely to interpolate the crossing
    // above. Reset alongside fillPhaseAnchorMs so a re-lock cannot form a straddling pair out
    // of one sample from each episode.
    double phasePrevFillPct = -1.0;
    double phasePrevCaptureMs = -1.0;
    // Go-To freshness trust. lastSampleFreshAccept = the most recent meter sample was a
    // CLEAN raw accept this frame (NOT a stale_or_memory held/extrapolated sample, which
    // the sidecar flags via raw_fed when raw detection was meter_memory/roi_not_found).
    // sawFreshMeterThisShot latches once any fresh accept has arrived this shot. A Go-To
    // may release only while the current sample is a fresh accept, so a phantom held 100
    // reported during a capture dropout can never fire it. Both reset by beginShot.
    bool sawFreshMeterThisShot = false;
    bool lastSampleFreshAccept = false;
    // Stronger release-authority bit: THIS telemetry payload came from a genuinely
    // detected, unique game frame. memory_trust may keep lastSampleFreshAccept true
    // across one held echo, but an echo/duplicate/stale frame must never arm or fire a
    // vision deadline. The bounded no-meter/hard-cap paths do not read this bit.
    bool lastSampleGenuineAccept = false;
    // Incremented whenever a detector re-lock starts a discontinuous trajectory.
    // A scheduled vision fire carries the epoch it was computed from; an old-epoch
    // deadline cannot survive a lock jump into the replacement trajectory.
    int visionEpoch = 0;
    // Lowest FRESH-accept fill (%) observed this shot (1000 = none yet). Proves the meter
    // was genuinely seen climbing from below the target — distinguishes a real rising shot
    // meter from a lingering prior-shot meter / weak false-positive that is already at the
    // top from the first sample. Gates the non-Go-To reactive release. Reset by beginShot.
    double minFreshFillPct = 1000.0;
    bool released = false;
    // True when this owned shot exists only to generate an admissible
    // release/effect pair for the session latency oracle. Calibration probes
    // release from the first validated live rising trajectory; they never use
    // a learned/fixed shot clock or become normal gameplay timing authority.
    bool latencyCalibrationProbe = false;
    // Automatic cold-start additionally requires independent current-shot meter structure.
    // Explicit/manual calibration retains its intentional user-authorized behavior.
    bool latencyCalibrationAutomaticProbe = false;
    // Second-stage causal validation metadata frozen when its in-green deadline is armed. Negative
    // means this is an ordinary release or the telemetry-only first controlled probe.
    double latencyValidationTargetPct = -1.0;
    double latencyValidationTolerancePct = -1.0;
    bool gameplayStructureVerified = false;
    // Controller-origin physical edge expected by this shot and the exact reader epoch
    // that proved structure. Automatic probes require both nonzero values to match.
    quint64 physicalShotEpoch = 0;
    quint64 gameplayStructureEpoch = 0;
    // Monotonic id stamped at triggerRelease. Pairs the "Release issued" telemetry
    // line with the "Release submit" line (logged at the actual ViGEm write) so a
    // logged release can be proven to have reached the virtual pad. 0 = never released.
    int releaseSeq = 0;
    // Precise scheduler token that owned the physical release transaction. Zero
    // means an immediate/in-tick release. This is diagnostic identity only.
    quint64 releaseScheduleToken = 0;
    // Immutable controller-route proof carried by every route-sensitive release,
    // including immediate/in-tick releases. The controller uses it at the final
    // submit boundary; zero/None means legacy/unbound release authority only.
    // releaseScheduleToken remains zero for an immediate release even when these
    // fields contain a valid route binding.
    quint64 releaseScheduleRouteGeneration = 0;
    LatencyControllerRoute releaseScheduleRoute = LatencyControllerRoute::None;
    // [ORION_ARMED_SOURCE] Predictor attribution of the armed token this release actually
    // fired from, copied verbatim from the schedFireArmed* arm-time snapshot at the two
    // token-consume sites (consumeDueScheduledFire and abort's consumeConfirmedSchedule)
    // BEFORE clearScheduledFire() wipes it. Empty source / -1 values mean the release did
    // not fire from an attributed autonomous vision token (in-tick, feedforward, pose,
    // legacy, calibration). DIAGNOSTIC IDENTITY ONLY -- nothing in timing, scheduling or
    // learning reads these; they exist so the per-shot "Outcome identity:" line can say
    // WHICH predictor's decision armed the shot and how uncertain that decision was.
    QString armedPredictorSource;
    double armedPredictorSigmaMs = -1.0;
    double armedPredictorFillPct = -1.0;
    double armedPredictorCommandEtaMs = -1.0;
    double releaseFillPct = 0.0;
    double targetPct = 96.0;
    double releaseEtaMs = -1.0;
    double effectiveLatencyMs = 0.0;
    // === Ceiling stack diagnostics (stamped every Holding tick; 0/-1 when the flags are off,
    // so a default engine records exactly the old values). ===
    double plateauShiftMs = 0.0;         // [ORION_PLATEAU_AIM] +½·capHold applied to the crossing
    double templateArrivalMs = -1.0;     // [ORION_TEMPLATE_ARRIVAL] template-predicted crossing (abs ms)
    double templateBlendWeight = 0.0;    // template prior weight used in the crossing blend
    int templateMatchedIdx = -1;         // matched template index (-1 = none)
    double banditLeadOffsetMs = 0.0;     // legacy telemetry; live authority is permanently zero
    // === Release-path attribution snapshot (DIAGNOSTIC, read-only) ===
    // Records the inputs the release decision used, stamped every Holding frame so they
    // are current when triggerRelease fires on the same frame. Purpose: make a Go-To
    // release mechanically attributable to its block (green_confirmed vs predictive_target)
    // and reveal the green-confirmation race. NONE of these feed any release/timing/output
    // decision — they are pure recordings logged on the "Release attribution:" line.
    bool greenConfirmedAtRelease = false;
    // green_center | green_tip | meter_full — which target source the holding frame used.
    QString targetModeAtRelease = QStringLiteral("idle");
    double greenWidthAtReleasePct = 0.0;
    // Fill% when greenTracker_ FIRST confirmed this shot (-1 = never confirmed), and ms
    // from first-meter-seen to that confirm. Together they show whether/when the tracker
    // won the race against the velocity path for a fast Go-To meter.
    double greenConfirmFillPct = -1.0;
    double greenConfirmMs = -1.0;
    // Engine sampler velocity (%/ms) used by the predictive paths — NOT the detector's
    // velocityPctS above. The reachability gate's clamped expected rise and its result.
    double releaseVelocityPctMs = 0.0;
    double releaseCrossingEtaMs = -1.0;   // crossing - now (-1 = no usable crossing)
    double expectedRiseAtReleasePct = 0.0;
    bool withinReachAtRelease = false;
    // Sub-tick scheduler attribution: true when the release output was submitted by the
    // precise fire thread; delta = actual fire - scheduled deadline (ms, target sub-ms).
    bool firedByScheduler = false;
    double scheduledFireDeltaMs = 0.0;
    // Auto wifi mode + the jitter EMA that engaged it (telemetry only).
    bool wifiMode = false;
    double networkJitterMs = 0.0;
    // Feedforward-clock vs vision-crossing divergence at release (ms; 0 when only one
    // source existed). Logged so the learner can spot a drifting clock or a lying ETA.
    double clockVisionDivergenceMs = 0.0;
    // === SHADOW MODE (autonomous_vision_shadow) — compute-only, NEVER controls release ===
    // The global-velocity phase-aligned autonomous model's decision, recorded for offline/live A/B
    // against the actual release + post-release verdict. shadowFireMs = engine-clock ms the model
    // WOULD have released (first frame its deadline came due; -1 = never). Logged on "Shadow timing:".
    double shadowFireMs = -1.0;
    double shadowFireFillPct = -1.0;
    double shadowPredTipMs = -1.0;     // predicted absolute tip time at the model's fire moment
    double shadowVgPctMs = 0.0;        // global rise velocity used (%/ms)
    QString shotType = QStringLiteral("Standstill");
    // [ORION_TEMPO_FADE_MIRROR] LATCHED at arm time from shotType, and never re-read
    // afterwards. A fade runs the mirrored Tempo gesture (gather RS-UP, release flick
    // DOWN); everything else keeps gather DOWN / flick UP.
    //
    // Latched rather than derived per tick for a specific reason: classifyShotType is
    // re-evaluated every frame and CAN change mid-press while the thumb is still ramping
    // (Standstill -> Right Fade). Deriving the direction live would physically reverse a
    // HELD stick mid-hold, which is worse than the bug this fixes. That hazard is exactly
    // what the old "every shot type uses the same gesture" invariant was protecting, and
    // latching preserves it while still letting a fade use its own gesture: gather and
    // release both read this one value, so the press is self-consistent end to end.
    bool tempoFadeGesture = false;
    // Canonical per-shot-type timing bucket. TempoSquare is only an output remap,
    // so it shares this key and every learned timing value with ButtonShot.
    QString bucketKey = QStringLiteral("Standstill");
    // Left-stick vector at arm time (for Tempo LS-cancel: a change of classified fade direction).
    double lsArmX = 0.0;
    double lsArmY = 0.0;
    int lsCancelFrames = 0;   // consecutive LS-cancel-qualifying frames (telemetry/gate)
    // Tempo flick scheduling (telemetry): plannedFlickMs = chosen deadline relative to the anchor;
    // actualFlickMs = engine ms when the RS-up flick actually started in processReleasing.
    double plannedFlickMs = -1.0;
    double actualFlickMs = -1.0;
    QString releasePlan = QStringLiteral("Idle / pass-through");
    QString releaseReason = QStringLiteral("Idle / pass-through");
    // Canonical machine-parseable release reason. Release examples: green_confirmed,
    // predictive_target, feedforward_target, reactive_target. Abort examples:
    // no_meter_abort, detector_authority_lost_abort, stale_detection_abort.
    QString releaseReasonCode = QStringLiteral("idle");
    // Detection-presence state at decision time: no_sample_ever | stale |
    // rejected | accepted | fallback. Distinguishes "never received a sample"
    // from "received a fresh, valid one" so a 0/0/0 sample can't masquerade as fresh.
    QString detectionPresence = QStringLiteral("no_sample_ever");
    // Origin of the last accepted sample (e.g. "sidecar-fusion", "native-preview", "none").
    QString detectorSource = QStringLiteral("none");
    QString abortReason;
    // === Per-shot detection-sample census (DIAGNOSTIC, read-only) ===
    // Counts every detection sample that arrived while this shot was Armed/Holding,
    // classified by the gate that accepted/blocked it, so a starved shot ("meter not
    // visible" at release) is attributable from one "Release detsummary:" log line:
    // detFreshAccepts==0 with detStaleOrMemory high = sidecar starved mid-shot
    // (detector/feed-gate side); detSamplesTotal==0 = no payloads at all (pipe/
    // capture side); detConfLow/detStaleFrameDrop high = native gates rejecting.
    // None of these feed any timing decision. Reset by beginShot (struct re-init).
    int detSamplesTotal = 0;
    int detFreshAccepts = 0;
    int detStaleOrMemory = 0;
    int detConfLow = 0;
    int detStaleFrameDrop = 0;
    int detNotDetected = 0;
    int detDuplicateFrameDrop = 0;
    int detRelockResets = 0;
    // Raw sidecar classification from the most recent payload received while
    // this shot was owned. Diagnostic only; never release authority.
    QString lastDetectionStage = QStringLiteral("none");
    QString lastDetectionRejectionReason = QStringLiteral("none");
    QString lastDetectionPayloadSource = QStringLiteral("none");
    // EXPERIMENT census: meter_memory echoes promoted to fresh-equivalent by the memory-trust
    // gate this shot (0 unless memoryTrustEnabled). Surfaced on the "Release freshness:" log
    // line as memTrusted= so a live A/B can see how often the trust fired.
    int detMemoryTrusted = 0;
    // Engine-clock ms of the FIRST fresh accept this shot (-1 = never).
    double firstFreshAcceptMs = -1.0;
    // Engine-clock ms of the MOST RECENT fresh accept this shot (-1 = never). Subtracted
    // from releaseTriggerMs to audit source age at the instant of release. Production
    // release authority requires this sample to remain inside the strict total-age lease.
    double lastFreshAcceptMs = -1.0;
    // History tracking (3-frame rolling window)
    std::deque<int> rightStickYHistory;
    std::deque<int> rightStickXHistory;
    std::deque<bool> xButtonHistory;
    // Smoothed values
    double smoothRightStickY = 0.0;
    double smoothRightStickX = 0.0;
    bool xButtonConsistent = true;
};

[[nodiscard]] constexpr bool poseArmTokenMatches(quint64 expected, quint64 received) noexcept
{
    return expected != 0 && received == expected;
}

// No-meter pose-based timing state. Tracks pose landmarks (push/release) received
// from the Python sidecar. The engine schedules release using its own clock at
// message arrival + noMeterBaseOffsetMs - noMeterDecodeCompMs.
// frame_seq is for staleness rejection only (no cross-process wall-clock).
struct PoseTimingState {
    bool pushReceived = false;
    double pushArrivalMs = 0.0;
    int pushFrameSeq = -1;
    double pushConfidence = 0.0;
    bool releaseReceived = false;
    double releaseArrivalMs = 0.0;
    int releaseFrameSeq = -1;
    double releaseConfidence = 0.0;
    // Release authority is a short, per-shot lease on the latest accepted pose
    // payload. Arrival time is used because the sidecar and engine do not share a
    // monotonic clock; frame_seq still prevents replay/out-of-order refreshes.
    bool lastLandmarkFreshAccept = false;
    double lastLandmarkArrivalMs = -1.0;
    int lastLandmarkFrameSeq = -1;
    quint64 acceptedArmToken = 0;
    int authorityEpoch = 0;
    bool poseScheduled = false;
};

// === [ORION_FUSED_FIRE] Per-shot Gaussian posterior over the tip time ==================
// State: (mu, var) over the ABSOLUTE engine-clock ms the meter reaches the tip. Anchored
// once per shot from the per-type appear->tip clock prior; updated per FRESH sample by
// sequential Bayesian fusion of the registration-fit tip and the near-tip sampler crossing,
// each with a dynamic sigma computed by the caller. Correlation control: an information
// (contraction) update happens only on samples whose fill actually STEPPED (a new game
// frame); unchanged-fill samples only grow process noise. chi^2-gates each source against
// the current posterior, with the vision-pair override (two agreeing fresh sources beat a
// wrong prior: both pass + the anchor's authority is permanently diluted for the shot).
class ORION_AUTOMATION_API FusedTipEstimator final {
public:
    struct SourceMeas {
        double tipAbsMs = -1.0;   // absolute engine-clock tip estimate
        double sigmaMs = -1.0;    // <=0 => source invalid this sample
    };
    void reset();
    void anchor(double tipAbsMs, double sigmaMs);
    void update(bool newInfo, const SourceMeas& reg, const SourceMeas& sampler,
                bool riseBecamePeak, double processNoiseMs, double peakInflateMs,
                double chiGate = 9.0);
    [[nodiscard]] bool anchored() const noexcept { return anchored_; }
    [[nodiscard]] double muTipMs() const noexcept { return mu_; }
    [[nodiscard]] double sigmaMs() const noexcept;
    [[nodiscard]] int infoUpdates() const noexcept { return infoUpdates_; }
    [[nodiscard]] bool pairOverrideFired() const noexcept { return pairOverrideFired_; }
private:
    void fuseOne(double m, double sigmaMs);
    bool anchored_ = false;
    double mu_ = 0.0;
    double var_ = 0.0;
    int infoUpdates_ = 0;
    bool pairOverrideFired_ = false;
};

class ORION_AUTOMATION_API TemporalSampler final {
public:
    void reset();
    void addSample(double fillPct, double timestampMs);
    // Crossing prediction WITH fit diagnostics: the Phase-1 fused posterior needs an honest
    // per-frame sigma, which requires the adopted model's residual + support, not just the
    // crossing time. wrss = weighted RSS of the ADOPTED model (quad when usedQuad, else the
    // linear fit; -1 when no fit was computed, e.g. already-at-target early return).
    struct CrossingFit {
        double crossingMs = -1.0;   // absolute ms on the sampler clock; -1 = no usable crossing
        double wrss = -1.0;
        double weightSum = 0.0;
        double slopePctPerMs = 0.0; // adopted model's derivative at the latest sample
        double spanMs = 0.0;        // oldest-to-newest evidence span in the fit window
        int n = 0;
        bool usedQuad = false;
        // True only when a concave-down fit cannot reach targetPct and crossingMs
        // therefore names the fitted peak.  Release authority must treat this as
        // weaker than an actual target crossing and independently prove that the
        // predicted peak reaches the detected green band.
        bool usedPeakFallback = false;
        double predictedPeakPct = -1.0;
    };
    [[nodiscard]] CrossingFit predictCrossing(double targetPct) const;
    [[nodiscard]] double predictCrossingMs(double targetPct) const;
    [[nodiscard]] double velocityPctPerMs() const;
    // Item 9: Kalman-filtered fill prediction
    [[nodiscard]] double kalmanFillPct() const noexcept { return kf_.position; }
    [[nodiscard]] double kalmanVelocity() const noexcept { return kf_.velocity; }
    void configureKalman(double processNoise, double measurementNoise) {
        kf_.Q = processNoise;
        kf_.R = measurementNoise;
    }

private:
    struct Sample {
        double fillPct = 0.0;
        double timestampMs = 0.0;
    };
    std::deque<Sample> samples_;
    // Item 9: 2-state Kalman filter (position=fill%, velocity=%/ms)
    struct FillKalmanState {
        double position = 0.0;    // estimated fill%
        double velocity = 0.0;    // estimated rise rate (%/ms)
        double P00 = 100.0;       // position uncertainty
        double P01 = 0.0;         // cross covariance
        double P11 = 100.0;       // velocity uncertainty
        double Q = 0.5;           // process noise
        double R = 2.0;           // measurement noise
        double lastTs = -1.0;     // last update timestamp
        bool initialized = false;
    };
    FillKalmanState kf_;
    void updateKalman(double fillPct, double timestampMs);
};

class GreenWindowTracker final {
public:
    void reset();
    void update(double startPct, double endPct, double confidence);
    void setFastPath(bool enabled) noexcept { fastPath_ = enabled; }
    [[nodiscard]] bool confirmed() const noexcept { return confirmed_; }
    [[nodiscard]] double widthPct() const noexcept { return widthPct_; }
    [[nodiscard]] double centerPct() const noexcept { return bestCenterPct_; }
    // Green-window ENTRY (lower edge). Fades aim here (not center/top) so the whole made-shot
    // window entry->tip is landing tolerance; returns -1 when no green is confirmed.
    [[nodiscard]] double startPct() const noexcept { return confirmed_ ? bestStartPct_ : -1.0; }
    [[nodiscard]] double targetPct(const QString& mode, double fallbackPct, double learningBiasPct) const;
    // Dead-top target: aim the confirmed window's TOP EDGE (bestEndPct_, the contest-invariant
    // make-point) for ALL widths, pulled down by tipMarginPct (0 = dead top). Returns -1 when no
    // green is confirmed (caller times the meter fully to the top instead).
    [[nodiscard]] double adaptiveTargetPct(double tipMarginPct, double learningBiasPct) const;

private:
    std::deque<double> starts_;
    std::deque<double> ends_;
    int stableFrames_ = 0;
    bool confirmed_ = false;
    double bestStartPct_ = -1.0;
    double bestEndPct_ = -1.0;
    double bestCenterPct_ = -1.0;
    double widthPct_ = 0.0;
    double confidence_ = 0.0;
    bool fastPath_ = false;
};

// [ORION_TEMPLATE_ARRIVAL] H4 template-matched green-arrival estimator.
// Reads TIME-AT-FILL crossings (the deterministic-replay measurement; fill-at-time capture
// jitter and argmax-plateau tip noise are what made the first offline test look random),
// clusters completed shots ONLINE into <= k per-bucket templates (EMA cluster means), matches
// the in-flight shot's early crossing-vector by SHAPE (inter-crossing deltas -> t0-free), and
// predicts the green-arrival by anchoring the matched template at the LATEST observed crossing
// (t@80-90: the shortest extrapolation latency allows). With press-t0 the press->first-crossing
// interval joins the match features (earlier SELECT; the press timestamp is already native).
// Session-scoped (templates are per-user/per-connection live state; persistence is a follow-up).
class ORION_AUTOMATION_API TemplateArrivalEstimator final {
public:
    // Crossing grid: fills 10,15,...,90 (17 rungs) + 96 (the green-arrival label slot).
    static constexpr int kGridN = 18;
    [[nodiscard]] static double gridFill(int i) noexcept
    {
        return i < kGridN - 1 ? 10.0 + 5.0 * i : 96.0;
    }
    void configure(int k, int matchMinFills, bool pressT0) noexcept
    {
        k_ = std::clamp(k, 1, 8);
        matchMinFills_ = std::max(2, matchMinFills);
        pressT0_ = pressT0;
    }
    void beginShot(const QString& bucketKey, double pressT0Ms);
    // Learn from the shot that just ENDED (fold its completed crossing-vector into the nearest
    // template / spawn a new cluster). Call BEFORE beginShot resets the vector.
    void endShot();
    void addSample(double fillPct, double tMs);
    [[nodiscard]] bool matched() const noexcept { return matchedIdx_ >= 0; }
    [[nodiscard]] int matchedIdx() const noexcept { return matchedIdx_; }
    // Template-predicted ABSOLUTE ms (engine clock) the fill reaches targetPct, anchored at the
    // latest observed crossing. < 0 when no match / no anchor yet.
    [[nodiscard]] double predictArrivalMs(double targetPct) const;
    [[nodiscard]] int templateCount(const QString& bucketKey) const
    {
        return templates_.value(bucketKey).size();
    }

private:
    struct Row {
        std::array<double, kGridN> cross{};   // crossing ms relative to the shot's FIRST crossing
        double pressOffsetMs = -1.0;          // press -> first-crossing interval (-1 = unknown)
        int n = 0;                            // shots folded into this cluster mean
    };
    [[nodiscard]] double shapeDistance(const Row& tmpl) const;
    void rematch();
    static constexpr double kNewClusterGateMs = 40.0;  // RMS split threshold for a new template
    QHash<QString, QVector<Row>> templates_;
    std::array<double, kGridN> cross_{};      // ABSOLUTE ms this shot; <0 = not crossed yet
    QString key_;
    double press0_ = -1.0;
    double lastFill_ = -1.0;
    double lastT_ = -1.0;
    int crossedCount_ = 0;                    // crossed rungs BELOW the 96 slot
    int matchedIdx_ = -1;
    int k_ = 4;
    int matchMinFills_ = 4;
    bool pressT0_ = false;
};

// [ORION_BANDIT_LEAD] offline experiment utility. Arms are lead offsets −4,−2,0,+2,+4 ms.
// AutomationEngine deliberately does not wire live detector diagnostics into this class.
class ORION_AUTOMATION_API BanditLeadTuner final {
public:
    static constexpr int kArms = 5;
    [[nodiscard]] static double armOffsetMs(int i) noexcept { return (i - 2) * 2.0; }
    void configure(int minPullsPerArm, int maxShots) noexcept
    {
        minPulls_ = std::max(1, minPullsPerArm);
        maxShots_ = std::max(kArms, maxShots);
    }
    void reset();
    [[nodiscard]] bool locked() const noexcept { return locked_; }
    [[nodiscard]] double lockedOffsetMs() const noexcept { return lockedOffsetMs_; }
    // The lead offset to APPLY: the exploration arm while unlocked+exploring, the converged
    // optimum once locked, 0 when unlocked and exploration is not allowed (not in calibration).
    [[nodiscard]] double currentOffsetMs(bool exploreAllowed) const noexcept;
    // A release actually fired with the current arm -> latch it for grade attribution.
    void noteRelease(bool exploreAllowed);
    // Offline experiment reward: 0 = EARLY, 1 = GREEN, 2 = OVER.
    void noteGrade(int label);
    [[nodiscard]] int grades() const noexcept { return grades_; }
    [[nodiscard]] int armPulls(int i) const noexcept { return armPulls_[i]; }
    [[nodiscard]] int armGreens(int i) const noexcept { return armGreens_[i]; }
    [[nodiscard]] int armEarly(int i) const noexcept { return armEarly_[i]; }
    [[nodiscard]] int armOver(int i) const noexcept { return armOver_[i]; }

private:
    void maybeLock();
    void advanceArm();
    std::array<int, kArms> armPulls_{};
    std::array<int, kArms> armGreens_{};
    std::array<int, kArms> armEarly_{};
    std::array<int, kArms> armOver_{};
    int currentArm_ = 2;      // start at δ=0
    int pendingArm_ = -1;     // arm awaiting its grade (-1 = none)
    int grades_ = 0;
    bool locked_ = false;
    double lockedOffsetMs_ = 0.0;
    int minPulls_ = 3;
    int maxShots_ = 25;
};

// === C1: watchdog SAFE-MODE auto-recovery policy ==========================================
// OrionAppController's safe mode latches automation OFF after repeated watchdog trips. That
// protection must never become a PERMANENT mid-session kill switch when the trips were
// transient (live 2026-07: an input-hook blip paired with a frame-stall restart latched safe
// mode until a manual reset — the bot "stopped firing entirely"). Once the stream has
// DEMONSTRABLY re-healed — a continuous run of healthy watchdog ticks — safe mode may
// auto-exit and re-arm. Pure, headless-testable policy (lives here, not in the controller,
// so the QtTest suite can exercise it without the full app):
//   - the controller feeds one observation per watchdog tick while safe mode is ACTIVE;
//   - "healthy" = stream Running with fresh frames (the controller computes it);
//   - the health run must be CONTINUOUS for stabilityWindowMs — any unhealthy tick resets
//     it, so a crash-looping stream never accumulates the window;
//   - BOUNDED: at most maxAutoRecoveries per session — after that only the user's manual
//     exitSafeMode() clears it. The genuine crash-loop latch is preserved.
class SafeModeRecoveryTracker final {
public:
    void configure(qint64 stabilityWindowMs, int maxAutoRecoveries) noexcept
    {
        stabilityWindowMs_ = stabilityWindowMs > 0 ? stabilityWindowMs : 1;
        maxAutoRecoveries_ = maxAutoRecoveries < 0 ? 0 : maxAutoRecoveries;
    }
    // Safe mode just latched: any previously-accumulated health run is stale.
    void onEnterSafeMode() noexcept { stableSinceMs_ = -1; }
    // One observation per watchdog tick while safe mode is ACTIVE. Returns true when the
    // controller should auto-exit safe mode NOW; the true return consumes one bounded
    // auto-recovery. nowMs is any monotonic-enough wall/engine clock in ms.
    [[nodiscard]] bool observe(bool streamHealthy, qint64 nowMs) noexcept
    {
        if (!streamHealthy) {
            stableSinceMs_ = -1;                    // reset the continuous-health run
            return false;
        }
        if (stableSinceMs_ < 0) {
            stableSinceMs_ = nowMs;                 // health run starts this tick
            return false;
        }
        if (nowMs - stableSinceMs_ < stabilityWindowMs_) {
            return false;                           // still inside the stability window
        }
        if (autoRecoveries_ >= maxAutoRecoveries_) {
            return false;                           // budget spent -> manual reset only
        }
        ++autoRecoveries_;
        stableSinceMs_ = -1;
        return true;
    }
    [[nodiscard]] int autoRecoveries() const noexcept { return autoRecoveries_; }
    [[nodiscard]] int maxAutoRecoveries() const noexcept { return maxAutoRecoveries_; }

private:
    qint64 stabilityWindowMs_ = 20000;   // 20s of continuous health before auto-exit
    int maxAutoRecoveries_ = 2;          // per session; beyond this = manual only
    qint64 stableSinceMs_ = -1;          // first ms of the current continuous-health run
    int autoRecoveries_ = 0;
};

class ORION_AUTOMATION_API AutomationEngine final : public QObject {
    Q_OBJECT
    friend class ::AutomationEngineTests;
public:
    explicit AutomationEngine(QObject* parent = nullptr);

    void applyConfig(const AppConfigData& settings, const LearningData& learning);
    void updateDetection(const DetectionResult& result);
    // [ORION_METER_DELAY_LEAD_STARVATION 2026-08-08] The APPLIED inbound meter delay
    // (MeterDelayController::currentDelayMs()), pushed by OrionAppController on every
    // delay-state publish. The engine consumes it as a diagnosis/refusal input ONLY —
    // it never becomes a clock, an offset, a lead, or a tip estimate (the same fence
    // MeterDelayController.h documents for every value the delay actuator produces).
    // 0 (the default) keeps every path bit-identical to the pre-delay engine.
    //
    // WHY THE ENGINE NEEDS IT: the shipped tip predictors are ALL dated from the
    // captured video (the phase member's anchor is the observed fill crossing —
    // notePhaseAnchorSample; the sampler/registration members extrapolate observed
    // fills). An inbound delay D shifts the RENDERED meter D later than the judged
    // animation, so a correct Shot Lead grows to loop+D while the visible runway from
    // any witnessable anchor stays == the tip constant. Once lead > usable max, no
    // in-video evidence can produce a schedulable, accurate deadline: the phase member
    // is born with its command deadline already past (2026-08-08 23:07:26 live:
    // tip_eta 328.054, command_eta -211.946 at lead 540), and the only estimates whose
    // horizon clears the lead are pre-anchor long-horizon extrapolations — the
    // sampler's single worst regime (the two 23:07/23:08 "squeakers" armed at fill
    // 13-18 and landed ~300ms early on the visible meter).
    void setMeterDelayCondition(double appliedMs, bool settled) noexcept
    {
        meterDelayAppliedMs_ = std::isfinite(appliedMs) ? std::max(0.0, appliedMs) : 0.0;
        meterDelaySettled_ = settled;
    }
    [[nodiscard]] double meterDelayAppliedMsForTests() const noexcept
    {
        return meterDelayAppliedMs_;
    }
    // [ORION_TEMPO_BRIDGE_LIVE 2026-08-08 task #36] Bridge-live inputs for the Tempo remap
    // gate. The Square->Tempo remap EATS the physical Square press and substitutes the
    // right-stick gesture; when the packet-bridge intercept behind the meter delay is not
    // actually live (service missing, sniff-only debug bridge, dead backend) that substitution
    // buys nothing and costs the user every press ("can't press square for anything" —
    // owner-reported). The remap therefore engages on a NEW press only while the bridge is
    // provably live:
    //     (delay engine Locked || applied delay > 0)          [the delay engine is commanding]
    //  && intercept applying (the service's OWN armed echo)   [the backend confirms holding]
    //  && a backend health beat within kTempoRemapBridgeHealthFreshMs
    // Both setters WIRE the gate: an engine that never receives them (standalone/unit use)
    // keeps the legacy toggle-only behavior bit-identical, exactly like the default-armed
    // idiom. OrionAppController wires the gate at construction, before the first input poll.
    // Fail direction is passthrough-only: a not-live verdict demotes the press to the plain
    // Button representation (the press reaches the console); it can never eat a press.
    void setTempoRemapBridgeState(bool delayEngineEngaged, bool interceptApplying,
                                  const QString& delayStateLabel) noexcept;
    void noteTempoRemapBridgeHealthBeat() noexcept;
    // Synchronous controller-edge fence. The controller calls this before process();
    // delayed sidecar proof from any smaller epoch cannot own or release an auto probe.
    void setPhysicalShotEpoch(quint64 epoch) noexcept;
    // Publish the live capture tier ("capture_card" / "decoder") so a packaged factory prior's
    // VIDEO half can be re-verified natively. Until this is called the video half is unverified
    // and only the controller half is enforced, exactly as before.
    void setLatencyVideoRoute(const QString& videoRoute);
    // Sub-tick decision-latency win (~2-4ms): call RIGHT AFTER updateDetection on a fresh meter
    // sample. Re-runs ONLY predictCrossingMs + the fire SCHEDULE from the already-updated meter and
    // the latched last physical state, WITHOUT advancing the 4ms tracking-history / tempo / tap
    // cadence, so a fresh meter can move an already-armed scheduled fire deadline EARLIER (or arm
    // one this instant) instead of waiting up to a full inputPollTimer_ tick. RESCHEDULE-ONLY and
    // bounded: never fires in-tick, never triggers a safety/abort/commit, and only ever moves the
    // deadline EARLIER — the 4ms tick's processHolding stays authoritative for every other decision.
    void reevaluateScheduleOnFreshSample();
    // No-meter pose landmark from the Python sidecar. kind = "push" or "release".
    // The engine latches the first push/release per shot and schedules the release
    // using its own clock. frame_seq is for staleness rejection only.
    void updatePoseLandmark(const QString& kind, int frameSeq, double confidence,
                            quint64 armToken);
    void updateNetworkOffset(double offsetMs);
    // Network offset + measured jitter (ms). Offset is sample-and-held per shot; the
    // jitter EMA auto-engages wifi mode (tighter clamps, stricter freshness gate).
    void updateNetworkQuality(double offsetMs, double jitterMs);
    // TEST/DIAGNOSTIC verdict-injection hook (NOT fed by any live HUD — the on-screen
    // TIMING-banner reader was removed; live calibration ground truth is the bot's own
    // post-release shot meter via evaluatePostReleaseMeter). Injects an EARLY/LATE/
    // EXCELLENT verdict + signed residual (errorMsHint: + late, - early, 0 on-target; NaN
    // if unusable), pairs it to the most recent release, and runs it through learnFromOutcome
    // — used by the unit tests to exercise the calibration controller directly.
    void updateShotOutcome(const QString& verdict, double errorMsHint, double confidence);
    // Authoritative calibration oracle (Layer C): the on-screen shot-FEEDBACK-TEXT reader
    // ("Excellent/Slightly Early/Late") delivers a SIGNED residual (+late, -early, 0 on-target).
    // Unlike the meter self-grade it is reliable at the dead-top, so in calibrationMode it is
    // allowed to teach learnFromOutcome even while calibrationFrozen. Pairs to the last release.
    void setShotVerdict(const QString& verdict, double signedResidualMs, double confidence);
    // [ORION_TICK_LOCK] Pure phase-alignment math (static so it is unit-testable headlessly): nudge an
    // absolute fire deadline so the release ARRIVES mid-tick on the 60Hz console grid. nextTickEtaMs is
    // the surfaced eta from nowMs to the next console tick edge; ticks repeat every tickIntervalMs. The
    // returned deadline is the smallest-magnitude nudge (within ±tickIntervalMs/2) that lands the arrival
    // at phase tickIntervalMs/2 (mid-tick). A non-positive tickIntervalMs or nextTickEtaMs returns the
    // deadline unchanged (telemetry not ready).
    [[nodiscard]] static double tickAlignedFireDeadlineMs(double deadlineMs, double nowMs,
                                                          double nextTickEtaMs, double tickIntervalMs);
    // [Phase-2 A2(c)] EARLIER-ONLY console-tick snap for the FUSED fire deadline (static = unit-
    // testable headlessly). Returns the LATEST t <= tFireMs whose press-submit instant sits at
    // engine-clock phase (edgePhaseEngineMs - epsilonMs) mod tickIntervalMs — i.e. the press
    // ARRIVES epsilon before an input-tick edge. NEVER later than the unsnapped deadline (the
    // legacy tickAlignedFireDeadlineMs centers mid-tick and can move +8.3ms late — untouched).
    // Non-positive interval / non-finite inputs return the deadline unchanged.
    [[nodiscard]] static double earlierOnlyTickSnapMs(double tFireMs, double edgePhaseEngineMs,
                                                      double epsilonMs, double tickIntervalMs);
    // [Phase-2 A2(c)] snap engage gate: probe-run phase telemetry present AND trustworthy
    // (conf >= minConf, 0 < sd <= maxSd). Disengaged == byte-identical fused deadline.
    [[nodiscard]] static bool tickSnapEngaged(double conf, double sdMs,
                                              double minConf, double maxSdMs);
    // [ORION_PRIOR_POSTERIOR] C2: inverse-variance fuse of the clock PRIOR (clockDueMs, variance
    // priorSd^2) and the vision POSTERIOR (visionFireAtMs = crossing - effectiveLatency, variance grows
    // from visionBaseSd^2 at full confidence to visionMaxSd^2 at confidence 0). Pure math (static) so the
    // convergence properties are unit-testable directly. Guarantees: visionUsable==false OR confidence 0
    // returns clockDueMs EXACTLY (never degrades below the pure clock); as visionBaseSd/noise -> 0 with
    // full confidence it converges to visionFireAtMs; otherwise the fuse lies between the two.
    [[nodiscard]] static double blendedFireDeadlineMs(double clockDueMs, double visionFireAtMs,
                                                      bool visionUsable, double confidence,
                                                      double priorSdMs, double visionBaseSdMs,
                                                      double visionMaxSdMs);
    // P0.1: a release that never reached the virtual pad (preview / virtual-disconnected) must NOT be
    // graded — the meter that rises afterwards is the human's shot, not this bot's release, so grading it
    // would poison the learner and let the overlay "grade a shot that never fired". The controller calls
    // this with the un-submitted release seq to drop the open post-release capture. No-op if the seq
    // doesn't match the active capture (a later shot already opened its own).
    void cancelPostReleaseGrade(int releaseSeq);
    ControllerState process(const ControllerState& physical);
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] process() is now a thin wrapper: it runs the state
    // machine (processInternal) and then applies the passthrough to the FINAL output. Deliberately
    // NOT folded into the state machine -- processInternal has FIVE return points and the remap
    // clears Square at 21 sites, so any earlier placement can be silently undone downstream.
    ControllerState processInternal(const ControllerState& physical);
    [[nodiscard]] uint16_t squarePassthroughBit() const noexcept;
    void applySquarePassthrough(ControllerState& output,
                                const ControllerState& physical) noexcept;
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] True when the LAST process() injected Square from the
    // stick click rather than the shot path producing it.
    //
    // updateReleaseOwnershipTrace() reads output.square() to prove Orion held the output Square at
    // 0 for a whole pulse+cooldown, and its own comment calls any 0 there "a real engine override
    // bug". A steal pressed during a live shot's Releasing window sets that bit and manufactures
    // exactly the signature that branch exists to hunt -- poisoning the diagnostic we tune shot
    // timing with. The trace subtracts THIS rather than recomputing the gate condition, so the two
    // can never drift apart.
    [[nodiscard]] bool squarePassthroughInjectedLastTick() const noexcept
    {
        return squarePassthroughInjected_;
    }
    // Tempo's left-stick intent is committed as its own ordered controller
    // packet before any generated RS-down gather may be emitted. The controller
    // uses these read-only transaction facts to protect that packet from defense
    // shaping and to request an exact acknowledgement from the active route.
    [[nodiscard]] bool tempoMovementOwned() const noexcept;
    [[nodiscard]] bool tempoMovementCommitPending() const noexcept;
    [[nodiscard]] quint64 tempoMovementCommitGeneration() const noexcept;
    void confirmTempoMovementCommit(quint64 generation, bool accepted);
    void reset();
    // User-driven per-type calibration (launcher Calibrate/Lock panel). Recalibrate drops a type back
    // to ACQUIRE (re-converge); Lock freezes its dialed clock (micro-trim only). Both persist via
    // calPhaseUpdated.
    void recalibrateShotType(const QString& type);
    void lockShotType(const QString& type);
    // Autonomous recalibration trigger: drop EVERY type back to ACQUIRE (clock kept as a
    // warm start). Fired silently on meter style/color change and stream restart so a
    // changed meter look / fresh session re-converges without user action.
    void recalibrateAllShotTypes();

    // --- Sub-tick scheduler interop (controller-owned precise fire thread) ------------
    // Engine exposes a pending deadline; the controller arms its fire thread with it and,
    // after the thread submits the release output at the deadline, confirms back with the
    // actual fire time. Token pairs an arm with its confirm so a stale confirm is ignored.
    // Read-only value snapshot of the autonomous tip reservation, for HUD/telemetry.
    //
    // `state` is the live disposition, using the same vocabulary the abort diagnostic uses so an
    // operator watching the overlay and an operator reading the log see the same words:
    //   reserved      - tracking a command deadline that is still in the future
    //   reserved_late - the deadline is already behind the clock; the LEAD is wrong for this
    //                   shot's geometry and this shot will fail closed (unschedulable_lead)
    //   promoted      - a precise-fire token has been armed from this plan
    // Empty state / active=false means no plan is held and the HUD hides the row.
    struct TipReservationView {
        bool active = false;
        QString state;
        int updates = -1;
    };
    [[nodiscard]] TipReservationView tipReservationView() const noexcept;

    [[nodiscard]] double scheduledFireDeadlineMs() const noexcept { return schedFireDeadlineMs_; }
    [[nodiscard]] quint64 scheduledFireToken() const noexcept { return schedFireToken_; }
    [[nodiscard]] quint64 scheduledFireRouteGeneration() const noexcept
    {
        return schedFireRouteGeneration_;
    }
    [[nodiscard]] LatencyControllerRoute scheduledFireRoute() const noexcept
    {
        return schedFireRoute_;
    }
    [[nodiscard]] bool scheduledFireRouteBindingMatches(
        quint64 generation, LatencyControllerRoute route) const noexcept
    {
        // This helper is called by the TIME_CRITICAL worker. The GUI-owned schedule
        // fields are intentionally not read here: doing so would race deadline
        // rescheduling. The immutable token mailbox already binds those values; the
        // only cross-thread authority read is the lock-free live route attestation.
        return controllerDeliveryRouteAttestationExpected(generation, route);
    }
    [[nodiscard]] double scheduledFireAuthorityExpiryMs() const noexcept
    {
        // The worker takes ONE immutable snapshot of this at arm time and refuses to submit past
        // it. [ORION_ROLLING_LEASE] An early-armed meter token is deliberately allowed to hold a
        // deadline the arming frame's evidence does not yet cover -- a later frame will cover it --
        // so handing the worker the raw arm-time lease would make it silently drop exactly the
        // tokens this change exists to create.
        //
        // Give it a bound that still cannot produce a LATE release: the deadline itself. The
        // worker's role for meter tokens is the frozen-GUI backstop ("never fire after the instant
        // the engine committed to"), which this preserves exactly. Evidence freshness is enforced
        // by the engine's rolling fences, which run every 4 ms and linearize disarm against the
        // worker's submit under submitMutex_ -- a strictly live check where the snapshot was a
        // stale one. Pose and non-vision tokens keep the raw lease unchanged.
        if (schedFireDeadlineMs_ >= 0.0 && schedFireRequiresGenuineFrame_
            && schedFireAuthorityExpiryMs_ >= 0.0) {
            return std::max(schedFireAuthorityExpiryMs_, schedFireDeadlineMs_);
        }
        return schedFireAuthorityExpiryMs_;
    }
    void confirmScheduledFire(quint64 token, double actualFireMs);
    void rejectScheduledFire(quint64 token);
    void cancelScheduledFire(quint64 token);
    // [CONCURRENCY N1 / ORION_TICK_LOCK] Reconcile the engine's stored fire deadline with the
    // deadline the fire thread was ACTUALLY armed at. The tick-lock phase-align nudge is applied
    // at arm time in the controller, so the engine otherwise keeps the un-nudged deadline; when
    // the nudge pulls the fire EARLIER, the grace + earlier-only reschedule guards (which key off
    // schedFireDeadlineMs_) still see the later value and a fresh sample in the gap can re-arm the
    // shot -> a second physical press. Writing the armed deadline back keeps those guards honest.
    // No-op unless the token still matches the live schedule (a stale/cleared arm can't move it).
    // Only called when the nudge actually moved the deadline, so tick-lock OFF is byte-identical.
    void noteArmedFireDeadlineMs(quint64 token, double deadlineMs) noexcept
    {
        if (token != 0 && token == schedFireToken_ && schedFireDeadlineMs_ >= 0.0) {
            schedFireDeadlineMs_ = deadlineMs;
        }
    }
    // [ORION_PROBE] warmup pump-fake latency probes (engine Idle only; user-triggered).
    // Each probe presses virtual Square for probePressMs then releases, emitting probeMarker
    // at the press instant. Any physical Square press cancels the whole run.
    void startLatencyProbes(int count = 8);
    void cancelLatencyProbes() noexcept;
    [[nodiscard]] bool latencyProbesActive() const noexcept
    {
        // probeShootAtMs_ MUST be included: during the call->shoot wait no press is down and no
        // probe is queued, so without it the run would read as inactive, probeTick would stop
        // being called, and the pending shoot would never fire.
        return probesRemaining_ > 0 || probePressEndMs_ >= 0.0 || probeShootAtMs_ >= 0.0;
    }
    // [ORION_PROBE] Read-back for what a probe run actually produced. These are the two
    // quantities the fused rule and the earlier-only tick snap consume, and BOTH sit at their
    // "never measured" sentinels until a run completes: measuredLatencySdMs_ == 0 makes the
    // fused sigma fall back to a flat 12.0 (AutomationEngine.cpp sigmaL), and tickPhaseConf_
    // == 0 keeps tickPhaseAuthoritative() false so the snap is skipped and sigma_tick stays
    // at the unsnapped 4.8. Neither fallback is logged as an assumption anywhere, so without
    // this read-back a factory prior is indistinguishable from a real measurement.
    [[nodiscard]] double measuredLatencyMs() const noexcept { return measuredLatencyMs_; }
    [[nodiscard]] double measuredLatencySdMs() const noexcept { return measuredLatencySdMs_; }
    [[nodiscard]] int measuredLatencyN() const noexcept { return measuredLatencyN_; }
    [[nodiscard]] double tickPhaseConf() const noexcept { return tickPhaseConf_; }
    [[nodiscard]] double tickPhaseSdMs() const noexcept { return tickPhaseSdMs_; }
    // Live-only latency calibration. While armed, supported metered shots are owned solely
    // to produce release/effect labels until the current estimator epoch reaches
    // production authority. This is separate from the legacy press->appearance
    // warmup probes above, which depend on a calibrated spawn offset.
    void setLatencyCalibrationMode(bool enabled);
    // Zero-click mode differs from explicit calibration: until a strict current-shot rising
    // meter episode exists, the shot input remains physical and no ShotContext may be owned.
    void setAutomaticLatencyCalibrationMode(bool enabled);
    [[nodiscard]] bool latencyCalibrationMode() const noexcept
    {
        return latencyCalibrationMode_;
    }
    [[nodiscard]] bool automaticLatencyCalibrationMode() const noexcept
    {
        return latencyCalibrationMode_ && latencyCalibrationAutomatic_;
    }
    [[nodiscard]] bool autonomousLiveMeterReady() const noexcept;
    // "Has this machine actually been MEASURED", as opposed to "may the bot shoot". A packaged
    // factory prior satisfies the latter but not the former, and conflating the two deadlocked
    // self-calibration permanently. Only this predicate may retire calibration mode.
    [[nodiscard]] bool measuredLeadValidatedAuthority() const noexcept;
    [[nodiscard]] int measuredLatencySampleCount() const noexcept { return measuredLatencyN_; }
    [[nodiscard]] double measuredLatencyValueMs() const noexcept { return measuredLatencyMs_; }
    [[nodiscard]] double measuredLatencyPosteriorSdMs() const noexcept
    {
        return measuredLatencySdMs_;
    }
    [[nodiscard]] QString measuredLatencyAuthorityKind() const
    {
        return measuredLatencyAuthorityKind_;
    }
    [[nodiscard]] double measuredLatencyAuthorityValueMs() const noexcept
    {
        return measuredLatencyAuthorityMs_;
    }
    [[nodiscard]] double measuredLatencyAuthoritySdMs() const noexcept
    {
        return measuredLatencyAuthoritySdMs_;
    }
    [[nodiscard]] bool restoredLatencyAwaitingControllerAttestation() const noexcept
    {
        const auto expected = controllerDeliveryRouteAttestationSnapshot();
        return measuredLeadTelemetryPresent_ && measuredLatencyRestored_
            && measuredLatencyVideoRouteAttested_
            && (measuredLatencyScopeEpoch_ == 0 || !expected.valid()
                || measuredLatencyAttestationGeneration_ != expected.generation
                || measuredLatencyDeliveryRoute_ != expected.route);
    }
    struct ControllerDeliveryRouteAttestationSnapshot final {
        quint64 generation = 0;
        LatencyControllerRoute route = LatencyControllerRoute::None;

        [[nodiscard]] bool valid() const noexcept
        {
            return generation != 0 && route != LatencyControllerRoute::None;
        }
    };
    // Generation and route form one authorization token. Route occupies the low two bits
    // and generation the remaining 62 bits, so publication is one lock-free store on the
    // supported x64 build. This is also the watchdog/WM_CLOSE boundary: it may never spin
    // behind a frozen GUI writer.
    [[nodiscard]] ControllerDeliveryRouteAttestationSnapshot
    controllerDeliveryRouteAttestationSnapshot() const noexcept
    {
        if (!armed_.load(std::memory_order_acquire)) {
            return {};
        }
        const quint64 packed =
            controllerDeliveryRouteAttestationPacked_.load(std::memory_order_acquire);
        if (!armed_.load(std::memory_order_acquire)) {
            return {};
        }
        const auto route = static_cast<LatencyControllerRoute>(packed & 0x3ULL);
        const quint64 generation = packed >> 2;
        if ((generation == 0) != (route == LatencyControllerRoute::None)) {
            return {};
        }
        return {generation, route};
    }
    [[nodiscard]] bool controllerDeliveryRouteAttestationExpected(
        quint64 generation, LatencyControllerRoute route) const noexcept
    {
        if (generation == 0 || route == LatencyControllerRoute::None) {
            return false;
        }
        const auto expected = controllerDeliveryRouteAttestationSnapshot();
        return expected.generation == generation && expected.route == route;
    }
    [[nodiscard]] bool restoredLatencyAttestationEchoMatches(
        quint64 generation, LatencyControllerRoute route,
        quint64 scopeEpoch) const noexcept
    {
        return generation != 0 && route != LatencyControllerRoute::None
            && scopeEpoch != 0 && measuredLatencyScopeEpoch_ == scopeEpoch
            && measuredLeadTelemetryPresent_ && measuredLatencyRestored_
            && measuredLatencyVideoRouteAttested_
            && measuredLatencyAttestationGeneration_ == generation
            && measuredLatencyDeliveryRoute_ == route;
    }
    void setControllerDeliveryRouteAttestation(
        quint64 generation, LatencyControllerRoute route);
    // Establish a fresh namespace for timing state owned by a newly-started
    // sidecar process. This revokes copied release work and clears the old
    // process's estimator/scope epoch without changing shot configuration,
    // learned clocks, or the native handshake's monotonic generation.
    void beginSidecarProcessGeneration();
    // OrionAppController enables this once at construction. Keeping the core
    // default false preserves isolated/offline timing-model tests that have no
    // controller transport, while every shipped GUI release fails closed unless
    // it carries an exact route proof.
    void setControllerRouteBindingRequired(bool required) noexcept
    {
        controllerRouteBindingRequired_ = required;
    }
    // The engine's monotonic clock (same timebase as every deadline it exposes).
    [[nodiscard]] double engineNowMs() const { return nowMs(); }
    // Ceiling-stack read-only accessors (tests + telemetry; both objects are inert while their
    // flags are off, so exposing them changes no behavior).
    [[nodiscard]] const TemplateArrivalEstimator& templateArrivalEstimator() const noexcept
    {
        return templateArrival_;
    }
    [[nodiscard]] const BanditLeadTuner& banditLeadTuner() const noexcept { return banditTuner_; }
    // TEST-ONLY deterministic clock: once advanced, nowMs() returns this injected value instead of
    // the real QElapsedTimer, so unit tests advance time INSTANTLY (no real qWait sleeps) and stay
    // deterministic. NEVER called in production, and testClockMs_ defaults < 0, so the live path uses
    // the real clock unchanged. The first call captures the current real time so the mock stays
    // monotonic with any earlier real-clock reads in the same test.
    void advanceTestClock(double deltaMs);
    // TEST-ONLY: install the [ORION_TIP_PHASE] tuning and the sampler-de-bias flag directly.
    // Neither has settings.json plumbing (the shipped values ARE the defaults), so the suite
    // otherwise has no way to exercise a non-default anchor/constant or the double-count guard.
    // Call BEFORE applyConfig when the guard is under test -- applyConfig assigns fields
    // individually and never resets config_, so values set here survive it. Production never
    // calls this.
    void setTipPhaseConfigForTests(bool enabled, double anchorPct, double constantMs,
                                   double seedPhysicalMs, double sigmaMs,
                                   bool samplerHorizonDebias = false)
    {
        config_.tipPhaseEnabled = enabled;
        config_.tipPhaseAnchorPct = anchorPct;
        config_.tipPhaseConstantMs = constantMs;
        config_.tipPhaseSeedPhysicalMs = seedPhysicalMs;
        config_.tipPhaseSigmaMs = sigmaMs;
        config_.samplerHorizonDebiasEnabled = samplerHorizonDebias;
    }
    // TEST-ONLY read-back of the learned PHYSICAL term (-1 = window not yet full) and of the
    // constant the decision path actually consumes.
    [[nodiscard]] double learnedPhasePhysicalMsForTests() const noexcept
    {
        return learnedPhasePhysicalMs_;
    }
    [[nodiscard]] double tipPhaseLevelAdjustmentMsForTests(double levelPct) const noexcept
    {
        return tipPhaseLevelAdjustmentMs(levelPct);
    }
    [[nodiscard]] double tipPhaseTypeTrimMsForTests(const QString& shotType) const noexcept
    {
        return tipPhaseTypeTrimMs(shotType);
    }
    // [ORION_PHASE_PORTABILITY] How many landings the learner has actually TAKEN, so a test can
    // distinguish "recorded in the log" from "learned from" -- the whole point of emitting a
    // rejected sample is that those two stop being the same thing.
    [[nodiscard]] int phaseConstantSampleCountForTests() const noexcept
    {
        return phaseConstantSamplesMs_.size();
    }
    [[nodiscard]] bool phaseAnchorImminentForTests(double fillPct,
                                                   double slopePctPerMs) const noexcept
    {
        return phaseAnchorImminent(fillPct, slopePctPerMs);
    }
    [[nodiscard]] double effectiveTipPhaseConstantMsForTests() const noexcept
    {
        return effectiveTipPhaseConstantMs();
    }
    [[nodiscard]] double phasePriorShiftMsForTests() const noexcept
    {
        return phasePriorShiftMs();
    }
    // [ORION_SLOW_METER_DEFER] TEST-ONLY replay of an arm-time snapshot through the real
    // predicate: phase tip eta and candidate command eta exactly as the TIP RESERVATION log
    // line carries them (phase_tip_eta_ms, command_eta_ms, lead_ms). Constructs the minimal
    // decision the predicate reads -- a present, recently-anchored, horizon-plausible phase
    // member -- so the blast-radius census of the 2026-08-06 sessions can be replayed
    // number-for-number against the shipped predicate. Production never calls this.
    [[nodiscard]] bool slowMeterDeferBindsForTests(double phaseTipEtaMs,
                                                   double commandEtaMs,
                                                   double leadMs) const noexcept
    {
        AutonomousTipDecision decision;
        const double now = std::max(1000.0, nowMs());
        decision.phaseAnchorMs = std::max(0.0, now - 60.0);
        decision.phaseTipAbsMs = now + phaseTipEtaMs;
        decision.phaseSigmaMs = config_.tipPhaseSigmaMs;
        return slowMeterDeferBinds(decision, now + commandEtaMs, leadMs, now);
    }
    [[nodiscard]] bool wifiModeActive() const noexcept { return wifiMode_; }
    // Defense Mode: disarm shot automation entirely (inputs pass through untouched).
    // A revocation generation closes the false->true-between-controller-ticks hole:
    // detector callbacks cannot promote proof captured before any disarm, and the
    // next engine-thread process() fences/cancels that physical epoch even if the
    // watchdog has already re-armed by then.
    void setArmed(bool armed) noexcept
    {
        if (!armed) {
            armed_.store(false, std::memory_order_release);
            armRevocationGeneration_.fetch_add(1, std::memory_order_acq_rel);
            publishControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
            return;
        }
        const bool wasArmed = armed_.exchange(true, std::memory_order_acq_rel);
        if (!wasArmed) {
            // A re-arm cannot expose the previous route proof. The controller must
            // complete a fresh neutral-route handshake for the new arm epoch.
            publishControllerDeliveryRouteAttestation(
                0, LatencyControllerRoute::None);
        }
    }
    [[nodiscard]] bool armed() const noexcept
    {
        return armed_.load(std::memory_order_acquire);
    }
    [[nodiscard]] QMap<QString, double> rttBaselineByType() const { return config_.shotTypeRttBaselineMs; }
    [[nodiscard]] QMap<QString, double> velocityPriorByType() const { return config_.shotTypeVelocityPriorPctMs; }

    [[nodiscard]] ShotContext context() const { return shot_; }
    [[nodiscard]] RemapConfig config() const { return config_; }
    // Per-type consecutive-green count (calibration progress for the launcher panel).
    [[nodiscard]] QMap<QString, int> calGreensByType() const { return calGreens_; }
    // Which ground truth is currently grading shots: the TIMING banner, or the
    // post-release meter self-grade after the banner went silent (auto-fallback).
    [[nodiscard]] QString activeGraderName() const
    {
        if (!config_.bannerCalibration)
            return QStringLiteral("Meter");
        return bannerUnclearStreak_ >= config_.bannerUnclearFallbackShots
            ? QStringLiteral("Meter (banner silent)")
            : QStringLiteral("Banner");
    }
    [[nodiscard]] int shotsAttempted() const noexcept { return shotsAttempted_; }
    [[nodiscard]] int shotsReleased() const noexcept { return shotsReleased_; }
    [[nodiscard]] int shotsAborted() const noexcept { return shotsAborted_; }

signals:
    void shotStateChanged(orion::ShotContext context);
    // Synchronous invalidation fence for a live-vision precise-fire token. The
    // controller disarms its copied deadline before this signal returns; if the
    // fire thread already won the serialized submit, it confirms the token here.
    void visionScheduleInvalidating(quint64 token);
    // Grace-expiry takeover is distinct from detector/authority invalidation.
    // The controller may retire a matching worker failure only here because
    // this path immediately retries the identical release on the GUI route.
    void scheduledFireFallbackInvalidating(quint64 token);
    // UNCONDITIONAL shot-armed notification, emitted exactly once from beginShot for EVERY
    // mode/config. The reader's whole "armed" tier (relaxed acquire, armed coast, the armed
    // confidence floor, colour calibration) and lab pose search are both driven by the one
    // RemotePlaySession::armPose() command. armToken binds every returned pose landmark to
    // this shot; a second no-meter-only arm signal would create two epochs for one shot.
    void shotArmed(orion::ShotMode mode, QString shotType, quint64 armToken);
    void releaseIssued(orion::ShotContext context);
    // RC-3: emitted at the release-submit site with the release seq and the wall-clock EPOCH ms of the
    // press. OrionAppController relays it to RemotePlaySession::sendReleaseMarker -> the sidecar's
    // frozen-meter latency oracle. Epoch (not the engine's monotonic clock) so it shares the sidecar's
    // fill-sample timebase. Mirrors the shotArmed -> armPose stdin-send wiring.
    void releaseMarker(int seq, double wallMsEpoch, bool latencyCalibration,
                       double validationTargetPct, double validationTolerancePct,
                       quint64 physicalShotEpoch, quint64 shotAttempt);
    // Truth-only status for a later controller/QML binding. Emitted on explicit
    // start/cancel, accepted estimator changes, freshness transitions, and ready.
    void latencyCalibrationStatusChanged(bool active, bool ready, int acceptedSamples,
                                         double measuredLeadMs, double posteriorSdMs);
    // RC-3/C3: a feedforward clock fire was SUPPRESSED because the current frame had no valid detection
    // and the fire was off-schedule (a drifted clock landing on a dead frame). This signal is retained
    // for legacy diagnostics; the central authority gate now rejects every non-genuine current frame.
    void blindFireSuppressed(QString reason);
    // B2c/C5: a plain diagnostic line from the engine (the engine never logs directly — it is on
    // the real-time path — so OrionAppController relays these to the session log). Currently used
    // for IDLE arm-gate reason TRANSITIONS, so a "the bot stopped arming" report is greppable.
    void engineDiagnostic(QString line);
    // [ORION_FUSED_FIRE] diagnostic lines from the fused-posterior stack (anchor learning,
    // per-shot FusedShadow verdicts). Log-only; the engine never logs directly (real-time path).
    void fusedDiagnostic(QString line);
    // [ORION_PROBE] a warmup pump-fake probe PRESS was just injected: seq + epoch ms (same
    // clock as releaseMarker) + the configured spawn offset. Relayed to the sidecar as the
    // probe_marker command so the estimator can close it on the meter-appear that follows.
    void probeMarker(int seq, double wallMsEpoch, double spawnOffsetMs);
    // [ORION_FUSED_FIRE] v4 learning fields to persist: the per-bucket appear->tip anchor
    // clocks (posthoc-taught) + the one-shot lead re-baseline latch.
    void fusedLearningUpdated(QMap<QString, double> shotTypeAppearToTipMs, bool leadRebaselined);
    void shotAborted(QString reason);
    void learningUpdated(QMap<QString, double> shotTypeLearnedOffsetMs);
    // [ORION_PHASE_COLD_START] Emitted when the learner moves the physical animation constant, so
    // it can be persisted and the cold start paid once per jumpshot rather than once per launch.
    void phaseConstantUpdated(double learnedPhysicalMs);
    // [ORION_AIM_FREEZE] What the phase instrument MEASURED (full-window median, canonical
    // base-30), emitted alongside phaseConstantUpdated. While tip_phase_aim_frozen holds, the
    // signal above deliberately re-persists the manual value (the Tip Timing card's contract),
    // which used to mean a frozen session discarded its own measurement entirely; this one keeps
    // the measurement visible across restarts (learning.json measured_phase_physical_ms) so the
    // restore path can warn when the manual value disagrees with the rig's own instrument.
    // Persistence-only: nothing on the decision path consumes it.
    void phaseMeasuredMedianUpdated(double measuredPhysicalMs);
    // [ORION_LEAD_CONFLICT] The engine has established -- by config arithmetic at apply time, or
    // by a live missed deadline -- that the active Shot Lead cannot be scheduled against the
    // active tip-timing constant: the phase member's command deadline is already behind `now` on
    // the first tick it can ever be computed (2026-08-08 live log: Tip Timing effective 314 ms +
    // Shot Lead 300/315/320 ms -> every phase-source decision rejected_missed with
    // lateness == lead - tip_eta). Emitted so the app layer can surface a persistent, actionable
    // warning; the engine itself only logs. maxUsableLeadMs is the largest lead the current tip
    // constant can schedule (decision-latency margin already subtracted). missesThisSession
    // counts shots aborted live_tip_deadline_missed while the conflict held. Diagnostic-only:
    // nothing about scheduling or the fail-closed abort changes.
    void shotLeadConflictDiagnosed(double leadMs, double maxUsableLeadMs,
                                   int missesThisSession);
    // [ORION_LEAD_CONFLICT] A VALIDATED latency posterior disagrees with the in-band Shot Lead
    // that replaces it by more than 3*sd (2026-08-08: validated 197.1 sd=3.2 vs user 320 --
    // 123 ms apart -- and nothing surfaced it). Advisory: consumers may badge/log; nothing
    // adopts the measured value on the user's behalf.
    void leadAuthorityDisagreementDiagnosed(double leadMs, double authorityMs,
                                            double authoritySdMs, int authorityN);
    // Emitted when the per-shot-type feedforward animation clock is updated, so it can
    // be persisted to learning.json (survives restarts, like the learned offset).
    void feedforwardUpdated(QMap<QString, double> shotTypeFeedforwardMs);
    // Emitted when the per-shot-type meter-appear->release clock is updated, persisted to
    // learning.json (the meter_appear anchor's clock; mirrors feedforwardUpdated).
    void meterClockUpdated(QMap<QString, double> shotTypeMeterToReleaseMs);
    // Emitted when a per-type calibration phase transitions (Acquire<->Lock), persisted so a
    // dialed-in type restarts LOCKED instead of re-hunting a converged baseline.
    void calPhaseUpdated(QMap<QString, int> shotTypeCalPhase);
    // [ORION_PRESS_ANCHOR] Emitted after each ACCEPTED press->tip observation (H weight 1.0,
    // M weight 0.5) with the full per-type calibration snapshot — learned wall-time
    // press->real-tip median, MAD-derived sigma, accumulated weight. Persisted to
    // settings.json (press_anchored_tip_ms / _sigma_ms / _n) so the Aug 9-17 collection
    // accumulates across restarts and the Phase-B predictor can be armed on real data.
    void pressAnchoredCalibrationUpdated(QMap<QString, double> tipMs,
                                         QMap<QString, double> sigmaMs,
                                         QMap<QString, double> weight);
    // Persisted alongside the clocks: the network-offset baseline captured at LOCK
    // (delta-compensation reference) and the per-type velocity prior.
    void rttBaselineUpdated(QMap<QString, double> shotTypeRttBaselineMs);
    void velocityPriorUpdated(QMap<QString, double> shotTypeVelocityPriorPctMs);
    // Hybrid global phase-clock self-learned globals (autonomous_vision path), persisted to
    // learning.json. Emitted whenever any of the three is nudged by a clean, vision-timed shot.
    void globalTimingLearned(double globalAppearToTipMs, double globalHoldToReleaseMs,
                             double learnedLatencyMs, double globalRiseVelocityPctMs);
    // Emitted when a real HUD verdict was attributed to a shot and the per-type
    // learned offset updated. For telemetry (the "Shot outcome:" log line). Carries the
    // bucket's full calibration state so a batch shows the annealing + feedforward
    // converging per shot type: learnedOffsetMs (current per-type offset),
    // feedforwardMs (the learned animation clock), learnCount (annealing progress).
    void shotOutcomeLearned(int seq, QString shotType, QString verdict, double errorMs,
                            double learnedOffsetMs, double feedforwardMs, int learnCount,
                            QString calPhase, int calGreens, int calMisses,
                            double meterClockMs, QString anchor);
    // [ORION_USER_LEAD 2026-08-08] this install's end-to-end lead as measured by the
    // marker-anchored VALIDATED estimator authority (value/sd/n straight from the accepted
    // telemetry tuple; emitted with 1 ms hysteresis from the ingestion site). It previously
    // carried the landing travel_pp/velocity median -- REFUTED as a lead instrument (the release
    // does not move the meter; the quantity is capture-side and circular) and observed live
    // drifting 225 -> 300 in seven minutes while captioned "measured on your setup". Pure
    // measurement with NO policy: the controller decides whether to seed the user-facing Shot
    // Lead from it (only when the user has never touched it) or merely display it.
    void actuationLeadMeasured(double medianLeadMs, int samples);
    // Detector-only release-window diagnostic. Emitted only for a non-proxy record whose immutable
    // native release id matches the latest submitted release. This must never be interpreted as a
    // game outcome or feed session accuracy/gameplay learning/bandit tuning.
    void releaseWindowDiagnostic(int releaseSeq, QString shotType, int label,
                                 double fillAtRelease, double greenStartPct);

private:
    void publishControllerDeliveryRouteAttestation(
        quint64 generation, LatencyControllerRoute route) noexcept
    {
        constexpr quint64 kMaximumPackedGeneration =
            std::numeric_limits<quint64>::max() >> 2;
        const quint64 routeBits = static_cast<quint64>(route);
        if (generation == 0 || generation > kMaximumPackedGeneration
            || routeBits == 0 || routeBits > 3) {
            controllerDeliveryRouteAttestationPacked_.store(0, std::memory_order_release);
            return;
        }

        const quint64 armEpoch =
            armRevocationGeneration_.load(std::memory_order_acquire);
        if (!armed_.load(std::memory_order_acquire)) {
            controllerDeliveryRouteAttestationPacked_.store(0, std::memory_order_release);
            return;
        }
        const quint64 packed = (generation << 2) | routeBits;
        controllerDeliveryRouteAttestationPacked_.store(packed, std::memory_order_release);
        if (!armed_.load(std::memory_order_acquire)
            || armRevocationGeneration_.load(std::memory_order_acquire) != armEpoch) {
            quint64 expected = packed;
            (void)controllerDeliveryRouteAttestationPacked_.compare_exchange_strong(
                expected, 0, std::memory_order_acq_rel);
        }
    }
    [[nodiscard]] double nowMs() const;
    [[nodiscard]] bool squareInputAllowed() const;
    [[nodiscard]] bool stickInputAllowed() const;
    // Raw RS-DOWN is a separate stick-shot source. The Square Tempo toggle never
    // enables it; the configured input source must explicitly allow stick input.
    [[nodiscard]] bool stickTempoArmAllowed() const;
    [[nodiscard]] bool stickShotActive(const ControllerState& state) const;
    void beginShot(ShotMode mode, double now, const QString& shotType,
                   double physicalPressMs = -1.0);
    void startPumpFake(ShotMode mode, double now);
    [[nodiscard]] QString classifyShotType(const ControllerState& physical, ShotMode mode) const;
    // Output mode never changes timing authority: every mode uses the plain shot type.
    [[nodiscard]] static QString timingKey(const QString& shotType, ShotMode mode);
    // Lazy-seeded per-type read. `fallback` is returned only when neither the
    // requested key nor its canonical plain shot type has a learned value.
    [[nodiscard]] static double seededValue(const QMap<QString, double>& map,
                                            const QString& bucketKey, const QString& shotType,
                                            double fallback = 0.0);
    // Resolve Square output mode from the sole user-facing Tempo toggle.
    [[nodiscard]] bool resolveTempoForType(const QString& shotType) const;
    // Before ownership, any supported physical shot gesture may be promoted only
    // after the required unique, strict-age genuine meter frames from that exact
    // physical epoch form a valid rising episode.  Stick cold-start calibration
    // additionally retains each mode's existing intent dwell before promotion.
    [[nodiscard]] bool recordPendingMeterOwnershipSample(
        const DetectionResult& result, double sampleNow, ShotMode mode,
        double gestureStartMs, quint64 physicalEpoch, bool inputQualified);
    [[nodiscard]] bool pendingMeterOwnershipCandidate(
        double atMs, ShotMode& mode, double& gestureStartMs,
        quint64& physicalEpoch, QString& shotType, bool& inputQualified) const;
    // Match the output edge contract while ownership is still pending: one or
    // two bad controller polls do not end the physical press until the existing
    // three-sample release debounce says they do.
    [[nodiscard]] bool pendingSquarePressActive() const noexcept;
    [[nodiscard]] bool physicalGestureActiveForMode(ShotMode mode,
                                                    const ControllerState& physical) const noexcept;
    [[nodiscard]] bool automaticCalibrationEpochCurrent() const noexcept;
    void seedPromotedMeterOwnershipEpisode();
    void clearPendingMeterOwnershipEpisode() noexcept;
    void clearPendingMeterOwnershipEvidence() noexcept;
    // True only when EVERY structure-verified frame of the current physical press was
    // above the first-sight fill bound AND that run has been numerically static across a
    // long window. Reporting it ends the pending wait early; it never owns, never arms and
    // never releases, so the fail-closed contract is identical to ownership_proof_timeout.
    [[nodiscard]] bool pendingMeterOwnershipStaleMeterBlocked(
        double now, double pressAgeMs) const noexcept;
    void clearPendingStickCalibration() noexcept;
    void cancelPendingLatencyCalibrationOwnership();
    // Go-To-only LS cancel gate. Square-triggered Button and Tempo modes share
    // the same non-cancellable ownership contract once promoted.
    [[nodiscard]] bool lsCancelRequested();
    [[nodiscard]] double shotTypeOffsetMs(const QString& type) const;
    void latchSuppressedShotControlOverlap(const ControllerState& physical);
    void processIdle(ControllerState& output, const ControllerState& physical, double now);
    void processArmed(ControllerState& output, double now);
    void processHolding(ControllerState& output, double now);
    void processAutonomousLiveMeterHolding(ControllerState& output, double now);
    void processReleasing(ControllerState& output, double now);
    void processPumpFake(ControllerState& output, double now);
    void processCooldown(ControllerState& output, double now);
    void forceTempoSquareGather(ControllerState& output) const;
    void forceHeldOutput(ControllerState& output) const;
    void pulsePumpFakeOutput(ControllerState& output) const;
    // Rhythm shooting evaluates movement through the up-flick. Preserve an
    // arm-time fade vector only across a transient centered LS sample; a new
    // non-neutral player vector wins and cooldown returns full movement control.
    void preserveTempoFadeMovementContext(ControllerState& output) const;
    void preserveFadeMovementVector(ControllerState& output,
                                    const QString& shotType,
                                    double armX, double armY,
                                    bool valid) const;
    void preservePendingTempoStickMovementContext(ControllerState& output) const;
    void clearPendingSquareMovementContext() noexcept;
    void clearPendingTempoStickMovementContext() noexcept;
    enum class TempoMovementPhase {
        None,
        AcquireIntent,
        AwaitCommitDelivery,
        CommitDwell,
        Active,
    };
    struct TempoMovementTransaction {
        TempoMovementPhase phase = TempoMovementPhase::None;
        ShotMode mode = ShotMode::ButtonShot;
        quint64 generation = 0;
        double startedMs = -1.0;
        double commitStartedMs = -1.0;
        double commitConfirmedMs = -1.0;
        // [ORION_TEMPO_INTENT_WINDOW] Widened 4 -> 8 on 2026-08-06. A fade must show THREE
        // CONSECUTIVE samples past movingSquareThreshold that agree on shot type; with only
        // 4 slots inside a 20ms window a thumb still ramping into the stick got essentially
        // one attempt, and 19 of 47 tempo gestures in one live session (40%) failed as
        // tempo_movement_intent_unstable and fell back to the plain button path — the owner's
        // "some fades don't apply the tempo timing remapping". median3() always reads the LAST
        // three samples, so more slots means it judges the SETTLED stick instead of the ramp.
        static constexpr int kIntentSlots = 8;
        std::array<int, kIntentSlots> lsX{};
        std::array<int, kIntentSlots> lsY{};
        std::array<QString, kIntentSlots> shotType;
        int sampleCount = 0;
        int committedLsX = 0;
        int committedLsY = 0;
        QString committedShotType;
    };
    void startTempoMovementTransaction(ShotMode mode, double now);
    // Returns true while this poll is consumed by acquire/commit/dwell. False
    // means the transaction is Active (normal Tempo gather may proceed) or it
    // failed closed and the caller must use its native fallback path.
    bool advanceTempoMovementTransaction(ControllerState& output,
                                         const ControllerState& physical,
                                         ShotMode mode, double now);
    [[nodiscard]] bool stabilizeTempoMovementIntent();
    void applyTempoMovementCommitOutput(ControllerState& output) const;
    void failTempoMovementTransaction(ShotMode mode, const QString& reason);
    void resetTempoMovementTransaction() noexcept;
    // Apply the canonical mode-aware release packet on the exact fire tick:
    // ButtonShot drops Square, Go-To neutralizes RS, and tempo modes emit their
    // initial RS-up flick. OrionAppController uses the same policy
    // when copying a sub-tick deadline to the precise-fire worker.
    void clearReleaseOutput(ControllerState& output) const;
    bool triggerRelease(double now, bool alreadySubmitted = false);
    void completeRelease(double now);
    void abort(QString reason);
    // [ORION_ABORT_IDENTITY] Single emitter for the "Shot abort identity:" line, so that EVERY
    // `emit shotAborted(...)` site produces a joinable stamp instead of only abort(). Before this,
    // 4 of the 5 abort sites (the pre-ownership ones in cancelPendingLatencyCalibrationOwnership /
    // reportPendingStickFault / the Square proof-timeout / the Square early-release) emitted the
    // signal with no identity line at all, so every `ownership_proof_incomplete` and
    // `latency_calibration_cancelled` abort was unattributable to a shot and silently dropped by
    // arming analyses. Verified against logs/orion_native.log.1: in the post-identity era of that
    // log the abort()-routed reasons are 1:1 with identity lines, while all 23
    // `ownership_proof_incomplete` aborts have zero.
    //
    // `site` names the emit site so an abort can be attributed to the code path that raised it.
    // The numeric fields take -1 as an explicit "structurally absent at this site" sentinel: the
    // pre-ownership sites have no ShotContext, hence no attempt/seq/schedule token. -1 is
    // impossible as a real epoch/token/seq, so a sentinel is distinguishable from a genuine 0
    // (which abort() can and does emit), and both are distinguishable from a MISSING line.
    //
    // `shotType` is the classification the engine ACTUALLY HELD for the aborted attempt, never
    // one re-derived at the abort site: re-classifying from lastPhysical_ here would read stick
    // and L2 state that may already have moved, and label the abort with a shot the user did not
    // take. Sites that hold no classification pass an empty string and the line reports the
    // explicit `unclassified` token, so "the engine never typed this attempt" stays
    // distinguishable from "this build does not emit the field".
    void emitShotAbortIdentity(const char* site, qint64 physicalEpoch, qint64 shotAttempt,
                               qint64 releaseSeq, qint64 scheduleToken, const QString& reason,
                               const QString& shotType);
    // "No Dip" -> "No_Dip"; empty -> "unclassified". Static because it is a pure rendering of
    // its argument -- it must never be able to reach for engine state and substitute a
    // different shot's classification for a missing one.
    [[nodiscard]] static QString abortShotTypeToken(const QString& shotType);
    void learnFromRelease(const ShotContext& ctx);
    // Integral correction of the per-shot-type learned offset from the real HUD
    // verdict residual (learned += gain*errorMs): a LATE residual adds lead until
    // shots center; EXCELLENT (residual 0) leaves the offset untouched.
    void learnFromOutcome(const QString& shotType, double errorMs);
    // Emit shotOutcomeLearned with the RIGHT learner state for the active mode: the per-type ACQUIRE->LOCK
    // values normally, or the autonomous global-lead state (learnedLatencyMs + "autonomous" phase/anchor)
    // under autonomousVision, where the per-type maps are frozen (P3.5 overlay honesty).
    void emitShotOutcome(int seq, const QString& displayType, const QString& key,
                         const QString& verdict, double errorMs);
    // Tracking / smoothing helpers
    void updateTrackingHistory(const ControllerState& physical);
    void updateShootingState();
    // Sub-tick scheduler: arm a precise fire for a deadline inside the horizon. Returns
    // true when armed (the holding frame should keep holding and surface release_scheduled).
    // horizonOverrideMs > 0 widens the commit horizon for THIS arm (the fused deadline uses
    // fusedSchedulerHorizonMs; every legacy caller keeps the tight default horizon).
    enum class ScheduledFireAuthority { MeterVision, AutonomousMeterVision, Pose };
    bool scheduleFire(double deadlineMs, double now, double horizonOverrideMs = -1.0,
                      ScheduledFireAuthority authority = ScheduledFireAuthority::MeterVision);
    [[nodiscard]] double adaptiveAutonomousSchedulerHorizonMs() const noexcept;
    [[nodiscard]] double meterAuthorityExpiryMs() const noexcept;
    // Observed genuine-frame cadence, stall-spikes bounded. One definition for the scheduler
    // horizon, the authority cadence floor and every imminent-token guard.
    [[nodiscard]] double cleanSourceGapMs() const noexcept;
    // [ORION_IMMINENT_TOKEN] One source cadence, bounded. Inside this distance from its own
    // deadline an armed token is IRREPLACEABLE: no newer frame can arrive, be fitted and be
    // re-armed before the instant it already owns, so tearing it down can only convert a
    // release into an abort. Every reschedule/invalidity guard shares this one definition so
    // the sub-tick mirror can never drift from the 4 ms tick again (see [ORION_SUBTICK_PARITY]).
    [[nodiscard]] double imminentTokenWindowMs() const noexcept;
    // True when an armed vision token is inside imminentTokenWindowMs() of firing.
    [[nodiscard]] bool armedTokenIrreplaceable(double now) const noexcept;
    // [ORION_INFLIGHT_TOKEN] True when an armed, unconfirmed vision token's own deadline has
    // PASSED but its submit is still legitimately in flight, i.e. `now` is inside
    // schedulerGraceMs of that deadline.
    //
    // The precise-fire worker does not press exactly ON the deadline: it spins toward the target
    // and its physical edge lands a short, jittery moment later (OrionAppController's fire loop
    // documents ~1.2 ms for the post-spin path). schedulerGraceMs exists precisely to cover that
    // interval -- consumeDueScheduledFire() refuses to give up on a token until
    // `deadline + schedulerGraceMs`.
    //
    // Every reschedule/teardown site, however, used `untilArmedMs > 0.0` as its "protect this
    // token" test, so the instant `now` crossed the deadline by ANY amount the token stopped
    // being protected and the next 4 ms tick fenced it -- which calls
    // OrionPreciseFireThread::disarm(), which sets `aborted_` and is honoured by the fire loop
    // MID-SPIN. The engine was therefore cancelling the very press it was waiting for, roughly
    // one millisecond before it landed, and then declaring the shot lost.
    //
    // Measured, 2026-08-04 live batch (32 fires / 13 aborts): 4 of the 8 live_tip_deadline_missed
    // aborts happened 0.2 / 1.3 / 1.8 / 2.4 ms after their own armed token's deadline -- every one
    // inside the 8 ms grace the design already grants. They were invisible in the logs because
    // invalidateUnconfirmedVisionSchedule() only reported a kill when until_armed_ms was positive.
    //
    // Holding a token through this window can NEVER produce a late command: no new command is
    // created, the worker still presses at its own already-committed deadline, and once the grace
    // expires the token is fenced and the shot aborts exactly as it does today.
    [[nodiscard]] bool armedTokenSubmitInFlight(double now) const noexcept;
    // [ORION_DEADLINE_DRIFT] How far the newest estimate's fireAt may sit from the ARMED deadline
    // before the armed token is torn down and replaced.
    //
    // The tick recomputes canonicalAutonomousTipDecision(now) every 4 ms, and the inverse-variance
    // fusion weight depends on the sampler sigma, which depends on the horizon `crossing - now`.
    // So the fused deadline MOVES a fraction of a millisecond on every tick even when no new frame
    // arrived and nothing about the trajectory changed. Against an exact-equality (1e-6 ms) test
    // that meant a revoke + re-arm on essentially every tick: 298 of the 373 reschedule kills in
    // the 2026-08-04 batch had NO new frame between them, and the deadline had moved p50 0.32 ms /
    // p90 0.95 ms. Each of those kills is a synchronous cross-thread fence plus a worker re-arm,
    // and it resets the worker's lead time to under one tick.
    //
    // The comparison is always against the token's FIXED armed deadline, so this tolerance bounds
    // the total divergence from the newest estimate at its own value -- it cannot accumulate tick
    // over tick. One millisecond is ~1/20th of the predictor sigma the same decision was just
    // accepted as valid on (17-63 ms live) and well inside one console tick, while a genuine
    // frame-driven refinement (p50 21.6 ms) still reschedules exactly as before.
    [[nodiscard]] static constexpr double tokenDeadlineDriftToleranceMs() noexcept
    {
        return 1.0;
    }
    // [ORION_TRANSIENT_INVALID] Record that this observation produced no authoritative crossing.
    // Returns true when the ARMED token must be preserved anyway -- either because it is already
    // irreplaceable, or because invalidity has lasted no longer than one source cadence and is
    // therefore a single-frame observation spike rather than a changed trajectory. Returns false
    // (and the caller tears the token down exactly as before) once invalidity persists.
    [[nodiscard]] bool noteTipPredictionInvalid(double now) noexcept;
    // The prediction is authoritative again: end any in-progress transient.
    void noteTipPredictionValid() noexcept;
    // [ORION_ROLLING_LEASE] Extend an armed vision token's authority from the newest genuine frame
    // in its own vision epoch. Forward only; never shortens, never crosses an epoch.
    void refreshScheduledFireAuthorityLease() noexcept;
    // Does the (rolled) lease authorize the armed deadline itself? This is the submit-time
    // invariant that replaced the arm-time `deadline <= expiry` precondition.
    [[nodiscard]] bool scheduledFireAuthorityCoversDeadline() const noexcept;
    // Named reasons scheduleFire() can refuse. Every one of these used to be a silent
    // `return false`; see [ORION_ARM_GATE_TRACE].
    enum class ArmGate : unsigned {
        DisabledOrDisarmed = 0,
        AlreadyArmed = 1,
        DeadlinePast = 2,
        OutsideHorizon = 3,
        MinHold = 4,
        NoMeterMode = 5,
        NoAuthority = 6,
        AuthorityLease = 7,
        AuthorityStaleNow = 8,
        PoseAuthority = 9,
    };
    [[nodiscard]] static const char* armGateName(ArmGate gate) noexcept;
    [[nodiscard]] bool meterReleaseAuthorityCurrent(double atMs) const noexcept;
    [[nodiscard]] double poseAuthorityExpiryMs() const noexcept;
    [[nodiscard]] bool poseReleaseAuthorityCurrent(double atMs) const noexcept;
    [[nodiscard]] bool autonomousLiveMeterTimingEnabled() const noexcept;
    [[nodiscard]] bool measuredLeadAuthoritative(double atMs) const noexcept;
    // [ORION_USER_LEAD_AUTHORITY] The user-lead alternative path of measuredLeadAuthoritative(),
    // factored out so the gate and the value both consume the SAME predicate: flag armed, owner
    // explicitly set an in-band lead, no env sweep override, telemetry present and fresh, and
    // the exact controller-route attestation echo. False whenever the flag is OFF.
    [[nodiscard]] bool userLeadAuthorityActive(double atMs) const noexcept;
    [[nodiscard]] bool controlledCalibrationAnchorAvailable(double atMs) const noexcept;
    // L1 never actuates a tip. Once the distinct validation succeeds, production uses the same
    // posterior mean in both the input-tick and fresh-frame scheduling paths.
    [[nodiscard]] double measuredLeadForActuationMs() const noexcept;
    // [ORION_USER_LEAD_AUTHORITY] The lead sd the SCHEDULER should spend: the estimator's
    // authority sd whenever one exists (bit-identical to before), else the 6.0ms factory-floor
    // stand-in while the user-lead readiness path is the active authority, else the raw value.
    [[nodiscard]] double effectiveLeadSdMs() const noexcept;
    // [ORION_GREEN_CENTER] ms to fire EARLIER so the landing sits inside the green window rather
    // than on its late edge. Derived from this shot's own tracked window and the live rise slope;
    // 0.0 whenever the feature is off or the window/slope are not trustworthy, which restores the
    // previous edge-aimed behaviour exactly. Not const: it logs the offset once per shot.
    [[nodiscard]] double autonomousGreenCenterOffsetMs(double slopePctPerMs);
    // [ORION_CONTESTED_DEADLINE] The single slope bound both registration guards pivot on.
    // At or below it the sampler is positively asserting the meter has STOPPED. It is the
    // ceiling of registrationSoloContradictedByFlatMeter() and, doubled, the floor of
    // contestedTipDeadlineOutrunByMeasuredRunway(); keeping one named constant is what makes
    // the two provably disjoint by inspection, so nothing on the rising side can ever be
    // reached with a fit the flat side would have vetoed.
    static constexpr double kFlatMeterSlopePctPerMs = 0.05;
    // Minimum sampler slope that counts as POSITIVE proof the meter is still climbing.
    // Deliberately 2x kFlatMeterSlopePctPerMs, and still below the slowest genuine live rise
    // measured on this rig (0.13-0.18 pp/ms; `Lead measurement: velocity_pct_ms=0.1770`,
    // per-release `vel=0.1755 / 0.1819`, and 0.146 pp/ms reconstructed across live epoch 5).
    static constexpr double kProvenRiseSlopePctPerMs = 2.0 * kFlatMeterSlopePctPerMs;
    // Minimum fit width before the sampler is allowed to speak about the meter's motion at all,
    // in EITHER direction. Shared by both guards for the same reason as the slope bound.
    static constexpr int kMinSamplesToJudgeMeterMotion = 4;
    // Sampler veto over a SOLO registration estimate. Registration extrapolates, so on a flat-held
    // meter it projects a tip that never arrives; this lets the sampler contradict it -- but only
    // when the sampler actually has samples, never merely because it has none.
    [[nodiscard]] static bool registrationSoloContradictedByFlatMeter(
        const TemporalSampler::CrossingFit& fit) noexcept;
    // [ORION_CONTESTED_DEADLINE] The mirror image of the guard above, for the one source the
    // engine has ALREADY rated untrustworthy: `registration_far_disagreement`. Returns true when
    // the detector's own fill + slope independently prove the command is still submittable, so
    // the honest response to a "missed" deadline computed from the distrusted estimate is to keep
    // waiting for a trustworthy one -- never to release, and never to relinquish the shot.
    // Static and argument-only so the whole predicate is auditable from its inputs.
    [[nodiscard]] static bool contestedTipDeadlineOutrunByMeasuredRunway(
        const QString& source, const TemporalSampler::CrossingFit& fit,
        double fillPct, double leadMs) noexcept;
    [[nodiscard]] bool liveMeterCrossingAuthoritative(
        const TemporalSampler::CrossingFit& fit, double atMs) const noexcept;
    struct AutonomousTipDecision {
        TemporalSampler::CrossingFit samplerFit;
        bool samplerAuthoritative = false;
        bool valid = false;
        double tipAbsMs = -1.0;
        double sigmaMs = -1.0;
        double combinedSigmaMs = -1.0;
        double velocityPctPerMs = 0.0;
        QString source = QStringLiteral("none");
        // [ORION_HORIZON_DEBIAS] ms subtracted from the raw sampler crossing this decision
        // (negative = the crossing was pushed later). Telemetry only; 0 when the feature is
        // off or the reference has not formed yet.
        double horizonDebiasMs = 0.0;
        // [ORION_FUSION_TELEMETRY] 2026-08-04, STEP 0. Append-only snapshot of BOTH fusion
        // members -- what won and what lost -- plus the weight registration actually carried.
        // Strictly observational: no branch reads these back, and the decision is byte-identical
        // with them present. They exist because the reg/sampler weighting was invisible from the
        // log: the fused sigma alone cannot distinguish registration contributing 2% from 50%,
        // and the whole template-fit question is a weighting question. -1.0 = member absent.
        double regTipAbsMs = -1.0;
        double regSigmaMs = -1.0;
        double samplerCrossingMs = -1.0;
        double samplerSigmaMs = -1.0;
        // regVar^-1 / (regVar^-1 + samplerVar^-1) on the fused branch; -1.0 elsewhere. This must
        // be reproducible BY HAND from reg_sigma_ms and smp_sigma_ms on the same log line -- that
        // identity is the verification for this step, not a passing test.
        double regWeight = -1.0;
        // [ORION_TIP_PHASE] The THIRD predictor member, alongside samplerFit and regTipAbsMs.
        // Unlike those two it is a LOOKUP, not an extrapolation: phaseTipAbsMs is simply
        // phaseAnchorMs + effectiveTipPhaseConstantMs(). -1.0 = the member was absent (no
        // observed anchor crossing this shot). phasePrimary records whether it actually won
        // the decision, and phaseWeight is the inverse-variance weight it WOULD have carried
        // against whatever the pre-existing chain selected -- published so this member's
        // influence is visible in the log the way reg_weight already makes registration's.
        double phaseAnchorMs = -1.0;
        double phaseTipAbsMs = -1.0;
        double phaseSigmaMs = -1.0;
        double phaseWeight = -1.0;
        bool   phasePrimary = false;
        // [ORION_METER_DELAY_LEAD_STARVATION] True when a decision that would otherwise have
        // been valid was withdrawn because an applied meter delay has pushed the required lead
        // past what ANY in-video estimate can schedule accurately (lead > usable max), and the
        // winning member was a visible-meter EXTRAPOLATOR (sampler/registration — not the
        // phase lookup). Such estimates are precisely the pre-anchor long-horizon fits the
        // engine already documents as its worst regime; arming on one under heavy delay is the
        // measured 2026-08-08 early-giveaway failure. Consumed by the tick for a once-per-shot
        // attributed diagnostic; scheduling treats the decision exactly like any other invalid
        // one (hold, never arm, bounded abort).
        bool   delayStarvedVisibleSource = false;
        // [ORION_PRESS_ANCHOR] The FOURTH member — the press-wall-anchored lookup, the only
        // one immune to the applied meter delay (its anchor is the input hook's press wall
        // time, not a delayed video frame). -1.0 = absent (flag off, press undated, learner
        // below its min-samples floor, or no V estimate). pressAnchoredPrimary records that
        // it actually owns the decision, which it may do ONLY when every in-video member
        // failed to produce a valid one (including the delay-starvation withdrawal); its
        // per-type MAD-derived sigma keeps the fusion/validity weighting honest.
        double pressAnchoredTipAbsMs = -1.0;
        double pressAnchoredSigmaMs = -1.0;
        double pressAnchoredWeight = -1.0;
        bool   pressAnchoredPrimary = false;
    };
    // Single source-selection/fusion function shared by the input tick and the
    // fresh-frame rescheduler. Neither caller may replace its decision with an
    // independently calculated sampler-only deadline.
    [[nodiscard]] AutonomousTipDecision canonicalAutonomousTipDecision(
        double atMs) const;
    // [ORION_SLOW_METER_DEFER] True when a candidate autonomous live-tip deadline (fireAtMs)
    // undercuts the SAME decision's phase-member deadline by more than
    // RemapConfig::slowMeterDeferUndercutMs -- see the field's block comment for the 2026-08-06
    // measurement. Pure predicate: it creates nothing, moves nothing, and returns false
    // whenever the flag is off, the phase member is absent, or its horizon is implausible, so
    // every refusal it produces is a refusal to fire EARLIER and never a refusal to fire.
    [[nodiscard]] bool slowMeterDeferBinds(const AutonomousTipDecision& decision,
                                           double fireAtMs, double effectiveLeadMs,
                                           double atMs) const noexcept;
    // A strict autonomous deadline already copied from a genuine frame may
    // survive a detector blink only inside the immutable frame+lead lease it
    // carried when armed. No new deadline or extrapolation is created here.
    [[nodiscard]] bool autonomousVisionScheduleLeaseCurrent(double atMs) const noexcept;
    void invalidateVisionScheduleForDropout(double atMs);
    void refreshLatencyCalibrationStatus(bool force = false);
    enum class OwnedOutputDrain {
        None,
        HoldUntilPhysicalEnd,
        ReleasedUntilPhysicalEnd,
    };
    void latchOwnedOutputDrain(OwnedOutputDrain state, ShotMode mode,
                               const QString& shotType = {},
                               double lsArmX = 0.0, double lsArmY = 0.0,
                               bool movementValid = false);
    void clearOwnedOutputDrain() noexcept;
    // An owned gesture cannot be returned to one noisy raw controller poll.  The
    // drain preserves the last bot-owned representation until three physical
    // end samples, or suppresses it after a release/cancel, without permitting
    // the same uninterrupted gesture to become another shot.
    bool applyOwnedOutputDrain(ControllerState& output,
                               const ControllerState& physical, double now);
    // Route/safety gates still receive real controller polls while automation is
    // disarmed. Consume their debounced physical-end evidence so a fence left by
    // the old route cannot silently sacrifice the first shot after recovery.
    void observePhysicalEndWhileDisarmed(const ControllerState& physical);
    void restoreAbortOutput(ControllerState& output);
    // Complete a confirmed precise fire, or synchronously fence its worker before
    // using the GUI-tick fallback. The fence is the linearization point: a worker
    // submit that already won is consumed once; otherwise it is impossible for a
    // late result to arrive after the fallback release.
    bool consumeDueScheduledFire(ControllerState& output, double now);
    void relinquishAutonomousLiveMeterShot(ControllerState& output,
                                           const QString& reason);
    [[nodiscard]] bool tickPhaseAuthoritative(double atMs) const noexcept;
    // `site` names the caller that killed the token, mirrored into the same
    // TIP TOKEN KILL line the vision wrapper emits. Callers that already emitted
    // their own attributed kill line (the vision wrapper) pass nullptr to suppress
    // the duplicate; every other caller inherits "unattributed" so a live-token
    // teardown can never again be silent. Diagnostic only — no behaviour change.
    bool invalidateUnconfirmedSchedule(bool fallbackTakeover = false,
                                       const char* site = "unattributed");
    // `site` names the call site that destroyed the token. When an ARMED, unconfirmed
    // token with a still-future deadline is torn down, the site + remaining time is
    // logged (TIP TOKEN KILL) so a lost release is attributable to one code path
    // instead of inferred from surrounding telemetry. Diagnostic only — no behaviour.
    void invalidateUnconfirmedVisionSchedule(const char* site = "unspecified");
    void invalidateUnconfirmedPoseSchedule();
    void clearScheduledFire();

    RemapConfig config_;
    ShotContext shot_;
    PoseTimingState pose_;
    TemporalSampler sampler_;
    // Source-cadence estimate for early precise-worker handoff. A fixed 6 ms horizon means a
    // 60-FPS detector normally discovers the deadline only after it is due; retain a decaying
    // high-water mark so ordinary 16.7-25 ms source gaps still arm before the final frame.
    double lastGenuineCaptureSampleMs_ = -1.0;
    double genuineFrameGapEmaMs_ = 1000.0 / 60.0;
    double genuineFrameGapHighWaterMs_ = 1000.0 / 60.0;
    // [ORION_FUSED_FIRE] the per-shot fused tip posterior + its bookkeeping. All engine-clock.
    FusedTipEstimator fused_;
    double lastFusedSampleMs_ = -1.0;     // shot_.lastDetectionMs already consumed by fused_
    double lastFusedFillPct_ = -1.0;      // newInfo detection: fill actually stepped
    double fusedFireAtMs_ = -1.0;         // current fused fire deadline (diagnostic/shadow)
    bool fusedPeakLatched_ = false;       // rise->peak regime transition fired once this shot
    // Mean capture->ingestion delay of consumed samples (EMA): the posterior lives on the
    // ARRIVAL timeline; the fire deadline subtracts this once to land on the capture timeline
    // the latency oracle's L is referenced to (frame_age as a timeline correction, not a lead).
    double fusedFrameAgeEmaMs_ = -1.0;
    // Last detected source-frame identity, engine-scoped so the same decoder frame
    // cannot be re-served across Idle -> Holding and masquerade as a new timing sample.
    // captureTs is authoritative when both sides provide it; frameNumber is the
    // backward-compatible fallback for older sidecars.
    int lastDetectorFrameNumber_ = -1;
    double lastDetectorCaptureTsMs_ = -1.0;
    // Observed posterior SD (measured_latency_sd_ms), engine-scoped like measuredLatencyMs_.
    double measuredLatencySdMs_ = 0.0;
    // Explicit actuation tuple. It is intentionally separate from the observed
    // posterior so an ordinary N=1 refinement cannot move a factory-authorized
    // command deadline. Only measuredLeadForActuationMs() consumes this value.
    QString measuredLatencyAuthorityKind_ = QStringLiteral("none");
    double measuredLatencyAuthorityMs_ = 0.0;
    double measuredLatencyAuthoritySdMs_ = 0.0;
    // Oracle label count + two-stage controlled provenance. L1 may schedule only the causal
    // validation stop; provisional tip authority requires a distinct validated L2.
    int measuredLatencyN_ = 0;
    bool measuredLatencyControlledAnchor_ = false;
    bool measuredLatencyProvisional_ = false;
    bool measuredLatencyRestored_ = false;
    bool measuredLatencyVideoRouteAttested_ = false;
    // Live capture tier ("capture_card" / "decoder"), used to re-verify the VIDEO half of a
    // packaged factory prior's route binding. Empty means not yet attested, which keeps the
    // previous controller-half-only behaviour instead of failing a route that always passed.
    QString latencyVideoRoute_;
    bool measuredLatencyFactoryPrior_ = false;
    QString measuredLatencyPriorSource_;
    QString measuredLatencyModelVersion_;
    quint64 measuredLatencyAttestationGeneration_ = 0;
    LatencyControllerRoute measuredLatencyDeliveryRoute_ = LatencyControllerRoute::None;
    quint64 measuredLatencyScopeEpoch_ = 0;
    static_assert(std::atomic<quint64>::is_always_lock_free,
                  "controller route attestation must remain lock-free");
    std::atomic<quint64> controllerDeliveryRouteAttestationPacked_{0};
    bool controllerRouteBindingRequired_ = false;
    // Receipt freshness + estimator epoch fence. Value/N/SD are one atomic telemetry
    // snapshot: absent/invalid fields clear them together. A count regression, detector
    // source restart, or telemetry gap opens a new runtime epoch; the persisted one-time
    // clock-rebaseline latch remains intact while this epoch reconverges independently.
    double measuredLeadLastUpdateMs_ = -1.0;
    bool measuredLeadTelemetryPresent_ = false;
    bool measuredLeadEverValid_ = false;
    bool measuredLeadEpochReady_ = false;
    bool measuredLeadRequireNProgress_ = false;
    int measuredLeadRestartFloorN_ = -1;
    // Explicit calibration intent survives individual shot resets. It auto-clears
    // only when the current measured-latency epoch is truly authoritative.
    bool latencyCalibrationMode_ = false;
    bool latencyCalibrationAutomatic_ = false;
    bool lastLatencyCalibrationStatusActive_ = false;
    bool lastLatencyCalibrationStatusReady_ = false;
    int lastLatencyCalibrationStatusN_ = -1;
    double lastLatencyCalibrationStatusLeadMs_ = -1.0;
    double lastLatencyCalibrationStatusSdMs_ = -1.0;
    // [Phase-2 A2(c)] console input-tick phase telemetry, engine-scoped like measuredLatencyMs_
    // (overwritten per frame that carries it — the sidecar decays conf, so a stale phase
    // self-retires through the engage gate). tickPhaseEpochMs_ is the EDGE phase in press-epoch
    // ms modulo the 16.7ms tick; the fused snap converts it onto the engine clock.
    double tickPhaseEpochMs_ = -1.0;
    double tickPhaseConf_ = 0.0;
    double tickPhaseSdMs_ = -1.0;
    double tickPhaseLastUpdateMs_ = -1.0;
    // [ORION_MEASURED_LEAD] shift every lead-absorbing clock ONCE by (measured - old lead).
    void rebaselineLeadClocks();
    // [ORION_PROBE] warmup pump-fake probe run state (engine Idle only).
    void probeTick(ControllerState& output, const ControllerState& physical, double now);
    int probesRemaining_ = 0;
    int probeSeq_ = 0;
    double nextProbeAtMs_ = -1.0;
    double probePressEndMs_ = -1.0;
    double probeCallEndMs_ = -1.0;     // Cross held until this instant (call for the ball)
    double probeShootAtMs_ = -1.0;     // the Square press fires at this instant
    // Epoch(capture)<->engine-clock bridge: offset = engineNow - captureTsMs, median-initialised
    // over the first 5 stamped samples then EMA alpha=0.05. Converts the sidecar's epoch
    // posthoc_tip_ms labels onto the engine clock for the appear->tip anchor EMAs.
    double epochToEngineOffsetMs_ = 0.0;
    int epochBridgeN_ = 0;
    std::array<double, 5> epochBridgeInit_{{0, 0, 0, 0, 0}};
    // Post-hoc label consumption (once per completed shot) + the ended shot's anchor snapshot
    // the label teaches against (stamped at release/abort; the label arrives ~0.5s later).
    int lastPosthocNConsumed_ = 0;
    double endedShotAppearMs_ = -1.0;     // engine-clock anchorValidMs of the ended shot
    QString endedShotBucketKey_;
    GreenWindowTracker greenTracker_;
    // === Ceiling stack runtime (all inert while their flags are off) ===
    // [ORION_TEMPLATE_ARRIVAL] session-scoped per-bucket crossing templates + in-flight match.
    TemplateArrivalEstimator templateArrival_;
    // [ORION_BANDIT_LEAD] retained for offline/test experiments only. Detector release-window
    // diagnostics are deliberately never routed into it.
    BanditLeadTuner banditTuner_;
    // Dedupe uses the native monotonic release id, not the sidecar-local diagnostic id (which
    // restarts after recovery). releaseCounter_ survives reset(), so this can too.
    int lastReleaseWindowDiagnosticSeq_ = 0;
    // Release-window telemetry has a different trust/use contract from gameplay outcomes.
    // Calibration releases deliberately never populate lastReleaseSeq_, but they still emit a
    // delivered native marker and therefore need an immutable id/type join for observability.
    // These fields are never consumed by timing readiness, outcome learning, or the bandit.
    int lastReleaseMarkerSeq_ = 0;
    QString lastReleaseMarkerShotType_;
    quint64 lastReleaseMarkerPhysicalShotEpoch_ = 0;
    quint64 lastReleaseMarkerShotAttempt_ = 0;
    QElapsedTimer clock_;
    double testClockMs_ = -1.0;   // < 0 = real clock (production); >= 0 = test-injected mock time
    // Latched each process() tick so processArmed/processHolding can read the live physical
    // left stick for LS-cancel (those handlers don't otherwise receive the physical state).
    ControllerState lastPhysical_;
    bool prevSquare_ = false;
    bool prevStickActive_ = false;
    double squareHoldStartMs_ = -1.0;
    // Current physical shot-intent identity supplied synchronously by OrionAppController.
    // reset() clears the expectation; the controller's generator itself never resets.
    quint64 physicalShotEpoch_ = 0;
    quint64 pendingSquarePhysicalEpoch_ = 0;
    quint64 pendingSquareArmGeneration_ = 0;
    // A terminal pre-ownership/owned attempt cannot be rebuilt under the same
    // controller identity after its release/neutral fence clears. The upstream
    // monotonic edge tracker must publish a strictly newer epoch.
    quint64 retiredPhysicalShotEpoch_ = 0;
    bool pendingSquareTempoRemap_ = false;
    QString pendingSquareShotType_;
    double pendingSquareLsArmX_ = 0.0;
    double pendingSquareLsArmY_ = 0.0;
    bool pendingSquareMovementValid_ = false;
    TempoMovementTransaction tempoMovement_;
    quint64 tempoMovementGenerationCounter_ = 0;
    bool tempoPassThroughPulseActive_ = false;
    double tempoPassThroughPulseEndMs_ = -1.0;
    double stickDownHoldStartMs_ = -1.0;
    double stickUpHoldStartMs_ = -1.0;
    bool squareLatchedUntilRelease_ = false;
    bool stickDownLatchedUntilNeutral_ = false;
    bool stickUpLatchedUntilNeutral_ = false;
    OwnedOutputDrain ownedOutputDrain_ = OwnedOutputDrain::None;
    ShotMode ownedOutputDrainMode_ = ShotMode::ButtonShot;
    QString ownedOutputDrainShotType_;
    double ownedOutputDrainLsArmX_ = 0.0;
    double ownedOutputDrainLsArmY_ = 0.0;
    bool ownedOutputDrainMovementValid_ = false;
    int ownedOutputDrainEndFrames_ = 0;
    // B2c/C5 follow-up (2026-08-04, drain end-evidence leak). ownedOutputDrainEndFrames_
    // is zeroed by ANY re-assertion of the primary control, so a player who releases and
    // presses again inside the drain can never accumulate three consecutive end samples.
    // For ReleasedUntilPhysicalEnd that leaves applyReleased() masking their new press for
    // as long as they hold it. Latch the end evidence instead: once a genuine physical end
    // has been proven for THIS drain it stays proven. This bounds the FENCE only — no timer
    // ever drops the button, because for ButtonShot that edge is a blind release.
    bool ownedOutputDrainEndObserved_ = false;
    // Consecutive physical Square-UP polls. Identical evidence to processIdle's
    // `!physical.square() && allFalse(xButtonHistory)` three-sample debounce, but held on the
    // engine rather than inside ShotContext, so beginShot()/abort()'s `shot_ = ShotContext{}`
    // cannot erase a release the player really performed (the exact history-wipe class of bug
    // documented at processCooldown's cooldown-end reset).
    int physicalSquareUpPolls_ = 0;
    // Sticky: a genuine three-poll physical Square release has been observed since this shot
    // armed. Cleared in beginShot()/reset(). Consumed at the cooldown-end latch reset so a
    // player who released AND re-pressed inside the ~250ms release+cooldown window is not
    // suppressed by `squareLatchedUntilRelease_` for the whole of their next press. A
    // continuously-held Square never sets this, so auto-repeat behaviour is unchanged.
    bool physicalSquareReleaseSeenSinceArm_ = false;
    // The press half of that cycle. Without it, UP polls collected BEFORE an overlap press
    // existed would count as that press's release and unlatch a suppression that must hold.
    bool physicalSquareDownSeenSinceArm_ = false;
    // A physical vertical-stick shot gesture that arrived while another mode
    // owned/overrode RS cannot become a fresh shot after cooldown. Unlike abort
    // rearm blocking, this latch also keeps the suppressed control neutral until
    // three real neutral samples arrive.
    bool stickOverlapLatchedUntilNeutral_ = false;
    // Every abort of an already-owned Square shot relinquishes output immediately,
    // but the same physical gesture is not a new shot. This block is cleared only
    // by the existing three-sample UP debounce; unlike the ownership latch it never
    // suppresses physical pass-through.
    bool squareRearmBlockedUntilRelease_ = false;
    bool stickRearmBlockedUntilNeutral_ = false;
    // A cold stick gesture remains physical until the same strict meter proof
    // used by Square is complete.  This immutable identity survives lead
    // convergence during the gesture, but never a new physical epoch.
    bool pendingStickCalibrationActive_ = false;
    ShotMode pendingStickCalibrationMode_ = ShotMode::GoToStick;
    double pendingStickCalibrationStartMs_ = -1.0;
    quint64 pendingStickCalibrationPhysicalEpoch_ = 0;
    quint64 pendingStickCalibrationArmGeneration_ = 0;
    QString pendingStickCalibrationShotType_;
    // TempoStick's movement intent is latched on the physical RS-down edge,
    // before the three-frame meter proof can observe a transient centered LS.
    QString pendingTempoStickShotType_;
    double pendingTempoStickLsArmX_ = 0.0;
    double pendingTempoStickLsArmY_ = 0.0;
    bool pendingTempoStickMovementValid_ = false;
    struct PendingMeterOwnershipSample {
        DetectionResult result;
        double sampleMs = -1.0;
    };
    struct PendingMeterOwnershipEpisode {
        bool active = false;
        ShotMode mode = ShotMode::ButtonShot;
        double gestureStartMs = -1.0;
        quint64 physicalEpoch = 0;
        DetectionResult first;
        double firstMs = -1.0;
        double lastMs = -1.0;
        double lastFillPct = 0.0;
        int sampleCount = 0;
        int lastFrameNumber = -1;
        double lastCaptureTsMs = 0.0;
        int lastX = 0;
        int lastY = 0;
        int lastWidth = 0;
        int lastHeight = 0;
        // Strict live ownership requires three unique rising frames. Retain the
        // bounded proof episode so promotion does not silently discard the middle
        // frame and leave the autonomous predictor with only two samples exactly
        // when a fast shot or immediate occlusion needs a crossing estimate.
        QVector<PendingMeterOwnershipSample> samples;
    } pendingMeterOwnership_;
    // Audit lifetime is the physical debounced press, not the current 100ms
    // trajectory episode. If current-shot evidence is followed by a long gap,
    // the eventual pass-through must still be reported rather than silent.
    // [ORION_EPISODE_REANCHOR] Why the three-frame proof episode kept restarting. The live
    // `ownership_proof_incomplete` line reported samples=1 over a 1,459 ms press with no way to
    // tell whether the episode was never opened, opened and torn down by the detector (geometry /
    // identity / gap), or torn down by the fill itself (stale-meter descent). Counting is free and
    // makes the next batch decisive instead of inferential.
    struct PendingMeterOwnershipBreakCensus {
        int restarts = 0;     // episodes opened for this press
        int drop = 0;         // >8 pp cliff off the previous sample
        int anchor = 0;       // material descent below the episode anchor (stale meter)
        int geometry = 0;     // box shape/overlap discontinuity
        int identity = 0;     // no unique frame advance
        int gapOrCandidate = 0; // sample gap too long, or a different gesture/epoch
    };
    PendingMeterOwnershipBreakCensus pendingMeterOwnershipBreaks_;
    bool pendingMeterOwnershipCurrentEvidenceSeen_ = false;
    int pendingMeterOwnershipMaxProofSamples_ = 0;
    double pendingMeterOwnershipFirstEvidenceFillPct_ = 0.0;
    double pendingMeterOwnershipLastEvidenceFillPct_ = 0.0;
    // Stale carry-over meter census (2026-08-04). A press issued while the PREVIOUS shot's
    // meter is still rendered sees only frames above anchorMaxFirstFillPct, which can never
    // open an ownership episode (see recordPendingMeterOwnershipSample's first-sight bound).
    // Today that costs the player the full buttonNoMeterAbortMs holding the ball. Track the
    // rejected high-fill run so a STATIC one can be reported early. Any frame at/below the
    // first-sight bound disarms this permanently for the press: a genuine meter always
    // starts low, so a real rising meter can never be censused here.
    bool pendingMeterOwnershipSubAnchorSeen_ = false;
    int pendingMeterOwnershipStaleSamples_ = 0;
    double pendingMeterOwnershipStaleFirstMs_ = -1.0;
    double pendingMeterOwnershipStaleMinPct_ = 0.0;
    double pendingMeterOwnershipStaleMaxPct_ = 0.0;
    // Monotonic release id; stamped onto ShotContext::releaseSeq in triggerRelease.
    int releaseCounter_ = 0;
    // Monotonic shot-arm identity. Zero is permanently invalid; the counter survives
    // reset() so stale sidecar traffic cannot collide with the next shot after a reset.
    quint64 shotArmCounter_ = 0;
    // Last release, for pairing a HUD verdict (which arrives ~1.2s later) back to the
    // shot that produced it. A verdict is attributed only to a release whose age is in
    // the banner window and is consumed exactly once (lastOutcomeConsumedSeq_).
    double lastReleaseWallMs_ = -1.0;
    QString lastReleaseShotType_;
    // Canonical timing bucket of the last release, shared by Button and Tempo.
    QString lastReleaseBucketKey_;
    int lastReleaseSeq_ = 0;
    quint64 lastReleasePhysicalShotEpoch_ = 0;
    quint64 lastReleaseShotAttempt_ = 0;
    // [ORION_ARMED_SOURCE] Outcome-line attribution of the last gameplay release, paired
    // with lastReleaseSeq_ exactly like the identity fields above (the outcome arrives
    // ~1.2s later, after shot_ has been reset): which predictor's decision armed the token
    // that fired, that decision's sigma, the fill / command ETA at the arm, and the
    // schedule token itself (joins the outcome back to its promotion line). Empty source /
    // -1 / 0 = the release did not fire from an attributed autonomous vision token.
    // Stamped in triggerRelease from ShotContext::armedPredictor*; read only by
    // emitShotOutcome's "Outcome identity:" line.
    QString lastReleaseArmedSource_;
    double lastReleaseArmedSigmaMs_ = -1.0;
    double lastReleaseArmedFillPct_ = -1.0;
    double lastReleaseArmedCommandEtaMs_ = -1.0;
    quint64 lastReleaseScheduleToken_ = 0;
    int lastOutcomeConsumedSeq_ = -1;
    // [ORION_MEASURED_LEAD] Latest live-measured release-path latency (ms) from the sidecar
    // (measured_latency_ms). Engine-scoped (a boot-probe global, not per-shot) so it survives beginShot.
    // 0 = never measured -> the measured-lead path stays inert (byte-identical to today).
    double measuredLatencyMs_ = 0.0;
    // [ORION_LEAD_VISION_GATE] Fire-time vision confidence of the last release (vision-timed release code
    // AND fresh vision AND lock quality >= gate). Stamped in triggerRelease, paired with lastReleaseSeq_;
    // gates the autonomous lead learner so a blind/low-confidence outcome cannot move the lead.
    bool lastReleaseVisionConfident_ = false;
    // [TIMING FINDING 3] Whether the last release fired on a VISION-timed estimator
    // (green/predictive/pose-reactive) vs a feedforward/blind/timeout clock. Stamped in
    // triggerRelease, paired with lastReleaseSeq_, consumed by the per-type calibrator so a
    // single outcome corrects ONLY the knob its own fire path reads (offset for vision-timed,
    // clock for feedforward-timed) — never both (which double-counts the deadline motion).
    bool lastReleaseWasVisionTimed_ = false;
    // Banner-calibrate: the post-release meter self-grade for the pending release, STORED (not learned)
    // when bannerCalibration is on. setBannerVerdict() then vetoes it (banner GREEN -> EXCELLENT) or
    // confirms its EARLY/LATE direction (banner RED). Valid only while seq == lastReleaseSeq_.
    int pendingSelfGradeSeq_ = -1;
    double pendingSelfGradeErrorMs_ = 0.0;
    QString pendingSelfGradeVerdict_;
    // Banner auto-fallback: deadline by which the banner must have delivered a verdict for
    // the pending release; consecutive silent releases counted toward the fallback.
    double pendingSelfGradeDeadlineMs_ = -1.0;
    int bannerUnclearStreak_ = 0;
    // Latches true on the first real outcome (post-release meter calibration); thereafter
    // the internal learnFromRelease estimate (blind to true latency, wrong-signed for
    // predictive shots) defers to the real-outcome learner.
    bool outcomeFeedbackActive_ = false;
    // Scoped true only while an AUTHORITATIVE verdict (the feedback-text oracle, setShotVerdict)
    // is being applied, so learnFromOutcome may bypass calibrationFrozen for that one verdict
    // while the unreliable meter self-grade stays frozen.
    bool authoritativeVerdict_ = false;

    // --- Network sample-and-hold state -------------------------------------------------
    // Freshest measured offset (updated continuously); shot_.networkOffsetMs is latched
    // from it at shot start (delta-clamped vs the type's LOCK baseline) and held.
    double pendingNetworkOffsetMs_ = 0.0;
    double networkJitterEmaMs_ = 0.0;
    bool wifiMode_ = false;
    // Defense Mode disarm gate (true = shot automation live).
    // [CONCURRENCY N3] Written by the GUI-freeze watchdog thread (setArmed(false) after a >6s
    // stall) and read by process()/reevaluateScheduleOnFreshSample on the GUI thread. A plain
    // bool written cross-thread is a formal data race (UB); atomic (relaxed on x86 = a plain
    // load/store) removes the UB and documents the single-writer intent. Zero behavior change.
    std::atomic<bool> armed_{true};
    std::atomic<quint64> armRevocationGeneration_{0};
    quint64 handledArmRevocationGeneration_ = 0;

    // [ORION_GREEN_CENTER] last centring offset written to the log, so a stable window reports
    // once per shot instead of on every tick. -1 = nothing logged yet this shot.
    double greenCenterLoggedOffsetMs_ = -1.0;
    // [ORION_SLOW_METER_DEFER] Once-per-token latch for the reschedule_refused diagnostic --
    // the keep runs every 4ms tick while a transient sampler swing lasts (measured 8-30ms),
    // and one line per protected token is the whole story. Monotonic token ids make a reset
    // unnecessary.
    quint64 slowMeterDeferKeepLoggedToken_ = 0;

    // --- Sub-tick scheduler state -------------------------------------------------------
    double schedFireDeadlineMs_ = -1.0;   // -1 = nothing armed
    // [ORION_DEV_FIRE_OFFSET] Displacement carried by the armed token (displaced - undisplaced,
    // 0 = none). Callers that decide whether to re-arm compare their fresh candidate against the
    // UNDISPLACED deadline, derived as (schedFireDeadlineMs_ - this): comparing a clean candidate
    // to a displaced armed value would read the commanded offset as per-tick drift and churn
    // invalidate/re-arm every 4ms tick. Derived (not stored separately) so any path that moves
    // schedFireDeadlineMs_ directly (tick-lock nudge write-back, test state placement) keeps the
    // pair coherent; with the hook disarmed this is 0.0 and the comparisons are bit-identical.
    double schedFireAppliedDevOffsetMs_ = 0.0;
    // [ORION_DEV_FIRE_OFFSET] Dev-only commanded per-shot fire-time displacement, for the sweep
    // experiment that grades displaced fires with the game's own banner (the only instrument a
    // capture-side observation grid cannot contaminate). Compiled out of production builds and
    // armed ONLY by ORION_DEV_FIRE_OFFSET_SWEEP ("list:a,b,c" cycled per shot, or "uniform:lo:hi"
    // drawn per shot); every offset is clamped to +/-kDevFireOffsetMaxAbsMs. It deliberately
    // MISTIMES scheduled fires, so while armed every learner that rides the release instant
    // (phase constant, feedforward clocks, landing lead, lead-outcome gate) is fenced shut.
    bool devFireOffsetArmed_ = false;
    bool devFireOffsetEnvParsed_ = false;  // parse once; a config re-apply must not reset the cycle
    bool devFireOffsetUniform_ = false;
    double devFireOffsetUniformLoMs_ = 0.0;
    double devFireOffsetUniformHiMs_ = 0.0;
    QVector<double> devFireOffsetListMs_;
    int devFireOffsetNextIdx_ = 0;
    // armToken the cached draw belongs to. Sentinel-initialized (all-ones) so the very first
    // shot -- including test paths where armToken is still 0 -- gets a genuine draw.
    quint64 devFireOffsetShotToken_ = ~quint64{0};
    double devFireOffsetShotMs_ = 0.0;     // cached per-shot draw (stable across re-arms)
    double lastReleaseDevOffsetMs_ = 0.0;  // offset carried by the token that actually fired
    [[nodiscard]] double devFireOffsetForShotMs();
    void parseDevFireOffsetEnv();
    quint64 schedFireToken_ = 0;          // increments per arm; pairs confirms to arms
    quint64 schedFireConfirmedToken_ = 0; // last token confirmed by the fire thread
    quint64 schedFireFailedToken_ = 0;    // current token rejected by the worker route
    double schedFireActualMs_ = -1.0;     // actual fire time of the confirmed token
    // Shot ownership captured when this token was armed. Token equality prevents
    // most stale confirms; these ids make the correlation explicit and guard
    // against any future token-lifecycle regression.
    quint64 schedFirePhysicalShotEpoch_ = 0;
    quint64 schedFireShotAttempt_ = 0;
    // Route generation captured atomically with the timing decision. A token
    // cannot migrate between direct Pipe and either ViGEm backend while queued.
    quint64 schedFireRouteGeneration_ = 0;
    LatencyControllerRoute schedFireRoute_ = LatencyControllerRoute::None;
    // The release attribution the armed deadline will carry when it fires (the holding
    // frames in between surface the honest release_scheduled code instead).
    QString schedFirePlan_;
    QString schedFireReason_;
    QString schedFireCode_;
    // [ORION_ARMED_SOURCE] Arm-time snapshot of the tip decision that created the CURRENT
    // token: the predictor source string exactly as the "TIP RESERVATION:
    // disposition=reservation_promoted" line logs it, that decision's combined sigma, and
    // the fill / command ETA at the arm instant. Written only by the two
    // AutonomousMeterVision live-tip arm sites (tick + subtick mirror) immediately after
    // their scheduleFire() succeeds; every OTHER successful arm resets it to absent inside
    // scheduleFire(), so a stale live-tip attribution can never leak onto a feedforward /
    // pose / fused / calibration token. On a re-arm the snapshot is overwritten, so it
    // always describes the token that actually fires. Consumed into
    // ShotContext::armedPredictor* at the token-consume sites; never read by any decision.
    QString schedFireArmedSource_;
    double schedFireArmedSigmaMs_ = -1.0;
    double schedFireArmedFillPct_ = -1.0;
    double schedFireArmedCommandEtaMs_ = -1.0;
    // True only for deadlines derived from live meter vision. Legacy timing
    // cancels on any non-genuine payload. Strict autonomous timing may retain an
    // already-armed deadline through a blink only until this deadline's immutable
    // source-frame/lead authority expiry; it never creates a new stale deadline.
    bool schedFireRequiresGenuineFrame_ = false;
    int schedFireVisionEpoch_ = -1;
    bool schedFireRequiresPoseFrame_ = false;
    int schedFirePoseEpoch_ = -1;
    quint64 schedFirePoseArmToken_ = 0;
    // Engine-clock expiry of the evidence that authorizes a vision arm. Meter and pose schedules
    // both carry a finite lease. [ORION_ROLLING_LEASE] For meter tokens this is no longer frozen to
    // the arming frame: refreshScheduledFireAuthorityLease() rolls it forward on every genuine
    // frame in the same vision epoch, and the tick/hold fences retire the token the moment `now`
    // passes it. The precise worker independently enforces its own arm-time snapshot so a GUI
    // stall cannot turn old evidence into a late release.
    double schedFireAuthorityExpiryMs_ = -1.0;
    // [ORION_TRANSIENT_INVALID] Engine-clock instant at which the tip prediction most recently
    // STOPPED being valid while a token was armed, or -1 whenever the prediction is valid.
    // A lone frame of registration disagreement spikes the combined sigma (measured: 120 ms on a
    // single frame against a 34 ms running sigma) and used to destroy a healthy token up to 58 ms
    // before its own deadline, with no later frame able to re-arm it. The physical trajectory did
    // not change; only one observation of it did. The armed deadline is held across a transient no
    // longer than one source cadence and is retired normally once invalidity persists past that.
    // This never manufactures a deadline: the token fires only at the instant a VALID prediction
    // already proved future, and the epoch/authority/uncovered fences still run every tick ahead
    // of this guard.
    double tipPredictionInvalidSinceMs_ = -1.0;
    // [ORION_ARM_GATE_TRACE] One diagnostic line per refusal REASON per shot attempt.
    // scheduleFire() runs on every 4 ms tick, so an unbounded log here would be ~250 lines/s.
    quint32 schedFireRejectLoggedMask_ = 0;
    quint64 schedFireRejectLogArmToken_ = 0;
    // --- Early cancellable tip reservation ----------------------------------------------
    // A RESERVATION is a continuously-refined causal PLAN. It is not an armed command and it
    // never submits anything by itself: scheduleFire() drives a spin-wait worker and can only
    // arm inside a 22-64 ms horizon, so it must stay the sole submission path.
    //
    // What this closes: between evidence-backed ownership (~14-25% fill) and the instant the
    // canonical decision first becomes valid (~47-58% fill, because sampler sigma grows 0.20 ms
    // per ms of horizon against a 65 ms combined cap) the engine tracked NOTHING. It therefore
    // discovered its own command deadline only at that late instant, and on 2026-08-03 it found
    // fireAt already in the past on 8 of 10 owned shots. The reservation makes the deadline
    // observable from ownership onward, binds it to the exact physical identity, and lets an
    // abort say WHICH of the two failures happened -- the estimate never became authoritative in
    // time, or authority was lost after it did.
    //
    // Deliberately NOT a gate: a provisional far-horizon estimate is too weak to abort on. Adding
    // an abort here would kill shots that recover as the fit tightens. The fail-closed authority
    // stays exactly where it was, at the fireAtMs <= now test on a VALID decision.
    struct AutonomousTipReservation {
        bool active = false;
        double createdMs = -1.0;        // engine clock at reservation_created
        double firstTipAbsMs = -1.0;    // the very first provisional tip estimate
        double firstFillPct = -1.0;     // meter fill when the reservation was created
        double fireAtMs = -1.0;         // latest refined command deadline (tipAbs - lead)
        double tipAbsMs = -1.0;         // latest refined tip estimate
        double leadMs = -1.0;           // route lead authority used for the latest refinement
        double leadSdMs = -1.0;
        double sigmaMs = -1.0;          // combined predictor sigma at the latest refinement
        QString source;                 // predictor source string at the latest refinement
        bool everValid = false;         // did the canonical decision ever become valid while held
        double firstValidMs = -1.0;     // engine clock of the first valid decision
        // Was the command deadline EVER in the future while this plan was held? If it never was,
        // the miss is not a prediction failure at all -- the lead is simply too large for this
        // shot's geometry, which is an authority problem and points at the route prior rather than
        // at the estimator. Conflating the two is what made the live aborts unattributable.
        bool everFuture = false;
        // Was there ever a moment we could ACTUALLY have acted on -- decision valid AND the
        // deadline still ahead of us? This is the only honest test for "we had our chance and lost
        // it". everValid alone cannot serve: the abort path runs only when the decision IS valid,
        // and the per-frame refinement happens earlier in the same tick, so everValid is always
        // true by the time the miss is classified. Using it would label every miss
        // authority_lost_before_submit and permanently hide the causality failure this whole
        // change exists to expose.
        bool everValidAndFuture = false;
        // Was there ever a tick on which scheduleFire() could actually have said yes -- valid,
        // still ahead, AND inside the adaptive arming horizon? everValidAndFuture is true of
        // essentially every shot (a deadline is valid and future for its whole approach), so it
        // labelled 106 of 115 live aborts "authority_lost_before_submit" while 32 of 42 had never
        // armed anything. everArmable is the narrow, honest version of that question.
        bool everArmable = false;
        int updates = 0;
        bool promoted = false;          // a scheduleFire() token was armed from this reservation
        // Refinement runs on every 4 ms tick and every fresh frame; logging every one of those
        // would put ~250 lines/s into the live log and drown the events that matter. Only a
        // materially moved deadline is worth a line.
        double loggedFireAtMs = -1.0;
        // Identity this reservation is bound to. Any mismatch cancels it.
        quint64 physicalShotEpoch = 0;
        quint64 armToken = 0;
        int visionEpoch = -1;
        quint64 routeGeneration = 0;
        LatencyControllerRoute route = LatencyControllerRoute::None;
    };
    AutonomousTipReservation tipReservation_;
    void updateAutonomousTipReservation(const AutonomousTipDecision& decision, double now);
    void cancelAutonomousTipReservation(const QString& reason);
    [[nodiscard]] bool autonomousTipReservationIdentityCurrent() const noexcept;

    // GUI-thread coalescing for reevaluateScheduleOnFreshSample: engine-clock ms of the last
    // ACCEPTED fresh-sample re-evaluation. The event-driven sidecar can burst 60+ meter
    // messages/s onto the GUI thread between 4ms input ticks; the mirrored predict math runs
    // at most once per half input tick (see the gate in the function). -1 = never ran.
    double lastFreshReevalMs_ = -1.0;

    // Per-type ACQUIRE->LOCK runtime streak counters (NOT persisted — only the phase is).
    // calGreens_ counts consecutive in-green outcomes toward LOCK; calMisses_ counts
    // consecutive off-target outcomes toward UNLOCK. Both reset on the opposite verdict.
    QMap<QString, int> calGreens_;
    QMap<QString, int> calMisses_;
    // Divergence guard (NOT persisted): per-type count of consecutive RAILED (|errorMs| == max-clamp)
    // verdicts of the SAME sign without reaching green; once it exceeds calDivergenceGuardShots the
    // clock integration freezes for that type. calRailSign_ holds the railed sign of the current run.
    QMap<QString, int> calRailStreak_;
    QMap<QString, int> calRailSign_;
    // Autonomous global-lead divergence guard (NOT persisted). The autonomous branch integrates the
    // post-release meter self-grade into the ONE global learnedLatencyMs, but that self-grade
    // false-LATEs at the dead-top with a CONSTANT deflate error (meterRecedeLatePct*meterMsPerPct).
    // Left ungoverned it climbed the lead to the globalLatencyClampMs (the live LATE-66 runaway),
    // because the per-type rail guard sits BELOW the autonomous branch's early return. This tracks the
    // consecutive same-sign, non-green outcome streak; once it exceeds calDivergenceGuardShots the
    // global-latency integration FREEZES, so a stream of false LATE can't rail the lead. A green
    // (|errorMs| <= 0.5) or a sign flip resets it so a genuine, converging bias is still corrected.
    int autoLeadDivergeStreak_ = 0;
    int autoLeadDivergeSign_ = 0;
    // RC-4(c) ARTIFACT-SIGNATURE guard (NOT persisted). The same-sign streak guard above never trips on
    // the ALTERNATING dead-top artifact (LATE +66 / EARLY -90 = fixed meterRecede / overshoot magnitudes),
    // because the sign flips every shot so the streak resets to 1. That two-mode, near-zero-variance
    // fixed-value histogram is itself the artifact fingerprint. Buffer the recent non-green outcomes and
    // FREEZE the global lead whenever they collapse onto <=2 fixed magnitudes covering most of the window
    // — regardless of sign. A genuine converging grade stream is spread across many magnitudes -> no freeze.
    static constexpr int kAutoLeadArtifactWindow = 8;    // outcomes retained for the histogram
    static constexpr int kAutoLeadArtifactMinShots = 6;  // need this many before judging a signature
    std::deque<double> autoLeadRecentErr_;
    [[nodiscard]] bool isArtifactOutcomeSignature() const;
    // RC-3/C3 legacy diagnostic latch: emit BLIND_SUPPRESSED at most once per shot if the
    // defense-in-depth check is ever reached. Reset in beginShot.
    bool blindSuppressLoggedThisShot_ = false;
    // B2c/C5: last IDLE releaseReason emitted on engineDiagnostic. processIdle re-stamps its gate
    // reason every 4ms tick, so only TRANSITIONS are logged — that turns the arm gate into a
    // greppable event stream ("why is a new shot not arming?") without spamming the log.
    QString lastIdleReasonLogged_;

    // --- Post-release meter calibration capture (the closed-loop ground truth) ---------
    struct MeterCalSample {
        double fillPct = 0.0;
        double greenStartPct = -1.0;
        double greenEndPct = -1.0;
        double greenCenterPct = -1.0;
        // [ORION_GREEN_TRUTH] Was the green window OBSERVED on this frame, or substituted?
        //
        // MeterDetector falls back to `config_.greenWindowStartPct` -- which is the user's Release
        // Threshold slider -- whenever the green scan fails, and the landing line then logged that
        // slider as if it were a measurement. Measured across 242 graded landings: 175 (72.3%)
        // carry green_start of exactly 98.00 (the slider), and 37 of those have a peak_fill BELOW
        // 98.00, i.e. a "green window" the meter provably never entered. Only 67 rows were real.
        //
        // greenConfidence is 0.0 exactly when the scan failed, so carrying it is what separates a
        // measurement from a default. Without it, every green statistic this project has ever
        // computed silently averaged the slider into the answer -- including the window width that
        // the whole tip-timing ceiling is divided by.
        double greenConfidence = 0.0;
        double confidence = 0.0;
        bool accepted = false;
        double bx = 0.0;                  // meter bbox center (px) - to detect a MOVING vs SETTLED meter
        double by = 0.0;
        // [ORION_COURT_POSITION] the same bbox center expressed FRAME-NORMALIZED (0..1), taken
        // from the same immutable detector snapshot (bbox_wh) as bx/by. -1 = frame dims absent.
        // Normalized rather than px because it must stay comparable across a resolution handoff.
        double nx = -1.0;
        double ny = -1.0;
        // Engine-clock stamp (nowMs at ingestion). Grading v2's trajectory classification needs
        // real per-sample timing — index-based timing breaks on dropped/stale frames.
        double tMs = 0.0;
    };
    bool meterCapActive_ = false;
    int meterCapSeq_ = 0;
    QString meterCapShotType_;
    double meterCapDeadlineMs_ = 0.0;     // soft deadline: grade now IF the meter has settled
    double meterCapHardDeadlineMs_ = 0.0; // hard cap: grade (or skip) regardless once reached
    double meterCapPeakFillPct_ = 0.0;
    // Fill at the instant the release command was SUBMITTED, snapshotted alongside the peak so
    // the two can be logged as one seq-paired pair. The "Release timing:" line's peakFill has
    // always been byte-identical to fillAtRel (verified: all 475 rows across
    // logs/orion_native.log{,.1}) because it is read at submit time, when the meter has not yet
    // travelled. The real landing evidence only exists here, after the capture window.
    double meterCapFillAtReleasePct_ = -1.0;
    // [ORION_USER_LEAD] the meter's rise velocity (%/ms) for THIS shot, snapshotted when the
    // release command was submitted. travel_pp / velocity is the lead, so the divisor has to be
    // the velocity the meter actually had — NOT config_.globalRiseVelocityPctMs, whose 0.226
    // default is ~25% above the measured live rise (0.174-0.188) and would seed every fresh
    // install ~25% short. No honest per-shot velocity -> no sample (see recordLandingLeadSample).
    double meterCapRiseVelocityPctPerMs_ = 0.0;
    // [ORION_COURT_POSITION] WHY THIS EXISTS.
    // The bot has never had a court-position or shot-distance signal. classifyShotType() reads
    // only leftStickX/Y and l2 and says so in its own comment ("The bot also cannot see court
    // position from the controller"); the detframes `zone` column is unpopulated on every row
    // (12,431/12,431 == 'none' in logs/batch_20260803/detframes_batch.csv); pose is off in
    // production; contest_level is produced by nothing.
    //
    // But the shot meter is PLAYER-anchored, not HUD-anchored. Measured over the 4,560
    // green-confirmed rows of that same batch, the frame-normalized bbox CENTER spans
    // x/W 0.054-0.989 (sd 0.281) and y/H 0.276-0.787 (sd 0.115). A fixed HUD element cannot do
    // that. So the meter's own normalized position IS a court-position proxy, and it already
    // arrives on every detection at zero extra cost.
    //
    // WHAT IT WOULD PROVE. Stamping it per shot makes "does the bot's timing error depend on
    // where the shot was taken from" (the half-court question) answerable by grouping any future
    // batch's `Release landing:` rows on meter_x/meter_y — no special capture, no new detector,
    // no pose. meter_jump additionally separates a settled meter from one still sliding with the
    // player, which is the confound that would otherwise masquerade as a position effect.
    //
    // Telemetry only: nothing reads these back into timing, scheduling, or gating.
    double lastMeterNormX_ = -1.0;        // last accepted detection, frame-normalized center
    double lastMeterNormY_ = -1.0;
    double meterLockPrevNormX_ = -1.0;    // previous accepted detection in the current lock run
    double meterLockPrevNormY_ = -1.0;
    // Max single-frame normalized center jump within the CURRENT contiguous accepted-detection
    // run. Resets when the run breaks, so it describes one unbroken lock rather than a session.
    double meterLockMaxJumpNorm_ = -1.0;
    double meterCapNormXAtRel_ = -1.0;    // court position snapshotted at release-command submit
    double meterCapNormYAtRel_ = -1.0;
    // [ORION_LANDING_FEATURES 2026-08-12] Pipeline state AT THE FIRE INSTANT, sample-and-held for
    // the same reason meterCapNormXAtRel_ is: the capture window runs hundreds of ms past the
    // command, so a grade-time read describes a different world than the one the shot was fired in.
    //
    // WHY THIS EXISTS. The rich at-fire dump ("TIP DEADLINE DECISION") is emitted only on MISSES,
    // and RTT-at-fire is latched into shot_.networkOffsetMs and then never logged anywhere. So the
    // shots that actually matter -- the graded ones -- carry no record of the pipeline state that
    // produced them, and "is the Fade residual RTT-driven, velocity-driven, or random?" is
    // unanswerable from the log. Every one of these is already computed; none was reaching disk.
    //
    // Deliberately only TWO members: armed_sigma_ms and command_eta_ms already reach the log on
    // the "Outcome identity" line (lastReleaseArmedSigmaMs_ / lastReleaseArmedCommandEtaMs_), and
    // the rise velocity is already latched as meterCapRiseVelocityPctPerMs_. Duplicating them here
    // would create a second source of truth for the same numbers. These two had no path to disk.
    //
    // OBSERVABILITY ONLY. Nothing reads these back into timing. -1 = not available for this shot.
    double meterCapFrameAgeAtRelMs_ = -1.0;       // capture staleness of the deciding sample
    double meterCapNetworkOffsetAtRelMs_ = -1.0;  // RTT/network offset latched for this shot
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] Set by applySquarePassthrough on every process() tick;
    // read by the release-ownership trace so a steal is never mistaken for an engine override.
    bool squarePassthroughInjected_ = false;
    // [ORION_LEAD_CEILING_WARN 2026-08-12] De-dup key for the load-time over-ceiling advisory,
    // encoding the (base lead, offset) pair it was last emitted for. NaN = never emitted.
    double leadCeilingWarnKey_ = std::numeric_limits<double>::quiet_NaN();
    QVector<MeterCalSample> meterCapSamples_;
    // === [ORION_TIP_PHASE] per-landing observation of the animation constant ===========
    // The anchor is snapshotted at release-command submit (the same sample-and-hold shape
    // meterCapFillAtReleasePct_ uses) because the post-release capture window can outlive the
    // shot: a new beginShot() would otherwise clear shot_.fillPhaseAnchorMs out from under the
    // landing that is still being measured, and the observation would silently pair the NEXT
    // shot's anchor with THIS shot's stop.
    double meterCapPhaseAnchorMs_ = -1.0;
    // [ORION_TIP_PHASE_LADDER] and WHICH level that anchor dates. Snapshotted with the anchor for
    // the same reason: the capture window outlives the shot. Without it the learner would average
    // a level-40 observation (~52 ms shorter animation remaining) against level-30 ones and walk
    // the learned physical constant down by however many No Dips happened to be in the window --
    // a silent re-aim of every OTHER shot type, caused by a shot type that used to contribute
    // nothing at all.
    double meterCapPhaseAnchorLevelPct_ = -1.0;
    // End-of-rise state, all on the CAPTURE clock -- the same clock the anchor is on. Mixing in
    // the engine-arrival clock (MeterCalSample::tMs) would inject the per-frame decode/IPC age
    // straight into the learned constant, which is exactly the error this predictor exists to
    // avoid. Once the quiet window confirms a stop it is FROZEN, mirroring the offline
    // estimator's break: a later second rise belongs to a different animation stage.
    double meterCapPhaseStopMs_ = -1.0;
    double meterCapPhaseRunMaxPct_ = -1.0;
    bool   meterCapPhaseStopConfirmed_ = false;
    // [ORION_STOP_CORROBORATE] Pending (uncommitted) stop re-open — see stopReopenCorroborate.
    // A single-frame fill SPIKE above the settled plateau must not re-date the stop, so a
    // re-open is held here until a second accepted sample confirms the rise really resumed.
    // -1 = nothing pending. The prior run max is kept so the spike itself never owns it.
    // [ORION_AIM_FREEZE] The aim value latched at session start (restored prior, else the seed).
    // Set once in applyConfig; never updated by learning. -1 = nothing to freeze onto.
    double frozenAimPhysicalMs_ = -1.0;
    double meterCapPhaseReopenPendingMs_ = -1.0;
    double meterCapPhaseReopenPendingFillPct_ = -1.0;
    double meterCapPhaseReopenPriorMaxPct_ = -1.0;
    // [ORION_STOP_SUBFRAME] Capture-clock (captureAlignedMs, fillPct) samples buffered while the
    // post-release capture window is open, ONLY when stopDatingSubframe is on (flag OFF keeps the
    // engine bit-identical). MeterCalSample::tMs cannot be reused here: it is engine-arrival time
    // and would fold per-frame decode/IPC age into the refined stop — the exact error the
    // capture-aligned stop dating exists to avoid.
    QVector<QPair<double, double>> meterCapPhaseCapSamples_;
    // Refined sub-frame stop for the CURRENT capture window, or -1. Computed lazily at
    // measurement time (recordPhaseConstantSample) from the buffer + the committed snapped stop,
    // so it composes with stopReopenCorroborate for free: whatever snapped stop the corroboration
    // logic committed is the one refined around.
    [[nodiscard]] double refinePhaseStopSubframeMs() const;
    // Rolling window of (stop - anchor) landings and the median published from a FULL window.
    // < 0 = not yet learned; the seed in config_ carries the session until then.
    QVector<double> phaseConstantSamplesMs_;
    double learnedPhasePhysicalMs_ = -1.0;
    // [ORION_PHASE_COLD_START] The value this session STARTED from -- the restored prior when
    // learning.json carried one, otherwise the seed. The partial-window shrink must move away
    // from this, not from the hardcoded seed; see recordPhaseConstantSample().
    double phaseShrinkBaseMs_ = -1.0;
    // === [ORION_PRESS_ANCHOR] press-wall dating + the press->tip learner =================
    // Wall time (engine clock) of the LAST physical shot press, captured in
    // setPhysicalShotEpoch on the same synchronous GUI tick the input hook's raw edge was
    // polled — i.e. before any video/delay pipeline. Keyed by epoch so a stale press can
    // never date a different shot: consumers require pressWallEpoch_ == the shot's epoch.
    quint64 pressWallEpoch_ = 0;
    double pressWallMsForEpoch_ = -1.0;
    // Sample-and-hold snapshots for the landing measurement (same reason as
    // meterCapPhaseAnchorMs_: the post-release capture window outlives the shot, and a
    // beginShot() during it must not re-pair this landing with the NEXT press).
    quint64 meterCapPhysicalEpoch_ = 0;
    double meterCapPressWallMs_ = -1.0;
    double meterCapAppliedDelayMs_ = 0.0;
    // Sidecar l_fixed (measured latency with rtt_now + tick_wait stripped) from the latest
    // valid latency snapshot — the V (video-pipeline latency) estimate the press-anchored
    // observation subtracts and the predictor adds back. 0 = not provided.
    double measuredLFixedMs_ = 0.0;
    // In-session per-type observation window: (press->tip wall ms, weight). H-confidence
    // landings weigh 1.0, M-confidence 0.5, L is never recorded. Bounded at
    // kPressAnchoredWindow entries; blends with the restored prior by weight so restarts
    // neither forget the accumulated calibration nor double-count it.
    QMap<QString, QVector<QPair<double, double>>> pressTipWindow_;
    // The calibration restored from settings at the FIRST applyConfig (latched: our own
    // write-back re-enters applyConfig via the save path, and re-snapshotting the freshly
    // blended values while the session window still holds the same samples would count them
    // twice). pressTipPrior*_ stay fixed for the session; the published maps in config_ are
    // prior+window blends recomputed on each accepted observation.
    bool pressAnchoredPriorsLoaded_ = false;
    QMap<QString, double> pressTipPriorMs_;
    QMap<QString, double> pressTipPriorSigmaMs_;
    QMap<QString, double> pressTipPriorW_;
    static constexpr int kPressAnchoredWindow = 100;
    static constexpr double kPressAnchoredFactorySigmaMs = 60.0;
    static constexpr double kPressAnchoredSigmaFloorMs = 8.0;
    static constexpr double kPressAnchoredLearnMinMs = 150.0;
    static constexpr double kPressAnchoredLearnMaxMs = 2500.0;
    // V estimate for the press-anchored frames: l_fixed when the sidecar provided one, else
    // the loop authority as a degraded stand-in (srcOut: "l_fixed" / "loop" / "none").
    [[nodiscard]] double pressAnchoredVideoLatencyMs(QString* srcOut = nullptr) const;
    // Current learned press->tip / sigma / weight for a type (prior+window blend; falls back
    // to the factory maps in config_ and the wide factory sigma).
    [[nodiscard]] double pressAnchoredLearnedTipMs(const QString& shotType) const;
    [[nodiscard]] double pressAnchoredLearnedSigmaMs(const QString& shotType) const;
    [[nodiscard]] double pressAnchoredLearnedWeight(const QString& shotType) const;
    // Fold one accepted observation into the window, recompute the blended per-type
    // calibration into config_, and emit pressAnchoredCalibrationUpdated for persistence.
    void recordPressToTipObservation(const QString& shotType, double pressToTipMs,
                                     double weight);
    // Phase-A collector: emit the once-per-landing "PRESS-TIP OBSERVATION:" line (and feed
    // the learner on H/M confidence). Called from evaluatePostReleaseMeter on every landing
    // outcome; never from an aborted shot (aborts do not reach the landing evaluator).
    // `samples` is the landing's capture-window buffer (the evaluator's local copy — the
    // member vector is already cleared by then), used only for the peak-sample fallback
    // when the stop detector never confirmed an end of rise.
    void emitPressTipObservation(int seq, const QString& shotType, bool graded,
                                 bool greenObserved,
                                 const QVector<MeterCalSample>& samples);
    // [ORION_LEAD_CONFLICT] Bookkeeping for the structurally-unschedulable-lead diagnosis
    // (Shot Lead >= tip constant - margin; see maxSchedulableTipLeadMs()). The warned pair
    // de-duplicates the config-time advisory so a settings save that changes nothing does not
    // repeat it; the counter feeds the live miss diagnostic and the UI signal.
    int tipLeadConflictMisses_ = 0;
    double tipLeadConflictWarnedLeadMs_ = -1.0;
    double tipLeadConflictWarnedConstMs_ = -1.0;
    // [ORION_METER_DELAY_LEAD_STARVATION] Applied inbound meter delay (ms) and whether the
    // delay condition is settled, pushed via setMeterDelayCondition(). 0 = no delay: every
    // predicate below is inert and the engine is bit-identical to the pre-delay build.
    // Same-thread with process() (OrionAppController owns both the MeterDelayController and
    // the input poll on one thread), so plain members suffice.
    double meterDelayAppliedMs_ = 0.0;
    bool meterDelaySettled_ = true;
    // [ORION_TEMPO_BRIDGE_LIVE task #36] Bridge-live gate state (see the public setters).
    // wired_ false = standalone/unit engine, gate permanently open (legacy behavior).
    bool tempoBridgeGateWired_ = false;
    bool tempoBridgeDelayEngaged_ = false;
    bool tempoBridgeInterceptApplying_ = false;
    QString tempoBridgeStateLabel_ = QStringLiteral("unwired");
    double tempoBridgeHealthBeatMs_ = -1.0;
    // Generous on purpose: the beat sources (bridge telemetry, VeniceNet snapshot changes)
    // are event-driven, not strictly periodic. A stale window only ever demotes NEW presses
    // to passthrough — the safe direction — so false-negatives cost a remap, never a press.
    static constexpr double kTempoRemapBridgeHealthFreshMs = 5000.0;
    [[nodiscard]] bool tempoRemapBridgeLive(double now) const noexcept;
    // Once-per-shot latch for the starved-source diagnostic (reset in beginShot).
    bool meterDelayStarvedLoggedThisShot_ = false;
    // True when the applied meter delay has pushed the consumed lead past the visible-evidence
    // ceiling: no estimate dated from the delayed video can be BOTH schedulable and accurate.
    [[nodiscard]] bool visibleEvidenceLeadStarvedByMeterDelay(double leadMs) const noexcept;
    // Largest applied delay at which this lead's wall-clock component (lead - the delay-keyed
    // offset, i.e. the loop latency the user calibrated at delay 0) still fits the visible
    // runway. The single actionable number for the operator: run the demo at/below this delay.
    [[nodiscard]] double maxMeterDelayForLeadMs(double leadMs) const noexcept;
    // === [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] ==========================================
    // The delay-condition lead offset ACTUALLY IN FORCE right now: config_.meterDelayLeadOffsetMs
    // while a meter delay is applied and the value is finite and in band, 0.0 otherwise. The env
    // lead sweep suppresses it for the same reason it out-ranks the user lead — a dev sweep must
    // stay byte-identical to its design. Every consumer reaches the offset through this one
    // accessor so "delay applied" has exactly one definition.
    [[nodiscard]] double appliedMeterDelayLeadOffsetMs() const noexcept;
    // Largest delay-condition offset this rig can still schedule on top of the operator's
    // delay-0 Shot Lead: maxSchedulableTipLeadMs() - baseLead. Negative/zero means the delay-0
    // lead already sits at the visible-evidence ceiling and NO delayed condition is reachable
    // through an in-video anchor. This is the number the operator can act on; it replaces the
    // "just type a bigger Shot Lead" advice that the 2026-08-09 session proved unactionable.
    [[nodiscard]] double maxMeterDelayLeadOffsetMs() const noexcept;
    // One line, de-duplicated on the applied-delay value, when a meter delay engages while the
    // delay-condition offset is still 0 — i.e. the engine is about to fire the delayed condition
    // with the lead the operator calibrated at delay 0. Before this line that failure was
    // COMPLETELY SILENT: visibleEvidenceLeadStarvedByMeterDelay() only trips above the ceiling,
    // which a delay-0-calibrated lead never is. Diagnostic-only; changes no scheduling.
    void maybeWarnMeterDelayLeadUncalibrated();
    double meterDelayLeadUncalibratedWarnedMs_ = -1.0;
    // [ORION_AIM_FREEZE] De-dup pair for the frozen-vs-measured divergence warning (values
    // quantised to 5 ms when compared). A repeat of the SAME disagreement stays quiet across
    // applyConfig churn and landings; a NEW manual value or a materially moved median warns
    // again. -1 = never warned.
    double tipTimingDivergenceWarnedFrozenMs_ = -1.0;
    double tipTimingDivergenceWarnedMeasuredMs_ = -1.0;
    // [ORION_LEAD_CONFLICT] De-dup pair for the lead-vs-validated-authority disagreement
    // advisory (10 ms grid -- ingestion runs per telemetry frame and a posterior refining by a
    // millisecond per label must not re-warn). -1 = never warned.
    double leadAuthorityWarnedLeadMs_ = -1.0;
    double leadAuthorityWarnedAuthorityMs_ = -1.0;
    void maybeWarnLeadAuthorityDisagreement();
    // [ORION_USER_LEAD 2026-08-08] Last actuationLeadMeasured value published from the validated
    // authority (1 ms hysteresis; see the telemetry ingestion site). -1 = never published.
    double lastPublishedMeasuredSeedMs_ = -1.0;
    // [ORION_LEAD_CONFLICT] Largest actuation lead the phase member can still schedule under the
    // ACTIVE tip constant. The phase estimate is born with tip_eta ~= constant - decision
    // latency, so any lead within kTipLeadScheduleMarginMs of the constant leaves no tick with a
    // future command deadline. Diagnostic-only -- never consulted by scheduling.
    [[nodiscard]] double maxSchedulableTipLeadMs() const noexcept;
    // [ORION_LEAD_CONFLICT] Names where the CONSUMED lead actually came from: "user" (the owner
    // typed it), "seed" (auto-installed measured seed, not user-chosen), "authority" (estimator
    // posterior / factory prior), "none". Exists because lead_kind= on the decision lines names
    // only the AUTHORITY -- when the in-band Shot Lead replaces the authority the pair
    // "lead_kind=validated lead_ms=320" asserts a provenance the value does not have, which is
    // exactly what misdirected the 2026-08-08 investigation.
    [[nodiscard]] QString actuationLeadSourceLabel(double consumedLeadMs) const noexcept;
    // [ORION_AIM_FREEZE] One line, in the effective (Tip Timing card) frame, when the frozen
    // manual value and the instrument's own median disagree by more than
    // kTipTimingDivergenceWarnMs. De-duplicated via the warned pair above. Diagnostic-only:
    // never changes the consumed constant, the persisted slots, or the freeze.
    void maybeWarnTipTimingDivergence(double frozenPhysicalMs, double measuredPhysicalMs);
    double maxFillThisShot_ = 0.0;        // peak fill seen during the active shot, for recede/LATE
    void startPostReleaseMeterCapture(double now);
    void evaluatePostReleaseMeter();
    // [ORION_GRADE_V2] Phase-2 trajectory grading (plan A3): classifies the post-release
    // trajectory (Case G/R/D + dead zone) off the per-sample MeterCalSample.tMs series, emits a
    // machine-parseable "GradeV2:" fusedDiagnostic line, and — as the SOLE writer — trims
    // shotTypeLearnedOffsetMs directly. Called from evaluatePostReleaseMeter under the flag with
    // the settled-run medians already computed (F_stop = settled fill, gs/ge/gc = green band).
    void evaluatePostReleaseMeterV2(const QString& shotType, int seq, double peak, double fStop,
                                    double gs, double ge, double gc,
                                    const QVector<MeterCalSample>& samples);
    // [ORION_GRADE_V2] v2 artifact guard: >= 6 consecutive same-sign applied errs with stdev
    // < 3ms is a grader artifact fingerprint (a real converging stream spreads + shrinks) ->
    // freeze updates for 10 shots + a fusedDiagnostic log.
    [[nodiscard]] bool gradeV2ArtifactTripped() const;
    std::deque<double> gradeV2RecentErr_;     // applied (non-dead, non-frozen) errs, window 8
    int gradeV2FreezeShots_ = 0;              // graded shots left in an artifact freeze
    // Session-start snapshot of shotTypeLearnedOffsetMs (applyConfig): the v2 trim's cumulative
    // drift is bounded to ±gradeV2DriftBoundMs around THIS baseline per bucket.
    QMap<QString, double> gradeV2OffsetBaseline_;
    // Select the SETTLED frozen-marker frames: the latest run of clean, near-top-green frames whose
    // bbox is static (the player has landed / the meter stopped sliding) with a stable fill. A moving
    // fade/Go-To meter never forms such a run -> returns empty -> the shot is NOT graded (no false
    // EXCELLENT off a moving read). Standstill is static throughout -> the whole window qualifies.
    QVector<MeterCalSample> meterSettledFrames(const QVector<MeterCalSample>& samples) const;
    bool meterCapHasSettledRun() const;
    // [ORION_USER_LEAD 2026-08-08] fold one post-release landing into the travel_pp/velocity
    // window and log the running median ("Lead measurement:" line). DIAGNOSTIC ONLY -- the proxy
    // is refuted as a lead instrument and no longer publishes actuationLeadMeasured; see the
    // note inside for the refutation and where the seed now comes from.
    void recordLandingLeadSample(double peakFillPct, double fillAtReleasePct);
    // [ORION_TIP_PHASE] Fold this shot's genuine sample into the anchor-crossing detector. One
    // helper, called from EVERY sampler_.addSample() site, so the ownership-seed replay and the
    // live path cannot disagree about when the meter crossed the anchor.
    void notePhaseAnchorSample(double fillPct, double captureMs);
    // [ORION_TIP_PHASE] Fold one completed landing into the learned PHYSICAL constant.
    void recordPhaseConstantSample();
    // [ORION_TIP_PHASE] The constant actually used: learned physical term (once a full window
    // exists) plus the fixed aim-preservation offset. See RemapConfig::tipPhaseSeedPhysicalMs.
    [[nodiscard]] double effectiveTipPhaseConstantMs() const noexcept;
    // [ORION_ANCHOR_BASE20] ms to ADD to a canonically-stored (base-30) phase prior to express
    // it at the active base anchor, and to SUBTRACT before persisting. 0 with the flag off.
    [[nodiscard]] double phasePriorShiftMs() const noexcept;
    // [ORION_TIP_PHASE_LADDER] Signed ms to add to the constant for a shot dated at levelPct.
    // Exactly 0 at tipPhaseAnchorPct, so the ladder is arithmetically inert for every shot that
    // anchors where it always did.
    [[nodiscard]] double tipPhaseLevelAdjustmentMs(double levelPct) const noexcept;
    // [ORION_TYPE_TRIM] Signed ms to add to the tip-phase constant for this shot type, and to
    // subtract from the landing observation before the learner's band check (symmetric by
    // construction so the pooled learner cannot fight the trim). 0 unless
    // tipPhaseTypeTrimEnabled and the exact classifier label has an entry; clamped to
    // single-digit ms either way.
    [[nodiscard]] double tipPhaseTypeTrimMs(const QString& shotType) const noexcept;
    // [ORION_TIP_PHASE_IMMINENT] True when the meter is rising and sits just below the anchor, so
    // a non-phase estimate must not release yet. Every branch is a refusal, so an unreadable
    // input falls through to existing behaviour rather than holding.
    [[nodiscard]] bool phaseAnchorImminent(double fillPct,
                                           double slopePctPerMs) const noexcept;
    // [ORION_RUNG_IMMINENT] The level the imminent hold is waiting for: the base anchor
    // (flag OFF -- bit-identical to the pre-flag predicate, including a negative belowBy for
    // any fill above the base), or the lowest ladder rung STRICTLY above fillPct (flag ON).
    // NaN when no witnessable level remains above the fill; every caller treats NaN as "do
    // not hold".
    [[nodiscard]] double phaseAnchorImminentTargetLevelPct(double fillPct) const noexcept;
    std::deque<double> landingLeadSamplesMs_;
    int stickUpFrames_ = 0;
    // Consecutive frames the PHYSICAL right stick has been truly neutral (magnitude
    // below the deadzone). The Go-To neutral latch clears only after this reaches 3 —
    // a diagonal/sideways RS or a 1-frame dropout is NOT neutral and must not drop the
    // latch (else a still-deflected post-shot RS re-arms a Go-To / leaks a dribble).
    int rightStickNeutralFrames_ = 0;
    int shotsAttempted_ = 0;
    int shotsReleased_ = 0;
    int shotsAborted_ = 0;
};

// [ORION_LEAD_CONFLICT_UI 2026-08-08 task #48] Pure text core of the ShotLeadUsableMaxIndicator
// (QML), exposed through OrionAppController::shotLeadUsableMaxWarning(). Returns the red
// warning line when leadMs exceeds usableMaxMs, empty otherwise. usableMaxMs is
// AutomationEngine::maxSchedulableTipLeadMs() — deliberately DELAY-INDEPENDENT in total-lead
// terms (the visible runway stays == the tip constant regardless of the applied delay; the
// 2026-08-08 verification REFUTED extending it by the delay). What the applied delay changes
// is the wording (it names the delay that is consuming the runway) and the actionable ceiling
// (max delay for this lead = usableMax - (lead - applied)). Free + argument-driven so
// shotLeadUsableMaxClampReflectsAppliedDelay pins the shipped composition without a controller.
[[nodiscard]] ORION_AUTOMATION_API QString shotLeadUsableMaxWarningLine(
    double leadMs, double usableMaxMs, double appliedDelayMs);

} // namespace orion

Q_DECLARE_METATYPE(orion::ShotContext)
