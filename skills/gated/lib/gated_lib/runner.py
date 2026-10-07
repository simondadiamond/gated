"""Run lifecycle: start, plan approval, checks, advancing, reports. The only writer of run state."""

from __future__ import annotations

import datetime
import hashlib
import os
import secrets
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import gates as G
from .core import (
    DEFAULT_ATTEMPTS,
    FINISHED_STATUSES,
    GatedError,
    Run,
    every_collisions,
    find_workflow,
    git_lines,
    ignore_runs,
    protected_files,
    record_claim,
    lint_checkpoint,
    load_workflow,
    new_run_id,
    now,
    read_json,
    render,
    runs_root,
    sha256_file,
    write_json,
)

# A plan judge that gives no verdict is retried on the next stop this many times before it counts.
JUDGE_ERROR_RETRIES = 3

TODOS_GATE = {"id": "todos", "type": "todos"}
FRESH_GATE = {"id": "fresh-context", "type": "fresh-context"}


def detect_harness() -> str:
    return "codex" if os.environ.get("CODEX_THREAD_ID") else "claude"


def parse_inputs(wf: Dict[str, Any], args: List[str]) -> Dict[str, str]:
    spec = wf.get("inputs", {})
    given: Dict[str, str] = {}
    for a in args:
        if "=" not in a:
            raise GatedError(f"inputs are key=value; got '{a}'. This workflow takes: {', '.join(spec) or 'nothing'}")
        k, v = a.split("=", 1)
        if k not in spec:
            raise GatedError(f"unknown input '{k}'. This workflow takes: {', '.join(spec) or 'nothing'}")
        given[k] = v
    for k, s in spec.items():
        if k not in given and "default" in s:
            given[k] = str(s["default"])
    missing = [k for k, s in spec.items() if s.get("required") and k not in given]
    if missing:
        lines = [f"  {k}=...  {spec[k].get('description', '')}".rstrip() for k in missing]
        raise GatedError("missing inputs:\n" + "\n".join(lines))
    return given


def materialize(wf: Dict[str, Any], wdir: Path, cps: List[Dict[str, Any]], planned: bool) -> List[Dict[str, Any]]:
    out = []
    for cp in cps:
        item = {
            "id": cp["id"],
            "title": cp.get("title", cp["id"]),
            "gates": list(cp.get("gates", [])) + list(wf.get("every", [])) + [dict(TODOS_GATE)]
            + ([dict(FRESH_GATE)] if wf.get("freshContext", True) else [])
            + ([{"id": "skills", "type": "skills", "skills": skills_for(wf, cp)}] if skills_for(wf, cp) else []),
            "status": "pending",
        }
        if planned:
            item["instructions"] = cp["instructions"]
        else:
            item["step"] = str(wdir / cp["step"])
        out.append(item)
    return out


def skills_for(wf: Dict[str, Any], cp: Dict[str, Any]) -> List[str]:
    """Workflow-wide skills first, then the checkpoint's own, without repeats."""
    out: List[str] = []
    for name in list(wf.get("skills", [])) + list(cp.get("skills", [])):
        if name not in out:
            out.append(name)
    return out


def definition_files(wdir: Path, shares: Optional[List[str]] = None) -> List[Path]:
    """Every file in the workflow folder and the folders it shares, except learnings.md and
    health.json (feedback, written between runs): steps, rubrics and check scripts. Locked for the
    whole run, so no gate can be loosened mid-run."""
    files = set()
    for folder in [wdir] + [(wdir / s).resolve() for s in shares or []]:
        files |= {p for p in folder.rglob("*") if p.is_file() and p.name not in ("learnings.md", "health.json")
                  and ".git" not in p.parts}
    return sorted(files)


def lock(run: Run, paths: List[Path]) -> None:
    locks = run.state.setdefault("locks", {})
    for p in paths:
        digest = sha256_file(p)
        if digest:
            locks[str(p.resolve())] = digest


def start(project: Path, name: str, args: List[str]) -> Run:
    wdir = find_workflow(name, project)
    wf = load_workflow(wdir)
    inputs = parse_inputs(wf, args)
    if wf.get("commit") and not subprocess.run(["git", "-C", str(project), "config", "user.email"],
                                               capture_output=True).stdout.strip():
        raise GatedError("this workflow commits after each checkpoint, and git has no user.email in this "
                         "repository. Set it first: git config user.name \"<name>\" && git config user.email <email>")
    runs_root(project).mkdir(parents=True, exist_ok=True)
    ignore_runs(project)
    while True:  # two starts at once each get their own id
        rid = new_run_id(project, wf["name"])
        rdir = runs_root(project) / rid
        try:
            rdir.mkdir()
            break
        except FileExistsError:
            continue
    planned = bool(wf.get("plan"))
    before = materialize(wf, wdir, wf.get("before", []), planned=False)
    starts_running = bool(before) or not planned
    state = {
        "id": rid,
        "workflow": wf["name"],
        "workflowDir": str(wdir),
        "project": str(project),
        "inputs": inputs,
        "harness": detect_harness(),
        "owner": None,
        "claim": secrets.token_hex(8),
        "status": "running" if starts_running else "planning",
        "current": 0 if starts_running else None,
        "checkpoints": before if planned else before + materialize(wf, wdir, wf["checkpoints"], planned=False),
        "planAt": len(before) if planned else None,
        "planned": not planned,
        "attempts": {},
        "red": {},
        "locks": {},
        "approvals": [],
        "last": {},
        "createdAt": now(),
    }
    write_json(rdir / "state.json", state)
    run = Run(rdir)
    lock(run, definition_files(wdir, wf.get("shares")))
    if wf.get("protect"):
        # Code outside the workflow that its gates trust (a test config, a harness a check reads).
        # Locked like the workflow, so the agent being checked can't weaken the checker.
        run.state["protect"] = list(wf["protect"])
        files = protected_files(project, wf["protect"])
        lock(run, files)
        run.state["protectLocked"] = [str(p.resolve()) for p in files]
    if starts_running:
        activate(run)
    run.save()
    record_claim(state["claim"], rdir)
    return run


def activate(run: Run) -> None:
    cp = run.current()
    if cp is None:
        return
    cp["status"] = "active"
    cp.setdefault("startedAt", now())
    (run.dir / cp["id"]).mkdir(parents=True, exist_ok=True)


def plan_path(run: Run) -> Path:
    return run.dir / "checkpoints.json"


def read_plan(run: Run, path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        raise GatedError(f"no plan yet: write {path}")
    data = read_json(path)
    cps = data.get("checkpoints") if isinstance(data, dict) else None
    questions = plan_questions(data)
    if isinstance(cps, list) and not cps and questions:
        raise GatedError("the planner stopped with questions instead of a plan. Ask the person:\n"
                         + "\n".join(f"  - {q}" for q in questions))
    if not isinstance(cps, list) or not cps:
        raise GatedError(f"{path} needs {{\"checkpoints\": [ ... ]}} with at least one checkpoint")
    errors: List[str] = []
    for i, cp in enumerate(cps):
        lint_checkpoint(cp, f"checkpoints[{i}]", run.workflow_dir, errors, planned=True)
    taken = {c["id"] for c in run.state["checkpoints"]} | {c.get("id") for c in run.workflow().get("checkpoints", [])}
    for cp in cps:
        if isinstance(cp, dict) and cp.get("id") in taken:
            errors.append(f"checkpoint id '{cp['id']}' is already used in this run")
    ids = [c.get("id") for c in cps if isinstance(c, dict)]
    errors += [f"checkpoint id '{i}' is used twice" for i in sorted({i for i in ids if ids.count(i) > 1 and i})]
    errors += every_collisions(run.workflow().get("every", []), cps)
    if errors:
        raise GatedError(f"{path} has problems:\n  - " + "\n  - ".join(errors))
    return cps


def plan_questions(data: Any) -> List[str]:
    qs = data.get("questions", []) if isinstance(data, dict) else []
    return [str(q) for q in qs if str(q).strip()] if isinstance(qs, list) else []


def plan_summary(cps: List[Dict[str, Any]]) -> str:
    lines = []
    for n, cp in enumerate(cps, 1):
        gates = ", ".join(f"{g['id']} ({g['type']})" for g in cp["gates"])
        lines.append(f"{n}. {cp['title']}  [{cp['id']}]\n   gates: {gates}")
    return "\n".join(lines)


def snapshot(run: Run, path: Path, prefix: str) -> Path:
    """Copy a submitted plan to a numbered file and lock the copy, so the working file stays editable."""
    n = 1 + len(list(run.dir.glob(f"{prefix}-*.json")))
    copy = run.dir / f"{prefix}-{n}.json"
    copy.write_text(path.read_text())
    lock(run, [copy])
    return copy


def submit_plan(run: Run, path: Optional[Path] = None) -> str:
    if run.status not in ("planning", "awaiting-approval") or run.state.get("amendment"):
        raise GatedError(f"{run.id} is {run.status}; submit a plan while planning or while the plan awaits approval")
    path = path or plan_path(run)
    wf = run.workflow()
    if wf.get("freshContext", True):
        plan_rows = [r for r in G.read_activity(run) if r.get("phase") == "plan"]
        if not any(r.get("agent") != "main" for r in plan_rows):
            raise GatedError("no planning subagent worked on this plan. Hand `gated step` to a subagent and let it write the plan")
        wanted = wf.get("plan", {}).get("skills", [])
        if wanted and run.state.get("harness") != "codex":
            used = G.skills_used(run, "plan")
            missing = [w for w in wanted if not any(G.skill_matches(u, w) for u in used)]
            if missing:
                raise GatedError("the planning subagent never loaded: " + ", ".join(missing))
        edits = [r for r in plan_rows if r.get("agent") == "main" and r.get("edit")]
        if edits:
            raise GatedError(f"the orchestrator changed files itself while planning (e.g. {edits[0].get('what', '?')}). "
                             "The planning subagent writes the plan")
    head = run.state["checkpoints"][: run.state.get("planAt") or 0]
    run.state["checkpoints"] = head
    cps = read_plan(run, path)
    plan = wf.get("plan", {})
    if plan.get("approval", "human") != "judge" and plan.get("gates"):
        # With a judge, plan gates run at the stop, before the judge is paid. With a person, they run
        # here: a plan whose own files fail a script never reaches the person.
        failing = [r for r in (G.evaluate(run, {"id": "plan"}, g) for g in plan["gates"]) if not r["ok"]]
        if failing:
            raise GatedError("the plan fails its gates, so it wasn't submitted:\n"
                             + "\n".join(f"  - {r['id']}: {r['summary']}\n{r.get('log', '')[-800:]}" for r in failing))
    items = materialize(wf, run.workflow_dir, cps, planned=True)
    tail = materialize(wf, run.workflow_dir, wf.get("checkpoints", []), planned=False)
    run.state["checkpoints"] = head + items + tail
    run.state["planned"] = True
    run.state["plan"] = str(snapshot(run, path, "plan"))
    run.state["status"] = "awaiting-approval"
    run.save()
    data = read_json(path)
    questions = plan_questions(data)
    asked = ("\n\nOpen questions from the planner:\n" + "\n".join(f"- {q}" for q in questions)) if questions else ""
    notes = [str(n) for n in data.get("notes", []) if str(n).strip()] if isinstance(data.get("notes"), list) else []
    noted = ("\n\nNotes from the planner (for example, criteria it amended and why):\n"
             + "\n".join(f"- {n}" for n in notes)) if notes else ""
    return plan_summary(run.state["checkpoints"]) + noted + asked


def amend(run: Run, path: Path) -> str:
    """Add checkpoints to a run, usually from feedback after it finished. Needs approval again."""
    if run.status not in ("done", "waiting", "blocked"):
        raise GatedError(f"{run.id} is {run.status}. Amend a run once it's waiting on the person, blocked or done; "
                         "while it runs, finish the current checkpoint first")
    cps = read_plan(run, path)
    items = materialize(run.workflow(), run.workflow_dir, cps, planned=True)
    first_new = len(run.state["checkpoints"])
    run.state["checkpoints"].extend(items)
    snapshot(run, path, "amend")
    run.state["amendment"] = {"from": first_new, "status": run.status, "current": run.state.get("current")}
    if run.status == "done":
        run.state["current"] = first_new
    run.state["status"] = "awaiting-approval"
    run.save()
    return plan_summary(items)


def approve_awaiting(run: Run, text: str, by: str = "person") -> Optional[Dict[str, Any]]:
    """Activate an approved submitted plan or amendment and return its first checkpoint."""
    stamp = {"at": now(), "text": text.strip()[:200], "by": by}
    amendment = run.state.pop("amendment", None)
    run.state["approvals"].append({**stamp, "gate": "amendment" if amendment else "plan"})
    run.state.pop("planReview", None)
    run.state["status"] = "running"
    if not amendment:
        # What the plan was approved against (the acceptance criteria) freezes with it. A change after
        # this goes through `gated relock`, with a reason and a person's approve.
        frozen = [G.resolve_path(run, p, {}) for p in run.workflow().get("plan", {}).get("lock", [])]
        frozen = [p for p in frozen if p.is_file()]
        lock(run, frozen)
        run.state["planLocked"] = sorted({*run.state.get("planLocked", []), *(str(p.resolve()) for p in frozen)})
    if run.state.get("current") is None:
        run.state["current"] = run.state.get("planAt") or 0
    activate(run)
    run.save()
    return run.current()


def approve(run: Run, text: str) -> str:
    """Called by the UserPromptSubmit hook with what the person typed. Records, never checks:
    the next Stop reruns the gates, so the person's prompt never waits on a test suite."""
    stamp = {"at": now(), "text": text.strip()[:200], "by": "person"}
    if run.status == "awaiting-approval":
        amendment = bool(run.state.get("amendment"))
        cp = approve_awaiting(run, text)
        return f"gated: you approved {'the new checkpoints' if amendment else 'the plan'} for {run.id}. Next is '{cp['id']}'; the agent runs `gated step` for its brief."
    if run.status == "waiting" and run.state.get("relock"):
        req = run.state.pop("relock")
        for f in req["files"]:
            run.state["locks"].pop(f, None)
        run.state.setdefault("unlocked", {}).update({f: req["reason"] for f in req["files"]})
        run.state.setdefault("relocks", []).append({**req, "approvedAt": now(), "by": "person"})
        run.state["approvals"].append({**stamp, "gate": "relock"})
        run.state["status"] = "running"
        run.save()
        names = ", ".join(Path(f).name for f in req["files"])
        return (f"gated: you unlocked {names}. The agent amends them now; the next stop locks them again "
                "and the report lists the change with its reason.")
    if run.status == "waiting":
        cp = run.current()
        for g in cp["gates"]:
            if g["type"] == "human":
                run.state["approvals"].append({**stamp, "gate": f"{cp['id']}/{g['id']}"})
        run.state["status"] = "running"
        run.save()
        return f"gated: you approved '{cp['id']}'. When the agent next stops, the gates run once more and the run moves on."
    if run.status == "blocked" and run.state.get("resumeRequested"):
        cp = run.current()
        plan_blocked = "plan" in run.state.get("blockedOn", [])
        for key in list(run.state["attempts"]):
            if (cp and key.startswith(f"{cp['id']}/")) or key == "plan":
                del run.state["attempts"][key]
        if plan_blocked:
            head = run.state["checkpoints"][: run.state.get("planAt") or 0]
            run.state["checkpoints"] = head
            run.state["planned"] = False
            run.state["current"] = run.state.get("planAt") or (0 if head else None)
        for k in ("blockedOn", "resumeRequested"):
            run.state.pop(k, None)
        if cp and not plan_blocked:  # fresh-context judges only activity logged after this redo
            cp["freshFrom"] = len(G.read_activity(run))
        run.state["approvals"].append({**stamp, "gate": "resume"})
        run.state["status"] = "planning" if plan_blocked or not run.state["checkpoints"] else "running"
        run.save()
        return f"gated: you gave {run.id} fresh attempts on '{cp['id'] if cp and not plan_blocked else 'plan'}'."
    return ""


def reject(run: Run, text: str) -> str:
    """The person turned down an amendment or a relock. Put the run back how it was."""
    if run.status == "waiting" and run.state.get("relock"):
        req = run.state.pop("relock")
        run.state.setdefault("relocks", []).append({**req, "refusedAt": now(), "by": "person"})
        run.state["status"] = "running"
        run.save()
        return "gated: you refused the relock. The tests stay locked as they are."
    amendment = run.state.pop("amendment", None)
    if run.status != "awaiting-approval" or not amendment:
        return ""
    del run.state["checkpoints"][amendment["from"]:]
    run.state["current"] = amendment["current"]
    run.state["status"] = amendment["status"]
    run.state["approvals"].append({"at": now(), "text": text.strip()[:200], "gate": "amendment-rejected"})
    run.save()
    return f"gated: you rejected the new checkpoints. {run.id} is {run.status} again."


def cancel(run: Run, text: str) -> str:
    run.state["status"] = "cancelled"
    run.state["approvals"].append({"at": now(), "text": text.strip()[:200], "gate": "cancelled"})
    run.save()
    write_report(run)
    return f"gated: you cancelled {run.id}. Its report is at {run.dir / 'report.md'}."


def budget_for(run: Run, gate: Dict[str, Any]) -> int:
    return gate.get("attempts") or run.workflow().get("attempts") or DEFAULT_ATTEMPTS


def write_log(run: Run, cp: Dict[str, Any], r: Dict[str, Any], advisory: bool = False) -> None:
    d = run.dir / cp["id"] / "gates"
    d.mkdir(parents=True, exist_ok=True)
    # Appended, not overwritten: a gate's history (each judge verdict, each rerun) is the evidence
    # for why a checkpoint took the attempts it took. "(check)" marks an advisory `gated check`.
    cost = f"  ${r['cost']:.2f}" if isinstance(r.get("cost"), (int, float)) else ""
    with (d / f"{r['id']}.log").open("a") as f:
        f.write(f"{r['at']}  {'PASS' if r['ok'] else 'FAIL'}{' (check)' if advisory else ''}  {r['summary']}{cost}\n\n{r['log']}\n\n")


def work_digest(run: Run) -> Optional[str]:
    """A fingerprint of the work in the repository: HEAD, uncommitted changes to tracked files,
    and untracked files git doesn't ignore. With the digest of a judge's file inputs (the plan,
    a criteria file), two verdicts on the same fingerprint judged the same work."""
    head = git_lines(run.project, ["rev-parse", "--verify", "-q", "HEAD"])
    if head is None and git_lines(run.project, ["rev-parse", "--is-inside-work-tree"]) != ["true"]:
        return None
    h = hashlib.sha256("\n".join(head or ["(no commits)"]).encode())
    try:
        h.update(subprocess.run(["git", "-C", str(run.project), "diff", "HEAD" if head else "--cached"],
                                capture_output=True, timeout=60).stdout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for rel in git_lines(run.project, ["ls-files", "-o", "--exclude-standard"]) or []:
        h.update(rel.encode())
        try:
            h.update((run.project / rel).read_bytes())
        except OSError:
            pass
    return h.hexdigest()[:16]


HISTORY_CAP = 2000  # per run; the oldest results go first


def record_history(run: Run, cp: Dict[str, Any], results: List[Dict[str, Any]]) -> None:
    """Keep every gate result a stop counted, in order. `gated health` reads this across runs to
    find gates that cost more than they catch. Advisory checks aren't recorded."""
    tree = work_digest(run) if any(r["type"] == "judge" for r in results) else None
    history = run.state.setdefault("history", [])
    planned = "instructions" in cp
    for r in results:
        entry = {"at": r["at"], "cp": cp["id"], "gate": r["id"], "type": r["type"], "ok": r["ok"],
                 "summary": r["summary"][:200]}
        if planned:
            entry["planned"] = True
        for k in ("pending", "cached", "decision", "error"):
            if r.get(k):
                entry[k] = r[k]
        if isinstance(r.get("cost"), (int, float)):
            entry["cost"] = round(float(r["cost"]), 4)
        if r.get("notMet"):
            entry["notMet"] = r["notMet"]
        if r["type"] == "judge" and tree:
            entry["tree"] = hashlib.sha256(f"{tree}:{r.get('files', '')}".encode()).hexdigest()[:16]
        history.append(entry)
    del history[:-HISTORY_CAP]


def commit_checkpoint(run: Run, cp: Dict[str, Any]) -> str:
    git = ["git", "-C", str(run.project)]
    subprocess.run(git + ["add", "-A", "--", ".", ":(exclude).gated"], capture_output=True, text=True)
    if subprocess.run(git + ["diff", "--cached", "--quiet"]).returncode == 0:
        return "nothing to commit"
    msg = f"gated({run.id}): {cp['title']}\n\nCheckpoint '{cp['id']}' passed: " + ", ".join(g["id"] for g in cp["gates"])
    p = subprocess.run(git + ["commit", "-q", "-m", msg], capture_output=True, text=True)
    if p.returncode != 0:
        return f"commit failed: {(p.stdout + p.stderr).strip()[-300:]}"
    sha = subprocess.run(git + ["rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    return f"committed {sha}"


def advance(run: Run) -> str:
    cp = run.current()
    cp["status"] = "done"
    cp["doneAt"] = now()
    note = ""
    if run.workflow().get("commit"):
        note = f" ({commit_checkpoint(run, cp)})"
    run.state["current"] += 1
    if not run.state.get("planned", True) and run.state["current"] == run.state.get("planAt"):
        run.state["status"] = "planning"
        return f"Checkpoint '{cp['id']}' passed{note}. Next is the plan: run `gated step` for the planner's brief."
    nxt = run.current()
    if nxt is None:
        run.state["status"] = "done"
        write_report(run)
        return f"Checkpoint '{cp['id']}' passed{note}. The run is done. Report: {run.dir / 'report.md'}"
    activate(run)
    return f"Checkpoint '{cp['id']}' passed{note}. Start checkpoint '{nxt['id']}': run `gated step` for its brief."


def findings_path(run: Run, cp: Dict[str, Any]) -> Path:
    return run.dir / cp["id"] / "noticed.md"


def collect_findings(run: Run, cp: Dict[str, Any]) -> int:
    """Record what a step noticed but didn't fix. Each `- ` line is one finding, recorded once.
    Findings never change a run; they're the input for improving the workflow over time."""
    path = findings_path(run, cp)
    if not path.is_file():
        return 0
    lines = [ln.strip()[2:].strip() for ln in path.read_text().splitlines() if ln.strip().startswith("- ")]
    known = {f["text"] for f in run.state.setdefault("findings", [])}
    new = [t for t in lines if t and t not in known]
    for text in new:
        run.state["findings"].append({"checkpoint": cp["id"], "text": text, "at": now()})
    return len(new)


def splits_path(run: Run, cp: Dict[str, Any]) -> Path:
    return run.dir / cp["id"] / "splits.md"


def collect_splits(run: Run, cp: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Record stories a step split off, and check each one exists on GitHub. Returns the unverified ones.
    A split that doesn't resolve fails the checkpoint: otherwise an agent could drop scope by claiming it."""
    path = splits_path(run, cp)
    splits = run.state.setdefault("splits", [])
    if path.is_file():
        known = {x["url"] for x in splits}
        for line in path.read_text().splitlines():
            m = G.ISSUE_URL_RE.search(line) if line.strip().startswith("- ") else None
            if m and m.group(0) not in known:
                why = line.strip()[2:].replace(m.group(0), "").strip(" -:")
                splits.append({"checkpoint": cp["id"], "url": m.group(0), "why": why, "at": now(), "verified": False})
                known.add(m.group(0))
            elif line.strip().startswith("- ") and not m:
                splits.append({"checkpoint": cp["id"], "url": line.strip()[2:60], "why": "not a GitHub issue URL",
                               "at": now(), "verified": False, "bad": True})
    pending = []
    for x in splits:
        if x["checkpoint"] != cp["id"] or x["verified"]:
            continue
        if not x.get("bad"):
            ok, why = G.verify_issue(run, x["url"])
            x["verified"] = ok
            x["check"] = why
        if not x["verified"]:
            pending.append(x)
    return pending


def new_protected_files(run: Run) -> List[str]:
    """Files under the workflow's `protect` paths that didn't exist when the run started."""
    patterns = run.state.get("protect") or []
    if not patterns:
        return []
    locked = set(run.state.get("locks", {}))
    return [str(p) for p in protected_files(run.project, patterns) if str(p) not in locked]


def tracked_run_files(run: Run) -> List[str]:
    """Files of this run that git tracks. `.gated/.gitignore` keeps them out, but `git add -f` gets
    past it, and a run's to-do list or ledger then lands in the pull request (seen live)."""
    try:
        rel = run.dir.relative_to(run.project)
    except ValueError:
        return []
    return git_lines(run.project, ["ls-files", "--", str(rel)]) or []


def ask_decision(run: Run, gate_key: str, question: str, resume: str = "running") -> str:
    """A judge said the verdict hinges on a choice only a person can make. Ask it once, spend no
    attempt, and pass the answer to every later judge call in this run."""
    text = f"{gate_key}: {question}"
    n = 1 + len(run.state.get("answers", []))
    (run.dir / f"question-{n}.md").write_text(text + "\n")
    run.state["question"] = {"text": text[:2000], "at": now(), "resume": resume, "from": "judge"}
    run.state["status"] = "waiting"
    run.save()
    return (f"A judge needs a decision only the person can make: {question}\nPut it to them as written and "
            "end your turn. No attempt was spent; their answer goes to the judge on the next stop.")


def question_path(run: Run) -> Path:
    return run.dir / "question.md"


def take_question(run: Run) -> str:
    """The Stop hook calls this. A question the agent left for the person pauses the run without
    spending an attempt; the person's next message answers it and the run carries on."""
    path = question_path(run)
    text = path.read_text().strip() if path.is_file() else ""
    if not text or run.status not in ("running", "planning"):
        return ""
    n = 1 + len(run.state.get("answers", []))
    path.rename(run.dir / f"question-{n}.md")
    run.state["question"] = {"text": text[:2000], "at": now(), "resume": run.status}
    run.state["status"] = "waiting"
    run.save()
    return text


def answer_pending(run: Run, text: str) -> str:
    """A question tool (AskUserQuestion) answered the question the agent saved with `gated ask`
    inside the same turn, before any Stop. Record it and drop the pause that Stop would have
    started: the person has already spoken."""
    path = question_path(run)
    question = path.read_text().strip() if path.is_file() else ""
    if not question:
        return ""
    n = 1 + len(run.state.get("answers", []))
    path.rename(run.dir / f"question-{n}.md")
    run.state.setdefault("answers", []).append({"question": question[:2000], "answer": text.strip()[:2000],
                                                "at": now(), "from": "dialog"})
    run.save()
    return "gated: recorded the answer from the question dialog. The run continues; its gates still have to pass."


def answer(run: Run, text: str) -> str:
    q = run.state.pop("question")
    run.state.setdefault("answers", []).append({"question": q["text"], "answer": text.strip()[:2000], "at": now(),
                                                "from": q.get("from", "agent")})
    run.state["status"] = q["resume"]
    run.save()
    return "gated: recorded your answer. The run continues; its gates still have to pass."


def block(run: Run, reasons: List[str]) -> str:
    run.state["status"] = "blocked"
    run.state["blockedOn"] = reasons
    run.state.setdefault("blocks", []).append({"at": now(), "on": reasons})
    run.save()
    write_report(run)
    return (f"Run {run.id} is blocked: {', '.join(reasons)}. No gate was skipped. "
            f"Report: {run.dir / 'report.md'}. Only the person can give it fresh attempts.")


def check(run: Run, count_attempts: bool = False, move: bool = True) -> Tuple[bool, str]:
    """Rerun the current checkpoint's gates. Returns (may stop, message).

    Only the Stop hook calls this with move=True, from the harness's environment. When the
    agent runs `gated check`, move=False: it sees results but the run doesn't move and
    nothing is saved, so no environment trick in the agent's shell can pass a gate."""
    if move and run.tampered:
        return True, block(run, ["state.json was changed outside gated"])
    if run.status == "planning":
        path = plan_path(run)
        questions = plan_questions(read_json(path)) if path.is_file() else []
        if questions:
            return True, "The planner has questions. Ask the person:\n" + "\n".join(f"- {q}" for q in questions)
        if count_attempts and move:
            n = run.state["attempts"].get("plan", 0) + 1
            run.state["attempts"]["plan"] = n
            if n >= (run.workflow().get("attempts") or DEFAULT_ATTEMPTS):
                return True, block(run, ["plan"])
            run.save()
        if path.is_file():
            return False, "The plan is written but not submitted. Run `gated submit-plan`, then ask for approval."
        return False, f"Planning isn't finished. Write the plan to {path}, then run `gated submit-plan`."
    if run.status == "awaiting-approval":
        plan = run.workflow().get("plan", {})
        judge_approval = plan.get("approval", "human") == "judge" and not run.state.get("amendment")
        if not judge_approval:
            return True, "Waiting for the person to approve. They type `approve`, `reject` or `cancel run`."
        plan_cp = {"id": "plan"}
        # Code first: a plan whose own files fail a script never costs a judge call.
        code_results = [G.evaluate(run, plan_cp, g) for g in plan.get("gates", [])]
        code_failing = [r for r in code_results if not r["ok"]]
        if not move:
            if code_failing:
                return True, ("Submitted, but these plan gates fail, so the judge won't see it:\n"
                              + "\n".join(f"- {r['id']}: {r['summary']}" for r in code_failing))
            return True, "Submitted. When you end your turn the stop hook has the judge review the plan."
        for r in code_results:
            write_log(run, plan_cp, r)
        inputs = plan.get("inputs")
        if inputs is None:
            inputs = [{"file": run.state["plan"]}]
            acceptance = run.dir / "acceptance.md"
            if acceptance.is_file():
                inputs.append({"file": str(acceptance)})
        gate = {"id": "plan-review", "type": "judge", "rubric": plan["rubric"], "inputs": inputs}
        for key in ("maxChars", "timeout", "decisions"):
            if key in plan:
                gate[key] = plan[key]
        if code_failing:
            review = {"ok": False, "summary": "plan gates failed before the judge: "
                      + "; ".join(f"{r['id']}: {r['summary']}" for r in code_failing),
                      "log": "\n\n".join(f"{r['id']}:\n{r.get('log', '')[-1500:]}" for r in code_failing)}
        else:
            review = G.check_judge(run, plan_cp, gate)
            write_log(run, plan_cp, review)
        record_history(run, plan_cp, code_results + ([] if code_failing else [review]))
        if review.get("decision"):
            # Not a rejection: the plan stays submitted, and the judge sees the answer next stop.
            return True, ask_decision(run, "plan/plan-review", review["decision"], resume="awaiting-approval")
        errors = run.state.setdefault("judgeErrors", {})
        if review.get("error") and errors.get("plan", 0) + 1 < JUDGE_ERROR_RETRIES:
            # No verdict is not a rejection: keep the plan, spend no attempt, retry on the next stop.
            errors["plan"] = errors.get("plan", 0) + 1
            run.save()
            return False, (f"The judge gave no verdict ({review['summary']}). The plan stands and no attempt "
                           "was spent. End your turn again and the stop hook retries the judge.")
        errors.pop("plan", None)
        if review["ok"]:
            cp = approve_awaiting(run, review["summary"], by="judge")
            return False, f"The judge approved the plan. Next is '{cp['id']}': run `gated step` for its brief."
        n = run.state["attempts"].get("plan", 0) + 1
        run.state["attempts"]["plan"] = n
        if n >= (run.workflow().get("attempts") or DEFAULT_ATTEMPTS):
            return True, block(run, ["plan"])
        head = run.state["checkpoints"][: run.state.get("planAt") or 0]
        run.state["checkpoints"] = head
        run.state["planned"] = False
        run.state["current"] = run.state.get("planAt") or (0 if head else None)
        review_text = review["summary"] + "\n" + review.get("log", "")[-3000:]
        run.state["planReview"] = review_text
        run.state["status"] = "planning"
        run.save()
        who = "Its gates" if code_failing else "The judge"
        return False, (f"{who} rejected the plan:\n" + review_text
                       + "\nHand it to a NEW planning subagent with `gated step`, then `gated submit-plan` again.")
    if run.status == "waiting" and run.state.get("relock"):
        req = run.state["relock"]
        return True, ("Waiting for the person to approve unlocking "
                      + ", ".join(Path(f).name for f in req["files"]) + ": " + req["reason"])
    if run.status == "waiting" and run.state.get("question"):
        return True, "Waiting for the person to answer: " + run.state["question"]["text"]
    if run.status == "waiting":
        return True, "Waiting for the person: " + "; ".join(
            render(g["ask"], run.ctx()) for g in run.current()["gates"] if g["type"] == "human")
    if run.status in FINISHED_STATUSES:
        return True, f"{run.id} is {run.status}."
    if run.current() is None:
        run.state["status"] = "done"
        if move:
            run.save()
            write_report(run)
        return True, f"{run.id} is done."
    cp = run.current()
    unverified: List[Dict[str, Any]] = []
    if move:
        collect_findings(run, cp)
        unverified = collect_splits(run, cp)
    if move and run.state.get("unlocked"):
        lock(run, [Path(f) for f in run.state.pop("unlocked")])
    # A judge runs once per stop, where its verdict counts. An advisory check can't save a verdict
    # (the agent's shell could fake one), so judging there paid for a call nobody kept.
    deferred = [g for g in cp["gates"] if g["type"] == "judge"] if not move else []
    results = [G.evaluate(run, cp, g) for g in cp["gates"] if g not in deferred]
    if unverified:
        results.append({"id": "splits", "type": "splits", "ok": False, "at": now(), "log": "",
                        "summary": "split stories that don't resolve on GitHub: "
                        + "; ".join(f"{x['url']} ({x.get('check') or x['why']})" for x in unverified)})
    changed = G.changed_locks(run)
    added = new_protected_files(run)
    if changed or added:
        parts = (["locked files changed since they were locked: " + ", ".join(changed)] if changed else []) + (
            ["new files under the workflow's protected paths: " + ", ".join(added[:5])] if added else [])
        results.append({"id": "locks", "type": "locks", "ok": False, "at": now(), "log": "",
                        "summary": "; ".join(parts) + (". Protected paths change in their own change, outside a run"
                                                        if added else "")})
    tracked = tracked_run_files(run)
    if tracked:
        results.append({"id": "run-files", "type": "run-files", "ok": False, "at": now(), "log": "",
                        "summary": "this run's own files are tracked by git: " + ", ".join(tracked[:5])
                        + ". Remove them with `git rm --cached` in a new commit; never `git add -f` under .gated/"})
    gate_by_id = {g["id"]: g for g in cp["gates"]}
    checked_at = now()
    pending_since = run.state.setdefault("pendingSince", {}) if move else dict(run.state.get("pendingSince", {}))
    pending = []
    pending_keys = set()
    for r in results:
        if not r.get("pending"):
            continue
        key = f"{cp['id']}/{r['id']}"
        since = pending_since.get(key, checked_at)
        maximum = gate_by_id.get(r["id"], {}).get("pendingMax", 21600)
        try:
            start = datetime.datetime.strptime(since, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            current = datetime.datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
            expired = (current - start).total_seconds() > maximum
        except (TypeError, ValueError):
            since, expired = checked_at, False
        if expired:
            r.pop("pending", None)
            r["summary"] = f"still pending after {maximum}s: {r['summary']}"
        else:
            pending_since[key] = since
            pending_keys.add(key)
            r["since"] = since
            pending.append(r)
    if move:
        prefix = cp["id"] + "/"
        for key in list(pending_since):
            if key.startswith(prefix) and key not in pending_keys:
                del pending_since[key]
    for r in results:
        write_log(run, cp, r, advisory=not move)
    if move:
        record_history(run, cp, results)
    failing = [r for r in results if not r["ok"] and r["type"] != "human" and not r.get("pending")]
    human = [r for r in results if not r["ok"] and r["type"] == "human"]
    if not move:
        sections = []
        later = ("\n\nNot run here: " + ", ".join(g["id"] for g in deferred)
                 + " (judge). Judges run only when you end your turn, and each verdict counts.") if deferred else ""
        if failing:
            lines = "\n".join(f"- {r['id']} ({r['type']}): {r['summary']}" for r in failing)
            sections.append(f"Failing gates:\n{lines}")
        if pending:
            lines = "\n".join(f"- {r['id']} ({r['type']}): {r['summary']}" for r in pending)
            sections.append(f"Pending gates:\n{lines}")
        if sections:
            return not failing, (f"Checkpoint '{cp['id']}' isn't done. " + "\n\n".join(sections)
                                 + later + f"\n\nFull logs: {run.dir / cp['id'] / 'gates'}")
        if human:
            return True, (f"Checkpoint '{cp['id']}' passes its checks. End your turn: the stop hook confirms, "
                          "then the person is asked." + later)
        every = "Every other gate" if deferred else "Every gate"
        return True, (f"{every} on '{cp['id']}' passes. End your turn: the stop hook reruns the gates and moves "
                      "the run on. Only the hook can advance a run." + later)
    last_results = []
    for r in results:
        saved = {k: r[k] for k in ("id", "type", "ok", "summary", "at")}
        if r.get("pending"):
            saved.update({"pending": True, "since": r["since"]})
        last_results.append(saved)
    run.state["last"][cp["id"]] = last_results
    if pending and not failing:
        run.state["status"] = "running"
        run.save()
        details = "; ".join(f"{r['id']}: {r['summary']} (pending since {r['since']})" for r in pending)
        return True, ("Waiting on outside systems: " + details + ". No attempt spent. The next stop rechecks; "
                      "something has to give the agent a turn (the person, a scheduled `claude -p` resume, or a "
                      "background wait finishing).")
    if not failing and not human:
        message = advance(run)
        run.save()
        return run.status == "done", message
    if not failing:
        run.state["status"] = "waiting"
        run.save()
        asks = "; ".join(r["summary"] for r in human)
        return True, f"Checkpoint '{cp['id']}' passed its checks and is {asks}. The person approves by typing `approve`."
    run.state["status"] = "running"
    exhausted = []
    if count_attempts:
        gates_by_id = {g["id"]: g for g in cp["gates"]}
        errors = run.state.setdefault("judgeErrors", {})
        for r in results:
            if r["type"] == "judge" and not r.get("error"):
                errors.pop(f"{cp['id']}/{r['id']}", None)
        for r in failing:
            key = f"{cp['id']}/{r['id']}"
            if r.get("decision"):
                continue  # a question for the person, not a verdict on the work
            if r.get("error") and errors.get(key, 0) + 1 < JUDGE_ERROR_RETRIES:
                errors[key] = errors.get(key, 0) + 1  # no verdict says nothing about the work
                continue
            n = run.state["attempts"].get(key, 0) + 1
            run.state["attempts"][key] = n
            if n >= budget_for(run, gates_by_id.get(r["id"], {})):
                exhausted.append(r)
    lines = [f"- {r['id']} ({r['type']}): {r['summary']}" for r in failing]
    if exhausted:
        return True, block(run, [f"{cp['id']}/{r['id']}" for r in exhausted])
    decision = next((r for r in failing if r.get("decision")), None)
    if decision and move:
        others = [r for r in failing if r is not decision and not r.get("decision")]
        note = ("\nThese gates also failed and spent an attempt: " + ", ".join(r["id"] for r in others)) if others else ""
        return True, ask_decision(run, f"{cp['id']}/{decision['id']}", decision["decision"]) + note
    run.save()
    tries = ""
    if count_attempts:
        tries = "\nAttempts used: " + ", ".join(
            f"{r['id']} {run.state['attempts'].get(cp['id'] + '/' + r['id'], 0)}/{budget_for(run, {g['id']: g for g in cp['gates']}.get(r['id'], {}))}"
            + (" (no verdict, not counted)" if r.get("error") and cp['id'] + '/' + r['id'] not in run.state['attempts'] else "")
            for r in failing)
    tail = "\n\n".join(f"{r['id']}:\n{r['log'][-1200:]}" for r in failing if r.get("log"))
    return False, (f"Checkpoint '{cp['id']}' isn't done. Failing gates:\n" + "\n".join(lines) + tries
                   + (f"\n\nOutput:\n{tail}" if tail else "")
                   + f"\n\nFull logs: {run.dir / cp['id'] / 'gates'}")


def request_relock(run: Run, paths: List[str], reason: str) -> str:
    """Ask the person to unlock tests the spec proved wrong. Workflow files can never be relocked."""
    if run.status != "running":
        raise GatedError(f"{run.id} is {run.status}; a relock needs a running checkpoint")
    if not reason.strip():
        raise GatedError("say why the locked tests are wrong: `gated relock <file> --reason \"...\"`")
    red_files = {f for entry in run.state.get("red", {}).values() for f in entry.get("files", [])}
    red_files |= set(run.state.get("planLocked", []))
    files = []
    for p in paths:
        full = str((run.project / p).resolve()) if not Path(p).is_absolute() else str(Path(p).resolve())
        if full not in red_files:
            raise GatedError(f"{p} can't be relocked: only tests locked by `gated red` and files the plan "
                             "froze can, never workflow files")
        files.append(full)
    run.state["relock"] = {"files": files, "reason": reason.strip()[:1000], "at": now()}
    run.state["status"] = "waiting"
    run.save()
    return ("Relock requested. Put the reason to the person and end your turn: the run waits without spending "
            "an attempt. If they type `approve`, edit the tests, commit the change on its own, and the next stop "
            "locks them again. `reject` keeps them as they are.")


def red(run: Run, gate_id: str) -> str:
    """Record that a red-first gate's tests fail before the code exists, then lock them."""
    cp = run.current()
    if run.status != "running" or cp is None:
        raise GatedError(f"{run.id} is {run.status}; `gated red` needs a running checkpoint")
    gate = next((g for g in cp["gates"] if g["id"] == gate_id), None)
    if gate is None or gate["type"] != "red-first":
        names = [g["id"] for g in cp["gates"] if g["type"] == "red-first"]
        raise GatedError(f"no red-first gate '{gate_id}' in '{cp['id']}'. Red-first gates here: {', '.join(names) or 'none'}")
    key = f"{cp['id']}/{gate_id}"
    if key in run.state["red"]:
        raise GatedError(f"'{gate_id}' is already locked; its tests can't change now")
    files: List[Path] = []
    for pattern in gate["lock"]:
        files += [p for p in run.project.glob(render(pattern, run.ctx(cp))) if p.is_file()]
    if not files:
        raise GatedError(f"the lock globs {gate['lock']} match no files. Write the tests first.")
    cmd = render(gate["run"], run.ctx(cp))
    code, out = G.sh(cmd, run.project, gate.get("timeout", 600), env=G.gate_env(run, cp))
    if code in (None, 126, 127):
        raise GatedError(f"`{cmd}` didn't run (exit {code}), which isn't the same as failing. Fix the command, "
                         f"then run `gated red {gate_id}` again.\n\n{out[-1500:]}")
    if code == 0:
        raise GatedError(f"`{cmd}` already passes, so these tests don't test anything new. "
                         f"Make them fail against the current code, then run `gated red {gate_id}` again.\n\n{out[-1500:]}")
    lock(run, files)
    run.state["red"][key] = {"at": now(), "exit": code, "files": [str(p) for p in files]}
    run.save()
    rel = ", ".join(str(p.relative_to(run.project)) for p in files)
    return f"Red recorded: `{cmd}` exited {code}. Locked {len(files)} test file(s): {rel}. Now make them pass without editing them."


def step_brief(run: Run) -> str:
    """The brief an orchestrator hands a subagent for the current step."""
    wf = run.workflow()
    ctx = run.ctx()
    learn = run.workflow_dir / "learnings.md"
    learnings = learn.read_text().strip() if learn.is_file() else ""
    inputs = "\n".join(f"- {k}: {v}" for k, v in run.state["inputs"].items()) or "- none"
    if run.status == "planning":
        body = render((run.workflow_dir / wf["plan"]["step"]).read_text(), ctx)
        out = [f"# {run.id}: plan", "", body, "", "## Inputs", inputs, "",
               "## Output",
               f"Write the plan to {plan_path(run)} as JSON: "
               '{"checkpoints": [{"id": "kebab-id", "title": "...", "instructions": "...", "gates": [...]}]}.',
               "Order checkpoints from the smallest foundation to the most complex. Every checkpoint needs at "
               "least one gate that code can check. See references/writing-gates.md in the gated skill.",
               "Don't run `gated submit-plan`; the orchestrator does."]
        plan_gates = wf["plan"].get("gates", [])
        if plan_gates:
            out += ["", "## Plan gates (code checks them before the judge sees the plan)"]
            out += [f"- {g['id']}: `{render(g['run'], ctx)}` must exit 0" if g["type"] == "command"
                    else f"- {g['id']}: {render(g['path'], ctx)} must exist" for g in plan_gates]
        if run.state.get("planReview"):
            out += ["", "## The last plan was rejected", run.state["planReview"]]
        if wf.get("checkpoints"):
            out += ["", "These fixed checkpoints run after yours, so don't duplicate them: "
                    + ", ".join(c["id"] for c in wf["checkpoints"])]
    else:
        cp = run.current()
        if cp is None:
            return f"{run.id} is {run.status}; there's no current step."
        body = render(Path(cp["step"]).read_text(), ctx) if cp.get("step") else render(cp["instructions"], ctx)
        gate_lines = []
        for g in cp["gates"]:
            if g["type"] == "command":
                gate_lines.append(f"- {g['id']}: `{render(g['run'], ctx)}` must exit 0")
            elif g["type"] == "file":
                extra = [k for k in ("json", "headings", "contains", "links") if g.get(k)]
                gate_lines.append(f"- {g['id']}: {render(g['path'], ctx)} must exist" + (f" and pass: {', '.join(extra)}" if extra else ""))
            elif g["type"] == "red-first":
                gate_lines.append(f"- {g['id']}: write tests matching {g['lock']} first. When they fail, run "
                                  f"`gated red {g['id']}` to lock them. Then `{render(g['run'], ctx)}` must pass without editing them.")
            elif g["type"] == "todos":
                gate_lines.append(f"- todos: before anything else, write {G.todo_path(run, cp)} as `- [ ] item` lines. "
                                  "Check each off as `- [x]`. An item you drop is `- [ ] ~~item~~ reason`.")
            elif g["type"] == "judge":
                gate_lines.append(f"- {g['id']}: an independent reviewer grades the work against {g['rubric']}")
            elif g["type"] == "human":
                gate_lines.append(f"- {g['id']}: the person approves: {render(g['ask'], ctx)}")
        out = [f"# {run.id}: checkpoint '{cp['id']}' ({run.state['current'] + 1} of {len(run.state['checkpoints'])})", "",
               body, "", "## Inputs", inputs, "", "## Gates (checked by code, not by you)", *gate_lines]
        last = [r for r in run.state.get("last", {}).get(cp["id"], []) if not r["ok"]]
        if last:
            out += ["", "## Last check failed on", *[f"- {r['id']}: {r['summary']}" for r in last]]
    if learnings:
        out += ["", "## Learnings from earlier runs (these override your defaults)", learnings]
    brief_cp = run.current() if run.status != "planning" else None
    wanted = next((g["skills"] for g in (brief_cp or {}).get("gates", []) if g["type"] == "skills"), None) \
        if brief_cp else wf.get("plan", {}).get("skills")
    if wanted:
        out += ["", "## Skills to use",
                "Load each of these before starting, and follow it: " + ", ".join(wanted) + ". In Claude Code use the "
                "Skill tool; in Codex read the skill's SKILL.md. In Claude Code a gate checks that you loaded them."]
    if brief_cp:
        story = wf.get("storySkill")
        how = f"the `{story}` skill" if story else "`gh issue create`"
        out += ["", "## Too big for this run?",
                f"If part of the work belongs in its own story, create that story yourself now with {how}, while you "
                "have the context. Give it the problem, acceptance criteria, what this run already did, and a link "
                f"back to this run. Then add its URL to {splits_path(run, brief_cp)} as `- <issue URL> why`. The hook "
                "checks each URL exists on GitHub, and one that doesn't fails the checkpoint."]
    if run.status != "planning" and run.current():
        out += ["", "## Found something you won't fix?",
                f"Add it to {findings_path(run, run.current())} as a `- ` line: the file, what's wrong, and why it's "
                "out of scope here. It isn't a gate and doesn't fail anything. It's how the workflow improves."]
    out += ["", f"Working folder for notes and evidence: {run.dir}"]
    return "\n".join(out)


def write_report(run: Run) -> Path:
    s = run.state
    lines = [f"# {s['id']}", "", f"- Status: **{s['status']}**", f"- Workflow: {s['workflow']}",
             f"- Started: {s['createdAt']}", f"- Updated: {now()}"]
    if s.get("inputs"):
        lines.append("- Inputs: " + ", ".join(f"{k}={v}" for k, v in s["inputs"].items()))
    if s.get("blockedOn"):
        lines += ["", "## Blocked", "", "These gates used every attempt. None was skipped. Fix what they report, "
                  "then `gated resume` to continue with fresh attempts.", ""]
        lines += [f"- {k}" for k in s["blockedOn"]]
    if s.get("pendingSince"):
        lines += ["", "## Pending outside systems", ""]
        for key, since in sorted(s["pendingSince"].items()):
            cp_id, gate_id = key.split("/", 1)
            last = next((r for r in s.get("last", {}).get(cp_id, []) if r["id"] == gate_id), None)
            summary = f": {last['summary']}" if last else ""
            lines.append(f"- {key} since {since}{summary}")
    lines += ["", "## Checkpoints", ""]
    for cp in s["checkpoints"]:
        lines.append(f"### {cp['title']} [{cp['id']}]: {cp['status']}")
        for r in s.get("last", {}).get(cp["id"], []):
            used = s["attempts"].get(f"{cp['id']}/{r['id']}")
            verdict = "PENDING" if r.get("pending") else ("PASS" if r["ok"] else "FAIL")
            lines.append(f"- {verdict} {r['id']} ({r['type']}): {r['summary']}" + (f"  [attempts: {used}]" if used else ""))
        lines.append("")
    if s.get("splits"):
        lines += ["## Split into new stories", ""] + [
            f"- [{x['checkpoint']}] {x['url']} {x['why']}" + ("" if x["verified"] else "  (NOT VERIFIED)") for x in s["splits"]] + [""]
    if s.get("findings"):
        lines += ["## Found, not fixed", ""] + [f"- [{f['checkpoint']}] {f['text']}" for f in s["findings"]] + [""]
    if s.get("answers"):
        lines += ["## Questions asked mid-run", ""] + [f"- Q: {a['question'][:200]}\n  A: {a['answer'][:200]}" for a in s["answers"]] + [""]
    if s.get("relocks"):
        lines += ["## Relocked tests", ""] + [
            f"- {', '.join(Path(f).name for f in r['files'])}: {'approved' if r.get('approvedAt') else 'refused'} "
            f"by {r.get('by', 'person')}. Reason: {r['reason'][:300]}" for r in s["relocks"]
        ] + [""]
    if s.get("approvals"):
        lines += ["## Approvals", ""] + [
            f"- {a['gate']} at {a['at']} by {a.get('by', 'person')}: \"{a['text']}\"" for a in s["approvals"]
        ] + [""]
    path = run.dir / "report.md"
    path.write_text("\n".join(lines))
    return path


def resume(run: Run) -> str:
    """Hand a run to this session. A blocked run only gets fresh attempts when the person approves."""
    run.state["owner"] = None
    run.state["claim"] = secrets.token_hex(8)
    if run.status == "blocked":
        run.state["resumeRequested"] = True
    run.save()
    record_claim(run.state["claim"], run.dir)
    return run.state["claim"]


def learn(wdir: Path, text: str) -> Path:
    path = wdir / "learnings.md"
    existing = path.read_text() if path.is_file() else "# Learnings\n\nFeedback from past runs. Every step reads this.\n"
    stamp = datetime.date.today().isoformat()
    path.write_text(existing.rstrip("\n") + f"\n\n- {stamp}: {text.strip()}\n")
    return path


def customize(project: Path, name: str, to: str) -> Path:
    """Copy a workflow into this project (or ~/.claude/workflows) so a team can make it its own."""
    import shutil

    from .core import SKILL_DIR

    src = find_workflow(name, project)
    base = project / ".claude" / "workflows" if to == "project" else Path.home() / ".claude" / "workflows"
    dest = base / src.name
    if dest.resolve() == src.resolve():
        raise GatedError(f"{src} is already the {to} copy. Edit it directly.")
    if dest.exists():
        raise GatedError(f"{dest} already exists. Edit it, or remove it to copy again.")
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("learnings.md"))
    origin = "built-in" if SKILL_DIR in src.parents else ("user" if Path.home() / ".claude" / "workflows" in src.parents else "project")
    data = read_json(dest / "workflow.json")
    data["basedOn"] = f"{name} ({origin})"
    write_json(dest / "workflow.json", data)
    return dest
