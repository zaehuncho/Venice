param(
    [string]$Python = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

# [ORION_NEXUS_SERVICE_BUILD] Nuitka-compile nexus_svc.py -> NexusVisionSvc.exe, the exact
# same standalone pattern as scripts/build_orion_sidecar.ps1. This is the elevated inbound
# meter-delay packet bridge. A loose nexus_svc.py + .venv311 interpreter MUST NEVER ship
# (docs/IP_PROTECTION_PLAN.md:360; tools/release_filter_policy bans this subsystem's dev
# artifacts) — the compiled exe is the only customer host, registered by installer/orion.iss
# as the demand-start LocalSystem service NexusVisionSvc.
#
# The build interpreter must have Nuitka AND the runtime deps the bridge imports: pywin32
# (win32service/win32serviceutil/servicemanager/win32security/ntsecuritycon) and pydivert.
# pip install: <python.exe> -m pip install -r requirements-build.txt pywin32 pydivert
function Resolve-ServicePython {
    param([string]$Preferred)

    $candidates = @()
    if (-not [string]::IsNullOrWhiteSpace($Preferred)) {
        $candidates += $Preferred
        $cmd = Get-Command $Preferred -ErrorAction SilentlyContinue
        if ($cmd) { $candidates += $cmd.Source }
    }
    $candidates += (Join-Path $Root ".venv\Scripts\python.exe")
    $onPath = Get-Command python -ErrorAction SilentlyContinue
    if ($onPath) { $candidates += $onPath.Source }
    $candidates += (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" `
                        -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName })

    $seen = @{}
    $existing = @()
    foreach ($c in $candidates) {
        if ([string]::IsNullOrWhiteSpace($c)) { continue }
        if (-not (Test-Path -LiteralPath $c)) { continue }
        $full = (Resolve-Path -LiteralPath $c).Path
        if ($seen.ContainsKey($full)) { continue }
        $seen[$full] = $true
        $existing += $full
        # Require Nuitka AND the two runtime deps the frozen service links.
        & $full -c "import nuitka, pydivert, win32serviceutil, servicemanager" 2>$null
        if ($LASTEXITCODE -eq 0) { return $full }
    }

    if ($existing.Count -eq 0) {
        throw "No Python interpreter found. Pass -Python <python.exe>."
    }
    throw ("None of these interpreters has Nuitka + pywin32 + pydivert: " + ($existing -join ", ") +
           ". Install with: <python.exe> -m pip install -r requirements-build.txt pywin32 pydivert")
}

$Python = Resolve-ServicePython -Preferred $Python
Write-Host "[nexus-service] using interpreter: $Python"

$OutputDir = Join-Path $Root "build\service"
$Dist = Join-Path $OutputDir "nexus_svc.dist"
$Exe = Join-Path $Dist "NexusVisionSvc.exe"
$Source = Join-Path $Root "nexus_svc.py"

function Test-ServiceBundleFresh {
    if (-not (Test-Path -LiteralPath $Exe)) { return $false }
    $srcTime = (Get-Item -LiteralPath $Source).LastWriteTimeUtc
    $exeTime = (Get-Item -LiteralPath $Exe).LastWriteTimeUtc
    return $exeTime -ge $srcTime
}

if (-not $Force -and (Test-ServiceBundleFresh)) {
    Write-Host "[nexus-service] current bundle is fresh; rebuild skipped (pass -Force to rebuild)"
    exit 0
}

if ($Force -and (Test-Path -LiteralPath $Dist)) {
    Remove-Item -LiteralPath $Dist -Recurse -Force
}
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

# pywin32's service framework is imported lazily in places; name every module the frozen
# exe needs so Nuitka does not prune them. --include-package-data=pydivert bundles the
# WinDivert64.dll/.sys pair beside the compiled pydivert package.
& $Python -m nuitka `
    --standalone `
    --assume-yes-for-downloads `
    --include-package=pydivert `
    --include-package-data=pydivert `
    --include-module=win32service `
    --include-module=win32serviceutil `
    --include-module=win32event `
    --include-module=servicemanager `
    --include-module=win32api `
    --include-module=win32con `
    --include-module=win32security `
    --include-module=ntsecuritycon `
    --include-module=win32process `
    --include-module=pywintypes `
    --windows-console-mode=disable `
    --output-dir=$OutputDir `
    -o NexusVisionSvc.exe `
    nexus_svc.py
if ($LASTEXITCODE -ne 0) {
    throw "NexusVisionSvc Nuitka build failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath $Exe)) {
    throw "NexusVisionSvc.exe was not produced at $Exe"
}

# The bridge cannot open a packet handle without the WinDivert pair. pydivert's loader
# resolves WinDivert64.dll from its own package dir (pydivert/windivert_dll/__init__.py:
# DLL_PATH = dirname(__file__)/WinDivert64.dll), and Nuitka's --include-package-data
# EXCLUDES .dll files, so the dist can carry the .sys while silently missing the .dll —
# the service then dies the moment it opens a packet handle (bit us 2026-08-08).
# Require BOTH files, each beside the other; source any missing one from the committed
# vendor override into the directory the loader actually reads.
$wdSys = Get-ChildItem -LiteralPath $Dist -Recurse -Filter "WinDivert64.sys" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($wdSys) { $wdDir = $wdSys.DirectoryName } else { $wdDir = $Dist }
$vendor = Join-Path $Root "vendor\windivert"
foreach ($name in @("WinDivert64.dll", "WinDivert64.sys")) {
    $dest = Join-Path $wdDir $name
    if (Test-Path -LiteralPath $dest) { continue }
    $src = Join-Path $vendor $name
    if (-not (Test-Path -LiteralPath $src)) {
        throw "WinDivert bundle missing: neither pydivert nor $vendor supplied $name. The packager also enforces this."
    }
    Copy-Item -LiteralPath $src -Destination $dest -Force
    Write-Host "[nexus-service] $name sourced from vendor override -> $dest"
}
Write-Host "[nexus-service] WinDivert pair present at $wdDir"

Write-Host "[nexus-service] production bundle ready: $Exe"
