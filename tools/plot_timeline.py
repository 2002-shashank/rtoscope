"""Plot per-thread timelines from the scheduler trace (goal 4 board logs).

Usage:
    tools/.venv.nosync/bin/python tools/plot_timeline.py            # default pairs -> docs/
    tools/.venv.nosync/bin/python tools/plot_timeline.py A.log B.log -o docs/out.png

The board (app/src/trace.c) records every context switch ("in"/"out"), wake-up
("ready"), pend and priority change from Zephyr's tracing hooks, plus app
events (lock request / locked / unlock / done), stamped with CCOUNT. Every
state drawn here comes from those records; nothing is inferred.
"""

import argparse
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

import tracelog

ROOT = Path(__file__).resolve().parent.parent

ROWS = ["H", "M", "L"]  # top to bottom, highest priority first

# Light-mode surface/ink and state colors (validated: blue/orange/aqua, all-pairs).
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e4e3df"
C_RUNNING = "#2a78d6"
C_BLOCKED = "#eb6834"
C_LOCK = "#1baf7a"
C_READY = "#c9c8c2"

HEADER_RE = re.compile(r"rtoscope step 3: (?P<mode>.*?), M work (?P<mwork>\d+) ms")


@dataclass
class Run:
    path: Path
    mode: str
    m_work_ms: int
    hz: int
    call_cycles: int
    events: list


def parse_log(path):
    """The step 3 run header plus the trace (parsed by tracelog)."""
    try:
        trace = tracelog.parse(path)
    except tracelog.TraceError as err:
        sys.exit(str(err))
    headers = [m for ln in trace.lines if (m := HEADER_RE.search(ln))]
    if not headers:
        sys.exit(f"{path}: no step 3 run header")
    m = headers[-1]
    return Run(trace.path, m["mode"], int(m["mwork"]), trace.hz, trace.call_cycles, trace.events)


def app_time(run, thread, label):
    return next(e.t for e in run.events
                if e.thread == thread and e.event == "app" and e.arg == label)


def reconstruct(run):
    """Turn the record stream into per-thread state intervals (µs).

    Each thread is a small state machine driven by its own records:
      in                 -> running
      out, arg=ready     -> ready     (preempted: still runnable)
      out, arg=pending   -> blocked   (waiting on a kernel object)
      out, arg=sleeping  -> sleeping  (k_sleep; not drawn)
      out, arg=dead      -> gone
      ready              -> ready     (woken; a release if it was sleeping)
    """
    spans = {th: {"running": [], "ready": [], "blocked": []} for th in ROWS}
    state, since = {}, {}
    releases = {th: [] for th in ROWS}
    base_prio, prio_now, boosts = {}, {}, {th: [] for th in ROWS}
    holding = {th: [] for th in ROWS}
    lock_at = {}

    def enter(th, new, t):
        old = state.get(th)
        if old in spans[th]:
            spans[th][old].append((since[th], t))
        state[th], since[th] = new, t

    for e in run.events:
        th = e.thread
        if th not in spans:
            continue
        base_prio.setdefault(th, e.prio)
        if e.event == "in":
            enter(th, "running", e.t)
        elif e.event == "out":
            enter(th, {"ready": "ready", "pending": "blocked",
                       "sleeping": "sleeping", "dead": "dead"}.get(e.arg, e.arg), e.t)
        elif e.event == "ready":
            if state.get(th) == "sleeping":
                releases[th].append(e.t)
            enter(th, "ready", e.t)
        elif e.event == "prio":
            # k_mutex inheritance: prio_set to a better (lower) number than
            # the thread's own starts a boost; setting it back ends it.
            if e.prio < base_prio[th] and th not in prio_now:
                prio_now[th] = (e.t, e.prio)
            elif e.prio >= base_prio[th] and th in prio_now:
                t0, p = prio_now.pop(th)
                boosts[th].append((t0, e.t, p))
        elif e.event == "app" and e.arg == "locked":
            lock_at[th] = e.t
        elif e.event == "app" and e.arg == "unlock" and th in lock_at:
            holding[th].append((lock_at.pop(th), e.t))

    return {"spans": spans, "releases": releases, "base_prio": base_prio,
            "boosts": boosts, "holding": holding}


def switch_stats(run):
    """Context-switch cost: gap between each 'out' and the 'in' right after it."""
    gaps = {"preempted": [], "blocked/slept/exited": []}
    for a, b in zip(run.events, run.events[1:]):
        if a.event == "out" and b.event == "in":
            kind = "preempted" if a.arg == "ready" else "blocked/slept/exited"
            gaps[kind].append(b.t - a.t)
    return gaps


def h_block_ms(run):
    return (app_time(run, "H", "locked") - app_time(run, "H", "lock request")) / 1000


def draw(ax, run, st, origin, xmax_ms):
    ms = lambda spans: [((t0 - origin) / 1000, (t1 - t0) / 1000) for t0, t1 in spans]
    rel = lambda t: (t - origin) / 1000
    edge = dict(edgecolor=SURFACE, linewidth=1)  # surface gap between adjacent fills

    for row, th in enumerate(ROWS):
        y = len(ROWS) - 1 - row
        sp = st["spans"][th]
        ax.broken_barh(ms(sp["ready"]), (y - 0.07, 0.14), facecolor=C_READY, **edge)
        ax.broken_barh(ms(sp["blocked"]), (y - 0.25, 0.5), facecolor=C_BLOCKED, **edge)
        ax.broken_barh(ms(sp["running"]), (y - 0.25, 0.5), facecolor=C_RUNNING, **edge)
        ax.broken_barh(ms(st["holding"][th]), (y - 0.40, 0.09), facecolor=C_LOCK, **edge)
        for t0, _ in st["holding"][th]:
            ax.text(rel(t0) + 1, y - 0.45, "holds lock", color=INK_2, fontsize=7.5, va="top")
        for t in st["releases"][th]:
            ax.plot(rel(t), y + 0.33, marker="v", markersize=7, color=INK_2, linestyle="none")
        for t0, t1, prio in st["boosts"][th]:
            # Right of the boosted span, above the gray line: clear of the
            # release marker even when the span is only a few pixels wide.
            ax.annotate(f"← boosted {st['base_prio'][th]} → {prio} (measured prio_set)",
                        xy=(rel(t1), y + 0.05), xytext=(4, 0), textcoords="offset points",
                        ha="left", va="bottom", fontsize=8, color=INK)

    req, got = rel(app_time(run, "H", "lock request")), rel(app_time(run, "H", "locked"))
    yh = len(ROWS) - 1
    ax.annotate("", xy=(got, yh + 0.40), xytext=(req, yh + 0.40),
                arrowprops=dict(arrowstyle="<->", color=INK, lw=1, shrinkA=0, shrinkB=0))
    ax.text((req + got) / 2, yh + 0.45, f"H blocked {got - req:.1f} ms",
            ha="center", va="bottom", fontsize=8.5, color=INK)

    # Match the whole phrase: the OFF header also contains "CONFIG_..." ("ON").
    on = "inheritance ON" in run.mode
    ax.set_title(f"Priority inheritance {'ON' if on else 'OFF'}  ·  H blocked "
                 f"{h_block_ms(run):.1f} ms", loc="left", fontsize=11, color=INK)
    ax.set_yticks(range(len(ROWS)))
    ax.set_yticklabels([f"{th}  (prio {st['base_prio'][th]})" for th in reversed(ROWS)],
                       color=INK)
    ax.set_ylim(-0.7, len(ROWS) - 0.2)
    ax.set_xlim(-5, xmax_ms)
    ax.set_xlabel("time since L's release (ms)", color=INK_2)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK_2, length=0)
    ax.set_facecolor(SURFACE)


def report(run):
    on = "ON" if "inheritance ON" in run.mode else "OFF"
    print(f"{run.path.relative_to(ROOT)}: PI {on}, M {run.m_work_ms} ms, "
          f"{len(run.events)} records, trace call {run.call_cycles} cycles "
          f"({run.call_cycles * 1e6 / run.hz:.2f} µs)")
    for kind, gaps in switch_stats(run).items():
        if gaps:
            print(f"  switch out->in ({kind:>20}): n={len(gaps):2d}  min {min(gaps):5.2f}  "
                  f"median {statistics.median(gaps):5.2f}  max {max(gaps):5.2f} µs")


def plot_pair(left, right, out):
    runs = [parse_log(Path(p)) for p in (left, right)]
    states = [reconstruct(r) for r in runs]
    for run in runs:
        report(run)

    # Origin: L's release in each run, measured (the timer ISR's "ready").
    origins = [st["releases"]["L"][0] for st in states]
    xmax = max(max(e.t for e in r.events if e.thread in ROWS) - o
               for r, o in zip(runs, origins)) / 1000 * 1.04
    fig, axes = plt.subplots(1, 2, figsize=(15, 4.4), sharey=True, facecolor=SURFACE)
    for ax, run, st, origin in zip(axes, runs, states, origins):
        draw(ax, run, st, origin, xmax)
    fig.suptitle(f"Priority inversion on ESP32 (Zephyr k_mutex), M works {runs[0].m_work_ms} ms"
                 f"  ·  measured from the scheduler trace", x=0.01, ha="left",
                 fontsize=13, color=INK)
    fig.legend(handles=[
        Patch(facecolor=C_RUNNING, label="running"),
        Patch(facecolor=C_BLOCKED, label="blocked (pending)"),
        Patch(facecolor=C_READY, label="ready, preempted"),
        Patch(facecolor=C_LOCK, label="holds the mutex"),
        plt.Line2D([], [], marker="v", color=INK_2, linestyle="none", label="release"),
    ], loc="upper right", ncol=5, frameon=False, fontsize=9, labelcolor=INK,
        bbox_to_anchor=(1, 0.94))
    fig.tight_layout(rect=(0, 0, 1, 0.88))
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    print(f"wrote {out.relative_to(ROOT)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logs", nargs="*", help="left and right log (PI off, PI on)")
    ap.add_argument("-o", "--out", type=Path)
    args = ap.parse_args()

    if args.logs:
        if len(args.logs) != 2 or not args.out:
            ap.error("give exactly two logs and -o OUT.png")
        plot_pair(*args.logs, args.out)
        return

    logs = ROOT / "logs"
    plot_pair(logs / "step4_nopi.log", logs / "step4_pi.log",
              ROOT / "docs" / "step4_timeline.png")
    plot_pair(logs / "step4_nopi_m400.log", logs / "step4_pi_m400.log",
              ROOT / "docs" / "step4_timeline_m400.png")


if __name__ == "__main__":
    main()
