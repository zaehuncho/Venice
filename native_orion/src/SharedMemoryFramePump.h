#pragma once

#include "OrionExports.h"
#include "SharedMemoryFrameReader.h"

#include <QtCore/QObject>
#include <QtCore/QString>
#include <QtGui/QImage>

#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <thread>
#include <vector>

namespace orion {

// Narrow source contract used by the persistent SHM pump. Production supplies
// SharedMemoryFrameReader; tests inject a deterministic blocking source to pin
// thread affinity, epoch cancellation, and bounded backpressure without
// weakening the production reader API.
class ORION_REMOTEPLAY_API SharedMemoryFrameSource {
public:
    virtual ~SharedMemoryFrameSource() = default;
    virtual void configureTransport(const SharedMemoryTransportNames&) {}
    virtual bool open() = 0;
    virtual void close() = 0;
    [[nodiscard]] virtual bool isOpen() const noexcept = 0;
    virtual SharedMemoryFrameWaitResult waitForFrameReady(int) {
        return SharedMemoryFrameWaitResult::Unsupported;
    }
    virtual void interruptWait() noexcept {}
    virtual bool probeGeneration(std::uint64_t& writeCount) {
        writeCount = 0;
        return false;
    }
    virtual QImage readFrame(int& frameNumber) = 0;
    [[nodiscard]] virtual std::uint64_t lastReadGeneration() const noexcept {
        return 0;
    }
    [[nodiscard]] virtual std::uint64_t lastReadTimestampNs() const noexcept {
        return 0;
    }
    [[nodiscard]] virtual QString lastError() const = 0;
};

// An owned preview frame kept with its original identities and timestamps.
// These images are display-only, never detector or release-timing inputs.
struct ORION_REMOTEPLAY_API SharedMemoryFramePumpImage final {
    QImage image;
    int mappedFrameNumber = 0;
    int eventFrameNumber = 0;
    quint64 sourceTimestampNs = 0;
    quint64 pumpReadCompletedTimestampNs = 0;
};

// One coalesced GUI delivery. Counters are source-epoch totals/runs rather than
// per-callback deltas, so replacing a ready image while the GUI is busy cannot
// hide transport health or corrupt the existing open/read failure semantics.
struct ORION_REMOTEPLAY_API SharedMemoryFramePumpBatch final {
    quint64 sourceEpoch = 0;
    bool readerOpen = false;
    int openFailureRun = 0;
    QString openError;
    int readFailureRun = 0;
    QString readError;
    // Distinct named-event integrity failure. A non-zero run means the mapping
    // advanced after bounded event timeouts, so the consumer can coordinate an
    // in-place JPEG fallback instead of waiting forever on the wrong event.
    int notificationFailureRun = 0;
    QString notificationError;
    quint64 framesRead = 0;
    QImage image;
    // At most two preceding frames, oldest first. The newest frame remains in
    // image for health/latency diagnostics and freshness-first fallback. A short
    // GUI scheduling hiccup must not empty the downstream presentation reserve.
    std::vector<SharedMemoryFramePumpImage> precedingFrames;
    int mappedFrameNumber = 0;
    int eventFrameNumber = 0;
    // The producer timestamp is read from the same committed SHM header as
    // ``image``. The other stamps quantify pump/callback delay without ever
    // laundering old pixels with a consumer-side freshness timestamp.
    quint64 sourceTimestampNs = 0;
    quint64 pumpReadCompletedTimestampNs = 0;
    quint64 presentationDispatchTimestampNs = 0;
    // Cumulative pump observability exported with every worker-state delivery.
    quint64 eventWaitTimeouts = 0;
    quint64 eventWaitFailures = 0;
    quint64 eventGenerationProbes = 0;
    quint64 eventNotificationLosses = 0;
    quint64 readyFrameReplaced = 0;
    quint64 deliveryScheduleFailures = 0;
};

struct ORION_REMOTEPLAY_API SharedMemoryFramePumpStats final {
    quint64 submitted = 0;
    quint64 notificationCoalesced = 0;
    quint64 eventSignals = 0;
    quint64 eventWaitTimeouts = 0;
    quint64 eventWaitFailures = 0;
    quint64 eventGenerationProbes = 0;
    quint64 eventGenerationProbeFailures = 0;
    quint64 eventNotificationLosses = 0;
    quint64 openAttempts = 0;
    quint64 openFailures = 0;
    quint64 readAttempts = 0;
    quint64 framesRead = 0;
    quint64 ordinaryNoFrame = 0;
    quint64 readFaults = 0;
    quint64 readyFrameReplaced = 0;
    quint64 staleCompletionDropped = 0;
    quint64 deliveredBatches = 0;
    quint64 deliveryScheduleFailures = 0;
    std::size_t maxPendingDepth = 0;
    std::size_t maxReadyDepth = 0;
    bool workerObserved = false;
    bool workerDifferentFromOwner = false;
};

// Persistent off-GUI-thread reader for the display-only shared-memory preview.
//
// There is exactly one in-flight read, one pending latest notification, one
// bounded three-image ready burst, and at most one queued GUI wake-up. No extra
// holdback is introduced: every wake immediately drains the available burst.
// Preceding images older than 50 ms at delivery are dropped, and overflow keeps
// only the newest three. The presentation queue's own capacity stays unchanged.
// Detector frames and
// timing never enter this object. Every result is tagged with a source epoch;
// a sidecar restart/retire invalidates an in-flight completion before delivery.
class ORION_REMOTEPLAY_API SharedMemoryFramePump final : public QObject {
    Q_OBJECT
public:
    static constexpr std::size_t kMaxReadyFrames = 3;
    static constexpr quint64 kMaxBufferedAgeNs = 50'000'000;

    explicit SharedMemoryFramePump(QObject* parent = nullptr);
    explicit SharedMemoryFramePump(
        std::unique_ptr<SharedMemoryFrameSource> source,
        QObject* parent = nullptr);
    ~SharedMemoryFramePump() override;

    SharedMemoryFramePump(const SharedMemoryFramePump&) = delete;
    SharedMemoryFramePump& operator=(const SharedMemoryFramePump&) = delete;

    // Begin a fresh sidecar/mapping identity. Epoch zero is reserved and
    // ignored. These methods are thread-safe; source open/close always occurs
    // on the worker thread.
    void beginSourceEpoch(
        quint64 sourceEpoch,
        SharedMemoryTransportNames transportNames = {});
    void retireSourceEpoch(quint64 sourceEpoch);
    void submitFrameNotification(quint64 sourceEpoch, int eventFrameNumber);

    [[nodiscard]] SharedMemoryFramePumpStats stats() const noexcept;

signals:
    // Emitted by the pump object on its affinity thread (normally GUI). The
    // batch owns its QImage; it never aliases producer-controlled memory.
    void batchReady(const orion::SharedMemoryFramePumpBatch& batch);

private:
    void threadLoop();
    void publishWorkerState(
        quint64 sourceEpoch,
        bool readerOpen,
        int openFailureRun,
        const QString& openError,
        int readFailureRun,
        const QString& readError,
        int notificationFailureRun,
        const QString& notificationError,
        quint64 framesRead,
        QImage image = {},
        int mappedFrameNumber = 0,
        int eventFrameNumber = 0,
        quint64 sourceTimestampNs = 0,
        quint64 pumpReadCompletedTimestampNs = 0);
    void deliverLatest();
    [[nodiscard]] bool epochIsCurrent(quint64 sourceEpoch) const noexcept;

    std::unique_ptr<SharedMemoryFrameSource> source_;
    std::thread thread_;
    const std::thread::id ownerThreadId_;

    mutable std::mutex mutex_;
    std::condition_variable cv_;
    bool stop_ = false;

    quint64 desiredEpoch_ = 0;
    SharedMemoryTransportNames desiredTransportNames_;
    bool desiredActive_ = false;
    bool resetPending_ = false;
    bool hasPending_ = false;
    quint64 pendingEpoch_ = 0;
    int pendingEventFrameNumber_ = 0;

    bool readyValid_ = false;
    SharedMemoryFramePumpBatch readyBatch_;
    bool deliveryScheduled_ = false;

    SharedMemoryFramePumpStats stats_;
};

} // namespace orion

Q_DECLARE_METATYPE(orion::SharedMemoryFramePumpBatch)
