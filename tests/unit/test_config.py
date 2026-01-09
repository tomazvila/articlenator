"""Tests for config.py - configuration management."""

from pathlib import Path



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


class TestCookieParsing:
    """Tests for cookie input parsing (DevTools format support)."""

    def test_parse_standard_format_unchanged(self):
        """Test standard cookie format passes through unchanged."""
        from twitter_articlenator.config import Config

        config = Config()
        standard = "auth_token=abc123; ct0=xyz789"
        result = config._parse_cookie_input(standard)
        assert result == standard

    def test_parse_devtools_format_with_tabs(self):
        """Test parsing DevTools format with tab separators."""
        from twitter_articlenator.config import Config

        config = Config()
        devtools_input = "ct0\tvalue123\t.x.com\t/\t2026-01-01\t100 B\nauth_token\ttoken456\t.x.com\t/\t2026-01-01\t50 B"
        result = config._parse_cookie_input(devtools_input)

        assert "ct0=value123" in result
        assert "auth_token=token456" in result

    def test_parse_devtools_format_with_spaces(self):
        """Test parsing DevTools format with multiple space separators."""
        from twitter_articlenator.config import Config

        config = Config()
        # Using 4 spaces as separator (common in DevTools copy)
        devtools_input = "ct0    value123    .x.com    /    2026-01-01    100 B\nauth_token    token456    .x.com    /    2026-01-01    50 B"
        result = config._parse_cookie_input(devtools_input)

        assert "ct0=value123" in result
        assert "auth_token=token456" in result

    def test_parse_devtools_real_world_format(self):
        """Test parsing real DevTools copy-paste format with checkmarks."""
        from twitter_articlenator.config import Config

        config = Config()
        # Real format from Chrome DevTools with checkmarks and extra columns
        devtools_input = """ct0    4659f60d187797c7388366c349d729a0261421c7467e57203b6b82816d828f20be6caf9e171d42e046b017518f31b08afd12e59e177f2c8efbb09ef1dae86d4ff2b4ac835beff6f8bb04f69599528100    .x.com    /    19/06/2026, 21:42:14    163 B    ✓
auth_token    d1badbeaafb428e17244c00a3fed7d16340a9119    .x.com    /    19/06/2026, 21:42:14    50 B    ✓    ✓"""

        result = config._parse_cookie_input(devtools_input)

        assert "ct0=4659f60d187797c7388366c349d729a0261421c7467e57203b6b82816d828f20be6caf9e171d42e046b017518f31b08afd12e59e177f2c8efbb09ef1dae86d4ff2b4ac835beff6f8bb04f69599528100" in result
        assert "auth_token=d1badbeaafb428e17244c00a3fed7d16340a9119" in result

    def test_parse_devtools_ignores_irrelevant_cookies(self):
        """Test that parsing ignores non-Twitter cookies."""
        from twitter_articlenator.config import Config

        config = Config()
        devtools_input = "ct0\tvalue123\t.x.com\nsome_other_cookie\tignored\t.x.com\nauth_token\ttoken456\t.x.com"
        result = config._parse_cookie_input(devtools_input)

        assert "ct0=value123" in result
        assert "auth_token=token456" in result
        assert "some_other_cookie" not in result
        assert "ignored" not in result

    def test_parse_devtools_empty_returns_empty(self):
        """Test that empty DevTools format returns empty string."""
        from twitter_articlenator.config import Config

        config = Config()
        devtools_input = "random_cookie\tvalue\t.other.com"
        result = config._parse_cookie_input(devtools_input)

        # No Twitter cookies found, should return empty or original
        assert "auth_token" not in result
        assert "ct0" not in result

    def test_save_and_load_devtools_format(self, tmp_path, monkeypatch):
        """Test full cycle: save DevTools format, load normalized."""
        from twitter_articlenator.config import Config

        config_dir = tmp_path / "config"
        monkeypatch.setenv("TWITTER_ARTICLENATOR_CONFIG_DIR", str(config_dir))

        config = Config()

        # Save DevTools format
        devtools_input = "ct0    myct0value    .x.com    /\nauth_token    myauthtoken    .x.com    /"
        config.save_cookies(devtools_input)

        # Load should return normalized format
        result = config.load_cookies()
        assert "ct0=myct0value" in result
        assert "auth_token=myauthtoken" in result
        assert "\t" not in result  # No tabs in normalized output
