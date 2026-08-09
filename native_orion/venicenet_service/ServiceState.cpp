#include "ServiceState.h"

namespace venicenet {

const char* driverStateName(DriverState state)
{
    switch (state) {
    case DriverState::NotInstalled: return "not_installed";
    case DriverState::NotStarted: return "not_started";
    case DriverState::Ready: return "ready";
    case DriverState::HandleOpen: return "handle_open";
    case DriverState::Error: return "error";
    }
    return "unknown";
}

DriverState ServiceStateMachine::driverState() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return driver_;
}

bool ServiceStateMachine::armed() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return armed_;
}

ServiceError ServiceStateMachine::errorCode() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return errorCode_;
}

std::string ServiceStateMachine::errorText() const
{
    std::lock_guard<std::mutex> lock(mutex_);
    return errorText_;
}

bool ServiceStateMachine::onProbe(bool installed, bool running)
{
    std::lock_guard<std::mutex> lock(mutex_);

    if (!installed) {
        // Only the installer can leave NOT_INSTALLED; the engine merely observes
        // it. A running handle cannot coexist with "not installed", so if we were
        // HANDLE_OPEN this is a hard fault, not a graceful demotion.
        if (driver_ == DriverState::HandleOpen) {
            return false;
        }
        driver_ = DriverState::NotInstalled;
        // A successful probe clears a stale error.
        errorCode_ = ServiceError::Ok;
        errorText_.clear();
        armed_ = false;
        return true;
    }

    // installed && running -> READY (unless a handle is already open, which is a
    // strictly further-along legal state and the probe must not knock it back).
    // installed && !running -> NOT_STARTED.
    if (driver_ == DriverState::HandleOpen) {
        // The probe observed the service registered+running while we hold a
        // handle: nothing to change, and it is not a demotion.
        if (running) {
            errorCode_ = ServiceError::Ok;
            errorText_.clear();
            return true;
        }
        // Driver reported not-running while we believe a handle is open — treat as
        // an externally-stopped driver: drop to NOT_STARTED (docs: READY ->
        // NOT_STARTED "driver stopped externally"; the open handle is void).
        driver_ = DriverState::NotStarted;
        armed_ = false;
        return true;
    }

    driver_ = running ? DriverState::Ready : DriverState::NotStarted;
    errorCode_ = ServiceError::Ok;
    errorText_.clear();
    return true;
}

bool ServiceStateMachine::onHandleOpened()
{
    std::lock_guard<std::mutex> lock(mutex_);
    // READY -> HANDLE_OPEN. Opening implies the driver started (a demand-start
    // service starts on first open), so tolerate NOT_STARTED as well.
    if (driver_ == DriverState::Ready || driver_ == DriverState::NotStarted) {
        driver_ = DriverState::HandleOpen;
        errorCode_ = ServiceError::Ok;
        errorText_.clear();
        return true;
    }
    if (driver_ == DriverState::HandleOpen) {
        return true; // idempotent
    }
    // NOT_INSTALLED -> HANDLE_OPEN directly is illegal (docs: no transition skips
    // the probe).
    return false;
}

bool ServiceStateMachine::onHandleClosed()
{
    std::lock_guard<std::mutex> lock(mutex_);
    armed_ = false;
    if (driver_ == DriverState::HandleOpen) {
        driver_ = DriverState::Ready;
        return true;
    }
    // Closing when no handle is open is a no-op, not a fault.
    return true;
}

bool ServiceStateMachine::onError(ServiceError code, const std::string& text)
{
    std::lock_guard<std::mutex> lock(mutex_);
    driver_ = DriverState::Error;
    errorCode_ = code;
    errorText_ = text;
    armed_ = false; // a driver error disarms (docs armed-machine exit condition)
    return true;
}

void ServiceStateMachine::clearError()
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (driver_ == DriverState::Error) {
        driver_ = DriverState::NotStarted;
    }
    errorCode_ = ServiceError::Ok;
    errorText_.clear();
}

bool ServiceStateMachine::setArmed(bool armed, bool enabled, bool courtDetected)
{
    std::lock_guard<std::mutex> lock(mutex_);
    if (!armed) {
        armed_ = false;
        return true;
    }
    // false -> true structural preconditions (docs armed machine).
    if (driver_ != DriverState::HandleOpen || !enabled || !courtDetected) {
        return false;
    }
    armed_ = true;
    return true;
}

void ServiceStateMachine::forceDisarm()
{
    std::lock_guard<std::mutex> lock(mutex_);
    armed_ = false;
}

} // namespace venicenet
