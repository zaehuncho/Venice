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
        // Live meter-side HUD lines, production placeholders. The three rows
        // bind these pre-formatted native strings directly; the HUD test
        // overwrites them to prove QML surfaces the native values verbatim.
        // meterHudCourtLine/meterHudJitterLine removed 2026-08-06 with the
        // COURT/JITTER rows (owner: three values only, the ones the bot uses).
        insert(QStringLiteral("meterHudMeasured"), false);
        insert(QStringLiteral("meterHudLive"), false);
        insert(QStringLiteral("meterHudCommandLate"), false);
        insert(QStringLiteral("meterHudFillLine"), QStringLiteral("--"));
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
        auto* metricsOverlay = page->findChild<QQuickItem*>(
            QStringLiteral("liveMeterMetricsOverlay"));
        auto* etaMetric = page->findChild<QQuickItem*>(QStringLiteral("meterEtaMetric"));
        auto* holdMetric = page->findChild<QQuickItem*>(QStringLiteral("meterHoldMetric"));
        QVERIFY(previewA != nullptr);
        QVERIFY(previewB != nullptr);
        QVERIFY(captureHost != nullptr);
        QVERIFY(layer != nullptr);
        QVERIFY(lock != nullptr);
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
        QVERIFY(metricsOverlay->isVisible());
        QCOMPARE(etaMetric->property("text").toString(), QStringLiteral("ETA  18 ms"));
        QCOMPARE(holdMetric->property("text").toString(), QStringLiteral("HOLD 487 ms"));
        QVERIFY(etaMetric->isVisible());
        QVERIFY(holdMetric->isVisible());

        // The distant 9x64 capture box is uniformly aspect-fit into the live
        // image. Width and height use the same scale, so the meter cannot be
        // stretched into a horizontal HUD-like rectangle.
        const double drawScale = layer->property("drawScale").toDouble();
        QVERIFY(drawScale > 0.0);
        QCOMPARE(lock->width(), 9.0 * drawScale);
        QCOMPARE(lock->height(), 64.0 * drawScale);
        QVERIFY(lock->height() > lock->width());

        // The user toggle is presentation-only: it hides the overlay without
        // changing the detector box or either native metric value.
        stub.insert(QStringLiteral("showLiveMeterMetrics"), false);
        QCoreApplication::processEvents();
        QVERIFY(!metricsOverlay->isVisible());
        QVERIFY(layer->isVisible());
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

    // 2026-08-06 display work: (1) the factory lock colour is the bright blue
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
        // migration history remains asserted.
        QCOMPARE(AppConfigData{}.meterOverlayColor, QStringLiteral("#00A8FF"));
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
        // the stub carries the production default, so this pins the bright
        // blue end-to-end without any QML-side colour math.
        auto* lockFrame =
            page->findChild<QQuickItem*>(QStringLiteral("meterLockFrame"));
        QVERIFY(lockFrame != nullptr);
        QObject* border = qvariant_cast<QObject*>(lockFrame->property("border"));
        QVERIFY(border != nullptr);
        QCOMPARE(border->property("color").value<QColor>(),
                 QColor(QStringLiteral("#00A8FF")));

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

    void productionSetupHasNoManualTimingOrNetworkTelemetrySurface()
    {
        QFile dashboard(QStringLiteral(ORION_QML_SOURCE_DIR)
                        + QStringLiteral("/pages/DashboardPage.qml"));
        QVERIFY2(dashboard.open(QIODevice::ReadOnly | QIODevice::Text),
                 qPrintable(dashboard.errorString()));
        const QByteArray source = dashboard.readAll();

        QVERIFY(source.contains("objectName: \"passiveTimingStatus\""));
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
        stub.insert(QStringLiteral("logText"), QStringLiteral(
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
        stub.insert(QStringLiteral("logText"), QStringLiteral(
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
        stub.insert(QStringLiteral("logText"), QStringLiteral(
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

        stub.insert(QStringLiteral("logText"), QString{});
        QVERIFY(syncLogModel());
        QCoreApplication::processEvents();
        QCOMPARE(page->property("captureLogLines").toInt(), 0);

        // A fresh page reconstructs the bounded view from current native state.
        stub.insert(QStringLiteral("logText"), QStringLiteral("09:01:00  Reloaded"));
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
};

QTEST_MAIN(PreviewPresentationBufferTests)
#include "PreviewPresentationBufferTests.moc"
