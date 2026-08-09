#include "ControllerDeviceSelector.h"

#include <algorithm>

namespace orion {

void ControllerDeviceSelector::reset()
{
    pinnedKey_.clear();
    pendingKey_.clear();
    pendingSinceMs_ = 0;
}

int ControllerDeviceSelector::effectivePriority(const ControllerDeviceCandidate& c)
{
    if (c.virtualDevice) {
        return -1000;
    }
    if (!c.live) {
        return -100;
    }
    if (c.physicalSony && c.source == QLatin1String("RawInput")) {
        return 300;
    }
    if (c.source == QLatin1String("XInput")) {
        return 170;
    }
    if (c.source == QLatin1String("WinMM")) {
        return 120;
    }
    return c.priority;
}

ControllerDeviceSelection ControllerDeviceSelector::select(
    const QVector<ControllerDeviceCandidate>& candidates,
    qint64 nowMs,
    int switchDelayMs)
{
    ControllerDeviceSelection out;
    out.virtualVisible = std::any_of(candidates.cbegin(), candidates.cend(), [](const auto& c) {
        return c.virtualDevice;
    });
    out.physicalSonyVisible = std::any_of(candidates.cbegin(), candidates.cend(), [](const auto& c) {
        return c.physicalSony && !c.virtualDevice;
    });
    out.physicalSonyLive = std::any_of(candidates.cbegin(), candidates.cend(), [](const auto& c) {
        return c.physicalSony && !c.virtualDevice && c.live;
    });

    QVector<ControllerDeviceCandidate> usable;
    usable.reserve(candidates.size());
    for (const auto& c : candidates) {
        if (!c.live || c.virtualDevice) {
            continue;
        }
        auto copy = c;
        copy.priority = effectivePriority(c);
        usable.push_back(copy);
    }

    if (usable.isEmpty()) {
        reset();
        if (out.physicalSonyVisible) {
            out.health = QStringLiteral("Present but no live reports");
        } else if (out.virtualVisible) {
            out.health = QStringLiteral("Only virtual controller visible");
        } else {
            out.health = QStringLiteral("Physical Sony device not visible to Windows");
        }
        return out;
    }

    std::sort(usable.begin(), usable.end(), [](const auto& a, const auto& b) {
        if (a.priority != b.priority) {
            return a.priority > b.priority;
        }
        if (a.liveReportAgeMs != b.liveReportAgeMs) {
            if (a.liveReportAgeMs < 0) return false;
            if (b.liveReportAgeMs < 0) return true;
            return a.liveReportAgeMs < b.liveReportAgeMs;
        }
        return a.key < b.key;
    });

    const auto best = usable.first();
    auto findPinned = std::find_if(usable.cbegin(), usable.cend(), [this](const auto& c) {
        return c.key == pinnedKey_;
    });
    if (pinnedKey_.isEmpty() || findPinned == usable.cend()) {
        pinnedKey_ = best.key;
        pendingKey_.clear();
        pendingSinceMs_ = 0;
        out.device = best;
    } else if (best.key == pinnedKey_) {
        pendingKey_.clear();
        pendingSinceMs_ = 0;
        out.device = *findPinned;
    } else {
        if (pendingKey_ != best.key) {
            pendingKey_ = best.key;
            pendingSinceMs_ = nowMs;
        }
        const bool pendingLongEnough = nowMs - pendingSinceMs_ >= switchDelayMs;
        if (pendingLongEnough) {
            pinnedKey_ = best.key;
            pendingKey_.clear();
            pendingSinceMs_ = 0;
            out.device = best;
        } else {
            out.device = *findPinned;
        }
    }

    out.active = true;
    if (out.device.source == QLatin1String("RawInput")) {
        out.health = QStringLiteral("Using RawInput");
    } else if (out.device.source == QLatin1String("XInput")) {
        out.health = QStringLiteral("Using XInput fallback");
    } else if (out.device.source == QLatin1String("WinMM")) {
        out.health = QStringLiteral("Using WinMM fallback");
    } else {
        out.health = QStringLiteral("Using controller input");
    }
    return out;
}

} // namespace orion
