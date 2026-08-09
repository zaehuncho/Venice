# Security review — TLS / admin tool / license (2026-06-20)

Read-only review of the security-sensitive code committed this session (`NetworkSecurity`,
`AdminToolController`, `LicenseClient`, the NVDEV dev path). **Verdict: no security issues found.**

## TLS certificate pinning — strong, fail-closed
- `applyStrictTls`: `VerifyPeer` + `setPeerVerifyName(host)` (hostname enforced).
- `replyMatchesPinnedCertificate`: leaf-cert SHA256 vs the embedded pin; returns **false** on empty
  chain or mismatch (fail-closed). Pin is a public cert fingerprint (not a secret).
- **Enforced everywhere**: every Admin + License network call calls `applyStrictTls` then rejects on
  `!replyMatchesPinnedCertificate`. `LicenseClient` checks it **twice** (on `encrypted` *and*
  `finished`) and aborts on `sslErrors`. A defined-but-unchecked pin would be useless — this one is
  checked at every call site.

## Staff token (admin tool) — clean
- Held in memory only (`staffToken_`), used only in `Authorization: Bearer` headers, `clear()`ed.
- **Never logged or written to disk** (grep-confirmed). No plaintext persistence in the C++.

## License model — sound (server-authoritative over pinned TLS)
- Validity = the pinned server's `{"ok":true}`; the cert pin makes the response MITM-resistant.
- No client-side signature on the license token — a deliberate online-activation design. The inherent
  limit of *any* client-side license check is binary patching; that's unchanged by adding a signature,
  so this is not a real weakness. (The updater separately does Ed25519 manifest verification.)

## NVDEV dev-bypass — correctly production-gated
- `NVDEV-` keys are accepted only when `localDevAllowed()` is true.
- `localDevAllowed()` is **compile-time `false` under `ORION_PRODUCTION_BUILD`** (set by CMake), in
  BOTH `MainWindow.cpp` and `OrionAppController.cpp`, with the comment *"Production: never. On-disk
  markers and env vars are attacker-controlled."* The dev path keys off a dev checkout
  (CMakeLists.txt / nexus_server.db present) or `ORION_LOCAL_UI_TEST`, none of which ship.

## Secret hygiene in logs/diagnostics
- `Diagnostics.cpp` masks `ORION-`/`NVDEV-` license keys (keeps last 4 chars only).
- Auto-auth from `ORION_LICENSE_KEY` is dev-gated and logs only the key suffix.

## Net
Security-sensitive code is well-designed: pinning enforced + fail-closed, no secret leakage, license
checks production-hardened, dev bypass compile-time-excluded from production. Nothing to fix.
