"""
Stage 1: Trigger and discovery.

Resolves a commit hash to its author, timestamp, and message; extracts the Jira
ticket reference and PR number; enumerates all SQL files changed in the commit;
classifies each file by path pattern; detects intra-commit filename duplicates;
and writes the raw manifest JSON to the release directory.

Hard fail conditions:
  - Commit hash cannot be resolved (git log fails).
  - Jira ticket reference absent from the commit message.
  - PR number absent from the commit message.
  - No SQL files detected in the diff.

The commit hash is read from the first positional CLI argument or from the
GITHUB_SHA environment variable set by GitHub Actions.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger
from pipeline.models import Manifest, SqlFile

log = get_logger(__name__)

# Classification constants
#
# Date-prefixed SQL filenames are treated as schema migrations even when the
# path itself does not include a schema directory.
_MIGRATION_FILENAME_RE = re.compile(r"^\d{4}_\d{2}_\d{2}_")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------
#
# Discovery reads commit metadata and changed paths directly from git. Helpers
# keep subprocess usage shell-free and centralise logging for command replay.

def _run_git(repo_root: str, args: list[str]) -> str:
    """Run a git command in repo_root and return stdout.

    Uses subprocess with an explicit argument list (no shell=True). Raises
    subprocess.CalledProcessError on a non-zero exit code.
    """
    cmd = ["git", "-C", repo_root] + args
    log.debug("git: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def _run_git_result(repo_root: str, args: list[str]) -> subprocess.CompletedProcess:
    """Run a git command and return the CompletedProcess without raising on failure.

    Used for operations where the caller needs to inspect the return code and
    stderr before deciding how to proceed (e.g. cherry-pick conflict handling).
    """
    cmd = ["git", "-C", repo_root] + args
    log.debug("git: %s", " ".join(cmd))
    return subprocess.run(cmd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# Commit resolution
# ---------------------------------------------------------------------------
#
# Commit provenance is written into the manifest and later into generated SQL
# headers, so each value is read explicitly from the squash commit.

def resolve_cherry_pick_hash(repo_root: str, commit_hash: str) -> str:
    """Return the commit hash to use for git cherry-pick.

    Some squash-merge flows create two commits: a squash commit (single parent)
    and a merge commit (two parents) on the target branch. GITHUB_SHA may be
    the merge commit, but cherry-pick needs the squash commit (second parent)
    to apply cleanly without the '-m 1' flag.
    """
    try:
        parents_str = _run_git(
            repo_root, ["log", "--format=%P", "-1", commit_hash]
        ).strip()
        parents = parents_str.split()
        if len(parents) >= 2:
            return parents[1]
    except subprocess.CalledProcessError:
        pass
    return commit_hash


def get_commit_info(repo_root: str, commit_hash: str) -> tuple[str, str, str]:
    """Return (author, iso_timestamp, full_message) for the given commit.

    Each field is retrieved with a separate git log call so that special
    characters in the commit message cannot corrupt the other fields.
    """
    author = _run_git(
        repo_root, ["log", "--format=%an", "-1", commit_hash]
    ).strip()
    timestamp = _run_git(
        repo_root, ["log", "--format=%aI", "-1", commit_hash]
    ).strip()
    message = _run_git(
        repo_root, ["log", "--format=%B", "-1", commit_hash]
    ).strip()
    return author, timestamp, message


# ---------------------------------------------------------------------------
# Commit message parsing
# ---------------------------------------------------------------------------
#
# Jira and PR references are approval/audit anchors. Missing references are
# hard failures because generated artefacts would otherwise lose traceability.

def parse_commit_message(
    message: str,
    jira_pattern: str,
    pr_pattern: str,
) -> tuple[str, str, list[str]]:
    """Extract the Jira ticket reference and PR number from a commit message.

    Returns (jira_ticket, pr_number, fail_reasons).
    fail_reasons is non-empty if either reference is absent — a hard fail.
    """
    fails: list[str] = []

    jira_match = re.search(jira_pattern, message)
    if not jira_match:
        fails.append(
            f"Jira ticket not found in commit message "
            f"(expected pattern: {jira_pattern!r})"
        )
        jira_ticket = ""
    else:
        jira_ticket = jira_match.group(0)

    pr_match = re.search(pr_pattern, message)
    if not pr_match:
        fails.append(
            f"PR number not found in commit message "
            f"(expected pattern: {pr_pattern!r})"
        )
        pr_number = ""
    else:
        pr_number = pr_match.group(0)

    return jira_ticket, pr_number, fails


# ---------------------------------------------------------------------------
# Diff enumeration
# ---------------------------------------------------------------------------
#
# The manifest scope is derived from the commit diff. Rename/copy entries are
# normalized into add/delete semantics so downstream artefacts can handle them
# with deploy.lst and delete consistently.

def get_diff_files(repo_root: str, commit_hash: str) -> list[tuple[str, str]]:
    """Return (status, path) pairs for all files changed in the commit.

    Status codes: A (added), M (modified), D (deleted). Renamed files are
    returned as a (D, old_path) followed by (A, new_path) pair so that the
    old path is flagged for deletion and the new path enters the release.
    """
    output = _run_git(
        repo_root,
        ["diff", "--name-status", f"{commit_hash}~1", commit_hash],
    )
    files: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status_raw = parts[0].upper()

        if (status_raw.startswith("R") or status_raw.startswith("C")) and len(parts) >= 3:
            # Renamed or copied: emit delete of old path and add of new path.
            files.append(("D", parts[1]))
            files.append(("A", parts[2]))
        elif len(parts) >= 2:
            # Normalise status to single character.
            files.append((status_raw[0], parts[1]))

    return files


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
#
# Classification is path-based because the source repositories already encode
# deployment intent in their folder layout. Later stages use this to enforce
# static checks and deploy ordering.

def classify_file(relative_path: str) -> str:
    """Return the classification for a SQL file based on its path.

    Rules (first match wins):
      - Path contains /schema/ or filename matches YYYY_MM_DD_*: schema
      - Path contains /postgraphile/ or /functions/:             function
      - Path contains /types/:                                   type
      - Otherwise:                                               config
    """
    # Normalise separators for reliable matching.
    normalised = relative_path.replace("\\", "/")
    filename = os.path.basename(relative_path)

    if "/schema/" in normalised or _MIGRATION_FILENAME_RE.match(filename):
        return "schema"
    if "/postgraphile/" in normalised or "/functions/" in normalised:
        return "function"
    if "/types/" in normalised:
        return "type"
    return "config"


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------
#
# Duplicate basenames inside one release can cause accidental double deploys.
# The first occurrence remains deployable; later duplicates are retained in the
# manifest but commented out in deploy.lst.

def detect_duplicates(sql_files: list[SqlFile]) -> list[SqlFile]:
    """Annotate files that share the same basename across different paths.

    When two or more active (non-deleted) files have the same filename, the
    first occurrence is the canonical entry. All subsequent occurrences are
    flagged with is_duplicate=True and a duplicate_annotation referencing the
    canonical path.
    """
    # Build a map from basename to the index of its first appearance.
    first_seen: dict[str, int] = {}
    for i, sql_file in enumerate(sql_files):
        if sql_file.is_deleted:
            continue
        basename = os.path.basename(sql_file.relative_path)
        if basename not in first_seen:
            first_seen[basename] = i

    updated: list[SqlFile] = []
    for i, sql_file in enumerate(sql_files):
        if sql_file.is_deleted:
            updated.append(sql_file)
            continue

        basename = os.path.basename(sql_file.relative_path)
        canonical_idx = first_seen.get(basename, i)

        if canonical_idx != i:
            canonical_path = sql_files[canonical_idx].relative_path
            updated.append(
                SqlFile(
                    relative_path=sql_file.relative_path,
                    classification=sql_file.classification,
                    is_deleted=False,
                    is_duplicate=True,
                    duplicate_annotation=f"duplicate of {canonical_path}",
                )
            )
        else:
            updated.append(sql_file)

    return updated


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------
#
# run() performs pure discovery and returns data to the CLI. File writes and
# process exit decisions stay in main() so unit tests can inspect the manifest
# without touching the release directory.

def run(commit_hash: str, cfg: Config) -> tuple[Manifest, list[str]]:
    """Run Stage 1 trigger and discovery.

    Returns (manifest, fail_reasons). Fail reasons are also recorded on the
    manifest so callers can choose how to surface them.
    """
    all_fails: list[str] = []

    # Commit provenance is required before any manifest can be considered
    # auditable.
    log.info("Resolving commit: %s", commit_hash)
    try:
        author, timestamp, message = get_commit_info(
            cfg.git_repo_dataschema, commit_hash
        )
    except subprocess.CalledProcessError as exc:
        fail = f"Failed to resolve commit {commit_hash}: {exc.stderr.strip()}"
        log.error("HARD FAIL: %s", fail)
        return (
            Manifest(
                commit_hash=commit_hash,
                jira_ticket="",
                ticket_number="",
                pr_number="",
                author="",
                timestamp="",
                release_dir="",
                reports_dir="",
                sql_files=[],
                deleted_files=[],
                sql_changes=False,
                has_hard_fail=True,
                fail_reasons=[fail],
                warnings=[],
            ),
            [fail],
        )

    log.info("Commit: author=%s timestamp=%s", author, timestamp)

    # Jira and PR references tie generated artefacts back to reviewed work.
    jira_ticket, pr_number, message_fails = parse_commit_message(
        message, cfg.jira_ticket_pattern, cfg.pr_number_pattern
    )
    all_fails.extend(message_fails)
    for fail in message_fails:
        log.error("HARD FAIL: %s", fail)

    # Steps 3-5: Enumerate changed files from the dataschema repo.
    sql_files: list[SqlFile] = []
    deleted_files: list[str] = []

    try:
        diff_files = get_diff_files(cfg.git_repo_dataschema, commit_hash)
    except subprocess.CalledProcessError as exc:
        fail = f"git diff failed: {exc.stderr.strip()}"
        all_fails.append(fail)
        log.error("HARD FAIL: %s", fail)
        diff_files = []

    for status, path in diff_files:
        if not path.lower().endswith(".sql"):
            continue
        is_deleted = status == "D"
        sql_files.append(
            SqlFile(
                relative_path=path,
                classification=classify_file(path),
                is_deleted=is_deleted,
                is_duplicate=False,
                duplicate_annotation=None,
            )
        )
        if is_deleted:
            deleted_files.append(path)
        else:
            log.info("Discovered: %s (%s)", path, classify_file(path))

    # Serverless repo (optional): same diff analysis, classification = serverless.
    if cfg.serverless_configured():
        try:
            serverless_diff = get_diff_files(cfg.git_repo_serverless, commit_hash)
        except subprocess.CalledProcessError as exc:
            log.warning(
                "Serverless repo diff failed (non-fatal): %s",
                exc.stderr.strip(),
            )
            serverless_diff = []

        for status, path in serverless_diff:
            if not path.lower().endswith(".sql"):
                continue
            is_deleted = status == "D"
            sql_files.append(
                SqlFile(
                    relative_path=path,
                    classification="serverless",
                    is_deleted=is_deleted,
                    is_duplicate=False,
                    duplicate_annotation=None,
                )
            )
            if is_deleted:
                deleted_files.append(path)

    active = [f for f in sql_files if not f.is_deleted]
    no_sql = not active
    if no_sql:
        fail = "No SQL files detected in the commit diff."
        log.error("HARD FAIL: %s", fail)
        all_fails.append(fail)

    # Duplicate annotations are applied after all repositories are inspected so
    # cross-repo duplicate names are surfaced consistently.
    sql_files = detect_duplicates(sql_files)

    # If GITHUB_SHA is a merge commit, store the squash
    # commit (second parent) as the cherry-pick target, not the merge commit.
    cherry_pick_hash = resolve_cherry_pick_hash(cfg.git_repo_dataschema, commit_hash)
    if cherry_pick_hash != commit_hash:
        log.info(
            "Merge commit detected: using squash commit %s for cherry-pick.",
            cherry_pick_hash,
        )

    import re as _re
    m = _re.search(r"\d+$", jira_ticket) if jira_ticket else None
    ticket_number = m.group(0) if m else (jira_ticket or "unknown")
    release_dir = os.path.join(cfg.releases_base_dir, ticket_number)
    reports_dir = os.path.join(release_dir, "reports")

    manifest = Manifest(
        commit_hash=cherry_pick_hash,
        jira_ticket=jira_ticket,
        ticket_number=ticket_number,
        pr_number=pr_number,
        author=author,
        timestamp=timestamp,
        release_dir=release_dir,
        reports_dir=reports_dir,
        sql_files=sql_files,
        deleted_files=deleted_files,
        sql_changes=not no_sql,
        has_hard_fail=bool(all_fails),
        fail_reasons=all_fails,
        warnings=[],
    )

    return manifest, all_fails


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
#
# CI invokes this script as the first pipeline job. It creates the release
# directory, writes raw-manifest.json, and exits non-zero when discovery cannot
# produce an auditable release scope.

def main() -> None:
    """CLI entry point for Stage 1 trigger and discovery."""
    parser = argparse.ArgumentParser(
        description="Stage 1: Trigger and discovery."
    )
    parser.add_argument(
        "commit_hash",
        nargs="?",
        help="Commit hash to analyse. Defaults to GITHUB_SHA env var.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable DEBUG logging.")
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    try:
        cfg = Config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    commit_hash = args.commit_hash or os.environ.get("GITHUB_SHA", "").strip()
    if not commit_hash:
        log.error(
            "HARD FAIL: No commit hash provided. "
            "Pass as argument or set GITHUB_SHA."
        )
        sys.exit(1)

    manifest, _ = run(commit_hash, cfg)

    release_dir = manifest.release_dir or os.path.join(
        cfg.releases_base_dir, manifest.ticket_number or "unknown"
    )
    reports_dir = manifest.reports_dir or os.path.join(release_dir, "reports")
    os.makedirs(release_dir, exist_ok=True)
    os.makedirs(reports_dir, exist_ok=True)

    manifest_path = os.path.join(reports_dir, "raw-manifest.json")
    try:
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, indent=2)
        log.info("Manifest written to %s", manifest_path)
    except OSError as exc:
        log.error("Failed to write manifest: %s", exc)
        sys.exit(1)

    # Write pipeline.env so all downstream CI jobs can receive JIRA_TICKET,
    # JIRA_NUMBER, and RELEASE_DIR without re-extracting them inline.
    pipeline_env_path = "pipeline.env"
    try:
        with open(pipeline_env_path, "w", encoding="utf-8") as fh:
            fh.write(f"JIRA_TICKET={manifest.jira_ticket or 'unknown'}\n")
            fh.write(f"JIRA_NUMBER={manifest.ticket_number or 'unknown'}\n")
            fh.write(f"RELEASE_DIR={release_dir}\n")
        log.info("pipeline.env written.")
    except OSError as exc:
        log.error("Failed to write pipeline.env: %s", exc)
        sys.exit(1)

    if manifest.has_hard_fail:
        log.error(
            "Stage 1 completed with %d hard fail(s). Deployment is blocked.",
            len(manifest.fail_reasons),
        )
        sys.exit(1)

    log.info(
        "Stage 1 complete. Jira: %s, PR: %s, %d SQL file(s) discovered.",
        manifest.jira_ticket,
        manifest.pr_number,
        len([f for f in manifest.sql_files if not f.is_deleted]),
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
