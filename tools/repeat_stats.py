"""Statistics over repeated runs of the periodic demo.

Usage:
    tools/.venv.nosync/bin/python tools/repeat_stats.py [logs/step6_repeat.log]

The board (CONFIG_RTOSCOPE_PERIODIC_RUNS) dumps one trace per run. For every
job in every run this computes:
  excess    measured response - simulated response (µs): the overhead the
            nominal model leaves out
  jitter    release -> CPU serving that priority level (µs)
and, for every context switch, the out -> in gap. Prints n / min / mean /
p50 / p99 / max, compares run 1 (cold caches) with the rest, checks every job
against the overhead-aware RTA bound using the worst J, S and Cmax over all
runs, and writes histograms to docs/step6_histograms.png.
"""

import re
import sys
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

import response_times as rt
import tracelog

ROOT = Path(__file__).resolve().parent.parent

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
C_BAR = "#2a78d6"

RUN_NO_RE = re.compile(r"^# run (?P<k>\d+)/(?P<n>\d+)$")


def run_number(trace, fallback):
    nos = [int(m["k"]) for ln in trace.lines if (m := RUN_NO_RE.match(ln))]
    return nos[-1] if nos else fallback


def summary(xs):
    a = np.asarray(xs)
    return dict(n=len(a), min=a.min(), mean=a.mean(), p50=np.percentile(a, 50),
                p99=np.percentile(a, 99), max=a.max())


def fmt_row(label, xs):
    s = summary(xs)
    return (f"{label:28} {s['n']:5d} {s['min']:8.2f} {s['mean']:8.2f} {s['p50']:8.2f} "
            f"{s['p99']:8.2f} {s['max']:8.2f}")


def analyze(path):
    """Collect per-job and per-switch measurements over every intact run.

    strict=False: one run corrupted in transit is excluded and reported,
    rather than discarding the rest. Raises TraceError if no run is intact
    or a run's job count disagrees with the simulation.
    """
    all_traces = tracelog.parse_all(path, strict=False)
    tasks, (n_hyper, hyper_us) = rt.read_tasks(all_traces[0])
    numbered = [(run_number(tr, k), tr) for k, tr in enumerate(all_traces, 1)]
    bad = [(k, tr.error) for k, tr in numbered if tr.error]
    good = [(k, tr) for k, tr in numbered if not tr.error]
    if not good:
        raise tracelog.TraceError("no intact runs:\n" + "\n".join(err for _, err in bad))
    sim = rt.simulate(tasks, n_hyper * hyper_us)

    excess = {t.name: [] for t in tasks}        # per job, all runs
    excess_by_run = {t.name: [] for t in tasks} # per run: max excess
    jitter = {t.name: [] for t in tasks}
    gaps = {"preempted": [], "voluntary": []}
    resp_max = {t.name: 0.0 for t in tasks}
    c_max = {t.name: 0.0 for t in tasks}

    for run, trace in good:
        for t in tasks:
            jobs = tracelog.jobs(trace, t.name)
            if len(jobs) != len(sim[t.name]):
                raise tracelog.TraceError(f"run {run}: {t.name} has {len(jobs)} jobs, "
                                          f"simulation {len(sim[t.name])}")
            ex = [(done - rel) - s for (rel, _, done, _), s in zip(jobs, sim[t.name])]
            excess[t.name] += ex
            excess_by_run[t.name].append(max(ex))
            jitter[t.name] += rt.release_jitters(trace, t)
            resp_max[t.name] = max(resp_max[t.name], max(done - rel for rel, _, done, _ in jobs))
            c_max[t.name] = max(c_max[t.name], max(cpu for *_, cpu in jobs))
        for kind, gap in rt.switch_gaps(trace):
            gaps[kind].append(gap)

    # Overhead-aware RTA with the worst J, S and Cmax seen in ANY run: the
    # observed WCET over many runs, not one.
    s_worst = max(max(xs) for xs in gaps.values())
    bound = rt.rta(tasks, c_of=lambda t: c_max[t.name] + 2 * s_worst,
                   j_of=lambda t: max(jitter[t.name]))
    return dict(tasks=tasks, n_hyper=n_hyper, hyper_us=hyper_us, good=good, bad=bad,
                excess=excess, excess_by_run=excess_by_run, jitter=jitter, gaps=gaps,
                resp_max=resp_max, s_worst=s_worst, bound=bound,
                within={t.name: resp_max[t.name] <= bound[t.name] for t in tasks})


def main():
    path = (Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "logs" / "step6_repeat.log").resolve()
    try:
        a = analyze(path)
    except tracelog.TraceError as err:
        sys.exit(str(err))
    tasks, good = a["tasks"], a["good"]
    traces = [tr for _, tr in good]

    calls = {tr.call_cycles for tr in traces}
    print(f"{path.relative_to(ROOT)}: {len(traces)} intact runs x {a['n_hyper']} hyperperiod(s) "
          f"of {a['hyper_us'] / 1000:.0f} ms, trace call {'/'.join(map(str, sorted(calls)))} cycles")
    for k, err in a["bad"]:
        print(f"  excluded run {k}: {err.split(': ', 2)[-1]}")
    print()
    hdr = f"{'µs':28} {'n':>5} {'min':>8} {'mean':>8} {'p50':>8} {'p99':>8} {'max':>8}"
    print(hdr)
    print("-" * len(hdr))
    for t in tasks:
        print(fmt_row(f"{t.name} response - simulated", a["excess"][t.name]))
    for t in tasks:
        print(fmt_row(f"{t.name} release jitter", a["jitter"][t.name]))
    for kind, xs in a["gaps"].items():
        print(fmt_row(f"switch out->in, {kind}", xs))

    if len(traces) > 1 and good[0][0] == 1:
        print("\nrun 1 (cold caches) vs runs 2..N, worst excess per run (µs):")
        for t in tasks:
            by_run = a["excess_by_run"][t.name]
            print(f"  {t.name}: run 1 {by_run[0]:7.2f}   runs 2..N max "
                  f"{max(by_run[1:]):7.2f}   mean {np.mean(by_run[1:]):7.2f}")

    print(f"\noverhead-aware RTA (all-run worst J, S = {a['s_worst']:.2f} µs, Cmax):")
    for t in tasks:
        print(f"  {t.name}: worst response {a['resp_max'][t.name] / 1000:9.3f} ms  bound "
              f"{a['bound'][t.name] / 1000:9.3f} ms  "
              f"{'ok' if a['within'][t.name] else 'EXCEEDED'}")

    plot(tasks, a["excess"], a["jitter"], a["gaps"], len(traces),
         ROOT / "docs" / "step6_histograms.png")
    sys.exit(0 if all(a["within"].values()) else 1)


def panel(ax, xs, title):
    s = summary(xs)
    # Values are whole CPU cycles (1/240 µs); a range below one cycle is a
    # single value plus float noise, which hist() would draw as fake bins.
    if s["max"] - s["min"] < 1 / 240:
        ax.bar([s["min"]], [s["n"]], width=0.05, color=C_BAR)
        ax.set_xlim(s["min"] - 0.5, s["min"] + 0.5)
        ax.ticklabel_format(axis="x", useOffset=False)
        ax.annotate(f"all {s['n']} identical: {s['min']:.4f} µs", xy=(0.03, 0.95),
                    xycoords="axes fraction", ha="left", va="top", fontsize=8.5, color=INK)
    else:
        ax.hist(xs, bins=40, color=C_BAR, edgecolor=SURFACE, linewidth=0.6)
        marks = [(s["p99"], "p99", "--"), (s["max"], "max", "-")]
        if s["max"] - s["p99"] < 1 / 240:
            marks = [(s["max"], "p99 = max", "-")]
        for x, label, style in marks:
            ax.axvline(x, color=INK, linewidth=1, linestyle=style)
            ax.annotate(f"{label} {x:.1f}", xy=(x, 1), xycoords=("data", "axes fraction"),
                        xytext=(-3, -3), textcoords="offset points", ha="right", va="top",
                        fontsize=7.5, color=INK)
    ax.set_title(f"{title}  (n={s['n']}, p50 {s['p50']:.1f})", loc="left", fontsize=9.5,
                 color=INK)
    ax.set_xlabel("µs", color=INK_2, fontsize=8.5)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=8, length=0)
    ax.set_facecolor(SURFACE)


def plot(tasks, excess, jitter, gaps, n_runs, out):
    fig, axes = plt.subplots(2, 3, figsize=(15, 7.2), facecolor=SURFACE)
    for ax, t in zip(axes[0], tasks):
        panel(ax, excess[t.name], f"{t.name}: response - simulated")
    panel(axes[1][0], [x for t in tasks for x in jitter[t.name]], "release jitter, all tasks")
    panel(axes[1][1], gaps["preempted"], "switch out→in, preemption")
    panel(axes[1][2], gaps["voluntary"], "switch out→in, voluntary")
    axes[0][0].set_ylabel("jobs", color=INK_2, fontsize=8.5)
    axes[1][0].set_ylabel("count", color=INK_2, fontsize=8.5)
    fig.suptitle(f"Periodic demo on ESP32, {n_runs} runs: overhead the nominal model leaves out",
                 x=0.01, ha="left", fontsize=13, color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print(f"\nwrote {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
