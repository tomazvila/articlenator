"""Configuration management."""

import json
import os
from pathlib import Path

# Global singleton instance
_config_instance: "Config | None" = None


class Config:
    """Application configuration."""

    def __init__(self) -> None:
        """Initialize configuration with defaults and env overrides."""
        # Base config directory (can be overridden)
        self._config_dir = Path(
            os.environ.get(
                "TWITTER_ARTICLENATOR_CONFIG_DIR",
                Path.home() / ".config" / "twitter-articlenator",
            )
        )

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

    @property
    def cookie_path(self) -> Path:
        """Path to Twitter cookies file."""
        return self._config_dir / "cookies.json"

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

    def load_cookies(self) -> str | None:
        """Load Twitter cookies from file.

        Returns:
            Cookie string if file exists, None otherwise.
        """
        if not self.cookie_path.exists():
            return None

        try:
            data = json.loads(self.cookie_path.read_text())
            return data.get("cookies")
        except (json.JSONDecodeError, KeyError):
            return None

    def save_cookies(self, cookies: str) -> None:
        """Save Twitter cookies to file.

        Args:
            cookies: Cookie string to save (supports multiple formats).
        """
        # Parse and normalize the cookies
        normalized = self._parse_cookie_input(cookies)

        # Ensure directory exists
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)

        data = {"cookies": normalized}
        self.cookie_path.write_text(json.dumps(data, indent=2))

    def _parse_cookie_input(self, raw_input: str) -> str:
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
            line.strip().startswith(("ct0", "auth_token", "twid", "guest_id"))
            for line in lines
        )

        if starts_with_cookie_name and (has_tabs or has_multi_spaces):
            return self._parse_devtools_cookies(raw_input)

        if (has_tabs or has_multi_spaces) and has_multiple_lines and has_no_equals:
            return self._parse_devtools_cookies(raw_input)

        # Already in standard format
        return raw_input

    def _parse_devtools_cookies(self, raw_input: str) -> str:
        """Parse cookies from Chrome DevTools copy-paste format.

        Format: name<tab or spaces>value<tab or spaces>domain<tab or spaces>...

        Args:
            raw_input: Tab or space-separated cookie data.

        Returns:
            Cookie string in format: name=value; name2=value2
        """
        import re

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


def get_config() -> Config:
    """Get the global configuration instance.

    Returns:
        The singleton Config instance.
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance
