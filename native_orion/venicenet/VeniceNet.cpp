// ───────────────────────────────────────────────────────────────────────────
//  VeniceNet.cpp — wave-2B IPC-client implementation of the VeniceNet C ABI
// ───────────────────────────────────────────────────────────────────────────
//
//  The DLL is a THIN IPC CLIENT. The privileged WinDivert engine runs in a
//  separate SYSTEM service (VeniceNetSvc.exe / NexusVisionSvc.exe). This file
//  maintains a loopback-TCP connection to that service, authenticates with the
//  per-session bearer token, translates the ratified ABI verbs to/from the
//  EXISTING nexus_svc wire protocol (newline-delimited JSON), and derives the
//  VeniceNetSnapshot / callback feed from the service's broadcasts + the pipe
//  connection state.
//
//  Transport note: the "pipe" in the wave-1 docs and the task brief is nexus_svc
//  .py's loopback socket (127.0.0.1:47291), NOT a Windows named pipe. The wave-2A
//  service ports that same wire protocol as-is, so both ends interoperate over
//  this loopback channel. See venicenet_service_route.{h,cpp}.
//
//  Behavioural contract: docs/VENICENET_API.md.
// ───────────────────────────────────────────────────────────────────────────

#include "VeniceNet_internal.h"
#include "venicenet_service_route.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>

#include <cctype>
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#pragma comment(lib, "Ws2_32.lib")

// The snapshot layout is the ABI.  Pin every offset so an accidental field
// reorder or type change fails the build instead of shipping a torn struct.
static_assert(offsetof(VeniceNetSnapshot, struct_size) == 0);
static_assert(offsetof(VeniceNetSnapshot, driver_state) == 4);
static_assert(offsetof(VeniceNetSnapshot, applied_delay_ms) == 8);
static_assert(offsetof(VeniceNetSnapshot, target_delay_ms) == 16);
static_assert(offsetof(VeniceNetSnapshot, latency_p50_ms) == 24);
static_assert(offsetof(VeniceNetSnapshot, latency_p95_ms) == 32);
static_assert(offsetof(VeniceNetSnapshot, buffer_depth) == 40);
static_assert(offsetof(VeniceNetSnapshot, error_code) == 44);
static_assert(offsetof(VeniceNetSnapshot, armed) == 48);
static_assert(offsetof(VeniceNetSnapshot, enabled) == 52);
static_assert(offsetof(VeniceNetSnapshot, engage_policy) == 56);
static_assert(offsetof(VeniceNetSnapshot, court_ip_detected) == 60);
static_assert(offsetof(VeniceNetSnapshot, offense) == 64);
static_assert(offsetof(VeniceNetSnapshot, live_ball) == 68);
static_assert(offsetof(VeniceNetSnapshot, court_ip) == 72);
static_assert(offsetof(VeniceNetSnapshot, error_text) == 88);
static_assert(sizeof(VeniceNetSnapshot) == 344,
              "VeniceNetSnapshot layout changed — this is an ABI break; bump "
              "VENICENET_ABI_VERSION and update docs/VENICENET_API.md");

namespace venicenet {
namespace {

// Strict dotted-quad IPv4 parse: exactly four decimal octets 0-255, no
// leading '+'/'-', no leading zeros beyond a bare "0", nothing trailing.
bool isDottedQuadIpv4(const char* text)
{
    if (text == nullptr) {
        return false;
    }
    int octets = 0;
    const char* p = text;
    while (true) {
        if (*p < '0' || *p > '9') {
            return false;
        }
        int value = 0;
        int digits = 0;
        const bool leadingZero = (*p == '0');
        while (*p >= '0' && *p <= '9') {
            value = value * 10 + (*p - '0');
            ++digits;
            ++p;
            if (digits > 3 || value > 255) {
                return false;
            }
        }
        if (leadingZero && digits > 1) {
            return false;
        }
        ++octets;
        if (*p == '.') {
            if (octets == 4) {
                return false;
            }
            ++p;
            continue;
        }
        break;
    }
    return octets == 4 && *p == '\0';
}

// ── minimal flat-JSON field readers for the service's newline-JSON lines ──
bool jsonString(const std::string& obj, const char* key, std::string* out)
{
    const std::string needle = std::string("\"") + key + "\"";
    size_t k = obj.find(needle);
    if (k == std::string::npos) return false;
    size_t p = obj.find(':', k + needle.size());
    if (p == std::string::npos) return false;
    ++p;
    while (p < obj.size() && std::isspace(static_cast<unsigned char>(obj[p]))) ++p;
    if (p >= obj.size() || obj[p] != '"') return false;
    ++p;
    std::string value;
    while (p < obj.size() && obj[p] != '"') {
        if (obj[p] == '\\' && p + 1 < obj.size()) {
            ++p;
            switch (obj[p]) {
            case 'n': value.push_back('\n'); break;
            case 't': value.push_back('\t'); break;
            case 'r': value.push_back('\r'); break;
            default:  value.push_back(obj[p]); break;
            }
            ++p;
            continue;
        }
        value.push_back(obj[p]);
        ++p;
    }
    *out = value;
    return true;
}

// Reads a bool ONLY when the value is a literal true/false (not a quoted
// string), so a malformed {"meter_delay_active":"yes"} is rejected upstream.
bool jsonBool(const std::string& obj, const char* key, bool* out)
{
    const std::string needle = std::string("\"") + key + "\"";
    size_t k = obj.find(needle);
    if (k == std::string::npos) return false;
    size_t p = obj.find(':', k + needle.size());
    if (p == std::string::npos) return false;
    ++p;
    while (p < obj.size() && std::isspace(static_cast<unsigned char>(obj[p]))) ++p;
    if (obj.compare(p, 4, "true") == 0) { *out = true; return true; }
    if (obj.compare(p, 5, "false") == 0) { *out = false; return true; }
    return false;
}

// Reads a numeric value ONLY when it is a bare JSON number (not quoted).
bool jsonNumber(const std::string& obj, const char* key, double* out)
{
    const std::string needle = std::string("\"") + key + "\"";
    size_t k = obj.find(needle);
    if (k == std::string::npos) return false;
    size_t p = obj.find(':', k + needle.size());
    if (p == std::string::npos) return false;
    ++p;
    while (p < obj.size() && std::isspace(static_cast<unsigned char>(obj[p]))) ++p;
    const size_t start = p;
    if (p < obj.size() && (obj[p] == '-' || obj[p] == '+')) ++p;
    bool sawDigit = false;
    while (p < obj.size() &&
           (std::isdigit(static_cast<unsigned char>(obj[p])) || obj[p] == '.'
            || obj[p] == 'e' || obj[p] == 'E' || obj[p] == '-' || obj[p] == '+')) {
        if (std::isdigit(static_cast<unsigned char>(obj[p]))) sawDigit = true;
        ++p;
    }
    if (!sawDigit) return false;
    *out = std::strtod(obj.substr(start, p - start).c_str(), nullptr);
    return true;
}

// True iff a "features" array in `obj` contains the string "meter_delay".
bool featuresAdvertiseMeterDelay(const std::string& obj, bool* hasFeaturesArray)
{
    *hasFeaturesArray = false;
    const std::string needle = "\"features\"";
    size_t k = obj.find(needle);
    if (k == std::string::npos) return false;
    size_t open = obj.find('[', k + needle.size());
    if (open == std::string::npos) return false;
    size_t close = obj.find(']', open);
    if (close == std::string::npos) return false;
    *hasFeaturesArray = true;
    const size_t hit = obj.find("\"meter_delay\"", open);
    return hit != std::string::npos && hit < close;
}

std::string jsonEscape(const std::string& in)
{
    std::string out;
    out.reserve(in.size() + 2);
    for (const char c : in) {
        switch (c) {
        case '"':  out += "\\\""; break;
        case '\\': out += "\\\\"; break;
        case '\n': out += "\\n"; break;
        case '\r': out += "\\r"; break;
        case '\t': out += "\\t"; break;
        default:   out.push_back(c); break;
        }
    }
    return out;
}

std::string formatDelay(double ms)
{
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.3f", ms);
    return std::string(buf);
}

// Bounded, always-NUL-terminated copy into a fixed C-string field. Avoids
// strncpy (banned by /sdl as C4996) while keeping the ABI's fixed-buffer layout.
void copyBounded(char* dst, size_t cap, const std::string& src)
{
    if (cap == 0) {
        return;
    }
    const size_t n = src.size() < (cap - 1) ? src.size() : (cap - 1);
    if (n > 0) {
        std::memcpy(dst, src.data(), n);
    }
    dst[n] = '\0';
}

constexpr uintptr_t kInvalidSocket = ~static_cast<uintptr_t>(0);

} // namespace

Core& Core::instance()
{
    static Core core;
    return core;
}

Core::~Core()
{
    // By ABI contract FreeLibrary happens only after venicenet_shutdown(), so
    // this is normally a no-op. Kept as a backstop so a static-teardown never
    // leaves the I/O thread running against freed state.
    shutdown();
}

// ───────────────────────────────────────────────────────────────────────────
//  Lifecycle
// ───────────────────────────────────────────────────────────────────────────

VeniceNetStatus Core::init()
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (initialized_) {
            return VENICENET_ERR_ALREADY_INITIALIZED;
        }
        // Always come up at defaults: a stale target from a previous init must
        // never be applied implicitly.
        enabled_ = false;
        targetDelayMs_ = 0.0;
        policy_ = VENICENET_POLICY_ALWAYS_ON;
        offense_ = false;
        liveBall_ = true;
        consoleIp_.clear();
        driverState_ = VENICENET_DRIVER_NOT_INSTALLED;
        appliedDelayMs_ = 0.0;
        latencyP50Ms_ = -1.0;
        latencyP95Ms_ = -1.0;
        bufferDepth_ = 0;
        armed_ = false;
        courtIpDetected_ = false;
        courtIp_.clear();
        errorCode_ = VENICENET_OK;
        errorText_.clear();
        callback_ = nullptr;
        callbackUserData_ = nullptr;
        haveLastNotified_ = false;
        initialized_ = true;
    }

    if (!wsaReady_) {
        WSADATA wsa;
        if (WSAStartup(MAKEWORD(2, 2), &wsa) == 0) {
            wsaReady_ = true;
        }
        // A WSAStartup failure is non-fatal: init still succeeds and the driver
        // state reports NOT_INSTALLED (the connection simply never comes up),
        // exactly the honest "not available" surface the contract mandates.
    }

    ioRunning_.store(true);
    connected_.store(false);
    ioThread_ = std::thread([this]() { ioThreadMain(); });
    return VENICENET_OK;
}

void Core::shutdown()
{
    // Idempotent: safe when never initialised and safe to call repeatedly.
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return;
        }
    }

    // Stop the I/O thread first, WITHOUT holding mutex_ (the thread takes it).
    ioRunning_.store(false);
    {
        std::lock_guard<std::mutex> sendLock(sendMutex_);
        if (socket_ != kInvalidSocket) {
            // Unblocks a recv()/connect() in progress on the I/O thread.
            ::shutdown(static_cast<SOCKET>(socket_), SD_BOTH);
            ::closesocket(static_cast<SOCKET>(socket_));
            socket_ = kInvalidSocket;
        }
    }
    if (ioThread_.joinable()) {
        ioThread_.join();
    }
    connected_.store(false);

    if (wsaReady_) {
        WSACleanup();
        wsaReady_ = false;
    }

    // Reset state to defaults and forget the callback. No callback can fire
    // after this returns because the I/O thread is joined and callback_ is null.
    std::lock_guard<std::mutex> lock(mutex_);
    initialized_ = false;
    enabled_ = false;
    targetDelayMs_ = 0.0;
    policy_ = VENICENET_POLICY_ALWAYS_ON;
    offense_ = false;
    liveBall_ = true;
    consoleIp_.clear();
    driverState_ = VENICENET_DRIVER_NOT_INSTALLED;
    appliedDelayMs_ = 0.0;
    latencyP50Ms_ = -1.0;
    latencyP95Ms_ = -1.0;
    bufferDepth_ = 0;
    armed_ = false;
    courtIpDetected_ = false;
    courtIp_.clear();
    errorCode_ = VENICENET_OK;
    errorText_.clear();
    callback_ = nullptr;
    callbackUserData_ = nullptr;
    haveLastNotified_ = false;
}

// ───────────────────────────────────────────────────────────────────────────
//  Configuration setters (thread-safe from any thread after init)
// ───────────────────────────────────────────────────────────────────────────

VeniceNetStatus Core::setEnabled(bool enabled)
{
    bool transitioned = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        if (enabled_ != enabled) {
            enabled_ = enabled;
            transitioned = true;
        }
    }
    if (transitioned) {
        wireSetEnabled(enabled);
    }
    notifyStateChangedIfChanged();
    return VENICENET_OK;
}

VeniceNetStatus Core::setTargetDelayMs(double targetMs)
{
    if (!std::isfinite(targetMs)) {
        return VENICENET_ERR_INVALID_ARGUMENT;
    }
    const double clamped =
        targetMs < 0.0 ? 0.0
                       : (targetMs > VENICENET_MAX_DELAY_MS ? VENICENET_MAX_DELAY_MS
                                                            : targetMs);
    bool enabledNow = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        targetDelayMs_ = clamped;
        enabledNow = enabled_;
    }
    // Sent on EVERY call (not only on change): the service runs a starvation
    // watchdog that forces the delay to 0 if set_meter_delay stops arriving, so
    // an unchanged value is still a required keepalive re-assertion.
    if (enabledNow) {
        wireSetTargetDelay(clamped);
    }
    notifyStateChangedIfChanged();
    return VENICENET_OK;
}

VeniceNetStatus Core::setEngagePolicy(VeniceNetEngagePolicy policy)
{
    switch (policy) {
    case VENICENET_POLICY_ALWAYS_ON:
    case VENICENET_POLICY_DEAD_BALL:
    case VENICENET_POLICY_OFFENSE_DEFENSE:
    case VENICENET_POLICY_SHOT_GATED:
        break;
    default:
        return VENICENET_ERR_INVALID_ARGUMENT;
    }
    bool transitioned = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        if (policy_ != policy) {
            policy_ = policy;
            transitioned = true;
        }
    }
    if (transitioned) {
        wireSetEngagePolicy(policy);
    }
    notifyStateChangedIfChanged();
    return VENICENET_OK;
}

VeniceNetStatus Core::setOffense(bool offense)
{
    bool transitioned = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        if (offense_ != offense) {
            offense_ = offense;
            transitioned = true;
        }
    }
    if (transitioned) {
        wireSetOffense(offense);
    }
    notifyStateChangedIfChanged();
    return VENICENET_OK;
}

VeniceNetStatus Core::setLive(bool liveBall)
{
    bool transitioned = false;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        if (liveBall_ != liveBall) {
            liveBall_ = liveBall;
            transitioned = true;
        }
    }
    if (transitioned) {
        wireSetLive(liveBall);
    }
    notifyStateChangedIfChanged();
    return VENICENET_OK;
}

VeniceNetStatus Core::setConsoleIp(const char* consoleIpv4)
{
    const bool clearing = (consoleIpv4 == nullptr || consoleIpv4[0] == '\0');
    if (!clearing && !isDottedQuadIpv4(consoleIpv4)) {
        return VENICENET_ERR_INVALID_ARGUMENT;
    }
    bool transitioned = false;
    std::string next;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        next = clearing ? std::string() : std::string(consoleIpv4);
        if (consoleIp_ != next) {
            consoleIp_ = next;
            transitioned = true;
        }
    }
    if (transitioned) {
        wireSetConsoleIp(next);
    }
    notifyStateChangedIfChanged();
    return VENICENET_OK;
}

VeniceNetStatus Core::notifyShotEdge()
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
    }
    wireNotifyShotEdge();
    return VENICENET_OK;
}

// ───────────────────────────────────────────────────────────────────────────
//  Snapshot + callback
// ───────────────────────────────────────────────────────────────────────────

void Core::fillSnapshotLocked(VeniceNetSnapshot& out) const
{
    std::memset(&out, 0, sizeof(out));
    out.struct_size = sizeof(VeniceNetSnapshot);
    // ── Live facts from the service + pipe state ──
    out.driver_state = driverState_;
    out.applied_delay_ms = appliedDelayMs_;
    out.latency_p50_ms = latencyP50Ms_;
    out.latency_p95_ms = latencyP95Ms_;
    out.buffer_depth = bufferDepth_;
    out.error_code = errorCode_;
    out.armed = armed_ ? 1 : 0;
    out.court_ip_detected = courtIpDetected_ ? 1 : 0;
    // ── Configuration mirror ──
    out.target_delay_ms = targetDelayMs_;
    out.enabled = enabled_ ? 1 : 0;
    out.engage_policy = static_cast<int32_t>(policy_);
    out.offense = offense_ ? 1 : 0;
    out.live_ball = liveBall_ ? 1 : 0;
    // ── Strings (bounded copies, always NUL-terminated) ──
    if (!courtIp_.empty()) {
        copyBounded(out.court_ip, VENICENET_IP_TEXT_CAP, courtIp_);
    }
    if (!errorText_.empty()) {
        copyBounded(out.error_text, VENICENET_ERROR_TEXT_CAP, errorText_);
    }
}

void Core::notifyStateChangedIfChanged()
{
    VeniceNetSnapshot copy;
    VeniceNetStateCallback callback = nullptr;
    void* userData = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_ || callback_ == nullptr) {
            return;
        }
        fillSnapshotLocked(copy);
        if (haveLastNotified_ && std::memcmp(&copy, &lastNotified_, sizeof(copy)) == 0) {
            return; // no observable change — do not wake the receiver
        }
        lastNotified_ = copy;
        haveLastNotified_ = true;
        callback = callback_;
        userData = callbackUserData_;
    }
    callback(&copy, userData);
}

void Core::primeCallback()
{
    VeniceNetSnapshot copy;
    VeniceNetStateCallback callback = nullptr;
    void* userData = nullptr;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_ || callback_ == nullptr) {
            return;
        }
        fillSnapshotLocked(copy);
        lastNotified_ = copy;
        haveLastNotified_ = true;
        callback = callback_;
        userData = callbackUserData_;
    }
    callback(&copy, userData);
}

VeniceNetStatus Core::snapshot(VeniceNetSnapshot* out) const
{
    if (out == nullptr) {
        return VENICENET_ERR_INVALID_ARGUMENT;
    }
    if (out->struct_size != sizeof(VeniceNetSnapshot)) {
        return VENICENET_ERR_ABI_MISMATCH;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    if (!initialized_) {
        return VENICENET_ERR_NOT_INITIALIZED;
    }
    fillSnapshotLocked(*out);
    return VENICENET_OK;
}

VeniceNetStatus Core::registerStateCallback(VeniceNetStateCallback callback,
                                            void* userData)
{
    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (!initialized_) {
            return VENICENET_ERR_NOT_INITIALIZED;
        }
        callback_ = callback;
        callbackUserData_ = callback == nullptr ? nullptr : userData;
    }
    // Prime the receiver immediately + synchronously so it never starts blind
    // (contract). NULL unregisters and primes nothing.
    if (callback != nullptr) {
        primeCallback();
    }
    return VENICENET_OK;
}

// ───────────────────────────────────────────────────────────────────────────
//  Transport — I/O thread
// ───────────────────────────────────────────────────────────────────────────

bool Core::sendRaw(const std::string& line)
{
    std::lock_guard<std::mutex> lock(sendMutex_);
    if (socket_ == kInvalidSocket) {
        return false;
    }
    const SOCKET sock = static_cast<SOCKET>(socket_);
    size_t sent = 0;
    while (sent < line.size()) {
        const int chunk = static_cast<int>(
            (line.size() - sent) > 0x40000000u ? 0x40000000 : (line.size() - sent));
        const int n = ::send(sock, line.data() + sent, chunk, 0);
        if (n == SOCKET_ERROR || n <= 0) {
            return false;
        }
        sent += static_cast<size_t>(n);
    }
    return true;
}

void Core::wireSetConsoleIp(const std::string& ip)
{
    if (!connected_.load()) return;
    if (ip.empty()) {
        sendRaw("{\"cmd\":\"clear_filter\"}\n");
    } else {
        sendRaw("{\"cmd\":\"set_filter\",\"console_ip\":\"" + jsonEscape(ip) + "\"}\n");
    }
}

void Core::wireSetEnabled(bool enabled)
{
    if (!connected_.load()) return;
    if (enabled) {
        sendRaw("{\"cmd\":\"start_meter_intercept\"}\n");
        // Push the standing target immediately so the intercept opens at the
        // intended delay rather than 0 until the next keepalive.
        double target = 0.0;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            target = targetDelayMs_;
        }
        sendRaw("{\"cmd\":\"set_meter_delay\",\"delay_ms\":" + formatDelay(target) + "}\n");
    } else {
        sendRaw("{\"cmd\":\"stop_meter_intercept\"}\n");
    }
}

void Core::wireSetTargetDelay(double ms)
{
    if (!connected_.load()) return;
    sendRaw("{\"cmd\":\"set_meter_delay\",\"delay_ms\":" + formatDelay(ms) + "}\n");
}

void Core::wireSetEngagePolicy(VeniceNetEngagePolicy policy)
{
    if (!connected_.load()) return;
    // NEW verb (not in nexus_svc.py today): the wave-2A service implements it;
    // an older service answers unknown_command, which the I/O thread treats as
    // benign. See the report's "invented wire verbs" note.
    sendRaw("{\"cmd\":\"set_engage_policy\",\"policy\":" +
            std::to_string(static_cast<int>(policy)) + "}\n");
}

void Core::wireSetOffense(bool offense)
{
    if (!connected_.load()) return;
    sendRaw(std::string("{\"cmd\":\"set_offense\",\"offense\":") +
            (offense ? "true" : "false") + "}\n");
}

void Core::wireSetLive(bool liveBall)
{
    if (!connected_.load()) return;
    sendRaw(std::string("{\"cmd\":\"set_live\",\"live\":") +
            (liveBall ? "true" : "false") + "}\n");
}

void Core::wireNotifyShotEdge()
{
    if (!connected_.load()) return;
    sendRaw("{\"cmd\":\"notify_shot_edge\"}\n");
}

void Core::replayConfigOnConnect()
{
    // Snapshot the intent under the lock, then emit outside it.
    bool enabled = false;
    double target = 0.0;
    VeniceNetEngagePolicy policy = VENICENET_POLICY_ALWAYS_ON;
    bool offense = false;
    bool live = true;
    std::string consoleIp;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        enabled = enabled_;
        target = targetDelayMs_;
        policy = policy_;
        offense = offense_;
        live = liveBall_;
        consoleIp = consoleIp_;
    }
    wireSetConsoleIp(consoleIp);
    wireSetEngagePolicy(policy);
    wireSetOffense(offense);
    wireSetLive(live);
    if (enabled) {
        // start_meter_intercept + the standing target.
        if (connected_.load()) {
            sendRaw("{\"cmd\":\"start_meter_intercept\"}\n");
            sendRaw("{\"cmd\":\"set_meter_delay\",\"delay_ms\":" + formatDelay(target) + "}\n");
        }
    }
}

void Core::markDisconnected(int32_t driverState, int32_t errorCode,
                            const std::string& errorText)
{
    connected_.store(false);
    {
        std::lock_guard<std::mutex> sendLock(sendMutex_);
        if (socket_ != kInvalidSocket) {
            ::closesocket(static_cast<SOCKET>(socket_));
            socket_ = kInvalidSocket;
        }
    }
    {
        std::lock_guard<std::mutex> lock(mutex_);
        driverState_ = driverState;
        errorCode_ = errorCode;
        errorText_ = errorText;
        appliedDelayMs_ = 0.0;
        bufferDepth_ = 0;
        armed_ = false;
        courtIpDetected_ = false;
        courtIp_.clear();
    }
    notifyStateChangedIfChanged();
}

void Core::handleServiceLine(const std::string& line)
{
    std::string event;
    if (!jsonString(line, "event", &event)) {
        return;
    }

    if (event == "hello") {
        bool hasFeatures = false;
        const bool advertises = featuresAdvertiseMeterDelay(line, &hasFeatures);
        // A features array that omits "meter_delay" means the service shipped its
        // intercept DISARMED (arming is an out-of-band operator action). Surface
        // that as NOT_STARTED once authenticated. A missing features array is an
        // older service that cannot report arming — leave the state alone.
        if (hasFeatures && !advertises) {
            std::lock_guard<std::mutex> lock(mutex_);
            if (driverState_ != VENICENET_DRIVER_ERROR) {
                driverState_ = VENICENET_DRIVER_NOT_STARTED;
            }
        }
        notifyStateChangedIfChanged();
        return;
    }

    std::string cmd;
    const bool haveCmd = jsonString(line, "cmd", &cmd);

    if (event == "ack" && haveCmd && cmd == "auth") {
        connected_.store(true);
        {
            std::lock_guard<std::mutex> lock(mutex_);
            errorCode_ = VENICENET_OK;
            errorText_.clear();
            if (driverState_ == VENICENET_DRIVER_NOT_INSTALLED) {
                driverState_ = VENICENET_DRIVER_READY;
            }
        }
        replayConfigOnConnect();
        notifyStateChangedIfChanged();
        return;
    }

    if (event == "error") {
        std::string msg;
        jsonString(line, "msg", &msg);
        if (msg == "unauthorized") {
            // Auth failed: this connection cannot drive the service. Report an
            // access-denied fault and drop; the connect loop backs off + retries.
            markDisconnected(VENICENET_DRIVER_ERROR, VENICENET_ERR_DRIVER_ACCESS_DENIED,
                             "packet-bridge authentication rejected");
            return;
        }
        if (msg == "meter_delay_disarmed") {
            std::lock_guard<std::mutex> lock(mutex_);
            if (driverState_ != VENICENET_DRIVER_ERROR) {
                driverState_ = VENICENET_DRIVER_NOT_STARTED;
            }
            armed_ = false;
            appliedDelayMs_ = 0.0;
            bufferDepth_ = 0;
            errorCode_ = VENICENET_OK;
            errorText_.clear();
            // fallthrough to notify below
        } else if (msg == "unknown_command" || msg == "intercept_not_running"
                   || msg == "intercept_requires_ips" || msg == "intercept_already_running"
                   || msg.rfind("invalid_", 0) == 0 || msg.rfind("intercept_open_failed", 0) == 0) {
            // Benign command-level refusals: an older service without a new verb,
            // or a verb sent slightly out of order (start races court-IP). These
            // are not driver faults and must not flap driver_state.
            notifyStateChangedIfChanged();
            return;
        } else if (!msg.empty()) {
            std::lock_guard<std::mutex> lock(mutex_);
            driverState_ = VENICENET_DRIVER_ERROR;
            errorCode_ = VENICENET_ERR_INTERNAL;
            errorText_ = msg;
        }
        notifyStateChangedIfChanged();
        return;
    }

    // meter_delay state broadcast OR a delay-verb ack — both carry the service's
    // own applied-delay snapshot. Ignore lines that arrive before auth.
    const bool isSnapshotCarrier =
        event == "meter_delay"
        || (event == "ack" && haveCmd
            && (cmd == "set_meter_delay" || cmd == "start_meter_intercept"
                || cmd == "stop_meter_intercept" || cmd == "meter_delay_stats"
                || cmd == "status" || cmd == "set_court_ip"));
    if (!isSnapshotCarrier || !connected_.load()) {
        return;
    }

    bool active = false;
    const bool haveActive = jsonBool(line, "meter_delay_active", &active);
    double appliedMs = 0.0;
    const bool haveApplied = jsonNumber(line, "meter_delay_ms", &appliedMs);
    double depthD = 0.0;
    const bool haveDepth = jsonNumber(line, "meter_buffer_depth", &depthD);
    std::string courtIp;
    const bool haveCourt = jsonString(line, "court_ip", &courtIp);

    {
        std::lock_guard<std::mutex> lock(mutex_);
        if (haveActive) {
            armed_ = active;
            if (driverState_ != VENICENET_DRIVER_ERROR) {
                driverState_ = active ? VENICENET_DRIVER_HANDLE_OPEN
                                      : VENICENET_DRIVER_READY;
            }
        }
        if (haveApplied && std::isfinite(appliedMs) && appliedMs >= 0.0
            && appliedMs <= 1000.0) {
            appliedDelayMs_ = appliedMs;
        }
        if (haveDepth && depthD >= 0.0 && depthD <= 100000.0) {
            bufferDepth_ = static_cast<uint32_t>(depthD);
        }
        if (haveCourt && !courtIp.empty()) {
            courtIp_ = courtIp;
            courtIpDetected_ = true;
        }
        // A healthy snapshot clears a prior transient error.
        errorCode_ = VENICENET_OK;
        errorText_.clear();
    }
    notifyStateChangedIfChanged();
}

void Core::runOneConnection()
{
    const ServiceEndpoint endpoint = resolveServiceEndpoint();

    SOCKET sock = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (sock == INVALID_SOCKET) {
        markDisconnected(VENICENET_DRIVER_NOT_INSTALLED, VENICENET_OK, std::string());
        return;
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(endpoint.port);
    ::inet_pton(AF_INET, endpoint.host.c_str(), &addr.sin_addr);

    if (::connect(sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        ::closesocket(sock);
        // Service not listening: a stale install / service not started. Degrade
        // gracefully to NOT_INSTALLED — never crash, never fault.
        markDisconnected(VENICENET_DRIVER_NOT_INSTALLED, VENICENET_OK, std::string());
        return;
    }

    // Publish the socket so setters can send on it; keep a local copy for recv.
    {
        std::lock_guard<std::mutex> sendLock(sendMutex_);
        socket_ = static_cast<uintptr_t>(sock);
    }

    // Authenticate with the per-session bearer token.
    const TokenSelection token = resolveBridgeToken();
    if (token.token.empty()) {
        markDisconnected(VENICENET_DRIVER_ERROR, VENICENET_ERR_DRIVER_ACCESS_DENIED,
                         "no usable packet-bridge token");
        return;
    }
    if (!sendRaw("{\"cmd\":\"auth\",\"token\":\"" + jsonEscape(token.token) + "\"}\n")) {
        markDisconnected(VENICENET_DRIVER_NOT_INSTALLED, VENICENET_OK, std::string());
        return;
    }

    // 200 ms recv timeout so the loop can observe ioRunning_ promptly.
    DWORD rcvTimeout = 200;
    ::setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO,
                 reinterpret_cast<const char*>(&rcvTimeout), sizeof(rcvTimeout));

    std::string buf;
    char tmp[8192];
    while (ioRunning_.load()) {
        const int n = ::recv(sock, tmp, sizeof(tmp), 0);
        if (n > 0) {
            buf.append(tmp, static_cast<size_t>(n));
            if (buf.size() > 256u * 1024u) {
                markDisconnected(VENICENET_DRIVER_ERROR, VENICENET_ERR_INTERNAL,
                                 "bridge protocol buffer overflow");
                return;
            }
            size_t nl;
            while ((nl = buf.find('\n')) != std::string::npos) {
                const std::string lineStr = buf.substr(0, nl);
                buf.erase(0, nl + 1);
                handleServiceLine(lineStr);
                if (!connected_.load() && !ioRunning_.load()) {
                    return;
                }
            }
            // handleServiceLine may have already dropped the socket (auth fail).
            {
                std::lock_guard<std::mutex> sendLock(sendMutex_);
                if (socket_ == kInvalidSocket) {
                    return;
                }
            }
        } else if (n == 0) {
            markDisconnected(VENICENET_DRIVER_NOT_INSTALLED, VENICENET_OK, std::string());
            return;
        } else {
            const int err = WSAGetLastError();
            if (err == WSAETIMEDOUT) {
                continue; // idle poll — re-check ioRunning_
            }
            markDisconnected(VENICENET_DRIVER_NOT_INSTALLED, VENICENET_OK, std::string());
            return;
        }
    }

    // Normal shutdown: close cleanly.
    {
        std::lock_guard<std::mutex> sendLock(sendMutex_);
        if (socket_ != kInvalidSocket) {
            ::closesocket(static_cast<SOCKET>(socket_));
            socket_ = kInvalidSocket;
        }
    }
    connected_.store(false);
}

void Core::ioThreadMain()
{
    int backoffMs = 500; // grows to a 5 s ceiling (mirrors _WD_RETRY_DELAY)
    while (ioRunning_.load()) {
        runOneConnection();
        if (!ioRunning_.load()) {
            break;
        }
        // Backoff before the next reconnect attempt, polling the stop flag so
        // shutdown() is not blocked behind a long sleep.
        int remaining = backoffMs;
        while (ioRunning_.load() && remaining > 0) {
            const int slice = remaining < 50 ? remaining : 50;
            ::Sleep(static_cast<DWORD>(slice));
            remaining -= slice;
        }
        backoffMs = backoffMs * 2 < 5000 ? backoffMs * 2 : 5000;
    }
}

} // namespace venicenet

// ───────────────────────────────────────────────────────────────────────────
//  C ABI shims
// ───────────────────────────────────────────────────────────────────────────

extern "C" {

VENICENET_API uint32_t venicenet_abi_version(void)
{
    return VENICENET_ABI_VERSION;
}

VENICENET_API const char* venicenet_version(void)
{
    return VENICENET_VERSION_STRING;
}

VENICENET_API VeniceNetStatus venicenet_init(void)
{
    return venicenet::Core::instance().init();
}

VENICENET_API void venicenet_shutdown(void)
{
    venicenet::Core::instance().shutdown();
}

VENICENET_API VeniceNetStatus venicenet_set_enabled(bool enabled)
{
    return venicenet::Core::instance().setEnabled(enabled);
}

VENICENET_API VeniceNetStatus venicenet_set_target_delay_ms(double target_ms)
{
    return venicenet::Core::instance().setTargetDelayMs(target_ms);
}

VENICENET_API VeniceNetStatus venicenet_set_engage_policy(VeniceNetEngagePolicy policy)
{
    return venicenet::Core::instance().setEngagePolicy(policy);
}

VENICENET_API VeniceNetStatus venicenet_set_offense(bool offense)
{
    return venicenet::Core::instance().setOffense(offense);
}

VENICENET_API VeniceNetStatus venicenet_set_live(bool live_ball)
{
    return venicenet::Core::instance().setLive(live_ball);
}

VENICENET_API VeniceNetStatus venicenet_set_console_ip(const char* console_ipv4)
{
    return venicenet::Core::instance().setConsoleIp(console_ipv4);
}

VENICENET_API VeniceNetStatus venicenet_notify_shot_edge(void)
{
    return venicenet::Core::instance().notifyShotEdge();
}

VENICENET_API VeniceNetStatus venicenet_snapshot(VeniceNetSnapshot* out)
{
    return venicenet::Core::instance().snapshot(out);
}

VENICENET_API VeniceNetStatus venicenet_register_state_callback(
    VeniceNetStateCallback callback, void* user_data)
{
    return venicenet::Core::instance().registerStateCallback(callback, user_data);
}

} // extern "C"
