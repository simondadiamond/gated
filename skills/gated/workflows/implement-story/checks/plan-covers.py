"""Fail unless every acceptance criterion is named in some planned checkpoint's instructions.

usage: plan-covers.py <run dir>

Runs on a submitted plan, before anyone approves it: a criterion no checkpoint covers would
otherwise surface only at review, after the code is written.
"""
import json
import os
import re
import sys

run = sys.argv[1]
ids = sorted(set(re.findall(r"^\s*[-*]\s*\**(AC-\d+)", open(os.path.join(run, "acceptance.md")).read(), re.M)),
             key=lambda i: int(i.split("-")[1]))
plan = json.load(open(os.path.join(run, "checkpoints.json")))
text = "\n".join(str(cp.get("instructions", "")) for cp in plan.get("checkpoints", []) if isinstance(cp, dict))
missing = [i for i in ids if not re.search(re.escape(i) + r"\b", text)]
if not ids:
    sys.exit("acceptance.md has no criteria")
if missing:
    sys.exit(f"{len(missing)} of {len(ids)} criteria are named in no checkpoint's instructions: {', '.join(missing)}")
print(f"all {len(ids)} criteria are covered by the plan")
