#pragma once

#include "OrionExports.h"
#include "OrionTypes.h"

#include <QtCore/QString>
#include <QtCore/QVector>

namespace orion {

struct ControllerDeviceCandidate {
    QString key;
    QString source;
    QString label;
    QString detail;
    ControllerState state;
    bool live = false;
    bool physicalSony = false;
    bool virtualDevice = false;
    qint64 liveReportAgeMs = -1;
    int priority = 0;
};

struct ControllerDeviceSelection {
    bool active = false;
    bool physicalSonyVisible = false;
    bool physicalSonyLive = false;
    bool virtualVisible = false;
    ControllerDeviceCandidate device;
    QString health = QStringLiteral("Physical Sony device not visible to Windows");
};

class ORION_AUTOMATION_API ControllerDeviceSelector final {
public:
    void reset();
    [[nodiscard]] QString pinnedKey() const noexcept { return pinnedKey_; }

    ControllerDeviceSelection select(const QVector<ControllerDeviceCandidate>& candidates,
                                     qint64 nowMs,
                                     int switchDelayMs = 900);

private:
    [[nodiscard]] static int effectivePriority(const ControllerDeviceCandidate& c);

    QString pinnedKey_;
    QString pendingKey_;
    qint64 pendingSinceMs_ = 0;
};

} // namespace orion
