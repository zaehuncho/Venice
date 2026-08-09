#pragma once

#include <QtCore/QByteArray>

namespace orion {

// Python's logging.basicConfig writes orchestrator INFO records to stderr. Keep
// ordinary INFO off the native diagnostics path, but surface the two bounded,
// shot-scoped records needed to diagnose automatic timing setup:
//   1. receipt of the native release marker;
//   2. the latency oracle's accept/reject result for that marker.
[[nodiscard]] inline bool shouldRelaySidecarInfoLine(const QByteArray& line) noexcept
{
    return line.contains(
               QByteArrayLiteral(" INFO RemotePlayOrchestrator: release marker:"))
        || line.contains(
               QByteArrayLiteral(" INFO latency_estimator: latency observation:"));
}

} // namespace orion
