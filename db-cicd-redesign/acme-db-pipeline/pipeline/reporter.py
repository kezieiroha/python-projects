"""
Gate report generator for the acme DB pipeline.

Reads JSON artefacts from the release directory, produces structured Markdown
reports for each gate, and posts them as comments on the GitHub pull request via
the GitHub REST API.

Five public entry points:
  generate_gate1_report(release_dir, cfg) -> str
  generate_gate2_report(release_dir, cfg) -> str
  generate_gate3_report(release_dir, cfg) -> str
  generate_gate4_report(release_dir, cfg) -> str  (prod test DB)
  generate_gate5_report(release_dir, cfg) -> str  (prod live DB)

Each returns the full Markdown string. Callers post it with post_pr_comment().
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger

# Module logger
#
# Report generation is intentionally tolerant of missing artefacts so failed
# stages can still produce useful gate context. Warnings identify incomplete
# inputs without preventing report rendering.
log = get_logger(__name__)

# Gate 1 check accounting
#
# The summary line reports broad check categories rather than individual file
# findings. Detailed failures remain listed later in the report.
# Total number of distinct checks in Gate 1 (4 static + 3 DB = 7).
_GATE1_TOTAL_CHECKS = 7


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
#
# Reports are generated from artefacts that may be missing after a failed stage.
# These helpers keep that failure mode readable: return empty structures, log
# what could not be loaded, and let the report show sparse context.

def _load_json(path: str) -> dict:
    """Load a JSON file. Returns an empty dict if the file is missing or invalid."""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return {}


def _load_json_list(path: str) -> list:
    """Load a JSON file expected to contain a list. Returns [] on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read %s: %s", path, exc)
        return []


def _read_lines(path: str) -> list[str]:
    """Read all lines from a text file. Returns [] if the file is missing."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def _extract_pr_number(value: str) -> int | None:
    """Extract a numeric PR number from a string such as 'PR-99', '42', or 'pull-request-123'.

    Returns the last run of digits as an int, or None if no digits are present.
    """
    m = re.search(r"(\d+)\D*$", value)
    return int(m.group(1)) if m else None


def _lookup_pr_number(commit_sha: str, cfg: Config) -> int | None:
    """Return the GitHub PR number associated with commit_sha via the commits API.

    Uses GET /repos/{owner}/{repo}/commits/{sha}/pulls.
    Returns None if the lookup fails or no PR is found.
    """
    repository = getattr(cfg, "github_repository", "")
    url = (
        f"{cfg.github_api_url}/repos/{repository}"
        f"/commits/{commit_sha}/pulls"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {cfg.github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            prs = json.loads(resp.read().decode("utf-8"))
            if prs:
                return prs[0].get("number")
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, json.JSONDecodeError) as exc:
        log.warning("PR number lookup failed for %s: %s", commit_sha, exc)
    return None


# ---------------------------------------------------------------------------
# deploy.lst / delete file parsing
# ---------------------------------------------------------------------------
#
# Gate reports need human-facing summaries, not raw artefact text. These helpers
# extract only the actionable entries while preserving duplicate annotations and
# delete paths for reviewer attention.

def _parse_deploy_lst(release_dir: str) -> tuple[list[str], list[str]]:
    """Return (active_entries, commented_entries) from deploy.lst.

    active_entries are non-commented, non-empty lines.
    commented_entries are lines starting with '#' that reference SQL paths.
    """
    path = os.path.join(release_dir, "deploy.lst")
    lines = _read_lines(path)
    active: list[str] = []
    commented: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            if ".sql" in stripped:
                commented.append(stripped)
        else:
            active.append(stripped)
    return active, commented


def _parse_delete_file(release_dir: str) -> list[str]:
    """Return non-commented, non-empty lines from the delete file."""
    path = os.path.join(release_dir, "delete")
    lines = _read_lines(path)
    return [
        l.strip() for l in lines
        if l.strip() and not l.strip().startswith("#")
    ]


# ---------------------------------------------------------------------------
# Gate 1 — analysis report
# ---------------------------------------------------------------------------
#
# Gate 1 is the approval point before any test deployment. It combines static
# and DB-assisted analysis so DevOps can review proposed drops, deploy order,
# audit work, warnings, and any hard fails in one comment.

def generate_gate1_report(release_dir: str, cfg: Config) -> str:
    """Generate the Gate 1 Markdown report from analysis artefacts.

    Reads: raw-manifest.json, static-analysis-report.json,
           db-analysis-report.json (all in reports/ subdirectory),
           deploy.lst, delete (release_dir root).
    """
    reports_dir = os.path.join(release_dir, "reports")
    manifest = _load_json(os.path.join(reports_dir, "raw-manifest.json"))
    static = _load_json(os.path.join(reports_dir, "static-analysis-report.json"))
    db = _load_json(os.path.join(reports_dir, "db-analysis-report.json"))

    jira = manifest.get("jira_ticket", "UNKNOWN")
    pr = manifest.get("pr_number", "UNKNOWN")

    s2_fails: list[str] = static.get("hard_fails", [])
    s3_fails: list[str] = db.get("hard_fails", [])
    all_fails = s2_fails + s3_fails

    s2_warnings: list[str] = static.get("warnings", [])
    s3_warnings: list[str] = db.get("warnings", [])
    all_warnings = s2_warnings + s3_warnings

    checks_passed = max(0, _GATE1_TOTAL_CHECKS - len(all_fails))
    lines: list[str] = []

    # Opening decision line: gives approvers the high-level pass/warn/fail state
    # before they scan the detailed artefact sections.
    lines.append(
        f"## Analysis complete for {jira} {pr} — "
        f"{checks_passed} check(s) passed, "
        f"{len(all_warnings)} warning(s), "
        f"{len(all_fails)} hard fail(s)"
    )
    lines.append("")

    # Release scope: shows exactly which SQL files the pipeline believes are in
    # scope and the deploy order that downstream scripts will use.
    lines.append("### Manifest")
    lines.append("")
    commit = manifest.get("commit_hash", "")
    author = manifest.get("author", "")
    timestamp = manifest.get("timestamp", "")
    lines.append(f"Commit: `{commit}` | Author: {author} | Timestamp: {timestamp}")
    lines.append("")

    sql_files = manifest.get("sql_files", [])
    deploy_order = static.get("deploy_order", [f["relative_path"] for f in sql_files if not f.get("is_deleted")])
    file_map = {f["relative_path"]: f for f in sql_files}

    active_files = [p for p in deploy_order if not file_map.get(p, {}).get("is_deleted", False)]
    if active_files:
        lines.append(f"{len(active_files)} SQL file(s) in deploy order:")
        lines.append("")
        for path in active_files:
            sql_f = file_map.get(path, {})
            cls = sql_f.get("classification", "")
            dup_note = ""
            if sql_f.get("is_duplicate"):
                dup_note = f" — {sql_f.get('duplicate_annotation', 'duplicate')}"
            lines.append(f"- `{path}` ({cls}){dup_note}")
    else:
        lines.append("No SQL files detected.")
    lines.append("")

    # prep.sql review: signature/type drops are the riskiest generated artefact,
    # so each entry includes both the statement and why it was required.
    lines.append("### prep.sql")
    lines.append("")
    deltas = db.get("signature_deltas", [])
    drop_stmts = db.get("drop_order", [])
    if deltas:
        lines.append(f"{len(deltas)} DROP statement(s) required:")
        lines.append("")
        for delta in deltas:
            schema = delta.get("schema", "")
            name = delta.get("name", "")
            reason = delta.get("reason", "")
            stmt = delta.get("drop_statement", "")
            lines.append(f"- `{schema}.{name}` — {reason}")
            if stmt:
                lines.append(f"  `{stmt}`")
    elif drop_stmts:
        lines.append(f"{len(drop_stmts)} DROP statement(s) in prep.sql:")
        lines.append("")
        for stmt in drop_stmts:
            if stmt:
                lines.append(f"- `{stmt}`")
    else:
        lines.append("No DROP statements required.")
    lines.append("")

    # post.sql review: audit actions are generated from DB-assisted gap analysis
    # and must be visible before any deployment gate is approved.
    lines.append("### post.sql")
    lines.append("")
    audit_gaps = db.get("audit_gaps", [])
    if audit_gaps:
        lines.append(f"{len(audit_gaps)} audit action(s):")
        lines.append("")
        for gap in audit_gaps:
            schema = gap.get("schema", "")
            table = gap.get("table", "")
            gap_type = gap.get("gap_type", "")
            cols = gap.get("columns_to_add", [])
            rebuild = gap.get("requires_rebuild", False)
            detail = ""
            if gap_type == "missing_audit_table":
                detail = f"CREATE `audit.{schema}_{table}`"
            elif gap_type == "missing_columns":
                col_list = ", ".join(cols) if cols else "(none)"
                rebuild_note = " (requires rebuild)" if rebuild else ""
                detail = f"ADD COLUMN: {col_list}{rebuild_note}"
            elif gap_type == "trigger_missing":
                detail = "CREATE TRIGGER"
            lines.append(f"- `{gap_type}`: {schema}.{table} — {detail}")
    else:
        lines.append("No audit actions required.")
    lines.append("")

    # deploy.lst review: active entries drive the release script count check,
    # while commented duplicates explain intentionally skipped paths.
    lines.append("### deploy.lst")
    lines.append("")
    active_entries, commented_entries = _parse_deploy_lst(release_dir)
    if active_entries:
        lines.append(f"{len(active_entries)} file(s) queued for deploy:")
        lines.append("")
        for entry in active_entries:
            lines.append(f"- `{entry}`")
    else:
        lines.append("No active deploy entries.")
    if commented_entries:
        lines.append("")
        lines.append(f"{len(commented_entries)} duplicate(s) commented out:")
        lines.append("")
        for entry in commented_entries:
            lines.append(f"- {entry}")
    lines.append("")

    # Delete review: these paths are removed during git promotion after the
    # cherry-pick, so they are listed separately from deployable SQL files.
    lines.append("### Files to delete")
    lines.append("")
    delete_paths = _parse_delete_file(release_dir)
    if delete_paths:
        for p in delete_paths:
            lines.append(f"- `{p}`")
    else:
        lines.append("No files to delete.")
    lines.append("")

    # Cascade review: out-of-scope victims are deployment blockers because they
    # would remove objects not restored by this release.
    lines.append("### Cascade simulation")
    lines.append("")
    victims = db.get("cascade_victims", [])
    if not victims:
        lines.append("CLEAN — no cascade victims detected.")
    else:
        out_of_scope = [v for v in victims if not v.get("in_release_scope", True)]
        if out_of_scope:
            lines.append(f"{len(victims)} cascade victim(s) — {len(out_of_scope)} OUT OF SCOPE (HARD FAIL):")
        else:
            lines.append(f"{len(victims)} cascade victim(s) — all in release scope:")
        lines.append("")
        for victim in victims:
            scope = "IN SCOPE" if victim.get("in_release_scope") else "OUT OF SCOPE"
            obj_name = victim.get("object_name", "")
            obj_type = victim.get("object_type", "")
            lines.append(f"- `{obj_name}` ({obj_type}) — {scope}")
    lines.append("")

    # Warnings are non-blocking but must remain visible at the approval point.
    lines.append("### Warnings")
    lines.append("")
    if all_warnings:
        for w in all_warnings:
            lines.append(f"- {w}")
    else:
        lines.append("No warnings.")
    lines.append("")

    # Hard fails document why the pipeline already blocked automatic progress.
    lines.append("### Hard fails")
    lines.append("")
    if all_fails:
        for f in all_fails:
            lines.append(f"- {f}")
    else:
        lines.append("No hard fails.")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate 2 — test DB deployment report
# ---------------------------------------------------------------------------
#
# Gate 2 is the approval point after test DB deployment. It focuses on evidence
# from the release script, duplicate-function checks, count verification, and
# audit alignment after post.sql.

def generate_gate2_report(release_dir: str, cfg: Config) -> str:
    """Generate the Gate 2 Markdown report from test deployment artefacts.

    Reads: raw-manifest.json, test-deploy-summary.json, test-count-verify.json,
           test-audit-verify.json, test-deploy-pass1.log (all in reports/ subdirectory).
    """
    reports_dir = os.path.join(release_dir, "reports")
    manifest = _load_json(os.path.join(reports_dir, "raw-manifest.json"))
    summary = _load_json(os.path.join(reports_dir, "test-deploy-summary.json"))
    count_verify = _load_json(os.path.join(reports_dir, "test-count-verify.json"))
    audit_results = _load_json_list(os.path.join(reports_dir, "test-audit-verify.json"))
    deploy_log = os.path.join(reports_dir, "test-deploy-pass1.log")

    jira = manifest.get("jira_ticket", "UNKNOWN")
    pr = manifest.get("pr_number", "UNKNOWN")

    has_fail = summary.get("has_hard_fail", False)
    result_label = "FAIL" if has_fail else "PASS"

    lines: list[str] = []

    # Opening decision line: summarizes whether the test deployment can be
    # trusted before live dev deployment is manually approved.
    lines.append(f"## Test DB deployment: {result_label} — {jira} {pr}")
    lines.append("")

    if summary.get("fail_reasons"):
        lines.append("**Fail reasons:**")
        lines.append("")
        for reason in summary["fail_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    # Count verification protects against partial deployments or release script
    # drift from deploy.lst.
    lines.append("### Count verification")
    lines.append("")
    expected = count_verify.get("expected", summary.get("expected_file_count", "?"))
    processed = count_verify.get("processed", summary.get("processed_file_count", "?"))
    match = count_verify.get("match", summary.get("count_match", False))
    match_label = "MATCH" if match else "MISMATCH"
    lines.append(f"Expected: {expected} | Processed: {processed} — {match_label}")
    lines.append("")

    # Duplicate functions after test deployment indicate prep.sql did not remove
    # old signatures correctly.
    lines.append("### Duplicate function check")
    lines.append("")
    dups = summary.get("duplicate_functions", [])
    if dups:
        lines.append(f"{len(dups)} duplicate function(s) detected:")
        lines.append("")
        for d in dups:
            lines.append(f"- `{d['schema']}.{d['function']}` ({d['count']} overloads)")
    else:
        lines.append("CLEAN")
    lines.append("")

    # Audit verification confirms post.sql repaired or created audit structures
    # for every table mutation in scope.
    lines.append("### Audit verification")
    lines.append("")
    if audit_results:
        for r in audit_results:
            schema = r.get("schema", "")
            table = r.get("table", "")
            audit_table = r.get("audit_table", f"audit.{schema}_{table}")
            aligned = r.get("aligned", False)
            status = "ALIGNED" if aligned else "MISALIGNED"
            detail = ""
            if not aligned:
                missing = r.get("missing_in_audit", [])
                extra = r.get("extra_in_audit", [])
                parts = []
                if missing:
                    parts.append(f"missing in audit: {', '.join(missing)}")
                if extra:
                    parts.append(f"extra in audit: {', '.join(extra)}")
                detail = " — " + "; ".join(parts) if parts else ""
            lines.append(f"- {schema}.{table} -> {audit_table}: {status}{detail}")
    else:
        lines.append("No table mutations in this release.")
    lines.append("")

    # Log excerpt gives approvers immediate failure context without requiring
    # them to open the full artefact first.
    lines.append("### Log excerpt (first 50 lines of test-deploy-pass1.log)")
    lines.append("")
    log_lines = _read_lines(deploy_log)
    if log_lines:
        lines.append("```")
        for l in log_lines[:50]:
            lines.append(l.rstrip())
        lines.append("```")
    else:
        lines.append("Log not available.")
    lines.append("")
    lines.append(f"Log file: `{deploy_log}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate 3 — live DB deployment report
# ---------------------------------------------------------------------------
#
# Gate 3 is the permanent deployment record. It captures live DB result, health
# checks, duplicate-function status, audit verification, Jira transition, and
# generated artefact checksums/availability.

def generate_gate3_report(release_dir: str, cfg: Config) -> str:
    """Generate the Gate 3 Markdown report from live deployment artefacts.

    Reads: raw-manifest.json, live-deploy-summary.json, live-health-check.json,
           live-audit-verify.json, artefact-manifest.json (all in reports/ subdirectory).
    """
    reports_dir = os.path.join(release_dir, "reports")
    manifest = _load_json(os.path.join(reports_dir, "raw-manifest.json"))
    summary = _load_json(os.path.join(reports_dir, "live-deploy-summary.json"))
    health_checks = _load_json_list(os.path.join(reports_dir, "live-health-check.json"))
    audit_results = _load_json_list(os.path.join(reports_dir, "live-audit-verify.json"))
    artefact_manifest = _load_json(os.path.join(reports_dir, "artefact-manifest.json"))

    jira = manifest.get("jira_ticket", "UNKNOWN")
    pr = manifest.get("pr_number", "UNKNOWN")

    has_fail = summary.get("has_hard_fail", False)
    result_label = "FAIL" if has_fail else "PASS"

    lines: list[str] = []

    # Opening decision line: records the final live deployment outcome.
    lines.append(f"## Live DB deployment: {result_label} — {jira} {pr}")
    lines.append("")

    if summary.get("fail_reasons"):
        lines.append("**Fail reasons:**")
        lines.append("")
        for reason in summary["fail_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    # Health checks prove app-facing endpoints recovered after DB deploy and
    # service restart.
    lines.append("### GraphQL health checks")
    lines.append("")
    if health_checks:
        for hc in health_checks:
            label = hc.get("label", "")
            url = hc.get("url", "")
            success = hc.get("success", False)
            status = hc.get("last_status", "?")
            attempts = hc.get("attempts", "?")
            result = "OK" if success else "FAIL"
            lines.append(f"- {label}: {result} (HTTP {status}, {attempts} attempt(s)) — {url}")
    else:
        lines.append("Health check results not available.")
    lines.append("")

    # Duplicate function check is repeated live because test DB success is not
    # proof of live catalog state.
    lines.append("### Duplicate function check")
    lines.append("")
    dups = summary.get("duplicate_functions", [])
    if dups:
        lines.append(f"{len(dups)} duplicate function(s) detected:")
        lines.append("")
        for d in dups:
            lines.append(f"- `{d['schema']}.{d['function']}` ({d['count']} overloads)")
    else:
        lines.append("CLEAN")
    lines.append("")

    # Audit verification is repeated live to prove post.sql alignment in the
    # actual dev database.
    lines.append("### Audit verification")
    lines.append("")
    if audit_results:
        for r in audit_results:
            schema = r.get("schema", "")
            table = r.get("table", "")
            audit_table = r.get("audit_table", f"audit.{schema}_{table}")
            aligned = r.get("aligned", False)
            status = "ALIGNED" if aligned else "MISALIGNED"
            detail = ""
            if not aligned:
                missing = r.get("missing_in_audit", [])
                extra = r.get("extra_in_audit", [])
                parts = []
                if missing:
                    parts.append(f"missing in audit: {', '.join(missing)}")
                if extra:
                    parts.append(f"extra in audit: {', '.join(extra)}")
                detail = " — " + "; ".join(parts) if parts else ""
            lines.append(f"- {schema}.{table} -> {audit_table}: {status}{detail}")
    else:
        lines.append("No table mutations in this release.")
    lines.append("")

    # Jira transition is informational; deployment completion is not rolled back
    # because the ticketing API is unavailable.
    lines.append("### Jira transition")
    lines.append("")
    jira_result = summary.get("jira_transition", {})
    if not jira_result.get("attempted"):
        lines.append(f"{jira}: not configured (skipped)")
    elif jira_result.get("success"):
        status_code = jira_result.get("status_code", "")
        lines.append(f"{jira}: transitioned successfully (HTTP {status_code})")
    else:
        detail = jira_result.get("detail", "unknown error")
        lines.append(f"{jira}: transition failed — {detail}")
    lines.append("")

    # Artefact listing is the audit trail for what was generated and retained
    # for the release.
    lines.append("### Artefacts")
    lines.append("")
    files_info = artefact_manifest.get("files", [])
    if files_info:
        for item in files_info:
            fname = item.get("filename", "")
            size = item.get("size_bytes", "")
            sha = item.get("sha256", "")[:12] if item.get("sha256") else ""
            lines.append(f"- `{fname}` — {size} bytes — sha256: `{sha}...`")
    else:
        artefact_names = [
            "prep.sql", "deploy.lst", "post.sql", "delete", "NOTES.txt",
            "artefact-manifest.json",
        ]
        lines.append(f"Release directory: `{release_dir}`")
        lines.append("")
        for name in artefact_names:
            path = os.path.join(release_dir, name)
            exists = os.path.exists(path)
            marker = "present" if exists else "absent"
            lines.append(f"- `{name}` — {marker}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate 4 — prod test DB deployment report
# ---------------------------------------------------------------------------
#
# Gate 4 is the approval point before production live deployment. It mirrors
# Gate 2 but reads prod test artefacts. Infrastructure guard: if prod stages
# were skipped (EC2_HOST_PROD not set), the report reflects that clearly.

def generate_gate4_report(release_dir: str, cfg: Config) -> str:
    """Generate the Gate 4 Markdown report from prod test deployment artefacts.

    Reads: raw-manifest.json, prod-test-deploy-summary.json,
           prod-test-count-verify.json, prod-test-audit-verify.json,
           prod-test-deploy.log.
    """
    reports_dir = os.path.join(release_dir, "reports")
    manifest = _load_json(os.path.join(reports_dir, "raw-manifest.json"))
    summary = _load_json(os.path.join(reports_dir, "prod-test-deploy-summary.json"))
    count_verify = _load_json(os.path.join(reports_dir, "prod-test-count-verify.json"))
    audit_results = _load_json_list(os.path.join(reports_dir, "prod-test-audit-verify.json"))
    deploy_log = os.path.join(reports_dir, "prod-test-deploy.log")

    jira = manifest.get("jira_ticket", "UNKNOWN")
    pr = manifest.get("pr_number", "UNKNOWN")

    if summary.get("skipped"):
        lines = [
            f"## Prod test DB deployment: SKIPPED — {jira} {pr}",
            "",
            summary.get("reason", "Production infrastructure not configured."),
            "",
        ]
        return "\n".join(lines)

    has_fail = summary.get("has_hard_fail", False)
    result_label = "FAIL" if has_fail else "PASS"

    lines: list[str] = []
    lines.append(f"## Prod test DB deployment: {result_label} — {jira} {pr}")
    lines.append("")

    if summary.get("fail_reasons"):
        lines.append("**Fail reasons:**")
        lines.append("")
        for reason in summary["fail_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    lines.append("### Count verification")
    lines.append("")
    expected = count_verify.get("expected", summary.get("expected_file_count", "?"))
    processed = count_verify.get("processed", summary.get("processed_file_count", "?"))
    match = count_verify.get("match", summary.get("count_match", False))
    match_label = "MATCH" if match else "MISMATCH"
    lines.append(f"Expected: {expected} | Processed: {processed} — {match_label}")
    lines.append("")

    lines.append("### Duplicate function check")
    lines.append("")
    dups = summary.get("duplicate_functions", [])
    if dups:
        lines.append(f"{len(dups)} duplicate function(s) detected:")
        lines.append("")
        for d in dups:
            lines.append(f"- `{d['schema']}.{d['function']}` ({d['count']} overloads)")
    else:
        lines.append("CLEAN")
    lines.append("")

    lines.append("### Audit verification")
    lines.append("")
    if audit_results:
        for r in audit_results:
            schema = r.get("schema", "")
            table = r.get("table", "")
            audit_table = r.get("audit_table", f"audit.{schema}_{table}")
            aligned = r.get("aligned", False)
            status = "ALIGNED" if aligned else "MISALIGNED"
            detail = ""
            if not aligned:
                missing = r.get("missing_in_audit", [])
                extra = r.get("extra_in_audit", [])
                parts = []
                if missing:
                    parts.append(f"missing in audit: {', '.join(missing)}")
                if extra:
                    parts.append(f"extra in audit: {', '.join(extra)}")
                detail = " — " + "; ".join(parts) if parts else ""
            lines.append(f"- {schema}.{table} -> {audit_table}: {status}{detail}")
    else:
        lines.append("No table mutations in this release.")
    lines.append("")

    lines.append("### Log excerpt (first 50 lines of prod-test-deploy.log)")
    lines.append("")
    log_lines = _read_lines(deploy_log)
    if log_lines:
        lines.append("```")
        for l in log_lines[:50]:
            lines.append(l.rstrip())
        lines.append("```")
    else:
        lines.append("Log not available.")
    lines.append("")
    lines.append(f"Log file: `{deploy_log}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate 5 — prod live DB deployment report
# ---------------------------------------------------------------------------
#
# Gate 5 is the permanent production deployment record. It mirrors Gate 3 but
# reads prod live artefacts and includes prod-specific health check and restart
# information.

def generate_gate5_report(release_dir: str, cfg: Config) -> str:
    """Generate the Gate 5 Markdown report from prod live deployment artefacts.

    Reads: raw-manifest.json, prod-live-deploy-summary.json,
           prod-live-health-check.json, prod-live-audit-verify.json.
    """
    reports_dir = os.path.join(release_dir, "reports")
    manifest = _load_json(os.path.join(reports_dir, "raw-manifest.json"))
    summary = _load_json(os.path.join(reports_dir, "prod-live-deploy-summary.json"))
    health_checks = _load_json_list(os.path.join(reports_dir, "prod-live-health-check.json"))
    audit_results = _load_json_list(os.path.join(reports_dir, "prod-live-audit-verify.json"))

    jira = manifest.get("jira_ticket", "UNKNOWN")
    pr = manifest.get("pr_number", "UNKNOWN")

    if summary.get("skipped"):
        lines = [
            f"## Prod live DB deployment: SKIPPED — {jira} {pr}",
            "",
            summary.get("reason", "Production infrastructure not configured."),
            "",
        ]
        return "\n".join(lines)

    has_fail = summary.get("has_hard_fail", False)
    result_label = "FAIL" if has_fail else "PASS"

    lines: list[str] = []
    lines.append(f"## Prod live DB deployment: {result_label} — {jira} {pr}")
    lines.append("")

    if summary.get("fail_reasons"):
        lines.append("**Fail reasons:**")
        lines.append("")
        for reason in summary["fail_reasons"]:
            lines.append(f"- {reason}")
        lines.append("")

    lines.append("### Production GraphQL health checks")
    lines.append("")
    non_skipped = [hc for hc in health_checks if not hc.get("skipped")]
    if non_skipped:
        for hc in non_skipped:
            label = hc.get("label", "")
            url = hc.get("url", "")
            success = hc.get("success", False)
            status = hc.get("last_status", "?")
            attempts = hc.get("attempts", "?")
            result = "OK" if success else "FAIL"
            lines.append(f"- {label}: {result} (HTTP {status}, {attempts} attempt(s)) — {url}")
    elif health_checks:
        lines.append("All health check endpoints skipped (not configured).")
    else:
        lines.append("Health check results not available.")
    lines.append("")

    lines.append("### Prod node restarts")
    lines.append("")
    restarts = summary.get("node_restarts", [])
    if restarts:
        for r in restarts:
            if r.get("skipped"):
                lines.append("- No prod nodes configured (PROD_NODES not set).")
            else:
                rc = r.get("returncode", "?")
                status = "OK" if rc == 0 else f"FAILED (exit {rc})"
                lines.append(f"- {r.get('node', '?')}: {status}")
    else:
        lines.append("No restart information available.")
    lines.append("")

    lines.append("### Duplicate function check")
    lines.append("")
    dups = summary.get("duplicate_functions", [])
    if dups:
        lines.append(f"{len(dups)} duplicate function(s) detected:")
        lines.append("")
        for d in dups:
            lines.append(f"- `{d['schema']}.{d['function']}` ({d['count']} overloads)")
    else:
        lines.append("CLEAN")
    lines.append("")

    lines.append("### Audit verification")
    lines.append("")
    if audit_results:
        for r in audit_results:
            schema = r.get("schema", "")
            table = r.get("table", "")
            audit_table = r.get("audit_table", f"audit.{schema}_{table}")
            aligned = r.get("aligned", False)
            status = "ALIGNED" if aligned else "MISALIGNED"
            detail = ""
            if not aligned:
                missing = r.get("missing_in_audit", [])
                extra = r.get("extra_in_audit", [])
                parts = []
                if missing:
                    parts.append(f"missing in audit: {', '.join(missing)}")
                if extra:
                    parts.append(f"extra in audit: {', '.join(extra)}")
                detail = " — " + "; ".join(parts) if parts else ""
            lines.append(f"- {schema}.{table} -> {audit_table}: {status}{detail}")
    else:
        lines.append("No table mutations in this release.")
    lines.append("")

    lines.append("### Jira transition")
    lines.append("")
    jira_result = summary.get("jira_transition", {})
    if not jira_result.get("attempted"):
        lines.append(f"{jira}: not configured (skipped)")
    elif jira_result.get("success"):
        status_code = jira_result.get("status_code", "")
        lines.append(f"{jira}: transitioned to deployed-to-production (HTTP {status_code})")
    else:
        detail = jira_result.get("detail", "unknown error")
        lines.append(f"{jira}: transition failed — {detail}")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# GitHub API
# ---------------------------------------------------------------------------
#
# Posting is deliberately separate from report generation. Tests can validate
# Markdown content without network access, and the CLI can print reports even
# when the GitHub API call fails.

def post_pr_comment(report: str, pr_number: int, cfg: Config) -> dict:
    """Post report as a comment on the GitHub PR identified by pr_number.

    Returns a result dict with 'success', 'status_code', and 'detail'.
    Raises on hard errors only if the caller explicitly checks; otherwise logs.
    """
    repository = getattr(cfg, "github_repository", "")
    url = f"{cfg.github_api_url}/repos/{repository}/issues/{pr_number}/comments"
    payload = json.dumps({"body": report}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {cfg.github_token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {
                "success": True,
                "status_code": resp.status,
                "detail": f"HTTP {resp.status}",
            }
    except urllib.error.HTTPError as exc:
        log.error("GitHub API returned HTTP %d when posting PR comment.", exc.code)
        return {
            "success": False,
            "status_code": exc.code,
            "detail": f"HTTP {exc.code}: {exc.reason}",
        }
    except (urllib.error.URLError, OSError) as exc:
        log.error("GitHub API network error: %s", exc)
        return {
            "success": False,
            "status_code": None,
            "detail": str(exc),
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
#
# The CLI is used by CI gate jobs. It always prints the generated report so
# the job log remains useful, and optionally posts the same Markdown to the PR.

def main() -> None:
    """CLI entry point for report generation and posting."""
    parser = argparse.ArgumentParser(
        description="Generate and post a pipeline gate report to the GitHub PR."
    )
    parser.add_argument(
        "gate",
        choices=["gate1", "gate2", "gate3", "gate4", "gate5"],
        help="Which gate report to generate.",
    )
    parser.add_argument(
        "release_dir",
        help="Path to the release directory containing pipeline artefacts.",
    )
    parser.add_argument(
        "--post",
        action="store_true",
        help="Post the report as a GitHub PR comment (requires GITHUB_TOKEN).",
    )
    parser.add_argument(
        "--pr-number",
        "--mr-iid",
        dest="pr_number",
        type=int,
        help=(
            "GitHub PR number to post to. "
            "Auto-extracted from the manifest PR number if omitted."
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

    generators = {
        "gate1": generate_gate1_report,
        "gate2": generate_gate2_report,
        "gate3": generate_gate3_report,
        "gate4": generate_gate4_report,
        "gate5": generate_gate5_report,
    }
    report = generators[args.gate](args.release_dir, cfg)
    sys.stdout.write(report)
    sys.stdout.write("\n")

    if args.post:
        pr_number = args.pr_number
        if pr_number is None:
            manifest = _load_json(
                os.path.join(args.release_dir, "reports", "raw-manifest.json")
            )
            pr_number = _extract_pr_number(manifest.get("pr_number", ""))
            commit_sha = manifest.get("commit_hash", "") or os.environ.get("GITHUB_SHA", "")
            if commit_sha:
                pr_number = pr_number or _lookup_pr_number(commit_sha, cfg)
        if pr_number is None:
            log.error(
                "Cannot post: PR number could not be determined. "
                "Pass --pr-number or ensure GITHUB_SHA resolves to an open PR."
            )
            sys.exit(1)

        log.info("Posting report to PR #%d.", pr_number)
        result = post_pr_comment(report, pr_number, cfg)
        if result["success"]:
            log.info("Comment posted successfully (%s).", result["detail"])
        else:
            log.error("Failed to post comment: %s", result["detail"])
            sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
