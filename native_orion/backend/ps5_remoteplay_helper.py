#!/usr/bin/env python3
"""PS5 Remote Play helper for Orion Native.

This helper intentionally uses the normal pyremoteplay profile, OAuth, discovery,
and registration APIs. It does not extract device secrets, patch packets, or
modify console traffic.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any


def _print(payload: dict[str, Any], exit_code: int = 0) -> int:
    print(json.dumps(payload, separators=(",", ":"), sort_keys=True), flush=True)
    return exit_code


def _root_from_args(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


def _bootstrap(root: Path) -> None:
    candidates = [
        root / "analysis_cosmic_static" / "pyremoteplay_src",
        root.parent / "analysis_cosmic_static" / "pyremoteplay_src",
        Path(__file__).resolve().parents[2] / "analysis_cosmic_static" / "pyremoteplay_src",
    ]
    for candidate in candidates:
        if (candidate / "pyremoteplay").is_dir():
            sys.path.insert(0, str(candidate))
            break


def _profile_path(root: Path) -> Path:
    profiles_dir = root / "native_orion" / "profiles"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    return profiles_dir / "ps5_profiles.json"


def _imports() -> tuple[Any, Any, Any, Any, Any, Any]:
    from pyremoteplay.const import TYPE_PS5
    from pyremoteplay.device import RPDevice
    from pyremoteplay.oauth import get_login_url
    from pyremoteplay.profile import Profiles
    from pyremoteplay.__version__ import VERSION
    from pyremoteplay.receiver import QueueReceiver

    return TYPE_PS5, RPDevice, get_login_url, Profiles, VERSION, QueueReceiver


def _set_profiles(root: Path):
    _, _, _, Profiles, _, _ = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    return Profiles


def cmd_check(root: Path, _args: argparse.Namespace) -> int:
    try:
        TYPE_PS5, _RPDevice, _get_login_url, Profiles, version, _QueueReceiver = _imports()
        Profiles.set_default_path(str(_profile_path(root)))
        return _print(
            {
                "ok": True,
                "type": "check",
                "backend": "pyremoteplay",
                "version": version,
                "host_type": TYPE_PS5,
                "profile_path": str(_profile_path(root)),
                "python": sys.executable,
            }
        )
    except Exception as exc:
        return _print(
            {
                "ok": False,
                "type": "check",
                "error": str(exc),
                "python": sys.executable,
                "trace": traceback.format_exc(limit=2),
            },
            2,
        )


def cmd_discover(root: Path, args: argparse.Namespace) -> int:
    TYPE_PS5, RPDevice, _get_login_url, Profiles, _version, _QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    devices = RPDevice.search()
    out = []
    for device in devices:
        status = device.status or {}
        if status.get("host-type") != TYPE_PS5:
            continue
        out.append(
            {
                "ip": device.ip_address or status.get("host-ip") or device.host,
                "name": device.host_name or status.get("host-name") or "PS5",
                "host_id": device.mac_address or status.get("host-id") or "",
                "host_type": status.get("host-type") or TYPE_PS5,
                "status_code": status.get("status-code"),
                "running_app": status.get("running-app-name") or "",
            }
        )
    return _print({"ok": True, "type": "discover", "devices": out, "count": len(out)})


def cmd_status(root: Path, args: argparse.Namespace) -> int:
    _TYPE_PS5, RPDevice, _get_login_url, Profiles, _version, _QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    device = RPDevice(args.host)
    status = device.get_status()
    if not status:
        return _print({"ok": False, "type": "status", "error": "host_unreachable", "host": args.host}, 3)
    return _print({"ok": True, "type": "status", "host": args.host, "status": status})


def cmd_oauth_url(root: Path, _args: argparse.Namespace) -> int:
    _TYPE_PS5, _RPDevice, get_login_url, Profiles, _version, _QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    return _print({"ok": True, "type": "oauth_url", "url": get_login_url()})


def cmd_add_profile(root: Path, args: argparse.Namespace) -> int:
    _TYPE_PS5, _RPDevice, _get_login_url, Profiles, _version, _QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    profiles = Profiles.load()
    profile = profiles.new_user(args.redirect_url, save=True)
    if not profile:
        return _print({"ok": False, "type": "add_profile", "error": "profile_create_failed"}, 4)
    return _print(
        {
            "ok": True,
            "type": "add_profile",
            "user": profile.name,
            "profile_path": str(_profile_path(root)),
        }
    )


def _registered_hosts(profile: Any) -> list[dict[str, Any]]:
    hosts = []
    for host in profile.hosts:
        hosts.append({"host_id": host.name, "type": host.type})
    return hosts


def _profile_names(profiles: Any) -> list[str]:
    try:
        return list(profiles.usernames)
    except Exception:
        return []


def cmd_profiles(root: Path, _args: argparse.Namespace) -> int:
    _TYPE_PS5, _RPDevice, _get_login_url, Profiles, _version, _QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    profiles = Profiles.load()
    users = []
    for profile in profiles.users:
        users.append({"user": profile.name, "hosts": _registered_hosts(profile)})
    return _print({"ok": True, "type": "profiles", "profiles": users, "count": len(users)})


def cmd_register(root: Path, args: argparse.Namespace) -> int:
    _TYPE_PS5, RPDevice, _get_login_url, Profiles, _version, _QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    if not args.pin.isdigit() or len(args.pin) != 8:
        return _print({"ok": False, "type": "register", "error": "pin_must_be_8_digits"}, 5)
    profiles = Profiles.load()
    if args.user not in _profile_names(profiles):
        return _print(
            {
                "ok": False,
                "type": "register",
                "error": "no_saved_profile",
                "user": args.user,
                "saved_users": _profile_names(profiles),
                "profile_path": str(_profile_path(root)),
            },
            4,
        )
    device = RPDevice(args.host)
    status = device.get_status()
    if not status:
        return _print({"ok": False, "type": "register", "error": "host_unreachable", "host": args.host}, 3)
    profile = device.register(args.user, args.pin, timeout=args.timeout, profiles=profiles, save=True)
    if not profile:
        return _print(
            {
                "ok": False,
                "type": "register",
                "error": "register_failed_check_pin_console",
                "host": args.host,
                "user": args.user,
                "status_code": status.get("status-code"),
            },
            6,
        )
    return _print(
        {
            "ok": True,
            "type": "register",
            "host": args.host,
            "user": profile.name,
            "profile_path": str(_profile_path(root)),
        }
    )


def cmd_test_session(root: Path, args: argparse.Namespace) -> int:
    _TYPE_PS5, RPDevice, _get_login_url, Profiles, _version, QueueReceiver = _imports()
    Profiles.set_default_path(str(_profile_path(root)))
    profiles = Profiles.load()
    if args.user not in _profile_names(profiles):
        return _print(
            {
                "ok": False,
                "type": "test_session",
                "error": "no_saved_profile",
                "user": args.user,
                "saved_users": _profile_names(profiles),
                "profile_path": str(_profile_path(root)),
            },
            4,
        )
    receiver = QueueReceiver(max_video_frames=2, max_audio_frames=1)
    device = RPDevice(args.host)
    status = device.get_status()
    if not status:
        return _print({"ok": False, "type": "test_session", "error": "host_unreachable", "host": args.host}, 3)

    registered_users = device.get_users(profiles)
    if args.user not in registered_users:
        return _print(
            {
                "ok": False,
                "type": "test_session",
                "error": "user_not_registered_with_console",
                "host": args.host,
                "user": args.user,
                "registered_users": registered_users,
                "host_id": status.get("host-id", ""),
                "host_name": status.get("host-name", ""),
                "status_code": status.get("status-code"),
            },
            7,
        )
    session = device.create_session(
        args.user,
        profiles=profiles,
        receiver=receiver,
        resolution=args.resolution,
        fps=args.fps,
        codec=args.codec,
        quality="default",
        hdr=False,
    )
    if not session:
        return _print(
            {
                "ok": False,
                "type": "test_session",
                "error": "session_create_failed",
                "host": args.host,
                "user": args.user,
                "status_code": status.get("status-code"),
                "host_name": status.get("host-name", ""),
            },
            7,
        )

    import asyncio

    async def run() -> dict[str, Any]:
        ok = await device.connect()
        if not ok:
            return {"ok": False, "error": session.error or "connect_failed"}
        ready = await device.async_wait_for_session(timeout=args.timeout)
        frame_count = 0
        if ready:
            start = asyncio.get_running_loop().time()
            while asyncio.get_running_loop().time() - start < 2.0:
                await asyncio.sleep(0.05)
                if receiver.get_latest_video_frame() is not None:
                    frame_count += 1
                    break
        device.disconnect()
        return {"ok": bool(ready and frame_count > 0), "ready": bool(ready), "frames_seen": frame_count}

    result = asyncio.run(run())
    result.update({"type": "test_session", "host": args.host, "user": args.user})
    return _print(result, 0 if result.get("ok") else 8)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Orion PS5 Remote Play helper")
    parser.add_argument("--root", default="", help="NexusVision root")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check")
    sub.add_parser("discover")

    p_status = sub.add_parser("status")
    p_status.add_argument("--host", required=True)

    sub.add_parser("oauth-url")

    p_add = sub.add_parser("add-profile")
    p_add.add_argument("--redirect-url", required=True)

    sub.add_parser("profiles")

    p_register = sub.add_parser("register")
    p_register.add_argument("--host", required=True)
    p_register.add_argument("--user", required=True)
    p_register.add_argument("--pin", required=True)
    p_register.add_argument("--timeout", type=float, default=8.0)

    p_test = sub.add_parser("test-session")
    p_test.add_argument("--host", required=True)
    p_test.add_argument("--user", required=True)
    p_test.add_argument("--resolution", default="720p")
    p_test.add_argument("--fps", default="high")
    p_test.add_argument("--codec", default="h264")
    p_test.add_argument("--timeout", type=float, default=15.0)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = _root_from_args(args.root)
    _bootstrap(root)

    commands = {
        "check": cmd_check,
        "discover": cmd_discover,
        "status": cmd_status,
        "oauth-url": cmd_oauth_url,
        "add-profile": cmd_add_profile,
        "profiles": cmd_profiles,
        "register": cmd_register,
        "test-session": cmd_test_session,
    }
    try:
        return commands[args.cmd](root, args)
    except Exception as exc:
        return _print(
            {
                "ok": False,
                "type": args.cmd,
                "error": str(exc),
                "trace": traceback.format_exc(limit=4),
            },
            1,
        )


if __name__ == "__main__":
    raise SystemExit(main())
