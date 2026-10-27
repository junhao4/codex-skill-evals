# MP3 harness comparison — 2026-10-26

Compared the complete reusable harness file set from the local MP3 installation
against both upstream's committed revision and its current working tree.

- Source installation: `/Users/junhaong/Documents/y3s1/CS3227/CS3227-2610-MP3/workflow/evals`.
- MP3 checkout HEAD: `253f8f4699185a89bdd4c38be8d62eca2448d86c`.
- MP3 records its upstream installation revision as `1bb3e98`.
- Upstream committed revision: `1bb3e9858ca34a62a91eefbd06295dd32f5b424b`.
- MP3 was read only. Its existing untracked evaluation runs were not changed,
  copied, or executed. MP3 tests were inspected, not run in place.
- Remote branches and archived worktrees are outside this local-version comparison.

## Result

Most advertised MP3 harness features are already in upstream: the CLI,
agent-profile helpers, grading rules, relocation lookup, timing, report links,
comparison logic, and named subagent parsing. Several apparent source diffs
are trailing whitespace; MP3's UI is the older committed renderer.

The useful missing material was regression coverage and a small inventory fix.
Imported or adapted **22 test invocations**: four profile tests, six path and
inventory tests, five timing tests, three report-link tests, and four subagent
trace tests. At that port checkpoint the full suite passed **195 invocations** with pytest 8.4.2.

The inventory fix stops treating an arbitrary sibling `workflow/skills` folder
as installed Codex skills. Discovery now uses the documented `.agents/skills`
location regardless of whether the harness is installed at `.agents/evals` or
`workflow/evals`. Both layouts have explicit regression coverage.

Also imported the test dependency pin and generic documentation clarifications.
MP3's hardcoded workflow installation paths, dated usage snapshot, and product
routing policies were not applied to the generic harness.

## Complete reusable-file disposition

This covers 41 relevant paths across implementation, entry point, tests,
documentation, dependency declaration, installation configuration, and the MP3
usage snapshot. Generated caches, bytecode, run artifacts, and OS metadata
were excluded from source comparison.

| Path | Decision |
|---|---|
| `.gitignore` | Keep upstream: its recursive usage-artifact ignores cover more layouts. |
| `README.md` | Keep generic installation; add a development-test documentation link. |
| `_lib/__init__.py` | Already identical; no port needed. |
| `_lib/agent_profiles.py` | Already identical; no port needed. |
| `_lib/common.py` | Only a trailing blank line differs; no port needed. |
| `_lib/compare.py` | Already identical; no port needed. |
| `_lib/git_workspace.py` | Only a trailing blank line differs; no port needed. |
| `_lib/grade.schema.json` | Already identical; no port needed. |
| `_lib/grading.py` | Already identical; no port needed. |
| `_lib/harness.py` | MP3 matches committed upstream; retain current upstream regrade-evidence guard. |
| `_lib/paths.py` | Already identical; no port needed. |
| `_lib/pricing.py` | Only a trailing blank line differs; no port needed. |
| `_lib/report.py` | Already identical; no port needed. |
| `_lib/timing.py` | Already identical; no port needed. |
| `_lib/trace.py` | Only a trailing blank line differs; named-activity support is already upstream. |
| `_lib/usage.py` | Port canonical .agents/skills inventory; keep portable notify/root defaults and current activity-span fixes. |
| `_lib/usage_html.py` | MP3 matches committed upstream; retain current AA/Z renderer changes here. |
| `degit.json` | Keep upstream distribution rule that omits development tests. |
| `docs/authoring.md` | Port generic profile evidence/sandbox guidance; keep .agents/evals examples. |
| `docs/outputs.md` | Port relocation, unavailable-evidence, concurrent-duration, and ignored-artifact clarifications. |
| `docs/running.md` | Installation-path substitutions and whitespace only; retain generic paths. |
| `eval.py` | Already identical; no port needed. |
| `requirements-test.txt` | Port pinned pytest 8.4.2 development dependency. |
| `tests/test_agent_profiles.py` | Port four generic tests; existing upstream integration tests cover injection/tampering with synthetic cases. |
| `tests/test_aggregate_tokens.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_compare.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_cost.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_harness_integration.py` | Upstream-only new orchestrator regression suite; preserve. |
| `tests/test_paths.py` | Port five relocation tests; adapt CLI check to synthetic installs in both layouts; add inventory regression (six methods total). |
| `tests/test_provenance.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_report_tokens.py` | Port three report-link tests; retain the stronger existing unknown-timing assertion. |
| `tests/test_review_routing.py` | MP3-specific grader calibration; do not copy product skill names, policies, or fixture dependencies. |
| `tests/test_run_trial_logic.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_skill_snapshot.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_subagents.py` | Port four named-activity, pending-spawn, wait-order, and injected-skill tests. |
| `tests/test_timing.py` | Port all five timing tests. |
| `tests/test_trace_metrics.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `tests/test_usage.py` | Keep default-layout tests; new path tests cover workflow/evals without replacing existing coverage. |
| `tests/test_usage_timeline.py` | Upstream-only new activity-span and linked-trace regression suite; preserve. |
| `tests/test_verdict_abstention.py` | Only test-command examples and/or trailing whitespace differ; no behavior change to port. |
| `usage-report-2026-10-23.md` | Project-specific generated snapshot; do not port. |

## Evaluation definitions and calibration outside the reusable package

MP3 has four local evaluation suites: `code-review`, `implement`,
`implement-task`, and `plan-task`. The current directories contain 18 case
checkers. Additional calibration suites under `grader-tests/` exercise the
implementation and planning oracles; `tests/test_review_routing.py` calibrates
the two review-routing checkers.

These enforce project/workflow contracts: receipts and stage order, planning
approval, correction budgets, owned output paths, role selection, forbidden
parent/child reads, source immutability, and functional application oracles.
Their fixtures and assertions depend on MP3 skill definitions and product
artifacts, so copying them would violate the upstream harness's convention of
shipping no target skills or test cases.

Their useful reusable pattern is **checker calibration**: synthetic passing
and deliberately invalid workspaces, real checker execution, alternate event
formats, and assertions that misleading path/role names or missing evidence
do not earn points. This is described in `docs/testing.md`. A future explicit
command for discovering/running case-local calibration tests could generalize
that pattern; no such CLI feature was introduced in this port.

## Validation

Executed in upstream only:

```sh
python3 -m pytest tests/ -q
```

Actual environment: `/opt/anaconda3/bin/python`, pytest 8.4.2. Result: 195 passed.
No model calls were made. See `docs/testing.md` for remaining confidence gaps.
