Open the pull request for this branch against {{input.base}}.

1. Make sure the working tree is clean and every change is committed.
2. Push the branch (never {{input.base}}, never with force).
3. Open the pull request with `gh pr create --base {{input.base}}`:
   - Title: a conventional commit in plain words, e.g.
     `feat(leave): managers can approve leave requests`.
   - Body: the problem in a sentence or two, then how you solved it. Link
     the story if it's an issue.
4. Don't merge it.

Then the person tries the change. Tell them exactly how: which command to run
or which page to open, and what they should see.
