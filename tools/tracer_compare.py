"""Compare the custom tracer (trace.c) with Zephyr's CTF tracer on the same workload.

Usage:
    tools/.venv.nosync/bin/python tools/tracer_compare.py [logs/step8_{none,user,ctf,ctf-noisr}.log]

Each log is one hyperperiod of the periodic demo built with a different
tracer (CONFIG_RTOSCOPE_PROBE). Reports, per tracer:
  hook cost      cycles per sys_trace_k_thread_switched_in() call (board bench)
  buffer use     bytes per event, events per run, run length a 32 KB buffer holds
  perturbation   each job's finish time vs the same job with no tracer, from
                 the tracer-independent probe (finish = done - H's wake at the
                 same release instant)
  information    scheduling events each tracer recorded for the same schedule
"""

import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import ctf_decode
import response_times as rt
import tracelog

ROOT = Path(__file__).resolve().parent.parent
TRACERS = ["none", "user", "ctf", "ctf-noisr"]

PROBE_RE = re.compile(r"^# rtoscope probe, cycle clock (?P<hz>\d+) Hz, (?P<jobs>\d+) jobs$")
ROW_RE = re.compile(r"^(?P<task>[^,]+),(?P<job>\d+),(?P<wake>\d+),(?P<done>\d+)$")
BENCH_RE = re.compile(r"^# bench: tracer (?P<tracer>\S+), switched_in hook cycles: min (?P<min>\d+), "
                      r"median (?P<median>\d+), max (?P<max>\d+)")
USER_RECORD_BYTES = 16   # struct rec in app/src/trace.c
RING_BYTES = 32 * 1024   # both: trace.c 2048 x 16 B, CTF CONFIG_RAM_TRACING_BUFFER_SIZE


def lines_of(path):
    return [ln.strip() for ln in Path(path).read_text(errors="replace").splitlines()]


def read_probe(path):
    """task -> {job: (wake_us, done_us)}, CRC-checked."""
    lines = lines_of(path)
    start = next((i for i, ln in enumerate(lines) if PROBE_RE.match(ln)), None)
    if start is None:
        raise ctf_decode.CtfError(f"{path}: no probe dump")
    m, body = ctf_decode.read_crc_block(lines, start, PROBE_RE)
    hz = int(m["hz"])
    out = {}
    for ln in body:
        if r := ROW_RE.match(ln):
            out.setdefault(r["task"], {})[int(r["job"])] = (int(r["wake"]) * 1e6 / hz,
                                                            int(r["done"]) * 1e6 / hz)
    if sum(len(v) for v in out.values()) != int(m["jobs"]):
        raise ctf_decode.CtfError(f"{path}: probe header says {m['jobs']} jobs")
    return out


def finish_times(probe, periods):
    """task -> [finish µs per job], measured from H's wake at that release instant."""
    h_period = periods["H"]
    out = {}
    for task, jobs in probe.items():
        out[task] = []
        for job, (_, done) in sorted(jobs.items()):
            release = (job - 1) * periods[task]          # µs after the first release
            h_job = release // h_period + 1               # H is released at the same instant
            out[task].append(done - probe["H"][h_job][0])
    return out


def bench(path):
    for ln in lines_of(path):
        if b := BENCH_RE.match(ln):
            return {k: int(b[k]) for k in ("min", "median", "max")}
    return None


def periods_of(path):
    tasks = [ln for ln in lines_of(path) if ln.startswith("# task,") and ln != "# task,name,prio,work_ms,period_ms"]
    return {t.split(",")[1]: int(t.split(",")[4]) * 1000 for t in tasks}


def information(path, tracer):
    """Counts of the scheduling facts both tracers can express, plus everything recorded."""
    if tracer == "user":
        evs = tracelog.parse(path).events
        names = Counter(e.event for e in evs)
        sched = {"switch in": names["in"], "switch out": names["out"],
                 "ready": names["ready"], "pend": names["pend"], "prio": names["prio"]}
        return len(evs), len(evs) * USER_RECORD_BYTES, sched, names
    if tracer.startswith("ctf"):
        dump = ctf_decode.parse(path)
        names = Counter(e.name for e in dump.events)
        sched = {"switch in": names["thread_switched_in"], "switch out": names["thread_switched_out"],
                 "ready": names["thread_sched_ready"], "pend": names["thread_sched_pend"],
                 "prio": names["thread_priority_set"] + names["thread_sched_priority_set"]}
        return len(dump.events), sum(e.size for e in dump.events), sched, names
    return 0, 0, {}, Counter()


def analyze(paths):
    periods = periods_of(paths["none"])
    base = finish_times(read_probe(paths["none"]), periods)
    out = {}
    for tracer, path in paths.items():
        fin = finish_times(read_probe(path), periods)
        # Job 1 of each task runs from a cold flash cache, and each build
        # reaches it with a different cache state (different code layout, and
        # trace.c's calibration warms shared paths just before the release).
        # Comparing job 1 across builds measures cache luck, so the steady-
        # state delta uses jobs 2..n; job 1 is reported on its own.
        delta = {t: [a - b for a, b in zip(fin[t][1:], base[t][1:])] for t in fin}
        first = {t: fin[t][0] - base[t][0] for t in fin}
        n, nbytes, sched, names = information(path, tracer)
        out[tracer] = dict(bench=bench(path), finish=fin, delta=delta, first=first, events=n,
                           bytes=nbytes, sched=sched, names=names)
    return out


SURFACE, INK, INK_2, GRID, C_BAR = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6"


def plot(res, out):
    """Two single-series panels: hook cost, and how much later L's jobs finish."""
    tracers = [t for t in res if res[t]["bench"]]
    fig, axes = plt.subplots(1, 2, figsize=(11, 3.6), facecolor=SURFACE)
    panels = [
        (axes[0], [res[t]["bench"]["median"] for t in tracers], "cycles",
         "switch hook cost (median of 64 calls)", "{:.0f}"),
        (axes[1], [statistics.mean(res[t]["delta"]["L"]) for t in tracers], "µs",
         "L jobs finish later than with no tracer (steady-state mean)", "{:.1f}"),
    ]
    for ax, vals, unit, title, fmt in panels:
        bars = ax.barh(tracers, vals, color=C_BAR, height=0.5)
        for b, v in zip(bars, vals):
            ax.annotate(fmt.format(v) + f" {unit}", xy=(v, b.get_y() + b.get_height() / 2),
                        xytext=(4, 0), textcoords="offset points", va="center",
                        fontsize=9, color=INK)
        ax.set_title(title, loc="left", fontsize=10.5, color=INK)
        ax.invert_yaxis()
        ax.set_xlim(0, max(vals) * 1.3)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.spines["bottom"].set_color(GRID)
        ax.tick_params(colors=INK_2, length=0)
        ax.set_facecolor(SURFACE)
    fig.suptitle("Custom tracer (trace.c) vs Zephyr CTF (sync, RAM backend) on ESP32, same workload",
                 x=0.01, ha="left", fontsize=12, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print(f"\nwrote {out.relative_to(ROOT)}")


def main():
    paths = ({t: Path(p).resolve() for t, p in zip(TRACERS, sys.argv[1:5])} if len(sys.argv) > 4
             else {t: ROOT / "logs" / f"step8_{t}.log" for t in TRACERS})
    try:
        res = analyze(paths)
    except (ctf_decode.CtfError, tracelog.TraceError) as err:
        sys.exit(str(err))

    print("1. Hook cost: sys_trace_k_thread_switched_in(), 64 calls, IRQs locked (cycles; µs at 240 MHz)")
    for t, r in res.items():
        b = r["bench"]
        print(f"   {t:10} " + (f"min {b['min']:4}  median {b['median']:4}  max {b['max']:4}"
                                 f"   = {b['median'] / 240:.2f} µs" if b else "no tracer"))

    print("\n2. Buffer use for one 3 s hyperperiod")
    for t, r in res.items():
        if r["events"]:
            per = r["bytes"] / r["events"]
            print(f"   {t:10} {r['events']:4} events, {r['bytes']:5} bytes, {per:5.1f} B/event; "
                  f"32 KB holds {RING_BYTES / r['bytes'] * 3:5.1f} s of this workload")

    print("\n3. Perturbation: job finish time minus the same job with no tracer (µs),"
          " steady state (jobs 2..n)")
    for t, r in res.items():
        if t == "none":
            continue
        cells = [f"{task} mean {statistics.mean(d):6.1f} max {max(d):6.1f}" for task, d in r["delta"].items()]
        print(f"   {t:10} " + "   ".join(cells))
    print("   first job of each task (cold flash cache; layout-dependent, not tracer cost):")
    for t, r in res.items():
        if t != "none":
            print(f"   {t:10} " + "   ".join(f"{task} {v:+6.1f}" for task, v in r["first"].items()))

    print("\n4. Scheduling facts recorded for the same schedule")
    keys = ["switch in", "switch out", "ready", "pend", "prio"]
    print("   " + " " * 10 + "".join(f"{k:>12}" for k in keys))
    for t, r in res.items():
        if r["sched"]:
            print(f"   {t:10}" + "".join(f"{r['sched'][k]:12}" for k in keys))
    for t in ("ctf",):
        only = sorted(set(res[t]["names"]) - {"thread_switched_in", "thread_switched_out",
                                              "thread_sched_ready", "thread_sched_pend"})
        print(f"   only in {t}: " + ", ".join(f"{n} ({res[t]['names'][n]})" for n in only))
    print("   only in user: why each thread left the CPU (preempted/pending/sleeping/dead),"
          " app labels")
    print("   neither: ISR events (no sys_trace_isr_* calls in arch/xtensa in v4.4.2)")

    plot(res, ROOT / "docs" / "step8_tracer_compare.png")


if __name__ == "__main__":
    main()
