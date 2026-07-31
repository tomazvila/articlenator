"""Per-user filesystem namespaces."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from flask import current_app

from .auth import current_user
from .config import get_config


@dataclass(frozen=True, slots=True)
class UserPaths:
    """All persistent paths owned by one immutable user id."""

    user_id: str
    output_dir: Path
    config_dir: Path

    @property
    def sessions_dir(self) -> Path:
        return self.output_dir / "sessions"

    @property
    def videos_dir(self) -> Path:
        return self.output_dir / "videos"

    @property
    def youtube_dir(self) -> Path:
        return self.output_dir / "youtube"

    @property
    def transcription_dir(self) -> Path:
        return self.output_dir / "transcriptions"

    @property
    def channel_dir(self) -> Path:
        return self.output_dir / "channels"

    @property
    def twitter_cookie_path(self) -> Path:
        return self.config_dir / "twitter-cookies.enc"

    @property
    def youtube_cookie_path(self) -> Path:
        return self.config_dir / "youtube-cookies.enc"

    @property
    def youtube_oauth_token_path(self) -> Path:
        return self.config_dir / "youtube-oauth-token.enc"


def paths_for_user(user_id: str, output_root: Path | str, config_root: Path | str) -> UserPaths:
    """Build safe filesystem roots for a database-issued UUID."""
    canonical_id = str(uuid.UUID(user_id))
    return UserPaths(
        user_id=canonical_id,
        output_dir=Path(output_root) / "users" / canonical_id,
        config_dir=Path(config_root) / "users" / canonical_id,
    )


def current_user_paths() -> UserPaths:
    """Return scoped paths, with legacy roots only in explicit auth-disabled mode."""
    user = current_user()
    output_root = Path(current_app.config.get("OUTPUT_ROOT", get_config().output_dir))
    config_root = Path(current_app.config.get("CONFIG_ROOT", get_config().config_dir))
    if user is not None:
        paths = paths_for_user(user.id, output_root, config_root)
        _ensure_private_roots(paths)
        return paths
    if current_app.config.get("AUTH_REQUIRED", True):
        raise RuntimeError("Authenticated user context is required")
    return UserPaths(user_id="legacy-test-mode", output_dir=output_root, config_dir=config_root)


def _ensure_private_roots(paths: UserPaths) -> None:
    """Create account roots without granting access to other host users."""
    for path in (paths.output_dir, paths.config_dir):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.chmod(0o700)
        except OSError:
            pass
