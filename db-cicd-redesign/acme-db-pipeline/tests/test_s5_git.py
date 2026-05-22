"""
Tests for stages/s1_discovery.py and stages/s5_git_promotion.py.

s1 tests use unittest.mock.patch to avoid any real git subprocess calls.
s5 tests mock subprocess.run at the module level so promote_repo exercises the
full control-flow logic without touching the filesystem or a real git repo.
"""

import json
import os
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.models import Manifest, SqlFile
from stages.s1_discovery import (
    classify_file,
    detect_duplicates,
    get_commit_info,
    get_diff_files,
    parse_commit_message,
    run as s1_run,
)
from stages.s5_git_promotion import (
    promote_repo,
    run as s5_run,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# Discovery and promotion tests share Manifest, SqlFile, and CompletedProcess
# builders so git subprocess behavior can be modeled without touching real
# repositories.

def _make_sql_file(path: str, classification: str = "function", deleted: bool = False) -> SqlFile:
    return SqlFile(
        relative_path=path,
        classification=classification,
        is_deleted=deleted,
        is_duplicate=False,
        duplicate_annotation=None,
    )


def _make_manifest(**kwargs) -> Manifest:
    defaults = dict(
        commit_hash="abc1234",
        jira_ticket="DEV-42",
        ticket_number="42",
        pr_number="PR-99",
        author="Alice",
        timestamp="2024-01-15T12:00:00+00:00",
        release_dir="",
        reports_dir="",
        sql_files=[],
        deleted_files=[],
        sql_changes=True,
        has_hard_fail=False,
        fail_reasons=[],
        warnings=[],
    )
    defaults.update(kwargs)
    return Manifest(**defaults)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    result = MagicMock(spec=subprocess.CompletedProcess)
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ---------------------------------------------------------------------------
# Stage 1 — classify_file
# ---------------------------------------------------------------------------
#
# Classification tests pin the path rules that later static analysis and
# deploy-order generation depend on.

class TestClassifyFile:
    def test_schema_path(self):
        assert classify_file("db/schema/2024_01_01_add_orders.sql") == "schema"

    def test_schema_filename_pattern(self):
        assert classify_file("migrations/2024_06_30_alter_table.sql") == "schema"

    def test_schema_filename_pattern_no_path_marker(self):
        assert classify_file("2024_01_01_create_payments.sql") == "schema"

    def test_function_postgraphile(self):
        assert classify_file("db/postgraphile/get_user.sql") == "function"

    def test_function_functions(self):
        assert classify_file("db/functions/calculate_fee.sql") == "function"

    def test_type_path(self):
        assert classify_file("db/types/order_status.sql") == "type"

    def test_config_fallback(self):
        assert classify_file("db/config/seed_data.sql") == "config"

    def test_config_no_path_markers(self):
        assert classify_file("roles.sql") == "config"

    def test_windows_separator_schema(self):
        assert classify_file("db\\schema\\2024_01_01_foo.sql") == "schema"

    def test_windows_separator_function(self):
        assert classify_file("db\\functions\\foo.sql") == "function"

    def test_schema_pattern_not_mid_filename(self):
        # basename starts with non-digit; does not match _MIGRATION_FILENAME_RE
        assert classify_file("db/other/init_2024_01_01_table.sql") == "config"


# ---------------------------------------------------------------------------
# Stage 1 — parse_commit_message
# ---------------------------------------------------------------------------

class TestParseCommitMessage:
    def test_both_present(self):
        msg = "DEV-42 PR-99 fix the thing"
        ticket, pr, fails = parse_commit_message(msg, r"DEV-\d+", r"PR-\d+")
        assert ticket == "DEV-42"
        assert pr == "PR-99"
        assert fails == []

    def test_jira_missing(self):
        msg = "PR-99 fix the thing (no jira)"
        ticket, pr, fails = parse_commit_message(msg, r"DEV-\d+", r"PR-\d+")
        assert ticket == ""
        assert pr == "PR-99"
        assert len(fails) == 1
        assert "Jira" in fails[0]

    def test_pr_missing(self):
        msg = "DEV-42 fix the thing (no pr)"
        ticket, pr, fails = parse_commit_message(msg, r"DEV-\d+", r"PR-\d+")
        assert ticket == "DEV-42"
        assert pr == ""
        assert len(fails) == 1
        assert "PR number" in fails[0]

    def test_both_missing(self):
        msg = "fix the thing"
        ticket, pr, fails = parse_commit_message(msg, r"DEV-\d+", r"PR-\d+")
        assert ticket == ""
        assert pr == ""
        assert len(fails) == 2

    def test_pattern_in_fail_message(self):
        msg = "fix without refs"
        jira_pat = r"DEV-\d+"
        _, _, fails = parse_commit_message(msg, jira_pat, r"PR-\d+")
        # The fail message embeds repr(pattern); check that the base ticket prefix is present.
        assert any("DEV-" in f for f in fails)

    def test_multiline_message(self):
        msg = "DEV-100 PR-200\n\nDetails about the fix."
        ticket, pr, fails = parse_commit_message(msg, r"DEV-\d+", r"PR-\d+")
        assert ticket == "DEV-100"
        assert pr == "PR-200"
        assert fails == []


# ---------------------------------------------------------------------------
# Stage 1 — get_diff_files
# ---------------------------------------------------------------------------

class TestGetDiffFiles:
    def test_added_file(self):
        output = "A\tdb/functions/foo.sql\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert result == [("A", "db/functions/foo.sql")]

    def test_modified_file(self):
        output = "M\tdb/schema/2024_01_01_alter.sql\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert result == [("M", "db/schema/2024_01_01_alter.sql")]

    def test_deleted_file(self):
        output = "D\tdb/functions/old.sql\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert result == [("D", "db/functions/old.sql")]

    def test_renamed_file_emits_delete_and_add(self):
        output = "R100\told/path.sql\tnew/path.sql\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert ("D", "old/path.sql") in result
        assert ("A", "new/path.sql") in result

    def test_copied_file_emits_delete_and_add(self):
        output = "C100\tsrc.sql\tdst.sql\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert ("D", "src.sql") in result
        assert ("A", "dst.sql") in result

    def test_non_sql_file_included(self):
        # get_diff_files returns all files; filtering is in run()
        output = "A\tREADME.md\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert result == [("A", "README.md")]

    def test_empty_lines_ignored(self):
        output = "A\tdb/functions/foo.sql\n\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert len(result) == 1

    def test_mixed_status_codes(self):
        output = "A\tdb/functions/new.sql\nM\tdb/functions/updated.sql\nD\tdb/functions/old.sql\n"
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout=output)
            result = get_diff_files("/repo", "abc123")
        assert len(result) == 3


# ---------------------------------------------------------------------------
# Stage 1 — get_commit_info
# ---------------------------------------------------------------------------

class TestGetCommitInfo:
    def test_returns_three_values(self):
        with patch("stages.s1_discovery.subprocess.run") as mock_run:
            mock_run.return_value = _completed(stdout="Alice\n")
            with patch("stages.s1_discovery._run_git") as mock_git:
                mock_git.side_effect = [
                    "Alice",
                    "2024-01-15T12:00:00+00:00",
                    "DEV-42 PR-99 message",
                ]
                author, ts, msg = get_commit_info("/repo", "abc123")
        assert author == "Alice"
        assert ts == "2024-01-15T12:00:00+00:00"
        assert msg == "DEV-42 PR-99 message"

    def test_strips_whitespace(self):
        with patch("stages.s1_discovery._run_git") as mock_git:
            mock_git.side_effect = [
                "  Bob  ",
                "  2024-06-01T08:00:00+00:00  ",
                "  DEV-1 PR-1 msg  ",
            ]
            author, ts, msg = get_commit_info("/repo", "deadbeef")
        assert author == "Bob"
        assert ts == "2024-06-01T08:00:00+00:00"
        assert msg == "DEV-1 PR-1 msg"


# ---------------------------------------------------------------------------
# Stage 1 — detect_duplicates
# ---------------------------------------------------------------------------

class TestDetectDuplicates:
    def test_no_duplicates(self):
        files = [
            _make_sql_file("a/foo.sql"),
            _make_sql_file("b/bar.sql"),
        ]
        result = detect_duplicates(files)
        assert all(not f.is_duplicate for f in result)

    def test_duplicate_flagged(self):
        files = [
            _make_sql_file("a/foo.sql"),
            _make_sql_file("b/foo.sql"),
        ]
        result = detect_duplicates(files)
        assert not result[0].is_duplicate
        assert result[1].is_duplicate
        assert "a/foo.sql" in result[1].duplicate_annotation

    def test_deleted_files_not_flagged(self):
        files = [
            _make_sql_file("a/foo.sql", deleted=True),
            _make_sql_file("b/foo.sql"),
        ]
        result = detect_duplicates(files)
        assert all(not f.is_duplicate for f in result)

    def test_triple_duplicate(self):
        files = [
            _make_sql_file("a/foo.sql"),
            _make_sql_file("b/foo.sql"),
            _make_sql_file("c/foo.sql"),
        ]
        result = detect_duplicates(files)
        assert not result[0].is_duplicate
        assert result[1].is_duplicate
        assert result[2].is_duplicate
        assert "a/foo.sql" in result[1].duplicate_annotation
        assert "a/foo.sql" in result[2].duplicate_annotation

    def test_different_filenames_not_duplicate(self):
        files = [
            _make_sql_file("a/foo.sql"),
            _make_sql_file("a/bar.sql"),
        ]
        result = detect_duplicates(files)
        assert all(not f.is_duplicate for f in result)

    def test_deleted_canonical_not_considered(self):
        # First occurrence is deleted; second should become canonical, not a duplicate.
        files = [
            _make_sql_file("a/foo.sql", deleted=True),
            _make_sql_file("b/foo.sql"),
            _make_sql_file("c/foo.sql"),
        ]
        result = detect_duplicates(files)
        assert not result[1].is_duplicate
        assert result[2].is_duplicate
        assert "b/foo.sql" in result[2].duplicate_annotation


# ---------------------------------------------------------------------------
# Stage 1 — run()
# ---------------------------------------------------------------------------

class TestS1Run:
    def _make_config(self, tmp_path):
        cfg = MagicMock()
        cfg.git_repo_dataschema = "/repo/dataschema"
        cfg.git_repo_serverless = None
        cfg.releases_base_dir = str(tmp_path)
        cfg.jira_ticket_pattern = r"DEV-\d+"
        cfg.pr_number_pattern = r"PR-\d+"
        cfg.serverless_configured.return_value = False
        return cfg

    def test_successful_discovery(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "DEV-42 PR-99 fix")
            mock_df.return_value = [("A", "db/functions/foo.sql")]
            manifest, fails = s1_run("abc123", cfg)
        assert fails == []
        assert manifest.jira_ticket == "DEV-42"
        assert manifest.pr_number == "PR-99"
        assert len(manifest.sql_files) == 1
        assert manifest.sql_files[0].classification == "function"

    def test_no_sql_files_is_hard_fail(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "DEV-42 PR-99 fix")
            mock_df.return_value = [("A", "README.md")]
            manifest, fails = s1_run("abc123", cfg)
        assert manifest.has_hard_fail
        assert any("No SQL files" in f for f in fails)

    def test_missing_jira_is_hard_fail(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "PR-99 fix no jira")
            mock_df.return_value = [("A", "db/functions/foo.sql")]
            manifest, fails = s1_run("abc123", cfg)
        assert manifest.has_hard_fail
        assert any("Jira" in f for f in fails)

    def test_missing_pr_is_hard_fail(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "DEV-42 fix no pr")
            mock_df.return_value = [("A", "db/functions/foo.sql")]
            manifest, fails = s1_run("abc123", cfg)
        assert manifest.has_hard_fail
        assert any("PR number" in f for f in fails)

    def test_commit_resolve_failure_is_hard_fail(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci:
            mock_ci.side_effect = subprocess.CalledProcessError(
                128, "git", stderr="bad object abc123"
            )
            manifest, fails = s1_run("abc123", cfg)
        assert manifest.has_hard_fail
        assert any("Failed to resolve commit" in f for f in fails)

    def test_deleted_files_collected(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "DEV-42 PR-99 fix")
            mock_df.return_value = [
                ("D", "db/functions/old.sql"),
                ("A", "db/functions/new.sql"),
            ]
            manifest, fails = s1_run("abc123", cfg)
        assert "db/functions/old.sql" in manifest.deleted_files
        assert not manifest.has_hard_fail

    def test_serverless_files_classified_serverless(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg.git_repo_serverless = "/repo/serverless"
        cfg.serverless_configured.return_value = True
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "DEV-42 PR-99 fix")
            # dataschema returns one SQL file, serverless repo returns another
            mock_df.side_effect = [
                [("A", "db/functions/foo.sql")],
                [("A", "functions/serverless_fn.sql")],
            ]
            manifest, fails = s1_run("abc123", cfg)
        serverless_files = [f for f in manifest.sql_files if f.classification == "serverless"]
        assert len(serverless_files) == 1

    def test_intra_commit_duplicate_flagged(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Alice", "2024-01-15T12:00:00+00:00", "DEV-42 PR-99 fix")
            mock_df.return_value = [
                ("A", "a/functions/foo.sql"),
                ("A", "b/functions/foo.sql"),
            ]
            manifest, fails = s1_run("abc123", cfg)
        duplicates = [f for f in manifest.sql_files if f.is_duplicate]
        assert len(duplicates) == 1

    def test_manifest_fields_populated(self, tmp_path):
        cfg = self._make_config(tmp_path)
        with patch("stages.s1_discovery.get_commit_info") as mock_ci, \
             patch("stages.s1_discovery.get_diff_files") as mock_df:
            mock_ci.return_value = ("Bob", "2024-03-10T09:00:00+00:00", "DEV-7 PR-3 update")
            mock_df.return_value = [("A", "db/types/my_type.sql")]
            manifest, _ = s1_run("deadbeef", cfg)
        assert manifest.commit_hash == "deadbeef"
        assert manifest.author == "Bob"
        assert manifest.timestamp == "2024-03-10T09:00:00+00:00"


# ---------------------------------------------------------------------------
# Stage 5 — promote_repo
# ---------------------------------------------------------------------------

class TestPromoteRepo:
    """Unit tests for promote_repo().

    Patches _run_git (which raises CalledProcessError on failure) and
    _run_git_result (which returns CompletedProcess) directly so the test
    controls each git call independently without depending on subprocess internals.
    """

    def _git_ok(self, stdout: str = "") -> str:
        """Successful _run_git return value (stdout string)."""
        return stdout

    def _git_fail(self, stderr: str = "error") -> subprocess.CalledProcessError:
        """Simulate _run_git failure by raising CalledProcessError."""
        return subprocess.CalledProcessError(1, "git", stderr=stderr)

    def _cp_ok(self) -> subprocess.CompletedProcess:
        return _completed(0)

    def _cp_fail(self, stderr: str = "conflict") -> subprocess.CompletedProcess:
        return _completed(1, stderr=stderr)

    def _patch(self, run_git_effects, run_git_result_effects):
        """Context manager patching both _run_git and _run_git_result."""
        from contextlib import ExitStack

        def make_side_effect(effects):
            it = iter(effects)

            def side_effect(*args, **kwargs):
                val = next(it)
                if isinstance(val, Exception):
                    raise val
                return val

            return side_effect

        return (
            patch(
                "stages.s5_git_promotion._run_git",
                side_effect=make_side_effect(run_git_effects),
            ),
            patch(
                "stages.s5_git_promotion._run_git_result",
                side_effect=make_side_effect(run_git_result_effects),
            ),
        )

    def test_successful_promotion_no_deletes(self):
        run_git_fx = ["", "", "newsha\n", ""]       # checkout, pull, rev-parse, push
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            result = promote_repo(
                repo_root="/repo",
                commit_hash="abc123",
                target_branch="staging",
                delete_paths=[],
                jira_ticket="DEV-42",
                pr_number="PR-99",
            )
        assert result["cherry_pick_hash"] == "newsha"
        assert result["deleted_files"] == []

    def test_checkout_failure_exits(self):
        run_git_fx = [self._git_fail("branch not found")]
        p1, p2 = self._patch(run_git_fx, [])
        with p1, p2:
            with pytest.raises(SystemExit):
                promote_repo("/repo", "abc123", "staging", [], "DEV-42", "PR-99")

    def test_pull_failure_exits(self):
        run_git_fx = ["", self._git_fail("network error")]  # checkout ok, pull fails
        p1, p2 = self._patch(run_git_fx, [])
        with p1, p2:
            with pytest.raises(SystemExit):
                promote_repo("/repo", "abc123", "staging", [], "DEV-42", "PR-99")

    def test_cherry_pick_conflict_aborts_and_exits(self):
        run_git_fx = ["", ""]                                       # checkout, pull
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_fail("conflict"), self._cp_ok()]  # fetch, parent-count, cp fails, abort ok
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            with pytest.raises(SystemExit):
                promote_repo("/repo", "abc123", "staging", [], "DEV-42", "PR-99")

    def test_push_failure_exits(self):
        # checkout, pull, rev-parse, push-fails
        run_git_fx = ["", "", "sha\n", self._git_fail("rejected")]
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            with pytest.raises(SystemExit):
                promote_repo("/repo", "abc123", "staging", [], "DEV-42", "PR-99")

    def test_delete_paths_removed_and_pushed(self, tmp_path):
        fake_file = tmp_path / "db" / "functions" / "old.sql"
        fake_file.parent.mkdir(parents=True)
        fake_file.write_text("-- old")

        # checkout, pull, rev-parse, push(cherry-pick), rm, commit, push(delete)
        run_git_fx = ["", "", "sha\n", "", "", "", ""]
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            result = promote_repo(
                repo_root=str(tmp_path),
                commit_hash="abc123",
                target_branch="staging",
                delete_paths=["db/functions/old.sql"],
                jira_ticket="DEV-42",
                pr_number="PR-99",
            )
        assert "db/functions/old.sql" in result["deleted_files"]

    def test_missing_delete_path_skipped(self, tmp_path):
        # File does not exist on disk — should warn and skip, not fail.
        run_git_fx = ["", "", "sha\n", ""]  # checkout, pull, rev-parse, push
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            result = promote_repo(
                repo_root=str(tmp_path),
                commit_hash="abc123",
                target_branch="staging",
                delete_paths=["nonexistent/file.sql"],
                jira_ticket="DEV-42",
                pr_number="PR-99",
            )
        assert result["deleted_files"] == []

    def test_delete_commit_push_failure_exits(self, tmp_path):
        fake_file = tmp_path / "old.sql"
        fake_file.write_text("-- old")

        # checkout, pull, rev-parse, push(cp), rm, commit, push(delete)-fails
        run_git_fx = ["", "", "sha\n", "", "", "", self._git_fail("rejected")]
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            with pytest.raises(SystemExit):
                promote_repo(
                    repo_root=str(tmp_path),
                    commit_hash="abc123",
                    target_branch="staging",
                    delete_paths=["old.sql"],
                    jira_ticket="DEV-42",
                    pr_number="PR-99",
                )

    def test_result_records_target_branch(self):
        run_git_fx = ["", "", "sha\n", ""]
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            result = promote_repo("/repo", "abc123", "production", [], "DEV-1", "PR-1")
        assert result["target_branch"] == "production"

    def test_result_records_original_hash(self):
        run_git_fx = ["", "", "newsha\n", ""]
        run_git_result_fx = [self._cp_ok(), self._cp_ok(), self._cp_ok()]  # fetch, parent-count, cherry-pick
        p1, p2 = self._patch(run_git_fx, run_git_result_fx)
        with p1, p2:
            result = promote_repo("/repo", "deadbeef", "staging", [], "DEV-1", "PR-1")
        assert result["original_hash"] == "deadbeef"


# ---------------------------------------------------------------------------
# Stage 5 — run()
# ---------------------------------------------------------------------------

class TestS5Run:
    def _make_config(self, tmp_path):
        cfg = MagicMock()
        cfg.git_repo_dataschema = "/repo/dataschema"
        cfg.git_repo_serverless = None
        cfg.releases_base_dir = str(tmp_path)
        cfg.serverless_configured.return_value = False
        return cfg

    def test_run_calls_promote_repo(self, tmp_path):
        cfg = self._make_config(tmp_path)
        manifest = _make_manifest(
            jira_ticket="DEV-10",
            pr_number="PR-5",
            sql_files=[_make_sql_file("db/functions/foo.sql", "function")],
        )
        with patch("stages.s5_git_promotion.promote_repo") as mock_promote:
            mock_promote.return_value = {
                "repo": "/repo/dataschema",
                "original_hash": "abc123",
                "cherry_pick_hash": "def456",
                "target_branch": "staging",
                "promotion_timestamp": "2024-01-15T12:00:00+00:00",
                "deleted_files": [],
            }
            summary = s5_run(manifest, "staging", cfg)
        assert mock_promote.called
        assert summary["target_branch"] == "staging"
        assert len(summary["promotions"]) == 1

    def test_serverless_repo_promoted_when_serverless_files_present(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg.git_repo_serverless = "/repo/serverless"
        cfg.serverless_configured.return_value = True
        manifest = _make_manifest(
            jira_ticket="DEV-10",
            pr_number="PR-5",
            sql_files=[
                _make_sql_file("db/functions/foo.sql", "function"),
                _make_sql_file("functions/sl_fn.sql", "serverless"),
            ],
        )
        promo_return = {
            "repo": "",
            "original_hash": "abc123",
            "cherry_pick_hash": "def456",
            "target_branch": "staging",
            "promotion_timestamp": "2024-01-15T12:00:00+00:00",
            "deleted_files": [],
        }
        with patch("stages.s5_git_promotion.promote_repo") as mock_promote:
            mock_promote.return_value = promo_return
            summary = s5_run(manifest, "staging", cfg)
        assert mock_promote.call_count == 2

    def test_serverless_repo_skipped_when_no_serverless_files(self, tmp_path):
        cfg = self._make_config(tmp_path)
        cfg.git_repo_serverless = "/repo/serverless"
        cfg.serverless_configured.return_value = True
        manifest = _make_manifest(
            jira_ticket="DEV-10",
            pr_number="PR-5",
            sql_files=[_make_sql_file("db/functions/foo.sql", "function")],
        )
        promo_return = {
            "repo": "",
            "original_hash": "abc123",
            "cherry_pick_hash": "def456",
            "target_branch": "staging",
            "promotion_timestamp": "2024-01-15T12:00:00+00:00",
            "deleted_files": [],
        }
        with patch("stages.s5_git_promotion.promote_repo") as mock_promote:
            mock_promote.return_value = promo_return
            summary = s5_run(manifest, "staging", cfg)
        assert mock_promote.call_count == 1

    def test_delete_paths_read_from_delete_file(self, tmp_path):
        cfg = self._make_config(tmp_path)
        release_dir = tmp_path / "DEV-10"
        release_dir.mkdir()
        delete_file = release_dir / "delete"
        delete_file.write_text(
            "# DEV-10 PR-5\ndb/functions/old.sql\ndb/types/old_type.sql\n"
        )
        manifest = _make_manifest(
            jira_ticket="DEV-10",
            pr_number="PR-5",
            sql_files=[_make_sql_file("db/functions/new.sql", "function")],
        )
        promo_return = {
            "repo": "",
            "original_hash": "abc123",
            "cherry_pick_hash": "def456",
            "target_branch": "staging",
            "promotion_timestamp": "2024-01-15T12:00:00+00:00",
            "deleted_files": ["db/functions/old.sql", "db/types/old_type.sql"],
        }
        with patch("stages.s5_git_promotion.promote_repo") as mock_promote:
            mock_promote.return_value = promo_return
            s5_run(manifest, "staging", cfg)
        call_kwargs = mock_promote.call_args
        passed_deletes = call_kwargs.kwargs.get("delete_paths") or call_kwargs.args[3]
        assert "db/functions/old.sql" in passed_deletes
        assert "db/types/old_type.sql" in passed_deletes

    def test_summary_structure(self, tmp_path):
        cfg = self._make_config(tmp_path)
        manifest = _make_manifest(
            jira_ticket="DEV-55",
            pr_number="PR-12",
            sql_files=[_make_sql_file("db/functions/foo.sql")],
        )
        promo_return = {
            "repo": "/repo",
            "original_hash": "abc",
            "cherry_pick_hash": "def",
            "target_branch": "staging",
            "promotion_timestamp": "2024-01-15T12:00:00+00:00",
            "deleted_files": [],
        }
        with patch("stages.s5_git_promotion.promote_repo") as mock_promote:
            mock_promote.return_value = promo_return
            summary = s5_run(manifest, "staging", cfg)
        assert "jira_ticket" in summary
        assert "pr_number" in summary
        assert "original_commit_hash" in summary
        assert "target_branch" in summary
        assert "promotions" in summary
        assert summary["jira_ticket"] == "DEV-55"
