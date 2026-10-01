---
name: gated
description: Run a workflow as a series of checkpoints, each blocked by gates that code checks instead of the agent. Use when the user types /gated, asks to run a named workflow (implement-story, weekly-report, onboarding or any of their own), wants a long job done "properly" with enforced tests or reviews, wants to create a new repeatable workflow, or asks for the status of a gated run. Also the entry point for scheduled tasks that run a workflow by name.
argument-hint: "[workflow] [key=value ...] | new <name> | customize <workflow> | status | findings | resume [run]"
allowed-tools: Bash(python3 *bin/gated*)
---

# gated

You run a workflow one checkpoint at a time. Your job is to make sure your
subagents don't cheat and do their job properly: read what they hand back as
if it came from someone cutting corners, and send it back when it doesn't hold
up. Never do a checkpoint's work yourself; that's what keeps each one in a
fresh context.

This is enforced. The hooks log every tool call with the id of the agent that
made it. A plan is refused unless a planning subagent wrote it. Every
checkpoint has a `fresh-context` gate that fails unless a subagent that hasn't
worked on any other phase did the work, and fails if you edited files
yourself. Use a new subagent for each phase. Within one checkpoint, more
subagents for fixes are fine.

A checkpoint is finished when its gates pass, and `bin/gated` checks them,
not you. You can be wrong as many times as the attempt budget allows. You
can't move on until the gates agree.

Arguments: `$ARGUMENTS`

All commands below are `gated <command>`. That means:

```bash
python3 "${CLAUDE_SKILL_DIR}/bin/gated" <command>
```

In Codex, use the `bin/gated` next to this file.

The hooks that enforce the gates live at the settings level, so they cover
your subagents too. The plugin registers them. If `gated status` shows
`owner unclaimed` right after you start a run, they aren't registered: run
`gated install claude` (a copied skill) or `gated install codex`, tell the
person, and ask them to restart the session.

## What the arguments mean

| Arguments | Do this |
| --- | --- |
| empty | Show the picker. See "Picking a workflow". |
| `status` | Run `gated status` and summarize it in a few lines. |
| `findings [--since 30d]` | Run `gated findings` and group what keeps coming up. Suggest a workflow change or a `gated learn` line for anything repeated. |
| `resume [run]` | Run `gated resume [--run <id>]`, then continue the loop below from wherever the run is. |
| `new <name>` | Write a new workflow with the person. See "Writing a workflow". |
| `customize <workflow>` | Make a workflow the team's own. See "Customizing a workflow". |
| `<workflow> [key=value ...]` | Start and run it. See "Running a workflow". |

## Picking a workflow

A bare `/gated` is the whole interface for most people. Make it one tap.

1. Run `gated status --since 7d` and `gated list`.
2. If a run in this project is `waiting`, `awaiting-approval` or `blocked`,
   the first choice is to pick it back up (`resume`).
3. Ask with your question tool (AskUserQuestion in Claude Code): one option
   per workflow, its description as the option's description, the one that
   fits this project best first and marked `(Recommended)`. Add "Write a new
   workflow" as the last option. Without a question tool, print the same
   choices as a short numbered list.
4. Ask for the chosen workflow's required inputs only, in one message, with
   a suggested value for each where the project makes one obvious (the
   test command from `package.json`, the repository from `git remote`).
   Defaults stay defaults.
5. Start it.

## Running a workflow

1. **Start it.** Run `gated start <workflow> [key=value ...]`. If it reports
   missing inputs, ask the person for them in one message, then start again.
   The output has a `gated-claim:` line; leave it alone. The hook uses it to
   make this session the owner of the run.
2. **Plan, if the run says `planning`.**
   - Run `gated step` and give its whole output to a planning subagent.
   - When the subagent returns, run `gated submit-plan`. If it lists problems,
     give them to a new foreground planning subagent along with the plan's
     path, and submit again.
   - For human approval, show the person the plan summary it prints, and
     nothing more. If the run wrote acceptance criteria first
     (`implement-story` does), show those too, marking any `(assumed)`, so one
     approval covers both. Ask them to type `approve` or say what to change.
   - Then end your turn. With human approval, only the person's own message can
     approve. With opt-in judge approval, the stop hook reviews the plan. If it
     rejects the plan, run `gated step` and hand the rejection brief to a new
     planning subagent before submitting again. Never approve on the person's
     behalf, and never say they approved.
   - If they ask for changes, send the changes to the planner, have it rewrite
     the plan, and run `gated submit-plan` again.
3. **Work each checkpoint.**
   - Run `gated step` and give its whole output to a fresh subagent, in the
     foreground. The brief holds the instructions, the inputs, the gates, the
     to-do rule and the learnings. Add nothing from this conversation beyond
     what the step needs. A fresh context is the point.
   - When the subagent returns, run `gated check`. It only reports; it never
     moves the run.
   - If a gate fails, start a new foreground subagent with the same brief,
     the check's output, and a note that the earlier work is in place and
     only the failures need fixing. Then run `gated check` again. Don't
     continue the first subagent with SendMessage: in Claude Code that runs
     in the background, and the only way to wait for it is to end your turn,
     which makes the Stop hook judge the files before the fix lands.
   - When every gate passes, end your turn. The Stop hook reruns the gates
     itself, moves the run to the next checkpoint and tells you which one.
     Run `gated step` again and repeat. Only the hook can advance a run.
4. **Human gates.** When the run is waiting for the person, put the question
   to them exactly as it's written, then end your turn. They answer by typing
   `approve` as the whole message, and a hook records it. Anything else they
   say is feedback: have a subagent act on it, then `gated check`. They can
   also type `cancel run` to end the run.
5. **Finishing.** When the run is `done`, show `gated report` in a few lines
   and ask the person to try the result. Where the report and what a
   subagent told you disagree, check the files and say which is true.
   Mention anything under "Found, not fixed" and ask whether it deserves its
   own run.
   - Record what they tell you with `gated learn "<one sentence>"`. Every
     later run of this workflow reads it.
   - If they want changes, write them as checkpoints in a JSON file (same shape
     as a plan, with gates), run `gated amend <file>`, and get their approval
     the same way. They can type `reject` to drop the new checkpoints.

### Rules that keep it honest

- Never edit a workflow's files, a run's `state.json`, a submitted plan, or
  tests locked by `gated red`. The hooks deny it, and a changed locked file
  fails every gate anyway. If a gate itself is wrong, say so to the person.
  Fixing gates belongs to them, outside the run.
- When you try to end your turn, the Stop hook reruns the current gates. If
  they fail, you're told why and you keep going. That counts as one attempt.
  When a gate uses up its attempts (5 unless the workflow says otherwise), the
  run stops as `blocked`. No gate is ever skipped.
- Run subagents in the foreground, and never end your turn while one is
  still working: the hook checks the files as they are, and a pass on
  unfinished work moves the run on without checking what lands later.
- A blocked run isn't a failure to hide. Show the report, say which gate is
  stuck and what it last printed, and suggest the fix. Only the person can
  grant fresh attempts: run `gated resume`, then ask them to type `approve`.
- `gated check` from your shell is advisory. Nothing you set in your shell
  (variables, `PATH`, a judge override) can pass a gate.
- Need the person mid-run, for something only they can decide? Run
  `gated ask "<question>"`, put the question to them, and end your turn. The
  run waits without spending an attempt, and their next message answers it.
  Don't end your turn to wait any other way: every other stop reruns the gates
  and counts.
- Keep the person's interruptions to one decision each.

### Scheduled and headless runs

When there's no one to answer, as with `claude -p "/gated weekly-report"` or a
Codex automation, don't ask questions. Run the loop to the end. A plan with
human approval stops as `awaiting-approval`; a plan with judge approval can
continue unattended. A human gate stops as `waiting`, which is correct. Run
`gated report` last, and end your reply with the report's path and the run's
status.

## Customizing a workflow

Every team builds things its own way. `/gated customize <workflow>` makes a
copy the team owns, and the copy then wins over the original.

1. Run `gated customize <workflow>` for this repository, or add `--to user`
   for every repository of this person.
2. Ask, one question at a time with a recommended answer each time:
   - which skills the team uses to plan, write tests, review, open pull
     requests and write stories. List the skills you can see, and recommend a
     match for each step;
   - the test command, and any other checks a change must pass;
   - review standards that belong in the review rubric;
   - steps they always do that the workflow lacks, and steps it has that they
     skip.
3. Edit the copy:
   - `"skills"` at the top of `workflow.json` for every checkpoint, on a
     checkpoint for one step, or on `"plan"` for the planner;
   - `"storySkill"` for the skill that writes new stories when work is split
     off;
   - gates and rubrics for their standards.
   In Claude Code a `skills` gate checks that a subagent loaded each listed
   skill. In Codex it can't be checked, and the report says so.
4. Run `gated lint <workflow>` until it's clean. Ask the person to commit the
   copy to their main branch before the first run: a customization committed
   on a story branch shows up in that story's diff, and the review judge
   rightly fails it as out of scope.
5. Offer a first run.

## Writing a workflow

`/gated new <name>` turns a process the person describes into a workflow
folder. Read `references/workflow-format.md` and
`references/writing-gates.md` first.

1. Ask where it lives. Recommend `~/.claude/workflows/<name>/` for personal
   workflows and `<project>/.claude/workflows/<name>/` for one that belongs to
   a repository.
2. Interview the person one question at a time, with a recommended answer each
   time:
   - what starts it,
   - what inputs it needs,
   - the steps in order,
   - for each step, what "done" looks like as something you can check.
3. Draft the steps yourself. Get the gates from a separate subagent that sees
   only the step files and `references/writing-gates.md`. Whoever writes a
   gate shouldn't be the one who needs it to pass, so the gates don't bend to
   fit the work.
4. Show the person the checkpoints and gates as a short list. Push back on any
   gate an agent could pass without doing the work.
5. Write the folder, run `gated lint <name>` until it's clean, and offer a
   first run. Claude Code asks the person before any write under `.claude/`;
   that's expected. If they refuse, write it elsewhere and give them the one
   `mv` that puts it in place.

If the process needs a plan per run (a feature, a migration), give the workflow
a `plan` step instead of fixed checkpoints. If it's the same every time (a
report, an onboarding checklist, a release), use fixed checkpoints so it can
run on a schedule.
