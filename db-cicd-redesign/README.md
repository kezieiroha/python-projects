# acme DB Pipeline

Sanitized portfolio project demonstrating a gated PostgreSQL release automation
platform. The original manual release process took roughly 5 hours to 1 day per
release; this project models the controls needed to automate that workflow
without removing human approval from production-impacting steps.

This repository is not intended to run unchanged in another organisation's
environment. Its value is in the pipeline architecture, validation stages,
release artefact generation, deployment safety checks, and test coverage.

## What It Demonstrates

- SQL change discovery from a reviewed squash commit.
- Static SQL checks for required `SET ROLE`, misplaced DDL/DROP statements,
  privilege escalation keywords, CASCADE usage, function signatures, and table
  mutations.
- Database-assisted analysis for signature deltas, audit table gaps, DROP order,
  duplicate functions, and CASCADE blast radius.
- Generated release artefacts: `prep.sql`, `deploy.lst`, `post.sql`, `delete`,
  and `NOTES.txt`.
- Git promotion by cherry-pick from `develop` to `staging` to `production`.
- Test-clone deployment before live deployment.
- Manual approval gates with Markdown reports.
- Post-deploy validation through count checks, duplicate-function checks, audit
  alignment checks, service restarts, GraphQL health checks, and optional Jira
  transition.
- GitHub Actions validation and security scanning.

For the full design narrative, see
[docs/pipeline_architecture.md](docs/pipeline_architecture.md).

## Pipeline Stages

| Stage | Purpose |
|---|---|
| `s1_discovery.py` | Resolve commit metadata, Jira/PR references, changed SQL files, deleted files, and release manifest. |
| `s2_static_analysis.py` | Run SQL safety checks and extract signatures, type definitions, table mutations, and deploy order. |
| `s3_db_analysis.py` | Query PostgreSQL catalog metadata for dependency, audit, duplicate, and CASCADE risk analysis. |
| `s4_artefact_gen.py` | Generate release artefacts from discovery, static analysis, and DB analysis outputs. |
| `s5_git_promotion.py` | Cherry-pick the reviewed release commit onto the staging branch. |
| `s6a_recreate_staging_test_clone.py` | Recreate the staging test database clone when configured. |
| `s6_test_db_deploy.py` | Deploy to the staging test database and validate the result. |
| `s6b_recreate_staging_live_clone.py` | Recreate the staging live clone when configured. |
| `s7_live_db_deploy.py` | Deploy to staging live, restart services, and run health checks. |
| `s8_git_promotion_prod.py` | Promote the staging cherry-pick hash onto production. |
| `s9a_recreate_prod_test_clone.py` | Recreate the production test database clone when configured. |
| `s9b_prod_test_db_deploy.py` | Deploy to the production test database and validate the result. |
| `s10_prod_live_db_deploy.py` | Deploy to production live, restart services, and run health checks. |

## Repository Layout

```text
.
├── .github/workflows/ci.yml
├── docs/
│   └── pipeline_architecture.md
├── requirements.in
├── requirements.txt
└── acme-db-pipeline/
    ├── pipeline/
    │   ├── config.py
    │   ├── db.py
    │   ├── logger.py
    │   ├── models.py
    │   └── reporter.py
    ├── scripts/
    │   ├── backend_db_release.py
    │   └── ci_backend_db_release.py
    ├── stages/
    │   └── s1...s10 pipeline stage modules
    └── tests/
        ├── fixtures/
        └── pytest coverage for analysis, artefact generation, deployment,
            reporting, and git promotion behavior
```

## GitHub Actions

The public workflow in `.github/workflows/ci.yml` runs on pull requests, pushes
to `main`, `develop`, and `staging`, and manual `workflow_dispatch` runs.

It includes:

- `pytest`
- Ruff
- mypy
- ShellCheck
- yamllint
- Trivy filesystem scan
- OSV dependency scan

The workflow is intentionally limited to validation and security scanning. Live
database deployment requires organisation-specific runners, network access,
secrets, approval rules, database hosts, and service restart commands.

## Design Decisions

| Decision | Reason |
|---|---|
| Squash-merge release unit | One PR becomes one auditable release commit. |
| Cherry-pick promotion | Preserves commit lineage and stops safely on conflicts instead of copying files over branch drift. |
| Generated artefacts | `prep.sql`, `deploy.lst`, and `post.sql` are produced from analysis output so reviewers inspect a deterministic release package. |
| Human gates | Database deployments remain gated even when analysis and artefact generation are automated. |
| Non-shell subprocess usage | Python stages call subprocesses with explicit argument lists to avoid shell injection. |
| Hashed dependencies | `requirements.txt` is generated with hashes to detect package substitution. |
| Public sanitization | Company-specific names, internal URLs, and sandbox infrastructure scripts were removed or generalized. |

## Running Tests

Unit tests do not require external services. DB integration tests are skipped
unless the required `DB_*` environment variables are present.

```bash
cd /path/to/db-cicd-redesign
python -m pip install --require-hashes -r requirements.txt
pytest acme-db-pipeline/tests -q
```

Current local result:

```text
362 passed, 7 skipped
```

## Manual Stage Execution

Each stage has a CLI entry point and can be run independently when the required
artefacts and environment variables exist. Example:

```bash
python acme-db-pipeline/stages/s2_static_analysis.py \
  releases/DEV-42/reports/raw-manifest.json

python acme-db-pipeline/stages/s4_artefact_gen.py \
  releases/DEV-42/reports/raw-manifest.json \
  releases/DEV-42/reports/static-analysis-report.json \
  releases/DEV-42/reports/db-analysis-report.json
```

Deployment stages require PostgreSQL connectivity, release artefacts, repository
paths, SSH configuration, and environment-specific restart or clone commands.
They are included to demonstrate the deployment control flow, not as a turnkey
deployment package.

## Runtime Configuration

Configuration is loaded from environment variables by
`acme-db-pipeline/pipeline/config.py`. Important groups include:

- PostgreSQL read-only connection settings: `DB_HOST`, `DB_PORT`, `DB_NAME`,
  `DB_USER_READONLY`, `DB_PASS_READONLY`.
- Repository and release paths: `GIT_REPO_DATASCHEMA`, `GIT_REPO_SERVERLESS`,
  `RELEASES_BASE_DIR`.
- Review metadata patterns: `JIRA_TICKET_PATTERN`, `PR_NUMBER_PATTERN`.
- GitHub comment integration: `GITHUB_TOKEN`, `GITHUB_REPOSITORY`.
- Optional Jira transition settings: `JIRA_TOKEN`, `JIRA_BASE_URL`,
  `JIRA_TRANSITION_ID`, `JIRA_TRANSITION_ID_PROD`.
- Deployment host settings: `EC2_HOST`, `EC2_USER`, `SSH_PRIVATE_KEY`,
  `EC2_RELEASES_DIR`, and production equivalents.
- Health checks and restarts: `GRAPHQL_API_URL`, `GRAPHQL_CRM_URL`,
  `CRM_RESTART_CMD`, `API_RESTART_CMD`, and production equivalents.

Secrets should be supplied through GitHub Actions secrets or a protected
self-hosted runner environment. They should not be committed.
