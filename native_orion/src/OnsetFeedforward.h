#pragma once

#include <QHash>
#include <QString>
#include <QVector>

#include <algorithm>
#include <cmath>

namespace orion {

// [ORION_ONSET_FF 2026-09-21 owner] PER-SHOT onset feedforward.
//
// THE MEASUREMENT (logs/orion_native.log(.1), 284 banner-graded online standstills over three
// sessions, 2026-09-20/21). The meter's onset after the press -- the game's online sync delay
// as this engine sees it -- has a shot-to-shot spread of 62-70 ms with a lag-1 autocorrelation
// of ~0 (-0.02 / +0.34 / -0.03). It is re-rolled on every shot, so no integrator over past
// verdicts can follow it: the banner lead trim (two CONSECUTIVE agreeing verdicts per 3 ms step)
// moved ONCE in a 74-verdict session. And it is the miss mechanism, one-sided:
//
//   onset vs its local 10-shot median | EXCELLENT | LATE
//   earlier than usual                | 74-100 %  | 0-14 %
//   within +-20 ms         (n=154)    |    71 %   | 14 %
//   +20 .. +60 ms          (n=46)     |    43 %   | 43 %
//   > +60 ms               (n=45)     |    44 %   | 47 %
//
// A later-than-usual meter misses LATE half the time; an earlier one costs nothing. An ordinal
// fit of those tails puts the correctable BIAS at ~10-12 ms (the rest of the tail loss is
// variance from the shorter runway, which this term cannot buy back), so the shipped gain is
// deliberately small and the clamp deliberately tight.
//
// WHAT IT IS. A reference onset per shot-type bucket (the median of the last `window` completed
// shots' onsets -- never the live shot's own), a signed deviation of the live shot from it, and
// a displacement of the scheduled fire of -gain * deviation, clamped. Positive deviation (the
// meter came LATER than usual) fires EARLIER. `oneSided` keeps an earlier-than-usual meter at
// zero, because the table above says that side does not miss.
//
// WHAT IT IS NOT. It is not a lead. It is zero-mean by construction against the bucket's own
// recent history, so the learned lead, the banner trim and the per-type offsets keep their
// meaning; it never touches tipAbsMs (every horizon, veto and learner reads that untouched);
// and it is applied at scheduleFire's single choke point exactly as the dev sweep offset is, so
// it reaches the scheduled instant 1:1 and the undisplaced deadline stays recoverable.
struct OnsetFeedforwardLimits {
    double gain = 0.0;        // ms of fire displacement per ms of late onset (0 = off)
    double clampMs = 10.0;    // |displacement| ceiling, ms
    int window = 10;          // completed onsets kept per bucket
    int minSamples = 4;       // fewer than this -> no reference -> no displacement
    bool oneSided = true;     // only a LATER-than-reference onset moves the fire
    // A bucket whose last completed onset is older than this is a DIFFERENT context (a new
    // game, another court, a long pause): its median must not aim the next shot. 10 min.
    double staleMs = 600000.0;
};

class OnsetFeedforward {
public:
    static constexpr double kMinAppliedMs = 2.0;   // shared pure/final displacement floor

    struct Decision {
        bool applied = false;
        double onsetMs = -1.0;
        double referenceMs = -1.0;
        double deviationMs = 0.0;
        double offsetMs = 0.0;    // signed displacement of the scheduled fire (negative = earlier)
        int samples = 0;
        QString reason;           // off | no_onset | cold | stale | one_sided | zero | applied
    };

    // A completed shot's onset (press -> first meter sight), stamped at its release like the
    // banner trim's own ring. Ignored when not a finite, non-negative onset. `nowMs` is the
    // engine clock (-1 = no clock: the pure policy tests); a ring whose last observation is
    // older than `staleMs` is emptied BEFORE the new onset enters it, so a new context starts
    // cold rather than inheriting the previous one's median.
    void observe(const QString& bucket, double onsetMs, int window, double nowMs = -1.0,
                 double staleMs = 600000.0)
    {
        if (!std::isfinite(onsetMs) || onsetMs < 0.0) {
            return;
        }
        if (isStale(bucket, nowMs, staleMs)) {
            ring_.remove(bucket);
        }
        const int keep = std::max(1, window);
        QVector<double>& ring = ring_[bucket];
        ring.append(onsetMs);
        while (ring.size() > keep) {
            ring.removeFirst();
        }
        if (nowMs >= 0.0) {
            lastMs_[bucket] = nowMs;
        }
    }

    [[nodiscard]] bool isStale(const QString& bucket, double nowMs, double staleMs) const
    {
        const auto it = lastMs_.constFind(bucket);
        return nowMs >= 0.0 && it != lastMs_.constEnd() && staleMs > 0.0
            && nowMs - it.value() > staleMs;
    }

    [[nodiscard]] int samples(const QString& bucket) const
    {
        const auto it = ring_.constFind(bucket);
        return it == ring_.constEnd() ? 0 : it->size();
    }

    [[nodiscard]] double referenceMs(const QString& bucket, int minSamples) const
    {
        const auto it = ring_.constFind(bucket);
        if (it == ring_.constEnd() || it->size() < std::max(1, minSamples)) {
            return -1.0;
        }
        return median(*it);
    }

    [[nodiscard]] Decision decide(const QString& bucket, double onsetMs,
                                  const OnsetFeedforwardLimits& limits,
                                  double nowMs = -1.0) const
    {
        Decision d;
        d.onsetMs = onsetMs;
        d.samples = samples(bucket);
        if (!(limits.gain > 0.0)) {
            d.reason = QStringLiteral("off");
            return d;
        }
        if (!std::isfinite(onsetMs) || onsetMs < 0.0) {
            d.reason = QStringLiteral("no_onset");
            return d;
        }
        if (isStale(bucket, nowMs, limits.staleMs)) {
            d.reason = QStringLiteral("stale");
            return d;
        }
        d.referenceMs = referenceMs(bucket, limits.minSamples);
        if (d.referenceMs < 0.0) {
            d.reason = QStringLiteral("cold");
            return d;
        }
        d.deviationMs = onsetMs - d.referenceMs;
        if (limits.oneSided && d.deviationMs <= 0.0) {
            d.reason = QStringLiteral("one_sided");
            return d;
        }
        const double clampMs = std::max(0.0, limits.clampMs);
        double offset = -limits.gain * d.deviationMs;
        offset = std::clamp(offset, -clampMs, clampMs);
        // [CL2-P6-004 2026-09-22] A displacement is a DISPLACEMENT for every learner: the release is
        // fenced from the latency marker, the banner trim, the oracle and the phase sample. Below
        // kMinAppliedMs the shift is under half a 4 ms timing tick and buys nothing, but it used to
        // cost that evidence on ~45 % of all shots (70 % of displaced shots moved < 4 ms), which is
        // why the trim barely adapted. Such shots now fly undisplaced and train the learners.
        if (std::abs(offset) < kMinAppliedMs) {
            d.reason = QStringLiteral("zero");
            return d;
        }
        d.offsetMs = offset;
        d.applied = true;
        d.reason = QStringLiteral("applied");
        return d;
    }

    void reset()
    {
        ring_.clear();
        lastMs_.clear();
    }

private:
    static double median(QVector<double> v)
    {
        std::sort(v.begin(), v.end());
        const int n = v.size();
        if (n == 0) {
            return -1.0;
        }
        return (n % 2 == 1) ? v[n / 2] : 0.5 * (v[n / 2 - 1] + v[n / 2]);
    }

    QHash<QString, QVector<double>> ring_;
    QHash<QString, double> lastMs_;   // engine clock of the last observation per bucket
};

// [2026-09-22 VARIANCE HUNT] Randomised per-shot A/B of the feedforward's limits. The ordinal fit in
// docs/variance/ONLINE_GRADING_CLOCK_2026-09-22.md identifies beta ~= 0.45 (the share of the online
// grade that follows the press clock), i.e. an optimal gain ~0.45 against the shipped 0.2 / clamp 10
// / one-sided. That estimate is in-sample; this is the live confirmation. Spec (env
// ORION_ONSET_FF_AB): comma-separated arms "gain:clampMs:oneSided", e.g. "0.2:10:1,0.45:40:0".
// Values are clamped into the setting bands; a malformed arm is dropped; fewer than two valid arms
// disables the A/B (the ordinary single-setting path then runs unchanged).
struct OnsetFeedforwardArm {
    double gain = 0.0;
    double clampMs = 0.0;
    bool oneSided = true;
};

inline QVector<OnsetFeedforwardArm> parseOnsetFeedforwardArms(const QString& spec,
                                                              double gainMax, double clampMax)
{
    QVector<OnsetFeedforwardArm> arms;
    for (const QString& raw : spec.split(QLatin1Char(','), Qt::SkipEmptyParts)) {
        const QStringList f = raw.trimmed().split(QLatin1Char(':'));
        if (f.size() != 3) {
            continue;
        }
        bool okG = false, okC = false;
        const double g = f[0].trimmed().toDouble(&okG);
        const double c = f[1].trimmed().toDouble(&okC);
        const QString side = f[2].trimmed();
        if (!okG || !okC || !std::isfinite(g) || !std::isfinite(c)
            || (side != QLatin1String("0") && side != QLatin1String("1"))) {
            continue;
        }
        OnsetFeedforwardArm a;
        a.gain = std::clamp(g, 0.0, gainMax);
        a.clampMs = std::clamp(c, 0.0, clampMax);
        a.oneSided = side == QLatin1String("1");
        arms.push_back(a);
    }
    if (arms.size() < 2) {
        arms.clear();
    }
    return arms;
}

}  // namespace orion
