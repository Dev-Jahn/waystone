"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class DelegateSnapshotTests(unittest.TestCase):
    """0.8.0 M1 §3 — snapshot primitive (temp-index read-tree-HEAD seeding). Real temp git repos."""

    def _repo(self, d) -> Path:
        root = Path(d)
        init_repo(root)
        return root

    def test_clean_tree_shortcut(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            head = git(root, "rev-parse", "HEAD").stdout.strip()
            before = git(root, "rev-list", "--count", "HEAD").stdout.strip()
            sha, dirty = delegate._snapshot(root, "snap")
            self.assertFalse(dirty)
            self.assertEqual(sha, head)  # no snapshot commit created
            self.assertEqual(git(root, "rev-list", "--count", "HEAD").stdout.strip(), before)

    def test_dirty_includes_untracked_and_staged_excludes_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "f.txt").write_text("MODIFIED")           # tracked modification
            (root / "new_untracked.txt").write_text("wip")    # untracked, non-ignored
            (root / ".gitignore").write_text("secret.txt\n")
            (root / "secret.txt").write_text("nope")          # ignored
            (root / "staged.txt").write_text("stg")
            git(root, "add", "staged.txt")                    # staged addition
            sha, dirty = delegate._snapshot(root, "snap")
            self.assertTrue(dirty)
            tree = git(root, "ls-tree", "-r", "--name-only", sha).stdout.split()
            self.assertIn("new_untracked.txt", tree)
            self.assertIn("staged.txt", tree)
            self.assertNotIn("secret.txt", tree)
            self.assertEqual(git(root, "show", f"{sha}:f.txt").stdout, "MODIFIED")

    def test_live_tree_and_index_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "f.txt").write_text("MODIFIED")
            (root / "new.txt").write_text("wip")
            git(root, "add", "new.txt")
            status_before = git(root, "status", "--porcelain").stdout
            head_before = git(root, "rev-parse", "HEAD").stdout
            delegate._snapshot(root, "snap")
            self.assertEqual(git(root, "status", "--porcelain").stdout, status_before)
            self.assertEqual(git(root, "rev-parse", "HEAD").stdout, head_before)

    def test_precondition_unborn_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            git(root, "init", "-q", "-b", "main")
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_submodule(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".gitmodules").write_text("[submodule \"x\"]\n")
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_unmerged_index(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            git(root, "checkout", "-q", "-b", "other")
            (root / "f.txt").write_text("other-side")
            git(root, "commit", "-qam", "other")
            git(root, "checkout", "-q", "main")
            (root / "f.txt").write_text("main-side")
            git(root, "commit", "-qam", "main")
            git(root, "merge", "other")  # conflicts -> unmerged entries
            self.assertTrue(git(root, "ls-files", "-u").stdout.strip())
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_rebase_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".git" / "rebase-merge").mkdir()
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_precondition_cherry_pick_head(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / ".git" / "CHERRY_PICK_HEAD").write_text("deadbeef\n")
            with self.assertRaises(delegate.WorkflowError):
                delegate._check_snapshot_preconditions(root)

    def test_preconditions_pass_on_clean_repo(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            delegate._check_snapshot_preconditions(root)  # no raise

    def test_precondition_reserved_report_filename(self):
        # H2: a pre-existing WAYSTONE_REPORT.yaml would be baked into the base, then consumed as the
        # delegate's report and phantom-deleted by the patch — refuse up front.
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "WAYSTONE_REPORT.yaml").write_text("stale: report\n")
            with self.assertRaises(delegate.WorkflowError) as cm:
                delegate._check_snapshot_preconditions(root)
            self.assertIn("reserved", str(cm.exception))

    def test_make_did_shape(self):
        did = delegate._make_did("feat/xyz")
        self.assertRegex(did, r"^\d{8}T\d{6}Z-feat-xyz$")


class DelegateProfileTests(unittest.TestCase):
    """0.8.0 M1 §11 — profile binding resolution (fail-loud, no default-model guessing)."""

    @staticmethod
    def _schema_accepts(schema: dict, instance: object) -> bool:
        """Interpret only the JSON Schema vocabulary used by profile-schema.json."""
        import re

        def resolve(ref: str) -> dict:
            node = schema
            for part in ref.removeprefix("#/").split("/"):
                node = node[part]
            return node

        def valid(node: dict, value: object) -> bool:
            if "$ref" in node and not valid(resolve(node["$ref"]), value):
                return False
            if "allOf" in node and not all(valid(part, value) for part in node["allOf"]):
                return False
            if "oneOf" in node and sum(valid(part, value) for part in node["oneOf"]) != 1:
                return False
            expected_type = node.get("type")
            type_matches = {
                "object": isinstance(value, dict),
                "string": isinstance(value, str),
                "null": value is None,
            }
            if expected_type is not None and not type_matches.get(expected_type, False):
                return False
            if "const" in node and value != node["const"]:
                return False
            if "enum" in node and value not in node["enum"]:
                return False
            if isinstance(value, str):
                if len(value) < node.get("minLength", 0):
                    return False
                if "pattern" in node and re.search(node["pattern"], value) is None:
                    return False
            if isinstance(value, dict):
                required = node.get("required", [])
                if any(field not in value for field in required):
                    return False
                if len(value) < node.get("minProperties", 0):
                    return False
                properties = node.get("properties", {})
                if node.get("additionalProperties") is False and any(
                        field not in properties for field in value):
                    return False
                if any(field in value and not valid(rule, value[field])
                       for field, rule in properties.items()):
                    return False
            return True

        return valid(schema, instance)

    @staticmethod
    def _schema_role_executions(schema: dict, role: str) -> list[str]:
        definition = schema["$defs"][role]
        branch = definition["oneOf"][0] if role == "verifier" else definition
        execution = branch["allOf"][1]["properties"]["execution"]
        return execution["enum"] if "enum" in execution else [execution["const"]]

    def test_missing_profile_raises(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            home.mkdir()
            root = Path(d) / "repo"
            root.mkdir()
            with self.assertRaises(delegate.WorkflowError) as cm:
                _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertIn(str(root / ".waystone" / "profile.yml"), str(cm.exception))
            self.assertIn("verifier: {execution: external-runner, backend:", str(cm.exception))

    def test_resolve_binding_ok_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            root = Path(d) / "repo"
            root.mkdir()
            _write_profile(root)
            profile, fp = _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertTrue(fp.startswith("sha256:"))
            b = delegate._resolve_binding(profile, "implementer", root)
            self.assertEqual(b["backend"], "codex:gpt-5.4-codex")
            self.assertEqual(b["execution"], "external-runner")
            self.assertEqual(b["source"], "profile")

    def test_profile_schema_matches_runtime_combinations_and_profile_corpus(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  main: {execution: main-session, backend: 'claude:opus'}\n"
                "  orchestrator: {execution: deterministic-workflow, backend: 'claude:opus'}\n"
                "  implementer: {execution: external-runner, backend: 'codex:gpt'}\n"
                "  clerk: {execution: forked-subagent, backend: 'local-runner:small'}\n"
                "  verifier: {backend: 'gemini:pro'}\n"
                "  reviewer: {execution: forked-subagent, backend: 'future.runner:model'}\n"
            ))
            profile, _fingerprint = delegate._load_profile(root)
            self.assertEqual(set(profile["bindings"]), set(delegate.PROFILE_ROLES))
            schema = _json.loads(
                (SCRIPTS.parent / "templates" / "profile-schema.json").read_text())
            self.assertEqual(
                set(schema["properties"]["bindings"]["properties"]),
                set(delegate.PROFILE_ROLES),
            )
            standard = schema["$defs"]["binding"]["properties"]["execution"]["oneOf"][0]
            self.assertEqual(standard["enum"], list(delegate.PROFILE_EXECUTIONS))
            self.assertEqual(
                set(delegate.WAYSTONE_EXECUTABLE_EXECUTIONS)
                | set(delegate.HOST_GUIDED_EXECUTIONS),
                set(delegate.PROFILE_EXECUTIONS),
            )
            self.assertFalse(
                set(delegate.WAYSTONE_EXECUTABLE_EXECUTIONS)
                & set(delegate.HOST_GUIDED_EXECUTIONS))
            for role in delegate.PROFILE_ROLES:
                self.assertEqual(
                    self._schema_role_executions(schema, role),
                    list(delegate.VALID_ROLE_EXECUTIONS[role]),
                )

            corpus = [profile]
            for legacy_execution in delegate._LEGACY_VERIFIER_EXECUTIONS:
                corpus.append({
                    "schema": "waystone-profile-1",
                    "bindings": {
                        "implementer": {
                            "execution": "external-runner", "backend": "codex:gpt-test"},
                        "verifier": {
                            "execution": legacy_execution, "backend": "codex:gpt-test"},
                    },
                })
            for instance in corpus:
                delegate._validate_profile(instance, Path("profile.yml"))
                self.assertTrue(self._schema_accepts(schema, instance), instance)

            legacy_branch = schema["$defs"]["verifier"]["oneOf"][1]
            legacy_execution = legacy_branch["allOf"][1]["properties"]["execution"]
            self.assertEqual(
                legacy_execution["enum"], list(delegate._LEGACY_VERIFIER_EXECUTIONS))
            self.assertIs(legacy_execution["deprecated"], True)

            for role in delegate.PROFILE_ROLES:
                for execution in delegate.PROFILE_EXECUTIONS:
                    instance = {"schema": "waystone-profile-1", "bindings": {role: {
                        "execution": execution, "backend": "runner:model"}}}
                    try:
                        delegate._validate_profile(instance, Path("profile.yml"))
                        runtime_accepts = True
                    except delegate.WorkflowError:
                        runtime_accepts = False
                    self.assertEqual(
                        self._schema_accepts(schema, instance), runtime_accepts,
                        f"schema/runtime mismatch for {role}/{execution}",
                    )

    def test_role_execution_combination_violation_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  main: {execution: external-runner, backend: 'codex:gpt'}\n"
            ))
            with self.assertRaisesRegex(delegate.WorkflowError, "not valid for role 'main'"):
                delegate._load_profile(root)

    def test_schema_valid_but_unimplemented_execution_fails_loud(self):
        profile = {"bindings": {"implementer": {
            "execution": "clean-subagent", "backend": "claude:sonnet"}}}
        with self.assertRaisesRegex(
                delegate.WorkflowError,
                "routing contract.*skill routing.*observation attribution"):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_missing_role_binding_raises(self):
        with tempfile.TemporaryDirectory() as d:
            profile = yaml.safe_load(_PROFILE_BODY)
            root = Path(d) / "repo"
            root.mkdir()
            with self.assertRaises(delegate.WorkflowError):
                delegate._resolve_binding(profile, "verifier", root)

    def test_unsupported_execution_raises(self):
        profile = {"bindings": {"implementer": {"execution": "in-process", "backend": "codex:x"}}}
        with self.assertRaises(delegate.WorkflowError):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_bad_backend_format_raises(self):
        profile = {"bindings": {"implementer": {"execution": "external-runner", "backend": "codexonly"}}}
        with self.assertRaises(delegate.WorkflowError):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_external_runner_backend_tokens_are_explicit(self):
        with self.assertRaises(delegate.WorkflowError):
            delegate._runner_model("gemini:pro")
        self.assertEqual(delegate._runner_model("claude:sonnet"), "sonnet")
        self.assertEqual(delegate._runner_model("codex:gpt-5.4-codex"), "gpt-5.4-codex")

    def test_profile_schema_documents_waystone_executable_vs_host_guided(self):
        schema = _json.loads(
            (SCRIPTS.parent / "templates" / "profile-schema.json").read_text())
        description = schema["$defs"]["binding"]["properties"]["execution"]["description"]
        self.assertIn("waystone-executable", description)
        self.assertIn("host-guided", description)

    def test_claude_verifier_binding_selects_claude_transport(self):
        profile = {"bindings": {"verifier": {
            "execution": "external-runner", "backend": "claude:sonnet"}}}
        binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        self.assertEqual(binding["execution"], "claude-cli")
        self.assertEqual(binding["backend"], "claude:sonnet")

    def test_invalid_effort_field_is_rejected(self):
        for effort in ("extreme", "pro"):
            with self.subTest(effort=effort):
                profile = {"bindings": {"implementer": {
                    "execution": "external-runner", "backend": "codex:x", "effort": effort}}}
                with self.assertRaises(delegate.WorkflowError) as cm:
                    delegate._resolve_binding(profile, "implementer", Path("/project"))
                self.assertIn("effort", str(cm.exception))

    def test_effort_vocabulary_matches_profile_schema(self):
        schema = _json.loads(
            (SCRIPTS.parent / "templates" / "profile-schema.json").read_text())
        schema_values = schema["$defs"]["binding"]["properties"]["effort"]["enum"]
        expected = ["none", "minimal", "low", "medium", "high", "xhigh", "ultra"]
        self.assertEqual(schema_values, expected)
        self.assertEqual(list(delegate._EFFORT_VALUES), expected)

    def test_ultra_effort_is_rejected_by_claude_external_runner(self):
        profile = {"bindings": {"implementer": {
            "execution": "external-runner", "backend": "claude:sonnet", "effort": "ultra"}}}
        with self.assertRaisesRegex(delegate.WorkflowError, "claude external-runner effort"):
            delegate._resolve_binding(profile, "implementer", Path("/project"))

    def test_ultra_verifier_effort_uses_codex_exec(self):
        profile = {"bindings": {"verifier": {
            "execution": "external-runner", "backend": "codex:gpt-test", "effort": "ultra"}}}
        binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        self.assertEqual(binding["execution"], "codex-exec")
        self.assertEqual(binding["effort"], "ultra")

    def test_verifier_execution_absent_uses_codex_exec(self):
        profile = {"bindings": {"verifier": {"backend": "codex:x"}}}
        binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        self.assertEqual(binding["execution"], "codex-exec")

    def test_verifier_external_runner_axis_preserves_codex_transport(self):
        profile = {"bindings": {"verifier": {
            "execution": "external-runner", "backend": "codex:x"}}}
        binding = delegate._resolve_verifier_binding(profile, Path("/project"))
        self.assertEqual(binding["execution"], "codex-exec")
        self.assertEqual(binding["backend"], "codex:x")

    def test_legacy_codex_execution_conflicts_with_claude_backend(self):
        profile = {"bindings": {"verifier": {
            "execution": "codex-cli", "backend": "claude:sonnet"}}}
        with self.assertRaisesRegex(delegate.WorkflowError, "legacy Codex transport"):
            delegate._resolve_verifier_binding(profile, Path("/project"))

    def test_codex_verifier_binding_is_host_independent_and_legacy_values_normalize(self):
        import contextlib
        import io
        import os

        canonical = {"bindings": {"verifier": {"backend": "codex:x"}}}
        old_host = os.environ.get("WAYSTONE_HOST")
        try:
            for host in (None, "claude", "codex"):
                if host is None:
                    os.environ.pop("WAYSTONE_HOST", None)
                else:
                    os.environ["WAYSTONE_HOST"] = host
                with self.subTest(host=host):
                    binding = delegate._resolve_verifier_binding(canonical, Path("/project"))
                    self.assertEqual(binding["execution"], "codex-exec")

            for execution in ("codex-cli", "codex-companion"):
                legacy = {"bindings": {"verifier": {
                    "execution": execution, "backend": "codex:x",
                    "entry": "adversarial-review",
                }}}
                err = io.StringIO()
                with self.subTest(execution=execution), contextlib.redirect_stderr(err):
                    binding = delegate._resolve_verifier_binding(legacy, Path("/project"))
                self.assertEqual(binding["execution"], "codex-exec")
                self.assertIn("deprecated", err.getvalue())
                self.assertIn("execution", err.getvalue())
                self.assertIn("entry", err.getvalue())
        finally:
            if old_host is None:
                os.environ.pop("WAYSTONE_HOST", None)
            else:
                os.environ["WAYSTONE_HOST"] = old_host


class DelegatePacketTests(unittest.TestCase):
    """0.8.0 M1 §7 — task packet assembly + acceptance merge (fail-loud on empty)."""

    def test_packet_merges_accept_and_flags_dedup_order(self):
        data = _packet_registry()
        packet, acceptance = delegate._build_packet(data, "feat/xyz",
                                                       ["flag criterion", "registry criterion one"], Path("/x"))
        self.assertEqual(packet["schema"], "waystone-packet-1")
        self.assertEqual(acceptance, ["registry criterion one", "flag criterion"])  # order + dedup
        self.assertEqual(packet["task"]["deps"], [{"id": "feat/dep", "status": "done"}])
        self.assertEqual(packet["project"]["name"], "demo")

    def test_rendered_worker_prompt_pins_i10_contract_and_known_debt(self):
        data = _packet_registry()
        task = next(item for item in data["tasks"] if item["id"] == "feat/xyz")
        scope = "src/i10_scope.py"
        task.update({
            "milestone": "M1-A",
            "round": "2026-07-20-i10",
            "scope": [scope],
        })
        packet, _acceptance = delegate._build_packet(
            data, "feat/xyz", [], Path("/x"),
            routing_note="budget favors delegated execution")
        base_sha = "a" * 40
        prompt = delegate._render_prompt(packet, base_sha)

        # I-10 bounds: the packet retains declared scope while the current prompt states the
        # worker-facing scope/worktree boundary generically.
        self.assertEqual(packet["declared_scope"], [scope])
        self.assertIn("- title: implement the xyz feature", prompt)
        self.assertIn(
            "Stay strictly within scope. Modify only files inside this worktree.", prompt)
        self.assertIn("1. registry criterion one", prompt)
        self.assertIn("Do NOT accept your own work", prompt)
        self.assertIn("a separate verifier decides that. Never declare success.", prompt)
        for report_contract_line in (
                "## Report (required)", "`WAYSTONE_REPORT.yaml`", "verification:",
                "limitations:", "risks:", "escalations:"):
            with self.subTest(report_contract_line=report_contract_line):
                self.assertIn(report_contract_line, prompt)

        task_block = prompt.split("## Task\n\n", 1)[1].split(
            "\n\n## Acceptance criteria", 1)[0]
        expected_task_lines = [
            "- id: feat/xyz",
            "- title: implement the xyz feature",
            "- status: active",  # ADR-0014 Addendum §1: pinned I-10 debt.
            "- milestone: M1-A",  # ADR-0014 Addendum §1: pinned I-10 debt.
            "- round: 2026-07-20-i10",  # ADR-0014 Addendum §1: pinned I-10 debt.
            "- anchor: SSOT §2",  # ADR-0014 Addendum §1: pinned I-10 debt.
            "- notes: do the thing",
            # ADR-0014 Addendum §1: dependency presence also pins its registry status.
            "- deps: feat/dep (done)",
            # ADR-0014 Addendum §1: routing_note is the fifth pinned I-10 debt surface.
            "- routing_note: budget favors delegated execution",
        ]
        self.assertEqual(task_block.splitlines(), expected_task_lines)

        normalized_prompt = prompt.casefold()
        for internal_surface in (
                "tasks.yaml",  # I-10/Addendum §2: registry paths are outside pinned debt.
                "roadmap",  # I-10/Addendum §2: project-roadmap bookkeeping stays internal.
                "progress",  # I-10/Addendum §2: progress bookkeeping stays internal.
                ".waystone/",  # I-10/Addendum §2: machine/runtime state paths stay internal.
                "round close",  # I-10/Addendum §2: round-close protocol is not worker intent.
                "exposure",  # I-10/Addendum §2: exposure protocol stays internal.
                "overlay",  # I-10/Addendum §2: overlay protocol stays internal.
        ):
            with self.subTest(internal_surface=internal_surface):
                self.assertNotIn(internal_surface, normalized_prompt)

        # Pin both the static template and its three declared substitutions so a new
        # registry/internal projection cannot appear elsewhere and evade TASK_BLOCK.
        template = delegate._TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertEqual(
            hashlib.sha256(template.encode("utf-8")).hexdigest(),
            "f5f43018a3b64db121529bf3f1a91439bdd888aa583575efbbebb424e50bbcd4",
        )
        expected_prompt = (template.replace("{{TASK_BLOCK}}", "\n".join(expected_task_lines))
                           .replace("{{ACCEPTANCE}}", "1. registry criterion one")
                           .replace("{{BASE_SHA}}", base_sha))
        self.assertEqual(prompt, expected_prompt)

    def test_empty_acceptance_raises(self):
        data = {"project": "d", "tasks": [{"id": "feat/na", "title": "no acceptance here", "status": "active"}]}
        with self.assertRaises(delegate.WorkflowError) as cm:
            delegate._build_packet(data, "feat/na", [], Path("/x"))
        self.assertIn("no acceptance criteria", str(cm.exception))

    def test_blocked_task_message(self):
        with self.assertRaises(delegate.WorkflowError) as cm:
            delegate._build_packet(_packet_registry(), "feat/blk", ["c"], Path("/x"))
        msg = str(cm.exception)
        self.assertIn("blocked", msg)
        # R10: must NOT assert deps are unmet (stale-blocked exists) — offer the conditional path
        self.assertIn("if its deps are now satisfied", msg)
        self.assertNotIn("unmet", msg.lower())

    def test_done_task_rejected(self):
        with self.assertRaises(delegate.WorkflowError):
            delegate._build_packet(_packet_registry(), "feat/dn", ["c"], Path("/x"))

    def test_unknown_task_rejected(self):
        with self.assertRaises(delegate.WorkflowError):
            delegate._build_packet(_packet_registry(), "feat/nope", ["c"], Path("/x"))

    def test_all_unsatisfied_dependencies_are_rejected_with_effective_statuses(self):
        data = {"project": "demo", "tasks": [
            {"id": "feat/child", "title": "a dependent child task", "status": "active",
             "deps": ["feat/parked", "feat/blocked", "feat/pending", "feat/default",
                      "feat/missing"], "accept": ["all dependencies are done"]},
            {"id": "feat/parked", "title": "a parked dependency task", "status": "parked"},
            {"id": "feat/blocked", "title": "a blocked dependency task", "status": "blocked"},
            {"id": "feat/pending", "title": "a pending dependency task", "status": "pending"},
            {"id": "feat/default", "title": "a default pending dependency task"},
        ]}
        with self.assertRaises(delegate.WorkflowError) as cm:
            delegate._build_packet(data, "feat/child", [], Path("/x"))
        message = str(cm.exception)
        for diagnostic in (
                "feat/parked (parked)", "feat/blocked (blocked)", "feat/pending (pending)",
                "feat/default (pending)", "feat/missing (unknown)"):
            with self.subTest(diagnostic=diagnostic):
                self.assertIn(diagnostic, message)


class DelegateRunTests(unittest.TestCase):
    """Full run flow with injected Codex/Claude runners; never invokes a real runner."""

    def setUp(self):
        self.original_fingerprint = delegate._codex_runner_fingerprint
        delegate._codex_runner_fingerprint = _synthetic_codex_fingerprint

    def tearDown(self):
        delegate._codex_runner_fingerprint = self.original_fingerprint

    def _project(self, d) -> tuple[Path, Path]:
        root = Path(d) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text(
            "version: 1\nproject: demo\ntasks:\n"
            '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
            '    accept:\n      - "criterion alpha here"\n')
        git(root, "add", "-A")
        git(root, "commit", "-qm", "setup")
        home = Path(d) / "home"
        _write_profile(root)
        return root, home

    def _fake_runner(self, changes, report=None, rc=0):
        def fake(worktree, model, prompt_path, record_dir):
            for name, content in changes.items():
                (worktree / name).write_text(content)
            (record_dir / "last_message.md").write_text("delegate summary", encoding="utf-8")
            (record_dir / "runner.jsonl").write_text("{}\n", encoding="utf-8")
            if report is not None:
                (worktree / "WAYSTONE_REPORT.yaml").write_text(report, encoding="utf-8")
            return (rc, 0.42)
        return fake

    def _run(self, root, home, fake, task="feat/xyz", accept=None):
        orig = delegate._run_codex
        delegate._run_codex = fake
        try:
            return _run_with_home(home, lambda: delegate.run_delegation(root, task, "implementer", accept or []))
        finally:
            delegate._run_codex = orig

    def _run_claude_backend(self, root, home, fake, task="feat/xyz", accept=None):
        _write_profile(root, (
            "schema: waystone-profile-1\nbindings:\n"
            "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
        orig = delegate._run_claude
        delegate._run_claude = fake
        try:
            return _run_with_home(
                home, lambda: delegate.run_delegation(
                    root, task, "implementer", accept or [],
                    allow_unsandboxed_runner=True,
                    unsandboxed_reason="synthetic test transport"))
        finally:
            delegate._run_claude = orig

    def _record_dir(self, root, home):
        return _run_with_home(home, lambda: sorted(delegate._delegations_dir(root).iterdir())[-1])

    def test_run_refuses_symlinked_worktrees_cache_ancestor_without_external_write(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            external = Path(d) / "external-worktrees"
            external.mkdir()
            worktrees = home / ".waystone" / "cache" / "worktrees"
            worktrees.parent.mkdir(parents=True)
            worktrees.symlink_to(external, target_is_directory=True)

            with self.assertRaises(delegate._RefusedWrite) as raised:
                self._run(root, home, self._fake_runner({}))

            self.assertIn(str(worktrees), str(raised.exception))
            self.assertTrue(worktrees.is_symlink())
            self.assertFalse((external / common._project_slug(root)).exists())

    def test_run_refuses_worktrees_cache_outside_machine_root_without_write(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            outside = Path(d) / "outside-worktrees"
            original = delegate.worktrees_cache_dir
            delegate.worktrees_cache_dir = lambda: outside
            try:
                with self.assertRaises(delegate._RefusedWrite) as raised:
                    self._run(root, home, self._fake_runner({}))
            finally:
                delegate.worktrees_cache_dir = original

            self.assertIn(str(outside), str(raised.exception))
            self.assertIn(str(home / ".waystone"), str(raised.exception))
            self.assertFalse((outside / common._project_slug(root)).exists())

    def test_success_path_contract_and_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            report = ("verification:\n  - {cmd: \"pytest\", rc: 0, summary: \"passed\"}\n"
                      "limitations: [\"none\"]\nrisks: []\nescalations: []\n")
            fake = self._fake_runner({"impl.py": "print('hi')\n"}, report=report)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self._run(root, home, fake)
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            # prompt carries acceptance criterion text
            self.assertIn("criterion alpha here", (rec / "prompt.txt").read_text())
            # WAYSTONE_REPORT consumed from the worktree (not left to pollute the patch)
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            self.assertFalse((wt / "WAYSTONE_REPORT.yaml").exists())
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["schema"], "waystone-artifact-1")
            self.assertFalse(contract["empty"])
            self.assertEqual([c["path"] for c in contract["changed_files"]], ["impl.py"])
            self.assertEqual(contract["changed_files"][0]["status"], "A")
            self.assertEqual(contract["delegate_report"]["present"], True)
            self.assertEqual(contract["delegate_report"]["verification"][0]["rc"], 0)
            self.assertEqual(contract["runner"]["backend"], "codex:gpt-5.4-codex")
            self.assertTrue((rec / "artifact" / "changes.patch").exists())
            # exposure immutable fields
            import json as _json
            exp = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(exp["schema"], "waystone-exposure-1")
            self.assertEqual(exp["sandbox"], "workspace-write")
            self.assertEqual(exp["binding"]["backend"], "codex:gpt-5.4-codex")
            self.assertEqual(
                {row["identity"]["layer"] for row in exp["overlays"]}, {"base"})
            # result ref exists
            self.assertTrue(git(root, "rev-parse", "--verify",
                                f"refs/waystone/delegations/{rec.name}-result").returncode == 0)

    def test_claude_runner_injected_success_and_delta_warning(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            report = "verification: []\nlimitations: []\nrisks: []\nescalations: []\n"
            fake = self._fake_runner({"impl.py": "claude\n"}, report=report)
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                rc = self._run_claude_backend(root, home, fake)
            self.assertEqual(rc, 0)
            self.assertIn("waystone warn:", err.getvalue())
            for delta in ("filesystem", "process", "network"):
                self.assertIn(delta, err.getvalue().lower())
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["runner"]["backend"], "claude:sonnet")
            exp = _json.loads((rec / "exposure.json").read_text())
            self.assertEqual(exp["sandbox"], "none")

    def test_claude_implementer_refuses_without_explicit_unsandboxed_override(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            called = []
            original = delegate._run_claude
            delegate._run_claude = lambda *args, **kwargs: (called.append(True) or (0, 0.1))
            try:
                with self.assertRaisesRegex(
                        delegate.WorkflowError, "--allow-unsandboxed-runner.*--reason"):
                    _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", []))
            finally:
                delegate._run_claude = original
            self.assertEqual(called, [])
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_claude_unsandboxed_override_records_packet_exposure_and_full_delta(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            fake = self._fake_runner({"impl.py": "claude\n"})
            original = delegate._run_claude
            delegate._run_claude = fake
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.run_delegation(
                        root, "feat/xyz", "implementer", [],
                        allow_unsandboxed_runner=True,
                        unsandboxed_reason="legacy Claude-only backend"))
            finally:
                delegate._run_claude = original
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            packet = yaml.safe_load((rec / "packet.yaml").read_text())
            exposure = _json.loads((rec / "exposure.json").read_text())
            expected = {"kind": "allow-unsandboxed-runner",
                        "reason": "legacy Claude-only backend", "provenance": "user"}
            self.assertEqual(packet["runner_override"], expected)
            self.assertEqual(exposure["runner_override"], expected)
            self.assertEqual(exposure["sandbox"], "none")
            for delta in ("filesystem", "process", "network"):
                self.assertIn(delta, err.getvalue().lower())

    def test_claude_unsandboxed_cli_override_requires_paired_reason(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            _write_profile(root, (
                "schema: waystone-profile-1\nbindings:\n"
                "  implementer: {execution: external-runner, backend: 'claude:sonnet'}\n"))
            cases = (
                (["--allow-unsandboxed-runner"], "requires --reason"),
                (["--reason", "because"], "only valid with --allow-unsandboxed-runner"),
            )
            for extra, needle in cases:
                err = io.StringIO()
                with self.subTest(extra=extra), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.main([
                        "run", "feat/xyz", "--root", str(root), *extra]))
                self.assertEqual(rc, 1)
                self.assertIn(needle, err.getvalue())
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_claude_runner_injected_failure_preserves_failed_runner(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "partial\n"}, rc=9)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run_claude_backend(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-runner")

    def test_claude_runner_missing_report_uses_shared_contract_path(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "no report\n"}, report=None)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self._run_claude_backend(root, home, fake), 0)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertIs(contract["delegate_report"]["present"], False)

    def test_run_claude_transport_is_injectable_and_confined(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = root / "prompt.txt"
            prompt.write_text("implement")
            calls = []

            def transport(cmd, **kwargs):
                calls.append((cmd, kwargs))
                kwargs["stdout"].write('{"type":"result","result":"done"}\n')
                return subprocess.CompletedProcess(cmd, 0)

            rc, _duration = delegate._run_implementer_transport(
                delegate._run_claude, root, "sonnet", prompt, root, runner=transport)
            self.assertEqual(rc, 0)
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[:4], ["claude", "-p", "--model", "sonnet"])
            for flag in ("--safe-mode", "--strict-mcp-config", "--no-chrome",
                         "--allowedTools", "--disallowedTools", "--no-session-persistence"):
                self.assertIn(flag, cmd)
            self.assertIn("WebFetch", cmd[cmd.index("--disallowedTools") + 1])
            self.assertEqual(Path(kwargs["cwd"]), root)
            self.assertNotIn("WAYSTONE_VERIFIER_SESSION", kwargs["env"])
            self.assertEqual(
                kwargs["env"]["UV_CACHE_DIR"], str(root / ".waystone-uv-cache"))
            self.assertEqual(
                kwargs["env"]["UV_TOOL_DIR"], str(root / ".waystone-uv-cache" / "tools"))
            self.assertEqual(
                kwargs["env"]["UV_TOOL_BIN_DIR"], str(root / ".waystone-uv-cache" / "bin"))
            self.assertEqual((root / "last_message.md").read_text(), "done")

    def test_run_claude_rejects_non_object_stream_event_as_transport_failure(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            prompt = root / "prompt.txt"
            prompt.write_text("implement")

            def transport(cmd, **kwargs):
                kwargs["stdout"].write("[]\n")
                return subprocess.CompletedProcess(cmd, 0)

            rc, _duration = delegate._run_claude(
                root, "sonnet", prompt, root, runner=transport)
            self.assertNotEqual(rc, 0)
            self.assertIn("object", (root / "runner.stderr").read_text().lower())

    def test_unexpected_runner_exception_transitions_failed_runner(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def boom(*_args, **_kwargs):
                raise RuntimeError("transport exploded")

            with self.assertRaisesRegex(delegate.WorkflowError, "transport exploded"):
                self._run(root, home, boom)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-runner")

    def test_cli_prepares_slow_inputs_before_claim_lock_and_revalidates_inside(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            depth = {"value": 0}
            observed = {"preflight": [], "packet": [], "overlay": [], "composition": []}
            originals = (delegate.hold_lock, delegate._check_snapshot_preconditions,
                         delegate._build_packet, delegate._active_overlays,
                         delegate._run_codex, delegate.hold_project_lock)

            @contextlib.contextmanager
            def tracked_lock(path, timeout=None):
                depth["value"] += 1
                try:
                    yield
                finally:
                    depth["value"] -= 1

            def tracked_project_lock(project, timeout=None):
                return tracked_lock(common.project_lock_path(project), timeout=timeout)

            def preflight(project):
                observed["preflight"].append(depth["value"])
                return originals[1](project)

            def build_packet(*args, **kwargs):
                observed["packet"].append(depth["value"])
                return originals[2](*args, **kwargs)

            def active_overlays(project, composition):
                observed["overlay"].append(depth["value"])
                observed["composition"].append(composition)
                return originals[3](project, composition)

            delegate.hold_lock = tracked_lock
            delegate.hold_project_lock = tracked_project_lock
            delegate._check_snapshot_preconditions = preflight
            delegate._build_packet = build_packet
            delegate._active_overlays = active_overlays
            delegate._run_codex = self._fake_runner({"impl.py": "x\n"})
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root)]))
            finally:
                (delegate.hold_lock, delegate._check_snapshot_preconditions,
                 delegate._build_packet, delegate._active_overlays,
                 delegate._run_codex, delegate.hold_project_lock) = originals
            self.assertEqual(rc, 0)
            self.assertEqual(observed["preflight"], [0])
            self.assertEqual(observed["packet"], [0, 1])
            self.assertEqual(observed["overlay"], [1])
            self.assertEqual(len(observed["composition"]), 1)
            self.assertIn("effective", observed["composition"][0])

    def test_cli_rejects_task_state_changed_after_packet_preparation(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            original_prepare = delegate._prepare_run
            original_runner = delegate._run_codex
            runner_calls = []

            def prepare(*args, **kwargs):
                plan = original_prepare(*args, **kwargs)
                tasks_path = root / "tasks.yaml"
                tasks_path.write_text(tasks_path.read_text().replace(
                    "status: active", "status: done"))
                return plan

            def runner(*args, **kwargs):
                runner_calls.append(True)
                return (0, 0.1)

            delegate._prepare_run = prepare
            delegate._run_codex = runner
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root)]))
            finally:
                delegate._prepare_run = original_prepare
                delegate._run_codex = original_runner
            self.assertEqual(rc, 1)
            self.assertEqual(runner_calls, [])
            self.assertIn("changed while preparing", err.getvalue())
            self.assertFalse(delegate._delegations_dir(root).exists())

    def test_exposure_replace_failure_leaves_no_partial_or_temp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "repo"
            root.mkdir()
            record = root / ".waystone" / "delegations" / "did"
            record.mkdir(parents=True)
            original_replace = common.os.replace

            def fail_replace(_source, _target):
                raise OSError("injected exposure replace failure")

            common.os.replace = fail_replace
            try:
                with self.assertRaises(OSError):
                    delegate._write_exposure(
                        record, "did", root, {"project": {"name": "demo"}}, "feat/xyz",
                        "head", "base", False, {}, "sha256:test", [])
            finally:
                common.os.replace = original_replace
            self.assertFalse((record / "exposure.json").exists())
            self.assertEqual(list(record.glob(".exposure.json.*.tmp")), [])

    def test_missing_report_marked_absent(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"}, report=None)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self._run(root, home, fake)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["delegate_report"]["present"], False)

    def test_empty_diff_marks_empty(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("no changes", encoding="utf-8")
                (record_dir / "runner.stderr").write_text(
                    "WARNING: could not create PATH aliases: Operation not permitted\n",
                    encoding="utf-8")
                return (0, 0.42)

            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self._run(root, home, fake)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertTrue(contract["empty"])
            self.assertFalse((rec / "artifact" / "changes.patch").exists())
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")

    def test_rc_zero_empty_missing_report_with_sandbox_write_failure_is_failed_env(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            stderr = "loopback: Failed RTM_NEWADDR: Operation not permitted\n"

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("could not write", encoding="utf-8")
                (record_dir / "runner.stderr").write_text(stderr, encoding="utf-8")
                return (0, 0.42)

            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(
                        delegate.WorkflowError, "runner environment failure despite rc 0"):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "failed-env")
            self.assertIn(stderr.strip(), status["error"])
            self.assertFalse((rec / "artifact" / "contract.yaml").exists())
            failure = io.StringIO()
            with contextlib.redirect_stdout(failure):
                self.assertEqual(delegate.show(root, rec.name, "failure"), 0)
            self.assertIn(".waystone/codex-runner-verified", failure.getvalue())

    def test_empty_diff_with_report_is_not_misclassified_by_sandbox_stderr(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            report = "verification: []\nlimitations: []\nrisks: []\nescalations: []\n"

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("no change needed", encoding="utf-8")
                (record_dir / "runner.stderr").write_text(
                    "loopback: Failed RTM_NEWADDR: Operation not permitted\n", encoding="utf-8")
                (worktree / "WAYSTONE_REPORT.yaml").write_text(report, encoding="utf-8")
                return (0, 0.42)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self._run(root, home, fake), 0)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertTrue(contract["empty"])
            self.assertIs(contract["delegate_report"]["present"], True)

    def test_codex_sandbox_probe_uses_workspace_write_and_preserves_raw_stderr(self):
        import types

        with tempfile.TemporaryDirectory() as d:
            worktree = Path(d) / "repo"
            record = Path(d) / "record"
            worktree.mkdir()
            init_repo(worktree)
            record.mkdir()
            calls = []
            stderr = "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted\n"
            original = delegate.subprocess.run

            def fake(cmd, **kwargs):
                if len(cmd) < 2 or cmd[1] != "exec" or not Path(cmd[0]).is_absolute():
                    return original(cmd, **kwargs)
                calls.append((cmd, kwargs))
                kwargs["stderr"].write(stderr)
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            try:
                with self.assertRaises(delegate._RunnerSandboxUnusable) as cm:
                    delegate._run_codex_sandbox_probe(
                        worktree, "gpt-test", record, effort="xhigh")
            finally:
                delegate.subprocess.run = original
            self.assertEqual(len(calls), 1)
            command = calls[0][0]
            self.assertTrue(Path(command[0]).is_absolute())
            self.assertEqual(command[1], "exec")
            probe_worktree = Path(command[command.index("-C") + 1])
            self.assertNotEqual(probe_worktree, worktree)
            self.assertEqual(
                os.path.realpath(probe_worktree.parent), os.path.realpath(worktree.parent))
            self.assertEqual(command[command.index("-s") + 1], "workspace-write")
            self.assertIn(stderr.strip(), str(cm.exception))
            self.assertEqual((record / "sandbox-probe.stderr").read_text(), stderr)
            self.assertFalse(os.path.lexists(probe_worktree))

    def test_codex_sandbox_probe_failure_records_failed_env_without_main_runner(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            stderr = "landlock sandbox: failed to write: Permission denied\n"
            original = delegate._run_codex_sandbox_probe

            def fail_probe(worktree, model, record_dir, *, effort=None, fingerprint=None):
                (record_dir / "sandbox-probe.stderr").write_text(stderr, encoding="utf-8")
                raise delegate._RunnerSandboxUnusable(
                    f"runner sandbox unusable: {stderr}")

            delegate._run_codex_sandbox_probe = fail_probe
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                            delegate.WorkflowError, "runner sandbox unusable") as cm:
                        _run_with_home(home, lambda: delegate.run_delegation(
                            root, "feat/xyz", "implementer", []))
            finally:
                delegate._run_codex_sandbox_probe = original
            self.assertIn(stderr, str(cm.exception))
            rec = self._record_dir(root, home)
            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "failed-env")
            self.assertIn(stderr, status["error"])
            self.assertFalse((rec / "runner.jsonl").exists())

    def test_codex_probe_transport_failure_records_failed_runner_probe_result(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            probe = {
                "schema": "waystone-sandbox-probe-1",
                "outcome": "failed",
                "classification": "transport",
                "transport_kind": "authentication",
                "duration_s": 1.25,
            }
            original = delegate._run_codex_sandbox_probe

            def fail_probe(worktree, model, record_dir, *, effort=None, fingerprint=None):
                raise delegate._RunnerProbeTransportFailure(
                    "runner preflight transport failed: invalid API key", probe)

            delegate._run_codex_sandbox_probe = fail_probe
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                            delegate.WorkflowError, "preflight transport failed"):
                        _run_with_home(home, lambda: delegate.run_delegation(
                            root, "feat/xyz", "implementer", []))
            finally:
                delegate._run_codex_sandbox_probe = original
            rec = self._record_dir(root, home)
            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "failed-runner")
            self.assertEqual(status["probe"], probe)
            self.assertFalse((rec / "runner.jsonl").exists())

    def test_contract_records_probe_breakdown_inside_total_runner_duration(self):
        import contextlib
        import io
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            probe = {
                "schema": "waystone-sandbox-probe-1",
                "outcome": "passed",
                "classification": None,
                "duration_s": 0.25,
            }

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("done", encoding="utf-8")
                (record_dir / "sandbox-probe-result.json").write_text(
                    _json.dumps(probe), encoding="utf-8")
                return 0, 0.75

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(self._run(root, home, fake), 0)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["runner"]["duration_s"], 0.75)
            self.assertEqual(contract["runner"]["probe"], probe)

    def test_codex_sandbox_probe_isolates_all_probe_edits_in_disposable_sibling(self):
        import json as _json
        import types

        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            record = Path(d) / "record"
            record.mkdir()
            original = delegate.subprocess.run
            observed = {}

            def fake(cmd, **kwargs):
                if len(cmd) < 2 or cmd[1] != "exec" or not Path(cmd[0]).is_absolute():
                    return original(cmd, **kwargs)
                probe_worktree = Path(cmd[cmd.index("-C") + 1])
                observed["worktree"] = probe_worktree
                probe_name = f".waystone-sandbox-write-probe-{record.name}"
                (probe_worktree / probe_name).write_text(
                    "waystone-sandbox-write-probe\n", encoding="utf-8")
                (probe_worktree / "UNRELATED_PROBE_EDIT.txt").write_text(
                    "must be discarded\n", encoding="utf-8")
                (probe_worktree / "f.txt").write_text("probe changed tracked content\n")
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            try:
                result = delegate._run_codex_sandbox_probe(root, "gpt-test", record)
            finally:
                delegate.subprocess.run = original

            self.assertNotEqual(observed["worktree"], root)
            self.assertEqual(
                os.path.realpath(observed["worktree"].parent), os.path.realpath(root.parent))
            self.assertFalse(os.path.lexists(observed["worktree"]))
            self.assertEqual((root / "f.txt").read_text(), "0")
            self.assertFalse((root / "UNRELATED_PROBE_EDIT.txt").exists())
            self.assertEqual(result["outcome"], "passed")
            self.assertGreaterEqual(result["duration_s"], 0)
            recorded = _json.loads((record / "sandbox-probe-result.json").read_text())
            self.assertEqual(recorded, result)
            self.assertEqual(recorded["cleanup_state"], "cleaned")
            listed = git(root, "worktree", "list", "--porcelain").stdout
            self.assertNotIn(str(observed["worktree"]), listed)

    def test_codex_sandbox_probe_distinguishes_sandbox_and_transport_failures(self):
        import json as _json
        import types

        cases = (
            ("Landlock setup failed: permission denied\n", "sandbox", None,
             delegate._RunnerSandboxUnusable),
            ("bwrap: network namespace setup failed: Operation not permitted\n", "sandbox", None,
             delegate._RunnerSandboxUnusable),
            ("401 Unauthorized: invalid API key\n", "transport", "authentication",
             delegate._RunnerProbeTransportFailure),
            ("network request failed: connection refused\n", "transport", "network",
             delegate._RunnerProbeTransportFailure),
        )
        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            original = delegate.subprocess.run
            for index, (stderr, classification, transport_kind, exception_type) in enumerate(cases):
                with self.subTest(stderr=stderr):
                    record = Path(d) / f"record-{index}"
                    record.mkdir()

                    def fake(cmd, **kwargs):
                        if len(cmd) < 2 or cmd[1] != "exec" or not Path(cmd[0]).is_absolute():
                            return original(cmd, **kwargs)
                        kwargs["stderr"].write(stderr)
                        return types.SimpleNamespace(returncode=1)

                    delegate.subprocess.run = fake
                    try:
                        with self.assertRaises(exception_type) as cm:
                            delegate._run_codex_sandbox_probe(root, "gpt-test", record)
                    finally:
                        delegate.subprocess.run = original
                    self.assertEqual(cm.exception.probe_result["classification"], classification)
                    recorded = _json.loads((record / "sandbox-probe-result.json").read_text())
                    self.assertEqual(recorded["classification"], classification)
                    self.assertEqual(recorded["transport_kind"], transport_kind)
                    self.assertGreaterEqual(recorded["duration_s"], 0)

    def test_sandbox_failure_matching_is_order_independent_and_ignores_success_narrative(self):
        with tempfile.TemporaryDirectory() as d:
            record = Path(d)
            runner_stderr = record / "runner.stderr"
            report = {"present": False}
            sandbox_cases = (
                "Landlock setup failed: permission denied",
                "failed to apply Landlock sandbox rules",
                "bwrap: network namespace setup failed: Operation not permitted",
                "sandbox-exec: sandbox_apply failed: Operation not permitted",
            )
            for stderr in sandbox_cases:
                with self.subTest(stderr=stderr):
                    runner_stderr.write_text(stderr + "\n", encoding="utf-8")
                    self.assertIsNotNone(
                        delegate._runner_environment_failure_reason(record, True, report))
                    self.assertIsNotNone(delegate._runner_sandbox_diagnostic_hint(stderr))

            non_failures = (
                "cache cleanup skipped: permission denied",
                "correctly denied invalid access; all Landlock tests passed",
            )
            for stderr in non_failures:
                with self.subTest(stderr=stderr):
                    runner_stderr.write_text(stderr + "\n", encoding="utf-8")
                    self.assertIsNone(
                        delegate._runner_environment_failure_reason(record, True, report))
                    self.assertIsNone(delegate._runner_sandbox_diagnostic_hint(stderr))

    def test_codex_sandbox_probe_records_stderr_read_failure_and_duration(self):
        import json as _json
        import types

        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            record = Path(d) / "record"
            record.mkdir()
            original_run = delegate.subprocess.run
            original_read_text = Path.read_text

            def fake_run(cmd, **kwargs):
                if len(cmd) < 2 or cmd[1] != "exec" or not Path(cmd[0]).is_absolute():
                    return original_run(cmd, **kwargs)
                probe_worktree = Path(cmd[cmd.index("-C") + 1])
                (probe_worktree / f".waystone-sandbox-write-probe-{record.name}").write_text(
                    "waystone-sandbox-write-probe\n", encoding="utf-8")
                return types.SimpleNamespace(returncode=0)

            def fail_probe_stderr_read(path, *args, **kwargs):
                if path == record / "sandbox-probe.stderr":
                    raise PermissionError("injected unreadable probe stderr")
                return original_read_text(path, *args, **kwargs)

            delegate.subprocess.run = fake_run
            Path.read_text = fail_probe_stderr_read
            try:
                with self.assertRaises(delegate._RunnerProbeEvidenceFailure) as cm:
                    delegate._run_codex_sandbox_probe(root, "gpt-test", record)
            finally:
                Path.read_text = original_read_text
                delegate.subprocess.run = original_run
            self.assertIn("injected unreadable probe stderr", str(cm.exception))
            self.assertEqual(cm.exception.probe_result["classification"], "evidence")
            self.assertGreaterEqual(cm.exception.probe_result["duration_s"], 0)
            recorded = _json.loads((record / "sandbox-probe-result.json").read_text())
            self.assertEqual(recorded["classification"], "evidence")
            self.assertEqual(recorded["cleanup_state"], "cleaned")

    def test_codex_probe_internal_defect_is_labeled_honestly_not_transport(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            record = Path(d) / "record"
            record.mkdir()
            original = delegate._run_codex_sandbox_probe

            def broken_probe(*args, **kwargs):
                raise TypeError("injected harness defect")

            delegate._run_codex_sandbox_probe = broken_probe
            try:
                with self.assertRaises(delegate._RunnerProbeFailure) as cm:
                    delegate._run_codex(root, "gpt-test", record / "prompt.txt", record)
            finally:
                delegate._run_codex_sandbox_probe = original
            self.assertIn("harness defect, not transport", str(cm.exception))
            self.assertEqual(cm.exception.probe_result["classification"], "internal")
            recorded = _json.loads((record / "sandbox-probe-result.json").read_text())
            self.assertEqual(recorded["classification"], "internal")
            self.assertEqual(recorded["detail"], "TypeError: injected harness defect")

    def test_codex_sandbox_probe_cleans_partially_created_worktree_after_timeout(self):
        import json as _json

        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            record = Path(d) / "record"
            record.mkdir()
            original_git = delegate._git
            observed = {}

            def timeout_after_create(cwd, *args, **kwargs):
                if args[:3] == ("worktree", "add", "--detach"):
                    recorded = _json.loads((record / "sandbox-probe-result.json").read_text())
                    observed["creation_state_at_attempt"] = recorded["creation_state"]
                    result = original_git(cwd, *args, **kwargs)
                    observed["path"] = Path(args[3])
                    self.assertEqual(result[0], 0)
                    return 127, "", "injected worktree add timeout"
                return original_git(cwd, *args, **kwargs)

            delegate._git = timeout_after_create
            try:
                with self.assertRaises(delegate._RunnerProbeLifecycleFailure) as cm:
                    delegate._run_codex_sandbox_probe(root, "gpt-test", record)
            finally:
                delegate._git = original_git
            self.assertEqual(observed["creation_state_at_attempt"], "attempted")
            self.assertFalse(os.path.lexists(observed["path"]))
            self.assertNotIn(
                str(observed["path"]), git(root, "worktree", "list", "--porcelain").stdout)
            self.assertEqual(cm.exception.probe_result["classification"], "lifecycle")
            self.assertEqual(cm.exception.probe_result["cleanup_state"], "cleaned")

    def test_codex_sandbox_probe_auto_cleans_stale_sibling_before_next_attempt(self):
        import types

        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            record = Path(d) / "record"
            record.mkdir()
            stale = delegate._sandbox_probe_worktree_path(root, record)
            self.assertEqual(git(root, "worktree", "add", "--detach", str(stale), "HEAD").returncode, 0)
            (stale / "stale.txt").write_text("stale\n", encoding="utf-8")
            original = delegate.subprocess.run

            def fake(cmd, **kwargs):
                if len(cmd) < 2 or cmd[1] != "exec" or not Path(cmd[0]).is_absolute():
                    return original(cmd, **kwargs)
                probe_worktree = Path(cmd[cmd.index("-C") + 1])
                self.assertFalse((probe_worktree / "stale.txt").exists())
                (probe_worktree / f".waystone-sandbox-write-probe-{record.name}").write_text(
                    "waystone-sandbox-write-probe\n", encoding="utf-8")
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            try:
                result = delegate._run_codex_sandbox_probe(root, "gpt-test", record)
            finally:
                delegate.subprocess.run = original
            self.assertTrue(result["stale_detected"])
            self.assertEqual(result["stale_cleanup_state"], "cleaned")
            self.assertFalse(os.path.lexists(stale))

    def test_codex_runner_total_duration_includes_recorded_probe_duration(self):
        import json as _json
        import types

        with tempfile.TemporaryDirectory() as d:
            root, _home = self._project(d)
            record = Path(d) / "record"
            record.mkdir()
            prompt = Path(d) / "prompt.txt"
            prompt.write_text("implement", encoding="utf-8")
            original_run = delegate.subprocess.run
            original_monotonic = delegate.time.monotonic
            ticks = iter((10.0, 11.0, 13.0, 16.0))

            def fake(cmd, **kwargs):
                if len(cmd) < 2 or cmd[1] != "exec" or not Path(cmd[0]).is_absolute():
                    return original_run(cmd, **kwargs)
                if "--ephemeral" in cmd:
                    probe_worktree = Path(cmd[cmd.index("-C") + 1])
                    (probe_worktree / f".waystone-sandbox-write-probe-{record.name}").write_text(
                        "waystone-sandbox-write-probe\n", encoding="utf-8")
                return types.SimpleNamespace(returncode=0)

            delegate.subprocess.run = fake
            delegate.time.monotonic = lambda: next(ticks)
            try:
                rc, duration = delegate._run_codex(root, "gpt-test", prompt, record)
            finally:
                delegate.time.monotonic = original_monotonic
                delegate.subprocess.run = original_run
            self.assertEqual(rc, 0)
            self.assertEqual(duration, 6.0)
            probe = _json.loads((record / "sandbox-probe-result.json").read_text())
            self.assertEqual(probe["duration_s"], 2.0)

    def test_binary_change_preserved_in_patch(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "blob.bin").write_bytes(bytes(range(256)))
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                self._run(root, home, fake)
            rec = self._record_dir(root, home)
            patch = (rec / "artifact" / "changes.patch").read_text()
            self.assertIn("GIT binary patch", patch)

    def test_env_prep_failure_is_failed_env_no_runner(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / ".waystone.yml").write_text(
                "version: 1\nproject: demo\ndelegation:\n  env_prep:\n    - \"false\"\n")
            called = {"n": 0}

            def fake(*a, **k):
                called["n"] += 1
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake)
            self.assertEqual(called["n"], 0)  # runner never invoked
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-env")

    def test_runner_failure_is_failed_runner_with_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"}, rc=3)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-runner")
            self.assertTrue((rec / "exposure.json").exists())  # exposure recorded before runner

    def test_run_refuses_preexisting_waystone_report(self):
        # H2 repro: an untracked WAYSTONE_REPORT.yaml in the user's tree must refuse the run entirely —
        # before any record is created (no phantom deletion via the patch).
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / "WAYSTONE_REPORT.yaml").write_text("stale: report\n")  # untracked user file
            with self.assertRaises(delegate.WorkflowError) as cm:
                self._run(root, home, self._fake_runner({"impl.py": "x\n"}))
            self.assertIn("WAYSTONE_REPORT.yaml", str(cm.exception))
            self.assertFalse(_run_with_home(home, lambda: delegate._delegations_dir(root)).exists())

    def test_non_utf8_text_change_roundtrip(self):
        # H1 repro: latin-1 content (0xE9, no NUL -> git classifies it as text) must not crash the
        # harness — the patch is bytes and must never round-trip through strict UTF-8.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "cafe.txt").write_bytes(b"caf\xe9 au lait\n")
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self._run(root, home, fake)
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "needs-review")
            self.assertTrue((rec / "artifact" / "contract.yaml").exists())
            patch = (rec / "artifact" / "changes.patch").read_bytes()
            self.assertIn(b"caf\xe9 au lait", patch)  # original bytes preserved, not mangled

    def test_non_utf8_report_marked_invalid(self):
        # H1: a non-UTF-8 WAYSTONE_REPORT.yaml must surface as delegate_report invalid, not crash the run
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (worktree / "impl.py").write_text("x\n")
                (worktree / "WAYSTONE_REPORT.yaml").write_bytes(b"verification: caf\xe9\n")
                (record_dir / "last_message.md").write_text("x")
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                rc = self._run(root, home, fake)
            self.assertEqual(rc, 0)
            rec = self._record_dir(root, home)
            contract = yaml.safe_load((rec / "artifact" / "contract.yaml").read_text())
            self.assertEqual(contract["delegate_report"]["present"], "invalid")

    def test_artifact_failure_is_failed_artifact_lock_held(self):
        # H1: a post-runner artifact-computation failure must not strand the record as `running`
        # (permanent owner-lock) — it transitions to failed-artifact, preserves the worktree as
        # evidence, and keeps the lock. A broken git worktree must make discard cleanup fail loud.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def fake(worktree, model, prompt_path, record_dir):
                (record_dir / "last_message.md").write_text("x")
                (worktree / ".git").unlink()  # breaks the result snapshot
                return (0, 0.1)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake)
            rec = self._record_dir(root, home)
            self.assertEqual(delegate._read_status(rec)["state"], "failed-artifact")
            wt = _run_with_home(home, lambda: delegate._worktree_path(root, rec.name))
            self.assertTrue(wt.exists())  # evidence preserved
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError) as cm:
                    self._run(root, home, self._fake_runner({"impl.py": "y\n"}))
                self.assertIn("already has active delegation", str(cm.exception))
                with self.assertRaises(delegate.WorkflowError) as cleanup:
                    _run_with_home(
                        home, lambda: delegate.discard_delegation(
                            root, rec.name, "clear failed artifact"))
            self.assertIn("git worktree remove", str(cleanup.exception))
            status = delegate._read_status(rec)
            self.assertEqual(status["state"], "discarding")
            self.assertEqual(status["at_transitions"][-1]["reason"], "clear failed artifact")

    def test_same_second_did_gets_suffix(self):
        # H4: two delegations minted in the same second must land in two independent records —
        # deterministic -2/-3... suffix, no state transitions appended to the first record.
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            (root / "tasks.yaml").write_text(
                "version: 1\nproject: demo\ntasks:\n"
                '  - id: feat/xyz\n    title: "implement xyz feature"\n    status: active\n'
                '    accept:\n      - "criterion alpha here"\n'
                '  - id: feat/two\n    title: "the second task here"\n    status: active\n'
                '    accept:\n      - "criterion beta here"\n')
            orig = delegate._make_did
            delegate._make_did = lambda tid: "20260713T120000Z-fixed"
            import contextlib
            import io
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc1 = self._run(root, home, self._fake_runner({"a.py": "x\n"}), task="feat/xyz")
                    rc2 = self._run(root, home, self._fake_runner({"b.py": "y\n"}), task="feat/two")
            finally:
                delegate._make_did = orig
            self.assertEqual((rc1, rc2), (0, 0))
            ddir = _run_with_home(home, lambda: delegate._delegations_dir(root))
            names = sorted(p.name for p in ddir.iterdir())
            self.assertEqual(names, ["20260713T120000Z-fixed", "20260713T120000Z-fixed-2"])
            st1 = delegate._read_status(ddir / names[0])
            self.assertEqual(st1["state"], "needs-review")
            self.assertEqual(len(st1["at_transitions"]), 2)  # running -> needs-review only, untouched
            import json as _json
            exp2 = _json.loads((ddir / names[1] / "exposure.json").read_text())
            self.assertEqual(exp2["task_id"], "feat/two")

    def test_owner_lock_refuses_second_delegation_from_failed_state(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake_fail = self._fake_runner({"impl.py": "x\n"}, rc=3)
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(delegate.WorkflowError):
                    self._run(root, home, fake_fail)  # -> failed-runner (non-terminal, holds lock)
                with self.assertRaises(delegate.WorkflowError) as cm:
                    self._run(root, home, self._fake_runner({"impl.py": "y\n"}))
            self.assertIn("already has active delegation", str(cm.exception))

    def test_claim_without_exposure_blocks_second_cli_run(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            fake = self._fake_runner({"impl.py": "x\n"})
            original_snapshot = delegate._snapshot
            original_runner = delegate._run_codex
            observed = {}

            def snapshot(cwd, message, *, exclude_uv_cache=False):
                if Path(cwd).resolve() == root.resolve() and not observed:
                    records = sorted(delegate._delegations_dir(root).iterdir())
                    self.assertEqual(len(records), 1)
                    claim = records[0] / "claim.json"
                    self.assertTrue(claim.is_file())
                    self.assertFalse((records[0] / "exposure.json").exists())
                    err = io.StringIO()
                    with contextlib.redirect_stderr(err):
                        observed["second_rc"] = delegate.main(
                            ["run", "feat/xyz", "--root", str(root)])
                    observed["second_err"] = err.getvalue()
                return original_snapshot(cwd, message, exclude_uv_cache=exclude_uv_cache)

            delegate._snapshot = snapshot
            delegate._run_codex = fake
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    first_rc = _run_with_home(
                        home, lambda: delegate.main(
                            ["run", "feat/xyz", "--root", str(root)]))
            finally:
                delegate._snapshot = original_snapshot
                delegate._run_codex = original_runner
            self.assertEqual(first_rc, 0)
            self.assertEqual(observed["second_rc"], 1)
            self.assertIn("already has active delegation", observed["second_err"])

    def test_claim_only_crash_remnant_is_discardable(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)

            def claim():
                plan = delegate._prepare_run(root, "feat/xyz", "implementer", [])
                with common.hold_lock(common.project_lock_path(root)):
                    return delegate._claim_run(root, plan)

            did, rec = _run_with_home(home, claim)
            self.assertTrue((rec / "claim.json").is_file())
            self.assertFalse((rec / "exposure.json").exists())
            with contextlib.redirect_stdout(io.StringIO()):
                rc = _run_with_home(home, lambda: delegate.main(
                    ["discard", did, "--root", str(root), "--reason", "clear incomplete claim"]))
            self.assertEqual(rc, 0)
            self.assertEqual(delegate._read_status(rec)["state"], "discarded")
            self.assertIsNone(delegate._active_delegation_for_task(root, "feat/xyz"))

    def test_base_ref_creation_uses_cas_and_detects_collision(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(d)
            original_snapshot = delegate._snapshot
            original_runner = delegate._run_codex
            injected = {"done": False}
            runner_calls = {"count": 0}

            def snapshot(cwd, message, *, exclude_uv_cache=False):
                result = original_snapshot(cwd, message, exclude_uv_cache=exclude_uv_cache)
                if Path(cwd).resolve() == root.resolve() and not injected["done"]:
                    injected["done"] = True
                    did = next(delegate._delegations_dir(root).iterdir()).name
                    self.assertEqual(git(
                        root, "update-ref", f"refs/waystone/delegations/{did}", result[0]
                    ).returncode, 0)
                return result

            def runner(*args, **kwargs):
                runner_calls["count"] += 1
                return (0, 0.1)

            delegate._snapshot = snapshot
            delegate._run_codex = runner
            err = io.StringIO()
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                    rc = _run_with_home(home, lambda: delegate.main(
                        ["run", "feat/xyz", "--root", str(root)]))
            finally:
                delegate._snapshot = original_snapshot
                delegate._run_codex = original_runner
            self.assertEqual(rc, 1)
            self.assertTrue(injected["done"])
            self.assertEqual(runner_calls["count"], 0)
            self.assertIn("git update-ref failed", err.getvalue())
            rec = next(delegate._delegations_dir(root).iterdir())
            self.assertTrue((rec / "claim.json").is_file())
            self.assertFalse((rec / "exposure.json").exists())


class CodexRunnerVerificationGateTests(unittest.TestCase):
    def setUp(self):
        self.proof = {
            "schema": "waystone-codex-runner-proof-3",
            "resolved_codex_path": "/opt/waystone-test/bin/codex",
            "codex_version": {"stdout": "codex-cli 9.9.9", "stderr": "build test"},
            "codex_executable": {"size": 1234, "mtime_ns": 5678},
            "hostname": "test-machine",
            "host_identity": {"source": "/etc/machine-id", "value": "test-host-id"},
            "platform": {"system": "TestOS", "machine": "test-arch"},
            "kernel": {"release": "1.2.3", "version": "test-kernel"},
            "sandbox_invocation_contract": "codex-exec:workspace-write:v1",
            "host_sandbox_observation": {
                "source": "none", "status": "not-observed", "platform": "TestOS",
            },
            "execution_principal": {
                "effective_uid": 1000, "effective_gid": 1000,
                "supplementary_groups": [20, 1000],
            },
            "codex_config_root": {
                "source": "default", "configured_path": "~/.codex",
                "resolved_path": "/home/waystone-test/.codex", "status": "not-present",
                "config_toml": {
                    "path": "/home/waystone-test/.codex/config.toml",
                    "status": "not-present",
                },
            },
            "process_context": {
                "Seccomp": {
                    "source": "/proc/self/status", "status": "observed", "value": "2",
                },
                "NoNewPrivs": {
                    "source": "/proc/self/status", "status": "observed", "value": "1",
                },
                "CapEff": {
                    "source": "/proc/self/status", "status": "observed",
                    "value": "0000000000000000",
                },
                "security_label": {
                    "source": "/proc/self/attr/current", "status": "observed",
                    "value": "waystone-test (enforce)",
                },
            },
            "worktree_cache_mount": {
                "device_boundary": "/test-cache", "device": 42, "filesystem_id": 84,
                "readonly": False,
            },
        }
        self.original_fingerprint = delegate._codex_runner_fingerprint
        delegate._codex_runner_fingerprint = lambda _worktree: _json.loads(
            _json.dumps(self.proof))

    def tearDown(self):
        delegate._codex_runner_fingerprint = self.original_fingerprint

    def _proof_text(self, proof=None):
        return _json.dumps(
            self.proof if proof is None else proof,
            ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    def _passed_probe(self):
        return {
            "schema": "waystone-sandbox-probe-1",
            "outcome": "passed",
            "worktree_cache_mount": _json.loads(_json.dumps(
                self.proof["worktree_cache_mount"])),
        }

    @staticmethod
    def _fixture(base: Path, config: bytes) -> tuple[Path, Path, Path, Path]:
        import json

        root = base / "repo"
        worktree = base / "worktree"
        record = base / "record"
        root.mkdir()
        worktree.mkdir()
        record.mkdir()
        (root / ".waystone.yml").write_bytes(config)
        init_repo(root)
        (record / "exposure.json").write_text(
            json.dumps({"project": {"root": str(root)}}) + "\n", encoding="utf-8")
        prompt = base / "prompt.txt"
        prompt.write_text("implement", encoding="utf-8")
        return root, worktree, record, prompt

    def test_runtime_fingerprint_records_all_bounded_axes(self):
        import types
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\nprintf 'codex-cli 7.8.9\\n'\nprintf 'build abc\\n' >&2\n")
            fake_codex.chmod(0o755)
            worktree = base / "worktree"
            worktree.mkdir()
            host = {"source": "test-host", "value": "host-123"}
            observed = {"source": "test", "status": "observed", "value": "test-lsm"}
            principal = {
                "effective_uid": 501, "effective_gid": 20,
                "supplementary_groups": [20, 80],
            }
            config_root = {
                "source": "CODEX_HOME", "configured_path": str(base / "codex-home"),
                "resolved_path": str(base / "codex-home"), "status": "not-present",
            }
            process_context = {
                "Seccomp": {
                    "source": "/proc/self/status", "status": "observed", "value": "2",
                },
                "NoNewPrivs": {
                    "source": "/proc/self/status", "status": "observed", "value": "1",
                },
                "CapEff": {
                    "source": "/proc/self/status", "status": "observed", "value": "0",
                },
                "security_label": {
                    "source": "/proc/self/attr/current", "status": "observed",
                    "value": "test-profile",
                },
            }
            uname = types.SimpleNamespace(
                node="", system="TestOS", machine="test-arch",
                release="1.2.3", version="test-kernel")
            with mock.patch.dict(os.environ, {"PATH": str(fake_bin)}), \
                 mock.patch.object(delegate.platform, "uname", return_value=uname), \
                 mock.patch.object(delegate, "_stable_host_identity", return_value=host), \
                 mock.patch.object(delegate, "_host_sandbox_observation", return_value=observed), \
                 mock.patch.object(delegate, "_execution_principal_identity",
                                   return_value=principal), \
                 mock.patch.object(delegate, "_codex_config_root_identity",
                                   return_value=config_root), \
                 mock.patch.object(delegate, "_process_security_context",
                                   return_value=process_context):
                proof = self.original_fingerprint(worktree)

            self.assertEqual(proof["schema"], "waystone-codex-runner-proof-3")
            self.assertEqual(proof["resolved_codex_path"], str(fake_codex.resolve()))
            self.assertEqual(proof["codex_version"], {
                "stdout": "codex-cli 7.8.9", "stderr": "build abc"})
            self.assertEqual(proof["codex_executable"], {
                "size": fake_codex.stat().st_size, "mtime_ns": fake_codex.stat().st_mtime_ns})
            self.assertEqual(proof["hostname"], "")
            self.assertEqual(proof["host_identity"], host)
            self.assertTrue(proof["platform"]["system"])
            self.assertTrue(proof["platform"]["machine"])
            self.assertTrue(proof["kernel"]["release"])
            self.assertTrue(proof["kernel"]["version"])
            self.assertEqual(
                proof["sandbox_invocation_contract"], "codex-exec:workspace-write:v1")
            self.assertEqual(proof["host_sandbox_observation"], observed)
            self.assertEqual(proof["execution_principal"], principal)
            self.assertEqual(proof["codex_config_root"], config_root)
            self.assertEqual(proof["process_context"], process_context)
            mount = proof["worktree_cache_mount"]
            self.assertTrue(Path(mount["device_boundary"]).is_absolute())
            self.assertEqual(mount["device"], worktree.stat().st_dev)
            self.assertIsInstance(mount["filesystem_id"], int)
            self.assertIsInstance(mount["readonly"], bool)

    def test_execution_principal_is_normalized_and_collection_failures_are_closed(self):
        from unittest import mock

        with mock.patch.object(delegate.os, "geteuid", return_value=501), \
             mock.patch.object(delegate.os, "getegid", return_value=20), \
             mock.patch.object(delegate.os, "getgroups", return_value=[80, 20, 80]):
            self.assertEqual(delegate._execution_principal_identity(), {
                "effective_uid": 501,
                "effective_gid": 20,
                "supplementary_groups": [20, 80],
            })

        for function in ("geteuid", "getegid", "getgroups"):
            with self.subTest(function=function), \
                 mock.patch.object(
                     delegate.os, function, side_effect=OSError(f"{function} denied")):
                with self.assertRaisesRegex(
                        common.WorkflowError, rf"execution principal.*{function} denied"):
                    delegate._execution_principal_identity()

    def test_codex_config_root_records_digest_only_stat_diagnostics_and_honest_absence(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            target = base / "actual-codex-home"
            target.mkdir()
            config_content = b'model = "gpt-test"\nsecret = "must-not-be-recorded"\n'
            (target / "config.toml").write_bytes(config_content)
            configured = base / "codex-home-link"
            configured.symlink_to(target, target_is_directory=True)
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(configured)}):
                identity = delegate._codex_config_root_identity()
            info = target.stat()
            self.assertEqual(identity, {
                "source": "CODEX_HOME",
                "configured_path": str(configured),
                "resolved_path": str(target.resolve()),
                "status": "present",
                "stat": {
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "mode": info.st_mode,
                    "uid": info.st_uid,
                    "gid": info.st_gid,
                    "size": info.st_size,
                    "mtime_ns": info.st_mtime_ns,
                    "ctime_ns": info.st_ctime_ns,
                },
                "config_toml": {
                    "path": str(target.resolve() / "config.toml"),
                    "status": "present",
                    "digest": "sha256:" + hashlib.sha256(config_content).hexdigest(),
                },
            })
            self.assertNotIn("must-not-be-recorded", _json.dumps(identity))

            home = base / "missing-home"
            home.mkdir()
            with mock.patch.dict(os.environ, {"CODEX_HOME": ""}), \
                 mock.patch.object(Path, "home", return_value=home):
                missing = delegate._codex_config_root_identity()
            self.assertEqual(missing, {
                "source": "default",
                "configured_path": "~/.codex",
                "resolved_path": str((home / ".codex").resolve()),
                "status": "not-present",
                "config_toml": {
                    "path": str((home / ".codex" / "config.toml").resolve()),
                    "status": "not-present",
                },
            })

    def test_codex_config_toml_read_failure_blocks_marker_reuse(self):
        from unittest import mock

        with tempfile.TemporaryDirectory() as d:
            config_home = Path(d) / "codex-home"
            config_home.mkdir()
            (config_home / "config.toml").write_text('model = "gpt-test"\n')
            with mock.patch.dict(os.environ, {"CODEX_HOME": str(config_home)}), \
                 mock.patch.object(
                     Path, "read_bytes", side_effect=PermissionError("config denied")):
                identity = delegate._codex_config_root_identity()

            self.assertEqual(identity["config_toml"], {
                "path": str(config_home.resolve() / "config.toml"),
                "status": "not-observed",
                "reason": "PermissionError",
            })
            unavailable = _json.loads(_json.dumps(self.proof))
            unavailable["codex_config_root"] = identity
            self.assertEqual(
                delegate._codex_runner_reuse_blockers(unavailable), ["codex_config_root"])

    def test_codex_config_content_change_reprobes_without_directory_stat_change(self):
        import contextlib
        import io
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, worktree, record, prompt = self._fixture(base, original)
            config_home = base / "codex-home"
            config_home.mkdir()
            config_path = config_home / "config.toml"
            config_a = b'model = "gpt-a"\nsecret = "proof-secret-a"\n'
            config_b = b'model = "gpt-b"\nsecret = "proof-secret-b"\n'
            config_path.write_bytes(config_a)
            directory_stat = config_home.stat()
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def fingerprint(_worktree):
                proof = _json.loads(_json.dumps(self.proof))
                proof["codex_config_root"] = delegate._codex_config_root_identity()
                return proof

            def probe(*args, **kwargs):
                calls["probe"] += 1
                result = self._passed_probe()
                result["worktree_cache_mount"] = _json.loads(_json.dumps(
                    kwargs["fingerprint"]["worktree_cache_mount"]))
                return result

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._codex_runner_fingerprint = fingerprint
            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                with mock.patch.dict(os.environ, {"CODEX_HOME": str(config_home)}), \
                     contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
                    first_marker = (
                        root / ".waystone" / "codex-runner-verified").read_text()
                    config_path.write_bytes(config_b)
                    changed_directory_stat = config_home.stat()
                    self.assertEqual(
                        (directory_stat.st_ino, directory_stat.st_size,
                         directory_stat.st_mtime_ns, directory_stat.st_ctime_ns),
                        (changed_directory_stat.st_ino, changed_directory_stat.st_size,
                         changed_directory_stat.st_mtime_ns,
                         changed_directory_stat.st_ctime_ns),
                    )
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            marker = (root / ".waystone" / "codex-runner-verified").read_text()
            self.assertEqual(calls, {"probe": 2, "runner": 2})
            self.assertNotEqual(first_marker, marker)
            self.assertEqual(
                _json.loads(marker)["codex_config_root"]["config_toml"]["digest"],
                "sha256:" + hashlib.sha256(config_b).hexdigest(),
            )
            self.assertNotIn("proof-secret-a", marker)
            self.assertNotIn("proof-secret-b", marker)

    def test_probe_config_root_directory_churn_is_diagnostic_and_absent_config_reuses(self):
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, worktree, record, prompt = self._fixture(base, original)
            config_home = base / "codex-home"
            config_home.mkdir()
            before = _json.loads(_json.dumps(self.proof))
            before["codex_config_root"] = {
                "source": "CODEX_HOME",
                "configured_path": str(config_home),
                "resolved_path": str(config_home.resolve()),
                "status": "present",
                "stat": {"mtime_ns": 1, "ctime_ns": 1},
                "config_toml": {
                    "path": str(config_home.resolve() / "config.toml"),
                    "status": "not-present",
                },
            }
            after = _json.loads(_json.dumps(before))
            after["codex_config_root"]["stat"] = {"mtime_ns": 2, "ctime_ns": 2}
            fingerprint_calls = {"count": 0}
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def fingerprint(_worktree):
                fingerprint_calls["count"] += 1
                proof = before if fingerprint_calls["count"] <= 2 else after
                return _json.loads(_json.dumps(proof))

            def probe(*args, **kwargs):
                calls["probe"] += 1
                result = self._passed_probe()
                result["worktree_cache_mount"] = _json.loads(_json.dumps(
                    kwargs["fingerprint"]["worktree_cache_mount"]))
                return result

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._codex_runner_fingerprint = fingerprint
            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                with mock.patch.dict(os.environ, {"CODEX_HOME": str(config_home)}):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            marker = _json.loads(
                (root / ".waystone" / "codex-runner-verified").read_text())
            self.assertEqual(calls, {"probe": 1, "runner": 2})
            self.assertEqual(marker["codex_config_root"], after["codex_config_root"])

    def test_linux_process_context_records_each_axis_and_marks_unobserved_explicitly(self):
        from unittest import mock

        status = (
            "Name:\tpython\n"
            "NoNewPrivs:\t1\n"
            "Seccomp:\t2\n"
            "CapEff:\t00000000a80425fb\n"
        )
        with mock.patch.object(
                Path, "read_text", side_effect=[status, "docker-default (enforce)\n"]):
            context = delegate._process_security_context("Linux")
        self.assertEqual(context, {
            "Seccomp": {
                "source": "/proc/self/status", "status": "observed", "value": "2",
            },
            "NoNewPrivs": {
                "source": "/proc/self/status", "status": "observed", "value": "1",
            },
            "CapEff": {
                "source": "/proc/self/status", "status": "observed",
                "value": "00000000a80425fb",
            },
            "security_label": {
                "source": "/proc/self/attr/current", "status": "observed",
                "value": "docker-default (enforce)",
            },
        })

        with mock.patch.object(
                Path, "read_text", side_effect=["Seccomp:\t2\n", FileNotFoundError()]):
            partial = delegate._process_security_context("Linux")
        self.assertEqual(partial["Seccomp"]["status"], "observed")
        self.assertEqual(partial["NoNewPrivs"], {
            "source": "/proc/self/status", "status": "not-observed", "reason": "missing",
        })
        self.assertEqual(partial["CapEff"], {
            "source": "/proc/self/status", "status": "not-observed", "reason": "missing",
        })
        self.assertEqual(partial["security_label"], {
            "source": "/proc/self/attr/current", "status": "not-observed",
            "reason": "not-present",
        })

        unsupported = delegate._process_security_context("Darwin")
        self.assertEqual(set(unsupported), {"Seccomp", "NoNewPrivs", "CapEff", "security_label"})
        self.assertTrue(all(
            axis["status"] == "not-observed" and axis["reason"] == "unsupported-platform"
            for axis in unsupported.values()))

    def test_stable_host_identity_uses_machine_id_and_ioplatformuuid_fail_closed(self):
        import types
        from unittest import mock

        machine_id = "0123456789abcdef0123456789abcdef"
        with mock.patch.object(Path, "read_text", return_value=machine_id + "\n"):
            self.assertEqual(delegate._stable_host_identity("Linux"), {
                "source": "/etc/machine-id", "value": machine_id})
        with mock.patch.object(Path, "read_text", side_effect=OSError("denied")):
            with self.assertRaisesRegex(common.WorkflowError, "machine identity.*denied"):
                delegate._stable_host_identity("Linux")

        for invalid in ("", " \n", "uninitialized\n", "UNINITIALIZED"):
            with self.subTest(machine_id=invalid), \
                 mock.patch.object(Path, "read_text", return_value=invalid):
                with self.assertRaisesRegex(common.WorkflowError, "invalid sentinel|empty"):
                    delegate._stable_host_identity("Linux")

        for invalid in (
                "linux-host-id", "0123456789ABCDEF0123456789ABCDEF",
                "0123456789abcdef0123456789abcde", "g" * 32, "0" * 32):
            with self.subTest(machine_id=invalid), \
                 mock.patch.object(Path, "read_text", return_value=invalid):
                with self.assertRaisesRegex(
                        common.WorkflowError,
                        "non-zero 32-character lowercase hexadecimal"):
                    delegate._stable_host_identity("Linux")

        uuid = "01234567-89AB-CDEF-0123-456789ABCDEF"
        ioreg = types.SimpleNamespace(
            returncode=0,
            stdout=f'    "IOPlatformUUID" = "{uuid}"\n',
            stderr="",
        )
        with mock.patch.object(delegate.subprocess, "run", return_value=ioreg):
            self.assertEqual(delegate._stable_host_identity("Darwin"), {
                "source": "IOPlatformUUID", "value": uuid})
        malformed = types.SimpleNamespace(
            returncode=0, stdout='"IOPlatformUUID" = "not-a-uuid"\n', stderr="")
        with mock.patch.object(delegate.subprocess, "run", return_value=malformed):
            with self.assertRaisesRegex(common.WorkflowError, "invalid UUID format"):
                delegate._stable_host_identity("Darwin")
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="not available")
        with mock.patch.object(delegate.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(common.WorkflowError, "IOPlatformUUID.*not available"):
                delegate._stable_host_identity("Darwin")

    def test_linux_lsm_observation_is_explicitly_best_effort(self):
        from unittest import mock

        with mock.patch.object(Path, "read_text", return_value="landlock,apparmor\n"):
            self.assertEqual(delegate._host_sandbox_observation("Linux"), {
                "source": "/sys/kernel/security/lsm", "status": "observed",
                "value": "landlock,apparmor",
            })
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertEqual(delegate._host_sandbox_observation("Linux"), {
                "source": "/sys/kernel/security/lsm", "status": "unavailable",
            })

    def test_hostname_change_is_diagnostic_only_and_skips_probe(self):
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            (marker.parent / ".gitignore").write_text("*\n")
            recorded = _json.loads(_json.dumps(self.proof))
            recorded["hostname"] = "Mac.local"
            marker.write_text(self._proof_text(recorded))
            calls = {"runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                raise AssertionError("a hostname-only change must not rerun the probe")

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                self.assertEqual(delegate._run_codex(
                    worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text(recorded))
            self.assertEqual(_json.loads(marker.read_text())["hostname"], "Mac.local")

    def test_environment_identity_and_version_mismatch_reprobe_and_name_axes(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        cases = (
            ({"host_identity": {"source": "/etc/machine-id", "value": "other-host-id"}},
             ("host_identity",)),
            ({"platform": {"system": "OtherOS", "machine": "other-arch"},
              "worktree_cache_mount": {
                  "device_boundary": "/foreign-cache", "device": 999,
                  "filesystem_id": 1000, "readonly": True,
              }}, ("platform", "worktree_cache_mount")),
            ({"codex_version": {"stdout": "codex-cli 9.9.8", "stderr": "build test"}},
             ("codex_version",)),
            ({"codex_version": {"stdout": "codex-cli 9.9.9"}},
             ("codex_version",)),
            ({"codex_version": {"stdout": "codex-cli 9.9.9", "stderr": None}},
             ("codex_version",)),
            ({"worktree_cache_mount": {
                "device_boundary": "/test-cache", "device": 42, "filesystem_id": 84,
                "readonly": 0,
            }}, ("worktree_cache_mount",)),
            ({"execution_principal": {
                "effective_uid": 2000, "effective_gid": 1000,
                "supplementary_groups": [20, 1000],
            }}, ("execution_principal",)),
            ({"codex_config_root": {
                "source": "CODEX_HOME", "configured_path": "/foreign/.codex",
                "resolved_path": "/foreign/.codex", "status": "not-present",
            }}, ("codex_config_root",)),
            ({"foreign_axis": None}, ("foreign_axis",)),
        )
        for changes, changed_axes in cases:
            with self.subTest(changed_axes=changed_axes), tempfile.TemporaryDirectory() as d:
                root, worktree, record, prompt = self._fixture(Path(d), original)
                marker = root / ".waystone" / "codex-runner-verified"
                marker.parent.mkdir()
                (marker.parent / ".gitignore").write_text("*\n")
                stale = _json.loads(_json.dumps(self.proof))
                stale.update(changes)
                marker.write_text(self._proof_text(stale))
                calls = {"probe": 0, "runner": 0}
                original_probe = delegate._run_codex_sandbox_probe
                original_run = delegate.subprocess.run

                def probe(*args, **kwargs):
                    calls["probe"] += 1
                    result = self._passed_probe()
                    result["worktree_cache_mount"] = _json.loads(_json.dumps(
                        kwargs["fingerprint"]["worktree_cache_mount"]))
                    return result

                def run(*args, **kwargs):
                    if args and args[0] and args[0][0] == "git":
                        return original_run(*args, **kwargs)
                    calls["runner"] += 1
                    return types.SimpleNamespace(returncode=0)

                delegate._run_codex_sandbox_probe = probe
                delegate.subprocess.run = run
                stderr = io.StringIO()
                try:
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(delegate._run_codex(
                            worktree, "gpt-test", prompt, record)[0], 0)
                finally:
                    delegate.subprocess.run = original_run
                    delegate._run_codex_sandbox_probe = original_probe

                self.assertEqual(calls, {"probe": 1, "runner": 1})
                self.assertEqual(marker.read_text(), self._proof_text())
                self.assertIn("fingerprint mismatch", stderr.getvalue())
                for axis in changed_axes:
                    self.assertIn(axis, stderr.getvalue())

    def test_v2_marker_reprobes_once_and_is_rewritten_as_v3(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            (marker.parent / ".gitignore").write_text("*\n")
            legacy = _json.loads(_json.dumps(self.proof))
            legacy["schema"] = "waystone-codex-runner-proof-2"
            legacy["machine"] = legacy.pop("hostname")
            marker.write_text(self._proof_text(legacy))
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 2})
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertIn("fingerprint mismatch", stderr.getvalue())
            self.assertIn("schema", stderr.getvalue())

    def test_legacy_fixed_string_marker_is_reprobed_and_rewritten_as_json(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            (marker.parent / ".gitignore").write_text("*\n")
            marker.write_text("verified\n")
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertIn("legacy fixed-string", stderr.getvalue())
            self.assertIn("fresh preflight probe", stderr.getvalue())

    def test_fixed_stdout_shim_replacement_reprobes_via_executable_stat_identity(self):
        import contextlib
        import io
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, worktree, record, prompt = self._fixture(base, original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            (marker.parent / ".gitignore").write_text("*\n")
            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text("#!/bin/sh\nprintf 'codex-cli stable\\n'\n")
            fake_codex.chmod(0o755)
            path = str(fake_bin) + os.pathsep + os.environ["PATH"]
            host = {"source": "test-host", "value": "host-123"}
            observed = {"source": "test", "status": "observed", "value": "test-lsm"}

            with mock.patch.dict(os.environ, {"PATH": path}), \
                 mock.patch.object(delegate, "_stable_host_identity", return_value=host), \
                 mock.patch.object(delegate, "_host_sandbox_observation", return_value=observed):
                stale = self.original_fingerprint(worktree)
                marker.write_text(delegate._codex_runner_proof_text(stale))
                fake_codex.write_text(
                    "#!/bin/sh\n# replaced implementation\nprintf 'codex-cli stable\\n'\n")
                fake_codex.chmod(0o755)
                replaced = fake_codex.stat()
                os.utime(fake_codex, ns=(
                    replaced.st_atime_ns,
                    max(replaced.st_mtime_ns, stale["codex_executable"]["mtime_ns"] + 1_000_000),
                ))
                current = self.original_fingerprint(worktree)

                self.assertEqual(stale["codex_version"], current["codex_version"])
                self.assertNotEqual(stale["codex_executable"], current["codex_executable"])
                calls = {"probe": 0, "runner": 0}
                original_probe = delegate._run_codex_sandbox_probe
                original_run = delegate.subprocess.run

                def probe(*args, **kwargs):
                    calls["probe"] += 1
                    result = self._passed_probe()
                    result["worktree_cache_mount"] = _json.loads(_json.dumps(
                        kwargs["fingerprint"]["worktree_cache_mount"]))
                    return result

                def run(*args, **kwargs):
                    command = args[0] if args else []
                    if (command and command[0] == "git") or (
                            len(command) >= 2
                            and command[0] == str(fake_codex.resolve())
                            and command[1] == "--version"):
                        return original_run(*args, **kwargs)
                    calls["runner"] += 1
                    return types.SimpleNamespace(returncode=0)

                delegate._run_codex_sandbox_probe = probe
                delegate.subprocess.run = run
                stderr = io.StringIO()
                try:
                    with mock.patch.object(
                            delegate, "_codex_runner_fingerprint",
                            side_effect=self.original_fingerprint):
                        with contextlib.redirect_stderr(stderr):
                            self.assertEqual(delegate._run_codex(
                                worktree, "gpt-test", prompt, record)[0], 0)
                finally:
                    delegate.subprocess.run = original_run
                    delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertEqual(_json.loads(marker.read_text()), current)
            self.assertIn("codex_executable", stderr.getvalue())

    def test_dynamic_version_stderr_uses_resolved_path_and_still_records_proof(self):
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            root, _worktree, record, prompt = self._fixture(base, original)
            fake_bin = base / "bin"
            fake_bin.mkdir()
            fake_codex = fake_bin / "codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = --version ]; then\n"
                "  printf 'codex-cli stable\\n'\n"
                "  printf 'pid=%s\\n' \"$$\" >&2\n"
                "  exit 0\n"
                "fi\n"
                "exit 99\n")
            fake_codex.chmod(0o755)
            resolved = str(fake_codex.resolve())
            path = str(fake_bin) + os.pathsep + os.environ["PATH"]
            host = {"source": "test-host", "value": "host-123"}
            observed = {"source": "test", "status": "observed", "value": "test-lsm"}
            commands = []
            version_stderr = []
            original_run = delegate.subprocess.run

            def run(command, **kwargs):
                if command[0] == "git":
                    return original_run(command, **kwargs)
                self.assertEqual(command[0], resolved)
                if command[1] == "--version":
                    process = original_run(command, **kwargs)
                    version_stderr.append(process.stderr.strip())
                    return process
                self.assertEqual(command[1], "exec")
                commands.append(command)
                if "--ephemeral" in command:
                    probe_worktree = Path(command[command.index("-C") + 1])
                    (probe_worktree / f".waystone-sandbox-write-probe-{record.name}").write_text(
                        "waystone-sandbox-write-probe\n")
                return types.SimpleNamespace(returncode=0)

            with mock.patch.dict(os.environ, {"PATH": path}), \
                 mock.patch.object(delegate, "_stable_host_identity", return_value=host), \
                 mock.patch.object(delegate, "_host_sandbox_observation", return_value=observed), \
                 mock.patch.object(
                     delegate, "_codex_runner_fingerprint",
                     side_effect=self.original_fingerprint), \
                 mock.patch.object(delegate.subprocess, "run", side_effect=run):
                self.assertEqual(delegate._run_codex(
                    root, "gpt-test", prompt, record)[0], 0)
                self.assertEqual(delegate._run_codex(
                    root, "gpt-test", prompt, record)[0], 0)

            self.assertGreaterEqual(len(set(version_stderr)), 2)
            self.assertEqual(sum("--ephemeral" in command for command in commands), 1)
            self.assertEqual(len(commands), 3)
            self.assertTrue(all(command[0] == resolved for command in commands))
            marker = root / ".waystone" / "codex-runner-verified"
            proof = _json.loads(marker.read_text())
            self.assertEqual(proof["resolved_codex_path"], resolved)
            self.assertEqual(proof["codex_version"]["stdout"], "codex-cli stable")
            self.assertIn(proof["codex_version"]["stderr"], version_stderr)

    def test_probe_refuses_to_record_a_different_mount_than_its_write_target(self):
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, _worktree, record, _prompt = self._fixture(Path(d), original)
            probe_target = delegate._sandbox_probe_worktree_path(root, record)
            foreign_mount = {
                "device_boundary": "/foreign-cache", "device": 9001,
                "filesystem_id": 9002, "readonly": True,
            }
            observed_targets = []

            def mount_identity(target):
                observed_targets.append(target)
                self.assertEqual(target, probe_target)
                self.assertTrue(target.is_dir())
                return foreign_mount

            with mock.patch.object(
                    delegate, "_worktree_mount_identity", side_effect=mount_identity):
                with self.assertRaisesRegex(
                        delegate._RunnerProbeEvidenceFailure,
                        "refusing to prove one mount and record another") as failure:
                    delegate._run_codex_sandbox_probe(
                        root, "gpt-test", record, fingerprint=self.proof)

            self.assertEqual(observed_targets, [probe_target])
            self.assertEqual(
                failure.exception.probe_result["worktree_cache_mount"], foreign_mount)
            self.assertEqual(failure.exception.probe_result["cleanup_state"], "cleaned")
            self.assertFalse(os.path.lexists(probe_target))

    def test_lock_recheck_reports_fingerprint_mismatch_discovered_after_initial_check(self):
        import contextlib
        import io
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            (marker.parent / ".gitignore").write_text("*\n")
            marker.write_text(self._proof_text())
            stale = _json.loads(_json.dumps(self.proof))
            stale["host_identity"]["value"] = "appeared-after-initial-check"
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run
            original_flock = delegate.fcntl.flock

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            def flock(stream, operation):
                result = original_flock(stream, operation)
                if operation == delegate.fcntl.LOCK_EX:
                    marker.write_text(self._proof_text(stale))
                return result

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with mock.patch.object(delegate.fcntl, "flock", side_effect=flock):
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(delegate._run_codex(
                            worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertIn("fingerprint mismatch", stderr.getvalue())
            self.assertIn("host_identity", stderr.getvalue())

    def test_lock_recheck_suppresses_prelock_mismatch_if_race_resolves(self):
        import contextlib
        import io
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            (marker.parent / ".gitignore").write_text("*\n")
            stale = _json.loads(_json.dumps(self.proof))
            stale["host_identity"]["value"] = "resolved-before-lock-recheck"
            marker.write_text(self._proof_text(stale))
            calls = {"runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run
            original_flock = delegate.fcntl.flock

            def probe(*args, **kwargs):
                raise AssertionError("a race-resolved marker must skip the probe")

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            def flock(stream, operation):
                result = original_flock(stream, operation)
                if operation == delegate.fcntl.LOCK_EX:
                    marker.write_text(self._proof_text())
                return result

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with mock.patch.object(delegate.fcntl, "flock", side_effect=flock):
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(delegate._run_codex(
                            worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertEqual(stderr.getvalue(), "")

    def test_concurrent_runner_paths_probe_once_under_checkout_local_lock(self):
        import concurrent.futures
        import threading
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, first_record, prompt = self._fixture(Path(d), original)
            second_record = Path(d) / "record-2"
            second_record.mkdir()
            (second_record / "exposure.json").write_bytes(
                (first_record / "exposure.json").read_bytes())
            probe_started = threading.Event()
            release_probe = threading.Event()
            initial_check_barrier = threading.Barrier(2)
            initial_check_local = threading.local()
            counter_lock = threading.Lock()
            calls = {"initial_absent": 0, "probe": 0, "runner": 0}
            original_verification = delegate._codex_runner_verification_marker
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def verification(record, candidate_worktree):
                result = original_verification(record, candidate_worktree)
                if not getattr(initial_check_local, "passed", False):
                    initial_check_local.passed = True
                    with counter_lock:
                        calls["initial_absent"] += int(result is not None and not result[1])
                    initial_check_barrier.wait(5)
                return result

            def probe(*args, **kwargs):
                with counter_lock:
                    calls["probe"] += 1
                probe_started.set()
                if not release_probe.wait(5):
                    raise AssertionError("test did not release the first probe")
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                with counter_lock:
                    calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._codex_runner_verification_marker = verification
            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(
                            delegate._run_codex, worktree, "gpt-test", prompt, record)
                        for record in (first_record, second_record)
                    ]
                    try:
                        self.assertTrue(probe_started.wait(5))
                    finally:
                        release_probe.set()
                    self.assertEqual([future.result()[0] for future in futures], [0, 0])
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe
                delegate._codex_runner_verification_marker = original_verification

            self.assertEqual(calls, {"initial_absent": 2, "probe": 1, "runner": 2})
            self.assertEqual((root / ".waystone.yml").read_bytes(), original)
            self.assertEqual((root / ".waystone" / "codex-runner-verified").read_text(),
                             self._proof_text())

    def test_success_records_local_marker_and_skips_probe(self):
        import types

        original = (
            b"# keep top\r\nversion: 1\r\nproject: demo\r\ndelegation:\r\n"
            b"  # keep enabled comment\r\n  enabled: true # keep inline\r\n"
            b"state:\r\n  last_round_commit: null\r\n"
        )
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                self.assertEqual(delegate._run_codex(
                    worktree, "gpt-test", prompt, record)[0], 0)
                self.assertEqual(delegate._run_codex(
                    worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 2})
            self.assertEqual((root / ".waystone.yml").read_bytes(), original)
            marker = root / ".waystone" / "codex-runner-verified"
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertEqual(_json.loads(marker.read_text()), self.proof)
            self.assertEqual((root / ".waystone" / ".gitignore").read_text(), "*\n")
            self.assertFalse(any(record.glob("sandbox-probe*")))

    def test_committed_legacy_true_does_not_skip_probe_without_local_marker(self):
        import contextlib
        import io
        import types

        original = (
            b"version: 1\nproject: demo\ndelegation:\n"
            b"  enabled: true\n  codex_runner_verified: true\n"
        )
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            self.assertEqual(
                git(root, "ls-files", "--error-unmatch", ".waystone.yml").returncode, 0)
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertEqual((root / ".waystone.yml").read_bytes(), original)
            self.assertTrue((root / ".waystone" / "codex-runner-verified").is_file())
            self.assertEqual(git(
                root, "check-ignore", "--quiet",
                ".waystone/codex-runner-verified").returncode, 0)
            self.assertIn("legacy delegation.codex_runner_verified", stderr.getvalue())
            self.assertIn("remove the key from", stderr.getvalue())

    def test_existing_local_marker_skips_probe(self):
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            state = root / ".waystone"
            state.mkdir()
            (state / ".gitignore").write_text("*\n")
            (state / "codex-runner-verified").write_text(self._proof_text())
            calls = {"runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                raise AssertionError("existing local marker must skip the probe")

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                self.assertEqual(delegate._run_codex(
                    worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"runner": 1})

    def test_darwin_unobserved_process_context_state_equivalent_marker_skips_probe(self):
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            darwin_proof = _json.loads(_json.dumps(self.proof))
            darwin_proof["platform"]["system"] = "Darwin"
            darwin_proof["host_sandbox_observation"] = {
                "source": "none", "status": "not-observed", "platform": "Darwin",
            }
            darwin_proof["process_context"] = delegate._process_security_context("Darwin")
            delegate._codex_runner_fingerprint = lambda _worktree: _json.loads(
                _json.dumps(darwin_proof))
            state = root / ".waystone"
            state.mkdir()
            (state / ".gitignore").write_text("*\n")
            marker = state / "codex-runner-verified"
            recorded = _json.loads(_json.dumps(darwin_proof))
            recorded["process_context"]["security_label"]["reason"] = "not-present"
            marker.write_text(self._proof_text(recorded))
            calls = {"runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                raise AssertionError("equal not-observed process state must reuse the proof")

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            try:
                self.assertEqual(delegate._run_codex(
                    worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text(recorded))

    def test_observed_and_unobserved_process_context_transitions_reprobe(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        unobserved_axis = {
            "source": "/proc/self/status", "status": "not-observed",
            "reason": "not-present",
        }
        observed_axis = {
            "source": "/proc/self/status", "status": "observed", "value": "2",
        }
        for recorded_axis, current_axis in (
                (observed_axis, unobserved_axis), (unobserved_axis, observed_axis)):
            with self.subTest(
                    recorded=recorded_axis["status"], current=current_axis["status"]), \
                 tempfile.TemporaryDirectory() as d:
                root, worktree, record, prompt = self._fixture(Path(d), original)
                current = _json.loads(_json.dumps(self.proof))
                current["process_context"]["Seccomp"] = current_axis
                recorded = _json.loads(_json.dumps(current))
                recorded["process_context"]["Seccomp"] = recorded_axis
                delegate._codex_runner_fingerprint = lambda _worktree: _json.loads(
                    _json.dumps(current))
                state = root / ".waystone"
                state.mkdir()
                (state / ".gitignore").write_text("*\n")
                marker = state / "codex-runner-verified"
                marker.write_text(self._proof_text(recorded))
                calls = {"probe": 0, "runner": 0}
                original_probe = delegate._run_codex_sandbox_probe
                original_run = delegate.subprocess.run

                def probe(*args, **kwargs):
                    calls["probe"] += 1
                    result = self._passed_probe()
                    result["worktree_cache_mount"] = _json.loads(_json.dumps(
                        kwargs["fingerprint"]["worktree_cache_mount"]))
                    return result

                def run(*args, **kwargs):
                    if args and args[0] and args[0][0] == "git":
                        return original_run(*args, **kwargs)
                    calls["runner"] += 1
                    return types.SimpleNamespace(returncode=0)

                delegate._run_codex_sandbox_probe = probe
                delegate.subprocess.run = run
                stderr = io.StringIO()
                try:
                    with contextlib.redirect_stderr(stderr):
                        self.assertEqual(delegate._run_codex(
                            worktree, "gpt-test", prompt, record)[0], 0)
                finally:
                    delegate.subprocess.run = original_run
                    delegate._run_codex_sandbox_probe = original_probe

                self.assertEqual(calls, {"probe": 1, "runner": 1})
                self.assertEqual(marker.read_text(), self._proof_text(current))
                self.assertIn("process_context", stderr.getvalue())

    def test_git_tracked_marker_is_ignored_and_reprobed_with_untrack_guidance(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            marker.write_text(self._proof_text())
            self.assertEqual(git(root, "add", "-f", str(marker)).returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "track invalid proof").returncode, 0)
            self.assertEqual(git(
                root, "ls-files", "--error-unmatch",
                ".waystone/codex-runner-verified").returncode, 0)
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertIn("tracked", stderr.getvalue())
            self.assertEqual(stderr.getvalue().count(
                "git rm --cached -f -- .waystone/codex-runner-verified"), 1)

    def test_tracked_marker_deleted_from_worktree_still_prints_untrack_guidance(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            marker.write_text(self._proof_text())
            self.assertEqual(git(root, "add", "-f", str(marker)).returncode, 0)
            self.assertEqual(git(root, "commit", "-qm", "track invalid proof").returncode, 0)
            marker.unlink()
            self.assertFalse(marker.exists())
            self.assertEqual(git(
                root, "ls-files", "--error-unmatch",
                ".waystone/codex-runner-verified").returncode, 0)
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertEqual(stderr.getvalue().count(
                "git rm --cached -f -- .waystone/codex-runner-verified"), 1)

    def test_staged_invalid_marker_guidance_uses_forced_cached_removal(self):
        import contextlib
        import io
        import types

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            marker = root / ".waystone" / "codex-runner-verified"
            marker.parent.mkdir()
            marker.write_text("invalid staged proof\n")
            self.assertEqual(git(root, "add", "-f", str(marker)).returncode, 0)
            self.assertEqual(git(
                root, "ls-files", "--error-unmatch",
                ".waystone/codex-runner-verified").returncode, 0)
            calls = {"probe": 0, "runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run

            def probe(*args, **kwargs):
                calls["probe"] += 1
                return self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                return types.SimpleNamespace(returncode=0)

            delegate._run_codex_sandbox_probe = probe
            delegate.subprocess.run = run
            stderr = io.StringIO()
            try:
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(delegate._run_codex(
                        worktree, "gpt-test", prompt, record)[0], 0)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls, {"probe": 1, "runner": 1})
            self.assertEqual(marker.read_text(), self._proof_text())
            self.assertEqual(stderr.getvalue().count(
                "git rm --cached -f -- .waystone/codex-runner-verified"), 1)

    def test_invalid_marker_content_is_reprobed_and_atomically_rewritten(self):
        import contextlib
        import io
        import types
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        for content in (b"", b"corrupt\n", b"verified"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as d:
                root, worktree, record, prompt = self._fixture(Path(d), original)
                marker = root / ".waystone" / "codex-runner-verified"
                marker.parent.mkdir()
                (marker.parent / ".gitignore").write_text("*\n")
                marker.write_bytes(content)
                init_repo(root)
                calls = {"probe": 0, "runner": 0}
                original_probe = delegate._run_codex_sandbox_probe
                original_run = delegate.subprocess.run

                def probe(*args, **kwargs):
                    calls["probe"] += 1
                    return self._passed_probe()

                def run(*args, **kwargs):
                    if args and args[0] and args[0][0] == "git":
                        return original_run(*args, **kwargs)
                    calls["runner"] += 1
                    return types.SimpleNamespace(returncode=0)

                delegate._run_codex_sandbox_probe = probe
                delegate.subprocess.run = run
                stderr = io.StringIO()
                try:
                    with mock.patch.object(
                            delegate, "write_text_atomic",
                            wraps=delegate.write_text_atomic) as atomic:
                        with contextlib.redirect_stderr(stderr):
                            self.assertEqual(delegate._run_codex(
                                worktree, "gpt-test", prompt, record)[0], 0)
                        atomic.assert_called_once_with(marker, self._proof_text())
                finally:
                    delegate.subprocess.run = original_run
                    delegate._run_codex_sandbox_probe = original_probe

                self.assertEqual(calls, {"probe": 1, "runner": 1})
                self.assertEqual(marker.read_text(), self._proof_text())
                self.assertIn("invalid", stderr.getvalue())
                self.assertIn("fresh preflight probe", stderr.getvalue())

    def test_probe_failure_does_not_record_marker_and_next_call_reprobes(self):
        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            calls = {"probe": 0}
            original_probe = delegate._run_codex_sandbox_probe

            def probe(*args, **kwargs):
                calls["probe"] += 1
                result = {"schema": "waystone-sandbox-probe-1", "classification": "sandbox"}
                raise delegate._RunnerSandboxUnusable("runner sandbox unusable", result)

            delegate._run_codex_sandbox_probe = probe
            try:
                for _ in range(2):
                    with self.assertRaises(delegate._RunnerSandboxUnusable):
                        delegate._run_codex(worktree, "gpt-test", prompt, record)
            finally:
                delegate._run_codex_sandbox_probe = original_probe

            self.assertEqual(calls["probe"], 2)
            self.assertEqual((root / ".waystone.yml").read_bytes(), original)
            self.assertFalse((root / ".waystone" / "codex-runner-verified").exists())

    def test_marker_write_failure_is_loud_and_does_not_start_runner(self):
        from unittest import mock

        original = b"version: 1\nproject: demo\ndelegation:\n  enabled: true\n"
        with tempfile.TemporaryDirectory() as d:
            root, worktree, record, prompt = self._fixture(Path(d), original)
            calls = {"runner": 0}
            original_probe = delegate._run_codex_sandbox_probe
            original_run = delegate.subprocess.run
            delegate._run_codex_sandbox_probe = lambda *args, **kwargs: self._passed_probe()

            def run(*args, **kwargs):
                if args and args[0] and args[0][0] == "git":
                    return original_run(*args, **kwargs)
                calls["runner"] += 1
                raise AssertionError("main runner must not start")

            delegate.subprocess.run = run
            try:
                with mock.patch.object(
                        delegate, "write_text_atomic", side_effect=OSError("read-only")):
                    with self.assertRaisesRegex(
                            delegate._RunnerProbeFailure, "codex-runner-verified|read-only"):
                        delegate._run_codex(worktree, "gpt-test", prompt, record)
            finally:
                delegate.subprocess.run = original_run
                delegate._run_codex_sandbox_probe = original_probe
            self.assertEqual(calls["runner"], 0)
            self.assertEqual((root / ".waystone.yml").read_bytes(), original)
            self.assertFalse((root / ".waystone" / "codex-runner-verified").exists())
