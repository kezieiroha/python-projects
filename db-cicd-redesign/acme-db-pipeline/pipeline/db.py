"""
Database connection factory for the acme DB pipeline.

All pipeline code that needs a database connection must use the readonly_connection
context manager exported here. Direct psycopg2.connect() calls must not appear
elsewhere in the codebase.

All Python-level DB queries in the pipeline are read-only SELECT statements.
Deployment mutations (prep.sql, deploy.lst, post.sql) are executed by
ci_backend_db_release.py on EC2 via psql, not by Python stages directly.

The context manager closes the underlying connection on exit regardless of
whether an exception occurred. Callers are responsible for rolling back
transactions; no implicit commits are issued.
"""

from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.extensions

from pipeline.config import Config
from pipeline.logger import get_logger

# Module logger
#
# Connection creation and teardown are logged at DEBUG so normal CI output stays
# focused on stage decisions while still allowing connection diagnostics when
# --verbose is passed to a stage script.
log = get_logger(__name__)


# Read-only connection factory
#
# Used by s3 for catalog analysis and by s6/s7 for post-deploy verification
# queries (duplicate function check, audit table alignment). All Python-level
# DB access is read-only SELECT; callers must roll back any implicit transaction
# state before exiting the context.
@contextmanager
def readonly_connection(
    cfg: Config,
) -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a psycopg2 connection for the read-only database role.

    The connection is closed on exit. Transactions are not implicitly committed
    or rolled back — callers must manage their own transaction lifecycle.

    Usage:
        with readonly_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
                rows = cur.fetchall()
            conn.rollback()  # discard any implicit transaction state
    """
    log.debug(
        "Opening read-only connection: host=%s db=%s user=%s",
        cfg.db_host,
        cfg.db_name,
        cfg.db_user_readonly,
    )
    conn = psycopg2.connect(cfg.db_dsn_readonly())
    try:
        yield conn
    finally:
        conn.close()
        log.debug("Closed read-only connection: host=%s db=%s", cfg.db_host, cfg.db_name)
