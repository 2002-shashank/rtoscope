"""Reset the ESP32 via RTS and capture its serial output to a file.

Usage: python3 tools/capture_serial.py /dev/cu.usbserial-110 logs/out.log 3.5
       python3 tools/capture_serial.py PORT OUT 600 --until "# rtoscope runs done"
Needs pyserial (the Zephyr venv has it).

SECONDS is the capture length, or with --until, the upper limit: capture stops
once a line containing TEXT has arrived.
"""
import argparse
import sys
import time

import serial

ap = argparse.ArgumentParser()
ap.add_argument("port")
ap.add_argument("out")
ap.add_argument("seconds", type=float)
ap.add_argument("--until", help="stop after a line containing this text")
args = ap.parse_args()

s = serial.Serial(args.port, 115200, timeout=0.05)
# CYD auto-reset circuit: RTS drives EN (reset), DTR drives GPIO0 (boot mode).
s.dtr = False          # GPIO0 high -> normal boot, not download mode
s.rts = True           # EN low -> hold in reset
time.sleep(0.1)
s.reset_input_buffer()
s.rts = False          # release reset
buf = bytearray()
until = args.until.encode() if args.until else None
t_end = time.monotonic() + args.seconds
found = False
while time.monotonic() < t_end:
    buf += s.read(4096)
    # Search only the tail, and require the line to have ended.
    if until and until in buf[-4096:] and buf.rstrip(b"\r\n") != buf:
        found = True
        break
s.close()
open(args.out, "wb").write(buf)
print(f"{len(buf)} bytes" + (" (stop text seen)" if found else ""))
if until and not found:
    sys.exit(f"timed out after {args.seconds:.0f} s without seeing {args.until!r}")
