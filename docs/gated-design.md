# gated: design

`gated` runs a workflow as a series of checkpoints. A checkpoint is done when
its gates pass, and code checks the gates, not the agent doing the work. The
same engine runs a feature build, a weekly report, or an onboarding checklist.
A workflow is a folder, and `/gated new` helps you write one.

Status: built and rehearsed live in Claude Code and Codex, 2026-09-24.

## Why

Agents skip steps. An instruction to "run the tests before you finish" works
until the day it doesn't, and nothing notices. Shopify's Helix showed that small
checkpoints with blocking gates keep a long agent job at a real standard. The
public reconstructions of it let the model mark its own gates as passed. In
`gated`, the model never writes a pass.

## How you use it

```
/gated <workflow> [inputs]   start a run
/gated new <workflow>        write a new workflow with you
/gated status                list runs in this project
/gated resume [run]          pick a run back up in a new session
```

A scheduled task is one line: `claude -p "/gated weekly-report"`, or
`codex exec "use the gated skill to run weekly-report"`.

Parallel runs are separate sessions. Each run belongs to the session that
started it, so two windows in one project never block each other. Code
workflows should run in their own worktree (`claude -w`) so two runs don't edit
the same checkout.

## Moving parts

```
hooks/hooks.json              the plugin's hooks, at the repository root
skills/gated/
  SKILL.md                    the orchestrator
  bin/gated                   the runner (python3, standard library only)
  lib/gated_lib/              runner, gates, hooks, installer
  references/workflow-format.md
  references/writing-gates.md
  workflows/hello/            example: a one-minute tour
  workflows/implement-story/  example: ticket to reviewed PR (planned)
  workflows/weekly-report/    example: scheduled, non-code (fixed)
  tests/
```

The runner needs only the `python3` that ships with macOS (3.9) and most
Linux distributions. It owns all run state. The model calls it; it never edits
state itself.

## Workflows

The skill looks for a workflow in three places, and the first match wins:

1. `<project>/.claude/workflows/<name>/`
2. `~/.claude/workflows/<name>/`
3. the examples shipped with the skill

A workflow folder holds `workflow.json`, one markdown file per step, and
`learnings.md`.

```json
{
  "name": "weekly-report",
  "description": "Summarize the week's merged PRs into a report",
  "inputs": { "repo": { "required": true } },
  "attempts": 5,
  "checkpoints": [
    {
      "id": "gather",
      "step": "steps/gather.md",
      "gates": [
        { "id": "data", "type": "file", "path": "{{run}}/prs.json", "json": true }
      ]
    },
    {
      "id": "write",
      "step": "steps/write.md",
      "gates": [
        { "id": "report", "type": "file", "path": "{{run}}/weekly.md",
          "headings": ["Summary", "Merged", "Risks"], "links": "resolve" }
      ]
    }
  ]
}
```

Checkpoints come in two kinds:

- **Fixed.** Listed in `workflow.json`. The run needs no approval, so it can
  run on a schedule.
- **Planned.** The workflow has a `plan` step instead of a checkpoint list. A
  planning subagent writes `checkpoints.json` for this run, including each
  checkpoint's gates. You approve it once, and then it is locked.

Templates available in gates and steps: `{{run}}` (the run folder),
`{{project}}`, and `{{input.<name>}}`.

## Gates

| Type | Passes when |
| --- | --- |
| `command` | the command exits 0 within its timeout |
| `file` | the file exists, and optionally parses as JSON, contains the listed headings, and every link in it returns HTTP 2xx or 3xx |
| `red-first` | the command failed when `gated red` locked the tests, and passes now |
| `todos` | the checkpoint's to-do list has at least one item, and every item is checked or struck through with a reason |
| `judge` | a separate `claude -p` process, given only the rubric and the named inputs, returns `PASS` |
| `human` | you typed `approve` as the whole message |
| `fresh-context` | added to every checkpoint: a new subagent did the work and the orchestrator edited nothing |

The runner adds a `todos` gate to every checkpoint. Each checkpoint starts
with the agent writing `{{run}}/<checkpoint>/todo.md`, and it can't finish
with an unchecked item.

`red-first` is how a code workflow keeps tests honest. Once the tests are
written, the agent runs `gated red <gate>`. The runner runs the command and
requires a real failure: a pass means the tests test nothing new, and exit 126
or 127 means the command never ran. Then it hashes the files matched by the
gate's `lock` globs, so an edited test fails the gate. Put test configuration
(`package.json`, `pytest.ini`) in `lock` too if a changed script could skip
tests.

A `judge` verdict is cached against a hash of its inputs, so an unchanged diff
isn't judged twice.

A `human` gate reads your messages through a `UserPromptSubmit` hook, which
sees what you typed, not what the agent says you typed. In a scheduled run a
human gate can't pass, so the run stops as `waiting` and reports why.

## Enforcement

Four hooks enforce the run. They're registered at the settings level, not in
the skill's frontmatter. A live probe on 2026-09-24 showed that hooks declared
in a skill fire only for the main agent's own tool calls, never for its
subagents. Subagents do the editing, so those hooks would miss exactly the
calls that matter. Settings-level hooks fire for subagent calls, carrying the
parent's `session_id` and the subagent's `agent_id`.

Where they're registered:

- **Claude Code, plugin install:** `hooks/hooks.json` at the plugin root.
- **Claude Code, copied skill:** `gated install claude` writes the same hooks
  into `~/.claude/settings.json`. It refuses when the plugin is installed,
  because then every check would run twice.
- **Codex:** `gated install codex` writes them into `~/.codex/hooks.json`.

They run in every session. Each one exits 0 at once unless the calling session
owns a run. That same check keeps two windows in one project from blocking
each other.

**Stop.** It reruns the current checkpoint's gates through `bin/gated check`.

- The gates pass and more checkpoints remain: the hook marks the checkpoint
  done and blocks the stop with "checkpoint `gather` passed, start `write`",
  so the loop drives itself.
- The gates pass on the last checkpoint: the run is `done`. The hook writes the
  report and holds the stop once, telling the agent to summarize the report
  for the person. The next stop goes through.
- A gate fails: the hook counts an attempt and blocks the stop with the end of
  the gate's output. When a gate reaches its attempt budget (5 by default,
  `attempts` in `workflow.json` overrides it), the run becomes `blocked`, the
  report names the gate and its last output, and the stop is held once so the
  agent reports it. A gate is never skipped, and only the person can grant
  fresh attempts.
- The run is waiting on you (plan approval or a `human` gate): the hook allows
  the stop.

Claude Code and Codex both set a `stop_hook_active` flag once a Stop hook has
blocked. The hook ignores it and counts attempts itself, so the flag never
ends a run early.

**PreToolUse.** It denies Edit, Write and `apply_patch` on locked files: run
state, gate definitions, submitted plans and locked tests. It also denies shell
commands that name a locked file and look like writes. That check is a
heuristic, so every gate check also compares each locked file against its hash
from lock time. A changed file fails the gate.

**PostToolUse.** It claims a new run for the calling session. `gated start`
prints a `gated-claim:` token, and the hook records its own `session_id` as the
owner. The token matters because shell commands can't reliably name their
session: a process started by one harness inside another inherits both sets of
variables.

**UserPromptSubmit.** It records approvals for plan and `human` gates from the
text the person typed.

The two harnesses send the same fields. One hook script serves both. It reads
the project from `CLAUDE_PROJECT_DIR` or the payload's `cwd`, and maps
`apply_patch` to the same lock check as Edit and Write. A `judge` gate calls
`claude -p` or `codex exec`, whichever harness is running the workflow, and a
workflow can pin one.

In a harness with no hooks at all, the skill tells the agent to run
`gated check` before ending a turn. The gates are the same; only the backstop
is missing.

## Who can move a run

Only hooks change a run's status, and they run in the harness's environment,
not the agent's shell.

- **The agent's `gated check` is advisory.** It shows which gates fail, but it
  never advances a run, spends an attempt or caches a judge verdict. A
  variable or a fake `PATH` in the agent's shell can't pass anything.
- **The Stop hook** reruns the gates, advances, counts attempts and blocks.
- **Only the person** can approve a plan or a human gate, grant fresh
  attempts, reject an amendment or cancel a run. They type the whole message:
  `approve` (also `lgtm`, `yes`, `ship it`), `reject ...` or `cancel run`.
  "Go ahead and change step 2" is feedback, not approval.

The hooks find the run through `~/.gated/sessions/`, an index written when the
PostToolUse hook claims a run. That's one file read per hook. It works from
any directory, and stray files in a project can't confuse it. Every state
change takes a lock on the run folder, so a Stop hook and a subagent's command
can't lose each other's updates.

## What this protects against, and what it doesn't

`gated` stops the shortcuts agents actually take:

- declaring victory early,
- skipping tests,
- weakening a test or a gate to get green,
- claiming a pass nobody checked.

Locked files are hashed. `state.json` has a digest in `~/.gated/digests/`, and
any change made outside `gated` blocks the run until the person looks.

It doesn't stop an agent that sets out to defeat it with your own shell: one
that recomputes the digest, or writes a fake test runner that tests pass by
default. No local check can, because the agent has the same permissions you
do. What `gated` guarantees is that those moves are deliberate and visible in
the logs, never an accident.

## The orchestrator

The session that runs `/gated` coordinates and doesn't do the work. Its prompt
casts it as the check on its subagents: it reads what they return as if it
came from someone cutting corners.

A fresh subagent per phase is enforced, not requested. Settings-level hooks
see every tool call, and in both Claude Code and Codex a subagent's call
carries its own `agent_id` next to the parent's `session_id` (probed live on
2026-09-25). The PreToolUse hook appends each call to the run's
`activity.jsonl`, tagged with the phase and the caller. The agent can't write
that file. From it:

- `gated submit-plan` refuses a plan unless a planning subagent worked on it
  and the orchestrator edited nothing.
- Every checkpoint gets a `fresh-context` gate. It passes only if a subagent
  worked on the checkpoint, at least one of them never worked on another phase
  (the planner can't build, and the builder of one checkpoint can't build the
  next), and the orchestrator made no edits, including write-looking shell
  commands.

Sending fixes back to the same subagent inside one checkpoint is allowed; that
subagent keeps its context for the correction. For each
checkpoint it sends a subagent a fresh context holding:

- the step file,
- the inputs,
- `learnings.md`,
- the to-do path,
- the gates it must pass.

When a gate fails, the orchestrator sends the gate's output back to the same
subagent, which keeps its context for the fix.

Before planning, a code workflow checks that the project builds and its tests
run. A run doesn't start on a broken base.

Code workflows can set `"commit": true` to commit once per passed checkpoint.

## Runs

Each run lives in `.gated/runs/<workflow>-<n>/`. Starting a run writes
`.gated/.gitignore`, so nothing leaks into the repository. It doesn't use
`.git/info/exclude`, because Codex's sandbox makes `.git/` read-only.

- `state.json`: owner session, status, current checkpoint, attempts, lock hashes
- `<checkpoint>/todo.md`
- `<checkpoint>/gates/<gate>.log`: each gate's output, exit code and time
- `report.md`: written when the run ends as `done`, `blocked` or `cancelled`, and by `gated report` at any time

Run statuses are `planning`, `awaiting-approval`, `running`, `waiting`,
`blocked`, `done` and `cancelled`.

## Feedback

When a run ends, you test the result. Each change you ask for becomes a new
checkpoint with its own gates, and the feedback goes into the workflow's
`learnings.md`, which every later subagent reads.

## Findings

A step that notices something it won't fix writes it to
`{{run}}/<checkpoint>/noticed.md` as a `- ` line. Findings aren't gates and
never change a run. The Stop hook records each line once, the report lists
them under "Found, not fixed", and `gated findings --since 30d` gathers them
across every run in the project. What keeps coming up becomes a `gated learn`
line or a change to the workflow. That's how a workflow improves over time.

## Splitting off a story

When a step finds part of the work belongs in its own story, it creates that
story itself, while it has the context. A note for another agent to turn into
a story later loses most of that context. The step uses `gh issue create`, or
the workflow's `storySkill`. It gives the story the problem, acceptance
criteria, what this run already did and a link back, then lists the URL in
`splits.md`. The Stop hook checks every URL with `gh issue view`, and one that
doesn't resolve fails the checkpoint. Otherwise an agent could shrink a story
by claiming the rest lives somewhere it doesn't.

## Customizing

The built-in workflows are meant to do good work for anyone: acceptance
criteria before the plan, tests before code, an independent review. Every team
also has its own way of doing each step, often already written down as skills.
`gated customize <workflow>` copies a workflow into the repository (or
`~/.claude/workflows`), records where it came from in `basedOn`, and the
orchestrator interviews the team to wire their skills in. A `skills` list on
the workflow, the plan or a checkpoint goes into the step's brief. In Claude
Code a `skills` gate checks, from the activity log, that a subagent loaded
each one. Codex loads skills by reading files, so there the brief is all
there is, and the report says so.

## When acceptance criteria are written

`implement-story` writes acceptance criteria before the plan, in a `before`
checkpoint with its own subagent. Acceptance test-driven development and BDD
put criteria in backlog refinement, before development starts. Spec-driven
tools order it the same way: GitHub's Spec Kit goes specify, plan, tasks, and
Kiro writes requirements with acceptance criteria before design and tasks.
The criteria say what done means; the plan says how.

Each criterion is `AC-n: Given ..., when ..., then ...`. The planner maps
every criterion to a checkpoint, and each checkpoint writes failing tests
that name the criteria they prove before any code. The review step runs
`criteria-covered.py`, which fails unless every criterion id appears in a test
locked by `gated red`. A locked test failed before the code existed and
passed after it, unchanged. The person approves the criteria and the plan
together, in one stop.

## Writing workflows: `/gated new`

`/gated new onboarding` interviews you about the process: the steps, what
"done" looks like for each one, and what can be checked without judgment. It
proposes checkpoints and gates, and prefers them in this order: `command`,
`file`, `red-first`, then `judge`, then `human`. You approve the folder, and
`bin/gated lint` checks it against the format.

## Not in v1

- an HTML plan viewer
- a UI or prototype comparison gate
- several runs sharing one session

## Testing

- Unit tests for `bin/gated` with Python's `unittest`: each gate type,
  templating, attempt budgets, lock hashes, state transitions, and each hook's
  stdin to exit code. GitHub Actions runs them on macOS and Linux.
- Live rehearsals with `claude -p` in a scratch directory:
  1. A demo workflow whose gate passes once a file exists. The run should end
     `done`.
  2. A gate that can never pass. The hook should block five times and then end
     the run as `blocked`. This proves Claude Code doesn't cut the loop short.
  3. A second session in the same directory. It shouldn't be blocked by the
     first session's run.
  4. Rehearsals 1 and 2 again under `codex exec`, with the hooks installed.
- A hands-on run of the two examples by Simon before the repository goes
  public.
