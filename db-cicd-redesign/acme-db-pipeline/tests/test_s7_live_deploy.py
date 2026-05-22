"""
Tests for stages/s7_live_db_deploy.py.

Unit tests mock all external calls (subprocess, urllib, DB). Integration tests
(marked skip_no_db) require a real PostgreSQL instance.
"""

import json
import os
import subprocess
import sys
import urllib.error
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.models import Manifest
from stages.s7_live_db_deploy import (
    _check_url,
    _run_release_script_live,
    run as s7_run,
    run_health_checks,
    run_service_restarts,
    transition_jira_ticket,
)

# ---------------------------------------------------------------------------
# DB availability guard
# ---------------------------------------------------------------------------
#
# Integration tests are skipped unless a real database is explicitly configured.
# Unit tests in this file mock DB and network boundaries and always run.

_REQUIRED_DB_VARS = ["DB_HOST", "DB_NAME", "DB_USER_READONLY", "DB_PASS_READONLY"]
_db_available = all(os.environ.get(v) for v in _REQUIRED_DB_VARS)
skip_no_db = pytest.mark.skipif(
    not _db_available,
    reason="Requires live PostgreSQL (set DB_HOST, DB_NAME, DB_USER_READONLY, DB_PASS_READONLY)",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
#
# Test helpers build minimal manifests/configs with sane defaults so individual
# tests only override the fields relevant to the behavior under test.

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


def _make_config(**kwargs) -> MagicMock:
    cfg = MagicMock()
    cfg.db_host = "localhost"
    cfg.db_port = 5432
    cfg.db_name = "testdb"
    cfg.ec2_host = "ec2.example.com"
    cfg.ec2_user = "ubuntu"
    cfg.ssh_private_key = "/tmp/key.pem"
    cfg.ec2_releases_dir = "/home/ubuntu/releases"
    cfg.crm_restart_cmd = ""
    cfg.api_restart_cmd = ""
    cfg.graphql_api_url = "http://api.test/graphiql"
    cfg.graphql_crm_url = "http://crm.test/graphiql"
    cfg.health_check_retries = 2
    cfg.health_check_backoff_seconds = 0
    cfg.jira_token = ""
    cfg.jira_base_url = ""
    cfg.jira_transition_id = ""
    for k, v in kwargs.items():
        setattr(cfg, k, v)
    return cfg


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


# ---------------------------------------------------------------------------
# _run_release_script_live
# ---------------------------------------------------------------------------

class TestRunReleaseScriptLive:
    def test_success(self, tmp_path):
        cfg = _make_config()
        log_path = str(tmp_path / "live.log")
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            _run_release_script_live(str(tmp_path), cfg, log_path)
        assert mock_run.called

    def test_live_and_skip_flags_passed(self, tmp_path):
        cfg = _make_config()
        log_path = str(tmp_path / "live.log")
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            _run_release_script_live("/some/release", cfg, log_path)
        # Last call is the SSH run; last element of cmd list is the remote command string.
        cmd = mock_run.call_args[0][0]
        remote_cmd_str = cmd[-1]
        assert "--target live" in remote_cmd_str
        assert "--skip-git" in remote_cmd_str

    def test_non_zero_exit_hard_fails(self, tmp_path):
        cfg = _make_config()
        log_path = str(tmp_path / "live.log")
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(1)
            with pytest.raises(SystemExit):
                _run_release_script_live(str(tmp_path), cfg, log_path)

    def test_unconfigured_ec2_host_hard_fails(self, tmp_path):
        cfg = _make_config(ec2_host="")
        log_path = str(tmp_path / "live.log")
        with pytest.raises(SystemExit):
            _run_release_script_live(str(tmp_path), cfg, log_path)


# ---------------------------------------------------------------------------
# run_service_restarts
# ---------------------------------------------------------------------------

class TestRunServiceRestarts:
    def test_both_skipped_when_unconfigured(self):
        cfg = _make_config(crm_restart_cmd="", api_restart_cmd="")
        results = run_service_restarts(cfg)
        assert all(r["skipped"] for r in results)
        assert len(results) == 2

    def test_runs_configured_command(self):
        cfg = _make_config(crm_restart_cmd="systemctl restart crm")
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            results = run_service_restarts(cfg)
        assert any(not r.get("skipped") and r["label"] == "crm_restart" for r in results)

    def test_non_zero_exit_hard_fails(self):
        cfg = _make_config(crm_restart_cmd="systemctl restart crm")
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(1, stderr="failed")
            with pytest.raises(SystemExit):
                run_service_restarts(cfg)

    def test_both_commands_run(self):
        cfg = _make_config(
            crm_restart_cmd="systemctl restart crm",
            api_restart_cmd="systemctl restart api",
        )
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            results = run_service_restarts(cfg)
        assert mock_run.call_count == 2
        labels = [r["label"] for r in results]
        assert "crm_restart" in labels
        assert "api_restart" in labels

    def test_result_records_returncode(self):
        cfg = _make_config(api_restart_cmd="systemctl restart api")
        with patch("stages.s7_live_db_deploy.subprocess.run") as mock_run:
            mock_run.return_value = _completed(0)
            results = run_service_restarts(cfg)
        api_result = next(r for r in results if r["label"] == "api_restart")
        assert api_result["returncode"] == 0


# ---------------------------------------------------------------------------
# _check_url
# ---------------------------------------------------------------------------

class TestCheckUrl:
    def test_returns_200_on_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("stages.s7_live_db_deploy.urllib.request.urlopen", return_value=mock_resp):
            assert _check_url("http://test") == 200

    def test_returns_status_code_on_http_error(self):
        with patch("stages.s7_live_db_deploy.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="http://test", code=503, msg="Service Unavailable",
                hdrs=None, fp=None,
            )
            assert _check_url("http://test") == 503

    def test_returns_minus_one_on_network_error(self):
        with patch("stages.s7_live_db_deploy.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("connection refused")
            assert _check_url("http://test") == -1


# ---------------------------------------------------------------------------
# run_health_checks
# ---------------------------------------------------------------------------

class TestRunHealthChecks:
    def test_both_ok(self):
        cfg = _make_config(
            graphql_api_url="http://api/graphiql",
            graphql_crm_url="http://crm/graphiql",
            health_check_retries=1,
        )
        with patch("stages.s7_live_db_deploy._check_url", return_value=200):
            results = run_health_checks(cfg)
        assert all(r["success"] for r in results)
        assert len(results) == 2

    def test_failure_after_retries_hard_fails(self):
        cfg = _make_config(health_check_retries=2, health_check_backoff_seconds=0)
        with patch("stages.s7_live_db_deploy._check_url", return_value=503), \
             patch("stages.s7_live_db_deploy.time.sleep"):
            with pytest.raises(SystemExit):
                run_health_checks(cfg)

    def test_succeeds_on_second_attempt(self):
        cfg = _make_config(health_check_retries=3, health_check_backoff_seconds=0)
        responses = [503, 200, 200]
        with patch("stages.s7_live_db_deploy._check_url", side_effect=responses), \
             patch("stages.s7_live_db_deploy.time.sleep"):
            results = run_health_checks(cfg)
        assert results[0]["success"] is True
        assert results[0]["attempts"] == 2

    def test_result_records_url(self):
        cfg = _make_config(
            graphql_api_url="http://api/graphiql",
            graphql_crm_url="http://crm/graphiql",
            health_check_retries=1,
        )
        with patch("stages.s7_live_db_deploy._check_url", return_value=200):
            results = run_health_checks(cfg)
        urls = [r["url"] for r in results]
        assert "http://api/graphiql" in urls
        assert "http://crm/graphiql" in urls

    def test_sleep_called_between_retries(self):
        cfg = _make_config(health_check_retries=2, health_check_backoff_seconds=5)
        with patch("stages.s7_live_db_deploy._check_url", return_value=503), \
             patch("stages.s7_live_db_deploy.time.sleep") as mock_sleep:
            with pytest.raises(SystemExit):
                run_health_checks(cfg)
        # sleep called once between the two attempts (before failure on 2nd)
        assert mock_sleep.called


# ---------------------------------------------------------------------------
# transition_jira_ticket
# ---------------------------------------------------------------------------

class TestTransitionJiraTicket:
    def test_skipped_when_not_configured(self):
        cfg = _make_config()
        result = transition_jira_ticket("DEV-42", cfg)
        assert result["attempted"] is False
        assert "not configured" in result["detail"]

    def test_success_on_204(self):
        cfg = _make_config(
            jira_token="token",
            jira_base_url="https://jira.example.com",
            jira_transition_id="21",
        )
        mock_resp = MagicMock()
        mock_resp.status = 204
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("stages.s7_live_db_deploy.urllib.request.urlopen", return_value=mock_resp):
            result = transition_jira_ticket("DEV-42", cfg)
        assert result["attempted"] is True
        assert result["success"] is True

    def test_http_error_does_not_hard_fail(self):
        cfg = _make_config(
            jira_token="token",
            jira_base_url="https://jira.example.com",
            jira_transition_id="21",
        )
        with patch("stages.s7_live_db_deploy.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.HTTPError(
                url="https://jira.example.com/...", code=401, msg="Unauthorized",
                hdrs=None, fp=None,
            )
            result = transition_jira_ticket("DEV-42", cfg)
        assert result["attempted"] is True
        assert result["success"] is False
        assert "401" in result["detail"]

    def test_network_error_does_not_hard_fail(self):
        cfg = _make_config(
            jira_token="token",
            jira_base_url="https://jira.example.com",
            jira_transition_id="21",
        )
        with patch("stages.s7_live_db_deploy.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("no route to host")
            result = transition_jira_ticket("DEV-42", cfg)
        assert result["success"] is False

    def test_partial_config_is_skipped(self):
        # Only JIRA_TOKEN set — should skip, not error.
        cfg = _make_config(jira_token="token", jira_base_url="", jira_transition_id="")
        result = transition_jira_ticket("DEV-42", cfg)
        assert result["attempted"] is False


# ---------------------------------------------------------------------------
# run() — orchestration
# ---------------------------------------------------------------------------

class TestS7Run:
    def _patch_all(
        self,
        tmp_path,
        *,
        dup_rows=None,
        audit_results_override=None,
    ):
        """Return list of patches that make run() succeed end-to-end.

        Patches the functions imported from s6 directly in s7's namespace so
        the readonly_connection binding in s6 is never exercised.
        """
        if dup_rows is None:
            dup_rows = []

        if audit_results_override is None:
            audit_results_override = [
                {
                    "schema": "public", "table": "orders",
                    "audit_table": "audit.public_orders",
                    "aligned": True, "missing_in_audit": [], "extra_in_audit": [],
                }
            ]

        def noop_release(release_dir, cfg, log_path):
            open(log_path, "w").close()

        restart_results = [
            {"label": "crm_restart", "skipped": True},
            {"label": "api_restart", "skipped": True},
        ]
        health_results = [
            {"label": "graphql_api", "url": "http://api", "success": True, "last_status": 200, "attempts": 1},
            {"label": "graphql_crm", "url": "http://crm", "success": True, "last_status": 200, "attempts": 1},
        ]
        jira_result = {"attempted": False, "success": False, "detail": "not configured", "status_code": None}

        from unittest.mock import patch as _patch

        return [
            _patch("stages.s7_live_db_deploy._run_release_script_live", side_effect=noop_release),
            _patch("stages.s7_live_db_deploy.parse_deploy_log", return_value=([], 2)),
            _patch("stages.s7_live_db_deploy._count_deploy_lst_entries", return_value=2),
            _patch("stages.s7_live_db_deploy.run_service_restarts", return_value=restart_results),
            _patch("stages.s7_live_db_deploy.run_health_checks", return_value=health_results),
            # Patch functions imported from s6 into s7's namespace directly.
            _patch("stages.s7_live_db_deploy.check_duplicate_functions", return_value=dup_rows),
            _patch("stages.s7_live_db_deploy.check_audit_alignment_from_mutations", return_value=audit_results_override),
            _patch("stages.s7_live_db_deploy.transition_jira_ticket", return_value=jira_result),
        ]

    def test_successful_run(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()
        mutations = [{"schema": "public", "table": "orders"}]

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path):
                stack.enter_context(p)
            summary = s7_run(manifest, str(tmp_path), mutations, cfg)

        assert summary["has_hard_fail"] is False
        assert summary["jira_ticket"] == "DEV-42"

    def test_duplicate_functions_hard_fail(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path, dup_rows=[("public", "get_user", 2)]):
                stack.enter_context(p)
            with pytest.raises(SystemExit):
                s7_run(manifest, str(tmp_path), [], cfg)

    def test_audit_misalignment_hard_fail(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()
        mutations = [{"schema": "public", "table": "orders"}]
        misaligned = [
            {
                "schema": "public", "table": "orders",
                "audit_table": "audit.public_orders",
                "aligned": False, "missing_in_audit": ["amount"], "extra_in_audit": [],
            }
        ]

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path, audit_results_override=misaligned):
                stack.enter_context(p)
            with pytest.raises(SystemExit):
                s7_run(manifest, str(tmp_path), mutations, cfg)

    def test_health_check_json_written(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path):
                stack.enter_context(p)
            s7_run(manifest, str(tmp_path), [], cfg)

        hc_path = tmp_path / "reports" / "live-health-check.json"
        assert hc_path.exists()
        data = json.loads(hc_path.read_text())
        assert isinstance(data, list)

    def test_audit_verify_json_written(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()
        mutations = [{"schema": "public", "table": "orders"}]

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path):
                stack.enter_context(p)
            s7_run(manifest, str(tmp_path), mutations, cfg)

        audit_path = tmp_path / "reports" / "live-audit-verify.json"
        assert audit_path.exists()

    def test_count_verify_json_written(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path):
                stack.enter_context(p)
            s7_run(manifest, str(tmp_path), [], cfg)

        cv_path = tmp_path / "reports" / "live-count-verify.json"
        assert cv_path.exists()
        data = json.loads(cv_path.read_text())
        assert data["match"] is True

    def test_count_mismatch_hard_fails(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()

        # Build patches but override the count/parse ones to create a mismatch.
        from contextlib import ExitStack
        from unittest.mock import patch as _patch
        patches = self._patch_all(tmp_path)
        # Replace parse_deploy_log and _count_deploy_lst_entries with mismatching values.
        patches = [
            p for p in patches
            if "parse_deploy_log" not in str(p) and "_count_deploy_lst_entries" not in str(p)
        ]
        patches += [
            _patch("stages.s7_live_db_deploy.parse_deploy_log", return_value=([], 1)),
            _patch("stages.s7_live_db_deploy._count_deploy_lst_entries", return_value=2),
        ]
        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with pytest.raises(SystemExit):
                s7_run(manifest, str(tmp_path), [], cfg)

    def test_jira_transition_called_with_ticket(self, tmp_path):
        manifest = _make_manifest(jira_ticket="DEV-99")
        cfg = _make_config()

        from contextlib import ExitStack
        from unittest.mock import patch as _patch, MagicMock
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path):
                stack.enter_context(p)
            with _patch("stages.s7_live_db_deploy.transition_jira_ticket") as mock_jira:
                mock_jira.return_value = {"attempted": False, "success": False, "detail": "not configured", "status_code": None}
                s7_run(manifest, str(tmp_path), [], cfg)
            mock_jira.assert_called_once_with("DEV-99", cfg)

    def test_summary_has_required_keys(self, tmp_path):
        manifest = _make_manifest()
        cfg = _make_config()

        from contextlib import ExitStack
        with ExitStack() as stack:
            for p in self._patch_all(tmp_path):
                stack.enter_context(p)
            summary = s7_run(manifest, str(tmp_path), [], cfg)

        required_keys = [
            "jira_ticket", "pr_number", "deploy_log",
            "health_checks", "duplicate_functions", "audit_results",
            "jira_transition", "has_hard_fail", "fail_reasons",
        ]
        for key in required_keys:
            assert key in summary, f"Missing key: {key}"
