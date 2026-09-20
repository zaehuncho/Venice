"""PS5 rest-mode detection + Remote Play wakeup for the input-only Chiaki launch.

Why this exists: OrionStream is launched in CLI direct-stream mode
(``OrionStream.exe stream <nickname> <host>``), and that code path in chiaki-ng
builds a StreamSessionConnectInfo and calls RunStream() directly — it never runs
discovery, never checks for standby, and never sends a wakeup packet. Only the
GUI list-click path (QmlBackend::connectToHost) wakes a sleeping console. The
result on a rig whose PS5 dropped to rest: every Connect fails ~5s in with
"Session request connect failed: Timeout" on TCP 9295 while UDP discovery is
answering "620 Server Standby" the whole time.

This module supplies the missing half from the Python side, before the client
process is ever spawned:

  * ``probe_console_state``  — UDP discovery SRCH (ports 9302/987) -> ready /
    standby / no_answer.
  * ``read_chiaki_regist_key`` — the RP regist key chiaki stored at
    registration time (QSettings -> HKCU\\Software\\Chiaki\\Chiaki), which is the
    credential the WAKEUP packet must carry.
  * ``send_wakeup``          — the exact WAKEUP datagram chiaki's
    DiscoveryManager::SendWakeup sends (user-credential = regist key parsed as
    hex, printed as decimal).
  * ``wait_for_session_port`` — poll TCP 9295 until the console actually
    accepts Remote Play sessions.

Fail-open by design: every entry point traps its own errors and reports a
neutral outcome, so a firewall that eats UDP replies (or a registry format
surprise) degrades to today's behavior — launch and let chiaki report — never
to a new failure mode.
"""

from __future__ import annotations

import logging
import re
import socket
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

logger = logging.getLogger("PS5Wake")

# Constants mirrored from chiaki-ng lib/include/chiaki/discovery.h — these are
# protocol facts, not tunables.
DISCOVERY_PORT_PS5 = 9302
DISCOVERY_PORT_PS4 = 987
PROTOCOL_VERSION_PS5 = "00030010"
PROTOCOL_VERSION_PS4 = "00020020"
SESSION_PORT = 9295

# chiaki_discovery_send transmits snprintf's length + 1, so every real chiaki
# datagram (SRCH and WAKEUP) carries a trailing NUL after the final newline.
# Mirror that byte-exactly.
_SRCH_HEAD = "SRCH * HTTP/1.1\n"

# chiaki_http_response_parse anchors "HTTP/1.1 " at offset 0 and strtol()s the
# whole digit run (so "6200" parses as 6200 = unknown, not 620).
_STATUS_RE = re.compile(r"^HTTP/1\.1 (\d+)")


def is_ip_literal(host: str) -> bool:
    """True for a numeric IPv4/IPv6 address — the only inputs the probes
    accept, because sendto/connect on a hostname would block on synchronous
    DNS resolution outside every probe budget."""
    text = str(host or "").strip()
    if not text:
        return False
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(family, text)
            return True
        except (OSError, ValueError):
            continue
    return False


@dataclass(frozen=True)
class ConsoleProbe:
    state: str          # "ready" | "standby" | "no_answer"
    port: int           # discovery port that answered (0 when no_answer)
    detail: str


def _parse_probe_response(data: bytes) -> Optional[str]:
    """Map a discovery response's status line to a console state."""
    try:
        text = data.decode("utf-8", "replace")
    except Exception:
        return None
    match = _STATUS_RE.match(text)
    if not match:
        return None
    try:
        code = int(match.group(1))
    except ValueError:
        return None
    if code == 200:
        return "ready"
    if code == 620:
        return "standby"
    # Any other well-formed status is still an answering console; treat unknown
    # codes conservatively as ready so we fall through to the normal launch.
    return "ready"


def probe_console_state(host: str, timeout_s: float = 2.5) -> ConsoleProbe:
    """UDP discovery probe. Never raises; no_answer covers every failure."""
    host = str(host or "").strip()
    if not host:
        return ConsoleProbe("no_answer", 0, "no console address configured")
    ports = (
        (DISCOVERY_PORT_PS5, PROTOCOL_VERSION_PS5),
        (DISCOVERY_PORT_PS4, PROTOCOL_VERSION_PS4),
    )
    deadline = time.monotonic() + max(0.5, float(timeout_s))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        return ConsoleProbe("no_answer", 0, f"socket unavailable: {exc}")
    try:
        sock.setblocking(False)
        while time.monotonic() < deadline:
            for port, version in ports:
                payload = (
                    f"{_SRCH_HEAD}device-discovery-protocol-version:{version}\n"
                ).encode("utf-8") + b"\x00"
                try:
                    sock.sendto(payload, (host, port))
                except OSError:
                    continue
            recv_until = min(deadline, time.monotonic() + 0.6)
            while time.monotonic() < recv_until:
                try:
                    data, addr = sock.recvfrom(2048)
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.02)
                    continue
                except ConnectionResetError:
                    # Windows surfaces ICMP port-unreachable (e.g. the PS4 port
                    # on a PS5) as WSAECONNRESET on a LATER recvfrom. That is a
                    # queued error, not the end of this window — drain it and
                    # keep listening or the real 620 reply behind it is lost.
                    time.sleep(0.005)
                    continue
                except OSError:
                    time.sleep(0.05)
                    break
                state = _parse_probe_response(data)
                if state:
                    return ConsoleProbe(state, int(addr[1]), f"discovery answered {state}")
        return ConsoleProbe("no_answer", 0, "no discovery response")
    finally:
        try:
            sock.close()
        except OSError:
            pass


@dataclass(frozen=True)
class DiscoveredConsole:
    host: str           # answering address
    state: str          # "ready" | "standby"
    port: int           # discovery port that answered


def discover_consoles(broadcast: str, timeout_s: float = 1.2) -> List[DiscoveredConsole]:
    """Broadcast SRCH on the given address and collect every answering console.

    Exists for CONSOLE ADDRESS DRIFT: the PS5 sits on the PC's ICS network and its
    DHCP lease moves (.100 -> .138 -> .126 within two days of live testing). Each time,
    the configured ip stopped answering and the input session died before it became
    ready ("the current Chiaki session ended before becoming ready"), while the console
    was answering discovery one address over. Never raises; an empty list covers every
    failure (no socket, no broadcast permission, no answer). One entry per address.
    """
    broadcast = str(broadcast or "").strip()
    if not broadcast:
        return []
    ports = (
        (DISCOVERY_PORT_PS5, PROTOCOL_VERSION_PS5),
        (DISCOVERY_PORT_PS4, PROTOCOL_VERSION_PS4),
    )
    found: dict = {}
    deadline = time.monotonic() + max(0.3, float(timeout_s))
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return []
    try:
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError:
            pass
        for port, version in ports:
            payload = (
                f"{_SRCH_HEAD}device-discovery-protocol-version:{version}\n"
            ).encode("utf-8") + b"\x00"
            try:
                sock.sendto(payload, (broadcast, port))
            except OSError:
                continue
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError, ConnectionResetError):
                time.sleep(0.02)
                continue
            except OSError:
                break
            state = _parse_probe_response(data)
            if state and addr[0] not in found:
                found[addr[0]] = DiscoveredConsole(str(addr[0]), state, int(addr[1]))
        return list(found.values())
    finally:
        try:
            sock.close()
        except OSError:
            pass


def subnet_broadcast(host: str) -> str:
    """The /24 directed broadcast for an IPv4 literal ('192.168.137.126' -> '192.168.137.255').

    ICS hands out a /24 (255.255.255.0) and that is the only topology this rig runs on;
    a non-IPv4 input yields '' so the caller skips discovery rather than guessing."""
    text = str(host or "").strip()
    try:
        socket.inet_pton(socket.AF_INET, text)
    except (OSError, ValueError):
        return ""
    parts = text.split(".")
    if len(parts) != 4:
        return ""
    return ".".join(parts[:3] + ["255"])


def _decode_qsettings_bytearray(value) -> bytes:
    """Recover raw QByteArray bytes from what QSettings stored in the registry.

    QSettings' Windows backend stores a QByteArray either as REG_BINARY of the
    raw bytes, or as the string form ``@ByteArray(<raw bytes>)`` encoded
    UTF-16LE. The @ByteArray wrapper maps each raw byte to the same code point,
    so utf-16 decode followed by latin-1 encode is byte-exact.
    """
    if value is None:
        return b""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (bytes, bytearray)):
        raw = bytes(value)
        try:
            text = raw.decode("utf-16-le")
        except UnicodeDecodeError:
            return raw
        if not text.startswith("@ByteArray("):
            return raw
    else:
        return b""
    if text.startswith("@ByteArray(") and text.endswith(")"):
        inner = text[len("@ByteArray("):-1]
        try:
            return inner.encode("latin-1")
        except UnicodeEncodeError:
            return b""
    try:
        return text.encode("latin-1")
    except UnicodeEncodeError:
        return b""


def _extract_regist_key(raw: bytes) -> Optional[str]:
    """Truncate at the first NUL and validate: 1-8 hex chars (chiaki's rule)."""
    key = raw.split(b"\x00", 1)[0]
    if not key or len(key) > 8:
        return None
    try:
        text = key.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not re.fullmatch(r"[0-9a-fA-F]{1,8}", text):
        return None
    return text


def read_chiaki_regist_key(nickname: Optional[str] = None) -> Tuple[Optional[str], bool]:
    """Return (regist_key_hex, is_ps5) from chiaki's registered_hosts settings.

    Prefers the entry whose server_nickname matches; falls back to the sole
    entry when only one console is registered. (None, True) when unreadable.
    """
    try:
        import winreg
    except ImportError:
        return None, True
    wanted = str(nickname or "").strip()
    entries: List[Tuple[str, Optional[str], bool]] = []
    try:
        base = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Chiaki\Chiaki\registered_hosts")
    except OSError:
        return None, True
    try:
        index = 0
        while True:
            try:
                sub_name = winreg.EnumKey(base, index)
            except OSError:
                break
            index += 1
            try:
                sub = winreg.OpenKey(base, sub_name)
            except OSError:
                continue
            try:
                try:
                    nick_val = winreg.QueryValueEx(sub, "server_nickname")[0]
                except OSError:
                    nick_val = ""
                try:
                    key_val = winreg.QueryValueEx(sub, "rp_regist_key")[0]
                except OSError:
                    key_val = None
                try:
                    target_val = winreg.QueryValueEx(sub, "target")[0]
                    is_ps5 = int(target_val) >= 1000000
                except (OSError, TypeError, ValueError):
                    is_ps5 = True
                key = _extract_regist_key(_decode_qsettings_bytearray(key_val))
                entries.append((str(nick_val or ""), key, is_ps5))
            finally:
                winreg.CloseKey(sub)
    finally:
        winreg.CloseKey(base)

    if wanted:
        for nick, key, is_ps5 in entries:
            if nick == wanted and key:
                return key, is_ps5
        # Tolerate whitespace/case drift between `chiaki list` output and the
        # stored nickname before falling back to the single-console rule.
        folded = wanted.casefold()
        for nick, key, is_ps5 in entries:
            if nick.strip().casefold() == folded and key:
                return key, is_ps5
    with_keys = [(nick, key, is_ps5) for nick, key, is_ps5 in entries if key]
    if len(with_keys) == 1:
        return with_keys[0][1], with_keys[0][2]
    return None, True


def build_wakeup_packet(regist_key: str, ps5: bool = True) -> Optional[bytes]:
    """The exact WAKEUP datagram chiaki_discovery_packet_fmt produces."""
    key = str(regist_key or "").split("\x00", 1)[0]
    if not key or len(key) > 8:
        return None
    try:
        credential = int(key, 16)
    except ValueError:
        return None
    version = PROTOCOL_VERSION_PS5 if ps5 else PROTOCOL_VERSION_PS4
    # Trailing NUL: chiaki_discovery_send transmits formatted length + 1.
    return (
        "WAKEUP * HTTP/1.1\n"
        "client-type:vr\n"
        "auth-type:R\n"
        "model:w\n"
        "app-type:r\n"
        f"user-credential:{credential}\n"
        f"device-discovery-protocol-version:{version}\n"
    ).encode("utf-8") + b"\x00"


def send_wakeup(host: str, regist_key: str, ps5: bool = True) -> bool:
    """Send the wakeup datagram (3x against UDP loss). Never raises."""
    packet = build_wakeup_packet(regist_key, ps5)
    if packet is None:
        logger.warning("PS5 wakeup skipped: regist key is missing or malformed")
        return False
    port = DISCOVERY_PORT_PS5 if ps5 else DISCOVERY_PORT_PS4
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError as exc:
        logger.warning("PS5 wakeup skipped: socket unavailable (%s)", exc)
        return False
    try:
        sent = False
        for _ in range(3):
            try:
                sock.sendto(packet, (str(host).strip(), port))
                sent = True
            except OSError as exc:
                logger.warning("PS5 wakeup send failed: %s", exc)
                break
            time.sleep(0.05)
        return sent
    finally:
        try:
            sock.close()
        except OSError:
            pass


def session_port_open(host: str, timeout_s: float = 0.6) -> bool:
    """True when the console accepts TCP on the Remote Play session port."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        sock.settimeout(max(0.1, float(timeout_s)))
        return sock.connect_ex((str(host).strip(), SESSION_PORT)) == 0
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def wait_for_session_port(host: str, budget_s: float,
                          poll_s: Optional[float] = None,
                          connect_timeout_s: Optional[float] = None) -> bool:
    """Poll the session port until it opens or the budget runs out.

    Granularity matters here: the old 0.7s sleep + 0.6s connect timeout meant a console
    whose port had just opened was noticed up to ~1.3s late, on EVERY wake. A refused
    connect on the LAN returns instantly and a dropped SYN (stack still booting) only
    costs the connect timeout, so 0.25s/0.4s keeps the cost of a miss small while
    noticing readiness ~4x sooner. Both are env-tunable (ORION_PS5_WAKE_POLL_S /
    ORION_PS5_WAKE_CONNECT_TIMEOUT_S) for slow networks."""
    def _env_f(name: str, default: float, lo: float, hi: float) -> float:
        try:
            v = float(os.environ.get(name, "") or default)
        except Exception:
            v = default
        return max(lo, min(hi, v))
    poll = _env_f("ORION_PS5_WAKE_POLL_S", 0.25 if poll_s is None else float(poll_s), 0.05, 2.0)
    cto = _env_f("ORION_PS5_WAKE_CONNECT_TIMEOUT_S",
                 0.4 if connect_timeout_s is None else float(connect_timeout_s), 0.1, 2.0)
    deadline = time.monotonic() + max(0.0, float(budget_s))
    while True:
        if session_port_open(host, timeout_s=cto):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
            return False
        time.sleep(min(poll, remaining))
