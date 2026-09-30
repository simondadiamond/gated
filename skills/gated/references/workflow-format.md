# Workflow format

A workflow is a folder. `gated` looks for it in the project's
`.claude/workflows/<name>/`, then `~/.claude/workflows/<name>/`, then the
examples shipped with the skill. The first match wins.

```
weekly-report/
  workflow.json      checkpoints and gates
  steps/*.md         instructions, one file per step
  checks/*           scripts that gates call (optional)
  rubrics/*.md       rubrics for judge gates (optional)
  learnings.md       feedback from past runs; `gated learn` appends to it
```

Run `gated lint <name>` after every change.

## workflow.json

```json
{
  "name": "weekly-report",
  "description": "One line. `gated list` shows it.",
  "inputs": {
    "repo": { "required": true, "description": "owner/name" },
    "days": { "default": "7" }
  },
  "attempts": 5,
  "commit": false,
  "judge": "claude",
  "plan": { "step": "steps/plan.md" },
  "every": [ { "id": "suite", "type": "command", "run": "npm test" } ],
  "checkpoints": [
    { "id": "gather", "title": "Gather merged PRs", "step": "steps/gather.md", "gates": [ ... ] }
  ]
}
```

| Field | Meaning |
| --- | --- |
| `name` | Lowercase letters, digits and dashes. Matches the folder name by convention. |
| `description` | Required. |
| `inputs` | Values passed as `key=value` when the run starts. `required` or `default`. |
| `attempts` | How many failed stop attempts a gate gets before the run is `blocked`. Default 5. A gate can set its own. |
| `commit` | `true` commits the project once per passed checkpoint. For code workflows. |
| `freshContext` | `false` turns off the fresh-subagent rule. Default `true`. |
| `skills` | Skills every checkpoint's subagent must load. A checkpoint can list its own `skills` too. |
| `storySkill` | The skill a step uses to create a new story when it splits work off. Default: `gh issue create`. |
| `basedOn` | Set by `gated customize`: which workflow this copy came from. |
| `judge` | `claude` or `codex`: which CLI grades `judge` gates. Defaults to the harness running the workflow. |
| `before` | Fixed checkpoints that run ahead of the `plan`, like writing acceptance criteria. Needs a `plan`. |
| `plan` | A planning step. Its subagent writes this run's checkpoints, and the person approves them. It can list `skills` for the planner. |
| `every` | Gates added to every checkpoint, planned or fixed. A test suite usually goes here. |
| `checkpoints` | Fixed checkpoints. With a `plan`, they run after the planned ones. |

A workflow needs `checkpoints`, a `plan`, or both. Every checkpoint gets two
gates automatically:

- `todos`: the to-do list is written and finished.
- `fresh-context`: a subagent that hasn't worked on any other phase did the
  work, and the orchestrator edited nothing itself. The hooks record every
  tool call with the caller's `agent_id` in `activity.jsonl`, in both Claude
  Code and Codex. `gated submit-plan` applies the same rule to the planner.
  Set `"freshContext": false` only for a harness without subagents.

## Templates

These work in step files, gate commands, paths and rubrics:

| Template | Value |
| --- | --- |
| `{{run}}` | this run's folder, `.gated/runs/<id>` |
| `{{project}}` | the project root |
| `{{workflow}}` | the workflow folder, for bundled scripts |
| `{{checkpoint}}` | the current checkpoint id |
| `{{input.<name>}}` | an input value |

An unknown template fails the gate that uses it. It never quietly becomes an
empty string.

## A planned run's checkpoints

A planner writes `{{run}}/checkpoints.json`:

```json
{
  "checkpoints": [
    {
      "id": "leave-model",
      "title": "Leave request model",
      "instructions": "Add the LeaveRequest model with start, end and status...",
      "gates": [
        { "id": "tests", "type": "red-first", "run": "npm test -- leave", "lock": ["src/**/leave*.test.ts"] }
      ]
    }
  ]
}
```

Planned checkpoints hold `instructions` text instead of a `step` file. `gated
submit-plan` validates them, snapshots the file as `plan-<n>.json`, locks the
snapshot and waits for approval. `gated amend <file>` adds checkpoints in the
same shape to a running or finished run, and also needs approval.

## Gates

Every gate has an `id` (unique in its checkpoint) and a `type`. Optional:
`timeout` in seconds and `attempts`.

| Type | Required fields | Optional fields |
| --- | --- | --- |
| `command` | `run` | `timeout` (default 600) |
| `file` | `path` | `json`, `nonEmpty`, `headings` (list), `contains` (list of regexes), `links: "resolve"` |
| `red-first` | `run`, `lock` (list of globs) | `timeout` |
| `judge` | `rubric`: a file in the workflow folder | `inputs`: list of `{"run": "cmd"}` or `{"file": "path"}` |
| `human` | `ask` | |

Commands run with `/bin/sh` in the project root. They see `GATED_RUN`,
`GATED_RUN_ID`, `GATED_CHECKPOINT` and `GATED_PROJECT`.

## Run files

```
.gated/runs/<workflow>-<n>/
  state.json                 written only by bin/gated
  checkpoints.json           the planner's working file
  plan-<n>.json              submitted plans, locked
  <checkpoint>/todo.md       the to-do list
  <checkpoint>/noticed.md   things the step noticed but didn't fix, one `- ` line each
  <checkpoint>/splits.md     stories the step created for work it split off, one `- <issue URL> why` line each
  activity.jsonl             every tool call during the run, with the caller's agent id
  <checkpoint>/gates/*.log   each gate's last output
  report.md                  written when the run ends
```

Starting a run writes `.gated/.gitignore`, so run folders never show up in git.

## Statuses

| Status | Meaning | Stop hook |
| --- | --- | --- |
| `planning` | a planner is writing the checkpoints | blocks until a plan is submitted |
| `awaiting-approval` | a plan or amendment waits for the person | lets the turn end |
| `running` | a checkpoint is in progress | blocks while gates fail |
| `waiting` | only a `human` gate is left, or the agent asked the person something with `gated ask` | lets the turn end without spending an attempt |
| `blocked` | a gate used all its attempts, or `state.json` changed outside gated | lets the turn end; the person types `approve` to grant fresh attempts after `gated resume` |
| `done` | every checkpoint passed | lets the turn end |
| `cancelled` | the person typed `cancel run` | lets the turn end |

A checkpoint with `skills` gets a `skills` gate: in Claude Code, a subagent
must have loaded each one with the Skill tool during that checkpoint.

Splits are checked. The Stop hook runs `gh issue view` on every URL in a
checkpoint's `splits.md`, and a URL that doesn't resolve fails the
checkpoint, so scope can't be dropped by claiming a story that doesn't exist.
The report lists verified splits under "Split into new stories".

Findings aren't gates. The Stop hook records each `- ` line in a checkpoint's
`noticed.md` once, the report lists them under "Found, not fixed", and
`gated findings --since 30d` gathers them across runs.

`gated check`, when the agent runs it, only reports. The Stop hook is the only
thing that advances a run. Everything in the workflow folder except
`learnings.md` is locked while a run uses it, check scripts and rubrics
included. A gate id in `every` can't be reused by a checkpoint.
