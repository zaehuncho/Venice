#!/usr/bin/env python
"""Passive per-shot network instrument for the online variance hunt (2026-09-22).

WHY. The online grade carries ~40 ms sd of error that does not exist offline
(docs/variance/ONLINE_GRADING_CLOCK_2026-09-22.md). It has never been measured:
the sidecar's RTT engine is fed only by the meter-delay capture service, which the
capture-card rig never runs, so every shot record says court_ready=0. On ICS the
PS5's game traffic is routed THROUGH this PC, so we can see, per shot:

  * the Remote Play datagram that carries the press / release to the PS5 (LAN),
  * the PS5's next outbound packet to the 2K server (its send-tick phase),
  * the server's inbound cadence (its tick rate and phase),
  * RTT to the court and to the home gateway (network jitter vs local bufferbloat).

WHAT IT DOES. Nothing that touches the game: tshark captures HEADERS ONLY (snaplen 42 =
Ethernet 14 + IPv4 20 + UDP 8, and the filter drops non-initial IPv4 fragments, which have no UDP
header and would otherwise put payload inside those 42 bytes; the original length is still recorded) on the PS5 adapter, and an ICMP pinger (IcmpSendEcho, no admin needed)
probes the court and the internet gateway at 20 Hz. Everything is timestamped on the
Windows wall clock, the same clock the shot records use (press_ts_ms, release wall_ms).

    python tools/timing/net_probe.py                 # until Ctrl+C
    python tools/timing/net_probe.py --duration 1800

Output: D:\\NexusVision\\netcap\\<utc-stamp>\\{packets.csv, pings.csv, capture.pcapng, meta.json}
Analyse with tools/timing/net_join.py.
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import csv
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
DEFAULT_OUT = r"D:\NexusVision\netcap"
DEFAULT_PS5 = "192.168.137.81"


def is_public(ip: str) -> bool:
    import ipaddress
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_multicast
                or a.is_reserved or a.is_unspecified)


def find_interface(name_hint: str) -> str:
    out = subprocess.run([TSHARK, "-D"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if f"({name_hint})" in line:
            return line.split(". ", 1)[1].split(" (", 1)[0]
    raise SystemExit(f"no capture interface named {name_hint!r}; tshark -D:\n{out}")


def resolve_ps5(wait_s: float) -> str:
    """The PS5's CURRENT address = where OrionStream's Remote Play session is connected.

    ICS hands the PS5 a new DHCP lease now and then (09-22: .81 -> .126, same MAC, both still in
    the neighbour cache), and a hard-coded address silently captured only discovery pings. The
    launcher starts this probe before the stream connects, so poll for the session.
    """
    ps = ("$p = Get-Process OrionStream -ErrorAction SilentlyContinue; if ($p) {"
          " Get-NetTCPConnection -OwningProcess $p.Id -State Established -ErrorAction SilentlyContinue"
          " | Where-Object { $_.RemoteAddress -like '192.168.137.*' }"
          " | Select-Object -First 1 -ExpandProperty RemoteAddress }")
    deadline = time.time() + wait_s
    while True:
        try:
            ip = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                                capture_output=True, text=True, timeout=20).stdout.strip()
        except Exception:
            ip = ""
        if ip:
            return ip
        if time.time() >= deadline:
            print(f"[net_probe] no Remote Play session found in {wait_s:.0f} s; "
                  f"falling back to {DEFAULT_PS5} (may be stale)")
            return DEFAULT_PS5
        time.sleep(5)


def internet_gateway() -> str:
    ps = ("(Get-NetRoute -DestinationPrefix '0.0.0.0/0' | Sort-Object RouteMetric,InterfaceMetric"
          " | Select-Object -First 1).NextHop")
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                              capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


# ---- ICMP via iphlpapi (unprivileged) --------------------------------------------------
class _IcmpReply(ctypes.Structure):
    _fields_ = [("Address", wt.ULONG), ("Status", wt.ULONG), ("RoundTripTime", wt.ULONG),
                ("DataSize", wt.USHORT), ("Reserved", wt.USHORT), ("Data", ctypes.c_void_p),
                ("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class Pinger(threading.Thread):
    def __init__(self, label: str, target_fn, hz: float, writer, lock, stop):
        super().__init__(daemon=True)
        self.label, self.target_fn, self.period = label, target_fn, 1.0 / hz
        self.writer, self.lock, self.stop = writer, lock, stop
        self.iphlp = ctypes.windll.iphlpapi
        self.iphlp.IcmpCreateFile.restype = wt.HANDLE
        # HANDLE is 64-bit: without argtypes ctypes passes it as a 32-bit int and every call
        # raises OverflowError (which the first version swallowed - zero pings logged).
        self.iphlp.IcmpSendEcho.argtypes = [wt.HANDLE, wt.ULONG, ctypes.c_void_p, wt.WORD,
                                            ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, wt.DWORD]
        self.iphlp.IcmpSendEcho.restype = wt.DWORD
        self.handle = self.iphlp.IcmpCreateFile()
        self.reported_error = False

    def run(self):
        payload = b"venice-netprobe!"
        reply_size = ctypes.sizeof(_IcmpReply) + len(payload) + 8
        buf = ctypes.create_string_buffer(reply_size)
        nxt = time.perf_counter()
        while not self.stop.is_set():
            ip = self.target_fn()
            if ip:
                try:
                    addr = struct.unpack("<I", socket.inet_aton(ip))[0]
                    t_wall = time.time() * 1000.0
                    t0 = time.perf_counter()
                    n = self.iphlp.IcmpSendEcho(self.handle, addr, payload, len(payload), None,
                                                buf, reply_size, 250)
                    dt = (time.perf_counter() - t0) * 1000.0
                    status = _IcmpReply.from_buffer(buf).Status if n else -1
                    with self.lock:
                        self.writer.writerow([f"{t_wall:.3f}", self.label, ip,
                                              f"{dt:.3f}" if n and status == 0 else "", status])
                except Exception as exc:
                    if not self.reported_error:
                        print(f"[net_probe] {self.label} pinger error: {exc!r}")
                        self.reported_error = True
            nxt += self.period
            time.sleep(max(0.0, nxt - time.perf_counter()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--interface", default="PS5", help="tshark interface NAME (default: PS5)")
    ap.add_argument("--ps5", default="auto",
                    help="PS5 address, or 'auto' = OrionStream's Remote Play peer (default)")
    ap.add_argument("--ps5-wait", type=float, default=300.0,
                    help="seconds to wait for the Remote Play session when --ps5 auto")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--ping-hz", type=float, default=20.0)
    ap.add_argument("--duration", type=float, default=0.0, help="seconds; 0 = until Ctrl+C")
    args = ap.parse_args()

    iface = find_interface(args.interface)
    if args.ps5 == "auto":
        args.ps5 = resolve_ps5(args.ps5_wait)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    outdir = Path(args.out) / stamp
    outdir.mkdir(parents=True, exist_ok=True)
    gw = internet_gateway()
    meta = {"started_utc": stamp, "interface": iface, "ps5": args.ps5, "gateway": gw,
            "ping_hz": args.ping_hz, "snaplen": 42, "clock": "wall ms (time.time / frame.time_epoch)"}
    (outdir / "meta.json").write_text(json.dumps(meta, indent=1))
    print(f"[net_probe] {outdir}  iface={args.interface}  ps5={args.ps5}  gateway={gw or '?'}")

    stop = threading.Event()
    lock = threading.Lock()
    pf = open(outdir / "pings.csv", "w", newline="", buffering=1)
    pw = csv.writer(pf)
    pw.writerow(["wall_ms", "target", "ip", "rtt_ms", "status"])

    peers: Counter = Counter()
    court = {"ip": ""}

    def court_ip():
        return court["ip"]

    Pinger("court", court_ip, args.ping_hz, pw, lock, stop).start()
    if gw:
        Pinger("gateway", lambda: gw, args.ping_hz, pw, lock, stop).start()

    cmd = [TSHARK, "-i", iface, "-s", "42", "-f", f"host {args.ps5} and udp and (ip[6:2] & 0x1fff) = 0",
           "-w", str(outdir / "capture.pcapng"), "-P", "-l", "-Q",
           "-T", "fields", "-E", "separator=,",
           "-e", "frame.time_epoch", "-e", "ip.src", "-e", "ip.dst",
           "-e", "udp.srcport", "-e", "udp.dstport", "-e", "frame.len"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            bufsize=1)
    kf = open(outdir / "packets.csv", "w", newline="", buffering=1)
    kw = csv.writer(kf)
    kw.writerow(["wall_ms", "dir", "peer", "sport", "dport", "len"])
    t_end = time.time() + args.duration if args.duration > 0 else None
    if t_end:
        # The read loop blocks while no packets arrive (PS5 idle), so the deadline must
        # be enforced from outside it: end tshark on time, which ends the loop.
        threading.Timer(args.duration, proc.terminate).start()
    n = 0
    last_report = time.time()
    try:
        for line in proc.stdout:
            parts = line.strip().split(",")
            if len(parts) < 6 or not parts[0]:
                continue
            t, src, dst, sp, dp, ln = parts[:6]
            if src == args.ps5:
                d, peer = "out", dst
            elif dst == args.ps5:
                d, peer = "in", src
            else:
                continue
            kw.writerow([f"{float(t) * 1000.0:.3f}", d, peer, sp, dp, ln])
            n += 1
            if is_public(peer):
                peers[peer] += 1
                top, cnt = peers.most_common(1)[0]
                if cnt >= 200 and top != court["ip"]:
                    court["ip"] = top
                    print(f"[net_probe] court = {top} ({cnt} packets)")
            now = time.time()
            if now - last_report >= 30:
                print(f"[net_probe] {n} packets, court={court['ip'] or '?'}")
                last_report = now
            if t_end and now >= t_end:
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        proc.terminate()
        meta.update({"packets": n, "court": court["ip"], "top_peers": peers.most_common(5)})
        (outdir / "meta.json").write_text(json.dumps(meta, indent=1))
        kf.close()
        pf.close()
        print(f"[net_probe] stopped: {n} packets, court={court['ip'] or 'never seen'} -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
