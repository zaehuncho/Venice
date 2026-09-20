# Pre-flight for the autonomous tip-timing live batch.
#
# Every check here exists because it has silently burned a batch before. Run it immediately before
# launching; it is read-only and takes a couple of seconds.
#
#   powershell -ExecutionPolicy Bypass -File scripts\preflight_tip_batch.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\preflight_tip_batch.ps1 -ExpectedShotLeadMs 285
#
# Exit code 0 = clear to launch. 1 = at least one BLOCK.

param(
    # A controlled A/B must pin the other timing authority exactly. The old
    # zero/wildcard default allowed two lead arms with different Tip Timing to
    # both certify as "single-treatment".
    [ValidateRange(150.0, 800.0)]
    [double]$ExpectedTipTimingMs = 421.3,

    # Keep 300 as the reference default, but require an explicit declaration for
    # a one-variable Shot Lead A/B.  The preflight remains read-only and still
    # blocks whenever the signed settings do not match the declared arm.
    [ValidateRange(150.0, 800.0)]
    [double]$ExpectedShotLeadMs = 300.0,

    # The learner is profile-scoped. Require the intended profile and audit the
    # exact learning file that the runtime will open for it.
    [ValidateNotNullOrEmpty()]
    [string]$ExpectedProfile = 'Default',

    # The live forward-crossing gate changes fire authority and is therefore part
    # of the frozen treatment, not merely a detector preference.
    [ValidateRange(0.0, 400.0)]
    [double]$ExpectedTipGateCapMs = 300.0
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$blocks = @()
$warns  = @()

function Ok   ($m) { Write-Host ("  [ OK ]   " + $m) -ForegroundColor Green }
function Block($m) { Write-Host ("  [BLOCK]  " + $m) -ForegroundColor Red;    $script:blocks += $m }
function Warn ($m) { Write-Host ("  [ warn]  " + $m) -ForegroundColor Yellow; $script:warns  += $m }

Write-Host ""
Write-Host "=== Orion tip-timing batch pre-flight ===" -ForegroundColor Cyan

# --- 1. capture device is free -------------------------------------------------------------
# An OBS dshow source OWNS the Elgato. The symptom is "no live device" / 0xC00D3704, a black
# preview and fresh=0 -- opens-but-no-frames means BUSY, not a wrong device index.
Write-Host "`n[1] Capture device"
$obs = Get-Process -Name obs64, obs32, obs -ErrorAction SilentlyContinue
if ($obs) { Block "OBS is running (pid $($obs.Id -join ',')) - it holds the Elgato. Close it." }
else      { Ok "no OBS process holding the capture card" }

# --- 2. ICS / console reachability ---------------------------------------------------------
# ICS binds Hyper-V vEthernet vNICs, never physical NICs. A GW-Lab VM with a PS5-Internal NIC
# steals 192.168.137.1 and kills all discovery/registration.
Write-Host "`n[2] Network (ICS + console)"
$ics = Get-NetIPAddress -IPAddress 192.168.137.1 -ErrorAction SilentlyContinue
if (-not $ics) {
    Block "192.168.137.1 is not assigned - ICS is down; the console cannot be reached"
} else {
    $adapters = @($ics | ForEach-Object { (Get-NetAdapter -InterfaceIndex $_.InterfaceIndex -ErrorAction SilentlyContinue).Name })
    $adapters = @($adapters | Where-Object { $_ })
    if ($adapters.Count -gt 1) {
        Block "192.168.137.1 claimed by MULTIPLE adapters ($($adapters -join ', ')) - a VM is stealing it"
    } else {
        Ok "ICS host address on $($adapters[0])"
    }
}
# Reachability is the real proof the ICS path works, so it decides the adapter-name question too:
# a renamed vNIC that routes correctly is fine, and only an unreachable console makes the name
# worth questioning.
# The console address is DHCP-assigned and has drifted three times (.100 -> .138 -> .126); the app
# connects to settings.json remote_play_console_ip, so probe THAT address, not a literal. Probing
# a stale literal reported "not answering" on 2026-09-01 while the console was awake at .126 and
# every session that day had connected to it.
$consoleIp = '192.168.137.100'
try {
    $consoleIpFromSettings = (Get-Content -LiteralPath (Join-Path $root 'settings.json') -Raw |
        ConvertFrom-Json).remote_play_console_ip
    if ($consoleIpFromSettings -and "$consoleIpFromSettings" -match '^\d+\.\d+\.\d+\.\d+$') {
        $consoleIp = "$consoleIpFromSettings"
    }
} catch {}
$consoleUp = Test-Connection -ComputerName $consoleIp -Count 1 -Quiet -ErrorAction SilentlyContinue
if ($consoleUp) {
    Ok "console reachable at $consoleIp (settings.json remote_play_console_ip; proves the ICS path routes)"
} else {
    Warn "$consoleIp (settings.json remote_play_console_ip) not answering (fine if the PS5 is asleep - it MUST be up before you shoot)"
    if ($adapters -and $adapters[0] -notmatch 'vEthernet') {
        Warn "...and 192.168.137.1 is on '$($adapters[0])', not a vEthernet vNIC - suspect a VM stealing ICS"
    }
}

# --- 3. binaries current -------------------------------------------------------------------
Write-Host "`n[3] Build freshness"
function Test-TargetFreshness([string]$binaryRelativePath, [string[]]$sourceRelativePaths) {
    $binaryPath = Join-Path $root $binaryRelativePath
    $binary = Get-Item -LiteralPath $binaryPath -ErrorAction SilentlyContinue
    $binaryName = Split-Path -Leaf $binaryRelativePath
    if (-not $binary) {
        Block "$binaryName missing - build Release first"
        return
    }

    $sources = @($sourceRelativePaths | ForEach-Object {
        Get-Item -LiteralPath (Join-Path $root $_) -ErrorAction SilentlyContinue
    } | Where-Object { $_ })
    if ($sources.Count -ne $sourceRelativePaths.Count) {
        Block "$binaryName freshness source inventory is incomplete"
        return
    }
    $newest = $sources | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($binary.LastWriteTime -lt $newest.LastWriteTime) {
        Block "$binaryName is OLDER than $($newest.Name) - you would batch a stale loaded module"
    } else {
        Ok "$binaryName newer than its newest source ($($newest.Name))"
    }
}

# AppConfig lives in OrionCommon and the fire logic lives in AutomationCore. Checking only
# OrionNative.exe can pass while Windows loads a stale timing DLL next to it.
Test-TargetFreshness 'native_orion\build\Release\OrionCommon.dll' @(
    'native_orion\src\AppConfig.cpp',
    'native_orion\src\AppConfig.h',
    'native_orion\src\Diagnostics.cpp',
    'native_orion\src\Diagnostics.h',
    'native_orion\src\OrionTypes.h',
    'native_orion\src\RemotePlayExecutablePolicy.h'
)
Test-TargetFreshness 'native_orion\build\Release\AutomationCore.dll' @(
    'native_orion\src\AutomationEngine.cpp',
    'native_orion\src\AutomationEngine.h',
    'native_orion\src\ControllerDeviceSelector.cpp',
    'native_orion\src\ControllerDeviceSelector.h',
    'native_orion\src\MeterDelayController.cpp',
    'native_orion\src\MeterDelayController.h'
)
Test-TargetFreshness 'native_orion\build\Release\OrionNative.exe' @(
    'native_orion\src\OrionAppController.cpp',
    'native_orion\src\OrionAppController.h',
    'native_orion\src\main.cpp'
)

# --- 4. latency prior ----------------------------------------------------------------------
# The engine actuates on this. A 241.4 mean here is the pre-fix artefact and reproduces the
# 8-of-10 abort session.
Write-Host "`n[4] Latency factory prior"
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }
$priorInfo = & $py -c @"
import json, sys
sys.path.insert(0, r'$root')
from latency_estimator import _FACTORY_PRIOR_PATH
d = json.load(open(_FACTORY_PRIOR_PATH))
means = sorted(set(p['mean_ms'] for p in d['profiles']))
print(d.get('model_version', '?'))
print(','.join(str(m) for m in means))
print(_FACTORY_PRIOR_PATH)
"@ 2>&1
$lines = @($priorInfo -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($lines.Count -lt 3) {
    Block "could not read the factory prior: $priorInfo"
} else {
    $ver = $lines[0]; $means = $lines[1]
    if ($means -eq '241.4') { Block "prior is the pre-fix uniform 241.4 across all routes" }
    elseif ($ver -notmatch '^sha256-') { Warn "prior model_version '$ver' is not content-derived - possible hand-edit" }
    else { Ok "prior $ver  means=$means" }
    # Validate the content-derived version and runtime bounds even when the
    # historical evidence log is no longer present. Running --check over every
    # cumulative orion_native.log is invalid: those logs span multiple route
    # scopes and the builder correctly refuses to pool them; the old preflight
    # misreported that refusal as a hand edit.
    $priorPath = $lines[2]
    & $py -c "import json; from tools.timing.build_latency_factory_prior import validate; validate(json.load(open(r'$priorPath', encoding='utf-8')))" *> $null
    if ($LASTEXITCODE -eq 0) { Ok "prior content hash + runtime bounds validate" }
    else { Block "prior content hash or runtime bounds are invalid" }

    try {
        $priorJson = Get-Content -LiteralPath $priorPath -Raw | ConvertFrom-Json
        $evidence = @($priorJson.generated_from.logs | ForEach-Object {
            Join-Path (Join-Path $root 'logs') $_
        })
        $missingEvidence = @($evidence | Where-Object { -not (Test-Path -LiteralPath $_) })
        if ($evidence.Count -gt 0 -and $missingEvidence.Count -eq 0) {
            $checkArgs = @((Join-Path $root 'tools\timing\build_latency_factory_prior.py'), '--check')
            foreach ($evidencePath in $evidence) { $checkArgs += @('--log', $evidencePath) }
            & $py @checkArgs *> $null
            if ($LASTEXITCODE -eq 0) { Ok "prior is byte-identical to its declared evidence regeneration" }
            else { Block "prior differs from its declared evidence regeneration" }
        } else {
            Warn "declared latency-prior evidence log is unavailable; content is valid but regeneration cannot be reproduced"
        }
    } catch {
        Warn "could not audit declared latency-prior evidence: $($_.Exception.Message)"
    }
}

# --- 5. measurement instrumentation --------------------------------------------------------
# DETDIAG (log lines) and DETCSV (detframes.csv) are INDEPENDENT gates. Setting only the first
# makes the usual liveness grep pass while the CSV stays stale -- this is exactly how the
# 2026-08-03 batch was lost.
Write-Host "`n[5] Measurement instrumentation"
$launcher = Get-Content (Join-Path $root 'run_orion.local.ps1') -Raw
function Has-ActiveLiteralEnvAssignment([string]$name, [string]$value) {
    # Anchor to a real PowerShell assignment line. A broad regex matched the
    # commented-out ORION_FILL_KALMAN example and made the old preflight green
    # while no Kalman telemetry was actually enabled.
    $escapedName = [regex]::Escape($name)
    $escapedValue = [regex]::Escape($value)
    $pattern = '(?m)^\s*\$env:{0}\s*=\s*["'']{1}["'']\s*(?:#.*)?$' -f `
               $escapedName, $escapedValue
    return $launcher -match $pattern
}
if (Has-ActiveLiteralEnvAssignment 'ORION_DETCSV' '1') { Ok "-Detdiag sets ORION_DETCSV (detframes.csv will be written)" }
else { Block "run_orion.local.ps1 never sets ORION_DETCSV - detframes.csv stays empty and the batch yields no residuals" }
if ($launcher -match 'if \(-not \$env:ORION_FRAMEDUMP_MAX\)') { Ok "-Framedump respects pre-set INTERVAL/MAX" }
else { Warn "-Framedump may clobber operator-set ORION_FRAMEDUMP_INTERVAL/MAX" }
if (Has-ActiveLiteralEnvAssignment 'ORION_FILL_KALMAN' '1') { Ok "ORION_FILL_KALMAN enabled (kalmanTipMs telemetry)" }
else { Warn "ORION_FILL_KALMAN not set in the launcher - export it manually to grade Kalman vs sampler" }

$overrideNames = @(
    'ORION_DEV_FIRE_OFFSET_SWEEP',
    'ORION_LEAD_FLOOR_MS',
    'ORION_LEAD_BIAS_MS',
    'ORION_GREEN_CENTER_FRAC'
)
$scrubbedOverrides = @($overrideNames | Where-Object {
    $escaped = [regex]::Escape($_)
    $launcher -match ('Remove-Item[^\r\n]+Env:[^\r\n]*' + $escaped)
})
# The launcher uses a compact foreach over the literal list, so prove both the
# list membership and the guarded Remove-Item loop instead of requiring four
# duplicated removal statements.
$hasGuardedOverrideScrub = ($launcher -match 'if\s*\(\s*-not\s+\$AllowTimingOverrides\s*\)') -and
                           ($launcher -match 'Remove-Item\s+-LiteralPath\s+\("Env:"\s*\+\s*\$_\)')
$missingOverrideLiterals = @($overrideNames | Where-Object { $launcher -notmatch [regex]::Escape("'$_'") })
if ($hasGuardedOverrideScrub -and $missingOverrideLiterals.Count -eq 0) {
    Ok "normal launcher scrubs inherited lead/offset/green-center experiment overrides"
} else {
    Block "launcher does not deterministically scrub every timing experiment override"
}
if (Has-ActiveLiteralEnvAssignment 'ORION_HORIZON_DEBIAS' '1') {
    Ok "developer reference arm explicitly sets ORION_HORIZON_DEBIAS=1"
} else {
    Block "developer reference arm does not explicitly set ORION_HORIZON_DEBIAS=1"
}

# The fire worker defaults to its high-resolution wait, but the local launcher once forced both
# timing facilities OFF for a forgotten A/B. That put the live batch back on a Windows condvar
# whose measured p99 wake error is one scheduler quantum (~15.8ms). Verify the launcher's normal
# branch explicitly resolves to 1 while preserving the named diagnostic rollback switch.
$preciseWaitDefault = '(?s)\$preciseFireWaitMode\s*=\s*if\s*\(\s*\$LegacyFireWait\s*\)\s*\{\s*["'']0["'']\s*\}\s*else\s*\{\s*["'']1["'']\s*\}'
$hiresUsesPolicy = $launcher -match '(?m)^\s*\$env:ORION_PRECISE_WAIT_HIRES\s*=\s*\$preciseFireWaitMode\s*$'
$guardUsesPolicy = $launcher -match '(?m)^\s*\$env:ORION_TIMER_RES_GUARD\s*=\s*\$preciseFireWaitMode\s*$'
if (($launcher -match $preciseWaitDefault) -and $hiresUsesPolicy -and $guardUsesPolicy) {
    Ok "normal launcher keeps precise fire wait ON; -LegacyFireWait is the explicit diagnostic escape hatch"
} else {
    Block "launcher precise-wait policy is missing or unsafe - normal batches must set HIRES=1 and TIMER_RES_GUARD=1"
}
if ($launcher -match '(?m)^\s*\$env:(ORION_PRECISE_WAIT_HIRES|ORION_TIMER_RES_GUARD)\s*=\s*["'']0["'']\s*$') {
    Block "launcher directly forces a precise-wait facility OFF - use only the guarded -LegacyFireWait policy"
}

# A timing batch is uninterpretable if the target itself walks. The handoff's
# claimed cross-session collapse mixed a 421.3 -> 400.1ms auto-unlock with a
# changing reader and then attributed the result solely to the reader. Require
# one locked aim and no auto-unlock for the counted batch. This changes no
# production default; it audits the local, signed development state only.
$settingsPath = Join-Path $root 'settings.json'
function Get-ProfileLearningPath([string]$profileName) {
    $name = $profileName.Trim()
    if ([string]::IsNullOrWhiteSpace($name) -or $name.Equals('Default', [StringComparison]::OrdinalIgnoreCase)) {
        return Join-Path $root 'learning.json'
    }

    $slug = New-Object System.Text.StringBuilder
    foreach ($c in $name.ToLowerInvariant().ToCharArray()) {
        if ([char]::IsLetterOrDigit($c)) {
            [void]$slug.Append($c)
        } elseif ($slug.Length -gt 0 -and $slug[$slug.Length - 1] -ne '-') {
            [void]$slug.Append('-')
        }
    }
    $slugText = $slug.ToString().TrimEnd('-')
    if ($slugText.Length -gt 48) { $slugText = $slugText.Substring(0, 48).TrimEnd('-') }
    if ([string]::IsNullOrEmpty($slugText)) { return Join-Path $root 'learning.json' }
    return Join-Path $root ("learning.{0}.json" -f $slugText)
}
try {
    $settings = Get-Content -LiteralPath $settingsPath -Raw | ConvertFrom-Json
    $actualProfile = [string]$settings.active_profile
    if (-not $actualProfile.Equals($ExpectedProfile, [StringComparison]::OrdinalIgnoreCase)) {
        Block "active_profile is '$actualProfile', expected declared profile '$ExpectedProfile'"
    } else {
        Ok "active profile matches declared arm: $ExpectedProfile"
    }
    $learningPath = Get-ProfileLearningPath $actualProfile
    $learning = Get-Content -LiteralPath $learningPath -Raw | ConvertFrom-Json
    if ($settings.tip_phase_aim_frozen -ne $true) {
        Block "tip_phase_aim_frozen is not true - the effective target can walk during the batch"
    } else { Ok "Tip Timing aim is pinned for this batch" }
    if ($settings.tip_timing_auto_unlock -ne $false) {
        Block "tip_timing_auto_unlock is not false - the batch target can change mid-run"
    } else { Ok "Tip Timing auto-unlock is disabled for the controlled batch" }
    if ($settings.tip_timing_user_set -ne $true) {
        Block "tip_timing_user_set is not true - the pinned test value lacks explicit user authority"
    } else { Ok "pinned Tip Timing has explicit user authority" }

    # One-variable reference arm. These guards prevent a stale UI toggle or a
    # legacy per-shot trim from turning the 421.3ms run into an unlabelled mixed
    # treatment. Go-To previously carried +155.5ms plus a second +9ms trim.
    $baselineProblems = @()
    if ([math]::Abs([double]$settings.actuation_lead_ms - $ExpectedShotLeadMs) -gt 0.001) {
        $baselineProblems += "actuation_lead_ms != declared arm $ExpectedShotLeadMs"
    }
    if ($settings.actuation_lead_user_set -ne $true) { $baselineProblems += 'actuation_lead_user_set != true' }
    if ($settings.autonomous_vision -ne $true) { $baselineProblems += 'autonomous_vision != true' }
    if ($settings.autonomous_vision_shadow -ne $false) { $baselineProblems += 'autonomous_vision_shadow != false' }
    if ($settings.meter_enabled -ne $true) { $baselineProblems += 'meter_enabled != true' }
    if ([string]$settings.vision_mode -ne 'meter') { $baselineProblems += 'vision_mode != meter' }
    if ($settings.meter_delay_enabled -ne $false) { $baselineProblems += 'meter_delay_enabled != false' }
    if ([string]$settings.active_shot_type -ne 'Standstill') { $baselineProblems += 'active_shot_type != Standstill' }
    if ($settings.tempo.enabled -ne $false -or $settings.tempo_remap_enabled -ne $false) {
        $baselineProblems += 'tempo/remap is enabled'
    }
    if ([double]$settings.early_late_offset_ms -ne 0.0) { $baselineProblems += 'early_late_offset_ms != 0' }
    if ([double]$settings.autonomous_green_center_frac -ne 0.0) { $baselineProblems += 'autonomous_green_center_frac != 0' }
    if ($settings.tip_phase_anchor_base20 -ne $true) { $baselineProblems += 'tip_phase_anchor_base20 != true' }
    if ($settings.tip_phase_anchor_consensus -ne $true) { $baselineProblems += 'tip_phase_anchor_consensus != true' }
    if ($settings.stop_dating_subframe -ne $true) { $baselineProblems += 'stop_dating_subframe != true' }
    if ($settings.tip_phase_type_trim_enabled -ne $false) { $baselineProblems += 'tip_phase_type_trim_enabled != false' }
    if ($settings.tip_gate_enabled -ne $true) { $baselineProblems += 'tip_gate_enabled != true' }
    if ([math]::Abs([double]$settings.tip_gate_cap_ms - $ExpectedTipGateCapMs) -gt 0.001) {
        $baselineProblems += "tip_gate_cap_ms != declared value $ExpectedTipGateCapMs"
    }
    $nonZeroOffsets = @($settings.shot_type_offsets.PSObject.Properties | Where-Object {
        [math]::Abs([double]$_.Value) -gt 0.0001
    } | ForEach-Object { "$($_.Name)=$($_.Value)" })
    if ($nonZeroOffsets.Count -gt 0) {
        $baselineProblems += ('nonzero shot_type_offsets: ' + ($nonZeroOffsets -join ', '))
    }
    if ($baselineProblems.Count -gt 0) {
        foreach ($problem in $baselineProblems) { Block ("reference baseline contaminated: " + $problem) }
    } else {
        if ([math]::Abs($ExpectedShotLeadMs - 300.0) -le 0.001) {
            Ok "reference baseline is single-treatment: meter vision, 300ms lead, pinned gate/profile, no tempo/type/static trims"
        } else {
            Ok "declared single-treatment A/B arm: meter vision, $($ExpectedShotLeadMs)ms lead, pinned gate/profile, no tempo/type/static trims"
        }
    }

    $engineHeader = Get-Content -LiteralPath (Join-Path $root 'native_orion\src\AutomationEngine.h') -Raw
    $engineSource = Get-Content -LiteralPath (Join-Path $root 'native_orion\src\AutomationEngine.cpp') -Raw
    $constMatch = [regex]::Match($engineHeader, 'double\s+tipPhaseConstantMs\s*=\s*([\d.]+)')
    $seedMatch = [regex]::Match($engineHeader, 'double\s+tipPhaseSeedPhysicalMs\s*=\s*([\d.]+)')
    $shiftMatch = [regex]::Match($engineSource, 'kAnchorBase20ShiftMs\s*=\s*([\d.]+)')
    if (-not ($constMatch.Success -and $seedMatch.Success -and $shiftMatch.Success) -or
        -not ($learning.learned_phase_physical_ms -gt 0)) {
        Block "could not derive the effective Tip Timing from current source + learning.json"
    } else {
        $canonical = [double]$learning.learned_phase_physical_ms
        $baseConstant = [double]$constMatch.Groups[1].Value
        $defaultSeed = [double]$seedMatch.Groups[1].Value
        $baseShift = if ($settings.tip_phase_anchor_base20 -eq $true) {
            [double]$shiftMatch.Groups[1].Value
        } else { 0.0 }
        $effectiveTipMs = $canonical + $baseShift + $baseConstant - $defaultSeed
        if ([math]::Abs($effectiveTipMs - $ExpectedTipTimingMs) -gt 0.05) {
            Block ("effective Tip Timing is {0:N1}ms, expected {1:N1}ms for this A/B" -f
                   $effectiveTipMs, $ExpectedTipTimingMs)
        } else {
            Ok ("effective Tip Timing matches declared arm at {0:N1}ms ({1})" -f
                $effectiveTipMs, (Split-Path -Leaf $learningPath))
        }
    }
} catch {
    Block "could not audit pinned Tip Timing state: $($_.Exception.Message)"
}

$det = Join-Path $root 'logs\diagnostics\detframes.csv'
if (Test-Path $det) {
    $age = (New-TimeSpan -Start (Get-Item $det).LastWriteTime -End (Get-Date)).TotalDays
    Warn ("detframes.csv exists and is {0:N1} days old ({1} bytes) - archive it, then confirm it GROWS after launch" -f $age, (Get-Item $det).Length)
} else { Ok "no stale detframes.csv" }

# --- 6. log emitters present ---------------------------------------------------------------
# A checklist whose greps silently match nothing is worse than no checklist.
Write-Host "`n[6] Diagnostic emitters"
$emitters = @{
  'TIP RESERVATION:'         = 'native_orion\src\AutomationEngine.cpp'
  'reservation_disposition=' = 'native_orion\src\AutomationEngine.cpp'
  'TIP DEADLINE DECISION:'   = 'native_orion\src\AutomationEngine.cpp'
  'READER ACQUIRE'           = 'simple_meter_reader.py'
  'fillKalman'               = 'remote_play_orchestrator.py'
}
foreach ($pat in $emitters.Keys) {
    $f = Join-Path $root $emitters[$pat]
    if ((Test-Path $f) -and (Select-String -Path $f -SimpleMatch -Pattern $pat -Quiet)) { Ok "'$pat' is emitted" }
    else { Block "'$pat' is NOT emitted by $($emitters[$pat]) - the post-batch grep would match nothing" }
}

# --- 7. disk headroom ----------------------------------------------------------------------
Write-Host "`n[7] Disk"
$drive = Get-PSDrive -Name ((Get-Item $root).PSDrive.Name)
$freeGb = [math]::Round($drive.Free / 1GB, 1)
if ($freeGb -lt 5) { Block "only ${freeGb} GB free - framedump PNGs will fill the disk mid-batch" }
elseif ($freeGb -lt 20) { Warn "${freeGb} GB free - a long framedump batch may be tight" }
else { Ok "${freeGb} GB free" }

# --- verdict -------------------------------------------------------------------------------
Write-Host ""
if ($blocks.Count -gt 0) {
    Write-Host "=== NOT CLEAR TO LAUNCH - $($blocks.Count) blocking issue(s) ===" -ForegroundColor Red
    $blocks | ForEach-Object { Write-Host "   - $_" -ForegroundColor Red }
    exit 1
}
Write-Host "=== CLEAR TO LAUNCH ===" -ForegroundColor Green
if ($warns.Count -gt 0) {
    Write-Host "$($warns.Count) warning(s) - read them, they are not automatically fine:" -ForegroundColor Yellow
    $warns | ForEach-Object { Write-Host "   - $_" -ForegroundColor Yellow }
}
Write-Host ""
Write-Host "Launch:  .\run_orion.local.ps1 -Framedump -Detdiag"
Write-Host "Then confirm BOTH:  log has DETDIAG lines  AND  logs\diagnostics\detframes.csv is growing."
exit 0
