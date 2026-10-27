# Authoring test cases

Guidance for writing evaluations for a Codex skill. See the [README](../README.md) for installation and the convention that links a skill to its tests.

## Add a test case

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
| It does not contain `.agents/` or `.codex/` | The harness controls injected skills and declared agent profiles; private evaluator files remain outside the workspace |

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

Essential pass conditions (cap `score` below 70, or at 0 for a disqualifier, if violated):
- The defect is fixed and the regression test passes.
- No prohibited files outside the target module are modified (0 if violated).
```

Make the criteria total 100 points and state any essential pass conditions or
hard gates explicitly. Both semantic and deterministic graders use `score`
(`0..100`) as the single source of truth (`score >= 70` passes). If an
essential condition fails, the grader should cap `score` below `70` (or at `0`
for a complete disqualifier) even if other criteria earned points. The worker
cannot see this file.

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

The output must match [`_lib/grade.schema.json`](../_lib/grade.schema.json).
`score` (`0..100`) is authoritative: the harness derives `overall_pass`
(`score >= 70`) and the display band (`pass` for `70..100`, `partial` for
`1..69`, `fail` for `0`) directly from `score`. `checks[].points` record the
points earned per check and do not need to sum to `score`.

To enforce a hard gate ("instafail") in `check.py` — for example, failing
immediately if the worker modifies prohibited files or breaks compilation:
- either weight that check `> 30` points so failing it drops a 100-point rubric
  below `70`, or
- cap `score` at `0` (or `min(score, 69)`) when the gate check fails while
  keeping per-check `points` on the remaining checks for diagnostic visibility.

Keep `_lib/` limited to generic harness infrastructure. Project-specific facts,
such as source-directory names, frameworks, endpoints, user roles, or required
behaviour, belong directly in the relevant test case's `check.py`. For this
small harness, keeping each deterministic grader self-contained is clearer than
introducing another shared-helper directory.

### Check behaviour, not structure

Write checks that assert what the change *does*, not how it is *spelled*. A good
check accepts any correct implementation and rejects any incorrect one. A
brittle check asserts an incidental detail — an exact string, a specific symbol
name, a particular file layout — so it false-fails a valid alternative
implementation and false-passes a shallow one that merely name-drops the right
words.

This is the single highest-leverage habit for trustworthy grading. In the
DeepSWE study, when an independent judge re-reviewed graded runs, it disagreed
with *functional* verifiers (which "check the requested functionality and accept
any implementation that provides it") only 1.4% of the time, versus 32.4% for
*inherited, structural* tests — about an order of magnitude fewer wrong verdicts.
(*Measuring Frontier Coding Agents on Original, Long-Horizon Engineering Tasks*,
arXiv:2607.07946.)

| Prefer (behavioural) | Avoid (structural) |
|---|---|
| A build/test command actually ran and succeeded in the trace | A test *file* mentions a certain word |
| An endpoint returns the expected result | The source contains an exact identifier string |
| Only in-scope files changed (via Git diff) | A function has one specific name |
| A required report/artifact exists and has the required content | A signature matches an exact, formatting-sensitive string |

Good vs brittle, concretely:

```python
# Brittle: false-fails if the author uses a constant, a client wrapper,
# or simply different names; false-passes a test that only name-drops the word.
passed = "operation-contract" in source_text

# Behavioural: asserts the observable outcome, accepts any implementation.
passed = ran_and_succeeded(trace, "run-tests.sh") and not out_of_scope_files_changed(diff)
```

Two rules that follow from this:

- Keep each verifier scoped to the *requested* functionality. If you also want
  to reward regression coverage or extra robustness, make that a *separate,
  explicit* check with its own points — do not fold it into the correctness
  check.
- Reserve exact-string and signature matching for cases where the string truly
  *is* the contract (for example, a public API name the task explicitly fixes).
  Even then, normalise formatting so a pure reformat does not false-fail.

Qualities that genuinely need judgement (architecture, meaningful test quality,
honest reporting) belong in `expected.md` for the semantic judges, not in a
brittle deterministic string match.

### Evaluating orchestrator skills that spawn subagents

Some skills *delegate* subtasks to spawned child agents instead of doing all the
work themselves. The harness captures the main worker's persisted rollout into
`worker/trace.jsonl` and spawned child rollouts into
`worker/subagents/<thread-id>.jsonl` read-only from `~/.codex/sessions/`
(stripping only opaque `encrypted_content` ciphertext and recursing when a child
spawns its own subagents), normalises collaboration events, and rolls up parent
+ subagent token usage in `worker/metrics.json` and `result.json`.

The key failure mode to catch is an orchestrator that *claims* to delegate but
did the work itself (or rubber-stamped a result). Grade delegation behaviourally:

| Check | Evidence in `worker/metrics.json` / `worker/subagents/manifest.json` | Raw evidence in `worker/trace.jsonl` |
|---|---|---|
| At least N subagents actually ran | `subagent_spawn_count >= N` | Events with `tool == "spawn_agent"` |
| Distinct children (not one spawned N times) | `len(subagent_thread_ids) >= N` | De-duplicated `receiver_thread_ids` |
| The orchestrator waited for results | `subagent_wait_count >= 1` (or `subagents[i]["waited"] == True`) | Matching `tool` in `{"wait", "wait_agent"}` |
| Child lineage, model, and token breakdown | `subagents[i]` (`depth`, `parent_thread_id`, `model`, `token_usage`) | `worker/subagents/<thread-id>.jsonl` |
| Each subagent's work is correct | Grade the consolidated *artifact* the orchestrator produced (and use `worker/subagents/` when checking handoff fidelity) | — |

If a `check.py` script inspects `worker/trace.jsonl` directly rather than
reading `worker/metrics.json`, tolerate both Codex event casings: the live
`--json` stream emits `collab_tool_call`, while persisted rollouts emit
`CollabAgentToolCall`.

Grade subagent *outcomes* primarily through the workspace artifact the
orchestrator writes, paired with `worker/metrics.json` (or
`worker/subagents/manifest.json`) to prove delegation and wait handoffs actually
occurred. Semantic judges are also pointed at `worker/metrics.json` and
`worker/subagents/` when present so they can verify specialist prompts and
handoff evidence without depending on fragile internal rollout formatting.

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

### `agent-profiles.json`: optional project agent dependencies

If a test case needs custom subagent profiles from `.codex/agents/<name>.toml`,
declare them explicitly in `agent-profiles.json`:

```json
["explorer", "spec_conformance_auditor"]
```

`eval.py check` verifies that every listed profile exists under `.codex/agents/`.
At runtime the harness copies those profiles into `workspace/.codex/agents/`,
snapshots them in `agent-profile-snapshot/`, passes equivalent `-c agents.<name>.*`
overrides to `codex exec`, and treats them as protected inputs alongside injected
skills.

Both configurations receive the same declared profiles; baseline excludes the
target skill. The harness does not copy global agent configuration. Verify
actual child role metadata and behavior rather than treating a role mentioned
in a spawn prompt as proof that the role ran. Role instructions do not create
an independent sandbox, and an unavailable role should be reported as a blocker
rather than silently replaced with a different agent.

Because the setup commit includes `TASK.md`, injected skills, and any declared
agent profiles, `HEAD` starts one commit ahead of `fixture-target`. In test
cases that inspect a baseline Git revision, use the `fixture-target` tag rather
than the later setup commit.

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

Turn 1 always creates a persisted session so provenance and the full rollout can
be read back from `~/.codex/sessions/`; each interaction in a multi-turn case
then uses `codex exec resume` with the thread ID from turn 1. This is why each
interaction adds one worker model call. The installed Codex CLI must support
`codex exec resume`.

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
