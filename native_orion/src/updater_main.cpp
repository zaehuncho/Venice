// OrionUpdater.exe
//
// A standalone, single-shot updater shipped next to OrionNative.exe. OrionNative
// cannot overwrite itself while running, so the launcher hands off to this small
// executable, exits, and lets the updater wait, verify, replace, and relaunch.
//
// Hard rules (see docs/UPDATER_CLIENT.md):
//   * download sources MUST be HTTPS
//   * the manifest signature MUST verify (Ed25519; only the PUBLIC key ships with
//     the client — no shared secret is ever required)
//   * signature_alg MUST be "ed25519" and public_key_id MUST resolve to a trusted key
//   * the artifact SHA-256 MUST match the signed manifest
//   * the manifest version MUST be newer (downgrades require allow_rollback)
//   * every archive entry MUST stay inside the install dir (no path traversal)
//   * on any apply failure the previous install is restored from backup
// Nothing downloaded is executed until every gate above passes.

#include "UpdateManifest.h"
#include "UpdaterArchive.h"
#include "UpdaterTrust.h"
#include "SecurityManager.h"

#include <QtCore/QCommandLineOption>
#include <QtCore/QCommandLineParser>
#include <QtCore/QCoreApplication>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QElapsedTimer>
#include <QtCore/QEventLoop>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QCryptographicHash>
#include <QtCore/QJsonArray>
#include <QtCore/QSaveFile>
#include <QtCore/QStandardPaths>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QProcess>
#include <QtCore/QStringList>
#include <QtCore/QThread>
#include <QtCore/QTemporaryDir>
#include <QtCore/QTextStream>
#include <QtCore/QTimer>
#include <QtNetwork/QNetworkAccessManager>
#include <QtNetwork/QNetworkReply>
#include <QtNetwork/QNetworkRequest>
#include <QtNetwork/QSslConfiguration>
#include <QtNetwork/QSslSocket>
#include <QtWidgets/QApplication>
#include <QtWidgets/QLabel>
#include <QtWidgets/QPlainTextEdit>
#include <QtWidgets/QProgressBar>
#include <QtWidgets/QPushButton>
#include <QtWidgets/QVBoxLayout>
#include <QtWidgets/QWidget>

#ifdef Q_OS_WIN
#include <Windows.h>
#include <TlHelp32.h>
#endif

using namespace orion;

namespace {

constexpr int kManifestTimeoutMs = 15'000;
constexpr int kArtifactTimeoutMs = 120'000;
constexpr int kLauncherWaitMs = 30'000;

struct UpdaterConfig {
    QString manifestUrl;
    QString manifestFile;   // local manifest source (testing / offline)
    QString artifactFile;   // local artifact source (development fixture only)
    QString installDir;
    QString relaunchExe = QStringLiteral("OrionNative.exe");
    QString currentVersion;
    QString pubkeysFile;
    qint64 launcherPid = 0;
    qint64 bootstrapPid = 0;
    bool dryRun = false;
    bool relaunch = true;
    bool exitOnComplete = false;
    bool injectFailureAfterPrune = false; // development fixture only
    bool stagedHelper = false;
};

#if defined(ORION_PRODUCTION_BUILD)
constexpr bool kProductionBuild = true;
#else
constexpr bool kProductionBuild = false;
#endif

// Resolve a trusted Ed25519 PUBLIC key for the manifest's public_key_id. Public
// keys are not secret. The compile-time embedded key is the trust root; in
// development builds a shipped update_pubkeys.json ({"keys": {"<id>": "<hex|base64>"}})
// may add rotated keys. PRODUCTION trusts only the embedded key -- see
// UpdaterTrust.h for why every external source is refused there. No shared
// secret / HMAC key is ever required (fail closed on an unknown id).
// The keys compiled into this binary: the primary, plus an optional second one that
// exists only during a planned rotation (docs/UPDATER_CLIENT.md "Key rotation").
QList<updater::EmbeddedKey> embeddedKeys()
{
    QList<updater::EmbeddedKey> keys;
#if defined(ORION_UPDATE_ED25519_PUBKEY_ID) && defined(ORION_UPDATE_ED25519_PUBKEY)
    keys.append({QStringLiteral(ORION_UPDATE_ED25519_PUBKEY_ID),
                 decodeEd25519PublicKey(QStringLiteral(ORION_UPDATE_ED25519_PUBKEY))});
#endif
#if defined(ORION_UPDATE_ED25519_PUBKEY2_ID) && defined(ORION_UPDATE_ED25519_PUBKEY2)
    keys.append({QStringLiteral(ORION_UPDATE_ED25519_PUBKEY2_ID),
                 decodeEd25519PublicKey(QStringLiteral(ORION_UPDATE_ED25519_PUBKEY2))});
#endif
    return keys;
}

updater::TrustRootDecision resolvePublicKey(const QString& keyId, const UpdaterConfig& cfg)
{
    updater::TrustRootSources sources;
    sources.cliPubkeysFile = cfg.pubkeysFile;
    sources.envPubkeysFile = QString::fromLocal8Bit(qgetenv("ORION_UPDATE_PUBKEYS"));
    sources.installDir = cfg.installDir;
    sources.applicationDir = QCoreApplication::applicationDirPath();
    return updater::resolveTrustRoot(keyId, embeddedKeys(), sources, kProductionBuild,
                                     [](const QString& encoded) { return decodeEd25519PublicKey(encoded); });
}

// [Codex F1] Build attestation for the packager: what this binary was compiled as.
// Written as a file (this is a WIN32 GUI executable; stdout is not dependable).
bool writeBuildProfile(const QString& path)
{
    QJsonObject obj;
    obj.insert(QStringLiteral("profile"), kProductionBuild ? QStringLiteral("production")
                                                           : QStringLiteral("development"));
    obj.insert(QStringLiteral("version"), QStringLiteral(ORION_NATIVE_VERSION));
    QJsonArray ids;
    QJsonArray keys;
    for (const updater::EmbeddedKey& k : embeddedKeys()) {
        if (k.key.size() == 32) {
            ids.append(k.id);
            // [Codex r3 F1] Bind the id to the BYTES: the packager compares this
            // fingerprint against the pinned production public key, so a binary that
            // reports the right id over different key material is refused.
            QJsonObject entry;
            entry.insert(QStringLiteral("id"), k.id);
            entry.insert(QStringLiteral("sha256"), QString::fromLatin1(
                QCryptographicHash::hash(k.key, QCryptographicHash::Sha256).toHex()));
            keys.append(entry);
        }
    }
    obj.insert(QStringLiteral("embedded_key_ids"), ids);
    obj.insert(QStringLiteral("embedded_keys"), keys);
    obj.insert(QStringLiteral("local_manifest_allowed"), updater::localManifestAllowed(kProductionBuild));
    // [Codex r2] Constrained target + atomic write: never an arbitrary-file-write primitive.
    QString why;
    const QString target = updater::buildProfileOutputPath(
        path, QStandardPaths::writableLocation(QStandardPaths::TempLocation), &why);
    if (target.isEmpty()) {
        return false;
    }
    QSaveFile f(target);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        return false;
    }
    f.write(QJsonDocument(obj).toJson(QJsonDocument::Compact));
    return f.commit();
}

QJsonObject verifiedReleaseFiles(const QString& root, QString* error)
{
    const QString manifestPath = QDir(root).absoluteFilePath(QStringLiteral("release_manifest.json"));
    if (!QFileInfo(manifestPath).isFile()) {
        if (error) *error = QStringLiteral("Signed release manifest is missing from the exact install/stage root");
        return {};
    }
    QFile before(manifestPath);
    if (!before.open(QIODevice::ReadOnly)) {
        if (error) *error = QStringLiteral("Could not read release manifest before verification");
        return {};
    }
    const QByteArray bytesBefore = before.readAll();
    before.close();
    SecurityManager verifier(root);
    if (!verifier.verifyReleaseIntegrity(error, /*exactRoot*/ true)) return {};
    QFile manifestFile(manifestPath);
    if (!manifestFile.open(QIODevice::ReadOnly)) {
        if (error) *error = QStringLiteral("Could not reopen verified release manifest");
        return {};
    }
    const QByteArray bytesAfter = manifestFile.readAll();
    if (bytesAfter != bytesBefore) {
        if (error) *error = QStringLiteral("Release manifest changed during verification");
        return {};
    }
    const QJsonObject files = QJsonDocument::fromJson(bytesAfter).object()
                                  .value(QStringLiteral("files")).toObject();
    if (files.isEmpty() && error) *error = QStringLiteral("Verified release manifest has no files");
    return files;
}

#ifdef Q_OS_WIN
bool waitForPidExit(qint64 pid, QString* error)
{
    if (pid <= 0) return true;
    HANDLE handle = OpenProcess(SYNCHRONIZE, FALSE, static_cast<DWORD>(pid));
    if (!handle) {
        if (GetLastError() == ERROR_INVALID_PARAMETER) return true; // already exited
        if (error) *error = QStringLiteral("Cannot inspect update owner process %1").arg(pid);
        return false;
    }
    const DWORD result = WaitForSingleObject(handle, kLauncherWaitMs);
    CloseHandle(handle);
    if (result == WAIT_OBJECT_0) return true;
    if (error) *error = QStringLiteral("Update owner process %1 did not exit within 30 seconds").arg(pid);
    return false;
}

bool installOwnersGone(const QString& installDir, QString* error)
{
    const QString root = QDir::cleanPath(QDir(installDir).absolutePath()) + QLatin1Char('/');
    QElapsedTimer elapsed;
    elapsed.start();
    for (;;) {
        HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
        if (snapshot == INVALID_HANDLE_VALUE) {
            if (error) *error = QStringLiteral("Cannot inspect install-local processes");
            return false;
        }
        bool liveOwner = false;
        PROCESSENTRY32W entry{};
        entry.dwSize = sizeof(entry);
        if (Process32FirstW(snapshot, &entry)) {
            do {
                if (entry.th32ProcessID == GetCurrentProcessId()) continue;
                const QString name = QString::fromWCharArray(entry.szExeFile).toLower();
                const bool knownOwner = name == QLatin1String("orionnative.exe")
                    || name == QLatin1String("orionupdater.exe")
                    || name == QLatin1String("orionsidecar.exe")
                    || name == QLatin1String("orionstream.exe")
                    || name == QLatin1String("venicenet.exe");
                HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE,
                                             FALSE, entry.th32ProcessID);
                if (!process) {
                    if (knownOwner) liveOwner = true; // cannot prove it is outside install
                    continue;
                }
                wchar_t path[MAX_PATH * 4]{};
                DWORD capacity = static_cast<DWORD>(sizeof(path) / sizeof(path[0]));
                if (QueryFullProcessImageNameW(process, 0, path, &capacity)) {
                    const QString executable = QDir::fromNativeSeparators(QString::fromWCharArray(path, capacity));
                    if (executable.startsWith(root, Qt::CaseInsensitive)
                        && WaitForSingleObject(process, 0) != WAIT_OBJECT_0) {
                        liveOwner = true;
                    }
                } else if (knownOwner) {
                    liveOwner = true;
                }
                // A process launched elsewhere can still map an install-local
                // DLL. Include its module list, not just the executable path.
                if (!liveOwner && WaitForSingleObject(process, 0) != WAIT_OBJECT_0) {
                    HANDLE modules = CreateToolhelp32Snapshot(
                        TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, entry.th32ProcessID);
                    if (modules != INVALID_HANDLE_VALUE) {
                        MODULEENTRY32W module{};
                        module.dwSize = sizeof(module);
                        if (Module32FirstW(modules, &module)) {
                            do {
                                const QString modulePath = QDir::fromNativeSeparators(
                                    QString::fromWCharArray(module.szExePath));
                                if (modulePath.startsWith(root, Qt::CaseInsensitive)) {
                                    liveOwner = true;
                                    break;
                                }
                            } while (Module32NextW(modules, &module));
                        }
                        CloseHandle(modules);
                    }
                }
                CloseHandle(process);
            } while (Process32NextW(snapshot, &entry));
        }
        CloseHandle(snapshot);
        if (!liveOwner) return true;
        if (elapsed.elapsed() >= kLauncherWaitMs) {
            if (error) *error = QStringLiteral("Install-local process/service did not exit within 30 seconds");
            return false;
        }
        QThread::msleep(200);
    }
}
#endif

} // namespace

// -------- progress window --------------------------------------------------

class UpdaterWindow final : public QWidget {
    Q_OBJECT
public:
    UpdaterWindow()
    {
        setWindowTitle(QStringLiteral("Venice Updater"));
        setMinimumWidth(460);
        // Match the launcher's dark visual system (OrionNative Main.qml palette).
        setStyleSheet(QStringLiteral(
            "QWidget { background-color: #0B0F14; color: #E5EAF2; font-family: 'Segoe UI Variable','Segoe UI'; font-size: 13px; }"
            "QLabel#statusLabel { color: #F4F7FA; font-size: 15px; font-weight: 600; }"
            "QProgressBar { background-color: #16202C; border: 1px solid #263241; border-radius: 6px; height: 14px; text-align: center; color: #9AA8BF; font-size: 10px; }"
            "QProgressBar::chunk { background-color: #4F8CFF; border-radius: 5px; }"
            "QPlainTextEdit { background-color: #080C12; border: 1px solid #1B2738; border-radius: 8px; color: #C3CDDB; font-family: 'Cascadia Mono','Consolas'; font-size: 11px; }"
            "QPushButton { background-color: #16202C; border: 1px solid #263241; border-radius: 8px; padding: 8px 18px; color: #E5EAF2; }"
            "QPushButton:hover { background-color: #1B2738; }"
            "QPushButton:disabled { color: #566273; }"));
        auto* layout = new QVBoxLayout(this);
        layout->setContentsMargins(20, 20, 20, 20);
        layout->setSpacing(12);

        status_ = new QLabel(QStringLiteral("Preparing update..."), this);
        status_->setObjectName(QStringLiteral("statusLabel"));
        progress_ = new QProgressBar(this);
        progress_->setRange(0, 100);
        log_ = new QPlainTextEdit(this);
        log_->setReadOnly(true);
        log_->setMinimumHeight(160);
        close_ = new QPushButton(QStringLiteral("Close"), this);
        close_->setEnabled(false);
        connect(close_, &QPushButton::clicked, this, &QWidget::close);

        layout->addWidget(status_);
        layout->addWidget(progress_);
        layout->addWidget(log_);
        layout->addWidget(close_);
    }

    void setStatus(const QString& text) { status_->setText(text); }
    void setProgress(int pct) { progress_->setValue(pct); }
    void appendLog(const QString& line) { log_->appendPlainText(line); }
    void finish(bool /*success*/) { close_->setEnabled(true); }

private:
    QLabel* status_ = nullptr;
    QProgressBar* progress_ = nullptr;
    QPlainTextEdit* log_ = nullptr;
    QPushButton* close_ = nullptr;
};

// -------- the update flow --------------------------------------------------

class UpdateRunner final : public QObject {
    Q_OBJECT
public:
    UpdateRunner(UpdaterConfig cfg, UpdaterWindow* window)
        : cfg_(std::move(cfg)), window_(window)
    {
        logPath_ = cfg_.installDir + QStringLiteral("/orion_updater.log");
    }

    void run()
    {
        log(QStringLiteral("Orion updater started (current=%1, install=%2)")
                .arg(cfg_.currentVersion, QDir::toNativeSeparators(cfg_.installDir)));

        if (cfg_.installDir.isEmpty() || !QFileInfo(cfg_.installDir).isDir()) {
            fail(QStringLiteral("Install directory is missing or invalid."));
            return;
        }

        step(QStringLiteral("Waiting for Venice to close..."), 5);
        QString err;
        if (!waitForLauncherExit(&err)) {
            fail(QStringLiteral("Install ownership barrier failed: %1").arg(err));
            return;
        }

        step(QStringLiteral("Fetching update manifest..."), 10);
        const QByteArray manifestBytes = fetchManifest(&err);
        if (manifestBytes.isEmpty()) {
            fail(QStringLiteral("Could not retrieve update manifest: %1").arg(err));
            return;
        }

        const UpdateManifest manifest = parseUpdateManifest(manifestBytes);
        if (!manifest.ok) {
            fail(QStringLiteral("Invalid update manifest: %1").arg(manifest.message));
            return;
        }

        step(QStringLiteral("Verifying manifest signature..."), 20);
        if (manifest.signatureAlg.compare(QStringLiteral("ed25519"), Qt::CaseInsensitive) != 0) {
            fail(QStringLiteral("Unsupported signature_alg '%1'; only ed25519 is accepted. Refusing.")
                     .arg(manifest.signatureAlg.isEmpty() ? QStringLiteral("(none)") : manifest.signatureAlg));
            return;
        }
        if (manifest.publicKeyId.isEmpty()) {
            fail(QStringLiteral("Manifest is missing public_key_id. Refusing."));
            return;
        }
        const updater::TrustRootDecision trust = resolvePublicKey(manifest.publicKeyId, cfg_);
        for (const QString& ignored : trust.ignored) {
            log(QStringLiteral("Ignored external public-key source (production trusts only the embedded key): %1")
                    .arg(ignored));
        }
        const QByteArray publicKey = trust.publicKey;
        if (publicKey.size() != 32) {
            fail(QStringLiteral("No trusted public key for public_key_id '%1'. Refusing.").arg(manifest.publicKeyId));
            return;
        }
        log(QStringLiteral("Trust root: %1").arg(trust.source));
        if (!verifyManifestSignature(manifest, publicKey)) {
            fail(QStringLiteral("Manifest signature verification FAILED. Refusing update."));
            return;
        }
        log(QStringLiteral("Manifest signature OK (ed25519, key %1). Offered version: %2")
                .arg(manifest.publicKeyId, manifest.version));

        const UpdateDecision decision = evaluateUpdate(cfg_.currentVersion, manifest);
        if (decision == UpdateDecision::DowngradeBlocked) {
            fail(QStringLiteral("Manifest version %1 is older than installed %2 and rollback is not allowed.")
                     .arg(manifest.version, cfg_.currentVersion));
            return;
        }
        if (decision == UpdateDecision::UpToDate) {
            log(QStringLiteral("Already up to date; relaunching."));
            succeedNoChange();
            return;
        }

        if (!manifest.url.startsWith(QStringLiteral("https://"), Qt::CaseInsensitive)) {
            fail(QStringLiteral("Artifact URL is not HTTPS. Refusing to download."));
            return;
        }

        step(QStringLiteral("Downloading update..."), 35);
        QByteArray artifact;
        if (!cfg_.artifactFile.isEmpty()) {
            if (!updater::localManifestAllowed(kProductionBuild)) {
                fail(QStringLiteral("Local artifact files are forbidden in production."));
                return;
            }
            QFile local(cfg_.artifactFile);
            if (!local.open(QIODevice::ReadOnly) || local.size() > 1024LL * 1024LL * 1024LL) {
                fail(QStringLiteral("Local development artifact is unreadable or too large."));
                return;
            }
            artifact = local.readAll();
        } else {
            artifact = downloadBytes(QUrl(manifest.url), kArtifactTimeoutMs, &err, /*track*/ true);
        }
        if (artifact.isEmpty()) {
            fail(QStringLiteral("Artifact download failed: %1").arg(err));
            return;
        }

        step(QStringLiteral("Verifying download integrity..."), 60);
        if (!verifyArtifactSha256(artifact, manifest.sha256)) {
            fail(QStringLiteral("Artifact SHA-256 did not match the signed manifest. Refusing update."));
            return;
        }
        log(QStringLiteral("Artifact hash OK (%1 bytes).").arg(artifact.size()));

        // Persist artifact to a temp file for the zip reader.
        QTemporaryDir work;
        if (!work.isValid()) {
            fail(QStringLiteral("Could not create a temporary working directory."));
            return;
        }
        const QString artifactPath = work.filePath(QStringLiteral("orion-update.zip"));
        {
            QFile f(artifactPath);
            if (!f.open(QIODevice::WriteOnly) || f.write(artifact) != artifact.size()) {
                fail(QStringLiteral("Could not stage the downloaded artifact."));
                return;
            }
        }

        step(QStringLiteral("Extracting update..."), 70);
        const QString stageDir = work.filePath(QStringLiteral("stage"));
        const updater::ExtractResult extracted = updater::extractZipSafely(artifactPath, stageDir);
        if (!extracted.ok) {
            QString detail = extracted.error;
            if (!extracted.rejectedEntry.isEmpty()) {
                detail += QStringLiteral(" (entry: %1)").arg(extracted.rejectedEntry);
            }
            fail(QStringLiteral("Extraction rejected: %1").arg(detail));
            return;
        }
        log(QStringLiteral("Extracted %1 files.").arg(extracted.files.size()));

        // Verify both complete, signed runtime inventories before touching the
        // install. This also binds the old-minus-new retirement list to two
        // authenticated manifests rather than untrusted ZIP filenames.
        const QJsonObject oldFiles = verifiedReleaseFiles(cfg_.installDir, &err);
        if (oldFiles.isEmpty()) {
            fail(QStringLiteral("Installed runtime integrity failed: %1").arg(err));
            return;
        }
        const QJsonObject newFiles = verifiedReleaseFiles(stageDir, &err);
        if (newFiles.isEmpty()) {
            fail(QStringLiteral("Staged runtime integrity failed: %1").arg(err));
            return;
        }

        if (cfg_.dryRun) {
            log(QStringLiteral("Dry run: skipping backup/replace/relaunch."));
            succeedNoChange();
            return;
        }

#ifdef Q_OS_WIN
        if (!installOwnersGone(cfg_.installDir, &err)) {
            fail(QStringLiteral("Install ownership barrier failed before backup: %1").arg(err));
            return;
        }
#endif

        step(QStringLiteral("Backing up current install..."), 80);
        const QString backupDir = QDir::cleanPath(cfg_.installDir + QStringLiteral("/../.orion_update_backup"));
        if (!updater::backupTree(cfg_.installDir, backupDir, &err)) {
            fail(QStringLiteral("Backup failed, aborting before any change: %1").arg(err));
            return;
        }

        step(QStringLiteral("Installing update..."), 90);
        QStringList applied;
        if (!updater::applyTree(stageDir, cfg_.installDir, &applied, &err)) {
            log(QStringLiteral("Apply failed (%1). Rolling back...").arg(err));
            QString restoreErr;
            if (updater::restoreTree(backupDir, cfg_.installDir, &restoreErr)) {
                fail(QStringLiteral("Update failed and was rolled back to the previous version."));
            } else {
                fail(QStringLiteral("Update failed AND rollback failed (%1). Reinstall may be required.")
                         .arg(restoreErr));
            }
            return;
        }

        if (!updater::pruneRetiredRuntimeFiles(cfg_.installDir, oldFiles, newFiles, &err)) {
            log(QStringLiteral("Retired-runtime prune failed (%1). Rolling back...").arg(err));
            QString restoreErr;
            if (updater::restoreTree(backupDir, cfg_.installDir, &restoreErr)) {
                fail(QStringLiteral("Update failed and was rolled back to the previous version."));
            } else {
                fail(QStringLiteral("Update failed AND rollback failed (%1). Reinstall may be required.").arg(restoreErr));
            }
            return;
        }

        if (cfg_.injectFailureAfterPrune) {
            QString restoreErr;
            if (updater::restoreTree(backupDir, cfg_.installDir, &restoreErr)) {
                fail(QStringLiteral("Injected development fault; exact rollback restored the previous version."));
            } else {
                fail(QStringLiteral("Injected development fault AND rollback failed (%1).").arg(restoreErr));
            }
            return;
        }

        QString installedIntegrity;
        SecurityManager installedVerifier(cfg_.installDir);
        if (!installedVerifier.verifyReleaseIntegrity(&installedIntegrity, /*exactRoot*/ true)) {
            log(QStringLiteral("Post-update integrity failed (%1). Rolling back...").arg(installedIntegrity));
            QString restoreErr;
            if (updater::restoreTree(backupDir, cfg_.installDir, &restoreErr)) {
                fail(QStringLiteral("Update failed integrity validation and was rolled back."));
            } else {
                fail(QStringLiteral("Update integrity failed AND rollback failed (%1). Reinstall may be required.").arg(restoreErr));
            }
            return;
        }

        // Sanity check: the relaunch target must exist post-apply, else roll back.
        QString relaunchWhy;
        const QString relaunchPath = updater::safeRelaunchPath(cfg_.installDir, cfg_.relaunchExe, &relaunchWhy);
        if (relaunchPath.isEmpty()) {
            log(QStringLiteral("Updated install has no launchable %1 (%2). Rolling back...")
                    .arg(cfg_.relaunchExe, relaunchWhy));
            QString restoreErr;
            // [Codex r4] The rollback's own result is the truth here: a restore that failed
            // is reported as a failed update, never as a successful rollback.
            if (updater::restoreTree(backupDir, cfg_.installDir, &restoreErr)) {
                fail(QStringLiteral("Update incomplete; rolled back to previous version."));
            } else {
                fail(QStringLiteral("Update incomplete AND rollback failed (%1). Reinstall may be required.")
                         .arg(restoreErr));
            }
            return;
        }

        log(QStringLiteral("Applied %1 files.").arg(applied.size()));
        {
            QString cleanupErr;
            if (!updater::removeTreeSafely(backupDir, &cleanupErr)) {
                log(QStringLiteral("Backup cleanup left something behind (%1); the install is complete regardless.")
                        .arg(cleanupErr));
            }
        }

        if (cfg_.relaunch) {
            step(QStringLiteral("Relaunching Venice..."), 100);
            QProcess::startDetached(relaunchPath, {}, cfg_.installDir);
        }
        succeed(QStringLiteral("Update to %1 complete.").arg(manifest.version));
    }

signals:
    void finished(int exitCode);

private:
    void step(const QString& status, int pct)
    {
        if (window_) {
            window_->setStatus(status);
            window_->setProgress(pct);
        }
        log(status);
        QApplication::processEvents();
    }

    void log(const QString& line)
    {
        const QString stamped = QDateTime::currentDateTimeUtc().toString(Qt::ISODate) + QStringLiteral(" ") + line;
        if (window_) {
            window_->appendLog(line);
        }
        QFile f(logPath_);
        if (f.open(QIODevice::Append | QIODevice::Text)) {
            QTextStream(&f) << stamped << '\n';
        }
    }

    void succeed(const QString& message)
    {
        if (window_) {
            window_->setStatus(message);
            window_->setProgress(100);
            window_->finish(true);
        }
        log(message);
        scheduleStageCleanup();
        emit finished(0);
    }

    void succeedNoChange()
    {
        QString relaunchWhy;
        const QString relaunchPath = updater::safeRelaunchPath(cfg_.installDir, cfg_.relaunchExe, &relaunchWhy);
        if (cfg_.relaunch && !cfg_.dryRun) {
            if (!relaunchPath.isEmpty()) {
                QProcess::startDetached(relaunchPath, {}, cfg_.installDir);
            } else {
                log(QStringLiteral("Not relaunching: %1").arg(relaunchWhy));
            }
        }
        succeed(QStringLiteral("No update applied."));
    }

    void fail(const QString& message)
    {
        if (window_) {
            window_->setStatus(QStringLiteral("Update failed"));
            window_->appendLog(message);
            window_->finish(false);
        }
        log(QStringLiteral("ERROR: ") + message);
        scheduleStageCleanup();
        emit finished(1);
    }

    void scheduleStageCleanup()
    {
        if (!cfg_.stagedHelper) return;
        const QString installedUpdater = QDir(cfg_.installDir).absoluteFilePath(QStringLiteral("OrionUpdater.exe"));
        if (!QFileInfo(installedUpdater).isFile()) return;
        const QStringList args = {
            QStringLiteral("--cleanup-stage"), QCoreApplication::applicationDirPath(),
            QStringLiteral("--wait-pid"), QString::number(QCoreApplication::applicationPid())};
        if (!QProcess::startDetached(installedUpdater, args, cfg_.installDir)) {
            log(QStringLiteral("Could not schedule staged-helper cleanup; remove the verified sibling stage later."));
        }
    }

    bool waitForLauncherExit(QString* error)
    {
#ifdef Q_OS_WIN
        if (!waitForPidExit(cfg_.launcherPid, error)
            || !waitForPidExit(cfg_.bootstrapPid, error)) return false;
        return installOwnersGone(cfg_.installDir, error);
#else
        Q_UNUSED(cfg_);
        Q_UNUSED(error);
        return true;
#endif
    }

    QByteArray fetchManifest(QString* err)
    {
        if (!cfg_.manifestFile.isEmpty()) {
            if (!updater::localManifestAllowed(kProductionBuild)) {
                if (err) *err = QStringLiteral("a local manifest file is not permitted in production builds");
                return {};
            }
            QFile f(cfg_.manifestFile);
            if (!f.open(QIODevice::ReadOnly)) {
                if (err) *err = QStringLiteral("cannot read manifest file");
                return {};
            }
            return f.readAll();
        }
        if (!cfg_.manifestUrl.startsWith(QStringLiteral("https://"), Qt::CaseInsensitive)) {
            if (err) *err = QStringLiteral("manifest URL is not HTTPS");
            return {};
        }
        return downloadBytes(QUrl(cfg_.manifestUrl), kManifestTimeoutMs, err, /*track*/ false);
    }

    QByteArray downloadBytes(const QUrl& url, int timeoutMs, QString* err, bool track)
    {
        if (url.scheme().compare(QStringLiteral("https"), Qt::CaseInsensitive) != 0) {
            if (err) *err = QStringLiteral("refusing non-HTTPS URL");
            return {};
        }
        QNetworkRequest request(url);
        request.setRawHeader("Accept", "application/octet-stream, application/json");
        // Match the launcher's UA so the manifest fetch passes Cloudflare's bot filter.
        request.setRawHeader("User-Agent",
                             QByteArrayLiteral("OrionLauncher/") + QByteArrayLiteral(ORION_NATIVE_VERSION)
                                 + QByteArrayLiteral(" (Windows NT 10.0; Win64; x64)"));
        request.setTransferTimeout(timeoutMs);
        auto ssl = QSslConfiguration::defaultConfiguration();
        ssl.setPeerVerifyMode(QSslSocket::VerifyPeer);
        request.setSslConfiguration(ssl);

        QNetworkReply* reply = nam_.get(request);
        QEventLoop loop;
        connect(reply, &QNetworkReply::finished, &loop, &QEventLoop::quit);
        connect(reply, &QNetworkReply::sslErrors, reply, [reply](const QList<QSslError>&) { reply->abort(); });
        if (track) {
            connect(reply, &QNetworkReply::downloadProgress, this, [this](qint64 got, qint64 total) {
                if (total > 0 && window_) {
                    window_->setProgress(35 + static_cast<int>(25.0 * static_cast<double>(got) / static_cast<double>(total)));
                    QApplication::processEvents();
                }
            });
        }
        loop.exec();

        const QByteArray data = reply->readAll();
        const QNetworkReply::NetworkError netErr = reply->error();
        const QString netErrString = reply->errorString();
        reply->deleteLater();
        if (netErr != QNetworkReply::NoError) {
            if (err) *err = netErrString;
            return {};
        }
        return data;
    }

    UpdaterConfig cfg_;
    UpdaterWindow* window_ = nullptr;
    QNetworkAccessManager nam_;
    QString logPath_;
};

int main(int argc, char* argv[])
{
    QApplication app(argc, argv);
    QApplication::setApplicationName(QStringLiteral("Orion Updater"));
    QApplication::setOrganizationName(QStringLiteral("NexusVision"));

    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral("Orion self-update helper."));
    parser.addHelpOption();
    const QCommandLineOption manifestUrlOpt(QStringLiteral("manifest-url"), QStringLiteral("HTTPS manifest URL."), QStringLiteral("url"));
    const QCommandLineOption manifestFileOpt(QStringLiteral("manifest-file"), QStringLiteral("Local manifest file (testing)."), QStringLiteral("path"));
    const QCommandLineOption artifactFileOpt(QStringLiteral("artifact-file"),
        QStringLiteral("Development fixture only: local artifact file."), QStringLiteral("path"));
    const QCommandLineOption installDirOpt(QStringLiteral("install-dir"), QStringLiteral("Orion install directory."), QStringLiteral("path"));
    const QCommandLineOption pidOpt(QStringLiteral("launcher-pid"), QStringLiteral("PID of OrionNative to wait for."), QStringLiteral("pid"));
    const QCommandLineOption stagedHelperOpt(QStringLiteral("staged-helper"),
        QStringLiteral("Internal: executing the verified updater closure outside the install tree."));
    const QCommandLineOption bootstrapPidOpt(QStringLiteral("bootstrap-pid"),
        QStringLiteral("Internal: first-stage updater PID to wait for."), QStringLiteral("pid"));
    const QCommandLineOption stageRuntimeOpt(QStringLiteral("stage-runtime"),
        QStringLiteral("Development fixture: exercise the production staged-helper flow."));
    const QCommandLineOption exitOnCompleteOpt(QStringLiteral("exit-on-complete"),
        QStringLiteral("Development fixture: close the updater when complete."));
    const QCommandLineOption injectFailureOpt(QStringLiteral("inject-failure-after-prune"),
        QStringLiteral("Development fixture: force rollback after applying and pruning."));
    const QCommandLineOption cleanupStageOpt(QStringLiteral("cleanup-stage"),
        QStringLiteral("Internal: remove a completed sibling updater stage."), QStringLiteral("path"));
    const QCommandLineOption waitPidOpt(QStringLiteral("wait-pid"),
        QStringLiteral("Internal: wait for the staged updater to exit before cleanup."), QStringLiteral("pid"));
    const QCommandLineOption relaunchOpt(QStringLiteral("relaunch"), QStringLiteral("Executable to relaunch."), QStringLiteral("exe"), QStringLiteral("OrionNative.exe"));
    const QCommandLineOption versionOpt(QStringLiteral("current-version"), QStringLiteral("Installed Orion version."), QStringLiteral("ver"));
    const QCommandLineOption pubkeysOpt(QStringLiteral("pubkeys-file"), QStringLiteral("Path to trusted Ed25519 public keys JSON."), QStringLiteral("path"));
    const QCommandLineOption dryRunOpt(QStringLiteral("dry-run"), QStringLiteral("Verify and extract but do not replace files."));
    const QCommandLineOption noRelaunchOpt(QStringLiteral("no-relaunch"), QStringLiteral("Do not relaunch after updating."));
    const QCommandLineOption buildProfileOpt(QStringLiteral("build-profile"),
        QStringLiteral("Write this binary's build attestation (profile, embedded key ids) as JSON to <path> and exit."),
        QStringLiteral("path"));
    parser.addOptions({manifestUrlOpt, manifestFileOpt, artifactFileOpt, installDirOpt, pidOpt,
                       stagedHelperOpt, bootstrapPidOpt, stageRuntimeOpt, exitOnCompleteOpt, injectFailureOpt,
                       cleanupStageOpt, waitPidOpt,
                       relaunchOpt, versionOpt, pubkeysOpt, dryRunOpt, noRelaunchOpt, buildProfileOpt});
    parser.process(app);

    if (parser.isSet(buildProfileOpt)) {
        return writeBuildProfile(parser.value(buildProfileOpt)) ? 0 : 2;
    }
    if (parser.isSet(cleanupStageOpt)) {
        QString cleanupError;
        const QString install = updater::bindInstallRoot({}, QCoreApplication::applicationDirPath(),
                                                          kProductionBuild, &cleanupError);
        const QString stage = parser.value(cleanupStageOpt);
        if (install.isEmpty()
            || updater::bindStagedInstallRoot(install, stage, kProductionBuild, &cleanupError).isEmpty()) {
            return 2;
        }
        const qint64 ownerPid = parser.value(waitPidOpt).toLongLong();
        if (!parser.isSet(waitPidOpt) || ownerPid <= 0) return 2;
#ifdef Q_OS_WIN
        if (!waitForPidExit(ownerPid, &cleanupError)) return 2;
#endif
        return updater::removeTreeSafely(stage, &cleanupError) ? 0 : 2;
    }
    if (kProductionBuild && (parser.isSet(artifactFileOpt) || parser.isSet(stageRuntimeOpt)
                             || parser.isSet(exitOnCompleteOpt) || parser.isSet(injectFailureOpt))) {
        return 2;
    }

    UpdaterConfig cfg;
    cfg.manifestUrl = parser.value(manifestUrlOpt);
    cfg.manifestFile = parser.value(manifestFileOpt);
    cfg.artifactFile = parser.value(artifactFileOpt);
    {
        // [Codex F2] Bind the install root before anything else can be written.
        QString rootError;
        cfg.installDir = parser.isSet(stagedHelperOpt)
            ? updater::bindStagedInstallRoot(
                  parser.isSet(installDirOpt) ? parser.value(installDirOpt) : QString(),
                  QCoreApplication::applicationDirPath(), kProductionBuild, &rootError)
            : updater::bindInstallRoot(
                  parser.isSet(installDirOpt) ? parser.value(installDirOpt) : QString(),
                  QCoreApplication::applicationDirPath(), kProductionBuild, &rootError);
        if (cfg.installDir.isEmpty()) {
            // Audit trail for a refused root goes to the updater's OWN directory (never
            // the requested one, which is exactly what we refused to write into).
            QFile audit(QCoreApplication::applicationDirPath() + QStringLiteral("/orion_updater.log"));
            if (audit.open(QIODevice::Append | QIODevice::Text)) {
                QTextStream(&audit) << QDateTime::currentDateTimeUtc().toString(Qt::ISODate)
                                    << QStringLiteral(" ERROR: install root refused: ") << rootError << Qt::endl;
            }
            UpdaterWindow window;
            window.show();
            window.setStatus(QStringLiteral("Update failed"));
            window.appendLog(rootError);
            window.finish(false);
            return app.exec(), 1;
        }
    }
    {
        // [Codex r3 F7] Production ignores --relaunch; development accepts a bare name only.
        bool ignoredOverride = false;
        QString relaunchError;
        cfg.relaunchExe = updater::relaunchExecutableName(parser.value(relaunchOpt), kProductionBuild,
                                                          &ignoredOverride, &relaunchError);
        if (cfg.relaunchExe.isEmpty()) {
            UpdaterWindow window;
            window.show();
            window.setStatus(QStringLiteral("Update failed"));
            window.appendLog(relaunchError);
            window.finish(false);
            return app.exec(), 1;
        }
        if (ignoredOverride) {
            QFile audit(QCoreApplication::applicationDirPath() + QStringLiteral("/orion_updater.log"));
            if (audit.open(QIODevice::Append | QIODevice::Text)) {
                QTextStream(&audit) << QDateTime::currentDateTimeUtc().toString(Qt::ISODate)
                                    << QStringLiteral(" WARNING: --relaunch override ignored (production relaunches OrionNative.exe only)")
                                    << Qt::endl;
            }
        }
    }
    cfg.currentVersion = parser.value(versionOpt);
    cfg.pubkeysFile = parser.value(pubkeysOpt);
    cfg.launcherPid = parser.isSet(pidOpt) ? parser.value(pidOpt).toLongLong() : 0;
    cfg.bootstrapPid = parser.isSet(bootstrapPidOpt) ? parser.value(bootstrapPidOpt).toLongLong() : 0;
    cfg.dryRun = parser.isSet(dryRunOpt);
    cfg.relaunch = !parser.isSet(noRelaunchOpt);
    cfg.exitOnComplete = !kProductionBuild && parser.isSet(exitOnCompleteOpt);
    cfg.injectFailureAfterPrune = !kProductionBuild && parser.isSet(injectFailureOpt);
    cfg.stagedHelper = parser.isSet(stagedHelperOpt);

    if (kProductionBuild || parser.isSet(stageRuntimeOpt) || parser.isSet(stagedHelperOpt)) {
        QString stageError;
        const QJsonObject oldFiles = verifiedReleaseFiles(cfg.installDir, &stageError);
        bool stageOk = !oldFiles.isEmpty();
        if (stageOk && parser.isSet(stagedHelperOpt)) {
            stageOk = updater::verifyStagedUpdaterRuntime(
                cfg.installDir, QCoreApplication::applicationDirPath(), oldFiles, &stageError);
        }
        if (!stageOk) {
            UpdaterWindow window;
            window.show();
            window.setStatus(QStringLiteral("Update failed"));
            window.appendLog(QStringLiteral("Updater runtime attestation failed: %1").arg(stageError));
            window.finish(false);
            app.exec();
            return 1;
        }
        if (!parser.isSet(stagedHelperOpt)) {
            // The installed updater is only a bootstrap. It must exit before
            // any destination file changes, including its own EXE and DLLs.
            const QString parent = QFileInfo(cfg.installDir).dir().absolutePath();
            QTemporaryDir privateStage(parent + QStringLiteral("/.orion_updater_stage-XXXXXX"));
            if (!privateStage.isValid()
                || !updater::stageUpdaterRuntime(cfg.installDir, privateStage.path(), oldFiles, &stageError)) {
                UpdaterWindow window;
                window.show();
                window.setStatus(QStringLiteral("Update failed"));
                window.appendLog(QStringLiteral("Could not stage updater outside install: %1").arg(stageError));
                window.finish(false);
                app.exec();
                return 1;
            }
            const QString stagedExe = privateStage.filePath(QStringLiteral("OrionUpdater.exe"));
            QStringList childArgs = app.arguments().mid(1);
            childArgs << QStringLiteral("--staged-helper") << QStringLiteral("--bootstrap-pid")
                      << QString::number(QCoreApplication::applicationPid());
            privateStage.setAutoRemove(false);
            if (!QProcess::startDetached(stagedExe, childArgs, privateStage.path())) {
                QString cleanup;
                updater::removeTreeSafely(privateStage.path(), &cleanup);
                UpdaterWindow window;
                window.show();
                window.setStatus(QStringLiteral("Update failed"));
                window.appendLog(QStringLiteral("Could not start staged updater."));
                window.finish(false);
                app.exec();
                return 1;
            }
            return 0;
        }
    }

    UpdaterWindow window;
    window.show();

    UpdateRunner runner(cfg, &window);
    int exitCode = 0;
    QObject::connect(&runner, &UpdateRunner::finished, &app, [&exitCode, &app, exitOnComplete = cfg.exitOnComplete](int code) {
        exitCode = code;
        if (exitOnComplete) app.quit();
    });
    QTimer::singleShot(0, &runner, &UpdateRunner::run);

    app.exec();
    return exitCode;
}

#include "updater_main.moc"
