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


def create_fake_youtube_downloader(path, log_path):
    """Create a deterministic yt-dlp stand-in for YouTube E2E tests."""
    path.write_text(
        """#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
url = args[-1] if args else ""
mode = "mp3" if "--audio-format" in args and "mp3" in args else "video"

cookie_info = None
if "--cookies" in args:
    try:
        cookie_path = Path(args[args.index("--cookies") + 1])
        cookie_bytes = cookie_path.read_bytes()
        cookie_info = {
            "exists": cookie_path.exists(),
            "sha256": hashlib.sha256(cookie_bytes).hexdigest(),
            "length": len(cookie_bytes),
        }
    except Exception as exc:
        cookie_info = {"error": str(exc)}

if "-F" in args:
    record = {
        "args": args,
        "url": url,
        "mode": "verify",
        "cookies": cookie_info,
    }
    log_file = os.environ.get("TWITTER_ARTICLENATOR_YOUTUBE_FAKE_LOG")
    if log_file:
        with open(log_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\\n")
    print("ID EXT RESOLUTION")
    print("18 mp4 640x360")
    sys.exit(0)

try:
    output_template = args[args.index("-o") + 1]
except (ValueError, IndexError):
    print("missing output template", file=sys.stderr)
    sys.exit(2)

record = {
    "args": args,
    "url": url,
    "mode": mode,
    "cookies": cookie_info,
}

log_file = os.environ.get("TWITTER_ARTICLENATOR_YOUTUBE_FAKE_LOG")
if log_file:
    with open(log_file, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\\n")

if "slow" in url:
    time.sleep(2)

if "fail" in url:
    print("requested fake failure", file=sys.stderr)
    sys.exit(3)

extension = "mp3" if mode == "mp3" else "mp4"
fake_id = "fake_" + hashlib.sha256(url.encode()).hexdigest()[:8]
output_path = Path(output_template.replace("%(id)s", fake_id).replace("%(ext)s", extension))
output_path.parent.mkdir(parents=True, exist_ok=True)
output_path.write_bytes((f"fake {mode} download for {url}\\n").encode("utf-8") * 64)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.fixture(scope="session")
def flask_server(tmp_path_factory):
    """Start Flask server for E2E tests."""
    port = find_free_port()

    # Create temp directories for this test session
    tmp_dir = tmp_path_factory.mktemp("e2e")
    output_dir = tmp_dir / "output"
    output_dir.mkdir()
    fake_ytdlp = tmp_dir / "fake-youtube-ytdlp"
    fake_ytdlp_log = tmp_dir / "fake-youtube-ytdlp.jsonl"
    if os.environ.get("RUN_REAL_YOUTUBE_E2E") != "1":
        create_fake_youtube_downloader(fake_ytdlp, fake_ytdlp_log)

    env = os.environ.copy()
    env["TWITTER_ARTICLENATOR_OUTPUT_DIR"] = str(output_dir)
    env["TWITTER_ARTICLENATOR_JSON_LOGGING"] = "false"
    env["FLASK_APP"] = "twitter_articlenator.app:create_app"
    env["FLASK_RUN_PORT"] = str(port)
    env["TWITTER_ARTICLENATOR_YOUTUBE_TIMEOUT"] = "30"
    env["TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY"] = (
        "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="
    )
    env["TWITTER_ARTICLENATOR_REQUIRE_COOKIE_ENCRYPTION"] = "true"
    if os.environ.get("RUN_REAL_YOUTUBE_E2E") != "1":
        env["TWITTER_ARTICLENATOR_YOUTUBE_DOWNLOADER"] = str(fake_ytdlp)
        env["TWITTER_ARTICLENATOR_YOUTUBE_FAKE_LOG"] = str(fake_ytdlp_log)

    # Start Flask in a subprocess
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import os
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

    yield {
        "port": port,
        "process": proc,
        "output_dir": output_dir,
        "youtube_fake_log": fake_ytdlp_log,
    }

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
def output_dir(flask_server):
    """Output directory for the test session."""
    return flask_server["output_dir"]
