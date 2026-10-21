# Running evaluations

How to run skill evaluations and interpret the pass/fail decision. See [authoring](authoring.md) for writing test cases and [outputs](outputs.md) for the generated files.

## Run the evaluations

Run commands from the repository root.

### Commands

| Goal | Command | Calls a model? |
|---|---|---:|
| Set up auto-updating project usage reports + list skills | `python3 .agents/evals/eval.py setup` | No |
| Refresh project usage reports on demand | `python3 .agents/evals/eval.py usage` | No |
| List available skill evaluations | `python3 .agents/evals/eval.py list` | No |
| List one skill's test cases | `python3 .agents/evals/eval.py list <skill-name>` | No |
| Structurally validate one evaluation | `python3 .agents/evals/eval.py check <skill-name>` | No |
| Run every test case | `python3 .agents/evals/eval.py run <skill-name>` | Yes |
| Run one test case | `python3 .agents/evals/eval.py run <skill-name> <test-case-name>` | Yes |
| Compare two existing experiments | `python3 .agents/evals/eval.py compare <skill-name> <old-exp> <new-exp>` | No |

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
the task. Only `list`, `check`, and `compare` make no model calls.

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
| Score out of 100 | Rounded median of all deciding judge scores (`round(median(scores))`) |
| Pass/fail & band | Derived from the consensus score: `score >= 70` passes (`full_pass` `100`, `pass` `70..99`, `partial` `1..69`, `fail` `0`) |
| Abstention (`insufficient_evidence`) | Excluded from the score median; if deciding judges fall below quorum (`floor(N / 2) + 1`), the trial becomes `needs_review` |
| Judge failure | Tolerated only while enough valid judges remain to reach the original quorum (`floor(N / 2) + 1`) |
| Explanations | Every individual grade is preserved; the consensus records deciding scores and abstentions |

The final trial passes only when all of these are true:

1. the worker exits successfully;
2. protected inputs remain intact;
3. every configured grading system returns a valid grade;
4. the deterministic grader passes (`score >= 70`), when configured;
5. the semantic jury passes (`score >= 70`), when configured.

Deterministic and semantic scores are recorded separately (`deterministic_score`
and `semantic_score`) because they measure different kinds of evidence. When
both graders run, the trial's headline `score` is `min(deterministic_score,
semantic_score)` so a failing grader is never masked by a passing one. Both
grader sub-scores are shown alongside `Score` in the `## Trials` table, and any
point deductions (`score < 100`) are surfaced in `## Findings` even on passing
trials.

### What `--compare` does (within one experiment)

| Configuration | Target skill available? | Worker prompt |
|---|---:|---|
| `baseline` | No | Exact contents of `prompt.md` |
| `with-skill` | Yes | Exact contents of `prompt.md` |

Use an implicit prompt for a normal baseline comparison. If the prompt
explicitly requests `$<skill-name>`, the baseline instead tests what happens
when a requested skill is unavailable.

### Comparing two experiments (`eval.py compare`)

To compare two saved experiments (for example before and after editing
`SKILL.md`) without making any new model calls:

```bash
# Compare with-skill cells (default) -> writes comparisons/<old>-vs-<new>.md
python3 .agents/evals/eval.py compare <skill-name> experiment-001 experiment-002

# Compare baseline cells -> writes comparisons/<old>-vs-<new>-baseline.md
python3 .agents/evals/eval.py compare <skill-name> experiment-001 experiment-002 --config baseline
```

The generated Markdown report under `.agents/evals/<skill-name>/comparisons/`
includes a `Setup` and `Summary` header, a one-row-per-case `## Case overview`
table, per-case score/token/cost breakdowns (including `det score` and
`sem score` when available), confound detection when the model or fixture
changed, and a unified diff of the captured `SKILL.md` snapshots.

### Token usage and API-equivalent cost

Every run that produces results includes a `## Token usage` section in
`report.md`, showing the mean tokens per trial for each configuration, a
`**Total**` row summing across all trials in the experiment, and an estimated
API-equivalent cost column:

| Column | Meaning |
|---|---|
| Uncached input | `input − cached` input tokens (billed at the full rate) |
| Cached input | Input tokens served from cache (billed at a discount) |
| Output | Visible output tokens |
| Reasoning | Reasoning tokens — these bill as output |
| Billed output | `output + reasoning`, the figure that drives output cost |
| Est. cost | Estimated API-equivalent USD at public API list prices (not actual subscription or credit spend) |

With `--compare`, an extra **delta** row per test case shows with-skill minus
baseline for each column (including 4-decimal USD and percent deltas on
`Est. cost`); a negative delta means the with-skill run used fewer tokens. When
a worker spawns subagents, the reported token counts and cost automatically roll
up the parent session and all captured subagent rollouts. Read tokens together
with the score: a skill is only more efficient if it achieves an equal-or-better
score for fewer output and reasoning tokens.

Token counts are only comparable across runs on the **same model and reasoning
effort** — different models or reasoning levels produce different counts for the
same task. The report's `**Setup:**` header line records the model and reasoning
effort that actually produced the run (read back from the Codex rollout), so you
can confirm two runs are comparable.

### Zero-token real-work usage tracking (`eval.py setup` and `eval.py usage`)

In addition to synthetic fixture benchmarks (`eval.py run`), the harness can
passively track ordinary Codex work in the current project with **zero model
calls**:

```bash
# One-time setup: configure .codex/config.toml notify hook and backfill _usage/
python3 .agents/evals/eval.py setup

# Manual refresh at any time
python3 .agents/evals/eval.py usage
```

`setup` configures `.codex/config.toml` with:

```toml
notify = ["python3", ".agents/evals/eval.py", "usage", "--quiet"]
```

Whenever a Codex turn finishes in this repository, Codex invokes `eval.py usage
--quiet` in the background. The collector incrementally parses changed rollout
files in `~/.codex/sessions/` (caching `(mtime_ns, size)` in
`.agents/evals/_usage/cache.json`), filters strictly to sessions whose working
directory is inside this project while excluding synthetic `eval.py run` trial
workspaces, links spawned subagents to their parent sessions, and updates:

- `.agents/evals/_usage/report.html` — self-contained interactive dashboard
  (KPI cards, skill trigger breakdown including `0`-trigger installed skills,
  subagent breakdown including `0`-spawn `.codex/agents/*.toml` profiles, model
  mix, daily usage bars, and a searchable/filterable session table);
- `.agents/evals/_usage/report.md` — clean GitHub-flavored Markdown summary; and
- `.agents/evals/_usage/summary.json` — machine-readable aggregate data.



