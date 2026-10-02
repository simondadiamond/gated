"""Command line for gated. The agent runs these; people rarely need to."""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path
from typing import List, Optional

from . import hooks, install, runner
from .core import (
    GatedError,
    available_workflows,
    find_project,
    find_workflow,
    lint_workflow,
    list_runs,
    read_json,
    locked,
    resolve_run,
)


def cmd_start(a: argparse.Namespace) -> str:
    run = runner.start(find_project(), a.workflow, a.inputs)
    lines = [f"Started {run.id} ({run.status}). Run folder: {run.dir}", f"gated-claim:{run.state['claim']}", ""]
    if run.status == "planning":
        lines.append("Next: `gated step` prints the planner's brief. Hand it to a planning subagent.")
    else:
        lines.append("Next: `gated step` prints the brief for the first checkpoint. Hand it to a subagent.")
    return "\n".join(lines)


def since_cutoff(text: Optional[str]) -> str:
    """'30d', '2w' or '12h' to an ISO timestamp; timestamps compare as strings."""
    if not text:
        return ""
    m = re.fullmatch(r"(\d+)([hdw])", text.strip())
    if not m:
        raise GatedError(f"--since takes a number and h, d or w, like 30d; got '{text}'")
    hours = int(m.group(1)) * {"h": 1, "d": 24, "w": 168}[m.group(2)]
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_findings(a: argparse.Namespace) -> str:
    cutoff = since_cutoff(a.since)
    rows = []
    for r in list_runs(find_project()):
        for f in r.state.get("findings", []):
            if f["at"] >= cutoff and (not a.workflow or r.state["workflow"] == a.workflow):
                rows.append((r.state["workflow"], f["at"], r.id, f["checkpoint"], f["text"]))
    if not rows:
        return "No findings" + (f" since {a.since}" if a.since else "") + "."
    out: List[str] = []
    for wf in sorted({r[0] for r in rows}):
        out += ([""] if out else []) + [f"{wf}:"]
        out += [f"  {at[:10]}  {rid}/{cp}  {text}" for w, at, rid, cp, text in sorted(rows, key=lambda x: x[1]) if w == wf]
    return "\n".join(out)


def cmd_status(a: argparse.Namespace) -> str:
    project = find_project()
    cutoff = since_cutoff(a.since)
    runs = [r for r in list_runs(project) if r.state.get("updatedAt", "") >= cutoff]
    if a.run:
        runs = [resolve_run(project, a.run, finished=True)]
    if not runs:
        wfs = available_workflows(project)
        listing = "\n".join(f"  {w['name']:<20} {w['description']}" for w in wfs) or "  (none found)"
        return f"No runs in {project}.\n\nWorkflows you can start:\n{listing}"
    out = []
    for r in runs:
        cp = r.current()
        where = f"at '{cp['id']}' ({r.state['current'] + 1}/{len(r.state['checkpoints'])})" if cp else ""
        owner = (r.state.get("owner") or "unclaimed")[:8]
        out.append(f"{r.id:<24} {r.status:<18} {where:<28} owner {owner}  updated {r.state.get('updatedAt', '')}")
        for key, since in sorted(r.state.get("pendingSince", {}).items()):
            out.append(f"  pending {key} since {since}")
    return "\n".join(out)


def cmd_step(a: argparse.Namespace) -> str:
    return runner.step_brief(resolve_run(find_project(), a.run))


def cmd_check(a: argparse.Namespace) -> str:
    run = resolve_run(find_project(), a.run)
    may_stop, message = runner.check(run, move=False)
    a.exit_code = 0 if may_stop else 1
    return message


def cmd_red(a: argparse.Namespace) -> str:
    with locked(resolve_run(find_project(), a.run).dir) as run:
        return runner.red(run, a.gate)


def cmd_submit_plan(a: argparse.Namespace) -> str:
    with locked(resolve_run(find_project(), a.run).dir) as run:
        summary = runner.submit_plan(run, Path(a.file) if a.file else None)
    return f"Plan for {run.id}:\n\n{summary}\n\nShow this to the person. They approve by typing `approve`."


def cmd_amend(a: argparse.Namespace) -> str:
    with locked(resolve_run(find_project(), a.run, finished=True).dir) as run:
        summary = runner.amend(run, Path(a.file))
    return f"New checkpoints for {run.id}:\n\n{summary}\n\nShow this to the person. They approve by typing `approve`."


def cmd_resume(a: argparse.Namespace) -> str:
    with locked(resolve_run(find_project(), a.run, finished=True).dir) as run:
        token = runner.resume(run)
    extra = ("\nIt's blocked. Show the person the report and ask them to type `approve` for fresh attempts."
             if run.status == "blocked" else "\n\nNext: `gated step`.")
    return f"Resuming {run.id} ({run.status}).\ngated-claim:{token}{extra}"


def cmd_report(a: argparse.Namespace) -> str:
    run = resolve_run(find_project(), a.run, finished=True)
    return runner.write_report(run).read_text()


def cmd_learn(a: argparse.Namespace) -> str:
    project = find_project()
    wdir = find_workflow(a.workflow, project) if a.workflow else resolve_run(project, a.run, finished=True).workflow_dir
    return f"Added to {runner.learn(wdir, a.text)}"


def cmd_lint(a: argparse.Namespace) -> str:
    wdir = find_workflow(a.workflow, find_project())
    errors = lint_workflow(read_json(wdir / "workflow.json"), wdir)
    if errors:
        a.exit_code = 1
        return f"{wdir}:\n  - " + "\n  - ".join(errors)
    return f"{wdir}: ok"


def cmd_list(a: argparse.Namespace) -> str:
    wfs = available_workflows(find_project())
    return "\n".join(f"{w['name']:<20} {w['description']}\n{'':<20} {w['path']}" for w in wfs) or "No workflows found."


def cmd_customize(a: argparse.Namespace) -> str:
    dest = runner.customize(find_project(), a.workflow, a.to)
    return (f"Copied to {dest}. This copy now wins over the original. Next: ask which skills the team uses at each "
            "step and add them as \"skills\" in workflow.json, then `gated lint " + a.workflow + "`.")


def cmd_ask(a: argparse.Namespace) -> str:
    run = resolve_run(find_project(), a.run)
    runner.question_path(run).write_text(a.question.strip() + "\n")
    return ("Question saved. Put it to the person and end your turn: the run waits for their answer "
            "without spending an attempt.")


def cmd_relock(a: argparse.Namespace) -> str:
    with locked(resolve_run(find_project(), a.run).dir) as run:
        return runner.request_relock(run, a.files, a.reason or "")


def cmd_install(a: argparse.Namespace) -> str:
    path = install.install(a.harness)
    note = f"Added gated's hooks to {path}. They do nothing in sessions that don't own a run."
    if a.harness == "codex":
        note += ("\nCodex skips hooks until you trust them: open Codex, run /hooks, and trust the four gated entries. "
                 "Register them only here, not also in a project's .codex/hooks.json, or every check runs twice.")
    return note + "\nRestart open sessions to load them."


def cmd_uninstall(a: argparse.Namespace) -> str:
    return f"Removed gated's hooks from {install.uninstall(a.harness)}."


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gated", description="Run workflows as checkpoints with gates that code checks.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, fn, help_text: str, run_arg: bool = True) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_text)
        sp.set_defaults(fn=fn)
        if run_arg:
            sp.add_argument("--run", help="run id; defaults to this session's run")
        return sp

    sp = add("start", cmd_start, "start a run of a workflow", run_arg=False)
    sp.add_argument("workflow")
    sp.add_argument("inputs", nargs="*", help="key=value")
    add("step", cmd_step, "print the brief for the current step")
    add("check", cmd_check, "rerun the current checkpoint's gates")
    sp = add("red", cmd_red, "record failing tests for a red-first gate and lock them")
    sp.add_argument("gate")
    sp = add("submit-plan", cmd_submit_plan, "validate the plan and ask for approval")
    sp.add_argument("file", nargs="?")
    sp = add("amend", cmd_amend, "add checkpoints to a run; needs approval")
    sp.add_argument("file")
    sp = add("status", cmd_status, "list runs")
    sp.add_argument("--since", help="only runs updated in the last 12h, 30d, 2w...")
    sp = add("findings", cmd_findings, "list what steps found but didn't fix, across runs", run_arg=False)
    sp.add_argument("--since", help="12h, 30d, 2w...")
    sp.add_argument("--workflow")
    add("resume", cmd_resume, "hand a run to this session")
    add("report", cmd_report, "write and print the run report")
    sp = add("learn", cmd_learn, "append feedback to a workflow's learnings.md")
    sp.add_argument("text")
    sp.add_argument("--workflow")
    sp = add("lint", cmd_lint, "check a workflow definition", run_arg=False)
    sp.add_argument("workflow")
    add("list", cmd_list, "list workflows you can start", run_arg=False)
    sp = add("relock", cmd_relock, "ask the person to unlock red-first tests the spec proved wrong")
    sp.add_argument("files", nargs="+")
    sp.add_argument("--reason", required=True)
    sp = add("ask", cmd_ask, "pause the run to ask the person something, without spending an attempt")
    sp.add_argument("question")
    sp = add("customize", cmd_customize, "copy a workflow into this project or ~/.claude/workflows to make it your own", run_arg=False)
    sp.add_argument("workflow")
    sp.add_argument("--to", choices=["project", "user"], default="project")
    sp = add("install", cmd_install, "register the hooks for a copied skill (claude) or for codex", run_arg=False)
    sp.add_argument("harness", choices=["claude", "codex"])
    sp = add("uninstall", cmd_uninstall, "remove gated's hooks", run_arg=False)
    sp.add_argument("harness", choices=["claude", "codex"])
    sp = sub.add_parser("hook", help="entry point for harness hooks")
    sp.add_argument("kind", choices=sorted(hooks.HANDLERS))
    return p


def main(argv: Optional[List[str]] = None) -> int:
    a = parser().parse_args(argv)
    if a.cmd == "hook":
        return hooks.main(a.kind)
    a.exit_code = 0
    try:
        print(a.fn(a))
    except GatedError as e:
        print(f"gated: {e}", file=sys.stderr)
        return 1
    return a.exit_code
