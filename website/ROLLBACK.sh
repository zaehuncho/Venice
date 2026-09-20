#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE="${1:-}"

if [[ "$MODE" == "--stripe-fix-copy" ]]; then
  TARGET="${2:?target directory required}"
  mkdir -p "$TARGET/src" "$TARGET/tests"
  TARGET_ABS="$(cd "$TARGET" && pwd)"
  [[ "$TARGET_ABS" != "/" ]] || { echo "Refusing root target." >&2; exit 10; }
  cp "$ROOT/../.codex_artifacts/stripe-embedded-page-20260916/worker.ORIGINAL.js" "$TARGET_ABS/src/worker.js"
  cp "$ROOT/../.codex_artifacts/stripe-embedded-page-20260916/worker.test.ORIGINAL.mjs" "$TARGET_ABS/tests/worker.test.mjs"
  echo "ROLLBACK_STRIPE_FIX_OK=worker_and_test_originals_restored"
  exit 0
fi

if [[ "$MODE" == "--files-only" || "$MODE" == "--copy" ]]; then
  TARGET="${2:?target directory required}"
  TARGET_ABS="$(cd "$TARGET" && pwd)"
  [[ "$TARGET_ABS" != "/" ]] || { echo "Refusing root target." >&2; exit 10; }
  mkdir -p "$TARGET_ABS/public" "$TARGET_ABS/website/public" "$TARGET_ABS/backend" \
    "$TARGET_ABS/discord_launch/gumroad_webhook" "$TARGET_ABS/discord_launch"
  cp "$ROOT/../.codex_artifacts/venice-site-20260915/index.ORIGINAL.html" "$TARGET_ABS/public/index.html"
  cp "$ROOT/../.codex_artifacts/venice-site-20260915/index.ORIGINAL.html" "$TARGET_ABS/website/public/index.html"
  cp "$ROOT/../.codex_artifacts/venice-site-20260915/backend_lambda.ORIGINAL.py" "$TARGET_ABS/backend/lambda_function.py"
  cp "$ROOT/../.codex_artifacts/venice-site-20260915/gumroad_webhook.ORIGINAL.py" "$TARGET_ABS/discord_launch/gumroad_webhook/lambda_function.py"
  cp "$ROOT/../.codex_artifacts/venice-site-20260915/triton_gateway.PREWEBSITE.py" "$TARGET_ABS/discord_launch/orion_bot.py"
  rm -f "$TARGET_ABS/public/styles.css" "$TARGET_ABS/public/app.js" "$TARGET_ABS/public/orion.png" \
    "$TARGET_ABS/public/favicon.ico" "$TARGET_ABS/public/terms.html" "$TARGET_ABS/public/privacy.html" \
    "$TARGET_ABS/public/refunds.html" "$TARGET_ABS/public/legal.css" "$TARGET_ABS/public/404.html" \
    "$TARGET_ABS/public/robots.txt" "$TARGET_ABS/public/sitemap.xml" "$TARGET_ABS/src/worker.js" \
    "$TARGET_ABS/wrangler.jsonc" "$TARGET_ABS/package.json"
  echo "ROLLBACK_FILES_OK=website_backend_webhook_bot_originals_restored"
  exit 0
fi

if [[ "$MODE" == "--cloud" ]]; then
  cd "$ROOT"
  npx wrangler delete --name venice-site-production --force
  echo "ROLLBACK_CLOUD_OK=worker_route_removed_origin_restored"
  exit 0
fi

echo "Usage: ROLLBACK.sh --stripe-fix-copy TARGET | --copy TARGET | --files-only TARGET | --cloud" >&2
exit 2
