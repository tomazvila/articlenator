"""Tests for logging.py - structlog configuration."""

import structlog


class TestConfigureLogging:
    """Tests for configure_logging function."""

    def setup_method(self):
        """Reset structlog before each test."""
        structlog.reset_defaults()

    def test_configure_logging_json_output(self):
        """Test configure_logging sets up JSON output."""
        from twitter_articlenator.logging import configure_logging

        configure_logging(json_output=True)

        # Verify structlog is configured
        assert structlog.is_configured()

    def test_configure_logging_console_output(self):
        """Test configure_logging sets up console output when json_output=False."""
        from twitter_articlenator.logging import configure_logging

        configure_logging(json_output=False)
        assert structlog.is_configured()

    def test_get_logger_returns_bound_logger(self):
        """Test get_logger returns a structlog bound logger."""
        from twitter_articlenator.logging import configure_logging, get_logger

        configure_logging(json_output=False)
        log = get_logger()

        # Should have the bind method (bound logger)
        assert hasattr(log, "bind")
        assert hasattr(log, "info")
        assert hasattr(log, "error")

    def test_get_logger_with_name(self):
        """Test get_logger with a name parameter."""
        from twitter_articlenator.logging import configure_logging, get_logger

        configure_logging(json_output=False)
        log = get_logger("mylogger")

        assert hasattr(log, "info")


class TestLogOutput:
    """Tests for actual log output format."""

    def setup_method(self):
        """Reset structlog before each test."""
        structlog.reset_defaults()

    def test_json_log_format(self):
        """Test JSON logs have expected structure."""
        from twitter_articlenator.logging import configure_logging

        # Configure for JSON output
        configure_logging(json_output=True)

        # Get the configured processors to verify setup
        config = structlog.get_config()
        processors = config.get("processors", [])

        # Should have JSONRenderer as last processor
        processor_names = [p.__class__.__name__ for p in processors]
        assert "JSONRenderer" in processor_names

    def test_json_log_has_timestamp(self):
        """Test JSON logs include ISO timestamp processor."""
        from twitter_articlenator.logging import configure_logging

        configure_logging(json_output=True)
        config = structlog.get_config()
        processors = config.get("processors", [])

        # Should have TimeStamper processor
        has_timestamper = any(
            isinstance(p, structlog.processors.TimeStamper) for p in processors
        )
        assert has_timestamper

    def test_json_log_has_level(self):
        """Test JSON logs include log level processor."""
        from twitter_articlenator.logging import configure_logging

        configure_logging(json_output=True)
        config = structlog.get_config()
        processors = config.get("processors", [])

        # Should have add_log_level processor
        processor_funcs = [getattr(p, "__name__", str(p)) for p in processors]
        assert any("log_level" in str(p) for p in processor_funcs)

    def test_json_log_has_callsite_info(self):
        """Test JSON logs include filename, func_name, lineno."""
        from twitter_articlenator.logging import configure_logging

        configure_logging(json_output=True)
        config = structlog.get_config()
        processors = config.get("processors", [])

        # Should have CallsiteParameterAdder processor
        has_callsite = any(
            isinstance(p, structlog.processors.CallsiteParameterAdder)
            for p in processors
        )
        assert has_callsite

    def test_log_can_bind_extra_fields(self):
        """Test logger can bind extra fields."""
        from twitter_articlenator.logging import configure_logging, get_logger

        configure_logging(json_output=False)
        log = get_logger()

        # Bind extra fields
        bound_log = log.bind(user_id=123, action="test")

        # Should still be a logger
        assert hasattr(bound_log, "info")
        assert hasattr(bound_log, "bind")
