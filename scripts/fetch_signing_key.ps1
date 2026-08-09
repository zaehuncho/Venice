# Fetches the Ed25519 release-signing private key from AWS SSM into codesigning\.
#
# Why this exists as a script: the same one-liner has to be written three different ways
# depending on whether it is pasted into Git Bash, PowerShell, or cmd (Out-File vs >,
# MSYS path mangling of "/orion/...", and relative paths resolving against ~ instead of
# the repo). A script removes all three variables.
#
# The key is written to disk and NEVER printed. This script reports only length and shape.
# codesigning\ is gitignored (as are *.pem and *.key), so the key cannot be committed.

$ErrorActionPreference = "Stop"

$repo = Split-Path -Parent $PSScriptRoot
$dir  = Join-Path $repo "codesigning"
$dest = Join-Path $dir  "venice_update_signing.pem"

if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }

Write-Host "Fetching /orion/ed25519_private_key from AWS SSM..."

# --% stops PowerShell parsing the rest, so the leading slash in the parameter name is
# passed through verbatim instead of being treated as a path or an operator.
$value = aws --% ssm get-parameter --name /orion/ed25519_private_key --with-decryption --query Parameter.Value --output text

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "FAILED: aws returned exit code $LASTEXITCODE" -ForegroundColor Red
    Write-Host "If this says AccessDenied, check the resource name in the error: a mangled"
    Write-Host "path (e.g. 'parameter/C') means the shell rewrote the argument, not that"
    Write-Host "your permissions are wrong."
    exit 1
}

$text = ($value | Out-String).Trim()
if ([string]::IsNullOrWhiteSpace($text)) {
    Write-Host "FAILED: SSM returned an empty value." -ForegroundColor Red
    exit 1
}

# PEM needs LF line endings and NO byte-order mark; a UTF-8 BOM breaks PEM parsing.
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($dest, ($text -replace "`r`n", "`n") + "`n", $utf8NoBom)

# Report shape only - never the key material.
$bytes  = (Get-Item $dest).Length
$first  = (Get-Content $dest -TotalCount 1)
$isPem  = $first -match "BEGIN .*PRIVATE KEY"

Write-Host ""
Write-Host "wrote  : $dest"
Write-Host "bytes  : $bytes"
Write-Host "header : $first"
if ($isPem) {
    Write-Host "STATUS : looks like a PEM private key - ready for packaging" -ForegroundColor Green
} else {
    Write-Host "STATUS : does NOT look like a PEM private key - check the SSM parameter" -ForegroundColor Yellow
}
