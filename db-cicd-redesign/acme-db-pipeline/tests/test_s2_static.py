"""
Tests for Stage 2: SQL static analysis.

Each test function exercises one logical check or extraction. Fixtures are
loaded from tests/fixtures/ by path so the test suite does not depend on a
running database or CI instance.

Run with: pytest tests/test_s2_static.py -v
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.models import Manifest, SqlFile
from stages.s2_static_analysis import (
    check_cascade_warnings,
    check_ddl_in_wrong_file,
    check_drop_in_wrong_location,
    check_privilege_escalation,
    check_set_role,
    extract_function_signatures,
    extract_table_mutations,
    extract_type_definitions,
    run,
    sort_sql_files,
)

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _fixture(name: str) -> str:
    """Return the absolute path to a fixture file."""
    return os.path.join(FIXTURES_DIR, name)


def _read(name: str) -> str:
    """Return the content of a fixture file as a string."""
    with open(_fixture(name), "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Check 1: SET ROLE
# ---------------------------------------------------------------------------
#
# These tests protect the first-statement rule: comments and whitespace may
# precede SET ROLE, but no executable SQL may run before it.

class TestCheckSetRole:
    """Tests for the SET ROLE first-statement check."""

    def test_valid_file_passes(self):
        """A file starting with SET ROLE acme_admin produces no failures."""
        content = _read("good_function.sql")
        result = check_set_role("good_function.sql", content)
        assert result == []

    def test_missing_set_role_fails(self):
        """A file missing the SET ROLE statement produces one failure naming the file."""
        content = _read("bad_no_setrole.sql")
        result = check_set_role("bad_no_setrole.sql", content)
        assert len(result) == 1
        assert "bad_no_setrole.sql" in result[0]
        assert "SET ROLE" in result[0]

    def test_set_role_after_blank_lines_passes(self):
        """Leading blank lines before SET ROLE are ignored during the check."""
        content = "\n\n\nSET ROLE \"acme_admin\";\nSELECT 1;"
        result = check_set_role("test.sql", content)
        assert result == []

    def test_set_role_after_comment_block_passes(self):
        """A header line-comment block before SET ROLE does not trigger a failure."""
        content = "-- header comment\n-- another line\nSET ROLE \"acme_admin\";\nSELECT 1;"
        result = check_set_role("test.sql", content)
        assert result == []

    def test_set_role_after_block_comment_passes(self):
        """A block comment before SET ROLE does not trigger a failure."""
        content = "/* block\ncomment */\nSET ROLE \"acme_admin\";\nSELECT 1;"
        result = check_set_role("test.sql", content)
        assert result == []

    def test_wrong_role_name_fails(self):
        """SET ROLE with a name other than acme_admin is treated as a failure."""
        content = 'SET ROLE "other_admin";\nSELECT 1;'
        result = check_set_role("test.sql", content)
        assert len(result) == 1

    def test_set_role_case_insensitive_keywords(self):
        """The SET ROLE keyword match is case-insensitive."""
        content = 'set role "acme_admin";\nSELECT 1;'
        result = check_set_role("test.sql", content)
        assert result == []

    def test_empty_file_fails(self):
        """An empty file has no SET ROLE statement and therefore fails."""
        result = check_set_role("empty.sql", "")
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Check 2: Privilege escalation
# ---------------------------------------------------------------------------

class TestCheckPrivilegeEscalation:
    """Tests for the privilege-escalation keyword check."""

    def test_clean_file_passes(self):
        """A file with no privilege keywords produces no failures."""
        content = _read("good_function.sql")
        result = check_privilege_escalation("good_function.sql", content)
        assert result == []

    def test_grant_detected(self):
        """GRANT keyword in a file is flagged as a privilege escalation failure."""
        content = _read("bad_privilege_escalation.sql")
        result = check_privilege_escalation("bad_privilege_escalation.sql", content)
        assert any("GRANT" in r for r in result)

    def test_revoke_detected(self):
        """REVOKE keyword is flagged."""
        content = 'SET ROLE "acme_admin";\nREVOKE ALL ON TABLE public.foo FROM web_user;'
        result = check_privilege_escalation("test.sql", content)
        assert any("REVOKE" in r for r in result)

    def test_create_user_detected(self):
        """CREATE USER keyword is flagged."""
        content = 'SET ROLE "acme_admin";\nCREATE USER service_account WITH PASSWORD \'x\';'
        result = check_privilege_escalation("test.sql", content)
        assert any("CREATE USER" in r for r in result)

    def test_alter_role_detected(self):
        """ALTER ROLE keyword is flagged."""
        content = 'SET ROLE "acme_admin";\nALTER ROLE web_user SET search_path TO public;'
        result = check_privilege_escalation("test.sql", content)
        assert any("ALTER ROLE" in r for r in result)

    def test_superuser_detected(self):
        """SUPERUSER keyword is flagged even when it is the only privilege keyword on the line."""
        # Use a line where SUPERUSER is the only flagged keyword so the pattern
        # fires on SUPERUSER rather than on ALTER ROLE before it in the scan order.
        content = 'SET ROLE "acme_admin";\nSUPERUSER;'
        result = check_privilege_escalation("test.sql", content)
        assert any("SUPERUSER" in r for r in result)

    def test_create_role_detected(self):
        """CREATE ROLE keyword is flagged."""
        content = 'SET ROLE "acme_admin";\nCREATE ROLE readonly;'
        result = check_privilege_escalation("test.sql", content)
        assert any("CREATE ROLE" in r for r in result)

    def test_line_number_included(self):
        """Failure messages include the line number where the keyword appears."""
        content = 'SET ROLE "acme_admin";\n\nGRANT SELECT ON public.users TO web_user;'
        result = check_privilege_escalation("test.sql", content)
        assert len(result) == 1
        assert ":3" in result[0]

    def test_keyword_in_comment_still_detected(self):
        """Privilege keywords inside SQL comments are still flagged (security check)."""
        content = 'SET ROLE "acme_admin";\n-- GRANT access to this\nSELECT 1;'
        result = check_privilege_escalation("test.sql", content)
        assert len(result) == 1

    def test_case_insensitive(self):
        """Keyword matching is case-insensitive."""
        content = 'SET ROLE "acme_admin";\ngrant select on public.users to web_user;'
        result = check_privilege_escalation("test.sql", content)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Check 3: DDL in wrong file type
# ---------------------------------------------------------------------------

class TestCheckDdlInWrongFile:
    """Tests for the DDL-in-wrong-file-type check."""

    def test_schema_file_with_ddl_passes(self):
        """CREATE TABLE in a schema-classified file does not fail."""
        content = _read("good_schema_create_table.sql")
        result = check_ddl_in_wrong_file("good_schema_create_table.sql", "schema", content)
        assert result == []

    def test_function_file_with_create_table_fails(self):
        """CREATE TABLE in a function-classified file is a hard fail."""
        content = _read("bad_ddl_in_function.sql")
        result = check_ddl_in_wrong_file("bad_ddl_in_function.sql", "function", content)
        assert len(result) >= 1
        assert any("CREATE" in r or "DDL" in r for r in result)

    def test_type_file_with_alter_table_fails(self):
        """ALTER TABLE in a type-classified file is a hard fail."""
        content = 'SET ROLE "acme_admin";\nALTER TABLE public.users ADD COLUMN x text;'
        result = check_ddl_in_wrong_file("test.sql", "type", content)
        assert len(result) == 1

    def test_config_file_with_create_table_fails(self):
        """CREATE TABLE in a config-classified file is a hard fail."""
        content = 'SET ROLE "acme_admin";\nCREATE TABLE public.config_entries (k text, v text);'
        result = check_ddl_in_wrong_file("test.sql", "config", content)
        assert len(result) == 1

    def test_ddl_in_comment_not_flagged_in_function(self):
        """DDL keywords inside SQL comments are not flagged after comment stripping."""
        content = 'SET ROLE "acme_admin";\n-- CREATE TABLE would go here\nSELECT 1;'
        result = check_ddl_in_wrong_file("test.sql", "function", content)
        assert result == []

    def test_line_number_included(self):
        """Failure messages include the line number of the DDL statement."""
        content = 'SET ROLE "acme_admin";\n\nCREATE TABLE public.bad (id uuid);'
        result = check_ddl_in_wrong_file("test.sql", "function", content)
        assert ":3" in result[0]


# ---------------------------------------------------------------------------
# Check 4: DROP in wrong location
# ---------------------------------------------------------------------------

class TestCheckDropInWrongLocation:
    """Tests for the DROP-in-wrong-location check (DROPs belong in prep.sql only)."""

    def test_type_file_with_drop_function_passes(self):
        """DROP FUNCTION in a type-classified file is allowed (not schema or function)."""
        content = 'SET ROLE "acme_admin";\nDROP FUNCTION IF EXISTS public.old_func(uuid);'
        result = check_drop_in_wrong_location("test.sql", "type", content)
        assert result == []

    def test_schema_file_with_drop_function_fails(self):
        """DROP FUNCTION in a schema-classified file is a hard fail."""
        content = _read("bad_drop_misplaced.sql")
        result = check_drop_in_wrong_location("bad_drop_misplaced.sql", "schema", content)
        assert len(result) >= 1
        assert any("DROP" in r for r in result)

    def test_function_file_with_drop_type_fails(self):
        """DROP TYPE in a function-classified file is a hard fail."""
        content = 'SET ROLE "acme_admin";\nDROP TYPE IF EXISTS public.old_status;'
        result = check_drop_in_wrong_location("test.sql", "function", content)
        assert len(result) == 1

    def test_function_file_with_drop_procedure_fails(self):
        """DROP PROCEDURE in a function-classified file is a hard fail."""
        content = 'SET ROLE "acme_admin";\nDROP PROCEDURE IF EXISTS public.old_proc(uuid);'
        result = check_drop_in_wrong_location("test.sql", "function", content)
        assert len(result) == 1

    def test_drop_in_comment_not_flagged(self):
        """DROP keywords inside SQL comments are not flagged after comment stripping."""
        content = 'SET ROLE "acme_admin";\n-- DROP FUNCTION here is fine in comment\nSELECT 1;'
        result = check_drop_in_wrong_location("test.sql", "schema", content)
        assert result == []

    def test_line_number_included(self):
        """Failure messages include the line number of the misplaced DROP."""
        content = 'SET ROLE "acme_admin";\n\nDROP FUNCTION IF EXISTS public.old(uuid);'
        result = check_drop_in_wrong_location("test.sql", "schema", content)
        assert ":3" in result[0]


# ---------------------------------------------------------------------------
# Check 5: CASCADE warnings
# ---------------------------------------------------------------------------

class TestCheckCascadeWarnings:
    """Tests for the CASCADE-keyword-outside-DROP warning check."""

    def test_no_cascade_no_warning(self):
        """A file with no CASCADE keyword produces no warnings."""
        content = _read("good_function.sql")
        result = check_cascade_warnings("good_function.sql", content)
        assert result == []

    def test_cascade_in_comment_warns(self):
        """CASCADE keyword in a comment (outside DROP context) produces a warning."""
        content = _read("cascade_warning.sql")
        result = check_cascade_warnings("cascade_warning.sql", content)
        assert len(result) >= 1
        assert all("CASCADE" in r for r in result)

    def test_drop_cascade_does_not_warn(self):
        """CASCADE immediately following a DROP statement is allowed and not warned."""
        content = 'SET ROLE "acme_admin";\nDROP FUNCTION IF EXISTS public.old(uuid) CASCADE;'
        result = check_cascade_warnings("test.sql", content)
        assert result == []

    def test_cascade_outside_drop_warns(self):
        """CASCADE on a non-DROP statement (e.g. TRUNCATE) produces a warning with line number."""
        content = 'SET ROLE "acme_admin";\nTRUNCATE public.orders CASCADE;'
        result = check_cascade_warnings("test.sql", content)
        assert len(result) == 1
        assert ":2" in result[0]

    def test_line_number_correct(self):
        """Warning messages include the correct source line number."""
        content = 'SET ROLE "acme_admin";\n\n-- will CASCADE\nSELECT 1;'
        result = check_cascade_warnings("test.sql", content)
        assert len(result) == 1
        assert ":3" in result[0]


# ---------------------------------------------------------------------------
# Extraction 6: Function signatures
# ---------------------------------------------------------------------------

class TestExtractFunctionSignatures:
    """Tests for function-signature extraction from CREATE OR REPLACE FUNCTION statements."""

    def test_extracts_signature_from_good_function(self):
        """Standard fixture function yields one signature with correct schema, name, and params."""
        content = _read("good_function.sql")
        sigs = extract_function_signatures("good_function.sql", content)
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.schema == "public"
        assert sig.name == "get_user_by_id"
        assert sig.param_types == ["uuid"]
        assert "TABLE" in sig.return_type.upper()
        assert sig.source_file == "good_function.sql"

    def test_no_function_returns_empty(self):
        """A file with no function definition returns an empty list."""
        content = _read("good_schema_create_table.sql")
        sigs = extract_function_signatures("good_schema_create_table.sql", content)
        assert sigs == []

    def test_extracts_void_return_type(self):
        """A RETURNS void function is extracted with the void return type preserved."""
        content = (
            'SET ROLE "acme_admin";\n'
            'CREATE OR REPLACE FUNCTION api.do_work(p_id uuid, p_name text)\n'
            'RETURNS void\n'
            'LANGUAGE plpgsql\n'
            'AS $$\nBEGIN\nEND;\n$$;\n'
        )
        sigs = extract_function_signatures("test.sql", content)
        assert len(sigs) == 1
        assert sigs[0].schema == "api"
        assert sigs[0].name == "do_work"
        assert sigs[0].param_types == ["uuid", "text"]
        assert sigs[0].return_type.lower() == "void"

    def test_defaults_schema_to_public(self):
        """An unqualified function name is assigned the public schema by default."""
        content = (
            'SET ROLE "acme_admin";\n'
            'CREATE OR REPLACE FUNCTION unqualified_func(p_id uuid)\n'
            'RETURNS text\n'
            'LANGUAGE sql\n'
            'AS $$ SELECT \'x\'; $$;\n'
        )
        sigs = extract_function_signatures("test.sql", content)
        assert len(sigs) == 1
        assert sigs[0].schema == "public"

    def test_setof_return_type(self):
        """A RETURNS SETOF function captures the full SETOF clause in return_type."""
        content = (
            'SET ROLE "acme_admin";\n'
            'CREATE OR REPLACE FUNCTION public.list_users()\n'
            'RETURNS SETOF public.users\n'
            'LANGUAGE sql\n'
            'AS $$ SELECT * FROM public.users; $$;\n'
        )
        sigs = extract_function_signatures("test.sql", content)
        assert len(sigs) == 1
        assert "SETOF" in sigs[0].return_type.upper()

    def test_no_params_function(self):
        """A function with an empty parameter list yields an empty param_types list."""
        content = (
            'SET ROLE "acme_admin";\n'
            'CREATE OR REPLACE FUNCTION public.now_utc()\n'
            'RETURNS timestamp\n'
            'LANGUAGE sql\n'
            'AS $$ SELECT now() AT TIME ZONE \'UTC\'; $$;\n'
        )
        sigs = extract_function_signatures("test.sql", content)
        assert len(sigs) == 1
        assert sigs[0].param_types == []

    def test_in_mode_prefix_stripped(self):
        """IN mode prefixes on parameter names are stripped, leaving only the type."""
        content = (
            'SET ROLE "acme_admin";\n'
            'CREATE OR REPLACE FUNCTION public.f(IN p_id uuid, IN p_name text)\n'
            'RETURNS void\n'
            'LANGUAGE plpgsql\n'
            'AS $$\nBEGIN\nEND;\n$$;\n'
        )
        sigs = extract_function_signatures("test.sql", content)
        assert len(sigs) == 1
        assert sigs[0].param_types == ["uuid", "text"]

    def test_quoted_identifiers_extracted(self):
        """Double-quoted schema and function names are extracted correctly."""
        content = _read("good_function_quoted.sql")
        sigs = extract_function_signatures("good_function_quoted.sql", content)
        assert len(sigs) == 1
        sig = sigs[0]
        assert sig.schema == "acme_api"
        assert sig.name == "get_reward_estimate"
        assert sig.param_types == ["UUID", "INTEGER", "NUMERIC"]
        assert sig.return_type.upper() == "NUMERIC"


# ---------------------------------------------------------------------------
# Extraction 7: Type definitions
# ---------------------------------------------------------------------------

class TestExtractTypeDefinitions:
    """Tests for type-definition extraction from CREATE TYPE statements."""

    def test_extracts_type_from_good_type_file(self):
        """Standard fixture type file yields one type dict with correct schema and name."""
        content = _read("good_type.sql")
        types = extract_type_definitions("good_type.sql", content)
        assert len(types) == 1
        t = types[0]
        assert t["schema"] == "public"
        assert t["name"] == "order_status"
        assert t["source_file"] == "good_type.sql"

    def test_no_type_returns_empty(self):
        """A file with no CREATE TYPE statement returns an empty list."""
        content = _read("good_function.sql")
        types = extract_type_definitions("good_function.sql", content)
        assert types == []

    def test_defaults_schema_to_public(self):
        """An unqualified type name is assigned the public schema by default."""
        content = 'SET ROLE "acme_admin";\nCREATE TYPE unqualified_type AS ENUM (\'a\', \'b\');'
        types = extract_type_definitions("test.sql", content)
        assert len(types) == 1
        assert types[0]["schema"] == "public"

    def test_explicit_schema_preserved(self):
        """A schema-qualified type name preserves the explicit schema."""
        content = 'SET ROLE "acme_admin";\nCREATE TYPE api.payment_status AS ENUM (\'paid\', \'pending\');'
        types = extract_type_definitions("test.sql", content)
        assert len(types) == 1
        assert types[0]["schema"] == "api"
        assert types[0]["name"] == "payment_status"


# ---------------------------------------------------------------------------
# Extraction 8: Table mutations
# ---------------------------------------------------------------------------

class TestExtractTableMutations:
    """Tests for table-mutation extraction from ALTER TABLE and CREATE TABLE statements."""

    def test_create_table_extracted(self):
        """CREATE TABLE in a schema fixture yields one create_table mutation with correct identifiers."""
        content = _read("good_schema_create_table.sql")
        mutations = extract_table_mutations("good_schema_create_table.sql", content)
        create_mutations = [m for m in mutations if m.mutation_type == "create_table"]
        assert len(create_mutations) == 1
        assert create_mutations[0].schema == "public"
        assert create_mutations[0].table == "invoices"

    def test_add_column_extracted(self):
        """ADD COLUMN statements in a schema fixture yield one add_column mutation per column."""
        content = _read("good_schema_add_col.sql")
        mutations = extract_table_mutations("good_schema_add_col.sql", content)
        add_mutations = [m for m in mutations if m.mutation_type == "add_column"]
        assert len(add_mutations) == 2
        column_names = [m.columns_added[0] for m in add_mutations]
        assert "filled_at" in column_names
        assert "settlement_currency" in column_names

    def test_drop_column_extracted(self):
        """DROP COLUMN produces one drop_column mutation with the column name in columns_dropped."""
        content = (
            'SET ROLE "acme_admin";\n'
            'ALTER TABLE public.orders DROP COLUMN legacy_field;'
        )
        mutations = extract_table_mutations("test.sql", content)
        drop_mutations = [m for m in mutations if m.mutation_type == "drop_column"]
        assert len(drop_mutations) == 1
        assert drop_mutations[0].columns_dropped == ["legacy_field"]

    def test_rename_column_extracted(self):
        """RENAME COLUMN records the old name in columns_dropped and the new name in columns_added."""
        content = (
            'SET ROLE "acme_admin";\n'
            'ALTER TABLE public.orders RENAME COLUMN old_name TO new_name;'
        )
        mutations = extract_table_mutations("test.sql", content)
        rename_mutations = [m for m in mutations if m.mutation_type == "rename_column"]
        assert len(rename_mutations) == 1
        assert rename_mutations[0].columns_dropped == ["old_name"]
        assert rename_mutations[0].columns_added == ["new_name"]

    def test_no_mutations_returns_empty(self):
        """A pure function file with no DDL produces an empty mutations list."""
        content = _read("good_function.sql")
        mutations = extract_table_mutations("good_function.sql", content)
        assert mutations == []

    def test_schema_defaulted_to_public(self):
        """An unqualified table name in ALTER TABLE is assigned the public schema."""
        content = (
            'SET ROLE "acme_admin";\n'
            'ALTER TABLE unqualified_table ADD COLUMN x text;'
        )
        mutations = extract_table_mutations("test.sql", content)
        assert len(mutations) == 1
        assert mutations[0].schema == "public"

    def test_if_not_exists_handled(self):
        """CREATE TABLE IF NOT EXISTS is correctly parsed as a create_table mutation."""
        content = (
            'SET ROLE "acme_admin";\n'
            'CREATE TABLE IF NOT EXISTS public.safe_create (id uuid);'
        )
        mutations = extract_table_mutations("test.sql", content)
        assert len(mutations) == 1
        assert mutations[0].mutation_type == "create_table"
        assert mutations[0].table == "safe_create"

    def test_source_file_recorded(self):
        """Every mutation carries the source_file path it was extracted from."""
        content = _read("good_schema_add_col.sql")
        mutations = extract_table_mutations("good_schema_add_col.sql", content)
        assert all(m.source_file == "good_schema_add_col.sql" for m in mutations)


# ---------------------------------------------------------------------------
# Deploy ordering (sort_sql_files)
# ---------------------------------------------------------------------------

class TestSortSqlFiles:
    """Tests for the deploy-order sort that enforces schema → type → function → config → serverless."""

    def _make_file(self, path: str, classification: str) -> SqlFile:
        """Build a minimal SqlFile with the given path and classification."""
        return SqlFile(
            relative_path=path,
            classification=classification,
            is_deleted=False,
            is_duplicate=False,
            duplicate_annotation=None,
        )

    def test_schema_before_type_before_function(self):
        """Files are reordered so schema precedes type, which precedes function."""
        files = [
            self._make_file("func/f.sql", "function"),
            self._make_file("types/t.sql", "type"),
            self._make_file("schema/s.sql", "schema"),
        ]
        sorted_files = sort_sql_files(files)
        classifications = [f.classification for f in sorted_files]
        assert classifications == ["schema", "type", "function"]

    def test_stable_within_group(self):
        """Files within the same classification group retain their original relative order."""
        files = [
            self._make_file("func/b.sql", "function"),
            self._make_file("func/a.sql", "function"),
            self._make_file("schema/s.sql", "schema"),
        ]
        sorted_files = sort_sql_files(files)
        func_files = [f for f in sorted_files if f.classification == "function"]
        assert [f.relative_path for f in func_files] == ["func/b.sql", "func/a.sql"]

    def test_config_after_function(self):
        """Config-classified files are placed after function files in deploy order."""
        files = [
            self._make_file("config/c.sql", "config"),
            self._make_file("func/f.sql", "function"),
        ]
        sorted_files = sort_sql_files(files)
        assert sorted_files[0].classification == "function"
        assert sorted_files[1].classification == "config"

    def test_serverless_last(self):
        """Serverless-classified files are placed last regardless of input order."""
        files = [
            self._make_file("serverless/s.sql", "serverless"),
            self._make_file("func/f.sql", "function"),
            self._make_file("schema/s.sql", "schema"),
        ]
        sorted_files = sort_sql_files(files)
        assert sorted_files[-1].classification == "serverless"

    def test_empty_list(self):
        """An empty input list returns an empty list without error."""
        assert sort_sql_files([]) == []

    def test_single_file_unchanged(self):
        """A single-element list is returned unchanged."""
        files = [self._make_file("func/f.sql", "function")]
        assert sort_sql_files(files) == files


# ---------------------------------------------------------------------------
# Full stage run (integration)
# ---------------------------------------------------------------------------

class TestRun:
    """Integration tests that exercise run() with real fixture files."""

    def _make_cfg(self, repo_root: str) -> MagicMock:
        """Return a MagicMock Config pointing at repo_root with serverless disabled."""
        cfg = MagicMock()
        cfg.git_repo_dataschema = repo_root
        cfg.git_repo_serverless = ""
        cfg.serverless_configured.return_value = False
        return cfg

    def _make_sql_file(self, name: str, classification: str) -> SqlFile:
        """Return a non-deleted, non-duplicate SqlFile with the given name and classification."""
        return SqlFile(
            relative_path=name,
            classification=classification,
            is_deleted=False,
            is_duplicate=False,
            duplicate_annotation=None,
        )

    def _make_manifest(self, sql_files: list[SqlFile]) -> Manifest:
        """Return a Manifest with the given sql_files and no initial failures or warnings."""
        return Manifest(
            commit_hash="abc123",
            jira_ticket="DEV-1042",
            ticket_number="1042",
            pr_number="PR-88",
            author="test",
            timestamp="2024-01-15T14:30:00Z",
            release_dir="/tmp/releases/1042",
            reports_dir="/tmp/releases/1042/reports",
            sql_files=sql_files,
            deleted_files=[],
            sql_changes=True,
            has_hard_fail=False,
            fail_reasons=[],
            warnings=[],
        )

    def test_good_function_passes(self):
        """A valid function fixture produces no hard fails and one extracted signature."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("good_function.sql", "function")]
        )
        updated, report = run(manifest, cfg)
        assert not updated.has_hard_fail
        assert report["hard_fails"] == []
        assert len(report["function_signatures"]) == 1

    def test_bad_no_setrole_hard_fails(self):
        """A file missing SET ROLE causes a hard fail mentioning SET ROLE."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("bad_no_setrole.sql", "function")]
        )
        updated, report = run(manifest, cfg)
        assert updated.has_hard_fail
        assert any("SET ROLE" in r for r in report["hard_fails"])

    def test_privilege_escalation_hard_fails(self):
        """A file containing GRANT causes a hard fail mentioning GRANT."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("bad_privilege_escalation.sql", "function")]
        )
        updated, report = run(manifest, cfg)
        assert updated.has_hard_fail
        assert any("GRANT" in r for r in report["hard_fails"])

    def test_ddl_in_function_file_hard_fails(self):
        """A function file containing DDL (CREATE TABLE) causes a hard fail."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("bad_ddl_in_function.sql", "function")]
        )
        updated, report = run(manifest, cfg)
        assert updated.has_hard_fail
        assert any("DDL" in r or "CREATE" in r for r in report["hard_fails"])

    def test_drop_in_schema_file_hard_fails(self):
        """A schema file containing a misplaced DROP causes a hard fail mentioning DROP."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("bad_drop_misplaced.sql", "schema")]
        )
        updated, report = run(manifest, cfg)
        assert updated.has_hard_fail
        assert any("DROP" in r for r in report["hard_fails"])

    def test_cascade_in_comment_warns_not_fails(self):
        """CASCADE outside a DROP context produces a warning but not a hard fail."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("cascade_warning.sql", "function")]
        )
        updated, report = run(manifest, cfg)
        assert not updated.has_hard_fail
        assert len(report["warnings"]) >= 1
        assert any("CASCADE" in w for w in report["warnings"])

    def test_good_schema_creates_table_mutations(self):
        """A valid schema fixture with CREATE TABLE yields one create_table mutation."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("good_schema_create_table.sql", "schema")]
        )
        updated, report = run(manifest, cfg)
        assert not updated.has_hard_fail
        assert len(report["table_mutations"]) == 1
        assert report["table_mutations"][0]["mutation_type"] == "create_table"

    def test_good_schema_add_col_creates_mutations(self):
        """A valid schema fixture with two ADD COLUMN statements yields two mutations."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("good_schema_add_col.sql", "schema")]
        )
        updated, report = run(manifest, cfg)
        assert not updated.has_hard_fail
        assert len(report["table_mutations"]) == 2

    def test_good_type_extracts_type_definition(self):
        """A valid type fixture yields one type definition with the correct name."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("good_type.sql", "type")]
        )
        updated, report = run(manifest, cfg)
        assert not updated.has_hard_fail
        assert len(report["type_definitions"]) == 1
        assert report["type_definitions"][0]["name"] == "order_status"

    def test_deploy_order_applied(self):
        """run() reorders sql_files so schema precedes type, which precedes function."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest([
            self._make_sql_file("good_function.sql", "function"),
            self._make_sql_file("good_type.sql", "type"),
            self._make_sql_file("good_schema_create_table.sql", "schema"),
        ])
        updated, report = run(manifest, cfg)
        classifications = [f.classification for f in updated.sql_files]
        schema_idx = classifications.index("schema")
        type_idx = classifications.index("type")
        func_idx = classifications.index("function")
        assert schema_idx < type_idx < func_idx

    def test_deleted_files_skipped(self):
        """Deleted files are not analysed; missing SET ROLE in a deleted file is not a hard fail."""
        cfg = self._make_cfg(FIXTURES_DIR)
        deleted = SqlFile(
            relative_path="bad_no_setrole.sql",
            classification="function",
            is_deleted=True,
            is_duplicate=False,
            duplicate_annotation=None,
        )
        manifest = self._make_manifest([deleted])
        updated, report = run(manifest, cfg)
        # Deleted file is skipped — no hard fail for missing SET ROLE.
        assert not updated.has_hard_fail

    def test_multiple_files_all_failures_collected(self):
        """Failures from multiple files are all collected rather than stopping on the first."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest([
            self._make_sql_file("bad_no_setrole.sql", "function"),
            self._make_sql_file("bad_privilege_escalation.sql", "function"),
        ])
        updated, report = run(manifest, cfg)
        assert updated.has_hard_fail
        # Both files contribute fails — at least one from each.
        assert len(report["hard_fails"]) >= 2

    def test_fail_reasons_appended_to_manifest(self):
        """Hard-fail reasons from static checks are appended to the returned Manifest."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("bad_no_setrole.sql", "function")]
        )
        updated, _ = run(manifest, cfg)
        assert len(updated.fail_reasons) >= 1

    def test_warnings_appended_to_manifest(self):
        """Warnings from static checks are appended to the returned Manifest."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("cascade_warning.sql", "function")]
        )
        updated, _ = run(manifest, cfg)
        assert len(updated.warnings) >= 1

    def test_report_contains_deploy_order(self):
        """The report dict includes a deploy_order key listing processed file names."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("good_function.sql", "function")]
        )
        _, report = run(manifest, cfg)
        assert "deploy_order" in report
        assert "good_function.sql" in report["deploy_order"]

    def test_missing_file_hard_fails(self):
        """A SqlFile whose path does not exist on disk is a hard fail with a readable message."""
        cfg = self._make_cfg(FIXTURES_DIR)
        manifest = self._make_manifest(
            [self._make_sql_file("nonexistent_file.sql", "function")]
        )
        updated, report = run(manifest, cfg)
        assert updated.has_hard_fail
        assert any("Cannot read" in r for r in report["hard_fails"])
