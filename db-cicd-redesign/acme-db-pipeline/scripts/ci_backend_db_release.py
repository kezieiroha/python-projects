"""
Non-interactive pipeline variant of backend_db_release.py.

Used by s6_test_db_deploy.py (--target test) and s7_live_db_deploy.py (--target live).
No prompts are issued. The pipeline approval gates are the confirmation mechanism.

DB_ROOT resolution (same priority order as backend_db_release.py):
  1. Environment variable DB_ROOT
  2. AWS Secrets Manager: secret named by DB_ROOT_SECRET_NAME env var
  3. Hardcoded fallback 'read-pg-pass'

Hostname resolution: same pgpass-based derivation as backend_db_release.py.

Usage:
  python ci_backend_db_release.py <release_dir> --target <test|live>
                                  [--skip-git] [--log <path>]

Exit codes:
  0  all SQL files executed successfully
  1  any hard fail (missing files, psql error, pgpass not found)
"""

import argparse
import getpass
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Constants matching production script exactly
# ---------------------------------------------------------------------------

DB_NAME = "acme_v3"
DB_NAME_TEST = "acme_v3_test"
RDS_NAME_LIVE = "acme-v3"
RDS_NAME_TEST = "acme-v3-deployment-test"
RDS_POSTFIX = ".us-east-2.rds.amazonaws.com"
RDS_PORT = "5432"

POST_DEPLOY_FILES = [
    "postgraphile_apis/acme_api/run_post_mutation_queries/post_deployment_permissions.sql",
    "postgraphile_apis/acme_crm/run_post_mutation_queries/post_deployment_permissions.sql",
    "postgraphile_apis/acme_evt/run_post_mutation_queries/post_deployment_permissions.sql",
    "roles/read_access_role.sql",
]

REPO_DIR = os.path.expanduser(
    os.environ.get("GIT_REPO_DATASCHEMA", "~/v3/acme_v3_dataschema")
)


# ---------------------------------------------------------------------------
# DB_ROOT resolution
# ---------------------------------------------------------------------------

def _resolve_db_root() -> str:
    """Return DB_ROOT username from env, AWS Secrets Manager, or hardcoded fallback."""
    if os.environ.get("DB_ROOT"):
        return os.environ["DB_ROOT"]
    secret_name = os.environ.get("DB_ROOT_SECRET_NAME")
    if secret_name:
        try:
            result = subprocess.run(
                [
                    "aws", "secretsmanager", "get-secret-value",
                    "--secret-id", secret_name,
                    "--query", "SecretString",
                    "--output", "text",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            data = json.loads(result.stdout.strip())
            if "username" in data:
                return data["username"]
        except (subprocess.CalledProcessError, json.JSONDecodeError, KeyError):
            pass
    return "read-pg-pass"


# ---------------------------------------------------------------------------
# Hostname resolution from ~/.pgpass
# ---------------------------------------------------------------------------

def _resolve_hosts(db_root: str) -> tuple[str, str]:
    """Return (live_host, test_host) derived from ~/.pgpass or env var overrides.

    DB_HOST / DB_HOST_LIVE and DB_HOST_TEST env vars bypass pgpass pattern
    matching — used when the RDS hostname does not follow the acme-v3.* naming
    convention (e.g. sandbox environments with a different RDS prefix).
    """
    live_host = os.environ.get("DB_HOST_LIVE") or os.environ.get("DB_HOST", "")
    test_host = os.environ.get("DB_HOST_TEST") or os.environ.get("DB_HOST", "")
    if live_host and test_host:
        return live_host, test_host

    pgpass = Path.home() / ".pgpass"
    if not pgpass.exists():
        _die(f"~/.pgpass not found at {pgpass}")

    pattern = re.compile(
        rf"^({re.escape(RDS_NAME_LIVE)}\.[^.]+{re.escape(RDS_POSTFIX)})"
        rf":{RDS_PORT}:{re.escape(DB_NAME)}:{re.escape(db_root)}:"
    )

    live_host = ""
    for line in pgpass.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            live_host = m.group(1)
            break

    if not live_host:
        _die(
            f"No matching entry in ~/.pgpass for host={RDS_NAME_LIVE}.*, "
            f"db={DB_NAME}, user={db_root}"
        )

    parts = live_host.split(".")
    if len(parts) < 2:
        _die(f"Cannot extract RDS random suffix from host: {live_host}")
    rds_rand = parts[1]

    test_host = f"{RDS_NAME_TEST}.{rds_rand}{RDS_POSTFIX}"
    return live_host, test_host


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _default_log_path() -> str:
    user = getpass.getuser()
    ts = datetime.now().strftime("%Y-%m-%d-%H-%M")
    return f"/tmp/release_{user}_{ts}.log"


def _write_log(log: str, line: str) -> None:
    # Non-interactive: stdout only. The pipeline stage captures SSH stdout into
    # its own log file via subprocess.run(stdout=log_fh). Writing directly to
    # the log path here would duplicate every line when the pipeline reads it.
    print(line)


def _psql_cmd(host: str, db_root: str, database: str) -> list[str]:
    return ["psql", "-w", "-h", host, "-U", db_root, "-d", database]


def _run_sql_file(psql_base: list[str], sql_file: str, log: str) -> None:
    """Execute a single SQL file via psql. Hard fail on non-zero exit."""
    _write_log(log, f"Deploying: {sql_file}")
    result = subprocess.run(
        psql_base + ["-f", sql_file],
        capture_output=True,
        text=True,
    )
    with open(log, "a", encoding="utf-8") as fh:
        if result.stdout:
            fh.write(result.stdout)
        if result.stderr:
            fh.write(result.stderr)
    if result.returncode != 0:
        _die(f"psql exited {result.returncode} on {sql_file}")


def _git_current_branch() -> str:
    result = subprocess.run(
        ["git", "-C", REPO_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


# ---------------------------------------------------------------------------
# Optional git pull (skipped by default in pipeline context)
# ---------------------------------------------------------------------------

def _git_pull(log: str) -> None:
    """Run git reset + pull on REPO_DIR. Called only when --skip-git is not set."""
    branch = _git_current_branch()
    _write_log(log, f"Running git pull on {branch}...")
    for git_cmd in (
        ["git", "-C", REPO_DIR, "reset", "--hard", "HEAD"],
        ["git", "-C", REPO_DIR, "checkout", "."],
        ["git", "-C", REPO_DIR, "pull"],
    ):
        result = subprocess.run(git_cmd, capture_output=True, text=True)
        with open(log, "a", encoding="utf-8") as fh:
            if result.stdout:
                fh.write(result.stdout)
            if result.stderr:
                fh.write(result.stderr)
        if result.returncode != 0:
            _die(f"git command failed: {' '.join(git_cmd)}")


# ---------------------------------------------------------------------------
# Deployment sequence
# ---------------------------------------------------------------------------

def _process_files(release_dir: str, psql_base: list[str], log: str) -> None:
    """Run prep.sql, deploy.lst entries, post.sql in order."""
    _write_log(log, f"Logging to {log}")

    prep = os.path.join(release_dir, "prep.sql")
    if os.path.isfile(prep) and os.path.getsize(prep) > 0:
        _write_log(log, "Executing prep.sql...")
        _run_sql_file(psql_base, prep, log)

    deploy_lst = os.path.join(release_dir, "deploy.lst")
    if os.path.isfile(deploy_lst) and os.path.getsize(deploy_lst) > 0:
        _write_log(log, "Deploying SQL files from deploy.lst...")
        with open(deploy_lst, encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                sql_file = os.path.join(REPO_DIR, line)
                if not os.path.isfile(sql_file):
                    _die(f"File not found: {sql_file}")
                _write_log(log, f"Running {os.path.basename(sql_file)}")
                _run_sql_file(psql_base, sql_file, log)

    post = os.path.join(release_dir, "post.sql")
    if os.path.isfile(post) and os.path.getsize(post) > 0:
        _write_log(log, "Executing post.sql...")
        _run_sql_file(psql_base, post, log)


def _post_deploy_files(psql_base: list[str], log: str) -> None:
    """Run the fixed post-deploy permission files, matching production behaviour."""
    for rel in POST_DEPLOY_FILES:
        sql_file = os.path.join(REPO_DIR, rel)
        if os.path.isfile(sql_file):
            _run_sql_file(psql_base, sql_file, log)

    if _git_current_branch() == "staging":
        write_role = os.path.join(REPO_DIR, "roles/write_access_role.sql")
        if os.path.isfile(write_role):
            _run_sql_file(psql_base, write_role, log)


# ---------------------------------------------------------------------------
# Validations
# ---------------------------------------------------------------------------

def _validate(release_dir: str) -> None:
    if not os.path.isdir(release_dir):
        _die(f"Release directory not found: {release_dir}")
    if not os.path.isdir(REPO_DIR):
        _die(f"Repository directory not found: {REPO_DIR}")

    artefacts = [
        os.path.join(release_dir, f) for f in ("prep.sql", "deploy.lst", "post.sql")
    ]
    non_empty = any(
        os.path.isfile(p) and os.path.getsize(p) > 0 for p in artefacts
    )
    if not non_empty:
        _die("At least one non-empty artefact file must be present (prep.sql, deploy.lst, post.sql).")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Non-interactive pipeline DB deployment script."
    )
    parser.add_argument("release_dir", help="Path to the release directory.")
    parser.add_argument(
        "--target",
        choices=["test", "live"],
        required=True,
        help="Target database: 'test' or 'live'.",
    )
    parser.add_argument(
        "--skip-git",
        action="store_true",
        default=True,
        help="Skip git reset/pull (default: True; pipeline manages repo state).",
    )
    parser.add_argument(
        "--log",
        default=None,
        help="Log file path. Defaults to /tmp/release_<user>_<timestamp>.log.",
    )
    args = parser.parse_args()

    release_dir = args.release_dir
    _validate(release_dir)

    db_root = _resolve_db_root()
    live_host, test_host = _resolve_hosts(db_root)

    log = args.log or _default_log_path()
    host = live_host if args.target == "live" else test_host
    database = DB_NAME if args.target == "live" else DB_NAME_TEST

    _write_log(log, f"Release directory: {release_dir}")
    _write_log(log, f"Logging to {log}")
    _write_log(log, f"Target: {args.target.upper()} ({database} on {host})")

    if not args.skip_git:
        _git_pull(log)
    else:
        _write_log(log, "Skipping git commands (pipeline manages repo state).")

    psql_base = _psql_cmd(host, db_root, database)

    _process_files(release_dir, psql_base, log)
    _post_deploy_files(psql_base, log)

    _write_log(log, "done")


if __name__ == "__main__":
    main()
