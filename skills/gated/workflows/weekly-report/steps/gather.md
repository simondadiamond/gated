Collect every pull request merged into {{input.repo}} in the last {{input.days}} days.

1. Work out the start date, {{input.days}} days before today, as YYYY-MM-DD.
2. Run:

   ```bash
   gh pr list --repo {{input.repo}} --state merged --limit 200 \
     --search "merged:>=<start date>" \
     --json number,title,url,author,mergedAt
   ```

3. Write the result to `{{run}}/prs.json` as a JSON array. Keep GitHub's
   values exactly: don't edit titles or drop entries. An empty array is a
   valid result for a quiet week.

If `gh` isn't authenticated, stop and say so. Don't invent data.
