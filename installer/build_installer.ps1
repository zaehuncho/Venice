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
    [switch]$AllowUnsigned
)

$ErrorActionPreference = "Stop"
$InstallerDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $InstallerDir

if ([string]::IsNullOrWhiteSpace($PackageDir)) {
    $PackageDir = Join-Path $Root "release\orion-package"
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
foreach ($required in @("OrionNative.exe", "OrionSidecar.exe", "security_policy.json",
                        # WAVE 3 (2026-08-08): the C++ packet-bridge service is a required
                        # payload — orion.iss registers packet_bridge\VeniceNetSvc.exe as
                        # the VeniceNetSvc service; a package without it would fail-soft
                        # into "Meter Delay unavailable" on every customer machine.
                        "packet_bridge\VeniceNetSvc.exe",
                        "packet_bridge\WinDivert64.dll",
                        "packet_bridge\WinDivert64.sys")) {
    if (-not (Test-Path -LiteralPath (Join-Path $PackageDir $required))) {
        throw "Package is incomplete (missing $required): $PackageDir`nA pre-wave-3 package (Nuitka NexusVisionSvc bundle) cannot build this installer - re-run tools\package_orion_release.py."
    }
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
} else {
    Write-Warning "No -SignToolCommand: the installer exe will be UNSIGNED (SmartScreen/Defender will flag it). Supply the EV signing command for a shippable build."
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
    $ServiceChecks = @(
        'create VeniceNetSvc binPath= ',
        'packet_bridge\VeniceNetSvc.exe',
        '--arm-meter-delay',
        'ORION_METER_DELAY_ARMED=1',
        'RemoveBridgeService(''NexusVisionSvc'')',
        '(A;;CCLCSWRPWPLOCRRC;;;IU)',
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
} else {
    Write-Warning "Preprocessed script not emitted ($Preprocessed missing) - service-registration read-back skipped."
}
Write-Host "[orion-installer] OK"
