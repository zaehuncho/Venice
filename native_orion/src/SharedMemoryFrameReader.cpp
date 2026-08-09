#include "SharedMemoryFrameReader.h"

#ifdef Q_OS_WIN
#include <windows.h>
#endif

#include <QtCore/QtGlobal>

#include <algorithm>
#include <cstring>
#include <limits>
#include <string>
#include <utility>

namespace orion {
namespace {

constexpr quint32 kMagic = 0x4F52464D; // "ORFM"
constexpr quint32 kVersion = 1;
constexpr size_t kHeaderSize = 64;
constexpr quint32 kMaxWidth = 1280;
constexpr quint32 kMaxHeight = 720;
constexpr quint32 kChannels = 3;
constexpr size_t kMaxBodySize =
    static_cast<size_t>(kMaxWidth) * kMaxHeight * kChannels;
// Keep protocol constants portable even though the implementation is enabled
// only on Windows; DWORD is not available to non-Windows Qt builds.
constexpr unsigned long kMutexWaitMs = 3;

#pragma pack(push, 1)
struct ShmHeader {
    quint32 magic;
    quint32 version;
    quint32 width;
    quint32 height;
    quint32 channels;
    quint32 frameNumber;
    quint64 timestampNs;
    quint64 writeCount;
    quint8 reserved[24];
};
#pragma pack(pop)

static_assert(sizeof(ShmHeader) == kHeaderSize);

bool validKernelObjectName(const QString& name) noexcept
{
    if (name.isEmpty() || name.size() > 180) {
        return false;
    }
    for (const QChar ch : name) {
        const ushort value = ch.unicode();
        const bool asciiAlphaNumeric =
            (value >= 'a' && value <= 'z')
            || (value >= 'A' && value <= 'Z')
            || (value >= '0' && value <= '9');
        if (!(asciiAlphaNumeric || value == '_' || value == '.' || value == '-')) {
            return false;
        }
    }
    return true;
}

} // namespace

SharedMemoryFrameReader::~SharedMemoryFrameReader()
{
    close();
}

void SharedMemoryFrameReader::configureTransport(SharedMemoryTransportNames names)
{
    close();
    names_ = std::move(names);
}

bool SharedMemoryFrameReader::open()
{
    closeMappingHandles();
#ifdef Q_OS_WIN
    if (!validKernelObjectName(names_.mappingName)
        || !validKernelObjectName(names_.mutexName)) {
        lastError_ = QStringLiteral("SHM transport object name is invalid");
        return false;
    }
    const std::wstring mappingName = names_.mappingName.toStdWString();
    mapping_ = OpenFileMappingW(FILE_MAP_READ, FALSE, mappingName.c_str());
    if (!mapping_) {
        lastError_ = QStringLiteral("OpenFileMappingW failed (winerr=%1)").arg(GetLastError());
        return false;
    }

    // ReleaseMutex requires MUTEX_MODIFY_STATE; SYNCHRONIZE alone can wait but
    // cannot release ownership and wedges the producer after the first read.
    const std::wstring mutexName = names_.mutexName.toStdWString();
    mutex_ = OpenMutexW(
        SYNCHRONIZE | MUTEX_MODIFY_STATE, FALSE, mutexName.c_str());
    if (!mutex_) {
        lastError_ = QStringLiteral("OpenMutexW failed (winerr=%1)").arg(GetLastError());
        CloseHandle(static_cast<HANDLE>(mapping_));
        mapping_ = nullptr;
        return false;
    }

    // Map exactly the protocol size.  A corrupt header can therefore never
    // make the reader walk beyond the known section boundary.
    view_ = MapViewOfFile(
        static_cast<HANDLE>(mapping_), FILE_MAP_READ, 0, 0,
        kHeaderSize + kMaxBodySize);
    if (!view_) {
        lastError_ = QStringLiteral("MapViewOfFile failed (winerr=%1)").arg(GetLastError());
        CloseHandle(static_cast<HANDLE>(mutex_));
        mutex_ = nullptr;
        CloseHandle(static_cast<HANDLE>(mapping_));
        mapping_ = nullptr;
        return false;
    }
    lastError_.clear();
    return true;
#else
    lastError_ = QStringLiteral("shared-memory preview is Windows-only");
    return false;
#endif
}

SharedMemoryFrameWaitResult SharedMemoryFrameReader::waitForFrameReady(int timeoutMs)
{
    lastError_.clear();
#ifdef Q_OS_WIN
    if (names_.readyEventName.isEmpty()) {
        return SharedMemoryFrameWaitResult::Unsupported;
    }
    if (!validKernelObjectName(names_.readyEventName)) {
        lastError_ = QStringLiteral("SHM frame-ready event name is invalid");
        return SharedMemoryFrameWaitResult::Failed;
    }

    HANDLE ready = nullptr;
    HANDLE interrupt = nullptr;
    {
        std::lock_guard<std::mutex> lock(waitHandleMutex_);
        if (!readyEvent_) {
            const std::wstring eventName = names_.readyEventName.toStdWString();
            readyEvent_ = CreateEventW(nullptr, FALSE, FALSE, eventName.c_str());
        }
        if (!interruptEvent_) {
            interruptEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
        }
        ready = static_cast<HANDLE>(readyEvent_);
        interrupt = static_cast<HANDLE>(interruptEvent_);
        if (!ready || !interrupt) {
            lastError_ = QStringLiteral("CreateEventW failed (winerr=%1)").arg(GetLastError());
            return SharedMemoryFrameWaitResult::Failed;
        }
    }

    const HANDLE handles[] = {ready, interrupt};
    const DWORD waitMs = static_cast<DWORD>(std::clamp(timeoutMs, 0, 1000));
    const DWORD result = WaitForMultipleObjects(2, handles, FALSE, waitMs);
    if (result == WAIT_OBJECT_0) {
        return SharedMemoryFrameWaitResult::Ready;
    }
    if (result == WAIT_OBJECT_0 + 1) {
        std::lock_guard<std::mutex> lock(waitHandleMutex_);
        if (interruptEvent_) {
            ResetEvent(static_cast<HANDLE>(interruptEvent_));
        }
        return SharedMemoryFrameWaitResult::Interrupted;
    }
    if (result == WAIT_TIMEOUT) {
        return SharedMemoryFrameWaitResult::Timeout;
    }
    lastError_ = QStringLiteral("SHM frame-ready wait failed (result=%1 winerr=%2)")
                     .arg(result)
                     .arg(GetLastError());
    return SharedMemoryFrameWaitResult::Failed;
#else
    Q_UNUSED(timeoutMs);
    return SharedMemoryFrameWaitResult::Unsupported;
#endif
}

void SharedMemoryFrameReader::interruptWait() noexcept
{
#ifdef Q_OS_WIN
    std::lock_guard<std::mutex> lock(waitHandleMutex_);
    // Latch an interrupt even if the worker has not entered its first named
    // event wait yet. This closes the narrow race where a legacy `frame_shm`
    // fallback notice lands after the pump snapshots its queue but before this
    // reader creates the wait handles; without the latch that frame waits for
    // the full 250 ms safety timeout.
    if (!interruptEvent_) {
        interruptEvent_ = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    }
    if (interruptEvent_) {
        SetEvent(static_cast<HANDLE>(interruptEvent_));
    }
#endif
}

bool SharedMemoryFrameReader::probeGeneration(std::uint64_t& writeCount)
{
    writeCount = 0;
    lastError_.clear();
#ifdef Q_OS_WIN
    if (!view_ || !mutex_) {
        lastError_ = QStringLiteral("SHM reader is not open");
        return false;
    }

    const DWORD waitResult = WaitForSingleObject(
        static_cast<HANDLE>(mutex_), kMutexWaitMs);
    if (waitResult == WAIT_TIMEOUT) {
        // A diagnostic probe never competes with an in-flight producer copy.
        // The next bounded timeout can retry without escalating this ordinary
        // latest-wins collision into a transport fault.
        return false;
    }
    if (waitResult == WAIT_ABANDONED) {
        // Ownership transfers to this thread, but the producer may have died at
        // any byte of its publish. Never inspect or copy that generation.
        lastWriteCount_ = 0;
        lastTimestampNs_ = 0;
        cachedWidth_ = 0;
        cachedHeight_ = 0;
        if (!ReleaseMutex(static_cast<HANDLE>(mutex_))) {
            lastError_ = QStringLiteral(
                "ReleaseMutex after abandoned SHM publish failed (winerr=%1)")
                             .arg(GetLastError());
        } else {
            lastError_ = QStringLiteral(
                "SHM producer abandoned a partial publish; generation dropped");
        }
        return false;
    }
    if (waitResult != WAIT_OBJECT_0) {
        lastError_ = QStringLiteral("SHM mutex probe failed (result=%1)").arg(waitResult);
        return false;
    }

    bool valid = false;
    const auto* const header = static_cast<const ShmHeader*>(view_);
    if (header->writeCount == 0) {
        // Explicit never-published/incomplete transactional header.
    } else if (header->magic != kMagic || header->version != kVersion) {
        lastError_ = QStringLiteral("SHM probe header magic/version mismatch");
    } else if (header->width == 0 || header->height == 0
               || header->width > kMaxWidth || header->height > kMaxHeight
               || header->channels != kChannels) {
        lastError_ = QStringLiteral("SHM probe geometry/channel contract failed");
    } else {
        const size_t bodySize = static_cast<size_t>(header->width)
            * static_cast<size_t>(header->height) * kChannels;
        if (bodySize > kMaxBodySize) {
            lastError_ = QStringLiteral("SHM probe body exceeds mapping bounds");
        } else {
            writeCount = header->writeCount;
            valid = true;
        }
    }
    if (!ReleaseMutex(static_cast<HANDLE>(mutex_))) {
        writeCount = 0;
        lastError_ = QStringLiteral("ReleaseMutex after SHM probe failed (winerr=%1)")
                         .arg(GetLastError());
        return false;
    }
    return valid;
#else
    lastError_ = QStringLiteral("shared-memory preview is Windows-only");
    return false;
#endif
}

QImage SharedMemoryFrameReader::readFrame(int& frameNumber)
{
    frameNumber = 0;
    lastError_.clear();
#ifdef Q_OS_WIN
    if (!view_ || !mutex_) {
        lastError_ = QStringLiteral("SHM reader is not open");
        return {};
    }

    // Allocate before taking the inter-process mutex. At 1280x720 the owning
    // image is 2.64 MiB; allocator/page-fault latency has no useful upper bound
    // and must never make the producer miss its own bounded 3 ms wait. The first
    // frame (and a genuine resolution transition) discovers geometry under the
    // mutex, releases it, allocates, then revalidates before copying.
    QImage image;
    if (cachedWidth_ > 0 && cachedHeight_ > 0) {
        image = QImage(cachedWidth_, cachedHeight_, QImage::Format_BGR888);
        if (image.isNull()) {
            lastError_ = QStringLiteral("QImage allocation failed for cached SHM geometry");
            return {};
        }
    }

    // A writer can change geometry between the discovery and revalidation
    // locks. Bound retries so a corrupt/flapping producer cannot monopolize the
    // worker; the next latest-wins notification will retry with the new cache.
    constexpr int kGeometryAttempts = 3;
    for (int attempt = 0; attempt < kGeometryAttempts; ++attempt) {
        const DWORD waitResult = WaitForSingleObject(
            static_cast<HANDLE>(mutex_), kMutexWaitMs);
        if (waitResult == WAIT_TIMEOUT) {
            // Ordinary latest-wins contention: a newer producer copy is in flight.
            // The next frame_shm notification will read that committed generation.
            // Counting this as a read fault used to trigger the expensive JPEG
            // fallback after 60 harmless scheduling collisions, reintroducing the
            // choppy stdout/base64 path even though the mapping was healthy.
            return {};
        }
        if (waitResult == WAIT_ABANDONED) {
            // WAIT_ABANDONED grants ownership, but it is evidence that the
            // producer died while its transactional publish was in progress.
            // Drop without consulting the header/body, release ownership, and
            // forget prior geometry/generation so a clean writer can recover.
            lastWriteCount_ = 0;
            lastTimestampNs_ = 0;
            cachedWidth_ = 0;
            cachedHeight_ = 0;
            if (!ReleaseMutex(static_cast<HANDLE>(mutex_))) {
                lastError_ = QStringLiteral(
                    "ReleaseMutex after abandoned SHM publish failed (winerr=%1)")
                                 .arg(GetLastError());
            } else {
                lastError_ = QStringLiteral(
                    "SHM producer abandoned a partial publish; frame dropped");
            }
            return {};
        }
        if (waitResult != WAIT_OBJECT_0) {
            lastError_ = QStringLiteral("SHM mutex wait failed (result=%1)").arg(waitResult);
            return {};
        }

        bool copied = false;
        bool reallocate = false;
        int requiredWidth = 0;
        int requiredHeight = 0;
        const auto* const header = static_cast<const ShmHeader*>(view_);
        const quint64 writeCount = header->writeCount;
        if (writeCount == 0) {
            // Explicit never-published/incomplete transactional header. A
            // writer failure can leave this state after releasing normally;
            // it must never be treated as pixels or a duplicate generation.
        } else if (header->magic != kMagic || header->version != kVersion) {
            lastError_ = QStringLiteral("SHM header magic/version mismatch");
        } else if (writeCount == lastWriteCount_) {
            // Expected when stdout notifications coalesce: the first handler
            // already consumed the newest mapping generation.
        } else if (header->width == 0 || header->height == 0
                   || header->width > kMaxWidth || header->height > kMaxHeight
                   || header->channels != kChannels) {
            lastError_ = QStringLiteral("SHM frame geometry/channel contract failed");
        } else {
            const size_t bodySize = static_cast<size_t>(header->width)
                * static_cast<size_t>(header->height) * kChannels;
            if (bodySize > kMaxBodySize
                || header->width > static_cast<quint32>(std::numeric_limits<int>::max())
                || header->height > static_cast<quint32>(std::numeric_limits<int>::max())) {
                lastError_ = QStringLiteral("SHM frame body exceeds mapping bounds");
            } else {
                const int width = static_cast<int>(header->width);
                const int height = static_cast<int>(header->height);
                const int sourceStride = width * static_cast<int>(kChannels);
                if (image.size() != QSize(width, height)
                    || image.format() != QImage::Format_BGR888) {
                    // Do not allocate while owning the named mutex. Remember the
                    // validated geometry, release, allocate, then retry against
                    // the current committed header/write count.
                    cachedWidth_ = width;
                    cachedHeight_ = height;
                    requiredWidth = width;
                    requiredHeight = height;
                    reallocate = true;
                } else {
                    const auto* const source =
                        static_cast<const quint8*>(view_) + kHeaderSize;
                    if (image.bytesPerLine() == sourceStride) {
                        std::memcpy(image.bits(), source, bodySize);
                    } else {
                        for (int y = 0; y < height; ++y) {
                            std::memcpy(
                                image.scanLine(y), source + y * sourceStride,
                                static_cast<size_t>(sourceStride));
                        }
                    }
                    frameNumber = static_cast<int>(header->frameNumber);
                    lastWriteCount_ = writeCount;
                    lastTimestampNs_ = header->timestampNs;
                    ++framesRead_;
                    copied = true;
                }
            }
        }
        if (!ReleaseMutex(static_cast<HANDLE>(mutex_))) {
            lastError_ = QStringLiteral("ReleaseMutex failed (winerr=%1)").arg(GetLastError());
            return {};
        }
        if (copied) {
            return image;
        }
        if (!reallocate) {
            return {};
        }

        image = QImage(requiredWidth, requiredHeight, QImage::Format_BGR888);
        if (image.isNull()) {
            lastError_ = QStringLiteral("QImage allocation failed for SHM frame");
            return {};
        }
    }
    // Geometry changed on every bounded revalidation. This is a benign
    // latest-wins drop; the cached final geometry makes the next notification
    // steady-state and it must not advance the hard-fault fallback counter.
    lastError_.clear();
    return {};
#else
    lastError_ = QStringLiteral("shared-memory preview is Windows-only");
    return {};
#endif
}

void SharedMemoryFrameReader::closeMappingHandles()
{
#ifdef Q_OS_WIN
    if (view_) {
        UnmapViewOfFile(view_);
        view_ = nullptr;
    }
    if (mutex_) {
        CloseHandle(static_cast<HANDLE>(mutex_));
        mutex_ = nullptr;
    }
    if (mapping_) {
        CloseHandle(static_cast<HANDLE>(mapping_));
        mapping_ = nullptr;
    }
#else
    view_ = nullptr;
    mutex_ = nullptr;
    mapping_ = nullptr;
#endif
    lastWriteCount_ = 0;
    lastTimestampNs_ = 0;
    framesRead_ = 0;
    cachedWidth_ = 0;
    cachedHeight_ = 0;
    lastError_.clear();
}

void SharedMemoryFrameReader::closeNotificationHandles()
{
#ifdef Q_OS_WIN
    std::lock_guard<std::mutex> lock(waitHandleMutex_);
    if (interruptEvent_) {
        CloseHandle(static_cast<HANDLE>(interruptEvent_));
        interruptEvent_ = nullptr;
    }
    if (readyEvent_) {
        CloseHandle(static_cast<HANDLE>(readyEvent_));
        readyEvent_ = nullptr;
    }
#else
    interruptEvent_ = nullptr;
    readyEvent_ = nullptr;
#endif
}

void SharedMemoryFrameReader::close()
{
    closeMappingHandles();
    closeNotificationHandles();
}

} // namespace orion
