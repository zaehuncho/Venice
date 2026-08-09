# Orion Client Update + OrionUpdater.exe

This documents the **client** side of Orion self-update: the `/api/update`
contract the launcher consumes, the manifest signing scheme `OrionUpdater.exe`
verifies, and the update flow. The server side (`/api/update`, the S3/CloudFront
artifact host, and signing) is built separately — see `docs/SERVER_HANDOFF.md`.

## Components

| Piece | Role |
|-------|------|
| `OrionNative.exe` | Main launcher. Checks `/api/update` at startup **before AuthGate** (blocking gate, fail-soft), auto-proceeds into the updater after a countdown, and keeps the Patch pill live for later checks. |
| `OrionUpdater.exe` | Standalone helper shipped next to `OrionNative.exe`. Waits for the launcher to exit, re-fetches + re-verifies the manifest, downloads, verifies, backs up, replaces, rolls back on failure, relaunches. |
| `UpdateManifest` (SecurityCore) | Parse + signature + hash + version-policy logic. Unit-tested. |
| `UpdaterArchive` (UpdaterCore) | Path-traversal-safe zip extraction + backup/restore. Unit-tested. |

## `/api/update` response contract

`GET https://api.zaeorion.com/api/update?client_version=<ver>&channel=<ring>`
returns JSON. `channel` is the client's release ring (`dev` | `internal` | `beta`
| `stable`; settings key `update_channel`, default `beta`); the server serves the
per-ring manifest and may echo a top-level `"channel"` field (informational,
**not signed**). The launcher and updater are tolerant of field-name variants
(parser accepts the aliases in parentheses):

```json
{
  "ok": true,
  "version": "1.0.4",                       // latest client build (alias: latest)
  "minimum_supported_version": "1.0.0",     // clients below this are blocked (alias: min_version)
  "url": "https://updates.zaeorion.com/orion-1.0.4.zip",   // HTTPS only (alias: download_url)
  "sha256": "<lowercase hex sha256 of the zip>",           // (alias: artifact_sha256)
  "signature_alg": "ed25519",               // REQUIRED; only ed25519 is accepted
  "public_key_id": "orion-2026-1",          // selects which trusted public key signed this
  "signature": "<hex or base64 Ed25519 signature over the canonical string>",
  "published_at": "2026-06-09T00:00:00Z",   // signed verbatim (string; ISO-8601 or unix seconds)
  "mandatory": false,                        // (alias: required)
  "allow_rollback": false,                   // permit installing an older version (alias: allow_downgrade)
  "notes": "Human-readable release notes"
}
```

`ok:false` (or a non-200 / unreachable endpoint) is treated as **fail-soft**: the
launcher keeps working and falls back to `/api/version` for the Patch pill. The
**only** hard block is `minimum_supported_version > client_version`
(`updateBlocked`), which surfaces a "Update required" gate. (The launcher only
*displays* update status; it never executes a download — all verification happens
in `OrionUpdater.exe`.)

## Startup update gate (blocking, fail-soft)

The first update check arms a full-screen gate that mounts **before AuthGate**
(`Main.qml` → `UpdateGatePage.qml`, driven by `orion.updateGatePhase`):

- `checking` — minimal splash while `/api/update` resolves. The request carries a
  15 s transfer timeout and a 20 s watchdog clears the gate if anything strands
  it: **offline / slow never bricks the app**.
- `offer` — optional update. Auto-proceeds into `OrionUpdater.exe` after a 5 s
  countdown; "Continue without updating" is available.
- `force` — `mandatory:true` or `updateBlocked`. Same countdown, **no skip**.
- `clear` — proceed to AuthGate. Later checks (Patch pill, `checkBackend()`)
  never re-block a live session; gate policy is `evaluateUpdateGate()` in
  `UpdateManifest.h` (unit-tested).

**Dev guard:** a launch from a CMake build tree (`CMakeCache.txt` found in the
exe dir or up to 3 parents) never auto-applies and is always escapable —
`startUpdate()` refuses outright so an update can never overwrite a source
checkout. **Missing updater:** an optional update degrades to the Patch pill;
only a server hard block still gates.

## Manifest signature — Ed25519 (asymmetric)

The manifest is signed with **Ed25519**. This is **asymmetric**: the server holds
the private key and the client ships **only the 32-byte public key**, so a
shipped client cannot be used to forge manifests. HMAC/shared-secret signing is
**not** used for production client verification, and the updater never requires
`ORION_UPDATE_VERIFY_KEY`, an HMAC key file, or any compile-time shared secret.

The signed bytes are **compact JSON** of exactly these eight fields, in this
order, produced by Python `json.dumps(sort_keys=False, separators=(",", ":"),
ensure_ascii=True)` — i.e. no whitespace, quoted string values, lowercase
boolean literals (`true`/`false`), and `\uXXXX`-escaped non-ASCII. The signed
JSON is **not** the full manifest, only these fields, in order:

```
latest_version, minimum_supported_version, artifact_url, sha256,
published_at, mandatory, allow_rollback, public_key_id
```

Example signed payload (the live v0.4.0 manifest, verbatim — note `mandatory` and
`allow_rollback` are JSON booleans, `sha256` is used exactly as emitted):

```json
{"latest_version":"0.1.0","minimum_supported_version":"0.1.0","artifact_url":"https://orion-artifacts-987622176566.s3.amazonaws.com/orion-launcher-0.1.0.exe","sha256":"abc123placeholder000000000000000000000000000000000000000000000000","published_at":"2026-06-10T05:53:09Z","mandatory":false,"allow_rollback":false,"public_key_id":"orion-ed25519-v1"}
```

`signature = base64url( Ed25519_sign( server_private_key, payload_bytes ) )` with
trailing `=` padding stripped (the client also accepts standard base64 or hex).

Reference (Python, server side — requires `cryptography`):

```python
import json, base64
FIELDS = ["latest_version","minimum_supported_version","artifact_url","sha256",
          "published_at","mandatory","allow_rollback","public_key_id"]
canonical = {f: manifest[f] for f in FIELDS}
payload = json.dumps(canonical, sort_keys=False, separators=(",", ":"), ensure_ascii=True).encode()
signature = base64.urlsafe_b64encode(private_key.sign(payload)).rstrip(b"=").decode()
```

The client reproduces these exact bytes in `canonicalManifestSigningString`
(`UpdateManifest.cpp`) — covered by the `canonicalMatchesServerBytes` and
`verifiesLiveBackendManifestVector` tests, which pin the client to the deployed
signer using a captured live signature.

**Pinned public key.** The deployed key (SSM `/orion/ed25519_public_key`, not
secret) is **`orion-ed25519-v1`** =
`OJQ2E7ZAFClOCM4S4/5QzLeQjjEMSZjPiGMzbiLZdWs=`
(hex `38943613b64014294e08ce12e3fe50ccb7908e310c4998cf8863336e22d9756b`). It is
embedded in `OrionUpdater.exe` at build time via the CMake defines
`ORION_UPDATE_ED25519_PUBKEY` / `ORION_UPDATE_ED25519_PUBKEY_ID`. Additional /
rotated keys can also be supplied (resolved by `public_key_id`) via
`--pubkeys-file`, the `ORION_UPDATE_PUBKEYS` env var, or an `update_pubkeys.json`
next to the executable: `{ "keys": { "<id>": "<hex or base64 32-byte key>" } }`.

Verification uses the bundled OpenSSL `libcrypto` loaded at runtime; if it (or the
public key) is unavailable, the updater **fails closed**. The updater **refuses
to update** unless ALL hold: `signature_alg == "ed25519"`, `public_key_id`
resolves to a trusted key, the signature is valid over the canonical bytes, the
artifact SHA-256 matches, the URL is HTTPS, the zip contains no unsafe paths, and
the version is not a downgrade (unless `allow_rollback`).

## Networking / Cloudflare

`api.zaeorion.com` is fronted by Cloudflare with Bot Fight Mode, which blocks
bare/curl-style user agents. The launcher and updater therefore send
`User-Agent: OrionLauncher/<version> (Windows NT 10.0; Win64; x64)` on every API
call (`LicenseClient` and the `OrionUpdater` manifest fetch), which passes the
filter. All endpoints are HTTPS.

## Update flow (OrionUpdater.exe)

1. Launcher starts `OrionUpdater.exe` with `--manifest-url https://api.zaeorion.com/api/update`,
   `--install-dir <dir>`, `--launcher-pid <pid>`, `--current-version <ver>`,
   `--relaunch OrionNative.exe`, then exits.
2. Updater waits (≤30 s) for the launcher PID to close.
3. Downloads the manifest (**HTTPS enforced**).
4. Verifies: HTTPS source · `signature_alg == ed25519` · `public_key_id` resolves
   to a trusted public key · Ed25519 manifest signature · then, after download,
   the artifact SHA-256 · version is newer (downgrade only if `allow_rollback`).
5. Extracts to a temp staging dir, **validating every zip entry stays inside the
   install dir** (rejects `..` traversal, absolute/drive/UNC paths, symlinks).
6. Backs up the current install.
7. Replaces files from the staging dir.
8. On any apply failure (or a missing relaunch target) restores the backup.
9. Relaunches `OrionNative.exe`.

Nothing downloaded is executed until every verification gate in step 4–5 passes.
A log is written to `<install-dir>/orion_updater.log` (no secrets).

## Artifact packaging

The artifact is a zip of the stripped runtime package (the output of
`tools/package_orion_release.py --strict`, which already contains a fresh
`release_manifest.json`). After replacement + relaunch, `SecurityCore`
re-verifies `release_manifest.json`, so the artifact must carry its own matching
manifest. `OrionUpdater.exe` itself is part of the shipped package.

## Flags

`OrionUpdater.exe` options: `--manifest-url` / `--manifest-file` (local, for
testing) · `--install-dir` · `--launcher-pid` · `--relaunch` · `--current-version`
· `--pubkeys-file` (trusted Ed25519 public keys JSON) · `--dry-run` (verify +
extract, no replace) · `--no-relaunch`.

## Tests

`native_orion/tests/OrionUpdaterTests.cpp` (ctest target `OrionUpdaterTests`)
covers semantic-version comparison, manifest parsing (+ field variants + bad
JSON), the byte-exact compact-JSON canonical (`canonicalMatchesServerBytes`) and
a captured-live-signature cross-check against the deployed signer
(`verifiesLiveBackendManifestVector`), the **Ed25519** primitive round-trip and
manifest verification — valid signature passes; tampered url/sha256/version/policy
fails; wrong public key fails; missing `public_key_id` fails; unsupported
`signature_alg` fails — plus
artifact SHA-256 verify/mismatch, update/downgrade/min-version policy,
path-traversal rejection (interior `..` and absolute paths that `QZipReader`
passes through), symlink rejection, clean extraction, and backup/replace/rollback.
