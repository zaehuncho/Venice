// ───────────────────────────────────────────────────────────────────────────
//  VeniceNetSvc.exe — C++ port of nexus_svc.py's meter-delay bridge
// ───────────────────────────────────────────────────────────────────────────
//
//  WAVE 2A of the VeniceNet rewrite. Option 3: keep the SYSTEM service, rewrite
//  in C++, ship no Python runtime. This exe is a drop-in for NexusVisionSvc.exe
//  (the Nuitka-compiled nexus_svc.py): it registers under the SAME service name,
//  listens on the SAME loopback endpoint (127.0.0.1:47291) with the SAME bearer
//  token file, and speaks the SAME JSON-lines wire protocol, so both the shipped
//  NetworkBridge client and the wave-2B thin DLL client drive it unchanged. It
//  runs ALONGSIDE nexus_svc.py until wave 3-lite swaps them; nothing here is
//  registered or started on this rig (owner-only actions).
//
//  Command verbs mirror nexus_svc.py's main():
//    install | start | stop | remove | debug | driver-status | unload-driver
//  and a bare invocation runs the SCM control dispatcher (service mode).

// Winsock2 MUST precede <windows.h> (project builds with WIN32_LEAN_AND_MEAN, so
// windows.h does not pull the clashing winsock.h, but keep the canonical order).
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include "CourtIpDetector.h"
#include "IpcServer.h"
#include "MeterDelayIntercept.h"
#include "ServiceArgs.h"
#include "ServiceState.h"
#include "WinDivertApi.h"

#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <ctime>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

using namespace venicenet;

// Same identity as nexus_svc.py's SVC_* so wave 3-lite is a binary swap and the
// operator's owner_verify_meter_delay.ps1 (which queries "NexusVisionSvc") keeps
// working. NOT a rebrand — the internal orion-*/NexusVision slug is preserved.
const wchar_t* kSvcName = L"NexusVisionSvc";
const wchar_t* kSvcDisplayName = L"Nexus Vision Packet Bridge";
const wchar_t* kSvcDescription =
    L"Privileged WinDivert packet capture bridge for Nexus Vision. "
    L"Allows the main application to run without Administrator rights.";

// ── Logging ────────────────────────────────────────────────────────────────

std::mutex g_logMutex;
std::wstring g_logPath;

std::wstring exeDirW()
{
    wchar_t buf[MAX_PATH];
    const DWORD n = ::GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring path(buf, n);
    const size_t slash = path.find_last_of(L"\\/");
    return (slash == std::wstring::npos) ? std::wstring(L".") : path.substr(0, slash);
}

void logLine(const std::string& line)
{
    std::lock_guard<std::mutex> lock(g_logMutex);
    if (g_logPath.empty()) {
        g_logPath = exeDirW() + L"\\venicenet_svc.log";
    }
    FILE* f = nullptr;
    // Mode is plain "a" (append, byte-oriented). DO NOT use "a, ccs=UTF-8" here
    // — that puts the stream in Unicode CCS mode which the MSVC CRT then requires
    // to be driven with wide-char I/O (fwprintf/fputws). Calling std::fprintf on
    // a Unicode-CCS stream trips __fastfail(FAST_FAIL_INCORRECT_STACK) and
    // aborts the process with 0xC0000409 (BEX64) — every logLine call would
    // silently kill the service (owner rig 2026-08-08, log file was 3 bytes of
    // BOM with nothing after it). Everything logged here is ASCII / plain UTF-8
    // narrow bytes, so a byte-oriented file is the right choice.
    if (_wfopen_s(&f, g_logPath.c_str(), L"a") == 0 && f) {
        const std::time_t t = std::time(nullptr);
        char stamp[32];
        std::tm tmv{};
        localtime_s(&tmv, &t);
        std::strftime(stamp, sizeof(stamp), "%Y-%m-%d %H:%M:%S", &tmv);
        std::fprintf(f, "%s %s\n", stamp, line.c_str());
        std::fclose(f);
    }
}

// ── WinDivert driver residency (ports _windivert_driver_running /
//    _unload_windivert_driver) ────────────────────────────────────────────

// Returns 1 running, 0 not running / not installed, -1 unknown.
int windivertDriverRunning()
{
    SC_HANDLE scm = ::OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT);
    if (!scm) {
        return -1;
    }
    int result = -1;
    SC_HANDLE svc = ::OpenServiceW(scm, L"WinDivert", SERVICE_QUERY_STATUS);
    if (!svc) {
        // 1060 == service does not exist == not installed.
        result = (::GetLastError() == ERROR_SERVICE_DOES_NOT_EXIST) ? 0 : -1;
    } else {
        SERVICE_STATUS status{};
        if (::QueryServiceStatus(svc, &status)) {
            result = (status.dwCurrentState == SERVICE_RUNNING
                      || status.dwCurrentState == SERVICE_START_PENDING)
                ? 1
                : 0;
        }
        ::CloseServiceHandle(svc);
    }
    ::CloseServiceHandle(scm);
    return result;
}

// Stop the WinDivert kernel driver and VERIFY it is gone. Closing handles does
// not unload the driver (verified on-rig 2026-08-06); shipping a resident kernel
// driver after exit is not acceptable and was a stated reason the feature was
// pulled. MUST be called only after EVERY WinDivert handle is closed.
bool unloadWinDivertDriver(const std::string& reason)
{
    const int before = windivertDriverRunning();
    if (before == 0) {
        return true; // not running / not installed — nothing to unload
    }
    SC_HANDLE scm = ::OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT);
    if (!scm) {
        logLine("WinDivert unload: cannot open SCM (" + reason + ")");
        return false;
    }
    bool gone = false;
    SC_HANDLE svc = ::OpenServiceW(scm, L"WinDivert", SERVICE_STOP | SERVICE_QUERY_STATUS);
    if (svc) {
        SERVICE_STATUS status{};
        ::ControlService(svc, SERVICE_CONTROL_STOP, &status);
        // The stop is asynchronous; give the SCM a moment before believing it.
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(3);
        while (std::chrono::steady_clock::now() < deadline) {
            if (::QueryServiceStatus(svc, &status)
                && status.dwCurrentState == SERVICE_STOPPED) {
                gone = true;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
        }
        ::CloseServiceHandle(svc);
    } else if (::GetLastError() == ERROR_SERVICE_DOES_NOT_EXIST) {
        gone = true;
    } else if (::GetLastError() == ERROR_ACCESS_DENIED) {
        logLine("WinDivert unload: access denied (SCM error 5) — needs elevation. Service mode "
                "runs as LocalSystem and can do this; an unelevated debug bridge cannot. (" + reason
                + ")");
    }
    ::CloseServiceHandle(scm);
    if (gone) {
        logLine("WinDivert driver unloaded (" + reason + ")");
    } else {
        logLine("WinDivert driver STILL RESIDENT after unload attempt (" + reason
                + ") — most likely a handle is still open, or the stop lacked privilege");
    }
    return gone;
}

// ── IPv4 packet parse (for the sniff loop's packet dispatch) ──────────────

struct ParsedPacket {
    bool ok = false;
    std::string proto;
    std::string srcIp;
    std::string dstIp;
    int srcPort = 0;
    int dstPort = 0;
    int size = 0;
};

std::string ipToString(const std::uint8_t* p)
{
    return std::to_string(p[0]) + "." + std::to_string(p[1]) + "." + std::to_string(p[2]) + "."
        + std::to_string(p[3]);
}

ParsedPacket parseIpv4Packet(const std::uint8_t* data, size_t len)
{
    ParsedPacket out;
    if (len < 20) {
        return out;
    }
    const int version = (data[0] >> 4) & 0xF;
    if (version != 4) {
        return out;
    }
    const size_t ihl = static_cast<size_t>(data[0] & 0xF) * 4;
    if (ihl < 20 || len < ihl + 4) {
        return out;
    }
    const int protoByte = data[9];
    if (protoByte == 17) {
        out.proto = "UDP";
    } else if (protoByte == 6) {
        out.proto = "TCP";
    } else {
        return out;
    }
    out.srcIp = ipToString(data + 12);
    out.dstIp = ipToString(data + 16);
    out.srcPort = (static_cast<int>(data[ihl]) << 8) | data[ihl + 1];
    out.dstPort = (static_cast<int>(data[ihl + 2]) << 8) | data[ihl + 3];
    out.size = static_cast<int>(len);
    out.ok = out.srcPort > 0 || out.dstPort > 0;
    return out;
}

double perfMs()
{
    static const double freq = [] {
        LARGE_INTEGER f;
        QueryPerformanceFrequency(&f);
        return static_cast<double>(f.QuadPart);
    }();
    LARGE_INTEGER c;
    QueryPerformanceCounter(&c);
    return static_cast<double>(c.QuadPart) / freq * 1000.0;
}

// ── WinDivert sniff loop (port of _WinDivertLoop) ─────────────────────────
//
// The SECOND, INDEPENDENT WinDivert handle (hazard 8): a SNIFF NETWORK_FORWARD
// handle that OBSERVES traffic and broadcasts {"event":"packet"} for the
// client's passive court classifier. It is force-closed to trigger a filter
// restart when the console IP changes; that must NEVER disturb the intercept
// handle, which is owned by MeterDelayIntercept and shares no handle/lock here.
//
// [COURT_IP_STARVATION 2026-08-08] The sniff handle MUST open at a strictly
// HIGHER WinDivert priority than the intercept handle (which opens at 0).
// WinDivert delivers packets to higher-priority handles first; a non-SNIFF
// handle CAPTURES the packet (same-priority handles do not see it), and
// re-injected packets are delivered only to strictly LOWER priorities. With
// both handles at 0 (the pydivert default nexus_svc.py also shipped), opening
// the intercept starved this sniff handle of the very court->console flow whose
// observation keeps the classifier qualified: the client demoted to "Court IP
// unknown" ~1s later, stopped the intercept, requalified, restarted it — a
// relaxation oscillation measured at 472 intercept open/stop cycles in 6
// minutes on the owner rig. At priority 1000 the sniff observes the ORIGINAL
// packet before capture, and never sees the re-injected duplicate (injections
// from priority 0 travel only to priorities < 0), so nothing double-counts.
constexpr INT16 kSniffPriority = 1000;
class WinDivertSniffLoop {
public:
    WinDivertSniffLoop(WinDivertLibrary* lib, IpcServer* server) : lib_(lib), server_(server) {}

    void requestRestart()
    {
        restart_.store(true);
        std::lock_guard<std::mutex> lock(handleMutex_);
        if (handle_) {
            lib_->close(handle_); // force-close to break the blocking recv
            handle_ = nullptr;
        }
    }

    void start()
    {
        stop_.store(false);
        thread_ = std::thread(&WinDivertSniffLoop::run, this);
    }

    void stop()
    {
        stop_.store(true);
        {
            std::lock_guard<std::mutex> lock(handleMutex_);
            if (handle_) {
                lib_->close(handle_);
                handle_ = nullptr;
            }
        }
        if (thread_.joinable()) {
            thread_.join();
        }
    }

private:
    std::string buildFilter() const
    {
        const std::string console = server_->consoleIp();
        if (!console.empty()) {
            return "ip and (udp or tcp) and (ip.SrcAddr == " + console + " or ip.DstAddr == "
                + console + ")";
        }
        return "ip and (udp or tcp)";
    }

    void run()
    {
        if (!lib_ || !lib_->loaded()) {
            logLine("pydivert not available: WinDivert64.dll did not load");
            server_->broadcastError("pydivert not available");
            return;
        }
        std::vector<std::uint8_t> buf(0xFFFF);
        while (!stop_.load()) {
            restart_.store(false);
            const std::string filterStr = buildFilter();
            void* handle = lib_->open(filterStr.c_str(), WINDIVERT_LAYER_NETWORK_FORWARD,
                                      kSniffPriority, WINDIVERT_FLAG_SNIFF);
            if (handle == nullptr || handle == INVALID_HANDLE_VALUE) {
                if (!restart_.load() && !stop_.load()) {
                    server_->broadcastError("WinDivert open failed (GetLastError="
                                            + std::to_string(static_cast<unsigned>(::GetLastError()))
                                            + ")");
                    // Backoff, mirroring _WD_RETRY_DELAY (5s), polling the stop flag.
                    for (int i = 0; i < 50 && !stop_.load() && !restart_.load(); ++i) {
                        std::this_thread::sleep_for(std::chrono::milliseconds(100));
                    }
                }
                continue;
            }
            {
                std::lock_guard<std::mutex> lock(handleMutex_);
                handle_ = handle;
            }
            logLine("WinDivert (sniff) opened, filter: " + filterStr);
            server_->broadcastReady(filterStr);

            while (!stop_.load() && !restart_.load()) {
                UINT recvLen = 0;
                std::uint8_t addr[kWinDivertAddrSize] = {};
                if (!lib_->recv(handle, buf.data(), static_cast<UINT>(buf.size()), &recvLen, addr)) {
                    break;
                }
                dispatch(buf.data(), recvLen);
            }
            // Close ONLY if we won the race to claim the handle. stop() and requestRestart()
            // both close handle_ and null it under this same mutex; the guard below exists
            // precisely because they can, so closing unconditionally afterwards is a double
            // close. WinDivert handles are kernel handles and the value can already have been
            // reissued -- to the intercept's own handle, whose delay would then die silently --
            // and under strict handle checking a stale close raises EXCEPTION_INVALID_HANDLE
            // and takes the whole LocalSystem service down.
            bool owned = false;
            {
                std::lock_guard<std::mutex> lock(handleMutex_);
                if (handle_ == handle) {
                    handle_ = nullptr;
                    owned = true;
                }
            }
            if (owned) {
                lib_->close(handle);
            }
        }
    }

    void dispatch(const std::uint8_t* data, size_t len)
    {
        const ParsedPacket p = parseIpv4Packet(data, len);
        if (!p.ok) {
            return;
        }
        // At NETWORK_FORWARD WinDivert reports routed traffic as outbound; the
        // passive classifier derives direction from IP kinds, not this flag.
        server_->broadcastPacket(p.srcIp, p.dstIp, p.srcPort, p.dstPort, p.proto, p.size,
                                 /*outbound=*/true, perfMs());
        if (p.proto == "UDP") {
            const std::int64_t nowMs = static_cast<std::int64_t>(perfMs());
            detector_.observe(p.srcIp, p.dstIp, p.srcPort, p.dstPort,
                              CourtIpDetector::Transport::Udp, nowMs);
            const auto snap = detector_.snapshot(nowMs);
            traceCourtDecision(snap, nowMs);
            if (snap.qualified && !snap.endpointIp.empty()) {
                // Feed the intercept's court IP only if the client hasn't pinned
                // one (setDetectedCourtIp is a no-op once a client set wins).
                server_->setDetectedCourtIp(snap.endpointIp);
            }
        }
    }

    // [COURT_IP_TRACER 2026-08-08] Every accept/reject decision the detector
    // makes is visible in the service log: state EDGES (accept, demote, endpoint
    // change, candidate loss) log immediately; a standing REJECT re-logs every
    // 2 s and a standing ACCEPT heartbeats every 30 s so a stuck state is
    // diagnosable from the tail without flooding at packet rate.
    void traceCourtDecision(const CourtIpDetector::Snapshot& snap, std::int64_t nowMs)
    {
        const std::string endpoint = snap.hasCandidate
            ? snap.candidateIp + ":" + std::to_string(snap.candidatePort)
            : std::string("<none>");
        const bool edge = snap.qualified != traceQualified_ || endpoint != traceEndpoint_;
        const std::int64_t sinceLog = nowMs - traceLastLogMs_;
        // Standing state re-log cadence: REJECT-with-candidate every 2 s, ACCEPT
        // heartbeat every 30 s, standing no-candidate only on the edge (idle
        // sessions with zero court flow must not accrete log volume).
        const bool due = !snap.hasCandidate ? false
            : snap.qualified              ? (sinceLog >= 30000)
                                          : (sinceLog >= 2000);
        if (!edge && !due) {
            return;
        }
        traceQualified_ = snap.qualified;
        traceEndpoint_ = endpoint;
        traceLastLogMs_ = nowMs;
        if (!snap.hasCandidate) {
            logLine("CourtIP: candidate=<none> REJECT reason=no_candidate_flow");
            return;
        }
        char detail[160];
        std::snprintf(detail, sizeof(detail), " rate=%.1fpps dur=%.1fs in=%d out=%d window=%d",
                      snap.packetsPerSecond,
                      static_cast<double>(snap.observationSpanMs) / 1000.0, snap.recentInbound,
                      snap.recentOutbound, snap.recentPackets);
        if (snap.qualified) {
            logLine("CourtIP: candidate=" + endpoint + detail + " ACCEPT");
        } else {
            logLine("CourtIP: candidate=" + endpoint + detail + " REJECT reason="
                    + snap.rejectReason);
        }
    }

    WinDivertLibrary* lib_ = nullptr;
    IpcServer* server_ = nullptr;
    CourtIpDetector detector_;
    // Court tracer state — only touched on the sniff thread inside dispatch().
    bool traceQualified_ = false;
    std::string traceEndpoint_ = "<none>";
    std::int64_t traceLastLogMs_ = 0;
    std::mutex handleMutex_;
    void* handle_ = nullptr;
    std::atomic<bool> stop_{true};
    std::atomic<bool> restart_{false};
    std::thread thread_;
};

// ── The service body (port of _run_service) ───────────────────────────────

std::atomic<bool>* g_runStopFlag = nullptr; // set by SvcCtrlHandler
HANDLE g_stopEvent = nullptr;               // waited on by runService

void runService(const std::string& mode)
{
    logLine("TRACE runService: entered mode=" + mode);
    const std::vector<std::string> argv; // service mode reads arm from the environment
    std::string envArm;
    {
        char* buf = nullptr;
        size_t len = 0;
        if (_dupenv_s(&buf, &len, kMeterArmEnv) == 0 && buf) {
            envArm = buf;
        }
        if (buf) free(buf);
    }
    logLine("TRACE runService: envArm='" + envArm + "'");
    const auto arm = meterArmRequested(argv, envArm);
    const bool armed = arm.first;
    logLine(std::string("TRACE runService: armed=") + (armed ? "1" : "0") + " src=" + arm.second);

    logLine(std::string(mode == "service" ? "NexusVisionSvc" : "debug") + " starting"
            + (armed ? " (meter delay ARMED via " + arm.second + ")" : " (meter delay DISARMED)"));

    logLine("TRACE runService: constructing WinDivertLibrary");
    WinDivertLibrary lib;
    logLine("TRACE runService: calling lib.load");
    const bool pydivertOk = lib.load(exeDirW());
    logLine(std::string("TRACE runService: lib.load returned ok=") + (pydivertOk ? "1" : "0"));
    if (!pydivertOk) {
        logLine("WinDivert not loadable: " + lib.lastError());
    }

    logLine("TRACE runService: constructing ServiceStateMachine");
    ServiceStateMachine stateMachine;
    // Initial driver probe (docs driver_state machine). windivertDriverRunning()
    // returns 1=running, 0=not running/not installed, -1=unknown; a demand-start
    // driver starts on the intercept's first open, so "not running now" is
    // reported as NOT_INSTALLED until the open moves it forward.
    const int running = windivertDriverRunning();
    stateMachine.onProbe(/*installed=*/running == 1, /*running=*/running == 1);

    logLine("TRACE runService: constructing MeterDelayIntercept");
    MeterDelayIntercept intercept(MeterDelayIntercept::kMaxSlewMsPerS);
    logLine("TRACE runService: setWinDivert");
    intercept.setWinDivert(&lib);
    logLine("TRACE runService: setLogger");
    intercept.setLogger(logLine);

    logLine("TRACE runService: constructing IpcServer");
    IpcServer server;
    logLine("TRACE runService: server.setLogger");
    server.setLogger(logLine);
    logLine("TRACE runService: server.configure");
    server.configure(armed ? &intercept : nullptr, &stateMachine, armed, pydivertOk);
    logLine("TRACE runService: intercept.setStateBroadcaster");
    intercept.setStateBroadcaster(
        [&server](const std::string& state, const std::string& reason) {
            server.broadcastMeterDelay(state, reason);
        });

    logLine("TRACE runService: publishToken");
    if (!server.publishToken(mode)) {
        logLine("Token publish failed — auth is fail-closed for this session");
    }

    logLine("TRACE runService: constructing WinDivertSniffLoop");
    WinDivertSniffLoop sniff(&lib, &server);
    logLine("TRACE runService: setFilterChangedCallback");
    server.setFilterChangedCallback([&sniff] { sniff.requestRestart(); });

    if (armed) {
        logLine("INBOUND METER DELAY ARMED. Slew capped at 100 ms/s.");
    } else {
        logLine("Inbound meter delay DISARMED (default). Verbs will answer meter_delay_disarmed.");
    }

    logLine("TRACE runService: sniff.start");
    sniff.start();
    logLine("TRACE runService: server.startListening");
    if (!server.startListening()) {
        logLine("Cannot bind 127.0.0.1:47291 — shutting down");
    } else {
        logLine("TRACE runService: waiting on g_stopEvent");
        // Block until SvcStop / Ctrl-C signals the stop event.
        if (g_stopEvent) {
            ::WaitForSingleObject(g_stopEvent, INFINITE);
        }
    }

    logLine("Stop signal received, shutting down");
    // Flush and release the console before anything else during shutdown.
    if (armed) {
        intercept.stop("service_shutdown");
    }
    sniff.stop();
    server.stop();
    // Only now, with every handle closed, can the driver actually be unloaded.
    unloadWinDivertDriver("service_shutdown");
    logLine("stopped");
}

// ── SCM plumbing ──────────────────────────────────────────────────────────

SERVICE_STATUS g_svcStatus{};
SERVICE_STATUS_HANDLE g_svcStatusHandle = nullptr;
std::atomic<bool> g_svcStop{false};

void reportStatus(DWORD state, DWORD exitCode, DWORD waitHintMs)
{
    g_svcStatus.dwCurrentState = state;
    g_svcStatus.dwWin32ExitCode = exitCode;
    g_svcStatus.dwWaitHint = waitHintMs;
    g_svcStatus.dwControlsAccepted =
        (state == SERVICE_START_PENDING) ? 0 : SERVICE_ACCEPT_STOP | SERVICE_ACCEPT_SHUTDOWN;
    if (state == SERVICE_RUNNING || state == SERVICE_STOPPED) {
        g_svcStatus.dwCheckPoint = 0;
    } else {
        g_svcStatus.dwCheckPoint++;
    }
    if (g_svcStatusHandle) {
        ::SetServiceStatus(g_svcStatusHandle, &g_svcStatus);
    }
}

void WINAPI svcCtrlHandler(DWORD ctrl)
{
    switch (ctrl) {
    case SERVICE_CONTROL_STOP:
    case SERVICE_CONTROL_SHUTDOWN:
        reportStatus(SERVICE_STOP_PENDING, NO_ERROR, 5000);
        g_svcStop.store(true);
        if (g_stopEvent) {
            ::SetEvent(g_stopEvent);
        }
        break;
    default:
        break;
    }
}

void WINAPI svcMain(DWORD /*argc*/, LPWSTR* /*argv*/)
{
    g_svcStatusHandle = ::RegisterServiceCtrlHandlerW(kSvcName, svcCtrlHandler);
    if (!g_svcStatusHandle) {
        return;
    }
    g_svcStatus.dwServiceType = SERVICE_WIN32_OWN_PROCESS;
    reportStatus(SERVICE_START_PENDING, NO_ERROR, 5000);
    g_stopEvent = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
    reportStatus(SERVICE_RUNNING, NO_ERROR, 0);

    runService("service");

    if (g_stopEvent) {
        ::CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
    }
    reportStatus(SERVICE_STOPPED, NO_ERROR, 0);
}

// ── install / remove / start / stop ───────────────────────────────────────

std::wstring quotedImagePath(bool armed)
{
    wchar_t buf[MAX_PATH];
    ::GetModuleFileNameW(nullptr, buf, MAX_PATH);
    std::wstring path = std::wstring(L"\"") + buf + L"\"";
    if (armed) {
        // Bake the arm flag into binPath (mirrors nexus_svc's service-mode arm
        // path via the environment/binPath). Service mode reads it from binPath's
        // args, which the SCM passes to svcMain's argv (unused here) — arming in
        // service mode is via the environment, so this flag is informational.
        path += L" --arm-meter-delay";
    }
    return path;
}

int installService(bool armed)
{
    SC_HANDLE scm = ::OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CREATE_SERVICE);
    if (!scm) {
        std::fprintf(stderr, "[venicenet_svc] OpenSCManager failed (%lu)\n", ::GetLastError());
        return 1;
    }
    const std::wstring image = quotedImagePath(armed);
    SC_HANDLE svc = ::CreateServiceW(scm, kSvcName, kSvcDisplayName, SERVICE_ALL_ACCESS,
                                     SERVICE_WIN32_OWN_PROCESS, SERVICE_DEMAND_START,
                                     SERVICE_ERROR_NORMAL, image.c_str(), nullptr, nullptr, nullptr,
                                     nullptr, nullptr);
    int rc = 0;
    if (!svc) {
        std::fprintf(stderr, "[venicenet_svc] CreateService failed (%lu)\n", ::GetLastError());
        rc = 1;
    } else {
        SERVICE_DESCRIPTIONW desc{};
        desc.lpDescription = const_cast<wchar_t*>(kSvcDescription);
        ::ChangeServiceConfig2W(svc, SERVICE_CONFIG_DESCRIPTION, &desc);
        ::CloseServiceHandle(svc);
        std::printf("[venicenet_svc] installed as %ws\n", kSvcName);
    }
    ::CloseServiceHandle(scm);
    return rc;
}

int controlService(DWORD action)
{
    SC_HANDLE scm = ::OpenSCManagerW(nullptr, nullptr, SC_MANAGER_CONNECT);
    if (!scm) {
        return 1;
    }
    SC_HANDLE svc = ::OpenServiceW(scm, kSvcName,
                                   SERVICE_START | SERVICE_STOP | DELETE | SERVICE_QUERY_STATUS);
    int rc = 1;
    if (svc) {
        if (action == 0) { // start
            rc = ::StartServiceW(svc, 0, nullptr) ? 0 : 1;
        } else if (action == 1) { // stop
            SERVICE_STATUS st{};
            rc = ::ControlService(svc, SERVICE_CONTROL_STOP, &st) ? 0 : 1;
        } else if (action == 2) { // remove
            rc = ::DeleteService(svc) ? 0 : 1;
        }
        ::CloseServiceHandle(svc);
    }
    ::CloseServiceHandle(scm);
    return rc;
}

void runDebug()
{
    logLine("TRACE runDebug: entered");
    std::printf("[venicenet_svc] Running in debug mode on 127.0.0.1:47291\n");
    std::fflush(stdout);
    std::printf("[venicenet_svc] Press Ctrl+C to stop.\n");
    std::fflush(stdout);
    g_stopEvent = ::CreateEventW(nullptr, TRUE, FALSE, nullptr);
    logLine("TRACE runDebug: g_stopEvent created, calling runService");
    runService("debug");
    logLine("TRACE runDebug: runService returned");
    if (g_stopEvent) {
        ::CloseHandle(g_stopEvent);
        g_stopEvent = nullptr;
    }
}

BOOL WINAPI consoleCtrlHandler(DWORD type)
{
    if (type == CTRL_C_EVENT || type == CTRL_CLOSE_EVENT || type == CTRL_BREAK_EVENT) {
        if (g_stopEvent) {
            ::SetEvent(g_stopEvent);
        }
        return TRUE;
    }
    return FALSE;
}

} // namespace

int wmain(int argc, wchar_t** argv)
{
    std::vector<std::string> args;
    for (int i = 1; i < argc; ++i) {
        const int n =
            ::WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, nullptr, 0, nullptr, nullptr);
        std::string s(static_cast<size_t>(n > 0 ? n - 1 : 0), '\0');
        if (n > 0) {
            ::WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, s.data(), n, nullptr, nullptr);
        }
        args.push_back(s);
    }

    // A bare invocation (arm flag aside) means the SCM launched us: run the
    // control dispatcher, not the command parser (mirrors nexus_svc's
    // _is_service_run_invocation).
    if (isServiceRunInvocation(args)) {
        SERVICE_TABLE_ENTRYW table[] = {
            {const_cast<wchar_t*>(kSvcName), svcMain},
            {nullptr, nullptr},
        };
        if (!::StartServiceCtrlDispatcherW(table)) {
            // Not launched by the SCM (e.g. run bare from a console): fall back to
            // debug mode so a developer bare-launch still works.
            ::SetConsoleCtrlHandler(consoleCtrlHandler, TRUE);
            runDebug();
        }
        return 0;
    }

    const std::string cmd = args.empty() ? "" : args.front();
    const auto arm = meterArmRequested(args, "");

    if (cmd == "driver-status") {
        const int state = windivertDriverRunning();
        std::printf("[venicenet_svc] WinDivert driver running: %d\n", state);
        return state == 0 ? 0 : 1;
    }
    if (cmd == "unload-driver") {
        const bool ok = unloadWinDivertDriver("operator_request");
        std::printf("[venicenet_svc] WinDivert driver unloaded: %s\n", ok ? "true" : "false");
        return ok ? 0 : 1;
    }
    if (cmd == "debug") {
        ::SetConsoleCtrlHandler(consoleCtrlHandler, TRUE);
        runDebug();
        return 0;
    }
    if (cmd == "install") {
        return installService(arm.first);
    }
    if (cmd == "start") {
        return controlService(0);
    }
    if (cmd == "stop") {
        return controlService(1);
    }
    if (cmd == "remove") {
        return controlService(2);
    }

    std::printf("Usage: VeniceNetSvc install|start|stop|remove|debug|driver-status|unload-driver\n");
    std::printf("       add --arm-meter-delay to arm the inbound meter delay (default: disarmed)\n");
    return 1;
}
