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

#include <QtCore/QCommandLineOption>
#include <QtCore/QCommandLineParser>
#include <QtCore/QCoreApplication>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QEventLoop>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QProcess>
#include <QtCore/QStringList>
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
#endif

using namespace orion;

namespace {

constexpr int kManifestTimeoutMs = 15'000;
constexpr int kArtifactTimeoutMs = 120'000;
constexpr int kLauncherWaitMs = 30'000;

struct UpdaterConfig {
    QString manifestUrl;
    QString manifestFile;   // local manifest source (testing / offline)
    QString installDir;
    QString relaunchExe = QStringLiteral("OrionNative.exe");
    QString currentVersion;
    QString pubkeysFile;
    qint64 launcherPid = 0;
    bool dryRun = false;
    bool relaunch = true;
};

// Resolve a trusted Ed25519 PUBLIC key for the manifest's public_key_id. Public
// keys are not secret: they come from an optional compile-time embedded default
// and/or a shipped update_pubkeys.json ({"keys": {"<id>": "<hex|base64>"}}).
// No shared secret / HMAC key is ever required (fail closed on an unknown id).
QByteArray resolvePublicKey(const QString& keyId, const UpdaterConfig& cfg)
{
    if (keyId.trimmed().isEmpty()) {
        return {};
    }
#if defined(ORION_UPDATE_ED25519_PUBKEY_ID) && defined(ORION_UPDATE_ED25519_PUBKEY)
    if (keyId == QStringLiteral(ORION_UPDATE_ED25519_PUBKEY_ID)) {
        const QByteArray embedded = decodeEd25519PublicKey(QStringLiteral(ORION_UPDATE_ED25519_PUBKEY));
        if (embedded.size() == 32) {
            return embedded;
        }
    }
#endif
    QStringList candidates;
    if (!cfg.pubkeysFile.isEmpty()) {
        candidates << cfg.pubkeysFile;
    }
    const QByteArray envPath = qgetenv("ORION_UPDATE_PUBKEYS");
    if (!envPath.isEmpty()) {
        candidates << QString::fromLocal8Bit(envPath);
    }
    candidates << cfg.installDir + QStringLiteral("/update_pubkeys.json");
    candidates << QCoreApplication::applicationDirPath() + QStringLiteral("/update_pubkeys.json");
    for (const QString& path : candidates) {
        QFile f(path);
        if (!f.open(QIODevice::ReadOnly)) {
            continue;
        }
        const QJsonObject root = QJsonDocument::fromJson(f.readAll()).object();
        const QJsonObject keys = root.value(QStringLiteral("keys")).toObject();
        const QString encoded = keys.value(keyId).toString();
        if (!encoded.isEmpty()) {
            const QByteArray decoded = decodeEd25519PublicKey(encoded);
            if (decoded.size() == 32) {
                return decoded;
            }
        }
    }
    return {};
}

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
        waitForLauncherExit();

        step(QStringLiteral("Fetching update manifest..."), 10);
        QString err;
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
        const QByteArray publicKey = resolvePublicKey(manifest.publicKeyId, cfg_);
        if (publicKey.size() != 32) {
            fail(QStringLiteral("No trusted public key for public_key_id '%1'. Refusing.").arg(manifest.publicKeyId));
            return;
        }
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
        const QByteArray artifact = downloadBytes(QUrl(manifest.url), kArtifactTimeoutMs, &err, /*track*/ true);
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

        if (cfg_.dryRun) {
            log(QStringLiteral("Dry run: skipping backup/replace/relaunch."));
            succeedNoChange();
            return;
        }

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

        // Sanity check: the relaunch target must exist post-apply, else roll back.
        const QString relaunchPath = cfg_.installDir + QLatin1Char('/') + cfg_.relaunchExe;
        if (!QFileInfo::exists(relaunchPath)) {
            log(QStringLiteral("Updated install is missing %1. Rolling back...").arg(cfg_.relaunchExe));
            QString restoreErr;
            updater::restoreTree(backupDir, cfg_.installDir, &restoreErr);
            fail(QStringLiteral("Update incomplete; rolled back to previous version."));
            return;
        }

        log(QStringLiteral("Applied %1 files.").arg(applied.size()));
        QDir(backupDir).removeRecursively();

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
        emit finished(0);
    }

    void succeedNoChange()
    {
        const QString relaunchPath = cfg_.installDir + QLatin1Char('/') + cfg_.relaunchExe;
        if (cfg_.relaunch && !cfg_.dryRun && QFileInfo::exists(relaunchPath)) {
            QProcess::startDetached(relaunchPath, {}, cfg_.installDir);
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
        emit finished(1);
    }

    void waitForLauncherExit()
    {
#ifdef Q_OS_WIN
        if (cfg_.launcherPid <= 0) {
            return;
        }
        HANDLE handle = OpenProcess(SYNCHRONIZE, FALSE, static_cast<DWORD>(cfg_.launcherPid));
        if (!handle) {
            return; // already gone
        }
        WaitForSingleObject(handle, kLauncherWaitMs);
        CloseHandle(handle);
#else
        Q_UNUSED(cfg_);
#endif
    }

    QByteArray fetchManifest(QString* err)
    {
        if (!cfg_.manifestFile.isEmpty()) {
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
    const QCommandLineOption installDirOpt(QStringLiteral("install-dir"), QStringLiteral("Orion install directory."), QStringLiteral("path"));
    const QCommandLineOption pidOpt(QStringLiteral("launcher-pid"), QStringLiteral("PID of OrionNative to wait for."), QStringLiteral("pid"));
    const QCommandLineOption relaunchOpt(QStringLiteral("relaunch"), QStringLiteral("Executable to relaunch."), QStringLiteral("exe"), QStringLiteral("OrionNative.exe"));
    const QCommandLineOption versionOpt(QStringLiteral("current-version"), QStringLiteral("Installed Orion version."), QStringLiteral("ver"));
    const QCommandLineOption pubkeysOpt(QStringLiteral("pubkeys-file"), QStringLiteral("Path to trusted Ed25519 public keys JSON."), QStringLiteral("path"));
    const QCommandLineOption dryRunOpt(QStringLiteral("dry-run"), QStringLiteral("Verify and extract but do not replace files."));
    const QCommandLineOption noRelaunchOpt(QStringLiteral("no-relaunch"), QStringLiteral("Do not relaunch after updating."));
    parser.addOptions({manifestUrlOpt, manifestFileOpt, installDirOpt, pidOpt, relaunchOpt, versionOpt, pubkeysOpt, dryRunOpt, noRelaunchOpt});
    parser.process(app);

    UpdaterConfig cfg;
    cfg.manifestUrl = parser.value(manifestUrlOpt);
    cfg.manifestFile = parser.value(manifestFileOpt);
    cfg.installDir = parser.isSet(installDirOpt)
        ? QDir(parser.value(installDirOpt)).absolutePath()
        : QCoreApplication::applicationDirPath();
    cfg.relaunchExe = parser.value(relaunchOpt);
    cfg.currentVersion = parser.value(versionOpt);
    cfg.pubkeysFile = parser.value(pubkeysOpt);
    cfg.launcherPid = parser.isSet(pidOpt) ? parser.value(pidOpt).toLongLong() : 0;
    cfg.dryRun = parser.isSet(dryRunOpt);
    cfg.relaunch = !parser.isSet(noRelaunchOpt);

    UpdaterWindow window;
    window.show();

    UpdateRunner runner(cfg, &window);
    int exitCode = 0;
    QObject::connect(&runner, &UpdateRunner::finished, &app, [&exitCode](int code) { exitCode = code; });
    QTimer::singleShot(0, &runner, &UpdateRunner::run);

    app.exec();
    return exitCode;
}

#include "updater_main.moc"
