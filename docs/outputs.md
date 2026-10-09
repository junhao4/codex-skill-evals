# Evaluation outputs

Reference for the files written under `runs/` by each experiment and trial. See [running](running.md) for how these are produced.

### Where the outputs go

```text
.agents/evals/<skill-name>/runs/experiment-NNN/
├── report.md
├── experiment.json
└── trials/
    └── <test-case-name>/
        └── <configuration>/
            └── trial-NN/
```

Start with `report.md`. It is the only file intended for ordinary reading.
The harness writes it directly from the collected results; creating it does not
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
| `report.md` | Updated when the experiment completes, fails, or is interrupted | Human-readable outcome, mean score, run provenance (model, reasoning effort, trials, date), trial table, baseline comparison, token usage, and main failure reasons |
| `experiment.json` | Created before the first trial and finalized when the experiment stops | Complete machine-readable manifest, status, aggregate results, and all trial summaries |

Open `experiment.json` only when a script needs the data or when you need more
detail than the report provides. Follow the links in `report.md` into
`trials/` when investigating a particular result.

Each trial directory contains:

| Output | Meaning |
|---|---|
| `result.json` | Final trial result, separate grader scores, model, and a `tokens` breakdown (uncached/cached input, output, reasoning, billed output) |
| `run.json` | Trial inputs, configuration, initial Git state, invocation mode, turn metadata, thread id, and resolved model/reasoning effort read from the rollout |
| `workspace/` | Final file snapshot left by the worker; its temporary `.git/` is removed after the graders finish |
| `worker/result.json` | Worker exit status, metrics, final Git state, protected-input checks, and untracked paths |
| `worker/trace.jsonl` | Combined worker events and tool calls from every turn |
| `worker/turns/turn-NN/prompt.md` | Exact user message for one worker turn |
| `worker/turns/turn-NN/response.md` | Worker response for that turn |
| `worker/turns/turn-NN/trace.jsonl` | Events and tool calls from that turn |
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
