#pragma once

#include "OrionExports.h"

#include <QtCore/QByteArray>
#include <QtCore/QString>

// [SERVER-SHARD blocker #5, Codex finding #1/#2 — 2026-09-20 hardening round 2]
// SINGLE trust anchor for "is this OrionActivate.exe a genuine, tamper-checked
// server-shard broker?" — shared by BOTH:
//   * native_orion/src/main.cpp  (the packed app decides whether to DEFER the
//     orion:// handler to a sibling broker), and
//   * native_orion/broker/OrionActivate.cpp (the broker decides whether it may
//     SELF-REGISTER the orion:// handler / is running from a verified root).
//
// The two callers MUST reach the same verdict for the same file, so the logic
// lives here once. It reuses the EXISTING update/release verification path — the
// pinned Ed25519 public key (ReleaseManifestTrust.h, orion-ed25519-v1) and the
// libcrypto-backed orion::ed25519Verify() — rather than inventing a new one.
//
// A file is genuine iff EITHER gate holds, with NO unauthenticated fallback:
//   (A) it carries a VALID Authenticode signature whose signer subject matches
//       the Venice publisher (and optional SHA-1 thumbprint pin), OR
//   (B) it is byte-covered by a release_manifest.json whose DETACHED Ed25519
//       signature (release_manifest.sig) verifies against the pinned production
//       key id, with the production fields enforced (schema/audience/
//       signature_required/signature_alg/public_key_id).
namespace orion {

// Expected Authenticode signer of the Venice activation broker.
//
// ⚠️ OWNER ACTION REQUIRED ⚠️  These are PLACEHOLDERS. Before shipping a signed
// broker, the OWNER must set the REAL leaf SHA-1 thumbprint (and subject CN) from
// Desktop\VeniceSigning:
//   * kExpectedBrokerSignerThumbprint — the leaf cert's 40 hex chars, no spaces.
//     [FIX B — 2026-09-20] This is the SWITCH for gate (A): while it is EMPTY (or
//     not exactly 40 hex), gate (A) is INERT and returns false for every file, so
//     the signed release manifest (gate B) is the SOLE trust anchor. Subject-
//     substring matching is NEVER sufficient on its own — an exact thumbprint pin
//     is mandatory to enable gate (A). Set it to turn gate (A) on as
//     defense-in-depth once the broker is actually Authenticode-signed.
//   * kExpectedBrokerSignerSubstr  — a case-insensitive substring of the signer's
//     subject CN (e.g. "Venice" matches CN="Venice Software LLC"). Required IN
//     ADDITION to (never instead of) the thumbprint when gate (A) is enabled.
// The owner will NOT sign the broker for launch, so gate (A) stays inert and the
// signed release manifest authorizes today. Neither gate ever trusts a bare
// filename (that was Codex finding #2).
inline constexpr wchar_t kExpectedBrokerSignerSubstr[] = L"Venice";
inline constexpr wchar_t kExpectedBrokerSignerThumbprint[] = L""; // e.g. L"ABCD...40hex"

// Gate (A): INERT unless kExpectedBrokerSignerThumbprint is an exact 40-hex pin —
// while it is empty this returns false for any file (gate B is the sole anchor).
// When pinned: true iff `path` carries a VALID Authenticode signature (chain
// trusted) whose leaf thumbprint EQUALS kExpectedBrokerSignerThumbprint AND whose
// signer subject contains kExpectedBrokerSignerSubstr. Windows-only; false on any
// other platform. Never trusts a subject substring alone.
ORION_SECURITY_API bool fileAuthenticodeSignedByVenice(const QString& path,
                                                       QString* detail = nullptr);

// Gate (B): true iff release_manifest.json in `manifestDir` (1) is schema/audience
// valid, (2) carries a valid detached Ed25519 signature (release_manifest.sig)
// made with the PINNED production key id (orion-ed25519-v1), and (3) covers
// `targetFilePath` by an EXACT SHA-256 match of the bytes on disk. Signature is
// MANDATORY here (no dev-optional path) — an unsigned/wrong-key/tampered manifest
// returns false. `productionBuild` disables the dev-only test-key override; in a
// production build the test override args are ignored.
ORION_SECURITY_API bool releaseManifestCoversFileSigned(
    const QString& manifestDir,
    const QString& targetFilePath,
    bool productionBuild,
    const QString& testOverrideKeyId = {},
    const QByteArray& testOverrideKeyB64 = {},
    QString* detail = nullptr);

// The combined guard: gate (A) OR gate (B). `brokerExePath` is the OrionActivate.exe
// to authenticate; `manifestDir` is where release_manifest.json / .sig live (the
// install dir). Returns false if the file is absent or neither gate holds.
ORION_SECURITY_API bool genuineBrokerInstallPresent(
    const QString& brokerExePath,
    const QString& manifestDir,
    bool productionBuild,
    const QString& testOverrideKeyId = {},
    const QByteArray& testOverrideKeyB64 = {},
    QString* detail = nullptr);

} // namespace orion
