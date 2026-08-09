#include "OrderedFileLogSink.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>

#include <algorithm>
#include <chrono>
#include <limits>
#include <utility>

namespace orion {

namespace {

OrderedFileLogSink::Options sanitizedOptions(OrderedFileLogSink::Options options)
{
    options.maxFileBytes = std::max<qint64>(1, options.maxFileBytes);
    options.rotationCheckBatches = std::max(1, options.rotationCheckBatches);
    options.maxOutstandingBytes = std::max<qsizetype>(1, options.maxOutstandingBytes);
    options.retryDelayMs = std::max(1, options.retryDelayMs);
    return options;
}

} // namespace

OrderedFileLogSink::OrderedFileLogSink(QString logPath)
    : OrderedFileLogSink(std::move(logPath), Options{})
{
}

OrderedFileLogSink::OrderedFileLogSink(QString logPath, Options options)
    : logPath_(std::move(logPath)),
      options_(sanitizedOptions(options)),
      worker_(&OrderedFileLogSink::run, this)
{
}

OrderedFileLogSink::~OrderedFileLogSink()
{
    stopAndDrain();
}

bool OrderedFileLogSink::enqueue(const QStringList& lines)
{
    if (lines.isEmpty()) {
        return true;
    }

    // This mutex covers encoding and chunk admission. Therefore accepted
    // batches have one total order even when multiple producers call at once.
    std::unique_lock enqueueLock(enqueueOrderMutex_);
    {
        std::lock_guard stateLock(stateMutex_);
        if (stopping_) {
            return false;
        }
    }

    QByteArray payload;
    for (const QString& line : lines) {
        payload.append(line.toUtf8());
#ifdef Q_OS_WIN
        // Match QFile's former QIODevice::Text output byte-for-byte.
        payload.append("\r\n", 2);
#else
        payload.append('\n');
#endif
    }

    qsizetype offset = 0;
    bool firstChunk = true;
    {
        std::lock_guard stateLock(stateMutex_);
        ++stats_.acceptedBatches;
        stats_.acceptedLines += static_cast<quint64>(lines.size());
    }

    while (offset < payload.size()) {
        std::unique_lock stateLock(stateMutex_);
        capacityAvailable_.wait(stateLock, [this] {
            return stats_.outstandingBytes < options_.maxOutstandingBytes;
        });

        const qsizetype available = options_.maxOutstandingBytes - stats_.outstandingBytes;
        const qsizetype chunkSize = std::min(available, payload.size() - offset);
        PendingChunk chunk;
        chunk.bytes = payload.mid(offset, chunkSize);
        chunk.checkRotation = firstChunk;
        offset += chunkSize;
        firstChunk = false;
        if (offset == payload.size()) {
            chunk.completedBatchLines = static_cast<quint64>(lines.size());
        }

        stats_.outstandingBytes += chunkSize;
        stats_.maxObservedOutstandingBytes =
            std::max(stats_.maxObservedOutstandingBytes, stats_.outstandingBytes);
        queue_.push_back(std::move(chunk));
        stateLock.unlock();
        workAvailable_.notify_one();
    }
    return true;
}

void OrderedFileLogSink::drain()
{
    std::unique_lock lock(stateMutex_);
    drained_.wait(lock, [this] {
        return queue_.empty() && !writeActive_ && stats_.outstandingBytes == 0;
    });
}

void OrderedFileLogSink::stopAndDrain()
{
    // Joining a std::thread from two concurrent shutdown paths is undefined.
    // Serialize the full stop/join sequence and make it idempotent.
    std::lock_guard lifecycleLock(lifecycleMutex_);
    {
        // Wait for a batch already being admitted in bounded chunks. This
        // guarantees shutdown cannot stop halfway through an accepted batch.
        std::lock_guard enqueueLock(enqueueOrderMutex_);
        std::lock_guard stateLock(stateMutex_);
        stopping_ = true;
    }
    workAvailable_.notify_all();
    capacityAvailable_.notify_all();
    if (worker_.joinable()) {
        worker_.join();
    }
}

OrderedFileLogSink::Stats OrderedFileLogSink::stats() const
{
    std::lock_guard lock(stateMutex_);
    return stats_;
}

void OrderedFileLogSink::run()
{
    {
        std::lock_guard lock(stateMutex_);
        stats_.workerStarted = true;
    }

    for (;;) {
        PendingChunk chunk;
        {
            std::unique_lock lock(stateMutex_);
            workAvailable_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
            if (queue_.empty()) {
                if (stopping_) {
                    stats_.workerStopped = true;
                    drained_.notify_all();
                    return;
                }
                continue;
            }
            chunk = std::move(queue_.front());
            queue_.pop_front();
            writeActive_ = true;
        }

        if (chunk.checkRotation) {
            rotateIfNeeded();
        }
        writeLosslessly(chunk.bytes);

        {
            std::lock_guard lock(stateMutex_);
            stats_.outstandingBytes -= chunk.bytes.size();
            if (chunk.completedBatchLines > 0) {
                ++stats_.writtenBatches;
                stats_.writtenLines += chunk.completedBatchLines;
            }
            writeActive_ = false;
            if (queue_.empty() && stats_.outstandingBytes == 0) {
                drained_.notify_all();
            }
        }
        capacityAvailable_.notify_all();
    }
}

void OrderedFileLogSink::rotateIfNeeded()
{
    if (--rotationCountdown_ > 0) {
        return;
    }
    rotationCountdown_ = options_.rotationCheckBatches;

    const QFileInfo info(logPath_);
    if (!info.exists() || info.size() < options_.maxFileBytes) {
        return;
    }

    const QString previous = logPath_ + QStringLiteral(".1");
    QFile::remove(previous);
    QFile::remove(logPath_ + QStringLiteral(".2"));
    if (!QFile::rename(logPath_, previous)) {
        QFile current(logPath_);
        if (current.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            current.close();
        }
    }
}

void OrderedFileLogSink::writeLosslessly(const QByteArray& bytes)
{
    qsizetype offset = 0;
    while (offset < bytes.size()) {
        const QFileInfo info(logPath_);
        if (!QDir().mkpath(info.absolutePath())) {
            waitBeforeRetry();
            continue;
        }

        QFile file(logPath_);
        if (!file.open(QIODevice::WriteOnly | QIODevice::Append)) {
            waitBeforeRetry();
            continue;
        }

        while (offset < bytes.size()) {
            const qsizetype remaining = bytes.size() - offset;
            const qint64 request = std::min<qsizetype>(remaining, std::numeric_limits<qint64>::max());
            const qint64 written = file.write(bytes.constData() + offset, request);
            if (written <= 0) {
                // Preserve any prefix already accepted by this handle before
                // reopening at EOF for the remainder.
                while (!file.flush()) {
                    waitBeforeRetry();
                }
                break;
            }
            offset += written;
        }

        if (offset < bytes.size()) {
            file.close();
            waitBeforeRetry();
            continue;
        }

        // Bytes have already been accepted exactly once. If persistence is
        // temporarily unavailable, retry flush on this handle instead of
        // reopening and duplicating the batch.
        while (!file.flush()) {
            waitBeforeRetry();
        }
        file.close();
    }
}

void OrderedFileLogSink::waitBeforeRetry() const
{
    std::this_thread::sleep_for(std::chrono::milliseconds(options_.retryDelayMs));
}

} // namespace orion
