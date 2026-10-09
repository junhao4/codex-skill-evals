# Running evaluations

How to run skill evaluations and interpret the pass/fail decision. See [authoring](authoring.md) for writing test cases and [outputs](outputs.md) for the generated files.

## Run the evaluations

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
| `--parallel` | Off | Run every worker first, then batch all semantic judges together in parallel |

`--parallel` splits a run into two phases: all workers (and their deterministic
graders) run first in the normal sequence, then the semantic graders for every
completed trial are launched together in one pool instead of after each worker.
Workers still run one at a time; only the judge phase is parallelised across
trials. This shortens wall-clock when judges are a large share of the run
(for example many trials with `--judges 1`) without changing any trial's
result. It has no effect with `--no-semantic-judges`. The batched phase runs at
most 8 trials at once to avoid rate limits; each still uses `--judges` judges,
so peak concurrency is `8 × judges`.

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

### Token usage

Every run that produces results includes a token usage section in the report,
alongside the score — it is a measurement, not a comparison-only feature. It
shows the mean tokens per trial for each configuration, broken into four
categories:

| Column | Meaning |
|---|---|
| Uncached input | `input − cached` input tokens (billed at the full rate) |
| Cached input | Input tokens served from cache (billed at a discount) |
| Output | Visible output tokens |
| Reasoning | Reasoning tokens — these bill as output |
| Billed output | `output + reasoning`, the figure that drives output cost |

With `--compare`, an extra **delta** row per test case shows with-skill minus
baseline for each column; a negative delta means the with-skill run used fewer
tokens. Read tokens together with the score: a skill is only more efficient if
it achieves an equal-or-better score for fewer output and reasoning tokens.

Token counts are only comparable across runs on the **same model and reasoning
effort** — different models or reasoning levels produce different counts for the
same task. The harness currently uses whatever model and reasoning effort your
Codex configuration provides; it does not yet pin or record them in the report,
so ensure both are held constant when comparing runs.

