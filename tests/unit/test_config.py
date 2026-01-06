"""Tests for config.py - configuration management."""

import json
import os
from pathlib import Path

import pytest


class TestConfigProperties:
    """Tests for Config class properties."""

    def test_config_has_cookie_path(self):
        """Test Config has cookie_path property."""
        from twitter_articlenator.config import Config

        config = Config()
        assert hasattr(config, "cookie_path")
        assert isinstance(config.cookie_path, Path)

    def test_config_has_output_dir(self):
        """Test Config has output_dir property."""
        from twitter_articlenator.config import Config

        config = Config()
        assert hasattr(config, "output_dir")
        assert isinstance(config.output_dir, Path)

    def test_config_has_log_level(self):
        """Test Config has log_level property."""
        from twitter_articlenator.config import Config

        config = Config()
        assert hasattr(config, "log_level")
        assert isinstance(config.log_level, str)

    def test_config_has_json_logging(self):
        """Test Config has json_logging property."""
        from twitter_articlenator.config import Config

        config = Config()
        assert hasattr(config, "json_logging")
        assert isinstance(config.json_logging, bool)


class TestConfigDefaults:
    """Tests for Config default values."""

    def test_config_default_cookie_path(self):
        """Test default cookie path is in ~/.config."""
        from twitter_articlenator.config import Config

        config = Config()
        assert ".config" in str(config.cookie_path) or "twitter-articlenator" in str(
            config.cookie_path
        )
        assert config.cookie_path.name == "cookies.json"

    def test_config_default_output_dir(self):
        """Test default output dir is in ~/Downloads."""
        from twitter_articlenator.config import Config

        config = Config()
        assert "Downloads" in str(config.output_dir) or "twitter-articles" in str(
            config.output_dir
        )

    def test_config_default_log_level(self):
        """Test default log level is INFO."""
        from twitter_articlenator.config import Config

        config = Config()
        assert config.log_level == "INFO"


class TestConfigEnvOverrides:
    """Tests for environment variable overrides."""

    def test_config_env_override_output_dir(self, monkeypatch, tmp_path):
        """Test output_dir can be overridden by env var."""
        from twitter_articlenator.config import Config

        custom_dir = str(tmp_path / "custom_output")
        monkeypatch.setenv("TWITTER_ARTICLENATOR_OUTPUT_DIR", custom_dir)

        config = Config()
        assert str(config.output_dir) == custom_dir

    def test_config_env_override_log_level(self, monkeypatch):
        """Test log_level can be overridden by env var."""
        from twitter_articlenator.config import Config

        monkeypatch.setenv("TWITTER_ARTICLENATOR_LOG_LEVEL", "DEBUG")

        config = Config()
        assert config.log_level == "DEBUG"

    def test_config_env_override_json_logging(self, monkeypatch):
        """Test json_logging can be overridden by env var."""
        from twitter_articlenator.config import Config

        monkeypatch.setenv("TWITTER_ARTICLENATOR_JSON_LOGGING", "false")

        config = Config()
        assert config.json_logging is False


class TestCookieManagement:
    """Tests for cookie loading/saving."""

    def test_load_cookies_returns_none_if_missing(self, tmp_path, monkeypatch):
        """Test load_cookies returns None if file doesn't exist."""
        from twitter_articlenator.config import Config

        # Point to non-existent file
        monkeypatch.setenv(
            "TWITTER_ARTICLENATOR_CONFIG_DIR", str(tmp_path / "nonexistent")
        )

        config = Config()
        result = config.load_cookies()
        assert result is None

    def test_save_cookies_creates_file(self, tmp_path, monkeypatch):
        """Test save_cookies creates the cookies file."""
        from twitter_articlenator.config import Config

        config_dir = tmp_path / "config"
        monkeypatch.setenv("TWITTER_ARTICLENATOR_CONFIG_DIR", str(config_dir))

        config = Config()
        config.save_cookies("auth_token=abc123; ct0=xyz789")

        assert config.cookie_path.exists()
        content = config.cookie_path.read_text()
        assert "abc123" in content

    def test_load_cookies_returns_saved_value(self, tmp_path, monkeypatch):
        """Test load_cookies returns previously saved cookies."""
        from twitter_articlenator.config import Config

        config_dir = tmp_path / "config"
        monkeypatch.setenv("TWITTER_ARTICLENATOR_CONFIG_DIR", str(config_dir))

        config = Config()
        cookies = "auth_token=test123; ct0=testxyz"
        config.save_cookies(cookies)

        result = config.load_cookies()
        assert result == cookies


class TestGetConfig:
    """Tests for get_config singleton."""

    def test_get_config_returns_config(self):
        """Test get_config returns a Config instance."""
        from twitter_articlenator.config import get_config

        config = get_config()
        assert config is not None

    def test_get_config_returns_singleton(self):
        """Test get_config returns the same instance."""
        from twitter_articlenator.config import get_config

        config1 = get_config()
        config2 = get_config()
        assert config1 is config2
