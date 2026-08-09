#pragma once

#include "OrionExports.h"

#include <QtCore/QString>

namespace orion {

// Redact secrets from text destined for a support-diagnostics bundle. Removes or
// masks: license keys (ORION-/NVDEV- style — last 4 chars kept for support
// correlation), bearer/access tokens and JWTs, machine ids, email addresses, and
// generic `key`/`secret`/`password`-labelled values. Pure function, unit-tested;
// every byte written into a diagnostics export MUST pass through it.
ORION_COMMON_API QString redactDiagnosticsText(const QString& text);

} // namespace orion
