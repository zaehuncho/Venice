#pragma once

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QStandardPaths>
#include <QString>
#include <QStringLiteral>

namespace orion {

// [ORION_DATA_DIR] Where MUTABLE state lives, as opposed to where the program lives.
//
// THE SHIP BLOCKER THIS FIXES. main.cpp sets rootDir = applicationDirPath() and, in a
// production build, deliberately refuses the dev root-dir redirection (that redirection is an
// integrity hole in a shipping build). Everything writable then hangs off the INSTALL
// directory: settings.json, learning.json and its per-profile variants, logs/, status.json,
// calibration/. Installed to the default location that directory is C:\Program Files\..., which
// is not writable by a normal (unelevated) user account.
//
// The first run therefore appears to work -- the app reads defaults fine -- and then every
// write silently fails. Settings never persist, and on the second launch the app comes up
// unconfigured. That is a total first-run failure for every customer who does not happen to run
// as administrator, and it cannot be reproduced on this dev rig, where rootDir is a writable
// checkout under the user's own profile.
//
// SPLIT, not moved: read-only payload (assets/, models/, the packaged chiaki client, the Python
// sidecar and its venv) stays with the install, because that is what the release manifest
// validates and what an installer replaces on update. Only mutable state moves.
//
// DEV IS BYTE-IDENTICAL. Outside a production build this returns rootDir unchanged, so the
// checkout keeps writing settings.json and logs/ exactly where it does today and no dev tooling,
// runbook, or offline replay path has to learn a new location.
[[nodiscard]] inline QString orionDataDir(const QString& rootDir)
{
#ifdef ORION_PRODUCTION_BUILD
    // AppLocalDataLocation is per-user and machine-local (%LOCALAPPDATA%/<Org>/<App> on
    // Windows). Deliberately NOT the roaming location: this is machine-specific calibration and
    // logs, and roaming it onto another PC would carry THIS machine's measured latency with it.
    const QString base =
        QStandardPaths::writableLocation(QStandardPaths::AppLocalDataLocation);
    if (base.isEmpty()) {
        return rootDir;   // no standard location at all -> old behaviour, fail no worse
    }
    QDir dir(base);
    if (!dir.exists() && !QDir().mkpath(base)) {
        // Cannot create the data dir. Falling back to rootDir reproduces the old behaviour
        // rather than inventing a third location; the caller's own write errors still surface.
        return rootDir;
    }
    return base;
#else
    return rootDir;
#endif
}

// One-time carry-over of an existing install-dir file into the data dir.
//
// Only relevant to a build that previously wrote beside the executable (and to anyone who ran
// an elevated first launch, which WOULD have succeeded and left a real settings.json in Program
// Files). Copies rather than moves: the install dir may be read-only, a failed move would lose
// the file outright, and leaving the stale original costs nothing because it is never read once
// the data-dir copy exists.
//
// No-ops when the two paths are the same string, which is every dev build.
inline void migrateLegacyStateFile(const QString& legacyPath, const QString& dataPath)
{
    if (legacyPath == dataPath) {
        return;
    }
    if (QFileInfo::exists(dataPath) || !QFileInfo::exists(legacyPath)) {
        return;   // never clobber state the user already has in the new location
    }
    const QFileInfo target(dataPath);
    if (!target.dir().exists()) {
        QDir().mkpath(target.dir().absolutePath());
    }
    QFile::copy(legacyPath, dataPath);
}

} // namespace orion
