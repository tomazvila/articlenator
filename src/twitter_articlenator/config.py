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
            cookies: Cookie string to save.
        """
        # Ensure directory exists
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)

        data = {"cookies": cookies}
        self.cookie_path.write_text(json.dumps(data, indent=2))


def get_config() -> Config:
    """Get the global configuration instance.

    Returns:
        The singleton Config instance.
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = Config()
    return _config_instance
