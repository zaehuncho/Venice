#pragma once

#include <QtCore/QDir>
#include <QtCore/QCryptographicHash>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonObject>
#include <QtCore/QString>

namespace orion::remote_play_executable_policy {

enum class Source {
    None,
    Packaged,
    Repository,
    DeveloperConfigured,
};

struct Selection final {
    QString path;
    Source source = Source::None;
};

struct ExecutableIdentity final {
    bool valid = false;
    qint64 size = -1;
    QString sha256;
    QString error;
};

// Hash the selected image through one open QFile and reject visible path/size/
// timestamp transitions around the read. Python independently checks the same
// native-issued size/hash against the exact named-pipe server process.
[[nodiscard]] inline ExecutableIdentity readExecutableIdentity(const QString& path)
{
    ExecutableIdentity out;
    const QFileInfo before(path);
    const QString canonicalBefore = before.canonicalFilePath();
    if (canonicalBefore.isEmpty() || !before.exists() || !before.isFile()) {
        out.error = QStringLiteral("executable path is not a canonical regular file");
        return out;
    }

    QFile file(canonicalBefore);
    if (!file.open(QIODevice::ReadOnly)) {
        out.error = file.errorString();
        return out;
    }
    const qint64 openedSize = file.size();
    QCryptographicHash hash(QCryptographicHash::Sha256);
    constexpr qint64 kChunkBytes = 1024 * 1024;
    while (!file.atEnd()) {
        const QByteArray chunk = file.read(kChunkBytes);
        if (chunk.isEmpty() && !file.atEnd()) {
            out.error = file.errorString();
            return out;
        }
        hash.addData(chunk);
    }
    if (file.error() != QFileDevice::NoError || file.size() != openedSize) {
        out.error = file.error() == QFileDevice::NoError
            ? QStringLiteral("executable changed while hashing")
            : file.errorString();
        return out;
    }

    const QFileInfo after(canonicalBefore);
    if (!after.exists() || !after.isFile()
        || after.canonicalFilePath() != canonicalBefore
        || after.size() != openedSize
        || before.size() != openedSize
        || after.lastModified() != before.lastModified()) {
        out.error = QStringLiteral("executable identity changed while hashing");
        return out;
    }
    out.valid = true;
    out.size = openedSize;
    out.sha256 = QString::fromLatin1(hash.result().toHex());
    return out;
}

// Canonical strings keep the sidecar boundary exact: size never traverses JSON
// as a floating-point number, and missing identity is represented explicitly.
[[nodiscard]] inline bool insertDecoderPipeProducerExpectation(
    QJsonObject& object, const QString& path)
{
    const ExecutableIdentity identity = readExecutableIdentity(path);
    object.insert(QStringLiteral("chiaki_identity_size"),
                  identity.valid ? QString::number(identity.size) : QString());
    object.insert(QStringLiteral("chiaki_identity_sha256"),
                  identity.valid ? identity.sha256 : QString());
    return identity.valid;
}

[[nodiscard]] inline QString packagedOrionStreamPath(const QString& applicationDir)
{
    if (applicationDir.trimmed().isEmpty()) {
        return {};
    }
    return QDir::toNativeSeparators(
        QDir(applicationDir).filePath(
            QStringLiteral("chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe")));
}

[[nodiscard]] inline QString repositoryOrionStreamPath(const QString& rootDir)
{
    if (rootDir.trimmed().isEmpty()) {
        return {};
    }
    return QDir::toNativeSeparators(
        QDir(rootDir).filePath(
            QStringLiteral("native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe")));
}

// Return a canonical regular file only when it remains inside `ownerDir` after
// resolving links/junctions. Production constructs one exact install-relative
// path, but this containment check also prevents a bundled-looking reparse path
// from silently escaping to another checkout.
[[nodiscard]] inline QString existingFileWithin(
    const QString& candidate, const QString& ownerDir)
{
    if (candidate.trimmed().isEmpty() || ownerDir.trimmed().isEmpty()) {
        return {};
    }

    const QFileInfo fileInfo(candidate);
    if (!fileInfo.exists() || !fileInfo.isFile()) {
        return {};
    }

    const QString canonicalOwner = QDir(ownerDir).canonicalPath();
    const QString canonicalFile = fileInfo.canonicalFilePath();
    if (canonicalOwner.isEmpty() || canonicalFile.isEmpty()) {
        return {};
    }

    const QString relative = QDir(canonicalOwner).relativeFilePath(canonicalFile);
    if (relative == QLatin1String("..")
        || relative.startsWith(QStringLiteral("../"))
        || relative.startsWith(QStringLiteral("..\\"))
        || QDir::isAbsolutePath(relative)) {
        return {};
    }
    return QDir::toNativeSeparators(canonicalFile);
}

[[nodiscard]] inline QString existingRegularFile(const QString& candidate)
{
    const QFileInfo fileInfo(candidate);
    if (!fileInfo.exists() || !fileInfo.isFile()) {
        return {};
    }
    const QString canonical = fileInfo.canonicalFilePath();
    return canonical.isEmpty() ? QDir::toNativeSeparators(fileInfo.absoluteFilePath())
                               : QDir::toNativeSeparators(canonical);
}

// Production has exactly one permitted source: the OrionStream image shipped
// below the running executable. An existing repository/configured executable is
// intentionally ignored if that image is absent. Development retains the local
// deploy tree and explicit configured path as compile-gated fallbacks.
[[nodiscard]] inline Selection select(
    const QString& applicationDir,
    const QString& rootDir,
    const QString& configuredPath,
    bool productionBuild)
{
    const QString packaged = existingFileWithin(
        packagedOrionStreamPath(applicationDir), applicationDir);
    if (!packaged.isEmpty()) {
        return {packaged, Source::Packaged};
    }

    if (productionBuild) {
        return {};
    }

    const QString repository = existingFileWithin(
        repositoryOrionStreamPath(rootDir), rootDir);
    if (!repository.isEmpty()) {
        return {repository, Source::Repository};
    }

    const QString configured = existingRegularFile(configuredPath.trimmed());
    if (!configured.isEmpty()) {
        return {configured, Source::DeveloperConfigured};
    }
    return {};
}

[[nodiscard]] inline QString sourceName(Source source)
{
    switch (source) {
    case Source::Packaged:
        return QStringLiteral("packaged");
    case Source::Repository:
        return QStringLiteral("repository");
    case Source::DeveloperConfigured:
        return QStringLiteral("developer-configured");
    case Source::None:
        break;
    }
    return QStringLiteral("none");
}

} // namespace orion::remote_play_executable_policy
