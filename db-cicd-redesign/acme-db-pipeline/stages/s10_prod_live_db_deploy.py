"""
Stage 10: Prod live DB deployment.

Runs ci_backend_db_release.py --target live --skip-git on the prod VM,
restarts services on all configured prod nodes, performs GraphQL health checks,
verifies no duplicate functions, validates audit alignment, and optionally
transitions the Jira ticket via the Jira REST API.

Infrastructure guard: if EC2_HOST_PROD is not set, exits 0 immediately.

Only runs after Gate 4 human approval. No automatic approval bypass is
implemented or permitted in this module.

post.sql ownership: ci_backend_db_release.py runs prep.sql, deploy.lst entries,
and post.sql as steps 1-3 of its deployment sequence. This stage must not
execute post.sql a second time — doing so is a double-execution bug.

Hard fail conditions:
  - EC2_HOST_PROD is set but EC2_RELEASES_DIR_PROD is not configured.
  - ci_backend_db_release.py --target live exits non-zero on prod VM.
  - Any prod node service restart exits non-zero.
  - All GraphQL health-check retries exhausted (API or CRM endpoint).
  - Duplicate functions detected in prod live DB after deploy.
  - SQL file count in prod live log does not match deploy.lst entries.
  - Audit table misalignment detected after deployment.

Jira ticket transition to deployed-to-production status is attempted if
JIRA_TOKEN and JIRA_BASE_URL are configured. A failure does NOT hard fail
the stage — the deployment is complete regardless of Jira availability.
"""

import argparse
import json
import os
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
from stages.s7_live_db_deploy import (  # noqa: E402
    _check_url,
    transition_jira_ticket,
)

log = get_logger(__name__)

_INFRA_GUARD_MSG = (
    "Production infrastructure not configured (EC2_HOST_PROD not set) — skipping."
)


# ---------------------------------------------------------------------------
# Release script (prod live pass)
# ---------------------------------------------------------------------------

def _run_release_script_prod(release_dir: str, cfg: Config, log_path: str) -> None:
    """Run ci_backend_db_release.py --target live on the prod VM via SSH."""
    if not cfg.ec2_releases_dir_prod:
        log.error("HARD FAIL: EC2_RELEASES_DIR_PROD is not configured.")
        sys.exit(1)
    subprocess.run(["chmod", "600", cfg.ssh_private_key], check=True)
    ec2_dir = os.path.join(cfg.ec2_releases_dir_prod, os.path.basename(release_dir))
    ec2_log = f"{ec2_dir}/reports/prod-live-deploy.log"
    remote_cmd = (
        f"python3 /home/ubuntu/acme-db-pipeline/scripts/ci_backend_db_release.py"
        f" {ec2_dir} --target live --skip-git --log {ec2_log}"
    )
    cmd = [
        "ssh",
        "-i", cfg.ssh_private_key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{cfg.ec2_user_prod}@{cfg.ec2_host_prod}",
        remote_cmd,
    ]
    log.info("Running prod live deploy via SSH: %s@%s", cfg.ec2_user_prod, cfg.ec2_host_prod)
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
            "HARD FAIL: ci_backend_db_release.py (live) exited %d on prod VM. See %s",
            result.returncode, log_path,
        )
        sys.exit(1)
    log.info("Prod live DB deployment completed successfully.")


# ---------------------------------------------------------------------------
# Prod node service restarts
# ---------------------------------------------------------------------------
#
# Each node in PROD_NODES receives an SSH command running crm_restart and
# api_restart to make application layers pick up the database changes. A failure
# on any node hard fails the stage because a partial restart leaves the cluster
# in an inconsistent state.

def run_prod_node_restarts(cfg: Config) -> list[dict]:
    """SSH to each node in cfg.prod_nodes and run crm_restart; api_restart.

    Returns a list of result dicts. Hard fails if any node exits non-zero.
    """
    if not cfg.prod_nodes:
        log.info("PROD_NODES not configured — skipping service restarts.")
        return [{"node": "none", "skipped": True}]

    subprocess.run(["chmod", "600", cfg.ssh_private_key], check=True)
    results: list[dict] = []
    for node in cfg.prod_nodes:
        cmd = [
            "ssh",
            "-i", cfg.ssh_private_key,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            f"{cfg.ec2_user_prod}@{node}",
            "crm_restart; api_restart",
        ]
        log.info("Running service restart on %s", node)
        result = subprocess.run(cmd, capture_output=True, text=True)
        entry = {
            "node": node,
            "returncode": result.returncode,
            "skipped": False,
        }
        results.append(entry)
        if result.returncode != 0:
            fail = (
                f"Service restart failed on {node}: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
            log.error("HARD FAIL: %s", fail)
            sys.exit(1)
        log.info("Service restart completed on %s.", node)

    return results


# ---------------------------------------------------------------------------
# Production GraphQL health checks
# ---------------------------------------------------------------------------

def run_prod_health_checks(cfg: Config) -> list[dict]:
    """Perform GraphQL health checks against production API and CRM endpoints.

    Uses the same retry policy as staging. Hard fails if any endpoint does not
    return HTTP 200 after all retries.
    """
    endpoints = [
        ("graphql_api_prod", cfg.graphql_api_url_prod),
        ("graphql_crm_prod", cfg.graphql_crm_url_prod),
    ]
    results: list[dict] = []
    for label, url in endpoints:
        if not url:
            log.info("%s: URL not configured, skipping.", label)
            results.append({"label": label, "url": "", "skipped": True, "success": True})
            continue

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
            "skipped": False,
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
# Stage runner
# ---------------------------------------------------------------------------

def run(
    manifest: Manifest,
    release_dir: str,
    table_mutations: list[dict],
    cfg: Config,
) -> dict:
    """Run Stage 10 prod live DB deployment.

    Only runs after Gate 4 approval. Returns a summary dict; exits non-zero
    on any hard fail.
    """
    if not cfg.ec2_host_prod:
        log.info(_INFRA_GUARD_MSG)
        return {"skipped": True, "reason": "EC2_HOST_PROD not set"}

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
        "node_restarts": [],
        "health_checks": [],
        "duplicate_functions": [],
        "audit_results": [],
        "jira_transition": {},
        "has_hard_fail": False,
        "fail_reasons": [],
    }

    deploy_log = os.path.join(reports_dir, "prod-live-deploy.log")
    summary["deploy_log"] = deploy_log
    _run_release_script_prod(release_dir, cfg, deploy_log)

    error_lines, processed_count = parse_deploy_log(deploy_log)
    summary["error_lines"] = error_lines
    summary["processed_file_count"] = processed_count
    if error_lines:
        log.warning(
            "%d error/fatal line(s) in prod live deploy log (surfaced in report):",
            len(error_lines),
        )
        for line in error_lines[:10]:
            log.warning("  %s", line)

    log.info("Running service restarts on prod nodes.")
    summary["node_restarts"] = run_prod_node_restarts(cfg)

    log.info("Running prod GraphQL health checks.")
    health_results = run_prod_health_checks(cfg)
    summary["health_checks"] = health_results

    health_json_path = os.path.join(reports_dir, "prod-live-health-check.json")
    with open(health_json_path, "w", encoding="utf-8") as fh:
        json.dump(health_results, fh, indent=2)

    log.info("Checking for duplicate functions on prod live DB.")
    dup_rows = check_duplicate_functions(cfg)
    dup_list = [
        {"schema": row[0], "function": row[1], "count": row[2]}
        for row in dup_rows
    ]
    summary["duplicate_functions"] = dup_list
    if dup_list:
        fail = (
            "Duplicate functions in prod live DB after deploy: "
            + ", ".join(f"{d['schema']}.{d['function']}({d['count']})" for d in dup_list)
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("No duplicate functions on prod live DB.")

    expected = _count_deploy_lst_entries(release_dir)
    summary["expected_file_count"] = expected
    count_ok = processed_count == expected
    summary["count_match"] = count_ok

    count_verify_path = os.path.join(reports_dir, "prod-live-count-verify.json")
    with open(count_verify_path, "w", encoding="utf-8") as fh:
        json.dump(
            {"expected": expected, "processed": processed_count, "match": count_ok},
            fh,
            indent=2,
        )

    if not count_ok:
        fail = (
            f"SQL file count mismatch on prod live deploy: deploy.lst has {expected} "
            f"entries but log shows {processed_count} files processed."
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("Prod live file count verification passed (%d files).", expected)

    log.info(
        "Verifying audit alignment on prod live DB for %d mutation(s).",
        len(table_mutations),
    )
    audit_results = check_audit_alignment_from_mutations(table_mutations, cfg)
    summary["audit_results"] = audit_results

    audit_verify_path = os.path.join(reports_dir, "prod-live-audit-verify.json")
    with open(audit_verify_path, "w", encoding="utf-8") as fh:
        json.dump(audit_results, fh, indent=2)

    misaligned = [r for r in audit_results if not r["aligned"]]
    if misaligned:
        tables = ", ".join(f"{r['schema']}.{r['table']}" for r in misaligned)
        fail = f"Audit table misalignment on prod live DB after deployment for: {tables}"
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)

    # Jira transition uses the prod-specific transition ID if configured,
    # falling back to the staging transition ID.
    effective_transition_id = cfg.jira_transition_id_prod or cfg.jira_transition_id
    jira_cfg = cfg
    if effective_transition_id != cfg.jira_transition_id:
        # Temporarily shadow jira_transition_id with the prod value.
        # transition_jira_ticket reads cfg.jira_transition_id, so we create a
        # minimal override without subclassing Config.
        class _ProdJiraCfg:
            def __getattr__(self, name: str):
                return getattr(jira_cfg, name)
        override = _ProdJiraCfg()
        override.jira_transition_id = effective_transition_id  # type: ignore[attr-defined]
        jira_result = transition_jira_ticket(manifest.jira_ticket, override)  # type: ignore[arg-type]
    else:
        jira_result = transition_jira_ticket(manifest.jira_ticket, cfg)

    summary["jira_transition"] = jira_result

    log.info("Stage 10 complete. Prod live deployment finished.")
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for Stage 10 prod live DB deployment."""
    parser = argparse.ArgumentParser(
        description="Stage 10: Prod live DB deployment."
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
        help="Release directory. Defaults to $RELEASES_BASE_DIR_PROD/<jira_ticket>/",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging.")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    try:
        cfg = Config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    if not cfg.ec2_host_prod:
        log.info(_INFRA_GUARD_MSG)
        sys.exit(0)

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
        base = cfg.releases_base_dir_prod or cfg.releases_base_dir
        release_dir = os.path.join(
            base, manifest.ticket_number or "unknown"
        )

    os.makedirs(release_dir, exist_ok=True)

    summary = run(manifest, release_dir, table_mutations, cfg)

    output_path = os.path.join(release_dir, "reports", "prod-live-deploy-summary.json")
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
