"""The Chrome/Perfetto JSON exporter: structure, and Perfetto's own parser on the output."""

import json

import pytest

import perfetto_export as pe
import plot_timeline
import tracelog
from conftest import LOGS


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    out = tmp_path_factory.mktemp("perfetto")

    def export(name):
        path = out / f"{name}.json"
        if not path.exists():
            pe.export(LOGS / name, path)
        return path
    return export


@pytest.mark.parametrize("name", ["step7_nopi_mon.log", "step5_periodic.log", "step6_repeat.log"])
def test_structure(exported, name):
    doc = json.loads(exported(name).read_text())
    events = doc["traceEvents"]
    named = {(e["pid"], e["tid"]) for e in events if e["ph"] == "M" and e["name"] == "thread_name"}
    tracks = {}
    for e in events:
        if e["ph"] == "X":
            assert e["dur"] >= 0 and isinstance(e["ts"], (int, float))
            assert (e["pid"], e["tid"]) in named, e
            tracks.setdefault((e["pid"], e["tid"]), []).append((e["ts"], e["ts"] + e["dur"]))
    # Chrome JSON: slices on one track must nest (or follow each other), never overlap partially.
    for key, spans in tracks.items():
        stack = []
        for t0, t1 in sorted(spans, key=lambda s: (s[0], -s[1])):
            while stack and stack[-1] <= t0 + 1e-6:
                stack.pop()
            assert not stack or t1 <= stack[-1] + 1e-6, f"partial overlap on {key} at {t0}"
            stack.append(t1)


def test_one_process_per_run(exported):
    doc = json.loads(exported("step6_repeat.log").read_text())
    names = [e["args"]["name"] for e in doc["traceEvents"] if e["name"] == "process_name"]
    assert len(names) == 100 and names[0].endswith("run 1/100") and names[-1].endswith("run 100/100")


# --- Perfetto's trace processor -------------------------------------------------

@pytest.fixture(scope="module")
def tp(exported):
    tp_mod = pytest.importorskip("perfetto.trace_processor")
    opened = {}

    def load(name):
        if name not in opened:
            try:
                opened[name] = tp_mod.TraceProcessor(trace=str(exported(name)))
            except Exception as err:  # no network for the binary download, etc.
                pytest.skip(f"trace_processor unavailable: {err}")
        return opened[name]
    yield load
    for t in opened.values():
        t.close()


def slice_ms(tp, thread_prefix, slice_name):
    rows = list(tp.query(f"""
        select sum(s.dur) / 1e6 as ms, count(*) as n from slice s
        join thread_track tt on s.track_id = tt.id join thread t using(utid)
        where t.name like '{thread_prefix}%' and s.name = '{slice_name}'"""))
    return rows[0].ms or 0.0, rows[0].n


@pytest.mark.parametrize("name", ["step7_nopi_mon.log", "step7_pi_mon.log"])
def test_perfetto_reproduces_h_blocking(tp, name):
    log = LOGS / name
    blocked_us = sum(t1 - t0 for s, t0, t1 in tracelog.thread_states(tracelog.parse(log))["H"]
                     if s == "blocked")
    ms, n = slice_ms(tp(name), "H (", "blocked")
    assert n == 1 and ms == pytest.approx(blocked_us / 1000, abs=0.001)
    waiting, _ = slice_ms(tp(name), "H app", "waiting for lock")
    assert waiting == pytest.approx(plot_timeline.h_block_ms(plot_timeline.parse_log(log)), abs=0.001)


@pytest.mark.parametrize("name,steps", [("step7_pi_mon.log", [9, 5, 9]),
                                        ("step7_nopi_mon.log", [9])])
def test_priority_inheritance_is_a_counter_step(tp, name, steps):
    rows = tp(name).query("""select c.value from counter c join counter_track ct on c.track_id = ct.id
                             where ct.name = 'L prio' order by c.ts""")
    assert [int(r.value) for r in rows] == steps


def test_monitor_alarm_carries_its_snapshot(tp):
    rows = tp("step7_nopi_mon.log").query("""
        select a.key, a.display_value as v from slice s join args a using(arg_set_id)
        where s.name = 'blocked too long'""")
    args = {r.key: r.v for r in rows}
    assert args == {"args.running": "M (prio 7)", "args.mutex owner": "L (prio 9)"}


def test_cpu_track_and_jobs_match_the_trace(tp):
    tr = tracelog.parse(LOGS / "step5_periodic.log")
    running = sum(1 for spans in tracelog.thread_states(tr).values() for s, *_ in spans if s == "running")
    rows = list(tp("step5_periodic.log").query("""
        select count(*) as n from slice s join thread_track tt on s.track_id = tt.id
        join thread t using(utid) where t.name like 'CPU0%'"""))
    assert rows[0].n == running
    for th, jobs in {"H": 60, "M": 20, "L": 6}.items():
        rows = list(tp("step5_periodic.log").query(f"""
            select count(*) as n from slice s join thread_track tt on s.track_id = tt.id
            join thread t using(utid) where t.name = '{th} app' and s.name like 'job %'"""))
        assert rows[0].n == jobs
