# Pinned Botasaurus browser engine and its non-nixpkgs dependencies.
{ pkgs }:
let
  ps = pkgs.python3Packages;
  javascriptFixes = ps.buildPythonPackage {
    pname = "javascript-fixes";
    version = "1.1.29";
    pyproject = true;
    build-system = [ ps.setuptools ];
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/cf/b1/2be59af0e2dd9592d1ac9b910f1d1736d48c7a2e9ba6b4a15401796c344c/javascript_fixes-1.1.29.tar.gz";
      sha256 = "81df118bcf2cd390c81f1c4bb4885784ad077b8ac5274b8b1a58d028fd45d621";
    };
    doCheck = false;
  };
  proxyAuthentication = ps.buildPythonPackage {
    pname = "botasaurus-proxy-authentication";
    version = "1.0.16";
    pyproject = true;
    build-system = [ ps.setuptools ];
    dependencies = [ javascriptFixes ];
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/1e/f0/9a9de72d09666b9e3c66cfac26a9c41fd78cab643d75e044dc73560cc601/botasaurus_proxy_authentication-1.0.16.tar.gz";
      sha256 = "4a7b8bf030acd018288e3e67f771e542f84ac871d1d78c9daf7ad77118bebb8c";
    };
    doCheck = false;
  };
  # The driver imports botasaurus_humancursor (WebCursor) to click Cloudflare
  # challenges when get(..., bypass_cloudflare=True) meets one, but its
  # package metadata does not declare it. Without it the bypass fails with
  # ModuleNotFoundError (found in hermes on 2026-10-10).
  humanCursor = ps.buildPythonPackage {
    pname = "botasaurus-humancursor";
    version = "4.0.83";
    pyproject = true;
    build-system = [ ps.setuptools ];
    dependencies = with ps; [ numpy pytweening ];
    src = pkgs.fetchurl {
      url = "https://files.pythonhosted.org/packages/58/26/c86c94daf6cde237a24e60fbbbe4d7f1c0e5e0a84c4e591dd21c74df59d7/botasaurus_humancursor-4.0.83.tar.gz";
      sha256 = "28db7af683e4ff85059bbe259c254e1dcd21d116afbe3af6d91557db7915c8a9";
    };
    # It imports botasaurus_driver, so its import is checked in the driver.
    doCheck = false;
  };
in ps.buildPythonPackage {
  pname = "botasaurus-driver";
  version = "4.0.101";
  pyproject = true;
  build-system = [ ps.setuptools ];
  dependencies = with ps; [ requests deprecated psutil websocket-client pyvirtualdisplay proxyAuthentication humanCursor ];
  src = pkgs.fetchurl {
    url = "https://files.pythonhosted.org/packages/4e/0d/3de4b1810e4d5a213e0df563da10e870783a206c6c422fd7799a9001aaa0/botasaurus_driver-4.0.101.tar.gz";
    sha256 = "4f66a974724354bcb4ad89ec33da2876521eab3b22cfea5e19e673fc9db5fe28";
  };
  pythonImportsCheck = [ "botasaurus_driver" "botasaurus_humancursor" ];
  doCheck = false;
}
