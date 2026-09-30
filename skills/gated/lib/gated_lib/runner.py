"""Run lifecycle: start, plan approval, checks, advancing, reports. The only writer of run state."""

from __future__ import annotations

import datetime
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
    ignore_runs,
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


def definition_files(wdir: Path) -> List[Path]:
    """Every file in the workflow folder except learnings.md: steps, rubrics and check scripts.
    Locked for the whole run, so no gate can be loosened mid-run."""
    return sorted(p for p in wdir.rglob("*") if p.is_file() and p.name != "learnings.md" and ".git" not in p.parts)


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
    lock(run, definition_files(wdir))
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
    items = materialize(wf, run.workflow_dir, cps, planned=True)
    tail = materialize(wf, run.workflow_dir, wf.get("checkpoints", []), planned=False)
    run.state["checkpoints"] = head + items + tail
    run.state["planned"] = True
    run.state["plan"] = str(snapshot(run, path, "plan"))
    run.state["status"] = "awaiting-approval"
    run.save()
    questions = plan_questions(read_json(path))
    asked = ("\n\nOpen questions from the planner:\n" + "\n".join(f"- {q}" for q in questions)) if questions else ""
    return plan_summary(run.state["checkpoints"]) + asked


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


def approve(run: Run, text: str) -> str:
    """Called by the UserPromptSubmit hook with what the person typed. Records, never checks:
    the next Stop reruns the gates, so the person's prompt never waits on a test suite."""
    stamp = {"at": now(), "text": text.strip()[:200]}
    if run.status == "awaiting-approval":
        amendment = run.state.pop("amendment", None)
        run.state["approvals"].append({**stamp, "gate": "amendment" if amendment else "plan"})
        run.state["status"] = "running"
        if run.state.get("current") is None:
            run.state["current"] = run.state.get("planAt") or 0
        activate(run)
        run.save()
        cp = run.current()
        return f"gated: you approved {'the new checkpoints' if amendment else 'the plan'} for {run.id}. Next is '{cp['id']}'; the agent runs `gated step` for its brief."
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
        for key in list(run.state["attempts"]):
            if key.startswith(f"{cp['id']}/") or key == "plan":
                del run.state["attempts"][key]
        for k in ("blockedOn", "resumeRequested"):
            run.state.pop(k, None)
        run.state["approvals"].append({**stamp, "gate": "resume"})
        run.state["status"] = "planning" if not run.state["checkpoints"] else "running"
        run.save()
        return f"gated: you gave {run.id} fresh attempts on '{cp['id'] if cp else 'plan'}'."
    return ""


def reject(run: Run, text: str) -> str:
    """The person turned down an amendment. Put the run back how it was."""
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


def write_log(run: Run, cp: Dict[str, Any], r: Dict[str, Any]) -> None:
    d = run.dir / cp["id"] / "gates"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{r['id']}.log").write_text(f"{r['at']}  {'PASS' if r['ok'] else 'FAIL'}  {r['summary']}\n\n{r['log']}")


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


def answer(run: Run, text: str) -> str:
    q = run.state.pop("question")
    run.state.setdefault("answers", []).append({"question": q["text"], "answer": text.strip()[:2000], "at": now()})
    run.state["status"] = q["resume"]
    run.save()
    return "gated: recorded your answer. The run continues; its gates still have to pass."


def block(run: Run, reasons: List[str]) -> str:
    run.state["status"] = "blocked"
    run.state["blockedOn"] = reasons
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
        return True, "Waiting for the person to approve. They type `approve`, `reject` or `cancel run`."
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
    results = [G.evaluate(run, cp, g) for g in cp["gates"]]
    if unverified:
        results.append({"id": "splits", "type": "splits", "ok": False, "at": now(), "log": "",
                        "summary": "split stories that don't resolve on GitHub: "
                        + "; ".join(f"{x['url']} ({x.get('check') or x['why']})" for x in unverified)})
    changed = G.changed_locks(run)
    if changed:
        results.append({"id": "locks", "type": "locks", "ok": False, "at": now(), "log": "",
                        "summary": "locked files changed since they were locked: " + ", ".join(changed)})
    for r in results:
        write_log(run, cp, r)
    failing = [r for r in results if not r["ok"] and r["type"] != "human"]
    human = [r for r in results if not r["ok"] and r["type"] == "human"]
    if not move:
        if failing:
            lines = "\n".join(f"- {r['id']} ({r['type']}): {r['summary']}" for r in failing)
            return False, f"Checkpoint '{cp['id']}' isn't done. Failing gates:\n{lines}\n\nFull logs: {run.dir / cp['id'] / 'gates'}"
        if human:
            return True, f"Checkpoint '{cp['id']}' passes its checks. End your turn: the stop hook confirms, then the person is asked."
        return True, (f"Every gate on '{cp['id']}' passes. End your turn: the stop hook reruns the gates and moves "
                      "the run on. Only the hook can advance a run.")
    run.state["last"][cp["id"]] = [{k: r[k] for k in ("id", "type", "ok", "summary", "at")} for r in results]
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
        for r in failing:
            key = f"{cp['id']}/{r['id']}"
            n = run.state["attempts"].get(key, 0) + 1
            run.state["attempts"][key] = n
            if n >= budget_for(run, gates_by_id.get(r["id"], {})):
                exhausted.append(r)
    lines = [f"- {r['id']} ({r['type']}): {r['summary']}" for r in failing]
    if exhausted:
        return True, block(run, [f"{cp['id']}/{r['id']}" for r in exhausted])
    run.save()
    tries = ""
    if count_attempts:
        tries = "\nAttempts used: " + ", ".join(
            f"{r['id']} {run.state['attempts'][cp['id'] + '/' + r['id']]}/{budget_for(run, {g['id']: g for g in cp['gates']}.get(r['id'], {}))}"
            for r in failing)
    tail = "\n\n".join(f"{r['id']}:\n{r['log'][-1200:]}" for r in failing if r.get("log"))
    return False, (f"Checkpoint '{cp['id']}' isn't done. Failing gates:\n" + "\n".join(lines) + tries
                   + (f"\n\nOutput:\n{tail}" if tail else "")
                   + f"\n\nFull logs: {run.dir / cp['id'] / 'gates'}")


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
    lines += ["", "## Checkpoints", ""]
    for cp in s["checkpoints"]:
        lines.append(f"### {cp['title']} [{cp['id']}]: {cp['status']}")
        for r in s.get("last", {}).get(cp["id"], []):
            used = s["attempts"].get(f"{cp['id']}/{r['id']}")
            lines.append(f"- {'PASS' if r['ok'] else 'FAIL'} {r['id']} ({r['type']}): {r['summary']}" + (f"  [attempts: {used}]" if used else ""))
        lines.append("")
    if s.get("splits"):
        lines += ["## Split into new stories", ""] + [
            f"- [{x['checkpoint']}] {x['url']} {x['why']}" + ("" if x["verified"] else "  (NOT VERIFIED)") for x in s["splits"]] + [""]
    if s.get("findings"):
        lines += ["## Found, not fixed", ""] + [f"- [{f['checkpoint']}] {f['text']}" for f in s["findings"]] + [""]
    if s.get("answers"):
        lines += ["## Questions asked mid-run", ""] + [f"- Q: {a['question'][:200]}\n  A: {a['answer'][:200]}" for a in s["answers"]] + [""]
    if s.get("approvals"):
        lines += ["## Approvals", ""] + [f"- {a['gate']} at {a['at']}: \"{a['text']}\"" for a in s["approvals"]] + [""]
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
