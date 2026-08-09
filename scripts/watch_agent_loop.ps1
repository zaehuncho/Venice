<#
.SYNOPSIS
    Live watcher for the Claude + Codex Orion agent loop.

.DESCRIPTION
    Shows the latest STATUS.md token block, git status, diff stat, recent loop
    verification logs, and active Claude/Codex processes on a timer.
#>

[CmdletBinding()]
param(
    [int]$IntervalSeconds = 5,
    [switch]$Once
)

$ErrorActionPreference = "Stop"
$env:GIT_PAGER = "cat"
$env:PAGER = "cat"
$env:LESS = "-F -X"
$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $RepoRoot

function Show-AgentLoopState {
    Clear-Host
    Write-Host "Orion Agent Loop Watcher" -ForegroundColor Cyan
    Write-Host "Repo: $RepoRoot"
    Write-Host "Time: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    Write-Host ""

    Write-Host "== Current STATUS.md tokens ==" -ForegroundColor Cyan
    $status = Get-Content -LiteralPath "STATUS.md"
    $inBlock = $false
    foreach ($line in $status) {
        if ($line -match '^```') {
            if (-not $inBlock) {
                $inBlock = $true
                continue
            }
            break
        }
        if ($inBlock) {
            Write-Host $line
        }
    }

    Write-Host ""
    Write-Host "== git status ==" -ForegroundColor Cyan
    git --no-pager -c core.autocrlf=false status --short --untracked-files=all |
        Select-Object -First 40

    Write-Host ""
    Write-Host "== git diff --stat ==" -ForegroundColor Cyan
    git --no-pager -c core.autocrlf=false diff --stat |
        Select-Object -First 30

    Write-Host ""
    Write-Host "== Recent verify logs ==" -ForegroundColor Cyan
    Get-ChildItem -LiteralPath "logs\agent-loop" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 5 Name, Length, LastWriteTime |
        Format-Table -AutoSize

    Write-Host ""
    Write-Host "== Active agent processes ==" -ForegroundColor Cyan
    Get-CimInstance Win32_Process |
        Where-Object {
            $_.Name -in @("claude.exe", "codex.exe", "powershell.exe") -and
            (
                $_.Name -in @("claude.exe", "codex.exe") -or
                $_.CommandLine -match "agent-loop\.ps1|run_agent_loop\.ps1|watch_agent_loop\.ps1|watch-orion-agent-loop\.ps1|continue-orion-agent-batches\.ps1"
            )
        } |
        ForEach-Object {
            $startTime = $null
            if ($_.CreationDate) {
                if ($_.CreationDate -is [datetime]) {
                    $startTime = $_.CreationDate
                } else {
                    try {
                        $startTime = [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$_.CreationDate)
                    } catch {
                        $startTime = $null
                    }
                }
            }
            $cmd = [string]$_.CommandLine
            if ($cmd.Length -gt 90) {
                $cmd = $cmd.Substring(0, 87) + "..."
            }
            [pscustomobject]@{
                ProcessName = $_.Name
                Id = $_.ProcessId
                StartTime = $startTime
                CommandLine = $cmd
            }
        } |
        Sort-Object StartTime -Descending |
        Select-Object -First 12 |
        Format-Table -AutoSize

    Write-Host ""
    Write-Host "Press Ctrl+C to stop watching."
}

do {
    Show-AgentLoopState
    if ($Once) { break }
    Start-Sleep -Seconds $IntervalSeconds
} while ($true)
