# Test Suite

The tests protect the investment tracker redesign and workbook contract. They are intentionally deterministic: workbook tests run with `--offline`, a fixed `--run-date`, a temporary `--output`, and copied JSON data where mutation is required.

## Running Tests

From the repository root:

```bash
python3 -m compileall build_alert_levels.py modules workbook
python3 -m unittest discover -s tests
python3 -m pytest
```

CI runs the compile and `pytest` commands in both GitLab and GitHub.

The live provider check is separate and opt-in — see [Live Provider Check](#live-provider-check):

```bash
python3 tests/live_smoke.py
```

## Test Files

66 collected tests across eight modules.

| File | Tests | Purpose |
|---|---:|---|
| `test_phase0_contract.py` | 4 | End-to-end offline workbook contract. Verifies import safety, all four sheets, closed D1-D6 regressions, snapshot cells, representative formatting, same-run removal, and duplicate added-ticker rendering. |
| `test_calculations.py` | 2 | Pure Fib, upside-to-ATH, and alert-zone classification rules. |
| `test_cli.py` | 12 | Argument parsing, injectable run clock, `--offline` handling, exclusion of tickers from the fetch list, and the full/partial/static data-source label. |
| `test_data_store.py` | 10 | JSON row loading/saving, unknown-field preservation, duplicate ticker preservation, removal matching, and `PortfolioRow` identity and predicates. |
| `test_market_data.py` | 9 | Provider data normalization for P/E, D/E, margin, and float coercion, plus the shared moving-average and Fib-anchor computation and the Alpha Vantage call budget. |
| `test_market_orchestration.py` | 16 | The code that calls the provider, driven through injectable clients: symbol aliasing, per-ticker failure isolation, `.info` price override, EPS provenance, and Alpha Vantage rate-limit and sleep behaviour. |
| `test_market_sources.py` | 2 | Data-driven macro/sentiment ticker classification sets and defaults. |
| `test_portfolio.py` | 11 | Unified portfolio-row assembly, `--add` refresh behavior, same-ticker preservation, and live overlays that do not rewrite anchors/notes. |
| `live_smoke.py` | — | Not collected. Live provider check, run explicitly. |

## Workbook Contract Scope

The workbook contract checks high-signal behavior instead of snapshotting every cell:

- sheet names,
- stale generated text absence,
- one `IN BUY ZONES` section header,
- static/offline source label,
- no formulas in Alert Levels core domain cells `G:L`,
- representative fill and number formats,
- upside-to-ATH consistency between sheets,
- EPS provenance display behavior,
- persistent `--remove` same-run rendering behavior,
- duplicate portfolio rows sharing a ticker both render.

This keeps the tests stable while still catching the regressions that previously made the workbook misleading.

For refactors, a stronger check is available and was used throughout the package
split: generate the offline workbook before and after, then diff every cell's
value, number format, fill, font colour, weight, size, and alignment, plus
merges, column widths, row heights, and freeze panes. That is too slow and too
brittle to keep in the suite, but it is the right tool when the claim is "no
behaviour change" — it turns that claim into a measurement.

## Live Provider Check

Every collected test runs `--offline`. That is what makes them fast and
deterministic, and it is also a real limit: the suite exercises no provider
payload, so it cannot detect a breaking `yfinance` release. The shapes returned
by `.info` and the `yf.download` signature are exactly what it never touches. A
dependency bump can pass CI and still leave every price blank.

`live_smoke.py` covers that gap. It is deliberately not named `test_*`, so
neither `pytest` nor `unittest discover` collects it. It trims the fetch list to
three tickers, runs without `--offline`, and asserts that the data-source banner
reports a live refresh and that prices and D200 values arrive as numbers rather
than blanks.

Smoke tickers are derived at run time from `yahoo_fetch` intersected with the
portfolio rows, so a ticker that is fetched but has no row cannot produce a false
failure, and the check survives portfolio edits.

Both CI hosts carry it as an opt-in job — GitLab `live_smoke` (`when: manual`),
GitHub `.github/workflows/live-smoke.yml` (`workflow_dispatch` plus a weekly
cron). It never gates a merge request, because it depends on a third party.
Trigger it after a dependency bump.

## Known Boundaries

Provider orchestration is covered without network. `fetch_batch`,
`fetch_ticker`, and `fetch_alpha_vantage_eps` each take an injectable client,
and `portfolio.refresh_portfolio_rows(..., fetch=...)` takes an injectable
fetch, so every call path into a provider can be driven with stubs.

What the suite still cannot tell you is whether the real payloads match those
stubs — that is what `live_smoke.py` is for.

The suite protects stable row identity for placeholder tickers such as `TBC`:
ticker is treated as a provider lookup key, not as a row uniqueness key.
