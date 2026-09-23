#include "SpkiPin.h"

#include "PinnedSpki.h"

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <bcrypt.h>

#include <cctype>

#pragma comment(lib, "bcrypt.lib")

namespace orion {
namespace broker {

std::string spkiSha256Hex(const void* der, std::size_t len)
{
    if (!der || len == 0) {
        return {};
    }
    // Explicit provider API (matches Lethe/bootstrap/shard_bootstrap.c) so we do
    // not depend on the Win10-1903 pseudo-handle being present in every SDK.
    BCRYPT_ALG_HANDLE alg = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    unsigned char digest[32] = {};
    bool ok = false;
    if (BCryptOpenAlgorithmProvider(&alg, BCRYPT_SHA256_ALGORITHM, nullptr, 0) == 0 &&
        BCryptCreateHash(alg, &hash, nullptr, 0, nullptr, 0, 0) == 0 &&
        BCryptHashData(hash,
                       const_cast<PUCHAR>(static_cast<const UCHAR*>(der)),
                       static_cast<ULONG>(len), 0) == 0 &&
        BCryptFinishHash(hash, digest, sizeof(digest), 0) == 0) {
        ok = true;
    }
    if (hash) BCryptDestroyHash(hash);
    if (alg) BCryptCloseAlgorithmProvider(alg, 0);
    if (!ok) {
        SecureZeroMemory(digest, sizeof(digest));
        return {};
    }
    static const char* kHex = "0123456789abcdef";
    std::string out;
    out.resize(sizeof(digest) * 2);
    for (std::size_t i = 0; i < sizeof(digest); ++i) {
        out[i * 2]     = kHex[(digest[i] >> 4) & 0x0f];
        out[i * 2 + 1] = kHex[digest[i] & 0x0f];
    }
    SecureZeroMemory(digest, sizeof(digest));
    return out;
}

std::vector<std::string> pinnedSpkiSet()
{
    std::vector<std::string> pins;
    pins.reserve(orion::kApiPinnedSpkiSha256Count);
    for (std::size_t i = 0; i < orion::kApiPinnedSpkiSha256Count; ++i) {
        pins.emplace_back(orion::kApiPinnedSpkiSha256[i]);
    }
    return pins;
}

namespace {
std::string toLower(const std::string& s)
{
    std::string out(s.size(), '\0');
    for (std::size_t i = 0; i < s.size(); ++i) {
        out[i] = static_cast<char>(std::tolower(static_cast<unsigned char>(s[i])));
    }
    return out;
}
} // namespace

bool isPinnedSpkiHex(const std::string& hex)
{
    if (hex.empty()) {
        return false;
    }
    const std::string needle = toLower(hex);
    for (std::size_t i = 0; i < orion::kApiPinnedSpkiSha256Count; ++i) {
        if (needle == orion::kApiPinnedSpkiSha256[i]) {
            return true;
        }
    }
    return false;
}

bool chainMatchesPins(const std::vector<std::string>& chainSpkiDer,
                      const std::vector<std::string>& pinsLowerHex)
{
    // FAIL CLOSED: no chain or no pins can never authorize.
    if (chainSpkiDer.empty() || pinsLowerHex.empty()) {
        return false;
    }
    for (const std::string& der : chainSpkiDer) {
        if (der.empty()) {
            continue;
        }
        const std::string hex = spkiSha256Hex(der.data(), der.size());
        if (hex.empty()) {
            continue;
        }
        for (const std::string& pin : pinsLowerHex) {
            if (hex == pin) {
                return true;
            }
        }
    }
    return false;
}

bool chainMatchesPinnedSpki(const std::vector<std::string>& chainSpkiDer)
{
    return chainMatchesPins(chainSpkiDer, pinnedSpkiSet());
}

} // namespace broker
} // namespace orion
