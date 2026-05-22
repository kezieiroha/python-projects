# Test fixtures

SQL fixture files used by `tests/test_s2_static.py` to exercise the static
analysis checks without a database connection.

Each file is a minimal but realistic SQL script. The `bad_*` files contain
exactly one violation each so tests can assert on a specific failure in
isolation. The `good_*` files pass all checks and also serve as extraction
targets for signature, type, and mutation parsing.

## How the tests use these files

The fixtures are plain text. Tests read them with `_read()` and pass the string
content directly to the imported analysis functions — no subprocess, no database,
no filesystem scanning beyond the initial file read.

```
fixture file on disk
      │
      ▼  open().read()
  content: str
      │
      ▼  check_set_role("bad_no_setrole.sql", content)
  returns: list[str]   # failure messages, or [] if clean
      │
      ▼  assert len(result) == 1
```

The check and extraction functions (`check_set_role`, `check_privilege_escalation`,
`extract_function_signatures`, etc.) are pure functions — string in, list out.
A `bad_*` fixture produces a non-empty failure list; a `good_*` fixture produces
`[]`.

The `TestRun` integration tests go one level up: they build a `Manifest` pointing
at this directory and call `run()`, which internally invokes all checks and
returns an updated manifest plus a report dict. Assertions then inspect
`manifest.has_hard_fail`, `report["hard_fails"]`, `report["function_signatures"]`,
and so on.

The fixtures exist so tests have realistic multi-line SQL strings without
embedding them as inline literals in the test file.

---

## Files

### Passing fixtures

| File | Classification | Purpose |
|---|---|---|
| `good_function.sql` | `function` | Baseline valid file. Passes all checks. Used by signature-extraction tests — defines `public.get_user_by_id(uuid)` returning a table. Uses unquoted identifiers. |
| `good_function_quoted.sql` | `function` | Valid function with double-quoted identifiers (`"acme_api"."get_reward_estimate"`). Verifies that `_FUNC_RE` matches both quoted and unquoted forms. |
| `good_schema_create_table.sql` | `schema` | Valid `CREATE TABLE public.invoices`. Used by table-mutation extraction tests. |
| `good_schema_add_col.sql` | `schema` | Two `ALTER TABLE public.orders ADD COLUMN` statements. Used by add-column mutation extraction tests. |
| `good_type.sql` | `type` | Valid `CREATE TYPE public.order_status AS ENUM`. Used by type-definition extraction tests. |

### Failing fixtures

| File | Violation | Check triggered |
|---|---|---|
| `bad_no_setrole.sql` | Missing `SET ROLE "acme_admin"` at top of file | `check_set_role` — hard fail |
| `bad_privilege_escalation.sql` | Contains `GRANT EXECUTE ... TO web_user` | `check_privilege_escalation` — hard fail |
| `bad_ddl_in_function.sql` | Contains `CREATE TABLE` inside a function-classified file | `check_ddl_in_wrong_file` — hard fail |
| `bad_drop_misplaced.sql` | Contains `DROP FUNCTION` inside a schema-classified file | `check_drop_in_wrong_location` — hard fail |
| `cascade_warning.sql` | Contains `CASCADE` in a comment outside a DROP statement | `check_cascade_warnings` — warning (not hard fail) |

---

## Conventions

All fixtures carry a standard header comment block (commit hash, Jira ticket,
PR number, timestamp) and begin with `SET ROLE "acme_admin";`, except
`bad_no_setrole.sql` which intentionally omits it.

The static analysis checks strip line comments before scanning for most
keywords, but `check_privilege_escalation` and `check_cascade_warnings`
deliberately do not strip comments — those tests verify that the checks fire
even when the keyword appears inside a comment.
