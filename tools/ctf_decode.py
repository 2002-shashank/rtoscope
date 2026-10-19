"""Decode a Zephyr CTF RAM buffer dumped by the board (app/src/tracer.c).

Usage:
    tools/.venv.nosync/bin/python tools/ctf_decode.py logs/step8_ctf.log [--limit 40]

The board prints the buffer as hex inside a CRC-checked block:

    # rtoscope ctf, ram buffer 32768 bytes, 7856 dumped, isr events on
    ctf,00000,<64 hex digits>
    ...
    # crc32 xxxxxxxx

Each event is packed, little-endian: uint32 timestamp (ns), uint16 id, then
the fields in ctf_layouts.LAYOUTS[id]. Events sit back to back with no
length field, so an unknown id can't be skipped: decoding stops with an
error rather than silently desynchronising. A zero id marks the unused
(zeroed) rest of the buffer.
"""

import re
import struct
import sys
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from ctf_layouts import LAYOUTS

CTF_RE = re.compile(r"^# rtoscope ctf, ram buffer (?P<size>\d+) bytes, (?P<dumped>\d+) dumped, "
                    r"isr events (?P<isr>on|off)$")
CRC_RE = re.compile(r"^# crc32 (?P<crc>[0-9a-f]{8})$")
HEX_RE = re.compile(r"^ctf,(?P<off>[0-9a-f]{5}),(?P<hex>[0-9a-f]*)$")


class CtfError(Exception):
    pass


@dataclass
class CtfEvent:
    t_ns: int
    id: int
    name: str
    fields: dict
    offset: int
    size: int


@dataclass
class CtfDump:
    buffer_size: int
    isr_events: bool
    data: bytes
    events: list = field(default_factory=list)


def read_crc_block(lines, start, header_re):
    """(header match, body lines) of the CRC-checked block starting at lines[start].

    Raises CtfError if the closing CRC line is missing or doesn't match.
    """
    m = header_re.match(lines[start])
    crc = zlib.crc32((lines[start] + "\n").encode())
    body = []
    for ln in lines[start + 1:]:
        if c := CRC_RE.match(ln):
            if int(c["crc"], 16) != crc:
                raise CtfError(f"crc32 mismatch (board {c['crc']}, computed {crc:08x})")
            return m, body
        crc = zlib.crc32((ln + "\n").encode(), crc)
        body.append(ln)
    raise CtfError("no crc32 line (truncated capture?)")


def decode(data):
    events, off = [], 0
    while off + 6 <= len(data):
        t_ns, ev_id = struct.unpack_from("<IH", data, off)
        if ev_id == 0:
            break  # zero-filled remainder of the buffer
        if ev_id not in LAYOUTS:
            raise CtfError(f"unknown CTF event id {ev_id:#x} at offset {off}")
        name, layout = LAYOUTS[ev_id]
        pos, fields = off + 6, {}
        for fname, kind, size in layout:
            raw = data[pos:pos + size]
            if len(raw) < size:
                raise CtfError(f"event {name} at offset {off} runs past the dump")
            if kind == "str":
                fields[fname] = raw.split(b"\0", 1)[0].decode("latin-1")
            else:
                fields[fname] = int.from_bytes(raw, "little", signed=(kind == "int"))
            pos += size
        events.append(CtfEvent(t_ns, ev_id, name, fields, off, pos - off))
        off = pos
    return events


def parse(path):
    lines = [ln.strip() for ln in Path(path).read_text(errors="replace").splitlines()]
    starts = [i for i, ln in enumerate(lines) if CTF_RE.match(ln)]
    if not starts:
        raise CtfError(f"{path}: no CTF dump")
    m, body = read_crc_block(lines, starts[-1], CTF_RE)
    data, expect = bytearray(), 0
    for ln in body:
        h = HEX_RE.match(ln)
        if not h:
            raise CtfError(f"{path}: unexpected line in CTF dump: {ln[:40]!r}")
        if int(h["off"], 16) != expect:
            raise CtfError(f"{path}: CTF dump offset {h['off']} != expected {expect:05x}")
        chunk = bytes.fromhex(h["hex"])
        data += chunk
        expect += len(chunk)
    if len(data) != int(m["dumped"]):
        raise CtfError(f"{path}: header says {m['dumped']} bytes, got {len(data)}")
    dump = CtfDump(int(m["size"]), m["isr"] == "on", bytes(data))
    dump.events = decode(dump.data)
    return dump


def main():
    args = sys.argv[1:]
    limit = int(args[args.index("--limit") + 1]) if "--limit" in args else None
    path = next(a for a in args if not a.startswith("--") and not a.isdigit())
    dump = parse(path)
    used = sum(e.size for e in dump.events)
    print(f"{path}: {len(dump.events)} events, {used} bytes used of {dump.buffer_size}, "
          f"isr events {'on' if dump.isr_events else 'off'}")
    for e in dump.events[:limit]:
        print(f"{e.t_ns:>12} ns  {e.name:24} {e.fields}")


if __name__ == "__main__":
    main()
