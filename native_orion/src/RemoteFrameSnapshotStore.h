#pragma once

#include <QtCore/QString>
#include <QtCore/QRect>
#include <QtCore/QSize>
#include <QtGui/QImage>

#include <cstddef>
#include <deque>
#include <optional>
#include <utility>

namespace orion {

// Bounded immutable snapshots behind image://remote/live/<serial>.
//
// QQuick may service an asynchronous image-provider request after one or more
// newer frames have reached the GUI thread. Returning a single "latest" image
// in that case silently changes the identity of the requested URL and breaks
// the detector-bbox/frame join. Keep a short history instead: every URL either
// resolves to its exact QImage or to the lifecycle fallback, never to a newer
// frame wearing an older serial.
class RemoteFrameSnapshotStore final {
public:
    // At 60 Hz this retains about 200 ms of immutable QImage references. The
    // pixel buffers are copy-on-write and requests take their own shared
    // reference before eviction, so this is both bounded and safe for async
    // provider workers.
    static constexpr std::size_t kCapacity = 12;

    void reset(int serial, QImage fallback)
    {
        snapshots_.clear();
        fallback_ = std::move(fallback);
        if (!fallback_.isNull() && serial >= 0) {
            snapshots_.push_back(Snapshot{serial, fallback_});
        }
    }

    void publish(int serial, QImage image)
    {
        if (serial < 0 || image.isNull()) {
            return;
        }
        for (auto& snapshot : snapshots_) {
            if (snapshot.serial == serial) {
                snapshot.image = std::move(image);
                return;
            }
        }
        if (snapshots_.size() == kCapacity) {
            snapshots_.pop_front();
        }
        snapshots_.push_back(Snapshot{serial, std::move(image)});
    }

    [[nodiscard]] QImage lookupProviderId(
        const QString& providerId, bool* exact = nullptr) const
    {
        const qsizetype slash = providerId.lastIndexOf(QLatin1Char('/'));
        const QString serialText = slash >= 0
            ? providerId.mid(slash + 1) : providerId;
        bool parsed = false;
        const int serial = serialText.toInt(&parsed);
        if (parsed && serial >= 0) {
            for (auto it = snapshots_.crbegin(); it != snapshots_.crend(); ++it) {
                if (it->serial == serial) {
                    if (exact) {
                        *exact = true;
                    }
                    return it->image;
                }
            }
        }
        if (exact) {
            *exact = false;
        }
        return fallback_;
    }

    [[nodiscard]] std::size_t size() const noexcept { return snapshots_.size(); }

private:
    struct Snapshot final {
        int serial = -1;
        QImage image;
    };

    std::deque<Snapshot> snapshots_;
    QImage fallback_;
};

static_assert(RemoteFrameSnapshotStore::kCapacity >= 3);

enum class RemoteFrameAckDisposition {
    Accept,
    Duplicate,
    StaleOrFuture,
};

// Async presentation is intentionally allowed to trail native publication by
// a few serials. Rejecting every serial except the newest cancels otherwise
// valid textures whenever the 60 Hz producer advances during a QML upload.
// Monotonic visible serials are safe: each one still resolves to its immutable
// image and overlay snapshot, while an older completion can never roll back a
// newer texture.
[[nodiscard]] constexpr RemoteFrameAckDisposition classifyRemoteFrameAck(
    int presentedSerial, int newestPublishedSerial, int lastPresentedSerial) noexcept
{
    if (presentedSerial < 0 || presentedSerial > newestPublishedSerial
        || presentedSerial < lastPresentedSerial) {
        return RemoteFrameAckDisposition::StaleOrFuture;
    }
    if (presentedSerial == lastPresentedSerial) {
        return RemoteFrameAckDisposition::Duplicate;
    }
    return RemoteFrameAckDisposition::Accept;
}

// Frame-coupled QML state is acknowledged separately from the asynchronous
// image fetch. Until Image.Ready confirms a particular serial, QML intentionally
// keeps both the prior texture and the prior overlay geometry visible.
struct RemoteFrameOverlaySnapshot final {
    int serial = -1;
    QRect meterBox;
    QRect rejectedBox;
    bool meterConfirmed = false;
    QSize frameSize;
    // Immutable source identity for a preview that may be published before its
    // same-frame detector result reaches the GUI thread. The public QML state
    // is still changed only when this serial is acknowledged as Image.Ready.
    QSize captureSize;
    int sourceFrameNumber = -1;
    QRect joinedCaptureBox;
    int joinedDetectionFrameNumber = -1;
    quint64 shotToken = 0;
};

class RemoteFrameOverlaySnapshotStore final {
public:
    // Overlay metadata is tiny, so retain enough identities to cover a measured
    // 250 ms QML upload stall at 60 Hz plus scheduling headroom. The QImage
    // store remains at its lower memory-bounded capacity; provider requests take
    // a shared image reference before the later Image.Ready acknowledgement.
    static constexpr std::size_t kCapacity = 24;

    void clear() noexcept { snapshots_.clear(); }

    void reset(RemoteFrameOverlaySnapshot snapshot)
    {
        snapshots_.clear();
        publish(std::move(snapshot));
    }

    void publish(RemoteFrameOverlaySnapshot snapshot)
    {
        if (snapshot.serial < 0 || !snapshot.frameSize.isValid()) {
            return;
        }
        for (auto& current : snapshots_) {
            if (current.serial == snapshot.serial) {
                current = std::move(snapshot);
                return;
            }
        }
        if (snapshots_.size() == kCapacity) {
            snapshots_.pop_front();
        }
        snapshots_.push_back(std::move(snapshot));
    }

    [[nodiscard]] std::optional<RemoteFrameOverlaySnapshot> lookup(int serial) const
    {
        for (auto it = snapshots_.crbegin(); it != snapshots_.crend(); ++it) {
            if (it->serial == serial) {
                return *it;
            }
        }
        return std::nullopt;
    }

    // A provisional prior-frame bridge can already be visible when its exact
    // detector observation arrives. Refine ONLY the currently acknowledged
    // texture, never another pending/older serial or a newer-on-older join.
    // An existing visible lock is mandatory: this cannot resurrect a cleared
    // outline or turn late metadata into new detection/timing authority.
    [[nodiscard]] bool backfillPresentedExact(
        int lastAcknowledgedSerial, const QSize& captureSize,
        int sourceFrameNumber, const QRect& captureBox, quint64 shotToken)
    {
        if (lastAcknowledgedSerial < 0 || sourceFrameNumber < 0
            || !captureSize.isValid() || !captureBox.isValid()) {
            return false;
        }
        for (auto& snapshot : snapshots_) {
            if (snapshot.serial != lastAcknowledgedSerial) {
                continue;
            }
            if (!snapshot.meterConfirmed
                || (!snapshot.meterBox.isValid() && !snapshot.joinedCaptureBox.isValid())
                || snapshot.captureSize != captureSize
                || snapshot.sourceFrameNumber != sourceFrameNumber
                || snapshot.shotToken != shotToken
                || snapshot.joinedDetectionFrameNumber >= sourceFrameNumber) {
                return false;
            }
            snapshot.meterBox = {};
            snapshot.rejectedBox = {};
            snapshot.joinedCaptureBox = captureBox;
            snapshot.joinedDetectionFrameNumber = sourceFrameNumber;
            return true;
        }
        return false;
    }

    // A preview can beat its detector result through the independent decode and
    // telemetry paths. Patch only snapshots which QML has not acknowledged,
    // and let the caller resolve a box using the same exact-or-prior frame join
    // policy used during ordinary publication. The guard below independently
    // enforces direction and age: a newer detection can never be painted onto an
    // older image, and closer prior evidence supersedes an earlier provisional
    // join only while that image remains unpresented.
    template <typename JoinResolver>
    [[nodiscard]] std::size_t backfillUnacknowledged(
        int lastAcknowledgedSerial,
        const QSize& captureSize,
        int maximumPriorFrameDistance,
        JoinResolver&& resolveJoin)
    {
        if (!captureSize.isValid() || maximumPriorFrameDistance < 0) {
            return 0;
        }

        std::size_t patched = 0;
        for (auto& snapshot : snapshots_) {
            if (snapshot.serial <= lastAcknowledgedSerial
                || snapshot.sourceFrameNumber < 0
                || snapshot.captureSize != captureSize) {
                continue;
            }

            QRect captureBox;
            int detectionFrameNumber = -1;
            if (!resolveJoin(
                    snapshot.sourceFrameNumber, captureBox,
                    detectionFrameNumber)
                || !captureBox.isValid()
                || detectionFrameNumber < 0
                || detectionFrameNumber > snapshot.sourceFrameNumber
                || snapshot.sourceFrameNumber - detectionFrameNumber
                    > maximumPriorFrameDistance
                || detectionFrameNumber <= snapshot.joinedDetectionFrameNumber) {
                continue;
            }

            // The new join is closer to this source frame than the geometry
            // computed at publish time (which may only be a held prior box).
            // Resolve it once, in presentation order, when Image.Ready arrives.
            snapshot.meterBox = {};
            snapshot.rejectedBox = {};
            snapshot.joinedCaptureBox = captureBox;
            snapshot.joinedDetectionFrameNumber = detectionFrameNumber;
            snapshot.meterConfirmed = true;
            ++patched;
        }
        return patched;
    }

    // Detector loss is authoritative only for that detector frame and newer
    // unpresented images. Older immutable image/overlay joins stay intact so an
    // already-uploading texture cannot lose its matching box mid-flight.
    [[nodiscard]] std::size_t clearUnacknowledgedFromSourceFrame(
        int lastAcknowledgedSerial, int firstClearFrameNumber) noexcept
    {
        std::size_t cleared = 0;
        for (auto& snapshot : snapshots_) {
            if (snapshot.serial <= lastAcknowledgedSerial
                || (firstClearFrameNumber >= 0
                    && snapshot.sourceFrameNumber >= 0
                    && snapshot.sourceFrameNumber < firstClearFrameNumber)) {
                continue;
            }
            const bool hadOverlay = snapshot.meterBox.isValid()
                || snapshot.rejectedBox.isValid()
                || snapshot.meterConfirmed
                || snapshot.joinedCaptureBox.isValid()
                || snapshot.joinedDetectionFrameNumber >= 0;
            snapshot.meterBox = {};
            snapshot.rejectedBox = {};
            snapshot.meterConfirmed = false;
            snapshot.joinedCaptureBox = {};
            snapshot.joinedDetectionFrameNumber = -1;
            if (hadOverlay) {
                ++cleared;
            }
        }
        return cleared;
    }

    [[nodiscard]] std::size_t size() const noexcept { return snapshots_.size(); }

private:
    std::deque<RemoteFrameOverlaySnapshot> snapshots_;
};

static_assert(RemoteFrameOverlaySnapshotStore::kCapacity >= 18);

} // namespace orion
