#pragma once

#include "OrionExports.h"

#include <QtCore/QObject>
#include <QtCore/QByteArray>
#include <QtGui/QImage>

#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <thread>

namespace orion {

struct FrameDecoderStats {
    quint64 submitted = 0;
    quint64 decodeMailboxDropped = 0;
    quint64 decoded = 0;
    quint64 presented = 0;
    quint64 presentationMailboxDropped = 0;
};

// Off-GUI-thread preview JPEG decoder.
//
// The autogreen/preview sidecar streams each preview frame as a base64 JPEG line over stdout, up to
// ~60x/sec. Decoding it (QByteArray::fromBase64 of ~270 KB + QImage::loadFromData JPEG IDCT of a
// 1280x720 image) used to run inline in RemotePlaySession::handleSidecarMessage on the Qt GUI/render
// thread -- the single biggest preview-stutter source, because the same thread drives Qt Quick.
//
// FrameDecoder owns ONE dedicated std::thread and two 1-deep drop-oldest mailboxes. submit() (called
// on the GUI thread) stores only the latest base64 payload for the worker. Successful decodes then
// enter a second, decoded-image mailbox with at most ONE queued GUI wake-up. This second bound is
// important: queuing one QMetaObject lambda/QImage per decode merely moved an unbounded backlog from
// before JPEG decode to after it whenever Qt Quick presented slower than the worker decoded. Both
// stages are now newest-frame-wins, so stale frames cannot accumulate latency or retain large image
// buffers. QImage is implicitly shared and safe to hand across threads by value.
class ORION_REMOTEPLAY_API FrameDecoder final : public QObject {
    Q_OBJECT
public:
    explicit FrameDecoder(QObject* parent = nullptr);
    ~FrameDecoder() override;

    FrameDecoder(const FrameDecoder&) = delete;
    FrameDecoder& operator=(const FrameDecoder&) = delete;

    // Hand a base64-encoded JPEG payload to the worker. Thread-safe; intended to be called from the
    // GUI thread. Drop-oldest: replaces any still-undecoded payload so the queue never grows beyond
    // one in-flight decode + one pending payload.
    //
    // frameNumber is the sidecar's decoder frame seq for this JPEG; it is carried alongside the
    // payload and re-emitted with decoded() so the overlay can join the detection bbox to the exact
    // preview frame it was detected on (FRAME-ID JOIN). -1 when unknown (placeholder frames).
    void submit(QByteArray base64Jpeg, int frameNumber);

    // Diagnostics. stats() takes one lock so a telemetry sample cannot combine
    // counters from different instants while the worker is completing a decode.
    [[nodiscard]] FrameDecoderStats stats() const noexcept;
    [[nodiscard]] quint64 submittedCount() const noexcept;
    [[nodiscard]] quint64 droppedCount() const noexcept;
    [[nodiscard]] quint64 decodedCount() const noexcept;
    [[nodiscard]] quint64 presentedCount() const noexcept;
    [[nodiscard]] quint64 presentationDroppedCount() const noexcept;

signals:
    // Emitted on the thread FrameDecoder lives on (GUI thread) once a payload is decoded. frameNumber
    // is the decoder frame seq that was submitted with this payload (FRAME-ID JOIN key; -1 if unknown).
    void decoded(QImage image, int frameNumber);

private:
    void threadLoop();
    void deliverLatest();

    std::thread thread_;
    mutable std::mutex mutex_;
    std::condition_variable cv_;
    // Keep the wire payload as bytes end-to-end.  The sidecar already emits ASCII
    // base64; widening every frame to UTF-16 on the GUI thread only to narrow it
    // back to Latin-1 on this worker doubled the preview traffic and caused visible
    // presentation stalls under 60 Hz capture-card load.
    QByteArray pending_;
    int pendingFrame_ = -1;
    bool hasPending_ = false;
    // Successful worker decodes cross to the GUI through another one-slot mailbox. A single queued
    // deliverLatest() wake-up drains the newest image; further decodes replace readyImage_ instead
    // of appending QImages to Qt's event queue while the GUI/render thread is busy.
    QImage readyImage_;
    int readyFrame_ = -1;
    bool hasReady_ = false;
    bool deliveryScheduled_ = false;
    bool stop_ = false;
    quint64 submitted_ = 0;
    quint64 dropped_ = 0;
    quint64 decoded_ = 0;
    quint64 presented_ = 0;
    quint64 presentationDropped_ = 0;
};

} // namespace orion
