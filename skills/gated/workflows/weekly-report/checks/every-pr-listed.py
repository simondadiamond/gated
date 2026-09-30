"""Fail unless every pull request URL from prs.json appears in the report."""
import json
import sys

prs = json.load(open(sys.argv[1]))
report = open(sys.argv[2]).read()
missing = [f"#{pr['number']}" for pr in prs if pr["url"] not in report]
if missing:
    sys.exit(f"the report leaves out {len(missing)} of {len(prs)} pull requests: {', '.join(missing[:20])}")
print(f"all {len(prs)} pull requests are in the report")
