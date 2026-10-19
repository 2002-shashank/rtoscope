"""The CTF decoder, and the tracer comparison (phase 2, step 5) from the committed logs."""

import struct
import zlib

import pytest

import ctf_decode
import tracer_compare as tc
from conftest import LOGS
from ctf_layouts import LAYOUTS

LOG = {t: LOGS / f"step8_{t}.log" for t in tc.TRACERS}


def ev(t_ns, ev_id, *payload):
    return struct.pack("<IH", t_ns, ev_id) + b"".join(payload)


def name20(s):
    return s.encode().ljust(20, b"\0")


def ctf_capture(capture, data, isr="off", crc=None):
    lines = [f"# rtoscope ctf, ram buffer 4096 bytes, {len(data)} dumped, isr events {isr}"]
    lines += [f"ctf,{off:05x},{data[off:off + 32].hex()}" for off in range(0, len(data), 32)]
    good = 0
    for ln in lines:
        good = zlib.crc32((ln + "\n").encode(), good)
    return capture([*lines, f"# crc32 {good if crc is None else crc:08x}"])


def test_switch_event_is_30_bytes():
    name, layout = LAYOUTS[0x11]
    assert name == "thread_switched_in"
    assert 4 + 2 + sum(size for _, _, size in layout) == 30   # ts + id + thread_id + name[20]


def test_decode_packed_events_and_stop_at_zero_padding(capture):
    data = (ev(1000, 0x11, struct.pack("<I", 0x3FFB0000), name20("H"))
            + ev(2500, 0x12, struct.pack("<I", 0x3FFB0000), name20("L"), struct.pack("<b", 5))
            + ev(4000, 0x7F, struct.pack("<I", 7))
            + bytes(40))                                    # zeroed rest of the buffer
    dump = ctf_decode.parse(ctf_capture(capture, data))
    assert [e.name for e in dump.events] == ["thread_switched_in", "thread_priority_set",
                                             "thread_sleep_enter"]
    assert dump.events[0].fields == {"thread_id": 0x3FFB0000, "name": "H"}
    assert dump.events[1].fields["prio"] == 5 and dump.events[1].size == 31
    assert dump.events[2].t_ns == 4000


def test_unknown_event_id_is_an_error_not_skipped(capture):
    data = ev(1000, 0x7FFF, b"\x01\x02") + bytes(16)
    with pytest.raises(ctf_decode.CtfError, match="unknown CTF event id 0x7fff"):
        ctf_decode.parse(ctf_capture(capture, data))


def test_ctf_dump_crc_is_checked(capture):
    data = ev(1000, 0x1E) + bytes(8)   # idle
    with pytest.raises(ctf_decode.CtfError, match="crc32 mismatch"):
        ctf_decode.parse(ctf_capture(capture, data, crc=0x12345678))


@pytest.mark.parametrize("tracer", ["ctf", "ctf-noisr"])
def test_committed_ctf_dumps_decode_completely(tracer):
    dump = ctf_decode.parse(LOG[tracer])
    used = sum(e.size for e in dump.events)
    assert dump.events and used <= len(dump.data)
    assert all(b == 0 for b in dump.data[used:])          # nothing undecoded after the end


def test_xtensa_ctf_has_no_isr_events_even_with_tracing_isr_on():
    """v4.4.2's arch/xtensa never calls sys_trace_isr_*: CONFIG_TRACING_ISR has no effect."""
    on, off = ctf_decode.parse(LOG["ctf"]), ctf_decode.parse(LOG["ctf-noisr"])
    assert on.isr_events and not off.isr_events
    assert not any(e.name.startswith("isr_") for e in on.events)
    assert [(e.name, e.size) for e in on.events] == [(e.name, e.size) for e in off.events]


@pytest.fixture(scope="module")
def compared():
    return tc.analyze(LOG)


def test_both_tracers_record_the_same_schedule(compared):
    assert compared["user"]["sched"] == compared["ctf"]["sched"]
    assert compared["user"]["sched"]["switch in"] == 77


def test_ctf_hook_costs_several_times_more(compared):
    user, ctf = compared["user"]["bench"]["median"], compared["ctf"]["bench"]["median"]
    assert 100 < user < 200 and ctf > 4 * user


def test_ctf_events_are_bigger(compared):
    per = {t: compared[t]["bytes"] / compared[t]["events"] for t in ("user", "ctf")}
    assert per["user"] == 16 and per["ctf"] > 20


def test_perturbation_follows_hook_cost(compared):
    """L is preempted the most, so it collects the most tracing overhead."""
    mean_l = {t: sum(compared[t]["delta"]["L"]) / len(compared[t]["delta"]["L"])
              for t in ("user", "ctf")}
    assert 0 < mean_l["user"] < 15 and mean_l["ctf"] > 4 * mean_l["user"]
    # H's own finish (from its own wake) barely moves once warm: its hooks
    # fall outside the measured window. Under 1 µs either way is code-layout
    # noise between different binaries.
    for t in ("user", "ctf", "ctf-noisr"):
        assert max(abs(d) for d in compared[t]["delta"]["H"]) < 1.0


def test_first_jobs_are_excluded_because_cold_caches_differ_per_build(compared):
    """Job 1 runs from a cold flash cache; each binary arrives there differently.
    With trace.c, H's first job even beats the no-tracer baseline (its
    calibration warms shared code just before the release)."""
    assert compared["user"]["first"]["H"] < -5
    base = compared["none"]["finish"]["H"]
    assert base[0] - base[1] > 10    # cold first job vs warm second, no tracer
