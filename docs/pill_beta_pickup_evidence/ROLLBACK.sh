#!/usr/bin/env bash
set -euo pipefail

# Reverse only this review's reader patch. Refuse drift rather than overwrite
# someone else's concurrent edits. Pass a path to exercise it on a copy.
root="$(cd "$(dirname "$0")/../.." && pwd)"
patch="$root/docs/pill_beta_pickup_evidence/DIFF_FILE.patch"
original="$root/docs/pill_beta_pickup_evidence/ORIGINAL_simple_meter_reader.py"
target="${1:-$root/simple_meter_reader.py}"
case "$target" in /*) ;; *) target="$PWD/$target" ;; esac

expected_modified=54bdeba785a42c703bcf7903245b60f1dbaddefab0d91875f08e3365831fea21
expected_original=31255cb3f554d96cab64b093dad9d264131134f7fa9f185bef1517db1bed5065
expected_patch=2c18b7d1385797ad2eff9d9d3a864a7e7cf8edbfa9716acb36d91167a4643f27
hash() { sha256sum "$1" | awk '{print $1}'; }

[[ "$(basename "$target")" == "simple_meter_reader.py" ]] || { echo "TARGET_NAME_MISMATCH" >&2; exit 2; }
[[ "$(hash "$patch")" == "$expected_patch" ]] || { echo "PATCH_HASH_MISMATCH" >&2; exit 2; }
[[ "$(hash "$original")" == "$expected_original" ]] || { echo "ORIGINAL_HASH_MISMATCH" >&2; exit 2; }
[[ "$(hash "$target")" == "$expected_modified" ]] || { echo "TARGET_HASH_MISMATCH" >&2; exit 2; }

(cd "$(dirname "$target")" && git -c core.autocrlf=false apply --reverse --check "$patch")
(cd "$(dirname "$target")" && git -c core.autocrlf=false apply --reverse "$patch")
[[ "$(hash "$target")" == "$expected_original" ]] || { echo "RESTORED_HASH_MISMATCH" >&2; exit 3; }
if grep -q 'def _note_pill_pickup' "$target"; then
    echo "PILL_PICKUP_HOOK_STILL_PRESENT" >&2
    exit 3
fi
echo "RESTORED_SHA256=$expected_original"
echo "PILL_PICKUP_HOOK=absent"
