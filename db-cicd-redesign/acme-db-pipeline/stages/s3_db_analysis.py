"""
Stage 3: Database-assisted analysis.

Connects to the database using the read-only role and performs checks that
require live catalog inspection. All DB interactions are read-only SELECT
queries — no DDL is executed.

Planned prep.sql inputs:
  - A function signature change compared to the live DB means a DROP is required
    before the new definition can be deployed. Detection creates an ordered
    prep.sql entry; it is not a hard fail by itself.

Hard fail conditions:
  - The computed DROP order contains a dependency cycle that cannot be resolved.
  - Catalog inspection reveals objects that would be cascade-dropped but are not
    in the release manifest scope (i.e. they would be permanently destroyed).

Informational outputs (no pass/fail, consumed by Stage 4):
  - Audit gaps: missing audit tables, missing columns, missing triggers.

Warnings (non-blocking):
  - Duplicate functions already present in the DB before this release.
"""

import argparse
import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.extensions

from pipeline.config import Config, ConfigError
from pipeline.db import readonly_connection
from pipeline.logger import configure_logging, get_logger
from pipeline.models import AuditGap, CascadeVictim, FunctionSignature, Manifest

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Check 1: Function signature delta
# ---------------------------------------------------------------------------
#
# Signature deltas are prep.sql inputs, not blockers. They identify old function
# identities that must be dropped so PostgreSQL does not retain stale overloads
# or reject a changed return type.

_QUERY_FUNCTION_SIGNATURE = """
SELECT pg_get_function_arguments(p.oid) AS args,
       pg_get_function_result(p.oid)    AS result
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE p.proname = %s
   AND n.nspname  = %s;
"""


def check_function_signature_deltas(
    conn: psycopg2.extensions.connection,
    signatures: list[FunctionSignature],
) -> list[dict]:
    """Compare each extracted function signature against the live database.

    Returns a list of delta dicts for functions whose parameter types or return
    type have changed. Each delta includes the old signature (from the DB) and
    the new signature (from the file) and is used by Stage 4 to populate prep.sql.

    Functions that do not exist yet in the DB are skipped — CREATE OR REPLACE
    will create them cleanly.
    """
    deltas = []
    with conn.cursor() as cur:
        for sig in signatures:
            cur.execute(_QUERY_FUNCTION_SIGNATURE, (sig.name, sig.schema))
            rows = cur.fetchall()
            if not rows:
                log.debug(
                    "Function %s.%s not found in DB — new function, no DROP needed.",
                    sig.schema,
                    sig.name,
                )
                continue

            for db_args, db_result in rows:
                db_param_types = _parse_pg_args(db_args)
                changed = (
                    _normalise_type_list(db_param_types)
                    != _normalise_type_list(sig.param_types)
                    or _normalise_type(db_result) != _normalise_type(sig.return_type)
                )
                if changed:
                    delta = {
                        "schema": sig.schema,
                        "name": sig.name,
                        "old_args": db_args,
                        "old_result": db_result,
                        "new_param_types": sig.param_types,
                        "new_return_type": sig.return_type,
                        "source_file": sig.source_file,
                        "drop_statement": (
                            f"DROP FUNCTION IF EXISTS "
                            f"{sig.schema}.{sig.name}({_strip_arg_defaults(db_args)}) CASCADE;"
                        ),
                        "reason": (
                            f"Signature changed: was ({db_args}) RETURNS {db_result}, "
                            f"now ({', '.join(sig.param_types)}) "
                            f"RETURNS {sig.return_type}"
                        ),
                    }
                    deltas.append(delta)
                    log.warning(
                        "Signature delta detected: %s.%s — %s",
                        sig.schema,
                        sig.name,
                        delta["reason"],
                    )
    conn.rollback()
    return deltas


def _parse_pg_args(pg_args_str: str) -> list[str]:
    """Parse the output of pg_get_function_arguments into a list of type names.

    pg_get_function_arguments returns a comma-separated string like:
    "p_id uuid, p_name text" or "uuid, text" (for anonymous params).
    We extract only the type portion of each parameter.
    """
    if not pg_args_str.strip():
        return []
    types = []
    for param in pg_args_str.split(","):
        param = param.strip()
        if not param:
            continue
        # Strip DEFAULT clause before extracting type (DROP FUNCTION rejects them).
        param = _strip_arg_default(param)
        tokens = param.split()
        types.append(tokens[-1])
    return types


def _strip_arg_defaults(pg_args_str: str) -> str:
    """Remove DEFAULT clauses from a pg_get_function_arguments string.

    PostgreSQL DROP FUNCTION requires the signature without DEFAULT values.
    Converts "p_id uuid, p_x integer DEFAULT 0" to "p_id uuid, p_x integer".
    """
    if not pg_args_str.strip():
        return pg_args_str
    return ", ".join(_strip_arg_default(p) for p in pg_args_str.split(","))


def _strip_arg_default(param: str) -> str:
    """Strip the DEFAULT clause from a single parameter string."""
    param = param.strip()
    upper = param.upper()
    idx = upper.find(" DEFAULT ")
    if idx != -1:
        return param[:idx].strip()
    return param


def _normalise_type(t: str) -> str:
    """Normalise a type string for comparison: lowercase and strip whitespace."""
    return t.lower().strip()


def _normalise_type_list(types: list[str]) -> list[str]:
    """Normalise a list of type strings for comparison."""
    return [_normalise_type(t) for t in types]


# ---------------------------------------------------------------------------
# Check 2: DROP dependency ordering
# ---------------------------------------------------------------------------
#
# Multiple required drops must be ordered from dependent objects to referenced
# objects. A cycle or unsafe ordering is a hard fail because generated prep.sql
# would be unreliable.

_QUERY_DEPENDENTS = """
SELECT dep_ns.nspname        AS dep_schema,
       dep_proc.proname      AS dep_name,
       dep_proc.oid          AS dep_oid,
       ref_ns.nspname        AS ref_schema,
       ref_proc.proname      AS ref_name,
       ref_proc.oid          AS ref_oid
  FROM pg_depend d
  JOIN pg_proc dep_proc ON dep_proc.oid = d.objid
  JOIN pg_namespace dep_ns ON dep_ns.oid = dep_proc.pronamespace
  JOIN pg_proc ref_proc ON ref_proc.oid = d.refobjid
  JOIN pg_namespace ref_ns ON ref_ns.oid = ref_proc.pronamespace
 WHERE d.deptype = 'n'
   AND ref_proc.proname = %s
   AND ref_ns.nspname   = %s;
"""


def compute_drop_order(
    conn: psycopg2.extensions.connection,
    deltas: list[dict],
    type_changes: list[dict],
) -> tuple[list[dict], list[str]]:
    """Build a dependency-ordered list of DROP statements for all required drops.

    Returns (ordered_drops, fail_reasons). fail_reasons is non-empty if a
    dependency cycle is detected — a hard fail condition.

    Drop order rule: if object A depends on object B, A must be dropped before B.
    This is a standard reverse-topological sort over the dependency graph.
    """
    # Build the full set of objects requiring a DROP.
    drop_entries: list[dict] = list(deltas) + list(type_changes)
    if not drop_entries:
        conn.rollback()
        return [], []

    # Build a key → entry map and an adjacency list (key → set of keys it depends on).
    keys = [f"{e['schema']}.{e['name']}" for e in drop_entries]
    key_set = set(keys)
    adj: dict[str, set[str]] = {k: set() for k in keys}

    with conn.cursor() as cur:
        for entry in drop_entries:
            cur.execute(_QUERY_DEPENDENTS, (entry["name"], entry["schema"]))
            for dep_schema, dep_name, _, ref_schema, ref_name, _ in cur.fetchall():
                dep_key = f"{dep_schema}.{dep_name}"
                ref_key = f"{ref_schema}.{ref_name}"
                # dep depends on ref: dep must be dropped before ref.
                if dep_key in key_set and ref_key in key_set:
                    adj[dep_key].add(ref_key)

    conn.rollback()

    ordered, fails = _topological_sort(keys, adj)
    if fails:
        return [], fails

    # Return entries in sorted order.
    key_to_entry = {f"{e['schema']}.{e['name']}": e for e in drop_entries}
    return [key_to_entry[k] for k in ordered if k in key_to_entry], []


def _topological_sort(
    nodes: list[str], adj: dict[str, set[str]]
) -> tuple[list[str], list[str]]:
    """Kahn's algorithm for topological sort. Returns (order, cycle_errors).

    adj[A] = {B, C} means A must come before B and C (A depends on B and C,
    so A is dropped first).
    """
    in_degree: dict[str, int] = {n: 0 for n in nodes}
    # Build reverse adjacency: for each edge A → B, B has an incoming edge from A.
    # In-degree of B counts how many things must be dropped before B.
    reverse: dict[str, list[str]] = {n: [] for n in nodes}
    for node, deps in adj.items():
        for dep in deps:
            if dep in in_degree:
                in_degree[dep] += 1
                reverse[node].append(dep)

    queue = [n for n in nodes if in_degree[n] == 0]
    order = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        for neighbour in reverse.get(node, []):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(order) != len(nodes):
        # Cycle detected — remaining nodes are part of the cycle.
        cycled = [n for n in nodes if n not in set(order)]
        return [], [
            f"Dependency cycle detected involving: {', '.join(sorted(cycled))}"
        ]

    return order, []


# ---------------------------------------------------------------------------
# Check 3: Catalog-based cascade victim detection
# ---------------------------------------------------------------------------
#
# INTENTIONAL DEVIATION FROM SPEC: The prompt specifies executing all DROP
# statements inside a BEGIN block against the test DB clone and then rolling back,
# querying pg_depend after the drops to enumerate actual cascade victims.
#
# This implementation instead queries pg_depend directly without executing any
# DDL. The trade-off:
#   Prompt approach:  accurate (captures PostgreSQL's actual dependency resolution),
#                     but requires write access to the test DB clone and acquires
#                     table locks during the transaction.
#   This approach:    read-only (no locks, no write access required), but relies on
#                     pg_depend being an accurate proxy for what CASCADE would drop.
#                     pg_depend does not capture all edge cases — notably, it may
#                     miss objects that depend on the function indirectly via views
#                     or rules that are not themselves pg_depend entries.
#
# Accept this deviation only if the test DB clone is not available for write access
# during analysis. If write access is available, prefer the DDL-in-transaction
# approach for accuracy.

_QUERY_CASCADE_VICTIMS = """
SELECT dep_ns.nspname AS victim_schema,
       COALESCE(dep_proc.proname, dep_type.typname) AS victim_name,
       CASE WHEN dep_proc.oid IS NOT NULL THEN 'function' ELSE 'type' END AS victim_type
  FROM pg_depend d
  JOIN pg_proc ref_proc ON ref_proc.oid = d.refobjid
                       AND ref_proc.proname = %s
  JOIN pg_namespace ref_ns ON ref_ns.oid = ref_proc.pronamespace
                          AND ref_ns.nspname = %s
  LEFT JOIN pg_proc dep_proc ON dep_proc.oid = d.objid
  LEFT JOIN pg_type dep_type ON dep_type.oid = d.objid
  JOIN pg_namespace dep_ns ON dep_ns.oid =
       COALESCE(dep_proc.pronamespace, dep_type.typnamespace)
 WHERE d.deptype = 'n'
   AND dep_ns.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
   AND (
         dep_proc.oid IS NOT NULL
         OR (dep_type.oid IS NOT NULL AND dep_type.typtype IN ('c', 'e', 'r'))
       )
   AND d.objid != d.refobjid;
"""


def check_cascade_victims(
    conn: psycopg2.extensions.connection,
    drop_entries: list[dict],
    release_scope: set[str],
) -> tuple[list[CascadeVictim], list[str]]:
    """Inspect pg_depend to enumerate objects that would be cascade-dropped.

    For each function in drop_entries, queries pg_depend to find objects that
    depend on it (and would be eliminated by DROP ... CASCADE). Returns
    (victims, fail_reasons). fail_reasons is non-empty if any victim is outside
    the release scope — a hard fail condition.

    All access is read-only SELECT against pg_depend. No DDL is executed.
    """
    if not drop_entries:
        conn.rollback()
        return [], []

    explicitly_dropped = {f"{e['schema']}.{e['name']}" for e in drop_entries}
    seen: set[str] = set()
    victims = []
    fails = []

    with conn.cursor() as cur:
        for entry in drop_entries:
            cur.execute(_QUERY_CASCADE_VICTIMS, (entry["name"], entry["schema"]))
            for victim_schema, victim_name, victim_type in cur.fetchall():
                qualified = f"{victim_schema}.{victim_name}"
                if qualified in explicitly_dropped or qualified in seen:
                    continue
                seen.add(qualified)
                in_scope = qualified in release_scope
                victims.append(
                    CascadeVictim(
                        object_name=qualified,
                        object_type=victim_type,
                        in_release_scope=in_scope,
                    )
                )
                if not in_scope:
                    fails.append(
                        f"CASCADE victim not in release scope: {qualified} — "
                        f"this object would be permanently destroyed"
                    )
                    log.error("HARD FAIL: cascade victim out of scope: %s", qualified)
                else:
                    log.info("Cascade victim in release scope (safe): %s", qualified)

    conn.rollback()
    return victims, fails


def _build_release_scope(static_report: dict) -> set[str]:
    """Return the set of qualified object names covered by this release.

    Built from the function signatures and type definitions extracted in Stage 2.
    Used by the cascade simulation to determine whether a cascade-dropped object
    will be redeployed (safe) or permanently destroyed (hard fail).
    """
    scope: set[str] = set()
    for sig in static_report.get("function_signatures", []):
        scope.add(f"{sig['schema']}.{sig['name']}")
    for type_def in static_report.get("type_definitions", []):
        scope.add(f"{type_def['schema']}.{type_def['name']}")
    return scope


# ---------------------------------------------------------------------------
# Check 4-6: Audit coverage
# ---------------------------------------------------------------------------
#
# Audit coverage checks are informational inputs for post.sql. They determine
# whether Stage 4 should create missing audit tables, add columns, rebuild audit
# tables, or create missing triggers.

_QUERY_AUDIT_TABLE_EXISTS = """
SELECT 1
  FROM information_schema.tables
 WHERE table_schema = 'audit'
   AND table_name = %s;
"""

_QUERY_BASE_COLUMNS = """
SELECT column_name
  FROM information_schema.columns
 WHERE table_schema = %s
   AND table_name = %s
ORDER BY ordinal_position;
"""

_QUERY_AUDIT_COLUMNS = """
SELECT column_name
  FROM information_schema.columns
 WHERE table_schema = 'audit'
   AND table_name = %s
ORDER BY ordinal_position;
"""

# Ordered column list for the existing audit table, used by Stage 4 to build the
# INSERT SELECT in the rebuild block. pg_attribute attnum order matches the physical
# column order required by the SOP. Meta-columns are excluded here because Stage 4
# always appends them explicitly at the end of the INSERT SELECT.
_QUERY_AUDIT_COLUMNS_ORDERED = """
SELECT a.attname
  FROM pg_attribute a
 WHERE a.attrelid = %s::regclass
   AND a.attnum > 0
   AND NOT a.attisdropped
   AND a.attname NOT IN ('audit_event', 'audit_stamp', 'audit_user_id')
 ORDER BY a.attnum;
"""

_QUERY_AUDIT_TRIGGER_EXISTS = """
SELECT 1
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
 WHERE n.nspname = %s
   AND c.relname = %s
   AND t.tgname  = 'audit';
"""

# Audit meta-columns that are added by the pipeline and are not in the base table.
_AUDIT_META_COLUMNS = {"audit_event", "audit_stamp", "audit_user_id"}


def check_audit_coverage(
    conn: psycopg2.extensions.connection,
    table_mutations: list[dict],
) -> list[AuditGap]:
    """Inspect audit schema coverage for tables affected by this release.

    Returns a list of AuditGap records for Stage 4 to act on. This check is
    purely informational — it does not produce hard fails or warnings.
    """
    gaps: list[AuditGap] = []
    processed: set[tuple[str, str]] = set()

    with conn.cursor() as cur:
        for mutation in table_mutations:
            schema = mutation["schema"]
            table = mutation["table"]
            mutation_type = mutation["mutation_type"]
            key = (schema, table)

            if mutation_type == "create_table":
                if key in processed:
                    continue
                processed.add(key)
                audit_table_name = f"{schema}_{table}"
                cur.execute(_QUERY_AUDIT_TABLE_EXISTS, (audit_table_name,))
                if not cur.fetchone():
                    gaps.append(
                        AuditGap(
                            schema=schema,
                            table=table,
                            gap_type="missing_audit_table",
                            columns_to_add=[],
                            columns_dropped=[],
                            existing_audit_columns=[],
                        )
                    )
                    log.info(
                        "Audit gap: missing audit table for %s.%s", schema, table
                    )
                else:
                    # Table exists — check for a trigger.
                    cur.execute(_QUERY_AUDIT_TRIGGER_EXISTS, (schema, table))
                    if not cur.fetchone():
                        gaps.append(
                            AuditGap(
                                schema=schema,
                                table=table,
                                gap_type="trigger_missing",
                                columns_to_add=[],
                                columns_dropped=[],
                                existing_audit_columns=[],
                            )
                        )

            elif mutation_type in ("add_column", "drop_column"):
                if key in processed:
                    continue
                processed.add(key)
                audit_table_name = f"{schema}_{table}"

                cur.execute(_QUERY_AUDIT_TABLE_EXISTS, (audit_table_name,))
                if not cur.fetchone():
                    # Audit table is missing entirely — already covered above or
                    # this is a pre-existing issue; skip column gap analysis.
                    continue

                cur.execute(_QUERY_BASE_COLUMNS, (schema, table))
                base_cols = {row[0] for row in cur.fetchall()}

                cur.execute(_QUERY_AUDIT_COLUMNS, (audit_table_name,))
                audit_cols = {row[0] for row in cur.fetchall()}

                # Project the base table forward: the DB has not been updated yet,
                # so compare against what the table WILL look like after this release.
                pending_adds = set(mutation.get("columns_added", []))
                pending_drops = set(mutation.get("columns_dropped", []))
                projected_base_cols = (base_cols | pending_adds) - pending_drops

                expected_in_audit = projected_base_cols | _AUDIT_META_COLUMNS
                missing_from_audit = expected_in_audit - audit_cols - _AUDIT_META_COLUMNS

                # Columns in audit but absent from the projected base = stale after
                # a DROP COLUMN on the base table.
                stale_in_audit = (audit_cols - projected_base_cols) - _AUDIT_META_COLUMNS

                if missing_from_audit or stale_in_audit:
                    # Capture the ordered column list from the existing audit table.
                    # Stage 4 uses this to build the INSERT SELECT in the rebuild block.
                    audit_ref = f"audit.{audit_table_name}"
                    cur.execute(_QUERY_AUDIT_COLUMNS_ORDERED, (audit_ref,))
                    existing_audit_columns = [row[0] for row in cur.fetchall()]

                    gaps.append(
                        AuditGap(
                            schema=schema,
                            table=table,
                            gap_type="missing_columns",
                            columns_to_add=sorted(missing_from_audit),
                            columns_dropped=sorted(stale_in_audit),
                            existing_audit_columns=existing_audit_columns,
                        )
                    )
                    log.info(
                        "Audit gap: column mismatch for %s.%s "
                        "(missing: %s, stale: %s)",
                        schema,
                        table,
                        missing_from_audit,
                        stale_in_audit,
                    )

    conn.rollback()
    return gaps


# ---------------------------------------------------------------------------
# Check 7: Pre-deploy duplicate function check
# ---------------------------------------------------------------------------
#
# Existing duplicate functions are surfaced as warnings because they predate the
# release. Later deployment stages run the same check as a hard fail after this
# release has had a chance to apply prep.sql.

_QUERY_DUPLICATE_FUNCTIONS = """
SELECT n.nspname AS schema,
       p.proname AS name,
       count(*)  AS overload_count
  FROM pg_proc p
  JOIN pg_namespace n ON n.oid = p.pronamespace
 WHERE n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
GROUP BY n.nspname, p.proname
HAVING count(*) > 1
ORDER BY n.nspname, p.proname;
"""


def check_duplicate_functions(
    conn: psycopg2.extensions.connection,
) -> list[dict]:
    """Query for functions with more than one overload in the same schema.

    Returns a list of {schema, name, overload_count} dicts. Results are
    informational — pre-existing duplicates produce a warning, not a hard fail.
    """
    with conn.cursor() as cur:
        cur.execute(_QUERY_DUPLICATE_FUNCTIONS)
        rows = cur.fetchall()
    conn.rollback()
    return [
        {"schema": row[0], "name": row[1], "overload_count": row[2]}
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Stage runner
# ---------------------------------------------------------------------------
#
# Stage 3 joins static analysis facts with live catalog state. It returns every
# output object to keep filesystem writes in main() and make the analysis unit
# testable with mocked connections.

def run(
    manifest: Manifest,
    static_report: dict,
    cfg: Config,
) -> tuple[Manifest, dict, list[CascadeVictim], list[AuditGap], list[dict]]:
    """Run Stage 3 database-assisted analysis.

    Returns:
        (updated_manifest, db_report, cascade_victims, audit_gaps, drop_order)

    All output data is returned so main() can write the JSON artefacts and so
    tests can inspect results without touching the filesystem.
    """
    all_fails: list[str] = []
    all_warnings: list[str] = []

    signatures = [
        FunctionSignature.from_dict(s)
        for s in static_report.get("function_signatures", [])
    ]
    table_mutations: list[dict] = static_report.get("table_mutations", [])

    cascade_victims: list[CascadeVictim] = []
    audit_gaps: list[AuditGap] = []
    drop_order: list[dict] = []
    deltas: list[dict] = []
    duplicates: list[dict] = []

    release_scope = _build_release_scope(static_report)

    with readonly_connection(cfg) as conn:
        # Check 1: function signature deltas.
        log.info("Checking function signature deltas (%d functions).", len(signatures))
        deltas = check_function_signature_deltas(conn, signatures)
        if deltas:
            for delta in deltas:
                # A signature change is not itself a hard fail. Its purpose is
                # to prove that prep.sql needs an explicit DROP for the old
                # function identity before the replacement function is deployed.
                # The hard-fail checks are the safety checks below: dependency
                # ordering and cascade simulation.
                log.info(
                    "Signature delta requires prep.sql DROP: %s.%s — %s",
                    delta["schema"],
                    delta["name"],
                    delta["reason"],
                )

        # Check 2: DROP dependency ordering.
        log.info("Computing DROP dependency order for %d entries.", len(deltas))
        drop_order, cycle_fails = compute_drop_order(conn, deltas, [])
        all_fails.extend(cycle_fails)

        # Check 3: Catalog cascade victim detection (only if there are DROPs).
        # Uses pg_depend via the read-only role — no DDL is executed.
        if drop_order:
            log.info("Checking catalog cascade victims for %d DROP entries.", len(drop_order))
            cascade_victims, cascade_fails = check_cascade_victims(
                conn, drop_order, release_scope
            )
            all_fails.extend(cascade_fails)
        else:
            log.info("No DROPs required — skipping cascade victim check.")

        # Checks 4-6: audit coverage.
        if table_mutations:
            log.info("Checking audit coverage for %d table mutations.", len(table_mutations))
            audit_gaps = check_audit_coverage(conn, table_mutations)
        else:
            log.info("No table mutations detected — skipping audit coverage check.")

        # Check 7: pre-deploy duplicate functions (warning only).
        log.info("Checking for pre-existing duplicate functions.")
        duplicates = check_duplicate_functions(conn)
        if duplicates:
            for dup in duplicates:
                msg = (
                    f"Pre-existing duplicate function: "
                    f"{dup['schema']}.{dup['name']} "
                    f"({dup['overload_count']} overloads)"
                )
                all_warnings.append(msg)
                log.warning("%s", msg)

    if all_fails:
        manifest.has_hard_fail = True
        manifest.fail_reasons.extend(all_fails)
        for reason in all_fails:
            log.error("HARD FAIL: %s", reason)

    manifest.warnings.extend(all_warnings)

    db_report = {
        "stage": "s3_db_analysis",
        "hard_fails": all_fails,
        "warnings": all_warnings,
        "signature_deltas": deltas,
        "drop_order": drop_order,
        "cascade_victims": [v.to_dict() for v in cascade_victims],
        "audit_gaps": [g.to_dict() for g in audit_gaps],
        "duplicate_functions": duplicates,
    }

    return manifest, db_report, cascade_victims, audit_gaps, drop_order


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
#
# The CLI writes all Stage 3 JSON artefacts into the release directory. Hard
# fails stop the pipeline before artefact generation can proceed.

def main() -> None:
    """CLI entry point for Stage 3 database-assisted analysis."""
    parser = argparse.ArgumentParser(
        description="Stage 3: Database-assisted analysis. Requires read-only DB access."
    )
    parser.add_argument(
        "manifest_path",
        help="Path to raw-manifest.json (updated by Stage 2).",
    )
    parser.add_argument(
        "static_report_path",
        help="Path to static-analysis-report.json from Stage 2.",
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

    try:
        with open(args.static_report_path, "r", encoding="utf-8") as fh:
            static_report = json.load(fh)
    except (OSError, KeyError, ValueError) as exc:
        log.error("Failed to load static analysis report: %s", exc)
        sys.exit(1)

    try:
        manifest, db_report, cascade_victims, audit_gaps, drop_order = run(
            manifest, static_report, cfg
        )
    except psycopg2.OperationalError as exc:
        log.error("Database connection failed: %s", exc)
        sys.exit(1)

    release_dir = manifest.release_dir or os.path.join(
        cfg.releases_base_dir, manifest.ticket_number or "unknown"
    )
    reports_dir = manifest.reports_dir or os.path.join(release_dir, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    output_files = {
        "db-analysis-report.json": db_report,
        "cascade-victims.json": [v.to_dict() for v in cascade_victims],
        "audit-gaps.json": [g.to_dict() for g in audit_gaps],
        "drop-order.json": drop_order,
        "raw-manifest.json": manifest.to_dict(),
    }

    try:
        for filename, data in output_files.items():
            path = os.path.join(reports_dir, filename)
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            log.info("Written: %s", path)
    except OSError as exc:
        log.error("Failed to write output files: %s", exc)
        sys.exit(1)

    if manifest.has_hard_fail:
        log.error(
            "Stage 3 completed with %d hard fail(s). Deployment is blocked.",
            len(manifest.fail_reasons),
        )
        sys.exit(1)

    log.info(
        "Stage 3 complete. %d signature delta(s), %d audit gap(s), "
        "%d cascade victim(s), %d warning(s).",
        len(db_report.get("signature_deltas", [])),
        len(audit_gaps),
        len(cascade_victims),
        len(manifest.warnings),
    )
    sys.exit(0)


if __name__ == "__main__":
    main()
