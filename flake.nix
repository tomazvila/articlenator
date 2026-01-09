{
  description = "Twitter Article to PDF Converter";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" "x86_64-darwin" "aarch64-darwin" ];
      dockerSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
      isDockerSystem = system: builtins.elem system dockerSystems;
      # Git commit hash (8 chars) - uses shortRev if available, "dirty" otherwise
      gitCommit = if self ? shortRev then self.shortRev else "dirty";
    in {
      packages = forAllSystems (system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # Python with all dependencies - SINGLE SOURCE OF TRUTH
          pythonDeps = ps: with ps; [
            flask
            playwright
            weasyprint
            beautifulsoup4
            lxml
            structlog
            orjson
            python-slugify
            httpx
          ];

          python = pkgs.python3.withPackages pythonDeps;

          # Build the application package
          app = pkgs.python3Packages.buildPythonApplication {
            pname = "twitter-articlenator";
            version = "0.1.0";
            format = "pyproject";

            src = ./.;

            nativeBuildInputs = with pkgs.python3Packages; [
              hatchling
            ];

            propagatedBuildInputs = pythonDeps pkgs.python3Packages;

            # Skip tests during build (run separately)
            doCheck = false;
          };

          # Docker-specific definitions (only for Linux)
          dockerPkgs = if isDockerSystem system then
            let
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
                # Fonts
                pkgs.dejavu_fonts
                pkgs.liberation_ttf
                pkgs.noto-fonts
                # Playwright browsers (version matches python3Packages.playwright)
                pkgs.playwright-driver.browsers
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
                pkgs.coreutils
                pkgs.bash
                pkgs.cacert
              ];

              fontsConf = pkgs.makeFontsConf {
                fontDirectories = [
                  pkgs.dejavu_fonts
                  pkgs.liberation_ttf
                  pkgs.noto-fonts
                ];
              };

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

                exec ${app}/bin/twitter-articlenator "$@"
              '';
            in {
              docker = pkgs.dockerTools.buildLayeredImage {
                name = "twitter-articlenator";
                tag = "latest";

                contents = [
                  app
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
                    "GIT_COMMIT=${gitCommit}"
                  ];
                  ExposedPorts = {
                    "5001/tcp" = {};
                  };
                  Volumes = {
                    "/data" = {};
                  };
                  WorkingDir = "/";
                  Labels = {
                    "org.opencontainers.image.source" = "https://github.com/tomazvila/articlenator";
                    "org.opencontainers.image.description" = "Twitter Article to PDF Converter";
                    "org.opencontainers.image.revision" = gitCommit;
                  };
                };
              };
            }
          else {};
        in {
          default = app;
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

          # Dev dependencies
          pythonWithDeps = pkgs.python3.withPackages (ps: with ps; [
            # Runtime deps
            flask
            playwright
            weasyprint
            beautifulsoup4
            lxml
            structlog
            orjson
            python-slugify
            httpx
            # Dev/test deps
            pytest
            pytest-cov
            pytest-asyncio
            pytest-playwright
            hatchling
          ]);

          libPath = pkgs.lib.makeLibraryPath [
            pkgs.pango
            pkgs.cairo
            pkgs.gdk-pixbuf
            pkgs.fontconfig
            pkgs.glib
            pkgs.harfbuzz
            pkgs.freetype
            pkgs.zlib
            pkgs.stdenv.cc.cc.lib
          ];

          fontsConf = pkgs.makeFontsConf {
            fontDirectories = [
              pkgs.dejavu_fonts
              pkgs.liberation_ttf
              pkgs.noto-fonts
            ];
          };
        in {
          default = pkgs.mkShell {
            packages = [
              pythonWithDeps
              pkgs.ruff
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
              # Fonts
              pkgs.dejavu_fonts
              pkgs.liberation_ttf
              pkgs.noto-fonts
              # Playwright browsers
              pkgs.playwright-driver.browsers
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
              export FONTCONFIG_FILE="${fontsConf}"
              export PLAYWRIGHT_BROWSERS_PATH="${pkgs.playwright-driver.browsers}"
              export PYTHONPATH="$PWD/src:$PYTHONPATH"

              echo "Python: $(python --version)"
              echo ""
              echo "Commands:"
              echo "  python -m twitter_articlenator  # Run app"
              echo "  pytest tests/unit               # Run tests"
              echo "  ruff check src/                 # Lint"
              echo "  nix build .#docker              # Build Docker image"
            '';
          };
        }
      );
    };
}
