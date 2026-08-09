// ───────────────────────────────────────────────────────────────────────────
//  venicenet_service_route.cpp — endpoint + bearer-token resolution (Qt-free)
// ───────────────────────────────────────────────────────────────────────────

#include "venicenet_service_route.h"

#include <windows.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>

namespace venicenet {
namespace {

std::string envVar(const char* name)
{
    // GetEnvironmentVariableA rather than getenv: avoids the /sdl banned-CRT
    // warning and reads the live process environment (a test sets it before
    // venicenet_init).
    char buf[1024];
    const DWORD n = ::GetEnvironmentVariableA(name, buf, static_cast<DWORD>(sizeof(buf)));
    if (n == 0 || n >= sizeof(buf)) {
        return std::string();
    }
    return std::string(buf, n);
}

std::string trim(const std::string& s)
{
    size_t b = 0;
    size_t e = s.size();
    while (b < e && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

std::string joinPath(const std::string& base, const char* tail)
{
    if (base.empty()) {
        return std::string();
    }
    std::string out = base;
    if (out.back() != '\\' && out.back() != '/') {
        out.push_back('\\');
    }
    out += tail;
    return out;
}

std::string localAppDataTokenPath()
{
    std::string base = envVar("LOCALAPPDATA");
    if (base.empty()) {
        const std::string profile = envVar("USERPROFILE");
        if (!profile.empty()) {
            base = joinPath(profile, "AppData\\Local");
        }
    }
    if (base.empty()) {
        return std::string();
    }
    return joinPath(base, "NexusVision\\nexus_bridge.token");
}

std::string programDataTokenPath()
{
    std::string base = envVar("PROGRAMDATA");
    if (base.empty()) {
        base = "C:\\ProgramData";
    }
    return joinPath(base, "NexusVision\\nexus_bridge.token");
}

std::string readFileBody(const std::string& path, size_t maxBytes)
{
    HANDLE h = ::CreateFileA(path.c_str(), GENERIC_READ,
                             FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                             nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE) {
        return std::string();
    }
    std::string body;
    body.resize(maxBytes);
    DWORD read = 0;
    const BOOL ok = ::ReadFile(h, body.data(), static_cast<DWORD>(maxBytes), &read, nullptr);
    ::CloseHandle(h);
    if (!ok) {
        return std::string();
    }
    body.resize(read);
    return body;
}

// Only ever answers "yes" when the process is genuinely gone. Anything
// ambiguous (a pid we cannot query — e.g. a LocalSystem service seen from the
// unelevated app) answers "no", so a live bridge is never mistaken for stale.
// Mirrors NetworkBridge::processProvablyGone.
bool processProvablyGone(long long pid)
{
    if (pid <= 0) {
        return false;
    }
    const HANDLE proc = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE,
                                      static_cast<DWORD>(pid));
    if (proc != nullptr) {
        DWORD exitCode = 0;
        const bool queried = ::GetExitCodeProcess(proc, &exitCode) != FALSE;
        ::CloseHandle(proc);
        return queried && exitCode != STILL_ACTIVE;
    }
    return ::GetLastError() == ERROR_INVALID_PARAMETER;
}

// ── minimal flat-JSON field readers (the token record is a compact flat object)
bool jsonFindStringValue(const std::string& obj, const char* key, std::string* out)
{
    const std::string needle = std::string("\"") + key + "\"";
    size_t k = obj.find(needle);
    if (k == std::string::npos) {
        return false;
    }
    size_t p = obj.find(':', k + needle.size());
    if (p == std::string::npos) {
        return false;
    }
    ++p;
    while (p < obj.size() && std::isspace(static_cast<unsigned char>(obj[p]))) ++p;
    if (p >= obj.size() || obj[p] != '"') {
        return false;
    }
    ++p;
    std::string value;
    while (p < obj.size() && obj[p] != '"') {
        if (obj[p] == '\\' && p + 1 < obj.size()) {
            ++p; // token is hex, but tolerate escapes defensively
        }
        value.push_back(obj[p]);
        ++p;
    }
    *out = value;
    return true;
}

bool jsonFindNumberValue(const std::string& obj, const char* key, long long* out)
{
    const std::string needle = std::string("\"") + key + "\"";
    size_t k = obj.find(needle);
    if (k == std::string::npos) {
        return false;
    }
    size_t p = obj.find(':', k + needle.size());
    if (p == std::string::npos) {
        return false;
    }
    ++p;
    while (p < obj.size() && std::isspace(static_cast<unsigned char>(obj[p]))) ++p;
    const size_t start = p;
    if (p < obj.size() && (obj[p] == '-' || obj[p] == '+')) ++p;
    while (p < obj.size() && std::isdigit(static_cast<unsigned char>(obj[p]))) ++p;
    if (p == start) {
        return false;
    }
    *out = std::strtoll(obj.substr(start, p - start).c_str(), nullptr, 10);
    return true;
}

} // namespace

TokenRecord parseTokenRecord(const std::string& path, const std::string& body)
{
    TokenRecord record;
    record.path = path;
    record.exists = true;

    const std::string trimmed = trim(body);
    if (!trimmed.empty() && trimmed.front() == '{') {
        std::string token;
        long long pid = 0;
        long long started = 0;
        const bool haveToken = jsonFindStringValue(trimmed, "token", &token);
        const bool havePid = jsonFindNumberValue(trimmed, "pid", &pid);
        jsonFindNumberValue(trimmed, "started_ms", &started);
        record.token = trim(token);
        record.pid = pid;
        record.startedMs = started;
        record.structured = haveToken && havePid && !record.token.empty() && pid > 0;
        if (record.structured) {
            return record;
        }
        // A malformed JSON body with no usable token falls through to the bare path.
    }
    // Pre-v1 bridge wrote the bare token with no way to tell fresh from stale.
    record.token = trimmed;
    return record;
}

TokenSelection selectBridgeToken(const std::vector<TokenRecord>& records)
{
    TokenSelection selection;

    const TokenRecord* fresh = nullptr;
    const TokenRecord* legacy = nullptr;
    std::string staleNotes;
    std::string otherNotes;
    const auto appendNote = [](std::string& acc, const std::string& note) {
        if (!acc.empty()) acc += ", ";
        acc += note;
    };

    for (const TokenRecord& record : records) {
        if (!record.exists) {
            appendNote(otherNotes, record.path + " (absent)");
            continue;
        }
        if (record.token.empty()) {
            appendNote(otherNotes, record.path + " (present but unreadable or empty)");
            continue;
        }
        if (!record.structured) {
            if (legacy == nullptr) {
                legacy = &record;
            }
            continue;
        }
        if (!record.ownerAlive) {
            appendNote(staleNotes, record.path + " (stale: publishing pid exited)");
            continue;
        }
        if (fresh == nullptr || record.startedMs > fresh->startedMs) {
            fresh = &record;
        }
    }

    const std::string rejected = staleNotes.empty()
        ? std::string()
        : std::string("; ignored ") + staleNotes;

    if (fresh != nullptr) {
        selection.token = fresh->token;
        selection.path = fresh->path;
        selection.diagnostic = "using token from " + fresh->path + rejected;
        return selection;
    }
    if (legacy != nullptr) {
        selection.token = legacy->token;
        selection.path = legacy->path;
        selection.diagnostic =
            "using legacy (pre-v1) token from " + legacy->path +
            " — it carries no publisher pid, so it cannot be freshness-checked" + rejected;
        return selection;
    }

    std::string examined = staleNotes;
    if (!otherNotes.empty()) {
        if (!examined.empty()) examined += ", ";
        examined += otherNotes;
    }
    selection.diagnostic =
        "no usable packet-bridge token: " +
        (examined.empty() ? std::string("no candidate paths examined") : examined) +
        ". The bridge did not publish one for this session.";
    return selection;
}

ServiceEndpoint resolveServiceEndpoint()
{
    ServiceEndpoint endpoint;
    const std::string override = envVar("VENICENET_BRIDGE_PORT");
    if (!override.empty()) {
        const long parsed = std::strtol(override.c_str(), nullptr, 10);
        if (parsed > 0 && parsed <= 65535) {
            endpoint.port = static_cast<uint16_t>(parsed);
        }
    }
    return endpoint;
}

TokenSelection resolveBridgeToken()
{
    // Test/dev override: a literal token short-circuits the file dance so a mock
    // server can be authenticated without touching the real user's token files.
    const std::string override = envVar("VENICENET_BRIDGE_TOKEN");
    if (!override.empty()) {
        TokenSelection selection;
        selection.token = override;
        selection.path = "<VENICENET_BRIDGE_TOKEN env>";
        selection.diagnostic = "using bearer token from VENICENET_BRIDGE_TOKEN override";
        return selection;
    }

    static constexpr size_t kMaxTokenFileBytes = 8 * 1024;
    std::vector<TokenRecord> records;
    const std::string candidates[] = {localAppDataTokenPath(), programDataTokenPath()};
    for (const std::string& path : candidates) {
        if (path.empty()) {
            continue;
        }
        const std::string body = readFileBody(path, kMaxTokenFileBytes);
        if (body.empty()) {
            TokenRecord missing;
            missing.path = path;
            missing.exists = false;
            records.push_back(missing);
            continue;
        }
        TokenRecord record = parseTokenRecord(path, body);
        if (record.structured) {
            record.ownerAlive = !processProvablyGone(record.pid);
        }
        records.push_back(record);
    }
    return selectBridgeToken(records);
}

std::vector<std::string> candidateServiceNames()
{
    // VeniceNetSvc first (wave-2A), NexusVisionSvc second (legacy Python bridge).
    return {"VeniceNetSvc", "NexusVisionSvc"};
}

} // namespace venicenet
