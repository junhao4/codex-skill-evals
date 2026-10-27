# Harness audit — 2026-10-27

## Scope and method

Reviewed discovery and trial setup, controlled inputs and snapshots, worker
execution/resume, deterministic checks, semantic quorum/abstention, deferred
regrading, trace/captured-child evidence, aggregation, comparison, pricing,
usage reports, and existing regression tests. Ran the complete test suite.
New defects below were reproduced with failing tests before implementation
changes, followed by regression runs. External Codex processes are simulated;
local checker processes and disposable Git repositories are real.

This is a source and automated-behavior audit, not a live CLI, browser,
penetration, or individual skill-rubric certification. MP3 and testing-project
artifacts were not modified. Existing historical evaluation results were not
regraded or rewritten.

## Fixed in this audit

| Priority | Defect | Correction | Regression |
|---|---|---|---|
| High | A completed deterministic failure plus semantic abstention became `needs_review`; aggregation excluded the concrete failure from decided trials. | A completed failing grader prevents the combined outcome from becoming `needs_review`. | `test_completed_grader_failure_is_not_hidden_by_other_grader_abstention` |
| High | Deferred semantic regrading treated absent deterministic evidence as `not_requested`, allowing semantic success to replace a required checker. | Before launching judges, require retained deterministic evidence when required by the case or recorded trial status; validate completed results. Preserve invalid and failing checker outcomes. | `test_regrade_requires_retained_deterministic_evidence`, `test_regrade_preserves_retained_checker_decision` |
| Medium | A deterministic checker emitting `insufficient_evidence` with score 100 was normalized to a passing result. | Reject deterministic abstention explicitly; the abstention mechanism belongs to semantic judges. | `test_checker_cannot_turn_abstention_into_passing_score` |
| Medium | Needs-review trials consumed tokens but were omitted from per-cell token means. | Collect token samples before excluding undecided scores. | `test_needs_review_trials_still_count_in_token_usage` |

These fixes affect newly computed results. Historical persisted summaries still
reflect the implementation that generated them.

## Remaining findings and confidence gaps

### Concurrent writers and experiment allocation

`next_experiment_id` scans existing folders without reserving an identifier.
`write_json` writes directly to destination files. Independent invocations can
select the same experiment identifier or overwrite a report/cache while another
writer is reading it. Sequential tests do not establish concurrent safety.
Next: atomic reservation, replacement writes, and process-level contention tests.

### Interrupted runs and unexpected exceptions

**Follow-up implemented:** trial recovery now records failed/interrupted results
for ordinary exceptions and Ctrl-C, retains known usage and partial evidence,
and the CLI includes those results in partial reports. Original exceptions are
preserved even if recovery writing fails. Worker/checker fault injection and
CLI integration cases cover these paths. The original finding below is now
partially addressed; forced termination and process-tree cleanup remain open.

The original finding was that the CLI finalized partial results only for
`KeyboardInterrupt` and `HarnessError`, and other exceptions could leave a trial
without a terminal result. The follow-up covers those recoverable exceptions.
`subprocess.run` timeout handling still lacks explicit process-group cleanup
for descendants. Forced-termination and durable restart recovery remain open.

### Fixture symlinks

**Follow-up implemented:** all fixture symlinks are now rejected during discovery,
before direct execution, and in the copied fixture before Git setup. Tests cover
internal/external file links, directory links, dangling/cyclic links, and linked
fixture roots. This closes the preserved-link path described below for validated
static fixtures. Concurrent source mutation and malicious worker containment
remain outside this boundary.

The original finding was that fixtures copied with `symlinks=True` could
retain absolute or escaping links outside the disposable workspace. Root
control-directory checks alone did not prevent that. The follow-up rejects all
fixture links, including internal ones, with source-preservation regressions.
Disposable workspaces are not by themselves an isolation boundary.

### Token completeness and cost estimates

Several aggregation paths substitute zero for unavailable token fields. Missing
child rollouts preserve a missing flag, but combined usage remains incomplete.
Cost estimation chooses a context tier from supplied aggregate token usage;
callers pass trial/session totals or means, which do not establish the context
size of individual model requests. Mixed-model child usage can also be priced
using a worker model in the Markdown experiment report. These are limitations
of cost accounting, independent of whether the pinned rates are current.
Next: explicit usage completeness and per-request, per-model accounting;
otherwise clearly label incomplete/approximate estimates. The pricing table
was not refreshed or externally verified in this audit.

### Parser, UI, and live behavior

More independently reviewed real-schema fixtures are needed for resumed,
duplicate, malformed, and out-of-order events. Python HTML and JavaScript unit
checks do not establish browser geometry or readability. Simulated workers do
not establish compatibility with a live installed CLI. Individual checkers and
rubrics need known-good and deliberately broken examples; structural `check`
validation cannot establish grading quality.

### Automated gate

The full suite is documented but there is no checked-in CI workflow in this
checkout. Add a repeatable gate and retain its output per revision. Test counts
include parametrization and a legacy wrapper; they are not coverage percentages.

## Verification

Full suite after the reliability follow-up: **250 passed** using `/opt/anaconda3/bin/python -m pytest tests/ -q`.
No paid model calls were made. See `testing.md` for the supported test setup and
explicit boundaries of the automated evidence.
