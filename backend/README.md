# Orion Backend

This directory contains the server-side backend for the Orion license system.

## Structure
```
backend/
  lambda_function.py  # AWS Lambda handler (Python 3.12)
  test_endpoints.sh   # Direct API Gateway smoke tests
  README.md           # This file
docs/
  SERVER_HANDOFF.md   # Full infrastructure and ops documentation
```

## Quick start
See `../docs/SERVER_HANDOFF.md` for full details on endpoints, admin workflows,
AWS resources, secret names, and CloudWatch alarms.

## Deployment
From CloudShell (us-east-1):
```bash
cd backend
zip lambda.zip lambda_function.py
aws lambda update-function-code --function-name orion-activate \
  --zip-file fileb://lambda.zip --region us-east-1
```

Before deploying, confirm this local file has been merged with the current live
Lambda revision. The live backend may contain newer routes such as `/api/update`
and Ed25519 update-manifest signing that must not be overwritten by an older
local export. After any deploy, run the non-secret route contract check:

```bash
python tools/admin/check_backend_contract.py --base-url https://api.zaeorion.com
```

The check must report that `/api/staff/whoami`, `/api/staff/login`, and
`/api/admin/staff` exist and fail closed. A `404` means API Gateway or Lambda is
still missing the staff/owner route contract.
