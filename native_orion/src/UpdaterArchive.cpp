#include "UpdaterArchive.h"

#include <QtCore/QDir>
#include <QtCore/QDirIterator>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>

#include <QtCore/private/qzipreader_p.h>

namespace orion::updater {

namespace {

#if defined(Q_OS_WIN)
constexpr Qt::CaseSensitivity kPathCase = Qt::CaseInsensitive;
#else
constexpr Qt::CaseSensitivity kPathCase = Qt::CaseSensitive;
#endif

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

// Recursively copy the contents of srcDir into destDir, overwriting files.
bool copyDirContents(const QString& srcDir, const QString& destDir, QStringList* relWritten,
                     const QString& relPrefix, QString* error)
{
    QDir source(srcDir);
    if (!source.exists()) {
        if (error) {
            *error = QStringLiteral("Source directory does not exist: %1").arg(QDir::toNativeSeparators(srcDir));
        }
        return false;
    }
    if (!ensureDir(destDir, error)) {
        return false;
    }

    const auto entries = source.entryInfoList(QDir::Files | QDir::Dirs | QDir::NoDotAndDotDot | QDir::Hidden | QDir::System);
    for (const QFileInfo& entry : entries) {
        const QString destPath = destDir + QLatin1Char('/') + entry.fileName();
        const QString rel = relPrefix.isEmpty() ? entry.fileName() : relPrefix + QLatin1Char('/') + entry.fileName();
        if (entry.isDir()) {
            if (!copyDirContents(entry.absoluteFilePath(), destPath, relWritten, rel, error)) {
                return false;
            }
            continue;
        }
        if (QFile::exists(destPath) && !QFile::remove(destPath)) {
            if (error) {
                *error = QStringLiteral("Could not overwrite: %1").arg(QDir::toNativeSeparators(destPath));
            }
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

} // namespace

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

    // Start from a clean destination so a partial prior attempt cannot leak in.
    QDir destProbe(destDir);
    if (destProbe.exists() && !destProbe.removeRecursively()) {
        result.error = QStringLiteral("Could not clear staging directory.");
        return result;
    }
    if (!ensureDir(destDir, &result.error)) {
        return result;
    }

    for (const auto& info : infos) {
        bool safe = false;
        const QString resolved = safeJoinWithinRoot(destDir, info.filePath, &safe);
        if (!safe) {
            QDir(destDir).removeRecursively();
            result.files.clear();
            result.error = QStringLiteral("Archive entry escapes the install directory (path traversal).");
            result.rejectedEntry = info.filePath;
            return result;
        }
        if (info.isDir) {
            if (!ensureDir(resolved, &result.error)) {
                QDir(destDir).removeRecursively();
                result.files.clear();
                return result;
            }
            continue;
        }
        const QString parent = QFileInfo(resolved).absolutePath();
        if (!ensureDir(parent, &result.error)) {
            QDir(destDir).removeRecursively();
            result.files.clear();
            return result;
        }
        const QByteArray data = reader.fileData(info.filePath);
        QFile out(resolved);
        if (!out.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
            result.error = QStringLiteral("Could not write extracted file.");
            result.rejectedEntry = info.filePath;
            QDir(destDir).removeRecursively();
            result.files.clear();
            return result;
        }
        out.write(data);
        out.close();
        result.files.append(normalizeSeparators(info.filePath));
    }

    result.ok = true;
    return result;
}

bool backupTree(const QString& srcDir, const QString& backupDir, QString* error)
{
    QDir backup(backupDir);
    if (backup.exists() && !backup.removeRecursively()) {
        if (error) {
            *error = QStringLiteral("Could not clear backup directory.");
        }
        return false;
    }
    return copyDirContents(srcDir, backupDir, nullptr, QString(), error);
}

bool applyTree(const QString& srcDir, const QString& destDir, QStringList* applied, QString* error)
{
    return copyDirContents(srcDir, destDir, applied, QString(), error);
}

bool restoreTree(const QString& backupDir, const QString& destDir, QString* error)
{
    // Copy backup contents back over destDir. We intentionally do not delete files
    // that exist in destDir but not in backup: an interrupted apply may have added
    // new files, and overwriting the known-good set is sufficient to recover a
    // working install.
    return copyDirContents(backupDir, destDir, nullptr, QString(), error);
}

} // namespace orion::updater
