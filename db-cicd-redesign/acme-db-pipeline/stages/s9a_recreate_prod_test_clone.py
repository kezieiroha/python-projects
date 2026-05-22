"""
Stage 9a: Recreate prod test DB clone.

Runs before s9b. Creates a fresh prod test DB clone from prod-live by invoking
recreate_test_db_clone.sh on the prod VM via SSH.

Infrastructure guard: if EC2_HOST_PROD is not set, exits 0 immediately.

Operations:
1. Infrastructure guard.
2. If SKIP_PROD_TEST_CLONE_RECREATE is true, log reason and exit 0.
3. SSH to prod VM (EC2_HOST_PROD), run script at RECREATE_TEST_DB_SCRIPT.
   HARD FAIL if exit code is non-zero.
4. Log completion timestamp and write clone-recreate-prod-test.json to reports/.

Hard fail conditions:
  - EC2_HOST_PROD is set but SSH_PRIVATE_KEY is not configured.
  - RECREATE_TEST_DB_SCRIPT is not configured.
  - recreate_test_db_clone.sh exits non-zero on the prod VM.
"""

import argparse
import datetime
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger
from pipeline.models import Manifest

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
) -> int:
    """Run a subprocess, capture stdout+stderr to log_path, return returncode."""
    log.info("Running: %s", " ".join(cmd))
    with open(log_path, "w", encoding="utf-8") as log_fh:
        result = subprocess.run(
            cmd,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    log.info("Exit code: %d  log: %s", result.returncode, log_path)
    return result.returncode


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


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------

def run(manifest: Manifest, release_dir: str, cfg: Config) -> dict:
    """Run Stage 9a: recreate the prod test DB clone.

    Returns a summary dict; exits non-zero on hard fail.
    """
    if not cfg.ec2_host_prod:
        log.info(_INFRA_GUARD_MSG)
        return {"skipped": True, "status": "skipped", "reason": "EC2_HOST_PROD not set"}

    reports_dir = os.path.join(release_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    summary: dict = {
        "jira_ticket": manifest.jira_ticket,
        "pr_number": manifest.pr_number,
        "skipped": False,
        "status": "unknown",
        "timestamp": timestamp,
        "has_hard_fail": False,
        "fail_reasons": [],
    }

    if cfg.skip_prod_test_clone_recreate:
        log.info("SKIP_PROD_TEST_CLONE_RECREATE is set -- using existing prod test clone.")
        summary["skipped"] = True
        summary["status"] = "skipped"
        return summary

    if not cfg.ssh_private_key:
        log.error("HARD FAIL: SSH_PRIVATE_KEY is not configured.")
        sys.exit(1)
    if not cfg.recreate_test_db_script:
        log.error("HARD FAIL: RECREATE_TEST_DB_SCRIPT is not configured.")
        sys.exit(1)

    log_path = os.path.join(reports_dir, "clone-recreate-prod-test.log")
    rc = _run_script(_build_ssh_cmd_prod(cfg, cfg.recreate_test_db_script), log_path)
    if rc != 0:
        fail = f"recreate_test_db_clone.sh exited {rc} on prod VM. See {log_path}"
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        summary["status"] = "failed"
        sys.exit(1)

    summary["status"] = "ok"
    summary["timestamp"] = datetime.datetime.utcnow().isoformat() + "Z"
    log.info("Prod test clone recreated successfully at %s.", summary["timestamp"])
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for Stage 9a: recreate prod test clone."""
    parser = argparse.ArgumentParser(
        description="Stage 9a: Recreate prod test DB clone."
    )
    parser.add_argument(
        "manifest_path",
        help="Path to raw-manifest.json from Stage 1.",
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

    if args.release_dir:
        release_dir = args.release_dir
    else:
        base = cfg.releases_base_dir_prod or cfg.releases_base_dir
        release_dir = os.path.join(
            base, manifest.ticket_number or "unknown"
        )

    os.makedirs(release_dir, exist_ok=True)

    summary = run(manifest, release_dir, cfg)

    output_path = os.path.join(release_dir, "reports", "clone-recreate-prod-test.json")
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
