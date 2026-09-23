#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QtTypes>

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <mutex>
#include <thread>

namespace orion {

// Ordered, lossless append sink for the native diagnostic log. Callers retain
// ownership of the UI/ring state; this class owns every filesystem operation.
// Queue bytes include the chunk currently being written, so the configured
// bound covers all accepted payload retained by the sink.
class OrderedFileLogSink final {
public:
    struct Options {
        qint64 maxFileBytes = 16 * 1024 * 1024;
        int rotationCheckBatches = 300;
        qsizetype maxOutstandingBytes = 8 * 1024 * 1024;
        int retryDelayMs = 25;
        // [M-02 / CX-015 2026-09-22] Liveness bounds. A producer (the GUI thread, which also carries
        // the 4 ms input tick) waits at most admissionWaitMs for queue space while storage is
        // HEALTHY, and not at all while the writer is failing: a full/denied/unplugged disk drops
        // diagnostic batches (counted) instead of freezing input. Once stopping, the writer gives
        // up retrying after shutdownGiveUpMs so shutdown cannot hang on a dead disk.
        int admissionWaitMs = 250;
        int shutdownGiveUpMs = 2000;
    };

    struct Stats {
        quint64 acceptedBatches = 0;
        quint64 acceptedLines = 0;
        quint64 writtenBatches = 0;
        quint64 writtenLines = 0;
        qsizetype outstandingBytes = 0;
        qsizetype maxObservedOutstandingBytes = 0;
        bool workerStarted = false;
        bool workerStopped = false;
        // [M-02] Storage-fault visibility: the writer is currently retrying a failed
        // open/write/flush; episodes counted; batches/lines/bytes dropped instead of blocking.
        bool storageFault = false;
        quint64 storageFaultEpisodes = 0;
        quint64 droppedBatches = 0;
        quint64 droppedLines = 0;
        quint64 droppedBytes = 0;
    };

    explicit OrderedFileLogSink(QString logPath);
    OrderedFileLogSink(QString logPath, Options options);
    ~OrderedFileLogSink();

    OrderedFileLogSink(const OrderedFileLogSink&) = delete;
    OrderedFileLogSink& operator=(const OrderedFileLogSink&) = delete;

    // Accepts the whole batch in FIFO order. At the queue byte limit this call backpressures for
    // at most Options::admissionWaitMs while storage is healthy; while the writer is failing it
    // drops (and counts) instead of blocking. Returns false only after stop.
    [[nodiscard]] bool enqueue(const QStringList& lines);
    // Waits for every accepted byte to reach the file, at most `timeout`. True when drained.
    bool drain(std::chrono::milliseconds timeout = std::chrono::milliseconds(2000));
    void stopAndDrain();
    [[nodiscard]] Stats stats() const;

private:
    struct PendingChunk {
        QByteArray bytes;
        bool checkRotation = false;
        quint64 completedBatchLines = 0;
    };

    void run();
    void rotateIfNeeded();
    // False when retries were abandoned (stopping and past shutdownGiveUpMs).
    bool writeLosslessly(const QByteArray& bytes);
    // False when the caller must abandon the current write instead of retrying.
    bool waitBeforeRetry();
    void setStorageFault(bool faulted);

    const QString logPath_;
    const Options options_;

    // Serializes whole enqueue operations. A batch larger than the byte budget
    // is chunked while holding this mutex, so another producer cannot interleave.
    std::mutex enqueueOrderMutex_;
    mutable std::mutex stateMutex_;
    std::mutex lifecycleMutex_;
    std::condition_variable workAvailable_;
    std::condition_variable capacityAvailable_;
    std::condition_variable drained_;
    std::deque<PendingChunk> queue_;
    Stats stats_;
    bool stopping_ = false;
    std::chrono::steady_clock::time_point stopRequestedAt_{};
    bool writeActive_ = false;
    int rotationCountdown_ = 0;
    std::thread worker_;
};

} // namespace orion
