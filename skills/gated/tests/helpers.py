"""Shared fixtures: a throwaway project, HOME and workflow per test."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from gated_lib import cli  # noqa: E402
from gated_lib.core import Run, list_runs  # noqa: E402


class GatedCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="gated-test-")).resolve()
        self.home = self.tmp / "home"
        self.project = self.tmp / "project"
        self.home.mkdir()
        self.project.mkdir()
        self._env = dict(os.environ)
        os.environ["HOME"] = str(self.home)
        os.environ["CODEX_HOME"] = str(self.home / ".codex")
        for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDE_PROJECT_DIR", "GATED_JUDGE_CMD"):
            os.environ.pop(var, None)
        self._cwd = os.getcwd()
        os.chdir(self.project)

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        os.environ.clear()
        os.environ.update(self._env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ builders

    def git_init(self) -> None:
        for cmd in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@example.com"],
                    ["git", "config", "user.name", "Test"], ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(cmd, cwd=str(self.project), check=True, capture_output=True)

    def workflow(self, name: str, data: Dict[str, Any], files: Optional[Dict[str, str]] = None) -> Path:
        wdir = self.project / ".claude" / "workflows" / name
        wdir.mkdir(parents=True, exist_ok=True)
        data = {"name": name, "description": f"test workflow {name}", **data}
        (wdir / "workflow.json").write_text(json.dumps(data, indent=2))
        for rel, body in (files or {}).items():
            (wdir / rel).parent.mkdir(parents=True, exist_ok=True)
            (wdir / rel).write_text(body)
        return wdir

    def use_example(self, name: str) -> Path:
        """Copy a shipped example into the test project, so tests never write into the repo."""
        dest = self.project / ".claude" / "workflows" / name
        shutil.copytree(ROOT / "workflows" / name, dest)
        return dest

    def simple_workflow(self, gates: List[Dict[str, Any]], name: str = "demo", **extra: Any) -> Path:
        return self.workflow(name, {"checkpoints": [{"id": "one", "step": "steps/one.md", "gates": gates}], **extra},
                             {"steps/one.md": "Do the thing in {{run}}."})

    # ------------------------------------------------------------- actions

    def gated(self, *argv: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def start(self, workflow: str = "demo", *inputs: str, session: str = "s1") -> Run:
        code, out, err = self.gated("start", workflow, *inputs)
        self.assertEqual(code, 0, err)
        token = next(line for line in out.splitlines() if line.startswith("gated-claim:"))
        self.hook("posttool", {"session_id": session, "tool_name": "Bash", "tool_response": {"stdout": token}})
        return self.run_obj()

    def run_obj(self) -> Run:
        runs = list_runs(self.project)
        self.assertTrue(runs, "no run was created")
        return Run(runs[-1].dir)

    def hook(self, kind: str, payload: Dict[str, Any]) -> Tuple[int, str, str]:
        payload.setdefault("cwd", str(self.project))
        stdin = io.StringIO(json.dumps(payload))
        old = sys.stdin
        sys.stdin = stdin
        try:
            return self.gated("hook", kind)
        finally:
            sys.stdin = old

    def todos_done(self, run: Run, cp: str = "one", agent: Optional[str] = None) -> None:
        """Finish a checkpoint's to-dos, the way a fresh subagent would: its tool call goes through the hook."""
        path = run.dir / cp / "todo.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("- [x] did it\n")
        self.subagent_call(run, agent or f"sub-{cp}")

    def subagent_call(self, run: Run, agent: str, tool: str = "Write") -> None:
        owner = Run(run.dir).state.get("owner")
        self.assertIsNotNone(owner, "start the run with self.start() so the hooks know its owner")
        self.hook("pretool", {"session_id": owner, "agent_id": agent, "agent_type": "general-purpose",
                              "tool_name": tool, "tool_input": {"file_path": str(self.project / "work.txt")}})

    def submit_plan(self, *extra: str) -> Tuple[int, str, str]:
        """Submit the plan after a planning subagent has worked on it."""
        self.subagent_call(self.run_obj(), "planner")
        return self.gated("submit-plan", *extra)
