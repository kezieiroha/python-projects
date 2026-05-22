"""
Tests for Stage 3: Database-assisted analysis, and for pipeline/db.py.

These are integration tests that require a live PostgreSQL instance. All tests
are skipped automatically if the required environment variables are absent.

To run against a local Postgres instance, set the following env vars before
invoking pytest:

    export DB_HOST=localhost
    export DB_PORT=5432
    export DB_NAME=acme_test
    export DB_USER_READONLY=acme_ro
    export DB_PASS_READONLY=test
    export DB_USER_RW=acme_rw
    export DB_PASS_RW=test
    export GIT_REPO_DATASCHEMA=/tmp/repo
    export RELEASES_BASE_DIR=/tmp/releases
    export GITHUB_TOKEN=dummy
    export GITHUB_REPOSITORY=acme/db-pipeline
    export JIRA_TICKET_PATTERN=DEV-\\d+
    export PR_NUMBER_PATTERN=PR-\\d+
    export GRAPHQL_API_URL=http://localhost/graphiql
    export GRAPHQL_CRM_URL=http://localhost/crm/graphiql

Run with: pytest tests/test_s3_db.py -v
"""

import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.models import FunctionSignature, Manifest, SqlFile

# ---------------------------------------------------------------------------
# Skip guard — all tests in this file require DB env vars.
# ---------------------------------------------------------------------------
#
# The module contains both pure unit tests and DB integration tests. The skip
# marker is applied only to integration classes so mocked unit coverage still
# runs without a local PostgreSQL instance.

_REQUIRED_DB_VARS = (
    "DB_HOST", "DB_NAME", "DB_USER_READONLY", "DB_PASS_READONLY",
)

_db_available = all(os.environ.get(v) for v in _REQUIRED_DB_VARS)

skip_no_db = pytest.mark.skipif(
    not _db_available,
    reason="DB environment variables not set — skipping integration tests",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# Shared builders keep the catalog-analysis tests focused on comparison,
# ordering, cascade, and audit behavior rather than repetitive Manifest/Config
# setup.

def _make_manifest(sql_files=None) -> Manifest:
    """Return a Manifest with no initial failures, warnings, or deleted files."""
    return Manifest(
        commit_hash="abc123",
        jira_ticket="DEV-1042",
        ticket_number="1042",
        pr_number="PR-88",
        author="test",
        timestamp="2024-01-15T14:30:00Z",
        release_dir="",
        reports_dir="",
        sql_files=sql_files or [],
        deleted_files=[],
        sql_changes=True,
        has_hard_fail=False,
        fail_reasons=[],
        warnings=[],
    )


def _make_cfg() -> MagicMock:
    """Return a mock Config with values from env vars (for integration tests)."""
    cfg = MagicMock()
    cfg.db_host = os.environ.get("DB_HOST", "localhost")
    cfg.db_name = os.environ.get("DB_NAME", "acme_test")
    cfg.db_user_readonly = os.environ.get("DB_USER_READONLY", "ro")
    cfg.db_pass_readonly = os.environ.get("DB_PASS_READONLY", "")
    port = int(os.environ.get("DB_PORT", "5432"))
    cfg.db_port = port
    cfg.db_dsn_readonly.return_value = (
        f"host={cfg.db_host} port={cfg.db_port} "
        f"dbname={cfg.db_name} user={cfg.db_user_readonly} "
        f"password={cfg.db_pass_readonly}"
    )
    cfg.releases_base_dir = os.environ.get("RELEASES_BASE_DIR", "/tmp/releases")
    cfg.git_repo_dataschema = os.environ.get("GIT_REPO_DATASCHEMA", "/tmp/repo")
    cfg.git_repo_serverless = ""
    cfg.serverless_configured.return_value = False
    return cfg


def _rw_dsn() -> str:
    """Return a psycopg2 DSN for the read-write role from env vars.

    Used only in integration test setup/teardown that must create DB objects.
    Not routed through pipeline.db — the RW role is not part of the pipeline API.
    """
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    name = os.environ.get("DB_NAME", "acme_test")
    user = os.environ.get("DB_USER_RW", "")
    pw = os.environ.get("DB_PASS_RW", "")
    return f"host={host} port={port} dbname={name} user={user} password={pw}"


# ---------------------------------------------------------------------------
# Unit tests — no DB required (mock psycopg2 connections)
# ---------------------------------------------------------------------------

class TestCheckFunctionSignatureDeltas:
    """Unit tests using mock connections."""

    def _make_mock_conn(self, fetchall_return):
        """Return a mock psycopg2 connection whose cursor always returns fetchall_return."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = fetchall_return
        conn.cursor.return_value = cur
        return conn

    def test_no_delta_when_signatures_match(self):
        """No delta is produced when the DB signature exactly matches the file signature."""
        from stages.s3_db_analysis import check_function_signature_deltas
        # DB returns same args and result as in the file.
        conn = self._make_mock_conn([("p_id uuid", "void")])
        sig = FunctionSignature(
            schema="public",
            name="my_func",
            param_types=["uuid"],
            return_type="void",
            source_file="func/my_func.sql",
        )
        deltas = check_function_signature_deltas(conn, [sig])
        assert deltas == []

    def test_delta_when_params_differ(self):
        """A delta is reported when the file adds a parameter that the DB version lacks."""
        from stages.s3_db_analysis import check_function_signature_deltas
        # DB has (uuid) but file now has (uuid, text).
        conn = self._make_mock_conn([("p_id uuid", "void")])
        sig = FunctionSignature(
            schema="public",
            name="my_func",
            param_types=["uuid", "text"],
            return_type="void",
            source_file="func/my_func.sql",
        )
        deltas = check_function_signature_deltas(conn, [sig])
        assert len(deltas) == 1
        assert deltas[0]["name"] == "my_func"
        assert "drop_statement" in deltas[0]
        assert "reason" in deltas[0]

    def test_delta_when_return_type_differs(self):
        """A delta is reported when the file changes the return type and the new type appears in the reason."""
        from stages.s3_db_analysis import check_function_signature_deltas
        conn = self._make_mock_conn([("p_id uuid", "text")])
        sig = FunctionSignature(
            schema="public",
            name="my_func",
            param_types=["uuid"],
            return_type="json",
            source_file="func/my_func.sql",
        )
        deltas = check_function_signature_deltas(conn, [sig])
        assert len(deltas) == 1
        assert "json" in deltas[0]["reason"]

    def test_no_delta_for_new_function(self):
        """No delta is produced for a function that does not yet exist in the DB."""
        from stages.s3_db_analysis import check_function_signature_deltas
        # DB returns empty — function doesn't exist yet.
        conn = self._make_mock_conn([])
        sig = FunctionSignature(
            schema="public",
            name="brand_new_func",
            param_types=["uuid"],
            return_type="void",
            source_file="func/new.sql",
        )
        deltas = check_function_signature_deltas(conn, [sig])
        assert deltas == []

    def test_drop_statement_uses_db_args(self):
        """The generated DROP statement uses the canonical DB argument list, not the file's."""
        from stages.s3_db_analysis import check_function_signature_deltas
        # DB args are canonical — drop statement must use DB args, not file args.
        conn = self._make_mock_conn([("p_id uuid, p_name character varying", "void")])
        sig = FunctionSignature(
            schema="public",
            name="my_func",
            param_types=["uuid", "text"],
            return_type="void",
            source_file="func/my_func.sql",
        )
        deltas = check_function_signature_deltas(conn, [sig])
        assert len(deltas) == 1
        assert "p_id uuid, p_name character varying" in deltas[0]["drop_statement"]

    def test_empty_signatures_returns_empty(self):
        """An empty signatures list returns no deltas and still issues a rollback."""
        from stages.s3_db_analysis import check_function_signature_deltas
        conn = MagicMock()
        deltas = check_function_signature_deltas(conn, [])
        assert deltas == []
        conn.rollback.assert_called_once()

    def test_rollback_called_after_query(self):
        """rollback() is called after querying to release any read lock."""
        from stages.s3_db_analysis import check_function_signature_deltas
        conn = self._make_mock_conn([])
        sig = FunctionSignature("public", "f", [], "void", "f.sql")
        check_function_signature_deltas(conn, [sig])
        conn.rollback.assert_called()


class TestTopologicalSort:
    """Tests for the dependency-cycle-safe topological sort used to order DROP statements."""

    def test_linear_dependency(self):
        """A node that depends on another is placed before it in the sorted order."""
        from stages.s3_db_analysis import _topological_sort
        # A depends on B: A must be dropped first.
        order, fails = _topological_sort(
            ["A", "B"], {"A": {"B"}, "B": set()}
        )
        assert fails == []
        assert order.index("A") < order.index("B")

    def test_no_dependencies(self):
        """Nodes with no dependencies are all present in the output with no failures."""
        from stages.s3_db_analysis import _topological_sort
        order, fails = _topological_sort(["A", "B", "C"], {"A": set(), "B": set(), "C": set()})
        assert fails == []
        assert set(order) == {"A", "B", "C"}

    def test_cycle_detected(self):
        """A dependency cycle produces a failure message containing 'cycle' and an empty order."""
        from stages.s3_db_analysis import _topological_sort
        order, fails = _topological_sort(
            ["A", "B"], {"A": {"B"}, "B": {"A"}}
        )
        assert len(fails) == 1
        assert "cycle" in fails[0].lower()
        assert order == []

    def test_empty_input(self):
        """Empty node list and edge map returns empty order with no failures."""
        from stages.s3_db_analysis import _topological_sort
        order, fails = _topological_sort([], {})
        assert order == []
        assert fails == []


class TestCheckCascadeVictims:
    """Tests for check_cascade_victims which detects collateral DROP victims via pg_depend."""

    def _make_mock_conn(self, victim_rows_per_entry):
        """Return a mock conn whose cursor().fetchall() returns the given victim rows per DROP entry."""
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.side_effect = victim_rows_per_entry
        conn.cursor.return_value = cur
        return conn

    def test_no_victims_when_nothing_cascades(self):
        """When pg_depend returns no rows, there are no victims."""
        from stages.s3_db_analysis import check_cascade_victims
        conn = self._make_mock_conn([[]])  # one DROP entry, zero dependent rows
        drop_entries = [{"schema": "public", "name": "f1", "drop_statement": "DROP FUNCTION IF EXISTS public.f1() CASCADE;"}]
        release_scope = {"public.f1"}
        victims, fails = check_cascade_victims(conn, drop_entries, release_scope)
        assert fails == []
        assert victims == []

    def test_cascade_victim_in_scope_is_safe(self):
        """A cascade victim that is also in the release scope is reported but not a hard fail."""
        from stages.s3_db_analysis import check_cascade_victims
        # pg_depend returns f2 as a dependent of f1
        conn = self._make_mock_conn([[("public", "f2", "function")]])
        drop_entries = [{"schema": "public", "name": "f1", "drop_statement": "DROP FUNCTION IF EXISTS public.f1() CASCADE;"}]
        release_scope = {"public.f1", "public.f2"}
        victims, fails = check_cascade_victims(conn, drop_entries, release_scope)
        assert fails == []
        assert len(victims) == 1
        assert victims[0].object_name == "public.f2"
        assert victims[0].in_release_scope is True

    def test_cascade_victim_out_of_scope_hard_fails(self):
        """A cascade victim that is NOT in the release scope produces a hard fail."""
        from stages.s3_db_analysis import check_cascade_victims
        conn = self._make_mock_conn([[("public", "unrelated", "function")]])
        drop_entries = [{"schema": "public", "name": "f1", "drop_statement": "DROP FUNCTION IF EXISTS public.f1() CASCADE;"}]
        release_scope = {"public.f1"}
        victims, fails = check_cascade_victims(conn, drop_entries, release_scope)
        assert len(fails) == 1
        assert "unrelated" in fails[0]
        assert len(victims) == 1
        assert victims[0].in_release_scope is False

    def test_empty_drop_entries_returns_empty(self):
        """No drop entries returns empty lists and calls rollback."""
        from stages.s3_db_analysis import check_cascade_victims
        conn = MagicMock()
        victims, fails = check_cascade_victims(conn, [], set())
        assert victims == []
        assert fails == []
        conn.rollback.assert_called_once()

    def test_rollback_called_after_check(self):
        """rollback() is called after the catalog query to discard implicit transaction state."""
        from stages.s3_db_analysis import check_cascade_victims
        conn = self._make_mock_conn([[]])
        drop_entries = [{"schema": "public", "name": "f1", "drop_statement": "DROP FUNCTION IF EXISTS public.f1() CASCADE;"}]
        check_cascade_victims(conn, drop_entries, {"public.f1"})
        conn.rollback.assert_called()


class TestBuildReleaseScope:
    """Tests for the helper that builds the schema.name scope set from a static report."""

    def test_builds_scope_from_signatures(self):
        """Function signatures in the static report contribute schema.name entries to the scope."""
        from stages.s3_db_analysis import _build_release_scope
        static_report = {
            "function_signatures": [
                {"schema": "public", "name": "get_user"},
                {"schema": "api", "name": "create_order"},
            ],
            "type_definitions": [],
        }
        scope = _build_release_scope(static_report)
        assert "public.get_user" in scope
        assert "api.create_order" in scope

    def test_builds_scope_from_type_definitions(self):
        """Type definitions in the static report contribute schema.name entries to the scope."""
        from stages.s3_db_analysis import _build_release_scope
        static_report = {
            "function_signatures": [],
            "type_definitions": [{"schema": "public", "name": "order_status"}],
        }
        scope = _build_release_scope(static_report)
        assert "public.order_status" in scope

    def test_empty_report_returns_empty_scope(self):
        """An empty static report produces an empty scope set."""
        from stages.s3_db_analysis import _build_release_scope
        assert _build_release_scope({}) == set()


class TestCheckDuplicateFunctions:
    """Tests for the duplicate-function overload detector."""

    def test_no_duplicates_returns_empty(self):
        """When the DB has no overloaded functions, an empty list is returned."""
        from stages.s3_db_analysis import check_duplicate_functions
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        result = check_duplicate_functions(conn)
        assert result == []

    def test_duplicates_returned_correctly(self):
        """Overloaded functions are returned with schema, name, and overload_count."""
        from stages.s3_db_analysis import check_duplicate_functions
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = [("public", "get_user", 2)]
        conn.cursor.return_value = cur
        result = check_duplicate_functions(conn)
        assert len(result) == 1
        assert result[0]["schema"] == "public"
        assert result[0]["name"] == "get_user"
        assert result[0]["overload_count"] == 2

    def test_rollback_called(self):
        """rollback() is called after the query to release any read lock."""
        from stages.s3_db_analysis import check_duplicate_functions
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        check_duplicate_functions(conn)
        conn.rollback.assert_called_once()


class TestParseHelpers:
    def test_parse_pg_args_empty(self):
        from stages.s3_db_analysis import _parse_pg_args
        assert _parse_pg_args("") == []

    def test_parse_pg_args_named_params(self):
        from stages.s3_db_analysis import _parse_pg_args
        result = _parse_pg_args("p_id uuid, p_name text")
        assert result == ["uuid", "text"]

    def test_parse_pg_args_anonymous_params(self):
        from stages.s3_db_analysis import _parse_pg_args
        result = _parse_pg_args("uuid, text, integer")
        assert result == ["uuid", "text", "integer"]

    def test_normalise_type_lowercases(self):
        from stages.s3_db_analysis import _normalise_type
        assert _normalise_type("  UUID  ") == "uuid"

    def test_normalise_type_list(self):
        from stages.s3_db_analysis import _normalise_type_list
        assert _normalise_type_list(["UUID", "TEXT"]) == ["uuid", "text"]


class TestRunUnit:
    """Unit tests for run() using mock DB connections."""

    def _make_mock_cfg(self):
        cfg = MagicMock()
        cfg.releases_base_dir = "/tmp"
        cfg.git_repo_dataschema = "/tmp"
        cfg.git_repo_serverless = ""
        cfg.serverless_configured.return_value = False
        return cfg

    def _make_mock_conn(self):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall.return_value = []
        conn.cursor.return_value = cur
        return conn

    def test_clean_run_no_fails(self):
        from stages.s3_db_analysis import run
        manifest = _make_manifest()
        static_report = {
            "function_signatures": [],
            "table_mutations": [],
            "type_definitions": [],
        }
        cfg = self._make_mock_cfg()
        mock_conn = self._make_mock_conn()

        with patch("stages.s3_db_analysis.readonly_connection") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            updated, report, victims, gaps, drop_order = run(manifest, static_report, cfg)

        assert not updated.has_hard_fail
        assert report["hard_fails"] == []
        assert victims == []
        assert gaps == []
        assert drop_order == []

    def test_duplicate_functions_produce_warnings(self):
        from stages.s3_db_analysis import run
        manifest = _make_manifest()
        static_report = {"function_signatures": [], "table_mutations": [], "type_definitions": []}
        cfg = self._make_mock_cfg()
        mock_conn = self._make_mock_conn()
        # With no signatures, check_function_signature_deltas never calls fetchall.
        # The first (and only) fetchall call comes from check_duplicate_functions.
        mock_conn.cursor.return_value.fetchall.side_effect = [
            [("public", "get_user", 2)],  # duplicate functions
        ]

        with patch("stages.s3_db_analysis.readonly_connection") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            updated, report, victims, gaps, drop_order = run(manifest, static_report, cfg)

        assert not updated.has_hard_fail
        assert len(updated.warnings) >= 1
        assert any("get_user" in w for w in updated.warnings)

    def test_stage_field_in_report(self):
        from stages.s3_db_analysis import run
        manifest = _make_manifest()
        static_report = {"function_signatures": [], "table_mutations": [], "type_definitions": []}
        cfg = self._make_mock_cfg()
        mock_conn = self._make_mock_conn()

        with patch("stages.s3_db_analysis.readonly_connection") as mock_ctx:
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            _, report, _, _, _ = run(manifest, static_report, cfg)

        assert report["stage"] == "s3_db_analysis"

    def test_signature_delta_generates_drop_order_without_hard_fail(self):
        from stages.s3_db_analysis import run

        manifest = _make_manifest()
        static_report = {
            "function_signatures": [
                {
                    "schema": "public",
                    "name": "get_user",
                    "param_types": ["uuid"],
                    "return_type": "jsonb",
                    "source_file": "functions/get_user.sql",
                }
            ],
            "table_mutations": [],
            "type_definitions": [],
        }
        cfg = self._make_mock_cfg()
        mock_conn = self._make_mock_conn()
        delta = {
            "schema": "public",
            "name": "get_user",
            "reason": "Signature changed: was (p_id uuid) RETURNS json, now (uuid) RETURNS jsonb",
            "drop_statement": "DROP FUNCTION IF EXISTS public.get_user(p_id uuid) CASCADE;",
        }

        with patch("stages.s3_db_analysis.readonly_connection") as mock_ctx, \
             patch("stages.s3_db_analysis.check_function_signature_deltas", return_value=[delta]), \
             patch("stages.s3_db_analysis.compute_drop_order", return_value=([delta], [])), \
             patch("stages.s3_db_analysis.check_cascade_victims", return_value=([], [])), \
             patch("stages.s3_db_analysis.check_duplicate_functions", return_value=[]):
            mock_ctx.return_value.__enter__ = MagicMock(return_value=mock_conn)
            mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
            updated, report, victims, gaps, drop_order = run(manifest, static_report, cfg)

        assert not updated.has_hard_fail
        assert report["hard_fails"] == []
        assert report["signature_deltas"] == [delta]
        assert report["drop_order"] == [delta]
        assert drop_order == [delta]
        assert victims == []
        assert gaps == []


# ---------------------------------------------------------------------------
# Integration tests — require a live PostgreSQL instance
# ---------------------------------------------------------------------------

@skip_no_db
class TestDbConnectionIntegration:
    def test_readonly_connection_opens_and_closes(self):
        from pipeline.db import readonly_connection
        cfg = _make_cfg()
        with readonly_connection(cfg) as conn:
            assert conn is not None
            assert not conn.closed
        assert conn.closed

    def test_readonly_connection_can_query_pg_catalog(self):
        from pipeline.db import readonly_connection
        cfg = _make_cfg()
        with readonly_connection(cfg) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                row = cur.fetchone()
            conn.rollback()
        assert row == (1,)


@skip_no_db
class TestS3IntegrationWithRealDb:
    """Integration tests that exercise Stage 3 against a real PostgreSQL instance.

    These tests create and clean up their own schema and objects.
    """

    @pytest.fixture(autouse=True)
    def setup_and_teardown(self):
        """Create a test schema with known functions, then drop it after the test."""
        import psycopg2
        conn = psycopg2.connect(_rw_dsn())
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS s3_test;")
            cur.execute("""
                CREATE OR REPLACE FUNCTION s3_test.existing_func(p_id uuid)
                RETURNS void
                LANGUAGE plpgsql AS $$
                BEGIN
                END;
                $$;
            """)
        conn.commit()
        yield
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS s3_test CASCADE;")
        conn.commit()
        conn.close()

    def test_no_delta_when_signature_unchanged(self):
        from stages.s3_db_analysis import check_function_signature_deltas
        from pipeline.db import readonly_connection
        cfg = _make_cfg()
        sig = FunctionSignature(
            schema="s3_test",
            name="existing_func",
            param_types=["uuid"],
            return_type="void",
            source_file="test.sql",
        )
        with readonly_connection(cfg) as conn:
            deltas = check_function_signature_deltas(conn, [sig])
        assert deltas == []

    def test_delta_detected_when_return_type_changes(self):
        from stages.s3_db_analysis import check_function_signature_deltas
        from pipeline.db import readonly_connection
        cfg = _make_cfg()
        sig = FunctionSignature(
            schema="s3_test",
            name="existing_func",
            param_types=["uuid"],
            return_type="text",  # changed from void
            source_file="test.sql",
        )
        with readonly_connection(cfg) as conn:
            deltas = check_function_signature_deltas(conn, [sig])
        assert len(deltas) == 1
        assert deltas[0]["name"] == "existing_func"

    def test_check_duplicate_functions_clean(self):
        from stages.s3_db_analysis import check_duplicate_functions
        from pipeline.db import readonly_connection
        cfg = _make_cfg()
        with readonly_connection(cfg) as conn:
            dupes = check_duplicate_functions(conn)
        # s3_test schema has no duplicates.
        schema_dupes = [d for d in dupes if d["schema"] == "s3_test"]
        assert schema_dupes == []
