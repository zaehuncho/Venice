#pragma once

#include <utility>

namespace orion::packet_bridge_authority {

// Packet metadata from the loopback TCP bridge is useful for counters and UI,
// but it is not timing authority until the native client has independently
// verified the privileged service identity.  The current bearer-token protocol
// authenticates only the client to the listener, so its events are Unverified.
enum class ServiceIdentityTrust {
    Unverified,
    VerifiedService,
};

[[nodiscard]] inline constexpr bool mayFeedTiming(
    ServiceIdentityTrust trust, bool explicitDevelopmentExperiment) noexcept
{
    if (trust == ServiceIdentityTrust::VerifiedService) {
        return true;
    }
#ifdef ORION_PRODUCTION_BUILD
    // Compile-time production fence: no runtime setting or environment variable
    // can promote an unverified loopback listener into timing authority.
    (void)explicitDevelopmentExperiment;
    return false;
#else
    return explicitDevelopmentExperiment;
#endif
}

template <typename ForwardFn>
[[nodiscard]] inline bool forwardToTimingIfAuthorized(
    ServiceIdentityTrust trust, bool explicitDevelopmentExperiment,
    ForwardFn&& forward)
{
    if (!mayFeedTiming(trust, explicitDevelopmentExperiment)) {
        return false;
    }
    std::forward<ForwardFn>(forward)();
    return true;
}

} // namespace orion::packet_bridge_authority
