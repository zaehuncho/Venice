#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  IpcServer.h — the meter-delay bridge IPC endpoint (byte-compatible port)
// ───────────────────────────────────────────────────────────────────────────
//
//  TRANSPORT DECISION (load-bearing — read before "fixing" this to a pipe):
//  nexus_svc.py's IPC is a loopback TCP JSON-lines socket on 127.0.0.1:47291
//  with a bearer-token-file handshake — NOT a Windows named pipe. The task's
//  overriding constraint is "speak the EXACT same wire protocol as nexus_svc.py,
//  byte-for-byte; any protocol change breaks the sibling agent." The existing
//  shipped client (native_orion/src/NetworkBridge.cpp) connects a TCP socket to
//  47291 and authenticates with %PROGRAMDATA%/%LOCALAPPDATA%\NexusVision\
//  nexus_bridge.token. Switching to a named pipe would break BOTH that client
//  and any wave-2B client that also read nexus_svc.py for the protocol. So the
//  "existing name" this endpoint keeps is the existing loopback endpoint
//  identity: host 127.0.0.1, port 47291, token file nexus_bridge.token.
//
//  This class replicates, verb-for-verb and event-for-event:
//    commands: auth, ping, set_filter, clear_filter, set_court_ip,
//              start_meter_intercept, stop_meter_intercept, set_meter_delay,
//              meter_delay_stats, status
//    events:   hello, ack, error, pong, meter_delay, packet, ready, status
//  and the disarmed loud-path (meter_delay_disarmed), the F1 token auth, the
//  per-client broadcast gating, and the dead-man switch on last-client
//  disconnect.

#include "Json.h"
#include "MeterDelayIntercept.h"
#include "ServiceState.h"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace venicenet {

// v3 protocol (SVC_VERSION in nexus_svc.py). Unknown verbs return an explicit
// error; hello advertises "meter_delay" ONLY when armed.
inline constexpr int kSvcVersion = 3;
inline constexpr const char* kListenHost = "127.0.0.1";
inline constexpr int kListenPort = 47291;
inline constexpr const char* kBridgeTokenFilename = "nexus_bridge.token";
inline constexpr int kBridgeTokenRecordVersion = 1;
inline constexpr size_t kMaxCommandLineBytes = 16 * 1024;
inline constexpr size_t kMaxCommandBufferBytes = 64 * 1024;

// One connected client's outbound queue, gated on auth (capture events are
// privileged and must not reach an unauthenticated loopback client).
struct ClientQueue {
    static constexpr size_t kMaxQueue = 512;
    std::mutex m;
    std::condition_variable cv;
    std::deque<std::string> lines;
    bool authed = false;
    bool closed = false;

    void push(const std::string& line);
    void markAuthed();
    void close();
};

class BroadcastHub {
public:
    std::shared_ptr<ClientQueue> addClient();
    void removeClient(const std::shared_ptr<ClientQueue>& q);
    int authedClientCount();
    void broadcast(const std::string& line);

private:
    std::mutex mutex_;
    std::vector<std::shared_ptr<ClientQueue>> clients_;
};

class IpcServer {
public:
    IpcServer() = default;
    ~IpcServer();

    IpcServer(const IpcServer&) = delete;
    IpcServer& operator=(const IpcServer&) = delete;

    // Wiring. delay==nullptr means the intercept is disarmed (shipping default);
    // every delay verb then answers meter_delay_disarmed.
    void configure(MeterDelayIntercept* delay, ServiceStateMachine* state, bool armed,
                   bool pydivertOk);
    // Invoked when the console-IP filter changes (set_filter/clear_filter) so the
    // sniff loop can restart its own handle. Optional.
    void setFilterChangedCallback(std::function<void()> fn) { onFilterChanged_ = std::move(fn); }
    void setLogger(MeterDelayIntercept::LogFn fn) { log_ = std::move(fn); }

    BroadcastHub& hub() { return hub_; }

    // ── Token (F1 auth) ──
    // mode = "service" (LocalSystem -> ProgramData, INTERACTIVE read) or "debug"
    // (unelevated user -> LocalAppData, owner-only). Returns true if a hardened,
    // verified token was published; false leaves auth fail-closed (every auth
    // rejected) exactly like nexus_svc.
    bool publishToken(const std::string& mode);
    const std::string& tokenPath() const { return tokenPath_; }
    const std::string& tokenMode() const { return tokenMode_; }

    // ── Console / court IP state (the _WinDivertLoop IP fields) ──
    void setConsoleIpFromClient(const std::string& ip);
    void clearConsoleIp();
    void setCourtIpFromClient(const std::string& ip);
    // Server-side passive detection may propose a court IP when the client has
    // not pinned one; a client set always wins.
    void setDetectedCourtIp(const std::string& ip);
    std::pair<std::string, std::string> currentIps() const;
    std::string consoleIp() const;

    // ── Server lifecycle ──
    bool startListening();
    void stop();

    // Emit the {"event":"meter_delay",...} state broadcast (also used as the
    // MeterDelayIntercept state broadcaster).
    void broadcastMeterDelay(const std::string& state, const std::string& reason);
    // Broadcast a raw packet observation (the {"event":"packet",...} feed the
    // existing NetworkBridge classifier consumes).
    void broadcastPacket(const std::string& srcIp, const std::string& dstIp, int srcPort,
                         int dstPort, const std::string& proto, int size, bool outbound, double tsMs);
    void broadcastReady(const std::string& filter);
    void broadcastError(const std::string& msg);

    // ── Pure protocol helpers (unit-tested) ──
    static std::string extractCmdVerb(const std::string& line);
    static bool constantTimeEquals(const std::string& a, const std::string& b);
    static std::string buildHelloLine(int version, const std::vector<std::string>& features);
    static std::string buildUnauthorizedLine(const std::string& reason, const std::string& path,
                                             const std::string& mode);
    static std::string buildDisarmedErrorLine(const std::string& verb);
    static std::string buildUnknownCommandLine(const std::string& verb);
    static void addSnapshotFields(JsonValue& obj, const MeterDelayIntercept::Snapshot& s);

private:
    void acceptLoop();
    void handleClient(std::uintptr_t sock);
    void clientWriter(std::uintptr_t sock, std::shared_ptr<ClientQueue> q,
                      std::shared_ptr<std::atomic<bool>> done);
    void handleCommand(std::shared_ptr<ClientQueue> q, bool& authed, bool& authFailureLogged,
                       const JsonValue& cmd);
    void onClientDisconnected(const std::shared_ptr<ClientQueue>& q);
    MeterDelayIntercept* armedDelayOrError(std::shared_ptr<ClientQueue>& q, const std::string& verb);
    void retargetIntercept();

    BroadcastHub hub_;
    MeterDelayIntercept* delay_ = nullptr;
    ServiceStateMachine* state_ = nullptr;
    bool armed_ = false;
    bool pydivertOk_ = false;

    std::function<void()> onFilterChanged_;
    MeterDelayIntercept::LogFn log_;

    mutable std::mutex ipMutex_;
    std::string consoleIp_;
    std::string courtIp_;
    bool courtIpClientSet_ = false;

    std::string token_;
    std::string tokenPath_;
    std::string tokenMode_;

    std::uintptr_t listenSock_ = ~static_cast<std::uintptr_t>(0);
    std::atomic<bool> running_{false};
    std::thread acceptThread_;
    std::vector<std::thread> clientThreads_;
    std::mutex clientThreadsMutex_;
};

} // namespace venicenet
