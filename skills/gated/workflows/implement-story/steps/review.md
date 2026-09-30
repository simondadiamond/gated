Every planned checkpoint has passed. Before the independent reviewer sees the
branch, review it yourself the way it will.

1. Read `git diff {{input.base}}...HEAD` in full.
2. Check it against the rubric: correctness, tests that prove behavior, no
   unrelated changes, no leftovers, errors handled, the story fully done.
3. Fix what you find. Keep each fix small and in the same style as the code
   around it. Commit with a message that says what the fix is for. A fix to
   behavior gets a test that fails without it. Put it in a new test file:
   the tests written earlier are locked and can't change.

The `adversarial` gate then sends the diff to a separate reviewer that shares
none of your context. If it fails, its findings come back to you. Fix every
finding that names a real failure. If you think a finding is wrong, record why
in `{{run}}/review/declined.md`; the next review sees your commits, not your
argument.
