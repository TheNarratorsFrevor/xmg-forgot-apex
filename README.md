# XMG Forgot Apex (xfa)

Fan and keyboard-backlight control for the **XMG APEX 16 MAX (M25)** (Schenker, board
`X6DR5xxK`, Uniwill/Tongfang design). GNOME-first, with a CLI. No TUXEDO software needed.

| Piece | What it is |
|---|---|
| `xfad` | Root daemon. Talks to the EC directly, runs the fan curve, drives the keyboard LEDs, remembers your settings, re-applies them after resume. Exposes everything on the system D-Bus as `dev.nulliche.xfa`. |
| `xfa` | CLI client for the daemon. |
| `xfa@nulliche.dev` | GNOME Shell (48–50) Quick Settings tile: fan mode, manual speed slider, temps, keyboard color presets. |
| `flake.nix` | `packages.xfa`, `packages.xfa-gnome-extension`, `nixosModules.default`, `overlays.default`. |

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
xfa fan auto                 # EC curve
xfa fan curve                # xfad curve
xfa fan manual 55            # both fans 55 %, or: xfa fan manual 70 40
xfa fan source max|cpu|gpu   # which temperature the curve follows
xfa curve                    # show;  xfa curve "40:0,55:25,65:40,75:55,85:75,95:100"
xfa kbd color 00ff80 | brightness 25 | off | key 12 ff0000 13 00ff00 ...
xfa watch [-i 2]
xfa ec read 0x1804 | write ADDR VAL | dump 0x0f00 0x0f5f   # raw EC, root, bypasses daemon
```

Settings persist in `/var/lib/xfa/state.json`.

## D-Bus API

Bus `dev.nulliche.xfa`, object `/dev/nulliche/xfa`, interfaces `…Fan`, `…Keyboard`,
`…Daemon`; properties update via `PropertiesChanged` every poll. The full introspection XML
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
