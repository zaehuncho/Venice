#pragma once

#include "OrionExports.h"

#include <QtCore/QProcessEnvironment>
#include <QtCore/QString>
#include <QtCore/QStringList>

namespace orion {

// One atomic DirectShow enumeration snapshot. Both lists contain exactly one
// entry per IEnumMoniker row and therefore share the cv2 CAP_DSHOW device index.
// A stableIds entry may be empty when Windows cannot expose that moniker's
// display name; callers must treat an empty selected ID as non-restorable.
struct VideoInputDeviceInventory {
    QStringList friendlyNames;
    QStringList stableIds;

    [[nodiscard]] bool isAligned() const noexcept
    {
        return friendlyNames.size() == stableIds.size();
    }

    [[nodiscard]] bool isEmpty() const noexcept
    {
        return friendlyNames.isEmpty();
    }

    [[nodiscard]] bool hasAnyStableId() const noexcept
    {
        for (const QString& id : stableIds) {
            if (!id.isEmpty()) {
                return true;
            }
        }
        return false;
    }

    // A timeout may reuse labels to keep webcam probing safe, but cached
    // identity is never timing authority: the hardware could have been swapped
    // at the same DirectShow index while enumeration was wedged.
    [[nodiscard]] VideoInputDeviceInventory namesOnlyFallback() const
    {
        VideoInputDeviceInventory fallback;
        fallback.friendlyNames = friendlyNames;
        fallback.stableIds.reserve(friendlyNames.size());
        for (qsizetype i = 0; i < friendlyNames.size(); ++i) {
            fallback.stableIds.append(QString());
        }
        return fallback;
    }
};

// Return an opaque, versioned identity derived from the normalized DirectShow
// moniker display name. The raw PNP/USB path can contain a device serial, so it
// is never placed in the child environment or logs.
[[nodiscard]] ORION_REMOTEPLAY_API QString stableVideoInputDeviceId(
    const QString& monikerDisplayName);

// Append exactly one index-aligned row, even when FriendlyName or moniker
// lookup failed. This is public so the no-hardware contract can be unit-tested.
ORION_REMOTEPLAY_API void appendVideoInputDevice(
    VideoInputDeviceInventory& inventory,
    const QString& friendlyName,
    const QString& monikerDisplayName);

// Replace both child-process variables as one validated generation. On an
// empty/misaligned inventory, remove both variables so inherited process state
// can never impersonate a native enumeration result.
[[nodiscard]] ORION_REMOTEPLAY_API bool insertVideoInputDeviceEnvironment(
    QProcessEnvironment& environment,
    const VideoInputDeviceInventory& inventory);

// Property-only DirectShow enumeration: it never opens the video device. The
// caller owns bounding/off-thread execution because a third-party filter can
// still wedge COM enumeration while another process releases the card.
[[nodiscard]] ORION_REMOTEPLAY_API VideoInputDeviceInventory
enumerateVideoInputDevices();

} // namespace orion
