# Framedump recording launcher — pose-model training capture.
#
# WHY A WRAPPER: the sidecar reads ORION_FRAMEDUMP* ONCE, in
# RemotePlayOrchestrator.__init__ (remote_play_orchestrator.py:1168), so framedump
# can only be armed at launch. This sets the env and hands off to the normal
# launcher, which is left untouched (it holds the license key).
#
# ON  -> .\run_orion.framedump.ps1
# OFF -> .\run_orion.local.ps1     (your normal launcher, unchanged)
#
# Output: D:\VeniceTraining\framedump\session_<timestamp>\f#####_0_raw.png
# Routed to D: deliberately — C: hit 0 bytes free on 2026-08-09 and cost a day.
# D: also already holds VeniceTraining, so the pseudo-label pipeline is local to
# the data instead of reading across drives.
#
# These frames are what the CAPTURE CARD sees — same resolution, same compression
# path as live detection — so they match the deployment domain exactly. That is
# the reason to prefer them over PS5 share-button clips for training data.

param(
    # 0.5s = 2 fps. Enough pose variety without piles of near-duplicate frames.
    [double]$IntervalSec = 0.5,
    # Frame cap for the whole session. 12000 @ 0.5s ~= 100 min of play, so a
    # 3-4 game run finishes well inside it rather than truncating mid-session.
    [int]$MaxFrames = 12000,
    # Skip the annotated overlay PNG (halves disk + write cost). Training wants raw.
    [switch]$IncludeAnnotated,
    [string]$OutRoot = 'D:\VeniceTraining\framedump'
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

# --- target drive + disk guard ---------------------------------------------
$driveLetter = (Split-Path -Qualifier $OutRoot).TrimEnd(':')
$drive = Get-PSDrive $driveLetter -ErrorAction SilentlyContinue
if ($null -eq $drive) {
    Write-Host "  Drive $driveLetter`: not found. Pass -OutRoot to pick another location." -ForegroundColor Red
    exit 1
}

# A full 1080p PNG runs ~2 MB; the annotated copy doubles it.
$perFrameMB = 2
if ($IncludeAnnotated) { $perFrameMB = 4 }
$freeGB = [math]::Round($drive.Free / 1GB, 1)
$estGB  = [math]::Round(($MaxFrames * $perFrameMB * 1MB) / 1GB, 1)

Write-Host ""
Write-Host "  FRAMEDUMP RECORDING" -ForegroundColor Cyan
Write-Host ("  output     : {0}" -f $OutRoot)
Write-Host ("  interval   : {0}s  (~{1:N1} fps)" -f $IntervalSec, (1/$IntervalSec))
Write-Host ("  max frames : {0}" -f $MaxFrames)
Write-Host ("  raw only   : {0}" -f (-not $IncludeAnnotated))
Write-Host ("  worst case : ~{0} GB   |   {1}: has {2} GB free" -f $estGB, $driveLetter, $freeGB)
Write-Host ""

# Refuse a run that could fill the drive. Keep 10 GB spare, always.
if ($estGB -gt ($freeGB - 10)) {
    Write-Host "  REFUSING TO START: not enough headroom (want 10 GB spare after the run)." -ForegroundColor Red
    Write-Host "  Lower -MaxFrames, raise -IntervalSec, or free space on $driveLetter`:." -ForegroundColor Red
    exit 1
}

try { New-Item -ItemType Directory -Path $OutRoot -Force -ErrorAction Stop | Out-Null }
catch {
    Write-Host "  Could not create $OutRoot : $_" -ForegroundColor Red
    exit 1
}

# --- arm framedump ----------------------------------------------------------
$env:ORION_FRAMEDUMP          = '1'
$env:ORION_FRAMEDUMP_DIR      = $OutRoot
$env:ORION_FRAMEDUMP_INTERVAL = [string]$IntervalSec
$env:ORION_FRAMEDUMP_MAX      = [string]$MaxFrames
if ($IncludeAnnotated) {
    Remove-Item Env:\ORION_FRAMEDUMP_RAW_ONLY -ErrorAction SilentlyContinue
} else {
    $env:ORION_FRAMEDUMP_RAW_ONLY = '1'
}

Write-Host "  armed - launching Orion..." -ForegroundColor Green
Write-Host ""

# Hand off to the real launcher. It is NOT modified by this script; it only
# inherits the env vars set above.
$launcher = Join-Path $PSScriptRoot 'run_orion.local.ps1'
if (-not (Test-Path $launcher)) {
    Write-Host "  run_orion.local.ps1 not found next to this script." -ForegroundColor Red
    exit 1
}
& $launcher
