# Fix DualSense / DualShock USB power management (the same change as the in-app
# "Fix Controller" button, which needs administrator rights the -NoElevate launcher
# does not have).
#
# Why: Windows creates a NEW device entry every time the pad is plugged into a different
# USB port, and every new entry starts with EnhancedPowerManagementEnabled=1. On this rig
# that setting made the pad's report stream stall for 4-52 s mid-play while the pad stayed
# enumerated (orion_native.log 2026-09-11 20:52, 21:07; eleven more in the rotated log);
# Venice then tore the virtual pad down and the PS5 saw a controller disconnect.
#
# Run from the repo root (a UAC prompt appears once):
#   powershell -ExecutionPolicy Bypass -File tools\diagnostics\fix_dualsense_usb_power.ps1
# Then UNPLUG and re-plug the controller so the driver re-reads the setting.
param([switch]$NoElevate)

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    if ($NoElevate) { Write-Host "Not elevated; re-run as administrator."; exit 2 }
    Write-Host "Requesting administrator rights (UAC)..."
    $args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-NoElevate')
    $p = Start-Process powershell -Verb RunAs -ArgumentList $args -Wait -PassThru
    exit $p.ExitCode
}

$pids = @('PID_0CE6', 'PID_0DF2', 'PID_0E5F', 'PID_05C4', 'PID_09CC')   # DualSense, DualSense Edge, DS4
$root = 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB'
$fixed = 0; $already = 0; $seen = 0
Get-ChildItem $root | Where-Object { $n = $_.PSChildName; ($n -match 'VID_054C') -and ($n -match 'MI_03') -and (@($pids | Where-Object { $n -like "*$_*" }).Count -gt 0) } | ForEach-Object {
    Get-ChildItem $_.PSPath | ForEach-Object {
        $dp = Join-Path $_.PSPath 'Device Parameters'
        if (-not (Test-Path $dp)) { return }
        $seen++
        $v = Get-ItemProperty $dp -ErrorAction SilentlyContinue
        $cur = if ($v -and $v.PSObject.Properties['EnhancedPowerManagementEnabled']) { $v.EnhancedPowerManagementEnabled } else { $null }
        if ($cur -eq 1) {
            Set-ItemProperty -Path $dp -Name EnhancedPowerManagementEnabled -Value 0 -Type DWord
            Write-Host ("FIXED   {0}\{1}  EnhancedPowerManagementEnabled 1 -> 0" -f (Split-Path $_.PSParentPath -Leaf), $_.PSChildName)
            $fixed++
        } else {
            Write-Host ("ok      {0}\{1}  EnhancedPowerManagementEnabled={2}" -f (Split-Path $_.PSParentPath -Leaf), $_.PSChildName, ($(if ($null -eq $cur) { '-' } else { $cur })))
            $already++
        }
    }
}
Write-Host ("entries={0} fixed={1} already_ok={2}" -f $seen, $fixed, $already)
if ($fixed -gt 0) { Write-Host "Now unplug and re-plug the controller." }
if ($seen -eq 0) { Write-Host "No Sony pad USB entries found."; exit 3 }
exit 0
