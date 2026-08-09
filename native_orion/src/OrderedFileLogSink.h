#pragma once

#include <QtCore/QByteArray>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QtTypes>

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
    };

    explicit OrderedFileLogSink(QString logPath);
    OrderedFileLogSink(QString logPath, Options options);
    ~OrderedFileLogSink();

    OrderedFileLogSink(const OrderedFileLogSink&) = delete;
    OrderedFileLogSink& operator=(const OrderedFileLogSink&) = delete;

    // Accepts the whole batch in FIFO order. At the queue byte limit this call
    // backpressures instead of dropping diagnostic/security lines.
    [[nodiscard]] bool enqueue(const QStringList& lines);
    void drain();
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
    void writeLosslessly(const QByteArray& bytes);
    void waitBeforeRetry() const;

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
    bool writeActive_ = false;
    int rotationCountdown_ = 0;
    std::thread worker_;
};

} // namespace orion
