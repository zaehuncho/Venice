#include "InstallerController.h"

#include <QCoreApplication>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QCryptographicHash>
#include <QJsonDocument>
#include <QJsonArray>
#include <QJsonObject>
#include <QFile>
#include <QFileInfo>
#include <QDir>
#include <QProcess>
#include <QSettings>
#include <QStandardPaths>
#include <QDesktopServices>
#include <QUrl>
#include <QTimer>
#include <QtGlobal>

#ifdef Q_OS_WIN
#  include <windows.h>
#  include <softpub.h>
#  include <wintrust.h>
#  include <wincrypt.h>
#endif

namespace {
// A sha256 of 64 zeros (or an empty string) is treated as an unverified
// placeholder in the shipped template — the operator swaps in real digests.
bool isPlaceholderDigest(const QString &d) {
    if (d.isEmpty()) return true;
    if (d.length() != 64) return true;                 // not a real sha256
    for (const QChar c : d) if (c != QLatin1Char('0')) return false;
    return true;                                        // all zeros
}

#ifdef Q_OS_WIN
// Read the Authenticode signer's simple display name (e.g. "Nefarius Software
// Solutions e.U.") from a signed PE. Returns empty if unreadable; best-effort.
QString authenticodeSigner(const QString &path)
{
    const std::wstring wpath = path.toStdWString();
    HCERTSTORE  hStore = nullptr;
    HCRYPTMSG   hMsg   = nullptr;
    DWORD       encoding = 0, contentType = 0, formatType = 0;
    QString name;

    if (!CryptQueryObject(CERT_QUERY_OBJECT_FILE, wpath.c_str(),
                          CERT_QUERY_CONTENT_FLAG_PKCS7_SIGNED_EMBED,
                          CERT_QUERY_FORMAT_FLAG_BINARY, 0,
                          &encoding, &contentType, &formatType,
                          &hStore, &hMsg, nullptr) || !hMsg || !hStore) {
        if (hStore) CertCloseStore(hStore, 0);
        if (hMsg)   CryptMsgClose(hMsg);
        return name;
    }

    DWORD signerInfoSize = 0;
    if (CryptMsgGetParam(hMsg, CMSG_SIGNER_INFO_PARAM, 0, nullptr, &signerInfoSize)
        && signerInfoSize > 0) {
        QByteArray buf(int(signerInfoSize), Qt::Uninitialized);
        auto *info = reinterpret_cast<CMSG_SIGNER_INFO *>(buf.data());
        if (CryptMsgGetParam(hMsg, CMSG_SIGNER_INFO_PARAM, 0, info, &signerInfoSize)) {
            CERT_INFO ci{};
            ci.Issuer       = info->Issuer;
            ci.SerialNumber = info->SerialNumber;
            if (PCCERT_CONTEXT cert = CertFindCertificateInStore(
                    hStore, encoding, 0, CERT_FIND_SUBJECT_CERT, &ci, nullptr)) {
                const DWORD n = CertGetNameStringW(
                    cert, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0, nullptr, nullptr, 0);
                if (n > 1) {
                    std::wstring out(n, L'\0');
                    CertGetNameStringW(cert, CERT_NAME_SIMPLE_DISPLAY_TYPE, 0,
                                       nullptr, out.data(), n);
                    name = QString::fromWCharArray(out.c_str()).trimmed();
                }
                CertFreeCertificateContext(cert);
            }
        }
    }
    CertCloseStore(hStore, 0);
    CryptMsgClose(hMsg);
    return name;
}
#endif // Q_OS_WIN
QString prettySize(qint64 bytes) {
    if (bytes <= 0) return QStringLiteral("—");
    const double mb = double(bytes) / (1000.0 * 1000.0);
    if (mb >= 1000.0)
        return QString::number(mb / 1000.0, 'f', 1) + QStringLiteral(" GB");
    return QString::number(qRound(mb)) + QStringLiteral(" MB");
}
} // namespace

InstallerController::InstallerController(QObject *parent)
    : QObject(parent)
    , m_nam(new QNetworkAccessManager(this))
{
    const QString pf = qEnvironmentVariable("ProgramFiles",
                                            QStringLiteral("C:/Program Files"));
    m_installDir = QDir::cleanPath(pf + QStringLiteral("/Orion"));

    const QString temp = QStandardPaths::writableLocation(QStandardPaths::TempLocation);
    m_stagingDir = QDir::cleanPath(temp + QStringLiteral("/OrionSetup"));
    QDir().mkpath(m_stagingDir);

    // A production build (compile-time or manifest flag) refuses to install any
    // component that lacks a real, published SHA-256. Off by default so the
    // template still runs for developers.
#ifdef ORION_REQUIRE_VERIFIED
    m_requireVerified = true;
#endif

    loadManifest();

    // Truthful signed-state: check our OWN Authenticode signature once so the
    // footer can show "Signed ✓ <signer>" only when it is genuinely signed.
    m_selfSigned = verifyAuthenticode(QCoreApplication::applicationFilePath(),
                                      &m_signerName);
    if (m_selfSigned)
        qInfo("OrionSetup: self Authenticode OK — signer '%s'.",
              qPrintable(m_signerName.isEmpty() ? QStringLiteral("signed")
                                                : m_signerName));
    else
        qWarning("OrionSetup: this binary is NOT Authenticode-signed (dev build).");
}

InstallerController::~InstallerController() = default;

// ── manifest ────────────────────────────────────────────────────────────────
bool InstallerController::loadManifest()
{
    // Prefer components.json shipped next to the exe; fall back to the bundled
    // resource so the installer still runs if the file was not deployed.
    QString path = QCoreApplication::applicationDirPath() + QStringLiteral("/components.json");
    if (!QFile::exists(path))
        path = QStringLiteral(":/components.json");

    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) {
        qWarning("OrionSetup: cannot open manifest %s", qPrintable(path));
        return false;
    }
    QJsonParseError perr{};
    const QJsonDocument doc = QJsonDocument::fromJson(f.readAll(), &perr);
    if (perr.error != QJsonParseError::NoError) {
        qWarning("OrionSetup: manifest parse error: %s", qPrintable(perr.errorString()));
        return false;
    }

    QJsonArray arr;
    if (doc.isArray()) {
        arr = doc.array();
    } else if (doc.isObject()) {
        const QJsonObject root = doc.object();
        m_version = root.value(QStringLiteral("version")).toString(m_version);
        // A "production": true manifest hardens verification even without the
        // compile-time define: placeholder hashes then abort the install.
        if (root.value(QStringLiteral("production")).toBool(false))
            m_requireVerified = true;
        arr = root.value(QStringLiteral("components")).toArray();
    }

    m_components.clear();
    m_totalBytes = 0;
    for (const QJsonValue v : arr) {
        const QJsonObject o = v.toObject();
        Component c;
        c.name      = o.value(QStringLiteral("name")).toString();
        c.url       = o.value(QStringLiteral("url")).toString();
        c.sha256    = o.value(QStringLiteral("sha256")).toString().toLower();
        c.sizeBytes = qint64(o.value(QStringLiteral("size_bytes")).toDouble());
        c.kind      = o.value(QStringLiteral("kind")).toString(QStringLiteral("archive"));
        c.args      = o.value(QStringLiteral("args")).toString();
        c.target    = o.value(QStringLiteral("target")).toString();
        c.sizeText  = o.contains(QStringLiteral("size_text"))
                          ? o.value(QStringLiteral("size_text")).toString()
                          : prettySize(c.sizeBytes);
        if (c.kind != QStringLiteral("step"))
            m_totalBytes += c.sizeBytes;
        m_components.push_back(c);
    }
    emit componentsChanged();
    return !m_components.isEmpty();
}

QVariantList InstallerController::components() const
{
    QVariantList out;
    for (const Component &c : m_components) {
        QVariantMap m;
        m.insert(QStringLiteral("name"), c.name);
        m.insert(QStringLiteral("sizeText"), c.sizeText);
        m.insert(QStringLiteral("kind"), c.kind);
        out.push_back(m);
    }
    return out;
}

double InstallerController::totalMb() const
{
    return double(m_totalBytes) / (1000.0 * 1000.0);
}

// ── flow control ─────────────────────────────────────────────────────────────
void InstallerController::startInstall()
{
    if (m_running) return;
    m_index = 0;
    m_bytesBefore = 0;
    for (int i = 0; i < m_components.size(); ++i)
        emit componentState(i, QStringLiteral("pending"));
    m_running = true;
    processNext();
}

void InstallerController::retry()
{
    if (m_running) return;
    // Resume from the component that failed; bytes already fetched for earlier
    // components remain counted so the meter picks up where it left off.
    m_bytesBefore = 0;
    for (int i = 0; i < m_index && i < m_components.size(); ++i)
        if (m_components[i].kind != QStringLiteral("step"))
            m_bytesBefore += m_components[i].sizeBytes;
    m_running = true;
    processNext();
}

void InstallerController::cancel()
{
    if (m_reply) {
        m_reply->abort();
        m_reply->deleteLater();
        m_reply = nullptr;
    }
    if (m_file) {
        m_file->close();
        delete m_file;
        m_file = nullptr;
    }
    m_running = false;
}

void InstallerController::processNext()
{
    if (m_index >= m_components.size()) {
        // Everything installed — register the uninstaller and finish.
        registerUninstaller();
        emit progress(100.0, totalMb(), totalMb(), 0.0, 0);
        emit phase(QStringLiteral("Installing"));
        emit statusLine(QStringLiteral("Setting up drivers & shortcuts…"));
        emit subLine(QStringLiteral("Windows may ask permission for the drivers — click Yes."));
        m_running = false;
        emit finished();
        return;
    }

    const Component &c = m_components[m_index];
    emit componentState(m_index, QStringLiteral("active"));

    if (c.kind == QStringLiteral("step")) {
        emit phase(QStringLiteral("Installing"));
        emit statusLine(QStringLiteral("Setting up drivers & shortcuts…"));
        emit subLine(QStringLiteral("Installing ") + c.name);
        QString err;
        if (!runStep(m_index, &err)) { fail(m_index, err); return; }
        emit componentState(m_index, QStringLiteral("done"));
        ++m_index;
        processNext();
        return;
    }

    const bool driver = (c.kind == QStringLiteral("driver"));
    emit phase(driver ? QStringLiteral("Installing") : QStringLiteral("Downloading"));
    emit statusLine(driver ? QStringLiteral("Setting up drivers & shortcuts…")
                           : QStringLiteral("Getting Orion ready…"));
    emit subLine((driver ? QStringLiteral("Installing ")
                         : QStringLiteral("Downloading ")) + c.name);
    startDownload(m_index);
}

// ── download ─────────────────────────────────────────────────────────────────
QString InstallerController::partPath(int index) const
{
    const Component &c = m_components[index];
    QString base = c.target;
    if (base.isEmpty()) {
        base = QUrl(c.url).fileName();
        if (base.isEmpty())
            base = QStringLiteral("component_%1.bin").arg(index);
    }
    return QDir::cleanPath(m_stagingDir + QLatin1Char('/') + base + QStringLiteral(".part"));
}

void InstallerController::startDownload(int index)
{
    const Component &c = m_components[index];

    // HTTPS-only. Refuse plaintext http:// (or any non-TLS scheme) BEFORE a
    // single byte is fetched — an attacker on the wire could otherwise swap the
    // payload. No exception, even for the resume path.
    const QUrl url(c.url);
    if (url.scheme().compare(QLatin1String("https"), Qt::CaseInsensitive) != 0) {
        const QString scheme = url.scheme().isEmpty()
            ? QStringLiteral("(none)") : url.scheme();
        qCritical("OrionSetup: refusing insecure URL for '%s' (scheme '%s').",
                  qPrintable(c.name), qPrintable(scheme));
        fail(index, QStringLiteral(
                 "%1 is served over an insecure connection (%2://). Orion only "
                 "downloads over HTTPS, so this install was stopped for your "
                 "safety.").arg(c.name, scheme));
        return;
    }

    const QString path = partPath(index);

    m_resumeOffset = 0;
    if (QFileInfo::exists(path)) {
        const qint64 have = QFileInfo(path).size();
        if (have > 0 && (c.sizeBytes == 0 || have < c.sizeBytes))
            m_resumeOffset = have;                 // resume a partial transfer
        else if (c.sizeBytes > 0 && have >= c.sizeBytes)
            m_resumeOffset = 0, QFile::remove(path); // stale/oversized — restart
    }

    m_file = new QFile(path);
    const QIODevice::OpenMode mode = m_resumeOffset > 0
        ? (QIODevice::WriteOnly | QIODevice::Append)
        : (QIODevice::WriteOnly | QIODevice::Truncate);
    if (!m_file->open(mode)) {
        delete m_file; m_file = nullptr;
        fail(index, QStringLiteral("Cannot write to %1. Check disk space and permissions.")
                        .arg(QDir::toNativeSeparators(path)));
        return;
    }

    QNetworkRequest req{url};
    // NoLessSafeRedirectPolicy already blocks an https→http downgrade on
    // redirect, keeping the whole transfer on TLS.
    req.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                     QNetworkRequest::NoLessSafeRedirectPolicy);
    req.setHeader(QNetworkRequest::UserAgentHeader,
                  QStringLiteral("OrionSetup/%1").arg(m_version));
    if (m_resumeOffset > 0)
        req.setRawHeader("Range",
                         "bytes=" + QByteArray::number(m_resumeOffset) + "-");

    m_speedTimer.restart();
    m_lastBytes = 0;
    m_lastSpeed = 0.0;

    m_reply = m_nam->get(req);
    connect(m_reply, &QNetworkReply::downloadProgress,
            this, &InstallerController::onDownloadProgress);
    connect(m_reply, &QNetworkReply::readyRead,
            this, &InstallerController::onReadyRead);
    connect(m_reply, &QNetworkReply::finished,
            this, &InstallerController::onReplyFinished);
}

void InstallerController::onReadyRead()
{
    if (m_reply && m_file)
        m_file->write(m_reply->readAll());
}

void InstallerController::emitProgressForBytes(qint64 currentReplyBytes)
{
    const qint64 globalBytes = m_bytesBefore + m_resumeOffset + currentReplyBytes;

    // Instantaneous speed, sampled at most ~4x/sec for a stable readout.
    const qint64 elapsed = m_speedTimer.elapsed();
    if (elapsed >= 250) {
        const qint64 delta = (m_resumeOffset + currentReplyBytes) - m_lastBytes;
        m_lastSpeed = double(delta) / (double(elapsed) / 1000.0);
        m_lastBytes = m_resumeOffset + currentReplyBytes;
        m_speedTimer.restart();
    }

    double pct = m_totalBytes > 0
        ? qBound(0.0, double(globalBytes) / double(m_totalBytes) * 100.0, 100.0)
        : 0.0;

    int eta = -1;
    if (m_lastSpeed > 1.0 && m_totalBytes > 0)
        eta = int(double(m_totalBytes - globalBytes) / m_lastSpeed);

    emit progress(pct,
                  double(globalBytes) / (1000.0 * 1000.0),
                  totalMb(),
                  m_lastSpeed,
                  eta);
}

void InstallerController::onDownloadProgress(qint64 received, qint64 /*total*/)
{
    emitProgressForBytes(received);
}

void InstallerController::onReplyFinished()
{
    if (!m_reply) return;
    QNetworkReply *reply = m_reply;
    m_reply = nullptr;

    if (m_file) {
        m_file->write(reply->readAll());
        m_file->flush();
        m_file->close();
    }

    const int index = m_index;
    const QNetworkReply::NetworkError netErr = reply->error();
    const QString netErrStr = reply->errorString();
    reply->deleteLater();

    if (netErr != QNetworkReply::NoError) {
        delete m_file; m_file = nullptr;
        // The .part is kept so retry() can resume from where it stopped.
        fail(index, QStringLiteral("The connection dropped while getting %1. "
                                   "Your progress is saved — retrying resumes "
                                   "where it stopped.\n(%2)")
                        .arg(m_components[index].name, netErrStr));
        return;
    }

    const QString partFile = m_file ? m_file->fileName() : partPath(index);
    delete m_file; m_file = nullptr;

    // Verify.
    QString err;
    if (!verifyDigest(index, partFile, &err)) { fail(index, err); return; }

    // Install according to kind.
    const Component &c = m_components[index];
    if (c.kind == QStringLiteral("driver")) {
        if (!installDriver(index, partFile, &err)) { fail(index, err); return; }
    } else { // archive
        if (!installArchive(index, partFile, &err)) { fail(index, err); return; }
    }

    QFile::remove(partFile);                       // staging cleanup
    emit componentState(index, QStringLiteral("done"));
    ++m_index;
    processNext();
}

// ── verify ───────────────────────────────────────────────────────────────────
bool InstallerController::verifyDigest(int index, const QString &path, QString *err)
{
    const Component &c = m_components[index];
    if (isPlaceholderDigest(c.sha256)) {
        // The template ships placeholder hashes. In a real (production) build we
        // must NEVER install an unverified file, so abort loudly. Only a
        // dev/template build is allowed to proceed — and even then it warns.
        if (m_requireVerified) {
            qCritical("OrionSetup: placeholder sha256 for '%s' in a verified "
                      "build — refusing to install.", qPrintable(c.name));
            *err = QStringLiteral(
                       "%1 has no published SHA-256 checksum, so its integrity "
                       "cannot be verified. This build refuses to install "
                       "unverified files.").arg(c.name);
            return false;
        }
        qWarning("OrionSetup: DEV-ONLY placeholder sha256 for '%s' — SHA-256 "
                 "verification SKIPPED. Do not ship a release like this.",
                 qPrintable(c.name));
        return true;   // dev escape: not emitting componentVerified — honest.
    }
    QFile f(path);
    if (!f.open(QIODevice::ReadOnly)) {
        *err = QStringLiteral("Cannot read %1 to verify it.").arg(path);
        return false;
    }
    QCryptographicHash hash(QCryptographicHash::Sha256);
    if (!hash.addData(&f)) {
        *err = QStringLiteral("Failed while hashing %1.").arg(c.name);
        return false;
    }
    const QString got = QString::fromLatin1(hash.result().toHex());
    if (got.compare(c.sha256, Qt::CaseInsensitive) != 0) {
        // Mismatch = corrupt or tampered. ABORT — never install an unverified
        // file. (Retry re-fetches a clean copy.)
        qCritical("OrionSetup: SHA-256 MISMATCH for '%s' (expected %s, got %s).",
                  qPrintable(c.name), qPrintable(c.sha256), qPrintable(got));
        *err = QStringLiteral("%1 failed its SHA-256 integrity check — the "
                              "download is corrupt or has been tampered with, so "
                              "it was not installed. Retrying re-fetches it.")
                   .arg(c.name);
        return false;
    }
    // Genuinely verified against the published digest.
    emit componentVerified(index);
    return true;
}

// ── verify: Authenticode ─────────────────────────────────────────────────────
bool InstallerController::verifyAuthenticode(const QString &path, QString *signer) const
{
    if (signer) signer->clear();
#ifdef Q_OS_WIN
    const std::wstring wpath = path.toStdWString();

    WINTRUST_FILE_INFO fileInfo{};
    fileInfo.cbStruct       = sizeof(fileInfo);
    fileInfo.pcwszFilePath  = wpath.c_str();
    fileInfo.hFile          = nullptr;
    fileInfo.pgKnownSubject = nullptr;

    GUID action = WINTRUST_ACTION_GENERIC_VERIFY_V2;

    WINTRUST_DATA data{};
    data.cbStruct            = sizeof(data);
    data.dwUIChoice          = WTD_UI_NONE;
    data.fdwRevocationChecks = WTD_REVOKE_NONE;   // no network dependency
    data.dwUnionChoice       = WTD_CHOICE_FILE;
    data.pFile               = &fileInfo;
    data.dwStateAction       = WTD_STATEACTION_VERIFY;
    data.dwProvFlags         = WTD_SAFER_FLAG | WTD_CACHE_ONLY_URL_RETRIEVAL;

    const LONG status = WinVerifyTrust(
        static_cast<HWND>(INVALID_HANDLE_VALUE), &action, &data);

    // Always close the state handle we opened above.
    data.dwStateAction = WTD_STATEACTION_CLOSE;
    WinVerifyTrust(static_cast<HWND>(INVALID_HANDLE_VALUE), &action, &data);

    const bool ok = (status == ERROR_SUCCESS);
    if (ok && signer)
        *signer = authenticodeSigner(path);
    return ok;
#else
    Q_UNUSED(path);
    return false;
#endif
}

// ── install: archive ─────────────────────────────────────────────────────────
bool InstallerController::installArchive(int index, const QString &path, QString *err)
{
    const Component &c = m_components[index];
    emit subLine(QStringLiteral("Installing ") + c.name);
    QDir().mkpath(m_installDir);

    // Primary: bsdtar (tar.exe) ships in System32 on Windows 10 1803+ and
    // extracts .zip natively. Fallback: PowerShell Expand-Archive.
    {
        QProcess p;
        p.start(QStringLiteral("tar.exe"),
                {QStringLiteral("-xf"), path,
                 QStringLiteral("-C"), m_installDir});
        if (p.waitForStarted(5000) && p.waitForFinished(300000)
            && p.exitStatus() == QProcess::NormalExit && p.exitCode() == 0) {
            return true;
        }
    }
    {
        QProcess p;
        const QString cmd = QStringLiteral(
            "Expand-Archive -LiteralPath '%1' -DestinationPath '%2' -Force")
            .arg(QDir::toNativeSeparators(path), QDir::toNativeSeparators(m_installDir));
        p.start(QStringLiteral("powershell.exe"),
                {QStringLiteral("-NoProfile"),
                 QStringLiteral("-ExecutionPolicy"), QStringLiteral("Bypass"),
                 QStringLiteral("-Command"), cmd});
        if (p.waitForStarted(5000) && p.waitForFinished(300000)
            && p.exitStatus() == QProcess::NormalExit && p.exitCode() == 0) {
            return true;
        }
        *err = QStringLiteral("Couldn't extract %1. %2")
                   .arg(c.name, QString::fromLocal8Bit(p.readAllStandardError()).trimmed());
    }
    return false;
}

// ── install: driver ──────────────────────────────────────────────────────────
bool InstallerController::installDriver(int index, const QString &path, QString *err)
{
    const Component &c = m_components[index];

    // Never QProcess-launch a driver installer we can't prove is genuine. The
    // ViGEmBus/HidHide setup is vendor-signed, so a failed Authenticode check
    // means the file is corrupt or tampered with — refuse to run it.
    QString signer;
    if (!verifyAuthenticode(path, &signer)) {
        qCritical("OrionSetup: Authenticode check FAILED for driver '%s' — "
                  "refusing to run it.", qPrintable(c.name));
        *err = QStringLiteral(
                   "The %1 installer is not validly signed (Authenticode check "
                   "failed). For your safety Orion will not run it — the file "
                   "may be corrupt or tampered with. Retrying re-fetches it.")
                   .arg(c.name);
        return false;
    }
    qInfo("OrionSetup: driver '%s' Authenticode OK — signer '%s'.",
          qPrintable(c.name),
          qPrintable(signer.isEmpty() ? QStringLiteral("signed") : signer));
    emit subLine(signer.isEmpty()
                     ? QStringLiteral("Signature verified · installing ") + c.name
                     : QStringLiteral("Signed by %1 · installing %2")
                           .arg(signer, c.name));

    QStringList args;
    const QString a = c.args.isEmpty()
        ? QStringLiteral("/quiet /norestart") : c.args;
    for (const QString &tok : a.split(QLatin1Char(' '), Qt::SkipEmptyParts))
        args << tok;

    QProcess p;
    p.start(path, args);
    if (!p.waitForStarted(10000)) {
        *err = QStringLiteral("Couldn't launch the %1 installer.").arg(c.name);
        return false;
    }
    if (!p.waitForFinished(600000)) {
        *err = QStringLiteral("The %1 installer did not finish in time.").arg(c.name);
        return false;
    }
    const int code = p.exitCode();
    // 0 = success; 3010 = success, reboot required (common for driver MSIs).
    if (p.exitStatus() != QProcess::NormalExit || (code != 0 && code != 3010)) {
        *err = QStringLiteral("The %1 driver installer reported an error (code %2).")
                   .arg(c.name).arg(code);
        return false;
    }
    return true;
}

// ── install: step (shortcuts + first-run) ────────────────────────────────────
bool InstallerController::runStep(int index, QString *err)
{
    Q_UNUSED(index);
    const QString exe = QDir::cleanPath(m_installDir + QStringLiteral("/OrionNative.exe"));

    // Start-menu shortcut.
    const QString startMenu =
        qEnvironmentVariable("ProgramData", QStringLiteral("C:/ProgramData"))
        + QStringLiteral("/Microsoft/Windows/Start Menu/Programs");
    QDir().mkpath(startMenu);
    QString e1;
    if (!createShortcut(QDir::cleanPath(startMenu + QStringLiteral("/Orion.lnk")),
                        exe, QStringLiteral("Orion — perfect-green shot timing"), &e1)) {
        // A missing shortcut is not fatal to a working install; log and continue.
        qWarning("OrionSetup: start-menu shortcut failed: %s", qPrintable(e1));
    }

    // Desktop shortcut (best-effort).
    const QString desktop = QStandardPaths::writableLocation(QStandardPaths::DesktopLocation);
    if (!desktop.isEmpty()) {
        QString e2;
        createShortcut(QDir::cleanPath(desktop + QStringLiteral("/Orion.lnk")),
                       exe, QStringLiteral("Orion — perfect-green shot timing"), &e2);
    }
    Q_UNUSED(err);
    return true;
}

bool InstallerController::createShortcut(const QString &lnkPath, const QString &target,
                                         const QString &desc, QString *err)
{
    // WScript.Shell CreateShortcut via PowerShell — no COM headers needed and
    // works on every end-user machine.
    const QString workDir = QFileInfo(target).absolutePath();
    const QString script = QStringLiteral(
        "$w=New-Object -ComObject WScript.Shell; "
        "$s=$w.CreateShortcut('%1'); "
        "$s.TargetPath='%2'; $s.WorkingDirectory='%3'; "
        "$s.Description='%4'; $s.IconLocation='%2,0'; $s.Save()")
        .arg(QDir::toNativeSeparators(lnkPath),
             QDir::toNativeSeparators(target),
             QDir::toNativeSeparators(workDir),
             desc);
    QProcess p;
    p.start(QStringLiteral("powershell.exe"),
            {QStringLiteral("-NoProfile"),
             QStringLiteral("-ExecutionPolicy"), QStringLiteral("Bypass"),
             QStringLiteral("-Command"), script});
    if (p.waitForStarted(5000) && p.waitForFinished(30000)
        && p.exitStatus() == QProcess::NormalExit && p.exitCode() == 0)
        return true;
    if (err)
        *err = QString::fromLocal8Bit(p.readAllStandardError()).trimmed();
    return false;
}

// ── uninstaller registry entry ───────────────────────────────────────────────
void InstallerController::registerUninstaller()
{
    const QString exe = QDir::toNativeSeparators(
        QDir::cleanPath(m_installDir + QStringLiteral("/OrionNative.exe")));
    // A dedicated uninstaller ships with Orion; fall back to the app exe icon.
    QSettings reg(QStringLiteral("HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows\\"
                                 "CurrentVersion\\Uninstall\\Orion"),
                  QSettings::NativeFormat);
    // DisplayName/Publisher are what Add/Remove Programs shows the customer, so they
    // carry the product name. The registry KEY above (...\Uninstall\Orion) is the
    // uninstall identity, not a display string — renaming it would orphan the
    // uninstall entry of any existing install, so it stays frozen like AppId.
    reg.setValue(QStringLiteral("DisplayName"), QStringLiteral("Venice"));
    reg.setValue(QStringLiteral("DisplayVersion"), m_version);
    reg.setValue(QStringLiteral("Publisher"), QStringLiteral("Venice"));
    reg.setValue(QStringLiteral("DisplayIcon"), exe);
    reg.setValue(QStringLiteral("InstallLocation"),
                 QDir::toNativeSeparators(m_installDir));
    reg.setValue(QStringLiteral("UninstallString"),
                 QStringLiteral("\"%1\\uninstall.exe\"")
                     .arg(QDir::toNativeSeparators(m_installDir)));
    reg.setValue(QStringLiteral("NoModify"), 1);
    reg.setValue(QStringLiteral("NoRepair"), 1);
    // EstimatedSize is in KB (installed footprint ≈ ~2x the download).
    reg.setValue(QStringLiteral("EstimatedSize"),
                 int((m_totalBytes / 1024) * 2));
    reg.sync();
    if (reg.status() != QSettings::NoError)
        qWarning("OrionSetup: could not write uninstaller registry entry (need admin).");
}

// ── failure + FINISH actions ─────────────────────────────────────────────────
void InstallerController::fail(int index, const QString &message)
{
    if (index >= 0 && index < m_components.size())
        emit componentState(index, QStringLiteral("fail"));
    m_running = false;
    emit failed(message);
}

void InstallerController::launchOrion()
{
    const QString exe = QDir::cleanPath(m_installDir + QStringLiteral("/OrionNative.exe"));
    QProcess::startDetached(exe, {}, m_installDir);
}

void InstallerController::openSetupGuide()
{
    QDesktopServices::openUrl(QUrl(QStringLiteral("https://orion.gg/setup")));
}

void InstallerController::openDiscord()
{
    QDesktopServices::openUrl(QUrl(QStringLiteral("https://discord.gg/orion")));
}
