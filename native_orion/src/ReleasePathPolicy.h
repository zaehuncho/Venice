#pragma once
#include "AppConfig.h"

namespace orion {
// The active source is authoritative for legacy installs. While Tempo is off,
// retain the last two-choice preference without claiming stick ownership.
inline QString selectedTempoInputPath(const AppConfigData& data)
{
    const bool enabled = data.tempoEnabled || data.tempoRemapEnabled;
    const bool stick = enabled
        ? normalizedRemotePlayInputSource(data.remotePlayInputSource) == QLatin1String("stick")
        : data.tempoRemapType.trimmed().compare(QLatin1String("stick"), Qt::CaseInsensitive) == 0;
    return stick ? QStringLiteral("Stick") : QStringLiteral("Button");
}

inline void setTempoPathEnabled(AppConfigData& data, bool enabled)
{
    const QString path = selectedTempoInputPath(data);
    data.tempoRemapType = path.toLower();
    data.remotePlayInputSource = enabled && path == QLatin1String("Stick")
        ? QStringLiteral("stick") : QStringLiteral("square");
    data.tempoEnabled = enabled;
    data.tempoRemapEnabled = enabled;
    data.tempoFlickEnabled = enabled;
    // Both inputs use the existing meter timing, without hidden release offsets.
    data.rhythmFlickDelayMs = 0.0;
    data.tempoReleaseStyle = QStringLiteral("flick");
}

inline bool selectTempoInputPath(AppConfigData& data, const QString& value)
{
    const QString path = value.trimmed().toLower();
    if (path != QLatin1String("button") && path != QLatin1String("stick")) return false;
    const bool enabled = data.tempoEnabled || data.tempoRemapEnabled;
    data.tempoRemapType = path;
    if (enabled) data.remotePlayInputSource = path == QLatin1String("stick")
        ? QStringLiteral("stick") : QStringLiteral("square");
    setTempoPathEnabled(data, enabled);
    return true;
}

inline bool sameTempoPathSettings(const AppConfigData& a, const AppConfigData& b)
{
    return a.remotePlayInputSource == b.remotePlayInputSource
        && a.tempoRemapType == b.tempoRemapType
        && a.tempoEnabled == b.tempoEnabled
        && a.tempoRemapEnabled == b.tempoRemapEnabled
        && a.tempoFlickEnabled == b.tempoFlickEnabled
        && a.rhythmFlickDelayMs == b.rhythmFlickDelayMs
        && a.tempoReleaseStyle == b.tempoReleaseStyle;
}
} // namespace orion
