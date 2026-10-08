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

Then verify it from the same project root:

```bash
python3 .agents/evals/eval.py list
```

`list` makes no model calls. If it runs, the harness is installed correctly.

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


## Documentation

The authoring, running, and output reference material lives under `docs/` to
keep this README focused on getting started:

- [Authoring test cases](docs/authoring.md) — fixtures, prompts, graders
  (`check.py` and `expected.md`), supporting skills, and multi-turn
  interactions.
- [Running evaluations](docs/running.md) — the `list`, `check`, and `run`
  commands, run flags, cost, and how the semantic jury decides pass/fail.
- [Evaluation outputs](docs/outputs.md) — reference for every file written
  under `runs/`.
