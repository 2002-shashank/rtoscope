"""Predicted vs measured response times for the periodic demo.

Usage:
    tools/.venv.nosync/bin/python tools/response_times.py [logs/step5_periodic.log]

Predictions per task:
  RTA      worst-case bound from the response-time recurrence
           R = C + sum over higher-priority j of ceil(R / T_j) * C_j
  RTA+ovh  the same with measured overheads (Burns & Wellings style):
           R = J + w,  w = C' + sum ceil((w + J_j) / T_j) * C'_j,  C' = Cmax + 2S
           J = worst release jitter (release -> first switch-in),
           S = worst context-switch gap (out -> in), Cmax = worst CPU per job
  sim      exact per-job prediction: an event-driven fixed-priority
           preemptive simulation of the same task set, synchronous release
  measured from the board trace: release ("ready" ending a sleep) to "done"

The task set (name, prio, C, T) is read from the log header the board
prints, so the model can't drift from the firmware.
"""

import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import tracelog

ROOT = Path(__file__).resolve().parent.parent

RUN_RE = re.compile(r"^rtoscope periodic: (?P<n>\d+) hyperperiods of (?P<hyper>\d+) ms$")
TASK_RE = re.compile(r"^# task,(?P<name>[^,]+),(?P<prio>-?\d+),(?P<c>\d+),(?P<t>\d+)$")


@dataclass
class Task:
    name: str
    prio: int       # Zephyr: lower number = higher priority
    c_us: float     # execution time per job
    t_us: int       # period (= implicit deadline)


def read_tasks(trace):
    tasks, run = [], None
    for ln in trace.lines:
        if m := RUN_RE.match(ln):
            run = (int(m["n"]), int(m["hyper"]) * 1000)
            tasks = []  # a reboot mid-capture starts over
        elif m := TASK_RE.match(ln):
            tasks.append(Task(m["name"], int(m["prio"]), int(m["c"]) * 1000, int(m["t"]) * 1000))
    if run is None or not tasks:
        raise tracelog.TraceError(f"{trace.path}: no periodic task table in the header")
    return sorted(tasks, key=lambda t: t.prio), run


def rta(tasks, c_of=lambda t: t.c_us, j_of=lambda t: 0.0):
    """Worst-case response time per task, or None if it misses its deadline.

    With j_of = 0 this is the textbook recurrence; with release jitter J the
    busy window w counts higher-priority jobs released up to J_j early.
    """
    out = {}
    for i, task in enumerate(tasks):
        higher = tasks[:i]
        w = c_of(task)
        while True:
            nxt = c_of(task) + sum(math.ceil((w + j_of(h)) / h.t_us) * c_of(h) for h in higher)
            if j_of(task) + nxt > task.t_us:
                out[task.name] = None
                break
            if nxt == w:
                out[task.name] = j_of(task) + w
                break
            w = nxt
    return out


def release_jitter(trace, task):
    """Worst delay from a release to the CPU serving this priority level.

    That is: from the "ready" ending a sleep to the next switch-in of any
    thread at this priority or better. Waiting for higher-priority work after
    that is interference, which the recurrence's sum already counts; only the
    ISR tail and switch before it are jitter.
    """
    worst, sleeping, ready_at = 0.0, False, None
    for e in trace.events:
        if e.thread == task.name and e.event == "out":
            sleeping = e.arg == "sleeping"
        elif e.thread == task.name and e.event == "ready" and sleeping:
            ready_at, sleeping = e.t, False
        elif e.event == "in" and ready_at is not None and e.prio <= task.prio:
            worst, ready_at = max(worst, e.t - ready_at), None
    return worst


def worst_switch_gap(trace):
    return max(b.t - a.t for a, b in zip(trace.events, trace.events[1:])
               if a.event == "out" and b.event == "in")


def simulate(tasks, horizon_us):
    """Exact fixed-priority preemptive schedule; per-task list of job response times."""
    releases = sorted({k * t.t_us for t in tasks for k in range(horizon_us // t.t_us)})
    pending = []    # [prio, release, remaining, name], one per unfinished job
    resp = {t.name: [] for t in tasks}
    now, ri = 0, 0
    while ri < len(releases) or pending:
        if not pending:
            now = max(now, releases[ri])
        while ri < len(releases) and releases[ri] <= now:
            for t in tasks:
                if releases[ri] % t.t_us == 0:
                    pending.append([t.prio, releases[ri], t.c_us, t.name])
            ri += 1
        pending.sort()
        job = pending[0]
        nxt = releases[ri] if ri < len(releases) else math.inf
        run = min(job[2], nxt - now)
        now += run
        job[2] -= run
        if job[2] == 0:
            resp[job[3]].append(now - job[1])
            pending.pop(0)
    return resp


def main():
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logs" / "step5_periodic.log"
    trace = tracelog.parse(path)
    tasks, (n_hyper, hyper_us) = read_tasks(trace)

    measured = {t.name: tracelog.jobs(trace, t.name) for t in tasks}
    sim = simulate(tasks, n_hyper * hyper_us)
    bound = rta(tasks)
    c_max = {t.name: max(j[3] for j in measured[t.name]) for t in tasks}
    jitter = {t.name: release_jitter(trace, t) for t in tasks}
    switch = worst_switch_gap(trace)
    bound_ovh = rta(tasks, c_of=lambda t: c_max[t.name] + 2 * switch,
                    j_of=lambda t: jitter[t.name])

    print(f"{path.relative_to(ROOT)}: {n_hyper} hyperperiods of {hyper_us / 1000:.0f} ms, "
          f"{len(trace.events)} records, trace call {trace.call_cycles} cycles")
    print(f"utilization {sum(t.c_us / t.t_us for t in tasks):.1%}, worst switch gap S = "
          f"{switch:.2f} µs, release jitter J = "
          + ", ".join(f"{t.name} {jitter[t.name]:.2f}" for t in tasks) + " µs\n")
    hdr = (f"{'task':4} {'C ms':>5} {'T ms':>5} {'jobs':>4} | {'RTA':>8} {'RTA+ovh':>9} | "
           f"{'sim min..max':>15} | {'meas min..max':>19} | {'max|meas-sim|':>13} "
           f"{'C overhead':>10} | {'RTA - worst':>11}")
    print(hdr)
    print("-" * len(hdr))
    ok = True
    for t in tasks:
        m_resp = [done - rel for rel, _, done, _ in measured[t.name]]
        s_resp = sim[t.name]
        if len(m_resp) != len(s_resp):
            sys.exit(f"{t.name}: {len(m_resp)} measured jobs but {len(s_resp)} simulated")
        err = max(abs(m - s) for m, s in zip(m_resp, s_resp))
        overhead = c_max[t.name] - t.c_us
        margin = bound[t.name] - max(m_resp)
        within = max(m_resp) <= bound_ovh[t.name]
        ok &= within
        print(f"{t.name:4} {t.c_us / 1000:5.0f} {t.t_us / 1000:5.0f} {len(m_resp):4} | "
              f"{bound[t.name] / 1000:8.3f} {bound_ovh[t.name] / 1000:9.3f} | "
              f"{min(s_resp) / 1000:7.3f}..{max(s_resp) / 1000:7.3f} | "
              f"{min(m_resp) / 1000:9.3f}..{max(m_resp) / 1000:9.3f} | "
              f"{err:10.1f} µs {overhead:7.1f} µs | {margin:8.1f} µs"
              f"   meas <= RTA+ovh: {'yes' if within else 'NO'}")
    print("\nunits: ms unless marked. C overhead = worst measured CPU per job - nominal C.")
    print("every measured response <= RTA+ovh:", "yes" if ok else "NO")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
