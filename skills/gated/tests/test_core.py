import json
import subprocess

from helpers import GatedCase

from gated_lib.core import GatedError, find_workflow, lint_workflow, render


class RenderTest(GatedCase):
    def test_replaces_known_keys(self):
        ctx = {"run": "/r", "project": "/p", "checkpoint": "one", "input": {"repo": "a/b"}}
        self.assertEqual(render("{{run}} {{ project }} {{checkpoint}} {{input.repo}}", ctx), "/r /p one a/b")

    def test_unknown_input_names_the_ones_that_exist(self):
        with self.assertRaisesRegex(GatedError, "this run has: repo"):
            render("{{input.nope}}", {"input": {"repo": "x"}})

    def test_unknown_key_fails(self):
        with self.assertRaisesRegex(GatedError, "unknown template"):
            render("{{secret}}", {"input": {}})


class LintTest(GatedCase):
    def lint(self, data, files=None):
        wdir = self.workflow("w", data, files)
        return lint_workflow(json.loads((wdir / "workflow.json").read_text()), wdir)

    def test_valid_fixed_workflow(self):
        self.assertEqual(self.lint({"checkpoints": [{"id": "a", "step": "a.md", "gates": [
            {"id": "t", "type": "command", "run": "true"}]}]}, {"a.md": "x"}), [])

    def test_missing_step_file(self):
        errors = self.lint({"checkpoints": [{"id": "a", "step": "nope.md", "gates": []}]})
        self.assertTrue(any("does not exist" in e for e in errors), errors)

    def test_unknown_gate_type_and_missing_keys(self):
        errors = self.lint({"checkpoints": [{"id": "a", "step": "a.md", "gates": [
            {"id": "x", "type": "vibes"}, {"id": "y", "type": "command"},
            {"id": "z", "type": "red-first", "run": "t", "lock": []}]}]}, {"a.md": "x"})
        joined = "\n".join(errors)
        self.assertIn("type 'vibes'", joined)
        self.assertIn("needs 'run'", joined)
        self.assertIn("non-empty list of globs", joined)

    def test_todos_is_automatic(self):
        errors = self.lint({"checkpoints": [{"id": "a", "step": "a.md", "gates": [{"id": "todos", "type": "todos"}]}]}, {"a.md": "x"})
        self.assertTrue(any("automatically" in e for e in errors), errors)

    def test_file_gate_name_claude_code_blocks_for_subagents(self):
        gate = {"id": "f", "type": "file", "path": "{{run}}/Report-7d.md"}
        errors = self.lint({"checkpoints": [{"id": "a", "step": "a.md", "gates": [gate]}]}, {"a.md": "x"})
        self.assertTrue(any("won't let a subagent write" in e for e in errors), errors)

    def test_needs_checkpoints_or_plan(self):
        self.assertTrue(any("needs 'checkpoints'" in e for e in self.lint({})))

    def test_duplicate_ids(self):
        cp = {"id": "a", "step": "a.md", "gates": [{"id": "t", "type": "command", "run": "true"}] * 2}
        errors = self.lint({"checkpoints": [cp, cp]}, {"a.md": "x"})
        joined = "\n".join(errors)
        self.assertIn("checkpoint id 'a' is used twice", joined)
        self.assertIn("gate id 't' is used twice", joined)


class FindWorkflowTest(GatedCase):
    def test_project_beats_user(self):
        user = self.home / ".claude" / "workflows" / "demo"
        user.mkdir(parents=True)
        (user / "workflow.json").write_text("{}")
        proj = self.workflow("demo", {"checkpoints": []})
        self.assertEqual(find_workflow("demo", self.project), proj.resolve())

    def test_falls_back_to_user_then_examples(self):
        user = self.home / ".claude" / "workflows" / "mine"
        user.mkdir(parents=True)
        (user / "workflow.json").write_text("{}")
        self.assertEqual(find_workflow("mine", self.project), user.resolve())
        self.assertTrue(find_workflow("weekly-report", self.project).name == "weekly-report")

    def test_missing_lists_where_it_looked(self):
        with self.assertRaisesRegex(GatedError, "Looked in"):
            find_workflow("nope", self.project)


class RunIdTest(GatedCase):
    def test_ids_count_up_and_git_excludes_runs(self):
        self.git_init()
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}])
        self.gated("start", "demo")
        self.gated("start", "demo")
        ids = sorted(p.name for p in (self.project / ".gated" / "runs").iterdir())
        self.assertEqual(ids, ["demo-1", "demo-2"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=str(self.project), capture_output=True, text=True).stdout
        self.assertNotIn(".gated", status)
