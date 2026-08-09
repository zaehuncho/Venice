<#
.SYNOPSIS
    Bounded Claude (implementer) + Codex (reviewer/debugger) collaboration loop for
    the NexusVision / Orion repo.

.DESCRIPTION
    Each round:
      1. Claude reads AGENT_RULES.md / TASK.md / STATUS.md / AGENT_DIALOGUE.md and implements ONE small
         step, runs the relevant tests, and updates STATUS.md.
      2. Codex reviews the current git diff, fixes only obvious issues, runs tests,
         and updates STATUS.md with approved / needs-changes / blocked.
      3. The loop runs the authoritative verification gate, then prints
         `git status` and `git diff --stat`.
      4. Safety conditions are evaluated; the loop halts if any trips.

    This is a SAFE-BY-DEFAULT, BOUNDED loop. It is never unlimited unless you pass
    -Unlimited, and every per-round stop condition still applies even then.

    NOTHING runs until you explicitly invoke this script. Use -DryRun to preview the
    exact commands without executing anything.

.NOTES
    Read AGENT_RULES.md before using. Default: 2 rounds, no gameplay/core changes,
    no secret/auth/licensing/deployment edits.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File agent-loop.ps1 -DryRun
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File agent-loop.ps1 -MaxRounds 2
#>

[CmdletBinding()]
param(
    # Maximum number of Claude->Codex rounds. Default 2 (per AGENT_RULES.md).
    [int]$MaxRounds = 2,

    # Explicit opt-in to a very high round cap. Per-round stop conditions still apply.
    [switch]$Unlimited,

    # Preview the resolved commands and exit without running anything.
    [switch]$DryRun,

    # Skip the loop's own verification gate (rely on the agents). Faster harness test.
    [switch]$SkipVerify,

    # Force the strict (-StrictSecurity) verification every round.
    [switch]$VerifyStrict,

    # Proceed with only the Claude half if `codex` is not installed (degraded mode).
    [switch]$AllowMissingCodex,

    # Grant the agents broad tool autonomy (Claude bypassPermissions, Codex bypass
    # sandbox). OFF by default. Only use if you understand the risk.
    [switch]$Yolo,

    # Tool binaries (override if not on PATH or named differently).
    [string]$ClaudeBin = "claude",
    [string]$CodexBin  = "codex",
    [string]$Python    = "python",

    # Claude permission mode for headless runs: default | acceptEdits | plan |
    # bypassPermissions. acceptEdits lets it edit files without prompting; the loop is
    # the authoritative test gate so the agent does not need bash to prove the change.
    [ValidateSet("default", "acceptEdits", "plan", "bypassPermissions")]
    [string]$ClaudePermissionMode = "acceptEdits"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$VerifyScript = Join-Path $Root "scripts\verify_orion.ps1"
$StatusFile   = Join-Path $Root "STATUS.md"
$TaskFile     = Join-Path $Root "TASK.md"
$LogDir       = Join-Path $Root "logs\agent-loop"
$NotifyScript = Join-Path $env:USERPROFILE "ai-notify\notify-phone.ps1"

if ($Yolo) {
    $ClaudePermissionMode = "bypassPermissions"
}

# --------------------------------------------------------------------------------
# Protected-file patterns (kept in sync with AGENT_RULES.md). Paths are matched with
# forward slashes via -like (wildcards).
# --------------------------------------------------------------------------------

# Absolute hard stop -- never auto-edited, no override.
$SecretArtifacts = @(
    ".vault/*", "*/.vault/*",
    "settings.json", "settings.json.sig", "settings.json.local",
    "license_cache.*", "*.enc",
    "*.key", "*.pem", "*.crt", "*.p12", "*.pfx",
    "credentials.json", "auth_tokens.json",
    "codesigning/*", "*/codesigning/*"
)

# Security / licensing / release code -- stop UNLESS TASK.md has ALLOW_SECURITY_FILES: yes.
# When authorized, triggers a strict verification.
$SensitiveCode = @(
    "tools/package_orion_release.py",
    "docs/RELEASE_SECURITY.md", "docs/CODE_SIGNING.md",
    "native_orion/src/SecurityManager.*", "native_orion/src/LicenseClient.*",
    "requirements.txt", "version.json",
    "*deploy*", "*release*", "native_orion/CMakeLists.txt"
)

# --------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------

function Write-Section($text) {
    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Cyan
    Write-Host " $text" -ForegroundColor Cyan
    Write-Host "==================================================================" -ForegroundColor Cyan
}

function Resolve-Tool($name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-GitRepo {
    try {
        $inside = (& git -c core.autocrlf=false rev-parse --is-inside-work-tree 2>$null)
        return ($LASTEXITCODE -eq 0 -and $inside -eq "true")
    } catch {
        return $false
    }
}

# Files changed vs the working-tree baseline: tracked modifications + new untracked
# (non-ignored) files. Returns forward-slash relative paths.
function Get-ChangedFiles {
    $changed = @()
    $tracked = (& git -c core.autocrlf=false diff --name-only) 2>$null
    if ($tracked) { $changed += $tracked }
    $staged = (& git -c core.autocrlf=false diff --name-only --cached) 2>$null
    if ($staged) { $changed += $staged }
    $untracked = (& git -c core.autocrlf=false ls-files --others --exclude-standard) 2>$null
    if ($untracked) { $changed += $untracked }
    return @($changed | Where-Object { $_ -and $_.Trim() -ne "" } |
        ForEach-Object { $_.Replace("\", "/") } | Select-Object -Unique)
}

function Get-DeletedFiles {
    $deleted = (& git -c core.autocrlf=false diff --name-only --diff-filter=D) 2>$null
    return @($deleted | Where-Object { $_ -and $_.Trim() -ne "" } |
        ForEach-Object { $_.Replace("\", "/") } | Select-Object -Unique)
}

function Test-MatchesAny($path, $patterns) {
    foreach ($p in $patterns) {
        if ($path -like $p) { return $true }
    }
    return $false
}

function Invoke-AgentLoopNotify($title, $message) {
    if (-not (Test-Path -LiteralPath $NotifyScript)) {
        return
    }
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $NotifyScript -Title $title -Message $message | Out-Null
    } catch {
        # Notification failure must never alter loop behavior.
    }
}

function Get-TaskAllowsSecurity {
    if (-not (Test-Path $TaskFile)) { return $false }
    $content = Get-Content $TaskFile -Raw
    return ($content -match "(?im)^\s*ALLOW_SECURITY_FILES:\s*yes\s*$")
}

# Reads the token block under STATUS.md -> "## Current Status".
function Get-LatestStatusTokens {
    $tokens = @{ DECISION = ""; STOP_REQUESTED = ""; LIVE_VALIDATION_REQUIRED = "" }
    if (-not (Test-Path $StatusFile)) { return $tokens }
    $insideCurrent = $false
    $insideBlock = $false
    foreach ($line in (Get-Content $StatusFile)) {
        if ($line -match '^##\s+Current Status') {
            $insideCurrent = $true
            continue
        }
        if ($insideCurrent -and $line -match '^```') {
            if (-not $insideBlock) {
                $insideBlock = $true
                continue
            }
            break
        }
        if (-not $insideBlock) {
            continue
        }
        if ($line -match "(?i)^\s*DECISION:\s*(approved|needs-changes|blocked)") {
            $tokens.DECISION = $Matches[1].ToLower()
        } elseif ($line -match "(?i)^\s*STOP_REQUESTED:\s*(yes|no)") {
            $tokens.STOP_REQUESTED = $Matches[1].ToLower()
        } elseif ($line -match "(?i)^\s*LIVE_VALIDATION_REQUIRED:\s*(yes|no)") {
            $tokens.LIVE_VALIDATION_REQUIRED = $Matches[1].ToLower()
        }
    }
    return $tokens
}

# --------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------

$ClaudePrompt = @'
You are the IMPLEMENTER in a bounded Claude+Codex loop for the NexusVision/Orion repo.

Before doing anything, read these files in the repo root: AGENT_RULES.md, TASK.md,
STATUS.md, AGENT_DIALOGUE.md, TEST_COMMANDS.md. AGENT_RULES.md overrides anything here.
Use the compact context generated by `tools/agents/compact_agent_context.py --mode claude`
as a guide, but the repository files are authoritative.

Do EXACTLY this, then stop:
0. Check AGENT_DIALOGUE.md. If Codex left `needs-changes` or `rebuttal-needed`,
   either make the exact requested fix or write a short evidence-backed rebuttal
   in AGENT_DIALOGUE.md. If you rebut, do not broaden scope.
1. Implement ONE small step from TASK.md -> "Active Task". Only edit the files that
   task explicitly allows. Do NOT make gameplay/core timing changes unless TASK.md's
   header has GAMEPLAY_CHANGES: yes and names the file.
2. Do NOT touch any protected file (secrets, auth, licensing, deployment, release).
3. Do NOT delete files. Keep the patch narrow, test-backed, and reversible.
4. Run the relevant tests from TEST_COMMANDS.md for what you changed.
5. Append a short proposal/open-question entry to AGENT_DIALOGUE.md, then append a
   new round entry to STATUS.md and refresh its Current Status token block.
   Always set: DECISION, STOP_REQUESTED, LIVE_VALIDATION_REQUIRED.
   If progress needs live PS5/Remote Play gameplay, set LIVE_VALIDATION_REQUIRED: yes
   and STOP_REQUESTED: yes and stop.
6. Stop after this one focused pass. Do not start the next step or any refactor.
'@

$CodexPrompt = @'
You are the REVIEWER/DEBUGGER in a bounded Claude+Codex loop for the NexusVision/Orion
repo.

Before doing anything, read these files in the repo root: AGENT_RULES.md, TASK.md,
STATUS.md, AGENT_DIALOGUE.md, TEST_COMMANDS.md. AGENT_RULES.md overrides anything here.
Use the compact context generated by `tools/agents/compact_agent_context.py --mode codex`
as a guide, but the repository files and `git diff` are authoritative.

Do EXACTLY this, then stop:
1. Review the current `git diff` (the implementer's change this round).
2. Identify bugs, regressions, missing tests, unsafe assumptions, and scope creep.
3. Fix ONLY obvious, clearly-correct issues (typo, missing import, off-by-one, a
   missing test for code just added). Do NOT redesign, refactor, or expand scope.
4. Do NOT touch protected files (secrets, auth, licensing, deployment, release) and
   do NOT delete files.
5. Run the relevant tests from TEST_COMMANDS.md.
6. Append a concise review entry to AGENT_DIALOGUE.md with your decision, exact
   blocker/fix, evidence/tests, and agreement state. Then append a round entry to
   STATUS.md and refresh the Current Status tokens with your decision:
   DECISION: approved | needs-changes | blocked (plus STOP_REQUESTED and
   LIVE_VALIDATION_REQUIRED). If a real regression or unclear build break exists,
   use blocked.
'@

# --------------------------------------------------------------------------------
# Agent invocations
# --------------------------------------------------------------------------------

function Build-ClaudeArgs($prompt) {
    $context = ""
    try {
        $context = & $Python (Join-Path $Root "tools\agents\compact_agent_context.py") --mode claude --max-lines 180
    } catch {
        $context = "[compact context unavailable: $($_.Exception.Message)]"
    }
    return @("-p", "$prompt`n`n<context>`n$context`n</context>", "--permission-mode", $ClaudePermissionMode, "--add-dir", $Root)
}

function Build-CodexArgs($prompt) {
    $context = ""
    try {
        $context = & $Python (Join-Path $Root "tools\agents\compact_agent_context.py") --mode codex --max-lines 220
    } catch {
        $context = "[compact context unavailable: $($_.Exception.Message)]"
    }
    $fullPrompt = "$prompt`n`n<context>`n$context`n</context>"
    # `codex exec` is the non-interactive entrypoint. --full-auto = sandboxed
    # auto-approval (workspace-write). -Yolo escalates to bypass the sandbox.
    if ($Yolo) {
        return @("exec", "--dangerously-bypass-approvals-and-sandbox", $fullPrompt)
    }
    return @("exec", "--full-auto", $fullPrompt)
}

function Invoke-Claude {
    $a = Build-ClaudeArgs $ClaudePrompt
    Write-Host "[loop] claude $($a[0]) <prompt> --permission-mode $ClaudePermissionMode --add-dir <root>" -ForegroundColor DarkGray
    & $ClaudeBin @a
    return $LASTEXITCODE
}

function Invoke-Codex {
    $a = Build-CodexArgs $CodexPrompt
    Write-Host "[loop] codex $($a[0]) ... <prompt>" -ForegroundColor DarkGray
    & $CodexBin @a
    return $LASTEXITCODE
}

# --------------------------------------------------------------------------------
# Verification gate
# --------------------------------------------------------------------------------

# Returns a hashtable: @{ Ok = $bool; UnclearBuildBreak = $bool; LogPath = <path> }
function Invoke-Verify($useStrict) {
    if ($SkipVerify) {
        Write-Host "[loop] -SkipVerify set; skipping verification gate." -ForegroundColor Yellow
        return @{ Ok = $true; UnclearBuildBreak = $false; LogPath = $null; Skipped = $true }
    }
    if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    $logPath = Join-Path $LogDir "verify-$stamp.log"

    $verifyArgs = @("-ExecutionPolicy", "Bypass", "-File", $VerifyScript, "-Python", $Python)
    if ($useStrict -or $VerifyStrict) { $verifyArgs += "-StrictSecurity" }

    Write-Host "[loop] powershell $($verifyArgs -join ' ')" -ForegroundColor DarkGray
    $out = & powershell @verifyArgs 2>&1
    $exit = $LASTEXITCODE
    $out | Out-File -FilePath $logPath -Encoding utf8
    $out | ForEach-Object { Write-Host $_ }

    $joined = ($out | Out-String)
    $unclear = $false
    if ($exit -ne 0) {
        # A failure in the native build step is an "unclear build break" (halt now);
        # a plain pytest assertion failure is a countable test failure.
        if ($joined -match "Native build failed" -or $joined -match "cmake") {
            if ($joined -match "Native build failed") { $unclear = $true }
        }
    }
    return @{ Ok = ($exit -eq 0); UnclearBuildBreak = $unclear; LogPath = $logPath; Skipped = $false }
}

# --------------------------------------------------------------------------------
# Safety evaluation after a round. Returns $null to continue, or a string reason to stop.
# --------------------------------------------------------------------------------

function Test-StopConditions([ref]$FailCount, $VerifyResult) {
    # 1. Deletions.
    $deleted = Get-DeletedFiles
    if ($deleted.Count -gt 0) {
        return "Tracked file deletion detected (never delete major files): " + ($deleted -join ", ")
    }

    # 2. Secret/auth artifacts -- absolute hard stop.
    $changed = Get-ChangedFiles
    $secretHits = @($changed | Where-Object { Test-MatchesAny $_ $SecretArtifacts })
    if ($secretHits.Count -gt 0) {
        return "Secret/auth artifact would be touched (never allowed): " + ($secretHits -join ", ")
    }

    # 3. Security/licensing/release code -- stop unless TASK.md authorizes.
    $sensHits = @($changed | Where-Object { Test-MatchesAny $_ $SensitiveCode })
    if ($sensHits.Count -gt 0 -and -not (Get-TaskAllowsSecurity)) {
        return "Security/licensing/release file changed without TASK.md ALLOW_SECURITY_FILES: yes -> " + ($sensHits -join ", ")
    }

    # 4. Too many files changed.
    if ($changed.Count -gt 20) {
        return "More than 20 files changed ($($changed.Count)); halting for human review."
    }

    # 5. Verification outcome.
    if ($VerifyResult -and -not $VerifyResult.Skipped) {
        if ($VerifyResult.UnclearBuildBreak) {
            return "Native build broke unclearly (see $($VerifyResult.LogPath))."
        }
        if (-not $VerifyResult.Ok) {
            $FailCount.Value = $FailCount.Value + 1
            Write-Host "[loop] Verification failed (failure #$($FailCount.Value))." -ForegroundColor Yellow
            if ($FailCount.Value -ge 2) {
                return "Tests failed twice; halting per safety rules."
            }
        }
    }

    # 6. Agent-signalled stop via STATUS.md tokens.
    $tokens = Get-LatestStatusTokens
    if ($tokens.LIVE_VALIDATION_REQUIRED -eq "yes") {
        return "An agent flagged LIVE_VALIDATION_REQUIRED: yes -- live gameplay validation needed."
    }
    if ($tokens.STOP_REQUESTED -eq "yes") {
        return "An agent flagged STOP_REQUESTED: yes."
    }
    if ($tokens.DECISION -eq "blocked") {
        return "Codex set DECISION: blocked."
    }

    return $null
}

# --------------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------------

Write-Section "Preflight"

$claudePath = Resolve-Tool $ClaudeBin
$codexPath  = Resolve-Tool $CodexBin

Write-Host "where claude : " -NoNewline
if ($claudePath) { Write-Host $claudePath -ForegroundColor Green } else { Write-Host "NOT FOUND" -ForegroundColor Red }
Write-Host "where codex  : " -NoNewline
if ($codexPath)  { Write-Host $codexPath  -ForegroundColor Green } else { Write-Host "NOT FOUND" -ForegroundColor Red }

$isGit = Test-GitRepo
Write-Host "git repo     : " -NoNewline
if ($isGit) { Write-Host "yes" -ForegroundColor Green } else { Write-Host "NO" -ForegroundColor Red }
if ($isGit) {
    Write-Host "--- git status ---"
    & git status --short
}

$fatal = $false

if (-not $claudePath) {
    Write-Host "[fatal] '$ClaudeBin' not found on PATH. Install Claude Code or pass -ClaudeBin <path>." -ForegroundColor Red
    $fatal = $true
}
if (-not $codexPath) {
    if ($AllowMissingCodex) {
        Write-Host "[warn] '$CodexBin' not found -- running CLAUDE-ONLY (degraded) because -AllowMissingCodex was set." -ForegroundColor Yellow
    } else {
        Write-Host "[fatal] '$CodexBin' not found on PATH. Install the Codex CLI, pass -CodexBin <path>, or re-run with -AllowMissingCodex for the Claude-only half." -ForegroundColor Red
        $fatal = $true
    }
}
if (-not $isGit) {
    Write-Host "[fatal] This directory is not a git repository, but the Codex review step needs a `git diff` baseline." -ForegroundColor Red
    Write-Host "        Initialize it first, e.g.:" -ForegroundColor Red
    Write-Host "            git init" -ForegroundColor Red
    Write-Host "            git add -A" -ForegroundColor Red
    Write-Host "            git commit -m \"baseline before agent loop\"" -ForegroundColor Red
    $fatal = $true
}
if (-not (Test-Path $VerifyScript)) {
    Write-Host "[fatal] Verification script not found: $VerifyScript" -ForegroundColor Red
    $fatal = $true
}

# Resolve effective round cap.
$effectiveMax = $MaxRounds
if ($Unlimited) {
    $effectiveMax = 1000
    Write-Host "[warn] -Unlimited set: round cap raised to $effectiveMax. ALL per-round stop conditions still apply." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Config: MaxRounds=$effectiveMax  ClaudePermissionMode=$ClaudePermissionMode  Yolo=$Yolo  SkipVerify=$SkipVerify  VerifyStrict=$VerifyStrict  AllowMissingCodex=$AllowMissingCodex"

if ($DryRun) {
    Write-Section "DRY RUN -- no commands executed"
    Write-Host "Claude command per round:"
    Write-Host "  $ClaudeBin -p <ClaudePrompt + compact context> --permission-mode $ClaudePermissionMode --add-dir $Root"
    Write-Host ""
    Write-Host "Codex command per round:"
    if ($Yolo) {
        Write-Host "  $CodexBin exec --dangerously-bypass-approvals-and-sandbox <CodexPrompt + compact context>"
    } else {
        Write-Host "  $CodexBin exec --full-auto <CodexPrompt + compact context>"
    }
    Write-Host ""
    $dv = @("-ExecutionPolicy", "Bypass", "-File", $VerifyScript, "-Python", $Python)
    if ($VerifyStrict) { $dv += "-StrictSecurity" }
    Write-Host "Verification per round:"
    Write-Host "  powershell $($dv -join ' ')   (adds -StrictSecurity automatically if a security/release file changed and TASK.md authorizes it)"
    Write-Host ""
    Write-Host "After each round the loop prints: git status + git diff --stat, then checks stop conditions."
    if ($fatal) { Write-Host "`n[note] Preflight has FATAL issues above; a real run would refuse to start." -ForegroundColor Yellow }
    return
}

if ($fatal) {
    Write-Host "`n[loop] Preflight failed. Resolve the issues above and re-run. Nothing was executed." -ForegroundColor Red
    Invoke-AgentLoopNotify "Orion Agent Loop" "Preflight failed. Resolve the issues in the loop window before re-running."
    exit 2
}

# --------------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------------

$failCount = 0
$stopReason = $null

for ($round = 1; $round -le $effectiveMax; $round++) {
    Write-Section "Round $round / $effectiveMax"

    # --- Claude: implement one step ---
    Write-Host "--- Claude (implementer) ---" -ForegroundColor Magenta
    $claudeExit = Invoke-Claude
    if ($claudeExit -ne 0) {
        Write-Host "[warn] Claude exited with code $claudeExit." -ForegroundColor Yellow
    }

    # --- Codex: review/fix ---
    if ($codexPath) {
        Write-Host "--- Codex (reviewer/debugger) ---" -ForegroundColor Magenta
        $codexExit = Invoke-Codex
        if ($codexExit -ne 0) {
            Write-Host "[warn] Codex exited with code $codexExit." -ForegroundColor Yellow
        }
    } else {
        Write-Host "[warn] Skipping Codex (not installed; -AllowMissingCodex)." -ForegroundColor Yellow
    }

    # --- Determine strict verification need from changed files ---
    $changedNow = Get-ChangedFiles
    $needStrict = (@($changedNow | Where-Object { Test-MatchesAny $_ $SensitiveCode }).Count -gt 0) -and (Get-TaskAllowsSecurity)

    # --- Authoritative verification gate ---
    Write-Host "--- Verification ---" -ForegroundColor Magenta
    $verify = Invoke-Verify $needStrict

    # --- Status snapshot ---
    Write-Host "--- git status ---"
    & git status --short
    Write-Host "--- git diff --stat ---"
    & git diff --stat

    # --- Stop conditions ---
    $stopReason = Test-StopConditions ([ref]$failCount) $verify
    if ($stopReason) {
        Write-Section "STOP after round $round"
        Write-Host $stopReason -ForegroundColor Yellow
        break
    }

    Write-Host "[loop] Round $round complete; verification $([bool]$verify.Ok)." -ForegroundColor Green
}

Write-Section "Loop finished"
if ($stopReason) {
    Write-Host "Halted early: $stopReason" -ForegroundColor Yellow
    Invoke-AgentLoopNotify "Orion Agent Loop" "Agent loop halted early: $stopReason"
} else {
    Write-Host "Completed $effectiveMax round(s) with no stop condition tripped." -ForegroundColor Green
    Invoke-AgentLoopNotify "Orion Agent Loop" "Agent loop completed $effectiveMax round(s) with no stop condition tripped. Review STATUS.md and logs."
}
Write-Host "Review STATUS.md and the logs under logs\agent-loop\ before any further run."
