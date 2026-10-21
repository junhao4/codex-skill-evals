# Evaluation outputs

Reference for the files written under `runs/` by each experiment and trial. See [running](running.md) for how these are produced.

### Where the outputs go

```text
.agents/evals/<skill-name>/
├── comparisons/
│   └── <old-exp>-vs-<new-exp>.md
└── runs/
    └── experiment-NNN/
        ├── report.md
        ├── experiment.json
        └── trials/
            └── <test-case-name>/
                └── <configuration>/
                    └── trial-NN/
```

Start with `report.md` (or `comparisons/<old-exp>-vs-<new-exp>.md` after running `eval.py compare`). They are the files intended for ordinary reading.
The harness writes them directly from the collected results; creating them does not
make another model call.

At the end of a normal run, the terminal points directly to it:

```text
Evaluation complete.
Result: PASS
Score: 84/100
Report: .../.agents/evals/<skill-name>/runs/experiment-NNN/report.md
```

| Output | When written | Meaning |
|---|---|---|
| `report.md` | Updated when the experiment completes, fails, or is interrupted | One-line `Setup` provenance header (model, reasoning effort, trials, judges, status, start time), horizontal `Result` summary table (outcome, mean score, passed, needs review, completed, est. total cost), `Trials` table (`Score`, `Det`, `Sem`, `Band`, `Pass`, `Evidence`), `Findings` (failures and passing score deductions), `Baseline comparison`, and `Token usage` (mean per cell + total row) |
| `experiment.json` | Created before the first trial and finalized when the experiment stops | Complete machine-readable manifest, status, aggregate results, and all trial summaries |
| `comparisons/<old>-vs-<new>.md` | Written by `eval.py compare <skill> <old> <new>` | Cross-experiment diff (`Setup`, `Summary`, `Case overview`, per-case score/token/cost breakdown, and `SKILL.md` snapshot diff) |

Open `experiment.json` only when a script needs the data or when you need more
detail than the report provides. Follow the links in `report.md` into
`trials/` when investigating a particular result.

When `report.md` or `eval.py compare` reads trial evidence through a recorded
`run_directory`, the harness resolves relocated checkouts against the active
evaluation root first and never rewrites historical manifests.

### Worker runtime

`report.md` includes a `Worker runtime` table per cell (`Timed trials`,
`Mean worker time`, `Min–max`) and a `Worker time` column in `Trials`.
`comparisons/<old>-vs-<new>.md` adds `Mean worker time (v1 -> v2)` and
`Time change` (`v2 − v1`; negative means faster).

Worker time is wall-clock execution across all turns, including tool calls and
subagent waits; it excludes workspace setup, subsequent evidence capture, and
grading. Means and ranges include all timed trials regardless of pass/fail or
`needs_review` outcome. Legacy runs that only recorded timing in
`worker/metrics.json` are read without modifying the saved artifacts; runs
with no timing data render as `N/A`.

Each trial directory contains:

| Output | Meaning |
|---|---|
| `result.json` | Final trial result, separate grader scores, model, and a `tokens` breakdown (uncached/cached input, output, reasoning, billed output; automatically rolls up parent + spawned subagent usage when subagents ran) |
| `run.json` | Trial inputs, configuration, initial Git state, skill snapshot paths, invocation mode, turn metadata, thread id, and resolved model/reasoning effort read from the rollout |
| `skill-snapshot/` | Immutable copy of the injected target skill (and any supporting skills) captured at trial start for `eval.py compare` |
| `agent-profile-snapshot/` | Immutable copy of any declared `.codex/agents/<name>.toml` profiles injected for the trial |
| `workspace/` | Final file snapshot left by the worker; its temporary `.git/` is removed after the graders finish |
| `worker/result.json` | Worker exit status, rolled-up metrics, final Git state, protected-input checks, and untracked paths |
| `worker/metrics.json` | Normalized trace and token metrics, including `parent_token_usage`, `subagent_token_usage`, `subagent_spawn_count`, `subagent_wait_count`, `subagent_thread_ids`, and `subagents` summary records |
| `worker/trace.jsonl` | Read-only copy of the worker's persisted session rollout from `~/.codex/sessions/` (with only opaque `encrypted_content` ciphertext stripped), or concatenated per-turn `codex exec --json` stdout traces when no rollout file exists |
| `worker/subagents/manifest.json` | Created when the worker spawns subagents; records parent/child lineage (`depth`, `parent_thread_id`), agent role/nickname, model, prompt, wait status, final status/message, and per-subagent token usage |
| `worker/subagents/<thread-id>.jsonl` | Read-only copy of each spawned child (or nested grandchild) rollout captured from `~/.codex/sessions/` when available, with only `encrypted_content` stripped |
| `worker/turns/turn-NN/prompt.md` | Exact user message for one worker turn |
| `worker/turns/turn-NN/response.md` | Worker response for that turn |
| `worker/turns/turn-NN/trace.jsonl` | Live `codex exec --json` stdout stream for that turn |
| `worker/turns/turn-NN/stderr.log` | Diagnostics from that turn |
| `worker/turns/turn-NN/state.json` | Git state immediately after that turn |
| `git/changes.patch` | All final tracked changes compared with the prepared workspace base |
| `git/committed.patch` | Changes committed by the worker after the prepared workspace base |
| `git/index.patch` | Prepared workspace base compared with the final staged index |
| `git/unstaged.patch` | Final unstaged changes compared with the index |
| `git/untracked.patch` | Contents of worker-created untracked files, including Git binary patches when needed |
| `git/status.txt` | Final short Git status, including staged, unstaged, deleted, renamed, and untracked paths |
| `git/history.txt` | Compact commit and tag history captured before temporary Git metadata is removed |

The following grader directories are optional and therefore appear last:

| Output | Created when | Meaning |
|---|---|---|
| `grades/deterministic/result.json` | The case has `check.py` | Validated deterministic grade plus checker status and errors |
| `grades/deterministic/output.txt` | The case has `check.py` | Checker's raw standard output, retained to diagnose invalid JSON |
| `grades/deterministic/stderr.log` | The case has `check.py` | Checker diagnostics |
| `grades/semantic/result.json` | Semantic grading is enabled and the case has `expected.md` | Jury consensus, quorum, individual grades, and judge errors |
| `grades/semantic/input/` | Semantic grading is enabled and the case has `expected.md` | Private expectation and copied output schema given to the judges |
| `grades/semantic/judge-NN/output.json` | That semantic judge ran | One judge's raw grade |
| `grades/semantic/judge-NN/trace.jsonl` | That semantic judge ran | One judge's model trace |
| `grades/semantic/judge-NN/stderr.log` | That semantic judge ran | One judge's diagnostics |

Every worker trial begins from a fresh fixture copy. Repeated trials never share
a workspace, and neither workers nor judges modify the original fixture.
