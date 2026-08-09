<#
.SYNOPSIS
    Convenience wrapper for the bounded Claude + Codex Orion agent loop.

.DESCRIPTION
    Resolves the local Claude and Codex CLI binaries, then invokes the safe
    `agent-loop.ps1` harness from the NexusVision repo root.

    By default this wrapper performs a dry run. Pass -Run to execute real rounds.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_agent_loop.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\run_agent_loop.ps1 -Run -MaxRounds 10
#>

[CmdletBinding()]
param(
    [int]$MaxRounds = 10,
    [switch]$Run,
    [switch]$SkipVerify,
    [switch]$VerifyStrict,
    [ValidateSet("default", "acceptEdits", "plan", "bypassPermissions")]
    [string]$ClaudePermissionMode = "acceptEdits"
)

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$LoopScript = Join-Path $RepoRoot "agent-loop.ps1"

function Resolve-FirstExistingPath {
    param([string[]]$Candidates)
    foreach ($candidate in $Candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return $null
}

function Resolve-CommandOrFallback {
    param(
        [string]$CommandName,
        [string[]]$Fallbacks
    )

    $cmd = Get-Command $CommandName -ErrorAction SilentlyContinue
    if ($cmd) {
        return $cmd.Source
    }
    return (Resolve-FirstExistingPath -Candidates $Fallbacks)
}

$ClaudeBin = Resolve-CommandOrFallback -CommandName "claude" -Fallbacks @(
    "$env:USERPROFILE\.local\bin\claude.exe"
)

$CodexBin = Resolve-CommandOrFallback -CommandName "codex" -Fallbacks @(
    "$env:USERPROFILE\.vscode\extensions\openai.chatgpt-26.601.21317-win32-x64\bin\windows-x86_64\codex.exe"
)

if (-not (Test-Path -LiteralPath $LoopScript)) {
    throw "Missing loop script: $LoopScript"
}
if (-not $ClaudeBin) {
    throw "Claude CLI was not found. Install Claude Code or pass the binary path directly to agent-loop.ps1."
}
if (-not $CodexBin) {
    throw "Codex CLI was not found. Install Codex CLI or pass the binary path directly to agent-loop.ps1."
}

Set-Location $RepoRoot

$argsForLoop = @(
    "-MaxRounds", $MaxRounds,
    "-ClaudeBin", $ClaudeBin,
    "-CodexBin", $CodexBin,
    "-ClaudePermissionMode", $ClaudePermissionMode
)

if (-not $Run) {
    $argsForLoop += "-DryRun"
}
if ($SkipVerify) {
    $argsForLoop += "-SkipVerify"
}
if ($VerifyStrict) {
    $argsForLoop += "-VerifyStrict"
}

Write-Host "[orion-agents] Repo: $RepoRoot"
Write-Host "[orion-agents] Claude: $ClaudeBin"
Write-Host "[orion-agents] Codex: $CodexBin"
if (-not $Run) {
    Write-Host "[orion-agents] Dry run only. Add -Run to execute real rounds." -ForegroundColor Yellow
}

& powershell -ExecutionPolicy Bypass -File $LoopScript @argsForLoop
exit $LASTEXITCODE
