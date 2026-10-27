# Testing the harness

Run these checks from a source checkout of `codex-skill-evals`. Installed
copies produced by `degit` omit the development `tests/` directory.

```sh
python3 -m pip install -r requirements-test.txt
python3 -m pytest tests/ -q
```

The development dependency pins pytest 8.4.2. Git is needed for integration
tests. Automated tests use simulated Codex workers and judges; they do not
make model calls or require a signed-in Codex CLI.

## What the suite establishes

- Pure grading, quorum, abstention, aggregation, pricing, and comparison rules.
- Parsing live, persisted, legacy, and named collaboration event formats.
- Read-only rollout capture, provenance, profile injection and snapshots.
- Captured child recovery for live-cached and disk-discovered parents, nested
  lineage, missing-rollout findings, repeated import, and later recovery.
- Real disposable Git workspaces and exported tracked/untracked evidence.
- Trial protection against worker errors and changes to protected inputs.
- Fixture symlink rejection in discovery/direct execution, including nested,
  internal, external, dangling, cyclic, and root links.
- Ctrl-C and ordinary worker/checker exceptions retain explicit terminal trial
  records and appear in CLI partial reports; recovery write errors preserve the
  original interruption. Recorded worker usage survives later grading failure.
- Multi-turn resume and token accumulation using simulated worker processes.
- Regrading rejects missing worker evidence and preserves existing failures.
- Local checker execution rejects malformed output and nonzero exits.
- Simulated independent judges preserve quorum and handle failures/abstention.
- Relocated trial lookup and report links preserve historical source artifacts.
- Missing, invalid, zero, legacy, failed-trial, and needs-review timing values.
- CLI discovery works from unrelated working directories with installations at
  both `.agents/evals/` and `workflow/evals/`.
- Trace timestamps share the agent map's run origin; late settings updates do
  not extend task spans, and missing timestamps do not invent event positions.
- Filesystem diagrams preserve recorded grants and read-only overrides; absent
  permissions/network state remain unknown. Actor selection matches exact IDs.
- Context source markers do not inherit document-card styles. Directory listings
  and failed reads are excluded from file-content estimates. The capacity gauge
  uses the last recorded model input; missing or invalid snapshots stay unknown.

On 2026-10-27 the full suite passed **250 collected test invocations**. This
includes parametrized cases and a legacy unittest wrapper that repeats some
usage scenarios. It is not a statement or branch coverage percentage.

## Remaining checks

The automated suite does not establish real-browser layout/readability, live
Codex CLI compatibility, model judgment quality, forced-termination recovery,
process-tree cleanup, or safe behavior under concurrent cache/report writers. Keep these as explicit
coverage gaps rather than treating a green suite as evidence for them.

Each skill evaluation also needs its own checker calibration: construct a
known good result, deliberately break required behavior or evidence, invoke the
real checker, and verify the resulting grade. Validate the checker on missing
artifacts, misleading filenames/role names, unsupported trace shapes, and
out-of-scope edits. `eval.py check` validates structure; it does not establish
that a checker distinguishes correct work from incorrect work.

The MP3 comparison and the origin of imported regression coverage are recorded
in [MP3 upstream review](mp3-upstream-review.md).

The broader 2026-10-27 source audit, reproduced defects, fixes, and remaining
risks are recorded in [Harness audit](harness-audit.md).
