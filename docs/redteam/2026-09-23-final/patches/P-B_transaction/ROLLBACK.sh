#!/usr/bin/env bash
set -euo pipefail
if [ "$#" -ne 2 ]; then echo "usage: ROLLBACK.sh ORIGINAL_FILE TARGET_COPY" >&2; exit 2; fi
cp -- "$1" "$2"
cmp -- "$1" "$2"
echo "RESTORED=1"
