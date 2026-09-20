#include "SharedMemoryFramePump.h"

#include <QtCore/QElapsedTimer>
#include <QtTest/QTest>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <memory>
#include <mutex>

using namespace orion;

namespace {

// Models the production writer ordering: committed header becomes observable
// before SetEvent. The ready signal is latched during the diagnostic probe,
// after the preceding wait has already returned Timeout. No timing sleeps or
// test-only hooks in the pump are needed to make this interleaving deterministic.
class CommitBeforeSignalSource final : public SharedMemoryFrameSource {
public:
    CommitBeforeSignalSource(int advanceAfterProbes, bool notify,
                             bool initialFrame = false, bool keepAdvancing = false)
        : advanceAfterProbes_(advanceAfterProbes), notify_(notify),
          initialFrame_(initialFrame), keepAdvancing_(keepAdvancing) {}

    void configureTransport(const SharedMemoryTransportNames&) override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        interrupted_ = false;
        ready_ = initialFrame_;
        generation_ = initialFrame_ ? 1 : 0;
        readGeneration_ = 0;
        probesInEpoch_ = 0;
        deliveredInEpoch_ = 0;
    }
    bool open() override { open_ = true; return true; }
    void close() override { open_ = false; }
    bool isOpen() const noexcept override { return open_; }
    SharedMemoryFrameWaitResult waitForFrameReady(int timeoutMs) override
    {
        std::unique_lock<std::mutex> lock(mutex_);
        if (timeoutMs != 250) unexpectedTimeout_.store(true);
        if (!cv_.wait_for(lock, std::chrono::milliseconds(timeoutMs),
                          [this] { return interrupted_ || ready_; })) {
            return SharedMemoryFrameWaitResult::Timeout;
        }
        if (interrupted_) {
            interrupted_ = false;
            return SharedMemoryFrameWaitResult::Interrupted;
        }
        ready_ = false;
        return SharedMemoryFrameWaitResult::Ready;
    }
    void interruptWait() noexcept override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        interrupted_ = true;
        cv_.notify_all();
    }
    bool probeGeneration(std::uint64_t& writeCount) override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        ++probesInEpoch_;
        if (probesInEpoch_ == advanceAfterProbes_
            || (keepAdvancing_ && probesInEpoch_ > advanceAfterProbes_)) {
            ++generation_;
            // Producer signals only after its newly committed header was read
            // by this probe. Treating that probe as a loss is a false positive.
            if (notify_) { ready_ = true; cv_.notify_all(); }
        }
        writeCount = generation_;
        probes_.fetch_add(1);
        return true;
    }
    QImage readFrame(int& frameNumber) override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (generation_ == readGeneration_ || generation_ == 0) return {};
        readGeneration_ = generation_;
        frameNumber = static_cast<int>(generation_);
        ++deliveredInEpoch_;
        QImage image(4, 4, QImage::Format_BGR888);
        image.fill(Qt::green);
        return image;
    }
    std::uint64_t lastReadGeneration() const noexcept override
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return readGeneration_;
    }
    QString lastError() const override { return {}; }
    int probes() const { return probes_.load(); }
    bool unexpectedTimeout() const { return unexpectedTimeout_.load(); }
    void enableNotifications()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        notify_ = true;
    }

private:
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    bool open_ = false;
    bool interrupted_ = false;
    bool ready_ = false;
    int advanceAfterProbes_;
    bool notify_;
    bool initialFrame_;
    bool keepAdvancing_;
    int probesInEpoch_ = 0;
    int deliveredInEpoch_ = 0;
    std::uint64_t generation_ = 0;
    std::uint64_t readGeneration_ = 0;
    std::atomic<int> probes_{0};
    std::atomic<bool> unexpectedTimeout_{false};
};

SharedMemoryTransportNames eventNames()
{
    SharedMemoryTransportNames names;
    names.readyEventName = QStringLiteral("OrionPreviewReady_commit_race_test");
    return names;
}

} // namespace

class SharedMemoryFramePumpNotificationTests final : public QObject {
    Q_OBJECT
private slots:
    void commitBeforeReadySignalDoesNotTriggerFallback_data()
    {
        QTest::addColumn<int>("advanceAfterProbes");
        QTest::addColumn<bool>("initialFrame");
        QTest::newRow("first-commit") << 1 << false;
        QTest::newRow("first-commit-after-long-idle") << 4 << false;
        QTest::newRow("resume-after-consumed-frame") << 1 << true;
    }

    void commitBeforeReadySignalDoesNotTriggerFallback()
    {
        QFETCH(int, advanceAfterProbes);
        QFETCH(bool, initialFrame);
        auto source = std::make_unique<CommitBeforeSignalSource>(
            advanceAfterProbes, true, initialFrame);
        auto* raw = source.get();
        SharedMemoryFramePump pump(std::move(source));
        int delivered = 0;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (!batch.image.isNull()) ++delivered;
                });
        pump.beginSourceEpoch(701, eventNames());
        const int expectedFrames = initialFrame ? 2 : 1;
        QTRY_COMPARE_WITH_TIMEOUT(pump.stats().framesRead,
                                  quint64(expectedFrames), 2500);
        QTRY_COMPARE_WITH_TIMEOUT(delivered, expectedFrames, 1000);
        const auto stats = pump.stats();
        QVERIFY(stats.eventGenerationProbes >= quint64(advanceAfterProbes));
        QCOMPARE(stats.eventNotificationLosses, quint64{0});
        QCOMPARE(stats.readFaults, quint64{0});
        QCOMPARE(stats.eventSignals, quint64(expectedFrames));
        QVERIFY(!raw->unexpectedTimeout());
    }

    void missingEventStillFailsBoundedly_data()
    {
        QTest::addColumn<bool>("keepAdvancing");
        QTest::newRow("one-committed-frame") << false;
        QTest::newRow("continuing-commits-wrong-event") << true;
    }

    void missingEventStillFailsBoundedly()
    {
        QFETCH(bool, keepAdvancing);
        auto source = std::make_unique<CommitBeforeSignalSource>(1, false, false, keepAdvancing);
        SharedMemoryFramePump pump(std::move(source));
        SharedMemoryFramePumpBatch failure;
        connect(&pump, &SharedMemoryFramePump::batchReady, this,
                [&](const SharedMemoryFramePumpBatch& batch) {
                    if (batch.notificationFailureRun > 0) failure = batch;
                });
        QElapsedTimer elapsed;
        elapsed.start();
        pump.beginSourceEpoch(702, eventNames());
        QTRY_VERIFY_WITH_TIMEOUT(failure.notificationFailureRun > 0, 1500);
        QVERIFY(elapsed.elapsed() < 1400);
        QVERIFY(failure.notificationError.contains(QStringLiteral("advanced without ready-event")));
        QCOMPARE(pump.stats().framesRead, quint64{0});
        QVERIFY(failure.image.isNull());
        QVERIFY(pump.stats().eventGenerationProbes >= 2);
        QVERIFY(pump.stats().eventWaitTimeouts >= 3);
    }

    void pendingSuspicionIsDiscardedAtSourceEpochChange()
    {
        auto source = std::make_unique<CommitBeforeSignalSource>(1, false);
        auto* raw = source.get();
        SharedMemoryFramePump pump(std::move(source));
        pump.beginSourceEpoch(703, eventNames());
        QTRY_VERIFY_WITH_TIMEOUT(raw->probes() >= 1, 1200);
        // Switch before the confirming wait; a new source has its own identity.
        pump.retireSourceEpoch(703);
        raw->enableNotifications();
        pump.beginSourceEpoch(704, eventNames());
        QTRY_COMPARE_WITH_TIMEOUT(pump.stats().framesRead, quint64{1}, 1500);
        QCOMPARE(pump.stats().eventNotificationLosses, quint64{0});
        QCOMPARE(pump.stats().readFaults, quint64{0});
    }

    void pausedSourceDoesNotManufactureFramesOrLoss()
    {
        auto source = std::make_unique<CommitBeforeSignalSource>(1000000, false);
        SharedMemoryFramePump pump(std::move(source));
        pump.beginSourceEpoch(705, eventNames());
        QTRY_VERIFY_WITH_TIMEOUT(pump.stats().eventGenerationProbes >= 2, 1500);
        QCOMPARE(pump.stats().framesRead, quint64{0});
        QCOMPARE(pump.stats().eventNotificationLosses, quint64{0});
        QCOMPARE(pump.stats().readFaults, quint64{0});
    }
};

QTEST_GUILESS_MAIN(SharedMemoryFramePumpNotificationTests)
#include "SharedMemoryFramePumpNotificationTests.moc"
