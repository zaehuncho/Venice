#include "RemotePlaySession.h"
#include "RemotePlayDecodePolicy.h"
#include "ReleaseMarkerProtocol.h"
#include "RemotePlayExecutablePolicy.h"
#include "SidecarLaunchPolicy.h"
#include "SidecarLogRelayPolicy.h"
#include "SidecarWatchdog.h"  // Track W: restart pacing + bounded video-device enumeration

#include <QtCore/QByteArray>
#include <QtCore/QCoreApplication>
#include <QtCore/QCryptographicHash>
#include <QtCore/QDateTime>
#include <QtCore/QDeadlineTimer>
#include <QtCore/QDebug>
#include <QtCore/QDir>
#include <QtCore/QElapsedTimer>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QHash>
#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QProcessEnvironment>
#include <QtCore/QSet>
#include <QtCore/QSettings>
#include <QtCore/QStandardPaths>
#include <QtCore/QStringList>
#include <QtCore/QThread>
#include <QtCore/QUuid>
#include <QtGui/QPainter>
#include <QtNetwork/QHostAddress>
#include <QtNetwork/QNetworkInterface>
#include <QtNetwork/QUdpSocket>

#ifdef Q_OS_WIN
#include <audiopolicy.h>
#include <mmdeviceapi.h>
#include <windows.h>
#endif

#include <algorithm>
#include <cmath>
#include <utility>

namespace orion {

namespace {

#ifdef ORION_PRODUCTION_BUILD
constexpr bool kProductionBuild = true;
#else
constexpr bool kProductionBuild = false;
#endif

remote_play_executable_policy::Selection resolveChiakiExecutable(
    const AppConfigData& config, const QString& rootDir)
{
#ifdef ORION_PRODUCTION_BUILD
    // A production package has one executable authority: the exact
    // install-relative OrionStream beneath OrionNative.exe. Repository paths,
    // PATH lookup and a persisted absolute chiaki_path are intentionally not
    // compiled into this fallback branch.
    return remote_play_executable_policy::select(
        QCoreApplication::applicationDirPath(), rootDir, config.chiakiPath, true);
#else
    const auto selected = remote_play_executable_policy::select(
        QCoreApplication::applicationDirPath(), rootDir, config.chiakiPath, false);
    if (!selected.path.isEmpty()) {
        return selected;
    }

    // Developer-only compatibility lane for stock Chiaki experimentation. The
    // patched packaged/repository OrionStream and the explicit configured path
    // were already considered above; none of these candidates can enter a
    // production binary.
    const QStringList candidates = {
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-ng-orion/chiaki-ng-Win/chiaki.exe")),
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/chiaki-orion/chiaki.exe")),
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/chiaki-ng/chiaki.exe")),
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/chiaki-ng/chiaki-ng-Win/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-orion/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-ng/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki-ng/chiaki-ng-Win/chiaki.exe")),
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/chiaki/chiaki-ng.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/deploy/chiaki/chiaki-ng.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/native_orion/vendor/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(rootDir + QStringLiteral("/vendor/chiaki/chiaki.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Downloads/Chiaki/Chiaki/chiaki.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Downloads/Chiaki/chiaki.exe")),
        QStandardPaths::findExecutable(QStringLiteral("chiaki.exe")),
        QStandardPaths::findExecutable(QStringLiteral("chiaki-ng.exe"))
    };

    for (const auto& candidate : candidates) {
        const QString existing = remote_play_executable_policy::existingRegularFile(candidate);
        if (!existing.isEmpty()) {
            return {existing, remote_play_executable_policy::Source::DeveloperConfigured};
        }
    }
    return {};
#endif
}

QString resolveChiakiPath(const AppConfigData& config, const QString& rootDir)
{
    return resolveChiakiExecutable(config, rootDir).path;
}

using ExecutableIdentity = remote_play_executable_policy::ExecutableIdentity;

ExecutableIdentity readExecutableIdentity(const QString& path)
{
    return remote_play_executable_policy::readExecutableIdentity(path);
}

#ifdef Q_OS_WIN
void runTaskkillBlocking(const QStringList& args, int timeoutMs = 3500)
{
    QProcess proc;
    proc.setProgram(QStringLiteral("taskkill.exe"));
    proc.setArguments(args);
    proc.setProcessChannelMode(QProcess::ForwardedErrorChannel);
    proc.start();
    if (!proc.waitForFinished(timeoutMs)) {
        proc.kill();
        proc.waitForFinished(500);
    }
}

void cleanupRemotePlayClientsBlocking()
{
    static const QStringList names = {
        QStringLiteral("OrionStream.exe"),
        QStringLiteral("chiaki.exe"),
        QStringLiteral("chiaki-ng.exe"),
        QStringLiteral("chiaki4deck.exe")
    };

    // Two passes catch the custom stream client's renderer-fallback child, which
    // can appear immediately after the parent starts shutting down.
    for (int pass = 0; pass < 2; ++pass) {
        for (const QString& name : names) {
            runTaskkillBlocking({QStringLiteral("/IM"), name, QStringLiteral("/F"), QStringLiteral("/T")});
        }
        QThread::msleep(150);
    }
}

void reapOrphanStreamClientsBlocking()
{
    // Single fast pass (no settle sleep): kill any stream client orphaned by a crash or
    // hard-kill BEFORE a new connect, so the orchestrator can't end up with a SECOND
    // OrionStream fighting over the frame/input pipes ("Remote is already in use"). On a
    // clean first connect every target is absent and taskkill returns near-instantly.
    static const QStringList names = {
        QStringLiteral("OrionStream.exe"),
        QStringLiteral("chiaki.exe"),
        QStringLiteral("chiaki-ng.exe"),
        QStringLiteral("chiaki4deck.exe")
    };
    for (const QString& name : names) {
        runTaskkillBlocking({QStringLiteral("/IM"), name, QStringLiteral("/F"), QStringLiteral("/T")}, 1500);
    }
}
#endif

struct StreamPreset
{
    QString chiakiResolution;
    QString sidecarResolution;
    QString fps;
    QString codec;
    int bitrateKbps = 0;
    int previewFps = 30;
    int previewWidth = 854;
    QString label;
};

StreamPreset streamPresetForMode(RemotePlaySession::BandwidthMode mode)
{
    StreamPreset p;
    switch (mode) {
    case RemotePlaySession::BandwidthMode::Quality:
        // True 1080p test path. Keep software FFmpeg decode by leaving hw_decoder
        // unset below; if this still corrupts frames or overloads the machine, use
        // Performance/Balanced for gameplay and treat Quality as a diagnostic path.
        p.chiakiResolution = QStringLiteral("1080p");
        p.sidecarResolution = QStringLiteral("1920x1080");
        p.fps = QStringLiteral("60");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 12000;
        // 60fps preview at a sharp 1280px (downscaled from 1080p) for the live capture.
        // Needs hardware decode (d3d11va) on — applyBandwidthMode wires that when the
        // Hardware decode toggle is enabled.
        p.previewFps = 60;
        p.previewWidth = 1280;
        p.label = QStringLiteral("Quality (1080p60 / H264 / 12 Mbps)");
        break;
    case RemotePlaySession::BandwidthMode::Performance:
        p.chiakiResolution = QStringLiteral("720p");
        p.sidecarResolution = QStringLiteral("1280x720");
        p.fps = QStringLiteral("60");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 12000;
        p.previewFps = 60;        // was 30: throttled the QML preview to ~30fps ("feels 30fps")
        p.previewWidth = 1280;
        p.label = QStringLiteral("Performance (720p60 / H264 / 12 Mbps)");
        break;
    case RemotePlaySession::BandwidthMode::Balanced:
        p.chiakiResolution = QStringLiteral("720p");
        p.sidecarResolution = QStringLiteral("1280x720");
        p.fps = QStringLiteral("60");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 4000;
        // 60fps preview at full 720p width (1280) for a smooth, crisp live capture.
        p.previewFps = 60;
        p.previewWidth = 1280;
        p.label = QStringLiteral("Balanced (720p60 / H264 / 4 Mbps)");
        break;
    case RemotePlaySession::BandwidthMode::LowBandwidth:
        p.chiakiResolution = QStringLiteral("540p");
        p.sidecarResolution = QStringLiteral("960x540");
        p.fps = QStringLiteral("60");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 2500;
        p.previewFps = 24;
        p.previewWidth = 720;
        p.label = QStringLiteral("Low Bandwidth (540p60 / H264 / 2.5 Mbps)");
        break;
    case RemotePlaySession::BandwidthMode::UltraLow:
        p.chiakiResolution = QStringLiteral("360p");
        p.sidecarResolution = QStringLiteral("640x360");
        p.fps = QStringLiteral("30");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 1200;
        p.previewFps = 20;
        p.previewWidth = 640;
        p.label = QStringLiteral("Ultra-Low (360p30 / H264 / 1.2 Mbps)");
        break;
    case RemotePlaySession::BandwidthMode::Experimental120:
        p.chiakiResolution = QStringLiteral("720p");
        p.sidecarResolution = QStringLiteral("1280x720");
        p.fps = QStringLiteral("120");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 18000;
        p.previewFps = 60;        // was 30
        p.previewWidth = 854;
        p.label = QStringLiteral("Experimental (720p120 / H264 / 18 Mbps)");
        break;
    case RemotePlaySession::BandwidthMode::Experimental240:
        p.chiakiResolution = QStringLiteral("720p");
        p.sidecarResolution = QStringLiteral("1280x720");
        p.fps = QStringLiteral("240");
        p.codec = QStringLiteral("h264");
        p.bitrateKbps = 24000;
        p.previewFps = 60;        // was 30
        p.previewWidth = 854;
        p.label = QStringLiteral("Experimental (720p240 / H264 / 24 Mbps)");
        break;
    }
    return p;
}

RemotePlaySession::BandwidthMode bandwidthModeFromConfig(const AppConfigData& config)
{
    const QString lower = config.streamBandwidthMode.trimmed().toLower();
    if (lower == QLatin1String("quality")) {
        return RemotePlaySession::BandwidthMode::Quality;
    }
    if (lower == QLatin1String("performance")) {
        return RemotePlaySession::BandwidthMode::Performance;
    }
    if (lower == QLatin1String("lowbandwidth") || lower == QLatin1String("low_bandwidth") || lower == QLatin1String("low")) {
        return RemotePlaySession::BandwidthMode::LowBandwidth;
    }
    if (lower == QLatin1String("ultralow") || lower == QLatin1String("ultra_low") || lower == QLatin1String("ultra")) {
        return RemotePlaySession::BandwidthMode::UltraLow;
    }
    if (lower == QLatin1String("experimental120")) {
        return RemotePlaySession::BandwidthMode::Experimental120;
    }
    if (lower == QLatin1String("experimental240")) {
        return RemotePlaySession::BandwidthMode::Experimental240;
    }
    return RemotePlaySession::BandwidthMode::Balanced;
}

void pruneChiakiSessionLogs()
{
    const QString appData = QProcessEnvironment::systemEnvironment().value(QStringLiteral("APPDATA"));
    if (appData.isEmpty()) {
        return;
    }
    QDir dir(appData + QStringLiteral("/Chiaki/Chiaki/log"));
    if (!dir.exists()) {
        return;
    }
    dir.setNameFilters({QStringLiteral("chiaki_session_*.log")});
    QFileInfoList logs = dir.entryInfoList(QDir::Files, QDir::Time);
    constexpr qint64 maxSingleLogBytes = 64LL * 1024LL * 1024LL;
    for (int i = 0; i < logs.size(); ++i) {
        const QFileInfo& info = logs.at(i);
        if (i >= 5 || info.size() > maxSingleLogBytes) {
            QFile::remove(info.absoluteFilePath());
        }
    }
}

#ifdef Q_OS_WIN
QString processBaseName(DWORD pid)
{
    if (!pid) {
        return {};
    }
    HANDLE handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
    if (!handle) {
        return {};
    }
    wchar_t path[MAX_PATH * 4] = {};
    DWORD size = static_cast<DWORD>(sizeof(path) / sizeof(path[0]));
    const BOOL ok = QueryFullProcessImageNameW(handle, 0, path, &size);
    CloseHandle(handle);
    if (!ok || size == 0) {
        return {};
    }
    return QFileInfo(QString::fromWCharArray(path, static_cast<int>(size))).fileName().toLower();
}

bool isChiakiProcess(DWORD pid)
{
    const QString name = processBaseName(pid);
    return name == QLatin1String("orionstream.exe")
        || name == QLatin1String("chiaki.exe")
        || name == QLatin1String("chiaki-ng.exe")
        || name == QLatin1String("chiaki4deck.exe");
}

template <typename T>
void releaseCom(T*& ptr)
{
    if (ptr) {
        ptr->Release();
        ptr = nullptr;
    }
}

// Returns true if at least one Chiaki audio session was found and the mute
// state was applied. Returns false when there is no Chiaki audio session yet
// (e.g. chiaki just launched, audio hasn't started, audio device is rebinding)
// so the caller can schedule a retry. This is the fix for "audio mute button
// doesn't work for live capture" â€” Chiaki's audio session frequently isn't
// created until after the first remote-play frames have been decoded.
bool setChiakiAudioMuted(bool muted)
{
    HRESULT init = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    const bool uninit = SUCCEEDED(init);
    if (FAILED(init) && init != RPC_E_CHANGED_MODE) {
        return false;
    }

    bool appliedAny = false;
    IMMDeviceEnumerator* enumerator = nullptr;
    IMMDevice* device = nullptr;
    IAudioSessionManager2* manager = nullptr;
    IAudioSessionEnumerator* sessions = nullptr;

    if (SUCCEEDED(CoCreateInstance(__uuidof(MMDeviceEnumerator), nullptr, CLSCTX_ALL,
                                   __uuidof(IMMDeviceEnumerator), reinterpret_cast<void**>(&enumerator)))
        && SUCCEEDED(enumerator->GetDefaultAudioEndpoint(eRender, eConsole, &device))
        && SUCCEEDED(device->Activate(__uuidof(IAudioSessionManager2), CLSCTX_ALL, nullptr,
                                      reinterpret_cast<void**>(&manager)))
        && SUCCEEDED(manager->GetSessionEnumerator(&sessions))) {
        int count = 0;
        if (SUCCEEDED(sessions->GetCount(&count))) {
            for (int i = 0; i < count; ++i) {
                IAudioSessionControl* control = nullptr;
                IAudioSessionControl2* control2 = nullptr;
                ISimpleAudioVolume* volume = nullptr;
                if (SUCCEEDED(sessions->GetSession(i, &control))
                    && SUCCEEDED(control->QueryInterface(__uuidof(IAudioSessionControl2), reinterpret_cast<void**>(&control2)))) {
                    DWORD pid = 0;
                    if (SUCCEEDED(control2->GetProcessId(&pid)) && isChiakiProcess(pid)
                        && SUCCEEDED(control2->QueryInterface(__uuidof(ISimpleAudioVolume), reinterpret_cast<void**>(&volume)))) {
                        if (SUCCEEDED(volume->SetMute(muted ? TRUE : FALSE, nullptr))) {
                            appliedAny = true;
                        }
                    }
                }
                releaseCom(volume);
                releaseCom(control2);
                releaseCom(control);
            }
        }
    }

    releaseCom(sessions);
    releaseCom(manager);
    releaseCom(device);
    releaseCom(enumerator);
    if (uninit) {
        CoUninitialize();
    }
    return appliedAny;
}

struct EnvRestore {
    QByteArray key;
    QByteArray oldValue;
    bool hadValue = false;
};

QList<EnvRestore> applyChiakiControllerEnvironment()
{
    const QList<QPair<QByteArray, QByteArray>> values = {
        {QByteArrayLiteral("SDL_GAMECONTROLLER_IGNORE_DEVICES"),
         QByteArrayLiteral("0x054c/0x0ce6,0x054c/0x05c4,0x054c/0x09cc,0x054c/0x0df2,0x054c/0x0e5f")},
        {QByteArrayLiteral("SDL_JOYSTICK_HIDAPI_PS5"), QByteArrayLiteral("0")},
        {QByteArrayLiteral("SDL_JOYSTICK_HIDAPI_PS4"), QByteArrayLiteral("0")}
    };

    QList<EnvRestore> restore;
    for (const auto& item : values) {
        EnvRestore r;
        r.key = item.first;
        r.hadValue = qEnvironmentVariableIsSet(item.first.constData());
        if (r.hadValue) {
            r.oldValue = qgetenv(item.first.constData());
        }
        qputenv(item.first.constData(), item.second);
        restore.append(r);
    }
    return restore;
}

void restoreEnvironment(const QList<EnvRestore>& restore)
{
    for (const auto& item : restore) {
        if (item.hadValue) {
            qputenv(item.key.constData(), item.oldValue);
        } else {
            qunsetenv(item.key.constData());
        }
    }
}
#endif

struct RegisteredHost {
    QString nickname;       // human-readable name from chiaki ("Isaiahs PS5")
    QString hostIdHex;      // 12-char uppercase hex of server_mac, no separators
    QString lastKnownIp;    // last IP chiaki saw the console at, if available
};

// Normalise a MAC / host-id to bare uppercase hex (no colons, no dashes).
QString normalizeHostId(const QString& raw)
{
    QString out;
    out.reserve(raw.size());
    for (QChar c : raw) {
        if (c.isLetterOrNumber()) {
            out.append(c.toUpper());
        }
    }
    return out;
}

QString macBytesToHex(const QByteArray& bytes)
{
    QString hex;
    hex.reserve(bytes.size() * 2);
    for (unsigned char b : bytes) {
        hex.append(QString::asprintf("%02X", b));
    }
    return hex;
}

// Read chiaki's registered_hosts entries from QSettings (HKCU\Software\Chiaki\Chiaki
// on Windows, ~/.config/Chiaki/Chiaki.conf elsewhere). Each entry exposes the raw
// 6-byte server_mac which we hex-encode so we can compare against the host-id
// field returned by the discovery protocol.
QList<RegisteredHost> readRegisteredHosts()
{
    QList<RegisteredHost> result;
    QSettings settings(QSettings::UserScope, QStringLiteral("Chiaki"), QStringLiteral("Chiaki"));

    QHash<QString, QString> nicknameByHostId;
    settings.beginGroup(QStringLiteral("registered_hosts"));
    const QStringList registeredGroups = settings.childGroups();
    for (const QString& group : registeredGroups) {
        settings.beginGroup(group);
        RegisteredHost host;
        host.nickname = settings.value(QStringLiteral("server_nickname")).toString();
        if (host.nickname.isEmpty()) {
            host.nickname = settings.value(QStringLiteral("ps5_nickname")).toString();
        }
        const QVariant macVar = settings.value(QStringLiteral("server_mac"));
        QByteArray macBytes;
        if (macVar.userType() == QMetaType::QByteArray) {
            macBytes = macVar.toByteArray();
        } else {
            // Fall back to parsing whatever string representation chiaki stored.
            macBytes = QByteArray::fromHex(normalizeHostId(macVar.toString()).toLatin1());
        }
        host.hostIdHex = macBytesToHex(macBytes);
        if (!host.hostIdHex.isEmpty() || !host.nickname.isEmpty()) {
            result.append(host);
            if (!host.hostIdHex.isEmpty() && !host.nickname.isEmpty()) {
                nicknameByHostId.insert(host.hostIdHex, host.nickname);
            }
        }
        settings.endGroup();
    }
    settings.endGroup();

    // chiaki-ng stores manually added / last-used consoles separately from the
    // registered host credentials. This is the most reliable source for Orion's
    // one-time setup flow because it contains the actual host IP selected by Chiaki.
    settings.beginGroup(QStringLiteral("manual_hosts"));
    const QStringList manualGroups = settings.childGroups();
    for (const QString& group : manualGroups) {
        settings.beginGroup(group);
        RegisteredHost host;
        host.lastKnownIp = settings.value(QStringLiteral("host")).toString().trimmed();
        const QVariant macVar = settings.value(QStringLiteral("registered_mac"));
        QByteArray macBytes;
        if (macVar.userType() == QMetaType::QByteArray) {
            macBytes = macVar.toByteArray();
        } else {
            macBytes = QByteArray::fromHex(normalizeHostId(macVar.toString()).toLatin1());
        }
        host.hostIdHex = macBytesToHex(macBytes);
        host.nickname = nicknameByHostId.value(host.hostIdHex);
        if (!host.lastKnownIp.isEmpty()) {
            result.append(host);
        }
        settings.endGroup();
    }
    settings.endGroup();

    return result;
}

QString registeredConsoleRouteIdentity(const QString& consoleIp)
{
    const QString ip = consoleIp.trimmed();
    if (ip.isEmpty()) {
        return {};
    }

    QSet<QString> matchingHostIds;
    for (const auto& host : readRegisteredHosts()) {
        const QString hostId = normalizeHostId(host.hostIdHex);
        if (host.lastKnownIp.trimmed() == ip && hostId.size() == 12) {
            bool hexOnly = true;
            for (const QChar c : hostId) {
                if (!((c >= QLatin1Char('0') && c <= QLatin1Char('9'))
                      || (c >= QLatin1Char('A') && c <= QLatin1Char('F')))) {
                    hexOnly = false;
                    break;
                }
            }
            if (hexOnly) {
                matchingHostIds.insert(hostId);
            }
        }
    }
    // No match or an ambiguous IP is intentionally cold. Never fall back to hashing the IP,
    // nickname, account id, or registration secret; none is an exact console identity here.
    if (matchingHostIds.size() != 1) {
        return {};
    }

    QByteArray material("orion-console-route-v1", 22);
    material.append('\0');
    material.append(matchingHostIds.constBegin()->toLatin1());
    const QByteArray digest = QCryptographicHash::hash(
        material, QCryptographicHash::Sha256).toHex();
    return QStringLiteral("registered-host-sha256-v1:") + QString::fromLatin1(digest);
}

} // namespace

RemotePlaySession::RemotePlaySession(QObject* parent)
    : QObject(parent)
{
    // Placeholder frame timer is no longer used; the autogreen sidecar emits
    // real frames captured from the Chiaki stream window via stdout.
    frameTimer_.setInterval(16);
    connect(&frameTimer_, &QTimer::timeout, this, &RemotePlaySession::emitPlaceholderFrame);
    // Preview frames are base64-JPEG decoded on this worker's own thread (NOT the GUI/render thread).
    // Both decoded JPEG and SHM frames converge on the same bounded display-only presenter below;
    // detector pixels/timestamps never enter that queue.
    frameDecoder_ = new FrameDecoder(this);
    connect(frameDecoder_, &FrameDecoder::decoded, this,
            [this](QImage image, int frameNumber) {
                queuePreviewFrame(std::move(image), frameNumber);
            });
    connect(&shmPump_, &SharedMemoryFramePump::batchReady,
            this, &RemotePlaySession::handleShmPumpBatch);
    previewPresentationTimer_.setTimerType(Qt::PreciseTimer);
    previewPresentationTimer_.setSingleShot(true);
    updatePreviewPresentationCadence(true);
    connect(&previewPresentationTimer_, &QChronoTimer::timeout,
            this, &RemotePlaySession::presentNextPreviewFrame);
    audioRetryTimer_.setInterval(250);
    connect(&audioRetryTimer_, &QTimer::timeout, this, [this]() {
#ifdef Q_OS_WIN
        ++audioRetryAttempts_;
        if (setChiakiAudioMuted(pendingAudioMuted_) || audioRetryAttempts_ >= 24) {
            audioRetryTimer_.stop();
            setAudioBusy(false);
        }
#else
        audioRetryTimer_.stop();
        setAudioBusy(false);
#endif
    });
}

RemotePlaySession::~RemotePlaySession()
{
    sidecarRestartPending_ = false;
    resetPreviewPresentation(false);
    retireShmSourceEpoch();
    // GRACEFUL CHIAKI DISCONNECT: the job object closed below carries KILL_ON_JOB_CLOSE, so
    // closing its handle TerminateProcess-es the sidecar — and with it chiaki — with no chance to
    // run chiaki_session_stop(). No Takion/ctrl disconnect reaches the PS5, which is exactly the
    // abrupt transport loss it reports as "LAN cable disconnected". So the graceful shutdown must
    // COMPLETE first and the job close must be nothing more than a last-resort reaper.
    //
    // On the normal path stop() already ran (OrionAppController's teardown calls it before this
    // object dies) and sidecarProcess_ is null, so this is a no-op. This handles the abnormal
    // path — destruction without a prior stop() — where the job close was the ONLY teardown.
    //
    // Deliberately NOT stopSidecar(): waitForFinished() drives QProcess::finished, whose handler
    // emits setupMessage/sidecarExited/stateChanged. We are inside a member's destructor, so the
    // owning OrionAppController is already destroyed while its QObject base (and therefore its
    // connections) is still alive — emitting there would dispatch into a dead object. Detach the
    // process's signals first, then drain it directly.
    if (auto* proc = sidecarProcess_) {
        sidecarProcess_ = nullptr;
        proc->disconnect();   // no finished/readyRead handlers may run from here on
        if (proc->state() != QProcess::NotRunning) {
            const QByteArray line = QJsonDocument(QJsonObject{{QStringLiteral("cmd"), QStringLiteral("shutdown")}})
                                        .toJson(QJsonDocument::Compact) + '\n';
            proc->write(line);
            proc->closeWriteChannel();
            proc->terminate();
            // Bounded: if it refuses to go, the job close below still reaps it.
            proc->waitForFinished(kSidecarGracefulShutdownMs);
        }
        delete proc;   // not deleteLater(): no event loop is guaranteed to run after this point
    }
#ifdef Q_OS_WIN
    if (sidecarJob_) {
        CloseHandle(static_cast<HANDLE>(sidecarJob_));
        sidecarJob_ = nullptr;
    }
#endif
}

#ifdef Q_OS_WIN
void RemotePlaySession::assignSidecarToJob(QProcess* process)
{
    if (!process || process != sidecarProcess_ || process->processId() == 0) {
        return;
    }

    if (!sidecarJob_) {
        HANDLE job = CreateJobObjectW(nullptr, nullptr);
        if (!job) {
            emit setupMessage(QStringLiteral("Sidecar job object unavailable (winerr=%1); startup reaping remains active.")
                                  .arg(GetLastError()));
            return;
        }
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits{};
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if (!SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits))) {
            const DWORD error = GetLastError();
            CloseHandle(job);
            emit setupMessage(QStringLiteral("Sidecar job object setup failed (winerr=%1); startup reaping remains active.")
                                  .arg(error));
            return;
        }
        sidecarJob_ = job;
    }

    HANDLE child = OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, FALSE,
                               static_cast<DWORD>(process->processId()));
    if (!child) {
        emit setupMessage(QStringLiteral("Sidecar job assignment skipped (OpenProcess winerr=%1).")
                              .arg(GetLastError()));
        return;
    }
    const bool assigned = AssignProcessToJobObject(static_cast<HANDLE>(sidecarJob_), child);
    const DWORD error = assigned ? ERROR_SUCCESS : GetLastError();
    CloseHandle(child);
    if (assigned) {
        emit setupMessage(QStringLiteral("Sidecar job assigned: orphan cleanup armed."));
    } else {
        emit setupMessage(QStringLiteral("Sidecar job assignment failed (winerr=%1); startup reaping remains active.")
                              .arg(error));
    }
}
#endif

void RemotePlaySession::setRootDir(QString rootDir)
{
    rootDir_ = std::move(rootDir);
}

void RemotePlaySession::applyConfig(const AppConfigData& config)
{
    config_ = config;
    telemetry_.consoleIp = config.remotePlayConsoleIp;
    if (config_.streamAudioMode.compare(QStringLiteral("Off"), Qt::CaseInsensitive) == 0) {
        config_.streamAudioEnabled = false;
    } else if (config_.streamAudioMode.compare(QStringLiteral("Stabilized"), Qt::CaseInsensitive) == 0) {
        config_.streamAudioMode = QStringLiteral("Stabilized");
        config_.streamAudioEnabled = true;
    } else if (config_.streamAudioEnabled) {
        config_.streamAudioMode = QStringLiteral("Standard");
    } else {
        config_.streamAudioMode = QStringLiteral("Off");
    }
    scheduleAudioApply(!config_.streamAudioEnabled);
    // Push timing-relevant values to a RUNNING sidecar so UI edits take effect
    // LIVE. Previously these only applied on the next reconnect (fresh sidecar
    // --config-json), which made it look like the sliders did nothing mid-game.
    pushRemapUpdate();
}

void RemotePlaySession::pushRemapUpdate()
{
    if (!sidecarProcess_ || sidecarProcess_->state() != QProcess::Running) {
        return;
    }
    QJsonObject cmd;
    cmd.insert(QStringLiteral("cmd"), QStringLiteral("update_remap"));
    cmd.insert(QStringLiteral("enabled"), true);
    cmd.insert(QStringLiteral("shot_trigger_mode"), QStringLiteral("button"));
    QString inputModeStr = QStringLiteral("square_only");
    if (config_.remotePlayInputSource == QLatin1String("stick")) {
        inputModeStr = QStringLiteral("stick_only");
    } else if (config_.remotePlayInputSource == QLatin1String("both")) {
        inputModeStr = QStringLiteral("both");
    }
    cmd.insert(QStringLiteral("input_mode"), inputModeStr);
    cmd.insert(QStringLiteral("goto_enabled"), true);
    cmd.insert(QStringLiteral("early_late_offset_ms"), config_.earlyLateOffsetMs);
    cmd.insert(QStringLiteral("gpc_flick_chain_ms"), config_.latencyCompensationMs);
    cmd.insert(QStringLiteral("min_hold_ms"), config_.minimumHoldMs);
    cmd.insert(QStringLiteral("max_hold_ms"), config_.maximumHoldMs);
    cmd.insert(QStringLiteral("fixed_hold_ms"), config_.fixedHoldMs);
    cmd.insert(QStringLiteral("stable_frames_required"), config_.stableFrames);
    cmd.insert(QStringLiteral("confidence_gate"), config_.detectionConfidencePercent / 100.0);
    cmd.insert(QStringLiteral("green_window_target"), QStringLiteral("tip"));
    cmd.insert(QStringLiteral("meter_style"), config_.meterStyle);
    cmd.insert(QStringLiteral("meter_color"), config_.meterColor);
    cmd.insert(QStringLiteral("no_meter_enabled"), config_.noMeterEnabled);
    cmd.insert(QStringLiteral("no_meter_release_point"), config_.noMeterReleasePoint);
    cmd.insert(QStringLiteral("no_meter_base_offset_ms"), config_.noMeterBaseOffsetMs);
    cmd.insert(QStringLiteral("decode_latency_ms"), config_.noMeterDecodeCompMs);
    cmd.insert(QStringLiteral("no_meter_confidence_gate"), config_.noMeterConfidenceGate);
    cmd.insert(QStringLiteral("no_meter_push_release_window_ms"), config_.noMeterPushReleaseWindowMs);
    cmd.insert(QStringLiteral("no_meter_handedness"), config_.noMeterHandedness);
    cmd.insert(QStringLiteral("active_shot_type"), config_.activeShotType);
    QJsonObject sto;
    for (auto it = config_.shotTypeOffsets.constBegin(); it != config_.shotTypeOffsets.constEnd(); ++it) {
        sto.insert(it.key(), it.value());
    }
    cmd.insert(QStringLiteral("shot_type_offsets"), sto);
    sendSidecarCommand(cmd);
}

void RemotePlaySession::setAudioEnabled(bool enabled)
{
    config_.streamAudioEnabled = enabled;
    if (enabled && config_.streamAudioMode.compare(QStringLiteral("Off"), Qt::CaseInsensitive) == 0) {
        config_.streamAudioMode = QStringLiteral("Standard");
    } else if (!enabled) {
        config_.streamAudioMode = QStringLiteral("Off");
    }
    scheduleAudioApply(!enabled);
}

void RemotePlaySession::setAudioMode(const QString& mode)
{
    const QString lower = mode.trimmed().toLower();
    if (lower == QLatin1String("stabilized")) {
        config_.streamAudioMode = QStringLiteral("Stabilized");
        config_.streamAudioEnabled = true;
    } else if (lower == QLatin1String("standard") || lower == QLatin1String("on")) {
        config_.streamAudioMode = QStringLiteral("Standard");
        config_.streamAudioEnabled = true;
    } else {
        config_.streamAudioMode = QStringLiteral("Off");
        config_.streamAudioEnabled = false;
    }
    scheduleAudioApply(!config_.streamAudioEnabled);
}

void RemotePlaySession::setAudioBusy(bool busy)
{
    if (audioToggleBusy_ == busy) {
        return;
    }
    audioToggleBusy_ = busy;
    emit audioToggleBusyChanged(busy);
}

void RemotePlaySession::cancelAudioApply()
{
    audioRetryTimer_.stop();
    audioRetryAttempts_ = 0;
    setAudioBusy(false);
}

void RemotePlaySession::scheduleAudioApply(bool muted)
{
    pendingAudioMuted_ = muted;
    audioRetryAttempts_ = 0;
    if (state_ == RemotePlayState::Disconnected || state_ == RemotePlayState::Error) {
        setAudioBusy(false);
        return;
    }
#ifdef Q_OS_WIN
    setAudioBusy(true);
    if (setChiakiAudioMuted(pendingAudioMuted_)) {
        setAudioBusy(false);
        return;
    }
    if (!audioRetryTimer_.isActive()) {
        audioRetryTimer_.start();
    }
#else
    setAudioBusy(false);
#endif
}

bool RemotePlaySession::reportRemotePlayExecutableIdentity(
    const QString& path, const QString& context)
{
    const ExecutableIdentity identity = readExecutableIdentity(path);
    if (!identity.valid) {
        emit setupMessage(QStringLiteral("Remote Play client identity FAILED: context=%1 path=%2 error=%3")
                              .arg(context, QDir::toNativeSeparators(path),
                                   identity.error.left(160)));
        return false;
    }

    auto source = resolveChiakiExecutable(config_, rootDir_).source;
    if (source == remote_play_executable_policy::Source::None) {
        source = remote_play_executable_policy::Source::DeveloperConfigured;
    }
    emit setupMessage(QStringLiteral("Remote Play client image: context=%1 source=%2 "
                                     "size=%3 sha256=%4 path=%5")
                          .arg(context,
                               remote_play_executable_policy::sourceName(source))
                          .arg(identity.size)
                          .arg(identity.sha256, QDir::toNativeSeparators(path)));
    return true;
}

void RemotePlaySession::start()
{
    if (state_ == RemotePlayState::Running || state_ == RemotePlayState::Connecting) {
        return;
    }

    if (config_.remotePlayConsoleIp.trimmed().isEmpty()) {
        setState(RemotePlayState::Error, QStringLiteral("Console IP is required before starting Chiaki"));
        return;
    }

    if (resolveChiakiPath(config_, rootDir_).isEmpty()) {
#ifdef ORION_PRODUCTION_BUILD
        setState(RemotePlayState::Error,
                 QStringLiteral("Production package is incomplete: bundled "
                                "chiaki-ng-orion/chiaki-ng-Win/OrionStream.exe was not found "
                                "inside the Orion install."));
#else
        setState(RemotePlayState::Error,
                 QStringLiteral("Chiaki executable was not found. Set the Chiaki path before connecting."));
#endif
        return;
    }

    // A fresh connect supersedes any watchdog respawn still waiting out its release beat — this
    // path brings its own sidecar up, so letting the stale timer through would double-spawn.
    sidecarRestartPending_ = false;
    ++inputRecoveryGeneration_;
    inputRecoveryPending_ = false;
    rejectLateInputRecoveryReady_ = true;

    const bool warmPreviewRunning = previewMode_
                                    && sidecarProcess_ != nullptr
                                    && sidecarProcess_->state() != QProcess::NotRunning;

    // FAST-PATH (connect reuse): the warm preview sidecar already has the capture card + detector
    // running, and in capture-card mode Chiaki is INPUT-ONLY (the card is the video source). So when
    // the connect source matches the warm source, DON'T kill the preview and cold-rebuild a new
    // process (a ~5-6s waste: terminate + waitForFinished + 2-pass taskkill + the ~3s Elgato-release
    // beat + a from-scratch card re-open, all BEFORE Chiaki's ~9s cold start). Instead COMMAND the
    // live sidecar to launch Chiaki/input in place while its card feed + detector keep flowing —
    // seamless (the same sidecar keeps emitting preview frames, so the panel never blinks) and it
    // skips the teardown entirely. See shouldReuseWarmSidecarForConnect for the source-match guard.
    if (shouldReuseWarmSidecarForConnect(warmPreviewRunning,
                                         /*warmSourceIsCaptureCard=*/warmPreviewRunning,
                                         isCaptureCardSource(config_))) {
        streamPromoteFromWarmPreview_ = true;
        previewMode_ = false;
        pruneChiakiSessionLogs();
        setState(RemotePlayState::Connecting,
                 QStringLiteral("Reusing live preview - starting Chiaki input"));
        promoteWarmPreviewToStream();
        scheduleAudioApply(!config_.streamAudioEnabled);
        return;
    }

    streamPromoteFromWarmPreview_ = false;

    // Hand the capture device over from a live-preview sidecar (if one is running) to the full
    // pipeline. The Elgato can only be opened once, so tear the preview down FIRST — while the
    // session is still logically Disconnected, so its QProcess::finished is not misread as a
    // mid-connect crash — then bring the full sidecar up after the capture-card release beat so the
    // device is never double-opened. capturePreviewActive stays set until the Connecting transition
    // takes the panel over (OrionAppController), so the last preview frame holds through the handoff
    // instead of flashing the idle placeholder. Reached only when the source CHANGED between preview
    // and connect (e.g. switched off the capture card) — the reuse fast-path above handles the
    // common capture-card-unchanged connect.
    //
    // FIX 1 follow-on: the condition is "a sidecar is STILL RUNNING", not "a preview is running".
    // start() has already returned for Running/Connecting, so reaching here with a live sidecar
    // means an orphan from a session that ended in Disconnected/Error — most importantly the warm
    // sidecar left alive by a FAILED stream promotion. With the old `warmPreviewRunning` condition
    // (which requires previewMode_, cleared by the promotion) that orphan was not torn down, and
    // startSidecar()'s "already running" guard then made the retry a silent no-op: the state sat on
    // Connecting forever with nothing actually starting. Tearing any live sidecar down first also
    // keeps the single-open Elgato from being double-opened, which is the original intent here.
    const bool handOffPreview = sidecarProcess_ != nullptr
                                && sidecarProcess_->state() != QProcess::NotRunning;
    previewMode_ = false;
    if (handOffPreview) {
        intentionalSidecarRestart_ = true;   // preview teardown must not read as a mid-stream crash
        stopSidecar();
    }

    pruneChiakiSessionLogs();
#ifdef Q_OS_WIN
    // P4: reap any orphaned stream client from a previous crash / hard-kill BEFORE launching so
    // the orchestrator never spawns a duplicate. Safe here: start() returned above if a session is
    // already Connecting/Running, so this only ever clears genuine orphans. SKIP it on a warm
    // preview handoff — stopSidecar() just above already ran cleanupRemotePlayClientsBlocking(), a
    // 2-pass SUPERSET of this single sweep over the identical process names, so re-running it here
    // only added blocking taskkill spawns (GUI-thread stall) to every warm Connect for zero extra
    // cleanup. (Preview never launches chiaki/OrionStream, so on that path there is nothing to reap
    // beyond what the teardown already swept.)
    if (!handOffPreview) {
        reapOrphanStreamClientsBlocking();
    }
#endif
    setState(RemotePlayState::Connecting, QStringLiteral("Starting Chiaki autogreen sidecar"));
    if (handOffPreview) {
        // Deferred start once the preview sidecar has released the Elgato handle (same beat the
        // watchdog uses for a capture-card restart). Guard against a disconnect during the wait.
        QTimer::singleShot(sidecarRestartDelayMs(isCaptureCardSource(config_)), this, [this]() {
            if (state_ == RemotePlayState::Connecting) {
                startSidecar();
            }
        });
    } else {
        startSidecar();
    }
    scheduleAudioApply(!config_.streamAudioEnabled);
}

void RemotePlaySession::startCapturePreview()
{
    // Live capture-card preview BEFORE Connect: open the HDMI card and stream preview frames to the
    // QML panel WITHOUT launching Chiaki, the input hook, or the virtual pad. Only meaningful for
    // the capture-card source — the decoder pipe needs Chiaki, so there is nothing to show
    // pre-connect there. No-op if a session (preview or full) is already up.
    if (!isCaptureCardSource(config_)) {
        return;
    }
    if (sidecarProcess_ && sidecarProcess_->state() != QProcess::NotRunning) {
        return;
    }
    if (state_ == RemotePlayState::Running || state_ == RemotePlayState::Connecting) {
        return;
    }
    previewMode_ = true;
    // Stay logically Disconnected so Connect remains enabled; the status text signals preview.
    setState(RemotePlayState::Disconnected, QStringLiteral("Live capture preview"));
    startSidecar();
    emit previewActiveChanged(true);
}

void RemotePlaySession::stop()
{
    // User-initiated disconnect: flag the teardown as INTENTIONAL before killing the
    // sidecar so its QProcess::finished emits sidecarExited(false), not a phantom
    // mid-stream crash. Without this the crash handler in OrionAppController treats the
    // disconnect's sidecar exit as a crash, calls restartSidecar() (re-spawning a chiaki
    // window) and then drops to safe mode — i.e. "disconnect" failed to mean disconnect.
    // Only set it when a live process is actually being torn down here (same guard as
    // restartSidecar), so a genuine prior crash with no finished event still escalates.
    intentionalSidecarRestart_ = (sidecarProcess_ != nullptr
                                  && sidecarProcess_->state() != QProcess::NotRunning);
    // Cancel any watchdog respawn still waiting out its capture-card release beat. Without this a
    // restart armed just before the user clicked Disconnect relaunches the sidecar (and chiaki,
    // and therefore the PS5 session) seconds after the teardown finished.
    sidecarRestartPending_ = false;
    // FIX 1: cancel any in-flight promotion bookkeeping so its deferred fallback/deadline timers
    // stand down instead of firing an "Autogreen running" / "did not confirm" over a teardown.
    ++streamPromoteGeneration_;
    streamPromotePending_ = false;
    streamPromoteAcked_ = false;
    streamPromoteFromWarmPreview_ = false;
    ++inputRecoveryGeneration_;
    inputRecoveryPending_ = false;
    rejectLateInputRecoveryReady_ = true;
    if (previewMode_) {
        previewMode_ = false;
        emit previewActiveChanged(false);
    }
    stopSidecar();
    frameTimer_.stop();
    telemetry_ = {};
    frameCounter_ = 0;
    sidecarFps_ = 0;
    requestedFps_ = 60;
    captureLoopFps_ = 0;
    uniqueFrameFps_ = 0;
    duplicateFramePct_ = 0.0;
    frameAgeMs_ = 0.0;
    pixelAgeMs_ = 0.0;
    transportAgeMs_ = 0.0;
    capturePublicationAgeMs_ = 0.0;
    backendFrozen_ = false;
    captureWidth_ = 0;
    captureHeight_ = 0;
    captureTier_.clear();
    lastSidecarDetectionFrameCount_ = -1;
    lastSidecarDetectionFrameNumber_ = -1;
    previewLag_ = 0;
    fpsProbeState_ = QStringLiteral("stable-60");
    shotFillPct_ = 0.0;
    shotConfidence_ = 0.0;
    shotState_ = QStringLiteral("Idle");
    algorithmText_ = QStringLiteral("Waiting for autogreen telemetry...");
    emit sidecarStatsChanged();
    setState(RemotePlayState::Disconnected, QStringLiteral("Disconnected"));
    emit telemetryReady(telemetry_);
}

void RemotePlaySession::triggerGotoShot()
{
    sendSidecarCommand({{"cmd", "trigger_goto"}});
}

void RemotePlaySession::armPose(quint64 armToken)
{
    const QString encoded = encodePoseArmToken(armToken);
    if (encoded.isEmpty()) {
        emit setupMessage(QStringLiteral("Refused pose_arm with invalid shot token"));
        return;
    }
    sendSidecarCommand({{"cmd", "pose_arm"},
                        {"arm_token", encoded}});
}

void RemotePlaySession::sendReleaseMarker(int seq, double wallMs, bool calibration,
                                          double validationTargetPct,
                                          double validationTolerancePct,
                                          quint64 physicalShotEpoch,
                                          quint64 shotAttempt)
{
    // RC-3: feed the frozen-meter latency oracle the release COMMAND timestamp. Keys MUST match the
    // sidecar consume side (autogreen_sidecar.py release_marker -> orch.mark_release):
    //   {"cmd":"release_marker","seq":<int>,"wall_ms":<epoch ms>,"calibration":<bool>,
    //    "validation_target_pct":<optional double>,
    //    "validation_tolerance_pct":<optional double>}
    // wall_ms is epoch ms (same clock as the sidecar's time.time()*1000 fill samples) so the oracle's
    // t*-minus-release subtraction is exact. Best-effort: sendSidecarCommand no-ops if the sidecar is down.
    sendSidecarCommand(makeReleaseMarkerCommand(
        seq, wallMs, calibration, validationTargetPct, validationTolerancePct,
        physicalShotEpoch, shotAttempt));
}

void RemotePlaySession::sendProbeMarker(int seq, double wallMs, double spawnOffsetMs)
{
    // [ORION_PROBE] warmup pump-fake probe PRESS timestamp for the latency estimator. Keys
    // match the sidecar consume side (autogreen_sidecar.py probe_marker -> orch.mark_probe):
    //   {"cmd":"probe_marker","seq":<int>,"wall_ms":<epoch ms>,"spawn_offset_ms":<double>}
    // spawn_offset_ms travels with the marker (the engine owns learning.json) so the sidecar
    // needs no config read; 0 = uncalibrated -> the estimator logs the raw press->appear only.
    sendSidecarCommand({{"cmd", "probe_marker"},
                        {"seq", seq},
                        {"wall_ms", wallMs},
                        {"spawn_offset_ms", spawnOffsetMs}});
}

void RemotePlaySession::calibrateMeter(const QString& phase, int count)
{
    sendSidecarCommand({{"cmd", "calibrate_meter"}, {"phase", phase}, {"count", count}});
}

void RemotePlaySession::setCourtIp(const QString& ip)
{
    if (ip.isEmpty()) return;
    sendSidecarCommand({{"cmd", "set_court_ip"}, {"ip", ip}});
}

void RemotePlaySession::observePacket(const QString& srcIp, const QString& dstIp,
                                       int srcPort, int dstPort, int size, double ts)
{
    sendSidecarCommand({
        {"cmd", "observe_packet"},
        {"src_ip", srcIp},
        {"dst_ip", dstIp},
        {"src_port", srcPort},
        {"dst_port", dstPort},
        {"size", size},
        {"ts", ts}
    });
}

void RemotePlaySession::checkBackend()
{
    const auto selection = resolveChiakiExecutable(config_, rootDir_);
    const ExecutableIdentity identity = readExecutableIdentity(selection.path);
    const bool ready = !selection.path.isEmpty() && identity.valid;
    QJsonObject result;
    result.insert(QStringLiteral("ok"), ready);
    result.insert(QStringLiteral("backend"), QStringLiteral("chiaki"));
    result.insert(QStringLiteral("path"), selection.path);
    result.insert(QStringLiteral("source"),
                  remote_play_executable_policy::sourceName(selection.source));
    if (identity.valid) {
        result.insert(QStringLiteral("size"), identity.size);
        result.insert(QStringLiteral("sha256"), identity.sha256);
    }
    if (selection.path.isEmpty()) {
#ifdef ORION_PRODUCTION_BUILD
        const QString error = QStringLiteral("Bundled production OrionStream.exe was not found; "
                                             "external configured paths are not permitted.");
#else
        const QString error = QStringLiteral("Chiaki executable was not found.");
#endif
        result.insert(QStringLiteral("error"), error);
        emit setupMessage(error);
    } else if (!identity.valid) {
        result.insert(QStringLiteral("error"),
                      QStringLiteral("Remote Play client identity could not be read: %1")
                          .arg(identity.error));
        reportRemotePlayExecutableIdentity(selection.path, QStringLiteral("backend-check"));
    } else {
        reportRemotePlayExecutableIdentity(selection.path, QStringLiteral("backend-check"));
    }
    emit helperResult(QStringLiteral("check"), result);
}

void RemotePlaySession::attestLatencyControllerRoute(const QString& deliveryRoute,
                                                     quint64 attestationGeneration)
{
    const QString route = deliveryRoute.trimmed().toLower();
    const QString encodedGeneration = encodePoseArmToken(attestationGeneration);
    if (route != QLatin1String("pipe") && route != QLatin1String("vigem_ds4")
        && route != QLatin1String("vigem_xusb")) {
        return;
    }
    if (encodedGeneration.isEmpty()) {
        return;
    }
    sendSidecarCommand({{"cmd", "latency_route_attestation"},
                        {"delivery_route", route},
                        {"attestation_generation", encodedGeneration}});
}

void RemotePlaySession::clearCourtTarget()
{
    sendSidecarCommand({{"cmd", "clear_court_target"}});
}

void RemotePlaySession::openChiakiClient()
{
    const QString path = resolveChiakiPath(config_, rootDir_);
    if (path.isEmpty()) {
        emit setupMessage(QStringLiteral("The bundled Remote Play client was not found."));
        return;
    }
    if (!reportRemotePlayExecutableIdentity(path, QStringLiteral("configuration-open"))) {
        emit setupMessage(QStringLiteral("Orion Stream client was not opened because its build "
                                         "identity could not be verified."));
        return;
    }

    // Lobby/config mode ONLY — never auto-start a stream. This is the one-time
    // console-registration entry point; the in-app Enable Bot + Controller button owns the
    // actual streaming + embed. (Passing `stream nickname host` would launch a
    // stream straight away, which is exactly what we don't want here.)
    bool started = false;
#ifdef Q_OS_WIN
    const auto restore = applyChiakiControllerEnvironment();
    started = QProcess::startDetached(path, {}, QFileInfo(path).absolutePath());
    restoreEnvironment(restore);
#else
    started = QProcess::startDetached(path, {}, QFileInfo(path).absolutePath());
#endif
    if (!started) {
        emit setupMessage(QStringLiteral("Failed to open the bundled Remote Play client."));
        return;
    }
    emit setupMessage(QStringLiteral("Remote Play client opened. Register or select your console, then return to Venice and press Enable Bot + Controller."));
}

void RemotePlaySession::applyBandwidthMode(BandwidthMode mode)
{
    // Translate the preset into chiaki-ng's QSettings keys. Chiaki stores per-target
    // overrides under settings/<key>_local_<console_type>, plus a global default the
    // engine reads when no local override exists. We write both so the value sticks
    // regardless of which screen the user previously edited inside Chiaki itself.
    const StreamPreset preset = streamPresetForMode(mode);

    QSettings cs(QSettings::UserScope, QStringLiteral("Chiaki"), QStringLiteral("Chiaki"));
    cs.beginGroup(QStringLiteral("settings"));

    // Global fallbacks (used when no per-console override is set).
    cs.setValue(QStringLiteral("resolution"), preset.chiakiResolution);
    cs.setValue(QStringLiteral("fps"), preset.fps);
    cs.setValue(QStringLiteral("codec"), preset.codec);
    if (preset.bitrateKbps > 0) {
        cs.setValue(QStringLiteral("bitrate"), preset.bitrateKbps);
    }

    // Per-target overrides â€” chiaki distinguishes "local" vs "remote" PS5 vs PS4.
    // We push the same preset everywhere so the user never has to toggle inside chiaki.
    const QStringList targets = {
        QStringLiteral("local_ps5"),
        QStringLiteral("local_ps4"),
        QStringLiteral("remote_ps5"),
        QStringLiteral("remote_ps4"),
    };
    for (const QString& t : targets) {
        cs.setValue(QStringLiteral("resolution_") + t, preset.chiakiResolution);
        cs.setValue(QStringLiteral("fps_") + t, preset.fps);
        cs.setValue(QStringLiteral("codec_") + t, preset.codec);
        if (preset.bitrateKbps > 0) {
            cs.setValue(QStringLiteral("bitrate_") + t, preset.bitrateKbps);
        }
    }

    // Decoder path. Default: software FFmpeg decode (empty -> NULL to
    // chiaki_ffmpeg_decoder_init()), which is robust on VM/remote GPUs but caps
    // 1080p60 at ~24-30 uniq fps (the laggy preview). Hardware decode (d3d11va)
    // unlocks full 60fps decode/export but can fail/corrupt on some VM/remote GPUs,
    // so it's an opt-in toggle the user can A/B for the 60fps smooth feed.
    // The decoder-pipe / QML-render path needs TRUE SOFTWARE decode for a steady 60fps: hardware
    // d3d11va keeps frames on the GPU and forces a per-frame synchronous GPU->CPU readback inside
    // OrionFrameExport (av_hwframe_transfer_data) -> a ~35fps ceiling (the "feels 30fps"). An EMPTY
    // hw_decoder passes NULL to the ffmpeg decoder = software (instant av_frame_ref, no readback,
    // full 60fps at 720p). So FORCE software here and ignore the persisted legacy toggle that
    // would silently cap the feed; hw decode stays available only via an explicit ORION_HW_DECODE=1.
    const bool vulkanBackend = config_.streamRenderBackend.compare(
        QStringLiteral("opengl"), Qt::CaseInsensitive) != 0;
    const bool explicitHardwareOptIn =
        qEnvironmentVariable("ORION_HW_DECODE").trimmed() == QStringLiteral("1");
    const auto decodePolicy = remote_play_decode::resolve(
        explicitHardwareOptIn, vulkanBackend);
    if (decodePolicy.hardwareDecode) {
        cs.setValue(QStringLiteral("hw_decoder"), QStringLiteral("d3d11va"));
    } else {
        cs.setValue(QStringLiteral("hw_decoder"), QString());
    }
    cs.setValue(QStringLiteral("log_verbose"), false);

    // Render backend drives display latency: vulkan + zero-copy is the proven
    // lag-free pair (opengl + no-zero-copy was the measured display-lag cause).
    // If the Vulkan surface refuses to reparent into the capture panel, the
    // embed watchdog flips this config to opengl, persists it, and restarts.
    cs.setValue(QStringLiteral("render_backend"), vulkanBackend ? QStringLiteral("vulkan") : QStringLiteral("opengl"));
    cs.setValue(QStringLiteral("window_type"), QStringLiteral("windowed"));
    cs.setValue(QStringLiteral("fullscreen_doubleclick"), false);
    cs.setValue(QStringLiteral("vsync"), false);
    // Zero-copy only makes sense for hardware-decoded GPU frames. With software decode the
    // frames are already on the CPU, so leave it off (avoids a zero-copy path on non-GPU frames).
    // Keep decode and zero-copy as one atomic opt-in. A stale legacy
    // hardware_decode=true setting must never enable the GPU half of the pair
    // while hw_decoder remains software.
    cs.setValue(QStringLiteral("use_zero_copy"),
                decodePolicy.zeroCopy);
    cs.setValue(QStringLiteral("audio_video_disabled"), false);
    cs.remove(QStringLiteral("geometry"));
    cs.remove(QStringLiteral("stream_geometry"));

    // Adaptive bitrate scaling keeps Remote Play usable on bursty Wi-Fi.
    cs.setValue(QStringLiteral("adaptive_bitrate"), true);
    cs.setValue(QStringLiteral("audio_fec"), false);
    cs.setValue(QStringLiteral("auto_discovery"), false);

    const QString audioMode = config_.streamAudioMode.trimmed().toLower();
    const int audioBuffer = audioMode == QLatin1String("stabilized") ? 8192 : 1920;
    cs.setValue(QStringLiteral("audio_buffer_size"), audioBuffer);
    cs.setValue(QStringLiteral("audio_buffer_size_raw"), audioBuffer);
    for (const QString& t : targets) {
        cs.setValue(QStringLiteral("audio_buffer_size_") + t, audioBuffer);
        cs.setValue(QStringLiteral("audio_buffer_size_raw_") + t, audioBuffer);
    }

    cs.endGroup();
    cs.sync();

    emit setupMessage(QStringLiteral("Streaming preset: %1").arg(preset.label));
}

void RemotePlaySession::discoverPs5()
{
    emit setupMessage(QStringLiteral("Scanning network for PlayStation consoles..."));

    // Chiaki's UDP discovery protocol. PS5 listens on 9302, PS4 on 987. Both reply
    // with an HTTP/1.1-style payload containing host-type/host-name/host-state when
    // they receive an SRC2 broadcast on the discovery port â€” even from standby mode.
    const QByteArray discoveryMsg =
        "SRCH * HTTP/1.1\n"
        "device-discovery-protocol-version:00030010\n";

    // Build the list of candidate broadcast addresses: per-interface subnet broadcasts
    // (192.168.137.255, 192.168.1.255, ...) plus the global 255.255.255.255 fallback.
    QList<QHostAddress> broadcasts;
    broadcasts << QHostAddress(QHostAddress::Broadcast);
    for (const QNetworkInterface& iface : QNetworkInterface::allInterfaces()) {
        if (!(iface.flags() & QNetworkInterface::IsUp) ||
            !(iface.flags() & QNetworkInterface::IsRunning) ||
            (iface.flags() & QNetworkInterface::IsLoopBack)) {
            continue;
        }
        for (const QNetworkAddressEntry& entry : iface.addressEntries()) {
            const QHostAddress addr = entry.ip();
            const QHostAddress bcast = entry.broadcast();
            if (addr.protocol() == QAbstractSocket::IPv4Protocol && !bcast.isNull()) {
                broadcasts << bcast;
            }
        }
    }

    QUdpSocket socket;
    if (!socket.bind(QHostAddress(QHostAddress::AnyIPv4), 9303, QUdpSocket::ShareAddress | QUdpSocket::ReuseAddressHint) &&
        !socket.bind(QHostAddress(QHostAddress::AnyIPv4), 0, QUdpSocket::ShareAddress | QUdpSocket::ReuseAddressHint)) {
        QJsonObject result;
        result.insert(QStringLiteral("ok"), false);
        result.insert(QStringLiteral("error"), QStringLiteral("Failed to bind UDP socket for discovery"));
        emit helperResult(QStringLiteral("discover"), result);
        emit setupMessage(QStringLiteral("Discovery failed â€” could not bind UDP socket."));
        return;
    }
    socket.setSocketOption(QAbstractSocket::MulticastTtlOption, 1);

    // Send the discovery payload to every broadcast address on both PS5 and PS4 ports.
    for (const QHostAddress& bcast : broadcasts) {
        socket.writeDatagram(discoveryMsg, bcast, 9302);  // PS5
        socket.writeDatagram(discoveryMsg, bcast, 987);   // PS4
    }

    // Pre-load the registered-hosts list (the user's paired PS5/PS4s with their MACs)
    // so we can prefer the actual console they registered over any other device that
    // happens to respond to the broadcast (rare, but happens with dev kits or PS4s).
    const QList<RegisteredHost> registered = readRegisteredHosts();
    QSet<QString> registeredIds;
    for (const auto& h : registered) registeredIds.insert(h.hostIdHex);

    struct Candidate {
        QString ip;
        QString name;
        QString type;
        QString state;
        QString hostId;       // normalised, no separators, uppercase
        QString matchedNick;  // chiaki-registered nickname if MAC matches
        int score = 0;
    };
    QList<Candidate> candidates;
    QSet<QString> seenIps;

    // Seed candidates from Chiaki's saved manual host list. Broadcast discovery can
    // be blocked by Windows firewall, router isolation, or ICS edge cases, but Chiaki
    // already records the console IP after registration/manual setup.
    for (const auto& h : registered) {
        if (h.lastKnownIp.isEmpty() || seenIps.contains(h.lastKnownIp)) {
            continue;
        }
        Candidate c;
        c.ip = h.lastKnownIp;
        c.name = h.nickname;
        c.type = QStringLiteral("PS5");
        c.state = QStringLiteral("Saved");
        c.hostId = h.hostIdHex;
        c.matchedNick = h.nickname;
        c.score = 700;
        if (!c.hostId.isEmpty()) c.score += 50;
        if (!c.matchedNick.isEmpty()) c.score += 100;
        candidates.append(c);
        seenIps.insert(c.ip);
    }

    // Collect responses for ~2 seconds, then score and pick the best.
    QElapsedTimer timer;
    timer.start();
    while (timer.elapsed() < 2000) {
        if (!socket.waitForReadyRead(200)) continue;
        while (socket.hasPendingDatagrams()) {
            QByteArray buf(int(socket.pendingDatagramSize()), 0);
            QHostAddress sender;
            quint16 senderPort = 0;
            const qint64 n = socket.readDatagram(buf.data(), buf.size(), &sender, &senderPort);
            if (n <= 0) continue;
            const QString text = QString::fromUtf8(buf);
            if (!text.contains(QStringLiteral("200 Ok"), Qt::CaseInsensitive) &&
                !text.contains(QStringLiteral("200 OK"))) {
                continue;
            }

            Candidate c;
            c.ip = sender.toString().split(QChar('%')).first(); // strip zone id
            for (const QString& line : text.split(QChar('\n'))) {
                const QString trimmed = line.trimmed();
                if (trimmed.startsWith(QStringLiteral("host-name:"), Qt::CaseInsensitive)) {
                    c.name = trimmed.mid(10).trimmed();
                } else if (trimmed.startsWith(QStringLiteral("host-type:"), Qt::CaseInsensitive)) {
                    c.type = trimmed.mid(10).trimmed();
                } else if (trimmed.startsWith(QStringLiteral("host-state:"), Qt::CaseInsensitive)) {
                    c.state = trimmed.mid(11).trimmed();
                } else if (trimmed.startsWith(QStringLiteral("host-id:"), Qt::CaseInsensitive)) {
                    c.hostId = normalizeHostId(trimmed.mid(8).trimmed());
                }
            }

            // Deduplicate by IP â€” consoles answer once per broadcast interface and we
            // don't want a popular responder padding the score artificially.
            if (seenIps.contains(c.ip)) continue;
            seenIps.insert(c.ip);

            // Score: highest signal is the host-id matching a chiaki-registered MAC,
            // then state=Ready, then PS5 type, then the existence of a host-id field.
            if (!c.hostId.isEmpty() && registeredIds.contains(c.hostId)) {
                c.score += 1000;
                for (const auto& h : registered) {
                    if (h.hostIdHex == c.hostId) {
                        c.matchedNick = h.nickname;
                        break;
                    }
                }
            }
            if (c.state.compare(QStringLiteral("Ready"), Qt::CaseInsensitive) == 0) {
                c.score += 100;
            } else if (c.state.compare(QStringLiteral("Standby"), Qt::CaseInsensitive) == 0) {
                c.score += 50;
            }
            if (c.type.compare(QStringLiteral("PS5"), Qt::CaseInsensitive) == 0) {
                c.score += 20;
            } else if (c.type.compare(QStringLiteral("PS4"), Qt::CaseInsensitive) == 0) {
                c.score += 10;
            }
            if (!c.hostId.isEmpty()) c.score += 5;

            // Prefer the last-known IP from chiaki settings â€” if the console is still
            // on the same address, that's almost certainly the right device.
            for (const auto& h : registered) {
                if (!h.lastKnownIp.isEmpty() && h.lastKnownIp == c.ip) {
                    c.score += 200;
                    break;
                }
            }

            candidates.append(c);
        }
    }

    // Sort candidates by score (descending) and pick the winner.
    std::sort(candidates.begin(), candidates.end(),
              [](const Candidate& a, const Candidate& b) { return a.score > b.score; });

    QJsonArray foundArr;
    for (const Candidate& c : candidates) {
        QJsonObject e;
        e.insert(QStringLiteral("ip"), c.ip);
        e.insert(QStringLiteral("name"), c.name);
        e.insert(QStringLiteral("type"), c.type);
        e.insert(QStringLiteral("state"), c.state);
        e.insert(QStringLiteral("host_id"), c.hostId);
        e.insert(QStringLiteral("matched_nickname"), c.matchedNick);
        e.insert(QStringLiteral("score"), c.score);
        foundArr.append(e);
    }

    QJsonObject result;
    result.insert(QStringLiteral("ok"), !candidates.isEmpty());
    result.insert(QStringLiteral("backend"), QStringLiteral("chiaki-udp"));
    result.insert(QStringLiteral("found"), foundArr);
    result.insert(QStringLiteral("registered_count"), int(registered.size()));

    if (!candidates.isEmpty()) {
        const Candidate& w = candidates.first();
        result.insert(QStringLiteral("ip"), w.ip);
        result.insert(QStringLiteral("name"), w.name);
        result.insert(QStringLiteral("type"), w.type);
        result.insert(QStringLiteral("state"), w.state);
        result.insert(QStringLiteral("host_id"), w.hostId);
        result.insert(QStringLiteral("matched"), !w.matchedNick.isEmpty());
        const QString displayName = !w.matchedNick.isEmpty() ? w.matchedNick
                                  : (!w.name.isEmpty() ? w.name : QStringLiteral("PlayStation"));
        const QString matchTag = !w.matchedNick.isEmpty() ? QStringLiteral(" (registered)") : QString();
        emit setupMessage(QStringLiteral("Discovered %1 \"%2\" at %3%4").arg(
            w.type.isEmpty() ? QStringLiteral("console") : w.type,
            displayName,
            w.ip,
            matchTag));
    } else {
        result.insert(QStringLiteral("error"),
                      QStringLiteral("No PlayStation console responded to discovery. Ensure the console is powered on or in Rest Mode with Remote Play enabled, and that this PC is on the same network."));
        emit setupMessage(QStringLiteral("No PlayStation console found on the network. Power on the console or enable Remote Play in Rest Mode."));
    }
    emit helperResult(QStringLiteral("discover"), result);
}

void RemotePlaySession::requestOauthUrl()
{
    runHelper(QStringLiteral("oauth-url"), {QStringLiteral("oauth-url")});
}

void RemotePlaySession::addProfileFromRedirect(const QString& redirectUrl)
{
    runHelper(QStringLiteral("add-profile"), {
        QStringLiteral("add-profile"),
        QStringLiteral("--redirect-url"), redirectUrl.trimmed()
    });
}

void RemotePlaySession::refreshProfiles()
{
    runHelper(QStringLiteral("profiles"), {QStringLiteral("profiles")});
}

void RemotePlaySession::registerProfile(const QString& user, const QString& pin)
{
    runHelper(QStringLiteral("register"), {
        QStringLiteral("register"),
        QStringLiteral("--host"), config_.remotePlayConsoleIp.trimmed(),
        QStringLiteral("--user"), user.trimmed(),
        QStringLiteral("--pin"), pin.trimmed(),
        QStringLiteral("--timeout"), QStringLiteral("8")
    });
}

void RemotePlaySession::testSession(const QString& user)
{
    runHelper(QStringLiteral("test-session"), {
        QStringLiteral("test-session"),
        QStringLiteral("--host"), config_.remotePlayConsoleIp.trimmed(),
        QStringLiteral("--user"), user.trimmed(),
        QStringLiteral("--resolution"), QStringLiteral("720p"),
        QStringLiteral("--fps"), QStringLiteral("high"),
        QStringLiteral("--codec"), QStringLiteral("h264"),
        QStringLiteral("--timeout"), QStringLiteral("15")
    });
}

void RemotePlaySession::setState(RemotePlayState state, QString status)
{
    state_ = state;
    statusText_ = std::move(status);
    emit stateChanged(state_, statusText_);
}

void RemotePlaySession::emitPlaceholderFrame()
{
    frameCounter_++;

    QImage image(1280, 720, QImage::Format_RGB32);
    image.fill(QColor(5, 5, 9));
    QPainter p(&image);
    p.setRenderHint(QPainter::Antialiasing);
    p.setPen(QPen(QColor(124, 58, 237, 180), 2));
    p.drawRoundedRect(QRectF(48, 48, image.width() - 96, image.height() - 96), 10, 10);
    p.setPen(QColor(248, 250, 252));
    QFont font = p.font();
    font.setPointSize(18);
    font.setWeight(QFont::DemiBold);
    p.setFont(font);
    p.drawText(QRectF(0, 0, image.width(), image.height()), Qt::AlignCenter,
               QStringLiteral("Native Remote Play frame receiver pending"));
    p.setPen(QColor(144, 138, 168));
    font.setPointSize(10);
    font.setWeight(QFont::Normal);
    p.setFont(font);
    p.drawText(QRectF(0, image.height() / 2 + 34, image.width(), 40), Qt::AlignCenter,
               QStringLiteral("Desktop capture is disabled in the native pipeline"));
    p.end();

    telemetry_.inboundPackets += 1;
    if ((frameCounter_ % 4) == 0) {
        telemetry_.outboundPackets += 1;
    }
    if (telemetry_.consoleIp.isEmpty() && !config_.remotePlayConsoleIp.isEmpty()) {
        telemetry_.consoleIp = config_.remotePlayConsoleIp;
    }
    if (state_ == RemotePlayState::Running) {
        telemetry_.syncActive = true;
    }
    emit telemetryReady(telemetry_);
    // Placeholder frame carries no decoder id -> -1 so the overlay never tries to join a bbox to it.
    emit frameReady(image, -1);
}

void RemotePlaySession::runHelper(const QString& operation, const QStringList& args)
{
    if (helperProcess_ && helperProcess_->state() != QProcess::NotRunning) {
        emit setupMessage(QStringLiteral("Remote Play helper is already busy."));
        return;
    }

    const QString py = pythonExecutable();
    const QString helper = helperPath();
    if (py.isEmpty() || helper.isEmpty()) {
        QJsonObject result;
        result.insert(QStringLiteral("ok"), false);
        result.insert(QStringLiteral("error"), QStringLiteral("PS5 helper or Python runtime was not found."));
        emit helperResult(operation, result);
        emit setupMessage(result.value(QStringLiteral("error")).toString());
        return;
    }

    auto* proc = new QProcess(this);
    helperProcess_ = proc;
    proc->setProgram(py);
    QStringList fullArgs;
    const QString root = rootDir_.isEmpty()
                             ? QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Desktop/NexusVision"))
                             : QDir::toNativeSeparators(rootDir_);
    fullArgs << helper << QStringLiteral("--root") << root;
    fullArgs << args;
    proc->setArguments(fullArgs);
    proc->setProcessChannelMode(QProcess::SeparateChannels);
#ifdef Q_OS_WIN
    proc->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *cpargs) {
        cpargs->flags |= 0x08000000;  // CREATE_NO_WINDOW — hide the setup-helper console
    });
#endif

    connect(proc, &QProcess::finished, this, [this, proc, operation](int exitCode, QProcess::ExitStatus exitStatus) {
        const QByteArray stdoutData = proc->readAllStandardOutput().trimmed();
        const QByteArray stderrData = proc->readAllStandardError().trimmed();
        QJsonObject result;
        const auto doc = QJsonDocument::fromJson(stdoutData);
        if (doc.isObject()) {
            result = doc.object();
        } else {
            result.insert(QStringLiteral("ok"), false);
            result.insert(QStringLiteral("error"), QStringLiteral("Helper returned invalid JSON."));
            result.insert(QStringLiteral("stdout"), QString::fromUtf8(stdoutData.left(500)));
        }
        result.insert(QStringLiteral("exit_code"), exitCode);
        result.insert(QStringLiteral("exit_status"), exitStatus == QProcess::NormalExit ? QStringLiteral("normal") : QStringLiteral("crashed"));
        if (!stderrData.isEmpty()) {
            result.insert(QStringLiteral("stderr"), QString::fromUtf8(stderrData.left(1000)));
        }

        if (operation == QLatin1String("test-session")) {
            if (result.value(QStringLiteral("ok")).toBool(false)) {
                setState(RemotePlayState::Running, QStringLiteral("PS5 session test passed; frame receiver boundary active"));
                frameTimer_.start();
            } else {
                setState(RemotePlayState::Error, result.value(QStringLiteral("error")).toString(QStringLiteral("PS5 session test failed")));
            }
        }

        helperProcess_ = nullptr;
        emit helperResult(operation, result);
        emit setupMessage(result.value(QStringLiteral("ok")).toBool(false)
                              ? QStringLiteral("PS5 helper command completed: %1").arg(operation)
                              : result.value(QStringLiteral("error")).toString(QStringLiteral("PS5 helper command failed.")));
        proc->deleteLater();
    });
    proc->start();
}

QString RemotePlaySession::pythonExecutable() const
{
    const QString env = QString::fromLocal8Bit(qgetenv("ORION_PYREMOTEPLAY_PYTHON")).trimmed();
    const QString root = rootDir_.isEmpty()
                             ? QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Desktop/NexusVision"))
                             : QDir::toNativeSeparators(rootDir_);
    const QStringList candidates = {
        env,
        QDir::toNativeSeparators(root + QStringLiteral("/.venv311/Scripts/python.exe")),
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/python/python.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Desktop/NexusVision/.venv311/Scripts/python.exe")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Miniconda3/envs/cosmic_env/python.exe")),
        QStringLiteral("python.exe")
    };
    // Prefer an interpreter that can actually import cv2 (the orchestrator's core CV dep).
    // A freshly created but UNPOPULATED venv (e.g. one made for the pose deps before its
    // requirements are installed) exists on disk but can't run the sidecar — selecting it by
    // mere existence breaks detection with "No module named 'cv2'". Probe each candidate so we
    // skip an unusable env, fall back to a working one, and auto-adopt the venv once it has cv2.
    auto canImportCv2 = [](const QString& py) -> bool {
        QProcess probe;
        probe.setProgram(py);
        probe.setArguments({QStringLiteral("-c"), QStringLiteral("import cv2")});
        probe.setStandardOutputFile(QProcess::nullDevice());
        probe.setStandardErrorFile(QProcess::nullDevice());
#ifdef Q_OS_WIN
        probe.setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *cpargs) {
            cpargs->flags |= 0x08000000;  // CREATE_NO_WINDOW — hide the cv2 probe console
        });
#endif
        probe.start();
        if (!probe.waitForFinished(8000)) {
            probe.kill();
            probe.waitForFinished(1000);
            return false;
        }
        return probe.exitStatus() == QProcess::NormalExit && probe.exitCode() == 0;
    };

    QString firstExisting;
    for (const auto& candidate : candidates) {
        if (candidate.isEmpty()) {
            continue;
        }
        const bool exists = candidate == QLatin1String("python.exe") || QFileInfo::exists(candidate);
        if (!exists) {
            continue;
        }
        if (firstExisting.isEmpty()) {
            firstExisting = candidate;
        }
        // The explicit ORION_PYREMOTEPLAY_PYTHON override is trusted as-is; every auto-
        // discovered candidate must prove it can import cv2 before we commit to it.
        if (candidate == env || canImportCv2(candidate)) {
            return candidate;
        }
    }
    // Nothing could import cv2 — fall back to the first interpreter that at least exists (no
    // worse than the legacy behaviour; the launch will then surface the real import error).
    return firstExisting;
}

QString RemotePlaySession::helperPath() const
{
    const QStringList candidates = {
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/backend/ps5_remoteplay_helper.py")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Desktop/NexusVision/native_orion/backend/ps5_remoteplay_helper.py")),
        QDir::toNativeSeparators(QFileInfo(__FILE__).absolutePath() + QStringLiteral("/../backend/ps5_remoteplay_helper.py"))
    };
    for (const auto& candidate : candidates) {
        if (QFileInfo::exists(candidate)) {
            return candidate;
        }
    }
    return {};
}

QString RemotePlaySession::sidecarScriptPath() const
{
    const QStringList candidates = {
        QDir::toNativeSeparators(QCoreApplication::applicationDirPath() + QStringLiteral("/backend/autogreen_sidecar.py")),
        QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Desktop/NexusVision/native_orion/backend/autogreen_sidecar.py")),
        QDir::toNativeSeparators(QFileInfo(__FILE__).absolutePath() + QStringLiteral("/../backend/autogreen_sidecar.py"))
    };
    for (const auto& candidate : candidates) {
        if (QFileInfo::exists(candidate)) {
            return candidate;
        }
    }
    return {};
}

QByteArray RemotePlaySession::buildSidecarConfig() const
{
    const QString chiakiPath = resolveChiakiPath(config_, rootDir_);
    // Bind the Python pipe reader to the exact image native selected and hashed.
    // Size is transported as a canonical decimal string so the JSON boundary can
    // never round a future 64-bit value.  Missing identity leaves decoder timing
    // cold/unscoped; it is never substituted with path metadata alone.
    QString windowTitle = config_.remotePlayWindowTitle.trimmed();
    if (windowTitle.isEmpty() || windowTitle.compare(QStringLiteral("Chiaki"), Qt::CaseInsensitive) == 0) {
        // The Orion fork's stream window is titled by applicationDisplayName.
        if (chiakiPath.contains(QStringLiteral("OrionStream"), Qt::CaseInsensitive)) {
            windowTitle = QStringLiteral("Orion Stream");
        } else {
            windowTitle = chiakiPath.contains(QStringLiteral("chiaki-ng"), Qt::CaseInsensitive)
                ? QStringLiteral("chiaki-ng")
                : QStringLiteral("Chiaki");
        }
    }

    QJsonObject obj;
    obj.insert(QStringLiteral("console_ip"), config_.remotePlayConsoleIp.trimmed());
    obj.insert(QStringLiteral("console_identity"),
               registeredConsoleRouteIdentity(config_.remotePlayConsoleIp));
    obj.insert(QStringLiteral("platform"), QStringLiteral("ps5"));
    obj.insert(QStringLiteral("client_mode"), QStringLiteral("chiaki"));
    // Preview mode opens the capture card only (no Chiaki / Remote Play / input hook) so the panel
    // shows the HDMI feed before Connect; the full session launches Chiaki. See AppConfig.h. The
    // orchestrator already honours auto_launch_client=false and, in capture-card mode, brings the
    // Elgato up independently of any Chiaki window.
    obj.insert(QStringLiteral("auto_launch_client"), sidecarShouldAutoLaunchClient(previewMode_));
    obj.insert(QStringLiteral("close_client_on_disconnect"), true);
    obj.insert(QStringLiteral("chiaki_path"), chiakiPath);
    const bool decoderProducerIdentityReady =
        remote_play_executable_policy::insertDecoderPipeProducerExpectation(obj, chiakiPath);
    if (!decoderProducerIdentityReady) {
        // Do not expose a path/hash here. Python receives explicit empty fields
        // and permanently keeps decoder timing cold/unscoped for this process.
        qWarning("Remote Play decoder producer identity unavailable; reusable timing disabled");
    }
    obj.insert(QStringLiteral("window_title"), windowTitle);
    // Current-session readiness budget for ensure_running(). Authority is now the fresh Chiaki
    // streaminfo marker, not the much later HWND, so the historical 40s window wait only prolonged
    // an unreachable-console failure through eight 5s retries. Python must return/reap before the
    // native 20s hard deadline so a late successful child cannot outlive a rejected verdict.
    obj.insert(QStringLiteral("wait_timeout_s"), 15.0);
    const StreamPreset preset = streamPresetForMode(bandwidthModeFromConfig(config_));
    obj.insert(QStringLiteral("resolution"), preset.sidecarResolution);
    const int targetFps = preset.fps.toInt();
    obj.insert(QStringLiteral("target_fps"), targetFps);
    // Carry the selected capture route in the config as well as the process
    // environment.  The latency estimator deliberately scopes its posterior by
    // ``frame_source``; leaving the dataclass default at "auto" made both native
    // capture-card and no-card decoder sessions cold-start calibration on every
    // launch even though their fixed pipelines were identifiable.  AppConfig
    // already normalizes this value to exactly capture_card or decoder.
    obj.insert(QStringLiteral("frame_source"), config_.videoSource);
    obj.insert(QStringLiteral("preview_fps"), preset.previewFps);
    obj.insert(QStringLiteral("preview_width"), preset.previewWidth);
    obj.insert(QStringLiteral("show_video"), true);
    obj.insert(QStringLiteral("audio_enabled"), config_.streamAudioEnabled);
    obj.insert(QStringLiteral("virtual_controller"), false);
    obj.insert(QStringLiteral("hidhide"), false);
    obj.insert(QStringLiteral("goto_shot"), true);
    // Send the user's CONFIGURED meter colour to the detector. The old `autoMeterColor ? "Purple"`
    // override was a 2026-06 workaround for a stint when the meter was purple but meter_color was a
    // stale "Red"; it is now THE bug — the user's meter is RED, and a forced "Purple" mask (hue
    // 138-162) matches ZERO red pixels (red wraps 0/179), so the detector cannot see the meter at
    // all (the live "locks onto nothing / waiting for meter"). Honour the explicit colour; fall back
    // to Red (the live meter), not Purple. TODO: true colour auto-detect (sample the meter region).
    const QString sidecarMeterColor =
        config_.meterColor.isEmpty() ? QStringLiteral("Red") : config_.meterColor;
    obj.insert(QStringLiteral("meter_color"), sidecarMeterColor);
    obj.insert(QStringLiteral("meter_style"), config_.meterStyle);
    obj.insert(QStringLiteral("confidence_gate"), config_.detectionConfidencePercent / 100.0);
    obj.insert(QStringLiteral("green_window_start_pct"), config_.releaseThresholdPct > 0.0 ? config_.releaseThresholdPct : 93.0);
    obj.insert(QStringLiteral("green_window_end_pct"), 100.0);
    obj.insert(QStringLiteral("timing_delay_ms"), config_.earlyLateOffsetMs);
    obj.insert(QStringLiteral("latency_compensation_ms"), config_.latencyCompensationMs);
    obj.insert(QStringLiteral("stable_frames_required"), config_.stableFrames);
    obj.insert(QStringLiteral("tempo_flick_hold_ms"), config_.tempoFlickHoldMs);
    obj.insert(QStringLiteral("tempo_remap_type"), config_.tempoRemapType);
    QString sidecarInputMode = QStringLiteral("square_only");
    if (config_.remotePlayInputSource == QLatin1String("stick")) {
        sidecarInputMode = QStringLiteral("stick_only");
    } else if (config_.remotePlayInputSource == QLatin1String("both")) {
        sidecarInputMode = QStringLiteral("both");
    }
    obj.insert(QStringLiteral("input_mode"), sidecarInputMode);
    obj.insert(QStringLiteral("no_meter_enabled"), config_.noMeterEnabled);
    obj.insert(QStringLiteral("no_meter_release_point"), config_.noMeterReleasePoint);
    obj.insert(QStringLiteral("no_meter_base_offset_ms"), config_.noMeterBaseOffsetMs);
    obj.insert(QStringLiteral("decode_latency_ms"), config_.noMeterDecodeCompMs);
    obj.insert(QStringLiteral("no_meter_confidence_gate"), config_.noMeterConfidenceGate);
    obj.insert(QStringLiteral("no_meter_push_release_window_ms"), config_.noMeterPushReleaseWindowMs);
    obj.insert(QStringLiteral("no_meter_handedness"), config_.noMeterHandedness);
    // Hold-square autogreen: tempo/stick remap is disabled; the engine uses the
    // 'button' trigger (hold = autogreen, taps suppressed) and the active shot
    // type's fixed offset as additional release lead.
    obj.insert(QStringLiteral("shot_trigger_mode"), QStringLiteral("button"));
    obj.insert(QStringLiteral("active_shot_type"), config_.activeShotType);
    {
        QJsonObject sto;
        for (auto it = config_.shotTypeOffsets.constBegin(); it != config_.shotTypeOffsets.constEnd(); ++it) {
            sto.insert(it.key(), it.value());
        }
        obj.insert(QStringLiteral("shot_type_offsets"), sto);
    }
    return QJsonDocument(obj).toJson(QJsonDocument::Compact);
}

void RemotePlaySession::promoteWarmPreviewToStream()
{
    const QString remotePlayExecutable = resolveChiakiPath(config_, rootDir_);
    if (remotePlayExecutable.isEmpty()
        || !reportRemotePlayExecutableIdentity(
            remotePlayExecutable, QStringLiteral("preview-promotion"))) {
        rejectLateSidecarStarted_ = true;
        setState(RemotePlayState::Error,
                 QStringLiteral("Stream promotion blocked: trusted Remote Play client image unavailable"));
        restoreWarmPreviewAfterPromotionFailure();
        return;
    }

    // Command the already-running warm preview sidecar to bring up Chiaki/input in place. The
    // sidecar keeps its capture-card feed + detector running (no teardown, no device re-open), so
    // this is the whole ~5-6s connect saving. Mirrors the update_remap hot-apply stdin pattern.
    //   {"cmd":"start_stream","console_ip":"<ip>","console_identity":"<opaque digest>"}
    // The sidecar already holds the rest of its launch config (--config-json) and reads Chiaki's
    // bandwidth/codec from the QSettings connectRemotePlay wrote via applyBandwidthMode just before
    // this, so only the (possibly rediscovered) console IP needs to ride along.
    const quint64 generation = ++streamPromoteGeneration_;
    streamPromotePending_ = true;
    streamPromoteAcked_ = false;
    rejectLateSidecarStarted_ = false;

    QJsonObject cmd;
    cmd.insert(QStringLiteral("cmd"), QStringLiteral("start_stream"));
    cmd.insert(QStringLiteral("console_ip"), config_.remotePlayConsoleIp.trimmed());
    cmd.insert(QStringLiteral("console_identity"),
               registeredConsoleRouteIdentity(config_.remotePlayConsoleIp));
    // FIX 1: a command written into a dead pipe used to be indistinguishable from one that ran —
    // and the fallback timer below then reported "Autogreen running" over a stream that was never
    // even asked to start. Fail loudly instead.
    if (!sendSidecarCommand(cmd)) {
        streamPromotePending_ = false;
        emit setupMessage(QStringLiteral("Stream promotion aborted: the detection sidecar is not "
                                         "running, so start_stream was never delivered."));
        setState(RemotePlayState::Error,
                 QStringLiteral("Stream start failed: detection sidecar is not running"));
        restoreWarmPreviewAfterPromotionFailure();
        return;
    }
    // Push current tuning so the promoted stream carries the live meter/remap config (the same
    // values a fresh --config-json launch would have baked in).
    pushRemapUpdate();

    // Protocol grace only. A legacy sidecar that never acknowledges current-session
    // readiness is not allowed to inherit the old unconditional Running fallback.
    // Missing evidence must fail closed just like an explicit false verdict.
    QTimer::singleShot(kStreamPromoteFallbackMs, this, [this, generation]() {
        if (generation != streamPromoteGeneration_ || state_ != RemotePlayState::Connecting) {
            return;   // superseded by a newer connect, or the verdict already moved us
        }
        if (streamPromoteAcked_) {
            // The sidecar told us it is working on it; wait for the real result rather than
            // claiming a running bot we cannot vouch for.
            setState(RemotePlayState::Connecting,
                     QStringLiteral("Starting Chiaki input link..."));
            return;
        }
        streamPromotePending_ = false;
        rejectLateSidecarStarted_ = true;
        setState(RemotePlayState::Error,
                 QStringLiteral("Stream sidecar did not acknowledge current console-session "
                                "readiness; automation remains disabled."));
        restoreWarmPreviewAfterPromotionFailure();
    });

    // Hard deadline for an ACKed promotion that never reported started/error (sidecar wedged inside
    // the client launch, stdout stalled). Reporting the truth beats leaving the user on a
    // "Connecting" that will never resolve — and, critically, beats the old fake "running".
    QTimer::singleShot(kStreamPromoteDeadlineMs, this, [this, generation]() {
        if (generation != streamPromoteGeneration_ || !streamPromotePending_) {
            return;
        }
        if (state_ != RemotePlayState::Connecting) {
            return;
        }
        streamPromotePending_ = false;
        rejectLateSidecarStarted_ = true;
        emit setupMessage(QStringLiteral("Stream promotion timed out after %1 ms with no "
                                         "started/error from the sidecar.").arg(kStreamPromoteDeadlineMs));
        setState(RemotePlayState::Error,
                 QStringLiteral("Stream start did not confirm - no input link to the console. "
                                "Disconnect and try again."));
        restoreWarmPreviewAfterPromotionFailure();
    });
}

void RemotePlaySession::restoreWarmPreviewAfterPromotionFailure()
{
    const bool sidecarRunning = sidecarProcess_ != nullptr
        && sidecarProcess_->state() != QProcess::NotRunning;
    const bool restore = shouldRestoreWarmPreviewAfterPromotionFailure(
        streamPromoteFromWarmPreview_, sidecarRunning, isCaptureCardSource(config_));
    streamPromoteFromWarmPreview_ = false;
    if (!restore) {
        return;
    }

    // State remains Error so the failed console route is explicit and automation stays disarmed.
    // Only the already-live HDMI capture/detector ownership is restored; the Python failure path
    // separately stops and reaps the unproven Chiaki/input child.
    previewMode_ = true;
    emit previewActiveChanged(true);
    emit setupMessage(QStringLiteral("Stream input promotion failed; live capture-card preview and "
                                     "meter detection remain active."));
}

void RemotePlaySession::startSidecar()
{
    if (sidecarProcess_ && sidecarProcess_->state() != QProcess::NotRunning) {
        // Never silent: callers (start(), the deferred handoff/watchdog timers) set Connecting
        // FIRST and then call this, so a swallowed no-op here leaves the UI stuck on "Connecting"
        // with nothing actually starting and no line anywhere saying why.
        emit setupMessage(QStringLiteral("Sidecar start skipped: a detection sidecar (pid %1) is "
                                         "already running.").arg(sidecarProcess_->processId()));
        return;
    }

    // A full sidecar launch will immediately spawn the Remote Play client. Pin
    // and identify that image before any cleanup/process mutation. Capture-card
    // warm preview deliberately skips this because it does not launch Chiaki.
    if (sidecarShouldAutoLaunchClient(previewMode_)) {
        const QString remotePlayExecutable = resolveChiakiPath(config_, rootDir_);
        if (remotePlayExecutable.isEmpty()) {
            setState(RemotePlayState::Error,
                     kProductionBuild
                         ? QStringLiteral("Production package is incomplete: bundled OrionStream.exe not found")
                         : QStringLiteral("Chiaki executable was not found"));
            return;
        }
        if (!reportRemotePlayExecutableIdentity(
                remotePlayExecutable, QStringLiteral("sidecar-launch"))) {
            setState(RemotePlayState::Error,
                     QStringLiteral("Remote Play client identity could not be verified; launch blocked"));
            return;
        }
    }

#ifdef Q_OS_WIN
    // Reap an orphaned sidecar from a PREVIOUS OrionNative that was force-killed (taskkill /F,
    // crash). Such a zombie python.exe still holds the capture card open via OpenCV VideoCapture
    // and the new sidecar then cannot open the device. Filter on the "ActiveMovie Window" title:
    // DirectShow/Media Foundation creates it exactly when a video device is opened, so this
    // targets a python process holding a CAPTURE DEVICE and nothing else.
    //
    // Deliberately NOT matched here: "OleMainThreadWndName" (that hidden window exists in ANY
    // COM-initialising Python process, so filtering on it would kill unrelated user python
    // processes) and a wmic command-line sweep (wmic.exe is removed in current Windows 11 builds,
    // so it is a no-op that only blocks the GUI thread). Orphans created from NOW on are handled
    // structurally by the KILL_ON_JOB_CLOSE job object in assignSidecarToJob().
    QProcess::execute(QStringLiteral("taskkill.exe"),
                      {QStringLiteral("/FI"), QStringLiteral("WINDOWTITLE eq ActiveMovie Window"),
                       QStringLiteral("/IM"), QStringLiteral("python.exe"),
                       QStringLiteral("/F"), QStringLiteral("/T")});
#endif

    // Production packages deliberately omit the crown-jewel Python modules, so a production
    // build MUST launch the compiled sidecar regardless of attacker/user-controlled environment.
    // If OrionSidecar.exe is absent, the existing check below fails closed; there is no Python
    // fallback. Development builds retain the historical ORION_COMPILED_SIDECAR opt-in so source
    // iteration remains unchanged.
#ifdef ORION_PRODUCTION_BUILD
    constexpr bool productionBuild = true;
#else
    constexpr bool productionBuild = false;
#endif
    const bool useCompiledSidecar = shouldUseCompiledSidecar(
        productionBuild, qEnvironmentVariableIsSet("ORION_COMPILED_SIDECAR"));

    // Interpreter/script resolution stays intact for the default path. Skip it on the compiled
    // path (the cv2-probe in pythonExecutable() would be pure waste there).
    const QString py = useCompiledSidecar ? QString() : pythonExecutable();
    const QString script = useCompiledSidecar ? QString() : sidecarScriptPath();
    QString compiledExe;
    if (useCompiledSidecar) {
        compiledExe = QDir::toNativeSeparators(
            QCoreApplication::applicationDirPath() + QStringLiteral("/OrionSidecar.exe"));
        if (!QFileInfo::exists(compiledExe)) {
            setState(RemotePlayState::Error,
                     productionBuild
                         ? QStringLiteral("Production package is incomplete: required OrionSidecar.exe not found")
                         : QStringLiteral("Compiled sidecar OrionSidecar.exe not found"));
            return;
        }
    } else if (py.isEmpty() || script.isEmpty()) {
        setState(RemotePlayState::Error, QStringLiteral("Autogreen sidecar script or Python runtime not found"));
        return;
    }

    auto* proc = new QProcess(this);
    sidecarProcess_ = proc;
    // A fresh sidecar starts a fresh decoder-frame namespace. No image from the
    // previous process may survive into this presentation queue.
    resetPreviewPresentation(true);
    sidecarBuffer_.clear();
    previewChunks_.reset();
    beginShmSourceEpoch();
    shmOpenFailures_ = 0;
    shmReadFailures_ = 0;
    shmFramesRead_ = 0;
    lastShmFramesReadCount_ = 0;
    shmFallbackRequested_ = false;
    jpegFallbackFirstFrameNumber_ = 0;
    sidecarStderrTail_.clear();
    // [ORION_AUTHORITY_DEATH 2026-08-10] The revocation it announces is scoped to the sidecar
    // process, so a brand-new sidecar has earned a fresh announcement.
    authorityDeathAnnounced_ = false;
    // FIX 1: a brand-new sidecar has no promotion in flight. Bumping the generation invalidates any
    // deferred fallback/deadline timer still armed from a previous session so it cannot act here.
    ++streamPromoteGeneration_;
    streamPromotePending_ = false;
    streamPromoteAcked_ = false;
    streamPromoteFromWarmPreview_ = false;
    rejectLateSidecarStarted_ = false;
    ++inputRecoveryGeneration_;
    inputRecoveryPending_ = false;
    rejectLateInputRecoveryReady_ = false;
    // FIX 3: the drop counter is per-sidecar-process and monotonic within it. Reset with the
    // process so the fresh sidecar's first real drop is reported instead of being hidden under a
    // dead process's total.
    previewDroppedFrames_ = 0;
    lastPreviewDropLogMs_ = 0;
    lastPreviewPipelineStatsMs_ = 0;
    if (frameDecoder_) {
        const FrameDecoderStats stats = frameDecoder_->stats();
        lastPreviewSubmittedCount_ = stats.submitted;
        lastPreviewDecodedCount_ = stats.decoded;
        lastPreviewPresentedCount_ = stats.presented;
        lastPreviewMailboxDropCount_ = stats.decodeMailboxDropped;
        lastPreviewPresentationDropCount_ = stats.presentationMailboxDropped;
    }

    const QString root = rootDir_.isEmpty()
                             ? QDir::toNativeSeparators(QDir::homePath() + QStringLiteral("/Desktop/NexusVision"))
                             : QDir::toNativeSeparators(rootDir_);
    const QByteArray cfgJson = buildSidecarConfig();

    if (useCompiledSidecar) {
        // Compiled path: program is the exe, args drop the interpreter + leading script.
        proc->setProgram(compiledExe);
        proc->setArguments({
            QStringLiteral("--root"), root,
            QStringLiteral("--config-json"), QString::fromUtf8(cfgJson)
        });
    } else {
        proc->setProgram(py);
        proc->setArguments({
            script,
            QStringLiteral("--root"), root,
            QStringLiteral("--config-json"), QString::fromUtf8(cfgJson)
        });
    }
    proc->setProcessChannelMode(QProcess::SeparateChannels);
#ifdef Q_OS_WIN
    // Keep the sidecar's console HIDDEN. The sidecar runs under console Python, so without this
    // a black console window (the "worker" the user sees) pops up on every launch. CREATE_NO_WINDOW
    // (0x08000000) suppresses the console; the stdout/stderr pipes the app reads are unaffected.
    proc->setCreateProcessArgumentsModifier([](QProcess::CreateProcessArguments *cpargs) {
        cpargs->flags |= 0x08000000;  // CREATE_NO_WINDOW
    });
    QProcessEnvironment env = QProcessEnvironment::systemEnvironment();
    const bool captureCardSource = isCaptureCardSource(config_);
    const bool requireRemotePlayFramePipe = shouldRequireRemotePlayFramePipe(
        productionBuild, captureCardSource);
    const auto envFlagEnabled = [&env](const QString &name) {
        const QString value = env.value(name).trimmed().toLower();
        return !value.isEmpty()
            && value != QLatin1String("0")
            && value != QLatin1String("false")
            && value != QLatin1String("no")
            && value != QLatin1String("off");
    };
    const bool developmentFramePipeRequested =
        envFlagEnabled(QStringLiteral("ORION_FRAME_PIPE"))
        || envFlagEnabled(QStringLiteral("CHIAKI_ORION_FRAME_PIPE"));
    const bool frameExportEnabled = shouldEnableRemotePlayFrameExport(
        captureCardSource, requireRemotePlayFramePipe, developmentFramePipeRequested);
    const auto removeFrameExportEnvironment = [&env]() {
        env.remove(QStringLiteral("ORION_FRAME_PIPE"));
        env.remove(QStringLiteral("ORION_REQUIRE_FRAME_PIPE"));
        env.remove(QStringLiteral("ORION_FRAME_PIPE_READY_TIMEOUT_S"));
        env.remove(QStringLiteral("CHIAKI_ORION_FRAME_PIPE"));
        env.remove(QStringLiteral("CHIAKI_ORION_FRAME_FPS"));
    };
    // Force the proven SimpleMeterReader (2k_Vision/Starzen pure-CV approach) on ALL paths.
    // The legacy 5100-LOC MeterDetector chain (YOLO locator + park temporal + box-track Kalman)
    // is RETIRED — SimpleMeterReader matches it on accuracy at 1% of the code with zero decor
    // false-locks. Previously this was only set on the compiled sidecar path, leaving dev mode
    // running the retired chain (the meter detection regression).
    env.insert(QStringLiteral("ORION_SIMPLE_READER"), QStringLiteral("1"));
    // Lifecycle, telemetry, preview stats, and legacy frame_shm fallback remain
    // unbuffered. Negotiated SHM frames use the named event below and therefore
    // never contend with detector telemetry on stdout in the healthy path.
    env.insert(QStringLiteral("PYTHONUNBUFFERED"), QStringLiteral("1"));
    // Only a native build with the frame_shm reader opts the sidecar into SHM.
    // Preserve an explicit diagnostic opt-out; older native binaries never set
    // this key and automatically remain on the JPEG-compatible path.
    if (!env.contains(QStringLiteral("ORION_PREVIEW_SHM"))) {
        env.insert(QStringLiteral("ORION_PREVIEW_SHM"), QStringLiteral("1"));
    }
    if (shmSourceActive_ && shmTransportNames_.eventNotificationsEnabled()) {
        env.insert(QStringLiteral("ORION_PREVIEW_SHM_MAPPING"),
                   shmTransportNames_.mappingName);
        env.insert(QStringLiteral("ORION_PREVIEW_SHM_MUTEX"),
                   shmTransportNames_.mutexName);
        env.insert(QStringLiteral("ORION_PREVIEW_SHM_READY_EVENT"),
                   shmTransportNames_.readyEventName);
    }
    if (qEnvironmentVariableIsSet("ORION_INPUT_HOOK")) {
        env.insert(QStringLiteral("ORION_INPUT_HOOK"), qEnvironmentVariable("ORION_INPUT_HOOK"));
    }
    if (requireRemotePlayFramePipe) {
        // Compile-time production policy: the decoder pipe is the bot's only
        // authoritative no-card eye. Ignore inherited dev overrides and pin both
        // ends to the known private pipe; Python is also told window/WGC fallback
        // is forbidden. Capture-card selection never enters this branch.
        env.insert(QStringLiteral("ORION_FRAME_PIPE"), QStringLiteral("1"));
        env.insert(QStringLiteral("ORION_REQUIRE_FRAME_PIPE"), QStringLiteral("1"));
        env.insert(QStringLiteral("CHIAKI_ORION_FRAME_PIPE"),
                   QStringLiteral(R"(\\.\pipe\orion_frames)"));
        env.remove(QStringLiteral("ORION_CAPTURE_CARD"));
        env.remove(QStringLiteral("ORION_CAPTURE_CARD_INDEX"));
        env.remove(QStringLiteral("ORION_VIDEO_DEVICE_NAMES"));
        env.remove(QStringLiteral("ORION_VIDEO_DEVICE_IDS"));
        env.remove(QStringLiteral("ORION_WGC"));
        env.remove(QStringLiteral("ORION_WGC_MONITOR"));
    } else if (frameExportEnabled) {
        // Development may opt into the decoder pipe from either end of the
        // environment contract. Normalize the orchestrator flag so both the
        // Python client and OrionStream agree that frame export is active.
        env.insert(QStringLiteral("ORION_FRAME_PIPE"), QStringLiteral("1"));
    } else {
        removeFrameExportEnvironment();
    }
    if (qEnvironmentVariableIsSet("ORION_QML_RENDER")) {
        env.insert(QStringLiteral("ORION_QML_RENDER"), qEnvironmentVariable("ORION_QML_RENDER"));
    }
    // Export FPS cap: MUST sit ABOVE the 60fps source or it aliases. At exactly "60", normal
    // decode jitter (+-1-2ms) lands ~40% of frames just under the 16.67ms export gate, which
    // drops them -> ~37fps delivered (the "no 60fps" symptom; the ceiling was identical for
    // 1080p-software and 720p-d3d11va = a fixed-rate gate, not decode throughput). "120" leaves
    // headroom so every 60fps frame clears the gate. orionframeexport.cpp reads this env at
    // runtime, so this needs only a native rebuild (no OrionStream rebuild).
    if (frameExportEnabled) {
        env.insert(QStringLiteral("CHIAKI_ORION_FRAME_FPS"),
                   QString::number(kRemotePlayFrameExportGateFps));
    }
    // DETDIAG is opt-in: pass ORION_DETDIAG (and its interval) through to the Python
    // sidecar ONLY when explicitly truthy in the parent env, so a measurement launch
    // (ORION_DETDIAG=1) reaches remote_play_orchestrator.py. Normal launches stay
    // quiet — a missing/falsy value is stripped so nothing leaks and DETDIAG is never
    // on by default.
    const QString detdiag = env.value(QStringLiteral("ORION_DETDIAG")).trimmed().toLower();
    const bool detdiagOn = (detdiag == QLatin1String("1")
                            || detdiag == QLatin1String("true")
                            || detdiag == QLatin1String("yes"));
    if (!detdiagOn) {
        env.remove(QStringLiteral("ORION_DETDIAG"));
        env.remove(QStringLiteral("ORION_DETDIAG_INTERVAL"));
    }
    env.insert(QStringLiteral("SDL_GAMECONTROLLER_IGNORE_DEVICES"),
               QStringLiteral("0x054c/0x0ce6,0x054c/0x05c4,0x054c/0x09cc,0x054c/0x0df2,0x054c/0x0e5f"));
    env.insert(QStringLiteral("SDL_JOYSTICK_HIDAPI_PS5"), QStringLiteral("0"));
    env.insert(QStringLiteral("SDL_JOYSTICK_HIDAPI_PS4"), QStringLiteral("0"));
    // Capture-card detection source: when selected, the autogreen sidecar's orchestrator
    // reads the HDMI device (CaptureCardBackend) instead of the decoder pipe — a clean
    // low-latency 1080p feed. Gated env so a normal launch stays on the decoder pipe.
    if (captureCardSource) {
        // Defense in depth: a capture-card launch uses OrionStream for input
        // only. Strip every inherited decoder-export knob so the producer never
        // performs costly GPU readback or opens an unused frame pipe.
        removeFrameExportEnvironment();
        env.insert(QStringLiteral("ORION_CAPTURE_CARD"), QStringLiteral("1"));
        env.insert(QStringLiteral("ORION_CAPTURE_CARD_INDEX"), QString::number(config_.captureCardIndex));
        // Never let a caller-inherited identity survive a failed/empty native
        // enumeration. Only this launch's atomic inventory may authorize reuse.
        env.remove(QStringLiteral("ORION_VIDEO_DEVICE_NAMES"));
        env.remove(QStringLiteral("ORION_VIDEO_DEVICE_IDS"));
        // Webcam protection: hand the sidecar the index-ordered device NAME list so its card
        // auto-detect probes only capture-card-looking devices. A fresh enumeration also exports
        // an opaque, same-index stable ID list for route-scoped latency-cache validation.
        //
        // Track W: bound this enumeration so a contended Elgato (right after a watchdog restart)
        // can NEVER wedge the GUI thread into SAFE MODE. The DirectShow ICreateDevEnum call can
        // block 10-15s on a device the just-killed sidecar has not released; runBoundedEnumeration
        // runs it on a detached worker and, past kVideoEnumTimeoutMs, reuses the last-good cached
        // names instead of blocking. Cached IDs are stripped on that fallback because a same-model
        // card could have been swapped at the same index; only a fresh result exports identity.
        const VideoInputDeviceInventory videoDevices = runBoundedEnumeration(
            kVideoEnumTimeoutMs, cachedVideoDeviceInventory_.namesOnlyFallback(),
            []() { return enumerateVideoInputDevices(); });
        if (insertVideoInputDeviceEnvironment(env, videoDevices)) {
            // Store exactly what this attempt trusted. On timeout that is a
            // name-only snapshot, so stale identity is erased from memory too.
            cachedVideoDeviceInventory_ = videoDevices;
        }
    }
    // Deterministic ground truth of which ORION_* variables the child sidecar receives.
    // Never log their values: launch environments can contain license material, API
    // credentials, or future secrets whose names are not known to this build.  Key
    // presence is enough to diagnose whether a flag reached the child without creating
    // a plaintext credential sink in orion_native.log.
    {
        QStringList orionKeys;
        const QStringList envKeys = env.keys();
        for (const QString& key : envKeys) {
            if (key.startsWith(QStringLiteral("ORION_"))) {
                orionKeys.append(key);
            }
        }
        orionKeys.sort();
        emit setupMessage(QStringLiteral("Sidecar env keys: %1")
                              .arg(orionKeys.isEmpty() ? QStringLiteral("(none)")
                                                       : orionKeys.join(QLatin1Char(' '))));
    }
    proc->setProcessEnvironment(env);
#endif

    connect(proc, &QProcess::readyReadStandardOutput, this, &RemotePlaySession::onSidecarStdout);
    connect(proc, &QProcess::readyReadStandardError, this, &RemotePlaySession::onSidecarStderr);
    connect(proc, &QProcess::finished, this, [this, proc](int exitCode, QProcess::ExitStatus status) {
        // T5 observability: drain the FINAL stderr flush FIRST (it usually carries the
        // Python traceback), then unconditionally log the exit + the rolling stderr tail.
        // This deliberately bypasses the WARNING throttle — a sidecar death must never be
        // silent in orion_native.log again.
        const QByteArray finalStderr = proc->readAllStandardError();
        const auto finalLines = finalStderr.split('\n');
        for (const QByteArray& line : finalLines) {
            recordSidecarStderrLine(line);
        }
        // FIX 4: a CRASH must never read as a normal disconnect. `intentionalSidecarRestart_` is
        // still set here (it is consumed further down), and it is true for BOTH deliberate
        // teardowns — user disconnect (stop()) and watchdog recovery (restartSidecar()) — where a
        // forced kill legitimately produces a non-zero/CrashExit result. Anything else that dies
        // with a non-zero code or a crash status is a real, unexpected death.
        const bool unexpectedDeath = (status != QProcess::NormalExit) || (exitCode != 0);
        const bool crashed = unexpectedDeath && !intentionalSidecarRestart_;
        // Last recorded stderr line: for a Python death this is the exception message itself, so
        // it is the single most useful thing to put in front of the user.
        const QString lastTail = sidecarStderrTail_.isEmpty() ? QString()
                                                              : sidecarStderrTail_.constLast();
        emit setupMessage(QStringLiteral("Autogreen sidecar %1: code=%2 status=%3")
                              .arg(crashed ? QStringLiteral("CRASHED") : QStringLiteral("exited"))
                              .arg(exitCode)
                              .arg(status == QProcess::NormalExit ? QStringLiteral("normal")
                                                                  : QStringLiteral("crash")));
        for (const QString& tailLine : std::as_const(sidecarStderrTail_)) {
            emit setupMessage(QStringLiteral("Sidecar %1tail: %2")
                                  .arg(crashed ? QStringLiteral("crash ") : QString(), tailLine));
        }
        sidecarStderrTail_.clear();
        cancelAudioApply();
        resetPreviewPresentation(false);
        retireShmSourceEpoch();
        // The process generation that owned either readiness attempt is gone. Invalidate both
        // timers before emitting state/crash signals so no deferred verdict can act on a restarted
        // sidecar or a later user connect.
        ++streamPromoteGeneration_;
        streamPromotePending_ = false;
        streamPromoteAcked_ = false;
        streamPromoteFromWarmPreview_ = false;
        rejectLateSidecarStarted_ = true;
        ++inputRecoveryGeneration_;
        inputRecoveryPending_ = false;
        rejectLateInputRecoveryReady_ = true;
        const bool whileStreaming = state_ == RemotePlayState::Running || state_ == RemotePlayState::Connecting;
        if (whileStreaming) {
            // FIX 4: the status line the user actually reads used to say "Autogreen sidecar exited"
            // for a crash and a clean stop alike — a hard crash was indistinguishable from a normal
            // disconnect. Name the crash and carry the stderr tail into the status text. State stays
            // Disconnected on purpose: Error would change the controller's recovery behaviour
            // (it suppresses the decoder-stall restart), which is out of scope for an observability
            // fix.
            setState(RemotePlayState::Disconnected,
                     crashed ? QStringLiteral("Autogreen sidecar CRASHED (code %1)%2")
                                   .arg(exitCode)
                                   .arg(lastTail.isEmpty() ? QString()
                                                           : QStringLiteral(" - ") + lastTail.left(160))
                             : QStringLiteral("Autogreen sidecar exited"));
        } else if (previewMode_ && !intentionalSidecarRestart_
                   && state_ != RemotePlayState::Error) {
            // FIX 4: the pre-Connect preview sidecar dying on its own left the status text stuck on
            // "Live capture preview" while the panel silently fell back to the placeholder — the UI
            // claimed a feed that no longer existed. previewMode_ is already cleared before every
            // DELIBERATE teardown (stop() / the warm handoff in start()), so reaching here means the
            // preview genuinely died by itself.
            // The Error guard matters: a sidecar that reported a SPECIFIC {"event":"error"} (e.g.
            // "capture card index 0 is in use") and then exited must keep that message — it is far
            // more actionable than a generic "the sidecar stopped".
            setState(RemotePlayState::Disconnected,
                     crashed ? QStringLiteral("Live capture preview stopped: sidecar died (code %1)%2")
                                   .arg(exitCode)
                                   .arg(lastTail.isEmpty() ? QString()
                                                           : QStringLiteral(" - ") + lastTail.left(160))
                             : QStringLiteral("Live capture preview stopped: detection sidecar exited"));
        }
        sidecarProcess_ = nullptr;
        // An intentional restart (watchdog frame-stall / crash recovery) tears the
        // sidecar down on purpose. Reporting it as a mid-stream crash would bump the
        // controller's recovery counter a second time within milliseconds and trip
        // safe mode on every single recovery, so the restart could never succeed.
        const bool crashWhileStreaming = whileStreaming && !intentionalSidecarRestart_;
        intentionalSidecarRestart_ = false;
        if (previewMode_) {
            // The live-preview sidecar exited on its own (device lost / crash). Drop preview state
            // so the panel falls back to the idle placeholder and a later retry can reopen the card.
            previewMode_ = false;
            emit previewActiveChanged(false);
        }
        emit sidecarExited(crashWhileStreaming);
        sidecarFps_ = 0;
        captureLoopFps_ = 0;
        uniqueFrameFps_ = 0;
        lastSidecarDetectionFrameCount_ = -1;
        // [E4] Both frame counters die with the process: a fresh sidecar restarts frame_number near
        // 0, so carrying the old session's (large) value over would report a huge bogus preview lag
        // until the new one caught up.
        lastSidecarDetectionFrameNumber_ = -1;
        previewLag_ = 0;
        duplicateFramePct_ = 0.0;
        frameAgeMs_ = 0.0;
        pixelAgeMs_ = 0.0;
        transportAgeMs_ = 0.0;
        capturePublicationAgeMs_ = 0.0;
        backendFrozen_ = false;
        previewDroppedFrames_ = 0;   // FIX 3: per-process counter dies with the process
        lastPreviewDropLogMs_ = 0;
        lastPreviewPipelineStatsMs_ = 0;
        fpsProbeState_ = QStringLiteral("stable-60");
        emit sidecarStatsChanged();
    });

    connect(proc, &QProcess::errorOccurred, this, [this, proc](QProcess::ProcessError err) {
        // Launch failure only; a mid-stream crash arrives via QProcess::finished. We never
        // block the GUI thread on waitForStarted() — that up-to-5 s wait was the connect stall.
        if (err == QProcess::FailedToStart) {
            resetPreviewPresentation(false);
            retireShmSourceEpoch();
            emit setupMessage(QStringLiteral("Autogreen sidecar FAILED TO START: %1")
                                  .arg(proc->errorString()));
            setState(RemotePlayState::Error,
                     QStringLiteral("Failed to launch autogreen sidecar: %1").arg(proc->errorString()));
            if (sidecarProcess_ == proc) {
                sidecarProcess_ = nullptr;
            }
            proc->deleteLater();
            return;
        }
        // FIX 4: every other QProcess error (Crashed / Timedout / WriteError / ReadError) used to
        // be dropped on the floor. A WriteError in particular means our stdin commands (start_stream,
        // shutdown, release markers) are no longer reaching the sidecar — a bot that looks alive and
        // takes no instructions. Non-fatal (QProcess::finished still owns the state transition), but
        // never silent again.
        emit setupMessage(QStringLiteral("Autogreen sidecar process error (%1): %2")
                              .arg(static_cast<int>(err))
                              .arg(proc->errorString()));
    });

    connect(proc, &QProcess::started, this, [this, proc]() {
        // This is the first confirmed edge of a genuinely new sidecar process
        // generation. Emit before stdout can be consumed so every process-local
        // provenance namespace is reset before its first ACK/telemetry payload.
        if (sidecarProcess_ != proc) {
            return;
        }
#ifdef Q_OS_WIN
        assignSidecarToJob(proc);
#endif
        emit sidecarProcessGenerationStarted();
    });

    proc->start();
}

void RemotePlaySession::restartSidecar()
{
    // Only flag the restart as intentional when a live process is actually being
    // torn down here — its QProcess::finished will fire and consume the flag. If the
    // sidecar already exited on its own (real crash, sidecarProcess_ nulled), there
    // is no finished event to consume it, so leaving it set would wrongly suppress a
    // genuine repeat crash on the NEXT process and defeat the safe-mode escalation.
    intentionalSidecarRestart_ = (sidecarProcess_ != nullptr
                                  && sidecarProcess_->state() != QProcess::NotRunning);
    stopSidecar();
    // Give the old process a beat to release the frame pipe / capture handles before we
    // respawn. Track W: in capture-card mode wait longer (kSidecarRestartDelayMsCaptureCard)
    // so the Elgato's DirectShow handle releases first — respawning too soon makes the new
    // sidecar hit "no live device (index 0)" and leaves the device contended, which then stalls
    // the GUI-thread video-device enumeration into SAFE MODE. Decoder-pipe mode has no
    // DirectShow device to release, so it keeps the historical fast beat.
    const bool captureCard = isCaptureCardSource(config_);
    const int delayMs = sidecarRestartDelayMs(captureCard);
    emit setupMessage(QStringLiteral("Sidecar restart scheduled: delayMs=%1 captureCard=%2")
                          .arg(delayMs)
                          .arg(captureCard ? 1 : 0));
    // CANCELLATION GUARD (the warm-preview handoff timer in start() has the equivalent): this
    // deferred relaunch was unguarded, so a watchdog restart armed moments before the user hits
    // Disconnect fired ~0.6-3s LATER — after the teardown — and resurrected the whole session: a
    // fresh sidecar, which relaunches chiaki, which makes the PS5 reconnect unbidden, while also
    // racing startCapturePreview() for the single-open Elgato handle.
    //
    // The guard is a pending-intent flag, NOT `state_ == Running || Connecting`. stopSidecar()
    // above makes QProcess::finished fire, and that handler sets state_ to Disconnected whenever
    // it was Running/Connecting (see startSidecar's finished lambda) — while startSidecar() itself
    // never sets Connecting; Running only returns when telemetry arrives. So by the time this beat
    // elapses state_ is ALWAYS Disconnected and a state check would block every watchdog restart.
    // stop() clears the flag, which is exactly the disconnect case we need to cancel.
    sidecarRestartPending_ = true;
    QTimer::singleShot(delayMs, this, [this]() {
        if (!sidecarRestartPending_) {
            return;   // a disconnect (or a fresh connect) superseded this restart during the beat
        }
        sidecarRestartPending_ = false;
        startSidecar();
    });
}

void RemotePlaySession::stopSidecar()
{
    cancelAudioApply();
    resetPreviewPresentation(false);
    retireShmSourceEpoch();
    shmOpenFailures_ = 0;
    shmReadFailures_ = 0;
    shmFramesRead_ = 0;
    lastShmFramesReadCount_ = 0;
    shmFallbackRequested_ = false;
    jpegFallbackFirstFrameNumber_ = 0;
    auto* proc = sidecarProcess_;
    sidecarProcess_ = nullptr;
    sidecarBuffer_.clear();
    previewChunks_.reset();
    const qint64 sidecarPid = proc ? proc->processId() : 0;
    // GRACEFUL CHIAKI DISCONNECT: true only when the sidecar refused to exit within the grace
    // window and we had to force it. Gates the /F /T sweep below — see the comment there.
    bool forcedTermination = false;
    if (proc && proc->state() != QProcess::NotRunning) {
        const QByteArray line = QJsonDocument(QJsonObject{{QStringLiteral("cmd"), QStringLiteral("shutdown")}})
                                    .toJson(QJsonDocument::Compact) + '\n';
        proc->write(line);
        proc->closeWriteChannel();
        proc->terminate();
        // Give Python enough time to run orch.stop(), which closes the
        // RemotePlayClientManager and calls terminate_chiaki_processes() (loops
        // up to 6s doing taskkill+verify). 900ms was too short — the hard
        // taskkill /F /T fired before graceful shutdown, abruptly dropping the
        // PS5 TCP connection and triggering "LAN cable disconnected" on the PS5.
        if (!proc->waitForFinished(kSidecarGracefulShutdownMs)) {
            forcedTermination = true;
#ifdef Q_OS_WIN
            if (sidecarPid > 0) {
                runTaskkillBlocking({QStringLiteral("/PID"), QString::number(sidecarPid), QStringLiteral("/F"), QStringLiteral("/T")});
            }
#endif
            if (proc->state() != QProcess::NotRunning) {
                proc->kill();
                proc->waitForFinished(500);
            }
        }
    }

#ifdef Q_OS_WIN
    // Deterministic cleanup beats a detached best-effort taskkill here. The
    // custom OrionStream fallback can briefly spawn a child while the parent
    // still owns \\.\pipe\orion_input / \\.\pipe\orion_frames; a restart that
    // races that state leaves the new stream without hooks.
    //
    // GRACEFUL CHIAKI DISCONNECT: but this must NOT run on the clean-exit path. When the sidecar
    // exits normally it has already closed the Chiaki session properly (chiaki_session_stop ->
    // Takion/ctrl disconnect) and reaped its own client; running a blanket TerminateProcess sweep
    // anyway was what made EVERY teardown — including a mid-game watchdog restartSidecar() — look
    // to the PS5 like the transport had simply vanished ("LAN cable disconnected"). It also costs
    // 8 blocking taskkill spawns + 300ms of msleep on the GUI thread for nothing. Keep it strictly
    // as the recovery path for a sidecar that had to be force-killed, where an orphaned stream
    // client holding \\.\pipe\orion_input really can survive.
    if (forcedTermination) {
        cleanupRemotePlayClientsBlocking();
    }
#endif

    if (proc) {
        proc->deleteLater();
    }
}

bool RemotePlaySession::sendSidecarCommand(const QJsonObject& cmd)
{
    if (!sidecarProcess_ || sidecarProcess_->state() != QProcess::Running) {
        return false;
    }
    const QByteArray line = QJsonDocument(cmd).toJson(QJsonDocument::Compact) + '\n';
    // QIODevice::write returns the bytes queued, or -1 on error. A short/failed write means the
    // sidecar never sees this command; callers that care (promoteWarmPreviewToStream) must not
    // report success for work that was never requested.
    return sidecarProcess_->write(line) == line.size();
}

void RemotePlaySession::recordSidecarStderrLine(const QByteArray& line)
{
    const QByteArray trimmed = line.trimmed();
    if (trimmed.isEmpty()) {
        return;
    }
    sidecarStderrTail_.append(QString::fromUtf8(trimmed.left(300)));
    while (sidecarStderrTail_.size() > kSidecarStderrTailMax) {
        sidecarStderrTail_.removeFirst();
    }
}

void RemotePlaySession::onSidecarStderr()
{
    if (!sidecarProcess_) return;
    // sidecar logs to stderr â€” relay to setupMessage occasionally for debugging
    const QByteArray data = sidecarProcess_->readAllStandardError();
    if (data.isEmpty()) return;
    const auto lines = data.split('\n');
    for (const auto& l : lines) {
        const QByteArray trimmed = l.trimmed();
        if (trimmed.isEmpty()) continue;
        // Rolling tail (T5): every stderr line is remembered so the exit handler can dump
        // the last 10 even when none matched a relay filter below.
        recordSidecarStderrLine(trimmed);
        // Capture-card mode: Chiaki is INPUT-ONLY (the card is the video), so its stream WINDOW is
        // never needed and the client-launch wait deliberately short-circuits (wait_timeout_s=2s in
        // the orchestrator). The resulting "Remote Play client launch failed" / "stream window not
        // found" warning is therefore EXPECTED on every healthy capture-card connect — relaying it
        // surfaced a false "launch failed / finish pairing" alarm to the user each time. Keep it in
        // the rolling tail (above) for post-mortem, but never surface it as a live warning in cc-mode.
        if (isCaptureCardSource(config_)
            && (trimmed.contains("Remote Play client launch failed")
                || trimmed.contains("stream window")
                || trimmed.contains("Stream window"))) {
            continue;
        }
        // ERROR/CRITICAL always surface. WARNING is relayed too (throttled) so a
        // logger.warning diagnostic — e.g. a detector rejection reason — is no longer
        // silently dropped the way the old DETPROBE was. Plain INFO stays on the
        // stdout _emit({"event":"log"}) path to avoid flooding the setup log.
        // T5/T6: "Traceback"/"Error" (case-sensitive) catch a Python crash's traceback +
        // RuntimeError:/ValueError: lines; " ENABLED" catches the one-shot feature
        // confirmations (Fill forecaster/Kalman) the 1s WARNING throttle used to eat.
        const bool isReleaseMarkerInfo = shouldRelaySidecarInfoLine(trimmed);
        const bool isError = trimmed.contains("ERROR") || trimmed.contains("CRITICAL")
            || trimmed.contains("Traceback") || trimmed.contains("Error")
            || trimmed.contains(" ENABLED");
        const bool isWarning = trimmed.contains("WARNING");
        // [ORION_PROBE] Probe diagnostics BYPASS the 1s global warning throttle.
        //
        // That throttle is a single shared slot for every sidecar WARNING line, so a low-rate
        // diagnostic competes with high-rate chatter (Capture health alone fires every ~5s) and
        // is dropped essentially at random. On 2026-08-05 this cost most of a night: a
        // user-triggered probe run emitted its markers, closes, expiries and the raw-spread
        // result, and the log showed ONE line out of eight because the rest lost the race.
        // Successive runs looked like five different failures and were in fact one relay.
        //
        // Promoting the estimator's lines from INFO to WARNING did not help -- WARNING is
        // exactly what is throttled. Only an explicit bypass works, which is the same remedy
        // already applied above for release markers and for the " ENABLED" confirmations the
        // comment notes this throttle "used to eat". A user-triggered diagnostic is inherently
        // low-rate and bounded (8 presses per run); it must never be sampled.
        const bool isProbeDiagnostic =
            trimmed.contains("probe marker") || trimmed.contains("probe closed")
            || trimmed.contains("probe frame") || trimmed.contains("probe DROPPED")
            || trimmed.contains("probe raw spread") || trimmed.contains("probe EXPIRED")
            || trimmed.contains("probe spawn estimate") || trimmed.contains("tick phase fit")
            || trimmed.contains("mark_probe");
        // [ORION_AUTHORITY_DEATH] The two lines that explain why live timing stopped. Both are
        // emitted back-to-back at WARNING by _guard_capture_latency_route()'s revocation path, so
        // the 1/sec global throttle drops at least one and usually both -- while the far less
        // informative "Detector frame rejected: reason=capture_route_mismatch" wins the race and
        // survives. MEASURED 2026-08-05: two separate sessions died with 73 route-proof rejects,
        // 0 releases and 14 waiting_for_latency_calibration, and the reason line appeared ZERO
        // times in the log, leaving the actual cause (wrong DSHOW index? MSMF fallback? geometry
        // stamp mismatch?) unknowable after the fact.
        //
        // A once-per-session cause-of-death line must never be sampled. Same remedy, same reason,
        // as the probe diagnostics above.
        const bool isAuthorityDeath =
            trimmed.contains("warm timing revoked")
            || trimmed.contains("Latency authority reset")
            || trimmed.contains("capture_route_mismatch");
        if (isError || isReleaseMarkerInfo || isProbeDiagnostic || isAuthorityDeath) {
            emit setupMessage(QStringLiteral("Sidecar: %1").arg(QString::fromUtf8(trimmed.left(300))));
            // [ORION_AUTHORITY_DEATH 2026-08-10] The cause line above survives the throttle but
            // reads as one more diagnostic. State the consequence in the customer's terms: from
            // here on every press falls through and only a restart recovers. Emitted AFTER the
            // cause so the log keeps "what happened" then "what it means"; once per session.
            //
            // Deliberately NOT keyed on isAuthorityDeath. That predicate is correctly BROAD -- all
            // three of its lines must bypass the warn throttle -- but only ONE of them means the
            // bot is actually dead. "Latency authority reset" is logged UNCONDITIONALLY by
            // _replace_latency_authority (remote_play_orchestrator.py:2899, outside the if), which
            // routine transitions call: notably capture_negotiated_mode_attested on the FIRST
            // successful attestation of every fresh session, and the source-generation transition
            // on every Remote Play reconnect. Keying the announcement on the broad predicate would
            // scare the customer into restarting a perfectly healthy session AND latch the
            // once-per-session flag, silencing the real revocation if it happened later -- the
            // exact opposite of the point. "capture_route_mismatch" is likewise emitted by the
            // high-rate throttled Detector-frame-rejected line. Only "warm timing revoked"
            // (remote_play_orchestrator.py:3138) is unique to the one-way revocation itself.
            //
            // ASCII-only literal on purpose: this file has no UTF-8 BOM and the build sets no
            // /utf-8, so a non-ASCII dash would mojibake under a non-UTF-8 active code page. Same
            // rule, same reason, as captureResolution() in RemotePlaySession.h.
            if (!authorityDeathAnnounced_ && trimmed.contains("warm timing revoked")) {
                authorityDeathAnnounced_ = true;
                emit setupMessage(QStringLiteral(
                    "TIMING DISABLED - the capture card changed route mid-session, so shot timing "
                    "cannot be trusted and the bot will stop firing. Close Venice and reopen it to "
                    "recover. If it repeats, close anything else using the capture card (OBS, "
                    "Camera, a browser tab) before reconnecting."));
            }
        } else if (isWarning) {
            const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
            if (nowMs - lastSidecarWarnLogMs_ >= kSidecarWarnThrottleMs) {
                lastSidecarWarnLogMs_ = nowMs;
                emit setupMessage(QStringLiteral("Sidecar: %1").arg(QString::fromUtf8(trimmed.left(300))));
            }
        }
    }
}

namespace {

// [D3] Bounded classifier for the sidecar's preview-frame JSONL line.
//
// The line is {"event":"frame","jpeg_b64":"<200-595 KB of base64>","frame_number":N} and arrives
// ~56x/sec during gameplay. Matching on the whole tag INCLUDING the closing quote matters: it must
// not match a different event whose name merely starts with "frame" (e.g. "frame_shm"). The scan is
// capped so it never walks into the base64 body - the key order is fixed by the emitter
// (autogreen_sidecar._build_frame_line), so the tag is always in the first handful of bytes.
constexpr qsizetype kFrameTagScanBytes = 64;

bool isPreviewFrameLine(const char* data, qsizetype len)
{
    if (data == nullptr || len <= 0) return false;
    const QByteArray head = QByteArray::fromRawData(data, std::min(len, kFrameTagScanBytes));
    return head.indexOf(QByteArrayLiteral("\"event\":\"frame\"")) >= 0;
}

// Sets a bool for the lifetime of the scope and always clears it, so an exception escaping a
// message handler cannot leave the re-entrancy flag latched (which would silence the stdout feed).
struct ScopedFlag {
    bool& flag;
    explicit ScopedFlag(bool& f) : flag(f) { flag = true; }
    ~ScopedFlag() { flag = false; }
    ScopedFlag(const ScopedFlag&) = delete;
    ScopedFlag& operator=(const ScopedFlag&) = delete;
};

}  // namespace

void RemotePlaySession::submitPreviewPayload(QByteArray jpegBase64, int frameNum)
{
    if (jpegBase64.isEmpty()) return;
    if (shmFallbackRequested_ && jpegFallbackFirstFrameNumber_ > 0
        && frameNum > 0 && frameNum < jpegFallbackFirstFrameNumber_) {
        // A pre-fence JPEG can only be a delayed partial/full record from the
        // superseded transport interval. Never let it overtake valid SHM frames
        // already queued ahead of the explicit handoff.
        return;
    }
    if (frameDecoder_) {
        frameDecoder_->submit(std::move(jpegBase64), frameNum);
    }
}

bool isPreviewShmLine(const char* data, qsizetype len)
{
    if (data == nullptr || len <= 0) return false;
    const QByteArray head = QByteArray::fromRawData(data, std::min(len, kFrameTagScanBytes));
    return head.indexOf(QByteArrayLiteral("\"event\":\"frame_shm\"")) >= 0;
}

void RemotePlaySession::updatePreviewPresentationCadence(bool resetRateClass)
{
    if (resetRateClass) {
        previewPresentationRateClass_.reset(requestedFps_);
    }
    previewPresentationCadence_.reset(previewPresentationRateClass_.displayFps());
    // A render-clock owner already quantizes handoff to real display ticks. Its
    // ideal source phase must stay fixed; the timer-only occupancy servo's
    // sub-millisecond corrections can otherwise alias into a whole skipped
    // vsync on high-refresh displays.
    previewPresentationPeriodNs_ = previewPresentationRenderClockActive_
        ? PreviewPresentationBuffer::integerPeriodForRequestedFps(
              previewPresentationRateClass_.displayFps()).count()
        : previewPresentationCadence_.period().count();
    if (previewPresentationTimer_.isActive() && previewPresentationClock_.isValid()) {
        previewPresentationTimer_.stop();
        previewPresentationNextDeadlineNs_ = 0;
        scheduleNextPreviewPresentation(previewPresentationClock_.nsecsElapsed());
    }
}

void RemotePlaySession::scheduleNextPreviewPresentation(qint64 nowNs)
{
    const auto schedule = PreviewPresentationBuffer::scheduleAfterTick(
        previewPresentationNextDeadlineNs_, nowNs, previewPresentationPeriodNs_);
    previewPresentationNextDeadlineNs_ = schedule.deadlineNs;
    if (!previewPresentationRenderClockActive_) {
        previewPresentationTimer_.setInterval(std::chrono::nanoseconds{schedule.delayNs});
        previewPresentationTimer_.start();
    }
}

void RemotePlaySession::setPreviewRenderClockActive(bool active)
{
    if (previewPresentationRenderClockActive_ == active) {
        return;
    }

    // Ownership is exclusive. In particular, invalidate the old deadline so a
    // queued timeout from the former driver cannot consume beside a render tick.
    previewPresentationTimer_.stop();
    previewPresentationRenderClockActive_ = active;
    previewPresentationNextDeadlineNs_ = 0;
    previewPresentationLastRenderTickNs_ = 0;
    previewPresentationRenderTickPeriodNs_ = 0;
    previewPresentationPrimed_ = false;
    previewPresentationRecovering_ = false;
    updatePreviewPresentationCadence(false);

    if (!previewPresentationClock_.isValid()) {
        previewPresentationClock_.start();
    }
    const qint64 nowNs = previewPresentationClock_.nsecsElapsed();
    previewPresentationPrimeStartedNs_ = previewPresentationBuffer_.empty()
        ? 0 : std::max<qint64>(1, nowNs);

    // Hidden/non-QML pages retain the timer presenter so capture health and the
    // latest provider snapshot keep advancing. Visible QML never runs both.
    if (!active && previewPresentationAccepting_
        && !previewPresentationBuffer_.empty()) {
        scheduleNextPreviewPresentation(nowNs);
    }
}

void RemotePlaySession::presentNextPreviewFrameOnRenderTick()
{
    if (!previewPresentationRenderClockActive_
        || !previewPresentationAccepting_) {
        return;
    }
    if (!previewPresentationClock_.isValid()) {
        previewPresentationClock_.start();
    }

    const qint64 nowNs = previewPresentationClock_.nsecsElapsed();
    if (previewPresentationLastRenderTickNs_ > 0
        && nowNs > previewPresentationLastRenderTickNs_) {
        const qint64 sampleNs = nowNs - previewPresentationLastRenderTickNs_;
        previewPresentationWindowMaxRenderTickGapNs_ = std::max(
            previewPresentationWindowMaxRenderTickGapNs_, sampleNs);
        // Ignore a paused/minimized window's long first interval. It must not
        // turn into an oversized early-presentation tolerance after restore.
        if (sampleNs >= 1'000'000LL && sampleNs <= 50'000'000LL) {
            previewPresentationRenderTickPeriodNs_ =
                previewPresentationRenderTickPeriodNs_ > 0
                ? ((previewPresentationRenderTickPeriodNs_ * 7) + sampleNs) / 8
                : sampleNs;
        }
    }
    previewPresentationLastRenderTickNs_ = nowNs;

    // Before the first source image, a FrameAnimation is allowed to run but it
    // must not manufacture underflows/recovery state.
    if (!previewPresentationEverPresented_
        && previewPresentationInputFrames_ == 0
        && previewPresentationBuffer_.empty()) {
        return;
    }

    const qint64 measuredRenderPeriodNs =
        previewPresentationRenderTickPeriodNs_ > 0
        ? previewPresentationRenderTickPeriodNs_
        : previewPresentationPeriodNs_;
    if (!PreviewPresentationBuffer::renderTickIsDue(
            previewPresentationNextDeadlineNs_, nowNs,
            measuredRenderPeriodNs)) {
        return;
    }
    presentNextPreviewFrameAt(nowNs, true);
}

void RemotePlaySession::resetPreviewPresentation(bool acceptFrames)
{
    previewPresentationTimer_.stop();
    if (!acceptFrames) {
        // A destroyed/hidden QML page cannot leave render ownership latched.
        previewPresentationRenderClockActive_ = false;
    }
    updatePreviewPresentationCadence(true);
    previewPresentationBuffer_.clear();
    previewPresentationAccepting_ = acceptFrames;
    previewPresentationPrimed_ = false;
    previewPresentationRecovering_ = false;
    previewPresentationEverPresented_ = false;
    previewPresentationPrimeStartedNs_ = 0;
    previewPresentationNextDeadlineNs_ = 0;
    previewPresentationLastRenderTickNs_ = 0;
    previewPresentationRenderTickPeriodNs_ = 0;
    previewPresentationLastFrameNs_ = 0;
    previewPresentationWindowMaxGapNs_ = 0;
    previewPresentationWindowMaxRenderTickGapNs_ = 0;
    previewPresentationInputFrames_ = 0;
    previewPresentationFrames_ = 0;
    previewPresentationDroppedFrames_ = 0;
    previewPresentationUnderflows_ = 0;
    previewPresentationMaxDepth_ = 0;
    lastPreviewDisplayPresentedCount_ = 0;
    lastPreviewDisplayDroppedCount_ = 0;
    lastPreviewDisplayUnderflowCount_ = 0;
    previewPresentationClock_.restart();
}

void RemotePlaySession::queuePreviewFrame(QImage image, int frameNumber)
{
    if (!previewPresentationAccepting_ || image.isNull()) {
        return;
    }
    if (!previewPresentationClock_.isValid()) {
        previewPresentationClock_.start();
    }
    const auto pushed = previewPresentationBuffer_.push(std::move(image), frameNumber);
    if (!pushed.accepted) {
        return;
    }
    ++previewPresentationInputFrames_;
    if (pushed.droppedOldest) {
        ++previewPresentationDroppedFrames_;
    }
    previewPresentationMaxDepth_ = std::max(previewPresentationMaxDepth_, pushed.depth);

    if (previewPresentationRenderClockActive_) {
        if (previewPresentationPrimeStartedNs_ == 0) {
            previewPresentationPrimed_ = false;
            previewPresentationRecovering_ = false;
            const qint64 nowNs = previewPresentationClock_.nsecsElapsed();
            previewPresentationPrimeStartedNs_ = std::max<qint64>(1, nowNs);
            previewPresentationNextDeadlineNs_ = 0;
        }
    } else if (!previewPresentationTimer_.isActive()) {
        previewPresentationPrimed_ = false;
        previewPresentationRecovering_ = false;
        const qint64 nowNs = previewPresentationClock_.nsecsElapsed();
        previewPresentationPrimeStartedNs_ = std::max<qint64>(1, nowNs);
        previewPresentationNextDeadlineNs_ = 0;
        scheduleNextPreviewPresentation(nowNs);
    }
}

void RemotePlaySession::presentNextPreviewFrame()
{
    const qint64 nowNs = previewPresentationClock_.isValid()
        ? previewPresentationClock_.nsecsElapsed() : 0;
    presentNextPreviewFrameAt(nowNs, false);
}

void RemotePlaySession::presentNextPreviewFrameAt(qint64 nowNs, bool renderClockTick)
{
    // Reject a stale timeout or stale FrameAnimation callback at an ownership
    // boundary. Exactly one clock may dequeue this atomic image/frame-id pair.
    if (renderClockTick != previewPresentationRenderClockActive_) {
        return;
    }
    if (!previewPresentationAccepting_) {
        previewPresentationTimer_.stop();
        previewPresentationBuffer_.clear();
        previewPresentationNextDeadlineNs_ = 0;
        return;
    }

    if (previewPresentationBuffer_.empty()) {
        if (previewPresentationEverPresented_) {
            ++previewPresentationUnderflows_;
        }
        // Preserve the absolute display phase while rebuilding the bounded
        // cushion. Stopping the clock here made the next source frame start a
        // fresh full-period timer and then wait for another prime, turning one
        // ordinary producer burst into a 40-62 ms visible freeze. Keep one
        // cadence wake active instead; QML continues holding the prior texture
        // and the producer can refill without a phase reset.
        if (!previewPresentationRecovering_) {
            previewPresentationPrimed_ = false;
            previewPresentationRecovering_ = true;
            previewPresentationPrimeStartedNs_ = std::max<qint64>(1, nowNs);
        }
        if (!previewPresentationRenderClockActive_) {
            previewPresentationCadence_.observeEmptyWake();
            previewPresentationPeriodNs_ = previewPresentationCadence_.period().count();
        }
        scheduleNextPreviewPresentation(nowNs);
        return;
    }

    if (!previewPresentationPrimed_) {
        // Ordinarily wait for three frames (about two display frames of
        // latency), which absorbs the observed 0/32/47 ms source bursts. Never
        // strand a lone valid image: after one bounded prime window it is
        // presented even if the source stopped before filling the cushion.
        const std::size_t requiredDepth = previewPresentationRecovering_
            ? PreviewPresentationBuffer::kRecoveryDepth
            : PreviewPresentationBuffer::kPrimeDepth;
        const bool primeTimedOut = previewPresentationPrimeStartedNs_ > 0
            && nowNs - previewPresentationPrimeStartedNs_
                >= static_cast<qint64>(requiredDepth)
                    * previewPresentationPeriodNs_;
        if (previewPresentationBuffer_.depth() < requiredDepth
            && !primeTimedOut) {
            scheduleNextPreviewPresentation(nowNs);
            return;
        }
        previewPresentationPrimed_ = true;
        previewPresentationRecovering_ = false;
    }

    auto next = previewPresentationBuffer_.take();
    if (!next.has_value()) {
        scheduleNextPreviewPresentation(nowNs);
        return;
    }

    ++previewPresentationFrames_;
    previewPresentationEverPresented_ = true;
    if (!previewPresentationRenderClockActive_) {
        previewPresentationCadence_.observePresentedReserve(
            previewPresentationBuffer_.depth());
        previewPresentationPeriodNs_ = previewPresentationCadence_.period().count();
    }
    if (previewPresentationLastFrameNs_ > 0 && nowNs > previewPresentationLastFrameNs_) {
        previewPresentationWindowMaxGapNs_ = std::max(
            previewPresentationWindowMaxGapNs_, nowNs - previewPresentationLastFrameNs_);
    }
    previewPresentationLastFrameNs_ = nowNs;

    // previewLag_ now describes the frame actually handed to QML, not the newer
    // frame that merely entered the jitter buffer.
    if (next->frameNumber > 0 && lastSidecarDetectionFrameNumber_ > 0) {
        previewLag_ = std::max(0, lastSidecarDetectionFrameNumber_ - next->frameNumber);
    } else {
        previewLag_ = 0;
    }

    // Preserve the absolute cadence before emission: a direct receiver may tear
    // down the owning session, so no member access is safe after frameReady.
    scheduleNextPreviewPresentation(nowNs);
    emit frameReady(std::move(next->image), next->frameNumber);
}

void RemotePlaySession::resetShmPresentationTimingDiagnostics()
{
    shmSourceToReadAgeMs_ = 0.0;
    shmReadToDispatchAgeMs_ = 0.0;
    shmSourceToDispatchAgeMs_ = 0.0;
    shmSourceToReadWindowMaxMs_ = 0.0;
    shmReadToDispatchWindowMaxMs_ = 0.0;
    shmSourceToDispatchWindowMaxMs_ = 0.0;
    shmPresentationTimestampRejects_ = 0;
}

void RemotePlaySession::beginShmSourceEpoch()
{
    if (shmSourceActive_) {
        shmPump_.retireSourceEpoch(shmSourceEpoch_);
    }
    ++shmSourceEpoch_;
    if (shmSourceEpoch_ == 0) {
        ++shmSourceEpoch_;
    }
    // Per-process/session kernel names prevent a stale or concurrent launcher
    // from signaling or reading this sidecar's display-only mapping. The token
    // is not an authority secret; it is solely a collision-resistant namespace.
    QString token = QUuid::createUuid().toString(QUuid::WithoutBraces);
    token.remove(QLatin1Char('-'));
    shmTransportNames_.mappingName = QStringLiteral("OrionPreviewFrame_%1").arg(token);
    shmTransportNames_.mutexName = QStringLiteral("OrionPreviewMutex_%1").arg(token);
    shmTransportNames_.readyEventName = QStringLiteral("OrionPreviewReady_%1").arg(token);
    shmSourceActive_ = true;
    shmReaderOpen_ = false;
    shmEventWaitTimeouts_ = 0;
    shmEventWaitFailures_ = 0;
    shmEventGenerationProbes_ = 0;
    shmEventNotificationLosses_ = 0;
    shmReadyFrameReplaced_ = 0;
    shmDeliveryScheduleFailures_ = 0;
    resetShmPresentationTimingDiagnostics();
    shmPump_.beginSourceEpoch(shmSourceEpoch_, shmTransportNames_);
}

void RemotePlaySession::retireShmSourceEpoch()
{
    if (shmSourceActive_ && shmSourceEpoch_ != 0) {
        shmPump_.retireSourceEpoch(shmSourceEpoch_);
    }
    shmSourceActive_ = false;
    shmReaderOpen_ = false;
    resetShmPresentationTimingDiagnostics();
}

bool RemotePlaySession::switchPreviewToJpegFallback(const QString& reason,
                                                    int firstJpegFrameNumber,
                                                    bool requestSidecar)
{
    if (shmFallbackRequested_) {
        return true;
    }
    if (!shmSourceActive_ || shmSourceEpoch_ == 0) {
        return false;
    }
    if (requestSidecar
        && !sendSidecarCommand(
            {{QStringLiteral("cmd"), QStringLiteral("preview_transport")},
             {QStringLiteral("mode"), QStringLiteral("jpeg")}})) {
        // The producer is still allowed to write SHM. Keep this epoch active
        // until it either acknowledges a self-handoff or the process exits;
        // accepting both transports here would reintroduce frame reordering.
        return false;
    }

    shmFallbackRequested_ = true;
    jpegFallbackFirstFrameNumber_ = std::max(0, firstJpegFrameNumber);

    // No partial JPEG from an earlier transport attempt may be completed after
    // the fence. Already-buffered, fully-owned SHM QImages remain valid and can
    // drain ahead of the first JPEG, avoiding a visible re-prime hitch.
    previewChunks_.reset();
    retireShmSourceEpoch();

    // Do not report one mixed SHM/JPEG diagnostics window. The next telemetry
    // sample captures fresh decoder/presenter baselines before calculating a
    // JPEG rate; the actual presentation queue stays live and bounded.
    lastPreviewPipelineStatsMs_ = 0;
    lastShmFramesReadCount_ = shmFramesRead_;
    previewPresentationWindowMaxGapNs_ = 0;

    QString safeReason = reason.trimmed().left(64);
    safeReason.replace(QLatin1Char('\n'), QLatin1Char(' '));
    safeReason.replace(QLatin1Char('\r'), QLatin1Char(' '));
    emit setupMessage(QStringLiteral("Preview transport switched in place to JPEG fallback: %1")
                          .arg(safeReason.isEmpty()
                                   ? QStringLiteral("shared-memory transport unavailable")
                                   : safeReason));
    return true;
}

void RemotePlaySession::handlePreviewTransportHandoff(const QJsonObject& msg)
{
    const int protocol = msg.value(QStringLiteral("protocol")).toInt(0);
    const QString mode = msg.value(QStringLiteral("mode")).toString();
    const QString readyEvent = msg.value(QStringLiteral("shm_ready_event")).toString();
    if (protocol != 1 || mode != QLatin1String("jpeg")
        || readyEvent.isEmpty() || readyEvent != shmTransportNames_.readyEventName) {
        // Fail closed on an unscoped or delayed handoff. The current mapping
        // remains authoritative and a later valid message can still recover it.
        emit setupMessage(QStringLiteral("Ignored invalid/stale preview transport handoff."));
        return;
    }
    if (shmFallbackRequested_) {
        return; // Idempotent duplicate from the same source epoch.
    }
    if (!shmSourceActive_) {
        emit setupMessage(QStringLiteral("Ignored preview transport handoff for an inactive SHM epoch."));
        return;
    }

    switchPreviewToJpegFallback(
        msg.value(QStringLiteral("reason")).toString(),
        msg.value(QStringLiteral("first_jpeg_frame_number")).toInt(0),
        false);
}

void RemotePlaySession::handleShmPumpBatch(const SharedMemoryFramePumpBatch& batch)
{
    if (!shmSourceActive_ || batch.sourceEpoch != shmSourceEpoch_
        || shmFallbackRequested_) {
        // Source epochs are an integrity boundary, not a presentation hint. A
        // completion from an exited sidecar can carry a reused low frame number
        // and must never enter the new process's overlay/frame namespace.
        return;
    }

    shmEventWaitTimeouts_ = batch.eventWaitTimeouts;
    shmEventWaitFailures_ = batch.eventWaitFailures;
    shmEventGenerationProbes_ = batch.eventGenerationProbes;
    shmEventNotificationLosses_ = batch.eventNotificationLosses;
    shmReadyFrameReplaced_ = batch.readyFrameReplaced;
    shmDeliveryScheduleFailures_ = batch.deliveryScheduleFailures;

    if (batch.notificationFailureRun > 0) {
        // The pump only raises this after bounded event timeouts *and* proof
        // that the mapping generation advanced. A paused source is therefore
        // not mistaken for a broken event. Coordinate one in-place producer
        // switch now instead of polling the mapping or restarting capture.
        const QString detail = batch.notificationError.isEmpty()
            ? QStringLiteral("SHM ready-event notification was lost")
            : batch.notificationError;
        emit setupMessage(QStringLiteral(
            "preview_shm_integrity: event_wait_timeouts=%1 event_wait_failures=%2 "
            "event_generation_probes=%3 event_notification_losses=%4 "
            "ready_frame_replaced=%5 delivery_schedule_failures=%6 "
            "notification_failure_run=%7")
            .arg(shmEventWaitTimeouts_)
            .arg(shmEventWaitFailures_)
            .arg(shmEventGenerationProbes_)
            .arg(shmEventNotificationLosses_)
            .arg(shmReadyFrameReplaced_)
            .arg(shmDeliveryScheduleFailures_)
            .arg(batch.notificationFailureRun));
        switchPreviewToJpegFallback(detail, 0, true);
        return;
    }

    const bool openedNow = batch.readerOpen && !shmReaderOpen_;
    const int priorOpenFailureRun = shmOpenFailures_;
    const int priorReadFailureRun = shmReadFailures_;
    shmReaderOpen_ = batch.readerOpen;
    shmOpenFailures_ = batch.openFailureRun;
    shmReadFailures_ = batch.readFailureRun;

    if (openedNow) {
        emit setupMessage(QStringLiteral("SHM preview reader opened."));
    }

    const auto crossedLogBeat = [](int previous, int current) noexcept {
        if (current <= previous || current <= 0) {
            return false;
        }
        return previous == 0 || (current / 30) > (previous / 30);
    };
    if (crossedLogBeat(priorOpenFailureRun, shmOpenFailures_)) {
        emit setupMessage(QStringLiteral("SHM preview open pending: attempt=%1 error=%2")
                              .arg(shmOpenFailures_)
                              .arg(batch.openError));
    }
    if (shmOpenFailures_ >= 60) {
        switchPreviewToJpegFallback(
            QStringLiteral("SHM preview unavailable after 60 open attempts"), 0, true);
        return;
    }

    if (crossedLogBeat(priorReadFailureRun, shmReadFailures_)) {
        emit setupMessage(QStringLiteral("SHM preview read fault: run=%1 error=%2")
                              .arg(shmReadFailures_)
                              .arg(batch.readError));
    }
    if (shmReadFailures_ >= 60) {
        switchPreviewToJpegFallback(
            QStringLiteral("SHM preview read remained unhealthy"), 0, true);
        return;
    }

    const quint64 priorFramesRead = shmFramesRead_;
    shmFramesRead_ = batch.framesRead;
    if (batch.image.isNull()) {
        return;
    }
    const bool hasAnyPresentationTimestamp = batch.sourceTimestampNs != 0
        || batch.pumpReadCompletedTimestampNs != 0
        || batch.presentationDispatchTimestampNs != 0;
    if (hasAnyPresentationTimestamp) {
        // QPC/perf-counter clocks share one monotonic domain. Keep malformed or
        // cross-domain values out of diagnostics, but never reject the owning image:
        // this path is display-only and carries no detector/timing authority.
        constexpr quint64 kMaxPresentationDiagnosticAgeNs = 60'000'000'000ULL;
        const bool ordered = batch.sourceTimestampNs > 0
            && batch.pumpReadCompletedTimestampNs >= batch.sourceTimestampNs
            && batch.presentationDispatchTimestampNs
                >= batch.pumpReadCompletedTimestampNs;
        const quint64 totalAgeNs = ordered
            ? batch.presentationDispatchTimestampNs - batch.sourceTimestampNs : 0;
        if (ordered && totalAgeNs <= kMaxPresentationDiagnosticAgeNs) {
            shmSourceToReadAgeMs_ =
                (batch.pumpReadCompletedTimestampNs - batch.sourceTimestampNs) / 1'000'000.0;
            shmReadToDispatchAgeMs_ =
                (batch.presentationDispatchTimestampNs
                 - batch.pumpReadCompletedTimestampNs) / 1'000'000.0;
            shmSourceToDispatchAgeMs_ = totalAgeNs / 1'000'000.0;
            shmSourceToReadWindowMaxMs_ = std::max(
                shmSourceToReadWindowMaxMs_, shmSourceToReadAgeMs_);
            shmReadToDispatchWindowMaxMs_ = std::max(
                shmReadToDispatchWindowMaxMs_, shmReadToDispatchAgeMs_);
            shmSourceToDispatchWindowMaxMs_ = std::max(
                shmSourceToDispatchWindowMaxMs_, shmSourceToDispatchAgeMs_);
        } else {
            ++shmPresentationTimestampRejects_;
        }
    }
    const int frameNumber = batch.mappedFrameNumber > 0
        ? batch.mappedFrameNumber : batch.eventFrameNumber;
    if (priorFramesRead == 0
        || (shmFramesRead_ / 300) > (priorFramesRead / 300)) {
        emit setupMessage(QStringLiteral(
                              "SHM preview frame read: count=%1 frame=%2 %3x%4 "
                              "source_to_read_ms=%5 read_to_dispatch_ms=%6 "
                              "source_to_dispatch_ms=%7 timestamp_rejects=%8")
                              .arg(shmFramesRead_)
                              .arg(frameNumber)
                              .arg(batch.image.width())
                              .arg(batch.image.height())
                              .arg(shmSourceToReadAgeMs_, 0, 'f', 1)
                              .arg(shmReadToDispatchAgeMs_, 0, 'f', 1)
                              .arg(shmSourceToDispatchAgeMs_, 0, 'f', 1)
                              .arg(shmPresentationTimestampRejects_));
    }
    queuePreviewFrame(batch.image, frameNumber);
}

void RemotePlaySession::armMeterGate(const QString& source, quint64 physicalShotEpoch)
{
    const QString encodedEpoch = encodePoseArmToken(physicalShotEpoch);
    if (encodedEpoch.isEmpty()) {
        emit setupMessage(QStringLiteral("Refused shot_gate_arm with invalid physical-shot epoch"));
        return;
    }
    const QString normalized = source.trimmed().left(24);
    const QString effectiveSource =
        normalized.isEmpty() ? QStringLiteral("hw") : normalized;
    const bool sent = sendSidecarCommand({{"cmd", "shot_gate_arm"},
                                          {"source", effectiveSource},
                                          {"shot_epoch", encodedEpoch}});
    // PERMANENT t=0 ARM FORENSICS (one bounded line per physical shot edge). This command is
    // the reader's early wake-up (Path A) and used to be completely silent, which made "was
    // the reader armed during acquisition?" unanswerable from a session log (the visible
    // "POSE ARM" line belongs to the LATER pose_arm command at shot-begin). Pairs with the
    // orchestrator's "SHOT-GATE ARM RECEIPT" line: a send without a receipt = pipe loss; a
    // "Physical shot epoch" line without this send = the controller-guard blocked the arm.
    emit setupMessage(QStringLiteral("shot_gate_arm send: epoch=%1 source=%2 sent=%3")
                          .arg(physicalShotEpoch)
                          .arg(effectiveSource)
                          .arg(sent ? 1 : 0));
}

bool RemotePlaySession::recoverInputLink()
{
    if (state_ != RemotePlayState::Running || inputRecoveryPending_) {
        return false;
    }

    const QString remotePlayExecutable = resolveChiakiPath(config_, rootDir_);
    if (remotePlayExecutable.isEmpty()
        || !reportRemotePlayExecutableIdentity(
            remotePlayExecutable, QStringLiteral("input-recovery"))) {
        rejectLateInputRecoveryReady_ = true;
        setState(RemotePlayState::Error,
                 QStringLiteral("Chiaki input recovery blocked: trusted client image unavailable"));
        return false;
    }

    const quint64 generation = ++inputRecoveryGeneration_;
    inputRecoveryPending_ = true;
    rejectLateInputRecoveryReady_ = false;
    if (!sendSidecarCommand({{"cmd", "recover_input"}})) {
        inputRecoveryPending_ = false;
        rejectLateInputRecoveryReady_ = true;
        setState(RemotePlayState::Error,
                 QStringLiteral("Chiaki input recovery command could not reach the sidecar."));
        return false;
    }

    // Revoke route authority synchronously with the recovery request. Waiting for the sidecar's
    // asynchronous `begin` event would leave a small Running window after the pipe was known lost.
    setState(RemotePlayState::Connecting,
             QStringLiteral("Recovering console input session"));
    QTimer::singleShot(kInputSessionRecoveryDeadlineMs, this, [this, generation]() {
        if (!inputRecoveryDeadlineApplies(
                generation, inputRecoveryGeneration_, inputRecoveryPending_,
                state_ == RemotePlayState::Connecting)) {
            return;
        }
        inputRecoveryPending_ = false;
        rejectLateInputRecoveryReady_ = true;
        emit setupMessage(QStringLiteral("Chiaki input recovery timed out after %1 ms; late readiness "
                                         "from this attempt will be ignored.")
                              .arg(kInputSessionRecoveryDeadlineMs));
        setState(RemotePlayState::Error,
                 QStringLiteral("Chiaki input recovery did not prove a current console session; "
                                "automation remains disabled."));
    });
    return true;
}

bool RemotePlaySession::submitPreviewFrameChunkLine(const char* data, qsizetype len)
{
    if (data == nullptr || len <= 0) return false;
    const PreviewFrameChunkWireRecord wire =
        parsePreviewFrameChunkLine(QByteArrayView(data, len));
    if (!wire.valid) return false;

    PreviewChunkResult assembled = previewChunks_.push(
        wire.chunkFrameId, wire.frameNumber, wire.chunkIndex, wire.chunkCount, wire.payload);
    if (assembled.complete) {
        submitPreviewPayload(std::move(assembled.jpegBase64), assembled.frameNumber);
    }
    return true;
}

bool RemotePlaySession::submitPreviewFrameLine(const char* data, qsizetype len)
{
    // Byte-slice the payload straight out of the receive buffer. The old path pushed the whole line
    // through QJsonDocument::fromJson (a full parse in which the base64 became a UTF-16 QString =
    // 2x the bytes) and then QJsonValue::toString() (another full copy) - on the GUI thread, in
    // competition with QML paint. Base64 is drawn from [A-Za-z0-9+/=], so the value can never
    // contain a quote or an escape and the first '"' after the key genuinely ends it.
    static const QByteArray kJpegKey = QByteArrayLiteral("\"jpeg_b64\":\"");
    static const QByteArray kNumKey = QByteArrayLiteral("\"frame_number\":");
    if (data == nullptr || len <= 0) return false;
    const QByteArray view = QByteArray::fromRawData(data, len);
    const qsizetype k = view.indexOf(kJpegKey);
    if (k < 0) return false;
    const qsizetype b0 = k + kJpegKey.size();
    const qsizetype b1 = view.indexOf('"', b0);
    if (b1 < 0 || b1 < b0) return false;

    int frameNum = 0;
    const qsizetype nk = view.indexOf(kNumKey, b1);
    if (nk >= 0) {
        qsizetype vs = nk + kNumKey.size();
        while (vs < len && data[vs] == ' ') ++vs;          // tolerate a pretty-printed emitter
        qsizetype ve = vs;
        if (ve < len && data[ve] == '-') ++ve;
        while (ve < len && data[ve] >= '0' && data[ve] <= '9') ++ve;
        if (ve > vs) {
            bool ok = false;
            // Deep copy of the few digit bytes (NOT view.mid(), which can stay backed by the
            // non-null-terminated raw data) so the integer parse is unambiguously bounded.
            const int parsed = QByteArray(data + vs, ve - vs).toInt(&ok);
            if (ok) frameNum = parsed;
        }
    }

    if (b1 == b0) {
        // Empty payload: the old code's `if (!b64.isEmpty())` skipped the submit AND the lag
        // update. Same here - the line is understood, there is just nothing to show.
        return true;
    }

    submitPreviewPayload(QByteArray(data + b0, b1 - b0), frameNum);
    return true;
}

void RemotePlaySession::boundSidecarBuffer()
{
    if (sidecarBuffer_.size() <= kSidecarBufferMaxBytes) return;
    // Every COMPLETE line has already been consumed by the caller, so this can only ever be an
    // unterminated fragment: either the sidecar stopped emitting newlines or the stream is
    // corrupt. Dropping it resynchronises at the next '\n' instead of growing without limit.
    const qsizetype dropped = sidecarBuffer_.size();
    sidecarBuffer_.clear();
    previewChunks_.reset();
    const qint64 nowMs = QDateTime::currentMSecsSinceEpoch();
    if (nowMs - lastSidecarBufferOverflowMs_ >= kSidecarWarnThrottleMs) {
        lastSidecarBufferOverflowMs_ = nowMs;
        emit setupMessage(QStringLiteral("Sidecar stdout: discarded %1 bytes of an unterminated "
                                         "line (over the %2 byte cap) - the message stream will "
                                         "resynchronise at the next newline.")
                              .arg(dropped)
                              .arg(static_cast<qint64>(kSidecarBufferMaxBytes)));
    }
}

void RemotePlaySession::onSidecarStdout()
{
    if (!sidecarProcess_) return;
    sidecarBuffer_.append(sidecarProcess_->readAllStandardOutput());
    if (sidecarDispatching_) {
        // Re-entered from a handler below. The bytes are safely appended; the outer call re-scans
        // the buffer at the top of its next pass, so dispatch stays strictly FIFO.
        return;
    }
    const ScopedFlag dispatching(sidecarDispatching_);

    for (;;) {
        // --- PASS 1: locate the last complete line, and the newest preview-frame line among them.
        // Nothing is parsed or copied here (indexOf('\n') is a memchr; the frame test reads at most
        // 64 bytes per line).
        qsizetype consumed = 0;        // bytes belonging to complete lines
        qsizetype newestFrameAt = -1;  // start offset of newest JPEG preview line
        qsizetype newestShmAt = -1;    // start offset of newest SHM notification
        {
            const char* const scan = sidecarBuffer_.constData();
            const qsizetype total = sidecarBuffer_.size();
            qsizetype start = 0;
            while (start < total) {
                const qsizetype nl = sidecarBuffer_.indexOf('\n', start);
                if (nl < 0) break;
                if (nl > start && isPreviewFrameLine(scan + start, nl - start)) {
                    newestFrameAt = start;
                } else if (nl > start && isPreviewShmLine(scan + start, nl - start)) {
                    newestShmAt = start;
                }
                start = nl + 1;
                consumed = start;
            }
        }
        if (consumed <= 0) {
            boundSidecarBuffer();
            break;
        }

        // Detach the complete lines from the member buffer BEFORE dispatching any of them, so the
        // raw pointers the dispatch loop holds stay valid whatever a handler does to
        // sidecarBuffer_. Only the trailing PARTIAL line is copied (usually nothing). This also
        // retires the old per-line `remove(0, nl + 1)`, which memmoved the entire remaining tail -
        // up to several hundred KB - once for every single line in the burst.
        QByteArray residual = sidecarBuffer_.mid(consumed);
        const QByteArray pending = std::move(sidecarBuffer_);
        sidecarBuffer_ = std::move(residual);
        boundSidecarBuffer();

        // --- PASS 2: dispatch IN ORDER. Non-frame events are parsed and handled exactly as before.
        // Preview lines OTHER than the newest of each transport are dropped unparsed. JPEG has a
        // one-deep decoder mailbox; SHM is a latest-frame mapping, so an older notification can only
        // re-read the same newest generation. Parsing/locking every stale notification during a GUI
        // backlog amplified the very presentation stall the mapping was meant to avoid.
        const char* const base = pending.constData();
        // A handler may tear the sidecar down (stopSidecar) or replace it (restartSidecar). The old
        // loop stopped there implicitly, because those paths clear sidecarBuffer_ out from under it;
        // this snapshot reproduces that exactly, so lines belonging to a session that no longer
        // exists are never delivered to the new one.
        QProcess* const dispatchProc = sidecarProcess_;
        qsizetype start = 0;
        while (start < consumed) {
            const qsizetype nl = pending.indexOf('\n', start);
            if (nl < 0 || nl >= consumed) break;
            const qsizetype len = nl - start;
            if (len > 0) {
                if (start == newestFrameAt) {
                    if (!submitPreviewFrameLine(base + start, len)) {
                        // Not the expected shape - fall back to the full parse so behaviour is
                        // byte-identical to the old path for anything this fast path can't read.
                        const QByteArray line = QByteArray(base + start, len).trimmed();
                        const QJsonDocument doc = QJsonDocument::fromJson(line);
                        if (doc.isObject()) handleSidecarMessage(doc.object());
                    }
                } else if (newestFrameAt >= 0 && isPreviewFrameLine(base + start, len)) {
                    // Superseded preview frame - already obsolete, never parsed.
                } else if (newestShmAt >= 0 && start != newestShmAt
                           && isPreviewShmLine(base + start, len)) {
                    // Superseded SHM notification. The newest line below reads the newest committed
                    // mapping generation, so this cannot discard unique pixels.
                } else if (submitPreviewFrameChunkLine(base + start, len)) {
                    // Fixed-order raw-byte fast path: no GUI-thread JSON DOM and
                    // no base64 bytes -> UTF-16 -> bytes conversion per 8 KiB chunk.
                } else {
                    const QByteArray line = QByteArray(base + start, len).trimmed();
                    if (!line.isEmpty()) {
                        const QJsonDocument doc = QJsonDocument::fromJson(line);
                        if (doc.isObject()) handleSidecarMessage(doc.object());
                    }
                }
            }
            start = nl + 1;
            if (sidecarProcess_ != dispatchProc) break;
        }
        if (sidecarProcess_ != dispatchProc) break;
        // Loop: a re-entrant call (or a handler that pumped the process) may have appended more.
    }
}

void RemotePlaySession::handleSidecarMessage(const QJsonObject& msg)
{
    const QString event = msg.value(QStringLiteral("event")).toString();

    if (event == QLatin1String("started")) {
        streamPromotePending_ = false;
        // A failed promotion can restore previewMode_ while the blocking Python launch call is
        // still unwinding. Reject its late `started` before the preview branch, otherwise it would
        // overwrite the actionable Error as an ordinary preview-start notification.
        if (rejectLateSidecarStarted_) {
            emit setupMessage(QStringLiteral("Ignored late sidecar started event because the "
                                             "same promotion already failed."));
            return;
        }
        if (previewMode_) {
            // Preview sidecar is up: card feed + detector are running but there is NO Chiaki /
            // Remote Play session, so stay logically Disconnected (Connect stays enabled).
            setState(RemotePlayState::Disconnected, QStringLiteral("Live capture preview - meter detection active"));
        } else {
            const bool hasInputReady = msg.contains(QStringLiteral("input_ready"));
            const bool inputReady = msg.value(QStringLiteral("input_ready")).toBool(false);
            if (!sidecarStartedHasInputAuthority(false, hasInputReady, inputReady)) {
                rejectLateSidecarStarted_ = true;
                setState(RemotePlayState::Error,
                         QStringLiteral("Remote Play input session was not proven ready; "
                                        "automation remains disabled."));
                restoreWarmPreviewAfterPromotionFailure();
                return;
            }
            rejectLateSidecarStarted_ = false;
            streamPromoteFromWarmPreview_ = false;
            setState(RemotePlayState::Running, QStringLiteral("Autogreen running - meter detection active"));
        }
    } else if (event == QLatin1String("stream_promote")) {
        // The sidecar acknowledged the warm-preview -> stream promotion and will report a
        // real verdict (started/error). A missing ack is rejected after the short protocol grace;
        // neither branch can promote from process liveness alone.
        if (msg.value(QStringLiteral("state")).toString() == QLatin1String("begin")
                && streamPromotePending_) {
            streamPromoteAcked_ = true;
            emit setupMessage(QStringLiteral("Stream promotion started - waiting for the sidecar's "
                                             "Chiaki/input result."));
        }
    } else if (event == QLatin1String("input_recovery")) {
        const QString recoveryState = msg.value(QStringLiteral("state")).toString();
        if (recoveryState == QLatin1String("begin")) {
            if (!inputRecoveryPending_ || rejectLateInputRecoveryReady_
                    || state_ != RemotePlayState::Connecting) {
                emit setupMessage(QStringLiteral("Ignored input-recovery begin without a current "
                                                 "pending recovery attempt."));
                return;
            }
            // recoverInputLink() already moved to Connecting synchronously; refresh only the
            // explanatory status once the sidecar confirms it received the command.
            setState(RemotePlayState::Connecting,
                      QStringLiteral("Recovering console input session"));
            emit setupMessage(QStringLiteral("Recovering Chiaki input link in place; live capture "
                                              "and meter detection remain active."));
        } else if (recoveryState == QLatin1String("ready")) {
            const bool hasInputReady = msg.contains(QStringLiteral("input_ready"));
            const bool inputReady = msg.value(QStringLiteral("input_ready")).toBool(false);
            if (!shouldAcceptInputRecoveryReady(
                    inputRecoveryPending_, rejectLateInputRecoveryReady_,
                    state_ == RemotePlayState::Connecting, hasInputReady, inputReady)) {
                if (!inputRecoveryPending_ || rejectLateInputRecoveryReady_
                        || state_ != RemotePlayState::Connecting) {
                    emit setupMessage(QStringLiteral("Ignored late/out-of-scope input-recovery ready "
                                                     "verdict."));
                    return;
                }
                inputRecoveryPending_ = false;
                rejectLateInputRecoveryReady_ = true;
                setState(RemotePlayState::Error,
                          QStringLiteral("Recovered input child did not prove a fresh console session."));
                return;
            }
            inputRecoveryPending_ = false;
            rejectLateInputRecoveryReady_ = false;
            setState(RemotePlayState::Running,
                      QStringLiteral("Console input session recovered"));
            emit setupMessage(QStringLiteral("Chiaki input child relaunched; waiting for the direct "
                                              "controller pipe to reconnect."));
        } else if (recoveryState == QLatin1String("error")) {
            if (!inputRecoveryPending_ || rejectLateInputRecoveryReady_
                    || state_ != RemotePlayState::Connecting) {
                emit setupMessage(QStringLiteral("Ignored late/out-of-scope input-recovery error "
                                                 "verdict."));
                return;
            }
            inputRecoveryPending_ = false;
            rejectLateInputRecoveryReady_ = true;
            setState(RemotePlayState::Error,
                      msg.value(QStringLiteral("msg")).toString(
                          QStringLiteral("Chiaki input recovery failed.")));
            emit setupMessage(QStringLiteral("Chiaki input recovery failed: %1")
                                  .arg(msg.value(QStringLiteral("msg")).toString(
                                      QStringLiteral("unknown input-link error"))));
        }
    } else if (event == QLatin1String("capabilities")) {
        // FIX 2: what this install can actually do. Only `no_meter_available` today. Keys that are
        // absent leave the current value alone, so an older/partial payload changes nothing.
        if (msg.contains(QStringLiteral("no_meter_available"))) {
            const bool available = msg.value(QStringLiteral("no_meter_available")).toBool(true);
            const QString reason = msg.value(QStringLiteral("no_meter_reason")).toString();
            if (available != noMeterAvailable_ || reason != noMeterUnavailableReason_) {
                noMeterAvailable_ = available;
                noMeterUnavailableReason_ = reason;
                emit sidecarCapabilitiesChanged(noMeterAvailable_, noMeterUnavailableReason_);
            }
            if (!available) {
                // Always log it (not just on change): "No Meter" quietly doing nothing is the
                // failure mode, so the reason must be in every session's log.
                emit setupMessage(QStringLiteral("No-Meter (skele) mode is NOT available on this "
                                                 "install: %1").arg(reason.isEmpty()
                                                                        ? QStringLiteral("pose dependencies/models missing")
                                                                        : reason));
                if (config_.noMeterEnabled) {
                    emit setupMessage(QStringLiteral("WARNING: No-Meter mode is ENABLED but cannot "
                                                     "run - shot timing will not fire. Switch back "
                                                     "to meter detection."));
                }
            }
        }
    } else if (event == QLatin1String("error")) {
        const QString errMsg = msg.value(QStringLiteral("msg")).toString();
        // FIX 1: an error during a promotion is the failed-promotion verdict. Clearing the pending
        // flag stops the deadline timer from double-reporting; setState(Error) already prevents the
        // protocol-grace timer (it only acts while Connecting).
        const bool failedPromotion = streamPromotePending_;
        if (failedPromotion) {
            streamPromotePending_ = false;
            rejectLateSidecarStarted_ = true;
            emit setupMessage(QStringLiteral("Stream promotion FAILED - no input will reach the "
                                             "console: %1").arg(errMsg));
        }
        setState(RemotePlayState::Error, errMsg.isEmpty() ? QStringLiteral("Autogreen error") : errMsg);
        if (failedPromotion) {
            restoreWarmPreviewAfterPromotionFailure();
        }
    } else if (event == QLatin1String("stopped")) {
        setState(RemotePlayState::Disconnected, QStringLiteral("Autogreen stopped"));
    } else if (event == QLatin1String("preview_transport")) {
        handlePreviewTransportHandoff(msg);
    } else if (event == QLatin1String("frame_shm")) {
        if (shmFallbackRequested_ || !shmSourceActive_) {
            // One final notification may already be queued while the sidecar
            // applies preview_transport=jpeg. Do not reopen a retired mapping.
            return;
        }
        const int eventFrameNumber = msg.value(QStringLiteral("frame_number")).toInt(0);
        // Compatibility/fault fallback for an older sidecar or a writer whose
        // named-event setup/signal failed. Healthy negotiated SHM never emits
        // this per-frame JSON line.
        shmPump_.submitFrameNotification(shmSourceEpoch_, eventFrameNumber);
    } else if (event == QLatin1String("frame_chunk")) {
        const int frameNum = msg.value(QStringLiteral("frame_number")).toInt(0);
        const int chunkFrameId = msg.value(QStringLiteral("chunk_frame_id")).toInt(0);
        const int chunkIndex = msg.value(QStringLiteral("chunk_index")).toInt(-1);
        const int chunkCount = msg.value(QStringLiteral("chunk_count")).toInt(0);
        const QByteArray chunk = msg.value(QStringLiteral("jpeg_b64")).toString().toLatin1();
        PreviewChunkResult assembled = previewChunks_.push(
            chunkFrameId, frameNum, chunkIndex, chunkCount, QByteArrayView(chunk));
        if (assembled.complete) {
            submitPreviewPayload(std::move(assembled.jpegBase64), assembled.frameNumber);
        }
    } else if (event == QLatin1String("frame")) {
        const QString b64 = msg.value(QStringLiteral("jpeg_b64")).toString();
        const int frameNum = msg.value(QStringLiteral("frame_number")).toInt(0);
        if (!b64.isEmpty()) {
            // Legacy full-JSON fallback. Heavy decode remains on FrameDecoder's worker; decoded
            // QImage + frame id then enter the same bounded presenter used by SHM. previewLag_ is
            // computed only when that pair is actually handed to QML.
            frameDecoder_->submit(b64.toLatin1(), frameNum);
        }
    } else if (event == QLatin1String("latency_route_attestation_ack")) {
        const QString route = msg.value(QStringLiteral("delivery_route"))
                                  .toString().trimmed().toLower();
        quint64 generation = 0;
        const bool canonicalGeneration = decodePoseArmToken(
            msg.value(QStringLiteral("attestation_generation")), &generation);
        const bool validRoute = route == QLatin1String("pipe")
            || route == QLatin1String("vigem_ds4")
            || route == QLatin1String("vigem_xusb");
        if (!canonicalGeneration || !validRoute) {
            emit setupMessage(QStringLiteral(
                "Dropped malformed latency route acknowledgement from sidecar"));
            return;
        }
        const bool accepted = msg.value(QStringLiteral("accepted")).isBool()
            && msg.value(QStringLiteral("accepted")).toBool(false);
        quint64 scopeEpoch = 0;
        const bool canonicalScopeEpoch = decodePoseArmToken(
            msg.value(QStringLiteral("scope_epoch")), &scopeEpoch);
        const QString scopeDigest = msg.value(QStringLiteral("scope_digest"))
                                        .toString().trimmed().toLower();
        const bool validDigest = scopeDigest.size() == 64
            && std::all_of(scopeDigest.cbegin(), scopeDigest.cend(), [](QChar ch) {
                return ch.isDigit() || (ch >= QLatin1Char('a') && ch <= QLatin1Char('f'));
            });
        if (accepted && (!validDigest || !canonicalScopeEpoch)) {
            emit setupMessage(QStringLiteral(
                "Dropped accepted latency route acknowledgement without a valid scope binding"));
            return;
        }
        emit latencyRouteAttestationAck(
            accepted, route, generation, accepted ? scopeEpoch : 0,
            accepted ? scopeDigest : QString(),
            msg.value(QStringLiteral("reason")).toString().left(160));
    } else if (event == QLatin1String("telemetry")) {
        // Every authoritative telemetry stream carries the continuously
        // revocable session bit. Preview telemetry arrives while Disconnected
        // and is intentionally allowed to report false. Once Running, missing
        // evidence or an ended Chiaki session revokes authority immediately,
        // then gets one bounded input-only repair. recoverInputLink()
        // synchronously enters Connecting (which disarms automation), latches a
        // generation-scoped pending attempt, and preserves the capture sidecar.
        const bool hasInputReady = msg.contains(QStringLiteral("input_ready"));
        const bool inputReady = msg.value(QStringLiteral("input_ready")).toBool(false);
        if (shouldAutoRecoverInputFromTelemetry(
                state_ == RemotePlayState::Running, inputRecoveryPending_,
                hasInputReady, inputReady)) {
            emit setupMessage(QStringLiteral("Remote Play input readiness was lost; attempting one "
                                             "bounded in-place input recovery while capture stays live."));
            recoverInputLink();
            return;
        }
        bool changed = false;
        const int fps = msg.value(QStringLiteral("fps")).toInt();
        if (fps != sidecarFps_) { sidecarFps_ = fps; changed = true; }
        const int requested = msg.value(QStringLiteral("requested_fps")).toInt(requestedFps_);
        const int capture = msg.value(QStringLiteral("capture_loop_fps")).toInt(captureLoopFps_);
        const int unique = msg.value(QStringLiteral("unique_frame_fps")).toInt(uniqueFrameFps_);
        const double duplicatePct = msg.value(QStringLiteral("duplicate_frame_pct")).toDouble(duplicateFramePct_);
        const double frameAge = msg.value(QStringLiteral("frame_age_ms")).toDouble(frameAgeMs_);
        // Detector eligibility and raw transport liveness are separate clocks. A legitimate dark
        // loading screen can invalidate the former while frames still arrive continuously.
        const double pixelAge = msg.value(QStringLiteral("pixel_age_ms")).toDouble(pixelAgeMs_);
        // STAGE SPLIT (diagnostic only; feeds no gate, no release decision).
        // emit_ts_ms is stamped in autogreen_sidecar._emit immediately before the emit lock,
        // in the same Unix-epoch domain as measurement_capture_ts_ms. receipt - emit isolates
        // serialise + emit-lock contention + stdout pipe + this event loop from the sidecar's
        // own detect/build cost, which is the split that was missing when two sessions ran
        // +19ms staleness. An absent stamp (older sidecar) leaves the member at -1 and logs
        // nothing, so this can never fabricate a number.
        {
            const double emitTs = msg.value(QStringLiteral("emit_ts_ms")).toDouble(0.0);
            const qint64 nowEpochMs = QDateTime::currentMSecsSinceEpoch();
            if (std::isfinite(emitTs) && emitTs > 0.0) {
                const double transit = static_cast<double>(nowEpochMs) - emitTs;
                // Clamp obvious clock nonsense rather than logging a fantasy: a negative or
                // multi-second value means the two clocks disagree, not that transit was fast.
                if (transit >= 0.0 && transit < 5000.0) {
                    sidecarEmitTransitMs_ = transit;
                    emitTransitSamples_.push_back(transit);
                }
            }
            if (lastEmitTransitLogMs_ == 0) {
                lastEmitTransitLogMs_ = nowEpochMs;
            } else if (nowEpochMs - lastEmitTransitLogMs_ >= kEmitTransitLogIntervalMs
                       && emitTransitSamples_.size() >= 8) {
                lastEmitTransitLogMs_ = nowEpochMs;
                std::vector<double> s = emitTransitSamples_;
                emitTransitSamples_.clear();
                std::sort(s.begin(), s.end());
                const auto at = [&s](double q) {
                    return s[std::min(s.size() - 1,
                                      static_cast<std::size_t>(q * static_cast<double>(s.size())))];
                };
                emit setupMessage(QStringLiteral(
                            "Telemetry stage split: n=%1 emit->receipt p50=%2ms p90=%3ms max=%4ms "
                            "| detector frame age=%5ms pixel age=%6ms")
                            .arg(static_cast<int>(s.size()))
                            .arg(at(0.50), 0, 'f', 2)
                            .arg(at(0.90), 0, 'f', 2)
                            .arg(s.back(), 0, 'f', 2)
                            .arg(frameAge, 0, 'f', 1)
                            .arg(pixelAge, 0, 'f', 1));
            }
        }
        const double transportAge = msg.value(QStringLiteral("transport_age_ms"))
                                        .toDouble(transportAgeMs_);
        double capturePublicationAge =
            msg.value(QStringLiteral("capture_publication_age_ms"))
                .toDouble(capturePublicationAgeMs_);
        // This is backend-queue observability only. Keep malformed/cross-domain
        // telemetry bounded and never let it enter detector or release authority.
        if (!std::isfinite(capturePublicationAge)
            || capturePublicationAge < 0.0 || capturePublicationAge > 60'000.0) {
            capturePublicationAge = 0.0;
        }
        const bool backendFrozen = msg.value(QStringLiteral("backend_frozen"))
                                       .toBool(backendFrozen_);
        if (requested != requestedFps_) {
            requestedFps_ = requested;
            updatePreviewPresentationCadence(true);
            changed = true;
        }
        if (capture != captureLoopFps_) { captureLoopFps_ = capture; changed = true; }
        const qint64 rateObservationMs = previewPresentationClock_.isValid()
            ? previewPresentationClock_.nsecsElapsed() / 1'000'000LL : 0;
        if (previewPresentationRateClass_.observeCaptureFps(
                captureLoopFps_, rateObservationMs)) {
            updatePreviewPresentationCadence();
        }
        if (unique != uniqueFrameFps_) { uniqueFrameFps_ = unique; changed = true; }
        if (std::abs(duplicatePct - duplicateFramePct_) > 0.1) { duplicateFramePct_ = duplicatePct; changed = true; }
        if (std::abs(frameAge - frameAgeMs_) > 0.5) { frameAgeMs_ = frameAge; changed = true; }
        if (std::abs(pixelAge - pixelAgeMs_) > 0.5) { pixelAgeMs_ = pixelAge; changed = true; }
        if (std::abs(transportAge - transportAgeMs_) > 0.5) {
            transportAgeMs_ = transportAge;
            changed = true;
        }
        if (std::abs(capturePublicationAge - capturePublicationAgeMs_) > 0.5) {
            capturePublicationAgeMs_ = capturePublicationAge;
            changed = true;
        }
        if (backendFrozen != backendFrozen_) { backendFrozen_ = backendFrozen; changed = true; }
        // FIX 3: preview frames the sidecar LOST to a fault (not the deliberate latest-wins shed).
        // Monotonic; absent on an older sidecar, in which case toInt() keeps the current value and
        // nothing is ever logged. Only a genuine INCREASE is reported, and at most once per 10 s,
        // so a persistent fault is visible without flooding the activity log at 60 Hz.
        const int previewDropped = msg.value(QStringLiteral("preview_dropped")).toInt(previewDroppedFrames_);
        if (previewDropped > previewDroppedFrames_) {
            const int delta = previewDropped - previewDroppedFrames_;
            previewDroppedFrames_ = previewDropped;
            const qint64 nowDropMs = QDateTime::currentMSecsSinceEpoch();
            if (nowDropMs - lastPreviewDropLogMs_ >= kPreviewDropLogThrottleMs) {
                lastPreviewDropLogMs_ = nowDropMs;
                emit setupMessage(QStringLiteral("Preview frames dropped by the detector sidecar: "
                                                 "+%1 (total %2) - see the sidecar log for the fault")
                                      .arg(delta)
                                      .arg(previewDroppedFrames_));
            }
        }
        if (frameDecoder_) {
            const qint64 nowPipelineMs = QDateTime::currentMSecsSinceEpoch();
            if (lastPreviewPipelineStatsMs_ == 0) {
                const FrameDecoderStats stats = frameDecoder_->stats();
                lastPreviewPipelineStatsMs_ = nowPipelineMs;
                lastShmFramesReadCount_ = shmFramesRead_;
                lastPreviewSubmittedCount_ = stats.submitted;
                lastPreviewDecodedCount_ = stats.decoded;
                lastPreviewPresentedCount_ = stats.presented;
                lastPreviewMailboxDropCount_ = stats.decodeMailboxDropped;
                lastPreviewPresentationDropCount_ = stats.presentationMailboxDropped;
                lastPreviewDisplayPresentedCount_ = previewPresentationFrames_;
                lastPreviewDisplayDroppedCount_ = previewPresentationDroppedFrames_;
                lastPreviewDisplayUnderflowCount_ = previewPresentationUnderflows_;
                previewPresentationWindowMaxGapNs_ = 0;
                previewPresentationWindowMaxRenderTickGapNs_ = 0;
                shmSourceToReadWindowMaxMs_ = 0.0;
                shmReadToDispatchWindowMaxMs_ = 0.0;
                shmSourceToDispatchWindowMaxMs_ = 0.0;
            } else if (nowPipelineMs - lastPreviewPipelineStatsMs_
                       >= kPreviewPipelineStatsIntervalMs) {
                const FrameDecoderStats stats = frameDecoder_->stats();
                const double seconds = std::max(
                    0.001, static_cast<double>(nowPipelineMs - lastPreviewPipelineStatsMs_) / 1000.0);
                if (shmReaderOpen_ && !shmFallbackRequested_) {
                    const quint64 displayDelta =
                        previewPresentationFrames_ - lastPreviewDisplayPresentedCount_;
                    const quint64 jitterDropDelta =
                        previewPresentationDroppedFrames_ - lastPreviewDisplayDroppedCount_;
                    const quint64 underflowDelta =
                        previewPresentationUnderflows_ - lastPreviewDisplayUnderflowCount_;
                    // `present_fps` is the actual paced QML handoff. Keep source reads separate so
                    // a healthy mapping followed by a starved presenter is visible in one line.
                    emit setupMessage(QStringLiteral(
                        "preview_pipeline: transport=shm present_fps=%1 total=%2 "
                        "open_fail_run=%3 read_fault_run=%4 source_fps=%5 source_total=%6 "
                        "jitter_drop=%7 underflow=%8 depth=%9 max_depth=%10 present_gap_max_ms=%11 "
                        "event_wait_timeouts=%12 event_wait_failures=%13 "
                        "event_generation_probes=%14 event_notification_losses=%15 "
                        "ready_frame_replaced=%16 delivery_schedule_failures=%17 "
                        "render_clock=%18 render_tick_gap_max_ms=%19 "
                        "source_read_max_ms=%20 read_dispatch_max_ms=%21 "
                        "source_dispatch_max_ms=%22 timestamp_rejects=%23")
                        .arg(displayDelta / seconds, 0, 'f', 1)
                        .arg(previewPresentationFrames_)
                        .arg(shmOpenFailures_)
                        .arg(shmReadFailures_)
                        .arg((shmFramesRead_ - lastShmFramesReadCount_) / seconds, 0, 'f', 1)
                        .arg(shmFramesRead_)
                        .arg(jitterDropDelta)
                        .arg(underflowDelta)
                        .arg(previewPresentationBuffer_.depth())
                        .arg(previewPresentationMaxDepth_)
                        .arg(previewPresentationWindowMaxGapNs_ / 1'000'000.0, 0, 'f', 1)
                        .arg(shmEventWaitTimeouts_)
                        .arg(shmEventWaitFailures_)
                        .arg(shmEventGenerationProbes_)
                        .arg(shmEventNotificationLosses_)
                        .arg(shmReadyFrameReplaced_)
                        .arg(shmDeliveryScheduleFailures_)
                        .arg(previewPresentationRenderClockActive_ ? 1 : 0)
                        .arg(previewPresentationWindowMaxRenderTickGapNs_ / 1'000'000.0,
                             0, 'f', 1)
                        .arg(shmSourceToReadWindowMaxMs_, 0, 'f', 1)
                        .arg(shmReadToDispatchWindowMaxMs_, 0, 'f', 1)
                        .arg(shmSourceToDispatchWindowMaxMs_, 0, 'f', 1)
                        .arg(shmPresentationTimestampRejects_));
                } else {
                    const quint64 displayDelta =
                        previewPresentationFrames_ - lastPreviewDisplayPresentedCount_;
                    emit setupMessage(QStringLiteral(
                        "preview_pipeline: transport=jpeg rx_fps=%1 decode_fps=%2 present_fps=%3 "
                        "decode_mailbox_drop=%4 gui_mailbox_drop=%5 totals=%6/%7/%8/%9/%10 "
                        "decoded_handoff_fps=%11 jitter_drop=%12 underflow=%13 depth=%14 "
                        "max_depth=%15 present_gap_max_ms=%16 render_clock=%17 "
                        "render_tick_gap_max_ms=%18")
                        .arg((stats.submitted - lastPreviewSubmittedCount_) / seconds, 0, 'f', 1)
                        .arg((stats.decoded - lastPreviewDecodedCount_) / seconds, 0, 'f', 1)
                        .arg(displayDelta / seconds, 0, 'f', 1)
                        .arg(stats.decodeMailboxDropped - lastPreviewMailboxDropCount_)
                        .arg(stats.presentationMailboxDropped - lastPreviewPresentationDropCount_)
                        .arg(stats.submitted)
                        .arg(stats.decoded)
                        .arg(previewPresentationFrames_)
                        .arg(stats.decodeMailboxDropped)
                        .arg(stats.presentationMailboxDropped)
                        .arg((stats.presented - lastPreviewPresentedCount_) / seconds, 0, 'f', 1)
                        .arg(previewPresentationDroppedFrames_ - lastPreviewDisplayDroppedCount_)
                        .arg(previewPresentationUnderflows_ - lastPreviewDisplayUnderflowCount_)
                        .arg(previewPresentationBuffer_.depth())
                        .arg(previewPresentationMaxDepth_)
                        .arg(previewPresentationWindowMaxGapNs_ / 1'000'000.0, 0, 'f', 1)
                        .arg(previewPresentationRenderClockActive_ ? 1 : 0)
                        .arg(previewPresentationWindowMaxRenderTickGapNs_ / 1'000'000.0,
                             0, 'f', 1));
                }
                lastPreviewPipelineStatsMs_ = nowPipelineMs;
                lastShmFramesReadCount_ = shmFramesRead_;
                lastPreviewSubmittedCount_ = stats.submitted;
                lastPreviewDecodedCount_ = stats.decoded;
                lastPreviewPresentedCount_ = stats.presented;
                lastPreviewMailboxDropCount_ = stats.decodeMailboxDropped;
                lastPreviewPresentationDropCount_ = stats.presentationMailboxDropped;
                lastPreviewDisplayPresentedCount_ = previewPresentationFrames_;
                lastPreviewDisplayDroppedCount_ = previewPresentationDroppedFrames_;
                lastPreviewDisplayUnderflowCount_ = previewPresentationUnderflows_;
                previewPresentationWindowMaxGapNs_ = 0;
                previewPresentationWindowMaxRenderTickGapNs_ = 0;
                shmSourceToReadWindowMaxMs_ = 0.0;
                shmReadToDispatchWindowMaxMs_ = 0.0;
                shmSourceToDispatchWindowMaxMs_ = 0.0;
            }
        }
        const int capW = msg.value(QStringLiteral("capture_width")).toInt(captureWidth_);
        const int capH = msg.value(QStringLiteral("capture_height")).toInt(captureHeight_);
        const QString capTier = msg.value(QStringLiteral("capture_tier")).toString(captureTier_);
        if (capW != captureWidth_) { captureWidth_ = capW; changed = true; }
        if (capH != captureHeight_) { captureHeight_ = capH; changed = true; }
        if (capTier != captureTier_) { captureTier_ = capTier; changed = true; }
        QString probe = QStringLiteral("stable-60");
        if (requestedFps_ > 60) {
            probe = uniqueFrameFps_ > 70
                ? QStringLiteral("unique-fps-above-60")
                : QStringLiteral("remote-play-capped");
        }
        if (probe != fpsProbeState_) { fpsProbeState_ = probe; changed = true; }

        const int sidecarFrameCount = msg.value(QStringLiteral("frame_count")).toInt(lastSidecarDetectionFrameCount_);
        // FRAME-ID JOIN key: decoder frame seq of the detected frame (same counter the preview `frame`
        // stream stamps each JPEG with). Distinct from frame_count (the unique-frame dedup gate above).
        // -1 when an older sidecar omits it -> overlay falls back to latest-box behaviour.
        const int sidecarFrameNumber = msg.value(QStringLiteral("frame_number")).toInt(-1);
        // [E4] Remember it as the join partner for the preview stream's frame_number, so
        // previewLagFrames() subtracts two values of the SAME counter. Recorded independently of
        // the detection-emission gate below: preview lag is a transport measure, not a detection
        // one. Stays -1 on a sidecar that omits the field, which pins previewLag_ at 0.
        if (sidecarFrameNumber >= 0) {
            lastSidecarDetectionFrameNumber_ = sidecarFrameNumber;
        }
        const auto shot = msg.value(QStringLiteral("shot")).toObject();
        const double fill = shot.value(QStringLiteral("fill_pct")).toDouble();
        const double conf = shot.value(QStringLiteral("confidence")).toDouble();
        if (std::abs(fill - shotFillPct_) > 0.05) { shotFillPct_ = fill; changed = true; }
        if (std::abs(conf - shotConfidence_) > 0.005) { shotConfidence_ = conf; changed = true; }

        const bool gotoActive = msg.value(QStringLiteral("goto_active")).toBool();
        const bool gotoTrig = msg.value(QStringLiteral("goto_triggered")).toBool();
        QString newState = QStringLiteral("Idle");
        if (gotoTrig) newState = QStringLiteral("Released");
        else if (gotoActive) newState = QStringLiteral("Holding");
        if (newState != shotState_) { shotState_ = newState; changed = true; }

        // Merge the RTT sampler snapshot. At startup the sampler deliberately
        // targets the local console/gateway; that value is diagnostic only. It
        // becomes release-timing authority only after a public court endpoint is
        // locked and carried in this same snapshot.
        const auto rtt = msg.value(QStringLiteral("rtt")).toObject();
        if (!rtt.isEmpty()) {
            const double filtered = rtt.value(QStringLiteral("filtered_ms")).toDouble();
            const double jitter = rtt.value(QStringLiteral("jitter_ms")).toDouble();
            const double effective = rtt.value(QStringLiteral("effective_offset_ms")).toDouble();
            const double predicted = rtt.value(QStringLiteral("predicted_offset_ms")).toDouble(effective);
            const double packetInterval = rtt.value(QStringLiteral("packet_interval_ms")).toDouble(0.0);
            const double tickEta = rtt.value(QStringLiteral("next_tick_eta_ms")).toDouble(-1.0);
            const QString cIp = rtt.value(QStringLiteral("court_ip")).toString();
            const bool ready = rtt.value(QStringLiteral("ready")).toBool();
            const bool targetVerified = rtt.value(QStringLiteral("target_verified")).toBool(false);
            const bool phaseLocked = rtt.value(QStringLiteral("phase_locked")).toBool(false);
            const bool phaseSourceVerified = rtt.value(QStringLiteral("phase_source_verified")).toBool(false);
            const double phaseConfidence = rtt.value(QStringLiteral("phase_confidence")).toDouble(0.0);
            const QHostAddress courtAddress(cIp);
            const bool courtRttReady = ready && targetVerified && filtered > 0.0
                && !courtAddress.isNull() && courtAddress.isGlobal();
            telemetry_.rttMs = filtered;
            telemetry_.rttTargetVerified = courtRttReady;
            telemetry_.jitterMs = jitter;
            telemetry_.offsetMs = effective;
            telemetry_.syncAdjustMs = predicted > 0.0 ? predicted : effective;
            telemetry_.packetIntervalMs = packetInterval;
            const bool tickPhaseReady = courtRttReady && phaseLocked
                && phaseSourceVerified && phaseConfidence >= 0.55
                && tickEta >= 0.0 && tickEta <= 50.0;
            telemetry_.tickerLatencyMs = tickPhaseReady ? tickEta : 0.0;
            telemetry_.tickPhaseVerified = tickPhaseReady;
            telemetry_.tickPhaseConfidence = tickPhaseReady ? phaseConfidence : 0.0;
            telemetry_.tickPhaseObservedEpochMs = tickPhaseReady
                ? QDateTime::currentMSecsSinceEpoch() : 0;
            telemetry_.playingGame = sidecarFps_ > 0 || ready;
            telemetry_.syncSource = courtRttReady
                ? QStringLiteral("Court probe RTT")
                : (ready ? QStringLiteral("Local RTT (unverified)")
                         : QStringLiteral("Sidecar telemetry"));
            telemetry_.syncConfidence = courtRttReady ? 0.75 : 0.0;
            // Empty is an explicit revocation after a flow/session clear.  Do
            // not retain a court address from an earlier sidecar generation.
            telemetry_.courtIp = cIp;
            telemetry_.syncActive = courtRttReady;
        } else {
            // The sidecar intentionally omits this object if its sampler cannot
            // produce a snapshot.  Missing authority must revoke the previous
            // sample immediately; retaining the last object here allowed one
            // serialization/sampler fault to keep stale RTT and tick timing in
            // use indefinitely.
            telemetry_.courtIp.clear();
            telemetry_.rttMs = 0.0;
            telemetry_.rttTargetVerified = false;
            telemetry_.jitterMs = 0.0;
            telemetry_.offsetMs = 0.0;
            telemetry_.syncAdjustMs = 0.0;
            telemetry_.packetIntervalMs = 0.0;
            telemetry_.tickerLatencyMs = 0.0;
            telemetry_.tickPhaseVerified = false;
            telemetry_.tickPhaseConfidence = 0.0;
            telemetry_.tickPhaseObservedEpochMs = 0;
            telemetry_.syncSource = QStringLiteral("Sidecar telemetry (RTT unavailable)");
            telemetry_.syncConfidence = 0.0;
            telemetry_.syncActive = false;
        }
        telemetry_.playingGame = sidecarFps_ > 0
            || (!rtt.isEmpty() && rtt.value(QStringLiteral("ready")).toBool(false));
        emit telemetryReady(telemetry_);

        const auto tracking = msg.value(QStringLiteral("tracking")).toObject();
        const auto intelligence = msg.value(QStringLiteral("intelligence")).toObject();
        const auto fusion = msg.value(QStringLiteral("fusion")).toObject();
        const auto green = msg.value(QStringLiteral("green")).toObject();

        const double fusionFill = fusion.value(QStringLiteral("fill_pct")).toDouble(-1.0);
        const double trackingFill = tracking.value(QStringLiteral("fill_pct")).toDouble(-1.0);
        const double sidecarFill = fusionFill >= 0.0 ? fusionFill : (trackingFill >= 0.0 ? trackingFill : fill);
        const double sidecarConfidence = fusion.value(QStringLiteral("confidence")).toDouble(
            tracking.value(QStringLiteral("confidence")).toDouble(conf));
        const double sidecarVelocity = fusion.value(QStringLiteral("velocity_pct_s")).toDouble(
            tracking.value(QStringLiteral("velocity_pct_s")).toDouble(0.0));
        const double sidecarAccel = fusion.value(QStringLiteral("acceleration_pct_s2")).toDouble(
            tracking.value(QStringLiteral("acceleration_pct_s2")).toDouble(0.0));
        const double sidecarEta = fusion.value(QStringLiteral("eta_to_target_ms")).toDouble(
            tracking.value(QStringLiteral("eta_to_target_ms")).toDouble(-1.0));
        const double sidecarTarget = fusion.value(QStringLiteral("target_pct")).toDouble(96.0);
        // Freshness gate: the sample is fresh only when BOTH clocks are recent — the capture
        // clock (frame_age <= 350ms; the sidecar advances it on a GOOD, non-black frame) AND the
        // UNIQUE-pixel clock (pixel_age <= 200ms; time since the last non-duplicate frame). The old
        // gate ORed unique_frame_fps>0 with a confidence>=0.12 fallback — but a FROZEN echo pins
        // uniqueFrameFps at 0 while still reporting a high (0.91) confidence off the stale bar, so
        // that OR clause let a frozen echo pass freshness at uniqueFps=0 (exactly backwards). Gate on
        // pixel_age directly instead: a climbing pixel_age is an unambiguous "the picture is frozen"
        // even when the confidence is high. Sidecars that don't send pixel_age report 0 -> the pixel
        // term is inert and this degrades to the frame_age gate (no unique-fps/confidence OR).
        // DEAD-WIRE FIX: the sidecar has always emitted feed_healthy in this telemetry payload —
        // false once the capture card's content-stall detector has EXHAUSTED its reopen attempts,
        // i.e. the feed is genuinely dead rather than merely stale — but nothing under
        // native_orion/src ever parsed it, so the signal terminated at the JSON. AND it into the
        // freshness gate: on a dead feed no sample may be trusted, and in particular none may arm
        // a blind fire. Defaults TRUE when the field is absent (older sidecars), so this is
        // byte-identical to the previous behaviour unless the feed has actually died.
        const bool feedHealthy = msg.value(QStringLiteral("feed_healthy")).toBool(true);
        const double captureTsMs = msg.value(QStringLiteral("capture_ts_ms")).toDouble(0.0);
        const double measurementCaptureTsMs = msg.value(
            QStringLiteral("measurement_capture_ts_ms")).toDouble(0.0);
        constexpr double kMaxMeasurementClockDomainOffsetMs = 50.0;
        const bool coherentMeasurementClock = std::isfinite(captureTsMs)
            && captureTsMs > 0.0 && std::isfinite(measurementCaptureTsMs)
            && measurementCaptureTsMs > 0.0
            && std::abs(measurementCaptureTsMs - captureTsMs)
                <= kMaxMeasurementClockDomainOffsetMs;
        // Current sidecars publish one explicit timing epoch. Retain raw-age
        // compatibility for display/health with an older sidecar; autonomous
        // release authority independently requires measurementCaptureTsMs.
        const double frameAgeEpochMs = coherentMeasurementClock
            ? measurementCaptureTsMs : captureTsMs;
        const double effectiveFrameAgeMs = effectiveSidecarFrameAgeMs(
            frameAgeMs_, frameAgeEpochMs,
            static_cast<double>(QDateTime::currentMSecsSinceEpoch()));
        const bool sidecarFresh = std::isfinite(effectiveFrameAgeMs)
            && effectiveFrameAgeMs <= 350.0 && pixelAgeMs_ <= 200.0 && feedHealthy;
        // P1b: every frame now carries a top-level meter_present bool. On meter loss the sidecar emits
        // meter_present:false with fill=0/confidence=0, which the old (fill>0 || conf>0) gate REJECTED —
        // so a no-meter message was structurally impossible, the native engine never learned the meter
        // vanished, and OrionAppController's meterAppearMs_ HOLD clock never reset (grew to 293s+). Emit a
        // detected=false result whenever meter_present is false so the controller's meter-lost reset fires.
        // Default true for older sidecars that don't send the field (byte-identical to the old behaviour).
        const bool meterPresent = msg.value(QStringLiteral("meter_present")).toBool(true);
        // RC-2b (live-path bug #2): the frame_count dedupe must NOT drop a meter_present:false
        // emission — during a pixel-age stall the sidecar synthesizes it while frame_count is
        // frozen, and dropping it starves the meter-lost reset chain. Gate logic lives in
        // shouldAcceptSidecarDetectionEmission (SidecarWatchdog.h) so the tests can pin it.
        if (shouldAcceptSidecarDetectionEmission(sidecarFill > 0.0 || sidecarConfidence > 0.0,
                                                 meterPresent, sidecarFrameCount,
                                                 lastSidecarDetectionFrameCount_)) {
            lastSidecarDetectionFrameCount_ = sidecarFrameCount;
            DetectionResult sidecarResult;
            sidecarResult.detected = meterPresent && sidecarFresh && sidecarFill > 0.0 && sidecarConfidence >= 0.12;
            // Feed integrity is independent of whether this payload happens to contain a meter.
            // A newly-numbered detector-failure/no-meter payload from a dead or frozen capture
            // source must not masquerade as fresh capture proof and re-enable automation.
            sidecarResult.staleFrame = !sidecarFresh;
            sidecarResult.detectorSource = QStringLiteral("sidecar-fusion");
            sidecarResult.style = config_.meterStyle;
            sidecarResult.profileName = config_.meterStyle;
            sidecarResult.colorName = config_.meterColor;
            // raw_fed (default true for older sidecars) is false when THIS frame's fill
            // came from a meter_memory echo / roi_not_found rather than a clean raw
            // detection — i.e. a held/extrapolated sample. Mark it stale_or_memory so the
            // engine won't grant it freshness trust (Go-To release gate). Fill is kept so
            // the overlay still shows the last-known value.
            const bool rawFed = msg.value(QStringLiteral("raw_fed")).toBool(true);
            quint64 gameplayStructureEpoch = 0;
            const bool validStructureEpoch = decodePoseArmToken(
                msg.value(QStringLiteral("gameplay_structure_epoch")),
                &gameplayStructureEpoch);
            sidecarResult.gameplayStructureVerified = rawFed && validStructureEpoch
                && msg.value(QStringLiteral("gameplay_structure_verified")).toBool(false);
            sidecarResult.gameplayStructureEpoch = sidecarResult.gameplayStructureVerified
                ? gameplayStructureEpoch : 0;
            sidecarResult.rejectionReason = !meterPresent
                ? QStringLiteral("no_meter")
                : (sidecarResult.detected
                    ? (rawFed ? QString() : QStringLiteral("stale_or_memory"))
                    : (sidecarFresh ? QStringLiteral("confidence_low") : QStringLiteral("stale_frame")));
            // On meter loss, clear fill/confidence (and the derived tracking) so the overlay + engine see a
            // genuine no-meter sample, not a lingering last-known value that keeps the HOLD clock alive.
            sidecarResult.fillPct = meterPresent ? sidecarFill : 0.0;
            sidecarResult.confidence = meterPresent ? std::clamp(sidecarConfidence, 0.0, 1.0) : 0.0;
            sidecarResult.velocityPctS = meterPresent ? sidecarVelocity : 0.0;
            sidecarResult.accelerationPctS2 = meterPresent ? sidecarAccel : 0.0;
            sidecarResult.etaToGreenMs = meterPresent ? sidecarEta : -1.0;
            sidecarResult.targetPct = sidecarTarget;
            sidecarResult.frameAgeMs = effectiveFrameAgeMs;
            sidecarResult.consecutiveFrames = sidecarResult.detected ? 3 : 0;
            sidecarResult.releaseReady = fusion.value(QStringLiteral("release_ready")).toBool(false);
            // === Tip-timing IPC contract (Python sidecar per-frame payload fields) ===
            // [ORION_MEASURED_LEAD] The observed posterior is telemetry/calibration input only.
            // Autonomous actuation consumes the separate explicit authority kind/value/SD tuple.
            // [ORION_REG_FUSION] reg_tip_ms + reg_conf: registration time-to-TIP + confidence (far horizon).
            // Read at the top level of the frame payload; 0/-1 default when an older sidecar omits them,
            // so the engine's measured-lead / reg-fusion paths stay inert (byte-identical to today).
            const double legacyObservedLatencyMs = msg.value(
                QStringLiteral("measured_latency_ms")).toDouble(0.0);
            sidecarResult.measuredLatencyMs = msg.value(
                QStringLiteral("measured_latency_observed_ms"))
                    .toDouble(legacyObservedLatencyMs);
            sidecarResult.measuredLatencyAuthorityKind = msg.value(
                QStringLiteral("measured_latency_authority_kind"))
                    .toString(QStringLiteral("none")).trimmed().toLower().left(16);
            sidecarResult.measuredLatencyAuthorityMs = msg.value(
                QStringLiteral("measured_latency_authority_ms")).toDouble(0.0);
            sidecarResult.measuredLatencyAuthoritySdMs = msg.value(
                QStringLiteral("measured_latency_authority_sd_ms")).toDouble(0.0);
            // Retain the measured-latency CONFIDENCE + label count (previously dropped): the
            // prior->posterior blend needs them to weight the vision posterior's variance. A
            // bootstrapped (n==0) or absent measurement stays low-confidence -> the blend leans on
            // the clock prior. Older sidecars omit them -> 0/0 -> byte-identical to today.
            sidecarResult.measuredLatencyConf = msg.value(QStringLiteral("measured_latency_conf")).toDouble(0.0);
            sidecarResult.measuredLatencyN = msg.value(QStringLiteral("measured_latency_n")).toInt(0);
            sidecarResult.measuredLatencyControlledAnchor =
                msg.value(QStringLiteral("measured_latency_controlled_anchor")).toBool(false);
            sidecarResult.measuredLatencyProvisional =
                msg.value(QStringLiteral("measured_latency_provisional")).toBool(false);
            sidecarResult.measuredLatencyRestored =
                msg.value(QStringLiteral("measured_latency_restored")).toBool(false);
            sidecarResult.measuredLatencyVideoRouteAttested =
                msg.value(QStringLiteral("measured_latency_video_route_attested")).toBool(false);
            sidecarResult.measuredLatencyFactoryPrior =
                msg.value(QStringLiteral("measured_latency_factory_prior")).toBool(false);
            sidecarResult.measuredLatencyPriorSource = msg.value(
                QStringLiteral("measured_latency_prior_source"))
                    .toString().trimmed().left(112);
            sidecarResult.measuredLatencyModelVersion = msg.value(
                QStringLiteral("measured_latency_model_version"))
                    .toString().trimmed().left(32);
            quint64 latencyAttestationGeneration = 0;
            (void)decodePoseArmToken(
                msg.value(QStringLiteral("measured_latency_attestation_generation")),
                &latencyAttestationGeneration);
            const QString latencyDeliveryRoute = msg.value(
                QStringLiteral("measured_latency_delivery_route")).toString().trimmed().toLower();
            quint64 latencyScopeEpoch = 0;
            (void)decodePoseArmToken(
                msg.value(QStringLiteral("measured_latency_scope_epoch")),
                &latencyScopeEpoch);
            sidecarResult.measuredLatencyScopeEpoch = latencyScopeEpoch;
            if (latencyAttestationGeneration != 0
                && latencyDeliveryRoute == QLatin1String("pipe")) {
                sidecarResult.measuredLatencyAttestationGeneration =
                    latencyAttestationGeneration;
                sidecarResult.measuredLatencyDeliveryRoute =
                    LatencyControllerRoute::Pipe;
            } else if (latencyAttestationGeneration != 0
                       && latencyDeliveryRoute == QLatin1String("vigem_ds4")) {
                sidecarResult.measuredLatencyAttestationGeneration =
                    latencyAttestationGeneration;
                sidecarResult.measuredLatencyDeliveryRoute =
                    LatencyControllerRoute::VigemDs4;
            } else if (latencyAttestationGeneration != 0
                       && latencyDeliveryRoute == QLatin1String("vigem_xusb")) {
                sidecarResult.measuredLatencyAttestationGeneration =
                    latencyAttestationGeneration;
                sidecarResult.measuredLatencyDeliveryRoute =
                    LatencyControllerRoute::VigemXusb;
            }
            const int regSeq = msg.value(QStringLiteral("reg_seq")).toInt(-1);
            const double regTipMs = msg.value(QStringLiteral("reg_tip_ms")).toDouble(-1.0);
            const double regConf = msg.value(QStringLiteral("reg_conf")).toDouble(0.0);
            const double regRmsePp = msg.value(QStringLiteral("reg_rmse_pp")).toDouble(-1.0);
            const int regN = msg.value(QStringLiteral("reg_n")).toInt(0);
            const double regTipCaptureMs = msg.value(
                QStringLiteral("reg_tip_capture_ms")).toDouble(0.0);
            const double regSampleCaptureMs = msg.value(
                QStringLiteral("reg_sample_capture_ms")).toDouble(0.0);
            const double regSigmaMs = msg.value(
                QStringLiteral("reg_sigma_ms")).toDouble(-1.0);
            const QString regModelId = msg.value(QStringLiteral("reg_model_id"))
                                           .toString().trimmed().left(64);
            const QString regModelVersion = msg.value(
                QStringLiteral("reg_model_version")).toString().trimmed().left(32);
            const QString regFitMethod = msg.value(QStringLiteral("reg_fit_method"))
                                            .toString().trimmed().left(48);
            // Decoder registration and the top-level measurement timestamp share
            // one PTS-smoothed epoch. Raw capture_ts_ms remains identity-only;
            // exact reg_seq plus this clock equality proves a coherent same-frame fit.
            const bool coherentRegClock = std::isfinite(regTipCaptureMs)
                && std::isfinite(regSampleCaptureMs) && std::isfinite(regTipMs)
                && regTipCaptureMs > regSampleCaptureMs && regTipMs > 0.0
                && regTipMs <= 2000.0
                && std::abs((regTipCaptureMs - regSampleCaptureMs) - regTipMs) <= 2.0
                && coherentMeasurementClock
                && std::abs(regSampleCaptureMs - measurementCaptureTsMs) <= 2.0;
            const bool structuredRegValid = regSeq == sidecarFrameCount
                && coherentRegClock && std::isfinite(regConf)
                && regConf > 0.0 && regConf <= 1.0
                && std::isfinite(regRmsePp) && regRmsePp >= 0.0
                && std::isfinite(regSigmaMs) && regSigmaMs > 0.0
                && regSigmaMs <= 500.0 && regN >= 4
                && !regModelId.isEmpty() && !regModelVersion.isEmpty()
                && regFitMethod == QLatin1String("numpy_bounded_robust_v1");
            if (structuredRegValid) {
                sidecarResult.regSeq = regSeq;
                sidecarResult.regTipMs = regTipMs;
                sidecarResult.regConf = regConf;
                sidecarResult.regRmsePp = regRmsePp;
                sidecarResult.regN = regN;
                sidecarResult.regTipCaptureMs = regTipCaptureMs;
                sidecarResult.regSampleCaptureMs = regSampleCaptureMs;
                sidecarResult.regSigmaMs = regSigmaMs;
                sidecarResult.regModelId = regModelId;
                sidecarResult.regModelVersion = regModelVersion;
                sidecarResult.regFitMethod = regFitMethod;
            }
            // Post-hoc full-shot tip label (per completed shot) + oracle v2 posterior components.
            sidecarResult.posthocN = msg.value(QStringLiteral("posthoc_n")).toInt(0);
            sidecarResult.posthocTipMs = msg.value(QStringLiteral("posthoc_tip_ms")).toDouble(0.0);
            sidecarResult.posthocConf = msg.value(QStringLiteral("posthoc_conf")).toDouble(0.0);
            sidecarResult.measuredLFixedMs = msg.value(QStringLiteral("measured_l_fixed_ms")).toDouble(0.0);
            sidecarResult.measuredLatencySdMs = msg.value(QStringLiteral("measured_latency_sd_ms")).toDouble(0.0);
            // [Phase-2 A2(c)] console INPUT-tick phase (probe-run sawtooth fit; top-level payload
            // fields, NOT the rtt object's network tick_phase_ms). Absent -> -1/0/-1 defaults keep
            // the engine's earlier-only fused tick snap disengaged (byte-identical).
            sidecarResult.tickPhaseMs = msg.value(QStringLiteral("tick_phase_ms")).toDouble(-1.0);
            sidecarResult.tickPhaseConf = msg.value(QStringLiteral("tick_phase_conf")).toDouble(0.0);
            sidecarResult.tickPhaseSdMs = msg.value(QStringLiteral("tick_phase_sd_ms")).toDouble(-1.0);
            // Raw capture identity and the explicit measurement epoch remain
            // separate. Older sidecars stay display-compatible but cannot grant
            // strict autonomous timing authority without the measurement field.
            sidecarResult.captureTsMs = captureTsMs;
            sidecarResult.measurementCaptureTsMs = coherentMeasurementClock
                ? measurementCaptureTsMs : 0.0;
            sidecarResult.stage = msg.value(QStringLiteral("stage")).toString();
            // FRAME-ID JOIN: carry the decoder frame seq so the overlay composites this bbox on the
            // exact preview frame it was detected on (fixes drift/flicker from painting on a later image).
            sidecarResult.frameNumber = sidecarFrameNumber;
            sidecarResult.greenStartPct = green.value(QStringLiteral("start")).toDouble(-1.0);
            sidecarResult.greenEndPct = green.value(QStringLiteral("end")).toDouble(-1.0);
            sidecarResult.greenCenterPct = green.value(QStringLiteral("center")).toDouble(-1.0);
            sidecarResult.greenWidthPct = green.value(QStringLiteral("width")).toDouble(0.0);
            sidecarResult.greenConfidence = sidecarResult.greenCenterPct >= 0.0 ? sidecarResult.confidence : 0.0;
            // Detector-only release-window diagnostic. The new wire name is deliberately not
            // "grade" or "outcome": it says where our release command intersected detector
            // pixels, never whether the game made the shot. Legacy green_grade payloads are
            // ignored fail-closed so older/mutable-sequence sidecars cannot regain authority.
            const auto greenGrade =
                msg.value(QStringLiteral("release_window_diagnostic")).toObject();
            if (!greenGrade.isEmpty()) {
                const QString gl = greenGrade.value(QStringLiteral("label")).toString();
                sidecarResult.greenGradeLabel = gl == QLatin1String("EARLY") ? 0
                    : (gl == QLatin1String("GREEN") ? 1
                    : (gl == QLatin1String("OVER") ? 2 : -1));
                sidecarResult.greenGradeSeq = greenGrade.value(QStringLiteral("seq")).toInt(-1);
                sidecarResult.greenGradeReleaseSeq =
                    greenGrade.value(QStringLiteral("release_seq")).toInt(0);
                (void)decodePoseArmToken(
                    greenGrade.value(QStringLiteral("physical_epoch")),
                    &sidecarResult.greenGradePhysicalShotEpoch);
                (void)decodePoseArmToken(
                    greenGrade.value(QStringLiteral("shot_attempt")),
                    &sidecarResult.greenGradeShotAttempt);
                sidecarResult.greenGradeReleaseProxy =
                    greenGrade.value(QStringLiteral("release_proxy")).toBool(true);
                sidecarResult.greenGradeWindowConfidence =
                    greenGrade.value(QStringLiteral("window_conf")).toDouble(0.0);
                sidecarResult.greenGradeStartPct =
                    greenGrade.value(QStringLiteral("g_lo")).toDouble(-1.0);
                sidecarResult.greenGradeFillAtRelease =
                    greenGrade.value(QStringLiteral("fill_at_release")).toDouble(-1.0);
            }
            // Meter bbox (capture-frame px). AutomationEngine::updateDetection maps this into
            // the post-release MeterCalSample bbox center (bx,by) so meterSettledFrames can tell
            // a SLIDING fade/Go-To meter from a SETTLED frozen marker. Previously never set, so
            // every bx/by was 0 -> no motion ever detected -> moving shots graded -> false
            // EXCELLENT. Leave x/y/width/height at 0 when the sidecar omits bbox (older sidecar).
            const auto bbox = msg.value(QStringLiteral("bbox")).toArray();
            if (bbox.size() == 4) {
                sidecarResult.x = bbox.at(0).toInt();
                sidecarResult.y = bbox.at(1).toInt();
                sidecarResult.width = bbox.at(2).toInt();
                sidecarResult.height = bbox.at(3).toInt();
            }
            const auto bboxWh = msg.value(QStringLiteral("bbox_wh")).toArray();
            if (bboxWh.size() == 2) {
                const int bboxFrameWidth = bboxWh.at(0).toInt();
                const int bboxFrameHeight = bboxWh.at(1).toInt();
                if (bboxFrameWidth > 0 && bboxFrameHeight > 0) {
                    sidecarResult.bboxFrameWidth = bboxFrameWidth;
                    sidecarResult.bboxFrameHeight = bboxFrameHeight;
                }
            }
            emit sidecarDetectionReady(sidecarResult);
        }

        // Diagnostic overlay text is rebuilt at most ~60 Hz. This ~30-arg string build feeds a
        // debug stats panel only; the timing-critical sidecarDetectionReady emit is ABOVE this and
        // runs every message. Coalescing the build keeps per-message GUI-thread work down so, when a
        // burst of telemetry lines is buffered, the newest sample's detection emit isn't held behind
        // older messages' readout formatting under the event-driven (higher-frequency) feed.
        const qint64 nowDiagMs = QDateTime::currentMSecsSinceEpoch();
        if (nowDiagMs - lastAlgorithmTextBuildMs_ >= kAlgorithmTextThrottleMs) {
        lastAlgorithmTextBuildMs_ = nowDiagMs;
        QStringList lines;
        lines << QStringLiteral("Fill %1%  Target %2%  ETA %3 ms  Ready %4")
                     .arg(sidecarFill, 0, 'f', 1)
                     .arg(sidecarTarget, 0, 'f', 1)
                     .arg(sidecarEta, 0, 'f', 1)
                     .arg(fusion.value(QStringLiteral("release_ready")).toBool(false) ? QStringLiteral("YES") : QStringLiteral("No"));
        lines << QStringLiteral("Strategy %1  Confidence %2%  Velocity %3%/s")
                     .arg(fusion.value(QStringLiteral("strategy")).toString(QStringLiteral("detector")))
                     .arg(sidecarConfidence * 100.0, 0, 'f', 1)
                     .arg(sidecarVelocity, 0, 'f', 1);
        lines << QStringLiteral("Shot %1  Phase %2  Contest %3  Dynamic %4 ms")
                     .arg(intelligence.value(QStringLiteral("shot_type")).toString(QStringLiteral("unknown")),
                          intelligence.value(QStringLiteral("animation_phase")).toString(QStringLiteral("idle")),
                          intelligence.value(QStringLiteral("contest_level")).toString(QStringLiteral("unknown")))
                     .arg(fusion.value(QStringLiteral("dynamic_offset_ms")).toDouble(0.0), 0, 'f', 1);
        lines << QStringLiteral("Tracker %1  Flow q=%2  Delta %3%  Kalman %4%")
                     .arg(tracking.value(QStringLiteral("source")).toString(QStringLiteral("waiting")))
                     .arg(tracking.value(QStringLiteral("optical_flow_quality")).toDouble(0.0), 0, 'f', 2)
                     .arg(tracking.value(QStringLiteral("optical_flow_delta_pct")).toDouble(0.0), 0, 'f', 2)
                     .arg(tracking.value(QStringLiteral("kalman_prediction_pct")).toDouble(0.0), 0, 'f', 1);
        lines << QStringLiteral("Lead %1 ms  RTT %2 ms  Jitter %3 ms  Offset %4 ms")
                     .arg(fusion.value(QStringLiteral("total_lead_ms")).toDouble(0.0), 0, 'f', 1)
                     .arg(rtt.value(QStringLiteral("filtered_ms")).toDouble(telemetry_.rttMs), 0, 'f', 1)
                     .arg(rtt.value(QStringLiteral("jitter_ms")).toDouble(telemetry_.jitterMs), 0, 'f', 1)
                     .arg(rtt.value(QStringLiteral("predicted_offset_ms")).toDouble(telemetry_.offsetMs), 0, 'f', 1);
        lines << QStringLiteral("Packet %1 ms  Tick phase %2 ms  Phase %3 q=%4")
                     .arg(rtt.value(QStringLiteral("packet_interval_ms")).toDouble(0.0), 0, 'f', 2)
                     .arg(rtt.value(QStringLiteral("tick_phase_ms")).toDouble(0.0), 0, 'f', 2)
                     .arg(rtt.value(QStringLiteral("phase_locked")).toBool(false) ? QStringLiteral("locked") : QStringLiteral("syncing"))
                     .arg(rtt.value(QStringLiteral("phase_confidence")).toDouble(0.0), 0, 'f', 2);
        lines << QStringLiteral("Decode comp %1 ms  State %2  FPS %3 unique / %4 cap  Dup %5%  Age %6 ms")
                      .arg(rtt.value(QStringLiteral("decode_comp_ms")).toDouble(0.0), 0, 'f', 1)
                      .arg(newState,
                           QString::number(uniqueFrameFps_),
                           QString::number(captureLoopFps_))
                      .arg(duplicateFramePct_, 0, 'f', 1)
                      .arg(frameAgeMs_, 0, 'f', 1);
        const QString nextAlgorithm = lines.join(QLatin1Char('\n'));
        if (nextAlgorithm != algorithmText_) {
            algorithmText_ = nextAlgorithm;
            changed = true;
        }
        }   // end diagnostic-text ~60 Hz throttle (detection emit above is per-message)

        if (changed) emit sidecarStatsChanged();
    } else if (event == QLatin1String("log")) {
        const QString text = msg.value(QStringLiteral("msg")).toString();
        if (!text.isEmpty()) emit setupMessage(QStringLiteral("Sidecar: %1").arg(text));
    } else if (event == QLatin1String("pose_landmark")) {
        quint64 armToken = 0;
        if (!decodePoseArmToken(msg.value(QStringLiteral("arm_token")), &armToken)) {
            emit setupMessage(QStringLiteral("Dropped pose_landmark with invalid arm token"));
            return;
        }
        const QString kind = msg.value(QStringLiteral("kind")).toString();
        const int frameSeq = msg.value(QStringLiteral("frame_seq")).toInt();
        const double confidence = msg.value(QStringLiteral("confidence")).toDouble();
        emit poseLandmarkReady(kind, frameSeq, confidence, armToken);
    } else if (event == QLatin1String("pose_overlay")) {
        // Locked-player skeleton overlay for the on-capture draw (full-frame px coords).
        const QVariantList keypoints = msg.value(QStringLiteral("kpts")).toArray().toVariantList();
        const QVariantList box = msg.value(QStringLiteral("box")).toArray().toVariantList();
        // Camera-anchor extras. Absent/null (e.g. indicator not detected this
        // frame) -> toArray() yields an empty QJsonArray -> empty QVariantList, which the QML
        // draw simply skips. Backward compatible with payloads that omit these keys.
        const QVariantList anchor = msg.value(QStringLiteral("anchor")).toArray().toVariantList();
        const QVariantList lockCenter = msg.value(QStringLiteral("lock_center")).toArray().toVariantList();
        const QVariantList indicator = msg.value(QStringLiteral("indicator")).toArray().toVariantList();
        emit poseOverlayReady(keypoints, box, anchor, lockCenter, indicator);
    } else if (event == QLatin1String("calibrate_meter_status")) {
        // [Track B / B3] ColorCalibrator lifecycle status (per committed shot + per state
        // transition). Defaults keep an older sidecar harmless: 0/5/"" -> the controller's
        // badge stays on its last-known state.
        emit meterCalibrationStatusReady(
            msg.value(QStringLiteral("shots_done")).toInt(0),
            msg.value(QStringLiteral("shots_needed")).toInt(5),
            msg.value(QStringLiteral("state")).toString(),
            msg.value(QStringLiteral("learned_date")).toString(),
            msg.value(QStringLiteral("calibrating")).toBool(false));
    }
}

} // namespace orion
