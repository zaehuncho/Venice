#pragma once

#include <string>

// [SERVER-SHARD blocker #5] Pure, I/O-free contract helpers for the OrionActivate
// broker. These mirror the LIVE contracts extracted 2026-09-19:
//   * request shape  -> native_orion/src/LicenseClient.cpp (licenseActivateRequestBody
//                       / postActivate) and NetworkSecurity.cpp (host).
//   * DPAPI plaintext-> Lethe/bootstrap/shard_bootstrap.c (read_session_credential,
//                       json_string_field, is_hex_string).
// Kept free of Qt/Win32 so both the broker and OrionActivateContractTests compile
// the SAME code and the tests can assert byte-exact conformance.
namespace orion {
namespace broker {

// Host + activate path. NOTE: the path is /api/license/redeem, NOT /api/activate:
// Cloudflare's free-plan WordPress managed rule 403-blocks any path containing
// "activate" at the edge, and the Lambda aliases redeem -> handle_activate().
// This mirrors LicenseClient::postActivate; the build spec's literal
// "POST /api/activate" is the logical endpoint, redeem is the wire path.
extern const char*    kApiHostAscii;      // "api.zaeorion.com"
extern const wchar_t* kApiHostW;          // L"api.zaeorion.com"
extern const wchar_t* kActivatePathW;     // L"/api/license/redeem"
extern const wchar_t* kUserAgentPrefixW;  // L"OrionLauncher/"

// Mirrors shard_bootstrap.c:json_string_field — naive substring scan for
// "field", skip ws/colon, read a quoted value, reject any char <0x21, >0x7e, or
// '\'. Returns "" if absent/invalid.
std::string jsonStringField(const std::string& json, const std::string& field);

// Mirrors shard_bootstrap.c:is_hex_string(value, len).
bool isHexString(const std::string& value, size_t len);

// True iff the JSON has "ok":true or "success":true (whitespace-tolerant).
bool jsonBoolTrue(const std::string& json, const std::string& field);

// Activation request BODY. Mirrors licenseActivateRequestBody: license_key + key
// (both = pairCode), machine_id, client_version, fingerprint_version:2,
// request_nonce, request_timestamp. When `sessionOnly` (default), also emits
// "session_only":true so the backend omits canonical_license_key / license
// material from the response (Codex finding #1): the broker needs only the
// session token/token_id and never the canonical key.
std::string buildActivateBody(const std::string& pairCode,
                              const std::string& machineId,
                              const std::string& clientVersion,
                              const std::string& requestNonce,
                              long long requestTimestamp,
                              bool sessionOnly = true);

// Activation request HEADERS (CRLF-terminated block). Mirrors postActivate:
// Content-Type, Accept, User-Agent, X-Orion-Request-Nonce/-Timestamp/-Id.
std::string buildActivateHeaders(const std::string& clientVersion,
                                 const std::string& requestNonce,
                                 long long requestTimestamp,
                                 const std::string& requestId);

// DPAPI plaintext, read by shard_bootstrap.c:read_session_credential. The field
// is spelled "token_id" (the activate response spells it "tid" — the broker
// translates). Values must be printable-ASCII with no backslash or the bootstrap
// parser rejects them; buildSessionJson refuses to emit an unsafe value.
// Returns "" if any value is unsafe.
std::string buildSessionJson(const std::string& token,
                             const std::string& tokenId,
                             const std::string& machineId);

struct ActivateVerdict {
    bool ok = false;
    std::string token;    // "token" / "access_token"
    std::string tokenId;  // "tid" / "token_id"
    std::string error;    // server error code, if any
};

// STRICT response parser (Codex finding #4). Validates that `json` is exactly ONE
// well-formed top-level JSON object with NO duplicate top-level keys, then reads
// the verdict from TOP-LEVEL members ONLY, requiring each expected field to have
// the expected scalar type (ok/success: bool; token/access_token/tid/token_id/
// error: string). Unknown members (e.g. expires, profile) are validated for
// well-formedness but ignored. Returns false (out untouched) on any malformed,
// duplicate-key, non-object, trailing-garbage, or type-mismatched input. This
// replaces the old substring scan, which a nested/duplicate "token" could fool.
bool parseActivateResponseStrict(const std::string& json, ActivateVerdict& out);

// Back-compat wrapper: strict-parse and return the verdict (ok=false on reject).
ActivateVerdict parseActivateResponse(const std::string& json);

// Fail-closed verdict decision from the HTTP status AND body together (Codex
// finding #4): success requires HTTP 200 AND ok:true AND a usable token+token_id.
// A non-200 never yields ok=true even if the body says so; a malformed body is
// rejected. Never returns a usable verdict from a status/ body it did not fully
// validate.
ActivateVerdict decideActivateVerdict(long httpStatus, const std::string& body);

// Fail-closed transport/verdict finalization (Codex finding #2, round 2). `readOk`
// is whether the response body was read COMPLETELY and cleanly. When false (a
// WinHttpReadData/DataAvailable failure or an over-cap body), this is a TRANSPORT
// failure: `out` is reset to a not-ok verdict, the (partial) `body` is NEVER
// parsed, and the function returns false. When true, `out` is decided from
// (httpStatus, body) via decideActivateVerdict and the function returns true
// (transport succeeded — the verdict itself may still be a server decline). This
// is the exact decision the broker's read loop makes, factored out so it is
// unit-testable without a live WinHTTP handle.
bool finalizeActivateResponse(bool readOk, long httpStatus, const std::string& body,
                              ActivateVerdict& out);

// Map a server error code (or a transport/TLS failure) to stable customer text
// (design D6, extended for the activate-side codes).
std::string customerMessageForError(const std::string& errorCode, bool transportFailed);

// ── Broker self-trust enforcement gate (FIX A — 2026-09-20 hardening round 3) ──
// The broker must prove it is a genuine, tamper-checked server-shard broker BEFORE
// it performs ANY privileged action, on EVERY entry path. wWinMain evaluates the
// self-trust verdict ONCE (orion::genuineBrokerInstallPresent on the broker's OWN
// executable) and consults planBrokerActions() FIRST — before it touches the
// registry, the network, DPAPI, or launches anything. When the verdict is false
// the plan is `blocked` with a NONZERO exit and every side-effect flag clear, so
// no registry write, no WinHTTP request, no DPAPI/session write and no
// CreateProcess can occur. Kept pure (no Qt/Win32) so the enforcement is unit-
// testable without a live GUI/network: each flag maps 1:1 to a side effect in
// wWinMain, so asserting the flags are clear is a faithful proof of fail-closed.

// Nonzero process exit code returned when the broker is not a genuine install.
inline constexpr int kBrokerUntrustedExit = 9;

enum class BrokerInvocation {
    Register,       // OrionActivate.exe --register  (installer registration hook)
    UriActivation,  // orion://activate?code=PAIR-... (protocol activation)
    SessionLaunch,  // no flag / no URI (Start-menu/shortcut): launch or prompt
};

struct BrokerActionPlan {
    bool blocked = false;         // self-trust failed → perform NOTHING; show D6; exit
    bool registerHandler = false; // may write the orion:// HKCU registry keys
    bool runActivation = false;   // may parse the URI, WinHTTP-activate, DPAPI-write, launch
    bool launchOrPrompt = false;  // may launch an existing session or show the connect prompt
    int  exitCode = 0;            // process exit code (nonzero only when blocked)
};

// Pure decision consumed by wWinMain. `selfTrusted` is the verdict from
// orion::genuineBrokerInstallPresent(THIS exe). When false, EVERY invocation is
// blocked (no side effects, nonzero exit). When true, exactly the flag for the
// invocation's action is set; registration is also permitted on every path because
// a genuine broker owns the orion:// handler.
BrokerActionPlan planBrokerActions(BrokerInvocation invocation, bool selfTrusted);

} // namespace broker
} // namespace orion
