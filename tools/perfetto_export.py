"""Export board traces to Chrome Trace Event JSON, for ui.perfetto.dev.

Usage:
    tools/.venv.nosync/bin/python tools/perfetto_export.py LOG [-o OUT.json]
    tools/.venv.nosync/bin/python tools/perfetto_export.py        # the docs/perfetto/ examples

Open the JSON at https://ui.perfetto.dev (drag and drop), or chrome://tracing.

One process per trace (per run, for multi-run logs). Tracks, top to bottom:
  CPU0           which thread holds the CPU: one slice per switch-in
  <thread>       running / preempted / blocked / ready slices, release and
                 monitor-alarm instants, ordered by priority
  <thread> app   job k (start..done), waiting for lock, holds mutex
plus a priority counter per thread (Perfetto names it "<thread> prio", from
the event name and its "prio" arg), so priority inheritance shows as a step. Timestamps are µs with 3 decimals (CCOUNT ticks are 4.17 ns).

Chrome JSON requires slices on one track to nest; jobs span many
running/preempted slices and begin after the switch-in, so they live on the
separate app track.
"""

import argparse
import json
import re
import sys
from pathlib import Path

import tracelog

ROOT = Path(__file__).resolve().parent.parent

CPU_TID = 1                  # tids 2.. are threads, 1000+ their app tracks
APP_TID_OFFSET = 1000
STATE_NAMES = {"running": "running", "preempted": "preempted", "blocked": "blocked",
               "ready": "ready (woken)"}
INSTANTS = {"deadline miss", "blocked too long", "overrun", "fault: starve"}
INVERSION_RE = re.compile(r"^rtoscope step 3: .*inheritance (?P<pi>ON|OFF).*, M work (?P<m>\d+) ms")
PERIODIC_RE = re.compile(r"^rtoscope periodic: (?P<what>.*)$")
RUN_RE = re.compile(r"^# run (?P<k>\d+)/(?P<n>\d+)$")


def us(t):
    return round(t, 3)


def span(t0, t1):
    """ts and dur for a slice. Round the endpoints, then subtract: rounding
    ts and dur separately can push the end 1 ns past the next slice's start,
    and Perfetto then nests the next slice inside this one.
    """
    a, b = us(t0), us(t1)
    return {"ts": a, "dur": round(b - a, 3)}


def process_label(trace, k, headers):
    runs = [m for ln in trace.lines if (m := RUN_RE.match(ln))]
    return f"{headers}, run {runs[-1]['k']}/{runs[-1]['n']}" if runs else headers


def export_trace(trace, pid, label):
    """Chrome trace events for one trace."""
    events = [{"ph": "M", "pid": pid, "name": "process_name", "args": {"name": label}},
              {"ph": "M", "pid": pid, "tid": CPU_TID, "name": "thread_name",
               "args": {"name": "CPU0 (PRO CPU)"}},
              {"ph": "M", "pid": pid, "tid": CPU_TID, "name": "thread_sort_index",
               "args": {"sort_index": -1000}}]

    # Threads in priority order (lower number = higher priority = higher up).
    first_prio = {}
    for e in trace.events:
        first_prio.setdefault(e.thread, e.prio)
    threads = sorted(first_prio, key=lambda th: (first_prio[th], th))
    tid = {th: 2 + i for i, th in enumerate(threads)}
    has_app = {e.thread for e in trace.events if e.event == "app"}
    for i, th in enumerate(threads):
        tracks = [(tid[th], f"{th} (prio {first_prio[th]})", 2 * i)]
        if th in has_app:
            tracks.append((tid[th] + APP_TID_OFFSET, f"{th} app", 2 * i + 1))
        for t, name, idx in tracks:
            events.append({"ph": "M", "pid": pid, "tid": t, "name": "thread_name",
                           "args": {"name": name}})
            events.append({"ph": "M", "pid": pid, "tid": t, "name": "thread_sort_index",
                           "args": {"sort_index": idx}})

    # Thread state slices, and the CPU track from the running ones.
    for th, spans in tracelog.thread_states(trace).items():
        for state, t0, t1 in spans:
            if state not in STATE_NAMES:
                continue  # sleeping, dead: nothing to draw
            events.append({"ph": "X", "pid": pid, "tid": tid[th], **span(t0, t1), "name": STATE_NAMES[state]})
            if state == "running":
                events.append({"ph": "X", "pid": pid, "tid": CPU_TID, **span(t0, t1), "name": th})

    # Releases, priority counters, app slices and monitor instants.
    sleeping, job, opened = {}, {}, {}
    for e in trace.events:
        th = e.thread
        if e.event == "out":
            sleeping[th] = e.arg == "sleeping"
        elif e.event == "ready" and sleeping.get(th):
            sleeping[th] = False
            events.append({"ph": "i", "s": "t", "pid": pid, "tid": tid[th], "ts": us(e.t),
                           "name": "release"})
        elif e.event == "prio":
            events.append({"ph": "C", "pid": pid, "ts": us(e.t), "name": th,
                           "args": {"prio": e.prio}})
        elif e.event == "app":
            app = tid[th] + APP_TID_OFFSET
            if e.arg == "start":
                job[th] = job.get(th, 0) + 1
                opened[(th, "job")] = e.t
            elif e.arg == "done" and (th, "job") in opened:
                t0 = opened.pop((th, "job"))
                events.append({"ph": "X", "pid": pid, "tid": app, **span(t0, e.t), "name": f"job {job[th]}"})
            elif e.arg == "lock request":
                opened[(th, "lock")] = e.t
            elif e.arg == "locked" and (th, "lock") in opened:
                t0 = opened.pop((th, "lock"))
                events.append({"ph": "X", "pid": pid, "tid": app, **span(t0, e.t), "name": "waiting for lock"})
                opened[(th, "held")] = e.t
            elif e.arg == "unlock" and (th, "held") in opened:
                t0 = opened.pop((th, "held"))
                events.append({"ph": "X", "pid": pid, "tid": app, **span(t0, e.t), "name": "holds mutex"})
            elif e.arg in INSTANTS:
                events.append({"ph": "i", "s": "t", "pid": pid, "tid": app, "ts": us(e.t),
                               "name": e.arg})
        elif e.event == "mon" and e.arg in INSTANTS:
            # The snapshot records that follow name who ran and who owned the mutex.
            events.append({"ph": "i", "s": "t", "pid": pid, "tid": tid[th], "ts": us(e.t),
                           "name": e.arg, "args": {}})
        elif e.event == "mon" and events and events[-1].get("ph") == "i":
            key = "running" if e.arg == "running at alarm" else "mutex owner"
            events[-1]["args"][key] = f"{th} (prio {e.prio})"

    # Counters need a starting value, or Perfetto draws nothing before the
    # first change: every thread's priority at the start of the trace.
    t_first = trace.events[0].t if trace.events else 0.0
    events += [{"ph": "C", "pid": pid, "ts": us(t_first), "name": th,
                "args": {"prio": first_prio[th]}} for th in threads]
    return events


def export(log, out):
    traces = tracelog.parse_all(log)
    headers = next((f"inversion, PI {m['pi']}, M work {m['m']} ms" if "pi" in m.groupdict()
                    else f"periodic, {m['what']}"
                    for tr in traces for ln in tr.lines
                    if (m := INVERSION_RE.match(ln) or PERIODIC_RE.match(ln))), "trace")
    events = []
    for k, tr in enumerate(traces, 1):
        events += export_trace(tr, pid=k, label=process_label(tr, k, headers))
    doc = {"traceEvents": events, "displayTimeUnit": "ns",
           "otherData": {"source": Path(log).name, "cycle_clock_hz": traces[0].hz,
                         "trace_call_cycles": traces[0].call_cycles}}
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps(doc, separators=(",", ":")))
    return doc


EXAMPLES = ["step7_nopi_mon.log", "step7_pi_mon.log", "step5_periodic.log"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("log", nargs="?")
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()
    jobs = ([(Path(args.log), args.out or Path(args.log).with_suffix(".json"))] if args.log else
            [(ROOT / "logs" / n, ROOT / "docs" / "perfetto" / Path(n).with_suffix(".json").name)
             for n in EXAMPLES])
    for log, out in jobs:
        try:
            doc = export(log, out)
        except tracelog.TraceError as err:
            sys.exit(str(err))
        n = sum(1 for e in doc["traceEvents"] if e["ph"] != "M")
        print(f"wrote {out} ({n} events, {out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
