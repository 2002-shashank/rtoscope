"""Summarize on-device monitor findings ("mon" records) in board captures.

Usage:
    tools/.venv.nosync/bin/python tools/monitor_report.py logs/step7_*.log

For each alarm: what fired, on which thread, and the snapshot taken in the
expiry handler. Blocking alarms are classified: if the mutex owner was
running, the wait is bounded by its critical section; if a third thread was
running instead, it's unbounded priority inversion. Deadline alarms get
their detection latency: alarm time minus the deadline they guard.
"""

import re
import sys
from dataclasses import dataclass
from pathlib import Path

import tracelog

ROOT = Path(__file__).resolve().parent.parent

TASK_RE = re.compile(r"^# task,(?P<name>[^,]+),-?\d+,\d+,(?P<t>\d+)$")
H_DEADLINE_RE = re.compile(r"^# monitor: H deadline (?P<ms>\d+) ms after release")

ALARM_KINDS = ("deadline miss", "blocked too long")


@dataclass
class Alarm:
    kind: str
    victim: str
    t: float                  # µs since the first trace record
    running: str              # thread interrupted by the expiry
    owner: str | None = None  # blocking alarms: mutex owner...
    owner_prio: int | None = None  # ...and its priority at that moment

    @property
    def unbounded(self):
        """A blocking alarm where neither the owner nor the waiter was running."""
        return self.kind == "blocked too long" and self.running not in (self.owner, self.victim)


def alarms(trace):
    """Group each alarm record with the snapshot records that follow it."""
    out = []
    for e in trace.events:
        if e.event != "mon":
            continue
        if e.arg in ALARM_KINDS:
            out.append(Alarm(e.arg, e.thread, e.t, running="?"))
        elif out and e.arg == "running at alarm":
            out[-1].running = e.thread
        elif out and e.arg == "mutex owner":
            out[-1].owner, out[-1].owner_prio = e.thread, e.prio
    return out


def relative_deadlines(trace):
    """Thread -> relative deadline (µs), from the header the board printed."""
    deadlines = {m["name"]: int(m["t"]) * 1000 for ln in trace.lines if (m := TASK_RE.match(ln))}
    for ln in trace.lines:
        if m := H_DEADLINE_RE.match(ln):
            deadlines["H"] = int(m["ms"]) * 1000
    return deadlines


def releases(trace, thread):
    """Release times: each "ready" that ends one of the thread's sleeps."""
    out, sleeping = [], False
    for e in trace.events:
        if e.thread != thread:
            continue
        if e.event == "out":
            sleeping = e.arg == "sleeping"
        elif e.event == "ready" and sleeping:
            out.append(e.t)
            sleeping = False
    return out


def detection_latency(trace, alarm, deadlines):
    """Alarm time minus the deadline of the job it is about, in µs."""
    rel_deadline = deadlines[alarm.victim]
    due = [r + rel_deadline for r in releases(trace, alarm.victim) if r + rel_deadline <= alarm.t + 1]
    return alarm.t - max(due) if due else None


def main():
    paths = [Path(p).resolve() for p in sys.argv[1:]] or sorted((ROOT / "logs").glob("step7_*.log"))
    for path in paths:
        try:
            traces = tracelog.parse_all(path)
        except tracelog.TraceError as err:
            print(f"{path.name}: {err}")
            continue
        board = [ln for tr in traces for ln in tr.lines if ln.startswith(("# boot", "# wdt"))]
        print(f"{path.name}:")
        for ln in board:
            print(f"  {ln}")
        for k, tr in enumerate(traces, 1):
            deadlines = relative_deadlines(traces[0])
            found = alarms(tr)
            if not found:
                print(f"  trace {k}: no alarms")
            for a in found:
                line = f"  trace {k}: {a.kind:16} {a.victim} at {a.t / 1000:9.3f} ms, {a.running} running"
                if a.kind == "blocked too long":
                    line += (f", owner {a.owner} at prio {a.owner_prio}: "
                             + ("UNBOUNDED inversion" if a.unbounded else "bounded"))
                else:
                    lat = detection_latency(tr, a, deadlines)
                    line += f", detected {lat:.1f} µs after the deadline" if lat is not None else ""
                print(line)


if __name__ == "__main__":
    main()
