#!/usr/bin/env python3
"""Send raw "set brightness only" reports (09 02 <value>) to the ITE 8291 keyboard
controller over hidraw, bypassing the ite_8291 driver's 0..50 clamp. For finding the real
hardware range. Root required.

    sudo contrib/ite8291-raw-brightness.py 50 100 150 200 255        # 5 s apart
    sudo contrib/ite8291-raw-brightness.py -d 3 10 50 100            # 3 s apart

Afterwards the driver still thinks brightness is whatever sysfs last had; write it again
(echo 38 | sudo tee /sys/class/leds/rgb:kbd_backlight/brightness) to resync.
"""
import argparse, fcntl, glob, os, sys, time

def find_hidraw():
    for d in glob.glob("/sys/bus/hid/devices/*048D:600B*/hidraw/hidraw*"):
        return "/dev/" + os.path.basename(d)
    sys.exit("ITE 8291 (048d:600b) hidraw device not found")

def HIDIOCSFEATURE(n):
    return 0xC0000000 | (n << 16) | (ord("H") << 8) | 0x06

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("values", nargs="+", type=lambda v: int(v, 0), help="brightness bytes 0..255")
ap.add_argument("-d", "--delay", type=float, default=5.0, help="seconds between steps")
args = ap.parse_args()

fd = os.open(find_hidraw(), os.O_RDWR)
for v in args.values:
    v &= 0xFF
    fcntl.ioctl(fd, HIDIOCSFEATURE(8), bytes([0x09, 0x02, v, 0, 0, 0, 0, 0]))
    print(f"{time.strftime('%H:%M:%S')} brightness {v} (0x{v:02x})", flush=True)
    if v != args.values[-1] or len(args.values) == 1:
        time.sleep(args.delay)
