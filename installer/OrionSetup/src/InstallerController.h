// InstallerController.h — backend for the Orion downloader/installer (OrionSetup.exe).
//
// A single QObject exposed to QML. It reads components.json, downloads each
// component from the release host with real progress, verifies its SHA-256,
// installs it (extract archive / run driver installer / run a setup step),
// creates shortcuts, and registers an uninstaller entry. All UI state is
// pushed to QML through the signals declared below.
#pragma once

#include <QObject>
#include <QVector>
#include <QVariantList>
#include <QElapsedTimer>
#include <QString>

QT_BEGIN_NAMESPACE
class QNetworkAccessManager;
class QNetworkReply;
class QFile;
QT_END_NAMESPACE

// One installable component parsed from components.json.
struct Component {
    QString name;        // human label shown in the checklist
    QString url;         // release-host download URL (placeholder in the template)
    QString sha256;      // expected lowercase hex digest (placeholder in the template)
    qint64  sizeBytes = 0;
    QString kind;        // "archive" | "driver" | "step"
    QString args;        // extra args for "driver" installers (default: /quiet /norestart)
    QString sizeText;    // pretty size for the checklist (e.g. "172 MB" or "—")
    QString target;      // for "driver": the .exe filename after download
};

class InstallerController : public QObject {
    Q_OBJECT
    // Populated from the manifest so the QML checklist and "/ NNN MB" total are
    // data-driven rather than hardcoded.
    Q_PROPERTY(QVariantList components READ components NOTIFY componentsChanged)
    Q_PROPERTY(double totalMb READ totalMb NOTIFY componentsChanged)
    Q_PROPERTY(QString installDir READ installDir CONSTANT)
    Q_PROPERTY(QString version READ version CONSTANT)
    // Truthful signed-state of THIS running OrionSetup.exe, checked once at
    // startup via WinVerifyTrust on the self path. The footer binds to these so
    // it never claims "signed" when the binary is actually unsigned (dev).
    Q_PROPERTY(bool selfSigned READ selfSigned CONSTANT)
    Q_PROPERTY(QString signerName READ signerName CONSTANT)
    // True when this build refuses to install files that carry no real SHA-256
    // (ORION_REQUIRE_VERIFIED define, or "production": true in components.json).
    Q_PROPERTY(bool requireVerified READ requireVerified CONSTANT)

public:
    explicit InstallerController(QObject *parent = nullptr);
    ~InstallerController() override;

    QVariantList components() const;
    double totalMb() const;
    QString installDir() const { return m_installDir; }
    QString version() const { return m_version; }
    bool selfSigned() const { return m_selfSigned; }
    QString signerName() const { return m_signerName; }
    bool requireVerified() const { return m_requireVerified; }

    // Loads components.json from next to the exe (or the bundled resource
    // fallback). Returns false and leaves an empty list if nothing parses.
    bool loadManifest();

public slots:
    void startInstall();          // begin from the first component
    void retry();                 // resume from the component that failed
    void cancel();                // abort an in-flight install
    void launchOrion();           // FINISH -> run the installed OrionNative.exe
    void openSetupGuide();        // FINISH -> open the setup guide URL
    void openDiscord();           // FINISH -> open the Discord invite

signals:
    // pct 0..100, mbNow/mbTotal in MB, bytesPerSec instantaneous, etaSec seconds left.
    void progress(double pct, double mbNow, double mbTotal, double bytesPerSec, int etaSec);
    void componentState(int index, const QString &state); // "pending"|"active"|"done"|"fail"
    void componentVerified(int index);     // SHA-256 (and, for drivers, signature) passed
    void phase(const QString &text);       // eyebrow: "Downloading" / "Installing"
    void statusLine(const QString &text);  // bold status line
    void subLine(const QString &text);     // muted sub-status
    void finished();                       // all components installed
    void failed(const QString &message);   // hard error -> ERROR screen
    void componentsChanged();

private slots:
    void onDownloadProgress(qint64 received, qint64 total);
    void onReadyRead();
    void onReplyFinished();

private:
    void processNext();
    void startDownload(int index);
    bool verifyDigest(int index, const QString &path, QString *err);
    // WinVerifyTrust (WINTRUST_ACTION_GENERIC_VERIFY_V2) on a file. Returns true
    // only when the file carries a valid, trusted Authenticode signature; fills
    // *signer with the signing subject when it can be read cheaply.
    bool verifyAuthenticode(const QString &path, QString *signer) const;
    bool installArchive(int index, const QString &path, QString *err);
    bool installDriver(int index, const QString &path, QString *err);
    bool runStep(int index, QString *err);                 // shortcuts + uninstaller
    bool createShortcut(const QString &lnkPath, const QString &target,
                        const QString &desc, QString *err);
    void registerUninstaller();
    void fail(int index, const QString &message);
    QString partPath(int index) const;   // staging path for a component download
    void emitProgressForBytes(qint64 currentReplyBytes);

    QNetworkAccessManager *m_nam = nullptr;
    QNetworkReply         *m_reply = nullptr;
    QFile                 *m_file = nullptr;

    QVector<Component> m_components;
    int      m_index = 0;            // component currently processing
    qint64   m_totalBytes = 0;       // sum of all downloadable component sizes
    qint64   m_bytesBefore = 0;      // cumulative bytes of components already done
    qint64   m_resumeOffset = 0;     // bytes already on disk for the active .part
    bool     m_running = false;

    QElapsedTimer m_speedTimer;
    qint64        m_lastBytes = 0;
    double        m_lastSpeed = 0.0;

    QString m_installDir;
    QString m_stagingDir;
    QString m_version = QStringLiteral("1.0.0");

    bool    m_selfSigned = false;      // this exe's own Authenticode state
    QString m_signerName;              // its signer subject, when signed
    bool    m_requireVerified = false; // refuse placeholder hashes when true
};
