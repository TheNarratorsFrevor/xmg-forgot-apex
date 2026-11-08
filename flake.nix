{
  description = "XMG Forgot Apex (xfa): fan and keyboard control for the XMG APEX 16 MAX (M25)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" ];
      forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
      version = "0.2.1";
    in
    {
      packages = forAll (pkgs: rec {
        # Daemon (xfad) + CLI (xfa) + D-Bus policy.
        xfa = pkgs.python3Packages.buildPythonApplication {
          pname = "xmg-forgot-apex";
          inherit version;
          pyproject = true;
          src = ./.;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [ pkgs.python3Packages.pygobject3 ];
          nativeBuildInputs = [ pkgs.gobject-introspection pkgs.wrapGAppsNoGuiHook ];
          buildInputs = [ pkgs.glib ];
          dontWrapGApps = true;
          preFixup = ''
            makeWrapperArgs+=("''${gappsWrapperArgs[@]}")
          '';
          postInstall = ''
            install -Dm644 data/dev.nulliche.xfa.conf $out/share/dbus-1/system.d/dev.nulliche.xfa.conf
          '';
          doCheck = false;
          meta = {
            description = "xfad daemon and xfa CLI for the XMG APEX 16 MAX";
            license = pkgs.lib.licenses.gpl2Plus;
            mainProgram = "xfa";
            platforms = [ "x86_64-linux" ];
          };
        };

        # GNOME Shell extension (Quick Settings tile).
        xfa-gnome-extension = pkgs.stdenvNoCC.mkDerivation {
          pname = "gnome-shell-extension-xfa";
          inherit version;
          src = ./gnome-extension;
          passthru.extensionUuid = "xfa@nulliche.dev";
          installPhase = ''
            runHook preInstall
            mkdir -p $out/share/gnome-shell/extensions
            cp -r "xfa@nulliche.dev" $out/share/gnome-shell/extensions/
            runHook postInstall
          '';
          meta = {
            description = "GNOME Shell Quick Settings tile for XMG Forgot Apex";
            license = pkgs.lib.licenses.gpl2Plus;
            platforms = [ "x86_64-linux" ];
          };
        };

        default = xfa;
      });

      apps = forAll (pkgs: {
        default = { type = "app"; program = "${self.packages.${pkgs.system}.xfa}/bin/xfa"; };
        xfad = { type = "app"; program = "${self.packages.${pkgs.system}.xfa}/bin/xfad"; };
      });

      # Overlay: tuxedo-drivers patched for this board (see contrib/). Only needed if you
      # want the full TUXEDO stack (/dev/tuxedo_io for tailord or TCC). xfa itself does
      # not need it; the keyboard only needs the stock ite_8291 module.
      overlays.default = final: prev: {
        linuxKernel = prev.linuxKernel // {
          packagesFor = kernel: (prev.linuxKernel.packagesFor kernel).extend (lpfinal: lpprev: {
            tuxedo-drivers = lpprev.tuxedo-drivers.overrideAttrs (o: {
              patches = (o.patches or [ ]) ++ [
                ./contrib/tuxedo-drivers-xmg-apex16.patch
                ./contrib/ite8291-brightness-100.patch
              ];
            });
          });
        };
      };

      nixosModules.default = { config, lib, pkgs, ... }:
        let
          cfg = config.services.xmg-forgot-apex;
          pkg = self.packages.${pkgs.system}.xfa;
          ext = self.packages.${pkgs.system}.xfa-gnome-extension;
          # Stock tuxedo-drivers plus the brightness-ceiling fix; only ite_8291 is loaded
          # from it. (With patchedTuxedoDrivers the overlay already carries both patches.)
          keyboardDrivers = config.boot.kernelPackages.tuxedo-drivers.overrideAttrs (o: {
            patches = (o.patches or [ ]) ++ pkgs.lib.optional (!cfg.patchedTuxedoDrivers) ./contrib/ite8291-brightness-100.patch;
          });
        in
        {
          options.services.xmg-forgot-apex = {
            enable = lib.mkEnableOption "XMG Forgot Apex (xfad daemon, xfa CLI, D-Bus policy, EC access)";
            interval = lib.mkOption {
              type = lib.types.float;
              default = 2.0;
              description = "Seconds between control-loop iterations.";
            };
            keyboard.enable = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Load the ite_8291 module (tuxedo-drivers, with the 0..100 brightness patch) for the RGB keyboard backlight.";
            };
            gnomeExtension.enable = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Install the GNOME Shell Quick Settings extension (enable it with gnome-extensions or the Extensions app).";
            };
            patchedTuxedoDrivers = lib.mkEnableOption "the patched tuxedo-drivers overlay (then set hardware.tuxedo-drivers.enable = true yourself)";
          };

          config = lib.mkIf cfg.enable {
            nixpkgs.overlays = lib.mkIf cfg.patchedTuxedoDrivers [ self.overlays.default ];

            environment.systemPackages = [ pkg ] ++ lib.optional cfg.gnomeExtension.enable ext;
            services.dbus.packages = [ pkg ];

            boot.kernelModules = [ "ec_sys" ] ++ lib.optionals cfg.keyboard.enable [ "ite_8291" ];
            boot.extraModprobeConfig = "options ec_sys write_support=1";
            boot.extraModulePackages = lib.optional cfg.keyboard.enable keyboardDrivers;

            systemd.services.xfad = {
              description = "XMG Forgot Apex hardware daemon";
              wantedBy = [ "multi-user.target" ];
              after = [ "systemd-modules-load.service" "dbus.service" ];
              requires = [ "dbus.service" ];
              serviceConfig = {
                Type = "simple";
                ExecStart = "${pkg}/bin/xfad --interval ${toString cfg.interval}";
                Restart = "on-failure";
                RestartSec = 3;
                StateDirectory = "xfa";
              };
            };
          };
        };
    };
}
