#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>
#include <QtCore/QList>
#include <QtCore/QString>
#include <QtCore/QStringList>

#include <functional>

namespace orion::updater {

// A public key compiled into OrionUpdater.exe. More than one is embedded only during a
// planned rotation (old + new), so a client updated under either key keeps working.
struct EmbeddedKey {
    QString id;
    QByteArray key;            // 32 raw bytes
};

// Every place a trusted Ed25519 PUBLIC key may come from besides the binary, in the
// order they are consulted. All of them are external and therefore writable by whoever
// can write the user's environment or the install directory.
struct TrustRootSources {
    QString cliPubkeysFile;    // --pubkeys-file
    QString envPubkeysFile;    // ORION_UPDATE_PUBKEYS
    QString installDir;        // <installDir>/update_pubkeys.json
    QString applicationDir;    // <applicationDir>/update_pubkeys.json
};

struct TrustRootDecision {
    QByteArray publicKey;      // 32 bytes when resolved, empty otherwise (fail closed)
    QString source;            // "embedded" | "cli" | "env" | "install_dir" | "app_dir" | ""
    QStringList ignored;       // external sources that were offered but refused by policy
};

// Decodes the on-disk key encoding (hex or base64) to raw bytes; supplied by the
// caller so UpdaterCore does not depend on SecurityCore.
using PublicKeyDecoder = std::function<QByteArray(const QString&)>;

// [2026-09-21 TRUST ROOT] Resolve the public key for a manifest's public_key_id.
//
// The embedded keys are consulted first. Then, in DEVELOPMENT builds only, the
// external sources in TrustRootSources order.
//
// PRODUCTION builds trust NOTHING but the embedded keys. The external sources are the
// same trust level as "whoever can set a user environment variable or drop a file
// next to the install": an unprivileged local process could point
// ORION_UPDATE_PUBKEYS at its own key file and the elevated updater would then accept
// a manifest it signed itself -- a signed-update bypass and a privilege escalation in
// one. Refused sources are reported in `ignored` so the updater log shows the attempt.
// Rotation and compromise handling: docs/UPDATER_CLIENT.md "Key rotation".
ORION_UPDATER_API TrustRootDecision resolveTrustRoot(const QString& keyId,
                                                     const QList<EmbeddedKey>& embedded,
                                                     const TrustRootSources& sources,
                                                     bool productionBuild,
                                                     const PublicKeyDecoder& decode);

// Whether --manifest-file (a LOCAL manifest replacing the HTTPS one) is honoured.
// Never in production: combined with any key override it is an offline path to
// installing an arbitrary archive, and the launcher never passes it.
ORION_UPDATER_API bool localManifestAllowed(bool productionBuild);

// [Codex F2] The install root the updater will write into. In production it must be
// the updater's own directory (the launcher always passes exactly that) and it may not
// be a reparse point; in development an explicit --install-dir is allowed for tests but
// a reparse-point root is still refused. Returns an empty string and sets *error when
// the root is refused.
// [Codex r2] Where `--build-profile <path>` may write. An elevated updater must never be
// an arbitrary-file-write primitive, so the target has to be a NEW file directly under
// the system temp directory, with no reparse point on the way. Returns the cleaned path
// or an empty string with *error set.
ORION_UPDATER_API QString buildProfileOutputPath(const QString& requestedPath,
                                                 const QString& tempRoot,
                                                 QString* error);

// [Codex r3 F7] The executable the updater relaunches after an update. An elevated
// updater must never launch a caller-chosen path. Production ignores --relaunch entirely
// (always "OrionNative.exe"; *ignoredOverride reports a differing request so it is
// logged). Development accepts only a bare *.exe file name: no separators, no "..", no
// drive/ADS colon, no absolute path. The launch site still checks the resolved path is a
// regular file under the install root with no reparse point on the way.
ORION_UPDATER_API QString relaunchExecutableName(const QString& requested,
                                                 bool productionBuild,
                                                 bool* ignoredOverride,
                                                 QString* error);

// The full path to relaunch, or empty when it is not a plain file inside installDir
// (reparse point on the way, missing, a directory, escapes the root).
ORION_UPDATER_API QString safeRelaunchPath(const QString& installDir,
                                           const QString& executableName,
                                           QString* error);

ORION_UPDATER_API QString bindInstallRoot(const QString& requestedInstallDir,
                                          const QString& applicationDir,
                                          bool productionBuild,
                                          QString* error);

// Second-stage updater only: the executable lives in a direct sibling
// `.orion_updater_stage-*` directory created by the first stage, never under
// the install tree. Caller must additionally verify the signed old manifest and
// every staged helper file against it before writing the target.
ORION_UPDATER_API QString bindStagedInstallRoot(const QString& requestedInstallDir,
                                                const QString& applicationDir,
                                                bool productionBuild,
                                                QString* error);

} // namespace orion::updater
