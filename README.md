# XMG Forgot Apex (xfa)

Fan and keyboard-backlight control for the **XMG APEX 16 MAX (M25)** (Schenker, board
`X6DR5xxK`, Uniwill/Tongfang design). GNOME-first, with a CLI. No TUXEDO software needed.

| Piece | What it is |
|---|---|
| `xfad` | Root daemon. Talks to the EC directly, runs the fan curve, drives the keyboard LEDs, remembers your settings, re-applies them after resume. Exposes everything on the system D-Bus as `dev.nulliche.xfa`. |
| `xfa` | CLI client for the daemon. |
| `xfa@nulliche.dev` | GNOME Shell (48–50) Quick Settings tile: profile (tile click cycles it like the hardware button, OSD on button press), fan mode, curve presets, manual slider, temps, keyboard colors. |
| `flake.nix` | `packages.xfa`, `packages.xfa-gnome-extension`, `nixosModules.default`, `overlays.default`. |

## Performance profiles

Three profiles, named like power-profiles-daemon's so they *are* GNOME's **Power Mode**:
`power-saver`, `balanced`, `performance`. Measured on this machine (sustained all-core load):

| Profile | EC mode (reg 0x0751) | CPU package | EC fan curve |
|---|---|---|---|
| power-saver | 0xA0 | ~46 W, ~1.1 GHz | quiet |
| balanced | 0x20 | ~81 W, ~3.2 GHz | moderate |
| performance | 0x30 | ~81 W, ~3.2 GHz | aggressive |

`xfad` keeps three things in sync, in both directions:

* the EC mode register (what the firmware uses for power limits and its own fan curve),
* power-profiles-daemon's `ActiveProfile` (GNOME's Power Mode in Quick Settings, also
  switched automatically on battery),
* the **mode button** next to the power key. It only raises a WMI event, so the daemon polls
  the EC register and treats a change as a button press. The extension shows an OSD.

Each profile has its own fan settings (auto / curve / manual, curve points, source). The
`Fan` D-Bus interface and `xfa fan`/`xfa curve` always act on the active profile. TUXEDO
Control Center's fan tables ship as presets: Silent, Quiet, Balanced, Cool, Freezy
(`xfa preset Cool`, or the "Load curve preset" submenu). Defaults: power-saver → Silent,
balanced → Quiet, performance → Balanced, all in `auto` (EC) mode until you opt in.

`xfa profile sync off` decouples the EC mode from GNOME's Power Mode if you ever want that.

## How it works

* Fans: the EC has a 16-bit register space reachable through a 5-byte mailbox in standard EC
  RAM, the same one the firmware's WMI methods and tuxedo-drivers use. `xfad` drives it via
  the in-tree `ec_sys` module (`write_support=1`). Manual control = set bit `0x40` in register
  `0x0751`, write duty `0..200` to `0x1804`/`0x1809`; the daemon re-asserts every poll so the
  EC never takes the fans back. Clearing the bit returns to the EC's own curve. Details in
  `docs/findings.md`.
* Keyboard: the ITE 8291 USB controller is handled by tuxedo-drivers' `ite_8291` module
  (which does **not** have the TUXEDO-only DMI check), giving 126 multicolor LEDs under
  `/sys/class/leds/rgb:kbd_backlight*`. `xfad` writes the colors with the driver's
  `buffer_input` switch so a whole-keyboard change is one USB frame, not 126.
* The stock `ite_8291` clamps brightness (and color intensity) at 50, but this keyboard keeps
  getting brighter up to 100 (checked with raw HID reports, `contrib/ite8291-raw-brightness.py`).
  The module builds the driver with `contrib/ite8291-brightness-100.patch`, so sysfs, UPower
  and GNOME's slider all get the 0..100 range.
* UPower writes a brightness change to *every* `*kbd_backlight*` LED, i.e. 126 times here,
  and upstream `ite_8291` sends a full USB frame per write (2.6 s per change, and GNOME's
  slider queues one change per step). `contrib/ite8291-skip-noop-writes.patch` makes the
  driver skip the frame when nothing changed: 0.02 s per change.
* Keyboard brightness is GNOME's: UPower adopts any `*kbd_backlight` LED, so GNOME's own
  Quick Settings slider, the Fn keys and idle dimming already work. `xfad` never re-applies a
  brightness of its own; `xfa kbd brightness` goes through UPower so everything stays in sync.
  The extension therefore only offers colors.
* Safety: when `xfad` stops, or the machine suspends, fans go back to EC automatic. Speeds
  below 20 % are clamped (fan stall), "off" is written as duty 1 (duty 0 makes the EC spin
  up for 3 minutes first).

## NixOS

```nix
# flake.nix
inputs.xmg-forgot-apex.url = "git+ssh://git@git.nulliche.dev/Narrator/xmg-forgot-apex.git";

# configuration
imports = [ inputs.xmg-forgot-apex.nixosModules.default ];
services.xmg-forgot-apex.enable = true;
# optional knobs:
#   services.xmg-forgot-apex.interval = 2.0;            # control loop seconds
#   services.xmg-forgot-apex.keyboard.enable = true;    # loads ite_8291 (default)
#   services.xmg-forgot-apex.gnomeExtension.enable = true;
```

Then enable the extension once: `gnome-extensions enable xfa@nulliche.dev` (or the
Extensions app) and log out/in. Remove `hardware.tuxedo-rs` if you still have it; tailord and
xfad would fight over the fans.

The module sets up: the `xfad` systemd service, the D-Bus policy file, `ec_sys` with write
support, the `ite_8291` module, and installs `xfa` plus the extension.

## CLI

```sh
xfa status [--json]
xfa profile                       # active profile, EC mode, sync state
xfa profile performance           # also switches GNOME's Power Mode
xfa profile show                  # all three profiles with their fan settings
xfa profile sync on|off
xfa preset                        # list presets;  xfa preset Quiet  loads it (curve mode)
xfa fan auto|curve|manual 55 [40] # fan settings of the active profile
xfa fan source max|cpu|gpu
xfa curve                         # show;  xfa curve "40:0,55:25,65:40,75:55,85:75,95:100"
xfa kbd color 00ff80 | brightness 25 | off | key 12 ff0000 13 00ff00 ...
xfa watch [-i 2]
xfa ec read 0x0751 | write ADDR VAL | dump 0x0f00 0x0f5f   # raw EC, root, bypasses daemon
```

Settings persist in `/var/lib/xfa/state.json`.

## D-Bus API

Bus `dev.nulliche.xfa`, object `/dev/nulliche/xfa`, interfaces `…Profile`, `…Fan`,
`…Keyboard`, `…Daemon`; properties update via `PropertiesChanged` every poll. The full introspection XML
is in `xmg_forgot_apex/dbus_api.py`. Anything (a Waybar module, a script) can use it.

## Development

```sh
nix build .#xfa
contrib/dev.sh start                    # private bus on /tmp/xfa-dev-bus + root xfad on it
XFA_BUS_ADDRESS=unix:path=/tmp/xfa-dev-bus result/bin/xfa status
contrib/dev.sh stop
```

The system bus on NixOS only reads policy files from the store, so the dev harness runs its
own `dbus-daemon`. The GNOME extension always uses the system bus, so it needs the NixOS
module installed. Syntax-check it with `node --input-type=module --check < extension.js`.

## Alternative: patched tuxedo-drivers

`contrib/tuxedo-drivers-xmg-apex16.patch` lets the whole TUXEDO stack load on this board
(`/dev/tuxedo_io`, tailord/TCC). Verified working, exported as `overlays.default` and the
`patchedTuxedoDrivers` option, but xfa doesn't use it.
