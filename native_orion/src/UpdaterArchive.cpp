#include "UpdaterArchive.h"

#include <QtCore/QDir>
#include <QtCore/QDirIterator>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QCryptographicHash>
#include <QtCore/QSet>

#include <QtCore/private/qzipreader_p.h>

#if defined(Q_OS_WIN)
#include <Windows.h>
#endif

namespace orion::updater {

namespace {

#if defined(Q_OS_WIN)
constexpr Qt::CaseSensitivity kPathCase = Qt::CaseInsensitive;
#else
constexpr Qt::CaseSensitivity kPathCase = Qt::CaseSensitive;
#endif

// The updater's own log lives inside the install dir and is appended to while an
// apply/rollback is in flight; an exact restore must never delete it.
const QLatin1String kUpdaterLogName("orion_updater.log");

QString normalizeSeparators(const QString& value)
{
    QString normalized = value;
    normalized.replace(QLatin1Char('\\'), QLatin1Char('/'));
    return normalized;
}

bool ensureDir(const QString& path, QString* error)
{
    QDir dir;
    if (dir.mkpath(path)) {
        return true;
    }
    if (error) {
        *error = QStringLiteral("Could not create directory: %1").arg(QDir::toNativeSeparators(path));
    }
    return false;
}

#if defined(Q_OS_WIN)
#endif

// Remove a reparse point ITSELF (junction, symlink) without ever following it. A backup
// never contains reparse points (backupTree refuses them), so one found in the install
// tree during a restore is foreign -- planted after the backup -- and the exact-restore
// contract is to take it out, not to follow it and not to fail closed and leave a
// partial update behind ([Codex r3 F4]).
bool removeReparsePointItself(const QString& path, QString* error)
{
#if defined(Q_OS_WIN)
    const QString native = toExtendedLengthPath(path);
    const DWORD attrs = GetFileAttributesW(reinterpret_cast<LPCWSTR>(native.utf16()));
    BOOL ok = FALSE;
    if (attrs != INVALID_FILE_ATTRIBUTES && (attrs & FILE_ATTRIBUTE_DIRECTORY)) {
        ok = RemoveDirectoryW(reinterpret_cast<LPCWSTR>(native.utf16()));   // junction: removes the link only
    } else {
        ok = DeleteFileW(reinterpret_cast<LPCWSTR>(native.utf16()));        // file symlink: removes the link only
    }
    if (!ok && error) {
        *error = QStringLiteral("Could not remove a planted reparse point: %1").arg(QDir::toNativeSeparators(path));
    }
    return ok;
#else
    if (QFile::remove(path)) {
        return true;
    }
    if (error) {
        *error = QStringLiteral("Could not remove a planted reparse point: %1").arg(path);
    }
    return false;
#endif
}

// [Codex r4 F2] Remove a tree WITHOUT following anything: a reparse entry anywhere
// inside is unlinked as a link (never traversed), real directories recurse, files are
// deleted. QDir::removeRecursively cannot be trusted here: Qt's isSymLink() does not
// cover NTFS junctions, so it would descend through one and delete the outside target.
bool removeTreeSafelyImpl(const QString& dir, QString* error)
{
    const QString clean = QDir::cleanPath(QDir(dir).absolutePath());
    if (!QFileInfo::exists(clean) && !isReparsePointPath(clean)) {
        return true;
    }
    if (isReparsePointPath(clean)) {
        return removeReparsePointItself(clean, error);
    }
    const auto entries = QDir(clean).entryInfoList(QDir::Files | QDir::Dirs | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System);
    for (const QFileInfo& entry : entries) {
        const QString path = entry.absoluteFilePath();
        if (isReparsePointPath(path)) {
            if (!removeReparsePointItself(path, error)) {
                return false;
            }
            continue;
        }
        if (entry.isDir()) {
            if (!removeTreeSafelyImpl(path, error)) {
                return false;
            }
            continue;
        }
        if (!QFile::remove(path)) {
            if (error) {
                *error = QStringLiteral("Could not remove: %1").arg(QDir::toNativeSeparators(path));
            }
            return false;
        }
    }
    if (!QDir().rmdir(clean)) {
        if (error) {
            *error = QStringLiteral("Could not remove directory: %1").arg(QDir::toNativeSeparators(clean));
        }
        return false;
    }
    return true;
}

bool refuseReparse(const QString& destRoot, const QString& path, QString* error)
{
    if (!pathCrossesReparsePoint(destRoot, path)) {
        return false;
    }
    if (error) {
        *error = QStringLiteral("Destination crosses a reparse point (symlink/junction/mount point); refusing to write: %1")
                     .arg(QDir::toNativeSeparators(path));
    }
    return true;
}

// Recursively copy the contents of srcDir into destDir, overwriting files.
// destRoot is the tree every write must stay inside of (the install dir for apply/
// restore, the backup dir for backup); it never changes across the recursion.
//
// [2026-09-21 Codex F2/F3] The reparse check runs BEFORE the directory is created
// (mkpath would otherwise create children THROUGH a junction) and again right after
// (narrows the check-to-write window; a full fix needs handle-relative no-follow
// I/O, see UpdaterArchive.h).
// replacePlantedReparse: restore mode. A reparse point sitting where a backed-up entry
// belongs is foreign by construction (backups hold none), so it is unlinked -- never
// followed -- and the backed-up bytes take its place ([Codex r4 F4]). Apply mode keeps
// refusing: a link planted at apply time is tamper, and the rollback handles it.
bool copyDirContents(const QString& srcDir, const QString& destDir, const QString& destRoot,
                     QStringList* relWritten, const QString& relPrefix, QString* error,
                     bool skipTopLevelUpdaterLog = false, bool replacePlantedReparse = false)
{
    QDir source(srcDir);
    if (!source.exists()) {
        if (error) {
            *error = QStringLiteral("Source directory does not exist: %1").arg(QDir::toNativeSeparators(srcDir));
        }
        return false;
    }
    // [Codex F2] The source ROOT is not exempt either: backing up "the install dir"
    // when that directory is itself a junction would copy whatever it points at.
    if (isReparsePointPath(QDir::cleanPath(source.absolutePath()))) {
        if (error) {
            *error = QStringLiteral("Source tree root is a reparse point (symlink/junction/mount point); refusing: %1")
                         .arg(QDir::toNativeSeparators(srcDir));
        }
        return false;
    }
    if (replacePlantedReparse && destDir.compare(destRoot, kPathCase) != 0 && isReparsePointPath(destDir)) {
        if (!removeReparsePointItself(destDir, error)) {
            return false;
        }
    }
    if (refuseReparse(destRoot, destDir, error)) {
        return false;
    }
    if (!ensureDir(destDir, error)) {
        return false;
    }
    if (refuseReparse(destRoot, destDir, error)) {
        return false;
    }

    const auto entries = source.entryInfoList(QDir::Files | QDir::Dirs | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System);
    for (const QFileInfo& entry : entries) {
        const QString destPath = destDir + QLatin1Char('/') + entry.fileName();
        const QString rel = relPrefix.isEmpty() ? entry.fileName() : relPrefix + QLatin1Char('/') + entry.fileName();
        // [Codex r2 F4] The updater's live log is evidence, never payload: a backup does
        // not snapshot it and a restore never writes over it, so the failure + rollback
        // entries written DURING the update survive the rollback.
        if (skipTopLevelUpdaterLog && relPrefix.isEmpty() && entry.isFile()
            && entry.fileName().compare(kUpdaterLogName, kPathCase) == 0) {
            continue;
        }
        // A reparse point in the SOURCE tree is never ours: the staging dir is our own
        // extraction (which refuses links) and the install dir must not contain one.
        // Following it would copy or overwrite whatever it points at.
        if (isReparsePointPath(entry.absoluteFilePath())) {
            if (error) {
                *error = QStringLiteral("Source tree contains a reparse point (symlink/junction/mount point); refusing: %1")
                             .arg(QDir::toNativeSeparators(entry.absoluteFilePath()));
            }
            return false;
        }
        if (entry.isDir()) {
            if (!copyDirContents(entry.absoluteFilePath(), destPath, destRoot, relWritten, rel, error, false,
                                 replacePlantedReparse)) {
                return false;
            }
            continue;
        }
        if (replacePlantedReparse && isReparsePointPath(destPath)) {
            if (!removeReparsePointItself(destPath, error)) {
                return false;
            }
        }
        if (refuseReparse(destRoot, destPath, error)) {
            return false;
        }
        // Removing an existing name first also detaches a hardlink: the copy below then
        // creates a fresh file, and the other link's data is untouched (pinned by test).
        if (QFile::exists(destPath) && !QFile::remove(destPath)) {
            if (error) {
                *error = QStringLiteral("Could not overwrite: %1").arg(QDir::toNativeSeparators(destPath));
            }
            return false;
        }
        if (refuseReparse(destRoot, destPath, error)) {
            return false;
        }
        if (!QFile::copy(entry.absoluteFilePath(), destPath)) {
            if (error) {
                *error = QStringLiteral("Could not copy %1 -> %2")
                             .arg(QDir::toNativeSeparators(entry.absoluteFilePath()),
                                  QDir::toNativeSeparators(destPath));
            }
            return false;
        }
        if (relWritten) {
            relWritten->append(rel);
        }
    }
    return true;
}

// Remove every entry under destDir that backupDir does not contain, so a restore is
// the exact prior tree and not "prior tree plus whatever a failed apply added".
// Never removes the updater's own log. Fails closed on any reparse point.
bool pruneToBackup(const QString& backupDir, const QString& destDir, const QString& destRoot,
                   const QString& relPrefix, QString* error)
{
    const auto entries = QDir(destDir).entryInfoList(QDir::Files | QDir::Dirs | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System);
    for (const QFileInfo& entry : entries) {
        const QString rel = relPrefix.isEmpty() ? entry.fileName() : relPrefix + QLatin1Char('/') + entry.fileName();
        const QString mirror = backupDir + QLatin1Char('/') + rel;
        if (isReparsePointPath(entry.absoluteFilePath())) {
            // Foreign by construction (the backup has no reparse points): remove the
            // link itself, never its target. Exact restore proceeds.
            if (!removeReparsePointItself(entry.absoluteFilePath(), error)) {
                return false;
            }
            continue;
        }
        if (entry.isDir()) {
            if (!pruneToBackup(backupDir, entry.absoluteFilePath(), destRoot, rel, error)) {
                return false;
            }
            if (!QFileInfo::exists(mirror)) {
                // Contents are gone (pruned above); drop the directory itself.
                QDir().rmdir(entry.absoluteFilePath());
            }
            continue;
        }
        if (relPrefix.isEmpty() && entry.fileName().compare(kUpdaterLogName, kPathCase) == 0) {
            continue;
        }
        if (QFileInfo::exists(mirror)) {
            continue;
        }
        if (refuseReparse(destRoot, entry.absoluteFilePath(), error)) {
            return false;
        }
        if (!QFile::remove(entry.absoluteFilePath())) {
            if (error) {
                *error = QStringLiteral("Could not remove a file the backup does not contain: %1")
                             .arg(QDir::toNativeSeparators(entry.absoluteFilePath()));
            }
            return false;
        }
    }
    return true;
}

} // namespace

QString toExtendedLengthPath(const QString& path)
{
#if defined(Q_OS_WIN)
    const QString ext = QStringLiteral("\\\\?\\");   // extended-length prefix (drive form)
    const QString extUnc = QStringLiteral("\\\\?\\UNC\\");   // extended-length prefix (UNC form)
    QString native = QDir::toNativeSeparators(QDir::cleanPath(QFileInfo(path).absoluteFilePath()));
    if (native.startsWith(ext, Qt::CaseInsensitive)) {
        return native;                                          // already extended (drive or UNC form)
    }
    if (native.startsWith(QStringLiteral("\\\\"))) {
        return extUnc + native.mid(2);   // UNC: server-share form -> UNC extended form
    }
    return ext + native;
#else
    return path;
#endif
}

bool isReparsePointPath(const QString& path)
{
#if defined(Q_OS_WIN)
    // GetFileAttributesW does NOT follow reparse points, so a dangling link still
    // reports the attribute. Every tag counts: symlink, junction / mount point,
    // cloud placeholder, app-exec link -- none of them may sit under an install root.
    //
    // [Codex r2 F3] Tri-state, fail closed. The extended-length form keeps paths beyond
    // MAX_PATH (and UNC trees, [Codex r5]) answerable. A lookup that fails for any reason
    // other than "does not exist" (access denied, invalid name, network) is treated as
    // UNSAFE, and "does not exist" is cross-checked against Qt: a path the attribute
    // lookup cannot see but Qt can is also unsafe.
    const QString native = toExtendedLengthPath(path);
    const DWORD attrs = GetFileAttributesW(reinterpret_cast<LPCWSTR>(native.utf16()));
    if (attrs == INVALID_FILE_ATTRIBUTES) {
        const DWORD err = GetLastError();
        const bool missing = err == ERROR_FILE_NOT_FOUND || err == ERROR_PATH_NOT_FOUND;
        if (!missing) {
            return true;                    // lookup error: fail closed
        }
        return QFileInfo::exists(path) || QFileInfo(path).isSymLink();
    }
    return (attrs & FILE_ATTRIBUTE_REPARSE_POINT) != 0;
#else
    return QFileInfo(path).isSymLink();
#endif
}

bool removeTreeSafely(const QString& dir, QString* error)
{
    return removeTreeSafelyImpl(dir, error);
}

QString safeJoinWithinRoot(const QString& rootDir, const QString& entryName, bool* ok)
{
    const auto fail = [ok]() -> QString {
        if (ok) {
            *ok = false;
        }
        return QString();
    };

    const QString normalizedEntry = normalizeSeparators(entryName).trimmed();
    if (normalizedEntry.isEmpty()) {
        return fail();
    }
    // Embedded NULs or absolute / drive-letter / UNC entries are never allowed.
    if (normalizedEntry.contains(QChar(QChar::Null)) || QDir::isAbsolutePath(normalizedEntry)) {
        return fail();
    }
    // Reject a bare drive-relative entry such as "C:foo" that isAbsolutePath misses.
    if (normalizedEntry.size() >= 2 && normalizedEntry.at(1) == QLatin1Char(':')) {
        return fail();
    }

    const QString cleanRoot = QDir::cleanPath(QDir(rootDir).absolutePath());
    const QString candidate = QDir::cleanPath(cleanRoot + QLatin1Char('/') + normalizedEntry);

    // candidate must be the root itself or a descendant of root.
    if (candidate.compare(cleanRoot, kPathCase) == 0) {
        return fail(); // an entry resolving to the root dir itself is not a file target
    }
    const QString rootWithSep = cleanRoot.endsWith(QLatin1Char('/')) ? cleanRoot : cleanRoot + QLatin1Char('/');
    if (!candidate.startsWith(rootWithSep, kPathCase)) {
        return fail();
    }

    if (ok) {
        *ok = true;
    }
    return candidate;
}

bool pathCrossesReparsePoint(const QString& rootDir, const QString& path)
{
    const QString cleanRoot = QDir::cleanPath(QDir(rootDir).absolutePath());
    // [Codex F2] The root is not exempt: an install root that is itself a junction
    // sends every "inside the root" write somewhere else.
    if (isReparsePointPath(cleanRoot)) {
        return true;
    }
    const QString cleanPath = QDir::cleanPath(QFileInfo(path).absoluteFilePath());
    if (cleanPath.compare(cleanRoot, kPathCase) == 0) {
        return false;
    }
    const QString rootWithSep = cleanRoot.endsWith(QLatin1Char('/')) ? cleanRoot : cleanRoot + QLatin1Char('/');
    if (!cleanPath.startsWith(rootWithSep, kPathCase)) {
        return true;                        // not under the root at all: fail closed
    }
    const QStringList parts = cleanPath.mid(rootWithSep.size()).split(QLatin1Char('/'), Qt::SkipEmptyParts);
    QString current = cleanRoot;
    for (const QString& part : parts) {
        current += QLatin1Char('/') + part;
        if (isReparsePointPath(current)) {
            return true;
        }
        if (!QFileInfo::exists(current)) {
            break;                          // nothing below here exists yet
        }
    }
    return false;
}

ExtractResult extractZipSafely(const QString& zipPath, const QString& destDir, qint64 maxTotalBytes, int maxEntries)
{
    ExtractResult result;

    QZipReader reader(zipPath);
    if (!reader.exists() || !reader.isReadable()) {
        result.error = QStringLiteral("Archive is missing or unreadable.");
        return result;
    }

    const QList<QZipReader::FileInfo> infos = reader.fileInfoList();
    if (infos.isEmpty()) {
        result.error = QStringLiteral("Archive is empty.");
        return result;
    }
    if (maxEntries > 0 && infos.size() > maxEntries) {
        result.error = QStringLiteral("Archive entry count %1 exceeds limit.").arg(infos.size());
        return result;
    }

    qint64 totalBytes = 0;
    for (const auto& info : infos) {
        if (info.isSymLink) {
            result.error = QStringLiteral("Archive contains a symlink, which is not allowed.");
            result.rejectedEntry = info.filePath;
            return result;
        }
        if (info.size > 0) {
            totalBytes += info.size;
        }
        if (maxTotalBytes > 0 && totalBytes > maxTotalBytes) {
            result.error = QStringLiteral("Archive uncompressed size exceeds limit.");
            result.rejectedEntry = info.filePath;
            return result;
        }
        bool safe = false;
        const QString resolved = safeJoinWithinRoot(destDir, info.filePath, &safe);
        if (!safe || resolved.isEmpty()) {
            // Allow a directory entry whose path equals the root only when it is a
            // pure directory marker; everything else escaping the root is rejected.
            result.error = QStringLiteral("Archive entry escapes the install directory (path traversal).");
            result.rejectedEntry = info.filePath;
            return result;
        }
    }

    // [Codex r3 F2] Judge the existing root BEFORE clearing it: removeRecursively on a
    // junction root would delete through it, out of tree.
    if (isReparsePointPath(QDir::cleanPath(QDir(destDir).absolutePath()))) {
        result.error = QStringLiteral("Staging directory is a reparse point (symlink/junction/mount point).");
        return result;
    }
    // Start from a clean destination so a partial prior attempt cannot leak in --
    // cleared WITHOUT following anything inside it ([Codex r4 F2]).
    if (!removeTreeSafelyImpl(destDir, &result.error)) {
        if (result.error.isEmpty()) {
            result.error = QStringLiteral("Could not clear staging directory.");
        }
        return result;
    }
    if (!ensureDir(destDir, &result.error)) {
        return result;
    }
    if (isReparsePointPath(QDir::cleanPath(QDir(destDir).absolutePath()))) {
        result.error = QStringLiteral("Staging directory is a reparse point (symlink/junction/mount point).");
        return result;
    }

    const auto wipe = [&result, &destDir]() {
        QString ignored;
        removeTreeSafelyImpl(destDir, &ignored);   // never follows a link planted mid-extract
        result.files.clear();
    };
    const auto rejectReparse = [&](const QString& entry) {
        result.error = QStringLiteral("Staging path crosses a reparse point (symlink/junction/mount point).");
        result.rejectedEntry = entry;
        wipe();
        return result;
    };

    for (const auto& info : infos) {
        bool safe = false;
        const QString resolved = safeJoinWithinRoot(destDir, info.filePath, &safe);
        if (!safe) {
            wipe();
            result.error = QStringLiteral("Archive entry escapes the install directory (path traversal).");
            result.rejectedEntry = info.filePath;
            return result;
        }
        if (info.isDir) {
            if (pathCrossesReparsePoint(destDir, resolved)) {
                return rejectReparse(info.filePath);
            }
            if (!ensureDir(resolved, &result.error)) {
                wipe();
                return result;
            }
            if (pathCrossesReparsePoint(destDir, resolved)) {
                return rejectReparse(info.filePath);
            }
            continue;
        }
        const QString parent = QFileInfo(resolved).absolutePath();
        if (pathCrossesReparsePoint(destDir, parent)) {
            return rejectReparse(info.filePath);
        }
        if (!ensureDir(parent, &result.error)) {
            wipe();
            return result;
        }
        if (pathCrossesReparsePoint(destDir, resolved)) {
            return rejectReparse(info.filePath);
        }
        const QByteArray data = reader.fileData(info.filePath);
        // [Codex r5] A truncated or corrupt entry must never become "successfully
        // extracted": the bytes read must equal the entry's declared size, and the
        // bytes written must equal the bytes read. Anything else wipes the stage.
        if (info.size >= 0 && static_cast<qint64>(data.size()) != info.size) {
            result.error = QStringLiteral("Archive entry is truncated or corrupt (declared %1 bytes, read %2).")
                               .arg(info.size).arg(data.size());
            result.rejectedEntry = info.filePath;
            wipe();
            return result;
        }
        QFile out(resolved);
        if (!out.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            result.error = QStringLiteral("Could not write extracted file.");
            result.rejectedEntry = info.filePath;
            wipe();
            return result;
        }
        const qint64 written = out.write(data);
        const bool flushed = out.flush();
        out.close();
        if (written != data.size() || !flushed || out.error() != QFileDevice::NoError) {
            result.error = QStringLiteral("Short write while extracting (%1 of %2 bytes).")
                               .arg(written).arg(data.size());
            result.rejectedEntry = info.filePath;
            wipe();
            return result;
        }
        result.files.append(normalizeSeparators(info.filePath));
    }

    result.ok = true;
    return result;
}

bool backupTree(const QString& srcDir, const QString& backupDir, QString* error)
{
    // [Codex r3 F2] Same rule for the backup root: never clear through a reparse point.
    if (isReparsePointPath(QDir::cleanPath(QDir(backupDir).absolutePath()))) {
        if (error) {
            *error = QStringLiteral("Backup directory is a reparse point (symlink/junction/mount point); refusing: %1")
                         .arg(QDir::toNativeSeparators(backupDir));
        }
        return false;
    }
    if (!removeTreeSafelyImpl(backupDir, error)) {
        return false;
    }
    return copyDirContents(srcDir, backupDir, backupDir, nullptr, QString(), error, /*skipTopLevelUpdaterLog*/ true);
}

bool applyTree(const QString& srcDir, const QString& destDir, QStringList* applied, QString* error)
{
    return copyDirContents(srcDir, destDir, destDir, applied, QString(), error, false);
}

bool restoreTree(const QString& backupDir, const QString& destDir, QString* error)
{
    // [2026-09-21 Codex F4] An exact restore: copy the backup back over destDir, then
    // remove everything the backup does not contain. A failed apply may have added new
    // files before the entry that failed; leaving them behind would fail the release-
    // integrity manifest (unmanifested runtime files) or leave a loadable stale DLL.
    if (!copyDirContents(backupDir, destDir, destDir, nullptr, QString(), error, /*skipTopLevelUpdaterLog*/ true,
                         /*replacePlantedReparse*/ true)) {
        return false;
    }
    return pruneToBackup(QDir::cleanPath(QDir(backupDir).absolutePath()),
                         QDir::cleanPath(QDir(destDir).absolutePath()),
                         QDir::cleanPath(QDir(destDir).absolutePath()), QString(), error);
}

namespace {

bool safeManifestRelativeFile(const QString& rel)
{
    if (rel.isEmpty() || rel.contains(QLatin1Char('\\')) || rel.contains(QLatin1Char(':'))
        || rel.contains(QChar(QChar::Null)) || rel.startsWith(QLatin1Char('/'))
        || rel.endsWith(QLatin1Char('/')) || rel.contains(QStringLiteral("//"))) {
        return false;
    }
    for (const QString& part : rel.split(QLatin1Char('/'))) {
        if (part.isEmpty() || part == QLatin1String(".") || part == QLatin1String("..")) {
            return false;
        }
    }
    return QDir::cleanPath(rel) == rel;
}

QString manifestExpectedHash(const QJsonValue& value)
{
    const QString expected = (value.isObject() ? value.toObject().value(QStringLiteral("sha256")).toString()
                                               : value.toString()).trimmed().toLower();
    if (expected.size() != 64) return {};
    for (const QChar c : expected) {
        if (!c.isDigit() && (c < QLatin1Char('a') || c > QLatin1Char('f'))) return {};
    }
    return expected;
}

QString fileSha256(const QString& path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly)) return {};
    QCryptographicHash digest(QCryptographicHash::Sha256);
    while (!file.atEnd()) {
        const QByteArray chunk = file.read(1024 * 1024);
        if (chunk.isEmpty() && file.error() != QFileDevice::NoError) return {};
        digest.addData(chunk);
    }
    return QString::fromLatin1(digest.result().toHex());
}

bool isUpdaterRuntimeMember(const QString& rel)
{
    if (rel.compare(QLatin1String("OrionUpdater.exe"), Qt::CaseInsensitive) == 0) return true;
    if (!rel.contains(QLatin1Char('/')) && rel.endsWith(QLatin1String(".dll"), Qt::CaseInsensitive)) return true;
    static const QStringList kPluginRoots = {
        QStringLiteral("platforms/"), QStringLiteral("tls/"),
        QStringLiteral("networkinformation/"), QStringLiteral("generic/"),
        QStringLiteral("iconengines/"), QStringLiteral("imageformats/")};
    for (const QString& prefix : kPluginRoots) {
        if (rel.startsWith(prefix, Qt::CaseInsensitive)) return true;
    }
    return false;
}

QStringList updaterRuntimeMembers(const QJsonObject& files, QString* error)
{
    static const QStringList kRequired = {
        QStringLiteral("OrionUpdater.exe"), QStringLiteral("UpdaterCore.dll"),
        QStringLiteral("SecurityCore.dll"), QStringLiteral("OrionCommon.dll"),
        QStringLiteral("Qt6Core.dll"), QStringLiteral("Qt6Gui.dll"),
        QStringLiteral("Qt6Network.dll"), QStringLiteral("Qt6Widgets.dll"),
        QStringLiteral("platforms/qwindows.dll")};
    for (const QString& required : kRequired) {
        if (!files.contains(required)) {
            if (error) *error = QStringLiteral("Signed release manifest omits updater dependency: %1").arg(required);
            return {};
        }
    }
    QStringList selected;
    for (auto it = files.constBegin(); it != files.constEnd(); ++it) {
        const QString rel = it.key();
        if (!isUpdaterRuntimeMember(rel)) continue;
        if (!safeManifestRelativeFile(rel) || manifestExpectedHash(it.value()).isEmpty()) {
            if (error) *error = QStringLiteral("Unsafe updater runtime manifest entry: %1").arg(rel);
            return {};
        }
        selected.append(rel);
    }
    return selected;
}

bool verifiedFileAt(const QString& root, const QString& rel, const QJsonValue& value, QString* error)
{
    bool safe = false;
    const QString path = safeJoinWithinRoot(root, rel, &safe);
    if (!safe || pathCrossesReparsePoint(root, path) || !QFileInfo(path).isFile()) {
        if (error) *error = QStringLiteral("Updater runtime file missing or unsafe: %1").arg(rel);
        return false;
    }
    if (fileSha256(path) != manifestExpectedHash(value)) {
        if (error) *error = QStringLiteral("Updater runtime hash mismatch: %1").arg(rel);
        return false;
    }
    return true;
}

} // namespace

bool stageUpdaterRuntime(const QString& installDir, const QString& stageDir,
                         const QJsonObject& files, QString* error)
{
    const QStringList selected = updaterRuntimeMembers(files, error);
    if (selected.isEmpty()) return false;
    if (isReparsePointPath(installDir) || isReparsePointPath(stageDir)) {
        if (error) *error = QStringLiteral("Updater runtime root is a reparse point");
        return false;
    }
    if (QFileInfo(stageDir).exists()
        && !QDir(stageDir).entryList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System).isEmpty()) {
        if (error) *error = QStringLiteral("Updater staging directory is not empty");
        return false;
    }
    if (!QDir().mkpath(stageDir)) {
        if (error) *error = QStringLiteral("Cannot create updater staging directory");
        return false;
    }
    for (const QString& rel : selected) {
        if (!verifiedFileAt(installDir, rel, files.value(rel), error)) return false;
        bool srcSafe = false, destSafe = false;
        const QString src = safeJoinWithinRoot(installDir, rel, &srcSafe);
        const QString dest = safeJoinWithinRoot(stageDir, rel, &destSafe);
        if (!srcSafe || !destSafe || pathCrossesReparsePoint(stageDir, dest)
            || !QDir().mkpath(QFileInfo(dest).absolutePath())
            || pathCrossesReparsePoint(stageDir, dest)
            || !QFile::copy(src, dest)
            || !verifiedFileAt(stageDir, rel, files.value(rel), error)) {
            if (error && error->isEmpty()) *error = QStringLiteral("Could not stage updater runtime: %1").arg(rel);
            return false;
        }
    }
    return verifyStagedUpdaterRuntime(installDir, stageDir, files, error);
}

bool verifyStagedUpdaterRuntime(const QString& installDir, const QString& stageDir,
                                const QJsonObject& files, QString* error)
{
    Q_UNUSED(installDir);
    const QStringList selected = updaterRuntimeMembers(files, error);
    if (selected.isEmpty() || isReparsePointPath(stageDir)) return false;
    const QSet<QString> expected(selected.cbegin(), selected.cend());
    for (const QString& rel : selected) {
        if (!verifiedFileAt(stageDir, rel, files.value(rel), error)) return false;
    }
    QDirIterator it(stageDir, QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System,
                    QDirIterator::Subdirectories);
    while (it.hasNext()) {
        const QString path = it.next();
        if (isReparsePointPath(path)) {
            if (error) *error = QStringLiteral("Updater stage contains a reparse point");
            return false;
        }
        if (it.fileInfo().isFile()) {
            const QString rel = QDir(stageDir).relativeFilePath(path);
            if (!expected.contains(rel)) {
                if (error) *error = QStringLiteral("Updater stage contains an unverified file: %1").arg(rel);
                return false;
            }
        }
    }
    return true;
}

bool pruneRetiredRuntimeFiles(const QString& installDir, const QJsonObject& oldFiles,
                              const QJsonObject& newFiles, QString* error)
{
    if (oldFiles.isEmpty() || newFiles.isEmpty() || isReparsePointPath(installDir)) {
        if (error) *error = QStringLiteral("Cannot prune without verified old/new manifests and a plain install root");
        return false;
    }
    QStringList retired;
    QSet<QString> newNames;
    for (auto it = newFiles.constBegin(); it != newFiles.constEnd(); ++it) {
        newNames.insert(it.key().toLower());
    }
    for (auto it = oldFiles.constBegin(); it != oldFiles.constEnd(); ++it) {
        const QString rel = it.key();
        if (newNames.contains(rel.toLower())) continue;
        if (!safeManifestRelativeFile(rel)) {
            if (error) *error = QStringLiteral("Unsafe retired runtime path: %1").arg(rel);
            return false;
        }
        retired.append(rel);
    }
    // Validate the complete delta before deleting any entry.
    for (const QString& rel : retired) {
        bool safe = false;
        const QString path = safeJoinWithinRoot(installDir, rel, &safe);
        if (!safe || pathCrossesReparsePoint(installDir, path)) {
            if (error) *error = QStringLiteral("Retired runtime path crosses a reparse point: %1").arg(rel);
            return false;
        }
    }
    for (const QString& rel : retired) {
        bool safe = false;
        const QString path = safeJoinWithinRoot(installDir, rel, &safe);
        if (QFileInfo::exists(path) && (!QFileInfo(path).isFile() || !QFile::remove(path))) {
            if (error) *error = QStringLiteral("Cannot remove retired runtime file: %1").arg(rel);
            return false;
        }
    }
    return true;
}

} // namespace orion::updater
