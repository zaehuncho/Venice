// ───────────────────────────────────────────────────────────────────────────
//  VeniceNetStubTests — drives the wave-1 stub through the public C ABI
// ───────────────────────────────────────────────────────────────────────────
//
//  Deliberately NOT a QtTest binary: VeniceNet.dll is Qt-free by contract and
//  this test proves the surface is drivable by a plain C/C++ client with
//  nothing on PATH but the DLL itself (which CMake colocates with this exe).
//
//  What is pinned here is the WAVE-2 CONTRACT as far as the stub can express
//  it: lifecycle refusals, argument validation, the snapshot ABI handshake,
//  configuration mirroring, and the register-primes-immediately callback rule.
//  See docs/VENICENET_API.md for the full behavioural contract.
// ───────────────────────────────────────────────────────────────────────────

#include "VeniceNet.h"

#include <cmath>
#include <cstdio>
#include <cstring>

namespace {

int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            ++g_failures;                                                        \
            std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); \
        }                                                                        \
    } while (0)

VeniceNetSnapshot makeSnapshotRequest()
{
    VeniceNetSnapshot snap;
    std::memset(&snap, 0xCD, sizeof(snap)); // poison: prove every field is written
    snap.struct_size = sizeof(VeniceNetSnapshot);
    return snap;
}

struct CallbackRecord {
    int calls = 0;
    VeniceNetSnapshot last{};
};

void recordCallback(const VeniceNetSnapshot* snapshot, void* user_data)
{
    auto* record = static_cast<CallbackRecord*>(user_data);
    ++record->calls;
    record->last = *snapshot; // must copy: pointer is only valid during the call
}

void testVersionSurface()
{
    CHECK(venicenet_abi_version() == VENICENET_ABI_VERSION);
    const char* version = venicenet_version();
    CHECK(version != nullptr);
    // Semver shape: digits '.' digits '.' digits (prerelease/build tags allowed after).
    int dots = 0;
    bool shapeOk = version != nullptr && version[0] >= '0' && version[0] <= '9';
    for (const char* p = version; shapeOk && *p != '\0' && *p != '-' && *p != '+'; ++p) {
        if (*p == '.') {
            ++dots;
        } else if (*p < '0' || *p > '9') {
            shapeOk = false;
        }
    }
    CHECK(shapeOk && dots == 2);
}

void testLifecycleRefusals()
{
    // Everything stateful must refuse before init.
    CHECK(venicenet_set_enabled(true) == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_set_target_delay_ms(165.0) == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_set_engage_policy(VENICENET_POLICY_DEAD_BALL)
          == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_set_offense(true) == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_set_live(false) == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_set_console_ip("192.168.137.100") == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_notify_shot_edge() == VENICENET_ERR_NOT_INITIALIZED);
    CHECK(venicenet_register_state_callback(&recordCallback, nullptr)
          == VENICENET_ERR_NOT_INITIALIZED);
    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_ERR_NOT_INITIALIZED);

    // Shutdown before init is a documented no-op.
    venicenet_shutdown();

    CHECK(venicenet_init() == VENICENET_OK);
    CHECK(venicenet_init() == VENICENET_ERR_ALREADY_INITIALIZED);
}

void testSnapshotHandshakeAndDefaults()
{
    // ABI handshake: wrong struct_size is refused without writing.
    VeniceNetSnapshot bad = makeSnapshotRequest();
    bad.struct_size = sizeof(VeniceNetSnapshot) - 4;
    CHECK(venicenet_snapshot(&bad) == VENICENET_ERR_ABI_MISMATCH);
    CHECK(venicenet_snapshot(nullptr) == VENICENET_ERR_INVALID_ARGUMENT);

    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.struct_size == sizeof(VeniceNetSnapshot));
    // Stub facts.
    CHECK(snap.driver_state == VENICENET_DRIVER_NOT_INSTALLED);
    CHECK(snap.armed == 0);
    CHECK(snap.applied_delay_ms == 0.0);
    CHECK(snap.buffer_depth == 0);
    CHECK(snap.court_ip_detected == 0);
    CHECK(snap.latency_p50_ms < 0.0);
    CHECK(snap.latency_p95_ms < 0.0);
    CHECK(snap.error_code == VENICENET_OK);
    CHECK(snap.error_text[0] == '\0');
    CHECK(snap.court_ip[0] == '\0');
    // Configuration defaults.
    CHECK(snap.enabled == 0);
    CHECK(snap.target_delay_ms == 0.0);
    CHECK(snap.engage_policy == VENICENET_POLICY_ALWAYS_ON);
    CHECK(snap.offense == 0);
    CHECK(snap.live_ball == 1);
}

void testConfigurationValidationAndMirroring()
{
    // Delay target: non-finite refused, finite clamped into [0, 300].
    CHECK(venicenet_set_target_delay_ms(std::nan("")) == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_target_delay_ms(INFINITY) == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_target_delay_ms(-25.0) == VENICENET_OK);
    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.target_delay_ms == 0.0);
    CHECK(venicenet_set_target_delay_ms(1000.0) == VENICENET_OK);
    snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.target_delay_ms == VENICENET_MAX_DELAY_MS);
    CHECK(venicenet_set_target_delay_ms(204.5) == VENICENET_OK);

    // Policy: out-of-range refused, valid mirrored.
    CHECK(venicenet_set_engage_policy(static_cast<VeniceNetEngagePolicy>(42))
          == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_engage_policy(VENICENET_POLICY_OFFENSE_DEFENSE) == VENICENET_OK);

    // Console IP: syntactic validation.
    CHECK(venicenet_set_console_ip("999.1.1.1") == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_console_ip("192.168.137") == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_console_ip("192.168.137.100.7") == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_console_ip("192.168.01.100") == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_console_ip("not an ip") == VENICENET_ERR_INVALID_ARGUMENT);
    CHECK(venicenet_set_console_ip("192.168.137.100") == VENICENET_OK);
    CHECK(venicenet_set_console_ip("") == VENICENET_OK);      // clear
    CHECK(venicenet_set_console_ip(nullptr) == VENICENET_OK); // clear (idempotent)

    // Session inputs + enable mirror through; the stub must stay disarmed.
    CHECK(venicenet_set_enabled(true) == VENICENET_OK);
    CHECK(venicenet_set_offense(true) == VENICENET_OK);
    CHECK(venicenet_set_live(false) == VENICENET_OK);
    CHECK(venicenet_notify_shot_edge() == VENICENET_OK);
    snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.enabled == 1);
    CHECK(snap.offense == 1);
    CHECK(snap.live_ball == 0);
    CHECK(snap.target_delay_ms == 204.5);
    CHECK(snap.engage_policy == VENICENET_POLICY_OFFENSE_DEFENSE);
    CHECK(snap.armed == 0);
    CHECK(snap.driver_state == VENICENET_DRIVER_NOT_INSTALLED);
}

void testCallbackContract()
{
    CallbackRecord record;
    // Registration primes immediately + synchronously with the current state.
    CHECK(venicenet_register_state_callback(&recordCallback, &record) == VENICENET_OK);
    CHECK(record.calls == 1);
    CHECK(record.last.enabled == 1);
    CHECK(record.last.target_delay_ms == 204.5);

    // A state change fires it again; a no-op set does not.
    CHECK(venicenet_set_target_delay_ms(210.0) == VENICENET_OK);
    CHECK(record.calls == 2);
    CHECK(record.last.target_delay_ms == 210.0);
    CHECK(venicenet_set_target_delay_ms(210.0) == VENICENET_OK);
    CHECK(record.calls == 2);

    // NULL unregisters; later changes are silent.
    CHECK(venicenet_register_state_callback(nullptr, nullptr) == VENICENET_OK);
    CHECK(venicenet_set_target_delay_ms(165.0) == VENICENET_OK);
    CHECK(record.calls == 2);
}

void testShutdownResetsEverything()
{
    venicenet_shutdown();
    CHECK(venicenet_set_enabled(true) == VENICENET_ERR_NOT_INITIALIZED);
    venicenet_shutdown(); // idempotent

    // Re-init starts from clean defaults, not the pre-shutdown configuration.
    CHECK(venicenet_init() == VENICENET_OK);
    VeniceNetSnapshot snap = makeSnapshotRequest();
    CHECK(venicenet_snapshot(&snap) == VENICENET_OK);
    CHECK(snap.enabled == 0);
    CHECK(snap.target_delay_ms == 0.0);
    CHECK(snap.engage_policy == VENICENET_POLICY_ALWAYS_ON);
    CHECK(snap.offense == 0);
    CHECK(snap.live_ball == 1);
    venicenet_shutdown();
}

} // namespace

int main()
{
    testVersionSurface();
    testLifecycleRefusals();
    testSnapshotHandshakeAndDefaults();
    testConfigurationValidationAndMirroring();
    testCallbackContract();
    testShutdownResetsEverything();

    if (g_failures != 0) {
        std::fprintf(stderr, "VeniceNetStubTests: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("VeniceNetStubTests: all checks passed\n");
    return 0;
}
