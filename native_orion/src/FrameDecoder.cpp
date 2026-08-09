#include "FrameDecoder.h"

#include <QtCore/QByteArray>
#include <QtCore/QMetaObject>

#include <utility>

namespace orion {

FrameDecoder::FrameDecoder(QObject* parent)
    : QObject(parent)
{
    thread_ = std::thread([this]() { threadLoop(); });
}

FrameDecoder::~FrameDecoder()
{
    {
        std::lock_guard<std::mutex> lk(mutex_);
        stop_ = true;
        // Release any not-yet-presented image while the object is still intact. A queued
        // deliverLatest() is context-bound to this QObject and Qt removes it in QObject's
        // destructor; the worker is joined below so no new wake-up can be posted afterward.
        hasPending_ = false;
        pending_.clear();
        hasReady_ = false;
        readyImage_ = QImage{};
    }
    cv_.notify_all();
    if (thread_.joinable()) {
        thread_.join();
    }
}

void FrameDecoder::submit(QByteArray base64Jpeg, int frameNumber)
{
    {
        std::lock_guard<std::mutex> lk(mutex_);
        if (stop_) {
            return;
        }
        // Drop-oldest: if a payload is still waiting to be decoded, discard it — the display only ever
        // wants the newest frame, so overwriting keeps latency at zero instead of building a backlog.
        if (hasPending_) {
            ++dropped_;
        }
        pending_ = std::move(base64Jpeg);
        pendingFrame_ = frameNumber;
        hasPending_ = true;
        ++submitted_;
    }
    cv_.notify_one();
}

FrameDecoderStats FrameDecoder::stats() const noexcept
{
    std::lock_guard<std::mutex> lk(mutex_);
    return FrameDecoderStats{
        submitted_, dropped_, decoded_, presented_, presentationDropped_};
}

quint64 FrameDecoder::submittedCount() const noexcept
{
    std::lock_guard<std::mutex> lk(mutex_);
    return submitted_;
}

quint64 FrameDecoder::droppedCount() const noexcept
{
    std::lock_guard<std::mutex> lk(mutex_);
    return dropped_;
}

quint64 FrameDecoder::decodedCount() const noexcept
{
    std::lock_guard<std::mutex> lk(mutex_);
    return decoded_;
}

quint64 FrameDecoder::presentedCount() const noexcept
{
    std::lock_guard<std::mutex> lk(mutex_);
    return presented_;
}

quint64 FrameDecoder::presentationDroppedCount() const noexcept
{
    std::lock_guard<std::mutex> lk(mutex_);
    return presentationDropped_;
}

void FrameDecoder::threadLoop()
{
    for (;;) {
        QByteArray b64;
        int frameNumber = -1;
        {
            std::unique_lock<std::mutex> lk(mutex_);
            cv_.wait(lk, [this]() { return hasPending_ || stop_; });
            if (stop_) {
                return;
            }
            b64 = std::move(pending_);
            frameNumber = pendingFrame_;
            hasPending_ = false;
        }

        // Heavy work, OFF the GUI thread: base64 decode + JPEG IDCT.  The mailbox
        // stores the sidecar's ASCII bytes directly, so no GUI-thread UTF-16
        // widening and no worker-thread Latin-1 narrowing are required.
        const QByteArray jpegBytes = QByteArray::fromBase64(b64);
        if (jpegBytes.isEmpty()) {
            continue;
        }
        QImage image;
        if (!image.loadFromData(jpegBytes, "JPEG")) {
            continue;
        }
        bool scheduleDelivery = false;
        {
            std::lock_guard<std::mutex> lk(mutex_);
            // Destruction can begin while JPEG IDCT is in flight. Never publish or queue another
            // QObject invocation once the destructor has asked the worker to stop.
            if (stop_) {
                return;
            }
            ++decoded_;
            if (hasReady_) {
                ++presentationDropped_;
            }
            readyImage_ = std::move(image);
            readyFrame_ = frameNumber;
            hasReady_ = true;
            if (!deliveryScheduled_) {
                deliveryScheduled_ = true;
                scheduleDelivery = true;
            }
        }

        if (scheduleDelivery) {
            // Queue only a wake-up, not one QImage-owning closure per decoded frame. While this is
            // pending the worker may replace readyImage_ any number of times without growing Qt's
            // event queue. The QObject context also cancels the wake-up automatically on teardown.
            const bool queued = QMetaObject::invokeMethod(
                this, [this]() { deliverLatest(); }, Qt::QueuedConnection);
            if (!queued) {
                // No event dispatcher/object context means the image cannot be presented. Restore
                // the invariant and release the buffer rather than permanently wedging the mailbox.
                std::lock_guard<std::mutex> lk(mutex_);
                deliveryScheduled_ = false;
                if (hasReady_) {
                    ++presentationDropped_;
                    hasReady_ = false;
                    readyImage_ = QImage{};
                    readyFrame_ = -1;
                }
            }
        }
    }
}

void FrameDecoder::deliverLatest()
{
    QImage image;
    int frameNumber = -1;
    {
        std::lock_guard<std::mutex> lk(mutex_);
        // This function runs on the QObject/GUI thread. Clear the scheduled flag while holding the
        // same lock used by the worker: a decode racing after this snapshot will schedule exactly
        // one new wake-up, while a decode racing before it is included in this newest-frame snapshot.
        deliveryScheduled_ = false;
        if (stop_ || !hasReady_) {
            return;
        }
        image = std::move(readyImage_);
        frameNumber = readyFrame_;
        hasReady_ = false;
        readyFrame_ = -1;
        ++presented_;
    }

    // No member access after emit: a direct receiver is allowed to tear down its owning session.
    emit decoded(std::move(image), frameNumber);
}

} // namespace orion
