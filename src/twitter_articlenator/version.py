"""Version information for the application.

Version is read from pyproject.toml (single source of truth).
"""

import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path


def _get_version() -> str:
    """Read version from installed metadata, falling back to pyproject.toml."""
    try:
        from importlib.metadata import version

        return version("twitter-articlenator")
    except Exception:
        pass

    # Fallback: read pyproject.toml directly (dev shell / uninstalled)
    try:
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        match = re.search(r'version\s*=\s*"([^"]+)"', pyproject.read_text())
        if match:
            return match.group(1)
    except Exception:
        pass

    return "0.0.0"


__version__ = _get_version()


@lru_cache(maxsize=1)
def get_git_commit() -> str:
    """Get the git commit hash.

    Returns:
        8-character git commit hash, or 'unknown' if not available.
    """
    # First check environment variable (set during Docker build)
    commit = os.environ.get("GIT_COMMIT", "").strip()
    if commit:
        return commit[:8]

    # Try to get from git (works in development)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        )
        if result.returncode == 0:
            return result.stdout.strip()[:8]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass

    return "unknown"


def get_version_string() -> str:
    """Get full version string including git commit.

    Returns:
        Version string like '0.2.3 (abc12345)'.
    """
    commit = get_git_commit()
    return f"{__version__} ({commit})"
