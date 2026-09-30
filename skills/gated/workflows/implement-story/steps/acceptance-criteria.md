Write the acceptance criteria for this story: {{input.story}}

Don't plan and don't write code. Your job is to pin down what "done" means
before anyone decides how to build it.

1. Read the story. If it's an issue number or URL, read it with
   `gh issue view`, including comments. Read enough of the code to know what
   exists today.
2. Write `{{run}}/acceptance.md` with two sections:
   - `## Acceptance criteria`: one line per behavior, numbered, as
     `- AC-1: Given <starting state>, when <action>, then <observable result>.`
     Cover the main path, the edge cases the story implies (empty input,
     missing permissions, errors from dependencies), and anything the story
     explicitly rules out. Each criterion must be checkable by an automated
     test, from the outside, without reading the implementation.
   - `## Out of scope`: what this story doesn't cover, so nobody builds it by
     accident.
3. Where the story is ambiguous, write the criterion you'd recommend and add
   `(assumed)` at the end, so the person sees it when they approve the plan.

The criteria are shown to the person together with the plan, and every one of
them has to end up in a test that failed before the code existed.
