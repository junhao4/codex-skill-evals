# Codex Skill Evals

A reproducible harness for evaluating project-local Codex skills against
repeatable software-engineering tasks.

> **Status:** Public alpha. This harness is Codex-specific and should be
> validated in a disposable project before being used in important workflows.
>
> **Safety and cost:** Evaluations execute `codex exec` with write access to
> disposable workspaces, so only run trusted skills and avoid exposing
> unnecessary secrets. Model calls may use Codex quota or incur usage costs:
> a normal semantic evaluation uses one worker call plus the configured
> semantic-judge calls for each test case. Use `check` first, then
> `--no-semantic-judges` for a cheaper initial run.

## Install

This harness is designed to be installed into an existing Codex project at
`.agents/evals/`. It evaluates project-local skills from the sibling
`.agents/skills/` directory.

## Prerequisites

Before running an evaluation, make sure the project has:

- Python 3;
- Node.js 20 or later and `npx` (used by the installation command);
- the Codex CLI installed and enabled for the environment;
- a signed-in or otherwise configured Codex CLI session; and
- Git, which the harness uses to create and inspect isolated trial workspaces.

The Codex CLI is required because worker trials and semantic grading run through
`codex exec`. Installing this repository does not install or enable Codex, and
the harness cannot run model-based evaluations until the Codex CLI is available
and authorised.

## Install into an existing project

From the root of the project you want to evaluate, run:

```bash
npx degit junhao4/codex-skill-evals .agents/evals
```

The command downloads the harness without creating a nested Git repository.
Run it from the target project root so that the files are placed in that
project's `.agents/evals/`, rather than in a home-level or unrelated
`.agents/` directory. If `.agents` does not exist yet, create it first:

```bash
mkdir -p .agents
npx degit junhao4/codex-skill-evals .agents/evals
```

Do not run the command if `.agents/evals` already contains an evaluation
harness unless you intend to replace it. Pin a release when you need a stable
version:

```bash
npx degit junhao4/codex-skill-evals#v0.1.0 .agents/evals
```

Verify the installation from the project root:

```bash
python3 .agents/evals/eval.py list
```

The harness itself does not include the skill being evaluated or any test
cases. Add those to the target project separately:

```text
.agents/
├── skills/
│   └── <skill-name>/
│       └── SKILL.md
└── evals/
    ├── eval.py
    ├── _lib/
    └── <skill-name>/
        └── test-cases/
```

## Configure and run skill evaluations

This harness evaluates project-local Codex skills against repeatable test cases.
It is convention-based: matching folder names connect a skill to its tests, so
there is no central suite file or registry to maintain.

To evaluate a skill:

1. create an evaluation folder with the same name as the skill;
2. add one or more test cases;
3. use `eval.py` to list, check, or run them.

Adding a skill or test case does not require changes to `eval.py` or `_lib/`.

Names inside angle brackets are placeholders. Replace them with lowercase
kebab-case names without the `<` or `>` characters.

| Placeholder | Meaning | Example |
|---|---|---|
| `<skill-name>` | Skill directory name | `code-review` |
| `<test-case-name>` | One scenario for that skill | `missing-regression-test` |

The key relationship is:

```text
.agents/skills/<skill-name>/SKILL.md
                  │
                  └── evaluated by ──> .agents/evals/<skill-name>/test-cases/
```

## 1. Add a skill to evaluate

Create the skill and a matching evaluation directory:

```text
.agents/
├── skills/
│   └── <skill-name>/
│       └── SKILL.md
└── evals/
    ├── eval.py             # command-line entry point
    ├── _lib/               # internal harness implementation
    └── <skill-name>/
        ├── test-cases/
        └── runs/              # generated automatically after eval is run
```

| Path | Required? | Purpose |
|---|---:|---|
| `.agents/skills/<skill-name>/SKILL.md` | Yes | The skill being evaluated—the system under test |
| `.agents/evals/<skill-name>/test-cases/` | Yes | Holds the scenarios used to evaluate that skill |
| `.agents/evals/<skill-name>/runs/` | Generated | Numbered experiments, human-readable reports, workspaces, traces, and grades |

The two `<skill-name>` directory names must match exactly. A skill also needs at
least one valid test case before it can be run. You do not manually populate
`runs/`; the harness creates an experiment there each time `eval.py run` starts.

Check whether the harness discovers it:

```bash
python3 .agents/evals/eval.py list
```

No model is called by `list`.

## 2. Add a test case

A skill should normally have several test cases covering different tasks,
starting states, and failure modes. Create one folder per scenario:

```text
.agents/evals/<skill-name>/test-cases/<test-case-name>/
├── fixture/              # files copied into a fresh repository
├── prompt.md             # first user turn
├── expected.md           # private instructions for semantic judges
├── check.py              # deterministic grader
├── supporting-skills/    # optional skills needed by this scenario
└── interactions/         # optional follow-up user turns
```

| Item | Required? | Visible to worker? | Purpose |
|---|---:|---:|---|
| `fixture/` | Yes | Yes, as its workspace | Files that form the repository state before the task |
| `prompt.md` | Yes | Yes | Exact first user message sent to `codex exec` |
| `expected.md` | Conditional | No | Qualities and requirements that need semantic judgement |
| `check.py` | Conditional | No | Repeatable checks over code, Git state, and trace evidence |
| `supporting-skills/` | No | Yes | Controlled dependency skills available in both baseline and with-skill runs |
| `interactions/` | No | Yes, one at a time | Ordered follow-up user messages sent to the same Codex thread |

At least one grader is required: `expected.md`, `check.py`, or both. Use both
when a task contains mechanically checkable facts and qualities requiring
judgement.

### `fixture/`: the input repository

Every trial gets a fresh copy of this directory. The worker modifies the copy,
never the original fixture.

| Requirement | Reason |
|---|---|
| It contains the relevant code and task context | The worker must have enough information to do the task |
| It does not contain `.git/` | The fixture remains an ordinary directory that the main project can track; the harness creates Git history in each disposable copy |
| It does not contain `.agents/` | The harness controls every repository-local skill exposed to the worker, while private evaluator files remain outside the workspace |

Different test cases may—and usually should—use different fixture directories.

After copying the fixture, the harness initializes a temporary Git repository
in the disposable workspace. It creates an empty `fixture-base` revision,
commits the fixture files as `fixture-target`, then injects `TASK.md` and the
controlled skills in one local setup commit. The setup commit is the trial's
evidence base, so harness-provided inputs do not appear as worker changes. None
of these commits modify the original fixture directory.

The temporary `.git/` exists while the worker and graders run. After all graders
finish, the harness exports the Git evidence under `git/` and removes
`workspace/.git/`. The retained `workspace/` is therefore an ordinary snapshot
of the worker's final files, not a nested repository. If a trial is interrupted
before grading finishes, `.git/` is retained to help diagnose the incomplete
run.

The harness constructs the repository-local skill set after copying the
fixture:

| Configuration | Target skill | `supporting-skills/` |
|---|---:|---:|
| `baseline` | Not injected | Injected |
| `with-skill` | Injected from `.agents/skills/<skill-name>/` | Injected |

The `<skill-name>` shared by `.agents/skills/<skill-name>/` and
`.agents/evals/<skill-name>/` identifies the target skill. Because the fixture
contains no `.agents/`, these injections are the only repository-local skills
available in the disposable workspace.

This does not hide skills installed at user, administrator, or Codex system
scope. In particular, a user-level skill with the same name could contaminate a
baseline comparison. The current harness controls repository-local skills; it
does not yet provide complete process-level isolation. Treat `--compare` as
causal evidence only after checking that no externally installed skill shares
the target name.

### `prompt.md`: the exact worker prompt

There is no metadata or special Markdown format. Write exactly what you would
send to the coding agent. The entire file is passed unchanged to `codex exec`.

The prompt itself determines the invocation test:

| Test type | What `prompt.md` contains |
|---|---|
| Explicit invocation | Names the skill, for example `Use the $code-review skill.` |
| Implicit invocation | Describes the task without naming the skill |

Example explicit prompt:

```text
Use the $code-review skill to review the implementation for Issue #1.
Follow the project guidance, inspect the tests and Git diff, and write the
canonical report. Do not modify source code or tests.
```

Example implicit prompt:

```text
Review the implementation for Issue #1. Follow the project guidance, inspect
the tests and Git diff, and write the canonical report. Do not modify source
code or tests.
```

Do not put grader answers, scoring rules, or hidden expectations in `prompt.md`.

### `expected.md`: what semantic judges assess

Use this for correctness or quality that cannot be reduced safely to exact
string and file checks—for example architecture, meaningful test coverage,
workflow discipline, or an honest final handoff.

Example:

```markdown
# Expected behaviour

1. Fixes the root cause — 40 points
2. Adds a meaningful regression test — 25 points
3. Respects scope and dependency boundaries — 15 points
4. Runs verification and reports the result honestly — 20 points

Essential pass conditions: the defect is fixed, the regression test passes,
and no prohibited files change.
```

Make the criteria total 100 points and state essential pass conditions
separately. The worker cannot see this file.

### `check.py`: what deterministic grading assesses

Use deterministic checks wherever the answer can be computed from observable
evidence.

| Example check | Evidence source |
|---|---|
| Required or forbidden files changed | Git diff or completed workspace |
| Required tests passed | `trace.jsonl` command events |
| A symbol, endpoint, or report exists | Completed workspace |
| The intended skill was read | `trace.jsonl` command events |
| A prohibited command was attempted | `trace.jsonl` command events |
| The diff has whitespace errors | `git diff --check` |

The harness calls the checker as:

```text
python3 check.py \
  --workspace <finished-repository> \
  --trace <trace.jsonl> \
  --run <run.json>
```

For a multi-turn case, `trace.jsonl` contains all turns. A checker that needs
turn boundaries can derive the turn directory from the supplied run path:

```python
turns = Path(args.run).parent / "worker" / "turns"
```

Each `turn-NN/` contains that turn's user prompt, response, trace, diagnostics,
and immediate repository state.

It must print exactly one grade JSON object:

```json
{
  "overall_pass": true,
  "score": 100,
  "checks": [
    {
      "id": "required-file",
      "pass": true,
      "points": 100,
      "notes": "The required file exists."
    }
  ],
  "limitations": []
}
```

The output must match [`_lib/grade.schema.json`](_lib/grade.schema.json), and
the top-level `score` must equal the sum of all `checks[].points`.

Keep `_lib/` limited to generic harness infrastructure. Project-specific facts,
such as source-directory names, frameworks, endpoints, user roles, or required
behaviour, belong directly in the relevant test case's `check.py`. For this
small harness, keeping each deterministic grader self-contained is clearer than
introducing another shared-helper directory.

### `supporting-skills/`: optional skill dependencies

Use this only when the target skill is expected to collaborate with another
skill. Put a complete skill directory under it:

```text
supporting-skills/
└── <supporting-skill-name>/
    └── SKILL.md
```

The harness injects supporting skills into both configurations. A baseline
therefore means "without the target skill", not "without every skill". This
keeps the target skill as the one experimental variable. The target skill must
not also appear under `supporting-skills/`.

Injected target and supporting skills are protected inputs. A trial fails if
the worker edits or deletes one of them.

### `interactions/`: optional multi-turn user messages

Use interactions when the workflow requires user feedback, clarification, or
approval. `prompt.md` is turn 1. Files under `interactions/` are later turns,
sent in filename order to the same Codex session:

```text
interactions/
├── 01-approve-plan.md
└── 02-answer-question.md
```

Each filename must match `NN-description.md`. Each file contains only the next
user message. There is deliberately no `expect` field: the harness sends the
scripted turns unconditionally, then `check.py` and the semantic judges assess
whether the worker behaved correctly at each stage.

For a single-turn case, the worker runs ephemerally. For a multi-turn case, the
first `codex exec` creates a persisted session and each interaction uses
`codex exec resume` with the thread ID from turn 1. This is why each interaction
adds one worker model call. The installed Codex CLI must support `codex exec
resume`.

Structurally validate the evaluation definition before spending model calls:

```bash
python3 .agents/evals/eval.py check <skill-name>
```

This checks fixture cleanliness, required files, grader presence,
supporting-skill structure, interaction filenames, and the shared grade-schema
shape. It does not execute `check.py` or assess whether `expected.md` contains
sensible scoring criteria. Successful output therefore says that the test case
definitions are structurally valid—not that their graders are correct. No
model calls are made.

## 3. Run the evaluations

Run commands from the repository root.

### The three commands

| Goal | Command | Calls a model? |
|---|---|---:|
| List available skill evaluations | `python3 .agents/evals/eval.py list` | No |
| List one skill's test cases | `python3 .agents/evals/eval.py list <skill-name>` | No |
| Structurally validate one evaluation | `python3 .agents/evals/eval.py check <skill-name>` | No |
| Run every test case | `python3 .agents/evals/eval.py run <skill-name>` | Yes |
| Run one test case | `python3 .agents/evals/eval.py run <skill-name> <test-case-name>` | Yes |

The simplest real run is:

```bash
python3 .agents/evals/eval.py run <skill-name>
```

It runs every test case once with the target skill. For each completed worker
trial, it runs `check.py` when present and starts three semantic judges in
parallel when `expected.md` is present.

### Optional run flags

| Flag | Default | Meaning |
|---|---:|---|
| `--compare` | Off | Run a no-skill baseline and a with-skill trial for every selected test case |
| `--trials N` | `1` | Repeat each test-case/configuration cell `N` times |
| `--judges N` | `3` | Use `N` parallel semantic judges for each worker trial |
| `--no-semantic-judges` | Off | Run the worker and `check.py`, but make no semantic-judge model calls |

`--no-semantic-judges` rejects a selected test case that has no `check.py`.
This happens before an experiment is created or any model is called. The flag
does not make the entire evaluation model-free: the worker agent must still run
the task. Only `list` and `check` make no model calls.

`N` does not need to be odd; any positive number is accepted. Three is the
default because it permits a majority decision without the cost of a larger
jury.

Examples:

```bash
# Run one test case with the default three semantic judges
python3 .agents/evals/eval.py run <skill-name> <test-case-name>

# Run all test cases without any semantic-grader model calls
python3 .agents/evals/eval.py run <skill-name> --no-semantic-judges

# Run three independent worker trials per test case
python3 .agents/evals/eval.py run <skill-name> --trials 3

# Use five semantic judges per completed worker trial
python3 .agents/evals/eval.py run <skill-name> --judges 5

# Compare the same prompts without and with the target skill
python3 .agents/evals/eval.py run <skill-name> --compare
```

The runner prints the number of planned worker-turn and semantic-judge calls
before starting. While a command is running in an interactive terminal, a
spinner displays its elapsed time. Concurrent semantic judges share one spinner
that shows the number of active operations. One single-turn test case with
semantic grading makes four model calls: one worker plus three judges. A test
case with two follow-up interactions makes six calls: three worker turns plus
three judges. The judges run concurrently, but they are still three separate
model calls.

### How the semantic jury decides

All judges inspect the same completed worker trial independently. They do not
run the task again and cannot see one another's grades.

| Jury output | Rule |
|---|---|
| Pass/fail | Strict majority of the requested judge count: `floor(N / 2) + 1` pass votes |
| Score out of 100 | Rounded median of all valid judge scores |
| Judge failure | Tolerated only while enough valid judges remain to reach the original quorum |
| Explanations | Every individual grade is preserved; the consensus records votes and scores |

For an even jury, ties fail because there is no strict pass majority. This is
why odd counts are normally easier to interpret, although the harness permits
even counts for experiments.

The final trial passes only when all of these are true:

1. the worker exits successfully;
2. protected inputs remain intact;
3. every configured grading system returns a valid grade;
4. the deterministic grader passes, when configured;
5. the semantic jury passes, when configured.

Deterministic and semantic scores remain separate because they measure
different kinds of evidence. The headline score uses the semantic consensus
when available; otherwise it uses the deterministic score.

### What `--compare` does

| Configuration | Target skill available? | Worker prompt |
|---|---:|---|
| `baseline` | No | Exact contents of `prompt.md` |
| `with-skill` | Yes | Exact contents of `prompt.md` |

Use an implicit prompt for a normal baseline comparison. If the prompt
explicitly requests `$<skill-name>`, the baseline instead tests what happens
when a requested skill is unavailable.

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
| `report.md` | Updated when the experiment completes, fails, or is interrupted | Human-readable outcome, mean score, trial table, baseline comparison, and main failure reasons |
| `experiment.json` | Created before the first trial and finalized when the experiment stops | Complete machine-readable manifest, status, aggregate results, and all trial summaries |

Open `experiment.json` only when a script needs the data or when you need more
detail than the report provides. Follow the links in `report.md` into
`trials/` when investigating a particular result.

Each trial directory contains:

| Output | Meaning |
|---|---|
| `result.json` | Final trial result and separate grader scores |
| `run.json` | Trial inputs, configuration, initial Git state, invocation mode, and turn metadata |
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
