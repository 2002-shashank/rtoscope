"""Parse the scheduler trace (app/src/trace.c) out of a raw board capture.

A capture is everything that came over the UART: ROM bootloader text, Zephyr
boot lines, the demo's own header lines, then the trace:

    # rtoscope trace, cycle clock 240000000 Hz, 563 events, 0 dropped, trace call 143 cycles
    cycles,thread,event,prio,arg
    0,L,ready,9,
    ...
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

TRACE_RE = re.compile(r"^# rtoscope trace, cycle clock (?P<hz>\d+) Hz, (?P<n>\d+) events, "
                      r"(?P<dropped>\d+) dropped, trace call (?P<call>\d+) cycles$")
EVENT_RE = re.compile(r"^(?P<cyc>\d+),(?P<thread>[^,]+),(?P<event>in|out|ready|pend|prio|app),"
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
    lines: list                       # every stripped line before the trace header
    events: list = field(default_factory=list)


def parse(path):
    """Return the last complete trace in the capture at path.

    Raises TraceError if there is no trace, if the ring buffer dropped
    records, or if the dump was cut off.
    """
    path = Path(path)
    # errors="replace": captures start with ROM bootloader output and can end
    # mid-line, so don't assume clean UTF-8 or a complete last line.
    raw_lines = [ln.strip() for ln in path.read_text(errors="replace").splitlines()]

    start = max((i for i, ln in enumerate(raw_lines) if TRACE_RE.match(ln)), default=None)
    if start is None:
        raise TraceError(f"{path}: no trace header")
    m = TRACE_RE.match(raw_lines[start])
    if int(m["dropped"]):
        raise TraceError(f"{path}: ring buffer dropped {m['dropped']} records; enlarge TRACE_LEN")

    hz = int(m["hz"])
    trace = Trace(path, hz, int(m["call"]), raw_lines[:start])
    for ln in raw_lines[start + 1:]:
        if e := EVENT_RE.match(ln):
            trace.events.append(Event(int(e["cyc"]) * 1e6 / hz, e["thread"], e["event"],
                                      int(e["prio"]), e["arg"]))
    if len(trace.events) != int(m["n"]):
        raise TraceError(f"{path}: header says {m['n']} records, parsed {len(trace.events)} "
                         f"(truncated capture?)")
    return trace


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
