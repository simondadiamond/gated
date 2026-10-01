import http.server
import threading

from helpers import GatedCase

from gated_lib import gates as G
from gated_lib.gates import parse_todos


def gate_result(case, gate, prepare=None):
    """Start a one-checkpoint run with this gate and evaluate it once."""
    case.simple_workflow([gate])
    run = case.start()
    if prepare:
        prepare(run)
    return G.evaluate(run, run.current(), gate), run


class CommandGateTest(GatedCase):
    def test_pass_and_fail(self):
        ok, _ = gate_result(self, {"id": "t", "type": "command", "run": "exit 0"})
        self.assertTrue(ok["ok"])

    def test_fail_keeps_output(self):
        r, _ = gate_result(self, {"id": "t", "type": "command", "run": "echo boom; exit 3"})
        self.assertFalse(r["ok"])
        self.assertIn("exited 3", r["summary"])
        self.assertIn("boom", r["log"])

    def test_pending_exit_is_a_pending_result(self):
        r, _ = gate_result(self, {"id": "ci", "type": "command", "run": "exit 75", "pendingExit": 75})
        self.assertFalse(r["ok"])
        self.assertTrue(r["pending"])
        self.assertEqual(r["summary"], "`exit 75` says not decided yet (exit 75)")

    def test_exit_75_without_pending_setting_is_failure(self):
        r, _ = gate_result(self, {"id": "ci", "type": "command", "run": "exit 75"})
        self.assertFalse(r["ok"])
        self.assertNotIn("pending", r)
        self.assertIn("exited 75", r["summary"])

    def test_timeout(self):
        r, _ = gate_result(self, {"id": "t", "type": "command", "run": "sleep 5", "timeout": 1})
        self.assertFalse(r["ok"])
        self.assertEqual(r["summary"], "timed out")

    def test_workflow_template_reaches_bundled_scripts(self):
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "s.md", "gates": [
            {"id": "t", "type": "command", "run": "sh {{workflow}}/checks/ok.sh"}]}]},
            {"s.md": "x", "checks/ok.sh": "exit 0"})
        run = self.start()
        self.assertTrue(G.evaluate(run, run.current(), run.current()["gates"][0])["ok"])

    def test_runs_in_project_with_run_env(self):
        r, run = gate_result(self, {"id": "t", "type": "command", "run": 'test "$(pwd -P)" = "$GATED_PROJECT" && test -n "$GATED_RUN"'})
        self.assertTrue(r["ok"], r)


class FileGateTest(GatedCase):
    def test_missing_file(self):
        r, _ = gate_result(self, {"id": "f", "type": "file", "path": "{{run}}/out.md"})
        self.assertIn("does not exist", r["summary"])

    def test_headings_and_json(self):
        gate = {"id": "f", "type": "file", "path": "{{run}}/out.md", "headings": ["Summary", "Risks"], "contains": ["\\d+ PRs"]}
        r, _ = gate_result(self, gate, lambda run: (run.dir / "out.md").write_text("# Summary\n3 PRs\n## risks\n"))
        self.assertTrue(r["ok"], r)

    def test_missing_heading_named(self):
        gate = {"id": "f", "type": "file", "path": "{{run}}/out.md", "headings": ["Summary", "Risks"]}
        r, _ = gate_result(self, gate, lambda run: (run.dir / "out.md").write_text("# Summary\n"))
        self.assertIn("missing headings: Risks", r["summary"])

    def test_bad_json(self):
        gate = {"id": "f", "type": "file", "path": "{{run}}/d.json", "json": True}
        r, _ = gate_result(self, gate, lambda run: (run.dir / "d.json").write_text("{nope"))
        self.assertIn("not valid JSON", r["summary"])

    def test_empty_json_when_nonempty_required(self):
        gate = {"id": "f", "type": "file", "path": "{{run}}/d.json", "json": True, "nonEmpty": True}
        r, _ = gate_result(self, gate, lambda run: (run.dir / "d.json").write_text("[]"))
        self.assertIn("JSON is empty", r["summary"])

    def test_links_resolve_against_local_server(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_HEAD(self):
                self.send_response(200 if self.path == "/ok" else 404)
                self.end_headers()

            do_GET = do_HEAD

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            gate = {"id": "f", "type": "file", "path": "{{run}}/r.md", "links": "resolve"}
            r, _ = gate_result(self, gate, lambda run: (run.dir / "r.md").write_text(f"see {base}/ok and ({base}/missing)."))
            self.assertFalse(r["ok"])
            self.assertIn("/missing (404)", r["summary"])
            self.assertNotIn("/ok (", r["summary"])
        finally:
            server.shutdown()
            server.server_close()


class TodosTest(GatedCase):
    def test_parse(self):
        text = "- [x] done\n- [ ] ~~dropped~~ not needed after all\n- [ ] ~~dropped without reason~~\n- [ ] open\nnot a todo\n"
        count, open_items = parse_todos(text)
        self.assertEqual(count, 4)
        self.assertEqual(open_items, ["~~dropped without reason~~", "open"])

    def test_gate_needs_file_and_items(self):
        self.simple_workflow([])
        run = self.start()
        todo = {"id": "todos", "type": "todos"}
        self.assertIn("write the to-do list first", G.evaluate(run, run.current(), todo)["summary"])
        (run.dir / "one" / "todo.md").write_text("nothing here\n")
        self.assertIn("no `- [ ]` items", G.evaluate(run, run.current(), todo)["summary"])
        (run.dir / "one" / "todo.md").write_text("- [x] a\n- [ ] b\n")
        self.assertIn("1 of 2 to-dos open: b", G.evaluate(run, run.current(), todo)["summary"])
        (run.dir / "one" / "todo.md").write_text("- [x] a\n- [X] b\n")
        self.assertTrue(G.evaluate(run, run.current(), todo)["ok"])


class RedFirstTest(GatedCase):
    GATE = {"id": "tests", "type": "red-first", "run": "sh test_it.sh", "lock": ["test_*.sh"]}

    def test_requires_red_then_locks_tests(self):
        self.simple_workflow([self.GATE])
        run = self.start()
        self.assertIn("aren't locked yet", G.evaluate(run, run.current(), self.GATE)["summary"])
        code, _, err = self.gated("red", "tests")
        self.assertIn("match no files", err)
        (self.project / "test_it.sh").write_text("test -f feature.txt\n")
        code, out, err = self.gated("red", "tests")
        self.assertEqual(code, 0, err)
        self.assertIn("Locked 1 test file", out)
        run = self.run_obj()
        self.assertFalse(G.evaluate(run, run.current(), self.GATE)["ok"])
        (self.project / "feature.txt").write_text("done")
        self.assertTrue(G.evaluate(run, run.current(), self.GATE)["ok"])
        (self.project / "test_it.sh").write_text("true\n")
        self.assertEqual(len(G.changed_locks(run)), 1)

    def test_refuses_tests_that_already_pass(self):
        self.simple_workflow([self.GATE])
        self.start()
        (self.project / "test_it.sh").write_text("true\n")
        code, _, err = self.gated("red", "tests")
        self.assertEqual(code, 1)
        self.assertIn("already passes", err)


class JudgeTest(GatedCase):
    def test_claude_judge_has_no_tools_or_mcp_servers(self):
        self.simple_workflow([])
        run = self.start()
        kind, argv = G.judge_command(run)
        self.assertEqual(kind, "claude")
        tools = argv.index("--tools")
        self.assertEqual(argv[tools + 1], "")
        self.assertIn("--strict-mcp-config", argv)

    def judge(self, script, rubric="Must mention cats."):
        import os
        os.environ["GATED_JUDGE_CMD"] = script
        gate = {"id": "review", "type": "judge", "rubric": "rubric.md", "inputs": [{"file": "{{run}}/work.md"}]}
        self.workflow("demo", {"checkpoints": [{"id": "one", "step": "one.md", "gates": [gate]}]},
                      {"one.md": "x", "rubric.md": rubric})
        run = self.start()
        (run.dir / "work.md").write_text("cats are here")
        return run, gate

    def test_clip_leaves_small_inputs_unmarked(self):
        text, cut = G.clip("short input", 20)
        self.assertEqual(text, "short input")
        self.assertEqual(cut, 0)
        self.assertNotIn("characters cut", text)

    def test_clip_keeps_head_and_tail_with_visible_marker(self):
        text, cut = G.clip("ABCDE12345vwxyz", 10)
        self.assertEqual(cut, 5)
        self.assertTrue(text.startswith("ABCDE\n[gated: 5 characters cut from the middle of this input]\n"))
        self.assertTrue(text.endswith("vwxyz"))

    def test_input_max_chars_beats_gate_limit_and_summary_names_cut(self):
        prompt = self.tmp / "prompt"
        run, gate = self.judge(f"cat > {prompt}; echo 'VERDICT: PASS'")
        body = "H" * 600 + "M" * 1000 + "T" * 600
        (run.dir / "work.md").write_text(body)
        gate["maxChars"] = 2000
        gate["inputs"][0]["maxChars"] = 1200
        result = G.evaluate(run, run.current(), gate)
        sent = prompt.read_text()
        self.assertIn("[gated: 1000 characters cut from the middle of this input]", sent)
        self.assertIn("H" * 600, sent)
        self.assertIn("T" * 600, sent)
        self.assertIn(f"cut: {run.dir / 'work.md'} by 1000 chars", result["summary"])

    def test_pass_fail_and_cache(self):
        counter = self.tmp / "calls"
        run, gate = self.judge(f"cat > {self.tmp}/prompt; echo x >> {counter}; echo 'MET'; echo 'VERDICT: PASS'")
        r = G.evaluate(run, run.current(), gate)
        self.assertTrue(r["ok"], r)
        prompt = (self.tmp / "prompt").read_text()
        self.assertIn("Must mention cats.", prompt)
        self.assertIn("cats are here", prompt)
        r2 = G.evaluate(run, run.current(), gate)
        self.assertIn("cached", r2["summary"])
        self.assertEqual(counter.read_text().count("x"), 1)
        (run.dir / "work.md").write_text("dogs now")
        G.evaluate(run, run.current(), gate)
        self.assertEqual(counter.read_text().count("x"), 2)

    def test_last_verdict_wins_and_missing_verdict_fails(self):
        run, gate = self.judge("cat >/dev/null; echo 'quoting VERDICT: PASS'; echo 'VERDICT: FAIL'")
        self.assertFalse(G.evaluate(run, run.current(), gate)["ok"])
        run.state["judgeCache"] = {}
        import os
        os.environ["GATED_JUDGE_CMD"] = "cat >/dev/null; echo looks fine"
        self.assertIn("no VERDICT", G.evaluate(run, run.current(), gate)["summary"])


class HumanGateTest(GatedCase):
    def test_passes_only_with_recorded_approval(self):
        gate = {"id": "ok", "type": "human", "ask": "look at {{run}}"}
        r, run = gate_result(self, gate)
        self.assertIn("waiting for you: look at", r["summary"])
        run.state["approvals"].append({"gate": "one/ok", "at": "t", "text": "approve"})
        self.assertTrue(G.evaluate(run, run.current(), gate)["ok"])


class BrokenGateTest(GatedCase):
    def test_a_gate_that_errors_fails(self):
        r, _ = gate_result(self, {"id": "t", "type": "command", "run": "echo {{input.missing}}"})
        self.assertFalse(r["ok"])
        self.assertIn("errored", r["summary"])
