#include "AutomationEngine.h"
#include "FeedforwardAuthorityPolicy.h"
#include "ShotIntentPolicy.h"
#include "ShotReleasePolicy.h"

#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QRandomGenerator>
#include <QtCore/QTextStream>
#include <QtCore/QtMath>

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <limits>
#include <numeric>

namespace orion {

namespace {

constexpr double kPendingMeterOwnershipMaxGapMs = 100.0;

// Canonical physical-release debounce depth. Mirrors ShotIntentEdgeTracker::kReleaseSamples
// and processIdle's allFalse(xButtonHistory) so every seam agrees on what "released" means.
constexpr int kPhysicalReleaseSamples = 3;

// Stale carry-over meter census bounds. These are detector-integrity limits, not machine
// tuning: a genuine shot meter sweeps the full bar in well under a second, so it moves on
// the order of 0.15 pp/ms. Requiring the rejected high-fill run to stay inside
// kStaleMeterMaxSpreadPct across kStaleMeterMinWindowMs is ~0.004 pp/ms — roughly forty
// times slower than any real meter, and it is additionally only reachable when the run
// NEVER dipped to the first-sight bound that every genuine meter starts below.
// kStaleMeterMinPressAgeMs keeps the census strictly behind normal acquisition (a real
// meter renders and proves ownership long before this), so a slow-but-genuine meter is
// never censused; it only shortens a wait that was otherwise going to run to
// buttonNoMeterAbortMs with the player holding the ball.
constexpr int kStaleMeterMinSamples = 8;
constexpr double kStaleMeterMinWindowMs = 600.0;
constexpr double kStaleMeterMaxSpreadPct = 2.5;
// [ORION_ANCHOR_BASE20] Animation time from the 20% crossing to the 30% crossing, measured
// 2026-08-05 from recorded per-frame fill (tools/timing/measure_anchor_slope.py, n=21 shots
// with a witnessed 30% crossing, rSD 0.44 ms/pp). This is THE migration constant for the
// anchor 30 -> 20 move: constant, seed and learn band all shift by exactly this, and the
// learning.json prior is translated by it on restore/persist. The shipped slope constant
// (-5.235 ms/pp) implies 52.35 and is WRONG at the low end -- using it would fire ~6ms
// early on every shot (see the RemapConfig::anchorBase20 note).
constexpr double kAnchorBase20ShiftMs = 58.3;
// Measured secant offsets from base anchor 20 to each ladder rung: how much LESS animation
// remains when the shot is dated at that rung. Derived from the same n=21 measurement
// (slope(L->30) medians: 20: -5.830, 25: -5.776, 35: -5.583, 40: -5.428 ms/pp):
//   t(20->25) = 58.30 - 28.88 = 29.42     t(20->30) = 58.30
//   t(20->35) = 58.30 + 27.92 = 86.22     t(20->40) = 58.30 + 54.28 = 112.58
// A single constant cannot represent these (the meter accelerates with fill); the shipped
// -5.235 mis-dates rung 40 by +7.9ms (LATE) from a base of 20, which would re-create the
// systematic per-rung lateness Candidate B exists to remove.
struct AnchorBase20RungOffset { double levelPct; double offsetMs; };
constexpr AnchorBase20RungOffset kAnchorBase20RungOffsets[] = {
    {20.0, 0.0}, {25.0, 29.42}, {30.0, 58.30}, {35.0, 86.22}, {40.0, 112.58}};
// [ORION_LEAD_CONFLICT] Decision-latency margin between the tip constant and the largest lead
// the phase member can still schedule. The phase estimate is born tip_eta = constant - (fill
// advance past the anchor + frame age): measured on the 2026-08-08 production log, first-valid
// phase decisions arrived 11-27 ms after the anchor crossing (frame_age 8.5-17.4 ms + 2-9 ms of
// fill advance at 0.226 pp/ms), and every phase-source rejected_missed that night satisfied
// lead >= constant - this margin. 30 ms covers the whole observed range; it is deliberately
// advisory (diagnostics only) so a pessimistic margin can never block a schedulable shot.
constexpr double kTipLeadScheduleMarginMs = 30.0;
// [ORION_AIM_FREEZE] Divergence (in physical ms) between the frozen Tip Timing value and the
// live measured median above which the session gets a one-shot warning. Per-type rMAD of the
// landing measurement is 5-11 ms, so at the half-window sample floor the median's standard
// error is ~3-5 ms; 20 ms is ~4 sigma -- a real animation mismatch, not instrument noise.
constexpr double kTipTimingDivergenceWarnMs = 20.0;
// [ORION_STOP_SUBFRAME] Re-centring constant added to the refined sub-frame stop so the learned
// physical constant keeps the SAME MEAN it had under frame-snapped dating. The seed, the shipped
// constant, and every persisted learning.json prior were calibrated against snapped stops, and the
// aim is (learnedPhysical + constant - seed): an estimator that moved the mean would silently
// re-aim every shot the moment the flag flips. MEASURED 2026-08-06 as the median (snapped -
// refined-corner) over the engine-replica replay of the on-disk Aug-04..06 detframes episodes
// (tools/timing/stop_subframe_eval.py, n=107: median -9.61, i.e. the 2pp step threshold usually
// trips on the LAST RISING sample, dating the snap ~10ms EARLY of the true corner — rerun the
// tool before changing this number).
constexpr double kStopSubframeRecenterMs = -9.6;
// [ORION_STOP_SUBFRAME] Estimator guards: plateau margin below which a sample still counts as
// "on the rise" (matches the offline instrument's settle_drop), minimum defensible rise slope
// (a real tip rise runs ~0.18 pp/ms; anything below this is a fit artifact, not a rise), and
// how many trailing rising samples the line is fit through.
constexpr double kStopSubframePlateauMarginPct = 1.0;
constexpr double kStopSubframeMinRiseSlopePctPerMs = 0.05;
constexpr int kStopSubframeRiseFitSamples = 4;
// [ORION_TYPE_TRIM] Hard bound on a per-shot-type tip-phase trim. The measured per-type
// animation differences are single-digit ms (fade-labelled shots date 4-6.5 ms SHORTER than
// Standstill across base30+snap / base20+snap / base20+subframe; 500 PHASE SAMPLE
// observations, 2026-08-05..06 logs), so anything larger than 9 ms is a mis-entered setting,
// not a measurement. AppConfig clamps on read with the same bound; this one keeps a
// programmatic config honest too.
constexpr double kTipPhaseTypeTrimCapMs = 9.0;
// [ORION_DEV_FIRE_OFFSET] Hard bound on a commanded per-shot fire displacement. A sweep needs to
// cover about one frame period; anything larger stops being a timing experiment and starts being
// a thrown shot.
constexpr double kDevFireOffsetMaxAbsMs = 25.0;
constexpr double kStaleMeterMinPressAgeMs = 900.0;

// Sub-millisecond wall-clock epoch read. QDateTime::currentMSecsSinceEpoch() returns whole
// milliseconds, and that 1ms floor was quantizing every release/probe marker: the offline
// lattice tests (release phase within the 16.7ms capture grid) need the marker at the same
// sub-ms resolution the frame stamps already carry end to end.
double epochNowMsF()
{
    return std::chrono::duration<double, std::milli>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

double clampPct(double value)
{
    if (!std::isfinite(value)) {
        return 0.0;
    }
    return std::clamp(value, 0.0, 100.0);
}

// Native re-verification of the packaged prior's route binding.
//
// This used to test only the "-pipe"/"-vigem" SUFFIX, so a decoder profile was accepted verbatim on
// a capture-card session. That was invisible while all four profiles carried the same 241.4 ms mean;
// the moment the means genuinely differ per route (they now do) the same hole becomes a silent
// cross-route mis-application of the actuation lead. Two tightenings:
//
//   1. The name must be EXACTLY one of the four known profiles, not merely prefixed and suffixed.
//      An unknown or malformed profile name can no longer satisfy the controller half by accident.
//   2. The video half (capture-card vs decoder) is verified against the live capture tier when the
//      engine knows it. `videoRoute` empty means "not yet attested" and preserves the previous
//      controller-only behaviour rather than failing a route that was always accepted before.
//      Python already selects the profile by longest scope_contains match, so this is
//      defence-in-depth against a torn or mismatched payload, not the primary selector.
bool factoryLatencyPriorMatchesRoute(const QString& source,
                                     LatencyControllerRoute route,
                                     const QString& videoRoute = QString())
{
    constexpr auto kPrefix = "venice-e2e-route-prior:";
    if (!source.startsWith(QLatin1String(kPrefix))) {
        return false;
    }
    const QString profile = source.mid(static_cast<int>(qstrlen(kPrefix)));
    const bool isPipeProfile = profile == QLatin1String("capture-card-pipe")
        || profile == QLatin1String("decoder-pipe");
    const bool isVigemProfile = profile == QLatin1String("capture-card-vigem")
        || profile == QLatin1String("decoder-vigem");
    switch (route) {
    case LatencyControllerRoute::Pipe:
        if (!isPipeProfile) {
            return false;
        }
        break;
    case LatencyControllerRoute::VigemDs4:
    case LatencyControllerRoute::VigemXusb:
        if (!isVigemProfile) {
            return false;
        }
        break;
    case LatencyControllerRoute::None:
        return false;
    }
    if (videoRoute.isEmpty()) {
        return true;
    }
    const bool profileIsCaptureCard = profile.startsWith(QLatin1String("capture-card-"));
    const bool liveIsCaptureCard = videoRoute.compare(QLatin1String("capture_card"),
                                                      Qt::CaseInsensitive) == 0
        || videoRoute.compare(QLatin1String("capture-card"), Qt::CaseInsensitive) == 0
        || videoRoute.compare(QLatin1String("capturecard"), Qt::CaseInsensitive) == 0;
    const bool liveIsDecoder = videoRoute.compare(QLatin1String("decoder"),
                                                  Qt::CaseInsensitive) == 0
        || videoRoute.compare(QLatin1String("pipe"), Qt::CaseInsensitive) == 0
        || videoRoute.compare(QLatin1String("frame_pipe"), Qt::CaseInsensitive) == 0;
    if (!liveIsCaptureCard && !liveIsDecoder) {
        return true;   // an unrecognised tier is not evidence of a mismatch
    }
    return profileIsCaptureCard == liveIsCaptureCard;
}

} // namespace

// === [ORION_USER_LOG] UserFacingLog (fix 5b) — see the header for the design contract. =====

bool UserFacingLog::envEnabled()
{
    // SHIPPED DEFAULT-ON 2026-08-04. The human-readable log is the one artifact a CUSTOMER can
    // actually read and send to support, and it was default-OFF in code with only the dev
    // launcher turning it on -- so the people who needed it most were the only ones without it.
    // The diagnostic log is untouched and separate. An explicit "0"/"false"/"off"/"no" still
    // disables it.
    if (!qEnvironmentVariableIsSet("ORION_USER_LOG")) {
        return true;
    }
    const QByteArray v = qgetenv("ORION_USER_LOG").trimmed().toLower();
    return !(v == QByteArrayLiteral("0") || v == QByteArrayLiteral("false")
             || v == QByteArrayLiteral("off") || v == QByteArrayLiteral("no"));
}

QString UserFacingLog::fileName()
{
    return QStringLiteral("orion_user.log");
}

QString UserFacingLog::formatLine(const QDateTime& localTime, const QString& plainText)
{
    return QStringLiteral("%1  %2")
        .arg(localTime.toString(QStringLiteral("yyyy-MM-dd HH:mm:ss")), plainText.simplified());
}

void UserFacingLog::append(const QString& plainText)
{
    if (!enabled_) {
        return;   // disabled sink is fully inert — no buffering, no filesystem access, ever
    }
    pending_.append(formatLine(QDateTime::currentDateTime(), plainText));
}

bool UserFacingLog::flushTo(const QString& logsDirPath)
{
    if (pending_.isEmpty()) {
        return false;   // nothing queued (incl. every disabled session): touch NOTHING on disk
    }
    QDir().mkpath(logsDirPath);
    QFile file(logsDirPath + QLatin1Char('/') + fileName());
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
        return false;   // keep the lines queued for the next flush attempt
    }
    QTextStream out(&file);
    for (const QString& line : pending_) {
        out << line << '\n';
    }
    pending_.clear();
    return true;
}

void UserFacingReleaseTracker::stage(int releaseSeq, double fillPct, double targetPct,
                                     const QString& shotType, bool rttVerified, double rttMs)
{
    clear();
    if (releaseSeq < 0) {
        return;
    }
    pendingSeq_ = releaseSeq;
    confirmedText_ = QStringLiteral(
        "Release command accepted by the local controller route — meter at %1% "
        "(aiming for %2%), %3 shot. Console/game receipt is not confirmed.")
        .arg(fillPct, 0, 'f', 0)
        .arg(targetPct, 0, 'f', 0)
        .arg(shotType);
    if (rttVerified) {
        confirmedText_ += QStringLiteral(" Court probe RTT: %1 ms.").arg(rttMs, 0, 'f', 0);
    }
}

QString UserFacingReleaseTracker::confirm(int releaseSeq)
{
    if (pendingSeq_ < 0 || releaseSeq != pendingSeq_) {
        clear();
        return QStringLiteral(
            "Shot release was NOT confirmed — the delivery record did not match the issued shot; "
            "automation stopped for safety.");
    }
    const QString message = confirmedText_;
    clear();
    return message;
}

QString UserFacingReleaseTracker::fail(int releaseSeq, UserReleaseFailureReason reason)
{
    if (pendingSeq_ < 0 || releaseSeq != pendingSeq_) {
        clear();
        return QStringLiteral(
            "Shot release was NOT confirmed — the delivery record did not match the issued shot; "
            "automation stopped for safety.");
    }
    clear();
    return failureNotice(reason);
}

QString UserFacingReleaseTracker::failPending(UserReleaseFailureReason reason)
{
    if (pendingSeq_ < 0) {
        return {};
    }
    clear();
    return failureNotice(reason);
}

QString UserFacingReleaseTracker::failureNotice(UserReleaseFailureReason reason)
{
    switch (reason) {
    case UserReleaseFailureReason::VirtualControllerDisconnected:
        return QStringLiteral(
            "Shot release was NOT delivered — the virtual controller disconnected; "
            "automation stopped for safety.");
    case UserReleaseFailureReason::ControllerRouteRevoked:
        return QStringLiteral(
            "Shot release was NOT delivered — the controller route changed before confirmation; "
            "automation stopped for safety.");
    case UserReleaseFailureReason::RemotePlayDisconnected:
        return QStringLiteral(
            "Shot release was NOT delivered — Remote Play disconnected before confirmation; "
            "automation stopped for safety.");
    case UserReleaseFailureReason::PreciseWriteFailed:
        return QStringLiteral(
            "Shot release was NOT delivered — the precise controller write failed; "
            "automation stopped for safety.");
    case UserReleaseFailureReason::TransportNotConfirmed:
        return QStringLiteral(
            "Shot release was NOT delivered — controller output was not confirmed; "
            "automation stopped for safety.");
    }
    return QStringLiteral(
        "Shot release was NOT delivered — controller output was not confirmed; "
        "automation stopped for safety.");
}

void UserFacingReleaseTracker::clear()
{
    pendingSeq_ = -1;
    confirmedText_.clear();
}

void TemporalSampler::reset()
{
    samples_.clear();
    kf_.initialized = false;
    kf_.position = 0.0;
    kf_.velocity = 0.0;
    kf_.P00 = 100.0;
    kf_.P11 = 100.0;
    kf_.lastTs = -1.0;
}

void TemporalSampler::updateKalman(double fillPct, double timestampMs)
{
    // Item 9: 2-state Kalman filter (position=fill%, velocity=%/ms)
    // Predict + update step. Naturally handles acceleration/deceleration near top.
    if (!kf_.initialized) {
        kf_.position = fillPct;
        kf_.velocity = 0.0;
        kf_.P00 = 100.0;
        kf_.P11 = 100.0;
        kf_.lastTs = timestampMs;
        kf_.initialized = true;
        return;
    }
    const double dt = timestampMs - kf_.lastTs;
    if (dt <= 0.0 || dt > 500.0) {
        // Gap too large — reset filter
        kf_.position = fillPct;
        kf_.velocity = 0.0;
        kf_.P00 = 100.0;
        kf_.P11 = 100.0;
        kf_.lastTs = timestampMs;
        return;
    }
    // Predict: x = F*x, P = F*P*F' + Q
    // F = [[1, dt], [0, 1]]
    kf_.position += kf_.velocity * dt;
    kf_.P00 += dt * (kf_.P01 + kf_.P01) + dt * dt * kf_.P11 + kf_.Q * dt;
    kf_.P01 += dt * kf_.P11;
    kf_.P11 += kf_.Q * dt;
    // Update: y = z - H*x, S = H*P*H' + R, K = P*H'/S, x = x + K*y, P = (I-K*H)*P
    // H = [1, 0] (we observe position only)
    const double y = fillPct - kf_.position;
    const double S = kf_.P00 + kf_.R;
    const double K0 = kf_.P00 / S;
    const double K1 = kf_.P01 / S;
    kf_.position += K0 * y;
    kf_.velocity += K1 * y;
    kf_.P00 -= K0 * kf_.P00;
    kf_.P01 -= K0 * kf_.P01;
    kf_.P11 -= K1 * kf_.P01;
    kf_.lastTs = timestampMs;
}

void TemporalSampler::addSample(double fillPct, double timestampMs)
{
    if (!std::isfinite(fillPct) || !std::isfinite(timestampMs)) {
        return;
    }
    if (!samples_.empty()) {
        const auto& last = samples_.back();
        const double dt = timestampMs - last.timestampMs;
        if (dt <= 0.0) {
            return;
        }
        // Identical-fill dedupe widened 5ms -> 20ms (one ~60Hz telemetry period + margin):
        // the sidecar telemetry thread samples the SAME CV fill 2-3x at different
        // timestamps, and those 16.7ms-apart duplicates flattened the fitted velocity
        // (depressing every predictive path). A genuinely rising meter moves >=0.3%/frame,
        // so real samples never hit the |dFill|<0.05 identical-fill test.
        if (dt < 20.0 && std::abs(fillPct - last.fillPct) < 0.05) {
            return;
        }
        if (dt > 42.0) {
            samples_.clear();
        } else if (fillPct < last.fillPct - 4.0) {
            // Outlier rejection: within a single shot the meter only RISES. A fill
            // that drops meaningfully (and the gap isn't large enough to be a new
            // shot, handled above) is detector noise — dropping it instead of
            // seeding the velocity fit keeps the predictor from whipsawing and
            // releasing early/late off a single bad frame.
            return;
        }
    }

    samples_.push_back({fillPct, timestampMs});
    while (!samples_.empty() && timestampMs - samples_.front().timestampMs > 180.0) {
        samples_.pop_front();
    }
    while (samples_.size() > 16) {
        samples_.pop_front();
    }
    // Item 9: Update the Kalman filter alongside the sample deque
    updateKalman(fillPct, timestampMs);
}

double TemporalSampler::predictCrossingMs(double targetPct) const
{
    return predictCrossing(targetPct).crossingMs;
}

TemporalSampler::CrossingFit TemporalSampler::predictCrossing(double targetPct) const
{
    CrossingFit out;
    const auto n = samples_.size();
    out.n = static_cast<int>(n);
    if (n < 3) {
        return out;
    }
    out.spanMs = samples_.back().timestampMs - samples_.front().timestampMs;

    const auto& last = samples_.back();
    if (last.fillPct >= targetPct) {
        out.crossingMs = last.timestampMs;   // already at/above target; no fit computed (wrss -1)
        return out;
    }

    // Single weighting scheme for BOTH fits (matches the Python predictor). x is
    // centred at the latest sample (x<=0 for history) for conditioning.
    constexpr double lambda = 0.5;

    // ---- Weighted linear fit f = slope*x + intercept, with residual --------
    double sw = 0.0, xm = 0.0, fm = 0.0;
    for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
        const auto& s = samples_[static_cast<size_t>(i)];
        const double w = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
        const double x = s.timestampMs - last.timestampMs;
        sw += w;
        xm += w * x;
        fm += w * s.fillPct;
    }
    if (sw <= 1e-9) {
        return out;
    }
    out.weightSum = sw;
    xm /= sw;
    fm /= sw;
    double num = 0.0, den = 0.0;
    for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
        const auto& s = samples_[static_cast<size_t>(i)];
        const double w = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
        const double x = s.timestampMs - last.timestampMs;
        num += w * (x - xm) * (s.fillPct - fm);
        den += w * (x - xm) * (x - xm);
    }
    double linWrss = 1e300;   // sentinel "no usable linear fit"
    double linCrossing = -1.0;
    if (std::abs(den) >= 1e-9) {
        const double slope = num / den;             // %/ms
        out.slopePctPerMs = slope;
        const double intercept = fm - slope * xm;   // value at x=0 (last sample)
        linWrss = 0.0;
        for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
            const auto& s = samples_[static_cast<size_t>(i)];
            const double w = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
            const double x = s.timestampMs - last.timestampMs;
            const double r = s.fillPct - (intercept + slope * x);
            linWrss += w * r * r;
        }
        if (slope > 0.01) {
            const double deltaPct = targetPct - intercept;
            linCrossing = deltaPct <= 0.0
                ? last.timestampMs
                : last.timestampMs + deltaPct / slope;
        }
    }

    // ---- Weighted quadratic fit, adopted ONLY for observed DECELERATION and
    //      only if it explains the samples meaningfully better than the line --
    //
    // A convex (a > 0) fit over four or five early-rise frames is an unbounded
    // acceleration extrapolator.  It can interpolate those few points almost
    // perfectly and then invent a tip only a handful of milliseconds away even
    // though the latest derivative and the linear fit put it hundreds of
    // milliseconds out.  The live 2026-08-01 regular-button batch exposed the
    // exact failure: fill=24.5, current slope=0.0438 %/ms, yet crossingEta=10ms.
    // That crossing is mathematically impossible for the observed local slope
    // and caused an immediate low-fill release.  No-Dip generally acquired at a
    // higher fill, which only masked the same model risk.
    //
    // Curvature is useful at the other end of the animation: the meter visibly
    // decelerates near its cap.  Restrict quadratic authority to concave-down
    // evidence (a < 0).  Convex early rises keep the continuously refreshed
    // weighted-linear crossing; this is autonomous, shot-type neutral, and
    // becomes more accurate on every genuine frame without a fixed fill/clock.
    if (n >= 4) {
        double m[3][4] = {};
        for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
            const auto& s = samples_[static_cast<size_t>(i)];
            const double x = s.timestampMs - last.timestampMs;
            const double y = s.fillPct;
            const double weight = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
            const double xs[3] = {x * x, x, 1.0};
            for (int row = 0; row < 3; ++row) {
                for (int col = 0; col < 3; ++col) {
                    m[row][col] += weight * xs[row] * xs[col];
                }
                m[row][3] += weight * xs[row] * y;
            }
        }

        bool ok = true;
        for (int col = 0; col < 3 && ok; ++col) {
            int pivot = col;
            for (int row = col + 1; row < 3; ++row) {
                if (std::abs(m[row][col]) > std::abs(m[pivot][col])) {
                    pivot = row;
                }
            }
            if (std::abs(m[pivot][col]) < 1e-9) {
                ok = false;
                break;
            }
            if (pivot != col) {
                for (int k = col; k < 4; ++k) {
                    std::swap(m[col][k], m[pivot][k]);
                }
            }
            const double div = m[col][col];
            for (int k = col; k < 4; ++k) {
                m[col][k] /= div;
            }
            for (int row = 0; row < 3; ++row) {
                if (row == col) {
                    continue;
                }
                const double f = m[row][col];
                for (int k = col; k < 4; ++k) {
                    m[row][k] -= f * m[col][k];
                }
            }
        }

        if (ok) {
            const double a = m[0][3];
            const double b = m[1][3];
            const double cFull = m[2][3];
            const double c = cFull - targetPct;
            double bestFutureMs = -1.0;
            if (std::abs(a) > 1e-9) {
                const double disc = b * b - 4.0 * a * c;
                if (disc >= 0.0) {
                    const double root = std::sqrt(disc);
                    const double r1 = (-b + root) / (2.0 * a);
                    const double r2 = (-b - root) / (2.0 * a);
                    for (double r : {r1, r2}) {
                        if (std::isfinite(r) && r >= 0.0 && r <= 1200.0
                            && (bestFutureMs < 0.0 || r < bestFutureMs)) {
                            bestFutureMs = r;
                        }
                    }
                }
            } else if (std::abs(b) > 1e-9) {
                const double r = -c / b;
                if (std::isfinite(r) && r >= 0.0 && r <= 1200.0) {
                    bestFutureMs = r;
                }
            }
            // Weighted residual of the quadratic — computed regardless of whether a
            // forward crossing exists, so the same adoption gate guards the peak-clamp.
            double quadWrss = 0.0;
            for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
                const auto& s = samples_[static_cast<size_t>(i)];
                const double w = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
                const double x = s.timestampMs - last.timestampMs;
                const double predicted = a * x * x + b * x + cFull;
                const double r = predicted - s.fillPct;
                quadWrss += w * r * r;
            }
            // Adopt the quadratic only when it cuts the weighted residual to <= 80% of
            // the line's. A quadratic always fits at least as well (superset model), so
            // this flat ratio rejects marginal, noise-driven curvature that would blow
            // the extrapolation up.
            constexpr double kConcaveDownEpsilon = -1e-9;
            if (a < kConcaveDownEpsilon && quadWrss <= linWrss * 0.80) {
                if (bestFutureMs >= 0.0) {
                    out.crossingMs = last.timestampMs + bestFutureMs;
                    out.wrss = quadWrss;
                    out.slopePctPerMs = b;
                    out.usedQuad = true;
                    return out;
                }
                // No forward crossing, but the fit is good and CONCAVE-DOWN (a<0): the
                // meter is DECELERATING and will PEAK below the target — it never reaches
                // it (the Arrow2 "recede near the top" behaviour). Falling back to the
                // LINEAR crossing here would extrapolate a phantom crossing the meter
                // can't make and time the release late / past the cap. Instead return the
                // time to the actual PEAK (the highest the meter reaches ~ the top of the
                // green band at the cap), so the engine times the release to where the
                // meter genuinely tops out rather than a fictitious linear crossing.
                if (a < -1e-7) {
                    const double peakMs  = -b / (2.0 * a);          // vertex x (future if > 0)
                    const double peakVal = cFull - (b * b) / (4.0 * a);
                    if (peakMs > 0.0 && peakMs <= 1200.0 && peakVal < targetPct) {
                        out.crossingMs = last.timestampMs + peakMs;
                        out.wrss = quadWrss;
                        out.slopePctPerMs = b;
                        out.usedQuad = true;
                        out.usedPeakFallback = true;
                        out.predictedPeakPct = peakVal;
                        return out;
                    }
                }
            }
        }
    }

    out.crossingMs = linCrossing;
    if (linWrss < 1e300) {
        out.wrss = linWrss;
    }
    return out;
}

// === [ORION_FUSED_FIRE] FusedTipEstimator =============================================
void FusedTipEstimator::reset()
{
    anchored_ = false;
    mu_ = 0.0;
    var_ = 0.0;
    infoUpdates_ = 0;
    pairOverrideFired_ = false;
}

void FusedTipEstimator::anchor(double tipAbsMs, double sigmaMs)
{
    anchored_ = true;
    mu_ = tipAbsMs;
    var_ = std::max(1.0, sigmaMs * sigmaMs);
    infoUpdates_ = 0;
    pairOverrideFired_ = false;
}

double FusedTipEstimator::sigmaMs() const noexcept
{
    return anchored_ ? std::sqrt(var_) : -1.0;
}

void FusedTipEstimator::fuseOne(double m, double sigmaMs)
{
    const double varM = std::max(1.0, sigmaMs * sigmaMs);
    const double newVar = 1.0 / (1.0 / var_ + 1.0 / varM);
    mu_ = newVar * (mu_ / var_ + m / varM);
    var_ = newVar;
}

void FusedTipEstimator::update(bool newInfo, const SourceMeas& reg, const SourceMeas& sampler,
                               bool riseBecamePeak, double processNoiseMs, double peakInflateMs,
                               double chiGate)
{
    if (!anchored_) {
        return;
    }
    // Regime change at the rise->peak transition: the fit regime the posterior was built on
    // just ended, so open the variance back up before (possibly) ingesting this sample.
    if (riseBecamePeak) {
        var_ += peakInflateMs * peakInflateMs;
    }
    const bool regValid = reg.sigmaMs > 0.0 && reg.tipAbsMs > 0.0;
    const bool samplerValid = sampler.sigmaMs > 0.0 && sampler.tipAbsMs > 0.0;

    // chi^2 gate each source against the current posterior. EXCEPTION (vision-pair override):
    // when BOTH fresh sources agree with each other within 2*sqrt(sig_r^2+sig_s^2), they pass
    // even if the posterior disagrees — a wrong clock prior must LOSE to two agreeing live
    // sources — and the anchor's residual authority is permanently diluted for this shot.
    auto gated = [&](const SourceMeas& s) {
        const double d = s.tipAbsMs - mu_;
        return (d * d) > chiGate * (s.sigmaMs * s.sigmaMs + var_);
    };
    bool regPass = regValid && !gated(reg);
    bool samplerPass = samplerValid && !gated(sampler);
    if (regValid && samplerValid && (!regPass || !samplerPass)) {
        const double agreeTol = 2.0 * std::sqrt(reg.sigmaMs * reg.sigmaMs
                                                + sampler.sigmaMs * sampler.sigmaMs);
        if (std::abs(reg.tipAbsMs - sampler.tipAbsMs) <= agreeTol) {
            regPass = samplerPass = true;
            if (!pairOverrideFired_) {
                pairOverrideFired_ = true;
                var_ *= 4.0;   // sigma x2: the prior lost the argument once — trust it less
            }
        }
    }

    // Correlation control: contraction only on samples that carry NEW information (the fill
    // actually stepped — a new game frame). Unchanged-fill samples only grow process noise,
    // so the posterior cannot fake convergence off 60fps duplicates of one game frame.
    if (newInfo && (regPass || samplerPass)) {
        if (regPass) {
            fuseOne(reg.tipAbsMs, reg.sigmaMs);
        }
        if (samplerPass) {
            fuseOne(sampler.tipAbsMs, sampler.sigmaMs);
        }
        ++infoUpdates_;
    }
    var_ += processNoiseMs * processNoiseMs;
}

double TemporalSampler::velocityPctPerMs() const
{
    const auto n = samples_.size();
    if (n < 2) {
        return 0.0;
    }
    // Exponentially-weighted least-squares slope (noise-resistant), matching the
    // Python detector's velocity, instead of a crude first/last difference that
    // squares per-frame detection jitter into the release-path velocity.
    // Item 3: Added time-based decay on top of index-based decay — older samples
    // (by ms age from the latest) weigh less, improving velocity freshness.
    constexpr double lambda = 0.5;
    const double decayMs = 120.0;  // exponential decay constant for age weighting
    const double latestTs = samples_.back().timestampMs;
    double sw = 0.0, tm = 0.0, fm = 0.0;
    for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
        const auto& s = samples_[static_cast<size_t>(i)];
        const double indexWeight = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
        // Time-based decay: weight decreases exponentially with age in ms
        const double ageMs = std::max(0.0, latestTs - s.timestampMs);
        const double timeWeight = std::exp(-ageMs / decayMs);
        const double w = indexWeight * timeWeight;
        sw += w;
        tm += w * s.timestampMs;
        fm += w * s.fillPct;
    }
    if (sw <= 1e-9) {
        return 0.0;
    }
    tm /= sw;
    fm /= sw;
    double num = 0.0, den = 0.0;
    for (qsizetype i = 0; i < static_cast<qsizetype>(n); ++i) {
        const auto& s = samples_[static_cast<size_t>(i)];
        const double indexWeight = std::exp(-lambda * static_cast<double>(n - 1U - static_cast<size_t>(i)));
        const double ageMs = std::max(0.0, latestTs - s.timestampMs);
        const double timeWeight = std::exp(-ageMs / decayMs);
        const double w = indexWeight * timeWeight;
        const double dtv = s.timestampMs - tm;
        num += w * dtv * (s.fillPct - fm);
        den += w * dtv * dtv;
    }
    if (std::abs(den) < 1e-9) {
        return 0.0;
    }
    return num / den;
}

void GreenWindowTracker::reset()
{
    starts_.clear();
    ends_.clear();
    stableFrames_ = 0;
    confirmed_ = false;
    bestStartPct_ = -1.0;
    bestEndPct_ = -1.0;
    bestCenterPct_ = -1.0;
    widthPct_ = 0.0;
    confidence_ = 0.0;
}

void GreenWindowTracker::update(double startPct, double endPct, double confidence)
{
    if (startPct < 0.0 || endPct < 0.0 || endPct <= startPct) {
        stableFrames_ = std::max(0, stableFrames_ - 1);
        return;
    }

    starts_.push_back(startPct);
    ends_.push_back(endPct);
    while (starts_.size() > 12) {
        starts_.pop_front();
        ends_.pop_front();
    }

    if (starts_.size() < 3 && !fastPath_) {
        stableFrames_ = 0;
        return;
    }

    // Fast path: confirm on the first valid reading (for very fast meter styles).
    if (fastPath_ && starts_.size() >= 1) {
        bestStartPct_ = startPct;
        bestEndPct_ = endPct;
        bestCenterPct_ = (startPct + endPct) * 0.5;
        widthPct_ = endPct - startPct;
        confidence_ = confidence;
        stableFrames_ = 1;
        confirmed_ = true;
        return;
    }

    auto recentStart = std::vector<double>{starts_.end() - 3, starts_.end()};
    auto recentEnd = std::vector<double>{ends_.end() - 3, ends_.end()};
    const auto [sMin, sMax] = std::minmax_element(recentStart.begin(), recentStart.end());
    const auto [eMin, eMax] = std::minmax_element(recentEnd.begin(), recentEnd.end());
    // Tolerance 3.0% and confirmation reached at 1 stable frame (3 fresh green
    // readings, after the >=3-sample minimum above). History: a tight 2% gate on a
    // real stream sometimes never confirmed → 96% fallback → 100%, so it went 3 -> 2
    // stable frames once the full-res sidecar feed stopped fighting the tracker.
    // Live (2026-06-05) the Arrow2 Purple green window is FAST — only ~30-40 ms, a
    // couple of frames, wide — so requiring 2 stable frames (= 4 fresh green readings)
    // rarely confirmed before the meter forced a meter_full release (the bimodal
    // green_confirmed-vs-meter_full split = the live "late / didn't time it" shots).
    // One stable window confirms now. A roi_not_found dropout never calls update(), so
    // it can't decrement the streak — the 3 readings may span a single missed frame and
    // still confirm. confirmed_ is sticky-true (the else/invalid branches only decrement
    // stableFrames_), so a later jump can't un-confirm a locked green.
    if ((*sMax - *sMin) < 3.0 && (*eMax - *eMin) < 3.0) {
        std::sort(recentStart.begin(), recentStart.end());
        std::sort(recentEnd.begin(), recentEnd.end());
        stableFrames_++;
        bestStartPct_ = recentStart[1];
        bestEndPct_ = recentEnd[1];
        bestCenterPct_ = (bestStartPct_ + bestEndPct_) * 0.5;
        widthPct_ = bestEndPct_ - bestStartPct_;
        confidence_ = confidence;
        confirmed_ = stableFrames_ >= 1;
    } else {
        stableFrames_ = std::max(0, stableFrames_ - 1);
    }
}

double GreenWindowTracker::targetPct(const QString& mode, double fallbackPct, double learningBiasPct) const
{
    double base = fallbackPct;
    if (confirmed_) {
        if (mode == QLatin1String("start")) {
            base = bestStartPct_;
        } else if (mode == QLatin1String("center")) {
            base = bestCenterPct_;
        } else if (mode == QLatin1String("tip")) {
            base = bestStartPct_ + widthPct_ * 0.9;
        } else {
            base = bestEndPct_;
        }
    }
    return clampPct(base + learningBiasPct);
}

double GreenWindowTracker::adaptiveTargetPct(double tipMarginPct, double learningBiasPct) const
{
    if (!confirmed_) {
        return -1.0;  // no green -> caller times the meter fully to the top
    }
    // DEAD-TOP for EVERY width: aim the window's TOP EDGE (bestEndPct_). The top is the
    // contest-INVARIANT make-point (contest shrinks the green from BELOW, never the top), so it
    // stays green at any contest and is the one part still detectable when the window is a sliver.
    // Live-confirmed: fades green miniscule/contested windows by timing the tip. tipMarginPct pulls
    // the aim a hair below the edge for late-safety (default 0 = dead top); the per-type offset loop
    // keeps the meter actually reaching the top.
    const double base = bestEndPct_ - tipMarginPct;
    return clampPct(base + learningBiasPct);
}

// ===========================================================================================
// [ORION_TEMPLATE_ARRIVAL] TemplateArrivalEstimator — H4 template-matched green-arrival.
// ===========================================================================================
void TemplateArrivalEstimator::beginShot(const QString& bucketKey, double pressT0Ms)
{
    cross_.fill(-1.0);
    key_ = bucketKey;
    press0_ = pressT0Ms;
    lastFill_ = -1.0;
    lastT_ = -1.0;
    crossedCount_ = 0;
    matchedIdx_ = -1;
}

void TemplateArrivalEstimator::addSample(double fillPct, double tMs)
{
    if (key_.isEmpty() || !std::isfinite(fillPct) || !std::isfinite(tMs)) {
        return;
    }
    // Rising samples only: the crossing vector is TIME-AT-FILL on the way UP. A dip/deflate
    // sample must not overwrite a crossing already recorded.
    if (lastFill_ >= 0.0 && fillPct <= lastFill_) {
        lastFill_ = std::max(lastFill_, fillPct);
        lastT_ = tMs;
        return;
    }
    for (int i = 0; i < kGridN; ++i) {
        const double f = gridFill(i);
        if (cross_[static_cast<std::size_t>(i)] >= 0.0 || f > fillPct) {
            continue;
        }
        double t = tMs;
        if (lastFill_ >= 0.0 && fillPct > lastFill_ && f > lastFill_) {
            // linear interp between the last sample below and this one
            const double a = (f - lastFill_) / (fillPct - lastFill_);
            t = lastT_ + a * (tMs - lastT_);
        }
        cross_[static_cast<std::size_t>(i)] = t;
        if (i < kGridN - 1) {
            ++crossedCount_;
        }
    }
    lastFill_ = fillPct;
    lastT_ = tMs;
    if (crossedCount_ >= matchMinFills_) {
        rematch();
    }
}

double TemplateArrivalEstimator::shapeDistance(const Row& tmpl) const
{
    // SHAPE distance: inter-crossing deltas relative to the first common crossed rung, so the
    // match is t0-free (works without a press anchor). With press-t0, the press->first-crossing
    // interval joins as an extra feature — the earlier, stronger discriminator (H5).
    int i0 = -1;
    for (int i = 0; i < kGridN - 1; ++i) {
        if (cross_[static_cast<std::size_t>(i)] >= 0.0
            && tmpl.cross[static_cast<std::size_t>(i)] >= 0.0) {
            i0 = i;
            break;
        }
    }
    if (i0 < 0) {
        return 1e18;
    }
    double ss = 0.0;
    int n = 0;
    for (int i = i0 + 1; i < kGridN - 1; ++i) {
        const double a = cross_[static_cast<std::size_t>(i)];
        const double b = tmpl.cross[static_cast<std::size_t>(i)];
        if (a < 0.0 || b < 0.0) {
            continue;
        }
        const double d = (a - cross_[static_cast<std::size_t>(i0)])
            - (b - tmpl.cross[static_cast<std::size_t>(i0)]);
        ss += d * d;
        ++n;
    }
    if (pressT0_ && press0_ > 0.0 && tmpl.pressOffsetMs >= 0.0) {
        const double d = (cross_[static_cast<std::size_t>(i0)] - press0_) - tmpl.pressOffsetMs;
        ss += d * d;
        ++n;
    }
    if (n < 1) {
        return 1e18;
    }
    return std::sqrt(ss / n);
}

void TemplateArrivalEstimator::rematch()
{
    matchedIdx_ = -1;
    const auto rows = templates_.value(key_);
    double best = 1e17;
    for (int r = 0; r < rows.size(); ++r) {
        const double d = shapeDistance(rows[r]);
        if (d < best) {
            best = d;
            matchedIdx_ = r;
        }
    }
}

double TemplateArrivalEstimator::predictArrivalMs(double targetPct) const
{
    if (matchedIdx_ < 0) {
        return -1.0;
    }
    const auto rows = templates_.value(key_);
    if (matchedIdx_ >= rows.size()) {
        return -1.0;
    }
    const Row& tmpl = rows[matchedIdx_];
    // Anchor at the LATEST observed crossing that the template also has (t@80-90 when
    // available — the shortest extrapolation): predicted arrival = t_obs(anchor) +
    // (template t@96 - template t(anchor)).
    const double t96 = tmpl.cross[kGridN - 1];
    if (t96 < 0.0) {
        return -1.0;
    }
    for (int i = kGridN - 2; i >= 0; --i) {
        const double a = cross_[static_cast<std::size_t>(i)];
        const double b = tmpl.cross[static_cast<std::size_t>(i)];
        if (a < 0.0 || b < 0.0) {
            continue;
        }
        double arrival = a + (t96 - b);
        if (targetPct > 96.0) {
            // extrapolate past the green-arrival slot with the template's terminal slope
            const double t90 = tmpl.cross[kGridN - 2];
            if (t90 >= 0.0 && t96 > t90) {
                arrival += (t96 - t90) / 6.0 * (targetPct - 96.0);
            }
        } else if (targetPct < 96.0) {
            const double t90 = tmpl.cross[kGridN - 2];
            if (t90 >= 0.0 && t96 > t90) {
                arrival -= (t96 - t90) / 6.0 * (96.0 - targetPct);
            }
        }
        return arrival;
    }
    return -1.0;
}

void TemplateArrivalEstimator::endShot()
{
    // Fold the completed shot into the per-bucket clusters. Needs a real green-arrival label:
    // t@96 observed, or (fired just before 96) a short linear extrapolation from t@85->t@90.
    if (key_.isEmpty() || crossedCount_ < 6) {
        return;
    }
    Row row;
    row.cross = cross_;
    if (row.cross[kGridN - 1] < 0.0) {
        const double t90 = row.cross[kGridN - 2];
        const double t85 = row.cross[kGridN - 3];
        if (t90 >= 0.0 && t85 >= 0.0 && t90 > t85) {
            // ~1 rung of extrapolation (90->96): acceptable label noise for a v1 online learner
            row.cross[kGridN - 1] = t90 + (t90 - t85) / 5.0 * 6.0;
        } else {
            return;   // no usable green-arrival label -> contribute nothing
        }
    }
    if (pressT0_ && press0_ > 0.0) {
        for (int i = 0; i < kGridN - 1; ++i) {
            if (row.cross[static_cast<std::size_t>(i)] >= 0.0) {
                row.pressOffsetMs = row.cross[static_cast<std::size_t>(i)] - press0_;
                break;
            }
        }
    }
    row.n = 1;
    auto& rows = templates_[key_];
    // nearest existing cluster by shape
    int bestIdx = -1;
    double bestD = 1e18;
    for (int r = 0; r < rows.size(); ++r) {
        const double d = shapeDistance(rows[r]);
        if (d < bestD) {
            bestD = d;
            bestIdx = r;
        }
    }
    if (bestIdx < 0 || (bestD > kNewClusterGateMs && rows.size() < k_)) {
        rows.push_back(row);        // spawn a new cluster (k-capped)
        return;
    }
    // EMA-fold into the nearest cluster mean, elementwise on ALIGNED deltas: convert both to
    // first-crossing-relative shape, blend, and rebuild an absolute-like row anchored at 0.
    Row& c = rows[bestIdx];
    const double alpha = 0.3;
    int ci0 = -1;
    int ri0 = -1;
    for (int i = 0; i < kGridN - 1; ++i) {
        if (ci0 < 0 && c.cross[static_cast<std::size_t>(i)] >= 0.0) {
            ci0 = i;
        }
        if (ri0 < 0 && row.cross[static_cast<std::size_t>(i)] >= 0.0) {
            ri0 = i;
        }
    }
    if (ci0 < 0 || ri0 < 0) {
        return;
    }
    const double c0 = c.cross[static_cast<std::size_t>(ci0)];
    const double r0 = row.cross[static_cast<std::size_t>(ri0)];
    for (int i = 0; i < kGridN; ++i) {
        const double cv = c.cross[static_cast<std::size_t>(i)];
        const double rv = row.cross[static_cast<std::size_t>(i)];
        if (rv < 0.0) {
            continue;
        }
        const double rRel = rv - r0;
        if (cv < 0.0) {
            c.cross[static_cast<std::size_t>(i)] = c0 + rRel;    // fill a missing rung
        } else {
            const double cRel = cv - c0;
            c.cross[static_cast<std::size_t>(i)] = c0 + (1.0 - alpha) * cRel + alpha * rRel;
        }
    }
    if (row.pressOffsetMs >= 0.0) {
        c.pressOffsetMs = c.pressOffsetMs < 0.0
            ? row.pressOffsetMs
            : (1.0 - alpha) * c.pressOffsetMs + alpha * row.pressOffsetMs;
    }
    c.n += 1;
}

// ===========================================================================================
// [ORION_BANDIT_LEAD] BanditLeadTuner — warmup-only lead micro-calibration.
// ===========================================================================================
void BanditLeadTuner::reset()
{
    armPulls_.fill(0);
    armGreens_.fill(0);
    armEarly_.fill(0);
    armOver_.fill(0);
    currentArm_ = 2;
    pendingArm_ = -1;
    grades_ = 0;
    locked_ = false;
    lockedOffsetMs_ = 0.0;
}

double BanditLeadTuner::currentOffsetMs(bool exploreAllowed) const noexcept
{
    if (locked_) {
        return lockedOffsetMs_;
    }
    return exploreAllowed ? armOffsetMs(currentArm_) : 0.0;
}

void BanditLeadTuner::noteRelease(bool exploreAllowed)
{
    if (locked_ || !exploreAllowed) {
        return;
    }
    pendingArm_ = currentArm_;
}

void BanditLeadTuner::noteGrade(int label)
{
    if (locked_ || pendingArm_ < 0 || pendingArm_ >= kArms || label < 0 || label > 2) {
        return;
    }
    const int arm = pendingArm_;
    pendingArm_ = -1;
    armPulls_[static_cast<std::size_t>(arm)] += 1;
    if (label == 1) {
        armGreens_[static_cast<std::size_t>(arm)] += 1;
    } else if (label == 0) {
        armEarly_[static_cast<std::size_t>(arm)] += 1;
    } else {
        armOver_[static_cast<std::size_t>(arm)] += 1;
    }
    ++grades_;
    maybeLock();
    if (!locked_) {
        advanceArm();
    }
}

void BanditLeadTuner::advanceArm()
{
    // Deterministic least-pulled-first exploration (micro-grid: exploration is cheap and a
    // random policy would make the unit tests + live A/Bs non-reproducible). Ties -> smaller
    // |δ| (prefer staying near the dialed lead while information is equal).
    int best = 0;
    for (int i = 1; i < kArms; ++i) {
        const bool fewer = armPulls_[static_cast<std::size_t>(i)]
            < armPulls_[static_cast<std::size_t>(best)];
        const bool tie = armPulls_[static_cast<std::size_t>(i)]
            == armPulls_[static_cast<std::size_t>(best)];
        if (fewer || (tie && std::abs(armOffsetMs(i)) < std::abs(armOffsetMs(best)))) {
            best = i;
        }
    }
    currentArm_ = best;
}

void BanditLeadTuner::maybeLock()
{
    bool allSampled = true;
    for (int i = 0; i < kArms; ++i) {
        if (armPulls_[static_cast<std::size_t>(i)] < minPulls_) {
            allSampled = false;
            break;
        }
    }
    if (!allSampled && grades_ < maxShots_) {
        return;
    }
    // Converge on the best green RATE; ties -> smaller |δ| (don't move the lead without
    // evidence). EARLY/OVER tallies stay exposed as the measured early/late asymmetry.
    int best = 2;   // δ=0 default
    double bestRate = -1.0;
    for (int i = 0; i < kArms; ++i) {
        const int pulls = armPulls_[static_cast<std::size_t>(i)];
        if (pulls < 1) {
            continue;
        }
        const double rate = static_cast<double>(armGreens_[static_cast<std::size_t>(i)]) / pulls;
        const bool better = rate > bestRate + 1e-12;
        const bool tie = std::abs(rate - bestRate) <= 1e-12;
        if (better || (tie && std::abs(armOffsetMs(i)) < std::abs(armOffsetMs(best)))) {
            bestRate = rate;
            best = i;
        }
    }
    locked_ = true;
    lockedOffsetMs_ = armOffsetMs(best);
}

AutomationEngine::AutomationEngine(QObject* parent)
    : QObject(parent)
{
    clock_.start();
}

void AutomationEngine::applyConfig(const AppConfigData& settings, const LearningData& learning)
{
    // [ORION_PHASE_COLD_START] Restore the animation constant measured in a previous session. The
    // constant belongs to the equipped jumpshot, not to the process, so re-deriving it from the
    // seed on every launch made every session open with the same rough patch. Only ever ADOPTED
    // when the file actually carries one (>0); otherwise the seed carries the session as before.
    // [ORION_ANCHOR_BASE20] learning.json stores the phase prior canonically at base anchor 30;
    // express it at the ACTIVE base here. Uses settings (not config_, which is not yet updated)
    // deliberately. A regime flip mid-session also invalidates the accumulated sample window --
    // its observations were normalised at the OLD base and are not commensurable with the new
    // one -- so it is cleared rather than translated (samples are raw measurements; a translated
    // sample is a guess wearing a measurement's clothes).
    const double phaseRestoreShiftMs = settings.tipPhaseAnchorBase20 ? kAnchorBase20ShiftMs : 0.0;
    if (config_.anchorBase20 != settings.tipPhaseAnchorBase20) {
        phaseConstantSamplesMs_.clear();
        learnedPhasePhysicalMs_ = -1.0;
    }
    if (learning.learnedPhasePhysicalMs > 0.0) {
        learnedPhasePhysicalMs_ = learning.learnedPhasePhysicalMs + phaseRestoreShiftMs;
    }
    // Remember what this session STARTED from, so the partial-window shrink moves away from the
    // restored prior rather than from the seed. Without this the persistence above is defeated on
    // the third landing -- see recordPhaseConstantSample().
    // tipPhaseSeedPhysicalMs lives on RemapConfig, not AppConfigData, and config_ is not yet
    // updated at this point in applyConfig -- so -1 means "no prior", and the consumer falls back
    // to the seed itself. Keeps the fallback in one place rather than two.
    phaseShrinkBaseMs_ = learning.learnedPhasePhysicalMs > 0.0
        ? learning.learnedPhasePhysicalMs + phaseRestoreShiftMs
        : -1.0;
    // [ORION_AIM_FREEZE] Latch the session's starting aim ONCE, from the restored prior when there
    // is one, else the seed. Learning never writes this, so the frozen value is exactly what the
    // session opened with -- the value a previous session converged on and a counted batch graded.
    frozenAimPhysicalMs_ = learning.learnedPhasePhysicalMs > 0.0
        ? learning.learnedPhasePhysicalMs + phaseRestoreShiftMs
        : config_.tipPhaseSeedPhysicalMs;
    const bool priorRouteEnabled = config_.enabled;
    const bool priorLiveMeterAuthority = autonomousLiveMeterTimingEnabled();
    const bool pendingOwnershipAtConfigBoundary =
        pendingStickCalibrationActive_
        || (squareHoldStartMs_ >= 0.0 && pendingSquarePhysicalEpoch_ != 0)
        || pendingMeterOwnership_.active
        || tempoMovement_.phase != TempoMovementPhase::None;
#if defined(ORION_ENABLE_DEV_NO_METER) && ORION_ENABLE_DEV_NO_METER
    const bool effectiveNoMeterEnabled = settings.noMeterEnabled;
#else
    // Production contract: the meter is the bot's eyes. Pose-only timing is retained
    // solely as compile-time-gated lab code; a stale/hand-edited settings file cannot
    // select it in a shipped build.
    const bool effectiveNoMeterEnabled = false;
#endif
    const bool authorityModeChanged = config_.noMeterEnabled != effectiveNoMeterEnabled;
    if (authorityModeChanged) {
        // Never reinterpret an in-flight hold or copied precise-fire deadline under a
        // different authority model. Abort first (which synchronously fences the worker)
        // and require a fresh physical release/neutral edge before another arm.
        if (shot_.state == HoldState::Releasing || shot_.state == HoldState::Cooldown) {
            latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                  shot_.mode, shot_.shotType);
        } else if (shot_.state != HoldState::Idle) {
            abort(QStringLiteral("authority_mode_changed_abort"));
            if (shot_.state == HoldState::Releasing) {
                latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                      shot_.mode, shot_.shotType);
            }
        } else {
            invalidateUnconfirmedSchedule(false, "authority_mode_change");
            pose_.poseScheduled = false;
        }
    }
    // Meter detection is the only shipped release-authority mode, so the
    // engine arms unconditionally here. A stale settings.json meter_enabled=false must not
    // silently disable the whole pipeline — there is no UI to flip it back. The
    // remotePlayInputSource==off override below remains the explicit hard-disable.
    config_.enabled = true;
    const auto input = settings.remotePlayInputSource.toLower();
    if (input == QLatin1String("off") || input == QLatin1String("disabled")) {
        config_.enabled = false;
    }
    if (input == QLatin1String("stick") || input == QLatin1String("stick_only")) {
        config_.inputMode = QStringLiteral("stick_only");
    } else if (input == QLatin1String("both")) {
        config_.inputMode = QStringLiteral("both");
    } else {
        config_.inputMode = QStringLiteral("square_only");
    }
    config_.tempoRemapEnabled = settings.tempoRemapEnabled;
    config_.tempoRemapType = settings.tempoRemapType;
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] #88
    config_.squarePassthroughEnabled = settings.squarePassthroughEnabled;
    config_.squarePassthroughButton = settings.squarePassthroughButton;
    // Built-in default-ON (user directive): no-dip is the default shot, so the engine forces the
    // mode on regardless of the (signed) settings value. noDipLeadMs stays live-tuned (default 0 =
    // behaviour-neutral until dialled). Was: config_.noDipEnabled = settings.noDipEnabled;
    config_.noDipEnabled = true;
    config_.noDipLeadMs = settings.noDipLeadMs;
    config_.tempoWaitMs = settings.tempoWaitMs;
    config_.tempoFlickHoldMs = settings.tempoFlickHoldMs;
    config_.tempoMinStickHoldMs = settings.tempoMinStickHoldMs;
    config_.tempoFallbackTimeoutMs = settings.tempoFallbackTimeoutMs;
    config_.shotTypeModeOverride = settings.shotTypeModeOverride;
    config_.shotTypeFlickHoldMs = settings.shotTypeFlickHoldMs;
    // Tempo is an output remap, not a timing mode. A stale signed setting must
    // not revive the old Tempo-only open-loop release authority.
    config_.tempoOpenLoopPrimary = false;
    config_.ffReachabilitySlackPct = settings.ffReachabilitySlackPct;
    config_.lsCancelThresholdPct = settings.lsCancelThresholdPct;
    config_.lsCancelFrames = settings.lsCancelFrames;
    config_.calibrationMode = settings.calibrationMode;
    config_.minHoldMs = settings.minimumHoldMs;
    config_.maxHoldMs = settings.maximumHoldMs;
    config_.fixedHoldMs = settings.fixedHoldMs;
    config_.squareHoldArmMs = std::clamp(settings.minimumHoldMs + 90.0, 190.0, 320.0);
    // Kept for settings/telemetry compatibility; Tempo uses the exact Button gate.
    config_.tempoSquareHoldArmMs = config_.squareHoldArmMs;
    config_.gotoHoldArmMs = std::clamp(settings.minimumHoldMs + 140.0, 240.0, 420.0);
    config_.standstillCommitMinMs = std::clamp(settings.minimumHoldMs + 60.0, 150.0, 300.0);
    config_.fadeCommitMinMs = std::clamp(settings.fixedHoldMs * 0.68, 420.0, 900.0);
    config_.movingCommitMinMs = std::clamp(settings.fixedHoldMs * 0.72, 440.0, 950.0);
    // Go-To shots need MORE time to activate: the player animation must surface
    // the meter before any release is allowed. Earliest-release floor raised from
    // *1.10/720..1350 to *1.25/820..1500, AND given its own max-hold ceiling well
    // above the global one so the longer signature wind-up isn't cut short by the
    // global safety. Together: a wide window (floor..ceiling) for the meter to
    // appear and the predictive release to time it. NOTE: this commit floor gates
    // ALL release paths, so it stays LOW (~820ms) — a Go-To with a VISIBLE meter
    // must fire on its vision crossing at its real fill, un-floored. The separate
    // ~1600ms hold-clock floor is retained for learned timing, but that clock still
    // requires a validated, current meter anchor before it may release.
    config_.gotoCommitMinMs = std::clamp(settings.fixedHoldMs * 1.25, 820.0, 1500.0);
    // [ORION_GOTO_CLOCK] Go-To hold-clock floor. The clock can only act while
    // detector authority is current, so this is a timing floor rather than a fallback.
    config_.gotoBlindFireFloorMs = std::clamp(settings.fixedHoldMs * 1.25, 1600.0, 1900.0);
    // Post-meter timing windows (measured from when the meter APPEARS, not hold
    // start): once the meter is up the predictive paths get this long before a
    // labelled last-resort fires.
    config_.gotoMaxHoldMs = std::max(config_.maxHoldMs, config_.gotoCommitMinMs + 650.0);
    // Hold-start abort caps: how long to keep holding while waiting for the meter
    // to surface before ABORTING (no blind shot). Go-To / half-court / contested
    // animations can take several seconds, so they wait far longer than buttons.
    config_.gotoNoMeterAbortMs = std::max(6000.0, config_.gotoMaxHoldMs + 1500.0);
    // A1 (2026-07-25): floor raised 1500 -> 2800. The old floor sat INSIDE the measured
    // meter-acquire distribution (live firstMeterMs = 1424/1535/1445), so a held button whose
    // meter surfaced a few tens of ms late was blind-fired by the no-meter net instead of being
    // timed — live ButtonShot seq 1-6 all logged `fresh=0 nodet=ALL firstMeterMs=-1 blindFire=1`.
    // This is only a wait budget; expiry aborts automation and restores pass-through.
    config_.buttonNoMeterAbortMs = std::max(2800.0, config_.maxHoldMs + 200.0);
    config_.noMeterEnabled = effectiveNoMeterEnabled;
    config_.noMeterReleasePoint = settings.noMeterReleasePoint;
    config_.noMeterBaseOffsetMs = settings.noMeterBaseOffsetMs;
    config_.noMeterDecodeCompMs = settings.noMeterDecodeCompMs;
    config_.noMeterConfidenceGate = settings.noMeterConfidenceGate;
    config_.noMeterPushReleaseWindowMs = settings.noMeterPushReleaseWindowMs;
    config_.noMeterShotTypeOffsets = settings.noMeterShotTypeOffsets;
    config_.earlyLateOffsetMs = settings.earlyLateOffsetMs;
    config_.controllerChainMs = settings.latencyCompensationMs;  // user's total latency comp
    config_.greenWindowTarget = settings.greenWindowTargetMode.isEmpty()
                                    ? QStringLiteral("tip")
                                    : settings.greenWindowTargetMode;
    config_.fallbackTargetPct = std::clamp(settings.releaseThresholdPct > 0.0
                                               ? settings.releaseThresholdPct
                                               : 96.0,
                                           50.0,
                                           100.0);
    config_.gotoEnabled = true;
    config_.greenWindowPriority = true;
    config_.confidenceGate = std::clamp(settings.detectionConfidencePercent / 100.0, 0.05, 1.0);
    config_.stableFramesRequired = std::clamp(settings.stableFrames, 1, 10);
    config_.learningBiasPct = learning.biasPct;
    config_.shotTypeOffsets = settings.shotTypeOffsets;
    config_.shotTypeLearnedOffsetMs = learning.shotTypeLearnedOffsetMs;
    config_.shotTypeFeedforwardMs = learning.shotTypeFeedforwardMs;
    // Compiled-in safe per-type timing seeds for fresh profiles. Learned values
    // already present win; these only fill gaps. A seed is timing, never release
    // authority: a current genuine meter is still required before it can fire.
    {
        static const QMap<QString, double> kFeedforwardDefaults{
            {QStringLiteral("Standstill"), 460.0},
            {QStringLiteral("Left Fade"), 800.0}, {QStringLiteral("Right Fade"), 800.0},
            {QStringLiteral("Back Fade"), 820.0}, {QStringLiteral("Front Fade"), 760.0},
            {QStringLiteral("No Dip"), 350.0}, {QStringLiteral("Moving"), 520.0},
        };
        for (auto it = kFeedforwardDefaults.constBegin(); it != kFeedforwardDefaults.constEnd(); ++it) {
            if (!config_.shotTypeFeedforwardMs.contains(it.key())) {
                config_.shotTypeFeedforwardMs.insert(it.key(), it.value());
            }
        }
    }
    config_.shotTypeMeterToReleaseMs = learning.shotTypeMeterToReleaseMs;
    config_.shotTypeCalPhase = learning.shotTypeCalPhase;
    config_.shotTypeRttBaselineMs = learning.shotTypeRttBaselineMs;
    config_.shotTypeVelocityPriorPctMs = learning.shotTypeVelocityPriorPctMs;
    // Autonomous tip-vision timing (the launch design): tip target + self-measured global lead is
    // the PRODUCTION default. The launcher forces it on via ORION_AUTONOMOUS_VISION (envDefaultOn,
    // exactly like ORION_FREEZE_CAL below), so a stale settings.json can't silently drop back to the
    // per-type path. Dev/test builds WITHOUT the env respect settings.autonomousVision (default OFF),
    // so the per-type fallback path stays default-constructible + unit-testable. The learned globals
    // come from learning.json (self-seeded/-tuned; survive restarts via globalTimingLearned).
    config_.autonomousVision = settings.autonomousVision
        || qEnvironmentVariableIsSet("ORION_AUTONOMOUS_VISION");
    config_.autonomousVisionShadow = settings.autonomousVisionShadow;
    // Tip-timing improvement flags (2026-07 W-series). Each is settings-driven OR forced on by its env
    // var (mirrors autonomousVision), and defaults OFF so a default-constructed engine keeps current
    // behavior — the unit tests flip config_ directly, live A/B flips settings.json or the env var.
    // Strict autonomous timing has no fallback clock: its only release lead is
    // the live route measurement produced by the controlled calibration.  Keep
    // that dependency inseparable from autonomousVision so a fresh/stale
    // settings.json cannot enable the bot while silently disabling the one
    // authority it needs to ever leave pass-through mode.
    config_.measuredLeadEnabled = config_.autonomousVision
        || settings.measuredLeadEnabled
        || qEnvironmentVariableIsSet("ORION_MEASURED_LEAD");
    config_.tickLockEnabled = settings.tickLockEnabled
        || qEnvironmentVariableIsSet("ORION_TICK_LOCK");
    config_.regFusionEnabled = config_.autonomousVision || settings.regFusionEnabled
        || qEnvironmentVariableIsSet("ORION_REG_FUSION");
    config_.leadLearnerVisionGate = settings.leadLearnerVisionGate
        || qEnvironmentVariableIsSet("ORION_LEAD_VISION_GATE");
    config_.blindFireSuppressEnabled = settings.blindFireSuppressEnabled
        || qEnvironmentVariableIsSet("ORION_BLIND_SUPPRESS");
    config_.blindFireSchedTolMs = settings.blindFireSchedTolMs;
    config_.priorPosteriorBlendEnabled = settings.priorPosteriorBlendEnabled
        || qEnvironmentVariableIsSet("ORION_PRIOR_POSTERIOR");
    // [ORION_PRESS_ANCHOR] Phase-B predictor arm (default OFF — data collection first) + the
    // learner's floor. The per-type calibration is restored ONCE per process: the learner's
    // own write-back re-enters applyConfig through the settings save path, and re-snapshotting
    // the freshly blended values while the in-session window still holds the same observations
    // would double-count them (see pressAnchoredPriorsLoaded_).
    config_.pressAnchoredEnabled = settings.pressAnchoredPredictorEnabled;
    config_.pressAnchoredMinSamples = std::max(1, settings.pressAnchoredPredictorMinSamples);
    if (!pressAnchoredPriorsLoaded_) {
        pressAnchoredPriorsLoaded_ = true;
        config_.pressAnchoredTipMs = settings.pressAnchoredTipMs;
        config_.pressAnchoredTipSigmaMs = settings.pressAnchoredTipSigmaMs;
        config_.pressAnchoredTipN = settings.pressAnchoredTipN;
        pressTipPriorMs_ = settings.pressAnchoredTipMs;
        pressTipPriorSigmaMs_ = settings.pressAnchoredTipSigmaMs;
        pressTipPriorW_ = settings.pressAnchoredTipN;
    }
    // [ORION_GOTO_METER_WAIT] (fix 2b) Go-To waits for a REAL meter before timing.
    // SHIPPED DEFAULT-ON 2026-08-04. It was default-OFF in code and set only by the dev
    // launcher, so every measured Go-To result on this rig -- including the 9/9 and 10/11 phase
    // coverage behind the 96.9% batch -- was produced with it ON, while a customer would have run
    // without it. Go-To animations are variable-length, which is the whole reason this wait
    // exists; shipping the un-waited path would have been shipping an untested one.
    // Set-but-"0" still counts as OFF so a launcher can force-clear it for an A/B.
    config_.gotoMeterWait = !qEnvironmentVariableIsSet("ORION_GOTO_METER_WAIT")
        || qgetenv("ORION_GOTO_METER_WAIT").trimmed() != QByteArrayLiteral("0");
    // Retired: an environment variable cannot fork Tempo from Button timing.
    config_.tempoOwnClockOnly = false;
    // [ORION_FUSED_FIRE] Phase-1 fused t_tip posterior: fire AUTHORITY (default OFF ->
    // byte-identical ladder). Shadow compute+log defaults ON; ORION_FUSED_SHADOW=0 disables.
    // [ORION_PROBE] Settings override for the probe hold. Only a positive in-band value wins, so
    // an absent key or a 0 leaves the compiled default alone rather than silently zeroing the
    // press (a 0ms hold would emit a press and release on the same tick and spawn nothing).
    if (settings.probePressMs > 0.0) {
        config_.probePressMs = std::clamp(settings.probePressMs, 50.0, 2000.0);
    }
    if (settings.probeCallLeadMs > 0.0) {
        config_.probeCallLeadMs = std::clamp(settings.probeCallLeadMs, 0.0, 10000.0);
    }
    if (settings.probeGapMs > 0.0) {
        // Floor at call-lead + press + 500ms: the gap has to contain the WHOLE cycle (call for
        // the ball, wait for the pass, hold the shot), or the next probe would start while the
        // previous one is mid-shot and the run would fight itself.
        config_.probeGapMs = std::clamp(
            settings.probeGapMs,
            config_.probeCallLeadMs + config_.probePressMs + 500.0, 30000.0);
    }
    config_.fusedFireEnabled = settings.fusedFireEnabled
        || qEnvironmentVariableIsSet("ORION_FUSED_FIRE");
    config_.fusedShadowEnabled = settings.fusedShadowEnabled;
    if (qgetenv("ORION_FUSED_SHADOW").trimmed() == QByteArrayLiteral("0")) {
        config_.fusedShadowEnabled = false;
    }
    // [ORION_GRADE_V2] Phase-2 trajectory grading (settings grade_v2 OR the env var, mirroring
    // fused_fire). Default OFF — the flag IS the user's offline-validation gate (decision №2).
    config_.gradeV2Enabled = settings.gradeV2Enabled
        || qEnvironmentVariableIsSet("ORION_GRADE_V2");
    // === Ceiling stack flags (2026-07 perfect-green build; default OFF -> byte-identical). ===
    // [ORION_PLATEAU_AIM] H6 plateau time-center aim; [ORION_TEMPLATE_ARRIVAL] H4 template
    // green-arrival prior; [ORION_PRESS_T0] H5 press-anchored template select. The legacy bandit
    // setting is parsed for compatibility but has no live timing or learning authority.
    config_.plateauAimEnabled = settings.plateauAimEnabled
        || qEnvironmentVariableIsSet("ORION_PLATEAU_AIM");
    config_.templateArrivalEnabled = settings.templateArrivalEnabled
        || qEnvironmentVariableIsSet("ORION_TEMPLATE_ARRIVAL");
    config_.pressT0Enabled = settings.pressT0Enabled
        || qEnvironmentVariableIsSet("ORION_PRESS_T0");
    config_.banditLeadEnabled = settings.banditLeadEnabled
        || qEnvironmentVariableIsSet("ORION_BANDIT_LEAD");
    templateArrival_.configure(config_.templateArrivalK, config_.templateMatchMinFills,
                               config_.pressT0Enabled);
    banditTuner_.configure(config_.banditMinPullsPerArm, config_.banditMaxShots);
    config_.globalAppearToTipMs = learning.globalAppearToTipMs;
    config_.globalHoldToReleaseMs = learning.globalHoldToReleaseMs;
    config_.learnedLatencyMs = learning.learnedLatencyMs;
    config_.probeSpawnOffsetMs = learning.probeSpawnOffsetMs;
    config_.shotTypeAppearToTipMs = learning.shotTypeAppearToTipMs;
    config_.leadRebaselined = learning.leadRebaselined;
    config_.globalRiseVelocityPctMs = learning.globalRiseVelocityPctMs;
    config_.shotTypeLatencyMs = learning.shotTypeLatencyMs;
    config_.feedforwardAnchor = settings.feedforwardAnchor;
    // T2 anchor-validity gates (carryover rejection) for the meter-appear clock.
    config_.anchorMaxFirstFillPct = settings.anchorMaxFirstFillPct;
    config_.anchorRiseMinPct = settings.anchorRiseMinPct;
    config_.ownershipProofTwoFrame = settings.ownershipProofTwoFrame;
    // [ORION_TYPE_TRIM] per-shot-type tip-phase trim (default OFF; see RemapConfig).
    config_.tipPhaseTypeTrimEnabled = settings.tipPhaseTypeTrimEnabled;
    config_.tipPhaseTypeTrims = settings.tipPhaseTypeTrimMs;
    // [ORION_SLOW_METER_DEFER] Default OFF; armed by the launcher env key ORION_SLOW_METER_DEFER
    // (mirroring the W-series flags above). Env-only for now: the settings.json plumbing would
    // touch AppConfig.{h,cpp}, which are mid-flight in the concurrent UI cleanup -- add
    // `slow_meter_defer` there later as a one-line OR, exactly like ownership_proof_two_frame.
    // Set-but-"0" counts as OFF so a launcher can force-clear it for an A/B.
    config_.slowMeterDeferEnabled =
        qEnvironmentVariableIsSet("ORION_SLOW_METER_DEFER")
        && qgetenv("ORION_SLOW_METER_DEFER").trimmed() != QByteArrayLiteral("0");
    config_.stopReopenCorroborate = settings.stopReopenCorroborate;
    // [ORION_STOP_SUBFRAME] sub-frame end-of-rise stop dating for the phase learner (default OFF).
    config_.stopDatingSubframe = settings.stopDatingSubframe;
    // [ORION_DEV_FIRE_OFFSET] dev-only commanded fire-offset sweep hook (env-only, default OFF,
    // compiled out of production builds). Parsed once; malformed input leaves it disarmed.
    parseDevFireOffsetEnv();
    config_.phaseVetoDirectional = settings.phaseVetoDirectional;
    config_.tipPhaseAimFrozen = settings.tipPhaseAimFrozen;
    // [ORION_RUNG_IMMINENT] Extend the imminent hold's target to ladder rungs (default OFF).
    config_.tipPhaseRungImminentHold = settings.tipPhaseRungImminentHold;
    // [ORION_TEMPO_PARITY] TempoStick joins the canonical tip pipeline (default OFF).
    config_.tempoTipParity = settings.tempoTipParity;
    // [ORION_GOTO_PARITY] GoToStick joins the owned bounded-hold ladder.
    // Owner directive 2026-08-09 ("fix the go to shot bug (should use tempo)"): forced ON
    // regardless of the persisted value. The owner's live settings.json carries a
    // pre-directive goto_tip_parity=false and there is no UI to flip it. Same force-on
    // pattern as noDipEnabled above.
    //
    // The exclusion this lifts was never load-bearing — see the RemapConfig note: "nothing in
    // that ladder is Square-specific... every hold below is equally sound for it", and the
    // 2026-08-04..06 census where Go-To was 5 of 16 dated no-fire aborts. Parity can only
    // shorten a doomed wait, never extend one (maxHoldMs 1200 < gotoMaxHoldMs 1400).
    //
    // This is HALF the Go-To fix and does nothing on its own: the release edge was also wrong
    // (neutral instead of the mirrored down flick — see ORION_GOTO_DOWN_FLICK in
    // ShotReleasePolicy.h). Parity alone would only let Go-To hold longer before emitting a
    // release the game does not read.
    // Was: config_.gotoTipParity = settings.gotoTipParity;
    config_.gotoTipParity = true;
    // [ORION_USER_LEAD_AUTHORITY] user-set lead may satisfy readiness (default OFF; see RemapConfig).
    config_.userLeadSatisfiesAuthority = settings.userLeadSatisfiesAuthority;
    config_.tempoFadeMirrorGesture = settings.tempoFadeMirrorGesture;
    // [ORION_PROBE_CACHE_AUTHORITY] accept the probe-cache seed source (default OFF; see RemapConfig).
    config_.probeCachePriorAuthority = settings.probeCachePriorAuthority;
    // [ORION_PROBE_COUNT] warmup probe-run length (default 16 = today's behaviour).
    config_.latencyProbeCount = settings.latencyProbeCount;
    // [ORION_ANCHOR_BASE20] Candidate B: the anchor migration is ONE flag moving the whole
    // coupled constellation, never four independently-tunable knobs -- a partial migration
    // (e.g. anchor moved, band not) silently mis-aims every shot. Applied only on a regime
    // TRANSITION: with the flag steadily off, applyConfig never touches these fields at all,
    // which keeps this branch bit-identical to a build without the feature and preserves the
    // setTipPhaseConfigForTests() contract (hook values survive applyConfig). Values are
    // derived from the compiled defaults so ON -> OFF restores them exactly. Ladder count 4 is
    // MANDATORY with base 20: No-Dip is first seen at 32.4-37.3 fill, and with the default 2
    // rungs ({25,30}) it would miss every rung and fall off the phase ladder onto the biased
    // sampler chain (+61-92ms) that the phase member exists to replace.
    if (settings.tipPhaseAnchorBase20 != config_.anchorBase20) {
        const RemapConfig remapDefaults;
        const double shiftMs = settings.tipPhaseAnchorBase20 ? kAnchorBase20ShiftMs : 0.0;
        config_.anchorBase20 = settings.tipPhaseAnchorBase20;
        config_.tipPhaseAnchorPct = settings.tipPhaseAnchorBase20
            ? 20.0 : remapDefaults.tipPhaseAnchorPct;
        config_.tipPhaseConstantMs = remapDefaults.tipPhaseConstantMs + shiftMs;
        config_.tipPhaseSeedPhysicalMs = remapDefaults.tipPhaseSeedPhysicalMs + shiftMs;
        config_.tipPhaseLearnMinMs = remapDefaults.tipPhaseLearnMinMs + shiftMs;
        config_.tipPhaseLearnMaxMs = remapDefaults.tipPhaseLearnMaxMs + shiftMs;
        config_.tipPhaseAnchorLadderCount = settings.tipPhaseAnchorBase20
            ? 4 : remapDefaults.tipPhaseAnchorLadderCount;
    }
    // T4 tip gate: defer a due clock to the predicted tip crossing when vision is healthy.
    config_.tipGateEnabled = settings.tipGateEnabled;
    config_.tipGateCapMs = settings.tipGateCapMs;
    config_.memoryTrustEnabled = settings.memoryTrustEnabled;
    config_.memoryTrustMaxAgeMs = settings.memoryTrustMaxAgeMs;
    config_.memoryTrustFillSlackPct = settings.memoryTrustFillSlackPct;
    config_.memoryTrustMaxFillPct = settings.memoryTrustMaxFillPct;
    config_.greenConfirmFastPath = settings.greenConfirmFastPath;
    greenTracker_.setFastPath(config_.greenConfirmFastPath);
    // GRADER REMOVED in production: the post-release meter self-grade false-grades early/late at the
    // dead-top (the meter peaks ~100% then recedes for BOTH), so it mis-trained the offsets. The
    // launcher forces ORION_FREEZE_CAL on (OrionAppController) so the live app times OPEN-LOOP off the
    // meter — no self-grade ever drives learnFromOutcome, and the removed banner oracle can't bypass it.
    // Dev/test builds WITHOUT the env respect settings.freezeCalibration, so the calibration machinery's
    // unit tests can still exercise the learning path.
    config_.calibrationFrozen = settings.freezeCalibration || qEnvironmentVariableIsSet("ORION_FREEZE_CAL");
    // Actuation-lead floor (see RemapConfig::autonomousLeadFloorMs). Env overrides settings so a
    // live batch can sweep it without a rebuild. Bounded to the same 0..500ms envelope the
    // latency authority itself is validated against; anything outside that is ignored, not clamped,
    // so a typo cannot silently install a lead the authority gate would have rejected.
    // [ORION_USER_LEAD] the shipped user-facing Shot Lead. Copied verbatim; measuredLeadForActuationMs
    // does the band validation so an out-of-range persisted value degrades to "not configured"
    // (the pre-existing authority path) rather than installing a lead no other gate would accept.
    config_.userActuationLeadMs = settings.actuationLeadMs;
    config_.userActuationLeadSet = settings.actuationLeadUserSet;
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] The delay-condition Shot Lead offset. Copied
    // verbatim (AppConfig has already banded it); appliedMeterDelayLeadOffsetMs() re-validates
    // and gates it on an actually-applied delay, so a persisted value can never reach a
    // delay-0 session. Re-arm the uncalibrated advisory on every apply: the operator may have
    // just set the offset, and the next engage must be judged against the NEW value.
    config_.meterDelayLeadOffsetMs = settings.meterDelayLeadOffsetMs;
    meterDelayLeadUncalibratedWarnedMs_ = -1.0;
    // [ORION_LEAD_CEILING_WARN 2026-08-12] Say it AT LOAD when the delayed-condition lead cannot be
    // scheduled, instead of waiting for the operator to turn delay on and lose a session.
    //
    // THE BUG THIS ENDS. On 2026-08-12 the offset sat at 155 against a 96 ms headroom: base 290 +
    // 155 = 445 vs a 386 ceiling. Every delayed shot died SHOT LEAD CONFLICT -- 151 aborts, 0
    // releases -- and nothing said so until someone read two headers side by side. Worse, the band
    // in AppConfig (kMeterDelayLeadOffsetMaxMs = 400) is ITSELF above the ceiling, and cleanDouble
    // clamps silently, so the config layer can hand the engine a value it can never honour and
    // report nothing. Then the lead LEARNS: 290 drifted to 297 the same afternoon, which quietly
    // pushed a hand-picked "safe" 90 to 387 -- 0.7 ms over. Any fixed offset eventually crosses.
    //
    // Advisory only. It changes no timing and suppresses no shot -- the runtime fail-closed path is
    // unchanged. It fires regardless of whether delay is currently applied, because the whole point
    // is to be heard BEFORE the delayed session starts. De-duplicated on the (lead, offset) pair so
    // an apply storm cannot spam the Activity log.
    if (config_.meterDelayLeadOffsetMs > 0.0) {
        const double base = measuredLeadForActuationMs();
        const double ceiling = maxSchedulableTipLeadMs();
        const double total = base + config_.meterDelayLeadOffsetMs;
        if (std::isfinite(base) && std::isfinite(ceiling) && ceiling > 0.0 && total > ceiling) {
            const double key = base * 100000.0 + config_.meterDelayLeadOffsetMs;
            if (!qFuzzyCompare(leadCeilingWarnKey_, key)) {
                leadCeilingWarnKey_ = key;
                emit engineDiagnostic(
                    QStringLiteral("SHOT LEAD CONFLICT: Shot Lead %1ms + delayed-condition offset "
                                   "%2ms = %3ms exceeds the schedulable ceiling %4ms. Meter delay "
                                   "will abort EVERY shot while this holds; lower the offset to "
                                   "%5ms or less.")
                        .arg(base, 0, 'f', 0)
                        .arg(config_.meterDelayLeadOffsetMs, 0, 'f', 0)
                        .arg(total, 0, 'f', 0)
                        .arg(ceiling, 0, 'f', 0)
                        .arg(std::max(0.0, std::floor(ceiling - base)), 0, 'f', 0));
            }
        }
    }
    // [ORION_GREEN_CENTER] Aim inside the green window instead of on its late edge. AppConfig has
    // already banded these; copy verbatim so a settings sweep needs no rebuild. Env override
    // ORION_GREEN_CENTER_FRAC exists for the same reason the lead knobs have one -- so a live A/B
    // can be driven from the launcher without touching persisted user settings.
    config_.autonomousGreenCenterFrac = settings.autonomousGreenCenterFrac;
    config_.autonomousGreenCenterMaxMs = settings.autonomousGreenCenterMaxMs;
    if (qEnvironmentVariableIsSet("ORION_GREEN_CENTER_FRAC")) {
        bool fracOk = false;
        const double envFrac =
            qEnvironmentVariable("ORION_GREEN_CENTER_FRAC").trimmed().toDouble(&fracOk);
        if (fracOk && std::isfinite(envFrac) && envFrac >= 0.0 && envFrac <= 1.0) {
            config_.autonomousGreenCenterFrac = envFrac;
        }
    }
    bool leadOverrideFromEnv = false;
    if (qEnvironmentVariableIsSet("ORION_LEAD_FLOOR_MS")) {
        bool leadFloorOk = false;
        const double envFloor =
            qEnvironmentVariable("ORION_LEAD_FLOOR_MS").trimmed().toDouble(&leadFloorOk);
        if (leadFloorOk && std::isfinite(envFloor) && envFloor >= 0.0 && envFloor <= 500.0) {
            config_.autonomousLeadFloorMs = envFloor;
            leadOverrideFromEnv = true;
        }
    }
    // Portable label-bias correction (see RemapConfig::autonomousLeadBiasMs). Same 0..500 envelope
    // and same ignore-don't-clamp policy as the floor, so a typo cannot silently install a lead
    // the authority gate would have rejected. Sweep this to re-measure the bias on a new rig.
    if (qEnvironmentVariableIsSet("ORION_LEAD_BIAS_MS")) {
        bool leadBiasOk = false;
        const double envBias =
            qEnvironmentVariable("ORION_LEAD_BIAS_MS").trimmed().toDouble(&leadBiasOk);
        if (leadBiasOk && std::isfinite(envBias) && envBias >= 0.0 && envBias <= 500.0) {
            config_.autonomousLeadBiasMs = envBias;
            leadOverrideFromEnv = true;
        }
    }
    // Recomputed every apply (never latched): the dev override is live only while the env var is.
    // Its whole job is to keep a live sweep byte-identical to today, so it OUT-RANKS the user
    // setting — see measuredLeadForActuationMs.
    config_.leadOverrideFromEnv = leadOverrideFromEnv;
    // [ORION_TIP_PHASE] DOUBLE-COUNT GUARD -- name both, pick neither.
    //
    // autonomousLeadBiasMs and samplerHorizonDebiasEnabled are two corrections for ONE physical
    // error: the weighted-linear crossing arrives late (measured +92.6 ms median against real
    // landings; the audit's +61.0 on its own subset) because the meter eases in and the fit is
    // extrapolated. The lead bias cancels it by firing that much earlier; the horizon de-bias
    // cancels it by moving the crossing itself. Enabling both subtracts it twice and the bot
    // fires a full bias EARLY -- the exact regression the de-bias was written to prevent, which
    // is why the flag ships off.
    //
    // Deliberately a DIAGNOSTIC and not a silent resolution. Both knobs are legitimate; which one
    // is right depends on whether the operator wants the correction to travel between installs
    // (the bias does, the pivot is measured per-machine), and an engine that quietly disabled one
    // would make a live sweep of the other read as a null result for reasons nothing logged.
    if (std::isfinite(config_.autonomousLeadBiasMs) && config_.autonomousLeadBiasMs > 0.0
        && config_.samplerHorizonDebiasEnabled
        && std::isfinite(config_.samplerHorizonBiasMsPerMs)
        && config_.samplerHorizonBiasMsPerMs != 0.0) {
        emit engineDiagnostic(QStringLiteral(
            "TIMING CONFIG WARNING: double-counted predictor bias correction. "
            "autonomousLeadBiasMs=%1 (fires earlier by that much) and "
            "samplerHorizonDebiasEnabled=1 slope=%2 (moves the crossing earlier) both correct the "
            "SAME sampler extrapolation bias. Enabling both applies it twice. Set exactly one: "
            "ORION_LEAD_BIAS_MS=0 to keep the horizon de-bias, or samplerHorizonDebiasEnabled=false "
            "to keep the lead bias.")
                                  .arg(config_.autonomousLeadBiasMs, 0, 'f', 1)
                                  .arg(config_.samplerHorizonBiasMsPerMs, 0, 'f', 3));
    }
    // [ORION_AIM_FREEZE] While the manual Tip Timing latch holds, the previous session's own
    // measurement (persisted separately -- see LearningData::measuredPhasePhysicalMs) may
    // already refute the manual value. Warn at restore instead of waiting for this session to
    // re-fill half a learner window. Runs after the regime block above so the effective-frame
    // numbers in the warning use the active constant/seed.
    if (settings.tipPhaseAimFrozen && learning.measuredPhasePhysicalMs > 0.0) {
        maybeWarnTipTimingDivergence(frozenAimPhysicalMs_,
                                     learning.measuredPhasePhysicalMs + phaseRestoreShiftMs);
    }
    // [ORION_LEAD_CONFLICT] Two user knobs, one impossibility. A Shot Lead at/above the active
    // tip-timing constant (minus the measured decision-latency margin) can NEVER be scheduled by
    // the phase member: the command deadline is already past on the first tick it exists, so the
    // shot dies live_tip_deadline_missed and the only releases come from long-horizon sampler
    // arms -- the exact 2026-08-08 production signature (Tip Timing effective 314 ms, Shot Lead
    // 300/315/320 ms, every phase-source miss with lateness == lead - tip_eta, and the user
    // raising the lead further after each LATE, which only widens the conflict). Both values are
    // user-set and deliberately NOT overridden or clamped here -- an abort is fail-closed while a
    // silently re-timed release is a guaranteed mistime -- but the conflicting PAIR must be named
    // the moment it is applied instead of letting the session die shot by shot with a cryptic
    // reason code. De-duplicated per (lead, constant) pair so settings churn cannot spam.
    if (config_.tipPhaseEnabled && config_.userActuationLeadSet
        && !config_.leadOverrideFromEnv
        && std::isfinite(config_.userActuationLeadMs)
        && config_.userActuationLeadMs >= config_.actuationLeadMinMs
        && config_.userActuationLeadMs <= config_.actuationLeadMaxMs) {
        const double tipConstMs = effectiveTipPhaseConstantMs();
        const double maxUsableMs = maxSchedulableTipLeadMs();
        const bool conflict = config_.userActuationLeadMs > maxUsableMs;
        const bool samePairWarned =
            std::abs(config_.userActuationLeadMs - tipLeadConflictWarnedLeadMs_) <= 0.5
            && std::abs(tipConstMs - tipLeadConflictWarnedConstMs_) <= 0.5;
        if (conflict && !samePairWarned) {
            tipLeadConflictWarnedLeadMs_ = config_.userActuationLeadMs;
            tipLeadConflictWarnedConstMs_ = tipConstMs;
            // [ORION_METER_DELAY_LEAD_STARVATION] When an inbound meter delay is applied the
            // conflict is usually the DELAY's arithmetic, not a mistyped lead: the correct
            // lead is loop+delay while the visible runway stays the tip constant. Say so, and
            // give the one number that makes the pair schedulable again (max delay for this
            // lead's loop component). APPEND-ONLY: the base sentence is byte-identical so
            // existing parsers/tests of this line are untouched.
            // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] The advice, corrected. The delay's
            // contribution to the lead is now an explicit term (the delayed-condition offset),
            // so the number quoted is the OFFSET HEADROOM this pair leaves — not the old
            // "max meter delay", which assumed the operator had already folded the applied
            // delay into Shot Lead and therefore over-reported by exactly that delay whenever
            // they had not (2026-08-09: lead 295, applied 175, it claimed 271 ms of room on a
            // rig with 96).
            // maxMeterDelayForLeadMs() subtracts the in-force offset from whatever lead it is
            // handed, so it must be handed the KEYED lead (base + offset) — the same value
            // measuredLeadForActuationMs() returns — or it would under-report the headroom by
            // exactly the offset. The other two call sites already pass the keyed lead; this
            // one holds the raw setting, so it adds the term explicitly.
            const QString delayContext = meterDelayAppliedMs_ > 0.0
                ? QStringLiteral(
                      " Meter delay %1ms is applied; the delayed condition is reached with the "
                      "Shot Lead offset, and this pair leaves +%2ms of offset headroom.")
                      .arg(meterDelayAppliedMs_, 0, 'f', 0)
                      .arg(maxMeterDelayForLeadMs(config_.userActuationLeadMs
                                                  + appliedMeterDelayLeadOffsetMs()),
                           0, 'f', 0)
                : QString();
            emit engineDiagnostic(QStringLiteral(
                "SHOT LEAD CONFLICT: Shot Lead %1ms > usable max %2ms for tip timing %3ms. "
                "Live tip shots CANNOT be scheduled and will abort. Lower Shot Lead to <=%2ms "
                "or raise/reset Tip Timing.%4")
                                      .arg(config_.userActuationLeadMs, 0, 'f', 0)
                                      .arg(maxUsableMs, 0, 'f', 0)
                                      .arg(tipConstMs, 0, 'f', 0)
                                      .arg(delayContext));
            emit shotLeadConflictDiagnosed(config_.userActuationLeadMs, maxUsableMs,
                                           tipLeadConflictMisses_);
        } else if (!conflict) {
            // The active pair is schedulable; re-arm so a future regression warns again
            // (clearing only on an exact stored-pair match would leave the latch loaded after
            // lead 320 -> 250 -> 320 and silently swallow the re-created conflict).
            tipLeadConflictWarnedLeadMs_ = -1.0;
            tipLeadConflictWarnedConstMs_ = -1.0;
        }
    }
    // [ORION_GRADE_V2] session-start offset baseline: the v2 sole-writer trim may drift each
    // bucket at most ±gradeV2DriftBoundMs away from the value it started this session with.
    // Guard state starts clean per applyConfig (session-scoped, like the phase streaks below).
    gradeV2OffsetBaseline_ = config_.shotTypeLearnedOffsetMs;
    gradeV2RecentErr_.clear();
    gradeV2FreezeShots_ = 0;
    // Per-type ACQUIRE->LOCK runtime streaks always start clean (only the phase persists).
    calGreens_.clear();
    calMisses_.clear();
    // Seed the annealing counter: a bucket that already has a persisted (non-zero)
    // offset is treated as calibrated, so it starts at the small stable gain instead of
    // aggressively re-adjusting a converged offset. Fresh buckets (offset 0) start at
    // count 0 -> the large initial gain for fast from-scratch calibration. A converged
    // bucket with no explicit persisted phase (an older learning.json) is also seeded LOCKED,
    // so a dialed-in type restarts in micro-trim mode instead of re-hunting its baseline.
    config_.shotTypeLearnCount.clear();
    for (auto it = config_.shotTypeLearnedOffsetMs.constBegin();
         it != config_.shotTypeLearnedOffsetMs.constEnd(); ++it) {
        if (std::abs(it.value()) > 0.01) {
            config_.shotTypeLearnCount.insert(it.key(), 6);
            if (!config_.shotTypeCalPhase.contains(it.key())) {
                config_.shotTypeCalPhase.insert(it.key(), 1);   // 1 = Lock
            }
        }
    }
    const bool liveMeterAuthorityLost = priorLiveMeterAuthority
        && !autonomousLiveMeterTimingEnabled();
    const bool routeDisabled = priorRouteEnabled && !config_.enabled;
    // A config application is an authority boundary for an unowned physical
    // gesture.  Its thresholds/source/remap may have changed underneath the
    // candidate, so fence that exact epoch even when automatic calibration has
    // just converged and already cleared latencyCalibrationMode_.  Doing this
    // synchronously also closes enabled off->on between controller ticks.
    if (pendingOwnershipAtConfigBoundary && shot_.state == HoldState::Idle) {
        cancelPendingLatencyCalibrationOwnership();
    }
    if ((liveMeterAuthorityLost || routeDisabled)
        && shot_.state != HoldState::Idle) {
        // A release already submitted remains authoritative and must finish its
        // pulse/cooldown even when settings revoke future ownership. Pre-release
        // state is aborted, but its output representation drains to the same
        // debounced physical end instead of bouncing through one raw poll.
        if (shot_.state == HoldState::Releasing || shot_.state == HoldState::Cooldown) {
            latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                  shot_.mode, shot_.shotType);
        } else {
            abort(QStringLiteral("authority_mode_changed_abort"));
            if (shot_.state == HoldState::Releasing) {
                latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                      shot_.mode, shot_.shotType);
            }
        }
    }
    if ((latencyCalibrationMode_ && !autonomousLiveMeterTimingEnabled())
        || liveMeterAuthorityLost || routeDisabled) {
        // Calibration is meaningful only for strict live-meter timing. A
        // settings/source transition must not retain probe intent after its
        // release contract disappeared.
        latencyCalibrationMode_ = false;
        latencyCalibrationAutomatic_ = false;
        clearPendingMeterOwnershipEpisode();
        clearPendingMeterOwnershipEvidence();
        clearPendingStickCalibration();
        refreshLatencyCalibrationStatus(true);
    }
}

void AutomationEngine::updatePoseLandmark(const QString& kind, int frameSeq, double confidence,
                                          quint64 armToken)
{
    // Pose is lab-only release authority. Ignore overlay/late sidecar traffic unless
    // a no-meter shot currently owns the input; beginShot resets pose_ so an idle
    // landmark can never prime the next shot.
    const bool shotActive = shot_.state == HoldState::Armed
        || shot_.state == HoldState::Holding
        || shot_.state == HoldState::GreenWindow;
    if (!config_.noMeterEnabled || !shotActive) {
        return;
    }

    // Shot identity is checked before kind/frame/confidence can mutate pose state.
    // A landmark may have been detected correctly but sat in the sidecar/stdout
    // pipeline until the next shot began; without this fence its fresh arrival
    // clock and increasing frame_seq could authorize the wrong physical shot.
    if (!poseArmTokenMatches(shot_.armToken, armToken)) {
        pose_.lastLandmarkFreshAccept = false;
        invalidateUnconfirmedPoseSchedule();
        return;
    }

    const QString normalizedKind = kind.trimmed().toLower();
    const bool kindValid = normalizedKind == QLatin1String("push")
        || normalizedKind == QLatin1String("release");
    const bool sequenceAdvanced = frameSeq >= 0
        && (pose_.lastLandmarkFrameSeq < 0 || frameSeq > pose_.lastLandmarkFrameSeq);

    // The latest payload owns authority. A replay/out-of-order identity, malformed
    // kind, non-finite confidence, or below-gate landmark revokes a copied pose
    // deadline instead of silently preserving authority from an older frame.
    if (!sequenceAdvanced) {
        pose_.lastLandmarkFreshAccept = false;
        invalidateUnconfirmedPoseSchedule();
        return;
    }
    pose_.lastLandmarkFrameSeq = frameSeq;
    if (!kindValid || !std::isfinite(confidence)
        || confidence < config_.noMeterConfidenceGate) {
        pose_.lastLandmarkFreshAccept = false;
        invalidateUnconfirmedPoseSchedule();
        return;
    }
    const bool kindAlreadyLatched = normalizedKind == QLatin1String("push")
        ? pose_.pushReceived : pose_.releaseReceived;
    if (kindAlreadyLatched) {
        pose_.lastLandmarkFreshAccept = false;
        invalidateUnconfirmedPoseSchedule();
        return;
    }

    // A newer genuine landmark is a new authority epoch. Fence any unconfirmed
    // deadline derived from the previous pose frame before publishing the new one.
    invalidateUnconfirmedPoseSchedule();
    const double now = nowMs();
    if (normalizedKind == QLatin1String("push")) {
        if (!pose_.pushReceived) {
            pose_.pushReceived = true;
            pose_.pushArrivalMs = now;
            pose_.pushFrameSeq = frameSeq;
            pose_.pushConfidence = confidence;
        }
    } else {
        if (!pose_.releaseReceived) {
            pose_.releaseReceived = true;
            pose_.releaseArrivalMs = now;
            pose_.releaseFrameSeq = frameSeq;
            pose_.releaseConfidence = confidence;
        }
    }
    pose_.lastLandmarkFreshAccept = true;
    pose_.lastLandmarkArrivalMs = now;
    pose_.acceptedArmToken = armToken;
    ++pose_.authorityEpoch;
}

void AutomationEngine::updateDetection(const DetectionResult& result)
{
    const double sampleNow = nowMs();
    // Invalid decoder payloads are negative evidence, never proof frames. In strict
    // live-meter mode, however, one such callback must not erase the valid current-shot
    // frames immediately around it. Retain only a bounded candidate whose first frame
    // already carried this exact physical-shot epoch; recordPendingMeterOwnershipSample()
    // still rejects the bad payload, never increments the proof count, and restarts the
    // episode if the next genuine frame arrives outside the continuity window.
    const auto clearPendingUnlessBoundedCurrentProof = [this, sampleNow]() {
        const bool strictOwnershipProof = latencyCalibrationAutomatic_
            || autonomousLiveMeterTimingEnabled();
        ShotMode mode = ShotMode::ButtonShot;
        double gestureStartMs = -1.0;
        quint64 physicalEpoch = 0;
        QString shotType;
        bool inputQualified = false;
        const bool candidate = pendingMeterOwnershipCandidate(
            sampleNow, mode, gestureStartMs, physicalEpoch, shotType, inputQualified);
        Q_UNUSED(shotType);
        Q_UNUSED(inputQualified);
        const bool preserve = strictOwnershipProof && candidate
            && pendingMeterOwnership_.active && shot_.state == HoldState::Idle
            && pendingMeterOwnership_.mode == mode
            && std::abs(pendingMeterOwnership_.gestureStartMs - gestureStartMs) <= 1e-6
            && pendingMeterOwnership_.physicalEpoch == physicalEpoch
            && pendingMeterOwnership_.first.gameplayStructureVerified
            && pendingMeterOwnership_.first.gameplayStructureEpoch == physicalEpoch
            && sampleNow >= pendingMeterOwnership_.lastMs
            && sampleNow - pendingMeterOwnership_.lastMs <= kPendingMeterOwnershipMaxGapMs;
        if (!preserve) {
            clearPendingMeterOwnershipEpisode();
        }
    };
    bool priorSampleGenuine = shot_.lastSampleGenuineAccept;
    // Defense-in-depth frame identity. RemotePlaySession normally de-duplicates
    // frame_count before constructing DetectionResult, but the engine is the final
    // release authority and must not treat a re-served decoder frame as new timing
    // evidence. Prefer the capture timestamp when both samples have one (decoder
    // frame numbers may restart after reconnect); otherwise fall back to frameNumber.
    const bool hasCaptureTs = std::isfinite(result.captureTsMs) && result.captureTsMs > 0.0;
    const bool hasMeasurementCaptureTs = std::isfinite(result.measurementCaptureTsMs)
        && result.measurementCaptureTsMs > 0.0;
    const bool hadCaptureTs = std::isfinite(lastDetectorCaptureTsMs_)
        && lastDetectorCaptureTsMs_ > 0.0;
    bool duplicateDetectedFrame = false;
    bool outOfOrderDetectedFrame = false;
    if (hasCaptureTs && hadCaptureTs) {
        const double captureDeltaMs = result.captureTsMs - lastDetectorCaptureTsMs_;
        duplicateDetectedFrame = std::abs(captureDeltaMs) < 0.001;
        outOfOrderDetectedFrame = captureDeltaMs < -0.001;
    } else if (result.frameNumber >= 0 && lastDetectorFrameNumber_ >= 0) {
        duplicateDetectedFrame = result.frameNumber == lastDetectorFrameNumber_;
        outOfOrderDetectedFrame = result.frameNumber < lastDetectorFrameNumber_;
    }
    // A decoder/sidecar sequence may restart while capture timestamps keep advancing.
    // That is a new source epoch, not an out-of-order wall-clock sample. Detector timing
    // already follows captureTs; the measured-lead estimator additionally needs an epoch
    // fence so an old converged count cannot silently authorize the restarted source.
    const bool detectorSequenceRestart = hasCaptureTs && hadCaptureTs
        && result.captureTsMs > lastDetectorCaptureTsMs_ + 0.001
        && result.frameNumber >= 0 && lastDetectorFrameNumber_ >= 0
        && result.frameNumber < lastDetectorFrameNumber_;
    // Per-shot detection census (diagnostic only — see ShotContext). Counted for
    // every sample that arrives while THIS shot is Armed/Holding, including the
    // early-return paths below, so the "Release detsummary:" line attributes a
    // starved shot to the exact gate that blocked its samples.
    const bool censusActive = (shot_.state == HoldState::Armed || shot_.state == HoldState::Holding);
    if (censusActive) {
        shot_.detSamplesTotal += 1;
        shot_.lastDetectionStage = result.stage.trimmed().isEmpty()
            ? QStringLiteral("none") : result.stage.trimmed().left(64);
        shot_.lastDetectionRejectionReason = result.rejectionReason.trimmed().isEmpty()
            ? QStringLiteral("none") : result.rejectionReason.trimmed().left(96);
        shot_.lastDetectionPayloadSource = result.detectorSource.trimmed().isEmpty()
            ? QStringLiteral("none") : result.detectorSource.trimmed().left(64);
    }
    // [ORION_MEASURED_LEAD] Consume value/N/SD as ONE estimator snapshot. The old
    // code independently overwrote value/SD but max-latched N forever, so a restarted
    // sidecar's N=1 point actuated as the prior process's N>=6 estimate. Missing fields
    // also left the old values live indefinitely. Every authoritative source frame now
    // either replaces the whole snapshot or clears the whole authority state.
    const bool frameIdentityAuthoritative = !duplicateDetectedFrame && !outOfOrderDetectedFrame;
    if (frameIdentityAuthoritative) {
        const quint64 incomingLatencyScopeEpoch = result.measuredLatencyScopeEpoch;
        const quint64 priorLatencyScopeEpoch = measuredLatencyScopeEpoch_;
        const bool latencyScopeTransition = incomingLatencyScopeEpoch != 0
            && priorLatencyScopeEpoch != 0
            && incomingLatencyScopeEpoch != priorLatencyScopeEpoch;
        if (incomingLatencyScopeEpoch != 0) {
            if (priorLatencyScopeEpoch == 0
                || incomingLatencyScopeEpoch > priorLatencyScopeEpoch) {
                measuredLatencyScopeEpoch_ = incomingLatencyScopeEpoch;
            }
            // A process-monotonic regression is stale/corrupt telemetry. Keep the
            // high-water mark, but revoke just as strictly as a legitimate advance.
            if (latencyScopeTransition) {
                if (config_.measuredLeadEnabled && measuredLeadEpochReady_) {
                    invalidateUnconfirmedVisionSchedule("detect_reject");
                }
                measuredLeadEpochReady_ = false;
                publishControllerDeliveryRouteAttestation(
                    0, LatencyControllerRoute::None);
            }
        }
        const bool currentLatencyScopeEpoch = incomingLatencyScopeEpoch != 0
            && incomingLatencyScopeEpoch == measuredLatencyScopeEpoch_;
        const bool latencySnapshotValid = std::isfinite(result.measuredLatencyMs)
            && result.measuredLatencyMs > 0.0 && result.measuredLatencyMs <= 500.0
            && result.measuredLatencyN >= 0
            && std::isfinite(result.measuredLatencySdMs)
            && result.measuredLatencySdMs > 0.0 && result.measuredLatencySdMs <= 250.0;
        const QString incomingAuthorityKind =
            result.measuredLatencyAuthorityKind.trimmed().toLower();
        const bool authorityNumbersValid =
            std::isfinite(result.measuredLatencyAuthorityMs)
            && result.measuredLatencyAuthorityMs > 0.0
            && result.measuredLatencyAuthorityMs <= 500.0
            && std::isfinite(result.measuredLatencyAuthoritySdMs)
            && result.measuredLatencyAuthoritySdMs > 0.0
            && result.measuredLatencyAuthoritySdMs <= 250.0;
        const bool authorityNoneValid = incomingAuthorityKind == QLatin1String("none")
            && std::abs(result.measuredLatencyAuthorityMs) <= 1e-9
            && std::abs(result.measuredLatencyAuthoritySdMs) <= 1e-9;
        // A validated tuple is the same immutable snapshot generation as the
        // observed posterior. Reject a torn/mixed-generation payload instead of
        // selecting whichever individual fields happen to look authoritative.
        const bool validatedAuthorityContract =
            incomingAuthorityKind == QLatin1String("validated")
            && authorityNumbersValid && !result.measuredLatencyFactoryPrior
            && std::abs(result.measuredLatencyAuthorityMs
                        - result.measuredLatencyMs) <= 0.05
            && std::abs(result.measuredLatencyAuthoritySdMs
                        - result.measuredLatencySdMs) <= 0.05;
        const bool factoryAuthorityContract =
            incomingAuthorityKind == QLatin1String("factory")
            && authorityNumbersValid && result.measuredLatencyFactoryPrior;
        const bool latencyAuthorityContractValid = authorityNoneValid
            || validatedAuthorityContract || factoryAuthorityContract;
        const QString acceptedAuthorityKind = latencyAuthorityContractValid
            ? incomingAuthorityKind : QStringLiteral("none");
        const bool priorSnapshotExpired = measuredLeadTelemetryPresent_
            && measuredLeadLastUpdateMs_ >= 0.0
            && config_.measuredLeadFreshnessMs > 0.0
            && sampleNow - measuredLeadLastUpdateMs_ > config_.measuredLeadFreshnessMs;
        const bool estimatorRestart = latencySnapshotValid && measuredLeadTelemetryPresent_
            && result.measuredLatencyN < measuredLatencyN_;
        const bool newEstimatorEpoch = estimatorRestart || detectorSequenceRestart
            || latencyScopeTransition
            || priorSnapshotExpired;

        if (!latencySnapshotValid || newEstimatorEpoch) {
            if (config_.measuredLeadEnabled && measuredLeadEpochReady_) {
                // Revoke a copied, not-yet-submitted measured/fused deadline before dropping
                // its lead authority. Confirmed submissions are fenced by the scheduler itself.
                invalidateUnconfirmedVisionSchedule("detect_duplicate");
            }
            measuredLeadEpochReady_ = false;
            // A cold/missing/expired estimator snapshot revokes only VALUE authority.  It does
            // not prove that the native-to-sidecar controller route changed, so clearing the
            // independent route command token here caused every ordinary detector frame to mint
            // another generation.  A genuine detector/source sequence transition is the sole
            // frame-driven revocation; explicit session/backend transitions revoke through
            // reset() or setControllerDeliveryRouteAttestation(0, None).
            if (detectorSequenceRestart || latencyScopeTransition) {
                publishControllerDeliveryRouteAttestation(
                    0, LatencyControllerRoute::None);
            }
        }

        if (latencySnapshotValid) {
            if (newEstimatorEpoch) {
                // Below six, the absolute convergence gate is sufficient. If a restarted
                // source already reports a converged count, require one genuinely newer label
                // so replaying an old terminal snapshot cannot reopen authority.
                measuredLeadRequireNProgress_ = true;
                measuredLeadRestartFloorN_ = result.measuredLatencyN;
            } else if (!measuredLeadTelemetryPresent_ && measuredLeadEverValid_) {
                // A prior absent/invalid payload ended the old epoch. Establish the first
                // count of the fresh epoch as its progress floor.
                measuredLeadRequireNProgress_ = true;
                measuredLeadRestartFloorN_ = result.measuredLatencyN;
            }

            measuredLatencyMs_ = result.measuredLatencyMs;
            measuredLatencyN_ = result.measuredLatencyN;
            measuredLatencySdMs_ = result.measuredLatencySdMs;
            // [ORION_PRESS_ANCHOR] l_fixed (rtt_now + tick_wait stripped) rides the same
            // snapshot — it is the V (video-pipeline) estimate for the press-anchored
            // observation/predictor pair. Not authority: consumers treat 0 as absent.
            measuredLFixedMs_ = std::isfinite(result.measuredLFixedMs)
                    && result.measuredLFixedMs > 0.0
                ? result.measuredLFixedMs : 0.0;
            measuredLatencyAuthorityKind_ = acceptedAuthorityKind;
            measuredLatencyAuthorityMs_ = latencyAuthorityContractValid
                && acceptedAuthorityKind != QLatin1String("none")
                ? result.measuredLatencyAuthorityMs : 0.0;
            measuredLatencyAuthoritySdMs_ = latencyAuthorityContractValid
                && acceptedAuthorityKind != QLatin1String("none")
                ? result.measuredLatencyAuthoritySdMs : 0.0;
            measuredLatencyControlledAnchor_ = result.measuredLatencyControlledAnchor;
            measuredLatencyProvisional_ = result.measuredLatencyProvisional;
            measuredLatencyRestored_ = result.measuredLatencyRestored;
            measuredLatencyVideoRouteAttested_ =
                result.measuredLatencyVideoRouteAttested;
            measuredLatencyFactoryPrior_ = result.measuredLatencyFactoryPrior;
            measuredLatencyPriorSource_ = result.measuredLatencyPriorSource;
            measuredLatencyModelVersion_ = result.measuredLatencyModelVersion;
            measuredLatencyAttestationGeneration_ = currentLatencyScopeEpoch
                ? result.measuredLatencyAttestationGeneration : 0;
            measuredLatencyDeliveryRoute_ = currentLatencyScopeEpoch
                ? result.measuredLatencyDeliveryRoute
                : LatencyControllerRoute::None;
            measuredLeadLastUpdateMs_ = sampleNow;
            measuredLeadTelemetryPresent_ = true;
            measuredLeadEverValid_ = true;

            // [ORION_LEAD_CONFLICT] The in-band Shot Lead REPLACES this authority outright
            // (measuredLeadForActuationMs), so a validated posterior that contradicts it is
            // otherwise invisible: 2026-08-08 the sidecar minted validated 197.1ms sd=3.2 n=6
            // at 05:41:34Z and the engine kept firing decisions labelled lead_ms=320 with
            // nothing anywhere surfacing the 123ms disagreement. Advisory only -- the user's
            // value keeps firing; nothing is adopted on their behalf.
            maybeWarnLeadAuthorityDisagreement();

            // [ORION_USER_LEAD 2026-08-08] The measured-lead readout/seed now comes from HERE --
            // the marker-anchored VALIDATED estimator authority -- instead of the refuted
            // travel_pp/velocity landing proxy (see recordLandingLeadSample). Same signal, same
            // controller contract: the handler seeds only a never-configured Shot Lead and
            // otherwise just refreshes the card readout. 1 ms hysteresis so a posterior
            // refining every telemetry frame does not churn the UI binding per frame.
            if (measuredLatencyAuthorityKind_ == QLatin1String("validated")
                && measuredLatencyAuthorityMs_ > 0.0
                && measuredLatencyAuthoritySdMs_ > 0.0
                && std::abs(measuredLatencyAuthorityMs_ - lastPublishedMeasuredSeedMs_)
                    >= 1.0) {
                lastPublishedMeasuredSeedMs_ = measuredLatencyAuthorityMs_;
                emit actuationLeadMeasured(measuredLatencyAuthorityMs_, measuredLatencyN_);
            }

            const bool countConverged = measuredLatencyN_ >= 6;
            constexpr double kConvergedPosteriorSdMs = 3.3;
            const bool posteriorConverged = measuredLatencySdMs_ <= kConvergedPosteriorSdMs;
            constexpr double kProvisionalPosteriorSdMs = 6.0;
            const bool controlledWarmStart = measuredLatencyProvisional_
                && measuredLatencyControlledAnchor_
                && measuredLatencyN_ >= 2
                && measuredLatencySdMs_ <= kProvisionalPosteriorSdMs;
            const bool freshEpochProgressed = !measuredLeadRequireNProgress_
                || measuredLatencyN_ > measuredLeadRestartFloorN_;
            const auto expectedAttestation =
                controllerDeliveryRouteAttestationSnapshot();
            const quint64 expectedAttestationGeneration = expectedAttestation.generation;
            const LatencyControllerRoute expectedAttestationRoute = expectedAttestation.route;
            const bool exactControllerRouteEcho =
                measuredLatencyScopeEpoch_ != 0
                && expectedAttestationGeneration != 0
                && measuredLatencyAttestationGeneration_
                    == expectedAttestationGeneration
                && expectedAttestationRoute != LatencyControllerRoute::None
                && measuredLatencyDeliveryRoute_ == expectedAttestationRoute;
            const bool routeProofInPlay = expectedAttestationGeneration != 0
                || measuredLatencyAttestationGeneration_ != 0
                || measuredLatencyDeliveryRoute_ != LatencyControllerRoute::None;
            // Legacy test/offline snapshots without either side of the route protocol remain
            // usable. Once native or sidecar publishes a route generation, every learned value
            // (not only restored caches) must echo the exact live generation and route. This
            // prevents an in-flight old-route frame from reopening authority after a re-key.
            const bool learnedControllerRouteReady = !routeProofInPlay
                || exactControllerRouteEcho;
            const bool restoredRouteReady = !measuredLatencyRestored_
                || (measuredLatencyVideoRouteAttested_ && exactControllerRouteEcho);
            const bool validatedAuthorityStructured =
                measuredLatencyAuthorityKind_ == QLatin1String("validated")
                && validatedAuthorityContract;
            const bool factoryPriorStructured =
                measuredLatencyAuthorityKind_ == QLatin1String("factory")
                && factoryAuthorityContract
                && measuredLatencyFactoryPrior_
                && measuredLatencyN_ < 6
                && !measuredLatencyRestored_
                && measuredLatencyAuthoritySdMs_ >= 6.0
                && measuredLatencyAuthoritySdMs_ <= 100.0
                // [ORION_PROBE_CACHE_AUTHORITY] (default OFF) The probe-persisted lead cache
                // restores with this exact replacement source; route safety is carried by the
                // cache's own scope-digest keying plus the attestation echo in
                // factoryRouteReady below (see RemapConfig::probeCachePriorAuthority).
                && (factoryLatencyPriorMatchesRoute(
                        measuredLatencyPriorSource_, measuredLatencyDeliveryRoute_,
                        latencyVideoRoute_)
                    || (config_.probeCachePriorAuthority
                        && measuredLatencyPriorSource_
                            == QLatin1String("probe_cache:self_measured")))
                && !measuredLatencyModelVersion_.isEmpty();
            const bool factoryRouteReady = factoryPriorStructured
                && expectedAttestationGeneration != 0
                && measuredLatencyAttestationGeneration_
                    == expectedAttestationGeneration
                && expectedAttestationRoute != LatencyControllerRoute::None
                && measuredLatencyDeliveryRoute_ == expectedAttestationRoute;
            if ((validatedAuthorityStructured
                    && ((countConverged && posteriorConverged) || controlledWarmStart)
                    && freshEpochProgressed && restoredRouteReady
                    && learnedControllerRouteReady)
                || factoryRouteReady) {
                // Live-meter-only timing consumes no learned clock, so engaging
                // measured lead must not mutate/rebaseline those clocks or depend
                // on the persisted compatibility latch. Legacy timing retains its
                // one-time shift while that path remains available.
                if (config_.measuredLeadEnabled && !autonomousLiveMeterTimingEnabled()
                    && !config_.leadRebaselined) {
                    rebaselineLeadClocks();
                }
                measuredLeadEpochReady_ = autonomousLiveMeterTimingEnabled()
                    || config_.leadRebaselined;
                if (measuredLeadEpochReady_) {
                    measuredLeadRequireNProgress_ = false;
                    measuredLeadRestartFloorN_ = -1;
                }
            } else {
                if (config_.measuredLeadEnabled && measuredLeadEpochReady_) {
                    invalidateUnconfirmedVisionSchedule("detect_out_of_order");
                }
                measuredLeadEpochReady_ = false;
            }
        } else {
            measuredLatencyMs_ = 0.0;
            measuredLatencyN_ = 0;
            measuredLatencySdMs_ = 0.0;
            measuredLFixedMs_ = 0.0;   // [ORION_PRESS_ANCHOR]
            measuredLatencyAuthorityKind_ = QStringLiteral("none");
            measuredLatencyAuthorityMs_ = 0.0;
            measuredLatencyAuthoritySdMs_ = 0.0;
            measuredLatencyControlledAnchor_ = false;
            measuredLatencyProvisional_ = false;
            measuredLatencyRestored_ = false;
            measuredLatencyVideoRouteAttested_ = false;
            measuredLatencyFactoryPrior_ = false;
            measuredLatencyPriorSource_.clear();
            measuredLatencyModelVersion_.clear();
            measuredLatencyAttestationGeneration_ = 0;
            measuredLatencyDeliveryRoute_ = LatencyControllerRoute::None;
            measuredLeadLastUpdateMs_ = -1.0;
            measuredLeadTelemetryPresent_ = false;
            if (measuredLeadEverValid_) {
                measuredLeadRequireNProgress_ = true;
                measuredLeadRestartFloorN_ = -1;
            }
        }
        refreshLatencyCalibrationStatus();

        // Tick phase follows the same fail-closed snapshot contract: absence means
        // no authority. The previous overwrite-only latch retained a probe phase after
        // a restarted sidecar stopped supplying the fields.
        const bool tickSnapshotValid = std::isfinite(result.tickPhaseConf)
            && result.tickPhaseConf > 0.0 && result.tickPhaseConf <= 1.0
            && std::isfinite(result.tickPhaseMs) && result.tickPhaseMs >= 0.0
            && std::isfinite(result.tickPhaseSdMs) && result.tickPhaseSdMs > 0.0;
        if (tickSnapshotValid && !detectorSequenceRestart) {
            tickPhaseEpochMs_ = result.tickPhaseMs;
            tickPhaseConf_ = result.tickPhaseConf;
            tickPhaseSdMs_ = result.tickPhaseSdMs;
            tickPhaseLastUpdateMs_ = sampleNow;
        } else {
            tickPhaseEpochMs_ = -1.0;
            tickPhaseConf_ = 0.0;
            tickPhaseSdMs_ = -1.0;
            tickPhaseLastUpdateMs_ = -1.0;
        }
    }
    // Detector-only release-window diagnostic. This compares our release COMMAND with the
    // colour-derived meter window; it is not evidence of NBA 2K's make/miss outcome. Accept only
    // a real marker-backed record whose immutable native release id matches the latest submitted
    // release. It may be logged for transport/detector forensics, but is never routed to session
    // accuracy, timing learning, or the bandit tuner.
    const bool releaseWindowDiagnosticPresent = result.greenGradeLabel >= 0
        && result.greenGradeLabel <= 2 && result.greenGradeSeq >= 0;
    if (releaseWindowDiagnosticPresent) {
        constexpr double kMinWindowConfidence = 0.75; // at least three exposed samples
        const bool metricsValid = std::isfinite(result.greenGradeWindowConfidence)
            && result.greenGradeWindowConfidence >= kMinWindowConfidence
            && result.greenGradeWindowConfidence <= 1.0
            && std::isfinite(result.greenGradeStartPct)
            && result.greenGradeStartPct >= 50.0 && result.greenGradeStartPct <= 100.0
            && std::isfinite(result.greenGradeFillAtRelease)
            && result.greenGradeFillAtRelease >= 0.0
            && result.greenGradeFillAtRelease <= 110.0;
        const bool releaseMatched = !result.greenGradeReleaseProxy
            && result.greenGradeReleaseSeq > 0
            && result.greenGradeReleaseSeq == lastReleaseMarkerSeq_
            && result.greenGradePhysicalShotEpoch != 0
            && result.greenGradePhysicalShotEpoch == lastReleaseMarkerPhysicalShotEpoch_
            && result.greenGradeShotAttempt != 0
            && result.greenGradeShotAttempt == lastReleaseMarkerShotAttempt_;
        if (metricsValid && releaseMatched
            && result.greenGradeReleaseSeq != lastReleaseWindowDiagnosticSeq_) {
            lastReleaseWindowDiagnosticSeq_ = result.greenGradeReleaseSeq;
            emit engineDiagnostic(QStringLiteral(
                "Release-window identity: physical_epoch=%1 shot_attempt=%2 release_seq=%3")
                                      .arg(result.greenGradePhysicalShotEpoch)
                                      .arg(result.greenGradeShotAttempt)
                                      .arg(result.greenGradeReleaseSeq));
            emit releaseWindowDiagnostic(result.greenGradeReleaseSeq,
                                         lastReleaseMarkerShotType_,
                                         result.greenGradeLabel,
                                         result.greenGradeFillAtRelease,
                                         result.greenGradeStartPct);
        } else if (!metricsValid || !releaseMatched) {
            emit engineDiagnostic(QStringLiteral(
                "Release-window diagnostic dropped: diag_seq=%1 release_seq=%2 "
                "latest_marker_release=%3 "
                "physical_epoch=%4/%5 shot_attempt=%6/%7 proxy=%8 window_conf=%9")
                    .arg(result.greenGradeSeq)
                    .arg(result.greenGradeReleaseSeq)
                    .arg(lastReleaseMarkerSeq_)
                    .arg(result.greenGradePhysicalShotEpoch)
                    .arg(lastReleaseMarkerPhysicalShotEpoch_)
                    .arg(result.greenGradeShotAttempt)
                    .arg(lastReleaseMarkerShotAttempt_)
                    .arg(result.greenGradeReleaseProxy ? 1 : 0)
                    .arg(result.greenGradeWindowConfidence, 0, 'f', 2));
        }
    }
    // Clock re-baselining is performed atomically with snapshot convergence above.
    // The persisted one-time latch stays separate from measuredLeadEpochReady_, so a
    // sidecar restart cannot shift the learned clocks a second time.
    // [ORION_FUSED_FIRE] Epoch(capture)<->engine bridge: robust init (median of the first 5
    // stamped samples) then slow EMA. Converts the sidecar's epoch-clock post-hoc tip labels
    // onto the engine clock. Survives beginShot (engine-scoped, like measuredLatencyMs_).
    const bool captureAgeUsable = std::isfinite(result.frameAgeMs)
        && result.frameAgeMs >= 0.0 && result.frameAgeMs <= 250.0;
    const double captureAlignedSampleMs = hasMeasurementCaptureTs && captureAgeUsable
        ? sampleNow - result.frameAgeMs : sampleNow;
    if (frameIdentityAuthoritative && hasMeasurementCaptureTs && captureAgeUsable) {
        // Map the canonical measurement clock, not raw source publication or
        // callback arrival. Registration, sampler, and post-hoc labels now share it.
        const double off = sampleNow - result.measurementCaptureTsMs
            - result.frameAgeMs;
        if (epochBridgeN_ < static_cast<int>(epochBridgeInit_.size())) {
            epochBridgeInit_[static_cast<std::size_t>(epochBridgeN_)] = off;
            ++epochBridgeN_;
            if (epochBridgeN_ == static_cast<int>(epochBridgeInit_.size())) {
                auto tmp = epochBridgeInit_;
                std::sort(tmp.begin(), tmp.end());
                epochToEngineOffsetMs_ = tmp[tmp.size() / 2];
            }
        } else {
            epochToEngineOffsetMs_ = epochToEngineOffsetMs_ * 0.95 + off * 0.05;
        }
    }
    // [ORION_FUSED_FIRE] Post-hoc full-shot tip label (once per completed shot): teach the
    // per-bucket appear->tip anchor clock. label = (epoch tip -> engine clock) - the ended
    // shot's validated appear time. EMA alpha=0.15, sanity-clamped; session-scoped (the
    // learning.json v4 persistence lands with the measured-lead flip).
    if (result.posthocN > 0 && result.posthocN != lastPosthocNConsumed_
        && std::isfinite(result.posthocTipMs) && result.posthocTipMs > 0.0) {
        lastPosthocNConsumed_ = result.posthocN;
        if (!autonomousLiveMeterTimingEnabled()
            && epochBridgeN_ >= static_cast<int>(epochBridgeInit_.size())
            && endedShotAppearMs_ >= 0.0 && !endedShotBucketKey_.isEmpty()) {
            const double tipEngineMs = result.posthocTipMs + epochToEngineOffsetMs_;
            const double label = tipEngineMs - endedShotAppearMs_;
            if (label > 150.0 && label < 3000.0) {
                const double prev = config_.shotTypeAppearToTipMs.value(
                    endedShotBucketKey_, config_.fusedAppearToTipSeedMs);
                const double next = prev + 0.15 * (label - prev);
                config_.shotTypeAppearToTipMs.insert(endedShotBucketKey_, next);
                emit fusedLearningUpdated(config_.shotTypeAppearToTipMs, config_.leadRebaselined);
                emit fusedDiagnostic(QStringLiteral(
                    "FusedAnchorLearn: bucket=%1 label=%2ms ema=%3ms (posthoc n=%4 conf=%5)")
                        .arg(endedShotBucketKey_)
                        .arg(label, 0, 'f', 0)
                        .arg(next, 0, 'f', 0)
                        .arg(result.posthocN)
                        .arg(result.posthocConf, 0, 'f', 2));
            }
        }
        endedShotAppearMs_ = -1.0;   // one label per ended shot
    }
    // Anti-starvation guard: a sample is only usable if its numbers are finite and
    // in-range. A non-finite or out-of-band fill/confidence (NaN from a torn frame,
    // a negative or >100 fill) must be REJECTED, never written into shot_ — otherwise
    // garbage telemetry masquerades as a live reading and the release logic acts on it.
    const bool finiteSample = std::isfinite(result.fillPct)
        && std::isfinite(result.confidence)
        && std::isfinite(result.frameAgeMs);
    const bool inRangeSample = result.fillPct >= 0.0 && result.fillPct <= 100.0
        && result.confidence >= 0.0 && result.confidence <= 1.0;
    if (result.detected && (!finiteSample || !inRangeSample)) {
        clearPendingUnlessBoundedCurrentProof();
        invalidateVisionScheduleForDropout(sampleNow);
        shot_.detectionPresence = QStringLiteral("rejected");
        shot_.lastSampleFreshAccept = false;
        shot_.lastSampleGenuineAccept = false;
        if (censusActive) {
            shot_.detNotDetected += 1;  // garbage sample = unusable, bucket with not-detected
        }
        return;
    }

    if (duplicateDetectedFrame) {
        // Do not refresh receipt time, fill, confidence, tracker state, or any timing
        // estimator. Most importantly, invalidate THIS-payload release authority so
        // an already-armed vision deadline is cancelled on the next process tick.
        invalidateVisionScheduleForDropout(sampleNow);
        if (!result.detected) {
            // A frozen capture can legitimately emit meter_present=false while its
            // decoder identity cannot advance. RemotePlaySession intentionally lets
            // that health-negative bypass frame_count dedupe. Apply only the
            // fail-closed absence state; do not advance identity, lastDetectionMs,
            // freshness, or any timing estimator from the repeated image.
            shot_.meterDetected = false;
            shot_.fillPct = 0.0;
            shot_.confidence = 0.0;
            shot_.velocityPctS = 0.0;
            shot_.accelerationPctS2 = 0.0;
            shot_.etaToGreenMs = -1.0;
        }
        shot_.detectionPresence = QStringLiteral("duplicate");
        shot_.lastSampleFreshAccept = false;
        shot_.lastSampleGenuineAccept = false;
        if (censusActive) {
            shot_.detDuplicateFrameDrop += 1;
        }
        return;
    }
    if (outOfOrderDetectedFrame) {
        clearPendingUnlessBoundedCurrentProof();
        // A delayed callback from an older decoder frame is even less
        // authoritative than an exact replay. Never let it rewind fill, refresh
        // the meter clock, seed velocity, or preserve an imminent release.
        invalidateVisionScheduleForDropout(sampleNow);
        shot_.detectionPresence = QStringLiteral("stale");
        shot_.lastSampleFreshAccept = false;
        shot_.lastSampleGenuineAccept = false;
        if (censusActive) {
            shot_.detStaleFrameDrop += 1;
        }
        if (shot_.releaseReason.isEmpty()
            || shot_.releaseReason.startsWith(QStringLiteral("Tracking"))) {
            shot_.releaseReason = QStringLiteral("out_of_order_frame");
        }
        return;
    }
    if (hasCaptureTs || result.frameNumber >= 0) {
        lastDetectorFrameNumber_ = result.frameNumber;
        lastDetectorCaptureTsMs_ = hasCaptureTs ? result.captureTsMs : -1.0;
    }

    // A frame flagged a true duplicate by the source, or one far older than the
    // usable bound, can't seed a new velocity sample — but it must NOT zero
    // meterDetected or discard the confirmed green window / last-known fill.
    // (The old hard `frameAgeMs > 180` reject did exactly that, so a single age
    //  spike on a static HUD knocked meterFresh false and every shot fell through
    //  to the max-hold safety path → released at 100%.) Let freshness lapse
    //  naturally through lastDetectionMs instead of force-tearing the lock here.
    if (result.staleFrame || result.ghostFrame
        || result.frameAgeMs > config_.staleFrameMaxMs) {
        clearPendingUnlessBoundedCurrentProof();
        invalidateVisionScheduleForDropout(sampleNow);
        if (censusActive) {
            shot_.detStaleFrameDrop += 1;
        }
        shot_.lastSampleFreshAccept = false;
        shot_.lastSampleGenuineAccept = false;
        shot_.frameAgeMs = result.frameAgeMs;
        shot_.detectionPresence = QStringLiteral("stale");
        if (shot_.releaseReason.isEmpty() || shot_.releaseReason.startsWith(QStringLiteral("Tracking"))) {
            shot_.releaseReason = QStringLiteral("stale_frame");
        }
        return;
    }

    // Track the meter's peak this shot (for the post-release recede/LATE test) and, while a
    // post-release capture is open, buffer the settled-meter sample. Done BEFORE the
    // shot-active split so it also captures the post-release (idle_overlay) frames that carry
    // the frozen release-vs-green we calibrate from.
    if (result.detected && std::isfinite(result.fillPct)
        && result.fillPct >= 0.0 && result.fillPct <= 100.0) {
        maxFillThisShot_ = std::max(maxFillThisShot_, result.fillPct);
    }

    // [ORION_COURT_POSITION] Frame-normalize this detection's bbox center and keep it as the
    // engine's live court-position proxy (see the member block in AutomationEngine.h for why the
    // meter's position is a court-position signal at all). Pure telemetry: no branch below reads
    // these, so nothing here can move a deadline, a gate, or the fire path.
    //
    // bbox_wh is used as the divisor rather than the independently-advancing capture_width/height
    // telemetry, for the same reason OrionTypes.h gives for carrying it: it belongs to the same
    // immutable detector snapshot as the bbox, so a source/resolution handoff cannot pair a box
    // with the wrong frame size and manufacture an off-court position.
    const bool meterNormUsable = result.detected && result.width > 0 && result.height > 0
        && result.bboxFrameWidth > 0 && result.bboxFrameHeight > 0;
    double meterNormX = -1.0;
    double meterNormY = -1.0;
    if (meterNormUsable) {
        meterNormX = (static_cast<double>(result.x) + static_cast<double>(result.width) * 0.5)
            / static_cast<double>(result.bboxFrameWidth);
        meterNormY = (static_cast<double>(result.y) + static_cast<double>(result.height) * 0.5)
            / static_cast<double>(result.bboxFrameHeight);
        if (meterLockPrevNormX_ >= 0.0 && meterLockPrevNormY_ >= 0.0) {
            const double jump = std::hypot(meterNormX - meterLockPrevNormX_,
                                           meterNormY - meterLockPrevNormY_);
            meterLockMaxJumpNorm_ = std::max(meterLockMaxJumpNorm_, jump);
        }
        meterLockPrevNormX_ = meterNormX;
        meterLockPrevNormY_ = meterNormY;
        lastMeterNormX_ = meterNormX;
        lastMeterNormY_ = meterNormY;
    } else {
        // Lock run broken. Drop the predecessor and the run's max so the next jump is measured
        // within one unbroken lock; lastMeterNorm* deliberately RETAIN the last known position so
        // an abort raised during a detector blink still carries where the meter last was.
        meterLockPrevNormX_ = -1.0;
        meterLockPrevNormY_ = -1.0;
        meterLockMaxJumpNorm_ = -1.0;
    }

    if (meterCapActive_) {
        MeterCalSample s;
        s.tMs = sampleNow;
        s.fillPct = result.fillPct;
        s.greenStartPct = result.greenStartPct;
        s.greenEndPct = result.greenEndPct;
        s.greenCenterPct = result.greenCenterPct;
        s.greenConfidence = result.greenConfidence;   // [ORION_GREEN_TRUTH] 0.0 == scan failed
        s.confidence = result.confidence;
        s.accepted = result.detected
            && (result.rejectionReason.isEmpty()
                || result.rejectionReason == QStringLiteral("green_not_found"));
        s.bx = static_cast<double>(result.x) + static_cast<double>(result.width) * 0.5;
        s.by = static_cast<double>(result.y) + static_cast<double>(result.height) * 0.5;
        // [ORION_COURT_POSITION] same center, frame-normalized (-1 when bbox_wh was absent).
        s.nx = meterNormX;
        s.ny = meterNormY;
        if (s.accepted && std::isfinite(s.fillPct)) {
            meterCapPeakFillPct_ = std::max(meterCapPeakFillPct_, s.fillPct);
        }
        // [ORION_TIP_PHASE] END-OF-RISE detector, on the CAPTURE clock (the clock the anchor is
        // on; s.tMs is engine-arrival and would fold this frame's decode/IPC age into the learned
        // constant). The stop is the last sample that pushed the running max up by more than
        // tipPhaseStopStepPct; tipPhaseStopQuietMs without further growth CONFIRMS it, and once
        // confirmed it is frozen. Freezing mirrors the offline estimator's break exactly: a
        // handful of rises stall and then tick again, and treating that second stage as the same
        // stop is what drove the offline sd from 15 ms to 115 ms.
        if (config_.tipPhaseEnabled && s.accepted && std::isfinite(s.fillPct)
            && std::isfinite(captureAlignedSampleMs) && !meterCapPhaseStopConfirmed_) {
            if (config_.stopReopenCorroborate) {
                // [ORION_STOP_CORROBORATE] MEASURED 2026-08-06 (counted batch, n=86): all three
                // apparent long-duration outliers (446.2 / 427.7 / 409.4 ms) were a SINGLE frame
                // reading >2pp above an already-settled plateau, 257-292ms after the meter had
                // visibly frozen. The rule below re-dated the stop onto that frame, and because
                // the spike then owned runMax nothing could undo it. All three were accepted into
                // the phase learner, inflating the learned animation and walking the aim LATE.
                //
                // A real second-stage rise (the case the freeze exists for) spans MULTIPLE frames;
                // a spike is one frame and the next sample falls back. So hold the re-open and
                // commit only when a second accepted sample also clears the PRIOR run max, dating
                // the stop at the first of the pair exactly as the uncorroborated rule would have.
                // Purely an observation/learning correction — this block cannot fire or schedule.
                if (s.fillPct > meterCapPhaseRunMaxPct_ + config_.tipPhaseStopStepPct) {
                    if (meterCapPhaseReopenPendingMs_ >= 0.0
                        && s.fillPct > meterCapPhaseReopenPriorMaxPct_) {
                        meterCapPhaseRunMaxPct_ = std::max(
                            s.fillPct, meterCapPhaseReopenPendingFillPct_);
                        meterCapPhaseStopMs_ = meterCapPhaseReopenPendingMs_;
                        meterCapPhaseReopenPendingMs_ = -1.0;
                    } else {
                        // Candidate only: deliberately do NOT advance the run max, so a spike
                        // cannot raise the bar and mask the genuine plateau behind it.
                        meterCapPhaseReopenPendingMs_ = captureAlignedSampleMs;
                        meterCapPhaseReopenPendingFillPct_ = s.fillPct;
                        meterCapPhaseReopenPriorMaxPct_ = meterCapPhaseRunMaxPct_;
                    }
                } else if (s.fillPct > meterCapPhaseRunMaxPct_) {
                    // Ordinary sub-step growth: the plateau is still creeping, not restarting.
                    meterCapPhaseRunMaxPct_ = s.fillPct;
                    meterCapPhaseReopenPendingMs_ = -1.0;
                } else {
                    // Fell back to (or below) the plateau — the candidate was noise.
                    meterCapPhaseReopenPendingMs_ = -1.0;
                }
            } else if (s.fillPct > meterCapPhaseRunMaxPct_ + config_.tipPhaseStopStepPct) {
                meterCapPhaseRunMaxPct_ = s.fillPct;
                meterCapPhaseStopMs_ = captureAlignedSampleMs;
            } else if (s.fillPct > meterCapPhaseRunMaxPct_) {
                meterCapPhaseRunMaxPct_ = s.fillPct;
            }
            if (meterCapPhaseStopMs_ >= 0.0
                && captureAlignedSampleMs - meterCapPhaseStopMs_
                       >= config_.tipPhaseStopQuietMs) {
                meterCapPhaseStopConfirmed_ = true;
            }
        }
        // [ORION_STOP_SUBFRAME] Buffer the capture-aligned (t, fill) pair for the sub-frame stop
        // refinement. Gated on the flag so the OFF path allocates and touches nothing. Appended
        // even after the stop confirms: the refinement needs the settled plateau on both sides of
        // the committed snapped stop, and with stopReopenCorroborate a later committed re-open
        // moves that stop, so the buffer must cover the whole capture window.
        if (config_.stopDatingSubframe && config_.tipPhaseEnabled && s.accepted
            && std::isfinite(s.fillPct) && std::isfinite(captureAlignedSampleMs)) {
            meterCapPhaseCapSamples_.append(qMakePair(captureAlignedSampleMs, s.fillPct));
            if (meterCapPhaseCapSamples_.size() > 240) {
                meterCapPhaseCapSamples_.removeFirst();
            }
        }
        meterCapSamples_.append(s);
        if (meterCapSamples_.size() > 240) {
            meterCapSamples_.removeFirst();
        }
    }

    // Detection while NO shot is owned is OVERLAY/DEBUG ONLY — it must not seed the
    // timing predictor or the meter-presence clock. Otherwise an idle track-window
    // (or a meter_memory echo that slipped through) could prime velocity/green
    // state or look "fresh" the instant a shot arms, biasing the release. We still
    // record the display fields so the overlay/UI can show it, but the
    // sampler/greenTracker/meterDetected timing state is fed ONLY while a shot is
    // actively Armed or Holding, so timing always starts clean at the real shot.
    // A real short shot can finish before the normal ownership debounce. Any
    // supported physical shot input can be promoted only after this exact edge's
    // strict rising proof. Stick cold-start candidates must additionally satisfy
    // their existing intent dwell, preventing a dribble/flick from being seized.
    ShotMode pendingMode = ShotMode::ButtonShot;
    double pendingGestureStartMs = -1.0;
    quint64 pendingPhysicalEpoch = 0;
    QString pendingShotType;
    bool pendingInputQualified = false;
    const bool pendingCandidate = pendingMeterOwnershipCandidate(
        sampleNow, pendingMode, pendingGestureStartMs, pendingPhysicalEpoch,
        pendingShotType, pendingInputQualified);
    const bool liveOwnershipAllowed = !autonomousLiveMeterTimingEnabled()
        || measuredLeadAuthoritative(sampleNow) || latencyCalibrationMode_;
    // Input routing can be revoked independently of detector delivery.  A proof
    // callback that races a GUI disable or Defense-mode disarm must remain
    // overlay-only; ownership may be promoted only while both gates are live.
    if (config_.enabled && armed_.load(std::memory_order_acquire)
        && shot_.state == HoldState::Idle && pendingCandidate && liveOwnershipAllowed
        && recordPendingMeterOwnershipSample(
            result, sampleNow, pendingMode, pendingGestureStartMs,
            pendingPhysicalEpoch, pendingInputQualified)) {
        if (pendingMode == ShotMode::ButtonShot || pendingMode == ShotMode::TempoSquare) {
            // The output representation is latched on physical DOWN. In particular, an
            // automatic cold-start begins as physical Button/Square even if its measured
            // lead becomes ready between proof frames; switching mid-hold would leak UP.
            squareLatchedUntilRelease_ = true;
        } else if (pendingMode == ShotMode::GoToStick) {
            stickUpLatchedUntilNeutral_ = true;
        } else {
            stickDownLatchedUntilNeutral_ = true;
        }
        beginShot(pendingMode, sampleNow, pendingShotType, pendingGestureStartMs);
        seedPromotedMeterOwnershipEpisode();
        priorSampleGenuine = true;
        if (pendingMode == ShotMode::ButtonShot || pendingMode == ShotMode::TempoSquare) {
            retiredPhysicalShotEpoch_ = std::max(
                retiredPhysicalShotEpoch_, pendingPhysicalEpoch);
            squareHoldStartMs_ = -1.0;
            pendingSquarePhysicalEpoch_ = 0;
            pendingSquareArmGeneration_ = 0;
            pendingSquareTempoRemap_ = false;
            clearPendingSquareMovementContext();
        } else {
            retiredPhysicalShotEpoch_ = std::max(
                retiredPhysicalShotEpoch_, pendingPhysicalEpoch);
            stickUpHoldStartMs_ = -1.0;
            stickDownHoldStartMs_ = -1.0;
            clearPendingStickCalibration();
            clearPendingTempoStickMovementContext();
        }
        emit engineDiagnostic(QStringLiteral(
            "Evidence-backed ownership: mode=%1 press_age=%2ms first_fill=%3 current_fill=%4 epoch=%5")
                .arg(static_cast<int>(pendingMode))
                .arg(sampleNow - pendingGestureStartMs, 0, 'f', 1)
                .arg(pendingMeterOwnership_.first.fillPct, 0, 'f', 1)
                .arg(result.fillPct, 0, 'f', 1)
                .arg(pendingPhysicalEpoch));
        clearPendingMeterOwnershipEpisode();
        clearPendingMeterOwnershipEvidence();
        clearPendingStickCalibration();
        clearPendingTempoStickMovementContext();
    }

    const bool shotActive = (shot_.state == HoldState::Armed || shot_.state == HoldState::Holding);
    if (!shotActive) {
        shot_.detectionPresence = QStringLiteral("idle_overlay");
        shot_.detectorSource = result.detectorSource;
        shot_.fillPct = result.fillPct;
        shot_.confidence = result.confidence;
        shot_.greenStartPct = result.greenStartPct;
        shot_.greenEndPct = result.greenEndPct;
        shot_.greenCenterPct = result.greenCenterPct;
        shot_.frameAgeMs = result.frameAgeMs;
        // B2b (2026-07-25): the overlay's RISE readout is fed from shot_.velocityPctS /
        // accelerationPctS2, which this early return never wrote — so RISE read a hard 0 on every
        // idle frame (i.e. whenever no shot is owned, which is most of the time) even though the
        // detector reports both. These are DISPLAY-ONLY copies of the detector's own numbers, in
        // the same spirit as the fill/confidence/green copies above. Deliberately NOT fed to
        // sampler_/greenTracker_/meterDetected: seeding the timing predictor from idle frames is
        // exactly the priming bias the block comment above guards against, so release timing stays
        // byte-identical.
        shot_.velocityPctS = result.velocityPctS;
        shot_.accelerationPctS2 = result.accelerationPctS2;
        return;  // do NOT touch meterDetected/lastDetectionMs/sampler_/greenTracker_
    }

    shot_.detectionPresence = result.detected ? QStringLiteral("accepted")
                                              : QStringLiteral("rejected");
    shot_.detectorSource = result.detectorSource;
    shot_.fillPct = result.fillPct;
    shot_.confidence = result.confidence;
    shot_.velocityPctS = result.velocityPctS;
    shot_.accelerationPctS2 = result.accelerationPctS2;
    shot_.frameAgeMs = result.frameAgeMs;
    shot_.greenStartPct = result.greenStartPct;
    shot_.greenEndPct = result.greenEndPct;
    shot_.greenCenterPct = result.greenCenterPct;
    shot_.etaToGreenMs = result.etaToGreenMs;
    // [ORION_REG_FUSION] Registration far-horizon time-to-TIP estimate + confidence (per-frame).
    shot_.regTipMs = result.regTipMs;
    shot_.regConf = result.regConf;
    shot_.regSeq = result.regSeq;
    shot_.regSigmaMs = result.regSigmaMs;
    shot_.regModelId = result.regModelId;
    shot_.regModelVersion = result.regModelVersion;
    shot_.regRmsePp = result.regRmsePp;
    shot_.regN = result.regN;
    const bool regClockDomainCoherent = hasMeasurementCaptureTs
        && std::isfinite(result.regSampleCaptureMs)
        && result.regSampleCaptureMs > 0.0
        && std::abs(result.regSampleCaptureMs - result.measurementCaptureTsMs) <= 2.0;
    shot_.regTipAbsMs = result.regTipMs > 0.0 && result.regSigmaMs > 0.0
        && regClockDomainCoherent
        ? captureAlignedSampleMs + result.regTipMs
        : -1.0;
    shot_.consecutiveFrames = result.consecutiveFrames;
    // Item 6: Meter-memory fill extrapolation. When the sample is a stale_or_memory
    // echo (occluded meter), extrapolate the held fill using the current velocity
    // instead of freezing it. The meter keeps rising during occlusion, so a frozen
    // fill under-estimates the true position when the meter reappears or when the
    // feedforward clock fires during occlusion.
    if (config_.memoryExtrapolationEnabled
            && result.detected
            && result.rejectionReason == QStringLiteral("stale_or_memory")
            && shot_.lastFreshAcceptMs >= 0.0) {
        const double occlusionMs = sampleNow - shot_.lastFreshAcceptMs;
        if (occlusionMs > 0.0 && occlusionMs < 500.0) {
            const double v = sampler_.velocityPctPerMs();
            if (v > 0.001 && v < 1.0) {
                const double extrapolated = shot_.fillPct + v * occlusionMs;
                shot_.fillPct = std::min(extrapolated, config_.memoryExtrapolationClampPct);
            }
        }
    }
    if (result.fillPct > shot_.peakFillPct) {
        shot_.peakFillPct = result.fillPct;
        shot_.peakFillMs = sampleNow;  // mark when the meter last made forward progress
    }
    shot_.meterDetected = result.detected;
    shot_.lastDetectionMs = sampleNow;

    // A GENUINE fresh accept is a clean raw detection this frame — NOT a held/extrapolated
    // stale_or_memory sample (the sidecar marks those when raw detection was
    // meter_memory/roi_not_found). Only genuine accepts may build the timing predictor,
    // confirm a green window, advance the Go-To rise trajectory, or advance the freshness
    // ANCHOR (lastFreshAcceptMs); a stale sample updates the display fill (above) but earns
    // no freshness trust by default. This keeps a held 100 (or an extrapolated rise) from
    // satisfying the Go-To release gate, and makes greenTracker_.confirmed() shot-local +
    // fresh (it is also reset by beginShot).
    const QString readerStage = result.stage.trimmed().toLower();
    const bool acceptedRawReason = result.rejectionReason.isEmpty()
        || result.rejectionReason == QStringLiteral("green_not_found");
    const bool stageIsCoasted = readerStage == QLatin1String("coast")
        || readerStage == QLatin1String("no_meter")
        || readerStage == QLatin1String("stale");
    const bool strictShotStructureRequired = autonomousLiveMeterTimingEnabled();
    const bool currentGameplayStructureProof = result.gameplayStructureVerified
        && shot_.physicalShotEpoch != 0 && shot_.physicalShotEpoch == physicalShotEpoch_
        && result.gameplayStructureEpoch == shot_.physicalShotEpoch;
    // Initial ownership already needs three structure-proven frames. Keep that provenance
    // current after ownership too: a discontinuous re-lock may publish a monotonic red
    // lookalike for display, but it cannot replace the bot's samples or move/re-arm a release.
    const bool genuineFresh = result.detected && acceptedRawReason && !stageIsCoasted
        && result.confidence >= config_.confidenceGate
        && (!strictShotStructureRequired
            || (currentGameplayStructureProof && hasCaptureTs
                && hasMeasurementCaptureTs));
    if (genuineFresh && currentGameplayStructureProof) {
        shot_.gameplayStructureVerified = true;
        shot_.gameplayStructureEpoch = result.gameplayStructureEpoch;
    }

    // A detector re-lock is a NEW trajectory episode. Never join its first fill to
    // the pre-occlusion samples: if the replacement lock lands higher/lower or on a
    // different candidate, the synthetic jump creates an enormous velocity and can
    // pull a scheduled release forward. Explicit `acquire` is authoritative; for
    // older sidecars without stage telemetry, a >50ms genuine-sample gap is the
    // conservative fallback (a one-frame ~40ms blink keeps its green-confirm streak).
    const double genuineGapMs = shot_.lastFreshAcceptMs >= 0.0
        ? sampleNow - shot_.lastFreshAcceptMs : 0.0;
    const bool relockDiscontinuity = genuineFresh && shot_.sawFreshMeterThisShot
        && ((!priorSampleGenuine && readerStage == QLatin1String("acquire"))
            // A silent decoder/sidecar gap has no intervening `coast` payload to
            // clear priorSampleGenuine. The elapsed gap itself must therefore be
            // sufficient to start a new trajectory episode.
            || genuineGapMs > 50.0);
    if (relockDiscontinuity) {
        invalidateUnconfirmedVisionSchedule("detect_stale_or_ghost");
        sampler_.reset();
        greenTracker_.reset();
        fused_.reset();
        lastFusedSampleMs_ = -1.0;
        lastFusedFillPct_ = -1.0;
        fusedFireAtMs_ = -1.0;
        fusedPeakLatched_ = false;
        fusedFrameAgeEmaMs_ = -1.0;
        if (!autonomousLiveMeterTimingEnabled()) {
            templateArrival_.beginShot(shot_.bucketKey, shot_.holdStartMs);
        }
        shot_.anchorCandFirstMs = -1.0;
        shot_.anchorCandFirstFill = 0.0;
        shot_.anchorCandFirstFrameAgeMs = 0.0;
        shot_.anchorCandLastMs = -1.0;
        shot_.anchorCandLastFill = 0.0;
        shot_.anchorValidMs = -1.0;
        shot_.greenConfirmFillPct = -1.0;
        shot_.greenConfirmMs = -1.0;
        shot_.freshAcceptRingMs.fill(-1.0);
        shot_.freshAcceptRingPos = 0;
        shot_.minFreshFillPct = 1000.0;
        shot_.peakFillPct = 0.0;
        shot_.peakFillMs = sampleNow;
        // [ORION_TIP_PHASE] A re-lock is a NEW trajectory episode (that is the whole reason this
        // block exists), so the previous episode's anchor no longer dates THIS animation. Keeping
        // it would be the worst available failure: a stale anchor plus a valid-looking constant
        // yields a confident absolute tip for a shot the detector has only just re-acquired.
        // Clearing the prev-sample pair as well stops the first post-relock sample from pairing
        // with the last pre-relock one to synthesise a crossing across the discontinuity.
        shot_.fillPhaseAnchorMs = -1.0;
        shot_.fillPhaseAnchorLevelPct = -1.0;
        shot_.phasePrevFillPct = -1.0;
        shot_.phasePrevCaptureMs = -1.0;
        ++shot_.visionEpoch;
        ++shot_.detRelockResets;
    }

    // EXPERIMENT (default OFF, settings memory_trust_enabled): a detector meter_memory echo
    // is a HELD copy of the last accepted read. Within ~1 frame of a genuine accept it is
    // still valid, so a single-frame wide-zone detection blink need not drop the engine to
    // stale. Promote such an echo to fresh-equivalent for lastSampleFreshAccept ONLY — it
    // does NOT feed the velocity sampler / green tracker / fresh-accept count below (a held
    // fill would inject a false zero-velocity sample). The age bound is measured from the
    // last GENUINE accept (lastFreshAcceptMs, which echoes never advance), so a real
    // multi-frame dropout still lapses to stale; the held fill must also stay within the
    // observed rise band. No detector threshold is touched (memory only fires after a real
    // lock) => no false-accept exposure. Target = the recoverable wide-zone meter_memory
    // misses in tools/diagnostics/replay_detector.py --rise-report.
    bool memoryTrusted = false;
    if (!genuineFresh
            && config_.memoryTrustEnabled
            && result.detected
            && result.rejectionReason == QStringLiteral("stale_or_memory")
            && shot_.lastFreshAcceptMs >= 0.0
            && (sampleNow - shot_.lastFreshAcceptMs) <= config_.memoryTrustMaxAgeMs
            && std::isfinite(result.fillPct)
            && result.fillPct >= shot_.minFreshFillPct - config_.memoryTrustFillSlackPct
            // TIP-GUARD: never trust a held echo at/above the near-green band — that's the
            // mistimed-release risk. The rising body is safe; the tip requires a fresh read.
            && result.fillPct < config_.memoryTrustMaxFillPct) {
        memoryTrusted = true;
    }
    const bool freshAccept = genuineFresh || memoryTrusted;
    shot_.lastSampleFreshAccept = freshAccept;
    shot_.lastSampleGenuineAccept = genuineFresh;
    if (!genuineFresh) {
        invalidateVisionScheduleForDropout(sampleNow);
    }

    // Census classification (shot is Armed/Holding here — the idle path returned above).
    // Only GENUINE accepts advance the fresh-accept count + the freshness anchor; a trusted
    // echo is tallied separately (detMemoryTrusted) so the live A/B can see how often it fired.
    if (genuineFresh) {
        shot_.detFreshAccepts += 1;
        if (shot_.firstFreshAcceptMs < 0.0) {
            shot_.firstFreshAcceptMs = sampleNow;
        }
        shot_.lastFreshAcceptMs = sampleNow;
        shot_.pushFreshAccept(sampleNow);   // T4 tip-gate health ring (continuous-tracking gate)
    } else if (memoryTrusted) {
        shot_.detMemoryTrusted += 1;
    } else if (result.detected) {
        shot_.detStaleOrMemory += 1;
    } else if (result.rejectionReason == QStringLiteral("confidence_low")) {
        shot_.detConfLow += 1;
    } else {
        shot_.detNotDetected += 1;
    }

    // Only GENUINE accepts seed the velocity sampler / green tracker / rise-band trackers. A
    // memory-trusted echo deliberately does NOT (its held fill is unchanged => a false
    // zero-velocity sample; its green is a stale copy) — it only keeps lastSampleFreshAccept
    // true across the blink.
    if (genuineFresh && result.fillPct > 0.0) {
        if (lastGenuineCaptureSampleMs_ >= 0.0) {
            const double sourceGapMs = captureAlignedSampleMs - lastGenuineCaptureSampleMs_;
            if (std::isfinite(sourceGapMs) && sourceGapMs >= 1.0 && sourceGapMs <= 100.0) {
                genuineFrameGapEmaMs_ = genuineFrameGapEmaMs_ * 0.85 + sourceGapMs * 0.15;
                genuineFrameGapHighWaterMs_ = std::max(
                    sourceGapMs, genuineFrameGapHighWaterMs_ * 0.94);
            }
        }
        lastGenuineCaptureSampleMs_ = captureAlignedSampleMs;
        // Autonomous timing operates on the source/capture clock. Timestamping samples at callback
        // arrival made a 5-20 ms processing variation look like real meter acceleration and was a
        // direct source of inconsistent tip extrapolation.
        sampler_.addSample(result.fillPct, captureAlignedSampleMs);
        // [ORION_TIP_PHASE] Same sample, same capture clock: date the anchor crossing. Placed on
        // the sampler feed deliberately -- these are the samples the engine already trusts to
        // build a trajectory, so the phase anchor inherits every genuineFresh gate above rather
        // than inventing its own weaker one.
        notePhaseAnchorSample(result.fillPct, captureAlignedSampleMs);
        // [ORION_TEMPLATE_ARRIVAL] H4: feed the crossing-vector on the CAPTURE timeline
        // (sampleNow - frameAgeMs) so the template's time-at-fill crossings are anchored to
        // when the meter actually showed that fill, not when the sample arrived.
        if (config_.templateArrivalEnabled && !autonomousLiveMeterTimingEnabled()) {
            const double captureMs = sampleNow
                - std::clamp(result.frameAgeMs, 0.0, config_.captureAgeLeadCapMs);
            templateArrival_.addSample(result.fillPct, captureMs);
        }
        shot_.sawFreshMeterThisShot = true;
        // Track the lowest fresh fill this shot so the reactive path can require the meter
        // to have genuinely climbed INTO the target (not been at the top from frame one).
        shot_.minFreshFillPct = std::min(shot_.minFreshFillPct, result.fillPct);
        // === T2 anchor-candidate episode tracking (carryover rejection) ===
        // A lingering PREVIOUS-shot meter is a REAL raw detection (fed=1, ~100/descending,
        // panning with the camera), so genuine-vs-echo cannot reject it — only its
        // trajectory can. Segment the genuine-accept stream into episodes (restart on a
        // >180ms gap or a >8% fill drop) and VALIDATE the meter-appear anchor only when
        // the episode looks like THIS shot's rising meter:
        //   - first sight low-fill (<= anchorMaxFirstFillPct, 40 inclusive) AND the next
        //     genuine sample did not descend (>= firstFill - 1.0), OR
        //   - the fill genuinely rose >= anchorRiseMinPct across >= 2 genuine samples
        //     (the only route for an episode first seen high — a real meter never starts
        //     high, and a frozen/deflating carryover never rises).
        // anchorValidMs BACKDATES to the episode's first genuine sample so the 1-frame
        // confirmation adds no timing error (the meter clock fires 85-340ms later).
        if (shot_.anchorValidMs < 0.0) {
            const bool episodeActive = shot_.anchorCandFirstMs >= 0.0;
            // Break the episode on a real dropout+reacquire (>300ms genuine-sample gap) OR a fill
            // DROP (>8%) — the latter is the carryover->real transition (the spent ~100 meter is
            // preempted by a fresh low one). 300ms (not 180) keeps a sparsely-sampled real rise in
            // ONE episode; the fill-drop break, not the gap, is the true carryover discriminator.
            const bool episodeBroken = episodeActive
                && ((sampleNow - shot_.anchorCandLastMs) > 300.0
                    || result.fillPct < shot_.anchorCandLastFill - 8.0);
            if (!episodeActive || episodeBroken) {
                shot_.anchorCandFirstMs = sampleNow;
                shot_.anchorCandFirstFill = result.fillPct;
                shot_.anchorCandFirstFrameAgeMs = result.frameAgeMs;
                // B7 (2026-07-25) ANCHOR HARDENING. This branch used to validate the anchor on the
                // FIRST frame of a new episode whenever the fill was low. But a decor phantom IS a
                // genuine fresh accept — genuine-vs-echo cannot reject it, and a phantom is low-fill
                // almost by definition — so a single low-fill lock validated anchorValidMs on the
                // first frame of a new shot and handed the meter-appear CLOCK an anchor that never
                // belonged to a meter. The block's own documented contract (see the episode comment
                // above) was already "first sight at/below anchorMaxFirstFillPct WITH a non-descending
                // CONFIRM sample" — the code just never waited for the confirm sample. Both branches
                // now require >= 2 genuine samples, matching the rise branch. anchorValidMs still
                // BACKDATES to anchorCandFirstMs, so validating one sample later costs ZERO timing
                // accuracy (the clock fires 85-340ms after the anchor); it only means a one-frame
                // phantom that never returns can no longer anchor anything.
            } else {
                // Second+ genuine sample of this episode — the confirm frame.
                //  * episode started HIGH (> anchorMaxFirstFillPct): the only route to validity is a
                //    genuine rise across the episode (a real meter caught mid-rise); a frozen or
                //    deflating carryover never rises.
                //  * episode started LOW: confirmed when this sample did not DESCEND from the first
                //    (>= firstFill - 1.0). A real meter rises or holds; a phantom that vanishes never
                //    produces this frame at all.
                const bool roseEnough =
                    (result.fillPct - shot_.anchorCandFirstFill) >= config_.anchorRiseMinPct;
                const bool lowFirstConfirmed =
                    shot_.anchorCandFirstFill <= config_.anchorMaxFirstFillPct
                    && result.fillPct >= shot_.anchorCandFirstFill - 1.0;
                if (roseEnough || lowFirstConfirmed) {
                    shot_.anchorValidMs = shot_.anchorCandFirstMs;
                }
            }
            shot_.anchorCandLastMs = sampleNow;
            shot_.anchorCandLastFill = result.fillPct;
        }
    }
    if (genuineFresh && result.greenStartPct >= 0.0 && result.greenEndPct >= 0.0) {
        greenTracker_.update(result.greenStartPct, result.greenEndPct, result.confidence);
    }
}

void AutomationEngine::updateNetworkOffset(double offsetMs)
{
    // Sample-and-hold: the freshest measured offset is recorded continuously, but an
    // in-flight shot keeps the offset it latched at shot start — a mid-hold RTT spike
    // must never move an already-computed deadline. An idle engine mirrors the fresh
    // value so telemetry and the next shot start from it.
    pendingNetworkOffsetMs_ = std::clamp(offsetMs, -100.0, 100.0);
    const bool shotActive = shot_.state == HoldState::Armed
        || shot_.state == HoldState::Holding
        || shot_.state == HoldState::GreenWindow
        || shot_.state == HoldState::Releasing;
    if (!shotActive) {
        shot_.networkOffsetMs = pendingNetworkOffsetMs_;
    }
}

void AutomationEngine::updateNetworkQuality(double offsetMs, double jitterMs)
{
    if (std::isfinite(jitterMs) && jitterMs >= 0.0) {
        const double j = std::min(jitterMs, 200.0);
        networkJitterEmaMs_ = networkJitterEmaMs_ <= 0.0 ? j
                                                         : networkJitterEmaMs_ * 0.8 + j * 0.2;
        // Auto wifi mode: sustained jitter above the threshold = a lossy/wireless link.
        // Tighter per-shot RTT delta clamp + stricter vision freshness gate; no user toggle.
        wifiMode_ = networkJitterEmaMs_ >= config_.wifiJitterThresholdMs;
    }
    updateNetworkOffset(offsetMs);
}

uint16_t AutomationEngine::squarePassthroughBit() const noexcept
{
    const QString button = config_.squarePassthroughButton.trimmed().toLower();
    if (button == QLatin1String("r3")) return XINPUT_GAMEPAD_RIGHT_THUMB;
    if (button == QLatin1String("l3")) return XINPUT_GAMEPAD_LEFT_THUMB;
    return 0;   // "none" and every unrecognised value: inert, never a crash
}

void AutomationEngine::applySquarePassthrough(ControllerState& output,
                                              const ControllerState& physical) noexcept
{
    // Cleared every tick BEFORE the gates, so a stale true can never outlive the click that set it
    // and mark an ordinary shot's output as passthrough-injected.
    squarePassthroughInjected_ = false;
    // [ORION_SQUARE_PASSTHROUGH 2026-08-12] #88 -- see RemapConfig::squarePassthroughEnabled.
    //
    // Gated on tempoRemapEnabled because that is the ONLY condition under which Square is
    // unreachable. With Tempo off the physical button already passes through, and a second
    // binding would be a surprise (a stick click silently shooting) rather than a fix.
    if (!config_.squarePassthroughEnabled || !config_.tempoRemapEnabled) {
        return;
    }
    const uint16_t bit = squarePassthroughBit();
    if (bit == 0 || (physical.buttons & bit) == 0) {
        return;
    }
    output.buttons |= XINPUT_GAMEPAD_X;
    // Consume the click. It is now a dedicated Square button, so forwarding it as well would
    // double-input anything the game binds to that stick press.
    output.buttons = static_cast<uint16_t>(output.buttons & ~bit);
    squarePassthroughInjected_ = true;
}

ControllerState AutomationEngine::process(const ControllerState& physical)
{
    ControllerState output = processInternal(physical);
    applySquarePassthrough(output, physical);
    return output;
}

ControllerState AutomationEngine::processInternal(const ControllerState& physical)
{
    const double now = nowMs();
    lastPhysical_ = physical;
    // Output-drain and authority-boundary handling use the same three-poll
    // physical evidence as normal ownership. Update it before any early gate so
    // disabled/disarmed ticks cannot become invisible release samples.
    updateTrackingHistory(physical);
    updateShootingState();
    // Engine-scoped mirror of processIdle's three-sample Square release debounce. It is
    // updated before every early gate (disabled/disarmed ticks included) and, unlike
    // ShotContext::xButtonHistory, survives the `shot_ = ShotContext{}` resets in beginShot()
    // and abort(). A release the player really performed can therefore never be erased by a
    // shot-state transition that happened to land on the same tick.
    if (physical.square()) {
        physicalSquareUpPolls_ = 0;
        physicalSquareDownSeenSinceArm_ = true;
    } else if (physicalSquareUpPolls_ < kPhysicalReleaseSamples) {
        ++physicalSquareUpPolls_;
    }
    // A COMPLETE press->release cycle is required, not merely a release. An overlap press that
    // arrives after ownership began (Square pushed while TempoStick/Go-To already owns the
    // stick) must stay suppressed forever: the UP polls that preceded it are evidence about a
    // button that was not yet down, and must never be spent unlatching it.
    if (physicalSquareDownSeenSinceArm_
        && physicalSquareUpPolls_ >= kPhysicalReleaseSamples) {
        physicalSquareReleaseSeenSinceArm_ = true;
    }
    auto output = physical.clone();

    const quint64 armGeneration = armRevocationGeneration_.load(
        std::memory_order_acquire);
    if (armGeneration != handledArmRevocationGeneration_) {
        // Consume every cross-thread disarm on the engine thread, even when the
        // watchdog re-armed before this controller tick.  A pre-proof gesture is
        // fenced; an owned shot can never resume from stale state. A release that
        // already crossed the worker submit boundary still wins and completes.
        cancelPendingLatencyCalibrationOwnership();
        if (shot_.state == HoldState::Releasing || shot_.state == HoldState::Cooldown) {
            latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                  shot_.mode, shot_.shotType);
        } else if (shot_.state != HoldState::Idle) {
            abort(QStringLiteral("automation_disarmed_abort"));
            if (shot_.state == HoldState::Releasing) {
                latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                      shot_.mode, shot_.shotType);
            }
        }
        handledArmRevocationGeneration_ = armGeneration;
    }
    if (!config_.enabled || !armed_.load(std::memory_order_acquire)) {
        // Disabling the route while a physical edge is awaiting meter proof is
        // terminal for that edge. Pre-release ownership is aborted into a
        // mode-aware drain; a submitted release keeps its exact pulse/cooldown.
        cancelPendingLatencyCalibrationOwnership();
        clearPendingMeterOwnershipEpisode();
        clearPendingMeterOwnershipEvidence();
        clearPendingStickCalibration();
        pendingSquarePhysicalEpoch_ = 0;
        pendingSquareArmGeneration_ = 0;
        // A submitted release is latched exactly once by the authority-boundary
        // handler above (arm generation), or synchronously by applyConfig() for
        // a disabled route. Re-latching it on every disabled poll resurrects a
        // drain that already consumed three physical-end samples while the
        // release pulse/cooldown was still advancing, which then suppresses the
        // first input after route recovery.
        if (shot_.state != HoldState::Releasing
            && shot_.state != HoldState::Cooldown
            && shot_.state != HoldState::Idle) {
            abort(QStringLiteral("automation_disarmed_abort"));
            if (shot_.state == HoldState::Releasing) {
                latchOwnedOutputDrain(OwnedOutputDrain::ReleasedUntilPhysicalEnd,
                                      shot_.mode, shot_.shotType);
            }
        }
        auto observeDrainWhileReleaseStateAdvances = [this, &physical, now]() {
            if (ownedOutputDrain_ == OwnedOutputDrain::None) {
                return;
            }
            // Releasing/Cooldown owns the actual packet shape, but the
            // independent drain must still consume physical UP/neutral polls in
            // parallel. Otherwise three recovery-neutral polls are discarded and
            // the first post-reconnect gesture is suppressed by a stale drain.
            const QString plan = shot_.releasePlan;
            const QString reason = shot_.releaseReason;
            ControllerState ignored = physical.clone();
            (void)applyOwnedOutputDrain(ignored, physical, now);
            shot_.releasePlan = plan;
            shot_.releaseReason = reason;
        };
        if (shot_.state == HoldState::Releasing) {
            observeDrainWhileReleaseStateAdvances();
            processReleasing(output, now);
            return output;
        }
        if (shot_.state == HoldState::Cooldown) {
            observeDrainWhileReleaseStateAdvances();
            processCooldown(output, now);
            return output;
        }
        if (applyOwnedOutputDrain(output, physical, now)) {
            return output;
        }
        observePhysicalEndWhileDisarmed(physical);
        return physical;
    }

    // Freshness can expire even when telemetry stops completely. Polling this
    // truth-only status from the controller tick exposes that transition and
    // auto-completes an armed calibration run as soon as authority converges.
    refreshLatencyCalibrationStatus();

    // Close out a post-release meter capture. Grade as soon as the meter has SETTLED (the player
    // landed / the bounce is over), otherwise keep capturing — up to the hard cap — so a moving
    // fade/Go-To meter isn't graded mid-flight (the false-EXCELLENT bug). Driven here so it fires
    // even if detection stops (meter vanished). Standstill settles immediately -> grades promptly.
    if (meterCapActive_ && now >= meterCapDeadlineMs_) {
        if (now >= meterCapHardDeadlineMs_ || meterCapHasSettledRun()) {
            evaluatePostReleaseMeter();
        } else {
            meterCapDeadlineMs_ = now + config_.meterSettlePollMs;
        }
    }

    // If an already-owned shot suppresses a second physical shot control, that
    // overlap is not a fresh gesture after cooldown. Latch it now, before the
    // mode-specific output shaping erases the physical evidence.
    latchSuppressedShotControlOverlap(physical);

    if (shot_.state == HoldState::Idle
        && applyOwnedOutputDrain(output, physical, now)) {
        prevSquare_ = physical.square();
        prevStickActive_ = stickShotActive(physical);
        return output;
    }

    switch (shot_.state) {
    case HoldState::Idle:
        processIdle(output, physical, now);
        break;
    case HoldState::Armed:
        processArmed(output, now);
        break;
    case HoldState::Holding:
    case HoldState::GreenWindow:
        processHolding(output, now);
        break;
    case HoldState::Releasing:
        processReleasing(output, now);
        break;
    case HoldState::PumpFake:
        processPumpFake(output, now);
        break;
    case HoldState::Cooldown:
        processCooldown(output, now);
        break;
    }

    // B2c/C5: make the IDLE arm gate greppable. processIdle re-stamps its reason every tick, so
    // only TRANSITIONS are emitted — a "why did the bot stop arming?" report becomes a short event
    // trail (e.g. waiting_for_button_release latched with the button already up = the C5 leak).
    // Diagnostic only: nothing here touches state or timing.
    if (shot_.state == HoldState::Idle && shot_.releaseReason != lastIdleReasonLogged_) {
        lastIdleReasonLogged_ = shot_.releaseReason;
        emit engineDiagnostic(QStringLiteral("IDLE-GATE: reason=%1 sqLatch=%2 rsUpLatch=%3 "
                                             "rsDownLatch=%4 sqPhys=%5 xHist=%6")
                                  .arg(shot_.releaseReason.isEmpty()
                                           ? QStringLiteral("-") : shot_.releaseReason)
                                  .arg(squareLatchedUntilRelease_ ? 1 : 0)
                                  .arg(stickUpLatchedUntilNeutral_ ? 1 : 0)
                                  .arg(stickDownLatchedUntilNeutral_ ? 1 : 0)
                                  .arg(physical.square() ? 1 : 0)
                                  .arg(static_cast<int>(shot_.xButtonHistory.size())));
    }

    // Fail-closed detector aborts return timing ownership to the player, but the
    // Tempo toggle still owns the physical representation of Square.
    if (shot_.state == HoldState::Idle && squareRearmBlockedUntilRelease_
        && pendingSquareTempoRemap_
        && physical.square()) {
        forceTempoSquareGather(output);
    }

    prevSquare_ = physical.square();
    prevStickActive_ = stickShotActive(physical);
    return output;
}

void AutomationEngine::reset()
{
    tipReservation_ = AutonomousTipReservation{};
    latencyCalibrationMode_ = false;
    latencyCalibrationAutomatic_ = false;
    beginSidecarProcessGeneration();
    const double offset = shot_.networkOffsetMs;
    shot_ = ShotContext{};
    shot_.networkOffsetMs = offset;
    pose_ = PoseTimingState{};
    sampler_.reset();
    greenTracker_.reset();
    prevSquare_ = false;
    prevStickActive_ = false;
    squareHoldStartMs_ = -1.0;
    physicalShotEpoch_ = 0;
    clearPendingMeterOwnershipEpisode();
    clearPendingMeterOwnershipEvidence();
    clearPendingStickCalibration();
    pendingSquarePhysicalEpoch_ = 0;
    pendingSquareArmGeneration_ = 0;
    pendingSquareTempoRemap_ = false;
    clearPendingSquareMovementContext();
    clearPendingTempoStickMovementContext();
    resetTempoMovementTransaction();
    tempoPassThroughPulseActive_ = false;
    tempoPassThroughPulseEndMs_ = -1.0;
    stickDownHoldStartMs_ = -1.0;
    stickUpHoldStartMs_ = -1.0;
    squareLatchedUntilRelease_ = false;
    stickDownLatchedUntilNeutral_ = false;
    stickUpLatchedUntilNeutral_ = false;
    stickOverlapLatchedUntilNeutral_ = false;
    clearOwnedOutputDrain();
    physicalSquareUpPolls_ = 0;
    physicalSquareReleaseSeenSinceArm_ = false;
    physicalSquareDownSeenSinceArm_ = false;
    stickUpFrames_ = 0;
    rightStickNeutralFrames_ = 0;
    lastReleaseWallMs_ = -1.0;
    lastReleaseSeq_ = 0;
    lastOutcomeConsumedSeq_ = -1;
    lastReleaseShotType_.clear();
    lastReleaseMarkerSeq_ = 0;
    lastReleaseMarkerShotType_.clear();
    lastReleaseMarkerPhysicalShotEpoch_ = 0;
    lastReleaseMarkerShotAttempt_ = 0;
    lastReleasePhysicalShotEpoch_ = 0;
    lastReleaseShotAttempt_ = 0;
    // [ORION_ARMED_SOURCE] A reset must not let a previous session's arming decision label
    // the next session's first outcome.
    lastReleaseArmedSource_.clear();
    lastReleaseArmedSigmaMs_ = -1.0;
    lastReleaseArmedFillPct_ = -1.0;
    lastReleaseArmedCommandEtaMs_ = -1.0;
    lastReleaseScheduleToken_ = 0;
    outcomeFeedbackActive_ = false;
    pendingSelfGradeSeq_ = -1;
    pendingSelfGradeDeadlineMs_ = -1.0;
    // [ORION_COURT_POSITION] telemetry only, but a stale position must not bleed across a
    // session boundary and label a new session's first abort with the old session's court spot.
    lastMeterNormX_ = -1.0;
    lastMeterNormY_ = -1.0;
    meterLockPrevNormX_ = -1.0;
    meterLockPrevNormY_ = -1.0;
    meterLockMaxJumpNorm_ = -1.0;
    meterCapNormXAtRel_ = -1.0;
    meterCapNormYAtRel_ = -1.0;
    // [ORION_LANDING_FEATURES 2026-08-12] Cleared with the rest of the at-release snapshot so a
    // stale value from the previous shot can never be attributed to this one.
    meterCapFrameAgeAtRelMs_ = -1.0;
    meterCapNetworkOffsetAtRelMs_ = -1.0;
}

void AutomationEngine::beginSidecarProcessGeneration()
{
    // Fence any copied precise-fire token before erasing its timing authority.
    // `clearScheduledFire()` alone cannot revoke a worker that already copied it.
    invalidateUnconfirmedSchedule(false, "sidecar_generation");

    // Frame ids and latency scope epochs are both process-local. Reset them on
    // the confirmed QProcess::started boundary so a valid fresh process may
    // begin below the prior process's high-water marks.
    lastDetectorFrameNumber_ = -1;
    lastDetectorCaptureTsMs_ = -1.0;
    measuredLatencyMs_ = 0.0;
    measuredLatencySdMs_ = 0.0;
    measuredLFixedMs_ = 0.0;   // [ORION_PRESS_ANCHOR] V estimate dies with its snapshot
    measuredLatencyAuthorityKind_ = QStringLiteral("none");
    measuredLatencyAuthorityMs_ = 0.0;
    measuredLatencyAuthoritySdMs_ = 0.0;
    measuredLatencyN_ = 0;
    measuredLatencyControlledAnchor_ = false;
    measuredLatencyProvisional_ = false;
    measuredLatencyRestored_ = false;
    measuredLatencyVideoRouteAttested_ = false;
    measuredLatencyFactoryPrior_ = false;
    measuredLatencyPriorSource_.clear();
    measuredLatencyModelVersion_.clear();
    measuredLatencyAttestationGeneration_ = 0;
    measuredLatencyDeliveryRoute_ = LatencyControllerRoute::None;
    measuredLatencyScopeEpoch_ = 0;
    publishControllerDeliveryRouteAttestation(
        0, LatencyControllerRoute::None);
    measuredLeadLastUpdateMs_ = -1.0;
    measuredLeadTelemetryPresent_ = false;
    measuredLeadEpochReady_ = false;
    if (measuredLeadEverValid_) {
        measuredLeadRequireNProgress_ = true;
        measuredLeadRestartFloorN_ = -1;
    }
    tickPhaseEpochMs_ = -1.0;
    tickPhaseConf_ = 0.0;
    tickPhaseSdMs_ = -1.0;
    tickPhaseLastUpdateMs_ = -1.0;
    clearScheduledFire();
    refreshLatencyCalibrationStatus(true);
}

double AutomationEngine::nowMs() const
{
    if (testClockMs_ >= 0.0)        // TEST-ONLY mock clock; always < 0 in production
        return testClockMs_;
    return static_cast<double>(clock_.nsecsElapsed()) / 1'000'000.0;
}

void AutomationEngine::advanceTestClock(double deltaMs)
{
    if (testClockMs_ < 0.0) {       // first advance: anchor to real time, then run on the mock
        testClockMs_ = static_cast<double>(clock_.nsecsElapsed()) / 1'000'000.0;
    }
    const double priorMs = testClockMs_;
    testClockMs_ += deltaMs;

    // Legacy engine tests intentionally skip the 4ms controller mirror between
    // large deterministic clock jumps. In production those omitted polls both
    // stabilize LS and synchronously acknowledge the route commit. Reconstruct
    // only that omitted test harness activity here; live execution can reach
    // these phases solely through real process()/controller callbacks.
    if (tempoMovement_.phase == TempoMovementPhase::AcquireIntent
        && physicalGestureActiveForMode(tempoMovement_.mode, lastPhysical_)) {
        while (tempoMovement_.sampleCount < 3) {
            const std::size_t index = static_cast<std::size_t>(
                tempoMovement_.sampleCount++);
            tempoMovement_.lsX[index] = lastPhysical_.leftStickX;
            tempoMovement_.lsY[index] = lastPhysical_.leftStickY;
            tempoMovement_.shotType[index] = classifyShotType(
                lastPhysical_, tempoMovement_.mode);
        }
        if (stabilizeTempoMovementIntent()) {
            tempoMovement_.phase = TempoMovementPhase::AwaitCommitDelivery;
            tempoMovement_.commitStartedMs = priorMs;
        }
    }
    if (tempoMovement_.phase == TempoMovementPhase::AwaitCommitDelivery) {
        tempoMovement_.phase = TempoMovementPhase::CommitDwell;
        tempoMovement_.commitConfirmedMs = priorMs;
    }
    if (tempoMovement_.phase == TempoMovementPhase::CommitDwell
        && testClockMs_ - tempoMovement_.commitConfirmedMs >= 12.0) {
        tempoMovement_.phase = TempoMovementPhase::Active;
    }
}

bool AutomationEngine::squareInputAllowed() const
{
    return config_.inputMode == QLatin1String("square_only") || config_.inputMode == QLatin1String("both");
}

bool AutomationEngine::stickInputAllowed() const
{
    return config_.inputMode == QLatin1String("stick_only") || config_.inputMode == QLatin1String("both");
}

bool AutomationEngine::stickShotActive(const ControllerState& state) const
{
    // Use the exact same raw threshold + lateral-dominance predicate as the
    // first-edge reader wake. Time-based latches (stickHoldArmMs) still filter
    // brief noise; a diagonal dribble must never become bot-owned after the
    // reader correctly declined to open its shot gate.
    return verticalStickShotIntent(
        state, false, config_.stickDownThreshold, config_.gotoLateralMaxRatio);
}

bool AutomationEngine::stickTempoArmAllowed() const
{
    // Raw RS-DOWN is its own physical shot source. The Square Tempo remap cannot
    // silently opt a square-only user into bot ownership of stick/dribble input.
    return stickInputAllowed();
}

QString AutomationEngine::classifyShotType(const ControllerState& physical, ShotMode mode) const
{
    if (mode == ShotMode::GoToStick) {
        return QStringLiteral("Go-To");
    }

    const double lx = static_cast<double>(physical.leftStickX);
    const double ly = static_cast<double>(physical.leftStickY);
    const double leftMag = std::hypot(lx, ly);
    if (physical.l2 >= 150) {
        // L2 held = a post MOVE (aim-stick deflection) vs a NO-DIP shot (square only, no stick).
        // Matches controller_remap.py's input signatures (L2+stick = Post *, L2+square-only = No Dip).
        // The old "any L2 -> Post Fade" mis-bucketed the user's no-dip shots (L2 + square, no stick)
        // as a post move on the wrong clock -- the "no-dip triggers a post shot" bug.
        if (leftMag < config_.movingSquareThreshold) {
            return QStringLiteral("No Dip");
        }
        return QStringLiteral("Post Fade");
    }
    // A fade is a directional stick deflection held during the shot. In this game a fade is
    // ALWAYS a LEFT or RIGHT fade — corner, wing, and top-of-the-key fades all reduce to a
    // side, and the slight "up" the player adds is part of EVERY fade, not a separate shot.
    // The bot also cannot see court position from the controller, so the only meaningful,
    // consistently-detectable buckets are Left Fade / Right Fade, keyed on the HORIZONTAL
    // sign of the aim stick (the vertical component is ignored). The old code split an
    // up-dominant fade into bogus "Back/Front Fade" buckets BY THE VERTICAL SIGN ALONE —
    // which mixed left AND right fades into the same bucket and fragmented each fade's
    // calibration shot-to-shot (there is no "back fade" in the game).
    if (leftMag >= config_.movingSquareThreshold) {
        return lx < 0.0 ? QStringLiteral("Left Fade") : QStringLiteral("Right Fade");
    }
    return QStringLiteral("Standstill");
}

double AutomationEngine::shotTypeOffsetMs(const QString& type) const
{
    // Per-shot-type release offset = the user's static trim (shot_type_offsets, 0
    // by default) PLUS the LIVE learned offset (shotTypeLearnedOffsetMs) that
    // learnFromRelease self-calibrates per type. Every type — Left Fade, Right
    // Fade, Standstill, Go-To, … — resolves to its OWN base+learned, so each
    // direction self-corrects independently. (The old code averaged Left+Right
    // for "Fade" and, worse, early-returned the static value for keys present in
    // shot_type_offsets WITHOUT adding the learned offset — so live calibration
    // never actually applied to the common shot types.)
    const QString key = type.trimmed();
    const double base = config_.shotTypeOffsets.value(key, 0.0);
    const double learned = config_.shotTypeLearnedOffsetMs.value(key, 0.0);
    // Clamp the NET effect: a stale / over-dialed slider (live: Right Fade hit the +/-250 UI
    // rail) must not push the release outside a plausible window around the learned clock. With
    // the wind-up-invariant meter-appear anchor a small trim suffices. Applied here so BOTH the
    // vision lead (effectiveLatency) and the feedforward clock honour the same bound.
    return std::clamp(base + learned, -config_.offsetCapMs, config_.offsetCapMs);
}

double AutomationEngine::tipPhaseTypeTrimMs(const QString& shotType) const noexcept
{
    // [ORION_TYPE_TRIM] Flag OFF (the default): exactly 0 for every type, so the decision and
    // the learner are bit-identical to the pre-flag engine. Flag ON: the exact classifier
    // label's entry, clamped to single-digit ms — at the measured ~0.185 pp/ms meter speed a
    // maximal wrong trim moves the release by under 2 pp of fill, and the symmetric learner
    // application means a wrong value mis-normalises that type's samples by the same amount it
    // mis-aims the shot (one error, visible in one place, not two compounding ones).
    //
    // Substring matching is deliberately NOT used ("Post Fade" must not inherit a trim measured
    // on "Right Fade" presses); an unmeasured type gets the pooled constant, unchanged.
    if (!config_.tipPhaseTypeTrimEnabled) {
        return 0.0;
    }
    const auto it = config_.tipPhaseTypeTrims.constFind(shotType.trimmed());
    if (it == config_.tipPhaseTypeTrims.constEnd() || !std::isfinite(it.value())) {
        return 0.0;
    }
    return std::clamp(it.value(), -kTipPhaseTypeTrimCapMs, kTipPhaseTypeTrimCapMs);
}

QString AutomationEngine::timingKey(const QString& shotType, ShotMode mode)
{
    Q_UNUSED(mode);
    const QString t = shotType.trimmed();
    return t;
}

double AutomationEngine::seededValue(const QMap<QString, double>& map,
                                     const QString& bucketKey, const QString& shotType,
                                     double fallback)
{
    // Prefer the canonical bucket, then the plain shot type for legacy callers.
    const auto it = map.constFind(bucketKey);
    if (it != map.constEnd()) {
        return it.value();
    }
    return map.value(shotType.trimmed(), fallback);
}

bool AutomationEngine::resolveTempoForType(const QString& shotType) const
{
    Q_UNUSED(shotType);
    // The user-facing toggle is the sole remap authority. Legacy per-type
    // overrides remain parseable but cannot leak Square while Tempo is on or
    // silently enable Tempo while the toggle is off.
    return config_.tempoRemapEnabled;
}

void AutomationEngine::setTempoRemapBridgeState(bool delayEngineEngaged, bool interceptApplying,
                                                const QString& delayStateLabel) noexcept
{
    // [ORION_TEMPO_BRIDGE_LIVE task #36] Plain member writes, same thread as process()
    // (OrionAppController owns the MeterDelayController, the VeniceNet client and the input
    // poll on one thread) — mirrors setMeterDelayCondition's contract. Idempotent under a
    // service restart by construction: the state is re-pushed on every backend transition
    // and no latch survives here.
    tempoBridgeGateWired_ = true;
    tempoBridgeDelayEngaged_ = delayEngineEngaged;
    tempoBridgeInterceptApplying_ = interceptApplying;
    tempoBridgeStateLabel_ = delayStateLabel.isEmpty()
        ? QStringLiteral("unknown") : delayStateLabel;
}

void AutomationEngine::noteTempoRemapBridgeHealthBeat() noexcept
{
    tempoBridgeGateWired_ = true;
    tempoBridgeHealthBeatMs_ = nowMs();
}

bool AutomationEngine::tempoRemapBridgeLive(double now) const noexcept
{
    // [ORION_TEMPO_BRIDGE_LIVE task #36] See the header note. Unwired = standalone/unit
    // engine: the gate is open and the toggle-only legacy behavior is bit-identical.
    if (!tempoBridgeGateWired_) {
        return true;
    }
    // [FIX 2026-08-09] The delay engine NOT commanding means the owner simply is not using
    // meter delay on this shot. Tempo remap does not ride the delay intercept -- it delivers
    // Square + right-stick through the input pipe, which is live whenever the hook is. The
    // filed symptom (#36) was delay ENABLED while the backend was not actually intercepting,
    // i.e. the interceptApplying check below; gating on delay-engaged as well was too broad
    // and silently disabled tempo remap for every delay-off shot. Legacy behavior here.
    if (!tempoBridgeDelayEngaged_ && meterDelayAppliedMs_ <= 0.0) {
        return true;
    }
    // ...the backend must CONFIRM it is holding packets (its own armed echo — the
    // MeterDelayController ramps open-loop, so its state alone cannot distinguish a live
    // intercept from a sniff-only debug bridge, which is exactly the filed symptom)...
    if (!tempoBridgeInterceptApplying_) {
        return false;
    }
    // ...and something behind the bridge must have produced a health beat recently.
    return tempoBridgeHealthBeatMs_ >= 0.0
        && (now - tempoBridgeHealthBeatMs_) <= kTempoRemapBridgeHealthFreshMs;
}

void AutomationEngine::setPhysicalShotEpoch(quint64 epoch) noexcept
{
    // The source is a local synchronous monotonic counter. Ignore zero and regressions so
    // no delayed/replayed caller can move the expected identity back to a prior shot.
    if (epoch > physicalShotEpoch_) {
        physicalShotEpoch_ = epoch;
        // [ORION_PRESS_ANCHOR] Date the press at WALL time. This call happens on the same
        // synchronous GUI tick that polled the raw input edge (OrionAppController assigns the
        // epoch and calls here before process()), so nowMs() is the input hook's press moment
        // to within one poll tick — BEFORE the video pipeline and before any applied meter
        // delay. Keyed by epoch: a consumer must match its own shot epoch to use it.
        pressWallEpoch_ = epoch;
        pressWallMsForEpoch_ = nowMs();
    }
}

bool AutomationEngine::automaticCalibrationEpochCurrent() const noexcept
{
    return shot_.physicalShotEpoch != 0
        && shot_.physicalShotEpoch == physicalShotEpoch_
        && shot_.gameplayStructureVerified
        && shot_.gameplayStructureEpoch == shot_.physicalShotEpoch;
}

void AutomationEngine::clearPendingMeterOwnershipEpisode() noexcept
{
    pendingMeterOwnership_ = PendingMeterOwnershipEpisode{};
}

void AutomationEngine::clearPendingMeterOwnershipEvidence() noexcept
{
    pendingMeterOwnershipBreaks_ = PendingMeterOwnershipBreakCensus{};
    pendingMeterOwnershipCurrentEvidenceSeen_ = false;
    pendingMeterOwnershipMaxProofSamples_ = 0;
    pendingMeterOwnershipFirstEvidenceFillPct_ = 0.0;
    pendingMeterOwnershipLastEvidenceFillPct_ = 0.0;
    pendingMeterOwnershipSubAnchorSeen_ = false;
    pendingMeterOwnershipStaleSamples_ = 0;
    pendingMeterOwnershipStaleFirstMs_ = -1.0;
    pendingMeterOwnershipStaleMinPct_ = 0.0;
    pendingMeterOwnershipStaleMaxPct_ = 0.0;
}

bool AutomationEngine::pendingMeterOwnershipStaleMeterBlocked(
    double now, double pressAgeMs) const noexcept
{
    // Every condition must hold, and each one alone is already unreachable for a real meter:
    //  (1) no frame of this press ever reached the first-sight bound. A genuine meter renders
    //      empty and fills, so it ALWAYS produces a sub-bound frame and disarms the census.
    //  (2) the press is older than any normal acquisition, so a merely slow meter is not
    //      censused — by this age a real one has long since been owned or seen low.
    //  (3) a long, densely-sampled run, so a couple of unlucky frames cannot trip it.
    //  (4) the run is numerically static. A real meter sweeps the bar in well under a second;
    //      holding inside kStaleMeterMaxSpreadPct for kStaleMeterMinWindowMs is ~40x slower
    //      than anything the game draws.
    if (pendingMeterOwnershipSubAnchorSeen_
        || pendingMeterOwnershipStaleSamples_ < kStaleMeterMinSamples
        || pendingMeterOwnershipStaleFirstMs_ < 0.0
        || pressAgeMs < kStaleMeterMinPressAgeMs) {
        return false;
    }
    if (now - pendingMeterOwnershipStaleFirstMs_ < kStaleMeterMinWindowMs) {
        return false;
    }
    return pendingMeterOwnershipStaleMaxPct_ - pendingMeterOwnershipStaleMinPct_
        <= kStaleMeterMaxSpreadPct;
}

void AutomationEngine::clearPendingSquareMovementContext() noexcept
{
    pendingSquareShotType_.clear();
    pendingSquareLsArmX_ = 0.0;
    pendingSquareLsArmY_ = 0.0;
    pendingSquareMovementValid_ = false;
}

void AutomationEngine::clearPendingTempoStickMovementContext() noexcept
{
    pendingTempoStickShotType_.clear();
    pendingTempoStickLsArmX_ = 0.0;
    pendingTempoStickLsArmY_ = 0.0;
    pendingTempoStickMovementValid_ = false;
}

bool AutomationEngine::tempoMovementOwned() const noexcept
{
    const bool transactionOwns = tempoMovement_.phase != TempoMovementPhase::None;
    const bool activeShotOwns = shot_.state != HoldState::Idle
        && (shot_.mode == ShotMode::TempoSquare
            || shot_.mode == ShotMode::TempoStick);
    const bool drainOwns = ownedOutputDrain_ != OwnedOutputDrain::None
        && (ownedOutputDrainMode_ == ShotMode::TempoSquare
            || ownedOutputDrainMode_ == ShotMode::TempoStick);
    return transactionOwns || activeShotOwns || drainOwns
        || tempoPassThroughPulseActive_;
}

bool AutomationEngine::tempoMovementCommitPending() const noexcept
{
    return tempoMovement_.phase == TempoMovementPhase::AwaitCommitDelivery;
}

quint64 AutomationEngine::tempoMovementCommitGeneration() const noexcept
{
    return tempoMovementCommitPending() ? tempoMovement_.generation : 0;
}

void AutomationEngine::confirmTempoMovementCommit(quint64 generation, bool accepted)
{
    if (generation == 0 || generation != tempoMovement_.generation
        || tempoMovement_.phase != TempoMovementPhase::AwaitCommitDelivery) {
        return;
    }

    if (!accepted) {
        // [ORION_METER_DELAY 2026-08-07] Do NOT fail the transaction here — the
        // commit line is re-emitted on every tick, and kCommitAckTimeoutMs (see
        // TempoMovementPhase::AwaitCommitDelivery block below) still fails
        // closed if delivery never succeeds. Bailing out on a single delivery
        // hiccup was killing transactions that would have completed on the next
        // tick.
        return;
    }

    tempoMovement_.phase = TempoMovementPhase::CommitDwell;
    tempoMovement_.commitConfirmedMs = nowMs();
    emit engineDiagnostic(QStringLiteral(
        "Tempo movement commit accepted: generation=%1 mode=%2 ls=(%3,%4) type=%5")
                              .arg(generation)
                              .arg(static_cast<int>(tempoMovement_.mode))
                              .arg(tempoMovement_.committedLsX)
                              .arg(tempoMovement_.committedLsY)
                              .arg(tempoMovement_.committedShotType));
}

void AutomationEngine::startTempoMovementTransaction(ShotMode mode, double now)
{
    resetTempoMovementTransaction();
    ++tempoMovementGenerationCounter_;
    if (tempoMovementGenerationCounter_ == 0) {
        ++tempoMovementGenerationCounter_;
    }
    tempoMovement_.phase = TempoMovementPhase::AcquireIntent;
    tempoMovement_.mode = mode;
    tempoMovement_.generation = tempoMovementGenerationCounter_;
    tempoMovement_.startedMs = now;
}

bool AutomationEngine::stabilizeTempoMovementIntent()
{
    // Three consecutive reports must agree on semantic intent. This filters a
    // transient centered LS poll without hiding a real change of direction.
    if (tempoMovement_.sampleCount < 3) {
        return false;
    }
    const int first = tempoMovement_.sampleCount - 3;
    const QString type = tempoMovement_.shotType[static_cast<std::size_t>(first)];
    if (type.isEmpty()) {
        return false;
    }
    for (int i = first + 1; i < tempoMovement_.sampleCount; ++i) {
        if (tempoMovement_.shotType[static_cast<std::size_t>(i)] != type) {
            return false;
        }
    }

    const bool fade = type.contains(QStringLiteral("Fade"), Qt::CaseInsensitive);
    const double releaseThreshold = config_.movingSquareThreshold * 0.8;
    for (int i = first; i < tempoMovement_.sampleCount; ++i) {
        const double magnitude = std::hypot(
            static_cast<double>(tempoMovement_.lsX[static_cast<std::size_t>(i)]),
            static_cast<double>(tempoMovement_.lsY[static_cast<std::size_t>(i)]));
        if ((fade && magnitude < config_.movingSquareThreshold)
            || (!fade && magnitude > releaseThreshold)) {
            return false;
        }
    }

    auto median3 = [first](
        const std::array<int, TempoMovementTransaction::kIntentSlots>& values) {
        std::array<int, 3> ordered{{
            values[static_cast<std::size_t>(first)],
            values[static_cast<std::size_t>(first + 1)],
            values[static_cast<std::size_t>(first + 2)],
        }};
        std::sort(ordered.begin(), ordered.end());
        return ordered[1];
    };

    int committedX = 0;
    int committedY = 0;
    if (fade) {
        committedX = median3(tempoMovement_.lsX);
        committedY = median3(tempoMovement_.lsY);
        const double magnitude = std::hypot(
            static_cast<double>(committedX), static_cast<double>(committedY));
        const bool directionMatches =
            (!type.startsWith(QStringLiteral("Left"), Qt::CaseInsensitive)
             || committedX < 0)
            && (!type.startsWith(QStringLiteral("Right"), Qt::CaseInsensitive)
                || committedX >= 0);
        // REVERTED 2026-08-06. A lateral-dominance refusal briefly lived here, added when
        // the step-back was (wrongly) attributed to back-dominant LS vectors being frozen by
        // the Tempo transaction. The logs disproved it: the refusal worked exactly as designed
        // — back-dominant commits went to zero — and the step-back survived untouched. The real
        // cause was the RIGHT stick (a fade needs the mirrored gather; see ShotReleasePolicy.h),
        // now fixed properly.
        //
        // Leaving the refusal in was actively harmful: it diverted ~29% of fades (6 of 21 in one
        // live session) into failTempoMovementTransaction, i.e. out of the Tempo remap entirely
        // and back onto the plain button path — reported by the owner as "some fades don't apply
        // the tempo timing remapping". With the gather direction fixed, a back-dominant LS vector
        // is no longer a step-back hazard, so the refusal has no remaining purpose.
        if (magnitude < config_.movingSquareThreshold || !directionMatches) {
            return false;
        }
    }

    tempoMovement_.committedLsX = committedX;
    tempoMovement_.committedLsY = committedY;
    tempoMovement_.committedShotType = type;
    if (tempoMovement_.mode == ShotMode::TempoSquare) {
        pendingSquareShotType_ = type;
        pendingSquareLsArmX_ = static_cast<double>(committedX);
        pendingSquareLsArmY_ = static_cast<double>(committedY);
        pendingSquareMovementValid_ = true;
    } else {
        pendingTempoStickShotType_ = type;
        pendingTempoStickLsArmX_ = static_cast<double>(committedX);
        pendingTempoStickLsArmY_ = static_cast<double>(committedY);
        pendingTempoStickMovementValid_ = true;
    }
    return true;
}

void AutomationEngine::applyTempoMovementCommitOutput(ControllerState& output) const
{
    // This is deliberately an LS-only transaction: no Square and no right-stick
    // edge may reach the game until the exact active route acknowledges it.
    output.buttons &= ~XINPUT_GAMEPAD_X;
    output.rightStickX = 0;
    output.rightStickY = 0;
    output.leftStickX = tempoMovement_.committedLsX;
    output.leftStickY = tempoMovement_.committedLsY;
}

void AutomationEngine::failTempoMovementTransaction(ShotMode mode,
                                                     const QString& reason)
{
    const quint64 generation = tempoMovement_.generation;
    emit engineDiagnostic(QStringLiteral(
        "Tempo movement transaction failed closed: generation=%1 mode=%2 reason=%3; "
        "no synthetic RS edge emitted")
                              .arg(generation)
                              .arg(static_cast<int>(mode))
                              .arg(reason));
    resetTempoMovementTransaction();
    if (mode == ShotMode::TempoSquare) {
        // The same still-held edge may continue through the regular Button path.
        pendingSquareTempoRemap_ = false;
    } else if (mode == ShotMode::TempoStick) {
        // Raw stick fallback stays byte-for-byte physical until a debounced
        // neutral creates a genuinely new gesture.
        stickRearmBlockedUntilNeutral_ = true;
        stickDownHoldStartMs_ = -1.0;
        clearPendingStickCalibration();
        clearPendingMeterOwnershipEpisode();
        clearPendingMeterOwnershipEvidence();
        clearPendingTempoStickMovementContext();
    }
}

void AutomationEngine::resetTempoMovementTransaction() noexcept
{
    tempoMovement_ = TempoMovementTransaction{};
}

bool AutomationEngine::advanceTempoMovementTransaction(
    ControllerState& output, const ControllerState& physical,
    ShotMode mode, double now)
{
    // [ORION_TEMPO_INTENT_WINDOW] 4/20ms -> 8/45ms. See TempoMovementTransaction::kIntentSlots
    // for the measured failure this fixes. 45ms is still a small fraction of the ~518ms meter
    // rise, so the gather still lands early; the transaction fails closed to the button path as
    // before if the gesture genuinely never settles.
    constexpr int kMaxIntentReports = TempoMovementTransaction::kIntentSlots;
    constexpr double kIntentWindowMs = 45.0;
    constexpr double kCommitAckTimeoutMs = 50.0;
    constexpr double kCommitDwellMs = 12.0;

    if (tempoMovement_.phase == TempoMovementPhase::None) {
        startTempoMovementTransaction(mode, now);
    } else if (tempoMovement_.mode != mode) {
        failTempoMovementTransaction(
            tempoMovement_.mode, QStringLiteral("tempo_movement_mode_changed"));
        return false;
    }

    if (tempoMovement_.phase == TempoMovementPhase::AcquireIntent) {
        if (tempoMovement_.sampleCount < kMaxIntentReports) {
            const std::size_t index = static_cast<std::size_t>(
                tempoMovement_.sampleCount++);
            tempoMovement_.lsX[index] = physical.leftStickX;
            tempoMovement_.lsY[index] = physical.leftStickY;
            tempoMovement_.shotType[index] = classifyShotType(physical, mode);
        }
        if (stabilizeTempoMovementIntent()) {
            tempoMovement_.phase = TempoMovementPhase::AwaitCommitDelivery;
            tempoMovement_.commitStartedMs = now;
            applyTempoMovementCommitOutput(output);
            return true;
        }
        if (tempoMovement_.sampleCount >= kMaxIntentReports
            || now - tempoMovement_.startedMs >= kIntentWindowMs) {
            failTempoMovementTransaction(
                mode, QStringLiteral("tempo_movement_intent_unstable"));
            return false;
        }

        // Acquire is trigger-neutral. Preserve all non-shot controls and the raw
        // LS reports being evaluated, but expose neither Square nor RS-down.
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        output.rightStickY = 0;
        return true;
    }

    if (tempoMovement_.phase == TempoMovementPhase::AwaitCommitDelivery) {
        if (now - tempoMovement_.commitStartedMs >= kCommitAckTimeoutMs) {
            failTempoMovementTransaction(
                mode, QStringLiteral("tempo_movement_commit_ack_timeout"));
            return false;
        }
        applyTempoMovementCommitOutput(output);
        return true;
    }

    if (tempoMovement_.phase == TempoMovementPhase::CommitDwell) {
        applyTempoMovementCommitOutput(output);
        if (now - tempoMovement_.commitConfirmedMs < kCommitDwellMs) {
            return true;
        }
        tempoMovement_.phase = TempoMovementPhase::Active;
    }

    return false;
}

void AutomationEngine::clearPendingStickCalibration() noexcept
{
    pendingStickCalibrationActive_ = false;
    pendingStickCalibrationMode_ = ShotMode::GoToStick;
    pendingStickCalibrationStartMs_ = -1.0;
    pendingStickCalibrationPhysicalEpoch_ = 0;
    pendingStickCalibrationArmGeneration_ = 0;
    pendingStickCalibrationShotType_.clear();
}

void AutomationEngine::cancelPendingLatencyCalibrationOwnership()
{
    bool cancelled = false;
    // [ORION_ABORT_IDENTITY] Both pending-epoch fields are cleared by the branches below, so the
    // identity has to be captured on the way past or the abort emitted at the end of this
    // function is unattributable (this is the `latency_calibration_cancelled` path).
    quint64 cancelledEpoch = 0;
    // [ORION_ABORT_SHOT_TYPE] Same capture-on-the-way-past reason as cancelledEpoch: the stick
    // branch below clears pendingStickCalibrationShotType_ before the emit is reached.
    QString cancelledShotType;
    if (tempoMovement_.phase != TempoMovementPhase::None) {
        if (tempoMovement_.mode == ShotMode::TempoSquare) {
            squareRearmBlockedUntilRelease_ = true;
            pendingSquareTempoRemap_ = false;
        } else if (tempoMovement_.mode == ShotMode::TempoStick) {
            stickRearmBlockedUntilNeutral_ = true;
        }
        resetTempoMovementTransaction();
        cancelled = true;
    }
    if (pendingStickCalibrationActive_) {
        // Pre-ownership output was physical, so cancellation must never emit a
        // synthetic release. Fence this exact gesture until three neutral polls.
        stickRearmBlockedUntilNeutral_ = true;
        stickUpHoldStartMs_ = -1.0;
        stickDownHoldStartMs_ = -1.0;
        stickUpFrames_ = 0;
        retiredPhysicalShotEpoch_ = std::max(
            retiredPhysicalShotEpoch_, pendingStickCalibrationPhysicalEpoch_);
        cancelledEpoch = pendingStickCalibrationPhysicalEpoch_;
        cancelledShotType = pendingStickCalibrationShotType_;
        clearPendingStickCalibration();
        cancelled = true;
    }
    if (squareHoldStartMs_ >= 0.0 && pendingSquarePhysicalEpoch_ != 0) {
        squareRearmBlockedUntilRelease_ = true;
        retiredPhysicalShotEpoch_ = std::max(
            retiredPhysicalShotEpoch_, pendingSquarePhysicalEpoch_);
        cancelledEpoch = pendingSquarePhysicalEpoch_;
        cancelledShotType = pendingSquareShotType_;
        pendingSquarePhysicalEpoch_ = 0;
        pendingSquareArmGeneration_ = 0;
        cancelled = true;
    }
    if (!cancelled) {
        return;
    }
    clearPendingMeterOwnershipEpisode();
    clearPendingMeterOwnershipEvidence();
    shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
    shot_.releaseReason = QStringLiteral("latency_calibration_cancelled");
    shot_.releaseReasonCode = shot_.releaseReason;
    // [ORION_ABORT_IDENTITY] Pre-ownership: no ShotContext exists, so attempt/seq/token are the
    // -1 "not applicable" sentinel. A tempo-only cancellation carries no physical epoch either
    // (nothing had claimed one), which is reported as -1 rather than a fabricated 0.
    // [ORION_ABORT_SHOT_TYPE] Whichever pending candidate this cancellation retired is the one
    // that carries the classification; the stick branch is tested first because it is the branch
    // that clears its own type on the way past (clearPendingStickCalibration()), so reading it
    // after the fact would always report `unclassified`. A tempo-only cancellation retired
    // neither candidate and genuinely has no type.
    emitShotAbortIdentity("cancel_latency_calibration",
                          cancelledEpoch != 0 ? static_cast<qint64>(cancelledEpoch) : -1,
                          -1, -1, -1, shot_.releaseReason, cancelledShotType);
    emit shotAborted(shot_.releaseReason);
}

bool AutomationEngine::pendingSquarePressActive() const noexcept
{
    if (!squareInputAllowed() || squareRearmBlockedUntilRelease_
        || squareHoldStartMs_ < 0.0
        || (pendingSquarePhysicalEpoch_ != 0
            && (pendingSquarePhysicalEpoch_ <= retiredPhysicalShotEpoch_
                || pendingSquareArmGeneration_
                    != armRevocationGeneration_.load(std::memory_order_acquire)))) {
        return false;
    }
    if (lastPhysical_.square()) {
        return true;
    }
    // processIdle() already bridges the virtual Square output until all three
    // raw samples agree on UP. Detection callbacks can land between those input
    // polls, so ownership proof must use the same physical-epoch lifetime. The
    // third UP poll synchronously clears squareHoldStartMs_ and the candidate;
    // this never extends proof beyond the established release debounce.
    return std::any_of(shot_.xButtonHistory.cbegin(), shot_.xButtonHistory.cend(),
                       [](bool pressed) { return pressed; });
}

bool AutomationEngine::physicalGestureActiveForMode(
    ShotMode mode, const ControllerState& physical) const noexcept
{
    switch (mode) {
    case ShotMode::ButtonShot:
    case ShotMode::TempoSquare:
        return squareInputAllowed() && physical.square();
    case ShotMode::GoToStick:
        return config_.gotoEnabled && verticalStickShotIntent(
            physical, true, config_.stickUpThreshold, config_.gotoLateralMaxRatio)
            && !physical.square() && !physical.cross()
            && std::hypot(static_cast<double>(physical.leftStickX),
                          static_cast<double>(physical.leftStickY))
                < config_.movingSquareThreshold
            && physical.l2 < 150 && physical.r2 < 150;
    case ShotMode::TempoStick:
        return stickTempoArmAllowed() && stickShotActive(physical);
    }
    return false;
}

bool AutomationEngine::pendingMeterOwnershipCandidate(
    double atMs, ShotMode& mode, double& gestureStartMs,
    quint64& physicalEpoch, QString& shotType, bool& inputQualified) const
{
    const bool strictOwnershipProof = latencyCalibrationAutomatic_
        || autonomousLiveMeterTimingEnabled();
    const bool squareEpochCurrent = !strictOwnershipProof
        || (pendingSquarePhysicalEpoch_ != 0
            && pendingSquarePhysicalEpoch_ == physicalShotEpoch_);
    if (pendingSquarePressActive() && squareEpochCurrent) {
        mode = pendingSquareTempoRemap_ ? ShotMode::TempoSquare : ShotMode::ButtonShot;
        if (mode == ShotMode::TempoSquare
            && (tempoMovement_.phase != TempoMovementPhase::Active
                || tempoMovement_.mode != ShotMode::TempoSquare)) {
            return false;
        }
        gestureStartMs = squareHoldStartMs_;
        physicalEpoch = pendingSquarePhysicalEpoch_;
        shotType = pendingSquareShotType_.isEmpty()
            ? classifyShotType(lastPhysical_, ShotMode::ButtonShot)
            : pendingSquareShotType_;
        inputQualified = true;
        if (strictOwnershipProof
            && atMs - gestureStartMs >= std::max(1.0, config_.buttonNoMeterAbortMs)) {
            return false;
        }
        return true;
    }

    if (!pendingStickCalibrationActive_
        || pendingStickCalibrationPhysicalEpoch_ == 0
        || pendingStickCalibrationPhysicalEpoch_ <= retiredPhysicalShotEpoch_
        || pendingStickCalibrationPhysicalEpoch_ != physicalShotEpoch_
        || pendingStickCalibrationArmGeneration_
            != armRevocationGeneration_.load(std::memory_order_acquire)
        || pendingStickCalibrationStartMs_ < 0.0
        || stickRearmBlockedUntilNeutral_
        || !physicalGestureActiveForMode(pendingStickCalibrationMode_, lastPhysical_)) {
        return false;
    }

    mode = pendingStickCalibrationMode_;
    gestureStartMs = pendingStickCalibrationStartMs_;
    physicalEpoch = pendingStickCalibrationPhysicalEpoch_;
    shotType = pendingStickCalibrationShotType_;
    if (mode == ShotMode::GoToStick) {
        inputQualified = stickUpFrames_ >= config_.gotoArmFrames
            && atMs - gestureStartMs >= config_.gotoHoldArmMs;
        const double capMs = std::max(
            1.0, config_.gotoMeterWait
                ? std::max(config_.gotoNoMeterAbortMs, config_.gotoMeterWaitCapMs)
                : config_.gotoNoMeterAbortMs);
        if (atMs - gestureStartMs >= capMs) {
            return false;
        }
    } else if (mode == ShotMode::TempoStick) {
        if (tempoMovement_.phase != TempoMovementPhase::Active
            || tempoMovement_.mode != ShotMode::TempoStick) {
            return false;
        }
        inputQualified = atMs - gestureStartMs
            >= std::max(config_.stickHoldArmMs, config_.tempoMinStickHoldMs);
        if (atMs - gestureStartMs
            >= std::max(1.0, config_.buttonNoMeterAbortMs)) {
            return false;
        }
    } else {
        return false;
    }
    return true;
}

bool AutomationEngine::recordPendingMeterOwnershipSample(
    const DetectionResult& result, double sampleNow, ShotMode mode,
    double gestureStartMs, quint64 physicalEpoch, bool inputQualified)
{
    const double pressAgeMs = sampleNow - gestureStartMs;
    const bool strictOwnershipProof = latencyCalibrationAutomatic_
        || autonomousLiveMeterTimingEnabled();
    double strictProofCapMs = std::max(1.0, config_.buttonNoMeterAbortMs);
    if (mode == ShotMode::GoToStick) {
        strictProofCapMs = std::max(
            1.0, config_.gotoMeterWait
                ? std::max(config_.gotoNoMeterAbortMs, config_.gotoMeterWaitCapMs)
                : config_.gotoNoMeterAbortMs);
    }
    const bool pendingWindow = strictOwnershipProof
        ? pressAgeMs >= 0.0 && pressAgeMs < strictProofCapMs
        : pressAgeMs >= 0.0 && pressAgeMs < config_.squareHoldArmMs;
    const bool physicalGestureActive = mode == ShotMode::ButtonShot
            || mode == ShotMode::TempoSquare
        ? pendingSquarePressActive()
        : physicalGestureActiveForMode(mode, lastPhysical_);
    const bool physicalEpochCurrent = !strictOwnershipProof
        || (physicalEpoch != 0 && physicalEpoch == physicalShotEpoch_);
    const bool pendingGesture = shot_.state == HoldState::Idle
        && physicalEpochCurrent
        && physicalGestureActive
        && gestureStartMs >= 0.0
        && pendingWindow;
    const QString stage = result.stage.trimmed().toLower();
    const bool acceptedReason = result.rejectionReason.isEmpty()
        || result.rejectionReason == QStringLiteral("green_not_found");
    const bool coasted = stage == QLatin1String("coast")
        || stage == QLatin1String("no_meter")
        || stage == QLatin1String("stale");
    const bool finite = std::isfinite(result.fillPct)
        && std::isfinite(result.confidence)
        && std::isfinite(result.frameAgeMs);
    const bool strictAge = finite && result.frameAgeMs >= 0.0
        && result.frameAgeMs <= config_.strictReleaseMaxSourceAgeMs;
    const bool usableIdentity = result.frameNumber >= 0
        || (std::isfinite(result.captureTsMs) && result.captureTsMs > 0.0);
    const bool canonicalTimingIdentity = std::isfinite(result.captureTsMs)
        && result.captureTsMs > 0.0
        && std::isfinite(result.measurementCaptureTsMs)
        && result.measurementCaptureTsMs > 0.0;
    // The frame itself, not merely its arrival, must belong to this press.
    const bool capturedAfterPress = strictAge
        && sampleNow - result.frameAgeMs >= gestureStartMs;
    const bool genuineCurrent = pendingGesture && result.detected && acceptedReason
        && !result.staleFrame && !result.ghostFrame && !coasted && strictAge
        && capturedAfterPress && result.confidence >= config_.confidenceGate
        && result.fillPct > 0.0 && result.fillPct <= 100.0
        && result.width > 0 && result.height > 0
        && (!strictOwnershipProof
            || (usableIdentity && canonicalTimingIdentity));
    const bool currentGameplayEpochProof = result.gameplayStructureVerified
        && physicalEpoch != 0
        && result.gameplayStructureEpoch == physicalEpoch;
    if (!genuineCurrent
        || (strictOwnershipProof && !currentGameplayEpochProof)) {
        // A rejected/coasted frame contributes no ownership evidence, but one
        // decoder blink must not erase the unique, structure-proven frames that
        // surround it. Preserve the candidate only inside the same bounded
        // episode. A delayed prior-epoch callback is ignored rather than being
        // allowed to erase current-epoch proof; it contributes no sample. A
        // released Square or longer gap still clears/restarts the episode. This
        // remains fail-closed: missing frames do not increment sampleCount and
        // the full complement of genuine rising frames (3, or 2 with
        // ownershipProofTwoFrame) is still required before input is seized.
        const bool sameCandidate = pendingMeterOwnership_.active
            && pendingMeterOwnership_.mode == mode
            && std::abs(pendingMeterOwnership_.gestureStartMs - gestureStartMs) <= 1e-6
            && pendingMeterOwnership_.physicalEpoch == physicalEpoch;
        const bool preserveAcrossBlink = strictOwnershipProof && pendingGesture
            && sameCandidate
            && pendingMeterOwnership_.first.gameplayStructureVerified
            && pendingMeterOwnership_.first.gameplayStructureEpoch == physicalEpoch
            && sampleNow - pendingMeterOwnership_.lastMs <= kPendingMeterOwnershipMaxGapMs;
        if (!preserveAcrossBlink) {
            clearPendingMeterOwnershipEpisode();
        }
        return false;
    }

    if (!pendingMeterOwnershipCurrentEvidenceSeen_) {
        pendingMeterOwnershipFirstEvidenceFillPct_ = result.fillPct;
    }
    pendingMeterOwnershipCurrentEvidenceSeen_ = true;
    pendingMeterOwnershipLastEvidenceFillPct_ = result.fillPct;

    bool identityAdvanced = true;
    bool geometryContinuous = true;
    const bool sameCandidate = pendingMeterOwnership_.active
        && pendingMeterOwnership_.mode == mode
        && std::abs(pendingMeterOwnership_.gestureStartMs - gestureStartMs) <= 1e-6
        && pendingMeterOwnership_.physicalEpoch == physicalEpoch;
    if (strictOwnershipProof && sameCandidate) {
        const bool frameAdvanced = result.frameNumber >= 0
            && pendingMeterOwnership_.lastFrameNumber >= 0
            && result.frameNumber > pendingMeterOwnership_.lastFrameNumber;
        const bool captureAdvanced = std::isfinite(result.captureTsMs)
            && result.captureTsMs > 0.0 && pendingMeterOwnership_.lastCaptureTsMs > 0.0
            && result.captureTsMs > pendingMeterOwnership_.lastCaptureTsMs + 1e-6;
        identityAdvanced = frameAdvanced || captureAdvanced;

        const int left = std::max(result.x, pendingMeterOwnership_.lastX);
        const int top = std::max(result.y, pendingMeterOwnership_.lastY);
        const int right = std::min(result.x + result.width,
                                   pendingMeterOwnership_.lastX + pendingMeterOwnership_.lastWidth);
        const int bottom = std::min(result.y + result.height,
                                    pendingMeterOwnership_.lastY + pendingMeterOwnership_.lastHeight);
        const double intersection = static_cast<double>(std::max(0, right - left))
            * static_cast<double>(std::max(0, bottom - top));
        const double areaNow = static_cast<double>(result.width) * result.height;
        const double areaLast = static_cast<double>(pendingMeterOwnership_.lastWidth)
            * pendingMeterOwnership_.lastHeight;
        const double unionArea = areaNow + areaLast - intersection;
        const double widthScale = static_cast<double>(result.width)
            / pendingMeterOwnership_.lastWidth;
        const double heightScale = static_cast<double>(result.height)
            / pendingMeterOwnership_.lastHeight;
        const double aspectNow = static_cast<double>(result.width) / result.height;
        const double aspectLast = static_cast<double>(pendingMeterOwnership_.lastWidth)
            / pendingMeterOwnership_.lastHeight;
        const double aspectScale = aspectNow / aspectLast;
        // A false lock can share a center with the meter yet stretch one dimension by 10x,
        // which still passes an IoU>=0.10 check. Strict live ownership proof therefore requires
        // both spatial overlap and bounded per-frame shape drift.  These are detector-integrity
        // limits, not release timing constants.
        constexpr double kMaxDimensionScale = 1.50;
        constexpr double kMaxAspectScale = 1.25;
        geometryContinuous = unionArea > 0.0 && intersection / unionArea >= 0.10
            && widthScale >= 1.0 / kMaxDimensionScale
            && widthScale <= kMaxDimensionScale
            && heightScale >= 1.0 / kMaxDimensionScale
            && heightScale <= kMaxDimensionScale
            && aspectScale >= 1.0 / kMaxAspectScale
            && aspectScale <= kMaxAspectScale;
        // A repeated payload is not a new proof frame. Ignore it without
        // destroying the genuine episode around it; if it persists beyond the
        // bounded gap, the normal broken-episode path below starts over.
        if (!identityAdvanced
            && sampleNow - pendingMeterOwnership_.lastMs <= kPendingMeterOwnershipMaxGapMs) {
            return false;
        }
    }
    // [ORION_EPISODE_REANCHOR] A sample BELOW this episode's anchor restarts the episode on that
    // sample instead of being accumulated under the old one.
    //
    // The first current-epoch frame of a press is frequently the PREVIOUS shot's meter, still
    // rendered and decaying. Captured live (2026-08-04, physical_epoch=20): the first evidence
    // frame read 40.0 -- exactly on anchorMaxFirstFillPct, so it opened an episode -- and this
    // press's real meter appeared later and far lower. With the anchor pinned to a stale 40.0 the
    // rise test below demanded the new meter climb past 43 before three samples could ever
    // qualify, and the user held Square for 1,459 ms and got no assistance at all.
    //
    // The existing sharp-drop rule only caught a >8 pp cliff. A stale meter that DECAYS gently
    // (40 -> 36 -> 32 -> ...) never trips it, so the episode kept a poisoned anchor for the whole
    // press. Anchoring on the lowest point observed is the honest reading: a real meter renders
    // empty and only ever rises away from its own start, so the lowest current-epoch sample is
    // this shot's meter-appear instant.
    //
    // Routed through `broken` deliberately rather than mutating the episode in place: the restart
    // path below re-applies the anchorMaxFirstFillPct first-sight bound (so a descending run that
    // is still ABOVE the bound is censused as a stale meter rather than silently anchoring), and
    // it preserves the samples[0] == first invariant that seedPromotedMeterOwnershipEpisode()
    // depends on when it replays the episode into the sampler.
    //
    // The descent must be MATERIAL -- more than this engine's own definition of numerically
    // static (kStaleMeterMaxSpreadPct) -- so ordinary detector noise on a rising meter cannot
    // restart the episode and slow acquisition. It cannot loosen the false-lock guard either: a
    // lookalike still has to RISE anchorRiseMinPct (3.0 pp) above its own minimum across unique,
    // geometry-continuous, structure-verified current-epoch frames (3, or 2 with
    // ownershipProofTwoFrame), which stays strictly outside the 2.5 pp static band a frozen or
    // decaying HUD occupies.
    const bool descendedBelowAnchor = pendingMeterOwnership_.active
        && result.fillPct
            < pendingMeterOwnership_.first.fillPct - kStaleMeterMaxSpreadPct;
    const bool broken = pendingMeterOwnership_.active
        && (!sameCandidate
            || (sampleNow - pendingMeterOwnership_.lastMs) > kPendingMeterOwnershipMaxGapMs
            || result.fillPct < pendingMeterOwnership_.lastFillPct - 8.0
            || descendedBelowAnchor
            || !identityAdvanced || !geometryContinuous);
    if (broken) {
        // Recording only: attribute the teardown so a press that never accumulates three proof
        // samples names its own cause instead of leaving samples=1 to be guessed at.
        if (!sameCandidate
            || (sampleNow - pendingMeterOwnership_.lastMs) > kPendingMeterOwnershipMaxGapMs) {
            ++pendingMeterOwnershipBreaks_.gapOrCandidate;
        } else if (result.fillPct < pendingMeterOwnership_.lastFillPct - 8.0) {
            ++pendingMeterOwnershipBreaks_.drop;
        } else if (descendedBelowAnchor) {
            ++pendingMeterOwnershipBreaks_.anchor;
        } else if (!identityAdvanced) {
            ++pendingMeterOwnershipBreaks_.identity;
        } else {
            ++pendingMeterOwnershipBreaks_.geometry;
        }
    }
    if (!pendingMeterOwnership_.active || broken) {
        // Strict autonomous ownership must observe the beginning of this shot's
        // meter, not merely three rising frames from a previous high-fill track.
        // A late court-wide acquire remains presentation-only until the detector
        // sees a fresh low-fill episode for the current physical input token.
        if (strictOwnershipProof
            && result.fillPct > config_.anchorMaxFirstFillPct + 1e-6) {
            // Census the rejected high-fill run so a STATIC one (the previous shot's meter
            // still rendered, or frozen, over this press's epoch) can be reported early
            // instead of costing the player the whole buttonNoMeterAbortMs wait holding the
            // ball. Recording only; this branch still refuses to open an episode exactly as
            // before, so nothing here can own, arm or release a shot.
            if (pendingMeterOwnershipStaleSamples_ == 0) {
                pendingMeterOwnershipStaleFirstMs_ = sampleNow;
                pendingMeterOwnershipStaleMinPct_ = result.fillPct;
                pendingMeterOwnershipStaleMaxPct_ = result.fillPct;
            } else {
                pendingMeterOwnershipStaleMinPct_ = std::min(
                    pendingMeterOwnershipStaleMinPct_, result.fillPct);
                pendingMeterOwnershipStaleMaxPct_ = std::max(
                    pendingMeterOwnershipStaleMaxPct_, result.fillPct);
            }
            ++pendingMeterOwnershipStaleSamples_;
            clearPendingMeterOwnershipEpisode();
            return false;
        }
        // A frame at/below the first-sight bound is the beginning of a real meter for this
        // press. Disarm the stale census permanently for this epoch: from here the ordinary
        // rise/geometry proof decides, and a genuine meter can never be censused.
        pendingMeterOwnershipSubAnchorSeen_ = true;
        ++pendingMeterOwnershipBreaks_.restarts;
        pendingMeterOwnership_ = PendingMeterOwnershipEpisode{};
        pendingMeterOwnership_.active = true;
        pendingMeterOwnership_.mode = mode;
        pendingMeterOwnership_.gestureStartMs = gestureStartMs;
        pendingMeterOwnership_.physicalEpoch = physicalEpoch;
        pendingMeterOwnership_.first = result;
        pendingMeterOwnership_.firstMs = sampleNow;
        pendingMeterOwnership_.lastMs = sampleNow;
        pendingMeterOwnership_.lastFillPct = result.fillPct;
        pendingMeterOwnership_.sampleCount = 1;
        pendingMeterOwnership_.lastFrameNumber = result.frameNumber;
        pendingMeterOwnership_.lastCaptureTsMs = result.captureTsMs;
        pendingMeterOwnership_.lastX = result.x;
        pendingMeterOwnership_.lastY = result.y;
        pendingMeterOwnership_.lastWidth = result.width;
        pendingMeterOwnership_.lastHeight = result.height;
        pendingMeterOwnership_.samples.append(PendingMeterOwnershipSample{result, sampleNow});
        pendingMeterOwnershipMaxProofSamples_ = std::max(
            pendingMeterOwnershipMaxProofSamples_, pendingMeterOwnership_.sampleCount);
        return false;
    }

    // Early ownership is intentionally stricter than the normal post-debounce
    // anchor: it requires material forward motion. A static two-frame menu HUD
    // lookalike may remain visible in the overlay but cannot seize Square.
    const bool roseEnough = result.fillPct - pendingMeterOwnership_.first.fillPct
        >= config_.anchorRiseMinPct;
    pendingMeterOwnership_.lastMs = sampleNow;
    pendingMeterOwnership_.lastFillPct = result.fillPct;
    pendingMeterOwnership_.sampleCount += 1;
    pendingMeterOwnership_.lastFrameNumber = result.frameNumber;
    pendingMeterOwnership_.lastCaptureTsMs = result.captureTsMs;
    pendingMeterOwnership_.lastX = result.x;
    pendingMeterOwnership_.lastY = result.y;
    pendingMeterOwnership_.lastWidth = result.width;
    pendingMeterOwnership_.lastHeight = result.height;
    // Match TemporalSampler's bounded evidence window. Preserve the episode's first
    // frame for the rise proof and the newest frames for the crossing fit.
    if (pendingMeterOwnership_.samples.size() >= 16) {
        pendingMeterOwnership_.samples.removeAt(1);
    }
    pendingMeterOwnership_.samples.append(PendingMeterOwnershipSample{result, sampleNow});
    pendingMeterOwnershipMaxProofSamples_ = std::max(
        pendingMeterOwnershipMaxProofSamples_, pendingMeterOwnership_.sampleCount);
    // Candidate-A decision-budget flag: the 3rd frame spends ~17ms of the 93ms
    // anchor->deadline budget, and offline replay (validate_ownership_proof.py,
    // 369 shot episodes) showed it admits nothing the 2-frame proof rejects. The
    // rise proof above stays the discriminator; a static/decaying lookalike is
    // bounded by kStaleMeterMaxSpreadPct (2.5pp) and cannot make anchorRiseMinPct.
    const int strictRequiredSamples = config_.ownershipProofTwoFrame ? 2 : 3;
    const int requiredSamples = strictOwnershipProof ? strictRequiredSamples : 2;
    return inputQualified && roseEnough
        && pendingMeterOwnership_.sampleCount >= requiredSamples;
}

void AutomationEngine::seedPromotedMeterOwnershipEpisode()
{
    if (!pendingMeterOwnership_.active) {
        return;
    }
    const DetectionResult& first = pendingMeterOwnership_.first;
    const double firstMs = pendingMeterOwnership_.firstMs;
    shot_.detectionPresence = QStringLiteral("accepted");
    shot_.detectorSource = first.detectorSource;
    shot_.fillPct = first.fillPct;
    shot_.confidence = first.confidence;
    shot_.velocityPctS = first.velocityPctS;
    shot_.accelerationPctS2 = first.accelerationPctS2;
    shot_.frameAgeMs = first.frameAgeMs;
    shot_.greenStartPct = first.greenStartPct;
    shot_.greenEndPct = first.greenEndPct;
    shot_.greenCenterPct = first.greenCenterPct;
    shot_.etaToGreenMs = first.etaToGreenMs;
    shot_.consecutiveFrames = std::max(1, first.consecutiveFrames);
    shot_.meterDetected = true;
    shot_.lastDetectionMs = firstMs;
    shot_.lastSampleFreshAccept = true;
    shot_.lastSampleGenuineAccept = true;
    shot_.gameplayStructureVerified = first.gameplayStructureVerified;
    shot_.gameplayStructureEpoch = first.gameplayStructureEpoch;
    shot_.firstFreshAcceptMs = firstMs;
    shot_.lastFreshAcceptMs = firstMs;
    shot_.pushFreshAccept(firstMs);
    shot_.detSamplesTotal = 1;
    shot_.detFreshAccepts = 1;
    shot_.sawFreshMeterThisShot = true;
    shot_.minFreshFillPct = first.fillPct;
    shot_.peakFillPct = first.fillPct;
    shot_.peakFillMs = firstMs;
    shot_.anchorCandFirstMs = firstMs;
    shot_.anchorCandFirstFill = first.fillPct;
    shot_.anchorCandFirstFrameAgeMs = first.frameAgeMs;
    shot_.anchorCandLastMs = firstMs;
    shot_.anchorCandLastFill = first.fillPct;
    const double firstCaptureMs = std::isfinite(first.frameAgeMs)
        && first.frameAgeMs >= 0.0 && first.frameAgeMs <= 250.0
        ? firstMs - first.frameAgeMs : firstMs;
    sampler_.addSample(first.fillPct, firstCaptureMs);
    notePhaseAnchorSample(first.fillPct, firstCaptureMs);
    if (first.greenStartPct >= 0.0 && first.greenEndPct >= 0.0) {
        greenTracker_.update(first.greenStartPct, first.greenEndPct, first.confidence);
    }
    if (config_.templateArrivalEnabled && !autonomousLiveMeterTimingEnabled()) {
        templateArrival_.addSample(first.fillPct, firstMs - first.frameAgeMs);
    }
    maxFillThisShot_ = first.fillPct;

    // updateDetection() continues with the promoting (last) payload after this
    // handoff. Seed every validated historical proof frame before it, so the live
    // path owns a three-sample trajectory immediately instead of throwing away the
    // middle sample. This affects estimator evidence only; ownership still required
    // the same current physical epoch, structure proof, identity advance, geometry
    // continuity, confidence, freshness, and material rise above.
    const int historicalCount = std::max(
        0, static_cast<int>(pendingMeterOwnership_.samples.size()) - 1);
    for (int i = 1; i < historicalCount; ++i) {
        const PendingMeterOwnershipSample& pending = pendingMeterOwnership_.samples.at(i);
        const DetectionResult& sample = pending.result;
        const double sampleMs = pending.sampleMs;
        const double captureMs = std::isfinite(sample.frameAgeMs)
            && sample.frameAgeMs >= 0.0 && sample.frameAgeMs <= 250.0
            ? sampleMs - sample.frameAgeMs : sampleMs;
        sampler_.addSample(sample.fillPct, captureMs);
        notePhaseAnchorSample(sample.fillPct, captureMs);
        if (sample.greenStartPct >= 0.0 && sample.greenEndPct >= 0.0) {
            greenTracker_.update(sample.greenStartPct, sample.greenEndPct,
                                 sample.confidence);
        }
        shot_.lastFreshAcceptMs = sampleMs;
        shot_.pushFreshAccept(sampleMs);
        shot_.minFreshFillPct = std::min(shot_.minFreshFillPct, sample.fillPct);
        if (sample.fillPct > shot_.peakFillPct) {
            shot_.peakFillPct = sample.fillPct;
            shot_.peakFillMs = sampleMs;
        }
        maxFillThisShot_ = std::max(maxFillThisShot_, sample.fillPct);
    }
    // The promoting payload is processed normally below, outside the pre-promotion
    // census branch. Account for the whole validated episode once so diagnostics
    // report the true number of accepted frames rather than the historical value 2.
    shot_.detSamplesTotal = pendingMeterOwnership_.samples.size();
    shot_.detFreshAccepts = historicalCount;
}

bool AutomationEngine::lsCancelRequested()
{
    // Only Go-To is cancellable mid-flight. Square-triggered Tempo shares the
    // exact Button ownership contract and cannot be aborted by shot-type drift.
    if (shot_.mode != ShotMode::GoToStick) {
        shot_.lsCancelFrames = 0;
        return false;
    }
    // Keeping this Go-To-only also prevents normal left-stick movement during a
    // Square shot from aborting and re-arming the same edge with reset safety caps.
    const double lx = static_cast<double>(lastPhysical_.leftStickX);
    const double ly = static_cast<double>(lastPhysical_.leftStickY);
    const double leftMag = std::hypot(lx, ly);   // same raw scale classifyShotType uses
    // Go-To is RS-armed; the LS should be neutral, so any sustained deflection = changed mind.
    const bool qualifies = leftMag >= config_.lsCancelThresholdPct;
    if (qualifies) {
        shot_.lsCancelFrames += 1;
    } else {
        shot_.lsCancelFrames = 0;
    }
    return shot_.lsCancelFrames >= config_.lsCancelFrames;
}

void AutomationEngine::beginShot(ShotMode mode, double now, const QString& shotType,
                                 double physicalPressMs)
{
    clearScheduledFire();
    // A new shot starts with no plan. Silent (not cancelAutonomousTipReservation) because a
    // fulfilled reservation from the previous shot is not an anomaly worth a log line; genuine
    // cancellations are already logged at their own abort sites.
    tipReservation_ = AutonomousTipReservation{};
    // [ORION_GREEN_CENTER] the centring offset is per-shot telemetry; re-arm the once-per-shot
    // log so every shot reports the offset it actually flew with.
    greenCenterLoggedOffsetMs_ = -1.0;
    // Live-path bug #1: the ButtonShot/Go-To branch below stamps holdStartFrameAgeMs from
    // shot_.frameAgeMs — but the ShotContext{} reset on the next line zeroes that field first,
    // so the stamp always read 0. The learned hold clock (ptsHoldStart = holdStartMs -
    // holdStartFrameAgeMs, triggerRelease) then ran ~one frame-age (~10-33ms) SHORT, teaching
    // globalHoldToReleaseMs/shotTypeFeedforwardMs short and firing every blind/feedforward
    // release on the hold clock ~a frame EARLY. Capture the pre-reset frame age (kept current
    // by updateDetection, including the idle_overlay path) and restore it after the reset —
    // the same save/restore shape startPumpFake uses for networkOffsetMs.
    const double preResetFrameAgeMs = shot_.frameAgeMs;
    shot_ = ShotContext{};
    pose_ = PoseTimingState{};
    blindSuppressLoggedThisShot_ = false;
    // [ORION_METER_DELAY_LEAD_STARVATION] once-per-shot diagnostic latch.
    meterDelayStarvedLoggedThisShot_ = false;
    // A new ownership begins with no release evidence. Only a COMPLETE press->release cycle
    // observed after this point may later unlatch the cooldown-end suppression. The down half
    // is seeded from this tick's real sample so a Square-driven shot (which arms with the
    // button already held) does not have to wait a poll to re-observe what it just consumed.
    physicalSquareReleaseSeenSinceArm_ = false;
    physicalSquareDownSeenSinceArm_ = lastPhysical_.square();
    shot_.mode = mode;
    ++shotArmCounter_;
    if (shotArmCounter_ == 0) {
        ++shotArmCounter_; // zero is the wire-protocol invalid sentinel
    }
    shot_.armToken = shotArmCounter_;
    // Preserve the actual Square edge for mode-neutral telemetry. Timing clocks
    // remain ownership-anchored below so existing Button calibration is not shifted.
    shot_.armTimestampMs = physicalPressMs >= 0.0 ? physicalPressMs : now;
    shot_.shotType = shotType.isEmpty() ? QStringLiteral("Standstill") : shotType;
    // [ORION_TEMPO_FADE_MIRROR] Decide the gesture ONCE, here, from the arm-time
    // classification. Later reclassification must not reverse a held stick.
    shot_.tempoFadeGesture =
        tempoGestureIsFade(shot_.shotType, config_.tempoFadeMirrorGesture);
    shot_.latencyCalibrationProbe = autonomousLiveMeterTimingEnabled()
        && latencyCalibrationMode_;
    shot_.latencyCalibrationAutomaticProbe = shot_.latencyCalibrationProbe
        && latencyCalibrationAutomatic_;
    shot_.physicalShotEpoch = physicalShotEpoch_;
    if (autonomousLiveMeterTimingEnabled() && physicalShotEpoch_ != 0) {
        // Ownership consumes the controller's global shot identity across every
        // input family.  A later delayed proof for this epoch can never migrate
        // from Square to stick (or vice versa) after the rearm fence clears.
        retiredPhysicalShotEpoch_ = std::max(
            retiredPhysicalShotEpoch_, physicalShotEpoch_);
    }
    // Hardware-arm the reader and, in the lab configuration, the pose detector
    // exactly once per shot. Meter mode still consumes this signal for its physical
    // shot gate. The token is generated after both per-shot structs reset, so a
    // delayed landmark cannot inherit authority from a previous shot.
    emit shotArmed(shot_.mode, shot_.shotType, shot_.armToken);
    // Tempo is output shaping only: Button and Tempo share one timing namespace.
    shot_.bucketKey = timingKey(shot_.shotType, mode);
    // Tempo movement intent belongs to the physical gesture edge, not the later
    // evidence-promotion callback. A transient centered controller poll while the
    // meter proof accumulates must not turn a fade into a standstill/step-back.
    if (mode == ShotMode::TempoSquare && pendingSquareMovementValid_) {
        shot_.lsArmX = pendingSquareLsArmX_;
        shot_.lsArmY = pendingSquareLsArmY_;
    } else if (mode == ShotMode::TempoStick && pendingTempoStickMovementValid_) {
        shot_.lsArmX = pendingTempoStickLsArmX_;
        shot_.lsArmY = pendingTempoStickLsArmY_;
    } else {
        // Retained for direct/non-pending callers and Go-To intent attribution.
        shot_.lsArmX = static_cast<double>(lastPhysical_.leftStickX);
        shot_.lsArmY = static_cast<double>(lastPhysical_.leftStickY);
    }
    if (mode == ShotMode::TempoSquare || mode == ShotMode::TempoStick) {
        // The committed vector now belongs to the immutable ShotContext. Retire
        // the pre-ownership transaction so a delayed delivery callback cannot
        // mutate an already-owned shot.
        resetTempoMovementTransaction();
    }
    // Sample-and-hold latch: take the freshest measured network offset NOW and hold it for
    // the whole shot. A type with a LOCK baseline follows only BOUNDED drift around it —
    // the locked clock was calibrated against that baseline, so a spike past the clamp is
    // treated as transient instead of moving the deadline. Wifi mode tightens the clamp.
    const double clampMs = wifiMode_ ? config_.wifiRttDeltaClampMs : config_.rttDeltaClampMs;
    double heldOffset = pendingNetworkOffsetMs_;
    // The canonical bucket is shared by Button and Tempo.
    const QString rttKey = config_.shotTypeRttBaselineMs.contains(shot_.bucketKey)
        ? shot_.bucketKey
        : (config_.shotTypeRttBaselineMs.contains(shot_.shotType) ? shot_.shotType : QString());
    if (!rttKey.isEmpty()) {
        const double baseline = config_.shotTypeRttBaselineMs.value(rttKey);
        heldOffset = baseline + std::clamp(pendingNetworkOffsetMs_ - baseline, -clampMs, clampMs);
    }
    shot_.networkOffsetMs = heldOffset;
    shot_.wifiMode = wifiMode_;
    shot_.networkJitterMs = networkJitterEmaMs_;
    shot_.releaseReason = QStringLiteral("Shot owned by bot");
    sampler_.reset();
    greenTracker_.reset();
    // [ORION_FUSED_FIRE] fresh posterior per shot; the anchor arrives with the validated
    // meter-appear episode in processHolding.
    fused_.reset();
    lastFusedSampleMs_ = -1.0;
    lastFusedFillPct_ = -1.0;
    fusedFireAtMs_ = -1.0;
    fusedPeakLatched_ = false;
    fusedFrameAgeEmaMs_ = -1.0;
    maxFillThisShot_ = 0.0;
    // [ORION_TEMPLATE_ARRIVAL] H4: fold the PREVIOUS shot's completed crossing-vector into the
    // per-bucket templates (online cluster learning), then open this shot's vector. Press-t0
    // (H5): the user's shoot-press timestamp is already native — squareHoldStartMs_ was stamped
    // on the rising physical hold, ~100-300ms before the meter appears — so the template SELECT
    // gains the press->first-crossing feature when [ORION_PRESS_T0] is on.
    if (config_.templateArrivalEnabled && !autonomousLiveMeterTimingEnabled()) {
        templateArrival_.endShot();
        const double pressT0 = (config_.pressT0Enabled && physicalPressMs > 0.0)
            ? physicalPressMs : -1.0;
        templateArrival_.beginShot(shot_.bucketKey, pressT0);
    }
    if (!shot_.latencyCalibrationProbe) {
        shotsAttempted_++;
    }

    // Orion 13.1 — pre-reset frame age (see the save above the ShotContext{} reset):
    // shot_.frameAgeMs is 0 here because beginShot just reset the context. Stamped for EVERY
    // mode now: the Armed modes used to re-stamp it in processArmed from shot_.frameAgeMs,
    // which is whatever the Armed dwell happened to leave there (0 when no detection landed).
    // The learned hold clock (ptsHoldStart = holdStartMs - holdStartFrameAgeMs) is the same
    // quantity for every mode, so it must be captured at the same instant for every mode.
    shot_.holdStartFrameAgeMs = preResetFrameAgeMs;

    // Skip the Armed wait phase for committed shots.
    // The user already held the button through the arm delay, proving intent. Waiting for N
    // stable detection frames on top of that just adds latency and can stall the release past
    // the green window.
    //
    // TEMPO PARITY (2026-07-24, owner: "tempo should work the same way button mode works").
    // TempoSquare joins the direct-to-Holding branch. It is the SAME trigger (a held Square) and
    // the SAME meter-timed release; only the OUTPUT shape differs (RS gather + flick). Routing it
    // through Armed bought nothing and cost everything: HoldState::Armed has no release path at
    // all, so until a detection confirmed (or tempoFallbackTimeoutMs — up to 650ms — expired)
    // the shot was in a release BLACKOUT. A meter that appeared and greened inside that window
    // could not be timed.
    // [ORION_TEMPO_PARITY] TempoStick joins the direct-to-Holding branch too, but ONLY under
    // the flag AND only on the autonomous live-meter path. There, ownership is granted solely
    // by the strict 3-frame rising proof (processIdle's stick lane returns before its legacy
    // beginShot whenever liveMeterOnly holds), so the Armed detection-confirm dwell re-proves
    // strictly weaker evidence than what promotion just required — while being a release
    // BLACKOUT that spends ~1-3 detector frames of the measured 93ms decision budget. That is
    // the same reasoning that moved TempoSquare on 2026-07-24. The legacy lane (ownership
    // without meter proof) keeps Armed: there the dwell is the meter confirmation.
    const bool tempoStickParityDirect = mode == ShotMode::TempoStick
        && config_.tempoTipParity && autonomousLiveMeterTimingEnabled();
    if (mode == ShotMode::ButtonShot || mode == ShotMode::GoToStick
            || mode == ShotMode::TempoSquare || tempoStickParityDirect) {
        shot_.state = HoldState::Holding;
        // Ownership is the shared timing zero. The physical Square edge remains in
        // armTimestampMs for telemetry/template matching.
        shot_.holdStartMs = now;
    } else {
        // TempoStick still needs the Armed detection-confirm dwell (its RS-down gather has no
        // physical-hold arm window to prove intent). Its hold clock anchors at the press-commit,
        // which is exactly the value processArmed used to stamp there (armTimestampMs == now);
        // stamping it here keeps ONE stamp site for every mode.
        shot_.state = HoldState::Armed;
        shot_.holdStartMs = now;
    }
    emit shotStateChanged(shot_);
}

void AutomationEngine::latchSuppressedShotControlOverlap(const ControllerState& physical)
{
    const bool ownsShot = shot_.state == HoldState::Armed
        || shot_.state == HoldState::Holding
        || shot_.state == HoldState::GreenWindow
        || shot_.state == HoldState::Releasing
        || shot_.state == HoldState::Cooldown;
    if (!ownsShot) {
        return;
    }

    const bool physicalUp = verticalStickShotIntent(
        physical, true, config_.stickUpThreshold, config_.gotoLateralMaxRatio);
    const bool physicalDown = verticalStickShotIntent(
        physical, false, config_.stickDownThreshold, config_.gotoLateralMaxRatio);

    bool suppressedStickOverlap = false;
    switch (shot_.mode) {
    case ShotMode::ButtonShot:
        // ButtonShot does not override the physical right stick.
        break;
    case ShotMode::GoToStick:
        // RS-up is this shot's own gesture. Square and a new RS-down gesture
        // are both hidden by Go-To's owned output.
        if (physical.square()) {
            squareLatchedUntilRelease_ = true;
        }
        suppressedStickOverlap = physicalDown;
        break;
    case ShotMode::TempoStick:
        // RS-down is this shot's own gesture. Square and a new RS-up gesture
        // are hidden by the tempo gather/flick output.
        if (physical.square()) {
            squareLatchedUntilRelease_ = true;
        }
        suppressedStickOverlap = physicalUp;
        break;
    case ShotMode::TempoSquare:
        // Square is this shot's own gesture; either physical vertical-stick
        // gesture is hidden beneath the generated tempo gather/flick.
        suppressedStickOverlap = physicalUp || physicalDown;
        break;
    }

    if (suppressedStickOverlap && !stickOverlapLatchedUntilNeutral_) {
        stickOverlapLatchedUntilNeutral_ = true;
        rightStickNeutralFrames_ = 0;
    }
}

void AutomationEngine::startPumpFake(ShotMode mode, double now)
{
    const double offset = shot_.networkOffsetMs;
    shot_ = ShotContext{};
    shot_.mode = mode;
    shot_.state = HoldState::PumpFake;
    shot_.armTimestampMs = now;
    shot_.releaseTriggerMs = now;
    shot_.networkOffsetMs = offset;
    sampler_.reset();
    greenTracker_.reset();
    emit shotStateChanged(shot_);
}

void AutomationEngine::processIdle(ControllerState& output, const ControllerState& physical, double now)
{
    // [ORION_PROBE] warmup pump-fake probe injection — engine Idle only. The press is virtual
    // OUTPUT only (physical untouched), so the engine can never arm a shot off it; detection of
    // the spawned meter lands as idle_overlay (never seeds timing). Any physical Square press
    // cancels the run instantly (the user owns the controller).
    //
    // The reader's shot-gate is armed by the probeMarker handler in OrionAppController, NOT here.
    // This comment previously claimed "the sidecar's CV self-arm still arms the READER's
    // shot-gate so the early rise is acquired". That was false by construction -- the CV self-arm
    // writes only the merged deadline and never _shot_gate_hw_deadline, and it is circular anyway
    // (it needs result.detected, which needs read(), which needs the hw gate). Because the claim
    // read as a design guarantee, nobody checked it, and the probe feature could not have worked
    // on any machine from the day it was written.
    if (latencyProbesActive()) {
        probeTick(output, physical, now);
    }
    // RAW input for detection thresholds (instant response).
    // HISTORY consistency for release detection (filters 1-frame jitter from
    // controller polling or Remote Play frame drops).
    const bool stickActive = stickTempoArmAllowed() && stickShotActive(physical);
    const bool stickUp = verticalStickShotIntent(
        physical, true, config_.stickUpThreshold, config_.gotoLateralMaxRatio);
    const bool squareHeld = squareInputAllowed() && physical.square();

    // Strict Go-To intent: a Go-To must arm ONLY on a sustained, vertical-DOMINANT
    // RS-up with no competing shot/dribble context — otherwise a diagonal RS, a dribble
    // (left stick + RS), a sprint (R2) or post (L2) move, or a Square shot would be
    // hijacked into a Go-To. When these fail, the RS-up FALLS THROUGH untouched so the
    // dribble/move plays normally.
    const double rsX = static_cast<double>(physical.rightStickX);
    const double rsY = static_cast<double>(physical.rightStickY);
    const bool stickUpStrict = stickUp;
    const double gotoLeftMag = std::hypot(static_cast<double>(physical.leftStickX),
                                          static_cast<double>(physical.leftStickY));
    const bool gotoContextClear = !squareHeld && !squareLatchedUntilRelease_
        && !physical.cross()
        && gotoLeftMag < config_.movingSquareThreshold
        && physical.l2 < 150 && physical.r2 < 150;
    const bool liveMeterOnly = autonomousLiveMeterTimingEnabled();
    const bool liveLeadReady = !liveMeterOnly || measuredLeadAuthoritative(now);

    // Jitter-filtered release detection: only treat the button as "released"
    // when ALL frames in the 3-frame history agree it's not pressed. Same idea
    // for sticks. This is the proper place to use the smoothed/history values.
    auto allFalse = [](const std::deque<bool>& q) {
        if (q.size() < 3) return false;
        for (bool v : q) if (v) return false;
        return true;
    };
    const bool squareTrulyReleased = !physical.square() && allFalse(shot_.xButtonHistory);

    if (tempoPassThroughPulseActive_) {
        if (squareHeld) {
            // A new physical edge supersedes the previous pass-through pulse.
            tempoPassThroughPulseActive_ = false;
            tempoPassThroughPulseEndMs_ = -1.0;
            resetTempoMovementTransaction();
        } else if (now < tempoPassThroughPulseEndMs_) {
            applyShotReleaseEdge(output, ShotMode::TempoSquare, pendingSquareShotType_,
                         tempoGestureIsFade(pendingSquareShotType_, config_.tempoFadeMirrorGesture));
            preserveFadeMovementVector(
                output, pendingSquareShotType_, pendingSquareLsArmX_,
                pendingSquareLsArmY_, pendingSquareMovementValid_);
            shot_.releasePlan = QStringLiteral("Idle / tempo pass-through flick");
            shot_.releaseReason = QStringLiteral("tempo_remap_passthrough");
            return;
        } else {
            tempoPassThroughPulseActive_ = false;
            tempoPassThroughPulseEndMs_ = -1.0;
            pendingSquareTempoRemap_ = false;
            clearPendingSquareMovementContext();
            resetTempoMovementTransaction();
        }
    }

    if (squareTrulyReleased) {
        squareLatchedUntilRelease_ = false;
        squareRearmBlockedUntilRelease_ = false;
    }
    if (tempoMovement_.mode == ShotMode::TempoSquare
        && tempoMovement_.phase != TempoMovementPhase::None
        && tempoMovement_.phase != TempoMovementPhase::Active
        && squareTrulyReleased) {
        // The trigger ended before its ordered movement commit completed. No RS
        // edge was emitted, so cancel rather than inventing a late Tempo flick.
        resetTempoMovementTransaction();
        pendingSquareTempoRemap_ = false;
    }
    if (tempoMovement_.mode == ShotMode::TempoStick
        && tempoMovement_.phase != TempoMovementPhase::None
        && tempoMovement_.phase != TempoMovementPhase::Active
        && !stickActive) {
        resetTempoMovementTransaction();
        stickDownHoldStartMs_ = -1.0;
        clearPendingTempoStickMovementContext();
        shot_.releasePlan = QStringLiteral("Idle / physical Tempo intent ended before commit");
        shot_.releaseReason = QStringLiteral("tempo_movement_commit_cancelled");
        return;
    }
    if (squareRearmBlockedUntilRelease_ && physical.square()) {
        // A pre-ownership latency gate never seizes the press. Do not reinterpret
        // the same held edge as a new automated shot; Button remains physical and
        // Tempo retains its configured pass-through representation until release.
        if (pendingSquareTempoRemap_) {
            forceTempoSquareGather(output);
        }
        shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
        shot_.releaseReason = QStringLiteral("waiting_for_new_button_press");
        return;
    }
    if (squareLatchedUntilRelease_ && physical.square()) {
        output.buttons &= ~XINPUT_GAMEPAD_X;
        shot_.releasePlan = QStringLiteral("Idle / pass-through");
        shot_.releaseReason = QStringLiteral("waiting_for_button_release");
        return;
    }
    // Arming counter resets the instant strict RS-up is no longer present (unchanged).
    if (!stickUp) {
        stickUpFrames_ = 0;
    }
    // Go-To neutral latch: after a Go-To arms it stays latched until the PHYSICAL right
    // stick is TRULY neutral (magnitude below the deadzone) for 3 consecutive frames.
    // !stickUp is NOT neutral — a diagonal/sideways RS, or a 1-frame dropout as the stick
    // recenters, is !stickUp yet still deflected, and clearing on it would let that
    // post-shot RS re-arm a Go-To or leak a dribble. While latched and the RS is deflected
    // in ANY direction, pin virtual RS to (0,0) and block re-arm (the arm gate at PRIORITY
    // 1 also checks !stickUpLatchedUntilNeutral_). Mirrors the Square allFalse(3) filter.
    // Cross-direction latch clear (TempoStick<->Go-To, do #4): a stick pushed CLEARLY in the OPPOSITE
    // direction to the one that set a hold latch is an unambiguous NEW gesture, not the lingering
    // deflection the neutral latch guards against. The neutral-only clears below never fire when the
    // stick goes STRAIGHT from RS-up (Go-To) to RS-down (tempo) — or back — without passing through
    // center, so a stale up-latch would early-return below and strand the follow-up RS-down tempo
    // (and a stale down-latch likewise). Clear the opposite-direction latch here so the two RS
    // gestures can follow each other. (`stickActive` already captures the "clearly down" arm input.)
    const bool rsClearlyDown = rsY >= rawStickThreshold(config_.stickDownThreshold);
    if (rsClearlyDown) {
        stickUpLatchedUntilNeutral_ = false;
    }
    if (stickUp) {
        stickDownLatchedUntilNeutral_ = false;
        clearPendingTempoStickMovementContext();
    }
    const bool rsNeutral = std::hypot(rsX, rsY) < rawStickThreshold(config_.stickUpThreshold);
    auto clearPendingStickCandidate = [this]() {
        const ShotMode cancelledMode = pendingStickCalibrationMode_;
        retiredPhysicalShotEpoch_ = std::max(
            retiredPhysicalShotEpoch_, pendingStickCalibrationPhysicalEpoch_);
        clearPendingMeterOwnershipEpisode();
        clearPendingMeterOwnershipEvidence();
        clearPendingStickCalibration();
        clearPendingTempoStickMovementContext();
        if (cancelledMode == ShotMode::TempoStick) {
            // This transaction belongs to the terminal physical epoch. Leaving
            // it Active would let the next post-neutral epoch inherit its old
            // start time and instantly exhaust the ownership-proof budget.
            resetTempoMovementTransaction();
        }
        stickUpHoldStartMs_ = -1.0;
        stickDownHoldStartMs_ = -1.0;
        stickUpFrames_ = 0;
    };
    auto reportPendingStickFault = [this](const QString& reason, double waitMs) {
        emit engineDiagnostic(QStringLiteral(
            "SHOT NOT OWNED: reason=%1 mode=%2 samples=%3 first_fill=%4 "
            "last_fill=%5 physical_epoch=%6 wait_ms=%7")
                                  .arg(reason)
                                  .arg(static_cast<int>(pendingStickCalibrationMode_))
                                  .arg(pendingMeterOwnershipMaxProofSamples_)
                                  .arg(pendingMeterOwnershipFirstEvidenceFillPct_, 0, 'f', 1)
                                  .arg(pendingMeterOwnershipLastEvidenceFillPct_, 0, 'f', 1)
                                  .arg(pendingStickCalibrationPhysicalEpoch_)
                                  .arg(waitMs, 0, 'f', 1));
        // [ORION_ABORT_IDENTITY] Pre-ownership stick fault (ownership_epoch_superseded /
        // ownership_proof_incomplete). The pending epoch is still live here — clearPendingStick-
        // Candidate() runs only after this lambda returns — so the abort is genuinely joinable.
        // [ORION_ABORT_SHOT_TYPE] The pending stick candidate is still live here (see above), so
        // its classification -- "Go-To" for a raw stick shot, the tempo bucket for a tempo one --
        // is the genuine type for this attempt. Empty until the candidate has been classified.
        emitShotAbortIdentity("pending_stick_fault",
                              static_cast<qint64>(pendingStickCalibrationPhysicalEpoch_),
                              -1, -1, -1, reason, pendingStickCalibrationShotType_);
        emit shotAborted(reason);
        shot_.releasePlan = QStringLiteral("Idle / physical stick pass-through");
        shot_.releaseReason = reason;
        shot_.releaseReasonCode = reason;
    };

    if (pendingStickCalibrationActive_
        && pendingStickCalibrationPhysicalEpoch_ != physicalShotEpoch_) {
        reportPendingStickFault(QStringLiteral("ownership_epoch_superseded"),
                                now - pendingStickCalibrationStartMs_);
        stickRearmBlockedUntilNeutral_ = true;
        clearPendingStickCandidate();
        return;
    }
    if (pendingStickCalibrationActive_
        && !physicalGestureActiveForMode(pendingStickCalibrationMode_, physical)) {
        // Pre-ownership output is byte-for-byte physical. The first raw inactive
        // sample therefore already exposed a real release/changed-direction edge to
        // the console; this epoch can never be claimed later even if RS bounces back.
        if (pendingMeterOwnershipCurrentEvidenceSeen_) {
            reportPendingStickFault(QStringLiteral("ownership_proof_incomplete"),
                                    now - pendingStickCalibrationStartMs_);
        }
        stickRearmBlockedUntilNeutral_ = true;
        clearPendingStickCandidate();
        return;
    }
    if (rsNeutral) {
        if (++rightStickNeutralFrames_ >= 3) {
            stickUpLatchedUntilNeutral_ = false;
            stickDownLatchedUntilNeutral_ = false;
            stickOverlapLatchedUntilNeutral_ = false;
            stickRearmBlockedUntilNeutral_ = false;
            clearPendingTempoStickMovementContext();
        }
    } else {
        rightStickNeutralFrames_ = 0;
    }
    if (physical.cross() && stickUpStrict) {
        // Cross + RS-up is a call-for-ball/pass context, never a Go-To edge.  Retire
        // the current controller epoch and require a real neutral stick before a
        // later RS-up can be considered.  Without this engine-local fence, releasing
        // Cross while the same stick deflection remained held could start the Go-To
        // dwell even though the controller-side edge tracker correctly emitted no
        // new shot epoch.
        retiredPhysicalShotEpoch_ = std::max(
            retiredPhysicalShotEpoch_, physicalShotEpoch_);
        stickRearmBlockedUntilNeutral_ = true;
        stickUpFrames_ = 0;
        stickUpHoldStartMs_ = -1.0;
    }
    if (stickOverlapLatchedUntilNeutral_) {
        // A vertical stick gesture hidden beneath another owned shot must not
        // surface as a brand-new shot edge after cooldown. Keep it neutral and
        // require three genuine neutral samples before any shot input may re-arm.
        output.rightStickX = 0;
        output.rightStickY = 0;
        stickUpFrames_ = 0;
        stickUpHoldStartMs_ = -1.0;
        stickDownHoldStartMs_ = -1.0;
        shot_.releasePlan = QStringLiteral("Idle / overlap latch");
        shot_.releaseReason = QStringLiteral("waiting_for_suppressed_stick_release");
        return;
    }
    if (stickRearmBlockedUntilNeutral_ && !rsNeutral) {
        // Same rule as Square: pass the physical stick through unchanged while
        // requiring a stable neutral edge before automation may own it again.
        stickUpFrames_ = 0;
        stickUpHoldStartMs_ = -1.0;
        stickDownHoldStartMs_ = -1.0;
        shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
        shot_.releaseReason = QStringLiteral("waiting_for_new_stick_gesture");
        return;
    }
    if (stickDownLatchedUntilNeutral_) {
        // TempoStick's physical RS-down may remain held well past the bot's
        // RS-up release and cooldown. Keep the virtual stick neutral until the
        // canonical three-neutral debounce proves that gesture ended; leaking
        // raw DOWN here creates a second gather edge from one physical hold.
        output.rightStickX = 0;
        output.rightStickY = 0;
        stickUpFrames_ = 0;
        stickUpHoldStartMs_ = -1.0;
        stickDownHoldStartMs_ = -1.0;
        shot_.releasePlan = QStringLiteral("Idle / TempoStick release latch");
        shot_.releaseReason = QStringLiteral("waiting_for_tempo_stick_neutral");
        return;
    }
    if (stickUpLatchedUntilNeutral_ && !rsNeutral) {
        output.rightStickX = 0;
        output.rightStickY = 0;
        return;
    }

    // PRIORITY 1: Go-to stick — STRICT, vertical-dominant RS-up sustained across
    // gotoArmFrames + gotoHoldArmMs (not a flick), with NO competing context (Square
    // held/owned, left-stick move/fade, R2 sprint, L2 post). A diagonal/left/right RS,
    // a dribble, or a Square shot therefore does NOT arm Go-To — the RS-up just falls
    // through and passes the dribble/move straight to the console. (This deliberately
    // gives Square/move shots precedence over Go-To, the inverse of the old behaviour.)
    if (config_.gotoEnabled && stickUpStrict && gotoContextClear && !stickUpLatchedUntilNeutral_) {
        if (liveMeterOnly && physicalShotEpoch_ != 0
            && physicalShotEpoch_ <= retiredPhysicalShotEpoch_) {
            stickRearmBlockedUntilNeutral_ = true;
            stickUpFrames_ = 0;
            stickUpHoldStartMs_ = -1.0;
            shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
            shot_.releaseReason = QStringLiteral("waiting_for_new_stick_epoch");
            return;
        }
        const bool pendingLiveGoTo = pendingStickCalibrationActive_
            && pendingStickCalibrationMode_ == ShotMode::GoToStick;
        if (liveMeterOnly || pendingLiveGoTo) {
            if (!pendingLiveGoTo) {
                if ((!liveLeadReady && !latencyCalibrationMode_)
                    || physicalShotEpoch_ == 0) {
                    // No calibration intent or no controller-origin epoch means there is no
                    // admissible way to associate future pixels with this gesture.
                    stickRearmBlockedUntilNeutral_ = true;
                    stickUpFrames_ = 0;
                    stickUpHoldStartMs_ = -1.0;
                    shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
                    shot_.releaseReason = QStringLiteral("waiting_for_latency_calibration");
                    return;
                }
                clearPendingMeterOwnershipEpisode();
                clearPendingMeterOwnershipEvidence();
                pendingStickCalibrationActive_ = true;
                pendingStickCalibrationMode_ = ShotMode::GoToStick;
                pendingStickCalibrationStartMs_ = now;
                pendingStickCalibrationPhysicalEpoch_ = physicalShotEpoch_;
                pendingStickCalibrationArmGeneration_ =
                    armRevocationGeneration_.load(std::memory_order_acquire);
                pendingStickCalibrationShotType_ = QStringLiteral("Go-To");
                stickUpHoldStartMs_ = now;
                stickUpFrames_ = 0;
            }
            ++stickUpFrames_;
            const double proofWaitMs = now - pendingStickCalibrationStartMs_;
            const double proofWaitCapMs = std::max(
                1.0, config_.gotoMeterWait
                    ? std::max(config_.gotoNoMeterAbortMs, config_.gotoMeterWaitCapMs)
                    : config_.gotoNoMeterAbortMs);
            if (proofWaitMs >= proofWaitCapMs) {
                reportPendingStickFault(QStringLiteral("ownership_proof_timeout"), proofWaitMs);
                stickRearmBlockedUntilNeutral_ = true;
                clearPendingStickCandidate();
                return;
            }
            // The exact physical RS-up remains on the wire for both cold
            // calibration and lead-ready production. Latency readiness selects
            // probe-vs-production behavior only after the same strict proof; it
            // never substitutes elapsed dwell for proof that a meter exists.
            shot_.releasePlan = latencyCalibrationMode_
                ? QStringLiteral("Idle / automatic calibration pass-through")
                : QStringLiteral("Idle / physical awaiting meter ownership proof");
            shot_.releaseReason = latencyCalibrationMode_
                ? QStringLiteral("waiting_for_automatic_calibration_meter")
                : QStringLiteral("waiting_for_genuine_meter_before_ownership");
            return;
        }
        stickUpFrames_++;
        if (stickUpHoldStartMs_ < 0.0) {
            stickUpHoldStartMs_ = now;
        }
        shot_.releasePlan = QStringLiteral("Idle / pass-through");
        shot_.releaseReason = QStringLiteral("waiting_for_strict_hold");
        if (stickUpFrames_ >= config_.gotoArmFrames && (now - stickUpHoldStartMs_) >= config_.gotoHoldArmMs) {
            stickUpHoldStartMs_ = -1.0;
            stickUpLatchedUntilNeutral_ = true;
            // Cancel any pending square/stick-down timers so they can't fire pump fakes.
            squareHoldStartMs_ = -1.0;
            stickDownHoldStartMs_ = -1.0;
            beginShot(ShotMode::GoToStick, now, classifyShotType(physical, ShotMode::GoToStick));
            forceHeldOutput(output);
        }
        return;
    }

    // PRIORITY 2: Square hold (TempoSquare or moving ButtonShot)
    if (squareHeld && !squareLatchedUntilRelease_) {
        if (liveMeterOnly && physicalShotEpoch_ != 0
            && physicalShotEpoch_ <= retiredPhysicalShotEpoch_) {
            squareRearmBlockedUntilRelease_ = true;
            shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
            shot_.releaseReason = QStringLiteral("waiting_for_new_button_epoch");
            return;
        }
        const bool newSquarePress = squareHoldStartMs_ < 0.0;
        // Classification and fade movement are physical-edge identity. Do not
        // continuously reclassify a held gesture from later controller polls:
        // a one-frame centered LS report may arrive while detector proof is
        // pending, but it is not a new shot intent.
        const QString provType = classifyShotType(physical, ShotMode::ButtonShot);
        if (newSquarePress) {
            squareHoldStartMs_ = now;
            pendingSquarePhysicalEpoch_ = physicalShotEpoch_;
            pendingSquareArmGeneration_ = armRevocationGeneration_.load(
                std::memory_order_acquire);
            clearPendingMeterOwnershipEpisode();
            clearPendingMeterOwnershipEvidence();
            pendingSquareShotType_ = provType;
            pendingSquareLsArmX_ = static_cast<double>(physical.leftStickX);
            pendingSquareLsArmY_ = static_cast<double>(physical.leftStickY);
            pendingSquareMovementValid_ = true;
        }
        // Classification remains timing metadata; only the global Tempo toggle
        // selects Button versus Tempo output representation. It never changes
        // the generated gather/flick direction during a physical press.
        // In automatic cold-start calibration the press stays byte-for-byte physical until a
        // strict rising meter proves this is a shot. Explicit calibration and normal ready play
        // retain the configured Tempo remap behavior.
        const bool autoCalibrationAwaitingMeter = latencyCalibrationAutomatic_ && !liveLeadReady;
        if (newSquarePress) {
            // Never change Button <-> Tempo representation in the middle of one physical hold.
            // Lead convergence and live shot classification can both change after DOWN; neither
            // is permission to manufacture a second input edge on the console.
            bool wantTempo = !autoCalibrationAwaitingMeter
                && resolveTempoForType(pendingSquareShotType_);
            // [ORION_TEMPO_BRIDGE_LIVE task #36] The remap only intercepts a NEW press while
            // the packet-bridge intercept is provably live; otherwise the press stays a plain
            // Button pass-through so the console always receives it. Decided once per physical
            // edge (mid-hold revocation would manufacture a second input edge — forbidden
            // above), so a service restart mid-gesture drains the current gesture coherently
            // and the gate simply re-evaluates on the next press.
            if (wantTempo && !tempoRemapBridgeLive(now)) {
                emit engineDiagnostic(QStringLiteral(
                    "Tempo remap: passthrough because bridge-not-live (delay_state=%1 applied_ms=%2)")
                                          .arg(tempoBridgeStateLabel_)
                                          .arg(meterDelayAppliedMs_, 0, 'f', 0));
                wantTempo = false;
            }
            pendingSquareTempoRemap_ = wantTempo;
        }
        bool useTempo = pendingSquareTempoRemap_;
        if (useTempo) {
            if (advanceTempoMovementTransaction(
                    output, physical, ShotMode::TempoSquare, now)) {
                shot_.releasePlan = tempoMovementCommitPending()
                    ? QStringLiteral("Idle / Tempo movement commit")
                    : QStringLiteral("Idle / Tempo movement intent acquire");
                shot_.releaseReason = tempoMovementCommitPending()
                    ? QStringLiteral("waiting_for_tempo_movement_commit_ack")
                    : QStringLiteral("stabilizing_tempo_movement_intent");
                return;
            }
            // A failed transaction demotes this physical edge to the regular
            // Button path; Active continues into the normal Tempo gather below.
            useTempo = pendingSquareTempoRemap_;
        }
        if (useTempo) {
            forceTempoSquareGather(output);
        }
        if (liveMeterOnly) {
            const double proofWaitMs = now - squareHoldStartMs_;
            const double proofWaitCapMs = std::max(1.0, config_.buttonNoMeterAbortMs);
            // B1 (2026-08-04, STALE CARRY-OVER METER HOSTAGE). When a press lands while the
            // previous shot's meter is still rendered — or frozen — every structure-verified
            // frame of this epoch sits above the first-sight bound, so no ownership episode can
            // ever open (live batches show `samples=0 first_fill=67.5` and a frozen
            // `first_fill=49.3 last_fill=49.3 wait_ms=2443.4`). Waiting the full
            // buttonNoMeterAbortMs costs the player ~2.8s standing there holding the ball for a
            // proof that provably cannot arrive. End the pending wait early under the strict
            // static-high-fill census and report a DISTINCT reason so the next batch can measure
            // it. The action taken is byte-for-byte the timeout path below: no release is
            // fabricated, no ownership is taken, the physical gesture stays untouched under the
            // player. Fail-closed is therefore unchanged — this only stops waiting sooner.
            const bool staleMeterBlocked =
                pendingMeterOwnershipStaleMeterBlocked(now, proofWaitMs);
            if (proofWaitMs >= proofWaitCapMs || staleMeterBlocked) {
                // There is no safe fabricated release here: Square-DOWN is what causes the game
                // to spawn the meter, so swallowing it would make visual proof impossible and
                // releasing on a clock would be a blind shot. Bound the pending interval, report
                // the non-intervention exactly once, and leave the already-forwarded physical
                // gesture byte-for-byte under the player until a real UP edge.
                const QString notOwnedReason = staleMeterBlocked
                    ? QStringLiteral("ownership_blocked_stale_meter")
                    : QStringLiteral("ownership_proof_timeout");
                emit engineDiagnostic(QStringLiteral(
                    "SHOT NOT OWNED: reason=%1 samples=%2 "
                    "first_fill=%3 last_fill=%4 physical_epoch=%5 wait_ms=%6 "
                    "stale_samples=%7 stale_span_pct=%8")
                                          .arg(notOwnedReason)
                                          .arg(pendingMeterOwnershipMaxProofSamples_)
                                          .arg(pendingMeterOwnershipFirstEvidenceFillPct_, 0, 'f', 1)
                                          .arg(pendingMeterOwnershipLastEvidenceFillPct_, 0, 'f', 1)
                                          .arg(pendingSquarePhysicalEpoch_)
                                          .arg(proofWaitMs, 0, 'f', 1)
                                          .arg(pendingMeterOwnershipStaleSamples_)
                                          .arg(pendingMeterOwnershipStaleMaxPct_
                                                   - pendingMeterOwnershipStaleMinPct_,
                                               0, 'f', 1));
                // [ORION_ABORT_IDENTITY] Square ownership-proof timeout /
                // ownership_blocked_stale_meter. pendingSquarePhysicalEpoch_ is retired and
                // zeroed a few lines below, so it must be read here while still valid.
                // [ORION_ABORT_SHOT_TYPE] pendingSquareShotType_ is the provisional classification
                // taken at the square press (classifyShotType on the arming poll), which is the
                // type this attempt WOULD have been had ownership completed -- and the only one
                // that exists, since the timeout means beginShot() never ran.
                emitShotAbortIdentity("square_proof_timeout",
                                      static_cast<qint64>(pendingSquarePhysicalEpoch_),
                                      -1, -1, -1, notOwnedReason, pendingSquareShotType_);
                emit shotAborted(notOwnedReason);
                squareRearmBlockedUntilRelease_ = true;
                // The timeout is the sole terminal report for this ownership
                // attempt.  Retain the physical/Tempo pass-through bookkeeping,
                // but retire its ownership epoch so a later calibration-off or
                // config transition cannot emit a second cancellation fault.
                retiredPhysicalShotEpoch_ = std::max(
                    retiredPhysicalShotEpoch_, pendingSquarePhysicalEpoch_);
                pendingSquarePhysicalEpoch_ = 0;
                pendingSquareArmGeneration_ = 0;
                clearPendingMeterOwnershipEpisode();
                clearPendingMeterOwnershipEvidence();
                shot_.releasePlan = useTempo
                    ? QStringLiteral("Idle / tempo proof timeout pass-through")
                    : QStringLiteral("Idle / physical proof timeout pass-through");
                shot_.releaseReason = notOwnedReason;
                return;
            }
        }
        if (!liveLeadReady && (!latencyCalibrationMode_ || latencyCalibrationAutomatic_)) {
            if (latencyCalibrationAutomatic_) {
                // Zero-click calibration has no explicit venue signal. Keep the entire physical
                // gesture untouched for as long as necessary; updateDetection may promote it only
                // after the strict unique three-frame rising episode validates real gameplay.
                shot_.releasePlan = QStringLiteral("Idle / automatic calibration pass-through");
                shot_.releaseReason = QStringLiteral("waiting_for_automatic_calibration_meter");
                return;
            }
            // Do not permanently strand a press merely because the lead was unavailable on its
            // first poll. The initial DOWN must reach the console to make the meter exist; keep
            // this exact physical epoch pending so a later authoritative lead plus the existing
            // strict three-frame, structure-verified meter proof can take ownership. Nothing here
            // grants release authority and a menu/no-meter press remains physical.
            shot_.releasePlan = useTempo
                ? QStringLiteral("Idle / tempo awaiting live authority")
                : QStringLiteral("Idle / physical awaiting live authority");
            shot_.releaseReason = QStringLiteral("waiting_for_latency_calibration");
            return;
        }
        // A Tempo Square press is represented as a pure right-stick gesture from
        // frame one, including the pre-ownership window. Button mode preserves
        // physical Square during that same window.
        shot_.releasePlan = QStringLiteral("Idle / pass-through");
        shot_.releaseReason = QStringLiteral("waiting_for_strict_hold");
        if (liveMeterOnly) {
            // Strict production ownership is evidence-driven, never elapsed-hold-
            // driven. Button mode stays physical and Tempo stays in its existing
            // remap representation until three unique, geometry-continuous rising
            // meter frames prove this exact physical-shot epoch. updateDetection()
            // promotes the shot synchronously when that proof arrives. A menu or a
            // detector-starved press therefore cannot become a claimed bot shot.
            shot_.releasePlan = useTempo
                ? QStringLiteral("Idle / tempo awaiting meter ownership proof")
                : QStringLiteral("Idle / physical awaiting meter ownership proof");
            shot_.releaseReason = QStringLiteral(
                "waiting_for_genuine_meter_before_ownership");
            return;
        }
        // Button and Tempo intentionally share this ownership gate. A genuine
        // two-frame meter episode may safely promote either one earlier.
        const double armMs = config_.squareHoldArmMs;
        if ((now - squareHoldStartMs_) >= armMs) {
            const double physicalPressMs = squareHoldStartMs_;
            squareLatchedUntilRelease_ = true;
            // Tempo converts the Square hold into the right-stick tempo motion (TempoSquare). The
            // arm gate + meter-timed release are identical; only forceHeldOutput / processReleasing
            // differ (RS down-load then up-flick).
            const ShotMode squareMode = useTempo ? ShotMode::TempoSquare : ShotMode::ButtonShot;
            beginShot(squareMode, now, pendingSquareShotType_, physicalPressMs);
            squareHoldStartMs_ = -1.0;
            retiredPhysicalShotEpoch_ = std::max(
                retiredPhysicalShotEpoch_, pendingSquarePhysicalEpoch_);
            pendingSquarePhysicalEpoch_ = 0;
            pendingSquareArmGeneration_ = 0;
            clearPendingMeterOwnershipEpisode();
            clearPendingMeterOwnershipEvidence();
            forceHeldOutput(output);
            pendingSquareTempoRemap_ = false;
            clearPendingSquareMovementContext();
        }
        return;
    }

    // Square early release passes through as the user's real tap or pump fake.
    // The bot only owns Square after the hold threshold is confirmed.
    if (squareTrulyReleased && squareHoldStartMs_ >= 0.0 && !squareLatchedUntilRelease_) {
        const bool incompleteStrictProof = liveMeterOnly
            && pendingMeterOwnershipCurrentEvidenceSeen_;
        if (incompleteStrictProof) {
            // We saw current-epoch gameplay meter structure, but never obtained
            // enough unique rising frames to take ownership safely. The physical
            // release still passes through untouched; surface the fail-closed
            // non-intervention so it cannot look like the bot silently ignored a
            // genuine shot. No structure evidence means this may be a menu press
            // and remains deliberately quiet.
            emit engineDiagnostic(QStringLiteral(
                "SHOT NOT OWNED: reason=ownership_proof_incomplete samples=%1 "
                "first_fill=%2 last_fill=%3 physical_epoch=%4 restarts=%5 "
                "break_drop=%6 break_anchor=%7 break_geometry=%8 break_identity=%9 "
                "break_gap=%10")
                                      .arg(pendingMeterOwnershipMaxProofSamples_)
                                      .arg(pendingMeterOwnershipFirstEvidenceFillPct_, 0, 'f', 1)
                                      .arg(pendingMeterOwnershipLastEvidenceFillPct_, 0, 'f', 1)
                                      .arg(pendingSquarePhysicalEpoch_)
                                      .arg(pendingMeterOwnershipBreaks_.restarts)
                                      .arg(pendingMeterOwnershipBreaks_.drop)
                                      .arg(pendingMeterOwnershipBreaks_.anchor)
                                      .arg(pendingMeterOwnershipBreaks_.geometry)
                                      .arg(pendingMeterOwnershipBreaks_.identity)
                                      .arg(pendingMeterOwnershipBreaks_.gapOrCandidate));
            // [ORION_ABORT_IDENTITY] Square early release with incomplete strict proof. This is
            // the single highest-volume unattributed abort in logs/orion_native.log.1 (all 23
            // `ownership_proof_incomplete` aborts in that log's post-identity era had no stamp).
            emitShotAbortIdentity("square_early_release",
                                  static_cast<qint64>(pendingSquarePhysicalEpoch_),
                                  -1, -1, -1,
                                  QStringLiteral("ownership_proof_incomplete"),
                                  pendingSquareShotType_);
            emit shotAborted(QStringLiteral("ownership_proof_incomplete"));
        }
        if (pendingSquareTempoRemap_) {
            squareHoldStartMs_ = -1.0;
            retiredPhysicalShotEpoch_ = std::max(
                retiredPhysicalShotEpoch_, pendingSquarePhysicalEpoch_);
            pendingSquarePhysicalEpoch_ = 0;
            pendingSquareArmGeneration_ = 0;
            clearPendingMeterOwnershipEpisode();
            clearPendingMeterOwnershipEvidence();
            tempoPassThroughPulseActive_ = true;
            tempoPassThroughPulseEndMs_ = now
                + std::max(config_.tempoFlickHoldMs, config_.releasePulseMs);
            applyShotReleaseEdge(output, ShotMode::TempoSquare, pendingSquareShotType_,
                         tempoGestureIsFade(pendingSquareShotType_, config_.tempoFadeMirrorGesture));
            preserveFadeMovementVector(
                output, pendingSquareShotType_, pendingSquareLsArmX_,
                pendingSquareLsArmY_, pendingSquareMovementValid_);
            shot_.releasePlan = QStringLiteral("Idle / tempo pass-through flick");
            shot_.releaseReason = QStringLiteral("tempo_remap_passthrough");
            return;
        }
        squareHoldStartMs_ = -1.0;
        retiredPhysicalShotEpoch_ = std::max(
            retiredPhysicalShotEpoch_, pendingSquarePhysicalEpoch_);
        pendingSquarePhysicalEpoch_ = 0;
        pendingSquareArmGeneration_ = 0;
        clearPendingMeterOwnershipEpisode();
        clearPendingMeterOwnershipEvidence();
        pendingSquareTempoRemap_ = false;
        clearPendingSquareMovementContext();
        shot_.releasePlan = QStringLiteral("Idle / pass-through");
        shot_.releaseReason = QStringLiteral("Idle / pass-through");
    } else if (squareTrulyReleased) {
        squareHoldStartMs_ = -1.0;
        pendingSquarePhysicalEpoch_ = 0;
        pendingSquareArmGeneration_ = 0;
        // The generic proof buffer may belong to a cold TempoStick candidate.
        // TempoStick reaches this Square-UP housekeeping before PRIORITY 3 on
        // every controller tick; clearing the shared episode here erased each
        // detector frame, so RS-down could never accumulate its three-frame
        // ownership proof. Only Square owns this cleanup when no stick candidate
        // is live.
        if (!pendingStickCalibrationActive_) {
            clearPendingMeterOwnershipEpisode();
            clearPendingMeterOwnershipEvidence();
        }
        pendingSquareTempoRemap_ = false;
        clearPendingSquareMovementContext();
    }
    // If square shows as released BUT history isn't consistent, ignore — it's
    // a 1-frame dropout while user is still actually holding the button.
    // The history filter protects the STATE MACHINE from that dropout, but the
    // pass-through OUTPUT must bridge it too: a leaked 1-frame release edge
    // reaches the game as a phantom pump fake that kills the real shot. Bridging
    // costs a true early release at most ~3 ticks (~12 ms) of extra hold.
    if (!squareHeld && !squareTrulyReleased && squareHoldStartMs_ >= 0.0 && squareInputAllowed()) {
        if (pendingSquareTempoRemap_) {
            forceTempoSquareGather(output);
        } else {
            output.buttons |= XINPUT_GAMEPAD_X;
        }
    }

    // PRIORITY 3: raw Stick down (TempoStick). This is independent of the
    // Square Tempo toggle and requires an explicitly allowed stick input source.
    if (stickActive && !stickDownLatchedUntilNeutral_) {
        if (liveMeterOnly && physicalShotEpoch_ != 0
            && physicalShotEpoch_ <= retiredPhysicalShotEpoch_) {
            stickRearmBlockedUntilNeutral_ = true;
            stickDownHoldStartMs_ = -1.0;
            shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
            shot_.releaseReason = QStringLiteral("waiting_for_new_stick_epoch");
            return;
        }
        if (advanceTempoMovementTransaction(
                output, physical, ShotMode::TempoStick, now)) {
            shot_.releasePlan = tempoMovementCommitPending()
                ? QStringLiteral("Idle / TempoStick movement commit")
                : QStringLiteral("Idle / TempoStick movement intent acquire");
            shot_.releaseReason = tempoMovementCommitPending()
                ? QStringLiteral("waiting_for_tempo_movement_commit_ack")
                : QStringLiteral("stabilizing_tempo_movement_intent");
            return;
        }
        if (stickRearmBlockedUntilNeutral_) {
            shot_.releasePlan = QStringLiteral("Idle / physical stick pass-through");
            shot_.releaseReason = QStringLiteral("tempo_movement_commit_failed");
            return;
        }
        // The movement commit is now ordered and settled. Emit the first
        // TempoStick gather explicitly even when this is the CommitDwell->Active
        // transition tick (advanceTempoMovementTransaction intentionally left
        // the output in its LS-only commit shape).
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        // [ORION_TEMPO_FADE_MIRROR] pre-arm gather: no ShotContext latch exists yet, so
        // key off the pending classification. This MUST agree with what the release edge
        // will do — a gather and flick in the same direction is not an edge at all, and
        // the shot would simply never fire.
        output.rightStickY = tempoGestureIsFade(
            pendingTempoStickShotType_, config_.tempoFadeMirrorGesture) ? -127 : 127;
        preservePendingTempoStickMovementContext(output);
        const bool pendingLiveTempo = pendingStickCalibrationActive_
            && pendingStickCalibrationMode_ == ShotMode::TempoStick;
        if (stickDownHoldStartMs_ < 0.0 && !pendingLiveTempo
            && !pendingTempoStickMovementValid_) {
            pendingTempoStickShotType_ = classifyShotType(
                physical, ShotMode::TempoStick);
            pendingTempoStickLsArmX_ = static_cast<double>(physical.leftStickX);
            pendingTempoStickLsArmY_ = static_cast<double>(physical.leftStickY);
            pendingTempoStickMovementValid_ = true;
        }
        if (liveMeterOnly || pendingLiveTempo) {
            if (!pendingLiveTempo) {
                if ((!liveLeadReady && !latencyCalibrationMode_)
                    || physicalShotEpoch_ == 0) {
                    stickRearmBlockedUntilNeutral_ = true;
                    stickDownHoldStartMs_ = -1.0;
                    shot_.releasePlan = QStringLiteral("Idle / physical pass-through");
                    shot_.releaseReason = QStringLiteral("waiting_for_latency_calibration");
                    return;
                }
                clearPendingMeterOwnershipEpisode();
                clearPendingMeterOwnershipEvidence();
                pendingStickCalibrationActive_ = true;
                pendingStickCalibrationMode_ = ShotMode::TempoStick;
                pendingStickCalibrationStartMs_ =
                    tempoMovement_.phase == TempoMovementPhase::Active
                        && tempoMovement_.startedMs >= 0.0
                    ? tempoMovement_.startedMs : now;
                pendingStickCalibrationPhysicalEpoch_ = physicalShotEpoch_;
                pendingStickCalibrationArmGeneration_ =
                    armRevocationGeneration_.load(std::memory_order_acquire);
                pendingStickCalibrationShotType_ = pendingTempoStickShotType_;
                stickDownHoldStartMs_ = pendingStickCalibrationStartMs_;
            }
            const double proofWaitMs = now - pendingStickCalibrationStartMs_;
            const double proofWaitCapMs = std::max(1.0, config_.buttonNoMeterAbortMs);
            if (proofWaitMs >= proofWaitCapMs) {
                reportPendingStickFault(QStringLiteral("ownership_proof_timeout"), proofWaitMs);
                stickRearmBlockedUntilNeutral_ = true;
                clearPendingStickCandidate();
                return;
            }
            // Do not neutralize RS while proof is pending. Lead-ready production
            // and cold calibration both need the real gesture to reach the game
            // before current-epoch pixels can prove ownership.
            shot_.releasePlan = latencyCalibrationMode_
                ? QStringLiteral("Idle / automatic calibration pass-through")
                : QStringLiteral("Idle / physical awaiting meter ownership proof");
            shot_.releaseReason = latencyCalibrationMode_
                ? QStringLiteral("waiting_for_automatic_calibration_meter")
                : QStringLiteral("waiting_for_genuine_meter_before_ownership");
            preservePendingTempoStickMovementContext(output);
            return;
        }
        if (stickDownHoldStartMs_ < 0.0) {
            stickDownHoldStartMs_ =
                tempoMovement_.phase == TempoMovementPhase::Active
                    && tempoMovement_.startedMs >= 0.0
                ? tempoMovement_.startedMs : now;
        }
        // The physical RS-down is already the Tempo gather. Preserve it during
        // the intent dwell so the console sees one continuous DOWN hold; taking
        // ownership later must not manufacture a DOWN->neutral->DOWN sequence.
        if ((now - stickDownHoldStartMs_) >= std::max(config_.stickHoldArmMs, config_.tempoMinStickHoldMs)) {
            const double physicalPressMs = stickDownHoldStartMs_;
            stickDownHoldStartMs_ = -1.0;
            stickDownLatchedUntilNeutral_ = true;
            beginShot(ShotMode::TempoStick, now, pendingTempoStickShotType_,
                      physicalPressMs);
            forceHeldOutput(output);
            clearPendingTempoStickMovementContext();
        }
        preservePendingTempoStickMovementContext(output);
        return;
    }

    if (!stickActive && stickDownHoldStartMs_ >= 0.0 && !stickDownLatchedUntilNeutral_
        && !pendingStickCalibrationActive_) {
        const double heldMs = now - stickDownHoldStartMs_;
        stickDownHoldStartMs_ = -1.0;
        if (heldMs > 8.0 && heldMs < config_.stickHoldArmMs) {
            startPumpFake(ShotMode::TempoStick, now);
            pulsePumpFakeOutput(output);
            clearPendingTempoStickMovementContext();
            resetTempoMovementTransaction();
            return;
        }
        clearPendingTempoStickMovementContext();
        resetTempoMovementTransaction();
    } else if (!stickActive && !pendingStickCalibrationActive_) {
        stickDownHoldStartMs_ = -1.0;
        clearPendingTempoStickMovementContext();
        if (tempoMovement_.mode == ShotMode::TempoStick) {
            resetTempoMovementTransaction();
        }
    }

    if (!stickUp && !(pendingStickCalibrationActive_
                      && pendingStickCalibrationMode_ == ShotMode::GoToStick)) {
        stickUpHoldStartMs_ = -1.0;
    }
}

void AutomationEngine::processArmed(ControllerState& output, double now)
{
    if (shot_.mode == ShotMode::GoToStick && lastPhysical_.cross()) {
        // Cross is the call-for-ball/pass action.  Once it appears, this is no
        // longer an unambiguous Go-To gesture; fail closed and preserve Cross on
        // the physical pass-through output.  abort() retains the stick-neutral
        // re-arm fence for the retired Go-To epoch.
        relinquishAutonomousLiveMeterShot(
            output, QStringLiteral("goto_competing_cross_abort"));
        return;
    }
    if (shot_.latencyCalibrationProbe) {
        // TempoStick is the only proven live-meter mode that retains the Armed
        // detection-confirm phase. Its absolute calibration ownership budget
        // starts at beginShot(), not at the later Armed->Holding transition, so
        // the first controller tick after a stall must relinquish immediately.
        const double heldMs = shot_.holdStartMs > 0.0
            ? now - shot_.holdStartMs : 0.0;
        const double capMs = std::max(
            1.0, shot_.mode == ShotMode::GoToStick
                ? config_.gotoMaxHoldMs : config_.maxHoldMs);
        if (heldMs >= capMs) {
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("latency_calibration_timeout_abort"));
            return;
        }
    }
    if (autonomousLiveMeterTimingEnabled() && !shot_.latencyCalibrationProbe
        && !measuredLeadAuthoritative(now)) {
        relinquishAutonomousLiveMeterShot(output,
                                           QStringLiteral("measured_lead_unready_abort"));
        return;
    }
    forceHeldOutput(output);
    if (lsCancelRequested()) {
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        output.rightStickY = 0;
        abort(QStringLiteral("ls_cancel"));
        return;
    }
    const double elapsed = now - shot_.armTimestampMs;
    if ((shot_.consecutiveFrames >= config_.stableFramesRequired && shot_.confidence >= config_.confidenceGate)
        || elapsed >= config_.tempoFallbackTimeoutMs) {
        shot_.state = HoldState::Holding;
        // D2 (2026-07-18, Armed-phase clock skew): the hold clock anchors at the PRESS-commit,
        // NOT at this detection-confirm instant — stamping it here shifted every hold-anchored
        // clock (feedforward timing, commit floors, timeout, min-hold) by the whole Armed
        // dwell (up to tempoFallbackTimeoutMs) relative to button mode.
        // 2026-07-24: the stamps moved to beginShot (ONE stamp site for every mode, so the
        // anchor cannot depend on which path a mode takes to Holding). beginShot already set
        // holdStartMs = armTimestampMs and holdStartFrameAgeMs = the pre-reset frame age, so
        // this transition now only changes STATE. Reached by TempoStick only — TempoSquare goes
        // direct to Holding (see beginShot).
        emit shotStateChanged(shot_);
    }
}

void AutomationEngine::latchOwnedOutputDrain(OwnedOutputDrain state, ShotMode mode,
                                              const QString& shotType,
                                              double lsArmX, double lsArmY,
                                              bool movementValid)
{
    if (state == OwnedOutputDrain::None) {
        clearOwnedOutputDrain();
        return;
    }
    if (ownedOutputDrain_ == state && ownedOutputDrainMode_ == mode) {
        if (!shotType.isEmpty()) {
            ownedOutputDrainShotType_ = shotType;
        }
        if (movementValid) {
            ownedOutputDrainLsArmX_ = lsArmX;
            ownedOutputDrainLsArmY_ = lsArmY;
            ownedOutputDrainMovementValid_ = true;
        }
        return;
    }
    ownedOutputDrain_ = state;
    ownedOutputDrainMode_ = mode;
    ownedOutputDrainShotType_ = shotType;
    if (!movementValid && (mode == ShotMode::TempoSquare
                           || mode == ShotMode::TempoStick)
        && shot_.mode == mode && shot_.state != HoldState::Idle) {
        lsArmX = shot_.lsArmX;
        lsArmY = shot_.lsArmY;
        movementValid = true;
    }
    ownedOutputDrainLsArmX_ = lsArmX;
    ownedOutputDrainLsArmY_ = lsArmY;
    ownedOutputDrainMovementValid_ = movementValid;
    ownedOutputDrainEndFrames_ = 0;
    // Seed the end evidence from what the engine has ALREADY proven about this physical
    // control. abort() wipes ShotContext (and with it xButtonHistory) before latching the
    // drain, so re-deriving from the shot buffer would demand three FRESH polls even when the
    // player's release is already three polls old — and a re-press inside those three polls
    // then zeroes the counter and strands the drain over their new gesture. The engine-level
    // counters are not wiped, so a drain latched over an already-released control is correctly
    // born complete. This is the same three-sample contract, not a weaker one.
    // Stick modes deliberately restart their neutral census here, so they keep the existing
    // three-fresh-sample behaviour untouched; only Square carries proven prior evidence.
    ownedOutputDrainEndObserved_ = (mode == ShotMode::ButtonShot
                                    || mode == ShotMode::TempoSquare)
        && physicalSquareUpPolls_ >= kPhysicalReleaseSamples;
    if (mode == ShotMode::GoToStick || mode == ShotMode::TempoStick) {
        rightStickNeutralFrames_ = 0;
    }
}

void AutomationEngine::clearOwnedOutputDrain() noexcept
{
    ownedOutputDrain_ = OwnedOutputDrain::None;
    ownedOutputDrainMode_ = ShotMode::ButtonShot;
    ownedOutputDrainShotType_.clear();
    ownedOutputDrainLsArmX_ = 0.0;
    ownedOutputDrainLsArmY_ = 0.0;
    ownedOutputDrainMovementValid_ = false;
    ownedOutputDrainEndFrames_ = 0;
    ownedOutputDrainEndObserved_ = false;
}

bool AutomationEngine::applyOwnedOutputDrain(ControllerState& output,
                                              const ControllerState& physical,
                                              double now)
{
    if (ownedOutputDrain_ == OwnedOutputDrain::None) {
        return false;
    }

    const ShotMode mode = ownedOutputDrainMode_;
    const QString shotType = ownedOutputDrainShotType_;
    const double drainLsArmX = ownedOutputDrainLsArmX_;
    const double drainLsArmY = ownedOutputDrainLsArmY_;
    const bool drainMovementValid = ownedOutputDrainMovementValid_;
    const bool squareMode = mode == ShotMode::ButtonShot
        || mode == ShotMode::TempoSquare;
    const bool physicalUp = verticalStickShotIntent(
        physical, true, config_.stickUpThreshold, config_.gotoLateralMaxRatio);
    const bool physicalDown = verticalStickShotIntent(
        physical, false, config_.stickDownThreshold, config_.gotoLateralMaxRatio);
    const bool rsNeutral = std::hypot(static_cast<double>(physical.rightStickX),
                                      static_cast<double>(physical.rightStickY))
        < rawStickThreshold(config_.stickUpThreshold);
    const bool primaryActive = mode == ShotMode::ButtonShot
        || mode == ShotMode::TempoSquare
        ? physical.square()
        : (mode == ShotMode::GoToStick ? physicalUp : physicalDown);
    const bool physicalEndSample = !primaryActive;

    // A second shot control can arrive before abort() or while the independent
    // drain is active. Keep using the normal overlap contract even though the
    // ShotContext is already Idle: hidden input cannot surface as a synthetic
    // DOWN edge on the exact primary-end tick.
    if (mode == ShotMode::GoToStick) {
        squareLatchedUntilRelease_ = squareLatchedUntilRelease_ || physical.square();
        stickOverlapLatchedUntilNeutral_ = stickOverlapLatchedUntilNeutral_ || physicalDown;
    } else if (mode == ShotMode::TempoStick) {
        squareLatchedUntilRelease_ = squareLatchedUntilRelease_ || physical.square();
        // During an unresolved/ambiguous RELEASE drain, RS-up could duplicate a
        // transaction that already reached the route and remains suppressed. In
        // a pre-release HOLD abort, however, RS-up is the player's physical
        // Tempo completion—not a second Go-To shot—and must be returned to them.
        if (!(ownedOutputDrain_ == OwnedOutputDrain::HoldUntilPhysicalEnd
              && physicalUp)) {
            stickOverlapLatchedUntilNeutral_ = stickOverlapLatchedUntilNeutral_
                || physicalUp;
        }
    } else if (mode == ShotMode::TempoSquare) {
        stickOverlapLatchedUntilNeutral_ = stickOverlapLatchedUntilNeutral_
            || physicalUp || physicalDown;
    }
    if (!squareMode) {
        if (rsNeutral) {
            rightStickNeutralFrames_ = std::min(3, rightStickNeutralFrames_ + 1);
        } else {
            rightStickNeutralFrames_ = 0;
        }
    }
    if (physicalEndSample) {
        ownedOutputDrainEndFrames_ = std::min(
            kPhysicalReleaseSamples, ownedOutputDrainEndFrames_ + 1);
    } else {
        ownedOutputDrainEndFrames_ = 0;
    }
    // Latch the end evidence rather than re-deriving it every tick. The counter above is
    // zeroed by ANY re-assertion of the primary control, so a player who releases and presses
    // again inside the drain never accumulates three consecutive end samples; under
    // ReleasedUntilPhysicalEnd that leaves applyReleased() masking their new press for as long
    // as they hold it, and processIdle (which owns re-arming) never runs at all because the
    // drain short-circuits it. Once a genuine physical end has been proven for THIS drain it
    // stays proven. This bounds the FENCE only — nothing here is time-based and nothing drops
    // the button on a clock, because for ButtonShot that would be a blind release edge.
    if (ownedOutputDrainEndFrames_ >= kPhysicalReleaseSamples) {
        ownedOutputDrainEndObserved_ = true;
    }
    const bool physicalEnded = ownedOutputDrainEndObserved_;

    if (mode == ShotMode::TempoStick
        && ownedOutputDrain_ == OwnedOutputDrain::HoldUntilPhysicalEnd
        && physicalUp) {
        // A full physical cross-direction sample is stronger evidence than a
        // missing/neutral poll: hand the real RS-up flick back immediately. This
        // emits no bot release marker and remains rearm-fenced until center, so a
        // continuing up hold cannot become a Go-To takeover from the same motion.
        stickDownLatchedUntilNeutral_ = false;
        stickUpLatchedUntilNeutral_ = false;
        stickDownHoldStartMs_ = -1.0;
        stickUpHoldStartMs_ = -1.0;
        stickRearmBlockedUntilNeutral_ = true;
        rightStickNeutralFrames_ = 0;
        clearOwnedOutputDrain();
        if (squareLatchedUntilRelease_) {
            output.buttons &= ~XINPUT_GAMEPAD_X;
        }
        shot_.releasePlan = QStringLiteral("Idle / physical Tempo flick returned");
        shot_.releaseReason = QStringLiteral("physical_tempo_flick_handoff");
        return true;
    }

    auto applyHeld = [&]() {
        switch (mode) {
        case ShotMode::ButtonShot:
            output.buttons |= XINPUT_GAMEPAD_X;
            break;
        case ShotMode::GoToStick:
            output.buttons &= ~XINPUT_GAMEPAD_X;
            output.rightStickX = 0;
            output.rightStickY = -127;
            break;
        case ShotMode::TempoSquare:
            // [ORION_TEMPO_FADE_MIRROR] fade gathers UP; see ShotReleasePolicy.h.
            output.buttons &= ~XINPUT_GAMEPAD_X;
            output.rightStickX = 0;
            output.rightStickY = shot_.tempoFadeGesture ? -127 : 127;
            break;
        case ShotMode::TempoStick:
            output.buttons &= ~XINPUT_GAMEPAD_X;
            output.rightStickX = 0;
            output.rightStickY = shot_.tempoFadeGesture ? -127 : 127;
            break;
        }
        if (mode == ShotMode::TempoSquare || mode == ShotMode::TempoStick) {
            preserveFadeMovementVector(
                output, shotType, ownedOutputDrainLsArmX_,
                ownedOutputDrainLsArmY_, ownedOutputDrainMovementValid_);
        }
    };
    auto applyReleased = [&]() {
        switch (mode) {
        case ShotMode::ButtonShot:
            output.buttons &= ~XINPUT_GAMEPAD_X;
            break;
        case ShotMode::GoToStick:
            output.rightStickX = 0;
            output.rightStickY = 0;
            break;
        case ShotMode::TempoSquare:
            output.buttons &= ~XINPUT_GAMEPAD_X;
            output.rightStickX = 0;
            output.rightStickY = 0;
            break;
        case ShotMode::TempoStick:
            output.rightStickX = 0;
            output.rightStickY = 0;
            break;
        }
        if (mode == ShotMode::TempoSquare || mode == ShotMode::TempoStick) {
            preserveFadeMovementVector(
                output, shotType, ownedOutputDrainLsArmX_,
                ownedOutputDrainLsArmY_, ownedOutputDrainMovementValid_);
        }
    };

    if (!physicalEnded) {
        if (ownedOutputDrain_ == OwnedOutputDrain::HoldUntilPhysicalEnd) {
            applyHeld();
            shot_.releasePlan = QStringLiteral("Owned abort drain / holding output");
            shot_.releaseReason = QStringLiteral("waiting_for_debounced_physical_end");
        } else {
            applyReleased();
            shot_.releasePlan = QStringLiteral("Owned release drain / output suppressed");
            shot_.releaseReason = QStringLiteral("waiting_for_released_physical_end");
        }
        if (!squareMode && squareLatchedUntilRelease_) {
            output.buttons &= ~XINPUT_GAMEPAD_X;
        }
        return true;
    }

    const OwnedOutputDrain completedDrain = ownedOutputDrain_;
    if (squareMode) {
        squareRearmBlockedUntilRelease_ = false;
        squareLatchedUntilRelease_ = false;
        squareHoldStartMs_ = -1.0;
    } else {
        stickDownLatchedUntilNeutral_ = false;
        stickUpLatchedUntilNeutral_ = false;
        stickDownHoldStartMs_ = -1.0;
        stickUpHoldStartMs_ = -1.0;
        if (rightStickNeutralFrames_ >= 3) {
            stickRearmBlockedUntilNeutral_ = false;
            stickOverlapLatchedUntilNeutral_ = false;
        } else {
            // A stable sideways/opposite command ends the old gesture without
            // authorizing a new shot. Pass it through below unless it is itself
            // a suppressed opposite-direction overlap, and retain the neutral
            // rearm fence until processIdle observes three centered polls.
            stickRearmBlockedUntilNeutral_ = true;
        }
    }
    clearOwnedOutputDrain();

    if (completedDrain == OwnedOutputDrain::HoldUntilPhysicalEnd
        && mode == ShotMode::TempoSquare) {
        // Square Tempo remains a complete remap even when detector authority is
        // relinquished: gather stays continuous through the debounced button-UP,
        // then the user's physical end produces one bounded up-flick. This is not
        // an autonomous release marker and does not teach timing.
        pendingSquareTempoRemap_ = true;
        pendingSquareShotType_ = shotType;
        pendingSquareLsArmX_ = drainLsArmX;
        pendingSquareLsArmY_ = drainLsArmY;
        pendingSquareMovementValid_ = drainMovementValid;
        tempoPassThroughPulseActive_ = true;
        tempoPassThroughPulseEndMs_ = now
            + std::max(config_.tempoFlickHoldMs, config_.releasePulseMs);
        applyShotReleaseEdge(output, ShotMode::TempoSquare, shotType,
                         tempoGestureIsFade(shotType, config_.tempoFadeMirrorGesture));
        preserveFadeMovementVector(
            output, shotType, drainLsArmX, drainLsArmY, drainMovementValid);
        shot_.releasePlan = QStringLiteral("Idle / tempo pass-through flick");
        shot_.releaseReason = QStringLiteral("tempo_remap_passthrough");
        return true;
    }

    // No-meter/no-authority aborts never invent a TempoStick flick. A stable
    // non-primary stick command may pass through after the debounce, but an
    // opposite shot gesture hidden under the old ownership stays neutral until
    // its own physical neutral fence. Successful Tempo releases already emitted
    // their up-flick before entering ReleasedUntilPhysicalEnd.
    if (squareMode) {
        applyReleased();
    } else {
        if (stickOverlapLatchedUntilNeutral_) {
            output.rightStickX = 0;
            output.rightStickY = 0;
        }
        if (squareLatchedUntilRelease_) {
            output.buttons &= ~XINPUT_GAMEPAD_X;
        }
    }
    pendingSquareTempoRemap_ = false;
    clearPendingSquareMovementContext();
    resetTempoMovementTransaction();
    shot_.releasePlan = QStringLiteral("Idle / physical end accepted");
    shot_.releaseReason = QStringLiteral("physical_end_debounced");
    return true;
}

void AutomationEngine::observePhysicalEndWhileDisarmed(
    const ControllerState& physical)
{
    const bool squareEnded = !physical.square()
        && shot_.xButtonHistory.size() >= 3
        && std::all_of(shot_.xButtonHistory.cbegin(),
                       shot_.xButtonHistory.cend(),
                       [](bool pressed) { return !pressed; });
    if (squareEnded) {
        squareRearmBlockedUntilRelease_ = false;
        squareLatchedUntilRelease_ = false;
        squareHoldStartMs_ = -1.0;
        pendingSquarePhysicalEpoch_ = 0;
        pendingSquareArmGeneration_ = 0;
        pendingSquareTempoRemap_ = false;
        clearPendingSquareMovementContext();
        resetTempoMovementTransaction();
        tempoPassThroughPulseActive_ = false;
        tempoPassThroughPulseEndMs_ = -1.0;
    }

    const bool rsNeutral = std::hypot(
        static_cast<double>(physical.rightStickX),
        static_cast<double>(physical.rightStickY))
        < rawStickThreshold(config_.stickUpThreshold);
    if (rsNeutral) {
        rightStickNeutralFrames_ = std::min(3, rightStickNeutralFrames_ + 1);
    } else {
        rightStickNeutralFrames_ = 0;
    }
    if (rightStickNeutralFrames_ >= 3) {
        stickRearmBlockedUntilNeutral_ = false;
        stickOverlapLatchedUntilNeutral_ = false;
        stickDownLatchedUntilNeutral_ = false;
        stickUpLatchedUntilNeutral_ = false;
        stickDownHoldStartMs_ = -1.0;
        stickUpHoldStartMs_ = -1.0;
        stickUpFrames_ = 0;
        clearPendingTempoStickMovementContext();
        if (tempoMovement_.mode == ShotMode::TempoStick) {
            resetTempoMovementTransaction();
        }
    }
}

void AutomationEngine::restoreAbortOutput(ControllerState& output)
{
    output = lastPhysical_;
    if (applyOwnedOutputDrain(output, lastPhysical_, nowMs())) {
        return;
    }
    if (pendingSquareTempoRemap_) {
        forceTempoSquareGather(output);
    }
}

bool AutomationEngine::consumeDueScheduledFire(ControllerState& output, double now)
{
    if (schedFireDeadlineMs_ < 0.0) {
        return false;
    }

    const quint64 token = schedFireToken_;
    const double deadlineMs = schedFireDeadlineMs_;
    const QString plan = schedFirePlan_;
    const QString reason = schedFireReason_;
    const QString code = schedFireCode_;
    const quint64 routeGeneration = schedFireRouteGeneration_;
    const LatencyControllerRoute route = schedFireRoute_;
    bool confirmed = schedFireConfirmedToken_ == token && schedFireActualMs_ >= 0.0;
    if (!confirmed && now < deadlineMs + config_.schedulerGraceMs) {
        return false;
    }

    if (!confirmed) {
        // Do not clear the engine token before the controller has serialized with
        // the precise worker. If its physical submit already won, the direct
        // invalidation callback confirms it before returning. Otherwise the
        // worker is disarmed and the in-tick release below is the sole submit.
        const bool workerFailed = invalidateUnconfirmedSchedule(true);
        if (workerFailed) {
            abort(QStringLiteral("precise_fire_delivery_failed"));
            restoreAbortOutput(output);
            return true;
        }
        if (schedFireDeadlineMs_ >= 0.0 && schedFireToken_ != token) {
            return false; // a newer schedule won; never consume it as this one
        }
        confirmed = schedFireDeadlineMs_ >= 0.0
            && schedFireToken_ == token
            && schedFireConfirmedToken_ == token
            && schedFireActualMs_ >= 0.0;
    }

    shot_.firedByScheduler = confirmed;
    shot_.scheduledFireDeltaMs = confirmed
        ? schedFireActualMs_ - deadlineMs : 0.0;
    shot_.releasePlan = plan;
    shot_.releaseReason = reason;
    shot_.releaseReasonCode = code;
    shot_.releaseScheduleToken = token;
    shot_.releaseScheduleRouteGeneration = routeGeneration;
    shot_.releaseScheduleRoute = route;
    // [ORION_ARMED_SOURCE] Carry the arming decision's attribution into the release context
    // BEFORE clearScheduledFire() wipes the snapshot; triggerRelease pairs it with
    // lastReleaseSeq_ so the outcome line (~1.2s later) can attribute the shot.
    shot_.armedPredictorSource = schedFireArmedSource_;
    shot_.armedPredictorSigmaMs = schedFireArmedSigmaMs_;
    shot_.armedPredictorFillPct = schedFireArmedFillPct_;
    shot_.armedPredictorCommandEtaMs = schedFireArmedCommandEtaMs_;
    // [ORION_DEV_FIRE_OFFSET] Carry the displacement this token actually fired with, so the
    // release-marker site can log it seq-paired for the offline sweep join.
    lastReleaseDevOffsetMs_ = schedFireAppliedDevOffsetMs_;
    const double firedAt = confirmed ? schedFireActualMs_ : now;
    clearScheduledFire();
    if (triggerRelease(firedAt, confirmed)) {
        clearReleaseOutput(output);
    } else {
        restoreAbortOutput(output);
    }
    return true;
}

void AutomationEngine::relinquishAutonomousLiveMeterShot(ControllerState& output,
                                                          const QString& reason)
{
    abort(reason);
    if (shot_.state == HoldState::Releasing) {
        // A copied precise-fire token can be confirmed while abort() fences the
        // worker.  That submitted edge wins and triggerRelease() has already
        // transitioned the shot to Releasing.  Return the same mode-specific
        // release packet on this tick instead of reasserting the held physical
        // input immediately after the worker's edge.
        output = lastPhysical_;
        clearReleaseOutput(output);
        return;
    }
    restoreAbortOutput(output);
}

void AutomationEngine::processAutonomousLiveMeterHolding(ControllerState& output, double now)
{
    constexpr double kTipTargetPct = 100.0;
    if (shot_.latencyCalibrationProbe && measuredLeadAuthoritative(now)) {
        // A probe may already have copied an L1/L2 deadline before a prior label
        // makes the posterior authoritative. Fence that calibration token while
        // its identity is intact. A submitted worker edge remains a calibration
        // release; only a successfully disarmed/no-token probe can be promoted.
        if (schedFireDeadlineMs_ >= 0.0) {
            const bool workerFailed = invalidateUnconfirmedSchedule();
            if (consumeDueScheduledFire(output, now)) {
                return;
            }
            if (workerFailed) {
                abort(QStringLiteral("precise_fire_delivery_failed"));
                restoreAbortOutput(output);
                return;
            }
        }
        // Calibration can converge from a prior delivered label while this real
        // shot is already owned. Promote that same physical gesture into normal
        // live-tip timing instead of abandoning it and waiting for another press.
        // beginShot intentionally excluded probes from attempt accounting, so
        // transfer the identity exactly once at this boundary.
        shot_.latencyCalibrationProbe = false;
        shot_.latencyCalibrationAutomaticProbe = false;
        shot_.latencyValidationTargetPct = -1.0;
        shot_.latencyValidationTolerancePct = -1.0;
        ++shotsAttempted_;
        emit engineDiagnostic(QStringLiteral(
            "Latency calibration converged mid-shot: promoted owned gesture to live-tip timing."));
    }
    const bool calibrationProbe = shot_.latencyCalibrationProbe;
    const double heldMs = shot_.holdStartMs > 0.0 ? now - shot_.holdStartMs : 0.0;
    const bool genuineCurrent = shot_.meterDetected && shot_.lastSampleGenuineAccept
        && meterReleaseAuthorityCurrent(now);
    const bool ownedSquare = shot_.mode == ShotMode::ButtonShot
        || shot_.mode == ShotMode::TempoSquare;
    // [ORION_TEMPO_PARITY] The bounded-hold semantics below (hold-not-abort on lead loss /
    // unresolved trajectory / contested estimate / imminent phase anchor, plus the lease
    // continuation across a detector blink) were narrowed to owned Square purely to keep the
    // original revert surface small — nothing in them is Square-specific: TempoStick's owned
    // output is the same bot-held gesture (RS-down gather) with the same maxHoldMs ceiling
    // and the same fail-closed teardown. With tempo_tip_parity ON, TempoStick joins them so
    // its tip timing degrades exactly like ButtonShot's instead of aborting first (or
    // releasing on the +61-92ms-early-biased sampler the imminent hold exists to outwait).
    // [ORION_GOTO_PARITY] Same reasoning, same guarantees for GoToStick under goto_tip_parity:
    // its owned output is the bot-held RS-up with the same fail-closed teardown, and it already
    // promotes direct to Holding via the identical strict rising proof, so every hold below is
    // equally sound for it. Live census 2026-08-04..06: Go-To was 5 of the 16 dated no-fire
    // aborts that died where ButtonShot would have held. Each hold keeps the absolute
    // maxHoldMs ceiling (1200ms — STRICTER than Go-To's own 1400ms gotoMaxHoldMs, so parity
    // can only shorten a doomed wait, never extend one) and the same relock grace.
    // OFF keeps ownedHold == ownedSquare, bit-identical to the pre-flag build.
    const bool ownedHold = ownedSquare
        || (config_.tempoTipParity && shot_.mode == ShotMode::TempoStick)
        || (config_.gotoTipParity && shot_.mode == ShotMode::GoToStick);

    // A copied autonomous deadline is immutable and may survive a detector blink
    // only through the finite frame+lead lease proven when it was armed. Consume a
    // confirmed/due edge before considering newer negative telemetry; a physical
    // submit that already happened always wins exactly once.
    // [ORION_ROLLING_LEASE] Extend first, fence second. The refresh only ever moves the lease
    // FORWARD and only from a genuine frame in this token's own vision epoch, so the fence below
    // still sees an honest "is this token's evidence current" answer.
    refreshScheduledFireAuthorityLease();
    if (schedFireDeadlineMs_ >= 0.0) {
        const bool confirmed = schedFireConfirmedToken_ == schedFireToken_
            && schedFireActualMs_ >= 0.0;
        const bool wrongEpoch = schedFireVisionEpoch_ != shot_.visionEpoch;
        const bool authorityExpired = schedFireAuthorityExpiryMs_ < 0.0
            || now > schedFireAuthorityExpiryMs_ + 1e-6;
        // A deadline the rolling lease still does not cover, with less than one source cadence
        // left to run, can never become authorized. Retire it rather than let it fire on evidence
        // that is guaranteed to be stale at the fire instant.
        const double untilArmedMs = schedFireDeadlineMs_ - now;
        const bool uncoveredAtCommit = !scheduledFireAuthorityCoversDeadline()
            && untilArmedMs > 0.0 && untilArmedMs <= cleanSourceGapMs();
        if (!confirmed && (wrongEpoch || authorityExpired || uncoveredAtCommit)) {
            invalidateUnconfirmedVisionSchedule(
                uncoveredAtCommit && !wrongEpoch && !authorityExpired
                    ? "tick_authority_uncovered" : "tick_epoch_or_authority");
        }
    }
    if (consumeDueScheduledFire(output, now)) {
        return;
    }

    if (calibrationProbe) {
        // Calibration owns a real physical input but intentionally has no normal tip
        // fallback. Every genuine-but-unusable waiting branch below is therefore
        // bounded by the mode's existing post-meter hold budget. A confirmed submit
        // was consumed above; an unconfirmed future deadline is revoked and no blind
        // release/marker is fabricated when this ceiling expires.
        const double calibrationOwnershipCapMs = std::max(
            1.0, shot_.mode == ShotMode::GoToStick
                ? config_.gotoMaxHoldMs : config_.maxHoldMs);
        if (heldMs >= calibrationOwnershipCapMs) {
            invalidateUnconfirmedVisionSchedule("calibration_timeout");
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("latency_calibration_timeout_abort"));
            return;
        }
    }

    auto holdOwnedSquareUnresolved = [&](const QString& reason,
                                          const QString& plan) {
        const bool transition = shot_.releaseReasonCode != reason;
        shot_.releasePlan = plan;
        shot_.releaseReason = reason;
        shot_.releaseReasonCode = reason;
        if (!transition) {
            return;
        }
        auto token = [](QString value) {
            value = value.trimmed();
            if (value.isEmpty()) {
                return QStringLiteral("none");
            }
            value.replace(QLatin1Char(' '), QLatin1Char('_'));
            value.replace(QLatin1Char('\t'), QLatin1Char('_'));
            return value.left(96);
        };
        emit engineDiagnostic(QStringLiteral(
            "OWNED-SHOT UNRESOLVED: reason=%1 mode=%2 shot_type=%3 "
            "action=hold_output_no_blind_release phys_sq=%4 presence=%5 "
            "last_stage=%6 last_reject=%7 det_total=%8 det_fresh=%9 "
            "det_not_detected=%10 det_conf_low=%11 det_stale=%12 det_dup=%13 "
            "det_memory=%14 det_relocks=%15 source=%16")
                                  .arg(token(reason),
                                       shot_.mode == ShotMode::TempoSquare
                                           ? QStringLiteral("TempoSquare")
                                           : shot_.mode == ShotMode::TempoStick
                                               ? QStringLiteral("TempoStick")
                                               // [ORION_GOTO_PARITY] a Go-To can reach the
                                               // owned holds under goto_tip_parity; name it.
                                               : shot_.mode == ShotMode::GoToStick
                                                   ? QStringLiteral("GoToStick")
                                                   : QStringLiteral("ButtonShot"),
                                       token(shot_.shotType))
                                  .arg(lastPhysical_.square() ? 1 : 0)
                                  .arg(token(shot_.detectionPresence),
                                       token(shot_.lastDetectionStage),
                                       token(shot_.lastDetectionRejectionReason))
                                  .arg(shot_.detSamplesTotal)
                                  .arg(shot_.detFreshAccepts)
                                  .arg(shot_.detNotDetected)
                                  .arg(shot_.detConfLow)
                                  .arg(shot_.detStaleFrameDrop)
                                  .arg(shot_.detDuplicateFrameDrop)
                                  .arg(shot_.detMemoryTrusted)
                                  .arg(shot_.detRelockResets)
                                  .arg(token(shot_.lastDetectionPayloadSource)));
    };

    if (calibrationProbe && !latencyCalibrationMode_) {
        // Manual cancel is authoritative over an already-owned probe. The per-shot bit records
        // how ownership began; it must not outlive the engine-scoped intent and release later.
        relinquishAutonomousLiveMeterShot(
            output, QStringLiteral("latency_calibration_cancelled"));
        return;
    }

    if (!calibrationProbe && !measuredLeadAuthoritative(now)) {
        invalidateUnconfirmedVisionSchedule("tick_dropout_guard");
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
        if (ownedHold) {
            // Lead loss revokes release authority, but returning physical control does not
            // require inventing a release: after the existing absolute Button hold budget,
            // abort ownership and mirror the user's current state. Until then a brief telemetry
            // gap may recover without leaking a second DOWN/UP edge into the active shot.
            if (heldMs >= std::max(1.0, config_.maxHoldMs)) {
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("measured_lead_unready_abort"));
                return;
            }
            holdOwnedSquareUnresolved(
                QStringLiteral("owned_square_measured_lead_unready"),
                QStringLiteral("Live meter / holding output; latency authority unavailable"));
        } else {
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("measured_lead_unready_abort"));
        }
        return;
    }

    if (!genuineCurrent) {
        invalidateVisionScheduleForDropout(now);
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
        if (calibrationProbe) {
            // Before current-shot ownership proof, the mode's physical release is an
            // unambiguous user cancel. After proof, a release that lands during a detector blink is ambiguous: the game may
            // already be executing the valid shot whose meter temporarily disappeared. Preserve
            // the owned DOWN edge for the same bounded re-lock grace used by detector authority,
            // then return the current physical state without manufacturing a release if vision
            // does not recover. This avoids both immediate user-timed leakage and an unbounded hold.
            const bool currentEpochProof = shot_.latencyCalibrationAutomaticProbe
                && automaticCalibrationEpochCurrent()
                && shot_.sawFreshMeterThisShot
                && shot_.anchorValidMs >= 0.0;
            if (!physicalGestureActiveForMode(shot_.mode, lastPhysical_)
                && !currentEpochProof) {
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("latency_calibration_cancelled"));
            } else {
                const double relockGraceMs = config_.meterFreshWindowMs
                    * (wifiMode_ ? config_.wifiFreshnessFactor : 1.0);
                if (currentEpochProof && shot_.lastFreshAcceptMs >= 0.0
                    && now - shot_.lastFreshAcceptMs > relockGraceMs) {
                    relinquishAutonomousLiveMeterShot(
                        output, QStringLiteral("detector_authority_lost_abort"));
                    return;
                }
                shot_.releasePlan = QStringLiteral("Latency calibration / waiting for meter");
                shot_.releaseReason = currentEpochProof
                    ? QStringLiteral("waiting_for_meter_relock")
                    : QStringLiteral("waiting_for_genuine_meter");
                shot_.releaseReasonCode = shot_.releaseReason;
            }
            return;
        }
        if (ownedHold) {
            if (autonomousVisionScheduleLeaseCurrent(now)) {
                shot_.releasePlan = QStringLiteral(
                    "Live meter / committed crossing inside authority lease");
                shot_.releaseReason = QStringLiteral(
                    "release_scheduled_lease_continuation");
                shot_.releaseReasonCode = shot_.releaseReason;
            } else {
                const double relockGraceMs = config_.meterFreshWindowMs
                    * (wifiMode_ ? config_.wifiFreshnessFactor : 1.0);
                if (shot_.lastFreshAcceptMs < 0.0
                    || now - shot_.lastFreshAcceptMs > relockGraceMs) {
                    relinquishAutonomousLiveMeterShot(
                        output, QStringLiteral("detector_authority_lost_abort"));
                    return;
                }
                holdOwnedSquareUnresolved(
                    QStringLiteral("owned_square_detector_unresolved"),
                    QStringLiteral("Live meter / holding output; waiting for genuine relock"));
            }
            return;
        }
        // Meter acquisition normally occurs well after Square ownership. Waiting
        // is safe because no deadline can exist without a current genuine frame;
        // only the bounded abort returns physical control if vision never arrives.
        const bool validatedMeterSeen = shot_.anchorValidMs >= 0.0;
        const double noMeterAbortMs = shot_.mode == ShotMode::GoToStick
            ? (config_.gotoMeterWait
                   ? std::max(config_.gotoNoMeterAbortMs, config_.gotoMeterWaitCapMs)
                   : config_.gotoNoMeterAbortMs)
            : config_.buttonNoMeterAbortMs;
        if (!validatedMeterSeen && heldMs >= noMeterAbortMs) {
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("no_meter_abort"));
            return;
        }
        // After a validated lock, tolerate a bounded re-lock gap. This never
        // preserves release authority: the schedule was synchronously revoked
        // above and only a new genuine trajectory can create another one.
        const double relockGraceMs = config_.meterFreshWindowMs
            * (wifiMode_ ? config_.wifiFreshnessFactor : 1.0);
        if (validatedMeterSeen && shot_.lastFreshAcceptMs >= 0.0
            && now - shot_.lastFreshAcceptMs > relockGraceMs) {
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("detector_authority_lost_abort"));
            return;
        }
        shot_.releasePlan = validatedMeterSeen
            ? QStringLiteral("Live meter / waiting for relock")
            : QStringLiteral("Live meter / waiting for meter");
        shot_.releaseReason = validatedMeterSeen
            ? QStringLiteral("waiting_for_meter_relock")
            : QStringLiteral("waiting_for_genuine_meter");
        shot_.releaseReasonCode = shot_.releaseReason;
        return;
    }

    if (shot_.sawFreshMeterThisShot && shot_.firstFreshAcceptMs >= 0.0
        && shot_.anchorValidMs >= 0.0 && !shot_.meterSeenThisShot) {
        shot_.meterSeenThisShot = true;
        shot_.firstMeterSeenMs = shot_.firstFreshAcceptMs;
        shot_.firstMeterFrameAgeMs = shot_.frameAgeMs;
    }

    const AutonomousTipDecision tipDecision = canonicalAutonomousTipDecision(now);
    const TemporalSampler::CrossingFit& fit = tipDecision.samplerFit;
    const double velocity = tipDecision.velocityPctPerMs;
    const bool liveTrajectory = tipDecision.samplerAuthoritative;
    const double predictedTipMs = tipDecision.tipAbsMs;
    const double predictedTipSigmaMs = tipDecision.combinedSigmaMs;
    const QString& predictedTipSource = tipDecision.source;
    const bool tipPredictionValid = tipDecision.valid;
    const bool tipEstimatePresent = std::isfinite(predictedTipMs)
        && predictedTipMs > now && !predictedTipSource.isEmpty();

    shot_.targetPct = kTipTargetPct;
    // Keep a finite estimate visible for diagnostics even when its uncertainty
    // is too large to authorize actuation.  `tipPredictionValid` remains the
    // sole release-authority gate below.
    shot_.targetModeAtRelease = tipEstimatePresent
        ? QStringLiteral("meter_tip_%1").arg(predictedTipSource)
        : QStringLiteral("meter_tip_live");
    shot_.releaseVelocityPctMs = velocity;
    shot_.releaseCrossingEtaMs = tipEstimatePresent ? predictedTipMs - now : -1.0;
    shot_.tipPredictionSource = predictedTipSource;
    shot_.tipPredictionSigmaMs = predictedTipSigmaMs;
    shot_.tipPredictionAuthorityKind = measuredLatencyAuthorityKind_;
    shot_.tipPredictionLeadMs = measuredLeadForActuationMs();
    // [ORION_USER_LEAD_AUTHORITY] effective (not raw) so the user-lead path carries the same
    // 6.0ms factory-floor sigma the proven cold-start sessions scheduled with.
    shot_.tipPredictionLeadSigmaMs = effectiveLeadSdMs();

    if (calibrationProbe) {
        if (shot_.latencyCalibrationAutomaticProbe
            && !automaticCalibrationEpochCurrent()) {
            invalidateUnconfirmedVisionSchedule("tick_green_path");
            if (consumeDueScheduledFire(output, now)) {
                return;
            }
            shot_.releasePlan = QStringLiteral("Latency calibration / validating meter structure");
            shot_.releaseReason = QStringLiteral("waiting_for_meter_structure");
            shot_.releaseReasonCode = QStringLiteral("waiting_for_meter_structure");
            return;
        }
        // Requiring the validated anchor plus a usable >=3-sample forward fit rejects static
        // menu lookalikes. Green geometry must also be confirmed from this shot: a plausible
        // rising bar without its meter window is not permission to sacrifice a live attempt.
        if (!liveTrajectory) {
            shot_.releasePlan = QStringLiteral("Latency calibration / validating rise");
            shot_.releaseReason = QStringLiteral("waiting_for_live_trajectory");
            shot_.releaseReasonCode = QStringLiteral("waiting_for_live_trajectory");
            return;
        }
        const double greenStart = greenTracker_.startPct();
        const double greenWidth = greenTracker_.widthPct();
        const double greenEnd = greenStart + greenWidth;
        const bool greenGeometryReady = greenTracker_.confirmed()
            && std::isfinite(greenStart) && std::isfinite(greenEnd)
            && std::isfinite(greenWidth)
            && greenStart > 0.0 && greenStart <= kTipTargetPct
            && greenEnd >= greenStart && greenEnd <= kTipTargetPct
            && greenWidth > 0.0
            && std::isfinite(shot_.minFreshFillPct)
            && shot_.minFreshFillPct >= 0.0
            && shot_.minFreshFillPct < greenStart;
        if (!greenGeometryReady) {
            shot_.releasePlan = QStringLiteral(
                "Latency calibration / validating current-shot green geometry");
            shot_.releaseReason = QStringLiteral("waiting_for_green_geometry");
            shot_.releaseReasonCode = QStringLiteral("waiting_for_green_geometry");
            return;
        }

        const bool validationAnchor = controlledCalibrationAnchorAvailable(now);
        if (validationAnchor) {
            // L1 is never allowed to time the tip. Its sole actuation privilege is this distinct
            // causal check: command a frame-derived planned stop that remains below the oracle's
            // hard non-cap inverse ceiling. The stop does not have to be in green; green geometry
            // is structural proof that this is the current shot's real meter, not the value being
            // measured. Requiring overlap made a legitimate 96-100 band impossible to validate
            // and could leave the bot permanently unready on that animation.
            //
            // Keep the whole marker tolerance below the cap. This chooses the latest safely
            // invertible target, maximizing causal headroom on measured high-latency routes while
            // remaining autonomous: slope, uncertainty, frame age, and deadline all come from the
            // current rise and the controlled L1 posterior.
            constexpr double kOracleEligibleStopMaxPct = 95.0;
            // Keep a validation command far enough below the oracle's hard cap that the
            // sidecar can apply its one bounded controlled-anchor recovery (currently a
            // maximum 15 percentage-point residual) without first classifying the freeze as
            // near-cap and resetting the whole two-label epoch.  The previous target/tolerance
            // envelope ended exactly at 95%, so even a small trajectory-model bias bypassed
            // recovery and deterministically returned setup to L1.
            constexpr double kControlledRecoveryMaxResidualPct = 15.0;
            constexpr double kRecoverableValidationTargetMaxPct =
                kOracleEligibleStopMaxPct - kControlledRecoveryMaxResidualPct;
            const double validationSlope = std::abs(fit.slopePctPerMs);
            const double oneSigmaStopPct = std::sqrt(
                0.4 * 0.4
                + std::pow(validationSlope * measuredLatencySdMs_, 2.0));
            const double preferredTolerancePct = 3.0 * oneSigmaStopPct;
            const double preferredTargetPct =
                kRecoverableValidationTargetMaxPct - preferredTolerancePct;
            const double latestReliableTargetPct =
                kRecoverableValidationTargetMaxPct;
            if (!std::isfinite(oneSigmaStopPct) || oneSigmaStopPct <= 0.0
                || !std::isfinite(preferredTargetPct)
                || !std::isfinite(latestReliableTargetPct)
                || preferredTargetPct < 20.0
                || latestReliableTargetPct <= preferredTargetPct + 1e-6) {
                invalidateUnconfirmedVisionSchedule("tick_cal_a");
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("latency_validation_residual_unavailable"));
                return;
            }

            const double frameAgeMs = std::max(0.0, shot_.frameAgeMs);
            // validationFit is already expressed on the capture-aligned engine clock. Adding
            // frameAge again would double-count decoder/IPC age and command the stop too early.
            const double effectiveLeadMs = measuredLatencyMs_;
            constexpr double kMinimumScheduleHeadroomMs = 1.0;
            auto commandEtaFor = [&](const TemporalSampler::CrossingFit& candidate) {
                return candidate.crossingMs - effectiveLeadMs - now;
            };

            // First prove that a target with the sidecar's complete bounded-recovery reserve is
            // still reachable. If it is, choose the lowest causal target between the preferred
            // three-sigma point and that upper bound. This maximizes validation tolerance without
            // asking the worker to fire in the past when ownership/green confirmation completed a
            // few milliseconds late.
            const TemporalSampler::CrossingFit latestReliableFit =
                sampler_.predictCrossing(latestReliableTargetPct);
            if (shot_.fillPct >= latestReliableTargetPct - 1e-6
                || (liveMeterCrossingAuthoritative(latestReliableFit, now)
                    && commandEtaFor(latestReliableFit)
                        <= kMinimumScheduleHeadroomMs)) {
                invalidateUnconfirmedVisionSchedule("tick_cal_b");
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("latency_validation_deadline_missed"));
                return;
            }
            if (!liveMeterCrossingAuthoritative(latestReliableFit, now)) {
                invalidateUnconfirmedVisionSchedule("tick_cal_c");
                if (consumeDueScheduledFire(output, now)) {
                    return;
                }
                shot_.releasePlan = QStringLiteral(
                    "Latency calibration / fitting non-cap validation target");
                shot_.releaseReason = QStringLiteral("waiting_for_validation_trajectory");
                shot_.releaseReasonCode = QStringLiteral("waiting_for_validation_trajectory");
                return;
            }

            double validationTarget = preferredTargetPct;
            TemporalSampler::CrossingFit validationFit =
                sampler_.predictCrossing(validationTarget);
            const bool preferredCausal = shot_.fillPct < validationTarget - 1e-6
                && liveMeterCrossingAuthoritative(validationFit, now)
                && commandEtaFor(validationFit) > kMinimumScheduleHeadroomMs;
            if (!preferredCausal) {
                double lowerTarget = std::max(
                    preferredTargetPct, shot_.fillPct + 1e-3);
                double upperTarget = latestReliableTargetPct;
                // The upper endpoint was proven authoritative and causal above. Crossing time is
                // monotone in target on this genuine rising episode, so bounded bisection finds
                // the maximum-tolerance target with a physically schedulable deadline.
                for (int iteration = 0; iteration < 24; ++iteration) {
                    const double middleTarget = 0.5 * (lowerTarget + upperTarget);
                    const TemporalSampler::CrossingFit middleFit =
                        sampler_.predictCrossing(middleTarget);
                    const bool middleCausal =
                        liveMeterCrossingAuthoritative(middleFit, now)
                        && commandEtaFor(middleFit) > kMinimumScheduleHeadroomMs;
                    if (middleCausal) {
                        upperTarget = middleTarget;
                    } else {
                        lowerTarget = middleTarget;
                    }
                }
                validationTarget = upperTarget;
                validationFit = sampler_.predictCrossing(validationTarget);
            }
            if (!liveMeterCrossingAuthoritative(validationFit, now)) {
                invalidateUnconfirmedVisionSchedule("tick_cal_d");
                if (consumeDueScheduledFire(output, now)) {
                    return;
                }
                shot_.releasePlan = QStringLiteral(
                    "Latency calibration / fitting non-cap validation target");
                shot_.releaseReason = QStringLiteral("waiting_for_validation_trajectory");
                shot_.releaseReasonCode = QStringLiteral("waiting_for_validation_trajectory");
                return;
            }

            const double targetSlope = std::abs(validationFit.slopePctPerMs);
            const double targetUncertaintyTolerance = 3.0 * std::sqrt(
                0.4 * 0.4
                + std::pow(targetSlope * measuredLatencySdMs_, 2.0));
            const double nonCapMargin =
                kOracleEligibleStopMaxPct - validationTarget;
            const double validationTolerance =
                std::min(nonCapMargin, targetUncertaintyTolerance);
            const double targetOneSigmaStopPct = std::sqrt(
                0.4 * 0.4
                + std::pow(targetSlope * measuredLatencySdMs_, 2.0));
            if (!std::isfinite(validationTolerance) || validationTolerance <= 0.0
                || !std::isfinite(targetOneSigmaStopPct)
                || validationTolerance + 1e-6 < targetOneSigmaStopPct) {
                invalidateUnconfirmedVisionSchedule("tick_cal_e");
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("latency_validation_residual_unavailable"));
                return;
            }

            const double fireAtMs = validationFit.crossingMs - effectiveLeadMs;
            shot_.targetPct = validationTarget;
            shot_.targetModeAtRelease = QStringLiteral("meter_calibration_validation_live");
            shot_.effectiveLatencyMs = effectiveLeadMs;
            shot_.releaseEtaMs = fireAtMs - now;
            shot_.releaseVelocityPctMs = validationFit.slopePctPerMs;
            shot_.releaseCrossingEtaMs = validationFit.crossingMs - now;
            shot_.latencyValidationTargetPct = validationTarget;
            shot_.latencyValidationTolerancePct = validationTolerance;

            if (fireAtMs <= now + 1e-6) {
                emit engineDiagnostic(QStringLiteral(
                    "LATENCY SETUP UNREACHABLE: fill=%1 target=%2 slope=%3 "
                    "crossing_eta_ms=%4 lead_ms=%5 frame_age_ms=%6 command_eta_ms=%7")
                                          .arg(shot_.fillPct, 0, 'f', 2)
                                          .arg(validationTarget, 0, 'f', 2)
                                          .arg(validationFit.slopePctPerMs, 0, 'f', 4)
                                          .arg(validationFit.crossingMs - now, 0, 'f', 1)
                                          .arg(measuredLatencyMs_, 0, 'f', 1)
                                          .arg(frameAgeMs, 0, 'f', 1)
                                          .arg(fireAtMs - now, 0, 'f', 1));
                invalidateUnconfirmedVisionSchedule("tick_validation_missed");
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("latency_validation_deadline_missed"));
                return;
            }
            if (schedFireDeadlineMs_ >= 0.0) {
                // [ORION_DEV_FIRE_OFFSET] Drift comparisons run against the UNDISPLACED armed
                // deadline (identical when the hook is disarmed, offset 0).
                const double armedBaseMs =
                    schedFireDeadlineMs_ - schedFireAppliedDevOffsetMs_;
                if (std::abs(fireAtMs - armedBaseMs) <= 1e-6
                    && schedFireCode_ == QLatin1String("latency_calibration_validation")) {
                    shot_.releasePlan = QStringLiteral("Non-cap latency validation scheduled");
                    shot_.releaseReason = QStringLiteral("release_scheduled");
                    shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                    return;
                }
                invalidateUnconfirmedVisionSchedule("tick_validation_reschedule");
                if (consumeDueScheduledFire(output, now)) {
                    return;
                }
            }
            if (scheduleFire(fireAtMs, now, -1.0,
                             ScheduledFireAuthority::AutonomousMeterVision)) {
                schedFirePlan_ = QStringLiteral("Non-cap latency validation");
                schedFireReason_ = QStringLiteral("latency_calibration_validation");
                schedFireCode_ = QStringLiteral("latency_calibration_validation");
                shot_.releasePlan = QStringLiteral("Non-cap latency validation scheduled");
                shot_.releaseReason = QStringLiteral("release_scheduled");
                shot_.releaseReasonCode = QStringLiteral("release_scheduled");
            } else {
                shot_.releasePlan = QStringLiteral("Latency calibration / tracking validation");
                shot_.releaseReason = QStringLiteral("waiting_for_validation_deadline");
                shot_.releaseReasonCode = QStringLiteral("waiting_for_validation_deadline");
            }
            return;
        }

        // A prior precise-fire token may already have been copied by the worker.
        // Fence it synchronously before changing calibration phase; if submit won
        // that race, consume the already-physical edge exactly once.
        invalidateUnconfirmedSchedule(false, "calibration_phase");
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
        // A cold causal measurement needs visible rise headroom after command submission; a
        // near-green release caps/deflates and cannot label latency at all. The old branch fired
        // immediately on proof frame three (~19% live), making every cold start look like a
        // deliberately ruined shot. Use the earlier of one detected-window width past the first
        // fill and one detected-window width before green entry. The second bound is what keeps a
        // wide window from consuming all of the causal headroom on the measured ~220ms route.
        // Both bounds are current-frame geometry, so this remains shot-type neutral.
        const double calibrationTarget = std::clamp(
            std::min(shot_.minFreshFillPct + greenWidth,
                     greenStart - greenWidth),
            shot_.minFreshFillPct, greenStart);
        shot_.targetPct = calibrationTarget;
        shot_.targetModeAtRelease = QStringLiteral("meter_calibration_headroom_live");
        shot_.latencyValidationTargetPct = -1.0;
        shot_.latencyValidationTolerancePct = -1.0;
        if (shot_.fillPct + 1e-6 < calibrationTarget) {
            shot_.releasePlan = QStringLiteral(
                "Latency calibration / tracking frame-derived headroom target");
            shot_.releaseReason = QStringLiteral("waiting_for_calibration_headroom");
            shot_.releaseReasonCode = QStringLiteral("waiting_for_calibration_headroom");
            return;
        }
        shot_.effectiveLatencyMs = 0.0;
        shot_.releaseEtaMs = 0.0;
        shot_.releasePlan = QStringLiteral("Latency calibration probe");
        shot_.releaseReason = QStringLiteral("latency_calibration_probe");
        shot_.releaseReasonCode = QStringLiteral("latency_calibration_probe");
        if (triggerRelease(now)) {
            clearReleaseOutput(output);
        } else {
            restoreAbortOutput(output);
        }
        return;
    }

    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Name the uncalibrated delayed condition before
    // the first arm of the first delayed shot can happen. Placed here (rather than in
    // setMeterDelayCondition) so it speaks only on a genuinely owned live-meter shot — a delay
    // that ramps while the operator is walking around must not warn — and so the lead it quotes
    // is the one this shot will actually consume. De-duplicated on the applied delay value.
    maybeWarnMeterDelayLeadUncalibrated();

    // Refine the causal plan on every genuine frame, valid or not. This is pure observation: it
    // arms nothing and gates nothing. Calibration probes have already returned above, so only a
    // genuinely owned live-meter shot ever holds a reservation.
    updateAutonomousTipReservation(tipDecision, now);

    if (!tipPredictionValid) {
        // [ORION_METER_DELAY_LEAD_STARVATION] Name the structural cause ONCE per shot, with
        // the two numbers the operator can act on. Without this line the delay regime dies as
        // an anonymous trajectory hold/timeout and the session reads as "bot completely
        // silent" — the 2026-08-08 owner report. Diagnostic + UI signal only: the hold/abort
        // flow below is unchanged and fail-closed (no arm, no release, bounded return of
        // physical control). The prefix deliberately differs from "SHOT LEAD CONFLICT: lead "
        // and "SHOT LEAD CONFLICT: Shot Lead " so existing parsers of those exact lines are
        // untouched.
        if (tipDecision.delayStarvedVisibleSource && !meterDelayStarvedLoggedThisShot_) {
            meterDelayStarvedLoggedThisShot_ = true;
            ++tipLeadConflictMisses_;
            const double leadMs = measuredLeadForActuationMs();
            // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Wording corrected on two counts, both
            // of which the 2026-08-09 mechanism review refuted:
            //   * "the delayed video cannot warn" — the video is NOT delayed. VeniceNetSvc's
            //     WinDivert filter is game-server -> console only (MeterDelayIntercept::
            //     buildInterceptFilter, courtIp validated PUBLIC, WINDIVERT_LAYER_NETWORK_FORWARD);
            //     the Remote Play stream is console -> PC on the local ICS segment and never
            //     matches it. Measured: the meter's own anchor->stop geometry is unchanged under
            //     delay (PHASE SAMPLE 355-380ms on vs 332-372ms off).
            //   * "lower the meter delay to <=Xms" — the applied delay does not consume the
            //     runway; the delayed-condition lead OFFSET does. Lowering the delay while the
            //     lead stays above the ceiling changes nothing: the shot still aborts.
            // The ceiling is real and unchanged — a lead past it cannot be scheduled from any
            // in-video anchor — so the refusal stands; only the arithmetic it hands the operator
            // is now the one that actually moves the failure.
            emit engineDiagnostic(QStringLiteral(
                "SHOT LEAD CONFLICT: meter delay %1ms starves lead %2ms > usable max %3ms; "
                "suppressed %4 arm (tip_eta %5ms) — no in-video anchor can warn %2ms ahead "
                "and pre-anchor extrapolations fire ~300ms early. Offset headroom for this Tip "
                "Timing is +%6ms; lower Shot Lead or the offset (#%7 this session).")
                                      .arg(meterDelayAppliedMs_, 0, 'f', 0)
                                      .arg(leadMs, 0, 'f', 0)
                                      .arg(maxSchedulableTipLeadMs(), 0, 'f', 0)
                                      .arg(predictedTipSource.left(64))
                                      .arg(tipEstimatePresent ? predictedTipMs - now : -1.0,
                                           0, 'f', 0)
                                      .arg(maxMeterDelayForLeadMs(leadMs), 0, 'f', 0)
                                      .arg(tipLeadConflictMisses_));
            emit shotLeadConflictDiagnosed(leadMs, maxSchedulableTipLeadMs(),
                                           tipLeadConflictMisses_);
        }
        // An armed token within one frame gap of firing was set from a VALID prediction.
        // A transient sigma spike (e.g. one frame of registration_far_disagreement) does
        // not change the physical trajectory. Genuinely dangerous states (epoch mismatch,
        // authority expiry, re-lock) are already caught at lines 5117-5126 before we get here.
        if (noteTipPredictionInvalid(now)) {
            shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
            shot_.releaseReason = QStringLiteral("release_scheduled");
            shot_.releaseReasonCode = QStringLiteral("release_scheduled");
            return;
        }
        invalidateUnconfirmedVisionSchedule("tick_prediction_invalid");
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
        if (ownedHold) {
            // A current detector can remain numerically present yet never form an authoritative
            // forward crossing (flat/noisy/relocking lookalike). Bound that unresolved ownership
            // by the existing absolute Button hold ceiling, then restore physical pass-through;
            // never turn the ceiling into a blind release.
            if (heldMs >= std::max(1.0, config_.maxHoldMs)) {
                relinquishAutonomousLiveMeterShot(
                    output, QStringLiteral("live_trajectory_timeout_abort"));
                return;
            }
            holdOwnedSquareUnresolved(
                QStringLiteral("owned_square_trajectory_unresolved"),
                QStringLiteral("Live meter / holding output; fitting genuine trajectory"));
            return;
        }
        const double postMeterElapsedMs = shot_.meterSeenThisShot
            && shot_.firstMeterSeenMs >= 0.0 ? now - shot_.firstMeterSeenMs : 0.0;
        const double trajectoryAbortMs = shot_.mode == ShotMode::GoToStick
            ? config_.gotoMaxHoldMs : config_.maxHoldMs;
        if ((shot_.meterSeenThisShot && postMeterElapsedMs >= trajectoryAbortMs)
            || (!shot_.meterSeenThisShot
                && heldMs >= (shot_.mode == ShotMode::GoToStick
                    ? config_.gotoNoMeterAbortMs : config_.buttonNoMeterAbortMs))) {
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("live_trajectory_timeout_abort"));
            return;
        }
        shot_.releasePlan = QStringLiteral("Live meter / fitting trajectory");
        shot_.releaseReason = QStringLiteral("waiting_for_live_trajectory");
        shot_.releaseReasonCode = QStringLiteral("waiting_for_live_trajectory");
        return;
    }

    // The prediction is authoritative again -- end any in-progress transient so a LATER blink
    // gets a full fresh cadence of grace rather than inheriting this one's elapsed time.
    noteTipPredictionValid();

    // [ORION_TIP_PHASE_IMMINENT] Do not let the SAMPLER win a race the phase member is about to
    // win anyway.
    //
    // MEASURED, 2026-08-04 (54 shots, two batches). 52 released on the phase member at a median
    // 170 ms after the meter appeared and a median fill of 42.6. Exactly ONE released on the
    // sampler: a Go-To that fired 55 ms after the meter appeared, on FOUR samples, at fill 29.3 --
    // below the 30 anchor, so no crossing had been observed and the phase member did not yet
    // exist. The meter had appeared at 18.3 and was climbing at 0.227 %/ms, so it would have
    // crossed the anchor about 3 ms later. The sampler's deadline beat the anchor by one frame.
    //
    // The cost of losing that race is not a slightly worse estimate, it is the WORST regime the
    // sampler has: below the anchor it is extrapolating furthest, on the fewest samples, carrying
    // the +61-92 ms bias the phase member exists to replace. That shot released at 29.3 into a
    // green window starting at 95.4 and the meter topped out at 77.5 -- a giveaway, not a shot.
    //
    // So: while the meter is still climbing toward an anchor it is about to reach, hold rather
    // than release on a non-phase estimate. The hold is bounded three ways and cannot become a
    // blind release or a late command:
    //   * only inside holdBand pp BELOW the lowest ladder level -- i.e. the anchor is ~1-2 frames
    //     away. A meter stalled well below the anchor is untouched and aborts exactly as before.
    //   * only while RISING. A flat or falling meter will never reach the anchor, so waiting for
    //     one would be waiting forever.
    //   * only owned Square, under the same absolute maxHoldMs ceiling every other
    //     holdOwnedSquareUnresolved() site uses, so an anchor that never arrives still returns
    //     physical control at the ceiling.
    // It creates no token and no command, so it cannot manufacture an overtime packet. Worst case
    // it converts a release the measurement says is a giveaway into an abort.
    if (config_.tipPhaseEnabled && ownedHold
        && !predictedTipSource.startsWith(QStringLiteral("phase"))
        && shot_.fillPhaseAnchorMs < 0.0
        && heldMs < std::max(1.0, config_.maxHoldMs)
        && phaseAnchorImminent(shot_.fillPct, fit.slopePctPerMs)) {
        // The level the hold is actually waiting for. With tipPhaseRungImminentHold OFF this is
        // the base anchor -- the lowest level a rising meter below it can still reach; the
        // ladder rungs only matter for meters first seen ABOVE it. With the flag ON it is the
        // lowest witnessable rung above the fill ([ORION_RUNG_IMMINENT]); phaseAnchorImminent()
        // just proved it finite and within the band, so the log and the ETA stay truthful.
        const double nextLevelPct = phaseAnchorImminentTargetLevelPct(shot_.fillPct);
        const double belowByPct = nextLevelPct - shot_.fillPct;
        {
            emit engineDiagnostic(QStringLiteral(
                "TIP PHASE IMMINENT HOLD: source=%1 fill_pct=%2 anchor_pct=%3 below_by_pct=%4 "
                "slope_pct_ms=%5 eta_to_anchor_ms=%6 fit_n=%7 held_ms=%8")
                                      .arg(predictedTipSource.left(64))
                                      .arg(shot_.fillPct, 0, 'f', 2)
                                      .arg(nextLevelPct, 0, 'f', 1)
                                      .arg(belowByPct, 0, 'f', 2)
                                      .arg(fit.slopePctPerMs, 0, 'f', 4)
                                      .arg(belowByPct / fit.slopePctPerMs, 0, 'f', 1)
                                      .arg(fit.n)
                                      .arg(heldMs, 0, 'f', 1));
            holdOwnedSquareUnresolved(
                QStringLiteral("owned_square_phase_anchor_imminent"),
                QStringLiteral("Live meter / holding output; phase anchor one frame away"));
            return;
        }
    }

    // Sole production lead: the current route's command-to-visible-effect latency. Both the
    // sampler and registration predictor now live on the capture-aligned engine clock, so frame
    // age is already represented in predictedTipMs and must not be added a second time.
    const double effectiveLeadMs = measuredLeadForActuationMs();
    // [ORION_GREEN_CENTER] predictedTipMs targets fill=100.0, which measurement shows is the LATE
    // edge of the green window on 130/130 releases. Firing this much earlier moves the landing off
    // that edge and into the window. 0.0 unless explicitly enabled, so the default build schedules
    // exactly as before.
    const double greenCenterOffsetMs = autonomousGreenCenterOffsetMs(fit.slopePctPerMs);
    const double fireAtMs = predictedTipMs - effectiveLeadMs - greenCenterOffsetMs;
    shot_.effectiveLatencyMs = effectiveLeadMs;
    shot_.releaseEtaMs = fireAtMs - now;
    shot_.tipPredictionDeadlineMs = fireAtMs;
    shot_.tipPredictionDeadlineLatenessMs = std::max(0.0, now - fireAtMs);

    if (fireAtMs <= now) {
        // The CURRENT tick's prediction says the deadline is past, but the ARMED token
        // may still be in the future (set from a previous tick's valid prediction).
        //
        // In THIS branch no replacement token can exist: scheduleFire() rejects any
        // deadline <= now, so once the current estimate's fireAt is already past there is
        // nothing left to re-arm. Destroying the armed token therefore cannot improve the
        // shot -- it can only convert a release into an abort. That is exactly what the
        // 2026-08-03 batch measured: epoch 6 was torn down 17.6 ms before its own deadline
        // (one frame gap is ~16.7 ms, so the old `<= gapMs` bound missed it by <1 ms) on an
        // estimate that had moved by 0.4-3.9 ms against a predictor sigma of ~36 ms.
        //
        // So the bound here is simply "is the armed deadline still ahead of us". This
        // changes WHETHER we cancel, never WHEN we fire: the token fires at its own armed
        // deadline, which scheduleFire() proved was future and inside its frame+lead lease
        // when it armed. No new command is created and no overtime packet is ever emitted.
        // Fail-closed is untouched: with no armed token, or one whose deadline has actually
        // passed, we fall through to the miss accounting below exactly as before.
        if (schedFireDeadlineMs_ >= 0.0) {
            const double untilArmedMs = schedFireDeadlineMs_ - now;
            if (untilArmedMs > 0.0) {
                shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
                shot_.releaseReason = QStringLiteral("release_scheduled");
                shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                return;
            }
            // [ORION_INFLIGHT_TOKEN] The armed token's own deadline has passed. That is NOT
            // proof the shot was lost -- the precise-fire worker does not press exactly ON the
            // deadline, it spins toward the target and its physical edge lands a jittery moment
            // later. The old code fenced the token here on the very first tick past the deadline,
            // and that fence is OrionPreciseFireThread::disarm(), which sets `aborted_` and is
            // honoured by the fire loop MID-SPIN. So the engine cancelled the press it was
            // waiting for, ~1 ms before it landed, and then reported the shot as missed.
            //
            // 2026-08-04 live batch: 4 of the 8 live_tip_deadline_missed aborts were exactly this
            // (aborted 0.2 / 1.3 / 1.8 / 2.4 ms past their own armed deadline, all inside the 8 ms
            // schedulerGraceMs the design already grants). They were invisible because the kill
            // reporter only spoke when until_armed_ms was positive.
            //
            // Give the submit its full, already-specified grace. consumeDueScheduledFire() before
            // the grace expires can ONLY fire a token the worker has CONFIRMED, and it fires it at
            // schedFireActualMs_ -- the worker's true submit instant -- so nothing here creates a
            // command, moves a deadline, or emits an overtime packet. Once the grace is spent,
            // armedTokenSubmitInFlight() goes false and we fall through to the identical fence +
            // abort below.
            if (armedTokenSubmitInFlight(now)) {
                if (consumeDueScheduledFire(output, now)) {
                    return;
                }
                shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
                shot_.releaseReason = QStringLiteral("release_scheduled");
                shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                return;
            }
        }
        // Capture the armed token's standing BEFORE the fence clears it, so the miss report can
        // say whether this shot died holding a token and how far past that token's own deadline
        // it had run. Without this the single most important fact about a miss is unrecoverable
        // from the log.
        const double armedTokenEtaAtMissMs = schedFireDeadlineMs_ >= 0.0
            ? schedFireDeadlineMs_ - now : std::numeric_limits<double>::quiet_NaN();
        invalidateUnconfirmedVisionSchedule("tick_fire_at_past");
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
        // [ORION_CONTESTED_DEADLINE] Everything above this point is unchanged, including both
        // in-flight-token holds and the fence: a confirmed submit still wins, and a stale token is
        // still revoked. What changes is only the TERMINAL action when the "missed" deadline was
        // computed from the one estimate the engine has already rated far-disagreeing, while the
        // detector's own fill and slope independently prove runway remains.
        //
        // Keep waiting for a trustworthy estimate. No release, no token, no command: this branch
        // creates nothing and can therefore never emit an overtime packet. The wait is bounded by
        // the SAME absolute Button hold ceiling the sibling unresolved-trajectory path uses
        // (line ~5985), so a contested estimate that never resolves still returns physical control
        // -- it just does so at the ceiling instead of on the first far-disagreeing frame.
        //
        // Restricted to owned Square exactly as every other holdOwnedSquareUnresolved() site is;
        // stick modes keep their existing abort. This is the mode the ship blocker and live
        // epoch 5 are in, and narrowing it keeps the revert surface a single predicate.
        if (ownedHold
            && heldMs < std::max(1.0, config_.maxHoldMs)
            && contestedTipDeadlineOutrunByMeasuredRunway(
                   predictedTipSource, fit, shot_.fillPct, effectiveLeadMs)) {
            emit engineDiagnostic(QStringLiteral(
                "TIP DEADLINE DECISION: disposition=contested_runway_remains source=%1 "
                "tip_eta_ms=%2 command_eta_ms=%3 lead_ms=%4 predictor_sigma_ms=%5 "
                "fill_pct=%6 meter_slope_pct_ms=%7 meter_runway_ms=%8 fit_n=%9 held_ms=%10")
                                      .arg(predictedTipSource.left(64))
                                      .arg(predictedTipMs - now, 0, 'f', 3)
                                      .arg(fireAtMs - now, 0, 'f', 3)
                                      .arg(effectiveLeadMs, 0, 'f', 3)
                                      .arg(predictedTipSigmaMs, 0, 'f', 3)
                                      .arg(shot_.fillPct, 0, 'f', 2)
                                      .arg(fit.slopePctPerMs, 0, 'f', 4)
                                      .arg((100.0 - shot_.fillPct)
                                               / std::max(1e-9, fit.slopePctPerMs),
                                           0, 'f', 3)
                                      .arg(fit.n)
                                      .arg(heldMs, 0, 'f', 1));
            holdOwnedSquareUnresolved(
                QStringLiteral("owned_square_contested_tip_estimate"),
                QStringLiteral("Live meter / holding output; far-disagreeing estimate, "
                               "measured runway remains"));
            return;
        }
        // An already-submitted precise-fire token is consumed above. Without one,
        // a command deadline in the past is physically unrecoverable: submitting
        // now would make the visible release late by exactly now-fireAtMs. Never
        // disguise that guaranteed overtime packet as a successful live-tip edge.
        const double latenessMs = std::max(0.0, now - fireAtMs);
        // Attribute the miss to the subsystem that actually caused it. The ordering matters:
        // classify on whether a genuine OPPORTUNITY ever existed (valid decision AND deadline
        // still ahead), not on whether the decision was ever valid at all.
        //
        //   unschedulable_lead              the deadline was NEVER ahead of us, not even at
        //                                   ownership -> the LEAD exceeds this shot's whole
        //                                   window. Look at the route prior.
        //   deadline_missed_before_validity the deadline WAS ahead of us, but the estimate never
        //                                   became authoritative while it still was -> the
        //                                   predictor / sigma-vs-horizon gate. THIS is the
        //                                   2026-08-03 signature: fireAt was +147ms at ownership,
        //                                   and validity did not arrive until it was already past.
        //   never_inside_arming_window      the deadline was valid and ahead of us, but never once
        //                                   inside the window scheduleFire() accepts -> it stepped
        //                                   over the window between two frames. THIS is the
        //                                   2026-08-04 signature and it was previously invisible:
        //                                   106 of 115 aborts reported authority_lost_before_submit
        //                                   while 32 of 42 had never armed a token at all.
        //   authority_lost_before_submit    we genuinely had an ARMABLE deadline -- valid, ahead,
        //                                   and inside the window -- and did not submit -> a lease,
        //                                   route or controller transition took it away.
        //
        // Do NOT reintroduce `everValid` here. The abort block only runs when the decision IS
        // valid and the per-frame refinement already ran earlier in this same tick, so everValid
        // is unconditionally true at this point; using it collapses every miss onto
        // authority_lost_before_submit and makes deadline_missed_before_validity dead code.
        // everValidAndFuture has the same defect one level down -- it is true of essentially every
        // shot, because every deadline is valid and future for the whole approach. Only
        // everArmable narrows to "a tick existed on which scheduleFire() could have said yes".
        const bool hadReservation = tipReservation_.active;
        const double reservationAgeMs = hadReservation && tipReservation_.createdMs >= 0.0
            ? now - tipReservation_.createdMs : -1.0;
        const QString reservationDisposition = !hadReservation
            ? QStringLiteral("no_reservation")
            : (tipReservation_.everArmable
                   ? QStringLiteral("authority_lost_before_submit")
                   : (tipReservation_.everValidAndFuture
                          ? QStringLiteral("never_inside_arming_window")
                          : (tipReservation_.everFuture
                                 ? QStringLiteral("deadline_missed_before_validity")
                                 : QStringLiteral("unschedulable_lead"))));
        emit engineDiagnostic(QStringLiteral(
            "TIP DEADLINE DECISION: disposition=rejected_missed source=%1 "
            "tip_eta_ms=%2 command_eta_ms=%3 lateness_ms=%4 "
            "lead_kind=%5 lead_ms=%6 lead_sd_ms=%7 predictor_sigma_ms=%8 "
            "fill_pct=%9 frame_age_ms=%10 reservation_disposition=%11 "
            "reservation_age_ms=%12 reservation_updates=%13 reservation_first_fill=%14 "
            "reservation_first_tip_eta_ms=%15 reservation_ever_armable=%16 "
            "reservation_promoted=%17 arming_horizon_ms=%18 authority_eta_ms=%19 "
            // [ORION_LEAD_CONFLICT] APPEND-ONLY (key=value parsers keep every position).
            // lead_source=user|seed|authority|none names where lead_ms actually came from;
            // lead_kind above names only the AUTHORITY, which the in-band Shot Lead replaces.
            // The 2026-08-08 production log was misread precisely because
            // lead_kind=factory/validated dressed a user-set 320 ms as an estimator product.
            "armed_token_eta_ms=%20 grace_ms=%21 lead_source=%22")
                                  .arg(predictedTipSource.left(64))
                                  .arg(predictedTipMs - now, 0, 'f', 3)
                                  .arg(fireAtMs - now, 0, 'f', 3)
                                  .arg(latenessMs, 0, 'f', 3)
                                  .arg(measuredLatencyAuthorityKind_.left(16))
                                  .arg(effectiveLeadMs, 0, 'f', 3)
                                  .arg(effectiveLeadSdMs(), 0, 'f', 3)
                                  .arg(predictedTipSigmaMs, 0, 'f', 3)
                                  .arg(shot_.fillPct, 0, 'f', 2)
                                  .arg(shot_.frameAgeMs, 0, 'f', 2)
                                  .arg(reservationDisposition)
                                  .arg(reservationAgeMs, 0, 'f', 3)
                                  .arg(hadReservation ? tipReservation_.updates : 0)
                                  .arg(hadReservation ? tipReservation_.firstFillPct : -1.0,
                                       0, 'f', 2)
                                  .arg(hadReservation && tipReservation_.firstTipAbsMs > 0.0
                                           ? tipReservation_.firstTipAbsMs
                                                 - tipReservation_.createdMs
                                           : -1.0,
                                       0, 'f', 3)
                                  .arg(hadReservation && tipReservation_.everArmable ? 1 : 0)
                                  .arg(hadReservation && tipReservation_.promoted ? 1 : 0)
                                  .arg(adaptiveAutonomousSchedulerHorizonMs(), 0, 'f', 3)
                                  .arg(meterAuthorityExpiryMs() >= 0.0
                                           ? meterAuthorityExpiryMs() - now : -1.0,
                                       0, 'f', 3)
                                  // NaN => this shot reached its miss holding NO armed token.
                                  // A negative value is the [ORION_INFLIGHT_TOKEN] signature:
                                  // a token existed and we ran past its own deadline by that
                                  // much before giving up. It must never again be smaller in
                                  // magnitude than grace_ms.
                                  .arg(armedTokenEtaAtMissMs, 0, 'f', 3)
                                  .arg(config_.schedulerGraceMs, 0, 'f', 3)
                                  .arg(actuationLeadSourceLabel(effectiveLeadMs)));
        // [ORION_LEAD_CONFLICT] When the miss is the structural lead-vs-tip-timing impossibility
        // rather than a transient, say WHY in one short line that leads with the two numbers the
        // user can act on -- the full decision line above carries every term but is routinely
        // truncated by the Activity view. Diagnostic-only: the abort below is unchanged and the
        // fail-closed contract (no command after a missed deadline, ever) is untouched.
        if (config_.tipPhaseEnabled && effectiveLeadMs > maxSchedulableTipLeadMs()) {
            ++tipLeadConflictMisses_;
            // [ORION_METER_DELAY_LEAD_STARVATION] APPEND-ONLY delay context (base sentence
            // byte-identical for existing parsers): with a delay applied this miss is the
            // delay arithmetic, and the actionable ceiling is the max delay, not the lead.
            // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Offset headroom, not "max meter delay":
            // see the config-time advisory above for why the old number was inflated by the
            // applied delay whenever the operator had not folded it into Shot Lead by hand.
            const QString delayContext = meterDelayAppliedMs_ > 0.0
                ? QStringLiteral(" Meter delay %1ms is applied; delayed-condition offset "
                                 "headroom for this lead/Tip Timing is +%2ms.")
                      .arg(meterDelayAppliedMs_, 0, 'f', 0)
                      .arg(maxMeterDelayForLeadMs(effectiveLeadMs), 0, 'f', 0)
                : QString();
            emit engineDiagnostic(QStringLiteral(
                "SHOT LEAD CONFLICT: lead %1ms > usable max %2ms (tip timing %3ms); shot aborted "
                "(#%4 this session). Lower Shot Lead or raise/reset Tip Timing.%5")
                                      .arg(effectiveLeadMs, 0, 'f', 0)
                                      .arg(maxSchedulableTipLeadMs(), 0, 'f', 0)
                                      .arg(effectiveTipPhaseConstantMs(), 0, 'f', 0)
                                      .arg(tipLeadConflictMisses_)
                                      .arg(delayContext));
            emit shotLeadConflictDiagnosed(effectiveLeadMs, maxSchedulableTipLeadMs(),
                                           tipLeadConflictMisses_);
        }
        relinquishAutonomousLiveMeterShot(
            output, QStringLiteral("live_tip_deadline_missed"));
        return;
    }

    // Until the worker confirms a physical submit, every genuine frame replaces
    // the copied deadline in either direction. Keeping an earlier deadline after
    // a real deceleration would make old trajectory state release the shot.
    if (schedFireDeadlineMs_ >= 0.0) {
        // An armed token that is due within one frame interval MUST NOT be revoked.
        //
        // This was the single largest loss in the 2026-08-03 batch. 24 of 34 owned shots armed a
        // precise-fire token; only 14 survived. The revoke test below was an exact-equality
        // comparison (1e-6 ms), and the tip estimate moves 54-103 ms per frame, so essentially
        // EVERY fresh frame revoked the pending token and tried to re-arm. In 9 of 10 measured
        // cases the destroyed token was still 1.7-16.1 ms from firing -- inside one frame gap, so
        // no newer estimate could possibly have replaced it in time. Revoking there cannot
        // improve the shot; it can only convert a release into an abort, which is exactly what it
        // did (median lateness of the resulting aborts: 6.8 ms, against a predictor sigma of
        // 43-63 ms the same decision had just accepted as valid).
        //
        // Note this changes WHETHER we cancel, never WHEN we fire: the token still fires at its
        // own armed deadline, which was in the future when armed and is still in the future now.
        // No late command is created, and the fail-closed contract below is untouched.
        const double gapMs = std::isfinite(genuineFrameGapEmaMs_) && genuineFrameGapEmaMs_ >= 1.0
            ? std::clamp(genuineFrameGapEmaMs_, 8.0, 29.0)
            : 1000.0 / 60.0;
        const double untilArmedMs = schedFireDeadlineMs_ - now;
        const bool irreplaceable = untilArmedMs > 0.0 && untilArmedMs <= gapMs;
        // [ORION_INFLIGHT_TOKEN] A token whose deadline has just passed is being pressed right
        // now. Reaching this line at all means the CURRENT estimate is still future, so the
        // fire-at-past branch above did not run -- but the armed token is nonetheless due, and
        // fencing it to "refine" a deadline it is already executing destroys the shot. Consume a
        // confirmed submit if the worker has landed one; otherwise hold it untouched for the
        // remainder of its grace.
        if (armedTokenSubmitInFlight(now)) {
            if (consumeDueScheduledFire(output, now)) {
                return;
            }
            shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
            shot_.releaseReason = QStringLiteral("release_scheduled");
            shot_.releaseReasonCode = QStringLiteral("release_scheduled");
            return;
        }
        // [ORION_DEADLINE_DRIFT] Exact equality (1e-6 ms) here meant "reschedule unless the
        // deadline is bit-identical", and the fused deadline is NOT bit-stable between frames:
        // canonicalAutonomousTipDecision(now) re-derives the sampler sigma from the horizon
        // `crossing - now`, which shrinks every tick, so the inverse-variance fusion re-weights
        // and the deadline wobbles a fraction of a millisecond on ticks where nothing whatsoever
        // was observed. Measured on the 2026-08-04 batch: 298 of 373 reschedule kills had NO new
        // frame between them, moving the deadline p50 0.32 ms / p90 0.95 ms. Each one is a
        // synchronous cross-thread fence plus a full worker re-arm, and it resets the worker's
        // lead time to under a single 4 ms tick.
        //
        // Comparing against the token's FIXED armed deadline means this tolerance bounds the
        // total divergence from the newest estimate at 1 ms -- it cannot accumulate tick over
        // tick -- while a genuine frame-driven refinement (p50 21.6 ms) still reschedules.
        //
        // [ORION_SLOW_METER_DEFER] Third keep condition (flag-gated, default OFF): never tear
        // down an armed token to REPLACE it with a candidate deadline that undercuts the phase
        // member's own deadline by more than the measured-gap threshold. This is the seq=7
        // 2026-08-06 kill: token armed from phase at command_eta 125.9, then a 13ms sampler
        // swing (crossing 400 -> 325 on an n=4 fit) demoted phase and re-armed at command_eta
        // 24.9 -- 88.7ms EARLIER than the phase deadline the meter actually kept. Keeping the
        // token changes WHETHER we reschedule earlier, never WHEN the kept token fires: it
        // fires at its own proven-future armed deadline, so no earlier command and no overtime
        // packet can be created here. Candidates at/after the armed deadline are untouched --
        // rescheduling LATER remains exactly as free as it is today.
        // [ORION_DEV_FIRE_OFFSET] Both re-arm decisions below compare the fresh candidate against
        // the UNDISPLACED armed deadline (equal to schedFireDeadlineMs_ whenever the hook is
        // disarmed, offset 0): comparing against a displaced deadline would read the commanded
        // offset as per-tick drift and churn invalidate/re-arm every 4ms tick for the whole shot.
        const double armedBaseMs = schedFireDeadlineMs_ - schedFireAppliedDevOffsetMs_;
        const bool deferEarlierCandidate = ownedHold
            && fireAtMs + 1e-6 < armedBaseMs
            && slowMeterDeferBinds(tipDecision, fireAtMs, effectiveLeadMs, now);
        if (deferEarlierCandidate && slowMeterDeferKeepLoggedToken_ != schedFireToken_) {
            slowMeterDeferKeepLoggedToken_ = schedFireToken_;
            emit engineDiagnostic(QStringLiteral(
                "SLOW METER DEFER: disposition=reschedule_refused source=%1 "
                "command_eta_ms=%2 armed_eta_ms=%3 phase_command_eta_ms=%4 undercut_ms=%5 "
                "threshold_ms=%6 lead_ms=%7 fill_pct=%8 vel_pct_ms=%9 fit_n=%10")
                                      .arg(predictedTipSource.left(64))
                                      .arg(fireAtMs - now, 0, 'f', 3)
                                      .arg(schedFireDeadlineMs_ - now, 0, 'f', 3)
                                      .arg(tipDecision.phaseTipAbsMs - effectiveLeadMs - now,
                                           0, 'f', 3)
                                      .arg(tipDecision.phaseTipAbsMs - effectiveLeadMs
                                               - fireAtMs, 0, 'f', 3)
                                      .arg(config_.slowMeterDeferUndercutMs, 0, 'f', 1)
                                      .arg(effectiveLeadMs, 0, 'f', 3)
                                      .arg(shot_.fillPct, 0, 'f', 2)
                                      .arg(fit.slopePctPerMs, 0, 'f', 4)
                                      .arg(fit.n));
        }
        if (std::abs(fireAtMs - armedBaseMs) <= tokenDeadlineDriftToleranceMs()
            || irreplaceable || deferEarlierCandidate) {
            shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
            shot_.releaseReason = QStringLiteral("release_scheduled");
            shot_.releaseReasonCode = QStringLiteral("release_scheduled");
            return;
        }
        invalidateUnconfirmedVisionSchedule("tick_reschedule");
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
    }
    // [ORION_SLOW_METER_DEFER] Fresh-arm refusal (flag-gated, default OFF): do not CREATE a
    // token at a deadline that undercuts the phase member's by more than the threshold. This is
    // the seq=52/seq=8 2026-08-06 kill: with no token armed yet, a transient sampler swing on an
    // n=4 fit demoted the phase member via the corroboration veto and armed at command_eta
    // 4.6/27.9ms while the phase member said 98-132ms; the swing reverted within one frame but
    // the token was already irreplaceable and fired 90-116ms early in meter phase (the owner's
    // counted earlies). DEFER instead: hold the owned output and let the very next ticks arm at
    // the phase deadline when the transient passes (measured: it passed within 8-16ms on all
    // three). Bounded exactly like every sibling hold -- owned Square only, under the absolute
    // maxHoldMs ceiling -- and it creates no token and no command, so it cannot manufacture an
    // overtime packet. If nothing ever becomes schedulable the existing fire-at-past /
    // rejected_missed / trajectory-timeout machinery returns pass-through: the failure
    // direction is "later, or no fire", never earlier.
    if (ownedHold
        && heldMs < std::max(1.0, config_.maxHoldMs)
        && slowMeterDeferBinds(tipDecision, fireAtMs, effectiveLeadMs, now)) {
        if (shot_.releaseReasonCode != QLatin1String("owned_square_slow_meter_defer")) {
            emit engineDiagnostic(QStringLiteral(
                "SLOW METER DEFER: disposition=arm_deferred source=%1 command_eta_ms=%2 "
                "phase_command_eta_ms=%3 undercut_ms=%4 threshold_ms=%5 lead_ms=%6 "
                "fill_pct=%7 vel_pct_ms=%8 fit_n=%9 held_ms=%10")
                                      .arg(predictedTipSource.left(64))
                                      .arg(fireAtMs - now, 0, 'f', 3)
                                      .arg(tipDecision.phaseTipAbsMs - effectiveLeadMs - now,
                                           0, 'f', 3)
                                      .arg(tipDecision.phaseTipAbsMs - effectiveLeadMs
                                               - fireAtMs, 0, 'f', 3)
                                      .arg(config_.slowMeterDeferUndercutMs, 0, 'f', 1)
                                      .arg(effectiveLeadMs, 0, 'f', 3)
                                      .arg(shot_.fillPct, 0, 'f', 2)
                                      .arg(fit.slopePctPerMs, 0, 'f', 4)
                                      .arg(fit.n)
                                      .arg(heldMs, 0, 'f', 1));
        }
        holdOwnedSquareUnresolved(
            QStringLiteral("owned_square_slow_meter_defer"),
            QStringLiteral("Live meter / holding output; candidate deadline undercuts "
                           "the phase schedule"));
        return;
    }
    if (scheduleFire(fireAtMs, now, -1.0,
                     ScheduledFireAuthority::AutonomousMeterVision)) {
        schedFirePlan_ = QStringLiteral("Live meter tip");
        schedFireReason_ = QStringLiteral("live_meter_tip");
        schedFireCode_ = QStringLiteral("live_meter_tip");
        // [ORION_ARMED_SOURCE] Attribution snapshot of the decision that created THIS token
        // (same source/sigma the promotion line below logs). A re-arm overwrites it, so the
        // snapshot always describes the token that actually fires.
        schedFireArmedSource_ = predictedTipSource.left(64);
        schedFireArmedSigmaMs_ = predictedTipSigmaMs;
        schedFireArmedFillPct_ = shot_.fillPct;
        schedFireArmedCommandEtaMs_ = fireAtMs - now;
        shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
        shot_.releaseReason = QStringLiteral("release_scheduled");
        shot_.releaseReasonCode = QStringLiteral("release_scheduled");
        // The reservation has become a real armed token. Log the promotion exactly once so the
        // ownership -> reservation -> submission chain is one traceable identity in the log.
        if (tipReservation_.active && !tipReservation_.promoted) {
            tipReservation_.promoted = true;
            emit engineDiagnostic(QStringLiteral(
                "TIP RESERVATION: disposition=reservation_promoted source=%1 "
                "command_eta_ms=%2 lead_kind=%3 lead_ms=%4 predictor_sigma_ms=%5 "
                "fill_pct=%6 physical_epoch=%7 shot_attempt=%8 schedule_token=%9 "
                "reservation_age_ms=%10 reservation_updates=%11 first_fill=%12")
                                      .arg(predictedTipSource.left(64))
                                      .arg(fireAtMs - now, 0, 'f', 3)
                                      .arg(measuredLatencyAuthorityKind_.left(16))
                                      .arg(effectiveLeadMs, 0, 'f', 3)
                                      .arg(predictedTipSigmaMs, 0, 'f', 3)
                                      .arg(shot_.fillPct, 0, 'f', 2)
                                      .arg(tipReservation_.physicalShotEpoch)
                                      .arg(tipReservation_.armToken)
                                      .arg(schedFireToken_)
                                      .arg(tipReservation_.createdMs >= 0.0
                                               ? now - tipReservation_.createdMs : -1.0,
                                           0, 'f', 3)
                                      .arg(tipReservation_.updates)
                                      .arg(tipReservation_.firstFillPct, 0, 'f', 2));
        }
    } else {
        shot_.releasePlan = QStringLiteral("Live meter / tracking tip");
        shot_.releaseReason = QStringLiteral("waiting_for_live_tip_deadline");
        shot_.releaseReasonCode = QStringLiteral("waiting_for_live_tip_deadline");
    }
}

void AutomationEngine::processHolding(ControllerState& output, double now)
{
    forceHeldOutput(output);
    shot_.holdFrames++;

    if (shot_.mode == ShotMode::GoToStick && lastPhysical_.cross()) {
        relinquishAutonomousLiveMeterShot(
            output, QStringLiteral("goto_competing_cross_abort"));
        return;
    }

    // Go-To LS-cancel: sustained left-stick movement changes intent before the
    // flick commits. Square-triggered Button and Tempo modes never enter here.
    if (lsCancelRequested()) {
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        output.rightStickY = 0;
        abort(QStringLiteral("ls_cancel"));
        return;
    }

    if (autonomousLiveMeterTimingEnabled()) {
        processAutonomousLiveMeterHolding(output, now);
        return;
    }

    const bool isGoto = shot_.mode == ShotMode::GoToStick;
    // A Fade (Left/Right/Post/Back/Front) is a FAST feedforward type whose real appear->tip
    // (~340-380ms) + rise (~0.28%/ms) + learned per-type offset (e.g. Right Fade +110ms) differ
    // sharply from the standstill-dominated GLOBAL clock. With a fade's ~2pp window (vs a
    // standstill's ~12pp) the global clock's error is not absorbed and the fade fires ~90-130ms
    // EARLY. Exempt fades from the global clock exactly as Go-To is (see the clock-selection site
    // below): they fall back to their per-type shot_type_* clocks/offset. Keyed on the classifier
    // labels ("Left Fade"/"Right Fade"/"Post Fade"/"Back Fade"/"Front Fade") via a substring match.
    const bool isFade = shot_.shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive);
    const double elapsed = shot_.holdStartMs > 0.0 ? now - shot_.holdStartMs : 0.0;
    const double noMeterAbortMs = isGoto
        ? (config_.gotoMeterWait
               ? std::max(config_.gotoNoMeterAbortMs, config_.gotoMeterWaitCapMs)
               : config_.gotoNoMeterAbortMs)
        : config_.buttonNoMeterAbortMs;

    // Release authority is stricter than display freshness: the latest payload must
    // be a genuinely detected UNIQUE frame. A memory echo may bridge UI continuity,
    // but it cannot authorize a vision deadline. Learned clocks and the bounded
    // physical hard caps are explicitly separate and remain available below.
    const double freshWindowMs = config_.meterFreshWindowMs
        * (wifiMode_ ? config_.wifiFreshnessFactor : 1.0);
    const bool meterFresh = shot_.meterDetected && shot_.lastDetectionMs > 0.0
        && (now - shot_.lastDetectionMs) <= freshWindowMs;
    const bool genuineVisionFrame = meterFresh && shot_.lastSampleGenuineAccept
        && meterReleaseAuthorityCurrent(now);

    // A still-unconfirmed VISION schedule loses authority as soon as the latest
    // payload is stale/memory/duplicate or the detector re-locked into a new
    // trajectory epoch. Do this before consuming the pending deadline. If the fire
    // thread already submitted, its confirmation wins: the physical edge happened
    // and must be consumed exactly once rather than forgotten and repeated later.
    refreshScheduledFireAuthorityLease();
    if (schedFireDeadlineMs_ >= 0.0 && schedFireRequiresGenuineFrame_) {
        const bool confirmed = schedFireConfirmedToken_ == schedFireToken_
            && schedFireActualMs_ >= 0.0;
        const bool wrongEpoch = schedFireVisionEpoch_ != shot_.visionEpoch;
        const bool authorityExpired = schedFireAuthorityExpiryMs_ >= 0.0
            && now > schedFireAuthorityExpiryMs_ + 1e-6;
        const double gapMs = cleanSourceGapMs();
        const double untilArmedMs = schedFireDeadlineMs_ - now;
        // [ORION_ROLLING_LEASE] An imminent token is protected from a transient detector blink, but
        // NEVER from an expired or uncovered authority. Two distinct fail-closed cases:
        //
        //   authorityExpired  -- `now` is already past the rolling lease. Under the old arm-time
        //                        check this could not happen with the deadline still ahead (arming
        //                        required deadline <= expiry), so exempting it from the bypass is a
        //                        no-op for every token the old code could create. It is load-bearing
        //                        for the early-armed tokens the rolling lease now permits.
        //   uncovered         -- the deadline sits beyond the rolling lease and the token is about
        //                        to become irreplaceable. No later frame can rescue it inside one
        //                        cadence, so it can never be authorized: abort instead of firing on
        //                        evidence that will be stale at the fire instant.
        const bool uncoveredAtCommit = !scheduledFireAuthorityCoversDeadline()
            && untilArmedMs > 0.0 && untilArmedMs <= gapMs;
        if (!confirmed && (authorityExpired || uncoveredAtCommit)) {
            invalidateUnconfirmedVisionSchedule(
                authorityExpired ? "hold_authority_expired" : "hold_authority_uncovered");
        } else if (!confirmed && (!genuineVisionFrame || wrongEpoch)) {
            if (untilArmedMs <= 0.0 || untilArmedMs > gapMs) {
                invalidateUnconfirmedVisionSchedule("hold_authority_fence");
            }
        }
    }
    if (schedFireDeadlineMs_ >= 0.0 && schedFireRequiresPoseFrame_) {
        const bool confirmed = schedFireConfirmedToken_ == schedFireToken_
            && schedFireActualMs_ >= 0.0;
        const bool wrongEpoch = schedFirePoseEpoch_ != pose_.authorityEpoch;
        const bool wrongArmToken = !poseArmTokenMatches(
            shot_.armToken, schedFirePoseArmToken_)
            || !poseArmTokenMatches(shot_.armToken, pose_.acceptedArmToken);
        const bool authorityExpired = schedFireAuthorityExpiryMs_ >= 0.0
            && now > schedFireAuthorityExpiryMs_ + 1e-6;
        if (!confirmed && (!poseReleaseAuthorityCurrent(now) || wrongEpoch
                           || wrongArmToken || authorityExpired)) {
            invalidateUnconfirmedPoseSchedule();
        }
    }

    // A sub-tick fire is armed for this shot. Either the controller's precise fire thread
    // already submitted the release output and confirmed (catch the engine state up at the
    // ACTUAL fire time), or the grace expired without a confirm (no fire thread / it missed
    // — fire in-tick as the engine always has), or the deadline is still pending (keep
    // holding; the decision is committed, so no re-evaluation can move it).
    if (consumeDueScheduledFire(output, now)) {
        return;
    }

    // Schedule-independent no-meter hard cap.  This must run before the generic
    // pending-schedule early return below; otherwise a copied pose token can keep
    // the shot held forever.  The direct invalidation fence either disarms the
    // worker or confirms that its physical edge already won, which we then consume.
    if (config_.noMeterEnabled && elapsed >= noMeterAbortMs + 1000.0) {
        invalidateUnconfirmedSchedule();
        if (consumeDueScheduledFire(output, now)) {
            return;
        }
        shot_.releasePlan = QStringLiteral("Abort (pose hard cap)");
        shot_.releaseReason = QStringLiteral("pose_hard_cap");
        shot_.releaseReasonCode = QStringLiteral("no_pose_hard_cap_abort");
        relinquishAutonomousLiveMeterShot(output, QStringLiteral("pose_hard_cap"));
        return;
    }

    if (schedFireDeadlineMs_ >= 0.0) {
        // [ORION_FUSED_FIRE] a fused-owned pending deadline stays RE-SCHEDULABLE until the
        // commit lock: fall through so the fused block re-evaluates t_fire off fresh samples
        // (later AND earlier moves allowed pre-commit — the whole point of the wider horizon).
        // Every legacy-owned pending fire keeps the committed no-re-evaluation contract.
        const bool fusedReschedulable = config_.fusedFireEnabled
            && schedFireCode_ == QLatin1String("fused_posterior")
            && schedFireConfirmedToken_ != schedFireToken_
            && (schedFireDeadlineMs_ - now) > config_.fusedCommitLockMs;
        if (!fusedReschedulable) {
            shot_.releasePlan = QStringLiteral("Scheduled fire");
            shot_.releaseReason = QStringLiteral("release_scheduled");
            shot_.releaseReasonCode = QStringLiteral("release_scheduled");
            return;
        }
    }

    // Wifi mode is already folded into genuineVisionFrame/freshWindowMs above.
    // Latch the first time a GENUINE fresh meter appears this shot — the meter-appear ANCHOR.
    // CRITICAL: gate on a GENUINE fresh accept (sawFreshMeterThisShot / firstFreshAcceptMs, set
    // in updateDetection ONLY for a clean raw detection), NOT on meterFresh — which is also true
    // for a stale_or_memory ECHO (meterDetected stays set across an echo). A cross-shot memory
    // echo surfaces at ~holdStart+10ms and is unrelated to THIS shot's meter; anchoring the clock
    // to it forced each per-type clock to absorb the entire variable fade WIND-UP (a fade meter
    // doesn't truly appear until ~440-590ms into the hold), so the clock's shot-to-shot variance
    // exceeded the ~30-40ms green window — every fade scattered EARLY/LATE while Standstill (no
    // wind-up) greened. Anchoring to the genuine appearance makes the clock the FIXED, wind-up-
    // invariant appear->tip segment (~100ms for ALL fast types, derived from the green Standstill
    // seq46 + the late fade trajectories). The hold is still gated on the meter surfacing — a
    // contested shot may never show green, so we never gate on a green window. firstMeterSeenMs is
    // the genuine-appear time so the post-meter timing window measures from there.
    if (shot_.sawFreshMeterThisShot && shot_.firstFreshAcceptMs >= 0.0
            && !shot_.meterSeenThisShot) {
        shot_.meterSeenThisShot = true;
        shot_.firstMeterSeenMs = shot_.firstFreshAcceptMs;
        shot_.firstMeterFrameAgeMs = shot_.frameAgeMs;  // Orion 13.1
    }

    // Hold-start cap: keep holding while waiting for the meter to surface; abort
    // (no blind shot) if it never does.
    // [ORION_GOTO_METER_WAIT] (2b) A waiting Go-To holds LONGER for the real meter (animation
    // lengths vary — the meter may surface seconds in) before the bounded no-meter abort.
    // Expiry relinquishes automation ownership and restores physical pass-through; it never
    // fabricates release authority. std::max preserves a longer configured abort budget.
    // Post-meter ceiling: a bounded window AFTER the meter appears for the
    // predictive paths to time the release, before a labelled last-resort fires.
    // Measured from firstMeterSeenMs (Go-To meters can surface seconds into the
    // hold, so a hold-relative ceiling would dump the shot the instant it appeared).
    const double meterElapsed = (shot_.meterSeenThisShot && shot_.firstMeterSeenMs >= 0.0)
        ? now - shot_.firstMeterSeenMs : 0.0;
    const double postMeterCeilingMs = isGoto ? config_.gotoMaxHoldMs : config_.maxHoldMs;
    const bool maxHoldSafety = shot_.meterSeenThisShot && meterElapsed >= postMeterCeilingMs;

    // Autonomous tip-vision applies to NON-Go-To (fast) types only — Go-To animates differently (long
    // gather) and already times vision-only, so it keeps its proven per-type path. globalApplies is the
    // gate for the whole tip-vision reshape: TARGET becomes the meter tip (fill=100, green telemetry-only),
    // the predictive FIRE floor rises to tipFireMinFillPct (~88, fire near the tip), effectiveLatency drops
    // the hardcoded controllerChain/pipeline for the ONE self-measured global learnedLatencyMs (+ a small
    // per-type residual), and the post-release grader dials learnedLatencyMs (not the per-type maps). This
    // is the PRODUCTION DEFAULT (launcher forces ORION_AUTONOMOUS_VISION on); autonomous_vision OFF (tests /
    // dev opt-out) restores the proven per-type fallback below.
    // [ORION_GOTO_FIRE] Go-To is treated like every other shot type now: it flows through the
    // same autonomous tip-vision reshape (was gated out with `&& !isGoto`). It may fire (accepting
    // some late/mistimed Go-To until its per-type clock re-calibrates) instead of aborting.
    const bool globalApplies = config_.autonomousVision;
    // [ORION_MEASURED_LEAD] THE big lever: when on and a real measurement has arrived, the sidecar's
    // live-measured release-path latency (~70ms true round-trip) IS the lead base, replacing the
    // under-compensating learnedLatencyMs (=0 now) + controllerChain (~13ms modeled) that fires ~50ms
    // LATE. The frame-age staleness term is still added below. Applies to BOTH the autonomous and the
    // per-type path (it is the true round-trip, so it must REPLACE the modeled chain, never stack on it).
    // [ORION_MEASURED_LEAD] Engagement is gated by measuredLeadAuthoritative: either a fully
    // converged oracle or a distinct controlled planned-stop validation, plus the
    // one-shot clock re-baseline having run (updateDetection fires it at the same threshold):
    // flipping the lead base from ~6ms to ~75ms without shifting the EMA-dialed clocks would
    // fire every path ~69ms early for the 15-30 shots the EMAs need to re-converge (Part-0).
    const bool useMeasuredLead = measuredLeadAuthoritative(now);
    // Detector release-window labels are not gameplay rewards. Keep the experimental tuner as an
    // offline unit-test utility, with no live timing authority even if a stale flag enables it.
    shot_.banditLeadOffsetMs = 0.0;
    // Latency chain: controllerChainMs is the user's total latency comp setting
    // (includes CV observation + ViGEm + Remote Play encoding), plus measured
    // network offset from RTT probes and user's early/late micro-adjustment. In the hybrid path the
    // per-type offset collapses to ONE global self-measured latency correction (learnedLatencyMs)
    // PLUS a per-shot-type residual (shotTypeLatencyMs) for type-specific rise-profile differences.
    const double effectiveLatency =
                                    // [ORION_MEASURED_LEAD] live-path bug #6: the oracle's measured
                                    // round-trip ALREADY contains the network RTT (the fused block
                                    // below never re-adds networkOffsetMs on top of it) — adding
                                    // networkOffsetMs here as well double-counted the network
                                    // leg and would fire ~half-RTT+jitter EARLY once the flag ships.
                                    // Modeled paths (learnedLatencyMs / controllerChain) keep it.
                                    // SIGN (2026-07-24): was std::abs(). In Manual sync mode this
                                    // offset carries the user's manual_sync_adjust_ms trim, which is
                                    // signed on purpose — a NEGATIVE trim means "fire LATER". abs()
                                    // folded it into extra LEAD, so a -50ms trim became +50ms of
                                    // lead: a 100ms swing the wrong way the moment sync mode is
                                    // switched to Manual. updateNetworkOffset already clamps the
                                    // value to +/-100ms, so the signed term is bounded.
                                    (useMeasuredLead ? 0.0 : shot_.networkOffsetMs)
                                  + config_.earlyLateOffsetMs
                                  // No-Dip shots skip the gather/dip -> they release earlier; add
                                  // the (live-tuned, default 0) no-dip lead when the mode is on.
                                  + (config_.noDipEnabled ? config_.noDipLeadMs : 0.0)
                                  + (useMeasuredLead
                                       // [ORION_MEASURED_LEAD] the measured round-trip is the lead base;
                                       // keep the small learned per-type residual on top for rise-profile
                                       // differences (0 until dialed). Fires the release ON the tip.
                                       ? measuredLatencyMs_
                                         // Button and Tempo read the same per-type residual.
                                         + seededValue(config_.shotTypeLatencyMs,
                                                       shot_.bucketKey, shot_.shotType)
                                       : (globalApplies
                                       // AUTONOMOUS TIP-VISION lead: ONE self-measured global
                                       // correction (learnedLatencyMs) + a per-type residual. The
                                       // fixed controllerChain/remotePlayPipeline constants are
                                       // deliberately NOT used here (see autonomousGlobalLeadConverges
                                       // FromZero / measuredLeadUsesMeasuredLatencyAsLeadBase, which
                                       // assert exactly that) -- the design is that this ONE number
                                       // carries the whole invisible loop.
                                       // CAUTION: that makes learnedLatencyMs load-bearing. It was
                                       // meant to be dialed from 0 by the post-release grader, but the
                                       // grader is frozen in production (ORION_FREEZE_CAL) and its only
                                       // signal -- the meter self-grade -- was retired as degenerate.
                                       // So whatever learning.json holds is what ships, permanently. A
                                       // seed near 0 means the press->console + capture legs go
                                       // UNCOMPENSATED and every release lands past the tip. It must be
                                       // seeded to the real measured loop latency, not left at 0.
                                       ? config_.learnedLatencyMs
                                         + seededValue(config_.shotTypeLatencyMs,
                                                       shot_.bucketKey, shot_.shotType)
                                       // Per-type FALLBACK keeps the measured chain + fixed RemotePlay
                                       // pipeline estimate + the per-type offset (the pre-tip-vision path).
                                       : config_.controllerChainMs + config_.remotePlayPipelineMs
                                         + shotTypeOffsetMs(shot_.bucketKey)))
                                  // Capture staleness: the meter sample is frameAgeMs old; lead by it
                                  // (clamped) so we don't extrapolate from a stale fill without comp.
                                  + std::clamp(shot_.frameAgeMs, 0.0,
                                               config_.captureAgeLeadCapMs);

    // Release target:
    //  - AUTONOMOUS TIP-VISION (globalApplies) -> the meter TIP (fill=100%) UNCONDITIONALLY. The green
    //    window is telemetry-only here (greenConfirmedAtRelease / targetModeAtRelease below still record
    //    it); it no longer changes WHERE we aim. The self-measured global lead absorbs the constant
    //    offset between the fill=100 tip reading and the true make-point. Go-To (globalApplies==false)
    //    keeps its proven green-top target below.
    //  - PER-TYPE fallback: green confirmed -> aim its TOP EDGE (dead-top, contest-invariant make-point);
    //    no green (contested) -> time the meter FULLY to the top.
    QString targetMode;
    double target;
    if (isFade && greenTracker_.confirmed() && greenTracker_.startPct() >= 0.0) {
        // [ORION_FADE_GREEN_ENTRY] A fade's PERFECT-green window is a tiny ~2pp sliver, but the
        // MADE-shot window (entry->tip) is far wider. Aim the confirmed window's ENTRY (lower edge)
        // so the whole entry->tip span is landing tolerance — anywhere in it is a make, and a fade
        // (which mis-fires EARLY on the global clock, and 2K punishes late more than early) is best
        // served landing at the earliest make-point. Non-fade types keep their tip/center logic.
        target = clampPct(greenTracker_.startPct() + config_.learningBiasPct);
        targetMode = QStringLiteral("fade_green_entry");
    } else if (globalApplies) {
        target = clampPct(config_.fullMeterTargetPct + config_.learningBiasPct);
        targetMode = QStringLiteral("meter_tip");   // tip-vision: fill=100 is authoritative
    } else if (greenTracker_.confirmed()) {
        const bool fadeTarget = shot_.shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive);
        const double margin = fadeTarget ? config_.tipMarginFadePct : config_.meterTipMarginPct;
        target = greenTracker_.adaptiveTargetPct(margin, config_.learningBiasPct);
        targetMode = fadeTarget ? QStringLiteral("green_tip_fade") : QStringLiteral("green_tip");
    } else {
        target = clampPct(config_.fullMeterTargetPct + config_.learningBiasPct);
        targetMode = QStringLiteral("meter_full");
    }
    // Item 7: Apply per-shot-type target offset. Different shot types have slightly
    // different green window positions (Standstill green at very top, Fade slightly lower).
    // Learned from post-release grade: consistently early -> lower target, late -> raise it.
    // Button and Tempo share the same canonical per-type target offset.
    const double typeTargetOffset = seededValue(config_.shotTypeTargetOffsetPct,
                                                shot_.bucketKey, shot_.shotType);
    target = clampPct(target + typeTargetOffset);
    // [ORION_PLATEAU_AIM] H6: GREEN = MAKE (binary, user-confirmed) and the ~50ms cap-hold
    // PLATEAU sits INSIDE the green band at fill=100 — so the make-window's TIME-center is the
    // plateau center, not the green band's fill-center. Aim the APEX (fill=100); the predicted
    // crossing then gets +½·plateauCapHoldMs below so the release lands mid-plateau,
    // t(fill=100)+½·capHold. Gated on a CONFIRMED green tracker (fire off low-σ confirmed-green,
    // never a far-horizon ML-ETA alone); a contested shot with no confirmed green keeps its
    // unmodified path. Applies to every class incl. fades (supersedes fade_green_entry while on).
    shot_.plateauShiftMs = 0.0;
    const bool plateauAim = config_.plateauAimEnabled && greenTracker_.confirmed();
    if (plateauAim) {
        target = clampPct(100.0);
        targetMode = QStringLiteral("plateau_cap");
    }
    // DIAGNOSTIC (no behavior change): record the fill% / meter-elapsed at which the green
    // tracker FIRST confirms this shot, so a Go-To release line shows whether the tracker
    // won the race against the velocity path. Latches once per shot (greenConfirmFillPct
    // resets to -1 in beginShot); reads existing state only.
    if (greenTracker_.confirmed() && shot_.greenConfirmFillPct < 0.0) {
        shot_.greenConfirmFillPct = shot_.fillPct;
        shot_.greenConfirmMs = meterElapsed;
    }
    double crossing = sampler_.predictCrossingMs(target);
    // [ORION_REG_FUSION] Far-horizon handoff: the quadratic sampler blows up when the tip is far out
    // (low fill / commit >~200ms away); the sidecar's registration time-to-TIP (reg_tip_ms) is
    // flat/accurate there. When reg confidence is adequate, use registration for the FAR horizon and
    // keep the sampler for the near-tip window (<160ms to tip / high fill) — mirrors the offline FUSION
    // handoff. Default OFF -> the sampler crossing is unchanged.
    if (config_.regFusionEnabled && shot_.regConf >= config_.regFusionMinConf && shot_.regTipMs > 0.0) {
        const bool farHorizon = shot_.regTipMs >= config_.regFusionFarHorizonMs
            || shot_.fillPct < config_.regFusionNearFillPct;
        if (farHorizon) {
            crossing = now + shot_.regTipMs;
        }
    }
    // [ORION_TEMPLATE_ARRIVAL] H4: blend the matched template's green-arrival prediction as a
    // PRIOR with the live crossing (inverse-variance). The template is anchored at this shot's
    // LATEST observed crossing (t@80-90 — the shortest extrapolation latency allows); offline
    // LOO σ ~14-16ms vs ~35ms for the reactive fit. No match / no anchor / flag off -> the live
    // crossing is byte-identical.
    shot_.templateArrivalMs = -1.0;
    shot_.templateBlendWeight = 0.0;
    shot_.templateMatchedIdx = -1;
    if (config_.templateArrivalEnabled && templateArrival_.matched()) {
        const double tmplArrival = templateArrival_.predictArrivalMs(target);
        if (tmplArrival > now) {
            shot_.templateArrivalMs = tmplArrival;
            shot_.templateMatchedIdx = templateArrival_.matchedIdx();
            if (crossing > 0.0) {
                const double vt = config_.templateArrivalSdMs * config_.templateArrivalSdMs;
                const double vl = config_.templateLiveSdMs * config_.templateLiveSdMs;
                const double w = vl / (vl + vt);   // template prior weight (smaller σ -> w > 0.5)
                crossing = w * tmplArrival + (1.0 - w) * crossing;
                shot_.templateBlendWeight = w;
            } else {
                crossing = tmplArrival;            // no usable live crossing -> template owns it
                shot_.templateBlendWeight = 1.0;
            }
        }
    }
    // [ORION_PLATEAU_AIM] H6: land the release mid-plateau — t(fill=100) + ½·capHold.
    if (plateauAim && crossing > 0.0) {
        shot_.plateauShiftMs = 0.5 * config_.plateauCapHoldMs;
        crossing += shot_.plateauShiftMs;
    }
    // Velocity sanity band: clamp the live sampler velocity to the persisted per-type prior's
    // band. A detector blip / torn wifi frame can spike the instantaneous velocity wildly;
    // the prior (EMA of this type's vision-timed releases) bounds it without discarding the
    // sample. A type with no prior yet passes through unclamped.
    double velocityPctPerMs = sampler_.velocityPctPerMs();
    {
        const double prior = seededValue(config_.shotTypeVelocityPriorPctMs, shot_.bucketKey, shot_.shotType);
        if (prior > 0.0 && velocityPctPerMs > 0.0) {
            velocityPctPerMs = std::clamp(velocityPctPerMs,
                                          prior * config_.velocityPriorLoFactor,
                                          prior * config_.velocityPriorHiFactor);
        }
    }
    shot_.effectiveLatencyMs = effectiveLatency;
    shot_.targetPct = target;
    shot_.releaseEtaMs = crossing > 0.0 ? crossing - now : -1.0;

    // === SHADOW MODE (autonomous_vision_shadow): COMPUTE-ONLY, never controls release. ===
    // The global-velocity phase-aligned autonomous model: predictedTip = now + (target-fill)/vg
    // (phase from THIS shot's current fresh fill, speed = the type-invariant global vg); release
    // deadline = predictedTip - effectiveLatency. Latch the FIRST holding frame the deadline comes
    // due (fresh meter, below target) as "when the autonomous model WOULD have fired" — recorded on
    // the "Shadow timing:" line for offline/live A/B against the actual release + post-release
    // verdict, BEFORE the model is ever allowed to take control. Placed before the release/return
    // blocks so it evaluates every frame regardless of which actual path fires.
    if (config_.autonomousVisionShadow && shot_.shadowFireMs < 0.0
            && meterFresh && shot_.meterSeenThisShot
            && shot_.fillPct > 0.0 && shot_.fillPct < target
            && config_.globalRiseVelocityPctMs > 1e-4) {
        const double vg = config_.globalRiseVelocityPctMs;
        const double predTip = now + (target - shot_.fillPct) / vg;
        const double deadline = predTip - effectiveLatency;
        shot_.shadowVgPctMs = vg;
        if (now >= deadline) {
            shot_.shadowFireMs = now;
            shot_.shadowFireFillPct = shot_.fillPct;
            shot_.shadowPredTipMs = predTip;
        }
    }
    shot_.releasePlan = QStringLiteral("Tracking");
    shot_.releaseReason = QStringLiteral("Tracking meter trajectory");
    shot_.releaseReasonCode = QStringLiteral("tracking");

    bool shouldRelease = false;

    double commitMinMs = config_.standstillCommitMinMs;
    QString commitReason = QStringLiteral("waiting_for_shot_commit");
    if (shot_.mode == ShotMode::GoToStick) {
        commitMinMs = config_.gotoCommitMinMs;
        commitReason = QStringLiteral("animation_commit");
    } else if (shot_.shotType == QLatin1String("Fade") || shot_.shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive)) {
        commitMinMs = config_.fadeCommitMinMs;
        commitReason = QStringLiteral("waiting_for_fade_commit");
    } else if (shot_.shotType == QLatin1String("Moving")) {
        commitMinMs = config_.movingCommitMinMs;
        commitReason = QStringLiteral("waiting_for_moving_commit");
    }
    if (!maxHoldSafety && elapsed < commitMinMs) {
        shot_.releasePlan = shot_.mode == ShotMode::GoToStick
            ? QStringLiteral("Go-To commit")
            : QStringLiteral("Shot commit");
        shot_.releaseReason = commitReason;
        shot_.releaseReasonCode = QStringLiteral("commit_wait");
        return;
    }

    // === [ORION_FUSED_FIRE] the fused t_tip posterior: PRIMARY rung under authority, =====
    // === compute+log under shadow. Non-Go-To only (Go-To keeps its proven vision path; ===
    // === the template corpus does not cover ~2s rises — Part-0 M2). =====================
    bool fusedOwns = false;                 // authority AND healthy this tick
    if ((config_.fusedFireEnabled || config_.fusedShadowEnabled)
        && !config_.noMeterEnabled && !isGoto) {
        // Anchor once per shot on the VALIDATED meter-appear episode (same validity gate the
        // FF meter clock requires — a carryover meter can never anchor the posterior).
        if (!fused_.anchored() && shot_.meterSeenThisShot && shot_.anchorValidMs >= 0.0) {
            // Button and Tempo share the canonical appear-to-tip clock.
            const double clockMs = seededValue(config_.shotTypeAppearToTipMs,
                                               shot_.bucketKey, shot_.shotType,
                                               config_.fusedAppearToTipSeedMs);
            fused_.anchor(shot_.anchorValidMs + clockMs, config_.fusedSigma0Ms);
        }
        // One posterior update per NEW detection sample (the tick runs at 4ms; genuine samples
        // land ~16.7ms apart). Clean raw accepts only — a held/echo fill must not update.
        if (fused_.anchored() && shot_.lastSampleGenuineAccept
            && shot_.lastDetectionMs > 0.0 && shot_.lastDetectionMs != lastFusedSampleMs_) {
            lastFusedSampleMs_ = shot_.lastDetectionMs;
            const bool newInfo = lastFusedFillPct_ < 0.0
                || std::abs(shot_.fillPct - lastFusedFillPct_) > 0.05;
            lastFusedFillPct_ = shot_.fillPct;
            const bool becamePeak = !fusedPeakLatched_ && shot_.fillPct >= 85.0;
            if (becamePeak) {
                fusedPeakLatched_ = true;
            }
            // Timeline correction: mean capture->ingestion delay of the samples the posterior
            // is built from (frame_age as a one-time timeline shift, never an additive lead).
            fusedFrameAgeEmaMs_ = fusedFrameAgeEmaMs_ < 0.0
                ? std::clamp(shot_.frameAgeMs, 0.0, 50.0)
                : fusedFrameAgeEmaMs_ * 0.8 + std::clamp(shot_.frameAgeMs, 0.0, 50.0) * 0.2;

            // Source: registration fit. HARD conf gate (a prior-dominated fit double-counts
            // the anchor); sigma from the Part-0 M2 MEASURED error curve, widened by the fit's
            // own unweighted residual.
            FusedTipEstimator::SourceMeas reg;
            if (shot_.regTipAbsMs > 0.0 && shot_.regConf >= config_.fusedRegMinConf
                && shot_.regN >= 4) {
                const double h = std::max(0.0, shot_.regTipAbsMs - now);
                reg.tipAbsMs = shot_.regTipAbsMs;
                reg.sigmaMs = config_.fusedRegSigmaBaseMs
                    + config_.fusedRegSigmaSlopeMsPerMs * std::max(0.0, h - 150.0)
                    + config_.fusedRegRmseSigmaMsPerPp * std::max(0.0, shot_.regRmsePp);
            }
            // Source: near-tip sampler crossing (with fit diagnostics). Hard horizon gate at
            // fusedSamplerMaxHorizonMs (the known far-horizon quadratic blowup) + min support.
            FusedTipEstimator::SourceMeas samp;
            const TemporalSampler::CrossingFit cf = sampler_.predictCrossing(target);
            if (cf.crossingMs > 0.0 && cf.n >= config_.fusedSamplerMinN) {
                const double h = cf.crossingMs - now;
                if (h > 0.0 && h <= config_.fusedSamplerMaxHorizonMs) {
                    const double qTrack = std::clamp((shot_.confidence - 0.95) / 0.05, 0.0, 1.0);
                    const double rFit = cf.wrss >= 0.0
                        ? std::clamp(std::sqrt(cf.wrss / std::max(1, cf.n)) / 0.8, 0.7, 3.0)
                        : 1.0;
                    double sigma = (6.0 + 0.20 * h + 40.0 * (1.0 - qTrack))
                        * rFit * std::max(1.0, std::sqrt(6.0 / cf.n));
                    // Velocity-collapse protection: a LINEAR crossing above the decel knee
                    // extrapolates through the flattening cap — biased early; the quadratic is
                    // the local decel model. Don't drop it (the chi^2 gate bounds it), tax it.
                    if (!cf.usedQuad && shot_.fillPct > config_.decelKneePct) {
                        sigma *= 2.0;
                    }
                    samp.tipAbsMs = cf.crossingMs;
                    samp.sigmaMs = sigma;
                }
            }
            fused_.update(newInfo, reg, samp, becamePeak,
                          config_.fusedProcessNoiseMs, config_.fusedPeakInflateMs);
        }
        // Fire rule: t_fire = mu - delta_aim - lead - timelineShift, aimed EARLY of the tip
        // inside the measured green-band time width (asymmetric loss: past-tip is a miss).
        if (fused_.anchored()) {
            shot_.fusedMuTipMs = fused_.muTipMs();
            shot_.fusedSigmaMs = fused_.sigmaMs();
            // Button and Tempo share the canonical green-band time width.
            const double wTime = seededValue(config_.shotTypeWTimeMs,
                                             shot_.bucketKey, shot_.shotType,
                                             config_.fusedWTimeDefaultMs);
            const double sigmaAim = std::max(fused_.sigmaMs(), config_.fusedSigmaFloorMs);
            const double sigmaL = measuredLatencySdMs_ > 0.0 ? measuredLatencySdMs_ : 12.0;
            // [Phase-2 A2(c)] earlier-only console-tick snap: engaged only on trustworthy
            // probe-run phase telemetry (self-gating — absent fields = byte-identical). While
            // snapped, the press lands epsilon before an input-tick edge instead of scattering
            // uniformly across the 16.7ms tick, so sigma_tick collapses 4.8 -> 1.8.
            const bool tickSnapOn = tickPhaseAuthoritative(now);
            const double sigmaTick = tickSnapOn ? config_.fusedTickSigmaSnappedMs : 4.8;
            const double sigmaLand = std::sqrt(sigmaAim * sigmaAim + sigmaL * sigmaL
                                               + sigmaTick * sigmaTick + 1.0);
            const double deltaAim = std::clamp(config_.fusedAimWFrac * wTime
                                                   + config_.fusedAimSigmaK * sigmaLand,
                                               config_.fusedAimMinMs,
                                               config_.fusedAimMaxWFrac * wTime + config_.fusedAimMinMs);
            // Lead: the oracle's measured round-trip (it already contains the typical network
            // RTT — never re-add networkOffsetMs on top) + user trim + per-type residual.
            // Without a real measurement the fused rule has no authority (backstop owns).
            const bool leadKnown = measuredLeadAuthoritative(now);
            const double fusedLead = leadKnown
                ? measuredLatencyMs_ + config_.earlyLateOffsetMs
                    // Button and Tempo share the canonical per-type residual.
                    + seededValue(config_.shotTypeLatencyMs, shot_.bucketKey, shot_.shotType)
                : effectiveLatency;
            double tFire = fused_.muTipMs() - deltaAim - fusedLead
                - std::max(0.0, fusedFrameAgeEmaMs_);
            if (tickSnapOn) {
                // Convert the probe-fit EDGE phase (press-EPOCH ms mod P — the same clock the
                // probe/release markers stamp at press submit) onto the engine clock with the
                // TRUE instantaneous epoch<->engine offset (a direct clock read, NOT the
                // capture bridge: epochToEngineOffsetMs_ = arrival - captureTs carries the
                // capture->ingestion transport delay as a systematic phase bias that could eat
                // the whole 5.5ms epsilon and slip fires a full tick late).
                const double P = 1000.0 / 60.0;
                const double engineMinusEpoch =
                    now - static_cast<double>(QDateTime::currentMSecsSinceEpoch());
                double edgeEngine = std::fmod(tickPhaseEpochMs_ + engineMinusEpoch, P);
                if (edgeEngine < 0.0) {
                    edgeEngine += P;
                }
                tFire = earlierOnlyTickSnapMs(tFire, edgeEngine,
                                              config_.fusedTickSnapEpsilonMs, P);
            }
            fusedFireAtMs_ = tFire;
            // Shadow latch: first tick the fused deadline is due (log-only A/B vs the actual).
            if (shot_.fusedShadowFireMs < 0.0 && now >= tFire) {
                shot_.fusedShadowFireMs = now;
                shot_.fusedShadowFillPct = shot_.fillPct;
            }
            fusedOwns = config_.fusedFireEnabled && leadKnown && genuineVisionFrame
                && fused_.sigmaMs() <= config_.fusedBackstopSigmaMs;
            // Ownership LOST (posterior blew up / lead vanished) with a fused deadline still
            // armed and unconfirmed: cancel it — a deadline the posterior no longer stands
            // behind must not fire; the FF backstop below owns the shot from here.
            if (!fusedOwns && schedFireDeadlineMs_ >= 0.0
                && schedFireCode_ == QLatin1String("fused_posterior")
                && schedFireConfirmedToken_ != schedFireToken_) {
                invalidateUnconfirmedVisionSchedule("fused_a");
            }
            if (fusedOwns) {
                // Re-schedule an armed fused deadline (pre-commit; later and earlier moves both
                // allowed — the pending block above falls through for exactly this case).
                if (schedFireDeadlineMs_ >= 0.0
                    && schedFireCode_ == QLatin1String("fused_posterior")
                    && schedFireConfirmedToken_ != schedFireToken_
                    && (schedFireDeadlineMs_ - now) > config_.fusedCommitLockMs
                    // [ORION_DEV_FIRE_OFFSET] undisplaced comparison (offset 0 when disarmed)
                    && std::abs(tFire - (schedFireDeadlineMs_
                                         - schedFireAppliedDevOffsetMs_))
                        > config_.fusedRescheduleMinDeltaMs) {
                    invalidateUnconfirmedVisionSchedule("fused_b");
                }
                if (schedFireDeadlineMs_ < 0.0) {
                    if (now >= tFire) {
                        // Deadline already due (late lock on a fast meter). Fire now unless the
                        // meter is already spent (past-peak deflate) — then the safeties own it.
                        if (shot_.fillPct >= 96.0 || fused_.muTipMs() - now > -40.0) {
                            shot_.releasePlan = QStringLiteral("Fused posterior");
                            shot_.releaseReason = QStringLiteral(
                                "Fused t_tip due (mu=%1 sigma=%2 aim=%3 lead=%4)")
                                    .arg(fused_.muTipMs(), 0, 'f', 0)
                                    .arg(fused_.sigmaMs(), 0, 'f', 1)
                                    .arg(deltaAim, 0, 'f', 1)
                                    .arg(fusedLead, 0, 'f', 1);
                            shot_.releaseReasonCode = QStringLiteral("fused_posterior");
                            if (triggerRelease(now)) {
                                clearReleaseOutput(output);
                            } else {
                                restoreAbortOutput(output);
                            }
                            return;
                        }
                    } else if (fused_.sigmaMs() <= config_.fusedConvergedSigmaMs
                               && scheduleFire(tFire, now, config_.fusedSchedulerHorizonMs)) {
                        schedFirePlan_ = QStringLiteral("Fused posterior");
                        schedFireReason_ = QStringLiteral("Fused t_tip deadline (scheduled)");
                        schedFireCode_ = QStringLiteral("fused_posterior");
                        shot_.releasePlan = QStringLiteral("Scheduled fire");
                        shot_.releaseReason = QStringLiteral("release_scheduled");
                        shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                        return;
                    }
                } else {
                    // Armed (fused or re-armed above) and inside horizon: hold.
                    shot_.releasePlan = QStringLiteral("Scheduled fire");
                    shot_.releaseReason = QStringLiteral("release_scheduled");
                    shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                    return;
                }
            }
        }
    }

    // Fast feedforward types (Standstill / any Fade) may use the deterministic
    // meter-appear clock because their rise+peak is shorter than capture/view latency.
    // The clock still needs a validated anchor and a current genuine frame below.
    const bool fastFeedforwardType = !isGoto
        && (shot_.shotType == QLatin1String("Standstill")
            || shot_.shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive));

    // Feedforward (deterministic animation clock) is timing assistance, not a
    // substitute for the bot's eyes. It is evaluated before the waiting branch so
    // a validated visible meter may use its learned animation clock, but
    // clockVisionAuthorized prevents any fire before a two-sample anchor or on a
    // stale, memory, duplicate, or missing current payload.
    //
    // ANCHOR (RemapConfig::feedforwardAnchor, runtime A/B): "meter_appear" times the FIXED
    // meter-appears->tip segment (firstMeterSeenMs + shotTypeMeterToReleaseMs) — far less
    // shot-to-shot variance than holdStart->tip for FAST meters, so a Standstill/Fade hits
    // the tiny window as reliably as a slow Right Fade. The hold-start deadline may be
    // selected, but it cannot fire until the meter is validated and current.
    //
    // No-meter (Skele) mode: the pose-landmark block below owns the release. The meter feedforward fires
    // off the leftover learned hold-start clock even with no meter, which pre-empts the pose path (the
    // engine released on a stale 388ms meter clock instead of waiting for the zero-cross). Skip the whole
    // meter feedforward when noMeterEnabled and fall through to the pose-based release.
    if (!config_.noMeterEnabled) {
        // The clock must be ARMED (learned > floor), so a fresh bucket still uses
        // reactive vision for its first shot(s) to seed it.
        // autonomous_vision (Phase C): ONE global clock replaces the 11 per-type clocks/offsets for
        // non-Go-To types. The model is always meter-appear-anchored (with a hold-start deadline
        // available only after vision authorization), the clocks are STABLE appear->tip / hold->tip durations, and the
        // lead is the single self-measured effectiveLatency (learnedLatencyMs). Default OFF.
        const bool useMeterAnchor = globalApplies || config_.feedforwardAnchor == QLatin1String("meter_appear");
        // Output mode does not key timing: Button and Tempo use one canonical
        // per-type clock, with the same seed and learned history.
        // [ORION_GOTO_CLOCK] Go-To stays METER-APPEAR-ANCHORED on its own per-type clock even though
        // globalApplies is now true for it (the a845118e fence removal keeps Go-To's fire eligibility,
        // target, and lead on the global path). Its meter reliably surfaces ~1620ms into the hold, so
        // routing it onto the 566ms global hold/appear clocks blind-fires it ~800ms early (nodet, fill
        // 0%). Gate ONLY the clock source back to the per-type meter-anchored value for Go-To; the rest
        // of globalApplies stays true. seededValue falls back to the "Go-To" shot_type_* clocks.
        // [ORION_FADE_CLOCK] Fades are exempted from the global clock exactly as Go-To is
        // (isFade, defined at function scope above). A fade's real appear->tip is ~340-380ms with
        // a ~0.28%/ms rise and a learned per-type offset — the standstill-dominated global 566/251
        // clock fires it ~90-130ms EARLY (the fade's ~2pp window can't absorb the error the way a
        // standstill's ~12pp window does). Route fades back to their per-type meter/hold clocks
        // (seededValue -> shot_type_meter_to_release_ms / shot_type_feedforward_ms). Standstill and
        // every other non-fade, non-Go-To type STAY on the global clock (do NOT touch the globals).
        // Output mode cannot alter these clocks; Tempo reads the same values as Button.
        const double holdClock = (globalApplies && !isGoto && !isFade)
            ? config_.globalHoldToReleaseMs
            : seededValue(config_.shotTypeFeedforwardMs, shot_.bucketKey, shot_.shotType);
        const double meterClock = (globalApplies && !isGoto && !isFade)
            ? config_.globalAppearToTipMs
            : seededValue(config_.shotTypeMeterToReleaseMs, shot_.bucketKey, shot_.shotType);
        // An armed clock can own timing only from a validated anchor. An unarmed
        // bucket keeps reactive vision, which may seed the clock on shot 1.
        // Lead: per-type path subtracts the per-type offset (user slider + learned), in the SAME
        // direction as the vision path. The autonomous path instead leads the (latency-independent)
        // appear->tip clock by the FULL effectiveLatency, so the press lands AT the tip and the single
        // global learnedLatencyMs (dialed by the post-release grader) corrects any residual bias.
        // [ORION_FADE_CLOCK] Fades also fall back to the per-type offset (shotTypeOffsetMs =
        // user trim + shot_type_learned_offset_ms, incl. Right Fade +110ms) rather than the global
        // effectiveLatency lead — so the fade leads its OWN clock by its OWN learned offset. Go-To
        // and other non-fade types under autonomous_vision keep the global effectiveLatency lead.
        const double ffOffset = (globalApplies && !isFade)
            ? effectiveLatency : shotTypeOffsetMs(shot_.bucketKey);
        const double rawMeterThresh = meterClock - ffOffset;
        const double meterThresh = std::max(rawMeterThresh, config_.meterClockMinMs);
        // [ORION_GOTO_CLOCK] The hold-start clock is an alternate learned deadline after the meter has
        // been validated. For Go-To it must not mature before the ~1600ms meter-appear window (its meter
        // surfaces ~1620ms in), preventing a short/drifted clock from becoming eligible ~800ms early.
        // This floors only the hold-start deadline; every clock path still requires current genuine
        // vision through clockVisionAuthorized below.
        const double holdThreshFloor = isGoto ? config_.gotoBlindFireFloorMs : config_.feedforwardMinMs;
        const double holdThresh = std::max(holdClock - ffOffset, holdThreshFloor);
        // (do #2) Floor-clamp audit: meterThresh got clamped UP to the meterClockMinMs floor, i.e. the
        // learned clock MINUS the lead is implausibly short (the latency over-compensated). The floor
        // never pulls the fire earlier than the clock implies, but a fire at the 80ms floor is still a
        // premature press on a barely-risen meter — so a floored anchor is honoured ONLY when a fresh
        // fill confirms reach; otherwise prefer the vision / hold-start path over an early floored fire.
        const bool meterThreshFloored = rawMeterThresh < config_.meterClockMinMs;

        // (do #1) Reachability cap on the OPEN-LOOP fire — the chief early-fire fix. The feedforward
        // fires AHEAD on the learned clock to beat view latency, so the meter often still reads BELOW
        // target at fire time (the reading lags the real rise) — the intended view-latency behaviour.
        // But a short-drifted clock would otherwise fire no matter how LOW the fill is. Bound it like
        // the vision path: allow the fire only while a TRUSTWORTHY fresh fill is within one MAX lead's
        // worth of rise of the target (the most-lenient vision reach) plus a feedforward slack. Uses the
        // CLAMPED max rise, NOT the instantaneous velocity (a capped/lagging fast meter often reports
        // ~0 velocity). A genuine current frame supplies the reach sample; stale/memory/duplicate input
        // is rejected by clockVisionAuthorized and cannot own either the clock or this gate.
        // [ORION_GOTO_FIRE] Go-To now uses the non-Go-To reach clamp (was the conservative 25).
        const double ffMaxRisePct = config_.nonGotoMaxPredictiveRisePct;
        const bool ffFillKnown = genuineVisionFrame && shot_.fillPct > 0.0;
        const double ffReachFloorPct = target - (ffMaxRisePct + config_.ffReachabilitySlackPct);
        // Unknown/zero fill is not "unconstrained". The learned clock is timing
        // assistance, never a substitute for a current positive reach sample.
        const bool ffWithinReach = ffFillKnown && shot_.fillPct >= ffReachFloorPct;
        // A floored meter-anchor fire additionally requires a known fresh fill (see #2 above).
        const bool meterAnchorFireOk = ffWithinReach && (!meterThreshFloored || ffFillKnown);

        // T2: the meter-appear clock additionally requires a VALIDATED anchor (episode
        // gates in updateDetection) in ALL modes — including globalApplies — so a lingering
        // previous-shot meter can never anchor the clock. Deadlines below measure from
        // anchorValidMs (the validated episode's first sight), not firstMeterSeenMs.
        const bool meterAnchorAvail = useMeterAnchor && shot_.meterSeenThisShot
            && shot_.firstMeterSeenMs >= 0.0 && meterClock > config_.meterClockMinMs
            && shot_.anchorValidMs >= 0.0;
        const bool holdClockArmed = holdClock > config_.feedforwardMinMs;
        // Release-authority invariant: a learned clock is timing assistance, not a
        // substitute for the bot's eyes. It may act only after THIS shot has a
        // validated two-sample meter anchor, and only while the latest payload is a
        // genuine unique frame. A one-frame phantom (meterSeen=true, anchor invalid),
        // memory echo, duplicate, or stale frame cannot own a release. If vision never
        // validates or recovers, the bounded no-meter/absolute hard-cap paths below
        // abort automation and return control to physical pass-through.
        const bool clockVisionAuthorized = shot_.anchorValidMs >= 0.0
            && genuineVisionFrame;
        bool ffFire = false;
        bool firedOnMeterAnchor = false;
        double clockUsedMs = 0.0;
        if (meterAnchorAvail && clockVisionAuthorized
                && (now - shot_.anchorValidMs) >= meterThresh && meterAnchorFireOk) {
            ffFire = true; firedOnMeterAnchor = true; clockUsedMs = meterThresh;
        } else if (holdClockArmed && clockVisionAuthorized
                   && (now - shot_.holdStartMs) >= holdThresh && ffWithinReach) {
            ffFire = true; clockUsedMs = holdThresh;
        }
        // DIAGNOSTIC (T1): a clock is DUE but the reachability/floored-anchor gate is
        // holding the fire — latch when the hold began so the "Release vision:" line can
        // report reachHeldMs (the reach gate opening, not the clock, was the de-facto
        // trigger of the observed ~52%-fill releases).
        if (!ffFire && shot_.reachHoldStartMs < 0.0) {
            const bool meterDueBlocked = meterAnchorAvail
                && (now - shot_.anchorValidMs) >= meterThresh && !meterAnchorFireOk;
            const bool holdDueBlocked = holdClockArmed
                && (now - shot_.holdStartMs) >= holdThresh && !ffWithinReach;
            if (meterDueBlocked || holdDueBlocked) {
                shot_.reachHoldStartMs = now;
            }
        }
        // === T4 FORWARD-CROSSING AUTHORITY ===
        // A raw animation clock is a backstop, never a parallel primary while
        // current vision has a credible forward target crossing. This gate owns
        // BOTH the due-fire and future scheduler lanes. The old clock+cap test
        // left two holes: it scheduled a sub-tick FF token before the clock was
        // due, and an overdue clock won immediately when vision acquired late.
        // A latency-compensated vision fire instant may already be due/past; the
        // crossing itself must be forward/future so the predictive ladder below
        // gets the chance to fire now.
        const bool healthyForwardCrossing = config_.tipGateEnabled
            && healthyForwardCrossingOwnsTiming(
                genuineVisionFrame,
                shot_.countFreshAcceptsWithin(now, 150.0),
                velocityPctPerMs,
                shot_.fillPct,
                target,
                crossing,
                now,
                effectiveLatency,
                config_.tipGateCapMs);
        if (healthyForwardCrossing && ffFire) {
            const double clockDueMs = firedOnMeterAnchor
                ? shot_.anchorValidMs + meterThresh
                : shot_.holdStartMs + holdThresh;
            shot_.tipGateDeferMs = std::max(0.0, now - clockDueMs);
            ffFire = false;
        }
        // Open-loop primary (Layer B): fast meters (Standstill/Fade) AND any Tempo shot prefer the
        // deterministic clock once armed. Every eligible path still requires current genuine vision.
        const bool ffEligible = fastFeedforwardType
            || globalApplies;                         // autonomous: the ONE global clock drives all non-Go-To
        auto ffReasonText = [&](bool meterAnchor, double clockMs) {
            const QString anchorLabel = meterAnchor
                ? QStringLiteral("meter-appear") : QStringLiteral("hold-start");
            return QStringLiteral(
                "Released on learned %1ms %2 clock (current meter authorized; view-latency compensated)")
                    .arg(clockMs, 0, 'f', 0).arg(anchorLabel);
        };
        // === RC-3/C3 LEGACY BLIND-FIRE DIAGNOSTIC (flag-gated, default OFF) ===
        // Retained for telemetry/config compatibility. clockVisionAuthorized now prevents ffFire on a
        // rejected, stale, duplicate, memory, or missing frame before this block is reached. If a future
        // refactor weakens that invariant, this remains a defense-in-depth soft skip; the bounded cap
        // aborts automation rather than manufacturing a release.
        bool blindSuppressed = false;
        if (config_.blindFireSuppressEnabled && ffFire && ffEligible) {
            const bool blindNoDetection =
                shot_.detectionPresence == QLatin1String("rejected") && !ffFillKnown;
            if (blindNoDetection) {
                const double clockDueMs = firedOnMeterAnchor
                    ? shot_.anchorValidMs + meterThresh
                    : shot_.holdStartMs + holdThresh;
                const bool onSchedule = std::abs(now - clockDueMs) <= config_.blindFireSchedTolMs;
                if (!(shot_.meterSeenThisShot && onSchedule)) {
                    blindSuppressed = true;
                    ffFire = false;
                    shot_.releasePlan = QStringLiteral("Blind fire suppressed");
                    shot_.releaseReason = QStringLiteral(
                        "BLIND_SUPPRESSED: feedforward clock due on a rejected/no-fill frame (off-schedule)");
                    shot_.releaseReasonCode = QStringLiteral("blind_suppressed");
                    if (!blindSuppressLoggedThisShot_) {
                        blindSuppressLoggedThisShot_ = true;
                        emit blindFireSuppressed(QStringLiteral(
                            "BLIND_SUPPRESSED %1: feedforward clock landed on a rejected/no-fill frame; "
                            "meterSeen=%2 held for a fresh read / hard cap")
                                .arg(shot_.shotType)
                                .arg(shot_.meterSeenThisShot ? 1 : 0));
                    }
                }
            }
        }
        // === C2 PRIOR->POSTERIOR BLEND (flag-gated, default OFF) ===
        // The hard feedforward preempt below fires on the clock and RETURNS before the measured-lead /
        // vision predictive blocks ever run — so a fast meter never applies the vision stack. Replace it
        // with an inverse-variance fuse of the clock deadline (prior) and the vision crossing (posterior):
        // fire at the fused deadline, degrading smoothly to the pure clock as vision confidence/freshness
        // fall and converging onto the vision tip as its variance falls. Byte-identical to the hard preempt
        // when the flag is off.
        if (config_.priorPosteriorBlendEnabled && ffFire && ffEligible) {
            const double clockDueMs = firedOnMeterAnchor
                ? shot_.anchorValidMs + meterThresh
                : shot_.holdStartMs + holdThresh;
            const double visionFireAtMs = crossing - effectiveLatency;
            const bool visionUsable = crossing > 0.0 && genuineVisionFrame;
            const double fused = blendedFireDeadlineMs(
                clockDueMs, visionFireAtMs, visionUsable, shot_.confidence,
                config_.blendClockPriorSdMs, config_.blendVisionBaseSdMs, config_.blendVisionMaxSdMs);
            shot_.plannedFlickMs = clockUsedMs;
            if (now + 1e-6 >= fused) {
                // The fused deadline is due -> fire on the blend. (When vision is low-confidence the fuse
                // sits at ~clockDue, so this fires the same tick the clock would have — no degradation.)
                shot_.releasePlan = QStringLiteral("Prior-posterior blend");
                shot_.releaseReason = QStringLiteral("Blended clock+vision fire (fused deadline due)");
                shot_.releaseReasonCode = QStringLiteral("feedforward_target");
                if (triggerRelease(now)) {
                    clearReleaseOutput(output);
                } else {
                    restoreAbortOutput(output);
                }
                return;
            }
            // Fused deadline still ahead (confident vision says the tip is later) -> hand it to the sub-tick
            // scheduler and wait; the clock no longer preempts. If the scheduler refuses, fall through and
            // re-blend next tick (now advances toward the fuse -> it fires within a tick of it).
            if (scheduleFire(fused, now)) {
                schedFirePlan_ = QStringLiteral("Prior-posterior blend");
                schedFireReason_ = QStringLiteral("Blended clock+vision fire");
                schedFireCode_ = QStringLiteral("feedforward_target");
                shot_.releasePlan = QStringLiteral("Scheduled fire");
                shot_.releaseReason = QStringLiteral("release_scheduled");
                shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                return;
            }
            // scheduler refused -> don't preempt on the raw clock this tick; re-evaluate next tick.
            ffFire = false;
            blindSuppressed = true;   // reuse the "skip the scheduled-clock block below" guard
        }
        // [ORION_FUSED_FIRE] Under fused ownership the feedforward clock is a BACKSTOP, not a
        // preempt: it fires only when the posterior lost authority (never anchored / sigma
        // blown / no measured lead — fusedOwns false covers all three). The tip-gate's defer
        // job and the prior-posterior blend are both superseded by the fusion itself.
        if (fusedOwns) {
            ffFire = false;
        }
        if (ffFire && ffEligible) {
            shot_.plannedFlickMs = clockUsedMs;
            shot_.releasePlan = QStringLiteral("Feedforward clock");
            shot_.releaseReason = ffReasonText(firedOnMeterAnchor, clockUsedMs);
            shot_.releaseReasonCode = QStringLiteral("feedforward_target");
            if (triggerRelease(now)) {
                clearReleaseOutput(output); // drop Square THIS tick
            } else {
                restoreAbortOutput(output);
            }
            return;
        }
        // Sub-tick: the clock deadline lands within the scheduler horizon — hand it to the
        // precise fire thread instead of letting the next 4ms tick fire it 0-4ms late. The
        // EARLIEST applicable deadline is scheduled (matches the in-tick firing order above:
        // whichever clock comes due first fires).
        if (!ffFire && ffEligible && !blindSuppressed && !fusedOwns
            && !healthyForwardCrossing) {
            double ffDeadline = -1.0;
            bool schedOnMeterAnchor = false;
            double schedClockMs = 0.0;
            // Gate each candidate by the SAME reachability/floor checks as the in-tick fire (do #1/#2):
            // a deadline is committed to the precise fire thread only when the fire would be allowed —
            // when currently out of reach we wait and re-evaluate next tick rather than schedule a fire
            // that the scheduler can no longer reconsider.
            if (meterAnchorAvail && clockVisionAuthorized && meterAnchorFireOk) {
                ffDeadline = shot_.anchorValidMs + meterThresh;   // T2: validated-anchor deadline
                schedOnMeterAnchor = true;
                schedClockMs = meterThresh;
            }
            // [ORION_GOTO_METER_WAIT] (2b) the Go-To hold-clock gate applies to the scheduled
            // deadline exactly as to the in-tick fire. The precise thread also rechecks current
            // authority at submit time, so a later dropout cancels the deadline.
            if (holdClockArmed && clockVisionAuthorized && ffWithinReach) {
                const double holdDeadline = shot_.holdStartMs + holdThresh;
                if (ffDeadline < 0.0 || holdDeadline < ffDeadline) {
                    ffDeadline = holdDeadline;
                    schedOnMeterAnchor = false;
                    schedClockMs = holdThresh;
                }
            }
            if (ffDeadline > now && scheduleFire(ffDeadline, now)) {
                shot_.plannedFlickMs = schedClockMs;
                schedFirePlan_ = QStringLiteral("Feedforward clock");
                schedFireReason_ = ffReasonText(schedOnMeterAnchor, schedClockMs);
                schedFireCode_ = QStringLiteral("feedforward_target");
                shot_.releasePlan = QStringLiteral("Scheduled fire");
                shot_.releaseReason = QStringLiteral("release_scheduled");
                shot_.releaseReasonCode = QStringLiteral("release_scheduled");
                return;
            }
        }
    }

    // === No-meter pose-based timing: schedule release from pose landmarks ===
    // When noMeterEnabled, the engine uses push/release landmarks from the Python
    // sidecar instead of meter detection. Push = feedforward (fire ahead), Release =
    // reactive (fires late). Safety: if no landmark by noMeterAbortMs, clean abort.
    if (config_.noMeterEnabled) {
        // Per-shot-type offset from the no-meter offset table (same 8 types as meter mode).
        double shotTypeOffset = 0.0;
        auto it = config_.noMeterShotTypeOffsets.find(shot_.shotType);
        if (it != config_.noMeterShotTypeOffsets.constEnd()) {
            shotTypeOffset = it.value();
        }
        const double totalOffset = config_.noMeterBaseOffsetMs + shotTypeOffset;
        const bool poseAuthorityCurrent = poseReleaseAuthorityCurrent(now);
        if (!pose_.poseScheduled) {
            // (B) Feedforward off the per-type hold-start clock once it's been seeded. This reuses the
            // SAME proven feedforward that greens Standstill in meter mode (shotTypeFeedforwardMs), now
            // fed by the no-meter zero-cross. Firing AHEAD off the learned clock covers the
            // ViGEm/RemotePlay latency the reactive crossing can't -- greens with NO input hook. The
            // clock is seeded by the FIRST shot of each type (the reactive crossing below, which is
            // tagged vision-timed so the learner at ~2180 records holdStart->crossing). Calibrate the
            // fire-ahead with noMeterBaseOffsetMs (negative = earlier) live, same as the meter offset.
            // Output mode does not change this canonical per-type clock.
            const double ffClock = seededValue(config_.shotTypeFeedforwardMs,
                                               shot_.bucketKey, shot_.shotType);
            // A learned hold clock is timing, never authority: it may act only
            // while a current, unique pose landmark from THIS shot owns a lease.
            if (poseAuthorityCurrent && ffClock > config_.feedforwardMinMs
                && shot_.holdStartMs > 0.0) {
                const double fireTime = shot_.holdStartMs + ffClock + totalOffset
                                        - config_.noMeterDecodeCompMs;
                if (fireTime <= now) {
                    shot_.releasePlan = QStringLiteral("Pose feedforward");
                    shot_.releaseReason = QStringLiteral("pose_feedforward");
                    shot_.releaseReasonCode = QStringLiteral("pose_feedforward_target");
                    triggerRelease(now);
                    clearReleaseOutput(output);
                    return;
                }
                // Future fire (ahead off the clock): schedule for sub-tick precision, but if the scheduler
                // REFUSES leave it UNSCHEDULED so the in-tick check above fires it when now reaches fireTime
                // -- never set poseScheduled on a phantom schedule (that gated off the abort -> 2.5min hang).
                if (scheduleFire(fireTime, now, -1.0, ScheduledFireAuthority::Pose)) {
                    pose_.poseScheduled = true;
                    shot_.releasePlan = QStringLiteral("Pose feedforward scheduled");
                    shot_.releaseReason = QStringLiteral("pose_feedforward_scheduled");
                    shot_.releaseReasonCode = QStringLiteral("pose_feedforward_target");
                }
                return;
            }
            const QString anchor = config_.noMeterReleasePoint.toLower();
            // Prefer the controller-armed zero-cross RELEASE (GLM's validated 63ms path) whenever it
            // arrives -- it emits only a release (no push), so the legacy "Push" anchor default must
            // not gate it out. This reactive fire is the bootstrap that seeds ffClock for next time.
            if (poseAuthorityCurrent && pose_.releaseReceived) {
                const double fireTime = pose_.releaseArrivalMs + totalOffset - config_.noMeterDecodeCompMs;
                if (fireTime <= now) {
                    shot_.releasePlan = QStringLiteral("Pose zero-cross (reactive bootstrap)");
                    shot_.releaseReason = QStringLiteral("pose_release");
                    shot_.releaseReasonCode = QStringLiteral("pose_release_reactive");
                    triggerRelease(now);
                    clearReleaseOutput(output);
                    return;
                }
                // A future deadline outside the scheduler horizon stays unscheduled
                // and is reconsidered next tick. Never fire it early merely because
                // the precision worker correctly refused a far-future arm.
                if (scheduleFire(fireTime, now, -1.0, ScheduledFireAuthority::Pose)) {
                    pose_.poseScheduled = true;
                    shot_.releasePlan = QStringLiteral("Pose zero-cross scheduled (bootstrap)");
                    shot_.releaseReason = QStringLiteral("pose_release_scheduled");
                    shot_.releaseReasonCode = QStringLiteral("pose_release_reactive");
                }
                return;
            }
            if (poseAuthorityCurrent && anchor == QStringLiteral("push") && pose_.pushReceived) {
                const double fireTime = pose_.pushArrivalMs
                    + totalOffset
                    - config_.noMeterDecodeCompMs;
                if (fireTime <= now) {
                    shot_.releasePlan = QStringLiteral("Pose push release");
                    shot_.releaseReason = QStringLiteral("pose_push");
                    shot_.releaseReasonCode = QStringLiteral("pose_push_feedforward");
                    triggerRelease(now);
                    clearReleaseOutput(output);
                    return;
                }
                if (scheduleFire(fireTime, now, -1.0, ScheduledFireAuthority::Pose)) {
                    pose_.poseScheduled = true;
                    shot_.releasePlan = QStringLiteral("Pose push scheduled");
                    shot_.releaseReason = QStringLiteral("pose_push_scheduled");
                    shot_.releaseReasonCode = QStringLiteral("pose_push_scheduled");
                }
                return;
            } else if (poseAuthorityCurrent && anchor == QStringLiteral("release")
                       && pose_.releaseReceived) {
                const double fireTime = pose_.releaseArrivalMs
                    + totalOffset
                    - config_.noMeterDecodeCompMs;
                if (fireTime <= now) {
                    shot_.releasePlan = QStringLiteral("Pose release (reactive)");
                    shot_.releaseReason = QStringLiteral("pose_release");
                    shot_.releaseReasonCode = QStringLiteral("pose_release_reactive");
                    triggerRelease(now);
                    clearReleaseOutput(output);
                    return;
                }
                if (scheduleFire(fireTime, now, -1.0, ScheduledFireAuthority::Pose)) {
                    pose_.poseScheduled = true;
                    shot_.releasePlan = QStringLiteral("Pose release scheduled");
                    shot_.releaseReason = QStringLiteral("pose_release_scheduled");
                    shot_.releaseReasonCode = QStringLiteral("pose_release_scheduled");
                }
                return;
            }
            // Push→Release validation: if push was received but no release within the
            // validation window, the push was likely a false positive — abort.
            if (pose_.pushReceived && !pose_.releaseReceived) {
                const double sincePush = now - pose_.pushArrivalMs;
                if (sincePush > config_.noMeterPushReleaseWindowMs) {
                    shot_.releasePlan = QStringLiteral("Abort (push without release)");
                    shot_.releaseReason = QStringLiteral("pose_push_no_release");
                    shot_.releaseReasonCode = QStringLiteral("pose_push_no_release_abort");
                    relinquishAutonomousLiveMeterShot(
                        output, QStringLiteral("pose_push_no_release"));
                    return;
                }
            }
        }
        // Safety fallback: no pose landmark by timeout -> clean abort
        if (!pose_.poseScheduled && elapsed >= noMeterAbortMs) {
            shot_.releasePlan = QStringLiteral("Abort (no pose landmark)");
            shot_.releaseReason = QStringLiteral("no_pose_landmark");
            shot_.releaseReasonCode = QStringLiteral("no_pose_landmark_abort");
            relinquishAutonomousLiveMeterShot(
                output, QStringLiteral("no_pose_landmark"));
            return;
        }
        shot_.releasePlan = QStringLiteral("Waiting for pose landmark");
        shot_.releaseReason = QStringLiteral("waiting_for_pose");
        shot_.releaseReasonCode = QStringLiteral("await_pose");
        return;
    }

    // === Meter-gated: NEVER release before a real meter has surfaced. ===
    // This lets Go-To / half-court / contested shots wait through the multi-second
    // wind-up instead of being dumped by a fixed ceiling, and stops standstill from
    // firing blind before its meter appears. If the meter never shows within the
    // hold-start cap, ABORT — no blind, mistimed shot (per the user's decision).
    if (!shot_.meterSeenThisShot) {
        if (elapsed >= noMeterAbortMs) {
            // Detector authority never arrived. Relinquish bot ownership on this
            // tick and preserve the user's physical input; do not synthesize a shot.
            shot_.releasePlan = QStringLiteral("Abort (no validated meter)");
            shot_.releaseReason = isGoto
                ? QStringLiteral("No validated meter (Go-To); control returned to player")
                : QStringLiteral("No validated meter; control returned to player");
            shot_.releaseReasonCode = QStringLiteral("no_meter_abort");
            relinquishAutonomousLiveMeterShot(output, QStringLiteral("no_meter_abort"));
            return;
        }
        shot_.releasePlan = QStringLiteral("Waiting for meter");
        shot_.releaseReason = isGoto ? QStringLiteral("waiting_for_goto_meter")
                                     : QStringLiteral("waiting_for_meter");
        shot_.releaseReasonCode = QStringLiteral("await_meter");
        return;
    }

    // Reachability sanity: only allow a PREDICTIVE/green release when the meter is
    // genuinely within one lead's worth of rise of the target. The expected rise is
    // velocity*lead, CLAMPED to maxPredictiveRisePct so a noisy fill/velocity spike
    // from the capture can't make the crossing predictor fire at, say, 29% fill when
    // green is at 98%. Critically, the clamp ALSO bounds how far below target a fast
    // meter may predictively release: beyond it the extrapolation across the meter's
    // decelerating, capping top is too unreliable, so the bot waits for the meter to
    // climb nearer the target (where it caps into the green band) instead of firing
    // wildly early and landing short. See RemapConfig::maxPredictiveRisePct.
    // Non-Go-To (Standstill/fade) uses a HIGHER predictive clamp so it can release earlier
    // than the Go-To target-31 (the +80 A/B proved target-31 lands LATE over Remote Play);
    // Go-To keeps the conservative clamp. A hard absolute floor still blocks any non-Go-To
    // predictive/green release at absurd low fill regardless of clamp/velocity.
    // [ORION_GOTO_FIRE] Go-To now uses the non-Go-To predictive reach clamp (was 25).
    const double maxRisePct = config_.nonGotoMaxPredictiveRisePct;
    const double expectedRisePct = std::clamp(velocityPctPerMs * effectiveLatency,
                                              0.0, maxRisePct);
    // FIRE NEAR THE TIP (autonomous tip-vision): only fire predictively once the fill is within a
    // tip's-worth of the top (>= tipFireMinFillPct, ~88) where the crossing IQR is tight. The per-type
    // fallback keeps its lower nonGotoMinPredictiveFillPct floor; Go-To is exempt (times vision-only).
    //
    // === C1 (2026-07-25) LEAD-AWARE FLOOR =============================================
    // A FIXED fill floor is also a LEAD CAP. Expressing a lead L at rise rate v REQUIRES firing at
    // `target - v*L`, so a floor F makes every lead longer than (target - F)/v unexpressible. Live
    // (target 99.6, v ~0.25 %/ms) the 88 floor capped the lead at ~46ms while learned_latency_ms
    // was 75 — the bot fired ~29ms LATE on every vision shot and the release fills piled up against
    // the floor (88.9/90.1/90.7/91.3/91.9) instead of spreading. Lower the EFFECTIVE floor to the
    // lead-implied fire fill, keeping tipFireMinFillPct as the CEILING (a slow meter still waits for
    // the tip) and tipFireAbsMinFillPct as the absolute sanity floor (a garbage velocity makes
    // `target - v*L` wildly negative and must not open the rung at any fill). v <= 0 keeps the base
    // floor untouched. For the per-type fallback the base floor already EQUALS the sanity floor, so
    // max(42, min(42, x)) == 42 — that path is byte-identical.
    const double baseFillFloor = globalApplies ? config_.tipFireMinFillPct
                                               : config_.nonGotoMinPredictiveFillPct;
    const double leadImpliedFloor = (velocityPctPerMs > 0.0 && effectiveLatency > 0.0)
        ? (target - velocityPctPerMs * effectiveLatency - config_.tipFireLeadSlackPct)
        : baseFillFloor;
    const double minPredictiveFillFloor =
        std::max(config_.tipFireAbsMinFillPct, std::min(baseFillFloor, leadImpliedFloor));
    const bool abovePredictiveFloor = isGoto
        || shot_.fillPct >= minPredictiveFillFloor;
    // === A3 (2026-07-25) REACHABILITY HORIZON ========================================
    // The one-lead reach window abandoned shots that WILL reach the tip (live seq 9/12/19: healthy
    // velocity, valid forward crossing at 210/355/143ms, withinReach=0 -> blind clock). Widen the
    // expected rise to what the meter will actually cover between now and its OWN predicted
    // crossing, bounded by reachabilityCrossingHorizonMs and reachabilityMaxRisePct. The gate is
    // still a gate: the lead-aware floor above decides how far below target a release may fire, and
    // every rung still fires at `crossing - effectiveLatency`, so a wider rung cannot fire earlier.
    const double crossingEtaMs = (crossing > 0.0) ? (crossing - now) : -1.0;
    const double horizonRisePct = (crossingEtaMs > 0.0 && velocityPctPerMs > 0.0)
        ? std::clamp(velocityPctPerMs * std::min(crossingEtaMs, config_.reachabilityCrossingHorizonMs),
                     0.0, config_.reachabilityMaxRisePct)
        : 0.0;
    const double reachAllowancePct =
        std::max(expectedRisePct, horizonRisePct) + config_.reachabilitySlackPct;
    const bool withinReach = (target - shot_.fillPct) <= reachAllowancePct
        && abovePredictiveFloor;

    // DIAGNOSTIC (no behavior change): stamp the release-path attribution snapshot with the
    // exact inputs the release blocks below use. Stamped here — after target/crossing/
    // velocity/withinReach are all computed and before any release block — so it is current
    // when triggerRelease fires on this same frame. Release blocks are reached only past
    // this point (commit-wait / await-meter returned earlier), so every release line reflects
    // its own frame's snapshot. Recording only; nothing here gates a release.
    shot_.greenConfirmedAtRelease = greenTracker_.confirmed();
    shot_.targetModeAtRelease = targetMode;
    shot_.greenWidthAtReleasePct = greenTracker_.confirmed() ? greenTracker_.widthPct() : 0.0;
    shot_.releaseVelocityPctMs = velocityPctPerMs;
    shot_.releaseCrossingEtaMs = crossing > 0.0 ? crossing - now : -1.0;
    shot_.expectedRiseAtReleasePct = expectedRisePct;
    shot_.withinReachAtRelease = withinReach;
    // Clock-vs-vision divergence (telemetry only): when the meter-anchor clock AND a vision
    // crossing both have a deadline this frame, log how far apart they'd fire. A persistent
    // large divergence means either the clock drifted or the ETA is lying — the live batch
    // analyzer uses it to pick which source to trust per type.
    {
        const double divMeterClock = seededValue(config_.shotTypeMeterToReleaseMs, shot_.bucketKey, shot_.shotType);
        if (crossing > 0.0 && shot_.firstMeterSeenMs >= 0.0
                && divMeterClock > config_.meterClockMinMs) {
            shot_.clockVisionDivergenceMs =
                (shot_.firstMeterSeenMs + divMeterClock) - (crossing - effectiveLatency);
        } else {
            shot_.clockVisionDivergenceMs = 0.0;
        }
    }

    // A vision release path whose deadline is still in the FUTURE records it here; after the
    // release blocks (and the Go-To trust gate) it is handed to the sub-tick scheduler when
    // it lands within the horizon, so the fire isn't quantized to the 4ms tick grid.
    double schedDeadlineMs = -1.0;
    QString schedPlan, schedReason, schedCode;

    // 1. Best path: predictive release using confirmed green-window + extrapolated crossing.
    //    [ORION_FUSED_FIRE] Skipped under fused ownership — the crossing IS a fusion source,
    //    so a separate rung firing on it would double-count the same signal with no sigma.
    //    STALENESS SIBLING (2026-07-24): gated on lastSampleFreshAccept for the same reason
    //    block 2 is (see its comment) — `withinReach` is computed from shot_.fillPct, which
    //    memory extrapolation advances by velocity*occlusion on echo frames, so a pure stream
    //    of echoes can walk an invented fill into reach and open this rung on a value the
    //    detector never observed. The crossing itself is fresh-built, but the GATE was not.
    if (!fusedOwns && genuineVisionFrame && withinReach
            && greenTracker_.confirmed() && crossing > 0.0) {
        const double fireAtMs = crossing - effectiveLatency;
        shouldRelease = now >= fireAtMs;
        shot_.releasePlan = QStringLiteral("Green window");
        shot_.releaseReason = QStringLiteral("Confirmed green-window crossing predicted (%1)").arg(targetMode);
        shot_.releaseReasonCode = QStringLiteral("green_confirmed");
        if (!shouldRelease && fireAtMs > now) {
            schedDeadlineMs = fireAtMs;
            schedPlan = shot_.releasePlan;
            schedReason = shot_.releaseReason;
            schedCode = shot_.releaseReasonCode;
        }
    }

    // Item 8: Jitter-predictive release guard. When jitter is high, require higher
    // confidence before firing predictively — the crossing prediction is less reliable.
    const double predictiveConfFloor = (networkJitterEmaMs_ > config_.jitterFloorTrigger)
        ? config_.jitterPredictiveFloor : config_.confidenceGate;

    // 2. Predictive release using velocity even without confirmed green window.
    //    If we'll cross the target within effectiveLatency ms, fire NOW so the
    //    real button press lands at the target. This avoids
    //    waiting for fillPct to actually reach target (which is always late).
    // shot_.lastSampleFreshAccept is REQUIRED here, not just meterFresh. meterFresh only means
    // "some detected sample arrived recently" — and memory ECHOES set meterDetected/lastDetectionMs
    // too, so a stream of echoes holds meterFresh true indefinitely while nothing is actually being
    // seen. Worse, memory extrapolation advances shot_.fillPct by velocity*occlusion on those echo
    // frames, so a held fill silently climbs INTO the tip band and this block fires on a value the
    // detector never observed. lastSampleFreshAccept = genuinely-fresh OR memory-trusted, and the
    // memory-trust path is itself tip-guarded (memoryTrustMaxFillPct), so gating on it keeps normal
    // vision fires intact while making a tip-band release impossible on invented fill. The
    // If fresh samples never resume, the bounded ownership cap aborts automation.
    if (!fusedOwns && !shouldRelease && genuineVisionFrame && withinReach
            && velocityPctPerMs > 0.001 && shot_.fillPct < target
            && shot_.confidence >= predictiveConfFloor) {
        double msUntilTarget = (target - shot_.fillPct) / velocityPctPerMs;
        // Item 1: Nonlinear meter-top deceleration correction (default OFF — decelCorrection
        // Factor is 0.0; the subtraction had the WRONG SIGN, see AutomationEngine.h). Kept as
        // inert plumbing so the knee/factor can be re-tried live without a code change.
        if (shot_.fillPct > config_.decelKneePct) {
            const double remainingGap = target - shot_.fillPct;
            const double decelReduction = remainingGap * config_.decelCorrectionFactor;
            msUntilTarget -= decelReduction / velocityPctPerMs;
            msUntilTarget = std::max(0.0, msUntilTarget);
        }
        // SAMPLE-AGE COMPENSATION (2026-07-24). msUntilTarget is "(target - fill)/v" — a span
        // measured from the instant the fill it was computed from was CAPTURED, i.e. from
        // shot_.lastDetectionMs, NOT from `now`. Everything downstream (the <= effectiveLatency
        // test and the `now + msUntilTarget - effectiveLatency` deadline) treats it as a span
        // from `now`. Under 30Hz sidecar telemetry (meterFreshWindowMs = 180) that silently
        // stretched the deadline by the sample's age — a pure LATE bias of 0-17ms typically and
        // up to the freshness window in a burst. Rebase the span onto `now` by subtracting the
        // sample's age. Blocks 1 / the fused posterior / the tip gate are NOT touched: they use
        // `crossing`, which is already an ABSOLUTE stamp on the arrival timeline (the sampler
        // builds it from sample timestamps), so subtracting age there would double-count.
        msUntilTarget = std::max(0.0, msUntilTarget - std::max(0.0, now - shot_.lastDetectionMs));
        shot_.releaseEtaMs = msUntilTarget;
        if (msUntilTarget <= effectiveLatency) {
            shouldRelease = true;
        }
        shot_.releasePlan = QStringLiteral("Trajectory tip");
        shot_.releaseReason = QStringLiteral("Fill trajectory reaches target inside release lead (%1)").arg(targetMode);
        shot_.releaseReasonCode = QStringLiteral("predictive_target");
        if (!shouldRelease && schedDeadlineMs < 0.0) {
            const double fireAtMs = now + msUntilTarget - effectiveLatency;
            if (fireAtMs > now) {
                schedDeadlineMs = fireAtMs;
                schedPlan = shot_.releasePlan;
                schedReason = shot_.releaseReason;
                schedCode = shot_.releaseReasonCode;
            }
        }
    }

    // 3. ML-predicted ETA path: use the Python-side etaToGreenMs when it is
    //    confident and imminent, even before velocity extrapolation fires.
    //    TARGET MISMATCH (2026-07-24): etaToGreenMs is the detector's ETA to the GREEN CENTRE,
    //    but under autonomous tip-vision every other rung aims the meter TIP (fill=100). The
    //    green centre arrives ~19ms before the tip, so this rung fired ~19ms early against a
    //    point nothing else is aiming at — and it stamps the SAME releaseReasonCode as block 2
    //    ("predictive_target"), so the early fires were invisible in telemetry. Gate it to the
    //    per-type fallback (!globalApplies), which does aim a green-derived target.
    //    Also fresh-gated (staleness sibling — see block 1/2).
    if (!fusedOwns && !shouldRelease && !globalApplies && genuineVisionFrame
            && withinReach && shot_.etaToGreenMs >= 0.0
            && shot_.etaToGreenMs <= effectiveLatency
            && shot_.confidence >= config_.confidenceGate) {
        shouldRelease = true;
        shot_.releasePlan = QStringLiteral("ETA prediction");
        shot_.releaseReason = QStringLiteral("Detector ETA is inside release lead");
        shot_.releaseReasonCode = QStringLiteral("predictive_target");
    }

    // Item 2: Dual-signal consensus release. When two independent release signals agree
    // within consensusWindowMs, fire at the EARLIER one — the later is already late.
    // This reduces timing scatter from jitter by requiring corroboration.
    //    Fresh-gated (staleness sibling — see block 1/2): both legs read shot_.fillPct, which
    //    memory extrapolation invents on echo frames.
    if (!fusedOwns && !shouldRelease && config_.consensusReleaseEnabled
            && genuineVisionFrame && withinReach
            && shot_.confidence >= config_.confidenceGate) {
        double greenCrossingMs = -1.0;
        double velocityCrossingMs = -1.0;
        if (greenTracker_.confirmed() && crossing > 0.0) {
            greenCrossingMs = crossing - effectiveLatency;
        }
        if (velocityPctPerMs > 0.001 && shot_.fillPct < target) {
            double msToTarget = (target - shot_.fillPct) / velocityPctPerMs;
            if (shot_.fillPct > config_.decelKneePct) {
                msToTarget -= (target - shot_.fillPct) * config_.decelCorrectionFactor / velocityPctPerMs;
                msToTarget = std::max(0.0, msToTarget);
            }
            // Same sample-age rebase as block 2 — this IS block 2's deadline, and the whole
            // point of the consensus test is that the two legs are directly comparable.
            msToTarget = std::max(0.0, msToTarget - std::max(0.0, now - shot_.lastDetectionMs));
            velocityCrossingMs = now + msToTarget - effectiveLatency;
        }
        if (greenCrossingMs > 0.0 && velocityCrossingMs > 0.0) {
            const double gap = std::abs(greenCrossingMs - velocityCrossingMs);
            if (gap <= config_.consensusWindowMs) {
                const double earlierFire = std::min(greenCrossingMs, velocityCrossingMs);
                if (earlierFire <= now) {
                    shouldRelease = true;
                    shot_.releasePlan = QStringLiteral("Consensus release");
                    shot_.releaseReason = QStringLiteral("Green + velocity crossing agree within %1ms").arg(
                        QString::number(gap, 'f', 1));
                    shot_.releaseReasonCode = QStringLiteral("consensus_target");
                } else if (schedDeadlineMs < 0.0) {
                    schedDeadlineMs = earlierFire;
                    schedPlan = QStringLiteral("Consensus fire");
                    schedReason = QStringLiteral("Scheduled consensus release");
                    schedCode = QStringLiteral("consensus_target");
                }
            }
        }
    }

    // The reactive path (#4) only fires on a meter that genuinely ROSE into the target this
    // shot. A fresh sample must have been seen at least reactiveRiseMarginPct below target —
    // otherwise a meter that is already at/above target from the first sample is a lingering
    // prior-shot meter or a weak false-positive (live: a Standstill fed conf~0.50 fill=100
    // with no green and reactive-fired ~150ms in — "doesn't time at all"), NOT this shot's
    // own timed meter. The sample must also be a fresh accept (never a stale_or_memory echo).
    const bool meterRoseIntoTarget = shot_.minFreshFillPct <= (target - config_.reactiveRiseMarginPct);

    // 4. Reactive path (last-resort safety): we're already at or past target. NOT for
    //    Go-To — a reactive release at fill>=target lands LATE (the meter is already at
    //    the top by the time the press + Remote Play pipeline latency register; live the
    //    12:53 batch showed Go-To reactive releases at 90-100% all landing late). A Go-To
    //    times its release ONLY predictively (blocks 1-3, which fire BEFORE target); if it
    //    only ever reaches target without a timeable crossing it waits, then aborts.
    // [ORION_GOTO_FIRE] Reactive-at-target is now ENABLED for Go-To (was `!isGoto`): a Go-To that
    // reaches target on a fresh meter that genuinely rose into it releases like every other type.
    if (!shouldRelease && genuineVisionFrame && shot_.fillPct >= target
            && shot_.confidence >= config_.confidenceGate && meterRoseIntoTarget) {
        shouldRelease = true;
        shot_.releasePlan = QStringLiteral("Target reached");
        shot_.releaseReason = QStringLiteral("Meter fill reached target threshold");
        shot_.releaseReasonCode = QStringLiteral("reactive_target");
    }

    // 4b. [ORION_FADE_SAFETY] Fade-only overshoot bound. A fade times on its per-type clock; if
    //     that clock runs a touch late and none of the predictive/green/reactive paths above have
    //     fired, a fade that has already climbed to ~fadeSafetyFillPct (or the confirmed green
    //     ENTRY, whichever is LOWER) is inside the made-shot window NOW and about to overshoot into
    //     a LATE miss. 2K's make-window is more forgiving slightly-early than late, so fire NOW.
    //     Fades ONLY (isFade) — standstill/others already land and are excluded. Gated on a fresh
    //     genuine accept that rose into the threshold (never a phantom/lingering at-fill echo).
    if (!shouldRelease && isFade && genuineVisionFrame
            && shot_.confidence >= config_.confidenceGate) {
        double fadeSafetyFloor = config_.fadeSafetyFillPct;
        if (greenTracker_.confirmed() && greenTracker_.startPct() >= 0.0) {
            fadeSafetyFloor = std::min(fadeSafetyFloor, greenTracker_.startPct());
        }
        if (shot_.fillPct >= fadeSafetyFloor && meterRoseIntoTarget) {
            shouldRelease = true;
            shot_.releasePlan = QStringLiteral("Fade safety");
            shot_.releaseReason = QStringLiteral("Fade reached make-window floor (%1%) before clock/vision fired")
                .arg(QString::number(fadeSafetyFloor, 'f', 1));
            shot_.releaseReasonCode = QStringLiteral("fade_safety_late");
        }
    }

    // Honest waiting telemetry: the predictive blocks above set releaseReasonCode
    // to green_confirmed/predictive_target/reactive_target as a SIDE EFFECT of
    // evaluating each path — even when shouldRelease stayed false (e.g. green is
    // confirmed but the crossing is still ms away). Leaving that code in place on a
    // non-releasing frame falsely tells live telemetry a release path fired. When we
    // are actually still holding, overwrite it with a waiting code so the logged
    // `code=` distinguishes a true release from "we have a plan but are still
    // waiting" / a stale sample. The genuine release reason for a shot that DOES fire
    // is set by the release blocks (above and the fallbacks below) on that frame.
    if (!shouldRelease && !meterFresh) {
        // Meter WAS seen this shot (we passed the await_meter early return) but the
        // latest sample is now stale. Distinct from the never-saw-a-meter await_meter
        // path, so give it its OWN human-readable reason too — NOT the identical
        // "waiting_for_meter" text the await_meter path uses — so live telemetry can
        // tell "meter went stale mid-shot" apart from "meter never appeared" (do #4).
        shot_.releasePlan = QStringLiteral("Waiting");
        shot_.releaseReason = shot_.mode == ShotMode::GoToStick
            ? QStringLiteral("goto_meter_sample_stale")
            : QStringLiteral("meter_sample_stale");
        shot_.releaseReasonCode = QStringLiteral("stale_sample");
    } else if (!shouldRelease && shot_.confidence < config_.confidenceGate) {
        shot_.releasePlan = QStringLiteral("Waiting");
        shot_.releaseReason = QStringLiteral("confidence_low");
        shot_.releaseReasonCode = QStringLiteral("confidence_low");
    } else if (!shouldRelease && !withinReach) {
        // Fresh + confident meter, but still further BELOW the target than the
        // reachability cap (do #2) allows a predictive release. The bot is waiting
        // SPECIFICALLY because the cap is holding a (typically fast) meter back until
        // it climbs near the top — distinct from "within reach but the crossing
        // isn't imminent yet" (eta_not_ready). Surfacing this as its own code lets
        // live telemetry confirm the cap is doing its job instead of misreading a
        // generic "eta not ready".
        shot_.releasePlan = QStringLiteral("Waiting");
        shot_.releaseReason = shot_.mode == ShotMode::GoToStick
            ? QStringLiteral("goto_below_reachability_cap")
            : QStringLiteral("below_reachability_cap");
        shot_.releaseReasonCode = QStringLiteral("out_of_reach");
    } else if (!shouldRelease && !isGoto && shot_.fillPct >= target
               && !(shot_.lastSampleFreshAccept && meterRoseIntoTarget)) {
        // At/above target, but the reactive path was SUPPRESSED because the meter did not
        // genuinely rise into the target this shot (lingering prior-shot meter / weak
        // false-positive at-target from frame one) or the sample is stale. The bot keeps
        // holding for THIS shot's own rising meter instead of firing instantly. Distinct
        // code so a live batch can confirm the phantom-at-target gate is doing its job.
        shot_.releasePlan = QStringLiteral("Waiting (meter did not rise into target)");
        shot_.releaseReason = QStringLiteral("at_target_no_rise");
        shot_.releaseReasonCode = QStringLiteral("phantom_target");
    } else if (!shouldRelease) {
        shot_.releasePlan = QStringLiteral("Waiting");
        shot_.releaseReason = shot_.mode == ShotMode::GoToStick
            ? QStringLiteral("goto_eta_not_ready")
            : QStringLiteral("eta_not_ready");
        shot_.releaseReasonCode = QStringLiteral("eta_not_ready");
    }

    // Single post-meter physical recovery bound. Ordinary timeout_fallback and
    // max_hold_safety releases are non-authoritative: elapsed time or a stale
    // last-seen fill can never stand in for a current genuine detector frame.
    const bool absoluteHardCap = shot_.meterSeenThisShot
        && meterElapsed >= (postMeterCeilingMs * 3.0);

    // A bounded ownership cap still prevents a stuck bot latch, but elapsed
    // time and stale fill never become release authority. Relinquish to the
    // still-held physical input instead of synthesizing a shot.
    if (!shouldRelease && absoluteHardCap) {
        shot_.releasePlan = QStringLiteral("Abort (detector authority lost)");
        shot_.releaseReason = QStringLiteral("Detector authority lost; control returned to player");
        shot_.releaseReasonCode = QStringLiteral("detector_authority_lost_abort");
        relinquishAutonomousLiveMeterShot(
            output, QStringLiteral("detector_authority_lost_abort"));
        return;
    }

    // Go-To shares the same genuine-frame authority boundary as every meter mode.
    // Only its bounded physical recovery interval differs.

    // Sub-tick scheduler: a vision deadline (block 1/2) landed within the horizon and no
    // release fired this tick — commit it to the precise fire thread. scheduleFire
    // records the current vision epoch so stale/re-lock payloads can revoke it.
    if (!shouldRelease && schedDeadlineMs > now
            && scheduleFire(schedDeadlineMs, now)) {
        schedFirePlan_ = schedPlan;
        schedFireReason_ = schedReason;
        schedFireCode_ = schedCode;
        shot_.releasePlan = QStringLiteral("Scheduled fire");
        shot_.releaseReason = QStringLiteral("release_scheduled");
        shot_.releaseReasonCode = QStringLiteral("release_scheduled");
        return;
    }

    if (shouldRelease && elapsed >= config_.minHoldMs) {
        if (triggerRelease(now)) {
            clearReleaseOutput(output);
        } else {
            restoreAbortOutput(output);
        }
    }
}

void AutomationEngine::processReleasing(ControllerState& output, double now)
{
    const double elapsed = now - shot_.releaseTriggerMs;

    applyShotReleaseEdge(output, shot_.mode, shot_.shotType, shot_.tempoFadeGesture);
    preserveTempoFadeMovementContext(output);
    // Stick-mode Square overlap is intentionally suppressed throughout the
    // active shot, then held until the physical button is genuinely released.
    if (squareLatchedUntilRelease_) {
        output.buttons &= ~XINPUT_GAMEPAD_X;
    }

    if (shot_.mode == ShotMode::GoToStick) {
        // [ORION_GOTO_DOWN_FLICK 2026-08-09] Go-To now releases with the mirrored DOWN flick
        // (+127) instead of returning to neutral, so it needs the same hold-then-neutralise
        // treatment the tempo modes get below: keep the edge visible for the console's poll
        // window, then drop the stick.
        //
        // This branch previously just ran out releasePulseMs and completed, which was correct
        // only because the old release edge WAS neutral. Left unchanged, the +127 would stay
        // pinned for the whole released-until-physical-end drain and read as a sustained
        // pro-stick down input rather than a single release edge.
        //
        // Global tempoFlickHoldMs, not the per-type shotTypeFlickHoldMs map the tempo branch
        // consults: that map has no Go-To entry, and inventing per-type flick calibration for
        // a gesture that has never once released on a live rig would be guessing.
        const double flickMs = std::max(config_.tempoFlickHoldMs, config_.releasePulseMs);
        if (elapsed >= flickMs) {
            output.rightStickY = 0;
            completeRelease(now);
        }
        return;
    }

    if (shot_.mode == ShotMode::TempoSquare || shot_.mode == ShotMode::TempoStick) {
        // Both Tempo entry paths are the same owned gesture once armed: gather
        // RS-down while the shared live-meter engine tracks the tip, then begin
        // one RS-up flick on the exact Button-equivalent release decision. Keep
        // the flick visible for the controller poll window, then neutralise.
        // TempoSquare additionally keeps Square suppressed in the canonical
        // applyShotReleaseEdge policy above.
        output.rightStickX = 0;
        // Flick-edge calibration: per-type flick hold (the edge crispness that decides which frame
        // the game registers the release), falling back to the global tempoFlickHoldMs.
        double flickHold = config_.tempoFlickHoldMs;
        if (config_.shotTypeFlickHoldMs.contains(shot_.bucketKey)) {
            flickHold = config_.shotTypeFlickHoldMs.value(shot_.bucketKey);
        } else if (config_.shotTypeFlickHoldMs.contains(shot_.shotType)) {
            flickHold = config_.shotTypeFlickHoldMs.value(shot_.shotType);
        }
        const double flickMs = std::max(flickHold, config_.releasePulseMs);
        if (elapsed < flickMs) {
            if (shot_.actualFlickMs < 0.0) {
                // Anchor-relative (matches plannedFlickMs): when the flick actually started.
                shot_.actualFlickMs = shot_.firstMeterSeenMs >= 0.0
                    ? now - shot_.firstMeterSeenMs
                    : (shot_.holdStartMs > 0.0 ? now - shot_.holdStartMs : 0.0);
            }
        } else {
            output.rightStickY = 0;
            completeRelease(now);
        }
        return;
    }

    if (elapsed >= config_.releasePulseMs) {
        completeRelease(now);
    }
}

void AutomationEngine::processPumpFake(ControllerState& output, double now)
{
    const double elapsed = now - shot_.releaseTriggerMs;
    if (elapsed < config_.pumpFakePulseMs) {
        pulsePumpFakeOutput(output);
        return;
    }

    output.buttons &= ~XINPUT_GAMEPAD_X;
    completeRelease(now);
}

void AutomationEngine::processCooldown(ControllerState& output, double now)
{
    // Square is owned by Button/TempoSquare. In stick modes it is preserved
    // unless it was physically pressed while Orion was suppressing the overlap.
    if (shot_.mode == ShotMode::ButtonShot || shot_.mode == ShotMode::TempoSquare
        || squareLatchedUntilRelease_) {
        output.buttons &= ~XINPUT_GAMEPAD_X;
    }
    if (shot_.mode == ShotMode::GoToStick || shot_.mode == ShotMode::TempoSquare
        || shot_.mode == ShotMode::TempoStick) {
        // Keep the RS pinned neutral through cooldown for every stick-driven mode so the user's still-
        // held gather (RS-down for TempoStick) doesn't leak to the game as a dribble before the latch
        // releases on neutral (do #4 hygiene; mirrors the TempoSquare/Go-To handling).
        output.rightStickX = 0;
        output.rightStickY = 0;
    }
    preserveTempoFadeMovementContext(output);
    if (now >= shot_.cooldownEndMs) {
        const double offset = shot_.networkOffsetMs;
        // B2c/C5 (2026-07-25) LATCH + HISTORY LEAK. Two separate leaks made a completed shot able
        // to strand the NEXT one:
        //  (1) the input-history leak. ShotContext{} empties xButtonHistory/rightStickYHistory,
        //      but those hold PHYSICAL input state, not shot state. processIdle's release detector
        //      is `!physical.square() && allFalse(xButtonHistory)`, and allFalse returns FALSE
        //      while the deque has fewer than 3 entries — so wiping it blinds the release detector
        //      for 3 ticks after every shot. Carry the physical history across the reset (the same
        //      save/restore idiom networkOffsetMs already uses).
        //  (2) the latch leak. abort() clears squareLatchedUntilRelease_ / the stick latches
        //      (:5578-5580) precisely so a still-owned input can never be stranded; the normal
        //      completion path did not, so a user who released Square DURING cooldown had that
        //      release thrown away with the history — the latch stayed set and their next press
        //      was suppressed by processIdle's `squareLatchedUntilRelease_ && physical.square()`
        //      branch until they released for a fresh 3 clean ticks.
        // The clear is conditioned on the input being physically RELEASED (lastPhysical_ is the
        // tick's latched physical state). Clearing unconditionally, as abort() does, is correct
        // for an abort (no shot fired, so the user's own press must reach the game) but WRONG
        // here: after a completed release a still-held Square would immediately re-arm and
        // auto-repeat the shot. Held -> keep the latch and let processIdle clear it on the real
        // release edge, which now works on the first clean tick because the history survived.
        const std::deque<bool> xHistory = shot_.xButtonHistory;
        const std::deque<int> rsHistory = shot_.rightStickYHistory;
        shot_ = ShotContext{};
        shot_.networkOffsetMs = offset;
        shot_.xButtonHistory = xHistory;
        shot_.rightStickYHistory = rsHistory;
        // A1 (2026-08-04, RELEASE-THEN-REPRESS LEAK). The clear above was conditioned on ONE
        // sample of lastPhysical_. A player shooting quickly releases Square the moment the bot
        // fires and presses again for the next shot, and releasePulseMs + cooldownMs is only
        // ~250ms, so both edges routinely land INSIDE this state. At the cooldown-end tick the
        // button is then down again, the latch was kept, and processIdle's
        // `squareLatchedUntilRelease_ && physical.square()` branch STRIPPED Square out of the
        // virtual pad for the whole of that next press — the console never saw the button at all
        // ("it doesn't register me holding square"; the player just holds the ball). The
        // intervening release edge was simply thrown away.
        //
        // physicalSquareReleaseSeenSinceArm_ is exactly the three-poll UP evidence this branch
        // already trusts, only remembered instead of sampled once. Safety is unchanged:
        //  * no auto-repeat — a CONTINUOUSLY held Square never sets the flag, so a still-held
        //    button keeps the latch and still cannot re-arm, which is the case this condition
        //    exists to defend;
        //  * no double-fire and no stale-epoch fire — releasing the latch only stops SUPPRESSING
        //    the player's own button. Arming still requires a fresh physical epoch
        //    (processIdle's retiredPhysicalShotEpoch_ fence, which beginShot stamped) plus the
        //    full strict meter ownership proof for that epoch;
        //  * no manual shot is stolen — this can only ever hand the player MORE of their own
        //    input, never less.
        if (!lastPhysical_.square() || physicalSquareReleaseSeenSinceArm_) {
            squareLatchedUntilRelease_ = false;
            squareHoldStartMs_ = -1.0;
        }
        const bool rsNeutral = std::hypot(static_cast<double>(lastPhysical_.rightStickX),
                                          static_cast<double>(lastPhysical_.rightStickY))
            < rawStickThreshold(config_.stickUpThreshold);
        if (rsNeutral) {
            stickDownLatchedUntilNeutral_ = false;
            stickUpLatchedUntilNeutral_ = false;
            stickDownHoldStartMs_ = -1.0;
            stickUpHoldStartMs_ = -1.0;
        }
        emit shotStateChanged(shot_);
    }
}

void AutomationEngine::forceTempoSquareGather(ControllerState& output) const
{
    // [ORION_TEMPO_FADE_MIRROR] A fade gathers UP and releases DOWN — the mirror of
    // every other shot type. Gather and release both read the SAME shot type, so the
    // gesture stays self-consistent for the whole press (the old invariant that
    // classification must never flip direction MID-press is preserved). Holding
    // RS-down through a fade is the game's step-back input; see ShotReleasePolicy.h.
    // Latched at arm time; see ShotContext::tempoFadeGesture. Before a shot owns the
    // press (pending Square edge, not yet armed) fall back to the pending classification
    // so the very first gather packet already matches what the release will do.
    const bool fade = shot_.armToken != 0
        ? shot_.tempoFadeGesture
        : tempoGestureIsFade(pendingSquareShotType_, config_.tempoFadeMirrorGesture);
    output.buttons &= ~XINPUT_GAMEPAD_X;
    output.rightStickX = 0;
    output.rightStickY = fade ? -127 : 127;
    if (shot_.mode == ShotMode::TempoSquare) {
        preserveTempoFadeMovementContext(output);
    } else {
        preserveFadeMovementVector(
            output, pendingSquareShotType_, pendingSquareLsArmX_,
            pendingSquareLsArmY_, pendingSquareMovementValid_);
    }
}

void AutomationEngine::preserveTempoFadeMovementContext(
    ControllerState& output) const
{
    const bool tempoMode = shot_.mode == ShotMode::TempoSquare
        || shot_.mode == ShotMode::TempoStick;
    if (!tempoMode) {
        return;
    }

    preserveFadeMovementVector(
        output, shot_.shotType, shot_.lsArmX, shot_.lsArmY, true);
}

void AutomationEngine::preservePendingTempoStickMovementContext(
    ControllerState& output) const
{
    preserveFadeMovementVector(
        output, pendingTempoStickShotType_, pendingTempoStickLsArmX_,
        pendingTempoStickLsArmY_, pendingTempoStickMovementValid_);
}

void AutomationEngine::preserveFadeMovementVector(
    ControllerState& output, const QString& shotType,
    double armX, double armY, bool valid) const
{
    if (!valid) {
        return;
    }

    const bool fade = shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive);
    const double armedMagnitude = std::hypot(armX, armY);
    if (fade && armedMagnitude >= config_.movingSquareThreshold) {
        // Once LS is acknowledged, Tempo owns one immutable movement vector for
        // the entire gather/flick/drain. Letting a later strong physical sample
        // replace it is exactly the Square+LS race that selects a step-back.
        output.leftStickX = std::clamp(static_cast<int>(std::lround(armX)), -127, 127);
        output.leftStickY = std::clamp(static_cast<int>(std::lround(armY)), -127, 127);
    } else {
        // Standstill and No-Dip explicitly commit neutral movement. Leaving a
        // later physical LS deflection live would turn their generated RS edge
        // into an unintended fade/step-back.
        output.leftStickX = 0;
        output.leftStickY = 0;
    }
}

void AutomationEngine::forceHeldOutput(ControllerState& output) const
{
    switch (shot_.mode) {
    case ShotMode::TempoSquare:
        // Tempo remap: the bot OWNS the shot as the right-stick tempo motion, so the
        // physical Square is suppressed (no double-input) and the stick is driven to
        // load the tempo while the meter rises. The flick that fires the shot happens
        // at release (processReleasing). Every shot type uses the invariant
        // gather-DOWN (+127), flick-UP (-127) gesture. Classification may change
        // timing metadata. Direction now varies by FADE vs non-fade (latched at arm
        // time, never re-derived mid-press). GoToStick uses -127 for up.
        forceTempoSquareGather(output);
        break;
    case ShotMode::TempoStick:
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        // [ORION_TEMPO_FADE_MIRROR] owned gather reads the arm-time latch.
        output.rightStickY = shot_.tempoFadeGesture ? -127 : 127;
        preserveTempoFadeMovementContext(output);
        break;
    case ShotMode::ButtonShot:
        output.buttons |= XINPUT_GAMEPAD_X;
        break;
    case ShotMode::GoToStick:
        output.buttons &= ~XINPUT_GAMEPAD_X;
        output.rightStickX = 0;
        output.rightStickY = -127;
        break;
    }
}

void AutomationEngine::pulsePumpFakeOutput(ControllerState& output) const
{
    output.buttons |= XINPUT_GAMEPAD_X;
}

void AutomationEngine::cancelPostReleaseGrade(int releaseSeq)
{
    // Close the open post-release meter capture WITHOUT grading it: the release for this seq never
    // reached the console, so the meter that follows is not this bot's shot. Matches the seq so a
    // stale cancel can't drop a newer shot's capture.
    if (meterCapActive_ && meterCapSeq_ == releaseSeq) {
        meterCapActive_ = false;
        meterCapSamples_.clear();
    }
}

// === [ORION_MEASURED_LEAD] one-shot clock re-baseline ==================================
void AutomationEngine::rebaselineLeadClocks()
{
    // The lead-absorbing clocks are the appear->releaseCmd / hold->releaseCmd durations,
    // EMA'd from fires whose lead base was learnedLatencyMs (~6). The fused anchor clocks
    // (shotTypeAppearToTipMs) are posthoc-taught in VIEW time — latency-independent — and
    // are deliberately NOT shifted.
    const double delta = measuredLatencyMs_ - config_.learnedLatencyMs;
    if (!std::isfinite(delta) || std::abs(delta) < 5.0 || std::abs(delta) > 200.0) {
        // A tiny delta needs no re-baseline; an absurd one means a broken oracle — either
        // way, do NOT mutate the clocks. Latch anyway so the gate can engage (tiny delta)
        // or stay off pending investigation (absurd one logs loudly).
        config_.leadRebaselined = std::abs(delta) < 5.0;
        if (!config_.leadRebaselined) {
            emit fusedDiagnostic(QStringLiteral(
                "LeadRebaseline: REFUSED — implausible delta %1ms (measured %2, learned %3)")
                    .arg(delta, 0, 'f', 1)
                    .arg(measuredLatencyMs_, 0, 'f', 1)
                    .arg(config_.learnedLatencyMs, 0, 'f', 1));
            return;
        }
        emit fusedLearningUpdated(config_.shotTypeAppearToTipMs, config_.leadRebaselined);
        return;
    }
    if (config_.globalAppearToTipMs > config_.meterClockMinMs) {
        config_.globalAppearToTipMs += delta;
    }
    if (config_.globalHoldToReleaseMs > config_.feedforwardMinMs) {
        config_.globalHoldToReleaseMs += delta;
    }
    // [TIMING FINDING 2 / ORION_FADE_CLOCK] Fades do NOT swap their lead base when the measured
    // lead engages: their FF path subtracts shotTypeOffsetMs (unchanged by measuredLatencyMs_),
    // not effectiveLatency. So adding +delta to a fade clock shifts its fire deadline LATER by
    // exactly delta with NO compensating lead increase -> every fade fires ~delta late (the very
    // transient this rebaseline exists to prevent, inflicted on fades for a premise that does not
    // hold). Skip fade buckets; only clocks whose FF path actually rebases its lead onto the
    // measured value (Go-To and the non-fade global path) get the shift. (Go-To leads by
    // effectiveLatency, so it is correctly shifted.)
    for (auto it = config_.shotTypeFeedforwardMs.begin(); it != config_.shotTypeFeedforwardMs.end(); ++it) {
        if (it.key().contains(QStringLiteral("Fade"), Qt::CaseInsensitive)) {
            continue;
        }
        if (it.value() > config_.feedforwardMinMs) {
            it.value() += delta;
        }
    }
    for (auto it = config_.shotTypeMeterToReleaseMs.begin(); it != config_.shotTypeMeterToReleaseMs.end(); ++it) {
        if (it.key().contains(QStringLiteral("Fade"), Qt::CaseInsensitive)) {
            continue;
        }
        if (it.value() > config_.meterClockMinMs) {
            it.value() += delta;
        }
    }
    config_.leadRebaselined = true;
    emit fusedDiagnostic(QStringLiteral(
        "LeadRebaseline: shifted clocks by %1ms (measured %2 replaces learned %3; n=%4) — one-shot")
            .arg(delta, 0, 'f', 1)
            .arg(measuredLatencyMs_, 0, 'f', 1)
            .arg(config_.learnedLatencyMs, 0, 'f', 1)
            .arg(measuredLatencyN_));
    // Persist everything the shift touched + the latch itself.
    emit feedforwardUpdated(config_.shotTypeFeedforwardMs);
    emit meterClockUpdated(config_.shotTypeMeterToReleaseMs);
    emit globalTimingLearned(config_.globalAppearToTipMs, config_.globalHoldToReleaseMs,
                             config_.learnedLatencyMs, config_.globalRiseVelocityPctMs);
    emit fusedLearningUpdated(config_.shotTypeAppearToTipMs, config_.leadRebaselined);
}

// === [ORION_PROBE] warmup pump-fake latency probes =====================================
void AutomationEngine::startLatencyProbes(int count)
{
    if (shot_.state != HoldState::Idle || count <= 0 || latencyProbesActive()) {
        return;
    }
    probesRemaining_ = count;
    probeSeq_ = 0;
    probePressEndMs_ = -1.0;
    probeCallEndMs_ = -1.0;
    probeShootAtMs_ = -1.0;
    nextProbeAtMs_ = nowMs() + 50.0;
    emit fusedDiagnostic(QStringLiteral(
        "LatencyProbes: starting %1 warmup probes (call-for-ball %7ms then shoot, press %2ms, "
        "gap %3ms, stagger %4ms, spawnOffset %5ms%6)")
            .arg(count)
            .arg(config_.probePressMs, 0, 'f', 0)
            .arg(config_.probeGapMs, 0, 'f', 0)
            .arg(config_.probeStaggerMs, 0, 'f', 3)
            .arg(config_.probeSpawnOffsetMs, 0, 'f', 1)
            .arg(config_.probeSpawnOffsetMs > 0.0
                     ? QString()
                     : QStringLiteral(" — UNCALIBRATED: labels logged raw only"))
            // %7 last: QString::arg fills the LOWEST-numbered remaining placeholder on each
            // call, so the ordering of these is positional-by-number, not by appearance.
            .arg(config_.probeCallLeadMs, 0, 'f', 0));
}

void AutomationEngine::cancelLatencyProbes() noexcept
{
    probesRemaining_ = 0;
    probeSeq_ = 0;
    nextProbeAtMs_ = -1.0;
    probePressEndMs_ = -1.0;
    probeCallEndMs_ = -1.0;
    probeShootAtMs_ = -1.0;
}

void AutomationEngine::probeTick(ControllerState& output, const ControllerState& physical, double now)
{
    // The user touching Square means warmup is over — the run must never fight a real shot.
    if (physical.square()) {
        cancelLatencyProbes();
        emit fusedDiagnostic(QStringLiteral("LatencyProbes: cancelled (physical Square pressed)"));
        return;
    }
    if (probePressEndMs_ >= 0.0) {
        if (now < probePressEndMs_) {
            output.buttons |= XINPUT_GAMEPAD_X;   // hold the pump-fake press
        } else {
            probePressEndMs_ = -1.0;              // release edge -> the fake cancels in-game
            if (probesRemaining_ <= 0) {
                emit fusedDiagnostic(QStringLiteral("LatencyProbes: run complete (%1 presses)")
                                         .arg(probeSeq_));
            }
        }
        return;
    }
    // Call issued, waiting for the ball to arrive before shooting.
    if (probeShootAtMs_ >= 0.0) {
        if (now < probeCallEndMs_) {
            output.buttons |= XINPUT_GAMEPAD_A;   // hold Cross -> PS_CROSS -> call for the ball
        }
        if (now >= probeShootAtMs_) {
            probeShootAtMs_ = -1.0;
            probeCallEndMs_ = -1.0;
            ++probeSeq_;
            probePressEndMs_ = now + config_.probePressMs;
            output.buttons |= XINPUT_GAMEPAD_X;
            // The marker stamps the SHOOT press, never the call: the estimator measures
            // press->meter-appear, and dating it from the call would fold the pass flight time
            // into every label.
            emit probeMarker(probeSeq_, epochNowMsF(), config_.probeSpawnOffsetMs);
        }
        return;
    }
    if (probesRemaining_ > 0 && nextProbeAtMs_ >= 0.0 && now >= nextProbeAtMs_) {
        --probesRemaining_;
        probeCallEndMs_ = now + config_.probeCallForBallMs;
        probeShootAtMs_ = now + config_.probeCallLeadMs;
        // The stagger advances each SHOOT press's phase on the 16.7ms console tick so one run
        // sweeps the tick (the sawtooth in the labels IS the input-tick phase — Phase 2). It
        // rides on nextProbeAtMs_, and probeCallLeadMs is constant, so the sweep is preserved.
        nextProbeAtMs_ = now + config_.probeGapMs + config_.probeStaggerMs;
        output.buttons |= XINPUT_GAMEPAD_A;   // call for the ball now; shoot probeCallLeadMs later
    }
}

void AutomationEngine::clearReleaseOutput(ControllerState& output) const
{
    if (shot_.state != HoldState::Releasing) {
        return; // triggerRelease failed closed; preserve physical pass-through
    }
    // Every immediate and scheduled release uses the exact same first packet as
    // the controller's precise-fire worker. Tempo modes reverse the held gather
    // on this tick; they may not wait for a later GUI processReleasing tick.
    applyShotReleaseEdge(output, shot_.mode, shot_.shotType, shot_.tempoFadeGesture);
    preserveTempoFadeMovementContext(output);
    if (squareLatchedUntilRelease_) {
        output.buttons &= ~XINPUT_GAMEPAD_X;
    }
}

bool AutomationEngine::triggerRelease(double now, bool alreadySubmitted)
{
    // Central fail-closed release boundary. Every meter path, including learned
    // clocks and immediate vision branches, must still own a genuinely detected
    // source frame whose TOTAL age (capture age + receipt elapsed) is <= the
    // strict lease. A confirmed precise fire already wrote the physical edge and
    // is consumed exactly once; its worker enforced the same expiry before submit.
    if (!alreadySubmitted && !armed()) {
        abort(QStringLiteral("automation_disarmed_abort"));
        return false;
    }
    if (!alreadySubmitted) {
        if (!config_.noMeterEnabled && !meterReleaseAuthorityCurrent(now)) {
            abort(QStringLiteral("detector_authority_lost_abort"));
            return false;
        }
        if (config_.noMeterEnabled && !poseReleaseAuthorityCurrent(now)) {
            abort(QStringLiteral("pose_authority_lost_abort"));
            return false;
        }
    }
    if (!alreadySubmitted && shot_.latencyCalibrationAutomaticProbe
        && !automaticCalibrationEpochCurrent()) {
        abort(QStringLiteral("meter_structure_unverified_abort"));
        return false;
    }
    if (!alreadySubmitted) {
        // Route delay is part of the release equation. Bind every release to the
        // controller route that was attested at the timing decision, including
        // immediate/in-tick releases that never entered the precise-fire worker.
        // A scheduled grace takeover already carries its original binding and may
        // not silently adopt a replacement route here.
        const auto liveBinding = controllerDeliveryRouteAttestationSnapshot();
        const bool carriedBinding = shot_.releaseScheduleRouteGeneration != 0
            && shot_.releaseScheduleRoute != LatencyControllerRoute::None;
        if (carriedBinding) {
            if (!controllerDeliveryRouteAttestationExpected(
                    shot_.releaseScheduleRouteGeneration,
                    shot_.releaseScheduleRoute)) {
                abort(QStringLiteral("controller_route_changed_abort"));
                return false;
            }
        } else if (liveBinding.valid()) {
            shot_.releaseScheduleRouteGeneration = liveBinding.generation;
            shot_.releaseScheduleRoute = liveBinding.route;
        }
        const bool routeSensitive = controllerRouteBindingRequired_
            && (autonomousLiveMeterTimingEnabled()
                || shot_.latencyCalibrationProbe);
        if (routeSensitive
            && (shot_.releaseScheduleRouteGeneration == 0
                || shot_.releaseScheduleRoute == LatencyControllerRoute::None)) {
            abort(QStringLiteral("controller_route_unattested_abort"));
            return false;
        }
    }
    clearScheduledFire();
    const QString decisionReason = shot_.releaseReason;
    shot_.state = HoldState::Releasing;
    shot_.releaseSeq = ++releaseCounter_;
    shot_.releaseTriggerMs = now;
    if ((shot_.mode == ShotMode::TempoSquare || shot_.mode == ShotMode::TempoStick)
        && shot_.actualFlickMs < 0.0) {
        // The first RS-up packet is emitted by clearReleaseOutput/the precise
        // worker on this exact trigger, not on the next GUI releasing tick.
        // Record both Tempo paths from the same physical edge timestamp.
        shot_.actualFlickMs = shot_.firstMeterSeenMs >= 0.0
            ? now - shot_.firstMeterSeenMs
            : (shot_.holdStartMs > 0.0 ? now - shot_.holdStartMs : 0.0);
    }
    // Orion 13.1: PTS-corrected release time removes variable pipe latency from
    // the learned feedforward clock. frameAgeMs = how old the current frame is
    // (decode -> engine). Subtracting it anchors the release to the frame's
    // actual decode time, not its arrival at the bot.
    shot_.releaseTriggerPtsMs = now - shot_.frameAgeMs;
    shot_.releaseFillPct = shot_.fillPct;
    // DIAGNOSTIC (T1 "Release vision:" line): was vision genuinely fresh at the trigger
    // instant? Same wifi-scaled window processHolding's meterFresh uses, but keyed on the
    // GENUINE-accept anchor so a stale_or_memory echo can't count as fresh here.
    {
        shot_.visionFreshAtRelease = alreadySubmitted
            || (config_.noMeterEnabled ? poseReleaseAuthorityCurrent(now)
                                       : meterReleaseAuthorityCurrent(now));
    }
    shot_.releasePlan = QStringLiteral("Release scheduled");
    shot_.releaseReason = shot_.mode == ShotMode::GoToStick
        ? QStringLiteral("goto_release")
        : QStringLiteral("release_scheduled");
    if (!decisionReason.isEmpty()
        && !decisionReason.startsWith(QStringLiteral("waiting_"))
        && decisionReason != shot_.releaseReason) {
        shot_.releaseReason += QStringLiteral(" / %1").arg(decisionReason.left(80));
    }
    const bool latencyCalibrationValidation =
        shot_.releaseReasonCode == QLatin1String("latency_calibration_validation");
    const bool latencyCalibrationRelease =
        (shot_.releaseReasonCode == QLatin1String("latency_calibration_probe")
         || latencyCalibrationValidation)
        && (!shot_.latencyCalibrationAutomaticProbe
            || alreadySubmitted
            || automaticCalibrationEpochCurrent());
    // The sidecar can emit a detector-only release-window record for every delivered native
    // marker, including calibration. Keep that identity separate from lastReleaseSeq_: the latter
    // is intentionally gameplay-outcome/learning state and calibration must never enter it.
    lastReleaseMarkerSeq_ = shot_.releaseSeq;
    lastReleaseMarkerShotType_ = shot_.shotType;
    lastReleaseMarkerPhysicalShotEpoch_ = shot_.physicalShotEpoch;
    lastReleaseMarkerShotAttempt_ = shot_.armToken;
    // Calibration releases are oracle probes, not gameplay attempts. Do not
    // open a native outcome/learning association for intentionally early probes.
    if (!latencyCalibrationRelease) {
        lastReleaseWallMs_ = now;
        lastReleaseShotType_ = shot_.shotType;
        lastReleaseBucketKey_ = shot_.bucketKey;
        lastReleaseSeq_ = shot_.releaseSeq;
        lastReleasePhysicalShotEpoch_ = shot_.physicalShotEpoch;
        lastReleaseShotAttempt_ = shot_.armToken;
        // [ORION_ARMED_SOURCE] Attribution for the outcome line (~1.2s later, after shot_
        // is reset). Populated by the token-consume sites; empty/-1/0 for an in-tick or
        // otherwise unattributed release, which the outcome line renders as armed_source=none.
        lastReleaseArmedSource_ = shot_.armedPredictorSource;
        lastReleaseArmedSigmaMs_ = shot_.armedPredictorSigmaMs;
        lastReleaseArmedFillPct_ = shot_.armedPredictorFillPct;
        lastReleaseArmedCommandEtaMs_ = shot_.armedPredictorCommandEtaMs;
        lastReleaseScheduleToken_ = shot_.releaseScheduleToken;
    }
    // [ORION_FUSED_FIRE] snapshot the ended shot's validated anchor for the post-hoc
    // appear->tip label (arrives ~0.5s later over IPC), and log the shadow A/B verdict.
    endedShotAppearMs_ = shot_.anchorValidMs;
    endedShotBucketKey_ = shot_.bucketKey;
    if ((config_.fusedShadowEnabled || config_.fusedFireEnabled) && fused_.anchored()) {
        const double delta = shot_.fusedShadowFireMs >= 0.0
            ? shot_.fusedShadowFireMs - now : 0.0;
        emit fusedDiagnostic(QStringLiteral(
            "FusedShadow: bucket=%1 mu=%2 sigma=%3 fireAt=%4 shadowFire=%5 actual=%6 "
            "delta=%7ms shadowFill=%8 info=%9 pair=%10 code=%11")
                .arg(shot_.bucketKey)
                .arg(fused_.muTipMs(), 0, 'f', 0)
                .arg(fused_.sigmaMs(), 0, 'f', 1)
                .arg(fusedFireAtMs_, 0, 'f', 0)
                .arg(shot_.fusedShadowFireMs, 0, 'f', 0)
                .arg(now, 0, 'f', 0)
                .arg(delta, 0, 'f', 1)
                .arg(shot_.fusedShadowFillPct, 0, 'f', 1)
                .arg(fused_.infoUpdates())
                .arg(fused_.pairOverrideFired() ? 1 : 0)
                .arg(shot_.releaseReasonCode));
    }
    // RC-3: push the release COMMAND marker (seq + wall-clock EPOCH ms) so the sidecar's frozen-meter
    // latency oracle can close (t*-minus-release). `now` is the engine's monotonic fire timestamp.
    // A precise-fire confirmation can reach this GUI thread after a stall, so current epoch time is
    // catch-up time rather than physical-submit time. Map the worker's monotonic timestamp back onto
    // the epoch clock by removing that observed delay. Immediate/in-tick releases have zero delay.
    double releaseWallEpochMs = epochNowMsF();
    if (alreadySubmitted) {
        const double observedEngineNowMs = nowMs();
        if (std::isfinite(observedEngineNowMs) && std::isfinite(now)
            && observedEngineNowMs > now) {
            releaseWallEpochMs -= observedEngineNowMs - now;
        }
    }
    // A validation target may leave the engine only on a genuine calibration release AND only
    // as the complete planner-produced (target, tolerance) pair frozen when the L2 deadline was
    // armed. Anything less ships as an ordinary target-less calibration marker: the estimator
    // demotes a target-less controlled observation to a corroboration label (posterior refined,
    // authority untouched), whereas a malformed/partial validation pair rejects the ENTIRE
    // marker sidecar-side and destroys the observation. Never widen this predicate to values the
    // planner did not actually commit — an invented target could let an unvalidated stop mint
    // VALIDATED authority, the exact contract the L1/L2 protocol exists to protect.
    const bool validationTargetMarker = latencyCalibrationRelease
        && latencyCalibrationValidation
        && std::isfinite(shot_.latencyValidationTargetPct)
        && shot_.latencyValidationTargetPct >= 0.0
        && std::isfinite(shot_.latencyValidationTolerancePct)
        && shot_.latencyValidationTolerancePct > 0.0;
    // The estimator hard-rejects any marker whose tolerance exceeds its 50pp sanity ceiling.
    // It also independently applies min(marker tolerance, its own anchor-SD-derived 3-sigma)
    // when checking the stop residual, so clamping here can only make validation stricter,
    // never looser — while keeping the observation alive on an extreme-slope rise.
    const double validationToleranceMarkerPct = validationTargetMarker
        ? std::min(shot_.latencyValidationTolerancePct, 50.0) : -1.0;
    emit releaseMarker(
        shot_.releaseSeq, releaseWallEpochMs, latencyCalibrationRelease,
        validationTargetMarker ? shot_.latencyValidationTargetPct : -1.0,
        validationToleranceMarkerPct,
        shot_.physicalShotEpoch, shot_.armToken);
    // [ORION_DEV_FIRE_OFFSET] One seq-paired line per release while the sweep hook is armed, so
    // the offline sweep can join (applied offset -> banner verdict) on the release seq exactly
    // like every other per-release analysis. applied_ms=0 covers both an undisplaced token and
    // an immediate in-tick release (which has no scheduled instant to displace).
    if (devFireOffsetArmed_) {
        const double appliedMs = shot_.firedByScheduler ? lastReleaseDevOffsetMs_ : 0.0;
        emit engineDiagnostic(QStringLiteral(
            "Release devoffset: seq=%1 applied_ms=%2 scheduled=%3 shot_attempt=%4")
                                  .arg(shot_.releaseSeq)
                                  .arg(appliedMs, 0, 'f', 2)
                                  .arg(shot_.firedByScheduler ? 1 : 0)
                                  .arg(shot_.armToken));
        // Normalize to what THIS release actually carried (0 for in-tick), so the delayed
        // PHASE SAMPLE fence field ~1.2s later reports this shot's displacement, not a
        // predecessor's.
        lastReleaseDevOffsetMs_ = appliedMs;
    }
    // Open the post-release meter capture: collect the bot's own settled meter for the next
    // ~1.2s and calibrate the per-type clock from where the release actually landed vs green.
    if (!latencyCalibrationRelease) {
        startPostReleaseMeterCapture(now);
    }

    // Feedforward: learn the per-shot-type hold-start->release CLOCK from VISION-timed
    // releases (green/predictive). The deterministic meter-animation timing helps compensate
    // capture latency, but it is usable only while current genuine vision authorizes it. Each shot type
    // (animation) learns its own time => animation-length compensation. A feedforward
    // release itself is NOT used to retrain (only vision-confirmed shots set the clock).
    //
    // The seed gate is PER-TYPE, not a global outcome flag: seed THIS type's clock from its
    // first vision-timed release while the clock is still UNARMED (<= floor); once armed, the
    // HUD relay (learnFromOutcome) calibrates it and seeding stops for this type. The old
    // `!outcomeFeedbackActive_` gate was GLOBAL — the first type to earn a HUD verdict latched
    // it true and PERMANENTLY blocked every OTHER type from ever seeding (live 15:58 batch:
    // Standstill seeded + timed on its clock, but every Fade stayed ffClock=0 -> never fired
    // feedforward -> reactive -> LATE). Per-type seeding fixes that bootstrap starvation.
    // BOTH anchor clocks are seeded so the runtime A/B toggle is a clean flip:
    //   shotTypeFeedforwardMs    = holdStart->release      (hold_start anchor)
    //   shotTypeMeterToReleaseMs = firstMeterSeen->release (meter_appear anchor)
    // Each is seeded only while its own clock is still UNARMED (<= its floor); once armed the
    // outcome relay (learnFromOutcome) calibrates it and seeding stops for that clock.
    const bool visionTimed = shot_.releaseReasonCode == QStringLiteral("green_confirmed")
        || shot_.releaseReasonCode == QStringLiteral("predictive_target")
        // (B) The no-meter zero-cross crossing is a vision-timed release too -> seed THIS type's
        // hold-start clock from holdStart->crossing so the NEXT shot fires feedforward (greens).
        // The feedforward release itself (pose_feedforward_target) is NOT vision-timed, so it never
        // re-seeds -- only a fresh crossing measurement does. Matches the meter green/predictive rule.
        || shot_.releaseReasonCode == QStringLiteral("pose_release_reactive");
    // [ORION_LEAD_VISION_GATE] Fire-time vision confidence for the autonomous lead learner: the release
    // was vision-timed (NOT a blind feedforward/timeout/abort clock), vision was genuinely fresh at the
    // trigger instant, and the lock quality (detector confidence) clears the gate. Stamped now and paired
    // with lastReleaseSeq_ so learnFromOutcome (which runs ~1.2s later, after shot_ has been reset) can
    // decide whether to trust this outcome. Computed unconditionally (it is cheap + diagnostic-safe);
    // only consumed when leadLearnerVisionGate is on.
    // [ORION_DEV_FIRE_OFFSET] A deliberately displaced release may never count as a confident
    // vision-timed outcome for any learner: the fence keeps the sweep from teaching the lead.
    lastReleaseVisionConfident_ = visionTimed
        && shot_.visionFreshAtRelease
        && shot_.confidence >= config_.leadLearnVisionConfGate
        && !devFireOffsetArmed_;
    // [TIMING FINDING 3] Stamp the raw vision-vs-feedforward timing of this release so the
    // per-type calibrator (runs ~1.2s later, shot_ already reset) can attribute the outcome to
    // the ONE knob its fire path actually read (offset for vision, clock for feedforward).
    lastReleaseWasVisionTimed_ = visionTimed;
    // [ORION_DEV_FIRE_OFFSET] holdStart->release and meterSeen->release both RIDE the release
    // instant, so a commanded displacement would be seeded straight into the per-type clocks.
    if (shot_.holdStartMs > 0.0 && visionTimed && !devFireOffsetArmed_) {
        // Orion 13.1: use PTS-corrected times so variable pipe latency (2-5ms)
        // doesn't corrupt the learned clock. Both endpoints are corrected by
        // their respective frame ages, so the difference is the true animation
        // duration independent of capture pipeline jitter.
        const double ptsHoldStart = shot_.holdStartMs - shot_.holdStartFrameAgeMs;
        const double holdElapsed = shot_.releaseTriggerPtsMs - ptsHoldStart;
        // Seed-accuracy gate: the no-meter pose bootstrap fires reactively off the wrist zero-cross. Under
        // a slow pose (CPU contention / no ROI crop) the crossing arrives ~1.2s after the arm vs a real
        // shot's ~0.3-0.5s -- seeding the per-type clock from that poisons every feedforward shot after.
        // Cap the no-meter seed window tight (800ms); meter mode keeps the wide bound for Go-To wind-ups.
        const double seedMaxMs = (shot_.releaseReasonCode == QStringLiteral("pose_release_reactive"))
            ? 800.0 : 5000.0;
        if (config_.shotTypeFeedforwardMs.value(shot_.bucketKey, 0.0) <= config_.feedforwardMinMs
                && holdElapsed > config_.feedforwardMinMs && holdElapsed < seedMaxMs) {
            double& ff = config_.shotTypeFeedforwardMs[shot_.bucketKey];
            ff = (ff <= 0.0) ? holdElapsed
                             : ff * (1.0 - config_.feedforwardGain) + holdElapsed * config_.feedforwardGain;
            emit feedforwardUpdated(config_.shotTypeFeedforwardMs);
        }
        // T2: only a VALIDATED anchor may teach the meter-appear clock, and the elapsed is
        // measured from anchorValidMs (the validated episode's first sight) — a carryover
        // wind-in from firstMeterSeenMs would poison the seed.
        if (shot_.meterSeenThisShot && shot_.anchorValidMs >= 0.0) {
            const double ptsFirstMeter = shot_.anchorValidMs - shot_.anchorCandFirstFrameAgeMs;
            const double meterElapsed = shot_.releaseTriggerPtsMs - ptsFirstMeter;
            if (config_.shotTypeMeterToReleaseMs.value(shot_.bucketKey, 0.0) <= config_.meterClockMinMs
                    && meterElapsed > config_.meterClockMinMs && meterElapsed < 5000.0) {
                double& mc = config_.shotTypeMeterToReleaseMs[shot_.bucketKey];
                mc = (mc <= 0.0) ? meterElapsed
                                 : mc * (1.0 - config_.feedforwardGain) + meterElapsed * config_.feedforwardGain;
                emit meterClockUpdated(config_.shotTypeMeterToReleaseMs);
            }
        }
    }
    // Per-type velocity prior: EMA the RAW sampler velocity at vision-timed releases (the
    // moments the meter was genuinely watched into the target). Persisted; live estimates
    // are sanity-banded against it in processHolding. Fenced with the other learners while
    // the dev fire-offset sweep is armed (a displaced release samples the rise at a
    // displaced instant).
    if (visionTimed && !devFireOffsetArmed_) {
        const double v = sampler_.velocityPctPerMs();
        if (v > 0.001 && v < 2.0) {
            double& prior = config_.shotTypeVelocityPriorPctMs[shot_.bucketKey];
            prior = prior <= 0.0
                ? v
                : prior * (1.0 - config_.velocityPriorGain) + v * config_.velocityPriorGain;
            emit velocityPriorUpdated(config_.shotTypeVelocityPriorPctMs);
        }
    }
    // Hybrid global phase-clock self-learning (autonomous_vision path): SLOWLY EMA the GLOBAL
    // appear->tip clock, hold->release fallback, and rise velocity from CLEAN vision-timed releases
    // ONLY (never fallback/timeout/contested), and only for fast (non-Go-To) types — one canonical
    // model across all of them (the type-invariant rise). Runs only when the autonomous/shadow flag
    // is on, so the per-type path and learning.json are untouched by default. The learned LATENCY
    // correction is dialed separately by the post-release grader (Phase C) to avoid double-counting.
    {
        const bool isGotoRel = shot_.mode == ShotMode::GoToStick;
        // C2 (2026-07-18): these three GLOBAL EMAs were gated only on visionTimed + the
        // autonomous/shadow flag — NOT on calibrationFrozen (the freeze gate only protected
        // learnFromOutcome), so a long frozen session still slowly drifted the SHARED clock
        // (cross-type contamination: every vision-timed release of any fast type nudged the one
        // global appear->tip/hold->release/rise model). With ORION_FREEZE_CAL default-on (the
        // shipped live default) they now hold at their learning.json values; freeze OFF keeps
        // the self-learning exactly as before.
        if (visionTimed && !isGotoRel && !config_.calibrationFrozen
                && (config_.autonomousVision || config_.autonomousVisionShadow)) {
            bool changed = false;
            const double v = sampler_.velocityPctPerMs();
            if (v > 0.05 && v < 1.0) {              // plausible rise band (rejects plateau/zero/spikes)
                config_.globalRiseVelocityPctMs = config_.globalRiseVelocityPctMs <= 0.0
                    ? v : config_.globalRiseVelocityPctMs * 0.9 + v * 0.1;   // slow
                changed = true;
            }
            if (shot_.meterSeenThisShot && shot_.anchorValidMs >= 0.0) {
                // STABLE appear->tip: add back the lead we released with, so the learned clock tracks
                // the meter's make-point (latency-independent) rather than our release moment. The ff
                // fire re-subtracts the live effectiveLatency, so this never double-counts or oscillates
                // as learnedLatencyMs changes. T2: measured from the VALIDATED anchor only.
                const double ptsFirstMeter = shot_.anchorValidMs - shot_.anchorCandFirstFrameAgeMs;
                const double appearToTip = (shot_.releaseTriggerPtsMs - ptsFirstMeter) + shot_.effectiveLatencyMs;
                if (appearToTip > config_.meterClockMinMs && appearToTip < 2000.0) {
                    config_.globalAppearToTipMs = config_.globalAppearToTipMs <= 0.0
                        ? appearToTip : config_.globalAppearToTipMs * 0.85 + appearToTip * 0.15;
                    changed = true;
                }
            }
            if (shot_.holdStartMs > 0.0) {
                const double ptsHoldStart = shot_.holdStartMs - shot_.holdStartFrameAgeMs;
                const double holdToTip = (shot_.releaseTriggerPtsMs - ptsHoldStart) + shot_.effectiveLatencyMs;  // stable hold->tip
                if (holdToTip > config_.feedforwardMinMs && holdToTip < 5000.0) {
                    config_.globalHoldToReleaseMs = config_.globalHoldToReleaseMs <= 0.0
                        ? holdToTip : config_.globalHoldToReleaseMs * 0.85 + holdToTip * 0.15;
                    changed = true;
                }
            }
            if (changed) {
                emit globalTimingLearned(config_.globalAppearToTipMs, config_.globalHoldToReleaseMs,
                                         config_.learnedLatencyMs, config_.globalRiseVelocityPctMs);
            }
        }
    }
    learnFromRelease(shot_);
    emit releaseIssued(shot_);
    emit shotStateChanged(shot_);
    return true;
}

void AutomationEngine::learnFromRelease(const ShotContext& ctx)
{
    if (autonomousLiveMeterTimingEnabled()) {
        return; // production live-only timing is read-only for every legacy learner
    }
    // [ORION_GRADE_V2] sole-writer arbitration: grading v2 owns shotTypeLearnedOffsetMs; this
    // internal fill-vs-target estimate (a second writer on the same map, active until the first
    // real outcome) must stay silent under the flag.
    if (config_.gradeV2Enabled) {
        return;
    }
    // FREEZE PARITY (the leak learnFromOutcome already closed at ~:4148). The shipped launcher
    // forces ORION_FREEZE_CAL, and the global clock EMAs are gated on calibrationFrozen — but
    // this second learner was left ungated, so "frozen" calibration still mutated per-type
    // offsets on every release. That matters concretely under the active autonomous_vision
    // config: FADES are exempt from the global clock and lead their per-type clock by
    // shotTypeOffsetMs(bucketKey), so this estimate — which the comment below admits is
    // "wrong-signed for predictive shots" — walked every fade LATER, shot after shot, and
    // persisted the drift to learning.json across restarts. Freeze must mean freeze.
    if (config_.calibrationFrozen) {
        return;
    }
    // Once real HUD outcomes are flowing they are authoritative; the internal
    // releaseFillPct-vs-target estimate is blind to true pipeline latency (and
    // wrong-signed for predictive shots that release far below target), so defer.
    if (outcomeFeedbackActive_) {
        return;
    }
    if (ctx.shotType.isEmpty() || ctx.releaseFillPct <= 0.0) {
        return;
    }
    const double errorPct = ctx.releaseFillPct - ctx.targetPct;
    const double velocity = sampler_.velocityPctPerMs();
    if (velocity <= 0.001) {
        return;
    }
    const double errorMs = errorPct / velocity;
    // Outlier rejection: a release that landed wildly off (network spike, a torn
    // detection, an aborted-then-fired shot) would otherwise yank the per-type
    // offset around. Ignore implausibly large errors so one bad shot can't drift
    // the calibration — it converges from the consistent shots only.
    if (!std::isfinite(errorMs) || std::abs(errorMs) > 120.0) {
        return;
    }
    // Live closed-loop per shot type: nudge THIS type's offset toward the observed
    // timing error so the next shot of the same type (e.g. another Left Fade)
    // lands tighter. EMA = gradual + robust; clamped so it stays a correction, not
    // a runaway.
    const double alpha = 0.15;
    double& learned = config_.shotTypeLearnedOffsetMs[ctx.bucketKey];
    learned = learned * (1.0 - alpha) + errorMs * alpha;
    learned = std::clamp(learned, -120.0, 120.0);   // matches learnFromOutcome's widened clamp
    emit learningUpdated(config_.shotTypeLearnedOffsetMs);
}

void AutomationEngine::updateShotOutcome(const QString& verdict, double errorMsHint,
                                         double confidence)
{
    // Pairing window: the TIMING banner appears ~1.2s after release and persists
    // ~1.5s. Attribute a verdict only to a release whose age falls in this window,
    // and consume each release exactly once so one banner can't teach twice.
    constexpr double kMinConfidence = 0.6;
    constexpr double kMinAgeMs = 500.0;
    constexpr double kMaxAgeMs = 3000.0;
    if (verdict.isEmpty() || confidence < kMinConfidence) {
        return;
    }
    if (lastReleaseWallMs_ < 0.0 || lastReleaseSeq_ == lastOutcomeConsumedSeq_) {
        return;
    }
    const double age = nowMs() - lastReleaseWallMs_;
    if (age < kMinAgeMs || age > kMaxAgeMs) {
        return;
    }
    lastOutcomeConsumedSeq_ = lastReleaseSeq_;
    outcomeFeedbackActive_ = true;  // real ground truth now drives learning
    // An on-target (EXCELLENT/GOOD) verdict has errorMs 0 and correctly leaves the
    // offset unchanged; a non-finite hint (unreadable word) is consumed but teaches
    // nothing. Only a finite, signed residual moves the offset.
    if (std::isfinite(errorMsHint)) {
        // Button and Tempo intentionally teach the same canonical shot-type bucket.
        learnFromOutcome(lastReleaseBucketKey_, errorMsHint);
    }
    emitShotOutcome(lastOutcomeConsumedSeq_, lastReleaseShotType_, lastReleaseBucketKey_,
                    verdict, errorMsHint);
}

void AutomationEngine::recalibrateShotType(const QString& type)
{
    // User-driven RECALIBRATE: drop this type back to ACQUIRE so it re-converges, and clear its
    // green/miss/divergence streaks. The clock value is kept as a warm start (faster re-converge).
    if (type.isEmpty()) {
        return;
    }
    config_.shotTypeCalPhase[type] = 0;   // Acquire
    calGreens_[type] = 0;
    calMisses_[type] = 0;
    calRailStreak_[type] = 0;
    calRailSign_[type] = 0;
    emit calPhaseUpdated(config_.shotTypeCalPhase);
}

void AutomationEngine::lockShotType(const QString& type)
{
    // User-driven LOCK: the bot is timing this type's tip -> freeze the baseline (micro-trim only).
    if (type.isEmpty()) {
        return;
    }
    config_.shotTypeCalPhase[type] = 1;   // Lock
    calMisses_[type] = 0;                  // enter lock with a clean miss streak
    emit calPhaseUpdated(config_.shotTypeCalPhase);
}

void AutomationEngine::recalibrateAllShotTypes()
{
    // Autonomous recalibration trigger (meter style/color change, stream restart): every
    // type back to ACQUIRE with a fresh annealing window. The clocks/offsets are KEPT as
    // warm starts — only the phase + streaks reset, so a genuinely unchanged type re-locks
    // within calLockAfterGreens shots while a changed one re-converges fast.
    bool phaseChanged = false;
    for (auto it = config_.shotTypeCalPhase.begin(); it != config_.shotTypeCalPhase.end(); ++it) {
        if (it.value() != 0) {
            it.value() = 0;
            phaseChanged = true;
        }
    }
    config_.shotTypeLearnCount.clear();
    config_.shotTypeLatencyMs.clear();  // reset per-type latency residuals
    calGreens_.clear();
    calMisses_.clear();
    calRailStreak_.clear();
    calRailSign_.clear();
    bannerUnclearStreak_ = 0;
    if (phaseChanged) {
        emit calPhaseUpdated(config_.shotTypeCalPhase);
    }
}

void AutomationEngine::confirmScheduledFire(quint64 token, double actualFireMs)
{
    // Called on the engine's thread (the controller marshals the fire thread's confirm in
    // before process()). A stale token — from an arm that was since cleared/re-armed — is
    // ignored so an old confirm can never complete a different shot's release.
    if (token == 0 || token != schedFireToken_ || schedFireDeadlineMs_ < 0.0
        || schedFirePhysicalShotEpoch_ != shot_.physicalShotEpoch
        || schedFireShotAttempt_ != shot_.armToken
        || (schedFireRouteGeneration_ != 0
            && !controllerDeliveryRouteAttestationExpected(
                schedFireRouteGeneration_, schedFireRoute_))) {
        // [ORION_SILENT_FAIL] This confirm reports a press that was PHYSICALLY SUBMITTED by the
        // worker. Dropping it mutely leaves that real press with no record: the engine then
        // treats the token as unconfirmed and either double-fires in-tick or aborts, and the
        // grading attaches to the wrong edge (the frozen-lease shape of 2026-08-04). The drop
        // stays -- fail-closed identity checks are correct -- but it must be countable.
        // expected_gen is the CURRENT route attestation; route_gen is the one the token armed
        // with. Both zero when nothing is armed/attested.
        const auto expectedAttestation = controllerDeliveryRouteAttestationSnapshot();
        emit engineDiagnostic(QStringLiteral(
            "PRECISE FIRE CONFIRM DROPPED: token=%1 sched_token=%2 actual_fire_ms=%3 "
            "route_gen=%4 expected_gen=%5 physical_epoch=%6 sched_epoch=%7 "
            "shot_attempt=%8 sched_attempt=%9 reason=identity_mismatch")
                                  .arg(token)
                                  .arg(schedFireToken_)
                                  .arg(actualFireMs, 0, 'f', 3)
                                  .arg(schedFireRouteGeneration_)
                                  .arg(expectedAttestation.generation)
                                  .arg(shot_.physicalShotEpoch)
                                  .arg(schedFirePhysicalShotEpoch_)
                                  .arg(shot_.armToken)
                                  .arg(schedFireShotAttempt_));
        return;
    }
    schedFireConfirmedToken_ = token;
    schedFireActualMs_ = actualFireMs;
}

void AutomationEngine::rejectScheduledFire(quint64 token)
{
    if (token == 0 || token != schedFireToken_ || schedFireDeadlineMs_ < 0.0
        || schedFirePhysicalShotEpoch_ != shot_.physicalShotEpoch
        || schedFireShotAttempt_ != shot_.armToken) {
        return;
    }
    schedFireFailedToken_ = token;
}

void AutomationEngine::cancelScheduledFire(quint64 token)
{
    if (token == 0 || token != schedFireToken_ || schedFireDeadlineMs_ < 0.0
        || schedFirePhysicalShotEpoch_ != shot_.physicalShotEpoch
        || schedFireShotAttempt_ != shot_.armToken) {
        return;
    }
    invalidateUnconfirmedSchedule(false, "cancel_scheduled_fire");
}

const char* AutomationEngine::armGateName(ArmGate gate) noexcept
{
    switch (gate) {
    case ArmGate::DisabledOrDisarmed: return "disabled_or_disarmed";
    case ArmGate::AlreadyArmed:       return "already_armed";
    case ArmGate::DeadlinePast:       return "deadline_past";
    case ArmGate::OutsideHorizon:     return "outside_horizon";
    case ArmGate::MinHold:            return "min_hold";
    case ArmGate::NoMeterMode:        return "no_meter_mode";
    case ArmGate::NoAuthority:        return "no_authority";
    case ArmGate::AuthorityLease:     return "authority_lease";
    case ArmGate::AuthorityStaleNow:  return "authority_stale_now";
    case ArmGate::PoseAuthority:      return "pose_authority";
    }
    return "unknown";
}

void AutomationEngine::refreshScheduledFireAuthorityLease() noexcept
{
    // [ORION_ROLLING_LEASE] The lease belongs to the EVIDENCE STREAM, not to the single frame that
    // happened to be newest when the token was armed. Before this, schedFireAuthorityExpiryMs_ was
    // written once in scheduleFire() and then decayed on the wall clock, so a token armed more than
    // `age_limit - frame_age` ms ahead of its deadline was guaranteed to be fenced off before it
    // could fire -- which is precisely why scheduleFire() refused to create one in the first place.
    //
    // Roll it forward on every genuine frame that keeps this token's vision epoch. Forward ONLY: a
    // later frame may extend the authority a token already holds, but nothing may shorten it
    // (that is the fences' job) and no frame from a different trajectory epoch may touch it.
    if (schedFireDeadlineMs_ < 0.0 || !schedFireRequiresGenuineFrame_) {
        return;
    }
    if (schedFireVisionEpoch_ != shot_.visionEpoch
        || schedFirePhysicalShotEpoch_ != shot_.physicalShotEpoch
        || schedFireShotAttempt_ != shot_.armToken) {
        return;   // identity moved; the fences retire the token, this must not prolong it
    }
    if (!shot_.lastSampleGenuineAccept) {
        return;   // a memory echo is not evidence and may never extend a release lease
    }
    double refreshedMs = meterAuthorityExpiryMs();
    if (refreshedMs < 0.0) {
        return;
    }
    if (measuredLeadLastUpdateMs_ >= 0.0
        && std::isfinite(config_.measuredLeadFreshnessMs)) {
        // Same intersection scheduleFire() applies: a token may outlive neither of its authorities.
        refreshedMs = std::min(refreshedMs,
            measuredLeadLastUpdateMs_ + config_.measuredLeadFreshnessMs);
    }
    if (refreshedMs > schedFireAuthorityExpiryMs_) {
        schedFireAuthorityExpiryMs_ = refreshedMs;
    }
}

bool AutomationEngine::scheduledFireAuthorityCoversDeadline() const noexcept
{
    return schedFireDeadlineMs_ >= 0.0
        && schedFireAuthorityExpiryMs_ >= 0.0
        && schedFireDeadlineMs_ <= schedFireAuthorityExpiryMs_ + 1e-6;
}

double AutomationEngine::cleanSourceGapMs() const noexcept
{
    // The observed genuine-frame cadence, with stall-sized high-water spikes bounded away.
    // Single definition shared by the scheduler horizon, the authority-lease cadence floor and
    // the imminent-token guards, so those three can never disagree about what "one frame" is.
    const double observedGapMs = std::max(genuineFrameGapEmaMs_,
                                          genuineFrameGapHighWaterMs_);
    constexpr double kDefaultSourceGapMs = 1000.0 / 60.0;
    return std::isfinite(observedGapMs) && observedGapMs >= 1.0
        ? std::clamp(observedGapMs, 8.0, 29.0)
        : kDefaultSourceGapMs;
}

double AutomationEngine::imminentTokenWindowMs() const noexcept
{
    // [ORION_IMMINENT_TOKEN] One genuine-frame cadence, bounded. Deliberately the plain EMA
    // (not cleanSourceGapMs()'s stall-aware high-water max): a stall must NOT widen the window
    // in which an armed token is treated as un-replaceable, or one bad cadence spike would make
    // every token imminent. This is the exact expression the 4 ms tick guards already used; it
    // is centralised here so the sub-tick mirror cannot drift away from it again.
    return std::isfinite(genuineFrameGapEmaMs_) && genuineFrameGapEmaMs_ >= 1.0
        ? std::clamp(genuineFrameGapEmaMs_, 8.0, 29.0)
        : 1000.0 / 60.0;
}

bool AutomationEngine::armedTokenIrreplaceable(double now) const noexcept
{
    if (schedFireDeadlineMs_ < 0.0) {
        return false;
    }
    const double untilArmedMs = schedFireDeadlineMs_ - now;
    return untilArmedMs > 0.0 && untilArmedMs <= imminentTokenWindowMs();
}

bool AutomationEngine::armedTokenSubmitInFlight(double now) const noexcept
{
    // [ORION_INFLIGHT_TOKEN] See the contract on the declaration. The window opens AT the armed
    // deadline and closes exactly where consumeDueScheduledFire() stops waiting for the worker,
    // so the two can never disagree about how long a submit is allowed to be in flight.
    if (schedFireDeadlineMs_ < 0.0 || !std::isfinite(now)) {
        return false;
    }
    if (schedFireConfirmedToken_ == schedFireToken_ && schedFireActualMs_ >= 0.0) {
        return false;   // already confirmed; the consume path owns it, not this guard
    }
    const double graceMs = std::isfinite(config_.schedulerGraceMs)
        ? std::max(0.0, config_.schedulerGraceMs) : 0.0;
    const double sinceDeadlineMs = now - schedFireDeadlineMs_;
    return sinceDeadlineMs >= 0.0 && sinceDeadlineMs < graceMs;
}

bool AutomationEngine::noteTipPredictionInvalid(double now) noexcept
{
    if (schedFireDeadlineMs_ < 0.0) {
        // Nothing armed: there is no token to protect and no transient to time.
        tipPredictionInvalidSinceMs_ = -1.0;
        return false;
    }
    if (tipPredictionInvalidSinceMs_ < 0.0 || !std::isfinite(tipPredictionInvalidSinceMs_)
        || tipPredictionInvalidSinceMs_ > now) {
        tipPredictionInvalidSinceMs_ = now;
    }
    // (a) Pre-existing guard, unchanged: a token already inside one cadence of its own deadline
    //     cannot be replaced by anything, so a blink may not revoke it.
    if (armedTokenIrreplaceable(now)) {
        return true;
    }
    // (a2) [ORION_INFLIGHT_TOKEN] Strictly stronger than (a): the deadline has already PASSED and
    //      the worker is mid-press. A blink cannot un-press it and cannot replace it -- the only
    //      thing revoking here achieves is cancelling a submit that is already executing.
    if (armedTokenSubmitInFlight(now)) {
        return true;
    }
    // (b) [ORION_TRANSIENT_INVALID] A token further out survives a transient no longer than one
    //     source cadence -- i.e. at most one genuine frame's worth of disagreement. Measured on
    //     the 2026-08-04 batch: three lost shots were killed here 26-58 ms before their own
    //     deadline by a SINGLE frame whose combined sigma spiked to 120 ms against a 34 ms
    //     running sigma, and the next valid frame arrived with the deadline already past, so no
    //     replacement was ever possible. Beyond one cadence the disagreement is no longer a
    //     blink and the token is retired below exactly as before.
    return (now - tipPredictionInvalidSinceMs_) <= imminentTokenWindowMs();
}

void AutomationEngine::noteTipPredictionValid() noexcept
{
    tipPredictionInvalidSinceMs_ = -1.0;
}

double AutomationEngine::meterAuthorityExpiryMs() const noexcept
{
    if (!shot_.lastSampleGenuineAccept || shot_.lastFreshAcceptMs < 0.0
        || !std::isfinite(shot_.frameAgeMs) || shot_.frameAgeMs < 0.0
        || !std::isfinite(shot_.fillPct) || shot_.fillPct <= 0.0
        || !std::isfinite(shot_.confidence)
        || shot_.confidence < config_.confidenceGate) {
        return -1.0;
    }
    // [ORION_WIFI_CLIFF] A degraded link must degrade GRACEFULLY, not fall off a cliff.
    //
    // The wifi factor (0.5) tightens the freshness bar to 25 ms. That number is BELOW the age at
    // which frames physically arrive on this pipeline (live p50 11.6 ms, p90 29.8 ms), so on a
    // jittery link the early-return below fires for most frames, meterAuthorityExpiryMs() returns
    // -1, and NOTHING can ever be armed: the fire rate goes to zero rather than degrading. The
    // customer rigs will be worse than the dev rig, so this is a shipped-product cliff.
    //
    // The bar may still tighten, but it may never drop below what the observed source cadence can
    // physically deliver. Two clean source gaps is that bound: one gap of transport plus one gap
    // of decode/scan is the minimum age a genuine frame can have when the next tick reads it.
    // Floored (never raised) against the unscaled limit, so a healthy link is unchanged and a bad
    // link saturates at "one cadence" instead of collapsing.
    const double baseLimitMs = config_.strictReleaseMaxSourceAgeMs;
    const double scaledLimitMs = baseLimitMs
        * (wifiMode_ ? config_.wifiFreshnessFactor : 1.0);
    const double cadenceFloorMs = std::min(baseLimitMs, 2.0 * cleanSourceGapMs());
    const double totalAgeLimitMs = std::max(scaledLimitMs, cadenceFloorMs);
    if (shot_.frameAgeMs > totalAgeLimitMs + 1e-6) {
        return -1.0; // already expired on arrival; no zero-length authority grant
    }
    return shot_.lastFreshAcceptMs
        + (totalAgeLimitMs - shot_.frameAgeMs);
}

bool AutomationEngine::meterReleaseAuthorityCurrent(double atMs) const noexcept
{
    const double expiryMs = meterAuthorityExpiryMs();
    return expiryMs >= 0.0 && atMs <= expiryMs + 1e-6;
}

double AutomationEngine::poseAuthorityExpiryMs() const noexcept
{
    if (!pose_.lastLandmarkFreshAccept || pose_.lastLandmarkArrivalMs < 0.0
        || !poseArmTokenMatches(shot_.armToken, pose_.acceptedArmToken)) {
        return -1.0;
    }
    // Reuse the existing push->release validation budget, but bound a corrupted
    // dev setting so pose evidence can never become an unbounded release lease.
    const double leaseMs = std::clamp(config_.noMeterPushReleaseWindowMs, 50.0, 500.0);
    return pose_.lastLandmarkArrivalMs + leaseMs;
}

bool AutomationEngine::poseReleaseAuthorityCurrent(double atMs) const noexcept
{
    const double expiryMs = poseAuthorityExpiryMs();
    return config_.noMeterEnabled && expiryMs >= 0.0 && atMs <= expiryMs + 1e-6;
}

bool AutomationEngine::autonomousLiveMeterTimingEnabled() const noexcept
{
    return config_.autonomousVision && !config_.noMeterEnabled;
}

bool AutomationEngine::measuredLeadAuthoritative(double atMs) const noexcept
{
    constexpr double kConvergedPosteriorSdMs = 3.3;
    constexpr double kProvisionalPosteriorSdMs = 6.0;
    const bool clockCompatibilityReady = autonomousLiveMeterTimingEnabled()
        || config_.leadRebaselined;
    const bool converged = measuredLatencyN_ >= 6
        && measuredLatencySdMs_ <= kConvergedPosteriorSdMs;
    const bool controlledWarmStart = measuredLatencyProvisional_
        && measuredLatencyControlledAnchor_
        && measuredLatencyN_ >= 2
        && measuredLatencySdMs_ <= kProvisionalPosteriorSdMs;
    const auto expectedAttestation = controllerDeliveryRouteAttestationSnapshot();
    const quint64 expectedAttestationGeneration = expectedAttestation.generation;
    const LatencyControllerRoute expectedAttestationRoute = expectedAttestation.route;
    const bool exactControllerRouteEcho = measuredLatencyScopeEpoch_ != 0
        && expectedAttestationGeneration != 0
        && measuredLatencyAttestationGeneration_ == expectedAttestationGeneration
        && expectedAttestationRoute != LatencyControllerRoute::None
        && measuredLatencyDeliveryRoute_ == expectedAttestationRoute;
    // [ORION_USER_LEAD_AUTHORITY] Route echo for a USER-TYPED lead, which is a different
    // kind of quantity from a measurement and needs a different proof.
    //
    // THE DEADLOCK THIS FIXES (observed live 2026-08-06, logs/orion_native.log): on a
    // Remote Play reconnect the sidecar emits
    // `decoder_or_capture_source_generation_transition` -> "Latency authority reset;
    // fresh route evidence required", which zeroes the echoed attestation generation.
    // exactControllerRouteEcho then fails, the engine refuses to fire, and re-earning
    // authority requires bot releases the gate is itself blocking. Measured consequence
    // after the reconnect: 0 armed tokens, 0 shot outcomes, and the bot gated on
    // waiting_for_latency_calibration for ~3 minutes until the app was restarted. The
    // user-lead path was supposed to be the escape hatch but required the SAME echo, so
    // it could not rescue it.
    //
    // What is dropped: ONLY the generation equality. That clause encodes provenance —
    // "this measurement was taken on this exact route generation" — which is the right
    // question for an estimator output and a meaningless one for a constant the user
    // typed. A number typed by a human has no route to go stale with.
    //
    // What is KEPT, so this is not a blanket waiver: a live scope epoch, a live route
    // that is not None, and telemetry arriving on THAT SAME route. Channel liveness is
    // still proven; only the historical-provenance clause is waived. Every other
    // user-lead clause (bounds, user-set, freshness, telemetry-present) is untouched at
    // the call sites.
    const bool userLeadRouteEcho = measuredLatencyScopeEpoch_ != 0
        && expectedAttestationRoute != LatencyControllerRoute::None
        && measuredLatencyDeliveryRoute_ == expectedAttestationRoute;
    const bool routeProofInPlay = expectedAttestationGeneration != 0
        || measuredLatencyAttestationGeneration_ != 0
        || measuredLatencyDeliveryRoute_ != LatencyControllerRoute::None;
    const bool learnedControllerRouteReady = !routeProofInPlay
        || exactControllerRouteEcho;
    const bool restoredRouteReady = !measuredLatencyRestored_
        || (measuredLatencyVideoRouteAttested_ && exactControllerRouteEcho);
    const bool validatedAuthorityStructured =
        measuredLatencyAuthorityKind_ == QLatin1String("validated")
        && std::isfinite(measuredLatencyAuthorityMs_)
        && measuredLatencyAuthorityMs_ > 0.0
        && measuredLatencyAuthorityMs_ <= 500.0
        && std::isfinite(measuredLatencyAuthoritySdMs_)
        && measuredLatencyAuthoritySdMs_ > 0.0
        && std::abs(measuredLatencyAuthorityMs_ - measuredLatencyMs_) <= 0.05
        && std::abs(measuredLatencyAuthoritySdMs_ - measuredLatencySdMs_) <= 0.05;
    const bool factoryPriorStructured =
        measuredLatencyAuthorityKind_ == QLatin1String("factory")
        && measuredLatencyFactoryPrior_ && !measuredLatencyRestored_
        && measuredLatencyN_ < 6
        && std::isfinite(measuredLatencyAuthorityMs_)
        && measuredLatencyAuthorityMs_ > 0.0
        && measuredLatencyAuthorityMs_ <= 500.0
        && std::isfinite(measuredLatencyAuthoritySdMs_)
        && measuredLatencyAuthoritySdMs_ >= 6.0
        && measuredLatencyAuthoritySdMs_ <= 100.0
        // [ORION_PROBE_CACHE_AUTHORITY] (default OFF) mirror of the ingestion-side clause;
        // both sites must accept or the tuple is refused before this gate ever sees it.
        && (factoryLatencyPriorMatchesRoute(
                measuredLatencyPriorSource_, measuredLatencyDeliveryRoute_,
                latencyVideoRoute_)
            || (config_.probeCachePriorAuthority
                && measuredLatencyPriorSource_
                    == QLatin1String("probe_cache:self_measured")))
        && !measuredLatencyModelVersion_.isEmpty();
    const bool factoryRouteReady = factoryPriorStructured
        && expectedAttestationGeneration != 0
        && measuredLatencyAttestationGeneration_ == expectedAttestationGeneration
        && expectedAttestationRoute != LatencyControllerRoute::None
        && measuredLatencyDeliveryRoute_ == expectedAttestationRoute;
    const bool learnedRouteReady = validatedAuthorityStructured
        && (converged || controlledWarmStart)
        && restoredRouteReady && learnedControllerRouteReady;
    // The estimator-posterior authority path, exactly as before: with user_lead_satisfies_authority OFF
    // (the default) the whole function is bit-identical to the previous single-path gate.
    const bool estimatorAuthorityStructured =
        std::isfinite(measuredLatencyAuthorityMs_)
        && measuredLatencyAuthorityMs_ > 0.0
        && measuredLatencyAuthorityMs_ <= 500.0
        && std::isfinite(measuredLatencyAuthoritySdMs_)
        && measuredLatencyAuthoritySdMs_ > 0.0;
    const bool estimatorRouteReady = (learnedRouteReady || factoryRouteReady)
        && estimatorAuthorityStructured && measuredLeadEpochReady_;
    // [ORION_USER_LEAD_AUTHORITY] The user-lead alternative (default OFF; see RemapConfig for
    // the full fail-closed argument). It waives ONLY the posterior-shape clauses — including
    // measuredLeadEpochReady_, which is itself derived from the authority-accept branch of
    // telemetry ingestion and therefore encodes the very posterior shape being waived. Its
    // route-epoch role is carried by the exact attestation echo below: the echoed generation is
    // zeroed on every scope transition (updateDetection) and route change
    // (setControllerDeliveryRouteAttestation), so telemetry from an old sidecar, route, or
    // scope can never satisfy this path either.
    const bool userLeadRouteReady = config_.userLeadSatisfiesAuthority
        && !config_.leadOverrideFromEnv
        && config_.userActuationLeadSet
        && std::isfinite(config_.userActuationLeadMs)
        && config_.userActuationLeadMs >= config_.actuationLeadMinMs
        && config_.userActuationLeadMs <= config_.actuationLeadMaxMs
        && userLeadRouteEcho;
    if (!config_.measuredLeadEnabled || !clockCompatibilityReady
        || !measuredLeadTelemetryPresent_
        || (!estimatorRouteReady && !userLeadRouteReady)
        || measuredLeadLastUpdateMs_ < 0.0
        || !std::isfinite(config_.measuredLeadFreshnessMs)
        || config_.measuredLeadFreshnessMs <= 0.0) {
        return false;
    }
    const double ageMs = atMs - measuredLeadLastUpdateMs_;
    return std::isfinite(ageMs) && ageMs >= -1e-6
        && ageMs <= config_.measuredLeadFreshnessMs + 1e-6;
}

bool AutomationEngine::userLeadAuthorityActive(double atMs) const noexcept
{
    // [ORION_USER_LEAD_AUTHORITY] The user-lead path in isolation. measuredLeadForActuationMs()
    // and effectiveLeadSdMs() consume this so the VALUE can never outrun the GATE: every clause
    // here is a clause measuredLeadAuthoritative() also requires on this path.
    if (!config_.userLeadSatisfiesAuthority || config_.leadOverrideFromEnv
        || !config_.userActuationLeadSet
        || !std::isfinite(config_.userActuationLeadMs)
        || config_.userActuationLeadMs < config_.actuationLeadMinMs
        || config_.userActuationLeadMs > config_.actuationLeadMaxMs
        || !config_.measuredLeadEnabled
        || !measuredLeadTelemetryPresent_
        || measuredLeadLastUpdateMs_ < 0.0
        || !std::isfinite(config_.measuredLeadFreshnessMs)
        || config_.measuredLeadFreshnessMs <= 0.0) {
        return false;
    }
    const bool clockCompatibilityReady = autonomousLiveMeterTimingEnabled()
        || config_.leadRebaselined;
    if (!clockCompatibilityReady) {
        return false;
    }
    const auto expectedAttestation = controllerDeliveryRouteAttestationSnapshot();
    // [ORION_USER_LEAD_AUTHORITY] MUST stay bit-identical to measuredLeadAuthoritative()'s
    // userLeadRouteEcho — the contract of this function is that the VALUE can never outrun
    // the GATE, so a clause relaxed there and not here would let the lead be consumed on a
    // shot the gate would have refused (or vice versa). See the deadlock note at that
    // definition for why the generation-equality clause is dropped for a user-typed lead.
    const bool userLeadRouteEcho = measuredLatencyScopeEpoch_ != 0
        && expectedAttestation.route != LatencyControllerRoute::None
        && measuredLatencyDeliveryRoute_ == expectedAttestation.route;
    if (!userLeadRouteEcho) {
        return false;
    }
    const double ageMs = atMs - measuredLeadLastUpdateMs_;
    return std::isfinite(ageMs) && ageMs >= -1e-6
        && ageMs <= config_.measuredLeadFreshnessMs + 1e-6;
}

double AutomationEngine::effectiveLeadSdMs() const noexcept
{
    // Bit-identical to reading measuredLatencyAuthoritySdMs_ whenever a posterior authority
    // exists. The 6.0ms stand-in is the factory-envelope FLOOR (factoryPriorStructured's own
    // lower bound), i.e. the same lead sd the proven cold-start factory sessions scheduled
    // with — never a tighter claim than any real authority would have made.
    if (std::isfinite(measuredLatencyAuthoritySdMs_)
        && measuredLatencyAuthoritySdMs_ > 0.0) {
        return measuredLatencyAuthoritySdMs_;
    }
    constexpr double kUserLeadAuthoritySdMs = 6.0;
    if (userLeadAuthorityActive(nowMs())) {
        return kUserLeadAuthoritySdMs;
    }
    return measuredLatencyAuthoritySdMs_;
}

void AutomationEngine::setControllerDeliveryRouteAttestation(
    quint64 generation, LatencyControllerRoute route)
{
    if (generation == 0 || route == LatencyControllerRoute::None) {
        generation = 0;
        route = LatencyControllerRoute::None;
    }
    const auto prior = controllerDeliveryRouteAttestationSnapshot();
    const bool routeChanged = prior.generation != generation || prior.route != route;
    if (routeChanged && schedFireDeadlineMs_ >= 0.0) {
        // Fence while the old proof is still published. A worker submit that
        // already won on that exact route is therefore consumed once; otherwise
        // it is revoked before the new route becomes visible.
        invalidateUnconfirmedSchedule(false, "route_reattestation");
    }
    publishControllerDeliveryRouteAttestation(generation, route);
    if (routeChanged) {
        // Even a newly proved local route remains non-authoritative until a sidecar frame echoes
        // this exact generation after its own exact-scope re-key. This rejects in-flight telemetry
        // from an old backend, console, capture mode, or source generation.
        measuredLeadEpochReady_ = false;
    }
}

bool AutomationEngine::controlledCalibrationAnchorAvailable(double atMs) const noexcept
{
    constexpr double kEstimatorMaximumLabelSdMs = 20.0;
    // An explicit, non-provisional controlled N=1 snapshot is the new epoch's L1 label.
    // It may authorize only the separately bounded L2 validation shot. Requiring N to advance
    // beyond that same snapshot before L2 created a deterministic loop after an L2 reset:
    // native emitted another target-less L1 while the sidecar (correctly) waited for L2.
    // measuredLeadAuthoritative() retains its independent N>=2/N>=6 progress fence, so this
    // exception cannot authorize a production tip release.
    const bool freshControlledL1 = measuredLeadRequireNProgress_
        && measuredLatencyN_ == 1
        && measuredLatencyControlledAnchor_
        && !measuredLatencyProvisional_;
    const bool freshEpochProgressed = !measuredLeadRequireNProgress_
        || measuredLatencyN_ > measuredLeadRestartFloorN_
        || freshControlledL1;
    if (!autonomousLiveMeterTimingEnabled() || !config_.measuredLeadEnabled
        || !measuredLeadTelemetryPresent_ || !measuredLatencyControlledAnchor_
        || measuredLatencyN_ < 1 || !freshEpochProgressed
        || !std::isfinite(measuredLatencyMs_) || measuredLatencyMs_ <= 0.0
        || measuredLatencyMs_ > 500.0
        || !std::isfinite(measuredLatencySdMs_) || measuredLatencySdMs_ <= 0.0
        || measuredLatencySdMs_ > kEstimatorMaximumLabelSdMs
        || measuredLeadLastUpdateMs_ < 0.0
        || !std::isfinite(config_.measuredLeadFreshnessMs)
        || config_.measuredLeadFreshnessMs <= 0.0) {
        return false;
    }
    const double ageMs = atMs - measuredLeadLastUpdateMs_;
    return std::isfinite(ageMs) && ageMs >= -1e-6
        && ageMs <= config_.measuredLeadFreshnessMs + 1e-6;
}

double AutomationEngine::appliedMeterDelayLeadOffsetMs() const noexcept
{
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] See RemapConfig::meterDelayLeadOffsetMs for the
    // measurement and the refutation of the "lead += appliedDelay" formula. THE ONE DEFINITION
    // of "the delay-condition offset is in force":
    //   * a delay is actually applied (0 -> byte-identical to every pre-keying build);
    //   * no env lead sweep is running (ORION_LEAD_FLOOR_MS/BIAS_MS must stay byte-identical to
    //     their design, exactly as they out-rank the user lead in measuredLeadForActuationMs);
    //   * the value is finite and inside the band AppConfig already clamped it to (a
    //     hand-edited settings.json reaching the engine out of band is ignored, not clamped —
    //     an ignored offset degrades to today's behaviour, a clamped one invents a lead).
    // Deliberately NOT gated on meterDelaySettled_: the ramp reaches target within ~100-200 ms
    // of the physical shot edge and the meter does not render until ~300 ms
    // (MeterDelayController.h), so every tick that can schedule a release is already settled;
    // gating on it would only drop the offset on the ramp-down of a shot that has already fired.
    if (!(meterDelayAppliedMs_ > 0.0) || config_.leadOverrideFromEnv) {
        return 0.0;
    }
    const double offset = config_.meterDelayLeadOffsetMs;
    if (!std::isfinite(offset) || offset < AppConfigData::kMeterDelayLeadOffsetMinMs
        || offset > AppConfigData::kMeterDelayLeadOffsetMaxMs) {
        return 0.0;
    }
    return offset;
}

double AutomationEngine::maxMeterDelayLeadOffsetMs() const noexcept
{
    // The headroom the operator actually has: the visible-evidence ceiling minus the delay-0
    // lead this install is calibrated to. Reported, never enforced — the codebase's standing
    // doctrine for the lead pair is "an abort is fail-closed while a silently re-timed release
    // is a guaranteed mistime", so an offset past this ceiling still aborts loudly through the
    // existing unschedulable_lead path rather than being quietly trimmed.
    const double baseLeadMs = measuredLeadForActuationMs() - appliedMeterDelayLeadOffsetMs();
    if (!std::isfinite(baseLeadMs) || baseLeadMs <= 0.0) {
        return 0.0;
    }
    return maxSchedulableTipLeadMs() - baseLeadMs;
}

double AutomationEngine::measuredLeadForActuationMs() const noexcept
{
    // This is deliberately NOT measuredLatencyMs_: ordinary observations may
    // refine that shadow posterior while a packaged factory authority remains
    // fixed. Both scheduling paths consume this one explicit tuple.
    //
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] Every non-zero return path below adds
    // appliedMeterDelayLeadOffsetMs(), which is 0 unless a meter delay is actually applied. That
    // makes the delay condition part of the lead END TO END: the reservation, the arm sites, the
    // deadline decision, visibleEvidenceLeadStarvedByMeterDelay() and both SHOT LEAD CONFLICT
    // surfaces all read this one accessor, so an offset that cannot be scheduled aborts through
    // the pre-existing fail-closed path instead of quietly mistiming. The zero-authority return
    // is deliberately NOT offset: an offset must re-scale an existing lead, never manufacture
    // one (the same fence the user lead itself sits behind).
    const double delayOffsetMs = appliedMeterDelayLeadOffsetMs();
    double lead = std::max(0.0, measuredLatencyAuthorityMs_);
    if (lead <= 0.0) {
        // [ORION_USER_LEAD_AUTHORITY] (default OFF) With no posterior authority, the owner's
        // own in-band Shot Lead may stand in — but ONLY while the full user-lead readiness
        // contract holds RIGHT NOW (flag armed, lead explicitly set and in band, no env sweep,
        // telemetry present and fresh, exact controller-route attestation echo). This is not an
        // invented lead: it is the identical value the branch below already returns on every
        // driven shot whenever any authority exists, re-checked against the same channel
        // proofs the gate demands, so an unguarded caller gets the gate's guarantees anyway.
        if (userLeadAuthorityActive(nowMs())) {
            return std::max(0.0, config_.userActuationLeadMs + delayOffsetMs);
        }
        return lead;   // no authority -> unschedulable; never invent a lead from a correction
    }
    // [ORION_USER_LEAD] The shipped path: the user's own banner-tuned Shot Lead IS the actuation
    // lead. It is applied strictly INSIDE the "authority exists" branch above, so it re-scales an
    // already-valid lead and can never manufacture one — the fail-closed contract is untouched.
    //
    // It REPLACES the authority rather than offsetting it: the user reads the game's own per-shot
    // TIMING banner, which reports the total end-to-end result, so the number they converge on is
    // already the whole lead. Adding it to a posterior that drifts down over a session would make
    // their setting mean something different every hour, which is exactly the failure this
    // control exists to remove.
    //
    // The env dev override wins outright (not max()-merged): ORION_LEAD_FLOOR_MS/BIAS_MS exist to
    // sweep a lead live, and a setting that replaces the lead would otherwise silently out-rank
    // the value being swept. With either env var present this function is byte-identical to before.
    if (!config_.leadOverrideFromEnv
        && std::isfinite(config_.userActuationLeadMs)
        && config_.userActuationLeadMs >= config_.actuationLeadMinMs
        && config_.userActuationLeadMs <= config_.actuationLeadMaxMs) {
        return std::max(0.0, config_.userActuationLeadMs + delayOffsetMs);
    }
    // Correct the label pipeline's structural short bias (see RemapConfig::autonomousLeadBiasMs).
    // Applied to factory AND learned/validated authority alike: the factory prior is built by
    // tools/timing/build_latency_factory_prior.py from these same accepted labels, so it carries
    // the identical bias. This is what lets the lead travel between installs -- each estimator
    // learns its own latency and the same game-derived correction lands it on its own true lead.
    if (std::isfinite(config_.autonomousLeadBiasMs) && config_.autonomousLeadBiasMs > 0.0) {
        lead += config_.autonomousLeadBiasMs;
    }
    // Legacy machine-specific pin, retained as an opt-in safety bound (default OFF). Superseded by
    // the portable bias above; a non-zero value here re-introduces a per-machine constant.
    if (std::isfinite(config_.autonomousLeadFloorMs)
        && config_.autonomousLeadFloorMs > 0.0) {
        lead = std::max(lead, config_.autonomousLeadFloorMs);
    }
    // [ORION_METER_DELAY_LEAD_KEYING] The authority path gets the same delay keying: the
    // posterior and the factory prior are both transport measurements taken in ONE delay
    // condition, so applying either blind in the other is the exact hazard MeterDelayController.h
    // names. 0 offset (the default, and always at delay 0) leaves this line inert.
    return std::max(0.0, lead + delayOffsetMs);
}

double AutomationEngine::autonomousGreenCenterOffsetMs(double slopePctPerMs)
{
    // [ORION_GREEN_CENTER] See RemapConfig::autonomousGreenCenterFrac for the measurement that
    // motivates this (130/130 releases aimed at position 1.000 of the green window).
    //
    // Fail-closed in every branch: any doubt about the window or the slope returns 0.0, which is
    // byte-identical to the previous edge-aimed behaviour. This function can only move the fire
    // EARLIER, and never by more than autonomousGreenCenterMaxMs.
    const double frac = config_.autonomousGreenCenterFrac;
    if (!std::isfinite(frac) || frac <= 0.0) {
        return 0.0;
    }
    // A near-zero or negative slope makes ms = pp / slope explode or flip sign. Use the same
    // positive-rise requirement the rest of the tip path pivots on rather than a bare != 0.
    if (!std::isfinite(slopePctPerMs) || slopePctPerMs <= kFlatMeterSlopePctPerMs) {
        return 0.0;
    }
    // THIS shot's own tracked window, not a session average: width varies 2.0pp (p10) to 10.3pp
    // (p90), and a shift sized for a wide window pushes a narrow one out the early side.
    if (!greenTracker_.confirmed()) {
        return 0.0;
    }
    const double greenStart = greenTracker_.startPct();
    const double greenWidth = greenTracker_.widthPct();
    if (!std::isfinite(greenStart) || !std::isfinite(greenWidth)
        || greenStart <= 0.0 || greenWidth <= 0.0) {
        return 0.0;
    }
    // [ORION_GREEN_CENTER] REJECT THE CLAMP FLOOR. simple_meter_reader._green_window() computes
    // band = min(20.0, max(2.0, _band_raw)), so an emitted width of exactly 2.00pp is the FLOOR
    // BINDING on a sub-2pp measurement, not a measurement. The reader's own comment gives the
    // proof: every genuine width is a whole-pixel multiple of 1/fillable_h (2.78 = 3px, 5.56 = 6px
    // on a 108px track), and 2.00pp is 2.16px -- not a whole pixel, so it cannot be real.
    //
    // MEASURED on this rig: the raw per-frame width is floor-bound on 89/130 releases (68.5%), and
    // the tracked width this function reads is floor-bound on 32/130 (24.6%). Centring off a clamp
    // means the shift is driven by the rail, not by the shot. It under-shifts rather than
    // over-shifts, so it is not dangerous -- but it is not evidence either, and a quarter of shots
    // silently getting a rail-derived aim is exactly the kind of thing that makes a graded batch
    // uninterpretable.
    //
    // Fail closed: no trustworthy width -> no shift -> previous edge-aim behaviour. Widening this
    // to use the reader's own _green_band_bound flag (already computed, currently diagnostic-only)
    // is the better long-term fix and needs the flag plumbed through the detection payload.
    constexpr double kGreenBandFloorPct = 2.0;
    if (greenWidth <= kGreenBandFloorPct + 1e-6) {
        return 0.0;
    }
    // Aim `frac` of the way from the late edge toward the early edge. frac = 1.0 lands on the
    // exact centre of the window; the caller sweeps it rather than sweeping a raw millisecond
    // count, so the shift stays proportional to whatever window this shot actually has.
    const double offsetPct = 0.5 * greenWidth * frac;
    double offsetMs = offsetPct / slopePctPerMs;
    if (!std::isfinite(offsetMs) || offsetMs <= 0.0) {
        return 0.0;
    }
    const double capMs = (std::isfinite(config_.autonomousGreenCenterMaxMs)
                          && config_.autonomousGreenCenterMaxMs > 0.0)
        ? config_.autonomousGreenCenterMaxMs : 0.0;
    if (capMs <= 0.0) {
        return 0.0;
    }
    offsetMs = std::min(offsetMs, capMs);
    // Report once per stable window rather than per tick, so a batch log carries the offset that
    // was actually in force without the scheduler's cadence drowning it.
    if (!std::isfinite(greenCenterLoggedOffsetMs_)
        || std::abs(offsetMs - greenCenterLoggedOffsetMs_) >= 2.0) {
        greenCenterLoggedOffsetMs_ = offsetMs;
        emit engineDiagnostic(QStringLiteral(
            "GREEN CENTER AIM: offset_ms=%1 frac=%2 green_start_pct=%3 green_width_pct=%4 "
            "slope_pct_ms=%5 cap_ms=%6 aim_pct=%7")
                                  .arg(offsetMs, 0, 'f', 2)
                                  .arg(frac, 0, 'f', 3)
                                  .arg(greenStart, 0, 'f', 2)
                                  .arg(greenWidth, 0, 'f', 2)
                                  .arg(slopePctPerMs, 0, 'f', 4)
                                  .arg(capMs, 0, 'f', 1)
                                  .arg(greenStart + greenWidth - offsetPct, 0, 'f', 2));
    }
    return offsetMs;
}

AutomationEngine::TipReservationView
AutomationEngine::tipReservationView() const noexcept
{
    TipReservationView view;
    if (!tipReservation_.active) {
        return view;
    }
    view.active = true;
    view.updates = tipReservation_.updates;
    if (tipReservation_.promoted) {
        view.state = QStringLiteral("promoted");
    } else if (std::isfinite(tipReservation_.fireAtMs)
               && tipReservation_.fireAtMs <= nowMs()) {
        view.state = QStringLiteral("reserved_late");
    } else {
        view.state = QStringLiteral("reserved");
    }
    return view;
}

void AutomationEngine::setLatencyVideoRoute(const QString& videoRoute)
{
    const QString next = videoRoute.trimmed();
    if (next == latencyVideoRoute_) {
        return;
    }
    latencyVideoRoute_ = next;
    // A video-route transition invalidates any lead authority derived under the previous tier:
    // capture-card and decoder differ by an entire encode/decode stage, so the packaged prior for
    // one is not evidence for the other. Drop actuation readiness and let the next snapshot
    // re-establish it against the new tier.
    measuredLeadEpochReady_ = false;
    invalidateUnconfirmedVisionSchedule("route_change");
}

bool AutomationEngine::registrationSoloContradictedByFlatMeter(
    const TemporalSampler::CrossingFit& fit) noexcept
{
    // Blocks on POSITIVE EVIDENCE OF FLATNESS, never on absence of evidence.
    //
    // The distinction matters and the first version of this guard got it wrong: requiring the
    // sampler to PROVE a rise silently disabled registration exactly where it is supposed to carry
    // the shot -- the early/low-fill horizon, where the sampler legitimately has too few samples to
    // say anything. The real failure being targeted is narrower: registration extrapolating a meter
    // the sampler can see has STOPPED. So the sampler only gets a veto when it actually has
    // something to say.
    //
    // Threshold sits far below the observed live rise (0.174-0.188 %/ms across every shot type in
    // the 2026-08-03 batch), so a genuine meter never trips it and a hold always does.
    return fit.n >= kMinSamplesToJudgeMeterMotion
        && std::isfinite(fit.slopePctPerMs)
        && fit.slopePctPerMs <= kFlatMeterSlopePctPerMs;
}

bool AutomationEngine::contestedTipDeadlineOutrunByMeasuredRunway(
    const QString& source, const TemporalSampler::CrossingFit& fit,
    double fillPct, double leadMs) noexcept
{
    // [ORION_CONTESTED_DEADLINE] Do not RELINQUISH a shot on an estimate this engine has already
    // classified as far-disagreeing, while the meter's own measured motion independently proves
    // the command is still submittable.
    //
    // This is NOT "fire anyway". Firing blind stays prohibited: the caller does not release, it
    // keeps waiting for a trustworthy estimate under the same bounded hold ceiling every other
    // unresolved-trajectory path uses. A genuinely missed deadline still aborts.
    //
    // LIVE CASE (2026-08-04 orion_native.log, physical_epoch=5, ButtonShot). Registration and the
    // sampler split hard: at fill 27.63 registration said the tip was 235 ms out while the sampler
    // said 443 ms. Three frames later the fused branch failed its own compatibility test, the
    // far-horizon rule handed the decision to registration (fill 47.39 < regFusionNearFillPct 55),
    // and registration's 73 ms tip against a 290 ms lead produced command_eta -216.9 ms ->
    // live_tip_deadline_missed on an estimate carrying a 66 ms sigma. The meter itself was at
    // 47.39% climbing ~0.146 pp/ms, i.e. ~360 ms of runway against a 290 ms lead. Nothing was
    // missed; the engine destroyed a live shot on the reading it trusted least.
    //
    // INDEPENDENCE IS THE WHOLE ARGUMENT, so it is enforced structurally:
    //   * the DISPUTED number, shot_.regTipAbsMs, comes from the registration model
    //     (AutomationEngine.cpp:2293, capture-aligned + result.regTipMs).
    //   * the PROOF, fit.slopePctPerMs and fillPct, is a weighted least-squares fit over
    //     DetectionResult fills only (sampler_.addSample(result.fillPct, ...) is the sole feed).
    // They share a clock, never an estimate. Nothing registration says can move this proof.
    //
    // AND IT CANNOT HELP REGISTRATION IN THE FLAT REGIME -- the regime the 2026-08-03 evidence
    // (46 of 66 catastrophic prediction errors were registration-source over a flat-holding fill,
    // median claimed sigma 151 ms vs median actual error 249 ms) says registration must be muted
    // in. The rise floor here is exactly 2x the flat ceiling of the guard above, both spelled from
    // one constant, so the two predicates are disjoint by construction: any fit that satisfies
    // this one is strictly outside the band that one vetoes.
    if (source != QLatin1String("registration_far_disagreement")) {
        // The only source whose deadline is BOTH driven by the distrusted estimate AND accompanied
        // by an authoritative independent sampler (canonicalAutonomousTipDecision only emits this
        // label when samplerAuthoritative && regTrajectory && !statisticallyCompatible). The
        // mirror label `sampler_near_disagreement` is deliberately excluded: there the deadline
        // IS the sampler, so proving runway from the sampler would be circular.
        return false;
    }
    // Same width bar the flat guard uses. Two noisy points are not a measurement of motion.
    if (fit.n < kMinSamplesToJudgeMeterMotion || !std::isfinite(fit.slopePctPerMs)
        || fit.slopePctPerMs < kProvenRiseSlopePctPerMs
        // Upper bound mirrors liveMeterCrossingAuthoritative()'s physical plausibility ceiling:
        // a near-vertical "slope" is a detector artefact, and an artefact must never be able to
        // manufacture runway by dividing by a huge number.
        || fit.slopePctPerMs > 2.0) {
        return false;
    }
    constexpr double kTipTargetPct = 100.0;
    if (!std::isfinite(fillPct) || fillPct <= 0.0 || fillPct >= kTipTargetPct
        || !std::isfinite(leadMs) || leadMs <= 0.0) {
        return false;
    }
    // The fail-closed half. This is the meter's own answer to "can the command still be
    // submitted": remaining travel divided by measured speed. The moment that stops exceeding the
    // lead, the deadline is genuinely gone and the caller aborts exactly as it always did.
    const double runwayMs = (kTipTargetPct - fillPct) / fit.slopePctPerMs;
    return std::isfinite(runwayMs) && runwayMs > leadMs;
}

bool AutomationEngine::autonomousTipReservationIdentityCurrent() const noexcept
{
    if (!tipReservation_.active) {
        return false;
    }
    const auto attestation = controllerDeliveryRouteAttestationSnapshot();
    return tipReservation_.physicalShotEpoch == shot_.physicalShotEpoch
        && tipReservation_.armToken == shot_.armToken
        && tipReservation_.visionEpoch == shot_.visionEpoch
        && tipReservation_.routeGeneration == attestation.generation
        && tipReservation_.route == attestation.route;
}

void AutomationEngine::cancelAutonomousTipReservation(const QString& reason)
{
    if (!tipReservation_.active) {
        return;
    }
    const AutonomousTipReservation prior = tipReservation_;
    tipReservation_ = AutonomousTipReservation{};
    emit engineDiagnostic(QStringLiteral(
        "TIP RESERVATION: disposition=reservation_canceled reason=%1 "
        "physical_epoch=%2 shot_attempt=%3 updates=%4 promoted=%5 ever_valid=%6 "
        "fire_at_ms=%7")
                              .arg(reason.left(48))
                              .arg(prior.physicalShotEpoch)
                              .arg(prior.armToken)
                              .arg(prior.updates)
                              .arg(prior.promoted ? 1 : 0)
                              .arg(prior.everValid ? 1 : 0)
                              .arg(prior.fireAtMs, 0, 'f', 3));
}

void AutomationEngine::updateAutonomousTipReservation(
    const AutonomousTipDecision& decision, double now)
{
    // A reservation plans against the SAME lead the submission path will spend, so the two can
    // never disagree about when the command is due.
    const double leadMs = measuredLeadForActuationMs();
    const bool estimatePresent = std::isfinite(decision.tipAbsMs)
        && decision.tipAbsMs > now
        && !decision.source.isEmpty()
        && decision.source != QLatin1String("none");
    if (!estimatePresent || !std::isfinite(leadMs) || leadMs <= 0.0) {
        // No usable forward estimate on this frame. A momentary gap is normal while the fit
        // re-forms, so an existing reservation is held; the identity and lease checks below (and
        // the caller's own abort paths) are what actually retire it.
        return;
    }

    if (tipReservation_.active && !autonomousTipReservationIdentityCurrent()) {
        cancelAutonomousTipReservation(QStringLiteral("identity_changed"));
    }

    const double fireAtMs = decision.tipAbsMs - leadMs;
    const bool created = !tipReservation_.active;
    if (created) {
        const auto attestation = controllerDeliveryRouteAttestationSnapshot();
        tipReservation_ = AutonomousTipReservation{};
        tipReservation_.active = true;
        tipReservation_.createdMs = now;
        tipReservation_.firstTipAbsMs = decision.tipAbsMs;
        tipReservation_.firstFillPct = shot_.fillPct;
        tipReservation_.physicalShotEpoch = shot_.physicalShotEpoch;
        tipReservation_.armToken = shot_.armToken;
        tipReservation_.visionEpoch = shot_.visionEpoch;
        tipReservation_.routeGeneration = attestation.generation;
        tipReservation_.route = attestation.route;
    }

    tipReservation_.tipAbsMs = decision.tipAbsMs;
    tipReservation_.fireAtMs = fireAtMs;
    tipReservation_.leadMs = leadMs;
    // [ORION_USER_LEAD_AUTHORITY] effective (not raw): under user-lead authority the raw sd is
    // 0.0 and would be reported as a 0-sd lead; the effective value is what scheduling used.
    tipReservation_.leadSdMs = effectiveLeadSdMs();
    tipReservation_.sigmaMs = decision.combinedSigmaMs;
    tipReservation_.source = decision.source;
    ++tipReservation_.updates;
    if (decision.valid && !tipReservation_.everValid) {
        tipReservation_.everValid = true;
        tipReservation_.firstValidMs = now;
    }
    if (fireAtMs > now) {
        tipReservation_.everFuture = true;
        if (decision.valid) {
            // A genuine opportunity: authoritative enough to submit AND still ahead of the
            // deadline. If a shot never reaches this state, no amount of authority bookkeeping
            // explains the miss -- the estimate simply never became usable in time.
            tipReservation_.everValidAndFuture = true;
            // ...but "valid and still ahead" is NOT the same as "we could have acted". A deadline
            // 300 ms out is valid and future on every tick of its life and can be armed on none of
            // them. Conflating the two is what made 106 of 115 live aborts report
            // authority_lost_before_submit -- a label that says "we had it and lost it" for shots
            // that were never once inside the arming window. everArmable is the honest test: valid,
            // still ahead, AND inside the window scheduleFire() will actually accept.
            const double untilCommandMs = fireAtMs - now;
            if (untilCommandMs <= adaptiveAutonomousSchedulerHorizonMs()) {
                tipReservation_.everArmable = true;
            }
        }
    }

    // Log creation always; afterwards only a materially moved deadline earns a line.
    constexpr double kReservationLogMoveMs = 8.0;
    const bool moved = !std::isfinite(tipReservation_.loggedFireAtMs)
        || tipReservation_.loggedFireAtMs < 0.0
        || std::abs(fireAtMs - tipReservation_.loggedFireAtMs) >= kReservationLogMoveMs;
    if (created || moved) {
        tipReservation_.loggedFireAtMs = fireAtMs;
        emit engineDiagnostic(QStringLiteral(
            "TIP RESERVATION: disposition=%1 source=%2 tip_eta_ms=%3 command_eta_ms=%4 "
            "lead_kind=%5 lead_ms=%6 lead_sd_ms=%7 predictor_sigma_ms=%8 valid=%9 "
            "fill_pct=%10 frame_age_ms=%11 physical_epoch=%12 shot_attempt=%13 updates=%14 "
            // [ORION_HORIZON_DEBIAS] APPEND-ONLY (key=value parsers are unaffected; existing
            // fields keep their positions). horizon_debias_ms = ms subtracted from the raw
            // sampler crossing this decision. It pivots on lead_ms (field 6), so on the
            // reservation that actually fires it must be ~0 — that is the check which proves
            // the already-learned lead was not silently invalidated. Larger values appear only
            // on early, long-horizon updates, which is exactly where the bias lives.
            "horizon_debias_ms=%15 "
            // [ORION_FUSION_TELEMETRY] APPEND-ONLY (key=value parsers unaffected; every field
            // above keeps its position). Both fusion members and the weight registration
            // carried. -1 = that member was absent from this decision.
            //   reg_weight = regVar^-1 / (regVar^-1 + samplerVar^-1)
            // and it MUST reproduce by hand from reg_sigma_ms and smp_sigma_ms on this same
            // line. If it does not, the fusion is not doing what this line claims.
            // The decision that FIRES sits at horizon ~= lead_ms (field 6); that is the line to
            // read, because the weighting moves with horizon and early updates are not the
            // shot. Nothing consumes these fields -- they are here so the weighting stops being
            // invisible before anyone tunes it.
            "reg_tip_eta_ms=%16 reg_sigma_ms=%17 smp_tip_eta_ms=%18 smp_sigma_ms=%19 "
            "reg_weight=%20 "
            // [ORION_TIP_PHASE] APPEND-ONLY (key=value parsers unaffected; every field above
            // keeps its position). The third member: an anchor-dated LOOKUP rather than an
            // extrapolation. -1 = absent from this decision (no observed anchor crossing).
            //   phase_primary = 1 when it WON and the deadline on this line is its own
            //   phase_weight  = phaseVar^-1 / (phaseVar^-1 + selectedVar^-1) -- the weight it
            //                   would have carried against whatever the pre-existing chain
            //                   picked, and it MUST reproduce by hand from phase_sigma_ms and
            //                   the sigma of that member on this same line
            //   phase_const_ms = learned physical term + the fixed aim-preservation offset;
            //                   watch it move as the learner fills its 10-landing window while
            //                   the AIM (tip_eta_ms - lead_ms) does not
            // [ORION_TIP_PHASE_LADDER] phase_anchor_pct is the level the shot was actually dated
            // at. Anything other than tipPhaseAnchorPct means the meter was first seen ABOVE the
            // base anchor and the ladder rescued it -- the No Dip case. phase_const_ms already
            // includes that level's adjustment, so the two read together.
            "phase_tip_eta_ms=%21 phase_sigma_ms=%22 phase_weight=%23 phase_const_ms=%24 "
            // [ORION_LEAD_CONFLICT] APPEND-ONLY. lead_source names where lead_ms (field 6)
            // actually came from (user|seed|authority|none); lead_kind (field 5) names only the
            // authority the in-band Shot Lead may have replaced.
            "phase_primary=%25 phase_anchor_pct=%26 lead_source=%27")
                                  .arg(created ? QStringLiteral("reservation_created")
                                               : QStringLiteral("reservation_updated"))
                                  .arg(decision.source.left(64))
                                  .arg(decision.tipAbsMs - now, 0, 'f', 3)
                                  .arg(fireAtMs - now, 0, 'f', 3)
                                  .arg(measuredLatencyAuthorityKind_.left(16))
                                  .arg(leadMs, 0, 'f', 3)
                                  .arg(effectiveLeadSdMs(), 0, 'f', 3)
                                  .arg(decision.combinedSigmaMs, 0, 'f', 3)
                                  .arg(decision.valid ? 1 : 0)
                                  .arg(shot_.fillPct, 0, 'f', 2)
                                  .arg(shot_.frameAgeMs, 0, 'f', 2)
                                  .arg(tipReservation_.physicalShotEpoch)
                                  .arg(tipReservation_.armToken)
                                  .arg(tipReservation_.updates)
                                  .arg(decision.horizonDebiasMs, 0, 'f', 2)
                                  .arg(decision.regTipAbsMs >= 0.0
                                           ? decision.regTipAbsMs - now : -1.0, 0, 'f', 3)
                                  .arg(decision.regSigmaMs, 0, 'f', 3)
                                  .arg(decision.samplerCrossingMs >= 0.0
                                           ? decision.samplerCrossingMs - now : -1.0, 0, 'f', 3)
                                  .arg(decision.samplerSigmaMs, 0, 'f', 3)
                                  .arg(decision.regWeight, 0, 'f', 4)
                                  .arg(decision.phaseTipAbsMs >= 0.0
                                           ? decision.phaseTipAbsMs - now : -1.0, 0, 'f', 3)
                                  .arg(decision.phaseSigmaMs, 0, 'f', 3)
                                  .arg(decision.phaseWeight, 0, 'f', 4)
                                  .arg(decision.phaseTipAbsMs >= 0.0
                                           ? effectiveTipPhaseConstantMs()
                                               + tipPhaseLevelAdjustmentMs(
                                                   shot_.fillPhaseAnchorLevelPct)
                                               + tipPhaseTypeTrimMs(shot_.shotType)
                                           : -1.0, 0, 'f', 3)
                                  .arg(decision.phasePrimary ? 1 : 0)
                                  .arg(shot_.fillPhaseAnchorLevelPct, 0, 'f', 1)
                                  .arg(actuationLeadSourceLabel(leadMs)));
    }
}

AutomationEngine::AutonomousTipDecision
AutomationEngine::canonicalAutonomousTipDecision(double atMs) const
{
    constexpr double kTipTargetPct = 100.0;
    AutonomousTipDecision decision;
    decision.samplerFit = sampler_.predictCrossing(kTipTargetPct);
    decision.velocityPctPerMs = decision.samplerFit.slopePctPerMs;
    // Authority is decided on the RAW fit, exactly as before this change: the de-bias below
    // corrects a crossing the engine already trusts, it never promotes one it did not.
    decision.samplerAuthoritative = liveMeterCrossingAuthoritative(
        decision.samplerFit, atMs);

    // [ORION_HORIZON_DEBIAS] Rotate the crossing about the engine's own median commit horizon.
    //
    // The weighted-linear crossing runs LATE by ~0.22ms per ms of horizon because the meter
    // eases IN — see the measurement table on AutomationEngine.h's samplerHorizonBiasMsPerMs.
    // A single learned lead cancels that at one horizon only; every other horizon keeps the
    // uncancelled remainder, which is why widening the commit-horizon range costs accuracy.
    //
    // Subtracting k*(h - h_ref) removes the horizon DEPENDENCE while leaving the MEAN aim
    // point where the currently-learned lead already expects it: at h == h_ref the correction
    // is exactly zero, and h_ref is the running median of the horizons this engine actually
    // commits at. So this shrinks shot-to-shot spread without asking the lead to re-converge.
    // Deliberately NOT a net-earlier or net-later change — moving the mean is a separate
    // decision that has to be made together with the lead.
    //
    // Fail-closed either way: pulling the crossing earlier can only make a deadline already
    // missed (rejected by the existing deadline path), never emit a command guaranteed late;
    // pushing it later only buys the scheduler more margin.
    //
    // THE PIVOT IS THE LEAD, and that choice is the whole safety argument. The command is
    // submitted at fireAt = tip - lead, so at the instant that actually matters the remaining
    // horizon IS the lead. Pivoting there makes the correction identically zero on the decision
    // that fires, which is what leaves the already-learned lead valid; every EARLIER estimate
    // (necessarily a longer horizon) gets pulled earlier, which stops the reservation's fireAt
    // from creeping later as the horizon closes.
    //
    // A first attempt pivoted on an EMA of the horizon over every authoritative frame. That is
    // wrong and the closed-loop suite caught it: a rise sweeps the horizon from ~500ms down to
    // ~0, so that mean sits far ABOVE the horizon at commit, the correction came out negative
    // at every commit, and twoStageLatencyCalibrationColdStartsGoToAndTempoStick landed 3.6pp
    // past the cap (~15ms late). Averaging the whole sweep smuggles in exactly the mean shift
    // this correction is supposed to avoid.
    //
    // SHIPPED OFF (samplerHorizonDebiasEnabled = false). The closed-loop suite proved that the
    // pivot above is still not the operative one: twoStageLatencyCalibration* fires from a
    // deadline SCHEDULED ~43ms before it executes (eta=42.9, crossing_eta=262.9, lead=220), so
    // the horizon that actually carries the bias is lead + scheduling-lookahead, not lead. With
    // the lead as pivot the correction came out ~9ms early at commit and both closed-loop cases
    // landed EARLY (residual -0.97pp and -3.43pp) instead of mean-neutral.
    //
    // That lookahead is exactly what the rolling authority lease just moved (promotion
    // command_eta p50 ~28ms -> 67.2ms), which is why it cannot be pinned from offline replay:
    // the replay has no scheduler. Calibrating it needs one live batch of the "Release landing:"
    // evidence added alongside this change. Until then the correction stays inert — enabling it
    // blind would add a mean EARLY shift to a system already firing early, which is the same
    // regression it is meant to cure.
    const bool leadPivotUsable = std::isfinite(measuredLatencyAuthorityMs_)
        && measuredLatencyAuthorityKind_ != QLatin1String("none")
        && measuredLatencyAuthorityMs_ >= 30.0
        && measuredLatencyAuthorityMs_ <= 600.0;
    if (config_.samplerHorizonDebiasEnabled
        && decision.samplerAuthoritative
        && leadPivotUsable
        && std::isfinite(config_.samplerHorizonBiasMsPerMs)
        && config_.samplerHorizonBiasMsPerMs != 0.0) {
        const double pivotMs = measuredLatencyAuthorityMs_;
        const double rawHorizonMs = decision.samplerFit.crossingMs - atMs;
        if (std::isfinite(rawHorizonMs)) {
            const double correctionMs = std::clamp(
                config_.samplerHorizonBiasMsPerMs * (rawHorizonMs - pivotMs),
                -config_.samplerHorizonDebiasMaxMs,
                config_.samplerHorizonDebiasMaxMs);
            const double correctedMs = decision.samplerFit.crossingMs - correctionMs;
            // Never let the correction manufacture a crossing in the past: a de-biased tip that
            // lands at/behind now carries no information the caller can act on, and the raw
            // crossing already passed the authority test.
            if (correctedMs > atMs) {
                decision.samplerFit.crossingMs = correctedMs;
                decision.horizonDebiasMs = correctionMs;
            }
        }
    }

    // Registration owns the far/low-fill horizon where a local polynomial is
    // weak; the capture-clock sampler owns the near-tip horizon. Compatible
    // estimates are fused by inverse variance. A disagreement is never averaged
    // blindly: the horizon chooses the independently stronger source.
    const bool regTrajectory = config_.regFusionEnabled
        && shot_.regTipAbsMs > atMs && shot_.regTipMs > 0.0
        && shot_.regTipMs <= 2000.0
        && shot_.regConf >= config_.fusedRegMinConf
        && shot_.regN >= config_.fusedSamplerMinN
        && std::isfinite(shot_.regSigmaMs) && shot_.regSigmaMs > 0.0
        && shot_.regSigmaMs <= 500.0
        && !shot_.regModelId.isEmpty() && !shot_.regModelVersion.isEmpty();

    double samplerSigmaMs = -1.0;
    if (decision.samplerAuthoritative) {
        const double horizonMs = std::max(
            0.0, decision.samplerFit.crossingMs - atMs);
        const double weightedRmsePct = decision.samplerFit.weightSum > 0.0
            ? std::sqrt(decision.samplerFit.wrss
                        / decision.samplerFit.weightSum)
            : 2.0;
        const double trackingQuality = std::clamp(
            (shot_.confidence - 0.90) / 0.10, 0.0, 1.0);
        const double residualInflation = std::clamp(
            weightedRmsePct / 0.8, 0.7, 3.0);
        // Horizon term recalibrated against MEASURED residuals (2026-08-03 batch, 288 predictions
        // paired to actual tip frames across 34 epochs). The old 6.0 + 0.20*horizon overstated the
        // true error by 2.6-3.5x and, worse, got the SHAPE wrong: the real residual is nearly
        // horizon-FLAT, not linear in horizon.
        //     horizon    modelled(old)   measured rMAD
        //     150-250ms      42.2ms          15.9ms
        //     250-350ms      50.9ms          16.7ms
        //     350-450ms      76.6ms          22.0ms
        // The consequence of the old curve was not conservatism, it was blindness: 61 reservations
        // were refused as invalid against the 65ms combined cap, and 28 of those were accurate to
        // within 50ms. A prediction is not made worse by being far away nearly as fast as this
        // formula assumed.
        samplerSigmaMs = (10.0 + 0.05 * horizonMs
                           + 40.0 * (1.0 - trackingQuality))
            * residualInflation
            * std::max(1.0, std::sqrt(
                6.0 / static_cast<double>(
                    std::max(1, decision.samplerFit.n))));
        if (!decision.samplerFit.usedQuad
            && shot_.fillPct > config_.decelKneePct) {
            samplerSigmaMs *= 2.0;
        }
    }

    // [ORION_FUSION_TELEMETRY] Snapshot both members BEFORE the branch below picks one, so a log
    // line records what the LOSER offered as well as the winner. Without this, a decision that
    // reads source=sampler is indistinguishable from one where registration was present and
    // outvoted -- which is precisely the distinction the template-fit work turns on.
    // Pure observation: nothing below reads these fields.
    if (decision.samplerAuthoritative) {
        decision.samplerCrossingMs = decision.samplerFit.crossingMs;
        decision.samplerSigmaMs = samplerSigmaMs;
    }
    if (regTrajectory) {
        decision.regTipAbsMs = shot_.regTipAbsMs;
        decision.regSigmaMs = shot_.regSigmaMs;
    }
    if (decision.samplerAuthoritative && regTrajectory) {
        const double samplerVar = samplerSigmaMs * samplerSigmaMs;
        const double regVar = shot_.regSigmaMs * shot_.regSigmaMs;
        // Telemetry only, and defensively guarded: the branch below already assumes both
        // variances are non-zero, but a diagnostic must never be the thing that divides by zero.
        if (samplerVar > 0.0 && regVar > 0.0) {
            const double regPrecision = 1.0 / regVar;
            decision.regWeight = regPrecision / (regPrecision + 1.0 / samplerVar);
        }
        const double disagreement = decision.samplerFit.crossingMs
            - shot_.regTipAbsMs;
        const bool statisticallyCompatible = disagreement * disagreement
            <= 9.0 * (samplerVar + regVar);
        const bool farHorizon = shot_.regTipMs >= config_.regFusionFarHorizonMs
            || shot_.fillPct < config_.regFusionNearFillPct;
        if (statisticallyCompatible) {
            const double fusedVar = 1.0 / (1.0 / samplerVar + 1.0 / regVar);
            decision.tipAbsMs = fusedVar * (
                decision.samplerFit.crossingMs / samplerVar
                + shot_.regTipAbsMs / regVar);
            decision.sigmaMs = std::sqrt(fusedVar);
            decision.source = farHorizon
                ? QStringLiteral("registration+sampler_far")
                : QStringLiteral("sampler+registration_near");
        } else if (farHorizon) {
            decision.tipAbsMs = shot_.regTipAbsMs;
            decision.sigmaMs = shot_.regSigmaMs;
            decision.source = QStringLiteral("registration_far_disagreement");
        } else {
            decision.tipAbsMs = decision.samplerFit.crossingMs;
            decision.sigmaMs = samplerSigmaMs;
            decision.source = QStringLiteral("sampler_near_disagreement");
        }
    } else if (regTrajectory
               && !registrationSoloContradictedByFlatMeter(decision.samplerFit)) {
        // REGISTRATION-ONLY, and only while the meter is demonstrably still rising.
        //
        // Registration owns the far/low-fill horizon, but it EXTRAPOLATES: handed a meter that has
        // stopped moving it happily projects a tip that will never arrive. In the 2026-08-03 batch
        // 46 of the 66 catastrophic (>100ms) prediction errors were registration-source, every one
        // of them produced while the fill was flat-holding, and registration claimed a median sigma
        // of 151ms against a median actual error of 249ms -- confidently wrong in the one regime
        // where nothing contradicts it.
        //
        // The fused branch above is unaffected: it already requires samplerAuthoritative, which
        // carries its own live-slope proof. This gate exists solely so a SOLO registration estimate
        // cannot speak for a meter the sampler cannot see moving.
        decision.tipAbsMs = shot_.regTipAbsMs;
        decision.sigmaMs = shot_.regSigmaMs;
        decision.source = QStringLiteral("registration");
    } else if (decision.samplerAuthoritative) {
        decision.tipAbsMs = decision.samplerFit.crossingMs;
        decision.sigmaMs = samplerSigmaMs;
        decision.source = QStringLiteral("sampler");
    }

    // === [ORION_TIP_PHASE] THE THIRD MEMBER, and the one that does not extrapolate ==========
    //
    // Everything above projects a curve FORWARD to a crossing it has not seen. That projection is
    // where the error lives: measured against 61 real landings the shipped chain's crossing at
    // submit missed the meter's actual stop by a median +92.6 ms with rMAD 18.4. This member
    // instead DATES an event that already happened -- the fill's first upward crossing of
    // tipPhaseAnchorPct -- and adds a constant. Same 61 landings: rMAD 9.6, sd 15.0, and flat in
    // horizon (rMAD 9.6-13.4 for anchors anywhere from 20% to 50%), which an extrapolator cannot
    // be. That is a ~1.9x variance reduction with, by construction, ZERO mean shift; see
    // RemapConfig::tipPhaseSeedPhysicalMs for why the constant is 379 and not the physical 318.
    //
    // PRIMARY, not fused. Inverse-variance fusion assumes independent members, and this one is
    // not independent of the sampler -- both read the same fill stream -- so averaging them would
    // understate the result's variance while dragging the tighter member toward the biased one.
    // The pre-existing chain stays as the fallback, unchanged, for every shot where the crossing
    // was never observed.
    //
    // FAIL-CLOSED, on all four gates below:
    //   * an anchor is only recorded from an OBSERVED straddling pair (notePhaseAnchorSample), so
    //     absence of evidence disables this member rather than inventing one;
    //   * shot_.anchorValidMs >= 0.0 is the SAME T2 carryover proof liveMeterCrossingAuthoritative
    //     already demands -- a lingering previous-shot meter cannot date this shot;
    //   * the tip must still be in the FUTURE, and no further than the constant itself, which
    //     bounds how stale an anchor may be (~380 ms) and rejects a backwards clock outright;
    //   * the flat-meter veto: this member cannot speak for a meter the sampler can positively
    //     see has STOPPED. It blocks on evidence of flatness, never on absence of evidence, so
    //     the early/low-sample horizon where the sampler has nothing to say is unaffected.
    // None of these can manufacture a deadline; each can only withdraw one, leaving the fallback
    // chain and, past it, the ordinary abort. Nothing here relaxes decision.valid: the combined
    // sigma still has to clear autonomousTipMaxCombinedSigmaMs, which still requires real lead
    // authority (an absent lead sd contributes 100 ms and fails the cap on its own).
    if (config_.tipPhaseEnabled
        && shot_.fillPhaseAnchorMs >= 0.0
        && shot_.anchorValidMs >= 0.0
        && std::isfinite(config_.tipPhaseSigmaMs) && config_.tipPhaseSigmaMs > 0.0
        && std::isfinite(shot_.fillPct) && shot_.fillPct >= config_.tipPhaseAnchorPct) {
        // [ORION_TIP_PHASE_LADDER] A shot dated at a HIGHER ladder level has less animation left,
        // so its constant is shortened by the measured slope. tipPhaseLevelAdjustmentMs returns
        // exactly 0 at the base anchor, so every shot that would have anchored before the ladder
        // existed keeps its constant, and therefore its aim, bit for bit.
        // [ORION_TYPE_TRIM] The per-type trim rides the same constant the ladder adjustment
        // does, so every phase consumer (the horizon-plausibility bound, slowMeterDeferBinds'
        // reconstruction of the constant, the corroboration veto) sees one self-consistent
        // dating. 0 for every type unless tip_phase_type_trim_enabled (default OFF).
        const double constantMs = effectiveTipPhaseConstantMs()
            + tipPhaseLevelAdjustmentMs(shot_.fillPhaseAnchorLevelPct)
            + tipPhaseTypeTrimMs(shot_.shotType);
        const double phaseTipAbsMs = shot_.fillPhaseAnchorMs + constantMs;
        if (std::isfinite(constantMs) && constantMs > 0.0 && std::isfinite(phaseTipAbsMs)) {
            decision.phaseAnchorMs = shot_.fillPhaseAnchorMs;
            decision.phaseTipAbsMs = phaseTipAbsMs;
            decision.phaseSigmaMs = config_.tipPhaseSigmaMs;
            // Publish the weight this member WOULD carry against whatever the chain above
            // selected, so the log shows its influence the way reg_weight shows registration's.
            // Telemetry only -- no branch reads it back -- and guarded so a diagnostic is never
            // the thing that divides by zero.
            if (std::isfinite(decision.sigmaMs) && decision.sigmaMs > 0.0) {
                const double phasePrecision = 1.0
                    / (config_.tipPhaseSigmaMs * config_.tipPhaseSigmaMs);
                decision.phaseWeight = phasePrecision
                    / (phasePrecision + 1.0 / (decision.sigmaMs * decision.sigmaMs));
            }
            const double horizonMs = phaseTipAbsMs - atMs;
            const bool horizonPlausible = horizonMs > 0.0 && horizonMs <= constantMs;
            // THE LIVE METER'S VETO. See RemapConfig::tipPhaseCorroborationSigmas: a lookup is a
            // claim about one specific animation, and handed a meter that is not that animation it
            // is confidently wrong in both directions -- including the direction that HOLDS a
            // release past a meter that has already topped out, which would be a guaranteed-late
            // fire. The sampler is biased late, but it is a direct measurement of THIS meter's
            // motion, so a gross disagreement is positive evidence that the constant does not
            // describe what is on screen. Same disagreement^2 <= k^2*(varA+varB) shape as the
            // reg/sampler compatibility test above.
            bool contradictedByLiveMeter = false;
            double disagreementMs = 0.0;
            if (decision.samplerAuthoritative && samplerSigmaMs > 0.0
                && std::isfinite(decision.samplerFit.crossingMs)
                && std::isfinite(config_.tipPhaseCorroborationSigmas)
                && config_.tipPhaseCorroborationSigmas > 0.0) {
                disagreementMs = decision.samplerFit.crossingMs - phaseTipAbsMs;
                const double combinedVar = samplerSigmaMs * samplerSigmaMs
                    + config_.tipPhaseSigmaMs * config_.tipPhaseSigmaMs;
                const double k = config_.tipPhaseCorroborationSigmas;
                contradictedByLiveMeter =
                    disagreementMs * disagreementMs > k * k * combinedVar;
            }
            // [ORION_PHASE_VETO_DIRECTIONAL] (settings phase_veto_directional, default OFF).
            //
            // THE VETO ABOVE IS SYMMETRIC, AND ONE OF ITS TWO DIRECTIONS IS BACKWARDS.
            // Its stated justification is the LATE hazard: a constant that describes the wrong
            // animation can hold a release past a meter that has already topped out. That reasoning
            // only licenses the direction where the sampler says the crossing is LATER than the
            // phase tip. In the opposite direction the veto fires on the sampler's own measured
            // +61-92ms EARLY bias, and demotes a healthy phase member on the strength of the one
            // estimator we know is wrong that way.
            //
            // MEASURED 2026-08-04..06, two independent investigations converging on this line:
            //   * 16 deadline-missed aborts (Go-To 5, No_Dip 8, Standstill 3) carried a dated,
            //     horizon-plausible phase tip with a schedulable command deadline (+12..+132ms)
            //     demoted on that tick because the sampler claimed a much earlier crossing; the
            //     sampler then declared its own deadline already past and the shot aborted at
            //     reservation age 0 -- the user's press falls through as a LATE.
            //   * 3 counted EARLIES where a transient sampler swing (crossing 490.9 -> 304.6 in
            //     8ms) demoted phase for a single tick; the sampler armed the token 90-116ms early
            //     and it fired inside the irreplaceable window. Per-frame records show those meters
            //     rose at normal speed -- the swing was a young-fit artifact, not the animation.
            //     Counterfactual: firing at the phase deadline releases all three at fill ~44,
            //     dead centre of the normal 33-44 band.
            // Both failure modes are the same defect, and both are on the sampler-EARLIER side.
            //
            // Fail-closed is preserved exactly: this only DECLINES to withdraw a member the engine
            // already dated from a witnessed anchor crossing. It cannot manufacture a deadline, and
            // the late hazard the veto exists for keeps its direction. A meter that has genuinely
            // stopped is still withdrawn by registrationSoloContradictedByFlatMeter() below.
            if (config_.phaseVetoDirectional && contradictedByLiveMeter
                && disagreementMs < 0.0) {
                contradictedByLiveMeter = false;
            }
            if (horizonPlausible && !contradictedByLiveMeter
                && !registrationSoloContradictedByFlatMeter(decision.samplerFit)) {
                decision.tipAbsMs = phaseTipAbsMs;
                decision.sigmaMs = config_.tipPhaseSigmaMs;
                decision.source = QStringLiteral("phase");
                decision.phasePrimary = true;
            }
        }
    }
    // [ORION_USER_LEAD_AUTHORITY] effective (not raw): under user-lead authority the raw sd is
    // 0.0, which would land in the 100.0 fallback and push combinedSigmaMs past the validity
    // cap — silently making every tip decision invalid and the whole readiness fix inert.
    const double effectiveLeadSd = effectiveLeadSdMs();
    const double leadSigmaMs = std::isfinite(effectiveLeadSd)
        && effectiveLeadSd > 0.0
        ? effectiveLeadSd : 100.0;
    const double tickSigmaMs = tickPhaseAuthoritative(atMs)
        ? std::max(0.5, tickPhaseSdMs_) : 4.8;
    if (std::isfinite(decision.sigmaMs) && decision.sigmaMs > 0.0
        && std::isfinite(leadSigmaMs) && std::isfinite(tickSigmaMs)) {
        decision.combinedSigmaMs = std::sqrt(
            decision.sigmaMs * decision.sigmaMs
            + leadSigmaMs * leadSigmaMs
            + tickSigmaMs * tickSigmaMs);
    }
    decision.valid = std::isfinite(decision.tipAbsMs)
        && decision.tipAbsMs > atMs && std::isfinite(decision.sigmaMs)
        && decision.sigmaMs > 0.0 && decision.sigmaMs <= 500.0
        && std::isfinite(decision.combinedSigmaMs)
        && decision.combinedSigmaMs > 0.0
        && decision.combinedSigmaMs <= config_.autonomousTipMaxCombinedSigmaMs;
    // [ORION_METER_DELAY_LEAD_STARVATION] With an applied meter delay pushing the consumed
    // lead past the visible-evidence ceiling, a visible-meter EXTRAPOLATOR (anything that is
    // not the phase lookup) can only be schedulable when its horizon exceeds the lead — i.e.
    // exactly the pre-anchor long-horizon regime this engine's own comments call the
    // sampler's worst (fewest samples, furthest extrapolation, the +61-92ms bias). Both live
    // squeakers on 2026-08-08 armed there and landed ~300ms early on the visible meter.
    // Withdraw the decision instead: the tick/subtick invalid paths hold the owned output
    // (never arm, never release) and the existing bounded aborts return pass-through. The
    // phase member is deliberately NOT withdrawn — its dating is accurate, its deadline is
    // simply past, and the unschedulable_lead attribution + SHOT LEAD CONFLICT line must
    // keep telling that truth. Fail-closed direction only: this can suppress a fire, never
    // create or hasten one, and it is inert (appliedDelay == 0) on every delay-free session.
    if (decision.valid && !decision.phasePrimary
        && visibleEvidenceLeadStarvedByMeterDelay(measuredLeadForActuationMs())) {
        decision.valid = false;
        decision.delayStarvedVisibleSource = true;
    }
    // === [ORION_PRESS_ANCHOR] THE FOURTH MEMBER, and the only delay-immune one ==============
    //
    // Every member above is dated from the DELAYED video, which is exactly why the starvation
    // clause above must withdraw them at high applied delay. This member's anchor is the raw
    // press WALL time (input hook edge, undelayed by construction), so its deadline
    //     press_wall + learned press->tip + V + applied_delay
    // is schedulable at ANY delay: the delay enters only as an additive shift into visible
    // time, where the pre-existing schedule-fire path consumes it unchanged.
    //
    // FAIL-CLOSED, on every gate:
    //   * flag OFF (default) -> this block runs ONLY in the delay-starved regime (the
    //     [ORION_PRESS_ANCHOR_BOOTSTRAP] clause below); every delay-0 session and every
    //     sub-starvation delay stays byte-identical to the pre-scaffold build;
    //   * the press must be dated for exactly THIS shot's physical epoch — a stale or
    //     foreign press dates nothing;
    //   * the learner must hold >= pressAnchoredMinSamples of accumulated REAL observation
    //     weight for this shot type — EXCEPT in the delay-starved regime, where the
    //     alternative is structurally "no shot, every shot" (see the bootstrap note below):
    //     there a MEASURED factory prior may fire at the wide factory sigma, labelled
    //     press_anchored_boot so the log carries the provenance;
    //   * a V estimate must exist (l_fixed, or the loop authority stand-in) — without one
    //     there is no honest wall->visible mapping;
    //   * ADOPT-ONLY-ON-FAILURE: when any in-video member produced a valid decision, that
    //     decision stands untouched (source, sigma, everything). This member may only adopt
    //     a decision the chain above could not make — including one withdrawn by the
    //     starvation clause, which is its entire reason to exist. It can therefore never
    //     hasten an in-video fire, and its per-type MAD sigma still has to clear the same
    //     combined-sigma validity cap every other member faces.
    // [ORION_PRESS_ANCHOR_BOOTSTRAP 2026-08-09] The owner's 2026-08-08 wave-3 session asked
    // the one question the strict min-samples gate cannot answer: with a delay large enough
    // that the starvation clause above withdraws every in-video member, and ZERO press-tip
    // observations banked (Phase A had never run in any binary on that rig), which member
    // fires the FIRST delayed shot? Under the strict gate: none, ever — aborted shots never
    // reach the landing evaluator, so the observations that would open the gate can only be
    // produced by the very fires the gate refuses. Owner-directed resolution: in EXACTLY the
    // delay-starved regime — where the honest alternative is no shot, this session and every
    // session — the member runs unflagged and may fire on a MEASURED factory prior at the
    // wide factory sigma (60ms, still subject to the same combined-sigma validity cap and
    // the same adopt-only-on-failure clause). Each such fire completes a landing, Phase A
    // records the observation, and the windowed learner replaces the prior within a session.
    // Delay-0 sessions and sub-starvation delays are byte-identical to the strict build:
    // the regime term is false there and the flag default stays OFF.
    // ─── [ORION_PRESS_ANCHOR_BOOTSTRAP] 2026-08-09 POST-MORTEM: THIS MEMBER HAS NEVER ARMED ──
    // Searched both production logs (orion_native.log{,.1}, 2026-08-08 + 2026-08-09, 400
    // PRESS-TIP observations, 60 of them with a delay applied at D in {100,150,175,210,300,600}
    // and leads from 270 to 800): the strings "press_anchored" and "press_anchored_boot" appear
    // ZERO times. Every armed_source is "phase" or "registration+sampler_far". Two independent
    // reasons, both still true of the code below:
    //
    //   1. THE REGIME NEVER OPENS AT A SANE LEAD. pressAnchorDelayBootstrapRegime requires
    //      lead > maxSchedulableTipLeadMs(). The operator's working lead is 270-309 ms against a
    //      391 ms ceiling, so on every delayed shot they actually played the regime was FALSE and
    //      this whole block was skipped — including the eight badly mistimed 13:33-13:34 landings
    //      the [ORION_METER_DELAY_LEAD_KEYING] work is about.
    //   2. WHEN IT DOES OPEN, THE ADOPT GATE IS UNREACHABLE IN THE CASE THAT ACTUALLY OCCURS —
    //      a PHASE-owned decision. The starvation clause above deliberately does not withdraw a
    //      phasePrimary decision ("its dating is accurate, its deadline is simply past"), so
    //      decision.valid stays TRUE, and the adoption below is gated on `if (!decision.valid)`.
    //      (With only a non-phase extrapolator present the clause DOES withdraw and the member
    //      does adopt — that is the path pressAnchoredDisabledByDefault() exercises. Live, the
    //      phase member is present on essentially every shot, so that path is theoretical.)
    //      Live proof, 2026-08-09 13:37 at lead 612:
    //      "TIP DEADLINE DECISION: disposition=rejected_missed source=phase ...
    //       reservation_disposition=unschedulable_lead" on all six attempts. The phase member
    //      owned a valid-but-unschedulable decision, so this member was never consulted, and the
    //      shots aborted.
    //
    // (2) IS DELIBERATELY LEFT UNFIXED, because fixing it would ship a fire this rig cannot
    // land. Measured press->tip dispersion, tightest cohort available (one session, one shot
    // type, one lead, delay 0, n=24, log.1 04:23-04:29): median 581.7 ms, sd 82.2 ms,
    // MAD 30.2 ms (robust sd ~45 ms), IQR 552-615. The target it must hit is the green window,
    // whose measured width on the same rig is 2.0-2.8 pp at a meter velocity of 0.17-0.21 pp/ms
    // — i.e. 10-16 ms. A predictor with a 45 ms robust sd aimed at a 13 ms window cannot green;
    // the in-video phase member it would replace runs sigma 13-15 ms. Opening the gate would
    // convert an honest abort into a confident miss, which is the trade this codebase's
    // fail-closed doctrine exists to refuse. If it is ever revisited, the blocker to clear FIRST
    // is the dispersion, not the gate.
    const bool pressAnchorDelayBootstrapRegime = meterDelayAppliedMs_ > 0.0
        && visibleEvidenceLeadStarvedByMeterDelay(measuredLeadForActuationMs());
    if (config_.pressAnchoredEnabled || pressAnchorDelayBootstrapRegime) {
        const double learnedTipMs = pressAnchoredLearnedTipMs(shot_.shotType);
        const double learnedWeight = pressAnchoredLearnedWeight(shot_.shotType);
        QString vSrc;
        const double vMs = pressAnchoredVideoLatencyMs(&vSrc);
        const bool pressDated = shot_.physicalShotEpoch != 0
            && pressWallEpoch_ == shot_.physicalShotEpoch
            && pressWallMsForEpoch_ >= 0.0;
        const bool samplesGateMet =
            learnedWeight >= static_cast<double>(config_.pressAnchoredMinSamples);
        // Factory-prior fires are confined to the starved regime and to shot types whose
        // packaged prior is a MEASUREMENT: Go-To's seed is an unmeasured placeholder (see
        // the AppConfigData note) and its wind-up is the variable-length animation
        // gotoMeterWait exists for — it stays behind the real-samples gate.
        const bool factoryBootstrap = !samplesGateMet && pressAnchorDelayBootstrapRegime
            && shot_.shotType != QLatin1String("Go-To");
        if (pressDated
            && (samplesGateMet || factoryBootstrap)
            && std::isfinite(learnedTipMs)
            && learnedTipMs >= kPressAnchoredLearnMinMs
            && learnedTipMs <= kPressAnchoredLearnMaxMs
            && vMs > 0.0) {
            const double tipAbsVisibleMs = pressWallMsForEpoch_ + learnedTipMs + vMs
                + meterDelayAppliedMs_;
            const double paSigmaMs = pressAnchoredLearnedSigmaMs(shot_.shotType);
            if (std::isfinite(tipAbsVisibleMs) && tipAbsVisibleMs > atMs
                && std::isfinite(paSigmaMs) && paSigmaMs > 0.0) {
                decision.pressAnchoredTipAbsMs = tipAbsVisibleMs;
                decision.pressAnchoredSigmaMs = paSigmaMs;
                // Publish the weight it WOULD carry against whatever the chain selected —
                // same telemetry contract as phaseWeight/regWeight, no branch reads it back.
                if (std::isfinite(decision.sigmaMs) && decision.sigmaMs > 0.0) {
                    const double paPrecision = 1.0 / (paSigmaMs * paSigmaMs);
                    decision.pressAnchoredWeight = paPrecision
                        / (paPrecision + 1.0 / (decision.sigmaMs * decision.sigmaMs));
                }
                if (!decision.valid) {
                    const double paCombinedMs = std::sqrt(
                        paSigmaMs * paSigmaMs + leadSigmaMs * leadSigmaMs
                        + tickSigmaMs * tickSigmaMs);
                    if (paSigmaMs <= 500.0 && std::isfinite(paCombinedMs)
                        && paCombinedMs > 0.0
                        && paCombinedMs <= config_.autonomousTipMaxCombinedSigmaMs) {
                        decision.tipAbsMs = tipAbsVisibleMs;
                        decision.sigmaMs = paSigmaMs;
                        decision.combinedSigmaMs = paCombinedMs;
                        decision.source = factoryBootstrap
                            ? QStringLiteral("press_anchored_boot")
                            : QStringLiteral("press_anchored");
                        decision.pressAnchoredPrimary = true;
                        decision.valid = true;
                        // The visible sources WERE starved, but this decision is now owned
                        // by the wall-anchored member — its lead passes through, and the
                        // once-per-shot starvation diagnostic must not fire for a shot that
                        // actually scheduled.
                        decision.delayStarvedVisibleSource = false;
                    }
                }
            }
        }
    }
    return decision;
}

bool AutomationEngine::slowMeterDeferBinds(const AutonomousTipDecision& decision,
                                           double fireAtMs, double effectiveLeadMs,
                                           double atMs) const noexcept
{
    // [ORION_SLOW_METER_DEFER] See RemapConfig::slowMeterDeferEnabled for the 2026-08-06
    // measurement this encodes. Every early return is a refusal to BIND, i.e. the existing
    // behaviour; binding itself only ever declines to create/adopt an EARLIER deadline.
    if (!config_.slowMeterDeferEnabled) {
        return false;
    }
    if (!std::isfinite(config_.slowMeterDeferUndercutMs)
        || config_.slowMeterDeferUndercutMs <= 0.0
        || !std::isfinite(fireAtMs) || !std::isfinite(effectiveLeadMs)) {
        return false;
    }
    // The phase member must be PRESENT on this decision (witnessed anchor, valid dating --
    // canonicalAutonomousTipDecision only fills these fields once its four fail-closed gates
    // passed) -- but deliberately NOT necessarily primary: the measured failure is precisely
    // the tick where the corroboration veto demoted it while its dating was still right.
    if (!std::isfinite(decision.phaseTipAbsMs) || decision.phaseTipAbsMs <= 0.0
        || !std::isfinite(decision.phaseAnchorMs) || decision.phaseAnchorMs < 0.0) {
        return false;
    }
    // Mirror the member's own horizon-plausibility: the phase tip must still be ahead and no
    // further than its own constant (a bound on anchor staleness). A phase tip already behind
    // us can justify nothing; the guard stands down and the ordinary paths decide.
    const double horizonMs = decision.phaseTipAbsMs - atMs;
    const double constantMs = decision.phaseTipAbsMs - decision.phaseAnchorMs;
    if (!std::isfinite(horizonMs) || !std::isfinite(constantMs)
        || horizonMs <= 0.0 || constantMs <= 0.0 || horizonMs > constantMs) {
        return false;
    }
    const double phaseFireAtMs = decision.phaseTipAbsMs - effectiveLeadMs;
    const double undercutMs = phaseFireAtMs - fireAtMs;
    // Measured populations (arm-time undercut, 120 releases): normal max +0.9ms, slow-regime
    // minimum +88.7ms. The 45ms default sits mid-gap; see the RemapConfig comment.
    return undercutMs > config_.slowMeterDeferUndercutMs;
}

bool AutomationEngine::liveMeterCrossingAuthoritative(
    const TemporalSampler::CrossingFit& fit, double atMs) const noexcept
{
    // Integrity bounds, not timing priors: three frames delivered in one burst
    // (or a physically impossible near-vertical jump) can fit a perfect line but
    // are not trustworthy evidence of a live animated meter.
    constexpr double kMinLiveTrajectorySpanMs = 8.0;
    constexpr double kMaxPlausibleMeterSlopePctPerMs = 2.0;
    if (fit.n < 3 || !std::isfinite(fit.crossingMs) || fit.crossingMs <= atMs
        || !std::isfinite(fit.slopePctPerMs) || fit.slopePctPerMs <= 0.01
        || fit.slopePctPerMs > kMaxPlausibleMeterSlopePctPerMs
        || !std::isfinite(fit.spanMs) || fit.spanMs < kMinLiveTrajectorySpanMs
        || !std::isfinite(fit.wrss) || fit.wrss < 0.0
        || !std::isfinite(fit.weightSum) || fit.weightSum <= 0.0
        || shot_.anchorValidMs < 0.0) {
        return false;
    }
    // A concave-down fit that never reaches the 100% target reports its fitted
    // peak as the best attainable release point.  That is useful only near the
    // real top of the meter.  Sparse early samples can also look concave-down
    // (for example 5,9,13,14) and predict a 14.5% "peak" a few milliseconds
    // away.  Never turn that mathematical vertex into a release unless it is
    // independently inside this shot's detected green band.  The threshold is
    // frame-derived and shot-type neutral; absent/invalid green evidence fails
    // closed and the live stream can establish a real target crossing later.
    if (fit.usedPeakFallback) {
        const bool greenBandValid = std::isfinite(shot_.greenStartPct)
            && std::isfinite(shot_.greenEndPct)
            && shot_.greenStartPct >= 0.0
            && shot_.greenEndPct >= shot_.greenStartPct;
        if (!greenBandValid || !std::isfinite(fit.predictedPeakPct)
            || fit.predictedPeakPct < shot_.greenStartPct) {
            return false;
        }
    }
    // predictCrossing uses exponentially weighted least squares. Normalize by
    // the adopted weight sum; raw n increasingly understates residual error.
    const double weightedRmsePct = std::sqrt(fit.wrss / fit.weightSum);
    return std::isfinite(weightedRmsePct) && weightedRmsePct <= 2.0;
}

bool AutomationEngine::autonomousLiveMeterReady() const noexcept
{
    return autonomousLiveMeterTimingEnabled() && measuredLeadAuthoritative(nowMs());
}

bool AutomationEngine::measuredLeadValidatedAuthority() const noexcept
{
    // Deliberately NOT autonomousLiveMeterReady(). Actuation readiness is satisfied by a packaged
    // factory prior alone (measuredLeadAuthoritative passes on factoryRouteReady), and using that
    // as the definition of "calibrated" created a hard deadlock: the prior granted readiness,
    // readiness both cleared and permanently blocked latencyCalibrationMode_, and the controlled
    // L1/L2 protocol is the only path to validated authority. Shipping any factory prior therefore
    // guaranteed the system could never self-calibrate -- which is exactly what the 2026-08-03
    // session shows (every marker calibration=0 controlled=0, authority stuck on the seed).
    //
    // A prior lets the bot SHOOT on shot #1. Only a validated posterior means it has MEASURED this
    // machine, and only that may retire calibration.
    return autonomousLiveMeterTimingEnabled()
        && measuredLeadAuthoritative(nowMs())
        && measuredLatencyAuthorityKind_ == QLatin1String("validated");
}

void AutomationEngine::setLatencyCalibrationMode(bool enabled)
{
    const bool next = enabled && autonomousLiveMeterTimingEnabled()
        && !measuredLeadValidatedAuthority();
    const bool nextAutomatic = false;
    if (latencyCalibrationMode_ == next
        && latencyCalibrationAutomatic_ == nextAutomatic) {
        refreshLatencyCalibrationStatus(true);
        return;
    }
    if (!enabled && latencyCalibrationMode_) {
        cancelPendingLatencyCalibrationOwnership();
    }
    latencyCalibrationMode_ = next;
    latencyCalibrationAutomatic_ = nextAutomatic;
    refreshLatencyCalibrationStatus(true);
}

void AutomationEngine::setAutomaticLatencyCalibrationMode(bool enabled)
{
    const bool next = enabled && autonomousLiveMeterTimingEnabled()
        && !measuredLeadValidatedAuthority();
    if (latencyCalibrationMode_ == next && latencyCalibrationAutomatic_ == next) {
        refreshLatencyCalibrationStatus(true);
        return;
    }
    if (!enabled && latencyCalibrationMode_) {
        cancelPendingLatencyCalibrationOwnership();
    }
    latencyCalibrationMode_ = next;
    latencyCalibrationAutomatic_ = next;
    refreshLatencyCalibrationStatus(true);
}

void AutomationEngine::refreshLatencyCalibrationStatus(bool force)
{
    const bool ready = autonomousLiveMeterReady();
    // Retire calibration only on a VALIDATED posterior. `ready` is still what the UI is told,
    // because it is the honest answer to "can the bot shoot right now" -- but a factory prior
    // answering yes to that question must not also silently end the measurement that would
    // replace it. See measuredLeadValidatedAuthority() for the deadlock this breaks.
    if (latencyCalibrationMode_ && measuredLeadValidatedAuthority()) {
        // A pending Square is not seized merely because the lead converged. It remains in the
        // same physical-shot epoch and can be promoted only by the normal strict current-epoch
        // meter proof. Keeping the candidate is essential: clearing it here stranded the exact
        // shot whose telemetry completed calibration and forced pass-through for its lifetime.
        latencyCalibrationMode_ = false;
        latencyCalibrationAutomatic_ = false;
    }
    const bool changed = force
        || latencyCalibrationMode_ != lastLatencyCalibrationStatusActive_
        || ready != lastLatencyCalibrationStatusReady_
        || measuredLatencyN_ != lastLatencyCalibrationStatusN_
        || std::abs(measuredLatencyMs_ - lastLatencyCalibrationStatusLeadMs_) > 1e-6
        || std::abs(measuredLatencySdMs_ - lastLatencyCalibrationStatusSdMs_) > 1e-6;
    if (!changed) {
        return;
    }
    lastLatencyCalibrationStatusActive_ = latencyCalibrationMode_;
    lastLatencyCalibrationStatusReady_ = ready;
    lastLatencyCalibrationStatusN_ = measuredLatencyN_;
    lastLatencyCalibrationStatusLeadMs_ = measuredLatencyMs_;
    lastLatencyCalibrationStatusSdMs_ = measuredLatencySdMs_;
    emit latencyCalibrationStatusChanged(latencyCalibrationMode_, ready, measuredLatencyN_,
                                         measuredLatencyMs_, measuredLatencySdMs_);
}

bool AutomationEngine::tickPhaseAuthoritative(double atMs) const noexcept
{
    if (tickPhaseLastUpdateMs_ < 0.0
        || !std::isfinite(config_.measuredLeadFreshnessMs)
        || config_.measuredLeadFreshnessMs <= 0.0) {
        return false;
    }
    const double ageMs = atMs - tickPhaseLastUpdateMs_;
    return std::isfinite(ageMs) && ageMs >= -1e-6
        && ageMs <= config_.measuredLeadFreshnessMs + 1e-6
        && tickSnapEngaged(tickPhaseConf_, tickPhaseSdMs_,
                           config_.fusedTickSnapMinConf,
                           config_.fusedTickSnapMaxSdMs);
}

double AutomationEngine::adaptiveAutonomousSchedulerHorizonMs() const noexcept
{
    // The window must be wider than the distance the command deadline MOVES between frames,
    // otherwise the deadline steps over it and is discovered already overdue.
    //
    // Measured on the 2026-08-04 live batch (1,067 refinements, 42 aborts): the per-frame move of
    // the command deadline is |21.6| ms at p50 and 148.7 ms at p90, and it drifts systematically
    // EARLIER (median -10 to -16 ms per refinement) as the fit sharpens. The old 2*gap+6 window is
    // 39.4 ms at 60 FPS -- narrower than a routine refinement -- so the last observation before an
    // abort was typically +43 ms (outside) and the next was already -16 ms (past). 58% of aborts
    // had a final move of -40 ms or worse. Three clean gaps of extra width is what covers that
    // step; the per-frame reschedule below still refines the armed deadline every frame, so a
    // wider window changes WHEN we commit a provisional deadline, not WHERE we fire (measured:
    // 89% of already-firing shots keep a bit-identical fire instant, 3% move >25 ms, mostly
    // earlier).
    //
    // 2026-08-04 POST-FIX BATCH (51 fires / 9 aborts, 58 shots carrying reservations). The 4*gap+6
    // widening above took the fire rate 71% -> 85%, but it is still NARROWER THAN THE STEP IT WAS
    // WRITTEN TO COVER: at 60 FPS it is 72.7 ms against a p90 per-refinement collapse of 105.8 ms
    // and p95 of 122.7 ms. The residue is directly measurable -- 8 of those 58 shots never once
    // observed a valid command_eta inside (0, 72.7], and the smallest positive command_eta each
    // one ever saw was 73.5 / 74.4 / 85.4 / 97.3 / 113.5 / 145.9 ms. Three of them aborted
    // `live_tip_deadline_missed` with reservation_disposition=never_inside_arming_window and
    // reservation_ever_armable=0: the deadline stepped clean over the window between two frames
    // and was next seen at -4.7 / -10.3 ms, i.e. missed by single digits after a >100 ms jump.
    //
    // Seven gaps covers the p95 collapse (7*16.7+6 = 122.9 ms) and admits the 97.3 and 113.5 ms
    // cases with margin. The ceiling moves 100 -> 135 so the formula is not silently clipped back
    // under its own target on a slower source.
    //
    // This cannot make a release late. scheduleFire() still refuses any deadline that is not
    // strictly in the future, the [ORION_ROLLING_LEASE] contract still kills a token whose rolling
    // authority does not cover its deadline by the time it turns irreplaceable, and the armed
    // deadline is still re-fitted on every genuine frame down to the drift tolerance. Arming
    // earlier changes only WHEN a provisional deadline is committed; the fire instant is set by
    // the LAST refinement, not the first arm. The measured cost is extra reschedule churn, which
    // the 1.0 ms drift tolerance and the imminent/in-flight guards already bound.
    const double sourceAwareMs = 7.0 * cleanSourceGapMs() + 6.0;
    return std::clamp(std::max(config_.schedulerHorizonMs, sourceAwareMs), 22.0, 135.0);
}

void AutomationEngine::parseDevFireOffsetEnv()
{
    // [ORION_DEV_FIRE_OFFSET] Dev-only, env-only, compiled out of production builds, and
    // impossible to arm by accident: the variable must parse EXACTLY as "list:a,b,c" or
    // "uniform:lo:hi" — a typo disarms the hook loudly instead of installing a guessed sweep.
    // The hook deliberately mistimes scheduled fires; it exists for the banner-graded offset
    // sweep, the one experiment whose verdict (the game's own TIMING banner) cannot be
    // contaminated by our capture-side observation grid.
#ifndef ORION_PRODUCTION_BUILD
    if (devFireOffsetEnvParsed_) {
        return;
    }
    devFireOffsetEnvParsed_ = true;
    if (!qEnvironmentVariableIsSet("ORION_DEV_FIRE_OFFSET_SWEEP")) {
        return;
    }
    const QString spec =
        qEnvironmentVariable("ORION_DEV_FIRE_OFFSET_SWEEP").trimmed();
    bool ok = false;
    if (spec.startsWith(QStringLiteral("list:"))) {
        const QStringList parts = spec.mid(5).split(QLatin1Char(','), Qt::SkipEmptyParts);
        QVector<double> values;
        bool allOk = !parts.isEmpty();
        for (const QString& p : parts) {
            bool numOk = false;
            const double v = p.trimmed().toDouble(&numOk);
            if (!numOk || !std::isfinite(v) || std::abs(v) > kDevFireOffsetMaxAbsMs) {
                allOk = false;
                break;
            }
            values.append(v);
        }
        if (allOk) {
            devFireOffsetListMs_ = values;
            devFireOffsetUniform_ = false;
            ok = true;
        }
    } else if (spec.startsWith(QStringLiteral("uniform:"))) {
        const QStringList parts = spec.mid(8).split(QLatin1Char(':'));
        if (parts.size() == 2) {
            bool loOk = false;
            bool hiOk = false;
            const double lo = parts[0].trimmed().toDouble(&loOk);
            const double hi = parts[1].trimmed().toDouble(&hiOk);
            if (loOk && hiOk && std::isfinite(lo) && std::isfinite(hi) && lo < hi
                && std::abs(lo) <= kDevFireOffsetMaxAbsMs
                && std::abs(hi) <= kDevFireOffsetMaxAbsMs) {
                devFireOffsetUniformLoMs_ = lo;
                devFireOffsetUniformHiMs_ = hi;
                devFireOffsetUniform_ = true;
                ok = true;
            }
        }
    }
    devFireOffsetArmed_ = ok;
    if (ok) {
        emit engineDiagnostic(QStringLiteral(
            "DEV FIRE OFFSET SWEEP ARMED: spec=%1 max_abs_ms=%2 -- scheduled fires will be "
            "deliberately displaced per shot; phase/feedforward/lead learning is FENCED for "
            "this session")
                                  .arg(spec)
                                  .arg(kDevFireOffsetMaxAbsMs, 0, 'f', 1));
    } else {
        emit engineDiagnostic(QStringLiteral(
            "DEV FIRE OFFSET SWEEP REFUSED: unparseable or out-of-bounds spec '%1' "
            "(want list:a,b,c or uniform:lo:hi, |offset| <= %2ms) -- hook stays DISARMED")
                                  .arg(spec)
                                  .arg(kDevFireOffsetMaxAbsMs, 0, 'f', 1));
    }
#endif
}

double AutomationEngine::devFireOffsetForShotMs()
{
    // [ORION_DEV_FIRE_OFFSET] One draw per SHOT (keyed by the arm token), stable across the
    // re-arm/refinement churn inside a shot so the displacement is a single well-defined
    // treatment the offline join can regress against.
    if (!devFireOffsetArmed_) {
        return 0.0;
    }
    if (devFireOffsetShotToken_ != shot_.armToken) {
        devFireOffsetShotToken_ = shot_.armToken;
        double draw = 0.0;
        if (devFireOffsetUniform_) {
            draw = devFireOffsetUniformLoMs_
                + QRandomGenerator::global()->generateDouble()
                    * (devFireOffsetUniformHiMs_ - devFireOffsetUniformLoMs_);
        } else if (!devFireOffsetListMs_.isEmpty()) {
            draw = devFireOffsetListMs_[devFireOffsetNextIdx_
                                        % devFireOffsetListMs_.size()];
            ++devFireOffsetNextIdx_;
        }
        devFireOffsetShotMs_ = std::clamp(draw, -kDevFireOffsetMaxAbsMs,
                                          kDevFireOffsetMaxAbsMs);
        emit engineDiagnostic(QStringLiteral(
            "DEV FIRE OFFSET DRAW: shot_attempt=%1 offset_ms=%2")
                                  .arg(shot_.armToken)
                                  .arg(devFireOffsetShotMs_, 0, 'f', 2));
    }
    return devFireOffsetShotMs_;
}

bool AutomationEngine::scheduleFire(double deadlineMs, double now, double horizonOverrideMs,
                                    ScheduledFireAuthority authority)
{
    // [ORION_ARM_GATE_TRACE] Every `return false` below used to be silent. That silence cost three
    // investigation rounds: the live logs showed a valid, still-future deadline being refused
    // thousands of times with no record of WHICH gate refused it. Name the gate exactly once per
    // gate per shot attempt (bounded: scheduleFire runs on every 4 ms tick).
    double horizonForLog = horizonOverrideMs;
    double authorityExpiryForLog = -1.0;
    const auto rejectGate = [&](AutomationEngine::ArmGate gate) -> bool {
        const quint32 bit = 1u << static_cast<unsigned>(gate);
        if (schedFireRejectLogArmToken_ != shot_.armToken) {
            schedFireRejectLogArmToken_ = shot_.armToken;
            schedFireRejectLoggedMask_ = 0;
        }
        if ((schedFireRejectLoggedMask_ & bit) == 0u) {
            schedFireRejectLoggedMask_ |= bit;
            emit engineDiagnostic(QStringLiteral(
                "SCHEDULE FIRE REJECT: gate=%1 authority=%2 command_eta_ms=%3 horizon_ms=%4 "
                "authority_eta_ms=%5 frame_age_ms=%6 fill_pct=%7 physical_epoch=%8 "
                "shot_attempt=%9")
                                      .arg(armGateName(gate))
                                      .arg(static_cast<int>(authority))
                                      .arg(deadlineMs - now, 0, 'f', 3)
                                      .arg(horizonForLog, 0, 'f', 3)
                                      .arg(authorityExpiryForLog >= 0.0
                                               ? authorityExpiryForLog - now : -1.0,
                                           0, 'f', 3)
                                      .arg(shot_.frameAgeMs, 0, 'f', 2)
                                      .arg(shot_.fillPct, 0, 'f', 2)
                                      .arg(shot_.physicalShotEpoch)
                                      .arg(shot_.armToken));
        }
        return false;
    };

    if (!config_.subTickScheduler || !armed()) {
        return rejectGate(ArmGate::DisabledOrDisarmed);
    }
    if (schedFireDeadlineMs_ >= 0.0) {
        return rejectGate(ArmGate::AlreadyArmed);
    }
    const auto routeAttestation = controllerDeliveryRouteAttestationSnapshot();
    const quint64 routeGeneration = routeAttestation.generation;
    const LatencyControllerRoute route = routeAttestation.route;
    double horizon = horizonOverrideMs > 0.0 ? horizonOverrideMs : config_.schedulerHorizonMs;
    if (horizonOverrideMs <= 0.0
        && authority == ScheduledFireAuthority::AutonomousMeterVision) {
        horizon = adaptiveAutonomousSchedulerHorizonMs();
    }
    horizonForLog = horizon;
    if (deadlineMs <= now) {
        return rejectGate(ArmGate::DeadlinePast);
    }
    if ((deadlineMs - now) > horizon) {
        return rejectGate(ArmGate::OutsideHorizon);
    }
    // The deadline must satisfy the same min-hold floor the in-tick release path enforces.
    if (authority != ScheduledFireAuthority::AutonomousMeterVision
        && shot_.holdStartMs > 0.0
        && (deadlineMs - shot_.holdStartMs) < config_.minHoldMs) {
        return rejectGate(ArmGate::MinHold);
    }
    const bool requiresGenuineFrame = authority == ScheduledFireAuthority::MeterVision
        || authority == ScheduledFireAuthority::AutonomousMeterVision;
    const bool requiresPoseFrame = authority == ScheduledFireAuthority::Pose;
    double authorityExpiryMs = requiresGenuineFrame
        ? meterAuthorityExpiryMs()
        : (requiresPoseFrame ? poseAuthorityExpiryMs() : -1.0);
    if (authority == ScheduledFireAuthority::AutonomousMeterVision
        && measuredLeadLastUpdateMs_ >= 0.0
        && std::isfinite(config_.measuredLeadFreshnessMs)) {
        // A precise-fire token may not outlive either of its two authorities:
        // the source-frame lease OR the measured-lead snapshot lease. The
        // worker consumes this single intersected expiry before submit.
        authorityExpiryMs = std::min(authorityExpiryMs,
            measuredLeadLastUpdateMs_ + config_.measuredLeadFreshnessMs);
    }
    authorityExpiryForLog = authorityExpiryMs;
    // [ORION_ROLLING_LEASE] `deadline <= authorityExpiry` is a SUBMIT-TIME invariant, not an
    // ARM-TIME one. Enforcing it here was the single largest remaining loss.
    //
    // meterAuthorityExpiryMs() is `capture_time_of_newest_frame + age_limit`. Between frames that
    // is a FIXED absolute instant, so requiring the deadline to sit under it means "the frame we
    // hold right now must, by itself, authorize a fire that happens up to a horizon from now".
    // The frame stream cannot satisfy that: it authorizes the fire with the frame that will exist
    // AT the fire, one or two cadences later. The arithmetic consequence was an effective arming
    // window of `50 - frame_age` ms -- and across 268 live promotions the largest command_eta ever
    // armed was 40.41 ms, against deadlines that routinely needed to be committed 60-150 ms out.
    // Counterfactual replay of the 2026-08-04 batch: widening the horizon with this check in place
    // recovered EXACTLY ZERO aborts at 64, 100, 150 and 250 ms; dropping it recovered 20-32 of 42.
    //
    // What replaces it, so fail-closed is strictly preserved:
    //   1. schedFireAuthorityExpiryMs_ now ROLLS FORWARD on every genuine frame that keeps this
    //      token's vision epoch (refreshScheduledFireAuthorityLease), instead of staying frozen to
    //      the arming frame.
    //   2. The hold/tick authority fences kill the token the moment `now` passes that rolling
    //      expiry, and an EXPIRED authority no longer gets the imminent-token bypass.
    //   3. A token whose deadline the rolling expiry still does not cover by the time it becomes
    //      irreplaceable is killed outright -- an uncovered deadline can never be authorized, so
    //      it aborts exactly as it does today rather than firing on stale evidence.
    // No path here creates a late command: an unauthorized deadline still ends as an abort.
    const bool armTimeLeaseRequired =
        authority != ScheduledFireAuthority::AutonomousMeterVision;
    if (requiresGenuineFrame) {
        if (config_.noMeterEnabled) {
            return rejectGate(ArmGate::NoMeterMode);
        }
        if (authorityExpiryMs < 0.0) {
            return rejectGate(ArmGate::NoAuthority);
        }
        if (armTimeLeaseRequired && deadlineMs > authorityExpiryMs + 1e-6) {
            return rejectGate(ArmGate::AuthorityLease);
        }
        if (!meterReleaseAuthorityCurrent(now)) {
            return rejectGate(ArmGate::AuthorityStaleNow);
        }
    }
    if (requiresPoseFrame
        && (!config_.noMeterEnabled || authorityExpiryMs < 0.0
            || deadlineMs > authorityExpiryMs + 1e-6
            || !poseReleaseAuthorityCurrent(now))) {
        return rejectGate(ArmGate::PoseAuthority);
    }
    // [Phase-2 A2(c)] EARLIER-ONLY console-tick snap, applied at the single choke point where a
    // deadline becomes THE scheduled fire -- so it covers every path that actually fires today
    // (live_meter_tip, feedforward_target, ...), not just the fused one.
    //
    // WHY IT MOVED HERE. The snap already existed, but only inside the fused-posterior block, and
    // it only reached the pad when `fusedOwns` was true -- which additionally requires
    // measuredLeadAuthoritative(). That authority comes from probe labels, and on 2026-08-05 the
    // live engine still read `sd_ms=28.7 n=0`: not one probe label has ever been accepted on this
    // install. So the fused rule could never own a shot, and a perfect tick fit would have changed
    // nothing. The snap was stranded behind a gate that has never opened.
    //
    // SAFETY. Every authority gate above tests `deadlineMs > authorityExpiryMs`; this only ever
    // moves the deadline EARLIER (earlierOnlyTickSnapMs never returns a later value, max shift is
    // under one tick), so a deadline that passed those gates still passes them. Snapping later
    // would be unsafe -- a slip past the edge costs a full tick -- which is exactly why the
    // earlier-only variant exists rather than the centering one.
    //
    // INERT UNTIL MEASURED. tickPhaseAuthoritative() is false without a trustworthy probe-run
    // phase fit, so with today's telemetry this branch never runs and scheduling is byte-identical.
    if (tickPhaseAuthoritative(now)) {
        const double kTickMs = 1000.0 / 60.0;
        // Convert the probe-fit EDGE phase (press-EPOCH ms mod P) onto the engine clock with a
        // DIRECT clock read, never the capture bridge: epochToEngineOffsetMs_ carries the
        // capture->ingestion transport delay as a systematic phase bias that could consume the
        // whole 5.5ms epsilon and slip the fire a full tick late.
        const double engineMinusEpoch =
            now - static_cast<double>(QDateTime::currentMSecsSinceEpoch());
        double edgeEngine = std::fmod(tickPhaseEpochMs_ + engineMinusEpoch, kTickMs);
        if (edgeEngine < 0.0) {
            edgeEngine += kTickMs;
        }
        const double snapped = earlierOnlyTickSnapMs(deadlineMs, edgeEngine,
                                                     config_.fusedTickSnapEpsilonMs, kTickMs);
        if (std::isfinite(snapped) && snapped <= deadlineMs) {
            // One bounded line per scheduled fire. A silent snap is indistinguishable from no
            // snap at all in a session log, and that ambiguity has cost this project whole
            // investigation rounds before.
            emit engineDiagnostic(QStringLiteral(
                "Tick snap: deadline %1 -> %2 (shift -%3ms, edge_phase=%4 conf=%5)")
                                      .arg(deadlineMs, 0, 'f', 1)
                                      .arg(snapped, 0, 'f', 1)
                                      .arg(deadlineMs - snapped, 0, 'f', 2)
                                      .arg(edgeEngine, 0, 'f', 2)
                                      .arg(tickPhaseConf_, 0, 'f', 2));
            deadlineMs = snapped;
        }
    }
    // [ORION_DEV_FIRE_OFFSET] Commanded per-shot displacement, applied at the same single choke
    // point the tick snap uses — where a deadline becomes THE scheduled fire — so it displaces
    // the final scheduled instant and nothing upstream of it. The shot reached here through every
    // normal decision/authority gate; the displaced token still lives under the same rolling
    // lease and the same submit-time invariants (a displacement the lease cannot cover aborts
    // exactly like any other uncovered deadline — fail closed, never fire-late-on-stale). The
    // applied offset is retained so re-arm drift comparisons can recover the undisplaced deadline.
    schedFireAppliedDevOffsetMs_ = 0.0;
    if (devFireOffsetArmed_
        && authority == ScheduledFireAuthority::AutonomousMeterVision
        // Calibration probes/validations measure the latency chain itself; displacing them
        // would fail their stop-vs-target check for no experimental gain. The sweep is for
        // ordinary driven shots.
        && !shot_.latencyCalibrationProbe) {
        const double offsetMs = devFireOffsetForShotMs();
        if (offsetMs != 0.0) {
            double displaced = deadlineMs + offsetMs;
            if (displaced <= now) {
                displaced = now + 0.05;   // a commanded offset may never arm a guaranteed-past deadline
            }
            schedFireAppliedDevOffsetMs_ = displaced - deadlineMs;
            emit engineDiagnostic(QStringLiteral(
                "DEV FIRE OFFSET APPLIED: base_deadline_eta_ms=%1 applied_ms=%2 "
                "shot_attempt=%3")
                                      .arg(deadlineMs - now, 0, 'f', 3)
                                      .arg(schedFireAppliedDevOffsetMs_, 0, 'f', 2)
                                      .arg(shot_.armToken));
            deadlineMs = displaced;
        }
    }
    schedFireDeadlineMs_ = deadlineMs;
    ++schedFireToken_;
    schedFireConfirmedToken_ = 0;
    schedFireFailedToken_ = 0;
    schedFireActualMs_ = -1.0;
    schedFireRequiresGenuineFrame_ = requiresGenuineFrame;
    schedFireVisionEpoch_ = requiresGenuineFrame ? shot_.visionEpoch : -1;
    schedFireRequiresPoseFrame_ = requiresPoseFrame;
    schedFirePoseEpoch_ = requiresPoseFrame ? pose_.authorityEpoch : -1;
    schedFirePoseArmToken_ = requiresPoseFrame ? pose_.acceptedArmToken : 0;
    schedFireAuthorityExpiryMs_ = authorityExpiryMs;
    schedFirePhysicalShotEpoch_ = shot_.physicalShotEpoch;
    schedFireShotAttempt_ = shot_.armToken;
    schedFireRouteGeneration_ = routeGeneration;
    schedFireRoute_ = route;
    // [ORION_ARMED_SOURCE] Every new token starts UNATTRIBUTED. The two AutonomousMeterVision
    // live-tip call sites stamp their tip decision immediately after this returns true (same
    // synchronous call, no tick can interleave); every other authority leaves it absent so a
    // prior live-tip attribution can never leak onto a feedforward/pose/fused token.
    schedFireArmedSource_.clear();
    schedFireArmedSigmaMs_ = -1.0;
    schedFireArmedFillPct_ = -1.0;
    schedFireArmedCommandEtaMs_ = -1.0;
    return true;
}

bool AutomationEngine::invalidateUnconfirmedSchedule(bool fallbackTakeover, const char* site)
{
    if (schedFireDeadlineMs_ < 0.0) {
        return false;
    }
    const quint64 token = schedFireToken_;
    const bool confirmed = schedFireConfirmedToken_ == token && schedFireActualMs_ >= 0.0;
    if (confirmed) {
        return false;
    }
    // [ORION_SILENT_FAIL] The vision wrapper attributed its kills; every direct caller of this
    // primitive killed tokens silently (sidecar restart, route re-attestation, authority-mode
    // change, calibration phase change, external cancel, ...). Emit the same TIP TOKEN KILL
    // line here so NO teardown of an armed unconfirmed token can be invisible to a shot count.
    // site == nullptr means the caller (the vision wrapper) already emitted its own attributed
    // line for this exact kill — do not double-report it.
    if (site) {
        const double untilArmedMs = schedFireDeadlineMs_ - nowMs();
        emit engineDiagnostic(QStringLiteral(
            "TIP TOKEN KILL: site=%1 until_armed_ms=%2 overdue_ms=%3 token=%4 "
            "physical_epoch=%5 shot_attempt=%6")
                                  .arg(QLatin1String(site))
                                  .arg(untilArmedMs, 0, 'f', 3)
                                  .arg(untilArmedMs < 0.0 ? -untilArmedMs : 0.0, 0, 'f', 3)
                                  .arg(token)
                                  .arg(shot_.physicalShotEpoch)
                                  .arg(shot_.armToken));
    }

    // Direct-connected in OrionAppController: this call does not return until
    // the worker's copied token is either disarmed or confirmed as submitted.
    if (fallbackTakeover) {
        emit scheduledFireFallbackInvalidating(token);
    } else {
        emit visionScheduleInvalidating(token);
    }
    const bool confirmedAfterFence = schedFireToken_ == token
        && schedFireConfirmedToken_ == token && schedFireActualMs_ >= 0.0;
    const bool failedAfterFence = schedFireToken_ == token
        && schedFireFailedToken_ == token;
    if (!confirmedAfterFence && schedFireToken_ == token) {
        clearScheduledFire();
    }
    return failedAfterFence;
}

void AutomationEngine::invalidateUnconfirmedVisionSchedule(const char* site)
{
    if (schedFireDeadlineMs_ < 0.0 || !schedFireRequiresGenuineFrame_) {
        return;
    }
    // Attribute the teardown of a LIVE token (armed, unconfirmed, deadline still ahead).
    // Every such kill converts a would-be release into an abort unless a replacement is
    // armed, so the owning call site is the single most useful fact when a shot is lost.
    const bool confirmed = schedFireConfirmedToken_ == schedFireToken_
        && schedFireActualMs_ >= 0.0;
    if (!confirmed) {
        // [ORION_INFLIGHT_TOKEN] This used to report ONLY when until_armed_ms was positive, and
        // that silence is precisely why the largest single loss in the 2026-08-04 batch could not
        // be seen: a token torn down AFTER its own deadline (while the worker was still spinning
        // toward the press) produced no line at all, so the site census looked clean while 4 of 8
        // deadline misses were dying here. Report both signs, and name the overdue case
        // explicitly so the next batch can be counted directly.
        const double untilArmedMs = schedFireDeadlineMs_ - nowMs();
        emit engineDiagnostic(QStringLiteral(
            "TIP TOKEN KILL: site=%1 until_armed_ms=%2 overdue_ms=%3 token=%4 "
            "physical_epoch=%5 shot_attempt=%6")
                                  .arg(QLatin1String(site))
                                  .arg(untilArmedMs, 0, 'f', 3)
                                  .arg(untilArmedMs < 0.0 ? -untilArmedMs : 0.0, 0, 'f', 3)
                                  .arg(schedFireToken_)
                                  .arg(shot_.physicalShotEpoch)
                                  .arg(shot_.armToken));
    }
    // nullptr: the attributed kill line for this teardown was emitted just above.
    invalidateUnconfirmedSchedule(false, nullptr);
}

bool AutomationEngine::autonomousVisionScheduleLeaseCurrent(double atMs) const noexcept
{
    // The immutable lease belongs to a validated meter frame and precise-fire
    // token, not to the controller representation that began the shot. Button,
    // Go-To, TempoSquare, and TempoStick therefore retain the same already-safe
    // deadline through a one-frame detector blink.
    return autonomousLiveMeterTimingEnabled()
        && (shot_.state == HoldState::Holding || shot_.state == HoldState::GreenWindow)
        && schedFireDeadlineMs_ >= 0.0
        && schedFirePhysicalShotEpoch_ == shot_.physicalShotEpoch
        && schedFireShotAttempt_ == shot_.armToken
        && schedFireRequiresGenuineFrame_
        && schedFireVisionEpoch_ == shot_.visionEpoch
        && schedFireAuthorityExpiryMs_ >= 0.0
        && std::isfinite(atMs)
        && atMs <= schedFireAuthorityExpiryMs_ + 1e-6
        && schedFireDeadlineMs_ <= schedFireAuthorityExpiryMs_ + 1e-6;
}

void AutomationEngine::invalidateVisionScheduleForDropout(double atMs)
{
    // The deadline was computed from a genuine frame and scheduleFire() already
    // proved that both its fire instant and the measured-lead snapshot fit inside
    // one immutable authority lease. A later absence/duplicate may not extend or
    // move it, but neither should it erase a release that is already safely due
    // inside that lease. Every other schedule is fenced synchronously.
    if (autonomousVisionScheduleLeaseCurrent(atMs)) {
        return;
    }
    invalidateUnconfirmedVisionSchedule("dropout");
}

void AutomationEngine::invalidateUnconfirmedPoseSchedule()
{
    if (schedFireDeadlineMs_ < 0.0 || !schedFireRequiresPoseFrame_) {
        return;
    }
    invalidateUnconfirmedSchedule();
}

void AutomationEngine::clearScheduledFire()
{
    schedFireDeadlineMs_ = -1.0;
    schedFireAppliedDevOffsetMs_ = 0.0;        // [ORION_DEV_FIRE_OFFSET]
    schedFireConfirmedToken_ = 0;
    schedFireFailedToken_ = 0;
    schedFireActualMs_ = -1.0;
    schedFirePhysicalShotEpoch_ = 0;
    schedFireShotAttempt_ = 0;
    schedFireRouteGeneration_ = 0;
    schedFireRoute_ = LatencyControllerRoute::None;
    schedFirePlan_.clear();
    schedFireReason_.clear();
    schedFireCode_.clear();
    schedFireRequiresGenuineFrame_ = false;
    schedFireVisionEpoch_ = -1;
    schedFireRequiresPoseFrame_ = false;
    schedFirePoseEpoch_ = -1;
    schedFirePoseArmToken_ = 0;
    schedFireAuthorityExpiryMs_ = -1.0;
    // [ORION_ARMED_SOURCE] The consume sites copy this snapshot into ShotContext BEFORE
    // calling here, so clearing is safe on the fire path and prevents a torn-down token's
    // attribution from being inherited by whatever arms next.
    schedFireArmedSource_.clear();
    schedFireArmedSigmaMs_ = -1.0;
    schedFireArmedFillPct_ = -1.0;
    schedFireArmedCommandEtaMs_ = -1.0;
    // [ORION_TRANSIENT_INVALID] No armed token means no transient to time. Resetting here keeps
    // a stale grace window from being inherited by the NEXT token this shot arms.
    tipPredictionInvalidSinceMs_ = -1.0;
    pose_.poseScheduled = false;
}

void AutomationEngine::reevaluateScheduleOnFreshSample()
{
    // Sub-tick decision-latency win (~2-4ms). A fresh meter sample just landed (updateDetection has
    // already updated fillPct / sampler_ / greenTracker_ / freshness). Re-run ONLY predictCrossingMs
    // + the fire SCHEDULE from that fresh meter and the latched lastPhysical_, WITHOUT advancing the
    // 4ms tracking-history / tempo / tap cadence (those stay owned by the inputPollTimer_ tick, which
    // is cadence-sensitive), so a fresh meter can replace an unconfirmed fire deadline in either
    // direction instead of waiting up to a full 4ms tick.
    //
    // RESCHEDULE-ONLY and bounded: it never fires in-tick and never fires a safety/abort/commit.
    // The synchronous invalidation fence permits movement only before physical submit;
    // processHolding on the 4ms tick remains authoritative for every other decision.
    if (!config_.enabled || !armed_ || !config_.subTickScheduler) {
        return;
    }
    if (shot_.state != HoldState::Holding && shot_.state != HoldState::GreenWindow) {
        return;
    }
    // Only the meter-vision predictive crossing is re-evaluated here. The feedforward clock, the
    // commit/abort/safety timers and the pose paths are elapsed-based (not fresh-sample sensitive)
    // and stay owned by the 4ms tick. No-meter mode has no meter crossing to move.
    if (config_.noMeterEnabled || !shot_.meterSeenThisShot) {
        return;
    }
    // [ORION_FUSED_FIRE] a fused-owned deadline is re-evaluated by the fused block on the 4ms
    // tick (later AND earlier moves, its own sigma math) — this earlier-only legacy mirror
    // must not touch it.
    if (config_.fusedFireEnabled && schedFireDeadlineMs_ >= 0.0
        && schedFireCode_ == QLatin1String("fused_posterior")) {
        return;
    }
    const double now = nowMs();
    // COALESCE (GUI-thread cost bound): the event-driven sidecar can deliver 60+ fresh samples/s
    // in bursts, each landing here on the GUI thread between 4ms input ticks — the same thread
    // the inputPollTimer_ needs for the user's controller pass-through. The reschedule is only a
    // sub-tick (~2-4ms) win, so run the full mirrored predict at most once per HALF input tick:
    // updateDetection has already ingested the sample either way, and the next processHolding
    // tick (which re-runs this math authoritatively) is never more than ~4ms away.
    constexpr double kFreshReevalMinGapMs = 2.0;
    if (lastFreshReevalMs_ >= 0.0 && (now - lastFreshReevalMs_) < kFreshReevalMinGapMs) {
        return;
    }
    lastFreshReevalMs_ = now;
    if (autonomousLiveMeterTimingEnabled()) {
        if (shot_.latencyCalibrationProbe) {
            return; // calibration releases only on the authoritative input tick
        }
        const bool genuineCurrent = shot_.meterDetected && shot_.lastSampleGenuineAccept
            && meterReleaseAuthorityCurrent(now);
        if (!measuredLeadAuthoritative(now) || !genuineCurrent) {
            // This mirror is called for every sidecar payload, including rejected and
            // structure-unverified frames. Preserve only a deadline already committed
            // inside its immutable frame+lead lease; this payload cannot extend, move,
            // or replace it. Anything else is synchronously fenced.
            invalidateVisionScheduleForDropout(now);
            return;
        }
        constexpr double kTipTargetPct = 100.0;
        const AutonomousTipDecision tipDecision =
            canonicalAutonomousTipDecision(now);
        if (!tipDecision.valid) {
            // [ORION_SUBTICK_PARITY] Identical policy to the 4 ms tick, via the shared helper.
            if (noteTipPredictionInvalid(now)) {
                return;
            }
            invalidateUnconfirmedVisionSchedule("subtick_prediction_invalid");
            return;
        }
        noteTipPredictionValid();
        // Both entry points consume the same registration/sampler decision. The
        // capture-aligned absolute tip already includes decoder/IPC age.
        const double effectiveLeadMs = measuredLeadForActuationMs();
        const double fireAtMs = tipDecision.tipAbsMs - effectiveLeadMs;
        shot_.targetPct = kTipTargetPct;
        shot_.targetModeAtRelease = QStringLiteral("meter_tip_%1")
            .arg(tipDecision.source);
        shot_.effectiveLatencyMs = effectiveLeadMs;
        shot_.releaseEtaMs = fireAtMs - now;
        shot_.releaseVelocityPctMs = tipDecision.velocityPctPerMs;
        shot_.releaseCrossingEtaMs = tipDecision.tipAbsMs - now;
        shot_.tipPredictionSource = tipDecision.source;
        shot_.tipPredictionSigmaMs = tipDecision.combinedSigmaMs;
        // This callback never fires. Due decisions are consumed by the next input
        // tick, but stale copied deadlines are still synchronously revoked here.
        if (fireAtMs <= now) {
            // MIRROR of the tick's fire-at-past branch: with the current estimate's fireAt
            // already behind us no replacement token can be armed, so revoking the armed
            // one can only cost the release. Keep any deadline still ahead of us; it fires
            // at its own proven-future instant, so no late command is manufactured here.
            if (schedFireDeadlineMs_ >= 0.0) {
                const double untilArmedMs = schedFireDeadlineMs_ - now;
                if (untilArmedMs > 0.0) {
                    return;
                }
                // [ORION_INFLIGHT_TOKEN] PARITY GAP, 2026-08-04 batch. The in-flight-submit hold
                // was mirrored onto the reschedule branch below but NOT onto this one, so the
                // instant `now` crossed the armed deadline this branch fenced the token with zero
                // grace while the 4 ms tick would have held it for the full schedulerGraceMs.
                // Because this mirror runs on every sidecar payload (60+/s) it reaches the token
                // first, so the tick's hold was unreachable in production for any shot whose
                // estimate had also accelerated past its own fireAt.
                //
                // Measured, the two `authority_lost_before_submit` aborts of that batch died
                // exactly here: token 142 killed 0.638 ms past its own deadline and token 137
                // killed 3.943 ms past -- both far inside the 8 ms grace the design grants, both
                // reported ever_armable=1, and both were the LAST teardown before the abort.
                //
                // Holding here creates nothing: the token fires at its own armed deadline, which
                // scheduleFire() proved future and inside its lease. This mirror has no
                // ControllerState and so cannot submit -- it only declines to destroy, and the
                // next 4 ms tick consumes the token well inside the same grace window. Once the
                // grace is spent armedTokenSubmitInFlight() goes false and we fall through to the
                // identical fence + abort below, so fail-closed is bit-for-bit unchanged.
                if (armedTokenSubmitInFlight(now)) {
                    return;
                }
            }
            invalidateUnconfirmedVisionSchedule("subtick_fire_at_past");
            return;
        }
        // [ORION_SUBTICK_PARITY] This mirror MUST refuse to revoke an imminent token for exactly
        // the same reason the 4 ms tick does (see the `irreplaceable` guard in
        // processAutonomousLiveMeterHolding). It did not, and that asymmetry was the single
        // largest remaining live loss.
        //
        // Measured, 2026-08-04 live batch (29 aborts that had ever armed a token): the LAST token
        // teardown before the abort was this site 10 times, and 7 of those 10 killed a token that
        // was already INSIDE one frame cadence of its own deadline -- the closest at 0.7 ms. The
        // sidecar delivers samples event-driven at 60+/s, so this mirror runs BETWEEN ticks and
        // reached those tokens first; the tick's guard never got the chance to protect them.
        // Re-arming 0.7 ms out cannot succeed: the worker needs lead time to submit, so the
        // deadline passes unsubmitted and the shot is lost with single-digit lateness (observed
        // 0.2 / 1.5 / 2.5 ms).
        //
        // This changes WHETHER we revoke, never WHERE we fire: the surviving token fires at its
        // own armed deadline, which scheduleFire() proved future and which the rolling lease,
        // vision-epoch and uncovered-at-commit fences re-check every tick. No later command is
        // created and no overtime packet can be emitted here.
        // [ORION_SUBTICK_PARITY] The tolerance, the imminent-token guard AND the in-flight-submit
        // guard are all shared with the 4 ms tick. This mirror runs on every sidecar payload, so
        // it reaches an armed token FIRST and any asymmetry here silently becomes the dominant
        // teardown site (which is exactly how it behaved before the imminent guard was mirrored).
        //
        // [ORION_SLOW_METER_DEFER] MIRROR of the tick's two defer sites, for exactly the
        // [ORION_SUBTICK_PARITY] reason above: this mirror reaches the token first, so without
        // the mirror the tick's refusal would be unreachable in production (the seq=52 arm ran
        // 8ms after its SCHEDULE FIRE REJECT, i.e. on the very next evaluation). ownedHold is
        // recomputed with the tick's own definition. Both uses are refusals: keep the armed
        // later token / decline to create an undercutting one. Declining here can never skip a
        // release -- this mirror is reschedule-only and the 4 ms tick remains authoritative.
        const bool ownedHold = shot_.mode == ShotMode::ButtonShot
            || shot_.mode == ShotMode::TempoSquare
            || (config_.tempoTipParity && shot_.mode == ShotMode::TempoStick)
            || (config_.gotoTipParity && shot_.mode == ShotMode::GoToStick);
        const bool deferUndercuttingCandidate = ownedHold
            && slowMeterDeferBinds(tipDecision, fireAtMs, effectiveLeadMs, now);
        // [ORION_DEV_FIRE_OFFSET] undisplaced comparison (offset 0 when disarmed).
        const double armedBaseMs = schedFireDeadlineMs_ - schedFireAppliedDevOffsetMs_;
        if (schedFireDeadlineMs_ >= 0.0
            && (std::abs(fireAtMs - armedBaseMs) <= tokenDeadlineDriftToleranceMs()
                || armedTokenIrreplaceable(now)
                || armedTokenSubmitInFlight(now)
                || (deferUndercuttingCandidate
                    && fireAtMs + 1e-6 < armedBaseMs))) {
            return;
        }
        if (deferUndercuttingCandidate) {
            // No arm and no teardown: an existing token (necessarily at/after the candidate
            // here) stays armed untouched; with none, the tick decides hold-vs-arm within 4ms.
            return;
        }
        invalidateUnconfirmedVisionSchedule("subtick_reschedule");
        if (schedFireDeadlineMs_ >= 0.0) {
            return; // invalidation raced a confirmed physical submit
        }
        if (scheduleFire(fireAtMs, now, -1.0,
                         ScheduledFireAuthority::AutonomousMeterVision)) {
            schedFirePlan_ = QStringLiteral("Live meter tip");
            schedFireReason_ = QStringLiteral("live_meter_tip");
            schedFireCode_ = QStringLiteral("live_meter_tip");
            // [ORION_ARMED_SOURCE] Subtick mirror of the tick arm site's attribution
            // snapshot: this re-arm's decision is what the fired token will carry.
            schedFireArmedSource_ = tipDecision.source.left(64);
            schedFireArmedSigmaMs_ = tipDecision.combinedSigmaMs;
            schedFireArmedFillPct_ = shot_.fillPct;
            schedFireArmedCommandEtaMs_ = fireAtMs - now;
            shot_.releasePlan = QStringLiteral("Live meter tip scheduled");
            shot_.releaseReason = QStringLiteral("release_scheduled");
            shot_.releaseReasonCode = QStringLiteral("release_scheduled");
            // [ORION_SILENT_FAIL] MIRROR of the tick arm site's promotion emit. This subtick
            // mirror is the DOMINANT arm path live (it reaches a killed token first on every
            // sidecar payload), yet it never marked the reservation promoted -- so half the
            // released shots had no reservation_promoted line and were invisible to the
            // (physical_epoch, shot_attempt) promotion join. Same fields as the tick site;
            // arm_site=subtick is APPEND-ONLY (key=value parsers ignore unknown keys) and
            // distinguishes the two arm sites without corrupting source=, which must remain
            // the PREDICTOR the decision came from (it is what armed_source joins against).
            if (tipReservation_.active && !tipReservation_.promoted) {
                tipReservation_.promoted = true;
                emit engineDiagnostic(QStringLiteral(
                    "TIP RESERVATION: disposition=reservation_promoted source=%1 "
                    "command_eta_ms=%2 lead_kind=%3 lead_ms=%4 predictor_sigma_ms=%5 "
                    "fill_pct=%6 physical_epoch=%7 shot_attempt=%8 schedule_token=%9 "
                    "reservation_age_ms=%10 reservation_updates=%11 first_fill=%12 "
                    "arm_site=subtick")
                                          .arg(tipDecision.source.left(64))
                                          .arg(fireAtMs - now, 0, 'f', 3)
                                          .arg(measuredLatencyAuthorityKind_.left(16))
                                          .arg(effectiveLeadMs, 0, 'f', 3)
                                          .arg(tipDecision.combinedSigmaMs, 0, 'f', 3)
                                          .arg(shot_.fillPct, 0, 'f', 2)
                                          .arg(tipReservation_.physicalShotEpoch)
                                          .arg(tipReservation_.armToken)
                                          .arg(schedFireToken_)
                                          .arg(tipReservation_.createdMs >= 0.0
                                                   ? now - tipReservation_.createdMs : -1.0,
                                               0, 'f', 3)
                                          .arg(tipReservation_.updates)
                                          .arg(tipReservation_.firstFillPct, 0, 'f', 2));
            }
        }
        return;
    }
    // A confirmed / grace-expired scheduled fire is committed — let the tick consume it.
    if (schedFireDeadlineMs_ >= 0.0) {
        const bool confirmed = schedFireConfirmedToken_ == schedFireToken_ && schedFireActualMs_ >= 0.0;
        const bool rawFeedforwardSchedule =
            schedFirePlan_ == QLatin1String("Feedforward clock");
        if (confirmed || (!rawFeedforwardSchedule
                          && now >= schedFireDeadlineMs_ + config_.schedulerGraceMs)) {
            return;
        }
    }
    const bool isGoto = shot_.mode == ShotMode::GoToStick;
    const double elapsed = shot_.holdStartMs > 0.0 ? now - shot_.holdStartMs : 0.0;
    // Same commit floor the tick enforces — a shot before commit must not schedule a fire.
    double commitMinMs = config_.standstillCommitMinMs;
    if (isGoto) {
        commitMinMs = config_.gotoCommitMinMs;
    } else if (shot_.shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive)) {
        commitMinMs = config_.fadeCommitMinMs;
    } else if (shot_.shotType == QLatin1String("Moving")) {
        commitMinMs = config_.movingCommitMinMs;
    }
    if (elapsed < commitMinMs || elapsed < config_.minHoldMs) {
        return;
    }
    const double freshWindowMs = config_.meterFreshWindowMs
        * (wifiMode_ ? config_.wifiFreshnessFactor : 1.0);
    const bool meterFresh = shot_.meterDetected && shot_.lastDetectionMs > 0.0
        && (now - shot_.lastDetectionMs) <= freshWindowMs;
    if (!meterFresh || !meterReleaseAuthorityCurrent(now)) {
        return;
    }
    // STALENESS SIBLING (2026-07-24) — MIRROR the release blocks' lastSampleFreshAccept gate.
    // This entry point runs on EVERY sidecar detection, including a stale_or_memory ECHO (the
    // caller does not filter), and memory extrapolation advances shot_.fillPct by
    // velocity*occlusion on those frames. Without this gate a stream of echoes could walk an
    // invented fill into reach here and ARM the precise-fire thread on a value the detector
    // never observed — a fire the 4ms tick then simply consumes. Reschedule-only, so returning
    // early can never skip a release: any already-armed deadline stays armed and processHolding
    // remains authoritative.
    if (!shot_.lastSampleGenuineAccept) {
        return;
    }
    // [ORION_GOTO_FIRE] Go-To is treated like every other shot type now: it flows through the
    // same autonomous tip-vision reshape (was gated out with `&& !isGoto`). It may fire (accepting
    // some late/mistimed Go-To until its per-type clock re-calibrates) instead of aborting.
    const bool globalApplies = config_.autonomousVision;
    // Target — MIRROR processHolding.
    double target;
    const bool greenConfirmed = greenTracker_.confirmed();
    const bool isFade = shot_.shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive);
    // [ORION_FADE_GREEN_ENTRY] — MIRROR processHolding, and it must come FIRST (ahead of
    // globalApplies) exactly as it does there. TARGET DIVERGENCE (2026-07-24): this mirror had
    // no fade branch at all, so for a confirmed-green fade the immediate (tick) fire aimed the
    // green window's ENTRY while the SCHEDULED fire aimed the tip (100) — two different points
    // for the most timing-sensitive shot class, and which one you got depended only on whether
    // a fresh sample happened to land between ticks.
    if (isFade && greenConfirmed && greenTracker_.startPct() >= 0.0) {
        target = clampPct(greenTracker_.startPct() + config_.learningBiasPct);
    } else if (globalApplies) {
        target = clampPct(config_.fullMeterTargetPct + config_.learningBiasPct);
    } else if (greenConfirmed) {
        const double margin = isFade ? config_.tipMarginFadePct : config_.meterTipMarginPct;
        target = greenTracker_.adaptiveTargetPct(margin, config_.learningBiasPct);
    } else {
        target = clampPct(config_.fullMeterTargetPct + config_.learningBiasPct);
    }
    target = clampPct(target + seededValue(config_.shotTypeTargetOffsetPct,
                                           shot_.bucketKey, shot_.shotType));   // D3 fallback
    // [ORION_PLATEAU_AIM] — MIRROR processHolding: confirmed green -> aim the apex.
    const bool plateauAim = config_.plateauAimEnabled && greenConfirmed;
    if (plateauAim) {
        target = clampPct(100.0);
    }
    // effectiveLatency — MIRROR processHolding (incl. [ORION_MEASURED_LEAD]).
    // [ORION_MEASURED_LEAD] Engagement is gated by measuredLeadAuthoritative: either a fully
    // converged oracle or a distinct controlled planned-stop validation, plus the
    // one-shot clock re-baseline having run (updateDetection fires it at the same threshold):
    // flipping the lead base from ~6ms to ~75ms without shifting the EMA-dialed clocks would
    // fire every path ~69ms early for the 15-30 shots the EMAs need to re-converge (Part-0).
    const bool useMeasuredLead = measuredLeadAuthoritative(now);
    const double effectiveLatency =
          // [ORION_MEASURED_LEAD] — MIRROR processHolding (bug #6): the measured round-trip already
          // contains the network leg; never re-add networkOffsetMs on the measured-lead path.
          // SIGNED (2026-07-24) — MIRROR processHolding: abs() inverted a negative manual sync trim.
          (useMeasuredLead ? 0.0 : shot_.networkOffsetMs)
        + config_.earlyLateOffsetMs
        + (config_.noDipEnabled ? config_.noDipLeadMs : 0.0)
        + (useMeasuredLead
             // MIRROR processHolding: Button and Tempo share the per-type residual.
             ? measuredLatencyMs_ + seededValue(config_.shotTypeLatencyMs, shot_.bucketKey, shot_.shotType)
             : (globalApplies
                  // MIRROR processHolding: the modelled press->console chain is part of the autonomous
                  // lead too. learnedLatencyMs alone is a residual that the (frozen) grader can never
                  // dial, so on its own it left the whole output path uncompensated -> releases late.
                  ? config_.learnedLatencyMs + seededValue(config_.shotTypeLatencyMs, shot_.bucketKey, shot_.shotType)
                  : config_.controllerChainMs + config_.remotePlayPipelineMs + shotTypeOffsetMs(shot_.bucketKey)))
        + std::clamp(shot_.frameAgeMs, 0.0, config_.captureAgeLeadCapMs);
    // Velocity (banded) + crossing — MIRROR processHolding.
    double velocityPctPerMs = sampler_.velocityPctPerMs();
    {
        const double prior = seededValue(config_.shotTypeVelocityPriorPctMs, shot_.bucketKey, shot_.shotType);
        if (prior > 0.0 && velocityPctPerMs > 0.0) {
            velocityPctPerMs = std::clamp(velocityPctPerMs,
                                          prior * config_.velocityPriorLoFactor,
                                          prior * config_.velocityPriorHiFactor);
        }
    }
    double crossing = sampler_.predictCrossingMs(target);
    // [ORION_REG_FUSION] Far-horizon handoff — MIRROR processHolding.
    if (config_.regFusionEnabled && shot_.regConf >= config_.regFusionMinConf && shot_.regTipMs > 0.0) {
        const bool farHorizon = shot_.regTipMs >= config_.regFusionFarHorizonMs
            || shot_.fillPct < config_.regFusionNearFillPct;
        if (farHorizon) {
            crossing = now + shot_.regTipMs;
        }
    }
    // [ORION_TEMPLATE_ARRIVAL] — MIRROR processHolding (template prior blended into the crossing).
    if (config_.templateArrivalEnabled && templateArrival_.matched()) {
        const double tmplArrival = templateArrival_.predictArrivalMs(target);
        if (tmplArrival > now) {
            if (crossing > 0.0) {
                const double vt = config_.templateArrivalSdMs * config_.templateArrivalSdMs;
                const double vl = config_.templateLiveSdMs * config_.templateLiveSdMs;
                const double w = vl / (vl + vt);
                crossing = w * tmplArrival + (1.0 - w) * crossing;
            } else {
                crossing = tmplArrival;
            }
        }
    }
    // [ORION_PLATEAU_AIM] — MIRROR processHolding (+½·capHold into the plateau).
    if (plateauAim && crossing > 0.0) {
        crossing += 0.5 * config_.plateauCapHoldMs;
    }
    const bool healthyForwardCrossing = config_.tipGateEnabled
        && healthyForwardCrossingOwnsTiming(
            shot_.lastSampleGenuineAccept && meterReleaseAuthorityCurrent(now),
            shot_.countFreshAcceptsWithin(now, 150.0),
            velocityPctPerMs,
            shot_.fillPct,
            target,
            crossing,
            now,
            effectiveLatency,
            config_.tipGateCapMs);
    if (healthyForwardCrossing && schedFireDeadlineMs_ >= 0.0
        && schedFirePlan_ == QLatin1String("Feedforward clock")) {
        // A fresh rising sample supersedes a copied raw-clock token even when
        // the vision deadline is later than that token or already due. The
        // serialized invalidation fence either disarms it or observes that the
        // physical edge already won; it can never silently double-fire.
        invalidateUnconfirmedVisionSchedule("subtick_guard");
    }
    // Reachability + fire-near-tip floor — MIRROR processHolding.
    // [ORION_GOTO_FIRE] Go-To now uses the non-Go-To predictive reach clamp (was 25).
    const double maxRisePct = config_.nonGotoMaxPredictiveRisePct;
    const double expectedRisePct = std::clamp(velocityPctPerMs * effectiveLatency, 0.0, maxRisePct);
    // C1 lead-aware floor + A3 reachability horizon — MIRROR processHolding EXACTLY (see the long
    // rationale there). Any divergence here would let the mirror schedule a fire the tick would not
    // (or vice versa), which is precisely the bug class this mirror exists to avoid.
    const double baseFillFloor = globalApplies ? config_.tipFireMinFillPct
                                               : config_.nonGotoMinPredictiveFillPct;
    const double leadImpliedFloor = (velocityPctPerMs > 0.0 && effectiveLatency > 0.0)
        ? (target - velocityPctPerMs * effectiveLatency - config_.tipFireLeadSlackPct)
        : baseFillFloor;
    const double minPredictiveFillFloor =
        std::max(config_.tipFireAbsMinFillPct, std::min(baseFillFloor, leadImpliedFloor));
    const bool abovePredictiveFloor = isGoto || shot_.fillPct >= minPredictiveFillFloor;
    const double crossingEtaMs = (crossing > 0.0) ? (crossing - now) : -1.0;
    const double horizonRisePct = (crossingEtaMs > 0.0 && velocityPctPerMs > 0.0)
        ? std::clamp(velocityPctPerMs * std::min(crossingEtaMs, config_.reachabilityCrossingHorizonMs),
                     0.0, config_.reachabilityMaxRisePct)
        : 0.0;
    const double reachAllowancePct =
        std::max(expectedRisePct, horizonRisePct) + config_.reachabilitySlackPct;
    const bool withinReach = (target - shot_.fillPct) <= reachAllowancePct && abovePredictiveFloor;
    if (!withinReach) {
        return;
    }
    // Earliest vision fire deadline (green crossing and/or velocity crossing) — MIRROR blocks 1 & 2.
    const double predictiveConfFloor = (networkJitterEmaMs_ > config_.jitterFloorTrigger)
        ? config_.jitterPredictiveFloor : config_.confidenceGate;
    double fireAtMs = -1.0;
    QString code, plan;
    if (greenConfirmed && crossing > 0.0) {
        fireAtMs = crossing - effectiveLatency;
        code = QStringLiteral("green_confirmed");
        plan = QStringLiteral("Green window");
    }
    if (velocityPctPerMs > 0.001 && shot_.fillPct < target && shot_.confidence >= predictiveConfFloor) {
        double msUntilTarget = (target - shot_.fillPct) / velocityPctPerMs;
        if (shot_.fillPct > config_.decelKneePct) {
            msUntilTarget -= (target - shot_.fillPct) * config_.decelCorrectionFactor / velocityPctPerMs;
            msUntilTarget = std::max(0.0, msUntilTarget);
        }
        // Sample-age rebase — MIRROR processHolding block 2. Near-zero here (this path only runs
        // on a just-arrived fresh sample), but keeping it identical stops the mirror from ever
        // computing a LATER deadline than the tick would.
        msUntilTarget = std::max(0.0, msUntilTarget - std::max(0.0, now - shot_.lastDetectionMs));
        const double velFire = now + msUntilTarget - effectiveLatency;
        if (fireAtMs < 0.0 || velFire < fireAtMs) {
            fireAtMs = velFire;
            code = QStringLiteral("predictive_target");
            plan = QStringLiteral("Trajectory tip");
        }
    }
    if (fireAtMs < 0.0) {
        return;
    }
    // [ORION_GOTO_FIRE] The Go-To freshness trust gate here (mirror of processHolding's, now removed)
    // is dropped too — Go-To reschedules a vision fire like every other type. The predictive/green
    // deadline that reaches this point is already fresh-gated (the crossing came from fresh samples),
    // so phantom protection is preserved without a Go-To-specific fill/trust fence.
    // Reschedule-only: the deadline must be a schedulable FUTURE deadline within the horizon and past
    // the min-hold floor (the same gates scheduleFire enforces). An already-due crossing (fireAtMs <=
    // now) is left for the next 4ms tick to fire in-tick — we never fire here.
    const bool schedulable = fireAtMs > now
        && (fireAtMs - now) <= config_.schedulerHorizonMs
        && (shot_.holdStartMs <= 0.0 || (fireAtMs - shot_.holdStartMs) >= config_.minHoldMs);
    if (!schedulable) {
        return;
    }
    // Only MOVE an already-armed deadline EARLIER (never later — a committed fire must not slip).
    if (schedFireDeadlineMs_ >= 0.0 && fireAtMs >= schedFireDeadlineMs_ - 1e-6) {
        return;
    }
    invalidateUnconfirmedVisionSchedule("arm_reset");
    if (scheduleFire(fireAtMs, now)) {
        schedFirePlan_ = plan;
        schedFireReason_ = QStringLiteral("release_scheduled");
        schedFireCode_ = code;
        shot_.releasePlan = QStringLiteral("Scheduled fire");
        shot_.releaseReason = QStringLiteral("release_scheduled");
        shot_.releaseReasonCode = QStringLiteral("release_scheduled");
        // Keep the telemetry snapshot current for a scheduler-driven fire between ticks.
        shot_.effectiveLatencyMs = effectiveLatency;
        shot_.targetPct = target;
        shot_.releaseEtaMs = fireAtMs - now;
        shot_.releaseVelocityPctMs = velocityPctPerMs;
    }
}

void AutomationEngine::setShotVerdict(const QString& verdict, double signedResidualMs, double confidence)
{
    // The feedback-text oracle is authoritative: scope the flag so this one verdict may teach even
    // under freeze (calibrationMode), then restore. The meter self-grade path never sets this.
    authoritativeVerdict_ = true;
    updateShotOutcome(verdict, signedResidualMs, confidence);
    authoritativeVerdict_ = false;
}

double AutomationEngine::tickAlignedFireDeadlineMs(double deadlineMs, double nowMs,
                                                   double nextTickEtaMs, double tickIntervalMs)
{
    // [ORION_TICK_LOCK] Nudge the absolute fire deadline so the release ARRIVES mid-tick on the 60Hz
    // console grid — maximally far from the ±tickIntervalMs/2 quantization edges (turns the 0-16.7ms
    // in-tick scatter into a centered landing). The next console tick edge is at nowMs + nextTickEtaMs;
    // edges repeat every tickIntervalMs. We want the deadline's phase relative to that grid to be
    // tickIntervalMs/2 (mid-tick). adj = T/2 - phase is already the smallest-magnitude nudge in
    // (-T/2, +T/2], so a committed fire never slips more than half a tick. Telemetry-not-ready (a
    // non-positive interval/eta, or a non-finite deadline) returns the deadline unchanged.
    if (!(tickIntervalMs > 0.0) || !std::isfinite(nextTickEtaMs) || nextTickEtaMs <= 0.0
            || !std::isfinite(deadlineMs) || !std::isfinite(nowMs)) {
        return deadlineMs;
    }
    const double tickEdge0 = nowMs + nextTickEtaMs;
    const double rel = deadlineMs - tickEdge0;
    const double phaseFromEdge = rel - std::floor(rel / tickIntervalMs) * tickIntervalMs;   // [0, T)
    const double adj = (tickIntervalMs * 0.5) - phaseFromEdge;                               // (-T/2, T/2]
    return deadlineMs + adj;
}

double AutomationEngine::earlierOnlyTickSnapMs(double tFireMs, double edgePhaseEngineMs,
                                               double epsilonMs, double tickIntervalMs)
{
    // [Phase-2 A2(c)] EARLIER-ONLY snap: floor tFire to the latest instant whose press submit
    // sits at engine phase (edge - epsilon) mod P, so the press ARRIVES epsilon before an
    // input-tick edge instead of scattering uniformly across the 0-16.7ms sample wait. A slip
    // past the edge costs a FULL tick, so the snap NEVER moves later than the unsnapped
    // deadline (contrast tickAlignedFireDeadlineMs, which centers mid-tick and can move
    // +8.3ms late — kept for the legacy path). Max earlier shift < one tick interval.
    if (!(tickIntervalMs > 0.0) || !std::isfinite(tFireMs)
        || !std::isfinite(edgePhaseEngineMs) || !std::isfinite(epsilonMs)) {
        return tFireMs;
    }
    const double target = edgePhaseEngineMs - epsilonMs;      // desired press phase (mod P)
    double delta = std::fmod(tFireMs - target, tickIntervalMs);
    if (delta < 0.0) {
        delta += tickIntervalMs;                              // [0, P)
    }
    return tFireMs - delta;                                   // latest t <= tFire on the phase
}

bool AutomationEngine::tickSnapEngaged(double conf, double sdMs, double minConf, double maxSdMs)
{
    // [Phase-2 A2(c)] engage only on trustworthy probe-run phase telemetry: conf >= minConf
    // (the sidecar HALVES conf per 10 minutes since the probe run, so a stale phase disengages
    // itself) AND a demonstrated finite sd in (0, maxSd]. Absent telemetry (conf 0 / sd -1)
    // always disengages -> the fused deadline is byte-identical to the pre-snap build.
    return std::isfinite(conf) && std::isfinite(sdMs)
        && conf >= minConf && sdMs > 0.0 && sdMs <= maxSdMs;
}

double AutomationEngine::blendedFireDeadlineMs(double clockDueMs, double visionFireAtMs,
                                               bool visionUsable, double confidence,
                                               double priorSdMs, double visionBaseSdMs,
                                               double visionMaxSdMs)
{
    // No usable vision posterior -> the fuse is the pure clock (never fires earlier/blind than the clock).
    if (!visionUsable || !std::isfinite(visionFireAtMs)) {
        return clockDueMs;
    }
    const double q = std::clamp(confidence, 0.0, 1.0);
    // Vision std-dev interpolates base (full confidence) -> max (confidence 0). At q==0 the vision
    // variance is so large its weight ~0 -> the fuse is the pure clock; at q==1 it is visionBaseSd, so a
    // small base converges the fuse onto the vision crossing.
    const double visionSd = visionBaseSdMs + (visionMaxSdMs - visionBaseSdMs) * (1.0 - q);
    const double priorVar = std::max(1e-6, priorSdMs * priorSdMs);
    const double visionVar = std::max(1e-6, visionSd * visionSd);
    const double wPrior = 1.0 / priorVar;
    const double wVision = 1.0 / visionVar;
    return (clockDueMs * wPrior + visionFireAtMs * wVision) / (wPrior + wVision);
}

bool AutomationEngine::isArtifactOutcomeSignature() const
{
    // RC-4(c): detect the dead-top meter self-grade's two-mode fixed-value histogram (alternating
    // LATE +66 / EARLY -90). A genuine converging grade stream is spread across MANY magnitudes that
    // trend toward green; the artifact collapses onto exactly two fixed, near-zero-variance values that
    // never approach green — a fingerprint the same-sign streak guard misses (the signs alternate).
    const int n = static_cast<int>(autoLeadRecentErr_.size());
    if (n < kAutoLeadArtifactMinShots) {
        return false;
    }
    // Bucket by rounding to the nearest 3ms (absorbs minor grader jitter around the fixed magnitudes).
    std::array<std::pair<long, int>, kAutoLeadArtifactWindow> buckets{};
    int nBuckets = 0;
    for (double e : autoLeadRecentErr_) {
        const long key = std::lround(e / 3.0);
        int j = 0;
        for (; j < nBuckets; ++j) {
            if (buckets[j].first == key) { buckets[j].second += 1; break; }
        }
        if (j == nBuckets && nBuckets < kAutoLeadArtifactWindow) {
            buckets[nBuckets++] = {key, 1};
        }
    }
    int modeCount = 0;       // distinct values that repeat (>=2 members)
    int coveredByModes = 0;  // how much of the window those modes cover
    for (int j = 0; j < nBuckets; ++j) {
        if (buckets[j].second >= 2) { ++modeCount; coveredByModes += buckets[j].second; }
    }
    // Artifact: EXACTLY two repeating fixed-value modes covering >=75% of the window, with <=3 distinct
    // values total (near-zero spread). Two-mode only — a single constant same-sign stream is already
    // handled by the divergence streak guard, and a varied converging stream has many distinct buckets.
    return modeCount == 2 && coveredByModes * 4 >= n * 3 && nBuckets <= 3;
}

void AutomationEngine::learnFromOutcome(const QString& shotType, double errorMs)
{
    if (autonomousLiveMeterTimingEnabled()) {
        return; // outcomes remain telemetry; they cannot change live-only firing state
    }
    // [ORION_FUSED_FIRE] LATE-66 backdoor freeze: under fused authority the fire deadline is
    // mu_tip - aim - MEASURED lead — the frozen self-grade must not re-enter through ANY of
    // the legacy learned components (learnedLatencyMs, per-type offsets/clocks, bias). The
    // fused stack's only teachers are the frozen-meter oracle (lead) and the post-hoc
    // registration labels (anchor clocks); grading v2 (Phase 2) becomes the sole trim writer.
    if (config_.fusedFireEnabled) {
        return;
    }
    // [ORION_GRADE_V2] sole-writer arbitration: under grading v2 the trim routes DIRECTLY to
    // shotTypeLearnedOffsetMs inside evaluatePostReleaseMeterV2 — the legacy ACQUIRE->LOCK
    // controller (and every path that funnels here: self-grade, banner fallback, injected
    // verdicts) must NOT also fire for the same outcome, or two writers fight over one knob.
    if (config_.gradeV2Enabled) {
        return;
    }
    // autonomous_vision (Phase C): ONE global latency correction, dialed type-agnostically by the
    // post-release grade INSTEAD of the per-type ACQUIRE->LOCK below. LATE (errorMs>0) raises the lead
    // so the next shot fires earlier; EARLY (<0) lowers it. Bounded + slow (globalLatencyGain/ClampMs).
    // The stable appear->tip clock is unaffected, so this corrects bias without oscillating, and the
    // per-type maps stay frozen (autonomous mode never touches them).
    //
    // RC-4: the global lead is FROZEN against the unreliable dead-top meter self-grade and only moves on
    // an independent-oracle grade. See the three gates below. (Previously this branch ran BEFORE the
    // calibrationFrozen gate and integrated the self-grade directly — the LATE-66 leak.)
    if (config_.autonomousVision) {
        // [ORION_LEAD_VISION_GATE] Distrust a blind / low-vision-confidence outcome entirely: it neither
        // moves the lead NOR updates any diagnostic streak. (Kept; hardening below is additional.)
        if (config_.leadLearnerVisionGate && !lastReleaseVisionConfident_) {
            return;
        }
        // Divergence streak + artifact histogram: DIAGNOSTICS — updated for EVERY outcome that reaches
        // here (reliable or not) so the freeze machinery has a live view, but they never by themselves
        // move the lead. A GREEN converges -> reset the streak; a non-green feeds the streak + histogram.
        if (std::isfinite(errorMs) && std::abs(errorMs) <= 0.5) {
            autoLeadDivergeStreak_ = 0;
            autoLeadDivergeSign_ = 0;
        } else if (std::isfinite(errorMs)) {
            autoLeadRecentErr_.push_back(errorMs);
            while (static_cast<int>(autoLeadRecentErr_.size()) > kAutoLeadArtifactWindow) {
                autoLeadRecentErr_.pop_front();
            }
            const int errSign = errorMs > 0.0 ? 1 : -1;
            if (autoLeadDivergeStreak_ > 0 && errSign == autoLeadDivergeSign_) {
                autoLeadDivergeStreak_ += 1;
            } else {
                autoLeadDivergeStreak_ = 1;
                autoLeadDivergeSign_ = errSign;
            }
            const bool diverging = autoLeadDivergeStreak_ > config_.calDivergenceGuardShots;
            // RC-4(c) ARTIFACT: a two-mode fixed-value histogram (alternating LATE-66/EARLY-90) is a
            // grader artifact, not a real bias, and the same-sign streak above never catches it. Freeze
            // regardless of sign.
            const bool artifact = isArtifactOutcomeSignature();
            if (diverging) {
                emit fusedDiagnostic(QStringLiteral("Divergence guard: streak=%1 sign=%2 shotType=%3 — learning frozen for this type")
                                     .arg(autoLeadDivergeStreak_)
                                     .arg(autoLeadDivergeSign_)
                                     .arg(shotType));
            }
            if (artifact) {
                emit fusedDiagnostic(QStringLiteral("Artifact detection: two-mode histogram signature shotType=%1 — learning frozen (grader artifact)")
                                     .arg(shotType));
            }
            // RC-4(a) FREEZE + RC-4(b) RELIABILITY: the calibrationFrozen gate now sits ABOVE the
            // global-lead mutation (so freeze actually freezes the global lead), and only an
            // INDEPENDENT-ORACLE grade may move it. authoritativeVerdict_ = the reliable HUD feedback-text
            // oracle; the RC-3 measured-latency oracle drives the lead via measuredLatencyMs_ directly
            // (never through here). The unreliable dead-top meter SELF-grade (authoritativeVerdict_==false)
            // is DIAGNOSTIC-ONLY: it fed the streak/histogram above but must NOT move the lead. This is
            // the LATE-66 leak fix — a confident-vision release with a FALSE LATE-66 self-grade previously
            // walked past the vision gate (which keys on RELEASE confidence, not GRADE reliability).
            const bool frozenByCal = config_.calibrationFrozen
                && !(config_.calibrationMode && authoritativeVerdict_);
            const bool leadMayMove = authoritativeVerdict_ && !frozenByCal && !diverging && !artifact;
            if (leadMayMove) {
                // Global latency correction (common bias across all types)
                config_.learnedLatencyMs = std::clamp(
                    config_.learnedLatencyMs + config_.globalLatencyGain * errorMs,
                    -config_.globalLatencyClampMs, config_.globalLatencyClampMs);
                // Per-shot-type residual (type-specific correction on top of global)
                // Slow gain, tight clamp — this is a residual, not a primary correction.
                if (!shotType.isEmpty()) {
                    double& typeLat = config_.shotTypeLatencyMs[shotType];
                    typeLat = std::clamp(
                        typeLat + config_.shotTypeLatencyGain * errorMs,
                        -config_.shotTypeLatencyClampMs, config_.shotTypeLatencyClampMs);
                    // Item 7: Per-type target offset learning. errorMs > 0 = LATE -> raise target
                    // (aim higher so we fire earlier); errorMs < 0 = EARLY -> lower target.
                    // Very slow gain, tight clamp — target is sensitive.
                    double& typeTarget = config_.shotTypeTargetOffsetPct[shotType];
                    const double targetCorrection = -config_.targetOffsetGain * errorMs / config_.meterMsPerPct;
                    typeTarget = std::clamp(typeTarget + targetCorrection,
                        -config_.targetOffsetClampPct, config_.targetOffsetClampPct);
                }
                // [TIMING FINDING 1 / ORION_FADE_CLOCK] Close the fade feedforward loop. Fades are
                // carved OUT of the global clock on the FIRE side (the isFade fence at the
                // clock-selection site): they fire on the per-type shotTypeMeterToReleaseMs /
                // shotTypeFeedforwardMs clocks and lead by shotTypeOffsetMs — NONE of which the
                // global learnedLatencyMs / shotTypeLatencyMs corrections above ever reach. So a
                // fade's fire deadline never sees the grader and any seed bias is PERMANENT. Route
                // the SAME gated, reliable outcome error into the fade's per-type clocks (the knobs
                // that actually drive its fire), mirroring the non-autonomous per-type clock nudge:
                // LATE (errorMs>0) shortens the clock -> the next fade fires earlier; EARLY lengthens.
                // Conservative: only fade buckets are touched (every non-fade type already closes its
                // loop through learnedLatencyMs above, so their behavior is unchanged); only an
                // already-armed clock (> its floor) is nudged; a slow bounded gain (globalClockGain)
                // with the SAME [floor, 5000] clamps as the working per-type path; and the per-type
                // OFFSET is deliberately left alone so one outcome moves the fade deadline by exactly
                // ONE step (no double-count, cf. Finding 3). Gated by leadMayMove, so fades track the
                // exact same correction regime as the global lead (both frozen when the grader is
                // frozen, both move on a reliable oracle) — the fire loop and the learn loop are now
                // the same set.
                if (!shotType.isEmpty()
                        && shotType.contains(QStringLiteral("Fade"), Qt::CaseInsensitive)) {
                    const double fadeStep = config_.globalClockGain * errorMs;
                    if (config_.shotTypeFeedforwardMs.contains(shotType)) {
                        double& clk = config_.shotTypeFeedforwardMs[shotType];
                        if (clk > config_.feedforwardMinMs) {
                            clk = std::clamp(clk - fadeStep, config_.feedforwardMinMs, 5000.0);
                            emit feedforwardUpdated(config_.shotTypeFeedforwardMs);
                        }
                    }
                    if (config_.shotTypeMeterToReleaseMs.contains(shotType)) {
                        double& clk = config_.shotTypeMeterToReleaseMs[shotType];
                        if (clk > config_.meterClockMinMs) {
                            clk = std::clamp(clk - fadeStep, config_.meterClockMinMs, 5000.0);
                            emit meterClockUpdated(config_.shotTypeMeterToReleaseMs);
                        }
                    }
                }
                emit globalTimingLearned(config_.globalAppearToTipMs, config_.globalHoldToReleaseMs,
                                         config_.learnedLatencyMs, config_.globalRiseVelocityPctMs);
            }
        }
        return;
    }
    if (config_.calibrationFrozen && !(config_.calibrationMode && authoritativeVerdict_)) {
        // Banner-calibrate workflow: self-learning frozen -> hold the per-type clock/offset at their
        // learning.json values (the meter grader can't tell green from late at the dead-top and would
        // creep the clock early). The verdict was still emitted for telemetry; we just don't act on it.
        // EXCEPTION: in calibrationMode the reliable feedback-text oracle (authoritativeVerdict_) is
        // allowed through — it IS reliable at the dead-top, so it actively dials the offset.
        return;
    }
    if (shotType.isEmpty() || !std::isfinite(errorMs)) {
        return;
    }
    // === Per-type ACQUIRE -> LOCK controller (the "find the offset, then minuscule
    // adjustments" model) ===
    // A verdict is "green" (EXCELLENT, inside the green deadband) when the post-release meter
    // evaluator reports ~0 residual; any non-trivial signed residual is a "miss" (EARLY/LATE).
    //  - ACQUIRE: converge FAST with the full annealed integral step.
    //  - after calLockAfterGreens consecutive greens -> LOCK: freeze the baseline, applying only
    //    a micro-trim (<= calLockMaxTrimMs) per shot so a dialed-in type stops hunting the
    //    ~30-40ms window.
    //  - calUnlockAfterMisses consecutive misses -> back to ACQUIRE (and restore the large
    //    initial gain) so a new build / contest level recalibrates quickly.
    const bool isGreen = std::abs(errorMs) <= 0.5;   // evaluator emits exactly 0 inside the green
    int& phase = config_.shotTypeCalPhase[shotType];   // 0 = Acquire, 1 = Lock (default-inserts 0)
    int& greens = calGreens_[shotType];
    int& misses = calMisses_[shotType];
    const bool locked = phase == 1;

    // ANNEALING gain: big for the first few shots of a bucket (fast from-scratch calibration),
    // decaying to a small stable gain so a converged bucket does not oscillate.
    const int count = config_.shotTypeLearnCount.value(shotType, 0);
    const double annealGain = config_.outcomeGainStable
        + (config_.outcomeGainInitial - config_.outcomeGainStable)
          * std::exp(-static_cast<double>(count) / std::max(0.5, config_.learnAnnealTau));
    // Divergence guard: if this type's verdict stays RAILED at the per-read max clamp with the SAME
    // sign for more than calDivergenceGuardShots shots in a row (never greening, and never un-railing
    // as it should when it nears green), FREEZE integration for this type so a degraded/ambiguous
    // signal can't pin the clock at a clamp (the inverted-grader runaway signature). A healthy ACQUIRE
    // greens or un-rails well before the threshold. Streak resets on any non-railed verdict.
    const bool railed = std::abs(errorMs) >= config_.meterMaxErrorMs - 0.5;
    const int errSign = errorMs > 0.0 ? 1 : (errorMs < 0.0 ? -1 : 0);
    int& railStreak = calRailStreak_[shotType];
    int& railSign = calRailSign_[shotType];
    if (!railed) {
        railStreak = 0; railSign = 0;
    } else if (railStreak > 0 && errSign == railSign) {
        railStreak += 1;
    } else {
        railStreak = 1; railSign = errSign;
    }
    const bool diverging = railStreak > config_.calDivergenceGuardShots;
    const bool divergenceTripped = diverging
        && railStreak == config_.calDivergenceGuardShots + 1;
    if (divergenceTripped) {
        // First trip -> silent recalibration: integration freezes (stepFor below) AND the
        // type drops to ACQUIRE with a fresh annealing window (the learn-count write below
        // honors the trip), so the moment the signal un-rails (one non-railed verdict
        // resets the streak) it re-converges fast instead of sitting half-locked on a clamp.
        phase = 0;
        greens = 0;
        misses = 0;
        emit calPhaseUpdated(config_.shotTypeCalPhase);
    }

    // The step actually applied: ACQUIRE uses the full annealed integral step; LOCK caps it to a
    // minuscule trim so the frozen baseline only nudges. A green verdict (errorMs ~ 0) moves
    // nothing in either phase. LATE (+) adds lead / shortens the clock; EARLY (-) the reverse.
    // While diverging, every step is frozen to 0 (the guard above).
    auto stepFor = [&](double raw) -> double {
        if (diverging) return 0.0;
        const double s = annealGain * raw;
        return locked ? std::clamp(s, -config_.calLockMaxTrimMs, config_.calLockMaxTrimMs) : s;
    };

    // [TIMING FINDING 3] ONE knob per outcome — never the offset AND the clock for a single
    // error. The per-type feedforward deadline is (clock - ffOffset) with ffOffset =
    // base + shotTypeLearnedOffsetMs, so nudging the offset +step AND the clock -step for one
    // outcome shifts the deadline by -2*step: the loop gain DOUBLES (annealGain up to 0.6 => an
    // effective ~1.2, above unity -> EARLY/LATE bang-bang). Attribute the correction to the
    // estimator that actually FIRED this shot: a VISION-timed release's deadline depends only on
    // the offset (the vision lead effectiveLatency reads shotTypeOffsetMs), so the offset owns it
    // and the clocks stay put; a FEEDFORWARD/blind/timeout release is clock-anchored, so the
    // clock(s) own it and the offset stays put. Each fire path then closes its own loop with
    // exactly one step. The learn-count + phase machinery is unchanged.
    const double perTypeStep = stepFor(errorMs);
    // Per-type offset (adds on top of the global earlyLateOffsetMs). Clamp WIDENED to ±120 so a
    // high-lead type (e.g. Go-To, previously railed at the old ±60 -> structurally late) isn't pinned.
    double& learned = config_.shotTypeLearnedOffsetMs[shotType];   // default-insert keeps the key live
    if (lastReleaseWasVisionTimed_) {
        learned = std::clamp(learned + perTypeStep, -120.0, 120.0);
    }
    config_.shotTypeLearnCount[shotType] = divergenceTripped ? 0 : count + 1;
    emit learningUpdated(config_.shotTypeLearnedOffsetMs);

    // Calibrate BOTH feedforward clocks from the same FEEDFORWARD-timed outcome (a LATE release
    // fired too late on EITHER anchor's clock -> shorten it; EARLY lengthens). Keeping both warm
    // makes the anchor A/B a clean flip. The two clocks anchor DIFFERENT fire paths (meter-appear
    // vs hold-start), only one of which fires per shot, so nudging both is a single step per
    // anchor — NOT the offset+clock double-count above. The post-release meter error is
    // proportional (real ms from green), so the clock converges fast then SETTLES (0 inside the
    // deadband -> holds; no bang-bang). Only an already-armed clock (> its floor) is nudged, and
    // only when the shot fired feedforward-timed (a vision-timed shot's deadline ignores the clock).
    if (!lastReleaseWasVisionTimed_) {
        if (config_.shotTypeFeedforwardMs.contains(shotType)) {
            double& clk = config_.shotTypeFeedforwardMs[shotType];
            if (clk > config_.feedforwardMinMs && std::abs(errorMs) > 1e-6) {
                clk = std::clamp(clk - perTypeStep, config_.feedforwardMinMs, 5000.0);
                emit feedforwardUpdated(config_.shotTypeFeedforwardMs);
            }
        }
        if (config_.shotTypeMeterToReleaseMs.contains(shotType)) {
            double& clk = config_.shotTypeMeterToReleaseMs[shotType];
            if (clk > config_.meterClockMinMs && std::abs(errorMs) > 1e-6) {
                clk = std::clamp(clk - perTypeStep, config_.meterClockMinMs, 5000.0);
                emit meterClockUpdated(config_.shotTypeMeterToReleaseMs);
            }
        }
    }

    // Phase transitions on the verdict streak.
    if (isGreen) {
        greens += 1;
        misses = 0;
        if (phase == 0 && greens >= config_.calLockAfterGreens) {
            phase = 1;                                  // ACQUIRE -> LOCK
            // Capture the network-offset baseline this type locked against — the
            // delta-compensation reference future shots clamp their drift to (persisted).
            config_.shotTypeRttBaselineMs[shotType] = pendingNetworkOffsetMs_;
            emit rttBaselineUpdated(config_.shotTypeRttBaselineMs);
            emit calPhaseUpdated(config_.shotTypeCalPhase);
        }
    } else {
        misses += 1;
        greens = 0;
        if (phase == 1 && misses >= config_.calUnlockAfterMisses) {
            phase = 0;                                  // LOCK -> ACQUIRE
            config_.shotTypeLearnCount[shotType] = 0;   // restore the large initial gain
            emit calPhaseUpdated(config_.shotTypeCalPhase);
        }
    }
}

QVector<AutomationEngine::MeterCalSample>
AutomationEngine::meterSettledFrames(const QVector<MeterCalSample>& samples) const
{
    // 1) keep clean, readable frames (accepted, confident, plausible near-top green chevron).
    QVector<MeterCalSample> clean;
    clean.reserve(samples.size());
    for (const auto& s : samples) {
        if (s.accepted && s.confidence >= config_.meterMinConfidence
            && s.greenStartPct >= config_.meterGreenMinStartPct
            && s.greenEndPct > s.greenStartPct
            && (s.greenEndPct - s.greenStartPct) <= config_.meterGreenMaxWidthPct
            && std::isfinite(s.fillPct) && std::isfinite(s.greenCenterPct)) {
            clean.append(s);
        }
    }
    const int need = config_.meterMinSettledFrames;
    if (clean.size() < need) {
        return {};
    }
    // 2) walk maximal STATIC-bbox runs (consecutive frames whose bbox barely moved = the meter
    //    stopped sliding). Within each run take the SETTLED TAIL: trailing frames whose fill is
    //    within meterSettleFillTolPct of the run's LAST fill — this excludes the rise/bounce that
    //    precedes the settle (e.g. a LATE shot's 100->52 bounce inside an otherwise static bbox).
    //    The latest qualifying tail wins (the frozen marker is the last thing on screen). A meter
    //    that keeps moving (airborne fade/Go-To) yields only length-1 runs -> empty -> not graded,
    //    UNLESS [ORION_SETTLE_SMOOTH_MOTION] admits it as a smoothly-translating run (see below).
    const double maxMove = config_.meterSettleMaxMovePx;
    const double fillTol = config_.meterSettleFillTolPct;
    // [ORION_SETTLE_SMOOTH_MOTION 2026-08-11] Default OFF -> every value below collapses out and
    // this function stays byte-for-byte the original static-run walk.
    const bool allowMotion = config_.meterSettleAllowSmoothMotion;
    const double maxTravel = config_.meterSettleMaxTravelPx;
    const double maxAccel = config_.meterSettleMaxAccelPx;
    QVector<MeterCalSample> best;
    const int n = clean.size();
    int i = 0;
    while (i < n) {
        int j = i + 1;
        // Whether THIS run needed the motion path even once. A run that stayed static is graded on
        // exactly the original terms; only a run that used motion pays the stricter price.
        bool usedMotion = false;
        double prevDx = 0.0;
        double prevDy = 0.0;
        bool havePrevStep = false;
        while (j < n) {
            const double dx = clean[j].bx - clean[j - 1].bx;
            const double dy = clean[j].by - clean[j - 1].by;
            const bool isStatic = std::hypot(dx, dy) <= maxMove;
            bool isSmooth = false;
            if (!isStatic && allowMotion && std::hypot(dx, dy) <= maxTravel) {
                // First step of a run has nothing to compare against, so travel alone admits it --
                // otherwise a marker that is ALREADY moving when the run opens (every Go-To) could
                // never seed a velocity and the motion path would be dead code on real captures.
                isSmooth = !havePrevStep
                    || std::hypot(dx - prevDx, dy - prevDy) <= maxAccel;
            }
            if (!isStatic && !isSmooth) {
                break;
            }
            usedMotion = usedMotion || !isStatic;
            prevDx = dx;
            prevDy = dy;
            havePrevStep = true;
            ++j;
        }
        // A translating marker is held to a TIGHTER fill agreement and a LONGER run than a static
        // one, so turning this on can never admit a run the static rule would already have taken.
        const double runFillTol = usedMotion ? fillTol * 0.5 : fillTol;
        const int runNeed = usedMotion
            ? std::max(need, config_.meterSettleMotionMinFrames)
            : need;
        const double lastFill = clean[j - 1].fillPct;
        int t = j;
        while (t > i && std::abs(clean[t - 1].fillPct - lastFill) <= runFillTol) {
            --t;
        }
        bool tailIsFrozen = true;
        if (usedMotion && j - t >= 2) {
            // [ORION_SETTLE_SMOOTH_MOTION fix-2] The tail above bounds TOTAL SPREAD against the
            // last fill, which a slow steady climb satisfies while never settling (<= 0.8 pp/frame
            // threads it -- see the header). Net drift is the property we actually want: a frozen
            // marker ends where it started. Motion runs only; the static path is unchanged.
            tailIsFrozen = std::abs(clean[j - 1].fillPct - clean[t].fillPct)
                <= config_.meterSettleMotionMaxNetDriftPct;
        }
        if (j - t >= runNeed && tailIsFrozen) {
            best = clean.mid(t, j - t);
        }
        i = j;
    }
    return best;
}

bool AutomationEngine::meterCapHasSettledRun() const
{
    return meterSettledFrames(meterCapSamples_).size() >= config_.meterMinSettledFrames;
}

void AutomationEngine::startPostReleaseMeterCapture(double now)
{
    meterCapActive_ = true;
    meterCapSeq_ = shot_.releaseSeq;
    meterCapShotType_ = shot_.shotType;
    meterCapDeadlineMs_ = now + config_.postReleaseWindowMs;        // soft: grade once settled
    meterCapHardDeadlineMs_ = now + config_.postReleaseMaxWindowMs; // hard: grade-or-skip regardless
    meterCapPeakFillPct_ = maxFillThisShot_;   // peak the meter reached this shot (recede/LATE)
    // Snapshot the fill the meter was showing when the command was SUBMITTED. The command then
    // travels for a whole lead (~220-310ms) during which the meter keeps climbing ~0.2%/ms, so
    // submit-fill and landing-fill are ~50pp apart and only the pair identifies the landing.
    meterCapFillAtReleasePct_ = shot_.fillPct;
    // [ORION_TIP_PHASE] Same sample-and-hold, same reason: this capture window can outlive the
    // shot, and a beginShot() during it would clear shot_.fillPhaseAnchorMs and leave the landing
    // pairing the NEXT shot's anchor with THIS shot's stop. The running max is seeded from the
    // fill at submit -- between the anchor and the submit the meter only rises, so its max IS the
    // submit fill and the detector below resumes exactly where the offline one would be.
    meterCapPhaseAnchorMs_ = shot_.fillPhaseAnchorMs;
    meterCapPhaseAnchorLevelPct_ = shot_.fillPhaseAnchorLevelPct;
    meterCapPhaseRunMaxPct_ = shot_.fillPct;
    meterCapPhaseStopMs_ = -1.0;
    meterCapPhaseStopConfirmed_ = false;
    meterCapPhaseReopenPendingMs_ = -1.0;   // [ORION_STOP_CORROBORATE] per-capture-window state
    meterCapPhaseCapSamples_.clear();       // [ORION_STOP_SUBFRAME] per-capture-window buffer
    // [ORION_PRESS_ANCHOR] Same sample-and-hold as the phase anchor above: the landing
    // measurement pairs THIS shot's press with THIS shot's stop even if a new press arrives
    // while the capture window is still open. The press wall time is adopted only when it was
    // dated for exactly this shot's physical epoch (a stale/foreign press records nothing).
    meterCapPhysicalEpoch_ = shot_.physicalShotEpoch;
    meterCapPressWallMs_ = (shot_.physicalShotEpoch != 0
                            && pressWallEpoch_ == shot_.physicalShotEpoch
                            && pressWallMsForEpoch_ >= 0.0)
        ? pressWallMsForEpoch_ : -1.0;
    // And the delay that was riding the video at submit — the tip lands within ~200-400ms of
    // it, long before the post-shot ramp can move (a grade-time read could see a decayed
    // ramp and mis-date the wall-time tip by the whole delta).
    meterCapAppliedDelayMs_ = meterDelayAppliedMs_;
    // [ORION_USER_LEAD] and the velocity that fill is travelling at, taken from the same instant.
    // travel_pp / velocity reads back the lead, so the divisor must belong to THIS shot.
    meterCapRiseVelocityPctPerMs_ = sampler_.velocityPctPerMs();
    // [ORION_COURT_POSITION] and WHERE ON THE COURT the player was standing at that same instant.
    // Snapshotted here, alongside fill-at-submit, rather than read at grade time: the capture
    // window runs for hundreds of ms after the command and the player keeps moving through it, so
    // a grade-time read would attribute the shot to the wrong spot on the floor.
    meterCapNormXAtRel_ = lastMeterNormX_;
    meterCapNormYAtRel_ = lastMeterNormY_;
    // [ORION_LANDING_FEATURES 2026-08-12] Same sample-and-hold, same instant: the two pipeline-state
    // numbers that were computed for every shot and reached disk for none. Sampled here rather than
    // at grade time because both keep moving through the post-release capture window.
    meterCapFrameAgeAtRelMs_ = std::isfinite(shot_.frameAgeMs) ? shot_.frameAgeMs : -1.0;
    meterCapNetworkOffsetAtRelMs_ =
        std::isfinite(shot_.networkOffsetMs) ? shot_.networkOffsetMs : -1.0;
    meterCapSamples_.clear();
}

void AutomationEngine::notePhaseAnchorSample(double fillPct, double captureMs)
{
    // [ORION_TIP_PHASE] Date this shot's animation by its first UPWARD crossing of the anchor.
    //
    // The crossing must be OBSERVED, not inferred: a straddling pair (below, then at-or-above)
    // is required, so a meter first seen already above the anchor records nothing and the
    // predictor stands down for that shot. That is the fail-closed direction and it is load
    // bearing -- a lingering previous-shot meter is a genuine detection that appears high, and
    // "the fill is above 30 so the crossing must have been ~here" is exactly the guess that
    // would hand a carryover's phase to a live shot.
    //
    // Interpolated, not snapped to the later frame: at 60 fps and ~0.18 %/ms the straddling
    // pair is ~16 ms and ~3 pp apart, so snapping would inject a uniform 0-16 ms error into a
    // constant whose whole claim is a 9.6 ms rMAD.
    if (!config_.tipPhaseEnabled || !std::isfinite(fillPct) || !std::isfinite(captureMs)) {
        return;
    }
    const double anchorPct = config_.tipPhaseAnchorPct;
    // [ORION_TIP_PHASE] THE STRADDLE MUST BE A CROSSING, NOT A JUMP. See
    // RemapConfig::tipPhaseAnchorMaxOvershootPct: the straddling-pair requirement above stops a
    // meter first seen HIGH from being dated, but a carryover only has to be sampled once below
    // and once above to satisfy it, and the interpolation across that pair then invents an
    // anchor for an animation that never ran. The live failure was a 28.7 -> 50.4 pair on a
    // meter parked at ~50% since the previous shot's abort; the resulting anchor fired the
    // release at fill 50.2 into a meter that then moved 1.1 pp.
    //
    // Bounding the OVERSHOOT (how far past the anchor the upper sample already is) rather than
    // the pair's implied rate is deliberate: 21 healthy anchors in the measured batch overshot
    // by at most 3.60 pp, the failure by 20.43 pp, and unlike a rate this needs no assumption
    // about frame cadence -- which is precisely what a re-lock or a dropped-frame burst breaks.
    const double maxOvershootPct = config_.tipPhaseAnchorMaxOvershootPct;
    // [ORION_TIP_PHASE_LADDER] Walk the ladder LOW to HIGH and take the first level this pair
    // genuinely straddles. See RemapConfig::tipPhaseAnchorLadderStepPct: No Dip's meter is first
    // seen at 32.4-37.3% fill -- entirely above the 30 anchor -- so its 30 crossing is already
    // history and no straddling pair for it can ever exist. A higher level it has NOT yet crossed
    // can still be witnessed under exactly these rules, so this buys No Dip the phase member
    // without ever inferring a crossing that was not observed.
    //
    // Ascending order is what preserves existing behaviour: any meter seen below 30 straddles 30
    // first and is dated there, identically to before. Only a meter that MISSED 30 reaches 35/40.
    const int ladderCount = std::max(0, config_.tipPhaseAnchorLadderCount);
    const double ladderStep = config_.tipPhaseAnchorLadderStepPct;
    const bool ladderUsable = ladderCount > 0
        && std::isfinite(ladderStep) && ladderStep > 0.0;
    if (shot_.fillPhaseAnchorMs < 0.0
        && std::isfinite(anchorPct) && anchorPct > 0.0
        && shot_.phasePrevCaptureMs >= 0.0
        && shot_.phasePrevFillPct >= 0.0
        && fillPct > shot_.phasePrevFillPct
        && captureMs > shot_.phasePrevCaptureMs) {
        // Find the LOWEST level this pair straddles. That level, and only that level, is the
        // crossing the pair actually witnesses -- the meter reached it first on the way up.
        double crossedLevelPct = -1.0;
        for (int step = 0; step <= (ladderUsable ? ladderCount : 0); ++step) {
            const double levelPct = anchorPct + static_cast<double>(step) * ladderStep;
            if (!std::isfinite(levelPct) || levelPct <= 0.0 || levelPct >= 100.0) {
                break;
            }
            if (shot_.phasePrevFillPct < levelPct && fillPct >= levelPct) {
                crossedLevelPct = levelPct;
                break;
            }
        }
        // THE OVERSHOOT GUARD IS APPLIED TO THE CROSSED LEVEL, NOT TO A LEVEL CHOSEN TO SUIT IT.
        //
        // Testing each level independently and taking the first that PASSES lets a carryover jump
        // climb out of the provenance gate: a 26 -> 44 pair overshoots the 30 anchor by 14 pp and
        // is correctly refused there, but overshoots 35 by only 9 pp and would be admitted at 35 --
        // dating a shot the pre-ladder engine rejected outright. Caught by the inertness test.
        //
        // Anchoring the guard to the lowest straddled level keeps the ladder strictly additive:
        // it can only ever date a pair whose true crossing was ALREADY plausible, and a pair the
        // old code refused stays refused.
        const bool straddleOvershootPlausible = !std::isfinite(maxOvershootPct)
            || maxOvershootPct <= 0.0
            || fillPct - crossedLevelPct <= maxOvershootPct;
        if (crossedLevelPct >= 0.0 && straddleOvershootPlausible) {
            const double frac = std::clamp(
                (crossedLevelPct - shot_.phasePrevFillPct)
                    / (fillPct - shot_.phasePrevFillPct),
                0.0, 1.0);
            shot_.fillPhaseAnchorMs = shot_.phasePrevCaptureMs
                + frac * (captureMs - shot_.phasePrevCaptureMs);
            shot_.fillPhaseAnchorLevelPct = crossedLevelPct;
        }
    }
    shot_.phasePrevFillPct = fillPct;
    shot_.phasePrevCaptureMs = captureMs;
}

double AutomationEngine::phaseAnchorImminentTargetLevelPct(double fillPct) const noexcept
{
    // [ORION_RUNG_IMMINENT] Flag OFF: the base anchor, always -- bit-identical to the pre-flag
    // predicate, including the negative belowBy (=> no hold) for any fill above the base.
    if (!config_.tipPhaseRungImminentHold) {
        return config_.tipPhaseAnchorPct;
    }
    if (!std::isfinite(fillPct)) {
        return std::numeric_limits<double>::quiet_NaN();
    }
    // Flag ON: the lowest ladder level STRICTLY above the current fill. Strictness mirrors the
    // witness condition in notePhaseAnchorSample (prevFill < level): once the fill has reached a
    // level, no future pair can straddle it, so that level is no longer witnessable and waiting
    // for it would be waiting forever. Enumeration matches the dating ladder exactly (base +
    // k*step, k <= ladderCount, finite and inside (0, 100)); a fill above the top rung has no
    // witnessable crossing left and returns NaN so every caller refuses to hold.
    const double basePct = config_.tipPhaseAnchorPct;
    const double stepPct = config_.tipPhaseAnchorLadderStepPct;
    const int ladderCount = std::max(0, config_.tipPhaseAnchorLadderCount);
    const bool ladderUsable = ladderCount > 0
        && std::isfinite(stepPct) && stepPct > 0.0;
    for (int step = 0; step <= (ladderUsable ? ladderCount : 0); ++step) {
        const double levelPct = basePct + static_cast<double>(step) * stepPct;
        if (!std::isfinite(levelPct) || levelPct <= 0.0 || levelPct >= 100.0) {
            break;
        }
        if (levelPct > fillPct) {
            return levelPct;
        }
    }
    return std::numeric_limits<double>::quiet_NaN();
}

bool AutomationEngine::phaseAnchorImminent(double fillPct,
                                           double slopePctPerMs) const noexcept
{
    // [ORION_TIP_PHASE_IMMINENT] "The phase member is about to exist, so do not release on a
    // worse one." See RemapConfig::tipPhaseImminentHoldBandPct for the live miss this prevents.
    //
    // Every condition here is a REFUSAL to hold, so the default answer is "release normally":
    // this predicate can only ever suppress a release inside a narrow band below the anchor, and
    // any input it cannot read (non-finite fill or slope) falls through to the existing behaviour.
    if (!config_.tipPhaseEnabled) {
        return false;
    }
    const double bandPct = config_.tipPhaseImminentHoldBandPct;
    if (!std::isfinite(bandPct) || bandPct <= 0.0) {
        return false;   // explicitly disabled
    }
    if (!std::isfinite(fillPct) || !std::isfinite(slopePctPerMs)) {
        return false;
    }
    // RISING ONLY. A flat or falling meter will never reach the anchor, so holding for its
    // crossing would be holding for something that is not coming -- the difference between a
    // one-frame wait and a hang that ends at the maxHoldMs ceiling.
    if (slopePctPerMs <= 0.0) {
        return false;
    }
    // [ORION_RUNG_IMMINENT] Flag OFF this is exactly the base anchor; flag ON it is the lowest
    // ladder rung strictly above the fill (NaN when none remains -- refuse to hold).
    const double targetPct = phaseAnchorImminentTargetLevelPct(fillPct);
    if (!std::isfinite(targetPct)) {
        return false;
    }
    const double belowByPct = targetPct - fillPct;
    // Strictly BELOW the target: at or above it the crossing has already happened (or the meter
    // was first seen high, which is the ladder's problem, not this one).
    return belowByPct > 0.0 && belowByPct <= bandPct;
}

double AutomationEngine::tipPhaseLevelAdjustmentMs(double levelPct) const noexcept
{
    // [ORION_TIP_PHASE_LADDER] How much SHORTER the remaining animation is when the shot was
    // dated at levelPct instead of the base anchor. See tipPhaseConstantSlopeMsPerPct: measured
    // -5.19 and -5.28 ms/pp on two independent sessions.
    //
    // Returns 0 for the base level, so a shot dated at 30 is arithmetically untouched -- the
    // ladder cannot perturb the aim of any shot that would have anchored before it existed.
    const double basePct = config_.tipPhaseAnchorPct;
    if (!std::isfinite(levelPct) || levelPct < 0.0 || !std::isfinite(basePct)) {
        return 0.0;
    }
    // [ORION_ANCHOR_BASE20] From a base of 20 the single slope constant is no longer an
    // acceptable approximation: the meter accelerates with fill, so the measured secants run
    // 5.88 ms/pp (20->25) down to 5.63 (20->40), and -5.235 would date rung 40 ~7.9ms LATE.
    // Use the measured per-rung offsets, linearly interpolated between knots (dating only ever
    // produces exact rung levels; interpolation is a guard, and beyond the measured 20-40 range
    // the nearest segment's secant extrapolates -- the measurement showed the relationship is
    // smooth there).
    if (config_.anchorBase20) {
        constexpr int n = static_cast<int>(std::size(kAnchorBase20RungOffsets));
        int hi = 1;
        while (hi < n - 1 && levelPct > kAnchorBase20RungOffsets[hi].levelPct) {
            ++hi;
        }
        const auto& a = kAnchorBase20RungOffsets[hi - 1];
        const auto& b = kAnchorBase20RungOffsets[hi];
        const double secant = (b.offsetMs - a.offsetMs) / (b.levelPct - a.levelPct);
        return -(a.offsetMs + secant * (levelPct - a.levelPct));
    }
    const double slope = config_.tipPhaseConstantSlopeMsPerPct;
    if (!std::isfinite(slope)) {
        return 0.0;
    }
    return slope * (levelPct - basePct);
}

double AutomationEngine::phasePriorShiftMs() const noexcept
{
    // [ORION_ANCHOR_BASE20] learning.json's learned_phase_physical_ms is ALWAYS stored at base
    // anchor 30 (the pre-migration convention), so a prior learned under either regime reads
    // back correctly under either regime. Restore adds this; persist subtracts it.
    return config_.anchorBase20 ? kAnchorBase20ShiftMs : 0.0;
}

double AutomationEngine::effectiveTipPhaseConstantMs() const noexcept
{
    // The shipped constant is (physical animation phase) + (aim-preservation offset). Only the
    // first is a measurement of the world, and only the first is learned. See the long note on
    // RemapConfig::tipPhaseSeedPhysicalMs: a learner that overwrote the whole constant would
    // converge on the physical ~320 and thereby walk the aim 61 ms EARLIER across its first ten
    // landings, which is a silent re-aim, not a calibration.
    const double aimOffsetMs = std::isfinite(config_.tipPhaseSeedPhysicalMs)
        ? config_.tipPhaseConstantMs - config_.tipPhaseSeedPhysicalMs
        : 0.0;
    if (std::isfinite(learnedPhasePhysicalMs_) && learnedPhasePhysicalMs_ > 0.0) {
        return learnedPhasePhysicalMs_ + aimOffsetMs;
    }
    return config_.tipPhaseConstantMs;
}

double AutomationEngine::maxSchedulableTipLeadMs() const noexcept
{
    // [ORION_LEAD_CONFLICT] See kTipLeadScheduleMarginMs for the measurement behind the margin.
    return std::max(0.0, effectiveTipPhaseConstantMs() - kTipLeadScheduleMarginMs);
}

bool AutomationEngine::visibleEvidenceLeadStarvedByMeterDelay(double leadMs) const noexcept
{
    // [ORION_METER_DELAY_LEAD_STARVATION 2026-08-08] The structural regime behind the
    // 2026-08-08 owner session (delay 250ms, Shot Lead 540ms, tip timing 437ms): every tip
    // estimate this engine can form is dated from the DELAYED video, so none can warn more
    // than the visible runway (~ the tip constant) ahead — yet the correct lead under an
    // applied inbound delay D is loop+D. Once lead > usable max:
    //   * the phase member (anchor = observed fill crossing) is born with command_eta < 0 —
    //     honest unschedulable_lead, kept as-is;
    //   * the ONLY estimates whose horizon clears the lead are pre-anchor long-horizon
    //     sampler/registration extrapolations, and both live sessions show those arms landing
    //     ~300ms early on the visible meter (fired at fill 13-18, landed at fill ~50).
    // This predicate therefore names a regime, not a fault: with it true, a visible-meter
    // EXTRAPOLATOR must not arm (fail-closed hold/abort), and the conflict diagnostics carry
    // the delay arithmetic. Inert (false) whenever no delay is applied, so the delay-0
    // production path is bit-identical. The env-override sweep is excluded for the same
    // reason it out-ranks the user lead: a dev sweep must stay byte-identical to its design.
    return config_.tipPhaseEnabled
        && meterDelayAppliedMs_ > 0.0
        && !config_.leadOverrideFromEnv
        && std::isfinite(leadMs)
        && leadMs > maxSchedulableTipLeadMs();
}

double AutomationEngine::maxMeterDelayForLeadMs(double leadMs) const noexcept
{
    // The wall-clock component of the consumed lead is (lead - the delay-condition offset in
    // force) — the loop latency the operator calibrated at delay 0. The largest delay whose
    // total required lead still fits inside the visible-evidence ceiling is usableMax - loop.
    //
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09 FIX] This used to subtract meterDelayAppliedMs_,
    // which silently ASSUMED the operator had already folded the applied delay into their Shot
    // Lead. On 2026-08-09 they had not — Shot Lead stayed 295 ms with 175 ms applied — and the
    // advice was inflated by exactly the applied delay: it reported "max meter delay 271 ms"
    // (391.3 - (295 - 175)) for a rig whose real headroom over that lead was 96 ms. Now that the
    // delay's contribution to the lead is an explicit, known term, the loop component is exact
    // and the number is honest whether or not the operator ever touched the offset.
    if (!std::isfinite(leadMs)) {
        return 0.0;
    }
    const double loopLeadMs = leadMs - appliedMeterDelayLeadOffsetMs();
    return std::max(0.0, maxSchedulableTipLeadMs() - loopLeadMs);
}

void AutomationEngine::maybeWarnMeterDelayLeadUncalibrated()
{
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] THE LINE THE 2026-08-09 SESSION DID NOT HAVE.
    //
    // Live evidence (logs/orion_native.log, one process, delay toggled mid-session):
    //   13:33:31  meter delay Locked, applied 175 ms, Shot Lead 295 ms
    //   13:33:38..13:34:53  8 graded landings, settled_fill 83.61/83.61/52.70/83.61/52.68/
    //                       50.42/52.70/52.69   (peak_fill 98-100 on all eight -> LATE)
    //   13:38:10  delay Disabled, SAME Shot Lead 295/294 ms
    //   13:38:17..13:41:07  35 graded landings, settled_fill median 94.3, 15 of them >= 96
    // Across both logs: 59 of 60 graded delay-on landings sit outside the good band.
    //
    // Every log line the engine emitted during the delay-on block claimed success —
    // "TIP RESERVATION: disposition=reservation_promoted source=phase", "PRECISE FIRE ARM",
    // "Release attribution: ... crossingEta=297" against a configured lead of 295. The one
    // predicate that could have objected, visibleEvidenceLeadStarvedByMeterDelay(), keys on
    // lead > maxSchedulableTipLeadMs (295 > 391 == false), so the whole delayed regime was
    // diagnostically SILENT: grep the session for "SHOT LEAD CONFLICT" and the first hit is
    // 13:37:25, four minutes later, after the operator had already given up and typed 612.
    //
    // So: when a delay engages and the delay-condition offset is still 0, say so once, name the
    // lead actually in force, and hand over the one number the operator cannot compute by hand
    // (the offset headroom before shots start aborting). Diagnostic-only.
    if (!(meterDelayAppliedMs_ > 0.0) || config_.leadOverrideFromEnv) {
        meterDelayLeadUncalibratedWarnedMs_ = -1.0;   // re-arm for the next engage
        return;
    }
    if (std::abs(appliedMeterDelayLeadOffsetMs()) > 1e-9) {
        return;   // the operator has calibrated this condition; nothing to say
    }
    const double leadMs = measuredLeadForActuationMs();
    if (!std::isfinite(leadMs) || leadMs <= 0.0) {
        return;   // no schedulable lead at all -> the fail-closed paths already speak
    }
    // De-dup on the 5 ms grid so a ramp that lands one millisecond off cannot re-emit.
    if (meterDelayLeadUncalibratedWarnedMs_ >= 0.0
        && std::abs(meterDelayAppliedMs_ - meterDelayLeadUncalibratedWarnedMs_) <= 5.0) {
        return;
    }
    meterDelayLeadUncalibratedWarnedMs_ = meterDelayAppliedMs_;
    const double headroomMs = maxMeterDelayLeadOffsetMs();
    const QString headroom = headroomMs > 0.0
        ? QStringLiteral("Calibrate Meter Delay Lead Offset from the game's TIMING banner "
                         "(headroom +%1ms before live tip shots abort).")
              .arg(headroomMs, 0, 'f', 0)
        : QStringLiteral("This Shot Lead already sits at the visible-evidence ceiling "
                         "(usable max %1ms), so NO positive offset is schedulable: lower Shot "
                         "Lead or raise Tip Timing before calibrating the delayed condition.")
              .arg(maxSchedulableTipLeadMs(), 0, 'f', 0);
    emit engineDiagnostic(QStringLiteral(
        "METER DELAY LEAD UNCALIBRATED: meter delay %1ms is applied but the delayed-condition "
        "Shot Lead offset is 0ms, so shots fire with the delay-0 lead (%2ms). The delay changes "
        "the console's response to the release, not the meter you can see, so this lead is "
        "wrong for this condition and landings will be mistimed. %3")
                              .arg(meterDelayAppliedMs_, 0, 'f', 0)
                              .arg(leadMs, 0, 'f', 0)
                              .arg(headroom));
}

QString shotLeadUsableMaxWarningLine(double leadMs, double usableMaxMs, double appliedDelayMs)
{
    // [ORION_LEAD_CONFLICT_UI task #48] See the header note. Soft-warn only: this composes
    // the sentence the indicator shows; nothing here (or in the QML that binds it) clamps
    // the user's control — the "Clamp to safe" affordance is an explicit click.
    if (!std::isfinite(leadMs) || !std::isfinite(usableMaxMs) || usableMaxMs <= 0.0
        || leadMs <= usableMaxMs) {
        return QString();
    }
    const double appliedMs =
        (std::isfinite(appliedDelayMs) && appliedDelayMs > 0.0) ? appliedDelayMs : 0.0;
    if (appliedMs > 0.0) {
        // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] The old second clause offered
        // "lower the meter delay to usableMax - (lead - applied)". Both halves are refuted:
        //   * lowering the applied delay does NOT restore schedulability while the lead stays
        //     above the ceiling — visibleEvidenceLeadStarvedByMeterDelay() compares the LEAD to
        //     the ceiling, so the same shot still aborts at any delay > 0; and
        //   * the subtraction assumed the operator had already folded the applied delay into
        //     Shot Lead. On 2026-08-09 they had not (lead 295, applied 175), and the number was
        //     inflated by exactly the applied delay.
        // Clamping Shot Lead is the ONLY move that makes this pair schedulable, so it is the
        // only move the indicator now offers; the delay is still named because it is what makes
        // the abort inevitable rather than merely likely.
        return QStringLiteral(
                   "Above usable max (%1 ms with %2 ms delay) — shots will abort. "
                   "Clamp Shot Lead to %1 ms; reach the delayed condition with the Meter Delay "
                   "Lead Offset instead.")
            .arg(usableMaxMs, 0, 'f', 0)
            .arg(appliedMs, 0, 'f', 0);
    }
    return QStringLiteral(
               "Above usable max (%1 ms) — live tip shots will abort. Clamp Shot Lead to "
               "%1 ms or less, or raise/reset Tip Timing.")
        .arg(usableMaxMs, 0, 'f', 0);
}

double AutomationEngine::pressAnchoredVideoLatencyMs(QString* srcOut) const
{
    // [ORION_PRESS_ANCHOR] The V estimate — how long a game event takes to reach this
    // engine's eyes through the video pipeline, EXCLUDING any applied meter delay (that is
    // carried separately, because it changes at runtime while V does not).
    //
    // l_fixed is the sidecar's measured release-path latency with rtt_now + tick_wait
    // stripped, so it still contains the console-side processing between the press landing
    // and the meter responding — we CANNOT decompose that residue out. It does not matter
    // for the press-anchored pair: the observation SUBTRACTS this V from the observed stop
    // and the predictor ADDS the same V back, so a constant decomposition error cancels
    // exactly, as long as both sides use this one accessor. What does not cancel is a V
    // SOURCE switch mid-calibration, which is why the source is logged per observation and
    // the full-loop stand-in caps the observation's confidence at M.
    if (std::isfinite(measuredLFixedMs_) && measuredLFixedMs_ > 0.0) {
        if (srcOut) {
            *srcOut = QStringLiteral("l_fixed");
        }
        return measuredLFixedMs_;
    }
    if (std::isfinite(measuredLatencyAuthorityMs_) && measuredLatencyAuthorityMs_ > 0.0) {
        if (srcOut) {
            *srcOut = QStringLiteral("loop");
        }
        return measuredLatencyAuthorityMs_;
    }
    if (srcOut) {
        *srcOut = QStringLiteral("none");
    }
    return -1.0;
}

double AutomationEngine::pressAnchoredLearnedTipMs(const QString& shotType) const
{
    return config_.pressAnchoredTipMs.value(shotType, -1.0);
}

double AutomationEngine::pressAnchoredLearnedSigmaMs(const QString& shotType) const
{
    return std::max(kPressAnchoredSigmaFloorMs,
                    config_.pressAnchoredTipSigmaMs.value(shotType,
                                                          kPressAnchoredFactorySigmaMs));
}

double AutomationEngine::pressAnchoredLearnedWeight(const QString& shotType) const
{
    return config_.pressAnchoredTipN.value(shotType, 0.0);
}

void AutomationEngine::recordPressToTipObservation(const QString& shotType,
                                                   double pressToTipMs, double weight)
{
    // [ORION_PRESS_ANCHOR] One accepted landing -> one observation of the per-type
    // press->real-tip constant. WINDOWED (last kPressAnchoredWindow entries), not lifetime:
    // the game's animation can drift with patches, and a windowed median tracks that while
    // a lifetime mean would fight it forever. H-confidence observations weigh 1.0,
    // M-confidence 0.5 (the caller decides); L never reaches here.
    if (!std::isfinite(pressToTipMs) || weight <= 0.0 || shotType.isEmpty()) {
        return;
    }
    auto& window = pressTipWindow_[shotType];
    window.append(qMakePair(pressToTipMs, weight));
    while (window.size() > kPressAnchoredWindow) {
        window.removeFirst();
    }
    const auto weightedMedian = [](QVector<QPair<double, double>> v) -> double {
        if (v.isEmpty()) {
            return std::numeric_limits<double>::quiet_NaN();
        }
        std::sort(v.begin(), v.end(),
                  [](const QPair<double, double>& a, const QPair<double, double>& b) {
                      return a.first < b.first;
                  });
        double total = 0.0;
        for (const auto& p : v) {
            total += p.second;
        }
        double acc = 0.0;
        for (const auto& p : v) {
            acc += p.second;
            if (acc * 2.0 >= total) {
                return p.first;
            }
        }
        return v.last().first;
    };
    const double sessionMedian = weightedMedian(window);
    double sessionW = 0.0;
    QVector<QPair<double, double>> deviations;
    deviations.reserve(window.size());
    for (const auto& p : window) {
        sessionW += p.second;
        deviations.append(qMakePair(std::abs(p.first - sessionMedian), p.second));
    }
    // Windowed MAD -> sigma (1.4826 * MAD is the normal-consistent scale), floored so a
    // couple of coincidentally identical observations cannot claim implausible precision.
    const double sessionSigma = std::max(kPressAnchoredSigmaFloorMs,
                                         1.4826 * weightedMedian(deviations));
    // Blend with what this session RESTORED from settings — that is data measured by earlier
    // sessions, not a guess — with the restored influence fading exactly as the in-session
    // window fills. A FACTORY prior restores with weight 0 and therefore contributes nothing
    // to either the value or the min-samples gate: only real observations count.
    const double priorW = std::min(pressTipPriorW_.value(shotType, 0.0),
                                   static_cast<double>(kPressAnchoredWindow));
    const double priorBlendW = std::clamp(
        static_cast<double>(kPressAnchoredWindow) - sessionW, 0.0, priorW);
    const double priorTip = pressTipPriorMs_.value(shotType, sessionMedian);
    const double priorSigma = pressTipPriorSigmaMs_.value(shotType,
                                                          kPressAnchoredFactorySigmaMs);
    const double denom = sessionW + priorBlendW;
    const double learnedTip = denom > 0.0
        ? (sessionMedian * sessionW + priorTip * priorBlendW) / denom
        : sessionMedian;
    const double learnedSigma = denom > 0.0
        ? (sessionSigma * sessionW + priorSigma * priorBlendW) / denom
        : sessionSigma;
    const double totalW = std::min(static_cast<double>(kPressAnchoredWindow),
                                   sessionW + priorW);
    config_.pressAnchoredTipMs.insert(shotType, learnedTip);
    config_.pressAnchoredTipSigmaMs.insert(shotType, learnedSigma);
    config_.pressAnchoredTipN.insert(shotType, totalW);
    emit pressAnchoredCalibrationUpdated(config_.pressAnchoredTipMs,
                                         config_.pressAnchoredTipSigmaMs,
                                         config_.pressAnchoredTipN);
}

void AutomationEngine::emitPressTipObservation(int seq, const QString& shotType,
                                               bool graded, bool greenObserved,
                                               const QVector<MeterCalSample>& samples)
{
    // [ORION_PRESS_ANCHOR] Phase-A collector. One line per completed landing pairing the
    // delay-immune press wall time with the wall-time-equivalent real tip:
    //     real_tip_wall = observed stop (delayed video) - applied_delay - V
    // Aborted shots never reach the landing evaluator, so they never emit (per design).
    if (meterCapPressWallMs_ < 0.0 || meterCapPhysicalEpoch_ == 0) {
        return;   // press not dated for this shot's epoch -> no anchor, no observation
    }
    // The real tip in (delayed) video time. The confirmed stop is the instrument of record
    // (capture-aligned, the same dating recordPhaseConstantSample measures against); the
    // engine-arrival time of the peak sample is a degraded fallback that folds in the
    // per-frame decode/IPC age, so it can never carry H confidence.
    double tipVideoMs = -1.0;
    bool stopConfirmed = false;
    if (meterCapPhaseStopConfirmed_ && meterCapPhaseStopMs_ >= 0.0) {
        tipVideoMs = meterCapPhaseStopMs_;
        stopConfirmed = true;
    } else {
        double bestFill = -1.0;
        for (const auto& s : samples) {
            if (s.accepted && s.fillPct > bestFill) {
                bestFill = s.fillPct;
                tipVideoMs = s.tMs;
            }
        }
        if (tipVideoMs < 0.0) {
            return;   // nothing dates the tip this shot
        }
    }
    QString vSrc;
    const double vMs = pressAnchoredVideoLatencyMs(&vSrc);
    const double appliedDelayMs = std::max(0.0, meterCapAppliedDelayMs_);
    const double realTipWallMs = tipVideoMs - appliedDelayMs - (vMs > 0.0 ? vMs : 0.0);
    const double pressToTipMs = realTipWallMs - meterCapPressWallMs_;
    // Confidence: H = graded landing + OBSERVED green window + confirmed stop + a true
    // l_fixed V. M = graded but one of those degraded (window substituted, stop dated from
    // the peak-sample fallback, or V standing in from the full loop). L = ungraded, or no V
    // estimate at all (the wall math is then V-less and must never teach the learner).
    QString confidence = QStringLiteral("L");
    double weight = 0.0;
    if (graded && vMs > 0.0) {
        const bool fullQuality = greenObserved && stopConfirmed
            && vSrc == QLatin1String("l_fixed");
        confidence = fullQuality ? QStringLiteral("H") : QStringLiteral("M");
        weight = fullQuality ? 1.0 : 0.5;
    }
    const bool inBand = std::isfinite(pressToTipMs)
        && pressToTipMs >= kPressAnchoredLearnMinMs
        && pressToTipMs <= kPressAnchoredLearnMaxMs;
    // [ORION_DEV_FIRE_OFFSET] The stop rides the release, so a deliberately displaced fire
    // displaces this observation too: log it (the sweep analysis wants it), never learn it.
    //
    // [ORION_METER_DELAY_LEAD_KEYING 2026-08-09] The same exclusion, for the same reason, when
    // a meter delay was applied for this shot. pressAnchoredTipMs is a SINGLE per-shot-type
    // prior with no delay key, and the delayed condition is measurably a different population:
    // Standstill press_to_tip median over the 2026-08-08/09 corpus is 601 ms at delay 0 (n=133)
    // against 735 ms at 150 (n=20), 700 ms at 175 (n=16), 932 ms at 300 (n=9) and 1131 ms at
    // 600 (n=6) — the observation subtracts the applied delay on the premise that the VIDEO is
    // delayed by it, which MeterDelayIntercept's game-server-only filter refutes, so the
    // residual is neither cancelled nor constant. Folding those into the delay-0 prior would
    // walk it by ~130 ms per delayed session and then fire delay-0 shots off the poisoned
    // value. Log it (the census wants it, and the observation is what a future delay-keyed
    // prior would be built from), never learn it.
    const bool accepted = weight > 0.0 && inBand && !devFireOffsetArmed_
        && !(meterCapAppliedDelayMs_ > 0.0);
    // The shot-type label travels underscore-joined ("Left Fade" -> "Left_Fade") because this
    // schema places it mid-line; the space-bearing form is reserved for trailing shot= fields.
    QString typeToken = shotType.isEmpty() ? QStringLiteral("unknown") : shotType;
    typeToken.replace(QLatin1Char(' '), QLatin1Char('_'));
    emit engineDiagnostic(QStringLiteral(
        "PRESS-TIP OBSERVATION: physical_epoch=%1 shot_type=%2 press_wall_ms=%3 "
        "real_tip_wall_ms=%4 press_to_tip_ms=%5 applied_delay_ms=%6 V_ms=%7 confidence=%8 "
        "v_src=%9 stop_confirmed=%10 accepted=%11 seq=%12")
                              .arg(meterCapPhysicalEpoch_)
                              .arg(typeToken)
                              .arg(meterCapPressWallMs_, 0, 'f', 1)
                              .arg(realTipWallMs, 0, 'f', 1)
                              .arg(pressToTipMs, 0, 'f', 1)
                              .arg(appliedDelayMs, 0, 'f', 0)
                              .arg(vMs > 0.0 ? vMs : -1.0, 0, 'f', 1)
                              .arg(confidence)
                              .arg(vSrc)
                              .arg(stopConfirmed ? 1 : 0)
                              .arg(accepted ? 1 : 0)
                              .arg(seq));
    if (accepted) {
        recordPressToTipObservation(shotType, pressToTipMs, weight);
    }
}

void AutomationEngine::maybeWarnTipTimingDivergence(double frozenPhysicalMs,
                                                    double measuredPhysicalMs)
{
    // [ORION_AIM_FREEZE] See kTipTimingDivergenceWarnMs. The warning speaks in the EFFECTIVE
    // frame (physical + aim offset) because that is the number the Tip Timing card shows and
    // the number the user can actually retype.
    if (!(frozenPhysicalMs > 0.0) || !(measuredPhysicalMs > 0.0)
        || std::abs(measuredPhysicalMs - frozenPhysicalMs) <= kTipTimingDivergenceWarnMs) {
        return;
    }
    const auto quantise = [](double v) { return std::round(v / 5.0) * 5.0; };
    if (quantise(frozenPhysicalMs) == quantise(tipTimingDivergenceWarnedFrozenMs_)
        && quantise(measuredPhysicalMs) == quantise(tipTimingDivergenceWarnedMeasuredMs_)) {
        return;
    }
    tipTimingDivergenceWarnedFrozenMs_ = frozenPhysicalMs;
    tipTimingDivergenceWarnedMeasuredMs_ = measuredPhysicalMs;
    const double aimOffsetMs = std::isfinite(config_.tipPhaseSeedPhysicalMs)
        ? config_.tipPhaseConstantMs - config_.tipPhaseSeedPhysicalMs
        : 0.0;
    emit engineDiagnostic(QStringLiteral(
        "TIP TIMING DIVERGENCE: manual %1ms vs measured %2ms (d=%3ms). Shots stay timed to the "
        "manual value; Reset Tip Timing to use the measured animation.")
                              .arg(frozenPhysicalMs + aimOffsetMs, 0, 'f', 0)
                              .arg(measuredPhysicalMs + aimOffsetMs, 0, 'f', 0)
                              .arg(measuredPhysicalMs - frozenPhysicalMs, 0, 'f', 0));
}

void AutomationEngine::maybeWarnLeadAuthorityDisagreement()
{
    // [ORION_LEAD_CONFLICT] Compare only against a VALIDATED posterior: the factory prior is a
    // packaged guess, not a measurement of this rig, and badging a user's tuned lead against a
    // guess would teach them to ignore the badge. 3*sd is the coordinate the estimator itself
    // treats as a real disagreement.
    if (config_.leadOverrideFromEnv
        || !std::isfinite(config_.userActuationLeadMs)
        || config_.userActuationLeadMs < config_.actuationLeadMinMs
        || config_.userActuationLeadMs > config_.actuationLeadMaxMs) {
        return;   // no in-band replacement value -> the authority itself fires; nothing to compare
    }
    if (measuredLatencyAuthorityKind_ != QLatin1String("validated")
        || !std::isfinite(measuredLatencyAuthorityMs_) || measuredLatencyAuthorityMs_ <= 0.0
        || !std::isfinite(measuredLatencyAuthoritySdMs_)
        || measuredLatencyAuthoritySdMs_ <= 0.0) {
        return;
    }
    const double diffMs = config_.userActuationLeadMs - measuredLatencyAuthorityMs_;
    if (std::abs(diffMs) <= 3.0 * measuredLatencyAuthoritySdMs_) {
        return;
    }
    // De-dup on the 10 ms grid: a posterior refining by a millisecond per label must not
    // re-emit the same disagreement every telemetry frame.
    const auto quantise = [](double v) { return std::round(v / 10.0) * 10.0; };
    if (quantise(config_.userActuationLeadMs) == quantise(leadAuthorityWarnedLeadMs_)
        && quantise(measuredLatencyAuthorityMs_)
            == quantise(leadAuthorityWarnedAuthorityMs_)) {
        return;
    }
    leadAuthorityWarnedLeadMs_ = config_.userActuationLeadMs;
    leadAuthorityWarnedAuthorityMs_ = measuredLatencyAuthorityMs_;
    emit engineDiagnostic(QStringLiteral(
        "SHOT LEAD DISAGREEMENT: %1 Shot Lead %2ms vs measured latency %3ms (sd %4, n=%5). "
        "The bot fires with %2ms; the measured value is advisory.")
                              .arg(config_.userActuationLeadSet ? QStringLiteral("user")
                                                                : QStringLiteral("seeded"))
                              .arg(config_.userActuationLeadMs, 0, 'f', 0)
                              .arg(measuredLatencyAuthorityMs_, 0, 'f', 1)
                              .arg(measuredLatencyAuthoritySdMs_, 0, 'f', 1)
                              .arg(measuredLatencyN_));
    emit leadAuthorityDisagreementDiagnosed(config_.userActuationLeadMs,
                                            measuredLatencyAuthorityMs_,
                                            measuredLatencyAuthoritySdMs_,
                                            measuredLatencyN_);
}

QString AutomationEngine::actuationLeadSourceLabel(double consumedLeadMs) const noexcept
{
    // Mirrors measuredLeadForActuationMs()'s precedence exactly: the env sweep out-ranks the
    // user setting, and the in-band user/seed value replaces the authority. Classification only
    // -- consuming code never branches on this.
    if (!std::isfinite(consumedLeadMs) || consumedLeadMs <= 0.0) {
        return QStringLiteral("none");
    }
    // [ORION_METER_DELAY_LEAD_KEYING] Compare against the value measuredLeadForActuationMs()
    // actually returns in the CURRENT delay condition (user lead + the in-force offset). Without
    // this term a delayed shot with a non-zero offset would be mislabelled "authority" — the
    // identical provenance lie the lead_kind/lead_source split was created to end. At offset 0
    // (always, at delay 0) this is byte-identical to the previous comparison.
    const double keyedUserLeadMs =
        config_.userActuationLeadMs + appliedMeterDelayLeadOffsetMs();
    const bool inBandReplacement = !config_.leadOverrideFromEnv
        && std::isfinite(config_.userActuationLeadMs)
        && config_.userActuationLeadMs >= config_.actuationLeadMinMs
        && config_.userActuationLeadMs <= config_.actuationLeadMaxMs
        && std::abs(consumedLeadMs - keyedUserLeadMs) <= 1e-9;
    if (inBandReplacement) {
        return config_.userActuationLeadSet ? QStringLiteral("user")
                                            : QStringLiteral("seed");
    }
    return QStringLiteral("authority");
}

double AutomationEngine::refinePhaseStopSubframeMs() const
{
    // [ORION_STOP_SUBFRAME] Sub-frame end-of-rise estimate: the corner of a ramp-and-plateau
    // model. The snapped stop takes the wall time of the SAMPLE that tripped the step threshold,
    // but the true corner sits anywhere inside the straddling frame interval — and on which side
    // of the snapped sample it sits depends on where the corner fell relative to the 2pp step
    // (a corner just after a frame leaves the next jump under the step, so the snap dates the
    // LAST RISING sample; a corner just before a frame trips the step, so the snap dates the
    // FIRST PLATEAU sample). Fitting the last rising samples and intersecting with the plateau
    // level recovers the corner; clamping to the observed straddle keeps a bad fit from ever
    // leaving the interval the data actually brackets.
    //
    // Every failure path returns -1 (caller falls back to the snapped date): a refinement is a
    // variance reduction, never a substitute for evidence.
    if (!config_.stopDatingSubframe || meterCapPhaseStopMs_ < 0.0
        || meterCapPhaseCapSamples_.size() < 4) {
        return -1.0;
    }
    const double tSnap = meterCapPhaseStopMs_;
    // Plateau level: median accepted fill over (tSnap, tSnap + quiet]. The quiet window is what
    // confirmed the stop, so it is by construction growth-free; the median rides out the ~1pp
    // dither and any single-frame spike stopReopenCorroborate already refused to re-date on.
    QVector<double> plateauFills;
    for (const auto& p : meterCapPhaseCapSamples_) {
        if (p.first > tSnap && p.first <= tSnap + config_.tipPhaseStopQuietMs) {
            plateauFills.append(p.second);
        }
    }
    if (plateauFills.size() < 2) {
        return -1.0;
    }
    std::sort(plateauFills.begin(), plateauFills.end());
    const int pn = plateauFills.size();
    const double plateauPct = (pn % 2) ? plateauFills[pn / 2]
                                       : 0.5 * (plateauFills[pn / 2 - 1] + plateauFills[pn / 2]);
    const double riseCeilingPct = plateauPct - kStopSubframePlateauMarginPct;
    // The observed straddle: the last sample still below the plateau band, and the first sample
    // at/inside it afterwards. The corner is inside (tLo, tHi] by construction.
    int loIdx = -1;
    for (int i = 0; i < meterCapPhaseCapSamples_.size(); ++i) {
        const auto& p = meterCapPhaseCapSamples_[i];
        if (p.first <= tSnap && p.second < riseCeilingPct) {
            loIdx = i;
        }
    }
    if (loIdx < 0) {
        return -1.0;
    }
    const double tLo = meterCapPhaseCapSamples_[loIdx].first;
    double tHi = -1.0;
    for (int i = loIdx + 1; i < meterCapPhaseCapSamples_.size(); ++i) {
        if (meterCapPhaseCapSamples_[i].second >= riseCeilingPct) {
            tHi = meterCapPhaseCapSamples_[i].first;
            break;
        }
    }
    if (tHi <= tLo) {
        return -1.0;
    }
    // Rise fit: the trailing STRICTLY-RISING run ending at tLo (walking back through a flat or
    // falling sample would let a first-stage plateau corrupt a second-stage rise under
    // stopReopenCorroborate — the flat samples belong to the previous stage, not this rise).
    QVector<QPair<double, double>> rise;
    rise.append(meterCapPhaseCapSamples_[loIdx]);
    for (int i = loIdx - 1; i >= 0 && rise.size() < kStopSubframeRiseFitSamples; --i) {
        const auto& p = meterCapPhaseCapSamples_[i];
        if (p.second < rise.last().second - 1e-9 && p.first < rise.last().first) {
            // (walking backwards, so "previous sample strictly below the later one")
            rise.append(p);
        } else {
            break;
        }
    }
    if (rise.size() < 2) {
        return -1.0;
    }
    // Least-squares line through the rising run (n <= 4, so the naive sums are exact enough).
    double st = 0.0, sf = 0.0, stt = 0.0, stf = 0.0;
    const double t0 = rise.last().first;   // rebase for conditioning
    for (const auto& p : rise) {
        const double t = p.first - t0;
        st += t;
        sf += p.second;
        stt += t * t;
        stf += t * p.second;
    }
    const double n = static_cast<double>(rise.size());
    const double denom = n * stt - st * st;
    if (denom <= 1e-9) {
        return -1.0;
    }
    const double slope = (n * stf - st * sf) / denom;
    if (!std::isfinite(slope) || slope < kStopSubframeMinRiseSlopePctPerMs) {
        return -1.0;
    }
    const double intercept = (sf - slope * st) / n;
    const double tCorner = t0 + (plateauPct - intercept) / slope;
    if (!std::isfinite(tCorner)) {
        return -1.0;
    }
    return std::clamp(tCorner, tLo, tHi);
}

void AutomationEngine::recordPhaseConstantSample()
{
    // [ORION_TIP_PHASE] One completed landing -> one observation of the PHYSICAL constant.
    //
    // WHAT THE STOP DETECTOR IS AND WHY ITS TWO CONSTANTS ARE WHAT THEY ARE. The meter does not
    // halt cleanly; it dithers ~1 pp at the plateau and creeps asymptotically into it, and a
    // minority of rises stall for ~200 ms partway up and then tick again. Sweeping the offline
    // estimator over the 61 measured landings (step = growth that re-opens the stop, quiet =
    // confirmation window):
    //     step 0.75 pp -> rMAD 24.9   (chases the plateau dither)
    //     step 1.00 pp -> rMAD 13.0
    //     step 1.50 pp -> rMAD 10.3
    //     step 2.00 pp -> rMAD  9.6   <- flat optimum, and stable across quiet 80-170 ms
    //     quiet 250 ms -> sd 115      (swallows the second stage of a two-stage rise)
    // The shipped values are that optimum. They are config, not literals, because they are an
    // estimator choice rather than a law.
    //
    // Every gate below DROPS the observation rather than repairing it, for the same reason
    // recordLandingLeadSample does: a repaired sample is a guess wearing a measurement's
    // clothes, and this one feeds the aim.
    if (!config_.tipPhaseEnabled) {
        return;
    }
    if (meterCapPhaseAnchorMs_ < 0.0 || !meterCapPhaseStopConfirmed_
        || meterCapPhaseStopMs_ < 0.0) {
        return;   // no dated anchor, or the rise never demonstrably ended -> no evidence
    }
    // [ORION_STOP_SUBFRAME] Flag ON: measure against the refined sub-frame stop, re-centred so
    // the learned constant keeps the mean the seed/constant were calibrated against (flipping
    // the flag removes dating noise; it must never move the aim). Refinement failure falls back
    // to the snapped date, which the re-centring makes commensurable by construction.
    double stopForMeasurementMs = meterCapPhaseStopMs_;
    double stopSubframeRawMs = -1.0;
    if (config_.stopDatingSubframe) {
        stopSubframeRawMs = refinePhaseStopSubframeMs();
        if (stopSubframeRawMs >= 0.0) {
            stopForMeasurementMs = stopSubframeRawMs + kStopSubframeRecenterMs;
        }
    }
    const double rawObservedMs = stopForMeasurementMs - meterCapPhaseAnchorMs_;
    // [ORION_TIP_PHASE_LADDER] NORMALISE TO THE BASE ANCHOR BEFORE LEARNING. A shot dated at
    // level 40 legitimately has ~52 ms less animation left than one dated at 30, so its raw
    // observation is not commensurable with the rest of the window. Adding the level adjustment
    // back expresses every sample at tipPhaseAnchorPct, which is the level the learned constant
    // is defined at and the level effectiveTipPhaseConstantMs() hands back out.
    //
    // Doing this BEFORE the min/max band check matters in both directions: an honest level-40
    // sample must not be discarded as implausibly short, and a genuinely mis-read stop must not
    // be laundered into the band by the correction.
    // [ORION_TYPE_TRIM] SUBTRACT THE PER-TYPE TRIM BEFORE THE BAND CHECK, symmetric with the
    // decision adding it. A fade whose animation genuinely runs trim ms short then normalises
    // to the SAME base-scale value a Standstill produces, so the pooled learner window stays
    // type-neutral and cannot walk the base constant toward whichever type the session happened
    // to contain -- which is exactly how the learner would otherwise fight the trim (fade-heavy
    // session drags the pooled median down, the trim then double-counts, the next standstill
    // fires early). 0 for every type with the flag off (default), keeping this line inert.
    const double typeTrimMs = tipPhaseTypeTrimMs(meterCapShotType_);
    const double observedMs = std::isfinite(rawObservedMs)
        ? rawObservedMs - tipPhaseLevelAdjustmentMs(meterCapPhaseAnchorLevelPct_) - typeTrimMs
        : rawObservedMs;
    // [ORION_DEV_FIRE_OFFSET] The stop rides the release (the meter freezes when the game
    // registers the press), so a deliberately displaced fire displaces this observation by the
    // commanded offset. While the sweep hook is armed NOTHING it produced may teach the aim:
    // the sample is still logged (the sweep analysis wants it) but never accepted.
    const bool devOffsetFenced = devFireOffsetArmed_;
    const bool accepted = std::isfinite(observedMs)
        && observedMs >= config_.tipPhaseLearnMinMs
        && observedMs <= config_.tipPhaseLearnMaxMs
        && !devOffsetFenced;
    // [ORION_PHASE_PORTABILITY] ONE LINE PER LANDING, ALWAYS, ON EVERY INSTALL.
    //
    // WHY THIS EXISTS. The shipped animation constant (tipPhaseConstantMs) is a property of the
    // GAME, not of the machine -- it differences two detector-clock frame times and never reads
    // the lead -- so in principle it ships globally while the lead stays a per-machine slider.
    // "In principle" is doing real work in that sentence: the 2026-08-05 re-aim to 399 was
    // derived on ONE rig, and there is no second rig to check it against. If any part of that
    // +12 was quietly absorbing this machine's lead being short, a correctly-tuned customer
    // would land LATE.
    //
    // The plan is to let the demo answer it -- many machines instead of one. That only works if
    // a returned log actually CONTAINS the measurement, and until now it did not: the
    // "Tip phase measurement:" line below publishes only after the learner's 10-sample window
    // fills, and says nothing at all about a rejected sample. A demo user's first nine shots,
    // and every mis-read stop, were invisible. The per-frame CSV that carries this offline is
    // behind ORION_DETCSV, which the dev launcher sets and no customer ever will.
    //
    // So: raw and normalised together (their difference is the ladder level adjustment),
    // the level actually used, and whether the learner took it. A handful of these lines from
    // any install answers "is 399 the animation, or is it this rig?" directly. Cost is one line
    // per completed shot -- not per frame -- so it is free next to the existing per-shot logging.
    // [ORION_STOP_SUBFRAME][ORION_DEV_FIRE_OFFSET] Extra fields appear ONLY while their feature
    // is active, keeping the flag-OFF line byte-identical. Inserted before shot_type, which every
    // existing parser reaches via a non-greedy skip (tools/timing RE_PHASE and kin).
    QString extraFields;
    if (config_.stopDatingSubframe) {
        extraFields += QStringLiteral("stop_subframe=%1 stop_snap_shift_ms=%2 ")
                           .arg(stopSubframeRawMs >= 0.0 ? 1 : 0)
                           .arg(meterCapPhaseStopMs_ - stopForMeasurementMs, 0, 'f', 2);
    }
    if (devOffsetFenced) {
        extraFields += QStringLiteral("dev_offset_fence=1 dev_offset_ms=%1 ")
                           .arg(lastReleaseDevOffsetMs_, 0, 'f', 2);
    }
    // [ORION_TYPE_TRIM] Only while the feature is live for THIS sample, keeping the flag-OFF
    // (and untrimmed-type) line byte-identical for every existing parser.
    if (typeTrimMs != 0.0) {
        extraFields += QStringLiteral("type_trim_ms=%1 ").arg(typeTrimMs, 0, 'f', 1);
    }
    emit engineDiagnostic(QStringLiteral(
        "PHASE SAMPLE: raw_ms=%1 normalized_ms=%2 anchor_pct=%3 accepted=%4 "
        "band_ms=%5..%6 shipped_const_ms=%7 effective_const_ms=%8 %9shot_type=%10")
                              .arg(rawObservedMs, 0, 'f', 1)
                              .arg(observedMs, 0, 'f', 1)
                              .arg(meterCapPhaseAnchorLevelPct_, 0, 'f', 1)
                              .arg(accepted ? 1 : 0)
                              .arg(config_.tipPhaseLearnMinMs, 0, 'f', 0)
                              .arg(config_.tipPhaseLearnMaxMs, 0, 'f', 0)
                              .arg(config_.tipPhaseConstantMs, 0, 'f', 1)
                              .arg(effectiveTipPhaseConstantMs(), 0, 'f', 1)
                              .arg(extraFields)
                              .arg(meterCapShotType_.isEmpty() ? QStringLiteral("unknown")
                                                               : meterCapShotType_));
    if (!accepted) {
        return;   // outside the physically observed band -> a mis-read stop, not a slow shot
    }
    phaseConstantSamplesMs_.append(observedMs);
    const int window = std::max(1, config_.tipPhaseLearnWindow);
    while (phaseConstantSamplesMs_.size() > window) {
        phaseConstantSamplesMs_.removeFirst();
    }
    const int n = phaseConstantSamplesMs_.size();
    if (n < std::max(1, config_.tipPhaseLearnMinSamples)) {
        // Below this there is no claim worth making at all: one or two landings cannot
        // distinguish a different animation from ordinary shot-to-shot spread.
        return;
    }
    QVector<double> sorted = phaseConstantSamplesMs_;
    std::sort(sorted.begin(), sorted.end());
    const double medianMs = (n % 2) ? sorted[n / 2]
                                    : 0.5 * (sorted[n / 2 - 1] + sorted[n / 2]);
    // [ORION_AIM_FREEZE] The frozen manual aim vs what this session is actually measuring. Half
    // a learner window is enough evidence to speak (per-type rMAD 5-11 ms -> median SE ~3-5 ms
    // at n=10, against a 20 ms threshold). 2026-08-08 production: the manual value was 70 ms
    // below the live median all session and nothing said so -- the user tuned Shot Lead against
    // an aim the rig's own instrument refuted, which is what walked the pair unschedulable.
    if (config_.tipPhaseAimFrozen && frozenAimPhysicalMs_ > 0.0 && n * 2 >= window) {
        maybeWarnTipTimingDivergence(frozenAimPhysicalMs_, medianMs);
    }
    // [ORION_PHASE_COLD_START] SHRINK toward the observation instead of waiting for a full window.
    //
    // This used to publish NOTHING until the window was full (10 landings) and then switch to the
    // median in one step. The justification was "the seed is already mean-neutral, so waiting
    // costs nothing" -- and that is true only while the seed MATCHES THE ANIMATION being played.
    //
    // It does not, in the case the owner hit: testing on another build with different jumpshots,
    // "a rough cold start but tip timing started to work". Different jumpshot animations have
    // different lengths, so the constant is per-animation; on an animation the seed does not fit,
    // the old gate spent TEN mistimed shots before it would use a single thing it had measured.
    // Every new install, every jumpshot change, and every equipment change pays that.
    //
    // Shrinkage removes the cliff without pretending three samples are ten: the estimate moves
    // n/window of the way from the seed to the observed median, so it is continuous, it equals
    // the median exactly at n == window (identical to the old behaviour from there on), and it
    // introduces no new tuning constant beyond the minimum-sample floor.
    //
    // The arithmetic favours it well before the window fills. Measured per-shot-type rMAD is
    // 5-11 ms, so the standard error of the median at n=3 is ~8 ms; a seed that is 20-30 ms wrong
    // for the current animation is far worse than that. At n=3 the estimate takes 30%% of the
    // correction -- roughly 6-9 ms of bias removed for ~2 ms of added noise -- and the ratio only
    // improves as n grows. It is also bounded in the failure direction: a bad early sample can
    // move the aim by at most (sample - seed) * n/window, and the plausibility band above has
    // already rejected anything outside 240-430 ms.
    if (n < window) {
        // SHRINK FROM WHAT THIS SESSION STARTED WITH, NOT FROM THE HARDCODED SEED.
        //
        // THE BUG THIS FIXES, introduced in the same commit that added persistence. applyConfig()
        // restores learnedPhasePhysicalMs_ from learning.json, and this line then overwrote it on
        // the THIRD accepted landing with a value shrunk from tipPhaseSeedPhysicalMs -- discarding
        // 70% of everything the previous session had measured and snapping back toward the seed.
        // Persistence was shipped and defeated in one change; the stated purpose above
        // ("the constant belongs to the equipped jumpshot, not to the process") was not achieved.
        //
        // Basing the shrink on the restored prior makes a returning session genuinely warm: with
        // nothing to correct the estimate simply stays where it was, and it still converges away
        // at n/window when the animation has actually changed. Falls back to the seed on a first
        // ever run, which is the only case that should start cold.
        //
        // Worst-case exposure of the old behaviour, analytically: the learn band floors at 240 ms,
        // so a single third-landing sample at the floor could drag the effective constant by tens
        // of ms away from a value the previous session had converged on over 20 landings.
        const double shrinkBase = phaseShrinkBaseMs_ > 0.0
            ? phaseShrinkBaseMs_ : config_.tipPhaseSeedPhysicalMs;
        const double shrink = static_cast<double>(n) / static_cast<double>(window);
        learnedPhasePhysicalMs_ = shrinkBase + (medianMs - shrinkBase) * shrink;
    } else {
        learnedPhasePhysicalMs_ = medianMs;
    }
    // [ORION_AIM_FREEZE] (settings tip_phase_aim_frozen, default OFF): hold the aim still for the
    // duration of a session.
    //
    // MEASURED 2026-08-06, the 46/50 counted batch: across 70 releases the effective aim constant
    // walked 439.2 -> 431.0 (-8.2ms, range 9.2ms) while the landing spread was only 10.5ms sd. The
    // aim therefore moved most of a sigma DURING the batch, and the session's second half measured
    // materially worse than its first (sd 8.93 -> 11.91). With a 20-sample median window and 70
    // releases the window turns over ~3x per session, and each turnover nudges the aim.
    //
    // Whether that walk is adaptation or noise-chasing depends on whether the animation really
    // varies shot to shot -- and the 2026-08-06 decomposition showed the apparent variance was
    // dominated by INSTRUMENT noise (stop-dating alone measured 7.7ms snapped), not by the game.
    // Chasing it is then pure harm. This flag keeps the restored/measured prior fixed: samples are
    // still collected, logged and persisted (so a session still MEASURES the animation and the next
    // launch starts warm), but the value the decision path consumes stops moving mid-session.
    //
    // Deliberately NOT the default: on a genuinely different jumpshot the learner is what finds the
    // new constant, and freezing a wrong prior would be worse than a drifting right one. This is a
    // DEMO/batch tool -- freeze once the aim is known good, and clear it when the animation changes.
    if (config_.tipPhaseAimFrozen && frozenAimPhysicalMs_ > 0.0) {
        learnedPhasePhysicalMs_ = frozenAimPhysicalMs_;
    }
    // [ORION_PHASE_COLD_START] Persist it. Only from a FULL window: a shrunk partial estimate is
    // deliberately a compromise with the seed, and writing that to disk would seed the next
    // session from a half-converged number and slow its convergence rather than removing it.
    if (n >= window) {
        // [ORION_ANCHOR_BASE20] Persist canonically at base 30 regardless of the active
        // regime -- see phasePriorShiftMs(). Persisting the base-20 value raw would make a
        // later flag-off session restore a prior ~58ms long and fire every shot late.
        emit phaseConstantUpdated(learnedPhasePhysicalMs_ - phasePriorShiftMs());
        // [ORION_AIM_FREEZE] Persist the MEASURED median separately. With the freeze on, the
        // emit above deliberately re-persists the frozen manual value (the Tip Timing card's
        // persistence contract, pinned by tipPhaseAimFrozenPersistsUserValue) -- which used to
        // mean the session's own measurement was discarded at the last instruction and the
        // manual-vs-measured disagreement was invisible across restarts. Same canonical frame
        // as the aim slot; consumed only by the restore-time divergence warning.
        emit phaseMeasuredMedianUpdated(medianMs - phasePriorShiftMs());
    }
    emit engineDiagnostic(QStringLiteral(
        "Tip phase measurement: physical_median_ms=%1 n=%2 sample_ms=%3 anchor_pct=%4 "
        "aim_offset_ms=%5 effective_const_ms=%6")
                              .arg(medianMs, 0, 'f', 1)
                              .arg(n)
                              .arg(observedMs, 0, 'f', 1)
                              .arg(config_.tipPhaseAnchorPct, 0, 'f', 1)
                              .arg(config_.tipPhaseConstantMs - config_.tipPhaseSeedPhysicalMs,
                                   0, 'f', 1)
                              .arg(effectiveTipPhaseConstantMs(), 0, 'f', 1));
}

void AutomationEngine::recordLandingLeadSample(double peakFillPct, double fillAtReleasePct)
{
    // [ORION_USER_LEAD 2026-08-08] DIAGNOSTIC ONLY. This function once claimed travel_pp
    // (peak_fill - fill_at_submit) / velocity "IS this install's true end-to-end lead" and
    // seeded the Shot Lead from it. That instrument is REFUTED: the release does not move the
    // meter (2026-08-04 measurement -- release fill is uncorrelated with peak/f_stop), so the
    // quantity is capture-side and circular, not end-to-end; and live on 2026-08-08 its own
    // median drifted 225 -> 300 within seven minutes while advertising itself as a measurement
    // ("Shot lead measured on your setup: 225 ms" at 03:19:42Z, on the session whose validated
    // estimator later measured 197.1 +/- 3.2). actuationLeadMeasured is now published from the
    // marker-anchored VALIDATED estimator authority at the telemetry ingestion site; nothing
    // here reaches the Shot Lead any more. The "Lead measurement:" line below is retained so
    // the proxy itself stays analysable offline.
    //
    // Every gate below still drops a sample rather than repairing it -- a repaired sample is a
    // guess wearing a measurement's clothes, even in a diagnostic.
    // [ORION_DEV_FIRE_OFFSET] peak - fill_at_submit rides the (displaced) release; while the
    // sweep hook is armed this cannot be evidence about the lead.
    if (devFireOffsetArmed_) {
        return;
    }
    const double velocity = meterCapRiseVelocityPctPerMs_;
    if (!std::isfinite(velocity) || velocity < 0.05 || velocity > 1.0) {
        return;   // no plausible per-shot rise (plateau / blip / never captured) -> no evidence
    }
    if (!std::isfinite(peakFillPct) || !std::isfinite(fillAtReleasePct)
        || fillAtReleasePct < 0.0) {
        return;
    }
    // A meter that pinned the top TRUNCATES its own travel: the command kept flying after the fill
    // stopped rising, so peak - submit under-reports the lead. Those samples would bias the seed
    // low, i.e. toward the late releases this control exists to fix.
    if (peakFillPct >= 99.0) {
        return;
    }
    const double travelPp = peakFillPct - fillAtReleasePct;
    if (travelPp <= 0.0) {
        return;
    }
    const double leadMs = travelPp / velocity;
    if (!std::isfinite(leadMs) || leadMs < config_.actuationLeadMinMs
        || leadMs > config_.actuationLeadMaxMs) {
        return;   // outside the band this control can install -> a bad read, not a lead
    }
    landingLeadSamplesMs_.push_back(leadMs);
    // Rolling window of 2N: deep enough for a robust median, shallow enough that the answer still
    // describes the session's current network/capture conditions.
    const int window = std::max(1, 2 * config_.actuationLeadSeedMinSamples);
    while (static_cast<int>(landingLeadSamplesMs_.size()) > window) {
        landingLeadSamplesMs_.pop_front();
    }
    const int n = static_cast<int>(landingLeadSamplesMs_.size());
    if (n < config_.actuationLeadSeedMinSamples) {
        return;
    }
    // MEDIAN, not mean: one mis-read landing (a fade whose peak was mis-attributed) moves a mean
    // by sample/n and a median by nothing.
    std::vector<double> sorted(landingLeadSamplesMs_.begin(), landingLeadSamplesMs_.end());
    std::sort(sorted.begin(), sorted.end());
    const double medianMs = (n % 2) ? sorted[n / 2]
                                    : 0.5 * (sorted[n / 2 - 1] + sorted[n / 2]);
    emit engineDiagnostic(QStringLiteral(
        "Lead measurement: median_ms=%1 n=%2 sample_ms=%3 travel_pp=%4 velocity_pct_ms=%5")
                              .arg(medianMs, 0, 'f', 1)
                              .arg(n)
                              .arg(leadMs, 0, 'f', 1)
                              .arg(travelPp, 0, 'f', 2)
                              .arg(velocity, 0, 'f', 4));
    // [ORION_USER_LEAD 2026-08-08] Deliberately NO actuationLeadMeasured emit here -- see the
    // refutation note at the top of this function. Reinstating it would re-seed customer Shot
    // Leads from a circular proxy.
}

void AutomationEngine::evaluatePostReleaseMeter()
{
    const QString shotType = meterCapShotType_;
    const int seq = meterCapSeq_;
    const double peak = meterCapPeakFillPct_;
    const QVector<MeterCalSample> samples = meterCapSamples_;
    meterCapActive_ = false;                   // close up-front so any early return resets state
    meterCapSamples_.clear();
    if (shotType.isEmpty()) {
        return;
    }

    // [ORION_COURT_POSITION] Largest single-frame normalized center move inside THIS shot's
    // capture window, over the accepted samples that carried usable bbox_wh. It is the same
    // quantity meterSettledFrames() thresholds in pixels to find a static-bbox run, expressed
    // frame-normalized so it is comparable across resolutions and across shots. -1 = fewer than
    // two usable samples. Read it alongside meter_x/meter_y: a shot whose meter never stopped
    // sliding is a shot whose court position is smeared, and must not be pooled with settled
    // ones when testing whether timing error depends on position.
    double landingMeterJump = -1.0;
    {
        double prevNx = -1.0;
        double prevNy = -1.0;
        for (const auto& s : samples) {
            if (!s.accepted || s.nx < 0.0 || s.ny < 0.0) {
                prevNx = -1.0;
                prevNy = -1.0;
                continue;
            }
            if (prevNx >= 0.0 && prevNy >= 0.0) {
                landingMeterJump = std::max(landingMeterJump,
                                            std::hypot(s.nx - prevNx, s.ny - prevNy));
            }
            prevNx = s.nx;
            prevNy = s.ny;
        }
    }

    // [ORION_USER_LEAD] Fold this landing into the measured end-to-end lead BEFORE the settle
    // gate: the peak is captured over the whole window and is valid whether or not the frozen
    // marker settled, so a fade that can never be GRADED still tells us how long the command took
    // to land. Its own sanity band rejects anything that isn't a clean read.
    recordLandingLeadSample(peak, meterCapFillAtReleasePct_);
    // [ORION_TIP_PHASE] and fold the same landing into the learned animation constant. Placed
    // beside the lead sample and before the settle gate for the identical reason: the anchor and
    // the end of the rise are both valid whether or not the frozen marker ever settled, so a
    // fade that can never be GRADED still measures the animation.
    recordPhaseConstantSample();

    // Grade ONLY the SETTLED frozen-marker frames: a static-bbox, stable-fill run (the player landed
    // and the meter stopped sliding / finished bouncing). A moving fade/Go-To meter never settles ->
    // empty -> SKIP (no false EXCELLENT off a moving read). This is the meter-settle fix; the peak
    // (meterCapPeakFillPct_, captured over the whole shot incl. the rise/bounce) still drives the
    // EARLY-vs-LATE call below.
    const QVector<MeterCalSample> good = meterSettledFrames(samples);
    if (good.size() < config_.meterMinSettledFrames) {
        // Emit the landing evidence even when the shot cannot be GRADED. The peak is captured
        // over the whole capture window and is valid regardless of whether the marker settled,
        // and a batch that never settles is exactly the case a reader must be able to see.
        emit engineDiagnostic(QStringLiteral(
            "Release landing: seq=%1 graded=0 reason=no_settled_run peak_fill=%2 "
            "fill_at_rel=%3 travel_pp=%4 settled_n=%5 samples_n=%6 "
            // [ORION_COURT_POSITION] APPEND-ONLY. Inserted BEFORE shot= on purpose: shot= is a
            // shot-type label that legitimately contains a space ("Left Fade", "No Dip"), so it
            // can only be parsed as the rest of the line and must stay last. Every pre-existing
            // key keeps its name and value; no key=value lookup changes.
            "meter_x=%7 meter_y=%8 meter_jump=%9 shot=%10")
                                  .arg(seq)
                                  .arg(peak, 0, 'f', 2)
                                  .arg(meterCapFillAtReleasePct_, 0, 'f', 2)
                                  .arg(peak - meterCapFillAtReleasePct_, 0, 'f', 2)
                                  .arg(good.size())
                                  .arg(samples.size())
                                  .arg(meterCapNormXAtRel_, 0, 'f', 4)
                                  .arg(meterCapNormYAtRel_, 0, 'f', 4)
                                  .arg(landingMeterJump, 0, 'f', 4)
                                  .arg(shotType));
        // [ORION_PRESS_ANCHOR] L-confidence observation: the landing could not be graded,
        // but the press and (usually) the stop are still dated. Logged for the census,
        // never fed to the learner.
        emitPressTipObservation(seq, shotType, /*graded=*/false, /*greenObserved=*/false,
                                samples);
        return;   // SKIP: the meter never settled into a readable frozen marker this shot
    }
    auto median = [](QVector<double> v) -> double {
        std::sort(v.begin(), v.end());
        const int n = v.size();
        if (n == 0) return std::numeric_limits<double>::quiet_NaN();
        return (n % 2) ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
    };
    // `good` is the SETTLED frozen-marker run (static bbox + stable fill, post-bounce). Its median
    // pins the frozen marker fill + green window robustly for both static (Standstill) and
    // post-landing (fade/Go-To) meters.
    QVector<double> fills, starts, ends, centers;
    // [ORION_GREEN_TRUTH] Separate the OBSERVED green readings from the substituted ones. See
    // MeterCalSample::greenConfidence: a failed scan silently yields the user's Release Threshold
    // slider, and mixing those into a median produces a "green window" that is partly a UI
    // setting. The observed-only width is the quantity the tip-timing ceiling actually divides by,
    // and it has never been logged.
    QVector<double> obsStarts, obsEnds;
    for (const auto& s : good) {
        fills.append(s.fillPct);
        starts.append(s.greenStartPct);
        ends.append(s.greenEndPct);
        centers.append(s.greenCenterPct);
        if (s.greenConfidence > 0.0 && s.greenStartPct >= 0.0 && s.greenEndPct >= 0.0) {
            obsStarts.append(s.greenStartPct);
            obsEnds.append(s.greenEndPct);
        }
    }
    const double fill = median(fills);
    const double gs = median(starts);
    const double ge = median(ends);
    const double gc = median(centers);
    const int greenObsN = obsStarts.size();
    const double obsGs = greenObsN > 0 ? median(obsStarts)
                                       : std::numeric_limits<double>::quiet_NaN();
    const double obsGe = greenObsN > 0 ? median(obsEnds)
                                       : std::numeric_limits<double>::quiet_NaN();
    const double obsWidth = greenObsN > 0 ? (obsGe - obsGs)
                                          : std::numeric_limits<double>::quiet_NaN();
    if (!std::isfinite(fill) || !std::isfinite(gc) || peak < config_.meterMinPeakFillPct) {
        // [ORION_SILENT_FAIL] The capture SETTLED (it passed the no_settled_run gate above,
        // which logs) and then this later gate returned without a landing line at all -- a
        // near-empty-bar misfire is exactly the shot a hand counter needs explained. Same
        // graded=0 shape as above; median_fill / green_center are inserted BEFORE shot= (the
        // append-only convention: shot= carries a space-bearing label and must stay last) so
        // a reader can tell a non-finite median from a merely low peak.
        emit engineDiagnostic(QStringLiteral(
            "Release landing: seq=%1 graded=0 reason=low_peak_or_nan peak_fill=%2 "
            "fill_at_rel=%3 travel_pp=%4 settled_n=%5 samples_n=%6 "
            "median_fill=%7 green_center=%8 "
            "meter_x=%9 meter_y=%10 meter_jump=%11 shot=%12")
                                  .arg(seq)
                                  .arg(peak, 0, 'f', 2)
                                  .arg(meterCapFillAtReleasePct_, 0, 'f', 2)
                                  .arg(peak - meterCapFillAtReleasePct_, 0, 'f', 2)
                                  .arg(good.size())
                                  .arg(samples.size())
                                  .arg(fill, 0, 'f', 2)
                                  .arg(gc, 0, 'f', 2)
                                  .arg(meterCapNormXAtRel_, 0, 'f', 4)
                                  .arg(meterCapNormYAtRel_, 0, 'f', 4)
                                  .arg(landingMeterJump, 0, 'f', 4)
                                  .arg(shotType));
        // [ORION_PRESS_ANCHOR] L-confidence: settled but ungradable landing (see above).
        emitPressTipObservation(seq, shotType, /*graded=*/false, /*greenObserved=*/false,
                                samples);
        return;
    }

    // [ORION_LANDING_TRUTH] Seq-paired POST-RELEASE ground truth — the measurement the log has
    // never carried. "Release timing:"'s peakFill is read at command-submit time, when the meter
    // has not yet travelled, so it is byte-identical to fillAtRel on every release ever logged
    // (verified across all 475 rows in logs/orion_native.log{,.1}); it cannot grade anything.
    // These fields can:
    //   peak_fill     - highest fill the meter reached, i.e. WHERE THE RELEASE LANDED
    //   settled_fill  - where the frozen marker came to rest afterwards
    //   green_*       - the detected green window for this shot, so "landed in green" is decidable
    //   travel_pp     - peak - fill_at_rel, the distance the meter covered during the lead;
    //                   divided by the meter velocity this is a direct read of the TRUE lead
    // Direction of proof for the horizon de-bias: peak_fill should hold its level while its
    // SPREAD narrows. A drop in mean peak_fill would mean the aim moved earlier, which this
    // change is specifically built not to do.
    emit engineDiagnostic(QStringLiteral(
        "Release landing: seq=%1 graded=1 peak_fill=%2 settled_fill=%3 fill_at_rel=%4 "
        "travel_pp=%5 green_start=%6 green_end=%7 green_center=%8 settled_n=%9 "
        // [ORION_COURT_POSITION] APPEND-ONLY, and inserted BEFORE shot= for the same reason as
        // the graded=0 line above (shot= holds a space-bearing label and must remain last).
        //   meter_x / meter_y - frame-normalized meter bbox center at the instant the release
        //                       command was SUBMITTED; the meter is player-anchored, so this is
        //                       a direct proxy for where on the court the shot was taken.
        //   meter_jump        - largest single-frame normalized center move in this shot's
        //                       capture window (settled vs still-sliding meter).
        // -1 in any of the three = the evidence was not available for this shot.
        // WHAT IT WOULD PROVE: grouping a batch's rows on meter_x answers whether travel_pp /
        // peak_fill error varies with court position — the half-court question — with no special
        // capture and no new detector. Nothing consumes these back into timing.
        // [ORION_GREEN_TRUTH] APPEND-ONLY, before shot= (which holds a space-bearing label and
        // must stay last). green_obs_n = frames whose green scan actually SUCCEEDED;
        // green_obs_start/width are the median over only those. When green_obs_n is 0 the
        // green_start/green_end above are the Release Threshold slider, NOT a measurement --
        // which was true of 72.3% of landings before this line existed.
        "green_obs_n=%10 green_obs_start=%11 green_obs_width=%12 "
        // [ORION_LANDING_FEATURES 2026-08-12] APPEND-ONLY, before shot= (space-bearing label, must
        // stay last). The pipeline state AT THE FIRE INSTANT, so a graded landing can finally be
        // regressed against the conditions that produced it.
        //   vel_at_rel   - meter rise velocity (pp/ms) at release-command submit
        //   frame_age_ms - how OLD the deciding capture sample was
        //   rtt_ms       - network offset latched for this shot
        // -1 = unavailable. WHAT IT WOULD PROVE: whether the per-shot-type residual (Fades and
        // Go-To show corr(fill_at_rel, peak) ~ +0.5 while Standstill is ~0.0) tracks RTT, tracks
        // velocity, or is random -- which decides whether a lead controller should be feed-forward
        // or purely feedback. Before this line those three were computed on every shot and written
        // on none, and the rich at-fire dump (TIP DEADLINE DECISION) fires only on MISSES, so the
        // graded shots carried no record of their own conditions.
        // Nothing consumes these back into timing.
        "vel_at_rel=%13 frame_age_ms=%14 rtt_ms=%15 "
        "meter_x=%16 meter_y=%17 meter_jump=%18 shot=%19")
                              .arg(seq)
                              .arg(peak, 0, 'f', 2)
                              .arg(fill, 0, 'f', 2)
                              .arg(meterCapFillAtReleasePct_, 0, 'f', 2)
                              .arg(peak - meterCapFillAtReleasePct_, 0, 'f', 2)
                              .arg(gs, 0, 'f', 2)
                              .arg(ge, 0, 'f', 2)
                              .arg(gc, 0, 'f', 2)
                              .arg(good.size())
                              .arg(greenObsN)
                              .arg(greenObsN > 0 ? obsGs : -1.0, 0, 'f', 2)
                              .arg(greenObsN > 0 ? obsWidth : -1.0, 0, 'f', 2)
                              .arg(meterCapRiseVelocityPctPerMs_, 0, 'f', 5)
                              .arg(meterCapFrameAgeAtRelMs_, 0, 'f', 2)
                              .arg(meterCapNetworkOffsetAtRelMs_, 0, 'f', 2)
                              .arg(meterCapNormXAtRel_, 0, 'f', 4)
                              .arg(meterCapNormYAtRel_, 0, 'f', 4)
                              .arg(landingMeterJump, 0, 'f', 4)
                              .arg(shotType));

    // [ORION_PRESS_ANCHOR] Graded landing -> H/M-confidence press->tip observation. H needs
    // the green window to have been OBSERVED this shot (green_obs_n > 0), not substituted
    // from the Release Threshold slider — the same distinction [ORION_GREEN_TRUTH] logs.
    emitPressTipObservation(seq, shotType, /*graded=*/true,
                            /*greenObserved=*/greenObsN > 0, samples);

    // [ORION_GRADE_V2] Phase-2 trajectory grading replaces the settled-fill rules below. Same
    // settle/sanity gates (a moving meter is still skipped above); classification + the sole-
    // writer trim live in the v2 evaluator. Flag OFF -> the legacy rules run byte-identically.
    if (config_.gradeV2Enabled) {
        evaluatePostReleaseMeterV2(shotType, seq, peak, fill, gs, ge, gc, samples);
        return;
    }

    // Grade the post-release shot meter vs the green WINDOW using BOTH the settled fill and the PEAK
    // the meter reached this shot. The meter is NOT a static frozen marker (recording-confirmed
    // 2026-06-07): a made/EXCELLENT shot HOLDS the meter high near green (~92%), while a LATE shot
    // rises THROUGH green then DEFLATES to ~52%. So:
    //   - settled inside green / within deadband             -> EXCELLENT (hold)
    //   - settled ABOVE green                                -> LATE (overshoot)
    //   - settled BELOW green but the peak REACHED green      -> LATE (deflate: rose into green then fell back)
    //   - settled BELOW green and the peak fell SHORT of green-> EARLY (meter never filled to green)
    // The PEAK gate is what distinguishes a deflate (LATE) from a genuine early shot (EARLY) and keeps
    // this from repeating the old ungated recede->LATE doom loop. Signed errorMs follows the release-lead
    // sign (+ late shortens the clock, - early lengthens it).
    const double fillErr = fill - gc;
    constexpr double kPeakTolPct = 1.0;
    const double winTol = config_.meterGreenWindowTolPct;
    // The PEAK gate (not the recede amount) is the discriminator: a deflate (peak reached green,
    // settled below) is LATE; a meter whose peak never reached green is EARLY. The deflate SHORTEN
    // step is a fixed bounded nudge (see below) because the recede magnitude is uninformative.
    const bool peakReachedGreen = peak >= (gs - kPeakTolPct);
    // EXCELLENT requires the settled fill to land GENUINELY inside the detected green WINDOW [gs,ge]
    // (within a small window-relative tolerance) -- NOT within a wide center deadband. A contested
    // fade's green window shrinks to a sliver near the very top (live 2026-06-08: ~[98,100]); the old
    // +/-7% center deadband stamped a fade that settled ~7% BELOW that sliver as EXCELLENT (false
    // green -> false LOCK -> fades never improved). Window-relative grading separates a real green
    // (settles in/at the window, gap<=~2) from a slightly-late deflate (settles well below it,
    // gap>=~6). Matches user-labeled shots exactly (Standstill 8 green, Left Fade 0 green).
    const bool settledInGreen = (fill >= gs - winTol) && (fill <= ge + winTol);
    double errorMs;
    QString verdict;
    if (settledInGreen) {
        errorMs = 0.0;                                          // settled inside the green window -> EXCELLENT -> hold
        verdict = QStringLiteral("EXCELLENT");
    } else if (fillErr > 0.0) {
        errorMs = std::min(config_.meterMaxErrorMs, fillErr * config_.meterMsPerPct);    // above green -> LATE (overshoot)
        verdict = QStringLiteral("LATE");
    } else if (peakReachedGreen) {
        // Deflate: the meter rose INTO green this shot, then settled well below it -> released LATE.
        // CRITICAL (live Right/Left Fade oscillation fix): the recede AMOUNT does NOT encode HOW late
        // the release was. Once you overtime PAST green the post-shot marker snaps back to ~the same
        // low rest point whether you were 20 ms or 120 ms late, so recede ~ constant for any LATE.
        // The old code scaled the step BY recede, which railed every deflate at meterMaxErrorMs (±160)
        // -> each LATE max-shortened the clock, the next shot went EARLY (-160), and the per-type
        // calibration bang-banged around green and never settled (the noisiest-Right-Fade symptom).
        // The deflate DIRECTION (late) is reliable; the magnitude is not. So apply a BOUNDED, CONSISTENT
        // shorten nudge (meterRecedeLatePct %% -> ms) that walks the clock into the window over a few
        // shots and HOLDS once an EXCELLENT lands, instead of overshooting it every single shot.
        errorMs = config_.meterRecedeLatePct * config_.meterMsPerPct;
        verdict = QStringLiteral("LATE");
    } else {
        errorMs = std::max(-config_.meterMaxErrorMs, fillErr * config_.meterMsPerPct);   // peak short of green -> EARLY
        verdict = QStringLiteral("EARLY");
    }

    // Real ground truth now drives learning; defer the internal releaseFillPct estimate.
    outcomeFeedbackActive_ = true;
    if (config_.bannerCalibration) {
        // Banner calibration: the on-screen TIMING banner is the green-vs-late ground truth (this meter
        // self-grade false-LATEs at the dead-top). Store this grade ONLY as the EARLY/LATE DIRECTION for
        // `seq`; setBannerVerdict() vetoes it (banner GREEN -> EXCELLENT) or confirms it (banner RED). No
        // learn/emit here -- the banner authoritatively grades + emits the shot.
        pendingSelfGradeSeq_ = seq;
        pendingSelfGradeErrorMs_ = errorMs;
        pendingSelfGradeVerdict_ = verdict;
        // Fallback deadline: late enough for a live banner verdict (~1.2-2s post-release) to
        // arrive first, while leaving the self-grade a real margin inside updateShotOutcome's
        // pairing window (release + 3000ms) when it must be applied.
        pendingSelfGradeDeadlineMs_ = (seq == lastReleaseSeq_ && lastReleaseWallMs_ >= 0.0)
            ? lastReleaseWallMs_ + 2400.0
            : nowMs() + 1200.0;
        return;
    }
    if (seq != lastOutcomeConsumedSeq_) {
        lastOutcomeConsumedSeq_ = seq;
        learnFromOutcome(shotType, errorMs);
    }
    // P3.5: in autonomous_vision the per-type ACQUIRE->LOCK maps (learnedOffset/learnCount/greens/misses/
    // phase) are NEVER updated — the ONE global self-measured lead (learnedLatencyMs) is what the mode
    // dials. Emitting the frozen per-type values made the overlay show a stale/zero "learned offset" and a
    // bogus acquire/lock phase for a shot the autonomous learner actually graded. Emit the autonomous-lead
    // state instead: learnedLatencyMs as the learned offset, the global clock, and an "autonomous" phase/
    // anchor label so the overlay reads honest.
    emitShotOutcome(seq, shotType, shotType, verdict, errorMs);
}

// === [ORION_GRADE_V2] Phase-2 trajectory grading (plan A3 + Part-0 M4/M5 gates) ==============
void AutomationEngine::evaluatePostReleaseMeterV2(const QString& shotType, int seq, double peak,
                                                  double fStop, double gs, double ge, double gc,
                                                  const QVector<MeterCalSample>& samples)
{
    // Trajectory classification off the settled freeze (F_stop = settled-run median fill) + the
    // shot's PEAK + the per-sample (tMs, fill) series. The settle gates upstream already
    // rejected moving/unreadable meters, so every branch below reads a genuine frozen marker.
    const double winTol = config_.meterGreenWindowTolPct;
    const bool inGreen = (fStop >= gs - winTol) && (fStop <= ge + winTol);
    QString caseTag;
    QString verdict;
    double errMs = 0.0;
    bool dirOnly = false;
    double slopeLocal = -1.0;
    if (inGreen) {
        // Case G — monotone rise -> freeze INSIDE green: on target.
        caseTag = QStringLiteral("G");
        verdict = QStringLiteral("EXCELLENT");
        errMs = 0.0;
    } else if (peak >= 97.0 && fStop < peak - 1.5) {
        // Case D — rise -> cap>=97 -> deflate -> freeze: LATE, DIRECTION-ONLY. Part-0 M4
        // PROVED F_stop snaps to a per-type rest constant (Go-To ~51, Standstill ~61.5)
        // UNCORRELATED with overtime (p=0.85) — a magnitude computed from the recede is the
        // exact LATE-66 mistake; never reinvent it.
        caseTag = QStringLiteral("D");
        verdict = QStringLiteral("LATE");
        errMs = config_.gradeV2DirOnlyLateMs;
        dirOnly = true;
    } else if (fStop > ge + winTol) {
        // Rise-freeze ABOVE the green band without a cap+deflate (rare: the band top is pinned
        // ~100 live). Direction is reliable (late), magnitude is not — direction-only.
        caseTag = QStringLiteral("O");
        verdict = QStringLiteral("LATE");
        errMs = config_.gradeV2DirOnlyLateMs;
        dirOnly = true;
    } else if (peak <= fStop + 1.5) {
        // Case R — rise -> freeze BELOW green with the peak AT the freeze: EARLY with
        // magnitude. LOCAL rise slope at the freeze, mirroring the oracle's margin-fixed
        // linear label path (Part-0 M5 measured it strictly better than the template
        // inverse on real data): last >= 3 below-freeze samples (1.0pp censor margin),
        // linear fit t = a*f + b -> slope_local = 1/a (pct/ms).
        caseTag = QStringLiteral("R");
        verdict = QStringLiteral("EARLY");
        QVector<double> tsv, fsv;
        for (const auto& s : samples) {
            if (s.accepted && std::isfinite(s.fillPct) && s.tMs > 0.0
                && s.fillPct < fStop - 1.0) {
                tsv.append(s.tMs);
                fsv.append(s.fillPct);
            }
        }
        const int m = static_cast<int>(tsv.size());
        const int tail = std::min(m, 5);
        bool fitOk = false;
        if (tail >= 3 && fsv[m - 1] > fsv[m - tail] + 0.2) {
            double sf = 0.0, st = 0.0, sff = 0.0, sft = 0.0;
            for (int i = m - tail; i < m; ++i) {
                sf += fsv[i];
                st += tsv[i];
                sff += fsv[i] * fsv[i];
                sft += fsv[i] * tsv[i];
            }
            const double den = tail * sff - sf * sf;
            if (den > 1e-9) {
                const double a = (tail * sft - sf * st) / den;   // ms per pp
                if (a > 1e-9) {
                    slopeLocal = std::max(1.0 / a, 0.08);        // pct/ms, floored (M5 gate)
                    fitOk = true;
                }
            }
        }
        if (fitOk) {
            errMs = std::clamp(-(gc - fStop) / slopeLocal, -config_.gradeV2ErrClampMs, 0.0);
        } else {
            errMs = config_.gradeV2DirOnlyEarlyMs;   // unfittable local rise -> direction-only
            dirOnly = true;
        }
    } else {
        // Partial deflate mid-band (peak above the freeze by >1.5pp but never capped >=97):
        // the local slope at the freeze is contaminated by the bounce, so no magnitude —
        // direction from whether the peak reached the green band (the legacy peak gate).
        dirOnly = true;
        if (peak >= gs - 1.0) {
            caseTag = QStringLiteral("D");
            verdict = QStringLiteral("LATE");
            errMs = config_.gradeV2DirOnlyLateMs;
        } else {
            caseTag = QStringLiteral("R");
            verdict = QStringLiteral("EARLY");
            errMs = config_.gradeV2DirOnlyEarlyMs;
        }
    }

    // Dead zone: |err| below the grader's real resolution (fill resolution / deflate rate ~
    // 3-6ms; / knee rise slope ~ 5-10ms) -> report the grade but move NOTHING. Also dead when
    // the peak-to-stop gap at the cap is <= 1.5pp — a green freeze and a micro-deflate are
    // indistinguishable there.
    bool dead = errMs > config_.gradeV2DeadEarlyMs && errMs < config_.gradeV2DeadLateMs;
    if (peak >= 97.0 && (peak - fStop) <= 1.5) {
        dead = true;
    }

    const bool frozen = gradeV2FreezeShots_ > 0;
    if (frozen) {
        --gradeV2FreezeShots_;
    }

    // Sole-writer trim: offset += gain * clamp(err, ±40), cumulative drift bounded to
    // ±gradeV2DriftBoundMs around the bucket's session-start value. Deliberately NOT routed
    // through learnFromOutcome (gated off under the flag) and NOT gated by calibrationFrozen —
    // that freeze protected against the UNRELIABLE legacy self-grade; grading v2 ships behind
    // its own offline-validation flag (default OFF).
    double appliedDelta = 0.0;
    if (!autonomousLiveMeterTimingEnabled() && !dead && !frozen) {
        const double errCl = std::clamp(errMs, -config_.gradeV2ErrClampMs,
                                        config_.gradeV2ErrClampMs);
        const double base = gradeV2OffsetBaseline_.value(shotType, 0.0);
        double& off = config_.shotTypeLearnedOffsetMs[shotType];
        const double next = std::clamp(off + config_.gradeV2Gain * errCl,
                                       base - config_.gradeV2DriftBoundMs,
                                       base + config_.gradeV2DriftBoundMs);
        appliedDelta = next - off;
        off = next;
        emit learningUpdated(config_.shotTypeLearnedOffsetMs);
        gradeV2RecentErr_.push_back(errCl);
        while (static_cast<int>(gradeV2RecentErr_.size()) > 8) {
            gradeV2RecentErr_.pop_front();
        }
        if (gradeV2ArtifactTripped()) {
            gradeV2FreezeShots_ = 10;
            gradeV2RecentErr_.clear();
            emit fusedDiagnostic(QStringLiteral(
                "GradeV2Guard: artifact signature (>=6 consecutive same-sign errs, stdev<3ms) "
                "-> offset updates FROZEN for 10 shots (bucket=%1)").arg(shotType));
        }
    }

    // Consume the release + mark real feedback active so no legacy path (banner fallback,
    // injected verdict, internal fill-vs-target estimate) can re-teach this same outcome.
    outcomeFeedbackActive_ = true;
    lastOutcomeConsumedSeq_ = seq;
    emit fusedDiagnostic(QStringLiteral(
        "GradeV2: seq=%1 bucket=%2 case=%3 verdict=%4 f_stop=%5 peak=%6 green=[%7,%8] "
        "err_ms=%9 dir_only=%10 dead=%11 frozen=%12 delta_ms=%13 slope=%14")
            .arg(seq)
            .arg(shotType, caseTag, verdict)
            .arg(fStop, 0, 'f', 1)
            .arg(peak, 0, 'f', 1)
            .arg(gs, 0, 'f', 1)
            .arg(ge, 0, 'f', 1)
            .arg(errMs, 0, 'f', 1)
            .arg(dirOnly ? 1 : 0)
            .arg(dead ? 1 : 0)
            .arg(frozen ? 1 : 0)
            .arg(appliedDelta, 0, 'f', 2)
            .arg(slopeLocal, 0, 'f', 3));
    emitShotOutcome(seq, shotType, shotType, verdict, errMs);
}

bool AutomationEngine::gradeV2ArtifactTripped() const
{
    // >= 6 CONSECUTIVE same-sign applied errs with stdev < 3ms: a genuine converging stream
    // spreads across magnitudes and shrinks toward the dead zone; a near-constant same-sign
    // stream is the grader feeding back its own fixed artifact (the v2 sibling of the legacy
    // two-mode histogram guard).
    const int n = static_cast<int>(gradeV2RecentErr_.size());
    if (n < 6) {
        return false;
    }
    double sum = 0.0;
    int sign = 0;
    for (int i = n - 6; i < n; ++i) {
        const double e = gradeV2RecentErr_[static_cast<std::size_t>(i)];
        const int s = e > 0.0 ? 1 : (e < 0.0 ? -1 : 0);
        if (s == 0) {
            return false;
        }
        if (sign == 0) {
            sign = s;
        } else if (s != sign) {
            return false;
        }
        sum += e;
    }
    const double mean = sum / 6.0;
    double var = 0.0;
    for (int i = n - 6; i < n; ++i) {
        const double d = gradeV2RecentErr_[static_cast<std::size_t>(i)] - mean;
        var += d * d;
    }
    return std::sqrt(var / 6.0) < 3.0;
}

void AutomationEngine::emitShotOutcome(int seq, const QString& displayType, const QString& key,
                                       const QString& verdict, double errorMs)
{
    const bool autonomous = config_.autonomousVision;
    if (seq > 0 && seq == lastReleaseSeq_
        && lastReleasePhysicalShotEpoch_ != 0 && lastReleaseShotAttempt_ != 0) {
        // [ORION_LEAD_CONFLICT 2026-08-08] grader_truth: APPEND-ONLY. With the grader frozen
        // (the ship config) the meter self-grade is known-degenerate at the dead top -- the
        // constant LATE-66 artifact -- and the controller already labels ITS line "not timing
        // truth" and gates the UI tally. This line carried the same verdict UNLABELED, and it
        // is the line the 2026-08-08 user read before flooring Tip Timing and raising Shot
        // Lead into the unschedulable regime. grader_truth=0 = diagnostic artifact, not a
        // timing measurement.
        const bool graderTruth = !config_.calibrationFrozen || config_.calibrationMode;
        // [ORION_ARMED_SOURCE] APPEND-ONLY (existing parsers keep every position; the
        // controller's grader_truth=0 rewrite matches on a substring and is unaffected).
        // armed_source / armed_sigma_ms are the PROMOTING decision's values, captured at the
        // arm instant and carried token->release->outcome -- NOT re-derived here (shot_ is
        // long reset by the time an outcome arrives). armed_source=none / -1 / token 0 means
        // the release did not fire from an attributed autonomous vision token, so a
        // hand-counted batch can be joined per predictor source afterwards
        // (tools/timing/armed_source_report.py).
        QString armedSource = lastReleaseArmedSource_;
        armedSource.replace(QLatin1Char(' '), QLatin1Char('_'));
        if (armedSource.isEmpty()) {
            armedSource = QStringLiteral("none");
        }
        emit engineDiagnostic(QStringLiteral(
            "Outcome identity: physical_epoch=%1 shot_attempt=%2 release_seq=%3 verdict=%4 "
            "grader_truth=%5 "
            "armed_source=%6 armed_sigma_ms=%7 armed_fill_pct=%8 command_eta_ms=%9 "
            "armed_schedule_token=%10")
                                  .arg(lastReleasePhysicalShotEpoch_)
                                  .arg(lastReleaseShotAttempt_)
                                  .arg(seq)
                                  .arg(verdict)
                                  .arg(graderTruth ? 1 : 0)
                                  .arg(armedSource)
                                  .arg(lastReleaseArmedSigmaMs_, 0, 'f', 3)
                                  .arg(lastReleaseArmedFillPct_, 0, 'f', 2)
                                  .arg(lastReleaseArmedCommandEtaMs_, 0, 'f', 3)
                                  .arg(lastReleaseScheduleToken_));
    }
    emit shotOutcomeLearned(seq, displayType, verdict, errorMs,
                            autonomous ? config_.learnedLatencyMs
                                       : config_.shotTypeLearnedOffsetMs.value(key, 0.0),
                            autonomous ? config_.globalHoldToReleaseMs
                                       : config_.shotTypeFeedforwardMs.value(key, 0.0),
                            config_.shotTypeLearnCount.value(key, 0),
                            autonomous ? QStringLiteral("autonomous")
                                       : (config_.shotTypeCalPhase.value(key, 0) == 1
                                              ? QStringLiteral("lock") : QStringLiteral("acquire")),
                            calGreens_.value(key, 0),
                            calMisses_.value(key, 0),
                            autonomous ? config_.globalAppearToTipMs
                                       : config_.shotTypeMeterToReleaseMs.value(key, 0.0),
                            autonomous ? QStringLiteral("autonomous") : config_.feedforwardAnchor);
}

void AutomationEngine::completeRelease(double now)
{
    shot_.state = HoldState::Cooldown;
    shot_.releaseCompleteMs = now;
    // Go-To needs a longer cooldown so the animation/gather fully resets before the
    // next shot can arm (prevents re-triggering into a half-played animation).
    shot_.cooldownEndMs = now + (shot_.mode == ShotMode::GoToStick
                                     ? config_.gotoCooldownMs
                                     : config_.cooldownMs);
    shot_.released = true;
    if (!shot_.latencyCalibrationProbe) {
        shotsReleased_++;
    }
    emit shotStateChanged(shot_);
}

void AutomationEngine::emitShotAbortIdentity(const char* site, qint64 physicalEpoch,
                                             qint64 shotAttempt, qint64 releaseSeq,
                                             qint64 scheduleToken, const QString& reason,
                                             const QString& shotType)
{
    // [ORION_ABORT_IDENTITY] The five original fields keep their exact names, order and
    // rendering, so every existing reader of this line is unaffected; `site` and the
    // [ORION_COURT_POSITION] fields are APPENDED after reason=, which is safe because every
    // abort reason is a single snake_case token with no spaces.
    //
    // -1 in any numeric field means "structurally absent at this site" (a pre-ownership abort has
    // no ShotContext, so no attempt / release seq / schedule token exists to report). That is
    // deliberately NOT 0: abort() legitimately emits 0 for a schedule token when no fire was
    // scheduled, so 0 must keep meaning "none was in play" rather than "not applicable here".
    emit engineDiagnostic(QStringLiteral(
        "Shot abort identity: physical_epoch=%1 shot_attempt=%2 release_seq=%3 "
        "schedule_token=%4 reason=%5 "
        // [ORION_ABORT_IDENTITY] which emit site raised this abort, so the 5 sites are
        // separable in a batch instead of being inferred from the reason string.
        "site=%6 "
        // [ORION_COURT_POSITION] APPEND-ONLY. Frame-normalized meter bbox center at the last
        // accepted detection, i.e. roughly where on the court this shot was being taken. -1 =
        // no detection carried usable bbox_wh yet. meter_jump = largest single-frame normalized
        // center move within the current unbroken lock (-1 = fewer than two frames in the run);
        // a large value means the meter was still sliding with the player when the abort fired,
        // which is what separates a genuine position effect from a moving-meter artifact.
        "meter_x=%7 meter_y=%8 meter_jump=%9 "
        // [ORION_ABORT_SHOT_TYPE] APPEND-ONLY, same convention as site= and the court-position
        // fields above. Without it an abort carries no shot type at all, so an entire BUCKET can
        // vanish from a batch with no way to tell an arming failure from a shot the user simply
        // did not take. That is not hypothetical: in the 2026-08-04 batch "No Dip" appeared zero
        // times as a fire AND zero times as a classified attempt, and its one real attempt
        // (physical_epoch=30, the only L2-held press in the batch) was invisible inside an
        // untyped `live_tip_deadline_missed` abort.
        //
        // Spaces become underscores so the value stays a single whitespace-free token like every
        // other field on this line ("No Dip" -> "No_Dip"), matching the existing `bucket=` and
        // OWNED-SHOT UNRESOLVED `shot_type=` renderings so one parser reads all three.
        // `unclassified` is EMITTED, never omitted, when the site held no classification -- a
        // reader must be able to separate "the engine never typed this attempt" from "this build
        // predates the field".
        "shot_type=%10")
                              .arg(physicalEpoch)
                              .arg(shotAttempt)
                              .arg(releaseSeq)
                              .arg(scheduleToken)
                              .arg(reason)
                              .arg(QLatin1String(site))
                              .arg(lastMeterNormX_, 0, 'f', 4)
                              .arg(lastMeterNormY_, 0, 'f', 4)
                              .arg(meterLockMaxJumpNorm_, 0, 'f', 4)
                              .arg(abortShotTypeToken(shotType)));
}

QString AutomationEngine::abortShotTypeToken(const QString& shotType)
{
    QString value = shotType.trimmed();
    if (value.isEmpty()) {
        return QStringLiteral("unclassified");
    }
    value.replace(QLatin1Char(' '), QLatin1Char('_'));
    value.replace(QLatin1Char('\t'), QLatin1Char('_'));
    return value.left(96);
}

void AutomationEngine::abort(QString reason)
{
    // Every abort path in the engine funnels through here, so this is the one place a
    // reservation can be retired without leaving a hole. A reservation must never outlive the
    // shot it was bound to: a stale plan is exactly the thing that could resurrect an old
    // deadline against a new epoch.
    cancelAutonomousTipReservation(reason);
    const QString abortCode = reason;
    const bool latencyCalibrationProbe = shot_.latencyCalibrationProbe;
    const ShotMode abortedMode = shot_.mode;
    const HoldState abortedState = shot_.state;
    const bool activeSquareAbort =
        (abortedMode == ShotMode::ButtonShot || abortedMode == ShotMode::TempoSquare)
        && shot_.state != HoldState::Idle;
    const bool activeStickAbort =
        (abortedMode == ShotMode::GoToStick || abortedMode == ShotMode::TempoStick)
        && shot_.state != HoldState::Idle;
    const QString abortedShotType = shot_.shotType;
    const double abortedPhysicalPressMs = shot_.armTimestampMs;
    const double abortedLsArmX = shot_.lsArmX;
    const double abortedLsArmY = shot_.lsArmY;
    const quint64 abortedPhysicalEpoch = shot_.physicalShotEpoch != 0
        ? shot_.physicalShotEpoch : physicalShotEpoch_;
    const quint64 abortedShotAttempt = shot_.armToken;
    const int abortedReleaseSeq = shot_.releaseSeq;
    const quint64 abortedScheduleToken = schedFireDeadlineMs_ >= 0.0
        ? schedFireToken_ : 0;
    const bool preserveTempoSquareGesture = activeSquareAbort
        && abortedMode == ShotMode::TempoSquare;
    // The primary-output drain must not erase a different shot control that was
    // already hidden underneath ownership.  Those secondary latches outlive the
    // aborted ShotContext and clear only after their own debounced physical end.
    const bool preserveSuppressedSquareOverlap = activeStickAbort
        && squareLatchedUntilRelease_;
    const bool preserveSuppressedStickOverlap = (activeSquareAbort || activeStickAbort)
        && stickOverlapLatchedUntilNeutral_;
    // Once a shot has been owned, no abort reason may reinterpret that physical
    // gesture as a brand-new shot. Shot state is authoritative here: lastPhysical_
    // is one raw poll and can contain a false release/neutral on the abort tick.
    // Square requires three UP samples; stick modes require three neutral samples.
    if (activeSquareAbort) {
        squareRearmBlockedUntilRelease_ = true;
    }
    if (activeStickAbort) {
        stickRearmBlockedUntilNeutral_ = true;
    }
    // A copied token lives in OrionPreciseFireThread, not in these fields. Revoke it
    // synchronously before changing ownership/state. If the worker crossed the submit
    // boundary while the fence was entering, its confirmation wins: that physical edge
    // already happened and must be consumed exactly once rather than mislabeled aborted.
    auto consumeConfirmedSchedule = [this]() -> bool {
        if (schedFireDeadlineMs_ < 0.0
            || schedFireConfirmedToken_ != schedFireToken_
            || schedFireActualMs_ < 0.0) {
            return false;
        }
        shot_.firedByScheduler = true;
        shot_.scheduledFireDeltaMs = schedFireActualMs_ - schedFireDeadlineMs_;
        shot_.releasePlan = schedFirePlan_;
        shot_.releaseReason = schedFireReason_;
        shot_.releaseReasonCode = schedFireCode_;
        shot_.releaseScheduleToken = schedFireToken_;
        shot_.releaseScheduleRouteGeneration = schedFireRouteGeneration_;
        shot_.releaseScheduleRoute = schedFireRoute_;
        // [ORION_ARMED_SOURCE] Same copy consumeDueScheduledFire performs: a confirmed
        // submit that wins the abort race is still a fired token and must keep its
        // arming decision's attribution on the outcome line.
        shot_.armedPredictorSource = schedFireArmedSource_;
        shot_.armedPredictorSigmaMs = schedFireArmedSigmaMs_;
        shot_.armedPredictorFillPct = schedFireArmedFillPct_;
        shot_.armedPredictorCommandEtaMs = schedFireArmedCommandEtaMs_;
        const double firedAt = schedFireActualMs_;
        clearScheduledFire();
        triggerRelease(firedAt, true);
        return true;
    };
    if (consumeConfirmedSchedule()) {
        return;
    }
    const bool scheduleFenceFailed = invalidateUnconfirmedSchedule();
    if (consumeConfirmedSchedule()) {
        return;
    }
    clearScheduledFire();
    if (!latencyCalibrationProbe) {
        shotsAborted_++;
    }
    shot_.abortReason = reason;
    // [ORION_ABORT_SHOT_TYPE] ShotContext::shotType DEFAULTS to "Standstill", so it is only a
    // classification once a shot has actually been owned. Reporting the default for an abort
    // raised against an Idle context would silently invent the most common bucket for attempts
    // that were never typed at all -- the exact confusion this field exists to remove -- so an
    // un-owned abort reports the `unclassified` sentinel instead.
    emitShotAbortIdentity("abort", static_cast<qint64>(abortedPhysicalEpoch),
                          static_cast<qint64>(abortedShotAttempt),
                          static_cast<qint64>(abortedReleaseSeq),
                          static_cast<qint64>(abortedScheduleToken), reason,
                          abortedState != HoldState::Idle ? abortedShotType : QString());
    // [ORION_FUSED_FIRE] an aborted shot can still yield a post-hoc appear->tip label (the
    // label is release-independent; the sidecar archive gates on a genuine >=20pp rise).
    endedShotAppearMs_ = shot_.anchorValidMs;
    endedShotBucketKey_ = shot_.bucketKey;
    emit shotAborted(reason);
    const double offset = shot_.networkOffsetMs;
    shot_ = ShotContext{};
    shot_.networkOffsetMs = offset;
    shot_.abortReason = reason;
    shot_.releasePlan = QStringLiteral("Aborted / physical pass-through");
    shot_.releaseReason = reason;
    shot_.releaseReasonCode = abortCode;
    // Clear the aborted primary ownership. Secondary controls already suppressed
    // beneath that ownership are restored below and remain fenced until their own
    // physical release/neutral debounce completes.
    squareLatchedUntilRelease_ = false;
    stickDownLatchedUntilNeutral_ = false;
    stickUpLatchedUntilNeutral_ = false;
    stickOverlapLatchedUntilNeutral_ = false;
    squareHoldStartMs_ = -1.0;
    stickDownHoldStartMs_ = -1.0;
    stickUpHoldStartMs_ = -1.0;
    clearPendingMeterOwnershipEpisode();
    clearPendingMeterOwnershipEvidence();
    clearPendingStickCalibration();
    resetTempoMovementTransaction();
    pendingSquarePhysicalEpoch_ = 0;
    if (activeSquareAbort || activeStickAbort) {
        const bool alreadyReleasedOrCancelled = scheduleFenceFailed
            || reason == QLatin1String("precise_fire_delivery_failed")
            || (activeStickAbort && reason == QLatin1String("ls_cancel"))
            || abortedState == HoldState::Releasing
            || abortedState == HoldState::Cooldown
            || abortedState == HoldState::PumpFake;
        latchOwnedOutputDrain(
            alreadyReleasedOrCancelled
                ? OwnedOutputDrain::ReleasedUntilPhysicalEnd
                : OwnedOutputDrain::HoldUntilPhysicalEnd,
            abortedMode, abortedShotType,
            abortedLsArmX, abortedLsArmY,
            abortedMode == ShotMode::TempoSquare
                || abortedMode == ShotMode::TempoStick);
    }
    if (preserveSuppressedSquareOverlap) {
        squareLatchedUntilRelease_ = true;
    }
    if (preserveSuppressedStickOverlap) {
        stickOverlapLatchedUntilNeutral_ = true;
        rightStickNeutralFrames_ = 0;
    }
    if (preserveTempoSquareGesture) {
        pendingSquareTempoRemap_ = true;
        pendingSquareShotType_ = abortedShotType;
        pendingSquareLsArmX_ = abortedLsArmX;
        pendingSquareLsArmY_ = abortedLsArmY;
        pendingSquareMovementValid_ = true;
        squareHoldStartMs_ = abortedPhysicalPressMs >= 0.0
            ? abortedPhysicalPressMs : nowMs();
    }
    sampler_.reset();
    greenTracker_.reset();
    emit shotStateChanged(shot_);
}

// Tracking / smoothing helpers

void AutomationEngine::updateTrackingHistory(const ControllerState& physical)
{
    // Update X button history (3-frame rolling window)
    const bool xButtonPressed = (physical.buttons & XINPUT_GAMEPAD_X) != 0;
    shot_.xButtonHistory.push_back(xButtonPressed);
    if (shot_.xButtonHistory.size() > 3) {
        shot_.xButtonHistory.pop_front();
    }

    // Update right stick history (3-frame rolling window)
    shot_.rightStickYHistory.push_back(physical.rightStickY);
    if (shot_.rightStickYHistory.size() > 3) {
        shot_.rightStickYHistory.pop_front();
    }

    shot_.rightStickXHistory.push_back(physical.rightStickX);
    if (shot_.rightStickXHistory.size() > 3) {
        shot_.rightStickXHistory.pop_front();
    }
}

void AutomationEngine::updateShootingState()
{
    // Calculate smoothed values from history
    if (shot_.rightStickYHistory.size() >= 3) {
        double sum = 0.0;
        for (int val : shot_.rightStickYHistory) {
            sum += static_cast<double>(val);
        }
        shot_.smoothRightStickY = sum / static_cast<double>(shot_.rightStickYHistory.size());
    } else {
        shot_.smoothRightStickY = 0.0;
    }

    if (shot_.rightStickXHistory.size() >= 3) {
        double sum = 0.0;
        for (int val : shot_.rightStickXHistory) {
            sum += static_cast<double>(val);
        }
        shot_.smoothRightStickX = sum / static_cast<double>(shot_.rightStickXHistory.size());
    } else {
        shot_.smoothRightStickX = 0.0;
    }

    // Calculate X button consistency
    if (shot_.xButtonHistory.size() >= 3) {
        bool allTrue = true;
        bool allFalse = true;
        for (bool pressed : shot_.xButtonHistory) {
            if (!pressed) allTrue = false;
            if (pressed) allFalse = false;
        }
        shot_.xButtonConsistent = allTrue || allFalse;
    } else {
        shot_.xButtonConsistent = true;
    }
}

} // namespace orion
