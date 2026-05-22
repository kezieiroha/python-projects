"""
Stage 2: SQL static analysis.

Reads each SQL file listed in the manifest and performs a series of checks that
require no database connection. Hard fails and warnings are recorded back onto the
manifest. Extracted function signatures, type definitions, and table mutations are
written to static-analysis-report.json for consumption by Stage 3.

Hard fail conditions (any one blocks deployment):
  - SET ROLE "acme_admin"; is not the first statement in a file
  - Privilege escalation keywords found (GRANT, REVOKE, CREATE USER, etc.)
  - CREATE TABLE or ALTER TABLE found in a non-schema file
  - DROP FUNCTION/TYPE/PROCEDURE found in a schema or function file

Warnings (non-blocking, surfaced in Gate 1 report):
  - CASCADE keyword detected outside a DROP statement context

Extractions (no pass/fail, data for Stage 3):
  - Function signatures from CREATE [OR REPLACE] FUNCTION statements
  - Type definitions from CREATE TYPE statements
  - Table mutations from CREATE TABLE and ALTER TABLE statements
"""

import argparse
import json
import os
import re
import sys

# Allow direct execution as a script from the stages/ directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.config import Config, ConfigError
from pipeline.logger import configure_logging, get_logger
from pipeline.models import FunctionSignature, Manifest, SqlFile, TableMutation

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Comment stripping
# ---------------------------------------------------------------------------
#
# Some checks need to ignore SQL comments so header blocks and explanatory
# comments do not look like executable statements.

_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")


def _strip_sql_comments(sql: str) -> str:
    """Remove SQL block (/* */) and line (--) comments from a string."""
    sql = _BLOCK_COMMENT_RE.sub("", sql)
    sql = _LINE_COMMENT_RE.sub("", sql)
    return sql


# ---------------------------------------------------------------------------
# Check 1: SET ROLE
# ---------------------------------------------------------------------------
#
# SET ROLE must be the first executable statement so every release file runs
# under the expected deployment role before any DDL or function body executes.

_SET_ROLE_RE = re.compile(r'SET\s+ROLE\s+"acme_admin"\s*;', re.IGNORECASE)


def check_set_role(relative_path: str, content: str) -> list[str]:
    """Return fail reasons if SET ROLE "acme_admin"; is not the first statement.

    Strips all comments and leading whitespace before testing, so header comment
    blocks do not affect the result.
    """
    stripped = _strip_sql_comments(content).lstrip()
    if not _SET_ROLE_RE.match(stripped):
        return [
            f'Missing SET ROLE "acme_admin"; as first statement: {relative_path}'
        ]
    return []


# ---------------------------------------------------------------------------
# Check 2: Privilege escalation
# ---------------------------------------------------------------------------
#
# Release SQL must not grant privileges or create/alter roles. This scan is
# intentionally performed on raw content, including comments, so reviewers see
# suspicious privilege text even if it is not executable.

# Each entry is (display_label, compiled_pattern).
# Scanned against raw file content including comments — a security check must
# not be bypassable by hiding a keyword inside a comment.
_PRIV_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("GRANT", re.compile(r"\bGRANT\b", re.IGNORECASE)),
    ("REVOKE", re.compile(r"\bREVOKE\b", re.IGNORECASE)),
    ("CREATE USER", re.compile(r"\bCREATE\s+USER\b", re.IGNORECASE)),
    ("ALTER ROLE", re.compile(r"\bALTER\s+ROLE\b", re.IGNORECASE)),
    ("SUPERUSER", re.compile(r"\bSUPERUSER\b", re.IGNORECASE)),
    ("CREATE ROLE", re.compile(r"\bCREATE\s+ROLE\b", re.IGNORECASE)),
]


def check_privilege_escalation(relative_path: str, content: str) -> list[str]:
    """Return fail reasons for any privilege-escalation keywords found in the file."""
    fails = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        for label, pattern in _PRIV_PATTERNS:
            if pattern.search(line):
                fails.append(
                    f"Privilege escalation keyword '{label}' at "
                    f"{relative_path}:{line_no}"
                )
                # One fail per line — report the first keyword matched.
                break
    return fails


# ---------------------------------------------------------------------------
# Check 3: DDL in wrong file type
# ---------------------------------------------------------------------------
#
# Table DDL belongs in schema-classified files. Enforcing that boundary keeps
# deploy order predictable and prevents hidden table changes in function/type
# releases.

_DDL_RE = re.compile(r"\b(CREATE\s+TABLE|ALTER\s+TABLE)\b", re.IGNORECASE)


def check_ddl_in_wrong_file(
    relative_path: str, classification: str, content: str
) -> list[str]:
    """Return fail reasons if CREATE TABLE or ALTER TABLE appears in a non-schema file."""
    if classification == "schema":
        return []
    stripped = _strip_sql_comments(content)
    fails = []
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        if _DDL_RE.search(line):
            fails.append(
                f"DDL statement (CREATE/ALTER TABLE) in {classification} file "
                f"{relative_path}:{line_no}"
            )
    return fails


# ---------------------------------------------------------------------------
# Check 4: DROP in wrong location
# ---------------------------------------------------------------------------
#
# Function/type/procedure drops are generated into prep.sql after DB-assisted
# safety checks. Hand-written drops in normal release files bypass that process.

_DROP_FORBIDDEN_RE = re.compile(
    r"\bDROP\s+(FUNCTION|TYPE|PROCEDURE)\b", re.IGNORECASE
)


def check_drop_in_wrong_location(
    relative_path: str, classification: str, content: str
) -> list[str]:
    """Return fail reasons if DROP FUNCTION/TYPE/PROCEDURE appears in a schema or function file.

    These statements belong in prep.sql only and must not appear in regular release files.
    """
    if classification not in ("schema", "function"):
        return []
    stripped = _strip_sql_comments(content)
    fails = []
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        if _DROP_FORBIDDEN_RE.search(line):
            fails.append(
                f"DROP statement outside prep.sql in {classification} file "
                f"{relative_path}:{line_no}"
            )
    return fails


# ---------------------------------------------------------------------------
# Check 5: CASCADE warning
# ---------------------------------------------------------------------------
#
# CASCADE outside an expected DROP context can imply broader side effects.
# It is surfaced as a warning so DevOps can review intent without blocking
# harmless mentions.

_CASCADE_RE = re.compile(r"\bCASCADE\b", re.IGNORECASE)
_DROP_RE = re.compile(r"\bDROP\b", re.IGNORECASE)


def check_cascade_warnings(relative_path: str, content: str) -> list[str]:
    """Return warnings for CASCADE keyword found outside a DROP statement context.

    Scans raw lines including SQL comments — any mention of CASCADE that is not
    on the same line as DROP is considered worth surfacing for DevOps review.
    DROP ... CASCADE is expected and intentional; CASCADE elsewhere needs review.
    """
    warnings = []
    for line_no, line in enumerate(content.splitlines(), start=1):
        if _CASCADE_RE.search(line) and not _DROP_RE.search(line):
            warnings.append(
                f"CASCADE keyword outside DROP context at {relative_path}:{line_no}"
            )
    return warnings


# ---------------------------------------------------------------------------
# Extraction 6: Function signatures
# ---------------------------------------------------------------------------
#
# Function signatures feed Stage 3 catalog comparison. A changed signature does
# not fail static analysis; it tells Stage 3 whether prep.sql needs an explicit
# DROP for the old function identity.

# Matches CREATE [OR REPLACE] FUNCTION [schema.]name(params) ... RETURNS type
# Identifiers may be double-quoted ("schema"."name") or unquoted (schema.name).
# [^;]*? stops before the next semicolon, preventing cross-function matching.
_FUNC_RE = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+"
    r'(?:"?(\w+)"?\.)?'
    r'"?(\w+)"?\s*'
    r"\(([^)]*)\)"
    r"[^;]*?"
    r"\bRETURNS\b\s+"
    r'(TABLE\s*\([^)]+\)|SETOF\s+\S+|"?\w+"?(?:\[\])?)',
    re.IGNORECASE | re.DOTALL,
)


def _extract_param_types(params_str: str) -> list[str]:
    """Extract the data type of each parameter from a raw parameter list string.

    Handles: plain types, named parameters ("name type"), mode-prefixed parameters
    ("IN name type", "OUT name type", "INOUT name type", "VARIADIC name type"),
    and default-value parameters ("name type DEFAULT val").
    """
    if not params_str.strip():
        return []
    types = []
    for param in params_str.split(","):
        param = param.strip()
        if not param:
            continue
        # Strip DEFAULT clause before parsing tokens.
        param = re.split(r"\bDEFAULT\b", param, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        tokens = param.split()
        if not tokens:
            continue
        # Leading mode keyword (IN/OUT/INOUT/VARIADIC) is not the type.
        mode_keywords = {"in", "out", "inout", "variadic"}
        if tokens[0].lower() in mode_keywords:
            tokens = tokens[1:]
        if not tokens:
            continue
        # If two or more tokens remain, format is "name type"; last token is the type.
        # If one token remains, it is an anonymous parameter and IS the type.
        types.append(tokens[-1])
    return types


def extract_function_signatures(
    relative_path: str, content: str
) -> list[FunctionSignature]:
    """Extract all function signatures from a SQL file using regex.

    Returns one FunctionSignature per CREATE [OR REPLACE] FUNCTION statement found.
    Skips matches that cannot be fully parsed rather than raising an exception.
    """
    # Strip block comments before matching to avoid false negatives from comment text.
    stripped = _BLOCK_COMMENT_RE.sub("", content)
    signatures = []
    for match in _FUNC_RE.finditer(stripped):
        schema = match.group(1) or "public"
        name = match.group(2)
        params_str = match.group(3)
        returns_raw = match.group(4).strip()
        param_types = _extract_param_types(params_str)
        log.debug(
            "Extracted function signature: %s.%s(%s) RETURNS %s",
            schema,
            name,
            ", ".join(param_types),
            returns_raw,
        )
        signatures.append(
            FunctionSignature(
                schema=schema,
                name=name,
                param_types=param_types,
                return_type=returns_raw,
                source_file=relative_path,
            )
        )
    return signatures


# ---------------------------------------------------------------------------
# Extraction 7: Type definitions
# ---------------------------------------------------------------------------
#
# Type definitions are tracked because type changes can affect function drop
# ordering and cascade safety.

_TYPE_RE = re.compile(
    r'\bCREATE\s+TYPE\s+(?:"?(\w+)"?\.)?\"?(\w+)\"?\b',
    re.IGNORECASE,
)


def extract_type_definitions(relative_path: str, content: str) -> list[dict]:
    """Extract all CREATE TYPE definitions from a SQL file.

    Returns a list of dicts with keys: schema, name, source_file.
    """
    stripped = _BLOCK_COMMENT_RE.sub("", content)
    definitions = []
    for match in _TYPE_RE.finditer(stripped):
        definitions.append(
            {
                "schema": match.group(1) or "public",
                "name": match.group(2),
                "source_file": relative_path,
            }
        )
    return definitions


# ---------------------------------------------------------------------------
# Extraction 8: Table mutations
# ---------------------------------------------------------------------------
#
# Table mutations feed audit-gap analysis. Only the mutation shape is extracted
# here; Stage 3 uses the database to decide what audit repair is required.

_CREATE_TABLE_RE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:(\w+)\.)?(\w+)\s*\(",
    re.IGNORECASE,
)
_ALTER_ADD_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:(\w+)\.)?(\w+)\s+"
    r"ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)
_ALTER_DROP_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:(\w+)\.)?(\w+)\s+"
    r"DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?(\w+)",
    re.IGNORECASE,
)
_ALTER_RENAME_RE = re.compile(
    r"\bALTER\s+TABLE\s+(?:(\w+)\.)?(\w+)\s+"
    r"RENAME\s+COLUMN\s+(\w+)\s+TO\s+(\w+)",
    re.IGNORECASE,
)


def extract_table_mutations(relative_path: str, content: str) -> list[TableMutation]:
    """Extract all table DDL mutations from a SQL file.

    Processes CREATE TABLE, ALTER TABLE ADD COLUMN, DROP COLUMN, and RENAME COLUMN.
    """
    stripped = _strip_sql_comments(content)
    mutations: list[TableMutation] = []

    for match in _CREATE_TABLE_RE.finditer(stripped):
        mutations.append(
            TableMutation(
                schema=match.group(1) or "public",
                table=match.group(2),
                mutation_type="create_table",
                columns_added=[],
                columns_dropped=[],
                source_file=relative_path,
            )
        )

    for match in _ALTER_ADD_RE.finditer(stripped):
        mutations.append(
            TableMutation(
                schema=match.group(1) or "public",
                table=match.group(2),
                mutation_type="add_column",
                columns_added=[match.group(3)],
                columns_dropped=[],
                source_file=relative_path,
            )
        )

    for match in _ALTER_DROP_RE.finditer(stripped):
        mutations.append(
            TableMutation(
                schema=match.group(1) or "public",
                table=match.group(2),
                mutation_type="drop_column",
                columns_added=[],
                columns_dropped=[match.group(3)],
                source_file=relative_path,
            )
        )

    for match in _ALTER_RENAME_RE.finditer(stripped):
        mutations.append(
            TableMutation(
                schema=match.group(1) or "public",
                table=match.group(2),
                mutation_type="rename_column",
                # columns_added holds the new name, columns_dropped holds the old name.
                columns_added=[match.group(4)],
                columns_dropped=[match.group(3)],
                source_file=relative_path,
            )
        )

    return mutations


# ---------------------------------------------------------------------------
# Check 9: Deploy ordering
# ---------------------------------------------------------------------------
#
# Deploy order is deterministic and stable within each class. Schema changes
# must land before types/functions that may depend on them.

_CLASSIFICATION_ORDER: dict[str, int] = {
    "schema": 0,
    "type": 1,
    "function": 2,
    "config": 3,
    "serverless": 4,
}


def sort_sql_files(sql_files: list[SqlFile]) -> list[SqlFile]:
    """Sort sql_files by deploy order: schema, type, function, config, serverless.

    Python's sort is stable so original order is preserved within each group.
    """
    return sorted(
        sql_files,
        key=lambda f: _CLASSIFICATION_ORDER.get(f.classification, 99),
    )


# ---------------------------------------------------------------------------
# Per-file analysis
# ---------------------------------------------------------------------------
#
# Per-file analysis keeps all checks independent and accumulates failures rather
# than stopping at the first problem, giving Gate 1 a complete review list.

def _resolve_file_path(sql_file: SqlFile, cfg: Config) -> str:
    """Return the absolute path to a SQL file on the runner.

    Serverless-classified files are resolved from git_repo_serverless when
    that repo is configured; all others come from git_repo_dataschema.
    """
    if sql_file.classification == "serverless" and cfg.serverless_configured():
        return os.path.join(cfg.git_repo_serverless, sql_file.relative_path)
    return os.path.join(cfg.git_repo_dataschema, sql_file.relative_path)


def analyse_file(
    sql_file: SqlFile,
    cfg: Config,
) -> tuple[list[str], list[str], list[FunctionSignature], list[dict], list[TableMutation]]:
    """Run all checks and extractions on a single SQL file.

    Returns:
        (fail_reasons, warnings, function_signatures, type_definitions, table_mutations)
    """
    file_path = _resolve_file_path(sql_file, cfg)

    try:
        with open(file_path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        return (
            [f"Cannot read file {sql_file.relative_path}: {exc}"],
            [],
            [],
            [],
            [],
        )

    fails: list[str] = []
    warnings: list[str] = []

    fails.extend(check_set_role(sql_file.relative_path, content))
    fails.extend(check_privilege_escalation(sql_file.relative_path, content))
    fails.extend(
        check_ddl_in_wrong_file(sql_file.relative_path, sql_file.classification, content)
    )
    fails.extend(
        check_drop_in_wrong_location(
            sql_file.relative_path, sql_file.classification, content
        )
    )
    warnings.extend(check_cascade_warnings(sql_file.relative_path, content))

    signatures = extract_function_signatures(sql_file.relative_path, content)
    type_defs = extract_type_definitions(sql_file.relative_path, content)
    mutations = extract_table_mutations(sql_file.relative_path, content)

    return fails, warnings, signatures, type_defs, mutations


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------
#
# Stage 2 updates the manifest with static warnings/failures and emits a report
# containing extracted facts for later DB-assisted analysis.

def run(manifest: Manifest, cfg: Config) -> tuple[Manifest, dict]:
    """Run Stage 2 static analysis on all active SQL files in the manifest.

    Appends fail_reasons and warnings to the manifest and sets has_hard_fail.
    Sorts sql_files by deploy order in place. Returns the updated manifest and
    the full analysis report as a dictionary ready for JSON serialisation.
    """
    all_fails: list[str] = []
    all_warnings: list[str] = []
    all_signatures: list[FunctionSignature] = []
    all_type_defs: list[dict] = []
    all_mutations: list[TableMutation] = []

    active_files = [f for f in manifest.sql_files if not f.is_deleted]

    for sql_file in active_files:
        log.info(
            "Analysing %s (classification: %s)",
            sql_file.relative_path,
            sql_file.classification,
        )
        fails, warnings, signatures, type_defs, mutations = analyse_file(sql_file, cfg)
        all_fails.extend(fails)
        all_warnings.extend(warnings)
        all_signatures.extend(signatures)
        all_type_defs.extend(type_defs)
        all_mutations.extend(mutations)

    manifest.sql_files = sort_sql_files(manifest.sql_files)

    if all_fails:
        manifest.has_hard_fail = True
        manifest.fail_reasons.extend(all_fails)
        for reason in all_fails:
            log.error("HARD FAIL: %s", reason)

    for warning in all_warnings:
        log.warning("%s", warning)

    manifest.warnings.extend(all_warnings)

    report = {
        "stage": "s2_static_analysis",
        "hard_fails": all_fails,
        "warnings": all_warnings,
        "function_signatures": [s.to_dict() for s in all_signatures],
        "type_definitions": all_type_defs,
        "table_mutations": [m.to_dict() for m in all_mutations],
        "deploy_order": [f.relative_path for f in manifest.sql_files],
    }

    return manifest, report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
#
# The CLI reads the raw manifest from Stage 1, writes static-analysis-report.json,
# rewrites the manifest with accumulated state, and exits non-zero on hard fails.

def main() -> None:
    """CLI entry point for Stage 2 static analysis."""
    parser = argparse.ArgumentParser(
        description="Stage 2: SQL static analysis. No database connection required."
    )
    parser.add_argument(
        "manifest_path",
        help="Path to raw-manifest.json produced by Stage 1.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable DEBUG-level logging.",
    )
    args = parser.parse_args()

    configure_logging(verbose=args.verbose)

    try:
        cfg = Config()
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        sys.exit(1)

    log.info("Loading manifest from %s", args.manifest_path)
    try:
        with open(args.manifest_path, "r", encoding="utf-8") as fh:
            manifest = Manifest.from_dict(json.load(fh))
    except (OSError, KeyError, ValueError) as exc:
        log.error("Failed to load manifest: %s", exc)
        sys.exit(1)

    if not manifest.sql_changes:
        log.info("Passthrough release — no SQL changes. Skipping.")
        sys.exit(0)

    manifest, report = run(manifest, cfg)

    release_dir = manifest.release_dir or os.path.join(
        cfg.releases_base_dir, manifest.ticket_number or "unknown"
    )
    reports_dir = manifest.reports_dir or os.path.join(release_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    report_path = os.path.join(reports_dir, "static-analysis-report.json")
    manifest_out = os.path.join(reports_dir, "raw-manifest.json")

    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        log.info("Static analysis report written to %s", report_path)

        with open(manifest_out, "w", encoding="utf-8") as fh:
            json.dump(manifest.to_dict(), fh, indent=2)
        log.info("Updated manifest written to %s", manifest_out)
    except OSError as exc:
        log.error("Failed to write output files: %s", exc)
        sys.exit(1)

    if manifest.has_hard_fail:
        log.error(
            "Stage 2 completed with %d hard fail(s). Deployment is blocked.",
            len(manifest.fail_reasons),
        )
        sys.exit(1)

    log.info(
        "Stage 2 complete. %d file(s) analysed, %d warning(s).",
        len(active_files := [f for f in manifest.sql_files if not f.is_deleted]),
        len(manifest.warnings),
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
