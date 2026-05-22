# acme DB CI/CD Pipeline — Solution Architecture

15 automated stages across 5 manual approval gates, covering trigger, static
analysis, database analysis, artefact generation, git promotion (develop → staging
and staging → production), clone recreation, test and live DB deployment across
staging and production environments.

---

## Deployment environments

Each environment has two database tiers. Deployments always run against the
test tier first, then the live tier, with a human gate between them.

| Tier | Description | Recreation |
|---|---|---|
| staging-test | Clone of staging-live with log data > 30 days purged | `recreate_test_db_clone.sh` on staging VM — stage 6a |
| staging-live | Redacted clone of production RDS | Separate CI/CD process — stage 6b |
| prod-test | Clone of prod-live with log data > 30 days purged | `recreate_test_db_clone.sh` on prod VM — stage 9a |
| prod-live | The actual production RDS instance | No recreation — it IS production |

Clone recreation stages are independently skippable via CI variables
(`SKIP_TEST_CLONE_RECREATE`, `SKIP_STAGING_CLONE_RECREATE`, `SKIP_PROD_TEST_CLONE_RECREATE`)
when the existing clone is known to be current.

Production stages (8 onwards) include an infrastructure guard: if `EC2_HOST_PROD`
is not configured, the stage exits 0 with a clear log message. This allows the
pipeline to be deployed before production EC2 is provisioned.

---

## Git promotion — architectural decision

| | |
|---|---|
| **Current** | `git checkout develop -- file1 file2` — content-only copy onto staging branch. Loses original commit author, timestamp, and hash. No audit traceability. |
| **Proposed** | `git cherry-pick <squash-commit-hash>` — replays the original commit. Preserves author, timestamp, commit message, and original hash in cherry-pick metadata. Full lineage in git log. |
| **Constraint** | Enforce squash merge per PR at repository level. 1 PR = 1 atomic squash commit = 1 cherry-pick. Cherry-pick atomicity is a feature, not a limitation. |
| **Chain** | `develop --> staging --> production`. The staging → production cherry-pick uses the staging cherry-pick hash from `git-promotion.json`, not the original develop hash. Never promote directly from develop to production. |
| **Rollback** | Never selectively uncommit. If 2 of 100 SQLs in a cherry-picked commit need reverting, the answer is a forward-fix PR — not selective uncommit. Pipeline documents the rollback SQL at each gate. |

---

## Pipeline

<details>
<summary><strong>PRE</strong> &nbsp; Developer workflow &nbsp;·&nbsp; 4 steps</summary>

Occurs before the pipeline fires. Developer writes SQL, peer reviews, and the PR
enters the DevOps merge queue. Pipeline owns everything from the merge click
onward.

- Developer writes SQL in a feature branch following SET ROLE and file naming conventions
- Peer developer reviews SQL logic, function signatures, and schema implications; approves PR in GitHub
- DevOps reviews PR metadata and marks ready for merge — does not yet click merge
- DevOps clicks merge; GitHub creates squash commit on develop; workflow fires immediately

**Trigger:** `GitHub pull_request / push event` `squash commit on develop`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 1 — Trigger &amp; discovery &nbsp;·&nbsp; 6 checks</summary>

Pipeline fires. Extracts commit context and builds a complete manifest of all
SQL changes in this merge. Hard fails here prevent wasted analysis on malformed
commits.

- Pipeline records commit hash, author, and timestamp from the merge to develop
- **HARD FAIL** — Commit message must contain Jira ticket ref (`DEV-XXXX`) and PR number (`PR-YYY`)
- `git diff HEAD~1..HEAD` enumerates all changed, added, and deleted files in the merge
- Classify each file: `schema` / `function` / `type` / `config` / `serverless` — by path pattern and naming convention
- Identify deleted SQL files — seed the delete list for DevOps repo cleanup action
- If no SQL files detected in diff: write passthrough manifest (`sql_changes: false`), log reason, exit 0 — all downstream stages skip automatically via the CI dependency chain; correct behaviour for Python-only or non-SQL merges

**Outputs:** `raw-manifest.json` `delete.lst (draft)` `pipeline.env (JIRA_TICKET, JIRA_NUMBER, RELEASE_DIR)`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 2 — SQL static analysis &nbsp;·&nbsp; 10 checks</summary>

Parse all SQL files without a database connection. Detect security violations,
structural problems, and extract metadata needed downstream. No DB credentials
required.

- **HARD FAIL** — `SET ROLE "acme_admin"` must be present at the top of every SQL file
- **HARD FAIL** — Privilege escalation keywords (`GRANT`, `REVOKE`, `CREATE USER`, `ALTER ROLE`, `SUPERUSER`) in any SQL file
- **HARD FAIL** — DDL statements (`CREATE TABLE`, `ALTER TABLE`) found inside function or type files — DDL must only appear in schema-classified files
- **HARD FAIL** — `DROP` statements found inside function or schema files — DROPs belong exclusively in `prep.sql`
- Extract function signatures: parse `CREATE [OR REPLACE] FUNCTION name(param types) RETURNS type` from each SQL file
- Extract type definitions: parse `CREATE TYPE name AS ENUM` or composite structure
- Detect table mutations: `ALTER TABLE ADD/DROP/RENAME COLUMN` and `CREATE TABLE` — flag for audit table analysis downstream
- Enforce deployment ordering: schema files must precede function and type files — classify and sort manifest accordingly
- **WARN** — `CASCADE` keyword detected inside SQL files — flagged for human review; not a hard fail at this stage
- Intra-PR duplicate detection: same SQL file appearing more than once in the diff — annotate in `deploy.lst` with comment

**Outputs:** `function-signatures.json` `table-mutations.json` `type-defs.json` `static-analysis-report.json`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 3 — Database-assisted analysis &nbsp;·&nbsp; 7 checks</summary>

Connect to staging DB using a read-only analysis role. Verify what the release
will change, simulate CASCADE risk, and identify audit table gaps. No writes occur
in this stage.

- For each extracted function signature: query `pg_proc` to detect parameter or return type delta — flag if changed (requires DROP entry in `prep.sql`)
- For each new `CREATE TABLE`: query `information_schema` to verify `audit.<schema>_<table>` does not yet exist — flag if audit table must be created in `post.sql`
- For each altered table (`ADD/DROP COLUMN`): compare `pg_attribute` between base table and audit table — identify column gaps to be closed in `post.sql`
- **HARD FAIL** — CASCADE simulation: execute `prep.sql` in a `BEGIN` block, query `pg_depend` to map all CASCADE victims, `ROLLBACK` — hard fail if any victim falls outside release scope
- **WARN** — Pre-deploy duplicate function check: run `check_for_duplicate_functions.sql` — warn if duplicates already exist before this release touches anything
- For new tables: query `pg_trigger` to verify no audit trigger already exists — idempotency guard before `post.sql` generation
- **HARD FAIL** — DROP dependency ordering: if functions reference custom types in the same release, verify types are dropped before dependent functions — fail if ordering would produce a missing dependency error

**Outputs:** `db-analysis-report.json` `cascade-victims.json` `audit-gaps.json` `drop-order.json`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 4 — Artefact generation &nbsp;·&nbsp; 8 operations</summary>

Generate all release artefacts from the metadata accumulated in prior stages.
Stage to `~/releases/<jira-ticket>/` on the staging VM. Count verification is
a hard fail.

- Generate `prep.sql`: `SET ROLE` header + `DROP FUNCTION/TYPE IF EXISTS ... CASCADE` statements ordered by `pg_depend` dependency graph (from `drop-order.json`)
- Generate `deploy.lst`: schema files first, then functions and types, each group labelled with Jira/PR ref; intra-PR duplicates commented with annotation and reason
- Generate `post.sql` — new table: `CREATE TABLE audit.<schema>_<table> (LIKE schema.table)` + `ADD COLUMN` audit metadata + `CREATE TRIGGER` template populated with real names
- Generate `post.sql` — existing table column add: `ALTER TABLE audit.<table> ADD COLUMN <new col> NULL`; INSERT from old audit data re-propagated correctly
- Generate `post.sql` — existing table column drop: rename audit to `_01`, `CREATE` new audit (`LIKE` base), `INSERT SELECT` with `NULL` for dropped col, `DROP _01` — wrapped in `BEGIN/COMMIT`
- Generate delete file: relative repo paths of SQL files removed by this PR; annotated with Jira/PR ref for DevOps manual repo deletion step
- Generate `NOTES.txt`: Jira link, PR list, serverless files touched, UI env var changes, AWS account ID modifications, S3 bucket renames
- **HARD FAIL** — Count verification: git diff SQL file count must equal non-commented entries in `deploy.lst`; SCP all artefacts to `~/releases/<ticket>/` on dev server

**Outputs:** `prep.sql` `deploy.lst` `post.sql` `delete` `NOTES.txt`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 5 — Git promotion &nbsp;·&nbsp; 4 operations</summary>

Promote SQL changes from develop to staging branch using cherry-pick, preserving
full commit provenance. Pipeline also handles repo file deletions — currently a
fully manual step.

- `git checkout staging && git pull` — ensure staging branch is at HEAD before promotion begins
- `git cherry-pick <squash-commit-hash>` — replays the original commit onto staging; author, timestamp, message, and parent hash preserved in git log
- `git push` — staging branch updated; GitHub records promotion event with full commit lineage
- Delete files listed in the delete artefact from the dataschema repo and commit — pipeline handles this cleanup (currently a manual DevOps step)

**Outputs:** `staging branch updated` `cherry-pick hash logged` `repo deletions committed`

</details>

---

> ### Gate 1 — Artefact review
>
> Pipeline posts a structured report to the PR: files detected, DROPs generated
> with reason, audit stubs created, CASCADE victims (if any), and count
> verification result. DevOps reviews generated files on the server before
> approving.
>
> - Bot report posted to PR: manifest summary, `prep.sql` entries with reasoning, `post.sql` stubs created, count match, any warnings from static or DB analysis
> - DevOps SSHs to staging VM, inspects `~/releases/<ticket>/` — edits artefacts if correction needed; early iterations will require this regularly as the analyser matures
> - DevOps approves the protected GitHub environment, or declines with a comment explaining what needs rework; developer is notified automatically
>
> **Approve** → staging test clone recreation &nbsp;&nbsp; | &nbsp;&nbsp; **Decline** → pipeline fails, developer notified

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 6a — Recreate staging test DB clone &nbsp;·&nbsp; 2 checks</summary>

Drop and recreate the staging test database clone from the live staging database
before deployment runs against it. Controlled by `SKIP_TEST_CLONE_RECREATE`.

- If `SKIP_TEST_CLONE_RECREATE=true`: log and exit 0 — existing clone is reused
- SSH to EC2, run `$RECREATE_TEST_DB_SCRIPT` with no arguments; pipeline waits for completion
- **HARD FAIL** — Script exits non-zero: pipeline halts, test deploy stage never runs
- Write `clone-recreate-staging-test.json` with status and timestamp

**Outputs:** `clone-recreate-staging-test.json` `clone-recreate-staging-test.log`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 6 — Test DB deployment &nbsp;·&nbsp; 6 checks</summary>

Execute the full release against the freshly recreated staging test DB clone.
Verify counts, duplicates, and audit structure before anything touches the
staging live DB.

- Execute `ci_backend_db_release.py --target test` on EC2 via SSH — runs `prep.sql`, all `deploy.lst` entries, and `post.sql` in sequence
- **HARD FAIL** — Script exits non-zero: deployment failed
- **HARD FAIL** — Count verification: SQL files processed in release log must equal non-commented entries in `deploy.lst`
- **HARD FAIL** — Duplicate function check: `pg_proc` query on test DB must return zero rows
- **HARD FAIL** — Audit table alignment: `pg_attribute` comparison for all mutated tables must pass
- Capture full release log file as downloadable CI artefact; retained per audit retention policy

**Outputs:** `test-deploy-pass1.log` `test-count-verify.json` `test-audit-verify.json` `test-deploy-summary.json`

</details>

---

> ### Gate 2 — Staging test DB result
>
> Pipeline posts the staging test DB deployment log and verification results to the PR.
> DevOps reviews the log before approving promotion to the staging live DB.
>
> - Bot report posted: log error summary, count match/mismatch result, duplicate function check result, audit table verification status, any warnings
> - DevOps reviews log — over iterations, a documented list of ignorable errors accumulates; the decision becomes increasingly fast
> - DevOps clicks **Approve** or **Decline**
>
> **Approve** → staging live clone recreation &nbsp;&nbsp; | &nbsp;&nbsp; **Decline** → pipeline fails, investigate

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 6b — Recreate staging live DB clone &nbsp;·&nbsp; 2 checks</summary>

Drop and recreate the staging live database clone before the live dev deployment
runs. The staging-live DB is a redacted clone of production RDS managed outside
this pipeline. Controlled by `SKIP_STAGING_CLONE_RECREATE`.

- If `SKIP_STAGING_CLONE_RECREATE=true`: log and exit 0 — existing clone is reused
- SSH to EC2, run `$STAGING_LIVE_RECREATE_CMD` with no arguments; pipeline waits for completion
- **HARD FAIL** — Command exits non-zero: pipeline halts, live deploy stage never runs
- Write `clone-recreate-staging-live.json` with status and timestamp

**Outputs:** `clone-recreate-staging-live.json` `clone-recreate-staging-live.log`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 7 — Staging live DB deployment &nbsp;·&nbsp; 8 checks</summary>

Deploy the release to the staging live database. Restart GraphQL services on all
staging nodes and verify both endpoints respond. Final duplicate and audit checks
confirm a clean live state.

- SSH to staging VM, run `ci_backend_db_release.py <release_dir> --target live --skip-git` — full release deployed to staging live DB
- **HARD FAIL** — Script exits non-zero: deployment failed
- `crm_restart`; `api_restart` — restart GraphQL services on all staging nodes
- **HARD FAIL** — HTTP health check: API GraphiQL endpoint must return 200 — retry 3× with 10 s back-off before failing pipeline
- **HARD FAIL** — HTTP health check: CRM GraphiQL endpoint must return 200 — retry 3× with 10 s back-off before failing pipeline
- **HARD FAIL** — Run `check_for_duplicate_functions.sql` on staging live DB — must return zero rows
- Final audit table verification: `pg_attribute` comparison for all tables mutated in this release
- Capture live release log and health check results as pipeline artefacts; transition Jira ticket to deployed-to-staging via API

**Outputs:** `live-deploy.log` `health-check.json` `audit-verify-live.json` `Jira transitioned`

</details>

---

> ### Gate 3 — Staging live confirmation
>
> Final sign-off for the staging environment. All artefacts are archived. Release is
> complete in staging. Production promotion begins via the staging branch trigger.
>
> - Bot report: health check status, live deploy log link, audit verification result, Jira transition confirmation
> - DevOps confirms staging live is stable — Approve closes the loop for this release in staging
> - All artefacts (`prep.sql`, `deploy.lst`, `post.sql`, all logs, all analysis JSON) archived as permanent CI artefacts with no expiry
>
> **Approve** → staging release complete &nbsp;&nbsp; | &nbsp;&nbsp; **Decline** → investigate staging live issue

---

## Production promotion pipeline

Runs on the `staging` branch. Every push to staging triggers it — including the
cherry-pick from stage 5. Commits without a JIRA ticket in the message
(infrastructure pushes, workflow edits, dependency updates) are detected
automatically: the pipeline writes `SKIP_PROD=true` and all production jobs exit 0,
producing no production activity.

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 8 — Git promotion (staging → production) &nbsp;·&nbsp; 4 operations</summary>

Cherry-pick the staging cherry-pick hash onto the production branch. This
preserves the full provenance chain in git history:
`develop squash → staging cherry-pick → production cherry-pick`.

- Extract JIRA ticket from staging commit message — same pattern as stage 1
- If no JIRA ticket found and no override set: write `SKIP_PROD=true` dotenv artefact and exit 0 — all downstream jobs will skip
- Read `git-promotion.json` from the release directory on the runner (SCPd there by the staging `git-promotion` stage); extract `cherry_pick_hash`
- `git checkout production && git pull` — ensure production branch is at HEAD
- Fetch cherry-pick commit with `--depth=2` to ensure parent is available (shallow clone guard)
- `git cherry-pick <staging_cherry_pick_hash>` — if the commit is already present ("now empty"), skip gracefully rather than hard fail
- `git push` — production branch updated; promotion hash written to `git-promotion-prod.json`
- If delete file is non-empty: `git rm` each listed path, commit, and push a second time

**Outputs:** `git-promotion-prod.json` `prod-pipeline.env (JIRA_TICKET, SKIP_PROD, PROD_RELEASE_DIR)`

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 9a — Recreate production test clone &nbsp;·&nbsp; 2 checks</summary>

Recreate the production test DB clone from the live production database so
Stage 9b deploys against a fresh copy of the current prod state.

- **SKIP_PROD guard** — if `SKIP_PROD=true` from Stage 8, exit 0 immediately
- If `SKIP_PROD_TEST_CLONE_RECREATE=true` (reusing staging infra as prod), log and skip the actual recreate
- Run `RECREATE_TEST_DB_SCRIPT` (or the prod-specific equivalent) — wait for completion; hard fail on non-zero exit
- Verify the test clone is reachable via a simple `psql` connection check

**Outputs:** `prod-test-clone-ready` (logged; no artefact file)

</details>

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 9b — Production test DB deploy &nbsp;·&nbsp; 5 checks</summary>

Run the full release against the production test DB clone and validate the
result before anything touches the live production database.

- **SKIP_PROD guard** — if `SKIP_PROD=true`, exit 0 immediately
- Execute `prep.sql` against prod test DB — DROP statements first
- SSH to prod VM, run `ci_backend_db_release.py <release_dir> --target test --skip-git` — deploy all SQL in `deploy.lst` order
- **HARD FAIL** — Count verification: SQL files processed in release log must equal non-commented entries in `deploy.lst`
- **HARD FAIL** — Run `check_for_duplicate_functions.sql` on prod test DB — must return zero rows
- Execute `post.sql` against prod test DB; verify `pg_attribute` alignment for all mutated tables
- Capture full release log as a downloadable pipeline artefact

**Outputs:** `prod-test-deploy.log` `prod-count-verification.json`

</details>

---

> ### Gate 4 — Production test result
>
> Pipeline posts the prod test deployment log and verification results. DevOps
> reviews before approving promotion to the live production database.
>
> - Report: log error summary, count match result, duplicate function check, audit table verification
> - DevOps reviews log and confirms the test DB is in the expected state
> - DevOps clicks **Approve** or **Decline**
>
> **Approve** → live production DB deployment &nbsp;&nbsp; | &nbsp;&nbsp; **Decline** → pipeline fails, investigate

---

<details>
<summary><strong>AUTO</strong> &nbsp; Stage 10 — Live production DB deploy &nbsp;·&nbsp; 7 checks</summary>

Deploy the release to the live production database. Restart GraphQL services on
all production nodes and verify both endpoints respond. This is the final
automated step before the audit record is permanently closed.

- **SKIP_PROD guard** — if `SKIP_PROD=true`, exit 0 immediately
- SSH to prod VM, run `ci_backend_db_release.py <release_dir> --target live --skip-git` — full release deployed to live prod DB
- Execute `post.sql` against live prod DB — audit table creation and column updates applied
- Restart GraphQL services on all `PROD_NODES` — `crm_restart`; `api_restart` per node
- **HARD FAIL** — HTTP health check: `GRAPHQL_API_URL_PROD` must return 200 — retry 3× with back-off
- **HARD FAIL** — HTTP health check: `GRAPHQL_CRM_URL_PROD` must return 200 — retry 3× with back-off
- **HARD FAIL** — Run `check_for_duplicate_functions.sql` on live prod DB — must return zero rows
- Transition Jira ticket to deployed-to-production via Jira REST API if `JIRA_TOKEN` is configured — log result, do not hard fail if unavailable
- Capture live release log and health check results as pipeline artefacts

**Outputs:** `prod-live-deploy.log` `prod-health-check.json` `Jira transitioned`

</details>

---

> ### Gate 5 — Production live confirmation
>
> Final sign-off for production. All artefacts are archived permanently.
>
> - Report: health check status, live deploy log link, Jira transition confirmation
> - DevOps confirms production is stable — Approve closes the release
> - All artefacts (`prep.sql`, `deploy.lst`, `post.sql`, all logs, all analysis JSON, both `git-promotion*.json`) archived as permanent pipeline artefacts
>
> **Approve** → release complete &nbsp;&nbsp; | &nbsp;&nbsp; **Decline** → investigate prod issue

---

## Infrastructure commits — SKIP_PROD pattern

Not every push to `staging` is a database release. Config changes,
workflow edits and dependency updates all land on staging but carry no
JIRA ticket in the commit message. The production pipeline stages need to handle
these without touching production.

**How it works:**

1. Stage 8 (`git-promotion-prod`) extracts the JIRA ticket from the staging
   commit message. If no ticket is found, Stage 8 writes `SKIP_PROD=true` to
   its `prod-pipeline.env` dotenv artefact and exits 0.
2. Every downstream production job declares `needs: [git-promotion-prod]` with
   `artifacts: true`, so the dotenv is loaded and `SKIP_PROD` is available as a
   CI variable.
3. Each downstream job (9a, 9b, gate-4, 10, gate-5, archive) checks this guard
   as its first script line:
   ```bash
   [ "${SKIP_PROD:-false}" = "true" ] && echo "Skipping — no JIRA ticket." && exit 0 || true
   ```
4. All jobs exit 0. No prod DB activity occurs. The pipeline completes green.

Gate-4 and gate-5 appear in the pipeline UI as manual jobs but will also exit 0
immediately if clicked — the SKIP_PROD guard runs before the reporter.

## Sprint bundled releases

Multi-PR aggregation, cross-PR duplicate SQL handling, and merge-file
construction are a separate orchestration problem. The current sprint SOP's
manual aggregation step is the equivalent of this pipeline running N times
with a fan-in at artefact generation. Out of scope.
