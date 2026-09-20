// -----------------------------------------------------------------------------
//  ShotVerdictTallyTests -- the live shot-verdict tally's rules
// -----------------------------------------------------------------------------
//
// [ORION_BANNER_VERDICT_LIVE 2026-09-14 owner] The owner tunes ONE slider against NBA 2K27's
// own shot-feedback banner and could not hold the running score in his head: "can't tell if
// I found my value or not because sometimes it's green". The sidecar grades the banner with
// the validated reader (tools/timing/panel_grade.py) and the controller rolls the last ten
// verdicts through orion::ShotVerdictTally; the card shows the counts and ONE sentence.
//
// That sentence is the whole product here, and it is pure policy, so it is pinned against
// the policy header directly -- no controller instance (its constructor spawns the packet
// bridge and its destructor force-kills chiaki images machine-wide; see
// MeterDelaySettingsPropertyTests.cpp). The controller's side of the contract -- the
// Q_PROPERTY names, the low-fanout notifier, resetBannerTally() -- is pinned there.
//
// THE RULES, as the owner stated them:
//   bucket:      EXCELLENT/PERFECT -> green; a word containing EARLY -> early; containing
//                LATE -> late; anything else -> other.  GREEN IS THE WORD, NOT THE COLOUR.
//   suggestion:  < 5 shots      -> collecting
//                >= 8 green     -> this is your value
//                late > early   -> move right
//                early > late   -> move left
//                tied, non-zero -> balanced, nudge one step
//   window:      the last 10, de-duped by the sidecar's gap-free seq, cleared on every
//                committed change to the value being tuned and on session start/end.
#include <limits>

#include "BannerLeadTrim.h"
#include "ShotVerdictTally.h"

#include <QtTest/QTest>

#include <cmath>

using orion::BannerLeadTrim;
using orion::BannerLeadTrimLimits;
using orion::ShotVerdictBucket;
using orion::ShotVerdictTally;

namespace {

// Record n verdicts of one word, with seqs continuing from the tally's own history.
void feed(ShotVerdictTally& tally, const QString& timing, int n,
          const QString& coverage = QStringLiteral("OPEN"), int firstSeq = 0)
{
    static int nextSeq = 1;
    if (firstSeq > 0) {
        nextSeq = firstSeq;
    }
    for (int i = 0; i < n; ++i) {
        tally.record(timing, coverage, 1'700'000'000'000LL + nextSeq, nextSeq);
        ++nextSeq;
    }
}

// [ORION_RELEASE_ORACLE_TRIM 2026-09-15] The synthetic rig the oracle descent is driven on.
// The landing sits `trueErrorMs` away from the green window and the reader measures
// gap = k * |error| with k = 0.9 px/ms -- the slope implied by the 09-15 drill (~11 px of
// retraction gap on the ~12 ms LATE shots). A trim of +t cancels t ms of a +12 ms error,
// exactly as a larger lead fires earlier. `green` uses the reader's OWN boundary (<= 3 px).
struct OracleRig {
    static constexpr double kPxPerMs = 0.9;
    double trueErrorMs = 12.0;
    [[nodiscard]] double gapPxFor(double trimMs) const
    {
        return kPxPerMs * std::abs(trueErrorMs - trimMs);
    }
    [[nodiscard]] bool greenFor(double trimMs) const { return gapPxFor(trimMs) <= 3.0; }
};

} // namespace

class ShotVerdictTallyTests final : public QObject {
    Q_OBJECT

private slots:
    // The panel paints a COVERAGE cell green as well, so a colour rule would score an open
    // miss as a make. The word is the only honest classifier.
    void bucketsClassifyTheGraderWordNotTheColour()
    {
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("EXCELLENT")),
                 ShotVerdictBucket::Green);
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("PERFECT")),
                 ShotVerdictBucket::Green);
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("EARLY")),
                 ShotVerdictBucket::Early);
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("VERY EARLY")),
                 ShotVerdictBucket::Early);
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("LATE")),
                 ShotVerdictBucket::Late);
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("SLIGHTLY LATE")),
                 ShotVerdictBucket::Late);
        // Case and padding are the sidecar's, not ours.
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("  excellent ")),
                 ShotVerdictBucket::Green);
        // A word the grader's library does not hold today must NOT be forced into a
        // bucket: the tally would then invent a direction out of a word it cannot read.
        QCOMPARE(ShotVerdictTally::bucketFor(QStringLiteral("GOOD")),
                 ShotVerdictBucket::Other);
        QCOMPARE(ShotVerdictTally::bucketFor(QString()), ShotVerdictBucket::Other);
    }

    void contestIsEveryDefendedCoverageWord()
    {
        for (const QString& word : {QStringLiteral("LIGHT CONTEST"),
                                    QStringLiteral("SOLID CONTEST"),
                                    QStringLiteral("HEAVY CONTEST"),
                                    QStringLiteral("BOTHERED"),
                                    QStringLiteral("SMOTHERED")}) {
            QVERIFY2(ShotVerdictTally::isContestedCoverage(word), qPrintable(word));
        }
        for (const QString& word : {QStringLiteral("WIDE OPEN"), QStringLiteral("OPEN"),
                                    QStringLiteral("SEMI-OPEN"), QString()}) {
            QVERIFY2(!ShotVerdictTally::isContestedCoverage(word), qPrintable(word));
        }
    }

    void windowHoldsTheLastTenAndCountsEachBucket()
    {
        ShotVerdictTally tally;
        feed(tally, QStringLiteral("EXCELLENT"), 6, QStringLiteral("WIDE OPEN"), 1);
        feed(tally, QStringLiteral("EARLY"), 3);
        feed(tally, QStringLiteral("LATE"), 1);
        QCOMPARE(tally.count(), 10);
        QCOMPARE(tally.green(), 6);
        QCOMPARE(tally.early(), 3);
        QCOMPARE(tally.late(), 1);
        QCOMPARE(tally.other(), 0);
        QCOMPARE(tally.pattern(), QStringLiteral("ggggggeeel"));
        QCOMPARE(tally.lastTiming(), QStringLiteral("LATE"));

        // The eleventh shot pushes the oldest out; the window never grows.
        feed(tally, QStringLiteral("LATE"), 1);
        QCOMPARE(tally.count(), 10);
        QCOMPARE(tally.green(), 5);
        QCOMPARE(tally.late(), 2);
        QCOMPARE(tally.pattern(), QStringLiteral("gggggeeell"));
    }

    // A re-delivered event must never double-count a shot the owner took once.
    void duplicateSeqIsIgnored()
    {
        ShotVerdictTally tally;
        QVERIFY(tally.record(QStringLiteral("EXCELLENT"), QStringLiteral("OPEN"), 10, 1));
        QVERIFY(!tally.record(QStringLiteral("EXCELLENT"), QStringLiteral("OPEN"), 11, 1));
        QVERIFY(!tally.record(QStringLiteral("LATE"), QStringLiteral("OPEN"), 12, 1));
        QCOMPARE(tally.count(), 1);
        QVERIFY(tally.record(QStringLiteral("LATE"), QStringLiteral("OPEN"), 13, 2));
        QCOMPARE(tally.count(), 2);
        // The sidecar never emits a blank word; if one ever arrives it is not a shot.
        QVERIFY(!tally.record(QStringLiteral("   "), QStringLiteral("OPEN"), 14, 3));
        QCOMPARE(tally.count(), 2);
    }

    // The tally describes ONE value. reset() is what the committed slider change and the
    // card's Reset link both call; the de-dupe watermark survives it because the session's
    // verdict counter has not restarted.
    void resetClearsTheWindowButKeepsTheDedupeWatermark()
    {
        ShotVerdictTally tally;
        feed(tally, QStringLiteral("EXCELLENT"), 3, QStringLiteral("OPEN"), 1);
        QVERIFY(tally.reset());
        QCOMPARE(tally.count(), 0);
        QCOMPARE(tally.pattern(), QString());
        QCOMPARE(tally.lastTiming(), QString());
        QVERIFY(!tally.reset());   // nothing to clear -> no notification
        // seq 3 was already counted before the reset; re-delivering it must not refill.
        QVERIFY(!tally.record(QStringLiteral("EXCELLENT"), QStringLiteral("OPEN"), 20, 3));
        QVERIFY(tally.record(QStringLiteral("EXCELLENT"), QStringLiteral("OPEN"), 21, 4));
        QCOMPARE(tally.count(), 1);
    }

    // A new session restarts the sidecar's seq at 1, so the watermark has to go with it or
    // the first ten shots of every session after the first would be swallowed as duplicates.
    void sessionResetAlsoDropsTheWatermark()
    {
        ShotVerdictTally tally;
        feed(tally, QStringLiteral("EXCELLENT"), 4, QStringLiteral("OPEN"), 1);
        QVERIFY(tally.resetSession());
        QVERIFY(!tally.resetSession());   // already clean -> no notification
        QVERIFY(tally.record(QStringLiteral("EXCELLENT"), QStringLiteral("OPEN"), 30, 1));
        QCOMPARE(tally.count(), 1);
    }

    void contestedCountTracksTheCoverageCell()
    {
        ShotVerdictTally tally;
        feed(tally, QStringLiteral("EXCELLENT"), 4, QStringLiteral("HEAVY CONTEST"), 1);
        feed(tally, QStringLiteral("EXCELLENT"), 2, QStringLiteral("WIDE OPEN"));
        QCOMPARE(tally.contested(), 4);
        QCOMPARE(tally.lastCoverage(), QStringLiteral("WIDE OPEN"));
        // A 2-cell TIMING|DISTANCE panel reports no coverage at all, and that is not a
        // contest.
        QVERIFY(tally.record(QStringLiteral("LATE"), QString(), 99, 99));
        QCOMPARE(tally.contested(), 4);
        QCOMPARE(tally.lastCoverage(), QString());
    }

    // -- THE SENTENCE ------------------------------------------------------------
    void suggestionCollectsUntilFiveShots()
    {
        ShotVerdictTally tally;
        const QString collecting = QStringLiteral("collecting\u2026 (5 shots minimum)");
        QCOMPARE(tally.suggestion(), collecting);
        feed(tally, QStringLiteral("LATE"), 4, QStringLiteral("OPEN"), 1);
        QCOMPARE(tally.suggestion(), collecting);   // 4 shots can read 3-1 on noise alone
        feed(tally, QStringLiteral("LATE"), 1);
        QVERIFY(tally.suggestion() != collecting);
    }

    void suggestionNamesTheDirectionToMove()
    {
        {   // late-heavy: the release is coming too late -> move the slider right
            ShotVerdictTally tally;
            feed(tally, QStringLiteral("LATE"), 4, QStringLiteral("OPEN"), 1);
            feed(tally, QStringLiteral("EXCELLENT"), 2);
            QCOMPARE(tally.suggestion(), QStringLiteral("\u2192 move the slider right"));
        }
        {   // early-heavy: the mirror image
            ShotVerdictTally tally;
            feed(tally, QStringLiteral("EARLY"), 4, QStringLiteral("OPEN"), 1);
            feed(tally, QStringLiteral("EXCELLENT"), 2);
            QCOMPARE(tally.suggestion(), QStringLiteral("\u2190 move the slider left"));
        }
        {   // straddling the target: one step, then ten more shots
            ShotVerdictTally tally;
            feed(tally, QStringLiteral("EARLY"), 3, QStringLiteral("OPEN"), 1);
            feed(tally, QStringLiteral("LATE"), 3);
            QCOMPARE(tally.suggestion(),
                     QStringLiteral("balanced \u2014 nudge 1 step and watch 10 more"));
        }
    }

    void eightGreenInTenIsTheStopMovingBar()
    {
        ShotVerdictTally tally;
        feed(tally, QStringLiteral("EXCELLENT"), 8, QStringLiteral("OPEN"), 1);
        feed(tally, QStringLiteral("LATE"), 2);
        QCOMPARE(tally.green(), 8);
        QCOMPARE(tally.suggestion(), QStringLiteral("\u2713 this is your value \u2014 leave it"));

        // Seven is not eight: still a direction, not a verdict on the value.
        ShotVerdictTally seven;
        feed(seven, QStringLiteral("EXCELLENT"), 7, QStringLiteral("OPEN"), 1);
        feed(seven, QStringLiteral("LATE"), 3);
        QCOMPARE(seven.suggestion(), QStringLiteral("\u2192 move the slider right"));
    }

    // Enough shots, not enough green, and not one early or late among them: every miss came
    // back as a word this tally cannot classify. There is no direction in that, and
    // inventing one is exactly the guess the card exists to replace.
    void noDirectionIsInventedFromUnclassifiedWords()
    {
        ShotVerdictTally tally;
        feed(tally, QStringLiteral("EXCELLENT"), 3, QStringLiteral("OPEN"), 1);
        feed(tally, QStringLiteral("GOOD"), 3);
        QCOMPARE(tally.other(), 3);
        QCOMPARE(tally.suggestion(),
                 QStringLiteral("no early or late shots \u2014 keep watching"));
    }

    // =====================================================================================
    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] The bounded closed loop's RULES.
    //
    // Same place as the tally's rules and for the same reason: this is pure policy, so it is
    // pinned against the policy header directly and needs no engine and no controller. The
    // ENGINE's half -- which lead the trim rides, the attribution ring, the log line and the
    // TIP RESERVATION field -- is pinned in AutomationEngineTests.
    //
    // THE RULES, as the owner stated them:
    //   LATE -> +step (fire earlier)   EARLY -> -step   EXCELLENT -> HOLD
    //   per shot-type bucket, clamped +-max, and NOTHING moves on fewer than two
    //   same-direction verdicts in the last four.
    //
    // [ORION_BANNER_TRIM_HOLD 2026-09-16 owner] EXCELLENT used to decay the trim 1 ms toward the
    // slider on the spot. The 2026-09-16 20:43-20:45 session (22 shots) is why it no longer does:
    // lates pushed the Standstill trim to +9 (effective 278) and two EXCELLENTs immediately began
    // walking it back, and after a slider move seven EXCELLENTs at -3 (271) walked it to 274 and
    // straight into two EARLIES. A trim that is PRODUCING EXCELLENT is the correct trim. The only
    // relaxation left inside a session is the IDLE decay, which starts after `holdShots`
    // consecutive EXCELLENT/GREEN verdicts so a merely TOLERATED value cannot live forever.
    // =====================================================================================

    // The hysteresis IS the promise: one misread banner can never move the owner's lead.
    void oneLateDoesNotMoveTheTrimAndTwoDo()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;   // step 3, max 15, decay 1

        const auto first = trim.observe(QStringLiteral("Standstill"),
                                        QStringLiteral("LATE"), limits);
        QVERIFY2(!first.applied, "a single LATE must not move the lead");
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 0.0);
        QCOMPARE(first.recent, QStringLiteral("L"));

        const auto second = trim.observe(QStringLiteral("Standstill"),
                                         QStringLiteral("LATE"), limits);
        QVERIFY(second.applied);
        QCOMPARE(second.beforeMs, 0.0);
        QCOMPARE(second.afterMs, 3.0);
        QCOMPARE(second.recent, QStringLiteral("L,L"));
        // LATE means the release landed late, so the fix is to fire EARLIER -- a LARGER lead.
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 3.0);

        // Once the direction is established each further verdict of it costs one more step:
        // the window is a GATE, not a rate limiter.
        QVERIFY(trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits).applied);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 6.0);
    }

    // The mirror image, and the sign that matters: EARLY lowers the effective lead.
    void twoEarliesLowerTheTrim()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        QVERIFY(!trim.observe(QStringLiteral("Standstill"),
                              QStringLiteral("EARLY"), limits).applied);
        const auto second = trim.observe(QStringLiteral("Standstill"),
                                         QStringLiteral("VERY EARLY"), limits);
        QVERIFY(second.applied);
        QCOMPARE(second.afterMs, -3.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), -3.0);
    }

    // [ORION_BANNER_TRIM_HOLD 2026-09-16 owner] THE HEADLINE OF THIS CHANGE: a trim that is
    // producing EXCELLENT is, by definition, the correct trim. It HOLDS. Eleven of them move
    // nothing; only the twelfth -- the point at which the value is no longer "right" but merely
    // "tolerated" -- starts the slow idle bleed, 1 ms per further EXCELLENT.
    void excellentHoldsTheTrimAndOnlyIdlesDownAfterADozen()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;   // step 3, max 15, decay 1, hold 12
        QCOMPARE(limits.holdShots, 12);
        for (int i = 0; i < 4; ++i) {
            trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        }
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 9.0);   // 4 LATE, 3 applied
        QCOMPARE(trim.holdStreakForType(QStringLiteral("Standstill")), 0);

        // The same hysteresis gate still comes first: one EXCELLENT is not evidence either.
        const auto first = trim.observe(QStringLiteral("Standstill"),
                                        QStringLiteral("EXCELLENT"), limits);
        QVERIFY(!first.applied);
        QCOMPARE(first.reason, QStringLiteral("evidence"));
        QCOMPARE(first.holdStreak, 1);

        // ...and then the HOLD. This is the 09-16 regression in one line: the second EXCELLENT
        // used to walk 9 -> 8, which is the loop undoing the correction the lates had just
        // earned. Eleven consecutive EXCELLENTs now move nothing at all.
        for (int i = 2; i <= 11; ++i) {
            const auto held = trim.observe(QStringLiteral("Standstill"),
                                           QStringLiteral("EXCELLENT"), limits);
            QVERIFY2(!held.applied,
                     qPrintable(QStringLiteral("EXCELLENT #%1 must hold").arg(i)));
            QCOMPARE(held.reason, QStringLiteral("hold"));
            QCOMPARE(held.holdStreak, i);
            QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 9.0);
        }

        // The twelfth is the first one past the hold: the value has been merely tolerated for a
        // dozen shots, so it starts bleeding off -- by less than a step, and one shot at a time.
        const auto twelfth = trim.observe(QStringLiteral("Standstill"),
                                          QStringLiteral("EXCELLENT"), limits);
        QVERIFY(twelfth.applied);
        QCOMPARE(twelfth.reason, QStringLiteral("idle_decay"));
        QCOMPARE(twelfth.holdStreak, 12);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 8.0);
        QVERIFY(trim.observe(QStringLiteral("Standstill"),
                             QStringLiteral("PERFECT"), limits).applied);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 7.0);

        // ...all the way down, and then it STOPS: a decay must never become a correction.
        for (int i = 0; i < 40; ++i) {
            trim.observe(QStringLiteral("Standstill"), QStringLiteral("PERFECT"), limits);
        }
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 0.0);
        QVERIFY(trim.isZero());
    }

    // THE COUNTER IS A RUN, NOT A TALLY. One LATE says the value stopped producing EXCELLENT, so
    // the run that would have retired it is over -- whether or not that LATE moved anything.
    void aLateOrEarlyResetsTheHoldStreak()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 3; ++i) {
            trim.observe(type, QStringLiteral("LATE"), limits);
        }
        QCOMPARE(trim.trimMsForType(type), 6.0);

        for (int i = 0; i < 11; ++i) {
            trim.observe(type, QStringLiteral("EXCELLENT"), limits);
        }
        QCOMPARE(trim.holdStreakForType(type), 11);
        QCOMPARE(trim.trimMsForType(type), 6.0);   // eleven greens, nothing moved

        // One LATE -- a single one, which the hysteresis refuses to act on -- still ends the run.
        const auto late = trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY2(!late.applied, "one LATE in a green run is not a direction");
        QCOMPARE(late.holdStreak, 0);
        QCOMPARE(trim.holdStreakForType(type), 0);

        // So the next dozen EXCELLENTs start the count over rather than resuming at 11.
        for (int i = 0; i < 11; ++i) {
            QVERIFY(!trim.observe(type, QStringLiteral("EXCELLENT"), limits).applied);
        }
        QCOMPARE(trim.trimMsForType(type), 6.0);
        QVERIFY(trim.observe(type, QStringLiteral("EXCELLENT"), limits).applied);
        QCOMPARE(trim.trimMsForType(type), 5.0);

        // An EARLY ends a run exactly as a LATE does, and an unclassified word does neither: it
        // is not evidence that the value stopped working, only that this panel said nothing.
        BannerLeadTrim other;
        for (int i = 0; i < 5; ++i) {
            other.observe(type, QStringLiteral("EXCELLENT"), limits);
        }
        QCOMPARE(other.holdStreakForType(type), 5);
        other.observe(type, QStringLiteral("GOOD"), limits);
        QCOMPARE(other.holdStreakForType(type), 5);
        other.observe(type, QStringLiteral("SLIGHTLY EARLY"), limits);
        QCOMPARE(other.holdStreakForType(type), 0);

        // The streak is per bucket, like everything else here: a green run on a fade cannot
        // retire the Standstill the owner is standing in.
        BannerLeadTrim split;
        for (int i = 0; i < 12; ++i) {
            split.observe(QStringLiteral("Left Fade"), QStringLiteral("EXCELLENT"), limits);
        }
        QCOMPARE(split.holdStreakForType(QStringLiteral("Left Fade")), 12);
        QCOMPARE(split.holdStreakForType(QStringLiteral("Standstill")), 0);
    }

    // The hold LENGTH is the setting (banner_trim_hold_shots, band 4..60), and a value no setting
    // could have produced must never degrade to "decay on the first EXCELLENT" -- that is the
    // behaviour the 09-16 evidence retired.
    void theHoldLengthComesFromTheSettingAndNeverDegradesToZero()
    {
        BannerLeadTrimLimits limits;
        limits.holdShots = 4;
        BannerLeadTrim trim;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 2; ++i) {
            trim.observe(type, QStringLiteral("LATE"), limits);
        }
        QCOMPARE(trim.trimMsForType(type), 3.0);
        for (int i = 0; i < 3; ++i) {
            QVERIFY(!trim.observe(type, QStringLiteral("EXCELLENT"), limits).applied);
        }
        QVERIFY(trim.observe(type, QStringLiteral("EXCELLENT"), limits).applied);
        QCOMPARE(trim.trimMsForType(type), 2.0);

        BannerLeadTrimLimits bogus;
        bogus.holdShots = 0;
        BannerLeadTrim guarded;
        for (int i = 0; i < 2; ++i) {
            guarded.observe(type, QStringLiteral("LATE"), bogus);
        }
        QCOMPARE(guarded.trimMsForType(type), 3.0);
        for (int i = 0; i < 11; ++i) {
            QVERIFY2(!guarded.observe(type, QStringLiteral("EXCELLENT"), bogus).applied,
                     "a 0 hold must fall back to the shipped dozen, not to an instant decay");
        }
        QCOMPARE(guarded.trimMsForType(type), 3.0);
        QVERIFY(guarded.observe(type, QStringLiteral("EXCELLENT"), bogus).applied);
        QCOMPARE(guarded.trimMsForType(type), 2.0);
    }

    // The clamp is what keeps this a TRIM and not a second, unaccountable lead control.
    void theTrimIsClampedInBothDirections()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        for (int i = 0; i < 30; ++i) {
            trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        }
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 15.0);
        // At the clamp an application is a NO-OP, not a silent saturating write.
        QVERIFY2(!trim.observe(QStringLiteral("Standstill"),
                               QStringLiteral("LATE"), limits).applied,
                 "a clamped trim must report that nothing moved");

        BannerLeadTrim early;
        for (int i = 0; i < 30; ++i) {
            early.observe(QStringLiteral("Right Fade"), QStringLiteral("EARLY"), limits);
        }
        QCOMPARE(early.trimMsForType(QStringLiteral("Right Fade")), -15.0);
    }

    // Fades are a different animation with a different landing, which is the whole reason the
    // engine keeps per-type holds. A run of late fades must not re-aim the owner's Standstill.
    void bucketsAreIsolatedPerShotType()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        trim.observe(QStringLiteral("Left Fade"), QStringLiteral("LATE"), limits);
        trim.observe(QStringLiteral("Left Fade"), QStringLiteral("LATE"), limits);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Left Fade")), 3.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 0.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Right Fade")), 0.0);

        // ...and the hysteresis window is per bucket too: one LATE in each of two buckets is
        // still two single readings, not a direction.
        BannerLeadTrim split;
        split.observe(QStringLiteral("Left Fade"), QStringLiteral("LATE"), limits);
        QVERIFY(!split.observe(QStringLiteral("Right Fade"),
                               QStringLiteral("LATE"), limits).applied);
        QVERIFY(split.isZero());
    }

    // Every shot type the engine can emit lands in exactly one of four buckets, and a word the
    // grader's library does not hold is evidence WITHOUT a direction -- never a forced bucket.
    void bucketingAndCodingAreExplicit()
    {
        QCOMPARE(BannerLeadTrim::bucketFor(QStringLiteral("Standstill")),
                 QStringLiteral("Standstill"));
        QCOMPARE(BannerLeadTrim::bucketFor(QString()), QStringLiteral("Standstill"));
        QCOMPARE(BannerLeadTrim::bucketFor(QStringLiteral("Left Fade")),
                 QStringLiteral("Left Fade"));
        QCOMPARE(BannerLeadTrim::bucketFor(QStringLiteral("Right Fade")),
                 QStringLiteral("Right Fade"));
        QCOMPARE(BannerLeadTrim::bucketFor(QStringLiteral("Go-To")), QStringLiteral("Other"));

        QCOMPARE(BannerLeadTrim::codeFor(QStringLiteral("LATE")), QLatin1Char('L'));
        QCOMPARE(BannerLeadTrim::codeFor(QStringLiteral("SLIGHTLY LATE")), QLatin1Char('L'));
        QCOMPARE(BannerLeadTrim::codeFor(QStringLiteral("EARLY")), QLatin1Char('E'));
        QCOMPARE(BannerLeadTrim::codeFor(QStringLiteral("EXCELLENT")), QLatin1Char('X'));
        QCOMPARE(BannerLeadTrim::codeFor(QStringLiteral("PERFECT")), QLatin1Char('X'));
        QCOMPARE(BannerLeadTrim::codeFor(QStringLiteral("GOOD")), QLatin1Char('O'));
        QCOMPARE(BannerLeadTrim::codeFor(QString()), QLatin1Char('O'));

        // An unclassified word is recorded (it dilutes the window, which is correct: it is a
        // shot that did NOT grade in the direction being accumulated) but moves nothing.
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        const auto other = trim.observe(QStringLiteral("Standstill"),
                                        QStringLiteral("GOOD"), limits);
        QVERIFY(!other.applied);
        QCOMPARE(other.recent, QStringLiteral("L,O"));
        QVERIFY(trim.isZero());
    }

    // The window is FOUR, oldest dropping out: a LATE five verdicts ago is not company for the
    // one that just arrived.
    void theHysteresisWindowIsFourVerdictsLong()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        for (int i = 0; i < 4; ++i) {
            trim.observe(QStringLiteral("Standstill"), QStringLiteral("GOOD"), limits);
        }
        const auto late = trim.observe(QStringLiteral("Standstill"),
                                       QStringLiteral("LATE"), limits);
        QCOMPARE(late.recent, QStringLiteral("O,O,O,L"));
        QVERIFY2(!late.applied, "the first LATE has aged out of the window");
        QVERIFY(trim.isZero());
    }

    // The owner moved the slider. Every verdict the trim was built from graded the OLD value.
    void aResetClearsBothTheTrimAndItsWindow()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        QVERIFY(trim.reset());
        QVERIFY(trim.isZero());
        QCOMPARE(trim.recentString(QStringLiteral("Standstill")), QString());
        QVERIFY2(!trim.reset(), "a reset of an already-clear trim reports no change");
        // The window went with it, so the next move costs two verdicts again.
        QVERIFY(!trim.observe(QStringLiteral("Standstill"),
                              QStringLiteral("LATE"), limits).applied);
        QVERIFY(trim.isZero());
    }

    // Contexts change between sessions -- a rapid-fire drill is not a 5v5 game, and that is the
    // whole measurement behind this feature. Last night's trim comes back as EVIDENCE at half
    // strength, and it has to re-earn the rest.
    void theStartRestoreHalvesTheTrimAndDropsCorruptEntries()
    {
        BannerLeadTrim trim;
        QMap<QString, double> persisted;
        persisted.insert(QStringLiteral("Standstill"), 9.0);
        persisted.insert(QStringLiteral("Left Fade"), -6.0);
        persisted.insert(QStringLiteral("Right Fade"), 0.06);      // decays below the epsilon
        persisted.insert(QStringLiteral("Other"), 1000.0);         // out of band
        persisted.insert(QStringLiteral("Bogus Type"), 5.0);       // not a bucket this build owns
        trim.restoreDecayed(persisted, 15.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 4.5);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Left Fade")), -3.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Right Fade")), 0.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Other")), 0.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Bogus Type")), 0.0);

        // The hysteresis window is NOT restored: last night is not evidence about tonight.
        QCOMPARE(trim.recentString(QStringLiteral("Standstill")), QString());
        const BannerLeadTrimLimits limits;
        QVERIFY(!trim.observe(QStringLiteral("Standstill"),
                              QStringLiteral("LATE"), limits).applied);

        // A restore is also clamped to the LIVE maximum, so lowering the setting cannot leave a
        // larger trim standing.
        BannerLeadTrim tight;
        tight.restoreDecayed(persisted, 2.0);
        QCOMPARE(tight.trimMsForType(QStringLiteral("Standstill")), 2.0);
        QCOMPARE(tight.trimMsForType(QStringLiteral("Left Fade")), -2.0);
    }

    // ...and the same guarantee against a clamp lowered while a trim is already standing.
    void loweringTheClampPullsAStandingTrimBackIntoBand()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        for (int i = 0; i < 10; ++i) {
            trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        }
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 15.0);
        QVERIFY(trim.clampTo(4.0));
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 4.0);
        QVERIFY2(!trim.clampTo(4.0), "an in-band trim reports no change");
    }

    // A zero step is a valid request: "count the verdicts, move nothing". It must not be
    // reported as an application, or the log and the persistence would churn on every shot.
    void aZeroStepRecordsEvidenceAndMovesNothing()
    {
        BannerLeadTrim trim;
        BannerLeadTrimLimits limits;
        limits.stepMs = 0.0;
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits);
        QVERIFY(!trim.observe(QStringLiteral("Standstill"),
                              QStringLiteral("LATE"), limits).applied);
        QVERIFY(trim.isZero());
    }

    // ===== [ORION_RELEASE_ORACLE_TRIM 2026-09-15 owner] the banner-free descent ==============
    //
    // The reader's post-release retraction gap is the make/miss oracle (<= 3 px = EXCELLENT,
    // >= 4 px = a miss; 27/27 against panel_grade on the 09-15 framedump, 25/25 against the live
    // banner) -- but it is UNSIGNED. These pin the 1-D descent that turns an unsigned error
    // magnitude into a lead correction, in the pure policy, with no engine and no clock.

    // THE HEADLINE: no banner at all, 12 ms of lead error, and the loop finds it.
    void theOracleDescentConvergesFromTwelveMsWrongWithNoBanner()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;   // step 3, max 15, decay 1
        const OracleRig rig;
        const QString type = QStringLiteral("Standstill");

        QCOMPARE(rig.gapPxFor(0.0), 10.8);   // where it starts: 12 ms wrong = 10.8 px of gap
        BannerLeadTrim::OracleApplication lastDecision;
        for (int shot = 1; shot <= 12; ++shot) {
            const double held = trim.trimMsForType(type);
            const auto out = trim.observeOracle(type, rig.gapPxFor(held),
                                                rig.greenFor(held), limits);
            if (out.decision) {
                lastDecision = out;
            }
        }
        const double settled = trim.trimMsForType(type);
        // Twelve shots is four decision points, i.e. at most four 3 ms steps. Landing within one
        // step of the truth is the whole claim.
        QVERIFY2(std::abs(rig.trueErrorMs - settled) <= 4.0,
                 qPrintable(QStringLiteral("settled at %1 ms").arg(settled)));
        // ...and the run it settled on reads as inside the window on the instrument's own
        // boundary: the last decision's MEDIAN gap is in the deadband, down from 10.8 px.
        QVERIFY2(lastDecision.medianGapPx >= 0.0
                     && lastDecision.medianGapPx <= BannerLeadTrim::kOracleGapDeadbandPx,
                 qPrintable(QStringLiteral("median gap %1 px").arg(lastDecision.medianGapPx)));
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] The last three shots land GREEN (2.7 px), so the
        // final decision point HOLDS the value that produced them. Under the old
        // decay-on-every-green rule the eleventh shot walked the trim 9 -> 8 and the twelfth
        // promptly missed again (3.6 px); the hold is what lets the descent PARK on what works.
        QCOMPARE(lastDecision.reason, QStringLiteral("hold"));
        QCOMPARE(settled, 9.0);
        // It never blew past the clamp on the way...
        QVERIFY(settled <= 15.0);
        // ...and the direction was right from the start, so it was never flipped.
        QCOMPARE(trim.oracleDirectionForType(type), 1);
    }

    // ONE ORACLE IS NOT A DIRECTION. The banner path needs two verdicts; this one needs three
    // graded shots, for the same reason: an unsigned single reading cannot be told from noise.
    void twoOraclesAreEvidenceAndTheThirdIsTheStep()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        QVERIFY(!trim.observeOracle(type, 10.8, false, limits).applied);
        QVERIFY(!trim.observeOracle(type, 10.8, false, limits).applied);
        const auto third = trim.observeOracle(type, 10.8, false, limits);
        QVERIFY(third.applied);
        QCOMPARE(third.afterMs, 3.0);
        QCOMPARE(third.reason, QStringLiteral("step"));
        QCOMPARE(third.medianGapPx, 10.8);
        QCOMPARE(trim.trimMsForType(type), 3.0);
    }

    // THE ONLY SIGN INFORMATION IN AN UNSIGNED INSTRUMENT: if the gap GREW, the direction was
    // wrong. Six shots whose second batch is worse than the first must reverse the descent.
    void aWorseningGapFlipsTheDescentDirection()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        // Batch 1: 6 px. The first decision steps +3 (the default direction).
        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(type, 6.0, false, limits);
        }
        QCOMPARE(trim.trimMsForType(type), 3.0);
        QCOMPARE(trim.oracleDirectionForType(type), 1);
        // Batch 2: 9 px -- the step made it WORSE. The second decision flips to -1 and then
        // steps in the new direction, so the trim comes back to 0 rather than to +6.
        for (int i = 0; i < 2; ++i) {
            QVERIFY(!trim.observeOracle(type, 9.0, false, limits).applied);
        }
        const auto sixth = trim.observeOracle(type, 9.0, false, limits);
        QVERIFY(sixth.applied);
        QVERIFY(sixth.flipped);
        QCOMPARE(sixth.reason, QStringLiteral("flip_step"));
        QCOMPARE(sixth.direction, -1);
        QCOMPARE(sixth.previousMedianGapPx, 6.0);
        QCOMPARE(sixth.medianGapPx, 9.0);
        QCOMPARE(trim.trimMsForType(type), 0.0);
        QCOMPARE(trim.oracleDirectionForType(type), -1);

        // An UNCHANGED median is not evidence that the direction is wrong. Flipping on it would
        // make a rig that is simply sitting at its floor oscillate forever.
        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(type, 9.0, false, limits);
        }
        QCOMPARE(trim.oracleDirectionForType(type), -1);
    }

    // BELOW THE DEADBAND THERE IS NOTHING TO CHASE. 3.5 px is the midpoint of the instrument's
    // own measured separation, so a median under it says the landing is already in the window.
    void aGapInsideTheDeadbandNeverSteps()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 9; ++i) {
            const auto out = trim.observeOracle(type, 3.4, false, limits);
            if (out.decision) {
                QCOMPARE(out.reason, QStringLiteral("deadband"));
            }
            QVERIFY(!out.applied);
        }
        QVERIFY(trim.isZero());
    }

    // A GREEN ORACLE IS AN EXCELLENT. Same 2-of-4 hysteresis, same HOLD, same idle decay past the
    // same dozen, and it never steps: "the value is right" means the same thing whichever
    // instrument said it, and so does "it has been right long enough to be worth retiring".
    // [ORION_BANNER_TRIM_HOLD 2026-09-16] This used to decay on the second green.
    void aGreenOracleHoldsTheTrimExactlyAsExcellentDoes()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 12; ++i) {
            trim.observeOracle(type, 10.8, false, limits);
        }
        QCOMPARE(trim.trimMsForType(type), 12.0);
        QCOMPARE(trim.holdStreakForType(type), 0);   // every one of those was a MISS
        // One green is a single reading -- exactly as one EXCELLENT is.
        const auto first = trim.observeOracle(type, 1.2, true, limits);
        QVERIFY(!first.applied);
        QCOMPARE(first.reason, QStringLiteral("evidence"));
        QCOMPARE(first.holdStreak, 1);
        QCOMPARE(trim.trimMsForType(type), 12.0);
        // ...and from there it HOLDS, for the same eleven shots the banner's EXCELLENT holds.
        for (int i = 2; i <= 11; ++i) {
            const auto held = trim.observeOracle(type, 1.2, true, limits);
            QVERIFY2(!held.applied, qPrintable(QStringLiteral("green #%1 must hold").arg(i)));
            QCOMPARE(held.reason, QStringLiteral("hold"));
            QCOMPARE(held.holdStreak, i);
            QCOMPARE(trim.trimMsForType(type), 12.0);
        }
        const auto twelfth = trim.observeOracle(type, 1.2, true, limits);
        QVERIFY(twelfth.applied);
        QCOMPARE(twelfth.reason, QStringLiteral("idle_decay"));
        QCOMPARE(trim.trimMsForType(type), 11.0);

        // ONE COUNTER, ONE TRIM: a MISS ends a green run exactly as a LATE does, and the two
        // instruments share it because they are describing the same value.
        const auto missed = trim.observeOracle(type, 9.0, false, limits);
        QCOMPARE(missed.holdStreak, 0);
        QCOMPARE(trim.holdStreakForType(type), 0);
        BannerLeadTrim mixed;
        for (int i = 0; i < 6; ++i) {
            mixed.observe(type, QStringLiteral("EXCELLENT"), limits);
        }
        QCOMPARE(mixed.holdStreakForType(type), 6);
        QCOMPARE(mixed.observeOracle(type, 1.0, true, limits).holdStreak, 7);
        QCOMPARE(mixed.observe(type, QStringLiteral("LATE"), limits).holdStreak, 0);
    }

    // The clamp is the trim's, not the instrument's: an oracle-driven descent stops at exactly
    // the same rail a run of LATEs stops at.
    void theOracleDescentIsClampedAtTheSameBand()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 30; ++i) {
            trim.observeOracle(type, 40.0, false, limits);
        }
        QCOMPARE(trim.trimMsForType(type), 15.0);
        const auto pinned = trim.observeOracle(type, 40.0, false, limits);
        QVERIFY(!pinned.applied);
        // ...and the other rail, through a flipped direction.
        BannerLeadTrim down;
        for (int i = 0; i < 3; ++i) {
            down.observeOracle(type, 5.0, false, limits);
        }
        for (int i = 0; i < 60; ++i) {
            down.observeOracle(type, 40.0, false, limits);
        }
        QCOMPARE(down.oracleDirectionForType(type), -1);
        QCOMPARE(down.trimMsForType(type), -15.0);
    }

    // BUCKETS, and the descent's own state, are per shot type: a run of late fades must not
    // re-aim the Standstill the owner is standing in, and must not steal its direction either.
    void theOracleDescentKeepsItsBucketsIsolated()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(QStringLiteral("Left Fade"), 10.8, false, limits);
        }
        QCOMPARE(trim.trimMsForType(QStringLiteral("Left Fade")), 3.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 0.0);
        QCOMPARE(trim.recentString(QStringLiteral("Left Fade")), QString());   // banner window
        QCOMPARE(trim.oracleRecentString(QStringLiteral("Left Fade")),
                 QStringLiteral("M,M,M"));
        QCOMPARE(trim.oracleRecentString(QStringLiteral("Standstill")), QString());
    }

    // PERSISTENCE IS UNCHANGED. Only the trim map crosses a restart; the descent's series,
    // window, count and direction are session evidence exactly as the banner's window is.
    void theOracleStateIsSessionEvidenceAndNeverPersisted()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(type, 6.0, false, limits);
        }
        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(type, 9.0, false, limits);   // flips to -1
        }
        QCOMPARE(trim.oracleDirectionForType(type), -1);
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] The hold streak is session evidence too, so a green
        // run standing right now must not survive a restart either.
        for (int i = 0; i < 4; ++i) {
            trim.observeOracle(type, 1.0, true, limits);
        }
        QCOMPARE(trim.holdStreakForType(type), 4);
        const QMap<QString, double> persisted = trim.snapshot();
        QCOMPARE(persisted.size(), 1);   // the trim map, and nothing else, is what is written

        BannerLeadTrim restarted;
        restarted.restoreDecayed(persisted, 15.0);
        QCOMPARE(restarted.trimMsForType(type), 0.5 * persisted.value(type));
        QCOMPARE(restarted.oracleDirectionForType(type), 1);          // back to the default
        QCOMPARE(restarted.oracleRecentString(type), QString());      // and no inherited window
        QCOMPARE(restarted.holdStreakForType(type), 0);               // nor an inherited run
        QVERIFY(!restarted.observeOracle(type, 9.0, false, limits).applied);

        // The owner moving the slider clears the descent too -- every gap in it was measured
        // against the old value.
        trim.reset();
        QCOMPARE(trim.oracleDirectionForType(type), 1);
        QCOMPARE(trim.oracleRecentString(type), QString());
        QCOMPARE(trim.holdStreakForType(type), 0);
        QCOMPARE(trim.trimMsForType(type), 0.0);
    }

    // == [ORION_BANNER_TRIM_ALTERNATION 2026-09-16 owner] ==============================
    //
    // THE 2026-09-16 21:00 SESSION IN ONE TEST. `recent=E,L,E,L` SATISFIES the 2-of-4
    // hysteresis -- two L's and two E's sit in the window -- so before this change every
    // single verdict of an alternation stepped the trim at full size: +3, -3, +3, -3. A
    // coin flip is not a direction, and a gate that a coin flip walks through is not a gate.
    void anAlternationHoldsTheTrimAndNamesItself()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");

        const auto e1 = trim.observe(type, QStringLiteral("EARLY"), limits);
        QVERIFY2(!e1.applied, "the first verdict is never a direction");
        QCOMPARE(e1.reason, QStringLiteral("evidence"));   // no predecessor, not an alternation

        const auto l1 = trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY(!l1.applied);
        QCOMPARE(l1.reason, QStringLiteral("alternating"));
        const auto e2 = trim.observe(type, QStringLiteral("EARLY"), limits);
        QVERIFY(!e2.applied);
        QCOMPARE(e2.reason, QStringLiteral("alternating"));
        const auto l2 = trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY(!l2.applied);
        QCOMPARE(l2.reason, QStringLiteral("alternating"));
        // The window still shows all four -- the display is unchanged, only the gate moved --
        // and that window is exactly the one the old rule stepped on every shot.
        QCOMPARE(l2.recent, QStringLiteral("E,L,E,L"));
        QCOMPARE(l2.sameDirection, 2);
        QCOMPARE(trim.trimMsForType(type), 0.0);
        QVERIFY2(trim.isZero(), "four alternating verdicts must move the lead by nothing at all");

        // ...and the loop is not deaf, it is PATIENT, and the wait is exactly one shot long: the
        // alternation ended on an L, so the very next LATE is the second consecutive one and it
        // steps exactly as it always did. The guard costs a genuine direction one verdict, once.
        const auto stepped = trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY(stepped.applied);
        QCOMPARE(stepped.reason, QStringLiteral("step"));
        QCOMPARE(stepped.recent, QStringLiteral("L,E,L,L"));
        QCOMPARE(trim.trimMsForType(type), 3.0);
    }

    // `alternating` is a NAME for one pathology, not a synonym for "refused". An EXCELLENT
    // between two lates, or an unclassified word, is ordinary `evidence` -- the loop did not
    // watch a coin flip there, it simply has not seen two of one direction in a row.
    void alternatingIsReservedForOppositeDirections()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Left Fade");
        trim.observe(type, QStringLiteral("LATE"), limits);
        QCOMPARE(trim.observe(type, QStringLiteral("EXCELLENT"), limits).reason,
                 QStringLiteral("evidence"));
        const auto afterGreen = trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY(!afterGreen.applied);
        QCOMPARE(afterGreen.reason, QStringLiteral("evidence"));

        BannerLeadTrim other;
        other.observe(type, QStringLiteral("LATE"), limits);
        other.observe(type, QStringLiteral("GOOD"), limits);
        const auto afterWord = other.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY2(!afterWord.applied, "a LATE separated by an unclassified word is not consecutive");
        QCOMPARE(afterWord.reason, QStringLiteral("evidence"));
    }

    // The descent's half of the same rule: the oracle is unsigned, so its coin flip is a MISS
    // straight after a GREEN. A batch that closes on one does not get to step.
    void anOracleMissAfterAGreenDoesNotStep()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        // Two misses then a green: the third shot closes a batch, but on a green -- the hold
        // path -- so nothing moves and the NEXT miss is the one that follows a green.
        trim.observeOracle(type, 9.0, false, limits);
        trim.observeOracle(type, 9.0, false, limits);
        trim.observeOracle(type, 0.0, true, limits);
        QCOMPARE(trim.trimMsForType(type), 0.0);

        trim.observeOracle(type, 9.0, false, limits);
        trim.observeOracle(type, 9.0, false, limits);
        const auto sixth = trim.observeOracle(type, 9.0, false, limits);
        QVERIFY2(sixth.decision, "every third graded oracle closes a batch");
        QVERIFY2(sixth.applied, "two consecutive misses ARE a direction");

        // Now the pathology itself: a green, then a miss that closes a batch.
        BannerLeadTrim flip;
        flip.observeOracle(type, 9.0, false, limits);
        flip.observeOracle(type, 0.0, true, limits);
        const auto third = flip.observeOracle(type, 9.0, false, limits);
        QVERIFY(third.decision);
        QVERIFY2(!third.applied, "a miss straight after a green is a coin flip, not a direction");
        QCOMPARE(third.reason, QStringLiteral("alternating"));
        QVERIFY(flip.isZero());
    }

    // == [ORION_BANNER_TRIM_TEMPO 2026-09-16 owner] ====================================
    //
    // THE OTHER HALF OF THE SAME SESSION. The alternation above happened because the owner's
    // shot tempo changed under a single bucket: the quick presses landed in a ~608 ms window
    // and the normal ones in a ~650 ms window, so one trim was being pulled both ways. Three
    // onsets, three sub-buckets, three independent trims.
    void theOnsetPicksTheTempoSubBucket()
    {
        using Tempo = BannerLeadTrim::Tempo;
        // The measured classes from the 2026-09-16 21:00 session, engine-side onsets.
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 467.0) == Tempo::Quick);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 480.0) == Tempo::Quick);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 518.0) == Tempo::Normal);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 578.0) == Tempo::Normal);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 640.0) == Tempo::Slow);
        // The cut points are INCLUSIVE of normal on both sides: quick is strictly below and
        // slow strictly above, so a shot sitting exactly on a boundary is the reference class.
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 500.0) == Tempo::Normal);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), 580.0) == Tempo::Normal);
        // Fades are a different animation and carry their own band.
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Left Fade"), 766.0) == Tempo::Quick);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Right Fade"), 850.0) == Tempo::Normal);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Right Fade"), 1051.0) == Tempo::Slow);
        // A standstill onset would read as "quick" on the fade band and vice versa, which is
        // precisely why the bands are per type rather than one global pair.
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Left Fade"), 520.0) == Tempo::Quick);
        // No onset, a negative one and a NaN are all the REFERENCE class, never a guess.
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"), -1.0) == Tempo::Normal);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Standstill"),
                                         std::numeric_limits<double>::quiet_NaN())
                == Tempo::Normal);
        // "Other" (Go-To and the Tempo words) surfaces its meter seconds into the hold, so an
        // onset there measures the player walking, not the animation: one bucket, as before.
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Go-To"), 120.0) == Tempo::Normal);
        QVERIFY(BannerLeadTrim::tempoFor(QStringLiteral("Go-To"), 4000.0) == Tempo::Normal);

        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Standstill"), Tempo::Quick, true),
                 QStringLiteral("Standstill/quick"));
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Right Fade"), Tempo::Slow, true),
                 QStringLiteral("Right Fade/slow"));
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Standstill"), Tempo::Quick, false),
                 QStringLiteral("Standstill"));
    }

    // Three tempos inside ONE shot type are three trims. A run of late quick presses must not
    // re-aim the normal ones -- that is the entire reason this split exists.
    void theTempoSubBucketsAreIsolatedTrims()
    {
        using Tempo = BannerLeadTrim::Tempo;
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");

        // Two consecutive LATEs on the QUICK class.
        trim.observe(type, QStringLiteral("LATE"), limits, 470.0);
        const auto quick = trim.observe(type, QStringLiteral("LATE"), limits, 468.0);
        QVERIFY(quick.applied);
        QCOMPARE(quick.key, QStringLiteral("Standstill/quick"));
        QVERIFY(quick.tempo == Tempo::Quick);
        QCOMPARE(quick.onsetMs, 468.0);
        QCOMPARE(quick.type, QStringLiteral("Standstill"));   // `type` stays the SHOT-TYPE bucket
        QCOMPARE(trim.trimMsForType(type, Tempo::Quick), 3.0);
        QCOMPARE(trim.trimMsForType(type, Tempo::Normal), 0.0);
        QCOMPARE(trim.trimMsForType(type, Tempo::Slow), 0.0);
        QCOMPARE(trim.trimMsForType(type), 0.0);   // the card reads NORMAL, and it has not moved

        // Two consecutive EARLIES on the NORMAL class, the opposite direction, at the same time.
        trim.observe(type, QStringLiteral("EARLY"), limits, 520.0);
        const auto normal = trim.observe(type, QStringLiteral("EARLY"), limits, 533.0);
        QVERIFY(normal.applied);
        QCOMPARE(normal.key, QStringLiteral("Standstill/normal"));
        QCOMPARE(trim.trimMsForType(type, Tempo::Normal), -3.0);
        QCOMPARE(trim.trimMsForType(type, Tempo::Quick), 3.0);
        QCOMPARE(trim.trimMsForType(type), -3.0);

        // ...and the windows are per sub-bucket too, so the quick class's L,L is not company
        // for a slow LATE arriving now.
        const auto slow = trim.observe(type, QStringLiteral("LATE"), limits, 640.0);
        QVERIFY2(!slow.applied, "one LATE in a fresh sub-bucket is one reading");
        QCOMPARE(slow.key, QStringLiteral("Standstill/slow"));
        QCOMPARE(slow.recent, QStringLiteral("L"));
        QCOMPARE(trim.trimMsForType(type, Tempo::Slow), 0.0);

        // THE 09-16 21:00 SEQUENCE, replayed: the same E,L,E,L that alternated under one bucket
        // is two clean per-class runs once the onsets are carried -- and it CONVERGES.
        BannerLeadTrim live;
        live.observe(type, QStringLiteral("EARLY"), limits, 520.0);   // normal
        live.observe(type, QStringLiteral("LATE"), limits, 469.0);    // quick
        live.observe(type, QStringLiteral("EARLY"), limits, 533.0);   // normal -> steps
        live.observe(type, QStringLiteral("LATE"), limits, 467.0);    // quick  -> steps
        QCOMPARE(live.trimMsForType(type, Tempo::Normal), -3.0);
        QCOMPARE(live.trimMsForType(type, Tempo::Quick), 3.0);
    }

    // The hold streak and the descent are session evidence PER SUB-BUCKET, for the same reason
    // the trim is: a dozen greens on the normal class say nothing about the quick one.
    void theHoldStreakAndDescentAreAlsoPerTempo()
    {
        using Tempo = BannerLeadTrim::Tempo;
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 5; ++i) {
            trim.observe(type, QStringLiteral("EXCELLENT"), limits, 520.0);
        }
        QCOMPARE(trim.holdStreakForType(type, Tempo::Normal), 5);
        QCOMPARE(trim.holdStreakForType(type, Tempo::Quick), 0);

        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(type, 9.0, false, limits, 470.0);
        }
        QCOMPARE(trim.trimMsForType(type, Tempo::Quick), 3.0);
        QCOMPARE(trim.trimMsForType(type, Tempo::Normal), 0.0);
        QCOMPARE(trim.oracleRecentString(type, Tempo::Quick), QStringLiteral("M,M,M"));
        QCOMPARE(trim.oracleRecentString(type, Tempo::Normal), QString());
        // Five greens on the normal class did not survive into the quick one's streak.
        QCOMPARE(trim.holdStreakForType(type, Tempo::Quick), 0);
        QCOMPARE(trim.holdStreakForType(type, Tempo::Normal), 5);
    }

    // PERSISTENCE. The keys on disk are "type/tempo", a file written before this change loads
    // into the REFERENCE class, and a key no build of ours could have written is still refused.
    void theTempoKeysPersistAndLegacyKeysLoadIntoNormal()
    {
        using Tempo = BannerLeadTrim::Tempo;
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits, 470.0);
        trim.observe(QStringLiteral("Standstill"), QStringLiteral("LATE"), limits, 470.0);
        trim.observe(QStringLiteral("Left Fade"), QStringLiteral("EARLY"), limits, 1000.0);
        trim.observe(QStringLiteral("Left Fade"), QStringLiteral("EARLY"), limits, 1000.0);
        const QMap<QString, double> written = trim.snapshot();
        QCOMPARE(written.value(QStringLiteral("Standstill/quick")), 3.0);
        QCOMPARE(written.value(QStringLiteral("Left Fade/slow")), -3.0);
        QCOMPARE(written.size(), 2);

        BannerLeadTrim restarted;
        restarted.restoreDecayed(written, 15.0, /*tempoBuckets=*/true);
        QCOMPARE(restarted.trimMsForType(QStringLiteral("Standstill"), Tempo::Quick), 1.5);
        QCOMPARE(restarted.trimMsForType(QStringLiteral("Left Fade"), Tempo::Slow), -1.5);
        QCOMPARE(restarted.trimMsForType(QStringLiteral("Standstill"), Tempo::Normal), 0.0);

        // A file from before this change: un-suffixed keys load into the REFERENCE class, which
        // is where the overwhelming majority of their evidence came from.
        QMap<QString, double> legacy;
        legacy.insert(QStringLiteral("Standstill"), 9.0);
        legacy.insert(QStringLiteral("Right Fade"), -6.0);
        legacy.insert(QStringLiteral("Bogus Type"), 5.0);              // not a bucket we own
        legacy.insert(QStringLiteral("Standstill/sprint"), 9.0);       // not a tempo we write
        legacy.insert(QStringLiteral("Bogus Type/quick"), 9.0);        // neither half is ours
        BannerLeadTrim migrated;
        migrated.restoreDecayed(legacy, 15.0, /*tempoBuckets=*/true);
        QCOMPARE(migrated.trimMsForType(QStringLiteral("Standstill"), Tempo::Normal), 4.5);
        QCOMPARE(migrated.trimMsForType(QStringLiteral("Right Fade"), Tempo::Normal), -3.0);
        QCOMPARE(migrated.trimMsForType(QStringLiteral("Standstill"), Tempo::Quick), 0.0);
        QCOMPARE(migrated.snapshot().size(), 2);
    }

    // THE KILL SWITCH. With the tempo buckets disarmed every key, every window and every read is
    // byte-for-byte the 2026-09-16 build's -- the onset is carried and ignored.
    void theTempoKillSwitchCollapsesToTheTypeBucket()
    {
        using Tempo = BannerLeadTrim::Tempo;
        BannerLeadTrimLimits off;
        off.tempoBuckets = false;
        BannerLeadTrim trim;
        const QString type = QStringLiteral("Standstill");
        trim.observe(type, QStringLiteral("LATE"), off, 470.0);   // quick...
        const auto second = trim.observe(type, QStringLiteral("LATE"), off, 640.0);   // ...and slow
        QVERIFY2(second.applied, "with the split off these are two lates in ONE bucket");
        QCOMPARE(second.key, QStringLiteral("Standstill"));
        QVERIFY(second.tempo == Tempo::Slow);   // still REPORTED, so a log reads the same either way
        QCOMPARE(second.onsetMs, 640.0);
        QCOMPARE(trim.trimMsForType(type), 3.0);
        QCOMPARE(trim.trimMsForType(type, Tempo::Quick), 3.0);   // every read finds the one key
        QCOMPARE(trim.trimMsForType(type, Tempo::Slow), 3.0);
        QCOMPARE(trim.snapshot().keys(), QStringList{QStringLiteral("Standstill")});
        QCOMPARE(trim.recentString(type), QStringLiteral("L,L"));

        // ...and the restore rule is the 2026-09-16 one exactly: a key that does not bucket to
        // ITSELF is not ours to install, tempo-suffixed keys included.
        QMap<QString, double> persisted;
        persisted.insert(QStringLiteral("Standstill"), 9.0);
        persisted.insert(QStringLiteral("Standstill/quick"), 9.0);
        BannerLeadTrim restarted;
        restarted.restoreDecayed(persisted, 15.0, /*tempoBuckets=*/false);
        QCOMPARE(restarted.snapshot().keys(), QStringList{QStringLiteral("Standstill")});
        QCOMPARE(restarted.trimMsForType(type), 4.5);
    }

    // === [ORION_BANNER_TRIM_RANGE 2026-09-17 owner] the FADE range sub-buckets ===============
    //
    // "middy fades should perform just as standstill shots (slightly bigger window); I'm fine
    // with 50-60 % for three-point fades." A mid-range fade and a three-point fade are different
    // animations into different windows, and the 09-16 night graded 13 EXCELLENT / 8 LATE /
    // 3 EARLY on fades sharing ONE bucket per tempo.
    void theRangeKeyIsFadeOnlyAndUnknownFallsBackToTheTempoBucket()
    {
        using Range = BannerLeadTrim::Range;
        using Tempo = BannerLeadTrim::Tempo;
        // A FADE with a named range gains the third segment...
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Left Fade"), Tempo::Normal, true,
                                        Range::Mid, true),
                 QStringLiteral("Left Fade/normal/mid"));
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Right Fade"), Tempo::Slow, true,
                                        Range::Three, true),
                 QStringLiteral("Right Fade/slow/three"));
        // ...an UNKNOWN range does not: that is the 2026-09-16 key, which is what an install
        // where the sidecar never reads the nameplate keeps using.
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Left Fade"), Tempo::Normal, true,
                                        Range::Unknown, true),
                 QStringLiteral("Left Fade/normal"));
        // ...and neither does a STANDSTILL, whatever range is handed in: range is a fade-only
        // dimension, because splitting a bucket that is already producing EXCELLENT halves its
        // evidence for nothing.
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Standstill"), Tempo::Quick, true,
                                        Range::Mid, true),
                 QStringLiteral("Standstill/quick"));
        QCOMPARE(BannerLeadTrim::keyFor(QStringLiteral("Go-To"), Tempo::Normal, true,
                                        Range::Three, true),
                 QStringLiteral("Other/normal"));
        // rangeFor is the single place that rule lives.
        QVERIFY(BannerLeadTrim::rangeFor(QStringLiteral("Left Fade"), Range::Mid) == Range::Mid);
        QVERIFY(BannerLeadTrim::rangeFor(QStringLiteral("Standstill"), Range::Mid)
                == Range::Unknown);
    }

    void theRangeSubBucketsAreIsolatedTrims()
    {
        using Range = BannerLeadTrim::Range;
        BannerLeadTrim trim;
        BannerLeadTrimLimits limits;          // step 3, max 15, tempo + range buckets on
        const QString type = QStringLiteral("Left Fade");
        // Two consecutive LATEs on a MID fade move only the mid bucket.
        QVERIFY(!trim.observe(type, QStringLiteral("LATE"), limits, 800.0, Range::Mid).applied);
        const auto mid = trim.observe(type, QStringLiteral("LATE"), limits, 800.0, Range::Mid);
        QVERIFY(mid.applied);
        QCOMPARE(mid.key, QStringLiteral("Left Fade/normal/mid"));
        QVERIFY(mid.range == Range::Mid);
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Mid), 3.0);
        // The THREE bucket has not moved, and neither has the un-ranged one.
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Three), 0.0);
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Unknown), 0.0);
        QCOMPARE(trim.trimMsForType(QStringLiteral("Standstill")), 0.0);
        // ...and a three-point fade needs its OWN two verdicts, in its own direction.
        QVERIFY(!trim.observe(type, QStringLiteral("EARLY"), limits, 800.0, Range::Three).applied);
        const auto three = trim.observe(type, QStringLiteral("EARLY"), limits, 800.0,
                                        Range::Three);
        QVERIFY(three.applied);
        QCOMPARE(three.key, QStringLiteral("Left Fade/normal/three"));
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Three), -3.0);
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Mid), 3.0);
        QCOMPARE(trim.snapshot().size(), 2);
    }

    void anUnknownRangeReadsTheTempoBucketItWouldHaveWritten()
    {
        using Range = BannerLeadTrim::Range;
        BannerLeadTrim trim;
        BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Right Fade");
        // Two lates on a fade whose range the sidecar could NOT read: the 2026-09-16 key.
        QVERIFY(!trim.observe(type, QStringLiteral("LATE"), limits, 800.0).applied);
        const auto unknown = trim.observe(type, QStringLiteral("LATE"), limits, 800.0);
        QVERIFY(unknown.applied);
        QCOMPARE(unknown.key, QStringLiteral("Right Fade/normal"));
        QVERIFY(unknown.range == Range::Unknown);
        // A LATER shot that DOES read `mid` has no bucket of its own yet, so it spends the
        // (type, tempo) evidence rather than starting from zero -- which is what makes the first
        // ranged shot of a session behave like today's build instead of like a fresh install.
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Mid), 3.0);
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal), 3.0);
    }

    void theOracleDescentIsAlsoPerRange()
    {
        using Range = BannerLeadTrim::Range;
        BannerLeadTrim trim;
        BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Left Fade");
        for (int i = 0; i < 3; ++i) {
            trim.observeOracle(type, 9.0, false, limits, 800.0, Range::Mid);
        }
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Mid), 3.0);
        QCOMPARE(trim.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Three), 0.0);
        // The hold streak is per key too -- it is ONE counter per bucket, and a bucket is now
        // (type, tempo, range) on a fade.
        for (int i = 0; i < 2; ++i) {
            trim.observeOracle(type, 0.0, true, limits, 800.0, Range::Three);
        }
        QCOMPARE(trim.holdStreakForType(type, BannerLeadTrim::Tempo::Normal, Range::Three), 2);
        QCOMPARE(trim.holdStreakForType(type, BannerLeadTrim::Tempo::Normal, Range::Mid), 0);
    }

    void theRangeKeysPersistAndLegacyKeysStillLoad()
    {
        using Range = BannerLeadTrim::Range;
        BannerLeadTrim trim;
        BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Left Fade");
        trim.observe(type, QStringLiteral("LATE"), limits, 800.0, Range::Mid);
        trim.observe(type, QStringLiteral("LATE"), limits, 800.0, Range::Mid);
        QCOMPARE(trim.snapshot().value(QStringLiteral("Left Fade/normal/mid")), 3.0);

        // Restart: every shape this project has ever written loads, at half strength.
        QMap<QString, double> persisted;
        persisted.insert(QStringLiteral("Left Fade/normal/mid"), 8.0);     // this build
        persisted.insert(QStringLiteral("Left Fade/normal/three"), 6.0);   // this build
        persisted.insert(QStringLiteral("Right Fade/quick"), 4.0);         // 2026-09-16
        persisted.insert(QStringLiteral("Standstill"), 10.0);              // pre-2026-09-16
        persisted.insert(QStringLiteral("Left Fade/normal/banana"), 9.0);  // not ours
        persisted.insert(QStringLiteral("Standstill/normal/mid"), 9.0);    // range is fade-only
        persisted.insert(QStringLiteral("Left Fade/mid"), 9.0);            // a range with no tempo
        BannerLeadTrim restarted;
        restarted.restoreDecayed(persisted, 15.0, true, true);
        QCOMPARE(restarted.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Mid), 4.0);
        QCOMPARE(restarted.trimMsForType(type, BannerLeadTrim::Tempo::Normal, Range::Three), 3.0);
        QCOMPARE(restarted.trimMsForType(QStringLiteral("Right Fade"),
                                         BannerLeadTrim::Tempo::Quick), 2.0);
        QCOMPARE(restarted.trimMsForType(QStringLiteral("Standstill")), 5.0);
        QCOMPARE(restarted.snapshot().size(), 4);   // the three junk keys are refused
    }

    void theRangeKillSwitchCollapsesToTheTempoBucket()
    {
        using Range = BannerLeadTrim::Range;
        BannerLeadTrim trim;
        BannerLeadTrimLimits off;
        off.rangeBuckets = false;                // ORION_BANNER_TRIM_RANGE=0
        const QString type = QStringLiteral("Left Fade");
        const auto first = trim.observe(type, QStringLiteral("LATE"), off, 800.0, Range::Mid);
        QCOMPARE(first.key, QStringLiteral("Left Fade/normal"));
        QVERIFY(first.range == Range::Mid);       // still REPORTED, so the log reads the same
        const auto second = trim.observe(type, QStringLiteral("LATE"), off, 800.0, Range::Three);
        QVERIFY2(second.applied, "with the range split off these are two lates in ONE bucket");
        QCOMPARE(second.key, QStringLiteral("Left Fade/normal"));
        QCOMPARE(trim.snapshot().keys(), QStringList{QStringLiteral("Left Fade/normal")});
        // ...and the restore folds a ranged file onto the tempo bucket rather than dropping it.
        QMap<QString, double> persisted;
        persisted.insert(QStringLiteral("Left Fade/normal/mid"), 4.0);
        persisted.insert(QStringLiteral("Left Fade/normal/three"), 10.0);
        BannerLeadTrim restarted;
        restarted.restoreDecayed(persisted, 15.0, true, false);
        QCOMPARE(restarted.snapshot().keys(), QStringList{QStringLiteral("Left Fade/normal")});
        QCOMPARE(restarted.trimMsForType(type), 5.0);   // the larger magnitude wins
    }

    void theRangeWordsAreTheOnlyLegalSuffix()
    {
        using Range = BannerLeadTrim::Range;
        QVERIFY(BannerLeadTrim::rangeFromName(QStringLiteral("  MID ")) == Range::Mid);
        QVERIFY(BannerLeadTrim::rangeFromName(QStringLiteral("Three")) == Range::Three);
        QVERIFY(BannerLeadTrim::rangeFromName(QStringLiteral("unknown")) == Range::Unknown);
        QVERIFY(BannerLeadTrim::rangeFromName(QStringLiteral("corner")) == Range::Unknown);
        QVERIFY(BannerLeadTrim::isRangeName(QStringLiteral("mid")));
        QVERIFY(BannerLeadTrim::isRangeName(QStringLiteral("three")));
        QVERIFY(!BannerLeadTrim::isRangeName(QStringLiteral("unknown")));
        QCOMPARE(BannerLeadTrim::rangeName(Range::Mid), QStringLiteral("mid"));
        QCOMPARE(BannerLeadTrim::rangeName(Range::Three), QStringLiteral("three"));
        QCOMPARE(BannerLeadTrim::rangeName(Range::Unknown), QStringLiteral("unknown"));
    }

    // =====================================================================================
    // [ORION_BANNER_TRIM_BIAS 2026-09-19 owner] THE 19 % THE STREAK RULE CANNOT SEE.
    //
    // MEASURED, the two 2026-09-18 sessions (281 graded releases). OPEN / WIDE OPEN
    // standstills: 76 EXCELLENT / 18 LATE / 2 EARLY -- 79 % green, and NINE lates for every
    // early. The trim stepped 0 to 2 times in a whole session, because a step needs two
    // CONSECUTIVE agreeing verdicts and at a 79 % green rate consecutive lates are rare.
    //
    // Everything above stays: these cases pin that the NET-VOTE integrator adds the missing
    // reading WITHOUT touching the streak rule, the alternation guard, the hold or the clamp.
    // =====================================================================================

    // THE HEADLINE. Three lates and no earlies inside twelve graded shots, with greens between
    // every pair, is a bias. The streak rule refuses every one of them as `evidence` --
    // correctly, on its own terms -- and the loop used to end the session at 0.
    void aNonConsecutiveLateBiasStepsWhereTheStreakRuleCannot()
    {
        const QString type = QStringLiteral("Standstill");
        QStringList stream;
        for (int i = 1; i <= 12; ++i) {
            stream << ((i % 4 == 0) ? QStringLiteral("LATE") : QStringLiteral("EXCELLENT"));
        }

        // 1) The streak-only loop, i.e. this build with the integrator disarmed. TWELVE graded
        //    shots carrying a 3:0 late bias move the lead by exactly nothing.
        BannerLeadTrim streakOnly;
        BannerLeadTrimLimits off;
        off.biasVotes = 0;                       // banner_trim_bias_votes = 0, the kill switch
        for (const QString& verdict : stream) {
            QVERIFY2(!streakOnly.observe(type, verdict, off).applied,
                     "the streak rule must not move on non-consecutive verdicts");
        }
        QCOMPARE(streakOnly.trimMsForType(type), 0.0);

        // 2) The same twelve verdicts with the integrator armed: one step, on the third late,
        //    and NOT a step earlier -- two lates in twelve is not a margin of three.
        BannerLeadTrim trim;
        // [2026-09-19] PIN THE SHIPPED DEFAULT: the integrator ships DISABLED because the
        // forensics refuted its premise (the lead is already within ~5 ms of optimum; the
        // LATE/EARLY imbalance comes from a pickup stall and late-drawn meters, which no lead
        // can fix). Re-arming it is an owner decision backed by a measurement.
        QCOMPARE(BannerLeadTrimLimits{}.biasVotes, 0);
        BannerLeadTrimLimits limits;             // step 3, max 15, window 12
        limits.biasVotes = 3;                    // armed HERE, for this test only
        QCOMPARE(limits.biasWindow, 12);
        BannerLeadTrim::Application last;
        for (int i = 0; i < stream.size(); ++i) {
            last = trim.observe(type, stream.at(i), limits);
            if (i < stream.size() - 1) {
                QVERIFY2(!last.applied, "the margin was not reached yet");
            }
        }
        QVERIFY(last.applied);
        QCOMPARE(last.reason, QStringLiteral("bias_votes"));
        QCOMPARE(last.beforeMs, 0.0);
        QCOMPARE(last.afterMs, 3.0);             // LATE -> fire EARLIER, the same sign as a step
        QCOMPARE(last.biasLate, 3);
        QCOMPARE(last.biasEarly, 0);
        QCOMPARE(last.biasDepth, 12);
        QCOMPARE(trim.trimMsForType(type), 3.0);
    }

    // The same rule in the other direction, and the sign is the one that matters: EARLY lowers
    // the effective lead.
    void theBiasIntegratorIsSymmetricInTheEarlyDirection()
    {
        BannerLeadTrim trim;
        // [2026-09-19] The integrator SHIPS DISABLED (banner_trim_bias_votes = 0); this test owns
        // its BEHAVIOUR, so it arms it explicitly rather than leaning on the default.
        BannerLeadTrimLimits limits;
        limits.biasVotes = 3;
        const QString type = QStringLiteral("Right Fade");
        for (int i = 1; i <= 11; ++i) {
            const bool early = (i % 4 == 0);     // 4 and 8: two earlies, never consecutive
            QVERIFY(!trim.observe(type, early ? QStringLiteral("EARLY")
                                              : QStringLiteral("EXCELLENT"), limits).applied);
        }
        const auto third = trim.observe(type, QStringLiteral("VERY EARLY"), limits);
        QVERIFY(third.applied);
        QCOMPARE(third.reason, QStringLiteral("bias_votes"));
        QCOMPARE(third.biasEarly, 3);
        QCOMPARE(third.biasLate, 0);
        QCOMPARE(trim.trimMsForType(type), -3.0);
    }

    // WHY THE INTEGRATOR NEEDS NO ALTERNATION GUARD. The 2026-09-16 pathology was a coin flip
    // satisfying a 2-of-4 COUNT; a NET vote is immune to it by construction -- E,L,E,L nets 0
    // or +-1 and can never reach three, however long it runs.
    void anAlternationCanNeverReachTheBiasMargin()
    {
        BannerLeadTrim trim;
        const BannerLeadTrimLimits limits;
        const QString type = QStringLiteral("Standstill");
        for (int i = 0; i < 40; ++i) {
            const auto out = trim.observe(type, (i % 2 == 0) ? QStringLiteral("EARLY")
                                                             : QStringLiteral("LATE"), limits);
            QVERIFY2(!out.applied, "an alternation is a coin flip, not a direction");
            QVERIFY(out.reason == QLatin1String("alternating")
                    || out.reason == QLatin1String("evidence"));
        }
        QVERIFY(trim.isZero());
    }

    // The window is EVIDENCE FOR A MOVE, and the move spends it. Otherwise the twelfth verdict
    // would step, the thirteenth would step again on the same three lates, and the integrator
    // would become the per-verdict stepper it exists not to be.
    void theBiasWindowIsSpentByTheStepItBuys()
    {
        BannerLeadTrim trim;
        // [2026-09-19] The integrator SHIPS DISABLED (banner_trim_bias_votes = 0); this test owns
        // its BEHAVIOUR, so it arms it explicitly rather than leaning on the default.
        BannerLeadTrimLimits limits;
        limits.biasVotes = 3;
        const QString type = QStringLiteral("Standstill");
        for (int i = 1; i <= 12; ++i) {
            trim.observe(type, (i % 4 == 0) ? QStringLiteral("LATE")
                                            : QStringLiteral("EXCELLENT"), limits);
        }
        QCOMPARE(trim.trimMsForType(type), 3.0);
        // Two more non-consecutive lates -- five in the last twenty shots -- move nothing: the
        // window behind the step is gone and the next one starts from empty.
        for (int i = 13; i <= 20; ++i) {
            const auto out = trim.observe(type, (i % 4 == 0) ? QStringLiteral("LATE")
                                                             : QStringLiteral("EXCELLENT"),
                                          limits);
            QVERIFY2(!out.applied, "the spent window must not re-buy the same step");
        }
        QCOMPARE(trim.trimMsForType(type), 3.0);
    }

    // ...and a STREAK step spends it too, so the two rules can never compound: without this,
    // two consecutive lates would step here and the vote would step again three shots later on
    // evidence that included them.
    void aStreakStepAlsoSpendsTheBiasWindow()
    {
        BannerLeadTrim trim;
        // [2026-09-19] The integrator SHIPS DISABLED (banner_trim_bias_votes = 0); this test owns
        // its BEHAVIOUR, so it arms it explicitly rather than leaning on the default.
        BannerLeadTrimLimits limits;
        limits.biasVotes = 3;
        const QString type = QStringLiteral("Standstill");
        trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY(trim.observe(type, QStringLiteral("LATE"), limits).applied);   // reason=step
        QCOMPARE(trim.trimMsForType(type), 3.0);
        // Three greens and ONE more late: with the window spent this is a margin of one, so
        // nothing moves. (With the window standing it would have been three, and the trim would
        // have taken a second step off evidence the first step had already used.)
        const QStringList firstRun{QStringLiteral("EXCELLENT"), QStringLiteral("EXCELLENT"),
                                   QStringLiteral("EXCELLENT"), QStringLiteral("LATE")};
        for (const QString& verdict : firstRun) {
            QVERIFY(!trim.observe(type, verdict, limits).applied);
        }
        QCOMPARE(trim.trimMsForType(type), 3.0);
        // Two further lates, still never consecutive, complete a fresh margin of three.
        const QStringList secondRun{QStringLiteral("EXCELLENT"), QStringLiteral("EXCELLENT"),
                                    QStringLiteral("EXCELLENT"), QStringLiteral("LATE"),
                                    QStringLiteral("EXCELLENT"), QStringLiteral("EXCELLENT"),
                                    QStringLiteral("EXCELLENT")};
        for (const QString& verdict : secondRun) {
            QVERIFY(!trim.observe(type, verdict, limits).applied);
        }
        const auto stepped = trim.observe(type, QStringLiteral("LATE"), limits);
        QVERIFY(stepped.applied);
        QCOMPARE(stepped.reason, QStringLiteral("bias_votes"));
        QCOMPARE(trim.trimMsForType(type), 6.0);
    }

    // [ORION_BANNER_TRIM_HOLD 2026-09-16] IS NOT WEAKENED. The window is read only when the
    // verdict in hand is a LATE or an EARLY, so an EXCELLENT can never move the trim -- not even
    // when its own arrival, by evicting the oldest entry, is what completes the margin. The vote
    // is not lost either: the next directional verdict casts it.
    void anExcellentCannotSpendTheBiasVote()
    {
        BannerLeadTrim trim;
        BannerLeadTrimLimits tight;
        tight.biasWindow = 4;
        tight.biasVotes = 2;
        const QString type = QStringLiteral("Standstill");
        // E,L,X,L leaves the window at [E,L,X,L] -- a margin of one.
        const QStringList lead{QStringLiteral("EARLY"), QStringLiteral("LATE"),
                               QStringLiteral("EXCELLENT"), QStringLiteral("LATE")};
        for (const QString& verdict : lead) {
            QVERIFY(!trim.observe(type, verdict, tight).applied);
        }
        // This EXCELLENT evicts the EARLY, which takes the margin to two -- and it must still
        // move nothing, because a trim producing EXCELLENT is the correct trim.
        const auto green = trim.observe(type, QStringLiteral("EXCELLENT"), tight);
        QVERIFY2(!green.applied, "an EXCELLENT must never step the trim");
        QCOMPARE(green.biasLate, 2);
        QCOMPARE(green.biasEarly, 0);
        QCOMPARE(trim.trimMsForType(type), 0.0);
        // The evidence is still there; the next LATE is what spends it.
        const auto late = trim.observe(type, QStringLiteral("LATE"), tight);
        QVERIFY(late.applied);
        QCOMPARE(late.reason, QStringLiteral("bias_votes"));
        QCOMPARE(trim.trimMsForType(type), 3.0);
    }

    // The clamp is the trim's whole safety property and the integrator is INSIDE it, not beside
    // it: at the ceiling a fired vote reports `clamped` and writes nothing.
    void theBiasIntegratorObeysTheClamp()
    {
        BannerLeadTrim trim;
        // [2026-09-19] The integrator SHIPS DISABLED (banner_trim_bias_votes = 0); this test owns
        // its BEHAVIOUR, so it arms it explicitly rather than leaning on the default.
        BannerLeadTrimLimits limits;
        limits.biasVotes = 3;
        const QString type = QStringLiteral("Standstill");
        for (int i = 1; i <= 200; ++i) {
            trim.observe(type, (i % 4 == 0) ? QStringLiteral("LATE")
                                            : QStringLiteral("EXCELLENT"), limits);
        }
        QCOMPARE(trim.trimMsForType(type), 15.0);
        // Drive one more full margin of three and watch it refuse.
        BannerLeadTrim::Application clamped;
        for (int i = 201; i <= 216; ++i) {
            const auto out = trim.observe(type, (i % 4 == 0) ? QStringLiteral("LATE")
                                                             : QStringLiteral("EXCELLENT"),
                                          limits);
            if (out.reason == QLatin1String("clamped")) {
                clamped = out;
            }
            QVERIFY2(!out.applied, "nothing may move past the clamp");
        }
        QCOMPARE(clamped.reason, QStringLiteral("clamped"));
        QCOMPARE(trim.trimMsForType(type), 15.0);
    }

    // banner_trim_bias_votes = 0 is the documented kill switch: the 2026-09-17 loop, exactly.
    // A non-positive limit degrades to "no integrator", never to a fallback that would move a
    // lead the owner asked to leave alone.
    void zeroBiasVotesRestoresTheStreakOnlyLoop()
    {
        const QString type = QStringLiteral("Standstill");
        for (const int votes : {0, -7}) {
            BannerLeadTrim trim;
            BannerLeadTrimLimits off;
            off.biasVotes = votes;
            for (int i = 1; i <= 60; ++i) {
                QVERIFY(!trim.observe(type, (i % 4 == 0) ? QStringLiteral("LATE")
                                                         : QStringLiteral("EXCELLENT"),
                                      off).applied);
            }
            QVERIFY2(trim.isZero(), "a disarmed integrator must leave the lead alone");
        }
        // A zero WINDOW is the same request said the other way round.
        BannerLeadTrim noWindow;
        BannerLeadTrimLimits none;
        none.biasWindow = 0;
        for (int i = 1; i <= 60; ++i) {
            QVERIFY(!noWindow.observe(type, (i % 4 == 0) ? QStringLiteral("LATE")
                                                         : QStringLiteral("EXCELLENT"),
                                      none).applied);
        }
        QVERIFY(noWindow.isZero());
    }

    // The window is per BUCKET and it is SESSION evidence, exactly like the hysteresis window:
    // a slider move clears it, so a bias earned under the old value cannot spend itself on the
    // new one, and a fade's lates cannot vote on a standstill.
    void theBiasWindowIsPerBucketAndDiesWithTheSlider()
    {
        BannerLeadTrim trim;
        // [2026-09-19] The integrator SHIPS DISABLED (banner_trim_bias_votes = 0); this test owns
        // its BEHAVIOUR, so it arms it explicitly rather than leaning on the default.
        BannerLeadTrimLimits limits;
        limits.biasVotes = 3;
        for (int i = 1; i <= 8; ++i) {
            trim.observe(QStringLiteral("Standstill"),
                         (i % 4 == 0) ? QStringLiteral("LATE") : QStringLiteral("EXCELLENT"),
                         limits);
        }
        // Two lates stand in the Standstill window, and a Left Fade late does NOT complete them.
        QVERIFY(!trim.observe(QStringLiteral("Left Fade"), QStringLiteral("LATE"),
                              limits).applied);
        QVERIFY(trim.isZero());
        // The owner moves their slider: every window goes with the value it was measured on.
        QVERIFY(trim.reset());
        for (int i = 1; i <= 11; ++i) {
            QVERIFY(!trim.observe(QStringLiteral("Standstill"),
                                  (i % 4 == 0) ? QStringLiteral("LATE")
                                               : QStringLiteral("EXCELLENT"), limits).applied);
        }
        QVERIFY2(!trim.observe(QStringLiteral("Standstill"), QStringLiteral("EXCELLENT"),
                               limits).applied,
                 "the pre-reset lates must not count toward the new value's margin");
        QVERIFY(trim.isZero());
    }
};

QTEST_GUILESS_MAIN(ShotVerdictTallyTests)
#include "ShotVerdictTallyTests.moc"
