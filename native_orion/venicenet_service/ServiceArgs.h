#pragma once

// ───────────────────────────────────────────────────────────────────────────
//  ServiceArgs.h — argv/env routing (ports _meter_arm_requested /
//                  _is_service_run_invocation)
// ───────────────────────────────────────────────────────────────────────────
//
//  Pure functions of (argv, env value) so the SCM-vs-command routing and the
//  arm gate are unit-testable without a live SCM. Mirrors nexus_svc.py exactly:
//    * --arm-meter-delay OR ORION_METER_DELAY_ARMED in {1,true,yes,on} arms.
//    * A bare invocation (no verb, arm flag aside) is a service-run invocation:
//      the SCM launches the exe via its ImagePath with no verb, and — when the
//      installer armed the bridge — with --arm-meter-delay baked into binPath.
//      That must run the SCM dispatcher, NOT the command parser.

#include <algorithm>
#include <cctype>
#include <string>
#include <vector>

namespace venicenet {

inline constexpr const char* kMeterArmEnv = "ORION_METER_DELAY_ARMED";
inline constexpr const char* kMeterArmFlag = "--arm-meter-delay";

inline bool envValueIsTruthy(const std::string& raw)
{
    std::string v = raw;
    // trim + lower
    size_t b = 0;
    size_t e = v.size();
    while (b < e && std::isspace(static_cast<unsigned char>(v[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(v[e - 1]))) --e;
    v = v.substr(b, e - b);
    for (char& c : v) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }
    return v == "1" || v == "true" || v == "yes" || v == "on";
}

// Returns {armed, source}. source is "cli", "env", or "".
inline std::pair<bool, std::string> meterArmRequested(const std::vector<std::string>& args,
                                                      const std::string& envValue)
{
    if (std::find(args.begin(), args.end(), std::string(kMeterArmFlag)) != args.end()) {
        return {true, "cli"};
    }
    if (envValueIsTruthy(envValue)) {
        return {true, "env"};
    }
    return {false, ""};
}

// args = argv WITHOUT the program name (argv[1:]). True when nothing but the arm
// flag is present.
inline bool isServiceRunInvocation(const std::vector<std::string>& args)
{
    for (const std::string& a : args) {
        if (a != kMeterArmFlag) {
            return false;
        }
    }
    return true;
}

} // namespace venicenet
