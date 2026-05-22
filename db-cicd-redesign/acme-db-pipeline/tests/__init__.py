"""
Test suite for the acme DB pipeline.

Unit tests use unittest.mock to avoid external dependencies. Integration tests
(marked with pytest.mark.skipif) require a live PostgreSQL instance and are
skipped automatically when the DB_* environment variables are absent.
"""
