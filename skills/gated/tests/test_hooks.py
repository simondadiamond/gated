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

    def test_own_cli_is_approved_without_a_prompt(self):
        from gated_lib.core import SKILL_DIR
        from gated_lib.hooks import is_own_cli
        run = self.run_obj()
        run.state["harness"] = "claude"
        run.save()
        cli = SKILL_DIR / "bin" / "gated"
        _, out, _ = self.pre("Bash", {"command": f'python3 "{cli}" step'})
        self.assertIn('"permissionDecision": "allow"', out)
        self.assertEqual(self.pre("Bash", {"command": f'python3 "{cli}" step; rm -rf x'})[1], "")
        self.assertEqual(self.pre("Bash", {"command": f'python3 "{cli}" step > /tmp/brief.md'})[1], "")
        self.assertFalse(is_own_cli("python3 /tmp/elsewhere/gated step"))
        self.assertTrue(is_own_cli(f"{cli} check"))

    def test_quoted_greater_than_is_not_an_edit(self):
        # Live run 2026-09-30: a read-only gh search flagged the orchestrator as editing files.
        from gated_lib.hooks import edits_files
        self.assertFalse(edits_files('gh pr list --search "merged:>=2026-09-23" --json number'))
        self.assertFalse(edits_files("python3 -c \"print(1 > 0)\""))
        self.assertTrue(edits_files('echo "x" > out.md'))
        self.assertTrue(edits_files("sed -i 's/a/b/' f.md"))

    def test_redirect_into_a_temp_folder_is_not_an_edit(self):
        # Live run 2026-10-01: `gated step > /tmp/brief.md` failed fresh-context for good.
        from gated_lib.hooks import edits_files
        self.assertFalse(edits_files("cd /repo; python3 bin/gated step > /tmp/gated-step.md"))
        self.assertFalse(edits_files("gated check >> /private/tmp/check.log 2>&1"))
        self.assertFalse(edits_files('gated step > $TMPDIR/brief.md'))
        self.assertTrue(edits_files("gated step > brief.md"))
        self.assertTrue(edits_files("echo x > /tmp/a; echo y > src/b.ts"))
        # Same day: `gated step > .gated/runs/<id>/brief.md`, the step's own workspace.
        self.assertFalse(edits_files("python3 bin/gated step > .gated/runs/story-1/brief.md"))
        self.assertTrue(edits_files("gated step > .gated/brief.md"))
        # Quotes and stderr redirects, the same as bare paths.
        self.assertFalse(edits_files('gated step > "$TMPDIR/brief.md"'))
        self.assertFalse(edits_files('gated step > ".gated/runs/story-1/brief.md"'))
        self.assertFalse(edits_files("gated step 2>/tmp/err.log"))
        # A path that leaves the folder, or can't be read safely, is still an edit.
        self.assertTrue(edits_files("echo x > /tmp/../repo/src/a.ts"))
        self.assertTrue(edits_files("echo x > .gated/runs/r/../../../src/a.ts"))
        self.assertTrue(edits_files("echo x > ../../.gated/runs/r/a.md"))
        self.assertTrue(edits_files("echo x > /tmp/$(cp a src/b)"))
        self.assertTrue(edits_files("echo x > /tmpfoo/a"))

    def test_separators_inside_quotes_do_not_split_the_command(self):
        # Live run 2026-10-06: python code piped from `gated step` holds `;` and a newline, which
        # cut the quoted string in half, so its `j>0` counted as a redirect.
        from gated_lib.hooks import edits_files
        self.assertFalse(edits_files(
            'python3 "/Users/x/.claude/plugins/cache/gated/gated/0.7.0/skills/gated/bin/gated" step | python3 -c "\n'
            "import sys;t=sys.stdin.read();i=t.find('Nothing is narrowed. Ledger row 37');"
            "j=t.find('. ',t.find('instead of an unrecorded brief'))\n"
            "print(t[:300]);print('...[planning history omitted]...');print(t[j+2:] if j>0 else t)\""))
        self.assertFalse(edits_files("""python3 -c 'a=1; print(a > 0)' && echo "x; y > z" || true"""))
        self.assertFalse(edits_files('echo "a && b > c\nd"'))
        # A redirect outside the quotes is still an edit, wherever the quotes sit.
        self.assertTrue(edits_files('python3 -c "import sys; print(1)" > out.md'))
        self.assertTrue(edits_files("""echo 'a; b' && echo x > src/a.ts"""))
        # An unclosed quote hides nothing: the rest is read as plain text.
        self.assertTrue(edits_files("""echo "a; echo x > src/a.ts"""))
        self.assertTrue(edits_files("""echo \\' ; echo x > src/a.ts ; echo \\'"""))
        # An escaped quote opens nothing either.
        self.assertTrue(edits_files("""echo \\' x > src/a.ts \\'"""))
        self.assertFalse(edits_files("""echo a \\> b"""))

    def test_pipe_from_gated_into_a_reader_is_not_an_edit(self):
        from gated_lib.hooks import edits_files
        self.assertFalse(edits_files('python3 bin/gated step | python3 -c "import sys; print(sys.stdin.read()[:9])"'))
        self.assertFalse(edits_files("gated status | jq '.checkpoints[] | select(.n > 1)'"))
        self.assertFalse(edits_files("gated step | sed -n '1,40p'"))
        self.assertFalse(edits_files("gated step | head -50; gated check | tail -5"))
        # Each stage is judged on its own: a write after the pipe is still an edit.
        self.assertTrue(edits_files("gated step | tee brief.md"))
        self.assertTrue(edits_files("gated step | sed -i s/a/b/ f.md"))
        self.assertTrue(edits_files("gated step | python3 -c 'print(1)' > out.md"))

    def test_heredoc_body_is_text_not_commands(self):
        # Live run 2026-10-07: a person's answer quoted into /tmp with a heredoc failed the plan. Its
        # lines start with '> ' and one says "no new install warning", which read as edits.
        from gated_lib.hooks import edits_files
        live = ("cat > /tmp/c2710.md <<'EOF'\nDecision on AC-8, quoted:\n\n> AC-8: two cases.\n"
                "> (2) Vous n'êtes pas connecté, so Chrome shows no new install warning.\nEOF\n"
                "gh issue comment 2710 --body-file /tmp/c2710.md")
        self.assertFalse(edits_files(live))
        self.assertFalse(edits_files("cat <<EOF > /tmp/a.md\nrm -rf src && git commit -m x\nEOF"))
        self.assertFalse(edits_files('cat > "$TMPDIR/a.md" <<"END"\ncp a b\nEND'))
        self.assertFalse(edits_files("cat > /tmp/a.md <<-EOF\n\t> quoted\n\tEOF\ngated status"))
        self.assertFalse(edits_files("python3 - <<'PY'\nprint(1 > 0)\nPY"))
        # A heredoc's target outside the scratch folders is still an edit.
        self.assertTrue(edits_files("cat > src/a.ts <<'EOF'\nexport {}\nEOF"))
        self.assertTrue(edits_files("cat <<EOF > src/a.ts\nx\nEOF"))
        # A heredoc fed to a shell is commands, so its body is still read.
        self.assertTrue(edits_files("bash <<'EOF'\nrm src/a.ts\nEOF"))
        self.assertTrue(edits_files("ssh box sh -s <<EOF\nmv a b\nEOF"))
        self.assertTrue(edits_files("cat <<'EOF' | bash\nrm src/a.ts\nEOF"))
        # A shift in arithmetic is not a heredoc, so the next line is still a command.
        self.assertTrue(edits_files("x=$((1<<4))\nrm src/a.ts"))
        # What follows the end marker is a command again.
        self.assertTrue(edits_files("cat > /tmp/a.md <<'EOF'\ntext\nEOF\ncp /tmp/a.md src/a.md"))
        # A body never closed runs to the end, like the shell reads it.
        self.assertFalse(edits_files("cat > /tmp/a.md <<EOF\nrm x"))
        # '<<' inside quotes and a here-string are not heredocs.
        self.assertTrue(edits_files("echo 'a << EOF'\nrm src/a.ts\nEOF"))
        self.assertTrue(edits_files("grep x <<< EOF\nrm src/a.ts\nEOF"))

    def test_removing_or_copying_scratch_files_is_not_an_edit(self):
        # Live run 2026-10-06: `rm -f /tmp/cp1.txt && gated step` failed fresh-context.
        from gated_lib.hooks import edits_files
        self.assertFalse(edits_files('rm -f /tmp/cp1.txt && python3 "/x/bin/gated" step'))
        self.assertFalse(edits_files("rm -rf /private/tmp/a /tmp/b"))
        self.assertFalse(edits_files('rm "$TMPDIR/brief.md" ${TMPDIR}/x.md'))
        self.assertFalse(edits_files("mv /tmp/a.md .gated/runs/story-1/brief.md"))
        self.assertFalse(edits_files("cp -- /tmp/a.md /tmp/b.md 2>/dev/null"))
        self.assertFalse(edits_files("gated step | tee /tmp/brief.md"))
        self.assertFalse(edits_files("gated step | tee -a .gated/runs/story-1/brief.md > /dev/null"))
        # Any path outside the scratch folders makes it an edit.
        self.assertTrue(edits_files("rm src/x.ts"))
        self.assertTrue(edits_files("rm -f /tmp/a src/x.ts"))
        self.assertTrue(edits_files("cp /tmp/a.ts src/a.ts"))
        self.assertTrue(edits_files("mv src/a.ts /tmp/a.ts"))
        self.assertTrue(edits_files("tee file"))
        self.assertTrue(edits_files("tee /tmp/a > src/a.ts"))
        self.assertTrue(edits_files("rm"))
        # Paths that leave the folder, or can't be read safely, are edits too.
        self.assertTrue(edits_files("rm /tmp/../repo/src/a.ts"))
        self.assertTrue(edits_files("rm .gated/runs/r/../../../src/a.ts"))
        self.assertTrue(edits_files("rm /tmp/$(cp a src/b)"))
        self.assertTrue(edits_files("rm /tmpfoo/a"))
        self.assertTrue(edits_files("rm '/tmp/a' \"/tmp/b"))
        self.assertTrue(edits_files("rm '$TMPDIR/a'"))
        self.assertTrue(edits_files("rm /tmp/a > src/a.ts"))
        # The other edits stay edits.
        self.assertTrue(edits_files("echo x > file"))
        self.assertTrue(edits_files("cat a > b.ts"))
        self.assertTrue(edits_files("git commit -m 'wip'"))
        self.assertTrue(edits_files("ln -s /tmp/a /tmp/b"))

    def test_saved_brief_logs_no_edit(self):
        self.assertEqual(self.pre("Bash", {"command": "python3 bin/gated step > /tmp/brief.md"})[0], 0)
        row = json.loads((self.run.dir / "activity.jsonl").read_text().splitlines()[-1])
        self.assertFalse(row["edit"])

    def test_temp_and_run_folder_redirects_are_still_guarded(self):
        # The edit exemption must not reach guard(): a lock or the run state stays denied by name.
        self.assertEqual(self.pre("Bash", {"command": f"echo x > /tmp/..{self.locked}"})[0], 2)
        self.assertEqual(self.pre("Bash", {"command": f"gated step > {self.run.dir}/state.json"})[0], 2)
        self.assertEqual(self.pre("Bash", {"command": f"gated step > {self.run.dir}/brief.md"})[0], 0)
        self.assertEqual(self.pre("Bash", {"command": f"rm -f {self.run.dir}/state.json"})[0], 2)

    def test_shell_writes_to_locked_file(self):
        self.assertEqual(self.pre("Bash", {"command": f"echo x > {self.locked}"})[0], 2)
        self.assertEqual(self.pre("Bash", {"command": "sed -i '' s/a/b/ .claude/workflows/demo/workflow.json"})[0], 2)
        self.assertEqual(self.pre("Bash", {"command": f"cat {self.locked}"})[0], 0)
        self.assertEqual(self.pre("Bash", {"command": "python3 gated status > /tmp/x; gated check"})[0], 0)


class PromptApprovalTest(GatedCase):
    def test_bare_yes_does_not_approve_but_approve_does(self):
        self.workflow("story", {"plan": {"step": "p.md"}}, {"p.md": "plan"})
        run = self.start("story", session="owner")
        (run.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": [
            {"id": "build", "instructions": "build", "gates": []}
        ]}))
        self.submit_plan()
        self.hook("prompt", {"session_id": "owner", "prompt": "yes"})
        self.assertEqual(self.run_obj().status, "awaiting-approval")
        self.hook("prompt", {"session_id": "owner", "prompt": "approve"})
        self.assertEqual(self.run_obj().status, "running")


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
            # an event may carry several entries (PostToolUse: Bash and AskUserQuestion)
            entries = [e for e in data["hooks"][event] if e.get("matcher", "") == matcher]
            self.assertEqual(len(entries), 1, f"{event}/{matcher!r} must appear exactly once in hooks.json")
            self.assertTrue(entries[0]["hooks"][0]["command"].endswith(f"hook {kind}"))
        plugin_count = sum(len(v) for v in data["hooks"].values())
        self.assertEqual(plugin_count, len(install.EVENTS["claude"]), "hooks.json has an entry the installer lacks")
