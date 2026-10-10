import json
import subprocess

from helpers import GatedCase

from gated_lib import gates as G
from gated_lib import runner


class FixedRunTest(GatedCase):
    def test_start_validates_inputs(self):
        self.workflow("demo", {"inputs": {"repo": {"required": True, "description": "owner/name"}, "days": {"default": 7}},
                               "checkpoints": [{"id": "one", "step": "one.md", "gates": []}]}, {"one.md": "x"})
        code, _, err = self.gated("start", "demo")
        self.assertEqual(code, 1)
        self.assertIn("repo=...  owner/name", err)
        code, _, err = self.gated("start", "demo", "nope=1")
        self.assertIn("unknown input 'nope'", err)
        run = self.start("demo", "repo=a/b")
        self.assertEqual(run.state["inputs"], {"repo": "a/b", "days": "7"})

    def test_checkpoints_advance_then_finish(self):
        self.workflow("demo", {"checkpoints": [
            {"id": "one", "step": "s.md", "gates": [{"id": "a", "type": "file", "path": "a.txt"}]},
            {"id": "two", "step": "s.md", "gates": [{"id": "b", "type": "file", "path": "b.txt"}]}]}, {"s.md": "x"})
        run = self.start()
        may_stop, msg = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("a (file)", msg)
        self.assertIn("todos (todos)", msg)
        self.todos_done(run)
        (self.project / "a.txt").write_text("a")
        may_stop, msg = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("Start checkpoint 'two'", msg)
        self.todos_done(run, "two")
        (self.project / "b.txt").write_text("b")
        may_stop, msg = runner.check(run)
        self.assertTrue(may_stop)
        self.assertEqual(run.status, "done")
        self.assertIn("### one [one]: done", (run.dir / "report.md").read_text())

    def test_check_without_counting_never_uses_attempts(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start()
        for _ in range(10):
            runner.check(run)
        self.assertEqual(run.state["attempts"], {})
        self.assertEqual(run.status, "running")

    def test_pending_gate_allows_stop_without_spending_attempt(self):
        self.simple_workflow([{"id": "ci", "type": "command", "run": "exit 75", "pendingExit": 75}])
        run = self.start()
        self.todos_done(run)
        may_stop, message = runner.check(run, count_attempts=True)
        self.assertTrue(may_stop)
        self.assertIn("Waiting on outside systems", message)
        self.assertIn("No attempt spent", message)
        self.assertEqual(run.status, "running")
        self.assertEqual(run.state["attempts"], {})
        self.assertIn("one/ci", run.state["pendingSince"])
        self.assertIn("one/ci", self.gated("status")[1])
        report = runner.write_report(run).read_text()
        self.assertIn("one/ci", report)
        self.assertIn("PENDING ci", report)

    def test_expired_pending_gate_becomes_failure_and_spends_attempt(self):
        self.simple_workflow([{"id": "ci", "type": "command", "run": "exit 75",
                               "pendingExit": 75, "pendingMax": 1}])
        run = self.start()
        self.todos_done(run)
        run.state["pendingSince"] = {"one/ci": "2000-01-01T00:00:00Z"}
        run.save()
        may_stop, message = runner.check(run, count_attempts=True)
        self.assertFalse(may_stop)
        self.assertIn("still pending after 1s", message)
        self.assertEqual(run.state["attempts"]["one/ci"], 1)

    def test_non_pending_failure_still_spends_attempt(self):
        self.simple_workflow([{"id": "ci", "type": "command", "run": "exit 2", "pendingExit": 75}])
        run = self.start()
        self.todos_done(run)
        runner.check(run, count_attempts=True)
        self.assertEqual(run.state["attempts"]["one/ci"], 1)

    def test_pending_with_failure_counts_only_failure(self):
        self.simple_workflow([
            {"id": "ci", "type": "command", "run": "exit 75", "pendingExit": 75},
            {"id": "tests", "type": "command", "run": "exit 1"},
        ])
        run = self.start()
        self.todos_done(run)
        may_stop, message = runner.check(run, count_attempts=True)
        self.assertFalse(may_stop)
        self.assertIn("tests", message)
        self.assertEqual(run.state["attempts"], {"one/tests": 1})
        self.assertIn("one/ci", run.state["pendingSince"])

    def test_advisory_check_reports_pending_separately_without_state_change(self):
        self.simple_workflow([
            {"id": "ci", "type": "command", "run": "exit 75", "pendingExit": 75},
            {"id": "tests", "type": "command", "run": "exit 1"},
        ])
        run = self.start()
        before = (run.dir / "state.json").read_bytes()
        may_stop, message = runner.check(run, move=False)
        self.assertFalse(may_stop)
        self.assertIn("Pending gates:", message)
        self.assertIn("Failing gates:", message)
        self.assertEqual((run.dir / "state.json").read_bytes(), before)

    def test_pending_since_is_cleared_when_gate_passes(self):
        self.simple_workflow([{"id": "ci", "type": "command",
                               "run": "test -f ready || exit 75", "pendingExit": 75}])
        run = self.start()
        self.todos_done(run)
        runner.check(run, count_attempts=True)
        self.assertIn("one/ci", run.state["pendingSince"])
        (self.project / "ready").write_text("yes")
        runner.check(run, count_attempts=True)
        self.assertNotIn("one/ci", run.state.get("pendingSince", {}))

    def test_budget_blocks_without_skipping(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}], attempts=3)
        run = self.start()
        self.todos_done(run)
        for n in range(2):
            may_stop, msg = runner.check(run, count_attempts=True)
            self.assertFalse(may_stop)
            self.assertIn(f"t {n + 1}/3", msg)
        may_stop, msg = runner.check(run, count_attempts=True)
        self.assertTrue(may_stop)
        self.assertEqual(run.status, "blocked")
        self.assertIn("No gate was skipped", msg)
        report = (run.dir / "report.md").read_text()
        self.assertIn("one/t", report)
        self.assertEqual(run.current()["status"], "active")

    def test_per_gate_budget_overrides_workflow(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false", "attempts": 1}])
        run = self.start()
        self.todos_done(run)
        runner.check(run, count_attempts=True)
        self.assertEqual(run.status, "blocked")

    def test_resume_after_blocked_gets_fresh_attempts(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "test -f fixed"}], attempts=1)
        run = self.start()
        self.todos_done(run)
        runner.check(run, count_attempts=True)
        self.assertEqual(run.status, "blocked")
        code, out, _ = self.gated("resume", "--run", run.id)
        self.assertIn("ask them to type `approve`", out)
        token = next(line for line in out.splitlines() if line.startswith("gated-claim:"))
        self.hook("posttool", {"session_id": "s2", "tool_response": token})
        run = self.run_obj()
        self.assertEqual(run.status, "blocked", "the agent alone must not get fresh attempts")
        self.hook("prompt", {"session_id": "s2", "prompt": "approve"})
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertEqual(run.state["owner"], "s2")
        self.assertEqual(run.state["attempts"], {})

    def test_fresh_attempts_forgive_earlier_orchestrator_edits(self):
        # Live run 2026-09-30: after the orchestrator edited files, fresh-context could never pass
        # again, even once the person granted fresh attempts and a new subagent redid the work.
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}], attempts=1)
        run = self.start(session="owner")
        self.hook("pretool", {"session_id": "owner", "tool_name": "Write", "tool_input": {"file_path": str(self.project / "x")}})
        self.todos_done(run)
        runner.check(run, count_attempts=True)
        self.assertEqual(self.run_obj().status, "blocked")
        _, out, _ = self.gated("resume", "--run", run.id)
        claim = next(line for line in out.splitlines() if line.startswith("gated-claim:"))
        self.hook("posttool", {"session_id": "s2", "tool_response": claim})
        self.hook("prompt", {"session_id": "s2", "prompt": "approve"})
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.todos_done(run, agent="sub-redo")
        from gated_lib import gates as G
        fresh = next(g for g in run.current()["gates"] if g["id"] == "fresh-context")
        self.assertTrue(G.evaluate(run, run.current(), fresh)["ok"])

    def test_tampered_definition_fails_every_check(self):
        wdir = self.simple_workflow([{"id": "t", "type": "command", "run": "true"}])
        run = self.start()
        self.todos_done(run)
        data = json.loads((wdir / "workflow.json").read_text())
        data["checkpoints"][0]["gates"] = []
        (wdir / "workflow.json").write_text(json.dumps(data))
        may_stop, msg = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("locked files changed", msg)

    def test_commit_per_checkpoint(self):
        self.git_init()
        self.simple_workflow([{"id": "t", "type": "file", "path": "feature.txt"}], commit=True)
        run = self.start()
        self.todos_done(run)
        (self.project / "feature.txt").write_text("x")
        may_stop, msg = runner.check(run)
        self.assertIn("committed", msg)
        log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=str(self.project), capture_output=True, text=True).stdout
        self.assertIn("gated(demo-1)", log)
        files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=str(self.project), capture_output=True, text=True).stdout
        self.assertNotIn(".gated", files)

    def test_step_brief_lists_gates_and_learnings(self):
        wdir = self.simple_workflow([{"id": "t", "type": "command", "run": "npm test"}])
        (wdir / "learnings.md").write_text("- use pnpm, not npm")
        self.start()
        code, out, _ = self.gated("step")
        self.assertIn("Do the thing in", out)
        self.assertIn("`npm test` must exit 0", out)
        self.assertIn("todo.md as `- [ ] item`", out)
        self.assertIn("use pnpm, not npm", out)


class PlannedRunTest(GatedCase):
    def setUp(self):
        super().setUp()
        self.workflow("story", {"plan": {"step": "plan.md"},
                                "every": [{"id": "suite", "type": "command", "run": "true"}],
                                "checkpoints": [{"id": "review", "step": "review.md", "gates": [
                                    {"id": "ok", "type": "human", "ask": "try it"}]}]},
                      {"plan.md": "Plan it.", "review.md": "Review it."})

    def write_plan(self, run, cps):
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": cps}))

    def test_plan_then_approval_then_run(self):
        run = self.start("story")
        self.assertEqual(run.status, "planning")
        may_stop, msg = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("Planning isn't finished", msg)
        self.write_plan(run, [{"id": "skeleton", "title": "Skeleton", "instructions": "Build it",
                               "gates": [{"id": "f", "type": "file", "path": "skeleton.txt"}]}])
        code, out, err = self.submit_plan()
        self.assertEqual(code, 0, err)
        self.assertIn("1. Skeleton  [skeleton]", out)
        self.assertIn("suite (command)", out)
        self.assertIn("2. review", out)
        run = self.run_obj()
        self.assertEqual(run.status, "awaiting-approval")
        self.assertTrue(runner.check(run)[0])
        self.hook("prompt", {"session_id": "s1", "prompt": "looks good?"})
        self.assertEqual(self.run_obj().status, "awaiting-approval")
        self.hook("prompt", {"session_id": "s1", "prompt": "go ahead and change step 2 to use a table"})
        self.assertEqual(self.run_obj().status, "awaiting-approval", "feedback that starts like approval isn't approval")
        code, out, _ = self.hook("prompt", {"session_id": "s1", "prompt": "Approve."})
        self.assertIn("approved the plan", out)
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertEqual(run.current()["id"], "skeleton")
        self.assertEqual(run.current()["instructions"], "Build it")

    def test_changes_requested_then_resubmitted(self):
        run = self.start("story")
        self.write_plan(run, [{"id": "a", "instructions": "first try", "gates": []}])
        self.submit_plan()
        self.hook("prompt", {"session_id": "s1", "prompt": "split a into two please"})
        run = self.run_obj()
        self.write_plan(run, [{"id": "a1", "instructions": "half", "gates": []},
                              {"id": "a2", "instructions": "other half", "gates": []}])
        code, out, err = self.submit_plan()
        self.assertEqual(code, 0, err)
        self.assertIn("2. a2", out)
        run = self.run_obj()
        self.assertTrue(run.state["plan"].endswith("plan-2.json"))
        self.hook("prompt", {"session_id": "s1", "prompt": "approve"})
        self.assertEqual(self.run_obj().current()["id"], "a1")

    def test_planner_questions_reach_the_person(self):
        run = self.start("story")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [], "questions": ["the suite fails on main"]}))
        code, _, err = self.submit_plan()
        self.assertEqual(code, 1)
        self.assertIn("the suite fails on main", err)
        (run.dir / "checkpoints.json").write_text(json.dumps({"questions": ["which table?"],
            "checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        code, out, err = self.submit_plan()
        self.assertEqual(code, 0, err)
        self.assertIn("- which table?", out)

    def test_bad_plan_is_rejected_with_reasons(self):
        run = self.start("story")
        self.write_plan(run, [{"id": "Bad Id", "gates": [{"id": "x", "type": "nope"}]}])
        code, _, err = self.submit_plan()
        self.assertEqual(code, 1)
        self.assertIn("lowercase letters", err)
        self.assertIn("needs 'instructions'", err)
        self.write_plan(run, [{"id": "review", "instructions": "x", "gates": []}])
        code, _, err = self.submit_plan()
        self.assertIn("already used", err)

    def test_human_gate_waits_then_approval_finishes(self):
        run = self.start("story")
        self.write_plan(run, [{"id": "a", "instructions": "x", "gates": []}])
        self.submit_plan()
        self.hook("prompt", {"session_id": "s1", "prompt": "approve"})
        run = self.run_obj()
        self.todos_done(run, "a")
        runner.check(run)
        self.todos_done(run, "review")
        may_stop, msg = runner.check(run)
        self.assertTrue(may_stop)
        self.assertEqual(run.status, "waiting")
        self.assertIn("try it", msg)
        code, out, _ = self.hook("prompt", {"session_id": "s1", "prompt": "lgtm"})
        self.assertIn("approved 'review'", out)
        self.assertEqual(self.run_obj().status, "running")
        code, _, err = self.hook("stop", {"session_id": "s1"})
        self.assertIn("The run is done", err)
        self.assertEqual(self.run_obj().status, "done")

    def test_amend_after_done_needs_approval(self):
        run = self.start("story")
        self.write_plan(run, [{"id": "a", "instructions": "x", "gates": []}])
        self.submit_plan()
        self.hook("prompt", {"session_id": "s1", "prompt": "approve"})
        run = self.run_obj()
        for cp in ("a", "review"):
            self.todos_done(run, cp)
            runner.check(run)
        self.hook("prompt", {"session_id": "s1", "prompt": "approve"})
        self.hook("stop", {"session_id": "s1"})
        self.assertEqual(self.run_obj().status, "done")
        change = self.tmp / "change.json"
        change.write_text(json.dumps({"checkpoints": [{"id": "fix-copy", "instructions": "fix the copy", "gates": []}]}))
        code, out, err = self.gated("amend", str(change), "--run", run.id)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_obj().status, "awaiting-approval")
        self.hook("prompt", {"session_id": "s1", "prompt": "approve"})
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertEqual(run.current()["id"], "fix-copy")


class JudgePlanApprovalTest(GatedCase):
    def make_run(self, verdict="PASS", attempts=2, approval="judge"):
        import os
        os.environ["GATED_JUDGE_CMD"] = f"cat >/dev/null; echo judge-detail; echo 'VERDICT: {verdict}'"
        plan = {"step": "plan.md", "approval": approval}
        if approval == "judge":
            plan["rubric"] = "rubric.md"
        self.workflow("judged", {"attempts": attempts, "plan": plan},
                      {"plan.md": "Plan it.", "rubric.md": "Approve a sound plan."})
        run = self.start("judged")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "build", "title": "Build", "instructions": "Build it", "gates": []}
        ]}))
        self.submit_plan()
        return self.run_obj()

    def test_judge_pass_activates_run_and_records_approval(self):
        run = self.make_run()
        may_stop, message = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("judge approved the plan", message)
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertEqual(run.current()["id"], "build")
        approval = next(a for a in run.state["approvals"] if a["gate"] == "plan")
        self.assertEqual(approval["by"], "judge")
        self.assertIn("judge: PASS", approval["text"])
        self.assertNotIn("planReview", run.state)
        self.assertIn("by judge", runner.write_report(run).read_text())
        self.assertTrue((run.dir / "plan" / "gates" / "plan-review.log").is_file())

    def test_plan_lock_freezes_criteria_at_approval_and_only_relock_changes_them(self):
        import os
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo 'VERDICT: PASS'"
        plan = {"step": "plan.md", "approval": "judge", "rubric": "rubric.md", "lock": ["{{run}}/acceptance.md"]}
        self.workflow("judged", {"plan": plan}, {"plan.md": "Plan it.", "rubric.md": "Approve a sound plan."})
        run = self.start("judged")
        criteria = run.dir / "acceptance.md"
        criteria.write_text("- AC-1: old\n")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "build", "title": "Build", "instructions": "Build it", "gates": []}]}))
        self.submit_plan()
        criteria.write_text("- AC-1: fixed before approval\n")  # the planner may still fix a criterion
        runner.check(self.run_obj())
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertEqual(G.changed_locks(run), [])
        criteria.write_text("- AC-1: quietly narrowed\n")
        self.assertEqual(G.changed_locks(self.run_obj()), [str(criteria.resolve())])
        code, _, err = self.gated("relock", str(criteria), "--reason", "the issue was edited")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.run_obj().status, "waiting")

    def test_plan_gates_fail_before_the_judge_is_paid(self):
        import os
        marker = self.project / "judge-called"
        os.environ["GATED_JUDGE_CMD"] = f"cat >/dev/null; touch {marker}; echo 'VERDICT: PASS'"
        plan = {"step": "plan.md", "approval": "judge", "rubric": "rubric.md",
                "gates": [{"id": "ledger", "type": "command", "run": "test -f {{run}}/ledger.md"}]}
        self.workflow("judged", {"plan": plan}, {"plan.md": "Plan it.", "rubric.md": "Approve a sound plan."})
        run = self.start("judged")
        self.assertIn("ledger: `test -f", runner.step_brief(run))
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "build", "title": "Build", "instructions": "Build it", "gates": []}]}))
        self.submit_plan()
        _, message = runner.check(self.run_obj(), move=False)
        self.assertIn("plan gates fail", message)
        runner.check(self.run_obj())
        run = self.run_obj()
        self.assertFalse(marker.exists())
        self.assertEqual((run.status, run.state["attempts"]["plan"]), ("planning", 1))
        self.assertIn("ledger", run.state["planReview"])
        (run.dir / "ledger.md").write_text("ok")
        self.submit_plan()
        runner.check(self.run_obj())
        self.assertTrue(marker.exists())
        self.assertEqual(self.run_obj().status, "running")

    def test_judge_fail_returns_to_planning_with_review_and_attempt(self):
        run = self.make_run("FAIL")
        may_stop, message = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("judge rejected the plan", message)
        run = self.run_obj()
        self.assertEqual(run.status, "planning")
        self.assertEqual(run.state["attempts"]["plan"], 1)
        self.assertIn("judge-detail", run.state["planReview"])
        self.assertIn("## The last plan was rejected", runner.step_brief(run))
        self.assertEqual(run.state["checkpoints"], [])

    def test_judge_timeout_keeps_plan_and_spends_no_attempt(self):
        import os
        run = self.make_run()
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; sleep 5"
        wf = json.loads((run.workflow_dir / "workflow.json").read_text())
        wf["plan"]["timeout"] = 1
        (run.workflow_dir / "workflow.json").write_text(json.dumps(wf))
        may_stop, message = runner.check(run)
        self.assertFalse(may_stop)
        self.assertIn("no verdict", message)
        self.assertIn("timed out", message)
        run = self.run_obj()
        self.assertEqual(run.status, "awaiting-approval")
        self.assertEqual(run.state["attempts"].get("plan", 0), 0)
        self.assertEqual(run.state["checkpoints"][-1]["id"], "build")
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo 'VERDICT: PASS'"
        may_stop, message = runner.check(run)
        self.assertIn("judge approved the plan", message)
        self.assertNotIn("plan", self.run_obj().state.get("judgeErrors", {}))

    def test_judge_without_verdict_counts_after_retries(self):
        import os
        run = self.make_run(attempts=5)
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo thinking"
        for _ in range(runner.JUDGE_ERROR_RETRIES - 1):
            runner.check(run)
            run = self.run_obj()
            self.assertEqual(run.status, "awaiting-approval")
        may_stop, message = runner.check(run)
        self.assertIn("judge rejected the plan", message)
        run = self.run_obj()
        self.assertEqual(run.status, "planning")
        self.assertEqual(run.state["attempts"]["plan"], 1)

    def test_judge_fail_to_budget_blocks_and_resume_replans(self):
        run = self.make_run("FAIL", attempts=2)
        runner.check(run)
        run = self.run_obj()
        self.submit_plan()
        run = self.run_obj()
        may_stop, message = runner.check(run)
        self.assertTrue(may_stop)
        self.assertIn("blocked", message)
        run = self.run_obj()
        self.assertEqual(run.state["blockedOn"], ["plan"])
        runner.resume(run)
        run = self.run_obj()
        message = runner.approve(run, "approve")
        self.assertIn("fresh attempts", message)
        run = self.run_obj()
        self.assertEqual(run.status, "planning")
        self.assertEqual(run.state["checkpoints"], [])

    def test_advisory_check_leaves_awaiting_state_file_identical(self):
        run = self.make_run()
        state_path = run.dir / "state.json"
        before = state_path.read_bytes()
        may_stop, message = runner.check(run, move=False)
        self.assertTrue(may_stop)
        self.assertIn("stop hook has the judge review", message)
        self.assertEqual(state_path.read_bytes(), before)

    def test_amendment_still_waits_for_person(self):
        run = self.make_run()
        run.state["amendment"] = {"from": 1, "status": "done", "current": 0}
        run.save()
        may_stop, message = runner.check(run)
        self.assertTrue(may_stop)
        self.assertIn("person", message)
        self.assertEqual(self.run_obj().status, "awaiting-approval")

    def test_human_plan_approval_does_not_invoke_judge(self):
        import os
        marker = self.tmp / "judge-called"
        run = self.make_run(approval="human")
        os.environ["GATED_JUDGE_CMD"] = f"touch {marker}; echo 'VERDICT: PASS'"
        may_stop, message = runner.check(run)
        self.assertTrue(may_stop)
        self.assertIn("person", message)
        self.assertFalse(marker.exists())
        runner.approve(run, "approve")
        approval = next(a for a in self.run_obj().state["approvals"] if a["gate"] == "plan")
        self.assertEqual(approval["by"], "person")


class ShippedExamplesTest(GatedCase):
    def test_every_example_lints(self):
        from helpers import ROOT
        for wdir in sorted((ROOT / "workflows").iterdir()):
            code, out, err = self.gated("lint", str(wdir))
            self.assertEqual(code, 0, out + err)

    def test_hello_runs_end_to_end(self):
        self.use_example("hello")
        run = self.start("hello")
        self.todos_done(run, "greet")
        (run.dir / "hello.md").write_text("# Hello\ngated checks work.\n## Today\n2026-09-24\n")
        may_stop, msg = runner.check(run)
        self.assertTrue(may_stop, msg)
        self.assertEqual(run.status, "done")

    def test_weekly_report_checks_catch_a_missing_pr(self):
        self.use_example("weekly-report")
        run = self.start("weekly-report", "repo=a/b")
        prs = [{"number": 1, "title": "One", "url": "https://github.com/a/b/pull/1", "mergedAt": "x"},
               {"number": 2, "title": "Two", "url": "https://github.com/a/b/pull/2", "mergedAt": "x"}]
        (run.dir / "prs.json").write_text(json.dumps(prs))
        self.todos_done(run, "gather")
        runner.check(run)
        self.assertEqual(run.current()["id"], "write")
        (run.dir / "weekly-7d.md").write_text("# Summary\nx\n## Merged\n- #1 https://github.com/a/b/pull/1\n## Risks\nNone\n")
        from gated_lib import gates as G
        complete = next(g for g in run.current()["gates"] if g["id"] == "complete")
        r = G.evaluate(run, run.current(), complete)
        self.assertFalse(r["ok"])
        self.assertIn("#2", r["log"])


class ReviewFindingsTest(GatedCase):
    """One test per finding from the independent review of the first version."""

    def test_agent_check_is_advisory(self):
        self.simple_workflow([{"id": "f", "type": "file", "path": "a.txt"}])
        run = self.start()
        self.todos_done(run)
        (self.project / "a.txt").write_text("a")
        code, out, _ = self.gated("check")
        self.assertEqual(code, 0)
        self.assertIn("Only the hook can advance", out)
        run = self.run_obj()
        self.assertEqual((run.status, run.current()["status"]), ("running", "active"))

    def test_env_cannot_fake_a_judge_from_the_agent_shell(self):
        import os
        gate = {"id": "review", "type": "judge", "rubric": "rubric.md"}
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "one.md", "gates": [gate]}]}, {"one.md": "x", "rubric.md": "strict"})
        run = self.start()
        self.todos_done(run)
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo 'VERDICT: PASS'"
        self.gated("check")
        run = self.run_obj()
        self.assertEqual(run.state.get("judgeCache", {}), {})
        self.assertEqual(run.status, "running")

    def test_advisory_check_never_calls_the_judge(self):
        import os
        marker = self.project / "judge-called"
        gate = {"id": "review", "type": "judge", "rubric": "rubric.md"}
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "one.md", "gates": [gate]}]}, {"one.md": "x", "rubric.md": "strict"})
        run = self.start()
        self.todos_done(run)
        os.environ["GATED_JUDGE_CMD"] = f"cat >/dev/null; touch {marker}; echo 'VERDICT: FAIL'"
        code, out, _ = self.gated("check")
        self.assertFalse(marker.exists(), "an advisory check paid for a judge call nobody keeps")
        self.assertEqual(code, 0)
        self.assertIn("Not run here: review (judge)", out)

    def test_stray_run_file_does_not_disable_hooks(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        self.start(session="owner")
        stray = self.project / ".gated" / "runs" / "zz"
        stray.mkdir()
        (stray / "state.json").write_text("[]")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, err)

    def test_state_edit_outside_gated_blocks_the_run(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start(session="owner")
        state = json.loads((run.dir / "state.json").read_text())
        state["status"] = "done"
        (run.dir / "state.json").write_text(json.dumps(state))
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, "a hand-written 'done' must not let the agent stop")
        self.assertIn("changed outside gated", err)
        self.assertEqual(self.run_obj().status, "blocked")

    def test_tampered_running_state_blocks(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start(session="owner")
        state = json.loads((run.dir / "state.json").read_text())
        state["attempts"] = {}
        state["checkpoints"][0]["gates"] = []
        (run.dir / "state.json").write_text(json.dumps(state))
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("changed outside gated", err)
        self.assertEqual(self.run_obj().status, "blocked")

    def test_shell_write_from_inside_the_run_folder_denied(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}])
        run = self.start(session="owner")
        cmd = f"cd {run.dir} && sed -i '' s/running/done/ state.json"
        code, _, _ = self.hook("pretool", {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": cmd}})
        self.assertEqual(code, 2)
        sneaky = "python3 bin/gated status; echo x > .gated/runs/demo-1/state.json"
        code, _, _ = self.hook("pretool", {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": sneaky}})
        self.assertEqual(code, 2)

    def test_check_scripts_are_locked(self):
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "s.md", "gates": [
            {"id": "t", "type": "command", "run": "sh {{workflow}}/checks/c.sh"}]}]}, {"s.md": "x", "checks/c.sh": "exit 1"})
        run = self.start()
        self.assertTrue(any(k.endswith("checks/c.sh") for k in run.state["locks"]))

    def test_shared_folder_is_locked_with_the_workflow(self):
        self.workflow("common", {"checkpoints": [{"id": "one", "step": "s.md", "gates": []}]},
                      {"s.md": "x", "checks/c.sh": "exit 1"})
        self.workflow("lite", {"shares": ["../common"], "checkpoints": [{"id": "one", "step": "../common/s.md", "gates": [
            {"id": "t", "type": "command", "run": "sh {{workflow}}/../common/checks/c.sh"}]}]})
        run = self.start("lite")
        self.assertTrue(any(k.endswith("common/checks/c.sh") for k in run.state["locks"]))
        self.assertIn("x", runner.step_brief(run))
        self.workflow("broken", {"shares": ["../nowhere"], "checkpoints": [{"id": "one", "step": "../common/s.md", "gates": []}]})
        code, _, err = self.gated("start", "broken")
        self.assertEqual(code, 1)
        self.assertIn("shared folder ../nowhere does not exist", err)

    def test_red_refuses_a_command_that_did_not_run(self):
        self.simple_workflow([{"id": "tests", "type": "red-first", "run": "no-such-runner", "lock": ["t_*.sh"]}])
        self.start()
        (self.project / "t_a.sh").write_text("x")
        code, _, err = self.gated("red", "tests")
        self.assertEqual(code, 1)
        self.assertIn("didn't run (exit 127)", err)

    def test_planner_questions_let_the_turn_end(self):
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [], "questions": ["which db?"]}))
        code, out, _ = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 0)
        self.assertIn("which db?", out)

    def test_planning_has_a_budget(self):
        self.workflow("story", {"plan": {"step": "p.md"}, "attempts": 2}, {"p.md": "x"})
        self.start("story", session="owner")
        codes = [self.hook("stop", {"session_id": "owner"})[0] for _ in range(3)]
        self.assertEqual(codes, [2, 2, 0])
        self.assertEqual(self.run_obj().status, "blocked")

    def test_waiting_on_a_planning_subagent_spends_no_attempt(self):
        # Live run 2026-10-09 (#2762): Claude Code ran the planner in the background, so the
        # orchestrator ended its turn to wait, and those stops used up all 5 plan attempts.
        self.workflow("story", {"plan": {"step": "p.md"}, "attempts": 2}, {"p.md": "x"})
        run = self.start("story", session="owner")
        self.subagent_call(run, "planner")
        for _ in range(3):
            code, out, _ = self.hook("stop", {"session_id": "owner"})
            self.assertEqual(code, 0)
            self.assertIn("Planning isn't finished", out)
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": []}))
        code, out, _ = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 0)
        self.assertIn("written but not submitted", out)
        run = self.run_obj()
        self.assertEqual((run.status, run.state["attempts"].get("plan", 0)), ("planning", 0))

    def test_submit_plan_refuses_while_a_question_is_pending(self):
        # Live run 2026-10-09 (#2762): the planner saved a question, the orchestrator submitted in the
        # same turn, the run left planning so the question was never asked, and the plan failed.
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        (run.dir / "question.md").write_text("Is AC-6 right?\n")
        code, _, err = self.submit_plan()
        self.assertEqual(code, 1)
        self.assertIn("End your turn", err)
        self.assertEqual(self.run_obj().status, "planning")
        code, out, _ = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Is AC-6 right?", out)
        self.hook("prompt", {"session_id": "owner", "prompt": "yes"})
        code, _, err = self.submit_plan()
        self.assertEqual(code, 0, err)

    def test_person_can_cancel_and_reject(self):
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        self.submit_plan()
        self.hook("prompt", {"session_id": "owner", "prompt": "approve"})
        run = self.run_obj()
        self.todos_done(run, "a")
        self.hook("stop", {"session_id": "owner"})
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done")
        change = self.tmp / "c.json"
        change.write_text(json.dumps({"checkpoints": [{"id": "b", "instructions": "x", "gates": []}]}))
        self.gated("amend", str(change), "--run", run.id)
        self.hook("prompt", {"session_id": "owner", "prompt": "reject, not needed"})
        run = self.run_obj()
        self.assertEqual((run.status, len(run.state["checkpoints"])), ("done", 1))
        self.hook("prompt", {"session_id": "owner", "prompt": "cancel"})
        self.assertEqual(self.run_obj().status, "done", "plain 'cancel' is not 'cancel run'")

    def test_cancel_run_ends_an_active_run(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        self.start(session="owner")
        self.hook("prompt", {"session_id": "owner", "prompt": "cancel run"})
        self.assertEqual(self.run_obj().status, "cancelled")
        self.assertEqual(self.hook("stop", {"session_id": "owner"})[0], 0)

    def test_amend_is_not_an_exit_from_a_failing_checkpoint(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start(session="owner")
        change = self.tmp / "c.json"
        change.write_text(json.dumps({"checkpoints": [{"id": "b", "instructions": "x", "gates": []}]}))
        code, _, err = self.gated("amend", str(change))
        self.assertEqual(code, 1)
        self.assertIn("finish the current checkpoint first", err)

    def test_waiting_does_not_rerun_gates(self):
        counter = self.tmp / "runs"
        self.simple_workflow([{"id": "t", "type": "command", "run": f"echo x >> {counter}"},
                              {"id": "ok", "type": "human", "ask": "look"}])
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "waiting")
        for _ in range(3):
            self.assertEqual(self.hook("stop", {"session_id": "owner"})[0], 0)
        self.assertEqual(counter.read_text().count("x"), 1)

    def test_every_id_collision_is_a_lint_error(self):
        self.workflow("demo", {"every": [{"id": "suite", "type": "command", "run": "true"}],
                               "checkpoints": [{"id": "one", "step": "s.md", "gates": [{"id": "suite", "type": "command", "run": "true"}]}]},
                      {"s.md": "x"})
        code, out, _ = self.gated("lint", "demo")
        self.assertEqual(code, 1)
        self.assertIn("reuses an id from 'every'", out)

    def test_hooks_find_the_run_from_any_directory(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        self.start(session="owner")
        code, _, _ = self.hook("stop", {"session_id": "owner", "cwd": "/"})
        self.assertEqual(code, 2)


class FindingsTest(GatedCase):
    def test_hook_records_each_finding_once_and_report_lists_them(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        run = self.start(session="owner")
        path = run.dir / "one" / "noticed.md"
        path.write_text("- src/a.py:10 swallows errors, out of scope\nnot a finding line\n")
        self.hook("stop", {"session_id": "owner"})
        path.write_text("- src/a.py:10 swallows errors, out of scope\n- README install step is stale\n")
        self.hook("stop", {"session_id": "owner"})
        run = self.run_obj()
        self.assertEqual([f["text"] for f in run.state["findings"]],
                         ["src/a.py:10 swallows errors, out of scope", "README install step is stale"])
        code, out, _ = self.gated("report")
        self.assertIn("## Found, not fixed", out)
        self.assertIn("[one] README install step is stale", out)

    def test_agent_check_does_not_record_findings(self):
        self.simple_workflow([])
        run = self.start()
        (run.dir / "one" / "noticed.md").write_text("- something\n")
        self.gated("check")
        self.assertEqual(self.run_obj().state.get("findings", []), [])

    def test_findings_across_runs_with_since(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        for n, session in enumerate(("a", "b")):
            run = self.start(session=session)
            (run.dir / "one" / "noticed.md").write_text(f"- finding {n}\n")
            self.hook("stop", {"session_id": session})
        old = self.run_obj()
        old.state["findings"][0]["at"] = "2020-01-01T00:00:00Z"
        old.save()
        code, out, _ = self.gated("findings")
        self.assertIn("demo:", out)
        self.assertIn("finding 0", out)
        self.assertIn("finding 1", out)
        code, out, _ = self.gated("findings", "--since", "30d")
        self.assertNotIn("finding 1", out)
        self.assertIn("finding 0", out)
        code, _, err = self.gated("findings", "--since", "soon")
        self.assertIn("like 30d", err)

    def test_status_since_filters_old_runs(self):
        self.simple_workflow([])
        run = self.start()
        run.state["updatedAt"] = "2020-01-01T00:00:00Z"
        from gated_lib.core import write_json
        write_json(run.dir / "state.json", run.state)
        code, out, _ = self.gated("status", "--since", "7d")
        self.assertIn("No runs", out)


class FreshContextTest(GatedCase):
    """Every phase must be done by a subagent that hasn't worked on another phase."""

    def two_checkpoints(self, **extra):
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "s.md", "gates": []},
                                               {"id": "two", "step": "s.md", "gates": []}], **extra}, {"s.md": "x"})
        return self.start(session="owner")

    def write_todo(self, run, cp):
        (run.dir / cp / "todo.md").write_text("- [x] done\n")

    def test_no_subagent_fails(self):
        run = self.two_checkpoints()
        self.write_todo(run, "one")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("no subagent worked on this checkpoint", err)

    def test_reusing_a_subagent_across_phases_fails(self):
        run = self.two_checkpoints()
        self.todos_done(run, "one", agent="worker")
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().current()["id"], "two")
        self.todos_done(run, "two", agent="worker")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("already worked on an earlier phase", err)
        self.subagent_call(run, "worker-2")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("The run is done", err)

    def test_same_subagent_may_fix_its_own_checkpoint(self):
        run = self.two_checkpoints()
        self.todos_done(run, "one", agent="worker")
        self.subagent_call(run, "worker")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Start checkpoint 'two'", err)

    def test_orchestrator_editing_files_fails(self):
        run = self.two_checkpoints()
        self.todos_done(run, "one")
        self.hook("pretool", {"session_id": "owner", "tool_name": "Edit", "tool_input": {"file_path": str(self.project / "app.py")}})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("orchestrator changed files itself", err)
        self.assertIn("app.py", err)

    def test_orchestrator_shell_writes_count_but_gated_calls_do_not(self):
        run = self.two_checkpoints()
        self.todos_done(run, "one")
        self.hook("pretool", {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": "python3 bin/gated check"}})
        self.hook("pretool", {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": "git status"}})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Start checkpoint 'two'", err)
        self.todos_done(run, "two")
        self.hook("pretool", {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": "echo hi > notes.txt"}})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("orchestrator changed files itself", err)

    def test_orchestrator_writing_git_ignored_files_does_not_count(self):
        # Live run 2026-10-06 (#2633): `rm -rf .next/dev/types && pnpm exec next typegen` at prove
        # failed fresh-context for good, though .next/ is a git-ignored build cache.
        self.git_init()
        (self.project / ".gitignore").write_text("/.next/\n")
        (self.project / "tracked.gen").write_text("x")
        subprocess.run(["git", "add", "-f", ".gitignore", "tracked.gen"], cwd=str(self.project), check=True, capture_output=True)
        run = self.two_checkpoints()
        self.todos_done(run, "one")
        for call in ({"tool_name": "Bash", "tool_input": {"command": "rm -rf .next/dev/types && pnpm exec next typegen"}},
                     {"tool_name": "Bash", "tool_input": {"command": f"rm -f {self.project}/.next/dev/types/validator.ts"}},
                     {"tool_name": "Write", "tool_input": {"file_path": str(self.project / ".next/dev/types/validator.ts")}}):
            self.hook("pretool", {"session_id": "owner", "cwd": str(self.project), **call})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Start checkpoint 'two'", err)
        # A tracked or unignored path, one outside the project, or one mixed with source still counts.
        from gated_lib.hooks import edits_files, ignored_by_git
        ignored = ignored_by_git(self.project, self.project)
        self.assertTrue(edits_files("rm .next/x src/a.ts", ignored))
        self.assertTrue(edits_files("rm src/a.ts", ignored))
        self.assertTrue(edits_files("rm .next/../src/a.ts", ignored))
        self.assertTrue(edits_files("rm /outside-the-project/home/.next/x", ignored))
        self.assertFalse(ignored("tracked.gen"))
        self.assertTrue(ignored(".next/x"))
        self.todos_done(run, "two")
        self.hook("pretool", {"session_id": "owner", "cwd": str(self.project), "tool_name": "Write",
                              "tool_input": {"file_path": str(self.project / "app.py")}})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("orchestrator changed files itself", err)

    def test_orchestrator_writing_the_plan_counts_though_runs_are_git_ignored(self):
        # .gated/ is git-ignored, so the git-ignored exemption (0.7.4) let the orchestrator's own
        # Write of checkpoints.json through. The run folder is the plan's home: writes there count.
        self.git_init()
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        self.subagent_call(run, "planner")
        plan = run.dir / "checkpoints.json"
        plan.write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        self.hook("pretool", {"session_id": "owner", "tool_name": "Write", "tool_input": {"file_path": str(plan)}})
        code, _, err = self.gated("submit-plan")
        self.assertEqual(code, 1)
        self.assertIn("orchestrator changed files itself", err)

    def test_orchestrator_writing_outside_the_project_does_not_count(self):
        # Live run 2026-10-09 (#2762): a Write to the session scratchpad in /private/tmp made
        # submit-plan refuse for good, and the finished plan was thrown away with the run.
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        self.hook("pretool", {"session_id": "owner", "tool_name": "Write",
                              "tool_input": {"file_path": str(self.tmp / "scratchpad" / "decisions.md")}})
        code, _, err = self.submit_plan()
        self.assertEqual(code, 0, err)
        self.workflow("story2", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        self.hook("prompt", {"session_id": "owner", "prompt": "cancel run"})
        run = self.start("story2", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        self.hook("pretool", {"session_id": "owner", "tool_name": "Edit", "tool_input": {"file_path": str(self.project / "app.py")}})
        code, _, err = self.submit_plan()
        self.assertEqual(code, 1)
        self.assertIn("cancel run", err, "the refusal names the way out")

    def test_plan_needs_a_planning_subagent(self):
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        code, _, err = self.gated("submit-plan")
        self.assertEqual(code, 1)
        self.assertIn("no planning subagent", err)
        code, _, err = self.submit_plan()
        self.assertEqual(code, 0, err)

    def test_planner_cannot_also_build(self):
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "x"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [{"id": "a", "instructions": "x", "gates": []}]}))
        self.submit_plan()
        self.hook("prompt", {"session_id": "owner", "prompt": "approve"})
        run = self.run_obj()
        self.todos_done(run, "a", agent="planner")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("already worked on an earlier phase", err)
        self.assertIn("planner (plan)", err)

    def test_command_shapes_from_the_live_run(self):
        # Seen in the live Claude rehearsal on 2026-09-25: these must not count as orchestrator edits.
        run = self.two_checkpoints()
        self.todos_done(run, "one")
        g = str(self.home / "skills" / "gated" / "bin" / "gated")
        for cmd in (f'python3 "{g}" step', f'python3 "{g}" check 2>&1', f"python3 {g} status | tail -5",
                    f"cat {run.dir}/one/todo.md 2>/dev/null", "ls -la .gated/runs/ 2>&1",
                    f'python3 "{g}" check; echo "exit: $?"', "python3 -c 'import json; print(1)'"):
            code, _, err = self.hook("pretool", {"session_id": "owner", "tool_name": "Bash",
                                                 "tool_input": {"command": cmd, "description": "Check gates; report exit"}})
            self.assertEqual(code, 0, f"{cmd}: {err}")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Start checkpoint 'two'", err)

    def test_subagent_may_write_its_own_working_files_by_shell(self):
        run = self.two_checkpoints()
        for cmd in (f"printf -- '- [x] a\\n' > {run.dir}/one/todo.md",
                    f"echo '- stale README' >> {run.dir / 'one' / 'noticed.md'}"):
            code, _, err = self.hook("pretool", {"session_id": "owner", "agent_id": "w", "tool_name": "Bash", "tool_input": {"command": cmd}})
            self.assertEqual(code, 0, f"{cmd}: {err}")
        code, _, _ = self.hook("pretool", {"session_id": "owner", "agent_id": "w", "tool_name": "Bash",
                                          "tool_input": {"command": f"echo x > {run.dir}/state.json"}})
        self.assertEqual(code, 2)

    def test_denied_commands_from_the_second_live_run_are_allowed(self):
        # Exact shapes the hook wrongly denied in the Claude run on 2026-09-25.
        run = self.two_checkpoints()
        d = run.dir / "one"
        g = "/x/skills/gated/bin/gated"
        for agent, cmd in (
            ("w", f"mkdir -p {d} && printf -- '- [ ] Create a.txt\\n' > {d}/todo.md && ls {self.project}"),
            ("w", f"D={d}; mkdir -p $D && printf -- '- [x] done\\n' > $D/todo.md && printf 'beta' > {self.project}/b.txt"),
            ("w", f"echo '- README is stale' >> {d}/noticed.md"),
            (None, f'python3 "{g}" check; cat {self.project}/a.txt; cat {d}/todo.md; ls {d}/'),
            (None, "cat a.txt; cat .gated/runs/demo-1/one/todo.md; ls .gated/runs/demo-1/one/"),
        ):
            payload = {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": cmd, "description": "x"}}
            if agent:
                payload["agent_id"] = agent
            code, _, err = self.hook("pretool", payload)
            self.assertEqual(code, 0, f"{cmd}\n{err}")

    def test_protected_files_still_denied_inside_chains_and_pipes(self):
        run = self.two_checkpoints()
        for cmd in (f"cat {run.dir}/state.json; echo x > {run.dir}/state.json",
                    f"echo '{{}}' | tee -a {run.dir}/activity.jsonl",
                    f"D={run.dir}; sed -i '' s/running/done/ $D/state.json"):
            code, _, _ = self.hook("pretool", {"session_id": "owner", "agent_id": "w", "tool_name": "Bash", "tool_input": {"command": cmd}})
            self.assertEqual(code, 2, cmd)
        code, _, _ = self.hook("pretool", {"session_id": "owner", "tool_name": "Bash",
                                          "tool_input": {"command": f"cat {run.dir}/state.json | python3 -m json.tool"}})
        self.assertEqual(code, 0, "reading state through a pipe is fine")

    def test_chained_gated_call_is_not_exempt(self):
        run = self.two_checkpoints()
        self.todos_done(run, "one")
        self.hook("pretool", {"session_id": "owner", "tool_name": "Bash", "tool_input": {"command": "python3 bin/gated status; echo x > app.py"}})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("orchestrator changed files itself", err)

    def test_workflow_can_opt_out(self):
        run = self.two_checkpoints(freshContext=False)
        self.write_todo(run, "one")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertIn("Start checkpoint 'two'", err)

    def test_activity_log_is_protected(self):
        run = self.two_checkpoints()
        path = run.dir / "activity.jsonl"
        code, _, _ = self.hook("pretool", {"session_id": "owner", "tool_name": "Write", "tool_input": {"file_path": str(path)}})
        self.assertEqual(code, 2)
        code, _, _ = self.hook("pretool", {"session_id": "owner", "tool_name": "Bash",
                                          "tool_input": {"command": "echo '{}' >> activity.jsonl"}})
        self.assertEqual(code, 2)


class LearnTest(GatedCase):
    def test_report_and_learn_work_after_the_run_is_done(self):
        # Found in live rehearsal 1: `gated report` said "no active run" once the run finished.
        self.use_example("hello")
        run = self.start("hello")
        self.todos_done(run, "greet")
        (run.dir / "hello.md").write_text("# Hello\nx\n## Today\n2026-09-24\n")
        runner.check(run)
        code, out, err = self.gated("report")
        self.assertEqual(code, 0, err)
        self.assertIn("Status: **done**", out)
        code, out, err = self.gated("learn", "shorter greetings")
        self.assertEqual(code, 0, err)

    def test_appends_dated_line(self):
        wdir = self.simple_workflow([])
        self.gated("learn", "keep titles short", "--workflow", "demo")
        self.gated("learn", "no emoji", "--workflow", "demo")
        text = (wdir / "learnings.md").read_text()
        self.assertIn(": keep titles short", text)
        self.assertIn(": no emoji", text)


class RelockTest(GatedCase):
    """A locked test the spec proved wrong can be amended, with a reason and a person's approve."""

    def locked_run(self):
        self.simple_workflow([{"id": "tests", "type": "red-first", "run": "sh t_a.sh", "lock": ["t_a.sh"]}])
        self.start(session="owner")
        (self.project / "t_a.sh").write_text("exit 1\n")
        code, _, err = self.gated("red", "tests")
        self.assertEqual(code, 0, err)
        return self.project / "t_a.sh"

    def edit(self, path):
        return self.hook("pretool", {"session_id": "owner", "tool_name": "Edit",
                                     "tool_input": {"file_path": str(path), "old_string": "1", "new_string": "0"}})[0]

    def test_relock_needs_approval_then_locks_again_at_the_next_stop(self):
        path = self.locked_run()
        code, out, err = self.gated("relock", "t_a.sh", "--reason", "the issue says the opposite of AC-9")
        self.assertEqual(code, 0, err)
        self.assertIn("approve", out)
        self.assertEqual(self.run_obj().status, "waiting")
        self.assertEqual(self.edit(path), 2)  # still locked until the person approves
        _, out, _ = self.hook("prompt", {"session_id": "owner", "prompt": "approve"})
        self.assertIn("unlocked", out)
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertNotIn(str(path.resolve()), run.state["locks"])
        self.assertEqual(self.edit(path), 0)
        path.write_text("exit 0\n")
        runner.check(self.run_obj(), count_attempts=True)
        run = self.run_obj()
        self.assertNotIn("unlocked", run.state)
        self.assertIn(str(path.resolve()), run.state["locks"])
        self.assertEqual(G.changed_locks(run), [])
        self.assertEqual(self.edit(path), 2)
        report = runner.write_report(run).read_text()
        self.assertIn("## Relocked tests", report)
        self.assertIn("the issue says the opposite of AC-9", report)

    def test_relock_rejected_keeps_the_lock(self):
        path = self.locked_run()
        self.gated("relock", "t_a.sh", "--reason", "convenient")
        self.hook("prompt", {"session_id": "owner", "prompt": "reject"})
        run = self.run_obj()
        self.assertEqual(run.status, "running")
        self.assertIn(str(path.resolve()), run.state["locks"])
        self.assertNotIn("relock", run.state)

    def test_relock_refuses_workflow_files_and_needs_a_reason(self):
        self.locked_run()
        code, _, err = self.gated("relock", ".claude/workflows/demo/workflow.json", "--reason", "loosen it")
        self.assertEqual(code, 1)
        self.assertIn("only tests locked by `gated red`", err)
        code, _, err = self.gated("relock", "t_a.sh", "--reason", "  ")
        self.assertEqual(code, 1)

    def test_a_waiting_relock_lets_the_turn_end_without_an_attempt(self):
        self.locked_run()
        self.gated("relock", "t_a.sh", "--reason", "spec changed")
        code, out, _ = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 0)
        self.assertIn("unlock", out)
        self.assertEqual(self.run_obj().state["attempts"], {})


class CheckpointJudgeErrorTest(GatedCase):
    def test_a_judge_without_verdict_spends_no_attempt_until_retries_run_out(self):
        import os
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo thinking"
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict"}], attempts=5)
        self.start()
        for _ in range(runner.JUDGE_ERROR_RETRIES - 1):
            runner.check(self.run_obj(), count_attempts=True)
            self.assertNotIn("one/review", self.run_obj().state["attempts"])
        runner.check(self.run_obj(), count_attempts=True)
        self.assertEqual(self.run_obj().state["attempts"]["one/review"], 1)


class GateLogHistoryTest(GatedCase):
    def test_a_gate_log_keeps_every_result(self):
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"}])
        self.start()
        runner.check(self.run_obj(), count_attempts=True)
        runner.check(self.run_obj(), count_attempts=True)
        log = (self.run_obj().dir / "one" / "gates" / "t.log").read_text()
        self.assertEqual(log.count("FAIL"), 2)


class RunFilesTest(GatedCase):
    def test_a_tracked_run_file_fails_the_checkpoint_until_untracked(self):
        self.git_init()
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}], attempts=5)
        run = self.start(session="owner")
        self.todos_done(run)
        todo = run.dir / "one" / "todo.md"
        subprocess.run(["git", "add", "-f", str(todo)], cwd=str(self.project), check=True)
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, err)
        self.assertIn("run-files", err)
        self.assertEqual(self.run_obj().state["attempts"], {"one/run-files": 1})
        subprocess.run(["git", "rm", "-q", "--cached", str(todo)], cwd=str(self.project), check=True)
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done", err)

    def test_files_tracked_outside_this_run_are_left_alone(self):
        self.git_init()
        kept = self.project / ".gated" / "notes.md"
        kept.parent.mkdir(parents=True)
        kept.write_text("a team keeps this on purpose")
        subprocess.run(["git", "add", "-f", str(kept)], cwd=str(self.project), check=True)
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}])
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done")


class ProtectTest(GatedCase):
    def protected_run(self):
        (self.project / "config").mkdir()
        (self.project / "config" / "test.cfg").write_text("strict = true\n")
        (self.project / "harness").mkdir()
        (self.project / "harness" / "proof.mjs").write_text("export const ok = 1\n")
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}],
                             protect=["config/test.cfg", "harness/"])
        return self.start(session="owner")

    def write_call(self, path):
        return self.hook("pretool", {"session_id": "owner", "agent_id": "sub", "tool_name": "Write",
                                     "tool_input": {"file_path": str(path)}})

    def test_edits_and_new_files_under_protected_paths_are_refused(self):
        self.protected_run()
        code, _, err = self.write_call(self.project / "config" / "test.cfg")
        self.assertEqual(code, 2)
        self.assertIn("protects", err)
        code, _, err = self.write_call(self.project / "harness" / "new-helper.mjs")
        self.assertEqual(code, 2, "a new file in a protected folder is refused too")
        code, _, _ = self.write_call(self.project / "src.txt")
        self.assertEqual(code, 0)

    def test_a_change_that_slips_past_the_hook_fails_the_locks_gate(self):
        run = self.protected_run()
        self.todos_done(run)
        (self.project / "harness" / "proof.mjs").write_text("export const ok = 0\n")
        (self.project / "harness" / "extra.mjs").write_text("x\n")
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, err)
        self.assertIn("locked files changed", err)
        self.assertIn("extra.mjs", err)

    def test_protected_files_cannot_be_relocked(self):
        self.protected_run()
        code, _, err = self.gated("relock", "config/test.cfg", "--reason", "want looser tests")
        self.assertEqual(code, 1)
        self.assertIn("can't be relocked", err)

    def test_protect_paths_must_stay_inside_the_project(self):
        from gated_lib.core import lint_workflow
        base = {"name": "x", "description": "d", "checkpoints": [{"id": "a", "step": "s.md"}]}
        for bad in (["/etc/passwd"], ["../other"], [".gated/runs"], "harness"):
            self.assertTrue(any("protect" in e for e in lint_workflow({**base, "protect": bad}, None)), bad)
        self.assertFalse(any("protect" in e for e in lint_workflow({**base, "protect": ["harness/", "*.cfg"]}, None)))


class JudgeDecisionTest(GatedCase):
    def judge_script(self, answered_marker):
        # Asks once; once the person's answer is in the prompt, passes.
        prompt = self.tmp / "prompt"
        return (f"cat > {prompt}; if grep -q '{answered_marker}' {prompt}; then echo 'VERDICT: PASS'; "
                "else echo 'VERDICT: DECISION Should group managers see the panel?'; fi")

    def test_a_checkpoint_decision_asks_the_person_and_spends_no_attempt(self):
        import os
        os.environ["GATED_JUDGE_CMD"] = self.judge_script("only managers")
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict"}], attempts=2, decisions=True)
        run = self.start(session="owner")
        self.todos_done(run)
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, "held once so the agent puts the question to the person")
        self.assertIn("Should group managers see the panel?", err)
        self.assertEqual(self.hook("stop", {"session_id": "owner"})[0], 0, "then the turn ends, still no attempt")
        run = self.run_obj()
        self.assertEqual(run.status, "waiting")
        self.assertIn("Should group managers see the panel?", run.state["question"]["text"])
        self.assertEqual(run.state["attempts"], {})
        self.hook("prompt", {"session_id": "owner", "prompt": "No, only managers for now."})
        self.assertEqual(self.run_obj().status, "running")
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done")
        self.assertIn("Decisions the person made in this run", (self.tmp / "prompt").read_text())

    def test_asking_an_answered_decision_again_is_a_fail(self):
        import os
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo 'VERDICT: DECISION Should group managers see the panel?'"
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict"}], attempts=3, decisions=True)
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        self.hook("prompt", {"session_id": "owner", "prompt": "No."})
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2, err)
        self.assertIn("already made", err)
        self.assertEqual(self.run_obj().state["attempts"], {"one/review": 1})

    def test_other_failing_gates_still_spend_an_attempt(self):
        import os
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo 'VERDICT: DECISION Which roles?'"
        self.simple_workflow([{"id": "t", "type": "command", "run": "false"},
                              {"id": "review", "type": "judge", "rubric": "be strict"}], attempts=3, decisions=True)
        run = self.start(session="owner")
        self.todos_done(run)
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertIn("also failed and spent an attempt: t", err)
        run = self.run_obj()
        self.assertEqual((run.status, run.state["attempts"]), ("waiting", {"one/t": 1}))

    def test_a_plan_judge_decision_keeps_the_plan_and_asks(self):
        import os
        os.environ["GATED_JUDGE_CMD"] = self.judge_script("read-only")
        self.workflow("judged", {"decisions": True, "plan": {"step": "plan.md", "approval": "judge", "rubric": "rubric.md"}},
                      {"plan.md": "Plan it.", "rubric.md": "Approve a sound plan."})
        run = self.start("judged")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "build", "title": "Build", "instructions": "Build it", "gates": []}]}))
        self.submit_plan()
        may_stop, message = runner.check(self.run_obj())
        self.assertTrue(may_stop)
        run = self.run_obj()
        self.assertEqual(run.status, "waiting")
        self.assertNotIn("plan", run.state["attempts"])
        self.hook("prompt", {"session_id": "s1", "prompt": "read-only for them"})
        self.assertEqual(self.run_obj().status, "awaiting-approval")
        runner.check(self.run_obj())
        self.assertEqual(self.run_obj().status, "running")


class HumanPlanGatesTest(GatedCase):
    def test_plan_gates_refuse_a_submission_before_the_person_sees_it(self):
        plan = {"step": "plan.md", "gates": [{"id": "ledger", "type": "command", "run": "test -f {{run}}/ledger.md"}]}
        self.workflow("story", {"plan": plan}, {"plan.md": "Plan it."})
        run = self.start("story")
        (run.dir / "checkpoints.json").write_text(json.dumps({
            "notes": ["AC-3 contradicted the story; now says the request is refused"],
            "checkpoints": [{"id": "build", "title": "Build", "instructions": "Build it", "gates": []}]}))
        code, _, err = self.submit_plan()
        self.assertEqual(code, 1)
        self.assertIn("fails its gates", err)
        self.assertEqual(self.run_obj().status, "planning")
        (run.dir / "ledger.md").write_text("ok")
        code, out, err = self.submit_plan()
        self.assertEqual(code, 0, err)
        self.assertIn("AC-3 contradicted the story", out)
        self.assertEqual(self.run_obj().status, "awaiting-approval")


class DecisionsOffByDefaultTest(GatedCase):
    def test_without_opt_in_the_judge_is_not_offered_decisions_and_sees_no_answers(self):
        import os
        prompt = self.tmp / "prompt"
        os.environ["GATED_JUDGE_CMD"] = f"cat > {prompt}; echo 'VERDICT: DECISION Which roles?'"
        self.simple_workflow([{"id": "review", "type": "judge", "rubric": "be strict"}], attempts=5)
        run = self.start(session="owner")
        self.todos_done(run)
        code, _, err = self.hook("stop", {"session_id": "owner"})
        self.assertEqual(code, 2)
        self.assertEqual(self.run_obj().status, "running", "a workflow that didn't opt in never waits on a person")
        self.assertIn("doesn't allow", err)
        self.assertNotIn("VERDICT: DECISION", prompt.read_text())
        self.assertNotIn("Decisions the person made", prompt.read_text())


class ProtectHardeningTest(GatedCase):
    def test_files_git_ignores_never_fail_the_locks_gate(self):
        self.git_init()
        (self.project / ".gitignore").write_text("__pycache__/\n")
        (self.project / "harness").mkdir()
        (self.project / "harness" / "proof.py").write_text("x = 1\n")
        self.simple_workflow([{"id": "t", "type": "command", "run": "mkdir -p harness/__pycache__ && touch harness/__pycache__/proof.pyc"}],
                             protect=["harness/"])
        run = self.start(session="owner")
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done", "a cache the suite writes isn't a change to protected code")

    def test_a_broad_glob_never_covers_the_run_folder(self):
        from gated_lib.core import is_protected, protected_files
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}], protect=["**/*.json"])
        run = self.start(session="owner")
        self.assertFalse(any(".gated" in str(p) for p in protected_files(self.project, ["**/*.json"])))
        self.assertFalse(is_protected(self.project, ["**/*.json"], run.dir / "checkpoints.json"))
        self.todos_done(run)
        self.hook("stop", {"session_id": "owner"})
        self.assertEqual(self.run_obj().status, "done")

    def test_hook_and_gate_read_a_glob_the_same_way(self):
        from gated_lib.core import is_protected
        p = self.project
        self.assertTrue(is_protected(p, ["*.config.js"], p / "jest.config.js"))
        self.assertFalse(is_protected(p, ["*.config.js"], p / "src" / "a" / "new.config.js"), "* stays in one folder")
        self.assertTrue(is_protected(p, ["**/x.cfg"], p / "x.cfg"), "**/ also matches the top level")
        self.assertTrue(is_protected(p, ["**/x.cfg"], p / "a" / "b" / "x.cfg"))
        self.assertTrue(is_protected(p, ["harness"], p / "harness" / "lib" / "a.mjs"))

    def test_a_common_name_in_a_protected_folder_does_not_refuse_unrelated_commands(self):
        (self.project / "harness").mkdir()
        (self.project / "harness" / "index.ts").write_text("x\n")
        self.simple_workflow([{"id": "t", "type": "command", "run": "true"}], protect=["harness/"])
        self.start(session="owner")
        code, _, err = self.hook("pretool", {"session_id": "owner", "agent_id": "sub", "tool_name": "Bash",
                                             "tool_input": {"command": "sed -i '' s/a/b/ src/index.ts"}})
        self.assertEqual(code, 0, err)

    def test_protecting_the_whole_project_is_refused(self):
        from gated_lib.core import lint_workflow
        base = {"name": "x", "description": "d", "checkpoints": [{"id": "a", "step": "s.md"}]}
        for bad in (["."], ["**"], ["*"]):
            self.assertTrue(any("whole project" in e for e in lint_workflow({**base, "protect": bad}, None)), bad)
