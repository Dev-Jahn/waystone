"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class DelegateVerifyTests(unittest.TestCase):
    """0.8.0 M2 §11/§12 — same-base independent verifier transport (synthetic only)."""

    def setUp(self):
        self.original_fingerprint = delegate._codex_runner_fingerprint
        delegate._codex_runner_fingerprint = _synthetic_codex_fingerprint

    def tearDown(self):
        delegate._codex_runner_fingerprint = self.original_fingerprint

    _PROFILE = (
        "schema: waystone-profile-1\nbindings:\n"
        "  implementer: {execution: external-runner, backend: \"codex:gpt-5.6-sol\"}\n"
        "  verifier: {backend: \"codex:gpt-5.6-sol\"}\n")

    def _setup(self, d, *, committed=True):
        root, home = _deleg_project(d)
        _write_profile(root, self._PROFILE)
        (root / ".gitignore").write_text(".ignored-cache/\n")
        git(root, "add", ".gitignore")
        git(root, "commit", "-qm", "ignore fixture")

        def runner(worktree, model, prompt_path, record_dir):
            (worktree / "f.txt").write_text("delegate result\n")
            (worktree / "new.txt").write_text("new result\n")
            (worktree / "blob.bin").write_bytes(bytes(range(256)))
            (record_dir / "last_message.md").write_text("summary")
            if committed:
                git(worktree, "add", "-A")
                git(worktree, "commit", "-qm", "delegate local commit")
            return (0, 0.1)

        _deleg_run(root, home, runner)
        rec = _latest_rec(root, home)
        worktree = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
        ignored = worktree / ".ignored-cache" / "keep.txt"
        ignored.parent.mkdir()
        ignored.write_text("keep")
        return root, home, rec, worktree, None

    def _with_claude_verifier(self, fake, fn):
        orig = delegate._run_claude_verifier
        delegate._run_claude_verifier = fake
        try:
            return fn()
        finally:
            delegate._run_claude_verifier = orig

    def _with_codex_verifier(self, fake, fn):
        orig = delegate._run_codex_verifier
        delegate._run_codex_verifier = fake
        try:
            return fn()
        finally:
            delegate._run_codex_verifier = orig

    def test_verifier_prompt_is_rendered_from_waystone_template(self):
        with tempfile.TemporaryDirectory() as d:
            rec = Path(d)
            (rec / "packet.yaml").write_text(yaml.safe_dump({
                "acceptance": ["criterion one", "criterion two"],
            }))
            prompt = delegate._render_verifier_prompt(rec, {
                "changed_files": [{"status": "M", "path": "src/example.py"}],
            })
            self.assertIn("criterion one", prompt)
            self.assertIn("criterion two", prompt)
            self.assertIn("M src/example.py", prompt)
            self.assertIn("independent adversarial verifier", prompt)

    def test_codex_verify_artifact_records_requested_and_effective_effort(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _plugin = self._setup(d, committed=False)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n"
                "  verifier: {backend: 'codex:gpt-test', effort: xhigh}\n"))

            def fake(_worktree, _model, _prompt, _record_dir, *, effort=None):
                self.assertEqual(effort, "xhigh")
                return (0, _json.dumps({
                    "summary": "checked", "findings": [], "limitations": [],
                }))

            _run_with_home(home, lambda: self._with_codex_verifier(
                fake, lambda: delegate.verify_delegation(root, rec.name)))
            artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
            self.assertEqual(artifact["requested_effort"], "xhigh")
            self.assertEqual(artifact["effective_effort"], "xhigh")

    def test_claude_verifier_transport_and_schema_artifact(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _plugin = self._setup(d, committed=False)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n"
                "  verifier: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            payload = {
                "summary": "checked", "findings": [], "limitations": [],
            }
            calls = []

            def fake(worktree, model, focus, record_dir):
                calls.append((worktree, model, focus, record_dir))
                return (0, _json.dumps(payload))

            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: self._with_claude_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(rc, 0)
            self.assertEqual(calls[0][1], "sonnet")
            artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
            self.assertEqual(artifact["transport"], "claude-print:read-only")
            self.assertEqual(artifact["backend"], "claude:sonnet")
            self.assertEqual(artifact["payload"], payload)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(artifact["profile_fingerprint"],
                             delegate._load_profile(root)[1])
            self.assertEqual(artifact["base_sha"], contract["base_sha"])
            self.assertEqual(artifact["result_sha"], contract["result_sha"])
            self.assertEqual(artifact["effective_tool_policy"], {
                "tools": ["Read", "Glob", "Grep"], "bash": False,
                "filesystem_postcondition": "git-status+untracked-content-unchanged",
            })
            for delta in ("filesystem", "process", "network"):
                self.assertIn(delta, err.getvalue().lower())

    def test_run_claude_verifier_transport_is_injectable_and_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "review-worktree"
            record = root / "record"
            worktree.mkdir()
            record.mkdir()
            calls = []
            payload = {"summary": "ok", "findings": [], "limitations": []}

            def transport(cmd, **kwargs):
                calls.append((cmd, kwargs))
                wrapped = {"type": "result", "subtype": "success",
                           "structured_output": payload}
                return subprocess.CompletedProcess(cmd, 0, stdout=_json.dumps(wrapped), stderr="")

            rc, output = delegate._run_claude_verifier(
                worktree, "sonnet", "review", record, runner=transport)
            self.assertEqual(rc, 0)
            self.assertEqual(_json.loads(output), payload)
            cmd, kwargs = calls[0]
            self.assertIn("--json-schema", cmd)
            self.assertIn("--permission-mode", cmd)
            self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "dontAsk")
            denied = cmd[cmd.index("--disallowedTools") + 1]
            for tool in ("Edit", "Write", "Bash", "WebFetch", "WebSearch"):
                self.assertIn(tool, denied)
            allowed = cmd[cmd.index("--allowedTools") + 1]
            self.assertNotIn("Bash", allowed)
            self.assertNotIn("Bash", cmd[cmd.index("--tools") + 1])
            self.assertEqual(Path(kwargs["cwd"]), worktree)
            self.assertEqual(kwargs["env"]["WAYSTONE_VERIFIER_SESSION"], "1")
            cache = Path(kwargs["env"]["UV_CACHE_DIR"])
            self.assertEqual(cache, record / "runtime" / "uv-cache")
            self.assertFalse(cache.resolve().is_relative_to(worktree.resolve()))

    def test_codex_exec_argv_schema_effort_guard_and_record_local_cache(self):
        import types

        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "review-worktree"
            record = root / "record"
            worktree.mkdir()
            record.mkdir()
            calls = []
            original = delegate.subprocess.run
            payload = {"summary": "checked", "findings": [], "limitations": []}

            def fake(cmd, **kwargs):
                calls.append((cmd, kwargs))
                output = Path(cmd[cmd.index("--output-last-message") + 1])
                output.write_text(_json.dumps(payload))
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            try:
                rc, output = delegate._run_codex_verifier(
                    worktree, "gpt-test", "review prompt", record, effort="xhigh")
            finally:
                delegate.subprocess.run = original
            self.assertEqual(rc, 0)
            self.assertEqual(_json.loads(output), payload)
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[:2], ["/opt/waystone-test/bin/codex", "exec"])
            self.assertEqual(cmd[cmd.index("-C") + 1], str(worktree))
            self.assertEqual(cmd[cmd.index("-s") + 1], "read-only")
            self.assertEqual(
                cmd[cmd.index("--output-schema") + 1], str(delegate._VERIFY_SCHEMA_PATH))
            self.assertEqual(
                cmd[cmd.index("--output-last-message") + 1],
                str(record / "verify-last-message.json"))
            self.assertIn('model_reasoning_effort="xhigh"', cmd)
            self.assertEqual(kwargs["input"], "review prompt")
            env = kwargs["env"]
            self.assertEqual(env["WAYSTONE_VERIFIER_SESSION"], "1")
            cache = Path(env["UV_CACHE_DIR"])
            self.assertEqual(cache, record / "runtime" / "uv-cache")
            self.assertFalse(cache.resolve().is_relative_to(worktree.resolve()))

    def test_codex_verifier_timeout_and_abnormal_exit_preserve_only_current_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            worktree = root / "review-worktree"
            record = root / "record"
            worktree.mkdir()
            record.mkdir()
            (record / "verify-last-message.json").write_text("STALE OUTPUT")
            (record / "verify-codex.jsonl").write_text("STALE JSONL")
            (record / "verify.stderr").write_text("STALE STDERR")
            original = delegate.subprocess.run

            def timeout(cmd, **kwargs):
                kwargs["stdout"].write("CURRENT EVENT\n")
                kwargs["stderr"].write("current timeout cause\n")
                raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

            delegate.subprocess.run = timeout
            try:
                rc, output = delegate._run_codex_verifier(
                    worktree, "gpt-test", "review prompt", record)
            finally:
                delegate.subprocess.run = original

            self.assertEqual(rc, 124)
            self.assertEqual(output, "")
            self.assertFalse((record / "verify-last-message.json").exists())
            self.assertEqual((record / "verify-codex.jsonl").read_text(), "CURRENT EVENT\n")
            stderr = (record / "verify.stderr").read_text()
            self.assertIn("current timeout cause", stderr)
            self.assertIn("timed out", stderr)
            self.assertNotIn("STALE", stderr)

            (record / "verify-last-message.json").write_text("STALE RETRY OUTPUT")

            def killed(cmd, **kwargs):
                kwargs["stdout"].write("RETRY EVENT\n")
                kwargs["stderr"].write("killed by supervisor\n")
                return subprocess.CompletedProcess(cmd, -9)

            delegate.subprocess.run = killed
            try:
                rc, output = delegate._run_codex_verifier(
                    worktree, "gpt-test", "retry prompt", record)
            finally:
                delegate.subprocess.run = original

            self.assertEqual(rc, -9)
            self.assertEqual(output, "")
            self.assertFalse((record / "verify-last-message.json").exists())
            self.assertEqual((record / "verify-codex.jsonl").read_text(), "RETRY EVENT\n")
            self.assertEqual((record / "verify.stderr").read_text(), "killed by supervisor\n")

    def test_codex_verifier_rejects_missing_empty_and_whitespace_output(self):
        import types

        variants = ((None, "missing"), ("", "empty"), (" \n\t", "whitespace"))
        for content, label in variants:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                worktree = root / "review-worktree"
                record = root / "record"
                worktree.mkdir()
                record.mkdir()
                (record / "verify-last-message.json").write_text("STALE VALID OUTPUT")
                (record / "verify-codex.jsonl").write_text("STALE JSONL")
                (record / "verify.stderr").write_text("STALE STDERR")
                original = delegate.subprocess.run

                def fake(cmd, **_kwargs):
                    if content is not None:
                        Path(cmd[cmd.index("--output-last-message") + 1]).write_text(content)
                    return types.SimpleNamespace(returncode=0)

                delegate.subprocess.run = fake
                try:
                    rc, output = delegate._run_codex_verifier(
                        worktree, "gpt-test", "review prompt", record)
                finally:
                    delegate.subprocess.run = original

                self.assertEqual(rc, 65)
                self.assertEqual(output, "")
                self.assertEqual((record / "verify-codex.jsonl").read_text(), "")
                stderr = (record / "verify.stderr").read_text()
                self.assertIn("empty verifier output", stderr)
                self.assertNotIn("STALE", stderr)

    def test_codex_verifier_retry_cleanup_failure_is_fail_loud_without_stale_diagnostic(self):
        import builtins
        from unittest import mock

        for filename in ("verify-codex.jsonl", "verify.stderr"):
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                worktree = root / "review-worktree"
                record = root / "record"
                worktree.mkdir()
                record.mkdir()
                failed_path = record / filename
                (record / "verify.stderr").write_text("STALE SECRET STDERR")
                (record / "verify-codex.jsonl").write_text("STALE JSONL")
                (record / "verify-last-message.json").write_text("STALE OUTPUT")
                original_open = builtins.open
                calls = {"runner": 0}

                def fail_truncate(file, mode="r", *args, **kwargs):
                    if Path(file) == failed_path and mode == "w":
                        raise OSError(f"injected {filename} truncate failure")
                    return original_open(file, mode, *args, **kwargs)

                def runner(*_args, **_kwargs):
                    calls["runner"] += 1

                with mock.patch("builtins.open", fail_truncate), \
                        mock.patch.object(delegate.subprocess, "run", runner):
                    with self.assertRaises(delegate.WorkflowError) as cm:
                        delegate._run_codex_verifier(
                            worktree, "gpt-test", "review prompt", record)

                message = str(cm.exception)
                self.assertIn("cannot prepare Codex verifier transport file", message)
                self.assertIn(f"injected {filename} truncate failure", message)
                self.assertNotIn("STALE SECRET STDERR", message)
                self.assertNotIn("STALE JSONL", message)
                self.assertEqual(calls["runner"], 0)

    def test_verifier_failure_diagnostic_includes_stderr_timeout_and_signal(self):
        cases = (
            (23, "transport rejected request", ("rc 23", "transport rejected request")),
            (124, "codex verifier timed out", ("rc 124", "timed out")),
            (-9, "worker killed", ("signal 9 SIGKILL", "worker killed")),
        )
        for rc, stderr, expected in cases:
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as d:
                root, home, rec, _worktree, _unused = self._setup(d, committed=False)

                def fake(_wt, _model, _focus, record_dir):
                    (record_dir / "verify.stderr").write_text(stderr)
                    return (rc, "")

                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: self._with_codex_verifier(
                        fake, lambda: delegate.verify_delegation(root, rec.name)))
                message = str(cm.exception)
                for part in expected:
                    self.assertIn(part, message)
                if rc < 0:
                    self.assertNotIn("rc -9", message)

    def test_claude_verifier_requires_success_structured_output(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            payload = {"summary": "ok", "findings": [], "limitations": []}
            envelopes = (
                {"type": "result", "structured_output": payload},
                {"type": "result", "subtype": "success", "result": _json.dumps(payload)},
                payload,
            )
            for envelope in envelopes:
                with self.subTest(envelope=envelope):
                    def transport(cmd, **kwargs):
                        return subprocess.CompletedProcess(
                            cmd, 0, stdout=_json.dumps(envelope), stderr="")

                    rc, output = delegate._run_claude_verifier(
                        root, "sonnet", "review", root, runner=transport)
                    self.assertNotEqual(rc, 0)
                    self.assertEqual(output, "")

    def test_success_normalizes_committed_delegate_and_preserves_labels(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _unused = self._setup(d, committed=True)
            calls = []

            def fake(wt, model, prompt, record_dir):
                calls.append((wt, model, prompt, record_dir))
                return (0, _json.dumps({
                    "summary": "challenged", "findings": [], "limitations": []}))

            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(rc, 0)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(git(worktree, "rev-parse", "HEAD").stdout.strip(), contract["base_sha"])
            self.assertEqual((worktree / "f.txt").read_text(), "delegate result\n")
            self.assertEqual((worktree / "blob.bin").read_bytes(), bytes(range(256)))
            self.assertTrue((worktree / ".ignored-cache" / "keep.txt").exists())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], worktree)
            self.assertEqual(calls[0][1], "gpt-5.6-sol")
            self.assertIn("independent adversarial verifier", calls[0][2])
            self.assertEqual(calls[0][3], rec)
            artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
            self.assertEqual(artifact["schema"], "waystone-verify-1")
            self.assertEqual(artifact["backend"], "codex:gpt-5.6-sol")
            self.assertEqual(artifact["provenance"], "independent-verifier")
            self.assertEqual(artifact["payload"]["summary"], "challenged")
            self.assertIsNone(artifact["requested_effort"])
            self.assertIsNone(artifact["effective_effort"])

    def test_verify_session_hook_does_not_seed_state_in_review_worktree(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _unused = self._setup(d, committed=False)
            legacy = home / ".codex" / "waystone.pre-0.9" / "profile.yml"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(self._PROFILE)
            legacy_mtime = legacy.stat().st_mtime_ns
            hooks = [
                SCRIPTS.parent / "hooks" / "scripts" / "session_context.sh",
                SCRIPTS.parent / "hooks" / "scripts" / "resume_snapshot.sh",
            ]
            self.assertFalse((worktree / ".waystone").exists())

            def fake(_worktree, _model, _prompt, record_dir):
                env = delegate._verifier_env(record_dir)
                for hook in hooks:
                    result = subprocess.run(
                        ["bash", str(hook)], input=_json.dumps({"cwd": str(worktree)}),
                        capture_output=True, text=True, env=env,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                return (0, _json.dumps({
                    "summary": "checked", "findings": [], "limitations": [],
                }))

            rc = _run_with_home(home, lambda: self._with_codex_verifier(
                fake, lambda: delegate.verify_delegation(root, rec.name)))

            self.assertEqual(rc, 0)
            self.assertFalse((worktree / ".waystone").exists())
            self.assertEqual(legacy.read_text(), self._PROFILE)
            self.assertEqual(legacy.stat().st_mtime_ns, legacy_mtime)
            self.assertTrue((rec / "artifact" / "verify-1.json").is_file())

    def test_manifest_hook_matrix_covers_normal_and_verifier_modes(self):
        manifest = _json.loads(
            (SCRIPTS.parent / "hooks" / "hooks.json").read_text())["hooks"]
        expected_worktree_writes = {
            "PreToolUse": set(),
            "SessionStart": {".waystone/.gitignore", ".waystone/lock"},
            "PreCompact": {".waystone/resume.md", ".waystone/.gitignore"},
            "SessionEnd": {".waystone/resume.md", ".waystone/.gitignore"},
            "PostToolUse": {".waystone/.gitignore", ".waystone/lock", "ROADMAP.md"},
            "Stop": {".waystone/.gitignore", ".waystone/lock"},
        }
        expected_home_writes = {
            "PreToolUse": set(),
            "SessionStart": {".waystone", ".waystone/registry.lock"},
            "PreCompact": set(),
            "SessionEnd": set(),
            "PostToolUse": set(),
            "Stop": {".waystone", ".waystone/registry.lock"},
        }
        mutation = os.environ.get("WAYSTONE_TEST_NOOP_HOOK")
        mutation_seen = False
        uv_env = os.environ.copy()
        uv_env.pop("FORCE_COLOR", None)
        uv_env.pop("CLICOLOR_FORCE", None)
        uv_env["NO_COLOR"] = "1"
        uv_cache_dir = subprocess.run(
            ["uv", "cache", "dir"], capture_output=True, text=True, check=True, env=uv_env,
        ).stdout.strip()
        self.assertTrue(Path(uv_cache_dir).is_absolute())

        def tree_snapshot(
                base: Path, *, skip_git: bool = False,
        ) -> dict[str, tuple]:
            import stat as _stat

            snapshot: dict[str, tuple] = {}
            for path in base.rglob("*"):
                rel = path.relative_to(base)
                if skip_git and ".git" in rel.parts:
                    continue
                info = path.lstat()
                if _stat.S_ISLNK(info.st_mode):
                    snapshot[str(rel)] = ("symlink", info.st_mode, os.readlink(path))
                elif path.is_dir():
                    snapshot[str(rel)] = ("directory", info.st_mode, b"")
                else:
                    snapshot[str(rel)] = ("file", info.st_mode, path.read_bytes())
            return snapshot

        def changed_paths(before: dict, after: dict) -> set[str]:
            return {path for path in before.keys() | after.keys()
                    if before.get(path) != after.get(path)}

        entries = []
        for event, groups in manifest.items():
            for group_index, group in enumerate(groups):
                for hook_index, hook in enumerate(group["hooks"]):
                    entry_id = f"{event}[{group_index}].hooks[{hook_index}]"
                    entries.append((entry_id, event, hook["command"]))
        self.assertEqual(
            {event for _entry_id, event, _command in entries},
            {"PreToolUse", "SessionStart", "PreCompact", "SessionEnd", "PostToolUse", "Stop"},
        )

        for entry_id, event, manifest_command in entries:
            for verifier_session in (False, True):
                with self.subTest(entry=entry_id, verifier_session=verifier_session), \
                        tempfile.TemporaryDirectory() as d:
                    base = Path(d)
                    root = base / "worktree"
                    home = base / "home"
                    root.mkdir()
                    home.mkdir()
                    init_repo(root)
                    (root / ".gitignore").write_text(".waystone/\n")
                    (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
                    (root / "tasks.yaml").write_text(
                        "version: 1\nproject: demo\nmilestones: []\ntasks: []\n")
                    (root / "ROADMAP.md").write_text("stale roadmap\n")
                    git(root, "add", "-A")
                    git(root, "commit", "-qm", "hook fixture")
                    marker = root / ".waystone" / "boundary-hooks-enabled"
                    marker.parent.mkdir()
                    marker.touch()

                    payloads = {
                        "PreToolUse": {
                            "hook_event_name": "PreToolUse", "tool_name": "Read",
                            "cwd": str(root),
                            "tool_input": {"file_path": str(root / "tasks.yaml")},
                        },
                        "SessionStart": {
                            "hook_event_name": "SessionStart", "source": "startup",
                            "cwd": str(root),
                        },
                        "PreCompact": {
                            "hook_event_name": "PreCompact", "trigger": "manual",
                            "cwd": str(root),
                        },
                        "SessionEnd": {
                            "hook_event_name": "SessionEnd", "reason": "other",
                            "cwd": str(root),
                        },
                        "PostToolUse": {
                            "hook_event_name": "PostToolUse", "tool_name": "Edit",
                            "cwd": str(root),
                            "tool_input": {"file_path": str(root / "tasks.yaml")},
                        },
                        "Stop": {"hook_event_name": "Stop", "cwd": str(root)},
                    }
                    env = {
                        "PATH": os.environ["PATH"],
                        "HOME": str(home),
                        "WAYSTONE_HOME": str(home / ".waystone"),
                        "CLAUDE_PLUGIN_ROOT": str(SCRIPTS.parent),
                        "UV_CACHE_DIR": uv_cache_dir,
                        "UV_OFFLINE": "1",
                        "PYTHONNOUSERSITE": "1",
                    }
                    if verifier_session:
                        env["WAYSTONE_VERIFIER_SESSION"] = "1"
                    self.assertNotEqual(env["HOME"], os.environ.get("HOME"))
                    self.assertEqual("WAYSTONE_VERIFIER_SESSION" in env, verifier_session)
                    worktree_before = tree_snapshot(root, skip_git=True)
                    home_before = tree_snapshot(home)
                    # Full git-aware manifest (status/HEAD/untracked/ignored/lstat) — the same
                    # capture the old hermetic assertion used; path snapshots alone miss
                    # git-only mutations.
                    git_state_before = delegate._verify_worktree_state(root)

                    command = manifest_command
                    if mutation == entry_id:
                        command = ":"
                        mutation_seen = True
                    result = subprocess.run(
                        ["bash", "-c", command], input=_json.dumps(payloads[event]),
                        cwd=root, capture_output=True, text=True, env=env, timeout=30,
                    )
                    worktree_after = tree_snapshot(root, skip_git=True)
                    home_after = tree_snapshot(home)

                    self.assertEqual(result.returncode, 0, result.stderr)
                    if verifier_session:
                        self.assertEqual(result.stdout, "")
                        self.assertEqual(result.stderr, "")
                        self.assertEqual(worktree_after, worktree_before)
                        self.assertEqual(home_after, home_before)
                        self.assertEqual(
                            delegate._verify_worktree_state(root), git_state_before)
                        if event == "SessionStart":
                            self.assertFalse((home / ".waystone" / "registry.lock").exists())
                        continue

                    self.assertEqual(result.stderr, "")
                    self.assertEqual(
                        changed_paths(worktree_before, worktree_after),
                        expected_worktree_writes[event],
                    )
                    self.assertEqual(
                        changed_paths(home_before, home_after), expected_home_writes[event])
                    # Normal-mode hooks may write declared files but never move git state.
                    git_state_after = delegate._verify_worktree_state(root)
                    self.assertEqual(git_state_after["head"], git_state_before["head"])
                    if event == "PostToolUse":
                        self.assertEqual(git_state_after["status"], b" M ROADMAP.md\x00")
                    else:
                        self.assertEqual(
                            git_state_after["status"], git_state_before["status"])
                    if event == "PreToolUse":
                        self.assertNotEqual(result.stdout, "")
                        output = _json.loads(result.stdout)
                        self.assertEqual(
                            output["hookSpecificOutput"]["permissionDecision"], "deny")
                    elif event == "SessionStart":
                        output = _json.loads(result.stdout)
                        hook_output = output["hookSpecificOutput"]
                        self.assertEqual(hook_output["hookEventName"], "SessionStart")
                        self.assertIn("[waystone] project: demo", hook_output["additionalContext"])
                        self.assertTrue((home / ".waystone" / "registry.lock").is_file())
                    elif event in {"PreCompact", "SessionEnd"}:
                        self.assertEqual(result.stdout, "")
                        resume_file = root / ".waystone" / "resume.md"
                        self.assertIn("captured_head:", resume_file.read_text())
                    elif event == "PostToolUse":
                        self.assertEqual(result.stdout, "")
                        self.assertIn("# Roadmap — demo", (root / "ROADMAP.md").read_text())
                    else:
                        self.assertEqual(result.stdout, "waystone check: no active-delta warnings\n")
        if mutation is not None:
            self.assertTrue(mutation_seen, f"unknown hook mutation target: {mutation}")

    def test_verifier_worktree_mutation_is_fail_loud_and_records_no_artifact(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _plugin = self._setup(d, committed=False)

            def fake(wt, _model, _prompt, _record_dir):
                (wt / "verifier-write.txt").write_text("forbidden\n")
                return (0, _json.dumps({
                    "summary": "mutated", "findings": [], "limitations": []}))

            with self.assertRaisesRegex(delegate.WorkflowError, "modified.*worktree"):
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_verifier_detects_content_change_to_existing_ignored_untracked_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, worktree, _plugin = self._setup(d, committed=False)

            def fake(wt, _model, _prompt, _record_dir):
                (wt / ".ignored-cache" / "keep.txt").write_text("mutated\n")
                return (0, _json.dumps({
                    "summary": "mutated", "findings": [], "limitations": []}))

            with self.assertRaisesRegex(delegate.WorkflowError, "modified.*worktree"):
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_invalid_verifier_payload_is_rejected_before_artifact_write(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _plugin = self._setup(d, committed=False)

            def fake(*_args):
                return (0, _json.dumps({"summary": "missing fields"}))

            with self.assertRaisesRegex(delegate.WorkflowError, "verify artifact schema"):
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

    def test_contract_empty_must_be_bool_before_normalization(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d, committed=False)
            contract_path = rec / "artifact" / "contract.yaml"
            contract = yaml.safe_load(contract_path.read_text())
            contract["empty"] = "false"
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, "{}")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertIn("empty", str(cm.exception))
            self.assertEqual(called["n"], 0)

    def test_contract_shas_are_strict_and_base_matches_exposure(self):
        cases = (
            ("base_sha", "short", "base_sha"),
            ("result_sha", "not-a-sha", "result_sha"),
            ("base_sha", "result", "exposure"),
        )
        for field, value, needle in cases:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as d:
                root, home, rec, _worktree, _unused = self._setup(d, committed=False)
                contract_path = rec / "artifact" / "contract.yaml"
                contract = yaml.safe_load(contract_path.read_text())
                contract[field] = contract["result_sha"] if value == "result" else value
                contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
                called = {"n": 0}

                def fake(*args):
                    called["n"] += 1
                    return (0, "{}")

                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: self._with_codex_verifier(
                        fake, lambda: delegate.verify_delegation(root, rec.name)))
                self.assertIn(needle, str(cm.exception))
                self.assertEqual(called["n"], 0)

    def test_contract_nonempty_requires_named_patch_file(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d, committed=False)
            (rec / "artifact" / "changes.patch").unlink()
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, "{}")

            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertIn("patch_file", str(cm.exception))
            self.assertEqual(called["n"], 0)

    def test_concurrent_verify_is_refused_by_record_lock(self):
        import contextlib
        import io
        import os
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d, committed=False)
            calls = {"n": 0}

            def fake(*args):
                calls["n"] += 1
                return (0, _json.dumps({"run": calls["n"]}))

            err = io.StringIO()
            with mock.patch.dict(os.environ, {"WAYSTONE_LOCK_TIMEOUT": "0.02"}, clear=False), \
                    common.hold_lock(rec / "record.lock", timeout=0.2), \
                    contextlib.redirect_stderr(err):
                rc = _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.main(
                        ["verify", rec.name, "--root", str(root)])))
            self.assertEqual(rc, 1)
            self.assertIn("record.lock is held", err.getvalue())
            self.assertEqual(calls["n"], 0)

    def test_unlocked_record_lock_marker_is_reused_and_preserved(self):
        import os

        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d, committed=False)
            lock = rec / "record.lock"
            lock.write_text("stale fixture\n")

            def fake(*args):
                return (0, _json.dumps({
                    "summary": "ok", "findings": [], "limitations": []}))

            rc = _run_with_home(home, lambda: self._with_codex_verifier(
                fake, lambda: delegate.main(["verify", rec.name, "--root", str(root)])))
            self.assertEqual(rc, 0)
            self.assertTrue(lock.exists())
            self.assertEqual(_json.loads(lock.read_text())["pid"], os.getpid())

    def test_verify_artifact_name_collision_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d, committed=False)
            sentinel = {"sentinel": True}
            injected = {"done": False}
            orig_paths = delegate._verify_paths

            def raced_paths(record_dir):
                paths = orig_paths(record_dir)
                if not injected["done"]:
                    injected["done"] = True
                    (record_dir / "artifact" / "verify-1.json").write_text(
                        _json.dumps(sentinel) + "\n")
                return paths

            def fake(*args):
                return (0, _json.dumps({
                    "summary": "new", "findings": [], "limitations": []}))

            delegate._verify_paths = raced_paths
            try:
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            finally:
                delegate._verify_paths = orig_paths
            self.assertEqual(_json.loads((rec / "artifact" / "verify-1.json").read_text()), sentinel)
            self.assertEqual(
                _json.loads((rec / "artifact" / "verify-2.json").read_text())["payload"]["summary"],
                "new")

    def test_repeated_verify_increments_and_show_surfaces_latest(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d, committed=False)
            n = {"value": 0}

            def fake(*args):
                n["value"] += 1
                return (0, _json.dumps({
                    "summary": f"run {n['value']}", "findings": [], "limitations": []}))

            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                for _ in range(2):
                    _run_with_home(home, lambda: self._with_codex_verifier(
                        fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertTrue((rec / "artifact" / "verify-1.json").exists())
            self.assertTrue((rec / "artifact" / "verify-2.json").exists())
            summary = io.StringIO()
            latest = io.StringIO()
            with contextlib.redirect_stdout(summary):
                _run_with_home(home, lambda: delegate.show(root, rec.name, None))
            with contextlib.redirect_stdout(latest):
                _run_with_home(home, lambda: delegate.show(root, rec.name, "verify"))
            self.assertIn("verify_artifacts: 2", summary.getvalue())
            self.assertEqual(_json.loads(latest.getvalue())["payload"]["summary"], "run 2")

    def test_wrong_state_fails_before_codex_exec(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d)
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (0, "{}")

            _run_with_home(home, lambda: delegate._set_state(rec, "applied"))
            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(called["n"], 0)

    def test_unimplemented_execution_and_entry_fail_loud(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d)
            for verifier, needle in (
                ("{execution: clean-subagent, backend: \"codex:x\"}",
                 "schema-valid but not executable"),
                ("{execution: external-runner, backend: \"codex:x\", entry: review}",
                 "entry 'review' is not a known verifier entry"),
            ):
                body = ("schema: waystone-profile-1\nbindings:\n"
                        "  implementer: {execution: external-runner, backend: \"codex:x\"}\n"
                        f"  verifier: {verifier}\n")
                _write_profile(root, body)
                with self.assertRaises(delegate.WorkflowError) as cm:
                    _run_with_home(home, lambda: delegate.verify_delegation(root, rec.name))
                self.assertIn(needle, str(cm.exception))

    def test_normalization_and_codex_failure_leave_state_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root, home, rec, _worktree, _unused = self._setup(d)
            contract_path = rec / "artifact" / "contract.yaml"
            contract = yaml.safe_load(contract_path.read_text())
            contract["base_sha"] = "0" * 40
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            called = {"n": 0}

            def fake(*args):
                called["n"] += 1
                return (3, "failed")

            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(called["n"], 0)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])

            contract["base_sha"] = _json.loads((rec / "exposure.json").read_text())["base"]["snapshot_sha"]
            contract_path.write_text(yaml.safe_dump(contract, sort_keys=False))
            with self.assertRaises(delegate.WorkflowError):
                _run_with_home(home, lambda: self._with_codex_verifier(
                    fake, lambda: delegate.verify_delegation(root, rec.name)))
            self.assertEqual(called["n"], 1)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertEqual(list((rec / "artifact").glob("verify-*.json")), [])


class UvCacheTests(unittest.TestCase):
    """0.8.0 M2 §13 — worktree-local uv cache env and result-snapshot exclusion."""

    def setUp(self):
        self.original_fingerprint = delegate._codex_runner_fingerprint
        delegate._codex_runner_fingerprint = _synthetic_codex_fingerprint

    def tearDown(self):
        delegate._codex_runner_fingerprint = self.original_fingerprint

    def test_env_is_passed_to_prep_and_codex_without_global_mutation(self):
        import os
        import types
        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "repo"
            record = Path(d) / "record"
            worktree.mkdir()
            init_repo(worktree)
            record.mkdir()
            prompt = Path(d) / "prompt.txt"
            prompt.write_text("prompt")
            seen = []
            orig = delegate.subprocess.run

            def fake(*args, **kwargs):
                command = args[0]
                if command[0] == "git":
                    return orig(*args, **kwargs)
                if (len(command) >= 2 and command[1] == "--version") or \
                        command[0] == "/usr/sbin/ioreg":
                    return orig(*args, **kwargs)
                seen.append(kwargs.get("env"))
                if (len(command) >= 2 and Path(command[0]).is_absolute()
                        and command[1] == "exec" and "--ephemeral" in command):
                    probe_worktree = Path(command[command.index("-C") + 1])
                    (probe_worktree / f".waystone-sandbox-write-probe-{record.name}").write_text(
                        "waystone-sandbox-write-probe\n")
                return types.SimpleNamespace(returncode=0, stderr="")

            env_names = (
                "UV_CACHE_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_NO_CACHE",
                "WAYSTONE_VERIFIER_SESSION",
            )
            before = tuple(os.environ.get(name) for name in env_names)
            delegate.subprocess.run = fake
            try:
                self.assertEqual(delegate._run_env_prep(worktree, ["true"])[0], 0)
                self.assertEqual(delegate._run_implementer_transport(
                    delegate._run_codex, worktree,
                    "gpt-5.6-sol", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = orig
            expected = str(worktree / ".waystone-uv-cache")
            self.assertEqual(seen[0]["UV_CACHE_DIR"], expected)
            self.assertEqual(seen[2]["UV_CACHE_DIR"], expected)
            self.assertEqual(seen[0]["UV_TOOL_DIR"], str(Path(expected) / "tools"))
            self.assertEqual(seen[2]["UV_TOOL_DIR"], str(Path(expected) / "tools"))
            self.assertEqual(seen[0]["UV_TOOL_BIN_DIR"], str(Path(expected) / "bin"))
            self.assertEqual(seen[2]["UV_TOOL_BIN_DIR"], str(Path(expected) / "bin"))
            self.assertEqual(
                os.path.realpath(Path(seen[1]["UV_CACHE_DIR"]).parent.parent),
                os.path.realpath(worktree.parent))
            self.assertTrue(all("WAYSTONE_VERIFIER_SESSION" not in env for env in seen))
            self.assertEqual(tuple(os.environ.get(name) for name in env_names), before)

    def test_declared_env_prep_warms_runner_cache_for_offline_suite_and_lint(self):
        import base64
        import functools
        import http.server
        import threading
        import zipfile

        def write_wheel(directory, name, version, files, entry_points=None):
            normalized = name.replace("-", "_")
            dist_info = f"{normalized}-{version}.dist-info"
            payloads = {
                **files,
                f"{dist_info}/METADATA": (
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"),
                f"{dist_info}/WHEEL": (
                    "Wheel-Version: 1.0\nGenerator: waystone-test\n"
                    "Root-Is-Purelib: true\nTag: py3-none-any\n"),
            }
            if entry_points is not None:
                payloads[f"{dist_info}/entry_points.txt"] = entry_points
            records = []
            for path, content in payloads.items():
                raw = content.encode()
                digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
                records.append(f"{path},sha256={digest},{len(raw)}")
            records.append(f"{dist_info}/RECORD,,")
            payloads[f"{dist_info}/RECORD"] = "\n".join(records) + "\n"
            wheel = directory / f"{normalized}-{version}-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for path, content in payloads.items():
                    archive.writestr(path, content)
            return wheel

        class QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, _format, *_args):
                pass

        declared = common.load_config(SCRIPTS.parent)["delegation"]["env_prep"]
        self.assertEqual(declared, [
            "uv sync --script scripts/tests/run_tests.py --locked",
            "uv tool run ruff@0.15.22 --version",
        ])
        self.assertIsNotNone(shutil.which("uv"), "real uv required by env-prep contract")

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            scripts = root / "scripts"
            (scripts / "tests").mkdir(parents=True)
            (scripts / "tests" / "run_tests.py").write_text(
                "#!/usr/bin/env python3\n"
                "# /// script\n"
                "# requires-python = \">=3.10\"\n"
                "# dependencies = [\"pyyaml\"]\n"
                "# ///\n"
                "import yaml\n"
                "assert yaml.GATE_MARKER == 'offline-suite-ready'\n")
            (scripts / "lint_target.py").write_text("VALUE = 1\n")
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep:\n"
                + "".join(f"    - {command}\n" for command in declared))

            index = Path(d) / "index"
            packages = index / "packages"
            packages.mkdir(parents=True)
            pyyaml_wheel = write_wheel(
                packages, "pyyaml", "6.0.3",
                {"yaml/__init__.py": "GATE_MARKER = 'offline-suite-ready'\n"})
            ruff_wheel = write_wheel(
                packages, "ruff", "0.15.22",
                {"ruff_stub.py": (
                    "import pathlib\n"
                    "import sys\n\n"
                    "def main():\n"
                    "    if sys.argv[1:] == ['--version']:\n"
                    "        print('ruff 0.15.22')\n"
                    "        return 0\n"
                    "    expected = ['check', 'scripts', '--select', 'F401,F841']\n"
                    "    if sys.argv[1:] != expected:\n"
                    "        print(f'unexpected arguments: {sys.argv[1:]!r}', file=sys.stderr)\n"
                    "        return 2\n"
                    "    if not pathlib.Path('scripts/lint_target.py').is_file():\n"
                    "        print('lint target missing', file=sys.stderr)\n"
                    "        return 2\n"
                    "    print('All checks passed!')\n"
                    "    return 0\n")},
                "[console_scripts]\nruff = ruff_stub:main\n")
            for name, wheel in (("pyyaml", pyyaml_wheel), ("ruff", ruff_wheel)):
                simple = index / "simple" / name
                simple.mkdir(parents=True)
                (simple / "index.html").write_text(
                    f'<a href="../../packages/{wheel.name}">{wheel.name}</a>\n')
            handler = functools.partial(QuietHandler, directory=str(index))
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            started = False
            stopped = False
            checks = {}
            managed_env = {
                "UV_DEFAULT_INDEX": f"http://127.0.0.1:{server.server_port}/simple/",
                "UV_NO_CONFIG": "1",
                "UV_PYTHON": sys.executable,
                "UV_PYTHON_DOWNLOADS": "never",
            }
            cleared_env = (
                "UV_OFFLINE", "UV_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL",
                "UV_FIND_LINKS", "UV_NO_INDEX", "UV_NO_CACHE", "UV_CONSTRAINT",
                "UV_OVERRIDE", "UV_EXCLUDE_NEWER",
            )
            previous = {
                name: os.environ.get(name) for name in (*managed_env, *cleared_env)
            }

            def fake(worktree, _model, _prompt_path, record_dir):
                nonlocal stopped
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                stopped = True
                shutil.rmtree(index)
                env = dict(os.environ)
                env["UV_OFFLINE"] = "1"
                suite = subprocess.run(
                    ["uv", "run", "scripts/tests/run_tests.py"], cwd=worktree,
                    capture_output=True, text=True, timeout=60, env=env)
                lint = subprocess.run(
                    ["uvx", "ruff", "check", "scripts", "--select", "F401,F841"],
                    cwd=worktree, capture_output=True, text=True, timeout=60, env=env)
                checks.update({"suite": suite, "lint": lint, "env": env})
                (record_dir / "last_message.md").write_text("offline gates exercised")
                return 0, 0.1

            try:
                thread.start()
                started = True
                for name in cleared_env:
                    os.environ.pop(name, None)
                os.environ.update(managed_env)
                lock_cache = Path(d) / "lock-cache"
                lock_env = {
                    **os.environ,
                    "UV_CACHE_DIR": str(lock_cache),
                    "UV_TOOL_DIR": str(lock_cache / "tools"),
                    "UV_TOOL_BIN_DIR": str(lock_cache / "bin"),
                }
                locked = subprocess.run(
                    ["uv", "lock", "--script", "scripts/tests/run_tests.py"],
                    cwd=root, capture_output=True, text=True, timeout=60, env=lock_env)
                self.assertEqual(locked.returncode, 0, locked.stderr or locked.stdout)
                self.assertTrue((scripts / "tests" / "run_tests.py.lock").is_file())
                if lock_cache.exists():
                    shutil.rmtree(lock_cache)
                added = git(
                    root, "add", ".waystone.yml", "scripts/tests/run_tests.py",
                    "scripts/tests/run_tests.py.lock", "scripts/lint_target.py")
                self.assertEqual(added.returncode, 0, added.stderr)
                committed = git(root, "commit", "-qm", "offline gate fixture")
                self.assertEqual(committed.returncode, 0, committed.stderr)
                _deleg_run(root, home, fake)
            finally:
                if started and not stopped:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=5)
                elif not started:
                    server.server_close()
                for name, value in previous.items():
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value

            self.assertFalse(thread.is_alive())
            for name in ("suite", "lint"):
                result = checks[name]
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            self.assertEqual(checks["env"]["UV_OFFLINE"], "1")
            self.assertEqual(
                checks["env"]["UV_CACHE_DIR"],
                str(_run_with_home(home, lambda: delegate._worktree_path(
                    root, _latest_rec(root, home).name)) / ".waystone-uv-cache"))

    def test_cache_is_excluded_but_other_untracked_result_is_kept(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            info_exclude = Path(git(root, "rev-parse", "--git-path", "info/exclude").stdout.strip())
            if not info_exclude.is_absolute():
                info_exclude = root / info_exclude
            before = info_exclude.read_bytes() if info_exclude.exists() else None

            def fake(worktree, model, prompt_path, record_dir):
                cache = worktree / ".waystone-uv-cache"
                cache.mkdir()
                (cache / "junk").write_text("cache")
                (worktree / "kept.txt").write_text("keep")
                (record_dir / "last_message.md").write_text("summary")
                return (0, 0.1)

            _deleg_run(root, home, fake)
            rec = _latest_rec(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["changed_files"], [{"path": "kept.txt", "status": "A"}])
            patch = (rec / "artifact" / "changes.patch").read_text()
            self.assertIn("kept.txt", patch)
            self.assertNotIn(".waystone-uv-cache", patch)
            after = info_exclude.read_bytes() if info_exclude.exists() else None
            self.assertEqual(after, before)

    def test_codex_effort_flag_is_exact_and_absent_when_unset(self):
        import types
        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "repo"
            record = Path(d) / "record"
            worktree.mkdir()
            init_repo(worktree)
            record.mkdir()
            prompt = Path(d) / "prompt.txt"
            prompt.write_text("prompt")
            commands = []
            orig = delegate.subprocess.run

            def fake(cmd, **kwargs):
                if cmd[0] == "git":
                    return orig(cmd, **kwargs)
                if (len(cmd) >= 2 and cmd[1] == "--version") or \
                        cmd[0] == "/usr/sbin/ioreg":
                    return orig(cmd, **kwargs)
                commands.append(cmd)
                if "--ephemeral" in cmd:
                    probe_worktree = Path(cmd[cmd.index("-C") + 1])
                    (probe_worktree / f".waystone-sandbox-write-probe-{record.name}").write_text(
                        "waystone-sandbox-write-probe\n")
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            try:
                delegate._run_codex(
                    worktree, "gpt-test", prompt, record, effort="ultra")
                delegate._run_codex(worktree, "gpt-test", prompt, record)
            finally:
                delegate.subprocess.run = orig
            self.assertEqual(len(commands), 4)  # probe + task for each invocation
            for command in commands[:2]:
                self.assertIn("-c", command)
                self.assertIn('model_reasoning_effort="ultra"', command)
            for command in commands[2:]:
                self.assertNotIn("-c", command)
                self.assertFalse(any(
                    arg.startswith("model_reasoning_effort=") for arg in command))

    def test_codex_verifier_ultra_effort_flag_is_exact(self):
        import types
        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "wt"
            record = Path(d) / "record"
            worktree.mkdir()
            record.mkdir()
            commands = []
            orig = delegate.subprocess.run

            def fake(cmd, **kwargs):
                commands.append(cmd)
                return types.SimpleNamespace(returncode=1)

            delegate.subprocess.run = fake
            try:
                delegate._run_codex_verifier(
                    worktree, "gpt-test", "review", record, effort="ultra")
            finally:
                delegate.subprocess.run = orig
            self.assertIn("-c", commands[0])
            self.assertIn('model_reasoning_effort="ultra"', commands[0])


class ContractInjectTests(unittest.TestCase):
    """0.8.0 M2 §10 — bounded, best-effort main operating contract injection."""

    def _module(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context
        return session_context

    def _project(self, d):
        root = Path(d) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            "  - id: feat/active\n    title: active task here\n    status: active\n")
        home = Path(d) / "home"
        home.mkdir()
        return root, home

    def _context(self, module, root, home):
        import contextlib
        import io
        old_argv = sys.argv
        try:
            sys.argv = ["session_context.py", str(root)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = _run_with_home(home, module.main)
            payload = _json.loads(out.getvalue())
            return rc, payload["hookSpecificOutput"]["additionalContext"]
        finally:
            sys.argv = old_argv

    def _delegation(self, root, home, did, state):
        rec = _run_with_home(home, lambda: delegate._record_dir(root, did))
        rec.mkdir(parents=True)
        (rec / "exposure.json").write_text(_json.dumps({"task_id": "feat/active"}))
        (rec / "status.json").write_text(_json.dumps({"state": state}))
        return rec

    def test_block_precedes_start_here_and_summarizes_live_inputs(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, DelegateVerifyTests._PROFILE)
            _run_with_home(home, lambda: overlay.add_delta(
                root, "verification_debt/warn", rule="delegation-verification-evidence-v1",
                summary="s"))
            _run_with_home(home, lambda: overlay.add_delta(
                root, "review_association/observe", rule="round-close-open-findings-v1",
                summary="s"))
            _force_status(root, home, "verification_debt/warn", "warning")
            self._delegation(root, home, "did-one", "needs-review")
            self._delegation(root, home, "did-two", "needs-review")
            self._delegation(root, home, "did-done", "applied")
            evidence = root / ".waystone" / "improve" / "evidence.jsonl"
            evidence.parent.mkdir(parents=True)
            _write_jsonl(evidence, [
                {"task_id": "feat/active", "project": "demo", "findings": [{"severity": "major"}],
                 "delegations": [{"verification_present": False}]},
                {"coverage": {"projects_scanned": ["demo"]}},
            ])
            sh = _run_with_home(home, lambda: common.start_here_path(root))
            sh.parent.mkdir(parents=True, exist_ok=True)
            sh.write_text("FRONTIER")

            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertLess(ctx.index("◆ OPERATING CONTRACT"), ctx.index("▶ START HERE"))
            self.assertIn("implementer:", ctx)
            self.assertIn("verifier:", ctx)
            self.assertNotIn("gpt-5.6-sol", ctx)
            self.assertIn("warning 1 (verification_debt/warn)", ctx)
            self.assertIn("observing 1 (review_association/observe)", ctx)
            self.assertIn("needs-review delegations 2 (did-one did-two)", ctx)
            self.assertIn("unverified+finding tasks 1", ctx)

    def test_routing_policy_renders_all_axes_questions_and_is_bounded(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  reviewer: {execution: external-runner, backend: 'gemini:pro', "
                "use_for: 'adversarial diff review'}\n"
                "  main: {execution: main-session, backend: 'claude:opus'}\n"
            ))
            block = module._routing_block(root)
            self.assertLessEqual(len(block), 12)
            rendered = "\n".join(block)
            for role in delegate.PROFILE_ROLES:
                self.assertIn(f"  {role}:", rendered)
            self.assertIn("bindings: see `waystone paths` → profile", rendered)
            for model in ("claude:opus", "gemini:pro"):
                self.assertNotIn(model, rendered)

            policy = yaml.safe_load(module.ROUTING_POLICY_PATH.read_text())
            self.assertEqual(policy["schema"], "waystone-routing-policy-1")
            self.assertEqual(len(policy["questions"]), 8)
            self.assertEqual(len({question["id"] for question in policy["questions"]}), 8)
            for question in policy["questions"]:
                preference = question["prefer"]
                self.assertTrue(preference["roles"])
                self.assertTrue(preference["executions"])
                self.assertLessEqual(set(preference["roles"]), set(delegate.PROFILE_ROLES))
                self.assertLessEqual(
                    set(preference["executions"]), set(delegate.PROFILE_EXECUTIONS))

    def test_machine_only_evidence_is_not_reported_as_project_evidence(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, DelegateVerifyTests._PROFILE)
            evidence = home / ".waystone" / "improve" / "evidence.jsonl"
            evidence.parent.mkdir(parents=True)
            _write_jsonl(evidence, [{
                "task_id": "feat/active", "project": "demo",
                "findings": [{"severity": "major"}],
                "delegations": [{"verification_present": False}],
            }])

            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertNotIn("unverified+finding tasks", ctx)

    def test_profile_absence_marks_bindings_unavailable_and_constitution_absence_omits_contract(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertIn("routing policy: role guidance", ctx)
            self.assertIn("bindings: unavailable; see `waystone paths` → profile", ctx)
            self.assertEqual(
                {path.name for path in (root / ".waystone").iterdir()},
                {".gitignore", "lock"},
            )
            original = module.CONTRACT_PATH
            module.CONTRACT_PATH = Path(d) / "missing-contract.md"
            try:
                rc, missing = self._context(module, root, home)
            finally:
                module.CONTRACT_PATH = original
            self.assertEqual(rc, 0)
            self.assertNotIn("◆ OPERATING CONTRACT", missing)

    def test_unwritable_project_state_never_breaks_session_start_json(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            state = root / ".waystone"
            state.write_text("not a directory")
            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertIn("[waystone] project: demo", ctx)
            self.assertTrue(state.is_file())

    def test_corrupt_inputs_are_field_local_and_never_break_session_start(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            profile = common.ensure_project_state_dir(root) / "profile.yml"
            profile.write_text("bindings: [\n")
            delta = _run_with_home(home, lambda: overlay._deltas_dir(root)) / "bad.json"
            delta.parent.mkdir(parents=True)
            delta.write_text("{bad")
            rec = self._delegation(root, home, "did-corrupt", "needs-review")
            (rec / "status.json").write_text("{bad")
            evidence = root / ".waystone" / "improve" / "evidence.jsonl"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("{bad\n")
            rc, ctx = self._context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertIn("◆ OPERATING CONTRACT", ctx)
            self.assertIn("unreadable", ctx)
            self.assertNotIn("config/tasks unreadable", ctx)

    def test_contract_has_its_own_1300_character_cap(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            long_contract = Path(d) / "contract.md"
            long_contract.write_text("\n".join("X" * 400 for _ in range(8)))
            original = module.CONTRACT_PATH
            module.CONTRACT_PATH = long_contract
            try:
                block = _run_with_home(home, lambda: module._operating_contract(root))
            finally:
                module.CONTRACT_PATH = original
            self.assertLessEqual(len("\n".join(block)), 1300)


class CodexVerifierTests(unittest.TestCase):
    def test_codex_verifier_uses_exec_without_host_or_plugin_state(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = _deleg_project(d)
            profile = common.ensure_project_state_dir(root) / "profile.yml"
            profile.write_text(
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: \"codex:gpt-test\"}\n"
                "  verifier: {backend: \"codex:gpt-test\"}\n"
            )
            _deleg_run(root, home, _deleg_fake({"f.txt": "changed\n"}))
            rec = _latest_rec(root, home)
            calls = []
            original_native = delegate._run_codex_verifier

            def fake_native(worktree, model, focus, record_dir):
                calls.append((worktree, model, focus, record_dir))
                return 0, _json.dumps({
                    "summary": "reviewed", "findings": [], "limitations": [],
                })

            delegate._run_codex_verifier = fake_native
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.verify_delegation(root, rec.name))
            finally:
                delegate._run_codex_verifier = original_native
            self.assertEqual(rc, 0)
            self.assertEqual(len(calls), 1)
            artifact = _json.loads((rec / "artifact" / "verify-1.json").read_text())
            self.assertEqual(artifact["transport"], "codex-exec:read-only")
            self.assertEqual(artifact["provenance"], "independent-verifier")
