# Code Signing & Release Verification

## Overview

All NexusVision release binaries must be Authenticode-signed before distribution. This document describes the signing flow, key management, and verification requirements.

---

## What Must Be Signed Before Release

| Artifact | Required | Notes |
|----------|----------|-------|
| `Orion.exe` (main binary) | YES | Primary application executable |
| `WinDivert64.dll` | YES | Kernel-mode driver (already vendor-signed) |
| `WinDivert64.sys` | YES | Must be Microsoft cross-signed |
| Installer/MSIX package | YES | SmartScreen reputation requires EV cert |
| Update manifest (`.json.sig`) | YES | Ed25519 publisher signature |
| Python wheel (if distributed) | OPTIONAL | Only for developer builds |

---

## Windows Authenticode Signing

### Prerequisites

- **EV Code Signing Certificate** from a trusted CA (DigiCert, Sectigo, GlobalSign)
- **Hardware token** (USB HSM) storing the private key (required for EV)
- **SignTool.exe** from Windows SDK
- **PowerShell** for automation

### Signing Command

```powershell
# Standard signing with SHA-256
signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  /sha1 <THUMBPRINT> "Orion.exe"

# Verify signature
signtool verify /pa /v "Orion.exe"

# PowerShell verification
Get-AuthenticodeSignature "Orion.exe" | Format-List
```

### Dual-Signing (SHA-1 + SHA-256) for Legacy Support

```powershell
# SHA-1 primary (legacy Windows 7)
signtool sign /fd SHA1 /t http://timestamp.digicert.com ^
  /sha1 <THUMBPRINT> "Orion.exe"

# SHA-256 append signature
signtool sign /as /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
  /sha1 <THUMBPRINT> "Orion.exe"
```

---

## Signed Update Flow

### Update Manifest Signing

The update manifest is signed with the project's Ed25519 publisher key:

```
1. Build release artifacts
2. Compute SHA-256 hash of each file
3. Construct manifest.json with version, files, hashes
4. Sign manifest canonical form with Ed25519 private key
5. Embed signature in manifest["signature"] field
6. Authenticode-sign the final .exe/.dll artifacts
7. Upload manifest + signed artifacts to update server
```

### Client Verification Flow

```
1. Download manifest.json
2. Verify Ed25519 signature against pinned publisher key
3. Check version > current (rollback protection)
4. Download files to temp directory
5. Verify SHA-256 hash of each file against manifest
6. Verify Authenticode signature of .exe/.dll files
7. Apply update (copy files) - never execute downloaded code
8. Record version in history (rollback protection persistence)
```

---

## Release Hash Verification

Each release includes a `checksums.sha256` file:

```
<sha256>  Orion.exe
<sha256>  WinDivert64.dll
<sha256>  settings.json.sig
```

Users can verify with:

```powershell
Get-FileHash -Algorithm SHA256 "Orion.exe" | Format-List
# Compare with published checksum
```

---

## Private Key / Certificate Storage Rules

### NEVER

- Store private keys in source control (`.git`)
- Store private keys on developer workstations without HSM
- Share private keys over email, chat, or unencrypted channels
- Use self-signed certificates for production releases
- Store certificate passwords in plaintext

### ALWAYS

- Store signing private key on hardware token (YubiKey, SafeNet)
- Use CI/CD secret management for automated signing (Azure Key Vault, GitHub Secrets)
- Rotate certificates before expiration (set calendar alert)
- Audit signing operations in a tamper-evident log
- Require 2-person approval for release signing

### Storage Hierarchy

```
Production EV Certificate:
  → Hardware token (physical) → Signing workstation (air-gapped preferred)
  → Azure Key Vault (for CI/CD) → Access via Managed Identity

Ed25519 Publisher Key:
  → Generated offline
  → Private key in Azure Key Vault or hardware token
  → Public key pinned in client binary (nexus_update_verifier.py)
  → Rotate annually, embed both old+new during transition
```

---

## CI/CD Integration

### GitHub Actions Signing (with Azure Key Vault)

```yaml
- name: Sign binary
  uses: azure/login@v1
  with:
    creds: ${{ secrets.AZURE_CREDENTIALS }}

- name: Code sign with AzureSignTool
  run: |
    AzureSignTool sign -kvu ${{ secrets.AZURE_KEY_VAULT_URL }} \
      -kvi ${{ secrets.AZURE_CLIENT_ID }} \
      -kvs ${{ secrets.AZURE_CLIENT_SECRET }} \
      -kvt ${{ secrets.AZURE_TENANT_ID }} \
      -kvc ${{ secrets.AZURE_CERT_NAME }} \
      -tr http://timestamp.digicert.com \
      -td sha256 \
      "dist/Orion.exe"
```

### Local Development (Not Signed)

Development builds are unsigned. The application detects unsigned state and:
- Shows "Development Build" watermark
- Logs warning in audit log
- Does not enforce signature validation on self-modules

---

## Checklist Before Release

- [ ] All `.py` module SHA-256 hashes recorded in manifest
- [ ] `Orion.exe` signed with EV Authenticode certificate
- [ ] `WinDivert64.dll` verified vendor signature intact
- [ ] Update manifest signed with Ed25519 publisher key
- [ ] `checksums.sha256` generated and published
- [ ] Version bumped in `version.json`
- [ ] Rollback protection: new version > all previously deployed versions
- [ ] CI security tests pass (148+ tests, 0 failures)
- [ ] Secret scan: CLEAN
- [ ] Manual review: no hardcoded secrets, no bypass features
