"""
Dataclasses for all structured data passed between pipeline stages.

All models are JSON-serialisable via to_dict(). Each model also provides a
from_dict() classmethod so stages can reconstruct objects from the JSON artefacts
written by earlier stages.

dataclasses.asdict() is used by to_dict() implementations. It recurses into nested
dataclasses automatically, so Manifest.to_dict() correctly serialises its SqlFile list
without extra work.
"""

import re
from dataclasses import dataclass, asdict
from typing import Optional


# File-level release inputs
#
# SqlFile is the smallest unit of release scope. Later stages use its
# classification to determine deploy ordering, duplicate handling, and whether
# a deleted path belongs in the delete artefact rather than deploy.lst.
@dataclass
class SqlFile:
    """A single SQL file detected in the commit diff.

    classification is one of: schema | function | type | config | serverless
    duplicate_annotation is set when is_duplicate is True and names the other path.
    """

    relative_path: str
    classification: str
    is_deleted: bool
    is_duplicate: bool
    duplicate_annotation: Optional[str]

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SqlFile":
        """Reconstruct a SqlFile from a dictionary (e.g. parsed from JSON)."""
        return cls(
            relative_path=data["relative_path"],
            classification=data["classification"],
            is_deleted=data["is_deleted"],
            is_duplicate=data["is_duplicate"],
            duplicate_annotation=data.get("duplicate_annotation"),
        )


# Static-analysis outputs
#
# These models describe facts extracted from SQL text before any database
# connection is opened. Stage 3 uses them to compare desired SQL state against
# live catalog state and to decide which generated artefacts are required.
@dataclass
class FunctionSignature:
    """Parsed signature of a PostgreSQL function found in the diff.

    param_types contains the positional parameter type list extracted from the
    CREATE [OR REPLACE] FUNCTION statement (e.g. ["uuid", "text", "integer"]).
    """

    schema: str
    name: str
    param_types: list[str]
    return_type: str
    source_file: str

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "FunctionSignature":
        """Reconstruct a FunctionSignature from a dictionary."""
        return cls(
            schema=data["schema"],
            name=data["name"],
            param_types=data.get("param_types", []),
            return_type=data["return_type"],
            source_file=data["source_file"],
        )


@dataclass
class TableMutation:
    """A DDL change to a table detected in the diff.

    mutation_type is one of: add_column | drop_column | rename_column | create_table
    columns_added and columns_dropped list the affected column names.
    """

    schema: str
    table: str
    mutation_type: str
    columns_added: list[str]
    columns_dropped: list[str]
    source_file: str

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "TableMutation":
        """Reconstruct a TableMutation from a dictionary."""
        return cls(
            schema=data["schema"],
            table=data["table"],
            mutation_type=data["mutation_type"],
            columns_added=data.get("columns_added", []),
            columns_dropped=data.get("columns_dropped", []),
            source_file=data["source_file"],
        )


# Database-analysis outputs
#
# These models capture DB-assisted findings. Audit gaps feed post.sql
# generation; cascade victims feed hard-fail decisions and Gate 1 reporting.
@dataclass
class AuditGap:
    """An identified gap between a base table and its audit counterpart.

    gap_type is one of: missing_audit_table | missing_columns | trigger_missing
    columns_to_add lists column names being added to the base table by this release.
    columns_dropped lists column names being dropped from the base table (stale in audit).
    existing_audit_columns is the ordered column list from the existing audit table,
    captured by Stage 3 before the release runs. Used by Stage 4 to build the INSERT
    SELECT in the rebuild block. Empty for missing_audit_table and trigger_missing.
    """

    schema: str
    table: str
    gap_type: str
    columns_to_add: list[str]
    columns_dropped: list[str]
    existing_audit_columns: list[str]

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "AuditGap":
        """Reconstruct an AuditGap from a dictionary."""
        return cls(
            schema=data["schema"],
            table=data["table"],
            gap_type=data["gap_type"],
            columns_to_add=data.get("columns_to_add", []),
            columns_dropped=data.get("columns_dropped", []),
            existing_audit_columns=data.get("existing_audit_columns", []),
        )


@dataclass
class CascadeVictim:
    """An object that would be dropped by cascade during prep.sql execution.

    in_release_scope is True when the object appears in the current release manifest,
    meaning it will be redeployed and the cascade drop is safe. False means the drop
    would destroy something not being redeployed — a HARD FAIL condition.
    """

    object_name: str
    object_type: str
    in_release_scope: bool

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "CascadeVictim":
        """Reconstruct a CascadeVictim from a dictionary."""
        return cls(
            object_name=data["object_name"],
            object_type=data["object_type"],
            in_release_scope=data["in_release_scope"],
        )


# Cross-stage manifest
#
# Manifest is the durable handoff between stages. It carries commit provenance,
# release file scope, and accumulated warnings/failures so every later stage can
# render or enforce the same deployment decision context.
@dataclass
class Manifest:
    """The central record for a single pipeline run.

    Written by s1_discovery and read by every subsequent stage. has_hard_fail is
    set to True and fail_reasons populated whenever any stage encounters a condition
    that must block deployment. warnings accumulate non-blocking issues for the gate
    report.
    """

    commit_hash: str
    jira_ticket: str
    ticket_number: str       # numeric part only — e.g. "DEV-1022" -> "1022"
    pr_number: str
    author: str
    timestamp: str
    release_dir: str         # $RELEASES_BASE_DIR/<ticket_number>
    reports_dir: str         # $RELEASES_BASE_DIR/<ticket_number>/reports
    sql_files: list[SqlFile]
    deleted_files: list[str]
    sql_changes: bool
    has_hard_fail: bool
    fail_reasons: list[str]
    warnings: list[str]

    @property
    def jira_ticket_number(self) -> str:
        """Return just the numeric part of jira_ticket, e.g. 'DEV-1022' -> '1022'."""
        m = re.search(r"\d+$", self.jira_ticket)
        return m.group(0) if m else self.jira_ticket

    def to_dict(self) -> dict:
        """Return a JSON-serialisable dictionary representation.

        asdict() recurses into the nested SqlFile dataclasses automatically.
        """
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        """Reconstruct a Manifest from a dictionary (e.g. parsed from raw-manifest.json).

        ticket_number, release_dir, and reports_dir are derived from jira_ticket when
        absent so that manifests written before these fields were added still load cleanly.
        """
        sql_files = [SqlFile.from_dict(f) for f in data.get("sql_files", [])]
        jira_ticket = data["jira_ticket"]
        m = re.search(r"\d+$", jira_ticket)
        derived_number = m.group(0) if m else jira_ticket
        ticket_number = data.get("ticket_number", derived_number)
        release_dir = data.get("release_dir", "")
        reports_dir = data.get("reports_dir", "")
        return cls(
            commit_hash=data["commit_hash"],
            jira_ticket=jira_ticket,
            ticket_number=ticket_number,
            pr_number=data["pr_number"],
            author=data["author"],
            timestamp=data["timestamp"],
            release_dir=release_dir,
            reports_dir=reports_dir,
            sql_files=sql_files,
            deleted_files=data.get("deleted_files", []),
            sql_changes=data.get("sql_changes", True),
            has_hard_fail=data.get("has_hard_fail", False),
            fail_reasons=data.get("fail_reasons", []),
            warnings=data.get("warnings", []),
        )
