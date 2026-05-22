"""
Stage 6: Test DB deployment.

Assumes the staging test clone is already ready (recreated by s6a). Invokes
ci_backend_db_release.py --target test --skip-git on EC2 via SSH, parses the
deploy log, verifies file counts, and audits table alignment.

post.sql ownership: ci_backend_db_release.py runs prep.sql, deploy.lst entries,
and post.sql as steps 1-3 of its deployment sequence. This stage must not
execute post.sql a second time — doing so is a double-execution bug.

Clone recreation is the responsibility of s6a. This stage must never call
recreate_test_db_clone.sh directly.

Hard fail conditions:
  - EC2_HOST or SSH_PRIVATE_KEY not configured.
  - ci_backend_db_release.py (--target test) exits non-zero.
  - Duplicate functions detected in test DB after deploy.
  - SQL file count in log does not match non-commented deploy.lst entries.
  - Audit table alignment is wrong after deployment.
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.db import readonly_connection
from pipeline.logger import configure_logging, get_logger
from pipeline.models import Manifest

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_QUERY_DUPLICATE_FUNCTIONS = """
SELECT nspname, proname, COUNT(*) AS cnt
FROM pg_proc
JOIN pg_namespace ON pg_namespace.oid = pg_proc.pronamespace
WHERE nspname NOT LIKE 'pg_%'
  AND nspname <> 'information_schema'
GROUP BY nspname, proname
HAVING COUNT(*) > 1
ORDER BY nspname, proname;
"""

_QUERY_TABLE_COLUMNS = """
SELECT attname
FROM pg_attribute
WHERE attrelid = (
    SELECT c.oid
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = %(table)s AND n.nspname = %(schema)s
)
AND attnum > 0 AND NOT attisdropped
ORDER BY attnum;
"""

# Lines matching ERROR or FATAL in the deploy log.
_ERROR_RE = re.compile(r"(?:^|\s)(?:ERROR|FATAL)[:\s]", re.IGNORECASE)
_PSQL_ERROR_RE = re.compile(r"psql:.*ERROR:", re.IGNORECASE)

# ci_backend_db_release.py emits "Running <basename>.sql" for each deploy.lst
# entry and "Executing ..." for prep.sql/post.sql, so this pattern counts only
# deploy.lst files without double-counting prep or post.
_FILE_PROCESSED_RE = re.compile(r"(?:Running\s+\S+\.sql|psql:\s*\S+\.sql:)", re.IGNORECASE)


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


def _build_ssh_cmd(cfg: Config, remote_cmd: str) -> list[str]:
    """Return an SSH command list for the configured EC2 host."""
    subprocess.run(["chmod", "600", cfg.ssh_private_key], check=True)
    return [
        "ssh",
        "-i", cfg.ssh_private_key,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        f"{cfg.ec2_user}@{cfg.ec2_host}",
        remote_cmd,
    ]


def _run_release_script(release_dir: str, cfg: Config, log_path: str) -> None:
    """Run ci_backend_db_release.py --target test on EC2 via SSH.

    DB_ROOT and hostname resolution are handled by ci_backend_db_release.py on
    EC2 (env var -> Secrets Manager -> fallback). The pipeline must not inject
    DB_ROOT via the SSH command.
    """
    if not cfg.ec2_host:
        log.error("HARD FAIL: EC2_HOST is not configured.")
        sys.exit(1)
    ec2_dir = os.path.join(cfg.ec2_releases_dir, os.path.basename(release_dir))
    ec2_log = f"{ec2_dir}/reports/test-deploy-pass1.log"
    remote_cmd = (
        f"python3 /home/ubuntu/acme-db-pipeline/scripts/ci_backend_db_release.py"
        f" {ec2_dir} --target test --skip-git --log {ec2_log}"
    )
    rc, _ = _run_script(_build_ssh_cmd(cfg, remote_cmd), log_path)
    if rc != 0:
        log.error("HARD FAIL: ci_backend_db_release.py (test) exited %d. See %s", rc, log_path)
        sys.exit(1)
    log.info("Test DB deployment completed successfully.")


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

def parse_deploy_log(log_path: str) -> tuple[list[str], int]:
    """Scan the deploy log for error lines and count processed SQL files.

    Returns (error_lines, processed_file_count).
    """
    error_lines: list[str] = []
    processed_count = 0
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                stripped = line.rstrip()
                if _ERROR_RE.search(stripped) or _PSQL_ERROR_RE.search(stripped):
                    error_lines.append(stripped)
                if _FILE_PROCESSED_RE.search(stripped):
                    processed_count += 1
    except OSError as exc:
        log.warning("Could not read deploy log %s: %s", log_path, exc)
    return error_lines, processed_count


def _count_deploy_lst_entries(release_dir: str) -> int:
    """Count non-commented, non-empty lines in deploy.lst."""
    deploy_lst = os.path.join(release_dir, "deploy.lst")
    count = 0
    try:
        with open(deploy_lst, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    count += 1
    except OSError as exc:
        log.warning("Could not read deploy.lst at %s: %s", deploy_lst, exc)
    return count


# ---------------------------------------------------------------------------
# DB checks (read-only)
# ---------------------------------------------------------------------------

def check_duplicate_functions(cfg: Config) -> list[tuple[str, str, int]]:
    """Return rows of (schema, function_name, count) for duplicated functions."""
    rows: list[tuple[str, str, int]] = []
    with readonly_connection(cfg) as conn:
        with conn.cursor() as cur:
            cur.execute(_QUERY_DUPLICATE_FUNCTIONS)
            rows = cur.fetchall()
        conn.rollback()
    return rows


def _get_table_columns(conn, schema: str, table: str) -> list[str]:
    """Return column names for schema.table from pg_attribute."""
    with conn.cursor() as cur:
        cur.execute(_QUERY_TABLE_COLUMNS, {"table": table, "schema": schema})
        return [row[0] for row in cur.fetchall()]


def check_audit_alignment_from_mutations(
    table_mutations: list[dict],
    cfg: Config,
) -> list[dict]:
    """Verify audit table column alignment for each entry in table_mutations.

    Returns a list of result dicts with schema, table, aligned, missing_in_audit,
    and extra_in_audit fields.
    """
    _AUDIT_EXTRA = {"audit_event", "audit_stamp", "audit_user_id"}

    seen: set[tuple[str, str]] = set()
    unique_mutations: list[dict] = []
    for m in table_mutations:
        key = (m["schema"], m["table"])
        if key not in seen:
            seen.add(key)
            unique_mutations.append(m)

    results: list[dict] = []
    with readonly_connection(cfg) as conn:
        for mutation in unique_mutations:
            schema = mutation["schema"]
            table = mutation["table"]
            audit_table = f"{schema}_{table}"

            base_cols = set(_get_table_columns(conn, schema, table))
            audit_cols = set(_get_table_columns(conn, "audit", audit_table))

            audit_data_cols = audit_cols - _AUDIT_EXTRA
            missing_in_audit = sorted(base_cols - audit_data_cols)
            extra_in_audit = sorted(audit_data_cols - base_cols)
            aligned = not missing_in_audit and not extra_in_audit

            result = {
                "schema": schema,
                "table": table,
                "audit_table": f"audit.{audit_table}",
                "aligned": aligned,
                "missing_in_audit": missing_in_audit,
                "extra_in_audit": extra_in_audit,
            }
            results.append(result)
            if aligned:
                log.info("Audit alignment OK: audit.%s", audit_table)
            else:
                log.error(
                    "HARD FAIL: Audit misalignment for audit.%s -- "
                    "missing: %s, extra: %s",
                    audit_table,
                    missing_in_audit,
                    extra_in_audit,
                )
        conn.rollback()

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
    """Run Stage 6 test DB deployment.

    Assumes s6a has already recreated the staging test clone. Does not invoke
    recreate_test_db_clone.sh. Returns a summary dict; exits non-zero on any
    hard fail.
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
        "duplicate_functions": [],
        "audit_results": [],
        "has_hard_fail": False,
        "fail_reasons": [],
    }

    # Deploy against the test clone. ci_backend_db_release.py runs prep.sql,
    # deploy.lst, and post.sql as part of its own sequence — s6 must not
    # execute any of those files again.
    deploy_log = os.path.join(reports_dir, "test-deploy-pass1.log")
    summary["deploy_log"] = deploy_log
    _run_release_script(release_dir, cfg, deploy_log)

    error_lines, processed_count = parse_deploy_log(deploy_log)
    summary["error_lines"] = error_lines
    summary["processed_file_count"] = processed_count
    if error_lines:
        log.warning(
            "%d error/fatal line(s) in deploy log (surfaced in report, not hard fail):",
            len(error_lines),
        )
        for line in error_lines[:10]:
            log.warning("  %s", line)

    log.info("Checking for duplicate functions on test DB.")
    dup_rows = check_duplicate_functions(cfg)
    dup_list = [
        {"schema": row[0], "function": row[1], "count": row[2]}
        for row in dup_rows
    ]
    summary["duplicate_functions"] = dup_list
    if dup_list:
        fail = (
            "Duplicate functions detected in test DB after deploy: "
            + ", ".join(f"{d['schema']}.{d['function']}({d['count']})" for d in dup_list)
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("No duplicate functions detected.")

    expected = _count_deploy_lst_entries(release_dir)
    summary["expected_file_count"] = expected
    count_ok = processed_count == expected
    summary["count_match"] = count_ok

    count_result = {
        "expected": expected,
        "processed": processed_count,
        "match": count_ok,
    }
    count_verify_path = os.path.join(reports_dir, "test-count-verify.json")
    with open(count_verify_path, "w", encoding="utf-8") as fh:
        json.dump(count_result, fh, indent=2)

    if not count_ok:
        fail = (
            f"SQL file count mismatch: deploy.lst has {expected} entries "
            f"but log shows {processed_count} files processed."
        )
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)
    log.info("File count verification passed (%d files).", expected)

    log.info("Verifying audit table alignment for %d mutation(s).", len(table_mutations))
    audit_results = check_audit_alignment_from_mutations(table_mutations, cfg)
    summary["audit_results"] = audit_results

    audit_verify_path = os.path.join(reports_dir, "test-audit-verify.json")
    with open(audit_verify_path, "w", encoding="utf-8") as fh:
        json.dump(audit_results, fh, indent=2)

    misaligned = [r for r in audit_results if not r["aligned"]]
    if misaligned:
        tables = ", ".join(f"{r['schema']}.{r['table']}" for r in misaligned)
        fail = f"Audit table misalignment after deployment for: {tables}"
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        sys.exit(1)

    log.info("Stage 6 complete. All checks passed.")
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for Stage 6 test DB deployment."""
    parser = argparse.ArgumentParser(
        description="Stage 6: Test DB deployment."
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

    output_path = os.path.join(release_dir, "reports", "test-deploy-summary.json")
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
