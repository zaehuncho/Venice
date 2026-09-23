#!/usr/bin/env python3
"""Orion owner/staff admin CLI.

A local, dependency-free (stdlib only) command-line tool for operating the Orion
license backend. It talks to the same HTTPS API the launcher uses
(https://api.zaeorion.com by default) and is intentionally kept *out* of the
customer gameplay UI.

It is the scriptable twin of OrionOwner.exe / OrionStaff.exe and speaks the same
contract: docs/ADMIN_PANEL_V2_CONTRACT.md. Every endpoint, body field and error
code below comes from that document (§2-§6).

Security model
--------------
* No admin secret or token is ever hardcoded. Credentials come from (in order):
  the environment (ORION_ADMIN_SECRET / ORION_ADMIN_TOKEN / ORION_ADMIN_DISCORD_ID
  / ORION_ADMIN_TOTP), or an ignored local config at ~/.orion/admin_config.json
  (written 0600 by ``login``). The config lives outside the repo and is never
  committed.
* Secrets are never printed or logged. ``config`` redacts them.
* Staff requests are machine-bound: the bearer token is sent with ``X-Machine-Id``
  (``require_staff`` refuses a bound token without it) and ``X-Orion-Discord-Id``.
* Owner requests send ``X-Orion-Admin-Secret`` plus ``X-Orion-Admin-TOTP`` when a
  code is available (mandatory once ``owner_totp_required`` is on, contract §5).
* Owner step-up (rc1 RT-CRIT-01): owner-sensitive actions (TOTP enrol/confirm,
  secret rotation, config set of owner_totp_required / owner_ip_allowlist / alerts,
  releasing the kill switch, creating an owner, touching an owner staff row, or
  promoting to owner) need a FRESH single-use owner TOTP code. With an owner-role
  staff token the code is asked for (or read from ORION_ADMIN_TOTP) for that one
  request only and sent in ``X-Orion-Admin-TOTP``; it is never saved or printed.
  An owner-role ``staff-login --owner`` sends it as body ``totp_code``.
* Full license keys are shown only when a key is first created (``license create``)
  or explicitly requested with ``--reveal``. Everywhere else keys are masked.
* Every mutation requires ``--reason`` (≤200 chars); destructive ones additionally
  require an interactive confirmation (skippable with ``--yes`` for scripted use).
* Only HTTPS endpoints are allowed.

Roles are server-enforced by ``require_capability``. ``ROLE_CAPABILITIES`` here is
display-only (``orion-admin caps``): it never blocks a call, because the server is
the only gate that counts.
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
REASON_MAX_CHARS = 200

# Mirrors backend/lambda_function.py (owner rule 2026-09-15). `month` is the only
# plan sold - a free 7-day trial, then a recurring monthly subscription - and a
# HWID reset past the 3 free ones costs one day off that subscription.
SELLABLE_PLAN = "month"
RESET_FREE_DEFAULT = 3
RESET_PENALTY_DAYS = 1

# Actions that get an extra "type yes" confirmation on top of --reason.
DESTRUCTIVE_COMMANDS = {
    "revoke",
    "reset-machine",
    "freeze",
    "transfer",
    "blacklist",
    "staff-disable",
    "staff-reset-machine",
    "staff-reissue-enrollment",
    "killswitch-engage",
    "rotate-secret",
}

# Contract §1 capability matrix. DISPLAY ONLY - printed by `caps`, never a gate.
ROLE_CAPABILITIES = {
    "owner": {"*"},
    "admin": {
        "license.lookup",
        "license.create",
        "license.reset_machine",
        "license.extend",
        "license.revoke",
        "license.unrevoke",
        "license.freeze",
        "license.unfreeze",
        "license.set_plan",
        "license.transfer",
        "audit.read_own",
    },
    "support": {
        "license.lookup",
        "license.reset_machine",
        "audit.read_own",
    },
}

OWNER_ONLY_CAPABILITIES = (
    "license.reset_machine force",
    "license.set_reset_policy",
    "blacklist",
    "staff.*",
    "config.*",
    "audit.read_all",
    "metrics",
)


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


def redact_config(config: "AdminConfig") -> dict:
    """A dict view of the config safe to print: secrets reduced to presence flags."""
    return {
        "base_url": config.base_url,
        "role": config.role or "(unknown)",
        "staff_id": config.staff_id or "(none)",
        "discord_user_id": config.discord_user_id or "(none)",
        "machine_id": config.machine_id_suffix,
        "admin_secret": "set" if config.admin_secret else "unset",
        "admin_totp": "set" if config.admin_totp else "unset",
        "staff_token": "set" if config.staff_token else "unset",
    }


def is_destructive(command: str) -> bool:
    return command in DESTRUCTIVE_COMMANDS


def role_can(role: Optional[str], capability: str) -> bool:
    """Display-only view of the contract matrix. The server is the source of truth
    and this function never blocks a request."""
    if not role:
        return True  # unknown role: let the server decide
    caps = ROLE_CAPABILITIES.get(role, set())
    return "*" in caps or capability in caps


def validate_reason(reason: str) -> str:
    """Normalise a reason, or raise CommandError. Contract: required, ≤200 chars."""
    text = (reason or "").strip()
    if not text:
        raise CommandError('This action requires --reason "...".')
    if len(text) > REASON_MAX_CHARS:
        raise CommandError(f"--reason must be {REASON_MAX_CHARS} characters or fewer (got {len(text)}).")
    return text


STEP_UP_CONFIG_KEYS = ("owner_totp_required", "owner_ip_allowlist", "alerts")
STEP_UP_CONFIG_ACTIONS = ("totp_enroll", "totp_confirm", "totp_disable", "rotate_admin_secret")
TOTP_DIGITS = 6

# rc1 RT-CRIT-01: plain wording for the step-up / owner sign-in refusals.
REPLAYED_OR_WRONG = "That code was already used or is wrong \u2014 wait for the next code."
STEP_UP_MESSAGES = {
    "invalid_totp": REPLAYED_OR_WRONG,
    "totp_replayed": REPLAYED_OR_WRONG,
    "totp_required": "This needs a fresh owner TOTP code. Enter the current 6-digit code.",
    "step_up_required": "This action needs owner TOTP. Turn TOTP on with the break-glass owner secret first.",
    "step_up_unavailable": "The code check is unavailable right now. Nothing changed; try again shortly.",
    "totp_not_provisioned": "Owner TOTP is required but not set up on the server. Use the owner recovery runbook.",
    "config_unavailable": "Security settings could not be read. Nothing changed; try again shortly.",
    "audit_unavailable": "The audit log is unavailable, so nothing was changed. Try again shortly.",
}


def config_body_needs_step_up(body: dict) -> bool:
    """Mirror of the backend's owner_step_up() callers on /api/admin/config."""
    action = str(body.get("action") or "").strip().lower()
    if action in STEP_UP_CONFIG_ACTIONS:
        return True
    if any(key in body for key in STEP_UP_CONFIG_KEYS):
        return True
    if "global_kill" in body:
        value = body["global_kill"]
        enabled = bool(value.get("enabled")) if isinstance(value, dict) else bool(value)
        return not enabled  # releasing the kill; engaging stays one step
    return False


def staff_body_needs_step_up(body: dict, target_role: Optional[str]) -> bool:
    """Owner staff rows, owner creation and promotion to owner need the step-up.
    target_role None = unknown -> treated as possibly owner."""
    action = str(body.get("action") or "").strip().lower()
    wanted = str(body.get("role") or "").strip().lower()
    if action == "create":
        return wanted == "owner"
    if target_role is None or str(target_role).strip().lower() == "owner":
        return True
    return action == "set_role" and wanted == "owner"


def sanitize_code(code: str) -> str:
    return "".join(ch for ch in str(code or "") if ch.isdigit())


def build_auth_headers(config: "AdminConfig", step_up_code: str = "") -> dict:
    """Auth headers for an admin request (contract §1).

    Owner: the admin secret, plus the TOTP code when one is configured.
    Staff: a bearer token, the machine it is bound to (``X-Machine-Id`` - the
    Lambda's ``require_staff`` refuses a bound token without it), and the Discord
    identity used for the audit actor.
    step_up_code: a fresh owner code for THIS request only. Break-glass sends it in
    place of the session code; an owner-role bearer sends X-Orion-Admin-TOTP ONLY
    when it is given (never on ordinary requests).
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.admin_secret:
        headers["X-Orion-Admin-Secret"] = config.admin_secret
        code = step_up_code or config.admin_totp
        if code:
            headers["X-Orion-Admin-TOTP"] = code
    if config.staff_token:
        headers["Authorization"] = "Bearer " + config.staff_token
        headers["X-Machine-Id"] = config.machine_id
        if step_up_code and not config.admin_secret:
            headers["X-Orion-Admin-TOTP"] = step_up_code
    if config.discord_user_id:
        headers["X-Orion-Discord-Id"] = config.discord_user_id
    return headers


def format_audit_row(event: dict) -> str:
    """One audit line from a contract §4 row, key suffix only, never a full key."""
    ts = event.get("ts") or event.get("timestamp") or event.get("time") or ""
    if isinstance(ts, (int, float)):
        ts = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(int(ts)))
    actor_type = event.get("actor_type") or ""
    actor_id = (
        event.get("actor_id")
        or event.get("actor_staff_id")
        or event.get("actor_discord_user_id")
        or event.get("actor_discord_id")
        or event.get("actor")
        or "?"
    )
    actor = f"{actor_type}:{actor_id}" if actor_type else str(actor_id)
    action = event.get("action") or "?"
    target = event.get("target") or event.get("key_suffix") or event.get("target_id")
    if not target and event.get("license_key"):
        target = mask_key(event["license_key"])
    target_type = event.get("target_type") or ""
    result = event.get("result") or "ok"
    reason = event.get("reason") or ""
    return (
        f"{ts}  {actor:<26}  {action:<22}  {target_type:<8} {str(target or '-'):<14}  "
        f"{result:<10}  {reason}"
    )


def format_license(lic: dict, reveal: bool) -> list:
    """Human-readable license block (contract §2 lookup / §3 policy fields)."""
    key = lic.get("license_key") or lic.get("key") or ""
    machine = lic.get("machine_id") or ""
    lines = [
        f"  key:      {key if reveal else mask_key(key)}",
        f"  status:   {lic.get('status', '?')}  plan={lic.get('plan', '?')}",
        f"  email:    {lic.get('email', '-')}",
        f"  discord:  {lic.get('discord_user_id', '-')}",
        f"  machine:  {machine[-8:] if machine else '(unbound)'}",
    ]
    expiry = lic.get("expiry")
    if expiry is not None:
        lines.append(
            "  expiry:   lifetime" if int(expiry or 0) == 0
            else "  expiry:   " + time.strftime("%Y-%m-%d", time.gmtime(int(expiry)))
        )
    if lic.get("frozen_at"):
        lines.append("  frozen:   " + time.strftime("%Y-%m-%d", time.gmtime(int(lic["frozen_at"]))))
    lines.append(
        "  hwid:     free {used}/{free}  paid_credits={paid}  locked={locked}".format(
            used=lic.get("hwid_resets_used", 0),
            free=lic.get("hwid_free_resets", "?"),
            paid=lic.get("hwid_paid_credits", 0),
            locked=bool(lic.get("hwid_reset_locked")),
        )
    )
    flags = lic.get("flags") or {}
    if flags:
        lines.append(f"  flags:    {json.dumps(flags, sort_keys=True)}")
    for entry in (lic.get("reset_history") or [])[-5:]:
        when = entry.get("ts")
        when = time.strftime("%Y-%m-%d", time.gmtime(int(when))) if when else "?"
        lines.append(
            f"    reset {when}  mode={entry.get('mode', '?')}  by={entry.get('by', '?')}"
            f"  was=...{entry.get('machine_before_suffix', '?')}"
        )
    return lines


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
    """Contract §2 / handle_staff_enroll: replay protection on the enrol/login bodies."""
    return {
        "nonce": uuid.uuid4().hex,
        "timestamp": int(time.time()),
    }


def parse_list(text: str) -> list:
    """"a, b ,c" -> ["a", "b", "c"]; empty string -> []."""
    return [part.strip() for part in str(text or "").split(",") if part.strip()]


def parse_caps(args) -> dict:
    """Staff caps from --keys-per-day/--resets-per-day/--extend-max-days."""
    caps = {}
    if getattr(args, "keys_per_day", None) is not None:
        caps["keys_per_day"] = int(args.keys_per_day)
    if getattr(args, "resets_per_day", None) is not None:
        caps["resets_per_day"] = int(args.resets_per_day)
    if getattr(args, "extend_max_days", None) is not None:
        caps["extend_max_days"] = int(args.extend_max_days)
    return caps


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

@dataclasses.dataclass
class AdminConfig:
    base_url: str = DEFAULT_BASE_URL
    admin_secret: str = ""
    admin_totp: str = ""
    staff_token: str = ""
    staff_id: str = ""
    discord_user_id: str = ""
    role: str = ""

    @property
    def has_auth(self) -> bool:
        return bool(self.admin_secret or self.staff_token)

    @property
    def machine_id(self) -> str:
        return local_machine_id()

    @property
    def machine_id_suffix(self) -> str:
        return "..." + self.machine_id[-10:]

    @property
    def uses_owner_routes(self) -> bool:
        """Owner routes (/api/admin/*) accept the admin secret or an owner-role token."""
        return bool(self.admin_secret) or self.role == "owner"


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
            config.staff_id = data.get("staff_id", "") or ""
            config.discord_user_id = data.get("discord_user_id", "") or ""
            config.role = data.get("role", "") or ""
        except (json.JSONDecodeError, OSError):
            pass
    config.base_url = env.get("ORION_API_BASE", config.base_url) or config.base_url
    config.admin_secret = env.get("ORION_ADMIN_SECRET", config.admin_secret) or config.admin_secret
    # The TOTP code is short-lived: environment only, never written to disk.
    config.admin_totp = env.get("ORION_ADMIN_TOTP", "") or ""
    config.staff_token = env.get("ORION_ADMIN_TOKEN", config.staff_token) or config.staff_token
    config.staff_id = env.get("ORION_ADMIN_STAFF_ID", config.staff_id) or config.staff_id
    config.discord_user_id = env.get("ORION_ADMIN_DISCORD_ID", config.discord_user_id) or config.discord_user_id
    config.role = env.get("ORION_ADMIN_ROLE", config.role) or config.role
    return config


def save_config(config: AdminConfig, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": config.base_url,
        "admin_secret": config.admin_secret,
        "staff_token": config.staff_token,
        "staff_id": config.staff_id,
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
    """Thin contract-shaped wrapper. One method per endpoint in §2-§6.

    code_prompter: asks the owner for a fresh TOTP code (getpass on a tty). It is
    only called for an owner-sensitive request made with an owner-role staff token
    (and for ``staff-login --owner``); the answer is used for one request."""

    def __init__(self, config: AdminConfig, transport: Transport,
                 code_prompter: Optional[Callable[[str], str]] = None) -> None:
        self.config = config
        self.transport = transport
        self.code_prompter = code_prompter

    # -- owner step-up -----------------------------------------------------
    @property
    def bearer_owner(self) -> bool:
        """Owner routes reached with a staff token (no break-glass secret)."""
        return bool(self.config.staff_token) and not self.config.admin_secret

    def fresh_owner_code(self, why: str) -> str:
        """One fresh owner code for one request. ORION_ADMIN_TOTP (set for this one
        command) wins, else the prompter. Never stored on the config."""
        code = sanitize_code(self.config.admin_totp)
        if not code and self.code_prompter is not None:
            code = sanitize_code(self.code_prompter(f"{why}\nFresh owner TOTP code: "))
        if not code:
            raise CommandError(f"{why} Run it interactively, or set ORION_ADMIN_TOTP for this one command.")
        if len(code) != TOTP_DIGITS:
            raise CommandError("Enter the 6-digit code from the authenticator.")
        return code

    def _post_step_up(self, path: str, body: dict, why: str) -> ApiResult:
        if not self.bearer_owner:
            # Break-glass (unchanged): the session code already rides every request.
            return self._post(path, body)
        code = self.fresh_owner_code(why)
        return self.transport.request("POST", self._url(path),
                                      build_auth_headers(self.config, step_up_code=code), body)

    # -- plumbing ----------------------------------------------------------
    def _url(self, path: str, query: Optional[dict] = None) -> str:
        url = self.config.base_url.rstrip("/") + path
        if query:
            url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        return url

    def _get(self, path: str, query: Optional[dict] = None) -> ApiResult:
        return self.transport.request("GET", self._url(path, query), build_auth_headers(self.config), None)

    def _post(self, path: str, body: dict) -> ApiResult:
        return self.transport.request("POST", self._url(path), build_auth_headers(self.config), body)

    @property
    def license_path(self) -> str:
        return "/api/admin/license" if self.config.uses_owner_routes else "/api/staff/license"

    @property
    def audit_path(self) -> str:
        return "/api/admin/audit" if self.config.uses_owner_routes else "/api/staff/audit"

    # -- endpoints ---------------------------------------------------------
    def get_version(self) -> ApiResult:
        return self.transport.request("GET", self._url("/api/version"), {"Accept": "application/json"}, None)

    def whoami(self) -> ApiResult:
        path = "/api/admin/whoami" if self.config.uses_owner_routes else "/api/staff/whoami"
        return self._get(path)

    def metrics(self) -> ApiResult:
        return self._get("/api/admin/metrics")

    def license(self, body: dict) -> ApiResult:
        """POST /api/admin/license or /api/staff/license, body {action, reason, ...}."""
        return self._post(self.license_path, body)

    def license_owner(self, body: dict) -> ApiResult:
        """Owner-only license actions (blacklist, set_reset_policy)."""
        return self._post("/api/admin/license", body)

    def staff_list(self) -> ApiResult:
        return self._get("/api/admin/staff")

    def staff(self, body: dict) -> ApiResult:
        if self.bearer_owner:
            target_role = None
            if str(body.get("action") or "").lower() != "create":
                target_role = self._staff_role(str(body.get("staff_id") or ""))
            if staff_body_needs_step_up(body, target_role):
                return self._post_step_up("/api/admin/staff", body,
                                          "Changing an owner account (or granting owner) needs a fresh owner code.")
        return self._post("/api/admin/staff", body)

    def _staff_role(self, staff_id: str) -> Optional[str]:
        """The target row's role (None when it cannot be read)."""
        result = self.staff_list()
        if not (result.ok and isinstance(result.data, dict)):
            return None
        for row in result.data.get("staff") or []:
            if isinstance(row, dict) and row.get("staff_id") == staff_id:
                return str(row.get("role") or "")
        return None

    def audit(self, query: dict) -> ApiResult:
        return self._get(self.audit_path, query)

    def server_config(self) -> ApiResult:
        return self._get("/api/admin/config")

    def server_config_update(self, body: dict) -> ApiResult:
        if self.bearer_owner and config_body_needs_step_up(body):
            return self._post_step_up("/api/admin/config", body,
                                      "This owner-sensitive change needs a fresh owner code.")
        return self._post("/api/admin/config", body)

    def staff_enroll(self, staff_id: str, enroll_key: str) -> ApiResult:
        # Contract §2: {enroll_key, staff_id, machine_id, nonce, timestamp}.
        body = {
            "staff_id": staff_id,
            "enroll_key": enroll_key,
            "machine_id": self.config.machine_id,
            **request_freshness_fields(),
        }
        return self.transport.request("POST", self._url("/api/staff/enroll"),
                                      {"Content-Type": "application/json", "Accept": "application/json"}, body)

    def staff_login(self, staff_id: str, totp_code: str = "") -> ApiResult:
        body = {
            "staff_id": staff_id,
            "machine_id": self.config.machine_id,
            **request_freshness_fields(),
        }
        # Owner-role rows only (rc1 RT-CRIT-01): sent only when a code was given.
        if totp_code:
            body["totp_code"] = totp_code
        return self.transport.request("POST", self._url("/api/staff/login"),
                                      {"Content-Type": "application/json", "Accept": "application/json"}, body)


# --------------------------------------------------------------------------- #
# Command handlers (take an injected client + io so they are testable)
# --------------------------------------------------------------------------- #

class CommandError(Exception):
    """Raised for user-facing failures (bad args, refused confirmation, API errors)."""


def _confirm_destructive(args, summary: str, confirmer: Callable[[str], bool]) -> None:
    if getattr(args, "yes", False):
        return
    if not confirmer(f"Confirm {summary}? Type 'yes' to proceed: "):
        raise CommandError("Aborted by user.")


def _unwrap(result: ApiResult, not_deployed_hint: str = "") -> dict:
    if result.ok and result.data.get("ok", True):
        return result.data
    if result.status == 404 and not_deployed_hint:
        raise CommandError(not_deployed_hint)
    error = result.data.get("error") if isinstance(result.data, dict) else ""
    message = result.data.get("message") if isinstance(result.data, dict) else ""
    if error in STEP_UP_MESSAGES:
        raise CommandError(STEP_UP_MESSAGES[error])
    detail = " - ".join([part for part in (error, message) if part])
    raise CommandError(detail or result.error or f"request failed (status {result.status})")


def _license_target(args) -> dict:
    """The {key | email | discord_user_id | machine_id} selector shared by §2 bodies."""
    target = {}
    if getattr(args, "key", ""):
        target["key"] = args.key.strip().upper()
    if getattr(args, "email", ""):
        target["email"] = args.email.strip()
    if getattr(args, "discord", ""):
        target["discord_user_id"] = args.discord.strip()
    if getattr(args, "machine", ""):
        target["machine_id"] = args.machine.strip()
    return target


def _require_key(args) -> str:
    key = (getattr(args, "key", "") or "").strip().upper()
    if not key:
        raise CommandError("--key is required.")
    return key


def cmd_version(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.get_version())
    out(f"backend: {data.get('api', '?')} v{data.get('version', '?')} env={data.get('env', '?')}")
    return data


def cmd_whoami(client: OrionAdminClient, args, out, confirmer) -> dict:
    result = client.whoami()
    if result.ok:
        staff = result.data.get("staff", result.data)
        out(f"role: {staff.get('role', '?')}  staff_id: {staff.get('staff_id', '-')}  "
            f"discord: {staff.get('discord_user_id', '-')}")
        caps = staff.get("caps") or {}
        usage = staff.get("usage") or {}
        if caps or usage:
            out(f"caps: {json.dumps(caps, sort_keys=True)}")
            out(f"usage today: {json.dumps(usage, sort_keys=True)}")
        return result.data
    # Degrade: report what the local config believes.
    out(f"(server whoami unavailable: {result.error or result.status}) local role: {client.config.role or 'unknown'}")
    return {"role": client.config.role}


def cmd_caps(client: OrionAdminClient, args, out, confirmer) -> dict:
    out("Capability matrix (display only - the server's require_capability decides):")
    for role in ("owner", "admin", "support"):
        caps = ROLE_CAPABILITIES[role]
        out(f"  {role:<8} {'everything' if '*' in caps else ', '.join(sorted(caps))}")
    out("  owner-only extras: " + ", ".join(OWNER_ONLY_CAPABILITIES))
    return {"roles": {role: sorted(ROLE_CAPABILITIES[role]) for role in ROLE_CAPABILITIES}}


def cmd_metrics(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.metrics(), not_deployed_hint="Metrics need the owner backend (not deployed yet).")
    metrics = data.get("metrics", data)
    for section in ("licenses", "trials", "activations", "resets", "staff", "versions"):
        if section in metrics:
            out(f"  {section}: {json.dumps(metrics[section], sort_keys=True)}")
    for scalar in ("online_now", "fraud_flagged"):
        if scalar in metrics:
            out(f"  {scalar}: {metrics[scalar]}")
    return data


# ---- licenses -------------------------------------------------------------- #

def cmd_license_lookup(client: OrionAdminClient, args, out, confirmer) -> dict:
    target = _license_target(args)
    if not target:
        raise CommandError("Provide --key, --email, --discord, or --machine to look up a license.")
    body = {"action": "lookup"}
    body.update(target)
    data = _unwrap(client.license(body))
    licenses = data.get("licenses")
    if licenses is None:
        licenses = [data["license"]] if isinstance(data.get("license"), dict) else []
    if data.get("lookup_mode"):
        out(f"lookup_mode: {data['lookup_mode']}")
    out(f"{len(licenses)} match(es):")
    for lic in licenses:
        for line in format_license(lic, args.reveal):
            out(line)
        out("  -")
    return data


def cmd_license_create(client: OrionAdminClient, args, out, confirmer) -> dict:
    reason = validate_reason(args.reason)
    if args.count < 1 or args.count > 25:
        raise CommandError("--count must be between 1 and 25 per call.")
    body = {
        "action": "create",
        "reason": reason,
        "plan": args.plan,
        "days": args.days,
        "count": args.count,
    }
    if args.discord:
        body["discord_user_id"] = args.discord
    if args.email:
        body["email"] = args.email
    if args.note:
        body["note"] = args.note[:REASON_MAX_CHARS]
    data = _unwrap(client.license(body))
    keys = data.get("keys") or ([data["license_key"]] if data.get("license_key") else [])
    out("License(s) created (store the keys now; they are not shown in full again):")
    for key in keys:
        out(f"  {key}")
    return data


def cmd_license_simple(client: OrionAdminClient, args, out, confirmer, action: str) -> dict:
    """revoke / unrevoke / freeze / unfreeze - body {action, key, reason}."""
    key = _require_key(args)
    reason = validate_reason(args.reason)
    if is_destructive(action):
        _confirm_destructive(args, f"{action} {mask_key(key)} ({reason})", confirmer)
    data = _unwrap(client.license({"action": action, "key": key, "reason": reason}))
    out(f"{action} applied to {mask_key(key)}.")
    return data


def cmd_license_reset_machine(client: OrionAdminClient, args, out, confirmer) -> dict:
    key = _require_key(args)
    reason = validate_reason(args.reason)
    _confirm_destructive(args, f"reset the machine binding for {mask_key(key)} ({reason})", confirmer)
    body = {"action": "reset_machine", "key": key, "reason": reason}
    if args.force:
        body["force"] = True
    data = _unwrap(client.license(body))
    out(f"Machine binding reset for {mask_key(key)}"
        + (" (forced, policy bypassed)." if args.force else f" (mode={data.get('mode', 'staff')})."))
    return data


def cmd_license_extend(client: OrionAdminClient, args, out, confirmer) -> dict:
    key = _require_key(args)
    reason = validate_reason(args.reason)
    if args.days is None or args.days <= 0:
        raise CommandError("--days must be a positive number of days.")
    data = _unwrap(client.license({"action": "extend", "key": key, "days": int(args.days), "reason": reason}))
    lic = data.get("license") or {}
    if lic.get("expiry"):
        out(f"Extended {mask_key(key)} to {time.strftime('%Y-%m-%d', time.gmtime(int(lic['expiry'])))}.")
    else:
        out(f"Extended {mask_key(key)} by {args.days} day(s).")
    return data


def cmd_license_set_plan(client: OrionAdminClient, args, out, confirmer) -> dict:
    key = _require_key(args)
    reason = validate_reason(args.reason)
    data = _unwrap(client.license({"action": "set_plan", "key": key, "plan": args.plan, "reason": reason}))
    out(f"Set plan for {mask_key(key)} to {args.plan}.")
    return data


def cmd_license_transfer(client: OrionAdminClient, args, out, confirmer) -> dict:
    key = _require_key(args)
    reason = validate_reason(args.reason)
    if not args.discord and not args.email:
        raise CommandError("--discord or --email is required to transfer a license.")
    _confirm_destructive(args, f"transfer {mask_key(key)} ({reason})", confirmer)
    body = {"action": "transfer", "key": key, "reason": reason}
    if args.discord:
        body["discord_user_id"] = args.discord
    if args.email:
        body["email"] = args.email
    data = _unwrap(client.license(body))
    out(f"Transferred {mask_key(key)} (machine binding cleared; reset counters travel with the key).")
    return data


def cmd_license_set_reset_policy(client: OrionAdminClient, args, out, confirmer) -> dict:
    key = _require_key(args)
    reason = validate_reason(args.reason)
    body = {"action": "set_reset_policy", "key": key, "reason": reason}
    if args.free_resets is not None:
        body["free_resets"] = int(args.free_resets)
    if args.penalty_days is not None:
        body["penalty_days"] = int(args.penalty_days)
    if args.locked is not None:
        body["locked"] = args.locked == "true"
    if len(body) == 3:
        raise CommandError("Nothing to set: pass --free-resets, --penalty-days, or --locked.")
    data = _unwrap(client.license_owner(body))
    out(f"Reset policy updated for {mask_key(key)}.")
    return data


def cmd_license_blacklist(client: OrionAdminClient, args, out, confirmer, add: bool) -> dict:
    reason = validate_reason(args.reason)
    if not args.machine and not args.discord:
        raise CommandError("--machine or --discord is required.")
    action = "blacklist" if add else "unblacklist"
    if add:
        _confirm_destructive(args, f"{action} {args.machine or args.discord} ({reason})", confirmer)
    body = {"action": action, "reason": reason}
    if args.machine:
        body["machine_id"] = args.machine
    if args.discord:
        body["discord_user_id"] = args.discord
    data = _unwrap(client.license_owner(body))
    out(f"{action} applied.")
    return data


# ---- staff ----------------------------------------------------------------- #

def cmd_staff_list(client: OrionAdminClient, args, out, confirmer) -> dict:
    hint = "Staff management needs the owner backend (not deployed yet)."
    data = _unwrap(client.staff_list(), not_deployed_hint=hint)
    members = data.get("staff", [])
    out(f"{len(members)} staff row(s):")
    for member in members:
        caps = member.get("caps") or {}
        usage = member.get("usage") or {}
        state = "disabled" if member.get("disabled") else "enabled"
        out(f"  {member.get('staff_id', '?'):<20} {member.get('discord_user_id', '?'):<22} "
            f"{member.get('role', '?'):<8} {state:<9} "
            f"keys {usage.get('keys', 0)}/{caps.get('keys_per_day', 0)} "
            f"resets {usage.get('resets', 0)}/{caps.get('resets_per_day', 0)} "
            f"extend<= {caps.get('extend_max_days', 0)}d")
    return data


def cmd_staff_create(client: OrionAdminClient, args, out, confirmer) -> dict:
    reason = validate_reason(args.reason)
    body = {"action": "create", "discord_user_id": args.discord, "role": args.role, "reason": reason}
    caps = parse_caps(args)
    if caps:
        body["caps"] = caps
    data = _unwrap(client.staff(body), not_deployed_hint="Staff management needs the owner backend.")
    out(f"Created staff {data.get('staff_id', '?')} ({args.role}).")
    if data.get("enroll_key"):
        out("Enrollment key (shown once; only its salted hash is stored):")
        out(f"  {data['enroll_key']}")
    return data


def cmd_staff_action(client: OrionAdminClient, args, out, confirmer, action: str) -> dict:
    reason = validate_reason(args.reason)
    if not args.staff_id:
        raise CommandError("--staff-id is required.")
    if is_destructive("staff-" + action.replace("_", "-")):
        _confirm_destructive(args, f"{action} for {args.staff_id} ({reason})", confirmer)
    body = {"action": action, "staff_id": args.staff_id, "reason": reason}
    if action == "set_role":
        body["role"] = args.role
    if action == "set_caps":
        caps = parse_caps(args)
        if not caps:
            raise CommandError("Pass at least one of --keys-per-day / --resets-per-day / --extend-max-days.")
        body["caps"] = caps
    data = _unwrap(client.staff(body), not_deployed_hint="Staff management needs the owner backend.")
    if action == "reissue_enrollment" and data.get("enroll_key"):
        out("Enrollment key reissued (shown once):")
        out(f"  {data['enroll_key']}")
    else:
        out(f"staff {action} applied to {args.staff_id}.")
    return data


def cmd_staff_enroll(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.staff_enroll(args.staff_id, args.enroll_key))
    token = data.get("token", "")
    if not token:
        raise CommandError("Enrollment succeeded but backend did not return a staff token.")
    staff = data.get("staff", {})
    config = load_config(CONFIG_PATH)
    if args.base_url:
        config.base_url = args.base_url
    config.admin_secret = ""
    config.staff_token = token
    config.staff_id = staff.get("staff_id", args.staff_id)
    config.discord_user_id = staff.get("discord_user_id", config.discord_user_id)
    config.role = staff.get("role", "support")
    save_config(config, CONFIG_PATH)
    out(f"Enrolled on this machine ({config.machine_id_suffix}); credentials saved to {CONFIG_PATH}. "
        "Token not displayed.")
    return data


def cmd_staff_login(client: OrionAdminClient, args, out, confirmer) -> dict:
    owner = bool(getattr(args, "owner", False))
    code = client.fresh_owner_code("Owner-role sign-in needs a fresh owner code.") if owner else ""
    result = client.staff_login(args.staff_id, totp_code=code)
    code = ""
    if (not owner and not result.ok and isinstance(result.data, dict)
            and result.data.get("error") == "totp_required"):
        raise CommandError("This is an owner account: run 'staff-login --owner' and enter a fresh owner code.")
    data = _unwrap(result)
    token = data.get("token", "")
    if not token:
        raise CommandError("Login succeeded but backend did not return a staff token.")
    staff = data.get("staff", {})
    config = load_config(CONFIG_PATH)
    if args.base_url:
        config.base_url = args.base_url
    config.admin_secret = ""
    config.staff_token = token
    config.staff_id = staff.get("staff_id", args.staff_id)
    config.discord_user_id = staff.get("discord_user_id", config.discord_user_id)
    config.role = staff.get("role", "support")
    save_config(config, CONFIG_PATH)
    out(f"Staff login saved to {CONFIG_PATH}. Token not displayed.")
    return data


# ---- audit ----------------------------------------------------------------- #

def cmd_audit(client: OrionAdminClient, args, out, confirmer) -> dict:
    query = {"limit": str(max(1, min(200, args.limit)))}
    for name in ("since", "until", "actor", "action", "target", "cursor"):
        value = getattr(args, name, "")
        if value:
            query[name] = str(value)
    data = _unwrap(client.audit(query), not_deployed_hint="Audit log needs the v2 backend (not deployed yet).")
    rows = data.get("audit", [])
    out(f"{len(rows)} audit row(s):")
    for row in rows:
        out("  " + format_audit_row(row))
    if data.get("next_cursor"):
        out(f"next cursor: {data['next_cursor']}")
    return data


# ---- config / killswitch --------------------------------------------------- #

def cmd_server_config_show(client: OrionAdminClient, args, out, confirmer) -> dict:
    data = _unwrap(client.server_config(), not_deployed_hint="Config endpoint needs the owner backend.")
    config = data.get("config", data)
    for key in sorted(config.keys()):
        if key == "ok":
            continue
        out(f"  {key}: {json.dumps(config[key], sort_keys=True)}")
    return data


def cmd_server_config_set(client: OrionAdminClient, args, out, confirmer) -> dict:
    reason = validate_reason(args.reason)
    body = {"reason": reason}
    if args.min_client_version:
        body["min_client_version"] = args.min_client_version
    if args.blocked_versions is not None:
        body["blocked_versions"] = parse_list(args.blocked_versions)
    if args.motd_text is not None:
        body["motd"] = {
            "text": args.motd_text,
            "level": args.motd_level,
            "until": int(args.motd_until or 0),
        }
    policy = {}
    for arg_name, field in (("free_resets", "free_resets"), ("penalty_days", "penalty_days"),
                            ("cooldown_s", "cooldown_s")):
        value = getattr(args, arg_name)
        if value is not None:
            policy[field] = int(value)
    if args.self_service is not None:
        policy["self_service"] = args.self_service == "true"
    if policy:
        body["reset_policy_defaults"] = policy
    fraud = {}
    if args.machines_30d is not None:
        fraud["machines_30d"] = int(args.machines_30d)
    if args.resets_30d is not None:
        fraud["resets_30d"] = int(args.resets_30d)
    if fraud:
        body["fraud_thresholds"] = fraud
    alerts = {}
    if args.alert_owner:
        alerts["owner_discord_user_id"] = args.alert_owner
    if args.alert_events is not None:
        alerts["events"] = parse_list(args.alert_events)
    if alerts:
        body["alerts"] = alerts
    if args.owner_totp_required is not None:
        body["owner_totp_required"] = args.owner_totp_required == "true"
    if args.ip_allowlist is not None:
        body["owner_ip_allowlist"] = parse_list(args.ip_allowlist)
    if len(body) == 1:
        raise CommandError("Nothing to set. See 'orion-admin server-config set --help'.")
    data = _unwrap(client.server_config_update(body))
    out("Config updated: " + ", ".join(sorted(k for k in body if k != "reason")))
    return data


def cmd_totp_enroll(client: OrionAdminClient, args, out, confirmer) -> dict:
    reason = validate_reason(args.reason)
    data = _unwrap(client.server_config_update({"action": "totp_enroll", "reason": reason}))
    out("TOTP enrollment (shown once):")
    out(f"  otpauth: {data.get('otpauth_uri', '(missing)')}")
    if data.get("secret"):
        out(f"  secret:  {data['secret']}")
    out("Confirm a code with 'orion-admin server-config totp-confirm --code ...' to turn the requirement on.")
    return data


def cmd_totp_confirm(client: OrionAdminClient, args, out, confirmer) -> dict:
    reason = validate_reason(args.reason)
    if not args.code:
        raise CommandError("--code is required.")
    data = _unwrap(client.server_config_update({"action": "totp_confirm", "code": args.code, "reason": reason}))
    out("TOTP confirmed. Owner-secret requests now require a code (send it with ORION_ADMIN_TOTP).")
    return data


def cmd_rotate_secret(client: OrionAdminClient, args, out, confirmer) -> dict:
    reason = validate_reason(args.reason)
    _confirm_destructive(args, f"rotate the owner admin secret ({reason})", confirmer)
    data = _unwrap(client.server_config_update({"action": "rotate_admin_secret", "reason": reason}))
    secret = data.get("admin_secret") or data.get("new_secret") or data.get("secret")
    if not secret:
        raise CommandError("Rotation succeeded but the backend returned no secret.")
    out("New admin secret (shown once; store it now, the old one is dead):")
    out(f"  {secret}")
    return data


def cmd_killswitch(client: OrionAdminClient, args, out, confirmer) -> dict:
    # Contract §5: global kill lives in the owner config; the bot route is read-only.
    if args.killswitch_command == "status":
        data = _unwrap(client.server_config())
        config = data.get("config", data)
        state = "ENGAGED" if config.get("global_kill") else "released"
        reason = config.get("kill_reason") or config.get("global_kill_reason") or ""
        out(f"killswitch: {state}" + (f"  reason={reason}" if reason else ""))
        return data
    reason = validate_reason(args.reason)
    engage = args.killswitch_command == "engage"
    if engage:
        _confirm_destructive(args, f"engage the GLOBAL killswitch ({reason}) - this disables the live service",
                             confirmer)
    data = _unwrap(client.server_config_update({"global_kill": engage, "reason": reason}))
    out(f"Killswitch {'engaged' if engage else 'released'}. reason={reason}")
    return data


# --------------------------------------------------------------------------- #
# Parser + dispatch
# --------------------------------------------------------------------------- #

def _add_reason(parser, default: str = "") -> None:
    parser.add_argument("--reason", default=default,
                        help=f"Required audit reason (≤{REASON_MAX_CHARS} chars).")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orion-admin", description="Orion owner/staff admin CLI.")
    parser.add_argument("--base-url", default=None, help="Override the API base URL (HTTPS).")
    parser.add_argument("--yes", action="store_true", help="Skip interactive confirmation (reason still required).")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="Show backend version.")
    sub.add_parser("whoami", help="Show the authenticated identity, role, caps and usage.")
    sub.add_parser("caps", help="Print the capability matrix (display only).")
    sub.add_parser("metrics", help="Owner dashboard metrics.")

    login = sub.add_parser("login", help="Store admin/staff credentials locally (prompted, never echoed).")
    login.add_argument("--mode", choices=["owner", "staff"], default="owner")
    login.add_argument("--staff-id", default="", help="Staff id (required for staff).")
    login.add_argument("--discord", default="", help="Discord user id (staff actor identity).")
    login.add_argument("--role", default="", help="Local role label for display.")
    sub.add_parser("logout", help="Remove the local credential file.")
    sub.add_parser("config", help="Show the (redacted) effective local config.")

    p = sub.add_parser("staff-enroll", help="Enrol this machine with the one-time enroll key.")
    p.add_argument("--staff-id", required=True, help="Staff id issued by the owner.")
    p.add_argument("--enroll-key", required=True, help="One-time enroll key from the owner.")

    p = sub.add_parser("staff-login", help="Refresh a staff bearer token for this machine.")
    p.add_argument("--staff-id", required=True, help="Staff id issued by the owner.")
    p.add_argument("--owner", action="store_true",
                   help="Owner-role account: ask for a fresh owner TOTP code (sent once, never saved).")

    # ---- licenses ------------------------------------------------------
    lic = sub.add_parser("license", help="License operations (contract §2).")
    lic_sub = lic.add_subparsers(dest="license_command", required=True)

    p = lic_sub.add_parser("lookup", help="Look up by key / email / discord / machine.")
    p.add_argument("--key", default="")
    p.add_argument("--email", default="")
    p.add_argument("--discord", default="")
    p.add_argument("--machine", default="")
    p.add_argument("--reveal", action="store_true", help="Show the full key (off by default).")

    p = lic_sub.add_parser("create", help="Create licenses (keys shown once).")
    # Owner rule 2026-09-15: `month` is the only plan sold (free 7-day trial, then
    # $19.99/month recurring). `lifetime`/`week`/`day` are still ACCEPTED so staff can
    # mint a comp and so keys already sold keep resolving — they are not sold.
    p.add_argument("--plan", default=SELLABLE_PLAN,
                   help=f"Plan tier. Default {SELLABLE_PLAN!r} — the only plan sold. "
                        "Legacy values (week/day/lifetime) still mint, for staff comps.")
    p.add_argument("--days", type=int, default=30, help="0 = lifetime (staff comp only).")
    p.add_argument("--count", type=int, default=1, help="1-25 per call.")
    p.add_argument("--discord", default="")
    p.add_argument("--email", default="")
    p.add_argument("--note", default="")
    _add_reason(p)

    for name, help_text in (("revoke", "Revoke a license."),
                            ("unrevoke", "Un-revoke a license."),
                            ("freeze", "Freeze a license (the clock stops)."),
                            ("unfreeze", "Unfreeze a license (expiry moves by the frozen span).")):
        p = lic_sub.add_parser(name, help=help_text)
        p.add_argument("--key", required=True)
        _add_reason(p)

    p = lic_sub.add_parser("reset-machine", help="Clear the machine binding (HWID reset).")
    p.add_argument("--key", required=True)
    p.add_argument("--force", action="store_true", help="Owner only: ignore cooldown, lock and counters.")
    _add_reason(p)

    p = lic_sub.add_parser("extend", help="Extend expiry by N days (the server computes the new expiry).")
    p.add_argument("--key", required=True)
    p.add_argument("--days", type=int, default=None)
    _add_reason(p)

    p = lic_sub.add_parser("set-plan", help=f"Change the plan tier (sold: {SELLABLE_PLAN}).")
    p.add_argument("--key", required=True)
    p.add_argument("--plan", required=True)
    _add_reason(p)

    p = lic_sub.add_parser("transfer", help="Transfer a key to another owner (clears the machine binding).")
    p.add_argument("--key", required=True)
    p.add_argument("--discord", default="")
    p.add_argument("--email", default="")
    _add_reason(p)

    p = lic_sub.add_parser("set-reset-policy", help="Owner: per-key HWID reset policy.")
    p.add_argument("--key", required=True)
    p.add_argument("--free-resets", type=int, default=None)
    p.add_argument("--penalty-days", "--deduct-days", type=int, default=None, dest="penalty_days")
    p.add_argument("--locked", choices=["true", "false"], default=None)
    _add_reason(p)

    for name, help_text in (("blacklist", "Owner: block a machine or Discord id."),
                            ("unblacklist", "Owner: remove a blacklist entry.")):
        p = lic_sub.add_parser(name, help=help_text)
        p.add_argument("--machine", default="")
        p.add_argument("--discord", default="")
        _add_reason(p)

    # ---- staff ---------------------------------------------------------
    staff = sub.add_parser("staff", help="Owner-only staff management (contract §2).")
    staff_sub = staff.add_subparsers(dest="staff_command", required=True)
    staff_sub.add_parser("list", help="List staff with role, caps and usage.")

    p = staff_sub.add_parser("create", help="Create a staff row; returns the one-time enroll key.")
    p.add_argument("--discord", required=True)
    p.add_argument("--role", required=True, choices=["owner", "admin", "support"])
    p.add_argument("--keys-per-day", type=int, default=None)
    p.add_argument("--resets-per-day", type=int, default=None)
    p.add_argument("--extend-max-days", type=int, default=None)
    _add_reason(p)

    for name, help_text in (("disable", "Disable a staff member."),
                            ("enable", "Re-enable a staff member."),
                            ("reset-machine", "Clear the machine binding and revoke tokens."),
                            ("reissue-enrollment", "Issue a new one-time enroll key.")):
        p = staff_sub.add_parser(name, help=help_text)
        p.add_argument("--staff-id", required=True)
        _add_reason(p)

    p = staff_sub.add_parser("set-role", help="Change a staff member's role.")
    p.add_argument("--staff-id", required=True)
    p.add_argument("--role", required=True, choices=["owner", "admin", "support"])
    _add_reason(p)

    p = staff_sub.add_parser("set-caps", help="Change a staff member's daily caps.")
    p.add_argument("--staff-id", required=True)
    p.add_argument("--keys-per-day", type=int, default=None)
    p.add_argument("--resets-per-day", type=int, default=None)
    p.add_argument("--extend-max-days", type=int, default=None)
    _add_reason(p)

    # ---- audit ---------------------------------------------------------
    p = sub.add_parser("audit", help="Audit rows (owner: everything; staff: own rows).")
    p.add_argument("--since", default="", help="Unix seconds.")
    p.add_argument("--until", default="", help="Unix seconds.")
    p.add_argument("--actor", default="")
    p.add_argument("--action", default="")
    p.add_argument("--target", default="")
    p.add_argument("--cursor", default="", help="Continue a previous page.")
    p.add_argument("--limit", type=int, default=50, help="Max 200.")

    # ---- server config -------------------------------------------------
    cfg = sub.add_parser("server-config", help="Owner config: version gate, MOTD, policy, alerts, TOTP (contract §5).")
    cfg_sub = cfg.add_subparsers(dest="config_command", required=True)
    cfg_sub.add_parser("show", help="Print the current server config.")

    p = cfg_sub.add_parser("set", help="Update config keys.")
    p.add_argument("--min-client-version", default="")
    p.add_argument("--blocked-versions", default=None, help="Comma separated; empty string clears.")
    p.add_argument("--motd-text", default=None, help="Empty string clears the MOTD.")
    p.add_argument("--motd-level", choices=["info", "warn", "maint"], default="info")
    p.add_argument("--motd-until", type=int, default=None, help="Unix seconds.")
    p.add_argument("--free-resets", type=int, default=None,
                   help=f"Free HWID resets per key (default {RESET_FREE_DEFAULT}).")
    p.add_argument("--penalty-days", "--deduct-days", type=int, default=None, dest="penalty_days",
                   help=f"Days deducted per reset once the free ones are used "
                        f"(default {RESET_PENALTY_DAYS}).")
    p.add_argument("--cooldown-s", type=int, default=None)
    p.add_argument("--self-service", choices=["true", "false"], default=None)
    p.add_argument("--machines-30d", type=int, default=None)
    p.add_argument("--resets-30d", type=int, default=None)
    p.add_argument("--alert-owner", default="", help="alerts.owner_discord_user_id")
    p.add_argument("--alert-events", default=None, help="Comma separated event names.")
    p.add_argument("--owner-totp-required", choices=["true", "false"], default=None)
    p.add_argument("--ip-allowlist", default=None, help="Comma separated CIDRs; empty string clears.")
    _add_reason(p)

    p = cfg_sub.add_parser("totp-enroll", help="Start owner TOTP enrollment (URI shown once).")
    _add_reason(p)
    p = cfg_sub.add_parser("totp-confirm", help="Confirm a TOTP code and turn the requirement on.")
    p.add_argument("--code", required=True)
    _add_reason(p)
    p = cfg_sub.add_parser("rotate-secret", help="Rotate the owner admin secret (new secret shown once).")
    _add_reason(p)

    # ---- killswitch ----------------------------------------------------
    kill = sub.add_parser("killswitch", help="Owner-only global killswitch (config.global_kill).")
    kill_sub = kill.add_subparsers(dest="killswitch_command", required=True)
    kill_sub.add_parser("status", help="Show whether the global killswitch is engaged.")
    p = kill_sub.add_parser("engage", help="Engage the global killswitch.")
    _add_reason(p)
    p = kill_sub.add_parser("release", help="Release the global killswitch.")
    _add_reason(p)

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
        if not args.staff_id:
            out("Staff login requires --staff-id <id>.")
            return 1
        token = prompter("Staff token: ")
        if not token:
            out("No token entered; nothing saved.")
            return 1
        config.staff_token = token.strip()
        config.staff_id = args.staff_id
        config.discord_user_id = args.discord or config.discord_user_id
        config.role = args.role or "support"
    save_config(config, path)
    out(f"Credentials saved to {path} (role={config.role}). Secret not displayed.")
    return 0


LICENSE_DISPATCH = {
    "lookup": cmd_license_lookup,
    "create": cmd_license_create,
    "reset-machine": cmd_license_reset_machine,
    "extend": cmd_license_extend,
    "set-plan": cmd_license_set_plan,
    "transfer": cmd_license_transfer,
    "set-reset-policy": cmd_license_set_reset_policy,
}

STAFF_ACTION_DISPATCH = {
    "disable": "disable",
    "enable": "enable",
    "reset-machine": "reset_machine",
    "reissue-enrollment": "reissue_enrollment",
    "set-role": "set_role",
    "set-caps": "set_caps",
}

CONFIG_DISPATCH = {
    "show": cmd_server_config_show,
    "set": cmd_server_config_set,
    "totp-enroll": cmd_totp_enroll,
    "totp-confirm": cmd_totp_confirm,
    "rotate-secret": cmd_rotate_secret,
}


def run(args, client: OrionAdminClient, out: Callable[[str], None], confirmer: Callable[[str], bool]) -> int:
    """Dispatch a parsed command. Returns a process exit code."""
    try:
        if args.command == "version":
            cmd_version(client, args, out, confirmer)
        elif args.command == "whoami":
            cmd_whoami(client, args, out, confirmer)
        elif args.command == "caps":
            cmd_caps(client, args, out, confirmer)
        elif args.command == "config":
            for key, value in redact_config(client.config).items():
                out(f"  {key}: {value}")
        elif args.command == "staff-enroll":
            cmd_staff_enroll(client, args, out, confirmer)
        elif args.command == "staff-login":
            cmd_staff_login(client, args, out, confirmer)
        elif args.command == "metrics":
            _require_auth(client)
            cmd_metrics(client, args, out, confirmer)
        elif args.command == "license":
            _require_auth(client)
            if args.license_command in ("revoke", "unrevoke", "freeze", "unfreeze"):
                cmd_license_simple(client, args, out, confirmer, action=args.license_command)
            elif args.license_command in ("blacklist", "unblacklist"):
                cmd_license_blacklist(client, args, out, confirmer, add=(args.license_command == "blacklist"))
            else:
                LICENSE_DISPATCH[args.license_command](client, args, out, confirmer)
        elif args.command == "staff":
            _require_auth(client)
            if args.staff_command == "list":
                cmd_staff_list(client, args, out, confirmer)
            elif args.staff_command == "create":
                cmd_staff_create(client, args, out, confirmer)
            else:
                cmd_staff_action(client, args, out, confirmer, action=STAFF_ACTION_DISPATCH[args.staff_command])
        elif args.command == "audit":
            _require_auth(client)
            cmd_audit(client, args, out, confirmer)
        elif args.command == "server-config":
            _require_auth(client)
            CONFIG_DISPATCH[args.config_command](client, args, out, confirmer)
        elif args.command == "killswitch":
            _require_auth(client)
            cmd_killswitch(client, args, out, confirmer)
        else:
            raise CommandError(f"Unknown command: {args.command}")
        return 0
    except CommandError as exc:
        out(f"error: {exc}")
        return 1


def _require_auth(client: OrionAdminClient) -> None:
    if not client.config.has_auth:
        raise CommandError("Not authenticated. Run 'orion-admin login' or set ORION_ADMIN_SECRET.")


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
    if (config.admin_secret and not config.admin_totp and sys.stdin.isatty()
            and args.command not in ("version", "caps", "config")):
        # Contract §5: an owner-secret request needs a TOTP once the owner has
        # enrolled. Prompted per invocation, never stored; blank stays blank so
        # a not-yet-enrolled owner just presses enter. Scripts (no tty) use
        # ORION_ADMIN_TOTP instead.
        code = input("TOTP code (blank if not enrolled): ").strip()
        config.admin_totp = "".join(ch for ch in code if ch.isdigit())
    def prompt_code(prompt: str) -> str:
        # Hidden input; the code is used for one request and never stored.
        return getpass.getpass(prompt)

    client = OrionAdminClient(config, UrllibTransport(),
                              code_prompter=prompt_code if sys.stdin.isatty() else None)

    def interactive_confirm(prompt: str) -> bool:
        try:
            return input(prompt).strip().lower() == "yes"
        except EOFError:
            return False

    return run(args, client, out, interactive_confirm)


if __name__ == "__main__":
    raise SystemExit(main())
