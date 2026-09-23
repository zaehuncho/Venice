#include "BrokerContract.h"

#include <cctype>
#include <cstdio>
#include <utility>
#include <vector>

namespace orion {
namespace broker {

const char*    kApiHostAscii     = "api.zaeorion.com";
const wchar_t* kApiHostW         = L"api.zaeorion.com";
const wchar_t* kActivatePathW    = L"/api/license/redeem";
const wchar_t* kUserAgentPrefixW = L"OrionLauncher/";

namespace {

// Exactly shard_bootstrap.c's value-char rule: printable ASCII, never backslash.
bool valueCharOk(unsigned char c)
{
    return c >= 0x21 && c <= 0x7e && c != '\\';
}

// Portable secure-zero (Codex finding #3). BrokerContract stays free of Win32, so
// this uses a volatile write loop the optimizer must not elide, then clears the
// string. Used to wipe every intermediate copy of a token/token_id the response
// parser makes before it is freed.
void secureWipe(std::string& s)
{
    if (!s.empty()) {
        volatile char* p = &s[0];
        for (std::size_t i = 0; i < s.size(); ++i) p[i] = 0;
    }
    s.clear();
}

// RAII: secureWipe a std::string on scope exit, UNCONDITIONALLY, on every path
// including exceptions and early returns (Codex finding #3, round 2). Used to wipe
// parser accumulators/intermediates that may transiently hold a token/token_id so
// no secret-bearing buffer is freed unwiped.
struct ScopedWipe {
    std::string& s;
    explicit ScopedWipe(std::string& str) : s(str) {}
    ~ScopedWipe() { secureWipe(s); }
    ScopedWipe(const ScopedWipe&) = delete;
    ScopedWipe& operator=(const ScopedWipe&) = delete;
};

} // namespace

std::string jsonStringField(const std::string& json, const std::string& field)
{
    const std::string needle = "\"" + field + "\"";
    std::size_t pos = json.find(needle);
    if (pos == std::string::npos) {
        return {};
    }
    pos += needle.size();
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t' || json[pos] == ':')) {
        ++pos;
    }
    if (pos >= json.size() || json[pos] != '"') {
        return {};
    }
    ++pos;
    const std::size_t start = pos;
    while (pos < json.size() && json[pos] != '"') {
        if (!valueCharOk(static_cast<unsigned char>(json[pos]))) {
            return {};
        }
        ++pos;
    }
    if (pos >= json.size() || pos == start) {
        return {};
    }
    return json.substr(start, pos - start);
}

bool isHexString(const std::string& value, size_t len)
{
    if (value.size() != len) {
        return false;
    }
    for (char c : value) {
        if (!std::isxdigit(static_cast<unsigned char>(c))) {
            return false;
        }
    }
    return true;
}

bool jsonBoolTrue(const std::string& json, const std::string& field)
{
    const std::string needle = "\"" + field + "\"";
    std::size_t pos = json.find(needle);
    if (pos == std::string::npos) {
        return false;
    }
    pos += needle.size();
    while (pos < json.size() && (json[pos] == ' ' || json[pos] == '\t' || json[pos] == ':')) {
        ++pos;
    }
    return json.compare(pos, 4, "true") == 0;
}

std::string buildActivateBody(const std::string& pairCode,
                              const std::string& machineId,
                              const std::string& clientVersion,
                              const std::string& requestNonce,
                              long long requestTimestamp,
                              bool sessionOnly)
{
    std::string ts = std::to_string(requestTimestamp);
    // Field set + names mirror licenseActivateRequestBody(). Order is irrelevant
    // to the server (json.loads); values here are constrained charsets (PAIR
    // code, hex machine_id, dotted version, UUID nonce) so no escaping is needed.
    std::string body;
    body.reserve(288);
    body += "{\"license_key\":\"";           body += pairCode;
    body += "\",\"key\":\"";                  body += pairCode;
    body += "\",\"machine_id\":\"";           body += machineId;
    body += "\",\"client_version\":\"";       body += clientVersion;
    body += "\",\"fingerprint_version\":2";
    body += ",\"request_nonce\":\"";          body += requestNonce;
    body += "\",\"request_timestamp\":";      body += ts;
    // Broker/session-only mode: tell the backend to withhold license material
    // (canonical_license_key) from the response — the broker only needs a session.
    if (sessionOnly) {
        body += ",\"session_only\":true";
    }
    body += "}";
    return body;
}

std::string buildActivateHeaders(const std::string& clientVersion,
                                 const std::string& requestNonce,
                                 long long requestTimestamp,
                                 const std::string& requestId)
{
    std::string ts = std::to_string(requestTimestamp);
    std::string h;
    h.reserve(256);
    h += "Content-Type: application/json\r\n";
    h += "Accept: application/json\r\n";
    h += "User-Agent: OrionLauncher/" + clientVersion + " (Windows NT 10.0; Win64; x64)\r\n";
    h += "X-Orion-Request-Nonce: " + requestNonce + "\r\n";
    h += "X-Orion-Request-Timestamp: " + ts + "\r\n";
    h += "X-Orion-Request-Id: " + requestId + "\r\n";
    return h;
}

std::string buildSessionJson(const std::string& token,
                             const std::string& tokenId,
                             const std::string& machineId)
{
    // Every value must survive shard_bootstrap.c's json_string_field AND (for the
    // token) fetch_shard's stricter check (also rejects '"'). Fail closed.
    auto safe = [](const std::string& v, bool rejectQuote) {
        if (v.empty()) {
            return false;
        }
        for (unsigned char c : v) {
            if (!valueCharOk(c)) {
                return false;
            }
            if (rejectQuote && c == '"') {
                return false;
            }
        }
        return true;
    };
    if (!safe(token, true) || !safe(tokenId, true) || !isHexString(machineId, 64)) {
        return {};
    }
    std::string j;
    j.reserve(160);
    j += "{\"token\":\"";        j += token;
    j += "\",\"token_id\":\"";   j += tokenId;
    j += "\",\"machine_id\":\""; j += machineId;
    j += "\"}";
    return j;
}

// ── strict, bounded JSON object parser (Codex finding #4) ───────────────────
namespace {

struct StrictMember {
    std::string key;
    int type = 0;        // 1=string, 2=true, 3=false, 4=other-scalar(num/null), 5=nested
    std::string str;     // decoded string value when type==1
};

// A hand-rolled validator that fully parses ONE JSON value, advancing `i`. It
// returns false on any malformed input. When `topMembers` is non-null and the
// value is the top-level object, it records each top-level member (with duplicate
// detection). Nested objects/arrays are fully validated but not recorded.
class StrictJson {
public:
    StrictJson(const std::string& s) : s_(s), i_(0) {}

    bool parseDocument(std::vector<StrictMember>* topMembers)
    {
        skipWs();
        if (peek() != '{') return false;
        if (!parseObject(topMembers)) return false;
        skipWs();
        return i_ >= s_.size();  // no trailing garbage
    }

private:
    const std::string& s_;
    std::size_t i_;

    char peek() const { return i_ < s_.size() ? s_[i_] : '\0'; }
    void skipWs()
    {
        while (i_ < s_.size()) {
            const char c = s_[i_];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') ++i_;
            else break;
        }
    }

    bool parseValue(int* typeOut, std::string* strOut)
    {
        skipWs();
        const char c = peek();
        if (c == '"') { if (typeOut) *typeOut = 1; return parseString(strOut); }
        if (c == '{') { if (typeOut) *typeOut = 5; return parseObject(nullptr); }
        if (c == '[') { if (typeOut) *typeOut = 5; return parseArray(); }
        if (c == 't') { if (typeOut) *typeOut = 2; return parseLiteral("true"); }
        if (c == 'f') { if (typeOut) *typeOut = 3; return parseLiteral("false"); }
        if (c == 'n') { if (typeOut) *typeOut = 4; return parseLiteral("null"); }
        if (c == '-' || (c >= '0' && c <= '9')) { if (typeOut) *typeOut = 4; return parseNumber(); }
        return false;
    }

    bool parseLiteral(const char* lit)
    {
        const std::size_t n = std::string(lit).size();
        if (s_.compare(i_, n, lit) != 0) return false;
        i_ += n;
        return true;
    }

    // RFC 8259 number grammar (Codex finding #4, round 2). The old scan accepted
    // "-", "1.2.3", "01", "1e", "1e+", "--5", etc. This rejects them:
    //   number = [ "-" ] int [ frac ] [ exp ]
    //   int    = "0" / ( digit1-9 *digit )      ; no leading zeros
    //   frac   = "." 1*digit                    ; non-empty
    //   exp    = ("e" / "E") ["+" / "-"] 1*digit; non-empty
    // peek() returns '\0' past end, so the range tests below are bounds-safe.
    bool parseNumber()
    {
        const std::size_t start = i_;
        // optional leading minus
        if (peek() == '-') ++i_;
        // integer part
        if (peek() == '0') {
            ++i_;
            // no leading zeros: "0" is ok, "01"/"00" are not
            if (peek() >= '0' && peek() <= '9') return false;
        } else if (peek() >= '1' && peek() <= '9') {
            ++i_;
            while (peek() >= '0' && peek() <= '9') ++i_;
        } else {
            return false;  // no integer digits (also rejects a lone '-')
        }
        // optional fraction: '.' then at least one digit
        if (peek() == '.') {
            ++i_;
            if (!(peek() >= '0' && peek() <= '9')) return false;
            while (peek() >= '0' && peek() <= '9') ++i_;
        }
        // optional exponent: e/E, optional sign, then at least one digit
        if (peek() == 'e' || peek() == 'E') {
            ++i_;
            if (peek() == '+' || peek() == '-') ++i_;
            if (!(peek() >= '0' && peek() <= '9')) return false;
            while (peek() >= '0' && peek() <= '9') ++i_;
        }
        return i_ > start;
    }

    bool parseString(std::string* out)
    {
        if (peek() != '"') return false;
        ++i_;
        std::string acc;
        // Wipe the accumulator UNCONDITIONALLY on every exit — including the
        // malformed-input `return false` paths below, which previously left a
        // partially-decoded (possibly token-bearing) `acc` unwiped (finding #3).
        ScopedWipe accWipe(acc);
        while (i_ < s_.size()) {
            const unsigned char c = static_cast<unsigned char>(s_[i_]);
            // Move (not copy) the decoded value into the caller's buffer; accWipe
            // then wipes the moved-from accumulator.
            if (c == '"') { ++i_; if (out) *out = std::move(acc); return true; }
            if (c == '\\') {
                ++i_;
                if (i_ >= s_.size()) return false;
                const char e = s_[i_];
                switch (e) {
                    case '"':  acc += '"';  break;
                    case '\\': acc += '\\'; break;
                    case '/':  acc += '/';  break;
                    case 'b':  acc += '\b'; break;
                    case 'f':  acc += '\f'; break;
                    case 'n':  acc += '\n'; break;
                    case 'r':  acc += '\r'; break;
                    case 't':  acc += '\t'; break;
                    case 'u': {
                        if (i_ + 4 >= s_.size()) return false;
                        unsigned int cp = 0;
                        for (int k = 1; k <= 4; ++k) {
                            const char h = s_[i_ + k];
                            cp <<= 4;
                            if (h >= '0' && h <= '9') cp |= (h - '0');
                            else if (h >= 'a' && h <= 'f') cp |= (h - 'a' + 10);
                            else if (h >= 'A' && h <= 'F') cp |= (h - 'A' + 10);
                            else return false;
                        }
                        i_ += 4;
                        // Keep it simple + fail-closed downstream: only pass through
                        // ASCII; any non-ASCII \u becomes a byte the session-JSON
                        // safety check will reject.
                        acc += (cp <= 0x7f) ? static_cast<char>(cp) : '\x01';
                        break;
                    }
                    default: return false;
                }
                ++i_;
                continue;
            }
            if (c < 0x20) return false;  // raw control char in string = malformed
            acc += static_cast<char>(c);
            ++i_;
        }
        return false;  // unterminated
    }

    bool parseArray()
    {
        if (peek() != '[') return false;
        ++i_;
        skipWs();
        if (peek() == ']') { ++i_; return true; }
        for (;;) {
            if (!parseValue(nullptr, nullptr)) return false;
            skipWs();
            const char c = peek();
            if (c == ',') { ++i_; continue; }
            if (c == ']') { ++i_; return true; }
            return false;
        }
    }

    bool parseObject(std::vector<StrictMember>* members)
    {
        if (peek() != '{') return false;
        ++i_;
        skipWs();
        if (peek() == '}') { ++i_; return true; }
        for (;;) {
            skipWs();
            std::string key;
            ScopedWipe keyWipe(key);    // wiped at end of every iteration/return
            if (!parseString(&key)) return false;
            skipWs();
            if (peek() != ':') return false;
            ++i_;
            int vtype = 0;
            std::string vstr;
            ScopedWipe vstrWipe(vstr);  // the value may be a token — wipe it too
            if (!parseValue(&vtype, &vstr)) return false;
            if (members) {
                for (const StrictMember& m : *members) {
                    if (m.key == key) return false;  // duplicate top-level key
                }
                StrictMember m;
                m.key = key;
                m.type = vtype;
                // MOVE (not copy) the value into the member — the recorded copy is
                // wiped by MembersWipe in parseActivateResponseStrict (finding #3).
                m.str = std::move(vstr);
                members->push_back(std::move(m));
            }
            skipWs();
            const char c = peek();
            if (c == ',') { ++i_; continue; }
            if (c == '}') { ++i_; return true; }
            return false;
        }
    }
};

const StrictMember* findMember(const std::vector<StrictMember>& members, const char* key)
{
    for (const StrictMember& m : members) {
        if (m.key == key) return &m;
    }
    return nullptr;
}

} // namespace

bool parseActivateResponseStrict(const std::string& json, ActivateVerdict& out)
{
    if (json.empty() || json.size() > 65536) return false;
    std::vector<StrictMember> members;
    // Wipe every parsed member value (any could be the token/token_id) on EVERY
    // exit path, including the reject paths (Codex finding #3).
    struct MembersWipe {
        std::vector<StrictMember>& m;
        ~MembersWipe() { for (StrictMember& x : m) secureWipe(x.str); }
    } membersWipe{members};

    StrictJson parser(json);
    if (!parser.parseDocument(&members)) return false;

    ActivateVerdict v;

    // ok / success must be booleans when present.
    for (const char* okKey : {"ok", "success"}) {
        if (const StrictMember* m = findMember(members, okKey)) {
            if (m->type == 2) v.ok = true;
            else if (m->type == 3) { /* false */ }
            else return false;  // present but not a bool -> reject
        }
    }
    // token / access_token / tid / token_id / error must be strings when present.
    auto strField = [&](const char* key, std::string& dst) -> bool {
        if (const StrictMember* m = findMember(members, key)) {
            if (m->type != 1) return false;  // present but not a string -> reject
            dst = m->str;
        }
        return true;
    };
    std::string token, accessToken, tid, tokenId2;
    // RAII-wipe every intermediate token copy on EVERY exit, including the reject
    // paths below where an earlier field was already copied out (finding #3, round 2).
    ScopedWipe tokenWipe(token);
    ScopedWipe accessTokenWipe(accessToken);
    ScopedWipe tidWipe(tid);
    ScopedWipe tokenId2Wipe(tokenId2);
    if (!strField("token", token))         return false;
    if (!strField("access_token", accessToken)) return false;
    if (!strField("tid", tid))             return false;
    if (!strField("token_id", tokenId2))   return false;
    if (!strField("error", v.error))       return false;

    v.token = !token.empty() ? token : accessToken;
    v.tokenId = !tid.empty() ? tid : tokenId2;
    // MOVE the verdict into the caller's out (which the caller wipes after the
    // DPAPI write); the moved-from local `v` is then wiped defensively.
    out = std::move(v);
    secureWipe(v.token); secureWipe(v.tokenId);
    return true;
}

ActivateVerdict parseActivateResponse(const std::string& json)
{
    ActivateVerdict v;
    if (!parseActivateResponseStrict(json, v)) {
        v = ActivateVerdict{};
        v.ok = false;
    }
    return v;
}

ActivateVerdict decideActivateVerdict(long httpStatus, const std::string& body)
{
    ActivateVerdict v;
    ActivateVerdict parsed;
    // Wipe the intermediate verdict's secrets on every exit (Codex finding #3);
    // the returned `v` is the caller's to wipe.
    struct VerdictWipe {
        ActivateVerdict& p;
        ~VerdictWipe() { secureWipe(p.token); secureWipe(p.tokenId); }
    } verdictWipe{parsed};
    if (!parseActivateResponseStrict(body, parsed)) {
        v.ok = false;
        v.error = "malformed_response";
        return v;
    }
    // Never accept ok:true from a non-200 response.
    if (httpStatus != 200) {
        v.ok = false;
        v.error = parsed.error.empty() ? ("http_" + std::to_string(httpStatus)) : parsed.error;
        return v;
    }
    v = parsed;
    // Success requires ok:true AND a usable token + token_id.
    if (v.ok && (v.token.empty() || v.tokenId.empty())) {
        v.ok = false;
        if (v.error.empty()) v.error = "malformed_response";
    }
    if (!v.ok && v.error.empty()) {
        v.error = "server_declined";
    }
    return v;
}

bool finalizeActivateResponse(bool readOk, long httpStatus, const std::string& body,
                              ActivateVerdict& out)
{
    if (!readOk) {
        // Truncated/failed read == transport failure. NEVER parse the partial body.
        secureWipe(out.token);
        secureWipe(out.tokenId);
        out = ActivateVerdict{};
        out.ok = false;
        return false;
    }
    out = decideActivateVerdict(httpStatus, body);
    return true;
}

std::string customerMessageForError(const std::string& code, bool transportFailed)
{
    if (transportFailed) {
        return "Can't reach Venice servers. Check your connection and Retry.";
    }
    auto is = [&](const char* c) { return code == c; };

    if (is("license_revoked") || is("license_inactive") || is("license_expired") ||
        is("subscription_required") || is("token_expired") || is("invalid_key") ||
        is("license_invalid_status") || is("license_record_invalid") ||
        is("frozen") || is("blacklisted")) {
        return "Your Venice access is not active. Subscribe at zaeorion.com or open a ticket.";
    }
    if (is("pair_invalid") || is("discord_signin_required")) {
        return "Your Discord sign-in link expired or was already used. Reconnect at zaeorion.com/connect, then Retry.";
    }
    if (is("machine_mismatch") || is("device_mismatch") ||
        is("device_limit_reached") || is("trial_used")) {
        return "This subscription is linked to another PC. Use /hwid_reset, then Retry.";
    }
    if (is("build_revoked") || is("build_expired") || is("build_not_found") ||
        is("version_blocked")) {
        return "This Venice build is no longer available. Update Venice.";
    }
    if (is("rate_limited")) {
        return "Too many launch attempts. Wait one minute, then Retry.";
    }
    if (is("service_disabled")) {
        return "Venice is temporarily unavailable. Try again shortly.";
    }
    return "Venice could not validate this installation. Update Venice or open a ticket.";
}

BrokerActionPlan planBrokerActions(BrokerInvocation invocation, bool selfTrusted)
{
    BrokerActionPlan plan;
    if (!selfTrusted) {
        // FIX A fail-closed: an unverified broker performs NO privileged action on
        // ANY path. Every side-effect flag stays clear and the process exits
        // nonzero. This is the single decision wWinMain consults before it touches
        // the registry, the network, DPAPI, or CreateProcess.
        plan.blocked = true;
        plan.exitCode = kBrokerUntrustedExit;
        return plan;
    }
    // Genuine broker: it owns the orion:// handler, so registration is permitted on
    // every entry path (idempotent HKCU write); then the per-mode action runs.
    plan.registerHandler = true;
    switch (invocation) {
    case BrokerInvocation::Register:
        // Installer hook: register (above) and exit success.
        plan.exitCode = 0;
        break;
    case BrokerInvocation::UriActivation:
        plan.runActivation = true;
        break;
    case BrokerInvocation::SessionLaunch:
        plan.launchOrPrompt = true;
        break;
    }
    return plan;
}

} // namespace broker
} // namespace orion
