#!/usr/bin/env bash
set -euo pipefail
expected=f68dc63f08848324efacb4ffe9e573c42e3f92ab91af57b8341d2fc65ff71876
target="${1:-}"
if [[ -z "$target" || ! -f "$target" || -L "$target" ]]; then
  printf 'ROLLBACK invalid regular-file target\n' >&2
  exit 2
fi
actual="$(sha256sum "$target" | awk '{print $1}')"
if [[ "$actual" != "$expected" ]]; then
  printf 'ROLLBACK hash mismatch\n' >&2
  exit 3
fi
rm -- "$target"
if [[ -e "$target" ]]; then
  printf 'ROLLBACK target still exists\n' >&2
  exit 4
fi
printf 'ROLLBACK removed exact modified report; original state absent\n'
