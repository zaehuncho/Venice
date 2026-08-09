#ifndef VENICENET_H
#define VENICENET_H

/* ───────────────────────────────────────────────────────────────────────────
 *  VeniceNet.dll — public C ABI
 * ───────────────────────────────────────────────────────────────────────────
 *
 *  In-process replacement for the NexusVisionSvc packet bridge (nexus_svc.py).
 *  OrionNative.exe loads this DLL via LoadLibrary at startup; the DLL owns the
 *  WinDivert handle lifecycle, the packet-delay queue with the 100 ms/s slew
 *  cap, court-IP detection, latency measurement and the arm/disarm state.  The
 *  app supplies configuration and possession context and consumes snapshots.
 *
 *  ABI RULES (read before editing)
 *  --------------------------------
 *  - extern "C" only.  No C++ types, no Qt, no STL in any export.
 *  - VeniceNetSnapshot is a fixed-layout struct with NO implicit padding: every
 *    field is naturally aligned by construction.  Append-only growth; the
 *    caller-initialised `struct_size` field is the version handshake.
 *  - Enum values are frozen once shipped.  Add, never renumber.
 *  - VENICENET_ABI_VERSION bumps on ANY breaking change; the client refuses a
 *    DLL whose venicenet_abi_version() differs from the one it compiled
 *    against.  venicenet_version() (semver) is informational.
 *
 *  The authoritative behavioural contract (threading, lifecycle, the
 *  driver/armed state machines, error semantics) is docs/VENICENET_API.md.
 *  Wave-2 agents implement against that document; this header is the surface.
 *
 *  WAVE 1: the implementation behind this header is a deterministic stub
 *  (driver_state = NotInstalled, armed = false, no packets are ever touched).
 *  Configuration setters record and mirror their inputs so client-side tests
 *  can drive the surface deterministically.
 * ─────────────────────────────────────────────────────────────────────────── */

#include <stdbool.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(VENICENET_BUILD_DLL)
#    define VENICENET_API __declspec(dllexport)
#  else
#    define VENICENET_API __declspec(dllimport)
#  endif
#else
#  define VENICENET_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Bump on any ABI-breaking change.  The client compares this against
 * venicenet_abi_version() before calling anything else. */
#define VENICENET_ABI_VERSION 1u

/* Informational semver of this header/DLL pairing. */
#define VENICENET_VERSION_STRING "0.1.0"

/* Hard safety clamp on the delay target, mirroring nexus_svc.py's
 * _METER_DELAY_HARD_MAX_MS.  venicenet_set_target_delay_ms() clamps into
 * [0, VENICENET_MAX_DELAY_MS]; it never errors on an out-of-band finite value.
 * [ORION_METER_DELAY_RANGE 2026-08-08] 300 -> 600 in lockstep with the app's
 * 100-600 ms slider band (AppConfigData::kMeterDelayMaxMs) and the service
 * caps.  A service still running the old binary clamps at its own 300. */
#define VENICENET_MAX_DELAY_MS 600.0

/* Structural anti-stutter guarantee, mirroring _METER_MAX_SLEW_MS_PER_S: the
 * applied delay never moves faster than this, no matter what the client
 * commands.  Informational — the DLL enforces it internally. */
#define VENICENET_MAX_SLEW_MS_PER_S 100.0

/* Capacity of VeniceNetSnapshot.error_text (including the NUL terminator). */
#define VENICENET_ERROR_TEXT_CAP 256

/* Capacity of VeniceNetSnapshot.court_ip: dotted-quad IPv4 + NUL. */
#define VENICENET_IP_TEXT_CAP 16

/* WinDivert driver / intercept-handle lifecycle.  Legal transitions are
 * documented in docs/VENICENET_API.md ("driver_state machine"). */
typedef enum VeniceNetDriverState {
    VENICENET_DRIVER_NOT_INSTALLED = 0, /* WinDivert service not registered   */
    VENICENET_DRIVER_NOT_STARTED   = 1, /* registered, driver not running     */
    VENICENET_DRIVER_READY         = 2, /* driver running, no handle open     */
    VENICENET_DRIVER_HANDLE_OPEN   = 3, /* intercept handle open              */
    VENICENET_DRIVER_ERROR         = 4  /* see snapshot error_code/error_text */
} VeniceNetDriverState;

/* Engagement policy — mirrors MeterDelayController::EngagePolicy exactly.
 * AlwaysOn is the default (see MeterDelayController.h for why a constant delay
 * is the only shot-safe configuration). */
typedef enum VeniceNetEngagePolicy {
    VENICENET_POLICY_ALWAYS_ON       = 0,
    VENICENET_POLICY_DEAD_BALL       = 1,
    VENICENET_POLICY_OFFENSE_DEFENSE = 2,
    VENICENET_POLICY_SHOT_GATED      = 3
} VeniceNetEngagePolicy;

/* Return status of every fallible export.  0 is success; everything else is a
 * refusal that left the DLL's state unchanged. */
typedef enum VeniceNetStatus {
    VENICENET_OK                       = 0,
    VENICENET_ERR_NOT_INITIALIZED      = 1, /* call venicenet_init() first     */
    VENICENET_ERR_ALREADY_INITIALIZED  = 2,
    VENICENET_ERR_INVALID_ARGUMENT     = 3,
    VENICENET_ERR_ABI_MISMATCH         = 4, /* snapshot struct_size mismatch   */
    VENICENET_ERR_DRIVER_NOT_INSTALLED = 5,
    VENICENET_ERR_DRIVER_ACCESS_DENIED = 6,
    VENICENET_ERR_DRIVER_OPEN_FAILED   = 7,
    VENICENET_ERR_INTERNAL             = 8
} VeniceNetStatus;

/* Point-in-time state feed.  Fixed layout, naturally aligned, no implicit
 * padding (asserted in the implementation).  Boolean facts are int32_t 0/1 so
 * the layout is compiler-independent.
 *
 * The caller MUST set struct_size = sizeof(VeniceNetSnapshot) before calling
 * venicenet_snapshot(); a mismatch returns VENICENET_ERR_ABI_MISMATCH instead
 * of writing anything. */
typedef struct VeniceNetSnapshot {
    uint32_t struct_size;        /* in: sizeof(VeniceNetSnapshot)             */
    int32_t  driver_state;       /* VeniceNetDriverState                      */
    double   applied_delay_ms;   /* what packets are experiencing NOW         */
    double   target_delay_ms;    /* what the client last commanded (clamped)  */
    double   latency_p50_ms;     /* court-flow latency; < 0 = no measurement  */
    double   latency_p95_ms;     /* < 0 = no measurement                      */
    uint32_t buffer_depth;       /* packets currently held in the delay queue */
    int32_t  error_code;         /* VeniceNetStatus of the last async fault;
                                    VENICENET_OK when healthy                 */
    int32_t  armed;              /* 1 = the intercept is holding/eligible to
                                    hold packets (see the armed state machine)*/
    int32_t  enabled;            /* mirrors venicenet_set_enabled()           */
    int32_t  engage_policy;      /* VeniceNetEngagePolicy, as configured      */
    int32_t  court_ip_detected;  /* 1 = a court server flow is identified     */
    int32_t  offense;            /* mirrors venicenet_set_offense()           */
    int32_t  live_ball;          /* mirrors venicenet_set_live()              */
    char     court_ip[VENICENET_IP_TEXT_CAP];     /* "" until detected        */
    char     error_text[VENICENET_ERROR_TEXT_CAP];/* UTF-8, NUL-terminated;
                                    "" when healthy.  Human-readable cause of
                                    error_code — safe to log verbatim         */
} VeniceNetSnapshot;

/* State-change callback.  `snapshot` is valid ONLY for the duration of the
 * call — copy it out.  May be invoked from an internal worker thread; the
 * receiver must marshal to its own thread before touching UI state, and must
 * NOT call back into any venicenet_* function from inside the callback. */
typedef void (*VeniceNetStateCallback)(const VeniceNetSnapshot* snapshot,
                                       void* user_data);

/* ── Version (callable at any time, including before init) ────────────────── */
VENICENET_API uint32_t venicenet_abi_version(void);
/* Static NUL-terminated semver string, e.g. "0.1.0".  Never NULL; owned by the
 * DLL; valid until FreeLibrary. */
VENICENET_API const char* venicenet_version(void);

/* ── Lifecycle ────────────────────────────────────────────────────────────── */
VENICENET_API VeniceNetStatus venicenet_init(void);
/* Idempotent; safe when never initialised.  Disarms, releases every held
 * packet in order, closes handles, forgets the callback, resets configuration
 * to defaults. */
VENICENET_API void venicenet_shutdown(void);

/* ── Configuration ────────────────────────────────────────────────────────── */
VENICENET_API VeniceNetStatus venicenet_set_enabled(bool enabled);
/* Finite values are clamped to [0, VENICENET_MAX_DELAY_MS] and accepted; NaN
 * and infinities are refused with VENICENET_ERR_INVALID_ARGUMENT.  Sets the
 * TARGET only — the applied delay slews toward it at
 * <= VENICENET_MAX_SLEW_MS_PER_S. */
VENICENET_API VeniceNetStatus venicenet_set_target_delay_ms(double target_ms);
VENICENET_API VeniceNetStatus venicenet_set_engage_policy(VeniceNetEngagePolicy policy);

/* ── Session / possession inputs from the app ─────────────────────────────── */
VENICENET_API VeniceNetStatus venicenet_set_offense(bool offense);
VENICENET_API VeniceNetStatus venicenet_set_live(bool live_ball);
/* Console (PS5) IPv4 in dotted-quad form; scopes court-IP detection and the
 * intercept filter.  NULL/empty clears it.  Syntactically invalid input is
 * refused with VENICENET_ERR_INVALID_ARGUMENT. */
VENICENET_API VeniceNetStatus venicenet_set_console_ip(const char* console_ipv4);
/* Physical shot edge (raw Square press / shot gesture).  Only consumed by
 * VENICENET_POLICY_SHOT_GATED; a no-op under every other policy. */
VENICENET_API VeniceNetStatus venicenet_notify_shot_edge(void);

/* ── State feed ───────────────────────────────────────────────────────────── */
/* Fills *out with the current state.  out->struct_size must equal
 * sizeof(VeniceNetSnapshot) on entry. */
VENICENET_API VeniceNetStatus venicenet_snapshot(VeniceNetSnapshot* out);
/* Registers the single state callback (replacing any previous one) and
 * immediately invokes it once, synchronously, with the current snapshot so the
 * receiver never starts blind.  NULL unregisters.  `user_data` is passed
 * through verbatim. */
VENICENET_API VeniceNetStatus venicenet_register_state_callback(
    VeniceNetStateCallback callback, void* user_data);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* VENICENET_H */
