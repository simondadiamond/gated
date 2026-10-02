You are planning one story: {{input.story}}

Don't write any code. Your output is a plan file. The acceptance criteria
are already written; your plan is how to satisfy them.

1. **Read the story.** If it's an issue number or URL, read it with
   `gh issue view`, including comments. If it's a sentence, that's the whole
   spec. When something important is ambiguous, note it at the top of the plan
   file under `"questions"`. Don't guess.
2. **Read the acceptance criteria** in `{{run}}/acceptance.md`. They were
   written before you, by someone else, and they define done. If one is
   wrong (it contradicts the story or another criterion, or it can't be
   tested), fix it in `acceptance.md` and list each change with its reason
   under `"notes"` in the plan file: the person sees them when they approve,
   and the criteria lock once they do. If the fix needs a decision only the
   person can make, ask under `"questions"` instead.
   Make sure you're on a branch other than {{input.base}}; create
   `story/<short-name>` if you aren't.
3. **Read the code the story touches.** Find the files, the existing tests
   and their naming, and how similar features were built. Follow those
   patterns.
4. **Split the work into checkpoints,** smallest foundation first. Each one
   should be reviewable in a few minutes, and each builds on the one before.
   A wrong early decision should be cheap to catch.
5. **Give every checkpoint:**
   - a kebab-case `id` and a short `title` (a few words),
   - `instructions`: what to build, which files, which existing patterns to
     follow, and what "done" means from a user's point of view,
   - the criteria it covers, by id (`AC-2`, `AC-3`), in its instructions.
     Every criterion must be covered by some checkpoint.
   - `gates`. Every checkpoint that changes behavior gets a `red-first` gate:
     `run` is the narrowest test command that runs its new tests, and `lock`
     is a glob that matches only those new test files. The tests are written
     first, must fail against the current code, and must name the criterion
     they prove (`test("AC-2: rejects leave longer than the balance")`). Assert
     what a user or caller sees, not how the code is written: a test that reads
     source files as text breaks on a rename and, once locked, can't be fixed.
     The review step fails unless every criterion id appears in a locked test. Add `command` or
     `file` gates for anything else that must hold. The suite
     (`{{input.test}}`) is added to every checkpoint automatically, so don't
     repeat it.

A review checkpoint and a pull request checkpoint already run after yours.
Don't plan either of them.

Write `{{run}}/checkpoints.json`:

Before anyone sees the plan, code checks that the criteria are still well
formed and that each one is named in some checkpoint's instructions.

```json
{
  "questions": [],
  "notes": [],
  "checkpoints": [
    {
      "id": "leave-model",
      "title": "Leave request model",
      "instructions": "...",
      "gates": [
        { "id": "tests", "type": "red-first", "run": "npm test -- leave-model", "lock": ["src/leave/model.test.ts"] }
      ]
    }
  ]
}
```
