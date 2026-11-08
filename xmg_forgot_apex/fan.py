"""Fan control for the XMG APEX 16 MAX (M25) / board X6DR5xxK (Uniwill/Tongfang EC).

Register map (EC extended address space, see ec.py), taken from tuxedo-drivers and
verified on this machine:

    0x043E  CPU temperature (deg C)
    0x044F  GPU temperature (deg C)
    0x0740  barebone id (0x1A on this board, unknown to tuxedo-drivers)
    0x0751  mode flags; bit 0x40 = "full fan mode" (manual duty is honoured while set)
    0x078E  bit 6 = EC claims "universal fan control" (custom tables) -- claimed but
            NOT honoured on this board, so we use the legacy path below
    0x1804  fan 0 (CPU side) duty, 0..200 (0xC8)
    0x1809  fan 1 (GPU side) duty, 0..200

Manual control ("legacy" path, what tuxedo-drivers uses for sibling boards such as
X6FR5xxY): set 0x0751 bit 0x40, then write the duty to 0x1804/0x1809. When the bit is
first set the EC jumps to 100% and ignores duty writes for a few seconds, so writes are
retried until the readback matches. Clearing the bit returns the fans to the EC's own
curve. Duty 1 stops the fan; duty 0 makes the EC spin to 30% for ~3 min first.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, asdict

from .ec import EcMailbox

MAX_RAW = 0xC8  # 200
MIN_ON_PERCENT = 20  # below this the fan motor may stall; mirrors tuxedo-drivers

R_TEMP_CPU = 0x043E
R_TEMP_GPU = 0x044F
R_BAREBONE_ID = 0x0740
R_MODE = 0x0751
MODE_FULL_FAN = 0x40
R_FANCTL_STATUS = 0x078E
R_USE_TBL0 = 0x07C5  # bit7: separate cpu/gpu custom tables (universal path, unused)
R_USE_TBL1 = 0x07C6  # bit2: enable custom tables (universal path, unused)
R_FAN = (0x1804, 0x1809)

SUPPORTED_BOARDS = {"X6DR5xxK"}


@dataclass
class FanStatus:
    cpu_temp: int
    gpu_temp: int
    fan_duty: tuple[int, int]        # raw 0..200
    fan_percent: tuple[int, int]
    manual: bool                      # full-fan-mode bit set
    mode_reg: int
    universal_fan_ctl_flag: bool
    barebone_id: int

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fan_duty"] = list(self.fan_duty)
        d["fan_percent"] = list(self.fan_percent)
        return d


def percent_to_raw(pct: int) -> int:
    pct = max(0, min(100, int(pct)))
    raw = pct * MAX_RAW // 100
    min_on = MIN_ON_PERCENT * MAX_RAW // 100
    if raw < min_on // 2:
        raw = 0
    elif raw < min_on:
        raw = min_on
    return 1 if raw == 0 else raw


def raw_to_percent(raw: int) -> int:
    return 0 if raw <= 1 else raw * 100 // MAX_RAW


class FanController:
    def __init__(self, ec: EcMailbox):
        self.ec = ec

    # --- queries -------------------------------------------------------------------
    def temps(self) -> tuple[int, int]:
        return self.ec.read(R_TEMP_CPU), self.ec.read(R_TEMP_GPU)

    def duty(self) -> tuple[int, int]:
        return self.ec.read(R_FAN[0]), self.ec.read(R_FAN[1])

    def is_manual(self) -> bool:
        return bool(self.ec.read(R_MODE) & MODE_FULL_FAN)

    def status(self) -> FanStatus:
        cpu, gpu = self.temps()
        d = self.duty()
        mode = self.ec.read(R_MODE)
        return FanStatus(
            cpu_temp=cpu,
            gpu_temp=gpu,
            fan_duty=d,
            fan_percent=(raw_to_percent(d[0]), raw_to_percent(d[1])),
            manual=bool(mode & MODE_FULL_FAN),
            mode_reg=mode,
            universal_fan_ctl_flag=bool(self.ec.read(R_FANCTL_STATUS) & 0x40),
            barebone_id=self.ec.read(R_BAREBONE_ID),
        )

    # --- control -------------------------------------------------------------------
    def set_raw(self, raw0: int, raw1: int, settle_timeout: float = 20.0) -> tuple[int, int]:
        """Force both fans to raw duties. Returns the duties read back."""
        self.ec.update_bits(R_MODE, set_mask=MODE_FULL_FAN)
        deadline = time.monotonic() + settle_timeout
        while True:
            self.ec.write(R_FAN[0], raw0)
            self.ec.write(R_FAN[1], raw1)
            time.sleep(0.5)
            d = self.duty()
            if d == (raw0, raw1) or time.monotonic() > deadline:
                return d

    def set_percent(self, pct0: int, pct1: int | None = None) -> tuple[int, int]:
        if pct1 is None:
            pct1 = pct0
        return self.set_raw(percent_to_raw(pct0), percent_to_raw(pct1))

    def reassert_raw(self, raw0: int, raw1: int) -> None:
        """Cheap periodic re-write for a control loop (no settle wait)."""
        self.ec.update_bits(R_MODE, set_mask=MODE_FULL_FAN)
        self.ec.write(R_FAN[0], raw0)
        self.ec.write(R_FAN[1], raw1)

    def set_auto(self) -> None:
        """Hand control back to the EC's built-in fan curve."""
        self.ec.update_bits(R_MODE, clear_mask=MODE_FULL_FAN)
        # Make sure the (ineffective here) custom-table bits are off as well.
        self.ec.update_bits(R_USE_TBL1, clear_mask=0x04)
        self.ec.update_bits(R_USE_TBL0, clear_mask=0x80)


# --- fan curve -------------------------------------------------------------------------
DEFAULT_CURVE = [(45, 0), (55, 25), (65, 35), (75, 50), (85, 70), (95, 100)]


def parse_curve(text: str) -> list[tuple[int, int]]:
    """Parse "45:0,55:25,65:35" into sorted (temp, percent) points."""
    pts = []
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        t, p = item.split(":")
        pts.append((int(t), int(p)))
    if len(pts) < 1:
        raise ValueError("curve needs at least one temp:percent point")
    pts.sort()
    return pts


def curve_percent(curve: list[tuple[int, int]], temp: int) -> int:
    """Linear interpolation on the curve, clamped at the ends."""
    if temp <= curve[0][0]:
        return curve[0][1]
    if temp >= curve[-1][0]:
        return curve[-1][1]
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if t0 <= temp <= t1:
            if t1 == t0:
                return p1
            return round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return curve[-1][1]
