#pragma once

#include <QtCore/QString>

#include <deque>

// [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] The live shot-verdict tally.
//
// WHY: the owner tunes Release timing / Shot Lead against NBA 2K27's own shot-feedback
// banner and cannot hold the running score in his head -- "can't tell if I found my value
// or not because sometimes it's green". The sidecar (banner_verdict_live.py, the live twin
// of the validated offline grader tools/timing/panel_grade.py) already emits ONE graded
// verdict per banner appearance; this is the rolling window that turns that stream into
// one number and one sentence.
//
// PRESENTATION ONLY. Nothing here ever reaches AutomationEngine, the telemetry snapshot or
// any timing path: it is a scoreboard for a human, and its only side effects are a QML
// notification and an Activity line.
//
// The window is deliberately the LAST 10 verdicts and is CLEARED whenever the value being
// tuned changes (see OrionAppController's Release timing / fade trim / Shot Lead setters),
// so what it shows always describes the CURRENT setting -- a tally that straddles a slider
// move is exactly the confusion this exists to remove.
namespace orion {

enum class ShotVerdictBucket {
    Green,   // the grader's made-by-timing words: EXCELLENT, PERFECT
    Early,
    Late,
    Other,   // a template word this UI does not classify (never invented -- see below)
};

struct ShotVerdictEntry {
    ShotVerdictBucket bucket = ShotVerdictBucket::Other;
    QString timing;     // the grader's own word, verbatim
    QString coverage;   // WIDE OPEN | ... | SMOTHERED, or empty on a 2-cell panel
    qint64 epochMs = 0;
    int seq = 0;
    bool contested = false;
};

class ShotVerdictTally final {
public:
    // The window the owner reads. Ten is one possession run: long enough for a direction to
    // be real, short enough to refill after a one-step slider move inside a minute of play.
    static constexpr int kWindow = 10;
    // Below this the tally says "collecting" and offers NO direction. Four shots can read
    // 3-1 on noise alone; the direction rule only earns its keep once the sample carries it.
    static constexpr int kMinimumForSuggestion = 5;
    // 8/10 green is the "stop moving the slider" bar.
    static constexpr int kConfidentGreen = 8;
    // Contest is the one confound the owner must see: a run of contested shots can drop the
    // green rate with the timing value untouched (measured: contested 60% vs open 79%).
    static constexpr int kContestedNoticeMin = 3;

    // The bucket rule, in one place. GREEN IS THE WORD, NOT THE COLOUR: the panel paints a
    // coverage cell green too, so classifying on timingColor would count open misses as
    // makes. Unknown words fall to Other rather than being forced into a bucket -- the
    // grader's library holds exactly EXCELLENT/EARLY/LATE today and this must not lie when
    // 2K adds a fourth.
    [[nodiscard]] static ShotVerdictBucket bucketFor(const QString& timing)
    {
        const QString word = timing.trimmed().toUpper();
        if (word.isEmpty()) {
            return ShotVerdictBucket::Other;
        }
        if (word == QLatin1String("EXCELLENT") || word == QLatin1String("PERFECT")) {
            return ShotVerdictBucket::Green;
        }
        if (word.contains(QLatin1String("EARLY"))) {
            return ShotVerdictBucket::Early;
        }
        if (word.contains(QLatin1String("LATE"))) {
            return ShotVerdictBucket::Late;
        }
        return ShotVerdictBucket::Other;
    }

    // Coverage words that mean a defender was on the shot. BOTHERED and SMOTHERED are the
    // two that do not carry the word CONTEST.
    [[nodiscard]] static bool isContestedCoverage(const QString& coverage)
    {
        const QString word = coverage.trimmed().toUpper();
        return word.contains(QLatin1String("CONTEST"))
            || word.contains(QLatin1String("BOTHERED"))
            || word.contains(QLatin1String("SMOTHERED"));
    }

    // One verdict in. Returns false when nothing was recorded (blank word, or a seq already
    // seen). De-dupe is by `seq`, the sidecar's gap-free 1-based counter: a re-delivered
    // event must never double-count a shot the owner took once.
    bool record(const QString& timing, const QString& coverage, qint64 epochMs, int seq)
    {
        if (timing.trimmed().isEmpty()) {
            return false;
        }
        if (seq > 0 && isDuplicate(seq)) {
            return false;
        }
        ShotVerdictEntry entry;
        entry.bucket = bucketFor(timing);
        entry.timing = timing.trimmed().toUpper();
        entry.coverage = coverage.trimmed().toUpper();
        entry.epochMs = epochMs;
        entry.seq = seq;
        entry.contested = isContestedCoverage(entry.coverage);
        window_.push_back(entry);
        while (window_.size() > static_cast<std::size_t>(kWindow)) {
            window_.pop_front();
        }
        if (seq > lastSeq_) {
            lastSeq_ = seq;
        }
        return true;
    }

    // The owner's Reset link, and the automatic clear on every committed slider change.
    // Keeps lastSeq_: the session's verdict counter has NOT restarted, so a duplicate that
    // arrives just after a reset is still a duplicate.
    bool reset()
    {
        if (window_.empty()) {
            return false;
        }
        window_.clear();
        return true;
    }

    // Session start / disconnect. The sidecar's seq restarts at 1 with the next session, so
    // the de-dupe watermark has to go with the window.
    bool resetSession()
    {
        const bool had = !window_.empty() || lastSeq_ != 0;
        window_.clear();
        lastSeq_ = 0;
        return had;
    }

    [[nodiscard]] int count() const noexcept { return static_cast<int>(window_.size()); }
    [[nodiscard]] int green() const noexcept { return countOf(ShotVerdictBucket::Green); }
    [[nodiscard]] int early() const noexcept { return countOf(ShotVerdictBucket::Early); }
    [[nodiscard]] int late() const noexcept { return countOf(ShotVerdictBucket::Late); }
    [[nodiscard]] int other() const noexcept { return countOf(ShotVerdictBucket::Other); }

    [[nodiscard]] int contested() const noexcept
    {
        int n = 0;
        for (const ShotVerdictEntry& entry : window_) {
            n += entry.contested ? 1 : 0;
        }
        return n;
    }

    [[nodiscard]] QString lastTiming() const
    {
        return window_.empty() ? QString() : window_.back().timing;
    }

    [[nodiscard]] QString lastCoverage() const
    {
        return window_.empty() ? QString() : window_.back().coverage;
    }

    // One char per verdict, OLDEST -> NEWEST, for the dot strip: g green, e early, l late,
    // o other. A string keeps the whole strip on ONE notified property instead of ten.
    [[nodiscard]] QString pattern() const
    {
        QString out;
        out.reserve(static_cast<qsizetype>(window_.size()));
        for (const ShotVerdictEntry& entry : window_) {
            switch (entry.bucket) {
            case ShotVerdictBucket::Green: out.append(QLatin1Char('g')); break;
            case ShotVerdictBucket::Early: out.append(QLatin1Char('e')); break;
            case ShotVerdictBucket::Late:  out.append(QLatin1Char('l')); break;
            case ShotVerdictBucket::Other: out.append(QLatin1Char('o')); break;
            }
        }
        return out;
    }

    // THE sentence. The only prose on the card, and the whole point of the feature: it says
    // which way to move the one slider the owner is holding.
    //
    // Escapes, not literal glyphs: these sources carry no BOM and the build sets no /utf-8,
    // so a raw multi-byte character in a header is at the mercy of the host codepage.
    // QStringLiteral makes these UTF-16 literals, where the escapes are exact.
    [[nodiscard]] QString suggestion() const
    {
        const int n = count();
        if (n < kMinimumForSuggestion) {
            return QStringLiteral("collecting\u2026 (5 shots minimum)");
        }
        if (green() >= kConfidentGreen) {
            return QStringLiteral("\u2713 this is your value \u2014 leave it");
        }
        const int e = early();
        const int l = late();
        if (l > e) {
            return QStringLiteral("\u2192 move the slider right");
        }
        if (e > l) {
            return QStringLiteral("\u2190 move the slider left");
        }
        if (e > 0) {
            return QStringLiteral("balanced \u2014 nudge 1 step and watch 10 more");
        }
        // Tied at ZERO: enough shots, not enough green, and not one early or late among
        // them -- every miss came back as a word this tally does not classify. There is no
        // direction to give, and inventing one would be exactly the guess this card exists
        // to replace.
        return QStringLiteral("no early or late shots \u2014 keep watching");
    }

    [[nodiscard]] const std::deque<ShotVerdictEntry>& entries() const noexcept { return window_; }

private:
    [[nodiscard]] bool isDuplicate(int seq) const
    {
        if (seq == lastSeq_) {
            return true;
        }
        for (const ShotVerdictEntry& entry : window_) {
            if (entry.seq == seq) {
                return true;
            }
        }
        return false;
    }

    [[nodiscard]] int countOf(ShotVerdictBucket bucket) const noexcept
    {
        int n = 0;
        for (const ShotVerdictEntry& entry : window_) {
            n += (entry.bucket == bucket) ? 1 : 0;
        }
        return n;
    }

    std::deque<ShotVerdictEntry> window_;
    int lastSeq_ = 0;
};

} // namespace orion
