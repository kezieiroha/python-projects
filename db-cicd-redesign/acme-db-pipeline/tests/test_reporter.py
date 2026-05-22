"""
Tests for pipeline/reporter.py.

All tests use tmp_path fixtures for release directory artefacts and mock
urllib to avoid any real network calls.
"""

import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.reporter import (
    _extract_pr_number,
    _parse_delete_file,
    _parse_deploy_lst,
    generate_gate1_report,
    generate_gate2_report,
    generate_gate3_report,
    post_pr_comment,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# Reporter tests build release artefacts in tmp_path so report generation can be
# exercised without running earlier pipeline stages or making network calls.

def _make_config(**kwargs) -> MagicMock:
    cfg = MagicMock()
    cfg.github_token = "test-token"
    cfg.github_repository = "acme/db-pipeline"
    cfg.github_api_url = "https://api.github.com"
    cfg.graphql_api_url = "http://api/graphiql"
    cfg.graphql_crm_url = "http://crm/graphiql"
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)


def _minimal_manifest(jira="DEV-42", pr="PR-99"):
    return {
        "commit_hash": "abc1234",
        "jira_ticket": jira,
        "pr_number": pr,
        "author": "Alice",
        "timestamp": "2024-01-15T12:00:00+00:00",
        "sql_files": [
            {
                "relative_path": "db/functions/get_user.sql",
                "classification": "function",
                "is_deleted": False,
                "is_duplicate": False,
                "duplicate_annotation": None,
            }
        ],
        "deleted_files": [],
        "has_hard_fail": False,
        "fail_reasons": [],
        "warnings": [],
    }


def _minimal_static():
    return {
        "stage": "s2_static_analysis",
        "hard_fails": [],
        "warnings": [],
        "function_signatures": [],
        "type_definitions": [],
        "table_mutations": [],
        "deploy_order": ["db/functions/get_user.sql"],
    }


def _minimal_db():
    return {
        "stage": "s3_db_analysis",
        "hard_fails": [],
        "warnings": [],
        "signature_deltas": [],
        "drop_order": [],
        "cascade_victims": [],
        "audit_gaps": [],
        "duplicate_functions": [],
    }


# ---------------------------------------------------------------------------
# _extract_pr_number
# ---------------------------------------------------------------------------

class TestExtractPrNumber:
    def test_pr_dash_number(self):
        assert _extract_pr_number("PR-99") == 99

    def test_plain_number(self):
        assert _extract_pr_number("42") == 42

    def test_number_at_end(self):
        assert _extract_pr_number("pull-request-123") == 123

    def test_no_number_returns_none(self):
        assert _extract_pr_number("no-numbers-here") is None

    def test_empty_string_returns_none(self):
        assert _extract_pr_number("") is None


# ---------------------------------------------------------------------------
# _parse_deploy_lst
# ---------------------------------------------------------------------------

class TestParseDeployLst:
    def test_active_and_commented(self, tmp_path):
        (tmp_path / "deploy.lst").write_text(
            "# DEV-42 PR-99\n"
            "db/schema/foo.sql\n"
            "db/functions/bar.sql\n"
            "# db/functions/dup.sql --> duplicate\n"
            "\n"
        )
        active, commented = _parse_deploy_lst(str(tmp_path))
        assert active == ["db/schema/foo.sql", "db/functions/bar.sql"]
        assert len(commented) == 1
        assert "dup.sql" in commented[0]

    def test_missing_file_returns_empty(self, tmp_path):
        active, commented = _parse_deploy_lst(str(tmp_path))
        assert active == []
        assert commented == []

    def test_group_comment_not_included(self, tmp_path):
        (tmp_path / "deploy.lst").write_text("# DEV-42 PR-99\ndb/schema/foo.sql\n")
        active, commented = _parse_deploy_lst(str(tmp_path))
        assert active == ["db/schema/foo.sql"]
        # group comment has no .sql path embedded so not in commented
        assert commented == []


# ---------------------------------------------------------------------------
# _parse_delete_file
# ---------------------------------------------------------------------------

class TestParseDeleteFile:
    def test_returns_paths(self, tmp_path):
        (tmp_path / "delete").write_text("# DEV-42 PR-99\ndb/functions/old.sql\n")
        paths = _parse_delete_file(str(tmp_path))
        assert paths == ["db/functions/old.sql"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert _parse_delete_file(str(tmp_path)) == []

    def test_empty_lines_excluded(self, tmp_path):
        (tmp_path / "delete").write_text("\n\ndb/functions/old.sql\n\n")
        paths = _parse_delete_file(str(tmp_path))
        assert paths == ["db/functions/old.sql"]


# ---------------------------------------------------------------------------
# generate_gate1_report
# ---------------------------------------------------------------------------

class TestGenerateGate1Report:
    def _write_artefacts(self, tmp_path, manifest=None, static=None, db=None):
        rpt = tmp_path / "reports"
        rpt.mkdir(exist_ok=True)
        _write_json(rpt / "raw-manifest.json", manifest or _minimal_manifest())
        _write_json(rpt / "static-analysis-report.json", static or _minimal_static())
        _write_json(rpt / "db-analysis-report.json", db or _minimal_db())

    def test_contains_jira_and_pr(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "DEV-42" in report
        assert "PR-99" in report

    def test_summary_line_format(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "Analysis complete for" in report
        assert "check(s) passed" in report
        assert "warning(s)" in report
        assert "hard fail(s)" in report

    def test_manifest_section_lists_files(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "db/functions/get_user.sql" in report
        assert "function" in report

    def test_no_drop_message_when_no_deltas(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "No DROP statements required" in report

    def test_drop_listed_when_deltas_present(self, tmp_path):
        db = _minimal_db()
        db["signature_deltas"] = [
            {
                "schema": "public",
                "name": "get_user",
                "old_args": "uuid",
                "old_result": "text",
                "new_param_types": ["uuid", "text"],
                "new_return_type": "text",
                "source_file": "db/functions/get_user.sql",
                "drop_statement": "DROP FUNCTION IF EXISTS public.get_user(uuid) CASCADE;",
                "reason": "Signature changed: was (uuid) RETURNS text, now (uuid, text) RETURNS text",
            }
        ]
        self._write_artefacts(tmp_path, db=db)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "public.get_user" in report
        assert "DROP FUNCTION IF EXISTS" in report

    def test_no_audit_actions_message_when_clean(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "No audit actions required" in report

    def test_audit_gap_listed(self, tmp_path):
        db = _minimal_db()
        db["audit_gaps"] = [
            {
                "schema": "public",
                "table": "orders",
                "gap_type": "missing_audit_table",
                "columns_to_add": [],
                "requires_rebuild": False,
            }
        ]
        self._write_artefacts(tmp_path, db=db)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "public.orders" in report
        assert "audit.public_orders" in report

    def test_deploy_lst_section(self, tmp_path):
        self._write_artefacts(tmp_path)
        (tmp_path / "deploy.lst").write_text("db/functions/get_user.sql\n")
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "deploy.lst" in report
        assert "db/functions/get_user.sql" in report

    def test_duplicate_entry_noted(self, tmp_path):
        self._write_artefacts(tmp_path)
        (tmp_path / "deploy.lst").write_text(
            "db/functions/get_user.sql\n"
            "# db/functions/dup_get_user.sql --> duplicate\n"
        )
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "dup_get_user.sql" in report

    def test_delete_section_no_files(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "No files to delete" in report

    def test_delete_section_lists_file(self, tmp_path):
        self._write_artefacts(tmp_path)
        (tmp_path / "delete").write_text("db/functions/old.sql\n")
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "db/functions/old.sql" in report

    def test_cascade_clean_message(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "CLEAN" in report

    def test_cascade_victims_listed(self, tmp_path):
        db = _minimal_db()
        db["cascade_victims"] = [
            {"object_name": "public.dependant_fn", "object_type": "function", "in_release_scope": True},
            {"object_name": "public.orphan_fn", "object_type": "function", "in_release_scope": False},
        ]
        self._write_artefacts(tmp_path, db=db)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "public.dependant_fn" in report
        assert "OUT OF SCOPE" in report
        assert "IN SCOPE" in report

    def test_warnings_listed(self, tmp_path):
        db = _minimal_db()
        db["warnings"] = ["db/functions/get_user.sql:42 — CASCADE detected"]
        self._write_artefacts(tmp_path, db=db)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "CASCADE detected" in report

    def test_no_warnings_message(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "No warnings" in report

    def test_hard_fails_listed(self, tmp_path):
        static = _minimal_static()
        static["hard_fails"] = ["SET ROLE missing in db/functions/bad.sql"]
        self._write_artefacts(tmp_path, static=static)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "SET ROLE missing" in report

    def test_no_hard_fails_message(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "No hard fails" in report

    def test_missing_artefacts_graceful(self, tmp_path):
        # No artefact files written — should not raise, just produce sparse report.
        report = generate_gate1_report(str(tmp_path), _make_config())
        assert "Analysis complete for" in report


# ---------------------------------------------------------------------------
# generate_gate2_report
# ---------------------------------------------------------------------------

class TestGenerateGate2Report:
    def _write_artefacts(self, tmp_path, *, pass_=True):
        rpt = tmp_path / "reports"
        rpt.mkdir(exist_ok=True)
        _write_json(rpt / "raw-manifest.json", _minimal_manifest())
        _write_json(rpt / "test-deploy-summary.json", {
            "jira_ticket": "DEV-42",
            "pr_number": "PR-99",
            "has_hard_fail": not pass_,
            "fail_reasons": [] if pass_ else ["count mismatch"],
            "duplicate_functions": [],
            "audit_results": [],
            "expected_file_count": 1,
            "processed_file_count": 1,
            "count_match": pass_,
        })
        _write_json(rpt / "test-count-verify.json", {
            "expected": 1,
            "processed": 1,
            "match": pass_,
        })
        with open(rpt / "test-audit-verify.json", "w") as fh:
            json.dump([], fh)
        (rpt / "test-deploy-pass1.log").write_text("Running db/functions/get_user.sql\nDone.\n")

    def test_pass_label(self, tmp_path):
        self._write_artefacts(tmp_path, pass_=True)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "PASS" in report

    def test_fail_label(self, tmp_path):
        self._write_artefacts(tmp_path, pass_=False)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "FAIL" in report

    def test_jira_in_header(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "DEV-42" in report

    def test_count_verification_section(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "Expected: 1" in report
        assert "MATCH" in report

    def test_duplicate_check_clean(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "CLEAN" in report

    def test_audit_verification_no_mutations(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "No table mutations" in report

    def test_audit_aligned_table(self, tmp_path):
        self._write_artefacts(tmp_path)
        _write_json(tmp_path / "reports" / "test-audit-verify.json", [
            {"schema": "public", "table": "orders",
             "audit_table": "audit.public_orders",
             "aligned": True, "missing_in_audit": [], "extra_in_audit": []}
        ])
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "ALIGNED" in report

    def test_audit_misaligned_table(self, tmp_path):
        self._write_artefacts(tmp_path)
        _write_json(tmp_path / "reports" / "test-audit-verify.json", [
            {"schema": "public", "table": "orders",
             "audit_table": "audit.public_orders",
             "aligned": False, "missing_in_audit": ["email"], "extra_in_audit": []}
        ])
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "MISALIGNED" in report
        assert "email" in report

    def test_log_excerpt_included(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "Running db/functions/get_user.sql" in report

    def test_log_truncated_at_50_lines(self, tmp_path):
        self._write_artefacts(tmp_path)
        lines = [f"line {i}\n" for i in range(100)]
        (tmp_path / "reports" / "test-deploy-pass1.log").write_text("".join(lines))
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "line 49" in report
        assert "line 50" not in report

    def test_fail_reasons_listed(self, tmp_path):
        self._write_artefacts(tmp_path, pass_=False)
        report = generate_gate2_report(str(tmp_path), _make_config())
        assert "count mismatch" in report


# ---------------------------------------------------------------------------
# generate_gate3_report
# ---------------------------------------------------------------------------

class TestGenerateGate3Report:
    def _write_artefacts(self, tmp_path, *, pass_=True):
        rpt = tmp_path / "reports"
        rpt.mkdir(exist_ok=True)
        _write_json(rpt / "raw-manifest.json", _minimal_manifest())
        _write_json(rpt / "live-deploy-summary.json", {
            "jira_ticket": "DEV-42",
            "pr_number": "PR-99",
            "has_hard_fail": not pass_,
            "fail_reasons": [],
            "duplicate_functions": [],
            "jira_transition": {"attempted": False, "success": False, "detail": "not configured", "status_code": None},
        })
        with open(rpt / "live-health-check.json", "w") as fh:
            json.dump([
                {"label": "graphql_api", "url": "http://api/graphiql", "success": True, "last_status": 200, "attempts": 1},
                {"label": "graphql_crm", "url": "http://crm/graphiql", "success": True, "last_status": 200, "attempts": 1},
            ], fh)
        with open(rpt / "live-audit-verify.json", "w") as fh:
            json.dump([], fh)

    def test_pass_label(self, tmp_path):
        self._write_artefacts(tmp_path, pass_=True)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "PASS" in report

    def test_fail_label(self, tmp_path):
        self._write_artefacts(tmp_path, pass_=False)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "FAIL" in report

    def test_health_checks_listed(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "graphql_api" in report
        assert "HTTP 200" in report
        assert "graphql_crm" in report

    def test_duplicate_check_clean(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "CLEAN" in report

    def test_jira_not_configured(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "not configured" in report

    def test_jira_success(self, tmp_path):
        self._write_artefacts(tmp_path)
        summary = json.loads((tmp_path / "reports" / "live-deploy-summary.json").read_text())
        summary["jira_transition"] = {"attempted": True, "success": True, "status_code": 204, "detail": "HTTP 204"}
        _write_json(tmp_path / "reports" / "live-deploy-summary.json", summary)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "transitioned successfully" in report
        assert "HTTP 204" in report

    def test_jira_failed(self, tmp_path):
        self._write_artefacts(tmp_path)
        summary = json.loads((tmp_path / "reports" / "live-deploy-summary.json").read_text())
        summary["jira_transition"] = {"attempted": True, "success": False, "status_code": 401, "detail": "HTTP 401: Unauthorized"}
        _write_json(tmp_path / "reports" / "live-deploy-summary.json", summary)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "transition failed" in report
        assert "401" in report

    def test_artefacts_section_lists_files(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "Artefacts" in report

    def test_no_table_mutations(self, tmp_path):
        self._write_artefacts(tmp_path)
        report = generate_gate3_report(str(tmp_path), _make_config())
        assert "No table mutations" in report


# ---------------------------------------------------------------------------
# post_pr_comment
# ---------------------------------------------------------------------------

class TestPostPrComment:
    def test_success_returns_dict(self):
        cfg = _make_config()
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("pipeline.reporter.urllib.request.urlopen", return_value=mock_resp):
            result = post_pr_comment("## Report", 42, cfg)
        assert result["success"] is True
        assert result["status_code"] == 201

    def test_http_error_returns_failure(self):
        cfg = _make_config()
        with patch("pipeline.reporter.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="https://api.github.com/...", code=403, msg="Forbidden",
                hdrs=None, fp=None,
            )
            result = post_pr_comment("## Report", 42, cfg)
        assert result["success"] is False
        assert result["status_code"] == 403

    def test_network_error_returns_failure(self):
        cfg = _make_config()
        with patch("pipeline.reporter.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("connection refused")
            result = post_pr_comment("## Report", 42, cfg)
        assert result["success"] is False
        assert result["status_code"] is None

    def test_repository_in_url(self):
        cfg = _make_config(github_repository="acme/other-pipeline")
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("pipeline.reporter.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            post_pr_comment("report", 10, cfg)
        req = mock_open.call_args[0][0]
        assert "acme/other-pipeline" in req.full_url

    def test_authorization_header_set(self):
        cfg = _make_config(github_token="my-secret-token")
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("pipeline.reporter.urllib.request.urlopen", return_value=mock_resp) as mock_open:
            post_pr_comment("report", 10, cfg)
        req = mock_open.call_args[0][0]
        assert req.get_header("Authorization") == "Bearer my-secret-token"
