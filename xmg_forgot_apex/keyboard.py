"""Keyboard backlight via the kernel multicolor LED class.

The XMG APEX 16 MAX keyboard is an ITE 8291 USB controller (048d:600b). tuxedo-drivers'
``ite_8291`` module (which does not depend on the TUXEDO DMI check, only on ``hid`` and
``led-class-multicolor``) exposes one multicolor LED per key:

    /sys/class/leds/rgb:kbd_backlight        (key 0)
    /sys/class/leds/rgb:kbd_backlight_1 .. _125

Each has ``brightness`` (0..50, global: writing any LED's brightness sets the keyboard
brightness) and ``multi_intensity`` ("R G B", each 0..50, per key).

This module presents colors as 0..255 and brightness as a percentage-free integer in the
LED's own range (clients read MaxBrightness).
"""
from __future__ import annotations

import glob
import os
import re

LED_GLOB = "/sys/class/leds/rgb:kbd_backlight*"
_SUFFIX = re.compile(r"rgb:kbd_backlight(?:_(\d+))?$")


def _read_int(path: str) -> int:
    with open(path) as f:
        return int(f.read().split()[0])


def _read_ints(path: str) -> list[int]:
    with open(path) as f:
        return [int(x) for x in f.read().split()]


def _write(path: str, text: str) -> None:
    with open(path, "w") as f:
        f.write(text)


class Keyboard:
    def __init__(self):
        self.leds: list[str] = []  # index -> sysfs dir
        self.refresh()

    def refresh(self) -> None:
        found = {}
        for d in glob.glob(LED_GLOB):
            m = _SUFFIX.search(d)
            if not m:
                continue
            found[int(m.group(1) or 0)] = d
        self.leds = [found[i] for i in sorted(found)]
        if self.leds:
            self.max_brightness = _read_int(os.path.join(self.leds[0], "max_brightness"))
            self.max_intensity = max(_read_ints(os.path.join(self.leds[0], "multi_max_intensity")))
        else:
            self.max_brightness = 0
            self.max_intensity = 0

    @property
    def available(self) -> bool:
        return bool(self.leds)

    # --- scaling -------------------------------------------------------------------
    def _to_hw(self, v: int) -> int:
        v = max(0, min(255, int(v)))
        return round(v * self.max_intensity / 255)

    def _from_hw(self, v: int) -> int:
        if self.max_intensity == 0:
            return 0
        return round(v * 255 / self.max_intensity)

    # --- queries ---------------------------------------------------------------------
    def brightness(self) -> int:
        return _read_int(os.path.join(self.leds[0], "brightness")) if self.leds else 0

    def color(self) -> tuple[int, int, int]:
        """Color of key 0, scaled to 0..255 (good enough for a whole-keyboard color)."""
        if not self.leds:
            return (0, 0, 0)
        r, g, b = _read_ints(os.path.join(self.leds[0], "multi_intensity"))[:3]
        return (self._from_hw(r), self._from_hw(g), self._from_hw(b))

    # --- control ---------------------------------------------------------------------
    def set_brightness(self, value: int) -> None:
        if not self.leds:
            return
        value = max(0, min(self.max_brightness, int(value)))
        _write(os.path.join(self.leds[0], "brightness"), str(value))

    def set_color(self, r: int, g: int, b: int) -> None:
        """Set every key to the same color. Brightness is left as is."""
        if not self.leds:
            return
        text = f"{self._to_hw(r)} {self._to_hw(g)} {self._to_hw(b)}"
        for d in self.leds:
            _write(os.path.join(d, "multi_intensity"), text)
        # The driver only pushes the frame to the keyboard on a brightness write.
        self.set_brightness(self.brightness())

    def set_key_colors(self, entries: list[tuple[int, int, int, int]]) -> None:
        """entries: (led_index, r, g, b)."""
        if not self.leds:
            return
        for idx, r, g, b in entries:
            if 0 <= idx < len(self.leds):
                _write(os.path.join(self.leds[idx], "multi_intensity"),
                       f"{self._to_hw(r)} {self._to_hw(g)} {self._to_hw(b)}")
        self.set_brightness(self.brightness())
