#pragma once

#include "OrderedFileLogSink.h"
#include "UiNotificationPolicy.h"

#include <QtGui/QClipboard>

#include <chrono>

namespace orion {

// The side-effecting portion of the controller's Copy Log action lives here so
// the exact disk-fault/clipboard path can be exercised without constructing a
// full controller (whose constructor bootstraps settings and runtime services).
struct ActivityLogClipboardCopyResult final {
    bool drained = false;
    bool diskIncomplete = false;
    bool storageFault = false;
    quint64 droppedBatches = 0;
    QString diskTail;
};

[[nodiscard]] inline ActivityLogClipboardCopyResult copyActivityLogToClipboard(
    QClipboard* clipboard, OrderedFileLogSink& sink,
    const QString& logPath, const QStringList& sessionRing)
{
    ActivityLogClipboardCopyResult result;
    result.drained = sink.drain(std::chrono::milliseconds(500));
    const auto stats = sink.stats();
    result.storageFault = stats.storageFault;
    result.droppedBatches = stats.droppedBatches;
    result.diskIncomplete = !result.drained || result.storageFault
        || result.droppedBatches != 0;
    if (!result.diskIncomplete)
        result.diskTail = ui_notifications::readLogTailForSharing(logPath);
    if (clipboard)
        clipboard->setText(ui_notifications::activityLogCopyForSharing(
            result.diskTail, sessionRing, result.drained,
            result.storageFault, result.droppedBatches));
    return result;
}

} // namespace orion
