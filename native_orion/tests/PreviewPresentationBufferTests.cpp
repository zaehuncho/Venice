#include "AppConfig.h"
#include "PreviewPresentationBuffer.h"
#include "MeterBoxRing.h"
#include "MeterOverlayPolicy.h"
#include "RemoteFrameSnapshotStore.h"

#include <QtCore/QAbstractItemModel>
#include <QtCore/QFile>
#include <QtCore/QUrl>
#include <QtGui/QColor>
#include <QtQml/QQmlComponent>
#include <QtQml/QQmlContext>
#include <QtQml/QQmlEngine>
#include <QtQml/QQmlError>
#include <QtQml/QQmlPropertyMap>
#include <QtQml/qqml.h>
#include <QtQuick/QQuickImageProvider>
#include <QtQuick/QQuickItem>
#include <QtTest/QSignalSpy>
#include <QtTest/QTest>

#include <limits>
#include <memory>
#include <vector>

using namespace orion;

namespace {

// EXACT_PRESENTED_REGRESSION_HELPER_BEGIN
// Baseline builds execute the old production backfill operation. The same
// scenarios then execute the real new method when that method is available.
template <typename Store>
bool applyPresentedExactCorrection(Store& store, int acknowledged,
                                   const QSize& captureSize, int sourceFrame,
                                   const QRect& box, quint64 shot)
{
    if constexpr (requires {
                      store.backfillPresentedExact(
                          acknowledged, captureSize, sourceFrame, box, shot);
                  }) {
        return store.backfillPresentedExact(
            acknowledged, captureSize, sourceFrame, box, shot);
    } else {
        // The old callback could amend only pending textures. Its already-
        // acknowledged snapshot necessarily remains unchanged in this scenario.
        static_cast<void>(store.backfillUnacknowledged(
            acknowledged, captureSize, 2,
            [&](int requestedFrame, QRect& out, int& matched) {
                if (requestedFrame != sourceFrame) return false;
                out = box;
                matched = sourceFrame;
                return true;
            }));
        return false;
    }
}
// EXACT_PRESENTED_REGRESSION_HELPER_END

QImage imageWithMarker(QRgb marker)
{
    QImage image(2, 2, QImage::Format_RGB32);
    image.fill(marker);
    return image;
}

class SmokeFrameProvider final : public QQuickImageProvider {
public:
    SmokeFrameProvider() : QQuickImageProvider(QQuickImageProvider::Image) {}

    QImage requestImage(const QString&, QSize* size, const QSize&) override
    {
        QImage image(16, 9, QImage::Format_RGB32);
        image.fill(Qt::black);
        if (size) {
            *size = image.size();
        }
        return image;
    }
};

class QmlOrionSmokeStub final : public QQmlPropertyMap {
    Q_OBJECT
public:
    QmlOrionSmokeStub()
    {
        insert(QStringLiteral("updateGatePhase"), QStringLiteral("clear"));
        insert(QStringLiteral("authenticated"), true);
        insert(QStringLiteral("legalAccepted"), true);
        insert(QStringLiteral("streamSetupComplete"), true);
        insert(QStringLiteral("preflightComplete"), true);
        insert(QStringLiteral("currentPage"), QStringLiteral("remotePlay"));
        insert(QStringLiteral("accentColor"), QStringLiteral("#2563EB"));
        insert(QStringLiteral("iconSource"), QString{});
        insert(QStringLiteral("displayVersion"), QStringLiteral("smoke"));
        insert(QStringLiteral("updateChannel"), QStringLiteral("stable"));
        insert(QStringLiteral("activeProfile"), QStringLiteral("Default"));
        insert(QStringLiteral("licenseState"), QStringLiteral("Verified"));
        insert(QStringLiteral("licenseKeyMasked"), QStringLiteral("****-****-****-A1B2"));
        insert(QStringLiteral("machineIdMasked"), QStringLiteral("******ABCDEF"));
        // [LICENCE STRIP 2026-09-15] The controller's own expiry phrasing, which the
        // sidebar strip falls back to when the backend has sent no profile block.
        // "-" is exactly what OrionAppController publishes with no expiry loaded.
        insert(QStringLiteral("timeLeft"), QStringLiteral("-"));
        insert(QStringLiteral("timeLeftDetail"), QStringLiteral("No expiry loaded"));
        insert(QStringLiteral("motdVisible"), false);
        insert(QStringLiteral("motdText"), QString{});
        insert(QStringLiteral("motdLevel"), QStringLiteral("info"));
        // [PROFILE BLOCK 2026-09-14] The backend `profile` block, as the controller
        // republishes it. These are the production DEFAULTS for a launcher whose
        // backend has not sent one yet (profileKnown == false), which is exactly the
        // state the sidebar licence strip must render without inventing an expiry.
        insert(QStringLiteral("profileKnown"), false);
        insert(QStringLiteral("profileDiscordId"), QString{});
        insert(QStringLiteral("profileDiscordName"), QString{});
        insert(QStringLiteral("profilePlan"), QString{});
        insert(QStringLiteral("profileExpiryEpochS"), 0.0);
        insert(QStringLiteral("profileDaysLeft"), -1);
        insert(QStringLiteral("profileLifetime"), false);
        insert(QStringLiteral("profileActivatedEpochS"), 0.0);
        insert(QStringLiteral("profileHwidResetsUsed"), 0);
        insert(QStringLiteral("profileHwidResetsFreeTotal"), 0);
        insert(QStringLiteral("profileHwidResetsFreeRemaining"), 0);
        insert(QStringLiteral("profileHwidPaidCredits"), 0);
        insert(QStringLiteral("activityText"), QString{});
        insert(QStringLiteral("debugUiEnabled"), false);
        insert(QStringLiteral("qmlRenderMode"), true);
        insert(QStringLiteral("previewAsync"), true);
        insert(QStringLiteral("frameSerial"), 0);
        insert(QStringLiteral("remoteRunning"), false);
        insert(QStringLiteral("capturePreviewActive"), false);
        insert(QStringLiteral("chiakiEmbedStatus"), QString{});
        insert(QStringLiteral("remoteState"), QStringLiteral("Disconnected"));
        insert(QStringLiteral("remoteStatus"), QStringLiteral("Smoke test"));
        // Detection-overlay appearance. Present with production defaults so the
        // page under test resolves real values instead of undefined — an
        // undefined colour would silently paint the lock box transparent.
        // The colour is the shipped bright-blue factory default; the HUD test
        // below asserts it stays equal to AppConfigData's constant.
        insert(QStringLiteral("meterOverlayColor"),
               QString::fromLatin1(AppConfigData::kMeterOverlayDefaultColor));
        insert(QStringLiteral("meterOverlayDrawColor"),
               QString::fromLatin1(AppConfigData::kMeterOverlayDefaultColor));
        insert(QStringLiteral("meterOverlayStyle"), QStringLiteral("Solid"));
        insert(QStringLiteral("meterOverlayRgb"), false);
        // [METER DETECTION CARD 2026-09-10] MeterConfigPanel's Detector combo and health
        // line. Production defaults: the pure-CV proposer, and no health report yet.
        insert(QStringLiteral("meterProposer"), QStringLiteral("cv"));
        insert(QStringLiteral("detectorHealthLine"), QString{});
        insert(QStringLiteral("detectorProvider"), QString{});
        // [ORION_BANNER_VERDICT_LIVE 2026-09-14] The live shot-verdict tally, as
        // components/ShotVerdictTally.qml reads it from inside BOTH tuning cards.
        // Production defaults for a launcher that has not graded a banner yet, so the
        // page under test resolves real values instead of undefined.
        insert(QStringLiteral("bannerGreen10"), 0);
        insert(QStringLiteral("bannerEarly10"), 0);
        insert(QStringLiteral("bannerLate10"), 0);
        insert(QStringLiteral("bannerOther10"), 0);
        insert(QStringLiteral("bannerCount10"), 0);
        insert(QStringLiteral("bannerContested10"), 0);
        insert(QStringLiteral("bannerLastTiming"), QString{});
        insert(QStringLiteral("bannerLastCoverage"), QString{});
        insert(QStringLiteral("bannerPattern10"), QString{});
        insert(QStringLiteral("bannerSuggestion"), QString{});
        // [ORION_BANNER_LEAD_TRIM 2026-09-15] The Shot Lead card's auto-trim caption binds these.
        // 0 is the state the caption HIDES in, which is what an untuned smoke should render.
        insert(QStringLiteral("bannerLeadTrimMs"), 0.0);
        insert(QStringLiteral("bannerLeadTrimEnabled"), true);
        // Live meter-side HUD lines, production placeholders. The three rows
        // bind these pre-formatted native strings directly; the HUD test
        // overwrites them to prove QML surfaces the native values verbatim.
        // meterHudCourtLine/meterHudJitterLine removed 2026-08-06 with the
        // COURT/JITTER rows (owner: three values only, the ones the bot uses).
        insert(QStringLiteral("meterHudMeasured"), false);
        insert(QStringLiteral("meterHudLive"), false);
        insert(QStringLiteral("meterHudCommandLate"), false);
        insert(QStringLiteral("meterHudFillLine"), QStringLiteral("--"));
        insert(QStringLiteral("meterDelayMaxUsableMs"), 118.0);
        insert(QStringLiteral("meterDelayLeadOffsetAppliedMs"), 0.0);
        insert(QStringLiteral("meterHudTipLine"), QStringLiteral("--"));
        insert(QStringLiteral("meterHudFireLine"), QStringLiteral("--"));
        insert(QStringLiteral("noMeterAvailable"), true);
        insert(QStringLiteral("noMeterUnavailableReason"), QString{});
        insert(QStringLiteral("noMeterEnabled"), false);
        insert(QStringLiteral("logText"), QString{});
        insert(QStringLiteral("meterConfirmed"), false);
        insert(QStringLiteral("showLiveMeterMetrics"), true);
        insert(QStringLiteral("meterCalibrating"), false);
        insert(QStringLiteral("meterCalibrationStatus"), QString{});
        insert(QStringLiteral("latencyCalibrationReady"), false);
        insert(QStringLiteral("latencyCalibrationActive"), false);
        insert(QStringLiteral("latencyCalibrationStatus"), QStringLiteral("Learning live"));
        // [ORION_USER_TIP] Tip Timing card bindings (production defaults: base-30
        // effective aim = the shipped constant, learner in control, unlocked).
        insert(QStringLiteral("tipTimingMs"), 393.0);
        insert(QStringLiteral("tipTimingUserSet"), false);
        insert(QStringLiteral("tipTimingLocked"), false);
        insert(QStringLiteral("tipTimingLearnedActive"), false);
        insert(QStringLiteral("tipTimingMinMs"), 314.0);
        insert(QStringLiteral("tipTimingMaxMs"), 504.0);
        // [ORION_TEMPO_RELEASE_STYLE 2026-09-15] Everything RhythmCard binds, at the production
        // defaults: the flick ON (otherwise the style row is correctly hidden and the test would
        // be asserting against an invisible control) and the shipped "flick" style.
        insert(QStringLiteral("tempoEnabled"), true);
        insert(QStringLiteral("tempoInputPath"), QStringLiteral("Button"));
        insert(QStringLiteral("inputTimedRhythmEnabled"), false);
        insert(QStringLiteral("rhythmFlickDelayMs"), 0.0);
        insert(QStringLiteral("tempoReleaseStyle"), QStringLiteral("flick"));
    }

    Q_INVOKABLE void startCapturePreview() {}
    Q_INVOKABLE void setChiakiEmbedVisible(bool) {}
    Q_INVOKABLE void updateChiakiEmbedRect(double, double, double, double) {}
    Q_INVOKABLE void acknowledgeRemoteFramePresented(int) {}
    Q_INVOKABLE void setPreviewRenderClockActive(bool) {}
    Q_INVOKABLE void advanceRemotePreviewPresentation() {}
    Q_INVOKABLE void requestApplicationShutdown() {}

signals:
    void statusChanged();
    void frameChanged();
    void poseOverlayChanged();
    void meterBoxChanged();
    void logsChanged();
};

void registerSmokeQmlTypes(const QString& qmlRoot)
{
    static bool registered = false;
    if (registered) {
        return;
    }
    qmlRegisterSingletonType(
        QUrl::fromLocalFile(qmlRoot + QStringLiteral("/Theme.qml")),
        "OrionNative", 1, 0, "Theme");
    qmlRegisterSingletonType(
        QUrl::fromLocalFile(qmlRoot + QStringLiteral("/components/TourRegistry.qml")),
        "OrionNative", 1, 0, "TourRegistry");
    registered = true;
}

struct CadenceSimulationResult final {
    int dropped = 0;
    int underflows = 0;
    std::size_t maximumDepth = 0;
    bool reserveFellBelowTarget = false;
    bool reserveRecovered = false;
    qint64 minimumPeriodNs = std::numeric_limits<qint64>::max();
    qint64 maximumPeriodNs = 0;
    qint64 finalIntegralCorrectionNs = 0;
};

CadenceSimulationResult simulatePresentationCadence(
    const std::vector<qint64>& sourceIntervalsNs,
    qint64 durationNs,
    std::size_t recoveryObservationStart = 0)
{
    PreviewPresentationBuffer::AdaptiveCadence cadence;
    cadence.reset(60);

    // This is the ordinary state immediately after the initial three-frame
    // prime presents its first frame.
    std::size_t depth = PreviewPresentationBuffer::kPrimeDepth - 1;
    qint64 nextSourceNs = sourceIntervalsNs.front();
    std::size_t sourceInterval = 1;
    qint64 nextPresentationNs = cadence.period().count();
    CadenceSimulationResult out;
    out.maximumDepth = depth;

    while (std::min(nextSourceNs, nextPresentationNs) <= durationNs) {
        if (nextSourceNs <= nextPresentationNs) {
            if (depth == PreviewPresentationBuffer::kCapacity) {
                ++out.dropped;
            } else {
                ++depth;
                out.maximumDepth = std::max(out.maximumDepth, depth);
            }
            nextSourceNs += sourceIntervalsNs[
                sourceInterval++ % sourceIntervalsNs.size()];
            continue;
        }

        if (depth == 0) {
            ++out.underflows;
            cadence.observeEmptyWake();
        } else {
            --depth;
            cadence.observePresentedReserve(depth);
            if (sourceInterval > recoveryObservationStart) {
                if (depth < PreviewPresentationBuffer::kRecoveryDepth) {
                    out.reserveFellBelowTarget = true;
                } else if (out.reserveFellBelowTarget) {
                    out.reserveRecovered = true;
                }
            }
        }
        const qint64 periodNs = cadence.period().count();
        out.minimumPeriodNs = std::min(out.minimumPeriodNs, periodNs);
        out.maximumPeriodNs = std::max(out.maximumPeriodNs, periodNs);
        nextPresentationNs += periodNs;
    }
    out.finalIntegralCorrectionNs = cadence.integralCorrectionNs();
    return out;
}

} // namespace

class PreviewPresentationBufferTests final : public QObject {
    Q_OBJECT

private slots:
    void rejectsNullFrames()
    {
        PreviewPresentationBuffer buffer;
        const auto result = buffer.push({}, 44);
        QVERIFY(!result.accepted);
        QCOMPARE(buffer.depth(), std::size_t{0});
    }

    void preservesFrameIdentityAndOrder()
    {
        PreviewPresentationBuffer buffer;
        const QRgb firstMarker = qRgb(10, 20, 30);
        const QRgb secondMarker = qRgb(40, 50, 60);
        QVERIFY(buffer.push(imageWithMarker(firstMarker), 101).accepted);
        QVERIFY(buffer.push(imageWithMarker(secondMarker), 102).accepted);

        auto first = buffer.take();
        QVERIFY(first.has_value());
        QCOMPARE(first->frameNumber, 101);
        QCOMPARE(first->image.pixel(0, 0), firstMarker);

        auto second = buffer.take();
        QVERIFY(second.has_value());
        QCOMPARE(second->frameNumber, 102);
        QCOMPARE(second->image.pixel(0, 0), secondMarker);
        QVERIFY(buffer.empty());
    }

    void overflowDropsOldestImageAndItsIdTogether()
    {
        PreviewPresentationBuffer buffer;
        for (int frame = 1;
             frame <= static_cast<int>(PreviewPresentationBuffer::kCapacity);
             ++frame) {
            const auto result = buffer.push(imageWithMarker(qRgb(frame, 0, 0)), frame);
            QVERIFY(result.accepted);
            QVERIFY(!result.droppedOldest);
        }
        const int overflowFrame = static_cast<int>(PreviewPresentationBuffer::kCapacity) + 1;
        const auto overflow = buffer.push(
            imageWithMarker(qRgb(overflowFrame, 0, 0)), overflowFrame);
        QVERIFY(overflow.accepted);
        QVERIFY(overflow.droppedOldest);
        QCOMPARE(overflow.depth, PreviewPresentationBuffer::kCapacity);

        auto first = buffer.take();
        QVERIFY(first.has_value());
        QCOMPARE(first->frameNumber, 2);
        QCOMPARE(qRed(first->image.pixel(0, 0)), 2);
    }

    void primeDepthAbsorbsObservedFortySevenMillisecondSourceBurst()
    {
        PreviewPresentationBuffer buffer;
        for (int frame = 1;
             frame <= static_cast<int>(PreviewPresentationBuffer::kPrimeDepth);
             ++frame) {
            QVERIFY(buffer.push(imageWithMarker(qRgb(frame, 0, 0)), frame).accepted);
        }

        // Present at t=0, 16.7 and 33.4 ms while the source is between frames.
        // The fresh source frame arrives at 47 ms, before the next 50.1 ms
        // presentation tick. A three-frame prime keeps every tick supplied;
        // the former two-frame cushion reached empty on this measured cadence.
        for (int expected = 1; expected <= 3; ++expected) {
            auto next = buffer.take();
            QVERIFY(next.has_value());
            QCOMPARE(next->frameNumber, expected);
        }
        QVERIFY(buffer.empty());
        // Source delivery resumes at 47 ms, before the fourth presentation
        // deadline. That deadline still has a real newest frame to consume.
        QVERIFY(buffer.push(imageWithMarker(qRgb(4, 0, 0)), 4).accepted);
        auto afterBurst = buffer.take();
        QVERIFY(afterBurst.has_value());
        QCOMPARE(afterBurst->frameNumber, 4);
    }

    void recoveryReserveIsBoundedBelowInitialPrime()
    {
        QCOMPARE(PreviewPresentationBuffer::kPrimeDepth, std::size_t{3});
        QCOMPARE(PreviewPresentationBuffer::kRecoveryDepth, std::size_t{2});
        QVERIFY(PreviewPresentationBuffer::kRecoveryDepth
                < PreviewPresentationBuffer::kPrimeDepth);
        QVERIFY(PreviewPresentationBuffer::kPrimeDepth
                < PreviewPresentationBuffer::kCapacity);
    }

    void clearRemovesPriorSessionFrames()
    {
        PreviewPresentationBuffer buffer;
        QVERIFY(buffer.push(imageWithMarker(qRgb(1, 2, 3)), 7).accepted);
        buffer.clear();
        QVERIFY(buffer.empty());
        QVERIFY(!buffer.take().has_value());
    }

    void cadenceMatchesVideoRatesAndCapsExperimentalModes()
    {
        QCOMPARE(PreviewPresentationBuffer::integerPeriodForRequestedFps(60).count(),
                 16'666'667LL);
        QCOMPARE(PreviewPresentationBuffer::integerPeriodForRequestedFps(30).count(),
                 33'333'333LL);
        QCOMPARE(PreviewPresentationBuffer::periodForRequestedFps(60).count(), 16'683'333LL);
        QCOMPARE(PreviewPresentationBuffer::periodForRequestedFps(30).count(), 33'366'666LL);
        QCOMPARE(PreviewPresentationBuffer::periodForRequestedFps(120),
                 PreviewPresentationBuffer::periodForRequestedFps(60));
        QCOMPARE(PreviewPresentationBuffer::periodForRequestedFps(0),
                 PreviewPresentationBuffer::periodForRequestedFps(60));
    }

    void adaptiveCadenceLocksToTrueSixtyAndFractionalSixtyWithoutDriftDrops()
    {
        constexpr qint64 tenMinutesNs = 600'000'000'000LL;
        const auto trueSixty = simulatePresentationCadence({16'666'667LL}, tenMinutesNs);
        QCOMPARE(trueSixty.dropped, 0);
        QCOMPARE(trueSixty.underflows, 0);
        QVERIFY(trueSixty.maximumDepth <= PreviewPresentationBuffer::kCapacity);
        QCOMPARE(trueSixty.finalIntegralCorrectionNs, 0LL);

        const auto fractionalSixty = simulatePresentationCadence(
            {16'683'333LL}, tenMinutesNs);
        QCOMPARE(fractionalSixty.dropped, 0);
        QCOMPARE(fractionalSixty.underflows, 0);
        QVERIFY(fractionalSixty.maximumDepth <= PreviewPresentationBuffer::kCapacity);
        // The learned correction converges on the ~16.7 us difference rather
        // than repeatedly consuming and rebuilding an entire queued frame.
        QVERIFY(fractionalSixty.finalIntegralCorrectionNs >= 15'000LL);
        QVERIFY(fractionalSixty.finalIntegralCorrectionNs <= 19'000LL);
    }

    void sustainedMeasuredThirtyDowngradesARequestedSixtyWithoutTelemetryBursting()
    {
        PreviewPresentationBuffer::SourceRateClass rateClass;
        rateClass.reset(60);
        QCOMPARE(rateClass.displayFps(), 60);

        // Telemetry repeats on every detector payload. Copies inside the same
        // one-second source-rate window must not count as independent evidence.
        QVERIFY(!rateClass.observeCaptureFps(30, 0));
        QVERIFY(!rateClass.observeCaptureFps(30, 20));
        QVERIFY(!rateClass.observeCaptureFps(30, 740));
        QCOMPARE(rateClass.displayFps(), 60);

        QVERIFY(!rateClass.observeCaptureFps(31, 1'000));
        QVERIFY(rateClass.observeCaptureFps(29, 2'000));
        QCOMPARE(rateClass.displayFps(), 30);

        // A single high-rate window cannot flap the presenter back to 60.
        QVERIFY(!rateClass.observeCaptureFps(60, 3'000));
        QCOMPARE(rateClass.displayFps(), 30);
        QVERIFY(rateClass.observeCaptureFps(59, 4'000));
        QCOMPARE(rateClass.displayFps(), 60);
    }

    void severeOrIndeterminateCaptureCollapseDoesNotMasqueradeAsThirtyFps()
    {
        PreviewPresentationBuffer::SourceRateClass rateClass;
        rateClass.reset(60);
        for (int second = 0; second < 6; ++second) {
            const int observed = second % 2 == 0 ? 2 : 40;
            QVERIFY(!rateClass.observeCaptureFps(observed, second * 1'000LL));
        }
        QCOMPARE(rateClass.displayFps(), 60);

        // An explicit 30-FPS request remains a hard display ceiling even if the
        // transport later reports a higher delivery rate.
        rateClass.reset(30);
        QVERIFY(!rateClass.observeCaptureFps(60, 0));
        QVERIFY(!rateClass.observeCaptureFps(60, 1'000));
        QCOMPARE(rateClass.displayFps(), 30);
    }

    void adaptiveCadenceAbsorbsBurstyDeliveryAndRecoversItsReserveSmoothly()
    {
        constexpr qint64 tenMinutesNs = 600'000'000'000LL;
        // Each group retains the exact long-run source rate while injecting
        // the measured same-wake pair (0 ms), 32 ms delivery gap, and a rare
        // 47 ms gap followed by catch-up delivery.
        // First hold a clean source for ten seconds; the disturbance therefore
        // tests recovery from a settled clock rather than startup priming.
        std::vector<qint64> trueSixtyBurst(600, 16'666'667LL);
        std::vector<qint64> fractionalSixtyBurst(600, 16'683'333LL);
        for (int group = 0; group < 20; ++group) {
            trueSixtyBurst.insert(trueSixtyBurst.end(),
                                  {0LL, 32'000'000LL, 18'000'000LL});
            fractionalSixtyBurst.insert(fractionalSixtyBurst.end(),
                                        {0LL, 32'000'000LL, 18'050'000LL});
        }
        trueSixtyBurst.insert(trueSixtyBurst.end(),
                              {47'000'000LL, 3'000'000LL, 0LL});
        fractionalSixtyBurst.insert(fractionalSixtyBurst.end(),
                                    {47'000'000LL, 3'050'000LL, 0LL});

        for (const auto& result : {
                 simulatePresentationCadence(trueSixtyBurst, tenMinutesNs, 600),
                 simulatePresentationCadence(fractionalSixtyBurst, tenMinutesNs, 600)}) {
            QCOMPARE(result.dropped, 0);
            QCOMPARE(result.underflows, 0);
            QVERIFY(result.maximumDepth <= PreviewPresentationBuffer::kCapacity);
            QVERIFY(result.reserveFellBelowTarget);
            QVERIFY(result.reserveRecovered);
            // No catch-up burst or visible recovery stall: even under the
            // injected delivery pattern every requested interval stays within
            // 1.2% of 60 Hz.
            QVERIFY(result.minimumPeriodNs >= 16'466'667LL);
            QVERIFY(result.maximumPeriodNs <= 16'866'667LL);
        }
    }

    void absoluteCadenceRepaysOrdinaryTimerLatenessWithoutBursting()
    {
        constexpr qint64 period = 16'683'333;
        const auto ordinaryLate = PreviewPresentationBuffer::scheduleAfterTick(
            period, 17'200'000, period);
        QCOMPARE(ordinaryLate.deadlineNs, 2 * period);
        QCOMPARE(ordinaryLate.delayNs, 16'166'666);

        // A callback that missed more than one display opportunity advances to
        // the next future phase. It never asks the GUI thread to burst several
        // stale textures back-to-back.
        const auto longStall = PreviewPresentationBuffer::scheduleAfterTick(
            period, 70'000'000, period);
        QCOMPARE(longStall.deadlineNs, 5 * period);
        QCOMPARE(longStall.delayNs, 13'416'665);
    }

    void renderTickSelectionUsesHalfTheMeasuredVsyncInterval()
    {
        constexpr qint64 target60 = 16'666'667;
        // 60 Hz: the matching tick is accepted despite nanosecond rounding.
        QVERIFY(PreviewPresentationBuffer::renderTickIsDue(
            target60, 16'666'666, 16'666'666));

        // 120 Hz: the first half-frame is too early; the second is due.
        QVERIFY(!PreviewPresentationBuffer::renderTickIsDue(
            target60, 8'333'333, 8'333'333));
        QVERIFY(PreviewPresentationBuffer::renderTickIsDue(
            target60, 16'666'666, 8'333'333));

        // 144 Hz has no integral 60-Hz divisor. Select the nearest tick using
        // half a 6.94-ms render interval, not an 8.33-ms source tolerance.
        QVERIFY(PreviewPresentationBuffer::renderTickIsDue(
            target60, 13'888'888, 6'944'444));
        QVERIFY(!PreviewPresentationBuffer::renderTickIsDue(
            target60, 6'944'444, 6'944'444));
        QVERIFY(PreviewPresentationBuffer::renderTickIsDue(
            0, 1, 6'944'444));
    }

    void renderClockPacesThirtyAndSixtyAcrossCommonDisplayRates()
    {
        const auto simulate = [](qint64 renderPeriodNs, qint64 targetPeriodNs,
                                 int renderTicks) {
            std::vector<int> presentationTicks;
            qint64 deadlineNs = 0;
            qint64 nowNs = 0;
            for (int tick = 1; tick <= renderTicks; ++tick) {
                nowNs += renderPeriodNs;
                if (!PreviewPresentationBuffer::renderTickIsDue(
                        deadlineNs, nowNs, renderPeriodNs)) {
                    continue;
                }
                presentationTicks.push_back(tick);
                deadlineNs = PreviewPresentationBuffer::scheduleAfterTick(
                    deadlineNs, nowNs, targetPeriodNs).deadlineNs;
            }
            return presentationTicks;
        };

        const auto sixtyOnSixty = simulate(16'666'667, 16'666'667, 600);
        QCOMPARE(sixtyOnSixty.size(), std::size_t{600});
        for (std::size_t i = 1; i < sixtyOnSixty.size(); ++i) {
            QCOMPARE(sixtyOnSixty[i] - sixtyOnSixty[i - 1], 1);
        }

        const auto sixtyOnOneTwenty = simulate(8'333'333, 16'666'667, 1'200);
        QVERIFY(sixtyOnOneTwenty.size() >= std::size_t{599});
        QVERIFY(sixtyOnOneTwenty.size() <= std::size_t{601});
        for (std::size_t i = 1; i < sixtyOnOneTwenty.size(); ++i) {
            QCOMPARE(sixtyOnOneTwenty[i] - sixtyOnOneTwenty[i - 1], 2);
        }

        const auto sixtyOnOneFortyFour = simulate(6'944'444, 16'666'667, 1'440);
        QVERIFY(sixtyOnOneFortyFour.size() >= std::size_t{599});
        QVERIFY(sixtyOnOneFortyFour.size() <= std::size_t{601});
        for (std::size_t i = 1; i < sixtyOnOneFortyFour.size(); ++i) {
            const int gap = sixtyOnOneFortyFour[i] - sixtyOnOneFortyFour[i - 1];
            QVERIFY(gap == 2 || gap == 3);
        }

        const auto thirtyOnSixty = simulate(16'666'667, 33'333'333, 600);
        QVERIFY(thirtyOnSixty.size() >= std::size_t{299});
        QVERIFY(thirtyOnSixty.size() <= std::size_t{301});
        for (std::size_t i = 1; i < thirtyOnSixty.size(); ++i) {
            QCOMPARE(thirtyOnSixty[i] - thirtyOnSixty[i - 1], 2);
        }
    }

    void asyncProviderIdCannotResolveToANewerFrame()
    {
        RemoteFrameSnapshotStore snapshots;
        snapshots.reset(0, imageWithMarker(qRgb(0, 0, 0)));
        snapshots.publish(101, imageWithMarker(qRgb(101, 0, 0)));
        snapshots.publish(102, imageWithMarker(qRgb(102, 0, 0)));

        bool exact = false;
        const QImage requested101 = snapshots.lookupProviderId(
            QStringLiteral("live/101"), &exact);
        QVERIFY(exact);
        QCOMPARE(qRed(requested101.pixel(0, 0)), 101);

        const QImage requested102 = snapshots.lookupProviderId(
            QStringLiteral("live/102"), &exact);
        QVERIFY(exact);
        QCOMPARE(qRed(requested102.pixel(0, 0)), 102);
    }

    void delayedAsyncPresentationIsAcceptedMonotonically()
    {
        QCOMPARE(classifyRemoteFrameAck(101, 104, 100),
                 RemoteFrameAckDisposition::Accept);
        QCOMPARE(classifyRemoteFrameAck(104, 104, 101),
                 RemoteFrameAckDisposition::Accept);
        QCOMPARE(classifyRemoteFrameAck(104, 104, 104),
                 RemoteFrameAckDisposition::Duplicate);
        QCOMPARE(classifyRemoteFrameAck(103, 105, 104),
                 RemoteFrameAckDisposition::StaleOrFuture);
        QCOMPARE(classifyRemoteFrameAck(106, 105, 104),
                 RemoteFrameAckDisposition::StaleOrFuture);
    }

    void productionQmlTreeInstantiatesOnTheSupportedQtRuntime()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(qmlRoot + QStringLiteral("/Main.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        const auto errors = component.errors();
        QStringList errorLines;
        for (const QQmlError& error : errors) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> rootObject(component.create());
        QVERIFY2(rootObject != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));
    }

    void remoteStatusDebounceCannotBeStarvedByBroadNotifications()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        const auto errors = component.errors();
        QStringList errorLines;
        for (const QQmlError& error : errors) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));
        QCOMPARE(page->property("stableStatus").toString(), QStringLiteral("Smoke test"));

        const QVariant connecting = QStringLiteral("Connecting");
        for (int broadNotification = 0; broadNotification < 12; ++broadNotification) {
            QVERIFY(QMetaObject::invokeMethod(
                page.get(), "considerRemoteStatus", Q_ARG(QVariant, connecting)));
            QTest::qWait(40);
        }
        // The first genuine candidate change owns the 350 ms window. Repeated
        // broad notifications carrying that same value cannot restart/starve it.
        QCOMPARE(page->property("stableStatus").toString(),
                 QStringLiteral("Connecting"));

        const QVariant ready = QStringLiteral("Ready");
        QVERIFY(QMetaObject::invokeMethod(
            page.get(), "considerRemoteStatus", Q_ARG(QVariant, ready)));
        QTest::qWait(100);
        QVERIFY(QMetaObject::invokeMethod(
            page.get(), "considerRemoteStatus", Q_ARG(QVariant, connecting)));
        QTest::qWait(300);
        // Returning to the already displayed value cancels the pending candidate.
        QCOMPARE(page->property("stableStatus").toString(),
                 QStringLiteral("Connecting"));
    }

    // Retiring the advice strip must remove its live QML instances, not just hide text.
    void bannerVerdictTallyIsAbsentFromTheLivePage()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        // A part-filled window: four shots, 3 green + 1 late, the last one wide open. The
        // headline must say FOUR, never "last 10" -- a thin sample that reads as a full one
        // is exactly how a tuning session goes wrong.
        stub.insert(QStringLiteral("bannerCount10"), 4);
        stub.insert(QStringLiteral("bannerGreen10"), 3);
        stub.insert(QStringLiteral("bannerLate10"), 1);
        stub.insert(QStringLiteral("bannerPattern10"), QStringLiteral("gggl"));
        stub.insert(QStringLiteral("bannerLastTiming"), QStringLiteral("LATE"));
        stub.insert(QStringLiteral("bannerLastCoverage"), QStringLiteral("WIDE OPEN"));
        stub.insert(QStringLiteral("bannerSuggestion"),
                    QStringLiteral("collecting (5 shots minimum)"));
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        QStringList errorLines;
        for (const QQmlError& error : component.errors()) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));

        const QList<QObject*> mounts =
            page->findChildren<QObject*>(QStringLiteral("shotVerdictTally"));
        QCOMPARE(mounts.size(), 0);
        QVERIFY(page->findChild<QObject*>(QStringLiteral("releasePathSelector")) != nullptr);

    }

    // [ORION_BANNER_LEAD_TRIM 2026-09-15 owner] The closed loop's ONE line of UI, read back
    // through the real QML tree. The owner's slider value is never written by the loop, so the
    // card has to SAY what has been added on top of it -- and has to say nothing at all when
    // nothing has, or an untuned install would carry a permanent "+0 ms" nobody asked for.
    void shotLeadAutoTrimCaptionShowsTheBannerTrimAndHidesAtZero()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        stub.insert(QStringLiteral("bannerLeadTrimEnabled"), true);
        stub.insert(QStringLiteral("bannerLeadTrimMs"), 6.0);
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        QStringList errorLines;
        for (const QQmlError& error : component.errors()) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));

        QObject* caption =
            page->findChild<QObject*>(QStringLiteral("shotLeadAutoTrimCaption"));
        QVERIFY2(caption != nullptr,
                 "the Shot Lead card must carry the auto-trim caption");
        QCOMPARE(caption->property("text").toString(),
                 QStringLiteral("Auto-trim from game feedback: +6 ms"));
        QVERIFY(caption->property("visible").toBool());

        // A negative trim reads as a MINUS SIGN, not a hyphen: the card is the only place the
        // owner sees the loop's direction and it has to be unambiguous next to the number.
        stub.insert(QStringLiteral("bannerLeadTrimMs"), -4.0);
        QTRY_COMPARE(caption->property("text").toString(),
                     QStringLiteral("Auto-trim from game feedback: \u2212" "4 ms"));

        // Nothing earned, nothing said.
        stub.insert(QStringLiteral("bannerLeadTrimMs"), 0.0);
        QTRY_VERIFY(!caption->property("visible").toBool());
        // ...and the kill switch hides it whatever the stored trim is.
        stub.insert(QStringLiteral("bannerLeadTrimMs"), 9.0);
        stub.insert(QStringLiteral("bannerLeadTrimEnabled"), false);
        QTRY_VERIFY(!caption->property("visible").toBool());
    }

    // [ORION_LEAD_AUTO_SEED 2026-09-15 owner] "How will every user find their tip timing lead...
    // I'm trying to get it as plug and play as possible." An untuned install now flies this rig's
    // measured latency plus the shipped game-side aim margin, and the card has to SAY so: a lead
    // that moves on its own with no explanation is how a user concludes the app is broken. Read
    // back through the real QML tree, both phrasings, and hidden the moment the owner has a value
    // of their own.
    void shotLeadAutoSeedCaptionExplainsTheAutoValueAndHidesOnceUserSet()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        // Calibrating: no authoritative latency for this rig yet, so the shipped placeholder.
        stub.insert(QStringLiteral("leadAutoSeedActive"), true);
        stub.insert(QStringLiteral("leadAutoSeedKind"), QStringLiteral("placeholder"));
        stub.insert(QStringLiteral("leadAutoSeedMs"), 269.0);
        stub.insert(QStringLiteral("leadAutoSeedMeasuredMs"), 0.0);
        stub.insert(QStringLiteral("leadAutoSeedMarginMs"), 69.0);
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        QStringList errorLines;
        for (const QQmlError& error : component.errors()) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));

        QObject* caption =
            page->findChild<QObject*>(QStringLiteral("shotLeadAutoSeedCaption"));
        QVERIFY2(caption != nullptr,
                 "the Shot Lead card must carry the auto-seed caption");
        QCOMPARE(caption->property("text").toString(),
                 QStringLiteral("Auto: calibrating… using 269 ms until your latency is "
                                "measured"));
        QVERIFY(caption->property("visible").toBool());

        // Measured: the arithmetic is spelled out, because "277" on its own is a number the user
        // has no way to check and every reason to distrust.
        stub.insert(QStringLiteral("leadAutoSeedKind"), QStringLiteral("measured"));
        stub.insert(QStringLiteral("leadAutoSeedMeasuredMs"), 208.3);
        stub.insert(QStringLiteral("leadAutoSeedMs"), 277.3);
        QTRY_COMPARE(caption->property("text").toString(),
                     QStringLiteral("Auto: measured latency 208 ms + 69 ms margin = 277 ms"));
        QVERIFY(caption->property("visible").toBool());

        // The owner sets their own value: the seed goes inert and the caption disappears. From
        // then on the slider above is the whole answer.
        stub.insert(QStringLiteral("leadAutoSeedActive"), false);
        QTRY_VERIFY(!caption->property("visible").toBool());

        // The banner-trim caption is a SEPARATE line and must still be there alongside it --
        // the two say different things (what the app assumed vs what the game has since said).
        QObject* trimCaption =
            page->findChild<QObject*>(QStringLiteral("shotLeadAutoTrimCaption"));
        QVERIFY2(trimCaption != nullptr,
                 "the auto-seed caption must not have displaced the banner-trim caption");

        // Every objectName the card shipped with is still mounted.
        for (const char* name : {"shotLeadValue", "shotLeadStatePill", "shotLeadResetAction",
                                 "shotLeadSlider", "shotLeadDirectionHint",
                                 "shotLeadMaxUsableTick", "shotLeadConflictBanner",
                                 "shotLeadDisagreementBanner"}) {
            QVERIFY2(page->findChild<QObject*>(QLatin1String(name)) != nullptr, name);
        }
    }

    void captureCardFreshMeterOverlayIsVisibleAbovePreviewTextures()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        // This is the capture-card preview state from the user's report: no
        // Chiaki session is running, but a fresh authoritative detector sample
        // and its capture-frame box are available for the current video frame.
        stub.insert(QStringLiteral("capturePreviewActive"), true);
        // The overlay contract is independent of the preview clock. Keeping
        // the clock stopped makes this offscreen layout test deterministic.
        stub.insert(QStringLiteral("qmlRenderMode"), false);
        stub.insert(QStringLiteral("frameSerial"), 501);
        stub.insert(QStringLiteral("meterConfirmed"), true);
        stub.insert(QStringLiteral("liveFrameWidth"), 1280);
        stub.insert(QStringLiteral("liveFrameHeight"), 720);
        stub.insert(QStringLiteral("meterBoxX"), 1120);
        stub.insert(QStringLiteral("meterBoxY"), 586);
        stub.insert(QStringLiteral("meterBoxWidth"), 9);
        stub.insert(QStringLiteral("meterBoxHeight"), 64);
        stub.insert(QStringLiteral("meterMetricsValid"), true);
        stub.insert(QStringLiteral("shotFillPct"), 64.0);
        stub.insert(QStringLiteral("shotEtaToTargetMs"), 18.0);
        stub.insert(QStringLiteral("shotHoldMs"), 487.0);
        QCOMPARE(stub.value(QStringLiteral("qmlRenderMode")).toBool(), false);
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        const auto errors = component.errors();
        QStringList errorLines;
        for (const QQmlError& error : errors) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));
        auto* pageItem = qobject_cast<QQuickItem*>(page.get());
        QVERIFY(pageItem != nullptr);
        pageItem->setSize(QSizeF(1280, 900));
        // Make one preview texture the currently presented front buffer. The
        // overlay must remain effectively visible above that opaque texture.
        QVERIFY(pageItem->setProperty("previewFrontSlot", 0));
        QCoreApplication::processEvents();

        auto* previewA = page->findChild<QQuickItem*>(QStringLiteral("previewImageA"));
        auto* previewB = page->findChild<QQuickItem*>(QStringLiteral("previewImageB"));
        auto* captureHost = page->findChild<QQuickItem*>(QStringLiteral("captureHost"));
        auto* layer = page->findChild<QQuickItem*>(QStringLiteral("meterDebugLayer"));
        auto* lock = page->findChild<QQuickItem*>(QStringLiteral("meterLockBox"));
        auto* telemetryHud = page->findChild<QQuickItem*>(
            QStringLiteral("meterTelemetryHud"));
        auto* metricsOverlay = page->findChild<QQuickItem*>(
            QStringLiteral("liveMeterMetricsOverlay"));
        auto* etaMetric = page->findChild<QQuickItem*>(QStringLiteral("meterEtaMetric"));
        auto* holdMetric = page->findChild<QQuickItem*>(QStringLiteral("meterHoldMetric"));
        QVERIFY(previewA != nullptr);
        QVERIFY(previewB != nullptr);
        QVERIFY(captureHost != nullptr);
        QVERIFY(layer != nullptr);
        QVERIFY(lock != nullptr);
        QVERIFY(telemetryHud != nullptr);
        QVERIFY(metricsOverlay != nullptr);
        QVERIFY(etaMetric != nullptr);
        QVERIFY(holdMetric != nullptr);
        // The old opaque/boxed companion card was intentionally removed. The
        // live values are now bare capture-corner text and cannot be confused
        // with a detector-attached geometry box.
        QVERIFY(page->findChild<QQuickItem*>(QStringLiteral("meterInfoHud")) == nullptr);

        // QQuickLayout performs its final allocation only after an item enters
        // a scene. Attaching this test page to QQuickWindow would start the
        // production FrameAnimation and turn an otherwise deterministic unit
        // test into a render-loop lifetime test. Give the already-discovered
        // production capture host the same concrete viewport allocation
        // directly; anchors below it still execute exactly as they do live.
        captureHost->setSize(QSizeF(960.0, 540.0));
        QCoreApplication::processEvents();

        for (int attempt = 0;
             attempt < 100 && (layer->width() <= 0.0 || layer->height() <= 0.0);
             ++attempt) {
            QTest::qWait(10);
        }
        const QString geometry = QStringLiteral(
                                     "page=%1x%2 captureHost=%3x%4 layer=%5x%6")
                                     .arg(pageItem->width())
                                     .arg(pageItem->height())
                                     .arg(captureHost->width())
                                     .arg(captureHost->height())
                                     .arg(layer->width())
                                     .arg(layer->height());
        QVERIFY2(layer->width() > 0.0 && layer->height() > 0.0,
                 qPrintable(geometry));
        QVERIFY(previewA->isVisible());
        QCOMPARE(previewA->opacity(), 1.0);
        QVERIFY(layer->isVisible());
        QVERIFY(lock->isVisible());
        // The attached reference-style instrument is primary while a lock is
        // visible; the optional ETA/HOLD corner readout must not duplicate it.
        QVERIFY(telemetryHud->isVisible());
        QVERIFY(!metricsOverlay->isVisible());
        QCOMPARE(etaMetric->property("text").toString(), QString{});
        QCOMPARE(holdMetric->property("text").toString(), QString{});

        // The distant 9x64 capture box is uniformly aspect-fit into the live
        // image. Width and height use the same scale, so the meter cannot be
        // stretched into a horizontal HUD-like rectangle.
        const double drawScale = layer->property("drawScale").toDouble();
        QVERIFY(drawScale > 0.0);
        QCOMPARE(lock->width(), 9.0 * drawScale);
        QCOMPARE(lock->height(), 64.0 * drawScale);
        QVERIFY(lock->height() > lock->width());

        // The reference treatment is compact, unboxed text above the lock.
        // There is no Rectangle/card surface, and placement stays inside the
        // preview while sharing the exact joined box's coordinate clock.
        QVERIFY(!telemetryHud->property("color").isValid());
        QVERIFY(telemetryHud->property("fitsAbove").toBool());
        QVERIFY(telemetryHud->y() + telemetryHud->height()
                <= telemetryHud->property("lockTop").toReal());
        QVERIFY(telemetryHud->x()
                >= telemetryHud->property("layerLeft").toReal());
        QVERIFY(telemetryHud->x() + telemetryHud->width()
                <= telemetryHud->property("layerRight").toReal());
        QCOMPARE(telemetryHud->width(), 76.0);
        const qreal telemetryXBeforeDigits = telemetryHud->x();
        stub.insert(QStringLiteral("meterHudMeasured"), true);
        stub.insert(QStringLiteral("meterHudLive"), true);
        stub.insert(QStringLiteral("meterHudFillLine"), QStringLiteral("8%"));
        stub.insert(QStringLiteral("meterHudTipLine"), QStringLiteral("9ms"));
        stub.insert(QStringLiteral("meterHudFireLine"), QStringLiteral("-148ms"));
        QCoreApplication::processEvents();
        QCOMPARE(telemetryHud->width(), 76.0);
        QCOMPARE(telemetryHud->x(), telemetryXBeforeDigits);

        // When the lock leaves, the corner readout may take over without
        // overlapping the primary instrument. The user toggle remains
        // presentation-only and cannot mutate either native metric value.
        stub.insert(QStringLiteral("meterConfirmed"), false);
        QCoreApplication::processEvents();
        QVERIFY(!layer->isVisible());
        QVERIFY(!telemetryHud->isVisible());
        QVERIFY(metricsOverlay->isVisible());
        QCOMPARE(etaMetric->property("text").toString(), QStringLiteral("ETA  18 ms"));
        QCOMPARE(holdMetric->property("text").toString(), QStringLiteral("HOLD 487 ms"));
        QVERIFY(etaMetric->isVisible());
        QVERIFY(holdMetric->isVisible());
        stub.insert(QStringLiteral("showLiveMeterMetrics"), false);
        QCoreApplication::processEvents();
        QVERIFY(!metricsOverlay->isVisible());
        QVERIFY(!layer->isVisible());
        QCOMPARE(stub.value(QStringLiteral("shotEtaToTargetMs")).toDouble(), 18.0);
        QCOMPARE(stub.value(QStringLiteral("shotHoldMs")).toDouble(), 487.0);
        stub.insert(QStringLiteral("showLiveMeterMetrics"), true);
        QCoreApplication::processEvents();
        QVERIFY(metricsOverlay->isVisible());

        // Unavailable native getters are sentinels, not display values. No zero,
        // dash, placeholder, or stale number remains on the capture.
        stub.insert(QStringLiteral("shotEtaToTargetMs"), -1.0);
        stub.insert(QStringLiteral("shotHoldMs"), -1.0);
        QCoreApplication::processEvents();
        QCOMPARE(etaMetric->property("text").toString(), QString{});
        QCOMPARE(holdMetric->property("text").toString(), QString{});
        QVERIFY(!etaMetric->isVisible());
        QVERIFY(!holdMetric->isVisible());
        QVERIFY(!metricsOverlay->isVisible());

        // The controller tick follows the detector callback. Once its real,
        // target-matched ETA becomes available, QML must leave standby without
        // requiring another detector-side notification or inventing a default.
        stub.insert(QStringLiteral("shotEtaToTargetMs"), 14.0);
        stub.insert(QStringLiteral("shotHoldMs"), 491.0);
        QCoreApplication::processEvents();
        QCOMPARE(etaMetric->property("text").toString(), QStringLiteral("ETA  14 ms"));
        QCOMPARE(holdMetric->property("text").toString(), QStringLiteral("HOLD 491 ms"));
        QVERIFY(metricsOverlay->isVisible());
        QVERIFY(etaMetric->isVisible());
        QVERIFY(holdMetric->isVisible());

        // z is evaluated between siblings: the detector layer must composite
        // after both possible front/back preview textures.
        QVERIFY(layer->z() > previewA->z());
        QVERIFY(layer->z() > previewB->z());
    }

    // 2026-08-31 display work: (1) the factory lock colour is the shipped blue
    // and the QML stroke resolves it purely from the controller-published draw
    // colour; (2) the meter-side HUD's five rows (FILL/TIP/FIRE + the new
    // COURT/JITTER) surface the native pre-formatted strings verbatim — no
    // QML-side formatting, no substituted values, "--" only when native says so.
    void meterHudRowsSurfaceNativeStringsAndTheBlueDefaultLock()
    {
        // Data-side default: the stroke the app ships with. Since 2026-08-06
        // (owner: colour customisation removed entirely) the loader pins EVERY
        // persisted meter_overlay_color / meter_overlay_rgb back to this
        // default; the legacy violet stays pinned as a named constant so the
        // migration history remains asserted. 2026-09-14: magenta #FF2BD6 ->
        // blue #1E90FF (owner: blue, visible on light and dark). Theme.meterLock
        // carries the same hex on the QML side.
        QCOMPARE(AppConfigData{}.meterOverlayColor, QStringLiteral("#1E90FF"));
        QCOMPARE(QString::fromLatin1(AppConfigData::kMeterOverlayLegacyDefaultColor),
                 QStringLiteral("#CC44FF"));

        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        stub.insert(QStringLiteral("capturePreviewActive"), true);
        stub.insert(QStringLiteral("qmlRenderMode"), false);
        stub.insert(QStringLiteral("meterConfirmed"), true);
        stub.insert(QStringLiteral("liveFrameWidth"), 1280);
        stub.insert(QStringLiteral("liveFrameHeight"), 720);
        stub.insert(QStringLiteral("meterBoxX"), 1120);
        stub.insert(QStringLiteral("meterBoxY"), 586);
        stub.insert(QStringLiteral("meterBoxWidth"), 9);
        stub.insert(QStringLiteral("meterBoxHeight"), 64);
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        const auto errors = component.errors();
        QStringList errorLines;
        for (const QQmlError& error : errors) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));

        // (1) The lock stroke is exactly the controller-published draw colour —
        // the stub carries the production default, so this pins the shipped
        // blue end-to-end without any QML-side colour math.
        auto* lockFrame =
            page->findChild<QQuickItem*>(QStringLiteral("meterLockFrame"));
        QVERIFY(lockFrame != nullptr);
        auto* lockBox =
            page->findChild<QQuickItem*>(QStringLiteral("meterLockBox"));
        auto* outerFrame =
            page->findChild<QQuickItem*>(QStringLiteral("meterLockOuterFrame"));
        QVERIFY(lockBox != nullptr);
        QVERIFY(outerFrame != nullptr);
        // The clean lock is one blue stroke plus one restrained contrast
        // keyline. The former third/inner ring made a tiny meter look cluttered.
        QVERIFY(page->findChild<QQuickItem*>(QStringLiteral("meterLockInnerFrame"))
                == nullptr);
        QCOMPARE(lockBox->property("airGap").toReal(), 1.0);
        QCOMPARE(lockBox->property("strokeW").toReal(), 2.0);
        QCOMPARE(lockBox->property("frameInset").toReal(), 4.0);
        QCOMPARE(outerFrame->property("opacity").toReal(), 0.68);
        QCOMPARE(lockFrame->property("opacity").toReal(), 0.96);
        QObject* border = qvariant_cast<QObject*>(lockFrame->property("border"));
        QVERIFY(border != nullptr);
        QCOMPARE(border->property("color").value<QColor>(),
                 QColor(QStringLiteral("#1E90FF")));

        // (2) EXACTLY the three rows exist, and they start on native's own
        // placeholder. COURT/JITTER were removed 2026-08-06 on owner direction
        // ("it only needs 3 values … the 3 values that the bot uses"): the two
        // network rows essentially never changed, which is what made the whole
        // plate read as static. The negative asserts pin the removal so the
        // rows cannot quietly return.
        auto* fillRow = page->findChild<QQuickItem*>(QStringLiteral("meterHudFillRow"));
        auto* tipRow = page->findChild<QQuickItem*>(QStringLiteral("meterHudTipRow"));
        auto* fireRow = page->findChild<QQuickItem*>(QStringLiteral("meterHudFireRow"));
        QVERIFY(fillRow != nullptr);
        QVERIFY(tipRow != nullptr);
        QVERIFY(fireRow != nullptr);
        QVERIFY(page->findChild<QQuickItem*>(QStringLiteral("meterHudCourtRow"))
                == nullptr);
        QVERIFY(page->findChild<QQuickItem*>(QStringLiteral("meterHudJitterRow"))
                == nullptr);

        // Native publishes real strings -> the rows show them verbatim.
        stub.insert(QStringLiteral("meterHudMeasured"), true);
        stub.insert(QStringLiteral("meterHudLive"), true);
        stub.insert(QStringLiteral("meterHudFillLine"), QStringLiteral("64%"));
        stub.insert(QStringLiteral("meterHudTipLine"), QStringLiteral("148ms"));
        stub.insert(QStringLiteral("meterHudFireLine"), QStringLiteral("73ms"));
        QCoreApplication::processEvents();
        QCOMPARE(fillRow->property("value").toString(), QStringLiteral("64%"));
        QCOMPARE(tipRow->property("value").toString(), QStringLiteral("148ms"));
        QCOMPARE(fireRow->property("value").toString(), QStringLiteral("73ms"));

        // A live countdown streams through the SAME line objects — the rows
        // must track a per-publish value change (FIRE counting down through
        // zero into the late/negative state), not present a latched string.
        stub.insert(QStringLiteral("meterHudFireLine"), QStringLiteral("12ms"));
        QCoreApplication::processEvents();
        QCOMPARE(fireRow->property("value").toString(), QStringLiteral("12ms"));
        stub.insert(QStringLiteral("meterHudFireLine"), QStringLiteral("-14ms"));
        stub.insert(QStringLiteral("meterHudCommandLate"), true);
        QCoreApplication::processEvents();
        QCOMPARE(fireRow->property("value").toString(), QStringLiteral("-14ms"));

        // Losing the shot demotes to the placeholder, never to an empty
        // string (an empty row would collapse the plate).
        stub.insert(QStringLiteral("meterHudTipLine"), QStringLiteral("--"));
        stub.insert(QStringLiteral("meterHudFireLine"), QStringLiteral("--"));
        QCoreApplication::processEvents();
        QCOMPARE(tipRow->property("value").toString(), QStringLiteral("--"));
        QCOMPARE(fireRow->property("value").toString(), QStringLiteral("--"));
    }

    void licenceStripDegradesWithoutABackendProfileAndCountsDaysWhenKnown()
    {
        // [PROFILE TAB -> LICENCE STRIP 2026-09-15 owner: "remove the profile tab,
        // license info and days left should be displayed on the bottom left corner"]
        // The strip is now on EVERY page, so it must instantiate on the shipped Qt
        // runtime -- and it must do so against the CURRENT live backend, which sends
        // no profile block. In that state the day line reads "Not reported", never
        // "0 days left" (which a customer would read as "expired today").
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/components/Sidebar.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        QStringList errorLines;
        for (const QQmlError& error : component.errors()) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> sidebar(component.create());
        QVERIFY2(sidebar != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));

        auto* strip = sidebar->findChild<QQuickItem*>(QStringLiteral("licenseStrip"));
        QVERIFY(strip != nullptr);
        auto* stateLine = sidebar->findChild<QQuickItem*>(
            QStringLiteral("licenseStripState"));
        QVERIFY(stateLine != nullptr);
        auto* daysLine = sidebar->findChild<QQuickItem*>(
            QStringLiteral("licenseStripDays"));
        QVERIFY(daysLine != nullptr);
        // The flyout is a Popup (not a QQuickItem), so it is looked up as a QObject.
        QVERIFY(sidebar->findChild<QObject*>(QStringLiteral("licenseFlyout")) != nullptr);

        // No profile block: a verified key with no plan, and an honestly unknown expiry.
        QCOMPARE(stateLine->property("text").toString(), QStringLiteral("Active"));
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("Not reported"));
        QVERIFY(!sidebar->property("licenseExpiringSoon").toBool());

        // A real backend profile fills both lines in place.
        stub.insert(QStringLiteral("profileKnown"), true);
        stub.insert(QStringLiteral("profilePlan"), QStringLiteral("1 Month"));
        stub.insert(QStringLiteral("profileDaysLeft"), 27);
        QCoreApplication::processEvents();
        QCOMPARE(stateLine->property("text").toString(),
                 QStringLiteral("Active \u00B7 1 Month"));
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("27 days left"));
        QVERIFY(!sidebar->property("licenseExpiringSoon").toBool());

        // The last three days take the subtle warning tone.
        stub.insert(QStringLiteral("profileDaysLeft"), 3);
        QCoreApplication::processEvents();
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("3 days left"));
        QVERIFY(sidebar->property("licenseExpiringSoon").toBool());
        // The tone crossfades (Behavior on color), so let the animation land.
        QTRY_COMPARE_WITH_TIMEOUT(
            qvariant_cast<QColor>(daysLine->property("color")),
            QColor(QStringLiteral("#F59E0B")), 3000);

        // 1 == less than a day of runway; 0 == the expiry already passed.
        stub.insert(QStringLiteral("profileDaysLeft"), 1);
        QCoreApplication::processEvents();
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("Expires today"));
        stub.insert(QStringLiteral("profileDaysLeft"), 0);
        QCoreApplication::processEvents();
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("Expired"));

        // Lifetime is a word on BOTH lines, never a zero-day countdown.
        stub.insert(QStringLiteral("profileLifetime"), true);
        stub.insert(QStringLiteral("profileDaysLeft"), -2);
        QCoreApplication::processEvents();
        QCOMPARE(stateLine->property("text").toString(), QStringLiteral("Lifetime"));
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("Lifetime"));
        QVERIFY(!sidebar->property("licenseExpiringSoon").toBool());

        // An unactivated launcher says so rather than claiming a plan.
        stub.insert(QStringLiteral("profileLifetime"), false);
        stub.insert(QStringLiteral("profilePlan"), QString{});
        stub.insert(QStringLiteral("profileKnown"), false);
        stub.insert(QStringLiteral("profileDaysLeft"), -1);
        stub.insert(QStringLiteral("licenseState"), QStringLiteral("Locked"));
        QCoreApplication::processEvents();
        QCOMPARE(stateLine->property("text").toString(), QStringLiteral("Not activated"));
        QCOMPARE(daysLine->property("text").toString(), QStringLiteral("Not reported"));
    }

    void staleProfileRouteFallsThroughToLiveAndTheStripReplacesTheTab()
    {
        // [PROFILE TAB REMOVED 2026-09-15 owner] A settings.json that still carries
        // current_page="profile" must land on Live exactly the way a stale "general"
        // does -- never on a blank panel from a null component.
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        stub.insert(QStringLiteral("currentPage"), QStringLiteral("profile"));
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(qmlRoot + QStringLiteral("/Main.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        QStringList errorLines;
        for (const QQmlError& error : component.errors()) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> rootObject(component.create());
        QVERIFY2(rootObject != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));
        QCoreApplication::processEvents();

        // The page is gone from the tree AND from the source tree.
        QVERIFY(rootObject->findChild<QQuickItem*>(QStringLiteral("profilePage"))
                == nullptr);
        QVERIFY(!QFile::exists(qmlRoot + QStringLiteral("/pages/ProfilePage.qml")));

        // The licence strip instantiates inside the real shell, on every page.
        QVERIFY(rootObject->findChild<QQuickItem*>(QStringLiteral("licenseStrip"))
                != nullptr);
        QVERIFY(rootObject->findChild<QQuickItem*>(QStringLiteral("licenseStripState"))
                != nullptr);
        QVERIFY(rootObject->findChild<QQuickItem*>(QStringLiteral("licenseStripDays"))
                != nullptr);

        // Nothing routes to "profile", and the nav rail no longer lists it.
        QFile shell(qmlRoot + QStringLiteral("/components/AppShell.qml"));
        QVERIFY2(shell.open(QIODevice::ReadOnly | QIODevice::Text),
                 qPrintable(shell.errorString()));
        const QByteArray shellSource = shell.readAll();
        QVERIFY(!shellSource.contains("ProfilePage"));
        QVERIFY(!shellSource.contains("currentPage === \"profile\""));
        QVERIFY(!shellSource.contains("currentPage !== \"profile\""));

        QFile rail(qmlRoot + QStringLiteral("/components/Sidebar.qml"));
        QVERIFY2(rail.open(QIODevice::ReadOnly | QIODevice::Text),
                 qPrintable(rail.errorString()));
        const QByteArray railSource = rail.readAll();
        QVERIFY(!railSource.contains("key: \"profile\""));
        QVERIFY(railSource.contains("objectName: \"licenseStrip\""));
        QVERIFY(railSource.contains("objectName: \"licenseFlyout\""));

        // Setup still does not carry the Account card the Profile page replaced.
        QFile dashboard(qmlRoot + QStringLiteral("/pages/DashboardPage.qml"));
        QVERIFY2(dashboard.open(QIODevice::ReadOnly | QIODevice::Text),
                 qPrintable(dashboard.errorString()));
        const QByteArray setup = dashboard.readAll();
        QVERIFY(!setup.contains("title: \"Account\""));
        QVERIFY(!setup.contains("orion.copyLicenseKey()"));
        QVERIFY(!setup.contains("orion.licenseKeyMasked"));
    }

    void productionSetupHasNoManualTimingOrNetworkTelemetrySurface()
    {
        QFile dashboard(QStringLiteral(ORION_QML_SOURCE_DIR)
                        + QStringLiteral("/pages/DashboardPage.qml"));
        QVERIFY2(dashboard.open(QIODevice::ReadOnly | QIODevice::Text),
                 qPrintable(dashboard.errorString()));
        const QByteArray source = dashboard.readAll();

        // [2026-09-14 UI REVAMP] The passive "timing is off" pill is GONE from the
        // Dashboard (and its Live-page twin, liveTimingStatus, with it): the revamped
        // shell has a single Connect action and no preview-versus-live status chrome,
        // so a pill explaining a state the user can no longer be parked in was pure
        // noise. Asserted as an ABSENCE so it cannot quietly return — what this test
        // actually guards is that no manual-timing or network-telemetry surface exists
        // on the production setup page, and a removed pill satisfies that strictly
        // more than a present one did.
        QVERIFY(!source.contains("passiveTimingStatus"));
        QVERIFY(!source.contains("liveTimingStatus"));
        QVERIFY(!source.contains("PREVIEW ONLY"));
        QVERIFY(!source.contains("Enable Bot"));
        QVERIFY(!source.contains("timingSetupAction"));
        QVERIFY(!source.contains("startLatencyCalibration"));
        QVERIFY(!source.contains("networkDash"));
        QVERIFY(!source.contains("packetCaptureOptIn"));
        QVERIFY(!source.contains("Court Path"));
    }

    void liveActivityModelHandlesRotationDuplicatesClearAndTelemetryOnlyUpdates()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);

        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        // [ORION_ACTIVITY_FEED 2026-09-14] The live Activity panel reads the
        // CUSTOMER ring (orion.activityText): the native rule
        // (ui_notifications::shouldEnterActivityRing) has already removed the
        // engineering telemetry. orion.logText stays the RAW ring the Debug
        // page shows, so the QML pass below is only the Live-page filter.
        stub.insert(QStringLiteral("activityText"), QStringLiteral(
            "09:00:00  Connected\n09:00:01  Meter found\n09:00:01  Meter found"));
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);

        QQmlComponent component(
            &engine, QUrl::fromLocalFile(
                         qmlRoot + QStringLiteral("/pages/RemotePlayPage.qml")));
        if (component.isLoading()) {
            QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        }
        const auto errors = component.errors();
        QStringList errorLines;
        for (const QQmlError& error : errors) {
            errorLines.append(error.toString());
        }
        std::unique_ptr<QObject> page(component.create());
        QVERIFY2(page != nullptr, qPrintable(errorLines.join(QLatin1Char('\n'))));
        QCOMPARE(page->property("captureLogLines").toInt(), 3);

        auto* model = page->findChild<QAbstractItemModel*>(QStringLiteral("captureLogModel"));
        QVERIFY(model != nullptr);
        QSignalSpy inserted(model, &QAbstractItemModel::rowsInserted);
        QSignalSpy removed(model, &QAbstractItemModel::rowsRemoved);
        const auto syncLogModel = [&page]() {
            return QMetaObject::invokeMethod(page.get(), "syncCaptureLogModel");
        };

        // Periodic transport diagnostics still reach the disk-backed native log,
        // but must not mutate/re-layout the live activity model.
        stub.insert(QStringLiteral("activityText"), QStringLiteral(
            "09:00:00  Connected\n09:00:01  Meter found\n09:00:01  Meter found\n"
            "09:00:05  qml_preview_pipeline: set_fps=60.0\n"
            "09:00:05  preview_pipeline: transport=shm present_fps=60.0\n"
            "09:00:05  Sidecar: preview_stats: fps=60.0\n"
            "09:00:05  Capture health: tier=capture_card\n"
            "09:00:05  SHM preview frame read: count=300"));
        QVERIFY(syncLogModel());
        QCoreApplication::processEvents();
        QCOMPARE(page->property("captureLogLines").toInt(), 3);
        QCOMPARE(inserted.count(), 0);
        QCOMPARE(removed.count(), 0);

        // Ring-prefix loss plus one new line is one incremental remove/append;
        // identical adjacent lines remain distinct rows.
        stub.insert(QStringLiteral("activityText"), QStringLiteral(
            "09:00:01  Meter found\n09:00:01  Meter found\n09:00:06  Shot owned"));
        QVERIFY(syncLogModel());
        QCoreApplication::processEvents();
        QCOMPARE(page->property("captureLogLines").toInt(), 3);
        const int lineRole = model->roleNames().key(QByteArrayLiteral("line"), -1);
        QVERIFY(lineRole >= 0);
        QCOMPARE(model->data(model->index(0, 0), lineRole).toString(),
                 QStringLiteral("09:00:01  Meter found"));
        QCOMPARE(model->data(model->index(1, 0), lineRole).toString(),
                 QStringLiteral("09:00:01  Meter found"));
        QCOMPARE(model->data(model->index(2, 0), lineRole).toString(),
                 QStringLiteral("09:00:06  Shot owned"));

        stub.insert(QStringLiteral("activityText"), QString{});
        QVERIFY(syncLogModel());
        QCoreApplication::processEvents();
        QCOMPARE(page->property("captureLogLines").toInt(), 0);

        // A fresh page reconstructs the bounded view from current native state.
        stub.insert(QStringLiteral("activityText"), QStringLiteral("09:01:00  Reloaded"));
        std::unique_ptr<QObject> reloaded(component.create());
        QVERIFY(reloaded != nullptr);
        QCOMPARE(reloaded->property("captureLogLines").toInt(), 1);
    }

    void snapshotHistoryIsBoundedAndMissingIdsNeverAliasLatest()
    {
        RemoteFrameSnapshotStore snapshots;
        const QRgb fallbackMarker = qRgb(7, 8, 9);
        snapshots.reset(0, imageWithMarker(fallbackMarker));
        for (int serial = 1;
             serial <= static_cast<int>(RemoteFrameSnapshotStore::kCapacity) + 2;
             ++serial) {
            snapshots.publish(serial, imageWithMarker(qRgb(serial, 0, 0)));
        }
        QCOMPARE(snapshots.size(), RemoteFrameSnapshotStore::kCapacity);

        bool exact = true;
        const QImage evicted = snapshots.lookupProviderId(QStringLiteral("live/1"), &exact);
        QVERIFY(!exact);
        QCOMPARE(evicted.pixel(0, 0), fallbackMarker);

        // The old single-latest provider returned the newest pixels here. The
        // fallback proves an evicted/invalid id can never impersonate a later frame.
        const QImage malformed = snapshots.lookupProviderId(
            QStringLiteral("live/not-a-serial"), &exact);
        QVERIFY(!exact);
        QCOMPARE(malformed.pixel(0, 0), fallbackMarker);
    }

    void overlaySnapshotWaitsForItsExactPresentedSerial()
    {
        RemoteFrameOverlaySnapshotStore overlays;
        const RemoteFrameOverlaySnapshot frame101{
            101, QRect(10, 20, 30, 40), QRect(1, 2, 3, 4), true, QSize(1280, 720)};
        const RemoteFrameOverlaySnapshot frame102{
            102, QRect(50, 60, 30, 40), {}, false, QSize(960, 540)};
        overlays.publish(frame101);
        overlays.publish(frame102);

        // Image 101 may reach Ready after 102 has already been queued. Its
        // keyed overlay remains exact and cannot silently resolve to 102.
        const auto ready101 = overlays.lookup(101);
        QVERIFY(ready101.has_value());
        QCOMPARE(ready101->meterBox, frame101.meterBox);
        QCOMPARE(ready101->rejectedBox, frame101.rejectedBox);
        QCOMPARE(ready101->meterConfirmed, true);
        QCOMPARE(ready101->frameSize, QSize(1280, 720));

        const auto ready102 = overlays.lookup(102);
        QVERIFY(ready102.has_value());
        QCOMPARE(ready102->meterBox, frame102.meterBox);
        QCOMPARE(ready102->meterConfirmed, false);
        QCOMPARE(ready102->frameSize, QSize(960, 540));
        QVERIFY(!overlays.lookup(99).has_value());
    }

    void meterOverlayJoinMissHoldsOnlyWhileDetectorIsRecent()
    {
        QCOMPARE(meterOverlayBoxAction(true, true),
                 MeterOverlayBoxAction::UpdateFromJoinedBox);
        QCOMPARE(meterOverlayBoxAction(true, false),
                 MeterOverlayBoxAction::HoldLastJoinedBox);
        QCOMPARE(meterOverlayBoxAction(false, true),
                 MeterOverlayBoxAction::Clear);
        QCOMPARE(meterOverlayBoxAction(false, false),
                 MeterOverlayBoxAction::Clear);
    }

    void meterOverlayVisualLeaseBridgesMeasuredDetectorCadenceOnly()
    {
        const qint64 seenAt = 1000;

        // The supplied trace's ordinary p90 and worst observed gaps remain
        // visible even though they exceed the separate 120ms timing TTL.
        QVERIFY(meterOverlayVisualRecent(true, seenAt, seenAt + 167));
        QVERIFY(meterOverlayVisualRecent(true, seenAt, seenAt + 299));

        // After the shot ends, the outline needs only the measured p90 plus
        // bounded presentation slack. It must not inherit the full
        // in-animation continuity TTL.
        QVERIFY(meterOverlayVisualRecent(
            true, seenAt, seenAt + 199, false));
        QVERIFY(!meterOverlayVisualRecent(
            true, seenAt, seenAt + kMeterOverlayIdleVisualFreshMs, false));

        // The lease is bounded and fails closed with runtime authority.
        QVERIFY(!meterOverlayVisualRecent(
            true, seenAt, seenAt + kMeterOverlayVisualFreshMs));
        QVERIFY(!meterOverlayVisualRecent(false, seenAt, seenAt + 1));
        QVERIFY(!meterOverlayVisualRecent(true, 0, seenAt + 1));
        QVERIFY(!meterOverlayVisualRecent(true, seenAt, seenAt - 1));
    }

    void meterOverlayRejectsEmptyEchoesAndUnstructuredIdleTeleports()
    {
        DetectionResult sample;
        sample.detected = true;
        sample.frameAgeMs = 10.0;
        sample.stage = QStringLiteral("track");
        sample.rejectionReason.clear();
        sample.x = 500;
        sample.y = 240;
        sample.width = 24;
        sample.height = 112;
        sample.fillPct = 0.0;
        sample.greenStartPct = -1.0;
        sample.greenEndPct = -1.0;
        sample.fillEstimatorMode = QStringLiteral("none");
        sample.fillEstimatorGeneration = 0;

        // The two observed post-shot payloads (detected=1/fill=0/no ruler/no
        // green) may not renew the presentation lease. During an active shot,
        // the same low-fill observation remains visible and cannot cause blink.
        QVERIFY(!isLiveMeterOverlayVisualEvidence(sample, 50.0, false));
        QVERIFY(isLiveMeterOverlayVisualEvidence(sample, 50.0, true));
        sample.gameplayStructureVerified = true;
        sample.gameplayStructureEpoch = 91;
        // A structure bit may arrive on a trailing post-shot echo; outside the
        // active shot it does not turn zero visual content back into a meter.
        QVERIFY(!isLiveMeterOverlayVisualEvidence(sample, 50.0, false));
        sample.fillPct = 18.0;
        QVERIFY(isLiveMeterOverlayVisualEvidence(sample, 50.0, false));

        constexpr quint64 epoch = 91;
        const QRect prior(498, 238, 24, 112);
        // A fresh box cannot appear in a menu or on the court without current
        // shot-specific structure proof.
        sample.gameplayStructureVerified = false;
        sample.gameplayStructureEpoch = 0;
        QVERIFY(!meterOverlayMayAcquireOrContinue(
            sample, epoch, false, {}));
        sample.gameplayStructureVerified = true;
        sample.gameplayStructureEpoch = epoch;
        QVERIFY(meterOverlayMayAcquireOrContinue(
            sample, epoch, false, {}));
        sample.gameplayStructureVerified = false;
        sample.gameplayStructureEpoch = 0;

        // Once established, exact joined frames may follow the same identity,
        // but not teleport to a distant court mark or stretch into a new shape.
        QVERIFY(meterOverlayMayAcquireOrContinue(
            sample, epoch, true, prior));

        // Moving/fade continuation compares each exact joined observation to
        // the last accepted one. It follows the meter without lag or a
        // hardcoded court region, and does not need renewed structure each frame.
        QRect movingPrior = prior;
        for (int i = 0; i < 12; ++i) {
            sample.x = movingPrior.x() + 9;
            sample.y = movingPrior.y() + 3;
            sample.width = 24;
            sample.height = 112;
            QVERIFY(meterOverlayMayAcquireOrContinue(
                sample, epoch, true, movingPrior));
            movingPrior = QRect(
                sample.x, sample.y, sample.width, sample.height);
        }

        sample.x = 900;
        QVERIFY(!meterOverlayMayAcquireOrContinue(
            sample, epoch, true, prior));
        sample.x = 500;
        sample.width = 80;
        QVERIFY(!meterOverlayMayAcquireOrContinue(
            sample, epoch, true, prior));
    }

    void previewBeforeDetectionBackfillsOnlyBeforeAcknowledgement()
    {
        const QSize captureSize(1920, 1080);
        const QSize previewSize(1280, 720);
        const QRect captureBox(1500, 300, 60, 300);

        RemoteFrameOverlaySnapshotStore overlays;
        overlays.publish(RemoteFrameOverlaySnapshot{
            100, {}, {}, false, previewSize, captureSize, 499, {}, -1, 11});
        overlays.publish(RemoteFrameOverlaySnapshot{
            101, {}, {}, false, previewSize, captureSize, 500, {}, -1, 12});

        // The preview exists first and therefore has no overlay geometry yet.
        const auto beforeDetection = overlays.lookup(101);
        QVERIFY(beforeDetection.has_value());
        QVERIFY(!beforeDetection->meterBox.isValid());
        QVERIFY(!beforeDetection->joinedCaptureBox.isValid());
        QVERIFY(!beforeDetection->meterConfirmed);

        MeterBoxRing ring;
        ring.record(500, captureBox);
        const std::size_t patched = overlays.backfillUnacknowledged(
            100, captureSize, MeterBoxRing::kJoinWindow,
            [&ring](int sourceFrameNumber, QRect& outCaptureBox,
                    int& matchedDetectionFrameNumber) {
                return ring.lookup(
                    sourceFrameNumber, outCaptureBox,
                    &matchedDetectionFrameNumber);
            });
        QCOMPARE(patched, std::size_t{1});

        const auto ready = overlays.lookup(101);
        QVERIFY(ready.has_value());
        QCOMPARE(ready->joinedCaptureBox, captureBox);
        QCOMPARE(ready->joinedDetectionFrameNumber, 500);
        QVERIFY(ready->meterConfirmed);

        // Image.Ready resolves the late raw box with the same exact aspect-fit
        // policy as ordinary publication; no guard or smoothing is added.
        const QRect resolved = resolveLateMeterOverlayBox(
            ready->joinedCaptureBox,
            ready->captureSize,
            ready->frameSize,
            ready->shotToken,
            ready->sourceFrameNumber);
        const QRect rawMapped = mapCaptureBoxAspectFit(
            captureBox, captureSize, previewSize);
        QCOMPARE(resolved, rawMapped);

        // Serial 100 was already acknowledged, and even a resolver which tries
        // to return newer evidence for frame 500 cannot rewrite the older image.
        const auto acknowledged = overlays.lookup(100);
        QVERIFY(acknowledged.has_value());
        QVERIFY(!acknowledged->meterConfirmed);
        const std::size_t newerOnOlder = overlays.backfillUnacknowledged(
            100, captureSize, MeterBoxRing::kJoinWindow,
            [](int, QRect& outCaptureBox, int& matchedDetectionFrameNumber) {
                outCaptureBox = QRect(10, 20, 30, 40);
                matchedDetectionFrameNumber = 501;
                return true;
            });
        QCOMPARE(newerOnOlder, std::size_t{0});
        QCOMPARE(overlays.lookup(101)->joinedDetectionFrameNumber, 500);
    }

    void meterOverlayPresentationIsZeroLagWithBoundedExtentJitter()
    {
        const QSize preview(1280, 720);
        MeterOverlayPresentationTracker tracker;

        // Quantized extent noise is presentation-only.  Twenty alternating
        // 18/19 x 64/65 observations have 38 px of raw total variation; the
        // deadband removes it while never moving either extent >1 px away.
        int rawExtentVariation = 0;
        int drawnExtentVariation = 0;
        QRect previousRaw;
        QRect previousDrawn;
        for (int i = 0; i < 20; ++i) {
            const QRect raw(100 - (i & 1), 300 - (i & 1),
                            18 + (i & 1), 64 + (i & 1));
            const QRect drawn = tracker.update(raw, 7, preview, 100 + i);
            QVERIFY(drawn.isValid());
            QVERIFY(std::abs(drawn.width() - raw.width()) <= 1);
            QVERIFY(std::abs(drawn.height() - raw.height()) <= 1);
            QVERIFY(std::abs(drawn.center().x() - raw.center().x()) <= 1);
            QVERIFY(std::abs(drawn.center().y() - raw.center().y()) <= 1);
            if (i > 0) {
                rawExtentVariation += std::abs(raw.width() - previousRaw.width())
                    + std::abs(raw.height() - previousRaw.height());
                drawnExtentVariation += std::abs(drawn.width() - previousDrawn.width())
                    + std::abs(drawn.height() - previousDrawn.height());
            }
            previousRaw = raw;
            previousDrawn = drawn;
        }
        QCOMPARE(rawExtentVariation, 38);
        QCOMPARE(drawnExtentVariation, 0);

        // The reader and exact/prior-frame join own position tracking. Presentation
        // follows the joined box on this frame, including motion onset, without
        // acquiring another velocity estimate behind it.
        for (int i = 0; i < 24; ++i) {
            const QRect raw(140 + 3 * i, 320 + i, 18, 64);
            const QRect drawn = tracker.update(raw, 7, preview, 120 + i);
            QCOMPARE(drawn.size(), raw.size());
            const int tol = 0;
            QVERIFY2(std::abs(drawn.center().x() - raw.center().x()) <= tol,
                     qPrintable(QStringLiteral("frame %1: drawn x %2 raw x %3")
                                    .arg(i).arg(drawn.center().x()).arg(raw.center().x())));
            QVERIFY2(std::abs(drawn.center().y() - raw.center().y()) <= tol,
                     qPrintable(QStringLiteral("frame %1: drawn y %2 raw y %3")
                                    .arg(i).arg(drawn.center().y()).arg(raw.center().y())));
        }

        // A distant identity never animates across the court.
        const QRect identity(900, 120, 18, 64);
        QCOMPARE(tracker.update(identity, 7, preview, 144), identity);

        // Dropout is an immediate reset; the next valid observation is an exact
        // relock, with no stale center or extent carried across the gap.
        QVERIFY(!tracker.update({}, 7, preview, 145).isValid());
        QVERIFY(!tracker.valid());
        const QRect relock(420, 410, 24, 110);
        QCOMPARE(tracker.update(relock, 7, preview, 146), relock);

        // A decoder discontinuity is the same hard boundary.
        const QRect afterGap(430, 408, 26, 112);
        QCOMPARE(tracker.update(afterGap, 7, preview, 151), afterGap);
    }

    void meterOverlayExactFrameTracksSlowStartStopAndReversalWithoutAddedLag()
    {
        const QSize preview(1280, 720);
        MeterOverlayPresentationTracker tracker;
        // One-pixel motion is not distinguishable from quantization noise at
        // this presentation boundary. Do not suppress measured motion here.
        const std::vector<QPoint> points{
            {100, 300}, {101, 300}, {102, 301}, {104, 302},
            {108, 304}, {112, 306}, {112, 306}, {112, 306},
            {111, 305}, {109, 303}, {105, 300}, {101, 297}};
        for (std::size_t i = 0; i < points.size(); ++i) {
            const QRect raw(points[i], QSize(18, 64));
            const QRect drawn = tracker.update(raw, 7, preview, 100 + static_cast<int>(i));
            QCOMPARE(drawn, raw);
        }
    }

    void meterOverlayPositionIsIndependentOfEarlyOrLateMetadataArrival()
    {
        const QSize preview(1280, 720);
        MeterOverlayPresentationTracker tracker;
        const std::vector<QPoint> points{
            {200, 310}, {203, 311}, {206, 312}, {209, 313},
            {209, 313}, {207, 312}, {205, 311}, {204, 310}};
        for (std::size_t i = 0; i < points.size(); ++i) {
            const int sourceFrame = 200 + static_cast<int>(i);
            const QRect captureBox(points[i], QSize(20, 80));
            const QRect mapped = mapCaptureBoxAspectFit(captureBox, preview, preview);
            const QRect atPublication = tracker.update(mapped, 8, preview, sourceFrame);
            const QRect atImageReady = resolveLateMeterOverlayBox(
                captureBox, preview, preview, 8, sourceFrame);
            // Both paths use identical frame/geometry evidence. The order in
            // which their independent channels arrived must not change position.
            QCOMPARE(atPublication, atImageReady);
        }
    }

    void overlayMetadataHistoryCoversBoundedQuarterSecondStall()
    {
        constexpr int kQuarterSecondFramesAt60Hz = 15;
        static_assert(
            RemoteFrameOverlaySnapshotStore::kCapacity
            >= static_cast<std::size_t>(kQuarterSecondFramesAt60Hz + 3));

        const QSize captureSize(1920, 1080);
        const QSize previewSize(1280, 720);
        RemoteFrameOverlaySnapshotStore overlays;
        const int newestSerial =
            static_cast<int>(RemoteFrameOverlaySnapshotStore::kCapacity) + 2;
        for (int serial = 1; serial <= newestSerial; ++serial) {
            overlays.publish(RemoteFrameOverlaySnapshot{
                serial, {}, {}, false, previewSize, captureSize,
                1000 + serial, {}, -1, 0});
        }

        QCOMPARE(overlays.size(), RemoteFrameOverlaySnapshotStore::kCapacity);
        QVERIFY(!overlays.lookup(1).has_value());
        QVERIFY(!overlays.lookup(2).has_value());

        const int stalledSerial = newestSerial - kQuarterSecondFramesAt60Hz;
        const int stalledFrame = 1000 + stalledSerial;
        QVERIFY(overlays.lookup(stalledSerial).has_value());
        const QRect captureBox(1200, 200, 48, 260);
        const std::size_t patched = overlays.backfillUnacknowledged(
            0, captureSize, MeterBoxRing::kJoinWindow,
            [stalledFrame, captureBox](
                int sourceFrameNumber, QRect& outCaptureBox,
                int& matchedDetectionFrameNumber) {
                if (sourceFrameNumber != stalledFrame) {
                    return false;
                }
                outCaptureBox = captureBox;
                matchedDetectionFrameNumber = stalledFrame;
                return true;
            });
        QCOMPARE(patched, std::size_t{1});
        QCOMPARE(
            overlays.lookup(stalledSerial)->joinedDetectionFrameNumber,
            stalledFrame);
    }

    void lateBackfillUsesClosestPriorDetectionWithinJoinWindow()
    {
        const QSize captureSize(1920, 1080);
        const QSize previewSize(1280, 720);
        RemoteFrameOverlaySnapshotStore overlays;
        overlays.publish(RemoteFrameOverlaySnapshot{
            200, QRect(1, 2, 3, 4), {}, true, previewSize,
            captureSize, 506, {}, -1, 3});

        MeterBoxRing ring;
        const QRect frame504Box(100, 200, 40, 220);
        ring.record(504, frame504Box);
        QCOMPARE(
            overlays.backfillUnacknowledged(
                199, captureSize, MeterBoxRing::kJoinWindow,
                [&ring](int sourceFrameNumber, QRect& outCaptureBox,
                        int& matchedDetectionFrameNumber) {
                    return ring.lookup(
                        sourceFrameNumber, outCaptureBox,
                        &matchedDetectionFrameNumber);
                }),
            std::size_t{1});
        QCOMPARE(overlays.lookup(200)->joinedDetectionFrameNumber, 504);
        QVERIFY(!overlays.lookup(200)->meterBox.isValid());

        // A one-frame-prior sample supersedes the provisional two-frame bridge
        // before ACK.
        const QRect frame505Box(130, 206, 40, 220);
        ring.record(505, frame505Box);
        QCOMPARE(
            overlays.backfillUnacknowledged(
                199, captureSize, MeterBoxRing::kJoinWindow,
                [&ring](int sourceFrameNumber, QRect& outCaptureBox,
                        int& matchedDetectionFrameNumber) {
                    return ring.lookup(
                        sourceFrameNumber, outCaptureBox,
                        &matchedDetectionFrameNumber);
                }),
            std::size_t{1});
        QCOMPARE(overlays.lookup(200)->joinedDetectionFrameNumber, 505);

        // A detection from frame 507 is newer than source frame 506. Ring lookup
        // and the store guard both prohibit it from moving the older preview.
        ring.record(507, QRect(170, 214, 40, 220));
        QCOMPARE(
            overlays.backfillUnacknowledged(
                199, captureSize, MeterBoxRing::kJoinWindow,
                [&ring](int sourceFrameNumber, QRect& outCaptureBox,
                        int& matchedDetectionFrameNumber) {
                    return ring.lookup(
                        sourceFrameNumber, outCaptureBox,
                        &matchedDetectionFrameNumber);
                }),
            std::size_t{0});
        QCOMPARE(overlays.lookup(200)->joinedDetectionFrameNumber, 505);
    }

    void authoritativeClearAffectsOnlyNewerUnacknowledgedFrames()
    {
        const QSize captureSize(1920, 1080);
        const QSize previewSize(1280, 720);
        const QRect box(100, 200, 40, 220);
        RemoteFrameOverlaySnapshotStore overlays;
        overlays.publish(RemoteFrameOverlaySnapshot{
            300, QRect(60, 120, 30, 150), {}, true, previewSize,
            captureSize, 900, box, 900, 1});
        overlays.publish(RemoteFrameOverlaySnapshot{
            301, QRect(62, 122, 30, 150), {}, true, previewSize,
            captureSize, 901, box, 900, 1});
        overlays.publish(RemoteFrameOverlaySnapshot{
            302, QRect(64, 124, 30, 150), {}, true, previewSize,
            captureSize, 899, box, 899, 1});

        const std::size_t cleared =
            overlays.clearUnacknowledgedFromSourceFrame(300, 901);
        QCOMPARE(cleared, std::size_t{1});

        // An acknowledged frame is immutable, as is an older unacknowledged
        // image whose pixels predate the detector-loss boundary.
        QVERIFY(overlays.lookup(300)->meterConfirmed);
        QVERIFY(overlays.lookup(300)->meterBox.isValid());
        QVERIFY(overlays.lookup(302)->meterConfirmed);
        QVERIFY(overlays.lookup(302)->meterBox.isValid());

        const auto clearedFrame = overlays.lookup(301);
        QVERIFY(clearedFrame.has_value());
        QVERIFY(!clearedFrame->meterConfirmed);
        QVERIFY(!clearedFrame->meterBox.isValid());
        QVERIFY(!clearedFrame->joinedCaptureBox.isValid());
        QCOMPARE(clearedFrame->joinedDetectionFrameNumber, -1);
    }

    // EXACT_PRESENTED_REGRESSION_CASES_BEGIN
    void exactMetadataAfterReadyCorrectsOnlyCurrentProvisionalBox()
    {
        const QSize size(1280, 720);
        const QRect provisional(430, 300, 30, 140);
        const QRect exact(410, 295, 28, 138); // meter stopped/reversed; old bridge overshot
        RemoteFrameOverlaySnapshotStore overlays;
        overlays.publish(RemoteFrameOverlaySnapshot{
            100, provisional, {}, true, size, size, 499, provisional, 498, 12});
        overlays.publish(RemoteFrameOverlaySnapshot{
            101, provisional, {}, true, size, size, 500, provisional, 498, 12});
        overlays.publish(RemoteFrameOverlaySnapshot{
            102, provisional, {}, true, size, size, 501, provisional, 498, 12});
        QVERIFY(applyPresentedExactCorrection(overlays, 101, size, 500, exact, 12));
        const auto corrected = overlays.lookup(101);
        QVERIFY(corrected.has_value());
        QCOMPARE(corrected->serial, 101);
        QCOMPARE(corrected->sourceFrameNumber, 500);
        QCOMPARE(corrected->shotToken, quint64{12});
        QCOMPARE(corrected->frameSize, size);
        QVERIFY(corrected->meterConfirmed);
        QVERIFY(!corrected->meterBox.isValid()); // normal exact mapping at publication
        QCOMPARE(corrected->joinedCaptureBox, exact);
        QCOMPARE(corrected->joinedDetectionFrameNumber, 500);
        QCOMPARE(overlays.lookup(100)->meterBox, provisional);
        QCOMPARE(overlays.lookup(102)->meterBox, provisional);
        QCOMPARE(overlays.lookup(100)->joinedDetectionFrameNumber, 498);
        QCOMPARE(overlays.lookup(102)->joinedDetectionFrameNumber, 498);
        // Repeated metadata cannot chatter or mutate an already-exact texture.
        QVERIFY(!applyPresentedExactCorrection(
            overlays, 101, size, 500, QRect(999, 111, 28, 138), 12));
        QCOMPARE(overlays.lookup(101)->joinedCaptureBox, exact);
    }

    void exactMetadataAfterReadyCanCorrectPreviouslyHeldGeometry()
    {
        const QSize capture(1920, 1080);
        const QSize preview(1280, 720);
        const QRect held(90, 180, 30, 180);
        const QRect measured(190, 265, 45, 270);
        RemoteFrameOverlaySnapshotStore overlays;
        // An existing visible lock, but this presentation had no usable frame join.
        overlays.publish(RemoteFrameOverlaySnapshot{
            201, held, {}, true, preview, capture, 900, {}, -1, 40});
        QVERIFY(applyPresentedExactCorrection(overlays, 201, capture, 900, measured, 40));
        const auto corrected = overlays.lookup(201);
        QCOMPARE(corrected->joinedCaptureBox, measured);
        QCOMPARE(corrected->joinedDetectionFrameNumber, 900);
        QCOMPARE(corrected->frameSize, preview);
        QCOMPARE(corrected->captureSize, capture);
        QVERIFY(corrected->meterConfirmed);
    }

    void exactMetadataAfterReadyRejectsMismatchedIdentity_data()
    {
        QTest::addColumn<int>("variant");
        QTest::newRow("older_source_frame") << 0;
        QTest::newRow("future_source_frame") << 1;
        QTest::newRow("old_shot") << 2;
        QTest::newRow("changed_capture_dimensions") << 3;
        QTest::newRow("invalid_source_identity") << 4;
        QTest::newRow("invalid_geometry") << 5;
        QTest::newRow("missing_or_evicted_serial") << 6;
        QTest::newRow("already_exact") << 7;
        QTest::newRow("cleared_or_absent_lock") << 8;
        QTest::newRow("texture_advanced_during_detection") << 9;
        QTest::newRow("old_serial_metadata_after_new_ack") << 10;
        QTest::newRow("confirmed_but_geometry_absent") << 11;
    }

    void exactMetadataAfterReadyRejectsMismatchedIdentity()
    {
        QFETCH(int, variant);
        const QSize size(1280, 720);
        const QRect prior(430, 300, 30, 140);
        QRect candidate(410, 295, 28, 138);
        RemoteFrameOverlaySnapshotStore overlays;
        overlays.publish(RemoteFrameOverlaySnapshot{
            100, prior, {}, true, size, size, 499, prior, 498, 12});
        overlays.publish(RemoteFrameOverlaySnapshot{
            101, variant == 11 ? QRect{} : prior, {}, variant != 8,
            size, size, 500, variant == 11 ? QRect{} : prior,
            variant == 7 ? 500 : 498, 12});
        overlays.publish(RemoteFrameOverlaySnapshot{
            102, prior, {}, true, size, size, 501, prior, 499, 12});
        int current = 101;
        int source = 500;
        quint64 shot = 12;
        QSize dimensions = size;
        switch (variant) {
        case 0: source = 499; break;
        case 1: source = 501; break;
        case 2: shot = 11; break;
        case 3: dimensions = QSize(1920, 1080); break;
        case 4: source = -1; break;
        case 5: candidate = {}; break;
        case 6: current = 999; break;
        case 9: current = 102; break;
        case 10: current = 102; source = 499; break;
        default: break;
        }
        const auto before = overlays.lookup(current);
        QVERIFY(!applyPresentedExactCorrection(
            overlays, current, dimensions, source, candidate, shot));
        const auto after = overlays.lookup(current);
        QCOMPARE(after.has_value(), before.has_value());
        if (before.has_value()) {
            QCOMPARE(after->meterBox, before->meterBox);
            QCOMPARE(after->joinedCaptureBox, before->joinedCaptureBox);
            QCOMPARE(after->joinedDetectionFrameNumber, before->joinedDetectionFrameNumber);
            QCOMPARE(after->meterConfirmed, before->meterConfirmed);
        }
        QCOMPARE(overlays.lookup(100)->meterBox, prior);
    }
    // EXACT_PRESENTED_REGRESSION_CASES_END

    // Exercise the real QML control, including external settings updates after a click.
    void tempoToggleRevealsSelectorAndTracksExternalChanges()
    {
        const QString qmlRoot = QStringLiteral(ORION_QML_SOURCE_DIR);
        registerSmokeQmlTypes(qmlRoot);
        QQmlEngine engine;
        engine.addImportPath(qmlRoot);
        engine.addImageProvider(QStringLiteral("remote"), new SmokeFrameProvider);
        QmlOrionSmokeStub stub;
        stub.insert(QStringLiteral("tempoEnabled"), false);
        engine.rootContext()->setContextProperty(QStringLiteral("orion"), &stub);
        QSignalSpy writes(&stub, &QQmlPropertyMap::valueChanged);
        QQmlComponent component(&engine, QUrl::fromLocalFile(
            qmlRoot + QStringLiteral("/components/RhythmCard.qml")));
        if (component.isLoading()) QTRY_VERIFY_WITH_TIMEOUT(!component.isLoading(), 3000);
        QStringList errorLines;
        for (const QQmlError& error : component.errors()) errorLines.append(error.toString());
        std::unique_ptr<QObject> card(component.create());
        QVERIFY2(card != nullptr, qPrintable(errorLines.join(QChar::LineFeed)));
        auto* selector = card->findChild<QQuickItem*>(QStringLiteral("releasePathSelector"));
        auto* toggle = card->findChild<QQuickItem*>(QStringLiteral("tempoToggle"));
        QVERIFY(selector != nullptr);
        QVERIFY(toggle != nullptr);
        QCOMPARE(selector->property("count").toInt(), 2);
        QVERIFY(!selector->isVisible());
        QVERIFY(!toggle->property("checked").toBool());
        QCOMPARE(selector->property("value").toString(), QStringLiteral("Button"));
        QCOMPARE(writes.count(), 0); // Creation must not rewrite saved/legacy settings.
        QVERIFY(card->findChild<QQuickItem*>(QStringLiteral("rhythmFlickSlider")) == nullptr);
        QVERIFY(card->findChild<QQuickItem*>(QStringLiteral("rhythmReleaseStyle")) == nullptr);
        QVERIFY(QMetaObject::invokeMethod(toggle, "toggled", Q_ARG(bool, true)));
        QCOMPARE(stub.value(QStringLiteral("tempoEnabled")).toBool(), true);
        QVERIFY(selector->isVisible());
        QVERIFY(toggle->property("checked").toBool());
        QVERIFY(QMetaObject::invokeMethod(selector, "activated", Q_ARG(int, 1)));
        QCOMPARE(stub.value(QStringLiteral("tempoInputPath")).toString(), QStringLiteral("Stick"));
        QCOMPARE(selector->property("value").toString(), QStringLiteral("Stick"));
        const int beforeExternal = writes.count();
        stub.insert(QStringLiteral("tempoInputPath"), QStringLiteral("Button"));
        QCOMPARE(selector->property("value").toString(), QStringLiteral("Button"));
        QCOMPARE(writes.count(), beforeExternal);
        QVERIFY(QMetaObject::invokeMethod(selector, "activated", Q_ARG(int, 1)));
        QVERIFY(QMetaObject::invokeMethod(toggle, "toggled", Q_ARG(bool, false)));
        QVERIFY(!selector->isVisible());
        QCOMPARE(stub.value(QStringLiteral("tempoInputPath")).toString(), QStringLiteral("Stick"));
        const int beforeHidden = writes.count();
        QVERIFY(QMetaObject::invokeMethod(card.get(), "selectPath", Q_ARG(QVariant, QVariant(0))));
        QCOMPARE(writes.count(), beforeHidden);
        QVERIFY(QMetaObject::invokeMethod(toggle, "toggled", Q_ARG(bool, true)));
        QVERIFY(selector->isVisible());
        QCOMPARE(selector->property("value").toString(), QStringLiteral("Stick"));
        const int beforeInvalid = writes.count();
        QVERIFY(QMetaObject::invokeMethod(card.get(), "selectPath", Q_ARG(QVariant, QVariant(-1))));
        QVERIFY(QMetaObject::invokeMethod(card.get(), "selectPath", Q_ARG(QVariant, QVariant(2))));
        QCOMPARE(writes.count(), beforeInvalid);
        stub.insert(QStringLiteral("tempoEnabled"), false);
        QVERIFY(!selector->isVisible());
        QVERIFY(!toggle->property("checked").toBool());
        card->setProperty("inputTimed", true);
        QVERIFY(QMetaObject::invokeMethod(toggle, "toggled", Q_ARG(bool, true)));
        QCOMPARE(stub.value(QStringLiteral("inputTimedRhythmEnabled")).toBool(), true);
        QCOMPARE(stub.value(QStringLiteral("tempoEnabled")).toBool(), false);
        QVERIFY(selector->isVisible());
        QVERIFY(!selector->isEnabled());
        QCOMPARE(selector->property("value").toString(), QStringLiteral("Button"));
    }


};

QTEST_MAIN(PreviewPresentationBufferTests)
#include "PreviewPresentationBufferTests.moc"
