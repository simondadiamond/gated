"""Fail unless prs.json is a list of pull requests with the fields the report needs."""
import json
import sys

items = json.load(open(sys.argv[1]))
if not isinstance(items, list):
    sys.exit("prs.json must be a JSON array")
need = ("number", "title", "url", "mergedAt")
bad = [i for i, pr in enumerate(items) if not isinstance(pr, dict) or any(k not in pr for k in need)]
if bad:
    sys.exit(f"entries {bad[:10]} are missing one of: {', '.join(need)}")
print(f"{len(items)} merged pull requests")
