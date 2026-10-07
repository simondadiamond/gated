"""Hook handlers for Claude Code and Codex. Both send JSON on stdin; exit 2 plus stderr blocks.

Hooks are registered at the settings level, so they run in every session and for subagents.
Each one looks up the calling session in ~/.gated/sessions (one file read). No run owned means
exit 0 at once, so sessions that never started a run pay nothing.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import runner
from .core import (FINISHED_STATUSES, SKILL_DIR, Run, is_protected, locked, now, owned_run, record_owner,
                   session_run_dir, take_claim)

# The whole message must be an explicit approval, so a conversational "yes" or feedback is not approval.
APPROVE_RE = re.compile(r"^\s*(approve|approved|lgtm|ship it)\s*[.!]*\s*$", re.IGNORECASE)
REJECT_RE = re.compile(r"^\s*reject\b", re.IGNORECASE)
CANCEL_RE = re.compile(r"^\s*cancel run\s*[.!]*\s*$", re.IGNORECASE)
CLAIM_RE = re.compile(r"gated-claim:([0-9a-f]{16})")
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)
# Running a script (`python3 checks/x.py`) reads it; only inline code (-c, -e, -i) can write anything.
WRITE_HINT_RE = re.compile(r"(>|\btee\b|\bsed\b|\brm\b|\bmv\b|\bcp\b|\bln\b|\btruncate\b|\bdd\b|\bchmod\b|\binstall\b"
                           r"|\b(?:python3?|perl|node|ruby)\b[^|;&]*\s-(?:c|e|i|pi)\b|\bgit\s+(checkout|restore|stash|reset)\b)")
GATED_CALL_RE = re.compile(r"""^\s*(?:python3\s+)?["']?[^\s"';&|]*\bgated["']?\s+[a-z-]+\b""")
HARMLESS_REDIRECT_RE = re.compile(r"\d*>&\d|\d*>\s*/dev/null")
# A '>' inside quotes is an argument, not a redirect: `gh pr list --search "merged:>=2026-09-23"`.
# An escaped character (`\'`, `\>`) is a plain character, so it neither opens a quote nor redirects.
QUOTED_RE = re.compile(r"\\.|'[^']*'|\"(?:[^\"\\]|\\.)*\"", re.S)


def unquoted(command: str) -> str:
    return HARMLESS_REDIRECT_RE.sub("", QUOTED_RE.sub(lambda m: "_" if m.group().startswith("\\") else '""', command))


def split_outside_quotes(command: str, separator: str) -> List[str]:
    """Split on a separator only where the shell would: `python3 -c "a;b"` is one command."""
    parts, start = [], 0
    for m in re.finditer(QUOTED_RE.pattern + "|(" + separator + ")", command, re.S):
        if m.group(1) is not None:
            parts.append(command[start:m.start()])
            start = m.end()
    return parts + [command[start:]]
CHAINING_RE = re.compile(r";|&&|\|\||`|\$\(|\n")


def is_gated_call(command: str) -> bool:
    """A single call to bin/gated, optionally piped to a pager. Chained commands don't count."""
    first = command.split("|")[0]
    return bool(GATED_CALL_RE.match(command)) and not CHAINING_RE.search(command) and ">" not in HARMLESS_REDIRECT_RE.sub("", first)


# For "did the orchestrator do the work itself": only unambiguous file writes. Interpreters are
# left out because the orchestrator runs read-only python and node all the time.
EDIT_HINT_RE = re.compile(r"(>|\btee\b|\bsed\s+-i|\brm\b|\bmv\b|\bcp\b|\bln\b|\btruncate\b|\binstall\b|\bgit\s+(checkout|restore|stash|reset|apply|commit|merge|rebase)\b)")
SEGMENT_RE = re.compile(r";|&&|\|\||\n")
PIPE = r"\|"
PIPELINE_RE = re.compile(r";|&&|\|\|?|\n")


def is_own_cli(command: str) -> bool:
    """A single call to this install's bin/gated, not to any file that happens to be named gated."""
    if not is_gated_call(command):
        return False
    words = command.split("|")[0].split()
    path = words[1] if words[0] == "python3" and len(words) > 1 else words[0]
    return Path(path.strip("\"'")).expanduser().resolve() == (SKILL_DIR / "bin" / "gated").resolve()


def command_text(tool_input: Dict[str, Any]) -> str:
    """The shell command itself, without the tool call's description or other fields."""
    cmd = tool_input.get("command")
    if isinstance(cmd, list):
        return " ".join(str(c) for c in cmd)
    if isinstance(cmd, str):
        return cmd
    return " ".join(strings(tool_input))


def names_file(segment: str, name: str) -> bool:
    """Whole path components only: a locked 's.md' must not match 'findings.md'."""
    return re.search(r"(?:^|[\s/\"'=])" + re.escape(name) + r"(?:$|[\s\"';|&)>])", segment) is not None


# Saving `gated step` to a temp folder or the run folder (the step's own workspace) to hand it on
# isn't the orchestrator doing the work. Only edits_files() skips these: guard() still sees them,
# so the run folder's protected files stay denied by name. A path with `..` could leave the
# folder, and one with quotes or `$(` mid-path can't be read safely, so neither is skipped.
_PATH_CHAR = r"(?:(?!\.\.)[^\s;&|<>\"'`$()])"
_SCRATCH_PATH = r"(?:(?:/(?:private/)?tmp|\$\{?TMPDIR\}?)/|(?:" + _PATH_CHAR + r"*/)?\.gated/runs/)" + _PATH_CHAR + "*"
SCRATCH_REDIRECT_RE = re.compile(r"\d*>>?\s*(?:\"" + _SCRATCH_PATH + r"\"|" + _SCRATCH_PATH + r"(?=[\s;&|)]|$))")


SCRATCH_PATH_RE = re.compile(_SCRATCH_PATH)
# Deleting, moving or copying scratch files is the same as redirecting into them.
SCRATCH_FILE_COMMANDS = {"rm", "mv", "cp", "tee"}
# One shell word, quotes kept. A lone unclosed quote stays in the word, so that path matches nothing.
SHELL_WORD_RE = re.compile(r"(?:" + QUOTED_RE.pattern + r"|[^\s'\"\\]|['\"\\])+", re.S)
REDIRECT_WORD_RE = re.compile(r"\d*>>?(&\d+|/dev/null)?")


def scratch_files_only(stage: str) -> bool:
    """rm, mv, cp or tee whose every path is in a temp folder or the run folder, bare or double-quoted.
    A path with `..`, `$(`, single quotes or an unclosed quote doesn't count, the same as for redirects."""
    if ">" in unquoted(stage):
        return False
    words = SHELL_WORD_RE.findall(stage)
    if not words or words[0] not in SCRATCH_FILE_COMMANDS:
        return False
    paths: List[str] = []
    options, skip = True, False
    for word in words[1:]:
        redirect = REDIRECT_WORD_RE.fullmatch(word)
        if skip or redirect:  # the check above left only /dev/null and fd dups; a bare '>' takes the next word
            skip = bool(redirect) and redirect.group(1) is None
        elif options and word == "--":
            options = False
        elif not (options and word.startswith("-")):
            paths.append(word[1:-1] if len(word) > 1 and word[0] == word[-1] == '"' else word)
    return bool(paths) and all(SCRATCH_PATH_RE.fullmatch(p) for p in paths)


# A heredoc's body is text the command reads, not shell. `<<<` is a here-string, not a heredoc.
HEREDOC_RE = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^'\n]+)'|\"([^\"\n]+)\"|\\?([\w.-]+))")
# `$((1<<4))` is a shift, so arithmetic is skipped like a quoted string.
HEREDOC_SCAN_RE = re.compile(QUOTED_RE.pattern + r"|\$\(\([^\n]*?\)\)|" + HEREDOC_RE.pattern)
# Fed to a shell, the body is commands after all, so it stays. Any shell word on the heredoc's line
# counts, so `cat <<EOF | bash` keeps its body too.
SHELL_WORDS = {"sh", "bash", "zsh", "dash", "ksh"}


def without_heredoc_bodies(command: str) -> str:
    """The command with each heredoc's body and end marker taken out, unless a shell reads it.
    A body never closed runs to the end of the command, as the shell reads it."""
    lines, out, i = command.split("\n"), [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC_SCAN_RE.finditer(line):
            marker = m.group(2) or m.group(3) or m.group(4)
            if not marker:
                continue  # a quoted string: a '<<' inside it is an argument
            to_shell = any(Path(w).name in SHELL_WORDS for w in re.split(r"[\s;&|()]+", line))
            while i < len(lines):
                body = lines[i]
                i += 1
                if to_shell:
                    out.append(body)
                if (body.lstrip("\t") if m.group(1) else body) == marker:
                    break
    return "\n".join(out)


def edits_files(command: str) -> bool:
    # Quotes first, then separators: a `;` or `>` inside a quoted argument is not shell syntax.
    # Each stage of a pipeline is judged alone, so `gated step | python3 -c "..."` reads only.
    command = without_heredoc_bodies(command)
    for segment in split_outside_quotes(command, SEGMENT_RE.pattern):
        for stage in split_outside_quotes(segment, PIPE):
            if not stage.strip() or is_gated_call(stage):
                continue
            rest = SCRATCH_REDIRECT_RE.sub("", stage)
            if EDIT_HINT_RE.search(unquoted(rest)) and not scratch_files_only(rest):
                return True
    return False


def writes(command: str) -> bool:
    """Does this shell command look like it writes files? Redirects to /dev/null and fd dups don't."""
    return bool(WRITE_HINT_RE.search(HARMLESS_REDIRECT_RE.sub("", command)))
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


class Decision:
    def __init__(self, code: int = 0, stderr: str = "", stdout: str = ""):
        self.code, self.stderr, self.stdout = code, stderr, stdout


def strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v)


def protected_paths(run: Run) -> List[Path]:
    paths = [Path(p) for p in run.state.get("locks", {})] + [run.dir / "state.json", run.dir / "activity.jsonl", run.digest_file()]
    return [p.resolve() for p in paths]


def stop(payload: Dict[str, Any]) -> Decision:
    target = session_run_dir(payload.get("session_id"))
    if target is None:
        return Decision()
    with locked(target) as run:
        if run.state.get("owner") != payload.get("session_id"):
            return Decision()
        if run.tampered:  # before the finished check: a hand-written "done" must not end the run
            return Decision(2, f"gated: {runner.block(run, ['state.json was changed outside gated'])}\n"
                               f"Tell the person now, then stop.")
        if run.status in FINISHED_STATUSES:
            return Decision()
        question = runner.take_question(run)
        if question:  # asking the person isn't an attempt; the gates still have to pass afterwards
            return Decision(stdout=json.dumps({"systemMessage": f"gated: waiting for the person to answer: {question}"}))
        # stop_hook_active is deliberately ignored: attempts are counted in the run, not by the harness.
        before = run.status
        may_stop, message = runner.check(run, count_attempts=True)
        if run.status in ("done", "blocked") and before != run.status:
            # Hold the stop once, so the agent tells the person instead of going quiet. The next
            # stop finds the run finished and goes through without spending an attempt.
            return Decision(2, f"gated: {message}\nTell the person now: summarize {run.dir / 'report.md'} in a few lines, then stop.")
        if run.status == "waiting" and before != "waiting" and (run.state.get("question") or {}).get("from") == "judge":
            # Same for a judge's decision: hold once so the agent puts the question to the person.
            # The next stop finds the run waiting and goes through without spending an attempt.
            return Decision(2, f"gated: {message}")
    if may_stop:
        return Decision(stdout=json.dumps({"systemMessage": f"gated: {message}"}))
    return Decision(2, f"gated: {message}")


def record_activity(run: Run, payload: Dict[str, Any], tool: str, tool_input: Dict[str, Any]) -> None:
    """Log who made this call, so the fresh-context gate can check that a new subagent did each phase.
    One appended line per call; the agent can't write this file."""
    if run.status == "planning":
        phase = "plan"
    elif run.status == "running" and run.current():
        phase = run.current()["id"]
    else:
        return
    command = command_text(tool_input) if tool not in EDIT_TOOLS else ""
    edit = tool in EDIT_TOOLS or tool == "apply_patch" or (bool(command) and edits_files(command))
    what = tool_input.get("skill") if tool == "Skill" else (tool_input.get("file_path") or tool_input.get("notebook_path") or command[:80])
    row = {"phase": phase, "agent": payload.get("agent_id") or "main", "tool": tool, "edit": edit, "what": str(what), "at": now()}
    with open(run.dir / "activity.jsonl", "a") as f:
        f.write(json.dumps(row) + "\n")


def pretool(payload: Dict[str, Any]) -> Decision:
    run = owned_run(payload.get("session_id"))
    if run is None:
        return Decision()
    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input") or {}
    decision = guard(run, payload, tool, tool_input)
    if decision.code == 0:  # a denied call changed nothing, so it mustn't count against fresh-context
        record_activity(run, payload, tool, tool_input)
        if tool == "Bash" and run.state.get("harness") == "claude" and is_own_cli(command_text(tool_input)):
            # The run's own CLI, alone and unchained: approve it, so the person isn't asked on every
            # turn after the one that invoked the skill, and subagents can run `gated red`.
            decision.stdout = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                                 "permissionDecisionReason": "gated's own command"}})
    return decision


def guard(run: Run, payload: Dict[str, Any], tool: str, tool_input: Dict[str, Any]) -> Decision:
    protected = protected_paths(run)
    cwd = Path(payload.get("cwd") or run.project)

    def hit(raw: str) -> Optional[Path]:
        p = Path(raw.strip()).expanduser()
        p = (p if p.is_absolute() else cwd / p).resolve()
        return p if p in protected else None

    targets: List[str] = []
    if tool in EDIT_TOOLS:
        targets = [tool_input.get(k) for k in ("file_path", "notebook_path", "path") if isinstance(tool_input.get(k), str)]
    else:
        for s in strings(tool_input):
            for m in PATCH_FILE_RE.finditer(s):
                targets.append(m.group(1) or m.group(2))
    patterns = run.state.get("protect") or []
    for t in targets:
        target = Path(t.strip()).expanduser()
        target = (target if target.is_absolute() else cwd / target).resolve()
        if patterns and is_protected(run.project, patterns, target):
            return Decision(2, f"gated: {target} is under a path the workflow protects, because its gates trust that "
                               f"code. Run {run.id} can't change it; it changes in its own change, outside a run.")
        p = hit(t)
        if p:
            return Decision(2, f"gated: {p} is locked by run {run.id}. The gates are checked against it, so it can't change. "
                               "Change the work under test instead.")
    if tool in EDIT_TOOLS or targets:
        return Decision()
    # Shell writes: deny a part of the command that both looks like a write and names a protected
    # file. Parts are judged one by one, so reading state while writing todo.md is fine. The rest of
    # the run folder is the step's own workspace. Hashes and the digest catch what this misses.
    # Files locked by `protect` are left out: a protected folder holds common names (index.ts,
    # package.json) and matching by name would refuse unrelated commands. The locks gate still
    # catches a change to them at the next stop.
    by_protect = set(run.state.get("protectLocked", []))
    names = {"state.json", "activity.jsonl"} | {p.name for p in protected if str(p) not in by_protect}
    for segment in PIPELINE_RE.split(command_text(tool_input)):
        if not segment.strip() or is_gated_call(segment) or not writes(segment):
            continue
        for name in sorted(names):
            if names_file(segment, name):
                return Decision(2, f"gated: this command looks like it writes '{name}', which run {run.id} protects. "
                                   "Locked files and run state can't change while the run is active.")
    return Decision()


def posttool(payload: Dict[str, Any]) -> Decision:
    """Claim a new run for this session. `gated start` and `gated resume` print the token."""
    session = payload.get("session_id")
    if not session:
        return Decision()
    text = json.dumps(payload)
    for token in set(CLAIM_RE.findall(text)):
        target = take_claim(token)
        if target is None or not (target / "state.json").is_file():
            continue
        with locked(target) as run:
            if run.state.get("owner") is not None or run.state.get("claim") != token:
                continue
            run.state["owner"] = session
            transcript = str(payload.get("transcript_path", ""))
            if "/.claude/" in transcript:
                run.state["harness"] = "claude"
            elif "/.codex/" in transcript or "/sessions/" in transcript:
                run.state["harness"] = "codex"
            run.save()
        record_owner(session, target)
    return Decision()


def prompt(payload: Dict[str, Any]) -> Decision:
    target = session_run_dir(payload.get("session_id"))
    text = payload.get("prompt") or ""
    if target is None:
        return Decision()
    with locked(target) as run:
        if run.state.get("owner") != payload.get("session_id") or run.status in ("done", "cancelled"):
            return Decision()
        if CANCEL_RE.match(text):
            return Decision(stdout=runner.cancel(run, text))
        if run.state.get("question"):  # any reply answers an open question
            return Decision(stdout=runner.answer(run, text))
        if not (APPROVE_RE.match(text) or REJECT_RE.match(text)):
            return Decision()
        if REJECT_RE.match(text):
            return Decision(stdout=runner.reject(run, text))
        return Decision(stdout=runner.approve(run, text))


def dialog_text(response: Any) -> str:
    """The question tool's result: {"answers": {question: choice}} in Claude Code; anything else
    is taken as text."""
    if isinstance(response, dict):
        answers = response.get("answers")
        if isinstance(answers, dict) and answers:
            return "; ".join(f"{q}: {a}" for q, a in answers.items())
        return json.dumps(response)
    return str(response or "")


def dialog(payload: Dict[str, Any]) -> Decision:
    """PostToolUse on the question tool. The person's choice in the dialog is their answer to an
    open question, exactly like a typed reply (the harness wrote it, not the agent). Approvals are
    not taken from a dialog: those stay a typed `approve`."""
    target = session_run_dir(payload.get("session_id"))
    if target is None:
        return Decision()
    text = dialog_text(payload.get("tool_response"))
    if not text.strip():
        return Decision()
    with locked(target) as run:
        if run.state.get("owner") != payload.get("session_id") or run.status in FINISHED_STATUSES:
            return Decision()
        if run.state.get("question"):  # the Stop hook already paused the run on it
            return Decision(stdout=runner.answer(run, text))
        if run.status in ("running", "planning"):  # asked and answered within one turn
            return Decision(stdout=runner.answer_pending(run, text))
    return Decision()


HANDLERS = {"stop": stop, "pretool": pretool, "posttool": posttool, "prompt": prompt, "dialog": dialog}


def main(kind: str) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
        decision = HANDLERS[kind](payload if isinstance(payload, dict) else {})
    except Exception as e:  # never wedge a session on a bug in gated; say so loudly instead
        sys.stderr.write(f"gated hook '{kind}' failed and let the action through: {e}\n")
        return 0
    if decision.stdout:
        sys.stdout.write(decision.stdout + "\n")
    if decision.stderr:
        sys.stderr.write(decision.stderr + "\n")
    return decision.code
