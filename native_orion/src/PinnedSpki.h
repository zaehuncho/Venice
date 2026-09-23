#pragma once

#include <cstddef>

// [SERVER-SHARD blocker #5 / MED-3] SINGLE SOURCE OF TRUTH for the api.zaeorion.com
// issuer-SPKI SHA-256 pin set.
//
// This is the exact pin set that used to live only in NetworkSecurity.cpp (the Qt
// path used by LicenseClient/AdminToolController). It is now shared verbatim by:
//   * native_orion/src/NetworkSecurity.cpp   (Qt QSslCertificate pin — LicenseClient)
//   * native_orion/broker/SpkiPin.cpp        (WinHTTP pin — OrionActivate broker)
//   * (kept byte-identical, by hand, in the SEPARATE Lethe repo:
//      Lethe/bootstrap/shard_bootstrap.c — that is C and cannot include this header)
//
// Do NOT invent a second pin set anywhere. The broker and the Lethe shard fetch
// pin the SAME issuer keys as the in-app license client so a rogue/edge MITM cert
// cannot serve a forged activation or shard response. Rotate here (and in the
// C copy in Lethe) if Cloudflare ever moves this zone to another CA family.
//
// Rationale (from NetworkSecurity.cpp): the ISSUING-CA SubjectPublicKeyInfo is
// stable across Cloudflare's ~90-day leaf renewals, so an SPKI pin fails CLOSED
// yet survives rotation. Values computed 2026-07-09 from the live chain and
// cross-checked against the CA-published certs (i.pki.goog, letsencrypt.org).
namespace orion {

inline constexpr const char* const kApiPinnedSpkiSha256[] = {
    // ── Google Trust Services (current issuer) ──
    "908769e8d34477cc2cba0632c88605b22d7294c0840f78596d247c645b1afc0e", // GTS WE1 (live issuer; primary pin)
    "be1efc292835472e0d6aa183575d30fdc4dbf551f7050519a0258d6bddd6fc46", // GTS WE2 (ECC sibling, backup)
    "9847e5653e5e9e847516e5cb818606aa7544a19be67fd7366d506988e8d84347", // GTS Root R4 (served in the live chain)
    // ── Let's Encrypt / ISRG (Cloudflare Universal SSL fallback CA) ──
    "3586d4ecf070578cbd27aedce20b964e48bc149faeb9dad72f46b857869172b8", // E5
    "d016e1fe311948aca64f2de44ce86c9a51ca041df6103bb52a88eb3f761f57d7", // E6
    "cbbc559b44d524d6a132bdac672744da3407f12aae5d5f722c5f6c7913871c75", // E7
    "885bf0572252c6741dc9a52f5044487fef2a93b811cdedfad7624cc283b7cdd5", // E8
    "f1440a9b76e1e41e53a4cb461329bf6337b419726be513e42e19f1c691c5d4b2", // E9
    "2bbad93ab5c79279ec121507f272cbe0c6647a3aae52e22f388afab426b4adba", // R10
    "6ddac18698f7f1f7e1c69b9bce420d974ac6f94ca8b2c761701623f99c767dc7", // R11
    "919c0df7a787b597ed056ace654b1de9c0387acf349f73734a4fd7b58cf612a4", // R12
    "025490860b498ab73c6a12f27a49ad5fe230fafe3ac8f6112c9b7d0aad46941d", // R13
    "f1647a5ee3efac54c892e930584fe47979b7acd1c76c1271bca1c5076d869888", // R14
};

inline constexpr std::size_t kApiPinnedSpkiSha256Count =
    sizeof(kApiPinnedSpkiSha256) / sizeof(kApiPinnedSpkiSha256[0]);

} // namespace orion
