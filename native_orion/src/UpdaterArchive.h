#pragma once

#include "OrionExports.h"

#include <QtCore/QString>
#include <QtCore/QStringList>

namespace orion::updater {

// Resolve a zip entry name against an install root, rejecting anything that would
// escape the root (absolute paths, drive letters, UNC paths, ".." traversal).
// On success returns the canonical absolute destination path and sets *ok=true.
// On rejection returns an empty string and sets *ok=false. This is the single
// choke point all extraction goes through and is exercised directly by tests.
ORION_UPDATER_API QString safeJoinWithinRoot(const QString& rootDir, const QString& entryName, bool* ok);

struct ExtractResult {
    bool ok = false;
    QString error;          // human-readable failure reason (safe to log)
    QString rejectedEntry;  // the offending entry name when ok == false
    QStringList files;      // relative paths written, on success
};

// Extract a zip into destDir, validating every entry through safeJoinWithinRoot
// and refusing symlinks. Fails closed: if any entry is unsafe nothing is left
// behind (the destination is wiped). maxTotalBytes / maxEntries bound zip-bomb
// exposure; pass 0 to disable a bound.
ORION_UPDATER_API ExtractResult extractZipSafely(const QString& zipPath,
                                                  const QString& destDir,
                                                  qint64 maxTotalBytes = 1024LL * 1024LL * 1024LL,
                                                  int maxEntries = 50000);

// Recursively copy srcDir into backupDir (backupDir is created fresh). Used to
// snapshot the current install before files are replaced.
ORION_UPDATER_API bool backupTree(const QString& srcDir, const QString& backupDir, QString* error = nullptr);

// Copy every file from srcDir into destDir, overwriting existing files and
// creating directories as needed. Existing files in destDir not present in
// srcDir are left untouched. Returns the list of destination-relative paths
// written via *applied when non-null.
ORION_UPDATER_API bool applyTree(const QString& srcDir, const QString& destDir,
                                 QStringList* applied = nullptr, QString* error = nullptr);

// Restore destDir from a backup made by backupTree: removes files that the
// backup does not contain and copies the backup contents back over destDir.
ORION_UPDATER_API bool restoreTree(const QString& backupDir, const QString& destDir, QString* error = nullptr);

} // namespace orion::updater
