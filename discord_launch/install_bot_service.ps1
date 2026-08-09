# Registers the Orion License Bot as an auto-starting background task (Windows Task Scheduler).
# Run ONCE as Administrator, AFTER creating run_bot.local.ps1 from the template.
# - Starts at boot (no login needed) and immediately.
# - The runner script itself auto-restarts the bot on crashes; the task restarts the runner.
# Remove with:  Unregister-ScheduledTask -TaskName OrionLicenseBot -Confirm:$false

$root   = Split-Path -Parent $MyInvocation.MyCommand.Path
$runner = Join-Path $root "run_bot.local.ps1"
if (-not (Test-Path $runner)) {
    Write-Error "run_bot.local.ps1 not found - copy run_bot.local.ps1.template and fill in the secrets first."
    exit 1
}

$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`"" `
    -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -RunLevel Highest

Register-ScheduledTask -TaskName "OrionLicenseBot" -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName "OrionLicenseBot"
Write-Host "OrionLicenseBot task registered + started. Log: $root\bot.log"
Write-Host "Check Discord: the bot should show ONLINE within ~30s and '/' should list its commands."
