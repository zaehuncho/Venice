<#
  Orion licensing-server provisioning — the Red-Team-V2/V3 fixes need this infra before the
  new backend/lambda_function.py is deployed (several fixes FAIL CLOSED). Companion to
  docs/SERVER_DEPLOY_CHECKLIST.md.

  This script does ONLY the fully-specifiable, non-secret parts (the two DynamoDB tables) so
  it is safe to run unattended. Everything that requires a SECRET or an environment-specific
  VALUE is left as clearly-marked manual commands below — YOU run those with your values, so
  no secret is ever hard-coded or handled by the tooling.

  Prereq: AWS CLI v2 configured (`aws configure`) with an identity that can create tables /
  put SSM params / update the Lambda. Runs on YOUR credentials — this script never sees them.

  Usage:  ./provision_server.ps1                 # create the two tables in us-east-1
          ./provision_server.ps1 -Region us-west-2
#>
param([string]$Region = "us-east-1")

$ErrorActionPreference = "Stop"
function Test-Table($name) {
  try { aws dynamodb describe-table --table-name $name --region $Region *> $null; return $true }
  catch { return $false }
}

Write-Host "== Orion server provisioning (region=$Region) ==" -ForegroundColor Cyan

# 1) orion-ratelimit (MED-1 rate limiting). PK rl_key (S), TTL attribute 'expires' for auto-cleanup.
if (Test-Table "orion-ratelimit") {
  Write-Host "orion-ratelimit already exists — skipping." -ForegroundColor Yellow
} else {
  Write-Host "Creating orion-ratelimit ..."
  aws dynamodb create-table --table-name orion-ratelimit `
    --attribute-definitions AttributeName=rl_key,AttributeType=S `
    --key-schema AttributeName=rl_key,KeyType=HASH `
    --billing-mode PAY_PER_REQUEST --region $Region | Out-Null
  aws dynamodb wait table-exists --table-name orion-ratelimit --region $Region
  aws dynamodb update-time-to-live --table-name orion-ratelimit `
    --time-to-live-specification "Enabled=true,AttributeName=expires" --region $Region | Out-Null
  Write-Host "  orion-ratelimit ready (TTL on 'expires')." -ForegroundColor Green
}

# 2) orion-update-manifest (CRIT-2 pre-signed manifest store). PK record_id (S).
if (Test-Table "orion-update-manifest") {
  Write-Host "orion-update-manifest already exists — skipping." -ForegroundColor Yellow
} else {
  Write-Host "Creating orion-update-manifest ..."
  aws dynamodb create-table --table-name orion-update-manifest `
    --attribute-definitions AttributeName=record_id,AttributeType=S `
    --key-schema AttributeName=record_id,KeyType=HASH `
    --billing-mode PAY_PER_REQUEST --region $Region | Out-Null
  aws dynamodb wait table-exists --table-name orion-update-manifest --region $Region
  Write-Host "  orion-update-manifest ready." -ForegroundColor Green
}

Write-Host ""
Write-Host "== Tables done. The rest is YOURS (secret / environment-specific) ==" -ForegroundColor Cyan
Write-Host @"
Run these yourself with your real values (never commit the secrets):

# --- SSM SecureString secrets (fail-closed fixes depend on these) ---
#  Lease signing key (NEW-1): the PRIVATE key I generated is in the scratchpad PEM file
#  (or generate your own per docs/SERVER_DEPLOY_CHECKLIST.md §2). Load it as SecureString:
aws ssm put-parameter --name /orion/lease_signing_key --type SecureString --region $Region \
    --value file://path\to\orion_lease_signing_key.pem --overwrite

#  Edge auth secret MUST exist (HIGH-4 fails closed if absent -> every request 403):
aws ssm put-parameter --name /orion/edge_auth_secret --type SecureString --region $Region \
    --value '<YOUR_EDGE_SECRET>' --overwrite

#  Per-consumer bot secrets (HIGH-3) — independent rotation:
aws ssm put-parameter --name /orion/webhook_bot_secret --type SecureString --region $Region --value '<...>' --overwrite
aws ssm put-parameter --name /orion/worker_bot_secret  --type SecureString --region $Region --value '<...>' --overwrite   # optional

# --- SSM String (public, not secret) ---
#  Update-signing PUBLIC key as a plain String (CRIT-2 reads it WithDecryption=False):
aws ssm put-parameter --name /orion/ed25519_public_key --type String --region $Region --value '<UPDATE_PUB_B64>' --overwrite

# --- Lambda config ---
#  Set the artifact_url allow-list to your real release origin(s):
aws lambda update-function-configuration --function-name <ORION_LICENSE_FN> --region $Region \
    --environment "Variables={ORION_ARTIFACT_ALLOWLIST=releases.yourdomain.com}"

# --- Cloudflare worker: inject X-Edge-Auth on ALL proxied routes (incl. /api/bot/*). ---
# --- CloudWatch: metric filter on the literal [ALERT] token -> SNS/paging. ---

Then deploy the new backend/lambda_function.py + the two webhooks. Deploy the SECRETS FIRST
(the code fails closed without them). See docs/SERVER_DEPLOY_CHECKLIST.md for the full list.
"@ -ForegroundColor Gray
