"""Structured logging configuration for Logstash/Grafana."""

import logging
import sys

import orjson
import structlog


def configure_logging(json_output: bool = True) -> None:
    """Configure structlog for Logstash/Grafana integration.

    Args:
        json_output: If True, output JSON logs. If False, use console renderer.
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.CallsiteParameterAdder(
            {
                structlog.processors.CallsiteParameter.FILENAME,
                structlog.processors.CallsiteParameter.FUNC_NAME,
                structlog.processors.CallsiteParameter.LINENO,
            }
        ),
    ]

    if json_output or not sys.stderr.isatty():
        # JSON for production / Logstash
        processors = shared_processors + [
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(serializer=orjson.dumps),
        ]
        logger_factory: structlog.types.WrappedLogger = structlog.BytesLoggerFactory()
    else:
        # Pretty console for development
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(),
        ]
        logger_factory = structlog.PrintLoggerFactory()

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=logger_factory,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger instance.

    Args:
        name: Optional logger name.

    Returns:
        A structlog bound logger.
    """
    return structlog.get_logger(name)
