"""The on-device monitor and watchdog runs (phase 2, step 4), from the committed logs."""

import re

import pytest

import monitor_report as mr
import response_times
import tracelog
from conftest import LOGS, make_dump


def only_trace(name):
    return tracelog.parse(LOGS / name)


def text(name):
    return (LOGS / name).read_text(errors="replace").replace("\r", "")


def app_time(tr, thread, label):
    return next(e.t for e in tr.events if e.thread == thread and e.event == "app" and e.arg == label)


def test_alarm_grouping_on_a_synthetic_trace(capture):
    path = capture(make_dump([
        "2400,H,mon,5,blocked too long",
        "2410,M,mon,7,running at alarm",
        "2420,L,mon,9,mutex owner",
        "4800,H,mon,5,deadline miss",
        "4810,L,mon,5,running at alarm",
    ]))
    a, b = mr.alarms(tracelog.parse(path))
    assert (a.kind, a.victim, a.running, a.owner, a.owner_prio) == \
        ("blocked too long", "H", "M", "L", 9)
    assert a.unbounded
    assert (b.kind, b.running, b.owner) == ("deadline miss", "L", None)
    assert not b.unbounded


def test_no_pi_blocking_alarm_names_the_inversion():
    tr = only_trace("step7_nopi_mon.log")
    blocked, deadline = mr.alarms(tr)
    # Fires exactly one budget (60 ms) after H asked for the lock...
    assert blocked.kind == "blocked too long" and blocked.victim == "H"
    assert blocked.t - app_time(tr, "H", "lock request") == pytest.approx(60_000, abs=100)
    # ...while M runs and the owner L sits at its own priority: unbounded.
    assert (blocked.running, blocked.owner, blocked.owner_prio) == ("M", "L", 9)
    assert blocked.unbounded
    # The deadline miss is caught at the deadline, long before H gets the lock.
    assert deadline.kind == "deadline miss"
    assert mr.detection_latency(tr, deadline, mr.relative_deadlines(tr)) == pytest.approx(0, abs=20)
    assert app_time(tr, "H", "locked") - deadline.t > 100_000


def test_pi_run_raises_no_alarm():
    tr = only_trace("step7_pi_mon.log")
    assert mr.alarms(tr) == []
    assert app_time(tr, "H", "locked") - app_time(tr, "H", "lock request") < 60_000


def test_healthy_periodic_run_has_no_misses_and_meets_the_bound():
    tr = only_trace("step7_periodic_mon.log")
    assert mr.alarms(tr) == []
    _, _, rows = response_times.analyze(tr)
    assert all(r["within"] for r in rows)


def test_injected_overrun_is_one_miss_detected_at_the_deadline():
    tr = only_trace("step7_overrun.log")
    [miss] = mr.alarms(tr)
    assert (miss.kind, miss.victim) == ("deadline miss", "M")
    assert mr.detection_latency(tr, miss, mr.relative_deadlines(tr)) == pytest.approx(0, abs=20)
    assert "deadline misses so far H 0, M 1, L 0" in text("step7_overrun.log")


def boots(name):
    return re.findall(r"^# boot: reset cause 0x[0-9a-f]+ ?(.*)$", text(name), re.M)


def test_watchdog_healthy_run_never_trips():
    log = text("step7_wdt_none.log")
    assert boots("step7_wdt_none.log") == ["power-on"]
    assert "task watchdog channel" not in log
    assert "# rtoscope runs done" in log


def test_watchdog_starve_names_the_culprit_and_recovers():
    log = text("step7_wdt_starve.log")
    assert boots("step7_wdt_starve.log") == ["power-on", "watchdog"]
    fault = int(re.search(r'fault "starve" injected at (\d+) ms', log)[1])
    fired = int(re.search(r"task watchdog channel M expired at (\d+) ms, M running", log)[1])
    # M's channel (600 ms) runs from its last feed, the end of job 3, which is
    # 300 ms before the fault in job 4: so it fires 300 ms after the fault.
    assert fired - fault == pytest.approx(300, abs=5)
    # The deadline monitor saw M late before the watchdog did.
    assert "deadline misses so far H 0, M 1, L 0" in log
    # Recovery boot: last words reported, fault not re-armed, healthy run.
    assert "last words: task watchdog channel M expired" in log
    assert "test starve, fault not armed" in log
    assert mr.alarms(tracelog.parse(LOGS / "step7_wdt_starve.log")) == []


def test_watchdog_hang_is_caught_by_hardware_only():
    log = text("step7_wdt_hang.log")
    assert boots("step7_wdt_hang.log") == ["power-on", "watchdog"]
    assert "TG0WDT_SYS_RESET" in log          # ROM: timer group 0 watchdog reset
    assert 'fault "hang" injected' in log
    assert "task watchdog channel" not in log  # its timer could not run
    assert "test hang, fault not armed" in log


def test_blocking_alarm_with_the_owner_running_is_bounded(capture):
    # The owner L (boosted to 5 by PI) is the one running: it is finishing its
    # critical section, so the wait is bounded, even though it overran the budget.
    path = capture(make_dump([
        "2400,H,mon,5,blocked too long",
        "2410,L,mon,5,running at alarm",
        "2420,L,mon,5,mutex owner",
    ]))
    [a] = mr.alarms(tracelog.parse(path))
    assert (a.running, a.owner, a.owner_prio) == ("L", "L", 5)
    assert not a.unbounded
