#pragma once

#include <string>
#include <vector>

// [SERVER-SHARD blocker #5, Codex finding #1] Issuer-SPKI certificate pinning for
// the OrionActivate broker's WinHTTP activation call. This is the WinHTTP-side
// equivalent of NetworkSecurity.cpp's Qt pin (LicenseClient). It pins the SAME
// issuer keys — orion::kApiPinnedSpkiSha256 in src/PinnedSpki.h — so the broker's
// /api/license/redeem call cannot be MITM'd by a rogue public-CA leaf.
//
// The pure hash/membership helpers below are Win32 (BCrypt) but free of WinHTTP so
// BrokerSpkiPinTests can exercise accept/reject against fixtures headlessly. The
// WinHTTP cert-chain extraction that feeds these lives in OrionActivate.cpp.
namespace orion {
namespace broker {

// Lowercase-hex SHA-256 of a DER SubjectPublicKeyInfo blob. Empty on failure.
std::string spkiSha256Hex(const void* der, std::size_t len);

// The authoritative pin set (copied from orion::kApiPinnedSpkiSha256), lowercase.
std::vector<std::string> pinnedSpkiSet();

// True iff `lowerHex` is in the authoritative pin set. Case-insensitive on input.
bool isPinnedSpkiHex(const std::string& hex);

// True iff ANY DER SPKI in `chainSpkiDer` hashes into `pinsLowerHex`. Fails CLOSED
// (returns false) on empty chain, empty pin set, or no match. Injectable pin set
// so the fixture tests can prove both accept and reject deterministically.
bool chainMatchesPins(const std::vector<std::string>& chainSpkiDer,
                      const std::vector<std::string>& pinsLowerHex);

// Production entry point: chainMatchesPins(chainSpkiDer, pinnedSpkiSet()).
bool chainMatchesPinnedSpki(const std::vector<std::string>& chainSpkiDer);

} // namespace broker
} // namespace orion
