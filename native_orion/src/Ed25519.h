#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>

namespace orion {

// Ed25519 signature verification for the update manifest. Backed by the bundled
// OpenSSL libcrypto (loaded at runtime via QLibrary), so no asymmetric-crypto
// dependency is linked at build time. Fails closed (returns false / empty) if
// libcrypto or the required EVP symbols cannot be loaded.
//
// The client only ever needs the 32-byte *public* key — no secret material is
// shipped or required to verify. Signing helpers exist for tests/tooling only;
// the production updater never signs.

// True iff a working libcrypto with Ed25519 EVP support could be resolved.
ORION_SECURITY_API bool ed25519Available();

// Verify a detached Ed25519 signature. publicKey must be 32 raw bytes and
// signature 64 raw bytes; anything else returns false.
ORION_SECURITY_API bool ed25519Verify(const QByteArray& publicKey,
                                       const QByteArray& message,
                                       const QByteArray& signature);

// Detached Ed25519 signing from a 32-byte raw private seed. For tests/tooling
// only. Returns the 64-byte signature, or empty on failure.
ORION_SECURITY_API QByteArray ed25519Sign(const QByteArray& privateSeed, const QByteArray& message);

// Derive the 32-byte public key from a 32-byte private seed. For tests/tooling
// only. Empty on failure.
ORION_SECURITY_API QByteArray ed25519DerivePublicKey(const QByteArray& privateSeed);

} // namespace orion
