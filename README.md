# gated

`/gated <workflow>` runs a job as a series of checkpoints. Each checkpoint
ends with gates, and the gates are checked by code, not by the agent. When the
agent tries to finish early, a hook reruns the gates and sends it back with the
failure. After five failed attempts on a gate the run stops as `blocked` with a
report. No gate is ever skipped.

```text
/gated hello                                  one-minute tour
/gated weekly-report repo=owner/name          scheduled-safe report of merged PRs
/gated implement-story story=#42 test="npm test"
/gated new onboarding                         build your own workflow
/gated customize implement-story              make a workflow your team's own
/gated status
```

A workflow is a folder with a `workflow.json` and a markdown file per step.
Gates can be:

- a command that must exit 0,
- a file that must have certain headings, content and working links,
- tests that must fail before the code exists and pass after without edits,
- a to-do list the agent writes first and must finish,
- an independent `claude -p` or `codex exec` reviewer grading against a rubric,
- your own approval, which only counts when you typed it.

Every phase is done by a fresh subagent, and that's checked: the hooks log
each tool call with the id of the agent that made it. `implement-story`
writes acceptance criteria before it plans, and every criterion has to end up
in a test that failed before the code existed. When a step finds work that
belongs in its own story, it creates the story on GitHub, and the hook checks
the link.

The built-in workflows are generic. `/gated customize` copies one into your
repository and wires in your team's own skills for each step.

It works the same in Claude Code and Codex, and runs from a schedule with one
line: `claude -p "/gated weekly-report repo=owner/name"`. The hooks do nothing
in a session that doesn't own a run. With the plugin they're registered for
you. For a copied skill run `gated install claude`, and for Codex run
`gated install codex`.

The design, and why it's shaped this way:
[docs/gated-design.md](docs/gated-design.md).

## Install

As a Claude Code plugin:

```bash
claude plugin marketplace add simondadiamond/gated
claude plugin install gated@gated
```

Or copy the skill with the [skills CLI](https://github.com/vercel-labs/skills):

```bash
npx skills@latest add simondadiamond/gated
```

gated started in [simondadiamond/skills](https://github.com/simondadiamond/skills),
which has its earlier commit history.

## License

MIT.
