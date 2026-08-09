# owner_verify_meter_delay.ps1
#
# READ-ONLY check that the meter delay is actually on the wire.  No elevation
# needed.  It queries the two services and tails the live log; it never
# writes, rotates, deletes, or otherwise touches anything.
#
# LOG LOCATION: the build under test is the DEV build (ORION_PRODUCTION off),
# which writes its log beside the repo at <repo>\logs\orion_native.log.  That
# is the default below.  (The INSTALLED production app logs under
# %LOCALAPPDATA%\NexusVision\Orion Native\ - that is NOT the file this script
# reads, and this script never touches that directory.)
#
# The wire-protocol truth fields are meter_delay_active / meter_delay_settled
# (service JSON echo).  They surface in orion_native.log as these lines, which
# is what this script matches:
#   meter_delay_active=true   ->  "Meter delay service echo: intercept ACTIVE (applying N ms, ...)"
#   meter_delay_settled=true  ->  "Meter delay condition: settled=1 key=...ms epoch=..."
#
# The checks come in two groups:
#   PRE-BATCH  - run right after enabling Meter Delay, BEFORE shooting:
#                service RUNNING, driver RUNNING, service ARMED, echo intercept
#                ACTIVE.  Any FAIL here = the delay is not engaging; fix it
#                before you shoot, or the batch is worthless.
#   MID-BATCH  - run again after at least 5 shots:
#                settled=1, Outcome identity w/ armed_source=, vel= below 0.15.
#                FAILs here before any shots were fired are expected - these
#                signals need shot traffic in the log.

param(
    [int]$TailLines = 5000,
    [string]$LogPath = ""
)

$ErrorActionPreference = "Continue"

if ([string]::IsNullOrWhiteSpace($LogPath)) {
    # DEV build: orionDataDir() resolves to the repo root, so the log lives at
    # <repo>\logs\orion_native.log (NOT under %LOCALAPPDATA% - that is the
    # installed production app's location).
    $LogPath = Join-Path (Split-Path -Parent $PSScriptRoot) "logs\orion_native.log"
}

$results = New-Object System.Collections.ArrayList

function Add-Result([string]$group, [string]$name, [bool]$pass, [string]$detail) {
    [void]$results.Add([pscustomobject]@{ Group = $group; Signal = $name; Pass = $pass; Detail = $detail })
}

Write-Host ""
Write-Host "=== Meter delay engagement check (read-only) ===" -ForegroundColor Cyan
Write-Host ""

# --- 1. NexusVisionSvc RUNNING (pre-batch) ------------------------------------
$svcOut = & sc.exe query NexusVisionSvc
if ($LASTEXITCODE -ne 0) {
    Add-Result "pre" "NexusVisionSvc service" $false "not registered (sc.exe query exit $LASTEXITCODE) - run scripts\owner_manual_test_setup.ps1 first"
} elseif (($svcOut | Out-String) -match "RUNNING") {
    Add-Result "pre" "NexusVisionSvc service" $true "RUNNING"
} else {
    $state = (($svcOut | Out-String) -split "`n" | Where-Object { $_ -match "STATE" } | Select-Object -First 1)
    if ($null -eq $state) { $state = "unknown state" }
    Add-Result "pre" "NexusVisionSvc service" $false ("registered but not running: " + $state.Trim() + " - start it via the app's Meter Delay toggle or an elevated 'sc.exe start NexusVisionSvc'")
}

# --- 2. WinDivert kernel driver loaded (pre-batch) ----------------------------
$wdOut = & sc.exe query WinDivert
if ($LASTEXITCODE -ne 0) {
    Add-Result "pre" "WinDivert driver" $false "not installed/loaded (sc.exe query WinDivert exit $LASTEXITCODE) - the service loads it on demand when the intercept opens"
} elseif (($wdOut | Out-String) -match "RUNNING") {
    Add-Result "pre" "WinDivert driver" $true "RUNNING"
} else {
    Add-Result "pre" "WinDivert driver" $false "present but not RUNNING - the service has not opened a packet handle yet (or AV blocked the driver load)"
}

# --- 3. Log signals ----------------------------------------------------------
if (-not (Test-Path -LiteralPath $LogPath)) {
    Add-Result "pre" "log file" $false "not found at $LogPath"
    Add-Result "mid" "log file" $false "not found at $LogPath"
    $tail = @()
} else {
    # Read-only tail. The log contains stray binary bytes; UTF8 decoding turns
    # them into replacement garbage, which is fine - every line we match is ASCII.
    $tail = Get-Content -LiteralPath $LogPath -Tail $TailLines -Encoding UTF8

    # 3a. (pre-batch) Service reports itself ARMED on the current connection.
    #     ARMED is transition-deduped: it logs once per service connection near
    #     session start, hence the deep default tail.
    $armed    = $tail | Where-Object { $_ -match "Meter delay service state: ARMED" }    | Select-Object -Last 1
    $disarmed = $tail | Where-Object { $_ -match "Meter delay service state: DISARMED" } | Select-Object -Last 1
    if ($armed) {
        Add-Result "pre" "service ARMED" $true "seen in tail"
    } elseif ($disarmed) {
        Add-Result "pre" "service ARMED" $false "service reports DISARMED - it was registered without --arm-meter-delay; re-run setup"
    } else {
        Add-Result "pre" "service ARMED" $false "no 'Meter delay service state: ARMED' in the last $TailLines lines"
    }

    # 3b. (pre-batch) meter_delay_active=true equivalent: the service's own applied-delay echo.
    $echoActive   = $tail | Where-Object { $_ -match "Meter delay service echo: intercept ACTIVE" }   | Select-Object -Last 1
    $echoInactive = $tail | Where-Object { $_ -match "Meter delay service echo: intercept inactive" } | Select-Object -Last 1
    if ($echoActive) {
        Add-Result "pre" "delay ACTIVE (service echo)" $true ($echoActive.Trim())
    } elseif ($echoInactive) {
        Add-Result "pre" "delay ACTIVE (service echo)" $false "last echo says intercept inactive - session gates not open (court IP known? Remote Play running?)"
    } else {
        Add-Result "pre" "delay ACTIVE (service echo)" $false "no service echo in the last $TailLines lines"
    }

    # 3c. (mid-batch) meter_delay_settled=true equivalent: the delay reached its target and held.
    $settled = $tail | Where-Object { $_ -match "Meter delay condition: settled=1" } | Select-Object -Last 1
    if ($settled) {
        Add-Result "mid" "delay settled" $true ($settled.Trim())
    } else {
        Add-Result "mid" "delay settled" $false "no 'Meter delay condition: settled=1' in the last $TailLines lines (no shots yet, still ramping, or never engaged)"
    }

    # 3d. (mid-batch) Recent Outcome identity with the armed_source attribution field.
    $outcome = $tail | Where-Object { $_ -match "Outcome identity:" -and $_ -match "armed_source=" } | Select-Object -Last 1
    if ($outcome) {
        Add-Result "mid" "Outcome identity w/ armed_source" $true ($outcome.Trim())
    } else {
        Add-Result "mid" "Outcome identity w/ armed_source" $false "no recent 'Outcome identity:' line carrying armed_source= (no shots yet, or an old exe without the attribution field)"
    }

    # 3e. (mid-batch) Any vel= reading below 0.15 %/ms (baseline ~0.226; a drop = engagement).
    $vels = @()
    foreach ($line in ($tail | Where-Object { $_ -match "Release attribution:" })) {
        if ($line -match "vel=([0-9]+\.[0-9]+)") { $vels += [double]$Matches[1] }
    }
    if ($vels.Count -eq 0) {
        Add-Result "mid" "vel= below 0.15" $false "no 'Release attribution: ... vel=' lines in the last $TailLines lines - take a few shots first (or re-run with a larger -TailLines)"
    } else {
        $minVel = ($vels | Measure-Object -Minimum).Minimum
        $detail = "$($vels.Count) readings, min=$minVel (baseline ~0.226)"
        if ($minVel -lt 0.15) {
            Add-Result "mid" "vel= below 0.15" $true $detail
        } else {
            Add-Result "mid" "vel= below 0.15" $false ($detail + " - meter speed unchanged, the delay is not reaching the console")
        }
    }
}

# --- 4. Summary --------------------------------------------------------------
function Show-Group([string]$title, [object[]]$rows) {
    Write-Host ""
    Write-Host $title -ForegroundColor Cyan
    Write-Host ("{0,-36} {1,-6} {2}" -f "SIGNAL", "RESULT", "DETAIL")
    Write-Host ("{0,-36} {1,-6} {2}" -f "------", "------", "------")
    $ok = $true
    foreach ($r in $rows) {
        if ($r.Pass) {
            Write-Host ("{0,-36} {1,-6} {2}" -f $r.Signal, "PASS", $r.Detail) -ForegroundColor Green
        } else {
            $ok = $false
            Write-Host ("{0,-36} {1,-6} {2}" -f $r.Signal, "FAIL", $r.Detail) -ForegroundColor Red
        }
    }
    return $ok
}

$preRows = @($results | Where-Object { $_.Group -eq "pre" })
$midRows = @($results | Where-Object { $_.Group -eq "mid" })
$preOk = Show-Group "PRE-BATCH checks (run right after enabling Meter Delay, before shooting):" $preRows
$midOk = Show-Group "MID-BATCH checks (run after at least 5 shots):" $midRows

Write-Host ""
if ($preOk -and $midOk) {
    Write-Host "ALL PASS - the delay is on the wire and shots are being attributed. This batch's data is valid." -ForegroundColor Green
} elseif (-not $preOk) {
    Write-Host "PRE-BATCH FAIL - the delay is NOT engaging. Fix this before shooting; do not credit the batch." -ForegroundColor Red
    Write-Host "(Did you run this before enabling Meter Delay in the app? Enable it first, then re-run.)"
    Write-Host "Troubleshooting is in docs\OWNER_MANUAL_TEST.md, section 7."
} else {
    Write-Host "PRE-BATCH all PASS, MID-BATCH has FAILs - have you fired shots yet?" -ForegroundColor Yellow
    Write-Host "If not, take at least 5 shots and re-run. If you have, this is a batch-collection"
    Write-Host "issue - re-run after a few more shots before condemning the batch"
    Write-Host "(docs\OWNER_MANUAL_TEST.md, section 4)."
}

# --- 5. Context: last few meter-delay lines from the tail --------------------
if ($tail.Count -gt 0) {
    Write-Host ""
    Write-Host "Last meter-delay lines in the tail (context):" -ForegroundColor Cyan
    $tail | Where-Object { $_ -match "Meter delay" } | Select-Object -Last 6 | ForEach-Object { Write-Host ("  " + $_.Trim()) }
}
Write-Host ""
