"""Reset the ESP32 via RTS and capture N seconds of serial output.

Usage: python3 tools/capture_serial.py /dev/cu.usbserial-110 logs/out.log 3.5
Needs pyserial (the Zephyr venv has it).
"""
import sys, time, serial
port, out, secs = sys.argv[1], sys.argv[2], float(sys.argv[3])
s = serial.Serial(port, 115200, timeout=0.05)
# CYD auto-reset circuit: RTS drives EN (reset), DTR drives GPIO0 (boot mode).
s.dtr = False          # GPIO0 high -> normal boot, not download mode
s.rts = True           # EN low -> hold in reset
time.sleep(0.1)
s.reset_input_buffer()
s.rts = False          # release reset
buf = bytearray()
t_end = time.monotonic() + secs
while time.monotonic() < t_end:
    buf += s.read(4096)
s.close()
open(out, "wb").write(buf)
print(f"{len(buf)} bytes")
