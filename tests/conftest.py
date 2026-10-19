import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"


def make_dump(records, *, hz=240_000_000, call=143, n=None, dropped=0, crc=None):
    """Lines of one trace dump exactly as trace.c prints them, CRC included.

    n overrides the record count in the header; crc overrides the checksum.
    """
    body = [f"# rtoscope trace, cycle clock {hz} Hz, {len(records) if n is None else n} events, "
            f"{dropped} dropped, trace call {call} cycles",
            "cycles,thread,event,prio,arg",
            *records]
    good = 0
    for ln in body:
        good = zlib.crc32((ln + "\n").encode(), good)
    return [*body, f"# crc32 {good if crc is None else crc:08x}"]


@pytest.fixture
def capture(tmp_path):
    """Write lines as a raw capture (\\r\\n endings, like the board) and return its path."""
    def write(lines, name="capture.log"):
        path = tmp_path / name
        path.write_bytes(("\r\n".join(lines) + "\r\n").encode())
        return path
    return write
