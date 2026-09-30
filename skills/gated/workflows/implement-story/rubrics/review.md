You are reviewing a branch that implements one story. Assume it's wrong until
the diff shows otherwise. Judge each criterion from the diff and commit list
below, and nothing else.

1. **Correct.** No logic errors, off-by-one mistakes, unhandled null or empty
   cases, or race conditions visible in the diff. Name the line for any you
   find.
2. **Tested.** Every behavior the diff adds or changes has a test that would
   fail without the change. Tests assert outcomes, not implementation details.
3. **Scoped.** No changes unrelated to the story: no drive-by refactors,
   formatting churn, or edits to unrelated files.
4. **Clean.** No debug output, commented-out code, TODOs without a linked
   issue, or leftover files.
5. **Errors handled.** Failures from I/O, network, parsing and external
   services are handled or surfaced, not swallowed.
6. **Safe.** No secrets, no injection risk from user input, no permission
   checks removed.
7. **Consistent.** New code follows the patterns and naming of the code around
   it.

A criterion that can't be judged from the inputs is NOT MET. Say which input
you'd need.
