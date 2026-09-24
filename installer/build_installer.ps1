# Build the Orion installer from the packaged release output.
# -----------------------------------------------------------------------------
# Replaces the hand-typed TODO steps in orion.iss with a fail-loud pipeline:
#   1. verifies release\orion-package is a real packager output (release_manifest.json,
#      and release_manifest.sig unless -AllowUnsigned)
#   2. reads the version FROM THE PACKAGE MANIFEST (single source of truth: the
#      packager derives it from native_orion/CMakeLists.txt PROJECT_VERSION)
#   3. verifies every redist against the pinned SHA-256 table (installer\README.md)
#   4. invokes iscc with /DMyAppVersion + /DSourcePackageDir (+ signing when supplied)
#
# Usage (unsigned local build):
#   powershell -NoProfile -ExecutionPolicy Bypass -File installer\build_installer.ps1 -AllowUnsigned
# Production (owner supplies the EV signing command; $f is Inno's file placeholder):
#   ... -SignToolCommand 'signtool.exe sign /fd SHA256 /a /tr http://timestamp.digicert.com /td SHA256 $f'
param(
    [string]$PackageDir = "",
    [string]$Iscc = "",
    [string]$SignToolCommand = "",
    [string]$Python = "",
    [switch]$AllowUnsigned
)

$ErrorActionPreference = "Stop"
$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $InstallerDir

if ([string]::IsNullOrWhiteSpace($PackageDir)) {
    $PackageDir = Join-Path $Root "release\orion-package-packed"
}
$PackageDir = [System.IO.Path]::GetFullPath($PackageDir)

# --- 1. The source must be a real, complete packager output. -------------------------
$ManifestPath = Join-Path $PackageDir "release_manifest.json"
if (-not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Not a packager output (missing release_manifest.json): $PackageDir`nRun tools\package_orion_release.py first."
}
if (-not (Test-Path -LiteralPath (Join-Path $PackageDir "release_manifest.sig"))) {
    if (-not $AllowUnsigned) {
        throw "release_manifest.sig missing from $PackageDir - this is an UNSIGNED dev package. Re-package with --signing-key, or pass -AllowUnsigned for a local test build (never ship it)."
    }
    Write-Warning "release_manifest.sig missing: building an UNSIGNED DEV installer. Never ship this artifact."
}
$Required = @("OrionNative.exe", "OrionSidecar.exe", "security_policy.json")
# [2026-09-24 owner] No packet-level driver ships: the packet bridge (VeniceNetSvc +
# WinDivert) is retired with meter delay. A package that still carries it is refused.
foreach ($bridge in @("packet_bridge\VeniceNetSvc.exe", "packet_bridge\WinDivert64.dll",
                      "packet_bridge\WinDivert64.sys")) {
    if (Test-Path -LiteralPath (Join-Path $PackageDir $bridge)) {
        throw "Package still contains the retired packet bridge ($bridge); re-run the packager."
    }
}

# SERVER-SHARD: when the packed package carries the unpacked activation broker, the
# packed inner payload MUST also be present (orion.iss registers the broker as the
# orion:// handler + launch target, and the bootstrap staged as OrionNative.exe needs
# OrionNative.packed.exe to decrypt). A broker-without-payload package is incomplete.
$IsServerShard = Test-Path -LiteralPath (Join-Path $PackageDir "OrionActivate.exe")
if ($IsServerShard) {
    $Required += "OrionActivate.exe"
    $Required += "OrionNative.packed.exe"
    # The broker's own import set. Windows resolves these BEFORE the broker's first
    # instruction runs, so a shard package missing any of them cannot activate at all
    # (and an installer built from it would ship a dead launch target).
    $Required += "SecurityCore.dll"
    $Required += "Qt6Core.dll"
    $Required += "Qt6Network.dll"
    $Required += "libcrypto-3-x64.dll"
    Write-Host "[orion-installer] server-shard package detected (OrionActivate.exe present)"
}

foreach ($required in $Required) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir $required))) {
        throw "Package is incomplete (missing $required): $PackageDir`nA pre-wave-3 package (Nuitka NexusVisionSvc bundle) or an incomplete server-shard chain cannot build this installer - re-run tools\package_orion_release.py / tools\security\pack_lethe_release.py --server-shard."
    }
}

# --- 1b. CRYPTOGRAPHIC verification of the package, before anything is compiled. -----
# Presence checks prove nothing about integrity: ISCC would happily compile (and the
# owner would happily EV-sign) an installer built from a tampered or internal-audience
# package. The pinned-key customer verifier is the gate - it validates the Ed25519
# manifest signature, every file hash, manifest COVERAGE (zero unmanifested runtime
# files), audience=customer, absence of Owner/Staff, the fail-closed security policy,
# and profile-aware executable admission. It is read-only: it never re-signs.
if (-not $AllowUnsigned) {
    if ([string]::IsNullOrWhiteSpace($Python)) {
        $Python = @("C:\Python314\python.exe", "C:\Python313\python.exe", "C:\Python312\python.exe") |
            Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
        if ([string]::IsNullOrWhiteSpace($Python)) {
            $pyCmd = Get-Command "python.exe" -ErrorAction SilentlyContinue
            if ($pyCmd) { $Python = $pyCmd.Source }
        }
    }
    if ([string]::IsNullOrWhiteSpace($Python) -or -not (Test-Path -LiteralPath $Python)) {
        throw "Python not found for the package verification gate. Pass -Python <path to python.exe> (or -AllowUnsigned for a local dev build that is never shipped)."
    }
    $VerifyArgs = @((Join-Path $Root "tools\security\pack_lethe_release.py"), "--verify-only", "--input", $PackageDir)
    if ($IsServerShard) { $VerifyArgs += "--server-shard" }
    Write-Host "[orion-installer] verifying package against the pinned release key..."
    & $Python @VerifyArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Package verification FAILED (exit $LASTEXITCODE) for $PackageDir - refusing to build an installer from an unverified package. Re-run tools\security\pack_lethe_release.py."
    }
    if (-not $IsServerShard) {
        # The installer must contain an EXACT signed file inventory: no extra DLL
        # or Qt plugin, even if a local development runtime permits diagnostics.
        $ExactInventoryArgs = @((Join-Path $Root "tools\verify_release_integrity.py"),
                                "--package", $PackageDir, "--strict-warnings")
        & $Python @ExactInventoryArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Package has unmanifested or invalid runtime files (exit $LASTEXITCODE): $PackageDir"
        }
    }
    Write-Host "[orion-installer] package verification OK"
} else {
    Write-Warning "-AllowUnsigned: skipping the pinned-key package verification gate. Never ship this artifact."
}

# --- 2. Version from the signed manifest (never hand-typed). -------------------------
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
$Version = [string]$Manifest.version
if ([string]::IsNullOrWhiteSpace($Version)) {
    # Older manifests may omit version; fall back to the same source the packager uses.
    $CMakeText = Get-Content -LiteralPath (Join-Path $Root "native_orion\CMakeLists.txt") -Raw
    if ($CMakeText -match 'project\(\s*OrionNative\s+VERSION\s+([0-9]+(?:\.[0-9]+)*)') {
        $Version = $Matches[1]
        Write-Warning "release_manifest.json carries no version; using CMake PROJECT_VERSION $Version"
    } else {
        throw "No version in release_manifest.json and PROJECT_VERSION not parseable from native_orion\CMakeLists.txt"
    }
}
if ($Version -notmatch '^[0-9]+(\.[0-9]+)*$') {
    throw "Refusing non-numeric version '$Version'"
}

# --- 3. Redists: exact pinned bytes (table also in installer\README.md). -------------
# ViGEmBus/HidHide are vendor-signed nefarius releases; a swapped binary here would run
# elevated on every customer machine, so the hash check is mandatory, not advisory.
$RedistPins = @{
    "ViGEmBus_1.22.0_x64_x86_arm64.exe" = "89220a7865076b342892f98865f3499fb7c4cfd673159e89d352c360fd014c6a";
    "HidHide_1.5.230_x64.exe"           = "f4bbbcb82e6258641b887c74bc81c4c5f66e4aa811808dfc304347687b7605f6";
}
foreach ($name in $RedistPins.Keys) {
    $path = Join-Path $InstallerDir "redist\$name"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Missing redist $name - download per installer\README.md (redists are gitignored)."
    }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $RedistPins[$name]) {
        throw "SHA-256 mismatch for ${name}:`n  expected $($RedistPins[$name])`n  actual   $actual`nRe-download from the official nefarius release and re-verify."
    }
}
# vc_redist.x64.exe is REQUIRED (the package has no app-local CRT; see orion.iss). It is
# updated by Microsoft in place, so pin trust to its Authenticode signature, not a hash.
$VcRedist = Join-Path $InstallerDir "redist\vc_redist.x64.exe"
if (-not (Test-Path -LiteralPath $VcRedist)) {
    throw "Missing redist vc_redist.x64.exe - download https://aka.ms/vs/17/release/vc_redist.x64.exe into installer\redist\ (required: the package carries no msvcp140/vcruntime140)."
}
$VcSig = Get-AuthenticodeSignature -LiteralPath $VcRedist
if ($VcSig.Status -ne 'Valid' -or $VcSig.SignerCertificate.Subject -notmatch 'Microsoft') {
    throw "vc_redist.x64.exe failed Authenticode validation (status $($VcSig.Status), signer $($VcSig.SignerCertificate.Subject)) - re-download from Microsoft."
}

# --- 4. Compile. ---------------------------------------------------------------------
if ([string]::IsNullOrWhiteSpace($Iscc)) {
    $isccCmd = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if ($isccCmd) {
        $Iscc = $isccCmd.Source
    } else {
        $Iscc = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
    if ([string]::IsNullOrWhiteSpace($Iscc)) {
        throw "iscc.exe not found. Install Inno Setup 6+ (https://jrsoftware.org/isinfo.php) or pass -Iscc <path>."
    }
}

$IssPath = Join-Path $InstallerDir "orion.iss"
# EmitPreprocessed makes orion.iss SaveToFile the fully-translated script into
# Output\orion.preprocessed.iss — the offline read-back artifact used to verify the
# service-registration strings that actually went into the compiled installer.
$OutputDir = Join-Path $InstallerDir "Output"
New-Item -ItemType Directory -Force $OutputDir | Out-Null
$IsccArgs = @(
    "/DMyAppVersion=$Version",
    "/DSourcePackageDir=$PackageDir",
    "/DEmitPreprocessed"
)
if (-not [string]::IsNullOrWhiteSpace($SignToolCommand)) {
    $IsccArgs += "/DSignToolName=signtool"
    $IsccArgs += "/Ssigntool=$SignToolCommand"
} elseif ($AllowUnsigned) {
    Write-Warning "No -SignToolCommand: building an UNSIGNED DEV installer (SmartScreen/Defender will flag it). Never ship this artifact."
} else {
    # [2026-09-21 release gate] An unsigned installer exe is a release blocker, the same
    # way an unsigned package manifest is (step 1 above). Make the two gates agree:
    # unsigned output only ever comes from an explicit -AllowUnsigned local build.
    throw "No -SignToolCommand supplied: refusing to build an UNSIGNED installer. Supply the EV signing command for a shippable build, or pass -AllowUnsigned for a local test build (never ship it)."
}
$IsccArgs += $IssPath

Write-Host "[orion-installer] version $Version"
Write-Host "[orion-installer] package $PackageDir"
Write-Host "[orion-installer] iscc    $Iscc"
& $Iscc @IsccArgs
if ($LASTEXITCODE -ne 0) {
    throw "iscc failed with exit code $LASTEXITCODE"
}
# OutputBaseFilename in orion.iss is VeniceSetup-<version> (the customer-facing name).
$Artifact = Join-Path $InstallerDir "Output\VeniceSetup-$Version.exe"
if (-not (Test-Path -LiteralPath $Artifact)) {
    throw "iscc reported success but the artifact is missing: $Artifact"
}
$hash = (Get-FileHash -LiteralPath $Artifact -Algorithm SHA256).Hash.ToLowerInvariant()
Write-Host "[orion-installer] artifact $Artifact"
Write-Host "[orion-installer] sha256   $hash"

# --- 5. Read back the service registration from the compiled script. -----------------
# The preprocessed script is what iscc actually compiled. A rebrand regression here
# (service re-registered under the legacy name, arm env var dropped, DACL lost) must
# fail the BUILD, not surface on a customer machine.
$Preprocessed = Join-Path $OutputDir "orion.preprocessed.iss"
if (Test-Path -LiteralPath $Preprocessed) {
    $Script = Get-Content -LiteralPath $Preprocessed -Raw
    # [2026-09-24] The bridge is retired: the installer must REMOVE any earlier
    # registration and must never create one again.
    foreach ($forbidden in @('create VeniceNetSvc', '--arm-meter-delay', 'ORION_METER_DELAY_ARMED=1')) {
        if ($Script.Contains($forbidden)) {
            throw "Compiled installer script still registers the retired packet bridge: $forbidden"
        }
    }
    $ServiceChecks = @(
        'RetireLegacyPacketBridge',
        'RemoveBridgeService(''NexusVisionSvc'')',
        # Uninstall path (2026-08-08): both bridge names removed at usUninstall
        # (stop -> poll STOPPED -> delete), the WinDivert safety-net stop, the
        # ProgramData token cleanup, and the interactive user-data choice.
        'CurUninstallStepChanged',
        'RemoveBridgeService(''VeniceNetSvc'')',
        'stop WinDivert',
        'nexus_bridge.token',
        'NexusVision\Orion Native'
    )
    foreach ($needle in $ServiceChecks) {
        if (-not $Script.Contains($needle)) {
            throw "Compiled installer script is missing the expected service-registration string: $needle"
        }
    }
    Write-Host "[orion-installer] service registration verified in $Preprocessed"

    # SERVER-SHARD: the compiled script must point the launch target / shortcuts / protocol
    # registration at the broker, or first launch of a shard build cannot activate.
    if ($IsServerShard) {
        $ShardChecks = @(
            '{app}\OrionActivate.exe',                  # launch target + protocol registration
            'RegisterActivationBroker',                 # the [Code] install hook that calls --register
            'Activation broker registration failed'     # registration failure is FATAL on a shard build
        )
        foreach ($needle in $ShardChecks) {
            if (-not $Script.Contains($needle)) {
                throw "Server-shard installer is missing the expected broker launch/registration string: $needle"
            }
        }
        Write-Host "[orion-installer] server-shard broker launch/registration verified in $Preprocessed"
    }
} else {
    # [2026-09-21] The read-back is the build's own proof that the compiled installer still
    # registers the service correctly. Skipping it silently printed OK on an unverified
    # artifact (Astra, bug sweep). /DEmitPreprocessed is always passed above, so a missing
    # file means iscc did not do what we asked -- fail, do not warn.
    throw "Preprocessed script not emitted ($Preprocessed missing): the service-registration read-back could not run, so this installer is UNVERIFIED. Do not ship it."
}
Write-Host "[orion-installer] OK"
