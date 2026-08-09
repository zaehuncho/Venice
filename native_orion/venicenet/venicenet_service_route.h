#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  venicenet_service_route.h — where + how the DLL reaches the bridge service
// ───────────────────────────────────────────────────────────────────────────
//
//  WAVE 2B: VeniceNet.dll is a thin IPC client. The privileged engine lives in
//  a separate SYSTEM service (VeniceNetSvc.exe / NexusVisionSvc.exe — whichever
//  is registered). This header encapsulates the two things the client must know
//  at connect time so nothing else in the DLL hardcodes them:
//
//    1. The loopback endpoint. The EXISTING wire transport (nexus_svc.py, which
//       the wave-2A service ports byte-for-byte) is newline-delimited JSON over
//       a loopback TCP socket on 127.0.0.1:47291 with a per-session bearer-token
//       handshake. It is NOT a Windows named pipe; "the pipe" in the task brief
//       is this loopback channel. The port is what identifies the bridge, not
//       the service name, so an older NexusVisionSvc and a new VeniceNetSvc are
//       reached identically — whichever owns the port answers.
//
//    2. The bearer token. nexus_svc mints a per-session token and publishes it
//       where only the signed-in user can read it (a different directory for
//       service vs debug mode). This mirrors NetworkBridge::resolveBridgeToken,
//       Qt-free, so the DLL authenticates exactly as the app's old pipe client
//       did. A leftover token from a dead session is rejected by publisher-pid
//       liveness, never sent.
//
//  Everything here is Qt-free and depends on nothing but the CRT + Win32.
// ───────────────────────────────────────────────────────────────────────────

#include <cstdint>
#include <string>
#include <vector>

namespace venicenet {

// Loopback endpoint of the packet-bridge service. host is always the loopback
// address; port defaults to nexus_svc.py's LISTEN_PORT (47291).
struct ServiceEndpoint {
    std::string host = "127.0.0.1";
    uint16_t port = 47291;
};

// Resolve the endpoint. Honors the VENICENET_BRIDGE_PORT environment override
// (a decimal port) so a mock server can be driven on an ephemeral port in tests
// and dev harnesses; production uses the default 47291.
ServiceEndpoint resolveServiceEndpoint();

// Bearer-token selection result. `token` empty => no usable token was found and
// the client must not attempt auth. `diagnostic` is always populated, contains
// no secret, and is safe to log verbatim.
struct TokenSelection {
    std::string token;
    std::string path;
    std::string diagnostic;
};

// Reads the candidate token files (per-user LOCALAPPDATA first, then the
// service-mode PROGRAMDATA location), rejects records whose publishing process
// is provably gone, and returns the freshest live one (falling back to a
// pre-v1 bare token). Honors the VENICENET_BRIDGE_TOKEN environment override
// (a literal token) for tests/dev, which short-circuits file resolution.
TokenSelection resolveBridgeToken();

// Candidate registered service names, most-current first. Informational only:
// the loopback port is the real selector during the migration window, but the
// DLL logs which service names it would expect so a stale-install diagnosis is
// self-serving. VeniceNetSvc (wave-2A) is preferred; NexusVisionSvc (the Python
// bridge from older installs) must keep working.
std::vector<std::string> candidateServiceNames();

// ── Exposed for unit tests (pure, no I/O) ──────────────────────────────────
// Parse ONE token file body (a v1 JSON record or a pre-v1 bare token).
struct TokenRecord {
    std::string path;
    std::string token;
    long long   pid = 0;
    long long   startedMs = 0;
    bool        exists = false;      // a file body was present
    bool        structured = false;  // carried both a token and a pid
    bool        ownerAlive = true;   // false ONLY when the pid is provably gone
};
TokenRecord parseTokenRecord(const std::string& path, const std::string& body);

// Pure selection policy over already-gathered records (no filesystem, no
// process probing) — mirrors NetworkBridge::selectBridgeToken.
TokenSelection selectBridgeToken(const std::vector<TokenRecord>& records);

} // namespace venicenet
