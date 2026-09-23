#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 1 ]; then
  printf 'Usage: ROLLBACK.sh ABSOLUTE_COPY_ROOT\n' >&2
  exit 64
fi
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
helper="$script_dir/transaction_check.py"
if command -v cygpath >/dev/null 2>&1; then
  helper="$(cygpath -m "$helper")"
fi
exec python -B "$helper" rollback --root "$1"
