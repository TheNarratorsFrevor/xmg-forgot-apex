"""xfad: the privileged XMG Forgot Apex daemon.

Owns the hardware (EC mailbox for fans, sysfs LEDs for the keyboard), runs the fan
control loop and exposes everything on D-Bus (see dbus_api.py). Runs as root.

    xfad                      # system bus (production, needs the D-Bus policy file)
    xfad --address ADDR       # connect to a private bus, for development (contrib/dev.sh)
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from . import __version__  # noqa: E402
from . import state as statemod  # noqa: E402
from .dbus_api import (  # noqa: E402
    BUS_NAME, FAN_MODES, FAN_SOURCES, IFACE_DAEMON, IFACE_FAN, IFACE_KEYBOARD,
    INTROSPECTION_XML, OBJECT_PATH,
)
from .ec import EcMailbox, EcError  # noqa: E402
from .fan import FanController, curve_percent, percent_to_raw, raw_to_percent  # noqa: E402
from .keyboard import Keyboard  # noqa: E402

ERROR_NAME = "dev.nulliche.xfa.Error"


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} xfad: {msg}", file=sys.stderr, flush=True)


def _board() -> str:
    try:
        with open("/sys/class/dmi/id/board_name") as f:
            return f.read().strip()
    except OSError:
        return "unknown"


class Xfad:
    def __init__(self, interval: float, state_path: str):
        self.interval = interval
        self.state_path = state_path
        self.state = statemod.load(state_path)
        self.ec = EcMailbox()
        self.fan = FanController(self.ec)
        self.kbd = Keyboard()
        self.temps = (0, 0)
        self.duty = (0, 0)
        self.curve_target = -1
        self.conn: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []
        self._node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)

    # ------------------------------------------------------------------ properties
    @property
    def fan_state(self) -> dict:
        return self.state["fan"]

    def _curve(self) -> list[tuple[int, int]]:
        return [(int(t), int(p)) for t, p in self.fan_state["curve"]]

    def get_property(self, iface: str, name: str) -> GLib.Variant:
        fs = self.fan_state
        if iface == IFACE_DAEMON:
            if name == "Version":
                return GLib.Variant("s", __version__)
            if name == "Board":
                return GLib.Variant("s", _board())
            if name == "Backend":
                return GLib.Variant("s", "ec_sys")
        elif iface == IFACE_FAN:
            if name == "Mode":
                return GLib.Variant("s", fs["mode"])
            if name == "Source":
                return GLib.Variant("s", fs["source"])
            if name == "Curve":
                return GLib.Variant("a(ii)", self._curve())
            if name == "ManualSpeed":
                return GLib.Variant("ai", [int(x) for x in fs["manual"]])
            if name == "Temps":
                return GLib.Variant("ai", list(self.temps))
            if name == "Duty":
                return GLib.Variant("ai", [raw_to_percent(d) for d in self.duty])
            if name == "CurveTarget":
                return GLib.Variant("i", self.curve_target)
        elif iface == IFACE_KEYBOARD:
            if name == "Available":
                return GLib.Variant("b", self.kbd.available)
            if name == "LedCount":
                return GLib.Variant("i", len(self.kbd.leds))
            if name == "Brightness":
                return GLib.Variant("i", self.kbd.brightness())
            if name == "MaxBrightness":
                return GLib.Variant("i", self.kbd.max_brightness)
            if name == "Color":
                return GLib.Variant("ai", list(self.kbd.color()))
        raise KeyError(f"{iface}.{name}")

    def _emit_changed(self, iface: str, names: list[str]) -> None:
        if not self.conn:
            return
        changed = {n: self.get_property(iface, n) for n in names}
        self.conn.emit_signal(
            None, OBJECT_PATH, "org.freedesktop.DBus.Properties", "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (iface, changed, [])),
        )

    # ------------------------------------------------------------------ fan control
    def _apply_fan(self) -> None:
        fs = self.fan_state
        mode = fs["mode"]
        if mode == "auto":
            self.fan.set_auto()
            self.curve_target = -1
        elif mode == "manual":
            p0, p1 = fs["manual"]
            self.fan.reassert_raw(percent_to_raw(p0), percent_to_raw(p1))
            self.curve_target = -1
        elif mode == "curve":
            cpu, gpu = self.temps
            src = fs["source"]
            temp = max(cpu, gpu) if src == "max" else (cpu if src == "cpu" else gpu)
            pct = curve_percent(self._curve(), temp)
            raw = percent_to_raw(pct)
            self.fan.reassert_raw(raw, raw)
            self.curve_target = pct

    def tick(self) -> bool:
        try:
            self.temps = self.fan.temps()
            self._apply_fan()
            self.duty = self.fan.duty()
        except EcError as e:
            log(f"EC error: {e}")
        self._emit_changed(IFACE_FAN, ["Temps", "Duty", "CurveTarget"])
        return True  # keep the timer

    def _save(self) -> None:
        try:
            statemod.save(self.state, self.state_path)
        except OSError as e:
            log(f"could not save state: {e}")

    # ------------------------------------------------------------------ keyboard
    def _apply_keyboard(self) -> None:
        ks = self.state["keyboard"]
        if not self.kbd.available:
            return
        if ks.get("color"):
            self.kbd.set_color(*ks["color"])
        if ks.get("brightness") is not None:
            self.kbd.set_brightness(ks["brightness"])

    # ------------------------------------------------------------------ D-Bus methods
    def handle_method(self, conn, sender, path, iface, method, params, invocation):
        try:
            if iface == IFACE_FAN:
                self._fan_method(method, params)
            elif iface == IFACE_KEYBOARD:
                self._kbd_method(method, params)
            else:
                raise ValueError(f"unknown method {iface}.{method}")
        except (ValueError, EcError, OSError) as e:
            invocation.return_dbus_error(ERROR_NAME, str(e))
            return
        invocation.return_value(None)

    def _fan_method(self, method: str, params: GLib.Variant) -> None:
        fs = self.fan_state
        if method == "SetMode":
            (mode,) = params.unpack()
            if mode not in FAN_MODES:
                raise ValueError(f"mode must be one of {FAN_MODES}")
            fs["mode"] = mode
            self._apply_fan()
            self._emit_changed(IFACE_FAN, ["Mode", "CurveTarget"])
        elif method == "SetManual":
            p0, p1 = params.unpack()
            for p in (p0, p1):
                if not 0 <= p <= 100:
                    raise ValueError("percent must be 0..100")
            fs["manual"] = [p0, p1]
            if fs["mode"] == "manual":
                self._apply_fan()
            self._emit_changed(IFACE_FAN, ["ManualSpeed"])
        elif method == "SetCurve":
            (points,) = params.unpack()
            pts = sorted((int(t), int(p)) for t, p in points)
            if not pts:
                raise ValueError("curve needs at least one point")
            for t, p in pts:
                if not (0 <= t <= 120 and 0 <= p <= 100):
                    raise ValueError("points must be 0..120 °C and 0..100 %")
            fs["curve"] = [list(p) for p in pts]
            if fs["mode"] == "curve":
                self._apply_fan()
            self._emit_changed(IFACE_FAN, ["Curve", "CurveTarget"])
        elif method == "SetSource":
            (src,) = params.unpack()
            if src not in FAN_SOURCES:
                raise ValueError(f"source must be one of {FAN_SOURCES}")
            fs["source"] = src
            self._emit_changed(IFACE_FAN, ["Source"])
        else:
            raise ValueError(f"unknown method {method}")
        self._save()

    def _kbd_method(self, method: str, params: GLib.Variant) -> None:
        if not self.kbd.available:
            raise ValueError("no keyboard backlight device (is the ite_8291 module loaded?)")
        ks = self.state["keyboard"]
        if method == "SetBrightness":
            (v,) = params.unpack()
            self.kbd.set_brightness(v)
            ks["brightness"] = self.kbd.brightness()
            self._emit_changed(IFACE_KEYBOARD, ["Brightness"])
        elif method == "SetColor":
            r, g, b = params.unpack()
            self.kbd.set_color(r, g, b)
            ks["color"] = [r, g, b]
            self._emit_changed(IFACE_KEYBOARD, ["Color"])
        elif method == "SetKeyColors":
            (entries,) = params.unpack()
            self.kbd.set_key_colors([tuple(e) for e in entries])
            ks["color"] = None  # per-key layout is not persisted (yet)
            self._emit_changed(IFACE_KEYBOARD, ["Color"])
        else:
            raise ValueError(f"unknown method {method}")
        self._save()

    def handle_get_property(self, conn, sender, path, iface, name):
        try:
            return self.get_property(iface, name)
        except (KeyError, EcError, OSError):
            return None

    # ------------------------------------------------------------------ lifecycle
    def register(self, conn: Gio.DBusConnection) -> None:
        self.conn = conn
        for iface in self._node.interfaces:
            rid = conn.register_object(
                OBJECT_PATH, iface, self.handle_method, self.handle_get_property, None
            )
            self._reg_ids.append(rid)
        log(f"exported {OBJECT_PATH} on bus connection {conn.get_unique_name()}")

    def watch_sleep(self) -> None:
        """Re-apply fan mode and keyboard state after resume (logind on the system bus)."""
        try:
            sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            log(f"no system bus for logind: {e.message}")
            return

        def on_sleep(conn, sender, path, iface, signal, params):
            (entering,) = params.unpack()
            if entering:
                log("suspend: handing fans to EC auto")
                try:
                    self.fan.set_auto()
                except EcError as e:
                    log(f"EC error: {e}")
            else:
                log("resume: re-applying state in 3 s")
                GLib.timeout_add_seconds(3, self._resume_apply)

        sysbus.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
            "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, on_sleep,
        )

    def _resume_apply(self) -> bool:
        try:
            self.kbd.refresh()
            self._apply_keyboard()
            self.tick()
        except (EcError, OSError) as e:
            log(f"resume apply failed: {e}")
        return False

    def start(self) -> None:
        self._apply_keyboard()
        self.tick()
        GLib.timeout_add(int(self.interval * 1000), self.tick)
        log(f"fan mode {self.fan_state['mode']}, keyboard {'ok' if self.kbd.available else 'absent'}, "
            f"poll {self.interval}s")

    def shutdown(self) -> None:
        log("shutting down, fans back to EC auto")
        try:
            self.fan.set_auto()
        except EcError as e:
            log(f"EC error: {e}")
        if self.conn:
            for rid in self._reg_ids:
                self.conn.unregister_object(rid)
        self.ec.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="xfad", description=__doc__.split("\n")[0])
    ap.add_argument("--interval", type=float, default=2.0, help="poll/control interval in seconds")
    ap.add_argument("--address", help="connect to this D-Bus address instead of the system bus")
    ap.add_argument("--state", default=statemod.STATE_FILE, help="state file path")
    ap.add_argument("--version", action="version", version=f"xfad {__version__}")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("xfad: must run as root", file=sys.stderr)
        return 1

    try:
        daemon = Xfad(args.interval, args.state)
    except EcError as e:
        print(f"xfad: {e}", file=sys.stderr)
        return 3

    loop = GLib.MainLoop()

    def on_name_acquired(conn, name):
        log(f"acquired {name}")
        daemon.start()

    def on_name_lost(conn, name):
        log(f"lost bus name {name}, exiting")
        loop.quit()

    if args.address:
        conn = Gio.DBusConnection.new_for_address_sync(
            args.address,
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None,
        )
        daemon.register(conn)
        Gio.bus_own_name_on_connection(conn, BUS_NAME, Gio.BusNameOwnerFlags.NONE, on_name_acquired, on_name_lost)
    else:
        Gio.bus_own_name(
            Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameOwnerFlags.NONE,
            lambda conn, name: daemon.register(conn), on_name_acquired, on_name_lost,
        )

    daemon.watch_sleep()
    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *a: (loop.quit(), False)[1])

    try:
        loop.run()
    finally:
        daemon.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
