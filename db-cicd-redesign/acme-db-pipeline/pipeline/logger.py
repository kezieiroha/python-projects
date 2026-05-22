"""
Logging setup for the acme DB pipeline.

All pipeline modules obtain their logger through get_logger(). The root pipeline
logger is configured once via configure_logging(). Subsequent calls to configure_logging()
are no-ops so that importing multiple modules does not duplicate handlers.

Log level is INFO by default. Pass verbose=True (or --verbose on CLI) to enable DEBUG.
All output goes to stdout so CI captures it in the job log without redirection.
"""

import logging
import sys

# Logger namespace and idempotency guard
#
# Every module logs under this namespace so CI job output can be filtered by
# one prefix. The configured flag prevents duplicate handlers when tests import
# multiple stage modules in the same process.
_PIPELINE_LOGGER_NAME = "acme_pipeline"
_configured = False


# Logging configuration
#
# Stage CLI entry points call this once after parsing --verbose. Library modules
# only call get_logger(), which avoids configuring global logging as a side
# effect of import.
def configure_logging(verbose: bool = False) -> None:
    """Configure the root pipeline logger.

    Safe to call multiple times — only the first call has any effect.
    Attaches a single StreamHandler writing to stdout with a structured format.

    Args:
        verbose: When True, set the log level to DEBUG. Default is INFO.
    """
    global _configured
    if _configured:
        return

    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger(_PIPELINE_LOGGER_NAME)
    logger.setLevel(level)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Prevent log records from propagating to the root logger, which avoids
    # duplicate output when the pipeline is imported inside a test runner that
    # has already configured its own root handler.
    logger.propagate = False

    _configured = True


# Logger access
#
# Returning child loggers keeps call sites simple while preserving the stage or
# helper module name in each log record.
def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the pipeline namespace.

    Callers should pass __name__ so log records are attributed to the originating
    module. configure_logging() must have been called before the first log record
    is emitted, otherwise output uses Python's default logging configuration.

    Args:
        name: Module name, typically __name__.

    Returns:
        A Logger instance parented to the pipeline root logger.
    """
    return logging.getLogger(f"{_PIPELINE_LOGGER_NAME}.{name}")
