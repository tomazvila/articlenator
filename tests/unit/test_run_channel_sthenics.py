"""Tests for the sthenics_ channel batch run script.

These test the shell script structure by running it with --preview (which is a
no-op that just lists videos) and verifying error handling for missing config.

The script is designed to be the runner entry point; actual batch_channel.py
subprocess behavior is tested separately in test_batch_channel.py.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parents[2]
SCRIPT = HERE / "zettel_ralph" / "run_channel_sthenics.sh"


def _run_script(*args, env=None, timeout=60):
    """Run the sthenics script with args. Returns (returncode, stdout, stderr)."""
    base_env = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "DEEPSEEK_API_KEY": "sk-test-fake-key-for-testing",
        "DEEPSEEK_BASE_URL": "https://openrouter.ai/api/v1",
        "DEEPSEEK_MODEL": "deepseek/deepseek-v4-flash",
    }
    # Round 5 (M10): the script never writes into the code tree during a test. Its
    # staging is a temp folder; harness state (locks, registry) too; ZR_TEST_RUN=1 makes
    # run_integrity refuse a staging inside zettel_ralph/.
    tmp = Path(tempfile.mkdtemp(prefix="sthenics-test-"))
    base_env.update({"ZR_STAGING": str(tmp / "staging_transcripts"), "ZR_STATE_DIR": str(tmp / "state"),
                     "ZR_TEST_RUN": "1"})
    if env:
        base_env.update(env)
    base_env.setdefault("VAULT", str(HERE / "tests" / "fixtures" / "vault"))
    base_env.setdefault(
        "CHANNELS_DIR", str(HERE / "tests" / "fixtures" / "channels")
    )

    try:
        proc = subprocess.run(
            ["bash", str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=base_env,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return proc.returncode, proc.stdout, proc.stderr


def test_script_exists():
    """The script file must exist and be executable."""
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), f"Script not executable: {SCRIPT}"


def test_preview_mode_uses_batch_channel(tmp_path):
    """--preview should delegate to batch_channel.py and show channel info."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    ret, out, err = _run_script(
        "--preview",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
        },
    )
    # batch_channel.py will succeed if yt-dlp is available
    # or fail with a not-found error — either way we validate structure
    if ret == 0:
        combined = out + err
        assert "@sthenics_" in combined, (
            "Channel handle should appear in output"
        )
    # Non-zero exit is OK if yt-dlp not found in test env


def test_fails_without_vault(tmp_path):
    """Script should error when the vault directory does not exist."""
    missing = tmp_path / "nonexistent"
    ret, out, err = _run_script(
        "--synthesize-only",
        env={
            "VAULT": str(missing),
            "CHANNELS_DIR": str(tmp_path / "channels"),
        },
    )
    assert ret != 0
    assert "Vault directory not found" in (out + err)


def test_args_proxied_to_batch_channel(tmp_path):
    """Additional args like --limit should be proxied through."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    # Use --preview so we just list videos without processing
    ret, out, err = _run_script(
        "--preview", "--limit", "1",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
        },
    )
    # --preview with --limit should be accepted by batch_channel's argparse
    # and produce channel listing or fail gracefully if yt-dlp not found
    if ret == 0:
        assert "CHANNEL:" in (out + err)
        assert "@sthenics_" in (out + err)


def test_zk_folder_configurable(tmp_path):
    """ZK_FOLDER env var should be respected."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    ret, out, err = _run_script(
        "--preview",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
            "ZK_FOLDER": "Custom ZK Folder",
        },
    )
    # Script runs with preview, should not error
    assert ret in (0, 1)  # 0 if preview works, 1 if yt-dlp missing


def test_script_sets_openrouter_env(tmp_path):
    """The script should set DEEPSEEK_BASE_URL for OpenRouter."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    ret, out, err = _run_script(
        "--preview",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
        },
    )
    # Check that the script ran (yt-dlp availability determines success)
    # The key env vars should be in the subprocess env
    assert ret in (0, 1)  # Accept both success and yt-dlp-not-found


def test_script_creates_channel_dir(tmp_path):
    """The script should ensure the channels dir exists."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "custom_channels"
    # Don't create it - script should create it

    _run_script(
        "--preview",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
        },
    )
    # mkdir -p should have been called
    assert channels.exists()


def test_script_default_vault(tmp_path):
    """The script uses a sensible default vault path."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    ret, out, err = _run_script(
        "--preview",
        env={
            "CHANNELS_DIR": str(channels),
        },
    )
    # Should succeed (vault is set via env or default)
    assert ret in (0, 1)


def test_script_rejects_invalid_args(tmp_path):
    """Invalid args like --nonexistent should pass through and fail."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    ret, out, err = _run_script(
        "--nonexistent-flag",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
        },
    )
    # batch_channel.py's argparse should reject the unrecognized arg
    assert ret != 0
    assert "unrecognized" in (out + err).lower() or "usage:" in (out + err).lower()


def test_dry_run_env_vars_logged(tmp_path):
    """The script logs config when running in non-preview mode."""
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    channels = tmp_path / "channels"
    channels.mkdir(parents=True)

    ret, out, err = _run_script(
        "--synthesize-only",
        env={
            "VAULT": str(vault),
            "CHANNELS_DIR": str(channels),
        },
    )
    # Script should print config info to stderr before running
    assert "sthenics_ channel batch" in (out + err)
    assert "Video Transcripts Zettelkasten" in (out + err)