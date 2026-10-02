import json
import os

from helpers import GatedCase

from gated_lib import gates as G


class HealthTest(GatedCase):
    def stops(self, session, n):
        for _ in range(n):
            self.hook("stop", {"session_id": session})

    def health(self, *args):
        code, out, err = self.gated("health", *args)
        self.assertEqual(code, 0, err)
        return out

    def test_a_gate_that_fails_three_times_in_a_run_is_a_candidate(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=9)
        run = self.start(session="owner")
        self.todos_done(run)
        self.stops("owner", 3)
        out = self.health()
        self.assertIn("one/t: failed 3 times in one run", out)

    def test_a_blocked_gate_is_a_candidate(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=2)
        run = self.start(session="owner")
        self.todos_done(run)
        self.stops("owner", 2)
        self.assertEqual(self.run_obj().status, "blocked")
        self.assertIn("blocked 1 time(s)", self.health())

    def test_a_failure_fixed_on_the_next_try_is_not_a_candidate(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "test -f fixed"}])
        run = self.start(session="owner")
        self.todos_done(run)
        self.stops("owner", 1)
        (self.project / "fixed").write_text("x")
        self.stops("owner", 1)
        self.assertEqual(self.run_obj().status, "done")
        out = self.health()
        self.assertIn("No candidates", out)
        self.assertIn("one/t", out)

    def test_the_same_judge_reason_in_two_runs_is_a_candidate(self):
        calls = self.tmp / "calls"
        os.environ["GATED_JUDGE_CMD"] = (f"cat >/dev/null; echo x >> {calls}; "
                                         f"if [ $(( $(wc -l < {calls}) % 2 )) -eq 1 ]; then echo '**2. Tested: NOT MET.**'; "
                                         "echo 'VERDICT: FAIL'; else echo 'VERDICT: PASS'; fi")
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict",
                               "inputs": [{"run": "date +%s%N"}]}])
        for session in ("s1", "s2"):
            run = self.start(session=session)
            self.todos_done(run)
            self.stops(session, 2)
        out = self.health()
        self.assertIn('same reason in 2 runs: "tested"', out)

    def test_a_command_failing_once_in_two_runs_is_not_a_candidate(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "test -f fixed"}])
        for session in ("s1", "s2"):
            (self.project / "fixed").unlink(missing_ok=True)
            run = self.start(session=session)
            self.todos_done(run)
            self.stops(session, 1)
            (self.project / "fixed").write_text("x")
            self.stops(session, 1)
        self.assertIn("No candidates", self.health())

    def test_a_new_plan_is_not_a_flip(self):
        self.git_init()
        calls = self.tmp / "calls"
        os.environ["GATED_JUDGE_CMD"] = (f"cat >/dev/null; echo x >> {calls}; "
                                         f"if [ $(wc -l < {calls}) -eq 1 ]; then echo 'VERDICT: FAIL'; else echo 'VERDICT: PASS'; fi")
        self.workflow("judged", {"plan": {"step": "plan.md", "approval": "judge", "rubric": "rubric.md"}},
                      {"plan.md": "Plan it.", "rubric.md": "Approve a sound plan."})
        from gated_lib import runner
        run = self.start("judged")
        for title in ("First try", "Second try"):
            (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [
                {"id": "build", "title": title, "instructions": title, "gates": []}]}))
            self.submit_plan()
            runner.check(self.run_obj())
        self.assertEqual(self.run_obj().status, "running")
        self.assertNotIn("changed its verdict", self.health())

    def test_a_judge_that_flips_on_unchanged_work_is_a_candidate(self):
        self.git_init()
        calls = self.tmp / "calls"
        os.environ["GATED_JUDGE_CMD"] = (f"cat >/dev/null; echo x >> {calls}; "
                                         f"if [ $(wc -l < {calls}) -eq 1 ]; then echo '**2. Tested: NOT MET.**'; "
                                         "echo 'VERDICT: FAIL'; else echo 'VERDICT: PASS'; fi")
        # A noisy input changes the prompt on every call, so the cache doesn't hide the second verdict.
        gate = {"id": "review", "type": "judge", "rubric": "be strict", "inputs": [{"run": "date +%s%N"}]}
        self.simple_workflow([gate], attempts=5)
        run = self.start(session="owner")
        self.todos_done(run)
        self.stops("owner", 2)
        self.assertEqual(self.run_obj().status, "done")
        out = self.health()
        self.assertIn("changed its verdict 1 time(s) on unchanged work", out)
        self.assertIn("tested", out)

    def test_repeated_decisions_are_a_candidate(self):
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo 'VERDICT: DECISION Who sees the panel?'"
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict"}], decisions=True)
        for session in ("s1", "s2"):
            run = self.start(session=session)
            self.todos_done(run)
            self.stops(session, 1)
            self.hook("prompt", {"session_id": session, "prompt": "cancel run"})
        out = self.health()
        self.assertIn("asked the person 2 time(s) in 2 run(s)", out)
        self.assertIn("asked: Who sees the panel?", out)

    def test_focus_on_one_run_and_json(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=9)
        run = self.start(session="owner")
        self.todos_done(run)
        self.stops("owner", 3)
        out = self.health("--run", run.id)
        self.assertIn(f"focus on {run.id}", out)
        self.assertIn("this run", out)
        data = json.loads(self.health("--json"))
        self.assertEqual(data["demo"]["candidates"][0]["gate"], "one/t")

    def test_advisory_checks_are_not_counted(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=9)
        run = self.start(session="owner")
        self.todos_done(run)
        for _ in range(4):
            self.gated("check")
        out = self.health()
        self.assertNotIn("Candidates for a workflow change", out)
        self.assertIn("no gate results recorded yet", out)
        log = (self.run_obj().dir / "one" / "gates" / "t.log").read_text()
        self.assertIn("FAIL (check)", log)


class JudgeOutputTest(GatedCase):
    def test_claude_judge_reports_its_cost_through_json(self):
        self.simple_workflow([])
        run = self.start()
        _, argv = G.judge_command(run)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")
        self.assertEqual(G.judge_reply("claude", '{"result": "MET\\nVERDICT: PASS", "total_cost_usd": 0.43}'),
                         ("MET\nVERDICT: PASS", 0.43))
        self.assertEqual(G.judge_reply("claude", "plain VERDICT: PASS"), ("plain VERDICT: PASS", None))
        self.assertEqual(G.judge_reply("codex", '{"result": "x"}'), ('{"result": "x"}', None))

    def test_not_met_labels(self):
        out = ("**1. Correct: MET.**\n**3. Tested: NOT MET.** Two panel behaviors...\n"
               "**Criterion 0: the contract still matches the issue. NOT MET.**\n"
               "4. **Scoped.** NOT MET. The diff adds\n"
               "A long explanation of the data model and its many details explains why AC-4 is NOT MET here.\n")
        self.assertEqual(G.not_met(out), ["tested", "criterion 0: the contract still matches the issue", "scoped"])


class DismissTest(GatedCase):
    def looping_run(self, session):
        run = self.start(session=session)
        self.todos_done(run)
        for _ in range(3):
            self.hook("stop", {"session_id": session})
        return run

    def test_a_dismissed_gate_returns_only_with_new_failures(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=9)
        run = self.looping_run("s1")
        code, out, err = self.gated("health", "--run", run.id, "--dismiss", "one/t", "--reason", "it catches real bugs")
        self.assertEqual(code, 0, err)
        self.assertIn("won't be flagged again", out)
        self.assertNotIn("Candidates for a workflow change", self.gated("health")[1])
        self.hook("prompt", {"session_id": "s1", "prompt": "cancel run"})
        import time
        time.sleep(1.1)  # timestamps have one-second resolution
        self.looping_run("s2")
        self.assertIn("one/t: failed 3 times", self.gated("health")[1])

    def test_dismissing_mid_run_does_not_break_its_locks(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "test -f ok"}])
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        code, _, err = self.gated("health", "--workflow", "demo", "--dismiss", "one/t", "--reason", "fine")
        self.assertEqual(code, 0, err)
        (self.project / "ok").write_text("x")
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done")

    def test_dismiss_checks_the_gate_name(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=9)
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        code, _, err = self.gated("health", "--workflow", "demo", "--dismiss", "one/typo", "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("Known: one/fresh-context, one/t", err)

    def test_dismiss_needs_a_reason(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        code, _, err = self.gated("health", "--workflow", "demo", "--dismiss", "one/t")
        self.assertEqual(code, 1)
        self.assertIn("say why", err)
