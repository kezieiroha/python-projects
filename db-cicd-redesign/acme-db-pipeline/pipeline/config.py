"""
Configuration loader for the acme DB pipeline.

All runtime configuration is sourced from environment variables. No defaults are
provided for credentials or hostnames. Optional variables have documented defaults.
A ConfigError is raised at import time if any required variable is absent, so
pipeline stages fail fast before performing any work.
"""

import os
import re


# Configuration errors
#
# Stages should fail before doing external work when required environment is
# absent or malformed. A dedicated exception keeps config failures distinct from
# git, filesystem, and database failures.
class ConfigError(Exception):
    """Raised when a required environment variable is absent or invalid."""


# Environment parsing helpers
#
# All helpers trim whitespace so CI variable values behave consistently whether
# entered directly in GitHub settings or loaded from dotenv-style sources.
def _require(name: str) -> str:
    """Return the value of a required environment variable.

    Raises ConfigError if the variable is not set or is empty.
    """
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Required environment variable is not set: {name}")
    return value


def _require_int(name: str) -> int:
    """Return the value of a required environment variable as an integer.

    Raises ConfigError if the variable is missing or not a valid integer.
    """
    raw = _require(name)
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer, got: {raw!r}")


def _optional(name: str, default: str) -> str:
    """Return the value of an optional environment variable, or a default."""
    value = os.environ.get(name, "").strip()
    return value if value else default


def _optional_bool(name: str) -> bool:
    """Return True if the env var is set to 'true', '1', or 'yes' (case-insensitive)."""
    return os.environ.get(name, "").strip().lower() in ("true", "1", "yes")


def _optional_int(name: str, default: int) -> int:
    """Return the value of an optional environment variable as an integer.

    Returns the default if the variable is unset. Raises ConfigError if set but invalid.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"Environment variable {name} must be an integer, got: {raw!r}")


# Runtime configuration object
#
# Config deliberately exposes simple attributes instead of nested configuration
# classes. Each stage script constructs Config once at startup and then passes it
# through to helpers that need environment, repository, database, or integration
# settings.
class Config:
    """Pipeline configuration, loaded from environment variables.

    Instantiate once per pipeline run. All attributes are read-only after construction.
    Raises ConfigError if any required variable is missing.
    """

    def __init__(self) -> None:
        """Load all configuration from environment variables.

        Raises ConfigError immediately if any required variable is absent,
        ensuring that pipeline stages fail before performing any external work.
        """
        # Database connection settings
        #
        # The read-only role is used by s3 for catalog analysis and by s6/s7 for
        # post-deploy verification queries (duplicate check, audit alignment).
        # All Python-level DB access is read-only SELECT. Deployment mutations
        # are performed by ci_backend_db_release.py on EC2 via psql as DB_ROOT.
        self.db_host: str = _require("DB_HOST")
        self.db_port: int = _optional_int("DB_PORT", 5432)
        self.db_name: str = _require("DB_NAME")
        self.db_user_readonly: str = _require("DB_USER_READONLY")
        self.db_pass_readonly: str = _require("DB_PASS_READONLY")

        # Repository paths
        #
        # The dataschema repository is mandatory because all release manifests
        # are based on its diff. The serverless repository is optional for this
        # phase and is skipped when no path is configured.
        self.git_repo_dataschema: str = _require("GIT_REPO_DATASCHEMA")
        self.git_repo_serverless: str = _optional("GIT_REPO_SERVERLESS", "")

        # Release artefact root
        #
        # Stages write JSON reports and generated SQL under
        # $RELEASES_BASE_DIR/<jira_ticket>/ so CI jobs can pass a single
        # release directory forward as an artifact.
        self.releases_base_dir: str = _require("RELEASES_BASE_DIR")

        # GitHub integration
        #
        # Reporter uses these values to post gate summaries back to the pull
        # request that produced the squash commit.
        self.github_token: str = _require("GITHUB_TOKEN")
        self.github_repository: str = _require("GITHUB_REPOSITORY")
        self.github_api_url: str = _optional("GITHUB_API_URL", "https://api.github.com").rstrip("/")

        # Commit metadata extraction
        #
        # Regexes are configurable because Jira and PR reference formats vary
        # across installations and team conventions.
        self.jira_ticket_pattern: str = _require("JIRA_TICKET_PATTERN")
        self.pr_number_pattern: str = _require("PR_NUMBER_PATTERN")
        self._validate_regex("JIRA_TICKET_PATTERN", self.jira_ticket_pattern)
        self._validate_regex("PR_NUMBER_PATTERN", self.pr_number_pattern)

        # Live service health checks
        #
        # Stage 7 uses these endpoints after live deployment and service restart
        # to prove the app-facing GraphQL surfaces recovered cleanly.
        self.graphql_api_url: str = _require("GRAPHQL_API_URL")
        self.graphql_crm_url: str = _require("GRAPHQL_CRM_URL")
        self.health_check_retries: int = _optional_int("HEALTH_CHECK_RETRIES", 3)
        self.health_check_backoff_seconds: int = _optional_int(
            "HEALTH_CHECK_BACKOFF_SECONDS", 10
        )

        # Clone recreation control
        #
        # Skip flags are optional (default false). When true, the pipeline reuses
        # the existing clone rather than recreating it. RECREATE_TEST_DB_SCRIPT is
        # the absolute path on EC2 to recreate_test_db_clone.sh — never hardcoded.
        self.skip_test_clone_recreate: bool = _optional_bool("SKIP_TEST_CLONE_RECREATE")
        self.skip_staging_clone_recreate: bool = _optional_bool("SKIP_STAGING_CLONE_RECREATE")
        self.recreate_test_db_script: str = _optional("RECREATE_TEST_DB_SCRIPT", "")
        self.staging_live_recreate_cmd: str = _optional("STAGING_LIVE_RECREATE_CMD", "")

        # EC2 SSH connection — used by s6a, s6b, s6, and s7 to invoke
        # ci_backend_db_release.py and clone recreation scripts on EC2.
        #
        # SSH_PRIVATE_KEY should point to a PEM file path supplied by the CI runner.
        # chmod 600 is applied before use.
        # All vars are optional at config-load time so earlier analysis stages
        # and unit tests can run without a deployment host configured.
        self.ec2_host: str = _optional("EC2_HOST", "")
        self.ec2_user: str = _optional("EC2_USER", "ubuntu")
        self.ssh_private_key: str = _optional("SSH_PRIVATE_KEY", "")
        self.ec2_releases_dir: str = _optional("EC2_RELEASES_DIR", "")

        # Live service restart commands
        #
        # Empty commands are treated as "not configured" and skipped by Stage 7;
        # non-empty commands must exit zero.
        self.crm_restart_cmd: str = _optional("CRM_RESTART_CMD", "")
        self.api_restart_cmd: str = _optional("API_RESTART_CMD", "")

        # Optional Jira transition integration
        #
        # Deployment success must not depend on Jira availability, so Stage 7
        # logs transition failures but does not hard fail once DB deployment and
        # health verification have completed.
        self.jira_token: str = _optional("JIRA_TOKEN", "")
        self.jira_base_url: str = _optional("JIRA_BASE_URL", "")
        self.jira_transition_id: str = _optional("JIRA_TRANSITION_ID", "")
        # Separate transition ID for the deployed-to-production status in s10.
        # Falls back to jira_transition_id if not set.
        self.jira_transition_id_prod: str = _optional(
            "JIRA_TRANSITION_ID_PROD", ""
        )

        # Production EC2 / SSH
        #
        # All production vars are optional. Stages apply an infrastructure guard
        # (exit 0) when ec2_host_prod is not set, so the staging pipeline runs
        # cleanly in environments where production is not yet configured.
        self.ec2_host_prod: str = _optional("EC2_HOST_PROD", "")
        self.ec2_user_prod: str = _optional("EC2_USER_PROD", "ubuntu")
        self.ec2_releases_dir_prod: str = _optional("EC2_RELEASES_DIR_PROD", "")
        self.releases_base_dir_prod: str = _optional("RELEASES_BASE_DIR_PROD", "")

        # Production clone recreation
        self.skip_prod_test_clone_recreate: bool = _optional_bool(
            "SKIP_PROD_TEST_CLONE_RECREATE"
        )

        # Production service restart nodes
        #
        # Comma-separated list of hostnames/IPs. Each node receives an SSH
        # command running `crm_restart; api_restart` after prod-live deployment.
        self.prod_nodes: list[str] = [
            n.strip()
            for n in _optional("PROD_NODES", "").split(",")
            if n.strip()
        ]

        # Production GraphQL health check endpoints
        self.graphql_api_url_prod: str = _optional("GRAPHQL_API_URL_PROD", "")
        self.graphql_crm_url_prod: str = _optional("GRAPHQL_CRM_URL_PROD", "")

    @staticmethod
    def _validate_regex(var_name: str, pattern: str) -> None:
        """Raise ConfigError if pattern is not a valid regular expression."""
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"Environment variable {var_name} is not a valid regex: {exc}"
            )

    def db_dsn_readonly(self) -> str:
        """Return a psycopg2-compatible DSN string for the read-only role."""
        return (
            f"host={self.db_host} "
            f"port={self.db_port} "
            f"dbname={self.db_name} "
            f"user={self.db_user_readonly} "
            f"password={self.db_pass_readonly}"
        )

    def serverless_configured(self) -> bool:
        """Return True if the serverless repo path is set."""
        return bool(self.git_repo_serverless)
