"""Register gated's hooks where a harness reads them.

Hooks registered in a skill's frontmatter don't reach subagents, and subagents
do the editing. So the hooks live at the settings level: the plugin's
hooks/hooks.json, ~/.claude/settings.json for a copied skill, or
~/.codex/hooks.json. Each hook exits at once unless its session owns a run.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .core import SKILL_DIR, GatedError, read_json, write_json

MARKER = "bin/gated hook"
PLUGIN_NAME = "gated"
EVENTS: Dict[str, List[Tuple[str, str, str, int]]] = {
    # event, matcher, hook kind, timeout seconds. Stop runs every gate, so it gets long.
    "claude": [
        ("Stop", "", "stop", 3600),
        ("PreToolUse", "Edit|Write|MultiEdit|NotebookEdit|Bash|Skill", "pretool", 30),
        ("PostToolUse", "Bash", "posttool", 30),
        ("PostToolUse", "AskUserQuestion", "dialog", 30),
        ("UserPromptSubmit", "", "prompt", 600),
    ],
    "codex": [
        ("Stop", "", "stop", 3600),
        ("PreToolUse", "Bash|apply_patch|Edit|Write", "pretool", 30),
        ("PostToolUse", "Bash", "posttool", 30),
        ("UserPromptSubmit", "", "prompt", 600),
    ],
}


def settings_file(harness: str) -> Path:
    if harness == "codex":
        return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "hooks.json"
    return Path.home() / ".claude" / "settings.json"


def plugin_installed() -> bool:
    path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not path.is_file():
        return False
    try:
        plugins = read_json(path).get("plugins", {})
    except GatedError:
        return False
    return any(key.split("@")[0] == PLUGIN_NAME for key in plugins)


def command(kind: str) -> str:
    return f"python3 {shlex.quote(str(SKILL_DIR / 'bin' / 'gated'))} hook {kind}"


def ours(entry: Dict[str, Any]) -> bool:
    return any(MARKER in h.get("command", "") for h in entry.get("hooks", []))


def without_ours(data: Dict[str, Any]) -> Dict[str, Any]:
    hooks = data.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not ours(e)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    return data


def install(harness: str) -> Path:
    if harness == "claude" and plugin_installed():
        raise GatedError(f"the {PLUGIN_NAME} plugin already registers these hooks. "
                         "Installing them again would make every check run twice.")
    path = settings_file(harness)
    data = without_ours(read_json(path) if path.is_file() else {})
    hooks = data.setdefault("hooks", {})
    for event, matcher, kind, timeout in EVENTS[harness]:
        entry: Dict[str, Any] = {"hooks": [{"type": "command", "command": command(kind), "timeout": timeout}]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
    write_json(path, data)
    return path


def uninstall(harness: str) -> Path:
    path = settings_file(harness)
    if path.is_file():
        write_json(path, without_ours(read_json(path)))
    return path
