"""Parse the scheduler trace (app/src/trace.c) out of a raw board capture.

A capture is everything that came over the UART: ROM bootloader text, Zephyr
boot lines, the demo's own header lines, then the trace:

    # rtoscope trace, cycle clock 240000000 Hz, 563 events, 0 dropped, trace call 143 cycles
    cycles,thread,event,prio,arg
    0,L,ready,9,
    ...
    # crc32 1a2b3c4d

The CRC-32 covers every dump line before it (each with its "\n"). The serial
path can lose bytes mid-line, and a lost digit would still parse.
"""

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path

TRACE_RE = re.compile(r"^# rtoscope trace, cycle clock (?P<hz>\d+) Hz, (?P<n>\d+) events, "
                      r"(?P<dropped>\d+) dropped, trace call (?P<call>\d+) cycles$")
CRC_RE = re.compile(r"^# crc32 (?P<crc>[0-9a-f]{8})$")
EVENT_RE = re.compile(r"^(?P<cyc>\d+),(?P<thread>[^,]+),(?P<event>in|out|ready|pend|prio|app|mon),"
                      r"(?P<prio>-?\d+),(?P<arg>.*)$")


class TraceError(Exception):
    pass


@dataclass
class Event:
    t: float          # µs since the first trace record
    thread: str
    event: str
    prio: int
    arg: str


@dataclass
class Trace:
    path: Path
    hz: int
    call_cycles: int
    lines: list                       # stripped lines since the previous trace ended
    events: list = field(default_factory=list)
    error: str | None = None          # set (strict=False only) if the dump is corrupt


def parse_all(path, strict=True):
    """Return every trace in the capture at path, in order.

    Each trace's lines are those printed between the previous trace's last
    line and its own header; for the first trace that includes the boot
    output and the demo's header.

    A trace is corrupt if its CRC-32 is missing or wrong, if its record count
    doesn't match the header, or if the ring buffer dropped records. With
    strict=True that raises TraceError; with strict=False the trace is
    returned with .error set, so a caller can skip one bad run of many.
    """
    path = Path(path)
    # errors="replace": captures start with ROM bootloader output and can end
    # mid-line, so don't assume clean UTF-8 or a complete last line.
    raw_lines = [ln.strip() for ln in path.read_text(errors="replace").splitlines()]

    traces, i, prev_end = [], 0, 0
    while i < len(raw_lines):
        m = TRACE_RE.match(raw_lines[i])
        if not m:
            i += 1
            continue
        hz, expected = int(m["hz"]), int(m["n"])
        trace = Trace(path, hz, int(m["call"]), raw_lines[prev_end:i])
        crc = zlib.crc32((raw_lines[i] + "\n").encode())
        i += 1
        # The dump body runs to its CRC line (or, if that was lost, to the
        # next trace header or the end of the capture).
        while i < len(raw_lines) and not CRC_RE.match(raw_lines[i]) \
                and not TRACE_RE.match(raw_lines[i]):
            crc = zlib.crc32((raw_lines[i] + "\n").encode(), crc)
            if e := EVENT_RE.match(raw_lines[i]):
                trace.events.append(Event(int(e["cyc"]) * 1e6 / hz, e["thread"], e["event"],
                                          int(e["prio"]), e["arg"]))
            i += 1
        got = CRC_RE.match(raw_lines[i]) if i < len(raw_lines) else None
        if got:
            i += 1

        where = f"{path}: trace {len(traces) + 1}"
        if int(m["dropped"]):
            trace.error = f"{where}: ring buffer dropped {m['dropped']} records; enlarge TRACE_LEN"
        elif not got:
            trace.error = f"{where}: no crc32 line (truncated capture, or firmware without it)"
        elif int(got["crc"], 16) != crc:
            trace.error = (f"{where}: crc32 mismatch (board {got['crc']}, computed {crc:08x}): "
                           f"bytes lost or changed in transit")
        elif len(trace.events) != expected:
            trace.error = (f"{where}: header says {expected} records, parsed "
                           f"{len(trace.events)}")
        if trace.error and strict:
            raise TraceError(trace.error)
        traces.append(trace)
        prev_end = i
    if not traces:
        raise TraceError(f"{path}: no trace header")
    return traces


def parse(path):
    """Return the last trace in the capture at path (see parse_all)."""
    return parse_all(path)[-1]


def jobs(trace, thread):
    """Per-job (release, start, done, cpu_us) for a periodic thread.

    release: the "ready" that ends a sleep (the timer ISR's wake-up).
    cpu_us:  sum of the thread's in->out intervals between release and done.
    """
    out, sleeping, release, start, cpu, running_since = [], False, None, None, 0.0, None
    for e in trace.events:
        if e.thread != thread:
            continue
        if e.event == "out":
            if running_since is not None and release is not None:
                cpu += e.t - running_since
            running_since = None
            sleeping = e.arg == "sleeping"
        elif e.event == "ready" and sleeping:
            release, start, cpu, sleeping = e.t, None, 0.0, False
        elif e.event == "in":
            running_since = e.t
        elif e.event == "app" and e.arg == "start" and release is not None:
            start = e.t
        elif e.event == "app" and e.arg == "done" and release is not None:
            cpu += e.t - running_since
            running_since = e.t
            out.append((release, start, e.t, cpu))
            release = None
    return out


# Why a thread left the CPU (trace.c state_name) -> the state it is in after.
OUT_STATE = {"ready": "preempted", "pending": "blocked", "sleeping": "sleeping",
             "dead": "dead", "suspended": "suspended"}


def thread_states(trace):
    """thread -> [(state, t0, t1)] for every thread in the trace, measured.

    States: running ("in" to "out"); after "out", what the record says the
    thread left for: preempted (still runnable), blocked (pending on a
    kernel object), sleeping, dead; after a "ready" (woken, or created),
    ready: runnable and waiting for the CPU. Zero-length intervals are
    dropped; intervals still open at the end of the trace are closed there.
    """
    state, since, out = {}, {}, {}

    def enter(th, new, t):
        if th in state and t > since[th]:
            out.setdefault(th, []).append((state[th], since[th], t))
        state[th], since[th] = new, t

    for e in trace.events:
        if e.event == "in":
            enter(e.thread, "running", e.t)
        elif e.event == "out":
            enter(e.thread, OUT_STATE.get(e.arg, e.arg), e.t)
        elif e.event == "ready" and state.get(e.thread) != "running":
            enter(e.thread, "ready", e.t)
    end = trace.events[-1].t if trace.events else 0.0
    for th, st in state.items():
        if st != "dead" and end > since[th]:
            out.setdefault(th, []).append((st, since[th], end))
    return out
