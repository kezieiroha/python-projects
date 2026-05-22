"""
Stage 8: Git promotion (staging → production).

Triggered by a push to the staging branch. Reads the staging cherry-pick hash
from git-promotion.json (written by s5 during the develop pipeline run) and
promotes it onto the production branch via cherry-pick.

Infrastructure guard: if EC2_HOST_PROD is not set, exits 0 immediately with a
log message. Apply this guard as the first check — the staging pipeline must
run cleanly in environments where production is not yet configured.

The source commit is the staging cherry-pick hash, not the original develop
squash hash. This preserves the full provenance chain in git history:
  develop squash → staging cherry-pick → production cherry-pick.

Hard fail conditions:
  - EC2_HOST_PROD is set but git-promotion.json cannot be loaded.
  - cherry_pick_hash is absent or empty in git-promotion.json.
  - Cherry-pick onto production branch fails with a conflict.
  - Push to production branch fails.

Output: git-promotion-prod.json (staging hash, production cherry-pick hash,
promotion timestamp) in $RELEASES_BASE_DIR_PROD/<ticket>/reports/
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger
from stages.s5_git_promotion import promote_repo

log = get_logger(__name__)

_INFRA_GUARD_MSG = (
    "Production infrastructure not configured (EC2_HOST_PROD not set) — skipping."
)


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------

def run(
    git_promotion: dict,
    release_dir: str,
    cfg: Config,
) -> dict:
    """Run Stage 8: promote staging cherry-pick hash onto production.

    Returns a summary dict. Exits non-zero on hard fail.
    Infrastructure guard exits 0 if EC2_HOST_PROD is not configured.
    """
    if not cfg.ec2_host_prod:
        log.info(_INFRA_GUARD_MSG)
        return {"skipped": True, "reason": "EC2_HOST_PROD not set"}

    jira_ticket = git_promotion.get("jira_ticket", "")
    pr_number = git_promotion.get("pr_number", "")
    promotions = git_promotion.get("promotions", [])

    if not promotions:
        log.error("HARD FAIL: git-promotion.json contains no promotion records.")
        sys.exit(1)

    staging_cherry_pick_hash = promotions[0].get("cherry_pick_hash", "")
    if not staging_cherry_pick_hash:
        log.error("HARD FAIL: cherry_pick_hash is empty in git-promotion.json.")
        sys.exit(1)

    log.info(
        "Promoting staging cherry-pick %s onto production for %s %s",
        staging_cherry_pick_hash, jira_ticket, pr_number,
    )

    # Read delete paths from release dir — same artefact written during s4 and
    # used by s5. Production promotion applies the same file removals.
    delete_file = os.path.join(release_dir, "delete")
    delete_paths: list[str] = []
    if os.path.exists(delete_file):
        with open(delete_file, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    delete_paths.append(stripped)
        log.info("Delete file lists %d path(s) for production.", len(delete_paths))

    result = promote_repo(
        repo_root=cfg.git_repo_dataschema,
        commit_hash=staging_cherry_pick_hash,
        target_branch="production",
        delete_paths=delete_paths,
        jira_ticket=jira_ticket,
        pr_number=pr_number,
    )

    promotion_ts = datetime.now(timezone.utc).isoformat()
    summary = {
        "jira_ticket": jira_ticket,
        "pr_number": pr_number,
        "staging_cherry_pick_hash": staging_cherry_pick_hash,
        "production_cherry_pick_hash": result.get("cherry_pick_hash", ""),
        "target_branch": "production",
        "promotion_timestamp": promotion_ts,
        "skipped": False,
    }
    log.info(
        "Stage 8 complete. Production cherry-pick hash: %s",
        summary["production_cherry_pick_hash"],
    )
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> None:
    """CLI entry point for Stage 8: git promotion to production."""
    parser = argparse.ArgumentParser(
        description="Stage 8: Git promotion (staging → production)."
    )
    parser.add_argument(
        "git_promotion_path",
        help="Path to git-promotion.json from the staging pipeline run.",
    )
    parser.add_argument(
        "--release-dir",
        help=(
            "Release directory containing the delete artefact. "
            "Defaults to $RELEASES_BASE_DIR_PROD/<ticket>/"
        ),
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
        with open(args.git_promotion_path, encoding="utf-8") as fh:
            git_promotion = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Failed to load git-promotion.json from %s: %s", args.git_promotion_path, exc)
        sys.exit(1)

    jira_ticket = git_promotion.get("jira_ticket", "")
    ticket_number = jira_ticket.split("-")[-1] if "-" in jira_ticket else jira_ticket

    if args.release_dir:
        release_dir = args.release_dir
    else:
        base = cfg.releases_base_dir_prod or cfg.releases_base_dir
        release_dir = os.path.join(base, ticket_number or "unknown")

    reports_dir = os.path.join(release_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    summary = run(git_promotion, release_dir, cfg)

    output_path = os.path.join(reports_dir, "git-promotion-prod.json")
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        log.info("Promotion record written to %s", output_path)
    except OSError as exc:
        log.error("Failed to write git-promotion-prod.json: %s", exc)
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
