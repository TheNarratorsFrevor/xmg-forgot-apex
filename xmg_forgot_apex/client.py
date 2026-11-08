"""D-Bus client for xfad, used by the xfa CLI (and usable from other Python code).

Connects to the system bus, or to the address in $XFA_BUS_ADDRESS when set (development).
"""
from __future__ import annotations

import os

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

from .dbus_api import BUS_NAME, IFACE_DAEMON, IFACE_FAN, IFACE_KEYBOARD, OBJECT_PATH  # noqa: E402


class DaemonUnavailable(RuntimeError):
    pass


class XfaClient:
    def __init__(self, address: str | None = None):
        address = address or os.environ.get("XFA_BUS_ADDRESS")
        try:
            if address:
                self.conn = Gio.DBusConnection.new_for_address_sync(
                    address,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None, None,
                )
            else:
                self.conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        except GLib.Error as e:
            raise DaemonUnavailable(f"cannot connect to D-Bus: {e.message}") from None
        self._proxies: dict[str, Gio.DBusProxy] = {}
        # Fail early with a clear message if the daemon is not running.
        if not self._name_has_owner():
            raise DaemonUnavailable(f"{BUS_NAME} is not on the bus (is xfad running?)")

    def _name_has_owner(self) -> bool:
        try:
            res = self.conn.call_sync(
                "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                "NameHasOwner", GLib.Variant("(s)", (BUS_NAME,)), GLib.VariantType("(b)"),
                Gio.DBusCallFlags.NONE, 2000, None,
            )
            return bool(res.unpack()[0])
        except GLib.Error:
            return False

    def proxy(self, iface: str) -> Gio.DBusProxy:
        if iface not in self._proxies:
            try:
                self._proxies[iface] = Gio.DBusProxy.new_sync(
                    self.conn, Gio.DBusProxyFlags.NONE, None, BUS_NAME, OBJECT_PATH, iface, None
                )
            except GLib.Error as e:
                raise DaemonUnavailable(e.message) from None
        return self._proxies[iface]

    # --- generic helpers ---------------------------------------------------------------
    def get(self, iface: str, prop: str):
        """Fresh property read (not the proxy cache)."""
        res = self.conn.call_sync(
            BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", (iface, prop)), GLib.VariantType("(v)"),
            Gio.DBusCallFlags.NONE, 5000, None,
        )
        return res.unpack()[0]

    def get_all(self, iface: str) -> dict:
        res = self.conn.call_sync(
            BUS_NAME, OBJECT_PATH, "org.freedesktop.DBus.Properties", "GetAll",
            GLib.Variant("(s)", (iface,)), GLib.VariantType("(a{sv})"),
            Gio.DBusCallFlags.NONE, 5000, None,
        )
        return res.unpack()[0]

    def call(self, iface: str, method: str, signature: str | None = None, *args):
        params = GLib.Variant(signature, args) if signature else None
        try:
            return self.conn.call_sync(
                BUS_NAME, OBJECT_PATH, iface, method, params, None,
                Gio.DBusCallFlags.NONE, 30000, None,
            )
        except GLib.Error as e:
            raise RuntimeError(Gio.DBusError.strip_remote_error(e) and e.message) from None

    # --- convenience --------------------------------------------------------------------
    def status(self) -> dict:
        return {
            "daemon": self.get_all(IFACE_DAEMON),
            "fan": self.get_all(IFACE_FAN),
            "keyboard": self.get_all(IFACE_KEYBOARD),
        }

    def set_mode(self, mode: str):
        self.call(IFACE_FAN, "SetMode", "(s)", mode)

    def set_manual(self, p0: int, p1: int):
        self.call(IFACE_FAN, "SetManual", "(ii)", p0, p1)

    def set_curve(self, points):
        self.call(IFACE_FAN, "SetCurve", "(a(ii))", [(int(t), int(p)) for t, p in points])

    def set_source(self, src: str):
        self.call(IFACE_FAN, "SetSource", "(s)", src)

    def set_color(self, r: int, g: int, b: int):
        self.call(IFACE_KEYBOARD, "SetColor", "(iii)", r, g, b)

    def set_brightness(self, v: int):
        self.call(IFACE_KEYBOARD, "SetBrightness", "(i)", v)

    def set_key_colors(self, entries):
        self.call(IFACE_KEYBOARD, "SetKeyColors", "(a(iiii))", [tuple(int(x) for x in e) for e in entries])
