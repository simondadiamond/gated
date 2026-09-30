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

## Size

Each checkpoint should be reviewable in a few minutes. Order them from the
smallest foundation to the most complex, so a wrong early decision is cheap to
fix. If a checkpoint needs more than about five gates, split it.
