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
    options.admissionWaitMs = std::max(0, options.admissionWaitMs);
    options.shutdownGiveUpMs = std::max(0, options.shutdownGiveUpMs);
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
    // [M-02 r2 2026-09-23, Codex] ONE admission deadline for the whole batch, not one per chunk.
    const auto admissionDeadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(options_.admissionWaitMs);
    {
        std::lock_guard stateLock(stateMutex_);
        ++stats_.acceptedBatches;
        stats_.acceptedLines += static_cast<quint64>(lines.size());
    }

    while (offset < payload.size()) {
        std::unique_lock stateLock(stateMutex_);
        const auto hasCapacity = [this] {
            return stats_.outstandingBytes < options_.maxOutstandingBytes;
        };
        // [M-02] Never block unboundedly: no wait at all while the writer is failing, a bounded
        // wait while it is healthy. Out of time or faulted -> drop the rest of this batch.
        const bool admitted = hasCapacity()
            || (!stats_.storageFault
                && capacityAvailable_.wait_until(
                       stateLock, admissionDeadline,
                       [&] { return hasCapacity() || stats_.storageFault; })
                && hasCapacity());
        if (!admitted) {
            ++stats_.droppedBatches;
            stats_.droppedLines += static_cast<quint64>(lines.size());
            stats_.droppedBytes += static_cast<quint64>(payload.size() - offset);
            return true;
        }

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

bool OrderedFileLogSink::drain(std::chrono::milliseconds timeout)
{
    // [M-02 r2 2026-09-23, Codex] Bounded: a dead disk must not freeze the GUI thread in Copy
    // Activity Log or at shutdown. False = not everything reached the file in time.
    std::unique_lock lock(stateMutex_);
    return drained_.wait_for(lock, timeout, [this] {
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
        stopRequestedAt_ = std::chrono::steady_clock::now();
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
        const bool written = writeLosslessly(chunk.bytes);

        {
            std::lock_guard lock(stateMutex_);
            stats_.outstandingBytes -= chunk.bytes.size();
            if (!written) {
                ++stats_.droppedBatches;
                stats_.droppedBytes += static_cast<quint64>(chunk.bytes.size());
                stats_.droppedLines += chunk.completedBatchLines;
            } else if (chunk.completedBatchLines > 0) {
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

bool OrderedFileLogSink::writeLosslessly(const QByteArray& bytes)
{
    qsizetype offset = 0;
    while (offset < bytes.size()) {
        const QFileInfo info(logPath_);
        if (!QDir().mkpath(info.absolutePath())) {
            if (!waitBeforeRetry()) {
                return false;
            }
            continue;
        }

        QFile file(logPath_);
        if (!file.open(QIODevice::WriteOnly | QIODevice::Append)) {
            if (!waitBeforeRetry()) {
                return false;
            }
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
                    if (!waitBeforeRetry()) {
                        return false;
                    }
                }
                break;
            }
            offset += written;
        }

        if (offset < bytes.size()) {
            file.close();
            if (!waitBeforeRetry()) {
                return false;
            }
            continue;
        }

        // Bytes have already been accepted exactly once. If persistence is
        // temporarily unavailable, retry flush on this handle instead of
        // reopening and duplicating the batch.
        while (!file.flush()) {
            if (!waitBeforeRetry()) {
                return false;
            }
        }
        file.close();
    }
    setStorageFault(false);
    return true;
}

bool OrderedFileLogSink::waitBeforeRetry()
{
    setStorageFault(true);
    {
        std::lock_guard lock(stateMutex_);
        if (stopping_
            && std::chrono::steady_clock::now() - stopRequestedAt_
                   >= std::chrono::milliseconds(options_.shutdownGiveUpMs)) {
            return false;   // [M-02] a dead disk must not hang shutdown
        }
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(options_.retryDelayMs));
    return true;
}

void OrderedFileLogSink::setStorageFault(bool faulted)
{
    bool wake = false;
    {
        std::lock_guard lock(stateMutex_);
        if (faulted && !stats_.storageFault) {
            ++stats_.storageFaultEpisodes;
            wake = true;   // producers waiting for capacity must stop waiting and drop
        }
        stats_.storageFault = faulted;
    }
    if (wake) {
        capacityAvailable_.notify_all();
    }
}

} // namespace orion
