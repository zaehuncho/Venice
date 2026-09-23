"""The sidecar's processed-sequence provenance must have one source key."""

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "native_orion" / "backend" / "autogreen_sidecar.py"


def test_processed_seq_is_not_overwritten_in_a_dictionary_literal():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    duplicates = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
            if keys.count("processed_seq") > 1:
                duplicates.append(node.lineno)
    assert not duplicates, f"duplicate processed_seq dictionary keys at lines {duplicates}"
