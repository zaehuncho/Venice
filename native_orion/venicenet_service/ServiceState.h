#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  ServiceState.h — the authoritative driver_state / armed machine
// ───────────────────────────────────────────────────────────────────────────
//
//  VeniceNetSvc.exe is the authoritative state machine for the meter-delay
//  engine (docs/VENICENET_API.md, "driver_state machine" + "armed machine").
//  The wave-2B DLL and the app merely mirror what this reports.
//
//  driver_state values are numerically identical to VeniceNetDriverState in
//  native_orion/venicenet/VeniceNet.h so the value can cross the wire and the
//  ABI unchanged (0=NotInstalled .. 4=Error). This header does NOT include
//  VeniceNet.h (the service is Qt-free and stands alone), but the values are
//  pinned by static_assert-style comments and covered by a unit test.
//
//  Every transition here is legal per the docs table; illegal transitions are
//  rejected (the method returns false and the state is unchanged) so a porting
//  bug surfaces as a test failure rather than a silent skipped-probe. The class
//  is thread-safe: the sniff/intercept/IPC threads all query and drive it.

#include <mutex>
#include <string>

namespace venicenet {

// Mirrors VeniceNetDriverState (VeniceNet.h). Add, never renumber.
enum class DriverState : int {
    NotInstalled = 0,
    NotStarted = 1,
    Ready = 2,
    HandleOpen = 3,
    Error = 4,
};

// Mirrors VeniceNetStatus error codes carried in the snapshot (VeniceNet.h).
enum class ServiceError : int {
    Ok = 0,
    DriverNotInstalled = 5,
    DriverAccessDenied = 6,
    DriverOpenFailed = 7,
    Internal = 8,
};

const char* driverStateName(DriverState state);

class ServiceStateMachine {
public:
    ServiceStateMachine() = default;

    DriverState driverState() const;
    bool armed() const;
    ServiceError errorCode() const;
    std::string errorText() const;

    // ── driver_state transitions (docs/VENICENET_API.md table) ──
    //
    // Each returns true if the transition was legal and applied. A no-op (already
    // in the target state, where the docs permit it) returns true. An illegal
    // transition returns false and leaves state untouched.

    // Periodic probe result. `installed`/`running` come from the SCM query.
    // Handles NotInstalled->NotStarted, NotStarted->Ready, Ready->NotStarted,
    // and Error->{NotInstalled,NotStarted,Ready} recovery while enabled.
    bool onProbe(bool installed, bool running);

    // READY -> HANDLE_OPEN: intercept handle opened.
    bool onHandleOpened();

    // HANDLE_OPEN -> READY: intercept handle closed (disable / IP change /
    // disengage / disarm). No-op-safe from READY.
    bool onHandleClosed();

    // any -> ERROR: open/recv/send hard failure, access denied, probe failure.
    // Records the sticky error_code/error_text.
    bool onError(ServiceError code, const std::string& text);

    // Clears a sticky error after a successful re-probe/re-open (docs: ERROR ->
    // {NotInstalled,NotStarted,Ready}). Called by onProbe internally; exposed for
    // the open path which clears the error the moment a handle opens cleanly.
    void clearError();

    // ── armed machine ──
    //
    // false->true requires ALL of: HANDLE_OPEN, enabled, court detected, and the
    // policy condition (evaluated by the caller). This method enforces the
    // structural preconditions and refuses otherwise.
    bool setArmed(bool armed, bool enabled, bool courtDetected);

    // Force disarm regardless of preconditions (disable, shutdown, error).
    void forceDisarm();

private:
    mutable std::mutex mutex_;
    DriverState driver_ = DriverState::NotInstalled;
    bool armed_ = false;
    ServiceError errorCode_ = ServiceError::Ok;
    std::string errorText_;
};

} // namespace venicenet
