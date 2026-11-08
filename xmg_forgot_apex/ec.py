"""Low-level access to the Uniwill/Tongfang embedded controller (EC).

The EC exposes a 16-bit address space (0x0000-0xFFFF) through a small mailbox in the
standard ACPI EC RAM. This is the same mechanism the firmware's own WMI methods
(DSDT: \\_SB.AMW0.RKBC / WKBC) and tuxedo-drivers' ``uniwill_wmi.c`` "direct" path use:

    EC RAM 0x8A  LDAT   address low byte
    EC RAM 0x8B  HDAT   address high byte
    EC RAM 0x8C  flags  bit0 RFLG read request, bit1 WFLG write request,
                        bit2 BFLG busy/lock, bit3 CFLG command, bit7 DRDY done
    EC RAM 0x8D  CMDL   data low byte
    EC RAM 0x8E  CMDH   data high byte

We reach EC RAM through the kernel's ``ec_sys`` debugfs interface, which must be loaded
with write support::

    modprobe ec_sys write_support=1

Concurrent users are serialised with a file lock so two processes never interleave a
mailbox transaction.
"""
from __future__ import annotations

import fcntl
import os
import time

EC_SYS_IO = "/sys/kernel/debug/ec/ec0/io"
LOCK_FILE = "/run/lock/xmg-forgot-apex.lock"

LDAT, HDAT, FLAGS, CMDL, CMDH = 0x8A, 0x8B, 0x8C, 0x8D, 0x8E
RFLG, WFLG, BFLG, CFLG, DRDY = 1 << 0, 1 << 1, 1 << 2, 1 << 3, 1 << 7

WAIT_CYCLES = 30
WAIT_DELAY = 0.015  # seconds; tuxedo-drivers uses 30 x 15 ms


class EcError(RuntimeError):
    pass


class EcUnavailable(EcError):
    pass


class EcMailbox:
    """Read/write the EC's extended address space via the ec_sys mailbox."""

    def __init__(self, path: str = EC_SYS_IO, lock_path: str = LOCK_FILE):
        try:
            self._fd = os.open(path, os.O_RDWR)
        except FileNotFoundError:
            raise EcUnavailable(
                f"{path} not found: load the ec_sys module (modprobe ec_sys write_support=1)"
            ) from None
        except PermissionError:
            raise EcUnavailable(
                f"cannot open {path}: run as root, and make sure ec_sys was loaded with write_support=1"
            ) from None
        try:
            self._lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self._lock_fd = -1  # best effort; still works single-process

    def close(self) -> None:
        os.close(self._fd)
        if self._lock_fd >= 0:
            os.close(self._lock_fd)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- raw EC RAM (0x00-0xFF) -------------------------------------------------
    def ram_read(self, off: int) -> int:
        return os.pread(self._fd, 1, off)[0]

    def ram_write(self, off: int, val: int) -> None:
        os.pwrite(self._fd, bytes([val & 0xFF]), off)

    # --- mailbox ------------------------------------------------------------------
    def _lock(self):
        if self._lock_fd >= 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)

    def _unlock(self):
        if self._lock_fd >= 0:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)

    def _wait_ready(self) -> bool:
        for _ in range(WAIT_CYCLES):
            time.sleep(WAIT_DELAY)
            if self.ram_read(FLAGS) & DRDY:
                return True
        return False

    def read16(self, addr: int) -> tuple[int, int]:
        """Return (data_low, data_high) at ``addr``. data_high is normally addr+1."""
        self._lock()
        try:
            flags = self.ram_read(FLAGS) | BFLG
            self.ram_write(FLAGS, flags)
            self.ram_write(LDAT, addr & 0xFF)
            self.ram_write(HDAT, (addr >> 8) & 0xFF)
            self.ram_write(FLAGS, (flags & ~DRDY) | RFLG)
            ok = self._wait_ready()
            lo, hi = (self.ram_read(CMDL), self.ram_read(CMDH)) if ok else (0, 0)
            self.ram_write(FLAGS, 0)
        finally:
            self._unlock()
        if not ok:
            raise EcError(f"EC read timeout at 0x{addr:04x}")
        return lo, hi

    def read(self, addr: int) -> int:
        return self.read16(addr)[0]

    def write(self, addr: int, data_lo: int, data_hi: int = 0) -> None:
        self._lock()
        try:
            flags = self.ram_read(FLAGS) | BFLG
            self.ram_write(FLAGS, flags)
            self.ram_write(LDAT, addr & 0xFF)
            self.ram_write(HDAT, (addr >> 8) & 0xFF)
            self.ram_write(CMDL, data_lo & 0xFF)
            self.ram_write(CMDH, data_hi & 0xFF)
            self.ram_write(FLAGS, (flags & ~DRDY) | WFLG)
            ok = self._wait_ready()
            self.ram_write(FLAGS, 0)
        finally:
            self._unlock()
        if not ok:
            raise EcError(f"EC write timeout at 0x{addr:04x}")

    def write_verified(self, addr: int, val: int, tries: int = 3) -> bool:
        for _ in range(tries):
            self.write(addr, val)
            if self.read(addr) == (val & 0xFF):
                return True
        return False

    def update_bits(self, addr: int, set_mask: int = 0, clear_mask: int = 0) -> int:
        cur = self.read(addr)
        new = (cur | set_mask) & ~clear_mask & 0xFF
        if new != cur:
            self.write(addr, new)
        return new
