# Findings: XMG APEX 16 MAX (M25) fan control

Date: 2026-11-08. Kernel 7.2.9 (NixOS). BIOS N.1.18A02.

## Identity

| DMI field | Value |
|---|---|
| sys_vendor | SchenkerTechnologiesGmbH |
| product_name | XMG APEX 16 MAX (M25) |
| product_sku | XAP16XM25 |
| board_vendor | NB02 |
| board_name | X6DR5xxK |
| CPU | AMD Ryzen 9 8945HX (family 25, model 0x61) |

`NB02` is the board-vendor string Uniwill/Tongfang firmware uses on TUXEDO's Stellaris
line (tuxedo-drivers' `tuxedo_nb02_*`). The board name pattern `X6?R5xx?` matches
Stellaris 16 Gen7 boards `X6AR5xxY` / `X6FR5xxY` in tuxedo-drivers. EC barebone id
(register `0x0740`) is `0x1A`, which tuxedo-drivers does not know (its list ends at `0x18`).

## WMI

`\_SB.AMW0` exposes the standard Uniwill GUID set: `ABBC0F6A..6F` (methods BA/BB/BC) and
`ABBC0F70..72` (events). `uniwill_wmi` in tuxedo-drivers would bind. `WMBC` method id 4
calls `OEMG`, which dispatches on a 40-byte buffer:

| `SAC1` (bytes 4..7) | action |
|---|---|
| 0x0000 / 0x0001 | `WKBC(addr_lo, addr_hi, data_lo, data_hi)` EC write |
| 0x0100 | `RKBC(addr_lo, addr_hi)` EC read |
| 0x0200 | `SCMD(cmd)` EC command |
| 0x0300 | GPU mux / iGPU toggles |
| 0x0400 | return `TJMX` (0x5F) |
| 0x0500 | misc: `INOU.CTAF` (control-center "custom" flag), `PMSF`, `FMFG` ... |

`asus_wmi` loads because of the generic `ABBC0F6x` GUIDs but bails ("ASUS Management GUID
not found"). Harmless.

## EC access paths

1. **Mailbox in EC RAM** (used by `xmg-forgot-apex`, by DSDT `RKBC`/`WKBC`, and by tuxedo-drivers'
   `uw_ec_*_direct`):
   `0x8A` LDAT, `0x8B` HDAT, `0x8C` flags (bit0 RFLG, bit1 WFLG, bit2 BFLG, bit3 CFLG,
   bit7 DRDY), `0x8D` CMDL, `0x8E` CMDH. Reads return 16 bits (addr, addr+1).
   Reachable from user space via `ec_sys write_support=1`.
2. **Memory-mapped window** `0xFED50000..0xFED50FFF` (ACPI device `INOU0000`, OpRegion
   `ECMG`): mirrors EC addresses `0x000..0xFFF` and is readable via `/dev/mem`
   (e.g. CPU temp at `0xFED5043E`). Fan registers at `0x18xx` are outside the window.
3. **WMI** via `uniwill_wmi` kernel driver (needs tuxedo-drivers, see below).

## Register map (verified)

| Addr | Meaning | Observed |
|---|---|---|
| 0x043E | CPU temperature °C | 62..67 |
| 0x044F | GPU temperature °C | 57..59 |
| 0x0740 | barebone id | 0x1A |
| 0x0741 | uniwill "mode enable" | 0x80 |
| 0x0751 | mode flags, bit 0x40 = full fan mode (manual) | 0x10 idle |
| 0x0765/0x0766 | feature bits | 0xA1 / 0x90 |
| 0x078E | bit6 = "universal fan control" supported | 0xEC (set) |
| 0x07C5 | bit7 = separate CPU/GPU custom tables | 0 |
| 0x07C6 | bit2 = enable custom tables | 0 |
| 0x0F00..0x0F5F | custom fan tables (CPU end/start/speed, GPU end/start/speed, 16 zones) | all 0 |
| 0x1804 | fan 0 duty (0..0xC8) | tracks EC curve, e.g. 0x66 |
| 0x1809 | fan 1 duty (0..0xC8) | same |
| 0x1800..0x180F | `e0 c8 00 00 66 00 00 c8 00 66 00 0f 10 40 00 00` | 0x1801/0x1807 = 0xC8 look like max duty |

## Experiments

* **Universal path** (what tuxedo-drivers would use because `0x078E` bit6 is set): writing
  the tables and setting `0x07C5`/`0x07C6` is accepted (reads back) but has **no effect** on
  the fans. A direct write to `0x1804` without manual mode gives an immediate jump, but the
  EC ramps back to its own target within ~30 s.
* **Legacy path**: set `0x0751 |= 0x40`, the EC goes to 100% immediately and ignores duty
  writes for a few seconds, after that writes to `0x1804`/`0x1809` are honoured exactly and
  independently per fan (tested 30%, 35%, 40%, 60/25%, off). Clearing the bit returns to
  the EC curve within a second or two. This is the path `xmg-forgot-apex` uses.

## tuxedo-drivers / tuxedo-rs

* `tuxedo_compatibility_check` only allows DMI vendor `TUXEDO` or old CPUs; this machine
  fails both, so `tuxedo_keyboard` (which hosts the Uniwill fan code) and therefore
  `tuxedo_io` never load. `hardware.tuxedo-rs` (tailord) was installed on the host but logged
  "No tuxedo ioctl interface available" and did nothing.
* `contrib/tuxedo-drivers-xmg-apex16.patch` adds `X6DR5xxK` to the DMI allow list and makes
  `has_universal_ec_fan_control()` return 0 for it (so `uw_set_fan` uses the legacy path).
  Built for kernel 7.2.9 via `linuxPackages.tuxedo-drivers.overrideAttrs` and **tested**:
  * Without the second hunk, `W_UW_FANSPEED` only gives the transient bump (universal path).
  * With it, `W_UW_FANSPEED=140`, `W_UW_FANSPEED2=80` hold exactly; `W_UW_FANAUTO` restores
    the EC curve. `R_UW_HW_IF_STR` = `uniwill_wmi`, `R_UW_MODEL_ID` = 26, min speed 25%.
  * tailord restarts, logs "Connected to Tuxedo ioctl interface with version 0.3.9" and
    drives per-fan duties from its `/etc/tailord/fan/default.json` curve. (The old tailord
    instance that had been running without an interface core-dumped on stop, SIGABRT.)
  * `ite_8291` binds the keyboard (048d:600b) and registers 126 multicolor LEDs,
    intensities 0..50 upstream. Raw `09 02 <value>` feature reports show the panel gets
    brighter up to 100 (255 is not brighter than 100), so we patch
    `ITE8291_KBD_BRIGHTNESS_MAX` to 0x64 (`contrib/ite8291-brightness-100.patch`).
    Whole-keyboard writes should use `device/controls/buffer_input` (1 then 0) or the
    driver sends 126 USB frames (~6 s). tuxedo_keyboard exposes `fn_lock`, `charging_profile`,
    `TUXEDO Keyboard` input device. `charging_prio` is not offered for this board.
* Register `0x0751` low bits are the performance profile (`uw_set_performance_profile_v1`):
  `0xA0` powersave, `0x00` enthusiast, `0x10` overboost. The firmware default here is
  `0x10`; `tuxedo_keyboard` writes `0x00` at probe. Bit `0x40` (full fan mode) can be
  combined with any of them. Register `0x0741` bit 0 is the driver's "manual mode enable"
  (set at probe, cleared at remove).
* Timing detail: after the `0x40` bit is set, the EC runs 100% and ignores duty writes
  for a few seconds; tuxedo-drivers' ten quick writes right after enabling are lost, so the
  fans stay at 100% until the next write (tailord writes periodically, so this is harmless
  there; `xmg-forgot-apex` retries until the readback matches).

## Sources consulted

* tuxedo-drivers `src/uniwill_wmi.c`, `src/uniwill_keyboard.h` (`uw_set_fan`,
  `uw_set_fan_auto`, `set_full_fan_mode`, `has_universal_ec_fan_control`),
  `src/tuxedo_io/tuxedo_io.c`, `src/tuxedo_compatibility_check/`.
* tuxedo-control-center (TCC) uses `/dev/tuxedo_io` ioctls `W_UW_FANSPEED{,2}`,
  `W_UW_FANAUTO`, `R_UW_FANSPEED{,2}`, `R_UW_FAN_TEMP{,2}` (magic `0xEC+3/4`).
* This machine's DSDT: `docs/DSDT.dsl` (`AMW0`, `INOU`, `EC0` field maps).
