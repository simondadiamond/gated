"""Fail unless every acceptance criterion id appears in a test that was locked by `gated red`.

A locked test failed before the code existed and passed after it, unchanged, so a criterion
named in one is proven, not just mentioned.
"""
import json
import os
import re
import sys

run = sys.argv[1]
ids = sorted(set(re.findall(r"^\s*[-*]\s*\**(AC-\d+)", open(os.path.join(run, "acceptance.md")).read(), re.M)),
             key=lambda i: int(i.split("-")[1]))
state = json.load(open(os.path.join(run, "state.json")))
locked = sorted({f for entry in state.get("red", {}).values() for f in entry.get("files", [])})
text = ""
for path in locked:
    try:
        text += open(path, errors="replace").read()
    except OSError:
        pass
missing = [i for i in ids if not re.search(re.escape(i) + r"\b", text)]
if missing:
    sys.exit(f"{len(missing)} of {len(ids)} criteria have no locked test naming them: {', '.join(missing)}. "
             f"Locked test files: {len(locked)}")
print(f"all {len(ids)} criteria are named in {len(locked)} locked test file(s)")
