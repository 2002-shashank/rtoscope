"""Regression checks on the committed board captures (logs/).

These pin down what the board measured, so a change to the tools that
silently alters a result fails here.
"""

import pytest

import plot_timeline
import repeat_stats
import response_times
import tracelog
from conftest import LOGS

# Every capture that contains a trace.c dump (step 2/3 logs predate the
# tracer; step 8's none/CTF builds use no tracer or Zephyr's).
TRACED = sorted(p for p in LOGS.glob("step*_*.log")
                if "# rtoscope trace, cycle clock" in p.read_text(errors="replace"))
INVERSION = sorted(LOGS.glob("step4_*.log"))


@pytest.mark.parametrize("path", TRACED, ids=lambda p: p.name)
def test_every_dump_is_intact(path):
    traces = tracelog.parse_all(path)  # strict: CRC, count and drops all checked
    assert traces


@pytest.mark.parametrize("path", TRACED, ids=lambda p: p.name)
def test_single_core_trace_invariants(path):
    """One CPU: time never goes backwards, and exactly the switched-in thread runs."""
    for k, tr in enumerate(tracelog.parse_all(path), 1):
        running = "main"  # trace_start() is called from main
        prev_t = 0.0
        for e in tr.events:
            assert e.t >= prev_t, f"trace {k}: time went backwards at {e}"
            prev_t = e.t
            if e.event == "out":
                assert e.thread == running, f"trace {k}: {e.thread} switched out while {running} ran"
                running = None
            elif e.event == "in":
                assert running is None, f"trace {k}: {e.thread} switched in while {running} ran"
                running = e.thread
            elif e.event == "app":
                assert e.thread == running, f"trace {k}: app event from {e.thread}, not {running}"


@pytest.mark.parametrize("path", INVERSION, ids=lambda p: p.name)
def test_inversion_blocking_time(path):
    """Without PI, H waits for L's remaining 40 ms plus all of M; with PI only 40 ms."""
    run = plot_timeline.parse_log(path)
    blocked = plot_timeline.h_block_ms(run)
    if "inheritance ON" in run.mode:
        assert blocked == pytest.approx(40.0, abs=0.5)
    else:
        assert blocked == pytest.approx(40.0 + run.m_work_ms, abs=0.5)


@pytest.mark.parametrize("path", INVERSION, ids=lambda p: p.name)
def test_priority_inheritance_is_visible_in_the_trace(path):
    run = plot_timeline.parse_log(path)
    l_prio_changes = [e.prio for e in run.events if e.event == "prio" and e.thread == "L"]
    if "inheritance ON" in run.mode:
        assert l_prio_changes == [5, 9]   # boosted to H's priority, then restored
    else:
        assert l_prio_changes == []       # PI compiled out: no prio_set at all


def test_periodic_single_run_within_overhead_aware_bound():
    tasks, info, rows = response_times.analyze(tracelog.parse(LOGS / "step5_periodic.log"))
    assert {r["task"].name: r["rta"] for r in rows} == {"H": 15_000, "M": 55_000, "L": 265_000}
    for r in rows:
        assert r["within"], f"{r['task'].name}: {r['meas_max']} µs > {r['rta_ovh']} µs"
        # Textbook RTA misses only by overhead: microseconds, never milliseconds.
        assert 0 < r["meas_max"] - r["rta"] < 200
        # ...and the overhead-aware bound is tight, not just safe: a loose model
        # (e.g. counting interference as jitter, a real bug in step 1) still
        # "holds" but overshoots by milliseconds.
        assert r["rta_ovh"] - r["meas_max"] < 50, f"{r['task'].name}: bound loose by " \
            f"{r['rta_ovh'] - r['meas_max']:.0f} µs"


def test_repeat_runs_all_intact_and_within_bound():
    a = repeat_stats.analyze(LOGS / "step6_repeat.log")
    assert len(a["good"]) == 100 and a["bad"] == []
    assert all(a["within"].values()), a["within"]
    for t in a["tasks"]:
        assert a["bound"][t.name] - a["resp_max"][t.name] < 50, f"{t.name}: bound loose"
    assert len(a["excess"]["H"]) == 3000 and len(a["excess"]["L"]) == 300
    # Every preemption switch is the same 237 cycles (finding from step 2).
    assert max(a["gaps"]["preempted"]) - min(a["gaps"]["preempted"]) < 1 / 240
