<#
.SYNOPSIS
    Start a visible 10-round Claude + Codex debug session plus watcher.

.DESCRIPTION
    Opens two PowerShell windows:
      1. agent loop
      2. live watcher

    This script is a convenience launcher only. The loop itself still enforces
    stop rules from AGENT_RULES.md and TASK.md.
#>

[CmdletBinding()]
param(
    [int]$MaxRounds = 10
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$loopCommand = @"
Set-Location '$RepoRoot'
powershell -ExecutionPolicy Bypass -File scripts\run_agent_loop.ps1 -Run -MaxRounds $MaxRounds
Write-Host ''
Write-Host 'Agent loop finished. Press Enter to close.'
Read-Host
"@

$watchCommand = @"
Set-Location '$RepoRoot'
powershell -ExecutionPolicy Bypass -File scripts\watch_agent_loop.ps1
"@

Start-Process powershell -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $loopCommand)
Start-Process powershell -ArgumentList @("-NoExit", "-ExecutionPolicy", "Bypass", "-Command", $watchCommand)

Write-Host "Started Orion agent debug session."
Write-Host "Repo: $RepoRoot"
Write-Host "Rounds: $MaxRounds"
