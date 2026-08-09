// rand_s (the CRT CSPRNG) requires this before any CRT header is pulled in.
#define _CRT_RAND_S

#include "IpcServer.h"

#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <aclapi.h>
#include <sddl.h>

#include <algorithm>
#include <cctype>
#include <chrono>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>

#pragma comment(lib, "ws2_32.lib")
#pragma comment(lib, "advapi32.lib")

namespace venicenet {

namespace {

std::string lowerTrim(const std::string& in)
{
    size_t b = 0;
    size_t e = in.size();
    while (b < e && std::isspace(static_cast<unsigned char>(in[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(in[e - 1]))) --e;
    std::string out = in.substr(b, e - b);
    for (char& c : out) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return out;
}

std::string envOr(const char* name, const std::string& fallback)
{
    char* buf = nullptr;
    size_t len = 0;
    if (_dupenv_s(&buf, &len, name) == 0 && buf) {
        std::string v(buf);
        free(buf);
        if (!v.empty()) {
            return v;
        }
    }
    if (buf) free(buf);
    return fallback;
}

std::string programDataTokenPath()
{
    return envOr("PROGRAMDATA", "C:\\ProgramData") + "\\NexusVision\\" + kBridgeTokenFilename;
}

std::string localAppDataTokenPath()
{
    std::string base = envOr("LOCALAPPDATA", "");
    if (base.empty()) {
        base = envOr("USERPROFILE", "C:\\Users\\Default") + "\\AppData\\Local";
    }
    return base + "\\NexusVision\\" + kBridgeTokenFilename;
}

std::wstring widen(const std::string& s)
{
    if (s.empty()) return std::wstring();
    const int n = ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), nullptr, 0);
    std::wstring w(static_cast<size_t>(n), L'\0');
    ::MultiByteToWideChar(CP_UTF8, 0, s.c_str(), static_cast<int>(s.size()), w.data(), n);
    return w;
}

// Best-effort recursive mkdir for the token's parent directory.
void ensureParentDir(const std::string& filePath)
{
    const size_t slash = filePath.find_last_of("\\/");
    if (slash == std::string::npos) return;
    const std::string dir = filePath.substr(0, slash);
    // SHCreateDirectoryEx would need shell32; build it up manually.
    std::string partial;
    size_t start = 0;
    while (start <= dir.size()) {
        size_t next = dir.find_first_of("\\/", start);
        std::string component =
            (next == std::string::npos) ? dir.substr(start) : dir.substr(start, next - start);
        if (!partial.empty()) partial += "\\";
        partial += component;
        if (partial.size() >= 2 && partial[1] == ':' && partial.size() == 2) {
            // drive root, skip creating
        } else if (!partial.empty()) {
            ::CreateDirectoryW(widen(partial).c_str(), nullptr);
        }
        if (next == std::string::npos) break;
        start = next + 1;
    }
}

std::string currentUserSidString()
{
    HANDLE token = nullptr;
    if (!::OpenProcessToken(::GetCurrentProcess(), TOKEN_QUERY, &token)) {
        return "";
    }
    DWORD len = 0;
    ::GetTokenInformation(token, TokenUser, nullptr, 0, &len);
    std::vector<std::uint8_t> buf(len);
    std::string result;
    if (len && ::GetTokenInformation(token, TokenUser, buf.data(), len, &len)) {
        auto* tu = reinterpret_cast<TOKEN_USER*>(buf.data());
        LPWSTR sidStr = nullptr;
        if (::ConvertSidToStringSidW(tu->User.Sid, &sidStr)) {
            const int n =
                ::WideCharToMultiByte(CP_UTF8, 0, sidStr, -1, nullptr, 0, nullptr, nullptr);
            std::string s(static_cast<size_t>(n > 0 ? n - 1 : 0), '\0');
            if (n > 0) {
                ::WideCharToMultiByte(CP_UTF8, 0, sidStr, -1, s.data(), n, nullptr, nullptr);
            }
            result = s;
            ::LocalFree(sidStr);
        }
    }
    ::CloseHandle(token);
    return result;
}

// Pin *path* to a protected minimal DACL and verify the round trip. Returns true
// on success. Mirrors nexus_svc._harden_token_acl + _verify_token_acl (protected
// DACL is the load-bearing property: it blocks the inherited "Users: read" ACE
// that %PROGRAMDATA% grants by default).
bool hardenTokenAcl(const std::string& path, bool allowInteractiveRead)
{
    std::string sddl = "D:P(A;;FA;;;SY)(A;;FA;;;BA)";
    if (allowInteractiveRead) {
        sddl += "(A;;FR;;;IU)";
    } else {
        const std::string sid = currentUserSidString();
        if (sid.empty()) {
            return false;
        }
        sddl += "(A;;FA;;;" + sid + ")";
    }

    PSECURITY_DESCRIPTOR psd = nullptr;
    if (!::ConvertStringSecurityDescriptorToSecurityDescriptorW(widen(sddl).c_str(), SDDL_REVISION_1,
                                                                &psd, nullptr)) {
        return false;
    }
    BOOL present = FALSE;
    BOOL defaulted = FALSE;
    PACL dacl = nullptr;
    bool ok = ::GetSecurityDescriptorDacl(psd, &present, &dacl, &defaulted) && present;
    if (ok) {
        const DWORD rc = ::SetNamedSecurityInfoW(
            widen(path).data(), SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION, nullptr, nullptr, dacl,
            nullptr);
        ok = (rc == ERROR_SUCCESS);
    }
    ::LocalFree(psd);
    if (!ok) {
        return false;
    }

    // Verify: the DACL must have come back PROTECTED, else an inherited ACE could
    // still grant access.
    PSECURITY_DESCRIPTOR readSd = nullptr;
    if (::GetNamedSecurityInfoW(widen(path).c_str(), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION,
                                nullptr, nullptr, nullptr, nullptr, &readSd)
        != ERROR_SUCCESS) {
        return false;
    }
    SECURITY_DESCRIPTOR_CONTROL control = 0;
    DWORD revision = 0;
    bool protectedOk = false;
    if (::GetSecurityDescriptorControl(readSd, &control, &revision)) {
        protectedOk = (control & SE_DACL_PROTECTED) != 0;
    }
    ::LocalFree(readSd);
    return protectedOk;
}

bool writeTokenFile(const std::string& path, const std::string& payload, bool allowInteractiveRead)
{
    ensureParentDir(path);
    HANDLE h = ::CreateFileW(widen(path).c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS,
                             FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        return false;
    }
    DWORD written = 0;
    const bool wrote =
        ::WriteFile(h, payload.data(), static_cast<DWORD>(payload.size()), &written, nullptr)
        && written == payload.size();
    ::CloseHandle(h);
    if (!wrote) {
        ::DeleteFileW(widen(path).c_str());
        return false;
    }
    if (!hardenTokenAcl(path, allowInteractiveRead)) {
        ::DeleteFileW(widen(path).c_str());
        return false;
    }
    // Prove the round trip: an unreadable/half-written token is as broken as none.
    HANDLE r = ::CreateFileW(widen(path).c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr,
                             OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (r == INVALID_HANDLE_VALUE) {
        ::DeleteFileW(widen(path).c_str());
        return false;
    }
    std::string readBack(payload.size(), '\0');
    DWORD got = 0;
    const bool same =
        ::ReadFile(r, readBack.data(), static_cast<DWORD>(payload.size()), &got, nullptr)
        && got == payload.size() && readBack == payload;
    ::CloseHandle(r);
    if (!same) {
        ::DeleteFileW(widen(path).c_str());
        return false;
    }
    return true;
}

std::string mintTokenHex()
{
    // 24 random bytes -> 48 hex chars (secrets.token_hex(24)). rand_s is the
    // CRT's CSPRNG on Windows.
    static const char* hex = "0123456789abcdef";
    std::string out;
    out.reserve(48);
    for (int i = 0; i < 24; ++i) {
        unsigned int v = 0;
        if (rand_s(&v) != 0) {
            v = static_cast<unsigned int>(std::rand());
        }
        const unsigned int b = v & 0xFFu;
        out.push_back(hex[(b >> 4) & 0xF]);
        out.push_back(hex[b & 0xF]);
    }
    return out;
}

bool sendAll(SOCKET sock, const std::string& data)
{
    size_t total = 0;
    while (total < data.size()) {
        const int chunk =
            static_cast<int>(std::min<size_t>(data.size() - total, static_cast<size_t>(INT_MAX)));
        const int sent = ::send(sock, data.data() + total, chunk, 0);
        if (sent == SOCKET_ERROR || sent <= 0) {
            return false;
        }
        total += static_cast<size_t>(sent);
    }
    return true;
}

} // namespace

// ── ClientQueue / BroadcastHub ───────────────────────────────────────────

void ClientQueue::push(const std::string& line)
{
    std::lock_guard<std::mutex> lock(m);
    if (closed) return;
    if (lines.size() >= kMaxQueue) {
        lines.pop_front(); // drop oldest, like nexus_svc's full-queue policy
    }
    lines.push_back(line);
    cv.notify_one();
}

void ClientQueue::markAuthed()
{
    std::lock_guard<std::mutex> lock(m);
    authed = true;
}

void ClientQueue::close()
{
    std::lock_guard<std::mutex> lock(m);
    closed = true;
    cv.notify_all();
}

std::shared_ptr<ClientQueue> BroadcastHub::addClient()
{
    auto q = std::make_shared<ClientQueue>();
    std::lock_guard<std::mutex> lock(mutex_);
    clients_.push_back(q);
    return q;
}

void BroadcastHub::removeClient(const std::shared_ptr<ClientQueue>& q)
{
    std::lock_guard<std::mutex> lock(mutex_);
    clients_.erase(std::remove(clients_.begin(), clients_.end(), q), clients_.end());
}

int BroadcastHub::authedClientCount()
{
    std::lock_guard<std::mutex> lock(mutex_);
    int n = 0;
    for (const auto& q : clients_) {
        std::lock_guard<std::mutex> qlock(q->m);
        if (q->authed) ++n;
    }
    return n;
}

void BroadcastHub::broadcast(const std::string& line)
{
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& q : clients_) {
        // Capture events are privileged: only authed clients receive them.
        std::lock_guard<std::mutex> qlock(q->m);
        if (!q->authed || q->closed) continue;
        if (q->lines.size() >= ClientQueue::kMaxQueue) {
            q->lines.pop_front();
        }
        q->lines.push_back(line);
        q->cv.notify_one();
    }
}

// ── IpcServer ────────────────────────────────────────────────────────────

IpcServer::~IpcServer()
{
    stop();
}

void IpcServer::configure(MeterDelayIntercept* delay, ServiceStateMachine* state, bool armed,
                          bool pydivertOk)
{
    delay_ = delay;
    state_ = state;
    armed_ = armed;
    pydivertOk_ = pydivertOk;
}

bool IpcServer::publishToken(const std::string& mode)
{
    token_.clear();
    tokenPath_.clear();
    tokenMode_.clear();

    const std::string token = mintTokenHex();

    JsonValue record = JsonValue::makeObject();
    record.setInt("v", kBridgeTokenRecordVersion);
    record.setString("token", token);
    record.setInt("pid", static_cast<std::int64_t>(::GetCurrentProcessId()));
    record.setInt("started_ms",
                  static_cast<std::int64_t>(static_cast<double>(::time(nullptr)) * 1000.0));
    record.setString("mode", mode);
    const std::string payload = record.serialize();

    std::vector<std::pair<std::string, bool>> attempts;
    if (mode == "service") {
        attempts.emplace_back(programDataTokenPath(), true);
    } else {
        attempts.emplace_back(localAppDataTokenPath(), false);
        attempts.emplace_back(programDataTokenPath(), false);
    }

    for (const auto& attempt : attempts) {
        if (writeTokenFile(attempt.first, payload, attempt.second)) {
            token_ = token;
            tokenPath_ = attempt.first;
            tokenMode_ = mode;
            if (log_) {
                log_("Bridge token published: path=" + attempt.first + " mode=" + mode
                     + " acl=protected+verified");
            }
            return true;
        }
        if (log_) {
            log_("Bridge token could not be published to " + attempt.first);
        }
    }
    if (log_) {
        log_("Bridge token could not be published to any location — auth is fail-closed");
    }
    return false;
}

void IpcServer::setConsoleIpFromClient(const std::string& ip)
{
    {
        std::lock_guard<std::mutex> lock(ipMutex_);
        if (consoleIp_ == ip) {
            return;
        }
        consoleIp_ = ip;
    }
    if (onFilterChanged_) onFilterChanged_();
    retargetIntercept();
}

void IpcServer::clearConsoleIp()
{
    setConsoleIpFromClient("");
}

void IpcServer::setCourtIpFromClient(const std::string& ip)
{
    {
        std::lock_guard<std::mutex> lock(ipMutex_);
        courtIp_ = ip;
        courtIpClientSet_ = true;
    }
    retargetIntercept();
}

void IpcServer::setDetectedCourtIp(const std::string& ip)
{
    {
        std::lock_guard<std::mutex> lock(ipMutex_);
        if (courtIpClientSet_ || courtIp_ == ip) {
            return; // a client set always wins
        }
        courtIp_ = ip;
    }
    retargetIntercept();
}

std::pair<std::string, std::string> IpcServer::currentIps() const
{
    std::lock_guard<std::mutex> lock(ipMutex_);
    return {consoleIp_, courtIp_};
}

std::string IpcServer::consoleIp() const
{
    std::lock_guard<std::mutex> lock(ipMutex_);
    return consoleIp_;
}

void IpcServer::retargetIntercept()
{
    if (!delay_) return;
    const auto ips = currentIps();
    try {
        delay_->retarget(ips.first, ips.second);
    } catch (...) {
    }
}

// ── Server lifecycle ──────────────────────────────────────────────────────

bool IpcServer::startListening()
{
    WSADATA wsa;
    if (::WSAStartup(MAKEWORD(2, 2), &wsa) != 0) {
        return false;
    }
    SOCKET srv = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (srv == INVALID_SOCKET) {
        return false;
    }
    BOOL reuse = TRUE;
    ::setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&reuse),
                 sizeof(reuse));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(static_cast<u_short>(kListenPort));
    ::inet_pton(AF_INET, kListenHost, &addr.sin_addr);
    if (::bind(srv, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0
        || ::listen(srv, 8) != 0) {
        ::closesocket(srv);
        return false;
    }
    listenSock_ = static_cast<std::uintptr_t>(srv);
    running_.store(true);
    acceptThread_ = std::thread(&IpcServer::acceptLoop, this);
    if (log_) {
        log_(std::string("Listening on ") + kListenHost + ":" + std::to_string(kListenPort));
    }
    return true;
}

void IpcServer::stop()
{
    if (!running_.exchange(false)) {
        // Even if never started, still join threads / cleanup below is a no-op.
    }
    if (listenSock_ != ~static_cast<std::uintptr_t>(0)) {
        ::closesocket(static_cast<SOCKET>(listenSock_));
        listenSock_ = ~static_cast<std::uintptr_t>(0);
    }
    if (acceptThread_.joinable()) {
        acceptThread_.join();
    }
    std::vector<std::thread> threads;
    {
        std::lock_guard<std::mutex> lock(clientThreadsMutex_);
        threads = std::move(clientThreads_);
        clientThreads_.clear();
    }
    for (auto& t : threads) {
        if (t.joinable()) t.join();
    }
}

void IpcServer::acceptLoop()
{
    const SOCKET srv = static_cast<SOCKET>(listenSock_);
    while (running_.load()) {
        fd_set readSet;
        FD_ZERO(&readSet);
        FD_SET(srv, &readSet);
        timeval tv{};
        tv.tv_sec = 1;
        tv.tv_usec = 0;
        const int ready = ::select(0, &readSet, nullptr, nullptr, &tv);
        if (ready == SOCKET_ERROR) {
            break;
        }
        if (ready == 0) {
            continue;
        }
        sockaddr_in peer{};
        int peerLen = sizeof(peer);
        const SOCKET conn = ::accept(srv, reinterpret_cast<sockaddr*>(&peer), &peerLen);
        if (conn == INVALID_SOCKET) {
            if (!running_.load()) break;
            continue;
        }
        std::lock_guard<std::mutex> lock(clientThreadsMutex_);
        // Reap finished client threads opportunistically.
        clientThreads_.erase(std::remove_if(clientThreads_.begin(), clientThreads_.end(),
                                            [](std::thread& t) { return !t.joinable(); }),
                             clientThreads_.end());
        clientThreads_.emplace_back(&IpcServer::handleClient, this,
                                    static_cast<std::uintptr_t>(conn));
    }
}

void IpcServer::clientWriter(std::uintptr_t sockHandle, std::shared_ptr<ClientQueue> q,
                             std::shared_ptr<std::atomic<bool>> done)
{
    const SOCKET sock = static_cast<SOCKET>(sockHandle);
    while (!done->load()) {
        std::string line;
        {
            std::unique_lock<std::mutex> lock(q->m);
            q->cv.wait_for(lock, std::chrono::milliseconds(200),
                           [&] { return !q->lines.empty() || q->closed; });
            if (q->closed && q->lines.empty()) {
                if (done->load()) break;
                continue;
            }
            if (q->lines.empty()) {
                continue;
            }
            line = std::move(q->lines.front());
            q->lines.pop_front();
        }
        if (!sendAll(sock, line)) {
            break;
        }
    }
}

void IpcServer::handleClient(std::uintptr_t sockHandle)
{
    const SOCKET sock = static_cast<SOCKET>(sockHandle);
    auto q = hub_.addClient();
    auto done = std::make_shared<std::atomic<bool>>(false);
    std::thread writer(&IpcServer::clientWriter, this, sockHandle, q, done);

    // 1s recv timeout so the reader loop can observe running_/done without a
    // dedicated wakeup channel (mirrors nexus_svc's conn.settimeout(1.0)).
    DWORD timeout = 1000;
    ::setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout),
                 sizeof(timeout));

    // Greet with hello BEFORE auth (the client keys meter-delay support off it).
    std::vector<std::string> features;
    if (armed_) features.emplace_back("meter_delay");
    q->push(buildHelloLine(kSvcVersion, features) + "\n");

    bool authed = false;
    bool authFailureLogged = false;
    std::string buf;
    while (running_.load() && !done->load()) {
        char tmp[4096];
        const int n = ::recv(sock, tmp, sizeof(tmp), 0);
        if (n == SOCKET_ERROR) {
            const int err = ::WSAGetLastError();
            if (err == WSAETIMEDOUT) {
                continue;
            }
            break;
        }
        if (n == 0) {
            break;
        }
        buf.append(tmp, static_cast<size_t>(n));
        if (buf.size() > kMaxCommandBufferBytes) {
            q->push(std::string("{\"event\":\"error\",\"msg\":\"command_too_large\"}\n"));
            break;
        }
        size_t nl;
        while ((nl = buf.find('\n')) != std::string::npos) {
            std::string line = buf.substr(0, nl);
            buf.erase(0, nl + 1);
            if (line.size() > kMaxCommandLineBytes) {
                q->push(std::string("{\"event\":\"error\",\"msg\":\"command_too_large\"}\n"));
                done->store(true);
                break;
            }
            JsonValue cmd;
            if (!JsonValue::parse(line, cmd)) {
                continue; // malformed JSON is ignored, like nexus_svc
            }
            handleCommand(q, authed, authFailureLogged, cmd);
        }
    }

    done->store(true);
    q->close();
    hub_.removeClient(q);
    onClientDisconnected(q);
    ::closesocket(sock);
    if (writer.joinable()) {
        writer.join();
    }
}

void IpcServer::onClientDisconnected(const std::shared_ptr<ClientQueue>& q)
{
    (void)q;
    // Dead-man switch: if nobody authenticated is left, nobody can turn the delay
    // off, so tear the intercept down and release every buffered packet.
    if (!delay_) return;
    try {
        if (hub_.authedClientCount() > 0) {
            return;
        }
        delay_->deadManStop("last_client_disconnected");
    } catch (...) {
    }
}

MeterDelayIntercept* IpcServer::armedDelayOrError(std::shared_ptr<ClientQueue>& q,
                                                  const std::string& verb)
{
    if (delay_ != nullptr) {
        return delay_;
    }
    q->push(buildDisarmedErrorLine(verb) + "\n");
    return nullptr;
}

void IpcServer::handleCommand(std::shared_ptr<ClientQueue> q, bool& authed, bool& authFailureLogged,
                              const JsonValue& cmd)
{
    if (!cmd.isObject()) {
        q->push(std::string("{\"event\":\"error\",\"msg\":\"invalid_command\"}\n"));
        return;
    }
    const std::string c = lowerTrim(cmd.getString("cmd"));

    if (!authed) {
        const std::string suppliedToken = cmd.getString("token");
        if (c == "auth" && !token_.empty() && constantTimeEquals(suppliedToken, token_)) {
            authed = true;
            q->markAuthed();
            JsonValue ack = JsonValue::makeObject();
            ack.setString("event", "ack");
            ack.setString("cmd", "auth");
            q->push(ack.serialize() + "\n");
            return;
        }
        std::string reason;
        if (c != "auth") {
            reason = "auth_required";
        } else if (token_.empty()) {
            reason = "no_server_token";
        } else {
            reason = "token_mismatch";
        }
        if (!authFailureLogged) {
            authFailureLogged = true;
            if (log_) log_("Auth rejected: " + reason);
        }
        q->push(buildUnauthorizedLine(reason, tokenPath_, tokenMode_) + "\n");
        return;
    }

    if (c == "ping") {
        q->push(std::string("{\"event\":\"pong\"}\n"));
    } else if (c == "set_filter") {
        const std::string ip = MeterDelayIntercept::validatedIpv4(cmd.getString("console_ip"));
        if (ip.empty()) {
            q->push(std::string("{\"event\":\"error\",\"msg\":\"invalid_console_ip\"}\n"));
            return;
        }
        setConsoleIpFromClient(ip);
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "set_filter");
        ack.setString("console_ip", ip);
        q->push(ack.serialize() + "\n");
    } else if (c == "clear_filter") {
        clearConsoleIp();
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "clear_filter");
        q->push(ack.serialize() + "\n");
    } else if (c == "set_court_ip") {
        const std::string ip =
            MeterDelayIntercept::validatedIpv4(cmd.getString("court_ip"), /*requirePublic=*/true);
        if (ip.empty()) {
            q->push(std::string("{\"event\":\"error\",\"msg\":\"invalid_court_ip\"}\n"));
            return;
        }
        setCourtIpFromClient(ip);
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "set_court_ip");
        ack.setString("court_ip", ip);
        q->push(ack.serialize() + "\n");
    } else if (c == "start_meter_intercept") {
        MeterDelayIntercept* delay = armedDelayOrError(q, c);
        if (!delay) return;
        const auto ips = currentIps();
        const auto result = delay->start(ips.first, ips.second);
        if (!result.first) {
            if (state_) {
                state_->onError(ServiceError::DriverOpenFailed, result.second);
            }
            JsonValue err = JsonValue::makeObject();
            err.setString("event", "error");
            err.setString("msg", result.second);
            err.setString("cmd", "start_meter_intercept");
            q->push(err.serialize() + "\n");
            return;
        }
        if (state_) {
            state_->onHandleOpened();
        }
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "start_meter_intercept");
        ack.setString("console_ip", ips.first);
        ack.setString("court_ip", ips.second);
        addSnapshotFields(ack, delay->snapshot());
        q->push(ack.serialize() + "\n");
    } else if (c == "stop_meter_intercept") {
        MeterDelayIntercept* delay = armedDelayOrError(q, c);
        if (!delay) return;
        const bool stopped = delay->stop("client_request");
        if (state_ && stopped) {
            state_->onHandleClosed();
        }
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "stop_meter_intercept");
        ack.setBool("was_active", stopped);
        addSnapshotFields(ack, delay->snapshot());
        q->push(ack.serialize() + "\n");
    } else if (c == "set_meter_delay") {
        MeterDelayIntercept* delay = armedDelayOrError(q, c);
        if (!delay) return;
        if (!delay->active()) {
            JsonValue err = JsonValue::makeObject();
            err.setString("event", "error");
            err.setString("msg", "intercept_not_running");
            err.setString("cmd", "set_meter_delay");
            q->push(err.serialize() + "\n");
            return;
        }
        if (!cmd.hasNumber("delay_ms")) {
            q->push(std::string("{\"event\":\"error\",\"msg\":\"invalid_delay_ms\"}\n"));
            return;
        }
        double accepted = 0.0;
        try {
            accepted = delay->setDelay(cmd.getNumber("delay_ms"));
        } catch (...) {
            q->push(std::string("{\"event\":\"error\",\"msg\":\"invalid_delay_ms\"}\n"));
            return;
        }
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "set_meter_delay");
        addSnapshotFields(ack, delay->snapshot());
        ack.setDouble("target_ms", accepted);
        ack.setDouble("slew_cap_ms_per_s", MeterDelayIntercept::kMaxSlewMsPerS);
        q->push(ack.serialize() + "\n");
    } else if (c == "meter_delay_stats") {
        MeterDelayIntercept* delay = armedDelayOrError(q, c);
        if (!delay) return;
        JsonValue ack = JsonValue::makeObject();
        ack.setString("event", "ack");
        ack.setString("cmd", "meter_delay_stats");
        addSnapshotFields(ack, delay->snapshot());
        const MeterDelayIntercept::Stats st = delay->stats();
        JsonValue stats = JsonValue::makeObject();
        stats.setInt("intercepted", st.intercepted);
        stats.setInt("passed", st.passed);
        stats.setInt("delayed", st.delayed);
        stats.setInt("sent", st.sent);
        stats.setInt("overflow_released", st.overflowReleased);
        stats.setInt("send_errors", st.sendErrors);
        stats.setInt("watchdog_trips", st.watchdogTrips);
        stats.setInt("dead_man_trips", st.deadManTrips);
        stats.setInt("slew_limited_steps", st.slewLimitedSteps);
        stats.setInt("emergency_zero", st.emergencyZero);
        stats.setDouble("peak_slew_ms_per_s", st.peakSlewMsPerS);
        stats.setDouble("slew_cap_ms_per_s", st.slewCapMsPerS);
        ack.set("stats", std::move(stats));
        q->push(ack.serialize() + "\n");
    } else if (c == "status") {
        JsonValue status = JsonValue::makeObject();
        status.setString("event", "status");
        status.setInt("version", kSvcVersion);
        JsonValue features = JsonValue::makeArray();
        if (armed_) features.append(JsonValue(std::string("meter_delay")));
        status.set("features", std::move(features));
        status.setBool("pydivert_ok", pydivertOk_);
        status.setBool("meter_delay_armed", armed_);
        if (delay_) {
            addSnapshotFields(status, delay_->snapshot());
        }
        q->push(status.serialize() + "\n");
    } else {
        q->push(buildUnknownCommandLine(c) + "\n");
    }
}

// ── Broadcast builders ────────────────────────────────────────────────────

void IpcServer::addSnapshotFields(JsonValue& obj, const MeterDelayIntercept::Snapshot& s)
{
    obj.setBool("meter_delay_active", s.active);
    obj.setDouble("meter_delay_ms", s.delayMs);
    obj.setDouble("meter_delay_target_ms", s.targetMs);
    obj.setBool("meter_delay_settled", s.settled);
    obj.setInt("meter_buffer_depth", s.bufferDepth);
}

void IpcServer::broadcastMeterDelay(const std::string& state, const std::string& reason)
{
    if (!delay_) return;
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "meter_delay");
    msg.setString("state", state);
    msg.setString("reason", reason);
    addSnapshotFields(msg, delay_->snapshot());
    const std::string filter = delay_->filter();
    if (!filter.empty()) {
        msg.setString("filter", filter);
    }
    hub_.broadcast(msg.serialize() + "\n");
}

void IpcServer::broadcastPacket(const std::string& srcIp, const std::string& dstIp, int srcPort,
                                int dstPort, const std::string& proto, int size, bool outbound,
                                double tsMs)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "packet");
    msg.setString("src_ip", srcIp);
    msg.setString("dst_ip", dstIp);
    msg.setInt("src_port", srcPort);
    msg.setInt("dst_port", dstPort);
    msg.setString("proto", proto);
    msg.setInt("size", size);
    msg.setBool("outbound", outbound);
    msg.setDouble("ts", tsMs);
    if (delay_) {
        addSnapshotFields(msg, delay_->snapshot());
    }
    hub_.broadcast(msg.serialize() + "\n");
}

void IpcServer::broadcastReady(const std::string& filter)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "ready");
    msg.setString("filter", filter);
    hub_.broadcast(msg.serialize() + "\n");
}

void IpcServer::broadcastError(const std::string& msgText)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "error");
    msg.setString("msg", msgText);
    hub_.broadcast(msg.serialize() + "\n");
}

// ── Pure protocol helpers ─────────────────────────────────────────────────

std::string IpcServer::extractCmdVerb(const std::string& line)
{
    JsonValue v;
    if (!JsonValue::parse(line, v) || !v.isObject()) {
        return "";
    }
    return lowerTrim(v.getString("cmd"));
}

bool IpcServer::constantTimeEquals(const std::string& a, const std::string& b)
{
    if (a.size() != b.size()) {
        return false;
    }
    unsigned char diff = 0;
    for (size_t i = 0; i < a.size(); ++i) {
        diff |= static_cast<unsigned char>(a[i]) ^ static_cast<unsigned char>(b[i]);
    }
    return diff == 0;
}

std::string IpcServer::buildHelloLine(int version, const std::vector<std::string>& features)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "hello");
    msg.setInt("version", version);
    JsonValue arr = JsonValue::makeArray();
    for (const std::string& f : features) {
        arr.append(JsonValue(f));
    }
    msg.set("features", std::move(arr));
    return msg.serialize();
}

std::string IpcServer::buildUnauthorizedLine(const std::string& reason, const std::string& path,
                                             const std::string& mode)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "error");
    msg.setString("msg", "unauthorized");
    msg.setString("reason", reason);
    msg.setString("token_path", path);
    msg.setString("token_mode", mode);
    return msg.serialize();
}

std::string IpcServer::buildDisarmedErrorLine(const std::string& verb)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "error");
    msg.setString("msg", "meter_delay_disarmed");
    msg.setString("cmd", verb);
    msg.setString("detail",
                  "the inbound meter delay ships DISARMED. Start the bridge with "
                  "--arm-meter-delay or set ORION_METER_DELAY_ARMED=1. It is deliberately "
                  "not exposed through the launcher or the app settings.");
    return msg.serialize();
}

std::string IpcServer::buildUnknownCommandLine(const std::string& verb)
{
    JsonValue msg = JsonValue::makeObject();
    msg.setString("event", "error");
    msg.setString("msg", "unknown_command");
    msg.setString("cmd", verb);
    return msg.serialize();
}

} // namespace venicenet
