"""xfad: the privileged XMG Forgot Apex daemon.

Owns the hardware (EC mailbox for fans and the performance mode, sysfs LEDs for the keyboard),
runs the fan control loop, keeps the EC performance mode and power-profiles-daemon (GNOME
"Power Mode") in sync, and exposes everything on D-Bus (see dbus_api.py). Runs as root.

    xfad                      # system bus (production, needs the D-Bus policy file)
    xfad --address ADDR       # connect to a private bus, for development (contrib/dev.sh)
"""
from __future__ import annotations

import argparse
import json
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
    BUS_NAME, FAN_MODES, FAN_SOURCES, IFACE_DAEMON, IFACE_FAN, IFACE_KEYBOARD, IFACE_PROFILE,
    INTROSPECTION_XML, OBJECT_PATH, PROFILES,
)
from .ec import EcMailbox, EcError  # noqa: E402
from .fan import FanController, curve_percent, percent_to_raw, raw_to_percent  # noqa: E402
from .keyboard import Keyboard  # noqa: E402
from .perf import PerfMode, profile_of  # noqa: E402
from .presets import PRESETS, preset_curve  # noqa: E402

ERROR_NAME = "dev.nulliche.xfa.Error"
PPD_NAME = "org.freedesktop.UPower.PowerProfiles"
PPD_PATH = "/org/freedesktop/UPower/PowerProfiles"
PPD_IFACE = "org.freedesktop.UPower.PowerProfiles"
PROPS_IFACE = "org.freedesktop.DBus.Properties"


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
        self.perf = PerfMode(self.ec)
        self.kbd = Keyboard()
        self.temps = (0, 0)
        self.duty = (0, 0)
        self.ec_mode = 0
        self.curve_target = -1
        self.conn: Gio.DBusConnection | None = None
        self.sysbus: Gio.DBusConnection | None = None
        self._reg_ids: list[int] = []
        self._node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION_XML)
        self._ppd_target: str | None = None  # last profile we pushed to PPD (echo filter)

    # ------------------------------------------------------------------ profile access
    @property
    def active(self) -> str:
        return self.state["active_profile"]

    @property
    def fan_state(self) -> dict:
        return self.state["profiles"][self.active]["fan"]

    def _curve(self) -> list[tuple[int, int]]:
        return [(int(t), int(p)) for t, p in self.fan_state["curve"]]

    # ------------------------------------------------------------------ properties
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
        elif iface == IFACE_PROFILE:
            if name == "ActiveProfile":
                return GLib.Variant("s", self.active)
            if name == "Profiles":
                return GLib.Variant("as", list(PROFILES))
            if name == "EcMode":
                return GLib.Variant("i", self.ec_mode)
            if name == "SyncPowerProfiles":
                return GLib.Variant("b", bool(self.state["sync_power_profiles"]))
            if name == "Presets":
                return GLib.Variant("as", list(PRESETS))
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
            None, OBJECT_PATH, PROPS_IFACE, "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (iface, changed, [])),
        )

    def _emit_fan_settings(self) -> None:
        self._emit_changed(IFACE_FAN, ["Mode", "Source", "Curve", "ManualSpeed", "CurveTarget"])

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
            self._poll_ec_mode()
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

    # ------------------------------------------------------------------ profiles
    def _poll_ec_mode(self) -> None:
        """Detect the hardware mode button: the EC register changed under us."""
        raw = self.perf.read_raw()
        if raw == self.ec_mode:
            return
        prev, self.ec_mode = self.ec_mode, raw
        self._emit_changed(IFACE_PROFILE, ["EcMode"])
        prof = profile_of(raw)
        if prof is None:
            # 0x10 (firmware boot default) or something unknown: put our profile back.
            log(f"EC mode 0x{raw:02x} is not a known profile, re-applying {self.active}")
            self.perf.write(self.active)
            self.ec_mode = self.perf.read_raw()
            return
        if prof != self.active and profile_of(prev) is not None:
            log(f"mode button: EC 0x{prev:02x} -> 0x{raw:02x} => {prof}")
            self.activate(prof, source="button")

    def activate(self, name: str, source: str) -> None:
        """Make `name` the active profile. source: dbus | button | ppd | startup | resume."""
        if name not in PROFILES:
            raise ValueError(f"unknown profile {name!r}")
        changed = name != self.active
        self.state["active_profile"] = name
        if source != "button":
            self.perf.write(name)
        self._apply_fan()
        self.ec_mode = self.perf.read_raw()  # after _apply_fan so the fan flag (0x40) is current
        if self.state["sync_power_profiles"] and source != "ppd":
            self._ppd_set(name)
        self._emit_changed(IFACE_PROFILE, ["ActiveProfile", "EcMode"])
        self._emit_fan_settings()
        if changed or source in ("dbus", "button"):
            log(f"profile {name} active (via {source})")
        self._save()

    # ------------------------------------------------------------------ power-profiles-daemon
    def _ppd_get(self) -> str | None:
        if not self.sysbus:
            return None
        try:
            res = self.sysbus.call_sync(
                PPD_NAME, PPD_PATH, PROPS_IFACE, "Get",
                GLib.Variant("(ss)", (PPD_IFACE, "ActiveProfile")), GLib.VariantType("(v)"),
                Gio.DBusCallFlags.NONE, 2000, None,
            )
            return res.unpack()[0]
        except GLib.Error as e:
            log(f"power-profiles-daemon not reachable: {e.message}")
            return None

    def _ppd_set(self, name: str) -> None:
        if not self.sysbus:
            return
        if self._ppd_get() == name:
            return
        self._ppd_target = name
        try:
            self.sysbus.call_sync(
                PPD_NAME, PPD_PATH, PROPS_IFACE, "Set",
                GLib.Variant("(ssv)", (PPD_IFACE, "ActiveProfile", GLib.Variant("s", name))),
                None, Gio.DBusCallFlags.NONE, 2000, None,
            )
            log(f"power-profiles-daemon -> {name}")
        except GLib.Error as e:
            log(f"could not set power-profiles-daemon profile: {e.message}")

    def _on_ppd_changed(self, conn, sender, path, iface, signal, params):
        ifname, changed, _ = params.unpack()
        if ifname != PPD_IFACE or "ActiveProfile" not in changed:
            return
        name = changed["ActiveProfile"]
        if name == self._ppd_target:
            self._ppd_target = None  # echo of our own write
            return
        if not self.state["sync_power_profiles"] or name not in PROFILES or name == self.active:
            return
        log(f"GNOME power mode -> {name}")
        try:
            self.activate(name, source="ppd")
        except EcError as e:
            log(f"EC error: {e}")

    def watch_system_bus(self) -> None:
        try:
            self.sysbus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            log(f"no system bus: {e.message}")
            return
        self.sysbus.signal_subscribe(
            PPD_NAME, PROPS_IFACE, "PropertiesChanged", PPD_PATH, None,
            Gio.DBusSignalFlags.NONE, self._on_ppd_changed,
        )

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

        self.sysbus.signal_subscribe(
            "org.freedesktop.login1", "org.freedesktop.login1.Manager", "PrepareForSleep",
            "/org/freedesktop/login1", None, Gio.DBusSignalFlags.NONE, on_sleep,
        )

    def _resume_apply(self) -> bool:
        try:
            self.kbd.refresh()
            self._apply_keyboard()
            self.activate(self.active, source="resume")
            self.tick()
        except (EcError, OSError) as e:
            log(f"resume apply failed: {e}")
        return False

    # ------------------------------------------------------------------ keyboard
    def _apply_keyboard(self) -> None:
        """Re-apply the saved color. Brightness is left to GNOME/UPower on purpose."""
        ks = self.state["keyboard"]
        if self.kbd.available and ks.get("color"):
            self.kbd.set_color(*ks["color"])

    def _set_brightness(self, value: int) -> None:
        """Prefer UPower so gsd-power (idle dim, OSD, GNOME's own slider) sees the change."""
        value = max(0, min(self.kbd.max_brightness, int(value)))
        if self.sysbus:
            try:
                self.sysbus.call_sync(
                    "org.freedesktop.UPower", "/org/freedesktop/UPower/KbdBacklight",
                    "org.freedesktop.UPower.KbdBacklight", "SetBrightness",
                    GLib.Variant("(i)", (value,)), None, Gio.DBusCallFlags.NONE, 2000, None,
                )
                return
            except GLib.Error as e:
                log(f"UPower SetBrightness failed ({e.message}), writing sysfs directly")
        self.kbd.set_brightness(value)

    # ------------------------------------------------------------------ D-Bus methods
    def handle_method(self, conn, sender, path, iface, method, params, invocation):
        try:
            if iface == IFACE_FAN:
                result = self._fan_method(method, params)
            elif iface == IFACE_PROFILE:
                result = self._profile_method(method, params)
            elif iface == IFACE_KEYBOARD:
                result = self._kbd_method(method, params)
            else:
                raise ValueError(f"unknown method {iface}.{method}")
        except (ValueError, KeyError, EcError, OSError) as e:
            invocation.return_dbus_error(ERROR_NAME, str(e))
            return
        invocation.return_value(result)

    def _fan_method(self, method: str, params: GLib.Variant):
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
            fs["curve"] = [list(p) for p in self._validate_curve(points)]
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
        return None

    @staticmethod
    def _validate_curve(points) -> list[tuple[int, int]]:
        pts = sorted((int(t), int(p)) for t, p in points)
        if not pts:
            raise ValueError("curve needs at least one point")
        for t, p in pts:
            if not (0 <= t <= 120 and 0 <= p <= 100):
                raise ValueError("points must be 0..120 °C and 0..100 %")
        return pts

    def _profile_method(self, method: str, params: GLib.Variant):
        if method == "SetActiveProfile":
            (name,) = params.unpack()
            self.activate(name, source="dbus")
            return None
        if method == "SetSyncPowerProfiles":
            (enabled,) = params.unpack()
            self.state["sync_power_profiles"] = bool(enabled)
            if enabled:
                self._ppd_set(self.active)
            self._emit_changed(IFACE_PROFILE, ["SyncPowerProfiles"])
            self._save()
            return None
        if method == "GetProfile":
            (name,) = params.unpack()
            if name not in PROFILES:
                raise ValueError(f"unknown profile {name!r}")
            return GLib.Variant("(s)", (json.dumps(self.state["profiles"][name]),))
        if method == "SetProfile":
            name, text = params.unpack()
            if name not in PROFILES:
                raise ValueError(f"unknown profile {name!r}")
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise ValueError(f"bad JSON: {e}") from None
            fan = data.get("fan", {}) if isinstance(data, dict) else {}
            target = self.state["profiles"][name]["fan"]
            if "mode" in fan:
                if fan["mode"] not in FAN_MODES:
                    raise ValueError(f"mode must be one of {FAN_MODES}")
                target["mode"] = fan["mode"]
            if "source" in fan:
                if fan["source"] not in FAN_SOURCES:
                    raise ValueError(f"source must be one of {FAN_SOURCES}")
                target["source"] = fan["source"]
            if "curve" in fan:
                target["curve"] = [list(p) for p in self._validate_curve(fan["curve"])]
            if "manual" in fan:
                p0, p1 = (int(x) for x in fan["manual"])
                if not (0 <= p0 <= 100 and 0 <= p1 <= 100):
                    raise ValueError("percent must be 0..100")
                target["manual"] = [p0, p1]
            if name == self.active:
                self._apply_fan()
                self._emit_fan_settings()
            self._save()
            return None
        if method == "ApplyPreset":
            (preset,) = params.unpack()
            if preset not in PRESETS:
                raise ValueError(f"preset must be one of {tuple(PRESETS)}")
            fs = self.fan_state
            fs["curve"] = [list(p) for p in preset_curve(preset)]
            fs["mode"] = "curve"
            self._apply_fan()
            self._emit_fan_settings()
            self._save()
            return None
        raise ValueError(f"unknown method {method}")

    def _kbd_method(self, method: str, params: GLib.Variant):
        if not self.kbd.available:
            raise ValueError("no keyboard backlight device (is the ite_8291 module loaded?)")
        ks = self.state["keyboard"]
        if method == "SetBrightness":
            (v,) = params.unpack()
            self._set_brightness(v)
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
        return None

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

    def start(self) -> None:
        self._apply_keyboard()
        # Initial profile: follow GNOME's power mode if syncing, otherwise push ours to the EC.
        ppd = self._ppd_get() if self.state["sync_power_profiles"] else None
        if ppd in PROFILES:
            self.activate(ppd, source="ppd")
        else:
            self.activate(self.active, source="startup")
        self.tick()
        GLib.timeout_add(int(self.interval * 1000), self.tick)
        log(f"profile {self.active} (EC 0x{self.ec_mode:02x}), fan mode {self.fan_state['mode']}, "
            f"keyboard {'ok' if self.kbd.available else 'absent'}, poll {self.interval}s, "
            f"ppd sync {'on' if self.state['sync_power_profiles'] else 'off'}")

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
    ap.add_argument("--interval", type=float, default=1.0, help="poll/control interval in seconds")
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
    daemon.watch_system_bus()

    def on_name_acquired(conn, name):
        log(f"acquired {name}")
        try:
            daemon.start()
        except EcError as e:
            log(f"EC error during start: {e}")

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

    for sig in (signal.SIGTERM, signal.SIGINT):
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, lambda *a: (loop.quit(), False)[1])

    try:
        loop.run()
    finally:
        daemon.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
