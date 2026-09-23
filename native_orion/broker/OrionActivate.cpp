/*
 * OrionActivate - Venice activation broker (server-shard blocker #5).
 *
 * A small, UNPACKED, code-signed-LATER helper that runs BEFORE the packed
 * payload on a clean install. It:
 *   1. Registers itself as the orion:// protocol handler (HKCU, no elevation).
 *   2. Parses orion://activate?code=PAIR-... (5-min single-use PAIR code).
 *   3. Derives machine_id via the SHARED translation unit (MachineIdentity.cpp)
 *      so it byte-matches SecurityManager::machineId() (design D3).
 *   4. POSTs the activation over TLS 1.2+ (WinHTTP, no redirects), mirroring
 *      LicenseClient::postActivate.
 *   5. On success writes {token,token_id,machine_id} as DPAPI (CurrentUser)
 *      atomically to %LOCALAPPDATA%\NexusVision\Orion Native\.vault\shard_session.dat
 *      with a current-user-only ACL - the record Lethe/bootstrap/shard_bootstrap.c
 *      reads.
 *   6. Launches the shipped OrionNative.exe (the Lethe bootstrap).
 *
 * NO offline fallback, NO silent unlock: any non-success fails closed with a
 * plain-English message and never writes a session or launches the app. Tokens
 * live only in the DPAPI blob; they are never logged, printed, or persisted
 * anywhere else, and every token buffer is SecureZeroMemory'd.
 *
 * Links: Qt6::Core (shared machine-id TU only), winhttp, crypt32, advapi32.
 */

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef UNICODE
#define UNICODE
#endif

#include <windows.h>
#include <winhttp.h>
#include <wincrypt.h>
#include <shellapi.h>
#include <objbase.h>

#include <ctime>
#include <string>
#include <vector>

#include <QtCore/QByteArray>
#include <QtCore/QCoreApplication>
#include <QtCore/QFileInfo>
#include <QtCore/QString>

#include "BrokerContract.h"
#include "BrokerInstallTrust.h"
#include "MachineIdentity.h"
#include "SessionStore.h"
#include "SpkiPin.h"

#pragma comment(lib, "winhttp.lib")
#pragma comment(lib, "crypt32.lib")
#pragma comment(lib, "advapi32.lib")

#ifndef ORION_NATIVE_VERSION
#define ORION_NATIVE_VERSION "0.0.0"
#endif

namespace {

using orion::broker::ActivateVerdict;

const wchar_t* kAppTitle = L"Venice";

// -- secret-wiping helpers ---------------------------------------------------
// RAII: SecureZeroMemory a std::string's storage on scope exit so tokens/PAIR
// codes/response bodies never linger in freed heap (Codex finding #3).
struct SecretWipe {
    std::string& s;
    explicit SecretWipe(std::string& str) : s(str) {}
    ~SecretWipe()
    {
        if (!s.empty()) SecureZeroMemory(&s[0], s.size());
    }
    SecretWipe(const SecretWipe&) = delete;
    SecretWipe& operator=(const SecretWipe&) = delete;
};

// -- string helpers ----------------------------------------------------------
std::wstring widen(const std::string& s)
{
    if (s.empty()) return std::wstring();
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), nullptr, 0);
    std::wstring w(n, L'\0');
    MultiByteToWideChar(CP_UTF8, 0, s.c_str(), (int)s.size(), &w[0], n);
    return w;
}

std::string narrow(const std::wstring& w)
{
    if (w.empty()) return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), nullptr, 0, nullptr, nullptr);
    std::string s(n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, w.c_str(), (int)w.size(), &s[0], n, nullptr, nullptr);
    return s;
}

void zeroString(std::string& s)
{
    if (!s.empty()) {
        SecureZeroMemory(&s[0], s.size());
    }
    s.clear();
}

void showMessage(const std::wstring& text, bool error)
{
    MessageBoxW(nullptr, text.c_str(), kAppTitle,
                MB_OK | (error ? MB_ICONERROR : MB_ICONINFORMATION));
}

// -- machine_id (shared TU - design D3) --------------------------------------
std::string machineIdUtf8()
{
    const QString id = orion::deriveMachineId();
    const QByteArray utf8 = id.toUtf8();
    return std::string(utf8.constData(), (size_t)utf8.size());
}

std::string newUuid()
{
    GUID g;
    if (CoCreateGuid(&g) != S_OK) {
        wchar_t buf[48];
        swprintf(buf, 48, L"%08x-%04x-%04x-fallback", (unsigned)GetTickCount(),
                 (unsigned)(GetCurrentProcessId() & 0xffff), (unsigned)(time(nullptr) & 0xffff));
        return narrow(buf);
    }
    wchar_t buf[48] = {};
    swprintf(buf, 48, L"%08lx-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x",
             g.Data1, g.Data2, g.Data3,
             g.Data4[0], g.Data4[1], g.Data4[2], g.Data4[3],
             g.Data4[4], g.Data4[5], g.Data4[6], g.Data4[7]);
    return narrow(buf);
}

// -- PAIR code extraction from orion://activate?code=... ----------------------
std::string uriParam(const std::string& uri, const std::string& key)
{
    const std::string needle = key + "=";
    size_t q = uri.find('?');
    if (q == std::string::npos) return {};
    size_t p = uri.find(needle, q);
    while (p != std::string::npos && p > 0) {
        char prev = uri[p - 1];
        if (prev == '?' || prev == '&') break;
        p = uri.find(needle, p + 1);
    }
    if (p == std::string::npos) return {};
    p += needle.size();
    size_t end = uri.find_first_of("&#", p);
    std::string v = uri.substr(p, end == std::string::npos ? std::string::npos : end - p);
    while (!v.empty() && (v.back() == ' ' || v.back() == '"' || v.back() == '\r' || v.back() == '\n')) {
        v.pop_back();
    }
    return v;
}

// PAIR-XXXXXXXX... : "PAIR-" + 32 chars from the server's charset.
bool looksLikePairCode(const std::string& code)
{
    if (code.rfind("PAIR-", 0) != 0 || code.size() != 37) return false;
    static const std::string charset = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
    for (size_t i = 5; i < code.size(); ++i) {
        if (charset.find(code[i]) == std::string::npos) return false;
    }
    return true;
}

// -- issuer-SPKI certificate pin (Codex finding #1) --------------------------
// After the TLS handshake (post-WinHttpReceiveResponse), pull the server cert,
// build its chain, and require that SOME cert in the chain carries a pinned
// issuer SPKI (orion::kApiPinnedSpkiSha256, shared with LicenseClient). WinHTTP's
// normal hostname + chain verification runs FIRST (default flags); this pin runs
// AFTER and narrows "any public CA" to the CAs Cloudflare issues for this zone.
// FAILS CLOSED: any failure to read/build/match aborts before the body is parsed.
bool verifyServerSpkiPin(HINTERNET request)
{
    PCCERT_CONTEXT leaf = nullptr;
    DWORD leafSize = sizeof(leaf);
    if (!WinHttpQueryOption(request, WINHTTP_OPTION_SERVER_CERT_CONTEXT,
                            &leaf, &leafSize) || !leaf) {
        return false;
    }

    bool matched = false;
    PCCERT_CHAIN_CONTEXT chain = nullptr;
    CERT_CHAIN_PARA chainPara = {};
    chainPara.cbSize = sizeof(chainPara);

    if (CertGetCertificateChain(nullptr, leaf, nullptr, leaf->hCertStore,
                                &chainPara, 0, nullptr, &chain) &&
        chain && chain->cChain > 0 && chain->rgpChain[0]) {
        std::vector<std::string> chainSpki;
        const CERT_SIMPLE_CHAIN* simple = chain->rgpChain[0];
        for (DWORD i = 0; i < simple->cElement; ++i) {
            PCCERT_CONTEXT cert = simple->rgpElement[i]->pCertContext;
            if (!cert || !cert->pCertInfo) continue;
            // DER-encode the SubjectPublicKeyInfo — the exact bytes RFC 7469-style
            // SPKI pins hash (same value Qt's QSslKey::toPem() body decodes to).
            DWORD der = 0;
            if (!CryptEncodeObjectEx(X509_ASN_ENCODING, X509_PUBLIC_KEY_INFO,
                                     &cert->pCertInfo->SubjectPublicKeyInfo,
                                     0, nullptr, nullptr, &der) || der == 0) {
                continue;
            }
            std::string blob(der, '\0');
            if (!CryptEncodeObjectEx(X509_ASN_ENCODING, X509_PUBLIC_KEY_INFO,
                                     &cert->pCertInfo->SubjectPublicKeyInfo, 0,
                                     nullptr, &blob[0], &der)) {
                continue;
            }
            blob.resize(der);
            chainSpki.push_back(std::move(blob));
        }
        matched = orion::broker::chainMatchesPinnedSpki(chainSpki);
    }

    if (chain) CertFreeCertificateChain(chain);
    CertFreeCertificateContext(leaf);
    return matched;
}

// -- WinHTTP activation (mirrors LicenseClient::postActivate) -----------------
// Returns true when a well-formed HTTP response was received (verdict filled);
// false on transport/TLS/pin failure.
bool postActivate(const std::string& pairCode, const std::string& machineId,
                  ActivateVerdict& verdict)
{
    const std::string clientVersion = ORION_NATIVE_VERSION;
    const std::string nonce = newUuid();
    const long long ts = (long long)time(nullptr);
    const std::string requestId = newUuid();

    std::string body = orion::broker::buildActivateBody(
        pairCode, machineId, clientVersion, nonce, ts, /*sessionOnly=*/true);
    SecretWipe bodyWipe(body);  // body carries the single-use PAIR code

    // Added request headers. The User-Agent rides as the WinHttpOpen session UA
    // to avoid a duplicate header; the full header CONTRACT incl. UA is pinned by
    // OrionActivateContractTests via buildActivateHeaders().
    std::string headers;
    headers += "Content-Type: application/json\r\n";
    headers += "Accept: application/json\r\n";
    headers += "X-Orion-Request-Nonce: " + nonce + "\r\n";
    headers += "X-Orion-Request-Timestamp: " + std::to_string(ts) + "\r\n";
    headers += "X-Orion-Request-Id: " + requestId + "\r\n";
    const std::wstring headersW = widen(headers);

    const std::wstring uaW = L"OrionLauncher/" + widen(clientVersion) + L" (Windows NT 10.0; Win64; x64)";

    bool transportOk = false;
    HINTERNET session = nullptr, connect = nullptr, request = nullptr;
    std::string response;
    // Preallocate a bounded buffer (finding #3): the body is capped at 64 KiB, so
    // reserving once up front means `response += chunk` never reallocates. A
    // reallocation would copy token-bearing bytes into a new buffer and free the
    // old one WITHOUT wiping it; with no growth, the single buffer is wiped by
    // zeroString(response) at `done`.
    response.reserve(66000);

    session = WinHttpOpen(uaW.c_str(), WINHTTP_ACCESS_TYPE_DEFAULT_PROXY,
                          WINHTTP_NO_PROXY_NAME, WINHTTP_NO_PROXY_BYPASS, 0);
    if (!session) goto done;
    WinHttpSetTimeouts(session, 10000, 10000, 15000, 15000);
    {
        // Fail closed if TLS 1.2+ cannot be pinned on the session (finding #4).
        DWORD proto = WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_2;
#ifdef WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3
        proto |= WINHTTP_FLAG_SECURE_PROTOCOL_TLS1_3;
#endif
        if (!WinHttpSetOption(session, WINHTTP_OPTION_SECURE_PROTOCOLS,
                              &proto, sizeof(proto))) {
            goto done;
        }
    }
    connect = WinHttpConnect(session, orion::broker::kApiHostW,
                             INTERNET_DEFAULT_HTTPS_PORT, 0);
    if (!connect) goto done;
    request = WinHttpOpenRequest(connect, L"POST", orion::broker::kActivatePathW,
                                 nullptr, WINHTTP_NO_REFERER,
                                 WINHTTP_DEFAULT_ACCEPT_TYPES, WINHTTP_FLAG_SECURE);
    if (!request) goto done;
    {
        // Fail closed if redirects cannot be disabled (finding #4): a permitted
        // redirect could move the request off the pinned host/path.
        DWORD redirect = WINHTTP_OPTION_REDIRECT_POLICY_NEVER;
        if (!WinHttpSetOption(request, WINHTTP_OPTION_REDIRECT_POLICY,
                              &redirect, sizeof(redirect))) {
            goto done;
        }
    }
    if (!WinHttpSendRequest(request, headersW.c_str(), (DWORD)-1L,
                            (LPVOID)body.data(), (DWORD)body.size(),
                            (DWORD)body.size(), 0)) {
        goto done;
    }
    if (!WinHttpReceiveResponse(request, nullptr)) goto done;

    // Enforce the issuer-SPKI pin BEFORE reading/parsing the body (finding #1).
    // A pin failure is a transport failure — never a server verdict.
    if (!verifyServerSpkiPin(request)) {
        goto done;
    }

    // A pinned response exists. A CLEAN, fully-read body yields a server verdict;
    // a truncated or failed read is a TRANSPORT failure (finding #2, round 2) — we
    // must never parse a partial or accumulated body.
    {
        DWORD status = 0, statusSize = sizeof(status);
        WinHttpQueryHeaders(request,
                            WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &status, &statusSize,
                            WINHTTP_NO_HEADER_INDEX);
        bool readOk = true;
        for (;;) {
            DWORD avail = 0;
            if (!WinHttpQueryDataAvailable(request, &avail)) { readOk = false; break; }
            if (avail == 0) break;  // clean end of body
            if (response.size() + avail > 65536) { readOk = false; break; }  // over cap -> reject
            std::string chunk(avail, '\0');
            DWORD read = 0;
            if (!WinHttpReadData(request, &chunk[0], avail, &read)) {
                if (!chunk.empty()) SecureZeroMemory(&chunk[0], chunk.size());
                readOk = false;  // transport failure: do NOT parse what we have
                break;
            }
            chunk.resize(read);
            response += chunk;  // capacity reserved up front -> no reallocation
            // Wipe the chunk copy before it is freed (finding #3).
            if (!chunk.empty()) SecureZeroMemory(&chunk[0], chunk.size());
            if (read == 0) break;  // defensive: no forward progress
        }
        // Fail-closed finalization (finding #2, round 2): a failed/truncated read
        // is a transport failure and the partial body is NEVER parsed; a clean read
        // yields the strict HTTP-200 + ok:true + token/token_id verdict (finding #4).
        transportOk = orion::broker::finalizeActivateResponse(
            readOk, (long)status, response, verdict);
    }

done:
    zeroString(response);
    if (request) WinHttpCloseHandle(request);
    if (connect) WinHttpCloseHandle(connect);
    if (session) WinHttpCloseHandle(session);
    return transportOk;
}

// -- DPAPI session persistence -----------------------------------------------
// The owner-only DACL, atomic write, rollback, and existence checks now live in
// broker/SessionStore.{h,cpp} so BrokerSessionStoreTests can exercise them
// headlessly against a temp path. Use orion::broker::writeSession / removeSession
// / sessionExists below.

// -- orion:// handler registration (mirrors main.cpp; HKCU, no elevation) -----
bool setRegDefault(HKEY root, const std::wstring& subkey, const std::wstring& value,
                   bool markUrlProtocol)
{
    HKEY k = nullptr;
    if (RegCreateKeyExW(root, subkey.c_str(), 0, nullptr, 0, KEY_WRITE, nullptr, &k, nullptr) != ERROR_SUCCESS) {
        return false;
    }
    LONG r = RegSetValueExW(k, nullptr, 0, REG_SZ, (const BYTE*)value.c_str(),
                            (DWORD)((value.size() + 1) * sizeof(wchar_t)));
    if (r == ERROR_SUCCESS && markUrlProtocol) {
        RegSetValueExW(k, L"URL Protocol", 0, REG_SZ, (const BYTE*)L"", (DWORD)sizeof(wchar_t));
    }
    RegCloseKey(k);
    return r == ERROR_SUCCESS;
}

void registerProtocolHandler()
{
    wchar_t exe[MAX_PATH] = {};
    if (GetModuleFileNameW(nullptr, exe, MAX_PATH) == 0) return;
    std::wstring exeStr(exe);
    setRegDefault(HKEY_CURRENT_USER, L"Software\\Classes\\orion", L"URL:Orion Protocol", true);
    setRegDefault(HKEY_CURRENT_USER, L"Software\\Classes\\orion\\DefaultIcon", L"\"" + exeStr + L"\",0", false);
    setRegDefault(HKEY_CURRENT_USER, L"Software\\Classes\\orion\\shell\\open\\command",
                  L"\"" + exeStr + L"\" \"%1\"", false);
}

// -- launch the shipped bootstrap (OrionNative.exe beside the broker) ----------
bool launchBootstrap()
{
    wchar_t exe[MAX_PATH] = {};
    if (GetModuleFileNameW(nullptr, exe, MAX_PATH) == 0) return false;
    std::wstring dir(exe);
    size_t slash = dir.find_last_of(L"\\/");
    if (slash == std::wstring::npos) return false;
    std::wstring target = dir.substr(0, slash + 1) + L"OrionNative.exe";
    std::wstring cmd = L"\"" + target + L"\"";
    std::wstring dirOnly = dir.substr(0, slash);

    STARTUPINFOW si = {}; si.cb = sizeof(si);
    PROCESS_INFORMATION pi = {};
    if (!CreateProcessW(target.c_str(), &cmd[0], nullptr, nullptr, FALSE,
                        0, nullptr, dirOnly.c_str(), &si, &pi)) {
        return false;
    }
    CloseHandle(pi.hThread);
    CloseHandle(pi.hProcess);
    return true;
}

// Real trust validation for broker self-registration (Codex finding #2, round 2).
// The broker may only SEIZE the orion:// handler on a run when it can prove it is
// a GENUINE server-shard broker, by the SAME rule the packed app uses to decide
// whether to DEFER to it (native_orion/src/BrokerInstallTrust.cpp):
//   (A) this exe carries a valid Authenticode signature from Venice, OR
//   (B) this exe is byte-covered by a release_manifest.json whose detached Ed25519
//       signature verifies against the pinned production key.
// The old "is OrionNative.exe beside me?" sibling-existence check is GONE — a bare
// file next to a stray broker copy was never a trust signal. Fails closed on any
// error (including a missing libcrypto for the manifest gate).
bool runningFromVerifiedInstallRoot()
{
    wchar_t exe[MAX_PATH] = {};
    if (GetModuleFileNameW(nullptr, exe, MAX_PATH) == 0) return false;
    const QString selfPath = QString::fromWCharArray(exe);
    const QFileInfo fi(selfPath);
    const QString selfDir = fi.absolutePath();
#ifdef ORION_PRODUCTION_BUILD
    constexpr bool productionBuild = true;
    const QString testId;
    const QByteArray testKey;
#else
    constexpr bool productionBuild = false;
    // [FIX A] Dev/test override, CONSISTENT with the existing release-manifest
    // test-key override below (both are non-production-only escape hatches,
    // compiled out entirely under ORION_PRODUCTION_BUILD). It lets unit tests and
    // non-packaged dev runs — a broker with no signed release manifest and no
    // Authenticode signature — still exercise the activation flow. In a production
    // (manifest-covered) build this whole branch is gone, so the signed manifest is
    // the real requirement.
    if (qEnvironmentVariable("ORION_BROKER_DEV_TRUST_SELF").trimmed()
            == QLatin1String("1")) {
        return true;
    }
    const QString testId = qEnvironmentVariable("ORION_RELEASE_MANIFEST_TEST_PUBKEY_ID").trimmed();
    const QByteArray testKey = qgetenv("ORION_RELEASE_MANIFEST_TEST_PUBKEY_B64").trimmed();
#endif
    return orion::genuineBrokerInstallPresent(fi.absoluteFilePath(), selfDir,
                                              productionBuild, testId, testKey, nullptr);
}

int runActivation(const std::string& pairCode)
{
    if (!looksLikePairCode(pairCode)) {
        showMessage(L"That Venice sign-in link is not valid. Reconnect at "
                    L"zaeorion.com/connect and click the link again.", true);
        return 2;
    }

    std::string machineId = machineIdUtf8();
    if (!orion::broker::isHexString(machineId, 64)) {
        showMessage(L"Venice could not read this PC identity. Reboot and try "
                    L"again, or open a ticket.", true);
        return 3;
    }

    ActivateVerdict verdict;
    const bool transportOk = postActivate(pairCode, machineId, verdict);
    if (!transportOk || !verdict.ok) {
        const std::string msg = orion::broker::customerMessageForError(
            verdict.error, !transportOk);
        zeroString(verdict.token);
        zeroString(verdict.tokenId);
        showMessage(widen(msg), true);
        return 4;
    }

    std::string sessionJson = orion::broker::buildSessionJson(
        verdict.token, verdict.tokenId, machineId);
    // Tokens must not outlive the DPAPI write.
    zeroString(verdict.token);
    zeroString(verdict.tokenId);
    if (sessionJson.empty()) {
        zeroString(sessionJson);
        showMessage(L"Venice could not validate this installation. Update Venice "
                    L"or open a ticket.", true);
        return 5;
    }

    const bool wrote = orion::broker::writeSession(sessionJson, nullptr);
    zeroString(sessionJson);
    if (!wrote) {
        showMessage(L"Venice could not save your session on this PC. Check disk "
                    L"space/permissions and Retry.", true);
        return 6;
    }

    if (!launchBootstrap()) {
        // A written session with no running app is a half-finished activation:
        // roll it back so a failed launch never leaves a session behind
        // (Codex finding #4). removeSession() returns true only when NEITHER the
        // session file NOR its .tmp remains; if it could not fully delete, the
        // session is still on disk, so report that explicitly rather than claiming
        // a clean state (finding, round 2).
        if (!orion::broker::removeSession()) {
            showMessage(L"Venice could not start and could not remove the partial "
                        L"activation on this PC. Reinstall Venice or open a ticket.", true);
            return 8;
        }
        showMessage(L"Venice activated, but the launcher could not start. Open "
                    L"Venice from the Start menu.", true);
        return 7;
    }
    return 0;
}

} // namespace

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR, int)
{
    QCoreApplication app(__argc, __argv);

    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    std::string uri;
    bool registerOnly = false;
    if (argv) {
        for (int i = 1; i < argc; ++i) {
            std::wstring a(argv[i]);
            if (_wcsicmp(a.c_str(), L"--register") == 0) {
                registerOnly = true;
            } else if (a.rfind(L"orion://", 0) == 0) {
                uri = narrow(a);
            }
        }
        LocalFree(argv);
    }

    // Classify the entry mode from the ARGUMENTS ALONE (no filesystem/registry/
    // network probing yet) so the self-trust gate can run BEFORE anything is
    // touched. The no-flag/no-URI Start-menu/shortcut launch is SessionLaunch: it
    // either starts an existing session or shows the connect prompt (decided only
    // after the gate passes).
    orion::broker::BrokerInvocation invocation;
    if (registerOnly) {
        invocation = orion::broker::BrokerInvocation::Register;
    } else if (!uri.empty()) {
        invocation = orion::broker::BrokerInvocation::UriActivation;
    } else {
        invocation = orion::broker::BrokerInvocation::SessionLaunch;
    }

    // [FIX A — 2026-09-20 hardening round 3] SELF-TRUST GATE, FIRST, on EVERY path.
    // Evaluate whether THIS broker executable is a genuine, tamper-checked server-
    // shard broker — by calling the SHARED trust TU on the broker's OWN path
    // (runningFromVerifiedInstallRoot() == orion::genuineBrokerInstallPresent(self))
    // — BEFORE any registry write, WinHTTP request, DPAPI/session write, or
    // CreateProcess. If it is not genuine, perform NO privileged action and exit
    // NONZERO. Previously the manifest verdict only gated self-REGISTRATION while
    // --register registered unconditionally and the URI / session paths ran
    // regardless; now the gate covers ALL THREE modes.
    const bool selfTrusted = runningFromVerifiedInstallRoot();
    const orion::broker::BrokerActionPlan plan =
        orion::broker::planBrokerActions(invocation, selfTrusted);
    if (plan.blocked) {
        showMessage(L"Venice can't confirm this installation is genuine, so it "
                    L"won't run. Reinstall Venice from zaeorion.com or open a "
                    L"ticket.", true);
        return plan.exitCode; // nonzero (orion::broker::kBrokerUntrustedExit)
    }

    // Registration happens only when the plan permits it (a genuine broker on any
    // path, incl. the --register installer hook). A stray/tampered broker copy
    // never reaches here (plan.blocked above), so it can never seize the handler.
    if (plan.registerHandler) {
        registerProtocolHandler();
    }

    if (invocation == orion::broker::BrokerInvocation::Register) {
        return plan.exitCode; // installer hook: registration handled above.
    }

    // Protocol activation: orion://activate?code=PAIR-...  (accept legacy key=).
    if (plan.runActivation) {
        std::string code = uriParam(uri, "code");
        if (code.empty()) code = uriParam(uri, "key");
        SecretWipe uriWipe(uri);   // single-use PAIR code lives in the URI
        SecretWipe codeWipe(code);
        return runActivation(code);
    }

    // No URI (Start-menu/shortcut launch): if a session already exists, just
    // start the app; otherwise send the customer to connect. Fails closed - the
    // broker never fabricates a session.
    if (plan.launchOrPrompt) {
        if (orion::broker::sessionExists()) {
            if (!launchBootstrap()) {
                showMessage(L"Venice could not start. Reinstall or open a ticket.", true);
                return 7;
            }
            return 0;
        }
        showMessage(L"Connect your Discord account at zaeorion.com/connect, then "
                    L"click Launch Venice to activate this PC.", false);
        return 1;
    }

    return plan.exitCode;
}
