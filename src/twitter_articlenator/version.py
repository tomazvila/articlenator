"""Version information for the application."""

import os
import subprocess
from functools import lru_cache

# Application version
__version__ = "0.1.0"


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
        Version string like '0.1.0 (abc12345)'.
    """
    commit = get_git_commit()
    return f"{__version__} ({commit})"
