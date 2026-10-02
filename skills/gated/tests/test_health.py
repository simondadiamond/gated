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

    def test_the_same_reason_in_two_runs_is_a_candidate(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "test -f fixed"}])
        for session in ("s1", "s2"):
            (self.project / "fixed").unlink(missing_ok=True)
            run = self.start(session=session)
            self.todos_done(run)
            self.stops(session, 1)
            (self.project / "fixed").write_text("x")
            self.stops(session, 1)
        out = self.health()
        self.assertIn("same reason in 2 runs", out)

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
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict"}])
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
