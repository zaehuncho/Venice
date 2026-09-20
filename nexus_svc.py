from __future__ import annotations

import collections
import json
import hmac
import ipaddress
import logging
import math
import os
import queue
import secrets
import socket
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Resolve pywin32 / pydivert before anything else.
# ---------------------------------------------------------------------------

_NEXUS_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Compiled (Nuitka standalone) service support.
# ---------------------------------------------------------------------------
# The shipped bridge is NexusVisionSvc.exe -- nexus_svc.py Nuitka-compiled the same
# way OrionSidecar.exe is (scripts/build_nexus_service.ps1). A loose .py + .venv311
# interpreter must never ship (docs/IP_PROTECTION_PLAN.md:360; tools/release_filter_policy
# bans this subsystem's dev artifacts), so the compiled exe is the only customer host.
#
# For pywin32 to host the service INSIDE this exe (instead of shelling out to the absent
# pythonservice.exe) the framework keys off sys.frozen -- see
# win32serviceutil.LocatePythonServiceExe/HandleCommandLine/DebugService. Nuitka does not
# set sys.frozen, so set it here, and re-anchor _NEXUS_DIR to the exe's own directory so
# the WinDivert search (below) and the bridge-token paths resolve beside the shipped
# binary rather than inside the frozen module tree.
_IS_FROZEN = ("__compiled__" in globals()) or bool(getattr(sys, "frozen", False))
if _IS_FROZEN:
    if not getattr(sys, "frozen", False):
        sys.frozen = "windows_exe"  # type: ignore[attr-defined]
    _NEXUS_DIR = os.path.dirname(os.path.abspath(sys.executable))


def _extend_path() -> None:
    candidates: list[str] = []
    candidates.append(os.path.join(_NEXUS_DIR, "vendor"))
    candidates.append(os.path.join(_NEXUS_DIR, ".venv", "Lib", "site-packages"))
    candidates.append(os.path.join(_NEXUS_DIR, ".venv311", "Lib", "site-packages"))
    # Fixed admin-controlled Python install paths only; DO NOT glob C:\Users\*
    # — an arbitrary user's site-packages could contain trojanized modules and
    # this service runs as SYSTEM.
    for ver in ("314", "311", "310", "312", "39"):
        for root in (r"C:\Python" + ver, rf"C:\Program Files\Python{ver}"):
            candidates.append(os.path.join(root, "Lib", "site-packages"))
    for p in candidates:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)

_extend_path()

try:
    import win32service        # noqa: F401
    import win32serviceutil
    import win32event
    import servicemanager
    _PYWIN32_OK = True
except ImportError:
    _PYWIN32_OK = False
    win32serviceutil = None  # type: ignore[assignment]

try:
    _wd_dirs = [
        _NEXUS_DIR,
        os.path.join(_NEXUS_DIR, "windivert"),
        os.path.join(_NEXUS_DIR, "vendor", "windivert"),
    ]
    for _d in _wd_dirs:
        if os.path.isfile(os.path.join(_d, "WinDivert64.sys")):
            os.add_dll_directory(_d)
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
            break
    import pydivert
    _PYDIVERT_OK = True
except Exception as _e:
    pydivert = None  # type: ignore[assignment]
    _PYDIVERT_OK = False
    _PYDIVERT_ERR = str(_e)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SVC_NAME         = "NexusVisionSvc"
SVC_DISPLAY_NAME = "Nexus Vision Packet Bridge"
SVC_DESCRIPTION  = (
    "Privileged WinDivert packet capture bridge for Nexus Vision. "
    "Allows the main application to run without Administrator rights."
)

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 47291
# v3 adds the inbound meter-delay intercept path:
#   set_meter_delay / start_meter_intercept / stop_meter_intercept
#   meter_delay_stats  (bench/diagnostic)
# and makes unknown verbs return an explicit error instead of being ignored.
#
# SVC_FEATURES advertises "meter_delay" ONLY when the intercept is armed, so a
# client can tell "this bridge cannot delay packets" apart from "this bridge is
# too old to know the verb". _run_service() rewrites it at startup.
SVC_VERSION = 3
SVC_FEATURES: tuple[str, ...] = ()

# F1 hardening: per-session token so a random local process can't drive the elevated bridge.
# The bridge writes a fresh token record on start; the client must send
# {"cmd":"auth","token":...} before any other command.
#
# WHERE the token lives depends on how the bridge was started, because the security property we
# need -- "only the signed-in user can read this file" -- costs a privileged operation in one
# location and nothing in the other:
#
#   service mode (LocalSystem)  -> %PROGRAMDATA%\NexusVision\nexus_bridge.token
#       ProgramData is world-readable by default, so the DACL MUST be rewritten. LocalSystem can
#       do that. INTERACTIVE gets read-only so the unelevated app can authenticate.
#   debug mode (ordinary user)  -> %LOCALAPPDATA%\NexusVision\nexus_bridge.token
#       LOCALAPPDATA is already per-user private, and because we create the file we own it, so
#       WRITE_DAC is implicit and the DACL can be pinned to owner-only with NO elevation.
#
# This split exists because packet capture ships enabled by default and most users never run as
# Administrator. Previously both modes targeted ProgramData: once an elevated run had hardened
# that file to {SYSTEM, Administrators, INTERACTIVE:R}, every later unelevated run failed to
# reopen it for writing, fell into the fail-closed path with an empty token, and left the stale
# file on disk for the client to read -- auth was then rejected forever.
_BRIDGE_TOKEN_FILENAME = "nexus_bridge.token"
# Record version. v1 = JSON {"v","token","pid","started_ms","mode"}. A bare (non-JSON) file is a
# pre-v1 raw token and is treated as legacy/last-resort by the client.
_BRIDGE_TOKEN_RECORD_VERSION = 1


def _programdata_token_path() -> str:
    """Service-mode token location (needs an ACL edit to be safe -> needs elevation)."""
    return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                        "NexusVision", _BRIDGE_TOKEN_FILENAME)


def _localappdata_token_path() -> str:
    """Debug/unelevated token location (per-user private by default ACL inheritance)."""
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.environ.get("USERPROFILE", r"C:\Users\Default"), "AppData", "Local")
    return os.path.join(base, "NexusVision", _BRIDGE_TOKEN_FILENAME)


# Retained under the historical name: this is the *service-mode* path.
_BRIDGE_TOKEN_PATH = _programdata_token_path()
_BRIDGE_TOKEN = ""
# Populated by _init_bridge_token() so failed auth can say where the token was published.
_BRIDGE_TOKEN_ACTIVE_PATH = ""
_BRIDGE_TOKEN_MODE = ""
_MAX_COMMAND_LINE_BYTES = 16 * 1024
_MAX_COMMAND_BUFFER_BYTES = 64 * 1024


def _validated_ipv4(value, *, require_public: bool = False) -> str | None:
    """Return a canonical, unicast IPv4 address or ``None``.

    Console filters may be private or public, but never accept syntax that can
    escape the address slot in a WinDivert expression.  Court hints are public
    game peers and therefore require a globally routable address.
    """
    try:
        address = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    if (address.is_unspecified or address.is_multicast or address.is_loopback
            or address.is_reserved):
        return None
    if require_public and not address.is_global:
        return None
    return str(address)


def _current_user_sid():
    """SID of the account this process runs as (the account that must read the token)."""
    import win32api, win32con, win32security
    handle = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        return win32security.GetTokenInformation(handle, win32security.TokenUser)[0]
    finally:
        try:
            handle.Close()
        except Exception:
            pass


def _harden_token_acl(path: str, *, allow_interactive_read: bool) -> None:
    """Pin *path* to an explicit, protected, minimal DACL.

    Raises on any failure -- the caller owns the fail-closed policy.  ``PROTECTED`` is set so an
    inherited "Users: read" ACE (which %PROGRAMDATA% grants by default) can never come back, and
    the result is read back and verified rather than assumed.

    ``allow_interactive_read`` is for service mode only: the writer is LocalSystem but the reader
    is the signed-in user, so INTERACTIVE gets read-only.  In debug mode writer and reader are the
    same account, so that account is named explicitly and INTERACTIVE is not granted at all --
    strictly tighter than the service-mode DACL.
    """
    import ntsecuritycon
    import win32security

    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    administrators_sid = win32security.ConvertStringSidToSid("S-1-5-32-544")

    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, system_sid)
    dacl.AddAccessAllowedAce(
        win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, administrators_sid)
    expected = [system_sid, administrators_sid]
    if allow_interactive_read:
        interactive_sid = win32security.ConvertStringSidToSid("S-1-5-4")
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, ntsecuritycon.FILE_GENERIC_READ, interactive_sid)
        expected.append(interactive_sid)
    else:
        user_sid = _current_user_sid()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, user_sid)
        expected.append(user_sid)
    # SetNamedSecurityInfo, not SetFileSecurity: the latter silently IGNORES
    # PROTECTED_DACL_SECURITY_INFORMATION, so the inherited ACEs would survive.  Both calls need
    # only WRITE_DAC, which the creator of the file holds implicitly -- no elevation involved.
    win32security.SetNamedSecurityInfo(
        path, win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None, None, dacl, None)
    _verify_token_acl(path, expected)


def _verify_token_acl(path: str, expected_sids) -> None:
    """Read the DACL back and raise unless it is protected and grants only *expected_sids*."""
    import win32security

    sd = win32security.GetFileSecurity(path, win32security.DACL_SECURITY_INFORMATION)
    control = sd.GetSecurityDescriptorControl()[0]
    if not control & win32security.SE_DACL_PROTECTED:
        raise PermissionError("token DACL is not protected; inherited access could be granted")
    dacl = sd.GetSecurityDescriptorDacl()
    if dacl is None:
        raise PermissionError("token has a NULL DACL (everyone would have full access)")
    allowed = {win32security.ConvertSidToStringSid(sid) for sid in expected_sids}
    for index in range(dacl.GetAceCount()):
        ace = dacl.GetAce(index)
        sid_text = win32security.ConvertSidToStringSid(ace[-1])
        if sid_text not in allowed:
            raise PermissionError(f"token DACL grants unexpected principal {sid_text}")


def _write_bridge_token_file(path: str, record: dict, *, allow_interactive_read: bool) -> None:
    """Publish *record* at *path* with a verified minimal ACL, or raise leaving nothing behind."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = json.dumps(record, separators=(",", ":"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(payload)
    try:
        _harden_token_acl(path, allow_interactive_read=allow_interactive_read)
        # An unreadable or half-written token is as broken as a missing one, and it fails much
        # later and much more confusingly, so prove the round trip now.
        with open(path, "r", encoding="utf-8") as fh:
            if fh.read() != payload:
                raise OSError("token file did not read back byte-identical")
    except Exception:
        # An unprotected bearer token is equivalent to no authentication: never leave one behind.
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def _log_competing_token_files(active_path: str) -> None:
    """Point at any other token file that a client could mistakenly read."""
    for candidate in (_programdata_token_path(), _localappdata_token_path()):
        if os.path.normcase(candidate) == os.path.normcase(active_path):
            continue
        try:
            mtime = os.path.getmtime(candidate)
        except OSError:
            continue
        _log.warning(
            "Another bridge token file exists at %s (mtime %s); it is NOT this session's token. "
            "Clients skip it because the pid recorded in it is not running -- but a pre-v1 file "
            "there carries no pid and cannot be checked, so delete it if auth is rejected.",
            candidate, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime)))


def _init_bridge_token(mode: str = "service") -> None:
    """Mint this session's bearer token and publish it where the client can read it.

    ``mode`` is ``"service"`` (LocalSystem -> ProgramData) or ``"debug"`` (ordinary user ->
    LOCALAPPDATA).  Publishing failure is fatal to authentication by design: ``_BRIDGE_TOKEN``
    stays empty and every auth is rejected rather than falling back to an unprotected token.
    """
    global _BRIDGE_TOKEN, _BRIDGE_TOKEN_ACTIVE_PATH, _BRIDGE_TOKEN_MODE
    _BRIDGE_TOKEN = ""
    _BRIDGE_TOKEN_ACTIVE_PATH = ""
    _BRIDGE_TOKEN_MODE = ""

    token = secrets.token_hex(24)
    record = {
        "v":          _BRIDGE_TOKEN_RECORD_VERSION,
        "token":      token,
        "pid":        os.getpid(),
        "started_ms": int(time.time() * 1000),
        "mode":       mode,
    }

    if mode == "service":
        # LocalSystem's own LOCALAPPDATA lives under systemprofile and the signed-in user cannot
        # read it, so there is no useful fallback here -- ProgramData or fail closed.
        attempts = [(_programdata_token_path(), True)]
    else:
        # ProgramData is the fallback only; unlike the old code it is written owner-only, so an
        # unelevated run never publishes a file it cannot rewrite next time.
        attempts = [(_localappdata_token_path(), False), (_programdata_token_path(), False)]

    failures: list[str] = []
    for path, allow_interactive_read in attempts:
        try:
            _write_bridge_token_file(path, record, allow_interactive_read=allow_interactive_read)
        except Exception as exc:
            failures.append(f"{path} -> {type(exc).__name__}: {exc}")
            _log.warning("Bridge token could not be published to %s: %s: %s",
                         path, type(exc).__name__, exc)
            continue
        _BRIDGE_TOKEN = token
        _BRIDGE_TOKEN_ACTIVE_PATH = path
        _BRIDGE_TOKEN_MODE = mode
        _log.info("Bridge token published: path=%s mode=%s pid=%d started_ms=%d "
                  "acl=protected+verified interactive_read=%s",
                  path, mode, record["pid"], record["started_ms"], allow_interactive_read)
        _log_competing_token_files(path)
        return

    _log.error(
        "Bridge token could not be published to any location, so the packet bridge is fail-closed "
        "and EVERY auth will be rejected. Attempts: %s", " | ".join(failures) or "(none)")

LOG_PATH = os.path.join(_NEXUS_DIR, "nexus_svc.log")

_WD_BASE_FILTER  = "ip and (udp or tcp)"
_WD_LAYER        = None
_WD_FLAGS        = None
_WD_RETRY_DELAY  = 5.0
_WD_OPEN_TIMEOUT = 30.0

# Game port ranges known for NBA 2K
_GAME_PORT_RANGES = [(30000, 30020), (3478, 3480), (9295, 9308), (10070, 10080)]

# ---------------------------------------------------------------------------
# Inbound meter-delay intercept parameters
# ---------------------------------------------------------------------------
# Empirically validated 2026-08-01 (docs/ADAPTIVE_DELAY_PLAN.md): delaying the
# server -> console UDP flow decouples the shot meter from the shot animation.
# The service is NOT a dumb actuator any more.  The first implementation let the
# client set the applied delay directly, and the C++ controller ramped it at
# kRampUpPerTickMs=55 every kTickMs=50 -- a slew of +1.1 ms/ms.  That is the
# defect that got the whole feature deleted on 2026-08-04 (968b127f: "holding
# packets through WinDivert stutters the game and cost a batch of tip timing").
#
# WHY A CHANGING DELAY STUTTERS AND A CONSTANT ONE DOES NOT
# ---------------------------------------------------------
# A packet arriving at t is released at t + D(t).  Two packets dt apart leave
# dt*(1 + D') apart, so the console receives the server's update stream at
#
#       rate multiplier = 1 / (1 + D')
#
# At steady state D' = 0 and the multiplier is exactly 1.0: a constant delay is
# bit-for-bit the original stream, merely time-shifted.  All the damage lives in
# the transitions.  The deleted design's numbers:
#
#       ramp up    D' = +1.10  ->  console sees  48% of normal update rate
#       ramp down  D' = -0.40  ->  console sees 167% (a catch-up burst)
#
# Half rate for ~150ms immediately before the meter renders, then a two-thirds
# surplus afterwards, once per shot.
#
# THE FIX: the client may only express INTENT (a target).  This service owns the
# applied value and slews it toward the target at a bounded rate, so no client
# bug, version mismatch or hostile caller can produce a stutter.  D' is capped
# structurally rather than by the controller's good behaviour.
_METER_ADAPTIVE_MIN_MS    = 150.0   # informational: controller's auto band floor
_METER_ADAPTIVE_MAX_MS    = 170.0   # informational: controller's auto band ceiling
_METER_MANUAL_MIN_MS      = 100.0   # informational: user override floor
# [ORION_METER_DELAY_RANGE 2026-08-08] 250/300 -> 600 in lockstep with the app's
# widened 100-600 ms slider band (AppConfigData::kMeterDelayMaxMs) and
# VENICENET_MAX_DELAY_MS.  A bridge process already running keeps the cap it
# started with; this takes effect at the next bridge start.
_METER_MANUAL_MAX_MS      = 600.0   # informational: user override ceiling
_METER_DELAY_HARD_MAX_MS  = 600.0   # hard clamp enforced by this service

# Slew ceiling.  100 ms/s == D' = 0.10 == the console sees 90.9% of the normal
# update rate while ramping, which is the same order as ordinary network jitter
# and an order of magnitude gentler than the 48% that got this feature pulled.
# 0 -> 165ms therefore takes ~1.65s, which is fine for a policy that engages at
# a dead ball or holds the delay all session, and is deliberately TOO SLOW for a
# per-shot ramp: the unsafe engagement policy is unreachable by construction
# rather than merely discouraged.
_METER_MAX_SLEW_MS_PER_S  = 100.0
# Token-bucket burst cap.  Without this, a client that goes quiet for 2s would
# bank a 200ms allowance and be permitted to apply it in a single step -- which
# is precisely the burst the slew limit exists to prevent.  A single slew step
# can never exceed _METER_MAX_SLEW_MS_PER_S * this.
_METER_SLEW_MAX_ACCUM_S   = 0.100

# Only the court server's game-port range is eligible for delay.  Matches the
# range delay_test.py used to produce the empirical numbers.
_METER_PORT_MIN = 30000
_METER_PORT_MAX = 30020

# Bug C: the reference design had an unbounded buffer.  At 165ms and a normal
# ~60-250pps game flow the steady-state depth is ~10-40 packets; 512 is >10x
# headroom while capping worst-case memory at roughly 512 * 1.5KB.
_METER_BUFFER_MAX_PACKETS = 512

# Bug B: set_delay(0) used to burst the whole queue into the NIC in one shot.
# Instead the backlog is re-timed and drained in arrival order with a minimum
# inter-packet spacing so the console sees a paced catch-up, not a microburst.
_METER_DRAIN_MIN_SPACING_S = 0.001

# Bug E: measured on this rig (CPython 3.12.10, Win11 26200):
#   time.sleep(0.0005)      -> mean 1.04ms  p95 1.51ms  max 2.42ms
#   threading.Event.wait()  -> mean 12.5ms  p95 16.6ms  max 18.4ms  (15.6ms tick)
#   threading.Condition.wait-> mean 12.3ms  p95 16.6ms  max 18.2ms
# CPython 3.11+ backs time.sleep() with a high-resolution waitable timer, but
# lock/Event/Condition timed waits still round to the legacy 15.6ms scheduler
# tick.  A condition variable is therefore ~15x WORSE here, so the flush thread
# uses time.sleep() with a deadline-aware coarse/fine split: it sleeps close to
# the next release deadline (near-zero wakeups while idle) and only switches to
# the ~1ms fine tick in the last few milliseconds.  This is a real kernel wait,
# not a spin: idle cost is ~200 wakeups/s.
_METER_FLUSH_FINE_TICK_S = 0.0005   # requested; ~1.0ms actual
_METER_FLUSH_SLACK_S     = 0.0010   # wake this early, then fine-tick in
_METER_FLUSH_COARSE_MAX_S = 0.005   # cap so a set_delay() cut is honoured fast
_METER_FLUSH_IDLE_S      = 0.005    # empty buffer -> ~200Hz poll

# Bug D: dead-man switch.  A stuck delay is a broken console, so a non-zero
# delay must be continuously re-asserted by the controller.  MeterDelayController
# re-emits its commanded value every kKeepaliveMs = 150ms (MeterDelayController.h)
# on top of the 50ms ramp ticks, so 500ms is >3 missed keepalives.  DO NOT raise
# the keepalive above this timeout: a Locked shot holds a constant delay for the
# whole 900ms dwell, and change-only signalling starves this watchdog.
_METER_WATCHDOG_TIMEOUT_S = 0.5
_METER_WATCHDOG_TICK_S    = 0.05

# ---------------------------------------------------------------------------
# Arm gate
# ---------------------------------------------------------------------------
# The intercept ships DISARMED.  Nothing about this feature exists at runtime --
# no buffer object, no verbs, no advertised feature flag -- unless it is armed
# explicitly and out of band.  This is deliberately NOT plumbed through the
# launcher: arming is an operator action, not a setting a user can trip into.
#
# Two equivalent switches, both default-off:
#     python nexus_svc.py debug --arm-meter-delay
#     set ORION_METER_DELAY_ARMED=1
#
# When disarmed the verbs answer "meter_delay_disarmed" rather than going quiet.
# A feature that silently does nothing is the trap this codebase keeps falling
# into (manual_meter_delay_ms was a no-op for months and nobody could tell), so
# the disarmed path is LOUD: it logs once at startup and errors per command.
_METER_ARM_ENV  = "ORION_METER_DELAY_ARMED"
_METER_ARM_FLAG = "--arm-meter-delay"
# Default is DISARMED (silent-no-op-prevention documented above). Opt-in via
# ORION_METER_DELAY_ARMED=1 at process start arms the module-scope flag so
# callers that don't go through _run_service() (tests, embedders, dev
# harnesses) still see the intended state.
_METER_ARMED    = str(os.environ.get(_METER_ARM_ENV, "")).strip().lower() in ("1", "true", "yes", "on")


def _meter_arm_requested(argv: list[str] | None = None) -> tuple[bool, str]:
    """Return (armed, source).  Pure function of argv + environ, for testing."""
    args = list(sys.argv if argv is None else argv)
    if _METER_ARM_FLAG in args:
        return True, "cli"
    raw = str(os.environ.get(_METER_ARM_ENV, "")).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True, "env"
    return False, ""


def _is_service_run_invocation(argv: list[str] | None = None) -> bool:
    """True when argv carries no operator command verb (arm flags aside).

    The SCM launches the compiled service exe via its registered ImagePath, which
    has no verb and -- when the installer armed the bridge -- carries
    ``--arm-meter-delay`` baked into binPath. That invocation must run the SCM
    control dispatcher, NOT the install/start/remove command parser, or the
    service dies at start with error 1053.  A bare ``nexus_svc.py`` with no args
    (the historical dev launch) is the same case and keeps its old behaviour.

    Pure function of argv so the routing is unit-testable without a live SCM.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    return not [a for a in args if a != _METER_ARM_FLAG]


def _windivert_driver_running() -> bool | None:
    """True/False if the WinDivert kernel service is running, None if unknown.

    Checked through pydivert's own service helper when available so the answer
    matches what the library will do; ``sc.exe`` is the fallback because the
    helper is private API and has moved between releases.
    """
    try:
        from pydivert import util as _pd_util  # type: ignore[attr-defined]
        svc = getattr(_pd_util, "service", None)
        if svc is not None and hasattr(svc, "is_running"):
            return bool(svc.is_running())
    except Exception:
        pass
    try:
        import subprocess
        out = subprocess.run(["sc.exe", "query", "WinDivert"],
                             capture_output=True, text=True, timeout=5.0)
        if "1060" in (out.stdout or "") + (out.stderr or ""):
            return False  # not installed at all
        return "RUNNING" in (out.stdout or "").upper()
    except Exception:
        return None


def _unload_windivert_driver(reason: str) -> bool:
    """Stop the WinDivert kernel driver.  Returns True if it is gone afterwards.

    WHY THIS EXISTS: closing a handle does not unload the driver.  Verified on
    this rig 2026-08-06 -- ``sc query WinDivert`` reported STATE: RUNNING with no
    Orion process alive, left resident by an earlier session.  Shipping a
    consumer product that leaves a kernel driver loaded after exit is not
    acceptable, and it was one of the two stated reasons this feature was pulled.

    MUST be called only after EVERY handle is closed (both the sniff handle and
    the intercept handle): pydivert's unregister() only *requests* a stop, and
    the SCM will refuse it while a handle is open.  The result is verified rather
    than assumed, and a refusal is logged loudly instead of being swallowed.
    """
    if not _PYDIVERT_OK:
        return True
    before = _windivert_driver_running()
    if before is False:
        return True
    try:
        pydivert.WinDivert.unregister()
    except Exception as exc:
        _log.warning("WinDivert unregister failed (%s): %s", reason, exc)
    # The stop is asynchronous; give the SCM a moment before believing it.
    deadline = time.perf_counter() + 3.0
    while time.perf_counter() < deadline:
        state = _windivert_driver_running()
        if state is False:
            _log.info("WinDivert driver unloaded (%s)", reason)
            return True
        if state is None:
            break
        time.sleep(0.1)
    # Still resident. WHY matters: "a handle is open" and "we lack the privilege
    # to stop a kernel service" need completely different fixes, and guessing
    # sends whoever reads this log down the wrong one. Ask the SCM directly.
    _log.error("WinDivert driver STILL RESIDENT after unload attempt (%s): %s",
               reason, _describe_driver_stop_failure())
    return False


def _describe_driver_stop_failure() -> str:
    """Best-effort, actionable reason the driver would not stop."""
    try:
        import subprocess
        out = subprocess.run(["sc.exe", "stop", "WinDivert"],
                             capture_output=True, text=True, timeout=5.0)
        blob = ((out.stdout or "") + (out.stderr or "")).strip()
    except Exception as exc:
        return f"could not query the SCM ({exc}). Manual recovery: sc stop WinDivert"

    if "FAILED 5" in blob or "Access is denied" in blob:
        # The shipping path runs as LocalSystem and has this privilege; an
        # unelevated debug-mode bridge does not. This is the expected dev-mode
        # outcome and is NOT evidence of a leaked handle.
        return ("access denied (SCM error 5) -- stopping a kernel driver needs "
                "elevation. Service mode runs as LocalSystem and can do this; an "
                "unelevated debug-mode bridge cannot. Manual recovery: run "
                "'sc stop WinDivert' from an elevated prompt.")
    if "1051" in blob or "1056" in blob or "dependent" in blob.lower():
        return ("another service or process still depends on the driver. "
                "Manual recovery: close every WinDivert user, then sc stop WinDivert")
    if "1060" in blob:
        return "the service is not installed (nothing to unload)"
    return (f"the SCM refused the stop, most likely because a handle is still "
            f"open. SCM said: {blob!r}")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("nexus_svc")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    try:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        pass
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger

_log = _setup_logging()


# ---------------------------------------------------------------------------
# Broadcast hub
# ---------------------------------------------------------------------------
class _BroadcastHub:
    MAX_QUEUE = 512

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._queues: list[tuple[queue.Queue, threading.Event]] = []

    def add_client(self, auth_ready: threading.Event) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.MAX_QUEUE)
        with self._lock:
            self._queues.append((q, auth_ready))
        return q

    def remove_client(self, q: queue.Queue) -> None:
        with self._lock:
            self._queues = [(entry_q, auth_ready)
                            for entry_q, auth_ready in self._queues
                            if entry_q is not q]

    def authed_client_count(self) -> int:
        """Number of currently connected clients that completed ``auth``.

        The meter-delay dead-man switch keys off this: when it reaches zero the
        console must be handed back at zero delay immediately.
        """
        with self._lock:
            return sum(1 for _q, auth_ready in self._queues if auth_ready.is_set())

    def broadcast(self, msg: dict) -> None:
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            for q, auth_ready in self._queues:
                # Capture events are privileged data. Before this gate an
                # unauthenticated loopback client could receive every packet
                # broadcast even though its commands were rejected.
                if not auth_ready.is_set():
                    continue
                if q.full():
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    q.put_nowait(line)
                except queue.Full:
                    pass


# ---------------------------------------------------------------------------
# Inbound meter-delay intercept
# ---------------------------------------------------------------------------
def _build_intercept_filter(console_ip: str, court_ip: str) -> str:
    """Return the WinDivert filter for the inbound (server -> console) game flow.

    Derivation — every clause is load-bearing:

    ``ip and udp``
        The game flow is IPv4 UDP.  This alone already excludes the RTT probe
        path, which is ICMP (ping3) or a TCP:80 connect (rtt_sync_engine.py
        :137-160).

    ``ip.SrcAddr == {court_ip}``
        Pins the source to the public 2K court server.  ``set_court_ip``
        validates with ``require_public=True``, so this address can never be a
        LAN address — which is what keeps Remote Play out: Remote Play is
        console-LAN-IP <-> PC-LAN-IP and can never match a public source.

    ``ip.DstAddr == {console_ip}``
        Restricts to the *inbound* direction (server -> console).  Note the
        ``inbound``/``outbound`` filter keywords are NOT usable here: at the
        NETWORK_FORWARD layer WinDivert reports every routed packet as
        outbound, so direction has to be expressed as an address pair.

    ``udp.SrcPort/DstPort in [30000, 30020]``
        The NBA 2K gameplay port range, and the exact predicate delay_test.py
        used to produce the empirical 165ms result.  Src-or-Dst is used (rather
        than SrcPort only) so the flow is still matched if the server answers
        from an ephemeral port; the SrcAddr clause already guarantees the peer
        is the court server, so widening the port test costs no selectivity.

    Two further containments are structural rather than textual:
      * The handle is opened at NETWORK_FORWARD, which only sees *routed*
        traffic.  Anything originated or terminated by this PC — including
        every RTT probe and the whole Remote Play session — traverses the
        NETWORK layer and is never presented to this handle at all.
      * This is a second, independent handle.  ``_WinDivertLoop`` force-closes
        its SNIFF handle to trigger a filter restart; that must never disturb
        the intercept, so the two share no handle and no lock.
    """
    return (
        "ip and udp"
        f" and ip.SrcAddr == {court_ip}"
        f" and ip.DstAddr == {console_ip}"
        f" and ((udp.SrcPort >= {_METER_PORT_MIN} and udp.SrcPort <= {_METER_PORT_MAX})"
        f" or (udp.DstPort >= {_METER_PORT_MIN} and udp.DstPort <= {_METER_PORT_MAX}))"
    )


def _open_intercept_handle(filter_str: str):
    """Open an *intercepting* (non-SNIFF) NETWORK_FORWARD handle.

    Split out as a module-level seam so tests can substitute a fake handle
    without the WinDivert driver.
    """
    if not _PYDIVERT_OK:
        raise RuntimeError("pydivert not available")
    ok, _, err_msg = pydivert.WinDivert.check_filter(filter_str)
    if not ok:
        raise RuntimeError(f"bad filter: {err_msg}")
    # No Flag.SNIFF: packets are removed from the stack and only reach the
    # console when we re-inject them.
    handle = pydivert.WinDivert(filter_str, layer=pydivert.Layer.NETWORK_FORWARD)
    handle.open()
    return handle


class _InboundDelayBuffer:
    """Holds inbound game packets (court server -> console) and re-injects late.

    Ordering contract
    -----------------
    Packets leave in exactly the order they arrived, unconditionally.  Two
    mechanisms enforce it:

    1. ``release_at`` is kept monotonically non-decreasing across the buffer, so
       the flush pass can release a strict prefix.
    2. The zero-delay fast path only fires when the buffer is *provably empty*
       under the same lock that guards the sends, so a fresh packet can never
       overtake an older buffered one (bug A in the reference design).

    Slew contract  (NEW -- this is the anti-stutter guarantee)
    ---------------------------------------------------------
    ``set_delay()`` sets a TARGET.  It never changes the applied delay.  The
    flush thread advances the applied delay toward the target at no more than
    ``_METER_MAX_SLEW_MS_PER_S``, with a bounded burst allowance, so the packet
    rate multiplier 1/(1+D') stays within ~9% of unity no matter what the client
    commands.  A client that jumps 0 -> 165 in one command gets a smooth 1.65s
    ramp, not a 48%-rate cliff.

    The ONLY paths that may move the applied delay instantly are the safety
    paths (``stop``, ``dead_man_stop``, watchdog starvation), because a stuck
    delay outranks a momentary catch-up burst -- and even then the backlog is
    re-paced by ``_reschedule_locked`` rather than bursted onto the wire.

    Safety contract
    ---------------
    A stuck non-zero delay is a broken console.  Two independent fail-safes:

    * dead-man: the last authenticated client disconnecting stops the intercept
      outright (flush + close handle), so a crashed Orion cannot leave delay on;
    * watchdog: a non-zero delay that is not re-asserted by ``set_delay`` within
      ``_METER_WATCHDOG_TIMEOUT_S`` is forced back to zero.

    Locking
    -------
    A single lock guards the buffer, the delay values *and* the handle sends.
    Serialising sends under the same lock is what makes the ordering contract
    provable; ``handle.recv()`` is the only handle call made outside it.
    """

    def __init__(self, hub: "_BroadcastHub | None" = None,
                 slew_ms_per_s: float = _METER_MAX_SLEW_MS_PER_S,
                 clock=None) -> None:
        self._hub = hub
        # Injectable monotonic clock. Defaults to the real one; the bench
        # harness substitutes a virtual clock so the packet-rate property can
        # be proven exactly instead of estimated through scheduler jitter.
        # Mirrors MeterDelayController::setMonotonicClockForTesting.
        self._clock = clock if clock is not None else time.perf_counter
        self._lock = threading.RLock()
        # entries are (release_at, arrival_at, packet), release_at monotonic
        self._buffer: collections.deque = collections.deque()
        # Applied vs commanded.  _delay_ms is what packets actually experience;
        # _target_ms is what the client asked for.  They converge at <= slew.
        self._delay_ms = 0.0
        self._target_ms = 0.0
        self._slew_ms_per_s = max(0.0, float(slew_ms_per_s))
        self._last_slew_ts = 0.0
        self._handle = None
        self._running = False
        self._stop = threading.Event()
        self._stop.set()
        self._threads: list[threading.Thread] = []
        self._console_ip = ""
        self._court_ip = ""
        self._filter = ""
        # Guards the un-locked WinDivert open in start(): two clients racing
        # start_meter_intercept used to open two intercepting handles and orphan
        # one of them.  An orphaned non-SNIFF NETWORK_FORWARD handle keeps
        # removing the console's game packets from the stack with nobody left to
        # re-inject them, i.e. it blackholes the whole game flow.
        self._starting = False
        self._last_cmd_ts = 0.0
        # Plain mirrors so the high-rate telemetry path can read state without
        # contending with the ~200Hz flush loop.
        self._depth = 0
        self._log_throttle_ts = 0.0
        self._stats = {
            "intercepted": 0,
            "passed": 0,
            "delayed": 0,
            "sent": 0,
            "overflow_released": 0,
            "send_errors": 0,
            "watchdog_trips": 0,
            "dead_man_trips": 0,
            "slew_limited_steps": 0,
            "emergency_zero": 0,
        }
        # Largest |D'| actually applied this session, for the bench harness to
        # assert against.  A run that never exceeds the cap proves the limiter.
        self._peak_slew_ms_per_s = 0.0

    def _now(self) -> float:
        return self._clock()

    # ------------------------------------------------------------------ #
    # Introspection
    # ------------------------------------------------------------------ #
    @property
    def active(self) -> bool:
        return self._running

    @property
    def current_delay_ms(self) -> float:
        return self._delay_ms

    @property
    def target_delay_ms(self) -> float:
        return self._target_ms

    @property
    def buffer_depth(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def peak_slew_ms_per_s(self) -> float:
        return self._peak_slew_ms_per_s

    def snapshot(self) -> dict:
        """Lock-free status read for the per-packet telemetry path."""
        return {
            "meter_delay_active": bool(self._running),
            "meter_delay_ms": round(float(self._delay_ms), 3),
            "meter_delay_target_ms": round(float(self._target_ms), 3),
            "meter_delay_settled": abs(self._delay_ms - self._target_ms) <= 0.5,
            "meter_buffer_depth": int(self._depth),
        }

    def stats(self) -> dict:
        with self._lock:
            out = dict(self._stats)
        out["peak_slew_ms_per_s"] = round(self._peak_slew_ms_per_s, 3)
        out["slew_cap_ms_per_s"] = self._slew_ms_per_s
        return out

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self, console_ip: str, court_ip: str) -> tuple[bool, str]:
        console = _validated_ipv4(console_ip)
        court = _validated_ipv4(court_ip, require_public=True)
        if console is None or court is None:
            return False, "intercept_requires_ips"
        with self._lock:
            if self._running:
                if (console, court) == (self._console_ip, self._court_ip):
                    return True, ""
                return False, "intercept_already_running"
            if self._starting:
                # Another thread is already inside _open_intercept_handle for
                # this buffer.  Opening a second one would orphan a handle.
                return False, "intercept_already_running"
            self._starting = True
        filter_str = _build_intercept_filter(console, court)
        try:
            handle = _open_intercept_handle(filter_str)
        except Exception as exc:
            with self._lock:
                self._starting = False
            _log.error("meter intercept open failed: %s", exc)
            return False, f"intercept_open_failed: {exc}"

        with self._lock:
            self._starting = False
            self._handle = handle
            self._console_ip = console
            self._court_ip = court
            self._filter = filter_str
            self._buffer.clear()
            self._depth = 0
            # Always come up at zero delay: a stale non-zero value must never
            # be applied implicitly by opening the handle.
            self._delay_ms = 0.0
            self._target_ms = 0.0
            self._last_slew_ts = self._now()
            self._last_cmd_ts = self._last_slew_ts
            self._peak_slew_ms_per_s = 0.0
            self._running = True
            self._stop = threading.Event()
            stop_evt = self._stop
            self._threads = [
                threading.Thread(target=self._capture_loop, args=(stop_evt,),
                                 daemon=True, name="meter-capture"),
                threading.Thread(target=self._flush_loop, args=(stop_evt,),
                                 daemon=True, name="meter-flush"),
                threading.Thread(target=self._watchdog_loop, args=(stop_evt,),
                                 daemon=True, name="meter-watchdog"),
            ]
            threads = list(self._threads)
        for t in threads:
            t.start()
        _log.info("meter intercept opened, filter: %s", filter_str)
        self._broadcast_state("started", "client_request")
        return True, ""

    def stop(self, reason: str = "client_request") -> bool:
        """Flush every buffered packet in order, then close the handle."""
        with self._lock:
            if not self._running:
                return False
            self._running = False
            handle = self._handle
            threads = list(self._threads)
            self._threads = []
            stop_evt = self._stop
        stop_evt.set()

        current = threading.current_thread()
        # Let the flush thread retire first so nothing double-sends while the
        # final drain runs.
        for t in threads:
            if t is not current and t.name == "meter-flush":
                t.join(timeout=1.0)

        with self._lock:
            pending = list(self._buffer)
            self._buffer.clear()
            self._depth = 0
            self._delay_ms = 0.0
            self._target_ms = 0.0
            for _release_at, _arrival_at, pkt in pending:
                self._send_locked(pkt)
            self._handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        # Closing the handle is what unblocks the capture thread's recv().
        for t in threads:
            if t is not current:
                t.join(timeout=1.0)
        _log.info("meter intercept stopped (%s), flushed %d buffered packet(s)",
                  reason, len(pending))
        self._broadcast_state("stopped", reason)
        return True

    def dead_man_stop(self, reason: str) -> bool:
        """Bug D: hand the console back unconditionally.

        Called when the last authenticated client goes away.  Orion crashing or
        NetworkBridge dropping into its 3s->30s reconnect backoff must not leave
        the console sitting behind a 165ms delay with no UI to turn it off.
        """
        with self._lock:
            if not self._running:
                return False
            self._stats["dead_man_trips"] += 1
        _log.warning("meter intercept dead-man switch: %s -> forcing delay 0", reason)
        self.force_zero("dead_man")
        return self.stop(reason)

    def retarget(self, console_ip: str, court_ip: str) -> None:
        """Re-open the intercept if the console/court address pair changed.

        Deliberately *not* wired into ``_WinDivertLoop.set_console_ip``: that
        method force-closes the SNIFF handle, and the intercept handle must
        survive a sniff filter restart untouched (bug F).  Only an actual
        address change, observed at the command layer, re-opens this handle.
        """
        console = _validated_ipv4(console_ip)
        court = _validated_ipv4(court_ip, require_public=True)
        with self._lock:
            if not self._running:
                return
            if console is not None and court is not None and \
                    (console, court) == (self._console_ip, self._court_ip):
                return
        self.stop("ip_changed")
        if console is not None and court is not None:
            self.start(console, court)

    # ------------------------------------------------------------------ #
    # Delay control
    # ------------------------------------------------------------------ #
    def set_delay(self, delay_ms, *, touch: bool = True) -> float:
        """Set the delay TARGET.  Returns the clamped target actually accepted.

        This does NOT change the applied delay -- ``advance_slew()`` does, at a
        bounded rate.  Callers that need the applied value should read
        ``current_delay_ms`` or ``snapshot()``.

        Raises ``ValueError`` for non-numeric / non-finite input.  The value is
        clamped to the hard [0, 600]ms safety range; the 150-170 / 100-600 bands
        are the controller's business, and intermediate ramp values (55, 110...)
        must pass through untouched.
        """
        try:
            value = float(delay_ms)
        except (TypeError, ValueError):
            raise ValueError("invalid_delay_ms")
        if not math.isfinite(value):
            raise ValueError("invalid_delay_ms")
        value = max(0.0, min(_METER_DELAY_HARD_MAX_MS, value))

        now = self._now()
        with self._lock:
            if touch:
                self._last_cmd_ts = now
            self._target_ms = value
            if self._slew_ms_per_s <= 0.0:
                # Limiter disabled: target and applied are the same thing, so
                # apply synchronously. Used by the ordering/safety tests, which
                # drive enqueue() directly without a flush thread running. The
                # shipping path always has a positive cap.
                self._advance_slew_locked(now)
        return value

    def force_zero(self, reason: str) -> None:
        """Safety-only instant zero.  Bypasses the slew limiter.

        A stuck delay outranks a momentary catch-up burst, so this is the one
        path allowed to violate the slew contract.  The backlog is still re-paced
        by ``_reschedule_locked`` rather than bursted straight onto the wire.
        """
        now = self._now()
        with self._lock:
            if self._delay_ms <= 0.0 and self._target_ms <= 0.0:
                return
            self._stats["emergency_zero"] += 1
            self._target_ms = 0.0
            self._delay_ms = 0.0
            self._last_slew_ts = now
            self._reschedule_locked(now, 0.0)
        _log.warning("meter delay forced to 0 (%s)", reason)

    def advance_slew(self, now: float | None = None) -> float:
        """Move the applied delay toward the target at <= the slew cap.

        Returns the applied delay.  Called from the flush loop at ~200Hz, so the
        step is ~0.5ms at the default 100 ms/s cap -- far finer than the ~4-16ms
        inter-packet spacing of the game flow, i.e. the ramp is invisible at
        packet granularity.
        """
        if now is None:
            now = self._now()
        with self._lock:
            return self._advance_slew_locked(now)

    def _advance_slew_locked(self, now: float) -> float:
        delta = self._target_ms - self._delay_ms
        if abs(delta) <= 1e-9:
            # SNAP, do not merely return.  perf_counter values are ~1e4, so a
            # 5ms step carries ~5e-13 of float error and the accumulated applied
            # delay lands a hair short of target and stays there.  Left un-snapped
            # on the way DOWN that residue is permanent and poisonous: _delay_ms
            # stays fractionally above 0, so enqueue()'s zero-delay fast path
            # (delay_s <= 0.0) never fires again and every packet is buffered and
            # rescheduled for the rest of the session, after the delay has
            # nominally been off for minutes.
            self._delay_ms = self._target_ms
            self._last_slew_ts = now
            return self._delay_ms

        if self._slew_ms_per_s <= 0.0:
            # Limiter disabled (tests only). Apply instantly.
            previous = self._delay_ms
            self._delay_ms = self._target_ms
            self._last_slew_ts = now
            if self._delay_ms < previous:
                self._reschedule_locked(now, self._delay_ms / 1000.0)
            return self._delay_ms

        elapsed = now - self._last_slew_ts
        if elapsed <= 0.0:
            return self._delay_ms
        # Token bucket: a long quiet period must not bank an unbounded step.
        elapsed = min(elapsed, _METER_SLEW_MAX_ACCUM_S)
        max_step = self._slew_ms_per_s * elapsed

        previous = self._delay_ms
        if abs(delta) > max_step:
            self._stats["slew_limited_steps"] += 1
            self._delay_ms = previous + math.copysign(max_step, delta)
        else:
            self._delay_ms = self._target_ms
        self._last_slew_ts = now

        applied = abs(self._delay_ms - previous)
        if elapsed > 0.0:
            self._peak_slew_ms_per_s = max(self._peak_slew_ms_per_s,
                                           applied / elapsed)
        if self._delay_ms < previous:
            self._reschedule_locked(now, self._delay_ms / 1000.0)
        return self._delay_ms

    def _reschedule_locked(self, now: float, delay_s: float) -> None:
        if not self._buffer:
            return
        rescheduled = collections.deque()
        prev_t = now - _METER_DRAIN_MIN_SPACING_S
        for release_at, arrival_at, pkt in self._buffer:
            t = min(release_at, arrival_at + delay_s)
            t = max(t, prev_t + _METER_DRAIN_MIN_SPACING_S)
            # Never push a packet later than it was already scheduled for, and
            # never let an overdue entry drag the pacing anchor into the past.
            t = min(t, max(release_at, now))
            prev_t = t
            rescheduled.append((t, arrival_at, pkt))
        self._buffer = rescheduled

    # ------------------------------------------------------------------ #
    # Packet path
    # ------------------------------------------------------------------ #
    def _send_locked(self, pkt) -> bool:
        handle = self._handle
        if handle is None:
            return False
        try:
            handle.send(pkt)
        except Exception as exc:
            self._stats["send_errors"] += 1
            self._throttled_warn("meter intercept send failed: %s", exc)
            return False
        self._stats["sent"] += 1
        return True

    def _throttled_warn(self, fmt: str, *args) -> None:
        now = self._now()
        if now - self._log_throttle_ts < 1.0:
            return
        self._log_throttle_ts = now
        _log.warning(fmt, *args)

    def enqueue(self, pkt) -> None:
        now = self._now()
        with self._lock:
            self._stats["intercepted"] += 1
            delay_s = self._delay_ms / 1000.0
            if delay_s <= 0.0 and not self._buffer:
                # Bug A: the fast path is only safe when the buffer is provably
                # empty *under the same lock that serialises sends*.  Otherwise
                # this packet would overtake older still-buffered ones during a
                # ramp-down or at the instant the delay reaches zero.
                self._stats["passed"] += 1
                self._send_locked(pkt)
                return

            release_at = now + delay_s
            if self._buffer:
                # Keep release times monotonic so FIFO order is structural.
                release_at = max(release_at, self._buffer[-1][0])

            if len(self._buffer) >= _METER_BUFFER_MAX_PACKETS:
                # Bug C overflow policy: release the OLDEST packet early rather
                # than dropping anything.  Dropping loses game state; releasing
                # early only shortens the effective delay, and taking from the
                # head preserves ordering.
                over = len(self._buffer) - _METER_BUFFER_MAX_PACKETS + 1
                for _ in range(over):
                    _r, _a, old_pkt = self._buffer.popleft()
                    self._stats["overflow_released"] += 1
                    self._send_locked(old_pkt)
                self._throttled_warn(
                    "meter buffer cap %d reached at delay %.1fms — "
                    "early-released %d oldest packet(s)",
                    _METER_BUFFER_MAX_PACKETS, self._delay_ms, over)

            self._buffer.append((release_at, now, pkt))
            self._depth = len(self._buffer)
            self._stats["delayed"] += 1

    def flush_due(self, now: float | None = None) -> int:
        """Release every packet whose deadline has passed, in order."""
        if now is None:
            now = self._now()
        with self._lock:
            sent = 0
            while self._buffer and self._buffer[0][0] <= now:
                _release_at, _arrival_at, pkt = self._buffer.popleft()
                self._send_locked(pkt)
                sent += 1
            self._depth = len(self._buffer)
            return sent

    def next_release_at(self) -> float | None:
        with self._lock:
            return self._buffer[0][0] if self._buffer else None

    # ------------------------------------------------------------------ #
    # Threads
    # ------------------------------------------------------------------ #
    def _capture_loop(self, stop_evt: threading.Event) -> None:
        while not stop_evt.is_set():
            handle = self._handle
            if handle is None:
                break
            try:
                pkt = handle.recv()
            except Exception:
                if stop_evt.is_set():
                    break
                # A closed/errored handle here means the intercept is over; the
                # console must not be left holding a buffer.
                _log.warning("meter intercept recv failed — stopping intercept")
                threading.Thread(target=self.stop, args=("recv_error",),
                                 daemon=True).start()
                break
            if pkt is None:
                continue
            if stop_evt.is_set():
                # Teardown is in progress but stop()'s final drain may not have
                # run yet.  Sending straight out would put this packet AHEAD of
                # everything still buffered, so park it on the tail instead and
                # let the drain release it last.
                with self._lock:
                    if self._buffer:
                        self._buffer.append((self._buffer[-1][0],
                                             self._now(), pkt))
                        self._depth = len(self._buffer)
                    else:
                        self._send_locked(pkt)
                break
            self.enqueue(pkt)

    def _flush_loop(self, stop_evt: threading.Event) -> None:
        while not stop_evt.is_set():
            # The slew advance MUST run before the flush: it is what makes the
            # applied delay a smooth function of wall time rather than of client
            # command cadence, and flush_due() reads the schedule it rewrites.
            self.advance_slew()
            self.flush_due()
            nxt = self.next_release_at()
            if nxt is None:
                time.sleep(_METER_FLUSH_IDLE_S)
                continue
            remaining = nxt - self._now()
            if remaining <= 0.0:
                continue
            if remaining > _METER_FLUSH_SLACK_S + _METER_FLUSH_FINE_TICK_S:
                # Coarse approach: sleep most of the way there, capped so that a
                # set_delay() cut and the slew step are picked up promptly.
                time.sleep(min(remaining - _METER_FLUSH_SLACK_S,
                               _METER_FLUSH_COARSE_MAX_S))
            else:
                time.sleep(_METER_FLUSH_FINE_TICK_S)

    def _watchdog_loop(self, stop_evt: threading.Event) -> None:
        """Bug D: force delay to zero when set_meter_delay stops arriving."""
        while not stop_evt.wait(_METER_WATCHDOG_TICK_S):
            starved = self.watchdog_check()
            if starved:
                _log.warning(
                    "meter delay watchdog: no set_meter_delay for >%.0fms — "
                    "forcing delay 0", _METER_WATCHDOG_TIMEOUT_S * 1000.0)

    def watchdog_check(self, now: float | None = None) -> bool:
        """One watchdog evaluation.  Returns True if it had to force zero."""
        if now is None:
            now = self._now()
        with self._lock:
            if not self._running:
                return False
            # Either an applied delay or a standing non-zero target counts as
            # armed: a target that has not yet slewed in is still a commitment
            # to hold packets, and a dead controller must not leave it standing.
            if self._delay_ms <= 0.0 and self._target_ms <= 0.0:
                return False
            if (now - self._last_cmd_ts) <= _METER_WATCHDOG_TIMEOUT_S:
                return False
            self._stats["watchdog_trips"] += 1
        self.force_zero("command_starvation")
        self._broadcast_state("watchdog_zero", "command_starvation")
        return True

    # ------------------------------------------------------------------ #
    def _broadcast_state(self, state: str, reason: str) -> None:
        if self._hub is None:
            return
        msg = {"event": "meter_delay", "state": state, "reason": reason}
        msg.update(self.snapshot())
        if self._filter:
            msg["filter"] = self._filter
        try:
            self._hub.broadcast(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WinDivert capture + kill loop
# ---------------------------------------------------------------------------
class _WinDivertLoop:
    def __init__(self, hub: _BroadcastHub, stop_evt: threading.Event) -> None:
        self._hub         = hub
        self._stop_evt    = stop_evt
        self._console_ip  = ""
        self._court_ip    = ""
        self._ip_lock     = threading.Lock()
        self._restart_evt = threading.Event()
        self._sniff_handle = None
        self._sniff_lock  = threading.Lock()
        self._observer    = None  # Optional[Callable[[str src, str dst], None]]
        self._meter_status = None  # Optional[Callable[[], dict]]

    def set_packet_observer(self, fn) -> None:
        """Register an optional per-packet (src_ip, dst_ip) observer callback."""
        self._observer = fn

    def set_meter_status_provider(self, fn) -> None:
        """Register a lock-free provider of meter-delay telemetry fields."""
        self._meter_status = fn

    # ------------------------------------------------------------------ #
    def set_console_ip(self, ip: str) -> None:
        with self._ip_lock:
            if ip == self._console_ip:
                return
            self._console_ip = ip
        self._restart_evt.set()
        with self._sniff_lock:
            h = self._sniff_handle
            if h is not None:
                try:
                    h.close()
                except Exception:
                    pass
                self._sniff_handle = None

    def clear_console_ip(self) -> None:
        self.set_console_ip("")

    def stop(self) -> None:
        """Interrupt a blocking WinDivert receive during service shutdown."""
        self._stop_evt.set()
        self._restart_evt.set()
        with self._sniff_lock:
            handle = self._sniff_handle
            self._sniff_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass

    def set_court_ip(self, ip: str) -> None:
        with self._ip_lock:
            self._court_ip = ip

    def _current_ips(self) -> tuple[str, str]:
        with self._ip_lock:
            return self._console_ip, self._court_ip

    def current_ips(self) -> tuple[str, str]:
        """Public accessor: (console_ip, court_ip) as currently configured."""
        return self._current_ips()

    # ------------------------------------------------------------------ #
    # Sniff filter
    # ------------------------------------------------------------------ #
    def _build_filter(self) -> str:
        console_ip, _ = self._current_ips()
        if console_ip:
            return (
                f"{_WD_BASE_FILTER} and "
                f"(ip.SrcAddr == {console_ip} or ip.DstAddr == {console_ip})"
            )
        return _WD_BASE_FILTER

    # ------------------------------------------------------------------ #
    # Main capture loop
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        if not _PYDIVERT_OK:
            _log.error("pydivert not available: %s",
                       _PYDIVERT_ERR if "_PYDIVERT_ERR" in globals() else "unknown")
            self._hub.broadcast({"event": "error", "msg": "pydivert not available"})
            return

        global _WD_LAYER, _WD_FLAGS
        _WD_LAYER = pydivert.Layer.NETWORK_FORWARD
        _WD_FLAGS = pydivert.Flag.SNIFF

        while not self._stop_evt.is_set():
            self._restart_evt.clear()
            filter_str = self._build_filter()
            handle = None
            try:
                ok, _, err_msg = pydivert.WinDivert.check_filter(filter_str)
                if not ok:
                    raise RuntimeError(f"bad filter: {err_msg}")

                handle = pydivert.WinDivert(filter_str, layer=_WD_LAYER, flags=_WD_FLAGS)
                handle.open()
                with self._sniff_lock:
                    self._sniff_handle = handle
                _log.info("WinDivert opened, filter: %s", filter_str)
                self._hub.broadcast({"event": "ready", "filter": filter_str})

                while not self._stop_evt.is_set() and not self._restart_evt.is_set():
                    try:
                        pkt = handle.recv()
                        self._dispatch(pkt)
                    except Exception as rx_exc:
                        if self._stop_evt.is_set() or self._restart_evt.is_set():
                            break
                        _log.warning("WinDivert recv error: %s", rx_exc)
                        break

            except Exception as exc:
                msg = str(exc).strip()
                if not self._restart_evt.is_set():
                    _log.error("WinDivert error: %s", msg)
                    self._hub.broadcast({"event": "error", "msg": msg})
                if not self._stop_evt.is_set() and not self._restart_evt.is_set():
                    self._stop_evt.wait(_WD_RETRY_DELAY)
            finally:
                with self._sniff_lock:
                    self._sniff_handle = None
                if handle is not None:
                    try:
                        handle.close()
                    except Exception:
                        pass

    def _dispatch(self, pkt) -> None:
        try:
            proto_raw = str(getattr(pkt, "protocol", "") or "").upper()
            if "UDP" in proto_raw:
                proto = "UDP"
            elif "TCP" in proto_raw:
                proto = "TCP"
            else:
                udp_obj = getattr(pkt, "udp", None)
                tcp_obj = getattr(pkt, "tcp", None)
                proto = "UDP" if udp_obj is not None else ("TCP" if tcp_obj is not None else "")
            if not proto:
                return

            src_ip   = str(getattr(pkt, "src_addr", "") or "")
            dst_ip   = str(getattr(pkt, "dst_addr", "") or "")
            src_port = int(getattr(pkt, "src_port", 0) or 0)
            dst_port = int(getattr(pkt, "dst_port", 0) or 0)
            size     = max(1, len(getattr(pkt, "raw", b"") or b""))

            if not src_ip or not dst_ip or (src_port <= 0 and dst_port <= 0):
                return

            outbound = bool(getattr(pkt, "is_outbound", False))
            if self._observer is not None:
                try:
                    self._observer(src_ip, dst_ip)
                except Exception:
                    pass
            msg = {
                "event":    "packet",
                "src_ip":   src_ip,
                "dst_ip":   dst_ip,
                "src_port": src_port,
                "dst_port": dst_port,
                "proto":    proto,
                "size":     size,
                "outbound": outbound,
                "ts":       round(time.perf_counter() * 1000.0, 3),
            }
            if self._meter_status is not None:
                try:
                    status = self._meter_status()
                except Exception:
                    status = None
                if status:
                    msg.update(status)
            self._hub.broadcast(msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-client handler
# ---------------------------------------------------------------------------
class _ClientHandler:
    def __init__(self, conn: socket.socket, addr, hub: _BroadcastHub,
                 wd_loop: _WinDivertLoop,
                 delay_buf: "_InboundDelayBuffer | None" = None) -> None:
        self._conn     = conn
        self._addr     = addr
        self._hub      = hub
        self._wd_loop  = wd_loop
        # None whenever the intercept is disarmed, which is the shipping default.
        self._delay    = delay_buf
        self._auth_ready = threading.Event()
        self._q        = hub.add_client(self._auth_ready)
        self._done     = threading.Event()
        self._authed   = False
        # Rate-limit: log the detailed auth failure once per connection, not once per retry.
        self._auth_failure_logged = False

    def run(self) -> None:
        writer = threading.Thread(target=self._writer, daemon=True)
        writer.start()
        try:
            self._send({"event": "hello", "version": SVC_VERSION,
                        "features": list(SVC_FEATURES)})
            self._reader()
        finally:
            self._done.set()
            self._hub.remove_client(self._q)
            self._on_disconnect()
            try:
                self._conn.close()
            except Exception:
                pass
            writer.join(timeout=2.0)

    def _on_disconnect(self) -> None:
        """Bug D dead-man switch.

        Runs after this client has been unregistered from the hub.  If nobody
        authenticated is left, nobody can turn the delay off any more, so the
        intercept is torn down and every buffered packet released.  Orion
        crashing, or NetworkBridge falling into its 3s->30s reconnect backoff,
        must never leave the console behind a 165ms delay with no UI.
        """
        if self._delay is None:
            return
        try:
            if self._hub.authed_client_count() > 0:
                return
            self._delay.dead_man_stop("last_client_disconnected")
        except Exception as exc:
            _log.error("dead-man switch failed: %s", exc)

    def _writer(self) -> None:
        while not self._done.is_set():
            try:
                line = self._q.get(timeout=0.2)
                self._conn.sendall(line)
            except queue.Empty:
                continue
            except Exception:
                break

    def _reader(self) -> None:
        buf = b""
        self._conn.settimeout(1.0)
        while not self._done.is_set():
            try:
                chunk = self._conn.recv(4096)
            except socket.timeout:
                continue
            except Exception:
                break
            if not chunk:
                break
            buf += chunk
            if len(buf) > _MAX_COMMAND_BUFFER_BYTES:
                self._send({"event": "error", "msg": "command_too_large"})
                return
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if len(line) > _MAX_COMMAND_LINE_BYTES:
                    self._send({"event": "error", "msg": "command_too_large"})
                    return
                try:
                    cmd = json.loads(line.decode("utf-8"))
                except Exception:
                    continue
                self._handle_cmd(cmd)

    def _handle_cmd(self, cmd: dict) -> None:
        if not isinstance(cmd, dict):
            self._send({"event": "error", "msg": "invalid_command"})
            return
        c = str(cmd.get("cmd", "")).strip().lower()
        if not self._authed:
            # F1: a valid token must arrive before any other command is honored.
            supplied_token = str(cmd.get("token", ""))
            if (c == "auth" and _BRIDGE_TOKEN
                    and hmac.compare_digest(supplied_token, _BRIDGE_TOKEN)):
                self._authed = True
                self._auth_ready.set()
                self._send({"event": "ack", "cmd": "auth"})
                return
            # "unauthorized" on its own forced whoever hit this to reverse-engineer the cause from
            # file mtimes. Say which of the three distinguishable failures happened. None of these
            # reveal the token or help an attacker guess it: the path is derived from public
            # environment variables and "did the server publish a token" is already observable.
            if c != "auth":
                reason = "auth_required"
                detail = f"client sent {c!r} before authenticating"
            elif not _BRIDGE_TOKEN:
                reason = "no_server_token"
                detail = ("this bridge never published a token (see the earlier "
                          "'Bridge token could not be published' error), so nothing can authenticate")
            else:
                reason = "token_mismatch"
                detail = (f"client token does not match the token published at "
                          f"{_BRIDGE_TOKEN_ACTIVE_PATH!r}; the client most likely read a stale "
                          f"token file from a previous session")
            if not self._auth_failure_logged:
                self._auth_failure_logged = True
                _log.error("Auth rejected for %s: %s (%s)", self._addr, reason, detail)
            self._send({"event": "error", "msg": "unauthorized", "reason": reason,
                        "token_path": _BRIDGE_TOKEN_ACTIVE_PATH,
                        "token_mode": _BRIDGE_TOKEN_MODE})
            return
        if c == "ping":
            self._send({"event": "pong"})
        elif c == "set_filter":
            ip = _validated_ipv4(cmd.get("console_ip"))
            if ip is None:
                self._send({"event": "error", "msg": "invalid_console_ip"})
                return
            self._wd_loop.set_console_ip(ip)
            self._retarget_intercept()
            self._send({"event": "ack", "cmd": "set_filter", "console_ip": ip})
        elif c == "clear_filter":
            self._wd_loop.clear_console_ip()
            self._retarget_intercept()
            self._send({"event": "ack", "cmd": "clear_filter"})
        elif c == "set_court_ip":
            ip = _validated_ipv4(cmd.get("court_ip"), require_public=True)
            if ip is None:
                self._send({"event": "error", "msg": "invalid_court_ip"})
                return
            self._wd_loop.set_court_ip(ip)
            self._retarget_intercept()
            self._send({"event": "ack", "cmd": "set_court_ip", "court_ip": ip})
        elif c == "start_meter_intercept":
            delay = self._armed_delay_or_error(c)
            if delay is None:
                return
            console_ip, court_ip = self._wd_loop.current_ips()
            ok, err = delay.start(console_ip, court_ip)
            if not ok:
                self._send({"event": "error", "msg": err,
                            "cmd": "start_meter_intercept"})
                return
            ack = {"event": "ack", "cmd": "start_meter_intercept",
                   "console_ip": console_ip, "court_ip": court_ip}
            ack.update(delay.snapshot())
            self._send(ack)
        elif c == "stop_meter_intercept":
            delay = self._armed_delay_or_error(c)
            if delay is None:
                return
            stopped = delay.stop("client_request")
            ack = {"event": "ack", "cmd": "stop_meter_intercept",
                   "was_active": bool(stopped)}
            ack.update(delay.snapshot())
            self._send(ack)
        elif c == "set_meter_delay":
            delay = self._armed_delay_or_error(c)
            if delay is None:
                return
            if not delay.active:
                # Fail loudly instead of silently arming a delay that would be
                # applied the instant the handle opens.
                self._send({"event": "error", "msg": "intercept_not_running",
                            "cmd": "set_meter_delay"})
                return
            try:
                accepted = delay.set_delay(cmd.get("delay_ms"))
            except ValueError:
                self._send({"event": "error", "msg": "invalid_delay_ms"})
                return
            ack = {"event": "ack", "cmd": "set_meter_delay"}
            ack.update(delay.snapshot())
            # `target_ms` is what was accepted; `meter_delay_ms` in the snapshot
            # is what is APPLIED right now. They differ while the service slews,
            # and a client that conflates them will misreport the condition key.
            ack["target_ms"] = accepted
            ack["slew_cap_ms_per_s"] = _METER_MAX_SLEW_MS_PER_S
            self._send(ack)
        elif c == "meter_delay_stats":
            # Bench/diagnostic verb: exposes the counters the harness asserts on
            # (peak applied slew, limiter trips, overflow releases).
            delay = self._armed_delay_or_error(c)
            if delay is None:
                return
            ack = {"event": "ack", "cmd": "meter_delay_stats"}
            ack.update(delay.snapshot())
            ack["stats"] = delay.stats()
            self._send(ack)
        elif c == "status":
            status = {
                "event":      "status",
                "version":    SVC_VERSION,
                "features":   list(SVC_FEATURES),
                "pydivert_ok": _PYDIVERT_OK,
                "meter_delay_armed": bool(_METER_ARMED),
            }
            if self._delay is not None:
                status.update(self._delay.snapshot())
            self._send(status)
        else:
            # v2 silently ignored unknown verbs, which made a version mismatch
            # look like a working connection that simply never did anything.
            self._send({"event": "error", "msg": "unknown_command", "cmd": c})

    def _armed_delay_or_error(self, verb: str) -> "_InboundDelayBuffer | None":
        """Return the buffer, or answer with an explicit disarmed error.

        Never returns None silently.  The whole point of the loud path is that
        `manual_meter_delay_ms` spent months as an invisible no-op -- the failure
        mode this codebase keeps reproducing is a feature that is off and says
        nothing about it.
        """
        if self._delay is not None:
            return self._delay
        self._send({
            "event": "error",
            "msg": "meter_delay_disarmed",
            "cmd": verb,
            "detail": (
                "the inbound meter delay ships DISARMED. Start the bridge with "
                f"{_METER_ARM_FLAG} or set {_METER_ARM_ENV}=1. It is deliberately "
                "not exposed through the launcher or the app settings."
            ),
        })
        return None

    def _retarget_intercept(self) -> None:
        if self._delay is None:
            return
        try:
            self._delay.retarget(*self._wd_loop.current_ips())
        except Exception as exc:
            _log.error("meter intercept retarget failed: %s", exc)

    def _send(self, msg: dict) -> None:
        try:
            line = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
            self._q.put_nowait(line)
        except queue.Full:
            pass


# ---------------------------------------------------------------------------
# TCP server loop
# ---------------------------------------------------------------------------
def _tcp_server_loop(hub: _BroadcastHub, wd_loop: _WinDivertLoop,
                     stop_evt: threading.Event,
                     delay_buf: "_InboundDelayBuffer | None" = None) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.settimeout(1.0)
    try:
        srv.bind((LISTEN_HOST, LISTEN_PORT))
        srv.listen(8)
        _log.info("Listening on %s:%d", LISTEN_HOST, LISTEN_PORT)
    except Exception as exc:
        _log.error("Cannot bind %s:%d — %s", LISTEN_HOST, LISTEN_PORT, exc)
        stop_evt.set()
        return

    while not stop_evt.is_set():
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        handler = _ClientHandler(conn, addr, hub, wd_loop, delay_buf)
        t = threading.Thread(target=handler.run, daemon=True, name=f"client-{addr}")
        t.start()

    srv.close()


# ---------------------------------------------------------------------------
# Windows service class
# ---------------------------------------------------------------------------
if _PYWIN32_OK:
    class NexusVisionService(win32serviceutil.ServiceFramework):  # type: ignore[misc]
        _svc_name_         = SVC_NAME
        _svc_display_name_ = SVC_DISPLAY_NAME
        _svc_description_  = SVC_DESCRIPTION
        # Frozen exe hosts the service itself; register THIS exe as the service binary
        # (pywin32 would otherwise default to the absent pythonservice.exe).
        if _IS_FROZEN:
            _exe_name_ = sys.executable

        def __init__(self, args) -> None:
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_evt  = threading.Event()
            self._hWaitStop = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self) -> None:
            import win32service
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._stop_evt.set()
            win32event.SetEvent(self._hWaitStop)

        def SvcDoRun(self) -> None:
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            _log.info("%s starting", SVC_NAME)
            _run_service(self._stop_evt)
            _log.info("%s stopped", SVC_NAME)


def _run_service(stop_evt: threading.Event, mode: str = "service") -> None:
    global _METER_ARMED, SVC_FEATURES
    _init_bridge_token(mode)

    armed, arm_source = _meter_arm_requested()
    _METER_ARMED = armed
    SVC_FEATURES = ("meter_delay",) if armed else ()

    hub      = _BroadcastHub()
    wd_loop  = _WinDivertLoop(hub, stop_evt)

    delay_buf: "_InboundDelayBuffer | None" = None
    if armed:
        # Second, fully independent WinDivert handle.  Never touched by the
        # sniff loop's filter-restart teardown.
        delay_buf = _InboundDelayBuffer(hub)
        wd_loop.set_meter_status_provider(delay_buf.snapshot)
        _log.warning(
            "INBOUND METER DELAY ARMED (via %s). This bridge will hold "
            "server->console game packets on request. Slew capped at %.0f ms/s.",
            arm_source, _METER_MAX_SLEW_MS_PER_S)
    else:
        _log.info("Inbound meter delay DISARMED (default). Verbs will answer "
                  "meter_delay_disarmed. Arm with %s or %s=1.",
                  _METER_ARM_FLAG, _METER_ARM_ENV)

    wd_thread = threading.Thread(target=wd_loop.run, daemon=True, name="wd-capture")
    wd_thread.start()

    tcp_thread = threading.Thread(
        target=_tcp_server_loop, args=(hub, wd_loop, stop_evt, delay_buf),
        daemon=True, name="tcp-server",
    )
    tcp_thread.start()

    stop_evt.wait()
    _log.info("Stop signal received, shutting down")
    # Flush and release the console before anything else during shutdown.
    if delay_buf is not None:
        try:
            delay_buf.stop("service_shutdown")
        except Exception as exc:
            _log.error("meter intercept shutdown failed: %s", exc)
    wd_loop.stop()
    wd_thread.join(timeout=4.0)
    tcp_thread.join(timeout=2.0)
    # Only now, with every handle closed, can the driver actually be unloaded.
    # Leaving a kernel driver resident after the product exits is not shippable,
    # and it was one of the two stated reasons this feature was pulled.
    _unload_windivert_driver("service_shutdown")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} "
              f"install|start|stop|remove|debug|driver-status|unload-driver")
        print(f"       add {_METER_ARM_FLAG} to arm the inbound meter delay "
              f"(default: disarmed)")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    # Operator escape hatches for the driver-residency guarantee. Deliberately
    # separate verbs so a stuck driver can be inspected and cleared without
    # starting a bridge (e.g. after a force-kill, which skips every teardown).
    if cmd == "driver-status":
        state = _windivert_driver_running()
        print(f"[nexus_svc] WinDivert driver running: {state}")
        sys.exit(0 if state is False else 1)
    if cmd == "unload-driver":
        ok = _unload_windivert_driver("operator_request")
        print(f"[nexus_svc] WinDivert driver unloaded: {ok}")
        sys.exit(0 if ok else 1)

    if cmd == "debug":
        armed, arm_source = _meter_arm_requested()
        print(f"[nexus_svc] Running in debug mode on {LISTEN_HOST}:{LISTEN_PORT}")
        print(f"[nexus_svc] Inbound meter delay: "
              f"{'ARMED (' + arm_source + ')' if armed else 'DISARMED'}")
        print("[nexus_svc] Press Ctrl+C to stop.")
        stop = threading.Event()
        try:
            # Debug mode is how the app starts the bridge when NexusVisionSvc is not installed,
            # i.e. as an ordinary unelevated user. It publishes its token to LOCALAPPDATA, which
            # is already per-user private, so no privileged ACL edit is needed.
            _run_service(stop, mode="debug")
        except KeyboardInterrupt:
            stop.set()
        return

    if not _PYWIN32_OK:
        print("ERROR: pywin32 not installed. Run: pip install pywin32")
        sys.exit(1)

    # HandleCommandLine does its own argv parsing and rejects unknown switches,
    # so the arm flag must not reach it. Service-mode arming goes through the
    # environment instead (the flag is a debug-mode convenience).
    sys.argv = [a for a in sys.argv if a != _METER_ARM_FLAG]
    win32serviceutil.HandleCommandLine(NexusVisionService)


if __name__ == "__main__":
    if _is_service_run_invocation():
        # Either a bare launch (dev) or the SCM starting the compiled service (its
        # ImagePath may carry --arm-meter-delay). Run the SCM control dispatcher;
        # _run_service() reads the arm flag from argv via _meter_arm_requested().
        if _PYWIN32_OK:
            servicemanager.Initialize()
            servicemanager.PrepareToHostSingle(NexusVisionService)
            servicemanager.StartServiceCtrlDispatcher()
        else:
            sys.exit(1)
    else:
        main()
