// ───────────────────────────────────────────────────────────────────────────
//  VeniceNetIpcClientTests — drives the wave-2B IPC client against a mock bridge
// ───────────────────────────────────────────────────────────────────────────
//
//  Qt-free by contract. Spins up a mock loopback-TCP server that speaks the
//  nexus_svc wire protocol (hello, auth ack, meter_delay broadcasts, error
//  events) on an ephemeral port, points the DLL at it via VENICENET_BRIDGE_PORT
//  + VENICENET_BRIDGE_TOKEN, and pins:
//    * connect + token auth handshake and the exact command translation
//      (set_filter / start_meter_intercept / set_meter_delay / new verbs),
//    * snapshot state inference from meter_delay broadcasts (armed/applied/
//      buffer/driver_state) and from the disarmed refusal,
//    * the "service not running" degrade path (driver_state == NOT_INSTALLED),
//    * the callback re-entry contract (a callback must never call venicenet_*),
//    * reconnect replay of the config mirror.
//
//  See docs/VENICENET_API.md for the behavioural contract under test.
// ───────────────────────────────────────────────────────────────────────────

#include "VeniceNet.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            ++g_failures;                                                        \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        }                                                                        \
    } while (0)

bool waitFor(const std::function<bool()>& pred, int timeoutMs = 3000)
{
    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::milliseconds(timeoutMs);
    while (std::chrono::steady_clock::now() < deadline) {
        if (pred()) return true;
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }
    return pred();
}

VeniceNetSnapshot makeSnapshotRequest()
{
    VeniceNetSnapshot snap;
    std::memset(&snap, 0xCD, sizeof(snap));
    snap.struct_size = sizeof(VeniceNetSnapshot);
    return snap;
}

// A minimal mock of the nexus_svc TCP listener. One client at a time is enough:
// the DLL keeps a single connection.
class MockBridge {
public:
    bool start(const std::string& token, bool sendHelloFeatures, bool armed)
    {
        return startOnPort(token, sendHelloFeatures, armed, 0); // ephemeral
    }

    bool startOnPort(const std::string& token, bool sendHelloFeatures, bool armed,
                     uint16_t requestedPort)
    {
        token_ = token;
        sendHelloFeatures_ = sendHelloFeatures;
        armed_ = armed;
        listen_ = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        if (listen_ == INVALID_SOCKET) return false;
        int reuse = 1;
        ::setsockopt(listen_, SOL_SOCKET, SO_REUSEADDR,
                     reinterpret_cast<const char*>(&reuse), sizeof(reuse));
        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(requestedPort);
        ::inet_pton(AF_INET, "127.0.0.1", &addr.sin_addr);
        if (::bind(listen_, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
            ::closesocket(listen_);
            listen_ = INVALID_SOCKET;
            return false;
        }
        sockaddr_in bound{};
        int len = sizeof(bound);
        if (::getsockname(listen_, reinterpret_cast<sockaddr*>(&bound), &len) != 0) {
            ::closesocket(listen_);
            listen_ = INVALID_SOCKET;
            return false;
        }
        port_ = ntohs(bound.sin_port);
        ::listen(listen_, 4);
        running_.store(true);
        thread_ = std::thread([this]() { serve(); });
        return true;
    }

    void stop()
    {
        running_.store(false);
        if (listen_ != INVALID_SOCKET) {
            ::closesocket(listen_);
            listen_ = INVALID_SOCKET;
        }
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (client_ != INVALID_SOCKET) {
                ::shutdown(client_, SD_BOTH);
                ::closesocket(client_);
                client_ = INVALID_SOCKET;
            }
        }
        if (thread_.joinable()) thread_.join();
    }

    uint16_t port() const { return port_; }
    bool authed() const { return authed_.load(); }
    bool sawStart() const { return sawStart_.load(); }
    bool sawStop() const { return sawStop_.load(); }
    bool sawSetFilter() const { return sawSetFilter_.load(); }
    bool sawSetDelay() const { return sawSetDelay_.load(); }
    bool sawEngagePolicy() const { return sawEngagePolicy_.load(); }
    bool sawShotEdge() const { return sawShotEdge_.load(); }
    int  setDelayCount() const { return setDelayCount_.load(); }

    std::vector<std::string> commands()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        return commands_;
    }

    // Broadcast a raw line (test drives service->client events).
    bool sendLine(const std::string& line)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (client_ == INVALID_SOCKET) return false;
        const std::string out = line + "\n";
        return ::send(client_, out.data(), static_cast<int>(out.size()), 0)
               == static_cast<int>(out.size());
    }

private:
    void serve()
    {
        while (running_.load()) {
            SOCKET c = ::accept(listen_, nullptr, nullptr);
            if (c == INVALID_SOCKET) {
                return;
            }
            {
                std::lock_guard<std::mutex> lock(mutex_);
                client_ = c;
            }
            // Greet exactly like nexus_svc: hello before auth.
            if (sendHelloFeatures_) {
                const std::string hello = armed_
                    ? "{\"event\":\"hello\",\"version\":3,\"features\":[\"meter_delay\"]}\n"
                    : "{\"event\":\"hello\",\"version\":3,\"features\":[]}\n";
                sendClient(hello);
            } else {
                sendClient("{\"event\":\"hello\",\"version\":2}\n");
            }
            std::string buf;
            char tmp[4096];
            while (running_.load()) {
                const int n = ::recv(c, tmp, sizeof(tmp), 0);
                if (n <= 0) break;
                buf.append(tmp, static_cast<size_t>(n));
                size_t nl;
                while ((nl = buf.find('\n')) != std::string::npos) {
                    const std::string line = buf.substr(0, nl);
                    buf.erase(0, nl + 1);
                    onCommand(line);
                }
            }
            {
                std::lock_guard<std::mutex> lock(mutex_);
                if (client_ != INVALID_SOCKET) {
                    ::closesocket(client_);
                    client_ = INVALID_SOCKET;
                }
            }
        }
    }

    void sendClient(const std::string& s)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (client_ != INVALID_SOCKET) {
            ::send(client_, s.data(), static_cast<int>(s.size()), 0);
        }
    }

    void onCommand(const std::string& line)
    {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            commands_.push_back(line);
        }
        if (line.find("\"cmd\":\"auth\"") != std::string::npos) {
            const std::string needle = "\"token\":\"" + token_ + "\"";
            if (line.find(needle) != std::string::npos) {
                authed_.store(true);
                sendClient("{\"event\":\"ack\",\"cmd\":\"auth\"}\n");
            } else {
                sendClient("{\"event\":\"error\",\"msg\":\"unauthorized\","
                           "\"reason\":\"token_mismatch\"}\n");
            }
            return;
        }
        if (!armed_ && (line.find("meter_intercept") != std::string::npos
                        || line.find("set_meter_delay") != std::string::npos)) {
            sendClient("{\"event\":\"error\",\"msg\":\"meter_delay_disarmed\","
                       "\"cmd\":\"set_meter_delay\"}\n");
            return;
        }
        if (line.find("\"cmd\":\"set_filter\"") != std::string::npos) {
            sawSetFilter_.store(true);
            sendClient("{\"event\":\"ack\",\"cmd\":\"set_filter\"}\n");
        } else if (line.find("\"cmd\":\"start_meter_intercept\"") != std::string::npos) {
            sawStart_.store(true);
            sendClient("{\"event\":\"ack\",\"cmd\":\"start_meter_intercept\","
                       "\"court_ip\":\"203.0.113.5\",\"meter_delay_active\":true,"
                       "\"meter_delay_ms\":0.0,\"meter_buffer_depth\":0}\n");
        } else if (line.find("\"cmd\":\"stop_meter_intercept\"") != std::string::npos) {
            sawStop_.store(true);
            sendClient("{\"event\":\"ack\",\"cmd\":\"stop_meter_intercept\","
                       "\"meter_delay_active\":false,\"meter_delay_ms\":0.0,"
                       "\"meter_buffer_depth\":0}\n");
        } else if (line.find("\"cmd\":\"set_meter_delay\"") != std::string::npos) {
            sawSetDelay_.store(true);
            setDelayCount_.fetch_add(1);
        } else if (line.find("\"cmd\":\"set_engage_policy\"") != std::string::npos) {
            sawEngagePolicy_.store(true);
        } else if (line.find("\"cmd\":\"notify_shot_edge\"") != std::string::npos) {
            sawShotEdge_.store(true);
        }
    }

    SOCKET listen_ = INVALID_SOCKET;
    SOCKET client_ = INVALID_SOCKET;
    std::mutex mutex_;
    std::thread thread_;
    std::atomic<bool> running_{false};
    std::atomic<bool> authed_{false};
    std::atomic<bool> sawStart_{false};
    std::atomic<bool> sawStop_{false};
    std::atomic<bool> sawSetFilter_{false};
    std::atomic<bool> sawSetDelay_{false};
    std::atomic<bool> sawEngagePolicy_{false};
    std::atomic<bool> sawShotEdge_{false};
    std::atomic<int>  setDelayCount_{0};
    std::vector<std::string> commands_;
    std::string token_;
    bool sendHelloFeatures_ = true;
    bool armed_ = true;
    uint16_t port_ = 0;
};

void setEnv(const char* key, const std::string& value)
{
    ::SetEnvironmentVariableA(key, value.empty() ? nullptr : value.c_str());
}

int driverState()
{
    VeniceNetSnapshot snap = makeSnapshotRequest();
    if (venicenet_snapshot(&snap) != VENICENET_OK) return -1;
    return snap.driver_state;
}

// ── Test 1: no service listening -> graceful NOT_INSTALLED, never a crash. ──
void testServiceAbsentDegradesGracefully()
{
    setEnv("VENICENET_BRIDGE_PORT", "9"); // discard port; nothing listens
    setEnv("VENICENET_BRIDGE_TOKEN", "unused");
    CHECK(venicenet_init() == VENICENET_OK);
    // Configure while offline: setters must succeed and mirror.
    CHECK(venicenet_set_console_ip("192.168.137.100") == VENICENET_OK);
    CHECK(venicenet_set_target_delay_ms(210.0) == VENICENET_OK);
    CHECK(venicenet_set_enabled(true) == VENICENET_OK);
    // Give the I/O thread a moment to fail its connect a few times.
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.driver_state == VENICENET_DRIVER_NOT_INSTALLED);
    CHECK(snap.armed == 0);
    CHECK(snap.enabled == 1);            // intent still mirrored
    CHECK(snap.target_delay_ms == 210.0);
    venicenet_shutdown();
}

// ── Test 2: full connect + auth + command translation + snapshot inference. ──
void testConnectAuthTranslateAndSnapshot()
{
    MockBridge bridge;
    CHECK(bridge.start("secrettoken123", /*sendHelloFeatures=*/true, /*armed=*/true));
    setEnv("VENICENET_BRIDGE_PORT", std::to_string(bridge.port()));
    setEnv("VENICENET_BRIDGE_TOKEN", "secrettoken123");

    CHECK(venicenet_init() == VENICENET_OK);
    CHECK(venicenet_set_console_ip("192.168.137.100") == VENICENET_OK);
    CHECK(venicenet_set_target_delay_ms(204.5) == VENICENET_OK);
    CHECK(venicenet_set_engage_policy(VENICENET_POLICY_OFFENSE_DEFENSE) == VENICENET_OK);
    CHECK(venicenet_set_enabled(true) == VENICENET_OK);

    CHECK(waitFor([&]() { return bridge.authed(); }));
    // Once authed, the config mirror is replayed and the enable path drives the
    // intercept lifecycle verbs.
    CHECK(waitFor([&]() { return bridge.sawSetFilter(); }));
    CHECK(waitFor([&]() { return bridge.sawStart(); }));
    CHECK(waitFor([&]() { return bridge.sawSetDelay(); }));
    CHECK(waitFor([&]() { return bridge.sawEngagePolicy(); }));

    // driver_state should reach READY (connected) and the start ack (active=true)
    // drives HANDLE_OPEN.
    CHECK(waitFor([&]() { return driverState() == VENICENET_DRIVER_HANDLE_OPEN; }));

    // Drive a service broadcast and confirm it flows into the snapshot.
    CHECK(bridge.sendLine(
        "{\"event\":\"meter_delay\",\"state\":\"started\",\"reason\":\"client_request\","
        "\"meter_delay_active\":true,\"meter_delay_ms\":204.5,\"meter_buffer_depth\":7}"));
    CHECK(waitFor([&]() {
        VeniceNetSnapshot s = makeSnapshotRequest();
        return venicenet_snapshot(&s) == VENICENET_OK
               && s.armed == 1 && s.applied_delay_ms == 204.5 && s.buffer_depth == 7;
    }));

    // The court_ip carried by the start ack is surfaced as detection output.
    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.court_ip_detected == 1);
    CHECK(std::strcmp(snap.court_ip, "203.0.113.5") == 0);
    CHECK(snap.driver_state == VENICENET_DRIVER_HANDLE_OPEN);

    // notify_shot_edge maps to the new wire verb.
    CHECK(venicenet_notify_shot_edge() == VENICENET_OK);
    CHECK(waitFor([&]() { return bridge.sawShotEdge(); }));

    venicenet_shutdown();
    bridge.stop();
}

// ── Test 3: disarmed service -> NOT_STARTED, never HANDLE_OPEN. ──
void testDisarmedServiceReportsNotStarted()
{
    MockBridge bridge;
    CHECK(bridge.start("tok", /*sendHelloFeatures=*/true, /*armed=*/false));
    setEnv("VENICENET_BRIDGE_PORT", std::to_string(bridge.port()));
    setEnv("VENICENET_BRIDGE_TOKEN", "tok");

    CHECK(venicenet_init() == VENICENET_OK);
    CHECK(venicenet_set_enabled(true) == VENICENET_OK);
    CHECK(venicenet_set_target_delay_ms(200.0) == VENICENET_OK);
    CHECK(waitFor([&]() { return bridge.authed(); }));
    // The disarmed refusal (or the empty hello features) must drive NOT_STARTED
    // and never let armed go true.
    CHECK(waitFor([&]() { return driverState() == VENICENET_DRIVER_NOT_STARTED; }));
    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.armed == 0);
    CHECK(snap.driver_state == VENICENET_DRIVER_NOT_STARTED);

    venicenet_shutdown();
    bridge.stop();
}

// ── Test 4: bad token -> access denied fault, no crash, retries. ──
void testAuthRejectionSurfacesAccessDenied()
{
    MockBridge bridge;
    CHECK(bridge.start("correct-token", true, true));
    setEnv("VENICENET_BRIDGE_PORT", std::to_string(bridge.port()));
    setEnv("VENICENET_BRIDGE_TOKEN", "wrong-token");

    CHECK(venicenet_init() == VENICENET_OK);
    CHECK(waitFor([&]() {
        VeniceNetSnapshot s = makeSnapshotRequest();
        return venicenet_snapshot(&s) == VENICENET_OK
               && s.driver_state == VENICENET_DRIVER_ERROR
               && s.error_code == VENICENET_ERR_DRIVER_ACCESS_DENIED;
    }));
    venicenet_shutdown();
    bridge.stop();
}

// ── Test 5: callback fires from the I/O thread and the re-entry ban holds. ──
struct CallbackProbe {
    std::atomic<int> calls{0};
    std::atomic<int> reentryStatus{-999};
    std::atomic<bool> sawHandleOpen{false};
};

void probeCallback(const VeniceNetSnapshot* snap, void* user)
{
    auto* probe = static_cast<CallbackProbe*>(user);
    probe->calls.fetch_add(1);
    if (snap->driver_state == VENICENET_DRIVER_HANDLE_OPEN) {
        probe->sawHandleOpen.store(true);
    }
    // The contract forbids calling venicenet_* from the callback. We do NOT call
    // one here (that would be the violation); this probe simply proves the
    // callback runs on the I/O thread without the internal lock held — if the
    // engine held mutex_ across the invocation, a concurrent snapshot() from the
    // main thread below would deadlock. We record that we ran.
    probe->reentryStatus.store(0);
}

void testCallbackFromWorkerThreadNoDeadlock()
{
    MockBridge bridge;
    CHECK(bridge.start("cbtok", true, true));
    setEnv("VENICENET_BRIDGE_PORT", std::to_string(bridge.port()));
    setEnv("VENICENET_BRIDGE_TOKEN", "cbtok");

    CHECK(venicenet_init() == VENICENET_OK);
    CallbackProbe probe;
    CHECK(venicenet_register_state_callback(&probeCallback, &probe) == VENICENET_OK);
    // Prime fired once synchronously.
    CHECK(probe.calls.load() == 1);

    CHECK(venicenet_set_enabled(true) == VENICENET_OK);
    CHECK(waitFor([&]() { return bridge.authed(); }));
    CHECK(bridge.sendLine(
        "{\"event\":\"meter_delay\",\"meter_delay_active\":true,"
        "\"meter_delay_ms\":165.0,\"meter_buffer_depth\":3}"));
    // The callback must fire from the worker thread on the state change, and a
    // concurrent snapshot() from this thread must not deadlock against it.
    CHECK(waitFor([&]() { return probe.sawHandleOpen.load(); }));
    for (int i = 0; i < 200; ++i) {
        VeniceNetSnapshot s = makeSnapshotRequest();
        (void)venicenet_snapshot(&s);
    }
    CHECK(probe.calls.load() >= 2);

    venicenet_register_state_callback(nullptr, nullptr);
    venicenet_shutdown();
    bridge.stop();
}

// ── Test 6: reconnect replays the config mirror onto a fresh connection. ──
void testReconnectReplaysConfig()
{
    setEnv("VENICENET_BRIDGE_TOKEN", "rtok");
    // First server generation.
    MockBridge* first = new MockBridge();
    CHECK(first->start("rtok", true, true));
    setEnv("VENICENET_BRIDGE_PORT", std::to_string(first->port()));

    CHECK(venicenet_init() == VENICENET_OK);
    CHECK(venicenet_set_console_ip("192.168.137.50") == VENICENET_OK);
    CHECK(venicenet_set_target_delay_ms(180.0) == VENICENET_OK);
    CHECK(venicenet_set_enabled(true) == VENICENET_OK);
    CHECK(waitFor([&]() { return first->sawStart(); }));
    const uint16_t port = first->port();
    first->stop();
    delete first;

    // Second server generation on the SAME port (the DLL reconnects to it).
    MockBridge second;
    // Bind the same ephemeral port; if the OS reassigned it, skip the strict
    // check but still prove no crash on reconnect.
    setEnv("VENICENET_BRIDGE_PORT", std::to_string(port));
    // Best-effort rebind on the same port via SO_REUSEADDR in start(); retry.
    bool bound = false;
    for (int i = 0; i < 50 && !bound; ++i) {
        bound = second.startOnPort("rtok", true, true, port);
        if (!bound) std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    if (bound) {
        // On reconnect the mirror (console + enabled + target) is replayed.
        CHECK(waitFor([&]() { return second.sawSetFilter(); }, 8000));
        CHECK(waitFor([&]() { return second.sawStart(); }, 8000));
    } else {
        std::fprintf(stderr, "NOTE: could not rebind port %u; reconnect-replay "
                             "assertions skipped (no crash is still proven)\n", port);
    }
    venicenet_shutdown();
    second.stop();
}

} // namespace

int main()
{
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        std::fprintf(stderr, "WSAStartup failed\n");
        return 1;
    }

    testServiceAbsentDegradesGracefully();
    testConnectAuthTranslateAndSnapshot();
    testDisarmedServiceReportsNotStarted();
    testAuthRejectionSurfacesAccessDenied();
    testCallbackFromWorkerThreadNoDeadlock();
    testReconnectReplaysConfig();

    WSACleanup();

    // Clear the test env so nothing leaks into a subsequent process.
    ::SetEnvironmentVariableA("VENICENET_BRIDGE_PORT", nullptr);
    ::SetEnvironmentVariableA("VENICENET_BRIDGE_TOKEN", nullptr);

    if (g_failures != 0) {
        std::fprintf(stderr, "VeniceNetIpcClientTests: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("VeniceNetIpcClientTests: all checks passed\n");
    return 0;
}
