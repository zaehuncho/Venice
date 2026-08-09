#pragma once

#include "OrionExports.h"
#include "OrionTypes.h"
#include "PassiveCourtFlowClassifier.h"

#include <QtCore/QByteArray>
#include <QtCore/QString>
#include <QtCore/QStringList>
#include <QtCore/QMutex>
#include <QtCore/QObject>
#include <QtCore/QThread>
#include <QtCore/QTimer>
#include <QtCore/QVector>
#include <QtCore/QtGlobal>

#include <atomic>
#include <deque>

QT_BEGIN_NAMESPACE
class QJsonObject;
QT_END_NAMESPACE

namespace orion {

// --- Packet-bridge bearer token discovery ---------------------------------------------------
// nexus_svc mints a per-session token and publishes it where the reader can be restricted to the
// signed-in user without a privileged operation.  That is a different directory depending on how
// the bridge was started:
//
//   service mode (LocalSystem)   -> %PROGRAMDATA%\NexusVision\nexus_bridge.token
//   debug mode (ordinary user)   -> %LOCALAPPDATA%\NexusVision\nexus_bridge.token
//
// Both files can exist at the same time, and the one that does not belong to the running bridge is
// actively harmful: authenticating with it yields a bare "unauthorized" that is indistinguishable
// from a real permission fault.  Each record therefore carries the publishing pid, and a record
// whose process is provably gone is rejected as stale rather than sent.
struct BridgeTokenRecord {
    QString path;
    QString token;
    qint64  pid = 0;
    qint64  startedMs = 0;
    bool    exists = false;      // a file was present at `path`
    bool    structured = false;  // v1 JSON record carrying pid/started_ms, not a bare token
    // False ONLY when the publishing process is provably gone.  An access-denied probe (what an
    // unelevated client can get for a LocalSystem service) counts as alive, otherwise a perfectly
    // healthy service-mode token would be thrown away as stale.
    bool    ownerAlive = true;
};

struct BridgeTokenSelection {
    QString token;                // empty => do not attempt auth at all
    QString path;                 // where `token` came from
    QString diagnostic;           // always populated; contains no secret, safe to log verbatim
    bool    staleRejected = false;
};

// Candidate locations, per-user first.  Order is informational: selectBridgeToken() decides on
// liveness and recency, because neither location is authoritative on its own.
[[nodiscard]] ORION_REMOTEPLAY_API QStringList bridgeTokenCandidatePaths();

// Parses one token file body: a v1 JSON record, or a pre-v1 bare token.
[[nodiscard]] ORION_REMOTEPLAY_API BridgeTokenRecord parseBridgeTokenRecord(const QString& path,
                                                                           const QByteArray& body);

// Pure selection policy over already-gathered records; touches no filesystem and no process.
[[nodiscard]] ORION_REMOTEPLAY_API BridgeTokenSelection selectBridgeToken(
    const QVector<BridgeTokenRecord>& records);

// Reads every candidate path, probes publisher liveness, and applies selectBridgeToken().
[[nodiscard]] ORION_REMOTEPLAY_API BridgeTokenSelection resolveBridgeToken();

// --- Service hello parsing ------------------------------------------------------------------
// nexus_svc greets every accepted connection, pre-auth, with
//     {"event":"hello","version":N,"features":[...]}    (v3+)
//     {"event":"hello","version":N}                     (older services)
// `features` advertises "meter_delay" ONLY when the intercept was armed at service start
// (--arm-meter-delay / ORION_METER_DELAY_ARMED=1, nexus_svc.py _run_service); a disarmed v3
// service sends an EMPTY array. An absent field is an older service that cannot report
// arming at all — that must degrade to "unknown", never to "armed".
struct BridgeHelloInfo {
    int version = 0;           // 0 when the version field is absent or malformed
    bool hasFeatures = false;  // a features ARRAY was present (empty still counts)
    QStringList features;
    [[nodiscard]] bool advertisesMeterDelay() const
    {
        return features.contains(QStringLiteral("meter_delay"));
    }
};

// Pure parse of one hello message; tolerates missing/malformed fields (older services).
[[nodiscard]] ORION_REMOTEPLAY_API BridgeHelloInfo parseBridgeHello(const QJsonObject& msg);

class ORION_REMOTEPLAY_API NetworkBridge final : public QObject {
    Q_OBJECT
public:
    explicit NetworkBridge(QObject* parent = nullptr);
    ~NetworkBridge() override;

    void setConsoleIp(const QString& ip);
    void start();
    void stop();

    [[nodiscard]] bool connected() const noexcept { return connected_.load(); }
    [[nodiscard]] int playerCount() const noexcept { return playerCount_.load(); }

    // Version reported by the service's {"event":"hello","version":N} greeting.
    // 0 until a greeting has been seen on the current connection.
    [[nodiscard]] int serviceVersion() const noexcept { return serviceVersion_.load(); }

    // [ORION_METER_DELAY_ARM_STATE 2026-08-08] What the CONNECTED SERVICE says about
    // its meter-delay intercept. Distinct from both "bridge connected" and the
    // MeterDelayController's own ramp state: the controller keeps book-keeping its
    // ramp whether or not anything downstream holds a packet, and conflating the two
    // let the Meter Delay card display "Armed" against a service that ships DISARMED
    // by default (nexus_svc.py arm gate). Derived from the hello `features` array and
    // confirmed (or, for a service whose hello was missed, discovered) by the per-verb
    // meter_delay_disarmed refusal; reset to Unknown on every connection generation.
    enum class MeterDelayServiceState {
        Unknown = 0,  // no hello on this connection, or a pre-features (v<3) service
        Disarmed,     // the service refused/does not advertise the intercept — NO delay is applied
        Armed,        // the service advertises the meter_delay feature
    };
    Q_ENUM(MeterDelayServiceState)

    [[nodiscard]] MeterDelayServiceState meterDelayServiceState() const noexcept
    {
        return meterDelayServiceState_.load();
    }

    // ── Test seam ──────────────────────────────────────────────────────────────
    // Feeds one service line through the real protocol handler (handleLine) without
    // a socket, so the hello/error → state wiring is pinnable in tests. Emits the
    // same signals the live path does; callers own their signal spies.
    void ingestLineForTesting(const QByteArray& line) { handleLine(line); }

public slots:
    // [ORION_METER_DELAY 2026-08-07] MeterDelayController -> service wiring.
    //
    // sendSetMeterDelay: coalesceKey="set_meter_delay", lifecycleBarrier=false.
    //   The value is idempotent per key; the newest one wins so a ramp tick
    //   never queues a stale value ahead of the current target.
    //
    // startMeterIntercept / stopMeterIntercept: no coalesce key,
    //   lifecycleBarrier=true. These are ordering-sensitive verbs — a coalesced
    //   set_meter_delay must never hop over them, and vice versa.
    void sendSetMeterDelay(double ms);
    void startMeterIntercept();
    void stopMeterIntercept();

signals:
    void telemetryUpdated(orion::TelemetrySnapshot snapshot);
    void connectionChanged(bool connected, QString message);
    void playerCountChanged(int count);
    void packetObserved(QString srcIp, QString dstIp, int srcPort, int dstPort, int size, double ts);
    // Transition-deduped by setMeterDelayServiceState — NEVER per refused verb: the
    // disarmed refusal arrives once per keepalive command (~every 150 ms while the
    // controller is engaged). `cause` is human-readable and safe to log verbatim.
    void meterDelayServiceStateChanged(orion::NetworkBridge::MeterDelayServiceState state,
                                       QString cause);
    // [ORION_METER_DELAY_ECHO 2026-08-08] Display-only echo of the service's OWN
    // applied-delay snapshot (meter_delay_active / meter_delay_ms /
    // meter_buffer_depth), carried by the {"event":"meter_delay"} state broadcast and
    // by every delay-verb ack. This is what the bridge says it is actually DOING, as
    // opposed to what MeterDelayController's ramp book-keeping says it commanded.
    // Deliberately kept out of TelemetrySnapshot so it can never be mistaken for a
    // timing field: per PacketBridgeAuthority.h this bridge is
    // ServiceIdentityTrust::Unverified, so the echo feeds the Meter Delay card and
    // the log census ONLY. Change-deduped (worker thread).
    void meterDelayServiceEcho(bool active, double appliedMs, int bufferDepth);

private:
    void workerLoop();
    void handleLine(const QByteArray& line);
    [[nodiscard]] bool sendLine(quintptr socketHandle, const QByteArray& data);

    struct PendingCommand {
        QByteArray coalesceKey;
        QByteArray line;
    };
    // Enqueues a command for the worker thread and wakes it. `coalesceKey`
    // non-empty means "replace any not-yet-sent command with the same key"
    // (i.e. only the newest value of that key matters).
    // `lifecycleBarrier` marks ordering-sensitive verbs that coalescing must not
    // hop over.
    bool queueCommand(const QByteArray& coalesceKey, const QJsonObject& cmd,
                      bool lifecycleBarrier);
    // Requires cmdMutex_ to be held.
    void wakeWorkerLocked();
    void setServiceVersion(int version);
    // Stores + dedupes on transition (worker thread only), logs once per transition
    // and emits meterDelayServiceStateChanged.
    void setMeterDelayServiceState(MeterDelayServiceState state, const QString& cause);
    // [ORION_METER_DELAY_ECHO] Parses the service's applied-delay snapshot out of a
    // bridge message (state broadcast or delay-verb ack), validates it, dedupes
    // against the last emitted values and emits meterDelayServiceEcho. Receiving one
    // also proves the intercept object exists service-side, so it confirms Armed.
    // Worker thread only.
    void maybeEmitMeterDelayEcho(const QJsonObject& msg);

    QThread workerThread_;
    std::atomic<bool> running_{false};
    std::atomic<bool> connected_{false};
    std::atomic<int> playerCount_{0};
    std::atomic<int> serviceVersion_{0};
    std::atomic<MeterDelayServiceState> meterDelayServiceState_{MeterDelayServiceState::Unknown};

    QMutex ipMutex_;
    QString consoleIp_;
    QString pendingIp_;
    bool ipDirty_ = false;

    // Cross-thread command channel. Mirrors the ipMutex_/pendingIp_/ipDirty_
    // pattern and is drained in the same block of workerLoop().
    QMutex cmdMutex_;
    std::deque<PendingCommand> pendingCommands_;
    quintptr wakeSocket_ = 0;   // SOCKET, valid only while wakeSocketValid_
    bool wakeSocketValid_ = false;
    quint16 wakePort_ = 0;


    bool authenticationRejected_ = false; // worker-thread only
    // [ORION_METER_DELAY_ECHO] Change-dedupe state for meterDelayServiceEcho (worker
    // thread only). Reset on every connection generation so a reconnect re-emits.
    bool   lastEchoValid_ = false;
    bool   lastEchoActive_ = false;
    double lastEchoDelayMs_ = 0.0;
    int    lastEchoDepth_ = -1;
    // Where this connection's token came from, and why that one. Worker-thread only; kept so a
    // rejection can name the file it read instead of just saying "rejected".
    QString tokenPath_;
    QString tokenDiagnostic_;
    TelemetrySnapshot telemetry_;

    PassiveCourtFlowClassifier passiveCourtFlow_;
    bool playingGame_ = false;

    void resetTelemetryState();
    void checkIdleTimeout(qint64 nowMonotonicMs);
    void applyPassiveSnapshot(const PassiveCourtFlowClassifier::Snapshot& snapshot);
};

} // namespace orion
