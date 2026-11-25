"""
Logging configuration and utilities

Correlation context
-------------------
``bind_log_context`` stores a small set of correlation identifiers in a
``contextvars.ContextVar``. ``LogContextFilter`` (installed on the console
handler by ``setup_logging``) copies them onto every ``LogRecord`` emitted while
the context is active, so the JSON formatter outputs them as top-level fields.

* HTTP: ``apps.api.middleware.request_context.RequestContextMiddleware`` binds
  ``request_id`` for the lifetime of each request.
* Background work (e.g. the campaign worker) can bind its own identifiers::

      from utils.logger import bind_log_context

      with bind_log_context(execution_id=execution.id, campaign_id=campaign.id):
          process(execution)

  Nested bindings merge with (and temporarily override) the outer context.

Only identifiers listed in ``LOG_CONTEXT_FIELDS`` are accepted, which keeps
free-form values (emails, names, CV text, transcripts, tokens) out of the
context. Values are stringified and truncated to 128 characters. Explicit
``extra=`` fields on a log call win over context values of the same name.
"""

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from types import MappingProxyType
from typing import Any

import structlog
from pythonjsonlogger import jsonlogger

from config import settings

LOG_CONTEXT_FIELDS = frozenset(
    {
        "request_id",
        "execution_id",
        "communication_id",
        "campaign_id",
        "application_id",
        "interview_id",
        "organization_id",
    }
)
_LOG_CONTEXT_MAX_VALUE_LENGTH = 128
_EMPTY_CONTEXT: Mapping[str, str] = MappingProxyType({})
_log_context: ContextVar[Mapping[str, str]] = ContextVar("ai_log_context", default=_EMPTY_CONTEXT)


def get_log_context() -> dict[str, str]:
    """Return a copy of the correlation identifiers bound to the current context."""
    return dict(_log_context.get())


def get_request_id() -> str | None:
    """Return the request id of the HTTP request being handled, if any."""
    return _log_context.get().get("request_id")


@contextmanager
def bind_log_context(**fields: Any) -> Iterator[None]:
    """Bind correlation identifiers to all log records emitted inside the block.

    ``None`` values are ignored. Unknown field names raise ``ValueError`` so that
    PII cannot be attached by accident.
    """
    unknown = set(fields) - LOG_CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"Unsupported log context fields: {sorted(unknown)}")
    merged = dict(_log_context.get())
    for key, value in fields.items():
        if value is not None:
            merged[key] = str(value)[:_LOG_CONTEXT_MAX_VALUE_LENGTH]
    token = _log_context.set(MappingProxyType(merged))
    try:
        yield
    finally:
        _log_context.reset(token)


class LogContextFilter(logging.Filter):
    """Attach the bound correlation identifiers to each record (never filters out)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _log_context.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class CustomJsonFormatter(jsonlogger.JsonFormatter):
    """Custom JSON formatter for structured logging"""

    def add_fields(self, log_record: dict[str, Any], record: logging.LogRecord, message_dict: dict[str, Any]) -> None:
        """Add custom fields to log record"""
        super().add_fields(log_record, record, message_dict)
        
        log_record["timestamp"] = datetime.utcnow().isoformat()
        log_record["level"] = record.levelname
        log_record["logger"] = record.name
        log_record["environment"] = settings.ENVIRONMENT
        
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)


def setup_logging() -> None:
    """Configure logging for the application"""
    
    # Configure structlog
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.JSONRenderer(),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(settings.LOG_LEVEL.upper())
    
    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(settings.LOG_LEVEL.upper())
    
    if settings.LOG_LEVEL == "debug":
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
    else:
        formatter = CustomJsonFormatter()
    
    console_handler.setFormatter(formatter)
    console_handler.addFilter(LogContextFilter())
    root_logger.addHandler(console_handler)


def get_logger(name: str) -> logging.Logger:
    """Get logger instance"""
    return logging.getLogger(name)
