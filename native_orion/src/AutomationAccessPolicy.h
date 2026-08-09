#pragma once

namespace orion {

// Pure policy seam for the release-critical automation authorization boundary.
// Production makes the lease gate mandatory at compile time (LeaseGate.cpp),
// while this policy independently requires an authenticated launcher session.
struct AutomationAccessPolicy final {
    [[nodiscard]] static constexpr bool localDevBypassEligible(
        bool localDevSession,
        bool securityLockActive,
        bool releaseManifestRequired) noexcept
    {
        return localDevSession && !securityLockActive && !releaseManifestRequired;
    }

    [[nodiscard]] static constexpr bool allowed(
        bool authenticated,
        bool localDevBypass,
        bool securityLockActive,
        bool leaseGateEnabled,
        bool leaseFireAllowed) noexcept
    {
        if (localDevBypass) {
            return true;
        }
        if (!authenticated || securityLockActive) {
            return false;
        }
        return !leaseGateEnabled || leaseFireAllowed;
    }
};

} // namespace orion
