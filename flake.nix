{
  description = "Twitter Article to PDF Converter";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    uv2nix.url = "github:pyproject-nix/uv2nix";
    uv2nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-nix.url = "github:pyproject-nix/pyproject.nix";
    pyproject-nix.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.url = "github:pyproject-nix/build-system-pkgs";
    pyproject-build-systems.inputs.nixpkgs.follows = "nixpkgs";
    pyproject-build-systems.inputs.pyproject-nix.follows = "pyproject-nix";
    pyproject-build-systems.inputs.uv2nix.follows = "uv2nix";
  };

  outputs = { self, nixpkgs, uv2nix, pyproject-nix, pyproject-build-systems }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      # Docker images only work on Linux
      dockerSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      forDockerSystems = nixpkgs.lib.genAttrs dockerSystems;
      # Helper to check if system supports Docker
      isDockerSystem = system: builtins.elem system dockerSystems;
    in {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          python = pkgs.python314;

          workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
          overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };

          pyprojectOverrides = final: prev: {
            # WeasyPrint needs native libs
            weasyprint = prev.weasyprint.overrideAttrs (old: {
              nativeBuildInputs = (old.nativeBuildInputs or []) ++ [
                pkgs.pkg-config
              ];
              buildInputs = (old.buildInputs or []) ++ [
                pkgs.pango
                pkgs.cairo
                pkgs.gdk-pixbuf
                pkgs.fontconfig
                pkgs.gobject-introspection
              ];
            });
          };

          pythonSet = (pkgs.callPackage pyproject-nix.build.packages {
            inherit python;
          }).overrideScope (
            pkgs.lib.composeManyExtensions [
              pyproject-build-systems.overlays.default
              overlay
              pyprojectOverrides
            ]
          );

          venv = pythonSet.mkVirtualEnv "twitter-articlenator-env" workspace.deps.default;

          # Docker-specific definitions (only for Linux)
          dockerPkgs = if isDockerSystem system then
            let
              # Runtime dependencies for the container
              runtimeDeps = [
                pkgs.pango
                pkgs.cairo
                pkgs.gdk-pixbuf
                pkgs.fontconfig
                pkgs.glib
                pkgs.harfbuzz
                pkgs.freetype
                pkgs.zlib
                pkgs.gobject-introspection
                # Fonts for PDF rendering
                pkgs.dejavu_fonts
                pkgs.liberation_ttf
                pkgs.noto-fonts
                # Playwright/Chromium dependencies
                pkgs.playwright-driver.browsers
                pkgs.chromium
                pkgs.nss
                pkgs.nspr
                pkgs.atk
                pkgs.cups
                pkgs.libdrm
                pkgs.gtk3
                pkgs.alsa-lib
                pkgs.at-spi2-atk
                pkgs.at-spi2-core
                pkgs.libxkbcommon
                pkgs.xorg.libX11
                pkgs.xorg.libXcomposite
                pkgs.xorg.libXdamage
                pkgs.xorg.libXext
                pkgs.xorg.libXfixes
                pkgs.xorg.libXrandr
                pkgs.mesa
                pkgs.expat
                pkgs.dbus
                # Basic utilities
                pkgs.coreutils
                pkgs.bash
                pkgs.cacert
              ];

              # Create fontconfig cache
              fontsConf = pkgs.makeFontsConf {
                fontDirectories = [
                  pkgs.dejavu_fonts
                  pkgs.liberation_ttf
                  pkgs.noto-fonts
                ];
              };

              # Wrapper script to run the app
              entrypoint = pkgs.writeShellScriptBin "entrypoint" ''
                export FONTCONFIG_FILE="${fontsConf}"
                export GI_TYPELIB_PATH="${pkgs.lib.makeSearchPath "lib/girepository-1.0" [
                  pkgs.pango pkgs.gdk-pixbuf pkgs.gobject-introspection
                ]}"
                export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath runtimeDeps}"
                export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
                export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
                export HOME="/tmp"
                export XDG_CONFIG_HOME="/data/config"
                export TWITTER_ARTICLENATOR_CONFIG_DIR="/data/config"
                export TWITTER_ARTICLENATOR_OUTPUT_DIR="/data/output"

                exec ${venv}/bin/twitter-articlenator "$@"
              '';
            in {
              docker = pkgs.dockerTools.buildLayeredImage {
                name = "twitter-articlenator";
                tag = "latest";

                contents = [
                  venv
                  entrypoint
                ] ++ runtimeDeps;

                extraCommands = ''
                  mkdir -p data/config data/output tmp
                '';

                config = {
                  Entrypoint = [ "${entrypoint}/bin/entrypoint" ];
                  Env = [
                    "PYTHONUNBUFFERED=1"
                    "TWITTER_ARTICLENATOR_JSON_LOGGING=true"
                  ];
                  ExposedPorts = {
                    "5001/tcp" = {};
                  };
                  Volumes = {
                    "/data" = {};
                  };
                  WorkingDir = "/";
                  Labels = {
                    "org.opencontainers.image.source" = "https://github.com/user/twitter-articlenator";
                    "org.opencontainers.image.description" = "Twitter Article to PDF Converter";
                  };
                };
              };
            }
          else {};
        in {
          default = venv;
        } // dockerPkgs
      );

      apps = forAllSystems (system: {
        default = {
          type = "app";
          program = "${self.packages.${system}.default}/bin/twitter-articlenator";
        };
      });

      devShells = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
          isDarwin = pkgs.stdenv.isDarwin;
          libPath = pkgs.lib.makeLibraryPath [
            pkgs.pango
            pkgs.cairo
            pkgs.gdk-pixbuf
            pkgs.fontconfig
            pkgs.glib
            pkgs.harfbuzz
            pkgs.freetype
            pkgs.zlib
          ];
        in {
          default = pkgs.mkShell {
            packages = [
              pkgs.uv
              pkgs.python314
              # WeasyPrint native dependencies
              pkgs.pango
              pkgs.cairo
              pkgs.gdk-pixbuf
              pkgs.gobject-introspection
              pkgs.fontconfig
              pkgs.glib
              pkgs.harfbuzz
              pkgs.freetype
              pkgs.pkg-config
            ];
            shellHook = ''
              ${if isDarwin then ''
                export DYLD_LIBRARY_PATH="${libPath}:$DYLD_LIBRARY_PATH"
              '' else ''
                export LD_LIBRARY_PATH="${libPath}:$LD_LIBRARY_PATH"
              ''}
              export GI_TYPELIB_PATH="${pkgs.lib.makeSearchPath "lib/girepository-1.0" [
                pkgs.pango pkgs.gdk-pixbuf pkgs.gobject-introspection
              ]}"
              # Don't use Nix Playwright browsers - version mismatch with Python package
              unset PLAYWRIGHT_BROWSERS_PATH
              unset PYTHONPATH

              # Auto-sync Python dependencies
              echo "Syncing Python dependencies..."
              uv sync --quiet

              # Install Playwright browsers if needed
              if [ ! -d "$HOME/.cache/ms-playwright/chromium"* ]; then
                echo "Installing Playwright browsers..."
                uv run playwright install chromium
              fi

              echo "Ready! Run: uv run twitter-articlenator"
            '';
          };
        }
      );
    };
}
