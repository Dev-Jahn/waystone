"""Mechanically split tests loaded by run_tests.py."""
from __future__ import annotations

from support import *  # noqa: F401,F403


class MigrationSunsetTests(unittest.TestCase):
    def _project(self, d: Path) -> tuple[Path, Path]:
        root = d / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
        home = d / "home"
        home.mkdir()
        return root, home

    def test_pre_0_9_layout_is_refused_with_0_11_x_guidance(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            source = (home / ".claude" / "waystone.pre-0.9" / "start_here" /
                      f"{common._project_slug(root)}.md")
            source.parent.mkdir(parents=True)
            source.write_bytes(b"LEGACY-FRONTIER")

            with self.assertRaises(common.Pre09StateError) as raised:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            message = str(raised.exception)
            self.assertIn("unsupported_pre_0_9_layout", message)
            self.assertIn("pre-0.9", message)
            self.assertIn("0.11.x", message)
            self.assertIn(str(source), message)
            self.assertEqual(source.read_bytes(), b"LEGACY-FRONTIER")
            self.assertFalse((root / ".waystone").exists())

    def test_plain_machine_state_is_refused_without_registry_merge(self):
        with tempfile.TemporaryDirectory() as d:
            home = Path(d) / "home"
            source = home / ".codex" / "waystone" / "projects.json"
            source.parent.mkdir(parents=True)
            source.write_bytes(b'{"projects": [{"repo": "org/legacy"}]}')

            with self.assertRaises(common.Pre09StateError) as raised:
                _run_with_home(home, common.migrate_home_data)

            self.assertIn("0.11.x", str(raised.exception))
            self.assertIn(str(source), str(raised.exception))
            self.assertEqual(source.read_bytes(), b'{"projects": [{"repo": "org/legacy"}]}')
            self.assertFalse((home / ".waystone" / "projects.json").exists())

    def test_pending_worktree_marker_is_refused_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            marker = (home / ".waystone" / "cache" / "worktrees" /
                      common._project_slug(root) / "did-pending.migrating")
            marker.parent.mkdir(parents=True)
            marker.write_bytes(b"/legacy/worktree/did-pending")

            with self.assertRaises(common.Pre09StateError) as raised:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertIn(str(marker), str(raised.exception))
            self.assertIn("does not migrate or repair", str(raised.exception))
            self.assertEqual(marker.read_bytes(), b"/legacy/worktree/did-pending")

    def test_unsafe_marker_ancestor_is_refused_without_repair(self):
        cases = (
            ("cache-symlink", ("cache",), "symlink"),
            ("cache-file", ("cache",), "file"),
            ("worktrees-symlink", ("cache", "worktrees"), "symlink"),
            ("worktrees-file", ("cache", "worktrees"), "file"),
        )
        for name, parts, kind in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as d:
                root, home = self._project(Path(d))
                ancestor = (home / ".waystone").joinpath(*parts)
                ancestor.parent.mkdir(parents=True)
                if kind == "symlink":
                    external = Path(d) / "external-markers"
                    external.mkdir()
                    ancestor.symlink_to(external, target_is_directory=True)
                else:
                    ancestor.write_bytes(b"not-a-marker-directory")

                with self.assertRaises(common.Pre09StateError) as raised:
                    _run_with_home(home, lambda: common.migrate_project_state(root))

                self.assertEqual(raised.exception.paths, (ancestor,))
                if kind == "symlink":
                    self.assertTrue(ancestor.is_symlink())
                    self.assertEqual(list(external.iterdir()), [])
                else:
                    self.assertEqual(ancestor.read_bytes(), b"not-a-marker-directory")
                self.assertFalse((root / ".waystone").exists())

    def test_symlinked_marker_container_is_refused_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            marker_dir = (home / ".waystone" / "cache" / "worktrees" /
                          common._project_slug(root))
            target = Path(d) / "external-markers"
            target.mkdir()
            marker = target / "did-pending.migrating"
            marker.write_bytes(b"/legacy/worktree/did-pending")
            marker_dir.parent.mkdir(parents=True)
            marker_dir.symlink_to(target, target_is_directory=True)

            with self.assertRaises(common.Pre09StateError) as raised:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertEqual(raised.exception.paths, (marker_dir,))
            self.assertTrue(marker_dir.is_symlink())
            self.assertEqual(marker.read_bytes(), b"/legacy/worktree/did-pending")
            self.assertFalse((root / ".waystone").exists())

    def test_regular_file_marker_container_is_refused_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            marker_dir = (home / ".waystone" / "cache" / "worktrees" /
                          common._project_slug(root))
            marker_dir.parent.mkdir(parents=True)
            marker_dir.write_bytes(b"not-a-marker-directory")

            with self.assertRaises(common.Pre09StateError) as raised:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertEqual(raised.exception.paths, (marker_dir,))
            self.assertEqual(marker_dir.read_bytes(), b"not-a-marker-directory")
            self.assertFalse((root / ".waystone").exists())

    def test_divergent_preserved_profiles_are_refused_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            profiles = {
                home / ".claude" / "waystone.pre-0.9" / "profile.yml":
                    b"schema: waystone-profile-1\nbindings: {}\n",
                home / ".codex" / "waystone.pre-0.9" / "profile.yml":
                    b"schema: waystone-profile-1\nbindings:\n  reviewer: gpt-other\n",
            }
            for path, body in profiles.items():
                path.parent.mkdir(parents=True)
                path.write_bytes(body)

            with self.assertRaises(common.Pre09StateError) as raised:
                _run_with_home(home, lambda: common.migrate_project_state(root))

            self.assertEqual(set(raised.exception.paths), set(profiles))
            for path, body in profiles.items():
                self.assertEqual(path.read_bytes(), body)
            self.assertFalse((root / ".waystone" / "profile.yml").exists())

    def test_preserved_profile_mismatching_live_is_accepted_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            preserved_body = b"schema: waystone-profile-1\nbindings: {}\n"
            live_body = b"schema: waystone-profile-1\nbindings:\n  reviewer: live-other\n"
            profiles = []
            for host in (".claude", ".codex"):
                profile = home / host / "waystone.pre-0.9" / "profile.yml"
                profile.parent.mkdir(parents=True)
                profile.write_bytes(preserved_body)
                profiles.append(profile)
            live = root / ".waystone" / "profile.yml"
            live.parent.mkdir()
            live.write_bytes(live_body)

            self.assertFalse(_run_with_home(
                home, lambda: common.migrate_project_state(root)))

            for profile in profiles:
                self.assertEqual(profile.read_bytes(), preserved_body)
            self.assertEqual(live.read_bytes(), live_body)

    def test_preserved_profile_without_live_is_accepted_without_repair(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            preserved = home / ".claude" / "waystone.pre-0.9" / "profile.yml"
            preserved_body = b"schema: waystone-profile-1\nbindings: {}\n"
            preserved.parent.mkdir(parents=True)
            preserved.write_bytes(preserved_body)

            self.assertFalse(_run_with_home(
                home, lambda: common.migrate_project_state(root)))

            self.assertEqual(preserved.read_bytes(), preserved_body)
            self.assertFalse((root / ".waystone" / "profile.yml").exists())

    def test_completed_0_11_seed_and_empty_scaffolding_are_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            profile = b"schema: waystone-profile-1\nbindings: {}\n"
            live = root / ".waystone" / "profile.yml"
            live.parent.mkdir()
            live.write_bytes(profile)
            slug = common._project_slug(root)
            for host in (".claude", ".codex"):
                preserved = home / host / "waystone.pre-0.9"
                preserved.mkdir(parents=True)
                (preserved / "profile.yml").write_bytes(profile)
                (preserved / "projects.json").write_text('{"projects": []}')
            (home / ".claude" / "waystone" / "worktrees" / slug).mkdir(parents=True)

            self.assertFalse(_run_with_home(
                home, lambda: common.migrate_project_state(root)))
            self.assertEqual(live.read_bytes(), profile)


class MigrationV2HookTests(unittest.TestCase):
    def _module(self):
        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context
        return session_context

    def _project(self, d: Path):
        root = d / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: demo\n")
        (root / "tasks.yaml").write_text("version: 1\nproject: demo\ntasks: []\n")
        home = d / "home"
        home.mkdir()
        return root, home

    def _run_context(self, module, root: Path, home: Path):
        import contextlib
        import io

        old_argv = sys.argv
        out, err = io.StringIO(), io.StringIO()
        try:
            sys.argv = ["session_context.py", str(root)]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = _run_with_home(home, module.main)
        finally:
            sys.argv = old_argv
        return rc, _json.loads(out.getvalue()), err.getvalue()

    def test_hook_warns_and_preserves_unsupported_legacy_source(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            slug = common._project_slug(root)
            source = home / ".claude" / "waystone" / "start_here" / f"{slug}.md"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"HOOK-PLAINTEXT-FRONTIER")
            rc, payload, err = self._run_context(module, root, home)
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertNotIn(
                "HOOK-PLAINTEXT-FRONTIER",
                payload["hookSpecificOutput"]["additionalContext"])
            self.assertIn("unsupported_pre_0_9_layout", err)
            self.assertIn("0.11.x", err)
            self.assertEqual(source.read_bytes(), b"HOOK-PLAINTEXT-FRONTIER")

    def test_hook_migration_failure_warns_but_always_emits_json_context(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original = module.migrate_project_state
            module.migrate_project_state = lambda _root: (_ for _ in ()).throw(
                common.WorkflowError("migration exploded"))
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                module.migrate_project_state = original
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("migration exploded", err)
            self.assertIn("migration", err.lower())

    def test_hook_acquires_registry_then_project_with_one_three_second_budget(self):
        import contextlib

        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original_hold = getattr(module, "hold_lock", None)
            original_hold_project = getattr(module, "hold_project_lock", None)
            original_time = getattr(module, "time", None)
            seen = []
            ticks = iter((100.0, 100.0, 101.25))

            @contextlib.contextmanager
            def tracked(path, timeout=None):
                seen.append((Path(path), timeout))
                yield

            def tracked_project(project, timeout=None):
                return tracked(common.project_lock_path(project), timeout=timeout)

            class FakeTime:
                @staticmethod
                def monotonic():
                    return next(ticks)

                @staticmethod
                def time_ns():
                    return 1

            module.hold_lock = tracked
            module.hold_project_lock = tracked_project
            module.time = FakeTime
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                if original_hold is None:
                    del module.hold_lock
                else:
                    module.hold_lock = original_hold
                if original_hold_project is None:
                    del module.hold_project_lock
                else:
                    module.hold_project_lock = original_hold_project
                if original_time is None:
                    del module.time
                else:
                    module.time = original_time
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertEqual(err, "")
            self.assertEqual([path.resolve() for path, _timeout in seen], [
                (home / ".waystone" / "registry.lock").resolve(),
                common.project_lock_path(root).resolve(),
            ])
            self.assertEqual([timeout for _path, timeout in seen], [3.0, 1.75])

    def test_hook_registry_lock_failure_warns_without_running_migration(self):
        import contextlib

        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original_hold = module.hold_lock
            original_migrate = module.migrate_project_state
            migrated = []

            @contextlib.contextmanager
            def blocked(_path, timeout=None):
                raise common.WorkflowError("synthetic registry lock timeout")
                yield

            module.hold_lock = blocked
            module.migrate_project_state = lambda _root: migrated.append(True)
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                module.hold_lock = original_hold
                module.migrate_project_state = original_migrate
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertEqual(migrated, [])
            self.assertIn("synthetic registry lock timeout", err)

    def test_hook_oserror_warns_but_always_emits_json_context(self):
        module = self._module()
        with tempfile.TemporaryDirectory() as d:
            root, home = self._project(Path(d))
            original = module.migrate_project_state
            module.migrate_project_state = lambda _root: (_ for _ in ()).throw(
                OSError("migration filesystem exploded"))
            try:
                rc, payload, err = self._run_context(module, root, home)
            finally:
                module.migrate_project_state = original
            self.assertEqual(rc, 0)
            self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
            self.assertIn("migration filesystem exploded", err)


class MigrationTests(unittest.TestCase):
    def test_legacy_profile_and_delta_schema_load(self):
        with tempfile.TemporaryDirectory() as d:
            root, home = _overlay_project(d)
            _write_profile(root, _PROFILE_BODY.replace("waystone-profile-1", "jw-profile-1"))
            profile, _ = _run_with_home(home, lambda: delegate._load_profile(root))
            self.assertEqual(profile["schema"], "jw-profile-1")
            delta = _add_delta(root, home, "verification_debt/legacy")
            path = _run_with_home(home, lambda: overlay._delta_path(root, delta["id"]))
            record = _json.loads(path.read_text())
            record["schema"] = "jw-delta-1"
            path.write_text(_json.dumps(record))
            loaded = _run_with_home(home, lambda: overlay.active_deltas_for_exposure(root))
            self.assertEqual(loaded[0]["schema"], "jw-delta-1")

    def test_legacy_review_marker_reads_and_new_marker_writes(self):
        legacy = "<!-- jw-review-cycle:v1\ncycle: 7\ntarget_sha: abc\n-->"
        parsed = review.parse_markers(legacy)
        self.assertEqual(parsed[0]["_kind"], "review-cycle")
        self.assertTrue(review.emit_marker("review-cycle", {"cycle": 8}).startswith(
            "<!-- waystone-review-cycle:v1"))

    def test_init_skill_uses_current_managed_markers(self):
        text = (SCRIPTS.parent / "skills" / "init" / "SKILL.md").read_text()
        for marker in ("<!-- waystone:begin -->", "<!-- waystone:end -->"):
            self.assertIn(marker, text)
        self.assertIn("never duplicate", text.lower())
        for surface in (
            "waystone consent record install.agents accept",
            "waystone consent record install.hooks accept",
            "waystone install agents", "waystone install hooks",
            "agent file is left uncommitted", ".waystone/boundary-hooks-enabled",
        ):
            self.assertIn(surface, text)


class M2DocsTests(unittest.TestCase):
    """0.8.0 M2 C7 — guided skills and public operating-surface documentation."""

    def test_delegate_skill_preserves_provenance_and_recorded_acceptance(self):
        text = (SCRIPTS.parent / "skills" / "delegate" / "SKILL.md").read_text()
        self.assertIn("name: delegate", text)
        self.assertIn("/waystone:delegate", text)
        for phrase in (
            "delegate-claimed", "independent-verifier", "delegate verify", "verdict",
            "main-session", "--reason", "Escalation", "apply", "discard", "runner.jsonl",
        ):
            self.assertIn(phrase, text)
        self.assertNotIn("AskUserQuestion", text)
        escalation = text.split("## Escalation table", 1)[1].split("## ", 1)[0]
        rows = [line for line in escalation.splitlines()
                if line.startswith("| ") and line.split("|", 2)[1].strip().isdigit()]
        self.assertEqual(len(rows), 10)
        for meaning in (
            "owner-authored", "profile is missing", "unresolved blocker", "Two run attempts",
            "after one retry", "Apply drift", "runner failure is deterministic",
            "waystone warn conflict", "--allow-unsandboxed-runner --reason",
            "user explicitly requested review",
        ):
            self.assertIn(meaning, escalation)
        self.assertIn("These are the only escalation cases. Otherwise, do not ask", escalation)
        self.assertIn("When a verifier binding exists, always run it", text)
        self.assertIn("Allow at most two total run attempts", text)
        self.assertIn("implementer` + `external-runner", text)
        self.assertIn("waystone task set <task-id> --scope-add", text)
        self.assertIn("host's native main-session", text)

    def test_delegate_report_summarizes_warnings_without_internal_delta_ids(self):
        text = (SCRIPTS.parent / "skills" / "delegate" / "SKILL.md").read_text()
        report = text.split("## Step 6", 1)[1].split("## Escalation table", 1)[0]
        self.assertIn("plain-language meaning", report)
        self.assertNotIn("verbatim", report)
        self.assertIn("warnings_seen", text)
        self.assertIn("verdict-input-schema.json", text)
        self.assertIn("verdict-schema.json", text)

    def test_improve_skill_has_current_lenses_metrics_and_consent_flows(self):
        text = (SCRIPTS.parent / "skills" / "improve" / "SKILL.md").read_text()
        self.assertIn("Step 3.5", text)
        self.assertIn("verification_debt/*", text)
        self.assertIn("delegation-verification-evidence-v1", text)
        self.assertIn("review_association/*", text)
        self.assertIn("round-close-open-findings-v1", text)
        for lens in (
            "delegation_opportunity", "worker_scope_drift", "warn_friction",
            "env_unpreparedness", "adaptive_feedback", "finding_concentration",
        ):
            self.assertIn(f"`{lens}`", text)
        self.assertIn("recommendation_tier: always-allowed", text)
        self.assertIn("evidence-strength label", text)
        self.assertIn("waystone improve metrics", text)
        self.assertIn("unavailable_reason", text)
        self.assertIn("previous/current/delta", text)
        self.assertIn("waystone overlay add", text)
        self.assertIn("waystone overlay promote-user", text)
        self.assertIn("waystone consent record materialize accept", text)
        self.assertIn("waystone overlay materialize", text)
        self.assertIn("Never write delta JSON", text)
        self.assertIn("prevented", text)
        self.assertIn("improved", text)
        self.assertIn("benefit", text)

    def test_readme_and_front_door_name_all_new_surfaces(self):
        readme = (SCRIPTS.parent / "README.md").read_text()
        for surface in (
            "waystone paths", "waystone project", "waystone overlay", "waystone check",
            "waystone improve evidence", "waystone delegate verify", "waystone delegate verdict",
            "waystone task set <id> --scope-add <prefix>", "waystone improve metrics",
            "waystone overlay compose", "waystone overlay promote-user",
            "waystone overlay materialize", "waystone consent record", "waystone install",
            "waystone delegate plan --json", "waystone statusline",
            "waystone install statusline",
        ):
            self.assertIn(f"`{surface}`", readme)
        self.assertIn("**v0.10 — Bind & Compose**", readme)
        self.assertIn("Implemented — current release", readme)
        import waystone
        for surface in (
            "improve", "evidence", "metrics", "delegate", "verify", "overlay", "promote-user",
            "materialize", "compose", "consent", "install", "scope-add", "check",
        ):
            self.assertIn(surface, waystone.__doc__)
        for surface in ("paths", "project"):
            self.assertIn(surface, waystone.__doc__)

    def test_waystone_bin_front_door(self):
        wrapper = SCRIPTS.parent / "bin" / "waystone"
        self.assertTrue(wrapper.is_file())
        self.assertTrue(wrapper.stat().st_mode & 0o111)
        self.assertIn('exec uv run "$here/../scripts/waystone.py" "$@"', wrapper.read_text())
        for skill in (SCRIPTS.parent / "skills").glob("*/SKILL.md"):
            text = skill.read_text()
            self.assertNotIn("<plugin-root>", text)
            self.assertNotIn("scripts/waystone.py", text)

    def test_conventions_state_overlay_evidence_invariants_and_residence(self):
        text = (SCRIPTS.parent / "references" / "conventions.md").read_text()
        for phrase in (
            "non-blocking", "least-restrictive", "task-id", "estimated nuisance rate",
            "workspace-write", "read-only", "harness-computed", "delegate-claimed",
            "independent-verifier", "main-session", "waystone delegate verdict",
            "{project_root}/.waystone/overlay/", "{project_root}/.waystone/exposure/",
            "{project_root}/.waystone/improve/evidence.jsonl", "~/.waystone/",
            "~/.waystone/cache/", ".pre-0.9", "git clean -fdx", "waystone paths",
            "~/.waystone/overlay/", ".waystone/maturity.json", ".waystone/consents.jsonl",
            ".waystone/improve/metrics.jsonl", "docs/waystone-policy.yaml", "scope:",
            "waystone task set <id> --scope-add", "{layer, id}", "waystone overlay compose",
            "waystone overlay promote-user", "waystone consent record",
            "waystone overlay materialize", "waystone install agents",
            "waystone improve metrics", "unavailable_reason", "tri-state",
        ):
            self.assertIn(phrase, text)


class CodexHookTests(unittest.TestCase):
    def _project(self, directory: str) -> Path:
        root = Path(directory) / "repo"
        root.mkdir()
        init_repo(root)
        (root / ".waystone.yml").write_text("version: 1\nproject: x\n")
        (root / "tasks.yaml").write_text(TASKS_FIXTURE)
        (root / "ROADMAP.md").write_text("stale\n")
        return root

    def _guard(self, root: Path, payload: dict):
        import os

        script = SCRIPTS.parent / "hooks" / "scripts" / "tasks_guard.sh"
        return subprocess.run(
            ["bash", str(script)], input=_json.dumps(payload), cwd=root,
            capture_output=True, text=True,
            env={**os.environ, "HOME": str(root.parent / "home")},
        )

    def test_claude_and_codex_payloads_regenerate_roadmap(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            claude = {
                "tool_name": "Edit", "cwd": str(root),
                "tool_input": {"file_path": str(root / "tasks.yaml")},
            }
            result = self._guard(root, claude)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual((root / "ROADMAP.md").read_text(), "stale\n")

            (root / "ROADMAP.md").write_text("stale-again\n")
            codex = {
                "tool_name": "apply_patch", "cwd": str(root),
                "tool_input": {"command": "*** Begin Patch\n*** Update File: tasks.yaml\n@@\n*** End Patch"},
            }
            result = self._guard(root, codex)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotEqual((root / "ROADMAP.md").read_text(), "stale-again\n")

    def test_codex_invalid_tasks_patch_fails_without_refresh(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            (root / "tasks.yaml").write_text(TASKS_FIXTURE.replace("status: active", "status: bogus"))
            before = (root / "ROADMAP.md").read_bytes()
            payload = {
                "tool_name": "apply_patch", "cwd": str(root),
                "tool_input": {"command": "*** Begin Patch\n*** Update File: tasks.yaml\n@@\n*** End Patch"},
            }
            result = self._guard(root, payload)
            self.assertEqual(result.returncode, 2)
            self.assertIn("violates the workflow convention", result.stderr)
            self.assertEqual((root / "ROADMAP.md").read_bytes(), before)

    def test_tasks_guard_lock_timeout_warns_skips_regen_and_exits_zero(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            payload = {
                "tool_name": "Edit", "cwd": str(root),
                "tool_input": {"file_path": str(root / "tasks.yaml")},
            }
            before = (root / "ROADMAP.md").read_bytes()
            with common.hold_lock(common.project_lock_path(root), timeout=0.2):
                result = self._guard(root, payload)
            self.assertEqual(result.returncode, 0)
            self.assertEqual((root / "ROADMAP.md").read_bytes(), before)
            self.assertIn("lock", result.stderr.lower())
            self.assertIn("skipping ROADMAP regeneration", result.stderr)

    def test_tasks_guard_oserror_warns_skips_regen_and_exits_zero(self):
        import contextlib
        import io

        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import tasks_guard

        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            payload = {
                "tool_name": "Edit", "cwd": str(root),
                "tool_input": {"file_path": str(root / "tasks.yaml")},
            }
            before = (root / "ROADMAP.md").read_bytes()
            original_hold = tasks_guard.hold_project_lock
            old_stdin = sys.stdin

            @contextlib.contextmanager
            def broken(_root, timeout=None):
                raise OSError("synthetic lock filesystem failure")
                yield

            tasks_guard.hold_project_lock = broken
            sys.stdin = io.StringIO(_json.dumps(payload))
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    rc = tasks_guard.main()
            finally:
                tasks_guard.hold_project_lock = original_hold
                sys.stdin = old_stdin
            self.assertEqual(rc, 0)
            self.assertEqual((root / "ROADMAP.md").read_bytes(), before)
            self.assertIn("synthetic lock filesystem failure", err.getvalue())
            self.assertIn("skipping ROADMAP regeneration", err.getvalue())

    def test_session_context_names_host_instruction_file(self):
        import contextlib
        import io
        import os

        sys.path.insert(0, str(SCRIPTS.parent / "hooks" / "scripts"))
        import session_context

        with tempfile.TemporaryDirectory() as d:
            root = self._project(d)
            home = Path(d) / "home"

            def capture(host: str) -> str:
                old_host = os.environ.get("WAYSTONE_HOST")
                old_argv = sys.argv
                try:
                    if host == "codex":
                        os.environ["WAYSTONE_HOST"] = "codex"
                    else:
                        os.environ.pop("WAYSTONE_HOST", None)
                    sys.argv = ["session_context.py", str(root)]
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        _run_with_home(home, session_context.main)
                    return _json.loads(output.getvalue())["hookSpecificOutput"]["additionalContext"]
                finally:
                    sys.argv = old_argv
                    if old_host is None:
                        os.environ.pop("WAYSTONE_HOST", None)
                    else:
                        os.environ["WAYSTONE_HOST"] = old_host

            self.assertIn("see CLAUDE.md workflow section", capture("claude"))
            codex_context = capture("codex")
            self.assertIn("see AGENTS.md workflow section", codex_context)
            self.assertNotIn("see CLAUDE.md workflow section", codex_context)
