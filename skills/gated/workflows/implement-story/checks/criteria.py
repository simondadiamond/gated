"""Fail unless acceptance.md has numbered, unique Given/When/Then criteria."""
import re
import sys

text = open(sys.argv[1]).read()
criteria = re.findall(r"^\s*[-*]\s*\**(AC-\d+)\**\s*:?\s*(.+)$", text, re.M)
if not criteria:
    sys.exit("no criteria found. Write them as `- AC-1: Given ..., when ..., then ...`")
ids = [i for i, _ in criteria]
dupes = sorted({i for i in ids if ids.count(i) > 1})
if dupes:
    sys.exit(f"criterion ids used twice: {', '.join(dupes)}")
vague = [i for i, body in criteria if not all(w in body.lower() for w in ("given", "when", "then"))]
if vague:
    sys.exit(f"not in Given/When/Then form: {', '.join(vague)}")
print(f"{len(criteria)} acceptance criteria")
