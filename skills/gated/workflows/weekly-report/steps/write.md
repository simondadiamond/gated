Write `{{run}}/report-{{input.days}}d.md` from `{{run}}/prs.json`. Nothing
else is a source: don't add pull requests from memory.

Use these sections, in this order:

- `# Summary`: three sentences at most. What changed for users this period,
  in plain words. If nothing merged, say that.
- `## Merged`: one line per pull request: `- [#<number>](<url>) <title> (<author>)`.
  Group them under `###` subheadings by theme if there are more than eight.
- `## Risks`: anything a reader should watch: large changes, reverts,
  migrations, anything touching auth or billing. Write "None spotted" if so.

The gates check the three headings, that every link resolves, and that every
pull request in `prs.json` appears in the report.
