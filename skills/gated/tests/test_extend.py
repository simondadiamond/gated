import json
import os

from helpers import GatedCase

from gated_lib import runner


class BeforePlanTest(GatedCase):
    def setUp(self):
        super().setUp()
        self.workflow("story", {"before": [{"id": "criteria", "step": "c.md", "gates": [{"id": "ac", "type": "file", "path": "{{run}}/ac.md"}]}],
                                "plan": {"step": "p.md"}, "checkpoints": [{"id": "review", "step": "r.md", "gates": []}]},
                      {"c.md": "write criteria", "p.md": "plan", "r.md": "review"})

    def test_before_runs_first_then_plan_then_the_rest(self):
        run = self.start("story", session="owner")
        self.assertEqual((run.status, run.current()["id"]), ("running", "criteria"))
        (run.dir / "ac.md").write_text("- AC-1")
        self.todos_done(run, "criteria", agent="critic")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Next is the plan", err)
        self.assertEqual(self.run_obj().status, "planning")
        run = self.run_obj()
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "build", "instructions": "x", "gates": []}]}))
        code, out, err = self.submit_plan()
        self.assertEqual(code, 0, err)
        self.assertEqual([c["id"] for c in self.run_obj().state["checkpoints"]], ["criteria", "build", "review"])
        self.hook("prompt", {"session_id": "owner", "prompt": "approve"})
        run = self.run_obj()
        self.assertEqual((run.status, run.current()["id"]), ("running", "build"))

    def test_before_without_plan_is_a_lint_error(self):
        self.workflow("odd", {"before": [{"id": "a", "step": "c.md", "gates": []}], "checkpoints": [{"id": "b", "step": "c.md", "gates": []}]}, {"c.md": "x"})
        code, out, _ = self.gated("lint", "odd")
        self.assertIn("without a 'plan'", out)


class SplitsTest(GatedCase):
    def fake_gh(self, exists):
        gh = self.tmp / "gh"
        gh.write_text("#!/bin/sh\n" + ("echo '{}'; exit 0\n" if exists else "echo 'Could not resolve to an issue' >&2; exit 1\n"))
        gh.chmod(0o755)
        os.environ["GATED_GH"] = str(gh)

    def split_run(self):
        self.simple_workflow([])
        run = self.start(session="owner")
        self.todos_done(run)
        return run

    def test_verified_split_is_recorded_and_reported(self):
        self.fake_gh(True)
        run = self.split_run()
        (run.dir / "one" / "splits.md").write_text("- https://github.com/a/b/issues/7 export to CSV is its own story\n")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("The run is done", err)
        report = (self.run_obj().dir / "report.md").read_text()
        self.assertIn("## Split into new stories", report)
        self.assertIn("issues/7 export to CSV is its own story", report)
        self.assertNotIn("NOT VERIFIED", report)

    def test_split_that_does_not_exist_fails_the_checkpoint(self):
        self.fake_gh(False)
        run = self.split_run()
        (run.dir / "one" / "splits.md").write_text("- https://github.com/a/b/issues/999 made up\n- see the notes\n")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("don't resolve on GitHub", err)
        self.assertIn("issues/999", err)
        self.assertIn("not a GitHub issue URL", err)

    def test_brief_explains_how_to_split(self):
        self.simple_workflow([], storySkill="write-story")
        self.start()
        code, out, _ = self.gated("step")
        self.assertIn("## Too big for this run?", out)
        self.assertIn("the `write-story` skill", out)


class SkillsTest(GatedCase):
    def skill_call(self, run, agent, skill):
        self.hook("pretool", {"session_id": run.state["owner"], "agent_id": agent, "tool_name": "Skill", "tool_input": {"skill": skill}})

    def test_skills_gate_needs_each_skill_loaded_by_a_subagent(self):
        self.simple_workflow([], skills=["superpowers:test-driven-development"])
        run = self.start(session="owner")
        self.todos_done(run, agent="w")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("no subagent loaded: superpowers:test-driven-development", err)
        self.skill_call(run, "w", "test-driven-development")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("The run is done", err)

    def test_orchestrator_loading_the_skill_does_not_count(self):
        self.simple_workflow([], skills=["tdd"])
        run = self.start(session="owner")
        self.todos_done(run, agent="w")
        self.hook("pretool", {"session_id": "owner", "tool_name": "Skill", "tool_input": {"skill": "tdd"}})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("no subagent loaded: tdd", err)

    def test_codex_says_it_cannot_check(self):
        self.simple_workflow([], skills=["tdd"])
        run = self.start(session="owner")
        with_codex = self.run_obj()
        with_codex.state["harness"] = "codex"
        with_codex.save()
        self.todos_done(run, agent="w")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("The run is done", err)
        self.assertIn("not checkable in Codex", (self.run_obj().dir / "report.md").read_text())

    def test_plan_skills_are_checked_at_submit(self):
        self.workflow("story", {"plan": {"step": "p.md", "skills": ["brainstorming"]}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        code, _, err = self.submit_plan()
        self.assertIn("never loaded: brainstorming", err)
        self.skill_call(run, "planner", "superpowers:brainstorming")
        code, _, err = self.gated("submit-plan")
        self.assertEqual(code, 0, err)

    def test_brief_lists_skills_and_lint_checks_them(self):
        self.simple_workflow([], skills=["tdd"])
        self.start()
        self.assertIn("## Skills to use", self.gated("step")[1])
        self.workflow("bad", {"skills": "tdd", "checkpoints": [{"id": "a", "step": "s.md", "gates": []}]}, {"s.md": "x"})
        self.assertIn("list of skill names", self.gated("lint", "bad")[1])


class CustomizeTest(GatedCase):
    def test_copies_builtin_into_project_and_records_origin(self):
        code, out, err = self.gated("customize", "implement-story")
        self.assertEqual(code, 0, err)
        dest = self.project / ".claude" / "workflows" / "implement-story"
        data = json.loads((dest / "workflow.json").read_text())
        self.assertEqual(data["basedOn"], "implement-story (built-in)")
        self.assertEqual(self.gated("lint", "implement-story")[0], 0)
        from gated_lib.core import find_workflow
        self.assertEqual(find_workflow("implement-story", self.project), dest.resolve())

    def test_to_user_and_refuses_to_overwrite(self):
        self.gated("customize", "hello", "--to", "user")
        self.assertTrue((self.home / ".claude" / "workflows" / "hello" / "workflow.json").is_file())
        code, _, err = self.gated("customize", "hello", "--to", "user")
        self.assertIn("already", err)


class ImplementStoryChecksTest(GatedCase):
    def script(self, name):
        from helpers import ROOT
        return ROOT / "workflows" / "implement-story" / "checks" / name

    def run_script(self, *args):
        import subprocess
        return subprocess.run(["python3", *map(str, args)], capture_output=True, text=True)

    def test_criteria_shape(self):
        doc = self.tmp / "acceptance.md"
        doc.write_text("## Acceptance criteria\n- AC-1: Given a manager, when they approve, then the request is approved.\n## Out of scope\n- none\n")
        self.assertEqual(self.run_script(self.script("criteria.py"), doc).returncode, 0)
        doc.write_text("- AC-1: Given x, when y, then z.\n- AC-1: Given x, when y, then z.\n")
        self.assertIn("used twice", self.run_script(self.script("criteria.py"), doc).stderr)
        doc.write_text("- AC-1: approvals work\n")
        self.assertIn("Given/When/Then", self.run_script(self.script("criteria.py"), doc).stderr)
        doc.write_text("nothing numbered\n")
        self.assertIn("no criteria", self.run_script(self.script("criteria.py"), doc).stderr)

    def test_every_criterion_needs_a_locked_test(self):
        run = self.tmp / "run"
        run.mkdir()
        (run / "acceptance.md").write_text("- AC-1: Given a, when b, then c.\n- AC-2: Given a, when b, then c.\n- AC-10: Given a, when b, then c.\n")
        test_file = self.tmp / "leave.test.ts"
        test_file.write_text('test("AC-1: approves", ...)\ntest("AC-10: rejects", ...)\n')
        (run / "state.json").write_text(json.dumps({"red": {"a/tests": {"files": [str(test_file)]}}}))
        p = self.run_script(self.script("criteria-covered.py"), run)
        self.assertEqual(p.returncode, 1)
        self.assertIn("AC-2", p.stderr)
        self.assertNotIn("AC-1,", p.stderr)
        test_file.write_text(test_file.read_text() + 'test("AC-2: edge", ...)\n')
        self.assertEqual(self.run_script(self.script("criteria-covered.py"), run).returncode, 0)

    def test_every_criterion_needs_a_planned_checkpoint(self):
        run = self.tmp / "run"
        run.mkdir()
        (run / "acceptance.md").write_text("- AC-1: Given a, when b, then c.\n- AC-2: Given a, when b, then c.\n")
        (run / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "a", "instructions": "Covers AC-1. Build the model.", "gates": []}]}))
        p = self.run_script(self.script("plan-covers.py"), run)
        self.assertEqual(p.returncode, 1)
        self.assertIn("AC-2", p.stderr)
        (run / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "a", "instructions": "Covers AC-1 and AC-2.", "gates": []}]}))
        self.assertEqual(self.run_script(self.script("plan-covers.py"), run).returncode, 0)

    def test_committing_workflow_needs_git_identity(self):
        # Live run 2026-09-30: the first auto-commit failed mid-run on a clone with no identity.
        self.use_example("implement-story")
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}):
            code, _, err = self.gated("start", "implement-story", "story=x", "test=true")
        self.assertEqual(code, 1)
        self.assertIn("user.email", err)

    def test_implement_story_starts_with_criteria(self):
        self.use_example("implement-story")
        self.git_init()
        run = self.start("implement-story", "story=add leave approvals", "test=true", session="owner")
        self.assertEqual((run.status, run.current()["id"]), ("running", "acceptance-criteria"))
        code, out, _ = self.gated("step")
        self.assertIn("Given <starting state>", out)


class QuestionTest(GatedCase):
    def failing_run(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=2)
        run = self.start(session="owner")
        self.todos_done(run)
        return run

    def test_asking_pauses_without_spending_attempts(self):
        self.failing_run()
        for _ in range(3):
            self.gated("ask", "Which base branch should the diff use?")
            code, out, _ = self.hook("stop", {"session_id": "owner"})
            self.assertEqual(code, 0)
            self.assertIn("waiting for the person to answer", out)
            self.assertEqual(self.run_obj().status, "waiting")
            self.assertEqual(self.hook("stop", {"session_id": "owner"})[0], 0, "a pending question lets every stop through")
            code, out, _ = self.hook("prompt", {"session_id": "owner", "prompt": "use main, I moved it"})
            self.assertIn("recorded your answer", out)
            self.assertEqual(self.run_obj().status, "running")
        run = self.run_obj()
        self.assertEqual(run.state["attempts"], {})
        self.assertEqual(len(run.state["answers"]), 3)
        self.assertIn("use main, I moved it", self.gated("report")[1])

    def test_gates_still_bind_after_the_answer(self):
        self.failing_run()
        self.gated("ask", "ok?")
        self.hook("stop", {"session_id": "owner"})
        self.hook("prompt", {"session_id": "owner", "prompt": "approve"})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, "an answer, even 'approve', passes no gate")
        self.assertIn("isn't done", err)

    def test_asking_outside_a_running_step_is_refused(self):
        # Seen live: a question asked while the plan awaited its judge sat unread, then paused the
        # run five hours later, mid-checkpoint, on a question the approved plan had settled.
        run = self.failing_run()
        run.state["status"] = "awaiting-approval"
        run.save()
        code, _, err = self.gated("ask", "Amend the criteria?")
        self.assertNotEqual(code, 0)
        self.assertIn("questions", err)
        self.assertFalse((run.dir / "question.md").exists())

    def test_cancel_still_works_while_a_question_is_open(self):
        self.failing_run()
        self.gated("ask", "ok?")
        self.hook("stop", {"session_id": "owner"})
        self.hook("prompt", {"session_id": "owner", "prompt": "cancel run"})
        self.assertEqual(self.run_obj().status, "cancelled")


class InterpreterHeuristicTest(GatedCase):
    def test_running_a_locked_script_is_a_read(self):
        # Seen live: the criteria-shape gate's own command was refused.
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "s.md", "gates": [
            {"id": "t", "type": "command", "run": "python3 {{workflow}}/checks/criteria.py x"}]}]},
            {"s.md": "x", "checks/criteria.py": "print(1)"})
        run = self.start(session="owner")
        script = self.project / ".claude" / "workflows" / "demo" / "checks" / "criteria.py"
        for cmd, expected in ((f'python3 "{script}" acceptance.md', 0), (f"node {script}", 0),
                              (f"python3 -c \"open('{script}','w').write('')\"", 2), (f"perl -pi -e 's/a/b/' {script}", 2)):
            code, _, _ = self.hook("pretool", {"session_id": "owner", "agent_id": "w", "tool_name": "Bash", "tool_input": {"command": cmd}})
            self.assertEqual(code, expected, cmd)
