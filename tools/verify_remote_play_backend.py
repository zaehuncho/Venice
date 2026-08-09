from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from remote_play_client import find_remote_play_window  # noqa: E402


def find_bundled_chiaki_binary() -> str:
    """Return only Orion's patched runtime, never an unrelated stock install."""
    candidate = os.path.join(
        ROOT,
        "native_orion",
        "deploy",
        "chiaki-ng-orion",
        "chiaki-ng-Win",
        "OrionStream.exe",
    )
    return candidate if os.path.isfile(candidate) else ""


def main() -> int:
    chiaki = find_bundled_chiaki_binary()
    hwnd, title = find_remote_play_window()
    payload = {
        "chiaki_binary": chiaki or None,
        "remote_play_window": {"hwnd": hwnd or 0, "title": title or None},
        "build_bundle_ready": bool(chiaki),
        # Orion's frame and input bridges exist only in the bundled custom
        # Chiaki client.  A Sony PS Remote Play install is not a viable fallback
        # for the production detector path and must not make this check pass.
        "runtime_client_available": bool(chiaki),
    }
    print(json.dumps(payload, indent=2))
    if not payload["runtime_client_available"]:
        print("Orion's bundled custom Chiaki client was not found.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
