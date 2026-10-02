"""Gate health across runs: which gates cost more than they catch.

Every stop records each gate's result in the run's `history` (see runner.record_history). This
module groups those results by gate, across the runs of a workflow, and flags the gates whose
cost shows a pattern a workflow change could remove. A gate that failed once or twice and then
passed is the workflow doing its job, so it is never a candidate.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from .core import Run

LOOP_FAILS = 3  # stop failures of one gate in one run before it counts as a loop
RECUR_RUNS = 2  # runs that must share a failure reason before it counts as recurring
DECISIONS = 2  # decisions a gate must raise (in total) before asking up front is worth it


def gate_key(cp: str, gate: str, planned: bool) -> str:
    # Planned checkpoints get new ids every run, so their gates are grouped by gate id alone.
    return f"(planned)/{gate}" if planned else f"{cp}/{gate}"


def reason_of(entry: Dict[str, Any]) -> List[str]:
    """What a failure was about, in a form that repeats across runs: the criteria a judge marked
    NOT MET. A command's summary ("`npm test` exited 1") is the same every time whatever broke,
    so it says nothing about recurrence and isn't used."""
    return list(entry.get("notMet") or [])


def run_stats(run: Run, since: Optional[Dict[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Per gate, what happened in one run. `since` maps a dismissed gate to when it was dismissed:
    only what happened after that counts for it."""
    since = since or {}
    planned_ids = {c["id"] for c in run.state.get("checkpoints", []) if "instructions" in c}
    stats: Dict[str, Dict[str, Any]] = defaultdict(lambda: {
        "fails": 0, "passes": 0, "judgeCalls": 0, "cost": 0.0, "flips": 0, "decisions": [], "reasons": Counter(),
        "blocks": 0, "first": None, "last": None})
    last_judge: Dict[str, Dict[str, Any]] = {}
    for e in run.state.get("history", []):
        key = gate_key(e["cp"], e["gate"], bool(e.get("planned")))
        if e["at"] <= since.get(key, ""):
            continue
        s = stats[key]
        s["first"] = s["first"] or e["at"]
        s["last"] = e["at"]
        if e.get("pending"):
            continue
        if e["type"] == "judge" and not e.get("cached") and not e.get("error"):
            s["judgeCalls"] += 1
        s["cost"] += float(e.get("cost") or 0)
        if e.get("decision"):
            s["decisions"].append(e["decision"])
            continue
        if e["ok"]:
            s["passes"] += 1
        else:
            s["fails"] += 1
            s["reasons"].update(reason_of(e))
        if e["type"] == "judge" and e.get("tree") and not e.get("error"):
            prev = last_judge.get(key)
            if prev and prev["tree"] == e["tree"] and prev["ok"] != e["ok"]:
                s["flips"] += 1
            last_judge[key] = e
    for b in run.state.get("blocks", []):
        for reason in b.get("on", []):
            if reason == "plan":
                # The plan judge's budget, or a planner that never submitted.
                judged = any(e["cp"] == "plan" and e["gate"] == "plan-review" for e in run.state.get("history", []))
                key = "plan/plan-review" if judged else "plan/(not submitted)"
            elif "/" in reason:
                cp, gate = reason.split("/", 1)
                key = gate_key(cp, gate, cp in planned_ids)
            else:
                continue
            if b.get("at", "") <= since.get(key, ""):
                continue
            stats[key]["blocks"] += 1
    return dict(stats)


def load_dismissed(workflow_dir) -> Dict[str, Dict[str, str]]:
    from pathlib import Path

    from .core import read_json

    path = Path(workflow_dir) / "health.json"
    if not path.is_file():
        return {}
    data = read_json(path)
    return data.get("dismissed", {}) if isinstance(data, dict) else {}


def dismiss(workflow_dir, gate: str, reason: str) -> str:
    """The person looked at a flagged gate and kept it: don't flag it again for what already
    happened. Kept in the workflow folder, so it travels with the workflow."""
    from pathlib import Path

    from .core import GatedError, now, read_json, write_json

    if not reason.strip():
        raise GatedError("say why the gate stays as it is: --reason \"...\"")
    path = Path(workflow_dir) / "health.json"
    data = read_json(path) if path.is_file() else {}
    data.setdefault("dismissed", {})[gate] = {"at": now(), "reason": reason.strip()[:500]}
    write_json(path, data)
    return f"{gate} won't be flagged again for what already happened. Recorded in {path}"


def analyze(runs: List[Run], focus: Optional[Run] = None,
            dismissed: Optional[Dict[str, Dict[str, str]]] = None) -> Dict[str, Any]:
    """Aggregate per gate over runs of one workflow and list the candidates for a change."""
    since = {gate: d.get("at", "") for gate, d in (dismissed or {}).items()}
    per_run = {r.id: run_stats(r, since) for r in runs}
    gates: Dict[str, Dict[str, Any]] = {}
    for rid, stats in per_run.items():
        for key, s in stats.items():
            g = gates.setdefault(key, {"runs": [], "fails": 0, "worst": (0, None), "blocks": 0, "judgeCalls": 0,
                                       "cost": 0.0, "flips": 0, "decisions": [], "reasons": defaultdict(set)})
            g["runs"].append(rid)
            g["fails"] += s["fails"]
            if s["fails"] > g["worst"][0]:
                g["worst"] = (s["fails"], rid)
            g["blocks"] += s["blocks"]
            g["judgeCalls"] += s["judgeCalls"]
            g["cost"] += s["cost"]
            g["flips"] += s["flips"]
            g["decisions"] += [(rid, q) for q in s["decisions"]]
            for label in s["reasons"]:
                g["reasons"][label].add(rid)
    candidates = []
    for key, g in gates.items():
        signals = []
        if g["blocks"]:
            signals.append(f"blocked {g['blocks']} time(s)")
        if g["worst"][0] >= LOOP_FAILS:
            signals.append(f"failed {g['worst'][0]} times in one run ({g['worst'][1]})")
        if g["flips"]:
            signals.append(f"the judge changed its verdict {g['flips']} time(s) on unchanged work")
        recurring = sorted(((label, sorted(rids)) for label, rids in g["reasons"].items() if len(rids) >= RECUR_RUNS),
                           key=lambda x: -len(x[1]))
        for label, rids in recurring[:3]:
            signals.append(f"same reason in {len(rids)} runs: \"{label}\"")
        decision_runs = {rid for rid, _ in g["decisions"]}
        if len(g["decisions"]) >= DECISIONS:
            signals.append(f"asked the person {len(g['decisions'])} time(s) in {len(decision_runs)} run(s)")
        if not signals:
            continue
        if focus is not None:
            mine = per_run.get(focus.id, {}).get(key)
            if not mine or not (mine["fails"] or mine["blocks"] or mine["flips"] or mine["decisions"]):
                continue
        top = Counter({label: len(rids) for label, rids in g["reasons"].items()}).most_common(3)
        candidates.append({"gate": key, "signals": signals, "fails": g["fails"], "judgeCost": round(g["cost"], 2),
                           "reasons": [f"{label} ({n} run(s))" for label, n in top],
                           "decisions": [q for _, q in g["decisions"]][:3]})
    candidates.sort(key=lambda c: (-len(c["signals"]), -c["fails"]))
    return {"runs": [r.id for r in runs], "gates": gates, "perRun": per_run, "candidates": candidates}


def render(workflow: str, result: Dict[str, Any], focus: Optional[Run] = None) -> str:
    gates = result["gates"]
    lines = [f"{workflow}: {len(result['runs'])} run(s)" + (f", focus on {focus.id}" if focus else "")]
    if not gates:
        return lines[0] + "\n  no gate results recorded yet: runs record them from this version of gated on"
    head = f"  {'gate':<36} {'runs':>4} {'fails':>5} {'worst':>5} {'blocks':>6} {'judge calls':>11} {'judge $':>8} {'flips':>5}"
    if focus:
        head += f" {'this run':>8}"
    lines += ["", head]
    for key in sorted(gates, key=lambda k: (-gates[k]["fails"], k)):
        g = gates[key]
        row = (f"  {key[:36]:<36} {len(g['runs']):>4} {g['fails']:>5} {g['worst'][0]:>5} {g['blocks']:>6} "
               f"{g['judgeCalls']:>11} {g['cost']:>8.2f} {g['flips']:>5}")
        if focus:
            row += f" {result['perRun'].get(focus.id, {}).get(key, {}).get('fails', 0):>8}"
        lines.append(row)
    cands = result["candidates"]
    lines += ["", "Candidates for a workflow change:" if cands else
              "No candidates: every failure was fixed within a few tries and didn't repeat across runs."]
    for n, c in enumerate(cands, 1):
        lines.append(f"{n}. {c['gate']}: " + "; ".join(c["signals"]))
        detail = f"   {c['fails']} stop failure(s)" + (f", ${c['judgeCost']:.2f} in judge calls" if c["judgeCost"] else "")
        if c["reasons"]:
            detail += ". Reasons: " + ", ".join(c["reasons"])
        lines.append(detail)
        for q in c["decisions"]:
            lines.append(f"   asked: {q}")
    return "\n".join(lines)
