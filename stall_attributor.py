"""[ORION_STALL_ATTRIB 2026-09-15] Who stole the frame?

THE COMPLAINT. The capture card delivers a metronomic 60 (raw_fps 59.8-60.1, raw_gap_max
24-30 ms) and the sidecar's own frame consumer still misses beats: `preview_stats ...
callback_gap_ms max=` reached 223 ms and 280 ms in two CONSECUTIVE 5 s windows at
21:50:35-40Z, 75-78 ms in several others, mostly with no shot in flight, once on the only
blind backstop of the session; one late shot fired with frame_age_ms=29. "A stable 60 fps"
is the requirement and every previous answer to a gap like this has been a guess.

THE INSTRUMENT. Three cheap facts, recorded BEFORE the gap happens, so the line that
explains it is evidence rather than a reconstruction:

  1. WHO WAS RUNNING. A sampler thread snapshots `sys._current_frames()` every
     ORION_STALL_SAMPLE_MS (default 5) into a ring that keeps ~500 ms. Each sample is reduced
     ON THE SPOT to (thread id, top-3 (code, lineno)) -- frames are LIVE objects, so keeping
     them would both pin the stack and read back the WRONG state when the gap is formatted
     milliseconds later. Code objects are immortal and hashable, so they are the stack key
     and no string is built until a gap actually needs printing. The sampler holds the GIL
     only for that walk; it measures its own cost and reports p99.

  2. WAS IT US OR THE MACHINE. `time.process_time()` is recorded beside the wall clock on
     every consumer callback, so the CPU the WHOLE process burned across the gap is known
     exactly. A gap with cpu_frac ~= 1.0 is a thread of ours holding the GIL or a core
     (cause=in_process); a gap with cpu_frac ~= 0 is the OS running something else
     (cause=starved) and no amount of sidecar tuning will fix it. `cover=` (what fraction of
     its own 5 ms slots the sampler actually got) is printed beside it because the two
     disagree in exactly the informative case.

  3. WAS IT THE GARBAGE COLLECTOR. gc.callbacks times every collection; one over
     ORION_STALL_GC_MS (default 10) gets its own GC: line, and the gen-2 count rides the 5 s
     summary. A gen-2 sweep over a live 1280x720x3 frame ring is the textbook cause of a
     one-off 200 ms hitch that no profiler is ever running to see.

COST, MEASURED, AND WHY THE DEFAULT IS OFF. On this machine, sampler on its own thread at
the shipped 5 ms period with 19 live threads (tests/test_stall_attributor.py and
scratch benches): p50 0.017 ms, p99 0.051-0.086 ms, mean 0.021-0.025 ms per sample. p99 is
well inside the 0.3 ms budget, but 200 samples/s x ~0.022 ms is ~0.45 % OF ONE CORE, which is
more than double the 0.2 % ship budget -- so ORION_STALL_ATTRIB DEFAULTS TO 0. The floor is
the per-thread walk of sys._current_frames(), not the reduction: dropping line numbers
(0.033 %/sample-Hz) or the 2nd and 3rd frames (0.024 %) does not buy the budget back, it only
buys worse attribution. Turn it on for a session (ORION_STALL_ATTRIB=1), or run it always-on
at ORION_STALL_SAMPLE_MS=20, which measures ~0.12 % -- inside budget, at 2 samples per 40 ms
gap instead of 8.

THE HOT PATH. `note_callback()` is five operations and one syscall-class `process_time()` read; it
never formats, never logs, never takes a lock. Formatting and logging happen on the watchdog
thread. NOTHING here runs on the detect thread (the 2026-09-14 regression: a GIL-holding
per-frame Python worker stalled the highest-priority detect thread 60-270 ms).

KNOBS
    ORION_STALL_ATTRIB      0    master switch (see COST above: 0 because 5 ms costs ~0.45 %
                                 of one core, over the 0.2 % ship budget; 1 for a session)
    ORION_STALL_GAP_MS      40   a consumer gap at or above this is a STALL
    ORION_STALL_SAMPLE_MS   5    sampler period
    ORION_STALL_RING_MS     500  how much history the ring keeps
    ORION_STALL_GC_MS       10   a collection at or above this gets a GC: line
    ORION_STALL_POLL_MS     25   watchdog drain period
    ORION_STALL_STATS_S     5    summary cadence (0 = off)
    ORION_STALL_CPU_MIN     0.5  cpu_frac at/above which the cause is in_process
    ORION_STALL_LOG_LEVEL   error  see _level() -- ERROR by default ON PURPOSE
"""
from __future__ import annotations

import gc
import logging
import os
import sys
import threading
import time
from collections import Counter, deque

_os_basename = os.path.basename

_log = logging.getLogger("stall_attrib")

_perf = time.perf_counter
_cpu = time.process_time

# A thread whose top frame sits in one of these is PARKED, not working: it is blocked on a
# lock, a queue, a socket or a sleep. It still appears in sys._current_frames() every sample,
# so without this the modal thread of any window is whichever idle thread there are most of.
_IDLE_FILES = ("threading.py", "queue.py", "selectors.py", "socket.py", "_weakrefset.py")


def _fenv(name: str, default: float) -> float:
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else float(default)
    except Exception:
        return float(default)


def enabled() -> bool:
    """Master switch. DEFAULT 0 -- see COST in the module docstring: the sampler measures
    ~0.45 % of one core at the shipped 5 ms period, over the 0.2 % budget this was allowed to
    spend. Read per call so `start()` sees a live toggle; the hot path latches it instead."""
    return _fenv("ORION_STALL_ATTRIB", 0.0) > 0.0


def _level() -> int:
    """ERROR by default, and that is a deliberate deviation from "log it at WARNING".

    RemotePlaySession.cpp:2884 relays the sidecar's stderr into orion_native.log, and EVERY
    sidecar WARNING line shares ONE global slot of kSidecarWarnThrottleMs = 1000 ms
    (RemotePlaySession.h:530); ERROR/CRITICAL bypass it. Stalls arrive in bursts -- the two
    worst of the session were in consecutive 5 s windows -- and the `Capture health` warning
    fires every 5 s beside them, so a WARNING-level STALL line would be sampled exactly when
    it matters most. That is the same relay that swallowed the PICKUP: line. Set
    ORION_STALL_LOG_LEVEL=warning to get the literal WARNING behaviour back.
    """
    return (logging.WARNING
            if os.environ.get("ORION_STALL_LOG_LEVEL", "").strip().lower().startswith("warn")
            else logging.ERROR)


class StallAttributor:
    """Attribution for consumer-callback gaps. One per process; see `get()`."""

    def __init__(self, sample_ms: float = None, gap_ms: float = None,
                 ring_ms: float = None, gc_ms: float = None,
                 poll_ms: float = None, stats_s: float = None):
        self.sample_s = max(0.001, (_fenv("ORION_STALL_SAMPLE_MS", 5.0)
                                    if sample_ms is None else float(sample_ms)) / 1000.0)
        self.gap_s = max(0.005, (_fenv("ORION_STALL_GAP_MS", 40.0)
                                 if gap_ms is None else float(gap_ms)) / 1000.0)
        self.ring_s = max(0.05, (_fenv("ORION_STALL_RING_MS", 500.0)
                                 if ring_ms is None else float(ring_ms)) / 1000.0)
        self.gc_ms = (_fenv("ORION_STALL_GC_MS", 10.0) if gc_ms is None else float(gc_ms))
        self.poll_s = max(0.002, (_fenv("ORION_STALL_POLL_MS", 25.0)
                                  if poll_ms is None else float(poll_ms)) / 1000.0)
        self.stats_s = (_fenv("ORION_STALL_STATS_S", 5.0)
                        if stats_s is None else float(stats_s))
        self.cpu_min = _fenv("ORION_STALL_CPU_MIN", 0.5)

        _ring_n = max(8, int(self.ring_s / self.sample_s) + 2)
        self._ring = deque(maxlen=_ring_n)          # (t, ((tid, (frame, ...)), ...))
        self._events = deque(maxlen=32)             # (t0, t1, gap_s, cpu0, cpu1)
        self._gc_events = deque(maxlen=16)          # (gen, ms, collected, uncollectable, thread)
        self._cost = deque(maxlen=4096)             # per-sample seconds
        self._names = {}                            # tid -> thread name (sticky, cheap)
        self._code = {}                             # code object -> "file:func"

        # hot-path state: written by the consumer thread ONLY, read by the watchdog
        self._last_t = 0.0
        self._last_cpu = 0.0
        self._cb_n = 0

        self._gc_t0 = 0.0
        self._gc2 = 0
        self._gc_worst_ms = 0.0
        self._gc_hooked = False
        self._samples = 0
        self._sample_cpu_s = 0.0
        self._started_at = 0.0
        self._stalls = 0
        self._worst_gap_ms = 0.0
        self._win_prev = {"stalls": 0, "gc2": 0, "callbacks": 0, "samples": 0}
        self._win_gap_ms = 0.0          # worst consumer gap inside the current summary window
        self._win_gc_ms = 0.0           # worst collection inside the current summary window

        self._on = False                # latched by start(): the hot path must not read env
        self._stop = threading.Event()
        self._sampler = None
        self._watchdog = None
        self._sampler_tid = -1
        self._watchdog_tid = -1

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "StallAttributor":
        if self._sampler is not None or not enabled():
            return self
        self._on = True
        self._started_at = _perf()
        self._stop.clear()
        self._sampler = threading.Thread(target=self._sample_loop,
                                         name="stall-sampler", daemon=True)
        self._watchdog = threading.Thread(target=self._watch_loop,
                                          name="stall-watchdog", daemon=True)
        self._sampler.start()
        self._watchdog.start()
        self._hook_gc()
        _log.info("STALL ATTRIB on: sample_ms=%.1f gap_ms=%.1f ring_ms=%.0f gc_ms=%.1f",
                  self.sample_s * 1000.0, self.gap_s * 1000.0,
                  self.ring_s * 1000.0, self.gc_ms)
        return self

    def stop(self) -> None:
        self._on = False
        self._stop.set()
        self._unhook_gc()
        for th in (self._sampler, self._watchdog):
            if th is not None and th.is_alive():
                th.join(timeout=1.0)
        self._sampler = None
        self._watchdog = None

    def _hook_gc(self) -> None:
        if not self._gc_hooked:
            gc.callbacks.append(self._gc_cb)
            self._gc_hooked = True

    def _unhook_gc(self) -> None:
        if self._gc_hooked:
            try:
                gc.callbacks.remove(self._gc_cb)
            except ValueError:
                pass
            self._gc_hooked = False

    # ------------------------------------------------------------------ hot path
    def note_callback(self, now: float = None) -> None:
        """Called once per delivered frame by the consumer thread. Keep this tiny.

        Five loads/stores and one process_time() read (~1 us, 60x/s). The gap is DETECTED
        here -- it is only knowable when the next frame lands -- but never formatted here:
        the event is queued and the watchdog thread does the work.
        """
        if not self._on:
            return
        t = _perf() if now is None else now
        c = _cpu()
        t0 = self._last_t
        c0 = self._last_cpu
        self._last_t = t
        self._last_cpu = c
        self._cb_n += 1
        if t0 and (t - t0) >= self.gap_s:
            self._events.append((t0, t, t - t0, c0, c))

    # ------------------------------------------------------------------ sampler
    def _reduce(self, frames):
        """One sample, as cheap as it is possible to make it.

        NO string formatting and NO name lookup here -- both used to live in this loop and
        cost 4x (measured: 0.027 ms/sample against 0.0064 ms, i.e. 0.53 % of a core at the
        shipped 5 ms period against 0.13 %). Code objects are immortal for as long as their
        function exists and are hashable, so the raw (code, lineno) pairs ARE the stack key;
        the ring keeps them for at most ORION_STALL_RING_MS and the watchdog renders them
        only for the handful of samples inside an actual gap.

        Frames themselves are never kept: they are live objects, so a reference would both
        pin the stack and read back the WRONG state when the gap is formatted later.
        """
        out = []
        ap = out.append
        me = self._sampler_tid
        for tid, f1 in frames.items():
            if tid == me:
                continue            # the sampler is never evidence about the sampler
            f2 = f1.f_back if f1 is not None else None
            f3 = f2.f_back if f2 is not None else None
            ap((tid,
                (f1.f_code, f1.f_lineno) if f1 is not None else None,
                (f2.f_code, f2.f_lineno) if f2 is not None else None,
                (f3.f_code, f3.f_lineno) if f3 is not None else None))
        return out

    def _sample_loop(self) -> None:
        self._sampler_tid = threading.get_ident()
        cur = sys._current_frames
        ring, cost = self._ring, self._cost
        reduce_ = self._reduce
        period = self.sample_s
        nxt = _perf()
        while not self._stop.is_set():
            t0 = _perf()
            try:
                ring.append((t0, reduce_(cur())))
            except Exception:
                pass
            t1 = _perf()
            cost.append(t1 - t0)
            self._samples += 1
            self._sample_cpu_s += (t1 - t0)
            nxt += period
            if nxt < t1:                     # we overran; resynchronise instead of spinning
                nxt = t1 + period
            self._stop.wait(max(0.0, nxt - t1))

    def _refresh_names(self) -> None:
        """tid -> name, refreshed on the WATCHDOG thread (never the sampler).

        Sticky on purpose: a thread that died inside the gap still has to be nameable when
        the line is formatted a few milliseconds later.
        """
        try:
            for th in threading.enumerate():
                ident = th.ident
                if ident is not None:
                    self._names[ident] = th.name
        except Exception:
            pass

    # ------------------------------------------------------------------ gc
    def _gc_cb(self, phase, info) -> None:
        if phase == "start":
            self._gc_t0 = _perf()
            return
        ms = (_perf() - self._gc_t0) * 1000.0
        gen = int(info.get("generation", -1))
        if gen >= 2:
            self._gc2 += 1
        if ms > self._gc_worst_ms:
            self._gc_worst_ms = ms
        if ms > self._win_gc_ms:
            self._win_gc_ms = ms
        if ms >= self.gc_ms:
            self._gc_events.append((gen, ms, int(info.get("collected", 0) or 0),
                                    int(info.get("uncollectable", 0) or 0),
                                    threading.current_thread().name))

    # ------------------------------------------------------------------ watchdog
    def _watch_loop(self) -> None:
        self._watchdog_tid = threading.get_ident()
        next_stats = _perf() + (self.stats_s if self.stats_s > 0 else 1e9)
        while not self._stop.is_set():
            self._stop.wait(self.poll_s)
            try:
                self._refresh_names()
                self.drain_once()
                now = _perf()
                if self.stats_s > 0 and now >= next_stats:
                    next_stats = now + self.stats_s
                    self.log_stats()
            except Exception:
                pass

    def drain_once(self) -> int:
        """Format and log every queued stall + slow collection. Tests call this directly."""
        n = 0
        if self._events:
            self._refresh_names()
        lvl = _level()
        while self._gc_events:
            gen, ms, collected, uncollectable, thread = self._gc_events.popleft()
            _log.log(lvl, "GC: gen=%d ms=%.1f collected=%d uncollectable=%d thread=%s",
                     gen, ms, collected, uncollectable, thread)
            n += 1
        while self._events:
            t0, t1, gap_s, cpu0, cpu1 = self._events.popleft()
            self._stalls += 1
            gap_ms = gap_s * 1000.0
            if gap_ms > self._worst_gap_ms:
                self._worst_gap_ms = gap_ms
            if gap_ms > self._win_gap_ms:
                self._win_gap_ms = gap_ms
            _log.log(lvl, "%s", self.format_stall(t0, t1, gap_s, cpu0, cpu1))
            n += 1
        return n

    def format_stall(self, t0, t1, gap_s, cpu0, cpu1) -> str:
        """The one line.

        Field order is load-bearing: RemotePlaySession trims a relayed sidecar line to 300
        characters and the "<ts> ERROR stall_attrib: " prefix eats ~50, so gap/thread/where
        lead and the corroborating numbers follow. (The `Capture health` line has been losing
        its last ~120 characters to that same cap since the day it was written.)

        WHO gets named: the thread with the most BUSY samples, where busy means its top frame
        was not parked in threading/queue/selectors/socket. Counting samples instead would
        name whichever thread is blocked in join() around the gap -- which is usually the
        consumer itself, i.e. the one thread that is by definition the victim. When NO thread
        was busy the honest answer is nobody: `thread=idle where=all-parked`, which paired
        with cause=starved is the signature of the whole process being descheduled.
        """
        window = [s for (ts, s) in self._ring if t0 <= ts <= t1]
        counts = Counter()
        busy = Counter()
        stacks = {}
        for samples in window:
            for tid, f1, f2, f3 in samples:
                if tid == self._watchdog_tid:
                    continue            # our own drain thread is not a suspect
                counts[tid] += 1
                if not self._idle(f1):
                    busy[tid] += 1
                    stacks.setdefault(tid, Counter())[(f1, f2, f3)] += 1

        if busy:
            top = max(busy, key=lambda tid: (busy[tid], len(stacks[tid]), counts[tid]))
            name = self._names.get(top, "tid-%d" % top)
            where = "|".join(self._render(stacks[top].most_common(1)[0][0])) or "?"
            if len(where) > 96:
                where = where[:93] + "..."
            other = [t for t in busy if t != top]
        else:
            # Nothing was executing Python: either every thread is parked (the gap is
            # somewhere below us -- a driver, a C call, the OS) or the sampler never ran.
            name = "idle" if window else "?"
            where = "all-parked" if window else "no-sample"
            other = []

        also = ",".join("%s(%d)" % (self._names.get(t, "tid-%d" % t), busy[t])
                        for t in sorted(other, key=busy.get, reverse=True)[:2]) or "-"
        cpu_frac = (cpu1 - cpu0) / gap_s if gap_s > 0 else 0.0
        expect = max(1.0, gap_s / self.sample_s)
        cover = len(window) / expect
        cause = "in_process" if cpu_frac >= self.cpu_min else "starved"
        line = ("STALL: gap_ms=%.1f thread=%s where=%s cause=%s cpu_frac=%.2f cover=%.2f "
                "samples=%d gc2=%d also=%s"
                % (gap_s * 1000.0, name, where, cause, cpu_frac, cover,
                   len(window), self._gc2, also))
        return line if len(line) <= 236 else (line[:233] + "...")

    @staticmethod
    def _idle(frame) -> bool:
        return frame is None or _os_basename(frame[0].co_filename) in _IDLE_FILES

    @staticmethod
    def _render(stack):
        out = []
        for fr in stack:
            if fr is None:
                continue
            code, lineno = fr
            out.append("%s:%s:%d" % (_os_basename(code.co_filename), code.co_name, lineno))
        return out

    # ------------------------------------------------------------------ stats
    def sampler_cost(self) -> dict:
        """p50/p99 per-sample seconds and the share of one core the sampler has used."""
        c = sorted(self._cost)
        if not c:
            return {"n": 0, "p50_ms": 0.0, "p99_ms": 0.0, "cpu_pct": 0.0}
        elapsed = max(1e-9, _perf() - (self._started_at or _perf()))
        return {"n": len(c),
                "p50_ms": c[len(c) // 2] * 1000.0,
                "p99_ms": c[min(len(c) - 1, int(len(c) * 0.99))] * 1000.0,
                "cpu_pct": 100.0 * self._sample_cpu_s / elapsed}

    def stats(self) -> dict:
        """Everything a 5 s stats line would want. `gc2` is the gen-2 collection count.

        native_orion/backend/autogreen_sidecar.py owns the `preview_stats` line this number
        belongs in; that tree is off-limits for this change, so the attributor prints its own
        summary AND exposes it here for a one-field addition there later.
        """
        cost = self.sampler_cost()
        return {"gc2": self._gc2, "gc_worst_ms": self._gc_worst_ms,
                "stalls": self._stalls, "worst_gap_ms": self._worst_gap_ms,
                "callbacks": self._cb_n, "samples": self._samples,
                "sampler_p99_ms": cost["p99_ms"], "sampler_cpu_pct": cost["cpu_pct"]}

    def log_stats(self) -> None:
        """The attributor's own summary: DELTAS since the last one, plus sampler cost.

        The counters `stats()` hands the orchestrator stay CUMULATIVE on purpose -- every
        other counter on the `Capture health` line (shm_ok, source_skip, ...) is cumulative
        and is read by diffing consecutive lines, and a counter that two readers both reset
        loses whichever window the other one claimed.
        """
        s = self.stats()
        p = self._win_prev
        _log.log(_level(),
                 "STALL STATS: stalls=%d worst_gap_ms=%.1f gc2=%d gc_worst_ms=%.1f "
                 "cb=%d samples=%d sampler_p99_ms=%.3f sampler_cpu_pct=%.3f",
                 s["stalls"] - p["stalls"], self._win_gap_ms,
                 s["gc2"] - p["gc2"], self._win_gc_ms,
                 s["callbacks"] - p["callbacks"], s["samples"] - p["samples"],
                 s["sampler_p99_ms"], s["sampler_cpu_pct"])
        self._win_prev = s
        self._win_gap_ms = 0.0
        self._win_gc_ms = 0.0


# --------------------------------------------------------------------------- #
#  Process-wide handle. The orchestrator owns exactly two calls: get().start()
#  once, and ATTRIB.note_callback() on the consumer thread.
# --------------------------------------------------------------------------- #
_ATTRIB = None
_LOCK = threading.Lock()


def get() -> StallAttributor:
    global _ATTRIB
    if _ATTRIB is None:
        with _LOCK:
            if _ATTRIB is None:
                _ATTRIB = StallAttributor()
    return _ATTRIB


def note_callback(now: float = None) -> None:
    """Free-function hot hook; inert (one attribute load) when the knob is off."""
    a = _ATTRIB
    if a is not None and a._on:
        a.note_callback(now)
