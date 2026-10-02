"""Gate evaluators. Each returns a result the runner records; none trusts the agent."""

from __future__ import annotations

import os
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .core import Run, now, render, sha256_file, sha256_text

LOG_TAIL = 4000
URL_RE = re.compile(r"https?://[^\s)<>\]\"'`]+")
TODO_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s*(.*)$")
STRUCK_RE = re.compile(r"^~~.+?~~\s*(?:[-:–—]\s*)?(\S.*)$")
VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL)\b", re.IGNORECASE)

JUDGE_PREAMBLE = """You are an independent reviewer. You did not do this work and you share no \
context with whoever did. Judge it only against the rubric below and the inputs that follow it. \
Do not change any files. Be strict: a criterion that is not clearly met is not met.

List every criterion from the rubric with MET or NOT MET and one line of evidence. Then end your \
reply with exactly one line, either `VERDICT: PASS` or `VERDICT: FAIL`.
"""


def result(gate: Dict[str, Any], ok: bool, summary: str, log: str = "") -> Dict[str, Any]:
    return {"id": gate["id"], "type": gate["type"], "ok": ok, "summary": summary, "log": log[-LOG_TAIL:], "at": now()}


def judge_error(gate: Dict[str, Any], summary: str, log: str = "") -> Dict[str, Any]:
    """A judge that gave no verdict (timeout, missing binary, no VERDICT line). The work
    wasn't judged, so callers that can retry shouldn't count it as a rejection."""
    r = result(gate, False, summary, log)
    r["error"] = True
    return r


def sh(command: str, cwd: Path, timeout: int, stdin: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> Tuple[Optional[int], str]:
    """Run a shell command. Returns (exit code or None on timeout, combined output)."""
    try:
        p = subprocess.run(
            ["/bin/sh", "-c", command], cwd=str(cwd), input=stdin, capture_output=True,
            text=True, timeout=timeout, env=env,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return None, out + f"\n(timed out after {timeout}s)"


def gate_env(run: Run, cp: Dict[str, Any]) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({"GATED_RUN": str(run.dir), "GATED_RUN_ID": run.id, "GATED_CHECKPOINT": cp["id"], "GATED_PROJECT": str(run.project)})
    return env


def resolve_path(run: Run, raw: str, cp: Dict[str, Any]) -> Path:
    p = Path(render(raw, run.ctx(cp))).expanduser()
    return p if p.is_absolute() else run.project / p


# -------------------------------------------------------------------- types


def check_command(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    cmd = render(gate["run"], run.ctx(cp))
    code, out = sh(cmd, run.project, gate.get("timeout", 600), env=gate_env(run, cp))
    if code is None:
        return result(gate, False, "timed out", out)
    if gate.get("pendingExit") == code:
        pending = result(gate, False, f"`{cmd}` says not decided yet (exit {code})", out)
        pending["pending"] = True
        return pending
    return result(gate, code == 0, f"`{cmd}` exited {code}", out)


def link_ok(url: str) -> Tuple[bool, str]:
    headers = {"User-Agent": "gated-link-check/1"}
    for method in ("HEAD", "GET"):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method=method, headers=headers), timeout=10) as r:
                return r.status < 400, str(r.status)
        except urllib.error.HTTPError as e:
            if method == "HEAD" and e.code in (403, 405, 501):
                continue
            return False, str(e.code)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return False, str(getattr(e, "reason", e))
    return False, "no response"


def check_file(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    path = resolve_path(run, gate["path"], cp)
    if not path.is_file():
        return result(gate, False, f"{path} does not exist")
    text = path.read_text(errors="replace")
    problems: List[str] = []
    if not text.strip():
        problems.append("the file is empty")
    if gate.get("json"):
        import json

        try:
            data = json.loads(text)
            if gate.get("nonEmpty") and not data:
                problems.append("the JSON is empty")
        except json.JSONDecodeError as e:
            problems.append(f"not valid JSON: {e}")
    if gate.get("headings"):
        found = {m.group(1).strip().lower() for m in re.finditer(r"^#{1,6}\s+(.+?)\s*#*\s*$", text, re.M)}
        missing = [h for h in gate["headings"] if h.strip().lower() not in found]
        if missing:
            problems.append("missing headings: " + ", ".join(missing))
    for pattern in gate.get("contains", []):
        if not re.search(pattern, text, re.M):
            problems.append(f"no match for /{pattern}/")
    log = ""
    if gate.get("links") == "resolve":
        urls = sorted({u.rstrip(".,;:") for u in URL_RE.findall(text)})
        bad = []
        for u in urls:
            ok, why = link_ok(u)
            log += f"{why}\t{u}\n"
            if not ok:
                bad.append(f"{u} ({why})")
        if bad:
            problems.append("links that don't resolve: " + ", ".join(bad))
    if problems:
        return result(gate, False, f"{path.name}: " + "; ".join(problems), log)
    return result(gate, True, f"{path.name} passes every check", log)


def parse_todos(text: str) -> Tuple[int, List[str]]:
    """Return (item count, open items). Struck items need a reason after the strike."""
    count, open_items = 0, []
    for line in text.splitlines():
        m = TODO_RE.match(line)
        if not m:
            continue
        count += 1
        mark, body = m.group(1), m.group(2).strip()
        if mark.lower() == "x":
            continue
        if STRUCK_RE.match(body):
            continue
        open_items.append(body or "(empty item)")
    return count, open_items


def todo_path(run: Run, cp: Dict[str, Any]) -> Path:
    return run.dir / cp["id"] / "todo.md"


def check_todos(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    path = todo_path(run, cp)
    if not path.is_file():
        return result(gate, False, f"write the to-do list first: {path}")
    count, open_items = parse_todos(path.read_text())
    if count == 0:
        return result(gate, False, f"{path} has no `- [ ]` items")
    if open_items:
        return result(gate, False, f"{len(open_items)} of {count} to-dos open: " + "; ".join(open_items[:5]))
    return result(gate, True, f"all {count} to-dos done")


def check_red_first(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    key = f"{cp['id']}/{gate['id']}"
    if key not in run.state.get("red", {}):
        return result(gate, False, f"tests aren't locked yet. Once they're written and failing, run `gated red {gate['id']}`")
    return check_command(run, cp, gate)


def judge_command(run: Run) -> Tuple[str, List[str]]:
    custom = os.environ.get("GATED_JUDGE_CMD")
    if custom:
        return "shell", ["/bin/sh", "-c", custom]
    harness = run.workflow().get("judge") or run.state.get("harness") or "claude"
    if harness == "codex":
        return "codex", ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-"]
    # --tools "" removes built-in tools only; MCP servers stay unless --strict-mcp-config.
    return "claude", ["claude", "-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"]


def clip(text: str, limit: int) -> Tuple[str, int]:
    """Keep both ends of an input and make middle truncation visible."""
    if len(text) <= limit:
        return text, 0
    cut = len(text) - limit
    first = limit // 2
    marker = f"\n[gated: {cut} characters cut from the middle of this input]\n"
    return text[:first] + marker + text[-(limit - first):], cut


def check_judge(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    ctx = run.ctx(cp)
    rubric = render((run.workflow_dir / gate["rubric"]).read_text(), ctx) if (run.workflow_dir / gate["rubric"]).is_file() else render(gate["rubric"], ctx)
    parts = [JUDGE_PREAMBLE, "## Rubric\n", rubric, "\n## Inputs\n"]
    cuts = []
    for inp in gate.get("inputs", []):
        limit = inp.get("maxChars", gate.get("maxChars", 200000))
        if "run" in inp:
            cmd = render(inp["run"], ctx)
            _, raw = sh(cmd, run.project, 120, env=gate_env(run, cp))
            out, cut = clip(raw, limit)
            label = cmd
            parts.append(f"### Output of `{cmd}`\n```\n{out}\n```\n")
        else:
            p = resolve_path(run, inp["file"], cp)
            raw = p.read_text(errors="replace") if p.is_file() else "(file does not exist)"
            body, cut = clip(raw, limit)
            label = str(p)
            parts.append(f"### {p}\n```\n{body}\n```\n")
        if cut:
            cuts.append(f"{label} by {cut} chars")
    cut_suffix = " (cut: " + "; ".join(cuts) + ")" if cuts else ""
    prompt = "\n".join(parts)
    key = f"{cp['id']}/{gate['id']}"
    digest = sha256_text(prompt)
    cached = run.state.setdefault("judgeCache", {}).get(key)
    if cached and cached.get("hash") == digest:
        return result(gate, cached["ok"], cached["summary"] + " (cached: inputs unchanged)", cached.get("log", ""))
    kind, argv = judge_command(run)
    env = dict(os.environ)
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CODEX_SESSION_ID"):
        env.pop(var, None)
    try:
        p = subprocess.run(argv, cwd=str(run.project), input=prompt, capture_output=True, text=True, timeout=gate.get("timeout", 900), env=env)
        out = (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return judge_error(gate, f"the {kind} judge timed out" + cut_suffix)
    except FileNotFoundError:
        return judge_error(gate, f"can't run the judge: `{argv[0]}` is not installed" + cut_suffix)
    verdicts = VERDICT_RE.findall(out)
    if not verdicts:
        return judge_error(gate, f"the {kind} judge gave no VERDICT line" + cut_suffix, out)
    ok = verdicts[-1].upper() == "PASS"
    summary = f"{kind} judge: {'PASS' if ok else 'FAIL'}" + cut_suffix
    run.state["judgeCache"][key] = {"hash": digest, "ok": ok, "summary": summary, "log": out[-LOG_TAIL:]}
    return result(gate, ok, summary, out)


def activity_path(run: Run) -> Path:
    return run.dir / "activity.jsonl"


def read_activity(run: Run) -> List[Dict[str, Any]]:
    import json

    path = activity_path(run)
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def check_fresh_context(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    """A new subagent did this checkpoint's work, and the orchestrator edited nothing itself.
    The hooks log every tool call with the caller's agent_id, so this is checked, not claimed."""
    rows = read_activity(run)
    # After the person grants fresh attempts, only the redo counts; the report told them what came before.
    mine = [r for r in rows[cp.get("freshFrom", 0):] if r.get("phase") == cp["id"]]
    agents = {r["agent"] for r in mine if r.get("agent") != "main"}
    earlier = {r["agent"]: r["phase"] for r in rows if r.get("phase") != cp["id"] and r.get("agent") != "main"}
    reused = sorted(a for a in agents if a in earlier)
    main_edits = [r for r in mine if r.get("agent") == "main" and r.get("edit")]
    problems = []
    if not agents:
        problems.append("no subagent worked on this checkpoint. Hand `gated step` to a fresh subagent")
    elif not agents - set(earlier):
        problems.append("every subagent here already worked on an earlier phase: "
                        + ", ".join(f"{a[:8]} ({earlier[a]})" for a in reused) + ". Use a new subagent")
    if main_edits:
        problems.append(f"the orchestrator changed files itself ({len(main_edits)} time(s), e.g. {main_edits[0].get('what', '?')}). "
                        "Checkpoint work belongs to the subagent")
    if problems:
        return result(gate, False, "; ".join(problems))
    return result(gate, True, f"done by a fresh subagent ({', '.join(sorted(a[:8] for a in agents - set(earlier)))})")


def skill_matches(called: str, wanted: str) -> bool:
    """'superpowers:test-driven-development' and 'test-driven-development' name the same skill."""
    c, w = called.strip().lower(), wanted.strip().lower()
    return c == w or c.split(":")[-1] == w.split(":")[-1]


def skills_used(run: Run, phase: str) -> List[str]:
    return [r.get("what", "") for r in read_activity(run)
            if r.get("phase") == phase and r.get("tool") == "Skill" and r.get("agent") != "main"]


def check_skills(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    wanted = gate.get("skills", [])
    if run.state.get("harness") == "codex":
        return result(gate, True, "not checkable in Codex, which loads skills by reading files. "
                                  "The brief told the subagent to use: " + ", ".join(wanted))
    used = skills_used(run, cp["id"])
    missing = [w for w in wanted if not any(skill_matches(u, w) for u in used)]
    if missing:
        return result(gate, False, "no subagent loaded: " + ", ".join(missing) + ". Load each with the Skill tool before the work")
    return result(gate, True, "loaded: " + ", ".join(wanted))


ISSUE_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/issues/\d+")


def verify_issue(run: Run, url: str) -> Tuple[bool, str]:
    """Ask GitHub whether the issue exists. GATED_GH points at another gh binary (tests use a fake)."""
    gh = os.environ.get("GATED_GH", "gh")
    try:
        p = subprocess.run([gh, "issue", "view", url, "--json", "url,title"], cwd=str(run.project),
                           capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return False, "gh is not installed"
    except subprocess.TimeoutExpired:
        return False, "gh timed out"
    if p.returncode != 0:
        return False, (p.stderr or p.stdout).strip()[-200:] or f"gh exited {p.returncode}"
    return True, "exists"


def check_human(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    key = f"{cp['id']}/{gate['id']}"
    for a in run.state.get("approvals", []):
        if a.get("gate") == key:
            return result(gate, True, f"approved in chat at {a['at']}")
    return result(gate, False, "waiting for you: " + render(gate["ask"], run.ctx(cp)))


CHECKS = {
    "command": check_command,
    "file": check_file,
    "todos": check_todos,
    "red-first": check_red_first,
    "judge": check_judge,
    "human": check_human,
    "fresh-context": check_fresh_context,
    "skills": check_skills,
}


def evaluate(run: Run, cp: Dict[str, Any], gate: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return CHECKS[gate["type"]](run, cp, gate)
    except Exception as e:  # a broken gate fails; it never passes by accident
        return result(gate, False, f"the gate itself errored: {e}")


def changed_locks(run: Run) -> List[str]:
    """Locked files whose content no longer matches the hash taken at lock time."""
    return [p for p, digest in run.state.get("locks", {}).items() if sha256_file(Path(p)) != digest]
