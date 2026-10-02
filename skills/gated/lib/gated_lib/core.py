"""Paths, state, templating and workflow loading shared by every gated command."""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parents[2]
DEFAULT_ATTEMPTS = 5
GATE_TYPES = ("command", "file", "red-first", "todos", "judge", "human")
AUTO_GATES = ("todos", "fresh-context", "skills", "splits")
ACTIVE_STATUSES = ("planning", "awaiting-approval", "running", "waiting")
FINISHED_STATUSES = ("done", "blocked", "cancelled")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")
# Claude Code refuses a subagent Write to a file named like this (found live 2026-09-30), so a file
# gate on such a name can never pass there.
SUBAGENT_BLOCKED_NAME_RE = re.compile(r"^(REPORT|SUMMARY|FINDINGS|ANALYSIS).*\.md$", re.I)


class GatedError(Exception):
    """A problem the person or agent can fix; printed without a traceback."""


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except json.JSONDecodeError as e:
        raise GatedError(f"{path} is not valid JSON: {e}")


def write_json(path: Path, data: Any) -> None:
    """Write atomically, so a crash never leaves half a state file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(path))


def sha256_file(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def find_project(start: Optional[str] = None) -> Path:
    """The nearest directory holding .gated or .git, else the start directory."""
    here = Path(start or os.getcwd()).resolve()
    for d in [here, *here.parents]:
        if (d / ".gated").is_dir() or (d / ".git").exists():
            return d
    return here


def render(text: str, ctx: Dict[str, Any]) -> str:
    """Replace {{run}}, {{project}}, {{workflow}}, {{checkpoint}} and {{input.<name>}}."""

    def sub(m: "re.Match[str]") -> str:
        key = m.group(1)
        if key.startswith("input."):
            name = key[len("input."):]
            inputs = ctx.get("input", {})
            if name not in inputs:
                raise GatedError(f"unknown input {{{{{key}}}}}; this run has: {', '.join(sorted(inputs)) or 'none'}")
            return str(inputs[name])
        if key in ctx and not isinstance(ctx[key], dict):
            return str(ctx[key])
        raise GatedError(f"unknown template {{{{{key}}}}}")

    return TEMPLATE_RE.sub(sub, text)


# ---------------------------------------------------------------- workflows


def workflow_dirs(project: Path) -> List[Path]:
    return [
        project / ".claude" / "workflows",
        Path.home() / ".claude" / "workflows",
        SKILL_DIR / "workflows",
    ]


def find_workflow(name: str, project: Path) -> Path:
    direct = Path(name).expanduser()
    if (direct / "workflow.json").is_file():
        return direct.resolve()
    if not ID_RE.match(name):
        raise GatedError(f"'{name}' is not a workflow name or a folder holding workflow.json")
    for base in workflow_dirs(project):
        if (base / name / "workflow.json").is_file():
            return (base / name).resolve()
    looked = "\n  ".join(str(b / name) for b in workflow_dirs(project))
    raise GatedError(f"no workflow named '{name}'. Looked in:\n  {looked}")


def available_workflows(project: Path) -> List[Dict[str, str]]:
    seen: Dict[str, Dict[str, str]] = {}
    for base in workflow_dirs(project):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.name in seen or not (d / "workflow.json").is_file():
                continue
            try:
                desc = read_json(d / "workflow.json").get("description", "")
            except GatedError:
                desc = "(workflow.json is not valid JSON)"
            seen[d.name] = {"name": d.name, "description": desc, "path": str(d)}
    return list(seen.values())


def rubric_is_path(value: str) -> bool:
    """Rubric prose is inline; names that look like files must resolve in the workflow."""
    return "\n" not in value and ("/" in value or Path(value).suffix.lower() in (".md", ".txt"))


def lint_judge_options(value: Dict[str, Any], where: str, wdir: Optional[Path], errors: List[str]) -> None:
    rubric = value.get("rubric")
    if "rubric" in value and not (isinstance(rubric, str) and rubric.strip()):
        errors.append(f"{where}: rubric must be non-empty text or a file in the workflow folder")
    elif wdir is not None and isinstance(rubric, str) and rubric_is_path(rubric) and not (wdir / rubric).is_file():
        errors.append(f"{where}: rubric {rubric} does not exist")
    if "maxChars" in value and not (type(value["maxChars"]) is int and value["maxChars"] >= 1000):
        errors.append(f"{where}: maxChars must be an integer >= 1000")
    if "timeout" in value and not (type(value["timeout"]) is int and value["timeout"] > 0):
        errors.append(f"{where}: timeout must be a positive integer")
    inputs = value.get("inputs", [])
    if not isinstance(inputs, list):
        errors.append(f"{where}: inputs must be a list of objects with either 'run' or 'file'")
        return
    for inp in inputs:
        if not (isinstance(inp, dict) and (("run" in inp) ^ ("file" in inp))):
            errors.append(f"{where}: inputs are objects with either 'run' or 'file'")
        elif "maxChars" in inp and not (type(inp["maxChars"]) is int and inp["maxChars"] >= 1000):
            errors.append(f"{where}: input maxChars must be an integer >= 1000")


def lint_gate(gate: Any, where: str, wdir: Optional[Path], errors: List[str]) -> None:
    if not isinstance(gate, dict):
        errors.append(f"{where}: a gate must be an object")
        return
    gid = gate.get("id")
    if not isinstance(gid, str) or not ID_RE.match(gid):
        errors.append(f"{where}: gate id must be lowercase letters, digits and dashes")
    kind = gate.get("type")
    if kind not in GATE_TYPES:
        errors.append(f"{where}: gate '{gid}' has type {kind!r}; use one of {', '.join(GATE_TYPES)}")
        return
    need = {"command": ["run"], "file": ["path"], "red-first": ["run", "lock"], "judge": ["rubric"], "human": ["ask"]}
    for key in need.get(kind, []):
        if key not in gate:
            errors.append(f"{where}: {kind} gate '{gid}' needs '{key}'")
    if kind != "command" and ("pendingExit" in gate or "pendingMax" in gate):
        errors.append(f"{where}: gate '{gid}' pendingExit and pendingMax are only allowed on command gates")
    if kind == "command":
        pending_exit = gate.get("pendingExit")
        if "pendingExit" in gate and not (
            type(pending_exit) is int and 1 <= pending_exit <= 255 and pending_exit not in (126, 127)
        ):
            errors.append(f"{where}: command gate '{gid}' pendingExit must be an integer from 1 to 255 except 126 and 127")
        pending_max = gate.get("pendingMax")
        if "pendingMax" in gate and not (
            not isinstance(pending_max, bool) and isinstance(pending_max, (int, float)) and pending_max > 0
        ):
            errors.append(f"{where}: command gate '{gid}' pendingMax must be a number greater than 0")
    if kind == "red-first" and "lock" in gate and not (
        isinstance(gate["lock"], list) and gate["lock"] and all(isinstance(g, str) for g in gate["lock"])
    ):
        errors.append(f"{where}: red-first gate '{gid}' needs 'lock' as a non-empty list of globs")
    if kind == "judge":
        lint_judge_options(gate, f"{where}: judge gate '{gid}'", wdir, errors)
    if kind == "file" and isinstance(gate.get("path"), str) and SUBAGENT_BLOCKED_NAME_RE.match(Path(gate["path"]).name):
        errors.append(f"{where}: file gate '{gid}' path {Path(gate['path']).name} starts with report, summary, findings "
                      "or analysis; Claude Code won't let a subagent write that name. Rename the file")
    if kind == "file" and "links" in gate and gate["links"] != "resolve":
        errors.append(f"{where}: file gate '{gid}' links must be \"resolve\"")
    for key in ("timeout", "attempts"):
        if key in gate and not (isinstance(gate[key], int) and gate[key] > 0):
            errors.append(f"{where}: gate '{gid}' {key} must be a positive integer")


def lint_checkpoint(cp: Any, where: str, wdir: Optional[Path], errors: List[str], planned: bool = False) -> None:
    if not isinstance(cp, dict):
        errors.append(f"{where}: a checkpoint must be an object")
        return
    cid = cp.get("id")
    if not isinstance(cid, str) or not ID_RE.match(cid):
        errors.append(f"{where}: checkpoint id must be lowercase letters, digits and dashes")
    where = f"{where} '{cid}'"
    if planned:
        if not isinstance(cp.get("instructions"), str) or not cp["instructions"].strip():
            errors.append(f"{where}: a planned checkpoint needs 'instructions' text")
    elif not isinstance(cp.get("step"), str):
        errors.append(f"{where}: needs 'step', the markdown file with its instructions")
    elif wdir is not None and not (wdir / cp["step"]).is_file():
        errors.append(f"{where}: step file {cp['step']} does not exist")
    gates = cp.get("gates", [])
    if not isinstance(gates, list):
        errors.append(f"{where}: 'gates' must be a list")
        return
    ids = [g.get("id") for g in gates if isinstance(g, dict)]
    for dup in sorted({i for i in ids if ids.count(i) > 1 and i}):
        errors.append(f"{where}: gate id '{dup}' is used twice")
    lint_skills(cp.get("skills"), where, errors)
    for auto in AUTO_GATES:
        if auto in ids:
            errors.append(f"{where}: '{auto}' is added to every checkpoint automatically; remove it")
    for g in gates:
        lint_gate(g, where, wdir, errors)


def lint_skills(value: Any, where: str, errors: List[str]) -> None:
    if value is None:
        return
    if not (isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)):
        errors.append(f"{where}: 'skills' must be a list of skill names, like [\"superpowers:test-driven-development\"]")


def lint_workflow(data: Any, wdir: Optional[Path]) -> List[str]:
    errors: List[str] = []
    if not isinstance(data, dict):
        return ["workflow.json must be an object"]
    if not isinstance(data.get("name"), str) or not ID_RE.match(data["name"]):
        errors.append("'name' must be lowercase letters, digits and dashes")
    if not isinstance(data.get("description"), str) or not data["description"].strip():
        errors.append("'description' is required; it's what `gated status` and `/gated` show")
    inputs = data.get("inputs", {})
    if not isinstance(inputs, dict):
        errors.append("'inputs' must be an object of name: {required, default, description}")
    else:
        for name, spec in inputs.items():
            if not ID_RE.match(name.replace("_", "-")) or not isinstance(spec, dict):
                errors.append(f"input '{name}' must map to an object")
    if "attempts" in data and not (isinstance(data["attempts"], int) and data["attempts"] > 0):
        errors.append("'attempts' must be a positive integer")
    lint_skills(data.get("skills"), "workflow", errors)
    for key in ("storySkill", "basedOn"):
        if key in data and not (isinstance(data[key], str) and data[key].strip()):
            errors.append(f"'{key}' must be a non-empty string")
    if "freshContext" in data and not isinstance(data["freshContext"], bool):
        errors.append("'freshContext' must be true or false")
    shares = data.get("shares", [])
    if not (isinstance(shares, list) and all(isinstance(s, str) and s.strip() for s in shares)):
        errors.append("'shares' must be a list of folders, relative to the workflow folder")
    elif wdir is not None:
        for s in shares:
            if not (wdir / s).is_dir():
                errors.append(f"shared folder {s} does not exist")
    protect = data.get("protect", [])
    if not (isinstance(protect, list) and all(isinstance(p, str) and p.strip() for p in protect)):
        errors.append("'protect' must be a list of paths or globs, relative to the project")
    else:
        for p in protect:
            parts = Path(p).parts
            if Path(p).is_absolute() or ".." in parts or (parts and parts[0] in (".git", ".gated")):
                errors.append(f"protect path {p} must be inside the project, and not under .git or .gated")
    if "judge" in data and data["judge"] not in ("claude", "codex"):
        errors.append("'judge' must be \"claude\" or \"codex\"")
    plan = data.get("plan")
    if plan is not None:
        if not isinstance(plan, dict) or not isinstance(plan.get("step"), str):
            errors.append("'plan' must be an object with 'step', the planner's instructions")
        else:
            if wdir is not None and not (wdir / plan["step"]).is_file():
                errors.append(f"plan step {plan['step']} does not exist")
            lint_skills(plan.get("skills"), "plan", errors)
            plan_lock = plan.get("lock", [])
            if not (isinstance(plan_lock, list) and all(isinstance(p, str) and p.strip() for p in plan_lock)):
                errors.append("plan 'lock' must be a list of file paths")
            plan_gates = plan.get("gates", [])
            if not isinstance(plan_gates, list):
                errors.append("plan 'gates' must be a list of command or file gates")
            else:
                for g in plan_gates:
                    if isinstance(g, dict) and g.get("type") not in ("command", "file"):
                        errors.append(f"plan gate '{g.get('id')}': only command and file gates run before the plan judge")
                    else:
                        lint_gate(g, "plan", wdir, errors)
            approval = plan.get("approval", "human")
            if approval not in ("human", "judge"):
                errors.append("plan approval must be \"human\" or \"judge\"")
            if approval == "judge":
                if "rubric" not in plan:
                    errors.append("plan approval \"judge\" requires 'rubric'")
                else:
                    lint_judge_options(plan, "plan judge", wdir, errors)
    cps = data.get("checkpoints", [])
    if not isinstance(cps, list):
        errors.append("'checkpoints' must be a list")
        cps = []
    before = data.get("before", [])
    if not isinstance(before, list):
        errors.append("'before' must be a list of checkpoints")
        before = []
    elif before and plan is None:
        errors.append("'before' runs checkpoints ahead of the plan; without a 'plan', put them in 'checkpoints'")
    for i, cp in enumerate(before):
        lint_checkpoint(cp, f"before[{i}]", wdir, errors)
    all_cps = before + cps
    if plan is None and not cps:
        errors.append("a workflow needs 'checkpoints', a 'plan' step, or both")
    ids = [c.get("id") for c in all_cps if isinstance(c, dict)]
    for dup in sorted({i for i in ids if ids.count(i) > 1 and i}):
        errors.append(f"checkpoint id '{dup}' is used twice")
    for i, cp in enumerate(cps):
        lint_checkpoint(cp, f"checkpoints[{i}]", wdir, errors)
    every = data.get("every", [])
    if not isinstance(every, list):
        errors.append("'every' must be a list of gates")
    else:
        for g in every:
            lint_gate(g, "every", wdir, errors)
        errors += every_collisions(every, all_cps)
    return errors


def every_collisions(every: List[Any], cps: List[Any]) -> List[str]:
    """A checkpoint gate can't reuse an id from 'every': logs and attempt counts would merge."""
    shared = {g.get("id") for g in every if isinstance(g, dict)} | {"todos"}
    out = []
    for cp in cps:
        if not isinstance(cp, dict):
            continue
        for g in cp.get("gates", []) if isinstance(cp.get("gates"), list) else []:
            if isinstance(g, dict) and g.get("id") in shared - {"todos"}:
                out.append(f"checkpoint '{cp.get('id')}' gate '{g['id']}' reuses an id from 'every'")
    return out


def load_workflow(wdir: Path) -> Dict[str, Any]:
    data = read_json(wdir / "workflow.json")
    errors = lint_workflow(data, wdir)
    if errors:
        raise GatedError(f"{wdir / 'workflow.json'} has problems:\n  - " + "\n  - ".join(errors))
    return data


# --------------------------------------------------------------------- runs


def runs_root(project: Path) -> Path:
    return project / ".gated" / "runs"


REQUIRED_STATE_KEYS = ("id", "status", "project", "workflowDir", "checkpoints", "inputs")


def gated_home() -> Path:
    """Per-user bookkeeping outside every project: session index, claims and state digests."""
    return Path(os.environ.get("GATED_HOME") or Path.home() / ".gated")


def run_key(directory: Path) -> str:
    return sha256_text(str(Path(directory).resolve()))[:24]


class Run:
    """One run's folder. Only bin/gated writes state.json, and every save records its digest
    in ~/.gated/digests so an edit made any other way is caught on the next load."""

    def __init__(self, directory: Path):
        self.dir = Path(directory).resolve()
        path = self.dir / "state.json"
        state = read_json(path)
        if not isinstance(state, dict) or any(k not in state for k in REQUIRED_STATE_KEYS):
            raise GatedError(f"{path} is not a gated run")
        self.state: Dict[str, Any] = state
        recorded = self.digest_file().read_text().strip() if self.digest_file().is_file() else None
        self.tampered = recorded is not None and recorded != sha256_file(path)

    def digest_file(self) -> Path:
        return gated_home() / "digests" / run_key(self.dir)

    @property
    def id(self) -> str:
        return self.state["id"]

    @property
    def project(self) -> Path:
        return Path(self.state["project"])

    @property
    def workflow_dir(self) -> Path:
        return Path(self.state["workflowDir"])

    @property
    def status(self) -> str:
        return self.state["status"]

    def workflow(self) -> Dict[str, Any]:
        return read_json(self.workflow_dir / "workflow.json")

    def current(self) -> Optional[Dict[str, Any]]:
        idx = self.state.get("current")
        cps = self.state.get("checkpoints", [])
        if idx is None or idx >= len(cps):
            return None
        return cps[idx]

    def ctx(self, cp: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        c = cp or self.current()
        return {
            "run": str(self.dir),
            "project": str(self.project),
            "workflow": str(self.workflow_dir),
            "input": self.state.get("inputs", {}),
            "checkpoint": c["id"] if c else "plan",
        }

    def save(self) -> None:
        self.state["updatedAt"] = now()
        write_json(self.dir / "state.json", self.state)
        self.digest_file().parent.mkdir(parents=True, exist_ok=True)
        self.digest_file().write_text((sha256_file(self.dir / "state.json") or "") + "\n")
        self.tampered = False


@contextlib.contextmanager
def locked(directory: Path) -> Iterator[Run]:
    """Hold the run's lock and yield a freshly loaded Run. Every state change goes through here."""
    directory = Path(directory)
    with open(directory / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield Run(directory)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def list_runs(project: Path) -> List[Run]:
    root = runs_root(project)
    if not root.is_dir():
        return []
    runs = []
    for d in sorted(root.iterdir()):
        if (d / "state.json").is_file():
            try:
                runs.append(Run(d))
            except (GatedError, OSError):
                continue
    return runs


def new_run_id(project: Path, workflow: str) -> str:
    n = 0
    root = runs_root(project)
    for d in root.iterdir() if root.is_dir() else []:
        m = re.match(rf"^{re.escape(workflow)}-(\d+)$", d.name)
        if m:
            n = max(n, int(m.group(1)))
    return f"{workflow}-{n + 1}"


def env_session() -> Optional[str]:
    return os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID")


# ------------------------------------------------ ownership (outside the project)


def record_claim(token: str, directory: Path) -> None:
    path = gated_home() / "claims" / token
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(Path(directory).resolve()))


def take_claim(token: str) -> Optional[Path]:
    path = gated_home() / "claims" / token
    if not re.fullmatch(r"[0-9a-f]{16}", token) or not path.is_file():
        return None
    target = Path(path.read_text().strip())
    path.unlink()
    return target


def record_owner(session: str, directory: Path) -> None:
    path = gated_home() / "sessions" / sha256_text(session)[:24]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(Path(directory).resolve()))


def session_run_dir(session: Optional[str]) -> Optional[Path]:
    """The run this session owns, from the index. One file read, no scanning."""
    if not session:
        return None
    path = gated_home() / "sessions" / sha256_text(session)[:24]
    if not path.is_file():
        return None
    target = Path(path.read_text().strip())
    return target if (target / "state.json").is_file() else None


def owned_run(session: Optional[str], statuses: Tuple[str, ...] = ACTIVE_STATUSES) -> Optional[Run]:
    target = session_run_dir(session)
    if target is None:
        return None
    try:
        run = Run(target)
    except (GatedError, OSError):
        return None
    if run.state.get("owner") != session or run.status not in statuses:
        return None
    return run


def resolve_run(project: Path, run_id: Optional[str] = None, finished: bool = False) -> Run:
    """Find the run a command means. With finished=True, fall back to the latest run of any status."""
    runs = list_runs(project)
    if run_id:
        for r in runs:
            if r.id == run_id:
                return r
        raise GatedError(f"no run '{run_id}' in {runs_root(project)}")
    mine = owned_run(env_session(), statuses=ACTIVE_STATUSES + (("done", "blocked", "cancelled") if finished else ()))
    if mine:
        return mine
    active = [r for r in runs if r.status in ACTIVE_STATUSES]
    if len(active) == 1:
        return active[0]
    if not active and finished and runs:
        return max(runs, key=lambda r: r.state.get("updatedAt", ""))
    if not active:
        raise GatedError("no active run here. Start one with `gated start <workflow>`.")
    raise GatedError("several runs are active; pass --run <id>: " + ", ".join(r.id for r in active))


def protected_files(project: Path, patterns: List[str]) -> List[Path]:
    """Existing files under a workflow's `protect` paths: a folder means everything in it, anything
    else is a glob relative to the project."""
    found = set()
    for pattern in patterns:
        base = project / pattern.rstrip("/")
        if base.is_dir():
            found |= {p for p in base.rglob("*") if p.is_file()}
        elif base.is_file():
            found.add(base)
        else:
            found |= {p for p in project.glob(pattern) if p.is_file()}
    return sorted(p.resolve() for p in found if ".git" not in p.relative_to(project).parts)


def is_protected(project: Path, patterns: List[str], path: Path) -> bool:
    """Does a path, existing or not yet, fall under a `protect` entry?"""
    import fnmatch

    try:
        rel = Path(path).resolve().relative_to(Path(project).resolve()).as_posix()
    except ValueError:
        return False
    for pattern in patterns:
        p = pattern.rstrip("/")
        if rel == p or rel.startswith(p + "/") or fnmatch.fnmatch(rel, p):
            return True
    return False


def ignore_runs(project: Path) -> None:
    """Keep .gated/ out of git. A .gitignore inside it works in sandboxes where .git/ is read-only."""
    path = project / ".gated" / ".gitignore"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text("*\n")
