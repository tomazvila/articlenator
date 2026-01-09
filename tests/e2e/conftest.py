"""Playwright fixtures for E2E tests."""

import os
import socket
import subprocess
import sys
import time
from contextlib import closing

import pytest


def find_free_port():
    """Find a free port to run the server on."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def wait_for_server(port: int, timeout: float = 10.0) -> bool:
    """Wait for server to be ready."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
                s.connect(("127.0.0.1", port))
                return True
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    return False


@pytest.fixture(scope="session")
def flask_server(tmp_path_factory):
    """Start Flask server for E2E tests."""
    port = find_free_port()

    # Create temp directories for this test session
    tmp_dir = tmp_path_factory.mktemp("e2e")
    config_dir = tmp_dir / "config"
    output_dir = tmp_dir / "output"
    config_dir.mkdir()
    output_dir.mkdir()

    env = os.environ.copy()
    env["TWITTER_ARTICLENATOR_CONFIG_DIR"] = str(config_dir)
    env["TWITTER_ARTICLENATOR_OUTPUT_DIR"] = str(output_dir)
    env["TWITTER_ARTICLENATOR_JSON_LOGGING"] = "false"
    env["FLASK_APP"] = "twitter_articlenator.app:create_app"
    env["FLASK_RUN_PORT"] = str(port)

    # Start Flask in a subprocess
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import os
os.environ['TWITTER_ARTICLENATOR_CONFIG_DIR'] = '{config_dir}'
os.environ['TWITTER_ARTICLENATOR_OUTPUT_DIR'] = '{output_dir}'
os.environ['TWITTER_ARTICLENATOR_JSON_LOGGING'] = 'false'

from twitter_articlenator.app import create_app
app = create_app()
app.run(host='127.0.0.1', port={port}, debug=False, use_reloader=False)
""",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Wait for server to start
    if not wait_for_server(port):
        proc.terminate()
        stdout, stderr = proc.communicate(timeout=5)
        pytest.fail(f"Server failed to start. stdout: {stdout.decode()}, stderr: {stderr.decode()}")

    yield {"port": port, "process": proc, "config_dir": config_dir, "output_dir": output_dir}

    # Cleanup
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="session")
def base_url(flask_server):
    """Base URL for the running Flask server."""
    return f"http://127.0.0.1:{flask_server['port']}"


@pytest.fixture(scope="session")
def config_dir(flask_server):
    """Config directory for the test session."""
    return flask_server["config_dir"]


@pytest.fixture(scope="session")
def output_dir(flask_server):
    """Output directory for the test session."""
    return flask_server["output_dir"]
