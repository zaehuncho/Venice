#pragma once

#include <QtCore/QHash>
#include <QtCore/QString>
#include <QtCore/QtGlobal>

#include <utility>

namespace orion {

struct RawInputDeviceIdentity final {
    QString path;
    QString kind;
};

// Raw Input device names are stable for the lifetime of an HRAWINPUT device
// handle. Resolving RIDI_DEVICENAME for every ~250 Hz report is both wasteful
// and dangerous when a GUI consumer shares the snapshot mutex: the Win32 query
// can block during device churn. Cache successful identities for the handle
// lifetime and rate-limit transient lookup failures so recovery remains possible.
class RawInputDeviceIdentityCache final {
public:
    static constexpr qint64 kFailedLookupRetryMs = 1000;

    template <typename PathResolver, typename PathClassifier>
    [[nodiscard]] RawInputDeviceIdentity resolve(
        quintptr handle,
        qint64 nowMs,
        PathResolver&& pathResolver,
        PathClassifier&& pathClassifier)
    {
        auto it = entries_.find(handle);
        if (it != entries_.end()) {
            if (it->resolved || nowMs < it->retryAfterMs) {
                return it->identity;
            }
        }

        Entry next;
        next.identity.path = std::forward<PathResolver>(pathResolver)();
        if (!next.identity.path.isEmpty()) {
            next.identity.kind = std::forward<PathClassifier>(pathClassifier)(
                next.identity.path);
            next.resolved = true;
        } else {
            next.retryAfterMs = nowMs + kFailedLookupRetryMs;
        }
        entries_.insert(handle, next);
        return next.identity;
    }

    [[nodiscard]] QString cachedPath(quintptr handle) const
    {
        const auto it = entries_.constFind(handle);
        return it == entries_.constEnd() ? QString{} : it->identity.path;
    }

    void clear() { entries_.clear(); }
    [[nodiscard]] qsizetype size() const noexcept { return entries_.size(); }

private:
    struct Entry final {
        RawInputDeviceIdentity identity;
        qint64 retryAfterMs = 0;
        bool resolved = false;
    };

    QHash<quintptr, Entry> entries_;
};

} // namespace orion
