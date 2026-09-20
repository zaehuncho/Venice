#include "SharedMemoryFramePump.h"

#include "SharedMemoryFrameReader.h"

#include <QtCore/QMetaObject>

#include <algorithm>
#include <chrono>
#include <utility>

namespace orion {
namespace {

class NativeSharedMemoryFrameSource final : public SharedMemoryFrameSource {
public:
    void configureTransport(const SharedMemoryTransportNames& names) override {
        reader_.configureTransport(names);
    }
    bool open() override { return reader_.open(); }
    void close() override { reader_.close(); }
    [[nodiscard]] bool isOpen() const noexcept override { return reader_.isOpen(); }
    SharedMemoryFrameWaitResult waitForFrameReady(int timeoutMs) override {
        return reader_.waitForFrameReady(timeoutMs);
    }
    void interruptWait() noexcept override { reader_.interruptWait(); }
    bool probeGeneration(std::uint64_t& writeCount) override {
        return reader_.probeGeneration(writeCount);
    }
    QImage readFrame(int& frameNumber) override { return reader_.readFrame(frameNumber); }
    [[nodiscard]] std::uint64_t lastReadGeneration() const noexcept override {
        return reader_.lastReadGeneration();
    }
    [[nodiscard]] std::uint64_t lastReadTimestampNs() const noexcept override {
        return reader_.lastReadTimestampNs();
    }
    [[nodiscard]] QString lastError() const override { return reader_.lastError(); }

private:
    SharedMemoryFrameReader reader_;
};

constexpr int kReadyEventWaitMs = 250;
// Two complete misses inspect the committed mapping identity. A first observed
// advance is only a suspicion: the producer commits under the mapping mutex,
// releases it, then signals the event. A timeout/probe can land between those
// steps, especially on the first frame after startup or a pause. Require one
// further complete event timeout before confirming the loss (at most 750 ms
// from the start of a missing-event run). Healthy event delivery never waits
// for this diagnostic confirmation and still reads immediately on Ready.
constexpr int kMissedReadyProbeTimeouts = 2;

quint64 monotonicNowNs() noexcept
{
    const auto count = std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    return count > 0 ? static_cast<quint64>(count) : 0;
}

} // namespace

SharedMemoryFramePump::SharedMemoryFramePump(QObject* parent)
    : SharedMemoryFramePump(std::make_unique<NativeSharedMemoryFrameSource>(), parent)
{
}

SharedMemoryFramePump::SharedMemoryFramePump(
    std::unique_ptr<SharedMemoryFrameSource> source, QObject* parent)
    : QObject(parent),
      source_(source ? std::move(source)
                     : std::make_unique<NativeSharedMemoryFrameSource>()),
      ownerThreadId_(std::this_thread::get_id())
{
    thread_ = std::thread([this]() { threadLoop(); });
}

SharedMemoryFramePump::~SharedMemoryFramePump()
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        stop_ = true;
        desiredActive_ = false;
        hasPending_ = false;
        readyValid_ = false;
        readyBatch_ = {};
    }
    source_->interruptWait();
    cv_.notify_all();
    if (thread_.joinable()) {
        thread_.join();
    }
}

void SharedMemoryFramePump::beginSourceEpoch(
    quint64 sourceEpoch, SharedMemoryTransportNames transportNames)
{
    if (sourceEpoch == 0) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stop_) {
            return;
        }
        desiredEpoch_ = sourceEpoch;
        desiredTransportNames_ = std::move(transportNames);
        desiredActive_ = true;
        resetPending_ = true;
        hasPending_ = false;
        pendingEpoch_ = 0;
        pendingEventFrameNumber_ = 0;
        // A queued deliverLatest wake-up may still exist. Clear its old payload
        // but keep deliveryScheduled_ latched; that same bounded wake-up can
        // deliver the new epoch if the worker publishes before it runs.
        readyValid_ = false;
        readyBatch_ = {};
    }
    source_->interruptWait();
    cv_.notify_one();
}

void SharedMemoryFramePump::retireSourceEpoch(quint64 sourceEpoch)
{
    if (sourceEpoch == 0) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stop_ || desiredEpoch_ != sourceEpoch) {
            return;
        }
        desiredActive_ = false;
        resetPending_ = true;
        hasPending_ = false;
        pendingEpoch_ = 0;
        pendingEventFrameNumber_ = 0;
        readyValid_ = false;
        readyBatch_ = {};
    }
    source_->interruptWait();
    cv_.notify_one();
}

void SharedMemoryFramePump::submitFrameNotification(
    quint64 sourceEpoch, int eventFrameNumber)
{
    if (sourceEpoch == 0) {
        return;
    }
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stop_ || !desiredActive_ || desiredEpoch_ != sourceEpoch) {
            return;
        }
        ++stats_.submitted;
        if (hasPending_) {
            ++stats_.notificationCoalesced;
        }
        pendingEpoch_ = sourceEpoch;
        pendingEventFrameNumber_ = eventFrameNumber;
        hasPending_ = true;
        stats_.maxPendingDepth = std::max<std::size_t>(stats_.maxPendingDepth, 1);
    }
    source_->interruptWait();
    cv_.notify_one();
}

SharedMemoryFramePumpStats SharedMemoryFramePump::stats() const noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

bool SharedMemoryFramePump::epochIsCurrent(quint64 sourceEpoch) const noexcept
{
    std::lock_guard<std::mutex> lock(mutex_);
    return !stop_ && desiredActive_ && desiredEpoch_ == sourceEpoch;
}

void SharedMemoryFramePump::threadLoop()
{
    quint64 workerEpoch = 0;
    bool readerOpen = false;
    int openFailureRun = 0;
    QString openError;
    int readFailureRun = 0;
    QString readError;
    int notificationFailureRun = 0;
    QString notificationError;
    int consecutiveEventTimeouts = 0;
    std::uint64_t lastDiagnosticGeneration = 0;
    std::uint64_t pendingNotificationGeneration = 0;
    quint64 framesRead = 0;

    for (;;) {
        bool reset = false;
        bool active = false;
        bool readRequested = false;
        bool eventMode = false;
        quint64 requestEpoch = 0;
        int eventFrameNumber = 0;
        SharedMemoryTransportNames transportNames;
        {
            std::unique_lock<std::mutex> lock(mutex_);
            cv_.wait(lock, [this]() {
                return stop_ || resetPending_ || hasPending_
                    || (desiredActive_
                        && desiredTransportNames_.eventNotificationsEnabled());
            });
            if (stop_) {
                break;
            }
            stats_.workerObserved = true;
            stats_.workerDifferentFromOwner =
                std::this_thread::get_id() != ownerThreadId_;

            reset = resetPending_;
            resetPending_ = false;
            active = desiredActive_;
            requestEpoch = desiredEpoch_;
            transportNames = desiredTransportNames_;
            eventMode = active && transportNames.eventNotificationsEnabled();
            if (active && hasPending_ && pendingEpoch_ == requestEpoch) {
                readRequested = true;
                eventFrameNumber = pendingEventFrameNumber_;
                hasPending_ = false;
                pendingEpoch_ = 0;
                pendingEventFrameNumber_ = 0;
            } else if (!active) {
                hasPending_ = false;
                pendingEpoch_ = 0;
                pendingEventFrameNumber_ = 0;
            }
        }

        if (reset) {
            source_->close();
            source_->configureTransport(transportNames);
            workerEpoch = requestEpoch;
            readerOpen = false;
            openFailureRun = 0;
            openError.clear();
            readFailureRun = 0;
            readError.clear();
            notificationFailureRun = 0;
            notificationError.clear();
            consecutiveEventTimeouts = 0;
            lastDiagnosticGeneration = 0;
            pendingNotificationGeneration = 0;
            framesRead = 0;
        }
        if (!active || requestEpoch == 0) {
            continue;
        }
        if (eventMode && !readRequested) {
            // The local interrupt handle wakes this wait immediately for
            // retire/restart/destruction or a legacy stdout fallback notice.
            // The timeout is a final fail-safe against a broken kernel handle;
            // it never triggers a frame copy or manufactures a frame. After a
            // bounded run, a header-only mapping probe distinguishes a paused
            // source from a lost/mismatched ready event.
            const SharedMemoryFrameWaitResult waitResult =
                source_->waitForFrameReady(kReadyEventWaitMs);
            if (waitResult == SharedMemoryFrameWaitResult::Ready) {
                readRequested = true;
                consecutiveEventTimeouts = 0;
                notificationFailureRun = 0;
                notificationError.clear();
                pendingNotificationGeneration = 0;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.eventSignals;
                }
            } else if (waitResult == SharedMemoryFrameWaitResult::Timeout) {
                ++consecutiveEventTimeouts;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.eventWaitTimeouts;
                }
                if (consecutiveEventTimeouts < kMissedReadyProbeTimeouts) {
                    continue;
                }

                workerEpoch = requestEpoch;
                if (!source_->isOpen()) {
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        ++stats_.openAttempts;
                    }
                    if (!source_->open()) {
                        readerOpen = false;
                        ++openFailureRun;
                        openError = source_->lastError();
                        {
                            std::lock_guard<std::mutex> lock(mutex_);
                            ++stats_.openFailures;
                        }
                        publishWorkerState(
                            workerEpoch, readerOpen, openFailureRun, openError,
                            readFailureRun, readError, notificationFailureRun,
                            notificationError, framesRead);
                        continue;
                    }
                    readerOpen = true;
                    openFailureRun = 0;
                    openError.clear();
                    publishWorkerState(
                        workerEpoch, readerOpen, openFailureRun, openError,
                        readFailureRun, readError, notificationFailureRun,
                        notificationError, framesRead);
                }

                if (!epochIsCurrent(workerEpoch)) {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.staleCompletionDropped;
                    continue;
                }

                std::uint64_t generation = 0;
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.eventGenerationProbes;
                }
                if (!source_->probeGeneration(generation)) {
                    const QString probeError = source_->lastError();
                    if (!probeError.isEmpty()) {
                        ++readFailureRun;
                        readError = probeError;
                        {
                            std::lock_guard<std::mutex> lock(mutex_);
                            ++stats_.eventGenerationProbeFailures;
                            ++stats_.readFaults;
                        }
                        publishWorkerState(
                            workerEpoch, readerOpen, openFailureRun, openError,
                            readFailureRun, readError, notificationFailureRun,
                            notificationError, framesRead);
                    }
                    continue;
                }
                readFailureRun = 0;
                readError.clear();
                if (generation > lastDiagnosticGeneration) {
                    if (pendingNotificationGeneration == 0
                        || generation < pendingNotificationGeneration) {
                        // Do not count pre-commit idle time as time that this
                        // particular notification has been missing. The next
                        // full wait gives the just-committed writer its chance
                        // to signal. Never copy/present a frame from a probe.
                        pendingNotificationGeneration = generation;
                        continue;
                    }
                    pendingNotificationGeneration = 0;
                    lastDiagnosticGeneration = generation;
                    ++notificationFailureRun;
                    notificationError = QStringLiteral(
                        "SHM mapping generation %1 advanced without ready-event signal")
                                            .arg(generation);
                    {
                        std::lock_guard<std::mutex> lock(mutex_);
                        ++stats_.eventNotificationLosses;
                    }
                    publishWorkerState(
                        workerEpoch, readerOpen, openFailureRun, openError,
                        readFailureRun, readError, notificationFailureRun,
                        notificationError, framesRead);
                } else {
                    pendingNotificationGeneration = 0;
                }
                continue;
            } else if (waitResult == SharedMemoryFrameWaitResult::Interrupted) {
                continue;
            } else if (waitResult == SharedMemoryFrameWaitResult::Unsupported) {
                // A test double or non-Windows source may not implement event
                // waits. Block for an explicit legacy notification instead of
                // polling the mapping or presenting duplicate generations.
                std::unique_lock<std::mutex> lock(mutex_);
                cv_.wait(lock, [this]() {
                    return stop_ || resetPending_ || hasPending_;
                });
                continue;
            } else {
                ++readFailureRun;
                readError = source_->lastError();
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.eventWaitFailures;
                    ++stats_.readFaults;
                }
                publishWorkerState(
                    requestEpoch, readerOpen, openFailureRun, openError,
                    readFailureRun, readError, notificationFailureRun,
                    notificationError, framesRead);
                std::this_thread::sleep_for(std::chrono::milliseconds(10));
                continue;
            }
        }
        if (!readRequested) {
            continue;
        }
        if (!epochIsCurrent(requestEpoch)) {
            std::lock_guard<std::mutex> lock(mutex_);
            ++stats_.staleCompletionDropped;
            continue;
        }
        workerEpoch = requestEpoch;

        if (!source_->isOpen()) {
            {
                std::lock_guard<std::mutex> lock(mutex_);
                ++stats_.openAttempts;
            }
            if (!source_->open()) {
                readerOpen = false;
                ++openFailureRun;
                openError = source_->lastError();
                {
                    std::lock_guard<std::mutex> lock(mutex_);
                    ++stats_.openFailures;
                }
                publishWorkerState(
                    workerEpoch, readerOpen, openFailureRun, openError,
                    readFailureRun, readError, notificationFailureRun,
                    notificationError, framesRead);
                continue;
            }
            readerOpen = true;
            openFailureRun = 0;
            openError.clear();
            // Publish the open transition even if the first read finds ordinary
            // contention/no newer generation and therefore has no image.
            publishWorkerState(
                workerEpoch, readerOpen, openFailureRun, openError,
                readFailureRun, readError, notificationFailureRun,
                notificationError, framesRead);
        }

        if (!epochIsCurrent(workerEpoch)) {
            std::lock_guard<std::mutex> lock(mutex_);
            ++stats_.staleCompletionDropped;
            continue;
        }

        int mappedFrameNumber = 0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ++stats_.readAttempts;
        }
        QImage image = source_->readFrame(mappedFrameNumber);
        const quint64 pumpReadCompletedTimestampNs = monotonicNowNs();
        if (!epochIsCurrent(workerEpoch)) {
            std::lock_guard<std::mutex> lock(mutex_);
            ++stats_.staleCompletionDropped;
            continue;
        }
        if (!image.isNull()) {
            ++framesRead;
            readFailureRun = 0;
            readError.clear();
            lastDiagnosticGeneration = std::max(
                lastDiagnosticGeneration, source_->lastReadGeneration());
            pendingNotificationGeneration = 0;
            {
                std::lock_guard<std::mutex> lock(mutex_);
                ++stats_.framesRead;
            }
            publishWorkerState(
                workerEpoch, readerOpen, openFailureRun, openError,
                readFailureRun, readError, notificationFailureRun,
                notificationError, framesRead, std::move(image),
                mappedFrameNumber, eventFrameNumber,
                source_->lastReadTimestampNs(), pumpReadCompletedTimestampNs);
            continue;
        }

        const QString error = source_->lastError();
        if (error.isEmpty()) {
            // Includes the reader's 3 ms mutex timeout and duplicate mapping
            // generation: both are ordinary latest-wins coalescing, never a
            // hard transport fault and never grounds for JPEG fallback.
            std::lock_guard<std::mutex> lock(mutex_);
            ++stats_.ordinaryNoFrame;
            continue;
        }
        ++readFailureRun;
        readError = error;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            ++stats_.readFaults;
        }
        publishWorkerState(
            workerEpoch, readerOpen, openFailureRun, openError,
            readFailureRun, readError, notificationFailureRun,
            notificationError, framesRead);
    }

    source_->close();
}

void SharedMemoryFramePump::publishWorkerState(
    quint64 sourceEpoch,
    bool readerOpen,
    int openFailureRun,
    const QString& openError,
    int readFailureRun,
    const QString& readError,
    int notificationFailureRun,
    const QString& notificationError,
    quint64 framesRead,
    QImage image,
    int mappedFrameNumber,
    int eventFrameNumber,
    quint64 sourceTimestampNs,
    quint64 pumpReadCompletedTimestampNs)
{
    bool scheduleDelivery = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stop_ || !desiredActive_ || desiredEpoch_ != sourceEpoch) {
            ++stats_.staleCompletionDropped;
            return;
        }
        if (!readyValid_ || readyBatch_.sourceEpoch != sourceEpoch) {
            readyBatch_ = {};
            readyBatch_.sourceEpoch = sourceEpoch;
            readyValid_ = true;
        }
        readyBatch_.readerOpen = readerOpen;
        readyBatch_.openFailureRun = openFailureRun;
        readyBatch_.openError = openError;
        readyBatch_.readFailureRun = readFailureRun;
        readyBatch_.readError = readError;
        readyBatch_.notificationFailureRun = notificationFailureRun;
        readyBatch_.notificationError = notificationError;
        readyBatch_.framesRead = framesRead;
        if (!image.isNull()) {
            if (!readyBatch_.image.isNull()) {
                auto& preceding = readyBatch_.precedingFrames;
                if (preceding.size() >= kMaxReadyFrames - 1) {
                    preceding.erase(preceding.begin());
                    ++stats_.readyFrameReplaced;
                }
                preceding.push_back({std::move(readyBatch_.image),
                    readyBatch_.mappedFrameNumber, readyBatch_.eventFrameNumber,
                    readyBatch_.sourceTimestampNs,
                    readyBatch_.pumpReadCompletedTimestampNs});
            }
            readyBatch_.image = std::move(image);
            readyBatch_.mappedFrameNumber = mappedFrameNumber;
            readyBatch_.eventFrameNumber = eventFrameNumber;
            readyBatch_.sourceTimestampNs = sourceTimestampNs;
            readyBatch_.pumpReadCompletedTimestampNs =
                pumpReadCompletedTimestampNs;
        }
        readyBatch_.eventWaitTimeouts = stats_.eventWaitTimeouts;
        readyBatch_.eventWaitFailures = stats_.eventWaitFailures;
        readyBatch_.eventGenerationProbes = stats_.eventGenerationProbes;
        readyBatch_.eventNotificationLosses = stats_.eventNotificationLosses;
        readyBatch_.readyFrameReplaced = stats_.readyFrameReplaced;
        readyBatch_.deliveryScheduleFailures = stats_.deliveryScheduleFailures;
        const std::size_t readyDepth = readyBatch_.precedingFrames.size()
            + (readyBatch_.image.isNull() ? 0 : 1);
        stats_.maxReadyDepth = std::max(stats_.maxReadyDepth, readyDepth);
        if (!deliveryScheduled_) {
            deliveryScheduled_ = true;
            scheduleDelivery = true;
        }
    }

    if (!scheduleDelivery) {
        return;
    }
    const bool queued = QMetaObject::invokeMethod(
        this, [this]() { deliverLatest(); }, Qt::QueuedConnection);
    if (!queued) {
        std::lock_guard<std::mutex> lock(mutex_);
        deliveryScheduled_ = false;
        readyValid_ = false;
        readyBatch_ = {};
        ++stats_.deliveryScheduleFailures;
    }
}

void SharedMemoryFramePump::deliverLatest()
{
    SharedMemoryFramePumpBatch batch;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        deliveryScheduled_ = false;
        if (stop_ || !readyValid_) {
            return;
        }
        if (!desiredActive_ || readyBatch_.sourceEpoch != desiredEpoch_) {
            readyValid_ = false;
            readyBatch_ = {};
            ++stats_.staleCompletionDropped;
            return;
        }
        batch = std::move(readyBatch_);
        readyBatch_ = {};
        readyValid_ = false;
        batch.presentationDispatchTimestampNs = monotonicNowNs();
        // A long stall or paused producer must not replay an old burst. Keep
        // the newest image (the previous latest-only behaviour) regardless,
        // but discard stale/invalid preceding images using our monotonic read
        // clock, never a regenerated producer timestamp.
        auto& preceding = batch.precedingFrames;
        const auto firstRemoved = std::remove_if(preceding.begin(), preceding.end(),
            [&batch](const SharedMemoryFramePumpImage& frame) {
                const auto readNs = frame.pumpReadCompletedTimestampNs;
                return readNs == 0 || readNs > batch.presentationDispatchTimestampNs
                    || batch.presentationDispatchTimestampNs - readNs > kMaxBufferedAgeNs;
            });
        stats_.readyFrameReplaced += std::distance(firstRemoved, preceding.end());
        preceding.erase(firstRemoved, preceding.end());
        batch.readyFrameReplaced = stats_.readyFrameReplaced;
        ++stats_.deliveredBatches;
    }

    // No member access after emission: a direct receiver may retire or destroy
    // the owning session during this callback.
    emit batchReady(batch);
}

} // namespace orion
