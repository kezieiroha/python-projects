"""
Stage 5: Git promotion.

Checks out the target branch, pulls latest, cherry-picks the squash commit from
develop, and pushes. If the release delete file is non-empty, removes each listed
SQL file from the repo, commits, and pushes a second time.

If acme_v3_serverless is configured and the manifest contains serverless files,
the same cherry-pick sequence is repeated on that repo.

Hard fail conditions:
  - checkout or pull fails on any configured repo.
  - cherry-pick produces a conflict or non-zero exit.
  - push fails after cherry-pick.
  - Any file listed in the delete file cannot be removed from the repo tree.
  - push fails after delete commit.

Output: git-promotion.json written to the release directory recording the
original commit hash, cherry-pick hashes, target branch, promotion timestamp,
and files deleted from the repo.
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger
from pipeline.models import Manifest

# Module logger
#
# Git promotion is intentionally verbose at INFO level because branch movement
# and cherry-pick hashes are part of the release audit trail.
log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
#
# All git operations go through subprocess argument lists. This avoids shell
# interpolation and keeps conflict/error handling close to the command result.

def _run_git(repo_root: str, args: list[str]) -> str:
    """Run a git command in repo_root, return stdout, raise on non-zero exit."""
    cmd = ["git", "-C", repo_root] + args
    log.debug("git: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def _run_git_result(repo_root: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command in repo_root, return CompletedProcess without raising."""
    cmd = ["git", "-C", repo_root] + args
    log.debug("git: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Single-repo promotion
# ---------------------------------------------------------------------------
#
# Promotion replays the reviewed squash commit onto the target branch. Delete
# artefacts are applied only after the cherry-pick succeeds so branch history
# preserves both the reviewed change and the cleanup commit.

def promote_repo(
    repo_root: str,
    commit_hash: str,
    target_branch: str,
    delete_paths: list[str],
    jira_ticket: str,
    pr_number: str,
) -> dict:
    """Cherry-pick commit_hash onto target_branch in repo_root.

    Returns a dict with promotion details. Raises SystemExit on hard fail.

    Steps:
    1. checkout target_branch
    2. pull
    3. cherry-pick commit_hash
    4. push
    5. delete any paths listed in delete_paths, commit, push (if non-empty)
    """
    result: dict = {
        "repo": repo_root,
        "original_hash": commit_hash,
        "cherry_pick_hash": "",
        "target_branch": target_branch,
        "promotion_timestamp": "",
        "deleted_files": [],
    }

    # Work from the target branch so the cherry-pick preserves reviewed commit
    # provenance in the promotion branch history.
    log.info("Checking out %s in %s", target_branch, repo_root)
    try:
        _run_git(repo_root, ["checkout", target_branch])
    except subprocess.CalledProcessError as exc:
        fail = (
            f"git checkout {target_branch} failed in {repo_root}: "
            f"{exc.stderr.strip()}"
        )
        log.error("HARD FAIL: %s", fail)
        sys.exit(1)

    # Pull before cherry-pick to avoid promoting onto stale branch state.
    log.info("Pulling %s in %s", target_branch, repo_root)
    try:
        _run_git(repo_root, ["pull"])
    except subprocess.CalledProcessError as exc:
        fail = (
            f"git pull on {target_branch} failed in {repo_root}: "
            f"{exc.stderr.strip()}"
        )
        log.error("HARD FAIL: %s", fail)
        sys.exit(1)

    # Ensure the cherry-pick commit and its parent are fully available.
    # CI clones with --depth=2 (shallow); the squash commit's parent may sit
    # at the shallow boundary, which can prevent cherry-pick from computing
    # the diff cleanly. Fetching the specific commit with depth=2 guarantees
    # both the commit and its direct parent are present in the object store.
    log.info("Fetching cherry-pick commit %s to ensure full availability.", commit_hash)
    fetch_result = _run_git_result(
        repo_root, ["fetch", "--depth=2", "origin", commit_hash]
    )
    if fetch_result.returncode != 0:
        log.warning(
            "Shallow fetch for %s failed (may already be complete): %s",
            commit_hash,
            fetch_result.stderr.strip(),
        )

    # Cherry-pick replays the exact squash commit that passed review.
    # Some providers create a merge commit on top of the squash when using
    # "merge commit" strategy; use -m 1 to tell git which parent is the mainline.
    parent_count_result = _run_git_result(
        repo_root, ["log", "--format=%P", "-1", commit_hash]
    )
    parent_count = len(parent_count_result.stdout.strip().split()) if parent_count_result.returncode == 0 else 1
    cherry_args = ["cherry-pick", "-m", "1", commit_hash] if parent_count > 1 else ["cherry-pick", commit_hash]
    log.info("Cherry-picking %s onto %s in %s", commit_hash, target_branch, repo_root)
    cp_result = _run_git_result(repo_root, cherry_args)
    if cp_result.returncode != 0:
        # Combine stdout+stderr — git may write the "now empty" hint to either.
        cp_output = (cp_result.stderr + cp_result.stdout).strip()
        # "now empty" means the target branch already contains this commit's
        # changes. Skip it — the content is already promoted.
        if "now empty" in cp_output or "allow-empty" in cp_output:
            log.warning(
                "Cherry-pick of %s onto %s is empty — content already present. Skipping.",
                commit_hash,
                target_branch,
            )
            _run_git_result(repo_root, ["cherry-pick", "--skip"])
        else:
            # Abort the cherry-pick to leave the repo clean before failing.
            abort_result = _run_git_result(repo_root, ["cherry-pick", "--abort"])
            if abort_result.returncode != 0:
                log.warning(
                    "cherry-pick --abort also failed in %s: %s",
                    repo_root,
                    abort_result.stderr.strip(),
                )
            fail = (
                f"git cherry-pick {commit_hash} failed in {repo_root}: {cp_output}"
            )
            log.error("HARD FAIL: %s", fail)
            sys.exit(1)

    # Record cherry-pick hash and timestamp immediately after success.
    try:
        cp_hash = _run_git(repo_root, ["rev-parse", "HEAD"]).strip()
    except subprocess.CalledProcessError:
        cp_hash = ""

    promotion_ts = datetime.now(timezone.utc).isoformat()
    result["cherry_pick_hash"] = cp_hash
    result["promotion_timestamp"] = promotion_ts
    log.info(
        "Cherry-pick complete: new HEAD %s at %s", cp_hash, promotion_ts
    )

    # Push the promoted commit before applying delete-file cleanup so the audit
    # trail separates reviewed SQL changes from repository cleanup.
    log.info("Pushing %s in %s", target_branch, repo_root)
    try:
        _run_git(repo_root, ["push"])
    except subprocess.CalledProcessError as exc:
        fail = (
            f"git push on {target_branch} failed in {repo_root}: "
            f"{exc.stderr.strip()}"
        )
        log.error("HARD FAIL: %s", fail)
        sys.exit(1)

    # Deleted SQL paths are committed separately after successful promotion.
    if not delete_paths:
        return result

    log.info("Removing %d deleted file(s) from %s", len(delete_paths), repo_root)
    actually_removed: list[str] = []
    for rel_path in delete_paths:
        abs_path = os.path.join(repo_root, rel_path)
        if not os.path.exists(abs_path):
            log.warning(
                "Skipping delete: %s does not exist in %s", rel_path, repo_root
            )
            continue
        try:
            _run_git(repo_root, ["rm", "--force", rel_path])
            actually_removed.append(rel_path)
        except subprocess.CalledProcessError as exc:
            fail = (
                f"git rm {rel_path} failed in {repo_root}: {exc.stderr.strip()}"
            )
            log.error("HARD FAIL: %s", fail)
            sys.exit(1)

    if not actually_removed:
        log.info("No files actually removed; skipping delete commit.")
        return result

    commit_msg = f"{jira_ticket} {pr_number} - remove deleted SQL files"
    try:
        _run_git(repo_root, ["commit", "-m", commit_msg])
    except subprocess.CalledProcessError as exc:
        fail = (
            f"git commit for deleted files failed in {repo_root}: "
            f"{exc.stderr.strip()}"
        )
        log.error("HARD FAIL: %s", fail)
        sys.exit(1)

    log.info("Pushing delete commit on %s in %s", target_branch, repo_root)
    try:
        _run_git(repo_root, ["push"])
    except subprocess.CalledProcessError as exc:
        fail = (
            f"git push (delete commit) on {target_branch} failed in {repo_root}: "
            f"{exc.stderr.strip()}"
        )
        log.error("HARD FAIL: %s", fail)
        sys.exit(1)

    result["deleted_files"] = actually_removed
    log.info("Deleted from repo: %s", actually_removed)
    return result


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------
#
# Stage 5 coordinates dataschema promotion and optional serverless promotion.
# It reads the generated delete artefact because deletion handling is part of
# promotion, not earlier analysis.

def run(
    manifest: Manifest,
    target_branch: str,
    cfg: Config,
) -> dict:
    """Run Stage 5 git promotion.

    Returns a promotion summary dict. Callers are responsible for writing it
    to git-promotion.json.
    """
    commit_hash = manifest.commit_hash
    jira_ticket = manifest.jira_ticket
    pr_number = manifest.pr_number

    # Determine which files the delete artefact lists.
    release_dir = manifest.release_dir or os.path.join(
        cfg.releases_base_dir, manifest.jira_ticket or "unknown"
    )
    delete_file = os.path.join(release_dir, "delete")

    delete_paths: list[str] = []
    if os.path.exists(delete_file):
        with open(delete_file, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    delete_paths.append(stripped)
        log.info("Delete file lists %d path(s).", len(delete_paths))
    else:
        log.info("No delete file found at %s; skipping file removal.", delete_file)

    promotion_records: list[dict] = []

    # Promote the dataschema repo.
    log.info(
        "Promoting dataschema repo: %s -> %s",
        cfg.git_repo_dataschema,
        target_branch,
    )
    ds_result = promote_repo(
        repo_root=cfg.git_repo_dataschema,
        commit_hash=commit_hash,
        target_branch=target_branch,
        delete_paths=delete_paths,
        jira_ticket=jira_ticket,
        pr_number=pr_number,
    )
    promotion_records.append(ds_result)

    # Optionally promote the serverless repo if it was touched in this diff.
    has_serverless = any(
        f.classification == "serverless" and not f.is_deleted
        for f in manifest.sql_files
    )
    if cfg.serverless_configured() and has_serverless:
        log.info(
            "Promoting serverless repo: %s -> %s",
            cfg.git_repo_serverless,
            target_branch,
        )
        sl_result = promote_repo(
            repo_root=cfg.git_repo_serverless,
            commit_hash=commit_hash,
            target_branch=target_branch,
            delete_paths=[],
            jira_ticket=jira_ticket,
            pr_number=pr_number,
        )
        promotion_records.append(sl_result)
    elif cfg.serverless_configured():
        log.info(
            "Serverless repo configured but no serverless files in manifest; skipping."
        )

    summary = {
        "jira_ticket": jira_ticket,
        "pr_number": pr_number,
        "original_commit_hash": commit_hash,
        "target_branch": target_branch,
        "promotions": promotion_records,
    }
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
#
# The CLI writes git-promotion.json for later gates and exits non-zero whenever
# promotion cannot be completed cleanly.

def main() -> None:
    """CLI entry point for Stage 5 git promotion."""
    parser = argparse.ArgumentParser(
        description="Stage 5: Git cherry-pick promotion to target branch."
    )
    parser.add_argument(
        "manifest_path",
        help="Path to the raw-manifest.json produced by Stage 1.",
    )
    parser.add_argument(
        "--target-branch",
        default="staging",
        help="Branch to cherry-pick onto (default: staging).",
    )
    parser.add_argument(
        "--source-branch",
        default="",
        help="Source branch the commit originates from (informational; default: develop).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable DEBUG logging."
    )
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
        log.error("Failed to load manifest from %s: %s", args.manifest_path, exc)
        sys.exit(1)

    if not manifest.sql_changes:
        log.info("Passthrough release — no SQL changes. Skipping.")
        sys.exit(0)

    summary = run(manifest, args.target_branch, cfg)

    release_dir = manifest.release_dir or os.path.join(
        cfg.releases_base_dir, manifest.ticket_number or "unknown"
    )
    reports_dir = manifest.reports_dir or os.path.join(release_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    output_path = os.path.join(reports_dir, "git-promotion.json")
    try:
        with open(output_path, "w", encoding="utf-8") as fh:
            json.dump(summary, fh, indent=2)
        log.info("Promotion record written to %s", output_path)
    except OSError as exc:
        log.error("Failed to write git-promotion.json: %s", exc)
        sys.exit(1)

    first = summary["promotions"][0] if summary["promotions"] else {}
    log.info(
        "Stage 5 complete. cherry-pick hash: %s, branch: %s",
        first.get("cherry_pick_hash", ""),
        args.target_branch,
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
