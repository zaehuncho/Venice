#pragma once

#include "OrionExports.h"

#include <QtCore/QString>
#include <QtGui/QImage>

#include <cstdint>
#include <mutex>

namespace orion {

struct ORION_REMOTEPLAY_API SharedMemoryTransportNames final {
    QString mappingName = QStringLiteral("OrionPreviewFrame_v1");
    QString mutexName = QStringLiteral("OrionPreviewMutex");
    QString readyEventName;

    [[nodiscard]] bool eventNotificationsEnabled() const noexcept {
        return !readyEventName.isEmpty();
    }
};

enum class SharedMemoryFrameWaitResult {
    Ready,
    Interrupted,
    Timeout,
    Unsupported,
    Failed,
};

// Bounded reader for shm_frame_bridge.py's fixed 1280x720 BGR mapping.
// readFrame() always returns an owning QImage copy; the QML/render path never
// aliases memory that the Python producer can overwrite.
class ORION_REMOTEPLAY_API SharedMemoryFrameReader final {
public:
    SharedMemoryFrameReader() = default;
    ~SharedMemoryFrameReader();

    SharedMemoryFrameReader(const SharedMemoryFrameReader&) = delete;
    SharedMemoryFrameReader& operator=(const SharedMemoryFrameReader&) = delete;

    void configureTransport(SharedMemoryTransportNames names);
    bool open();
    void close();
    SharedMemoryFrameWaitResult waitForFrameReady(int timeoutMs);
    void interruptWait() noexcept;

    // Inspect only the committed header under the named mutex. This diagnostic
    // does not consume/update the latest-frame identity; the pump uses it after
    // bounded ready-event timeouts to distinguish a paused producer from a lost
    // or mismatched event whose mapping is still advancing.
    bool probeGeneration(std::uint64_t& writeCount);

    // Returns null when no newer mapping generation exists or a bounded read
    // failed.  lastError() is empty for the ordinary no-new-frame case.
    QImage readFrame(int& frameNumber);

    [[nodiscard]] bool isOpen() const noexcept { return view_ != nullptr; }
    [[nodiscard]] QString lastError() const { return lastError_; }
    [[nodiscard]] std::uint64_t framesRead() const noexcept { return framesRead_; }
    [[nodiscard]] std::uint64_t lastReadGeneration() const noexcept {
        return lastWriteCount_;
    }
    // Producer QPC/perf-counter timestamp copied from the same committed header
    // as the returned pixels. It is never replaced with reader/callback time, so
    // presentation diagnostics preserve producer-to-display age.
    [[nodiscard]] std::uint64_t lastReadTimestampNs() const noexcept {
        return lastTimestampNs_;
    }

private:
    void closeMappingHandles();
    void closeNotificationHandles();

    SharedMemoryTransportNames names_;
    void* mapping_ = nullptr;  // HANDLE
    void* mutex_ = nullptr;    // HANDLE
    void* view_ = nullptr;
    void* readyEvent_ = nullptr;      // named auto-reset HANDLE
    void* interruptEvent_ = nullptr;  // local manual-reset HANDLE
    mutable std::mutex waitHandleMutex_;
    std::uint64_t lastWriteCount_ = 0;
    std::uint64_t lastTimestampNs_ = 0;
    std::uint64_t framesRead_ = 0;
    // Last validated mapping geometry. The destination for the steady-state
    // read is allocated before waiting on the inter-process mutex, so the
    // producer is blocked only by header validation + the immutable pixel copy.
    int cachedWidth_ = 0;
    int cachedHeight_ = 0;
    QString lastError_;
};

} // namespace orion
