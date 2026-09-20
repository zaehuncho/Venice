#pragma once

#include <QtCore/QChar>
#include <QtCore/QMap>
#include <QtCore/QString>
#include <QtCore/QStringList>

#include <algorithm>
#include <cmath>
#include <deque>
#include <vector>

// [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] The bounded closed loop from the game's own TIMING
// banner back onto the Shot Lead.
//
// WHY THIS EXISTS. The banner is the ONLY validated timing instrument on this project
// (tools/timing/panel_grade.py lineage; the live twin is banner_verdict_live.py). On
// 2026-09-15 the same lead of 274 graded three different ways inside one hour:
//
//   rapid-fire drill, lead 274 : 7 LATE / 2 EARLY / 7 EXCELLENT
//   5v5 game an hour earlier, lead 274 : 9 EXCELLENT / 1 EARLY / 2 LATE
//   5v5 game, lead 286 : 8 EARLY / 0 LATE
//
// The LATE and the EXCELLENT shots in the drill carried IDENTICAL holds and freeze fills
// (641-654 ms; freeze peak 90.7 vs 90.8), i.e. the landing sits on the top edge of a ~1-frame
// window and flips with the frame phase. The right lead is therefore CONTEXT-dependent by about
// +-10 ms, and nothing but the banner can say which context the owner is in right now.
//
// WHAT THIS IS NOT. It is not a learner of the lead: the owner's slider value is never written,
// never moved and never re-interpreted. This is a small, clamped, decaying ADDITIVE term that
// rides on top of it and is displayed next to it, exactly as the session lead probe's trim does.
//
// THE RULES, as the owner stated them:
//   LATE       -> trim += step  (fire EARLIER; effective lead = user lead + trim)
//   EARLY      -> trim -= step
//   EXCELLENT  -> HOLD. See [ORION_BANNER_TRIM_HOLD] below.
//   anything else (a template word this build does not classify) -> evidence, but no move
//   per shot-type bucket, clamped to +-maxMs
//   HYSTERESIS: a change is applied only once >= kRequiredSameDirection verdicts of that
//   direction sit in the last kRecentWindow for that bucket, so ONE misread can never move it.
//
// The hysteresis gate covers the decay too. A single stray EXCELLENT is exactly as likely to be
// a misread as a single stray LATE, and "one verdict can never move the lead" is the whole
// promise; a rule with an exception in it is not that promise.
//
// == [ORION_BANNER_TRIM_HOLD 2026-09-16 owner] AN EXCELLENT HOLDS; IT NO LONGER DECAYS ========
//
// Until today an EXCELLENT (and the oracle's GREEN) walked the trim 1.0 ms back toward the
// slider. LIVE EVIDENCE, 2026-09-16 20:43-20:45, 22 shots, Standstill bucket:
//
//   lates pushed the trim to +9 (effective 278) -> EXCELLENT x2 -> 278 -> 277 -> ...
//   after a slider move the trim settled at -3 (271) -> EXCELLENT x7 -> back to 274 -> EARLY x2
//
// A trim value that is PRODUCING EXCELLENT is, by definition, the correct value. Decaying it
// toward the slider re-introduces exactly the error the loop had just removed, and the second
// run shows the whole round trip: seven greens spent walking the good value away, then the
// verdicts it was supposed to prevent. So EXCELLENT/GREEN now MOVES NOTHING.
//
// WHY THERE IS STILL A DECAY AT ALL. A trim held forever would let a value that is merely
// TOLERATED (the landing sits inside the window but not on the tip, so the banner says EXCELLENT
// either way) live for a whole session with nothing able to retire it. So after
// `holdShots` CONSECUTIVE EXCELLENT/GREEN verdicts in a bucket -- a dozen at this owner's
// cadence -- each FURTHER one nibbles `decayMs` toward zero. A trim that is right stays for a
// dozen shots; a trim that is merely tolerated bleeds off slowly. Any LATE, EARLY or oracle MISS
// resets the counter: the run that would have retired the value is over.
//
// The two coarser relaxations are untouched, and they are the ones that matter across contexts:
// restoreDecayed() still halves the trim at app start (a drill is not a 5v5 game) and reset()
// still clears it outright the moment the owner moves their own slider.
// =============================================================================================
//
// == [ORION_BANNER_TRIM_ALTERNATION 2026-09-16 owner] A COIN FLIP IS NOT A DIRECTION ===========
//
// LIVE EVIDENCE, 2026-09-16 21:00-21:01, 21 presses. After ten EXCELLENT in a row at slider 281
// the owner's shot TEMPO changed and the verdicts began to alternate. The 2-of-4 hysteresis
// above is SATISFIED by an alternation -- `recent=E,L,E,L` holds two L's and two E's, so every
// single verdict passed the gate -- and the trim stepped on every one of them: +3, then -3, then
// +3, then -3. The loop chased the coin flip at full step size, which is the one thing a
// hysteresis gate exists to prevent.
//
// So a STEP (a LATE or an EARLY on the banner path, a MISS descent on the oracle path) now also
// requires the IMMEDIATELY PRECEDING verdict in the bucket to carry the SAME code: two
// CONSECUTIVE verdicts of one direction, not any two of the last four. An alternation therefore
// moves nothing, and the refusal is named `alternating` on the log line so a session can be read
// for "the loop saw a coin flip here" rather than inferred from an absent step.
//
// The 4-deep window is UNCHANGED and still does its two other jobs: it is what `recent=` prints,
// and its dilution by unclassified words is what keeps a stale verdict from pairing with a fresh
// one. `evidence` still names the ordinary "one verdict is not a direction" refusal -- the first
// verdict in a bucket, one that follows an unclassified word, and one that follows an EXCELLENT.
// `alternating` is reserved for the specific pathology: a LATE straight after an EARLY, or an
// EARLY straight after a LATE (on the oracle path, a MISS straight after a GREEN).
// =============================================================================================
//
// == [ORION_BANNER_TRIM_TEMPO 2026-09-16 owner] ONE TYPE IS NOT ONE ANIMATION ==================
//
// The SAME 21-press session also shows why the alternation happened at all. Across those shots
// the meter's ONSET -- the engine's first accepted vision sample, measured from the physical
// Square edge -- moved with the owner's shot tempo, and the game's window moved with it:
//
//   quick presses  onset 467-480 ms : natural holds 583-602 ms, window ~608 +- 5 ms
//   normal presses onset 500-578 ms : natural holds 627-693 ms, window ~650 ms
//   slow/ghost     onset 584-652 ms : natural holds 694-746 ms
//
// 602 graded EARLY and 613/616 graded LATE inside one four-shot run. Those are not one
// distribution with noise on it; they are DIFFERENT ANIMATION CLASSES with different windows,
// and one trim cannot serve both -- pushed toward the quick class it is wrong for the normal
// one, and the alternation above is exactly what that looks like from inside the loop.
//
// So the trim is keyed by (shot type, TEMPO), tempo in {quick, normal, slow}, derived from the
// onset. OFFLINE VALIDATION on the four logged sessions (cur_session11/12/13/14, 106 banner
// verdicts time-joined to their releases), scoring each candidate by the number of (EARLY, LATE)
// pairs in which the EARLY shot was actually held LONGER than the LATE one -- the signature of a
// bimodal bucket, and zero for a bucket with one window:
//
//   Standstill, one bucket (today)       63 inversions / 200 pairs  = 31.5 %
//   Standstill, quick<500 slow>580       16 inversions /  88 pairs  = 18.2 %
//   Fades, one bucket (today)             6 inversions /  15 pairs  = 40.0 %
//   Fades, quick<775 slow>915             2 inversions /   8 pairs  = 25.0 %
//
// THE CUT POINTS ARE IN THE ENGINE'S OWN FRAME, which is why they are not the owner's numbers
// verbatim. The owner read his thresholds off the sidecar's `first_sight_ms_after_press`, which
// is stamped on the CAPTURE clock; the engine's onset is stamped when the sample is accepted,
// a measured 36 ms later (n=77, p25 31 / p75 44). 465 + 36 = 501 and 540 + 36 = 576, so the
// cuts below ARE his 465/540 and 740/880, carried into the frame the engine can actually
// measure in. On the paired subset the two frames separate equally well (1-2 inversions of 18),
// so the engine-side onset costs nothing and needs no extra plumbing.
//
// PER (TYPE, TEMPO) PERSISTENCE uses composite keys -- "Standstill/quick" -- and a legacy
// un-suffixed key loads into that type's `normal` sub-bucket, which is where the overwhelming
// majority of the evidence behind it came from. `banner_trim_tempo_buckets` (env
// ORION_BANNER_TRIM_TEMPO=0) collapses every key back to the bare type bucket, which is
// byte-identical to the 2026-09-16 build including its persistence rules.
// =============================================================================================
//
// == [ORION_BANNER_TRIM_BIAS 2026-09-19 owner] A STREAK RULE CANNOT SEE A 19 % BIAS ==========
//
// THE MEASUREMENT, the two 2026-09-18 sessions (logs/orion_native.log.1 + orion_native.log,
// 281 graded releases). Restricted to OPEN / WIDE OPEN standstills:
//
//   76 EXCELLENT / 18 LATE / 2 EARLY   = 79 % green, and 18 late against 2 early
//
// That is a PERSISTENT ONE-SIDED BIAS -- nine lates for every early -- and the loop above
// stepped 0 to 2 times in a whole session. The reason is structural, not a tuning miss: the
// [ORION_BANNER_TRIM_ALTERNATION] rule needs two CONSECUTIVE verdicts of one direction, and at
// a 79 % green rate consecutive lates are RARE. A late lands, an EXCELLENT lands, the next late
// arrives four shots later with a green in between, and every one of them is refused as
// `evidence`. The instrument is telling the loop, all session long, that the lead is ~one step
// short, and the loop cannot hear it.
//
// THE ADDITION IS AN INTEGRATOR, NOT A RELAXATION. Nothing above changes: the streak rule, its
// alternation guard, the hold, the idle decay, the clamp and the slider reset are untouched and
// still own every consecutive-verdict step. ALONGSIDE them, each bucket now keeps a sliding
// window of its last `biasWindow` CALIBRATING verdicts (EXCELLENT, LATE and EARLY -- the
// greens are IN the window, which is exactly what makes it a rate and not a counter), and when
//
//   #LATE - #EARLY >= biasVotes      -> one +stepMs step, and the window is CLEARED
//   #EARLY - #LATE >= biasVotes      -> one -stepMs step, and the window is CLEARED
//
// WHY A NET VOTE IS SAFE WHERE A SINGLE VERDICT IS NOT. The alternation guard exists because a
// coin flip (E,L,E,L) satisfied a 2-of-4 count; a NET vote is immune to that by construction --
// an alternation nets 0 or +-1 and can never reach 3 -- so the integrator deliberately ignores
// the guard. Three more lates than earlies inside twelve graded shots is not a coin flip and it
// is not one misread: it is the shape the 09-18 sessions actually have.
//
// WHAT IT CANNOT DO. It cannot outrun the clamp (+-maxMs, the same ceiling), it cannot fire on
// an EXCELLENT (the window is read only when the verdict in hand is a LATE or an EARLY, so
// [ORION_BANNER_TRIM_HOLD]'s promise that a trim producing EXCELLENT never moves is intact),
// and it cannot compound with the streak rule: a step from EITHER rule clears the bucket's
// window, so the evidence behind a move is spent by the move. `banner_trim_bias_votes = 0` is
// the kill switch and restores the 2026-09-17 behaviour byte-for-byte.
// =============================================================================================
//
// PURE POLICY. No Qt object, no engine state, no I/O: it is pinned directly in
// ShotVerdictTallyTests.cpp, and AutomationEngine owns exactly one instance of it.
namespace orion {

// The step/ceiling/decay triple the engine hands in on every call, so a settings change takes
// effect on the next verdict without this class holding a copy of the config.
struct BannerLeadTrimLimits {
    double stepMs = 3.0;    // banner_trim_step_ms
    double maxMs = 15.0;    // banner_trim_max_ms
    double decayMs = 1.0;   // the IDLE relaxation, deliberately smaller than the step
    // [ORION_BANNER_TRIM_HOLD 2026-09-16 owner] banner_trim_hold_shots: how many CONSECUTIVE
    // EXCELLENT/GREEN verdicts a bucket must show before the idle decay starts. Below it an
    // EXCELLENT HOLDS -- a value that is producing EXCELLENT is the correct value.
    int holdShots = 12;
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16 owner] banner_trim_tempo_buckets: key the trim by
    // (shot type, tempo) rather than by shot type alone. False collapses every key back to the
    // bare type bucket and is byte-identical to the 2026-09-16 build, persistence included.
    bool tempoBuckets = true;
    // [ORION_BANNER_TRIM_RANGE 2026-09-17 owner] banner_trim_range_buckets: give FADES a third
    // key dimension, the shot's RANGE (three | mid). False collapses every key back to
    // (type, tempo) and is byte-identical to the 2026-09-16 build.
    bool rangeBuckets = true;
    // [ORION_BANNER_TRIM_BIAS 2026-09-19 owner] banner_trim_bias_window: how many CALIBRATING
    // verdicts (EXCELLENT / LATE / EARLY) one bucket's bias window holds. A dozen is about four
    // possessions at this owner's cadence -- long enough that a 19 % late rate shows up in it,
    // short enough that yesterday's lead cannot vote on today's.
    int biasWindow = 12;
    // banner_trim_bias_votes: the NET margin (#LATE - #EARLY, or the reverse) that buys one
    // step. 3 of 12 is the smallest margin an alternation can never reach and a one-sided 19 %
    // stream does. 0 DISABLES the integrator and restores the 2026-09-17 loop byte-for-byte,
    // and 0 is what SHIPS (2026-09-19): the premise was refuted -- see AppConfigData::
    // bannerTrimBiasVotes for the kernel fit that put the lead within ~5 ms of optimum.
    int biasVotes = 0;
};

class BannerLeadTrim final {
public:
    // "at least 2 same-direction verdicts in the last 4". Four is one short run at this
    // owner's cadence and two is the smallest sample that is not a single reading.
    static constexpr int kRecentWindow = 4;
    static constexpr int kRequiredSameDirection = 2;
    // Contexts change between sessions (a drill is not a 5v5 game), so a trim learned last
    // night is evidence, not a setting: it comes back at half strength and re-earns the rest.
    static constexpr double kStartDecayFactor = 0.5;
    // The schema guard for learning.json. Independent of banner_trim_max_ms so lowering the
    // setting cannot make a previously legitimate persisted value unreadable -- the engine
    // clamps to the live max after loading.
    static constexpr double kPersistCeilingMs = 40.0;
    // Below this a trim is noise on a 16 ms window; snap it to exactly 0 so the UI caption
    // disappears instead of showing "+0 ms" forever after a decay.
    static constexpr double kZeroEpsilonMs = 0.05;
    // [ORION_BANNER_TRIM_HOLD 2026-09-16] The fallback when a caller hands in a hold length no
    // setting could have produced (0, negative). The setting's own band is 4..60 and lives in
    // AppConfigData; this is only the policy's own floor so the hold can never degrade to the
    // decay-on-every-EXCELLENT behaviour the 09-16 evidence retired.
    static constexpr int kHoldShotsFallback = 12;

    // ---------------------------------------------------------------- classification
    // The shot-type buckets this trim keys on. AutomationEngine::classifyShotType emits
    // "Standstill", "Left Fade", "Right Fade" and (on the Tempo paths) other words; every one
    // of those others shares a single bucket rather than fragmenting the evidence.
    [[nodiscard]] static QString bucketFor(const QString& shotType)
    {
        const QString type = shotType.trimmed();
        if (type == QLatin1String("Left Fade") || type == QLatin1String("Right Fade")) {
            return type;
        }
        if (type.isEmpty() || type == QLatin1String("Standstill")) {
            // ShotContext::shotType defaults to "Standstill" and is never empty on a release;
            // an empty string here can only be a caller with no shot, and the reference bucket
            // is the honest place for it (it is also the bucket the card displays).
            return QStringLiteral("Standstill");
        }
        return QStringLiteral("Other");
    }

    // ---------------------------------------------------------------- tempo
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16] The animation CLASS inside a shot type, read off the
    // meter's onset. See the header comment for the 2026-09-16 evidence and for the offline
    // validation of the cut points.
    enum class Tempo { Quick = 0, Normal = 1, Slow = 2 };

    // The cut points, in ms of ONSET (the engine's first accepted vision sample for the shot,
    // measured from the physical Square edge). They are the owner's own sidecar-frame numbers
    // (Standstill 465/540, fades 740/880) carried into the engine's frame by the measured 36 ms
    // accept lag -- see the header comment. A shot with no onset (the meter never appeared, a
    // blind release, a caller that does not carry one) is NORMAL: the reference class, and the
    // one every legacy persisted key lands in.
    static constexpr double kStandstillQuickOnsetMs = 500.0;
    static constexpr double kStandstillSlowOnsetMs = 580.0;
    static constexpr double kFadeQuickOnsetMs = 775.0;
    static constexpr double kFadeSlowOnsetMs = 915.0;

    [[nodiscard]] static QString tempoName(Tempo tempo)
    {
        switch (tempo) {
        case Tempo::Quick:
            return QStringLiteral("quick");
        case Tempo::Slow:
            return QStringLiteral("slow");
        case Tempo::Normal:
            break;
        }
        return QStringLiteral("normal");
    }

    // Anything this build does not name is NORMAL rather than rejected: a persisted key from a
    // future build must degrade to the reference class, never to a class of its own.
    [[nodiscard]] static Tempo tempoFromName(const QString& name)
    {
        const QString word = name.trimmed().toLower();
        if (word == QLatin1String("quick")) {
            return Tempo::Quick;
        }
        if (word == QLatin1String("slow")) {
            return Tempo::Slow;
        }
        return Tempo::Normal;
    }

    // Only the three words this build writes are a legal SUFFIX. A key ending in anything else
    // is not ours to install (the same rule bucketFor's round-trip check applies to the type).
    [[nodiscard]] static bool isTempoName(const QString& name)
    {
        const QString word = name.trimmed().toLower();
        return word == QLatin1String("quick") || word == QLatin1String("normal")
            || word == QLatin1String("slow");
    }

    [[nodiscard]] static Tempo tempoFor(const QString& shotType, double onsetMs)
    {
        if (!std::isfinite(onsetMs) || onsetMs < 0.0) {
            return Tempo::Normal;   // no onset is not a tempo
        }
        const QString bucket = bucketFor(shotType);
        if (bucket == QLatin1String("Left Fade") || bucket == QLatin1String("Right Fade")) {
            return onsetMs < kFadeQuickOnsetMs ? Tempo::Quick
                 : (onsetMs > kFadeSlowOnsetMs ? Tempo::Slow : Tempo::Normal);
        }
        if (bucket == QLatin1String("Standstill")) {
            return onsetMs < kStandstillQuickOnsetMs ? Tempo::Quick
                 : (onsetMs > kStandstillSlowOnsetMs ? Tempo::Slow : Tempo::Normal);
        }
        // "Other" is Go-To and the Tempo words: their meters surface seconds into the hold, so an
        // onset there measures the player walking into position, not the animation. One bucket,
        // exactly as today, rather than three sub-buckets split on a quantity that means nothing.
        return Tempo::Normal;
    }

    // ---------------------------------------------------------------- range
    // == [ORION_BANNER_TRIM_RANGE 2026-09-17 owner] ONE FADE IS NOT ONE SHOT =================
    //
    // THE ASK, verbatim: "middy fades should perform just as standstill shots (slightly bigger
    // window); I'm fine with 50-60 % for three-point fades." The 2026-09-16 night graded 13
    // EXCELLENT / 8 LATE / 3 EARLY on fades sharing ONE bucket per tempo, and several of the
    // earlies "filled right before the green window" -- the signature of two windows inside one
    // bucket, exactly as the tempo split was. A mid-range fade is a shorter animation with a
    // wider window than a three-point fade, so it needs its own trim and its own offset.
    //
    // RANGE IS A FADE-ONLY DIMENSION. Standstill keeps "Standstill/normal" untouched: the owner's
    // standstills are already where he wants them, and splitting a bucket that is producing
    // EXCELLENT halves its evidence for nothing. "Other" keeps one bucket for the same reason it
    // keeps one tempo.
    //
    // UNKNOWN IS NOT A THIRD BUCKET. The sidecar reports `unknown` whenever it could not read the
    // nameplate "3" cell (no plate, a stale plate, an unreadable cell -- see shot_range.py), and
    // on today's court that is most presses. An unknown-range fade therefore files in the SAME
    // "Left Fade/normal" key the 2026-09-16 build used, so an install where the signal never
    // arrives is byte-identical to today, evidence included.
    // =========================================================================================
    enum class Range { Unknown = 0, Mid = 1, Three = 2 };

    [[nodiscard]] static QString rangeName(Range range)
    {
        switch (range) {
        case Range::Mid:
            return QStringLiteral("mid");
        case Range::Three:
            return QStringLiteral("three");
        case Range::Unknown:
            break;
        }
        return QStringLiteral("unknown");
    }

    // Anything this build does not name is UNKNOWN rather than rejected, for the same reason
    // tempoFromName degrades to Normal: a key from a future build must never become a class of
    // its own.
    [[nodiscard]] static Range rangeFromName(const QString& name)
    {
        const QString word = name.trimmed().toLower();
        if (word == QLatin1String("mid")) {
            return Range::Mid;
        }
        if (word == QLatin1String("three")) {
            return Range::Three;
        }
        return Range::Unknown;
    }

    // Only the two words this build WRITES are a legal suffix. "unknown" is deliberately not one
    // of them: an unknown range does not get a key, it falls back to the (type, tempo) key.
    [[nodiscard]] static bool isRangeName(const QString& name)
    {
        const QString word = name.trimmed().toLower();
        return word == QLatin1String("mid") || word == QLatin1String("three");
    }

    [[nodiscard]] static bool isFadeBucket(const QString& bucket)
    {
        return bucket == QLatin1String("Left Fade") || bucket == QLatin1String("Right Fade");
    }

    // The range this shot is FILED under: a named range on a fade, Unknown everywhere else.
    [[nodiscard]] static Range rangeFor(const QString& shotType, Range range)
    {
        return isFadeBucket(bucketFor(shotType)) ? range : Range::Unknown;
    }

    // The map key BOTH instruments, the persistence and every read use. With tempoBuckets false
    // it is the bare type bucket, byte-for-byte the 2026-09-16 key.
    // [ORION_BANNER_TRIM_RANGE 2026-09-17] ...and with a NAMED range on a FADE it gains a third
    // segment -- "Left Fade/normal/mid". An unknown range, a non-fade type, or the range kill
    // switch all produce the 2026-09-16 key exactly.
    [[nodiscard]] static QString keyFor(const QString& shotType, Tempo tempo, bool tempoBuckets,
                                        Range range = Range::Unknown, bool rangeBuckets = true)
    {
        const QString bucket = bucketFor(shotType);
        if (!tempoBuckets) {
            return bucket;   // the kill switch collapses everything, range included
        }
        QString key = bucket + QLatin1Char('/') + tempoName(tempo);
        const Range filed = rangeFor(shotType, range);
        if (rangeBuckets && filed != Range::Unknown) {
            key += QLatin1Char('/') + rangeName(filed);
        }
        return key;
    }

    // One char per verdict for the `recent=` field and for the hysteresis window:
    //   L LATE   E EARLY   X EXCELLENT/PERFECT (the grader's green words)   O anything else
    // Classified on the WORD, never on the panel's cell colour -- the panel paints a coverage
    // cell green as well, so a colour rule would read an open miss as a make. Same rule as
    // ShotVerdictTally::bucketFor, kept here so this header stands alone.
    [[nodiscard]] static QChar codeFor(const QString& timing)
    {
        const QString word = timing.trimmed().toUpper();
        if (word.isEmpty()) {
            return QLatin1Char('O');
        }
        if (word == QLatin1String("EXCELLENT") || word == QLatin1String("PERFECT")) {
            return QLatin1Char('X');
        }
        if (word.contains(QLatin1String("EARLY"))) {
            return QLatin1Char('E');
        }
        if (word.contains(QLatin1String("LATE"))) {
            return QLatin1Char('L');
        }
        return QLatin1Char('O');
    }

    // What one verdict did, for the log line and for the caller's change detection.
    struct Application {
        bool applied = false;     // the trim actually moved
        QChar code = QLatin1Char('O');
        QString type;             // the BUCKET, not the raw shot type
        double beforeMs = 0.0;
        double afterMs = 0.0;
        QString recent;           // "L,L,E,X", oldest -> newest, this bucket only
        int sameDirection = 0;    // how many of `recent` share `code`
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] Consecutive EXCELLENT/GREEN verdicts in this
        // bucket INCLUDING this one (0 on anything that is not one), and what the rule did:
        //   step | hold | idle_decay | evidence | clamped
        // Named like OracleApplication::reason so the engine's two log lines share a field.
        int holdStreak = 0;
        QString reason;
        // [ORION_BANNER_TRIM_TEMPO 2026-09-16] The tempo sub-bucket this verdict was filed in,
        // the onset it was derived from (-1 when the shot carried none) and the composite map
        // key that was actually read and written ("Standstill/quick", or the bare bucket with
        // the tempo buckets switched off). `type` above stays the SHOT-TYPE bucket, because that
        // is what the per-type lead offset and the card are keyed on.
        Tempo tempo = Tempo::Normal;
        double onsetMs = -1.0;
        QString key;
        // [ORION_BANNER_TRIM_RANGE 2026-09-17] The range sub-bucket this verdict was filed in
        // (Unknown on every non-fade and on a fade whose range the sidecar could not read).
        Range range = Range::Unknown;
        // [ORION_BANNER_TRIM_BIAS 2026-09-19] The bias window AS IT STOOD when this verdict was
        // judged (this verdict included, before any clear): how many LATEs and EARLYs it held
        // and how deep it was. Printed on the `reason=bias_votes` log line so a session can be
        // read for "what did the integrator actually see", and 0/0/0 on a build or a bucket
        // where the integrator is disabled.
        int biasLate = 0;
        int biasEarly = 0;
        int biasDepth = 0;
    };

    // ---------------------------------------------------------------- the loop
    // `onsetMs` is the engine's first accepted vision sample for this shot, in ms after the
    // physical press; -1 (the default) means the caller has none and files the verdict in the
    // NORMAL sub-bucket.
    Application observe(const QString& shotType, const QString& timing,
                        const BannerLeadTrimLimits& limits, double onsetMs = -1.0,
                        Range range = Range::Unknown)
    {
        Application out;
        out.code = codeFor(timing);
        out.type = bucketFor(shotType);
        out.onsetMs = (std::isfinite(onsetMs) && onsetMs >= 0.0) ? onsetMs : -1.0;
        out.tempo = tempoFor(shotType, out.onsetMs);
        out.range = rangeFor(shotType, range);
        out.key = keyFor(shotType, out.tempo, limits.tempoBuckets, out.range,
                         limits.rangeBuckets);

        std::deque<QChar>& window = recent_[out.key];
        // [ORION_BANNER_TRIM_ALTERNATION 2026-09-16] The IMMEDIATELY preceding verdict in this
        // bucket, read before this one joins the window. Two consecutive verdicts of one
        // direction are a direction; one of each is a coin flip.
        const QChar previous = window.empty() ? QLatin1Char('\0') : window.back();
        window.push_back(out.code);
        while (window.size() > static_cast<std::size_t>(kRecentWindow)) {
            window.pop_front();
        }
        out.recent = joinWindow(recent_, out.key);
        out.sameDirection = static_cast<int>(
            std::count(window.begin(), window.end(), out.code));

        const double before = trim_.value(out.key, 0.0);
        out.beforeMs = before;
        out.afterMs = before;
        if (out.code == QLatin1Char('O')) {
            out.reason = QStringLiteral("evidence");
            return out;   // evidence only: an unclassified word never moves the lead
        }
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] The hold streak is bookkept BEFORE the hysteresis
        // gate, and off the VERDICT rather than off the move: it counts what the instrument saw,
        // not what the trim was allowed to do. A LATE or an EARLY ends the run whether or not it
        // was itself applied -- the value stopped producing EXCELLENT, which is the whole signal.
        out.holdStreak = noteHoldVerdict(out.key, out.code == QLatin1Char('X'));
        // [ORION_BANNER_TRIM_BIAS 2026-09-19] The bias window is fed by EVERY calibrating
        // verdict, greens included -- that is what makes it a RATE. It is fed here, before any
        // gate can return, for the same reason the hold streak is: it records what the
        // instrument saw, not what the trim was allowed to do.
        std::deque<QChar>& bias = biasRecent_[out.key];
        const int biasDepthLimit = sanitizedBiasWindow(limits.biasWindow);
        if (biasDepthLimit > 0) {
            bias.push_back(out.code);
            while (bias.size() > static_cast<std::size_t>(biasDepthLimit)) {
                bias.pop_front();
            }
        } else {
            bias.clear();   // the kill switch also drops the evidence it can no longer use
        }
        out.biasLate = static_cast<int>(
            std::count(bias.begin(), bias.end(), QLatin1Char('L')));
        out.biasEarly = static_cast<int>(
            std::count(bias.begin(), bias.end(), QLatin1Char('E')));
        out.biasDepth = static_cast<int>(bias.size());

        const double step = sanitized(limits.stepMs, 3.0);
        const double decay = sanitized(limits.decayMs, 1.0);
        const double maxMs = std::max(0.0, sanitized(limits.maxMs, 15.0));

        const bool directional = (out.code == QLatin1Char('L') || out.code == QLatin1Char('E'));
        // The streak rule's own refusal, decided first and applied last: the integrator below
        // only ever gets a verdict the streak rule was not going to step on, so the two can
        // never move the same verdict twice.
        QString streakRefusal;
        if (directional && previous != out.code) {
            // [ORION_BANNER_TRIM_ALTERNATION 2026-09-16] A STEP needs two CONSECUTIVE verdicts of
            // its own direction. `alternating` is reserved for the pathology the 09-16 session
            // produced -- a LATE straight after an EARLY, or the reverse -- so that word in a log
            // means "the loop watched a coin flip", not "this was the first verdict".
            streakRefusal = (previous == QLatin1Char('L') || previous == QLatin1Char('E'))
                ? QStringLiteral("alternating")
                : QStringLiteral("evidence");
        } else if (out.sameDirection < kRequiredSameDirection) {
            streakRefusal = QStringLiteral("evidence");   // one verdict is not a direction
        }
        if (!streakRefusal.isEmpty()) {
            // [ORION_BANNER_TRIM_BIAS 2026-09-19] THE 19 % CASE. The streak rule refused -- the
            // lates are not consecutive, which at a 79 % green rate they almost never are -- so
            // the NET vote gets to speak. Only on a LATE or an EARLY: an EXCELLENT must never
            // move the trim ([ORION_BANNER_TRIM_HOLD]), whatever the window behind it says.
            if (directional && applyBiasVotes(out, bias, limits, step, maxMs)) {
                return out;
            }
            out.reason = streakRefusal;
            return out;
        }

        double next = before;
        if (out.code == QLatin1Char('L')) {
            next = before + step;          // fire EARLIER
            out.reason = QStringLiteral("step");
        } else if (out.code == QLatin1Char('E')) {
            next = before - step;          // fire LATER
            out.reason = QStringLiteral("step");
        } else {                           // 'X' -- the value is RIGHT
            if (out.holdStreak < sanitizedHoldShots(limits.holdShots)) {
                out.reason = QStringLiteral("hold");
                return out;   // a trim producing EXCELLENT is the correct trim: move nothing
            }
            // Past the hold: a value that is merely TOLERATED bleeds off, one decay per further
            // EXCELLENT, so nothing can live in the trim forever inside a session.
            if (before > 0.0) {
                next = std::max(0.0, before - decay);
            } else if (before < 0.0) {
                next = std::min(0.0, before + decay);
            }
            out.reason = QStringLiteral("idle_decay");
        }
        next = std::clamp(next, -maxMs, maxMs);
        if (std::abs(next) < kZeroEpsilonMs) {
            next = 0.0;
        }
        if (std::abs(next - before) < 1e-9) {
            out.reason = QStringLiteral("clamped");
            return out;   // already at the clamp, or already at 0 with nothing to decay
        }
        trim_.insert(out.key, next);
        out.afterMs = next;
        out.applied = true;
        // [ORION_BANNER_TRIM_BIAS 2026-09-19] A STREAK step spends the same evidence the vote
        // would have spent, so it clears the window too. Without this, two consecutive lates
        // would step once here and again on the vote a shot or two later -- the compounding the
        // integrator is explicitly not allowed to do. The idle decay does NOT clear it: a decay
        // is the absence of evidence, not the consumption of it.
        if (out.reason == QLatin1String("step")) {
            bias.clear();
        }
        return out;
    }

    // ================================================================================
    // [ORION_RELEASE_ORACLE_TRIM 2026-09-15 owner] THE BANNER-FREE HALF OF THE SAME LOOP.
    //
    // WHY. The banner is the only VALIDATED instrument, but it is not always there: the live
    // reader saw 16 of 30 panels before the 09-15 recall work, it emits 2.5-3 s after the
    // release, and a game mode that never paints a feedback panel produces none at all. The
    // reader's own post-release RETRACTION ORACLE does not have that problem -- it is measured
    // off the meter the engine is already watching, it is available ~300-500 ms after every
    // release, and on the 09-15 framedump drill it agreed with panel_grade on 27/27 shots and
    // with the live banner on 25/25:
    //
    //     white-top -> green-bottom gap <= 3 px  = EXCELLENT      (settled_fill 90.06)
    //     gap >= 4 px                            = a miss         (settled_fill 85.47)
    //
    // THE ONE THING IT CANNOT DO is tell LATE from EARLY. The gap is the DISTANCE from the
    // green window, unsigned: a 9 px gap says "9 px of error", never which side. That single
    // fact is what makes this a different algorithm rather than a second verdict word, and it
    // is why it must never be laundered into codeFor()'s L/E alphabet.
    //
    // THE ALGORITHM. A bounded 1-D descent -- the textbook answer to "minimise |f(x)| when you
    // can only measure |f|, not its sign":
    //
    //   * a per-bucket DIRECTION (+1 = larger lead, the same sign convention as a LATE verdict),
    //     starting at +1 because a late landing is the failure this owner's rig produces
    //     (7 LATE / 2 EARLY in the drill that motivated the whole loop);
    //   * every oracle-graded shot pushes its gap onto a 6-deep per-bucket series;
    //   * every THIRD graded shot is a decision point: median(last 3) vs median(previous 3).
    //     If the gap GREW, the direction was wrong -- flip it. Medians, not means, because a
    //     single mis-measured retraction (a ghost meter, a clipped top strip) would otherwise
    //     own the comparison, and three is the smallest sample that has a median at all;
    //   * at that same decision point, step `stepMs` in the current direction ONLY while
    //     median(last 3) > kOracleGapDeadbandPx. Below the deadband the landing is already
    //     inside the green window and there is nothing left to chase;
    //   * a GREEN oracle HOLDS the trim, under the SAME 2-of-4 hysteresis the banner's EXCELLENT
    //     uses, and never steps -- and past `holdShots` consecutive greens it idles down by
    //     `decayMs` exactly as the banner's EXCELLENT does. "The value is right" means the same
    //     thing whichever instrument said it, and so does "the value has been right long enough
    //     to be worth retiring": the hold streak is ONE counter per bucket, shared by both
    //     instruments, because it is ONE trim and an oracle MISS is as much an end to a good run
    //     as a LATE is. See [ORION_BANNER_TRIM_HOLD] above for the 09-16 evidence.
    //
    // HYSTERESIS, KEPT IN SPIRIT. On the banner path one verdict can never move the lead; here
    // one oracle can never move it either -- a step costs three graded shots and a flip six.
    // The clamp, the zero snap, the per-bucket keying and the persistence map are the trim's
    // own, shared with the banner path, because it is ONE trim: the two instruments disagreeing
    // about where it lives would be the bug.
    //
    // SEPARATE WINDOWS, ONE TRIM. The oracle keeps its own `recent` window and its own series
    // rather than pushing into the banner's: the alphabets differ (a gap is not a word), the
    // latencies differ, and in practice the oracle only runs when the banner is ABSENT. Sharing
    // the window would let an unsigned measurement supply hysteresis for a signed one.
    // ================================================================================

    // The oracle's own two constants. 3.5 px is the midpoint of the measured separation
    // (<= 3 px green, >= 4 px miss), so the deadband sits exactly on the instrument's own
    // decision boundary rather than on a number chosen for it.
    static constexpr double kOracleGapDeadbandPx = 3.5;
    static constexpr int kOracleBatchSize = 3;
    static constexpr int kOracleSeriesDepth = 2 * kOracleBatchSize;

    // What one oracle message did. Mirrors Application so the engine's log line can be built
    // from either without a second code path.
    struct OracleApplication {
        bool applied = false;
        bool green = false;          // the proxy said the landing was inside the window
        bool flipped = false;        // this message reversed the descent direction
        bool decision = false;       // this message closed a 3-shot batch
        QString type;                // the BUCKET, not the raw shot type
        double beforeMs = 0.0;
        double afterMs = 0.0;
        double gapPx = 0.0;          // this shot's own gap
        double medianGapPx = 0.0;    // median of the last 3 graded shots (this one included)
        double previousMedianGapPx = -1.0;   // median of the 3 before those; -1 = fewer than 6
        int direction = 1;           // the direction AFTER any flip
        int graded = 0;              // oracle-graded shots in this bucket, this session
        QString recent;              // "M,M,X,M", oldest -> newest, oracle codes only
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] `decay` is now `hold` until the streak is spent and
        // `idle_decay` after it, the same two words the banner path uses.
        QString reason;              // step | flip_step | hold | idle_decay | deadband
                                     // | evidence | alternating | clamped
        int holdStreak = 0;          // consecutive EXCELLENT/GREEN in this bucket, this one
                                     // included; 0 on a miss
        // [ORION_BANNER_TRIM_TEMPO 2026-09-16] Same three fields as Application, same meaning.
        Tempo tempo = Tempo::Normal;
        double onsetMs = -1.0;
        QString key;
        // [ORION_BANNER_TRIM_RANGE 2026-09-17] Same field, same meaning as Application::range.
        Range range = Range::Unknown;
    };

    // One char per oracle, for `recent=` and for the decay hysteresis:
    //   X = green (the gap is inside the window)   M = miss (it is outside)
    // Deliberately NOT L/E: the oracle is unsigned and must never read as a direction.
    [[nodiscard]] static QChar oracleCodeFor(bool green)
    {
        return green ? QLatin1Char('X') : QLatin1Char('M');
    }

    OracleApplication observeOracle(const QString& shotType, double gapPx, bool green,
                                    const BannerLeadTrimLimits& limits, double onsetMs = -1.0,
                                    Range range = Range::Unknown)
    {
        OracleApplication out;
        out.type = bucketFor(shotType);
        out.green = green;
        out.gapPx = std::isfinite(gapPx) ? std::max(0.0, gapPx) : 0.0;
        out.onsetMs = (std::isfinite(onsetMs) && onsetMs >= 0.0) ? onsetMs : -1.0;
        out.tempo = tempoFor(shotType, out.onsetMs);
        out.range = rangeFor(shotType, range);
        out.key = keyFor(shotType, out.tempo, limits.tempoBuckets, out.range,
                         limits.rangeBuckets);

        std::deque<double>& series = oracleGaps_[out.key];
        series.push_back(out.gapPx);
        while (series.size() > static_cast<std::size_t>(kOracleSeriesDepth)) {
            series.pop_front();
        }
        std::deque<QChar>& window = oracleRecent_[out.key];
        // [ORION_BANNER_TRIM_ALTERNATION 2026-09-16] The preceding oracle in this bucket, read
        // before this one joins the window -- the descent's half of the consecutive-verdict rule.
        const QChar previous = window.empty() ? QLatin1Char('\0') : window.back();
        window.push_back(oracleCodeFor(green));
        while (window.size() > static_cast<std::size_t>(kRecentWindow)) {
            window.pop_front();
        }
        out.recent = joinWindow(oracleRecent_, out.key);
        const int graded = oracleGraded_.value(out.key, 0) + 1;
        oracleGraded_.insert(out.key, graded);
        out.graded = graded;
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] Bookkept for EVERY graded oracle, before any branch
        // can return: a MISS ends a good run exactly as a LATE does, whether or not it was itself
        // the shot that closed a batch.
        out.holdStreak = noteHoldVerdict(out.key, green);

        int direction = oracleDirection_.value(out.key, 1);
        out.medianGapPx = medianOfLast(series, kOracleBatchSize, 0);
        out.decision = (graded % kOracleBatchSize) == 0;
        if (out.decision && series.size() >= static_cast<std::size_t>(kOracleSeriesDepth)) {
            out.previousMedianGapPx = medianOfLast(series, kOracleBatchSize, kOracleBatchSize);
            // THE ONLY SIGN INFORMATION THE ORACLE CARRIES: whether the last three shots were
            // WORSE than the three before them. Strictly greater -- an unchanged median is not
            // evidence that the direction is wrong, and flipping on it would oscillate forever
            // on a rig that is simply sitting at its floor.
            if (out.medianGapPx > out.previousMedianGapPx + 1e-9) {
                direction = -direction;
                out.flipped = true;
            }
        }
        oracleDirection_.insert(out.key, direction);
        out.direction = direction;

        const double before = trim_.value(out.key, 0.0);
        out.beforeMs = before;
        out.afterMs = before;

        const double step = sanitized(limits.stepMs, 3.0);
        const double decay = sanitized(limits.decayMs, 1.0);
        const double maxMs = std::max(0.0, sanitized(limits.maxMs, 15.0));
        double next = before;
        if (green) {
            // Exactly the banner's EXCELLENT rule, hysteresis and hold included.
            const int sameDirection = static_cast<int>(
                std::count(window.begin(), window.end(), QLatin1Char('X')));
            if (sameDirection < kRequiredSameDirection) {
                out.reason = QStringLiteral("evidence");
                return out;
            }
            if (out.holdStreak < sanitizedHoldShots(limits.holdShots)) {
                out.reason = QStringLiteral("hold");
                return out;   // a trim landing in the window is the correct trim
            }
            if (before > 0.0) {
                next = std::max(0.0, before - decay);
            } else if (before < 0.0) {
                next = std::min(0.0, before + decay);
            }
            out.reason = QStringLiteral("idle_decay");
        } else if (!out.decision) {
            out.reason = QStringLiteral("evidence");   // one gap is not a direction
            return out;
        } else if (out.medianGapPx <= kOracleGapDeadbandPx) {
            out.reason = QStringLiteral("deadband");   // already inside the window
            return out;
        } else if (previous == QLatin1Char('X')) {
            // [ORION_BANNER_TRIM_ALTERNATION 2026-09-16] The descent's half of the rule: a MISS
            // straight after a GREEN is the unsigned form of the same coin flip, so the batch
            // that closed on it does not get to step. The next batch that closes on two
            // consecutive misses does.
            out.reason = QStringLiteral("alternating");
            return out;
        } else {
            next = before + direction * step;
            out.reason = out.flipped ? QStringLiteral("flip_step") : QStringLiteral("step");
        }

        next = std::clamp(next, -maxMs, maxMs);
        if (std::abs(next) < kZeroEpsilonMs) {
            next = 0.0;
        }
        if (std::abs(next - before) < 1e-9) {
            out.reason = QStringLiteral("clamped");
            return out;
        }
        trim_.insert(out.key, next);
        out.afterMs = next;
        out.applied = true;
        return out;
    }

    [[nodiscard]] int oracleDirectionForType(const QString& shotType,
                                             Tempo tempo = Tempo::Normal,
                                             Range range = Range::Unknown) const
    {
        const QString key = resolvedKey(oracleDirection_, shotType, tempo, range);
        return oracleDirection_.value(key, 1);
    }

    // [ORION_BANNER_TRIM_HOLD 2026-09-16] Consecutive EXCELLENT/GREEN verdicts standing in this
    // bucket right now, from BOTH instruments. Session evidence, never persisted.
    [[nodiscard]] int holdStreakForType(const QString& shotType,
                                        Tempo tempo = Tempo::Normal,
                                        Range range = Range::Unknown) const
    {
        return holdStreak_.value(resolvedKey(holdStreak_, shotType, tempo, range), 0);
    }

    [[nodiscard]] QString oracleRecentString(const QString& shotType,
                                             Tempo tempo = Tempo::Normal,
                                             Range range = Range::Unknown) const
    {
        return joinWindow(oracleRecent_, resolvedKey(oracleRecent_, shotType, tempo, range));
    }

    // ---------------------------------------------------------------- reads
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16] EVERY read takes the tempo sub-bucket and falls back
    // to the bare type key, so one accessor serves both modes: with the tempo buckets armed the
    // entries are "Standstill/normal" and the first lookup finds them; with the kill switch
    // thrown they are "Standstill" and the fallback does. Tempo::Normal is the default because
    // that is the REFERENCE class -- what the card shows, what a caller with no onset files in,
    // and where every legacy persisted key lands.
    [[nodiscard]] double trimMsForType(const QString& shotType,
                                       Tempo tempo = Tempo::Normal,
                                       Range range = Range::Unknown) const
    {
        return trim_.value(resolvedKey(trim_, shotType, tempo, range), 0.0);
    }

    [[nodiscard]] QMap<QString, double> snapshot() const { return trim_; }

    [[nodiscard]] QString recentString(const QString& shotType,
                                       Tempo tempo = Tempo::Normal,
                                       Range range = Range::Unknown) const
    {
        return joinWindow(recent_, resolvedKey(recent_, shotType, tempo, range));
    }

    [[nodiscard]] bool isZero() const
    {
        for (auto it = trim_.constBegin(); it != trim_.constEnd(); ++it) {
            if (std::abs(it.value()) >= kZeroEpsilonMs) {
                return false;
            }
        }
        return true;
    }

    // ---------------------------------------------------------------- lifecycle
    // The owner moved the Shot Lead slider. Their intent outranks every verdict the trim was
    // built from: those verdicts described the OLD value, so keeping any part of them would
    // silently move the new one before a single shot has graded it.
    bool reset()
    {
        const bool had = !trim_.isEmpty() || !recent_.isEmpty()
            || !oracleGaps_.isEmpty() || !oracleRecent_.isEmpty();
        trim_.clear();
        recent_.clear();
        // [ORION_BANNER_TRIM_BIAS 2026-09-19] ...and so is the integrator's window: every
        // verdict in it graded the OLD slider position, so a bias learned there must not spend
        // itself on the new one.
        biasRecent_.clear();
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] The hold streak is evidence about the OLD value
        // exactly as the hysteresis window is: a dozen greens earned under the previous slider
        // position must not let the very first EXCELLENT under the new one decay anything.
        holdStreak_.clear();
        // [ORION_RELEASE_ORACLE_TRIM] The descent's state goes with it for the same reason the
        // banner window does: every gap in the series was measured against the OLD lead, and a
        // direction learned there would start walking the owner's new value before one shot has
        // graded it. The direction resets to +1 with the map, not to its last value.
        oracleGaps_.clear();
        oracleRecent_.clear();
        oracleGraded_.clear();
        oracleDirection_.clear();
        return had;
    }

    // App start, from learning.json. Half strength, and the hysteresis window is NOT restored:
    // last night's run is not evidence about this one, so the first move of the session still
    // costs two verdicts.
    // [ORION_BANNER_TRIM_TEMPO 2026-09-16] `tempoBuckets` is the live setting, not the one the
    // file was written under: a key is validated on what it SAYS and installed under what this
    // run keys on. With the buckets armed, "Standstill" (a file from before this change) loads
    // into "Standstill/normal" -- the reference class, and the one the overwhelming majority of
    // any legacy trim's evidence came from. With them disarmed the rule is byte-for-byte the
    // 2026-09-16 one, tempo-suffixed keys included: bucketFor("Standstill/quick") is "Other",
    // which does not round-trip, so the entry is not ours to install and is skipped.
    void restoreDecayed(const QMap<QString, double>& persisted, double maxMs,
                        bool tempoBuckets = true, bool rangeBuckets = true)
    {
        trim_.clear();
        recent_.clear();
        // [ORION_BANNER_TRIM_BIAS 2026-09-19] Session evidence for the same reason: last night's
        // 19 % was measured on last night's context, which is why the trim itself comes back
        // halved rather than whole.
        biasRecent_.clear();
        // [ORION_BANNER_TRIM_HOLD 2026-09-16] Session evidence, so it starts empty: the halved
        // trim has to re-earn its hold from this session's own verdicts.
        holdStreak_.clear();
        // [ORION_RELEASE_ORACLE_TRIM] PERSISTENCE IS UNCHANGED: the only thing on disk is the
        // trim map (banner_lead_trim_by_type). The descent's series, window, count and direction
        // are session evidence exactly as the banner's hysteresis window is -- last night's gaps
        // were measured on last night's context, which is the whole reason the trim itself comes
        // back halved.
        oracleGaps_.clear();
        oracleRecent_.clear();
        oracleGraded_.clear();
        oracleDirection_.clear();
        const double bound = std::max(0.0, sanitized(maxMs, 15.0));
        for (auto it = persisted.constBegin(); it != persisted.constEnd(); ++it) {
            const double raw = it.value();
            if (!std::isfinite(raw) || std::abs(raw) > kPersistCeilingMs) {
                continue;   // schema guard: a corrupt entry degrades to "no trim", never to a
                            // trim no setting could have produced
            }
            const QString rawKey = it.key().trimmed();
            QString typePart = rawKey;
            Tempo tempo = Tempo::Normal;
            Range range = Range::Unknown;
            if (tempoBuckets) {
                // [ORION_BANNER_TRIM_RANGE 2026-09-17] A key is PARSED RIGHT TO LEFT: an optional
                // range word ("mid"/"three"), then the tempo word, then the type. So
                //   "Left Fade"              (pre-2026-09-16) -> Left Fade / normal
                //   "Left Fade/normal"       (2026-09-16)     -> Left Fade / normal
                //   "Left Fade/normal/mid"   (this build)     -> Left Fade / normal / mid
                // all load, and a suffix this build does not write is still refused outright
                // rather than guessed at. With the RANGE kill switch thrown a ranged key folds
                // into its (type, tempo) bucket, which is where its evidence came from.
                QString head = rawKey;
                qsizetype slash = head.lastIndexOf(QLatin1Char('/'));
                if (slash >= 0 && isRangeName(head.mid(slash + 1))) {
                    range = rangeFromName(head.mid(slash + 1));
                    head = head.left(slash).trimmed();
                    slash = head.lastIndexOf(QLatin1Char('/'));
                }
                typePart = head;
                if (slash >= 0) {
                    const QString suffix = head.mid(slash + 1);
                    if (!isTempoName(suffix)) {
                        continue;   // a suffix this build does not write is not ours to install
                    }
                    typePart = head.left(slash).trimmed();
                    tempo = tempoFromName(suffix);
                } else if (range != Range::Unknown) {
                    continue;   // a range with no tempo is not a shape this build ever wrote
                }
            }
            const QString bucket = bucketFor(typePart);
            if (bucket != typePart) {
                continue;   // a key this build does not bucket to itself is not ours to install
            }
            if (range != Range::Unknown && !isFadeBucket(bucket)) {
                continue;   // range is a FADE-only dimension; a ranged Standstill is not ours
            }
            double value = std::clamp(raw * kStartDecayFactor, -bound, bound);
            if (std::abs(value) < kZeroEpsilonMs) {
                continue;
            }
            const QString key = keyFor(bucket, tempo, tempoBuckets, range, rangeBuckets);
            // The range kill switch can fold two persisted keys onto one live key; the LARGER
            // magnitude wins, because a trim that was earned is evidence and a zero is not.
            const auto existing = trim_.constFind(key);
            if (existing != trim_.constEnd() && std::abs(existing.value()) >= std::abs(value)) {
                continue;
            }
            trim_.insert(key, value);
        }
    }

    // A live change to banner_trim_max_ms must not leave a larger trim standing.
    bool clampTo(double maxMs)
    {
        const double bound = std::max(0.0, sanitized(maxMs, 15.0));
        bool changed = false;
        for (auto it = trim_.begin(); it != trim_.end(); ++it) {
            const double clamped = std::clamp(it.value(), -bound, bound);
            if (std::abs(clamped - it.value()) >= 1e-9) {
                it.value() = clamped;
                changed = true;
            }
        }
        return changed;
    }

private:
    [[nodiscard]] static double sanitized(double value, double fallback)
    {
        return std::isfinite(value) ? value : fallback;
    }

    // [ORION_BANNER_TRIM_HOLD 2026-09-16] A hold of 0 or less would mean "decay on the first
    // EXCELLENT", which is precisely the behaviour the 09-16 evidence retired, so a value no
    // setting could have produced falls back to the shipped dozen rather than to 0.
    [[nodiscard]] static int sanitizedHoldShots(int value)
    {
        return value > 0 ? value : kHoldShotsFallback;
    }

    // [ORION_BANNER_TRIM_BIAS 2026-09-19] Unlike the hold, a non-positive bias limit is a legal
    // REQUEST -- `banner_trim_bias_votes = 0` is the documented kill switch -- so it degrades to
    // "no integrator", never to a fallback that would move a lead the owner asked to leave alone.
    [[nodiscard]] static int sanitizedBiasWindow(int value) { return value > 0 ? value : 0; }
    [[nodiscard]] static int sanitizedBiasVotes(int value) { return value > 0 ? value : 0; }

    // [ORION_BANNER_TRIM_BIAS 2026-09-19] The net-vote integrator. Returns TRUE when the vote
    // fired at all -- including the case where the clamp then refused the move, which is
    // reported exactly as the streak path reports it -- so the caller knows the verdict was
    // consumed here rather than by its own refusal.
    bool applyBiasVotes(Application& out, std::deque<QChar>& window,
                        const BannerLeadTrimLimits& limits, double step, double maxMs)
    {
        const int votes = sanitizedBiasVotes(limits.biasVotes);
        if (votes <= 0 || window.empty()) {
            return false;
        }
        const int net = out.biasLate - out.biasEarly;
        const int direction = (net >= votes) ? 1 : ((-net >= votes) ? -1 : 0);
        if (direction == 0) {
            return false;
        }
        // Spent whether or not the clamp lets the trim move: the window is EVIDENCE FOR A MOVE,
        // and a decision has now been taken on it. Leaving it standing at the clamp would also
        // re-fire the vote on every single further verdict.
        window.clear();
        const double before = out.beforeMs;
        double next = std::clamp(before + direction * step, -maxMs, maxMs);
        if (std::abs(next) < kZeroEpsilonMs) {
            next = 0.0;
        }
        if (std::abs(next - before) < 1e-9) {
            out.reason = QStringLiteral("clamped");
            return true;
        }
        trim_.insert(out.key, next);
        out.afterMs = next;
        out.applied = true;
        out.reason = QStringLiteral("bias_votes");
        return true;
    }

    // [ORION_BANNER_TRIM_TEMPO 2026-09-16] The key an accessor should read for (type, tempo):
    // the composite one when this session actually wrote composite keys, the bare bucket when the
    // kill switch collapsed them. Decided per map by what is IN it, so one accessor serves both
    // modes without the policy having to remember which one produced its own state.
    // [ORION_BANNER_TRIM_RANGE 2026-09-17] ...and the range key is tried FIRST, so a fade whose
    // range this build knows reads its own sub-bucket and every other caller reads exactly what
    // it read before. The chain is deliberately narrowing, never widening: a shot with a known
    // range that has no range bucket yet falls back to its (type, tempo) evidence rather than
    // starting from zero, which is what makes the first range-keyed shot of a session behave
    // like today's build instead of like a fresh install.
    template <typename MapT>
    [[nodiscard]] static QString resolvedKey(const MapT& map, const QString& shotType, Tempo tempo,
                                             Range range = Range::Unknown)
    {
        const Range filed = rangeFor(shotType, range);
        if (filed != Range::Unknown) {
            const QString ranged = keyFor(shotType, tempo, true, filed, true);
            if (map.contains(ranged)) {
                return ranged;
            }
        }
        const QString composite = keyFor(shotType, tempo, true);
        return map.contains(composite) ? composite : bucketFor(shotType);
    }

    // The `recent=` rendering, off a RAW map key (never a shot type -- the internal callers hand
    // in the key they just wrote).
    template <typename MapT>
    [[nodiscard]] static QString joinWindow(const MapT& map, const QString& key)
    {
        const auto it = map.constFind(key);
        if (it == map.constEnd() || it->empty()) {
            return QString();
        }
        QStringList parts;
        parts.reserve(static_cast<qsizetype>(it->size()));
        for (const QChar ch : *it) {
            parts << QString(ch);
        }
        return parts.join(QLatin1Char(','));
    }

    // One counter per bucket, shared by the banner's EXCELLENT and the oracle's GREEN: it is one
    // trim, so "this value has been right for N shots" has to be one number. Returns the streak
    // AFTER this verdict.
    int noteHoldVerdict(const QString& bucket, bool good)
    {
        const int streak = good ? holdStreak_.value(bucket, 0) + 1 : 0;
        holdStreak_.insert(bucket, streak);
        return streak;
    }

    // [ORION_RELEASE_ORACLE_TRIM] Median of `count` samples ending `offset` back from the newest.
    // offset 0 = the last three, offset 3 = the three before those. Returns -1 when the series
    // is too short, which every caller treats as "no comparison available" rather than as 0 --
    // a missing median must never read as a perfect landing.
    [[nodiscard]] static double medianOfLast(const std::deque<double>& series, int count,
                                             int offset)
    {
        const int available = static_cast<int>(series.size()) - offset;
        if (count <= 0 || available < count) {
            return -1.0;
        }
        std::vector<double> window(series.end() - offset - count, series.end() - offset);
        std::sort(window.begin(), window.end());
        const std::size_t mid = window.size() / 2;
        return (window.size() % 2 == 0)
            ? 0.5 * (window[mid - 1] + window[mid])
            : window[mid];
    }

    QMap<QString, double> trim_;
    QMap<QString, std::deque<QChar>> recent_;
    // [ORION_BANNER_TRIM_BIAS 2026-09-19] The integrator's own sliding window per bucket, deeper
    // than `recent_` and read with a NET vote rather than a same-direction count. Session
    // evidence like every other window here: never persisted, cleared by a reset, by a restore
    // and by any step that spends it.
    QMap<QString, std::deque<QChar>> biasRecent_;
    // [ORION_BANNER_TRIM_HOLD 2026-09-16] Consecutive EXCELLENT/GREEN per bucket, from BOTH
    // instruments. Session evidence like every other window here: never persisted, cleared by a
    // reset and by a restore.
    QMap<QString, int> holdStreak_;
    // [ORION_RELEASE_ORACLE_TRIM] The descent's own state. Never persisted, never shared with
    // the banner window above.
    QMap<QString, std::deque<double>> oracleGaps_;
    QMap<QString, std::deque<QChar>> oracleRecent_;
    QMap<QString, int> oracleGraded_;
    QMap<QString, int> oracleDirection_;
};

} // namespace orion
