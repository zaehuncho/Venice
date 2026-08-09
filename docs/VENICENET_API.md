# VeniceNet.dll — API contract (v0.1.0, ABI 1)

Status: **wave 1 — surface ratified, implementation is a stub.**
Header: `native_orion/venicenet/VeniceNet.h` (the only header a consumer may include).
Wave-2 agents implement the behaviour specified here behind the exact exported
surface; the app side codes against this document, not against the stub.

## What this DLL is

`VeniceNet.dll` is the in-process replacement for the `NexusVisionSvc` Python
packet bridge (`nexus_svc.py`). `OrionNative.exe` loads it with `LoadLibrary`
at startup and resolves every export with `GetProcAddress`. The DLL owns:

- the **WinDivert handle lifecycle** (dynamic link against `WinDivert64.dll`,
  LGPL-clean; the driver service is registered at install time with a
  permissive DACL via `sc sdset`, so no runtime elevation),
- the **packet-delay queue** with the structural 100 ms/s slew cap
  (`VENICENET_MAX_SLEW_MS_PER_S`) and the [0, 300] ms hard clamp
  (`VENICENET_MAX_DELAY_MS`),
- **court-IP detection** (identifying the public 2K court server flow),
- **latency measurement** (p50/p95 over the court flow),
- the **arm/disarm state**.

The app supplies configuration (enable, target delay, engage policy, console
IP) and possession context (offense, live ball, shot edge), and consumes
state via `venicenet_snapshot()` and the registered callback. There is no
socket, no bearer token, no keepalive protocol and no separate service
process: everything the nexus_svc auth/dead-man machinery defended against is
structurally gone because the client and the engine share a process.

## ABI ground rules

- Every export is `extern "C"`; no C++ types, no Qt, no STL cross the boundary.
- `VeniceNetSnapshot` is a fixed-layout struct with no implicit padding
  (offsets are `static_assert`-pinned in `VeniceNet.cpp`; total size 344).
  Growth is append-only and requires an ABI bump.
- Enum values are frozen once shipped; add, never renumber.
- `venicenet_abi_version()` is the compatibility handshake. The client MUST
  refuse to proceed when it differs from the `VENICENET_ABI_VERSION` it was
  compiled against. `venicenet_version()` (semver) is informational/logging.
- All strings crossing the boundary are NUL-terminated UTF-8.

## Exported symbols

### `uint32_t venicenet_abi_version(void)`

Returns `VENICENET_ABI_VERSION` (currently `1`).
Callable at any time, including before `venicenet_init()` and after
`venicenet_shutdown()`. Never fails. **Thread-safe** (pure).

### `const char* venicenet_version(void)`

Returns a static NUL-terminated semver string (e.g. `"0.1.0"`). Never `NULL`.
The pointer is owned by the DLL and valid until `FreeLibrary`. Callable at any
time. **Thread-safe** (pure).

### `VeniceNetStatus venicenet_init(void)`

Initialises the engine: internal state, worker thread(s), and the initial
driver probe (`driver_state` settles to `NOT_INSTALLED` / `NOT_STARTED` /
`READY` / `ERROR`; init itself succeeds even when the driver is absent — the
absence is reported through the snapshot, not as an init failure, so the app
can render an honest "not available" UI instead of dying).

- Returns `VENICENET_OK` on success.
- Returns `VENICENET_ERR_ALREADY_INITIALIZED` if already initialised (state
  unchanged).
- May return `VENICENET_ERR_INTERNAL` for unrecoverable setup failure.

Must be called before any other stateful export. Call once per process.
**Not thread-safe against itself or `venicenet_shutdown()`** — the app calls
both from one owner thread (see "Threading model").

### `void venicenet_shutdown(void)`

Tears the engine down, in this order: disarm (force the applied delay to 0),
**release every held packet in arrival order** (paced, never bursted), close
every WinDivert handle, stop worker threads, unregister the callback, reset
configuration to defaults. Idempotent; a call when never initialised is a
no-op. After it returns, no callback will ever fire again and every stateful
export answers `VENICENET_ERR_NOT_INITIALIZED`.

The wave-2 implementation must also attempt the **driver residency
guarantee**: after the last handle closes, request a driver stop and verify it
(mirrors `nexus_svc._unload_windivert_driver`). A refusal is logged, never
raised.

### `VeniceNetStatus venicenet_set_enabled(bool enabled)`

Master switch (maps to the app's `meterDelayEnabled`). Enabling starts the
engage pipeline: driver probe → handle open (when court + console are known)
→ arm per policy. Disabling disarms (slew to 0, release the backlog in
order, close the intercept handle) but keeps init'd state so re-enabling is
cheap.

- `VENICENET_OK` — accepted (including no-op re-sets).
- `VENICENET_ERR_NOT_INITIALIZED`.
- Driver problems are **not** reported here: enabling with a missing driver
  succeeds and the failure surfaces in `driver_state`/`error_code`. Rationale:
  the enable is a persisted user setting; the driver state is environmental.

### `VeniceNetStatus venicenet_set_target_delay_ms(double target_ms)`

Sets the delay **target**. Never changes the applied delay directly — the
engine slews the applied value toward the target at
≤ `VENICENET_MAX_SLEW_MS_PER_S` with a bounded burst allowance (token bucket,
100 ms accumulation cap), exactly the `nexus_svc.py` anti-stutter contract.

- Non-finite input (`NaN`, `±inf`) → `VENICENET_ERR_INVALID_ARGUMENT`,
  state unchanged.
- Finite input is **clamped** into `[0, VENICENET_MAX_DELAY_MS]` and accepted
  (`VENICENET_OK`). Clamping-not-erroring mirrors the service and keeps ramp
  intermediates flowing.
- `VENICENET_ERR_NOT_INITIALIZED`.

### `VeniceNetStatus venicenet_set_engage_policy(VeniceNetEngagePolicy policy)`

Selects when the delay engages. Mirrors `MeterDelayController::EngagePolicy`
one-to-one:

| value | name | behaviour |
|---|---|---|
| 0 | `ALWAYS_ON` | engage as soon as armed prerequisites hold; disengage only on session end. **Default.** |
| 1 | `DEAD_BALL` | wait for the first dead ball (`venicenet_set_live(false)`), then hold like AlwaysOn (latched). |
| 2 | `OFFENSE_DEFENSE` | engage on offense, disengage on defense (two ramps per possession — callers must respect the settled condition before trusting timing). |
| 3 | `SHOT_GATED` | engage on `venicenet_notify_shot_edge()`. Parity/experiment only: at the shipping slew it cannot settle before the meter renders. |

- Out-of-range value → `VENICENET_ERR_INVALID_ARGUMENT`.
- `VENICENET_ERR_NOT_INITIALIZED`.
- Changing policy while armed re-evaluates engagement on the next engine tick;
  any resulting delay change obeys the slew cap.

### `VeniceNetStatus venicenet_set_offense(bool offense)`
### `VeniceNetStatus venicenet_set_live(bool live_ball)`

Possession context from the app (mirror `MeterDelayController::setOffense` /
`setBallLive`). Consumed by `OFFENSE_DEFENSE` and `DEAD_BALL` respectively;
ignored (but still recorded and mirrored in the snapshot) under other
policies. Errors: `VENICENET_ERR_NOT_INITIALIZED` only.

### `VeniceNetStatus venicenet_set_console_ip(const char* console_ipv4)`

The console's (PS5's) IPv4, dotted-quad. Scopes court-IP detection and the
intercept filter (the inbound flow is `court_ip -> console_ip` on UDP ports
30000–30020). `NULL` or `""` clears it, which drops `court_ip_detected`,
disarms and closes the intercept handle.

- Syntactically invalid input → `VENICENET_ERR_INVALID_ARGUMENT` (strict
  dotted quad, four octets 0–255, no leading zeros).
- Wave 2 additionally enforces the semantic rules from
  `nexus_svc._validated_ipv4`: unicast, not loopback/multicast/reserved/
  unspecified. The detected **court** IP is always required to be globally
  routable — that is what structurally keeps Remote Play (LAN↔LAN) out of the
  intercept.
- An address **change** while intercepting closes and re-opens the intercept
  handle (mirrors `retarget`), releasing the backlog in order first.
- `VENICENET_ERR_NOT_INITIALIZED`.

### `VeniceNetStatus venicenet_notify_shot_edge(void)`

Raw physical shot edge (Square press / shot gesture), the causally-early
signal. Only `SHOT_GATED` consumes it (it latches an engagement window);
under every other policy it is accepted and ignored. Errors:
`VENICENET_ERR_NOT_INITIALIZED` only.

### `VeniceNetStatus venicenet_snapshot(VeniceNetSnapshot* out)`

Fills `*out` with a coherent point-in-time state. The caller MUST set
`out->struct_size = sizeof(VeniceNetSnapshot)` first.

- `out == NULL` → `VENICENET_ERR_INVALID_ARGUMENT`.
- `struct_size` mismatch → `VENICENET_ERR_ABI_MISMATCH`; nothing is written.
- `VENICENET_ERR_NOT_INITIALIZED`.
- Otherwise `VENICENET_OK`; every field written.

Field semantics:

- `driver_state` — see the state machine below.
- `applied_delay_ms` — what packets are experiencing right now (the slewed
  value). `target_delay_ms` — the last accepted (clamped) command. They
  differ while a ramp is in motion; a consumer that conflates them will
  misreport the timing condition (this is the service's
  applied-vs-target split, preserved).
- `buffer_depth` — packets currently held. 0 whenever not armed.
- `latency_p50_ms` / `latency_p95_ms` — court-flow latency percentiles;
  **negative means "no measurement"**, never 0 (0 is a real measurement).
- `error_code` / `error_text` — last asynchronous fault (handle open failure,
  recv/send hard error). `VENICENET_OK` + empty text when healthy. Sticky
  until the condition clears (a successful re-probe/re-open resets them).
- `armed` — see the state machine below.
- `enabled`, `engage_policy`, `offense`, `live_ball` — configuration mirror.
- `court_ip_detected` / `court_ip` — detection output (the public court
  server), NOT an echo of `venicenet_set_console_ip`.

### `VeniceNetStatus venicenet_register_state_callback(VeniceNetStateCallback callback, void* user_data)`

Registers the **single** state callback, replacing any previous registration;
`NULL` unregisters. On successful registration with a non-NULL callback the
DLL immediately invokes it **once, synchronously on the calling thread**, with
the current snapshot, so the receiver never starts blind.

After that, the callback fires on every observable state change: driver_state
transitions, arm/disarm, applied-delay milestones (engage settled, released to
0), court-IP detection changes, error onset/clear, and configuration changes.
Ramps are **coalesced** — the engine guarantees a callback at the start and at
the settle of a ramp but does not promise one per slew step.

- `VENICENET_ERR_NOT_INITIALIZED` (registration requires an initialised
  engine; shutdown unregisters implicitly).

Callback rules (binding on both sides):

1. The `snapshot` pointer is valid **only during the call** — copy it out.
2. Calls may arrive from an internal worker thread. The app marshals to its
   own thread (Qt: queued invocation) before touching UI or Qt state.
3. The callback MUST NOT call any `venicenet_*` function (the wave-2 engine
   may hold internal locks around the invocation; the stub already documents
   the prohibition so nobody grows a dependency on the stub's laxness).
4. Callbacks never fire after `venicenet_shutdown()` returns, and never
   before `venicenet_register_state_callback` returns.

## Threading model

- **Owner thread**: `venicenet_init`, `venicenet_shutdown` and
  `venicenet_register_state_callback` must be called from a single owner
  thread (the app's main thread). They are not re-entrant and not safe to
  race against each other.
- **Any thread, after init**: every setter, `venicenet_notify_shot_edge` and
  `venicenet_snapshot` are thread-safe and may be called concurrently from
  any thread (the input poll thread feeds the shot edge at 4 ms cadence; the
  GUI thread feeds settings). Internally a single mutex serialises them.
- **Callback thread**: unspecified — may be the calling thread (registration
  prime, stub) or an internal worker (wave 2). Consumers must not assume.
- Calls concurrent WITH `venicenet_shutdown` are the app's bug; the app stops
  its feeders first. The engine still promises memory-safety (no UAF) for a
  straggler snapshot call racing shutdown, returning
  `VENICENET_ERR_NOT_INITIALIZED` when it loses.

## Lifecycle constraints

1. `venicenet_abi_version` / `venicenet_version` — callable always.
2. Everything else requires `venicenet_init()` first; before it, the answer is
   exactly `VENICENET_ERR_NOT_INITIALIZED` (never a crash).
3. After `venicenet_shutdown()`, the same refusal applies; `init` may be
   called again (fresh defaults — shutdown forgets configuration).
4. `FreeLibrary` only after `venicenet_shutdown()` returned.
5. Configuration is NOT persisted by the DLL. The app owns persistence
   (AppConfig) and re-feeds it after every init — same split as
   `applyMeterDelayRuntimeConfig()` today.

## `driver_state` machine

States: `NOT_INSTALLED(0)`, `NOT_STARTED(1)`, `READY(2)`, `HANDLE_OPEN(3)`,
`ERROR(4)`.

Legal transitions and their triggers:

| from | to | trigger (who) |
|---|---|---|
| `NOT_INSTALLED` | `NOT_STARTED` | re-probe finds the driver service registered (engine; the installer registered it out-of-band) |
| `NOT_STARTED` | `READY` | driver starts — normally as a side effect of the engine's first open attempt on a demand-start service (engine) |
| `READY` | `HANDLE_OPEN` | intercept handle opened: `enabled` && console IP set && court flow identified (engine) |
| `HANDLE_OPEN` | `READY` | intercept handle closed: disable, console-IP clear/change (reopen follows), policy disengage-to-idle, disarm (engine) |
| `READY` | `NOT_STARTED` | driver stopped externally, detected on probe (engine observes) |
| any | `ERROR` | open/recv/send hard failure, access denied, probe failure (engine; `error_code`/`error_text` say why) |
| `ERROR` | `NOT_INSTALLED` / `NOT_STARTED` / `READY` | periodic re-probe succeeds while `enabled` (engine; backoff ≥ 5 s, mirroring `_WD_RETRY_DELAY`) |

`NOT_INSTALLED` is terminal from the DLL's point of view — only the installer
can leave it; the engine merely re-probes. No transition ever skips the probe:
`NOT_INSTALLED → HANDLE_OPEN` directly is illegal.

Wave-1 stub: pinned at `NOT_INSTALLED` forever.

## `armed` machine

`armed` is a boolean with strict entry/exit conditions, not a free-running
flag:

**false → true** (engine only) requires ALL of:
- `driver_state == HANDLE_OPEN`,
- `enabled == 1`,
- `court_ip_detected == 1`,
- the engage policy's own condition holds (`ALWAYS_ON`: immediately;
  `DEAD_BALL`: first `live_ball == 0`, then latched; `OFFENSE_DEFENSE`:
  `offense == 1`; `SHOT_GATED`: within the latch window after
  `venicenet_notify_shot_edge()`).

**true → false** (engine only), triggered by ANY of:
- `venicenet_set_enabled(false)`,
- `venicenet_shutdown()`,
- console IP cleared/changed, court flow lost,
- policy disengage (`OFFENSE_DEFENSE` on defense; `SHOT_GATED` after the
  dwell expires),
- driver error (`driver_state → ERROR`).

Invariants:
- `applied_delay_ms > 0` ⇒ `armed == 1`. Disarming slews the applied delay to
  0 under the slew cap and releases the backlog **in arrival order** before
  reporting `armed == 0` — with two exceptions allowed to bypass the slew
  (never the ordering): `venicenet_shutdown()` and a driver error, where a
  stuck delay outranks a momentary catch-up burst (the backlog is still
  re-paced with ≥ 1 ms spacing, mirroring `_METER_DRAIN_MIN_SPACING_S`).
- `armed == 0` ⇒ `buffer_depth == 0` once the release drain has finished.
- There is no client keepalive/watchdog in this API: the engine and its client
  share a process, so "client died but delay stands" cannot happen. Process
  death closes the WinDivert handle and the driver stops diverting.

Wave-1 stub: pinned at `false` forever.

## Error-code summary

| code | meaning | typical caller reaction |
|---|---|---|
| `VENICENET_OK` (0) | success | — |
| `ERR_NOT_INITIALIZED` (1) | init not called / already shut down | programming error; fix call order |
| `ERR_ALREADY_INITIALIZED` (2) | double init | ignore or fix call order |
| `ERR_INVALID_ARGUMENT` (3) | NULL out-pointer, non-finite delay, bad enum, malformed IP | fix the input; state unchanged |
| `ERR_ABI_MISMATCH` (4) | snapshot `struct_size` disagreement | refuse the DLL; version skew |
| `ERR_DRIVER_NOT_INSTALLED` (5) | (async, via snapshot `error_code`) driver service missing | surface "not available on this install" |
| `ERR_DRIVER_ACCESS_DENIED` (6) | (async) handle open denied | installer DACL step missing/failed |
| `ERR_DRIVER_OPEN_FAILED` (7) | (async) open failed for another reason | see `error_text` |
| `ERR_INTERNAL` (8) | engine bug / unrecoverable | log `error_text`, disable feature |

Codes 5–7 are primarily **asynchronous** codes carried in
`VeniceNetSnapshot.error_code`; synchronous calls do not probe the driver on
the caller's thread.

## Known wave-2 risks (flagged during wave-1 review)

- **Device-object ACL vs service ACL**: `sc sdset WinDivert ...` relaxes the
  *service* DACL (start/stop rights). WinDivert's *device object* security
  descriptor is set by the driver itself at load (SYSTEM + Administrators).
  If the shipped WinDivert build keeps that SDDL, a non-admin
  `WinDivertOpen()` fails with access denied regardless of the service DACL.
  Wave 2 must verify on a rig and, if confirmed, either broker the handle
  differently or adjust the driver-open strategy. `ERR_DRIVER_ACCESS_DENIED`
  exists in the ABI precisely so this failure is honest, not silent.
- **Held packets at process death**: an in-flight non-SNIFF buffer dies with
  the process; those packets are lost (the console sees a short loss burst,
  then normal flow). Acceptable, but worth a crash-handler flush attempt.
- The full porting notes (threading, slew accumulator, float-residue snap,
  ordering proofs) live in the wave-1 report and in `nexus_svc.py`'s
  extensively-commented `_InboundDelayBuffer`, which remains in-tree as the
  reference implementation until wave 2 lands.
