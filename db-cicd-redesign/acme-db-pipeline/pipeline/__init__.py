"""
Shared pipeline infrastructure: configuration, database connections, logging, and models.

Import order matters for stages: configure_logging should be called before the first
log record is emitted, and Config should be instantiated before any DB connections are
opened, so that ConfigError surfaces before any work is attempted.
"""
