import json

from helpers import GatedCase

from gated_lib import install
from gated_lib.core import GatedError


class ClaimTest(GatedCase):
    def test_claim_sets_owner_once(self):
        self.simple_workflow([])
        run = self.start(session="first")
        self.assertEqual(run.state["owner"], "first")
        self.hook("posttool", {"session_id": "second", "tool_response": {"stdout": f"gated-claim:{run.state['claim']}"}})
        self.assertEqual(self.run_obj().state["owner"], "first")

    def test_harness_from_transcript_path(self):
        self.simple_workflow([])
        self.gated("start", "demo")
        run = self.run_obj()
        self.hook("posttool", {"session_id": "c", "transcript_path": "/Users/x/.codex/sessions/a.jsonl",
                               "tool_response": f"gated-claim:{run.state['claim']}"})
        self.assertEqual(self.run_obj().state["harness"], "codex")


class StopTest(GatedCase):
    def test_blocks_owner_only(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        self.start(session="owner")
        code, _, err = self.hook("stop", {"session_id": "owner", "stop_hook_active": True})
        self.assertEqual(code, 2)
        self.assertIn("isn't done", err)
        code, out, err = self.hook("stop", {"session_id": "someone-else"})
        self.assertEqual((code, out, err), (0, "", ""))

    def test_ignores_stop_hook_active_and_counts_to_budget(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start(session="owner")
        self.todos_done(run)
        results = [self.hook("stop", {"session_id": "owner", "stop_hook_active": True}) for _ in range(5)]
        self.assertEqual([r[0] for r in results], [2, 2, 2, 2, 2])
        self.assertIn("is blocked", results[-1][2])
        self.assertIn("Tell the person now", results[-1][2])
        self.assertEqual(self.run_obj().status, "blocked")
        code, out, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 0)
        self.assertEqual(self.run_obj().state["attempts"], {"one/t": 5})

    def test_passing_checkpoint_continues_to_next(self):
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "s.md", "gates": []},
                                               {"id": "two", "step": "s.md", "gates": []}]}, {"s.md": "x"})
        run = self.start(session="owner")
        self.todos_done(run)
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("Start checkpoint 'two'", err)
        self.todos_done(run, "two")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("The run is done", err)
        code, out, _ = self.hook("stop", {"session_id": "owner"})
        self.assertEqual((code, out), (0, ""))

    def test_no_run_is_free(self):
        code, out, err = self.hook("stop", {"session_id": "x"})
        self.assertEqual((code, out, err), (0, "", ""))

    def test_bad_payload_fails_open_loudly(self):
        import io
        import sys
        old = sys.stdin
        sys.stdin = io.StringIO("{not json")
        try:
            code, _, err = self.gated("hook", "stop")
        finally:
            sys.stdin = old
        self.assertEqual(code, 0)
        self.assertIn("failed and let the action through", err)


class PreToolTest(GatedCase):
    def setUp(self):
        super().setUp()
        self.wdir = self.simple_workflow([{"id": "t", "type": "command", "run": "true"}])
        self.run = self.start(session="owner")
        self.locked = str(self.wdir / "workflow.json")

    def pre(self, tool, tool_input, session="owner"):
        return self.hook("pretool", {"session_id": session, "tool_name": tool, "tool_input": tool_input})

    def test_edit_of_locked_file_denied(self):
        code, _, err = self.pre("Edit", {"file_path": self.locked})
        self.assertEqual(code, 2)
        self.assertIn("locked", err)
        self.assertEqual(self.pre("Write", {"file_path": str(self.project / "src.py")})[0], 0)

    def test_subagent_write_is_denied(self):
        # Shape captured from Claude Code: a subagent's call carries the parent's session_id.
        code, _, err = self.hook("pretool", {"session_id": "owner", "agent_id": "a1f7c4d2", "agent_type": "general-purpose",
                                             "tool_name": "Write", "tool_input": {"file_path": self.locked, "content": "x"}})
        self.assertEqual(code, 2, err)

    def test_state_file_denied(self):
        self.assertEqual(self.pre("Write", {"file_path": str(self.run.dir / "state.json")})[0], 2)

    def test_denied_edit_is_not_logged_as_activity(self):
        # Live run 2026-09-30: a denied state.json edit counted as a second orchestrator change.
        log = self.run.dir / "activity.jsonl"
        before = log.read_text() if log.exists() else ""
        self.assertEqual(self.pre("Edit", {"file_path": self.locked})[0], 2)
        self.assertEqual(log.read_text() if log.exists() else "", before)

    def test_other_session_not_affected(self):
        self.assertEqual(self.pre("Edit", {"file_path": self.locked}, session="other")[0], 0)

    def test_apply_patch_paths(self):
        patch = f"*** Begin Patch\n*** Update File: {self.locked}\n@@\n-a\n+b\n*** End Patch"
        self.assertEqual(self.pre("apply_patch", {"command": patch})[0], 2)
        rel = ".claude/workflows/demo/workflow.json"
        self.assertEqual(self.pre("apply_patch", {"command": f"*** Begin Patch\n*** Update File: {rel}\n*** End Patch"})[0], 2)
        self.assertEqual(self.pre("apply_patch", {"command": "*** Begin Patch\n*** Add File: new.py\n*** End Patch"})[0], 0)

    def test_names_match_whole_path_parts(self):
        from gated_lib.hooks import names_file
        self.assertTrue(names_file("echo x > a/s.md", "s.md"))
        self.assertTrue(names_file("sed -i '' x \"$D/state.json\"", "state.json"))
        self.assertFalse(names_file("echo x >> run/one/findings.md", "s.md"))
        self.assertFalse(names_file("echo x > old-state.json.bak", "state.json"))

    def test_quoted_greater_than_is_not_an_edit(self):
        # Live run 2026-09-30: a read-only gh search flagged the orchestrator as editing files.
        from gated_lib.hooks import edits_files
        self.assertFalse(edits_files('gh pr list --search "merged:>=2026-09-23" --json number'))
        self.assertFalse(edits_files("python3 -c \"print(1 > 0)\""))
        self.assertTrue(edits_files('echo "x" > out.md'))
        self.assertTrue(edits_files("sed -i 's/a/b/' f.md"))

    def test_shell_writes_to_locked_file(self):
        self.assertEqual(self.pre("Bash", {"command": f"echo x > {self.locked}"})[0], 2)
        self.assertEqual(self.pre("Bash", {"command": "sed -i '' s/a/b/ .claude/workflows/demo/workflow.json"})[0], 2)
        self.assertEqual(self.pre("Bash", {"command": f"cat {self.locked}"})[0], 0)
        self.assertEqual(self.pre("Bash", {"command": "python3 gated status > /tmp/x; gated check"})[0], 0)


class InstallTest(GatedCase):
    def test_codex_install_is_idempotent_and_keeps_other_hooks(self):
        path = self.home / ".codex" / "hooks.json"
        path.parent.mkdir(parents=True)
        mine = {"hooks": [{"type": "command", "command": "my-linter"}]}
        path.write_text(json.dumps({"hooks": {"Stop": [mine]}}))
        install.install("codex")
        install.install("codex")
        data = json.loads(path.read_text())
        self.assertEqual(len(data["hooks"]["Stop"]), 2)
        self.assertEqual(data["hooks"]["Stop"][0], mine)
        self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "Bash|apply_patch|Edit|Write")
        install.uninstall("codex")
        self.assertEqual(json.loads(path.read_text()), {"hooks": {"Stop": [mine]}})

    def test_claude_install_keeps_other_settings(self):
        path = self.home / ".claude" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(ls)"]}}))
        install.install("claude")
        data = json.loads(path.read_text())
        self.assertEqual(data["model"], "opus")
        self.assertIn("MultiEdit", data["hooks"]["PreToolUse"][0]["matcher"])
        install.uninstall("claude")
        self.assertEqual(json.loads(path.read_text()), {"model": "opus", "permissions": {"allow": ["Bash(ls)"]}})

    def test_claude_install_refuses_when_plugin_registers_hooks(self):
        path = self.home / ".claude" / "plugins" / "installed_plugins.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"version": 2, "plugins": {"gated@gated": []}}))
        with self.assertRaisesRegex(GatedError, "twice"):
            install.install("claude")

    def test_plugin_hooks_file_matches_installer(self):
        from helpers import ROOT
        data = json.loads((ROOT.parents[1] / "hooks" / "hooks.json").read_text())
        for event, matcher, kind, _ in install.EVENTS["claude"]:
            entry = data["hooks"][event][0]
            self.assertEqual(entry.get("matcher", ""), matcher)
            self.assertTrue(entry["hooks"][0]["command"].endswith(f"hook {kind}"))
