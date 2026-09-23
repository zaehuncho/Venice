#include "UpdaterTrust.h"

#include "UpdaterArchive.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>

namespace orion::updater {

namespace {

#if defined(Q_OS_WIN)
constexpr Qt::CaseSensitivity kPathCase = Qt::CaseInsensitive;
#else
constexpr Qt::CaseSensitivity kPathCase = Qt::CaseSensitive;
#endif

// Read {"keys": {"<id>": "<encoded>"}} from `path` and decode the entry for keyId.
// Any failure (missing file, bad JSON, unknown id, wrong length) yields an empty
// array so the caller falls through to the next source or fails closed.
QByteArray keyFromFile(const QString& path, const QString& keyId, const PublicKeyDecoder& decode)
{
    if (path.isEmpty() || !decode) {
        return {};
    }
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) {
        return {};
    }
    const QJsonObject root = QJsonDocument::fromJson(f.readAll()).object();
    const QJsonObject keys = root.value(QStringLiteral("keys")).toObject();
    const QString encoded = keys.value(keyId).toString();
    if (encoded.isEmpty()) {
        return {};
    }
    const QByteArray decoded = decode(encoded);
    return decoded.size() == 32 ? decoded : QByteArray();
}

} // namespace

TrustRootDecision resolveTrustRoot(const QString& keyId,
                                   const QList<EmbeddedKey>& embedded,
                                   const TrustRootSources& sources,
                                   bool productionBuild,
                                   const PublicKeyDecoder& decode)
{
    TrustRootDecision decision;
    const QString id = keyId.trimmed();
    if (id.isEmpty()) {
        return decision;
    }
    for (const EmbeddedKey& e : embedded) {
        if (!e.id.isEmpty() && id == e.id && e.key.size() == 32) {
            decision.publicKey = e.key;
            decision.source = QStringLiteral("embedded");
            return decision;
        }
    }

    struct External {
        const char* name;
        QString path;
    };
    const External externals[] = {
        {"cli", sources.cliPubkeysFile},
        {"env", sources.envPubkeysFile},
        {"install_dir", sources.installDir.isEmpty()
                            ? QString()
                            : sources.installDir + QStringLiteral("/update_pubkeys.json")},
        {"app_dir", sources.applicationDir.isEmpty()
                        ? QString()
                        : sources.applicationDir + QStringLiteral("/update_pubkeys.json")},
    };

    if (productionBuild) {
        // Report what was offered so the log shows the override attempt, then
        // fail closed: no external file may become the trust root.
        for (const External& ext : externals) {
            if (!ext.path.isEmpty() && QFile::exists(ext.path)) {
                decision.ignored << QString::fromLatin1(ext.name) + QLatin1Char(':') + ext.path;
            }
        }
        return decision;
    }

    for (const External& ext : externals) {
        const QByteArray key = keyFromFile(ext.path, id, decode);
        if (key.size() == 32) {
            decision.publicKey = key;
            decision.source = QString::fromLatin1(ext.name);
            return decision;
        }
    }
    return decision;
}

bool localManifestAllowed(bool productionBuild)
{
    return !productionBuild;
}

bool productionManifestUrlAllowed(const QUrl& url)
{
    return url.isValid() && !url.isRelative()
        && url.scheme().compare(QLatin1String("https"), Qt::CaseInsensitive) == 0
        && url.host().compare(QLatin1String("api.zaeorion.com"), Qt::CaseInsensitive) == 0
        && url.port(443) == 443
        && url.path() == QLatin1String("/api/update")
        && url.userName().isEmpty() && url.password().isEmpty()
        && !url.hasFragment();
}

QString buildProfileOutputPath(const QString& requestedPath,
                               const QString& tempRoot,
                               QString* error)
{
    const auto fail = [error](const QString& why) -> QString {
        if (error) {
            *error = why;
        }
        return QString();
    };
    if (requestedPath.trimmed().isEmpty()) {
        return fail(QStringLiteral("no output path"));
    }
    const QString root = QDir::cleanPath(QDir(tempRoot).absolutePath());
    const QString target = QDir::cleanPath(QFileInfo(requestedPath).absoluteFilePath());
    const QString rootWithSep = root.endsWith(QLatin1Char('/')) ? root : root + QLatin1Char('/');
    if (!target.startsWith(rootWithSep, kPathCase)) {
        return fail(QStringLiteral("build-profile output must be under the temp directory"));
    }
    if (QFileInfo::exists(target) || QFileInfo(target).isSymLink()) {
        return fail(QStringLiteral("build-profile output must be a NEW file"));
    }
    if (pathCrossesReparsePoint(root, target)) {
        return fail(QStringLiteral("build-profile output path crosses a reparse point"));
    }
    return target;
}

QString relaunchExecutableName(const QString& requested,
                               bool productionBuild,
                               bool* ignoredOverride,
                               QString* error)
{
    const QString kDefault = QStringLiteral("OrionNative.exe");
    const QString name = requested.trimmed();
    if (ignoredOverride) {
        *ignoredOverride = false;
    }
    if (productionBuild) {
        if (!name.isEmpty() && name.compare(kDefault, kPathCase) != 0 && ignoredOverride) {
            *ignoredOverride = true;
        }
        return kDefault;
    }
    if (name.isEmpty()) {
        return kDefault;
    }
    const auto fail = [error](const QString& why) -> QString {
        if (error) {
            *error = why;
        }
        return QString();
    };
    if (name.contains(QLatin1Char('/')) || name.contains(QLatin1Char('\\'))
        || name.contains(QLatin1Char(':')) || name.contains(QStringLiteral(".."))
        || name.startsWith(QLatin1Char('.')) || QDir::isAbsolutePath(name)
        || name != QFileInfo(name).fileName()) {
        return fail(QStringLiteral("--relaunch must be a bare executable name inside the install directory"));
    }
    if (!name.endsWith(QStringLiteral(".exe"), Qt::CaseInsensitive)) {
        return fail(QStringLiteral("--relaunch must name a .exe"));
    }
    return name;
}

QString safeRelaunchPath(const QString& installDir,
                         const QString& executableName,
                         QString* error)
{
    const auto fail = [error](const QString& why) -> QString {
        if (error) {
            *error = why;
        }
        return QString();
    };
    const QString root = QDir::cleanPath(QDir(installDir).absolutePath());
    const QString path = QDir::cleanPath(root + QLatin1Char('/') + executableName);
    const QString rootWithSep = root.endsWith(QLatin1Char('/')) ? root : root + QLatin1Char('/');
    if (!path.startsWith(rootWithSep, kPathCase) || path.mid(rootWithSep.size()).contains(QLatin1Char('/'))) {
        return fail(QStringLiteral("relaunch target escapes the install directory"));
    }
    if (pathCrossesReparsePoint(root, path)) {
        return fail(QStringLiteral("relaunch target crosses a reparse point"));
    }
    const QFileInfo info(path);
    if (!info.exists() || !info.isFile()) {
        return fail(QStringLiteral("relaunch target is missing or not a regular file"));
    }
    return path;
}

QString bindInstallRoot(const QString& requestedInstallDir,
                        const QString& applicationDir,
                        bool productionBuild,
                        QString* error)
{
    const auto fail = [error](const QString& why) -> QString {
        if (error) {
            *error = why;
        }
        return QString();
    };
    const QString appDir = QDir::cleanPath(QDir(applicationDir).absolutePath());
    const QString requested = requestedInstallDir.trimmed().isEmpty()
        ? appDir
        : QDir::cleanPath(QDir(requestedInstallDir).absolutePath());
    // [Codex r6 F3] A UNC install root is not a supported location (the installer fixes the
    // install under Program Files) and its reparse handling was never exercised on a real
    // share, so it is refused outright instead of being claimed. cleanPath keeps UNC roots
    // as //server/share, so the test is scheme-independent.
    if (requested.startsWith(QStringLiteral("//"))) {
        return fail(QStringLiteral("Install directory on a network (UNC) path is not supported; refusing: %1")
                        .arg(QDir::toNativeSeparators(requested)));
    }
    if (!QFileInfo(requested).isDir()) {
        return fail(QStringLiteral("Install directory does not exist: %1").arg(QDir::toNativeSeparators(requested)));
    }
    if (isReparsePointPath(requested)) {
        return fail(QStringLiteral("Install directory is a reparse point (symlink/junction/mount point); refusing: %1")
                        .arg(QDir::toNativeSeparators(requested)));
    }
    if (productionBuild) {
        // The launcher passes its own directory, which is where this updater lives too.
        // Anything else is an attempt to aim an elevated writer at a foreign tree.
        const QString canonicalRequested = QFileInfo(requested).canonicalFilePath();
        const QString canonicalApp = QFileInfo(appDir).canonicalFilePath();
        if (canonicalRequested.isEmpty() || canonicalApp.isEmpty()
            || canonicalRequested.compare(canonicalApp, kPathCase) != 0) {
            return fail(QStringLiteral("Install directory must be the updater's own directory in production builds; refusing: %1")
                            .arg(QDir::toNativeSeparators(requested)));
        }
    }
    return requested;
}

QString bindStagedInstallRoot(const QString& requestedInstallDir,
                              const QString& applicationDir,
                              bool productionBuild,
                              QString* error)
{
    Q_UNUSED(productionBuild);
    const auto fail = [error](const QString& why) -> QString {
        if (error) *error = why;
        return {};
    };
    if (requestedInstallDir.trimmed().isEmpty()) {
        return fail(QStringLiteral("Staged updater requires an explicit install directory"));
    }
    const QString install = QDir::cleanPath(QDir(requestedInstallDir).absolutePath());
    const QString stage = QDir::cleanPath(QDir(applicationDir).absolutePath());
    if (install.startsWith(QStringLiteral("//")) || stage.startsWith(QStringLiteral("//"))
        || !QFileInfo(install).isDir() || isReparsePointPath(install)
        || isReparsePointPath(stage)) {
        return fail(QStringLiteral("Staged updater install/stage root is missing or unsafe"));
    }
    const QFileInfo stageInfo(stage);
    if (!stageInfo.fileName().startsWith(QStringLiteral(".orion_updater_stage-"))) {
        return fail(QStringLiteral("Staged updater is not in a designated sibling directory"));
    }
    const QString installParent = QFileInfo(install).dir().canonicalPath();
    const QString stageParent = stageInfo.dir().canonicalPath();
    if (installParent.isEmpty() || stageParent.isEmpty()
        || installParent.compare(stageParent, kPathCase) != 0
        || stage.compare(install, kPathCase) == 0) {
        return fail(QStringLiteral("Staged updater directory is not a sibling of the install"));
    }
    return install;
}

} // namespace orion::updater
