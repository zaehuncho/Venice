#!/usr/bin/env python3
"""Orion owner/staff admin CLI.

A local, dependency-free (stdlib only) command-line tool for operating the Orion
license backend. It talks to the same HTTPS API the launcher uses
(https://api.zaeorion.com by default) and is intentionally kept *out* of the
customer gameplay UI.

Security model
--------------
* No admin secret or token is ever hardcoded. Credentials come from (in order):
  the environment (ORION_ADMIN_SECRET / ORION_ADMIN_TOKEN / ORION_ADMIN_DISCORD_ID),
  or an ignored local config at ~/.orion/admin_config.json (written 0600 by
  ``login``). The config lives outside the repo and is never committed.
* Secrets are never printed or logged. ``config`` redacts them.
* Full license keys are shown only when a key is first created (``license create``)
  or explicitly requested with ``--reveal``. Everywhere else keys are masked.
* Destructive actions (revoke, reset-hwid, deactivate, staff disable) require a
  non-empty ``--reason`` and an interactive confirmation (skippable with ``--yes``
  for scripted use, but ``--reason`` is still mandatory).
* Only HTTPS endpoints are allowed.

Roles (server-enforced; the CLI gates are advisory):
  owner   : everything, including staff management
  admin   : license create / revoke / reset / extend / plan
  support : lookup + limited HWID reset / deactivate

Staff-management, search, whoami, and audit endpoints are part of the planned
staff backend. When they are not yet deployed the CLI degrades gracefully with a
clear message instead of failing hard. See docs/ADMIN_TOOL.md for the contract.
"""

from __future__ import annotations

import argparse
import dataclasses
import getpass
import hashlib
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

DEFAULT_BASE_URL = "https://api.zaeorion.com"
CONFIG_PATH = Path.home() / ".orion" / "admin_config.json"
DESTRUCTIVE_COMMANDS = {"revoke", "reset-hwid", "deactivate", "staff-disable", "delete", "killswitch-engage"}
ROLE_CAPABILITIES = {
    "owner": {"*"},
    "admin": {"create", "revoke", "unrevoke", "reset-hwid", "deactivate", "extend", "set-plan", "lookup", "search", "audit"},
    "support": {"lookup", "search", "reset-hwid", "deactivate"},
}


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested without any network)
# --------------------------------------------------------------------------- #

def mask_key(key: str) -> str:
    """Render a license key with only its last 4 characters visible."""
    if not key:
        return "(none)"
    key = key.strip()
    if len(key) <= 4:
        return "****"
    return "ORION-…-" + key[-4:]


def compute_extend_expiry(current_expiry: int, now: int, add_days: int) -> int:
    """New expiry when extending by add_days. Extends from the later of now or the
    current expiry, so extending a not-yet-expired key never shortens it."""
    base = max(int(current_expiry or 0), int(now))
    return base + int(add_days) * 86400


def redact_config(config: "AdminConfig") -> dict:
    """A dict view of the config safe to print: secrets reduced to presence flags."""
    return {
        "base_url": config.base_url,
        "role": config.role or "(unknown)",
        "discord_user_id": config.discord_user_id or "(none)",
        "admin_secret": "set" if config.admin_secret else "unset",
        "staff_token": "set" if config.staff_token else "unset",
    }


def is_destructive(command: str) -> bool:
    return command in DESTRUCTIVE_COMMANDS


def role_can(role: Optional[str], capability: str) -> bool:
    """Advisory client-side gate. The server is the source of truth."""
    if not role:
        return True  # unknown role: let the server decide
    caps = ROLE_CAPABILITIES.get(role, set())
    return "*" in caps or capability in caps


def build_auth_headers(config: "AdminConfig") -> dict:
    """Auth headers for an admin request. Owner uses the admin secret; staff uses a
    bearer token plus their Discord identity."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.admin_secret:
        headers["X-Orion-Admin-Secret"] = config.admin_secret
    if config.staff_token:
        headers["Authorization"] = "Bearer " + config.staff_token
    if config.discord_user_id:
        headers["X-Orion-Discord-Id"] = config.discord_user_id
    return headers


def format_audit_row(event: dict) -> str:
    """One audit line, key suffix only, never a full key."""
    ts = event.get("timestamp") or event.get("time") or ""
    if isinstance(ts, (int, float)):
        ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(int(ts)))
    actor = event.get("actor_discord_user_id") or event.get("actor_discord_id") or event.get("actor_staff_id") or event.get("actor") or "?"
    action = event.get("action") or "?"
    suffix = event.get("key_suffix") or event.get("target_id")
    if not suffix and event.get("license_key"):
        suffix = mask_key(event["license_key"])
    target = event.get("target_email") or event.get("target_discord") or event.get("target_type") or ""
    reason = event.get("reason") or ""
    return f"{ts}  {actor:<20}  {action:<16}  {suffix or '-':<14}  {target:<28}  {reason}"


def local_machine_id() -> str:
    """Stable local machine marker for staff-tool binding.

    This is separate from the gameplay launcher HWID. It binds the owner/staff
    admin tool to one Windows install without exposing raw host identifiers.
    """
    material = "|".join([
        platform.node(),
        os.environ.get("COMPUTERNAME", ""),
        os.environ.get("USERNAME", ""),
        str(uuid.getnode()),
    ])
    return "stafftool-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def request_freshness_fields() -> dict:
    return {
        "request_nonce": uuid.uuid4().hex,
        "request_timestamp": int(time.time()),
    }


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class AdminConfig:
    base_url: str = DEFAULT_BASE_URL
    admin_secret: str = ""
    staff_token: str = ""
    discord_user_id: str = ""
    role: str = ""

    @property
    def has_auth(self) -> bool:
        return bool(self.admin_secret or self.staff_token)


def load_config(path: Path = CONFIG_PATH, env: Optional[dict] = None) -> AdminConfig:
    """Load config from the ignored file, then layer environment overrides on top."""
    env = os.environ if env is None else env
    config = AdminConfig()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            config.base_url = data.get("base_url", config.base_url) or config.base_url
            config.admin_secret = data.get("admin_secret", "") or ""
            config.staff_token = data.get("staff_token", "") or ""
            config.discord_user_id = data.get("discord_user_id", "") or ""
            config.role = data.get("role", "") or ""
        except (json.JSONDecodeError, OSError):
            pass
    config.base_url = env.get("ORION_API_BASE", config.base_url) or config.base_url
    config.admin_secret = env.get("ORION_ADMIN_SECRET", config.admin_secret) or config.admin_secret
    config.staff_token = env.get("ORION_ADMIN_TOKEN", config.staff_token) or config.staff_token
    config.discord_user_id = env.get("ORION_ADMIN_DISCORD_ID", config.discord_user_id) or config.discord_user_id
    config.role = env.get("ORION_ADMIN_ROLE", config.role) or config.role
    return config


def save_config(config: AdminConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": config.base_url,
        "admin_secret": config.admin_secret,
        "staff_token": config.staff_token,
        "discord_user_id": config.discord_user_id,
        "role": config.role,
    }
    # Write then tighten perms (best effort; harmless on Windows).
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class ApiResult:
    ok: bool
    status: int
    data: dict
    error: str = ""


class Transport:
    """Injectable HTTP transport so command logic is testable without a network."""

    def request(self, method: str, url: str, headers: dict, body: Optional[dict]) -> ApiResult:  # pragma: no cover
        raise NotImplementedError


class UrllibTransport(Transport):
    def __init__(self, timeout: float = 15.0) -> None:
        self.timeout = timeout

    def request(self, method: str, url: str, headers: dict, body: Optional[dict]) -> ApiResult:
        if not url.lower().startswith("https://"):
            return ApiResult(False, 0, {}, "refusing non-HTTPS endpoint")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                parsed = json.loads(raw) if raw.strip() else {}
                return ApiResult(True, resp.status, parsed if isinstance(parsed, dict) else {"data": parsed})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            try:
                parsed = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                parsed = {}
            msg = parsed.get("message") or parsed.get("error") or f"HTTP {exc.code}"
            return ApiResult(False, exc.code, parsed if isinstance(parsed, dict) else {}, msg)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return ApiResult(False, 0, {}, str(exc))


class OrionAdminClient:
    def __init__(self, config: AdminConfig, transport: Transport) -> None:
        self.config = config
        self.transport = transport

    def _url(self, path: str, query: Optional[dict] = None) -> str:
        url = self.config.base_url.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v})
        return url

    def get_version(self) -> ApiResult:
        return self.transport.request("GET", self._url("/api/version"), {"Accept": "application/json"}, None)

    def provision(self, body: dict) -> ApiResult:
        return self.transport.request("POST", self._url("/api/provision"), build_auth_headers(self.config), body)

    def get_license(self, key: str) -> ApiResult:
        path = "/api/staff/license" if self.config.staff_token and not self.config.admin_secret else "/api/admin/license"
        return self.transport.request("GET", self._url(path, {"key": key}),
                                      build_auth_headers(self.config), None)

    def update_license(self, body: dict) -> ApiResult:
        path = "/api/staff/license" if self.config.staff_token and not self.config.admin_secret else "/api/admin/license"
        return self.transport.request("POST", self._url(path), build_auth_headers(self.config), body)

    def search(self, by: str, value: str) -> ApiResult:
        return self.transport.request("GET", self._url("/api/admin/search", {by: value}),
                                      build_auth_headers(self.config), None)

    def whoami(self) -> ApiResult:
        path = "/api/staff/whoami" if self.config.staff_token and not self.config.admin_secret else "/api/admin/whoami"
        return self.transport.request("GET", self._url(path), build_auth_headers(self.config), None)

    def staff(self, method: str, body: Optional[dict] = None) -> ApiResult:
        return self.transport.request(method, self._url("/api/admin/staff"), build_auth_headers(self.config), body)

    def audit(self, limit: int) -> ApiResult:
        return self.transport.request("GET", self._url("/api/admin/staff/audit", {"limit": str(limit)}),
                                      build_auth_headers(self.config), None)

    def kill(self, body: dict) -> ApiResult:
        return self.transport.request("POST", self._url("/api/admin/kill"), build_auth_headers(self.config), body)

    def unkill(self, body: dict) -> ApiResult:
        return self.transport.request("POST", self._url("/api/admin/unkill"), build_auth_headers(self.config), body)

    def kill_status(self) -> ApiResult:
        return self.transport.request("GET", self._url("/api/admin/status"), build_auth_headers(self.config), None)

    def staff_enroll(self, discord_id: str, enrollment_key: str, display_name: str = "") -> ApiResult:
        body = {
            "discord_user_id": discord_id,
            "enrollment_key": enrollment_key,
            "machine_id": local_machine_id(),
            **request_freshness_fields(),
        }
        if display_name:
            body["display_name"] = display_name
        return self.transport.request("POST", self._url("/api/staff/enroll"),
                                      {"Content-Type": "application/json", "Accept": "application/json"}, body)

    def staff_login(self, discord_id: str) -> ApiResult:
        body = {
            "discord_user_id": discord_id,
            "machine_id": local_machine_id(),
            **request_freshness_fields(),
        }
        return self.transport.request("POST", self._url("/api/staff/login"),
                                      {"Content-Type": "application/json", "Accept": "application/json"}, body)


# --------------------------------------------------------------------------- #
# Command handlers (take an injected client + io so they are testable)
# --------------------------------------------------------------------------- #

class CommandError(Exception):
    """Raised for user-facing failures (bad args, refused confirmation, API errors)."""


def _require_reason(args) -> str:
    reason = (getattr(args, "reason", "") or "").strip()
    if not reason:
        raise CommandError("This action is destructive and requires --reason \"...\".")
    return reason


def _confirm_destructive(args, summary: str, confirmer: Callable[[str], bool]) -> None:
    if getattr(args, "yes", False):
        return
    if not confirmer(f"Confirm {summary}? Type 'yes' to proceed: "):
        raise CommandError("Aborted by user.")


def _unwrap(result: ApiResult, not_deployed_hint: str = "") -> dict:
    if result.ok:
        return result.data
    if result.status == 404 and not_deployed_hint:
        raise CommandError(not_deployed_hint)
    raise CommandError(result.error or f"request failed (status {result.status})")


def cmd_version(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.get_version())
    out(f"backend: {data.get('api', '?')} v{data.get('version', '?')} env={data.get('env', '?')}")
    return data


def cmd_whoami(client: OrionAdminClient, args, out, confirmer) -> dict:
    result = client.whoami()
    if result.ok:
        out(f"role: {result.data.get('role', '?')}  discord: {result.data.get('discord_user_id', '?')}")
        return result.data
    # Degrade: report what the local config believes.
    out(f"(server whoami unavailable: {result.error or result.status}) local role: {client.config.role or 'unknown'}")
    return {"role": client.config.role}


def cmd_license_create(client: OrionAdminClient, args, out, confirmer) -> dict:
    if not args.email:
        raise CommandError("--email is required to create a license.")
    body = {"email": args.email, "plan": args.plan, "days": args.days}
    if args.discord:
        body["discord_user_id"] = args.discord
    if args.key:
        body["license_key"] = args.key
    if client.config.discord_user_id:
        body["actor_discord_id"] = client.config.discord_user_id
    data = _unwrap(client.provision(body))
    # The full key is shown exactly once, here, at creation time.
    out("License created (store the key now; it will not be shown in full again):")
    out(f"  key:    {data.get('license_key', '(missing)')}")
    out(f"  email:  {data.get('email', args.email)}")
    out(f"  plan:   {data.get('plan', args.plan)}")
    if data.get("expiry_date"):
        out(f"  expiry: {time.strftime('%Y-%m-%d', time.gmtime(int(data['expiry_date'])))}")
    return data


def _print_license(out, lic: dict, reveal: bool) -> None:
    key = lic.get("license_key", "")
    out(f"  key:     {key if reveal else mask_key(key)}")
    out(f"  email:   {lic.get('email', '?')}")
    out(f"  plan:    {lic.get('plan', '?')}")
    out(f"  status:  {lic.get('status', '?')}  revoked={lic.get('revoked', '?')}")
    machine = lic.get("machine_id") or ""
    out(f"  machine: {'(unbound)' if not machine else machine[-8:]}")
    if lic.get("expiry"):
        out(f"  expiry:  {time.strftime('%Y-%m-%d', time.gmtime(int(lic['expiry'])))}")
    if lic.get("discord_user_id"):
        out(f"  discord: {lic['discord_user_id']}")


def cmd_license_lookup(client: OrionAdminClient, args, out, confirmer) -> dict:
    if args.key:
        data = _unwrap(client.get_license(args.key))
        lic = data.get("license", data)
        out("License:")
        _print_license(out, lic, args.reveal)
        return data
    if args.email or args.discord:
        by, value = ("email", args.email) if args.email else ("discord", args.discord)
        data = _unwrap(client.search(by, value),
                       not_deployed_hint="Search by email/discord needs the staff backend (not deployed yet). Look up by --key for now.")
        licenses = data.get("licenses", [])
        out(f"{len(licenses)} match(es):")
        for lic in licenses:
            _print_license(out, lic, args.reveal)
            out("  -")
        return data
    raise CommandError("Provide --key, --email, or --discord to look up a license.")


def cmd_license_revoke(client: OrionAdminClient, args, out, confirmer, revoke: bool) -> dict:
    if not args.key:
        raise CommandError("--key is required.")
    if revoke:
        reason = _require_reason(args)
        _confirm_destructive(args, f"revoke {mask_key(args.key)} ({reason})", confirmer)
    body = {"license_key": args.key, "action": "revoke" if revoke else "unrevoke"}
    if revoke:
        body["reason"] = args.reason
    if client.config.discord_user_id:
        body["actor_discord_id"] = client.config.discord_user_id
    data = _unwrap(client.update_license(body))
    out(f"{'Revoked' if revoke else 'Unrevoked'} {mask_key(args.key)}.")
    return data


def cmd_license_reset_hwid(client: OrionAdminClient, args, out, confirmer) -> dict:
    if not args.key:
        raise CommandError("--key is required.")
    reason = _require_reason(args)
    _confirm_destructive(args, f"reset HWID for {mask_key(args.key)} ({reason})", confirmer)
    body = {"license_key": args.key, "action": "reset_machine", "reason": reason}
    if client.config.discord_user_id:
        body["actor_discord_id"] = client.config.discord_user_id
    data = _unwrap(client.update_license(body))
    out(f"Cleared machine binding for {mask_key(args.key)}.")
    return data


def cmd_license_deactivate(client: OrionAdminClient, args, out, confirmer) -> dict:
    if not args.key:
        raise CommandError("--key is required.")
    reason = _require_reason(args)
    _confirm_destructive(args, f"deactivate machine binding for {mask_key(args.key)} ({reason})", confirmer)
    body = {"license_key": args.key, "action": "deactivate", "machine_id": "", "reason": reason}
    if client.config.discord_user_id:
        body["actor_discord_id"] = client.config.discord_user_id
    data = _unwrap(client.update_license(body))
    out(f"Deactivated machine binding for {mask_key(args.key)}.")
    return data


def cmd_license_extend(client: OrionAdminClient, args, out, confirmer) -> dict:
    if not args.key:
        raise CommandError("--key is required.")
    if args.expiry is None and args.days is None:
        raise CommandError("Provide --days N (extend) or --expiry <unix-ts> (absolute).")
    if args.expiry is not None:
        new_expiry = int(args.expiry)
    else:
        current = _unwrap(client.get_license(args.key)).get("license", {})
        new_expiry = compute_extend_expiry(int(current.get("expiry", 0) or 0), int(time.time()), int(args.days))
    body = {"license_key": args.key, "action": "extend_expiry", "expiry": new_expiry}
    if client.config.discord_user_id:
        body["actor_discord_id"] = client.config.discord_user_id
    _unwrap(client.update_license(body))
    out(f"Extended {mask_key(args.key)} to {time.strftime('%Y-%m-%d', time.gmtime(new_expiry))}.")
    return {"expiry": new_expiry}


def cmd_license_set_plan(client: OrionAdminClient, args, out, confirmer) -> dict:
    if not args.key or not args.plan:
        raise CommandError("--key and --plan are required.")
    body = {"license_key": args.key, "action": "change_plan", "plan": args.plan}
    if client.config.discord_user_id:
        body["actor_discord_id"] = client.config.discord_user_id
    _unwrap(client.update_license(body))
    out(f"Set plan for {mask_key(args.key)} to {args.plan}.")
    return {"plan": args.plan}


def cmd_staff(client: OrionAdminClient, args, out, confirmer) -> dict:
    hint = "Staff management endpoint is unavailable on this backend."
    if args.staff_command == "list":
        data = _unwrap(client.staff("GET"), not_deployed_hint=hint)
        for member in data.get("staff", []):
            status = "enabled" if member.get("enabled", False) else "disabled"
            out(f"  {member.get('staff_id', '?'):<18} {member.get('discord_user_id', '?'):<22} {member.get('role', '?'):<10} {status}")
        return data
    if args.staff_command == "create":
        if not args.discord or not args.role:
            raise CommandError("--discord and --role are required to create staff.")
        body = {
            "action": "create",
            "discord_user_id": args.discord,
            "display_name": args.name or args.discord,
            "role": args.role,
            "reason": args.reason or "staff create",
        }
        if client.config.discord_user_id:
            body["actor_discord_id"] = client.config.discord_user_id
        data = _unwrap(client.staff("POST", body), not_deployed_hint=hint)
        out(f"Created staff {args.discord} ({args.role}).")
        if data.get("enrollment_key"):
            out("Enrollment key (shown once; give it only to that staff member):")
            out(f"  {data['enrollment_key']}")
        return data
    if args.staff_command == "reissue-enrollment":
        if not args.discord and not args.staff_id:
            raise CommandError("--discord or --staff-id is required.")
        reason = _require_reason(args)
        _confirm_destructive(args, f"reissue staff enrollment ({reason})", confirmer)
        body = {"action": "reissue_enrollment", "reason": reason}
        if args.discord:
            body["discord_user_id"] = args.discord
        if args.staff_id:
            body["staff_id"] = args.staff_id
        data = _unwrap(client.staff("POST", body), not_deployed_hint=hint)
        out("Enrollment key reissued (shown once):")
        out(f"  {data.get('enrollment_key', '(missing)')}")
        return data
    if args.staff_command == "reset-machine":
        if not args.discord and not args.staff_id:
            raise CommandError("--discord or --staff-id is required.")
        reason = _require_reason(args)
        _confirm_destructive(args, f"reset staff machine binding ({reason})", confirmer)
        body = {"action": "reset_machine", "reason": reason}
        if args.discord:
            body["discord_user_id"] = args.discord
        if args.staff_id:
            body["staff_id"] = args.staff_id
        data = _unwrap(client.staff("POST", body), not_deployed_hint=hint)
        out("Staff machine binding reset.")
        return data
    if args.staff_command == "disable":
        if not args.discord:
            raise CommandError("--discord is required.")
        reason = _require_reason(args)
        _confirm_destructive(args, f"disable staff {args.discord} ({reason})", confirmer)
        body = {"action": "disable", "discord_user_id": args.discord, "reason": reason}
        if client.config.discord_user_id:
            body["actor_discord_id"] = client.config.discord_user_id
        data = _unwrap(client.staff("POST", body), not_deployed_hint=hint)
        out(f"Disabled staff {args.discord}.")
        return data
    raise CommandError("Unknown staff command.")


def cmd_killswitch(client: OrionAdminClient, args, out, confirmer) -> dict:
    if args.killswitch_command == "status":
        data = _unwrap(client.kill_status())
        state = "ENGAGED" if data.get("global_kill") else "released"
        out(f"killswitch: {state}" + (f"  reason={data['kill_reason']}" if data.get("kill_reason") else ""))
        return data
    if args.killswitch_command == "engage":
        reason = _require_reason(args)
        _confirm_destructive(args, f"engage the GLOBAL killswitch ({reason}) — this disables the live service", confirmer)
        data = _unwrap(client.kill({"target_type": "global", "reason": reason}))
        out(f"Killswitch engaged. reason={reason}")
        return data
    if args.killswitch_command == "release":
        data = _unwrap(client.unkill({"target_type": "global"}))
        out("Killswitch released.")
        return data
    raise CommandError("Unknown killswitch command.")


def cmd_staff_enroll(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.staff_enroll(args.discord, args.enrollment_key, args.name))
    token = data.get("token", "")
    if not token:
        raise CommandError("Enrollment succeeded but backend did not return a staff token.")
    config = load_config()
    if args.base_url:
        config.base_url = args.base_url
    staff = data.get("staff", {})
    config.admin_secret = ""
    config.staff_token = token
    config.discord_user_id = args.discord
    config.role = staff.get("role", "support")
    save_config(config)
    out(f"Staff enrolled and credentials saved to {CONFIG_PATH}. Token not displayed.")
    return data


def cmd_staff_login(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.staff_login(args.discord))
    token = data.get("token", "")
    if not token:
        raise CommandError("Login succeeded but backend did not return a staff token.")
    config = load_config()
    if args.base_url:
        config.base_url = args.base_url
    staff = data.get("staff", {})
    config.admin_secret = ""
    config.staff_token = token
    config.discord_user_id = args.discord
    config.role = staff.get("role", "support")
    save_config(config)
    out(f"Staff login saved to {CONFIG_PATH}. Token not displayed.")
    return data


def cmd_audit(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.audit(args.limit),
                   not_deployed_hint="Audit log needs the staff backend (not deployed yet).")
    events = data.get("events", [])
    out(f"{len(events)} audit event(s) (newest first):")
    for event in events:
        out("  " + format_audit_row(event))
    return data


# --------------------------------------------------------------------------- #
# Parser + dispatch
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orion-admin", description="Orion owner/staff admin CLI.")
    parser.add_argument("--base-url", default=None, help="Override the API base URL (HTTPS).")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation (reason still required).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="Show backend version.")
    sub.add_parser("whoami", help="Show the authenticated identity/role.")

    login = sub.add_parser("login", help="Store admin/staff credentials locally (prompted, never echoed).")
    login.add_argument("--mode", choices=["owner", "staff"], default="owner")
    login.add_argument("--discord", default="", help="Discord user id (required for staff).")
    login.add_argument("--role", default="", help="Local role label for advisory gating.")
    sub.add_parser("logout", help="Remove the local credential file.")
    sub.add_parser("config", help="Show the (redacted) effective config.")

    p = sub.add_parser("staff-enroll", help="Enroll this machine with a one-time staff key.")
    p.add_argument("--discord", required=True, help="Discord user id registered by the owner.")
    p.add_argument("--enrollment-key", required=True, help="One-time enrollment key from the owner.")
    p.add_argument("--name", default="", help="Optional local display name.")

    p = sub.add_parser("staff-login", help="Refresh a staff bearer token for this machine.")
    p.add_argument("--discord", required=True, help="Discord user id registered by the owner.")

    lic = sub.add_parser("license", help="License operations.")
    lic_sub = lic.add_subparsers(dest="license_command", required=True)

    p = lic_sub.add_parser("create", help="Provision a new license (key shown once).")
    p.add_argument("--email", required=True)
    p.add_argument("--plan", default="standard")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--discord", default="")
    p.add_argument("--key", default="", help="Optional custom key; auto-generated if omitted.")

    p = lic_sub.add_parser("lookup", help="Look up a license by key/email/discord.")
    p.add_argument("--key", default="")
    p.add_argument("--email", default="")
    p.add_argument("--discord", default="")
    p.add_argument("--reveal", action="store_true", help="Show the full key (off by default).")

    for name, help_text in (("revoke", "Revoke a license."), ("unrevoke", "Un-revoke a license.")):
        p = lic_sub.add_parser(name, help=help_text)
        p.add_argument("--key", required=True)
        p.add_argument("--reason", default="")

    p = lic_sub.add_parser("reset-hwid", help="Clear the machine binding (HWID reset).")
    p.add_argument("--key", required=True)
    p.add_argument("--reason", default="")

    p = lic_sub.add_parser("deactivate", help="Deactivate the current machine binding.")
    p.add_argument("--key", required=True)
    p.add_argument("--reason", default="")

    p = lic_sub.add_parser("extend", help="Extend or set expiry.")
    p.add_argument("--key", required=True)
    p.add_argument("--days", type=int, default=None, help="Add N days from the later of now/current expiry.")
    p.add_argument("--expiry", type=int, default=None, help="Absolute expiry as a unix timestamp.")

    p = lic_sub.add_parser("set-plan", help="Change the plan tier.")
    p.add_argument("--key", required=True)
    p.add_argument("--plan", required=True)

    staff = sub.add_parser("staff", help="Owner-only staff management.")
    staff_sub = staff.add_subparsers(dest="staff_command", required=True)
    staff_sub.add_parser("list", help="List staff.")
    p = staff_sub.add_parser("create", help="Create a staff member.")
    p.add_argument("--discord", required=True)
    p.add_argument("--name", default="", help="Staff display name.")
    p.add_argument("--role", required=True, choices=["owner", "admin", "support"])
    p.add_argument("--reason", default="staff create")
    p = staff_sub.add_parser("disable", help="Disable a staff member.")
    p.add_argument("--discord", required=True)
    p.add_argument("--reason", default="")
    p = staff_sub.add_parser("reset-machine", help="Clear a staff member's tool machine binding.")
    p.add_argument("--discord", default="")
    p.add_argument("--staff-id", default="")
    p.add_argument("--reason", default="")
    p = staff_sub.add_parser("reissue-enrollment", help="Issue a new one-time enrollment key.")
    p.add_argument("--discord", default="")
    p.add_argument("--staff-id", default="")
    p.add_argument("--reason", default="")

    kill = sub.add_parser("killswitch", help="Owner-only global killswitch (disables the live service).")
    kill_sub = kill.add_subparsers(dest="killswitch_command", required=True)
    kill_sub.add_parser("status", help="Show whether the global killswitch is engaged.")
    p = kill_sub.add_parser("engage", help="Engage the global killswitch (disables new activations + running sessions).")
    p.add_argument("--reason", default="")
    kill_sub.add_parser("release", help="Release the global killswitch.")

    p = sub.add_parser("audit", help="Show recent admin/audit events.")
    p.add_argument("--limit", type=int, default=50)

    return parser


def _handle_login(args, out, prompter: Callable[[str], str], path: Path = CONFIG_PATH) -> int:
    config = load_config(path)
    if args.base_url:
        config.base_url = args.base_url
    if args.mode == "owner":
        secret = prompter("Admin secret: ")
        if not secret:
            out("No secret entered; nothing saved.")
            return 1
        config.admin_secret = secret.strip()
        config.role = args.role or "owner"
    else:
        if not args.discord:
            out("Staff login requires --discord <id>.")
            return 1
        token = prompter("Staff token: ")
        if not token:
            out("No token entered; nothing saved.")
            return 1
        config.staff_token = token.strip()
        config.discord_user_id = args.discord
        config.role = args.role or "support"
    save_config(config, path)
    out(f"Credentials saved to {path} (role={config.role}). Secret not displayed.")
    return 0


def run(args, client: OrionAdminClient, out: Callable[[str], None], confirmer: Callable[[str], bool]) -> int:
    """Dispatch a parsed command. Returns a process exit code."""
    try:
        if args.command == "version":
            cmd_version(client, args, out, confirmer)
        elif args.command == "whoami":
            cmd_whoami(client, args, out, confirmer)
        elif args.command == "config":
            for key, value in redact_config(client.config).items():
                out(f"  {key}: {value}")
        elif args.command == "staff-enroll":
            cmd_staff_enroll(client, args, out, confirmer)
        elif args.command == "staff-login":
            cmd_staff_login(client, args, out, confirmer)
        elif args.command == "license":
            if not client.config.has_auth:
                raise CommandError("Not authenticated. Run 'orion-admin login' or set ORION_ADMIN_SECRET.")
            dispatch = {
                "create": cmd_license_create,
                "lookup": cmd_license_lookup,
                "reset-hwid": cmd_license_reset_hwid,
                "deactivate": cmd_license_deactivate,
                "extend": cmd_license_extend,
                "set-plan": cmd_license_set_plan,
            }
            if args.license_command in ("revoke", "unrevoke"):
                cmd_license_revoke(client, args, out, confirmer, revoke=(args.license_command == "revoke"))
            else:
                dispatch[args.license_command](client, args, out, confirmer)
        elif args.command == "staff":
            if not client.config.has_auth:
                raise CommandError("Not authenticated.")
            cmd_staff(client, args, out, confirmer)
        elif args.command == "killswitch":
            if not client.config.has_auth:
                raise CommandError("Not authenticated.")
            cmd_killswitch(client, args, out, confirmer)
        elif args.command == "audit":
            cmd_audit(client, args, out, confirmer)
        else:
            raise CommandError(f"Unknown command: {args.command}")
        return 0
    except CommandError as exc:
        out(f"error: {exc}")
        return 1


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    out = print

    if args.command == "login":
        return _handle_login(args, out, getpass.getpass)
    if args.command == "logout":
        try:
            CONFIG_PATH.unlink()
            out("Removed local credentials.")
        except FileNotFoundError:
            out("No local credentials to remove.")
        return 0

    config = load_config()
    if args.base_url:
        config.base_url = args.base_url
    client = OrionAdminClient(config, UrllibTransport())

    def interactive_confirm(prompt: str) -> bool:
        try:
            return input(prompt).strip().lower() == "yes"
        except EOFError:
            return False

    return run(args, client, out, interactive_confirm)


if __name__ == "__main__":
    raise SystemExit(main())
