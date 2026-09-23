# Orion Client Update + OrionUpdater.exe

This documents the **client** side of Orion self-update: the `/api/update`
contract the launcher consumes, the manifest signing scheme `OrionUpdater.exe`
verifies, and the update flow. The server side (`/api/update`, the S3/CloudFront
artifact host, and signing) is built separately — see `docs/SERVER_HANDOFF.md`.

## Components

| Piece | Role |
|-------|------|
| `OrionNative.exe` | Main launcher. Checks `/api/update` at startup **before AuthGate** (blocking gate, fail-soft), auto-proceeds into the updater after a countdown, and keeps the Patch pill live for later checks. |
| `OrionUpdater.exe` | Installed first stage verifies the signed old runtime, stages its own EXE and complete declared DLL/Qt-plugin closure in a sibling directory, starts that copy, then exits. The staged second stage waits for install owners, verifies the signed update and both release inventories, applies/rolls back, and relaunches. |
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
rotated keys can also be supplied **in development builds only** (resolved by
`public_key_id`) via `--pubkeys-file`, the `ORION_UPDATE_PUBKEYS` env var, or an
`update_pubkeys.json`. **Production builds (`ORION_PRODUCTION_BUILD`) trust only the
embedded key**: every external source is ignored and named in the updater log, and
`--manifest-file` is refused. Rationale (2026-09-21): an unprivileged local process
can set a user environment variable or drop a file next to the install; letting
either become the trust root of an elevated updater would be a signed-update bypass
and a privilege escalation. Key rotation in production ships the new embedded key
through a signed update (embed both ids during the transition).
## Install location (2026-09-21)

The installer fixes the install directory under Program Files (`installer/orion.iss`), and the
updater refuses any install root that is a reparse point (symlink, junction, mount point, cloud
placeholder) or, in production, any directory other than its own. Cloud-backed or redirected trees
(OneDrive, Dev Drive junctions, roaming redirects) are therefore unsupported as an install location
by construction: an update there fails closed with "reparse point" in `orion_updater.log`. A
network (UNC) install root is refused outright ("not supported") in every build profile.

## Relaunch and build attestation (2026-09-21)

- `--relaunch` is **ignored in production**: the updater relaunches `OrionNative.exe` from its
  own directory and nothing else (an override attempt is written to `orion_updater.log`).
  Development accepts a bare `*.exe` name only; the resolved path must be a regular file
  directly under the install root with no reparse point on the way, or no relaunch happens.
- `OrionUpdater.exe --build-profile <new file under %TEMP%>` writes the build attestation:
  `profile`, `version`, `embedded_key_ids`, and `embedded_keys` (id + SHA-256 of the 32 raw
  public-key bytes). `tools/package_orion_release.py` refuses a production package unless the
  profile is `production`, `--manifest-file` is disabled, every embedded key is listed in its
  `APPROVED_UPDATE_KEYS` with the matching fingerprint, and the staged `OrionUpdater.exe`
  is byte-identical to the binary that attested.

## Key rotation and compromise (2026-09-21)

**Planned rotation** (no compromise):
1. Generate the new Ed25519 pair server-side; store the private half in SSM next to the
   current one. Never on a workstation.
2. Build a release with BOTH public keys embedded:
   `-DORION_UPDATE_ED25519_PUBKEY2_ID=orion-ed25519-v2 -DORION_UPDATE_ED25519_PUBKEY2=<base64>`.
   Sign that release's manifest with the CURRENT key (every installed client trusts it).
3. Once the fleet is on that release (`minimum_supported_version` can force it), switch
   the server signer to the new key id.
4. The following release embeds only the new key (primary) and the old pair is retired.
   `OrionUpdater.exe --build-profile out.json` lists `embedded_key_ids` for any binary.

**Signing key on disk (2026-09-21)**: the private key may stay encrypted (PKCS8
"ENCRYPTED PRIVATE KEY" or a legacy passphrase PEM). `tools/package_orion_release.py` reads the
passphrase from `ORION_UPDATE_SIGNING_KEY_PASSPHRASE` or, when that is unset and the run is
interactive, from a hidden prompt; it is never accepted on the command line, never logged, and
the key is decrypted in memory only. The key file itself is named by `--signing-key` or
`ORION_UPDATE_SIGNING_KEY_PEM` (a path; `scripts/verify_orion.ps1 -StrictSecurity` uses the
env form) and must live outside the package tree.

**Compromised signing key**: the compromised key alone is not enough to push an update —
the launcher fetches the manifest from the pinned HTTPS endpoint only, so an attacker also
needs the endpoint (or its DNS + a valid certificate). Procedure, in order:
1. Rotate the server signer to a new key immediately and publish a **mandatory** update,
   signed with the OLD key (still trusted by installed clients), whose binary embeds ONLY
   the new key. Set `minimum_supported_version` past the compromised builds.
2. Revoke/disable the compromised key material in SSM; audit manifest publishes.
3. Until the fleet has moved, the endpoint is the control: the global kill switch and
   `minimum_supported_version` stop compromised-era clients from running.
4. Clients that missed the window (offline for the whole transition) reinstall from the
   signed installer; that is the recovery root.

Rotated keys were previously accepted from
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

1. Launcher starts the installed `OrionUpdater.exe` with `--manifest-url https://api.zaeorion.com/api/update`,
   `--install-dir <dir>`, `--launcher-pid <pid>`, `--current-version <ver>`,
   `--relaunch OrionNative.exe`, then exits.
2. The installed first stage verifies every file against the exact-root signed
   `release_manifest.json`, copies its EXE, all manifested root DLLs, and the
   required Qt-plugin families into a verified sibling `.orion_updater_stage-*`
   directory, starts the copied helper, then exits. Production rejects a
   foreign `--install-dir`; the second stage accepts only that sibling layout
   and re-verifies the copied closure against the signed old manifest.
3. The staged helper waits (≤30 s each) for the launcher and bootstrap PIDs,
   then for every observable install-local process/service or mapped DLL owner.
   Timeout/refused inspection aborts before backup or destination mutation.
4. Downloads the manifest (**HTTPS enforced**).
5. Verifies: HTTPS source · `signature_alg == ed25519` · `public_key_id` resolves
   to a trusted public key · Ed25519 manifest signature · then, after download,
   the artifact SHA-256 · version is newer (downgrade only if `allow_rollback`).
6. Extracts to a temp staging dir, rejecting traversal, absolute/drive/UNC
   paths and links. Both old and new exact-root release manifests must verify
   before the install changes.
7. Rechecks ownership, backs up the current install, overlays the new runtime,
   and removes only files named in the verified old manifest but absent from
   the verified new one. Unrelated user data stays in place.
8. Checks the installed new manifest and relaunch target. On any apply, prune,
   integrity or relaunch-target failure it restores the exact old backup.
9. Relaunches `OrionNative.exe`. A bounded installed-helper cleanup removes
   the sibling stage after the staged process exits.

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
extract, no replace) · `--no-relaunch`. The local `--artifact-file`,
`--stage-runtime`, `--exit-on-complete` and fault-injection fixture flags are
development-only; production refuses them. `--staged-helper`, `--bootstrap-pid`
and `--cleanup-stage` are internal handoff flags, with root/layout checks.

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
`tests/test_updater_e2e_disposable.py` runs a signed offline update and an
injected-failure rollback on copied install trees. It changes updater/Core/Qt
DLL hashes, retires a manifested file, preserves user data, checks the PID
barrier, and confirms the external helper stage is cleaned. It never starts
`OrionNative.exe` or calls the update service.
