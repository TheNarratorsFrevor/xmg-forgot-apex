"""xfa: command line client for the XMG Forgot Apex daemon (xfad)."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import __version__
from .dbus_api import FAN_MODES, FAN_SOURCES, IFACE_FAN, IFACE_KEYBOARD
from .fan import parse_curve


def _client():
    from .client import DaemonUnavailable, XfaClient
    try:
        return XfaClient()
    except DaemonUnavailable as e:
        sys.exit(f"xfa: {e}")


def _fmt_curve(points) -> str:
    return ",".join(f"{t}:{p}" for t, p in points)


def _print_status(st: dict) -> None:
    f, k, d = st["fan"], st["keyboard"], st["daemon"]
    cpu, gpu = f["Temps"]
    d0, d1 = f["Duty"]
    target = f" (curve target {f['CurveTarget']}%)" if f["Mode"] == "curve" else ""
    manual = f" (manual {f['ManualSpeed'][0]}/{f['ManualSpeed'][1]}%)" if f["Mode"] == "manual" else ""
    print(f"temps    cpu {cpu}°C  gpu {gpu}°C")
    print(f"fans     fan0 {d0:3d}%  fan1 {d1:3d}%")
    print(f"mode     {f['Mode']}{target}{manual}  source {f['Source']}")
    print(f"curve    {_fmt_curve(f['Curve'])}")
    if k["Available"]:
        r, g, b = k["Color"]
        print(f"keyboard {k['LedCount']} keys  brightness {k['Brightness']}/{k['MaxBrightness']}  color #{r:02x}{g:02x}{b:02x}")
    else:
        print("keyboard not available (ite_8291 module not loaded)")
    print(f"daemon   xfad {d['Version']} on {d['Board']} via {d['Backend']}")


def cmd_status(args) -> int:
    st = _client().status()
    if args.json:
        print(json.dumps(st))
    else:
        _print_status(st)
    return 0


def cmd_fan(args) -> int:
    c = _client()
    if args.what == "auto":
        c.set_mode("auto")
    elif args.what == "curve":
        c.set_mode("curve")
    elif args.what == "manual":
        if args.p0 is None:
            sys.exit("xfa: fan manual needs a percent (one for both fans, or two)")
        p1 = args.p1 if args.p1 is not None else args.p0
        c.set_manual(int(args.p0), int(p1))
        c.set_mode("manual")
    elif args.what == "source":
        if args.p0 not in FAN_SOURCES:
            sys.exit(f"xfa: fan source needs one of {', '.join(FAN_SOURCES)}")
        c.set_source(str(args.p0))
    f = c.get_all(IFACE_FAN)
    print(f"mode {f['Mode']}  source {f['Source']}  manual {f['ManualSpeed'][0]}/{f['ManualSpeed'][1]}%")
    return 0


def cmd_curve(args) -> int:
    c = _client()
    if args.points:
        c.set_curve(parse_curve(args.points))
    print(_fmt_curve(c.get(IFACE_FAN, "Curve")))
    return 0


def _hex_rgb(text: str) -> tuple[int, int, int]:
    h = text.lstrip("#")
    if len(h) != 6:
        raise ValueError("color must be RRGGBB")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def cmd_kbd(args) -> int:
    c = _client()
    try:
        if args.what == "color":
            c.set_color(*_hex_rgb(args.value))
        elif args.what == "brightness":
            c.set_brightness(int(args.value))
        elif args.what == "off":
            c.set_brightness(0)
        elif args.what == "key":
            vals = [args.value] + list(args.rest)
            if len(vals) % 2 or not args.value:
                raise ValueError("kbd key needs INDEX RRGGBB pairs")
            entries = [(int(vals[i]), *_hex_rgb(vals[i + 1])) for i in range(0, len(vals), 2)]
            c.set_key_colors(entries)
    except (ValueError, TypeError) as e:
        sys.exit(f"xfa: {e}")
    k = c.get_all(IFACE_KEYBOARD)
    if not k["Available"]:
        print("keyboard not available (ite_8291 module not loaded)")
        return 2
    r, g, b = k["Color"]
    print(f"brightness {k['Brightness']}/{k['MaxBrightness']}  color #{r:02x}{g:02x}{b:02x}  keys {k['LedCount']}")
    return 0


def cmd_watch(args) -> int:
    c = _client()
    try:
        while True:
            f = c.get_all(IFACE_FAN)
            cpu, gpu = f["Temps"]
            d0, d1 = f["Duty"]
            if args.json:
                print(json.dumps(f), flush=True)
            else:
                extra = f" target {f['CurveTarget']}%" if f["Mode"] == "curve" else ""
                print(f"{time.strftime('%H:%M:%S')}  cpu {cpu:3d}°C gpu {gpu:3d}°C  fan0 {d0:3d}% fan1 {d1:3d}%  {f['Mode']}{extra}", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_ec(args) -> int:
    """Direct EC access (root, bypasses the daemon). For debugging."""
    from .ec import EcError, EcMailbox, EcUnavailable
    try:
        ec = EcMailbox()
    except EcUnavailable as e:
        sys.exit(f"xfa: {e}")
    try:
        if args.op == "read":
            for a in args.args:
                addr = int(a, 0)
                lo, hi = ec.read16(addr)
                print(f"0x{addr:04x} = 0x{lo:02x} ({lo})  next=0x{hi:02x}")
        elif args.op == "write":
            addr, val = int(args.args[0], 0), int(args.args[1], 0)
            ec.write(addr, val)
            print(f"0x{addr:04x} <- 0x{val:02x}, readback 0x{ec.read(addr):02x}")
        elif args.op == "dump":
            s, e = int(args.args[0], 0), int(args.args[1], 0)
            row = []
            for addr in range(s, e + 1):
                row.append(f"{ec.read(addr):02x}")
                if len(row) == 16 or addr == e:
                    print(f"0x{addr - len(row) + 1:04x}: " + " ".join(row))
                    row = []
    except EcError as e:
        sys.exit(f"xfa: {e}")
    except (IndexError, ValueError):
        sys.exit("xfa: ec read ADDR... | write ADDR VALUE | dump START END")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xfa", description="XMG Forgot Apex: fan and keyboard control for the XMG APEX 16 MAX.")
    p.add_argument("--version", action="version", version=f"xfa {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("status", help="temperatures, fan state, keyboard, daemon")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("fan", help="fan mode: auto | curve | manual P [P2] | source max|cpu|gpu")
    s.add_argument("what", choices=list(FAN_MODES) + ["source"])
    s.add_argument("p0", nargs="?")
    s.add_argument("p1", nargs="?", type=int)
    s.set_defaults(func=cmd_fan)

    s = sub.add_parser("curve", help='show or set the curve: xfa curve "45:0,60:30,80:60,95:100"')
    s.add_argument("points", nargs="?")
    s.set_defaults(func=cmd_curve)

    s = sub.add_parser("kbd", help="keyboard: color RRGGBB | brightness N | off | key INDEX RRGGBB ...")
    s.add_argument("what", nargs="?", choices=["color", "brightness", "off", "key"])
    s.add_argument("value", nargs="?")
    s.add_argument("rest", nargs="*")
    s.set_defaults(func=cmd_kbd)

    s = sub.add_parser("watch", help="print live readings")
    s.add_argument("-i", "--interval", type=float, default=2.0)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser("ec", help="debug: direct EC register access (root)")
    s.add_argument("op", choices=["read", "write", "dump"])
    s.add_argument("args", nargs="*")
    s.set_defaults(func=cmd_ec)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"xfa: {e}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
