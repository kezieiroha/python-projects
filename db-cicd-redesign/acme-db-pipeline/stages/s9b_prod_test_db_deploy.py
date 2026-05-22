"""
Stage 9b: Prod test DB deployment.

Assumes the prod test clone is already ready (recreated by s9a). Invokes
ci_backend_db_release.py --target test --skip-git on the prod VM via SSH,
parses the deploy log, verifies file counts, and audits table alignment.

Infrastructure guard: if EC2_HOST_PROD is not set, exits 0 immediately.

post.sql ownership: ci_backend_db_release.py runs prep.sql, deploy.lst entries,
and post.sql as steps 1-3 of its deployment sequence. This stage must not
execute post.sql a second time — doing so is a double-execution bug.

Hard fail conditions:
  - EC2_HOST_PROD is set but EC2_RELEASES_DIR_PROD is not configured.
  - ci_backend_db_release.py (--target test) exits non-zero on prod VM.
  - Duplicate functions detected in prod test DB after deploy.
  - SQL file count in log does not match non-commented deploy.lst entries.
  - Audit table alignment is wrong after deployment.
"""

import argparse
import json
import os
import subprocess
import sys

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

log = get_logger(__name__)

_INFRA_GUARD_MSG = (
    "Production infrastructure not configured (EC2_HOST_PROD not set) — skipping."
)


# ---------------------------------------------------------------------------
# Script runner
# ---------------------------------------------------------------------------

def _run_script(
    cmd: list[str],
    log_path: str,
    timeout: int = 600,
) -> tuple[int, str]:
    """Run a subprocess, capture stdout+stderr to log_path, return (returncode, log_path)."""
    log.info("Running: %s", " ".join(cmd))
    with open(log_path, "w", encoding="utf-8") as log_fh:
        result = subprocess.run(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    log.info("Exit code: %d  log: %s", result.returncode, log_path)
    return result.returncode, log_path


def _build_ssh_cmd_prod(cfg: Config, remote_cmd: str) -> list[str]:
    """Return an SSH command list for the configured prod EC2 host."""
    subprocess.run(["chmod", "600", cfg.ssh_private_key], check=True)
    return [
        "ssh",
        "-i", cfg.ssh_private_key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{cfg.ec2_user_prod}@{cfg.ec2_host_prod}",
        remote_cmd,
    ]


def _run_release_script(release_dir: str, cfg: Config, log_path: str) -> None:
    """Run ci_backend_db_release.py --target test on the prod VM via SSH."""
    if not cfg.ec2_releases_dir_prod:
        log.error("HARD FAIL: EC2_RELEASES_DIR_PROD is not configured.")
        sys.exit(1)
    ec2_dir = os.path.join(cfg.ec2_releases_dir_prod, os.path.basename(release_dir))
    ec2_log = f"{ec2_dir}/reports/prod-test-deploy.log"
    remote_cmd = (
        f"python3 /home/ubuntu/acme-db-pipeline/scripts/ci_backend_db_release.py"
        f" {ec2_dir} --target test --skip-git --log {ec2_log}"
    )
    rc, _ = _run_script(_build_ssh_cmd_prod(cfg, remote_cmd), log_path)
    if rc != 0:
        log.error(
            "HARD FAIL: ci_backend_db_release.py (test) exited %d on prod VM. See %s",
            rc, log_path,
        )
        sys.exit(1)
    log.info("Prod test DB deployment completed successfully.")


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------

def run(
    manifest: Manifest,
    release_dir: str,
    table_mutations: list[dict],
    cfg: Config,
) -> dict:
    """Run Stage 9b prod test DB deployment.

    Assumes s9a has already recreated the prod test clone. Returns a summary
    dict; exits non-zero on any hard fail.
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
        "duplicate_functions": [],
        "audit_results": [],
        "has_hard_fail": False,
        "fail_reasons": [],
    }

    deploy_log = os.path.join(reports_dir, "prod-test-deploy.log")
    summary["deploy_log"] = deploy_log
    _run_release_script(release_dir, cfg, deploy_log)

    error_lines, processed_count = parse_deploy_log(deploy_log)
    summary["error_lines"] = error_lines
    summary["processed_file_count"] = processed_count
    if error_lines:
        log.warning(
            "%d error/fatal line(s) in prod test deploy log (surfaced in report):",
            len(error_lines),
        )
        for line in error_lines[:10]:
            log.warning("  %s", line)

    log.info("Checking for duplicate functions on prod test DB.")
    dup_rows = check_duplicate_functions(cfg)
    dup_list = [
        {"schema": row[0], "function": row[1], "count": row[2]}
        for row in dup_rows
    ]
    summary["duplicate_functions"] = dup_list
    if dup_list:
        fail = (
            "Duplicate functions detected in prod test DB after deploy: "
            + ", ".join(f"{d['schema']}.{d['function']}({d['count']})" for d in dup_list)
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("No duplicate functions detected on prod test DB.")

    expected = _count_deploy_lst_entries(release_dir)
    summary["expected_file_count"] = expected
    count_ok = processed_count == expected
    summary["count_match"] = count_ok

    count_result = {"expected": expected, "processed": processed_count, "match": count_ok}
    count_verify_path = os.path.join(reports_dir, "prod-test-count-verify.json")
    with open(count_verify_path, "w", encoding="utf-8") as fh:
        json.dump(count_result, fh, indent=2)

    if not count_ok:
        fail = (
            f"SQL file count mismatch on prod test deploy: deploy.lst has {expected} "
            f"entries but log shows {processed_count} files processed."
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("Prod test file count verification passed (%d files).", expected)

    log.info(
        "Verifying audit alignment on prod test DB for %d mutation(s).",
        len(table_mutations),
    )
    audit_results = check_audit_alignment_from_mutations(table_mutations, cfg)
    summary["audit_results"] = audit_results

    audit_verify_path = os.path.join(reports_dir, "prod-test-audit-verify.json")
    with open(audit_verify_path, "w", encoding="utf-8") as fh:
        json.dump(audit_results, fh, indent=2)

    misaligned = [r for r in audit_results if not r["aligned"]]
    if misaligned:
        tables = ", ".join(f"{r['schema']}.{r['table']}" for r in misaligned)
        fail = f"Audit table misalignment on prod test DB after deployment for: {tables}"
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)

    log.info("Stage 9b complete. All prod test DB checks passed.")
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for Stage 9b prod test DB deployment."""
    parser = argparse.ArgumentParser(
        description="Stage 9b: Prod test DB deployment."
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

    output_path = os.path.join(release_dir, "reports", "prod-test-deploy-summary.json")
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
