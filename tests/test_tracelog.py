"""The trace parser on synthetic captures: clean dumps, and every way a dump goes bad."""

import pytest

import tracelog
from conftest import LOGS, make_dump

RECORDS = [
    "0,L,ready,9,",
    "240,L,in,9,",
    "480,L,app,9,start",
    "2400,L,out,9,sleeping",
]


def test_clean_dump(capture):
    path = capture(["ets Jul 29 2019", "rtoscope periodic: 1 hyperperiods of 3000 ms",
                    *make_dump(RECORDS)])
    tr = tracelog.parse(path)
    assert tr.error is None
    assert tr.hz == 240_000_000 and tr.call_cycles == 143
    assert [e.event for e in tr.events] == ["ready", "in", "app", "out"]
    assert tr.events[1].t == pytest.approx(1.0)        # 240 cycles at 240 MHz
    assert tr.events[2].arg == "start" and tr.events[3].arg == "sleeping"
    assert "rtoscope periodic: 1 hyperperiods of 3000 ms" in tr.lines


def test_multiple_dumps_split_their_header_lines(capture):
    path = capture(["boot", "# run 1/2", *make_dump(RECORDS), "# run 2/2",
                    *make_dump(RECORDS[:2]), "# rtoscope runs done"])
    first, second = tracelog.parse_all(path)
    assert "boot" in first.lines and "# run 1/2" in first.lines
    assert second.lines == ["# run 2/2"]
    assert len(first.events) == 4 and len(second.events) == 2
    assert tracelog.parse(path) is not None and len(tracelog.parse(path).events) == 2


def test_wrong_crc_raises_when_strict(capture):
    path = capture(make_dump(RECORDS, crc=0xDEADBEEF))
    with pytest.raises(tracelog.TraceError, match="crc32 mismatch"):
        tracelog.parse_all(path)


def test_wrong_crc_is_marked_when_not_strict(capture):
    path = capture([*make_dump(RECORDS, crc=0xDEADBEEF), *make_dump(RECORDS)])
    bad, good = tracelog.parse_all(path, strict=False)
    assert "crc32 mismatch" in bad.error
    assert good.error is None


def test_missing_crc_line(capture):
    path = capture(make_dump(RECORDS)[:-1])
    with pytest.raises(tracelog.TraceError, match="no crc32 line"):
        tracelog.parse_all(path)


def test_dump_cut_short(capture):
    # Capture ended mid-dump: two records and the CRC line never arrived.
    path = capture(make_dump(RECORDS)[:-3])
    with pytest.raises(tracelog.TraceError, match="no crc32 line"):
        tracelog.parse_all(path)


def test_record_count_mismatch(capture):
    path = capture(make_dump(RECORDS, n=5))
    with pytest.raises(tracelog.TraceError, match="header says 5 records, parsed 4"):
        tracelog.parse_all(path)


def test_ring_buffer_overflow(capture):
    path = capture(make_dump(RECORDS, dropped=7))
    with pytest.raises(tracelog.TraceError, match="dropped 7"):
        tracelog.parse_all(path)


def test_no_trace_at_all(capture):
    with pytest.raises(tracelog.TraceError, match="no trace header"):
        tracelog.parse_all(capture(["ets Jul 29 2019", "*** Booting Zephyr OS ***"]))


def test_lost_digit_is_caught_only_by_the_crc(capture):
    """A dropped digit still parses and keeps the record count: only the CRC sees it."""
    lines = make_dump(RECORDS)
    lines[lines.index("2400,L,out,9,sleeping")] = "240,L,out,9,sleeping"
    path = capture(lines)
    tr = tracelog.parse_all(path, strict=False)[0]
    assert len(tr.events) == len(RECORDS)              # count check passes...
    assert tr.events[3].t == pytest.approx(1.0)        # ...with a wrong timestamp
    assert "crc32 mismatch" in tr.error                # and the CRC catches it


def test_replay_of_real_mid_line_byte_loss(capture):
    """The step 2 failure: 9 bytes lost in the middle of a record on the USB-serial path."""
    lines = (LOGS / "step4_pi.log").read_text().splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.rstrip().endswith(",H,app,5,done"))
    lines[i] = lines[i].replace(",H,app,5,", ",", 1)
    with pytest.raises(tracelog.TraceError, match="crc32 mismatch"):
        tracelog.parse_all(capture([ln.rstrip("\r") for ln in lines]))


def test_jobs_counts_cpu_across_a_preemption(capture):
    # H sleeps, is released at 1000 µs, switched in at 1001, runs 10 µs, is
    # preempted for 20 µs, runs 4 µs more and finishes: response 35 µs, CPU 14 µs.
    us = lambda x: str(int(x * 240))
    path = capture(make_dump([
        f"{us(0)},H,out,5,sleeping",
        f"{us(1000)},H,ready,5,",
        f"{us(1001)},H,in,5,",
        f"{us(1002)},H,app,5,start",
        f"{us(1011)},H,out,5,ready",
        f"{us(1031)},H,in,5,",
        f"{us(1035)},H,app,5,done",
        f"{us(1036)},H,out,5,sleeping",
    ]))
    [(release, start, done, cpu)] = tracelog.jobs(tracelog.parse(path), "H")
    assert (release, start, done) == pytest.approx((1000, 1002, 1035))
    assert cpu == pytest.approx(10 + 4)                 # in 1001->1011, in 1031->done 1035
