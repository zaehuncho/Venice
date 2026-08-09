<#
.SYNOPSIS
Install a build-bundled Remote Play backend for Orion.

.DESCRIPTION
Copies Chiaki/chiaki-ng into vendor\chiaki so PyInstaller can include it in
Orion builds. Sony PS Remote Play and Xbox app are detected at runtime, but are
not copied by this script.

.EXAMPLE
.\tools\install_remote_play_backend.ps1 -ChiakiExe C:\Tools\chiaki-ng\chiaki-ng.exe

.EXAMPLE
.\tools\install_remote_play_backend.ps1 -Auto
#>

param(
    [string]$ChiakiExe = "",
    [switch]$Auto,
    [switch]$VerifyOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$VendorChiaki = Join-Path $Root "vendor\chiaki"

function Find-Chiaki {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path -LiteralPath $Explicit -PathType Leaf)) {
        return (Resolve-Path -LiteralPath $Explicit).Path
    }
    foreach ($name in @("chiaki-ng.exe", "chiaki.exe", "chiaki4deck.exe")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Path -LiteralPath $cmd.Source -PathType Leaf)) {
            return $cmd.Source
        }
    }
    $roots = @(
        "$env:ProgramFiles\Chiaki",
        "$env:ProgramFiles\chiaki-ng",
        "${env:ProgramFiles(x86)}\Chiaki",
        "$env:LOCALAPPDATA\Chiaki",
        "$Root\vendor\chiaki"
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    foreach ($base in $roots) {
        $hit = Get-ChildItem -LiteralPath $base -Recurse -File -Include chiaki-ng.exe,chiaki.exe,chiaki4deck.exe -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return ""
}

function Copy-BackendFolder {
    param([string]$Exe)
    $srcDir = Split-Path -Parent $Exe
    New-Item -ItemType Directory -Force -Path $VendorChiaki | Out-Null
    Copy-Item -LiteralPath (Join-Path $srcDir "*") -Destination $VendorChiaki -Recurse -Force
    return Join-Path $VendorChiaki (Split-Path -Leaf $Exe)
}

function Test-OrionRemotePlayBackend {
    Push-Location $Root
    try {
        & ".\.venv311\Scripts\python.exe" ".\tools\verify_remote_play_backend.py"
        if ($LASTEXITCODE -ne 0) {
            throw "Remote Play backend verification failed."
        }
    } finally {
        Pop-Location
    }
}

if ($VerifyOnly) {
    Test-OrionRemotePlayBackend
    exit 0
}

$chiaki = Find-Chiaki -Explicit $ChiakiExe
if (-not $chiaki) {
    if ($Auto) {
        Write-Error "Chiaki/chiaki-ng was not found. Install it, add it to PATH, or pass -ChiakiExe."
    } else {
        Write-Error "Pass -ChiakiExe or use -Auto after installing chiaki-ng."
    }
}

$installed = Copy-BackendFolder -Exe $chiaki
Write-Host "[+] Installed Remote Play backend:" $installed -ForegroundColor Green
Test-OrionRemotePlayBackend
