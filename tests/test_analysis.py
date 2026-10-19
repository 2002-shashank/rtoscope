"""Response-time analysis and the scheduler simulation against hand-worked cases."""

import pytest

import tracelog
from conftest import make_dump
from response_times import Task, release_jitters, rta, simulate

# This project's periodic demo (µs): H 15/100 ms, M 40/300 ms, L 180/1000 ms.
DEMO = [Task("H", 5, 15_000, 100_000), Task("M", 7, 40_000, 300_000),
        Task("L", 9, 180_000, 1_000_000)]

# Textbook three-task set (C, T): (3, 7), (3, 12), (5, 20).
# Hand iteration for t3: 5 -> 11 -> 14 -> 17 -> 20 -> 20, so R3 = 20 = T3.
BOOK = [Task("t1", 1, 3, 7), Task("t2", 2, 3, 12), Task("t3", 3, 5, 20)]


def test_rta_demo_task_set():
    assert rta(DEMO) == {"H": 15_000, "M": 55_000, "L": 265_000}


def test_rta_textbook_set_meets_deadline_exactly():
    assert rta(BOOK) == {"t1": 3, "t2": 6, "t3": 20}


def test_rta_reports_a_deadline_miss_as_none():
    overloaded = [Task("a", 1, 3, 4), Task("b", 2, 3, 8)]  # b: 3 -> 6 -> 9 > 8
    assert rta(overloaded) == {"a": 3, "b": None}


def test_release_jitter_alone_breaks_the_textbook_set():
    """J1 = 2 lets one more t1 job land in t3's window: 5 -> 11 -> 14 -> 20 -> 23 > 20."""
    jitter = {"t1": 2, "t2": 0, "t3": 0}
    r = rta(BOOK, j_of=lambda t: jitter[t.name])
    assert r["t1"] == 2 + 3
    assert r["t2"] == 9          # 3 -> 6 -> 9 -> 9 (ceil((9+2)/7) = 2)
    assert r["t3"] is None


def test_simulate_two_tasks_by_hand():
    # a (C1, T2) beats b (C2, T4). 0-1 a, 1-2 b, 2-3 a (released at 2), 3-4 b done.
    resp = simulate([Task("a", 1, 1, 2), Task("b", 2, 2, 4)], horizon_us=4)
    assert resp == {"a": [1, 1], "b": [4]}


def test_simulate_demo_hyperperiod_matches_rta_at_critical_instant():
    # Every release coincides with an H release, so every job sees the worst case.
    resp = simulate(DEMO, horizon_us=3_000_000)
    assert len(resp["H"]) == 30 and set(resp["H"]) == {15_000}
    assert len(resp["M"]) == 10 and set(resp["M"]) == {55_000}
    assert len(resp["L"]) == 3 and set(resp["L"]) == {265_000}


def test_simulate_can_be_below_rta():
    # Textbook set over its hyperperiod (420): RTA is the worst case only at the
    # critical instant; later jobs of t3 finish sooner.
    resp = simulate(BOOK, horizon_us=420)
    assert max(resp["t3"]) == rta(BOOK)["t3"]
    assert min(resp["t3"]) < rta(BOOK)["t3"]
    assert resp["t1"] == pytest.approx([3] * 60)


def test_release_jitter_stops_at_the_first_switch_to_this_priority_or_better(capture):
    """M and H are released together; H runs first. M's jitter is the delay until
    H (priority >= M's) gets the CPU, not until M itself does: waiting for H
    is interference, which the RTA sum already counts.
    """
    us = lambda x: str(int(x * 240))
    path = capture(make_dump([
        f"{us(0)},M,out,7,sleeping",
        f"{us(1)},H,out,5,sleeping",
        f"{us(1000)},M,ready,7,",
        f"{us(1000)},H,ready,5,",
        f"{us(1005)},H,in,5,",
        f"{us(16005)},H,out,5,sleeping",
        f"{us(16007)},M,in,7,",
    ]))
    tr = tracelog.parse(path)
    assert release_jitters(tr, Task("M", 7, 40_000, 300_000)) == pytest.approx([5])
    assert release_jitters(tr, Task("H", 5, 15_000, 100_000)) == pytest.approx([5])
