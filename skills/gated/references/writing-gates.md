# Writing gates

A gate is worth having when an agent can't pass it without doing the work.
Write each one by asking what a lazy agent would do to make it pass. Then close
that route.

## Pick the type in this order

1. **`command`**, when a program can decide. Tests, a type check, a lint, a
   `curl` that expects a status, `gh pr view` for a PR's state, a script in the
   workflow's `checks/` folder.
2. **`file`**, when the output is a document or data. Check its structure
   (`headings`, `json`), its content (`contains`), and its links (`links:
   "resolve"`). Existence alone proves nothing. The link check fetches
   without logging in, so a private repo's links or any page behind a login
   fail it; don't use it for those.
3. **`red-first`**, whenever a checkpoint adds tests. The tests must fail
   before the code exists and pass after, unchanged. That kills tests that
   assert nothing.
4. **`judge`**, only for what code can't check: whether a review is thorough,
   whether writing is clear, whether a diff matches its plan. A separate
   process grades it against a rubric, with no shared context.
5. **`human`**, for taste and for anything irreversible that a person should
   see first. It's the only gate code can't verify, so use it sparingly.

## Close the easy routes

| Weak gate | What a lazy agent does | Stronger gate |
| --- | --- | --- |
| `file` with only `path` | writes an empty or placeholder file | add `headings` and `contains` for the facts that must be there |
| `command: "npm test"` on new work | writes no tests, so the old suite still passes | `red-first` with `lock` on the new test files |
| `command: "test -f report.md"` | same as above | a `checks/` script that validates the content |
| `judge` rubric "is it good?" | anything passes | a rubric that lists concrete criteria, each MET or NOT MET |
| a gate that trusts a file the agent writes, e.g. "status: done" | writes "status: done" | check the real thing: the API, the git log, the rendered output |

A gate that compares two sources is strong because the agent controls neither.
Example: every URL in `prs.json` (fetched from GitHub) must appear in
`weekly.md`.

Don't name a file a subagent writes `report*.md`, `summary*.md`,
`findings*.md` or `analysis*.md`: Claude Code refuses those writes from
subagents, so the gate could never pass. `gated lint` catches it.

## Examples by kind of work

**Code**

```json
{ "id": "tests", "type": "red-first", "run": "npm test -- leave", "lock": ["src/**/leave*.test.ts"] }
{ "id": "types", "type": "command", "run": "npx tsc --noEmit" }
{ "id": "pushed", "type": "command", "run": "test \"$(git rev-parse @)\" = \"$(git rev-parse @{u})\"" }
```

**Outside systems**

A command can exit 75 while CI or another service has not decided yet:

```json
{ "id": "pr-checks", "type": "command", "run": "sh {{workflow}}/checks/pr-checks.sh",
  "pendingExit": 75, "pendingMax": 21600 }
```

```sh
states=$(gh pr checks --required --json state --jq '.[].state') || exit 1
printf '%s\n' "$states" | grep -Eq 'FAILURE|ERROR|CANCELLED' && exit 1
printf '%s\n' "$states" | grep -Eq 'PENDING|QUEUED|IN_PROGRESS' && exit 75
exit 0
```

Exit 75 spends no attempt until `pendingMax` expires. Any other nonzero exit is
a failure immediately.

**Documents and reports**

```json
{ "id": "report", "type": "file", "path": "{{run}}/weekly.md",
  "headings": ["Summary", "Risks"], "contains": ["\\d+ merged"], "links": "resolve" }
{ "id": "complete", "type": "command", "run": "python3 {{workflow}}/checks/every-pr-listed.py {{run}}" }
```

**Onboarding and operations**

```json
{ "id": "account", "type": "command", "run": "gh api orgs/acme/members/{{input.github}} --silent" }
{ "id": "welcome-doc", "type": "file", "path": "people/{{input.name}}/welcome.md",
  "headings": ["First week", "Who to ask"], "contains": ["{{input.manager}}"] }
{ "id": "calendar", "type": "human", "ask": "confirm the intro meetings are on {{input.name}}'s calendar" }
```

**Review**

```json
{ "id": "adversarial", "type": "judge", "rubric": "rubrics/review.md",
  "inputs": [{ "run": "git diff main...HEAD" }, { "file": "{{run}}/checkpoints.json" }] }
```

## Rubrics for judge gates

- List criteria the judge can check against the inputs, one per line.
- Say what fails: "any function over 60 lines without a test fails".
- Give the judge every input it needs. It sees only the rubric and the
  `inputs`; it can't browse.
- The judge ends with `VERDICT: PASS` or `VERDICT: FAIL`, and the gate reads
  only that line. Its verdict is cached until the inputs change.
- Ask for every unmet criterion in one pass. A judge that stops at the first
  problem turns one fix round into five.
- Give each question one owner. If the plan judge decided what the contract
  is, the review and proof judges say "the contract is settled; don't re-judge
  it". A later judge that reopens it fails work that was already approved.
- Exempt what a code gate already proved. "Every signature compiles" belongs
  to the type check, not to a rubric row the judge has to take on trust.
- Keep run-specific noise out of the inputs. A bot comment or a timestamp in
  an input changes the hash, so the cached verdict is thrown away and the
  judge runs again on the same work.

## Gates that cost more than they catch

The table above stops an agent from skipping work. A customized workflow has a
second failure mode: gates that fail for reasons the work didn't cause, so the
run pays for rounds that change nothing. Check each gate against these before
the first run.

**Every gate must be passable by its own step.** List what the step's agent is
allowed to change, then check each gate's failure can be fixed with only that.
Seen in real runs:

| Gate | Why the step couldn't pass it | Fix |
| --- | --- | --- |
| a plan judge that fails contradictory criteria | the planner was told never to edit the criteria | let the planner amend them with a stated reason the judge rules on |
| a review judge that fails a test style | the test was locked by `red-first` | refuse that style when the test is locked (a `checks/` script), and tell the judge locked tests change only through `gated relock` |
| a proof judge that wants a database failure shown live | no drive can make a shared database fail on demand | accept a locked test for failure paths, and list it as "proven by test" |

**Protect what the gates trust.** `gated` locks the workflow folder. A gate
that runs code outside it (a test config, a harness a proof script reads, a
helper in `scripts/`) can be weakened by the same agent it checks. List those
paths in a `checks/untouched` script that fails when the branch changes them.
Fail the run's own files too: nothing under `.gated/` belongs in a commit.

**Cap every loop.** `gated check` is advisory and spends no attempt, so an
orchestrator can call a failing judge again and again. Say in the step: "after
three failed checks of the same gate, stop and `gated ask`." Long waits on
outside systems (CI, reviewers) belong outside the workflow, or in a
`pendingExit` gate whose step does nothing until the result lands: every
wake-up is a paid turn.

**Side effects count.** A gate that needs a new commit (a "verified on <sha>"
line, a marker file) pushes a new head. If the branch has CI or AI reviewers,
each push restarts them. Prefer gates that read evidence already on disk.

**Ask the question once, early.** Some failures are decisions only a person can
make: scope, which roles get access, a product trade-off. A judge that
re-raises one every round costs a fix round each time. Have the rubric name
the decision, and have the step `gated ask` the first time it appears.

**Test the gate before the run.** Run each `checks/` script against two
finished examples, one that should pass and one that should fail, before it
guards a live run. A gate that has only ever seen its happy path will either
pass everything or block a correct run.

## Size

Each checkpoint should be reviewable in a few minutes. Order them from the
smallest foundation to the most complex, so a wrong early decision is cheap to
fix. If a checkpoint needs more than about five gates, split it.
