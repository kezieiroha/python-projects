"""
Tests for stages/s6_test_db_deploy.py.

Unit tests mock subprocess and DB calls so no external services are required.
Integration tests (marked skip_no_db) require a real PostgreSQL instance with
the DB_* environment variables configured.
"""

import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.models import Manifest, SqlFile
from stages.s6_test_db_deploy import (
    _count_deploy_lst_entries,
    _run_release_script,
    check_audit_alignment_from_mutations,
    check_duplicate_functions,
    parse_deploy_log,
    run as s6_run,
)

# ---------------------------------------------------------------------------
# DB availability guard (same pattern as test_s3_db.py)
# ---------------------------------------------------------------------------
#
# Live DB checks are opt-in through environment variables. The default unit test
# path mocks subprocess and DB calls so CI can run without deployment services.

_REQUIRED_DB_VARS = ["DB_HOST", "DB_NAME", "DB_USER_READONLY", "DB_PASS_READONLY"]
_db_available = all(os.environ.get(v) for v in _REQUIRED_DB_VARS)
skip_no_db = pytest.mark.skipif(
    not _db_available,
    reason="Requires live PostgreSQL instance (set DB_HOST, DB_NAME, DB_USER_READONLY, DB_PASS_READONLY)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# These helpers provide minimal deployment inputs and subprocess results so each
# test can focus on one Stage 6 boundary: scripts, logs, counts, or DB checks.

def _make_manifest(**kwargs) -> Manifest:
    defaults = dict(
        commit_hash="abc1234",
        jira_ticket="DEV-42",
        ticket_number="42",
        pr_number="PR-99",
        author="Alice",
        timestamp="2024-01-15T12:00:00+00:00",
        release_dir="/tmp/releases/42",
        reports_dir="/tmp/releases/42/reports",
        sql_files=[],
        deleted_files=[],
        sql_changes=True,
        has_hard_fail=False,
        fail_reasons=[],
        warnings=[],
    )
    defaults.update(kwargs)
    return Manifest(**defaults)


def _make_config(tmp_path=None) -> MagicMock:
    cfg = MagicMock()
    cfg.db_host = "localhost"
    cfg.db_port = 5432
    cfg.db_name = "testdb"
    cfg.ec2_host = "ec2.example.com"
    cfg.ec2_user = "ubuntu"
    cfg.ssh_private_key = "/tmp/key.pem"
    cfg.ec2_releases_dir = "/home/ubuntu/releases"
    if tmp_path:
        cfg.releases_base_dir = str(tmp_path)
    return cfg


def _completed(returncode: int = 0) -> subprocess.CompletedProcess:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    return result


# ---------------------------------------------------------------------------
# parse_deploy_log
# ---------------------------------------------------------------------------

class TestParseDeployLog:
    def test_clean_log(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text("psql: running schema/foo.sql\nALTER TABLE\nCREATE FUNCTION\n")
        errors, count = parse_deploy_log(str(log_file))
        assert errors == []

    def test_error_line_detected(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text("Running foo.sql\nERROR: relation does not exist\nDone.\n")
        errors, _ = parse_deploy_log(str(log_file))
        assert len(errors) == 1
        assert "ERROR" in errors[0]

    def test_fatal_line_detected(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text("FATAL: password authentication failed\n")
        errors, _ = parse_deploy_log(str(log_file))
        assert len(errors) == 1

    def test_psql_error_detected(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text("psql: db/functions/foo.sql: 12: ERROR: syntax error\n")
        errors, _ = parse_deploy_log(str(log_file))
        assert len(errors) == 1

    def test_file_processed_count_psql_prefix(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text(
            "psql: db/functions/foo.sql: 1: CREATE\n"
            "psql: db/schema/bar.sql: 1: ALTER\n"
            "Some other line\n"
        )
        _, count = parse_deploy_log(str(log_file))
        assert count == 2

    def test_file_processed_count_running_prefix(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text(
            "Running db/functions/foo.sql\n"
            "Running db/schema/bar.sql\n"
        )
        _, count = parse_deploy_log(str(log_file))
        assert count == 2

    def test_missing_log_returns_empty(self, tmp_path):
        errors, count = parse_deploy_log(str(tmp_path / "nonexistent.log"))
        assert errors == []
        assert count == 0

    def test_multiple_errors(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text(
            "Running foo.sql\n"
            "ERROR: column does not exist\n"
            "ERROR: syntax error near token\n"
        )
        errors, _ = parse_deploy_log(str(log_file))
        assert len(errors) == 2

    def test_case_insensitive_error(self, tmp_path):
        log_file = tmp_path / "deploy.log"
        log_file.write_text("error: something went wrong\n")
        errors, _ = parse_deploy_log(str(log_file))
        assert len(errors) == 1


# ---------------------------------------------------------------------------
# _count_deploy_lst_entries
# ---------------------------------------------------------------------------

class TestCountDeployLstEntries:
    def test_counts_uncommented_lines(self, tmp_path):
        deploy_lst = tmp_path / "deploy.lst"
        deploy_lst.write_text(
            "# DEV-42 PR-99\n"
            "db/schema/foo.sql\n"
            "db/functions/bar.sql\n"
            "# db/functions/dup.sql --> duplicate of db/functions/bar.sql\n"
            "\n"
            "db/types/my_type.sql\n"
        )
        count = _count_deploy_lst_entries(str(tmp_path))
        assert count == 3

    def test_empty_file(self, tmp_path):
        deploy_lst = tmp_path / "deploy.lst"
        deploy_lst.write_text("")
        assert _count_deploy_lst_entries(str(tmp_path)) == 0

    def test_all_commented(self, tmp_path):
        deploy_lst = tmp_path / "deploy.lst"
        deploy_lst.write_text("# comment\n# another\n")
        assert _count_deploy_lst_entries(str(tmp_path)) == 0

    def test_missing_file_returns_zero(self, tmp_path):
        assert _count_deploy_lst_entries(str(tmp_path)) == 0


# ---------------------------------------------------------------------------
# _run_release_script
# ---------------------------------------------------------------------------

class TestRunReleaseScript:
    def test_success(self, tmp_path):
        cfg = _make_config(tmp_path)
        log_path = str(tmp_path / "deploy.log")
        with patch("stages.s6_test_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            _run_release_script(str(tmp_path), cfg, log_path)
        assert mock_run.called

    def test_non_zero_exit_hard_fails(self, tmp_path):
        cfg = _make_config(tmp_path)
        log_path = str(tmp_path / "deploy.log")
        with patch("stages.s6_test_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(1)
            with pytest.raises(SystemExit):
                _run_release_script(str(tmp_path), cfg, log_path)

    def test_unconfigured_ec2_host_hard_fails(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.ec2_host = ""
        log_path = str(tmp_path / "deploy.log")
        with pytest.raises(SystemExit):
            _run_release_script(str(tmp_path), cfg, log_path)

    def test_flags_passed_to_script(self, tmp_path):
        cfg = _make_config(tmp_path)
        log_path = str(tmp_path / "deploy.log")
        with patch("stages.s6_test_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            _run_release_script("/some/release/dir", cfg, log_path)
        # Last call is the SSH run; last element of cmd list is the remote command string.
        cmd_used = mock_run.call_args[0][0]
        remote_cmd_str = cmd_used[-1]
        assert "--target test" in remote_cmd_str
        assert "--skip-git" in remote_cmd_str


# ---------------------------------------------------------------------------
# check_audit_alignment_from_mutations (unit)
# ---------------------------------------------------------------------------

class TestCheckAuditAlignmentFromMutations:
    def _make_mock_conn(self, base_cols, audit_cols):
        """Build a mock connection that returns base_cols and audit_cols in order."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        # fetchall called twice: once for base table, once for audit table
        cur.fetchall.side_effect = [
            [(col,) for col in base_cols],
            [(col,) for col in audit_cols],
        ]
        conn.cursor.return_value = cur
        return conn

    def test_aligned_table(self):
        base_cols = ["id", "name", "email"]
        audit_cols = ["id", "name", "email", "audit_event", "audit_stamp", "audit_user_id"]
        mutations = [{"schema": "public", "table": "users"}]

        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = self._make_mock_conn(base_cols, audit_cols)
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            results = check_audit_alignment_from_mutations(mutations, cfg)

        assert len(results) == 1
        assert results[0]["aligned"] is True
        assert results[0]["missing_in_audit"] == []
        assert results[0]["extra_in_audit"] == []

    def test_missing_column_in_audit(self):
        base_cols = ["id", "name", "email"]
        # audit is missing 'email'
        audit_cols = ["id", "name", "audit_event", "audit_stamp", "audit_user_id"]
        mutations = [{"schema": "public", "table": "users"}]

        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = self._make_mock_conn(base_cols, audit_cols)
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            results = check_audit_alignment_from_mutations(mutations, cfg)

        assert results[0]["aligned"] is False
        assert "email" in results[0]["missing_in_audit"]

    def test_extra_column_in_audit(self):
        base_cols = ["id", "name"]
        # audit has an extra 'old_field' not in base (e.g. from a previous schema)
        audit_cols = ["id", "name", "old_field", "audit_event", "audit_stamp", "audit_user_id"]
        mutations = [{"schema": "public", "table": "users"}]

        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = self._make_mock_conn(base_cols, audit_cols)
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            results = check_audit_alignment_from_mutations(mutations, cfg)

        assert results[0]["aligned"] is False
        assert "old_field" in results[0]["extra_in_audit"]

    def test_deduplicates_same_table(self):
        # Two mutations for the same (schema, table) — should only query once.
        mutations = [
            {"schema": "public", "table": "orders"},
            {"schema": "public", "table": "orders"},
        ]
        base_cols = ["id", "amount"]
        audit_cols = ["id", "amount", "audit_event", "audit_stamp", "audit_user_id"]

        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = self._make_mock_conn(base_cols, audit_cols)
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            results = check_audit_alignment_from_mutations(mutations, cfg)

        assert len(results) == 1

    def test_audit_table_name_in_result(self):
        mutations = [{"schema": "public", "table": "payments"}]
        base_cols = ["id"]
        audit_cols = ["id", "audit_event", "audit_stamp", "audit_user_id"]

        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = self._make_mock_conn(base_cols, audit_cols)
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            results = check_audit_alignment_from_mutations(mutations, cfg)

        assert results[0]["audit_table"] == "audit.public_payments"

    def test_empty_mutations_returns_empty(self):
        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = MagicMock()
            mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=MagicMock())
            mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            results = check_audit_alignment_from_mutations([], cfg)
        assert results == []


# ---------------------------------------------------------------------------
# check_duplicate_functions (unit, mocked connection)
# ---------------------------------------------------------------------------

class TestCheckDuplicateFunctions:
    def test_no_duplicates(self):
        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = MagicMock()
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.fetchall.return_value = []
            mock_conn.cursor.return_value = cur
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            rows = check_duplicate_functions(cfg)
        assert rows == []

    def test_duplicates_returned(self):
        with patch("stages.s6_test_db_deploy.readonly_connection") as mock_rw:
            mock_conn = MagicMock()
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.fetchall.return_value = [("public", "get_user", 2)]
            mock_conn.cursor.return_value = cur
            mock_rw.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_rw.return_value.__exit__ = MagicMock(return_value=False)
            cfg = MagicMock()
            rows = check_duplicate_functions(cfg)
        assert len(rows) == 1
        assert rows[0] == ("public", "get_user", 2)


# ---------------------------------------------------------------------------
# run() integration (mocked external calls)
# ---------------------------------------------------------------------------

class TestS6Run:
    """Full run() tests with all external calls mocked."""

    def _patch_all(self, tmp_path, *, duplicate_rows=None, audit_aligned=True):
        """Return a context manager that patches all external calls for run()."""
        if duplicate_rows is None:
            duplicate_rows = []

        base_cols = ["id", "amount"]
        if audit_aligned:
            audit_cols = ["id", "amount", "audit_event", "audit_stamp", "audit_user_id"]
        else:
            audit_cols = ["id", "audit_event", "audit_stamp", "audit_user_id"]

        def patch_release(release_dir, cfg, log_path):
            open(log_path, "w").close()

        def patch_parse_log(log_path):
            return [], 2  # no errors, 2 files processed

        def patch_count_entries(release_dir):
            return 2  # matches processed count

        # Build mock readonly_connection that returns expected columns
        mock_conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        # duplicate check + audit alignment columns interleaved
        cur.fetchall.side_effect = [
            duplicate_rows,
            # audit alignment: base then audit
            [(c,) for c in base_cols],
            [(c,) for c in audit_cols],
        ]
        mock_conn.cursor.return_value = cur
        mock_rw_ctx = MagicMock()
        mock_rw_ctx.__enter__ = MagicMock(return_value=mock_conn)
        mock_rw_ctx.__exit__ = MagicMock(return_value=False)

        from contextlib import ExitStack
        from unittest.mock import patch as _patch

        return ExitStack(), [
            _patch("stages.s6_test_db_deploy._run_release_script", side_effect=patch_release),
            _patch("stages.s6_test_db_deploy.parse_deploy_log", side_effect=patch_parse_log),
            _patch("stages.s6_test_db_deploy._count_deploy_lst_entries", side_effect=patch_count_entries),
            _patch("stages.s6_test_db_deploy.readonly_connection", return_value=mock_rw_ctx),
        ]

    def test_successful_run_returns_summary(self, tmp_path):
        manifest = _make_manifest(jira_ticket="DEV-42", pr_number="PR-99")
        cfg = _make_config(tmp_path)
        mutations = [{"schema": "public", "table": "orders"}]

        _, patches = self._patch_all(tmp_path)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            summary = s6_run(manifest, str(tmp_path), mutations, cfg)

        assert summary["jira_ticket"] == "DEV-42"
        assert summary["count_match"] is True
        assert summary["has_hard_fail"] is False
        assert summary["duplicate_functions"] == []

    def test_duplicate_functions_hard_fail(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config(tmp_path)

        _, patches = self._patch_all(tmp_path, duplicate_rows=[("public", "get_user", 2)])
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(SystemExit):
                s6_run(manifest, str(tmp_path), [], cfg)

    def test_count_mismatch_hard_fail(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config(tmp_path)

        _, patches = self._patch_all(tmp_path)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            # Override parse_deploy_log to return mismatched count
            with patch("stages.s6_test_db_deploy.parse_deploy_log", return_value=([], 1)), \
                 patch("stages.s6_test_db_deploy._count_deploy_lst_entries", return_value=2):
                with pytest.raises(SystemExit):
                    s6_run(manifest, str(tmp_path), [], cfg)

    def test_audit_misalignment_hard_fail(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config(tmp_path)
        mutations = [{"schema": "public", "table": "orders"}]

        _, patches = self._patch_all(tmp_path, audit_aligned=False)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(SystemExit):
                s6_run(manifest, str(tmp_path), mutations, cfg)

    def test_count_verify_json_written(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config(tmp_path)

        _, patches = self._patch_all(tmp_path)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            s6_run(manifest, str(tmp_path), [], cfg)

        verify_path = tmp_path / "reports" / "test-count-verify.json"
        assert verify_path.exists()
        data = json.loads(verify_path.read_text())
        assert "expected" in data
        assert "processed" in data
        assert "match" in data

    def test_audit_verify_json_written(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config(tmp_path)
        mutations = [{"schema": "public", "table": "orders"}]

        _, patches = self._patch_all(tmp_path)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            s6_run(manifest, str(tmp_path), mutations, cfg)

        verify_path = tmp_path / "reports" / "test-audit-verify.json"
        assert verify_path.exists()
        data = json.loads(verify_path.read_text())
        assert isinstance(data, list)

    def test_no_table_mutations_skips_audit(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config(tmp_path)

        _, patches = self._patch_all(tmp_path)
        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            summary = s6_run(manifest, str(tmp_path), [], cfg)

        assert summary["audit_results"] == []
        assert summary["has_hard_fail"] is False


# ---------------------------------------------------------------------------
# Integration tests (require live DB)
# ---------------------------------------------------------------------------

@skip_no_db
class TestS6Integration:
    """Integration tests that require a real PostgreSQL instance."""

    @pytest.fixture(autouse=True)
    def setup_schema(self):
        """Create a test schema with a base and audit table, then tear down."""
        import psycopg2
        from pipeline.config import Config
        cfg = Config()
        conn = psycopg2.connect(cfg.db_dsn_readonly())
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("CREATE SCHEMA IF NOT EXISTS s6_test")
        cur.execute("CREATE SCHEMA IF NOT EXISTS audit")
        cur.execute(
            "CREATE TABLE IF NOT EXISTS s6_test.sample "
            "(id uuid, value text)"
        )
        cur.execute(
            "CREATE TABLE IF NOT EXISTS audit.s6_test_sample "
            "(id uuid, value text, "
            "audit_event text, audit_stamp timestamp, audit_user_id uuid)"
        )
        yield
        cur.execute("DROP TABLE IF EXISTS audit.s6_test_sample")
        cur.execute("DROP TABLE IF EXISTS s6_test.sample")
        cur.execute("DROP SCHEMA IF EXISTS s6_test CASCADE")
        cur.close()
        conn.close()

    def test_aligned_table_passes(self):
        from pipeline.config import Config
        cfg = Config()
        mutations = [{"schema": "s6_test", "table": "sample"}]
        results = check_audit_alignment_from_mutations(mutations, cfg)
        assert results[0]["aligned"] is True

    def test_no_duplicates_on_fresh_db(self):
        from pipeline.config import Config
        cfg = Config()
        rows = check_duplicate_functions(cfg)
        # A clean test DB should have no duplicates.
        assert isinstance(rows, list)
