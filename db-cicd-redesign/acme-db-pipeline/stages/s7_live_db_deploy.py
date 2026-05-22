"""
Stage 7: Live dev DB deployment.

Runs ci_backend_db_release.py --target live --skip-git against the live dev DB
(no git pull, no test clone), restarts services, performs GraphQL health checks,
verifies no duplicate functions, validates audit alignment, and optionally
transitions the Jira ticket via the Jira REST API.

Only runs after Gate 2 human approval in CI. No automatic approval
bypass is implemented or permitted in this module.

post.sql ownership: ci_backend_db_release.py runs prep.sql, deploy.lst entries,
and post.sql as steps 1-3 of its deployment sequence. This stage must not
execute post.sql a second time — doing so is a double-execution bug.

Staging-live clone recreation is handled by s6b, which runs before this stage.

Hard fail conditions:
  - EC2_HOST or SSH_PRIVATE_KEY not configured.
  - ci_backend_db_release.py --target live exits non-zero.
  - Any service restart command exits non-zero.
  - All GraphQL health-check retries exhausted (API or CRM endpoint).
  - Duplicate functions detected in live DB after deploy.
  - SQL file count in live log does not match deploy.lst entries.
  - Audit table misalignment detected after deployment.

Jira ticket transition is attempted if JIRA_TOKEN, JIRA_BASE_URL, and
JIRA_TRANSITION_ID are all configured. A failure here does NOT hard fail the
stage — the deployment is complete regardless of Jira availability.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger
from pipeline.models import Manifest
from stages.s6_test_db_deploy import (  # noqa: E402
    _count_deploy_lst_entries,
    check_audit_alignment_from_mutations,
    check_duplicate_functions,
    parse_deploy_log,
)

# Module logger
#
# Live deployment logs are the permanent operational record after Gate 2
# approval, so command outcomes and verification details are logged explicitly.
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Release script (live pass)
# ---------------------------------------------------------------------------
#
# The live pass runs the existing release script against the dev DB while
# skipping git operations that Stage 5 already completed.

def _run_release_script_live(release_dir: str, cfg: Config, log_path: str) -> None:
    """Run ci_backend_db_release.py --target live on EC2 via SSH.

    DB_ROOT and hostname resolution are handled by ci_backend_db_release.py on
    EC2 (env var -> Secrets Manager -> fallback). The pipeline must not inject
    DB_ROOT via the SSH command.
    """
    if not cfg.ec2_host:
        log.error("HARD FAIL: EC2_HOST is not configured.")
        sys.exit(1)
    subprocess.run(["chmod", "600", cfg.ssh_private_key], check=True)
    ec2_dir = os.path.join(cfg.ec2_releases_dir, os.path.basename(release_dir))
    ec2_log = f"{ec2_dir}/reports/live-deploy.log"
    remote_cmd = (
        f"python3 /home/ubuntu/acme-db-pipeline/scripts/ci_backend_db_release.py"
        f" {ec2_dir} --target live --skip-git --log {ec2_log}"
    )
    cmd = [
        "ssh",
        "-i", cfg.ssh_private_key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{cfg.ec2_user}@{cfg.ec2_host}",
        remote_cmd,
    ]
    log.info("Running live deploy via SSH: %s@%s", cfg.ec2_user, cfg.ec2_host)
    with open(log_path, "w", encoding="utf-8") as log_fh:
        result = subprocess.run(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            timeout=600,
        )
    log.info("Exit code: %d  log: %s", result.returncode, log_path)
    if result.returncode != 0:
        log.error(
            "HARD FAIL: ci_backend_db_release.py (live) exited %d. See %s",
            result.returncode, log_path,
        )
        sys.exit(1)
    log.info("Live DB deployment completed successfully.")


# ---------------------------------------------------------------------------
# Service restarts
# ---------------------------------------------------------------------------
#
# Service restarts make application layers pick up database changes. Commands
# are configured externally because node topology differs by environment.

def run_service_restarts(cfg: Config) -> list[dict]:
    """Run CRM and API restart commands. Hard fails if any exit non-zero.

    Commands are read from CRM_RESTART_CMD and API_RESTART_CMD env vars.
    An empty command string means that restart is not configured — it is
    skipped with an INFO log rather than failing.

    Returns a list of result dicts recording each command's exit code.
    """
    results: list[dict] = []
    commands = [
        ("crm_restart", cfg.crm_restart_cmd),
        ("api_restart", cfg.api_restart_cmd),
    ]
    for label, cmd_str in commands:
        if not cmd_str:
            log.info("%s: not configured, skipping.", label)
            results.append({"label": label, "skipped": True})
            continue

        cmd = cmd_str.split()
        log.info("Running %s: %s", label, cmd_str)
        result = subprocess.run(cmd, capture_output=True, text=True)
        entry = {
            "label": label,
            "command": cmd_str,
            "returncode": result.returncode,
            "skipped": False,
        }
        results.append(entry)
        if result.returncode != 0:
            fail = (
                f"{label} exited {result.returncode}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            log.error("HARD FAIL: %s", fail)
            sys.exit(1)
        log.info("%s completed (exit 0).", label)

    return results


# ---------------------------------------------------------------------------
# GraphQL health checks
# ---------------------------------------------------------------------------
#
# Health checks prove that API-facing surfaces recovered after DB deployment and
# service restart. Retry policy is configurable to absorb transient startup time.

def _check_url(url: str, timeout: int = 10) -> int:
    """HTTP GET url and return the status code. Returns -1 on network error."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError):
        return -1


def run_health_checks(cfg: Config) -> list[dict]:
    """Perform GraphQL health checks against API and CRM endpoints.

    Each endpoint is retried up to HEALTH_CHECK_RETRIES times with
    HEALTH_CHECK_BACKOFF_SECONDS between attempts. Hard fails if any
    endpoint does not return HTTP 200 after all retries.

    Returns a list of result dicts — one per endpoint.
    """
    endpoints = [
        ("graphql_api", cfg.graphql_api_url),
        ("graphql_crm", cfg.graphql_crm_url),
    ]
    results: list[dict] = []
    for label, url in endpoints:
        retries = cfg.health_check_retries
        backoff = cfg.health_check_backoff_seconds
        last_status = -1

        for attempt in range(1, retries + 1):
            log.info("%s health check attempt %d/%d: %s", label, attempt, retries, url)
            last_status = _check_url(url)
            if last_status == 200:
                log.info("%s: OK (HTTP 200) on attempt %d.", label, attempt)
                break
            log.warning(
                "%s: HTTP %s on attempt %d. Waiting %ds before retry.",
                label, last_status, attempt, backoff,
            )
            if attempt < retries:
                time.sleep(backoff)

        success = last_status == 200
        results.append({
            "label": label,
            "url": url,
            "success": success,
            "last_status": last_status,
            "attempts": attempt,
        })
        if not success:
            fail = (
                f"{label} health check failed after {retries} attempt(s): "
                f"last HTTP status {last_status} for {url}"
            )
            log.error("HARD FAIL: %s", fail)
            sys.exit(1)

    return results


# ---------------------------------------------------------------------------
# Jira transition (optional, non-fatal)
# ---------------------------------------------------------------------------
#
# Jira transition is operational bookkeeping. It should be attempted after a
# successful deployment but must not retroactively fail a completed release.

def transition_jira_ticket(
    jira_ticket: str, cfg: Config
) -> dict:
    """POST a status transition to the Jira REST API.

    Returns a result dict with 'success', 'status_code', and 'detail'.
    Never raises — logs errors but does not hard fail.
    """
    result: dict = {
        "attempted": False,
        "success": False,
        "status_code": None,
        "detail": "",
    }

    if not all([cfg.jira_token, cfg.jira_base_url, cfg.jira_transition_id]):
        log.info(
            "Jira transition skipped: JIRA_TOKEN, JIRA_BASE_URL, or JIRA_TRANSITION_ID "
            "not configured."
        )
        result["detail"] = "not configured"
        return result

    url = f"{cfg.jira_base_url.rstrip('/')}/rest/api/2/issue/{jira_ticket}/transitions"
    payload = json.dumps({"transition": {"id": cfg.jira_transition_id}}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.jira_token}",
        },
    )
    result["attempted"] = True
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result["status_code"] = resp.status
            result["success"] = resp.status in (200, 204)
            result["detail"] = f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        result["status_code"] = exc.code
        result["detail"] = f"HTTP {exc.code}: {exc.reason}"
        log.warning("Jira transition returned HTTP %d for %s.", exc.code, jira_ticket)
    except (urllib.error.URLError, OSError) as exc:
        result["detail"] = str(exc)
        log.warning("Jira transition network error for %s: %s", jira_ticket, exc)

    if result["success"]:
        log.info("Jira ticket %s transitioned successfully.", jira_ticket)
    else:
        log.warning(
            "Jira transition did not complete for %s (%s). "
            "Deployment is not affected.",
            jira_ticket, result["detail"],
        )
    return result


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------
#
# Stage 7 performs the live dev deployment and writes the final evidence for
# Gate 3: logs, health checks, count verification, audit verification, and Jira
# transition status.

def run(
    manifest: Manifest,
    release_dir: str,
    table_mutations: list[dict],
    cfg: Config,
) -> dict:
    """Run Stage 7 live DB deployment.

    Assumes s6b has already recreated the staging live clone. ci_backend_db_release.py
    runs prep.sql, deploy.lst, and post.sql as part of its own sequence — this stage
    must not execute post.sql a second time. Returns a summary dict; exits non-zero
    on any hard fail.
    """
    reports_dir = os.path.join(release_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    summary: dict = {
        "jira_ticket": manifest.jira_ticket,
        "pr_number": manifest.pr_number,
        "deploy_log": "",
        "error_lines": [],
        "processed_file_count": 0,
        "expected_file_count": 0,
        "count_match": False,
        "service_restarts": [],
        "health_checks": [],
        "duplicate_functions": [],
        "audit_results": [],
        "jira_transition": {},
        "has_hard_fail": False,
        "fail_reasons": [],
    }

    deploy_log = os.path.join(reports_dir, "live-deploy.log")
    summary["deploy_log"] = deploy_log
    _run_release_script_live(release_dir, cfg, deploy_log)

    error_lines, processed_count = parse_deploy_log(deploy_log)
    summary["error_lines"] = error_lines
    summary["processed_file_count"] = processed_count
    if error_lines:
        log.warning(
            "%d error/fatal line(s) in live deploy log (surfaced in report):",
            len(error_lines),
        )
        for line in error_lines[:10]:
            log.warning("  %s", line)

    log.info("Running service restarts.")
    summary["service_restarts"] = run_service_restarts(cfg)

    log.info("Running GraphQL health checks.")
    health_results = run_health_checks(cfg)
    summary["health_checks"] = health_results

    health_json_path = os.path.join(reports_dir, "live-health-check.json")
    with open(health_json_path, "w", encoding="utf-8") as fh:
        json.dump(health_results, fh, indent=2)

    log.info("Checking for duplicate functions on live DB.")
    dup_rows = check_duplicate_functions(cfg)
    dup_list = [
        {"schema": row[0], "function": row[1], "count": row[2]}
        for row in dup_rows
    ]
    summary["duplicate_functions"] = dup_list
    if dup_list:
        fail = (
            "Duplicate functions in live DB after deploy: "
            + ", ".join(f"{d['schema']}.{d['function']}({d['count']})" for d in dup_list)
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("No duplicate functions on live DB.")

    expected = _count_deploy_lst_entries(release_dir)
    summary["expected_file_count"] = expected
    count_ok = processed_count == expected
    summary["count_match"] = count_ok

    count_verify_path = os.path.join(reports_dir, "live-count-verify.json")
    with open(count_verify_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"expected": expected, "processed": processed_count, "match": count_ok},
            fh,
            indent=2,
        )

    if not count_ok:
        fail = (
            f"SQL file count mismatch on live deploy: deploy.lst has {expected} entries "
            f"but log shows {processed_count} files processed."
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("Live file count verification passed (%d files).", expected)

    log.info("Verifying audit alignment on live DB for %d mutation(s).", len(table_mutations))
    audit_results = check_audit_alignment_from_mutations(table_mutations, cfg)
    summary["audit_results"] = audit_results

    audit_verify_path = os.path.join(reports_dir, "live-audit-verify.json")
    with open(audit_verify_path, "w", encoding="utf-8") as fh:
        json.dump(audit_results, fh, indent=2)

    misaligned = [r for r in audit_results if not r["aligned"]]
    if misaligned:
        tables = ", ".join(f"{r['schema']}.{r['table']}" for r in misaligned)
        fail = f"Audit table misalignment on live DB after deployment for: {tables}"
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)

    jira_result = transition_jira_ticket(manifest.jira_ticket, cfg)
    summary["jira_transition"] = jira_result

    log.info("Stage 7 complete. Live deployment finished.")
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
#
# The CLI is invoked only after Gate 2 approval. It loads release context,
# executes the live flow, writes live-deploy-summary.json, and exits non-zero on
# live deployment hard fails.

def main() -> None:
    """CLI entry point for Stage 7 live DB deployment."""
    parser = argparse.ArgumentParser(
        description="Stage 7: Live dev DB deployment."
    )
    parser.add_argument(
        "manifest_path",
        help="Path to raw-manifest.json from Stage 1.",
    )
    parser.add_argument(
        "static_report_path",
        help="Path to static-analysis-report.json from Stage 2.",
    )
    parser.add_argument(
        "--release-dir",
        help="Release directory. Defaults to $RELEASES_BASE_DIR/<jira_ticket>/",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging.")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    try:
        cfg = Config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    try:
        with open(args.manifest_path, encoding="utf-8") as fh:
            manifest = Manifest.from_dict(json.load(fh))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        log.error("Failed to load manifest: %s", exc)
        sys.exit(1)

    if not manifest.sql_changes:
        log.info("Passthrough release — no SQL changes. Skipping.")
        sys.exit(0)

    try:
        with open(args.static_report_path, encoding="utf-8") as fh:
            static_report = json.load(fh)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        log.error("Failed to load static report: %s", exc)
        sys.exit(1)

    table_mutations: list[dict] = static_report.get("table_mutations", [])

    if args.release_dir:
        release_dir = args.release_dir
    else:
        release_dir = manifest.release_dir or os.path.join(
            cfg.releases_base_dir, manifest.ticket_number or "unknown"
        )

    os.makedirs(release_dir, exist_ok=True)

    summary = run(manifest, release_dir, table_mutations, cfg)

    output_path = os.path.join(release_dir, "reports", "live-deploy-summary.json")
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        log.info("Summary written to %s", output_path)
    except OSError as exc:
        log.error("Failed to write summary: %s", exc)
        sys.exit(1)

    if summary.get("has_hard_fail"):
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
