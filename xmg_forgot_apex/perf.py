"""EC performance mode (register 0x0751) and its mapping onto power-profiles-daemon names.

Measured on the XMG APEX 16 MAX (sustained all-core load, AC):

    0xA0  power-saver   ~46 W package, ~1.1 GHz, EC fan curve quiet
    0x20  balanced      ~81 W, ~3.2 GHz, moderate fan curve
    0x30  performance   ~81 W, ~3.2 GHz, aggressive fan curve
    0x10  firmware boot default, odd state (~52 W, fans 100 %); TUXEDO calls it "overboost"

The mode button next to the power key cycles 0xA0 -> 0x30 -> 0x20 -> 0xA0 and raises a
WMI event only (no input event), so xfad detects presses by polling the register.
Bit 0x40 is the manual fan-speed flag and is kept as is.
"""
from __future__ import annotations

R_MODE = 0x0751
MODE_FAN_MANUAL = 0x40
MODE_PROFILE_MASK = 0xBF  # everything except the fan flag

PROFILE_TO_EC = {"power-saver": 0xA0, "balanced": 0x20, "performance": 0x30}
EC_TO_PROFILE = {v: k for k, v in PROFILE_TO_EC.items()}
PROFILES = tuple(PROFILE_TO_EC)


def profile_of(reg: int) -> str | None:
    return EC_TO_PROFILE.get(reg & MODE_PROFILE_MASK)


class PerfMode:
    def __init__(self, ec):
        self.ec = ec

    def read_raw(self) -> int:
        return self.ec.read(R_MODE)

    def read(self) -> str | None:
        return profile_of(self.read_raw())

    def write(self, profile: str) -> None:
        if profile not in PROFILE_TO_EC:
            raise ValueError(f"unknown profile {profile!r}")
        cur = self.read_raw()
        new = (cur & MODE_FAN_MANUAL) | PROFILE_TO_EC[profile]
        if new != cur:
            self.ec.write(R_MODE, new)
