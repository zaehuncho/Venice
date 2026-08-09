#include "NetworkBridge.h"

#include <QtCore/QJsonArray>
#include <QtCore/QJsonDocument>
#include <QtCore/QJsonObject>
#include <QtCore/QDateTime>
#include <QtCore/QDir>
#include <QtCore/QFile>

#include <WinSock2.h>
#include <WS2tcpip.h>

#include <algorithm>
#include <chrono>
#include <climits>
#include <cmath>
#include <QRandomGenerator>

#pragma comment(lib, "Ws2_32.lib")

namespace orion {

static constexpr int BRIDGE_PORT = 47291;
static constexpr qsizetype MAX_BRIDGE_BUFFER_BYTES = 64 * 1024;
// Small on purpose: the only producers are low-rate control verbs, so the
// queue should never hold more than a couple of entries.
static constexpr size_t MAX_PENDING_COMMANDS = 32;
// Housekeeping cadence (idle-flow expiry + auth watchdog). Previously this was
// implied by the 100 ms SO_RCVTIMEO; it is now explicit so that a stream of
// wakeups cannot starve it.
static constexpr qint64 HOUSEKEEPING_INTERVAL_MS = 100;

static qint64 monotonicNowMs()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

// --- Packet-bridge bearer token discovery ---------------------------------------------------

// Generous but bounded: a v1 record is ~120 bytes. Anything larger is not a token file.
static constexpr qint64 MAX_TOKEN_FILE_BYTES = 8 * 1024;

namespace {

QString tokenPathUnder(const char* envVar, const QString& fallbackRoot)
{
    const QByteArray root = qgetenv(envVar);
    const QString base = root.isEmpty() ? fallbackRoot : QString::fromLocal8Bit(root);
    return base + QStringLiteral("/NexusVision/nexus_bridge.token");
}

// Only ever answers "yes" when the process is genuinely gone. Anything ambiguous — an
// unreadable process, a pid we are not allowed to query — answers "no", so a live bridge is
// never mistaken for a stale one.
bool processProvablyGone(qint64 pid)
{
    if (pid <= 0) {
        return false;  // no pid recorded: unknown, not provably gone
    }
    const HANDLE proc = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                                      static_cast<DWORD>(pid));
    if (proc != nullptr) {
        DWORD exitCode = 0;
        const bool queried = ::GetExitCodeProcess(proc, &exitCode) != FALSE;
        ::CloseHandle(proc);
        // A handle can outlive the process, so "opened" alone does not mean running.
        return queried && exitCode != STILL_ACTIVE;
    }
    // OpenProcess reports ERROR_INVALID_PARAMETER for a pid that no longer exists.
    // ERROR_ACCESS_DENIED means it exists but is out of reach (e.g. the LocalSystem service seen
    // from the unelevated app) and must NOT count as stale.
    return ::GetLastError() == ERROR_INVALID_PARAMETER;
}

}  // namespace

QStringList bridgeTokenCandidatePaths()
{
    return QStringList{
        // Debug/unelevated bridge: already per-user private, so it needs no privileged ACL edit.
        tokenPathUnder("LOCALAPPDATA", QDir::homePath() + QStringLiteral("/AppData/Local")),
        // Service bridge: LocalSystem hardens this one and grants INTERACTIVE read.
        tokenPathUnder("PROGRAMDATA", QStringLiteral("C:/ProgramData")),
    };
}

BridgeTokenRecord parseBridgeTokenRecord(const QString& path, const QByteArray& body)
{
    BridgeTokenRecord record;
    record.path = path;
    record.exists = true;

    const QByteArray trimmed = body.trimmed();
    if (trimmed.startsWith('{')) {
        const QJsonDocument doc = QJsonDocument::fromJson(trimmed);
        if (doc.isObject()) {
            const QJsonObject obj = doc.object();
            record.token = obj.value(QStringLiteral("token")).toString().trimmed();
            record.pid = static_cast<qint64>(obj.value(QStringLiteral("pid")).toDouble());
            record.startedMs =
                static_cast<qint64>(obj.value(QStringLiteral("started_ms")).toDouble());
            // Only a record that carries both a token and a pid can be freshness-checked.
            record.structured = !record.token.isEmpty() && record.pid > 0;
            return record;
        }
    }
    // Pre-v1 bridge wrote the bare token with no way to tell fresh from stale.
    record.token = QString::fromUtf8(trimmed).trimmed();
    return record;
}

BridgeTokenSelection selectBridgeToken(const QVector<BridgeTokenRecord>& records)
{
    BridgeTokenSelection selection;

    const BridgeTokenRecord* fresh = nullptr;
    const BridgeTokenRecord* legacy = nullptr;
    QStringList staleNotes;
    QStringList otherNotes;

    for (const BridgeTokenRecord& record : records) {
        if (!record.exists) {
            otherNotes << QStringLiteral("%1 (absent)").arg(record.path);
            continue;
        }
        if (record.token.isEmpty()) {
            otherNotes << QStringLiteral("%1 (present but unreadable or empty)").arg(record.path);
            continue;
        }
        if (!record.structured) {
            if (legacy == nullptr) {
                legacy = &record;
            }
            continue;
        }
        if (!record.ownerAlive) {
            staleNotes << QStringLiteral("%1 (stale: publishing pid %2 has exited)")
                              .arg(record.path)
                              .arg(record.pid);
            continue;
        }
        // Both files can be live in odd setups (service plus a leftover debug run); the most
        // recently published record is the one the currently listening bridge wrote.
        if (fresh == nullptr || record.startedMs > fresh->startedMs) {
            fresh = &record;
        }
    }

    selection.staleRejected = !staleNotes.isEmpty();
    const QString rejected = staleNotes.isEmpty()
        ? QString()
        : QStringLiteral("; ignored ") + staleNotes.join(QStringLiteral(", "));

    if (fresh != nullptr) {
        selection.token = fresh->token;
        selection.path = fresh->path;
        selection.diagnostic = QStringLiteral("using token from %1 (bridge pid %2, published %3)%4")
                                   .arg(fresh->path)
                                   .arg(fresh->pid)
                                   .arg(QDateTime::fromMSecsSinceEpoch(fresh->startedMs)
                                            .toString(Qt::ISODate))
                                   .arg(rejected);
        return selection;
    }

    if (legacy != nullptr) {
        selection.token = legacy->token;
        selection.path = legacy->path;
        selection.diagnostic =
            QStringLiteral("using legacy token from %1 — it carries no publisher pid, so it cannot "
                           "be freshness-checked; update nexus_svc if auth is rejected%2")
                .arg(legacy->path)
                .arg(rejected);
        return selection;
    }

    QStringList examined = staleNotes;
    examined << otherNotes;
    selection.diagnostic =
        QStringLiteral("no usable packet bridge token: %1. The bridge did not publish one for this "
                       "session — check nexus_svc.log for 'Bridge token could not be published'.")
            .arg(examined.isEmpty() ? QStringLiteral("no candidate paths examined")
                                    : examined.join(QStringLiteral(", ")));
    return selection;
}

BridgeTokenSelection resolveBridgeToken()
{
    QVector<BridgeTokenRecord> records;
    const QStringList paths = bridgeTokenCandidatePaths();
    records.reserve(paths.size());

    for (const QString& path : paths) {
        QFile file(path);
        if (!file.exists()) {
            BridgeTokenRecord missing;
            missing.path = path;
            records.push_back(missing);
            continue;
        }
        if (!file.open(QIODevice::ReadOnly)) {
            BridgeTokenRecord unreadable;
            unreadable.path = path;
            unreadable.exists = true;
            records.push_back(unreadable);
            continue;
        }
        const QByteArray body = file.read(MAX_TOKEN_FILE_BYTES);
        file.close();

        BridgeTokenRecord record = parseBridgeTokenRecord(path, body);
        if (record.structured) {
            record.ownerAlive = !processProvablyGone(record.pid);
        }
        records.push_back(record);
    }

    return selectBridgeToken(records);
}

// --- Service hello parsing ------------------------------------------------------------------

BridgeHelloInfo parseBridgeHello(const QJsonObject& msg)
{
    BridgeHelloInfo hello;
    const QJsonValue versionValue = msg.value(QStringLiteral("version"));
    if (versionValue.isDouble()) {
        hello.version = std::max(0, static_cast<int>(std::floor(versionValue.toDouble())));
    }
    // Tolerate the field being absent (a pre-v3 service) or malformed: hasFeatures
    // stays false and the caller must treat the arm state as unknown, not armed.
    const QJsonValue featuresValue = msg.value(QStringLiteral("features"));
    if (featuresValue.isArray()) {
        hello.hasFeatures = true;
        const QJsonArray features = featuresValue.toArray();
        hello.features.reserve(features.size());
        for (const QJsonValue& feature : features) {
            if (feature.isString()) {
                hello.features.append(feature.toString());
            }
        }
    }
    return hello;
}

NetworkBridge::NetworkBridge(QObject* parent)
    : QObject(parent)
{
    WSADATA wsa;
    WSAStartup(MAKEWORD(2, 2), &wsa);
    // Install this exactly once. Reconnecting the same NetworkBridge used to
    // append another started->workerLoop lambda on every start(), so later
    // restarts accumulated duplicate worker invocations.
    connect(&workerThread_, &QThread::started, this,
            [this]() { workerLoop(); }, Qt::DirectConnection);
}

NetworkBridge::~NetworkBridge()
{
    // stop() publishes a final empty diagnostic snapshot for ordinary runtime
    // opt-out. During owner destruction, disconnect first so that publication
    // cannot re-enter a partially destroyed OrionAppController.
    disconnect(this, nullptr, nullptr, nullptr);
    stop();
    WSACleanup();
}

void NetworkBridge::setConsoleIp(const QString& ip)
{
    QMutexLocker lock(&ipMutex_);
    pendingIp_ = ip;
    ipDirty_ = true;
}

void NetworkBridge::start()
{
    if (running_.exchange(true)) return;
    if (workerThread_.isRunning()) {
        running_ = false;
        emit connectionChanged(false, QStringLiteral("Packet bridge worker is still stopping"));
        return;
    }

    workerThread_.start();
}

void NetworkBridge::stop()
{
    running_ = false;
    if (workerThread_.isRunning()) {
        workerThread_.quit();
        workerThread_.wait(5000);
    }
    if (!workerThread_.isRunning()) {
        connected_ = false;
        resetTelemetryState();
    }
}

bool NetworkBridge::sendLine(quintptr socketHandle, const QByteArray& data)
{
    const SOCKET sock = static_cast<SOCKET>(socketHandle);
    if (sock == INVALID_SOCKET || data.isEmpty()) {
        return false;
    }
    qsizetype sentTotal = 0;
    while (sentTotal < data.size()) {
        const int remaining = static_cast<int>(std::min<qsizetype>(
            data.size() - sentTotal, static_cast<qsizetype>(INT_MAX)));
        const int sent = ::send(sock, data.constData() + sentTotal, remaining, 0);
        if (sent == SOCKET_ERROR || sent <= 0) {
            return false;
        }
        sentTotal += sent;
    }
    return true;
}

void NetworkBridge::setServiceVersion(int version)
{
    serviceVersion_.store(version);
}

void NetworkBridge::setMeterDelayServiceState(MeterDelayServiceState state, const QString& cause)
{
    const MeterDelayServiceState previous = meterDelayServiceState_.exchange(state);
    if (previous == state) {
        // Transition-dedupe: nexus_svc answers meter_delay_disarmed once per refused
        // verb — one per ~150 ms keepalive while the controller is engaged. A log
        // census must be able to tell "never armed" from "armed and working" without
        // wading through a line per tick.
        return;
    }
    switch (state) {
    case MeterDelayServiceState::Disarmed:
        qWarning("[NetworkBridge] Meter delay service state: DISARMED (%s) — the "
                 "service is refusing delay verbs; NO inbound delay is being applied",
                 qPrintable(cause));
        break;
    case MeterDelayServiceState::Armed:
        qInfo("[NetworkBridge] Meter delay service state: ARMED (%s)", qPrintable(cause));
        break;
    case MeterDelayServiceState::Unknown:
        qDebug("[NetworkBridge] Meter delay service state: unknown (%s)", qPrintable(cause));
        break;
    }
    emit meterDelayServiceStateChanged(state, cause);
}

void NetworkBridge::maybeEmitMeterDelayEcho(const QJsonObject& msg)
{
    const QJsonValue activeValue = msg.value(QStringLiteral("meter_delay_active"));
    const QJsonValue delayValue = msg.value(QStringLiteral("meter_delay_ms"));
    if (!activeValue.isBool() || !delayValue.isDouble()) {
        return; // ack without a snapshot (older service) — nothing to echo
    }
    const bool active = activeValue.toBool();
    const double delayMs = delayValue.toDouble();
    const int depth = msg.value(QStringLiteral("meter_buffer_depth")).toInt(0);
    // nexus_svc hard-caps the applied delay at 300 ms (_METER_DELAY_HARD_MAX_MS);
    // anything outside a generous envelope is a malformed line, not a state.
    if (!std::isfinite(delayMs) || delayMs < 0.0 || delayMs > 1000.0 || depth < 0) {
        qWarning("[NetworkBridge] Rejected malformed meter-delay echo from bridge");
        return;
    }
    // Only an ARMED service ever produces this snapshot: the delay buffer object
    // does not exist while disarmed and every delay verb is refused before its ack.
    // A missed/stale hello therefore cannot leave the arm state wrong while echoes
    // are flowing.
    setMeterDelayServiceState(MeterDelayServiceState::Armed,
                              QStringLiteral("service applied-delay echo received"));
    if (lastEchoValid_ && active == lastEchoActive_
        && std::abs(delayMs - lastEchoDelayMs_) < 0.05 && depth == lastEchoDepth_) {
        return;
    }
    lastEchoValid_ = true;
    lastEchoActive_ = active;
    lastEchoDelayMs_ = delayMs;
    lastEchoDepth_ = depth;
    emit meterDelayServiceEcho(active, delayMs, depth);
}

void NetworkBridge::wakeWorkerLocked()
{
    if (!wakeSocketValid_) {
        return;
    }
    sockaddr_in self{};
    self.sin_family = AF_INET;
    self.sin_port = htons(wakePort_);
    inet_pton(AF_INET, "127.0.0.1", &self.sin_addr);
    const char byte = 1;
    // Loopback UDP datagram to our own bound port. Never blocks; a failure only
    // costs up to one 100 ms select() timeout of extra latency.
    ::sendto(static_cast<SOCKET>(wakeSocket_), &byte, 1, 0,
             reinterpret_cast<sockaddr*>(&self), sizeof(self));
}

bool NetworkBridge::queueCommand(const QByteArray& coalesceKey, const QJsonObject& cmd,
                                 bool lifecycleBarrier)
{
    if (!connected_.load()) {
        return false;
    }
    const QByteArray line = QJsonDocument(cmd).toJson(QJsonDocument::Compact) + '\n';

    QMutexLocker lock(&cmdMutex_);
    bool coalesced = false;
    if (!coalesceKey.isEmpty()) {
        // Scan backwards and stop at the first ordering barrier so a coalesced
        // value can never hop over a start/stop_meter_intercept.
        for (auto it = pendingCommands_.rbegin(); it != pendingCommands_.rend(); ++it) {
            if (it->coalesceKey.isEmpty()) {
                break; // barrier
            }
            if (it->coalesceKey == coalesceKey) {
                it->line = line;
                coalesced = true;
                break;
            }
        }
    }
    if (!coalesced) {
        if (pendingCommands_.size() >= MAX_PENDING_COMMANDS) {
            // Drop the oldest COALESCABLE entry first; ordering barriers are
            // only sacrificed if nothing else is left.
            auto victim = std::find_if(
                pendingCommands_.begin(), pendingCommands_.end(),
                [](const PendingCommand& pc) { return !pc.coalesceKey.isEmpty(); });
            if (victim == pendingCommands_.end()) {
                victim = pendingCommands_.begin();
            }
            pendingCommands_.erase(victim);
        }
        pendingCommands_.push_back(
            PendingCommand{lifecycleBarrier ? QByteArray() : coalesceKey, line});
    }
    wakeWorkerLocked();
    return true;
}

// [ORION_METER_DELAY 2026-08-07] Public slots that translate MeterDelayController
// output into service verbs. All three go through queueCommand so they run on
// the worker thread's send loop, never blocking the caller (UI/controller).
void NetworkBridge::sendSetMeterDelay(double ms)
{
    QJsonObject cmd{
        {QStringLiteral("cmd"), QStringLiteral("set_meter_delay")},
        {QStringLiteral("delay_ms"), ms},
    };
    queueCommand(QByteArrayLiteral("set_meter_delay"), cmd, /*lifecycleBarrier=*/false);
}

void NetworkBridge::startMeterIntercept()
{
    QJsonObject cmd{{QStringLiteral("cmd"), QStringLiteral("start_meter_intercept")}};
    queueCommand(QByteArray(), cmd, /*lifecycleBarrier=*/true);
}

void NetworkBridge::stopMeterIntercept()
{
    QJsonObject cmd{{QStringLiteral("cmd"), QStringLiteral("stop_meter_intercept")}};
    queueCommand(QByteArray(), cmd, /*lifecycleBarrier=*/true);
}

void NetworkBridge::workerLoop()
{
    qDebug("[NetworkBridge] Worker thread started");

    // ── Wakeup channel ──────────────────────────────────────────────────────
    // The command drain used to be bounded by the 100 ms SO_RCVTIMEO whenever no
    // packets were arriving, which would quantise the 50 ms meter-delay ramp
    // tick to 100 ms and roughly double the time to reach the target delay -
    // pushing the ramp into the meter's rise, which is exactly what the
    // controller exists to avoid.
    //
    // Options considered:
    //   (a) shorten SO_RCVTIMEO to ~20 ms: simple, but still quantises, and
    //       raises the idle wake rate to 50/s forever.
    //   (b) WSAEventSelect + overlapped recv: true wakeup, but converts the
    //       whole blocking recv loop to async - a large, risky rewrite of a
    //       working transport.
    //   (c) (chosen) a loopback UDP self-pipe selected alongside the bridge
    //       socket. Producers send one byte to it; select() returns immediately.
    //       Sub-millisecond command latency, zero extra idle wakeups, and the
    //       existing blocking recv path is unchanged.
    SOCKET wakeSock = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    if (wakeSock != INVALID_SOCKET) {
        sockaddr_in wakeAddr{};
        wakeAddr.sin_family = AF_INET;
        wakeAddr.sin_port = 0;
        inet_pton(AF_INET, "127.0.0.1", &wakeAddr.sin_addr);
        sockaddr_in bound{};
        int boundLen = sizeof(bound);
        u_long nonBlocking = 1;
        if (::bind(wakeSock, reinterpret_cast<sockaddr*>(&wakeAddr), sizeof(wakeAddr)) != 0
            || ::getsockname(wakeSock, reinterpret_cast<sockaddr*>(&bound), &boundLen) != 0
            || ::ioctlsocket(wakeSock, FIONBIO, &nonBlocking) != 0) {
            ::closesocket(wakeSock);
            wakeSock = INVALID_SOCKET;
        } else {
            QMutexLocker lock(&cmdMutex_);
            wakeSocket_ = static_cast<quintptr>(wakeSock);
            wakePort_ = ntohs(bound.sin_port);
            wakeSocketValid_ = true;
        }
    }
    if (wakeSock == INVALID_SOCKET) {
        // Degraded but functional: commands then wait for the next select()
        // timeout instead of an immediate wakeup.
        qWarning("[NetworkBridge] Wakeup socket unavailable; command dispatch falls "
                 "back to the %lldms poll interval",
                 static_cast<long long>(HOUSEKEEPING_INTERVAL_MS));
    }
    const auto releaseWakeSocket = [this, &wakeSock]() {
        {
            QMutexLocker lock(&cmdMutex_);
            wakeSocketValid_ = false;
            wakeSocket_ = 0;
            wakePort_ = 0;
            pendingCommands_.clear();
        }
        if (wakeSock != INVALID_SOCKET) {
            ::closesocket(wakeSock);
            wakeSock = INVALID_SOCKET;
        }
    };

    int retryMs = 3000;  // exponential backoff with jitter, capped at 30s
    const auto waitForRetry = [this](int delayMs) {
        // QThread::msleep(retryMs) made stop() wait behind a 30-second backoff
        // while its destructor allowed only five seconds. Poll the stop flag in
        // short slices so teardown/restart is deterministic.
        int remaining = std::max(0, delayMs);
        while (running_.load() && remaining > 0) {
            const int slice = std::min(remaining, 50);
            QThread::msleep(static_cast<unsigned long>(slice));
            remaining -= slice;
        }
        return running_.load();
    };

    while (running_.load()) {
        SOCKET sock = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (sock == INVALID_SOCKET) {
            qDebug("[NetworkBridge] socket() failed");
            waitForRetry(retryMs);
            retryMs = std::min(30000, retryMs * 2 + (QRandomGenerator::global()->bounded(500)));
            continue;
        }

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(BRIDGE_PORT);
        inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);

        if (::connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
            ::closesocket(sock);
            emit connectionChanged(false, QStringLiteral("Packet bridge not reachable"));
            waitForRetry(retryMs);
            retryMs = std::min(30000, retryMs * 2 + (QRandomGenerator::global()->bounded(500)));
            continue;
        }

        // F1: authenticate with the per-session token (written by the elevated service) BEFORE any
        // command — the bridge rejects everything until a matching auth arrives. TCP ordering means
        // this auth line is processed before the set_filter pipelined right after it.
        // The token may live in either of two directories depending on how the bridge was started,
        // and a leftover file from a previous session is worse than none: it authenticates with a
        // dead session's secret and the resulting "unauthorized" hides the real cause. Pick by
        // publisher liveness, and record the reasoning so a rejection can quote it.
        const BridgeTokenSelection tokenSelection = resolveBridgeToken();
        tokenPath_ = tokenSelection.path;
        tokenDiagnostic_ = tokenSelection.diagnostic;
        const QString& bridgeToken = tokenSelection.token;
        if (bridgeToken.isEmpty()) {
            ::closesocket(sock);
            qWarning("[NetworkBridge] %s", qPrintable(tokenSelection.diagnostic));
            emit connectionChanged(
                false, QStringLiteral("Packet bridge authentication unavailable — %1")
                           .arg(tokenSelection.diagnostic));
            waitForRetry(retryMs);
            retryMs = std::min(30000, retryMs * 2 + QRandomGenerator::global()->bounded(500));
            continue;
        }
        qInfo("[NetworkBridge] %s", qPrintable(tokenSelection.diagnostic));
        const QJsonObject authCmd{{"cmd", "auth"}, {"token", bridgeToken}};
        if (!sendLine(static_cast<quintptr>(sock),
                      QJsonDocument(authCmd).toJson(QJsonDocument::Compact) + '\n')) {
            ::closesocket(sock);
            emit connectionChanged(false, QStringLiteral("Packet bridge authentication send failed"));
            waitForRetry(retryMs);
            retryMs = std::min(30000, retryMs * 2 + QRandomGenerator::global()->bounded(500));
            continue;
        }
        authenticationRejected_ = false;
        connected_ = false;
        // A new TCP generation means a possibly different service build; the
        // previous handshake version is not carried forward — nor is the previous
        // service's arm state (a restarted bridge may have been re-armed or not).
        setServiceVersion(0);
        setMeterDelayServiceState(MeterDelayServiceState::Unknown,
                                  QStringLiteral("new bridge connection"));
        lastEchoValid_ = false; // a new generation must re-emit its first echo
        {
            QMutexLocker lock(&cmdMutex_);
            pendingCommands_.clear();
        }
        const qint64 authSentEpochMs = QDateTime::currentMSecsSinceEpoch();

        {
            QMutexLocker lock(&ipMutex_);
            consoleIp_ = pendingIp_;
            ipDirty_ = false;
        }
        if (!consoleIp_.isEmpty()) {
            QJsonObject cmd{{"cmd", "set_filter"}, {"console_ip", consoleIp_}};
            QByteArray line = QJsonDocument(cmd).toJson(QJsonDocument::Compact) + '\n';
            if (!sendLine(static_cast<quintptr>(sock), line)) {
                ::closesocket(sock);
                emit connectionChanged(false, QStringLiteral("Packet bridge filter send failed"));
                waitForRetry(retryMs);
                retryMs = std::min(30000, retryMs * 2 + QRandomGenerator::global()->bounded(500));
                continue;
            }
            qDebug("[NetworkBridge] Sent set_filter for %s", qPrintable(consoleIp_));
        }

        retryMs = 3000;  // reset backoff on successful authenticated connect
        qDebug("[NetworkBridge] Connected to bridge transport on port %d; awaiting auth ACK", BRIDGE_PORT);

        // Backstop only. The loop below blocks in select(), not in recv(), so
        // this timeout should never actually fire; it stays as belt-and-braces
        // against a socket that reports readable and then stalls.
        DWORD timeout = 100;
        setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));

        QByteArray buf;
        qint64 lastHousekeepMs = monotonicNowMs();

        while (running_.load()) {
            bool commandSendFailed = false;
            {
                QMutexLocker lock(&ipMutex_);
                if (ipDirty_) {
                    consoleIp_ = pendingIp_;
                    ipDirty_ = false;
                    QJsonObject cmd;
                    if (consoleIp_.isEmpty()) {
                        cmd = {{"cmd", "clear_filter"}};
                    } else {
                        cmd = {{"cmd", "set_filter"}, {"console_ip", consoleIp_}};
                    }
                    QByteArray line = QJsonDocument(cmd).toJson(QJsonDocument::Compact) + '\n';
                    commandSendFailed = !sendLine(static_cast<quintptr>(sock), line);
                }
            }
            if (commandSendFailed) {
                break;
            }

            // Drain the cross-thread command queue in the same block as the
            // console-IP handoff above. Fire-and-forget: no ack is awaited, so a
            // ramp tick is never blocked on the service.
            while (!commandSendFailed) {
                PendingCommand pending;
                {
                    QMutexLocker lock(&cmdMutex_);
                    if (pendingCommands_.empty()) {
                        break;
                    }
                    pending = pendingCommands_.front();
                    pendingCommands_.pop_front();
                }
                commandSendFailed = !sendLine(static_cast<quintptr>(sock), pending.line);
            }
            if (commandSendFailed) {
                break;
            }

            // Housekeeping on its own clock. A steady stream of command wakeups
            // must not starve the idle-flow expiry or the auth watchdog.
            const qint64 housekeepNowMs = monotonicNowMs();
            if (housekeepNowMs - lastHousekeepMs >= HOUSEKEEPING_INTERVAL_MS) {
                lastHousekeepMs = housekeepNowMs;
                if (!connected_.load()
                    && QDateTime::currentMSecsSinceEpoch() - authSentEpochMs > 2000) {
                    break;
                }
                checkIdleTimeout(housekeepNowMs);
            }

            fd_set readSet;
            FD_ZERO(&readSet);
            FD_SET(sock, &readSet);
            if (wakeSock != INVALID_SOCKET) {
                FD_SET(wakeSock, &readSet);
            }
            timeval selectTimeout{};
            selectTimeout.tv_sec = 0;
            selectTimeout.tv_usec = static_cast<long>(HOUSEKEEPING_INTERVAL_MS * 1000);
            const int ready = ::select(0, &readSet, nullptr, nullptr, &selectTimeout);
            if (ready == SOCKET_ERROR) {
                break;
            }
            if (wakeSock != INVALID_SOCKET && FD_ISSET(wakeSock, &readSet)) {
                char drain[64];
                while (::recvfrom(wakeSock, drain, sizeof(drain), 0, nullptr, nullptr) > 0) {
                    // Coalesce: the queue itself carries the work.
                }
            }
            if (ready == 0 || !FD_ISSET(sock, &readSet)) {
                continue; // timeout, or wakeup-only -> loop back and drain
            }

            char tmp[8192];
            int n = ::recv(sock, tmp, sizeof(tmp), 0);
            if (n > 0) {
                buf.append(tmp, n);
                if (buf.size() > MAX_BRIDGE_BUFFER_BYTES) {
                    qWarning("[NetworkBridge] Bridge protocol buffer exceeded %lld bytes; reconnecting",
                             static_cast<long long>(MAX_BRIDGE_BUFFER_BYTES));
                    break;
                }
                while (buf.contains('\n')) {
                    int idx = buf.indexOf('\n');
                    QByteArray line = buf.left(idx);
                    buf.remove(0, idx + 1);
                    handleLine(line);
                }
                if (authenticationRejected_) {
                    break;
                }
            } else if (n == 0) {
                break;
            } else {
                int err = WSAGetLastError();
                if (err == WSAETIMEDOUT) {
                    if (!connected_.load()
                        && QDateTime::currentMSecsSinceEpoch() - authSentEpochMs > 2000) {
                        break;
                    }
                    // Use timeout as opportunity to check idle state
                    checkIdleTimeout(monotonicNowMs());
                    continue;
                }
                break;
            }
        }

        connected_ = false;
        ::closesocket(sock);
        // A TCP/auth generation boundary invalidates every cadence/flow value.
        // Carrying playingGame or packet runs into the next connection can keep
        // stale court authority alive until an unrelated idle timeout. The arm
        // state dies with the connection too: a disconnected bridge can no longer
        // claim the delay is armed (or disarmed).
        setServiceVersion(0);
        setMeterDelayServiceState(MeterDelayServiceState::Unknown,
                                  QStringLiteral("bridge disconnected"));
        lastEchoValid_ = false;
        {
            QMutexLocker lock(&cmdMutex_);
            pendingCommands_.clear();
        }
        resetTelemetryState();
        emit connectionChanged(false, QStringLiteral("Disconnected from bridge"));
        qDebug("[NetworkBridge] Disconnected");

        if (running_.load()) {
            waitForRetry(retryMs);
            retryMs = std::min(30000, retryMs * 2 + (QRandomGenerator::global()->bounded(500)));
        }
    }
    releaseWakeSocket();
    qDebug("[NetworkBridge] Worker thread exiting");
}

void NetworkBridge::resetTelemetryState()
{
    telemetry_.inboundPackets = 0;
    telemetry_.outboundPackets = 0;
    telemetry_.rttMs = 0.0;
    telemetry_.rttTargetVerified = false;
    telemetry_.jitterMs = 0.0;
    telemetry_.offsetMs = 0.0;
    telemetry_.syncAdjustMs = 0.0;
    telemetry_.consecutivePackets = 0;
    telemetry_.packetIntervalMs = 0.0;
    telemetry_.inboundConsecutive = 0;
    telemetry_.outboundConsecutive = 0;
    telemetry_.inboundIntervalMs = 0.0;
    telemetry_.outboundIntervalMs = 0.0;
    telemetry_.tickerLatencyMs = 0.0;
    telemetry_.tickPhaseVerified = false;
    telemetry_.tickPhaseConfidence = 0.0;
    telemetry_.tickPhaseObservedEpochMs = 0;
    telemetry_.playingGame = false;
    telemetry_.courtIp.clear();
    telemetry_.diagnosticCourtIp.clear();
    telemetry_.syncActive = false;
    telemetry_.syncSource = QStringLiteral("Waiting");
    telemetry_.syncConfidence = 0.0;
    passiveCourtFlow_.reset();
    playingGame_ = false;
    if (playerCount_.load() != 0) {
        playerCount_ = 0;
        emit playerCountChanged(0);
    }
    emit telemetryUpdated(telemetry_);
}

void NetworkBridge::applyPassiveSnapshot(
    const PassiveCourtFlowClassifier::Snapshot& snapshot)
{
    const auto boundedInt = [](quint64 value) {
        return static_cast<int>(std::min<quint64>(
            value, static_cast<quint64>(INT_MAX)));
    };

    // Passive packet observations never carry timing authority. Explicitly
    // zero every timing field on every publication so stale sidecar values
    // cannot leak into this independently produced snapshot.
    telemetry_.courtIp.clear();
    telemetry_.diagnosticCourtIp = snapshot.qualified
        ? snapshot.endpointIp : QString{};
    telemetry_.rttMs = 0.0;
    telemetry_.rttTargetVerified = false;
    telemetry_.jitterMs = 0.0;
    telemetry_.offsetMs = 0.0;
    telemetry_.syncAdjustMs = 0.0;
    telemetry_.tickerLatencyMs = 0.0;
    telemetry_.tickPhaseVerified = false;
    telemetry_.tickPhaseConfidence = 0.0;
    telemetry_.tickPhaseObservedEpochMs = 0;
    telemetry_.syncActive = false;
    telemetry_.syncConfidence = 0.0;
    telemetry_.syncSource = snapshot.qualified
        ? QStringLiteral("Passive court flow (display only)")
        : QStringLiteral("Observing passive court candidate");

    if (!consoleIp_.isEmpty()) {
        telemetry_.consoleIp = consoleIp_;
    }
    telemetry_.inboundPackets = boundedInt(snapshot.inboundPackets);
    telemetry_.outboundPackets = boundedInt(snapshot.outboundPackets);
    telemetry_.consecutivePackets = snapshot.recentPackets;
    telemetry_.packetIntervalMs = snapshot.packetIntervalMs;
    telemetry_.inboundConsecutive = snapshot.inboundRun;
    telemetry_.outboundConsecutive = snapshot.outboundRun;
    telemetry_.inboundIntervalMs = snapshot.inboundIntervalMs;
    telemetry_.outboundIntervalMs = snapshot.outboundIntervalMs;
    telemetry_.playingGame = snapshot.qualified;
    playingGame_ = snapshot.qualified;

    int detectedPlayers = snapshot.qualifiedEndpointCount;
    if (detectedPlayers > 0) {
        ++detectedPlayers; // include the local player
    }
    if (detectedPlayers != playerCount_.load()) {
        playerCount_ = detectedPlayers;
        emit playerCountChanged(detectedPlayers);
    }
    emit telemetryUpdated(telemetry_);
}

void NetworkBridge::checkIdleTimeout(qint64 nowMonotonicMs)
{
    const auto snapshot = passiveCourtFlow_.snapshot(nowMonotonicMs);
    if (!snapshot.hasCandidate) {
        if (playingGame_ || !telemetry_.diagnosticCourtIp.isEmpty()
            || telemetry_.inboundPackets != 0 || telemetry_.outboundPackets != 0) {
            qDebug("[NetworkBridge] Passive court flow expired; clearing diagnostics");
            resetTelemetryState();
        }
        return;
    }

    const QString nextEndpoint = snapshot.qualified
        ? snapshot.endpointIp : QString{};
    if (playingGame_ != snapshot.qualified
        || telemetry_.diagnosticCourtIp != nextEndpoint
        || telemetry_.consecutivePackets != snapshot.recentPackets) {
        applyPassiveSnapshot(snapshot);
    }
}

void NetworkBridge::handleLine(const QByteArray& line)
{
    const auto doc = QJsonDocument::fromJson(line);
    if (!doc.isObject()) return;
    const auto msg = doc.object();
    const auto event = msg.value(QStringLiteral("event")).toString();

    // The service greets with {"event":"hello","version":N,"features":[...]}
    // immediately on accept, before auth. The version decides whether the
    // meter-delay verbs exist at all: an older service silently ignores unknown
    // verbs, so sending them optimistically would look like a working delay that
    // never engages. The features array is the service's own statement of what it
    // will actually DO: nexus_svc advertises "meter_delay" only when the intercept
    // was armed at start (it ships disarmed), so "connected" and "delay applied"
    // are different facts and only this array separates them.
    if (event == QLatin1String("hello")) {
        const BridgeHelloInfo hello = parseBridgeHello(msg);
        setServiceVersion(hello.version);
        if (hello.hasFeatures) {
            setMeterDelayServiceState(
                hello.advertisesMeterDelay() ? MeterDelayServiceState::Armed
                                             : MeterDelayServiceState::Disarmed,
                QStringLiteral("hello features=[%1]")
                    .arg(hello.features.join(QLatin1Char(','))));
        } else {
            // Older service: no capability report. Degrade to unknown — the UI can
            // then say "cannot confirm" instead of guessing either way.
            setMeterDelayServiceState(
                MeterDelayServiceState::Unknown,
                QStringLiteral("hello carries no features array (service v%1 predates "
                               "capability reporting)")
                    .arg(hello.version));
        }
        qDebug("[NetworkBridge] Service hello: version=%d features=[%s]",
               serviceVersion_.load(),
               qPrintable(hello.hasFeatures ? hello.features.join(QLatin1Char(','))
                                            : QStringLiteral("<absent>")));
        return;
    }

    // Display-only echo of the actuator's effect. Deliberately kept out of
    // TelemetrySnapshot so it can never be mistaken for a timing field: per
    // PacketBridgeAuthority.h this bridge is ServiceIdentityTrust::Unverified, so
    // the echo may drive the Meter Delay card and the log census, never a clock.
    // (History: the original handler was deleted with the 968b127f feature removal
    // and the first two lines of this comment were stranded here; restored
    // 2026-08-08 for the relaunched meter delay, because the service's own
    // statement of what it is applying is the one display the controller's ramp
    // book-keeping cannot fake.) nexus_svc carries the same snapshot in its
    // {"event":"meter_delay"} state broadcast (_broadcast_state) and in every
    // delay-verb ack, so both feed the same deduped emitter.
    if (event == QLatin1String("meter_delay")) {
        if (!connected_.load()) return;
        maybeEmitMeterDelayEcho(msg);
        return;
    }

    if (event == QLatin1String("ack")
        && msg.value(QStringLiteral("cmd")).toString() == QLatin1String("auth")) {
        if (!connected_.exchange(true)) {
            qDebug("[NetworkBridge] Authenticated packet bridge session");
            emit connectionChanged(true, QStringLiteral("Connected to authenticated packet bridge"));
        }
        return;
    }

    // Delay-verb acks piggyback the same applied-delay snapshot as the state
    // broadcast above; while the controller keepalives at ~150 ms this is the
    // continuous echo stream that keeps the card's service-reported value live.
    if (event == QLatin1String("ack")) {
        const QString ackCmd = msg.value(QStringLiteral("cmd")).toString();
        if (ackCmd == QLatin1String("set_meter_delay")
            || ackCmd == QLatin1String("start_meter_intercept")
            || ackCmd == QLatin1String("stop_meter_intercept")) {
            if (connected_.load()) {
                maybeEmitMeterDelayEcho(msg);
            }
            return;
        }
    }

    if (event == QLatin1String("error")
        && msg.value(QStringLiteral("msg")).toString() == QLatin1String("unauthorized")) {
        authenticationRejected_ = true;
        connected_ = false;
        // A bare "rejected" cost a 10-hour forensic detour once already. Say which token file the
        // client used and what the bridge said was wrong with it; nexus_svc supplies `reason` and
        // the path it published to (neither contains the secret).
        const QString reason = msg.value(QStringLiteral("reason")).toString();
        const QString servicePath = msg.value(QStringLiteral("token_path")).toString();
        QString detail;
        if (reason == QLatin1String("no_server_token")) {
            detail = QStringLiteral(
                "the bridge never published a token (it could not write one anywhere) — see "
                "nexus_svc.log for 'Bridge token could not be published'");
        } else if (reason == QLatin1String("token_mismatch")) {
            detail = QStringLiteral("client token from %1 does not match the bridge's token at %2")
                         .arg(tokenPath_.isEmpty() ? QStringLiteral("(unknown)") : tokenPath_)
                         .arg(servicePath.isEmpty() ? QStringLiteral("(unreported)") : servicePath);
        } else if (!reason.isEmpty()) {
            detail = QStringLiteral("bridge reported '%1'").arg(reason);
        } else {
            detail = QStringLiteral("bridge gave no reason (older nexus_svc)");
        }
        const QString message =
            QStringLiteral("Packet bridge authentication rejected: %1 [%2]")
                .arg(detail)
                .arg(tokenDiagnostic_.isEmpty() ? QStringLiteral("no token diagnostic")
                                                : tokenDiagnostic_);
        qWarning("[NetworkBridge] %s", qPrintable(message));
        emit connectionChanged(false, message);
        return;
    }

    // The disarmed refusal is a STATE, not a transient fault: nexus_svc's
    // _armed_delay_or_error() answers it for EVERY delay verb while the intercept is
    // disarmed (its deliberate loud-path design). It must not fall through to the
    // generic error branch below — that both spams connectionChanged once per
    // keepalive verb and buries the one fact that matters: NO delay is applied even
    // though the bridge is connected and the card's actuator state says otherwise.
    if (event == QLatin1String("error")
        && msg.value(QStringLiteral("msg")).toString()
               == QLatin1String("meter_delay_disarmed")) {
        setMeterDelayServiceState(
            MeterDelayServiceState::Disarmed,
            QStringLiteral("service refused '%1': meter_delay_disarmed — arm nexus_svc "
                           "with --arm-meter-delay or ORION_METER_DELAY_ARMED=1")
                .arg(msg.value(QStringLiteral("cmd")).toString()));
        return;
    }

    if (event == QLatin1String("packet")) {
        if (!connected_.load()) return;
        const auto srcIp = msg.value(QStringLiteral("src_ip")).toString();
        const auto dstIp = msg.value(QStringLiteral("dst_ip")).toString();
        const auto proto = msg.value(QStringLiteral("proto")).toString();
        const int srcPort = msg.value(QStringLiteral("src_port")).toInt();
        const int dstPort = msg.value(QStringLiteral("dst_port")).toInt();
        const int size = msg.value(QStringLiteral("size")).toInt();
        const double ts = msg.value(QStringLiteral("ts")).toDouble();

        if (proto != QLatin1String("UDP")) return;
        if (srcPort <= 0 || srcPort > 65535
            || dstPort <= 0 || dstPort > 65535
            || size <= 0 || size > 65535
            || !std::isfinite(ts) || ts <= 0.0) {
            qWarning("[NetworkBridge] Rejected malformed packet event from bridge");
            return;
        }

        // The elevated capture service is not yet an authenticated packet
        // source. When the user configured a console, require every candidate
        // to include that exact private endpoint; another LAN host cannot earn
        // a diagnostic court label merely by using the same UDP port range.
        if (!consoleIp_.isEmpty()
            && srcIp != consoleIp_ && dstIp != consoleIp_) {
            return;
        }

        const qint64 observedAtMs = monotonicNowMs();
        if (!passiveCourtFlow_.observe(
                srcIp, dstIp, srcPort, dstPort,
                PassiveCourtFlowClassifier::Transport::Udp, observedAtMs)) {
            return;
        }
        applyPassiveSnapshot(passiveCourtFlow_.snapshot(observedAtMs));

        // Retained for explicitly authorized development experiments. The
        // production controller compiles the forwarding path out; this bridge
        // itself never calls RemotePlaySession, sends a probe, or mutates timing.
        emit packetObserved(srcIp, dstIp, srcPort, dstPort, size, ts);

    } else if (event == QLatin1String("ready")) {
        if (connected_.load()) {
            emit connectionChanged(true, QStringLiteral("Packet capture active"));
        }
    } else if (event == QLatin1String("error")) {
        emit connectionChanged(connected_.load(),
                               QStringLiteral("Bridge: %1").arg(msg.value(QStringLiteral("msg")).toString()));
    }
}

} // namespace orion
