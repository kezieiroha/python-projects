"""
Stage 6a: Recreate staging test DB clone.

Runs before s6. Creates a fresh staging test DB clone from staging-live by invoking
recreate_test_db_clone.sh on EC2 via SSH.

Operations:
1. If SKIP_TEST_CLONE_RECREATE is true, log and exit 0 immediately.
2. SSH to EC2, run cfg.recreate_test_db_script. Capture stdout/stderr.
   HARD FAIL if exit code is non-zero.
3. Log completion timestamp and write clone-recreate-staging-test.json to reports/.

Hard fail conditions:
  - EC2_HOST or SSH_PRIVATE_KEY not configured.
  - RECREATE_TEST_DB_SCRIPT not configured.
  - recreate_test_db_clone.sh exits non-zero.
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


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------

def run(manifest: Manifest, release_dir: str, cfg: Config) -> dict:
    """Run Stage 6a: recreate the staging test DB clone.

    Returns a summary dict; exits non-zero on any hard fail.
    """
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

    if cfg.skip_test_clone_recreate:
        log.info("SKIP_TEST_CLONE_RECREATE is set -- using existing staging test clone.")
        summary["skipped"] = True
        summary["status"] = "skipped"
        return summary

    if not cfg.ec2_host:
        log.error("HARD FAIL: EC2_HOST is not configured.")
        sys.exit(1)
    if not cfg.ssh_private_key:
        log.error("HARD FAIL: SSH_PRIVATE_KEY is not configured.")
        sys.exit(1)
    if not cfg.recreate_test_db_script:
        log.error("HARD FAIL: RECREATE_TEST_DB_SCRIPT is not configured.")
        sys.exit(1)

    log_path = os.path.join(reports_dir, "clone-recreate-staging-test.log")
    rc = _run_script(_build_ssh_cmd(cfg, cfg.recreate_test_db_script), log_path)
    if rc != 0:
        fail = f"recreate_test_db_clone.sh exited {rc}. See {log_path}"
        log.error("HARD FAIL: %s", fail)
        summary["has_hard_fail"] = True
        summary["fail_reasons"].append(fail)
        summary["status"] = "failed"
        sys.exit(1)

    summary["status"] = "ok"
    summary["timestamp"] = datetime.datetime.utcnow().isoformat() + "Z"
    log.info("Staging test clone recreated successfully at %s.", summary["timestamp"])
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for Stage 6a: recreate staging test clone."""
    parser = argparse.ArgumentParser(
        description="Stage 6a: Recreate staging test DB clone."
    )
    parser.add_argument(
        "manifest_path",
        help="Path to raw-manifest.json from Stage 1.",
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

    if args.release_dir:
        release_dir = args.release_dir
    else:
        release_dir = manifest.release_dir or os.path.join(
            cfg.releases_base_dir, manifest.ticket_number or "unknown"
        )

    os.makedirs(release_dir, exist_ok=True)

    summary = run(manifest, release_dir, cfg)

    output_path = os.path.join(release_dir, "reports", "clone-recreate-staging-test.json")
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
