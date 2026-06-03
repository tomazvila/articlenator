"""Configuration management."""

import os
import re
from pathlib import Path

# Global singleton instance
_config_instance: "Config | None" = None


class Config:
    """Application configuration."""

    def __init__(self) -> None:
        """Initialize configuration with defaults and env overrides."""
        # Output directory for PDFs
        self._output_dir = Path(
            os.environ.get(
                "TWITTER_ARTICLENATOR_OUTPUT_DIR",
                Path.home() / "Downloads" / "twitter-articles",
            )
        )

        # Log level
        self._log_level = os.environ.get("TWITTER_ARTICLENATOR_LOG_LEVEL", "INFO")

        # JSON logging
        json_logging_env = os.environ.get("TWITTER_ARTICLENATOR_JSON_LOGGING", "true")
        self._json_logging = json_logging_env.lower() in ("true", "1", "yes")

        # YouTube downloader settings
        self._youtube_downloader_bin = os.environ.get(
            "TWITTER_ARTICLENATOR_YOUTUBE_DOWNLOADER", "yt-dlp"
        )
        self._youtube_download_timeout = int(
            os.environ.get("TWITTER_ARTICLENATOR_YOUTUBE_TIMEOUT", "14400")
        )
        self._youtube_cookie_verify_timeout = int(
            os.environ.get("TWITTER_ARTICLENATOR_YOUTUBE_COOKIE_VERIFY_TIMEOUT", "60")
        )
        self._youtube_cookie_verify_url = os.environ.get(
            "TWITTER_ARTICLENATOR_YOUTUBE_COOKIE_VERIFY_URL",
            "https://www.youtube.com/watch?v=fv7TlVMETP0",
        )
        self._youtube_cookie_max_bytes = int(
            os.environ.get("TWITTER_ARTICLENATOR_YOUTUBE_COOKIE_MAX_BYTES", "262144")
        )
        self._youtube_cookie_encryption_key = os.environ.get(
            "TWITTER_ARTICLENATOR_COOKIE_ENCRYPTION_KEY"
        )
        require_cookie_encryption_env = os.environ.get(
            "TWITTER_ARTICLENATOR_REQUIRE_COOKIE_ENCRYPTION", "false"
        )
        self._require_youtube_cookie_encryption = require_cookie_encryption_env.lower() in (
            "true",
            "1",
            "yes",
        )

        config_dir_default = self._output_dir.parent / "config"
        self._config_dir = Path(
            os.environ.get("TWITTER_ARTICLENATOR_CONFIG_DIR", config_dir_default)
        )
        self._youtube_cookie_path = Path(
            os.environ.get(
                "TWITTER_ARTICLENATOR_YOUTUBE_COOKIE_PATH",
                self._config_dir / "youtube-cookies.txt",
            )
        )
        self._youtube_oauth_client_id = os.environ.get(
            "TWITTER_ARTICLENATOR_YOUTUBE_OAUTH_CLIENT_ID"
        )
        self._youtube_oauth_client_secret = os.environ.get(
            "TWITTER_ARTICLENATOR_YOUTUBE_OAUTH_CLIENT_SECRET"
        )
        self._youtube_oauth_redirect_uri = os.environ.get(
            "TWITTER_ARTICLENATOR_YOUTUBE_OAUTH_REDIRECT_URI"
        )
        self._youtube_oauth_token_path = Path(
            os.environ.get(
                "TWITTER_ARTICLENATOR_YOUTUBE_OAUTH_TOKEN_PATH",
                self._config_dir / "youtube-oauth-token.json",
            )
        )
        self._youtube_liked_max_results = int(
            os.environ.get("TWITTER_ARTICLENATOR_YOUTUBE_LIKED_MAX_RESULTS", "5000")
        )

    @property
    def output_dir(self) -> Path:
        """Directory for generated PDFs."""
        return self._output_dir

    @property
    def log_level(self) -> str:
        """Logging level."""
        return self._log_level

    @property
    def json_logging(self) -> bool:
        """Whether to use JSON logging format."""
        return self._json_logging

    @property
    def youtube_downloader_bin(self) -> str:
        """Executable used for YouTube downloads."""
        return self._youtube_downloader_bin

    @property
    def youtube_download_timeout(self) -> int:
        """Maximum seconds allowed for one YouTube download."""
        return self._youtube_download_timeout

    @property
    def youtube_cookie_verify_timeout(self) -> int:
        """Maximum seconds allowed for one YouTube cookie verification."""
        return self._youtube_cookie_verify_timeout

    @property
    def youtube_cookie_verify_url(self) -> str:
        """YouTube URL used to verify stored cookies."""
        return self._youtube_cookie_verify_url

    @property
    def youtube_cookie_max_bytes(self) -> int:
        """Maximum accepted YouTube cookies.txt upload size."""
        return self._youtube_cookie_max_bytes

    @property
    def youtube_cookie_encryption_key(self) -> str | None:
        """Fernet key for encrypting persistent YouTube cookies."""
        return self._youtube_cookie_encryption_key

    @property
    def require_youtube_cookie_encryption(self) -> bool:
        """Whether YouTube cookie storage must be encrypted."""
        return self._require_youtube_cookie_encryption

    @property
    def config_dir(self) -> Path:
        """Directory for persistent app configuration."""
        return self._config_dir

    @property
    def youtube_cookie_path(self) -> Path:
        """Server-side YouTube cookie storage path."""
        return self._youtube_cookie_path

    @property
    def youtube_oauth_client_id(self) -> str | None:
        """Google OAuth client ID for YouTube Data API access."""
        return self._youtube_oauth_client_id

    @property
    def youtube_oauth_client_secret(self) -> str | None:
        """Google OAuth client secret for YouTube Data API access."""
        return self._youtube_oauth_client_secret

    @property
    def youtube_oauth_redirect_uri(self) -> str | None:
        """Configured OAuth redirect URI, if explicitly provided."""
        return self._youtube_oauth_redirect_uri

    @property
    def youtube_oauth_token_path(self) -> Path:
        """Server-side encrypted YouTube OAuth token path."""
        return self._youtube_oauth_token_path

    @property
    def youtube_liked_max_results(self) -> int:
        """Maximum liked YouTube videos to fetch through OAuth."""
        return self._youtube_liked_max_results


def get_config() -> Config:
    """Get the global configuration instance.

    Returns:
        The singleton Config instance.
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance


def parse_cookie_input(raw_input: str) -> str:
    """Parse cookie input from various formats.

    Supports:
    - Standard format: auth_token=xxx; ct0=yyy
    - DevTools table copy-paste (tab or space-separated):
      ct0    value    .x.com    /    date    size    ...
      auth_token    value    .x.com    /    date    size    ...

    Args:
        raw_input: Raw cookie input string.

    Returns:
        Normalized cookie string in format: name=value; name2=value2
    """
    raw_input = raw_input.strip()

    # Check if it looks like DevTools format (contains tabs or multiple spaces)
    has_tabs = "\t" in raw_input
    has_multi_spaces = "    " in raw_input  # 4+ spaces
    has_multiple_lines = "\n" in raw_input
    has_no_equals = "=" not in raw_input.split("\n")[0]  # First line has no =

    # Check for known cookie names at start of lines (DevTools format indicator)
    lines = raw_input.split("\n")
    starts_with_cookie_name = any(
        line.strip().startswith(("ct0", "auth_token", "twid", "guest_id")) for line in lines
    )

    if starts_with_cookie_name and (has_tabs or has_multi_spaces):
        return _parse_devtools_cookies(raw_input)

    if (has_tabs or has_multi_spaces) and has_multiple_lines and has_no_equals:
        return _parse_devtools_cookies(raw_input)

    # Already in standard format
    return raw_input


def _parse_devtools_cookies(raw_input: str) -> str:
    """Parse cookies from Chrome DevTools copy-paste format.

    Format: name<tab or spaces>value<tab or spaces>domain<tab or spaces>...

    Args:
        raw_input: Tab or space-separated cookie data.

    Returns:
        Cookie string in format: name=value; name2=value2
    """
    cookies = {}
    lines = raw_input.strip().split("\n")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Split by tab or multiple spaces (2+)
        parts = re.split(r"\t|  +", line)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) >= 2:
            name = parts[0]
            value = parts[1]

            # Only include relevant Twitter cookies
            if name in ("auth_token", "ct0", "twid", "guest_id"):
                cookies[name] = value

    # Build cookie string
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def validate_cookies(cookies: str) -> dict:
    """Validate cookie string format and required cookies.

    Args:
        cookies: Cookie string in format name=value; name2=value2

    Returns:
        Dict with 'valid' bool, 'status' str, 'message' str, and 'missing' list.
    """
    if not cookies or not cookies.strip():
        return {
            "valid": False,
            "status": "not_configured",
            "message": "No cookies provided",
            "missing": ["auth_token", "ct0"],
        }

    cookie_dict = {}
    for part in cookies.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookie_dict[name.strip()] = value.strip()

    has_auth_token = "auth_token" in cookie_dict and len(cookie_dict["auth_token"]) > 20
    has_ct0 = "ct0" in cookie_dict and len(cookie_dict["ct0"]) > 20

    if has_auth_token and has_ct0:
        return {
            "valid": True,
            "status": "valid",
            "message": "Cookies validated (auth_token and ct0 present).",
            "missing": [],
        }

    missing = []
    if not has_auth_token:
        missing.append("auth_token (missing or too short)")
    if not has_ct0:
        missing.append("ct0 (missing or too short)")

    return {
        "valid": False,
        "status": "invalid",
        "message": f"Invalid cookies: {', '.join(missing)}",
        "missing": missing,
    }
