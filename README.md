# Codex Skill Evals

A reproducible harness for evaluating project-local Codex skills against
repeatable software-engineering tasks. You install it into an existing Codex
project at `.agents/evals/`, and it evaluates the skills in the sibling
`.agents/skills/` directory.

> **Safety and cost:** Trials run `codex exec` with write access to disposable
> workspaces, so only evaluate trusted skills and avoid exposing secrets. Runs
> also use Codex model quota. Keep the first run cheap with `check` and
> `--no-semantic-judges` (see [Running evaluations](docs/running.md)).

## Prerequisites

The project you install into needs:

- Python 3;
- Node.js 20+ and `npx` (used only by the install command);
- Git (used to create isolated trial workspaces); and
- the Codex CLI, installed and signed in.

The Codex CLI must be authorised separately — installing this harness does not
install or enable Codex, and no model-based evaluation can run until `codex
exec` works in your environment.

## Install

From the root of the project you want to evaluate:

```bash
mkdir -p .agents
npx degit junhao4/codex-skill-evals .agents/evals
```

Then run `setup` from the same project root to enable automatic per-project
Codex session usage tracking and verify the installation:

```bash
python3 .agents/evals/eval.py setup
```

`setup` makes no model calls. It:

1. adds `notify = ["python3", ".agents/evals/eval.py", "usage", "--quiet"]` to
   `.codex/config.toml` (creating it or preserving existing settings);
2. backfills past Codex sessions for this project into `.agents/evals/_usage/`
   (`report.html`, `report.md`, and `summary.json`), excluding synthetic
   `eval.py run` trial workspaces and other repositories; and
3. lists any discovered skill evaluations.

If you only want to check discovered evaluations without setting up the
per-project usage report, run `python3 .agents/evals/eval.py list`.

**Install notes**

- Run the command from the target project root so the files land in that
  project's `.agents/evals/`, not a home-level `.agents/`.
- `degit` copies the files without creating a nested Git repository.
- Pin a release for a stable version:
  `npx degit junhao4/codex-skill-evals#v0.1.0 .agents/evals`.
- Only re-run `degit` over an existing `.agents/evals/` if you intend to
  replace it.
- The harness ships no skills or test cases — you add those yourself (below).

## Configure and run skill evaluations

This harness evaluates project-local Codex skills against repeatable test cases.
It is convention-based: matching folder names connect a skill to its tests, so
there is no central suite file or registry to maintain.

To evaluate a skill:

1. create an evaluation folder with the same name as the skill;
2. add one or more test cases;
3. use `eval.py` to list, check, run, or compare them.

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

## Add a skill to evaluate

Create the skill and a matching evaluation directory:

```text
.agents/
├── skills/
│   └── <skill-name>/
│       └── SKILL.md
└── evals/
    ├── eval.py             # command-line entry point
    ├── _lib/               # internal harness implementation
    ├── _usage/             # auto-updated project session usage reports
    └── <skill-name>/
        ├── test-cases/
        ├── runs/              # generated automatically after eval is run
        └── comparisons/       # generated automatically by eval.py compare
```

| Path | Required? | Purpose |
|---|---:|---|
| `.agents/skills/<skill-name>/SKILL.md` | Yes | The skill being evaluated—the system under test |
| `.agents/evals/<skill-name>/test-cases/` | Yes | Holds the scenarios used to evaluate that skill |
| `.agents/evals/<skill-name>/runs/` | Generated | Numbered experiments, human-readable reports, workspaces, traces, and grades |
| `.agents/evals/<skill-name>/comparisons/` | Generated | Cross-experiment comparison reports (`<old>-vs-<new>.md`) |

The two `<skill-name>` directory names must match exactly. A skill also needs at
least one valid test case before it can be run. You do not manually populate
`runs/`; the harness creates an experiment there each time `eval.py run` starts.

Check whether the harness discovers it:

```bash
python3 .agents/evals/eval.py list
```

No model is called by `list`.


## Documentation

The authoring, running, and output reference material lives under `docs/` to
keep this README focused on getting started:

- [Authoring test cases](docs/authoring.md) — fixtures, prompts, graders
  (`check.py` and `expected.md`), subagent/orchestrator skills, supporting
  skills, and multi-turn interactions.
- [Running evaluations](docs/running.md) — the `setup`, `usage`, `list`,
  `check`, `run`, and `compare` commands, run flags, cost, and how the semantic
  jury decides pass/fail.
- [Evaluation outputs](docs/outputs.md) — reference for every file written
  under `runs/`, `comparisons/`, and `_usage/`.
- [Testing the harness](docs/testing.md) — development test environment,
  regression coverage, and the limits of simulated worker/judge tests.
