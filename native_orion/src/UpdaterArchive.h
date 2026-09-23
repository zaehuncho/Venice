#pragma once

#include "OrionExports.h"

#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QJsonObject>

namespace orion::updater {

// Resolve a zip entry name against an install root, rejecting anything that would
// escape the root (absolute paths, drive letters, UNC paths, ".." traversal).
// On success returns the canonical absolute destination path and sets *ok=true.
// On rejection returns an empty string and sets *ok=false. This is the single
// choke point all extraction goes through and is exercised directly by tests.
ORION_UPDATER_API QString safeJoinWithinRoot(const QString& rootDir, const QString& entryName, bool* ok);

// [2026-09-21] True when `path` carries ANY reparse tag (symlink, junction / mount
// point, cloud placeholder, app-exec link). Read with GetFileAttributesW, which does
// not follow the link, so a dangling link counts. False for a missing path.
ORION_UPDATER_API bool isReparsePointPath(const QString& path);

// [2026-09-21 Codex r5] The Win32 extended-length form of `path`: "\\\\?\\C:\\..." for a drive
// path and "\\\\?\\UNC\\server\\share\\..." for a UNC path (a bare "\\\\?\\server\\share" is
// NOT a valid form). Idempotent on an already-prefixed path. Every GetFileAttributesW /
// RemoveDirectoryW / DeleteFileW call in the updater goes through this so paths beyond
// MAX_PATH and UNC install trees are addressed correctly. Windows-only semantics; on other
// platforms the path is returned unchanged.
ORION_UPDATER_API QString toExtendedLengthPath(const QString& path);

// [2026-09-21] True when rootDir itself is a reparse point, when `path` is outside
// rootDir, or when any EXISTING component below rootDir on the way to `path` (`path`
// itself included) is a reparse point. safeJoinWithinRoot judges the STRING; this
// judges the FILESYSTEM: a junction planted at <install>/plugins -> C:/Windows would
// pass the string check and every write "inside the root" would land outside it.
// Every write the updater makes checks this immediately before the write. Known
// residual (Codex F2, accepted for beta): the check and the write are two syscalls, so
// a reparse point swapped in between them is not caught; closing that needs
// handle-relative no-follow I/O (NtCreateFile with RootDirectory per component). The
// attacker who can race the install tree already has write access to it.
ORION_UPDATER_API bool pathCrossesReparsePoint(const QString& rootDir, const QString& path);

// [2026-09-21 Codex r4 F2] Delete a tree without following anything inside it: every
// reparse entry (junction, symlink) is unlinked as a link, real directories recurse. Use
// this instead of QDir::removeRecursively, whose isSymLink() test does not cover NTFS
// junctions and would descend through one into the outside target.
ORION_UPDATER_API bool removeTreeSafely(const QString& dir, QString* error = nullptr);

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

// The launcher/updater verifies the old signed release manifest before calling
// these helpers. `files` is its authenticated files map. Stage the updater EXE,
// every root DLL, and the Qt plugin families used by the updater into a sibling
// tree so neither the applying process nor its DLLs load from the destination.
ORION_UPDATER_API bool stageUpdaterRuntime(const QString& installDir, const QString& stageDir,
                                           const QJsonObject& files, QString* error = nullptr);
ORION_UPDATER_API bool verifyStagedUpdaterRuntime(const QString& installDir, const QString& stageDir,
                                                  const QJsonObject& files, QString* error = nullptr);

// Call only with the independently verified old and new signed manifest maps.
// Remove exactly old-minus-new entries, never unrelated customer data.
ORION_UPDATER_API bool pruneRetiredRuntimeFiles(const QString& installDir,
                                                const QJsonObject& oldFiles,
                                                const QJsonObject& newFiles,
                                                QString* error = nullptr);

} // namespace orion::updater
