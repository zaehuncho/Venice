#!/usr/bin/env bash
set -euo pipefail

# Orion backend endpoint smoke tests.
# Usage:
#   ADMIN_SECRET=<secret> TEST_KEY=ORION-... MACHINE_ID=<machine_hash> bash backend/test_endpoints.sh
#
# Uses the direct API Gateway URL because Cloudflare can block CloudShell IPs.

DIRECT="${DIRECT:-https://v348t5hg3i.execute-api.us-east-1.amazonaws.com}"
ADMIN_SECRET="${ADMIN_SECRET:-SET_ME}"
TEST_KEY="${TEST_KEY:-ORION-TEST-2026-0002}"
MACHINE_ID="${MACHINE_ID:-test-machine-$(date +%s)}"

nonce() {
  python3 -c 'import secrets; print(secrets.token_hex(16))'
}

pretty() {
  python3 -m json.tool
}

echo "=== 1. Activate configured key ==="
echo "Note: this may return device_mismatch if TEST_KEY is already bound to a different machine."
curl -s -X POST "$DIRECT/api/activate" \
  -H "Content-Type: application/json" \
  -d "{\"license_key\":\"$TEST_KEY\",\"machine_id\":\"$MACHINE_ID\",\"request_timestamp\":$(date +%s),\"request_nonce\":\"$(nonce)\"}" | pretty

echo ""
echo "=== 2. Version ==="
curl -s "$DIRECT/api/version" | pretty

echo ""
echo "=== 3. Invalid key ==="
curl -s -X POST "$DIRECT/api/activate" \
  -H "Content-Type: application/json" \
  -d "{\"license_key\":\"ORION-FAKE-FAKE-FAKE\",\"machine_id\":\"$MACHINE_ID\",\"request_timestamp\":$(date +%s),\"request_nonce\":\"$(nonce)\"}" | pretty

echo ""
echo "=== 4. Admin lookup ==="
curl -s "$DIRECT/api/admin/license?key=$TEST_KEY" \
  -H "X-Orion-Admin-Secret: $ADMIN_SECRET" | pretty

echo ""
echo "=== 5. Invalid admin secret ==="
curl -s "$DIRECT/api/admin/license?key=$TEST_KEY" \
  -H "X-Orion-Admin-Secret: bad-secret" | pretty

echo ""
echo "Done. Check output above for PASS/FAIL."
